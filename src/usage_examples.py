"""PaddleOCR-VL-1.6 使用范例。

API 依据本机实测的 paddleocr 3.7.0 / paddlex 3.7.2，不是照抄模型卡。

运行（注意 PowerShell 里要加 &）：
    & "D:\\visonalbe\\.venv_paddleocr\\python.exe" usage_examples.py page.png
    & "D:\\visonalbe\\.venv_paddleocr\\python.exe" usage_examples.py page.png --level 2

三个层次，按需取用：
    1  页级解析   -> 结构化 JSON + Markdown + Excel（最常用）
    2  只消费块   -> 跳过 Markdown，直接读 parsing_res_list
    3  元素级识别 -> 只要某一种元素（表格 / 公式 / 印章），走 transformers
"""

from __future__ import annotations

import argparse
import json
import sys

OUTPUT_DIR = "output"

# 阅读顺序会被置为 None 的标签（页眉页脚、图、表等不作为正文流的一部分）
SKIP_ORDER_LABELS = {
    "figure_title", "vision_footnote", "image", "chart", "table",
    "header", "header_image", "footer", "footer_image", "footnote", "aside_text",
}

# 元素级识别用的 prompt（走 transformers 那条路时才需要）
ELEMENT_PROMPTS = {
    "ocr": "OCR:",
    "table": "Table Recognition:",
    "formula": "Formula Recognition:",
    "chart": "Chart Recognition:",
    "spotting": "Spotting:",
    "seal": "Seal Recognition:",
}


def build_pipeline(max_pixels: int | None = None, **overrides):
    """创建一个页级解析产线。

    8GB 显存（RTX 5060 Laptop）够用：0.9B 权重 bf16 约 1.8 GB，
    剩下的显存主要被页面图像 token 占用，所以 max_pixels 是关键旋钮。

    pipeline_version 默认就是 'v1.6'，不必显式传。
    """
    from paddleocr import PaddleOCRVL

    kwargs: dict = {"device": "gpu:0", "pipeline_version": "v1.6"}
    if max_pixels is not None:
        kwargs["max_pixels"] = max_pixels
    kwargs.update(overrides)
    return PaddleOCRVL(**kwargs)


# --------------------------------------------------------------------------- 1
def level1_page_parse(image: str) -> None:
    """页级解析：一次拿到 JSON / Markdown / Excel 三种产物。"""
    print(f"\n{'=' * 70}\n[层次 1] 页级解析: {image}\n{'=' * 70}")

    pipeline = build_pipeline()
    for res in pipeline.predict(image):
        res.print()                                  # 控制台预览
        res.save_to_json(save_path=OUTPUT_DIR)       # *_res.json  结构化块
        res.save_to_markdown(save_path=OUTPUT_DIR)   # *.md        阅读顺序还原
        res.save_to_xlsx(save_path=OUTPUT_DIR)       # *.xlsx      表格进 Excel
        # 其他可用导出：
        #   res.save_to_html / save_to_word / save_to_latex / save_to_csv / save_to_img
        #   res.json / res.markdown  取内存里的对象，不落盘

    print(f"\n产物已写入 ./{OUTPUT_DIR}/")


