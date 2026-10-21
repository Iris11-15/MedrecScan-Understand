# -*- coding: utf-8 -*-
"""表格解析（F2.2）：HTML -> 展开合并单元格的纯矩阵。

背景：``stack.py:html_table_to_markdown`` 只做对了一半 —— 检测到 rowspan/colspan
时保留 HTML（判断正确），但 HTML 与 Markdown 混排，模型仍要解析标签，token 昂贵。

本模块只做一件事：把 ``<table>`` 变成**纯矩阵**（``rows[r][c]`` 全是字符串），
rowspan/colspan 完全展开；再做表头合并、单元格归一化、表格类型识别。
输出写进 ``tables.json``，``compiled.md`` 里只留一行锚点。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

CELL_TAGS = {"td", "th"}
TYPE_RULES = (
    ("化验单", ("白细胞", "血红蛋白", "血小板", "中性粒", "淋巴", "红细胞",
                "检验", "参考值", "项目")),
    ("用药单", ("用法", "剂量", "频次", "口服", "静脉", "注射液", "医嘱", "用药")),
    ("体征表", ("体温", "脉搏", "呼吸", "血压", "心率")),
    ("费用表", ("金额", "费用", "单价", "合计")),
)
# 无信息量的列名（异常项落到这些列时，改用"该行首个非空单元格"作为异常项名）
GENERIC_HEADERS = {"", "提示", "结果", "数值", "值", "检查结果", "项目",
                   "单位", "参考值", "参考范围", "正常值"}
UP_TOKENS = {"↑", "↑↑", "偏高", "高"}
DOWN_TOKENS = {"↓", "↓↓", "偏低", "低"}
# 单位后缀（无括号时的识别范围）
UNIT_SUFFIX = re.compile(
    r"\s+(\d+[\^\d]*/[A-Za-z]+|[a-zA-Z%]+/[A-Za-z]+|mmHg|%|℃|次/分)\s*$")

# ──────────────────────────────────────────────────────────────────────
# PaddleOCR-VL 的**表格 token 流**（OCR-html 格式）
#
#   <fcel>  单元格（有内容）
#   <ecel>  空单元格
#   <lcel>  与左邻合并（colspan 的延续）
#   <ucel>  与上邻合并（rowspan 的延续）
#   <xcel>  同时与左、上合并
#   <nl>    换行（一行结束）
#
# ★ 这段文本**不带 `<table>` 外壳**，直接以 `<fcel>` 开头。
#   原来的 looks_like_table() 只认 `<table>`，于是 compile.py 第 156 行
#   把它整段包成 `<table><tr><td>…</td></tr></table>` —— 解析出来 1 行 1 列，
#   矩阵全丢，只剩一坨没有任何分隔符的文本（实测 31/31 张表都是这个样子，
#   化验 49 列 / 用药 104 列填不满，主要就是卡在这里）。
TABLE_TOKEN = re.compile(r"<(fcel|ecel|lcel|ucel|xcel|nl)>", re.I)
# 合并续格：lcel 只表示「跟左边那格是同一格」，本身没有新内容
_LEFT_MERGE = {"lcel"}
_UP_MERGE = {"ucel"}
_BOTH_MERGE = {"xcel"}


class _TableParser(HTMLParser):
    """收集 <tr>/<td>/<th>，保留 rowspan/colspan 原始值。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self._row: list[dict] | None = None
        self._cell: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in CELL_TAGS:
            self._cell = {"text": "", "rowspan": 1, "colspan": 1,
                          "header": tag == "th"}
            a = dict(attrs)
            for k in ("rowspan", "colspan"):
                try:
                    self._cell[k] = max(1, int(str(a.get(k, "1")).strip() or 1))
                except (TypeError, ValueError):
                    pass
        elif tag == "br" and self._cell is not None:
            self._cell["text"] += " "

    def handle_endtag(self, tag):
        if tag in CELL_TAGS and self._cell is not None:
            if self._row is None:
                self._row = []
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).replace("\u00a0", " ").strip()


def expand_html_table(html: str) -> dict:
    """``<table>`` -> {rows: [[str]], n_rows, n_cols, header_rows}。

    完全展开：合并单元格覆盖到的每一格都填上同一份文本，矩阵保证是矩形，
    下游按 (行, 列) 直接取用，不需要再理解合并语义。

    落格方式：逐行、逐格地放在"该行第一个未被占用的列"，再把文本写满
    rowspan × colspan 范围（只在目标格为空时写，避免覆盖先写入的 rowspan 内容）。
    """
    p = _TableParser()
    p.feed(html)
    grid: list[list[str | None]] = []

    def ensure(r: int, c: int) -> None:
        while len(grid) <= r:
            grid.append([])
        while len(grid[r]) <= c:
            grid[r].append(None)

    for r, raw_row in enumerate(p.rows):
        c = 0
        for cell in raw_row:
            while True:
                ensure(r, c)
                if grid[r][c] is None:
                    break
                c += 1
            text = _clean(cell["text"])
            for dr in range(cell["rowspan"]):
                for dc in range(cell["colspan"]):
                    ensure(r + dr, c + dc)
                    if grid[r + dr][c + dc] is None:
                        grid[r + dr][c + dc] = text
            c += cell["colspan"]

    n_cols = max((len(r) for r in grid), default=0)
    rows = [[("" if v is None else str(v)) for v in r] for r in grid]
    for r in rows:
        r.extend([""] * (n_cols - len(r)))

    header_rows = 0                     # 开头连续由 <th> 构成的行都算表头
    for raw_row in p.rows:
        if raw_row and all(c["header"] for c in raw_row):
            header_rows += 1
        else:
            break
    return {"rows": rows, "n_rows": len(rows), "n_cols": n_cols,
            "header_rows": header_rows}


