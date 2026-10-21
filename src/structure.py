#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F3：把 F2 的编译产物交给小模型，做「结构化 + 清理无关数据」。

输入（一个 compile.py 的输出目录）
    sections_text.json   ★ 优先：F2 按章节渲染好的送模文本（表格矩阵内联、
                           缺失/占位/截断标记保留、块引用带页码）
    compiled.json        逐块索引：block_id / page / label / route / content / table_index
    sections.json        章节 -> block_id 列表（阅读顺序）
    tables.json          表格矩阵（header + rows + abnormal），表格块在 compiled.json 里是空的
    compile_report.json  质量报告（含 alerts）
    compiled.md          拼好的 Markdown（--whole-file 时用）

    ★ 有 sections_text.json 就用它，没有才退回 build_chunks 的旧路径。
      旧路径有两个已确认的缺陷（详见 chunks_from_sections_text 的注释）：
      block_id 跨页重复导致的查表错配，以及缺失标记根本到不了模型。

输出
    structured.json      每个 chunk 的模型原始结果 + 合并后的条目；**每完成一块就落盘**

四个关键设计
  1. **按 section 分块**：小模型 max_model_len=16384，一份病历编译后可能超长。
     sections.json 正好给出章节边界 —— 按章送、结果再合并。
  2. **表格单独渲染**：compiled.json 里表格块的 content 是空的（矩阵在 tables.json），
     这里把 header+rows 拼成网格文本一起送，否则化验单会被整个漏掉。
  3. **约束解码**：请求带 response_format(json_schema)，让 8B 也吐合法 JSON。
     服务端不支持时用 --no-json-schema 退回提示词约束 + 容错解析。
  4. **质量闸门**：compile_report.json 的 alerts 里若有「不要进入 F3」，
     默认拒绝执行（--force 跳过）—— 缺失率高时进 F3 只是把错误放大。

用法
    python3 structure.py jobs/b1/compiled/<stem> --url http://127.0.0.1:8101 --model qwen-small
    python3 structure.py <目录> --dry-run              # 只看会发什么，不联网
    python3 structure.py <目录> --schema my.json --system prompt.txt
    python3 structure.py <目录> --resume               # 跳过已完成的 chunk

⚠️ 默认提示词与 schema 是**成对的**，会按实际数据源自动挑选（sections_text 版 /
   旧路径版）。同目录下若有 f3_system.txt / f3_schema.json，优先用它们 —— 那样
   提示词只有一份真相源。要换成你们自己的目标字段表，用 --schema / --system。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ★ 默认提示词/schema 与「数据源」必须成对，否则模型拿到的输入和被告知的格式不一致。
#   数据源由 compile.py 是否产出 sections_text.json 决定（见 chunks_from_sections_text），
#   所以这里也准备两套默认值，由 main() 按实际数据源挑选。
#
#   优先级：命令行 --system/--schema  >  structure.py 同目录下的
#           f3_system.txt / f3_schema.json  >  下面这两套内置常量
DEFAULT_SYSTEM_FILE = HERE / "f3_system.txt"
DEFAULT_SCHEMA_FILE = HERE / "f3_schema.json"

# ---- 新契约：数据来自 sections_text.json（块引用带页码 + 缺失标记 + 完整表格矩阵）
DEFAULT_SYSTEM_TEXT = """你是病历信息抽取器。目标是把病历拆成可以入表的结构化记录。

输入是病历某一章的 OCR 文本。每段开头有块引用，形如 【text p12-b3】/【table p29-b4】，
p 后面是页码，b 后面是块号。

文本里可能夹着这类注释，表示「这里原本有内容但没取到」，当成缺失，不要脑补：
    <!-- 缺失 p3-b15：table，VL 服务不可用 -->
    <!-- 截断 p7-b9：table，内容被截断 -->
    <!-- 占位 p8-b11：图片（无文本） -->

表格以网格文本给出，列用 | 分隔，第一行通常是表头，末尾可能有「异常项：…」。

只输出一个 JSON 对象：相关 / 理由 / 条目。
条目里每一条 = 目标表格里的一行，四个字段：
    类别 —— 一般情况 / 诊断 / 化验 / 检查 / 用药 / 手术 / 护理 / 其他
    项目 —— 这一行的主键（诊断名 / 检验项名 / 药品名 / 检查项目名）
    值   —— 对应内容，40 字以内；单位、参考范围、↑↓ 可以并进来
    出处 —— 只写块引用，不要抄原文；表格行写成 p12-b4#白细胞

规则：只依据原文；出处必须是正文里真实出现过的引用；缺失位置不要生成条目；
条目最多 60 条；不要重复汇总；只输出 JSON。"""

