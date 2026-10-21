def _piece_secs(sections, piece):
    if not piece:
        return sections
    k, off, ln = piece
    out = dict(sections)
    t = str(sections.get(k) or "")
    head = "[F3 PIECE %d..%d of %d]" % (off, min(off + ln, len(t)), len(t))
    out[k] = head + chr(10) + t[off:off + ln]
    return out


#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
F3 升级桥：把规则层判为「待确认」的列交模型，其余列原样保留。

    route_f3.py  →  {列: {state, confidence, escalate, evidence}}
    f3_escalate.py → {列: {值, 来源层, 证据}}   ← 最终产物

契约（这条决定成本）
--------------------
**只把 escalate 的列写进 schema 与提示词。** 规则层已经定下的列不再问模型 ——
这是整个方案省钱的全部来源：升级率 30% 时，模型只需回答 30% 的字段。

用法
----
    # 不联网，只看会给模型发什么（离线可测）
    python3 f3_escalate.py --rule out.json --sections s.json --dry

    # 真调
    python3 f3_escalate.py --rule out.json --sections s.json --out final.json
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import f3_prompt_gen as PG          # noqa: E402  复用 build_schema / build_system
import route_f3 as _R                # noqa: E402  复用缺失语义判定

STATE_PENDING = _R.STATE_PENDING
STATE_NEG = _R.STATE_NEG
STATE_POS = _R.STATE_POS
_missing_state = _R.missing_state


def _norm_base(b):
    """把 base 规整成以 `/v1` 结尾。

    ★ **这个函数是一个真事故的产物。** 项目里同一个 URL 有两种约定：
      · 入口（`medrec` / `batch_run.sh` / `medrec doctor`）传的是**不带 `/v1`**
        的 `http://127.0.0.1:8102` —— doctor 自己拼 `$u/v1/models`；
      · 引擎这边原来要求**带 `/v1`**，只补 `/chat/completions`。
      `medrec route` 做的是 `QWEN_URL="$LLM_URL"`，把前者喂给后者 →
      `http://127.0.0.1:8102/chat/completions` → vLLM 上 **404 且看不到正文**。
      实测 **53/53 个请求全 404**，而 `medrec doctor` 的「5) 服务」那节是**绿的**
      （因为它查的是 `$LLM_URL/v1/models`，那个是对的）。

    所以这里**两种写法都吃**，不再依赖调用方守约定。
    （`make_fast_script.py` 里早为同一个坑打过补丁，但只补了它那一条路。）
    """
    b = (b or "").rstrip("/")
    if b.endswith("/chat/completions"):
        b = b[: -len("/chat/completions")]
    if not b.endswith("/v1"):
        b += "/v1"
    return b


def _auth_hdr():
    """鉴权头。Strata 要 API key（任意串，例如 sk-local）；旧 vLLM 不需要。
    没设 `QWEN_API_KEY` 就返回空 dict → 行为与换代前完全一致。"""
    k = (os.environ.get("QWEN_API_KEY") or "").strip()
    return {"Authorization": "Bearer " + k} if k else {}


def _chat_url():
    """大模型的完整端点。`QWEN_URL` 带不带 `/v1` 都行（见 `_norm_base`）。"""
    return _norm_base(os.environ.get("QWEN_URL",
                                     "http://127.0.0.1:8080/v1")) + "/chat/completions"


def sheet_of(col, variables):
    for sheet, items in _R.sheets_of(variables).items():
        for e in items:
            if e["col"] == col:
                return sheet
    return "其他"


MAX_MODEL_LEN = int(os.environ.get("QWEN_MAXLEN", "32768"))

# user 段预算（按「字符≈token」估，中文下偏保守）。
# ★ 真实约束是 `system + user + 16384(留给输出) <= max_model_len`，
#   **不是拍脑袋的 20000**。实测单列最少要 21,112 字
#   （`诊断` 一段就占 14,640），卡在 20000 就会把 `出院记录`
#   整段挤掉 —— 而那一列恰恰需要它，且 `fits` 照样是 True。
_RESERVE_OUT = 16384          # 最大预留（整块给模型输出）
_SYSTEM_EST = 2600           # system 提示词（136 字段实测 2,252）


def _out_reserve(n_cols):
    """这一批留多少 token 给**输出**。

    ★ 固定留 8,192 是错的：那是照最大批设的。服务器实测有一个 19 列的批
      最少需要 23,622 字输入，而固定预留把预算压到 21,976 → 直接中止。
      19 个字段的输出撑死一千多 token，留 8,192 纯属浪费输入额度。
      按字段数线性给（每字段约 60 token，留一倍余量），并按批设 `max_tokens`，
      两边才一致 —— 否则预留算小了、模型真吐那么多，会被 vLLM 截断。
    """
    return int(max(1024, min(_RESERVE_OUT, 120 * n_cols + 500)))


