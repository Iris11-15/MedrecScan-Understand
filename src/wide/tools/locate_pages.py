#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/locate_pages.py —— 页面定位：找出每例的「首页页码」与「首程页码」。

为什么不能直接用旧批次的页号（实测，2026-10-09）：
    奉化__20230728_008 源 PDF 90 页，旧 compiled.json 里 page 字段是 **1..70 连续**，
    即旧产线丢了 20 页后**重新连续编号** —— 旧页号与源页号不对应，拿来定位会错位。
    所以旧页号只用来**决定扫描顺序**（先扫最可能的页），不作为结论；
    每个命中都由本页的页眉条 OCR 现场证实，写进 evidence。

为什么用页眉条 OCR 而不是整页 OCR（实测 2026-10-09，奉化 008，同一 PDF）：
    整页 layout=true 18.6–20.1s / plain 13.7s；中位 59 页/例 → 一例 ≈20 分钟，不可行。
    页眉条按高度实测（同一页，秒/次）：
        top12%  0.5–1.4s  —— **漏关键词**（「首次病程记录」「住院病案首页」都不在条内）
        top20%  1.2–3.2s  —— 关键词全中（默认）
        top30%  2.4–8.3s  —— 关键词全中，但贵 2–3 倍
    多页页眉条拼成一张图一次 OCR：4 条拼一张 = 37.1s，比分开扫（4×2s）更慢，
    而且版面块会整张合并、无法按 y 坐标可靠归属到页 —— 已否决（run/tmp/probe/stitch.py）。

早停：扫到「首页命中 + 首页续页已扫 + 最高优先级首程命中」即停（--no-early-exit 关掉）。
兜底阶梯（--escalate-positions / --fallback-frac）：窄条没扫全 → 逐级换边/加高**重扫同一批页**：
    第 1 级 top20%（默认，命中最快）→ 第 2 级 bottom20%（便宜，救页底标题的院区）
    → 第 3 级 45%×top+bottom（贵，最后手段）。
    「哪些页要重扫」必须按 **(frac, positions)** 记，不能只按页号 —— 2026-10-09 之前写的是
    `rest = [p for p in order if p not in strips]`，把「已用窄条扫过的页」全排除，
    剩下的只有已判空白的页，而空白判定用**整页** page_metrics（与条几何无关）
    → 兜底轮 0 次 OCR，**结构性空转**（实测 speed10 安吉两例：taller_strip 27 页全部
    再次判空，加高条一次都没跑过）→ 首页 0 命中 → s1 回退扫前 3 页 → 宽表整行空。
    实测标题页高因院区而异（run/workspace/anji_fix/probe_strip.py，2026-10-09）：
    安吉__09162565 p054/p055、__09220067 p056/p057 的「住院病案首页」在 **y≈0.85-0.89**，
    top20% 全 0 命中，bottom20% 全命中（1.3-4.7s/页，比 45%×top+bottom 的 10.7-16s 便宜 3-4 倍）。
风险②：定位阶段也用 s1_ocr.classify_page（ink_ratio + gray_std）判空白/低信息，
        不送 OCR。实测（奉化__20230728_008）只按 ink_min 卡会漏：
        0based 82 页眉条 OCR 出「識典古籍」、84 页出「晉書卷八」—— 与病历无关的幻觉，
        墨迹高于 ink_min 但信息量极低，classify_page 判 low_info 后不再送 OCR。
        空白判定按整页、与页眉条几何无关 → 已判空白的页换几何时**不必重扫**（重扫必然再空，
        白烧渲染），兜底阶梯因此跳过它们；`pages_blank_skipped` 按页去重（旧写法把同一页
        在兜底轮重复计数，56 页的例报成「跳过 54 页」）。
首页续页：病案首页常占两页，**续页常常没有标题**（实测奉化 008：标题在 0based 89，
        续页「手术及操作/伤口愈合/麻醉方式」在 0based 88，即在标题页**前**一页）。
        只扫 p+1 或只认关键词都会漏掉续页 → 离院方式/手术/质控整页丢失。
        现在 p±1 都扫，并用 locator.frontpage_continuation_markers 认续页。

定位缓存（2026-10-09 改动①）：
    同一个 case 重复跑时跳过全部页眉条 OCR，直接读上次确认过的定位结论。
    缓存键 = 源 PDF 内容哈希 + config/pipeline.yaml 内容哈希 + 本模块与空白守卫
    所在模块（s1_ocr）与 common 的代码哈希 + 定位词表/扫描参数 + 先验索引哈希。
    任一项变化 → 未命中，重扫。**改了 prompt/词表/代码还用旧缓存 = 静默错误**，
    所以宁可多扫也不可复用可疑缓存：命中前还要校验产物完整（见 cache_is_complete）。
    缓存只对「同一例重复跑」有效，对批量吞吐**零收益**（每例的键都不同，必然未命中）。
    --no-cache 强制忽略缓存（测提速时必须用）。

两阶段探针扫描（2026-10-09 改动②）：
    旧扫描顺序 = 先验预测位置 ±ring(12) 圈 + 两端向中间，实测奉化 008 要扫 13 页
    （8 次页眉条 OCR）才早停。改成「先探首页、再按首页位置探首程」：
      阶段 A 首页：先验预测页 → 尾部两页 → 头部两页，命中即停。
      阶段 B 首程：以首页**锚点**（标题页，若续页在标题页前则用续页）为基准，
             按 15 例实测的相对位置猜页 —— 尾部组首程在锚点**前** 7..15 页
             （实测偏移 7×3、11×2、13×2、15×2），头部组在锚点**后** 2..8 页
             （实测 2×5、8×1）；猜不中再逐步外扩到 --pn-window，仍不中回到
             旧的 ring/两端全扫兜底 —— **探针不会少扫内容**，兜底覆盖全页。
    每一页都记进 scan_trace（phase / page / 命中写法 / 空白跳过类别），
    并汇总 fp_hit_page、pn_hit_page、pn_probe_tried、pn_probe_expansions、
    probe_fallback —— 漏页时能查是哪一阶段没命中。
    --no-probe 回到旧扫描顺序。
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from wide import common as C  # noqa: E402
from wide import s1_ocr as S  # noqa: E402

STRIP_FRAC = 0.20          # 页眉条占页高比例（实测够读标题，见模块 docstring）
STRIP_TARGET_PX = 900_000  # 页眉条渲染像素上限（够读标题即可）

CACHE_DIR_NAME = "_locate_cache"   # run/wide/_locate_cache/（跨批次共享，按内容哈希失效）
# round47 S5：缓存键里的「代码指纹」换成**人工版本号**。
# 原 code_sha256 = sha256(本模块 ‖ s1_ocr ‖ common) ⇒ 任何一次改这三个文件（哪怕只改注释）
# 都会让全部历史缓存失效：实测 _locate_cache 12,895 个文件里 distinct code_sha256=3、
# distinct pdf_sha256=1,828，基线批命中率 0/3,831 ⇒ 缓存等于不存在。
# ★ 代价（必须知道）：这摘掉了「代码变了自动失效」的防线。**改了下面这些逻辑必须 bump 版本**：
#   扫描顺序 / 早停判据 / 续页判据 / 页眉条几何 STRIP_FRAC / 空白守卫 classify_page / OCR 调用与重试。
#   忘了 bump ⇒ 命中旧结论 ⇒ 页号是旧算法的 ⇒ 字段静默错位（静默错，红线①）。
LOCATOR_VERSION = "r47-1"


def scan_sig(frac: float, positions: tuple) -> str:
    """一次页眉条扫描的**几何签名**。兜底轮判「这页用这个几何扫过没有」只能按它比，
    按页号比会把「用窄条扫过的页」误判成已覆盖（安吉类院区就是这么失效的）。"""
    return f"{round(float(frac), 3)}|{'+'.join(positions)}"


# --------------------------------------------------------------- 定位缓存
def cache_key(case: str, pdf_path: str, locator_args: dict, prior_path) -> dict:
    """缓存键的**材料**（不是最终哈希）—— 材料原样存进缓存文件，未命中时能看出差在哪。"""
    parts = {
        "case_id": case,
        "pdf_sha256": C.sha256_file(Path(pdf_path)),
        "pipeline_yaml_sha256": C.sha256_file(C.CONFIG_DIR / "pipeline.yaml"),
        "locator_version": LOCATOR_VERSION,               # round47 S5：见上面 CACHE_DIR_NAME 处
        "prior_index_sha256": C.sha256_file(Path(prior_path)) if prior_path else "none",
        "locator_args": locator_args,
    }
    return {"key": C.sha256_text(C.canon_json(parts)), "parts": parts}