_PARTIAL_TAG = re.compile(r"<[^<>]*$")


def _clean_cell(s) -> str:
    """单元格文本归一化：剥掉**未闭合的残缺标记**再压空白。

    VL 的输出可能是被截断的（`…<lcel><l`），最后一个标记没有 `>`；
    不剥掉就会把 `<l` 当成内容写进格子。
    """
    return _clean(_PARTIAL_TAG.sub("", str(s or "")))


# ★ 多行表头：纸质表单的表头常是 2~4 行（护理记录单 = 日期/时间/… ；
#   心律/SPO2/… ；瞳孔 子列 大小/对光反射 ；左右/左/右）。
#   硬编码 1 会让后面几行表头混进数据行 -> header/cells 全错位。
MAX_HEADER_ROWS = 6
_DATA_HINT = re.compile(
    r"\d{4}\s*[-/年]\s*\d{1,2}"           # 日期
    r"|\d{1,2}\s*[:：]\s*\d{2}")           # 时间


def detect_header_rows(rows: list[list[str]],
                       max_hdr: int = MAX_HEADER_ROWS) -> int:
    """表头行数 = 第一行「带日期/时间格」的行号（上限 max_hdr）。

    找不到日期/时间 -> 沿用旧行为（1 行），不做任何猜测。
    """
    for i, r in enumerate(rows):
        if any(_DATA_HINT.search(str(c)) for c in r):
            if 1 <= i <= max_hdr:
                return i
            return 1 if len(rows) > 1 else 0
    return 1 if len(rows) > 1 else 0


def parse_tokens(s: str) -> dict:
    """表格 token 流 -> {rows, n_rows, n_cols, header_rows}。

    与 expand_html_table 同语义、同目标：输出保证是矩形矩阵，
    下游按 (行, 列) 直接取，不需要再理解合并语义。

    落格方式：
      <fcel>/<ecel>  → 自己占一格（ecel 内容为空）
      <ucel>/<xcel>  → 填**上一行同一列**的值（rowspan 实体化）
      <lcel>         → **留空**（colspan：左边那格已经带着值了，
                       重复一遍只会把像「住院病案首页」那种整行跨列的页眉
                       复制 N 份，纯烧 token）
    """
    rows: list[list[str]] = []
    row: list[str] = []
    parts = TABLE_TOKEN.split(s)
    lead = _clean_cell(parts[0])
    it = iter(parts[1:])
    for tok, txt in zip(it, it):
        tok = tok.strip("<>").lower()
        if tok == "nl":
            if row:
                rows.append(row)
            row = []
            continue
        val = _clean_cell(txt)
        if tok in _UP_MERGE or tok in _BOTH_MERGE:
            up = rows[-1][len(row)] if rows and len(rows[-1]) > len(row) else ""
            val = up
        elif tok in _LEFT_MERGE:
            val = ""
        row.append(val)
    if row:
        rows.append(row)

    # ★ 不要另外处理「末尾残留文本」：re.split 带捕获组时
    #   parts = [首个标记前的文本, 标记, 文本, 标记, 文本, …, 末标记, 末文本]，
    #   最后那段文本**已经**被上面的 zip 当成最后一个格子的内容消费掉了。
    #   （之前多写了一次 tail 追加，截断样本里就出现 `- -` 这种重复。）
    if lead and rows:
        rows[0][0] = _clean(lead + " " + rows[0][0])

    n_cols = max((len(r) for r in rows), default=0)
    for r in rows:
        r.extend([""] * (n_cols - len(r)))

    # ★ 丢掉**整行全空**的行。
    #   纸质表单的空白格子 VL 也会照抄出来：实测叶文达 p16 那张「长期医嘱单」
    #   只有 114 字符真内容，却带出 247 行、1729 个格子 —— 渲染成网格要 4800 字符，
    #   99% 是 ` |  |  | ` 这种占位。全空行不携带任何信息，删掉不影响任何一列。
    kept = [r for r in rows if any(str(c).strip() for c in r)]
    if kept:
        rows = kept
    return {"rows": rows, "n_rows": len(rows), "n_cols": n_cols,
            "header_rows": detect_header_rows(rows)}


