#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""f3_ocr_yield.py —— 量每份文书的 **OCR 文字产出率**。

为什么需要它：106 例那批里待确认最多的那 26 个病案首页列（安吉 1911 格），
我一开始以为要「补 section_alias 别名」。实际查真实文书发现：

    籍贯 / 身份证 / 民族 / 电话 / 地址 / 科别 / 总费用 / 入院时间 / 病案号 / 性别 / 年龄
    —— 在首页正文里**连字段标签都不出现**，而 F2 的产物自己记着
    `<!-- 缺失 p1-b7：text，空内容 -->` / `<!-- 占位 p1-b3：图片（无文本） -->`。

也就是说**那些区域没有被 OCR 出任何文字** —— 补别名、改正则都救不了，
只有把 OCR 修好（方向分类 / server 版检测识别模型 / 渲染分辨率）才有用。
实测那份样本：11 个无文字块，**光病案首页 p1 就占 3 个**。

这个产出率在流水线输出里原本**完全不可见** —— 于是"某几家医院某些列全空"
看起来像规则问题，查半天其实是 OCR 没吐字。本工具把它变成一行数字，
并且已经被 `f3_batch.py` 的批跑汇总直接调用。

用法：
    python3 f3_ocr_yield.py <compiled目录 | 病例目录 | jobs目录> [--top 20] [--min-blocks 3]