# ---- 旧契约：数据来自 build_chunks（没有页码、没有缺失标记、表格只有锚点）
DEFAULT_SYSTEM_LEGACY = """你是病历结构化助手。输入是病历中某一章的 OCR 文本，可能包含：
- 表格锚点注释 <!-- 表格 p1-t1 -->
- 缺失/占位/截断注释 <!-- 缺失 ... --> <!-- 占位 ... -->
- 已展开成网格的表格文本（用 | 分隔列）

请完成两件事：
1) 判断这一章是否与「病历信息结构化」相关；
2) 把相关的内容整理成结构化条目。

规则：
- 只依据原文，绝对不要补充原文里没有的内容；
- 每条条目都要给出原文片段作为 原文 证据；
- 无关内容（医嘱执行单、护理记录、签字时间表、宣教单、页眉页脚残留等）
  把 相关 设为 false，并在 理由 里说明原因，条目 留空数组；
- 只输出 JSON，不要解释，不要 markdown 代码块。"""

# ---- 两套 schema，字段必须与上面的提示词对得上
DEFAULT_SCHEMA_TEXT = {
    "type": "object",
    "properties": {
        "相关": {"type": "boolean"},
        "理由": {"type": "string"},
        "条目": {"type": "array", "maxItems": 60, "items": {
            "type": "object",
            "properties": {
                "类别": {"type": "string",
                         "enum": ["一般情况", "诊断", "化验", "检查", "用药",
                                  "手术", "护理", "其他"]},
                "项目": {"type": "string"},
                "值": {"type": "string"},
                "出处": {"type": "string"}},
            "required": ["类别", "项目", "值", "出处"],
            "additionalProperties": False}},
    },
    "required": ["相关", "条目"],
    "additionalProperties": False,
}