def cache_is_complete(entry, n_pages: int, pdf_path: str) -> tuple[bool, str]:
    """命中前必须过的完整性检查 —— 不完整就当未命中重跑。

    只信「结论 + 证据」都在、且和当前 PDF 对得上：
    首页页集非空、证据非空、每条证据带页眉条文本哈希、strips 条数 == pages_scanned、
    source_pages == 当前 PDF 页数、PDF 存在且非 0 字节。
    """
    if not isinstance(entry, dict):
        return False, "缓存条目不是对象"
    if not entry.get("frontpage_pages"):
        return False, "frontpage_pages 为空（定位本来就没成功，不该复用）"
    ev = entry.get("evidence") or []
    if not ev:
        return False, "evidence 为空（没有现场 OCR 证据）"
    if any(not e.get("strip_sha256") for e in ev):
        return False, "evidence 里有条目缺 strip_sha256"
    strips = entry.get("strips") or {}
    if not strips:
        return False, "strips 为空（没有页眉条扫描记录）"
    if int(entry.get("pages_scanned") or 0) != len(strips):
        return False, (f"pages_scanned={entry.get('pages_scanned')} 与 strips "
                       f"{len(strips)} 条不一致")
    if int(entry.get("source_pages") or -1) != int(n_pages):
        return False, (f"缓存 source_pages={entry.get('source_pages')} ≠ 当前 PDF "
                       f"{n_pages} 页")
    if not entry.get("locator_source"):
        return False, "locator_source 缺失"
    p = Path(pdf_path)
    if not p.exists() or p.stat().st_size <= 0:
        return False, "源 PDF 缺失或 0 字节"
    return True, ""


def render_strip(pdf, page_no: int, render_cfg: dict, frac: float = STRIP_FRAC,
                 positions: tuple = ("top",)):
    """渲染并裁页眉条。返回 (PIL.Image, metrics)。

    positions 可给 ("top","bottom")：实测首页标题的纵向位置**因院区而异**
    （run/tmp/probe/title_pos.py：桐乡 y≈0.04-0.14、玉环 p0 y≈0.07-0.17、
    长兴 p102 y≈0.02-0.09，但安吉 y≈0.85-0.89、玉环/长兴的续页标题也在 y≈0.85-0.97）
    → 只裁 top 对这些院区结构上失效。裁 top+bottom **拼成一张**送 OCR，
    调用次数不变（1 次/页），只是图更高。
    """
    import pypdfium2 as pdfium  # noqa: F401
    from PIL import Image
    page = pdf[page_no]
    w_pt, h_pt = page.get_size()
    scale = min((STRIP_TARGET_PX / (w_pt * h_pt)) ** 0.5,
                float(render_cfg["max_upscale"]),
                float(render_cfg["max_side_pt"]) / max(w_pt, h_pt))
    scale = max(scale, float(render_cfg["min_scale"]))
    img = Image.fromarray(page.render(scale=scale).to_numpy()).convert("RGB")
    W, H = img.size
    h = max(1, int(H * frac))
    parts = []
    if "top" in positions:
        parts.append(img.crop((0, 0, W, h)))
    if "bottom" in positions:
        parts.append(img.crop((0, H - h, W, H)))
    if not parts:
        parts = [img.crop((0, 0, W, h))]
    if len(parts) == 1:
        strip = parts[0]
    else:
        strip = Image.new("RGB", (W, h * len(parts)), "white")
        for i, pt in enumerate(parts):
            strip.paste(pt, (0, i * h))
    return strip, S.page_metrics(img, {"dark_threshold": 200})


def ocr_strip(img, cfg_ocr: dict) -> str:
    """页眉条 OCR。定位只读标题，**关版面检测**（实测更快，文本够用）。"""
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    if S.resolve_backend(cfg_ocr, "locate") == "vllm":
        # vLLM 通道：页眉条同样走 /v1/chat/completions，只要纯文本标题。
        # ---- round23 第 3 步：页眉条只需要标题（实测未截断响应最长 1812 字符），
        #      但 5.2% 的探针会复读死循环跑满 4096 token（s0_land2：33 次 / 40 例，
        #      占 locate 生成量 70%）。R23_OCR_MAX_TOKENS 压低上限，只砍复读。----
        cap = int(os.environ.get("R23_OCR_MAX_TOKENS", "0") or 0)
        if cap > 0:
            cfg_ocr = {**cfg_ocr,
                       "vllm_max_tokens": min(int(cfg_ocr.get("vllm_max_tokens", 4096)), cap)}
        # ---- round24 第 1 步：页眉条探针此前走**非流式** ocr_image_vllm → 复读时
        #      完全没有早停（round23 ms_3072_1：633 次探针里 35 次撞满 cap，
        #      占 locate 生成字符 65%）。开了周期判据/审计就改走流式 + 断连。
        #      line_abort 默认关：探针上「整行重复」判据本轮没做过回归，不顺手打开。----
        _b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        _pmr = S.repeat_period_min_run(cfg_ocr)
        if _pmr > 0 or S.repeat_audit_enabled():
            pr, _raw, e = S.ocr_image_vllm_stream(
                _b64, cfg_ocr, S.vllm_prompt_for(cfg_ocr, stage="locate"), img.size,
                line_abort=(os.environ.get("R24_LOCATE_LINE_ABORT", "0") == "1"),
                period_min_run=_pmr, stage="locate")
        else:
            pr, _raw, e = S.ocr_image_vllm(
                _b64, cfg_ocr, S.vllm_prompt_for(cfg_ocr, stage="locate"), img.size)
        if pr is None:
            C.err(f"页眉条 OCR 失败（vllm）{e}")
            return ""
        return " ".join((b.get("block_content") or "")
                        for b in pr.get("parsing_res_list", []))
    payload = {"file": base64.b64encode(buf.getvalue()).decode("ascii"),
               "fileType": 1, "useLayoutDetection": False,
               "promptLabel": cfg_ocr["prompt_label"],
               "returnMarkdownImages": False, "maxPixels": int(cfg_ocr["max_pixels"])}
    url = cfg_ocr["base_url"].rstrip("/") + cfg_ocr["endpoint"]
    status, js, e = C.http_json(url, payload, int(cfg_ocr["timeout_s"]),
                                int(cfg_ocr.get("retries", 0)))
    if js is None:
        C.err(f"页眉条 OCR 失败 status={status} {e}")
        return ""
    try:
        pr = js["result"]["layoutParsingResults"][0]["prunedResult"]
    except (KeyError, IndexError, TypeError):
        C.err("页眉条 OCR 响应结构异常")
        return ""
    return " ".join((b.get("block_content") or "") for b in pr.get("parsing_res_list", []))


def probe_batch(pdf, pages: list[int], frac: float, positions: tuple,
                cfg: dict, concurrency: int = 1) -> tuple[dict, int]:
    """一批页的页眉条 OCR。返回 ({page: {"kind":…, "txt":…} | {"render_error":…}},
    **实际发出的 OCR 请求数** = len(jobs))。

    ★ round47 S1：请求数必须在**发请求处**记账，不能在处理序上记 ——
      早停/扫描上限 break 之后 take 里剩下的页请求已经发出去了，
      旧写法（:758 逐页 +=1）把它们全丢掉 ⇒ 自报比实际少 40.2%。

    ★ 渲染留在主线程：pypdfium2 的 PdfDocument/page 不是线程安全的，并发渲染会崩或
      会静默出错。只有 OCR 的 **HTTP 等待** 并发 —— 那正是墙钟的大头。
    ★ 空白/低信息页**不送 OCR**（风险②）：判定在渲染后、发请求前，与串行同一位置。
    ★ concurrency<=1 时逐页发，与旧写法逐字节等价。
    """
    import concurrent.futures as cf
    out: dict[int, dict] = {}
    jobs: list[int] = []
    for p in pages:
        try:
            strip, m = render_strip(pdf, p, cfg["render"], frac, positions)
        except Exception as e:                                   # noqa: BLE001
            out[p] = {"render_error": f"{type(e).__name__}: {e}"}
            continue
        kind = S.classify_page(m, cfg["blank_guard"])
        out[p] = {"kind": kind, "img": strip}
        if kind == "ok":
            jobs.append(p)
    if jobs:
        if concurrency > 1 and len(jobs) > 1:
            with cf.ThreadPoolExecutor(max_workers=min(concurrency, len(jobs))) as ex:
                futs = {p: ex.submit(ocr_strip, out[p]["img"], cfg["ocr"]) for p in jobs}
                for p, f in futs.items():
                    out[p]["txt"] = f.result()
        else:
            for p in jobs:
                out[p]["txt"] = ocr_strip(out[p]["img"], cfg["ocr"])
    for p in pages:
        out[p].pop("img", None)
    return out, len(jobs)


