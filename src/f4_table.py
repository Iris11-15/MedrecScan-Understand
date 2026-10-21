#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F5：长表 -> 宽表。把 structured.json 的条目归并进 495 个目标列。

输入
    <患者目录>/structured.json       F3 产物（长表：类别/项目/值/出处/…）
    <患者目录>/sections_text.json    可选，用来校验「出处」
    <患者目录>/compile_report.json   可选，产出「缺失」表
    f4_columns.json                  495 个目标列的分组模板（f4_columns.xlsx 编译来的）
    f4_alias.json                    项目名 -> 目标列名 的别名表

输出
    <out>/<住院号>.xlsx   一患者一份：16 张宽表（目标列 × 1 行）+ 主诉/待归并/缺失/丢弃
    <out>/汇总.xlsx       整批：同样的表，行 = 患者，多「住院号/姓名」两列

归并规则（用户已确认）
    同一列有多个候选时：先排「页眉页脚级重复」（同一值来自 ≥3 个不同页 = 页眉），
    剩下的按**页号升序**（入院记录在最前）取第一条，同级取更长的值（更完整）。

为什么要归并而不能不归并
    每页页眉都带患者信息，长表会把「姓名/性别/年龄/住院号」原样记 8 遍；
    宽表只能有一格，所以必须在这里做取舍，而不是让模型别重复输出（它看到的输入就重复）。
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from to_xlsx import write_xlsx, split_stem, load_json, ref_set  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_COLUMNS = HERE / "f4_columns.json"
DEFAULT_ALIAS = HERE / "f4_alias.json"

# 「是否 / 既往 / 出院是否」类列：值是三态。带这些词的列不是布尔（时间/次数/程度）
_NOT_BOOL = re.compile(r"时间|日期|次数|年|月|程度|部位|名称|类型|原因|备注|说明|数量")
_BOOL_HEAD = re.compile(r"^(是否|出院是否|既往)")

# 这些列天然是多值的（同一列可以有多个不同值），归并时拼接而不是取一条。
MULTI_COL = re.compile(r"其它诊断|其他诊断|其它药物|其他药物|出院带药|出院其他药物|"
                       r"手术及操作名称|既往其他病史|给予.*药物名称")
MULTI_SEP = "；"


BLOCK_RE = re.compile(r"^【[^】]*?(p\d+-b\d+)】\s*(.*)$", re.S)
NEG_WORDS = ("否认","无","未见","未发现","阴性","不考虑","排除","不伴","未及","未引出","正常","未提及","未行","未做","未查","未予")
HISTORY_PREFIX = ("既往",)


def blocks_of(sec_text):
    out = {}
    for s in ((sec_text or {}).get("sections") or []):
        for part in (s.get("text") or "").split("\n\n"):
            m = BLOCK_RE.match(part.strip())
            if m:
                out[m.group(1)] = m.group(2)
    return out


def needs_neg_evidence(col):
    return any(str(col or "").startswith(x) for x in HISTORY_PREFIX)


BOOL_ALIASES = {
    "高血压": ["高血压"], "糖尿病": ["糖尿病"], "其他心脏病": ["心脏病", "心血管"],
    "冠心病": ["冠心病", "冠状动脉"], "房颤": ["房颤", "心房颤动"], "心肌梗死": ["心肌梗死", "心梗"],
    "缺血性脑卒中": ["脑梗死", "脑梗", "缺血性卒中", "脑卒中"], "脑出血": ["脑出血"],
    "肾功能不全": ["肾病", "肾功能"], "肝病": ["肝病", "肝功"], "肿瘤": ["肿瘤", "癌"],
    "贫血": ["贫血"], "血小板增多": ["血小板增多", "血小板"], "消化道溃疡": ["溃疡"],
    "偏头痛": ["偏头痛", "头痛"], "甲状腺功能亢进": ["甲状腺", "甲亢"], "静脉血栓": ["血栓"],
    "肺": ["肺及支气管", "肺部", "支气管"], "传染病": ["肝炎", "结核", "疟疾"],
    "手术": ["手术史"], "外伤": ["外伤史"], "输血": ["输血史"], "过敏": ["过敏史"],
}