DEFAULT_SCHEMA_LEGACY = {
    "type": "object",
    "properties": {
        "相关": {"type": "boolean"},
        "类型": {"type": "string",
                 "enum": ["诊断", "用药", "化验", "检查", "手术", "护理",
                          "宣教", "知情同意", "其他", "无关"]},
        "理由": {"type": "string"},
        "条目": {"type": "array", "items": {
            "type": "object",
            "properties": {"字段": {"type": "string"},
                           "值": {"type": "string"},
                           "原文": {"type": "string"}},
            "required": ["字段", "值", "原文"],
            "additionalProperties": False}},
    },
    "required": ["相关", "类型", "条目"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- 读
def load_dir(d: Path) -> tuple[dict, dict, dict, dict, list]:
    def rd(name, default):
        p = d / name
        if not p.exists():
            return default
        return json.loads(p.read_text(encoding="utf-8"))

    compiled = rd("compiled.json", {})
    sections = rd("sections.json", {}).get("sections") or {}
    report = rd("compile_report.json", {})
    # compile.py 写的是 {"tables": [...]}，不是裸列表
    tables = rd("tables.json", [])
    if isinstance(tables, dict):
        tables = tables.get("tables") or []
    # ★ compile.py 新增：按章节渲染好的送模文本（存在就优先用它）
    sec_text = rd("sections_text.json", {})
    if isinstance(sec_text, dict):
        sec_text = sec_text.get("sections") or []
    if not isinstance(sec_text, list):
        sec_text = []
    return compiled, sections, report, tables, sec_text


def render_table(t: dict) -> str:
    """表格对象 -> 网格文本（表头 + 数据行 + 异常项提示）。"""
    out: list[str] = []
    hdr = t.get("header") or []
    if hdr:
        out.append(" | ".join(str(c) for c in hdr))
    skip = t.get("header_rows") or 0
    for r in (t.get("rows") or [])[skip:]:
        out.append(" | ".join(str(c) for c in r))
    ab = t.get("abnormal") or []
    if ab:
        out.append("异常项：" + "; ".join(str(x) for x in ab[:20]))
    return "\n".join(out)


def build_chunks(sections: dict, compiled: dict, tables: list,
                 max_chars: int, whole_file: bool) -> list[dict]:
    idx = {b.get("block_id"): b for b in (compiled.get("pages") or [])}
    tmap = {t.get("index"): t for t in (tables or [])}

    def block_text(bid) -> tuple[str, str]:
        b = idx.get(bid) or {}
        label = b.get("label") or ""
        if b.get("table_index") is not None and b["table_index"] in tmap:
            return "表格", render_table(tmap[b["table_index"]])
        return label, (b.get("content") or "")

    chunks: list[dict] = []
    for name, ids in sections.items():
        buf: list[str] = []
        chars = 0
        part = 1

        def flush():
            nonlocal buf, chars, part
            if buf:
                chunks.append({"id": "%s#%d" % (name, part), "section": name,
                               "blocks": [], "text": "\n\n".join(buf), "chars": chars})
                part += 1
                buf, chars = [], 0

        for bid in ids:
            label, text = block_text(bid)
            text = (text or "").strip()
            if not text:
                continue
            piece = ("【%s b%s】\n%s" % (label, bid, text)) if label else text
            if chars and chars + len(piece) > max_chars:
                flush()
            buf.append(piece)
            chars += len(piece) + 2
        flush()

    if whole_file and len(chunks) > 1:
        merged = {"id": "整篇#1", "section": "整篇", "blocks": [],
                  "text": "\n\n".join(c["text"] for c in chunks), "chars": 0}
        merged["chars"] = len(merged["text"])
        chunks = [merged]
    return chunks


def _split_long(text: str, limit: int) -> list[str]:
    """把超长文本切成 <= limit 的片段：优先按行，单行仍超限就硬切。

    存在的理由：sections_text.json 里一个「片段」= 一个块（表格/段落）。
    如果某个块本身就比 max_chars 长（一整张大表就是这样），不切的话它会独占
    一个超长 chunk，输出 token 随内容线性上涨，最后被 max_tokens 截断。
    """
    if limit <= 0 or len(text) <= limit:
        return [text]
    out: list[str] = []
    buf: list[str] = []
    n = 0
    for ln in text.split("\n"):
        if n and n + len(ln) + 1 > limit:
            out.append("\n".join(buf))
            buf, n = [], 0
        buf.append(ln)
        n += len(ln) + 1
    if buf:
        out.append("\n".join(buf))
    res: list[str] = []
    for p in out:
        while len(p) > limit:
            res.append(p[:limit])
            p = p[limit:]
        res.append(p)
    return [p for p in res if p.strip()] or [text[:limit]]


def chunks_from_sections_text(sec_text: list, max_chars: int,
                              whole_file: bool) -> list[dict]:
    """★ 优先路径：直接用 F2 渲染好的章节文本切块。

    与 build_chunks（旧路径）的差别不是风格，是三处已确认的缺陷的修正：

      1) 不再用 block_id 做跨页全局查表。block_id 由 to_pages.py 用 enumerate
         生成，是**页内下标**；--merge 合并多页时从不重新编号，所以同一个
         block_id 在 40 页里出现 40 次。旧路径的
             idx = {b["block_id"]: b for b in compiled["pages"]}
         只会留下最后一页的那一块 —— 任何一页的章节文本都会被渲染成
         **最后一页同号块的内容**，同一 section 内还会因此重复。
      2) 缺失 / 占位 / 截断标记会真的出现在文本里。旧路径里这些块在
         compiled.json 的 content 是空串，被 `if not text: continue` 丢掉，
         于是系统提示词承诺的「缺失注释」永远到不了模型。
      3) 表格给的是完整矩阵。旧路径走 tables.json，而 table_index 同样是
         **页内序号**（compile.py 里 len(table_objs)+1），跨页同样会错配。

    除这三点外，送模文本与旧路径同形：同样的 【label p{页}-b{块}】 前缀、
    同样以空行分隔，另外在块首补一行章节名（旧路径里章节名只存在于 chunk id，
    模型看不到自己在读哪一章）。
    """
    chunks: list[dict] = []
    for s in sec_text:
        name = s.get("name") or "_其他"
        body = (s.get("text") or "").strip()
        if not body:
            continue
        head = "【章节：%s】" % name
        buf: list[str] = []
        chars = 0
        part = 1
        for piece in body.split("\n\n"):
            piece = piece.strip()
            if not piece:
                continue
            # ★ 单个片段本身超过 max_chars 时（例如一整张大表就是一个块），
            #   必须先按行切开。否则它会独占一个超长 chunk，输出 token 随内容
            #   线性上涨，必然撞 max_tokens 被截断（实测 5000 字符的块输出就
            #   超过 4096 token，finish_reason=length）。
            for sub in _split_long(piece, max_chars):
                step = len(sub) + 2
                if chars and chars + step > max_chars:
                    chunks.append({"id": "%s#%d" % (name, part), "section": name,
                                   "blocks": [], "chars": chars + len(head),
                                   "text": head + "\n\n" + "\n\n".join(buf)})
                    part += 1
                    buf, chars = [], 0
                buf.append(sub)
                chars += step
        if buf:
            chunks.append({"id": "%s#%d" % (name, part), "section": name,
                           "blocks": [], "chars": chars + len(head),
                           "text": head + "\n\n" + "\n\n".join(buf)})

    if whole_file and len(chunks) > 1:
        merged = {"id": "整篇#1", "section": "整篇", "blocks": [],
                  "text": "\n\n".join(c["text"] for c in chunks), "chars": 0}
        merged["chars"] = len(merged["text"])
        chunks = [merged]
    return chunks


# ---------------------------------------------------------------- 调
class ParseError(ValueError):
    """模型返回了内容，但不是合法 JSON。

    带上原文（raw）与 finish_reason —— 没有这两样，事后只能看到一句
    JSONDecodeError，既不知道模型到底写了什么，也不知道是不是被 max_tokens
    截断的（finish_reason == "length" 就是截断）。
    """

    def __init__(self, msg: str, raw: str, finish: str = ""):
        super().__init__(msg)
        self.raw = raw or ""
        self.finish = finish or ""


def extract_json(text: str) -> dict:
    t = (text or "").strip()
    low0 = t.lower()
    for m in ['</think>', '</thinking>', '<|end_of_thought|>', '</thought>']:
        i = low0.rfind(m.lower())
        if i >= 0:
            t = t[i + len(m):].strip()
            low0 = t.lower()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    for pat in (r"\{[^{}]*\}", r"\{.*\}"):
        mo = re.search(pat, t, re.S)
        if mo:
            try:
                return json.loads(mo.group(0))
            except Exception:
                continue
    raise ValueError("响应里找不到 JSON 对象")


def call_llm(url: str, model: str, system: str, user: str, schema: dict | None,
             max_tokens: int, timeout: float, temperature: float) -> tuple[dict, float, dict]:
    body = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    if schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "rec", "schema": schema}}
    # body["chat_template_kwargs"]={"enable_thinking":False}
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions",
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError("HTTP %s：%s" % (e.code, raw[:300])) from None
    dt = time.time() - t0
    content = (((data.get("choices") or [{}])[0].get("message") or {})
               .get("content") or "")
    finish = ((data.get("choices") or [{}])[0].get("finish_reason") or "")
    try:
        return extract_json(content), dt, data.get("usage") or {}
    except Exception as e:                                      # noqa: BLE001
        # ★ 解析失败必须把原文带出来。否则 structured.json 里只有一句
        #   「Expecting ',' delimiter: line 29 column 6」，事后完全无法诊断：
        #   到底是模型吐了非法 JSON、还是被 max_tokens 截断、还是语法没被约束住。
        raise ParseError(str(e), content, finish) from None


