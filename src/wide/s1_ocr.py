#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段① s1_ocr.py —— PDF → 逐页 PNG → PaddleX 版面 OCR → 每页 JSON。

输入   run/wide/<batch>/00_index/manifest.tsv   （由 --discover 生成）
       run/wide/<batch>/00_index/page_index.json（定位缓存，可选）
输出   run/wide/<batch>/01_ocr/<case_id>/page_NNNN.png   渲染页
       run/wide/<batch>/01_ocr/<case_id>/page_NNNN.json  该页 OCR 结果 + 度量
       run/wide/<batch>/01_ocr/<case_id>/_manifest.json  页数对账
       run/wide/<batch>/01_ocr/<case_id>/.done | .incomplete
       run/wide/<batch>/_audit.jsonl             审计（输入哈希 / 输出哈希）

三个风险的落点：
  ① 首页是表格 → 默认 useLayoutDetection=true，块带 block_bbox，下游可按坐标配对。
  ② 空白页幻觉 → 渲染后先量墨迹/信息量，低信息页**不送 OCR**，标 blank/low_info。
  ③ 退化裁剪框丢页 → 块裁剪走 common.safe_crop（退化回退整页）；
     产物页数 ≠ 源页数 → 写 .incomplete 并非零退出；异常一律进 stderr（--quiet 压不掉）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
import concurrent.futures as cf
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wide import common as C  # noqa: E402

STAGE = "01_ocr"
MANIFEST_COLS = ["case_id", "source_dir", "input_file", "pdf_path", "n_pages",
                 "frontpage_pages", "first_progress_pages"]


# --------------------------------------------------------------- 发现病例
def discover(data_root: Path) -> list[dict]:
    """data_root 下所有 PDF → 病例清单。深度固定 3（院区/病例/文件）。"""
    rows = []
    seen: dict[str, int] = {}
    # round34 E4：扫描根本身就是病例目录（叶子目录直接当 case 跑）时 rel.parent == "."
    # ⇒ case_id == "." ⇒ 该例产物直接落进**阶段根**（01_ocr/_manifest.json、01_ocr/.done）
    # 并和别的例互相覆盖（round33 C2 首跑 922 s 因此作废）。
    # 修法：用「根的上一级 __ 根名」补回 院区__病例 两级 ⇒ 与全根跑法出**同一个** case_id。
    root_p = Path(data_root).resolve()
    leaf_fallback = C.case_id_from_rel(f"{root_p.parent.name}/{root_p.name}")
    leaf_warned = False
    for pdf in sorted(Path(data_root).rglob("*.pdf")):
        rel = pdf.relative_to(data_root)
        src_dir = str(rel.parent)
        base = C.case_id_from_rel(src_dir)
        if base.strip() in ("", ".", ".."):
            base = leaf_fallback
            # round38 E4b：source_dir 也补回，否则「标准化就诊医院」注入不到 ⇒ 去重键少分量
            src_dir = f"{root_p.parent.name}/{root_p.name}"
            if not leaf_warned:
                C.warn(f"discover：扫描根 {root_p} 本身就是病例目录（rel.parent='.'）"
                       f"⇒ case_id 用目录名补回 = {base!r}；跑单例更稳的写法是 "
                       f"`medrec run --case {base} --batch TAG`")
                leaf_warned = True
        # 一个目录里可能放多份 PDF（实测玉环第一 20 个目录 / 26 份）—— 一例 = 一份 PDF，
        # 撞名就在 case_id 后面补上文件名，保证唯一。
        cid = base
        if seen.get(base):
            cid = f"{base}__{C.case_id_from_rel(pdf.stem)}"
        seen[base] = seen.get(base, 0) + 1
        rows.append({
            "case_id": cid,
            "source_dir": src_dir,
            "input_file": pdf.name,
            "pdf_path": str(pdf),
            "n_pages": "",
            "frontpage_pages": "",
            "first_progress_pages": "",
        })
    return rows


def write_manifest(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(MANIFEST_COLS)]
    lines += ["\t".join(str(r.get(c, "")) for c in MANIFEST_COLS) for r in rows]
    C.write_text(path, "\n".join(lines) + "\n")


def read_manifest(path: Path) -> list[dict]:
    lines = Path(path).read_text(encoding="utf-8").rstrip("\n").split("\n")
    head = lines[0].split("\t")
    return [dict(zip(head, ln.split("\t"))) for ln in lines[1:] if ln.strip()]


# --------------------------------------------------------------- 渲染 + 空白守卫
def render_page(pdf, page_no: int, render_cfg: dict):
    """渲染一页为 PIL.Image。缩放三重约束沿用 pdf2png.py 的实测校正。"""
    import pypdfium2 as pdfium
    from PIL import Image
    page = pdf[page_no]
    w_pt, h_pt = page.get_size()
    target_px = float(render_cfg["target_px"])
    scale = min((target_px / (w_pt * h_pt)) ** 0.5,
                float(render_cfg["max_upscale"]),
                float(render_cfg["max_side_pt"]) / max(w_pt, h_pt))
    scale = max(scale, float(render_cfg["min_scale"]))
    bmp = page.render(scale=scale)
    arr = bmp.to_numpy()
    if arr.ndim == 2:
        arr = arr[..., None]
    return Image.fromarray(arr).convert("RGB"), scale


def page_metrics(img, blank_cfg: dict) -> dict:
    """墨迹覆盖率 + 灰度标准差 —— 空白/低信息页判定的唯一依据。"""
    import numpy as np
    a = np.asarray(img.convert("L"), dtype=np.float32)
    dark = float(blank_cfg["dark_threshold"])
    return {"ink_ratio": round(float((a < dark).mean()), 6),
            "gray_std": round(float(a.std()), 2),
            "width": img.size[0], "height": img.size[1]}


def classify_page(m: dict, blank_cfg: dict) -> str:
    """ok / blank / low_info —— 后两者不送 OCR（风险②）。"""
    if m["ink_ratio"] < float(blank_cfg["ink_min"]):
        return "blank"
    if m["gray_std"] < float(blank_cfg["std_min"]):
        return "low_info"
    if m["ink_ratio"] < float(blank_cfg["low_info_ink_min"]):
        return "low_info"
    return "ok"


# --------------------------------------------------------------- OCR
def ocr_page(png_path: Path, ocr_cfg: dict) -> tuple[dict | None, bytes | None, str]:
    return ocr_page_variant(png_path, ocr_cfg, bool(ocr_cfg["use_layout_detection"]))


def resolve_backend(ocr_cfg: dict, stage: str = "") -> str:
    """OCR 通道选择：ocr.backend 是全局默认，ocr.backends.<stage> 按阶段覆盖（混合方案）。
    stage ∈ locate / frontpage / first_progress；覆盖写空串 = 继承默认。"""
    base = str(ocr_cfg.get("backend") or "paddlex").strip().lower()
    over = str((ocr_cfg.get("backends") or {}).get(stage or "") or "").strip().lower()
    return over or base


def vllm_prompt_for(ocr_cfg: dict, variant: str = "", stage: str = "") -> str:
    """vLLM 是纯文本通道，prompt 决定它怎么排版。查找顺序：变体名 → 阶段 → default。"""
    prompts = ocr_cfg.get("vllm_prompts") or {}
    for key in (variant, stage):
        val = prompts.get(key or "")
        if val:
            return str(val)
    # round43 R3：config/pipeline.yaml:151 明写 vllm_prompt 是「默认 prompt
    # （变体/阶段未命中时用）」，但这里只读 vllm_prompts（复数）⇒ 改它不生效。
    # 现网 vllm_prompt="OCR:" 与原来的硬编码兜底同值 ⇒ 本次改动对现网零影响。
    return str(prompts.get("default") or ocr_cfg.get("vllm_prompt") or "OCR:")


def vllm_payload(img_b64: str, ocr_cfg: dict, prompt: str | None = None) -> dict:
    """vLLM 请求体。temperature 固定 0（AGENTS.md 红线⑩），**不在此处发请求**。"""
    # ---- round23 第 3 步：复读死循环的 token 上限（R23_OCR_MAX_TOKENS，0/未设=产线原值）----
    # 实测（s0_land2，40 例）：83/1151 次 OCR 请求 finish_reason=length，每次都跑满
    # vllm_max_tokens=4096，占整批 OCR 生成量 ~60%；这些页的真实正文只在前 ~200 字符，
    # 后面全是「术者 - 助 - 助 - 助…」式复读（s2 的 repeat_loop 已把它们折叠丢弃）。
    # 客户端的流式早停没有真正取消服务端解码（vllm:request_success_total{finished_reason="abort"}
    # 累计 = 0），所以卡仍然为整段复读付账。这里把上限压低 = 只砍复读，不砍正文。
    cap = int(os.environ.get("R23_OCR_MAX_TOKENS", "0") or 0)
    return {
        "model": str(ocr_cfg.get("vllm_model") or "PaddleOCR-VL"),
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + img_b64}},
            {"type": "text", "text": prompt or vllm_prompt_for(ocr_cfg)}]}],
        "max_tokens": (min(int(ocr_cfg.get("vllm_max_tokens", 4096)), cap)
                       if cap > 0 else int(ocr_cfg.get("vllm_max_tokens", 4096))),
        "temperature": 0.0,
    }


def vllm_parse(js: dict, size: tuple | None = None) -> tuple[dict | None, bytes, str]:
    """vLLM 响应 → 与 paddlex 同形的 pr（1 个整页 text 块，block_bbox=None）。

    **没有版面结构** → cell / coord 两条策略必然 0 命中，
    只有 block（标签-值同处一个文本流）和 LLM 抽取可用。
    finish_reason=length → 打 _truncated，下游不得当完整页用。
    """
    choices = js.get("choices")
    if not choices or not isinstance(choices[0], dict):
        return None, C.canon_json(js).encode(), "响应结构异常（无 choices[0]）"
    msg = choices[0].get("message")
    if not isinstance(msg, dict):
        return None, C.canon_json(js).encode(), "响应结构异常（无 choices[0].message）"
    text = msg.get("content") or ""          # 思维链字段 reasoning_content 一律不取
    finish = str(choices[0].get("finish_reason") or "")
    pr = {"width": (size or (None, None))[0], "height": (size or (None, None))[1],
          "_backend": "vllm", "_finish_reason": finish,
          "_truncated": finish == "length",
          "parsing_res_list": [{"block_id": 1, "block_label": "text",
                                "block_content": text, "block_bbox": None,
                                "block_order": 1}]}
    return pr, C.canon_json(js).encode("utf-8"), ""


