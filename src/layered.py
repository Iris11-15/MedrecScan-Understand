r"""分层识别：轻量 OCR 打底，大模型只啃难点，且按需启停。

为什么要分层
------------
实测同一张 1524x1368 的印刷体页面：

    PP-OCRv5 mobile      8.8 秒     143 行文本，最低置信度 0.928
    PaddleOCR-VL 1.6     约 300 秒   31 个结构化块

而 8GB 显存上还有个硬约束：VL 模型常驻吃 7.5/8.15 GB，
留给分层的两个小模型只剩 0.65 GB，会把它们拖慢 2.6 倍：

    阶段          VL 常驻    释放后
    版面检测       14.2s      6.5s
    轻量 OCR        8.3s      2.3s
    每页合计       22.4s      8.8s

所以本工具的策略是：**快路径常驻（很小），VL 只在真的遇到难块时拉起，用完可释放。**
服务管理见 vl_service.py。

流水线
------
    页面图
      ├─ PP-DocLayoutV3（版面检测）→ 块：类别 + 坐标
      ├─ PP-OCRv5 mobile（文字识别）→ 文本行：内容 + 坐标 + 置信度
      ↓  文本行按位置归属到块
      ↓  逐块决策
          table/formula/chart/seal  →  必须 VL
          text 且全部行置信度高     →  直接用轻量结果（0 大模型成本）
          text 但有低置信行         →  裁剪送 VL（疑似手写/模糊）
          image                     →  只占位，不识别
      ↓  合并输出 + 分阶段耗时

用法
----
    # 单页，纯轻量（不用大模型，最快）
    python layered.py page.png --no-vl

    # 单页，自动分层（遇到难块才拉起 VL 服务）
    python layered.py page.png

    # 批量：模型只加载一次，VL 用完自动释放
    python layered.py pages/ --vl-autostop

    # 多个文件
    python layered.py a.png b.png c.png

    # 只管 VL 服务
    python vl_service.py status|start|stop
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from collections import Counter
from io import BytesIO
from pathlib import Path

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

import requests
from PIL import Image

import vl_service

VL_API = os.environ.get("OCR_API", "http://127.0.0.1:8100/layout-parsing")

# 轻量模型**真的做不了**，必须交给 VL。
# 不要把 image 放进来：图片块没有文字要识别，送 VL 是纯浪费。
# figure_title / vision_footnote 是普通文本，轻量模型读得很好。
HARD_LABELS = {"table", "formula", "display_formula", "inline_formula",
               "chart", "seal"}
PLACEHOLDER_LABELS = {"image"}
NOISE_LABELS = {"header", "header_image", "footer", "footer_image", "number"}
# 块类型 -> VL 任务提示词（PaddleOCR-VL 的任务前缀）。
# 为什么需要：表格/公式/图表若按通用 OCR 提示词送模型，会退化成"读成一串文本"，
# 行列对应关系不可恢复。产线端的 promptLabel 取值需实测确认。
VL_TASK = {"table": "Table Recognition:",
           "formula": "Formula Recognition:",
           "display_formula": "Formula Recognition:",
           "inline_formula": "Formula Recognition:",
           "chart": "Chart Recognition:"}
VL_TASK_PADDLE = {"table": "table",
                  "formula": "formula",
                  "display_formula": "formula",
                  "inline_formula": "formula",
                  "chart": "chart"}


CN = {"text": "正文", "doc_title": "主标题", "paragraph_title": "段落标题",
      "title": "标题", "table": "表格", "formula": "公式", "image": "图片",
      "chart": "图表", "seal": "印章", "figure_title": "图注",
      "vision_footnote": "图片说明", "ocr": "文字"}

IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".pdf"}


# ---------------------------------------------------------------- 几何工具
def bbox_of(box):
    """4 点多边形或 [x1,y1,x2,y2] 统一成 [x1,y1,x2,y2]。"""
    if box is None:
        return None
    try:
        arr = list(box)
    except TypeError:
        return None
    if len(arr) == 4 and all(isinstance(v, (int, float)) for v in arr):
        return [float(v) for v in arr]
    xs, ys = [], []
    for pt in arr:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            xs.append(float(pt[0])); ys.append(float(pt[1]))
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def center(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def contains(outer, inner):
    cx, cy = center(inner)
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


# 模型缓存目录：PaddleX 把官方模型下到 ~/.paddlex/official_models/<名字>，
# 老版 PaddleOCR 下到 ~/.paddleocr/。**只用来"说清楚在不在"**，不参与加载逻辑 ——
# 强开一个本地没有的模型，PaddleOCR 会尝试联网下载，离线环境直接初始化失败，
# 而失败信息跟"没开这个开关"在日志里几乎长得一样，所以这里主动报一次。
_MODEL_ROOTS = (".paddlex/official_models", ".paddlex/custom_models",
                ".paddleocr/whl", ".paddleocr")


def model_cache_hits(name: str) -> list:
    """本地模型缓存里命中该名字的路径（可能多条，交给调用方取第一条）。"""
    home = Path.home()
    hits = []
    for rel in _MODEL_ROOTS:
        for cand in (home / rel / name, home / rel / (name + "_infer")):
            if cand.exists():
                hits.append(str(cand))
    return hits


# ---------------------------------------------------------------- 流水线
class LayeredPipeline:
    """模型只加载一次；之后 process() 可以反复调用（批量就靠这个）。"""

    def __init__(self, threshold: float = 0.90, pad: int = 10,
                 no_vl: bool = False, vl_limit: int | None = None,
                 vl_autostart: bool = True, quiet: bool = False,
                 vl_backend: str = "paddle", vl_url: str | None = None,
                 vl_model: str = "PaddleOCR-VL", vl_max_tokens: int = 2048):
        self.threshold = threshold
        self.pad = pad
        self.no_vl = no_vl
        self.vl_limit = vl_limit
        self.vl_autostart = vl_autostart
        self.quiet = quiet
        self.vl_backend = vl_backend                    # paddle | vllm
        self.vl_url = (vl_url or
                       ("http://127.0.0.1:8110" if vl_backend == "vllm"
                        else VL_API.rsplit("/", 1)[0]))
        self.vl_model = vl_model
        self.vl_max_tokens = vl_max_tokens
        self.layout_model = None
        self.ocr_model = None
        self.load_time = {"layout": 0.0, "fast_ocr": 0.0}
        self.vl_started_by_us = False
        # 2026-10-07：异常/警告必须计数并进汇总。以前 --quiet 把异常一起压掉，
        # 结果「118 页只出 84 个产物」这种事没人看得见。
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def log(self, *a):
        if not self.quiet:
            print(*a, flush=True)

    def warn(self, *a):
        """警告：不受 --quiet 影响（--quiet 只压正常流水，不压异常）。"""
        self.warnings.append(" ".join(str(x) for x in a))
        print(*a, file=sys.stderr, flush=True)

    def fail(self, *a):
        """异常：不受 --quiet 影响，并且会让进程以非零码退出。"""
        self.errors.append(" ".join(str(x) for x in a))
        print(*a, file=sys.stderr, flush=True)

    # ---------------------------------------------------------- 加载
    def load(self):
        import paddlex
        from paddleocr import PaddleOCR

        t = time.time()
        self.layout_model = paddlex.create_model("PP-DocLayoutV3")
        self.load_time["layout"] = time.time() - t

        t = time.time()
        # ★ 这里是「识别质量」最直接的旋钮，但**不能默认打开**：
        #   方向分类（PP-LCNet_x1_0_doc_ori）、去畸变（UVDoc）、server 版模型
        #   都必须在本地模型缓存里。没下过的话 PaddleOCR 初始化直接失败 →
        #   整条 OCR 通道起不来。所以默认保持「关 + mobile」，要开走环境变量。
        #
        #   实测依据（桌面那 106 份，用 Windows 离线 OCR 四向打分当裁判，
        #   审计脚本 `_orient_audit.py`，明细 `_anji/orient_*.json`）：
        #     · 奉化 / 桐乡二院 / 玉环第一 / 长兴人民 → 正立（方向 0 得分是次优的 2~4 倍）
        #     · 安吉 → **页级混合**，而且是有规律地交替：
        #           p1..p16  逆90 / 顺90 交替   ← 双面原件的正反面相差 180°
        #           p18..p40 0 / 180 交替
        #           p44..p104 多为 180
        #       （※ 我先前在这条注释里写"安吉内容页整体横躺 90°"是**错的** ——
        #        那是拿 380px 缩略图肉眼判的；OCR 打分显示同一份 PDF 里三种方向都有。）
        #   ⇒ 固定角度救不了安吉，只有**逐页**方向模型能救。
        #      这也正是 `MEDREC_ROTATE`（pdf2png.py）只当兜底、不当主力的原因。
        #
        #   推荐（前提：模型已在缓存里）：
        #     MEDREC_OCR_ORIENT=1                 方向分类 + 文本行方向 ← 安吉必需
        #     MEDREC_OCR_UNWARP=1                 去畸变（页边弯曲时）
        #     MEDREC_OCR_DET=PP-OCRv5_server_det  密集表格换 server 版
        #     MEDREC_OCR_REC=PP-OCRv5_server_rec
        def _flag(name, default=False):
            v = os.environ.get(name)
            if v is None:
                return default
            return v.strip().lower() in ("1", "true", "yes", "on")

        orient = True
        unwarp = _flag("MEDREC_OCR_UNWARP")
        det = os.environ.get("MEDREC_OCR_DET", "PP-OCRv5_mobile_det")
        rec = os.environ.get("MEDREC_OCR_REC", "PP-OCRv5_mobile_rec")
        # ★ 开了哪些非默认模型就**逐条报出来，并说明模型在不在本地**。
        #   不报的话，"开了方向分类但其实没生效"和"没开"在日志里长得一模一样。
        self.log("轻量 OCR 配置：方向分类=%s 去畸变=%s det=%s rec=%s"
                 % ("开" if orient else "关", "开" if unwarp else "关", det, rec))
        for nm in ((["PP-LCNet_x1_0_doc_ori"] if orient else []) +
                   (["UVDoc"] if unwarp else []) +
                   [d for d in (det, rec) if "mobile" not in d]):
            hits = model_cache_hits(nm)
            self.log("  模型 %-26s %s" % (nm, ("命中 " + hits[0]) if hits else
                     "**本地没找到** —— PaddleOCR 会尝试联网下载，离线环境会直接失败"))
        self.ocr_model = PaddleOCR(
            use_doc_orientation_classify=orient,
            use_doc_unwarping=unwarp,
            use_textline_orientation=orient,
            text_detection_model_name=det,
            text_recognition_model_name=rec)
        self.load_time["fast_ocr"] = time.time() - t

        self.log(f"模型已加载：版面 {self.load_time['layout']:.1f}s + "
                 f"轻量 OCR {self.load_time['fast_ocr']:.1f}s "
                 f"= {sum(self.load_time.values()):.1f}s（一次性）")

    # ---------------------------------------------------------- 两个快阶段
    def _layout(self, path: Path):
        """PP-DocLayoutV3。

        实测返回 {"res": {"boxes": [{"label":..,"score":..,"coordinate":[..]}]}}
        —— boxes 是**字典列表**，不是三个平行数组。
        另外这个模块只吃 numpy 或路径字符串，传 PIL 对象会被静默忽略（返回 0 块）。
        """
        t = time.time()
        out = list(self.layout_model.predict(str(path)))
        dt = time.time() - t

        blocks = []
        for r in out:
            d = r.json if hasattr(r, "json") else r
            d = d.get("res", d) if isinstance(d, dict) else d
            raw = d.get("boxes") or []
            if raw and isinstance(raw[0], dict):
                for it in raw:
                    bb = bbox_of(it.get("coordinate") or it.get("bbox"))
                    if bb:
                        blocks.append({"label": it.get("label") or "text",
                                       "bbox": bb,
                                       "score": float(it.get("score", 1.0))})
            else:
                labels = d.get("labels") or []
                scores = d.get("scores") or []
                for i, b in enumerate(raw):
                    bb = bbox_of(b)
                    if bb:
                        blocks.append({
                            "label": labels[i] if i < len(labels) else "text",
                            "bbox": bb,
                            "score": float(scores[i]) if i < len(scores) else 1.0})
        return blocks, dt

    def _fast_ocr(self, path: Path):
        t = time.time()
        out = list(self.ocr_model.predict(str(path)))
        dt = time.time() - t

        lines = []
        for r in out:
            d = r.json if hasattr(r, "json") else r
            d = d.get("res", d) if isinstance(d, dict) else d
            texts = d.get("rec_texts") or []
            scores = d.get("rec_scores") or []
            boxes = d.get("rec_boxes")
            if boxes is None:
                boxes = d.get("dt_polys") or d.get("rec_polys") or []
            for i, txt in enumerate(texts):
                bb = bbox_of(boxes[i]) if i < len(boxes) else None
                if bb is None:
                    continue
                lines.append({"text": txt, "bbox": bb,
                              "score": float(scores[i]) if i < len(scores) else 1.0})
        return lines, dt

    # ---------------------------------------------------------- 归属
    @staticmethod
    def _assign(blocks, lines):
        for b in blocks:
            b["lines"] = []
        orphans = []
        for ln in lines:
            best, best_area = None, None
            for b in blocks:
                if contains(b["bbox"], ln["bbox"]):
                    a = area(b["bbox"])
                    if best_area is None or a < best_area:   # 取最贴身的块
                        best, best_area = b, a
            (best["lines"] if best is not None else orphans).append(ln)

        if orphans:                                   # 块外的行按行间距聚段
            orphans.sort(key=lambda l: (l["bbox"][1], l["bbox"][0]))
            cur = []
            for ln in orphans:
                if cur and ln["bbox"][1] - cur[-1]["bbox"][3] > 18:
                    blocks.append(LayeredPipeline._orphan(cur)); cur = []
                cur.append(ln)
            if cur:
                blocks.append(LayeredPipeline._orphan(cur))
        return blocks

    @staticmethod
    def _orphan(lines):
        return {"label": "text", "score": 1.0, "orphan": True, "lines": lines,
                "bbox": [min(l["bbox"][0] for l in lines),
                         min(l["bbox"][1] for l in lines),
                         max(l["bbox"][2] for l in lines),
                         max(l["bbox"][3] for l in lines)]}

    @staticmethod
    def _crop_box(img, W, H, bbox, pad):
        """按 bbox 裁剪；裁剪框不可用时**回退整页图**，绝不抛异常、绝不静默跳过。

        2026-10-07 修（实测取证 page_0003：图宽 W=1663，OCR 行框 x=2291~2326）：
        坏框有两种形态，都会让 PIL.Image.crop 抛
        "Coordinate 'right' is less than 'left'"，把整页带进 except 里丢掉 ——
          ① 框本身退化：right<=left 或 bottom<=top；
          ② 框整体越界：x1>W（或 y1>H），把 right 夹到 W 之后
             right < left，**夹取这个动作本身把框搞反了**。
        所以必须「先夹取、再校验」，而不是只校验原始 bbox。
        返回 (crop, 不可用原因 dict 或 None)。
        """
        x1, y1, x2, y2 = bbox
        left, top = max(0, x1 - pad), max(0, y1 - pad)
        right, bottom = min(W, x2 + pad), min(H, y2 + pad)
        why = []
        if x2 <= x1 or y2 <= y1:
            why.append("原始框退化 right<=left 或 bottom<=top")
        if right <= left or bottom <= top:
            why.append("夹取到图幅后裁剪框反演/为空")
        if x1 > W or y1 > H or x2 < 0 or y2 < 0:
            why.append("原始框整体在图外")
        if why:
            return img, {"bbox": [int(x1), int(y1), int(x2), int(y2)],
                         "clamped": [int(left), int(top), int(right), int(bottom)],
                         "image": [int(W), int(H)], "why": "；".join(why)}
        return img.crop((left, top, right, bottom)), None

    # ---------------------------------------------------------- VL
    def _call_vl(self, crop: Image.Image, label: str | None = None, timeout: int = 600):
        """把裁剪块交给 VL 后端识别。

        两种后端协议不同，实测速度差 40 倍以上（同一张裁剪图）：
            native  PaddleOCR /layout-parsing     8 – 23 秒
            vllm    OpenAI /v1/chat/completions   0.2 秒（首次含 JIT 编译约 118s）
        """
        buf = BytesIO(); crop.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()

        if self.vl_backend == "vllm":
            body = {
                "model": self.vl_model,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": VL_TASK.get(label or "", "OCR:")},
                ]}],
                "max_tokens": self.vl_max_tokens,
                "temperature": 0.0,
            }
            t = time.time()
            r = requests.post(f"{self.vl_url}/v1/chat/completions",
                              json=body, timeout=timeout)
            dt = time.time() - t
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip(), dt

        # 默认：PaddleOCR-VL 产线接口
        body = {"file": b64,
                "fileType": 1,                  # 0=PDF 1=图片，服务端不猜
                "useLayoutDetection": False,    # 跳过版面检测，整块当一个元素
                "promptLabel": VL_TASK_PADDLE.get(label or "", "ocr"),
                "returnMarkdownImages": False,
                "maxPixels": 1003520}
        t = time.time()
        r = requests.post(f"{self.vl_url}/layout-parsing", json=body,
                          timeout=timeout).json()
        dt = time.time() - t
        if r.get("errorCode") != 0:
            raise RuntimeError(r.get("errorMsg"))
        txt = ""
        for it in ((r.get("result") or {}).get("layoutParsingResults") or []):
            for b in ((it.get("prunedResult") or {}).get("parsing_res_list") or []):
                txt += (b.get("block_content") or "")
        return txt.strip(), dt

    def _vl_alive(self) -> bool:
        if self.vl_backend == "vllm":
            return vl_service.vllm_running()
        return vl_service.is_running()

    def _ensure_vl(self) -> bool:
        if self._vl_alive():
            return True
        if not self.vl_autostart:
            return False
        self.log(f"  检测到难块，拉起 VL 服务（{self.vl_backend}）…")
        if self.vl_backend == "vllm":
            if vl_service.vllm_start(verbose=not self.quiet):
                self.vl_started_by_us = True
                return True
            return False
        if vl_service.ensure_running(verbose=not self.quiet):
            self.vl_started_by_us = True
            return True
        return False

    # ---------------------------------------------------------- 主处理
    def process(self, path: Path) -> dict:
        img = Image.open(path).convert("RGB")
        W, H = img.size
        rep = {"source": path.name, "size": [W, H], "stages": {}}
        t_all = time.time()

        blocks, t_layout = self._layout(path)
        rep["stages"]["layout"] = round(t_layout, 2)
        self.log(f"  版面检测 {len(blocks):>3} 块  {t_layout:>6.2f}s")

        lines, t_ocr = self._fast_ocr(path)
        rep["stages"]["fast_ocr"] = round(t_ocr, 2)
        self.log(f"  轻量 OCR {len(lines):>3} 行  {t_ocr:>6.2f}s")

        blocks = self._assign(blocks, lines)

        # ---- 逐块决策
        decided, vl_queue = [], []
        for b in blocks:
            label = b["label"]
            ls = b.get("lines") or []
            scores = [l["score"] for l in ls]
            min_s = min(scores) if scores else None
            e = {"label": label, "bbox": [int(v) for v in b["bbox"]],
                 "line_count": len(ls), "min_score": min_s}

            if label in NOISE_LABELS:
                e.update(route="drop", reason="页眉页脚页码，丢掉")
            elif label in PLACEHOLDER_LABELS:
                e.update(route="placeholder", text="",
                         reason=f"{CN.get(label, label)}——只需占位")
            elif label in HARD_LABELS:
                e.update(route="vl", reason=f"{CN.get(label, label)} 轻量模型做不了")
            elif not ls:
                e.update(route="drop", reason="块内无文本行")
            elif min_s is not None and min_s < self.threshold:
                e.update(route="vl", reason=f"最低置信度 {min_s:.3f} < {self.threshold}")
            else:
                e.update(route="fast", reason=f"全部行 ≥ {self.threshold}",
                         text="\n".join(l["text"] for l in ls))
            decided.append(e)
            if e["route"] == "vl":
                vl_queue.append(e)

        n_fast = sum(1 for e in decided if e["route"] == "fast")
        n_drop = sum(1 for e in decided if e["route"] == "drop")
        n_ph = sum(1 for e in decided if e["route"] == "placeholder")
        self.log(f"  分层决策：轻量 {n_fast} / 占位 {n_ph} / 丢弃 {n_drop} / "
                 f"升级 VL {len(vl_queue)}")

        # ---- 升级到 VL
        t_vl = 0.0
        n_vl_err = 0                                      # VL 调用异常条数
        n_degraded = 0                                    # 退化 bbox 回退整页数
        if self.no_vl:
            for e in vl_queue:
                e.update(route="skipped", reason=e["reason"] + "（--no-vl）")
            if vl_queue:
                self.log(f"  跳过 VL（--no-vl），{len(vl_queue)} 块未处理")
        elif vl_queue:
            if not self._ensure_vl():
                for e in vl_queue:
                    e.update(route="unavailable",
                             reason=e["reason"] + "（VL 服务不可用）")
                self.log("  VL 服务不可用，难块标记为未处理")
            else:
                queue = vl_queue[:self.vl_limit] if self.vl_limit else vl_queue
                if self.vl_limit and len(vl_queue) > self.vl_limit:
                    self.log(f"  送 VL {len(queue)} 块（上限 {self.vl_limit}，"
                             f"其余标记未处理）")
                if self.vl_backend == "vllm":
                    self.log("  后端 vllm：首次请求要按真实尺寸编译 Triton 内核，"
                             "约 1-2 分钟，之后每块约 0.2s")
                for i, e in enumerate(queue, 1):
                    crop, degraded = self._crop_box(img, W, H, e["bbox"], self.pad)
                    if degraded is not None:
                        e["degraded_bbox"] = degraded
                        n_degraded += 1
                        self.warn(f"    [警告] {path.name} 第 {i}/{len(queue)} 块 "
                                  f"裁剪框不可用：{degraded['why']} "
                                  f"bbox={degraded['bbox']} "
                                  f"夹取后={degraded['clamped']} "
                                  f"图幅={degraded['image']} "
                                  f"-> 已回退整页图，内容未丢")
                    try:
                        txt, dt = self._call_vl(crop, label=e.get("label"))
                        e.update(text=txt, vl_seconds=round(dt, 2))
                        t_vl += dt
                        self.log(f"    VL {i}/{len(queue)} "
                                 f"{CN.get(e['label'], e['label'])} {dt:.1f}s "
                                 f"-> {txt[:36]!r}")
                    except Exception as ex:                # noqa: BLE001
                        e.update(text="", vl_error=str(ex))
                        n_vl_err += 1
                        self.fail(f"    [异常] {path.name} VL {i}/{len(queue)} "
                                  f"失败: {ex}")
                for e in vl_queue[len(queue):]:
                    # 改成 skipped，否则界面上会显示「大模型」徽章但内容是空的，
                    # 看起来像识别失败，实际是主动跳过的
                    e["route"] = "skipped"
                    e["reason"] += "（超出块数上限，未处理）"
        else:
            self.log("  无需 VL —— 全部块都走通了轻量路径")

        rep["stages"]["vl_total"] = round(t_vl, 2)
        rep["stages"]["page_total"] = round(
            rep["stages"]["layout"] + rep["stages"]["fast_ocr"] + t_vl, 2)
        rep["stages"]["wall"] = round(time.time() - t_all, 2)
        rep["summary"] = {
            "blocks": len(decided), "fast": n_fast, "vl": len(vl_queue),
            "placeholder": n_ph, "dropped": n_drop,
            "label_counts": dict(Counter(e["label"] for e in decided)),
            "vl_errors": n_vl_err, "degraded_bbox": n_degraded,
        }
        rep["blocks"] = decided
        return rep


# ---------------------------------------------------------------- CLI
def collect_inputs(args) -> list[Path]:
    out: list[Path] = []
    for raw in args.image:
        p = Path(raw)
        if p.is_dir():
            out += sorted(f for f in p.iterdir()
                          if f.suffix.lower() in IMG_EXT)
        elif p.exists():
            out.append(p)
        else:
            print(f"跳过（不存在）: {p}", file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="分层识别：轻量 OCR + VL 只啃难点")
    ap.add_argument("image", nargs="+", help="图片/PDF 路径，或目录")
    ap.add_argument("--threshold", type=float, default=0.90,
                    help="低于此置信度的文本块升级到 VL（默认 0.90）")
    ap.add_argument("--no-vl", action="store_true", help="纯轻量，完全不调用大模型")
    ap.add_argument("--vl-limit", type=int, default=None,
                    help="每页送 VL 的块数上限，兜住最坏耗时")
    ap.add_argument("--vl-autostart", dest="vl_autostart", action="store_true",
                    default=True, help="遇到难块自动拉起 VL 服务（默认开）")
    ap.add_argument("--no-vl-autostart", dest="vl_autostart",
                    action="store_false", help="不自动拉起，只在本就运行时才用")
    ap.add_argument("--vl-autostop", action="store_true",
                    help="全部处理完后停掉 VL 服务并释放显存")
    ap.add_argument("--vl-backend", choices=["paddle", "vllm"], default="paddle",
                    help="paddle = Windows 上 PaddleOCR 产线接口；"
                         "vllm = WSL 里的 vLLM（实测快 40 倍以上）")
    ap.add_argument("--vl-url", default=None,
                    help="VL 服务地址，默认按 backend 推断")
    ap.add_argument("--vl-model", default="PaddleOCR-VL",
                    help="vllm 后端的模型名")
    ap.add_argument("--pad", type=int, default=10, help="裁剪时向外扩的像素")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    files = collect_inputs(args)
    if not files:
        print("没有可处理的文件", file=sys.stderr)
        return 1

    pipe = LayeredPipeline(threshold=args.threshold, pad=args.pad,
                           no_vl=args.no_vl, vl_limit=args.vl_limit,
                           vl_autostart=args.vl_autostart, quiet=args.quiet,
                           vl_backend=args.vl_backend, vl_url=args.vl_url,
                           vl_model=args.vl_model)
    pipe.load()

    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "novl" if args.no_vl else "layered"
    reports, t_all = [], time.time()
    failed: list[tuple[str, str]] = []

    for i, f in enumerate(files, 1):
        pipe.log(f"\n[{i}/{len(files)}] {f.name}")
        try:
            rep = pipe.process(f)
        except Exception as ex:                            # noqa: BLE001
            pipe.fail(f"  [异常] {f.name} 整页处理失败: {ex}")
            failed.append((f.name, str(ex)))
            continue
        reports.append(rep)
        jp = out_dir / f"{f.stem}_{suffix}.json"
        jp.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                      encoding="utf-8")

    wall = time.time() - t_all

    # ---- 汇总
    if reports:
        pages = len(reports)
        per_page = [r["stages"]["page_total"] for r in reports]
        n_vl = sum(r["summary"]["vl"] for r in reports)
        load = sum(pipe.load_time.values())
        vl_start = vl_service.vram_used()

        print("\n" + "=" * 60)
        print(f"  处理 {pages} 页成功 / {len(failed)} 页失败"
              f"（输入 {len(files)} 页）")
        print(f"  一次性加载（模型）    {load:>8.2f}s")
        print(f"  每页推理合计          {sum(per_page):>8.2f}s")
        print(f"  每页平均              {sum(per_page)/pages:>8.2f}s")
        print(f"  其中升级到 VL 的块    {n_vl:>8d} 个")
        n_vl_err = sum(r["summary"].get("vl_errors", 0) for r in reports)
        n_degr = sum(r["summary"].get("degraded_bbox", 0) for r in reports)
        print(f"  VL 调用失败           {n_vl_err:>8d} 次")
        print(f"  退化 bbox 回退整页    {n_degr:>8d} 次")
        print(f"  ──────────────────────────")
        print(f"  墙钟总计              {wall + load:>8.2f}s")
        print()
        print(f"  对比：整页直接走 PaddleOCR-VL 约 300s/页 "
              f"= {300*pages:.0f}s")
        avg = sum(per_page) / pages
        if avg > 0:
            print(f"  ★ 每页提速约 {300/avg:.0f}x")
        if vl_start is not None:
            print(f"  当前显存占用 {vl_start} MiB")
        print("=" * 60)

    # ---- 异常/警告汇总：不受 --quiet 影响，永远打到 stderr
    if pipe.errors or pipe.warnings or failed:
        print("\n" + "!" * 60, file=sys.stderr)
        print(f"  !! 异常 {len(pipe.errors)} 条 / 警告 {len(pipe.warnings)} 条 / "
              f"整页丢失 {len(failed)} 页", file=sys.stderr)
        for who, err in failed:
            print(f"     丢页: {who} -> {err}", file=sys.stderr)
        for line in (pipe.errors + pipe.warnings)[:40]:
            print(f"     {line}", file=sys.stderr)
        if len(pipe.errors) + len(pipe.warnings) > 40:
            print(f"     （其余 "
                  f"{len(pipe.errors) + len(pipe.warnings) - 40} 条省略）",
                  file=sys.stderr)
        print("!" * 60, file=sys.stderr)

    # ---- 收尾
    is_vllm = args.vl_backend == "vllm"
    running = (vl_service.vllm_running() if is_vllm
               else vl_service.is_running())
    if args.vl_autostop:
        print()
        if running:
            if is_vllm:
                vl_service.vllm_stop(verbose=not args.quiet)
            else:
                vl_service.stop(verbose=not args.quiet)
    elif pipe.vl_started_by_us:
        hint = ("python vl_service.py vllm stop" if is_vllm
                else "python vl_service.py stop")
        print(f"\nVL 服务仍在运行（本次自动拉起）。要释放显存：\n  {hint}")

    # 有页真的丢了 → 非零退出，让上层（batch_run.sh / 人）能发现。
    # 注意：VL 单块调用失败不算丢页（产物仍在，只是该块文本为空），只计数。
    if failed:
        print(f"\n[x] {len(failed)} 页处理失败，产物不完整（详见上面的 !! 汇总）",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
