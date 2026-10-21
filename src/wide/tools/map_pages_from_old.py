#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/map_pages_from_old.py —— 用旧批次产物做页面定位（可验证、零 OCR）。

为什么需要它（实测，2026-10-09）：
  1) 页眉条定位（tools/locate_pages.py）对某些院区**结构上失效**：
     安吉__img20231201_11004386 的首页标题「住院病案首页」在页面**底部**
     （旧 OCR 块 bbox y=2029/2350），top20% 页眉条永远抓不到 → 扫 25 页 0 命中。
  2) 旧产线 `scripts/pdf2png.py` 有 `SKIP_BLANK`（`INK_MIN=0.003`）：空白页不 yield，
     于是旧页号 = 源 PDF 里第 N 个**非空白**页。这个映射可以**离线精确重建**：
     实测 安吉 266 页 → 非空白 149 页 = 旧产物 page_0001..0149，
     逐页缩略图相关系数 corr=1.000（`run/tmp/probe/map_check.py`）。
     ⇒ 旧页号可映射（推翻「旧页号不可重建」的早期结论）。

做法（每例）：
  a. 源 PDF 逐页算墨迹（用产线自己的 `s1_ocr.render_page` + `page_metrics`，
     阈值取 `pipeline.yaml:blank_guard.ink_min`，与空白守卫同一把尺子）；
     非空白页按顺序排 → `nb[k-1]` = 旧产物 `page_NNNN.png` 对应的源页号。
  b. 读旧批次 `json/<旧例名>/pages.json`：每项有 `page_index` / `source_page`
     （= 旧 PNG 文件名）/ `parsing_res_list`（旧 OCR 文本）。
  c. **对账**：`len(nb) == max(旧文件号)` 才认（`map_ok`）；不等 → 该例不写，
     交给 locate_pages 现场扫。
  d. 关键词证据（`pipeline.yaml:locator.*`）在**旧 OCR 文本**上找首页/首程页；
     续页用 `frontpage_continuation_markers`。
  e. **像素对账**：选中的页，旧 PNG vs 新渲染缩略图，相关系数记进 `verify_corr`。

边界：旧 OCR 文本只用来**选页**，不用来填任何字段值（取值一律来自本产线自己的 OCR）。
每例都在 `locator_source` / `evidence` / `verify_corr` 里留来源，可追。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from wide import common as C          # noqa: E402
from wide import s1_ocr as S          # noqa: E402

FILE_NO = re.compile(r"page_(\d+)_")


def old_case_dir(old_json: Path, row: dict) -> tuple[str | None, str]:
    """我们的 case_id 与旧批次目录命名规则不同（旧：只把 '/' 换成 '__'；
    同目录多份 PDF 时旧名再追加 '__<文件名>'）。按 source_dir 反查，歧义时报出来。"""
    base = str(row["source_dir"]).replace("/", "__")
    stem = Path(str(row["input_file"])).stem
    cands = sorted(p.name for p in old_json.iterdir() if p.is_dir())
    exact = [c for c in cands if c == base or c == row["case_id"] or c == f"{base}__{stem}"]
    if exact:
        return exact[0], "exact"
    pref = [c for c in cands if c.startswith(base + "__") or c.startswith(row["case_id"] + "__")]
    if len(pref) == 1:
        return pref[0], "prefix_unique"
    if len(pref) > 1:
        return None, f"ambiguous:{len(pref)}"
    return None, "not_found"


def old_pages(path: Path) -> list[dict]:
    items = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for it in items:
        m = FILE_NO.search(str(it.get("source_page") or ""))
        txt = " ".join((b.get("block_content") or "") for b in it.get("parsing_res_list") or [])
        out.append({"page_index": it.get("page_index"), "file_no": int(m.group(1)) if m else None,
                    "source_page": it.get("source_page"), "text": txt})
    return out