def ocr_image_vllm(img_b64: str, ocr_cfg: dict, prompt: str | None = None,
                   size: tuple | None = None,
                   base_url: str | None = None,
                   abort_on_repeat: bool = False) -> tuple[dict | None, bytes | None, str]:
    """vLLM 版 PaddleOCR-VL：POST {endpoint}/v1/chat/completions，只取 message.content。

    端点：配了 ocr.vllm_urls（多副本）就**轮询**；只配 ocr.vllm_url 时行为与 round 3 一致。
    真正用到的端点写进 pr["_url"] → 逐请求审计里可查（并发下不往日志打，避免交错）。
    abort_on_repeat=True → 走流式 + 复读早停（round 7 第4件，见 ocr_image_vllm_stream）。
    """
    if abort_on_repeat:
        return ocr_image_vllm_stream(img_b64, ocr_cfg, prompt, size, base_url)
    payload = vllm_payload(img_b64, ocr_cfg, prompt)
    base = str(base_url or vllm_next_url(ocr_cfg)).rstrip("/")
    status, js, e = C.http_json(base + "/v1/chat/completions", payload,
                                int(ocr_cfg.get("vllm_timeout_s", 600)),
                                int(ocr_cfg.get("retries", 0)),
                                float(ocr_cfg.get("retry_backoff_s", 5)))
    if js is None:
        return None, None, f"status={status} {e} url={base}"
    pr, raw, err = vllm_parse(js, size)
    if pr is not None:
        pr["_url"] = base
    return pr, raw, err


# ---------------------------------------------------------- 复读循环检测（round 7 第1/4件）
# 判据在 c5a 与 r6f0p 两批（各 50 页 / 132 请求）上稳定抓出 3 个已知复读页，误报 0：
#   桐乡二院__00286042 p2(1-based) run=2027、桐乡二院__00335376 p2 run=2025、
#   长兴人民__200份_(13) p61 run(非连续)=1018 / uniq 8/1025。
# 阈值依据：正常页短行最大重复 7 次（奉化__008 p89，本身是截断页），其余 ≤3；
# 复读页 ≥1018。取 20 → 离正常 3 倍、离复读 50 倍。见 reports/round7-*。
REPEAT_RUN_MIN = 20        # 同一非空行连续重复 >= 20 次
REPEAT_UNIQ_RATIO = 0.05   # 或：非空行去重率 <= 5%（抓分带拼接后不连续的那种复读）
REPEAT_LINE_MIN = 20       # 且非空行数 >= 20，避免把「只有两行」的短页误判


def detect_repeat_loop(text: str) -> dict:
    """OCR 复读循环检测。**只读，不改文本**（第 4 件第一步：只检测不干预）。"""
    max_run = cur = 0
    prev = None
    nonempty = 0
    uniq = set()
    for ln in (text or "").split("\n"):
        k = ln.strip()
        if not k:
            cur, prev = 0, None
            continue
        nonempty += 1
        uniq.add(k)
        cur = cur + 1 if k == prev else 1
        prev = k
        if cur > max_run:
            max_run = cur
    n_uniq = len(uniq)
    ratio = (n_uniq / nonempty) if nonempty else 1.0
    why = []
    if max_run >= REPEAT_RUN_MIN:
        why.append("run")
    if nonempty >= REPEAT_LINE_MIN and ratio <= REPEAT_UNIQ_RATIO:
        why.append("ratio")
    return {"hit": bool(why), "max_run": max_run, "n_nonempty": nonempty,
            "n_uniq": n_uniq, "uniq_ratio": round(ratio, 4), "why": "+".join(why)}


# ---------------------------------------------------- 复读早停（round 7 第4件）
class RepeatWatcher:
    """流式增量判复读：同一条非空行在**本请求内**出现 >= run_min 次 → 命中。

    为什么数「出现次数」而不是「连续 run」：实测两种复读都在 ——
      桐乡二院__00286042 p2 连续 run=2027；
      长兴人民__200份_(13) p61 band3 同一条 2 字符行被空行隔开（连续 run=1）但出现 1018 次。
    出现次数判据蕴含连续判据，而且**单调**（前缀里只会变大）→ 不会出现
    「前缀去重率暂时很低、整页又恢复」那种误报。
    误报率（c5a / r6f0p 各 50 页）：正常页同一条非空行最多出现 **7** 次，
    复读页 >=1018 次 → run_min=20 两侧各留 3 倍 / 50 倍余量，误报 0。
    命中内容只记 sha/长度，不记正文（红线⑨）。
    """

    def __init__(self, run_min: int = REPEAT_RUN_MIN):
        self.run_min = run_min
        self.count: dict[str, int] = {}
        self.pending = ""
        self.n_lines = 0
        self.hit: dict | None = None

    def feed(self, chunk: str) -> None:
        if self.hit is not None:
            return
        self.pending += chunk
        while True:
            i = self.pending.find("\n")
            if i < 0:
                return
            self._line(self.pending[:i])
            self.pending = self.pending[i + 1:]

    def finish(self) -> None:
        if self.hit is None and self.pending:
            self._line(self.pending)
            self.pending = ""

    def _line(self, ln: str) -> None:
        k = ln.strip()
        if not k:
            return
        self.n_lines += 1
        self.count[k] = self.count.get(k, 0) + 1
        if self.count[k] >= self.run_min:
            self.hit = {"hit": True, "n_copies": self.count[k], "line_len": len(k),
                        "line_sha16": C.sha256_text(k)[:16],
                        "n_lines": self.n_lines, "n_uniq": len(self.count)}



# ------------------------------------------------- 短周期连续复读（round24 第 1 步）
# 判据：本次响应里存在一段「周期 = p (1<=p<=P) 的连续重复」长度 >= L 字符 → 判复读。
# 为什么必须加这一条：现网复读页的正文形态是「t t t t t …」单行短周期重复
#   （实测 workspace/round23/run_wide23/ms_3072_1/01_ocr/奉化__20230728_009/page_0123.json
#    band4：6143 字符、finish_reason=length、RepeatWatcher 口径 max_run=1 → 整行判据完全抓不到，
#    请求跑满 cap）。
# 阈值依据（round24 离线扫描 workspace/round24/out/periodfast_ms.txt：
#   round23 ms_* 四臂 745 页 = 608 正常页 + 137 撞上限页）：
#     P=32 时正常页 maxrun 最大只有 60 字符（p99=51、p90=44）；
#     撞上限页 maxrun p50=252、p10=39。取 L=150 = 正常页最大值 60 的 2.5 倍
#     → 正常页假阳性 0/608，撞上限页在 p50=204 字符处即可断（cap 3072 token ≈ 6143 字符）。
# 与 RepeatWatcher（同一条整行出现 >=20）正交，两者取「或」。
REPEAT_PERIOD_MAX = 32       # P  —— 允许的最大周期（字符）
REPEAT_PERIOD_MIN_RUN = 150  # L  —— 周期连续重复的最小字符数，达到即判复读


class PeriodicWatcher:
    """流式增量判「短周期连续重复」。O(P) 每字符，P=32。

    语义：eq_p[i] = (text[i] == text[i-p])；一段 r 个连续 True（起点 j）
    ⟺ text[j:j+r+p] 以 p 为周期，长度 r+p。r+p >= L 即命中。
    只记长度/周期/sha，不记正文（红线⑨）。
    """

    def __init__(self, max_period: int = REPEAT_PERIOD_MAX,
                 min_run: int = REPEAT_PERIOD_MIN_RUN):
        self.P = int(max_period)
        self.L = int(min_run)
        self.buf: list[str] = []
        self.rl = [0] * (self.P + 1)
        self.n = 0
        self.max_run = 0
        self.max_period = 0
        self.hit: dict | None = None

    def feed(self, chunk: str) -> None:
        buf = self.buf
        rl = self.rl
        P = self.P
        for ch in chunk:
            i = self.n
            buf.append(ch)
            upto = P if i >= P else i
            for p in range(1, upto + 1):
                if ch == buf[i - p]:
                    r = rl[p] + 1
                    rl[p] = r
                    chars = r + p
                    if chars > self.max_run:
                        self.max_run = chars
                        self.max_period = p
                    # 命中后**继续**测量（不 return）：审计臂要拿到整条响应的 max_run，
                    # 才能离线选阈值 L（round24：L=150 在 106 例普查里误杀 4 条合法响应，
                    # 而合法响应 max_run 最大正好 150 ⇒ 阈值必须留余量）。
                    # 断连臂不受影响 —— 调用方在 hit 出现后立刻 break 连接。
                    if chars >= self.L and self.hit is None:
                        self.hit = {"hit": True, "criterion": "periodic",
                                    "period": p, "run_chars": chars,
                                    "at_chars": i + 1,
                                    "tail_sha16": C.sha256_text(
                                        "".join(buf[max(0, i + 1 - chars):i + 1]))[:16]}
                else:
                    rl[p] = 0
            self.n += 1

    def stats(self) -> dict:
        return {"n_chars": self.n, "max_run": self.max_run,
                "max_period": self.max_period, "min_run": self.L,
                "max_period_allowed": self.P, "fired": self.hit is not None}


def repeat_period_min_run(ocr_cfg: dict) -> int:
    """周期判据阈值 L。0 = 关闭。env R24_REPEAT_PERIOD_MIN_RUN 覆盖，默认 0（产线原行为）。"""
    env = os.environ.get("R24_REPEAT_PERIOD_MIN_RUN")
    if env is not None:
        return max(0, int(env))
    return 0