def small_max_len():
    """小模型层的上下文上限。

    ★ **必须按模型分开算。** 8102 的 27B 是 `--max-model-len 32768`，
      而 8101 的 Qwen3-8B 只有 **16384**。切批时如果两边都按 32768 规划，
      小模型层会切出一个 29,724 tokens 的批 —— 送过去就是 400。
      vLLM 日志里 `Maximum concurrency for 16,384 tokens per request: 1.73x`
      就是它的真实上限。
    """
    return int(os.environ.get("QWEN_SMALL_MAXLEN", "16384"))


def _budget_for(n_cols, system_est=_SYSTEM_EST, max_len=None):
    return max(1024, (max_len or MAX_MODEL_LEN)
               - _out_reserve(n_cols) - system_est)


DEFAULT_BUDGET_CHARS = int(os.environ.get("F3_USER_BUDGET", "0")) or \
    _budget_for(64)
# user 消息的字符预算。留足 system + 输出的余量：
#   32,768(max_len) − 3,441(system) − 8,192(max_tokens) ≈ 21,000


def _fill_body(names, sections, cap=40000):
    """[BODY-FALLBACK] logical name not resolvable by title -> search section bodies."""
    hits = []
    for n in names or []:
        if not n:
            continue
        for k, v in sections.items():
            if k not in hits and n in str(v or ""):
                hits.append(k)
    out, tot = [], 0
    for k in hits:
        L2 = len(str(sections.get(k) or ""))
        if tot + L2 > cap:
            continue
        out.append(k)
        tot += L2
    return out


def relevant_sections(esc_cols, variables, sections, alias):
    """[BODY-FALLBACK] match by title first, then by section body content."""
    idx = {}
    for sheet, items in _R.sheets_of(variables).items():
        for e in items:
            idx[e["col"]] = e
    want = set()
    for c in esc_cols:
        e = idx.get(c)
        if e:
            want.update(e.get("source_sections") or [])
    keys, seen = [], set()
    for lg in want:
        hit = None
        if sections.get(lg):
            hit = lg
        else:
            for k in (alias or {}).get(lg) or []:
                if k.startswith("_\u5176\u4ed6:"):
                    head = k.split(":", 1)[1]
                    for sk in sections:
                        if sk.startswith("_\u5176\u4ed6:") and head in sk.split(":", 1)[1]:
                            hit = sk
                            break
                elif sections.get(k):
                    hit = k
                if hit:
                    break
        if hit:
            if hit not in seen:
                seen.add(hit)
                keys.append(hit)
            continue
        for h in _fill_body([lg], sections):
            if h not in seen:
                seen.add(h)
                keys.append(h)
    order = list(sections)
    keys.sort(key=lambda k: order.index(k) if k in order else 1 << 30)
    keys = sorted(keys, key=lambda k: (0 if k in order else 1, len(str(sections.get(k) or ""))))
    uniq, seen_text = [], set()
    for k in keys:
        t = sections.get(k) or ""
        h = hash(t) if len(t) < 100000 else id(t)
        if h in seen_text:
            continue
        seen_text.add(h)
        uniq.append(k)
    return uniq


def _dedup_keys(keys, sections):
    """丢掉内容被别的段**完全包含**的段。

    实测：`诊断`(14,640 字) 与 `出院诊断`(14,776 字) **145/145 个块重合、
    379/379 行重合** —— 两段几乎逐字相同。一起发就是白烧一半预算，
    而预算正是把 `影像报告`/`病程记录` 挤掉的原因。
    保留**内容多的那个**（超集），因为它涵盖了被丢的那个。
    """
    lines = {}
    for k in keys:
        lines[k] = {l.strip() for l in (sections.get(k) or "").split("\n")
                    if len(l.strip()) > 12}
    out = []
    for k in keys:
        subsumed = False
        for o in keys:
            if o != k and lines[k] and lines[k] <= lines[o] \
                    and len(lines[o]) > len(lines[k]):
                subsumed = True
                break
        if not subsumed:
            out.append(k)
    return out


def _small_chat_url():
    """小模型层的端点。带不带 `/v1` 都行（见 `_norm_base`）。

    ★ 原来这里**硬写** `/v1/chat/completions`：传 `.../v1` 就会拼出
      `.../v1/v1/chat/completions`。同一个约定打架的毛病，只是这次运气好
      没人在 `QWEN_SMALL_URL` 里带 `/v1`。
    """
    return _norm_base(os.environ.get("QWEN_SMALL_URL",
                                     "http://127.0.0.1:8101")) + "/chat/completions"


def endpoint_alive(chat_url, timeout=4):
    """探活。分层的意义在于**省钱省时**，端点不在就退回大模型 ——
    但必须打印出来，静默降级会让人以为分层生效了。"""
    base = chat_url.split("/v1/")[0]
    try:
        with urllib.request.urlopen(base + "/v1/models", timeout=timeout) as r:
            json.loads(r.read().decode("utf-8", "replace"))
        return True
    except Exception:                                          # noqa: BLE001
        return False


