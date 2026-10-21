r"""PaddleOCR-VL-1.6 HTTP 客户端示例。

服务端启动：  pwsh -File .\serve.ps1
然后：
    # 整页图片
    python client_example.py page.png

    # PDF（多页自动逐页返回）
    python client_example.py 合同.pdf

    # 只识别一个字段（跳过版面检测，等价于 field_ocr.py 的做法）
    python client_example.py --crop page.png --bbox 130 35 1384 127

    # 批量：整个目录（图片 + PDF 混着都行）
    python client_example.py --dir .\forms

    # 自检：服务活着吗
    python client_example.py --health

客户端只依赖 requests，可以放到任何机器上跑（只要网络能到服务端）。
文件以 base64 上传，服务端不需要能访问你的文件系统。

关于 fileType（踩过的坑）：
    服务端是 `file_type = "PDF" if request.fileType == 0 else "IMAGE"`，
    所以 0=PDF、1=IMAGE，**表示文档类型而非编码方式**；base64/URL 是服务端自己判断的。
    省略 fileType 会导致 422 "File type cannot be determined"。本脚本按文件头自动判断。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import requests

DEFAULT_URL = "http://127.0.0.1:8080"
TIMEOUT = 600  # 整页复杂版面要几分钟


def health(base: str) -> bool:
    try:
        r = requests.get(f"{base}/health", timeout=10).json()
        ok = r.get("errorCode") == 0
        print(f"/health -> {r.get('errorMsg')}  {'OK' if ok else '异常'}")
        return ok
    except requests.RequestException as e:
        print(f"服务不可达: {e}")
        print("先启动：pwsh -File .\\serve.ps1")
        return False


def encode(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def detect_file_type(path: Path) -> int:
    """返回值即请求里的 fileType。

    服务端逻辑（paddlex/inference/serving/basic_serving/_pipeline_apps/_common/ocr.py）:
        file_type = "PDF" if request.fileType == 0 else "IMAGE"

    也就是说 fileType 表示**文档类型**，不是「base64 还是 URL」：
        0 = PDF      1 = IMAGE
    base64 / URL 是服务端自己判断的。省略 fileType 时会去解析 URL，
    对 base64 一律失败（"File type cannot be determined"），所以必须显式给。
    """
    try:
        if path.read_bytes()[:5] == b"%PDF-":
            return 0
    except OSError:
        pass
    return 0 if path.suffix.lower() == ".pdf" else 1


def call(base: str, image: Path, *, crop: tuple | None = None,
         max_pixels: int | None = None, visualize: bool = False,
         file_type: int | None = None) -> dict:
    """调用 /layout-parsing。

    关键参数：
        fileType  0=PDF  1=IMAGE  （不传会 422）
        useLayoutDetection=False + promptLabel='ocr'
            -> 跳过版面检测，把整张图当成一个元素做识别。
               这正是「按字段裁剪后单独识别」的用法。
        maxPixels  -> 送进 VLM 的像素上限。裁剪块小，可以给得比整页宽裕。
    """
    payload: dict = {
        "file": encode(image),
        "fileType": detect_file_type(image) if file_type is None else file_type,
        "returnMarkdownImages": False,  # 关掉内嵌图，响应小很多
    }
    if crop:
        payload.update({
            "useLayoutDetection": False,
            "promptLabel": "ocr",
            "maxPixels": max_pixels or 1600 * 28 * 28,
        })
    elif max_pixels:
        payload["maxPixels"] = max_pixels
    if visualize:
        payload["visualize"] = True

    resp = requests.post(f"{base}/layout-parsing", json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errorCode") != 0:
        raise RuntimeError(f"服务端返回错误: {data.get('errorMsg')}")
    return data["result"]


def summarize(result: dict, image: Path) -> dict:
    """把服务端响应压成好用的形状。"""
    items = result.get("layoutParsingResults", [])
    out = {"source": image.name, "pages": []}
    for it in items:
        pruned = it.get("prunedResult") or {}
        blocks = pruned.get("parsing_res_list") or []
        out["pages"].append({
            "width": pruned.get("width"),
            "height": pruned.get("height"),
            "block_count": len(blocks),
            "markdown": (it.get("markdown") or {}).get("text", ""),
            "blocks": [
                {"label": b.get("block_label"), "order": b.get("block_order"),
                 "bbox": b.get("block_bbox"), "content": b.get("block_content")}
                for b in blocks
            ],
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="PaddleOCR-VL-1.6 HTTP 客户端")
    ap.add_argument("image", nargs="?", help="图片路径")
    ap.add_argument("--dir", help="批量处理整个目录")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"服务地址，默认 {DEFAULT_URL}")
    ap.add_argument("--crop", action="store_true",
                    help="按 --bbox 裁剪后只识别该区域（跳过版面检测）")
    ap.add_argument("--bbox", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"))
    ap.add_argument("--max-pixels", type=int)
    ap.add_argument("--file-type", type=int, choices=[0, 1], default=None,
                    help="0=PDF 1=IMAGE；默认按文件头自动判断")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--health", action="store_true")
    args = ap.parse_args()

    if args.health:
        return 0 if health(args.url) else 1
    if not health(args.url):
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    targets: list[Path] = []
    if args.dir:
        d = Path(args.dir)
        targets = sorted(p for p in d.iterdir()
                         if p.suffix.lower() in {".png", ".jpg", ".jpeg",
                                                 ".bmp", ".webp", ".pdf"})
    elif args.image:
        targets = [Path(args.image)]
    if not targets:
        print("需要 --image 或 --dir", file=sys.stderr)
        return 1

    crop_box = None
    if args.crop:
        if not args.bbox:
            print("--crop 需要配合 --bbox X1 Y1 X2 Y2", file=sys.stderr)
            return 1
        if any(t.suffix.lower() == ".pdf" for t in targets):
            print("--crop 只支持图片（PDF 请先渲染成图，或整份交给服务端）",
                  file=sys.stderr)
            return 1
        crop_box = tuple(args.bbox)

    for path in targets:
        source = path
        tmp = None
        if crop_box:
            from PIL import Image
            tmp = out_dir / f"_crop_{path.stem}.png"
            Image.open(path).crop(crop_box).save(tmp)
            source = tmp

        result = call(args.url, source, crop=crop_box, max_pixels=args.max_pixels,
                      file_type=args.file_type)
        summary = summarize(result, path)

        json_out = out_dir / f"{path.stem}_api.json"
        json_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding="utf-8")

        pages = summary["pages"]
        print(f"{path.name}: {len(pages)} 页, "
              f"{sum(p['block_count'] for p in pages)} 个块 -> {json_out.name}")
        if crop_box:
            for b in pages[0]["blocks"]:
                print(f"    字段识别: {b['content']!r}")
        if tmp:
            tmp.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