def neg_grounded(val: str, src: str, col: str = "") -> bool:
    v = str(val).strip()
    if v == "未提及":
        return True
    if v not in ("否", "无", "阴性"):
        return True
    s = str(src or "")
    if not s:
        return False
    kw = re.sub(r"^(既往|出院是否合并|出院是否有|出院是否|出院时有否|是否|有无)", "", str(col or "")).strip()
    kws = [kw]
    if kw in BOOL_ALIASES:
        kws = list(BOOL_ALIASES[kw])
    segs = [x for x in re.split("[。；;\n]", s) if x.strip()] or [s]
    for seg in segs:
        if not any(w in seg for w in NEG_WORDS):
            continue
        if not kw:
            return True
        for k in kws:
            if k and k in seg:
                return True
            for m in re.finditer("|".join(map(re.escape, NEG_WORDS)), seg):
                for m2 in re.finditer(re.escape(k), s):
                    if abs(m.start() - m2.start()) <= 30:
                        return True
    return False


def norm(s: str) -> str:
    s = str(s or "")
    for a, b in (("（", "("), ("）", ")"), ("，", ","), ("：", ":"), ("／", "/")):
        s = s.replace(a, b)
    return re.sub(r"[\s\.。·]+", "", s)


def core(s: str) -> str:
    s = re.sub(r"\([^)]*\)", "", norm(s))
    return re.sub(r"(检查时间|检查结果|检查|结果|数值|计数|测定|定量)$", "", s).strip()


def page_of(ref: str) -> int:
    m = re.match(r"p(\d+)", str(ref or ""))
    return int(m.group(1)) if m else 9999


def is_bool_col(col: str) -> bool:
    return bool(_BOOL_HEAD.match(col)) and not _NOT_BOOL.search(col)


def to_bool(v: str) -> str:
    t = re.sub(r"[\s。.]", "", str(v or ""))
    if not t:
        return ""
    if re.match(r"^(是|有|阳性|存在|已|阳性结果|有.*史)$", t):
        return "是"
    if re.match(r"^(否|无|否认|阴性|未见|未|没有|不存在)", t):
        return "否"
    if t in ("是", "否"):
        return t
    return t                      # 说不清的保留原文，不硬编