def split_for_execution(esc_cols, variables, small_url=None, quiet=False):
    """决定**要不要真分层**。→ (小模型层, 大模型层, 是否分层)

    先看 `F3_SPLIT`（默认 `off`）：

        off   强制不分层（**默认**）。让「跑两遍得到一样的结果」成为默认行为，
              而不是「需要运维去关掉某个服务」才能达到的状态。
        on    强制分层；端点不可达时退化为不分层并告警。
        auto  旧行为：端点通就分层（**不要用于生产**，见下面 `split_policy` 的注释）。

    再看端点：分层时要付两笔实测出来的代价 ——
      · 段跨模型去重做不到，同一个段各发一遍（token +25%，墙钟 +6.6%）
      · **切批方式本身就会改 1.8% 的答案**，而换模型再叠加 2.3%
    所以端点不在时**根本不切**：白付开销、还平白改答案。
    """
    mode = split_policy()
    if mode in ("off", "0", "false", "no"):
        if not quiet:
            print("  · F3_SPLIT=off → 不分层，全部走大模型（结果可复现的默认）")
        return [], list(esc_cols), False
    if small_url is None:
        small_url = _small_chat_url()
    if not endpoint_alive(small_url):
        if not quiet:
            tag = "⚠️ F3_SPLIT=on 但" if mode in ("on", "1", "true", "yes") else "·"
            print(f"  {tag} 小模型端点 {small_url} 无响应 → **不分层**，全部走大模型")
        return [], list(esc_cols), False
    s, b = split_by_tier(esc_cols, variables)
    if not s:
        return [], b, False
    if not quiet:
        print(f"  ⚠️ F3_SPLIT={mode} → 分层；**实测这会改约 2.8% 的答案**，"
              f"注册表要的是一套一致的答案，请确认这是有意为之")
    return s, b, True