# ---------------------------------------------------------------- 主
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="F3：编译产物 -> 小模型 -> structured.json")
    ap.add_argument("compiled_dir", help="compile.py 的输出目录")
    ap.add_argument("--url", default="http://127.0.0.1:8101", help="OpenAI 兼容端点")
    ap.add_argument("--model", default="qwen-small")
    ap.add_argument("--out", default=None, help="默认 <compiled_dir>/structured.json")
    ap.add_argument("--schema", default=None, help="JSON Schema 文件（默认用内置占位 schema）")
    ap.add_argument("--system", default=None, help="系统提示词：文件路径或直接给文本")
    ap.add_argument("--max-chars", type=int, default=5000, help="单个 chunk 的字符上限")
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--whole-file", action="store_true", help="整篇一次送（需自己确认不超上下文）")
    ap.add_argument("--no-json-schema", action="store_true", help="不带 response_format")
    ap.add_argument("--resume", action="store_true", help="跳过 structured.json 里已完成的 chunk")
    ap.add_argument("--force", action="store_true", help="无视质量告警闸门")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划与首个请求体，不联网")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--jobs", type=int, default=1,
                    help="并发发块的线程数（1=串行）")
    a = ap.parse_args(argv)

    d = Path(a.compiled_dir)
    if not d.is_dir():
        print("[x] 不是目录：%s" % d, file=sys.stderr)
        return 2
    compiled, sections, report, tables, sec_text = load_dir(d)
    if not sections:
        print("[x] 读不到 sections.json（或它为空）—— 确认这是 compile.py 的输出目录", file=sys.stderr)
        return 2

    out_path = Path(a.out) if a.out else d / "structured.json"

    # ---- 质量闸门
    alerts = report.get("alerts") or []
    blocked = [x for x in alerts if "F3" in str(x)]
    if blocked and not a.force:
        print("[x] F2 的质量告警要求先修识别，不要进 F3：", file=sys.stderr)
        for x in blocked:
            print("      %s" % x, file=sys.stderr)
        print("      确认要硬跑就加 --force。", file=sys.stderr)
        return 3
    if blocked and a.force and not a.quiet:
        print("[!] 忽略质量告警（--force）：%s" % "; ".join(str(x) for x in blocked))

    # ---- 数据源必须先定：提示词/schema 的默认值要和它对得上（否则模型拿到的输入
    #      和被告知的格式是两套东西 —— 这正是「模型怎么知道该用哪份数据」的答案）
    if sec_text:
        chunks = chunks_from_sections_text(sec_text, a.max_chars, a.whole_file)
        text_src = "sections_text.json（F2 渲染好的章节文本）"
        def_sys, def_sch = DEFAULT_SYSTEM_TEXT, DEFAULT_SCHEMA_TEXT
        def_tag = "内置默认（sections_text 版）"
    else:
        chunks = build_chunks(sections, compiled, tables, a.max_chars, a.whole_file)
        text_src = "[!] build_chunks 旧路径（没有 sections_text.json）"
        def_sys, def_sch = DEFAULT_SYSTEM_LEGACY, DEFAULT_SCHEMA_LEGACY
        def_tag = "内置默认（旧路径版）"
        print("[!] 本目录没有 sections_text.json，正在走 build_chunks 旧路径。", file=sys.stderr)
        print("    该路径有两个已知缺陷：block_id 是页内下标、跨页会错配；"
              "缺失/占位/截断标记到不了模型。", file=sys.stderr)
        print("    补一条命令即可修好（F2 零模型，秒级）：", file=sys.stderr)
        print("      %s ~/medrec/src/compile.py <pages.json> --out %s" % (sys.executable, d),
              file=sys.stderr)

    # ---- 提示词与 schema：命令行 > 同目录的 f3_system.txt/f3_schema.json > 内置默认
    #      注意两个文件描述的都是 **sections_text 契约**，所以只在 sections_text
    #      路径上才拿它们当默认；旧路径下用它们同样是错配。
    use_file = bool(sec_text)
    if a.system:
        sp = Path(a.system)
        if sp.exists():
            system, system_src = sp.read_text(encoding="utf-8"), str(sp)
        else:
            system, system_src = a.system, "（命令行直接给的文本）"
    elif use_file and DEFAULT_SYSTEM_FILE.exists():
        system = DEFAULT_SYSTEM_FILE.read_text(encoding="utf-8")
        system_src = str(DEFAULT_SYSTEM_FILE)
    else:
        system, system_src = def_sys, def_tag

    if a.schema:
        schema = json.loads(Path(a.schema).read_text(encoding="utf-8"))
        schema_src = str(a.schema)
    elif a.no_json_schema:
        schema, schema_src = None, "（--no-json-schema，未使用）"
    elif use_file and DEFAULT_SCHEMA_FILE.exists():
        schema = json.loads(DEFAULT_SCHEMA_FILE.read_text(encoding="utf-8"))
        schema_src = str(DEFAULT_SCHEMA_FILE)
    else:
        schema, schema_src = def_sch, def_tag
        if not use_file and DEFAULT_SCHEMA_FILE.exists():
            print("[!] 忽略了同目录的 f3_schema.json / f3_system.txt —— 它们描述的是 "
                  "sections_text 契约，与旧路径的数据不符。", file=sys.stderr)

    total_chars = sum(c["chars"] for c in chunks)

    if not a.quiet:
        print("编译目录 : %s" % d)
        print("文本来源 : %s" % text_src)
        print("来源     : %s（%d 页 / %d 块）"
              % (compiled.get("source"), report.get("pages"),
                 report.get("blocks_total")))
        print("章节     : %d 个 -> %d 个 chunk（共 %d 字符）"
              % (len(sections), len(chunks), total_chars))
        print("端点     : %s   模型：%s" % (a.url, a.model))
        print("提示词   : %s（%d 字）" % (system_src, len(system)))
        print("schema   : %s" % schema_src)
        print("输出     : %s" % out_path)

    if a.dry_run:
        print("\n--dry-run：各 chunk 的规模")
        for c in chunks:
            print("   %-18s %6d 字符  首行: %s"
                  % (c["id"], c["chars"], c["text"].splitlines()[0][:50]))
        if chunks:
            demo = {"model": a.model, "temperature": a.temperature,
                    "max_tokens": a.max_tokens,
                    "messages": [{"role": "system", "content": system[:200] + "…"},
                                 {"role": "user", "content": chunks[0]["text"][:400] + "…"}]}
            if schema is not None:
                demo["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "rec", "schema": schema}}
            print("\n首个请求体（截断预览）：")
            print(json.dumps(demo, ensure_ascii=False, indent=2)[:1600])
        print("\n（dry-run 结束，未发任何请求）")
        return 0

    # ---- 续跑
    state = {"compiled_dir": str(d), "source": compiled.get("source"),
             "url": a.url, "model": a.model, "schema_src": schema_src,
             "gate": {"alerts": alerts, "forced": bool(blocked and a.force)},
             "chunks": [], "merged": {"条目": [], "丢掉": []}, "stats": {}}
    done = set()
    if a.resume and out_path.exists():
        try:
            old = json.loads(out_path.read_text(encoding="utf-8"))
            state["chunks"] = old.get("chunks") or []
            state["merged"] = old.get("merged") or state["merged"]
            done = {c["id"] for c in state["chunks"] if c.get("ok")}
            if not a.quiet:
                print("续跑：已完成 %d 个 chunk，跳过" % len(done))
        except Exception as e:                                  # noqa: BLE001
            print("[!] 读旧 structured.json 失败（%s），从头开始" % e, file=sys.stderr)

    def save():
        payload = dict(state)
        ents, seen = [], set()
        for c in state["chunks"]:
            r = c.get("result") or {}
            if r.get("相关") is False:
                state["merged"]["丢掉"].append({"chunk": c["id"], "section": c["section"],
                                                "理由": r.get("理由") or ""})
                continue
            for e in (r.get("条目") or []):
                # ★ 去重键与 schema 无关：以前写死 (字段,值,原文)，换 schema 就失效。
                #   改成整条记录的规范化 JSON —— 任何字段表都能正确去重。
                key = json.dumps(e, ensure_ascii=False, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                ent = dict(e)
                ent["_来自"] = c["id"]
                ents.append(ent)
        payload["merged"] = {"条目": ents, "丢掉": state["merged"]["丢掉"]}
        payload["stats"] = {
            "sections": len(sections), "chunks": len(state["chunks"]),
            "ok": sum(1 for c in state["chunks"] if c.get("ok")),
            "failed": sum(1 for c in state["chunks"] if not c.get("ok")),
            "条目": len(ents), "丢掉": len(payload["merged"]["丢掉"]),
            "seconds": round(time.time() - t_start, 1),
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8", newline="\n")

    t_start = time.time()
    from concurrent.futures import ThreadPoolExecutor as _TPE
    import threading as _th
    done_now = {c["id"] for c in state["chunks"] if c.get("ok")}
    todo = [c for c in chunks if c["id"] not in done_now]
    jobs = max(1, min(int(getattr(a, "jobs", 1) or 1), len(todo) or 1))
    if not a.quiet:
        print("  并发 %d 线程，待发 %d 块" % (jobs, len(todo)), file=sys.stderr)
    state_lock = _th.Lock()
    by_id = {c["id"]: c for c in state["chunks"]}
    def run_one(c):
        item = {"id": c["id"], "section": c["section"], "chars": c["chars"],
                "ok": False, "seconds": 0.0, "result": None, "error": None}
        t0 = time.time()
        try:
            res, dt, usage = call_llm(a.url, a.model, system, c["text"], schema,
                                      a.max_tokens, a.timeout, a.temperature)
            item.update(ok=True, seconds=round(dt, 2), result=res)
            if usage:
                item["usage"] = usage
            if not a.quiet:
                print("  [%s] %.1fs 相关=%s 条目=%d" % (c["id"], dt, res.get("相关"), len(res.get("条目") or [])))
        except Exception as e:
            item["seconds"] = round(time.time() - t0, 2)
            item["error"] = "%s: %s" % (type(e).__name__, e)
            if isinstance(e, ParseError):
                item["finish_reason"] = e.finish
                item["raw"] = e.raw[:4000]
            print("  [%s] 失败：%s" % (c["id"], item["error"]), file=sys.stderr)
        return item
    def commit(item):
        with state_lock:
            by_id[item["id"]] = item
            state["chunks"] = [by_id[c["id"]] for c in chunks if c["id"] in by_id]
            save()
    if todo:
        if jobs > 1:
            with _TPE(max_workers=jobs) as ex:
                for it in ex.map(run_one, todo):
                    commit(it)
        else:
            for c in todo:
                commit(run_one(c))
    if not any(c.get("ok") for c in state["chunks"]):
        print("[x] 没有任何 chunk 成功 —— 先确认端点与模型名：curl -s %s/v1/models"
              % a.url.rstrip("/"), file=sys.stderr)
        n_raw = [c for c in state["chunks"] if c.get("raw")]
        if n_raw:
            f0 = n_raw[0]
            print("     已保存 %d 块的模型原文（见诊断文件的 chunks[].raw）" % len(n_raw),
                  file=sys.stderr)
            print("     例如 %s：finish_reason=%s  原文开头：%s"
                  % (f0["id"], f0.get("finish_reason") or "?",
                     (f0["raw"][:100].replace("\n", "⏎"))), file=sys.stderr)
            if f0.get("finish_reason") == "length":
                print("     finish_reason=length → 是被 max_tokens 截断，不是模型乱写；"
                      "调大 --max-tokens 或调小 --max-chars。", file=sys.stderr)
        # 批处理是「structured.json 存在就跳过」，所以这个名字不能留。
        # 但**也不能直接删** —— 那等于把唯一的失败证据销毁。
        # 改名成 structured_failed.json：跳过规则不受影响，证据留得住。
        diag = out_path.with_name("structured_failed.json")
        try:
            if out_path.exists():
                out_path.replace(diag)
            print("     失败诊断已留：%s" % diag, file=sys.stderr)
        except OSError:
            pass
        return 1

    save()
    st = json.loads(out_path.read_text(encoding="utf-8"))["stats"]
    if not a.quiet:
        print("\n== 完成：%d 个 chunk（成功 %d / 失败 %d），条目 %d，丢弃 %d 段，用时 %.1fs"
              % (st["chunks"], st["ok"], st["failed"], st["条目"], st["丢掉"], st["seconds"]))
        print("   -> %s" % out_path)
    return 0 if st["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
