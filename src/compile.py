# -*- coding: utf-8 -*-
"""F2 编译层（F2.1–F2.4）：blocks.json -> compiled/*，纯代码零模型。

产物（数据契约见 functional-spec.md 第八节）：
  compiled.md            给模型：有序、无坐标、缺块留锚点
  compiled.json          给代码：块 ID -> 文本 + 坐标索引
  tables.json            表格矩阵（已展开合并单元格）
  sections.json          章节切分（章节 -> 块 ID）
  sections_text.json     ★ 按章节渲染好的「送模文本」，供 F3 直接使用
  compile_report.json    缺失率 / 排序警告 / token 对比

★ 为什么新增 sections_text.json（纯附加产物，五个既有产物一个都不动）

  F3 原先从 compiled.json 重新拼装送模文本，靠 block_id 做**全局查表**
  （structure.py: idx = {b["block_id"]: b for b in compiled["pages"]}）。
  而 block_id 是 to_pages.py 用 enumerate 生成的**页内下标**，--merge 合并多页时
  从不重新编号 —— 于是同一个 block_id 在 40 页里出现 40 次，全局查表只留下最后一页
  的那一块。结果：任何一页的章节文本都会被渲染成**最后一页同号块的内容**，
  且同一 section 内反复出现同一个 bid 时内容还会重复。
  单页文档看不出这个问题（demo 夹具就是单页），所以一直没暴露。

  第二个问题：缺失/占位/截断块在 compiled.json 里 content 是空串，
  F3 的 build_chunks 遇到空文本直接 continue —— 于是 compiled.md 里那些
  「缺失必须可见（F2.3）」的锚点**永远到不了模型**，模型只会以为原文本来就没有。

  本文件由 build_page **在自己走页时同步产出**，全程不经任何跨页查表，
  所以这两个问题结构上都不存在。它也不改变 compiled.md 的任何字节。

设计要点：
  * 坐标**只进 compiled.json**，绝不进 compiled.md（F2.4：几何占原始 token 20%）；
  * 每个空块/截断块在 compiled.md 里都留显式锚点（F2.3：缺失必须可见）；
  * 表格只留一行锚点，矩阵进 tables.json（F2.2）；
  * 语言 I/O 一律走 textio（UTF-8 无 BOM / ensure_ascii=False）。

夹具实测（jobs/demo，1 页 31 块）：4 个文字栏、跨栏块 [0,2,3]、
compiled.md 2710 字符 = 原始 17001 的 15.9%、缺失 3 个全部有锚点、汉字 2212 无损失。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import order  # noqa: E402
import tables  # noqa: E402
import textio  # noqa: E402
from order import Block  # noqa: E402

# 直接丢弃的噪声标签（页眉页脚页码）
NOISE_LABELS = {"header", "header_image", "footer", "footer_image", "number"}
# 纯占位标签（内容本身就是图，无可读文本）
PLACEHOLDER_LABELS = {"image"}
# 标题类标签 -> Markdown 级别
TITLE_LEVEL = {"doc_title": "#", "title": "#", "paragraph_title": "##",
               "section_title": "##", "vision_footnote": ">", "footnote": ">"}

# 章节切分（F2.5）：章节名 -> 中文标题正则
SECTION_RULES = (
    ("主诉", r"^\s*主\s*诉"),
    ("现病史", r"^\s*现\s*病\s*史"),
    ("既往史", r"^\s*(既\s*往\s*史|过\s*去\s*史|既往)"),
    ("个人史", r"^\s*个\s*人\s*史"),
    ("家族史", r"^\s*家\s*族\s*史"),
    ("查体", r"^\s*(查\s*体|体格检查|入院查体|专科查体)"),
    ("辅助检查", r"^\s*(辅助检查|实验室检查|影像学检查|检查)"),
    ("诊断", r"^\s*(诊\s*断|出院诊断|入院诊断|主要诊断)"),
    ("医嘱", r"^\s*(医\s*嘱|长期医嘱|临时医嘱)"),
    ("出院带药", r"^\s*出\s*院\s*带\s*药"),
    ("手术", r"^\s*(手\s*术|手术记录)"),
)
MISSING_RATIO_ALERT = 0.05          # 缺失率告警阈值


def _reason(b: dict, route: str, reason: str) -> str:
    return reason or b.get("reason") or f"route={route}"


def render_grid(t: dict) -> str:
    """表格对象 -> 网格文本（表头 + 数据行 + 异常项）。

    与 structure.py 的 render_table() **同形同序**，这是刻意的：切到
    sections_text.json 之后，送模文本里的表格部分要和原来逐字一致，
    唯一的区别只是「现在表格矩阵真的会送到模型，而不是只剩一行锚点」。
    """
    out: list[str] = []
    hdr = t.get("header") or []
    if hdr:
        out.append(" | ".join(str(c) for c in hdr))
    skip = t.get("header_rows") or 0
    for r in (t.get("rows") or [])[skip:]:
        out.append(" | ".join(str(c) for c in r))
    ab = t.get("abnormal") or []
    if ab:
        out.append("异常项：" + "; ".join(str(x) for x in ab[:20]))
    return "\n".join(out)


def build_page(page: dict, page_no: int) -> dict:
    """编译单页：返回 {md_lines, blocks, tables, missing, truncated, sections}。"""
    # ① 权威排序（唯一权威值；下游只读不排）
    meta = order.apply_order(page)
    blocks = page.get("parsing_res_list") or []
    W = page.get("width") or 1

    md: list[str] = []
    index: list[dict] = []
    table_objs: list[dict] = []
    missing: list[dict] = []
    truncated: list[dict] = []
    sections: dict[str, list] = {}
    cur_section = "_其他"

    # ★ 新增：按章节分组的「送模文本」与块引用。只增不减，不参与 md 的组装。
    sec_lines: dict[str, list] = {}
    sec_refs: dict[str, list] = {}

    def emit(md_line: str, sec_body: str | None = None) -> None:
        """一行内容同时进 md（compiled.md）与 sec_lines（sections_text.json）。

        md_line  -> compiled.md，**逐字节与改动前一致**（顺序、内容都不变）
        sec_body -> 送模文本的正文；None 表示与 md_line 相同（表格除外：
                    表格在 compiled.md 里只留锚点，在送模文本里要给完整矩阵）
        前缀 【label p{页}-b{块}】 带页码 —— block_id 是页内下标，不带页号就无法区分。
        """
        md.append(md_line)
        body = md_line if sec_body is None else sec_body
        sec_lines.setdefault(cur_section, []).append(
            "【%s p%d-b%s】\n%s" % (label, page_no, bid, body))
        sec_refs.setdefault(cur_section, []).append("p%d-b%s" % (page_no, bid))

    for b in blocks:
        bid = b.get("block_id")
        label = (b.get("block_label") or "").strip()
        route = (b.get("route") or "").strip()
        raw = b.get("block_content") or b.get("text") or ""
        text = textio.normalize(raw).strip()
        bbox = b.get("block_bbox") or []
        bbox = [float(v) for v in bbox[:4]] if len(bbox) >= 4 else []

        # 坐标索引：只给代码用（F2.4）
        index.append({"block_id": bid, "page": page_no, "label": label,
                      "route": route, "truncated": bool(b.get("truncated")),
                      "bbox": bbox,
                      "x1": bbox[0] if bbox else None, "y1": bbox[1] if bbox else None,
                      "x2": bbox[2] if bbox else None, "y2": bbox[3] if bbox else None,
                      "vl_error": b.get("vl_error")})

        if label in NOISE_LABELS:
            continue                                   # 噪声块：直接丢，不算缺失

        # ---------- 表格：展开成矩阵，md 里只留锚点（F2.2）----------
        if label == "table" or tables.looks_like_table(text):
            t = tables.parse_table(text if tables.looks_like_table(text) else
                                   f"<table><tr><td>{text}</td></tr></table>",
                                   block_id=bid, page=page_no,
                                   table_index=len(table_objs) + 1)
            table_objs.append(t)
            # compiled.md 里仍只留一行锚点（F2.2）；送模文本里给「锚点 + 完整矩阵」
            emit(tables.anchor(t), tables.anchor(t) + "\n" + render_grid(t))
            index[-1]["content"] = ""
            index[-1]["table_index"] = t["index"]
            _add_section(sections, cur_section, bid)
            continue

        # ---------- 空块 / 截断块：缺失必须可见（F2.3）----------
        if not text:
            if b.get("truncated"):
                missing.append({"block_id": bid, "page": page_no, "label": label,
                                "reason": "VL 输出被截断（max_tokens 不足）",
                                "kind": "truncated"})
                truncated.append({"block_id": bid, "page": page_no, "label": label})
                emit(f"<!-- 截断 p{page_no}-b{bid}：{label}，内容被截断 -->")
            elif route in ("skipped", "unavailable", "vl") or b.get("vl_error"):
                act = {"skipped": "未处理（超出块数上限）",
                       "unavailable": "VL 服务不可用",
                       "vl": "VL 处理失败"}.get(route, "空内容")
                missing.append({"block_id": bid, "page": page_no, "label": label,
                                "reason": _reason(b, route, act),
                                "kind": "empty"})
                emit(f"<!-- 缺失 p{page_no}-b{bid}：{label}，{act} -->")
            elif label in PLACEHOLDER_LABELS:
                emit(f"<!-- 占位 p{page_no}-b{bid}：图片（无文本） -->")
            else:
                missing.append({"block_id": bid, "page": page_no, "label": label,
                                "reason": _reason(b, route, "空内容"), "kind": "empty"})
                emit(f"<!-- 缺失 p{page_no}-b{bid}：{label}，空内容 -->")
            index[-1]["content"] = ""
            _add_section(sections, cur_section, bid)
            continue

        # ---------- 正文 / 标题 ----------
        sec = _match_section(text, label)
        if sec:
            cur_section = sec
        _add_section(sections, cur_section, bid)

        if label in TITLE_LEVEL:
            emit(f"{TITLE_LEVEL[label]} {text}")
        else:
            emit(text)
        index[-1]["content"] = text

    return {"md": "\n\n".join(md), "index": index, "tables": table_objs,
            "missing": missing, "truncated": truncated, "sections": sections,
            "sec_lines": sec_lines, "sec_refs": sec_refs,
            "order_meta": meta}


def _match_section(text: str, label: str) -> str | None:
    if not text:
        return None
    head = text[:24]
    for name, pat in SECTION_RULES:
        if re.search(pat, head):
            return name
    if label in ("doc_title", "title", "paragraph_title") and len(text) <= 30:
        return None
    return None


def _add_section(sections: dict, name: str, bid) -> None:
    sections.setdefault(name, []).append(bid)


def raw_chars(pages: list[dict]) -> int:
    """原始块级字符数（含坐标/字段名），用于 token 对比。"""
    import json
    return sum(len(json.dumps(p, ensure_ascii=False)) for p in pages)


def compile_blocks(data, out_dir: Path, source: str = "") -> dict:
    """主流程：读 blocks（单页 dict 或页数组）-> 写全部 F2 产物。"""
    pages_in = order.iter_pages(data)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    md_parts: list[str] = []
    index_all: list[dict] = []
    tables_all: list[dict] = []
    missing_all: list[dict] = []
    truncated_all: list[dict] = []
    sections_all: dict[str, list] = {}
    sec_lines_all: dict[str, list] = {}
    sec_refs_all: dict[str, list] = {}
    order_meta_all: list[dict] = []

    for i, page in enumerate(pages_in, 1):
        r = build_page(page, i)
        md_parts.append(f"<!-- ===== 第 {i} 页 ===== -->\n{r['md']}")
        index_all += r["index"]
        tables_all += r["tables"]
        missing_all += r["missing"]
        truncated_all += r["truncated"]
        order_meta_all.append(r["order_meta"])
        for k, v in r["sections"].items():
            sections_all.setdefault(k, []).extend(v)
        for k, v in r["sec_lines"].items():
            sec_lines_all.setdefault(k, []).extend(v)
        for k, v in r["sec_refs"].items():
            sec_refs_all.setdefault(k, []).extend(v)

    compiled_md = "\n\n".join(md_parts) + "\n"
    textio.write_text(out_dir / "compiled.md", compiled_md)
    textio.dump_json(out_dir / "compiled.json", {"source": source, "pages": index_all})
    textio.dump_json(out_dir / "tables.json", {"tables": tables_all})
    textio.dump_json(out_dir / "sections.json", {"sections": sections_all})

    # ★ 新增产物：按章节渲染好的送模文本。F3 优先读它（见 structure.py 的
    #   chunks_from_sections_text）；读不到时退回旧的 build_chunks。
    sec_out: list[dict] = []
    for name, lines in sec_lines_all.items():
        body = "\n\n".join(lines)
        refs = sec_refs_all.get(name) or []
        sec_out.append({"name": name, "chars": len(body), "n_blocks": len(refs),
                        "refs": refs, "text": body})
    textio.dump_json(out_dir / "sections_text.json",
                     {"source": source, "sections": sec_out})

    total_blocks = len(index_all)
    kept = sum(1 for b in index_all if b.get("content"))
    tables_n = len(tables_all)
    raw = raw_chars(pages_in)
    out_chars = len(compiled_md)
    report = {
        "source": source,
        "pages": len(pages_in),
        "blocks_total": total_blocks,
        "blocks_with_text": kept,
        "blocks_empty": total_blocks - kept,
        "tables": tables_n,
        "table_rows": [t["n_rows"] for t in tables_all],
        "missing_blocks": missing_all,
        "missing_count": len(missing_all),
        "missing_ratio": round(len(missing_all) / total_blocks, 4) if total_blocks else 0.0,
        "truncated_blocks": truncated_all,
        "truncated_count": len(truncated_all),
        "order_meta": order_meta_all,
        "order_warnings": [w for m in order_meta_all for w in m.get("warnings", [])],
        "tokens": {
            "raw_chars": raw,
            "compiled_md_chars": out_chars,
            "ratio": round(out_chars / raw, 4) if raw else 0.0,
            "noise_chars": sum(1 for b in index_all if b.get("label") in NOISE_LABELS
                               for _ in str(b)),
        },
        # 中文链路三判据（encoding-policy.md §5）
        "cn": textio.text_metrics("".join((b.get("content") or "") for b in index_all)),
        "alerts": [],
    }
    if report["missing_ratio"] > MISSING_RATIO_ALERT:
        report["alerts"].append(
            f"缺失率 {report['missing_ratio']:.1%} > {MISSING_RATIO_ALERT:.0%}，"
            f"请先回 F1 补识别，不要进入 F3")
    if report["truncated_count"]:
        report["alerts"].append(f"仍有 {report['truncated_count']} 个截断块，表格内容可能缺行")
    textio.dump_json(out_dir / "compile_report.json", report)

    # 缺失率告警落日志
    if report["alerts"]:
        log = out_dir.parent / "logs"
        log.mkdir(parents=True, exist_ok=True)
        with open(log / "compile_alerts.log", "a", encoding="utf-8", newline="\n") as f:
            for a in report["alerts"]:
                f.write(f"[{source}] {a}\n")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="F2 编译层：blocks.json -> compiled/*")
    ap.add_argument("blocks", help="raw/blocks.json（单页 dict 或页数组）")
    ap.add_argument("--out", required=True, help="输出目录（通常 jobs/<biz>/compiled）")
    ap.add_argument("--tol-frac", type=float, default=order.COL_TOL_FRAC,
                    help=f"聚栏左边界容差（默认 {order.COL_TOL_FRAC}）")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    src = Path(a.blocks)
    if not src.exists():
        print(f"找不到文件: {src}", file=sys.stderr)
        return 1
    data = textio.load_json(src)
    if a.tol_frac != order.COL_TOL_FRAC:
        order.COL_TOL_FRAC = a.tol_frac
    rep = compile_blocks(data, Path(a.out), source=src.name)

    if not a.quiet:
        print(f"编译完成 -> {Path(a.out).resolve()}")
        print(f"  页 {rep['pages']} · 块 {rep['blocks_total']} "
              f"(有文本 {rep['blocks_with_text']} / 空 {rep['blocks_empty']})")
        print(f"  表格 {rep['tables']} 个（行数 {rep['table_rows']}）")
        print(f"  缺失 {rep['missing_count']}（{rep['missing_ratio']:.1%}）· "
              f"截断 {rep['truncated_count']}")
        print(f"  字符 {rep['tokens']['raw_chars']:,} -> {rep['tokens']['compiled_md_chars']:,}"
              f"（{rep['tokens']['ratio']:.0%}）")
        stp = Path(a.out) / "sections_text.json"
        if stp.exists():
            n_sec = len(textio.load_json(stp).get("sections") or [])
            print(f"  送模文本 {n_sec} 个章节 -> sections_text.json（F3 优先读它）")
        print(f"  中文：长度 {rep['cn']['len']} · 汉字 {rep['cn']['cjk']} · "
              f"sha {rep['cn']['sha']} · 坏字符 {rep['cn']['broken']}")
        for w in rep["order_warnings"]:
            print(f"  · {w}")
        for al in rep["alerts"]:
            print(f"  ⚠ {al}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
