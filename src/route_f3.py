#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
F3 规则层执行器（route_f3.py）

读 variables_f3.json（495 列的抽取规格），对一份病历产出逐列三态结果：
    阳性 / 阴性 / 待确认        —— 缺失时落哪一态由该列的 missing_means 决定

设计要点
--------
1. **不依赖 tables.json 的具体 schema。** `flatten_rows()` 把任意嵌套的表结构
   摊平成 [[cell, ...], ...]，`table_lookup` 只在这些 cell 里找词条。等 schema
   到了再写精细适配器，现在不阻塞。
2. **否定门禁复用 F4/F6 的思路**：命中位置往前看 NEG_WINDOW 字。
3. **未实现的 kind 不静默跳过**，而是落 待确认 并把原因写进 note，
   这样它在统计里可见、可计数。
4. `compute` 那 12 列目前是**故意不实现的** —— 覆盖文件里写的是中文表达式
   （"出院日期 − 入院时间"），需要先翻译成可执行算子。它们会全部落 待确认。

用法
----
    python3 route_f3.py --sections <sections.json> [--tables <tables.json>]
                        [--vars variables_f3.json] [--out result.json]
    python3 route_f3.py --selftest
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_POS, STATE_NEG, STATE_PENDING = "阳性", "阴性", "待确认"
CONF_HIGH, CONF_MED, CONF_LOW = "high", "medium", "low"

# ── 门控参数的默认值（会被 variables_f3.json 的 `_gate` 覆盖）─────────────
# ★ F6 把门控阈值 `margin_min` 放在 `variables.json` 里，并且有
#   `gold.py score --sweep` 去扫它。**F3 原来把门控写死在代码里** ——
#   没有任何可调的阈值，也就没法拿金标准去扫。
#   目标书要求「对齐 F6 的 variables.json 结构」，实质就是这条约定：
#   **阈值必须在配置里**。字段名不同没关系（13 题的 {ab,site,win_*} 和
#   498 列的抽取规格本来就是两种东西），约定一致才是对齐。
GATE_DEFAULTS = {
    "escalate_at": "low",        # 置信度 ≤ 这档就升级（= F6 的 margin_min 的角色）
    "uncertain_window": 14,      # 「疑似/考虑」这类词离证据多近才算数
    "uncertain_words": ["疑似", "考虑", "不除外", "待排", "可能", "倾向", "可疑",
                        "印象", "拟诊"],
}


def sheets_of(variables):
    """跳过 `_` 开头的元数据键（`_gate` / `_comment` …），只留真正的 sheet。

    所有遍历配置的地方都必须走这个 —— 否则 `_gate` 会被当成一个 sheet，
    它的 dict 值会被当成"列条目"去 `.get('col')`，轻则报错重则静默错位。
    """
    return {k: v for k, v in (variables or {}).items()
            if not str(k).startswith("_") and isinstance(v, list)}


def load_gate(variables):
    """取出配置里的门控参数，缺项用默认值补齐"""
    g = dict(GATE_DEFAULTS)
    raw = (variables or {}).get("_gate")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not str(k).startswith("_"):
                g[k] = v
    return g

# functional-spec §F3.3 的信号③：医学文书里这些词是刚需 ——
# 出现即强制低置信。注意「考虑」在病历里是**诊断推测**，不是确定结论。
UNCERTAIN_WORDS = ("疑似", "考虑", "不除外", "待排", "可能", "倾向", "可疑",
                   "疑为", "拟诊", "?", "？")

# 否定词分两类 —— 混在一起处理必然出错：
#   紧邻型：必须贴在词条前面（"无高血压"、"不吸烟"、"未患病"）
#   管辖型：管到句读为止，**能覆盖整个枚举**
#           否认"高血压史"、"心脏病"、"糖尿病史"…  ← 固定窗口管不到第三个
NEG_ADJACENT = ("无", "未见", "未闻及", "未诉", "未患", "不伴", "否认",
                "排除", "阴性", "没有", "未", "不", "非")
NEG_SCOPED = ("否认", "未见", "未闻及", "未诉", "未患", "不伴", "排除",
              "阴性", "没有", "无")
NEG_WINDOW = 4

# 真实病历会把病名用引号/书名号/括号包起来，还会用顿号串成一串：
#     否认“高血压史”、“心脏病”、“糖尿病史”
# 引号和顿号插在否定词和病名之间，直接 endswith 判定会**全部失效**。
NOISE_CHARS = "\"'“”‘’「」『』（）()【】[]《》〈〉· 　\t"      # 不含句读
# 句读是「管辖」的终止符：跨过它就说明进入了下一个分句
BOUNDARY = "。；;，,！？!?\n"

# 中文病历的否定经常写在词条**后面**（枚举式书写）：
#     饮酒史：否认   高血压：无   房颤：阴性   吸烟(-)
# 只看前面会把这些全判成阳性。
POST_NEG = ("：否认", ":否认", "：无", ":无", "：否", ":否",
            "(-)", "（-）", "阴性", "未患")
POST_SPAN = 8


def negated_after(text, idx, term_len, span=POST_SPAN):
    seg = text[idx + term_len: idx + term_len + span]
    return any(p in seg for p in POST_NEG)

# 既往史段一旦出现这些，整段按「无既往疾病」处理（比逐条否定更可靠）。
# 只在病史类段落里生效 —— 「平素体健」出现在出院记录里，不能拿来否定并发症。
HEALTHY_PATTERNS = ("既往体健", "平素体健", "既往身体健康", "平素身体健康",
                    "既往健康", "否认特殊", "无特殊病史")
HISTORY_SECTIONS = ("既往史", "个人史", "家族史")


# ------------------------------------------------------------------ 基础工具
def _strip_noise(s):
    return "".join(c for c in s if c not in NOISE_CHARS)


def negated(text, idx, window=NEG_WINDOW):
    """词条是否被否定。两步：

    ① 紧邻型：命中位置往前一小段，剔掉引号顿号后以否定词结尾
       —— 管 `无高血压` `否认"高血压史"`。
    ② 管辖型：往前找最近的管辖否定词，若它与词条之间**没有句读**，
       则否定作用到这里 —— 管 `否认A、B、C` 这种枚举。
       没有这一步，枚举里的第二项往后全部会判成阳性。
    """
    pre = text[max(0, idx - window - 6):idx]
    if any(_strip_noise(pre).endswith(n) for n in NEG_ADJACENT):
        return True

    head = text[:idx]
    for n in NEG_SCOPED:
        j = head.rfind(n)
        if j < 0:
            continue
        between = _strip_noise(head[j + len(n):])
        if not any(b in between for b in BOUNDARY):
            return True
    return False


def find_terms_ex(text, terms, form_any=None, tail_exclude=None):
    """→ ('pos'|'neg', term, idx) | None

    关键区分：**「词条没出现」和「词条出现但被否定」不是一回事。**
    前者是「没有证据」→ 按 missing_means 落态；
    后者是「明确的否定证据」→ 必须判阴性。
    同一段里既出现肯定又出现否定时取肯定。

    form_any: 剂型约束。中文药名的剂型**永远跟在药名后面**
    （阿司匹林`肠溶片`、硫酸氢氯吡格雷`片`、丁苯酞`氯化钠注射液`），
    所以只看命中处**往后 10 个字**。往两边开窗会把前一个药名的
    「…肠溶片、丁苯酞氯化钠注射液」误算成片剂。
    不带剂型的命中不算阳性证据，也不算否定证据 —— 直接跳过继续找。

    tail_exclude: 命中词**后面紧跟着**这些字就不算阳性。
    `糖尿病` 会在 `糖尿病饮食` 里命中，而「建议糖尿病饮食」不是糖尿病诊断。
    ★ 只作用于阳性分支 —— 否定分支照旧，否则 `否认"糖尿病史"` 的
      阴性证据会被一起丢掉，从"明确否认"退化成"没找到"。
    """
    neg = None
    for t in terms:
        i = text.find(t)
        while i >= 0:
            if negated(text, i) or negated_after(text, i, len(t)):
                if neg is None:
                    neg = (t, i)
            elif form_any and not any(
                    f in text[i:i + len(t) + 10] for f in form_any):
                pass                       # 剂型不对：既不算阳性也不算否定
            elif tail_exclude and any(
                    text.startswith(x, i + len(t)) for x in tail_exclude):
                pass                       # 是复合词的一部分，不是这个意思
            else:
                return "pos", t, i
            i = text.find(t, i + 1)
    return ("neg", neg[0], neg[1]) if neg else None


def find_terms(text, terms):
    """只要阳性命中（表搜索等场景用）"""
    r = find_terms_ex(text, terms)
    return (r[1], r[2]) if r and r[0] == "pos" else None


HEAD_RE = re.compile(r"^#{1,4}[ \t]*(.+)$", re.M)