# ---------------------------------------------------------------- 列解析
class Cols:
    def __init__(self, template: dict, alias: dict):
        self.sheets = template["sheets"]
        self.all = [c for s in self.sheets for c in s["columns"]]
        # flat = 保持 数据条目.xlsx 的原始列顺序（重名列保留首次出现的位置）
        self.flat = template.get("flat") or self.all
        self.sheet_of = {c: s["name"] for s in self.sheets for c in s["columns"]}
        self.by_norm = {}
        self.by_core: dict[str, list[str]] = collections.defaultdict(list)
        for c in self.all:
            self.by_norm.setdefault(norm(c), c)
            self.by_core[core(c)].append(c)
        self.alias = {}
        self.bad_alias: list[tuple[str, str]] = []
        for k, v in (alias.get("映射") or {}).items():
            # ★ 只在**真实存在的列**上生效。映射到不存在的列名，条目会落进
            #   一个谁都不会输出的 key —— 既不在 Sheet1 也不在任何分组表里，
            #   却仍被算作「已填」。宁可让它进「待归并」被看见。
            if v in self.sheet_of:
                self.alias[k] = v
            else:
                self.bad_alias.append((k, str(v)))
        self.bp = alias.get("血压拆分") or {}
        self.bp_src = [norm(x) for x in (self.bp.get("来源项目") or [])]
        self.bp_re = re.compile(self.bp.get("规则") or r"(?!x)x")
        self.bp_dst = [c for c in (self.bp.get("目标列") or []) if c in self.sheet_of]

        # ---- 第三层：difflib 模糊匹配（零依赖）
        # 阈值可被 f4_alias.json 的「相似度」段覆盖。
        #   ≥ hi  → 自动映射，直接填进大表
        #   ≥ lo  → **只进「待确认」表，不填** —— 假映射（填错列）比留空更糟，
        #           人工确认后往别名表加一行，下一批就自动填
        #   < lo  → 待归并（判定「目标表里没有这一列」）
        fz = alias.get("相似度") or {}
        self.fz_hi = float(fz.get("自动映射", 0.82))
        self.fz_lo = float(fz.get("送人工", 0.62))
        self.fz_off = bool(fz.get("关闭", False))
        self._fz_pool = [(c, core(c), norm(c)) for c in self.all]
        self._fz_cache: dict[str, tuple[list[str], float, str]] = {}

    def fuzzy(self, item: str) -> tuple[list[str], float, str]:
        """返回 (目标列 or [], 相似度, 该列名)。用于「待确认」与工单。

        两段式：先用快的 quick_ratio 粗筛出前 8 个候选，再对它们算精确
        ratio。495 列逐个算精确 ratio 在 Python 层太慢（每条几百微秒），
        粗筛之后是微秒级。
        """
        if self.fz_off or not item:
            return [], 0.0, ""
        key = item
        hit = self._fz_cache.get(key)
        if hit is not None:
            return hit
        n = norm(item)

        # ① 前缀/包含匹配 —— 比字符相似度准得多，因为真实映射是**前缀式**的
        #    （`白细胞` 是 `白细胞计数` 的前缀、`体温` 与 `体温(℃)` 的核心名相等）。
        #    ★ 多命中 = 歧义，**绝不猜**：目标表里有成对的「同名不同单位」列
        #      （`肌酐` / `肌酐Cr`、`空腹血糖(mg/dL)` / `空腹血糖(mmol/L)`），
        #      猜错单位比留空严重得多。歧义时把候选列出来交给人工。
        pre = [col for col, c, cn in self._fz_pool
               if c and len(c) >= 2 and (c.startswith(n) or n.startswith(c))]
        if len(pre) == 1:
            out = ([pre[0]], 1.0, pre[0])
            self._fz_cache[key] = out
            return out
        if len(pre) > 1:
            out = ([], 0.99, " | ".join(pre[:4]))
            self._fz_cache[key] = out
            return out

        # ② difflib 字符相似度（兜底）
        q = difflib.SequenceMatcher()
        q.set_seq2(n)
        coarse = []
        for col, c, cn in self._fz_pool:
            # ★ 粗筛必须用**核心名**（c，已剥掉单位括号）算上界。
            #   只拿完整列名（cn）算会把短词全筛掉 —— difflib 的 ratio 是
            #   2M/(len(a)+len(b))，短词对长列名有结构性惩罚：
            #   「体温」(2) 对「体温(℃)」(6) 的理论上限只有 0.5，永远过不了阈值。
            #   real_quick_ratio 是 ratio 的合法上界，所以用它筛不会漏。
            q.set_seq1(c)
            r1 = q.real_quick_ratio()
            q.set_seq1(cn)
            r2 = q.real_quick_ratio()
            r = r1 if r1 > r2 else r2
            if r >= self.fz_lo:
                coarse.append((r, col, c, cn))
        coarse.sort(reverse=True)
        best, score, bestcol = None, 0.0, ""
        for _, col, c, cn in coarse[:8]:
            s = max(difflib.SequenceMatcher(None, n, c).ratio(),
                    difflib.SequenceMatcher(None, n, cn).ratio())
            if s > score:
                best, score, bestcol = col, s, col
        out = ([best] if (best and score >= self.fz_hi) else [], score, bestcol)
        self._fz_cache[key] = out
        return out

    def resolve(self, item: str):
        """项目名 -> 目标列名。返回 list（血压会拆成两个）。

        核心名匹配只在**唯一命中**时才接受 —— 因为去掉单位后同名是常态：
        `空腹血糖（mg/dL）` 与 `空腹血糖（mmol/L）` 的核心名都是「空腹血糖」，
        取第一个会把 mmol/L 的值填进 mg/dL 那一格。歧义时宁可落到「待归并」，
        让人补一条别名，也不要静默填错单位。
        """
        n = norm(item)
        if n in self.bp_src and len(self.bp_dst) == 2:
            return list(self.bp_dst)
        if item in self.alias:
            return [self.alias[item]]
        if n in self.by_norm:
            return [self.by_norm[n]]
        if item in self.sheet_of:
            return [item]
        hits = self.by_core.get(core(item))
        if hits and len(hits) == 1:
            return [hits[0]]
        # 第三层：difflib 模糊匹配（只有 ≥ fz_hi 才自动映射）
        fz, _, _ = self.fuzzy(item)
        return fz                    # 空 = 歧义/不认识 -> 待归并