def repeat_period_abort_enabled() -> bool:
    """命中后是否真的断连。R24_REPEAT_ABORT_PERIOD=1 才断；只开审计时不断（先量后砍）。"""
    return os.environ.get("R24_REPEAT_ABORT_PERIOD", "").strip().lower() in ("1", "true", "yes", "on")


def repeat_audit_enabled() -> bool:
    return os.environ.get("R24_REPEAT_AUDIT", "").strip().lower() in ("1", "true", "yes", "on")


# ---- round47 S1：审计行的 case 字段 ----
# 旧写法 s1_ocr.py:519 读 globals().get("_R16_CUR_CASE")，但**全仓从未赋值**
# ⇒ web-20261018-102926 的 158,396 行 repeat_audit.jsonl 里 case 100% 为空。
# 单例内多线程共用同一个 case；跨例是不同 subprocess（run_steps.py:290）⇒ 模块级安全。
_R16_CUR_CASE = ""


def set_audit_case(case_id: str) -> None:
    """把当前例号交给复读审计行（locate 与 s1 都要调）。"""
    global _R16_CUR_CASE
    _R16_CUR_CASE = str(case_id or "")


def repeat_audit_emit(rec: dict) -> None:
    """审计行落 $R24_REPEAT_LOG（逐进程一个文件）。只有计数/sha，没有正文（红线⑨）。"""
    path = os.environ.get("R24_REPEAT_LOG", "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def abort_repeat_enabled(ocr_cfg: dict) -> bool:
    """复读早停开关。**默认关** —— config/pipeline.yaml 只读，加键必须由人执行（见报告需要裁决）。

    优先级：env MEDREC_ABORT_REPEAT（A/B 对照用，不碰配置）> ocr.abort_repeat_loop > False。
    """
    env = os.environ.get("MEDREC_ABORT_REPEAT")
    if env is not None:
        return env.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(ocr_cfg.get("abort_repeat_loop", False))


def ocr_image_vllm_stream(img_b64: str, ocr_cfg: dict, prompt: str | None = None,
                          size: tuple | None = None, base_url: str | None = None,
                          run_min: int = REPEAT_RUN_MIN,
                          line_abort: bool = True,
                          period_min_run: int | None = None,
                          stage: str = "s1") -> tuple[dict | None, bytes | None, str]:
    """流式 vLLM 请求 + 复读早停：命中就把连接断掉，不再收后面的复读。

    为什么断流是安全的（实测，见 reports/round7-*）：3 个复读页里，**每个含复读的请求**
    的非循环内容全部排在循环之前 —— 桐乡两例 other_last < loop_first；
    长兴 p61 分 4 个 band，只有 band3 有循环，band4 的真实正文是**另一个请求**，不受影响。
    所以断在「第 run_min 份」处丢掉的只有循环本身。
    为什么可复现：断流点由**已生成的 token 序列**决定（temperature 0 + 贪心 +
    --max-num-seqs 1 + --no-enable-prefix-caching），不由到达时序决定。
    记账：断流后服务器还会继续跑一段，用 /metrics 的 finished_reason="abort" 与
    request_generation_tokens_sum 量（见报告）。
    """
    import urllib.error
    import urllib.request
    payload = vllm_payload(img_b64, ocr_cfg, prompt)
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}
    base = str(base_url or vllm_next_url(ocr_cfg)).rstrip("/")
    url = base + "/v1/chat/completions"
    C.clear_proxy()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdr = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    timeout = int(ocr_cfg.get("vllm_timeout_s", 600))
    retries = int(ocr_cfg.get("retries", 0))
    backoff = float(ocr_cfg.get("retry_backoff_s", 5))
    last_err = ""
    status = 0
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status = r.status
                parts: list[str] = []
                watch = RepeatWatcher(run_min)
                pmr = (repeat_period_min_run(ocr_cfg)
                       if period_min_run is None else int(period_min_run))
                pw = (PeriodicWatcher(REPEAT_PERIOD_MAX, pmr)
                      if (pmr > 0 or repeat_audit_enabled()) else None)
                period_abort_on = repeat_period_abort_enabled()
                crit = ""
                finish = ""
                usage = None
                aborted = False
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        ck = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if ck.get("usage"):
                        usage = ck["usage"]
                    ch = (ck.get("choices") or [{}])[0]
                    if ch.get("finish_reason"):
                        finish = str(ch["finish_reason"])
                    delta = ((ch.get("delta") or {}).get("content")) or ""
                    if delta:
                        parts.append(delta)
                        watch.feed(delta)
                        if pw is not None:
                            pw.feed(delta)
                        if line_abort and watch.hit is not None:
                            aborted = True
                            crit = "line"
                            break          # 出 with 块 → 连接关闭 → 服务器 abort
                        if pw is not None and pw.hit is not None and period_abort_on:
                            aborted = True
                            crit = "periodic"
                            break          # 出 with 块 → 连接关闭 → 服务器 abort
                watch.finish()
                text = "".join(parts)
                if pw is not None:
                    st = pw.stats()
                    st.update({"stage": stage, "aborted": aborted, "criterion": crit,
                               "line_max_occ": (watch.hit or {}).get("n_copies", watch.n_lines),
                               "finish_reason": finish, "n_chars": len(text),
                               "at_chars": (pw.hit or {}).get("at_chars"),
                               "period": (pw.hit or {}).get("period"),
                               "run_chars": (pw.hit or {}).get("run_chars"),
                               "case": _R16_CUR_CASE,
                               "url": base})
                    repeat_audit_emit(st)
                js: dict = {"choices": [{"message": {"content": text},
                                         "finish_reason": "repeat_abort" if aborted else finish}]}
                if usage:
                    js["usage"] = usage
                pr, raw_js, err = vllm_parse(js, size)
                if pr is not None:
                    pr["_url"] = base
                    pr["_stream"] = True
                    pr["_repeat_abort"] = watch.hit
                    if crit == "periodic":
                        pr["_repeat_abort"] = pw.hit
                    if pw is not None:
                        pr["_repeat_watch"] = pw.stats()
                    if aborted:
                        pr["_truncated"] = True
                        pr["_n_chars_at_abort"] = len(text)
                return pr, raw_js, err
        except urllib.error.HTTPError as e:
            status = e.code
            last_err = f"HTTPError {e.code}: {e.reason}"
        except Exception as exc:                                          # noqa: BLE001
            status = 0
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    return None, None, f"status={status} {last_err} url={base}"


def ocr_page_vllm(png_path: Path, ocr_cfg: dict,
                  prompt: str | None = None,
                  abort_on_repeat: bool = False) -> tuple[dict | None, bytes | None, str]:
    from PIL import Image
    with Image.open(png_path) as im:
        size = im.size
    return ocr_image_vllm(C.b64_file(png_path), ocr_cfg, prompt, size,
                          abort_on_repeat=abort_on_repeat)


# ---------------------------------------------------- 并发（vllm 通道）
_BI_PROBE: dict[str, bool] = {}
_URL_LOCK = threading.Lock()
_URL_RR: dict[str, int] = {}


def vllm_port(ocr_cfg: dict) -> int:
    return port_of_url(str(ocr_cfg.get("vllm_url") or ""))


def port_of_url(url: str) -> int:
    try:
        from urllib.parse import urlparse
        return int(urlparse(str(url or "")).port or 0)
    except Exception:                                       # noqa: BLE001
        return 0


def vllm_endpoints(ocr_cfg: dict) -> list[str]:
    """ocr.vllm_urls（多副本）优先；没配退回单个 ocr.vllm_url。去重、去尾斜杠。"""
    raw = ocr_cfg.get("vllm_urls") or [ocr_cfg.get("vllm_url")]
    eps: list[str] = []
    for u in raw:
        u = str(u or "").strip().rstrip("/")
        if u and u not in eps:
            eps.append(u)
    return eps


def vllm_next_url(ocr_cfg: dict) -> str:
    """轮询选端点。单端点时等价于 ocr.vllm_url（行为不变）。"""
    eps = vllm_endpoints(ocr_cfg)
    if not eps:
        return ""
    if len(eps) == 1:
        return eps[0]
    key = "|".join(eps)
    with _URL_LOCK:
        i = _URL_RR.get(key, 0) % len(eps)
        _URL_RR[key] = i + 1
    return eps[i]


def listener_pid(port: int) -> str:
    """监听该端口的进程 pid（vLLM APIServer）。查不到返回空串。"""
    if not port:
        return ""
    try:
        import re
        import subprocess
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True,
                             timeout=10).stdout
        for line in out.splitlines():
            if f":{port} " in line or line.strip().endswith(f":{port}"):
                m = re.search(r"pid=(\d+)", line)
                if m:
                    return m.group(1)
    except Exception:                                       # noqa: BLE001
        pass
    return ""


