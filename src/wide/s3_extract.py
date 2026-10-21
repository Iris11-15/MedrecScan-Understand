#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段③ s3_extract.py —— 首页字段候选 + 首程「主诉/查体」原文。

输入   run/wide/<batch>/02_text/<case_id>/page_NNNN.json
       run/wide/<batch>/00_index/page_index.json      （哪些页是首页 / 首程）
输出   run/wide/<batch>/03_extract/<case_id>/frontpage.json      四种策略的候选值
       run/wide/<batch>/03_extract/<case_id>/first_progress.json 主诉 / 查体（原文）
       run/wide/<batch>/03_extract/<case_id>/label_inventory.json 首页上实际出现的标签写法
       run/wide/<batch>/03_extract/<case_id>/_manifest.json  + .done / .incomplete

风险①的对照实验就在这一层：四种策略各跑一遍、各记命中数，谁强用谁（报告里有数）。
  cell  表格单元格内「标签+值」粘连拆分（实测首页 OCR 是 "姓名潘满兴" 这种粘连）
  block 整块文本按标签词切分，标签之间的片段当值
  coord 块级坐标邻近配对：标签块 ↔ 右侧/下方最近的值块
  llm   Strata 温度 0，只取 content（思维链 reasoning_content 一律丢弃）
  rule  首页 B 类列的规则层（标签词表 + 正则 + 校验），**不调 LLM**（round18 第3步）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wide import common as C  # noqa: E402

# round18 第3步：首页规则层。落地位置 = src/wide/frontpage_rules.py；
# 工地阶段它和本文件同在 workspace/round18/patched/ → 按 __file__ 同目录加载。
try:
    from wide import frontpage_rules as FR  # noqa: E402
except ImportError:
    import importlib.util as _ilu
    _spec_fr = _ilu.spec_from_file_location(
        "wide.frontpage_rules", Path(__file__).resolve().parent / "frontpage_rules.py")
    FR = _ilu.module_from_spec(_spec_fr)
    sys.modules["wide.frontpage_rules"] = FR
    _spec_fr.loader.exec_module(FR)

STAGE = "03_extract"
NULL_WORDS = {"", "-", "—", "/", "无", "未提及", "null", "None"}


# --------------------------------------------------------------- 标签词表
def load_vocab() -> tuple[dict, dict]:
    v = C.load_yaml(C.CONFIG_DIR / "label_vocab.yaml")
    alias2canon: dict[str, str] = {}
    for item in v["labels"]:
        for a in [item["canonical"], *(item.get("aliases") or [])]:
            a = str(a).strip()
            if a:
                alias2canon.setdefault(a, item["canonical"])
    return alias2canon, v


def label_pattern(alias2canon: dict) -> re.Pattern:
    """按标签长度倒序，优先匹配长写法（"联系人电话" 要赢过 "电话"）。"""
    alts = sorted(alias2canon, key=len, reverse=True)
    body = "|".join(re.escape(a) for a in alts)
    return re.compile(rf"(?P<label>{body})")