def split_headings(text):
    """把一段文本按 markdown 标题切成 [(标题, 正文), ...]；无标题则返回 [("", text)]"""
    marks = [(m.start(), m.group(1).strip()) for m in HEAD_RE.finditer(text or "")]
    if not marks:
        return [("", text or "")]
    out = []
    if marks[0][0] > 0:
        out.append(("", text[:marks[0][0]]))
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((name, text[pos:end]))
    return out


def expand_sections(sections, alias):
    """两步把「逻辑段名」变成可直接 .get() 的文本。

    ① 把 `_其他` 按 markdown 标题拆成 `_其他:<标题>` 伪段落。
       实测 F2 只切出 10 个段，而 `_其他` 一个段吃掉 28 个 chunk 里的 14 个 ——
       病程记录 / 出院记录 / 病案首页 / 护理记录全在里面。不拆的话，
       按段取文本就等于在半个文档里搜，F4 踩过的跨段污染会原样回来。
    ② 再把 alias 里的逻辑名（"病程记录"）解析成候选伪段落的拼接，
       这样各 extractor 只管 `sections.get("病程记录")`，不必知道 F2 的切分粒度。
    """
    out = dict(sections)
    # ★ ①-expand：把段按 markdown 标题拆成 `<段名>:<标题>` 伪段落。
    #   两处**实测**教训（2026-09-28，真实样本 _test/real_table/compiled）：
    #
    #   (a) 原来只拆 `_其他` 一个段，而真实文书里 **13 个标题落在别的段内部** ——
    #       最要命的是 `入院记录` 落在 `个人史` 段里：于是 `_其他:入院记录`
    #       根本不存在 → 逻辑段 `入院记录` 解析为**空** → 所有把它列为来源的列
    #       （26 个病案首页列 + 住院天数等）全部白等，而输出上只看得到"未命中"。
    #       所以 `_expand` 现在把 `_actual_sections` 全列上（见 section_alias.json）。
    #
    #   (b) 原来是 `out.setdefault(key, body)` —— **重复标题会把后面的正文丢掉**。
    #       实测 `宁波市第二医院` 作标题出现 20 次、`…检验报告单` 10 次、
    #       `…检查报告单` 10 次：**10 张化验单里 9 张的正文被丢**，
    #       `化验单` 只解析出 3053 字（10 张化验单应远超这个量）。
    #       改成**按文档顺序拼接** —— 标题行本身也在 body 里，所以内容不丢。
    for sec_name, spec in ((alias or {}).get("_expand") or {}).items():
        raw = sections.get(sec_name)
        if not raw:
            continue
        for head, body in split_headings(raw):
            if not head:
                continue
            key = f"{sec_name}:{head}"
            if key in out and out[key] != body:
                out[key] = out[key] + "\n" + body
            else:
                out.setdefault(key, body)

    # ② 逻辑段名 → 候选伪段落。
    #    `_其他:<标题>` 这类键必须按**子串**匹配，不能精确匹配 ——
    #    真实标题是「辜程遥主任医师查房记录」「许梦蓓主治医师首次查房记录」，
    #    带医生姓名前缀，精确键永远配不上。
    subs = {}
    for k in out:
        if ":" in k and k.startswith(tuple((alias or {}).get("_expand") or {})):
            subs[k] = out[k]
    for logical, keys in (alias or {}).items():
        if logical.startswith("_"):
            continue
        parts = []
        for k in keys:
            if ":" in k:
                head = k.split(":", 1)[1]
                for sk, sv in subs.items():
                    if head in sk.split(":", 1)[1] and sv not in parts:
                        parts.append(sv)
            elif out.get(k) and out[k] not in parts:
                parts.append(out[k])
        if parts:
            out[logical] = "\n\n".join(parts)
    return out


NAME_KEYS = ("section", "name", "title", "章节", "段名", "id")
TEXT_KEYS = ("text", "content", "body", "parts", "正文", "文本", "chunks")
BLOCK_ID_KEYS = ("id", "bid", "block_id", "ref", "块id", "block", "kid")


def index_compiled(obj):
    """medrec F2 的 `compiled.json` 专用适配器。

    实测形状：
        {"source": "pages.json",
         "pages": [{"block_id": 4, "page": 1, "label": "text", "route": "fast",
                    "bbox": [...], "content": "医疗付费方式：城镇职工基本医疗保险"}]}

    **块 id 是 `p{page}-b{block_id}` 拼出来的**，而 `block_id` 是**整数** ——
    通用的 `index_blocks()` 只收字符串 id，在这里会一个块都收不到。
    """
    pages = obj.get("pages") if isinstance(obj, dict) else None
    if not isinstance(pages, list):
        return {}
    out = {}
    for blk in pages:
        if not isinstance(blk, dict):
            continue
        pg, bid = blk.get("page"), blk.get("block_id")
        txt = blk.get("content")
        if pg is None or bid is None or not isinstance(txt, str):
            continue
        out[f"p{pg}-b{bid}"] = txt
    return out


def build_block_index(obj):
    """先试专用适配器（块 id 是拼的），再退回通用递归（块 id 是现成的字符串）"""
    out = index_compiled(obj)
    if out:
        return out
    return index_blocks(obj)


def index_blocks(obj, out=None, depth=0):
    """把任意嵌套结构里「有 id 且有 text」的对象收成 `{块id: 正文}`。

    真实的 `sections_text.json` 是：
        {"source": "pages.json",
         "sections": [{"name": "_其他", "chars": 35045,
                       "n_blocks": 353, "refs": ["p1-b8", ...]}]}
    **段对象里没有 text，只有 refs** —— 正文得靠块 id 去别的表里还原。
    这里跟 `flatten_rows` 一样不去猜 schema：凡是同时具备 id 类字段和 text 类字段的
    对象都收进来，块 id 长什么样都行（`p1-b8` / `b12` / 随便）。
    """
    if out is None:
        out = {}
    if depth > 12:
        return out
    if isinstance(obj, dict):
        bid = next((obj[k] for k in BLOCK_ID_KEYS
                    if isinstance(obj.get(k), str)), None)
        txt = next((obj[k] for k in TEXT_KEYS
                    if isinstance(obj.get(k), str)), None)
        if bid and txt:
            out.setdefault(bid, txt)
        for v in obj.values():
            index_blocks(v, out, depth + 1)
    elif isinstance(obj, list):
        for x in obj:
            index_blocks(x, out, depth + 1)
    return out


def resolve_refs(refs, block_index, sep="\n"):
    """按 refs 顺序拼出段正文；按顺序拼很重要（阅读顺序）"""
    return sep.join(block_index[r] for r in (refs or []) if r in block_index)


def _as_text(v):
    """把任意值拼成文本（list 拼、dict 找 text 类键、标量转字符串）"""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n\n".join(_as_text(x) for x in v)
    if isinstance(v, dict):
        for k in TEXT_KEYS:
            if k in v:
                return _as_text(v[k])
        return " ".join(str(x) for x in v.values()
                        if isinstance(x, (str, int, float)))
    return str(v)


def load_sections(path, blocks_json=None):
    """把各种可能的 sections_text.json 形状读成 {段名: 文本}。

    已知真实形状（batch_0924_1527 / 202424089790叶文达）：
        {"source": "pages.json",
         "sections": [{"name": "_其他", "chars": 35045,
                       "n_blocks": 353, "refs": [...]}, ...]}
    **段对象只有 refs、没有 text**，所以必须能借 `--blocks` 指向的块表还原正文。
    只认 dict 的旧加载器会把这段读成 {source, sections} 两个键，**段名全丢**。
    """
    d = json.loads(Path(path).read_text(encoding="utf-8"))

    # ── 形状 A：{source, sections:[{name, refs, ...}]} —— 实测就是这个
    if isinstance(d, dict) and isinstance(d.get("sections"), list):
        bi = {}
        if blocks_json:
            bi = build_block_index(json.loads(
                Path(blocks_json).read_text(encoding="utf-8")))
            print(f"[info] 块表读到 {len(bi)} 个块", file=sys.stderr)
        out, no_text = {}, []
        for i, s in enumerate(d["sections"]):
            if isinstance(s, str):
                out[f"sec{i}"] = s
                continue
            nm = next((s[k] for k in NAME_KEYS if isinstance(s.get(k), str)),
                      f"sec{i}")
            txt = next((s[k] for k in TEXT_KEYS if isinstance(s.get(k), str)), None)
            if txt is None and s.get("refs"):
                txt = resolve_refs(s["refs"], bi)
                if not txt:
                    no_text.append((nm, len(s["refs"])))
            out[nm] = txt or ""
        if no_text:
            print(f"[warn] {len(no_text)} 段的 refs 一个都没解析出来：{no_text[:4]}"
                  f"\n       → 需要 --blocks 指向含块正文的 json"
                  f"（compiled.json / pages.json）", file=sys.stderr)
        return out

    if isinstance(d, dict):
        return {k: _as_text(v) for k, v in d.items()}

    if isinstance(d, list):
        out, unnamed = {}, 0
        for i, item in enumerate(d):
            if isinstance(item, str):
                out[f"sec{i}"] = item
                unnamed += 1
                continue
            if isinstance(item, dict):
                nm = next((item[k] for k in NAME_KEYS
                           if isinstance(item.get(k), str)), None)
                tx = None
                for k in TEXT_KEYS:
                    if k in item:
                        tx = _as_text(item[k])
                        break
                if tx is None:
                    tx = _as_text(item)
                key = nm or f"sec{i}"
                if key in out:
                    key = f"{key}#{i}"
                if not nm:
                    unnamed += 1
                out[key] = tx
        if unnamed:
            print(f"[warn] {unnamed} 段没取到名字，退化成 secN —— "
                  f"source_sections 的按段过滤对它们无效", file=sys.stderr)
        return out

    raise SystemExit(f"sections 读不出：{type(d).__name__}")


