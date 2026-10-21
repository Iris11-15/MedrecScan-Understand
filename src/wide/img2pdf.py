#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""img2pdf.py —— 散页图片目录 → 病例 PDF 的**快速**归一脚本（round42l）。

病例规律（用户 2026-10-17 确认）：**存放图片的文件夹里的全部图片 = 同一份病例**
⇒ 一个散页目录 = 一个病例 = 一个 PDF。合并完直接按老路跑六阶段。

和 `wide.input_normalize` 的分工：
  * 无损嵌入 / 自然序 / EXIF 转正 / 白名单 / 幂等 sha256 —— 全部复用 input_normalize，
    本脚本不另写一份 PDF 生成代码（两份实现迟早不一致）。
  * 本脚本只改**默认值**：OCR 守卫默认关（那是 5.2 h 的全部来源），并行默认 8 进程。

★ 守卫不是删掉了，是变成开关：
  * `--guard` 打开原 OCR 守卫（首页/中间/末页 各 1 页，比对 住院号 + 姓名）。
  * 零成本的**文件名数字前缀守卫默认仍开**：同目录出现 ≥2 个不同数字前缀 ⇒ 禁止合并。
    实测 2449 目录 / 4.1 s / 0 个多前缀目录 ⇒ 白捡的一道防线。
  * 关掉 OCR 守卫的产物会在 summary 里记 `guard=skipped`，绝不冒充「守卫过了」。

产物布局与网页暂存区一致（<stage>/tree + <stage>/_merge_* + .stage_status.json），
所以网页能直接看见，命令行也能直接 `bin/medrec <stage>/tree --batch <b>`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wide import common as C                       # noqa: E402
from wide import input_normalize as IN             # noqa: E402

DEFAULT_WORKERS = 8


def chmod_tree(root: Path, dir_mode: int = 0o700, file_mode: int = 0o600) -> int:
    """产物含患者影像 ⇒ 目录 700 / 文件 600（红线⑨）。返回改过的文件数。"""
    root = Path(root)
    if not root.is_dir():
        return 0
    n = 0
    for dirpath, dirnames, filenames in os.walk(str(root)):
        try:
            os.chmod(dirpath, dir_mode)
        except OSError:
            pass
        for fn in filenames:
            try:
                os.chmod(Path(dirpath, fn), file_mode)
                n += 1
            except OSError:
                pass
    return n


