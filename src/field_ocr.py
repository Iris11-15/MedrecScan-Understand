"""结构化表单的字段级手写识别（PaddleOCR-VL-1.6）。

为什么不能整页直接跑：
    整页 1524x1368 送进 VLM 时会被 max_pixels 降采样，一个手写字最后只剩十几个像素。
    结构化表单的价值就在于 —— 你知道每个字段在哪，可以**按格子裁剪**单独识别，
    等效分辨率提升 5-10 倍。这是手写场景最大的杠杆。

三个关键手段：
    1. 裁剪 + 放大   把字段抠出来，缩放到足够像素，手写笔画才留得住
    2. 逐字段高分辨率 整页只能用 ~100 万像素；一个格子可以独占这个预算
    3. 约束解码      枚举/勾选字段不从零生成，而是从候选集里选 —— 把"识别"降级为"分类"

用法：
    # 1) 先生成字段定义模板
    python field_ocr.py form.png --init-fields fields.json

    # 2) 编辑 fields.json 填上每个字段的 bbox（可先在交互报告里量）

    # 3) 按字段识别
    python field_ocr.py form.png --fields fields.json
"""

from __future__ import annotations

import argparse
import base64
import difflib
import html
import json
import re
import sys
from pathlib import Path

# 字段裁剪时向外扩的像素，避免把字的边缘切掉
PAD = 10
# 裁剪后短边至少放大到这个尺寸（手写笔画需要足够像素）
MIN_SIDE = 120
# 单个裁剪块允许的像素上限；裁剪块很小，可以给得比整页宽裕
CROP_MAX_PIXELS = 1600 * 28 * 28
MAX_UPSCALE = 5.0