def extract_pairs(text: str, pat: re.Pattern) -> list[tuple[str, str]]:
    """按标签出现位置切值：值 = 本标签末尾 到 下一个标签开头。

    实测首页 OCR 是 "姓名潘满兴 性别男 年龄59岁" 这种**无分隔粘连**，
    用 [^\n]* 贪婪取会把后面几个字段全吞进第一个值里。
    """
    ms = list(pat.finditer(text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        val = text[m.end():end].strip(" :：	")
        val = val.split("\n")[0].strip()
        if val:
            out.append((m.group("label"), val))
    return out


# --------------------------------------------------------------- 策略 A/B/C
def strat_cell(pages: list[dict], alias2canon: dict, pat: re.Pattern) -> dict:
    out: dict[str, list] = {}
    for pg in pages:
        for b in pg.get("blocks", []):
            for row in (b.get("cells") or []):
                for cell in row:
                    cell = (cell or "").strip()
                    if not cell:
                        continue
                    for label, val in extract_pairs(cell, pat):
                        out.setdefault(label, []).append(
                            {"value": val, "page": pg["page"], "where": "cell"})
    return out


def strat_block(pages: list[dict], alias2canon: dict, pat: re.Pattern) -> dict:
    out: dict[str, list] = {}
    for pg in pages:
        for b in pg.get("blocks", []):
            text = b.get("text") or ""
            for label, val in extract_pairs(text, pat):
                out.setdefault(label, []).append(
                    {"value": val, "page": pg["page"], "where": "block"})
    return out


def strat_coord(pages: list[dict], alias2canon: dict, pat: re.Pattern) -> dict:
    """块级坐标邻近：文本几乎就是标签的块，配右侧（同带）或下方最近的块。"""
    out: dict[str, list] = {}
    for pg in pages:
        blocks = [b for b in pg.get("blocks", []) if b.get("block_bbox")]
        for b in blocks:
            t = (b.get("text") or "").strip()
            if not t or len(t) > 12 or t not in alias2canon:
                continue
            x1, y1, x2, y2 = b["block_bbox"]
            best, best_d = None, 1e18
            for o in blocks:
                if o is b:
                    continue
                ox1, oy1, ox2, oy2 = o["block_bbox"]
                cy = (oy1 + oy2) / 2
                if not (y1 - 6 <= cy <= y2 + 6):            # 同一水平带
                    continue
                d = ox1 - x2
                if d < -6:
                    continue
                if d < best_d:
                    best, best_d = o, d
            if best is None:                                # 退而求其次：正下方
                for o in blocks:
                    if o is b:
                        continue
                    ox1, oy1, ox2, oy2 = o["block_bbox"]
                    cx = (ox1 + ox2) / 2
                    if not (x1 - 6 <= cx <= x2 + 6):
                        continue
                    d = oy1 - y2
                    if d < -6 or d >= best_d:
                        continue
                    best, best_d = o, d
            if best and (best.get("text") or "").strip():
                out.setdefault(t, []).append({"value": best["text"].strip(),
                                              "page": pg["page"], "where": "coord",
                                              "gap": round(best_d, 1)})
    return out


# --------------------------------------------------------------- 策略 D：LLM
SYSTEM_PROMPT = (
    "你是病历结构化抽取器，只做逐字取值，不做推断。\n"
    "规则：\n"
    "1) 值必须能在给定文本里原样找到（可以只去掉多余空格）。找不到就输出 null。\n"
    "2) 绝不用「未提及」「无」「否」「未知」这类占位词冒充有值；文本里是空栏也输出 null。\n"
    "3) 不改写、不纠错、不补标点、不做单位换算。\n"
    "4) 只输出一个 JSON 对象，不要解释、不要代码块以外的文字。\n"
    "5) 每个键的取值形态：{\"value\": <字符串或 null>, \"label\": <首页上该字段的实际标签写法，没有则 null>}"
)


def llm_call(cfg_llm: dict, system: str, user: str) -> tuple[str, str, str, str, dict]:
    """调 Strata。返回 (content, err, raw_sha, finish_reason, usage)。思维链 reasoning_content 一律丢弃。

    实测坑：该模型 completion_tokens 含思维链，max_tokens 给小了会 finish_reason=length
    且 content 为空 —— 必须当失败，不能拿半截输出。

    步③（2026-10-10）：`llm.enable_thinking` 写进配置 → 透传 vLLM 的
    `chat_template_kwargs: {"enable_thinking": false}`（旧产线一直在用，见 src/f3_escalate.py:494、
    src/f3_batch.py:410、src/arbiter.py:69）。实测同一 prompt：completion_tokens 207→54、
    reasoning_content 222→0 字符、content 逐字不变。不写该键 = 不发参数 = 旧行为。
    usage 一律回传（失败分支给 {}），供 completion_tokens 前后对照。
    """
    payload = {"model": cfg_llm["model"], "temperature": float(cfg_llm["temperature"]),
               "top_p": float(cfg_llm.get("top_p", 1.0)),
               "max_tokens": int(cfg_llm.get("max_tokens", 4096)),
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    if "enable_thinking" in cfg_llm:
        payload["chat_template_kwargs"] = {"enable_thinking": bool(cfg_llm["enable_thinking"])}
    url = cfg_llm["base_url"].rstrip("/") + cfg_llm["endpoint"]
    status, js, e = C.http_json(url, payload, int(cfg_llm["timeout_s"]),
                                int(cfg_llm.get("retries", 0)),
                                auth_header="Bearer " + str(cfg_llm["api_key"]))
    if js is None:
        return "", f"status={status} {e}", "", "", {}
    usage = {k: js.get("usage", {}).get(k) for k in
             ("prompt_tokens", "completion_tokens", "total_tokens")} if js.get("usage") else {}
    raw_sha = C.sha256_bytes(C.canon_json(js).encode("utf-8"))
    try:
        msg = js["choices"][0]["message"]
        finish = js["choices"][0].get("finish_reason") or ""
    except (KeyError, IndexError, TypeError):
        return "", "响应没有 choices[0].message", raw_sha, "", usage
    content = msg.get(cfg_llm.get("take_field", "content")) or ""
    if finish == "length" and bool(cfg_llm.get("abort_on_length", True)):
        return "", f"finish_reason=length（max_tokens={payload['max_tokens']} 被思维链吃满，" \
                   f"content 长度 {len(content)}）—— 拒绝用截断输出", raw_sha, finish, usage
    if not content and "reasoning_content" in msg:
        return "", "content 为空且只有 reasoning_content（拒绝用思维链当结果）", raw_sha, finish, usage
    return content, "", raw_sha, finish, usage


def parse_json_loose(text: str) -> tuple[dict | None, str]:
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t), ""
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", t, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0)), ""
        except json.JSONDecodeError as e:
            return None, f"JSON 解析失败 {e}"
    return None, "响应里找不到 JSON"


def build_frontpage_prompt(cols_cfg: dict, page_text: str,
                           only: list[str] | None = None) -> str:
    """首页 prompt。round18 第3步两处改动（都是纯减法，不动正文）：
      · 镜像列（columns.yaml: mirror_of）不进 prompt —— s4_normalize.py:380-388
        会用基准列的值覆盖它，问 LLM 是纯浪费 completion token（实测 150/150 例）。
      · only= 只问规则层没拿到的列（兜底路径专用）。
    """
    cols = [c for c in cols_cfg["columns"]
            if c["source"] in ("frontpage",) and not c.get("mirror_of")]
    if only is not None:
        cols = [c for c in cols if c["id"] in set(only)]
    lines = [f"- {c['id']} = {c['header']}" for c in cols]
    return ("【要抽的列（id = 列名）】\n" + "\n".join(lines) +
            "\n\n【病历首页文本（OCR 结果，可能含表格行，用 | 分隔单元格）】\n" +
            page_text +
            "\n\n【输出】对上面每一列各给一个键；没有对应内容时 value 必须是 null。")


def build_progress_prompt(sections: dict, page_text: str) -> str:
    keys = "\n".join(f"- {k}（首页上可能写成：{'、'.join(v)}）" for k, v in sections.items())
    return ("【要原文照抄的两节】\n" + keys +
            "\n\n【首次病程记录文本】\n" + page_text +
            "\n\n【输出】JSON：{\"主诉\": {\"value\": \"…\", \"heading\": \"实际小标题\"}, "
            "\"查体\": {\"value\": \"…\", \"heading\": \"实际小标题\"}}；找不到就 value=null。")


def verbatim_check(values: dict, text: str) -> dict:
    """红线：主诉/查体必须「原文照抄」。空白归一化后做子串核对，
    只记布尔与长度，**不回显原文**（PII）。

    round35 草案（未落地）：再加 `absent_char_count` —— 值里有多少个字符
    在首程原文中**任何位置都不出现**。0 ⇒ 值逐字来自原文、只是不连续
    （页眉页脚被 OCR 内联进正文 / 同一段在页上印了多份 / 查体跨页）；
    >0 ⇒ 值里确有原文没有的字符 ⇒ 才是真「改写」。
    **不改 `verbatim` 布尔 ⇒ QC 命中数一个都不变**
    （红线：不许为了指标好看放宽判据）。"""
    norm = re.sub(r"\s+", "", text or "")
    out: dict = {}
    for k, v in (values or {}).items():
        val = v.get("value") if isinstance(v, dict) else v
        if val is None:
            out[k] = {"present": False}
            continue
        s = str(val)
        a = re.sub(r"\s+", "", s)
        charset = set(norm)
        absent = sum(1 for ch in a if ch not in charset)
        out[k] = {"present": True, "len": len(s),
                  "verbatim": bool(s) and a in norm,
                  "absent_char_count": absent}
    return out


# --------------------------------------------------------------- 单例
def pick_pages(index_entry: dict, stage_pages: dict, kind: str) -> list[dict]:
    want = set(index_entry.get(f"{kind}_pages") or [])
    return [p for pno, p in sorted(stage_pages.items()) if pno in want]


def page_variants(pages: list[dict]) -> list[dict]:
    """一页 → 主变体 + 各副变体（blocks_<v>）。副变体没有表格结构，但勾选框选中项在它里面。"""
    out: list[dict] = []
    for p in pages:
        out.append(p)
        for k in p:
            if k.startswith("blocks_"):
                q = dict(p)
                q["blocks"] = p[k]
                q["variant"] = k[len("blocks_"):]
                out.append(q)
    return out


def page_text(p: dict) -> str:
    """页文本 = 主变体文本 + 各副变体文本（明确标注来源，不混为一谈）。"""
    t = p.get("text") or ""
    for k in p:
        if k.startswith("text_") and not k.startswith("text_sha256"):
            t += f"\n\n【{k[len('text_'):]} 模式同一页的另一种 OCR】\n" + (p[k] or "")
    return t


def process_case(row: dict, cfg: dict, cols_cfg: dict, batch_dir: Path,
                 audit: C.Audit, *, use_llm: bool, strategies: list[str],
                 force: bool, quiet: bool) -> dict:
    case = row["case_id"]
    src = batch_dir / "02_text" / case
    dst = batch_dir / "03_extract" / case
    dst.mkdir(parents=True, exist_ok=True)
    (dst / ".done").unlink(missing_ok=True)
    (dst / ".incomplete").unlink(missing_ok=True)

    stage_pages = {}
    for pj in sorted(src.glob("page_*.json")):
        r = C.read_json(pj)
        # 低信息页（page_kind=low_info）允许参与抽取，但命中幻觉监视词的一律排除。
        if r.get("ocr_status") == "ok" and r.get("page_kind") in ("ok", "low_info") \
                and not r.get("watchwords"):
            stage_pages[r["page"]] = r
        elif r.get("watchwords"):
            C.warn(f"{case} page {r.get('page')}: 命中幻觉监视词 {r['watchwords']} → 不参与抽取")
    idx = {}
    ip = Path(cfg["locator"].get("index_file") or "") if cfg["locator"].get("index_file") else None
    if not ip or not ip.exists():
        ip = batch_dir / "00_index" / "page_index.json"
    if ip.exists():
        idx = C.read_json(ip).get("cases", {}).get(case, {})
    if not idx:
        # 兜底：按页文本里的关键词自己判页（不猜页号）
        v = load_vocab()[1]
        fp = {p for p, r in stage_pages.items()
              if any(k in r["text"] for k in cfg["locator"]["frontpage_keywords"])}
        pn = {p for p, r in stage_pages.items()
              if any(k in r["text"] for k in cfg["locator"]["first_progress_keywords"])}
        idx = {"frontpage_pages": sorted(fp), "first_progress_pages": sorted(pn)}

    fp_pages = pick_pages(idx, stage_pages, "frontpage")
    pn_pages = pick_pages(idx, stage_pages, "first_progress")
    # round 7 第1件：进 prompt 的页里有哪些是被 max_tokens 截断 / 复读循环的，必须记账。
    # 之前这里完全看不见 truncated → 坏页当完整页用，事后也查不出来。
    _used = list(fp_pages) + list(pn_pages)
    page_health = {
        "pages_truncated": sorted({p["page"] for p in _used if p.get("truncated")}),
        "pages_repeat_loop": sorted({p["page"] for p in _used if p.get("repeat_loop")}),
    }
    audit.emit(case_id=case, kind="page_health", **page_health)
    alias2canon, vocab = load_vocab()
    pat = label_pattern(alias2canon)

    results: dict[str, dict] = {}
    for s in strategies:
        fn = {"cell": strat_cell, "block": strat_block, "coord": strat_coord}[s]
        results[s] = fn(page_variants(fp_pages), alias2canon, pat)

    # round18 第3步：B 类列改由规则层产出。键 = **列 id**（不是标签）→ 下游单独取。
    # 扫 page_text 而不是 block.text：与 s4 来源守卫 J3 的核对文本逐字同一份
    # （s4_normalize.py:280-291），且跨单元格的值只在 page_text 里连续。
    rule_hits = FR.run_frontpage(fp_pages, alias2canon, pat)
    results["rule"] = rule_hits
    missing = FR.missing_columns(rule_hits, cols_cfg)
    audit.emit(case_id=case, kind="rule_layer", n_rule_columns_hit=len(rule_hits),
               rule_columns=sorted(rule_hits), n_missing_rule_columns=len(missing),
               missing_rule_columns=missing)

    inventory: dict[str, dict] = {}
    for s, res in results.items():
        if s == "rule":
            continue            # 规则层的键是列 id 不是标签 → 不进 label_inventory
        for label, hits in res.items():
            e = inventory.setdefault(label, {"label_as_written": label,
                                             "canonical": alias2canon.get(label),
                                             "strategies": set(), "n_hits": 0, "examples": []})
            e["strategies"].add(s)
            e["n_hits"] += len(hits)
            for h in hits[:2]:
                if len(e["examples"]) < 2:
                    e["examples"].append({"value": h["value"][:60], "where": h["where"]})
    for e in inventory.values():
        e["strategies"] = sorted(e["strategies"])

    llm_out: dict = {"status": "skipped"}
    if use_llm and fp_pages and not missing:
        # round18 第3步：12 个 B 类列规则层**全部**拿到 → 首页这次 LLM 调用整个省掉。
        # 判据是 missing 为空，不是「命中数够多」—— 少一列就照常调（红线⑦）。
        text = "\n\n".join(page_text(p) for p in fp_pages)
        llm_out = {"status": "skipped_rules", "values": {},
                   "input_text_sha256": C.sha256_text(text),
                   "n_rule_columns": len(rule_hits), "missing_columns": []}
        C.log(f"    首页 LLM 跳过（规则层 {len(rule_hits)}/{len(FR.RULES)} 列全命中）",
              quiet=quiet)
    if use_llm and fp_pages and missing:
        # 兜底：只问规则层没拿到的那几列（镜像列已在 build_frontpage_prompt 里排除）。
        text = "\n\n".join(page_text(p) for p in fp_pages)
        user = build_frontpage_prompt(cols_cfg, text, only=missing)
        sys_sha, user_sha = C.sha256_text(SYSTEM_PROMPT), C.sha256_text(user)
        t0 = time.time()
        content, e, raw_sha, finish, usage = llm_call(cfg["llm"], SYSTEM_PROMPT, user)
        parsed, pe = parse_json_loose(content)
        audit.emit(case_id=case, kind="prompt_frontpage", system_sha256=sys_sha,
                   prompt_sha256=user_sha, input_text_sha256=C.sha256_text(text),
                   response_sha256=raw_sha, content_sha256=C.sha256_text(content),
                   model=cfg["llm"]["model"], temperature=cfg["llm"]["temperature"],
                   finish_reason=finish, max_tokens=cfg["llm"].get("max_tokens"),
                   enable_thinking=bool(cfg["llm"].get("enable_thinking", True)),
                   prompt_tokens=usage.get("prompt_tokens"),
                   completion_tokens=usage.get("completion_tokens"),
                   elapsed_s=round(time.time() - t0, 1), error=e or pe)
        llm_out = {"status": "ok" if parsed else "parse_error",
                   "prompt_sha256": user_sha, "system_sha256": sys_sha,
                   "input_text_sha256": C.sha256_text(text),
                   "response_sha256": raw_sha, "content_sha256": C.sha256_text(content),
                   "elapsed_s": round(time.time() - t0, 1), "error": e or pe,
                   "finish_reason": finish,
                   "enable_thinking": bool(cfg["llm"].get("enable_thinking", True)),
                   "usage": usage,
                   "values": parsed or {},
                   "missing_columns": missing}
        C.log(f"    首页 LLM {llm_out['status']} {llm_out['elapsed_s']}s", quiet=quiet)

    prog: dict = {"status": "skipped", "pages": [p["page"] for p in pn_pages]}
    if use_llm and pn_pages:
        text = "\n\n".join(page_text(p) for p in pn_pages)
        user = build_progress_prompt(vocab["progress_sections"], text)
        t0 = time.time()
        content, e, raw_sha, finish, usage = llm_call(cfg["llm"], SYSTEM_PROMPT, user)
        parsed, pe = parse_json_loose(content)
        audit.emit(case_id=case, kind="prompt_first_progress",
                   system_sha256=C.sha256_text(SYSTEM_PROMPT), prompt_sha256=C.sha256_text(user),
                   input_text_sha256=C.sha256_text(text), response_sha256=raw_sha,
                   content_sha256=C.sha256_text(content), model=cfg["llm"]["model"],
                   temperature=cfg["llm"]["temperature"],
                   finish_reason=finish, max_tokens=cfg["llm"].get("max_tokens"),
                   enable_thinking=bool(cfg["llm"].get("enable_thinking", True)),
                   prompt_tokens=usage.get("prompt_tokens"),
                   completion_tokens=usage.get("completion_tokens"),
                   elapsed_s=round(time.time() - t0, 1), error=e or pe)
        prog = {"status": "ok" if parsed else "parse_error",
                "pages": [p["page"] for p in pn_pages],
                "prompt_sha256": C.sha256_text(user),
                "input_text_sha256": C.sha256_text(text),
                "response_sha256": raw_sha, "content_sha256": C.sha256_text(content),
                "elapsed_s": round(time.time() - t0, 1), "error": e or pe,
                "finish_reason": finish,
                "enable_thinking": bool(cfg["llm"].get("enable_thinking", True)),
                "usage": usage,
                "values": parsed or {}}
        prog["verbatim"] = verbatim_check(parsed or {}, text)
        C.log(f"    首程 LLM {prog['status']} {prog['elapsed_s']}s", quiet=quiet)

    C.write_json(dst / "frontpage.json", {
        "case_id": case, "frontpage_pages": [p["page"] for p in fp_pages],
        "locator": idx.get("locator_source", "keyword_fallback"),
        "strategies": results, "llm": llm_out})
    C.write_json(dst / "first_progress.json", {"case_id": case, **prog})
    C.write_json(dst / "label_inventory.json",
                 {"case_id": case, "vocab_version": vocab["version"],
                  "inventory": dict(sorted(inventory.items()))})
    summary = {"case_id": case, "frontpage_pages": [p["page"] for p in fp_pages],
               "first_progress_pages": [p["page"] for p in pn_pages],
               "pages_low_info_used": [p["page"] for p in list(fp_pages) + list(pn_pages)
                                       if p.get("page_kind") == "low_info"],
               "n_labels_seen": len(inventory),
               "strategy_hits": {s: sum(len(v) for v in r.values()) for s, r in results.items()},
               "n_rule_columns": len(rule_hits), "rule_missing_columns": missing,
               "llm_status": llm_out["status"], "progress_status": prog["status"]}
    summary.update(page_health)
    C.write_json(dst / "_manifest.json", summary)
    bad = (use_llm and (llm_out["status"] not in ("ok", "skipped_rules")
                        or prog["status"] != "ok"))
    C.write_marker(dst / ".incomplete" if bad else dst / ".done")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="阶段③ 结构化抽取")
    ap.add_argument("--batch", required=True)
    C.add_case_args(ap)
    ap.add_argument("--strategies", default="cell,block,coord",
                    help="确定性策略子集：cell,block,coord")
    ap.add_argument("--no-llm", action="store_true", help="只跑确定性策略（对照实验用）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--strict-progress", action="store_true",
                    help="旧口径：首程（first_progress）抽取失败也算 rc=1。"
                         "默认**不算** —— 首页成功、只有首程 parse_error/repeat_loop 时，"
                         "run_steps 会在 s3 处 break，整例连阶段④都进不去 ⇒ 整例从宽表消失")
    a = ap.parse_args(argv)
    cfg = C.load_pipeline()
    cols = C.load_columns()
    batch_dir = Path(cfg["paths"]["run_root"]) / a.batch
    audit = C.Audit(batch_dir / "_audit.jsonl", STAGE)
    rows = S_read(batch_dir)
    want = C.wanted_cases(a.case, a.case_file)
    if want:
        rows = [r for r in rows if r["case_id"] in want]
    n_bad = 0
    n_soft = 0
    for r in rows:
        dst = batch_dir / STAGE / r["case_id"]
        if (dst / ".done").exists() and not a.force:
            C.log(f"  跳过（已 .done）{r['case_id']}", quiet=a.quiet)
            continue
        s = process_case(r, cfg, cols, batch_dir, audit, use_llm=not a.no_llm,
                         strategies=[x for x in a.strategies.split(",") if x],
                         force=a.force, quiet=a.quiet)
        C.log(f"  {r['case_id']}: {s['strategy_hits']} llm={s['llm_status']} "
              f"progress={s['progress_status']}", quiet=a.quiet)
        hard_fail = s["llm_status"] not in ("ok", "skipped", "skipped_rules")
        soft_fail = s["progress_status"] not in ("ok", "skipped")
        if hard_fail:
            n_bad += 1
        elif soft_fail:
            n_soft += 1
            cid = r["case_id"]
            pstat = s["progress_status"]
            C.warn(f"{cid}: 首程抽取失败（progress={pstat}）"
                   " ⇒ 首页字段照常进阶段④，首程来源的列按缺失处理"
                   "（--strict-progress 恢复旧口径）")
            C.write_json(dst / ".progress_failed",
                         {"case_id": cid, "progress_status": pstat})
            if a.strict_progress:
                n_bad += 1
    C.log(f"完成：{len(rows)} 例，{n_bad} 例 LLM 环节失败，{n_soft} 例仅首程失败（不判失败）")
    return 1 if n_bad else 0


def S_read(batch_dir: Path) -> list[dict]:
    from wide import s1_ocr as S1
    return S1.read_manifest(batch_dir / "00_index" / "manifest.tsv")


if __name__ == "__main__":
    raise SystemExit(main())