def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="散页图片目录 → 病例 PDF（快速：默认不跑 OCR 守卫，8 进程并行）")
    ap.add_argument("--src-root", required=True,
                    help="散页图片源根（只读，例如 /raid5/history_13_15）")
    ap.add_argument("--stage", default="",
                    help="暂存目录：产出 <stage>/tree + <stage>/_merge_*（与网页暂存区同布局）")
    ap.add_argument("--out-tree", default="",
                    help="只给 PDF 树根时用（不给 --stage 时必填其一）")
    ap.add_argument("--meta-dir", default="", help="状态/清单落点，默认 = --out-tree 的父目录")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="并行合并进程数（默认 %d）" % DEFAULT_WORKERS)
    ap.add_argument("--guard", action="store_true",
                    help="打开 OCR 多患者守卫（每目录 3 页 OCR，慢一个量级；默认关）")
    ap.add_argument("--no-name-guard", action="store_true",
                    help="关掉零成本的文件名数字前缀守卫（只用于对照）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个目录（试跑）")
    ap.add_argument("--batch", default="", help="只用于打印下一步跑批命令，不会自动跑")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列目录与目标 PDF，不写一个字节")
    a = ap.parse_args()

    src = Path(a.src_root).expanduser()
    if not src.is_dir():
        print("ERROR 源根不存在：%s" % src)
        return 2
    if a.stage:
        stage = Path(a.stage).expanduser()
        tree, meta = stage / "tree", stage
    elif a.out_tree:
        tree = Path(a.out_tree).expanduser()
        meta = Path(a.meta_dir).expanduser() if a.meta_dir else tree.parent
    else:
        print("ERROR 要给 --stage <目录> 或 --out-tree <目录>")
        return 2

    dirs = IN.find_image_dirs(src)
    print("src_root=%s  散页目录=%d" % (src, len(dirs)))
    if a.limit:
        dirs = dirs[:a.limit]
        print("limit=%d ⇒ 本轮只处理前 %d 个" % (a.limit, len(dirs)))

    plan = IN.plan_targets(src, dirs, tree, False, True)
    dup = [p for p in plan if p[2] is None]
    if a.dry_run:
        for cid, d, out_pdf, _dup in plan[:20]:
            print("  %s  ->  %s" % (IN.mask_case_id(cid), out_pdf or "撞名:禁止"))
        print("dry-run：目标 PDF=%d 撞名禁止=%d（未写任何文件）"
              % (len(plan) - len(dup), len(dup)))
        return 0

    if not a.guard:
        print("\n*** OCR 守卫已关闭（--guard 未给）***")
        print("    依据：用户确认「一个图片目录 = 一份病例」。")
        print("    仍生效的防线：文件名数字前缀守卫（%s）。"
              % ("开" if not a.no_name_guard else "关"))
        print("    风险：同目录混两个患者的页时不会被拦下 ⇒ 会造出一份不存在的病历。")
        print("    上一轮开守卫实测：2449 目录拦下 127 个（multi_patient）+ 报警 263 个。\n")

    stage.mkdir(parents=True, exist_ok=True) if a.stage else None
    meta.mkdir(parents=True, exist_ok=True)
    tree.mkdir(parents=True, exist_ok=True)
    st = {"state": "running", "phase": "合并（%s）" % ("OCR 守卫开" if a.guard else "无 OCR 守卫"),
          "src": str(src), "tree": str(tree), "total_dirs": len(dirs), "done_dirs": 0,
          "workers": a.workers, "guard": bool(a.guard), "name_guard": (not a.no_name_guard),
          "errors": [], "tool": "wide.img2pdf"}
    write_json(meta / ".stage_status.json", st)

    t0 = time.time()
    last = [0.0]

    def prog(i, total, d, r):
        if time.time() - last[0] < 2.0 and i < total:
            return
        last[0] = time.time()
        st["done_dirs"] = i
        st["total_dirs"] = total
        st["elapsed_s"] = round(time.time() - t0, 1)
        write_json(meta / ".stage_status.json", st)
        print("[%d/%d] %.1fs %s" % (i, total, st["elapsed_s"], IN.mask_case_id(r.get("case_id", ""))),
              flush=True)

    cfg = C.load_pipeline()
    res = IN.merge_tree(src, tree, cfg, guard=a.guard, tree_out=True,
                        work_dir=meta / "_guard", progress=prog, workers=a.workers,
                        name_guard=not a.no_name_guard, limit=a.limit)
    dt = time.time() - t0

    IN.write_manifest(meta / "_merge_manifest.csv", res["rows"])
    IN.write_alerts(meta / "_merge_alerts.json", res["alerts"])
    IN.write_guard(meta / "_merge_guard.json", res["guard_records"])
    s = dict(res["summary"])
    s.update({"guard": ("ocr+name" if a.guard else ("name_only" if not a.no_name_guard else "none")),
              "workers": a.workers, "seconds": round(dt, 1),
              # 用 summary 里的 src_dirs 算速率，不用调用侧的 dirs —— 两者一度因为
              # --limit 只在 merge_tree 内部生效而不一致（速率少算一个量级）。
              "dirs_per_second": round(s["src_dirs"] / dt, 2) if dt else None,
              "alerts": len(res["alerts"]), "duplicate_target": len(dup),
              "src_root": str(src), "tree": str(tree)})
    write_json(meta / "_merge_summary.json", s)

    chmod_tree(tree)
    if (meta / "_guard").is_dir():
        chmod_tree(meta / "_guard")
    st["state"] = "done" if not res["alerts"] else "done_with_alerts"
    st["phase"] = "完成"
    st["elapsed_s"] = round(dt, 1)
    st["summary"] = s
    st["alert_path"] = str(meta / "_merge_alerts.json")
    write_json(meta / ".stage_status.json", st)

    print("\nsrc_dirs=%d merged=%d skipped_identical=%d blocked=%d no_image=%d pages=%d"
          % (s["src_dirs"], s["merged"], s["skipped"], s["blocked"],
             s["no_image_dirs"], s["pages"]))
    print("alerts=%d  guard=%s  workers=%d  用时=%.1fs（%.2f 目录/s）"
          % (s["alerts"], s["guard"], s["workers"], dt, s["dirs_per_second"]))
    print("清单：%s" % (meta / "_merge_manifest.csv"))
    if res["alerts"]:
        print("报警：%s" % (meta / "_merge_alerts.json"))
        for al in res["alerts"][:10]:
            print("  ALERT %s kind=%s" % (IN.mask_case_id(al["case_id"]), al["kind"]))
        if len(res["alerts"]) > 10:
            print("  … 其余 %d 条见文件" % (len(res["alerts"]) - 10))

    batch = a.batch or "<批次名>"
    print("\n下一步（按老路跑六阶段）：")
    print("  cd /home/ubuntu/medrec && bin/medrec %s --batch %s" % (tree, batch))
    return 0 if s["blocked"] == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
