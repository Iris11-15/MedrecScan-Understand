#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/ocr_healthcheck.py —— OCR 服务**退化自检**（把静默失败变成显式失败）。

背景（2026-10-10 实测，reports/ocr-vllm-round2-20261010.md §三(a)）：
  1) 用 vl-env 起 PaddleX，`/health` 仍返回 200 / Healthy，但推理退化成**单线程 CPU**
     （GPU util 0%、256×256 小图 76 s、整页 12 min 不返回）。
  2) 8100 还在跑时在同卡再起一个 PaddleX，新实例同样退化。
两种情况「服务看起来是活的」，产线却慢到不可用 —— 只看 /health 抓不出来。

判据：打一张**合成** 256×256 小图（不含任何真实患者像素，可安全进日志），
      超过 --max-s 未返回 → 判退化，打印明确错误 + nvidia-smi 证据，**非零退出**。
      顺带打印该服务进程在 nvidia-smi 里的利用率（附带证据）。

退出码：0 正常 / 2 服务无响应 / 3 退化（超时）/ 4 响应成功但内容为空 / 1 用法或前置错误

冷启动不算退化（2026-10-10 实测踩坑）：服务刚起时**第一个**请求要付 CUDA graph
capture / 内核 JIT 的钱 —— ocr-env PaddleX 起服务 6 s 后 /health 已 Healthy，
但第一张 256x256 在 10 s 内不回。所以先打一张**不计时**的 warmup（--warmup-s，
默认 120 s），再打计时的 probe；warmup 自身超过 --warmup-s 才直接判退化。
阈值不给 --max-s 时按后端分档：paddlex 120 s / vllm 10 s。

★ 2026-10-10 复测更正（两条，都推翻旧记录）：
  1) **256×256 合成小图在健康的 ocr-env PaddleX 上要 73.9-76.2 s**
     （workspace/repro5/hc_paddlex_before.out warmup=73.92s；
      workspace/repro5/rollback_clean_now.out warmup=76.2s gpu0_sm_max=20%）。
     旧文档把「256×256 76 s」当成 vl-env 退化的特征值 —— 那个数**健康态也是这个值**，
     所以小图耗时**不能**单独判退化。旧默认 paddlex 30 s 因此把健康服务判成退化
     （probe 30.03s 超时 → rc=3，而同一份输出里 nvidia-smi 显示该进程在 GPU0 上 18%）。
  2) **GPU sm 利用率才是判据**：健康 paddlex 整页 26.2 s / sm 峰值 36%，
     256×256 76 s / sm 峰值 20%；退化（vl-env、或同卡第二个 paddle 进程）sm 全程 0%
     且单核 ~100% CPU（workspace/repro5/diag2/main.out：离线整页 >150 s、sm 0%）。
     → 现在 probe 期间持续采 sm，峰值 < --min-sm 直接判退化（rc=3）。
  ⚠ 采样必须在**客户端进程**里做。在加载了 CUDA 的进程（paddle/vllm）里
     `subprocess.run(["nvidia-smi"])` 会 fork 一个 VIRT≈47 GB 的进程，把正在跑的
     推理拖死（实测离线整页 32.8 s → >150 s、sm 0%）—— 那是测量工具自己造的退化。

★ 2026-10-11 round 14 第5c件（本副本 = 5c 补丁，未落地 src/）：
  **sm 判据的证据力问题：小图 probe 在 vLLM 上只有 0.14-0.23 s，任何秒级采样器都抓不到。**
  二选一里选了「**真实整页探针**」而不是「把采样粒度降到 ≤50 ms」，理由：
    a) `nvidia-smi --query-gpu` 单次查询本身就要几十毫秒，50 ms 粒度做不到稳定，
       而且高频 fork nvidia-smi 会污染被测服务的耗时（红线：测量仪不许改变被测对象）；
    b) 整页 probe ~25 s，0.2 s 粒度能采到 ~100 个样本 → sm 判据自动获得证据力；
    c) 更重要的：round 10/13 的退化真实形态是「**VLM 解码退到 CPU**」，
       只有真实整页（长解码）才会经过那条路；256×256 小图 max_tokens=64 根本不经过。
  → 新增 `--probe page --page-image <png>`；page 模式下 sm 判据升级为**硬判据**（pn≥1 即参与判死）。
  → 采样粒度 `--sample-interval`（默认 1.0 s；page 模式自动降到 0.2 s）。

