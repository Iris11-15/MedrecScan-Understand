"""PaddleOCR-VL 中文网页界面（含同源反向代理）。

为什么需要这一层：
    PaddleX 的 /layout-parsing **不返回任何 CORS 响应头，OPTIONS 预检直接 405**。
    所以浏览器里任何独立页面（file:// 或别的端口）都调不通它。
    这里用 Python 做同源反向代理：网页和 /api/* 同源 -> 浏览器不再有跨域问题，
    真正的请求由 Python 发出去。

启动：
    & "D:\\visonalbe\\.venv_paddleocr\\python.exe" webui.py
    # 然后浏览器打开 http://127.0.0.1:8090

参数：
    --port       网页端口，默认 8090
    --backend    PaddleX 服务地址，默认 http://127.0.0.1:8100
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests

import vl_service          # 同一目录下的服务管理器（vLLM 按需启停）

# ---------------------------------------------------------------------------
# 分层流水线（延迟加载，模型只加载一次）
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


_layered = None
_layered_lock = threading.Lock()

# 两个 VL 后端统一按需启停。
#
# 为什么不"用完立刻停"：native 启动要 1-2 分钟，vLLM 启动约 60 秒且首次请求还要
# 100+ 秒编译 Triton 内核。处理完一页就停掉，下一页又要重付这几分钟。
# 所以用**空闲超时**：用的时候一直留着，闲置够久才释放显存。
#
# **两个后端互斥**：native 和 vLLM 各自都要约 7.5 GB 显存，8GB 卡上装不下两个。
# 所以启动任意一个之前，先把另一个停掉，否则必然 OOM。
BACKENDS = ("paddle", "vllm")

_vl_state = {
    "last_use": {"paddle": 0.0, "vllm": 0.0},
    "busy": False,        # 有请求在处理，回收器要避开
    "idle_timeout": 600,  # 秒；0 表示不自动停
    "enabled": True,
}
_reaper_started = False


def _be_running(name: str) -> bool:
    return vl_service.vllm_running() if name == "vllm" else vl_service.is_running()


def _be_stop(name: str) -> bool:
    if name == "vllm":
        return vl_service.vllm_stop(verbose=False)
    return vl_service.stop(wait=True, verbose=False)


def _be_start(name: str) -> bool:
    """启动某个后端；若另一个在跑，先停掉它（8GB 装不下两个）。"""
    other = "paddle" if name == "vllm" else "vllm"
    if _be_running(other):
        _log(f"[backend] 启动 {name} 前先停掉 {other}（8GB 显存装不下两个）")
        _be_stop(other)
        _vl_state["last_use"][other] = 0.0
    if name == "vllm":
        return vl_service.vllm_start(verbose=False)
    return vl_service.start(wait=True, verbose=False)


def _be_touch(name: str) -> None:
    _vl_state["last_use"][name] = time.time()


def _be_idle(name: str) -> float:
    t = _vl_state["last_use"][name]
    return (time.time() - t) if t else 0.0


def _start_reaper() -> None:
    """后台线程：哪个后端闲置超过阈值就停掉，把显存还回去。"""
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True

    def loop():
        while True:
            time.sleep(20)
            try:
                tmo = _vl_state["idle_timeout"]
                if not _vl_state["enabled"] or tmo <= 0 or _vl_state["busy"]:
                    continue
                for name in BACKENDS:
                    if not _vl_state["last_use"][name]:
                        continue
                    if not _be_running(name):
                        continue
                    idle = _be_idle(name)
                    if idle >= tmo:
                        _log(f"[reaper] {name} 空闲 {idle:.0f}s 超过 {tmo}s，"
                             f"自动停止释放显存")
                        _be_stop(name)
                        _vl_state["last_use"][name] = 0.0
            except Exception as e:                        # noqa: BLE001
                _log(f"[reaper] 异常: {e}")

    threading.Thread(target=loop, name="vl-reaper", daemon=True).start()
    _log(f"[reaper] 已启动，空闲阈值 {_vl_state['idle_timeout']}s，"
         f"管理后端 {list(BACKENDS)}")



def get_layered():
    """首次用到分层模式时才加载模型（约 15-30s），之后常驻。

    放在代理进程里而不是每次起子进程，是因为模型加载要几十秒 ——
    加载一次后每页只要 6-9 秒。
    """
    global _layered
    if _layered is None:
        with _layered_lock:
            if _layered is None:
                import layered as layered_mod
                p = layered_mod.LayeredPipeline(quiet=True)
                p.load()
                _layered = p
    return _layered

HERE = Path(__file__).resolve().parent
DEFAULT_BACKEND = "http://127.0.0.1:8100"
# 示例图用小裁剪块，10 秒内出结果；整页大图要几分钟，不适合当"试试"按钮
SAMPLE_CANDIDATES = [
    HERE / "output" / "sample_page.png",   # 含照片+图注，10 秒内出结果且有视觉内容
    HERE / "output" / "_crop_01.png",
    HERE / "output" / "demo_page.png",
]

PARSE_TIMEOUT = 900   # 整页复杂版面要几分钟，给足
PAGE_TARGET_PX = 2_000_000   # PDF 单页渲染目标像素（约 A4 @ 170dpi）
MAX_SIDE_PT = 3000           # 渲染后最长边上限（pt），防止宽扁页面被按面积放大
MAX_UPSCALE = 2.0            # 放大倍数上限：再大对识别没帮助，只烧时间


# ---------------------------------------------------------------------------
# PDF 拆页：服务端 /layout-parsing 是一次阻塞请求、没有任何进度流，
# 所以「跑到第几页」只能在代理层拆页才做得出来。
# ---------------------------------------------------------------------------
def iter_pdf_pages(raw: bytes, target_px: int = PAGE_TARGET_PX,
                   max_pages: int | None = None):
    """逐页渲染 PDF 并 yield (序号, 总页数, PNG 字节, 尺寸)。

    用生成器而不是先全渲染：100 页的 PDF 全渲进内存太浪费，
    而且流式本来就是一页一页发。
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(raw)
    total = len(doc)
    if max_pages:
        total = min(total, max_pages)

    for i in range(total):
        page = doc[i]
        w, h = page.get_size()                  # 单位 pt（1/72 英寸）
        # 三重约束，缺一个都会出问题：
        #   1) 按目标像素算缩放
        #   2) 限制最长边 —— 否则宽扁页面（如一条横幅）会被按面积放大到离谱尺寸
        #   3) 限制放大倍数 —— 超过 2 倍对识别没帮助，只是白烧时间和显存
        # 之前只做了第 1 条，一张 1365x120 的条状 PDF 被放大到 4777x420，
        # 单页识别要好几分钟，就是这个坑。
        scale = (target_px / max(1.0, w * h)) ** 0.5
        scale = min(scale, MAX_SIDE_PT / max(1.0, w, h), MAX_UPSCALE)
        scale = max(0.5, scale)
        pil = page.render(scale=scale).to_pil()
        buf = BytesIO()
        pil.save(buf, format="PNG")
        yield i, total, buf.getvalue(), (pil.width, pil.height)