def thumb_corr(old_png: Path, new_img) -> float:
    """旧 PNG 与新渲染页的缩略图相关系数（1.0 = 同一页）。不读内容，只对像素。"""
    import numpy as np
    from PIL import Image
    if not old_png.exists():
        return -1.0
    a = np.asarray(Image.open(old_png).convert("L").resize((160, 224)), dtype=np.float32)
    b = np.asarray(new_img.convert("L").resize((160, 224)), dtype=np.float32)
    a -= a.mean()
    b -= b.mean()
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return round(float((a * b).sum() / d), 4) if d else 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--case", action="append")
    ap.add_argument("--case-file", default="")
    ap.add_argument("--old-batch", default="/home/ubuntu/medrec/jobs/batch_1002_1936",
                    help="旧批次目录（含 json/<case>/pages.json 与 pages/<case>/page_*.png）")
    ap.add_argument("--out", default="")
    ap.add_argument("--overwrite", action="store_true",
                    help="覆盖 page_index.json 里已有的例（默认只补没有的）")
    ap.add_argument("--corr-min", type=float, default=0.90)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    cfg = C.load_pipeline()
    batch_dir = Path(cfg["paths"]["run_root"]) / a.batch
    out_path = Path(a.out) if a.out else batch_dir / "00_index" / "page_index.json"
    audit = C.Audit(batch_dir / "_audit.jsonl", "00_locate")
    loc = cfg["locator"]
    kw_fp, kw_pn = list(loc["frontpage_keywords"]), list(loc["first_progress_keywords"])
    prio = list(loc.get("first_progress_priority") or kw_pn)
    prio_rank = {k: i for i, k in enumerate(prio)}
    markers = list(loc.get("frontpage_continuation_markers") or [])
    max_fp = int(loc.get("max_frontpage_pages", 2))
    max_pn = int(loc.get("max_first_progress_pages", 3))
    follow = int(loc.get("first_progress_follow_pages", 0))
    guard = cfg["blank_guard"]

    old_json = Path(a.old_batch) / "json"
    old_pages_dir = Path(a.old_batch) / "pages"
    if not old_json.is_dir():
        C.err(f"旧批次 json 不存在：{old_json}")
        return 2

    import pypdfium2 as pdfium
    rows = S.read_manifest(batch_dir / "00_index" / "manifest.tsv")
    want = list(a.case or [])
    if a.case_file:
        want += [x.strip() for x in Path(a.case_file).read_text(encoding="utf-8").splitlines()
                 if x.strip()]
    if want:
        rows = [r for r in rows if r["case_id"] in want]

    existing = C.read_json(out_path).get("cases", {}) if out_path.exists() else {}
    t_all = time.time()
    skipped, failed = [], []

    for row in rows:
        case = row["case_id"]
        if case in existing and not a.overwrite:
            C.log(f"{case}: 已有定位（{existing[case].get('locator_source')}），跳过")
            continue
        old_name, how = old_case_dir(old_json, row)
        pj = old_json / old_name / "pages.json" if old_name else None
        if not old_name or not pj.exists():
            C.warn(f"{case}: 旧批次无 pages.json（{how}）→ 需要 locate_pages 现场扫")
            skipped.append((case, f"no_old_pages_json:{how}"))
            continue
        items = old_pages(pj)
        filenos = [it["file_no"] for it in items if it["file_no"]]
        if not filenos:
            C.warn(f"{case}: 旧 pages.json 里没有 page_NNNN 文件名 → 无法映射")
            skipped.append((case, "no_file_no"))
            continue
        # 对账不变量：**旧 PNG 张数** = 源 PDF 非空白页数（旧产线跳过空白页但 PNG 连续编号）。
        # 旧 pages.json 的条目常常少于 PNG 张数（实测 104 例里 66 例有缺口、合计少 931 页，
        # 见 reports 风险③），所以 max(file_no) 不能当页数用。
        n_png = len(list((old_pages_dir / old_name).glob("page_*.png")))

        pdf = pdfium.PdfDocument(row["pdf_path"])
        n = len(pdf)
        nb, kinds = [], {}
        for p in range(n):
            img, _scale = S.render_page(pdf, p, cfg["render"])
            m = S.page_metrics(img, guard)
            kind = S.classify_page(m, guard)
            if m["ink_ratio"] >= float(guard["ink_min"]):
                nb.append(p)
            else:
                kinds[p] = "blank"
        pdf.close()
        ref = n_png or max(filenos)
        if len(nb) != ref:
            C.warn(f"{case}: 墨迹序号映射对不上（非空白 {len(nb)} ≠ 旧 PNG {n_png}"
                   f"（旧最大页号 {max(filenos)}），源 {n} 页）→ 不映射，交给 locate_pages")
            skipped.append((case, f"map_mismatch:{len(nb)}!={ref}"))
            continue

        def to_new(file_no: int | None):
            return nb[file_no - 1] if file_no and 1 <= file_no <= len(nb) else None

        # ---- 关键词证据（旧 OCR 文本，只用于选页）----
        # 选页一律在**旧文件号**空间做（旧页序相邻 = 旧文件号相邻），选完再映射到源页号。
        # 直接在源页号上取连续窗口会吃进空白页（旧产线跳过的页），那些页既不在 nb 也
        # 没有旧产物，取到就是错的。
        by_file = {it["file_no"]: it for it in items if it["file_no"]}
        fp_files, pn_files = [], []
        for fno, it in by_file.items():
            hit_fp = [k for k in kw_fp if k in it["text"]]
            hit_pn = [k for k in kw_pn if k in it["text"]]
            if hit_fp:
                fp_files.append((fno, it, hit_fp))
            if hit_pn:
                pn_files.append((fno, it, hit_pn))

        # 首页：命中页 + 邻页里带续页标记的（首页常跨两页，续页常常没有标题）
        fp_title_f = sorted({f for f, _, _ in fp_files})
        fp_all_f = list(fp_title_f)
        for f in fp_title_f:
            for q in (f - 1, f + 1):
                if q in fp_all_f or q not in by_file:
                    continue
                if any(k in by_file[q]["text"] for k in markers):
                    fp_all_f.append(q)
        fp_pages = sorted(p for p in (to_new(f) for f in sorted(set(fp_all_f))[:max_fp])
                          if p is not None)
        fp_files_f = sorted(set(fp_all_f))[:max_fp]

        # 首程：按写法优先级挑标题页，再往后带 follow 页（旧文件号连续）
        pn_title_f = sorted({f for f, _, _ in pn_files},
                            key=lambda f: (min([prio_rank.get(k, 99) for k in
                                                next(h for q, _, h in pn_files if q == f)] or [99]), f))
        pn_win_f: list[int] = []
        for h in pn_title_f[:1]:
            pn_win_f += [q for q in range(h, h + 1 + follow)
                         if q in by_file and q not in fp_files_f]
        pn_win_f = sorted(pn_win_f)[:max_pn if not follow else max_pn + follow]
        pn_window = sorted(p for p in (to_new(f) for f in pn_win_f) if p is not None)
        pn_kind = None
        if pn_title_f:
            kinds_hit = next(h for q, _, h in pn_files if q == pn_title_f[0])
            pn_kind = min(kinds_hit, key=lambda k: prio_rank.get(k, 99))

        # ---- 像素对账：选中的页，旧 PNG vs 新渲染 ----
        pdf = pdfium.PdfDocument(row["pdf_path"])
        corr = {}
        for p in sorted(set(fp_pages) | set(pn_window)):
            img, _scale = S.render_page(pdf, p, cfg["render"])
            file_no = nb.index(p) + 1
            corr[p] = thumb_corr(old_pages_dir / old_name / f"page_{file_no:04d}.png", img)
        pdf.close()
        low = [p for p, c in corr.items() if c < a.corr_min]

        evidence = []
        for fno, it, hits in fp_files + pn_files:
            if fno in set(fp_files_f) | set(pn_win_f):
                cjk = sum(1 for ch in it["text"] if "\u4e00" <= ch <= "\u9fff")
                evidence.append({"new_page": to_new(fno), "old_file_no": fno,
                                 "old_page_index": it["page_index"],
                                 "old_source_page": it["source_page"], "keywords": hits,
                                 "old_text_cjk": cjk, "corr": corr.get(to_new(fno))})
        existing[case] = {
            "frontpage_pages": fp_pages,
            "frontpage_title_pages": [to_new(f) for f in fp_title_f if to_new(f) is not None],
            "frontpage_continuation_pages": [to_new(f) for f in
                                             sorted(set(fp_all_f) - set(fp_title_f))
                                             if to_new(f) is not None],
            "first_progress_pages": pn_window,
            "frontpage_all_hits": [to_new(f) for f in {f for f, _, _ in fp_files}
                                   if to_new(f) is not None],
            "first_progress_all_hits": sorted({to_new(f) for f, _, _ in pn_files
                                               if to_new(f) is not None}),
            "first_progress_title_pages": [to_new(f) for f in pn_title_f[:max_pn]
                                           if to_new(f) is not None],
            "first_progress_kind": pn_kind,
            "source_pages": n,
            "pages_scanned": len(items),
            "pages_blank_skipped": n - len(nb),
            "pages_skipped_kind": kinds,
            "scan_stopped_early": False,
            "scan_order_prior": "",
            "pred_pages": [],
            "scan_budget_hit": False,
            "grace_pages": 0,
            "grace_used": False,
            "evidence": evidence,
            "strips": {},
            "locator_source": f"old_batch_evidence:{Path(a.old_batch).name}+ink_rank_map",
            "old_case_dir": old_name,
            "old_pages_json": str(pj),
            "old_match": how,
            "old_n_items": len(items),
            "old_max_file_no": max(filenos),
            "old_n_png": n_png,
            "old_json_gap": n_png - len(items),
            "nonblank_count": len(nb),
            "map_ok": True,
            "verify_corr": corr,
            "verify_corr_min": a.corr_min,
            "verify_corr_low": low,
        }
        audit.emit(case_id=case, kind="locate_map", old_case_dir=old_name,
                   source_pages=n, nonblank=len(nb), frontpage_pages=fp_pages,
                   first_progress_pages=pn_window, verify_corr=corr,
                   pages_json_sha256=C.sha256_bytes(pj.read_bytes()))
        C.log(f"{case}: 源 {n} 页 / 非空白 {len(nb)}（旧 PNG {n_png} / JSON 条目 {len(items)}）→ 首页 {fp_pages}"
              f"（标题 {[to_new(f) for f in fp_title_f]}）/ 首程 {pn_window}（{pn_kind}）"
              f"/ corr {dict(corr)}")
        if low:
            C.warn(f"{case}: 像素对账低于 {a.corr_min}：{low} —— 映射可疑，别用")
        if not fp_pages:
            C.warn(f"{case}: 旧 OCR 文本里没有首页关键词 → 首页列将为 null，需现场扫")
        C.write_json(out_path, {"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                "source": f"old_batch_evidence:{Path(a.old_batch).name}"
                                          "+ink_rank_map",
                                "cases": existing})

    C.write_json(out_path, {"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            "source": f"old_batch_evidence:{Path(a.old_batch).name}+ink_rank_map",
                            "old_batch": a.old_batch, "ink_min": guard["ink_min"],
                            "cases": existing})
    C.log(f"写入 {out_path}（用时 {time.time() - t_all:.0f}s）")
    if skipped:
        C.log("未映射（需 locate_pages 现场扫）：")
        for c, why in skipped:
            C.log(f"  {c}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
