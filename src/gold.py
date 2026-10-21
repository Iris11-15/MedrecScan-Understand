#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
F6 金标准工具。

导出复核表（13 例 × 13 题，人工只填 gold_level 一列）:
    python3 gold.py export <final.json 目录> <sections 目录> <review.csv>
评分 + margin 阈值扫描:
    python3 gold.py score <填好的 review.csv> [--sweep]

gold_level 留空 = 该题不判（比如原文根本没这项检查）。
"""
import csv
import json
import sys
from pathlib import Path

COLS = ["case", "item", "section_len", "section_text", "pipeline_level", "source",
        "laya_margin", "evidence", "quote", "gold_level", "gold_note"]


def load_items():
    p = Path(__file__).resolve().parent / "laya_forms.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    return [it["key"] for it in d["scales"]["nihss_15"]["items"]]


def export(final_dir, sect_dir, out_csv):
    items = load_items()
    rows = []
    for jf in sorted(Path(final_dir).glob("*.final.json")):
        case = jf.name.replace(".final.json", "")
        d = json.loads(jf.read_text(encoding="utf-8"))
        st = Path(sect_dir) / (case + ".txt")
        text = st.read_text(encoding="utf-8") if st.exists() else ""
        for k in items:
            r = d["items"].get(k) or {}
            m = r.get("laya_margin")
            rows.append({
                "case": case, "item": k, "section_len": len(text),
                "section_text": text.replace("\n", " "),
                "pipeline_level": r.get("final_level", ""),
                "source": r.get("source", ""),
                "laya_margin": "" if m is None else m,
                "evidence": "/".join((r.get("evidence") or {}).get("ab") or []),
                "quote": (r.get("quote") or "").replace("\n", " "),
                "gold_level": "", "gold_note": "",
            })
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"导出 {len(rows)} 行（{len({r['case'] for r in rows})} 例 × {len(items)} 题）-> {out_csv}")
    print("人工只填 gold_level 一列（0~4）；留空 = 该题不判。")


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def score(path, sweep=False):
    rows = [r for r in read_rows(path) if (r.get("gold_level") or "").strip() != ""]
    if not rows:
        print("review.csv 里 gold_level 一列还是空的 —— 先人工填完再跑 score。")
        return
    n = len(rows)
    pl = lambda r: int(r["pipeline_level"])
    gl = lambda r: int(r["gold_level"])
    exact = sum(1 for r in rows if pl(r) == gl(r))
    near = sum(1 for r in rows if abs(pl(r) - gl(r)) <= 1)
    tp = sum(1 for r in rows if gl(r) > 0 and pl(r) > 0)
    fn = sum(1 for r in rows if gl(r) > 0 and pl(r) == 0)
    fp = sum(1 for r in rows if gl(r) == 0 and pl(r) > 0)
    tn = sum(1 for r in rows if gl(r) == 0 and pl(r) == 0)
    print(f"已标注 {n} 题")
    print(f"  完全一致 {exact}/{n} = {exact/n:.1%}     ±1 以内 {near}/{n} = {near/n:.1%}")
    print(f"  0/非0 二分类: TP={tp}  FN={fn}  FP={fp}  TN={tn}")
    if tp + fn:
        print(f"    召回 = {tp/(tp+fn):.1%}   （漏报 {fn} 项 —— NIHSS 里这是最危险的错误）")
    if tp + fp:
        print(f"    精确 = {tp/(tp+fp):.1%}   （误报 {fp} 项）")
    if sweep:
        print("\n  margin 阈值扫描（只对有 margin 的题；margin 为空的按采纳计）")
        for m in (0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50):
            acc = [r for r in rows if (r.get("laya_margin") or "").strip() == ""
                   or float(r["laya_margin"]) >= m]
            if not acc:
                continue
            e = sum(1 for r in acc if pl(r) == gl(r))
            esc = n - len(acc)
            print(f"    ≥{m:.2f}: 采纳 {len(acc):>3} 题，一致 {e:>3} = {e/len(acc):>5.1%}   "
                  f"（升级 {esc} 题）")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    if sys.argv[1] == "export":
        export(sys.argv[2], sys.argv[3], sys.argv[4])
    elif sys.argv[1] == "score":
        score(sys.argv[2], "--sweep" in sys.argv)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