def dump_sections(sections):
    """第一次接真实文件时先跑这个：确认段名到底是什么"""
    print(f"共 {len(sections)} 段：")
    for k, v in sections.items():
        head = v[:70].replace("\n", " ")
        print(f"  {k:34} {len(v):>7,} 字   {head!r}")


def load_alias(path=None):
    p = Path(path) if path else HERE / "section_alias.json"
    if not p.exists():
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    out = {k: v for k, v in d.items() if not k.startswith("_")}
    out["_expand"] = d.get("_expand", {})
    return out


def flatten_rows(obj, acc=None, depth=0):
    """把任意嵌套的 JSON 摊平成 [[cell, ...], ...]；只收字符串/数字叶子"""
    if acc is None:
        acc = []
    if depth > 8:
        return acc
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            for d in obj:
                row = [v for v in d.values()
                       if isinstance(v, (str, int, float))]
                if row:
                    acc.append([str(c) for c in row])
        elif obj and all(isinstance(x, list) for x in obj):
            for sub in obj:
                acc.append([str(c) for c in sub if isinstance(c, (str, int, float))])
        else:
            for x in obj:
                flatten_rows(x, acc, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            flatten_rows(v, acc, depth + 1)
    return acc


# ------------------------------------------------------------------ 各 kind 的实现
def index_tables(raw):
    """把任意形状的表格输入 → {逻辑表名: [[cell,...], ...]}

    ★ 真实 `tables.json` 是 `{"tables":[{type,header,rows,…}, …]}` 31 个表对象，
      不是 `{"医嘱表":[[…]]}`。旧代码直接对顶层调 `flatten_rows`，
      拿到的是 **31 行表格元数据**（`['8','1','1','未知',…]`），
      于是 175 个查表列**全部落阴性** —— 而且看不出错：
      阴性长得就像「这个病人没用过这个药」。

    同名多表必须**合并**：真实文书里 10 张化验单、5 张用药单，
    只取一张会漏掉大部分数据。

    形状 B（旧 fixture / 测试用的 `{逻辑名: rows}`）原样返回，老测试不受影响。
    """
    if not isinstance(raw, dict) or "tables" not in raw:
        return raw
    objs = raw.get("tables")
    if not isinstance(objs, list):
        return raw

    buckets = {}
    for t in objs:
        if not isinstance(t, dict):
            continue
        rows = t.get("rows")
        if not isinstance(rows, list) or not rows:
            continue
        rows = [[str(c) for c in r] if isinstance(r, list) else [str(r)]
                for r in rows]
        typ = str(t.get("type") or "未知")
        if typ == "用药单":
            # 用药单要再分一层：住院医嘱 vs 出院带药。
            # 分不开时**宁可只算医嘱** —— 出院带药判错等于说"这药出院后还在吃"，
            # 是会拿给临床看的错；漏掉只是待确认，人还能补。
            blob = " ".join(" ".join(r) for r in rows[:3])
            typ = "出院带药" if any(h in blob for h in DISCHARGE_HINT) else "医嘱表"
        buckets.setdefault(typ, []).extend(rows)

    out = {k: v for k, v in buckets.items() if k in TBL_ALIAS}
    for logical, types in TBL_ALIAS.items():
        if logical in out:
            continue
        rows = []
        for t in types:
            rows.extend(buckets.get(t, []))
        if rows:
            out[logical] = rows
    return out


# 逻辑表名 → 真实 `tables.json` 里的 type。
# 真实文书的类型识别器（`f2/tables.py`）只产出 化验单 / 用药单 / 未知，
# 而配置里写的是业务名 —— 这张对照表就是两边的桥。
# 「未知」里混着病案首页、查体、手术、门诊排班，靠 filter 内容自己去挑。
TBL_ALIAS = {
    "医嘱表": ("用药单",),
    "出院带药": ("用药单",),
    "化验单": ("化验单",),
    # 真实文书里这个类型叫 `体征表`（chunktest / 服务器产物实测），
    # 早先只映射 `未知`，于是一个都没接上。
    "生命体征表": ("体征表", "未知"),
    "病案首页": ("未知",),
    "检查报告": ("化验单", "用药单", "未知", "体征表", "费用表"),
    "检查报告": ("化验单", "用药单", "未知", "体征表", "费用表"),
}

DISCHARGE_HINT = ("出院带药", "出院医嘱", "出院用药")


def do_gazetteer_boolean(spec, cfg_entry, sections, tables):
    terms = spec.get("terms") or []
    srcs = cfg_entry.get("source_sections") or list(sections)
    table = spec.get("table")
    form_any = spec.get("form_any") or []
    tail_exclude = spec.get("tail_exclude") or []

    # 1) 先在指定章节的正文里找阳性证据。
    #    体健式兜底必须等**所有段落都扫完**才能用 —— 否则既往史写「体健」、
    #    个人史写「饮酒10年」时会被误判阴性。
    healthy_in = None
    neg_in = None
    pos_text = None
    for name in srcs:
        text = sec_text(sections, name)
        if not text:
            continue
        hit = find_terms_ex(text, terms, form_any=form_any,
                            tail_exclude=tail_exclude)
        if hit:
            tag, t, i = hit
            ev = {"term": t, "section": name,
                  "span": text[max(0, i - 20):i + len(t) + 20]}
            if tag == "pos":
                # ★ 带剂型的列（「住院丁苯酞（片剂\胶囊）」）**不能**凭正文先判阳性：
                #   正文提「丁苯酞」时说的可能是注射液，而医嘱表里有确定的剂型。
                #   所以先记下，等表查完再定。
                if not form_any:
                    return STATE_POS, ev
                if pos_text is None:
                    pos_text = ev
            elif neg_in is None:
                neg_in = ev          # 明确的否定证据，先记着
        if healthy_in is None and name in HISTORY_SECTIONS \
                and any(h in text for h in HEALTHY_PATTERNS):
            healthy_in = name

    # 2) 再在表里找（剂型列优先信表 —— 表里的剂型是确定的）
    if table and tables:
        rows = tables.get(table) or flatten_rows(tables.get(table))
        for row in rows if isinstance(rows, list) else []:
            cells = row if isinstance(row, list) else [str(row)]
            for c in cells:
                # ★ 剂型约束在**同一格内**判定：「住院丁苯酞（片剂\胶囊）」
                #   不能在「丁苯酞氯化钠注射液」这行上判阳性。
                if form_any and not any(f in str(c) for f in form_any):
                    continue
                hit = find_terms(str(c), terms)
                if hit:
                    return STATE_POS, {"term": hit[0], "table": table, "cell": str(c)[:60]}

    # 2.5) 表里没有，才认正文里那个带剂型的阳性命中
    if pos_text:
        return STATE_POS, pos_text

    # 3) 明确的否定证据 > 体健式兜底 > 缺失语义
    if neg_in:
        return STATE_NEG, {**neg_in, "why": "词条出现但被否定（前置或后置）"}
    if healthy_in:
        return STATE_NEG, {"why": f"未命中任何阳性证据，且 {healthy_in} 为体健式表述"}
    st, why = missing_state(cfg_entry, srcs, sections)
    return st, {"why": why}


def do_gazetteer_enum(spec, cfg_entry, sections, tables):
    terms = spec.get("terms") or []
    for name in (cfg_entry.get("source_sections") or list(sections)):
        text = sec_text(sections, name)
        if not text:
            continue
        for t in terms:
            i = text.find(t)
            if i >= 0:
                return STATE_POS, {"value": t, "section": name,
                                   "span": text[max(0, i - 15):i + len(t) + 15]}
    return missing_state(cfg_entry, cfg_entry.get("source_sections"), sections)


def do_regex(spec, cfg_entry, sections, tables):
    pat, grp = spec.get("pattern"), spec.get("group", 0)
    if not pat:
        return STATE_PENDING, {"why": "regex 规格缺 pattern"}
    rx = re.compile(pat)
    # ★ `reject`：「人名」这类列靠正则**猜边界**，猜错会抓到下一个字段名。
    #   实测 `主治医师首次查房记录` 被抽成人名「首次查房」。
    #   这是**看起来完全合法**的值 —— 4 个汉字，不报错，直接进库。
    reject = spec.get("reject") or []
    for name in (cfg_entry.get("source_sections") or list(sections)):
        text = sec_text(sections, name)
        if not text:
            continue
        for m in rx.finditer(text):
            try:
                val = m.group(grp) if grp else m.group(0)
            except IndexError:
                val = m.group(0)
            if not val or any(r in val for r in reject):
                continue
            # mul_if_lt: [阈值, 倍数] —— 单位换算。
            # 真实文书查体写「身高1.65m」，而登记表列是「身高（cm）」。
            # 只在数值小于阈值时换算，`身高65cm`（婴儿）不会被乘成 6500。
            mil = spec.get("mul_if_lt")
            if mil and re.fullmatch(r"-?\d+(?:\.\d+)?", str(val).strip()):
                thr, fac = mil
                if float(val) < float(thr):
                    v = float(val) * float(fac)
                    val = str(int(v)) if v == int(v) else str(round(v, 2))
            return STATE_POS, {"value": val, "section": name}
    return missing_state(cfg_entry, cfg_entry.get("source_sections"), sections)


def _cells_after(row, want):
    """filter 命中的那个单元格**之后**的非空内容。
    医嘱表 ['阿司匹林肠溶片','100mg','qd'] + want=['阿司匹林'] → ['100mg','qd']"""
    for i, c in enumerate(row):
        if any(str(w) in str(c) for w in want):
            return [str(x) for x in row[i + 1:] if str(x).strip()]
    return []


# ★ 取值类列必须按**字段类型**挑单元格，不能一律取 rest[0]。
#   医嘱表行是 [药名, 剂量, 频次, 途径, 类型] —— 一律取 rest[0] 的话，
#   `住院阿司匹林用药时间` / `使用频次` / `使用剂量` 三列会**返回同一个剂量值**。
#   实测：三列全都返回 '100mg'。
FIELD_PATTERNS = (
    ("频次", re.compile(r"^(?:qd|bid|tid|qid|qn|qw|qod|prn|st|q\d+h|"
                       r"每日|每天|一日|每\d+小时|\d+\s*次(?:\s*/?\s*[日天])?)",
                       re.I)),
    ("剂量", re.compile(r"^\d+(?:\.\d+)?\s*(?:mg|g|μg|ug|ml|mL|unit|U|IU|万U|"
                       r"毫克|克|毫升|微克|国际单位|万单位|单位|"
                       r"片|粒|支|袋|滴|喷)", re.I)),
    ("时间", re.compile(r"^(?:\d{4}\s*[-/年]|\d{1,2}\s*[:：]\s*\d{2}|第\d+天)")),
    ("途径", re.compile(r"^(?:口服|静滴|静推|静脉|皮下|肌注|外用|鼻饲|"
                       r"po|iv|sc|im)$", re.I)),
)


def pick_cell(rest, field):
    """→ (值, 说明)。按字段名在候选格里挑最像的那个；挑不到就返回 None 并说明原因。

    **宁可返回空，也不要返回另一个字段的值** —— 把剂量当频次写进登记表，
    比留空危险得多，而且没有任何检查能发现。
    """
    if not rest:
        return None, "匹配格后面没有内容"
    fld = str(field or "")
    for key, pat in FIELD_PATTERNS:
        if key in fld:
            for c in rest:
                if pat.match(c.strip()):
                    return c, None
            return None, f"行里没有像「{key}」的单元格（候选：{rest[:4]}）"
    return rest[0], None


def norm_cell(s):
    """洗掉化验单项目名/单位里的 markdown 与 LaTeX 噪声。

    真实文书里项目名长这样：`\\(^{{*}}尿酸\\)`、`*白细胞计数`、
    单位长这样：`\\(\\mu\\)mol/L`、`*10^9/L`。
    不洗干净，`白细胞计数（10E9/L）` 就永远配不上 `*白细胞计数`。
    """
    s = str(s or "")
    s = re.sub(r"\\\((.*?)\\\)", r"\1", s)       # \( … \) 外壳
    s = s.replace("\\mu", "μ")
    s = re.sub(r"\\(?:uparrow|downarrow|up|down)", "", s)
    s = re.sub(r"[\^_{}*\\\s，,、；;：:]+", "", s)   # 上下标/强调符/全角逗号
    s = re.sub(r"10[eE](\d)", r"10\1", s)        # 10E9/L 与 10^9/L 拉齐
    return s.strip().lower()


def col_unit(col):
    """从列名尾括号里抠出单位：`白细胞计数（10E9/L）` → `109/l`；没单位返回 ''"""
    m = re.search(r"[（(]([^）)]*)[）)]\s*$", str(col or ""))
    if not m:
        return ""
    inner = m.group(1)
    return norm_cell(inner) if re.search(r"\d|/", inner) else ""


def _cells_after_n(row, norm_wants):
    """`_cells_after` 的归一化版：按 norm_cell 后的内容定位，返回**原始**格"""
    for i, c in enumerate(row):
        n = norm_cell(c)
        if any(w and w in n for w in norm_wants):
            return [str(x) for x in row[i + 1:] if str(x).strip()]
    return []


def sec_text(sections, name):
    """取段正文，并**掐掉段首的「【块前缀】段名：」**。

    ★ 不掐掉的话，拿段名当词条搜会**永远命中段落自己的标题**。
      实测：`卒中家族史` 的词表里有 `家族史`，而 `家族史` 段的正文是
      「家族史：父亲已故，死因不详…」—— 于是这一列**恒判阳性**，
      而证据是段落标题本身，不是任何临床事实。这类错在多份文书上会稳定复现，
      因为每份文书的段标题都一样。

    只认**带冒号**的才算标题：`既往体健。否认…` 里的 `既往史` 是正文词，
    掐掉它会把 fixture 里的真实语义一起删掉。
    """
    text = sections.get(name)
    if not text:
        return text
    nm = str(name).split(":")[-1].split("：")[-1].strip()
    if not nm:
        return text
    pat = re.compile(r"^\s*(?:【[^】]*】\s*)*(?:#{1,6}\s*)*" +
                     re.escape(nm) + r"\s*[：:]\s*")
    return pat.sub("", text, count=1)


def _cell_matching(row, norm_wants, max_len=40, fallback_terms=None):
    """返回**命中格的原文** —— 「出院给予的调脂药物名称」这类列要的是药名本身，
    不是它后面的剂量格。

    ★ 必须挡掉超长格。旧版 F2 的表会**塌成 1 行 1 列**，整张表就是一格；
    这时"命中格"是整张表的正文，返回出去就是
    `开始医嘱内容医师 签名执行 时间执行人 签名备注日期…` 这种垃圾 ——
    而且它看着像个"填上了的值"，不报错。宁可返回 None 落待确认。

    ★ 但真实数据里还有一种**中等长度的塌格**，实测原文：
        `! [基]拜糖平片(阿卡波糖片/50mg*30)*60片 50mg 口服 次数中 一次`
    药名+规格+剂量+途径+频次全在这一格里，长度 40+ → 被 max_len 挡掉 → 返回 None。
    而 `want=match` 的那 7 列（全是「出院给予的 X 药物名称」，field=药名）
    要的**就是药名本身**，命中的那个词**就是**药名 ——
    所以这时返回命中的词，既不是垃圾也不是猜测。
    """
    for c in row:
        n = norm_cell(c)
        if not any(w and w in n for w in norm_wants):
            continue
        s = str(c)
        if len(s) > max_len:
            if fallback_terms:
                for w in fallback_terms:
                    if w and norm_cell(w) in n:
                        return str(w)
            continue
        return s
    return None


def _tail_after_terms(row, terms, cap=16):
    """塌表专用：整张表塌成一格，取「命中词**之后**」的那一小段原文。

    真实塌表长这样（旧版 F2 的产物）：
        `序号药物名称每次用量用法…1硫酸氢氯吡格雷片75毫克一日1次口服☐☑☐2辛伐他汀…`
    药名和剂量在**同一格**里，所以 `_cells_after()` 永远返回空 ——
    实测 36 列（剂量/频次/开始时间各一批）就死在这里，报「匹配格后面没有内容」。

    只取命中词后 16 个字：再往后就是下一个药了，会串值。
    """
    outs = []
    for c in row:
        s = str(c)
        for t in terms:
            i = s.find(str(t))
            if i >= 0:
                outs.append(s[i + len(str(t)): i + len(str(t)) + cap])
                break
    return outs


def field_pattern(field):
    """按字段名取「值长什么样」的正则；没有对应形态返回 None"""
    fld = str(field or "")
    for key, pat in FIELD_PATTERNS:
        if key in fld:
            return pat
    return None


def field_pattern_loose(field):
    """`field_pattern` 的**去锚**版 —— 只给「塌格」兜底用。

    `FIELD_PATTERNS` 里的正则带 `^`/`$`，`pick_cell` 靠它们做「宁可留空、
    也不返回另一个字段的值」的严格判定，那个语义**不能动**。
    但在真实数据里，表常塌成「药名+剂量+途径+频次都在同一个格」：

        `※[基]阿司匹林肠溶片 (阿司匹林肠溶 片/100mg*30) 100mg 口服 次`

    这时值必然出现在字符串**中间**，带锚就永远匹配不到 ——
    实测 `住院阿托伐他汀使用剂量(mg)` 的候选尾巴是
    `钙片 (阿托伐他汀钙 20mg 口服 次`，明明有 `20mg` 却报「行里没有像剂量的单元格」。
    所以兜底路径需要一份去锚的正则。
    """
    p = field_pattern(field)
    if p is None:
        return None
    pat = p.pattern
    if pat.startswith("^"):
        pat = pat[1:]
    if pat.endswith("$"):
        pat = pat[:-1]
    return re.compile(pat, p.flags)


def do_table_lookup(spec, cfg_entry, sections, tables):
    """★ 光判「有没有」不够 —— 登记的交付物是**值**。

    这里原来只返回 `{row, field, table}`，**没有 value** ——
    于是所有查表列都是「阳性 + 空值」，而 179 列都走这条路径。
    准确率测试在受控文书上把它钉出来了。
    """
    if not tables:
        return missing_state(cfg_entry, cfg_entry.get("source_sections"), sections)
    tbl = spec.get("table")
    flt = spec.get("filter") or {}
    any_terms = spec.get("filter_any") or []
    field = spec.get("field")

    rows = flatten_rows(tables.get(tbl)) if tbl in (tables or {}) else flatten_rows(tables)
    if not rows:
        return STATE_PENDING, {"why": f"表 {tbl} 里没摊出任何行"}

    want = list(flt.values()) + list(any_terms)
    if not want:
        return STATE_PENDING, {"why": "table_lookup 没给 filter"}

    fld = str(field or "")
    norm_want = [norm_cell(w) for w in flt.values()]
    norm_any = [norm_cell(w) for w in any_terms]
    unit = col_unit(cfg_entry.get("col"))

    # ★ **不能取第一个子串命中的行。** 真实化验单里尿液与血液两套项目并排：
    #   `白细胞计数` 既是血常规（4.3，10^9/L）也是尿沉渣（2.0，/ul）；
    #   `尿酸` 既是血尿酸（157.8 μmol/L）也撞上 `尿酸碱度`（6.0）。
    #   原来看见就取，于是 葡萄糖（mmol/L） 拿到了**尿葡萄糖**的值。
    #   改成打分：项目名**完全相等**优先，单位对得上再加分，取最高分那行。
    best = None
    for row in rows:
        norms = [norm_cell(c) for c in row]
        joined_n = " ".join(norms)
        if not all(w in joined_n for w in norm_want):          # filter 是 AND
            continue
        if norm_any and not any(w in joined_n for w in norm_any):
            continue
        exact = all(any(w == n for n in norms) for w in norm_want)
        # 单位候选格必须**像个单位**：长度≥3 且含字母或斜杠。
        # 不加这条，`红细胞计数.（10E12/L，1-100）` 的单位会退化成含 "12" 的长串，
        # 于是尿沉渣那行的 **NO. 序号 `12`** 也被当成"单位对得上"。
        unit_ok = (not unit) or any(
            len(n) >= 3 and re.search(r"[a-z/]", n) and (unit in n or n in unit)
            for n in norms)
        score = (2 if exact else 0) + (1 if unit_ok else 0)
        if best is None or score > best[0]:
            best = (score, row, unit_ok)
            if score == 3:                # 满分，不用再找
                break

    if best is not None:
        row, unit_ok = best[1], best[2]
        # ★ 列名写了单位、却没有一行对得上 → **宁可待确认，也不给别的单位的值**。
        #   `尿酸(mg/dL)` 曾经拿到 157.8（那是 μmol/L 的值），
        #   两个单位差 59 倍，填进登记表就是硬错。
        if unit and not unit_ok:
            return STATE_PENDING, {
                "why": f"没有单位对得上「{unit}」的行（列名指定了单位）",
                "row": row[:6], "field": field, "table": tbl,
            }
        # 定位格用**主 filter**（药名）找，别用 filter_any（剂型）——
        # 「片」这种单字剂型词可能先命中别的格，导致取值格整体偏移。
        rest = _cells_after_n(row, norm_want)
        if not rest:
            rest = _cells_after_n(row, norm_want + norm_any)
        # ★ 「整张表塌成一格」的判定（旧版 F2 会把表塌成 1 行 1 列）。
        #   实测 `_test/chunktest/tables.json` 的 31 张表**全是 1×1**，
        #   而 `_test/real_table/compiled` 是正常形状（5×4 / 26×5 …）。
        #   这种一坨字里的小字**不许猜值**：多张医嘱挤在一格时，
        #   「某个单位形态」很可能属于**另一个药**。`test_real_tables.py`
        #   钉着这条不变量。用它同时管住下面两条兜底路径。
        blob = len([c for c in row if str(c).strip()]) < 2
        mode = spec.get("want")
        if mode is None:                    # 没写就退回关键词猜（老配置兼容）
            mode = ("value" if any(k in fld for k in
                                   ("剂量", "频次", "时间", "结果", "值", "评分"))
                    else "flag")
        pick_why = None
        if mode == "value":
            val, pick_why = pick_cell(rest, field)
            # ★ 塌表兜底：整张表在一格里时「匹配格后面」当然是空的。
            #   真值是紧跟在药名后面的那几个字（`…氯吡格雷片75毫克一日1次…`），
            #   所以拿字段形态的正则去**那一小段里搜**。
            #   只搜命中词后 16 字，再往后就是下一个药，会串值。
            if val is None:
                fp = field_pattern(field)
                if fp:
                    for tail in _tail_after_terms(row, list(flt.values()) or want):
                        m = fp.search(tail)
                        if m:
                            val = m.group(0).strip()
                            pick_why = None
                            break
            # ★ 更常见的一种塌法：**药名、剂量、途径、频次全塌在同一个格**里。
            #   实测（_test/real_table/compiled，真实文书）：
            #     `※[基]阿司匹林肠溶片 (阿司匹林肠溶 片/100mg*30) 100mg 口服 次`
            #     `※[基]阿托伐他汀钙片 (阿托伐他汀钙 20mg 口服 次`
            #   上面那条兜底只取命中词后 16 字、且正则带 `^` —— 于是
            #   `钙片 (阿托伐他汀钙 20mg 口服 次` 这种尾巴永远配不上，
            #   明明有 `20mg`，却报「行里没有像剂量的单元格」。
            #   这里在**含命中词的整个格**里找字段形态，取**最后一个**命中：
            #   真实格里通常先出现包装规格（`片/100mg*30`）、后出现实际用量
            #   （`100mg`），后者才是要交出去的值。
            if val is None and not blob:
                loose = field_pattern_loose(field)
                if loose:
                    # 两级池子：先只看**含命中词**的格（最贴近命中行），
                    # 再退到**整行**。
                    # ★ 必须有第二级：实测真实行
                    #   `[..., 丁苯酞氯化钠注射液(...) 100ml, 静脉滴 注 2 次, ...]`
                    #   里「途径+频次」在**另一个格**（格 3），而药名在格 2 ——
                    #   只搜含命中词的格就够不到 `2 次`。
                    #   一行就是一条医嘱，所以同一行里的剂量/频次/途径都算它的。
                    pool_hi = [c for c in row
                               if any(w and w in norm_cell(c)
                                      for w in (norm_want + norm_any))]
                    for pool in (pool_hi, list(row)):
                        got = None
                        for c in pool:
                            hits = [h for h in loose.findall(str(c))
                                    if str(h).strip()]
                            if hits:
                                got = str(hits[-1]).strip()   # 取**最后**一个
                        if got:
                            val, pick_why = got, None
                            break
        elif mode == "match":
            # 「…药物名称」这类列：交付物是**药名本身**，不是它后面的剂量格。
            # ★ `fallback_terms` 必须把 **filter_any 也算进去**：这 7 列的
            #   `want=match` 列配的是 `filter_any: [二甲双胍, 格列美脲, 阿卡波糖, …]`
            #   这样一个**药名列表**，`filter` 是空的 —— 只取 `flt.values()` 会得到
            #   空列表，于是塌格兜底拿不到药名（实测 `出院给予的降糖药物名称`
            #   仍报「没定位到命中格」）。
            val = _cell_matching(
                row, norm_want + norm_any,
                # ★ 整表塌成一格（`blob`）时**不给** fallback_terms：
                #   那一坨字是整张表，从里面挑一个药名等于猜。
                #   （实测 `_test/chunktest` 的 31 张表全是 1×1。）
                fallback_terms=(None if blob
                                else list(flt.values()) + list(any_terms)))
            if val is None:
                pick_why = "没定位到命中格"
        else:
            val = "是"
        # ★ 命中行但**取不到值** → 待确认，不是阳性。
        #   登记表的交付物是"值"；阳性+空格子会一路静默进库，
        #   而且会**掩盖上游 F2 的缺陷**（表里根本没有时间列）。
        #   宁可升级，也不要交一个半截答案。
        if mode in ("value", "match") and val is None:
            return STATE_PENDING, {
                "why": f"命中行但取不到「{field}」：{pick_why}",
                "row": row[:6], "field": field, "table": tbl,
            }
        ev = {"value": val, "row": row[:6], "field": field, "table": tbl}
        if pick_why:
            ev["why"] = pick_why
        return STATE_POS, ev

    return missing_state(cfg_entry, cfg_entry.get("source_sections"), sections)


DISPATCH = {
    "gazetteer_boolean": do_gazetteer_boolean,
    "gazetteer_enum": do_gazetteer_enum,
    "regex": do_regex,
    "table_lookup": do_table_lookup,
}


# ------------------------------------------------------------------ 主流程
def _has_drug_list(sections):
    """文书里是否存在出院带药清单条目（★(商品名)药名…）。"""
    RX = re.compile(r"\u2605\s*[\uff08(][^\uff09)]{1,12}[\uff09)]\s*[\u4e00-\u9fa5A-Za-z0-9]{2,20}")
    for _v in (sections or {}).values():
        _s = str(_v or "")
        if len(_s) < 40:
            continue
        if RX.search(_s):
            return True
    return False


def missing_state(cfg_entry, srcs, sections):
    """找不到证据时该落哪一态 —— 升级门控的第一道闸。

    `missing_means` 有四种取值，其中 **`否若段在`** 是这轮新加的、最关键的一种：
        来源段**存在**却没找到 → 否（该段是完备的：化验单上没有就是没做）
        来源段**整段不存在**   → 待确认（很可能是文书缺失，不是「没有」）
    不分这两种的话，`化验` 49 列 + `检查` 55 列在每份文书里都会落进待确认 →
    升级率直接顶到 64%，规则层等于白做。
    """
    mm = cfg_entry.get("missing_means", "待确认")
    if mm == "否":
        return STATE_NEG, {"why": "配置断言「缺失即否」"}
    if mm == "不适用":
        # 条件列（仅TIA / 醒来卒中）与备注列：本来就不是每份文书都要填。
        # 落空**不等于**"没查证"，不该占待确认名额 ——
        # 实测这 12 列在 **22/22 例**上都落待确认，等于每份固定丢 12 格。
        # 三态不外扩，用阴性承载"空"：值留空、理由写明白、可审计。
        return STATE_NEG, {"why": "不适用（条件列/备注列非必填），无内容即留空"}
    if mm == "否若段在":
        present = [s for s in (srcs or []) if sections.get(s)]
        if present:
            return STATE_NEG, {"why": f"来源段存在（{present[0]}）却没命中 → 否"}
    if mm == "否若带药清单在":
        # 文书里能找到「出院带药清单」条目（★(商品名)药名 规格 …）→ 该清单完备，
        # 清单里没这个药 = 没用这个药（阴性）；找不到清单才留待确认。
        try:
            _hit = _has_drug_list(sections)
        except NameError:
            _hit = False
        if _hit:
            return STATE_NEG, {"why": "出院带药清单存在却没这个药 → 否"}
        return STATE_PENDING, {"why": "文书里找不到出院带药清单，无法断定"}
        # ★ 必须写清**缺的是哪几个段**。
        #   原来只写「来源段整段缺失」，于是从落盘的依据里根本看不出
        #   到底是 `病案首页` 没被切出来、还是文书就没这一页 —— 而这两件事
        #   的修法完全不同（补别名 vs 承认没有）。
        #   实测背景：106 例 / 498 列的宽表里，**4980 格的依据都是这一条**，
        #   占全部待确认的 45%，却因为不写段名而无法定位。
        miss = [s for s in (srcs or []) if s] or ["(该列未声明来源段)"]
        return STATE_PENDING, {
            "why": f"来源段整段缺失（缺 {'/'.join(miss)}），无法断定是「没有」还是「文书缺」"}
    return STATE_PENDING, {"why": f"缺失语义={mm}"}


# ------------------------------------------------------------------ pass 2：派生列
def _val_of(r):
    """从 pass 1 的结果里取出可用值。**只有阳性才算有值** ——
    把配置兜底的「阴性」当成值，会把「没找到」当成「没有」。"""
    if not r or r.get("state") != STATE_POS:
        return None
    ev = r.get("evidence") or {}
    return ev.get("value") or ev.get("term") or ev.get("cell")


def _date(s):
    m = re.search(r"(\d{4})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})", str(s or ""))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _time_min(s):
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", str(s or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _num(s):
    m = re.search(r"-?\d+(?:\.\d+)?", str(s or ""))
    return float(m.group(0)) if m else None


def apply_derived(spec, out):
    """compute / normalize 的实现。

    这些列以前一律落 `待确认` 并写明「表达式未翻译成算子」—— 本次把它们翻译成
    可执行 op。**每一列的 expr 都必须在配置里写成结构化 op，不接受中文散文。**
    """
    kind = spec.get("kind")
    op = spec.get("op")

    if kind == "normalize":
        src = spec.get("from")
        v = _val_of(out.get(src))
        if v is None:
            return STATE_PENDING, {"why": f"依赖列「{src}」无值"}
        return STATE_POS, {"value": str(v).strip(), "from": src}

    if op == "eq_literal":
        v = _val_of(out.get(spec.get("col")))
        if v is None:
            return STATE_PENDING, {"why": f"依赖列「{spec.get('col')}」无值"}
        hit = spec.get("value") in str(v)
        return (STATE_POS if hit else STATE_NEG), {
            "value": "是" if hit else "否", "from": spec.get("col"), "src": str(v)[:40]}

    if op == "date_diff_days":
        a = _date(_val_of(out.get(spec.get("a"))))
        b = _date(_val_of(out.get(spec.get("b"))))
        if not a or not b:
            return STATE_PENDING, {"why": f"日期取不到（{spec.get('a')} / {spec.get('b')}）"}
        import datetime as _dt
        return STATE_POS, {"value": str((_dt.date(*a) - _dt.date(*b)).days),
                           "from": [spec.get("a"), spec.get("b")]}

    if op == "time_diff_min":
        for bk in ("b", "fallback_b"):
            a = _time_min(_val_of(out.get(spec.get("a"))))
            b = _time_min(_val_of(out.get(spec.get(bk))))
            if a is not None and b is not None:
                return STATE_POS, {"value": str(a - b),
                                   "from": [spec.get("a"), spec.get(bk)]}
        return STATE_PENDING, {"why": f"时间取不到（{spec.get('a')}）"}

    if op == "sum":
        vals = [_num(_val_of(out.get(c))) for c in (spec.get("cols") or [])]
        vals = [v for v in vals if v is not None]
        if not vals:
            return STATE_PENDING, {"why": f"参与求和的列都没有值：{spec.get('cols')}"}
        tot = sum(vals)
        return STATE_POS, {"value": str(int(tot) if tot == int(tot) else round(tot, 2)),
                           "from": spec.get("cols")}

    if op == "classify":
        v = _val_of(out.get(spec.get("col")))
        if v is None:
            return STATE_PENDING, {"why": f"依赖列「{spec.get('col')}」无值"}
        for cls, drugs in (spec.get("classes") or {}).items():
            if any(d in str(v) for d in drugs):
                return STATE_POS, {"value": cls, "src": str(v)[:40]}
        return STATE_PENDING, {"why": f"「{v}」不在分类表里"}

    return STATE_PENDING, {"why": f"未实现的 op={op}（expr={spec.get('expr')}）"}


def ensure_pos_value(state, ev):
    """★ 契约：凡是 STATE_POS，就必须带一个**非空**的 value。

    为什么必须：登记的交付物是**值**。阳性 + 空格子会一路静默进宽表，
    看起来和「没抽到」一模一样 —— 实测就这样丢了 **1429 格**。

    漏在哪：`do_gazetteer_boolean` 命中词表后返回 `{"term": ...}` 而**没有 value**
    （`do_gazetteer_enum` 第 591 行有，`do_table_lookup` 的 flag 模式第 874 行有
    `val = "是"`，只有 boolean 这一条漏了），而 `f3_escalate.merge` 组装时
    **只认 `evidence["value"]`** → 那 1429 格状态是「阳性」、值是 None。
    高发列全是布尔列：既往脑梗 94、住院阿托伐他汀 83、住院阿司匹林 76、
    住院氯吡格雷 56、既往高血压 54、住院丁苯酞 50、吸烟 27……

    放在契约层（而不是只补 `do_gazetteer_boolean` 那一处）是为了让**以后新增的
    规则类型**也跑不掉这个契约 —— 同一个坑不该踩第二次。

    取值优先级与 `_val_of`（第 934 行）保持一致：value > term > cell > 兜底「是」。
    兜底写「是」而不是留空：布尔列的阳性语义就是「有/是」，留空是丢信息。
    """
    if state != STATE_POS or not isinstance(ev, dict):
        return ev
    if ev.get("value") not in (None, ""):
        return ev
    for k in ("term", "cell", "match", "hit"):
        cand = ev.get(k)
        if cand not in (None, ""):
            ev["value"] = str(cand)
            ev["value_from"] = k          # 可审计：值是从哪个键回退来的
            return ev
    ev["value"] = "是"
    ev["value_from"] = "pos_default"
    return ev


def extract_one(col, entry, sections, tables):
    """★ 契约：第二项**永远是 dict**。

    这些早退分支原来返回的是**字符串**，而 `evidence` 在别处一律当 dict 用 ——
    下游任何消费方（不变量检查、升级桥、落盘）碰到 `done_by_f6` 那一列就会
    `'str' object has no attribute 'get'`。不变量检查第一次运行就抓到了。
    """
    spec = entry.get("extract") or {}
    kind = spec.get("kind")
    layer = entry.get("layer")

    if kind == "done_by_f6":
        return STATE_PENDING, {"why": "由 F6 打分链路产出，不在规则层"}, "f6"
    if kind == "manual":
        # 人工字段（`质控打分`）：抽取链路和模型**都不该**填它。
        # 原来它写成 `normalize: from=质控打分` —— **指向自己**的循环引用：
        # `apply_derived` 的 normalize 分支永远返回 STATE_PENDING，
        # 而配置文件上看着像"有定义"，查起来得顺着 from 找一圈才发现是自己。
        # 显式写成 manual，语义就摆在台面上，也能被配置完整性测试钉住。
        return STATE_PENDING, {"why": "人工字段，不由抽取链路填"}, layer
    if kind in ("llm_text", "small", "small_then_llm"):
        return STATE_PENDING, {"why": f"该列走 {kind}，规则层不判"}, layer
    if kind == "compute":
        return STATE_PENDING, {"why": f"compute 未在 pass 2 之前处理: "
                                      f"{spec.get('expr') or spec.get('op')}"}, layer
    if kind == "normalize":
        return STATE_PENDING, {"why": f"normalize 未在 pass 2 之前处理: "
                                      f"{spec.get('from')}"}, layer
    fn = DISPATCH.get(kind)
    if fn is None:
        return STATE_PENDING, {"why": f"未实现的 kind={kind}"}, layer

    state, ev = fn(spec, entry, sections, tables)
    if not isinstance(ev, dict):          # 兜底：任何实现都不许返回字符串
        ev = {"why": str(ev)}
    ev = ensure_pos_value(state, ev)      # ★ 契约：阳性必须带值（见函数文档）

    # ★ fallback：主抽取器没抽到，就换一个再试。
    #   实测：8 个体征列配的是 `table_lookup:生命体征表`，而真实文书里
    #   体温/呼吸/血压**根本不在表里** —— 它们是查体段里的一句话：
    #   「脉搏59次∕分，呼吸18次/分，血压159∕79mmHg，体温36.8℃，…体重65kg」。
    #   于是这些列全部静默落**阴性**，看起来就像"这份文书没记体征"。
    #   两边都试才不会因为上游把表格摊成了正文就丢数据。
    fb = spec.get("fallback")
    if state != STATE_POS and isinstance(fb, dict):
        fn2 = DISPATCH.get(fb.get("kind"))
        if fn2 is not None:
            st2, ev2 = fn2(fb, entry, sections, tables)
            if st2 == STATE_POS:
                if not isinstance(ev2, dict):
                    ev2 = {"why": str(ev2)}
                ev2["via"] = f"fallback:{fb.get('kind')}"
                ev2 = ensure_pos_value(st2, ev2)   # 契约对 fallback 同样生效
                return st2, ev2, layer
    return state, ev, layer


def confidence_of(state, ev, cfg_entry, kind, gate=None):
    """升级门控的判据（functional-spec §F3.3 的信号②③）

    ★ 关键在于：**「阴性」不是一个可信度，它是两个。**
      逐条否定证据（`否认高血压`）是高可信；
      而 `missing_means=否` 的兜底（「没找到就当没有」）只是**配置断言**，
      它的错法恰恰是假阴性 —— 在 F3 上比 F6 更危险。
      把这两者混成一个「阴性」，等于把配置的猜测和真实证据一起信了。

    `gate` 来自 `variables_f3.json` 的 `_gate`（见 `load_gate`）——
    不确定词表和窗口宽度**必须在配置里**，否则没法拿金标准去扫。
    """
    gate = gate or GATE_DEFAULTS
    words = gate.get("uncertain_words") or GATE_DEFAULTS["uncertain_words"]
    win = int(gate.get("uncertain_window") or
              GATE_DEFAULTS["uncertain_window"])

    if state == STATE_PENDING:
        return CONF_LOW, "待确认"

    if state == STATE_POS:
        # ★ 不确定词只看**紧邻**词条的那一小段。
        #   用 ±20 字的宽窗口会踩到章节标题：
        #   「拟诊讨论：初步诊断：1.癫痫，2.脑梗死个人史」里的「拟诊」
        #   离「脑梗」不到 20 字，于是把一条明确的诊断判成了低置信。
        #   **章节标题不是临床对冲措辞。**
        span = str(ev.get("span") or ev.get("cell") or ev.get("row") or "")
        if ev.get("span"):
            span = span[-win:]         # 只留词条紧邻的尾部
        hit = next((w for w in words if w in span), None)
        if hit:
            return CONF_LOW, f"证据紧邻出现不确定词「{hit}」（§F3.3 信号③）"
        if kind in ("regex", "table_lookup"):
            return CONF_HIGH, f"{kind} 结构化命中"
        if ev.get("term"):
            return CONF_HIGH, f"词典命中「{ev['term']}」"
        return CONF_MED, "命中但无具体词条"

    # STATE_NEG
    why = str(ev.get("why", ""))
    if "体健" in why:
        return CONF_MED, "整段体健式兜底（不是逐条证据）"
    if "配置断言" in why or "来源段存在" in why:
        # 「没找到就当没有」是**配置的猜测**，不是证据。
        # 它的错法恰恰是假阴性 —— 在 F3 上比 F6 更危险，所以只给 medium。
        return CONF_MED, "配置断言/段存在推断，非逐条证据"
    if "未命中" in why or "缺失语义" in why or "无法断定" in why:
        return CONF_LOW, why
    return CONF_HIGH, "逐条否定证据（前置/后置/管辖）"


# ── R1 病史来源拆分（2026-10-07 临床需求）───────────────────────────
# 临床同事：病史把「出院诊断」「出院其他诊断」「既往诊断」混在一起，无法分辨。
#
# ★ 旧列一律保留不动 —— 这里只做**条目级来源标注**，不删、不改任何既有取值。
#   不变量（逐例成立）：三个来源的条目数之和 == 拆分前三列的条目总数。
#
# ★ 文书里**没有**字面「出院其他诊断」「既往诊断」这两个标题（104 例 grep=0）。
#   三者的真实形态是：
#     出院诊断     = 「出院诊断」清单的第 1 条（主要诊断）
#     出院其他诊断 = 同一清单里第 2 条往后的那些
#     既往诊断     = 既往史/现病史叙述里引用的**既往住院**的出院诊断
#   所以来源按「这条记录读自哪个列（= 哪个文书位置）」判定，不按内容猜。
HISTORY_SOURCE_COLS = (
    ("出院主要诊断", "出院诊断"),
    ("出院其它诊断", "出院其他诊断"),
    ("既往其他病史", "既往诊断"),
)
HISTORY_SOURCES = tuple(src for _c, src in HISTORY_SOURCE_COLS)
HISTORY_ENTRY_SPLIT = re.compile(r"[，、;；\n\r]+")
# 占位说法不是病史条目 —— 不剔掉，来源计数会虚增。
HISTORY_NOISE = frozenset((
    "未提及", "未记录", "无", "无特殊", "无异常", "不详", "未知", "unknown",
    "N/A", "n/a", "暂无", "未见明显异常", "未见异常", "未见异常", "正常",
))


def split_history_entries(text):
    """一格病史文本 → 条目列表（占位词与单字噪声剔掉）。"""
    out = []
    for e in HISTORY_ENTRY_SPLIT.split(str(text or "")):
        e = e.strip().strip("。.!！?？:： 　")
        if len(e) < 2 or e in HISTORY_NOISE:
            continue
        out.append(e)
    return out


def attribute_history_sources(items):
    """给三个病史列的**每一条记录**标来源，并写进 `证据["来源条目"]`。

    返回 `{列: [{"条目":…, "来源":…}]}`。

    ★ 跨列同名（同一条既在出院诊断里、又在既往病史里）是临床要看的
      **混源信号**，单独标 `疑似重复来源`，**不删数据** —— 删了就对不上
      「拆分前后条数之和相等」这条不变量。
    """
    ent = {c: split_history_entries((items.get(c) or {}).get("值"))
           for c, _src in HISTORY_SOURCE_COLS}
    disch = set(ent.get("出院主要诊断", [])) | set(ent.get("出院其它诊断", []))
    res = {}
    for col, src in HISTORY_SOURCE_COLS:
        recs = []
        for e in ent.get(col, []):
            r = {"条目": e, "来源": src}
            if src == "既往诊断" and e in disch:
                r["疑似重复来源"] = "出院诊断/出院其他诊断"
            recs.append(r)
        res[col] = recs
        it = items.get(col)
        if isinstance(it, dict):
            ev = dict(it.get("证据") or {})
            ev["来源条目"] = recs
            ev["来源条数"] = len(recs)
            it["证据"] = ev
    return res


def history_source_counts(items):
    """`{来源: 条数}` —— 给「拆分前后对照」用。"""
    res = attribute_history_sources(items)
    return {src: sum(len(res.get(c, [])) for c, s in HISTORY_SOURCE_COLS if s == src)
            for src in HISTORY_SOURCES}


def run(sections, tables, variables, alias=None, expand=True):
    """★ `expand=True` 让所有调用方都自动吃到段名解析。

    不加这一层的代价是实测过的：`route_f3.py --selftest` 里 `主诉` 这类**字面段名**
    能直接命中，所以看着一切正常；但 `test_e2e_doc.py` 传的是整篇文书的段名，
    `一般情况` 的列 source_sections 写的是逻辑名 `病案首页` —— 
    **不展开就一个都取不到，26 列全部静默落待确认**，而输出上只看到「未命中」。
    """
    if expand:
        sections = expand_sections(
            sections, alias if alias is not None else load_alias())
    tables = index_tables(tables)      # 真实 {"tables":[…]} → {逻辑名: rows}
    out, stat = {}, {}
    gate = load_gate(variables)        # 门控参数在配置里，不写死（对齐 F6）
    sheets = sheets_of(variables)      # 跳过 `_gate` 这类元数据键
    esc_at = gate.get("escalate_at", CONF_LOW)
    order = [CONF_HIGH, CONF_MED, CONF_LOW]
    # 「置信度 ≤ escalate_at 就升级」。F6 用 margin_min 干同一件事。
    def should_esc(c):
        try:
            return order.index(c) >= order.index(esc_at)
        except ValueError:
            return c == CONF_LOW

    # ── pass 1：逐列独立抽取 ──
    for sheet, items in sheets.items():
        for e in items:
            kind = (e.get("extract") or {}).get("kind")
            if kind in ("compute", "normalize"):
                continue                      # 留到 pass 2
            state, ev, layer = extract_one(e["col"], e, sections, tables)
            conf, why = confidence_of(state, ev, e, kind, gate)
            out[e["col"]] = {"sheet": sheet, "state": state, "confidence": conf,
                             "conf_reason": why, "escalate": should_esc(conf),
                             "layer": layer, "extract": kind,
                             "missing_means": e.get("missing_means"),
                             "evidence": ev}

    # ── pass 2：派生列（依赖 pass 1 的结果）──
    for sheet, items in sheets.items():
        for e in items:
            spec = e.get("extract") or {}
            if spec.get("kind") not in ("compute", "normalize"):
                continue
            state, ev = apply_derived(spec, out)
            conf, why = confidence_of(state, ev, e, spec.get("kind"), gate)
            out[e["col"]] = {"sheet": sheet, "state": state, "confidence": conf,
                             "conf_reason": why, "escalate": should_esc(conf),
                             "layer": e.get("layer"), "extract": spec.get("kind"),
                             "missing_means": e.get("missing_means"),
                             "evidence": ev}

    for r in out.values():
        stat[r["state"]] = stat.get(r["state"], 0) + 1
        stat[f"conf:{r['confidence']}"] = stat.get(f"conf:{r['confidence']}", 0) + 1
    stat["escalate"] = sum(1 for v in out.values() if v["escalate"])
    stat["n"] = len(out)
    return out, stat


# ------------------------------------------------------------------ 自测
SELFTEST_SECTIONS = {
    "既往史": "既往体健，否认高血压、糖尿病史。吸烟30年，每日20支，已戒烟5年。",
    "个人史": "吸烟30年，每日20支，已戒烟5年。饮酒史：否认。",
    "诊断": "1.急性脑梗死 2.心房颤动 3.高血压病3级",
    "查体": "体温36.8℃ 脉搏78次/分 呼吸18次/分 血压150/90mmHg。",
    "病程记录": "患者神志清楚，NIHSS评分4分。ESSEN评分3分。",
}


def selftest():
    variables = json.loads((HERE / "variables_f3.json").read_text(encoding="utf-8"))
    out, stat = run(SELFTEST_SECTIONS, {}, variables)
    print("三态分布:", {k: v for k, v in stat.items()
                    if k in (STATE_POS, STATE_NEG, STATE_PENDING)})
    print("门控统计:", {k: v for k, v in stat.items() if k.startswith("conf:") or k in ("escalate", "n")})
    print(f"  → 规则层挡下 {stat['n']-stat['escalate']} 列 = "
          f"{1-stat['escalate']/stat['n']:.1%}，需升级 {stat['escalate']} 列")
    print()
    print("=== 抽查看几个关键列 ===")
    for c in ("既往高血压", "既往糖尿病", "既往房颤", "吸烟", "饮酒史",
              "吸烟量（支/天）", "吸烟年数", "戒烟年数", "ESSEN评分",
              "收缩压（mmHg）", "是否死亡", "住院天数（天）", "基线-NIHSS"):
        r = out.get(c)
        print(f"  {c:22} {r['state']:4} {r['confidence']:6} "
              f"esc={'Y' if r['escalate'] else 'n'}  {r['conf_reason']}")
    print()
    bad = [c for c, r in out.items() if r["state"] not in
           (STATE_POS, STATE_NEG, STATE_PENDING)]
    assert not bad, f"有非法状态: {bad[:5]}"
    assert out["既往高血压"]["state"] == STATE_NEG
    assert out["吸烟"]["state"] == STATE_POS, "个人史里的吸烟应命中"
    assert out["吸烟量（支/天）"]["evidence"].get("value") == "20", "每日20支应抽出 20"
    assert out["ESSEN评分"]["evidence"].get("value") == "3", "ESSEN 应抽出 3"
    assert out["饮酒史"]["state"] == STATE_NEG, "「饮酒史：否认」是**后置否定**，须判阴性"
    print("自测通过：三态合法 / 体健式否定只在病史段生效 / 前置+后置否定都生效 / 正则抽出数值")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections")
    ap.add_argument("--blocks", help="含块正文的 json（compiled.json / pages.json）—— "
                                     "sections_text.json 的段只有 refs 时需要")
    ap.add_argument("--tables")
    ap.add_argument("--vars", default=str(HERE / "variables_f3.json"))
    ap.add_argument("--out")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--dump-sections", action="store_true",
                    help="只打印读到的段名与长度，不跑抽取")
    ap.add_argument("--escalate-out", help="把需升级模型列清单写到这个文件")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        return

    if not a.sections:
        ap.error("要么给 --sections，要么用 --selftest")

    sec = load_sections(a.sections, a.blocks)
    print(f"[info] 读到 {len(sec)} 段", file=sys.stderr)
    alias = load_alias()
    sec = expand_sections(sec, alias)
    # ★ dump 必须在展开**之后**。真实文书展开前只有 10 段
    #   （`_其他` 一坨 35,046 字），展开后 46 段。
    #   先 dump 会让人以为段名解析没生效 —— 我自己就被骗过一次。
    if a.verbose or a.dump_sections:
        print(f"[info] 展开后 {len(sec)} 段", file=sys.stderr)
    if a.dump_sections:
        dump_sections(sec)
        return
    tbl = json.loads(Path(a.tables).read_text(encoding="utf-8")) if a.tables else {}
    variables = json.loads(Path(a.vars).read_text(encoding="utf-8"))

    out, stat = run(sec, tbl, variables)
    if a.out:
        Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    esc = sorted(c for c, r in out.items() if r["escalate"])
    if a.escalate_out:
        Path(a.escalate_out).write_text(
            json.dumps({"need_model": esc, "n": len(esc)}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    n = stat.get("n", 0)
    print(json.dumps({k: v for k, v in stat.items()}, ensure_ascii=False))
    print(f"\n共 {n} 列：阳性 {stat.get(STATE_POS,0)}  阴性 {stat.get(STATE_NEG,0)}  "
          f"待确认 {stat.get(STATE_PENDING,0)}", file=sys.stderr)
    print(f"置信：high {stat.get('conf:high',0)}  medium {stat.get('conf:medium',0)}  "
          f"low {stat.get('conf:low',0)}", file=sys.stderr)
    print(f"**需升级模型 {len(esc)} 列 = {len(esc)/max(n,1):.1%}**"
          f"（规则层挡掉 {n-len(esc)} 列 = {1-len(esc)/max(n,1):.1%}）", file=sys.stderr)
    if a.escalate_out:
        print(f"升级清单已写 {a.escalate_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