"""
import argparse
import re
import sys
from pathlib import Path

MISS = re.compile(r"<!--\s*缺失\s+([^：]+)：[^>]*-->")
PLACE = re.compile(r"<!--\s*占位\s+([^：]+)：[^>]*-->")
BLOCK = re.compile(r"【(?:text|image|table|figure)\s+([^】]+)】")
PAGE = re.compile(r"p(\d+)")


def resolve(target):
    """接受 compiled.md 路径 / 病例目录 / 任意目录 → (md文件|None, sections_text.json|None)。

    两处都要读：块标记 `【text p1-b6】` 在 `sections_text.json` 里，
    而 `<!-- 缺失 -->` 注释主要在 `compiled.md` 里。少读一边就少算一半。
    """
    p = Path(target)
    if p.is_dir():
        md = p / "compiled.md"
        st = p / "sections_text.json"
        return (md if md.is_file() else None), (st if st.is_file() else None)
    if p.is_file():
        sib = p.parent / "sections_text.json"
        return p, (sib if sib.is_file() else None)
    raise FileNotFoundError(str(target))


def _read(p):
    return p.read_text(encoding="utf-8", errors="replace") if p else ""


def audit_one(target):
    """→ dict（一份文书的产出率）。`target` 可以是目录或 compiled.md。"""
    md, st = resolve(target)
    if md is None and st is None:
        raise FileNotFoundError(str(target))
    text = _read(md) + "\n" + _read(st)
    # ★ 一律**取并集/去重**：写成「compiled.md 里没有才去 sections_text.json 补」
    #   会在两边都有标记时漏掉一半（实测实得 2、真实 4）。
    miss = sorted({m.group(1).strip() for m in MISS.finditer(text)})
    place = sorted({m.group(1).strip() for m in PLACE.finditer(text)})
    # ★ 不要在 JSON 里猜结构 —— 直接在**原始文本**上数标记，结构无关。
    #   （踩过：按 obj["sections"] 取，结构一变静默取到 0，比例算成 5200%。）
    blocks = {m.group(1).strip() for m in BLOCK.finditer(text)}
    notext = sorted(set(miss) | set(place))
    pages = sorted({int(PAGE.match(b).group(1)) for b in notext if PAGE.match(b)})
    return {"case": (md.parent.name if md else Path(target).name),
            "md": str(md or ""), "chars": len(text),
            "n_block": len(blocks), "miss": len(miss), "place": len(place),
            "notext": len(notext), "pages": pages,
            "first_page_hit": 1 in pages}


def audit_many(paths):
    """对一组目录/文件逐个 audit，返回 (rows, failures)。"""
    rows, fails = [], []
    for p in paths:
        try:
            rows.append(audit_one(p))
        except Exception as e:                                 # noqa: BLE001
            fails.append((str(p), "%s: %s" % (type(e).__name__, e)))
    return rows, fails


def summarize(rows, min_blocks=3):
    """给批跑汇总用的一行结论 → (文本行列表, 统计 dict)。"""
    tot_notext = sum(r["notext"] for r in rows)
    known = [r for r in rows if r["n_block"] > 0]
    k_notext = sum(r["notext"] for r in known)
    k_block = sum(r["n_block"] for r in known)
    # ★ 比例只按**块数已知**的行算：块数取不到的行只贡献分子不贡献分母，
    #   比例必然失真，方向取决于缺多少行 —— 干脆排除，只单列绝对数。
    bad = [r for r in rows if r["notext"] >= min_blocks]
    p1 = [r for r in rows if r["first_page_hit"]]
    lines = []
    if k_block:
        lines.append("合计：无文字块 %d / 总块 %d = %.1f%%（只按块数已知的 %d 份算）"
                     % (k_notext, k_block, 100.0 * k_notext / k_block, len(known)))
    lines.append("      全部文书合计无文字块：%d（另有 %d 份取不到块总数）"
                 % (tot_notext, len(rows) - len(known)))
    lines.append("无文字块 >= %d 的文书：%d 份" % (min_blocks, len(bad)))
    if p1:
        lines.append("**病案首页（p1）就有无文字块的：%d 份** —— 这些文书的"
                     "「病案号/姓名/性别/年龄/籍贯/身份证/地址/电话」等首页字段"
                     "基本没救，因为正文里连字段标签都没有。" % len(p1))
    return lines, {"notext": tot_notext, "block": k_block,
                   "ratio": (100.0 * k_notext / k_block) if k_block else None,
                   "bad": len(bad), "p1": len(p1), "cases": len(rows)}


HOWTO = [
    "怎么办：这不是规则层能修的。按收益排序——",
    "  ① MEDREC_OCR_ORIENT=1        方向分类（安吉的内容页在像素里横躺 90°）",
    "  ② MEDREC_OCR_DET/REC=...server  密集表格换 server 版检测识别模型",
    "  ③ MEDREC_RENDER_PX           渲染分辨率（默认已提到 A4≈201 DPI）",
    "  三者都要求模型在本地缓存里，先跑 RUN-FIRST.md 的 ④.5 预检。",
]


def main(argv=None):
    ap = argparse.ArgumentParser(description="量每份文书的 OCR 文字产出率")
    ap.add_argument("root", help="compiled 目录 / 病例目录 / jobs 目录")
    ap.add_argument("--top", type=int, default=20, help="按无文字块排序列出前 N 份")
    ap.add_argument("--min-blocks", type=int, default=3, help="无文字块 >= 这个数就提示")
    a = ap.parse_args(argv)

    root = Path(a.root)
    if root.is_file():
        targets = [root]
    else:
        # 优先找 compiled.md；找不到就退回「含 sections_text.json 的目录」
        targets = sorted(root.rglob("compiled.md"))
        if not targets:
            targets = sorted(p.parent for p in root.rglob("sections_text.json"))
    if not targets:
        print("没找到 compiled.md / sections_text.json（给病例目录或 jobs 目录）",
              file=sys.stderr)
        return 2

    rows, fails = audit_many(targets)
    for p, why in fails:
        print("  [跳过] %s：%s" % (p, why), file=sys.stderr)

    print("=== OCR 文字产出率（%d 份）===" % len(rows))
    print("%-28s %7s %6s %5s %5s %6s  %s"
          % ("病例", "字数", "块", "缺失", "占位", "无文字", "涉及的页"))
    print("-" * 96)
    for r in sorted(rows, key=lambda x: -x["notext"])[:a.top]:
        print("%-28s %7d %6d %5d %5d %6d  %s"
              % (r["case"][:28], r["chars"], r["n_block"], r["miss"],
                 r["place"], r["notext"],
                 ",".join("p%d" % p for p in r["pages"][:10])))

    print()
    lines, _ = summarize(rows, a.min_blocks)
    for ln in lines:
        print(ln)
    print()
    for ln in HOWTO:
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
