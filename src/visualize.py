"""PaddleOCR-VL-1.6 识别结果可视化。

产出一个自包含的 HTML 报告（图片内嵌为 base64，离线可开）：
    * 原图上按 block_label 着色叠加每个版面块
    * 标出 block_order 阅读顺序序号
    * 右侧栏列出每块的类别 / 坐标 / 内容，点击高亮联动
    * 支持 block_polygon_points（异形框）用 SVG 多边形绘制

用法：
    # 跑一次解析并出报告
    & "...\\python.exe" visualize.py page.png

    # 复用已经产出的 *_res.json，不重跑模型
    & "...\\python.exe" visualize.py page.png --from-json output\\page_res.json

    # 顺便把内置的版面框叠加图也存出来
    & "...\\python.exe" visualize.py page.png --save-builtin
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import sys
from collections import Counter
from pathlib import Path

OUTPUT_DIR = Path("output")

# 按类别分配颜色，尽量语义化
LABEL_COLORS = {
    "text": "#3b82f6",
    "title": "#8b5cf6",
    "paragraph_title": "#8b5cf6",
    "doc_title": "#8b5cf6",
    "table": "#10b981",
    "formula": "#f59e0b",
    "image": "#6b7280",
    "chart": "#06b6d4",
    "seal": "#ef4444",
    "figure_title": "#a78bfa",
    "header": "#94a3b8",
    "header_image": "#94a3b8",
    "footer": "#94a3b8",
    "footer_image": "#94a3b8",
    "footnote": "#cbd5e1",
    "vision_footnote": "#cbd5e1",
    "aside_text": "#cbd5e1",
    "spotting": "#ec4899",
}
DEFAULT_COLOR = "#a3a3a3"


def color_for(label: str) -> str:
    return LABEL_COLORS.get(label, DEFAULT_COLOR)


def unwrap(data):
    """paddleocr 的坑：res.json 返回的是 {"res": {...}} 包了一层的字典，
    而 save_to_json() 落盘的却是内层（文件名后缀 _res 就是从这个 key 来的）。
    两种来源都要能吃，否则会静默渲染出一个空报告。"""
    if isinstance(data, dict) and set(data.keys()) == {"res"}:
        inner = data["res"]
        if isinstance(inner, dict):
            return inner
    return data


def img_to_data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def render_report(image_path: Path, data: dict, title: str) -> str:
    """把 parsing_res_list 渲染成自包含 HTML。"""
    blocks = data.get("parsing_res_list", []) or []
    W = float(data.get("width") or 1)
    H = float(data.get("height") or 1)
    # page_index / page_count 可能是 None（不是 0/1），必须兜住
    page_index = data.get("page_index") or 0
    page_count = data.get("page_count") or 1

    # 用百分比定位，图片响应式缩放时叠加层自动跟随
    boxes, polys = [], []
    for i, b in enumerate(blocks):
        label = b.get("block_label", "")
        color = color_for(label)
        x1, y1, x2, y2 = b["block_bbox"]
        left, top = x1 / W * 100, y1 / H * 100
        w, h = (x2 - x1) / W * 100, (y2 - y1) / H * 100
        order = b.get("block_order")
        badge = f'<span class="badge">{order}</span>' if order is not None else ""
        is_body = 1 if order is not None else 0
        boxes.append(
            f'<div class="box" data-i="{i}" data-label="{html.escape(label)}" '
            f'data-body="{is_body}" style="left:{left:.4f}%;top:{top:.4f}%;'
            f'width:{w:.4f}%;height:{h:.4f}%;--c:{color}" '
            f'title="{html.escape(label)}">{badge}</div>'
        )
        pts = b.get("block_polygon_points")
        if pts:
            pt_str = " ".join(f"{p[0]/W*100:.4f},{p[1]/H*100:.4f}" for p in pts)
            polys.append(
                f'<polygon points="{pt_str}" fill="none" stroke="{color}" '
                f'stroke-width="0.35" vector-effect="non-scaling-stroke" '
                f'data-i="{i}" data-label="{html.escape(label)}"/>'
            )

    rows = []
    for i, b in enumerate(blocks):
        label = b.get("block_label", "")
        color = color_for(label)
        order = b.get("block_order")
        order_txt = f"#{order}" if order is not None else "—"
        is_body = 1 if order is not None else 0
        content = (b.get("block_content") or "").strip()
        preview = html.escape(content[:280] + ("…" if len(content) > 280 else ""))
        full = html.escape(content) if len(content) > 280 else ""
        rows.append(
            f'<div class="row" data-i="{i}" data-label="{html.escape(label)}" '
            f'data-body="{is_body}" style="--c:{color}">'
            f'<div class="rowhead"><span class="ord">{order_txt}</span>'
            f'<span class="lbl">{html.escape(label)}</span>'
            f'<span class="bbox">{b["block_bbox"]}</span></div>'
            f'<div class="content">{preview}</div>'
            + (f'<div class="full" hidden>{full}</div>' if full else "")
            + "</div>"
        )

    hist = Counter(b.get("block_label", "") for b in blocks)
    chips = "".join(
        f'<span class="chip clickable" data-label="{html.escape(k)}" '
        f'style="--c:{color_for(k)}" title="点击筛选">{html.escape(k)} <b>{v}</b></span>'
        for k, v in hist.most_common()
    )
    body_count = sum(1 for b in blocks if b.get("block_order") is not None)
    toolbar = (
        f'<div class="toolbar">'
        f'<button class="tb on" id="f-all">全部 {len(blocks)}</button>'
        f'<button class="tb" id="f-body">仅正文流 {body_count}</button>'
        f'<span class="hint">点类别标签筛选 · 点框或右侧条目联动高亮</span>'
        f'</div>'
    )

    img_uri = img_to_data_uri(image_path)
    legend = "".join(
        f'<span class="chip" style="--c:{c}">{html.escape(k)}</span>'
        for k, c in sorted(LABEL_COLORS.items())
        if k in hist
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>PaddleOCR-VL 识别结果 · {html.escape(title)}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0f1115; color:#e5e7eb;
         font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
  header {{ padding:18px 24px; border-bottom:1px solid #1f2430; }}
  h1 {{ margin:0 0 6px; font-size:17px; font-weight:650; letter-spacing:-.01em; }}
  .meta {{ color:#8b93a1; font-size:12.5px; }}
  .chips {{ margin-top:10px; display:flex; flex-wrap:wrap; gap:6px; }}
  .chip {{ display:inline-flex; align-items:center; gap:5px; padding:2px 9px;
          border-radius:99px; font-size:12px;
          border:1px solid color-mix(in srgb, var(--c) 55%, transparent);
          background:color-mix(in srgb, var(--c) 14%, transparent); color:#e5e7eb; }}
  .chip b {{ font-variant-numeric:tabular-nums; }}
  .chip.clickable {{ cursor:pointer; user-select:none; }}
  .chip.clickable:hover {{ background:color-mix(in srgb, var(--c) 30%, transparent); }}
  .chip.off {{ opacity:.3; }}
  .toolbar {{ margin-top:12px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
  .tb {{ background:#171b23; color:#cbd5e1; border:1px solid #262c38;
        border-radius:7px; padding:4px 12px; font-size:12.5px; cursor:pointer;
        font-variant-numeric:tabular-nums; }}
  .tb:hover {{ background:#1d222c; }}
  .tb.on {{ background:#2563eb; border-color:#2563eb; color:#fff; }}
  .hint {{ font-size:12px; color:#6b7280; margin-left:4px; }}
  main {{ display:grid; grid-template-columns:minmax(0,1fr) 400px; gap:20px; padding:20px 24px; }}
  @media (max-width:1100px) {{ main {{ grid-template-columns:1fr; }} }}
  .stage {{ position:relative; align-self:start; border:1px solid #1f2430;
           border-radius:10px; overflow:hidden; background:#000; }}
  .stage img {{ display:block; width:100%; height:auto; }}
  .overlay {{ position:absolute; inset:0; }}
  .box {{ position:absolute; border:1.5px solid var(--c);
         background:color-mix(in srgb, var(--c) 12%, transparent);
         transition:background .12s, box-shadow .12s; cursor:pointer; }}
  .box.active {{ background:color-mix(in srgb, var(--c) 34%, transparent);
                box-shadow:0 0 0 2px var(--c) inset; z-index:5; }}
  .badge {{ position:absolute; top:-1px; left:-1px; padding:0 4px; font-size:10px;
           font-weight:700; background:var(--c); color:#0b0d11;
           font-variant-numeric:tabular-nums; }}
  svg.poly {{ position:absolute; inset:0; width:100%; height:100%; pointer-events:none; }}
  .panel {{ border:1px solid #1f2430; border-radius:10px; overflow:hidden;
           max-height:calc(100vh - 150px); display:flex; flex-direction:column; }}
  .panel h2 {{ margin:0; padding:11px 14px; font-size:13px; font-weight:650;
              border-bottom:1px solid #1f2430; background:#141821; }}
  .list {{ overflow:auto; }}
  .row {{ padding:9px 14px; border-bottom:1px solid #171b23; cursor:pointer;
         border-left:3px solid transparent; }}
  .row:hover {{ background:#151922; }}
  .row.active {{ background:#1a1f2b; border-left-color:var(--c); }}
  .rowhead {{ display:flex; align-items:center; gap:8px; margin-bottom:3px; }}
  .ord {{ font-variant-numeric:tabular-nums; font-weight:700; color:var(--c);
         min-width:26px; font-size:12px; }}
  .lbl {{ font-size:12px; font-weight:600; }}
  .bbox {{ margin-left:auto; font-size:11px; color:#6b7280;
          font-variant-numeric:tabular-nums; }}
  .content {{ font-size:12px; color:#9ca3af; white-space:pre-wrap; word-break:break-word; }}
  .full {{ margin-top:6px; padding-top:6px; border-top:1px dashed #262c38;
          font-size:12px; color:#cbd5e1; white-space:pre-wrap; word-break:break-word; }}
</style></head><body>
<header>
  <h1>{html.escape(title)}</h1>
  <div class="meta">原图 {int(W)}×{int(H)} · 共 {len(blocks)} 个结构化块
    · 页码 {page_index + 1}/{page_count}</div>
  <div class="chips">{chips}</div>
  {toolbar}
</header>
<main>
  <div class="stage">
    <img src="{img_uri}" alt="{html.escape(title)}">
    <div class="overlay">{''.join(boxes)}</div>
    <svg class="poly" viewBox="0 0 100 100" preserveAspectRatio="none">{''.join(polys)}</svg>
  </div>
  <div class="panel">
    <h2>结构化块（点击联动高亮）</h2>
    <div class="list">{''.join(rows)}</div>
  </div>
</main>
<script>
  const boxes = [...document.querySelectorAll('.box')];
  const rows  = [...document.querySelectorAll('.row')];
  const polys = [...document.querySelectorAll('polygon')];
  const chips = [...document.querySelectorAll('.chip.clickable')];
  const state = {{ label: null, bodyOnly: false }};

  function activate(i) {{
    boxes.forEach(e => e.classList.toggle('active', +e.dataset.i === i));
    rows.forEach(e => e.classList.toggle('active', +e.dataset.i === i));
    const r = rows.find(e => +e.dataset.i === i);
    if (r) r.scrollIntoView({{ block: 'nearest' }});
  }}

  function apply() {{
    [...boxes, ...rows].forEach(e => {{
      const okLabel = !state.label || e.dataset.label === state.label;
      const okBody  = !state.bodyOnly || e.dataset.body === '1';
      const show = okLabel && okBody;
      e.style.display = show ? '' : 'none';
      if (!show) e.classList.remove('active');
    }});
    polys.forEach(p => {{
      const okLabel = !state.label || p.dataset.label === state.label;
      p.style.display = okLabel ? '' : 'none';
    }});
    chips.forEach(c =>
      c.classList.toggle('off', !!state.label && c.dataset.label !== state.label));
    document.getElementById('f-all').classList.toggle('on', !state.label && !state.bodyOnly);
    document.getElementById('f-body').classList.toggle('on', state.bodyOnly);
  }}

  rows.forEach(e => e.addEventListener('click', () => {{
    activate(+e.dataset.i);
    const full = e.querySelector('.full');
    if (full) full.hidden = !full.hidden;
  }}));
  boxes.forEach(e => e.addEventListener('click', () => activate(+e.dataset.i)));
  chips.forEach(c => c.addEventListener('click', () => {{
    state.label = (state.label === c.dataset.label) ? null : c.dataset.label;
    apply();
  }}));
  document.getElementById('f-all').addEventListener('click', () => {{
    state.label = null; state.bodyOnly = false; apply();
  }});
  document.getElementById('f-body').addEventListener('click', () => {{
    state.bodyOnly = !state.bodyOnly; state.label = null; apply();
  }});
</script>
</body></html>"""