def merge_header(rows: list[list[str]], header_rows: int) -> list[str]:
    """多级表头合并：顶部 header_rows 行按列纵向拼成一个字段名。"""
    if not rows:
        return []
    if header_rows <= 1:
        return list(rows[0])
    cols = max(len(r) for r in rows[:header_rows])
    out = []
    for c in range(cols):
        parts: list[str] = []
        for r in range(min(header_rows, len(rows))):
            v = rows[r][c] if c < len(rows[r]) else ""
            v = _clean(v)
            if v and v not in parts:
                parts.append(v)
        out.append(" / ".join(parts))
    return out


def normalize_cell(s) -> dict:
    """单元格归一化：拆出 value / unit / flag。"""
    raw = _clean(s)
    flag = None
    m = re.search(r"([↑↓]+|偏高|偏低)\s*$", raw)
    if m:
        tok = m.group(1)
        flag = "high" if (tok in UP_TOKENS or tok.startswith("↑")) else "low"
        raw = _clean(raw[: m.start()])
    unit = None
    mb = re.search(r"\(([^()]{1,16})\)\s*$", raw)      # 括号式：7.2 (10^9/L)
    if mb:
        unit = _clean(mb.group(1))
        raw = _clean(raw[: mb.start()])
    else:                                                 # 后缀式：7.2 10^9/L
        ms = UNIT_SUFFIX.search(raw)
        if ms:
            unit = _clean(ms.group(1))
            raw = _clean(raw[: ms.start()])
    try:
        value: float | str = float(raw)
    except (TypeError, ValueError):
        value = raw
    return {"value": value, "unit": unit, "flag": flag, "raw": _clean(s)}


def detect_type(rows: list[list[str]]) -> str:
    """按关键词识别表格类型。"""
    text = " ".join(_clean(c) for r in rows[:3] for c in r)
    for name, kws in TYPE_RULES:
        if any(k in text for k in kws):
            return name
    return "未知"


def extract_abnormal(header: list[str], cells: list[list[dict]]) -> list[str]:
    """异常项：优先取"带 ↑/↓ 的列名"，列名无信息量时改用该行首个非空单元格。"""
    abnormal: list[str] = []

    def push(v) -> None:
        v = _clean(v)
        if v and v not in abnormal:
            abnormal.append(v)

    for c in range(len(header)):
        for r in cells:
            if c < len(r) and r[c]["flag"]:
                head = header[c] if c < len(header) else ""
                generic = (not head) or (head in GENERIC_HEADERS) or (" / " in head)
                if not generic:
                    push(head)
                elif r and r[0]["value"] not in (None, ""):
                    push(r[0]["value"])
    if not abnormal:                     # 表头缺失时，回退到带标志位单元格本身
        for r in cells:
            for cell in r:
                if cell["flag"]:
                    push(cell["value"])
    return abnormal


def parse_table(html: str, block_id=None, page: int | None = None,
                table_index: int = 1) -> dict:
    """完整解析：展开合并单元格 + 表头合并 + 类型识别 + 异常项提取。"""
    # PaddleOCR-VL 的表格 token 流（<fcel>…<nl>）不是 HTML，走另一条解析
    ex = parse_tokens(html) if _is_token_table(html) else expand_html_table(html)
    rows = ex["rows"]
    header = merge_header(rows, ex["header_rows"])
    body = rows[ex["header_rows"]:] if ex["header_rows"] else rows
    while body and not any(_clean(c) for c in body[0]):   # 剥掉表头溢出造成的空壳行
        body = body[1:]
    cells = [[normalize_cell(c) for c in r] for r in body]
    return {
        "block_id": block_id,
        "page": page,
        "index": table_index,
        "type": detect_type(rows),
        "header": header,
        "header_rows": ex["header_rows"],
        "rows": rows,
        "cells": cells,
        "n_rows": ex["n_rows"],
        "n_cols": ex["n_cols"],
        "body_rows": max(0, ex["n_rows"] - ex["header_rows"]),
        "abnormal": extract_abnormal(header, cells),
        "html_chars": len(html),
    }


def anchor(table: dict) -> str:
    """compiled.md 里的单行锚点（模型只看这一行，矩阵在 tables.json）。"""
    tid = (f"p{table['page']}-t{table['index']}" if table.get("page")
           else f"t{table['index']}")
    parts = [f"表 {tid}：{table['type']}，{table['body_rows']} 行"]
    if table.get("abnormal"):
        parts.append("异常项：" + "、".join(str(x) for x in table["abnormal"][:6]))
    return "<!-- " + "，".join(parts) + " -->"


def _is_token_table(s: str) -> bool:
    """是不是 PaddleOCR-VL 的表格 token 流（`<fcel>` + `<nl>`）。"""
    t = str(s or "").lower()
    return "<fcel>" in t and "<nl>" in t


def looks_like_table(s: str) -> bool:
    t = _clean(s).lower()
    if t.startswith("<table") or ("<table" in t and t.startswith("<html")):
        return True
    # ★ 表格 token 流也要认 —— 不认的话 compile.py 会把整段包成一个 <td>，
    #   31/31 张表就是这么塌成 1×1 的。
    return _is_token_table(t)