★ 2026-10-10 round 7 更正（第2件）：**sm 判据在 vLLM 上从来没生效过**。
  vLLM 的 256×256 probe 只要 0.14-0.23 s，1 s 间隔的 sm 采样器一次都没落进请求窗口
  （round 6 §8 #8：4 次 probe 全部 gpu0_sm_max=-1% (n=0)）→ 旧代码里
  `0 <= pmax < min_sm` 因为 pmax=-1 永远为假 → vLLM 的 rc=0 其实**只由耗时阈值**给出，
  AGENTS.md 规定的「请求期间 GPU sm」这道防线等于不存在。
  → **主判据换成 compute-apps 无窗口闸门**：`nvidia-smi --query-compute-apps` 列的是
    **持有显存的进程**，这个集合与请求窗口无关（vLLM 的 EngineCore 全程持 17 GiB），
    所以不依赖采样时机。断言「目标服务的进程树 ∩ compute-apps ≠ ∅」，
    空集 = 该服务不在任何 GPU 上 = 退化（rc=3）。
    进程树必须**递归取子孙**：vLLM 监听端口的是 APIServer，真正持显存的是 EngineCore。
  → sm 判据**保留为辅助证据**（n≥1 时打印），不再是 vLLM 的主判据。
  ⚠ 局限（如实记）：compute-apps 只证明「该进程持有显存」，不证明「这一次推理走了 GPU」。
    一个持有显存却把解码放到 CPU 上的进程会通过本闸门 —— 那种情况仍要靠 sm / 耗时兜。
  ⚠ 查询本身失败（nvidia-smi 不可用）→ **fail-closed 判退化 rc=3**，不猜「应该没问题」。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

_SRC = os.environ.get("R14_SRC", "/home/ubuntu/medrec/src")
sys.path.insert(0, _SRC)
from wide import common as C  # noqa: E402
from wide import s1_ocr as S  # noqa: E402


_PII_SAFE_PREVIEW = False


def _preview(txt: str) -> str:
    """page 模式打的是**真实患者页** —— 预览必须打码（红线⑨）：只留长度 + sha256 前 16。"""
    if not _PII_SAFE_PREVIEW:
        return json.dumps(txt[:60], ensure_ascii=False)
    return (f"<真实页正文已打码 chars={len(txt)} "
            f"sha256={C.sha256_text(txt)[:16]}>")


def synth_png(size: int = 256) -> str:
    """合成一张确定性的 256×256 灰度「文字样」图（无 PII）。"""
    from PIL import Image, ImageDraw
    img = Image.new("L", (size, size), 255)
    dr = ImageDraw.Draw(img)
    for y in range(24, size - 20, 28):
        for x in range(16, size - 24, 22):
            dr.rectangle([x, y, x + 14, y + 16], outline=0)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _descendants(pid: str) -> set[str]:
    """pid + 其全部后代 pid（BFS /proc/<pid>/stat 的 ppid 字段）。拿不到就只返回 {pid}。"""
    kids: dict[str, list[str]] = {}
    try:
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                ppid = d.joinpath("stat").read_text().split()[3]
            except Exception:                                       # noqa: BLE001
                continue
            kids.setdefault(ppid, []).append(d.name)
    except Exception:                                               # noqa: BLE001
        return {pid}
    seen, stack = {pid}, [pid]
    while stack:
        for k in kids.get(stack.pop(), []):
            if k not in seen:
                seen.add(k)
                stack.append(k)
    return seen


def listen_pid(port: int) -> str:
    """端口上监听的 pid（vLLM = APIServer）。拿不到返回 ""。"""
    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:                                               # noqa: BLE001
        return ""
    for ln in out.splitlines():
        if f":{port} " in ln:
            tok = [t for t in ln.split() if "pid=" in t]
            if tok:
                return tok[-1].split("pid=")[1].split(",")[0]
    return ""