def render_png(image_path: Path, data: dict, out_path: Path) -> None:
    """把叠加层直接画成 PNG（便于嵌报告、发给别人、或肉眼快速核对）。"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(image_path).convert("RGBA")
    W, H = img.size
    # bbox 用的是 JSON 里的坐标系，图像尺寸可能不同，做一次缩放
    dw = float(data.get("width") or W)
    dh = float(data.get("height") or H)
    sx, sy = W / dw, H / dh

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    fs = max(13, int(min(W, H) * 0.018))
    font = None
    for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
                 r"C:\Windows\Fonts\arial.ttf"):
        try:
            font = ImageFont.truetype(cand, fs)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    lw = max(2, int(min(W, H) * 0.0028))
    for b in data.get("parsing_res_list", []):
        label = b.get("block_label", "")
        color = color_for(label)
        x1, y1, x2, y2 = (v * s for v, s in zip(b["block_bbox"], (sx, sy, sx, sy)))

        d.rectangle([x1, y1, x2, y2], fill=color + "26")        # 半透明填充
        d.rectangle([x1, y1, x2, y2], outline=color, width=lw)  # 边框

        pts = b.get("block_polygon_points")
        if pts:
            d.polygon([(p[0] * sx, p[1] * sy) for p in pts], outline=color)

        order = b.get("block_order")
        tag = f"{order} {label}" if order is not None else label
        tb = d.textbbox((0, 0), tag, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        d.rectangle([x1, y1, x1 + tw + 10, y1 + th + 8], fill=color)
        d.text((x1 + 5, y1 + 2), tag, fill="#0b0d11", font=font)

    Image.alpha_composite(img, overlay).convert("RGB").save(out_path)


def main() -> int:
    ap = argparse.ArgumentParser(description="PaddleOCR-VL 识别结果可视化")
    ap.add_argument("image", help="原图路径（做 --from-json 时用于叠加底图）")
    ap.add_argument("--from-json", default=None,
                    help="复用已有的 *_res.json，不重跑模型")
    ap.add_argument("--out", default=None, help="HTML 输出路径")
    ap.add_argument("--save-builtin", action="store_true",
                    help="同时保存内置的版面框叠加图 layout_det_res")
    ap.add_argument("--max-pixels", type=int, default=None)
    args = ap.parse_args()

    image_path = Path(args.image)
    if not image_path.exists():
        print(f"找不到图片: {image_path}", file=sys.stderr)
        return 1
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.from_json:
        data = unwrap(json.loads(Path(args.from_json).read_text(encoding="utf-8")))
        builtin_dir = None
    else:
        from paddleocr import PaddleOCRVL

        kwargs: dict = {"device": "gpu:0", "pipeline_version": "v1.6"}
        if args.max_pixels:
            kwargs["max_pixels"] = args.max_pixels
        pipeline = PaddleOCRVL(**kwargs)

        data, builtin_dir = None, OUTPUT_DIR
        for res in pipeline.predict(str(image_path)):
            res.save_to_json(save_path=str(OUTPUT_DIR))
            data = unwrap(res.json)  # 必须 unwrap，否则拿到空壳
            if args.save_builtin:
                for name, im in res.img.items():
                    p = OUTPUT_DIR / f"{image_path.stem}_{name}.png"
                    im.save(p)
                    print(f"  内置叠加图 -> {p}")
        if data is None:
            print("解析没有返回结果", file=sys.stderr)
            return 1

    # 防呆：空结果以前会静默生成一个只有图片的报告，用户看到的"只有图片没有报告"
    # 就是这么来的。宁可吵一点。
    if not data.get("parsing_res_list"):
        print("警告：结果里没有 parsing_res_list，报告会是空的。"
              "检查是不是忘了 unwrap res.json 的 {'res': ...} 包装层。",
              file=sys.stderr)

    out = Path(args.out) if args.out else OUTPUT_DIR / f"{image_path.stem}_viz.html"
    out.write_text(render_report(image_path, data, image_path.name), encoding="utf-8")
    print(f"\n可视化报告 -> {out.resolve()}")

    png_out = out.with_suffix(".png")
    render_png(image_path, data, png_out)
    print(f"叠加图 PNG -> {png_out.resolve()}")

    print("HTML 用浏览器打开：框按类别着色，角标是阅读顺序，右侧点击联动高亮。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