def init_fields(image_path: Path, out_path: Path) -> None:
    """生成字段定义模板骨架。"""
    from PIL import Image

    with Image.open(image_path) as im:
        w, h = im.size

    template = {
        "template": image_path.stem,
        "coordinate_space": "pixel",  # 或 "normalized"(0-1000)
        "image_size": [w, h],
        "fields": [
            {"name": "字段示例_姓名", "bbox": [0, 0, 200, 60], "type": "text"},
            {"name": "字段示例_性别", "bbox": [0, 0, 200, 60], "type": "enum",
             "options": ["男", "女"]},
            {"name": "字段示例_日期", "bbox": [0, 0, 200, 60], "type": "date"},
        ],
        "_说明": [
            "bbox 用 [x1, y1, x2, y2]，坐标系由 coordinate_space 决定",
            "type: text | enum | date | number",
            "enum 必须给 options；识别结果会被约束到候选集里",
            "改完删掉 _说明 和字段示例即可",
        ],
    }
    out_path.write_text(json.dumps(template, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"字段模板 -> {out_path.resolve()}")
    print(f"原图尺寸 {w}x{h}，按这个坐标系填 bbox 即可。")


def resolve_bbox(field: dict, space: str, img_size: tuple[int, int]) -> list[int]:
    x1, y1, x2, y2 = field["bbox"]
    if space == "normalized":
        w, h = img_size
        x1, y1, x2, y2 = x1 / 1000 * w, y1 / 1000 * h, x2 / 1000 * w, y2 / 1000 * h
    return [int(round(v)) for v in (x1, y1, x2, y2)]


def crop_field(image, bbox: list[int]):
    """裁剪 + 放大，让手写笔画有足够像素。"""
    from PIL import Image

    x1, y1, x2, y2 = bbox
    W, H = image.size
    box = (max(0, x1 - PAD), max(0, y1 - PAD),
           min(W, x2 + PAD), min(H, y2 + PAD))
    crop = image.crop(box)
    cw, ch = crop.size
    if min(cw, ch) < MIN_SIDE:
        scale = min(MAX_UPSCALE, MIN_SIDE / max(1, min(cw, ch)))
        if scale > 1:
            crop = crop.resize((max(1, int(cw * scale)), max(1, int(ch * scale))),
                               Image.Resampling.LANCZOS)
    return crop


def normalize(s: str) -> str:
    """去掉空白、常见标点，并统一全角，便于和候选集比对。"""
    s = (s or "").strip()
    s = s.translate(str.maketrans("０１２３４５６７８９ＡＢＣ",
                                  "0123456789ABC"))
    return re.sub(r"[\s,，.。;；:：、\-—_/|]+", "", s)


def constrain(raw: str, field: dict) -> tuple[str, str, float]:
    """把原始输出约束到候选集。返回 (取值, 状态, 相似度)。"""
    ftype = field.get("type", "text")
    options = field.get("options") or []

    if ftype == "enum" and options:
        n = normalize(raw)
        if not n:
            return "", "空", 0.0
        best, best_ratio = None, 0.0
        for opt in options:
            no = normalize(opt)
            if n == no:
                return opt, "精确匹配", 1.0
            if no and (no in n or n in no):
                r = 0.95
            else:
                r = difflib.SequenceMatcher(None, n, no).ratio()
            if r > best_ratio:
                best, best_ratio = opt, r
        if best_ratio >= 0.6:
            return best, f"模糊匹配 {best_ratio:.2f}", best_ratio
        return raw, "不在候选集", best_ratio

    if ftype == "date":
        m = re.search(r"(\d{2,4})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})", raw or "")
        if m:
            y, mo, d = m.groups()
            if len(y) == 2:
                y = "20" + y
            return f"{y}-{int(mo):02d}-{int(d):02d}", "规范化为 YYYY-MM-DD", 1.0
        return raw.strip(), "日期未解析", 0.0

    if ftype == "number":
        m = re.search(r"-?\d+(?:\.\d+)?", raw or "")
        return (m.group(0) if m else raw.strip()), \
               ("提取数值" if m else "数值未解析"), (1.0 if m else 0.0)

    return (raw or "").strip(), "原文", 1.0


def render_html(image_path: Path, results: list[dict], out_path: Path) -> None:
    rows = []
    for r in results:
        status = r["status"]
        tone = "#10b981" if r["confidence"] >= 0.95 else (
            "#f59e0b" if r["confidence"] >= 0.6 else "#ef4444")
        crop_uri = base64.b64encode(r["crop_png"]).decode()
        raw = html.escape((r["raw"] or "").strip()[:200])
        rows.append(f"""
    <tr>
      <td class="fname">{html.escape(r['name'])}<div class="ftype">{html.escape(r['type'])}</div></td>
      <td><img src="data:image/png;base64,{crop_uri}" alt=""></td>
      <td class="val">{html.escape(r['value']) or '<span class="empty">（空）</span>'}</td>
      <td><span class="st" style="--c:{tone}">{html.escape(status)}</span></td>
      <td class="raw">{raw}</td>
    </tr>""")

    out_path.write_text(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>字段级识别结果 · {html.escape(image_path.name)}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; background:#0f1115; color:#e5e7eb;
         font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
  header {{ padding:16px 22px; border-bottom:1px solid #1f2430; }}
  h1 {{ margin:0 0 4px; font-size:16px; font-weight:650; }}
  .meta {{ color:#8b93a1; font-size:12.5px; }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ padding:8px 12px; border-bottom:1px solid #171b23; text-align:left;
           vertical-align:middle; }}
  th {{ font-size:12px; color:#8b93a1; font-weight:600; position:sticky; top:0;
       background:#141821; }}
  td img {{ max-height:76px; max-width:300px; background:#fff; border-radius:4px;
           display:block; }}
  .fname {{ font-weight:600; white-space:nowrap; }}
  .ftype {{ font-size:11px; color:#6b7280; font-weight:400; }}
  .val {{ font-size:15px; font-weight:600; color:#e5e7eb; }}
  .empty {{ color:#6b7280; font-weight:400; }}
  .st {{ display:inline-block; padding:1px 9px; border-radius:99px; font-size:12px;
        border:1px solid var(--c); color:var(--c);
        background:color-mix(in srgb, var(--c) 14%, transparent); }}
  .raw {{ font-size:12px; color:#9ca3af; max-width:360px; word-break:break-word; }}
</style></head><body>
<header>
  <h1>字段级识别结果</h1>
  <div class="meta">{html.escape(image_path.name)} · 共 {len(results)} 个字段 ·
    高置信 {sum(1 for r in results if r['confidence'] >= 0.95)} ·
    需人工复核 {sum(1 for r in results if r['confidence'] < 0.6)}</div>
</header>
<table>
  <thead><tr><th>字段</th><th>裁剪图</th><th>取值</th><th>状态</th><th>原始输出</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
</body></html>""", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="结构化表单字段级手写识别")
    ap.add_argument("image")
    ap.add_argument("--fields", help="字段定义 JSON")
    ap.add_argument("--init-fields", metavar="OUT", help="生成字段定义模板后退出")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--device", default="gpu:0")
    args = ap.parse_args()

    image_path = Path(args.image)
    if not image_path.exists():
        print(f"找不到图片: {image_path}", file=sys.stderr)
        return 1
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.init_fields:
        init_fields(image_path, Path(args.init_fields))
        return 0

    if not args.fields:
        print("需要 --fields，或先用 --init-fields 生成模板", file=sys.stderr)
        return 1

    spec = json.loads(Path(args.fields).read_text(encoding="utf-8"))
    space = spec.get("coordinate_space", "pixel")
    fields = spec.get("fields", [])
    if not fields:
        print("字段定义里没有 fields", file=sys.stderr)
        return 1

    from PIL import Image
    from paddleocr import PaddleOCRVL

    image = Image.open(image_path).convert("RGB")
    pipeline = PaddleOCRVL(device=args.device)

    results = []
    for i, f in enumerate(fields, 1):
        bbox = resolve_bbox(f, space, image.size)
        crop = crop_field(image, bbox)
        crop_path = out_dir / f"_crop_{i:02d}.png"
        crop.save(crop_path)

        raw = ""
        for res in pipeline.predict(
            str(crop_path),
            use_layout_detection=False,   # 关键：跳过版面检测，把整块当一个元素
            prompt_label="ocr",
            max_pixels=CROP_MAX_PIXELS,   # 小图可以给高分辨率预算
        ):
            data = res.json
            data = data.get("res", data) if isinstance(data, dict) else data
            for b in (data.get("parsing_res_list") or []):
                raw += (b.get("block_content") or "")

        value, status, conf = constrain(raw, f)
        results.append({
            "name": f["name"], "type": f.get("type", "text"),
            "bbox": bbox, "value": value, "status": status,
            "confidence": conf, "raw": raw,
            "crop_png": (out_dir / f"_crop_{i:02d}.png").read_bytes(),
        })
        flag = "OK " if conf >= 0.95 else ("?  " if conf >= 0.6 else "!! ")
        print(f"{flag}{f['name']:<20} {value!r:<28} {status}")

    # 落盘
    payload = {
        "template": spec.get("template"),
        "source": image_path.name,
        "fields": {r["name"]: r["value"] for r in results},
        "detail": [{k: r[k] for k in ("name", "type", "bbox", "value",
                                      "status", "confidence", "raw")}
                   for r in results],
    }
    json_out = out_dir / f"{image_path.stem}_fields.json"
    json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    html_out = out_dir / f"{image_path.stem}_fields.html"
    render_html(image_path, results, html_out)

    low = [r["name"] for r in results if r["confidence"] < 0.6]
    print(f"\n结构化结果 -> {json_out.resolve()}")
    print(f"核对报告   -> {html_out.resolve()}")
    if low:
        print(f"\n需要人工复核（{len(low)}）：{', '.join(low)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
