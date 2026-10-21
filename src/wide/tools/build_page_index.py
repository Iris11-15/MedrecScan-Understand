#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/build_page_index.py —— 页面定位缓存：每例的「首页页码」「首程页码」。

为什么要它：一份病历中位 59 页（实测 106 例），而本任务只需要 首页 + 首程 两三类页。
8100 实测串行 ~17-25 秒/页，全扫一例 ≈20 分钟 —— 不定位就没法批量。

两种来源：
  --from-compiled <dir>   从旧批次 compiled.json 树里读关键词命中页（冷启动加速器，
                          只读旧产物；provenance 会写进 index 的 source 字段）
  --from-stage1 <dir>     从本产线 01_ocr 的产物里读（跑过 --all-pages 之后用这个）

页码一律 **0-based**（与 pypdfium2 一致）。旧 compiled.json 的 "page" 字段是否 0-based
由 --verify 实测确认，不猜。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from wide import common as C  # noqa: E402


def hits(pages: list[dict], keywords: list[str]) -> list[int]:
    got = set()
    for p in pages:
        c = p.get("content") or ""
        if any(k in c for k in keywords):
            pg = p.get("page")
            if isinstance(pg, int):
                got.add(pg)
    return sorted(got)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-compiled", default="", help="旧批次 compiled 目录（含 */compiled.json）")
    ap.add_argument("--from-stage1", default="", help="本产线 01_ocr 目录（含 <case>/page_*.json）")
    ap.add_argument("--out", required=True, help="输出 page_index.json")
    ap.add_argument("--max-frontpage", type=int, default=2)
    ap.add_argument("--max-first-progress", type=int, default=3)
    a = ap.parse_args(argv)

    cfg = C.load_pipeline()
    kw_fp = list(cfg["locator"]["frontpage_keywords"])
    kw_pn = list(cfg["locator"]["first_progress_keywords"])

    index: dict[str, dict] = {}
    src = ""
    if a.from_compiled:
        src = f"compiled:{a.from_compiled}"
        for cj in sorted(Path(a.from_compiled).glob("*/compiled.json")):
            case = C.case_id_from_rel(cj.parent.name)
            pages = C.read_json(cj).get("pages", [])
            fp = hits(pages, kw_fp)
            pn = hits(pages, kw_pn)
            index[case] = {
                "frontpage_pages": fp[:a.max_frontpage],
                "first_progress_pages": pn[:a.max_first_progress],
                "frontpage_all_hits": fp, "first_progress_all_hits": pn,
                "n_pages_seen": len({p.get("page") for p in pages}),
                "locator_source": src,
            }
    elif a.from_stage1:
        src = f"stage1:{a.from_stage1}"
        for case_dir in sorted(p for p in Path(a.from_stage1).iterdir() if p.is_dir()):
            fp, pn = set(), set()
            for pj in sorted(case_dir.glob("page_*.json")):
                rec = C.read_json(pj)
                page0 = rec.get("page")
                blob = " ".join(b.get("block_content") or "" for b in rec.get("blocks", []))
                if any(k in blob for k in kw_fp):
                    fp.add(page0)
                if any(k in blob for k in kw_pn):
                    pn.add(page0)
            index[case_dir.name] = {
                "frontpage_pages": sorted(fp)[:a.max_frontpage],
                "first_progress_pages": sorted(pn)[:a.max_first_progress],
                "frontpage_all_hits": sorted(fp), "first_progress_all_hits": sorted(pn),
                "locator_source": src,
            }
    else:
        C.err("要指定 --from-compiled 或 --from-stage1")
        return 2

    out = Path(a.out)
    C.write_json(out, {"generated": __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "source": src,
                       "frontpage_keywords": kw_fp, "first_progress_keywords": kw_pn,
                       "page_base": 0, "cases": index})
    n_fp = sum(1 for v in index.values() if v["frontpage_pages"])
    n_pn = sum(1 for v in index.values() if v["first_progress_pages"])
    C.log(f"写入 {out}：{len(index)} 例；命中首页 {n_fp} 例、首程 {n_pn} 例")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
