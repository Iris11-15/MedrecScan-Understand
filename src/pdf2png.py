#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF -> 逐页 PNG（给 layered.py 做批量识别用）。

为什么需要它
    `layered.py` 的 `process()` 是 `Image.open(path)` —— PIL 读不了 PDF，
    所以把一个装着 PDF 的目录丢给它，会在第一个 PDF 上就失败。
    PDF 拆页必须在前面做掉，本文件就是做这件事。

**自包含**：不 import 项目里任何模块，只需要 `pypdfium2`。
（拆页的三重缩放约束原本在 `webui.py` 里，这里内联，免得整条 CLI 链路
 绑在一个已经不需要跑的网页服务上。）

输出布局（每个 PDF 一个子目录）
    pages/<pdf 去后缀>/page_0001.png
    pages/<pdf 去后缀>/page_0002.png ...
    每个 PDF 一个子目录 —— 这样也顺带解决了 `layered.py` 用 `f.stem` 命名产物时
    「不同目录下的同名文件互相覆盖」的问题。

用法（用 vl-env 的解释器，pypdfium2 装在那里）
    PY=/home/ubuntu/anaconda3/envs/vl-env/bin/python
    $PY pdf2png.py a.pdf   --out pages/
    $PY pdf2png.py pdfs/   --out pages/ --max-pages 50
    $PY pdf2png.py pdfs/   --out pages/ --quiet      # 已存在的页会跳过（可续跑）

摆正（扫描件方向）
    默认**一点没变**（不转）。要转用命令行或环境变量，见下面 ROTATE_SPEC 的实测依据：
    $PY pdf2png.py 安吉/ --out pages/ --rotate 180
    MEDREC_ROTATE="安吉=180,桐乡二院=90" $PY pdf2png.py pdfs/ --out pages/
    ★ 逐页混合方向的医院，固定角度解决不了 —— 那条路要走 OCR 侧的方向模型
      （`MEDREC_OCR_ORIENT=1`，见 layered.py 的 PP-LCNet_x1_0_doc_ori）。
