#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F1 → F2 适配器：把 layered.py 的报告转成 compile.py 认的页数组。

为什么需要它
    compile.py 读的是 PaddleX/webui 的导出格式：
        page["parsing_res_list"][i]["block_label"] / ["block_content"] / ["block_bbox"]
    而 layered.py 写出来的是它自己的报告：
        rep["size"]=[W,H]、rep["blocks"][i]["label"] / ["text"] / ["bbox"]
    两边只对得上文本那一处（compile.py 里有 `block_content or text` 回退），
    label / bbox 都对不上 → 直接把报告丢给 compile.py 会得到空页。

映射（纯改名，不改语义、不丢字段）
    size[0]/size[1]  -> width / height      ★ compile.py 靠 width 做分栏排序
    blocks[i].label  -> block_label
    blocks[i].text   -> block_content
    blocks[i].bbox   -> block_bbox
    route / reason / vl_error / vl_seconds / truncated 原样带过去
      （compile.py 用 route 判定“是缺失、占位还是截断”，不能丢）

用法
    python3 to_pages.py jobs/b1/raw/a_layered.json                 # -> 同目录 a_page.json
    python3 to_pages.py jobs/b1/raw/*_layered.json --out jobs/b1/pages
    python3 to_pages.py <目录> --quiet

幂等：输入如果已经是页数组（含 parsing_res_list），原样透传，方便混用。
零依赖：只用标准库。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

Page = dict


def to_page(rep: dict) -> Page:
    """layered 报告 -> 单页 dict。"""
    blocks = rep.get("blocks")
    if blocks is None:
        raise ValueError("输入里没有 blocks —— 这不是 layered.py 的报告")

    size = rep.get("size") or []
    bboxes = [b.get("bbox") or [] for b in blocks]
    xs = [float(v[2]) for v in bboxes if len(v) >= 4]
    ys = [float(v[3]) for v in bboxes if len(v) >= 4]
    W = float(size[0]) if len(size) >= 2 else (max(xs) if xs else 1.0)
    H = float(size[1]) if len(size) >= 2 else (max(ys) if ys else 1.0)

    out: list[dict] = []
    for i, b in enumerate(blocks):
        bbox = b.get("bbox") or []
        item = {
            "block_id": b.get("block_id", i),
            "block_label": b.get("label") or "text",
            "block_content": b.get("text") or "",
            "block_bbox": [float(v) for v in bbox[:4]] if len(bbox) >= 4 else [],
            # route 必须带：compile.py 靠它区分「缺失 / 占位 / 截断」
            "route": b.get("route") or "",
        }
        for k_src, k_dst in (("reason", "reason"), ("vl_error", "vl_error"),
                             ("vl_seconds", "vl_seconds"), ("truncated", "truncated"),
                             ("table_index", "table_index")):
            if b.get(k_src) is not None:
                item[k_dst] = b[k_src]
        out.append(item)

    return {
        "page_index": int(rep.get("page_index") or 0),
        "source": rep.get("source") or "",
        "width": W,
        "height": H,
        "parsing_res_list": out,
    }


def convert(data, quiet: bool = False):
    """返回页数组。已是对应格式则透传。"""
    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict) and "parsing_res_list" in data:
        pages = [data]
    elif isinstance(data, dict) and "prunedResult" in data:
        # webui 流式接口每页回的形状：{"prunedResult": {width,height,parsing_res_list}, ...}
        pages = [data["prunedResult"]]
    elif isinstance(data, dict) and "blocks" in data:
        pages = [to_page(data)]
    else:
        raise ValueError("认不出结构：既不是页数组，也不是 layered 报告")

    n_passthrough = sum(1 for p in pages if isinstance(p, dict) and "parsing_res_list" in p)
    if not quiet and n_passthrough == len(pages):
        print("  已是页数组格式，原样透传", file=sys.stderr)
    return pages


def split_batch(data: dict, out_root: Path, quiet: bool = False) -> int:
    """处理 webui 批量下载的 {batch: [{name, pages:[...]}]}：
    每个文件写一份 <out_root>/<名字>/pages.json。"""
    batch = data.get("batch")
    if not isinstance(batch, list):
        raise ValueError("不是 webui 批量格式（缺 batch 数组）")
    n = 0
    for item in batch:
        name = str(item.get("name") or ("doc%d" % (n + 1)))
        stem = Path(name).stem or name
        pages = item.get("pages") or []
        if not pages:
            continue
        d = out_root / stem
        d.mkdir(parents=True, exist_ok=True)
        (d / "pages.json").write_text(
            json.dumps(pages, ensure_ascii=False, indent=2),
            encoding="utf-8", newline="\n")
        n += 1
        if not quiet:
            blk = sum(len(p.get("parsing_res_list") or []) for p in pages)
            print("  %-36s -> %s/pages.json  (%d 页 / %d 块)" % (name, d, len(pages), blk))
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="F1（layered 报告）-> F2（compile 页数组）适配器")
    ap.add_argument("inputs", nargs="+", help="*_layered.json 或所在目录")
    ap.add_argument("--out", default=None, help="输出目录（默认写回输入文件同目录）")
    ap.add_argument("--merge", action="store_true",
                    help="把多个 *_layered.json 合并成一份多页数组 pages.json"
                         "（一份 PDF 拆成多页时必须用这个，否则每页会被当成独立文档）")
    ap.add_argument("--split", action="store_true",
                    help="输入是 webui 批量下载的 {batch:[{name,pages:[...]}]}，"
                         "按文件拆成 <out>/<名字>/pages.json")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    out_dir = Path(a.out) if a.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    files: list[Path] = []
    for raw in a.inputs:
        p = Path(raw)
        if p.is_dir():
            files += sorted(p.glob("*.json"))
        elif p.exists():
            files.append(p)
        else:
            print("跳过（不存在）: %s" % p, file=sys.stderr)

    ok = bad = 0
    merged: list[Page] = []

    if a.split:
        if not out_dir:
            print("[x] --split 需要 --out 指定输出目录", file=sys.stderr)
            return 2
        total = 0
        for f in files:
            try:
                total += split_batch(json.loads(f.read_text(encoding="utf-8")),
                                     out_dir, a.quiet)
            except Exception as e:                              # noqa: BLE001
                print("[x] %s：%s" % (f.name, e), file=sys.stderr)
                bad += 1
        if not a.quiet:
            print("\n== 拆出 %d 个文件到 %s" % (total, out_dir))
        return 0 if bad == 0 else 1

    for f in files:
        if f.name.endswith("_page.json") or f.name == "pages.json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            pages = convert(data, a.quiet)
        except Exception as e:                                  # noqa: BLE001
            print("[x] %s：%s" % (f.name, e), file=sys.stderr)
            bad += 1
            continue

        if a.merge:
            for p in pages:
                p = dict(p)
                p["page_index"] = len(merged)
                p.setdefault("source_page", f.name)
                merged.append(p)
            ok += 1
            if not a.quiet:
                blk = sum(len(p.get("parsing_res_list") or []) for p in pages)
                print("  + %-38s (%d 页 / %d 块)" % (f.name, len(pages), blk))
            continue

        name = f.name[:-len("_layered.json")] + "_page.json" if f.name.endswith("_layered.json") \
            else f.stem + "_page.json"
        dst = (out_dir or f.parent) / name
        dst.write_text(json.dumps(pages, ensure_ascii=False, indent=2),
                       encoding="utf-8", newline="\n")
        ok += 1
        if not a.quiet:
            blocks = sum(len(p.get("parsing_res_list") or []) for p in pages)
            print("  %-40s -> %s  (%d 页 / %d 块)" % (f.name, dst, len(pages), blocks))

    if a.merge and merged:
        dst = (out_dir or Path(".")) / "pages.json"
        dst.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                       encoding="utf-8", newline="\n")
        if not a.quiet:
            blk = sum(len(p.get("parsing_res_list") or []) for p in merged)
            print("\n== 合并成 %s：%d 页 / %d 块" % (dst, len(merged), blk))

    if not a.quiet:
        print("\n== 完成：%d 个转换成功，%d 个失败" % (ok, bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