# --------------------------------------------------------------------------- 2
def level2_consume_blocks(image: str) -> None:
    """只读结构化块：不落盘，直接消费 parsing_res_list。

    这才是「结构化识别」的实际形态——每个块带类别、内容、坐标、阅读序号。
    """
    print(f"\n{'=' * 70}\n[层次 2] 结构化块: {image}\n{'=' * 70}")

    pipeline = build_pipeline()
    for res in pipeline.predict(image):
        data = res.json
        print(f"页面 {data['page_index'] + 1}/{data['page_count']}  "
              f"{data['width']}x{data['height']}")

        for block in data["parsing_res_list"]:
            label = block["block_label"]
            bbox = block["block_bbox"]
            order = block["block_order"]
            content = (block["block_content"] or "").replace("\n", " ")[:60]

            tag = f"#{order}" if order is not None else "  -"
            print(f"  {tag:>4}  {label:<16} {str(bbox):<28} {content}")

        # 典型下游用法：按类别取数
        tables = [b for b in data["parsing_res_list"] if b["block_label"] == "table"]
        formulas = [b for b in data["parsing_res_list"] if b["block_label"] == "formula"]
        texts = [b for b in data["parsing_res_list"] if b["block_label"] == "text"]
        print(f"\n  统计: 文本 {len(texts)} / 表格 {len(tables)} / 公式 {len(formulas)}")

        # 按阅读顺序拼回纯文本（比 Markdown 更好接业务逻辑）
        ordered = sorted(
            (b for b in data["parsing_res_list"] if b["block_order"] is not None),
            key=lambda b: b["block_order"],
        )
        plain = "\n".join(b["block_content"] for b in ordered)
        print(f"  按阅读顺序拼回正文: {len(plain)} 字符")

        # 关键信息抽取示例：靠 bbox 做字段定位
        print("\n  块级 JSON 样例:")
        print(json.dumps(data["parsing_res_list"][0], ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- 3
def level3_element(image: str, task: str = "table") -> None:
    """元素级识别：只识别一种元素，走 transformers（不支持页级）。

    官方明确说明这条路只做元素级识别和 text spotting，
    拿不到版面结构。只有在「我已经切好了表格图，只要结构」时才用它。
    """
    print(f"\n{'=' * 70}\n[层次 3] 元素级识别 ({task}): {image}\n{'=' * 70}")

    from PIL import Image
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    model_path = "PaddlePaddle/PaddleOCR-VL-1.6"
    image_obj = Image.open(image).convert("RGB")

    # spotting 需要更高分辨率 + 小图先上采样，官方给的预处理规则
    if task == "spotting":
        w, h = image_obj.size
        if w < 1500 and h < 1500:
            image_obj = image_obj.resize((w * 2, h * 2), Image.Resampling.LANCZOS)
        max_pixels = 2048 * 28 * 28
    else:
        max_pixels = 1280 * 28 * 28

    processor = AutoProcessor.from_pretrained(model_path)
    model = (
        AutoModelForImageTextToText.from_pretrained(
            model_path, torch_dtype=torch.bfloat16
        )
        .to("cuda")
        .eval()
    )

    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "image": image_obj},
            {"type": "text", "text": ELEMENT_PROMPTS[task]},
        ],
    }]
    inputs = processor.apply_chat_template(
        messages, add_generation_prompt=True, tokenize=True,
        return_dict=True, return_tensors="pt",
        images_kwargs={"size": {
            "shortest_edge": processor.image_processor.min_pixels,
            "longest_edge": max_pixels,
        }},
    ).to(model.device)

    outputs = model.generate(**inputs, max_new_tokens=1024)
    print(processor.decode(outputs[0][inputs["input_ids"].shape[-1]:-1]))


def main() -> int:
    ap = argparse.ArgumentParser(description="PaddleOCR-VL-1.6 使用范例")
    ap.add_argument("image", help="图片路径或 URL（也支持 PDF）")
    ap.add_argument("--level", type=int, default=1, choices=[1, 2, 3],
                    help="1=页级解析 2=结构化块 3=元素级识别")
    ap.add_argument("--task", default="table", choices=list(ELEMENT_PROMPTS),
                    help="仅 --level 3 有效")
    ap.add_argument("--max-pixels", type=int, default=None,
                    help="送进 VLM 的像素上限；默认约 100 万，密集页面可调高")
    args = ap.parse_args()

    if args.level == 1:
        level1_page_parse(args.image)
    elif args.level == 2:
        level2_consume_blocks(args.image)
    else:
        level3_element(args.image, args.task)
    return 0


if __name__ == "__main__":
    sys.exit(main())