# --------------------------------------------------------------- 扫描顺序
def prior_positions(prior_cases: dict, case_id: str, n: int) -> tuple:
    """旧批次页号 → 预测位置。**只用来排序扫描**，不作为结论。

    旧产线丢页后重新编号，但「旧最后一页 ≈ 新最后一页」在实测样本里成立
    （奉化 008：旧 fp=70/70 → 预测 89，实测 89）。按 n_pages_seen 线性缩放。
    """
    e = prior_cases.get(case_id) or {}
    fp = list(e.get("frontpage_all_hits") or e.get("frontpage_pages") or [])
    pn = list(e.get("first_progress_all_hits") or e.get("first_progress_pages") or [])
    seen = int(e.get("n_pages_seen") or 0)
    if not seen or n <= 1 or (not fp and not pn):
        return None, None, "no_prior"
    pred_fp = min(n - 1, round(max(fp) / seen * (n - 1))) if fp else None
    pred_pn = min(n - 1, round(max(pn) / seen * (n - 1))) if pn else None
    return pred_fp, pred_pn, f"prior_index(old_fp={max(fp) if fp else None}," \
                             f"old_pn={max(pn) if pn else None},old_seen={seen})"


def load_locate_prior(path) -> dict:
    """位置先验表（config/locate_prior.yaml，AGENTS.md §四）。

    **只用来决定扫描顺序与页眉条几何，不作为结论** —— 每个命中仍由本页页眉条 OCR
    现场证实；先验错了 probe_fallback + 兜底阶梯照旧覆盖全部页，不会漏页。读不到=无先验。
    """
    p = Path(path) if path else None
    if not p or not p.exists():
        C.warn(f"位置先验 {p} 不存在 → 用「两端各试」的老策略")
        return {}
    try:
        cfg = C.load_yaml(p)
    except Exception as e:                                       # noqa: BLE001
        C.warn(f"位置先验 {p} 读不动（{type(e).__name__}: {e}）→ 老策略")
        return {}
    if not isinstance(cfg, dict) or not isinstance(cfg.get("hospitals"), dict):
        C.warn(f"位置先验 {p} 结构异常（缺 hospitals）→ 老策略")
        return {}
    return cfg


def prior_row(prior_cfg: dict, case_id: str) -> tuple:
    """case_id（<院区>__<病历号>）→ 该院区先验行。返回 (row, 院区, 是否命中表)。"""
    hospitals = (prior_cfg or {}).get("hospitals") or {}
    hosp = str(case_id).split("__", 1)[0]
    if hosp in hospitals:
        return hospitals[hosp] or {}, hosp, True
    return ((prior_cfg or {}).get("default") or {}), hosp, False


def geom_tuple(spec, fallback: tuple) -> tuple:
    """先验 geometry（top/bottom/top,bottom）→ 几何元组；空则用命令行默认。"""
    pos = tuple(x.strip() for x in str(spec or "").split(",") if x.strip())
    return pos or fallback


def fp_probe_groups(n: int, pred_fp, prior: dict | None = None) -> list:
    """阶段 A：首页候选，分三组依次试（命中即停，不再试后面的组）。

    实测 15 例只有两种结构：首页在**末尾** 2 页（奉化/安吉/长兴 9 例）
    或在**开头** 2 页（桐乡/玉环 6 例）—— 没有夹在中间的。

    位置先验（config/locate_prior.yaml）命中时**先验那一组排第一**：head 组 candidates
    写绝对页号（0/1），tail 组写「倒数第几页」（1 = n-1）。先验不中照样往下试
    [n-1,n-2] / [0,1]，再不行走 probe_fallback —— 只是慢，不会漏页。
    """
    groups = []
    cand = [int(c) for c in (((prior or {}).get("frontpage") or {})
                             .get("candidates") or [])]
    if cand:
        groups.append([n - c for c in cand]
                      if (prior or {}).get("group") == "tail" else cand)
    if pred_fp is not None:
        groups.append([pred_fp, pred_fp - 1])
    groups.append([n - 1, n - 2])
    groups.append([0, 1])
    return [[p for p in g if 0 <= p < n] for g in groups]


def pn_probe_order(n: int, anchor: int, group: str, window: int,
                   prior: dict | None = None) -> list:
    """阶段 B：首程候选，按「首程相对首页锚点的实测偏移」排序，再外扩到 window。

    先验命中时用先验 offsets（AGENTS.md §四：首程 = 首页 ±4~13 页），方向仍由 group 定；
    先验不中退回实测通用偏移表。两种情况后面都接 1..window 全外扩 —— 猜不中只是多扫。
    """
    pri = [abs(int(o)) for o in (((prior or {}).get("first_progress") or {})
                                 .get("offsets") or [])]
    if pri:
        offs = tuple(pri)
    elif group == "head":
        offs = (2, 3, 1, 4, 6, 8, 5, 7, 9, 10)
    else:
        offs = (11, 7, 13, 15, 9, 12, 14, 10, 8, 6, 16, 5)
    order, seen = [], set()
    for o in list(offs) + list(range(1, max(window, 0) + 1)):
        for p in ((anchor - o, anchor + o) if group == "tail" else (anchor + o, anchor - o)):
            if 0 <= p < n and p not in seen:
                seen.add(p)
                order.append(p)
    return order