"""
from __future__ import annotations

import argparse
import re
import sys
from io import BytesIO
from pathlib import Path

# 三重缩放约束（缺一个都会出问题，见下面的注释）
#
# ★ 2026-09-28 实测校正：默认值原来会**丢掉源扫描件已经有的分辨率**。
#   实测那 106 份（桌面「人工扫描病历测试/测试」）的嵌入图有效 DPI：
#       奉化 400 · 长兴人民 400 · 安吉 200 · 桐乡二院 200 · 玉环第一 200
#   而 TARGET_PX=2e6 对 A4 只有 **约 144 DPI**（原注释写"约 170dpi"是算错了：
#   A4 501k pt² → sqrt(2e6/501k)=2.0 → 1190×1684 px → 1190/8.27in = 144 DPI）。
#   中文密集表格 OCR 的底线是 200 DPI、常规要 300 —— 我们在渲染这一步就先丢一半。
#   现在默认保住 **200 DPI**（≈3.9M px/页，正好对上较弱那三家的源件）。
#   代价：像素约为原来的 2 倍，检测+识别时间会相应变长。
#   嫌慢就回退：MEDREC_RENDER_PX=2000000（A4 ≈144 DPI，旧行为）。
import os as _os

TARGET_PX = int(_os.environ.get("MEDREC_RENDER_PX", 3_900_000))   # A4 ≈ 200 DPI
MAX_SIDE_PT = 3000.0       # 渲染后最长边上限（pt），防止宽扁页面被按面积放大
# 放大倍数上限：A4 到 200 DPI 需要 scale≈2.79，所以上限必须 ≥3 才吃得到 TARGET_PX
MAX_UPSCALE = float(_os.environ.get("MEDREC_MAX_UPSCALE", 3.0))
MIN_SCALE = 0.5
# 空白页直接跳过，不送去 OCR。
# 实测安吉 20 份里 **48% 的页是空白**（而且严格交替 —— 单面原件做了双面扫描），
# 其他四家 0%。不跳的话白烧近一半的检测/识别时间，空白页的噪点还容易让
# 页眉/页脚检测器误触发。要保留就 --keep-blank-pages。
SKIP_BLANK = _os.environ.get("MEDREC_KEEP_BLANK", "") not in ("1", "true", "yes")
INK_MIN = 0.003            # 墨迹占比低于此值算空白页

# ---------------------------------------------------------------------------
# ★ 摆正（方向）：顺时针度数。**默认 0 = 行为一点不变**，要转必须显式开。
#
#   为什么需要它：桌面那 106 份里，方向**不是**统一的正立。
#   判据不再靠眼睛 —— 用 Windows 自带的离线 OCR（WinRT）当裁判，把同一页在
#   0 / 顺90 / 逆90 / 180 各识别一次，取**识别出的汉字数**最多的方向
#   （审计脚本 `_orient_audit.py`，明细 `_anji/orient_audit.json`）。
#   人眼在缩略图上会把 180 看成 90：安吉 p4 我先前肉眼判"顺时针90"，
#   OCR 打分是 0=286(乱码) / 顺90=62 / 逆90=92 / **180=834**。
#
#   实测结论（抽样 4 页/家 + 安吉全例）：
#       奉化 / 桐乡二院 / 玉环第一 / 长兴人民 → **0**（正立得分是次优的 2~4 倍）
#       安吉 → **页级混合**：同一份 PDF 里 p1 要逆90、p2 要顺90、p4 要 180
#   ⇒ 固定角度只服务"整家同向"的医院；页级混合必须靠 OCR 侧的逐页方向模型
#     （`MEDREC_OCR_ORIENT=1` → PP-LCNet_x1_0_doc_ori，见 layered.py）。
#     本开关是"那台服务器没有方向模型 / 重扫之后要显式纠正"时的确定性兜底。
#
#   配置：MEDREC_ROTATE="安吉=180,桐乡二院=90"（按输入路径里的**目录名**匹配，
#         就近优先），通用兜底写 "*=180" 或 MEDREC_ROTATE_DEFAULT=180。
#         命令行 `--rotate N` 优先级最高（忽略目录名，全部按 N 转）。
#   ★ 值非法一律**报错退出**，不静默当成 0 —— 静默不转正是最难发现的那类错。
ROTATE_SPEC = _os.environ.get("MEDREC_ROTATE", "")
ROTATE_DEFAULT = 0
_ROT_CACHE = None

# 用 transpose 而不是 rotate()：transpose 是像素搬运，无损、不插值。
# （Image.ROTATE_270 就是"逆时针 270°" = 顺时针 90°。）
_TRANSPOSE = {90: "ROTATE_270", 180: "ROTATE_180", 270: "ROTATE_90"}

PDF_EXT = {".pdf"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


def ink_ratio(pil) -> float:
    """墨迹占比（灰度 < 200 的像素比例）。用 PIL 直方图，不引入 numpy。"""
    g = pil.convert("L")
    hist = g.histogram()
    return sum(hist[:200]) / float(g.width * g.height or 1)


# ---------------------------------------------------------------------------
# 摆正：解析 / 匹配 / 应用（纯函数，便于单测，见 test_pdf2png.test_rotate）
# ---------------------------------------------------------------------------
def norm_deg(v) -> int:
    """归一化到 0/90/180/270。非法值**抛异常**（绝不静默当 0）。"""
    try:
        d = int(str(v).strip()) % 360
    except (TypeError, ValueError):
        raise ValueError("必须是 0/90/180/270（-90、270 这类等价写法也行），收到 %r" % (v,))
    if d not in (0, 90, 180, 270):
        raise ValueError("必须是 90 的整数倍，收到 %r" % (v,))
    return d


def parse_rotate(spec: str) -> dict:
    """"安吉=180,桐乡二院=90;*=0" -> {"安吉":180,"桐乡二院":90,"*":0}。

    键统一 casefold，这样 `anji=180` 也能匹配到目录名 `Anji`（中文不受影响）。
    """
    out: dict[str, int] = {}
    for part in re.split(r"[,;]", spec or ""):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError('MEDREC_ROTATE 每项要写成 目录名=度数，这一项是 %r' % part)
        k, _, v = part.partition("=")
        k = k.strip()
        if not k:
            raise ValueError("MEDREC_ROTATE 的目录名不能为空：%r" % part)
        out[k.casefold()] = norm_deg(v)
    return out


def rotate_map() -> dict:
    """生效中的方向表（含 MEDREC_ROTATE_DEFAULT 兜底），解析一次后缓存。"""
    global _ROT_CACHE
    if _ROT_CACHE is None:
        m = parse_rotate(ROTATE_SPEC)
        d = _os.environ.get("MEDREC_ROTATE_DEFAULT", "")
        if str(d).strip():
            m["*"] = norm_deg(d)
        _ROT_CACHE = m
    return _ROT_CACHE


def rotate_for(path, cli_deg=None, mapping=None) -> int:
    """这一份输入该顺时针转几度：命令行 > 最近的祖先目录名 > "*" 兜底 > 0。"""
    if cli_deg is not None:
        return norm_deg(cli_deg)
    m = rotate_map() if mapping is None else mapping
    p = Path(path)
    names = [p.stem] + [q.name for q in p.parents]      # 由近到远
    for nm in names:
        hit = m.get(nm.casefold())
        if hit is not None:
            return hit
    return m.get("*", ROTATE_DEFAULT)


def apply_rotate(pil, deg: int):
    """按顺时针 deg 度摆正。transpose = 无损搬运，不重采样。"""
    if not deg:
        return pil
    from PIL import Image                               # 延迟导入，跟 ink_ratio 的取用方式一致
    tr = getattr(Image, "Transpose", Image)             # Pillow ≥9.1 的枚举写法
    return pil.transpose(getattr(tr, _TRANSPOSE[deg]))


def render_pages(raw: bytes, target_px: int = TARGET_PX, max_pages: int | None = None,
                 skip_blank: bool | None = None, stats: dict | None = None,
                 rotate: int = 0):
    """逐页渲染，yield (序号, 总页数, PNG 字节, (宽, 高))。

    用生成器而不是先全渲染：上百页的 PDF 全渲进内存太浪费。

    `skip_blank` 为真时**空白页不 yield**（见模块顶部 SKIP_BLANK 的实测依据）。
    yield 的序号仍是**源页号**，所以跳页后文件名会有间隔 —— 幂等逻辑按文件名判，
    不受影响；`stats["blank"]` 记下跳了几页，方便和源页数对账。

    `rotate`：顺时针度数（0/90/180/270），在**判空白之前**摆正，见 ROTATE_SPEC。
    """
    import pypdfium2 as pdfium                      # 延迟导入，缺了能给友好提示

    if skip_blank is None:
        skip_blank = SKIP_BLANK
    if stats is None:
        stats = {}
    stats.setdefault("blank", 0)
    rotate = norm_deg(rotate) if rotate else 0

    doc = pdfium.PdfDocument(raw)
    total = len(doc)
    if max_pages:
        total = min(total, max_pages)

    for i in range(total):
        page = doc[i]
        w, h = page.get_size()                      # 单位 pt（1/72 英寸）
        #   1) 按目标像素算缩放
        #   2) 限制最长边 —— 否则宽扁页面（如一条横幅）会被按面积放大到离谱尺寸
        #      （实测：一张 1365x120 的条状 PDF 被放大到 4777x420，单页识别要几分钟）
        #   3) 限制放大倍数 —— 见 MAX_UPSCALE 的注释
        scale = (target_px / max(1.0, w * h)) ** 0.5
        scale = min(scale, MAX_SIDE_PT / max(1.0, w, h), MAX_UPSCALE)
        scale = max(MIN_SCALE, scale)
        pil = page.render(scale=scale).to_pil()
        pil = apply_rotate(pil, rotate)
        if skip_blank:
            try:
                if ink_ratio(pil) < INK_MIN:
                    stats["blank"] += 1
                    continue                    # 空白页不落盘、不送 OCR
            except Exception:                   # noqa: BLE001  判不出来就别跳
                pass
        buf = BytesIO()
        pil.save(buf, format="PNG")
        yield i, total, buf.getvalue(), (pil.width, pil.height)


def collect(paths: list[str]) -> tuple[list[Path], list[Path]]:
    pdfs: list[Path] = []
    imgs: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.suffix.lower() in PDF_EXT:
                    pdfs.append(f)
                elif f.suffix.lower() in IMG_EXT:
                    imgs.append(f)
        elif p.exists():
            (pdfs if p.suffix.lower() in PDF_EXT else imgs).append(p)
        else:
            print("跳过（不存在）: %s" % p, file=sys.stderr)
    return pdfs, imgs


def _max_page(sub) -> int:
    """目录里已有的最大页号（空目录 → 0）。"""
    n = 0
    for p in sub.glob("page_*.png"):
        try:
            n = max(n, int(p.stem.split("_")[1]))
        except (IndexError, ValueError):
            pass
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PDF -> 逐页 PNG（自包含，只需 pypdfium2）")
    ap.add_argument("inputs", nargs="+", help="PDF 文件或所在目录")
    ap.add_argument("--out", required=True, help="输出根目录")
    ap.add_argument("--max-pages", type=int, default=None, help="每个 PDF 最多渲染几页")
    ap.add_argument("--target-px", type=int, default=TARGET_PX,
                    help="单页目标像素（默认 %d，A4 约 200 DPI；可用 MEDREC_RENDER_PX 覆盖）" % TARGET_PX)
    ap.add_argument("--keep-blank-pages", action="store_true",
                    help="保留空白页（默认跳过；实测安吉 48%% 的页是空白）")
    # ★ 2026-10-03：**每个 PDF = 一份独立病历**，所以"整份 PDF 渲染出 0 页"
    #   不能只是跳过 —— 那等于把一份病历从登记表里抹掉，而且不报错。
    #   实测就是这样丢的：106 份 PDF 只产出 104 行，丢的那 2 份全是空白页
    #   （安吉奇数页无信息、偶数页非正向，OCR 拿不到字）。
    #   现在默认保住这一例（留首页 + 记账），下游会整行落「待确认」——
    #   那是**诚实的空**，而不是"这例从来没存在过"。
    ap.add_argument("--drop-empty-case", action="store_true",
                    help="整份 PDF 渲染出 0 页时**丢弃该例**（默认保住：留首页并记账）")
    # ★ 把**多个 PDF 的页并进同一个病例目录**。
    #   真实数据里一份病历常是一叠扫描件（实测某病例 6 个 PDF），而
    #   `layered.py` 用 `f"{页文件名}_{suffix}.json"` 命名产物 ——
    #   页号必须在病例内**连续**，否则第二份 PDF 的 page_0001 会盖掉第一份的。
    #   所以 `--as-name` 不只是改名，它同时把页号接在已有页之后。
    ap.add_argument("--as-name", default=None,
                    help="输出到 <out>/<这个名字>/（而不是 <out>/<pdf 去后缀>/），"
                         "页号接在已有页之后 —— 用于「一份病历多份扫描件」")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--rotate", type=int, default=None, metavar="DEG",
                    help="把每页顺时针转 DEG 度（0/90/180/270）。不传则看 MEDREC_ROTATE "
                         "（按输入路径里的目录名匹配，如 MEDREC_ROTATE='安吉=180'）")
    a = ap.parse_args(argv)

    # 方向表先解析一次：写错了要**在这里就报错退出**，不能等转出来才发现没转
    try:
        cli_rot = None if a.rotate is None else norm_deg(a.rotate)
        rmap = rotate_map()
    except ValueError as e:
        print("[x] 方向配置有问题：%s" % e, file=sys.stderr)
        return 2
    if not a.quiet:
        if cli_rot is not None:
            print("方向：全部顺时针 %d°（--rotate）" % cli_rot)
        elif rmap:
            print("方向表：%s（按输入路径里的目录名匹配）"
                  % ", ".join("%s=%d°" % (k, v) for k, v in sorted(rmap.items())))

    try:
        import pypdfium2                              # noqa: F401
    except ImportError:
        print("[x] 这个解释器里没有 pypdfium2。\n"
              "    它是跟 paddlex[serving] 一起装进 vl-env 的，用那个解释器跑：\n"
              "      /home/ubuntu/anaconda3/envs/vl-env/bin/python %s ...\n"
              "    或自行安装：<python> -m pip install pypdfium2"
              % Path(__file__).name, file=sys.stderr)
        return 2

    pdfs, imgs = collect(a.inputs)
    if imgs and not a.quiet:
        print("提示：有 %d 个图片输入，不需要拆页，可直接喂给 layered.py。" % len(imgs))
    if not pdfs:
        print("没有 PDF 需要拆页。", file=sys.stderr)
        return 0

    out_root = Path(a.out)

    # ★★ 合并模式（一份病历 = 一叠扫描件）：**先把所有页收齐，再整体替换**。
    #
    #   为什么不能「算 base、往后追加」—— 那样编号取决于**目录里已有什么**，
    #   而不是**输入是什么**，于是重跑不幂等。实测第一次 4 页、重跑变 8 页。
    #   在 batch_run.sh 里这会真发生：某例 6 份扫描件跑到第 3 份失败，
    #   重跑就翻倍 —— 而且翻倍不报错，只会让这例的内容变成两份。
    #
    #   现在的语义：编号只由「输入顺序 + 每份的页序」决定 → 天然幂等。
    #   任何一份 PDF 渲染失败 → **整例不落盘**（返回非零），重跑从头来。
    #   宁可这一例没结果，也不要一例残缺的页。
    if a.as_name:
        if imgs:
            print("[!] --as-name 模式会忽略 %d 个图片输入：%s"
                  % (len(imgs), ", ".join(i.name for i in imgs)), file=sys.stderr)
        collected = []                               # (来源名, 页序号, png, size)
        for pdf in pdfs:
            rot = rotate_for(pdf, cli_rot, rmap)
            if rot:
                print("  [方向] %s：顺时针 %d°" % (pdf.name, rot))
            try:
                for _idx, _tot, png, size in render_pages(
                        pdf.read_bytes(), target_px=a.target_px,
                        max_pages=a.max_pages, skip_blank=not a.keep_blank_pages,
                        rotate=rot):
                    collected.append((pdf.name, _idx, png, size))
            except Exception as e:                   # noqa: BLE001
                print("[x] %s 拆页失败：%s: %s —— **该病例整例不落盘**，"
                      "修好后重跑即可" % (pdf.name, type(e).__name__, e),
                      file=sys.stderr)
                return 3
        sub = out_root / a.as_name
        sub.mkdir(parents=True, exist_ok=True)
        # 收齐了才动盘：清掉旧的（可能是上一轮残留、或页数已经变了）
        old = sorted(sub.glob("page_*.png"))
        for p in old:
            p.unlink()
        for n, (_src, _i, png, _sz) in enumerate(collected, 1):
            (sub / ("page_%04d.png" % n)).write_bytes(png)
        if not a.quiet:
            print("  -> %s：%d 页（%d 份扫描件合并，替换掉旧的 %d 页）\n"
                  % (sub, len(collected), len(pdfs), len(old)))
        return 0 if collected else 1

    total_pages = skipped = 0
    bad = 0
    zero_page: list[str] = []          # 整份渲染出 0 页的 PDF —— 必须报出来
    for pdf in pdfs:
        # 默认模式保持原样（每份 PDF 一个目录、各自从 1 开始，
        # 靠 dst.exists() 跳过已存在的页）—— 改了会让历史批次目录结构变样
        sub = out_root / pdf.stem
        sub.mkdir(parents=True, exist_ok=True)
        base = 0
        rot = rotate_for(pdf, cli_rot, rmap)
        if rot:
            print("  [方向] %s：顺时针 %d°" % (pdf.name, rot))
        try:
            n = 0
            for idx, total, png, size in render_pages(pdf.read_bytes(),
                                                      target_px=a.target_px,
                                                      max_pages=a.max_pages,
                                                      skip_blank=not a.keep_blank_pages,
                                                      rotate=rot):
                dst = sub / ("page_%04d.png" % (base + idx + 1))
                n += 1
                if dst.exists():
                    skipped += 1
                    continue
                dst.write_bytes(png)
                if not a.quiet:
                    print("  [%d/%d] %s  %dx%d  %d KB"
                          % (idx + 1, total, dst.name, size[0], size[1], len(png) // 1024))
            total_pages += n
            if not a.quiet:
                print("  -> %s：%d 页\n" % (sub, n))
        except Exception as e:                        # noqa: BLE001
            print("[x] %s 拆页失败：%s: %s" % (pdf.name, type(e).__name__, e), file=sys.stderr)
            bad += 1

        if n == 0:
            zero_page.append(pdf.name)
            if a.drop_empty_case:
                print("[!] %s：整份 0 页（--drop-empty-case），这一例被丢弃"
                      % pdf.name, file=sys.stderr)
            else:
                # 保住这一例：不判空白，取第一页落盘。下游会整行「待确认」，
                # 而不是让这例从登记表里消失。
                try:
                    for _i, _t, png, _sz in render_pages(
                            pdf.read_bytes(), target_px=a.target_px,
                            max_pages=a.max_pages, skip_blank=False, rotate=rot):
                        (sub / "page_0001.png").write_bytes(png)
                        total_pages += 1
                        break
                    (sub / ".empty_case").write_text(
                        "整份 PDF 判为空白（安吉类：奇数页无信息 / 偶数页非正向）。"
                        "已保住首页，下游整行落「待确认」。\n", encoding="utf-8")
                    print("[!] %s：整份判为空白 —— 已保住首页并标记 .empty_case，"
                          "这一例在登记表里会整行「待确认」（不静默丢弃）"
                          % pdf.name, file=sys.stderr)
                except Exception as e:                # noqa: BLE001
                    print("[x] %s：0 页且连首页都渲染不出来：%s: %s"
                          % (pdf.name, type(e).__name__, e), file=sys.stderr)
                    bad += 1

    if zero_page and not a.drop_empty_case:
        print("\n⚠️ **%d 份 PDF 整份判为空白**（已保住首页，落 .empty_case 标记）："
              % len(zero_page), file=sys.stderr)
        for nm in zero_page:
            print("     · %s" % nm, file=sys.stderr)
        print("   → 这些例在登记表里会是整行「待确认」。真要出字得修 OCR："
              "export MEDREC_OCR_ORIENT=1（安吉必需，方向是逐页混合的）",
              file=sys.stderr)

    if not a.quiet:
        print("== 完成：%d 个 PDF / %d 页（跳过已存在 %d 页），失败 %d 个"
              % (len(pdfs), total_pages, skipped, bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
