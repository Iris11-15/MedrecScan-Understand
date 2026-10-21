#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F4：把 structured.json 导成多 sheet 的 .xlsx（零依赖，只用标准库）。

输入（每个患者一个 compile.py 输出目录）
    structured.json       F3 产物：merged.条目 / merged.丢掉
    compile_report.json   缺失清单（missing_blocks）
    sections_text.json    块引用清单（有则用来校验「出处」是否真实存在）

输出
    <输出目录>/<住院号>.xlsx          单个患者
    <输出目录>/汇总.xlsx              整个批次（每张表多两列：住院号 / 姓名）

sheet 划分
    一般情况 / 诊断 / 化验 / 检查 / 用药 / 手术 / 护理 / 其他
        —— 由条目的「类别」字段决定，空表不输出
    缺失   —— compile_report.json 里没识别到的块（对应 F2.3「缺失必须可见」）
    丢弃   —— 被判为无关、未入库的章节及理由（来自 merged.丢掉）

文件命名
    住院号取自目录名开头的数字串（例如 `202424089790叶文达` -> `202424089790`），
    符合 encoding-policy：文件名一律 ASCII，中文只放内容。

为什么不用 openpyxl
    这个项目里 compile.py / structure.py / to_pages.py 全是零依赖。xlsx 本身就是一个
    zip 加几个 XML 部件，用标准库 zipfile 写出来即可；用 inlineStr 连 sharedStrings
    都省了。少一个安装环节就少一处会在服务器上卡住的地方。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

# 每张表的列（类别由 sheet 名承载，所以表内不再重复这一列）
COLS = ["项目", "值", "出处", "出处校验", "来源"]
SHEET_ORDER = ["一般情况", "诊断", "化验", "检查", "用药", "手术", "护理", "其他"]
MAX_SHEET_LEN = 31
_BAD_SHEET_CHARS = set(r':\/?*[]')


# ---------------------------------------------------------------- 读
def load_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                            # noqa: BLE001
        return default


def split_stem(stem: str) -> tuple[str, str]:
    """`202424089790叶文达` -> (`202424089790`, `叶文达`)；没有数字前缀就退回净化后的原名。"""
    m = re.match(r"^\s*(\d{4,})\s*(.*)$", stem)
    if m:
        return m.group(1), m.group(2).strip()
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", stem).strip("_") or "patient"
    return safe, stem


def ref_set(sections_text: dict | None) -> set:
    """sections_text.json 里真实存在的块引用集合（用来验证模型的「出处」）。"""
    out = set()
    for s in ((sections_text or {}).get("sections") or []):
        for r in (s.get("refs") or []):
            out.add(str(r))
    return out


def check_ref(ref: str, refs: set) -> str:
    """出处是否指向真实存在的块。表格行引用形如 p12-b4#白细胞，按 # 前判断。"""
    r = (ref or "").strip()
    if not r:
        return "空"
    if not refs:
        return "—"                       # 没有 sections_text.json，无从校验
    return "✓" if r.split("#")[0] in refs else "✗ 不存在"


def collect(d: Path) -> dict | None:
    """一个患者的目录 -> sheet 数据。"""
    st = load_json(d / "structured.json")
    if not isinstance(st, dict):
        return None
    rep = load_json(d / "compile_report.json") or {}
    refs = ref_set(load_json(d / "sections_text.json"))
    hid, pname = split_stem(d.name)

    merged = st.get("merged") or {}
    by_cat: dict[str, list] = {}
    n_old = 0
    for e in (merged.get("条目") or []):
        if "类别" not in e:
            n_old += 1                     # 旧 schema 产的条目，没有类别，只能落进「其他」
        cat = str(e.get("类别") or e.get("类型") or "其他").strip() or "其他"
        if cat not in SHEET_ORDER:
            cat = "其他"
        src = str(e.get("_来自") or "")
        ref = str(e.get("出处") or e.get("原文") or "")
        by_cat.setdefault(cat, []).append([
            str(e.get("项目") or e.get("字段") or ""),
            str(e.get("值") or ""),
            ref,
            check_ref(ref, refs),
            src,
        ])

    sheets: list[tuple[str, list[str], list[list[str]]]] = []
    for cat in SHEET_ORDER:
        rows = by_cat.get(cat) or []
        if not rows:
            continue
        sheets.append((cat, COLS, rows))

    miss = rep.get("missing_blocks") or []
    if miss:
        sheets.append(("缺失", ["页", "块", "标签", "原因", "类型"],
                       [[str(m.get("page")), str(m.get("block_id")),
                         str(m.get("label")), str(m.get("reason")),
                         str(m.get("kind"))] for m in miss]))

    drop = merged.get("丢掉") or []
    if drop:
        sheets.append(("丢弃", ["章节", "chunk", "理由"],
                       [[str(x.get("section")), str(x.get("chunk")),
                         str(x.get("理由"))] for x in drop]))

    return {"住院号": hid, "姓名": pname, "dir": d, "sheets": sheets,
            "n_old": n_old, "stats": st.get("stats") or {}}