def proc_cmdline(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().decode("utf-8", "replace").replace("\0", " ")
    except Exception:                                       # noqa: BLE001
        return ""


def proc_environ_has(pid: str, kv: str) -> bool:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return kv.encode() in f.read().split(b"\0")
    except Exception:                                       # noqa: BLE001
        return False


def vllm_batch_invariant(ocr_cfg: dict) -> bool:
    """服务端进程环境里有没有 VLLM_BATCH_INVARIANT=1。

    为什么必须查：实测（workspace/conc/determinism*.out）vLLM 默认配置下
    **并发输出与串行不逐字节相同**，而且并发自身也不稳定 —— 同一页 3 轮跑出
    3 种正文（page_0009: 524daf4e / ef9a7d01 / 44a65507）。原因不是温度，是
    批形状改变 attention/cuBLAS 的归约顺序（prefix cache 冷热同样会改）。
    只有 VLLM_BATCH_INVARIANT=1 才做到「并发 == 串行」逐字节一致。
    **查不到按 False（fail-closed）**：宁可退回串行，也不能悄悄并发改变输出。
    """
    eps = vllm_endpoints(ocr_cfg)
    if not eps:
        return False
    key = "|".join(eps)
    if key in _BI_PROBE:
        return _BI_PROBE[key]
    ok = True
    for u in eps:
        pid = listener_pid(port_of_url(u))
        if not pid or not proc_environ_has(pid, "VLLM_BATCH_INVARIANT=1"):
            ok = False
            break
    _BI_PROBE[key] = ok
    return ok


def vllm_single_seq_replicas(ocr_cfg: dict) -> bool:
    """并发安全的第二条路：每个端点都是 **--max-num-seqs 1 + 关 prefix cache** 的独立副本。

    §2.2 实测「并发改变输出」的根因是**批形状**改变归约顺序。批形状恒为 1 时，
    并发 = 一串 batch-1 请求 → 归约顺序与串行相同。**这个充分条件只看服务端批形状，
    不看端点数**：2026-10-10 实测（workspace/conc/single_sq_conc.out）单端点
    --max-num-seqs 1 下 conc=2/4/8 的 5 页输出与串行 5/5 逐字节相同、
    completion_tokens 完全相同（1270 967 105 883 221），串行两轮之间也相同。
    所以 >=1 个端点即可；但**每个**端点的 cmdline 里都必须显式带着这两个参数，
    查不到按 False（fail-closed，红线①）。
    """
    eps = vllm_endpoints(ocr_cfg)
    if not eps:
        return False
    import re
    for u in eps:
        cmd = proc_cmdline(listener_pid(port_of_url(u)))
        if not cmd:
            return False
        m = re.search(r"--max-num-seqs(?:[= ]+)(\d+)", cmd)
        if not m or int(m.group(1)) != 1:
            return False
        if "--no-enable-prefix-caching" not in cmd:
            return False
    return True


def concurrency_gate(ocr_cfg: dict, stage: str = "") -> tuple[bool, str]:
    """红线①闸门：这个后端的并发是否被允许。**s1_ocr 与 locate 页眉条探测必须共用它**
    （2026-10-10 c4g 实测两边口径不一致：s1 因 require_batch_invariant=false 放行，
    locate 仍把 probe 压回 1）。fail-closed：查不到服务端参数 = 不允许。
    """
    if resolve_backend(ocr_cfg, stage) != "vllm":
        return False, "backend!=vllm（paddlex 通道不并发）"
    if not bool(ocr_cfg.get("require_batch_invariant", True)):
        return True, "gate:off（require_batch_invariant=false —— 已放弃红线①）"
    if vllm_batch_invariant(ocr_cfg):
        return True, "ok(VLLM_BATCH_INVARIANT=1)"
    if bool(ocr_cfg.get("replica_single_seq_ok", True)) and vllm_single_seq_replicas(ocr_cfg):
        return True, "ok(single-seq：每端点 --max-num-seqs 1 + 无 prefix cache)"
    return False, ("gate:既没有 VLLM_BATCH_INVARIANT=1，也不是 single-seq 端点 "
                   "→ 强制串行（红线①）")


def effective_concurrency(ocr_cfg: dict, stage: str = "") -> tuple[int, str]:
    """本轮实际用的并发度 + 原因（写进审计/manifest，不许只写在脑子里）。"""
    want = max(1, int(ocr_cfg.get("concurrency") or 1))
    if want <= 1:
        return 1, "config:concurrency=1"
    ok, why = concurrency_gate(ocr_cfg, stage)
    return (want if ok else 1), why


def vllm_jobs_for(png_path: Path, ocr_cfg: dict, prompt: str | None = None,
                  stage: str = "") -> list[dict]:
    """一页 × 一个变体 → 若干**独立请求**（整页 1 个 / 分带 N 个）。

    只存路径 + 裁剪框，不预读图片：并发时内存随并发度增长而不是随页数增长，
    --all-pages（上百页）不会把整批 PNG 塞进内存。
    """
    from PIL import Image
    with Image.open(png_path) as im:
        w, h = im.size
    plan = band_plan(w, h, ocr_cfg.get("bands") or {}) \
        if bands_enabled_for(ocr_cfg, stage) else [(0, h)]
    if len(plan) <= 1:
        return [{"png": Path(png_path), "band": 0, "crop": None,
                 "size": (w, h), "prompt": prompt}]
    return [{"png": Path(png_path), "band": i, "crop": (0, y0, w, y1),
             "size": (w, y1 - y0), "prompt": prompt}
            for i, (y0, y1) in enumerate(plan)]


def vllm_run_job(job: dict, ocr_cfg: dict) -> dict:
    """发一个 vLLM 请求。线程安全：不写日志、不写审计、不碰共享状态。"""
    import base64
    import io
    t0 = time.time()
    try:
        if job.get("crop") is None:
            b64 = C.b64_file(job["png"])          # 与串行同一份字节（直接读 PNG 文件）
        else:
            from PIL import Image
            with Image.open(job["png"]) as im:
                buf = io.BytesIO()
                im.convert("RGB").crop(job["crop"]).save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        # 只有**整页/分带的页请求**开复读早停；定位窄条走 locate_pages 自己的调用，
        # 默认 abort_on_repeat=False（那个文件本轮不可写，见报告）。
        pr, raw, e = ocr_image_vllm(b64, ocr_cfg, job.get("prompt"), job.get("size"),
                                    abort_on_repeat=abort_repeat_enabled(ocr_cfg))
        return {"ok": pr is not None, "pr": pr, "raw": raw, "err": e,
                "band": job.get("band"),
                "elapsed_s": round(time.time() - t0, 3)}
    except Exception as exc:                                      # noqa: BLE001
        return {"ok": False, "pr": None, "raw": None, "band": job.get("band"),
                "err": f"{type(exc).__name__}: {exc}",
                "elapsed_s": round(time.time() - t0, 3)}


def run_vllm_jobs(jobs: list[dict], ocr_cfg: dict,
                  executor: "cf.ThreadPoolExecutor | None" = None) -> list[dict]:
    """按 jobs 顺序返回结果（**完成顺序无关** —— 拼接只认带序，这是逐字节一致的前提）。"""
    if executor is None or len(jobs) <= 1:
        return [vllm_run_job(j, ocr_cfg) for j in jobs]
    return [f.result() for f in [executor.submit(vllm_run_job, j, ocr_cfg) for j in jobs]]


def vllm_join(jobs: list[dict], results: list[dict],
              ocr_cfg: dict) -> tuple[dict | None, bytes | None, str]:
    """把逐请求结果拼回 pr。串行/并发**共用这一个 joiner** → 两条路径逐字节同形。"""
    band_cfg = ocr_cfg.get("bands") or {}
    if len(jobs) == 1:
        r = results[0]
        return (r["pr"], r["raw"], "") if r["ok"] else (None, r["raw"], r["err"])
    lines: list[str] = []
    meta: list[dict] = []
    truncated = False
    for job, r in zip(jobs, results):
        if not r["ok"]:
            y0, y1 = job["crop"][1], job["crop"][3]
            return None, C.canon_json({"bands": meta}).encode("utf-8"), \
                f"band{job['band'] + 1}[{y0}:{y1}] {r['err']}"
        pr = r["pr"]
        txt = "\n".join(b.get("block_content") or ""
                        for b in pr.get("parsing_res_list", []))
        truncated = truncated or bool(pr.get("_truncated"))
        lines = dedup_join(lines, txt.split("\n"),
                           float(band_cfg.get("dedup_thresh", 0.6)))
        meta.append({"band": job["band"] + 1, "y0": job["crop"][1], "y1": job["crop"][3],
                     "chars": len(txt), "finish_reason": pr.get("_finish_reason"),
                     "truncated": bool(pr.get("_truncated")),
                     "repeat_abort": pr.get("_repeat_abort") or None,
                     "sha256": C.sha256_text(txt)})
    text = "\n".join(lines)
    pr = {"width": jobs[0]["size"][0], "height": None, "_backend": "vllm",
          "_finish_reason": "banded", "_truncated": truncated, "_banded": True,
          "_n_bands": len(jobs), "_bands": meta,
          "_repeat_abort": next((m["repeat_abort"] for m in meta if m["repeat_abort"]),
                                None),
          "parsing_res_list": [{"block_id": 1, "block_label": "text",
                                "block_content": text, "block_bbox": None,
                                "block_order": 1}]}
    for job in jobs:
        if job["crop"] is None:
            pr["height"] = job["size"][1]
    if pr["height"] is None:
        pr["height"] = max(j["crop"][3] for j in jobs)
    return pr, C.canon_json({"bands": meta}).encode("utf-8"), ""


# --------------------------------------------------------- 分带裁剪（仅 vllm 通道）
def bands_enabled_for(ocr_cfg: dict, stage: str) -> bool:
    """分带开关：ocr.bands.enabled + ocr.bands.stages 白名单（空 = 所有阶段）。"""
    b = ocr_cfg.get("bands") or {}
    if not b.get("enabled"):
        return False
    stages = [str(x).strip() for x in (b.get("stages") or [])]
    return not stages or (stage or "") in stages


def band_plan(w: int, h: int, band_cfg: dict) -> list:
    """按 vLLM 预处理器的像素预算把整页切成 N 条横带（带重叠）。

    为什么要分带（实测 2026-10-10，workspace/vllm_ocr/band_probe.out）：整页 PNG
    3.90 Mpx 被 preprocessor_config.json 的 max_pixels: 1003520 降到线性 50.7%，
    桐乡二院__00335376 page_0009 因此读错两处查体（口唇不绀→口唇不结、
    跟膝胫试验→跟踪试验）；切 4 条**原生分辨率**横带后两处都读回来了。
    带高 = ceil(H/N) + overlap，带起点再回退 overlap → 相邻带重叠 4% 页高（≈94 px），
    远大于行高（≈30 px）→ 每一行至少完整落在某一条带里。
    """
    cap = max(int(band_cfg.get("target_pixels") or 1_003_520), 1)
    nb = max(int(band_cfg.get("min_bands", 1)), -(-(w * h) // cap))
    nb = min(nb, int(band_cfg.get("max_bands", 8)))
    if nb <= 1:
        return [(0, h)]
    ov = int(round(h * float(band_cfg.get("overlap_frac", 0.04))))
    band_h = -(-h // nb) + ov
    plan = []
    for i in range(nb):
        y0, y1 = max(0, i * band_h - ov), min(h, (i + 1) * band_h)
        if y0 >= h:
            break
        plan.append((y0, y1))
    return plan


def dedup_join(acc: list, new: list, thresh: float = 0.6, max_ov: int = 60) -> list:
    """带间重叠去重 + 跨带拼接：找 acc 尾部与 new 头部的最大相似重叠行段，只接未重复部分。

    两带对同一行的 OCR 不会逐字相同（切边不同、上下文不同）→ 按**整行**做相似度匹配，
    取「平均相似度 >= thresh 的最大重叠行数」。宁可少删（留一行重复）也不能多删（丢正文）。
    """
    if not acc:
        return list(new)
    if not new:
        return acc
    import difflib
    hi = min(len(acc), len(new), max_ov)
    for L in range(hi, 0, -1):
        tail, head = acc[-L:], new[:L]
        if tail == head:
            return acc + new[L:]
        mean = sum(difflib.SequenceMatcher(None, a, b).ratio()
                   for a, b in zip(tail, head)) / L
        if mean >= thresh:
            return acc + new[L:]
    return acc + new


def ocr_page_vllm_banded(png_path: Path, ocr_cfg: dict, prompt: str | None = None,
                         stage: str = "") -> tuple[dict | None, bytes | None, str]:
    """vllm 分带 OCR：整页 → N 条原生分辨率横带 → 逐带 OCR → 去重拼接成一段正文。

    返回与 ocr_image_vllm **同形**的 pr（1 个整页 text 块，block_bbox=None）→ 下游
    s2/s3 无需改动。额外带 _bands（逐带 y0/y1/字符数/finish_reason/哈希）供报告与审计。
    **只用于 vllm 通道**：paddlex 通道返回版面块 + block_bbox，切带会破坏首页的
    字段-值坐标配对，因此首页默认不分带（见 pipeline.yaml:ocr.bands.stages）。
    """
    jobs = vllm_jobs_for(png_path, ocr_cfg, prompt, stage)
    return vllm_join(jobs, run_vllm_jobs(jobs, ocr_cfg), ocr_cfg)


def ocr_page_variant(png_path: Path, ocr_cfg: dict, use_layout: bool,
                     stage: str = "",
                     variant: str = "") -> tuple[dict | None, bytes | None, str]:
    if resolve_backend(ocr_cfg, stage) == "vllm":
        if bands_enabled_for(ocr_cfg, stage):
            return ocr_page_vllm_banded(png_path, ocr_cfg,
                                        vllm_prompt_for(ocr_cfg, variant, stage), stage)
        return ocr_page_vllm(png_path, ocr_cfg,
                             vllm_prompt_for(ocr_cfg, variant, stage))
    payload = {
        "file": C.b64_file(png_path),
        "fileType": 1,                          # 1=IMAGE；省略会 422
        "useLayoutDetection": bool(use_layout),
        "promptLabel": ocr_cfg["prompt_label"],
        "returnMarkdownImages": bool(ocr_cfg["return_markdown_images"]),
        "maxPixels": int(ocr_cfg["max_pixels"]),
    }
    url = ocr_cfg["base_url"].rstrip("/") + ocr_cfg["endpoint"]
    status, js, e = C.http_json(url, payload, int(ocr_cfg["timeout_s"]),
                                int(ocr_cfg.get("retries", 0)),
                                float(ocr_cfg.get("retry_backoff_s", 5)))
    if js is None:
        return None, None, f"status={status} {e}"
    try:
        pr = js["result"]["layoutParsingResults"][0]["prunedResult"]
    except (KeyError, IndexError, TypeError) as exc:
        return None, C.canon_json(js).encode(), f"响应结构异常 {type(exc).__name__}"
    return pr, C.canon_json(js).encode("utf-8"), ""


def extract_blocks(pr: dict) -> list[dict]:
    """parsing_res_list → 精简块列表（保留 label/content/bbox/order）。"""
    out = []
    for b in pr.get("parsing_res_list", []) or []:
        out.append({
            "block_id": b.get("block_id"),
            "block_label": b.get("block_label"),
            "block_content": b.get("block_content") or "",
            "block_bbox": b.get("block_bbox"),
            "block_order": b.get("block_order"),
        })
    return out


def crop_blocks(img, blocks: list[dict], out_dir: Path) -> list[dict]:
    """按 block_bbox 落裁剪图，退化框回退整页并记录原因（风险③）。"""
    recs = []
    for b in blocks:
        bb = b.get("block_bbox")
        if not bb:
            continue
        crop, degraded = C.safe_crop(img, bb, pad=4)
        name = f"block_{int(b['block_id']):03d}.png"
        crop.save(out_dir / name)
        rec = {"block_id": b["block_id"], "crop": name,
               "crop_sha256": C.sha256_file(out_dir / name)}
        C.chmod(out_dir / name)          # 裁剪图里可能有患者影像/PII → 600
        if degraded:
            rec["degraded_bbox"] = degraded
        recs.append(rec)
    return recs


# --------------------------------------------------------------- 单例
def _block_text(blocks: list[dict]) -> str:
    """块文本。主变体用 block_content，副变体（plain）用 text。"""
    return "\n".join(str(b.get("block_content") or b.get("text") or "")
                     for b in (blocks or []))


def on_demand_variants(case: str, out: Path, page_recs: list[dict], fp_pages: set[int],
                       cfg: dict, audit: C.Audit, quiet: bool) -> dict:
    """步②（2026-10-10）：主变体跑完后，**只有首页页集的必填列标签缺失时**才对最可能的页补跑副变体。

    为什么不是一刀切（实测数据写在 config/pipeline.yaml:ocr.on_demand 注释里）：
      layout 真正丢的是首页**人口学区**字段（身份证号/籍贯/现住址/联系人/病案号…），
      不是旧注释说的勾选框；但一刀切开 plain 每例多跑 2 页 = 26.6~90.5s（奉化 p88 单页 76.1s）。
      按需 = 只在真缺的例上补 1 页（安吉两例各 12.1s / 14.0s，其余 4 个院区 0s）。
    provenance：补跑过的页在 page_*.json 里写 variants.<v>.on_demand=true 和 on_demand={...}。
    """
    ocfg = cfg["ocr"]
    if not ocfg.get("variants_on_demand"):
        return {"enabled": False}
    od = ocfg.get("on_demand") or {}
    vname = str(od.get("variant") or "plain")
    req_ids = [str(x) for x in (od.get("required_columns") or [])]
    markers = [str(x) for x in (od.get("demographic_markers") or [])]
    min_markers = int(od.get("demographic_min_markers", 3))
    cap = int(od.get("max_pages", 1))
    if not req_ids:
        return {"enabled": True, "triggered": False, "reason": "no_required_columns"}

    labels = {c["id"]: [str(x) for x in (c.get("candidate_labels") or [])]
              for c in C.load_columns()["columns"] if c["id"] in req_ids}
    unknown = [cid for cid in req_ids if cid not in labels]
    if unknown:
        C.warn(f"{case} on_demand: required_columns 里这些 id 在 columns.yaml 找不到 {unknown}")
    req_ids = [cid for cid in req_ids if cid in labels]

    cand = [r for r in page_recs
            if r.get("page") in fp_pages and r.get("status") == "ok"
            and f"blocks_{vname}" not in r]
    if not cand:
        return {"enabled": True, "triggered": False, "reason": "no_candidate_pages"}

    def missing_now() -> list[str]:
        union = "\n".join(_block_text(r.get("blocks")) + "\n" + _block_text(r.get(f"blocks_{vname}"))
                          for r in cand)
        return [cid for cid in req_ids
                if not any(lb in union for lb in labels.get(cid) or [])]

    missing = missing_now()
    if not missing:
        return {"enabled": True, "triggered": False, "variant": vname,
                "required_columns": req_ids, "missing_columns": []}

    scored = []
    for r in cand:
        t = _block_text(r.get("blocks"))
        scored.append((sum(1 for m in markers if m in t),
                       sum(1 for cid in req_ids if any(lb in t for lb in labels[cid])),
                       int(r["page"]), r))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    picked = [s for s in scored if s[0] >= min_markers][:cap]
    if not picked:
        return {"enabled": True, "triggered": False, "variant": vname,
                "missing_columns_before": missing,
                "reason": "no_page_over_demographic_min_markers",
                "marker_counts": {s[2]: s[0] for s in scored}}

    use_layout = bool((ocfg.get("variants") or {}).get(vname))
    missing_before = list(missing)
    ran: list[dict] = []
    for _, _, pno, rec in picked:
        if not missing:
            break
        png = out / rec["png"]
        t0 = time.time()
        pr, raw, e = ocr_page_variant(png, cfg["ocr"], use_layout,
                                      stage="frontpage", variant=vname)
        el = round(time.time() - t0, 1)
        if pr is None:
            C.err(f"[{case}] page {pno} 按需补跑 {vname} 失败: {e}")
            ran.append({"page": pno, "status": "error", "error": str(e), "elapsed_s": el})
            audit.emit(case_id=case, page=pno, kind="ocr_request", variant=vname,
                       on_demand=True, ok=False,
                       backend=resolve_backend(cfg["ocr"], "frontpage"),
                       elapsed_s=el, error=str(e))
            continue
        blocks = extract_blocks(pr)
        rec[f"blocks_{vname}"] = blocks
        rec.setdefault("variants", {})[vname] = {
            "use_layout_detection": use_layout, "status": "ok", "elapsed_s": el,
            "n_blocks": len(blocks), "ocr_response_sha256": C.sha256_bytes(raw),
            "page_size": [pr.get("width"), pr.get("height")], "on_demand": True}
        rec["on_demand"] = {"variant": vname, "triggered": True,
                            "missing_columns_before": missing_before,
                            "demographic_markers": sum(1 for m in markers
                                                       if m in _block_text(rec.get("blocks")))}
        C.write_json(out / f"page_{pno + 1:04d}.json", rec)
        audit.emit(case_id=case, page=pno, kind="ocr_output", variant=vname, on_demand=True,
                   use_layout_detection=use_layout, png_sha256=rec.get("png_sha256"),
                   ocr_response_sha256=rec["variants"][vname]["ocr_response_sha256"],
                   n_blocks=len(blocks), elapsed_s=el)
        # 按需补跑也要落 kind=ocr_request：口径 A（manifest n_requests，含按需请求）与
        # 口径 B（审计 kind=ocr_request 行数）之前会差掉按需那几条
        # （2026-10-10 c4g 实测 A=133 / B=132，差值 = 安吉那 1 条按需补跑）。
        for bm in (pr.get("_bands") or [None]):
            audit.emit(case_id=case, page=pno, kind="ocr_request", variant=vname,
                       on_demand=True, ok=True,
                       backend=resolve_backend(cfg["ocr"], "frontpage"),
                       url=pr.get("_url"),
                       band=(bm or {}).get("band"),
                       crop=([bm["y0"], bm["y1"]] if bm else None),
                       truncated=bool((bm or {}).get("truncated") or pr.get("_truncated")),
                       finish_reason=(bm or {}).get("finish_reason")
                                     or pr.get("_finish_reason"),
                       elapsed_s=el,
                       ocr_response_sha256=(C.sha256_bytes(raw) if raw else None))
        missing = missing_now()
        ran.append({"page": pno, "status": "ok", "elapsed_s": el, "n_blocks": len(blocks),
                    "n_requests": int(pr.get("_n_bands") or 1),
                    "missing_after": list(missing)})
        C.log(f"    p{pno + 1:04d} {vname}(按需) ok blocks={len(blocks)} {el}s "
              f"仍缺 {len(missing)} 列", quiet=quiet)
    return {"enabled": True, "triggered": True, "variant": vname,
            "required_columns": req_ids, "missing_columns_before": missing_before,
            "pages": ran, "missing_columns_after": missing,
            # 按需补跑是**顺序依赖**的（每页跑完重算缺列，缺列满了就停）→ 不并发，
            # 但请求数照样进口径 A，否则 manifest 的 n_requests 会少算。
            "n_requests": sum(int(x.get("n_requests") or 1) for x in ran)}


def process_case(row: dict, cfg: dict, batch_dir: Path, audit: C.Audit,
                 index: dict, *, all_pages: bool, max_pages: int | None,
                 crop_blocks_on: bool, force: bool, quiet: bool,
                 only_pages: set[int] | None = None, append: bool = False) -> dict:
    import pypdfium2 as pdfium
    case = row["case_id"]
    set_audit_case(case)
    out = batch_dir / STAGE / case
    out.mkdir(parents=True, exist_ok=True)
    pdf_path = Path(row["pdf_path"])
    pdf = pdfium.PdfDocument(str(pdf_path))
    n_src = len(pdf)

    sel = index.get(case, {}) if index else {}
    fp_pages = set(sel.get("frontpage_pages") or [])
    if all_pages:
        pages = list(range(n_src))
    else:
        want = list(sel.get("frontpage_pages") or []) + list(sel.get("first_progress_pages") or [])
        pages = sorted({p for p in want if 0 <= p < n_src})
        if not pages:
            pages = list(range(min(n_src, 3)))       # 没定位信息：保底扫前 3 页并告警
            C.warn(f"{case}: 无定位信息，回退扫前 {len(pages)} 页")
    if max_pages:
        pages = pages[:int(max_pages)]
    # ── round16 第 5 步（locate↔s1 重叠）：分批 + 续跑 ──
    # --only-pages：本轮只处理这些页（首页批次用），其余页留给第二次调用。
    # --append     ：续跑模式 —— 不清 .done / 不删本轮页集合之外的产物 / 已落盘的页直接复用不重扫。
    # 两者合起来才等价于「整例一次跑完」：页集合的并集、每页的请求与正文都不变（每页 OCR 彼此独立，
    # vllm 通道没有跨页 restructure → 分批不可能改变任何一页的 ocr_text_sha256）。
    if only_pages is not None:
        pages = [p for p in pages if p in only_pages]
    pages_req = list(pages)
    batch_tag = ("partial:%s" % ",".join(str(p) for p in sorted(only_pages))
                 if only_pages is not None else "final")
    # 定位选中的页（首页 + 首程）走宽松档：低信息页也送 OCR，只保留标记。
    # 依据：run/tmp/probe/lowinfo_check.py —— 安吉__img20231201_11004386 0based 250
    # 是首次病程记录标题页（ink 0.00333 / std 12.68），严格档会把它整个跳过。
    target_pages = fp_pages | {int(p) for p in (sel.get("first_progress_pages") or [])}

    done_marker = out / ".done"
    bad_marker = out / ".incomplete"
    if not append:
        done_marker.unlink(missing_ok=True)
        bad_marker.unlink(missing_ok=True)
        # 只删本例目录下**不在本轮请求页集合内**的 page_*.{png,json}（上一轮跑过别的页会残留）
        keep = {f"page_{p + 1:04d}" for p in pages}
        for p in list(out.glob("page_*.png")) + list(out.glob("page_*.json")):
            if p.stem not in keep:
                p.unlink()
                C.warn(f"{case}: 删除上一轮残留 {p.name}")
        # 上一轮的 block_*.png 一律清掉：块数量/顺序每轮可能不同，留着会对不上账
        for p in out.glob("block_*.png"):
            p.unlink()

    page_recs, errors = [], []
    # 续跑：已经有合法 page_*.json 的页直接搬回来，不再渲染、不再发 OCR 请求（幂等、不覆盖已完成页）。
    reused = 0
    if append:
        for p in list(pages):
            pj = out / f"page_{p + 1:04d}.json"
            if not pj.exists():
                continue
            try:
                prev_rec = C.read_json(pj)
            except Exception:                                          # noqa: BLE001
                C.warn(f"{case} page {p}: 续跑复用失败（{pj.name} 读不动）→ 重扫该页")
                continue
            if not isinstance(prev_rec, dict) or "status" not in prev_rec:
                C.warn(f"{case} page {p}: 续跑复用失败（{pj.name} 无 status）→ 重扫该页")
                continue
            page_recs.append(prev_rec)
            pages.remove(p)
            reused += 1
        if reused:
            C.log(f"  {case}: 续跑复用 {reused} 页（已落盘，不重扫）", quiet=quiet)
    # ---- 步①：渲染 + 空白判定 + 落 PNG（CPU 活，串行），顺带把每页请求拆成 job ----
    # 为什么要拆步：原来「渲染 → 等 OCR → 渲染 → 等 OCR」交织在同一个 for p in pages
    # 里，OCR 的 IO 等待期间 CPU 全闲着，也没有可摊平的请求集合 → 无从并发。
    pending: list[dict] = []
    for p in pages:
        rec = {"case_id": case, "page": p, "page_no_1based": p + 1,
               "source_pdf": str(pdf_path), "source_pdf_sha256": row.get("pdf_sha256") or ""}
        try:
            img, scale = render_page(pdf, p, cfg["render"])
        except Exception as e:                                            # noqa: BLE001
            errors.append(f"page {p} 渲染失败: {type(e).__name__}: {e}")
            C.err(f"[{case}] page {p} 渲染失败: {e}")
            rec.update(status="render_error", error=str(e))
            page_recs.append(rec)
            continue
        m = page_metrics(img, cfg["blank_guard"])
        kind = classify_page(m, cfg["blank_guard"])
        png = out / f"page_{p + 1:04d}.png"
        img.save(png)
        C.chmod(png)                     # 渲染页含患者影像/PII → 600
        rec.update(render_scale=round(scale, 3), **m, page_kind=kind,
                   png=png.name, png_sha256=C.sha256_file(png))
        audit.emit(case_id=case, page=p, kind="ocr_input",
                   png_sha256=rec["png_sha256"], ink_ratio=m["ink_ratio"],
                   gray_std=m["gray_std"], page_kind=kind)
        policy = str(cfg["blank_guard"].get("requested_page_policy") or "ocr_low_info")
        if kind == "low_info" and policy == "ocr_low_info" and p in target_pages:
            C.warn(f"{case} page {p}: 低信息（墨迹 {m['ink_ratio']:.4f}，std {m['gray_std']}）"
                   f"但是定位选中页 → 仍送 OCR，靠 hallucination_watchwords 兜底")
        elif kind != "ok":
            rec.update(status="skipped_low_information", blocks=[], n_blocks=0)
            C.warn(f"{case} page {p}: 判为 {kind}（墨迹 {m['ink_ratio']:.4f}，"
                   f"std {m['gray_std']}）—— 未送 OCR，下游不得当内容用")
            C.write_json(out / f"page_{p + 1:04d}.json", rec)
            page_recs.append(rec)
            continue
        # 变体开关（改动③，2026-10-09）：pipeline.yaml:ocr.variants 里写 false 的变体
        # **一律不跑**。旧代码只在「非首页」过滤 falsy 变体，首页照样把 false 的副变体跑了一遍：
        # 实测奉化__20230728_008 首页 plain 白跑 71.8s(0based 89) + 13.6s(0based 90) = 85.4s，
        # 而 plain 存在的理由（勾选框列）已随 2026-10-09.4 删掉 4 个勾选框列而消失。
        all_variants = {k: bool(v) for k, v in
                        (cfg["ocr"].get("variants") or {"layout": True}).items()}
        declared_primary = next(iter(all_variants))
        variants = {k: v for k, v in all_variants.items() if v}
        # 副变体（纯文本）只为首页的勾选框/编号服务；首程页只跑主变体，省下约 1/3 OCR 时间
        if cfg["ocr"].get("variants_frontpage_only") and p not in fp_pages:
            variants = {k: v for k, v in variants.items() if v}
        if not variants:
            errors.append(f"page {p}: ocr.variants 全部为 false，没有任何变体可跑")
            C.err(f"[{case}] page {p}: config/pipeline.yaml 的 ocr.variants 全是 false —— "
                  f"没有可跑的变体（至少要有 layout）")
            rec.update(status="config_error", error="ocr.variants all false",
                       blocks=[], n_blocks=0, variants={})
            C.write_json(out / f"page_{p + 1:04d}.json", rec)
            page_recs.append(rec)
            continue
        # 主变体 = 声明顺序里**第一个开着**的变体；声明的第一位被关掉时告警而不是静默换
        primary = declared_primary if variants.get(declared_primary) else next(iter(variants))
        if primary != declared_primary:
            C.warn(f"{case} page {p}: 主变体 {declared_primary} 被关掉，改用 {primary} "
                   f"（blocks/坐标策略都来自它）")
        page_stage = "frontpage" if p in fp_pages else "first_progress"
        pending.append({"p": p, "rec": rec, "png": png, "stage": page_stage,
                        "variants": variants, "primary": primary,
                        "jobs": {}, "results": {}})

    # ---- 步②：把「页 × 变体 × 带」的请求**摊平**后一次性发出 ----
    # 摊平到「带」而不是「按页并发」：分带页一页 4 条带，按页并发时同一页的 4 条带
    # 仍然串行（4 × 单带延迟的地板还在）。摊平后整例的 N 条带才是真并行。
    conc, conc_why = effective_concurrency(cfg["ocr"], "")
    want_conc = int(cfg["ocr"].get("concurrency") or 1)
    if conc > 1:
        C.log(f"  {case}: OCR 并发={conc}（{conc_why}）", quiet=quiet)
    elif want_conc > 1:
        C.warn(f"{case}: 配置 ocr.concurrency={want_conc}，本轮实际串行 —— {conc_why}")
    flat: list[tuple] = []
    for it in pending:
        for vname, use_layout in it["variants"].items():
            if resolve_backend(cfg["ocr"], it["stage"]) != "vllm":
                # paddlex 通道不并发：同卡两个 PaddleX 实测会退化成单线程 CPU（AGENTS.md §一）
                continue
            jobs = vllm_jobs_for(it["png"], cfg["ocr"],
                                 vllm_prompt_for(cfg["ocr"], vname, it["stage"]),
                                 it["stage"])
            it["jobs"][vname] = jobs
            flat.append((it, vname, jobs))
    all_jobs = [j for _it, _v, js in flat for j in js]
    executor = cf.ThreadPoolExecutor(max_workers=conc) if conc > 1 and all_jobs else None
    t_batch = time.time()
    try:
        if executor is not None:
            futures = [executor.submit(vllm_run_job, j, cfg["ocr"]) for j in all_jobs]
            all_results = [f.result() for f in futures]
        else:
            all_results = [vllm_run_job(j, cfg["ocr"]) for j in all_jobs]
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
    el_batch = round(time.time() - t_batch, 1)
    # 请求数**口径 A（索引计数）**：摊平后的 job 数，与并发度无关，不受日志交错影响
    n_requests = len(all_jobs)
    off = 0
    for it, vname, js in flat:
        it["results"][vname] = all_results[off:off + len(js)]
        off += len(js)

    # ---- 步③：按页组装 + 审计 + 落盘。拼接只认**带序**不认完成序 → 逐字节同形 ----
    for it in pending:
        p, rec, png, page_stage = it["p"], it["rec"], it["png"], it["stage"]
        vres: dict[str, dict] = {}
        vfail: list[str] = []
        for vname, use_layout in it["variants"].items():
            t0 = time.time()
            jobs = it["jobs"].get(vname) or []
            if it["results"].get(vname) is not None:
                pr, raw, e = vllm_join(jobs, it["results"][vname], cfg["ocr"])
            else:                                   # paddlex：整页单请求，留在本轮串行发
                pr, raw, e = ocr_page_variant(png, cfg["ocr"], bool(use_layout),
                                              stage=page_stage, variant=vname)
            el = round(time.time() - t0, 1)
            if pr is None:
                vfail.append(f"{vname}: {e}")
                C.err(f"[{case}] page {p} OCR 变体 {vname} 失败: {e}")
                continue
            blocks = extract_blocks(pr)
            # ★ 内容哈希（2026-10-10 round4 发现）：vllm 整页通道的 ocr_response_sha256
            #   是**整个响应**的哈希，里面含 chatcmpl 的 id / created → 同一页两次跑必然不同。
            #   分带通道的 raw 只有 band 元数据，所以稳定；整页通道不稳定。
            #   回归对照必须用下面这个只含正文的 ocr_text_sha256，不能用响应哈希。
            merged_text = "\n".join(b["block_content"] for b in blocks)
            text_sha = C.sha256_text(merged_text)
            loop = detect_repeat_loop(merged_text)
            vres[vname] = {"use_layout_detection": bool(use_layout), "status": "ok",
                           "elapsed_s": el, "n_blocks": len(blocks), "blocks": blocks,
                           "ocr_response_sha256": C.sha256_bytes(raw),
                           "ocr_text_sha256": text_sha,
                           "page_size": [pr.get("width"), pr.get("height")],
                           "backend": resolve_backend(cfg["ocr"], page_stage),
                           # 分带元数据（只有 vllm + ocr.bands 命中该阶段时非空）
                           "n_bands": int(pr.get("_n_bands") or 1),
                           "bands": pr.get("_bands") or None,
                           "n_requests": max(1, len(jobs)),
                           "truncated": bool(pr.get("_truncated")),
                           # 截断 != 坏页；复读循环才是。两个信号分开记（round 7 第1件）。
                           "repeat_loop": loop,
                           # 客户端复读早停（round 7 第4件）：命中即断流，命中详情只记 sha/长度
                           "repeat_abort": pr.get("_repeat_abort") or None}
            audit.emit(case_id=case, page=p, kind="ocr_output", variant=vname,
                       use_layout_detection=bool(use_layout), png_sha256=rec["png_sha256"],
                       ocr_response_sha256=vres[vname]["ocr_response_sha256"],
                       ocr_text_sha256=text_sha,
                       n_blocks=len(blocks), elapsed_s=el,
                       backend=vres[vname]["backend"],
                       n_requests=vres[vname]["n_requests"],
                       concurrency=conc,
                       truncated=vres[vname]["truncated"],
                       repeat_loop=loop["hit"], repeat_loop_why=loop["why"],
                       repeat_loop_run=loop["max_run"],
                       repeat_loop_uniq_ratio=loop["uniq_ratio"],
                       repeat_abort=bool(vres[vname]["repeat_abort"]),
                       repeat_abort_copies=(vres[vname]["repeat_abort"] or {}).get("n_copies"),
                       repeat_abort_line_sha16=(vres[vname]["repeat_abort"] or {}).get("line_sha16"))
            # 逐请求审计：并发下「日志行数」不再是请求数（见报告口径 B），所以每条请求
            # 单独落一行 kind=ocr_request —— 审计口径与索引口径必须都能对上账。
            for jb, rs in zip(jobs, it["results"].get(vname) or []):
                rsp = rs.get("pr") or {}
                audit.emit(case_id=case, page=p, kind="ocr_request", variant=vname,
                           backend=resolve_backend(cfg["ocr"], page_stage),
                           url=rsp.get("_url"),
                           band=(jb["band"] + 1) if jb.get("crop") else None,
                           crop=list(jb["crop"]) if jb.get("crop") else None,
                           ok=bool(rs.get("ok")),
                           truncated=bool(rsp.get("_truncated")),
                           finish_reason=rsp.get("_finish_reason"),
                           repeat_abort=bool(rsp.get("_repeat_abort")),
                           n_chars_at_abort=rsp.get("_n_chars_at_abort"),
                           elapsed_s=rs.get("elapsed_s"),
                           ocr_response_sha256=(C.sha256_bytes(rs["raw"])
                                                if rs.get("raw") else None))
            C.log(f"    p{p + 1:04d} {vname} ok blocks={len(blocks)} {el}s"
                  f"{' bands=' + str(vres[vname]['n_bands']) if vres[vname]['bands'] else ''}",
                  quiet=quiet)
        rec["elapsed_s"] = el_batch
        rec["ocr_batch_wall_s"] = el_batch
        if primary not in vres:
            errors.append(f"page {p} OCR 失败（主变体 {primary}）: {'; '.join(vfail)}")
            rec.update(status="ocr_error", error="; ".join(vfail) or "主变体无输出",
                       blocks=[], n_blocks=0, variants={})
            C.write_json(out / f"page_{p + 1:04d}.json", rec)
            page_recs.append(rec)
            continue
        if vfail:
            rec["variant_errors"] = vfail
            C.warn(f"{case} page {p}: 副变体失败 {'; '.join(vfail)}（主变体仍可用）")
        pv = vres[primary]
        rec["variants"] = {k: {kk: vv for kk, vv in v.items() if kk != "blocks"}
                           for k, v in vres.items()}
        for vname, v in vres.items():
            if vname != primary:
                rec[f"blocks_{vname}"] = v["blocks"]
        rec.update(status="ok", n_blocks=pv["n_blocks"], blocks=pv["blocks"],
                   ocr_response_sha256=pv["ocr_response_sha256"],
                   ocr_text_sha256=pv["ocr_text_sha256"], page_size=pv["page_size"])
        if crop_blocks_on:
            from PIL import Image
            # 步① 不再把渲染图留在内存里（--all-pages 上百页会撑爆内存）→ 按页重开 PNG。
            # PNG 就是步① img.save() 写的同一份字节，重开后像素与原来逐像素相同。
            with Image.open(png) as im:
                rec["crops"] = crop_blocks(im.convert("RGB"), pv["blocks"], out)
            if not rec["crops"]:
                C.warn(f"{case} page {p}: --crop-blocks 开着但没有可用 block_bbox"
                       f"（backend={resolve_backend(cfg['ocr'], page_stage)}，"
                       f"vllm 通道不返回块结构）→ 本轮未落裁剪图")
        C.write_json(out / f"page_{p + 1:04d}.json", rec)
        page_recs.append(rec)

    # ---- 步②：副变体按需补跑（只在首页页集缺必填列标签时补，最多 max_pages 页）----
    if append and only_pages is not None:
        # round16 第 5 步：第一批只有首页标题页 → 补跑判据（「首页必填列标签缺失」）会在
        # **不完整的页集**上评估，于是凭空触发（实测安吉两例各多 1 个请求 → NREQ 187→189）。
        # 第一批一律不做步②，交给第二次调用在完整页集上做 → 与产线逐字一致。
        od_summary = {"enabled": bool(cfg["ocr"].get("variants_on_demand")),
                      "triggered": False, "reason": "batch1_deferred_to_final"}
    else:
        od_summary = on_demand_variants(case, out, page_recs, fp_pages, cfg, audit, quiet)
    if od_summary.get("triggered"):
        C.log(f"  {case}: 按需补跑 {od_summary['variant']} "
              f"页={[x['page'] for x in od_summary['pages']]} "
              f"缺失列 {len(od_summary['missing_columns_before'])} → "
              f"{len(od_summary['missing_columns_after'])}", quiet=quiet)

    # ---- 页数对账：产物数 ≠ 源页数 → .incomplete + 非零退出（风险③）----
    produced = sum(1 for r in page_recs if r.get("status") in ("ok",))
    summary = {
        "case_id": case, "source_pdf": str(pdf_path), "source_pages": n_src,
        "pages_requested": len(pages_req), "pages_requested_list": pages_req,
        "pages_written": len(page_recs), "pages_ocr_ok": produced,
        "pages_low_information": sum(1 for r in page_recs
                                     if r.get("status") == "skipped_low_information"),
        "pages_low_info_ocr": sum(1 for r in page_recs
                                  if r.get("page_kind") == "low_info"
                                  and r.get("status") == "ok"),
        "pages_error": len(errors), "errors": errors,
        "locator": "all" if all_pages else ("index" if sel else "fallback_first3"),
        "on_demand_variants": od_summary,
        # 请求数口径 A（索引计数）：摊平后的 OCR 请求数，与并发度无关。
        # 口径 B（审计行数）= _audit.jsonl 里 kind=ocr_request 的行数 —— 两者必须相等。
        "n_requests": n_requests + int((od_summary or {}).get("n_requests") or 0),
        "n_requests_index": n_requests,
        "ocr_concurrency": conc,
        "ocr_concurrency_reason": conc_why,
        "ocr_endpoints": vllm_endpoints(cfg["ocr"]),
        "ocr_batch_wall_s": el_batch,
    }
    if append:
        # 续跑：_manifest.json 是**每例一份**，第二次调用会把第一次的账覆盖掉 → 必须合并。
        # 页数/状态一律按**磁盘上真实的 page_*.json** 重算，保证分批后的账与整例跑法逐字相同。
        prev_path = out / "_manifest.json"
        try:
            prev_m = C.read_json(prev_path) if prev_path.exists() else {}
        except Exception:                                              # noqa: BLE001
            prev_m = {}
        if not isinstance(prev_m, dict):
            prev_m = {}
        merged_req = sorted(set(pages_req) | set(prev_m.get("pages_requested_list") or []))
        on_disk = []
        for pj in sorted(out.glob("page_*.json")):
            try:
                rr = C.read_json(pj)
            except Exception:                                          # noqa: BLE001
                continue
            if isinstance(rr, dict) and "status" in rr:
                on_disk.append(rr)
        summary.update({
            "pages_requested": len(merged_req), "pages_requested_list": merged_req,
            "pages_written": len(on_disk),
            "pages_ocr_ok": sum(1 for r in on_disk if r.get("status") == "ok"),
            "pages_low_information": sum(1 for r in on_disk
                                         if r.get("status") == "skipped_low_information"),
            "pages_low_info_ocr": sum(1 for r in on_disk
                                      if r.get("page_kind") == "low_info"
                                      and r.get("status") == "ok"),
            "pages_error": len(errors) + int(prev_m.get("pages_error") or 0),
            "errors": list(prev_m.get("errors") or []) + errors,
            "n_requests": int(prev_m.get("n_requests") or 0) + summary["n_requests"],
            "n_requests_index": int(prev_m.get("n_requests_index") or 0) + n_requests,
            "locator": prev_m.get("locator") or summary["locator"],
            "s1_batches": list(prev_m.get("s1_batches") or []) + [batch_tag],
            "s1_reused_pages": int(prev_m.get("s1_reused_pages") or 0) + reused,
        })
        if (prev_m.get("on_demand_variants") or {}).get("triggered"):
            summary["on_demand_variants"] = prev_m["on_demand_variants"]
    C.write_json(out / "_manifest.json", summary)
    if append and only_pages is not None:
        pass                      # 第一批：这一例还没跑完，.done / .incomplete 都不落
    elif summary["pages_error"] or summary["pages_ocr_ok"] == 0:
        C.write_marker(bad_marker, "；".join(summary["errors"]) or "no page produced\n")
    else:
        C.write_marker(done_marker)
    pdf.close()
    return summary


# --------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="阶段① OCR：PDF → 逐页 PNG → 版面 OCR")
    ap.add_argument("--batch", required=True, help="批次名（run/wide/<batch>/）")
    C.add_case_args(ap)
    ap.add_argument("--discover", action="store_true", help="扫描 data_root 写 manifest.tsv 后退出")
    ap.add_argument("--all-pages", action="store_true", help="OCR 全部页（默认只 OCR 定位到的页）")
    ap.add_argument("--index", default="", help="定位缓存 JSON（默认读 pipeline.yaml:locator.index_file）")
    ap.add_argument("--max-pages", type=int, default=0, help="每例最多 OCR 几页（0=不限）")
    ap.add_argument("--crop-blocks", action="store_true", help="额外按 block_bbox 落裁剪图（验证风险③）")
    ap.add_argument("--append", action="store_true",
                    help="续跑模式（round16 第 5 步 locate↔s1 重叠）：不清 .done、不删本轮页集合"
                         "之外的产物、已落盘的页直接复用不重扫，_manifest.json 合并而非覆盖")
    ap.add_argument("--only-pages", default="",
                    help="本轮只 OCR 这些页（0based，逗号分隔），与定位页集合取交集；"
                         "其余页留给后续 --append 调用补齐")
    ap.add_argument("--force", action="store_true", help="已 .done 的例也重跑")
    ap.add_argument("--quiet", action="store_true", help="只压正常流水，异常照旧可见")
    ap.add_argument("--concurrency", type=int, default=-1,
                    help="覆盖 config/pipeline.yaml:ocr.concurrency（-1=用配置值）；"
                         "A/B 串行 vs 并发用。仍受 require_batch_invariant 闸门约束")
    ap.add_argument("--vllm-urls", default="",
                    help="逗号分隔的多个 vLLM 端点（多副本轮询），覆盖 ocr.vllm_urls。"
                         "同样受闸门约束：只有 BI=1 或每端点 --max-num-seqs 1 才允许 >1")
    a = ap.parse_args(argv)

    cfg = C.load_pipeline()
    only_pages: set[int] | None = None
    if a.only_pages.strip():
        only_pages = {int(x) for x in a.only_pages.replace(" ", ",").split(",") if x.strip()}
    if a.concurrency >= 0:
        cfg["ocr"]["concurrency"] = a.concurrency
        C.log(f"ocr.concurrency 覆盖为 {a.concurrency}（命令行）")
    if a.vllm_urls.strip():
        cfg["ocr"]["vllm_urls"] = [u.strip() for u in a.vllm_urls.split(",") if u.strip()]
        C.log(f"ocr.vllm_urls 覆盖为 {cfg['ocr']['vllm_urls']}（命令行）")
    run_root = Path(cfg["paths"]["run_root"])
    batch_dir = run_root / a.batch
    idx_dir = batch_dir / "00_index"
    manifest = idx_dir / "manifest.tsv"

    if a.discover:
        rows = discover(cfg["paths"]["data_root"])
        write_manifest(manifest, rows)
        C.log(f"发现 {len(rows)} 例 → {manifest}")
        return 0
    if not manifest.exists():
        C.err(f"缺 {manifest}；先跑 --discover")
        return 2

    rows = read_manifest(manifest)
    want = C.wanted_cases(a.case, a.case_file)
    if want:
        rows = [r for r in rows if r["case_id"] in want]
    if not rows:
        C.err("manifest 里没有匹配的例")
        return 2

    # 定位文件默认就在批次目录里；Path("") 是真值，必须先判空串
    index_path = a.index or str(cfg["locator"].get("index_file") or "").strip() \
        or str(idx_dir / "page_index.json")
    index = {}
    if index_path and Path(index_path).exists():
        blob = C.read_json(Path(index_path))
        index = blob.get("cases", blob) if isinstance(blob, dict) else {}

    audit = C.Audit(batch_dir / "_audit.jsonl", STAGE)
    n_bad = 0
    for r in rows:
        out = batch_dir / STAGE / r["case_id"]
        if (out / ".done").exists() and not a.force:
            C.log(f"  跳过（已 .done）{r['case_id']}", quiet=a.quiet)
            continue
        C.log(f"  {r['case_id']}  {r['pdf_path']}", quiet=a.quiet)
        s = process_case(r, cfg, batch_dir, audit, index,
                         all_pages=a.all_pages, max_pages=a.max_pages,
                         crop_blocks_on=a.crop_blocks, force=a.force, quiet=a.quiet,
                         only_pages=only_pages, append=a.append)
        if s["pages_error"] or s["pages_ocr_ok"] == 0:
            n_bad += 1
    C.log(f"完成：{len(rows)} 例，其中 {n_bad} 例有页级失败")
    return 1 if n_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
