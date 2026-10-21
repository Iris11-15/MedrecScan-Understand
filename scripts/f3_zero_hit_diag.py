#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零命中列诊断：**映射错 还是 上游没产出** —— 一条命令分清，不用翻 PDF。

★ 为什么需要它（2026-10-03，零值根因分析的残留问题）：
    56 列整片为 0，只有两种可能，**修法相反**：
      A. 字段映射错 —— 词在文书里，就是没对上列名 → 改映射表
      B. 上游没产出 —— OCR/编译根本没出这个词 → 查那批页有没有进编译
    翻 PDF 人工判是下策；compiled.md 就在批量目录里，grep 一下就有答案。

判定表（对每个零命中列，统计关键词在多少例的产物里出现过）：

    | compiled.md 里 | tables.json 里 | 判定 |
    |---|---|---|
    | 无 | 无 | **上游没产出**（OCR/编译没出这个词） |
    | 有 | 无 | **表解析/表绑定问题**（词进了正文，没进表格矩阵） |
    | 有 | 有 | **映射/匹配规则错**（表里也有词，列还是阴性 → 查 filter/terms） |

★ 关键词从 `variables_f3.json` 的 extract 规约里取（terms / filter 的值 /
  正则里的字面量），再补上列名本身去单位括号 —— **不猜**，配置里有什么用什么。

用法：
    E=/home/ubuntu/anaconda3/envs/vl-env/bin/python
    $E f3_zero_hit_diag.py --batch ~/medrec/jobs/batch_1002_1936 \\
        --structured ~/medrec/jobs/batch_1002_1936/f3out
    # 只看指定的几列：
    $E f3_zero_hit_diag.py --batch ... --cols 头颅CT检查 空腹血糖（mg/dL）
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 正则模式里的 regex 元字符 —— 出现这些说明那段不是字面量，别拿去 grep
_REGEX_NOISE = set("[](){}?*+|^$.\\")


def _literal_runs(pattern: str) -> list[str]:
    """从正则里抠字面量：连续的中文/字母数字段（≥2 字），跳过含元字符的段。"""
    runs = []
    for seg in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", str(pattern or "")):
        if any(ch in _REGEX_NOISE for ch in seg):
            continue
        # 纯数字段（如 4,20 这种量词残留）没有检索价值
        if seg.isdigit():
            continue
        runs.append(seg)
    return runs


def keywords_of(col: str, spec: dict) -> list[str]:
    """列名 + 抽取规约 → 检索关键词（去重，保序）。

    ★ 刻意**偏多不偏少**：多一个关键词只会把「上游没产出」误判成「映射错」，
     而漏一个会把「映射错」误判成「上游没产出」—— 后者贵得多（会带错修法）。
    """
    ex = spec.get("extract") or {}
    kws: list[str] = []

    def add(x):
        x = str(x or "").strip()
        if len(x) >= 2 and x not in kws:
            kws.append(x)

    for t in (ex.get("terms") or []):
        add(t)
    f = ex.get("filter")
    if isinstance(f, dict):
        for v in f.values():
            add(v)
    elif isinstance(f, list):
        for v in f:
            add(v)
    if ex.get("kind") == "regex":
        for r in _literal_runs(ex.get("pattern")):
            add(r)
    for t in (ex.get("fallback_terms") or []):
        add(t)
    # 列名本身：去掉尾部单位括号（空腹血糖（mg/dL）→ 空腹血糖）
    base = re.sub(r"[（(][^（）()]*[）)]\s*$", "", col).strip()
    add(base)
    add(col)
    return kws