def service_tree(port: int) -> tuple[str, set[str]]:
    """(监听 pid, 含全部子孙的进程树)。

    ★ 监听端口的那个 pid **未必**是占 GPU 的那个：vLLM 是 APIServer 监听 8110，
      真正持有显存的是它的子进程 EngineCore（实测 pid 1402273 监听 / 1402862 占 5.9 GiB）。
      只查监听 pid 会把「一切正常」的 vLLM 误报成「不在任何 GPU 上 = CPU 推理」。
    """
    pid = listen_pid(port)
    return pid, (_descendants(pid) if pid else set())


def compute_apps() -> tuple[list[str], str]:
    """nvidia-smi --query-compute-apps 的**原始行**（pid, gpu_uuid, used_memory）。

    这是**无窗口**判据：它列的是当前持有显存的进程，与请求窗口无关，
    所以不受 probe 只有 0.14-0.23 s 的影响。返回 (行列表, 错误串)。
    """
    try:
        r = subprocess.run(["nvidia-smi",
                            "--query-compute-apps=pid,gpu_uuid,used_memory",
                            "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:                                          # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"
    if r.returncode != 0:
        return [], (r.stderr or r.stdout or f"rc={r.returncode}").strip()[:200]
    return [ln for ln in r.stdout.splitlines() if ln.strip()], ""


def compute_app_pids() -> tuple[set[str], list[str], str]:
    rows, err = compute_apps()
    if err:
        return set(), rows, err
    return {ln.split(",")[0].strip() for ln in rows}, rows, ""


class ComputeAppsWatch:
    """probe 期间反复取 compute-apps，取**并集**。查询瞬时完成，不依赖采样窗口。

    必须在客户端进程做（本工具不加载 CUDA，fork 便宜且无害）。
    """

    def __init__(self, interval: float = 0.2) -> None:
        self.interval = interval
        self._pids: set[str] = set()
        self._rows: list[str] = []
        self._err = ""
        self._n = 0
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def start(self) -> None:
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            pids, rows, err = compute_app_pids()
            self._n += 1
            if err:
                self._err = err
            else:
                self._pids |= pids
                for ln in rows:
                    if ln not in self._rows:
                        self._rows.append(ln)
            self._stop.wait(self.interval)

    def take(self) -> tuple[set[str], list[str], str, int]:
        return set(self._pids), list(self._rows), self._err, self._n

    def stop(self) -> None:
        self._stop.set()
        if self._th is not None:
            self._th.join(timeout=5)


def gpu_evidence(port: int) -> list[str]:
    """该端口进程在 nvidia-smi 里的利用率/显存（拿不到就写 unknown，不猜）。"""
    out: list[str] = []
    try:
        pid, tree = service_tree(port)
        if not pid:
            return [f"  nvidia-smi: unknown（端口 {port} 上没有监听进程）"]
        out.append(f"  服务 pid={pid} 进程树={sorted(tree)}")
        pids, rows, err = compute_app_pids()
        hit = [ln for ln in rows if ln.split(",")[0].strip() in tree]
        out.append("  compute-apps: " + ("; ".join(hit) if hit else
                    (f"查询失败 unknown（{err}）" if err else
                     f"进程树 {sorted(tree)} 都不在任何 GPU 上（=CPU 推理，典型退化）")))
        r2 = subprocess.run(["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used",
                             "--format=csv,noheader"], capture_output=True, text=True, timeout=15)
        out.append("  " + " | ".join(x.strip() for x in r2.stdout.strip().splitlines()))
        try:
            st = Path(f"/proc/{pid}/task")
            nthreads = len(list(st.iterdir())) if st.exists() else -1
            out.append(f"  线程数={nthreads}（单线程 ~100% CPU 是 vl-env 退化的特征）")
        except Exception:
            pass
    except Exception as e:                                            # noqa: BLE001
        out.append(f"  nvidia-smi: unknown（{type(e).__name__}: {e}）")
    return out


class GpuUtil:
    """在**客户端进程**里采 GPU sm 利用率（本工具不加载 CUDA，fork 便宜且无害）。

    判退化用「请求期间 GPU 有没有干活」，不用「小图耗时」—— 见模块 docstring 更正 1)。
    """

    def __init__(self, gpu: str = "0", interval: float = 1.0) -> None:
        interval = max(0.05, float(interval))
        self.gpu = str(gpu)
        self.interval = interval
        self._mx = -1
        self._n = 0
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def start(self) -> None:
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu",
                                    "--format=csv,noheader,nounits", "-i", self.gpu],
                                   capture_output=True, text=True, timeout=5)
                for ln in r.stdout.split():
                    if ln.strip().isdigit():
                        v = int(ln.strip())
                        if v > self._mx:
                            self._mx = v
                        self._n += 1
                        break
            except Exception:                                       # noqa: BLE001
                pass
            self._stop.wait(self.interval)

    def take(self) -> tuple[int, int]:
        """取当前窗口的 (sm 峰值, 样本数) 并清零窗口。没采到 → 峰值 = -1。"""
        mx, n = self._mx, self._n
        self._mx, self._n = -1, 0
        return mx, n

    def stop(self) -> None:
        self._stop.set()
        if self._th is not None:
            self._th.join(timeout=5)