# ---------------------------------------------------------------- XML
def esc(s: str) -> str:
    s = str(s)
    s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
          .replace('"', "&quot;"))
    return "".join(c for c in s if c >= " " or c == "\t")


def col_name(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA"""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def sheet_name(name: str, used: set) -> str:
    n = "".join("_" if c in _BAD_SHEET_CHARS else c for c in str(name))[:MAX_SHEET_LEN]
    n = n.strip("'") or "Sheet"
    base, k = n, 2
    while n in used:
        suf = "(%d)" % k
        n = base[:MAX_SHEET_LEN - len(suf)] + suf
        k += 1
    used.add(n)
    return n


_SHEET_HEAD = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
               '<worksheet xmlns="http://schemas.openxmlformats.org/'
               'spreadsheetml/2006/main">')
_STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def sheet_xml(header: list[str], rows: list[list[str]], widths: list[int]) -> str:
    out = [_SHEET_HEAD]
    ncol = max([len(header)] + [len(r) for r in rows]) if (header or rows) else 1
    out.append('<dimension ref="A1:%s%d"/>' % (col_name(ncol - 1), len(rows) + 1))
    out.append('<sheetViews><sheetView workbookViewId="0">'
               '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
               'state="frozen"/></sheetView></sheetViews>')
    out.append("<cols>")
    for i, w in enumerate(widths[:ncol]):
        out.append('<col min="%d" max="%d" width="%d" customWidth="1"/>' % (i + 1, i + 1, w))
    out.append("</cols><sheetData>")

    def row_xml(rno: int, cells: list[str], style: int = 0) -> str:
        cs = []
        for i, v in enumerate(cells):
            if v == "" and style == 0:
                continue
            s = ' s="%d"' % style if style else ""
            cs.append('<c r="%s%d"%s t="inlineStr"><is><t xml:space="preserve">%s'
                      "</t></is></c>" % (col_name(i), rno, s, esc(v)))
        return '<row r="%d">%s</row>' % (rno, "".join(cs))

    out.append(row_xml(1, header, style=1))
    for i, r in enumerate(rows, 2):
        out.append(row_xml(i, r))
    out.append("</sheetData></worksheet>")
    return "".join(out)


def write_xlsx(path: Path, sheets: list[tuple[str, list[str], list[list[str]]]]) -> None:
    """sheets: [(表名, 表头, 数据行)]。空列表会写成一个占位表，避免 Excel 打不开。"""
    used: set = set()
    named = [(sheet_name(n, used), h, r) for n, h, r in sheets]
    if not named:
        named = [("空", ["说明"], [["没有任何条目"]])]

    parts: list[tuple[str, str]] = []
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
          "<sheets>"]
    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']

    for i, (name, header, rows) in enumerate(named, 1):
        part = "xl/worksheets/sheet%d.xml" % i
        ct.append('<Override PartName="/%s" ContentType="application/vnd.openxmlformats-'
                  'officedocument.spreadsheetml.worksheet+xml"/>' % part)
        wb.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (esc(name), i, i))
        rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>'
                    % (i, i))
        # 列宽：按表头文字长度粗估，出处列固定宽一点
        widths = []
        for j, h in enumerate(header):
            if h == "出处" or h == "出处校验":
                widths.append(18 if h == "出处" else 10)
            elif h == "值":
                widths.append(38)
            else:
                widths.append(max(10, min(30, len(h) * 2 + 6)))
        parts.append((part, sheet_xml(header, rows, widths)))

    wb.append("</sheets></workbook>")
    n = len(named)
    rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/styles" Target="styles.xml"/>' % (n + 1))
    rels.append("</Relationships>")

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "".join(ct) + "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'relationships"><Relationship Id="rId1" Type="http://schemas.'
                   'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                   'Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", "".join(wb))
        z.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
        z.writestr("xl/styles.xml", _STYLES)
        for part, xml in parts:
            z.writestr(part, xml)


# ---------------------------------------------------------------- 主
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="F4：structured.json -> 多 sheet xlsx（零依赖）")
    ap.add_argument("paths", nargs="+", help="compile.py 的输出目录，或含 compiled/*/ 的批次目录")
    ap.add_argument("--out", default=None, help="输出目录（默认写在批次目录的 xlsx/ 下）")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    targets: list[Path] = []
    for raw in a.paths:
        p = Path(raw)
        if not p.is_dir():
            print("[x] 不是目录：%s" % p, file=sys.stderr)
            return 2
        subs = sorted([x for x in (p / "compiled").glob("*") if x.is_dir()]) \
            if (p / "compiled").is_dir() else []
        targets += subs if subs else [p]
    if not targets:
        print("[x] 没找到任何 compile 输出目录", file=sys.stderr)
        return 2

    batch = len(targets) > 1 or (targets and (targets[0].parent.name == "compiled"))
    root = Path(a.out) if a.out else (targets[0].parent.parent if batch else targets[0])
    out_dir = root if (a.out or not batch) else (root / "xlsx")

    pats, ok, skipped = [], 0, 0
    for d in targets:
        got = collect(d)
        if got is None:
            print("  [skip] %s：没有 structured.json" % d.name, file=sys.stderr)
            skipped += 1
            continue
        pats.append(got)

    for g in pats:
        f = out_dir / ("%s.xlsx" % g["住院号"])
        write_xlsx(f, g["sheets"])
        ok += 1
        if not a.quiet:
            print("  %-14s %-8s -> %s" % (g["住院号"], g["姓名"] or "-", f.name))

    if len(pats) > 1:
        agg: dict[str, list] = {}
        for g in pats:
            for name, header, rows in g["sheets"]:
                if name in ("缺失", "丢弃"):
                    continue
                for r in rows:
                    agg.setdefault(name, []).append([g["住院号"], g["姓名"]] + r)
        sheets = [(n, ["住院号", "姓名"] + COLS, agg[n]) for n in SHEET_ORDER if agg.get(n)]
        f = out_dir / "汇总.xlsx"
        write_xlsx(f, sheets)
        if not a.quiet:
            print("  %-14s %-8s -> %s（%d 个患者）"
                  % ("汇总", "-", f.name, len(pats)))

    if not a.quiet:
        nref = sum(1 for g in pats for nm, h, rs in g["sheets"]
                   if nm not in ("缺失", "丢弃") for r in rs if r[3].startswith("✗"))
        print("\n== 写出 %d 个 xlsx（跳过 %d）到 %s" % (ok, skipped, out_dir))
        nold = sum(g["n_old"] for g in pats)
        if nold:
            print("   [!] 有 %d 条条目没有「类别」字段 —— 它们是**旧 schema** 产的，"
                  "只能落进「其他」表，且「出处」多半校验不过。" % nold)
            print("       请用新 schema 重跑 F3："
                  "structure.py <目录> --schema ~/medrec/src/f3_schema.json "
                  "--system ~/medrec/src/f3_system.txt")
        if nref:
            print("   [!] 有 %d 条「出处」在 sections_text.json 里找不到 —— "
                  "模型编造了块引用，看「出处校验」列" % nref)
        else:
            print("   出处校验：全部命中（或没有 sections_text.json 可对）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