def load_specs(path) -> dict:
    """variables_f3.json → {列名: 条目}。顶层是 sheet 名 → 条目列表。"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for v in d.values():
        if isinstance(v, list):
            for e in v:
                if isinstance(e, dict) and e.get("col"):
                    out[e["col"]] = e
    return out


def zero_hit_cols(structured_dir, specs) -> list[str]:
    """从 f3 的输出目录里找「阳性 = 0」的列。"""
    cnt: collections.Counter = collections.Counter()
    for p in sorted(Path(structured_dir).glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            continue
        for c, r in (d.get("items") or {}).items():
            if (r or {}).get("state") == "阳性":
                cnt[c] += 1
    return [c for c in specs if cnt.get(c, 0) == 0]


def scan_batch(batch: Path, cases: list[str]) -> dict:
    """→ {病例名: {"text": 全文, "tables": 全部表格文本}}。缺文件记 None，不炸。"""
    out = {}
    for name in cases:
        cdir = batch / "compiled" / name
        md = cdir / "compiled.md"
        tj = cdir / "tables.json"
        text = md.read_text(encoding="utf-8", errors="replace") if md.is_file() else None
        tabs = tj.read_text(encoding="utf-8", errors="replace") if tj.is_file() else ""
        out[name] = {"text": text, "tables": tabs}
    return out


def main():
    ap = argparse.ArgumentParser(description="零命中列：映射错 还是 上游没产出")
    ap.add_argument("--batch", required=True,
                    help="批量目录（下面要有 compiled/<病例>/compiled.md）")
    ap.add_argument("--structured", default=None,
                    help="f3 输出目录；给了就自动找「阳性=0」的列")
    ap.add_argument("--variables", default=str(HERE / "variables_f3.json"))
    ap.add_argument("--cols", nargs="*", default=[],
                    help="只查这几列（不给就靠 --structured 自动找）")
    ap.add_argument("--limit", type=int, default=40, help="最多列几列")
    a = ap.parse_args()

    specs = load_specs(a.variables)
    print(f"配置读入 {len(specs)} 列（{a.variables}）")

    cols = list(a.cols)
    if not cols:
        if not a.structured:
            ap.error("要么给 --cols，要么给 --structured（自动找零命中列）")
        cols = zero_hit_cols(a.structured, specs)
        print(f"从 {a.structured} 找到零命中列 {len(cols)} 个")
    cols = cols[: a.limit]

    batch = Path(a.batch)
    case_dirs = sorted(p.name for p in (batch / "compiled").iterdir() if p.is_dir())
    if not case_dirs:
        print(f"[x] {batch}/compiled 下没有病例目录")
        sys.exit(2)
    print(f"扫描 {len(case_dirs)} 例（{batch}/compiled）\n")

    corpus = scan_batch(batch, case_dirs)
    n = len(case_dirs)
    rows = []
    for c in cols:
        kws = keywords_of(c, specs.get(c) or {})
        if not kws:
            rows.append([c, 0, 0, 0, "**没有关键词可查**（规约里 terms/filter 全空）"])
            continue
        t_hit = sum(1 for v in corpus.values()
                    if v["text"] and any(k in v["text"] for k in kws))
        tab_hit = sum(1 for v in corpus.values()
                      if v["tables"] and any(k in v["tables"] for k in kws))
        if t_hit == 0:
            verdict = "**上游没产出** —— OCR/编译没出这个词，查那批页"
        elif tab_hit == 0:
            verdict = ("**表解析/表绑定问题** —— 词进了正文没进表格矩阵，"
                       "查 tables.json 的表名与 filter.table 是否一致")
        else:
            verdict = ("**映射/匹配规则错** —— 表里也有词列还是阴性，"
                       "查 terms/filter 的写法")
        rows.append([c, len(kws), t_hit, tab_hit, verdict])

    w = max(len(r[0]) for r in rows) + 2
    print(f"{'列名':<{w}}{'词数':>4} {'正文命中':>8} {'表格命中':>8}  判定")
    for c, nk, t, tab, v in rows:
        print(f"{c:<{w}}{nk:>4} {t:>5}/{n} {tab:>5}/{n}   {v}")

    # 汇总：给一个"先修哪个"的排序依据
    by = collections.Counter(r[4].split(" —")[0].strip("*") for r in rows)
    print("\n汇总：")
    for k, v in by.most_common():
        print(f"  {v:>3} 列  {k}")
    print("\n★ 判定只是**线索**不是结论：正文命中是拿关键词 grep 的，"
          "词形变化（CT 平扫 vs 头颅CT）会漏。要定案，挑 1~2 例对着原文看。")


if __name__ == "__main__":
    main()