def probe_paddlex(base_url: str, b64: str, timeout_s: float, cfg_ocr: dict,
                  stage: str = "locate") -> tuple[bool, str, str, dict]:
    payload = {"file": b64, "fileType": 1, "useLayoutDetection": False,
               "promptLabel": cfg_ocr.get(f"prompt_label_{stage}", cfg_ocr["prompt_label"]),
               "returnMarkdownImages": False,
               "maxPixels": int(cfg_ocr["max_pixels"])}
    url = base_url.rstrip("/") + cfg_ocr.get("endpoint", "/layout-parsing")
    st, js, e = C.http_json(url, payload, int(timeout_s), 0)
    if js is None:
        return False, "", f"status={st} {e}", {}
    try:
        pr = js["result"]["layoutParsingResults"][0]["prunedResult"]
    except (KeyError, IndexError, TypeError):
        return False, C.canon_json(js)[:400], "响应结构异常", {}
    txt = " ".join((b.get("block_content") or "") for b in pr.get("parsing_res_list", []))
    return True, txt, "", {"n_blocks": len(pr.get("parsing_res_list") or [])}


def probe_vllm(url: str, b64: str, timeout_s: float, cfg_ocr: dict,
               max_tokens: int = 64, stage: str = "locate"
               ) -> tuple[bool, str, str, dict]:
    payload = {"model": str(cfg_ocr.get("vllm_model") or "PaddleOCR-VL"),
               "messages": [{"role": "user", "content": [
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
                   {"type": "text", "text": S.vllm_prompt_for(cfg_ocr, stage=stage)}]}],
               "max_tokens": int(max_tokens), "temperature": 0.0}
    st, js, e = C.http_json(url.rstrip("/") + "/v1/chat/completions", payload,
                            int(timeout_s), 0)
    if js is None:
        return False, "", f"status={st} {e}", {}
    try:
        txt = ((js["choices"][0].get("message") or {}).get("content")) or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return False, C.canon_json(js)[:400], "响应结构异常", {}
    usage = js.get("usage") or {}
    return True, txt, "", {"completion_tokens": usage.get("completion_tokens"),
                           "prompt_tokens": usage.get("prompt_tokens"),
                           "finish_reason": (js.get("choices") or [{}])[0].get("finish_reason")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OCR 服务退化自检（小图超时判退化）")
    ap.add_argument("--backend", default="", help="paddlex|vllm；默认读 pipeline.yaml:ocr.backend")
    ap.add_argument("--url", default="", help="覆盖服务地址")
    ap.add_argument("--size", type=int, default=256, help="合成小图边长（默认 256）")
    ap.add_argument("--max-s", dest="max_s", type=float, default=None,
                    help="小图允许的最长耗时，超过即判退化。默认按后端分档："
                         "paddlex 120 s（健康态实测 73.9-76.2 s）/ vllm 10 s")
    ap.add_argument("--gpu", default="0", help="采哪张卡的 sm 利用率（默认 0）")
    ap.add_argument("--min-sm", dest="min_sm", type=float, default=5.0,
                    help="paddlex：请求期间 GPU sm 峰值低于此值 → 判退化（默认 5%%）")
    ap.add_argument("--warmup-s", dest="warmup_s", type=float, default=120.0,
                    help="warmup 请求超时（不计时）；0 = 跳过 warmup（默认 120 s）")
    ap.add_argument("--reps", type=int, default=1, help="连打几次（默认 1；取最差值）")
    ap.add_argument("--probe", choices=["small", "page"], default="small",
                    help="small=合成 256x256 小图（旧行为）/ page=真实整页 PNG（5c 推荐）")
    ap.add_argument("--page-image", dest="page_image", default="",
                    help="page 模式必填：真实整页 PNG 路径（产线 01_ocr/page_XXXX.png）")
    ap.add_argument("--page-max-tokens", dest="page_max_tokens", type=int, default=4096,
                    help="page 模式 max_tokens（默认 4096，与产线 s1 同量级）")
    ap.add_argument("--page-max-s", dest="page_max_s", type=float, default=60.0,
                    help="page 模式耗时阈值（默认 60 s；健康整页实测 24.5-33.0 s）")
    ap.add_argument("--sample-interval", dest="sample_interval", type=float, default=1.0,
                    help="GPU sm 采样粒度秒（page 模式自动降到 0.2 s）")
    a = ap.parse_args(argv)

    C.clear_proxy()
    cfg = C.load_pipeline()
    oc = cfg["ocr"]
    backend = (a.backend or S.resolve_backend(oc, "locate")).strip().lower()
    if backend == "vllm":
        url = a.url or str(oc["vllm_url"])
        port = int(url.rsplit(":", 1)[-1].split("/")[0])
    else:
        url = a.url or str(oc["base_url"])
        port = int(url.rsplit(":", 1)[-1].split("/")[0])

    stage = "locate"
    max_tokens = 64
    if a.probe == "page":
        if not a.page_image:
            print("[healthcheck] ✗ --probe page 需要 --page-image <png>")
            return 1
        png = Path(a.page_image)
        if not png.exists():
            print(f"[healthcheck] ✗ 找不到整页 PNG：{png}")
            return 1
        global _PII_SAFE_PREVIEW
        _PII_SAFE_PREVIEW = True
        b64 = base64.b64encode(png.read_bytes()).decode("ascii")
        stage = "s1"
        max_tokens = a.page_max_tokens
        if a.max_s is None:
            a.max_s = a.page_max_s
        if a.sample_interval >= 1.0:
            a.sample_interval = 0.2
        print(f"[healthcheck] probe=page image={png.name} bytes={png.stat().st_size} "
              f"max_tokens={max_tokens} sample_interval={a.sample_interval}s")
    else:
        b64 = synth_png(a.size)
        if a.max_s is None:
            a.max_s = 120.0 if backend == "paddlex" else 10.0
    print(f"[healthcheck] backend={backend} url={url} probe_mode={a.probe} "
          f"max_s={a.max_s} reps={a.reps}")
    # 先探一次「服务是否应答」—— 与「是否退化」分开报，避免把 OOM 和 CPU 退化混为一谈
    try:
        if backend == "vllm":
            r = subprocess.run(["curl", "-s", "-m", "5", url.rstrip("/") + "/v1/models"],
                               capture_output=True, text=True, timeout=10)
            print(f"[healthcheck] /v1/models 首字节 ok={bool(r.stdout.strip())} "
                  f"len={len(r.stdout)}")
        else:
            r = subprocess.run(["curl", "-s", "-m", "5", url.rstrip("/") + "/health"],
                               capture_output=True, text=True, timeout=10)
            print(f"[healthcheck] /health -> {r.stdout.strip()[:120] or '(空)'}")
    except Exception as e:                                            # noqa: BLE001
        print(f"[healthcheck] 健康端点探测失败: {type(e).__name__}: {e}")

    watch = GpuUtil(a.gpu, interval=a.sample_interval)
    watch.start()
    # ★ 主判据（round 7 第2件）：compute-apps **无窗口**闸门，见模块 docstring。
    #   与 sm 采样器并行跑；查询瞬时完成，不依赖 probe 落在采样窗口里。
    cwatch = ComputeAppsWatch()
    cwatch.start()

    # ★ warmup（不计时）：冷启动只付一次 CUDA graph capture / 内核 JIT 的钱，
    #   不能算进退化判据 —— 否则健康的 ocr-env PaddleX 会被误报成退化。
    if a.warmup_s > 0:
        t0 = time.time()
        if backend == "vllm":
            w_ok, _w_txt, w_err, _w_u = probe_vllm(url, b64, a.warmup_s, oc,
                                                  max_tokens=max_tokens, stage=stage)
        else:
            w_ok, _w_txt, w_err, _w_u = probe_paddlex(url, b64, a.warmup_s, oc, stage=stage)
        w_el = time.time() - t0
        wmax, wn = watch.take()
        print(f"[healthcheck] warmup（不计时）: {w_el:.2f}s ok={w_ok} "
              f"gpu{a.gpu}_sm_max={wmax}% (n={wn}) "
              f"{'err=' + w_err if w_err else ''}")
        if not w_ok and ("timeout" in w_err.lower() or "timed out" in w_err.lower()):
            for ln in gpu_evidence(port):
                print(ln)
            print(f"[healthcheck] ✗ 退化：warmup 请求在 {a.warmup_s:.0f}s 内没返回 —— "
                  f"连冷启动预算都超。典型原因：环境不对（vl-env 而非 ocr-env）/ "
                  f"同卡第二个实例 / CPU 推理。 禁止在此服务上跑产线。")
            return 3

    worst = 0.0
    last_txt = ""
    err = ""
    usage: dict = {}
    # HTTP 超时 = max_s：退化（CPU 推理 / 同卡第二个实例）表现为**超时不回**，
    # 而不是报错。所以「超时」必须归到退化（退出码 3），不能和「服务没起」(2) 混一类。
    probe_timeout = max(1.0, a.max_s)
    for i in range(max(1, a.reps)):
        t0 = time.time()
        if backend == "vllm":
            ok, txt, e, usage = probe_vllm(url, b64, probe_timeout, oc,
                                          max_tokens=max_tokens, stage=stage)
        else:
            ok, txt, e, usage = probe_paddlex(url, b64, probe_timeout, oc, stage=stage)
        el = time.time() - t0
        worst = max(worst, el)
        last_txt, err = txt, e
        u = ""
        if usage.get("completion_tokens") is not None:
            ct = usage["completion_tokens"]
            u = (f" completion_tokens={ct} tok/s={ct / el:.1f} "
                 f"finish={usage.get('finish_reason')}")
        elif usage:
            u = " " + json.dumps(usage, ensure_ascii=False)
        print(f"[healthcheck] probe {i + 1}/{max(1, a.reps)}: {el:.2f}s ok={ok}{u} "
              f"{'err=' + e if e else 'text=' + _preview(txt)}")
        if not ok:
            for ln in gpu_evidence(port):
                print(ln)
            timed = "timeout" in e.lower() or "timed out" in e.lower()
            if timed or el >= probe_timeout - 0.5:
                print(f"[healthcheck] ✗ 退化：{a.probe} 探针在 "
                      f"{probe_timeout:.1f}s 内没返回（健康端点却是活的）。"
                      f" 典型原因：环境不对（vl-env 而非 ocr-env）/ 同卡第二个实例 / CPU 推理。"
                      f" 禁止在此服务上跑产线。")
                return 3
            print("[healthcheck] ✗ 服务无响应/结构异常 —— 不是慢，是根本没答")
            return 2
        if el > a.max_s:
            for ln in gpu_evidence(port):
                print(ln)
            print(f"[healthcheck] ✗ 退化：{a.probe} 探针 {el:.1f}s > {a.max_s}s。"
                  f" 典型原因：环境不对（vl-env 而非 ocr-env）/ 同卡第二个实例 / CPU 推理。"
                  f" 禁止在此服务上跑产线。")
            return 3
    pmax, pn = watch.take()
    watch.stop()
    # sm 只作**辅助证据**：n=0 时（vLLM probe 只有 0.14-0.23 s，1 s 采样器抓不到）
    # 这个数**没有证据力**，旧代码正是因此让 vLLM 的 rc=0 只由耗时阈值给出。
    if pn >= 1:
        print(f"[healthcheck] 辅助证据 GPU{a.gpu} sm 峰值={pmax}% (n={pn})")
    else:
        print(f"[healthcheck] 辅助证据 GPU{a.gpu} sm 峰值=n/a（n=0：采样器没落进请求窗口，"
              f"本次 sm 判据无证据力，不据此放行也不据此判死）")

    # ★ 主判据：目标服务的**进程树**必须出现在 nvidia-smi compute-apps（=持有显存）里。
    #   无窗口判据 → 与 probe 时长无关；空集 = 该服务不在任何 GPU 上 = CPU 推理 = 退化。
    #   进程树必须递归取子孙：vLLM 监听端口的是 APIServer，持显存的是 EngineCore。
    #   查询本身失败 → fail-closed 判退化（不猜「应该没问题」）。
    pid, tree = service_tree(port)
    gpids, grows, gerr, gn = cwatch.take()
    cwatch.stop()
    print(f"[healthcheck] compute-apps 采样 {gn} 次，去重后 {len(grows)} 行，原始返回：")
    for ln in grows:
        print(f"    {ln}")
    if gerr:
        print(f"    (查询错误: {gerr})")
    if not pid:
        for ln in gpu_evidence(port):
            print(ln)
        print(f"[healthcheck] ✗ 无法确定端口 {port} 上的服务进程 —— fail-closed 判退化")
        return 3
    print(f"[healthcheck] 服务 pid={pid} 进程树(含全部子孙)={sorted(tree)}")
    if gerr and not gpids:
        print(f"[healthcheck] ✗ nvidia-smi --query-compute-apps 不可用（{gerr}）"
              f" —— fail-closed 判退化。禁止在此服务上跑产线。")
        return 3
    hit = sorted(tree & gpids)
    if not hit:
        print(f"[healthcheck] ✗ 退化：服务进程树 {sorted(tree)} 不在任何 GPU 的 compute-apps 里"
              f"（当前持显存进程 = {sorted(gpids) or '空'}）—— 推理没在 GPU 上跑。"
              f" 典型原因：环境不对（CPU 版 paddle）/ 强制 CPU 环境变量 / 假服务。"
              f" 禁止在此服务上跑产线。")
        return 3
    print(f"[healthcheck] compute-apps 闸门：命中 {hit}")

    # 辅助：paddlex 的 probe 足够长（76 s），sm 采样器能落进窗口 —— 保留为第二道硬判据。
    # vLLM 的 probe 太短，sm 常常 n=0，不参与判死（否则会把健康服务误杀）。
    if ((backend == "paddlex" or a.probe == "page") and pn >= 1 and worst >= 3.0
            and 0 <= pmax < a.min_sm):
        for ln in gpu_evidence(port):
            print(ln)
        print(f"[healthcheck] ✗ 退化：请求跑了 {worst:.1f}s，但期间 GPU{a.gpu} sm 峰值只有 "
              f"{pmax}%（< {a.min_sm}%）—— 推理没在 GPU 上跑。"
              f" 典型原因：环境不对（vl-env 而非 ocr-env）/ 同卡第二个 paddle 进程 / "
              f"VLM 解码退到 CPU（page 探针专治这一种）。"
              f" 禁止在此服务上跑产线。")
        return 3
    if not last_txt.strip():
        for ln in gpu_evidence(port):
            print(ln)
        print("[healthcheck] ✗ 响应成功但内容为空（服务答了但没读出任何东西）")
        return 4
    for ln in gpu_evidence(port):
        print(ln)
    print(f"[healthcheck] ✓ 正常：最差 {worst:.2f}s ≤ {a.max_s}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