def _chain_first(first, rest):
    """把已经取出来的第一页放回流里，避免为了拿总数而丢页。"""
    yield first
    yield from rest


class Handler(BaseHTTPRequestHandler):
    backend = DEFAULT_BACKEND
    server_version = "PaddleOCRVLLocalUI/1.0"
    # 流式响应要用 chunked，必须 HTTP/1.1；其余响应都带 Content-Length，也安全
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- 工具
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def log_message(self, fmt, *args):  # 降噪
        if self.path.startswith("/api/parse"):
            sys.stderr.write("  [proxy] %s\n" % (fmt % args))

    # ------------------------------------------------------- 流式（NDJSON）
    def _stream_start(self) -> None:
        """开一个 chunked 响应，之后可以一批批往外写。"""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")   # 必须，否则浏览器等 Content-Length 会卡住
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _stream_write(self, obj: dict) -> bool:
        """写一行 NDJSON。返回 False 表示客户端已断开。"""
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _stream_end(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------------------------------------------------------------- 分页识别
    def _handle_stream(self) -> None:
        """包一层：任何内部异常都要回传成 error 事件，而不是重置连接。

        之前没包，代理一抛异常浏览器就只看到连接被重置，
        排查时完全不知道发生了什么 —— 这种"静默失败"最难查。
        """
        self._streaming = False
        try:
            self._handle_stream_inner()
        except Exception as e:                                # noqa: BLE001
            import traceback
            traceback.print_exc()
            detail = f"{type(e).__name__}: {e}"
            try:
                if self._streaming:
                    self._stream_write({"type": "error", "index": -1,
                                        "msg": f"代理内部错误 {detail}"})
                    self._stream_write({"type": "done"})
                    self._stream_end()
                else:
                    self._json(500, {"msg": f"代理内部错误 {detail}"})
            except Exception:                                 # noqa: BLE001
                pass

    # ---------------------------------------------------------- 分层模式
    def _handle_layered(self, body: dict) -> None:
        """分层识别：快路径打底，难块升级到 VL。

        复用同一套 NDJSON 流式协议，前端不用为两种模式写两套逻辑。
        额外带上路由信息，前端可以显示「这一块走的是快路径还是 vLLM」。
        """
        t_req = time.time()
        raw = base64.b64decode(body["file"])
        _log(f"[layered] 解码请求体 {len(raw)/1e6:.2f}MB 用 {time.time()-t_req:.2f}s")
        is_pdf = body["fileType"] == 0
        vl_backend = body.get("vlBackend", "paddle")

        if is_pdf:
            try:
                pages = iter_pdf_pages(raw, max_pages=body.get("maxPages"))
                first = next(pages)
            except Exception as e:
                return self._json(400, {"msg": f"PDF 拆页失败: {e}"})
            stream = _chain_first(first, pages)
        else:
            stream = iter([(0, 1, raw, None)])

        # 先把流开出来，后面加载模型 / 启动 vLLM 的过程才能实时反馈给前端。
        # 注意：这之后就不能再用 _json 报错了（响应头已发出），只能发事件。
        self._stream_start()

        def stage(msg: str) -> None:
            self._stream_write({"type": "stage", "msg": msg})
            _log(f"[layered] stage: {msg}")

        try:
            if _layered is None:
                stage("正在加载分层模型（首次约 20–30 秒）…")
            pipe = get_layered()
        except Exception as e:            # noqa: BLE001
            self._stream_write({"type": "error", "index": -1,
                                "msg": f"分层模型加载失败: {e}"})
            self._stream_write({"type": "done"})
            self._stream_end()
            return

        # 模型是共享的，每次请求只改判定参数
        pipe.threshold = float(body.get("threshold", 0.90))
        pipe.vl_limit = body.get("vlLimit")
        pipe.vl_backend = vl_backend
        pipe.vl_url = ("http://127.0.0.1:8110" if vl_backend == "vllm"
                       else DEFAULT_BACKEND)

        # 需要的后端不在跑就先拉起来，并把过程告诉前端（否则用户只觉得卡住）
        if not _be_running(vl_backend) and pipe.vl_autostart:
            stage(f"正在启动 {vl_backend} 服务"
                  + ("（约 1 分钟）…" if vl_backend == "vllm"
                     else "（约 1–2 分钟）…"))
            _vl_state["busy"] = True
            try:
                ok = _be_start(vl_backend)
            finally:
                _vl_state["busy"] = False
            if ok:
                stage(f"{vl_backend} 已就绪"
                      + ("；首次调用要编译内核，可能再等 1–2 分钟"
                         if vl_backend == "vllm" else ""))
            else:
                stage(f"{vl_backend} 启动失败，难块将标记为未处理")
        _be_touch(vl_backend)          # 有请求用到它就刷新空闲计时

        started = False
        _vl_state["busy"] = True
        tmpdir = Path(tempfile.mkdtemp(prefix="webui_layered_"))
        try:
            for idx, total, png, size in stream:
                if not started:
                    if not self._stream_write({"type": "start", "pages": total,
                                               "isPdf": is_pdf,
                                               "mode": "layered"}):
                        return
                    started = True
                tmp = tmpdir / f"p{idx}.png"
                tmp.write_bytes(png)      # 版面模型和 PP-OCRv5 都只吃路径/numpy
                t_proc = time.time()
                try:
                    rep = pipe.process(tmp)
                except Exception as e:    # noqa: BLE001
                    if not self._stream_write({"type": "error", "index": idx,
                                               "msg": f"第 {idx + 1} 页失败: {e}"}):
                        return
                    continue
                t_proc = time.time() - t_proc
                # 单图不回传原图（浏览器本地已有）；PDF 才需要回传渲染页
                page = self._report_to_page(rep, png, echo_image=is_pdf)
                t_send = time.time()
                ok = self._stream_write({"type": "page", "index": idx,
                                         "total": total, "result": page})
                t_send = time.time() - t_send
                _log(f"[layered] 第{idx+1}页 处理 {t_proc:.2f}s · "
                     f"发送 {t_send:.2f}s（{len(json.dumps(page, ensure_ascii=False))/1e6:.2f}MB）"
                     f" · 距请求开始 {time.time()-t_req:.2f}s")
                if not ok:
                    return
        finally:
            _vl_state["busy"] = False
            _be_touch(vl_backend)
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

        self._stream_write({"type": "done"})
        self._stream_end()

    @staticmethod
    def _report_to_page(rep: dict, png: bytes, echo_image: bool = False) -> dict:
        """把 layered.py 的报告转成前端统一消费的页结构。

        分两部分：
          - 正文部分（parsing_res_list / markdown），与整页 VL 模式同形
          - 路由信息（每块走的哪条路），前端显示徽章与统计

        echo_image：是否把页面图回传。
            **单张图片上传时不要回传** —— 浏览器本地已有这个文件，
            回传等于把 3 MB 的 base64 再发一遍。实测这一步占了 8.2 秒
            （总耗时 11 秒里的绝大部分）。
            只有 PDF 才需要，因为浏览器那边没有渲染出来的页图。
        """
        W, H = (rep.get("size") or [1, 1])
        blocks = []
        for e in rep.get("blocks") or []:
            if e.get("route") == "drop":
                continue
            blocks.append({
                "block_label": e.get("label") or "text",
                "block_content": e.get("text") or "",
                "block_bbox": e.get("bbox"),
                "block_order": None,
                "route": e.get("route"),          # fast / vl / placeholder / skipped
                "reason": e.get("reason"),
                "min_score": e.get("min_score"),
            })
        # 分层不产生阅读顺序，按位置给个近似序（先上下、再左右）
        ordered = sorted((b for b in blocks if b["block_bbox"]),
                         key=lambda b: (b["block_bbox"][1], b["block_bbox"][0]))
        for i, b in enumerate(ordered, 1):
            b["block_order"] = i

        md = "\n\n".join(b["block_content"] for b in ordered if b["block_content"])
        out = {
            "prunedResult": {"width": W, "height": H, "parsing_res_list": blocks},
            "markdown": {"text": md},
            "_layered": {"stages": rep.get("stages"),
                         "summary": rep.get("summary")},
        }
        if echo_image:
            out["inputImage"] = base64.b64encode(png).decode()
        return out

    def _handle_stream_inner(self) -> None:
        """逐页识别，每页完成立刻推给前端。

        两种模式：
            mode=vl       整页交给 PaddleOCR-VL 产线（版面结构最完整，慢，约 300s/页）
            mode=layered  分层：PP-DocLayout + PP-OCRv5 打底，只把难块升级到 VL
                          （约 6-9s/页；难块可走 vllm 后端，每块 0.2-1s）

        图片没有页可分，走同一套协议只发一页，前端不用分两种情况写。
        """
        try:
            body = self._read_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"msg": f"请求体不是合法 JSON: {e}"})
        if not body.get("file"):
            return self._json(400, {"msg": "缺少 file 字段"})
        if body.get("fileType") not in (0, 1):
            return self._json(400, {"msg": "fileType 必须是 0(PDF) 或 1(图片)"})

        if body.get("mode") == "layered":
            return self._handle_layered(body)

        raw = base64.b64decode(body["file"])
        is_pdf = body["fileType"] == 0

        # 下游请求统一按图片发（PDF 已在本地渲染成 PNG）
        base_payload = {k: v for k, v in body.items()
                        if k not in ("file", "fileType", "maxPages", "mode")}
        base_payload["fileType"] = 1

        if is_pdf:
            try:
                pages = iter_pdf_pages(raw, max_pages=body.get("maxPages"))
                first = next(pages)                       # 先取第一页，才知道总数
            except Exception as e:
                return self._json(400, {"msg": f"PDF 拆页失败: {e}"})
            stream = _chain_first(first, pages)
        else:
            stream = iter([(0, 1, raw, None)])

        self._stream_start()

        # 整页模式固定走 native 产线；不在跑就自动拉起来
        if not _be_running("paddle"):
            self._stream_write({"type": "stage",
                                "msg": "正在启动 native 服务（约 1–2 分钟）…"})
            _vl_state["busy"] = True
            try:
                ok = _be_start("paddle")
            finally:
                _vl_state["busy"] = False
            if ok:
                self._stream_write({"type": "stage", "msg": "native 已就绪"})
            else:
                self._stream_write({"type": "stage",
                                    "msg": "native 启动失败，请检查日志"})
        _be_touch("paddle")

        started = False
        _vl_state["busy"] = True
        try:
            for idx, total, png, size in stream:
                if not started:
                    if not self._stream_write({"type": "start", "pages": total,
                                               "isPdf": is_pdf}):
                        return
                    started = True
                payload = dict(base_payload)
                payload["file"] = base64.b64encode(png).decode()
                try:
                    r = requests.post(f"{self.backend}/layout-parsing",
                                      json=payload, timeout=PARSE_TIMEOUT)
                    data = r.json()
                except requests.RequestException as e:
                    if not self._stream_write({"type": "error", "index": idx,
                                               "msg": f"第 {idx + 1} 页转发失败: {e}"}):
                        return
                    continue
                except ValueError:
                    if not self._stream_write({"type": "error", "index": idx,
                                               "msg": f"第 {idx + 1} 页返回非 JSON"}):
                        return
                    continue

                if data.get("errorCode") != 0:
                    if not self._stream_write({"type": "error", "index": idx,
                                               "msg": data.get("errorMsg"),
                                               "code": data.get("errorCode")}):
                        return
                    continue

                items = (data.get("result") or {}).get("layoutParsingResults") or []
                page = items[0] if items else {}
                page["_pageSize"] = list(size) if size else None
                # 单图不回传原图：浏览器本地已有，回传纯属浪费带宽和时间
                if not is_pdf:
                    page.pop("inputImage", None)
                    page.pop("outputImages", None)
                if not self._stream_write({"type": "page", "index": idx,
                                           "total": total, "result": page}):
                    return                            # 客户端关了，立刻停手
        finally:
            _vl_state["busy"] = False
            _be_touch("paddle")

        self._stream_write({"type": "done"})
        self._stream_end()

    # ---------------------------------------------------------------- 路由
    def do_GET(self):
        path = urlparse(self.path).path      # 必须剥掉 ?query，否则 /?demo=1 会 404

        if path in ("/", "/index.html"):
            html = HERE / "webui.html"
            if not html.exists():
                return self._send(500, b"webui.html not found", "text/plain")
            return self._send(200, html.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/health":
            try:
                r = requests.get(f"{self.backend}/health", timeout=5).json()
                return self._json(200, {"ok": r.get("errorCode") == 0,
                                        "msg": r.get("errorMsg")})
            except requests.RequestException as e:
                return self._json(200, {"ok": False, "msg": f"后端不可达: {e}"})

        if path == "/api/status":
            paddle_ok = False
            try:
                paddle_ok = (requests.get(f"{self.backend}/health",
                                          timeout=3).json()
                             .get("errorCode") == 0)
            except requests.RequestException:
                pass
            vllm_ok, vllm_models = False, []
            try:
                r = requests.get("http://127.0.0.1:8110/v1/models", timeout=3)
                if r.status_code == 200:
                    vllm_ok = True
                    vllm_models = [m.get("id") for m in r.json().get("data", [])]
            except requests.RequestException:
                pass
            return self._json(200, {
                "paddle": paddle_ok, "vllm": vllm_ok, "vllmModels": vllm_models,
                "layeredLoaded": _layered is not None,
                "backend": self.backend,
                # 每个后端的空闲信息，前端显示倒计时用
                "idle": {
                    "paddle": {"seconds": round(_be_idle("paddle"), 1)
                               if paddle_ok else None},
                    "vllm": {"seconds": round(_be_idle("vllm"), 1)
                             if vllm_ok else None},
                },
                "idleTimeout": _vl_state["idle_timeout"],
                "autoStop": bool(_vl_state["enabled"] and
                                 _vl_state["idle_timeout"] > 0),
            })

        if path == "/api/sample":
            for p in SAMPLE_CANDIDATES:
                if p.exists():
                    return self._json(200, {
                        "name": p.name,
                        "fileType": 0 if p.suffix.lower() == ".pdf" else 1,
                        "file": base64.b64encode(p.read_bytes()).decode(),
                    })
            return self._json(404, {"msg": "没有可用的示例图"})

        return self._json(404, {"msg": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/parse-stream":
            return self._handle_stream()
        if path != "/api/parse":
            return self._json(404, {"msg": "not found"})

        try:
            body = self._read_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"msg": f"请求体不是合法 JSON: {e}"})

        if not body.get("file"):
            return self._json(400, {"msg": "缺少 file 字段"})
        # 服务端要求显式给 0/1，省略会 422
        if body.get("fileType") not in (0, 1):
            return self._json(400, {"msg": "fileType 必须是 0(PDF) 或 1(图片)"})

        try:
            r = requests.post(f"{self.backend}/layout-parsing",
                              json=body, timeout=PARSE_TIMEOUT)
        except requests.Timeout:
            return self._json(504, {"msg": "后端超时（超过 15 分钟）"})
        except requests.RequestException as e:
            return self._json(502, {"msg": f"转发失败: {e}"})

        try:
            data = r.json()
        except ValueError:
            return self._json(502, {"msg": f"后端返回了非 JSON（HTTP {r.status_code}）"})

        # 后端的业务错误码透传，前端统一按 errorCode 判断
        return self._json(200, data)


def main() -> int:
    ap = argparse.ArgumentParser(description="PaddleOCR-VL 中文网页界面")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--backend", default=DEFAULT_BACKEND)
    ap.add_argument("--vl-idle-timeout", type=int, default=600,
                    help="VL 后端空闲多少秒后自动停止释放显存；0 表示不自动停（默认 600）。"
                         "对 native 和 vLLM 都生效")
    ap.add_argument("--vl-prewarm", action="store_true",
                    help="界面启动时就把已选后端拉起来，省掉第一次请求的等待")
    args = ap.parse_args()

    Handler.backend = args.backend.rstrip("/")
    _vl_state["idle_timeout"] = args.vl_idle_timeout
    _start_reaper()

    # 启动前提示后端状态，但不阻止启动（后端可能晚一点起来）
    try:
        h = requests.get(f"{Handler.backend}/health", timeout=4).json()
        backend_ok = h.get("errorCode") == 0
    except requests.RequestException:
        backend_ok = False

    vllm_ok = vl_service.vllm_running()

    if args.vl_prewarm:
        # 两个后端互斥，预热只拉一个：优先 vLLM（大多数场景更快）
        target = None if vllm_ok else ("vllm" if not backend_ok else "paddle")
        if target:
            print(f"按 --vl-prewarm 启动 {target}（后台进行）…")
            _vl_state["busy"] = True

            def _warm(name=target):
                try:
                    _be_start(name)
                    _be_touch(name)
                finally:
                    _vl_state["busy"] = False

            threading.Thread(target=_warm, daemon=True).start()

    url = f"http://{args.host}:{args.port}"
    print("=" * 62)
    print("  PaddleOCR-VL 中文界面")
    print("=" * 62)
    print(f"  网页      : {url}")
    print(f"  native    : {Handler.backend}  "
          f"{'[在线]' if backend_ok else '[未启动，首次用到时自动拉起]'}")
    print(f"  vLLM      : http://127.0.0.1:8110  "
          f"{'[在线]' if vllm_ok else '[未启动，首次用到时自动拉起]'}")
    if args.vl_idle_timeout > 0:
        print(f"  空闲回收  : 闲置 {args.vl_idle_timeout}s 后自动停止，把显存还回去")
    else:
        print("  空闲回收  : 已关闭，后端会一直占着显存")
    print(f"  注意      : native 与 vLLM 互斥（各需约 7.5 GB，8GB 卡装不下两个），"
          f"\n              启动一个会自动停掉另一个")
    print()
    print("  Ctrl+C 停止")
    print("=" * 62)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())