def scan_order(n: int, pred_fp, pred_pn, ring: int = 12) -> list:
    """先扫预测位置附近（由内向外扩 ring 圈），再两端向中间。"""
    seen, order = set(), []

    def push(p):
        if 0 <= p < n and p not in seen:
            seen.add(p)
            order.append(p)

    for r in range(0, ring + 1):
        for c in (pred_fp, pred_pn):
            if c is None:
                continue
            push(c + r)
            if r:
                push(c - r)
    lo, hi = 0, n - 1
    while lo <= hi:
        push(hi)
        push(lo)
        lo, hi = lo + 1, hi - 1
    return order


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--case", action="append", help="只定位这些 case_id（可重复）")
    ap.add_argument("--case-file", default="", help="从文件读 case_id，一行一个")
    ap.add_argument("--out", default="", help="默认 run/wide/<batch>/00_index/page_index.json")
    ap.add_argument("--prior-index", default="",
                    help="旧批次定位（仅用于扫描顺序）；默认同目录 *_DEPRECATED.json")
    ap.add_argument("--strip-frac", type=float, default=STRIP_FRAC)
    ap.add_argument("--strip-positions", default="top",
                    help="页眉条裁哪几段：top / bottom / top,bottom")
    ap.add_argument("--fallback-frac", type=float, default=0.45,
                    help="没命中时加高页眉条重扫（0=不做兜底）")
    ap.add_argument("--fallback-positions", default="top,bottom",
                    help="兜底重扫时裁哪几段（默认 top,bottom —— 解决标题在页脚的院区）")
    ap.add_argument("--escalate-positions", default="bottom",
                    help="兜底第 1 级：换这几段但**不加高**（便宜，先试；空=跳过该级）")
    ap.add_argument("--max-scan-pages", type=int, default=0,
                    help="单例最多扫多少页（0=不设上限）；超线停下并记 scan_budget_hit")
    ap.add_argument("--grace-pages", type=int, default=25,
                    help="没有 top 优先级首程写法时，扫过这么多页且有次优首程命中就停（0=关）")
    ap.add_argument("--grace-rank", type=int, default=-1,
                    help="退让门槛：最佳首程命中的优先级 <= 此值才允许靠 grace 提前收手"
                         "（-1=用 pipeline.yaml:locator.grace_rank，默认 1）")
    ap.add_argument("--grace-pages-hard", type=int, default=-1,
                    help="最佳首程命中优先级 > grace_rank 时，要扫过这么多页才退让"
                         "（-1=用 pipeline.yaml:locator.grace_pages_hard，再缺省=2×grace-pages）")
    ap.add_argument("--no-geometry-preprobe", action="store_true",
                    help="关掉「top 找不到首页就先用 bottom 试同一批页」的几何预探针")
    ap.add_argument("--locate-prior", default="",
                    help="位置先验表（院区→首页位置组/页眉几何/首程页距），"
                         "默认 config/locate_prior.yaml；读不到=退回两端各试的老策略")
    ap.add_argument("--ring", type=int, default=12, help="预测位置外扩圈数")
    ap.add_argument("--try-first", default="",
                    help="先扫这些页（0based，逗号分隔），再走正常扫描顺序；索引里记 manual_seed_pages")
    ap.add_argument("--no-early-exit", action="store_true", help="关掉早停，全页扫")
    ap.add_argument("--max-frontpage", type=int, default=2)
    ap.add_argument("--max-first-progress", type=int, default=3)
    ap.add_argument("--follow", type=int, default=0,
                    help="首程标题页之后再多取几页（0=用 pipeline.yaml 的值）")
    ap.add_argument("--no-probe", action="store_true",
                    help="关掉两阶段探针，回到旧的 ring/两端扫描顺序")
    ap.add_argument("--probe-concurrency", type=int, default=-1,
                    help="同一批候选页的页眉条 OCR 并发度（-1=用 pipeline.yaml:locator."
                         "probe_concurrency，再缺省=1）。只并行**已在队列里**的页；"
                         "首页→首程窗口是顺序依赖，不并行。并发会**多扫**几页"
                         "（done() 早停时预取回来的结果被丢弃）→ 请求数上升、墙钟下降")
    ap.add_argument("--pn-window", type=int, default=25,
                    help="探针猜不中时，首程最多外扩到锚点 ±这么多页")
    ap.add_argument("--vllm-urls", default="",
                    help="逗号分隔的多个 vLLM 端点（多副本轮询），覆盖 ocr.vllm_urls")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-cache", action="store_true",
                    help="忽略并覆盖定位缓存（测提速时必须加，否则缓存会冒充提速）")
    ap.add_argument("--cache-dir", default="",
                    help=f"定位缓存目录，默认 {CACHE_DIR_NAME}/（在 run/wide 下，跨批次共享）")
    a = ap.parse_args(argv)
    manual_seed = [int(x) for x in re.split(r"[,\s]+", a.try_first.strip()) if x.strip()]

    import pypdfium2 as pdfium
    cfg = C.load_pipeline()
    batch_dir = Path(cfg["paths"]["run_root"]) / a.batch
    out_path = Path(a.out) if a.out else batch_dir / "00_index" / "page_index.json"
    # ── 5 号补丁（round15）：批级 page_index.json 的读-改-写竞态 ──
    # 根因：src/wide/tools/locate_pages.py:970/:973 对同一个 out_path 先读 existing
    # 再整份覆写，例级并行（2a）下后写的进程会把先写的整份覆盖 → 扫页审计只剩部分例。
    # 改法：每例只写自己的分片 00_index/page_index/<case>.json（跨进程无共享文件 → 天然无锁），
    # 批级 page_index.json 由分片合并得到（run_steps 收尾再合并一次，保证 10/10）。
    shard_dir = out_path.parent / "page_index"
    shard_dir.mkdir(parents=True, exist_ok=True)

    def write_shard(case: str, entry: dict) -> None:
        C.write_json(shard_dir / f"{case}.json",
                     {"case_id": case, "entry": entry,
                      "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z")})

    def write_json_locked(path: Path, obj: dict) -> None:
        # round16 补丁（6 号）：批级 page_index.json 在 K>1 下是**多写者**。
        # common.write_json 的临时名固定为 <name>.tmp —— 两个进程同时写会互相踩：
        #   A os.replace(tmp,path) 之后 B 的 fd 仍指向已被改名的 inode →
        #   B 直接写进**线上** page_index.json（实测：locate FileNotFoundError、
        #   s1 读到 0 字节 JSONDecodeError，各 1 例/臂）。
        # 改法：pid 唯一临时名 + flock 串行化（读-合并-写整体加锁）。
        import fcntl
        import os
        import json
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        def open_private(p: Path):
            # round45 R9：权限必须在 open 时就带上（0o600）。旧写法 tmp 按 umask 落成 664，
            # os.replace 继承该 mode，而 chmod 在 replace **之后** ⇒ 几十 MB、内容含
            # case_id（内嵌患者姓名）的批级索引存在 664 窗口；进程恰好在这一步被杀
            # ⇒ 线上文件永久 664。.lock 同理。
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            return os.fdopen(fd, "w", encoding="utf-8")

        with open_private(path.parent / (path.name + ".lock")) as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
            with open_private(tmp) as f:
                json.dump(obj, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            C.chmod(path, "600")
            fcntl.flock(lf, fcntl.LOCK_UN)

    def merge_shards() -> dict:
        merged: dict = {}
        for sp in sorted(shard_dir.glob("*.json")):
            try:
                blob = C.read_json(sp)
            except Exception:                                              # noqa: BLE001
                C.warn(f"分片 {sp.name} 读不动 → 跳过")
                continue
            entry = blob.get("entry") or {}
            cid = blob.get("case_id") or sp.stem
            if entry:
                merged[cid] = entry
        return merged

    audit = C.Audit(batch_dir / "_audit.jsonl", "00_locate")
    loc = cfg["locator"]
    kw_fp = list(loc["frontpage_keywords"])
    kw_pn = list(loc["first_progress_keywords"])
    prio = list(loc.get("first_progress_priority") or kw_pn)
    prio_rank = {k: i for i, k in enumerate(prio)}
    top_kw = prio[0] if prio else None
    follow = a.follow if a.follow else int(loc.get("first_progress_follow_pages", 0))
    # grace 门槛与几何预探针：命令行 -1 = 用配置值（配置再缺省 = 旧行为的两倍页数 / 开）
    if a.grace_rank < 0:
        a.grace_rank = int(loc.get("grace_rank", 1))
    if a.grace_pages_hard < 0:
        a.grace_pages_hard = int(loc.get("grace_pages_hard", 0) or 0)
    if a.grace_pages and a.grace_pages_hard <= 0:
        a.grace_pages_hard = 2 * a.grace_pages
    preprobe = bool(loc.get("geometry_preprobe", True)) and not a.no_geometry_preprobe
    # 页眉条探测并发（本轮任务第 4 件）。默认 1 = 与旧写法逐字节等价。
    probe_conc = max(1, int(a.probe_concurrency if a.probe_concurrency >= 0
                            else (loc.get("probe_concurrency") or 1)))
    if a.vllm_urls.strip():
        cfg["ocr"]["vllm_urls"] = [u.strip() for u in a.vllm_urls.split(",") if u.strip()]
        C.log(f"ocr.vllm_urls 覆盖为 {cfg['ocr']['vllm_urls']}（命令行）")
    # 红线①：页眉条并发同样会改 OCR 正文（批形状）→ 命中页可能跟着变 → 字段跟着变。
    # 闸门必须与 s1_ocr **同一个函数**（旧写法自己抄了一份，漏了 require_batch_invariant
    # 与 replica_single_seq_ok 两个开关 → 2026-10-10 c4g 日志里 s1 放行、probe 被压回 1）。
    if probe_conc > 1:
        gate_ok, gate_why = S.concurrency_gate(cfg["ocr"], "locate")
        if not gate_ok:
            C.warn(f"页眉条探测并发 {probe_conc} 被压回 1 —— {gate_why}")
            probe_conc = 1
    if probe_conc > 1:
        C.log(f"页眉条探测并发 = {probe_conc}（只并行同一批候选页；"
              "首页→首程窗口有依赖，不并行）")

    # 位置先验（AGENTS.md §四）：只决定**扫描顺序**和**页眉条几何**，不作为结论。
    # 每个命中仍由本页页眉条 OCR 现场证实；先验错了 probe_fallback + 兜底阶梯照旧全覆盖。
    locate_prior_file = Path(a.locate_prior or loc.get("locate_prior")
                             or (C.CONFIG_DIR / "locate_prior.yaml"))
    locate_prior = load_locate_prior(locate_prior_file)
    if locate_prior:
        C.log(f"位置先验：{locate_prior_file}"
              f"（{len(locate_prior.get('hospitals') or {})} 个院区）"
              " —— 只排序/选几何，不作结论")

    prior_path = Path(a.prior_index) if a.prior_index else \
        next((p for p in sorted((batch_dir / "00_index").glob("*DEPRECATED*.json"))), None)
    prior_cases = C.read_json(prior_path).get("cases", {}) if prior_path and \
        Path(prior_path).exists() else {}
    if prior_cases:
        C.log(f"扫描顺序先验：{prior_path}（{len(prior_cases)} 例）—— 只排序，不作结论")

    # 缓存目录与键材料里的「生效参数」—— 任何一个变了都必须算未命中
    cache_dir = Path(a.cache_dir) if a.cache_dir else \
        Path(cfg["paths"]["run_root"]) / CACHE_DIR_NAME
    loc_args = {
        "strip_frac": a.strip_frac, "strip_positions": a.strip_positions,
        "fallback_frac": a.fallback_frac, "fallback_positions": a.fallback_positions,
        "escalate_positions": a.escalate_positions,
        "max_scan_pages": a.max_scan_pages, "grace_pages": a.grace_pages,
        "grace_rank": a.grace_rank, "grace_pages_hard": a.grace_pages_hard,
        "geometry_preprobe": preprobe,
        # 先验表内容进缓存键：改了先验（几何/页距）必须算未命中，不能让旧缓存冒充提速
        "locate_prior_sha256": (C.sha256_file(locate_prior_file)
                                if Path(locate_prior_file).exists() else "none"),
        "ring": a.ring, "try_first": a.try_first, "no_early_exit": a.no_early_exit,
        "max_frontpage": a.max_frontpage, "max_first_progress": a.max_first_progress,
        "follow": follow, "frontpage_keywords": kw_fp, "first_progress_keywords": kw_pn,
        "first_progress_priority": prio,
        # 探测并发度进缓存键：它改变**扫了哪些页**（done() 早停时预取的页已被扫过），
        # 不能让并发批的缓存冒充串行批的结果。
        "probe_concurrency": probe_conc,
        "frontpage_continuation_markers": list(loc.get("frontpage_continuation_markers") or []),
        "blank_guard": cfg["blank_guard"],
    }

    rows = S.read_manifest(batch_dir / "00_index" / "manifest.tsv")
    want = list(a.case or [])
    if a.case_file:
        want += [x.strip() for x in Path(a.case_file).read_text(encoding="utf-8").splitlines()
                 if x.strip()]
    if want:
        rows = [r for r in rows if r["case_id"] in set(want)]
    if not rows:
        C.err("manifest 里没有匹配的例")
        return 2

    existing = merge_shards()
    if not existing and out_path.exists():
        try:
            existing = C.read_json(out_path).get("cases", {})
        except Exception:                                              # noqa: BLE001
            C.warn(f"{out_path} 读不动，按空索引继续")

    t_all = time.time()
    for r in rows:
        case = r["case_id"]
        S.set_audit_case(case)
        pdf = pdfium.PdfDocument(r["pdf_path"])
        n = len(pdf)
        key = cache_key(case, r["pdf_path"], loc_args, prior_path)
        cf = cache_dir / f"{case}__{key['key'][:16]}.json"
        if not a.no_cache and cf.exists():
            try:
                blob = C.read_json(cf)
            except Exception as e:                                       # noqa: BLE001
                blob = {}
                C.warn(f"{case}: 缓存 {cf.name} 读不动（{type(e).__name__}）→ 当未命中")
            ok, why = cache_is_complete(blob.get("entry"), n, r["pdf_path"]) \
                if blob else (False, "缓存文件读不动/为空")
            if ok and blob.get("key") != key["key"]:
                ok, why = False, "键不匹配（PDF / pipeline.yaml / 代码 / 定位参数 / 先验索引 任一变化）"
            if ok:
                e2 = dict(blob["entry"])
                e2["locate_cache"] = {"hit": True, "key": key["key"],
                                      "built": blob.get("built"), "cache_file": cf.name}
                existing[case] = e2
                pdf.close()
                C.log(f"{case}: 定位缓存命中（{cf.name}）—— 跳过全部页眉条 OCR；"
                      f"首页 {e2['frontpage_pages']} / 首程 {e2['first_progress_pages']}"
                      f"（缓存建于 {blob.get('built')}）")
                write_shard(case, e2)
                continue
            C.warn(f"{case}: 定位缓存未命中（{why}）→ 现场重扫")
        fp: list[int] = []
        pn: list[int] = []
        evidence: list[dict] = []
        strips: dict[int, str] = {}
        strip_full: dict[int, str] = {}
        strip_all: dict[int, list] = {}   # page -> 各几何窄条文本（续页判据要用全部几何）
        pn_kw: dict[int, list] = {}
        blank: list[int] = []
        blank_set: set[int] = set()      # 空白判定与条几何无关 → 换几何时不必重扫
        skip_kind: dict[int, str] = {}
        sig: dict[int, str] = {}         # page -> 最后一次扫描用的页眉条几何（scan_sig）
        visited: dict[str, int] = {}     # 几何签名 -> 该几何下**访问过**的页数（含判空白页）
        cur_sig = [""]                   # 当前正在扫描的几何
        ocr_calls = [0]                  # 页眉条 OCR **次数**（≠ pages_scanned：兜底轮会重扫同页）
        scan_budget_hit = [False]
        trace: list[dict] = []
        probe = {"pn_tried": 0, "pn_expansions": 0, "fallback_used": False,
                 "group": None, "anchor": None}
        probe["geometry_preprobe"] = None
        pred_fp, pred_pn, prior_src = prior_positions(prior_cases, case, n)
        pr, hosp_key, pr_hit = prior_row(locate_prior, case)

        def best_pn_rank() -> int:
            """已命中首程写法里**最可信**的那一档（prio 下标，越小越可信；无命中=99）。"""
            r = 99
            for p in pn:
                for k in (pn_kw.get(p) or []):
                    r = min(r, prio_rank.get(k, 99))
            return r

        def done() -> bool:
            """早停：首页命中 + 首页续页已扫 + 最高优先级首程命中。
            退让（grace）：有些院区根本不写「首次病程记录」，死等 top_kw 会全扫到底
            （实测 266 页的安吉例 ≈45 min）。已有次优首程命中且扫过 grace_pages 页 → 停，
            并在索引里留 grace_used，报告里如实说明首程窗口可能不是最高优先级写法。"""
            if not fp:
                return False
            for p in fp:
                for q in (p - 1, p + 1):          # 续页可能在标题页前（实测奉化 008）
                    if 0 <= q < n and q not in strips and q not in blank:
                        return False
            if bool(top_kw) and any(top_kw in (pn_kw.get(p) or []) for p in pn):
                return True
            if not a.grace_pages or not pn:
                return False
            # ★ 两处修正（安吉首程 bug，见 reports/speedup-round2-20261009.md 步①）：
            #   1) 页数按**当前几何**计。旧写法 len(strips)+len(blank) 是跨几何累计，
            #      top 轮已把页数计满 → bottom 兜底刚扫 2 页就判停（实测 speed10b
            #      安吉 09162565：escalation ocr_calls=2、grace_used=true、27 页候选从未扫过）。
            #   2) 加 rank 门槛。只有够可信的写法（<= grace_rank）才按 grace_pages 退让；
            #      只剩兜底写法（如「入院记录」）时要扫 grace_pages_hard 页才肯退让。
            here = visited.get(cur_sig[0], 0)
            need = a.grace_pages if best_pn_rank() <= a.grace_rank else a.grace_pages_hard
            return here >= need

        # 兜底阶梯的收工判据 = done()：早停判据本身已含 grace 退让，
        # 用它就不会出现「scan() 里已判停、阶梯却又换几何重扫一遍」的错配。

        def scan(frac: float, only: list[int], positions: tuple = ("top",),
                 phase: str = "ring") -> None:
            """扫一批页。**处理序 = 传入序**，与串行完全一致；并发只发生在
            「把接下来 k 页的页眉条 OCR 同时发出去」这一层（IO 等待重叠）。

            能并行 / 不能并行的边界（本轮任务第 4 件）：
              ✔ 同一批候选页的页眉条 OCR —— 彼此无依赖 → 预取 k 页并发。
              ✘ 首页 → 首程窗口：阶段 B 的候选列表由阶段 A 的锚点算出来，
                锚点又由阶段 A 的命中决定 → 顺序依赖，不能并行。
              ✘ 命中后追加的邻页（queue.append(p±1)）：依赖本页结果 → 不进预取，
                留到下一轮再发（所以预取窗口只取**已在队列里**的页）。
              ✘ done() 早停 / 扫描上限：仍在**处理序**上逐页判 → 预取回来的多余
                结果直接丢弃（代价是请求数变多，见报告口径 A/B；产物逐字节不变）。
            """
            queue = list(only)
            sig_now = scan_sig(frac, positions)
            cur_sig[0] = sig_now

            def drop_unprocessed(take: list, got: dict, start: int, why: str) -> None:
                """预取窗口里**已发 OCR 但从未被处理**的页 → 记进 scan_trace。

                break 发生在 take[start]（`i += 1` 在其之前已自增），
                所以未消费页 = take[start:]。只有 kind=="ok" 的页真发过请求。
                """
                for q in take[start:]:
                    if (got.get(q) or {}).get("kind") != "ok":
                        continue
                    trace.append({"page": q, "phase": phase, "discarded": why,
                                  "strip": scan_sig(frac, positions)})

            i, stop = 0, False
            while i < len(queue) and not stop:
                if not a.no_early_exit and done():
                    break
                if a.max_scan_pages and len(strips) + len(blank) >= a.max_scan_pages:
                    C.warn(f"{case}: 达到扫描上限 {a.max_scan_pages} 页，停止扫描"
                           f"（首页 {sorted(fp)} / 首程 {sorted(pn)}）")
                    scan_budget_hit[0] = True
                    break
                # —— 预取窗口：队列里接下来 k 页（k=1 时与旧写法逐字节等价）——
                take = [queue[i]]
                j = i + 1
                while probe_conc > 1 and len(take) < probe_conc and j < len(queue):
                    take.append(queue[j])
                    j += 1
                got, n_ocr = probe_batch(pdf, take, frac, positions, cfg, probe_conc)
                ocr_calls[0] += n_ocr
                for idx, p in enumerate(take):
                    i += 1
                    if not a.no_early_exit and done():
                        drop_unprocessed(take, got, idx, "early_exit")
                        stop = True
                        break
                    if a.max_scan_pages and len(strips) + len(blank) >= a.max_scan_pages:
                        C.warn(f"{case}: 达到扫描上限 {a.max_scan_pages} 页，停止扫描"
                               f"（首页 {sorted(fp)} / 首程 {sorted(pn)}）")
                        scan_budget_hit[0] = True
                        drop_unprocessed(take, got, idx, "scan_budget")
                        stop = True
                        break
                    if (len(strips) + len(blank)) and (len(strips) + len(blank)) % 25 == 0:
                        C.log(f"  {case}: 已扫 {len(strips) + len(blank)}/{n} 页"
                              f"（首页 {sorted(fp)} / 首程 {sorted(pn)}）", quiet=a.quiet)
                    r = got.get(p) or {}
                    if r.get("render_error"):
                        C.err(f"[{case}] page {p} 渲染失败: {r['render_error']}")
                        trace.append({"page": p, "phase": phase,
                                      "error": r["render_error"].split(":")[0]})
                        continue
                    visited[sig_now] = visited.get(sig_now, 0) + 1
                    kind = r["kind"]
                    if kind != "ok":               # blank / low_info 都不送 OCR（风险②）
                        if p not in blank_set:     # 按页去重（旧写法兜底轮重复计数）
                            blank.append(p)
                            blank_set.add(p)
                        skip_kind[p] = kind
                        trace.append({"page": p, "phase": phase, "skipped": kind,
                                      "strip": scan_sig(frac, positions)})
                        continue
                    txt = r["txt"]
                    sig[p] = scan_sig(frac, positions)
                    hit_fp = [k for k in kw_fp if k in txt]
                    hit_pn = [k for k in kw_pn if k in txt]
                    strips[p] = txt[:80].replace("\n", " ")
                    strip_full[p] = txt
                    strip_all.setdefault(p, []).append(txt)
                    pn_kw[p] = hit_pn
                    trace.append({"page": p, "phase": phase,
                                  "strip": scan_sig(frac, positions),
                                  "hit": (["FP:" + k for k in hit_fp]
                                          + ["PN:" + k for k in hit_pn])})
                    if hit_fp and p not in fp:
                        fp.append(p)
                    if hit_pn and p not in pn:
                        pn.append(p)
                    if hit_fp or hit_pn:
                        evidence.append({"page": p, "keywords": hit_fp + hit_pn,
                                         "strip_frac": frac,
                                         "strip_sha256": C.sha256_text(txt),
                                         "snippet": txt[:60].replace("\n", " ")})
                        audit.emit(case_id=case, page=p, kind="locate", strip_frac=frac,
                                   keywords=hit_fp + hit_pn,
                                   strip_text_sha256=C.sha256_text(txt))
                        if hit_fp and (p + 1) not in queue and p + 1 < n:
                            queue.append(p + 1)    # 首页常跨两页 → 邻页必须扫
                        if hit_fp and (p - 1) not in queue and p - 1 >= 0:
                            queue.append(p - 1)    # 实测续页也可能在标题页**前**面
                    C.log(f"  {case} p{p + 1:04d} {'FP' if hit_fp else ''}"
                          f"{'PN' if hit_pn else ''}", quiet=not (hit_fp or hit_pn) or a.quiet)

        markers = list(loc.get("frontpage_continuation_markers") or [])
        order = scan_order(n, pred_fp, pred_pn, a.ring)
        if manual_seed:
            order = [p for p in manual_seed if 0 <= p < n] + \
                    [p for p in order if p not in manual_seed]
        strip_pos = tuple(x.strip() for x in a.strip_positions.split(",") if x.strip())
        fb_pos = tuple(x.strip() for x in a.fallback_positions.split(",") if x.strip())
        # 页眉条几何**按文书类型分开**：首页与首程的页眉位置可以不在同一边。
        # 这是「45%×top+bottom 兜底白烧」回归的正解 —— 旧写法预探针一命中就把
        # **全局** strip_pos 翻成 bottom，于是标题在页眉顶部的首程在阶段 B 永远扫不中
        # （桐乡 00335376：bottom 窄条 23+10 次 0 命中），又因为 strip_pos 已含 bottom
        # 把便宜的 20%×bottom 级过滤掉，直接跳到最贵的 45%×top+bottom 重扫 35 页。
        fp_pos = geom_tuple((pr.get("frontpage") or {}).get("geometry"), strip_pos)
        pn_pos = geom_tuple((pr.get("first_progress") or {}).get("geometry"), strip_pos)
        if a.no_probe:
            scan(a.strip_frac, order, strip_pos, phase="ring")
        else:
            # 阶段 A：首页（命中即停，不试后面的候选组）
            for gi, grp in enumerate(fp_probe_groups(n, pred_fp,
                                                     pr if pr_hit else None)):
                if fp:
                    break
                todo = [p for p in grp if p not in strips and p not in blank]
                scan(a.strip_frac, todo, fp_pos, phase=f"fp_probe{gi}")
                # 几何预探针：top 找不到首页 → 对**同一批页**换另一边再试一次。
                # 实测安吉页眉在页底（y≈0.85-0.89），top 窄条结构上看不见标题，
                # 于是阶段 A/B 全空、只能靠兜底阶梯（speed10b 安吉两例 31/33 次页眉条 OCR）。
                # ★ 闸门（回归三例桐乡 00335376 / 玉环 201020 / 玉环 207232 的根因）：
                #   先验已说明该院区是 head 组（首页就在第 0-1 页、页眉在顶部）时**不启用**
                #   预探针 —— 切到 bottom 换不到任何命中，只会把几何翻错、拖垮阶段 B。
                alt = None
                if not fp and preprobe and todo:
                    if pr_hit and pr.get("group") == "head":
                        probe["geometry_preprobe"] = {
                            "skipped": f"prior_group=head（院区 {hosp_key}）"
                                       " —— 首页本就在页首，切边无意义"}
                    else:
                        alt = ("bottom",) if "top" in fp_pos else ("top",)
                        if alt == fp_pos:
                            alt = None
                if alt:
                    again = [p for p in todo if p in strips]
                    if again:
                        scan(a.strip_frac, again, alt,
                             phase=f"fp_probe{gi}_preprobe_{'-'.join(alt)}")
                        if fp:
                            fp_pos = pn_pos = alt
                            probe["geometry_preprobe"] = {
                                "pages": sorted(again), "switch_to": "-".join(alt),
                                "ocr_calls": len(again)}
                            C.warn(f"{case}: 页眉几何预探针 —— top 窄条 0 命中、"
                                   f"{'-'.join(alt)} 窄条命中首页 {sorted(fp)} → 后续扫描改用 "
                                   f"{int(a.strip_frac * 100)}%×{'-'.join(alt)}")
            # 阶段 B：首程（按首页锚点的实测偏移猜页）
            if fp:
                anchor = min(fp)
                q = anchor - 1
                if 0 <= q < n and q in strip_full and not pn_kw.get(q) and \
                        any(k in "\n".join(strip_all.get(q) or [strip_full[q]])
                            for k in markers):
                    anchor = q          # 续页在标题页**前**（实测奉化 008）→ 锚点用续页
                probe["anchor"] = anchor
                probe["group"] = "head" if min(fp) <= 2 else "tail"
                if pr_hit and pr.get("group") in ("head", "tail") and \
                        pr.get("group") != probe["group"]:
                    C.warn(f"{case}: 位置先验说 {pr.get('group')} 组、实测 {probe['group']} 组"
                           f"（院区 {hosp_key}）—— 按实测走，先验只排序不作结论")
                    probe["prior_group_mismatch"] = {"prior": pr.get("group"),
                                                     "observed": probe["group"]}
                # 候选列表由**阶段 A 的锚点**算出来 → 阶段 A/B 之间是顺序依赖，不能并行。
                # 能并行的只有「同一批候选页的页眉条探测」：按 probe_conc 分批交给 scan，
                # scan 内部按传入序处理（done()/top_kw 早停位置不变），只是把这几页的
                # OCR 请求同时发出。代价：早停时预取的那几页白扫 → 请求数上升。
                cands = [p for p in pn_probe_order(n, anchor, probe["group"],
                                                   a.pn_window, pr if pr_hit else None)
                         if p not in strips and p not in blank]
                k = max(1, probe_conc)
                for ci in range(0, len(cands), k):
                    chunk = cands[ci:ci + k]
                    if done():
                        break
                    if top_kw and any(top_kw in (pn_kw.get(x) or []) for x in pn):
                        break
                    for _ in chunk:
                        probe["pn_tried"] += 1
                        if probe["pn_tried"] > 12:
                            probe["pn_expansions"] += 1
                    scan(a.strip_frac, chunk, pn_pos, phase="pn_probe")
            # 阶段 B 之后（兜底阶梯 / probe_fallback / loc_src）沿用首程那一档几何：
            # 首程才是定位的主要缺口，兜底要围着它换边/加高。
            strip_pos = pn_pos
            # 探针没拿全 → 回到旧顺序全扫兜底（**不丢内容**）
            if not fp or not (top_kw and any(top_kw in (pn_kw.get(p) or []) for p in pn)):
                probe["fallback_used"] = True
                rest = [p for p in order if p not in strips and p not in blank]
                scan(a.strip_frac, rest, strip_pos, phase="probe_fallback")
        # ---- 兜底阶梯：定位不全 → 逐级换边/加高，**重扫同一批页**（按几何签名判覆盖）----
        # 旧写法 `rest = [p for p in order if p not in strips]` 只会剩下已判空白的页，
        # 而这些页换几何必然再判空 → 兜底轮 0 次 OCR（安吉两例的首页就是这么丢的）。
        escalation: list[dict] = []
        escalation_stopped = [""]        # 兜底提前收手的原因（no_gain / 空=跑完了所有级）
        rungs: list[tuple] = []
        for pos in [x.strip() for x in a.escalate_positions.split(",") if x.strip()]:
            if pos not in strip_pos:
                rungs.append((a.strip_frac, (pos,)))      # 便宜级：只换边，不加高
        if a.fallback_frac > a.strip_frac:
            rungs.append((a.fallback_frac, fb_pos))       # 最后手段：加高 + 上下都裁
        for rung_i, (frac, pos) in enumerate(rungs):
            if done():
                break
            want = scan_sig(frac, pos)
            rest = [p for p in order if p not in blank_set and sig.get(p) != want]
            if not rest:
                continue
            phase = f"escalate_{int(frac * 100)}pct_{'-'.join(pos)}"
            # ---- round22 step5 stop-loss draft (env gated, 0=off; draft only) ----
            esc_cap = int(os.environ.get("R22_ESC_MAX_CALLS", "0") or 0)
            esc_win = int(os.environ.get("R22_ESC_WINDOW", "0") or 0)
            if esc_win > 0 and probe.get("anchor") is not None:
                anchor = probe["anchor"]
                inside = [p for p in rest if abs(p - anchor) <= esc_win]
                if inside and len(inside) < len(rest):
                    C.warn(f"{case}: {phase} cand {len(rest)} -> anchor {anchor}+-{esc_win} "
                           f"keeps {len(inside)} (round22 stop-loss)")
                    rest = inside
            esc_used = sum(int(e["ocr_calls"]) for e in escalation)
            if esc_cap > 0 and esc_used >= esc_cap:
                C.warn(f"{case}: {phase} escalation calls {esc_used} >= cap {esc_cap} -> stop")
                escalation_stopped[0] = f"{phase}:esc_budget"
                break
            if esc_cap > 0 and len(rest) > esc_cap - esc_used:
                rest = rest[: esc_cap - esc_used]
            before = ocr_calls[0]
            hits_before = len(fp) + len(pn)
            C.warn(f"{case}: {int(a.strip_frac * 100)}%×{'+'.join(strip_pos)} 定位不全"
                   f"（首页 {sorted(fp)} / 首程 {sorted(pn)}）"
                   f"→ 换 {int(frac * 100)}%×{'+'.join(pos)} 重扫 {len(rest)} 页"
                   f"（已判空白的 {len(blank_set)} 页不重扫：判定用整页、与条几何无关）")
            scan(frac, rest, pos, phase=phase)
            escalation.append({"phase": phase, "strip_frac": frac, "positions": list(pos),
                               "pages_offered": len(rest),
                               "ocr_calls": ocr_calls[0] - before})
            # 这一级整批重扫连一个关键词都没多出来 → 别再去付更贵那级的钱。
            # 实测 45%×top+bottom 10.7-16s/页，是 bottom20% 的 3-4 倍；
            # 「本来就没有首页」的例继续扫下去 = 批量最坏 +50 min/例，换不到任何东西。
            if len(fp) + len(pn) == hits_before and rung_i + 1 < len(rungs):
                nxt_frac, nxt_pos = rungs[rung_i + 1]
                C.warn(f"{case}: {phase} 重扫 {len(rest)} 页仍 0 命中 → 不再试更贵的 "
                       f"{int(nxt_frac * 100)}%×{'+'.join(nxt_pos)}（记 escalation_stopped）")
                escalation_stopped[0] = f"{phase}:no_gain"
                break
        loc_src = (f"strip_ocr:{int(a.strip_frac * 100)}%({'+'.join(strip_pos)})"
                   + "".join(f"+{int(e['strip_frac'] * 100)}%({'+'.join(e['positions'])})"
                             f"×{e['ocr_calls']}ocr" for e in escalation)
                   + ("+early_exit" if not a.no_early_exit else ""))
        pdf.close()

        # ---- 首页续页：无标题但明显属于首页表格的邻页（风险①的完整性）----
        fp_cont: list[int] = []
        for p in fp:
            for q in (p - 1, p + 1):
                if not (0 <= q < n) or q in fp or q in fp_cont or q not in strip_full:
                    continue
                if pn_kw.get(q):                   # 邻页是别的文书标题（入院记录等）→ 不算续页
                    continue
                got = [k for k in markers if k in "\n".join(strip_all.get(q) or [strip_full[q]])]
                if got:
                    fp_cont.append(q)
                    evidence.append({"page": q, "keywords": got, "strip_frac": a.strip_frac,
                                     "strip_sha256": C.sha256_text(strip_full[q]),
                                     "role": "frontpage_continuation",
                                     "snippet": strip_full[q][:60].replace("\n", " ")})
                    audit.emit(case_id=case, page=q, kind="locate_continuation",
                               keywords=got, strip_text_sha256=C.sha256_text(strip_full[q]))
        fp_pages = sorted(set(fp) | set(fp_cont))[:a.max_frontpage]
        if fp_cont:
            C.log(f"  {case}: 首页续页（无标题）{sorted(fp_cont)} 已并入首页页集")

        # 首程窗口：按写法优先级挑标题页（同优先级取页号最小），再往后带 follow 页
        pn_sorted = sorted(pn, key=lambda p: (min([prio_rank.get(k, 99)
                                                   for k in pn_kw.get(p, [])] or [99]), p))
        pn_window: list[int] = []
        for h in pn_sorted[:1]:
            pn_window += [q for q in range(h, min(n, h + 1 + follow))
                          if q not in fp and q not in pn_window]
        pn_window = sorted(pn_window)[:a.max_first_progress + follow] if follow else \
            sorted(pn_window)[:max(1, a.max_first_progress)]
        # round42o：pn_sorted[0] 可能是「靠 follow/续页带进来的页」，pn_kw 里**没有关键词**
        # ⇒ min([]) 直接 ValueError，整例在阶段⓪崩掉，后面①②③根本没跑（本批 4 例）。
        pn_kind = (min(pn_kw[pn_sorted[0]], key=lambda k: prio_rank.get(k, 99))
                   if pn_sorted and pn_kw.get(pn_sorted[0]) else None)

        # 扫页记录（红线：每例必须能查出「扫了哪几页、第几页命中、扩了几次」）
        by_phase: dict[str, dict] = {}
        for t in trace:
            d = by_phase.setdefault(t.get("phase", "?"),
                                    {"pages": [], "hits": [], "skipped": []})
            if "page" in t and not t.get("discarded"):
                d["pages"].append(t["page"])
            if t.get("discarded"):
                d.setdefault("discarded", []).append(
                    {"page": t["page"], "why": t["discarded"]})
            if t.get("hit"):
                d["hits"].append({"page": t["page"], "hit": t["hit"]})
            if t.get("skipped"):
                d["skipped"].append({"page": t["page"], "kind": t["skipped"]})

        existing[case] = {
            "frontpage_pages": fp_pages,
            "frontpage_title_pages": sorted(fp),
            "frontpage_continuation_pages": sorted(fp_cont),
            "first_progress_pages": pn_window,
            "frontpage_all_hits": sorted(fp), "first_progress_all_hits": sorted(pn),
            "first_progress_title_pages": pn_sorted[:a.max_first_progress],
            "first_progress_kind": pn_kind,
            "source_pages": n, "pages_scanned": len(strips), "pages_blank_skipped": len(blank),
            "pages_skipped_kind": skip_kind,
            "scan_stopped_early": bool(not a.no_early_exit and len(strips) + len(blank) < n),
            "scan_order_prior": prior_src, "pred_pages": [pred_fp, pred_pn],
            "locate_prior": {
                "file": str(locate_prior_file), "hospital": hosp_key,
                "matched": pr_hit, "group": pr.get("group"),
                "frontpage_candidates": list(
                    ((pr.get("frontpage") or {}).get("candidates") or [])),
                "frontpage_geometry": list(fp_pos),
                "first_progress_offsets": list(
                    ((pr.get("first_progress") or {}).get("offsets") or [])),
                "first_progress_geometry": list(pn_pos),
                "prior_samples": pr.get("samples"),
                "preprobe_enabled": bool(preprobe and not (
                    pr_hit and pr.get("group") == "head")),
            },
            "scan_summary": by_phase,
            "manual_seed_pages": manual_seed,
            "scan_budget_hit": scan_budget_hit[0],
            "grace_pages": a.grace_pages,
            "grace_rank": a.grace_rank, "grace_pages_hard": a.grace_pages_hard,
            "first_progress_best_rank": (best_pn_rank() if pn else None),
            "strip_ocr_calls_by_geometry": {k: v for k, v in visited.items()},
            "grace_used": bool(a.grace_pages) and not (
                top_kw and any(top_kw in (pn_kw.get(p) or []) for p in pn)),
            "evidence": evidence,
            "strips": {str(k): v for k, v in strips.items()},
            "strip_ocr_calls": ocr_calls[0],
            "escalation": escalation,
            "escalation_stopped": escalation_stopped[0] or None,
            "scan_trace": trace,
            "fp_hit_page": (min(fp) if fp else None),
            "pn_hit_page": (pn_sorted[0] if pn_sorted else None),
            "probe": probe,
            "locate_cache": {"hit": False, "key": key["key"],
                             "cache_file": cf.name if fp else ""},
            "locator_source": loc_src,
        }
        if not fp_pages:
            C.err(f"{case}: 定位失败 —— 扫了 {len(strips)}/{n} 页都没找到首页关键词，"
                  f"该例的首页列将全部为 null")
        C.log(f"{case}: 源 {n} 页 / 扫 {len(strips)} 页（空白跳过 {len(blank)}）→ 首页 "
              f"{existing[case]['frontpage_pages']}（标题 {sorted(fp)}/续页 {sorted(fp_cont)}）"
              f" / 首程 {pn_window}（{pn_kind}）")
        if not a.no_probe:
            C.log(f"  {case}: 探针 组={probe['group']} 锚点={probe['anchor']} "
                  f"首程试={probe['pn_tried']} 外扩={probe['pn_expansions']} "
                  f"兜底={'是' if probe['fallback_used'] else '否'}")
            C.log(f"  {case}: 先验 院区={hosp_key} 组={pr.get('group')} "
                  f"在表={'是' if pr_hit else '否'} 首页几何={'-'.join(fp_pos)} "
                  f"首程几何={'-'.join(pn_pos)} 页眉条OCR={ocr_calls[0]}次")
            for ph, d in by_phase.items():
                C.log(f"    {case} {ph}: 扫 {sorted(set(d['pages']))} "
                      f"命中 {[(h['page'], h['hit']) for h in d['hits']]}"
                      f"{' 跳过 ' + str([s['page'] for s in d['skipped']]) if d['skipped'] else ''}")
        # 只有**定位成功**才写缓存：定位失败的例下次仍应现场重扫，不能把失败固化
        if fp_pages:
            cache_dir.mkdir(parents=True, exist_ok=True)
            C.write_json(cf, {"key": key["key"], "key_parts": key["parts"],
                              "built": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                              "case_id": case, "entry": existing[case]})
            C.log(f"  {case}: 定位缓存已写 {cf.name}")
        write_shard(case, existing[case])

    # ★ 「用时」以前用的是 t_all（:598 起算，含逐例 OCR/定位）⇒ 把 47 KB 的写盘夸大成几十秒。
    #   分开计时：合并分片 / 写盘 / 整轮，三个数各归各，别再指着写盘说它慢。
    t_b = time.perf_counter()
    merged = merge_shards()
    t_w = time.perf_counter()
    write_json_locked(out_path, {"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            "source": "strip_ocr_shards",
                            "strip_frac": a.strip_frac, "fallback_frac": a.fallback_frac,
                            "frontpage_keywords": kw_fp, "first_progress_keywords": kw_pn,
                            "first_progress_priority": prio, "page_base": 0,
                            "early_exit": not a.no_early_exit,
                            "cases": merged})
    t_wrote = time.perf_counter() - t_w
    if shard_dir.exists():
        C.log(f"批级索引由 {len(merged)} 个分片合并 → {out_path}")
    C.log(f"写入 {out_path}（合并分片 {t_w - t_b:.2f}s + 写盘 {t_wrote:.2f}s；"
          f"本次 locate 全程 {time.time() - t_all:.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
