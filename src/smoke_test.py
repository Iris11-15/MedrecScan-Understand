"""PaddleOCR-VL-1.6 环境自检与最小可用示例。

用法:
    python smoke_test.py                  # 只做环境自检（快，不下载模型）
    python smoke_test.py --run-ocr        # 额外跑一页官方 demo 图（首次会下载模型）
    python smoke_test.py --run-ocr path\\to\\page.png

自检覆盖三件事:
    1. Python 版本落在飞桨支持的区间 (3.8-3.12)
    2. paddle 是 GPU 构建且能真实拿到 RTX 50 系设备
    3. paddleocr 的 PaddleOCRVL 产线可导入（这才是"页级结构化解析"的正确入口）
"""

from __future__ import annotations

import argparse
import sys

DEMO_IMAGE = (
    "https://paddle-model-ecology.bj.bcebos.com/"
    "paddlex/imgs/demo_image/paddleocr_vl_demo.png"
)
OUTPUT_DIR = "output"


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def check_python() -> bool:
    section("Python")
    v = sys.version_info
    print(f"{sys.version.split()[0]}  |  {sys.executable}")
    if not (3, 8) <= (v.major, v.minor) <= (3, 12):
        print("[FAIL] 飞桨只支持 Python 3.8-3.12，当前版本不受支持。")
        return False
    print("[ OK ] 版本在支持区间内")
    return True


def check_paddle() -> bool:
    section("PaddlePaddle / GPU")
    try:
        import paddle
    except ImportError as exc:
        print(f"[FAIL] 无法导入 paddle: {exc}")
        return False

    print(f"paddle {paddle.__version__}")
    compiled_cuda = paddle.device.is_compiled_with_cuda()
    print(f"CUDA build      : {compiled_cuda}")
    if not compiled_cuda:
        print("[FAIL] 装成了 CPU 版。需要重装 cu129 的 paddlepaddle-gpu。")
        return False

    try:
        count = paddle.device.cuda.device_count()
    except Exception as exc:  # pragma: no cover - 驱动问题
        print(f"[FAIL] 查询 GPU 失败: {exc}")
        return False

    print(f"GPU count       : {count}")
    if count == 0:
        print("[FAIL] 检测不到 GPU。检查驱动是否支持 CUDA 12.9 以上。")
        return False

    print(f"current device  : {paddle.device.get_device()}")
    # 真正分配一次显存，确认不是"看得到用不了"
    try:
        t = paddle.zeros([1024, 1024], dtype="float32")
        print(f"allocated       : {t.numel() * 4 / 1024 / 1024:.1f} MB on {t.place}")
        del t
    except Exception as exc:
        print(f"[FAIL] 显存分配失败: {exc}")
        return False
    print("[ OK ] GPU 可用")
    return True


def check_pipeline() -> bool:
    section("PaddleOCR-VL 产线")
    try:
        from paddleocr import PaddleOCRVL  # noqa: F401
    except ImportError as exc:
        print(f"[FAIL] 无法导入 PaddleOCRVL: {exc}")
        print("       若是缺依赖，请确认装的是 paddleocr[doc-parser]。")
        return False
    print("[ OK ] PaddleOCRVL 可导入")
    return True


def run_ocr(image: str) -> None:
    section(f"页级解析: {image}")
    from paddleocr import PaddleOCRVL

    # 关键点：页级结构化解析走产线，不是裸 vLLM/transformers。
    # 裸 VLM 只能做元素级识别，拿不到版面结构。
    pipeline = PaddleOCRVL(pipeline_version="v1.6")
    for res in pipeline.predict(image):
        res.print()
        res.save_to_json(save_path=OUTPUT_DIR)
        res.save_to_markdown(save_path=OUTPUT_DIR)
    print(f"\n结果已写入 ./{OUTPUT_DIR}/")
    print("  *.json     页级结构化块（类别 + 内容 + bbox）")
    print("  *.md       阅读顺序还原的 Markdown")


def main() -> int:
    ap = argparse.ArgumentParser(description="PaddleOCR-VL-1.6 环境自检")
    ap.add_argument("--run-ocr", nargs="?", const=DEMO_IMAGE, default=None,
                    metavar="IMAGE", help="额外跑一页解析，可指定本地图片路径")
    args = ap.parse_args()

    results = [check_python(), check_paddle(), check_pipeline()]

    if args.run_ocr:
        if all(results):
            try:
                run_ocr(args.run_ocr)
            except Exception as exc:
                print(f"\n[FAIL] 解析失败: {type(exc).__name__}: {exc}")
                return 1
        else:
            print("\n跳过解析：上面的自检没全过。")

    section("结论")
    print("全部通过，环境可用。" if all(results) else "存在失败项，见上。")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
