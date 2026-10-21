#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段② s2_preprocess.py —— 块文本清洗/纠错 + 表格 HTML → 单元格矩阵。

输入   run/wide/<batch>/01_ocr/<case_id>/page_NNNN.json
输出   run/wide/<batch>/02_text/<case_id>/page_NNNN.json   结构化块（含 cells）
       run/wide/<batch>/02_text/<case_id>/page_NNNN.md     给人看的页文本
       run/wide/<batch>/02_text/<case_id>/_manifest.json   + .done / .incomplete
       run/wide/<batch>/_audit.jsonl                       输入哈希 / 输出哈希

为什么需要这一层（实测）：
  * 首页整页是一张表，OCR 给的是 `<table><tr><td>姓名潘满兴</td>…` —— 不拆成单元格，
    「字段名 ↔ 值」的对应关系就只剩一行糊在一起的字符串（风险①的下半段）。
  * OCR 会把同一行**重复输出十几遍**（实测奉化首页费用区同一行重复 30+ 次）——
    不去重会把下游 LLM 的上下文塞满噪声。
  * 空白/低信息页（风险②）在这一层**只留标记不留内容**，下游拿不到当内容的机会。
"""
from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wide import common as C  # noqa: E402
from wide import s1_ocr as S1  # noqa: E402  (round 7 第1件：复读检测判据同源)

STAGE = "02_text"


# --------------------------------------------------------------- 表格 HTML → 矩阵
class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            a = dict(attrs)
            self._cell = {"text": "", "colspan": int(a.get("colspan", 1) or 1),
                          "rowspan": int(a.get("rowspan", 1) or 1)}

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data


def table_to_matrix(html: str) -> list[list[str]]:
    """<table>…</table> → 二维矩阵；colspan/rowspan 展开成重复单元格。"""
    p = _TableParser()
    try:
        p.feed(html)
    except Exception:                                              # noqa: BLE001
        return []
    grid: list[list[str]] = []
    pending: dict[int, dict] = {}          # col -> {rows_left, text}
    for row in p.rows:
        out: list[str] = []
        col = 0
        # 先让 rowspan 的续行占位
        for c, info in list(pending.items()):
            if info["rows_left"] > 0:
                while len(out) < c:
                    out.append("")
                out.append(info["text"])
                info["rows_left"] -= 1
                if info["rows_left"] <= 0:
                    pending.pop(c, None)
                col = len(out)
        for cell in row:
            while len(out) < col:
                out.append("")
            for k in range(cell["colspan"]):
                out.append(cell["text"])
            if cell["rowspan"] > 1:
                pending[col] = {"rows_left": cell["rowspan"] - 1, "text": cell["text"]}
            col += cell["colspan"]
        grid.append(out)
    return grid


# --------------------------------------------------------------- 清洗规则
COLON = re.compile(r"[\s]*[:：][:：]?")
STRAY = re.compile(r"[|｜]{2,}")
SPACE_RUN = re.compile(r"[ \t]{2,}")


def clean_text(s: str) -> str:
    s = s.replace("\u3000", " ").replace("\r", "\n")
    s = STRAY.sub(" | ", s)
    s = SPACE_RUN.sub(" ", s)
    return s.strip()


def dedupe_repeats(lines: list[str], min_len: int = 6) -> tuple[list[str], int]:
    """连续重复行去重 + 全页同一条超长重复去重（OCR 复读 artifact）。

    round 7 第1件：这里**故意保持原语义**（只去 len>=min_len 的连续重复、len>=25 的全局重复）。
    原因：_blocks_to_text 是**按块**喂 lines 的，而 vLLM 一页只出 1 个块 → 复读循环的
    2029 行全挤在**一条** line 里，在这里加「按行」的规则根本看不到
    （实测 dropped_repeat_lines=0、02_text 的 text_sha256 与修复前完全相同）。
    真正处理复读的地方是下面按**换行**切开的 collapse_repeat_loops()。
    """
    out, dropped, prev = [], 0, None
    for ln in lines:
        key = ln.strip()
        if key and key == prev and len(key) >= min_len:
            dropped += 1
            continue
        out.append(ln)
        prev = key
    seen, out2, dropped2 = set(), [], 0
    for ln in out:
        key = ln.strip()
        if len(key) >= 25 and key in seen:
            dropped2 += 1
            continue
        if len(key) >= 25:
            seen.add(key)
        out2.append(ln)
    return out2, dropped + dropped2


def collapse_repeat_loops(lines: list[str],
                          run_min: int = S1.REPEAT_RUN_MIN) -> tuple[list[str], int]:
    """复读循环页专用清洗：同一条非空行出现 >= run_min 次 → 只留第一条。

    **只在 process_page / _variant_pages 判定 repeat_loop=True 之后调用** ——
    正常页一个字节都不动（47/50 页 text_sha256 不变，见 reports/round7-*）。
    按**换行**切开算，不是按块：vLLM 一页只出 1 个块。
    全页出现次数 >= run_min 这一条蕴含「连续 run >= run_min」，所以实现只做前者：
      桐乡二院__00286042 p2 连续 run=2027；
      长兴人民__200份_(13) p61 同一条 2 字符行出现 1018 次但被空行隔开（连续 run=1）。
    run_min 与检测器同源（s1_ocr.REPEAT_RUN_MIN=20），阈值依据见 s1_ocr 里的注释。
    """
    flat: list[str] = []
    for ln in lines:
        flat.extend(ln.split("\n"))
    count: dict[str, int] = {}
    for ln in flat:
        key = ln.strip()
        if key:
            count[key] = count.get(key, 0) + 1
    seen, out, dropped = set(), [], 0
    for ln in flat:
        key = ln.strip()
        if key and count[key] >= run_min:
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
        out.append(ln)
    return out, dropped


def page_truncation(rec: dict, body: str) -> tuple[dict, dict]:
    """把「本页是否被 max_tokens 截断」与「本页是否复读循环」整理成下游可读的两个结构。

    截断信号只认 01_ocr 记的（finish_reason=length），不猜；
    复读循环按本页**清洗后**正文现算 → 老批次没有 repeat_loop 字段也能跑。
    截断 != 坏页：一页可以合法地长到被切；复读循环才是确定的坏内容。两个信号分开记。
    """
    variants = rec.get("variants") or {}
    truncated = False
    n_bands = 1
    band_info: list[dict] = []
    aborts: list[dict] = []
    for v in variants.values():
        if v.get("truncated"):
            truncated = True
        try:
            n_bands = max(n_bands, int(v.get("n_bands") or 1))
        except (TypeError, ValueError):
            pass
        band_aborts = [{"band": b.get("band"), **b["repeat_abort"]}
                       for b in (v.get("bands") or []) if b.get("repeat_abort")]
        if band_aborts:
            aborts.extend(band_aborts)
        elif v.get("repeat_abort"):
            # 整页通道（无分带元数据）→ 断流事件只挂在变体级；有分带时只记带级，避免同一事件报两遍
            aborts.append({"band": None, **v["repeat_abort"]})
        for b in (v.get("bands") or []):
            if b.get("truncated"):
                band_info.append({"band": b.get("band"), "chars": b.get("chars"),
                                  "finish_reason": b.get("finish_reason")})
    trunc = {"truncated": truncated, "n_bands": n_bands,
             "truncated_bands": [b["band"] for b in band_info], "bands": band_info,
             "repeat_abort": bool(aborts), "repeat_abort_detail": aborts}
    loop = S1.detect_repeat_loop(body)
    if aborts and not loop["hit"]:
        # 断流把循环尾巴切掉了（正文里只剩 run_min 份）→ 对正文现算的判据可能不再命中，
        # 但服务器确实是在复读才被断的：断流事件本身就是坏页证据，照样折叠 + 照样报警。
        loop = {**loop, "hit": True, "why": (loop["why"] + "+abort").lstrip("+")}
    return trunc, loop


def watchword_hits(text: str, words: list[str]) -> list[str]:
    return [w for w in words if w in text]


# --------------------------------------------------------------- 单页
def _blocks_to_text(blocks: list, cfg: dict,
                    collapse: bool = False) -> tuple[list, str, int]:
    """块列表 → (结构化块, 页文本, 去掉的重复行数)。表格 HTML 顺带拆成单元格矩阵。

    collapse=True 只给 repeat_loop 页用（见 process_page）：块内正文也按换行折叠，
    否则 blocks[].text 里那 2027 行垃圾照样喂给 s3 的 block / checkbox 策略。
    """
    blocks_out, lines = [], []
    for b in blocks or []:
        label = b.get("block_label") or ""
        raw = b.get("block_content") or ""
        text = clean_text(raw)
        item = {"block_id": b.get("block_id"), "block_label": label,
                "block_bbox": b.get("block_bbox"), "text": text,
                "n_chars": len(text)}
        if label == "table" and "<table" in raw:
            item["cells"] = table_to_matrix(raw)
            item["n_rows"] = len(item["cells"])
        blocks_out.append(item)
        if label == "table" and item.get("cells"):
            for row in item["cells"]:
                lines.append(" | ".join(x for x in row))
        else:
            lines.append(text)
    lines, dropped = dedupe_repeats(lines)
    if collapse:
        lines, extra = collapse_repeat_loops(lines)
        dropped += extra
        for it in blocks_out:
            cl, _ = collapse_repeat_loops([it["text"]])
            it["text"] = "\n".join(x for x in cl if x)
            it["n_chars"] = len(it["text"])
    return blocks_out, "\n".join(x for x in lines if x), dropped


def process_page(rec: dict, cfg: dict) -> dict:
    blocks_out, body, dropped = _blocks_to_text(rec.get("blocks"), cfg)
    trunc, loop = page_truncation(rec, body)
    collapsed = 0
    if loop["hit"]:
        # 截断 != 坏页，复读循环才是确定的坏内容（第1件策略 a+d）。折叠在检测之后做
        # （先折叠就检不出来了）；折叠后 02_text 与「服务器这一页实际吐了几份」无关 ——
        # 这也是第4件客户端断流仍能保持逐字节确定的前提。
        blocks_out, body, dropped2 = _blocks_to_text(rec.get("blocks"), cfg, collapse=True)
        collapsed, dropped = dropped2 - dropped, dropped2
    return {
        "case_id": rec.get("case_id"), "page": rec.get("page"),
        "page_no_1based": rec.get("page_no_1based"),
        "page_kind": rec.get("page_kind"),
        "ocr_status": rec.get("status"),
        # round 7 第1件：truncated 之前只存在于 01_ocr，s2/s3/s5/s6 grep 不到 →
        # 下游把被截断的页当完整页用。这里把它（和复读循环）显式传到下游。
        "truncated": trunc["truncated"],
        "truncated_bands": trunc["truncated_bands"],
        "n_bands": trunc["n_bands"],
        "repeat_loop": loop["hit"],
        "repeat_loop_detail": loop,
        "repeat_loop_collapsed": collapsed,
        # round 7 第4件：这一页的循环是**客户端断流**砍掉的，不是跑完才看到的
        "repeat_abort": trunc["repeat_abort"],
        "repeat_abort_detail": trunc["repeat_abort_detail"],
        "input_png_sha256": rec.get("png_sha256"),
        "input_ocr_response_sha256": rec.get("ocr_response_sha256"),
        "n_blocks": len(blocks_out), "dropped_repeat_lines": dropped,
        "watchwords": watchword_hits(body, list(cfg["blank_guard"]["hallucination_watchwords"])),
        "blocks": blocks_out, "text_sha256": C.sha256_text(body), "text": body,
        "variants": rec.get("variants") or {},
        **_variant_pages(rec, cfg),
    }


def _variant_pages(rec: dict, cfg: dict) -> dict:
    """副变体（如整页模式）同样清洗，产出 blocks_<v>/text_<v> —— 勾选框选中项只在副变体里。"""
    out: dict = {}
    for key in rec:
        if not key.startswith("blocks_"):
            continue
        v = key[len("blocks_"):]
        vb, vbody, vdrop = _blocks_to_text(rec[key], cfg)
        vloop = S1.detect_repeat_loop(vbody)
        if vloop["hit"]:
            vb, vbody, vdrop = _blocks_to_text(rec[key], cfg, collapse=True)
        out[f"blocks_{v}"] = vb
        out[f"text_{v}"] = vbody
        out[f"text_sha256_{v}"] = C.sha256_text(vbody)
        out[f"dropped_repeat_lines_{v}"] = vdrop
        out[f"repeat_loop_{v}"] = vloop["hit"]
    return out


def process_case(case: str, src_dir: Path, dst_dir: Path, cfg: dict,
                 audit: C.Audit, force: bool, quiet: bool) -> dict:
    pages = sorted(src_dir.glob("page_*.json"))
    if not pages:
        return {"case_id": case, "pages_in": 0, "pages_out": 0, "errors": ["no stage-1 pages"]}
    (dst_dir / ".done").unlink(missing_ok=True)
    (dst_dir / ".incomplete").unlink(missing_ok=True)
    # 清掉上一轮留下的分页产物（只删本例目录下 page_*.{json,md}，别的不动）
    keep = {pj.stem for pj in pages}
    stale = [p for p in list(dst_dir.glob("page_*.json")) + list(dst_dir.glob("page_*.md"))
             if p.stem not in keep]
    for p in stale:
        p.unlink()
        C.warn(f"{case}: 删除上一轮残留 {p.name}")
    errs, n_ok, n_blank = [], 0, 0
    n_low = 0
    for pj in pages:
        rec = C.read_json(pj)
        try:
            out = process_page(rec, cfg)
        except Exception as e:                                     # noqa: BLE001
            errs.append(f"{pj.name}: {type(e).__name__}: {e}")
            C.err(f"[{case}] {pj.name} 预处理失败: {e}")
            continue
        C.write_json(dst_dir / pj.name, out)
        C.write_text(dst_dir / (pj.stem + ".md"), out["text"] + "\n")
        audit.emit(case_id=case, page=out["page"], kind="preprocess",
                   input_sha256=out.get("input_ocr_response_sha256"),
                   text_sha256=out["text_sha256"], n_blocks=out["n_blocks"],
                   dropped_repeat_lines=out["dropped_repeat_lines"],
                   watchwords=out["watchwords"])
        if out["ocr_status"] == "ok" and out["page_kind"] == "low_info":
            n_low += 1
        elif out["page_kind"] == "ok" and out["ocr_status"] == "ok":
            n_ok += 1
        else:
            n_blank += 1
        if out["watchwords"]:
            C.warn(f"{case} page {out['page']}: 命中幻觉监视词 {out['watchwords']}")
        C.log(f"    {pj.name} blocks={out['n_blocks']} dropped_repeat={out['dropped_repeat_lines']}",
              quiet=quiet)
    summary = {"case_id": case, "pages_in": len(pages), "pages_out": n_ok + n_blank,
               "pages_ok": n_ok, "pages_low_information": n_blank, "errors": errs}
    summary["pages_low_info_ocr"] = n_low
    C.write_json(dst_dir / "_manifest.json", summary)
    if errs:
        C.write_marker(dst_dir / ".incomplete", "；".join(errs))
    else:
        C.write_marker(dst_dir / ".done")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="阶段② 文本预处理与纠错")
    ap.add_argument("--batch", required=True)
    C.add_case_args(ap)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    cfg = C.load_pipeline()
    batch_dir = Path(cfg["paths"]["run_root"]) / a.batch
    src_root = batch_dir / "01_ocr"
    audit = C.Audit(batch_dir / "_audit.jsonl", STAGE)
    cases = [p.name for p in sorted(src_root.iterdir()) if p.is_dir()]
    want = C.wanted_cases(a.case, a.case_file)
    if want:
        cases = [c for c in cases if c in want]
    n_bad = 0
    for c in cases:
        dst = batch_dir / STAGE / c
        if (dst / ".done").exists() and not a.force:
            C.log(f"  跳过（已 .done）{c}", quiet=a.quiet)
            continue
        s = process_case(c, src_root / c, dst, cfg, audit, a.force, a.quiet)
        if s["errors"]:
            n_bad += 1
    C.log(f"完成：{len(cases)} 例，{n_bad} 例有页级失败")
    return 1 if n_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