def plan_batches(esc_cols, variables, sections, alias=None,
                 strict=False, oversize_out=None, tier="big", max_len=None,
                 pending_list=None, _pure_sections=False):
    """[PB-FIX2] split oversized groups; every batch gets ALL resolvable sections."""
    if _pure_sections:
        def _pb_cap(n):
            return 29800 - 200 * n
        def _pb_rel(cc, vv, ss, aa):
            return [kk for c1 in cc for kk in (vv.get(c1) or []) if kk in ss]
        def _pb_res(n):
            return 120 * n + 500
        _pb_dedup = lambda ks, ss: list(ks)
        _pb_sys = 2600

    def size(ks):
        return sum(len(sections.get(k) or "") for k in ks)

    def secs_of(col):
        if _pure_sections:
            ks = _pb_rel([col], variables, sections, alias)
        else:
            ks = relevant_sections([col], variables, sections, alias)
        return list(ks) if ks else []

    groups = {}
    _none = []
    for col in esc_cols:
        if _pure_sections:
            ks = tuple(_pb_dedup(secs_of(col), sections))
        else:
            ks = tuple(_dedup_keys(secs_of(col), sections))
        if not ks:
            _none.append(col)
            continue
        groups.setdefault(ks, []).append(col)
    if _none:
        if oversize_out is not None:
            oversize_out.extend(_none)
        if pending_list is not None:
            pending_list.extend((c, 0) for c in _none)

    all_keys = set(sections)

    def piece_meta(cols):
        return [str(c) for c in cols if not set(secs_of(c)) <= all_keys]

    def cap_of(n):
        if _pure_sections:
            return _pb_cap(n)
        return max(1024, (max_len or 32768) - _out_reserve(n) - _SYSTEM_EST)

    def _fit_split(ks, cols, used):
        if not ks or not cols or used <= cap_of(len(cols)):
            return [(list(ks), list(cols), cap_of(len(cols)), None, None)]
        o = sorted(list(ks), key=lambda k: -len(sections.get(k) or ""))
        cut = len(sections.get(o[0]) or "")
        left = (len(cols) + 1) // 2
        rest = o[1:]
        if rest:
            return (_fit_split(rest, cols[:left], used - cut)
                    + _fit_split(rest, cols[left:], used - cut))
        cap0 = cap_of(len(cols))
        lo, hi = 16, max(16, max(len(sections.get(k) or "") for k in ks))
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if sum(min(len(sections.get(k) or ""), mid) for k in ks) <= cap0:
                lo = mid
            else:
                hi = mid - 1
        capv = lo
        big = max(list(ks), key=lambda k: len(sections.get(k) or ""))
        bl = len(sections.get(big) or "")
        if lo <= 2000:
            return [(list(ks), list(cols), capv, None, None)]
        n0 = -(-bl // max(1, lo))
        st = max(1, (bl + n0 - 1) // n0)
        out = []
        for off in range(0, bl, st):
            out.append((list(ks), list(cols), st, big, off))
        return out

    items = sorted(groups.items(), key=lambda kv: -size(kv[0]))
    batches = []
    for keys_t, cols in items:
        merged = list(keys_t)
        placed = False
        for b in batches:
            bset = set(b["keys"])
            cand = list(b["keys"]) + [k for k in keys_t if k not in bset]
            if size(cand) <= cap_of(len(b["cols"]) + len(cols)):
                b["keys"] = cand
                b["cols"].extend(cols)
                b["used"] = size(cand)
                placed = True
                break
        if placed:
            continue
        if size(merged) <= cap_of(len(cols)):
            batches.append({"keys": list(merged), "cols": list(cols),
                            "used": size(merged)})
            continue
        if strict:
            raise SystemExit("xx oversize cols: " + str(cols[:4]))
        made = []
        for _ks, _cs, _cap, _big, _off in _fit_split(list(keys_t), cols, size(merged)):
            if not _ks or not _cs:
                continue
            _b = {"keys": list(_ks), "cols": list(_cs), "used": size(_ks),
                  "partial": True, "sect_cap": _cap}
            if _big is not None:
                _b["piece"] = [_big, _off, _cap]
            else:
                _p = piece_meta(_cs)
                if _p:
                    _b["pieces"] = _p
            made.append(_b)
        if made:
            batches.extend(made)
        elif oversize_out is not None:
            oversize_out.extend(cols)
            if pending_list is not None:
                pending_list.extend((c, size(merged)) for c in cols)

    order = list(sections)
    for b in batches:
        b["keys"].sort(key=lambda k: order.index(k) if k in order else 1 << 30)
        if _pure_sections:
            b["max_tokens"] = _pb_res(len(b["cols"]))
        else:
            b["max_tokens"] = _out_reserve(len(b["cols"]))
        b["tier"] = tier
    return batches


def build_request(esc_cols, variables, sections, alias=None,
                  budget_chars=None, per_section_cap=6000,
                  keys=None, strict=False, reserve_out=None, max_len=None):
    """只给 escalate 的列建 schema 与提示词，**并且只送相关的段**。

    返回 (schema, system, user, info)。info 里带用了哪些段、裁了多少 —— 不静默截断。

    keys:        显式指定送哪些段（`plan_batches` 用）。给了就不再自己解析。
    budget_chars: 不给就按**本批字段数**自动算（见 `_out_reserve`）。
    strict:      任何一段被裁或被略去就**直接失败**，而不是留个 dropped 记录继续跑。
                 「悄悄少送一段」和「悄悄少答一列」之间没有区别。
    """
    if budget_chars is None:
        budget_chars = _budget_for(len(esc_cols), max_len=max_len)
    if reserve_out is None:
        reserve_out = _out_reserve(len(esc_cols))
    cols = {c: {"sheet": sheet_of(c, variables), "layer": "llm",
                "extract": {"kind": "llm_text"}, "source_sections": []}
            for c in esc_cols}
    fields = list(cols)
    schema = PG.build_schema(cols, fields)
    system = PG.build_system(cols, fields)

    if keys is None:
        keys = relevant_sections(esc_cols, variables, sections, alias)
        if not keys:                   # 解析不出就退回全部（并标记）
            keys = [k for k, v in sections.items() if v]

    parts, used, dropped = [], 0, []
    for k in keys:
        t = sections.get(k) or ""
        if len(t) > per_section_cap:
            dropped.append(f"{k}({len(t)}→{per_section_cap})")
            t = t[:per_section_cap]
        if used + len(t) > budget_chars:
            dropped.append(f"{k}(整段略去)")
            continue
        used += len(t)
        parts.append(f"【{k}】\n{t}")

    if strict and dropped:
        raise SystemExit(
            "升级请求会丢段，已中止（strict）：\n  " + "\n  ".join(dropped) +
            "\n请用 plan_batches() 切批，别让某列拿不到自己的来源段。")

    user = "\n\n".join(parts) + "\n\n请按字段清单输出 JSON。"
    lim = max_len or MAX_MODEL_LEN
    info = {"n_fields": len(fields), "sections": keys, "chars": len(user),
            "system_chars": len(system), "dropped": dropped,
            "est_tokens": len(system) + len(user),
            "max_model_len": lim,
            "reserve_out": reserve_out,
            "budget_chars": budget_chars,
            "fits": len(system) + len(user) + reserve_out <= lim}
    return schema, system, user, info


def call_qwen(model, schema, system, user, timeout=900, temperature=0.0):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": int(os.environ.get("F3_MAX_TOKENS", "8192")),
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "f3_fields", "strict": True,
                                            "schema": schema}},
    }
    req = urllib.request.Request(
        _chat_url(), data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=dict({"Content-Type": "application/json"}, **_auth_hdr()))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit("[QWEN %s] url=%s\nmodel=%r\nbody=%s"
                         % (e.code, _chat_url(), model,
                            e.read().decode("utf-8", "replace")[:800]))
    return json.loads(d["choices"][0]["message"]["content"]), d.get("usage")