# ---------------------------------------------------------------- 归并
def pick(cands: list[dict]) -> dict:
    """按「重复页眉排最后 -> 页号升序 -> 值更长」取第一条。"""
    if len(cands) == 1:
        return cands[0]
    pages_per_value: dict[str, set] = collections.defaultdict(set)
    for c in cands:
        pages_per_value[str(c["值"])].add(c["page"])
    header_vals = {v for v, ps in pages_per_value.items() if len(ps) >= 3}
    def key(c):
        return (1 if str(c["值"]) in header_vals else 0, c["page"],
                -len(str(c["值"])))
    return sorted(cands, key=key)[0]


def collect(d: Path, cols: Cols) -> dict | None:
    st = load_json(d / "structured.json")
    if not isinstance(st, dict):
        return None
    rep = load_json(d / "compile_report.json") or {}
    refs = ref_set(load_json(d / "sections_text.json"))
    blockidx = blocks_of(load_json(d / "sections_text.json"))
    hid, pname = split_stem(d.name)

    wide: dict[str, list[dict]] = collections.defaultdict(list)
    zhuisu, pend, mid, extra = [], [], [], collections.Counter()

    for e in ((st.get("merged") or {}).get("条目") or []):
        item = str(e.get("项目") or e.get("字段") or "").strip()
        val = str(e.get("值") or "").strip()
        ref = str(e.get("出处") or e.get("原文") or "").strip()
        if not item:
            continue
        targets = cols.resolve(item)
        if not targets:
            # 模糊匹配到「不算高分、但也不算毫无关系」的中间档 → 只进「待确认」，不填表。
            # 假映射（填错列）比留空更糟；人工确认后往别名表加一行，下一批自动填。
            fz, score, bestcol = cols.fuzzy(item)
            if not fz and score >= cols.fz_lo and bestcol:
                mid.append([item, "%.2f" % score, bestcol, val, ref,
                            "✓" if ref.split("#")[0] in refs else ("—" if not refs else "✗")])
                extra[item] += 1
            else:
                pend.append([str(e.get("类别") or ""), item, val, ref,
                             "✓" if ref.split("#")[0] in refs else ("—" if not refs else "✗")])
                extra[item] += 1
            continue
        # 血压这类：一个来源值拆到多个目标列
        if len(targets) == 2 and item and norm(item) in cols.bp_src:
            m = cols.bp_re.match(val)
            if m:
                for tgt, v in zip(targets, m.groups()):
                    wide[tgt].append({"值": v, "page": page_of(ref), "出处": ref,
                                      "来自": e.get("_来自")})
                continue
        tgt = targets[0]
        rec = {"值": val, "page": page_of(ref), "出处": ref, "来自": e.get("_来自")}
        for k in ("单位", "参考范围", "异常", "剂量", "频次", "途径", "时间", "所见", "结论"):
            if e.get(k):
                rec[k] = e[k]
        if needs_neg_evidence(tgt) and not neg_grounded(val, blockidx.get(ref.split(chr(35))[0], chr(34)+chr(34)), tgt):
            mid.append([item, chr(45), tgt, val, ref, chr(34)+chr(21542)+chr(20294)+chr(20986)+chr(22788)+chr(26080)+chr(21542)+chr(23450)+chr(35777)+chr(25454)+chr(34)])
            extra[item] += 1
            continue
        wide[tgt].append(rec)
        if str(e.get("类别")) == "主诉":
            zhuisu.append(["主诉", val, ref, "✓" if ref.split("#")[0] in refs else "—"])

    # 每列取一条（多值列拼接）
    row: dict[str, str] = {}
    cells: dict[str, dict] = {}
    for col, cands in wide.items():
        if MULTI_COL.search(col):
            # 多值列：按页号顺序拼接，同值去重 —— 取一条会丢掉其余诊断/药物
            seen_v, vals, first = set(), [], None
            for c in sorted(cands, key=lambda x: x["page"]):
                v = str(c["值"]).strip()
                if v and v not in seen_v:
                    seen_v.add(v)
                    vals.append(v)
                if first is None:
                    first = c
            if not vals:
                continue
            row[col] = MULTI_SEP.join(vals)
            cells[col] = first or cands[0]
            continue
        best = pick(cands)
        v = str(best["值"])
        if is_bool_col(col):
            v = to_bool(v)
        if not v:
            continue
        parts = [v]
        for k, pre in (("单位", ""), ("参考范围", "参考"), ("异常", ""),
                       ("剂量", "剂量"), ("频次", "频次"), ("途径", "途径"),
                       ("时间", "时间"), ("所见", "所见"), ("结论", "结论")):
            if best.get(k):
                parts.append("%s%s" % (pre, best[k]) if pre else str(best[k]))
        row[col] = " ".join(parts)
        cells[col] = best

    miss = [[str(m.get("page")), str(m.get("block_id")), str(m.get("label")),
             str(m.get("reason"))] for m in (rep.get("missing_blocks") or [])]
    drop = [[str(x.get("section")), str(x.get("chunk")), str(x.get("理由"))]
            for x in ((st.get("merged") or {}).get("丢掉") or [])]

    return {"住院号": hid, "姓名": pname, "row": row, "cells": cells,
            "主诉": zhuisu, "待归并": pend, "待确认": mid, "缺失": miss, "丢弃": drop,
            "未映射计数": extra, "stats": st.get("stats") or {}}