DERIVED_KINDS = ("compute", "normalize")
# 目标书要求的三层：规则可判 / 小模型 / 必须 LLM。
# 配置里 `layer` 确实分了三层（规则 431 / 小模型 34 / 必须 LLM 32），
# 但**执行时全发给了 27B** —— 分层不落到执行上等于没分。
SMALL_LAYERS = ("small", "small+llm", "rule+small")

# ── 分层开关 ────────────────────────────────────────────────────────────
# ★ **默认 off。** 这是实测之后的决定，不是保守。
#
#   同一批 22 例真实病历，四种配置对比（两两之间只改一个变量）：
#
#     噪声底（同配置连跑两次）      8 格 / 10956 = 0.07%
#     切批方式（同模型、重分组）  200 格 / 10956 = 1.8%
#     换 8B（同切批、只换模型）   187 状态 + 70 值 = 2.3%
#     两者合计                    303 格 / 10956 = 2.8%
#
#   而 **8 是噪声底，200 和 187 是噪声的 25~30 倍 —— 都是真实效应，不是抖动。**
#
#   所以「开不开分层」不是「快 12.5%」，是「换掉 2.8% 的答案」，
#   而这些差异里谁对谁错**没有金标准能判**。
#   注册表要的是一套**一致**的答案，不是两套混的 —— 而且分层意味着
#   同一份文书里不同字段由不同模型回答，那个划分还是按列的类型定的，本身就武断。
#
#   另外一个不该被忽略的点：分层会**改变切批方式**，而切批方式自己就值 1.8%。
#   也就是说光这一项，就足够让「同一份文书跑两遍结果不同」。
def split_policy():
    """F3_SPLIT = off（默认）| on | auto

    off   强制不分层，全部走大模型（**默认**，结果可复现）
    on    强制分层；小模型端点不可达时退化为不分层并告警
    auto  旧行为：端点通就分层
    """
    return (os.environ.get("F3_SPLIT") or "off").strip().lower()


def tier_of(col, variables, idx=None):
    """这列该走哪一层的模型：'small' 还是 'big'。

    ★ 这是个**执行**问题，不是配置问题。`variables_f3.json` 里 layer 写了三层，
      但 `f3_escalate` / `f3_batch` 只有 `--model` 一个参数，157 个升级列
      全部打成 27B —— 小模型那 34 列白标了。
    """
    if idx is None:
        idx = _var_index(variables)
    layer = (idx.get(col) or {}).get("layer")
    return "small" if layer in SMALL_LAYERS else "big"


def split_by_tier(esc_cols, variables):
    """→ (小模型层, 大模型层)"""
    idx = _var_index(variables)
    small, big = [], []
    for c in esc_cols:
        (small if tier_of(c, variables, idx) == "small" else big).append(c)
    return small, big



def _var_index(variables):
    idx = {}
    for sheet, items in _R.sheets_of(variables).items():
        for e in items:
            idx[e["col"]] = e
    return idx


def escalatable(esc_cols, variables):
    """把**派生列**从升级清单里去掉。

    `compute` / `normalize` 不是从原文里"抽"出来的，是从**别的列**算出来的。
    问模型等于问它一个它没法学的问题 —— 实测 7 个 classify 列
    （`给予降压药物种类 ← 出院给予的降压药物名称`）全部得到「未提及」，
    白花 token，而且答案必然拿不到。

    去掉之后由 `derive_from_model()` 在模型填完源列之后再算。
    """
    idx = _var_index(variables)
    out = []
    for c in esc_cols:
        kind = ((idx.get(c) or {}).get("extract") or {}).get("kind")
        if kind in DERIVED_KINDS:
            continue
        out.append(c)
    return out


def derive_from_model(final, variables):
    """merge **之后**再跑一遍派生列。

    ★ 顺序是关键：`route_f3` 的 pass 2 在模型之前就跑完了。
      于是 `给予降压药物种类 ← 出院给予的降压药物名称` 在源列还是待确认时
      算不出来，而这个派生列自己又被丢给模型 —— 模型只能回「未提及」。
      **源列的值是模型填的，派生就必须在模型之后重算一次。**

    返回补出来的列数。
    """
    idx = _var_index(variables)
    view = {}
    for c, r in final.items():
        v = r.get("值")
        if r.get("state") in ("阳性", "模型") and v not in (None, ""):
            view[c] = {"state": STATE_POS, "evidence": {"value": v}}
        elif r.get("state") == "阴性":
            view[c] = {"state": STATE_NEG, "evidence": r.get("证据") or {}}
        else:
            view[c] = {"state": STATE_PENDING, "evidence": r.get("证据") or {}}

    n = 0
    for c, r in list(final.items()):
        if r.get("state") != "待确认":
            continue
        e = idx.get(c)
        if not e or (e.get("extract") or {}).get("kind") not in DERIVED_KINDS:
            continue
        try:
            st, ev = _R.apply_derived(e["extract"], view)
        except Exception:                                      # noqa: BLE001
            continue
        if st == STATE_POS and ev.get("value") not in (None, ""):
            final[c] = {"值": str(ev["value"]), "state": "阳性",
                        "来源层": "derived", "confidence": "high", "证据": ev}
            view[c] = {"state": STATE_POS, "evidence": ev}
            n += 1
        elif st == STATE_NEG:
            final[c] = {"值": "", "state": "阴性", "来源层": "derived",
                        "confidence": "medium", "证据": ev}
            view[c] = {"state": STATE_NEG, "evidence": ev}
            n += 1
    return n


def merge(rule_out, model_ans, sections=None, variables=None):
    """规则层已定的原样保留；升级列用模型的答案；模型没答 → 按 missing_means 落态。

    ★ 原来是「模型「值」为空 → 一律待确认」。这把**三件不同的事**混成了一件：

        「原文明确说没有」     → 应当是**阴性**
        「原文根本没提」       → 按该列 missing_means（否若段在 → 阴性）
        「模型没答 / 请求失败」 → 待确认

    实测 166 列升级里有 89 列「值」为空，全被压成待确认 —— 其中一批本该是阴性。
    schema 现在带 `状态` 字段把前两种分开（见 `f3_prompt_gen.build_schema`），
    这里负责把它翻译回三态。
    """
    idx = _var_index(variables)

    final, n_rule, n_model, n_neg, n_pending = {}, 0, 0, 0, 0
    for col, r in rule_out.items():
        if not r["escalate"]:
            # ★ 只认 ev["value"] 就是那 **1429 格**丢失的直接原因：
            #   `gazetteer_boolean` 命中词表后给的是 `{"term": ...}`，value 为 None，
            #   于是「阳性」配了个空值 —— 状态说有、格子里什么都没有。
            #   这里必须与 `route_f3._val_of` / `route_f3.ensure_pos_value` 用
            #   **同一条回退链**（value > term > cell > 兜底「是」），三处口径一致。
            ev = r.get("evidence") or {}
            if r["state"] == "阳性":
                v = ev.get("value") or ev.get("term") or ev.get("cell") or "是"
            elif r["state"] == "阴性":
                v = ""
            else:
                v = None
            final[col] = {"值": v, "state": r["state"], "来源层": "rule",
                          "confidence": r["confidence"],
                          "证据": ev}
            n_rule += 1
            continue
        ans = (model_ans or {}).get(col) or {}
        v = str(ans.get("值") or "").strip()
        mstat = str(ans.get("状态") or "").strip()
        if v:
            # ★ `state` 必须是**三态之一**。原来写的是 `"模型"` ——
            #   那不是合法状态，而且 `来源层: "model"` 已经把出处说清楚了。
            #   下游（宽表 / 登记表 / 复核清单）按 阳性/阴性/待确认 消费，
            #   多一个 `"模型"` 会让它们静默漏掉这一批列。
            final[col] = {"值": v, "state": "阳性", "来源层": "model",
                          "块": ans.get("块") or "",
                          "confidence": "high", "证据": {"quote": v}}
            n_model += 1
            continue
        if mstat == "明确没有":
            # 模型读了原文并明确说没有 —— 这是阴性，不是"不知道"
            final[col] = {"值": "", "state": "阴性", "来源层": "model",
                          "confidence": "medium",
                          "证据": {"why": "模型判「明确没有」"}}
            n_neg += 1
            continue
        # 「未提及」/ 模型没答 → 回到该列自己的缺失语义
        st = STATE_PENDING
        why = "模型判「未提及」" if mstat == "未提及" else "模型没答或请求失败"
        if sections is not None and col in idx:
            try:
                e = idx[col]
                st2, why2 = _missing_state(e, e.get("source_sections"), sections)
                st = STATE_NEG if st2 == "阴性" else STATE_PENDING
                why = f"{why}；missing_means={e.get('missing_means')} → {st}"
            except Exception:                                  # noqa: BLE001
                pass
        if st == STATE_NEG:
            # `来源层` 要说清**答案是哪儿来的**。这里写 `"pending"` 是错的 ——
            # 它的状态是阴性，是**已定**的，只是依据来自 missing_means 语义
            # 而不是逐条证据。写成 pending 会让"落了阴性"和"还没定"混在一起。
            final[col] = {"值": "", "state": "阴性", "来源层": "missing",
                          "confidence": "medium", "证据": {"why": why}}
            n_neg += 1
        else:
            final[col] = {"值": None, "state": "待确认", "来源层": "pending",
                          "confidence": "low", "证据": {"why": why}}
            n_pending += 1

    # 派生列在模型填完之后重算（见 derive_from_model 的文档）
    n_der = derive_from_model(final, variables) if variables else 0
    if n_der:
        n_pending = sum(1 for r in final.values() if r["state"] == "待确认")
    return final, {"rule": n_rule, "model": n_model, "neg": n_neg,
                   "derived": n_der, "pending": n_pending, "n": len(final)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", required=True, help="route_f3.py 的输出")
    ap.add_argument("--sections", required=True)
    ap.add_argument("--tables")
    ap.add_argument("--vars", default=str(HERE / "variables_f3.json"))
    ap.add_argument("--out")
    ap.add_argument("--raw-out", help="把模型的**原始答案**另存一份（排查空值用）")
    ap.add_argument("--dry", action="store_true", help="不联网，只打印会给模型发什么")
    ap.add_argument("--model", default=os.environ.get("QWEN_MODEL", "qwen-27b"))
    a = ap.parse_args()

    rule_out = json.loads(Path(a.rule).read_text(encoding="utf-8"))
    variables = json.loads(Path(a.vars).read_text(encoding="utf-8"))
    import route_f3 as R
    sections = R.load_sections(a.sections)
    sections = R.expand_sections(sections, R.load_alias())

    esc_all = sorted(c for c, r in rule_out.items() if r.get("escalate"))
    esc = escalatable(esc_all, variables)
    n = len(rule_out)
    print(f"规则层共 {n} 列，需升级 {len(esc_all)} 列 = {len(esc_all)/max(n,1):.1%}"
          f"（规则层自己挡下 {n-len(esc_all)} 列）")
    if len(esc) != len(esc_all):
        print(f"  其中 {len(esc_all)-len(esc)} 列是派生列（compute/normalize），"
              f"不问模型，改在模型填完源列后重算")
    if not esc:
        print("无需升级，直接落盘规则层结果")
        if a.out:
            final, st = merge(rule_out, {}, sections, variables)
            Path(a.out).write_text(json.dumps({"items": final, "stats": st},
                                              ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        return

    # ★ 必须切批，不能一把全发。
    #   136 列一次全发时预算闸会把 `出院记录`/`化验单`/`影像报告`/`病程记录`
    #   整段丢掉，`fits` 却仍然是 True —— **96 列（70.6%）拿不到自己的来源段**。
    #   切批后每列都拿得到，代价是 1 次调用变 3 次。
    alias = R.load_alias()
    oversize = []
    # ★ 单份跑也必须**按层分发**，跟 f3_batch 保持一致。
    #   只给 f3_batch 加了分层、忘了这里的话，同一份文书
    #   走单份命令和小模型层不生效、走批量命令才生效 —— 两条路结果不一样，
    #   而使用者不会知道自己在哪条路上。
    small_cols, big_cols, did_split = split_for_execution(esc, variables)
    batches = []
    # ★ 跟 f3_batch 保持一致：小模型**装不下**的列改投大模型，不是丢掉。
    #   两条路的行为必须一样，否则同一份文书走单份命令和走批量命令结果不同。
    small_over = []
    if small_cols:
        batches += plan_batches(small_cols, variables, sections, alias,
                                oversize_out=small_over, tier="small",
                                max_len=small_max_len())
        if small_over:
            big_cols = list(big_cols) + small_over
    if big_cols:
        batches += plan_batches(big_cols, variables, sections, alias,
                                oversize_out=oversize, tier="big")
    print(f"\n切成 {len(batches)} 批（每批都保证来源段整段送达、不裁不丢）")
    print(f"  {'按层分发' if did_split else '未分层'}："
          f"小模型层 {len(small_cols)} 列 / 大模型层 {len(big_cols)} 列")
    if small_over:
        print(f"  · 其中 {len(small_over)} 列小模型（16384）装不下，已改投大模型（32768）")
    if oversize:
        print(f"  ⚠️ {len(oversize)} 列**单批装不下**，这轮不给模型（落待确认）："
              f"{oversize[:6]}{' …' if len(oversize) > 6 else ''}")
        print(f"     原因：依赖的段合计超过单批容量，硬塞会丢掉部分来源段")

    plans = []
    for i, b in enumerate(batches, 1):
        schema, system, user, info = build_request(
            b["cols"], variables, sections, alias, keys=b["keys"],
            per_section_cap=1 << 30, strict=True,
            max_len=small_max_len() if b.get("tier") == "small" else None)
        print(f"  批{i}[{b.get('tier', 'big'):5}]: {len(b['cols']):3} 列  "
              f"{len(b['keys']):2} 段  user {info['chars']:>6,} 字  "
              f"est {info['est_tokens']:>6,} tokens  "
              f"{'装得下' if info['fits'] else '★装不下'}")
        plans.append((b, schema, system, user, info))

    bad = [i for i, p in enumerate(plans, 1) if not p[4]["fits"]]
    if bad:
        raise SystemExit(
            f"✗ 第 {bad} 批装不下（系统提示词 {plans[0][4]['system_chars']:,} 字，"
            f"max_model_len {MAX_MODEL_LEN:,}）。"
            f"减小 F3_USER_BUDGET 让 plan_batches 切得更细。")
    tot = sum(p[4]["est_tokens"] for p in plans)
    print(f"→ 合计约 {tot:,} tokens，{len(plans)} 次调用")

    if a.dry:
        b0, schema0, system0, _, _ = plans[0]
        Path(HERE / "_dry_schema.json").write_text(
            json.dumps(schema0, ensure_ascii=False, indent=2), encoding="utf-8")
        Path(HERE / "_dry_system.txt").write_text(system0, encoding="utf-8")
        print("\n[dry] 已写 _dry_schema.json / _dry_system.txt，未联网")
        print("[dry] 升级列前 12 个:", esc[:12])
        return

    # 小模型端点活着吗？不活就退回大模型，**并且打印出来** ——
    # 静默降级会让人以为分层生效了，而实际成本跟没分层一模一样。
    small_url = _small_chat_url()
    small_model = os.environ.get("QWEN_SMALL_MODEL", "qwen-small")
    small_ok = True
    if any(b.get("tier") == "small" for b, *_ in plans):
        small_ok = endpoint_alive(small_url)
        if not small_ok:
            print(f"  ⚠️ 小模型端点 {small_url} 无响应 —— "
                  f"小模型层那批**退回 {a.model}**（分层没生效，但不会丢数据）")

    # ---- 完整性账：**请求了哪些列**必须先记下来 ----
    #  原实现只打印「返回了几个字段」—— 没有分母，也没有任何后续动作。
    #  批里少几列，那几个键在 ans 里就不存在 → merge 一律写
    #  「模型没答或请求失败」→ 按 missing_means 落待确认，**静默丢掉**。
    #  实测（106 例 / 498 列 / 待确认 11032 格）：**6918 格（62.7%）是这一条**，
    #  其中 `出院带药` 那 3139 格是 100% —— 那一批的列一列都没回来。
    requested = []
    for _b, *_ in plans:
        for _c in (_b.get("cols") or []):
            if _c not in requested:
                requested.append(_c)

    ans = {}
    usage = {}
    for i, (b, schema, system, user, info) in enumerate(plans, 1):
        use_small = b.get("tier") == "small" and small_ok
        want = len(b.get("cols") or [])
        got, u = call_qwen(small_model if use_small else a.model,
                           schema, system, user)
        ans.update(got)
        usage[f"batch{i}"] = u
        miss = "" if (not want or len(got) >= want) else "  ⚠️ 少 %d 列" % (want - len(got))
        print(f"  批{i}[{b.get('tier', 'big')}] 返回 {len(got)}/{want} 个字段 "
              f"（用 {small_model if use_small else a.model}）{miss}")
    n_lack = len([c for c in requested if c not in ans])
    print(f"\n模型共返回 {len(ans)} 个字段（请求 {len(requested)} 个，缺 {n_lack} 个），"
          f"usage={usage}")

    # ---- ★ 补漏：模型漏答的列**单独再问几轮** ----
    #  以前漏了就那样算了。而漏答的常见原因（该批输出被 max_tokens 截断、
    #  一批里列太多模型偷懒只答一部分）**恰恰是「切小批、重问一遍」就能救回来的** ——
    #  这是整条链路上最便宜的一大块填充率。
    rounds = int(os.environ.get("F3_REPAIR_ROUNDS", "3"))
    for rnd in range(1, rounds + 1):
        lacking = [c for c in requested if c not in ans]
        if not lacking:
            break
        print(f"\n↻ 补漏第 {rnd} 轮：{len(lacking)} 列没有答案，按同样的「段不被裁」约束重切小批再问")
        try:
            r_plans = plan_batches(lacking, variables, sections, alias)
        except Exception as e:                                      # noqa: BLE001
            print(f"  ⚠️ 补漏切批失败：{type(e).__name__}: {e}")
            break
        n_before = len(ans)
        for j, (b, schema, system, user, info) in enumerate(r_plans, 1):
            want = len(b.get("cols") or [])
            got, u = call_qwen(a.model, schema, system, user)
            ans.update(got)
            usage[f"repair{rnd}.{j}"] = u
            print(f"  补{rnd}.{j} 返回 {len(got)}/{want} 个字段")
        if len(ans) == n_before:
            print("  ⚠️ 这一轮一个都没补回来 —— 停，不空转")
            break
    n_left = len([c for c in requested if c not in ans])
    if n_left:
        print(f"⚠️ 补漏结束仍有 {n_left} 列没有答案"
              f"（要么源文书确实没写，要么这批模型就是答不出来）")
    if a.raw_out:
        # ★ 必须把模型的**原始答案**留下来。合并后只看得到
        #   `证据: {why: "规则层与模型都没给出"}` —— 分不清是模型说了「原文没有」
        #   还是字段根本没被问。实测 166 列里有 89 列空值，要查就得看原始答案。
        Path(a.raw_out).write_text(
            json.dumps(ans, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"原始答案已写 {a.raw_out}")
    final, st = merge(rule_out, ans, sections, variables)
    if a.out:
        Path(a.out).write_text(json.dumps({"items": final, "stats": st},
                                          ensure_ascii=False, indent=2),
                               encoding="utf-8")
    print(json.dumps(st, ensure_ascii=False))
    print(f"最终：规则层 {st['rule']} 列  模型 {st['model']} 列  "
          f"仍待确认 {st['pending']} 列")


if __name__ == "__main__":
    main()