# ---------------------------------------------------------------- 写
LONG_COLS = {
    "主诉": (["项目", "值", "出处", "出处校验"], None),
    "待归并": (["类别", "项目", "值", "出处", "出处校验"], None),
    "待确认": (["项目", "相似度", "最像的目标列", "值", "出处", "出处校验"], None),
    "缺失": (["页", "块", "标签", "原因"], None),
    "丢弃": (["章节", "chunk", "理由"], None),
}


def sheets_for(cols: Cols, got: dict, layout: str = "both") -> list:
    """单患者视图，一行。

    layout:
      flat  —— 只出 `Sheet1`：495 列，严格按 数据条目.xlsx 的原始列顺序
      sheets—— 只出 16 张分组宽表
      both  —— 两套都出（默认）：Sheet1 给程序读，分组表给人核对
    """
    out = []
    if layout in ("both", "flat"):
        vals = [str(got["row"].get(c, "")) for c in cols.flat]
        out.append(("Sheet1", cols.flat, [vals]))
    if layout in ("both", "sheets"):
        for s in cols.sheets:
            vals = [str(got["row"].get(c, "")) for c in s["columns"]]
            if not any(vals):
                continue
            out.append((s["name"], s["columns"], [vals]))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="F5：长表 -> 宽表（495 列目标表）")
    ap.add_argument("paths", nargs="+", help="compile 输出目录，或含 compiled/*/ 的批次目录")
    ap.add_argument("--out", default=None, help="输出目录（默认 <批次>/xlsx）")
    ap.add_argument("--columns", default=str(DEFAULT_COLUMNS))
    ap.add_argument("--alias", default=str(DEFAULT_ALIAS))
    ap.add_argument("--layout", choices=["both", "flat", "sheets"], default="both",
                    help="both=Sheet1(照抄模板顺序)+分组表 | flat=只有 Sheet1 | sheets=只有分组表")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    cols = Cols(json.loads(Path(a.columns).read_text(encoding="utf-8")),
                json.loads(Path(a.alias).read_text(encoding="utf-8")))
    if cols.bad_alias:
        print("[!] f4_alias.json 里有 %d 条映射指向了不存在的列，已忽略（这些条目会进「待归并」）："
              % len(cols.bad_alias), file=sys.stderr)
        for k, v in cols.bad_alias[:10]:
            print("      %s -> %s（目标表里没有这一列）" % (k, v), file=sys.stderr)

    targets: list[Path] = []
    for raw in a.paths:
        p = Path(raw)
        if not p.is_dir():
            print("[x] 不是目录：%s" % p, file=sys.stderr)
            return 2
        subs = sorted([x for x in (p / "compiled").glob("*") if x.is_dir()]) \
            if (p / "compiled").is_dir() else []
        targets += subs if subs else [p]

    batch = len(targets) > 1 or (targets and targets[0].parent.name == "compiled")
    root = Path(a.out) if a.out else (targets[0].parent.parent if batch else targets[0])
    out_dir = root if (a.out or not batch) else (root / "xlsx")

    pats, skipped = [], 0
    for d in targets:
        g = collect(d, cols)
        if g is None:
            print("  [skip] %s：没有 structured.json" % d.name, file=sys.stderr)
            skipped += 1
            continue
        pats.append(g)

    for g in pats:
        sheets = sheets_for(cols, g, a.layout)
        for nm, rows in (("主诉", g["主诉"]), ("待归并", g["待归并"]),
                         ("待确认", g["待确认"]), ("缺失", g["缺失"]), ("丢弃", g["丢弃"])):
            if rows:
                sheets.append((nm, LONG_COLS[nm][0], rows))
        f = out_dir / ("%s.xlsx" % g["住院号"])
        write_xlsx(f, sheets)
        if not a.quiet:
            filled = sum(1 for v in g["row"].values() if v)
            print("  %-14s %-8s 填入 %3d 列 / 待归并 %3d / 待确认 %3d -> %s"
                  % (g["住院号"], g["姓名"] or "-", filled,
                     len(g["待归并"]), len(g["待确认"]), f.name))

    if len(pats) > 1:
        pre = ["住院号", "姓名"]
        sheets = []
        if a.layout in ("both", "flat"):
            # 整批的 Sheet1：495 列 + 前置的住院号/姓名（平表必须能认出是哪位患者）
            sheets.append(("Sheet1", pre + cols.flat,
                           [[g["住院号"], g["姓名"]] +
                            [str(g["row"].get(c, "")) for c in cols.flat] for g in pats]))
        if a.layout in ("both", "sheets"):
            agg: dict[str, list] = collections.defaultdict(list)
            for g in pats:
                for s in cols.sheets:
                    row = [str(g["row"].get(c, "")) for c in s["columns"]]
                    if any(row):
                        agg[s["name"]].append([g["住院号"], g["姓名"]] + row)
            sheets += [(s["name"], pre + s["columns"], agg[s["name"]])
                       for s in cols.sheets if agg.get(s["name"])]
        f = out_dir / "汇总.xlsx"
        write_xlsx(f, sheets)
        if not a.quiet:
            print("  %-14s %-8s -> %s（%d 个患者）" % ("汇总", "-", f.name, len(pats)))

    if not a.quiet:
        un = collections.Counter()
        for g in pats:
            un.update(g["未映射计数"])
        print("\n== 写出 %d 份到 %s（跳过 %d）" % (len(pats), out_dir, skipped))
        if un:
            print("   [!] 有 %d 种项目名没对上目标列，共 %d 条 —— 已在各患者的「待归并」表里"
                  % (len(un), sum(un.values())))
            print("       出现最多的：%s" % "、".join("%s(%d)" % (k, v) for k, v in un.most_common(8)))
            print("       要对上就往 f4_alias.json 的「映射」里加：\"项目名\": \"目标列名\"")
        else:
            print("   项目名全部对上了目标列 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
