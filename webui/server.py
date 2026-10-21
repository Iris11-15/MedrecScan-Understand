"""medrec 产线调用网页（round37）—— 控制面。

网页 = 壳，后台 = 命令。本进程**不调 OCR / 不调 Strata / 不解析 PDF**，只做三件事：
  ① 拼命令并起进程（一律 /home/ubuntu/medrec/bin/medrec，§0.2 入口唯一）
  ② 读产物文件（RUN_META.json / summary.json / stages.json / qc_flags.csv / baselines.jsonl）
  ③ 显示文本（PII 一律服务端打码后返回，F4）

起：/home/ubuntu/anaconda3/envs/vl-env/bin/python /home/ubuntu/medrec/webui/server.py
   默认 127.0.0.1:8090；非环回地址直接退出（F5）。
停：Ctrl-C，或 kill <pid>（正在跑的批次不受影响 —— 跑批是 setsid 独立会话）。
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

WEBUI = Path(__file__).resolve().parent
if str(WEBUI) not in sys.path:
    sys.path.insert(0, str(WEBUI))

import artifacts     # noqa: E402
import audit          # noqa: E402
import guards         # noqa: E402
import ingest         # noqa: E402
import jobs           # noqa: E402
import pii            # noqa: E402
from table_formats import UnknownTableFormat, load_registry  # noqa: E402

DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8090
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 2 * 1024 ** 3 + 4096
CELL_SHA_DIR = jobs.HEALTH / "cellsha"
# round41：把 8091 只读看板（跑批进度 / 检验流程 / 回放）**同源代理**进本端口。
# 只代理这张白名单，绝不拼用户给的 URL；上游地址必须是环回（F5）。
DEFAULT_VIZ_URL = "http://127.0.0.1:8091"
VIZ_PROXY = {"/viz/live": "/live", "/viz/qc": "/qc", "/viz/replay": "/replay",
             "/api/live": "/api/live", "/api/qc": "/api/qc",
             "/api/viz/batches": "/api/batches"}
VIZ_JSON = {"/api/live", "/api/qc", "/api/viz/batches"}
VIZ_START = ("setsid nohup env -u http_proxy -u https_proxy -u all_proxy "
             "-u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY "
             "/home/ubuntu/anaconda3/envs/vl-env/bin/python "
             "/home/ubuntu/medrec/workspace/viz/serve.py --port 8091 "
             # 批次根必须显式带上：写根已迁到 raid5，看板只认 config 里的旧根
             # 就会对新批次一律报「批次目录不存在」（round42l）。
             "--run-root \"%s:%s\" " % (jobs.RUN_ROOT, jobs.LEGACY_RUN_ROOT) +
             " "
             "> /home/ubuntu/medrec/workspace/viz/out/serve.log 2>&1 < /dev/null &")


def parse_args(argv: list[str]) -> tuple[str, int, bool, str]:
    """新增两个旗标：--disable-plain（禁掉一切明文出口）、--viz-url（看板地址）。"""
    host, port = DEFAULT_HOST, DEFAULT_PORT
    plain, viz = True, DEFAULT_VIZ_URL
    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if a in ("--host", "-H") and nxt is not None:
            host, i = nxt, i + 2
        elif a in ("--port", "-p") and nxt is not None:
            port, i = int(nxt), i + 2
        elif a.startswith("--host="):
            host, i = a.split("=", 1)[1], i + 1
        elif a.startswith("--port="):
            port, i = int(a.split("=", 1)[1]), i + 1
        elif a == "--disable-plain":
            plain, i = False, i + 1
        elif a == "--viz-url" and nxt is not None:
            viz, i = nxt, i + 2
        elif a.startswith("--viz-url="):
            viz, i = a.split("=", 1)[1], i + 1
        else:
            i += 1
    if host not in LOOPBACK:
        print(f"ERROR 拒绝绑定非环回地址 host={host!r}（F5：本页面只允许本机访问）。"
              f"\n      允许的值：{sorted(LOOPBACK)}。默认 {DEFAULT_HOST}:{DEFAULT_PORT}。"
              "\n      本页面会把批次日志与宽表内容显示出来，绑到 0.0.0.0 = 全网络可读 ⇒ 直接退出。",
              file=sys.stderr)
        raise SystemExit(2)
    if urlparse(viz).hostname not in LOOPBACK:
        print(f"ERROR 看板地址 --viz-url={viz!r} 不是环回地址 ⇒ 直接退出（F5）。",
              file=sys.stderr)
        raise SystemExit(2)
    return host, port, plain, viz.rstrip("/")


class Handler(BaseHTTPRequestHandler):
    server_version = "medrec-webui/1"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ 基础
    def log_message(self, fmt, *args):     # 访问日志不进 stdout 带 PII 的东西
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.command} "
                         f"{pii.scrub_text(urlparse(self.path).path)}\n")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_out(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def fail(self, code: int, msg: str, extra: dict | None = None) -> None:
        out = {"ok": False, "error": pii.scrub_text(msg)}
        out.update(extra or {})
        self.json_out(out, code)

    def _body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError(f"请求体 {n} 字节超过上限")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # ------------------------------------------------------------------ GET
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/favicon.ico":
            return self._send(204, b"", "text/plain; charset=utf-8")
        if path == "/api/status":
            return self.json_out(self.status_all())
        if path == "/api/table_formats":
            return self.json_out({"ok": True, "formats": load_registry()})
        if path == "/api/newbatch":
            return self.json_out({"ok": True, "batch": ingest.new_batch_name()})
        if path == "/api/browse":
            b = ingest.browse(q.get("path", ""))
            return self.json_out(b, 200 if b["ok"] else 400)
        if path == "/api/inspectlocal":
            r = ingest.inspect_local(q.get("path", ""))
            audit.write("inspect_local", pii.scrub_text(q.get("path", "")),
                        {"kind": r.get("kind"), "entries": r.get("entries"),
                         "image_dirs": r.get("image_dirs"), "errors": r.get("errors", [])[:2]})
            return self.json_out(r, 200 if r.get("ok") else 400)
        if path == "/api/stagestatus":
            sid = q.get("stage_id") or q.get("upload_id") or ""
            out = ingest.read_stage_status(sid)
            d = ingest.find_stage_dir(sid)
            if d:
                out["chain"] = ingest.read_chain(d)
            return self.json_out(out)
        if path == "/api/batches":
            return self.json_out({"ok": True, "batches": jobs.batches(int(q.get("limit", 40)))})
        if path == "/api/job":
            b = self._batch(q)
            if not b:
                return self.fail(400, "缺 ?batch=")
            prog = jobs.progress(b)
            prog["log_tail"] = jobs.log_tail(b, int(q.get("lines", 40)))
            return self.json_out({"ok": True, **prog})
        if path in ("/api/check", "/api/stages"):
            return self.run_readonly("check" if path.endswith("check") else "stages", q)
        if path == "/api/preview":
            return self.preview(q)
        if path == "/api/raw":
            return self.view_raw(q)
        if path == "/api/wide.csv":
            return self.download_wide(q)
        if path == "/api/wide.xlsx":
            return self.download_wide_xlsx(q)
        if path == "/api/audit":
            return self.json_out({"ok": True, "audit": audit.tail(int(q.get("n", 50)))})
        if path == "/api/viz/status":
            return self.json_out({"ok": True, **self.viz_status()})
        if path in VIZ_PROXY:
            return self.viz_get(path, q)
        return self.fail(404, f"没有这个接口：{path}")

    def _static(self, rel: str) -> None:
        p = (WEBUI / "static" / rel).resolve()
        if not str(p).startswith(str((WEBUI / "static").resolve())) or not p.is_file():
            return self.fail(404, f"没有静态文件：{rel}")
        ctype = "text/html; charset=utf-8" if p.suffix == ".html" else "text/plain; charset=utf-8"
        self._send(200, p.read_bytes(), ctype)

    def _batch(self, q: dict) -> str:
        b = (q.get("batch") or "").strip()
        try:
            return ingest._safe_id(b)
        except ValueError:
            return ""

    def status_all(self) -> dict:
        return {"ok": True, "services": guards.services(), "load": jobs.system_load(),
                "doctor": jobs.doctor_info(), "drafts": jobs.drafts(),
                "lock": jobs.read_lock(), "bound": f"{self.server.host}:{self.server.port}",
                "entry": jobs.MEDREC, "table_formats": list(load_registry()),
                "plain_allowed": bool(getattr(self.server, "plain_allowed", True)),
                "viz": self.viz_status(),
                "run_root": str(jobs.RUN_ROOT),
                "legacy_run_roots": [str(p) for p in jobs.RUN_ROOTS[1:]]}

    # ---------------------------------------------------------------- 看板代理
    def viz_status(self) -> dict:
        """8091 只读看板在不在。只读 /health，不启停任何服务（F3）。"""
        url = getattr(self.server, "viz_url", DEFAULT_VIZ_URL) + "/health"
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                d = json.loads(r.read().decode("utf-8"))
            return {"ok": bool(d.get("ok")), "url": getattr(self.server, "viz_url", DEFAULT_VIZ_URL),
                    "run_root": d.get("run_root")}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"ok": False, "url": getattr(self.server, "viz_url", DEFAULT_VIZ_URL),
                    "error": f"{type(exc).__name__}", "start_command": VIZ_START}

    def viz_get(self, path: str, q: dict) -> None:
        """同源代理到看板。路径走白名单；batch 过 _safe_id；上游 URL 不接受页面输入。"""
        sub = VIZ_PROXY[path]
        url = getattr(self.server, "viz_url", DEFAULT_VIZ_URL) + sub
        if "batch" in q or sub in ("/live", "/qc", "/replay"):
            b = self._batch(q)
            if not b:
                return self.fail(400, "缺 ?batch=（或批次名不合法）")
            url += f"?batch={quote(b)}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                body = r.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            audit.write("viz_unreachable", q.get("batch", ""), f"{sub} {type(exc).__name__}")
            return self.fail(502, f"进度看板没在跑（{getattr(self.server, 'viz_url', DEFAULT_VIZ_URL)}）："
                                  f"{type(exc).__name__}: {exc}", {"start_command": VIZ_START})
        except Exception as exc:                       # noqa: BLE001 —— 上游任何异常都不该掀翻网页
            return self.fail(502, f"看板请求失败：{type(exc).__name__}: {exc}")
        ctype = ("application/json; charset=utf-8" if path in VIZ_JSON
                 else "text/html; charset=utf-8")
        self._send(200, body, ctype)

    def run_readonly(self, sub: str, q: dict) -> None:
        b = self._batch(q)
        if not b:
            return self.fail(400, "缺 ?batch=")
        argv = [jobs.MEDREC, sub, b]
        if sub == "check":
            # bin/medrec check 默认把逐格 sha 写进 <批次>/_medrec/cell_sha.json
            # ⇒ 会碰既有批次目录（本轮禁写）。重定向到网页自己的 _health/cellsha/。
            CELL_SHA_DIR.mkdir(parents=True, exist_ok=True)
            argv += ["--cell-sha-out", str(CELL_SHA_DIR / f"{b}.json")]
        try:
            res = subprocess.run(argv, capture_output=True, text=True, timeout=300,
                                 cwd=str(Path(jobs.MEDREC).parent.parent), env=jobs._clean_env())
        except (OSError, subprocess.SubprocessError) as exc:
            return self.fail(500, f"{type(exc).__name__}: {exc}", {"command": _shown(argv)})
        text = pii.scrub_text((res.stdout or "") + (res.stderr or ""))
        fail = warn = None
        for ln in text.splitlines():
            if ln.startswith("=== check:"):
                fail, warn = jobs._kv(ln, ["FAIL", "WARN"])
        audit.write(f"view_{sub}", b, f"fail={fail} warn={warn} rc={res.returncode}")
        self.json_out({"ok": res.returncode in (0, 1), "command": _shown(argv),
                       "rc": res.returncode, "text": text, "fail": fail, "warn": warn,
                       "lines": text.splitlines()[-40:]})

    # ------------------------------------------------------------------ POST
    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/upload":
            return self.upload()
        if path == "/api/stagelocal":
            return self.stage_local_start()
        if path == "/api/chainack":
            return self.chain_ack()
        if path == "/api/plan":
            return self.plan_start(plan_only=True)
        if path == "/api/start":
            return self.plan_start(plan_only=False)
        return self.fail(404, f"没有这个接口：{path}")

    def stage_local_start(self) -> None:
        """本机 .zip / 散页目录 ⇒ 后台起归一 worker（同盘暂存，几小时级不能同步等）。"""
        try:
            payload = self._body_json()
        except (ValueError, json.JSONDecodeError) as exc:
            return self.fail(400, f"请求体不是 JSON：{exc}")
        raw = str(payload.get("src") or "").strip()
        # ★ round42k「只填一个路径就自动提取」：勾了自动提取 ⇒ 把跑批请求挂在暂存目录上，
        #   由本进程的链式轮询线程在归一完成后发起。挂文件不挂内存：归一是小时级，
        #   浏览器随时会关 / 刷新 / 换浏览器，链不能由前端代跑。
        chain_req = None
        if payload.get("auto_run"):
            b = self._batch(payload) or ingest.new_batch_name()
            fmt_id = str(payload.get("format") or "wide")
            try:
                load_registry(fmt_id)
            except UnknownTableFormat as exc:
                audit.write("stagelocal_reject", sid, f"出表类型不合法：{exc}")
                return self.fail(400, f"出表类型不合法：{exc}")
            chain_req = {"batch": b,
                         "steps": [s for s in (payload.get("steps") or "").split(",") if s],
                         "format": fmt_id, "ack_alerts": bool(payload.get("ack_alerts"))}
        # ★ round42h 真 bug：_safe_id("") 直接抛 ValueError ⇒ 页面点「暂存归一」
        #   （前端只发 {src}，不带 stage_id）永远被 400 拒成「非法名字：」，
        #   而且这条 400 在 audit 之前 ⇒ 审计里查不到，看起来就是「点了没反应」。
        #   没给 stage_id 时应该自己生成一个，而不是报错。
        raw_sid = str(payload.get("stage_id") or "").strip()
        try:
            sid = ingest._safe_id(raw_sid) if raw_sid else ingest.new_batch_name("stage")
        except ValueError as exc:
            audit.write("stagelocal_reject", "", pii.scrub_text(f"stage_id 不合法：{exc}"))
            return self.fail(400, f"stage_id 不合法：{exc}")
        # 同一个源根已经归一完成 ⇒ 直接复用那棵树。合并 + 守卫 OCR 是小时级
        # （/raid5/history_13_15 实测 2449 目录 ≈ 6.5 h），再点一次按钮就重跑一遍
        # 是不可接受的浪费；stage_id 存在浏览器 localStorage 里，刷新/换浏览器就会丢。
        # 想强制重跑（改了守卫参数之类）⇒ 请求体带 "force": true 绕过复用
        done = {} if payload.get("force") else ingest.find_done_stage(Path(raw))
        if done:
            audit.write("stagelocal_reuse", done["stage_id"],
                        {"src": pii.scrub_text(raw), "tree": pii.scrub_text(done["tree"])})
            return self.json_out({"ok": True, "reused": True, "pid": 0,
                                  "stage_id": done["stage_id"], "tree": done["tree"],
                                  "stage_dir": str(ingest.find_stage_dir(done["stage_id"])),
                                  "merged": done["merged"], "alerts": done["alerts"],
                                  "note": "同一源根已有归一完成的暂存树 ⇒ 直接复用，没有重跑合并",
                                  **self._attach_chain(done["stage_id"], chain_req)})
        # ★ round42i：同一个源根**正在归一中** ⇒ 把那个 worker 的 stage_id 还给前端接上它。
        #   前端 bug（api() 的 {code, body} 被当 body 用）让用户连点三次，起了三个 worker
        #   打同一个语料：3× OCR、3× 磁盘，而且每个都要跑完 2449 目录 ⇒ 一点没快。
        running = {} if payload.get("force") else ingest.find_running_stage(Path(raw))
        if running:
            audit.write("stagelocal_running", running["stage_id"],
                        {"src": pii.scrub_text(raw), "tree": pii.scrub_text(running["tree"]),
                         "done_dirs": running["done_dirs"],
                         "total_dirs": running["total_dirs"]})
            return self.json_out({"ok": True, "already_running": True, "pid": running["pid"],
                                  "stage_id": running["stage_id"], "tree": running["tree"],
                                  "stage_dir": running["stage_dir"],
                                  "done_dirs": running["done_dirs"],
                                  "total_dirs": running["total_dirs"],
                                  "merged": running["merged"],
                                  "note": "同一源根已有 worker 在归一中 ⇒ 接上它，没有再起第二个",
                                  **self._attach_chain(running["stage_id"], chain_req)})
        try:
            info = ingest.spawn_stage_local(Path(raw), sid)
        except (ValueError, RuntimeError, OSError) as exc:
            audit.write("stagelocal_reject", sid, pii.scrub_text(str(exc)))
            return self.fail(400, pii.scrub_text(str(exc)))
        audit.write("stagelocal_start", sid, {"src": pii.scrub_text(raw), "pid": info["pid"],
                                             "stage_dir": info["stage_dir"]})
        self.json_out({"ok": True, **info, **self._attach_chain(sid, chain_req)})

    def _attach_chain(self, stage_id: str, chain_req: dict | None) -> dict:
        """把「归一完自动提取」的请求写进暂存目录（round42k）。"""
        if not chain_req:
            return {}
        d = ingest.find_stage_dir(stage_id)
        if not d:
            return {}
        ch = dict(chain_req, status="pending", note="等归一完成后自动提取")
        ingest.write_chain(d, ch)
        audit.write("chain_request", chain_req["batch"],
                    {"stage_id": stage_id, "steps": ",".join(chain_req["steps"]) or "all",
                     "ack_alerts": chain_req["ack_alerts"]})
        return {"chain": ch}

    def chain_ack(self) -> None:
        """守卫报警清单人看过 ⇒ 让链式继续（不自动跳过确认，红线⑦）。"""
        try:
            payload = self._body_json()
        except (ValueError, json.JSONDecodeError) as exc:
            return self.fail(400, f"请求体不是 JSON：{exc}")
        try:
            sid = ingest._safe_id(str(payload.get("stage_id") or ""))
        except ValueError as exc:
            return self.fail(400, f"stage_id 不合法：{exc}")
        d = ingest.find_stage_dir(sid)
        if not d:
            return self.fail(404, f"没有暂存目录：{sid}（先点「暂存归一」）")
        ch = ingest.read_chain(d)
        if not ch:
            return self.fail(400, f"{sid} 没有链式提取请求（起归一时没勾「归一完自动提取」）")
        st = ingest.read_stage_status(sid)
        if st.get("state") == "running":
            return self.fail(409, "归一还在跑（%s/%s 目录）⇒ 等它完成"
                             % (st.get("done_dirs", 0), st.get("total_dirs", 0)))
        ingest.write_chain(d, dict(ch, ack_alerts=True, status="pending",
                                   note="已人工确认守卫报警 ⇒ 继续自动提取"))
        audit.write("chain_ack", str(ch.get("batch") or sid),
                    {"stage_id": sid, "alerts": st.get("alerts")})
        self.json_out({"ok": True, "chain": ingest.read_chain(d)})

    def upload(self) -> None:
        clen = int(self.headers.get("Content-Length") or 0)
        if clen <= 0:
            return self.fail(400, "空请求体")
        if clen > MAX_BODY:
            return self.fail(413, f"单次上传上限 2 GiB（本次 Content-Length={clen}）")
        try:
            uid = ingest._safe_id(self.headers.get("X-Upload-Id") or "anon")
            res = ingest.receive_upload(ingest.SeekBody(self.rfile, clen), clen,
                                        self.headers.get("Content-Type", ""), uid)
        except ValueError as exc:
            return self.fail(400, str(exc))
        try:
            post = ingest.post_process_uploads(res["upload_id"])
        except (OSError, ValueError) as exc:
            post = {"ok": False, "zips": [], "errors": [f"{type(exc).__name__}: {exc}"]}
        res["zips"] = post["zips"]
        res["errors"] += post["errors"]
        res["ok"] = res["ok"] and post["ok"]
        audit.write("upload", res["upload_id"],
                    {"files": len(res["files"]), "bytes": res["total_bytes"],
                     "zips": len(post["zips"]), "errors": res["errors"][:2]})
        self.json_out(res, 200 if res["ok"] else 400)

    def plan_start(self, plan_only: bool) -> None:
        try:
            payload = self._body_json()
        except (ValueError, json.JSONDecodeError) as exc:
            return self.fail(400, f"请求体不是 JSON：{exc}")
        batch = self._batch(payload)
        if not batch:
            return self.fail(400, "批次名不合法（只允许字母数字中文 . - _）")
        fmt_id = payload.get("format") or "wide"
        try:
            fmt = load_registry(fmt_id)
        except UnknownTableFormat as exc:
            return self.fail(400, str(exc))
        src, staged = self._source(payload, batch, stage=not plan_only)
        if src is None:
            return self.fail(400, staged)
        steps = [s for s in (payload.get("steps") or "").split(",") if s] or None
        cmd_src = None
        if payload.get("upload_id"):
            cmd_src = ingest.final_dir(batch)           # 起跑时暂存树会搬到这里（§6.3）
        stage_st = None
        if payload.get("stage_id"):
            try:
                stage_st = ingest.read_stage_status(ingest._safe_id(payload["stage_id"]))
            except ValueError as exc:
                return self.fail(400, f"stage_id 不合法：{exc}")
        res = self.gate(batch, src, steps, fmt, cmd_src, stage_st=stage_st,
                        ack_alerts=bool(payload.get("ack_alerts")))
        if not res["ok"]:
            audit.write("plan_reject", batch, res["errors"][:3])
            return self.json_out(res, 400)
        if plan_only:
            return self.json_out(res)
        return self.start(batch, src, steps, res)

    def _source(self, payload: dict, batch: str, stage: bool) -> tuple[Path | None, str]:
        """三种输入都归一成「一个源根目录」；本机路径只读引用（§6.4）。

        上传 ⇒ 先搬到 _ingest/<batch>/（§6.3），这样页面显示的命令里的源根
        和实际跑批的源根逐字相同（F7）。

        本机 .zip / 散页目录 ⇒ 用暂存归一好的 <stage>/<sid>/tree（round42b）。
        **不搬动**：raid5 上几十 GB 的树搬到 / 会撑爆，而且搬完页面显示的源根
        就和实际源根不一致（F7 破功）—— 所以 tree 本身就是源根。
        """
        sid = payload.get("stage_id")
        if sid:
            try:
                tid = ingest._safe_id(sid)
            except ValueError as exc:
                return None, f"{type(exc).__name__}: {exc}"
            st = ingest.read_stage_status(tid)
            if st.get("state") == "running":
                return None, ("暂存归一还在跑（%s/%s 目录，阶段：%s）⇒ 等它完成再提交"
                              % (st.get("done_dirs", 0), st.get("total_dirs", 0),
                                 st.get("phase", "")))
            d = ingest.find_stage_dir(tid)
            if not d:
                return None, f"没有找到暂存目录：{sid}（先在本机路径里点「暂存归一」）"
            tree = ingest.stage_tree_dir(d)
            if not tree.is_dir():
                return None, f"暂存树不存在：{tree}"
            # ★ round42s 真 bug：stage_id 存在浏览器 localStorage 里，前端换了源根输入框
            #   也照样带着旧 stage_id 提交 ⇒ 选了 history_19 实际跑的是上一批的 history_13_15
            #   （实测批次 web-20261017-183205，2434 行，白跑两小时）。
            #   后端必须自己核对：暂存状态里记的源根 != 现在填的源根 ⇒ 拒，不许沿用旧树。
            #   状态里的 src 是 pii.scrub_text 过的，所以比对前对 want 做同样处理。
            want = str(payload.get("src") or "").strip()
            got = str(st.get("src") or "").strip()
            if want and got and pii.scrub_text(want).rstrip("/") != got.rstrip("/"):
                return None, ("★ 暂存树 " + str(sid) + " 不是这个源根归一出来的："
                              "暂存时记的源根是 " + got + "，现在填的是 " +
                              pii.scrub_text(want) +
                              " ⇒ 换语料必须重新点「暂存归一」，不能沿用旧树"
                              "（沿用旧树 = 静默把上一批的语料又跑一遍）")
            return tree, ""
        uid = payload.get("upload_id")
        if uid:
            try:
                tid = ingest._safe_id(uid)
                if not stage and ingest.tmp_dir(tid).is_dir():
                    return ingest.tmp_dir(tid), ""      # 只看归一结果，先不搬
                return ingest.stage_to_final(tid, batch), ""
            except (ValueError, OSError) as exc:
                return None, f"{type(exc).__name__}: {exc}"
        raw = (payload.get("src") or "").strip()
        if not raw:
            return None, "既没有 upload_id 也没有 src"
        p = Path(raw)
        if not p.is_absolute():
            return None, f"src 必须是绝对路径：{raw!r}"
        return p, ""

    @staticmethod
    def gate(batch: str, src: Path, steps, fmt, cmd_src: Path | None = None,
             stage_st: dict | None = None, ack_alerts: bool = False) -> dict:
        """G1–G6 + F2 全跑一遍；任何一条不过 ⇒ 拒绝。

        ★ round42k 改成 staticmethod：链式轮询线程（没有 HTTP 请求上下文）要跑**同一份**
        闸门，不能复制第二份 —— 两份闸门迟早漂移，漂移的那一份就是没被验过的那一份。
        """
        errs, warns = [], []
        plan = ingest.plan(src)
        errs += plan["errors"]
        warns += plan["warnings"]
        if stage_st:
            n = int(stage_st.get("alerts") or 0)
            if n:
                if not ack_alerts:
                    errs.append(
                        f"★ 多患者守卫拦下 {n} 个散页目录：这些目录**没有合并、不在源根里、"
                        f"不会进本批**（守卫的强制形式）。待人工确认清单：{stage_st.get('alert_path')}。"
                        "看过清单后勾选「我已知晓被拦目录」再提交。")
                else:
                    warns.append(
                        f"已确认：{n} 个散页目录被守卫拦下，未合并、不进本批；"
                        f"清单 {stage_st.get('alert_path')}")
        bdir = jobs.batch_dir(batch)
        if bdir.exists():
            errs.append(f"F2 批次不可覆盖：{bdir} 已存在。"
                        "换个批次名（本轮不许 --force、不许删/改名/移动任何批次）。")
        ws = guards.writable_space(plan["total_bytes"])
        errs += ws["errors"]
        svcs = guards.services()
        for name, s in svcs.items():
            if not s["ok"]:
                errs.append(f"G3 服务不活：{name} /v1/models ⇒ {s['error'] or s['status']}")
        for f in jobs.foreign_runs():
            if f.split("batch=")[1] != batch:
                warns.append("F1 单飞锁只管网页发起的跑批；检测到网页之外在跑的批："
                             f"{f} —— 两批抢 GPU/Strata 会让 s/例 完全不可比（round36 实测）。")
        if steps:
            g6 = guards.steps_upstream(bdir, steps)
            errs += g6["errors"]
        fmt_ok, fmt_why = fmt.check_prereq(bdir)
        if not fmt_ok:
            errs.append(f"出表类型 {fmt.id}：{fmt_why}")
        log = bdir / "_webui" / "run.log"
        _argv, shown = jobs.build_command(cmd_src or src, batch, steps, log)
        return {"ok": not errs, "errors": errs, "warnings": warns, "plan": plan,
                "services": svcs, "space": ws, "command": shown, "batch": batch,
                "format": fmt.describe(), "src": str(cmd_src or src),
                "staged_from": str(src) if cmd_src and cmd_src != src else "",
                "stage": ({k: stage_st.get(k) for k in
                          ("stage_id", "stage_dir", "tree", "state", "merged", "blocked",
                           "alerts", "pages", "alert_path")} if stage_st else None),
                "lock": jobs.read_lock()}

    def start(self, batch: str, src: Path, steps, gate_res: dict) -> None:
        try:
            info = spawn_run(batch, src, steps, gate_res["command"])
        except Locked as exc:
            audit.write("start_reject_lock", batch, str(exc))
            return self.fail(409, str(exc), {"lock": jobs.read_lock()})
        except (OSError, RuntimeError, FileExistsError) as exc:
            audit.write("start_fail", batch, str(exc))
            return self.fail(500, str(exc))
        self.json_out({"ok": True, **info, "command": gate_res["command"],
                       "plan": gate_res["plan"]})

    # ------------------------------------------------------------------ 产物读取
    def _plain(self, q: dict) -> bool:
        return str(q.get("plain") or "").lower() in ("1", "true", "yes", "on")

    def _plain_gate(self, plain: bool, b: str, what: str) -> bool:
        """明文出口的唯一闸门：启动参数 --disable-plain ⇒ 一律 403，并记审计。"""
        if not plain:
            return True
        if getattr(self.server, "plain_allowed", True):
            return True
        audit.write("plain_denied", b, what)
        return False

    def preview(self, q: dict) -> None:
        b = self._batch(q)
        if not b:
            return self.fail(400, "缺 ?batch=")
        try:
            rows = int(q.get("rows", "10"))
        except ValueError:
            return self.fail(400, "rows 必须是整数")
        plain = self._plain(q)
        if not self._plain_gate(plain, b, "preview"):
            return self.fail(403, "明文（不打码）已被启动参数 --disable-plain 关闭")
        try:
            self.json_out({"ok": True, **artifacts.preview(b, rows, plain)})
        except (OSError, ValueError, FileNotFoundError, LookupError) as exc:
            self.fail(404, str(exc))

    def view_raw(self, q: dict) -> None:
        """F4「本地查看」：只回给浏览器显示，不落任何文件/日志；审计记 时间+列名+行号（不记值）。"""
        b = self._batch(q)
        try:
            row = int(q.get("row", "-1"))
        except ValueError:
            return self.fail(400, "row 必须是整数")
        col = q.get("col") or ""
        if not b or row < 0 or not col:
            return self.fail(400, "需要 batch / row(≥0) / col")
        try:
            value = artifacts.view_raw(b, row, col)
        except (OSError, ValueError, FileNotFoundError, LookupError) as exc:
            return self.fail(404, str(exc))
        self.json_out({"ok": True, "batch": b, "row": row, "col": col, "value": value,
                       "note": "仅本次浏览器显示；服务端未落盘，审计只记 时间/列名/行号"})

    def download_wide_xlsx(self, q: dict) -> None:
        """xlsx 出口（round42r）。打码规则与 csv 完全一致，见 artifacts.xlsx_bytes。

        注意：磁盘上的 06_table/wide.xlsx 是原文，不能直接发 —— 那会绕过 F4 打码，
        所以走 xlsx_bytes 从 wide.csv 现做一份（plain=True 才带 PII，且过 plain 闸门 + 记审计）。
        """
        b = self._batch(q)
        if not b:
            return self.fail(400, "缺 ?batch=")
        plain = self._plain(q)
        if not self._plain_gate(plain, b, "download_xlsx"):
            return self.fail(403, "明文（不打码）已被启动参数 --disable-plain 关闭")
        try:
            body, nrows, ncols = artifacts.xlsx_bytes(b, plain)
        except (OSError, ValueError, FileNotFoundError, LookupError, RuntimeError) as exc:
            return self.fail(404, str(exc))
        tag = "PLAIN" if plain else "masked"
        name = f"wide-{b}-{tag}.xlsx"
        disp = ("attachment; filename=\"" + _ascii_name(name) +
                "\"; filename*=UTF-8\u0027\u0027" + quote(name, safe=""))
        self._send(200, body,
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   {"Content-Disposition": disp})

    def download_wide(self, q: dict) -> None:
        b = self._batch(q)
        if not b:
            return self.fail(400, "缺 ?batch=")
        plain = self._plain(q)
        if not self._plain_gate(plain, b, "download_csv"):
            return self.fail(403, "明文（不打码）已被启动参数 --disable-plain 关闭")
        try:
            body, nrows, ncols = artifacts.csv_bytes(b, plain)
        except (OSError, ValueError, FileNotFoundError, LookupError) as exc:
            return self.fail(404, str(exc))
        # 文件名必须 ASCII 安全：send_header() 用 latin-1 编码，批次名允许中文 ⇒
        # 老代码 wide-<中文批次>-masked.csv 会 UnicodeEncodeError 直接掐断连接（round41 实测 rc=52）。
        # 稳妥写法：ASCII 兜底名 + RFC 5987 filename*（percent 编码，纯 ASCII）。
        name = f"wide-{b}-{'PLAIN' if plain else 'masked'}.csv"
        self._send(200, body, "text/csv; charset=utf-8",
                   {"Content-Disposition": 'attachment; filename="' + _ascii_name(name) +
                    '"; filename*=UTF-8\'\'' + quote(name, safe="")})


def _shown(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def _ascii_name(name: str) -> str:
    """Content-Disposition 的 ASCII 兜底名：非 ASCII 与 " \\ ; , 一律换成 _。"""
    return "".join(c if ord(c) < 128 and c not in '"\\;,' else "_" for c in name)


def _fingerprint(src: Path) -> str:
    """输入指纹：目录名 + 例数 + 总字节（不含任何 PII 值）。"""
    shp = guards.shape(src)
    return f"pdfs={shp['pdfs']};cases={shp['cases']};bytes={shp['total_bytes']}"


class Locked(RuntimeError):
    """F1 单飞锁被占。"""


def spawn_run(batch: str, src: Path, steps, shown: str) -> dict:
    """起跑批进程。HTTP 与链式轮询线程共用这一条，别复制两份。"""
    lock = jobs.read_lock()
    if lock.get("alive"):
        raise Locked(lock.get("error") or
                     f"正在跑 {lock.get('batch')}，已运行 {lock.get('running_min')} 分钟"
                     "（F1 单飞锁）")
    fp = _fingerprint(src)
    try:
        info = jobs.spawn(src, batch, steps, fp)
    except RuntimeError as exc:
        # jobs.spawn 里 acquire_lock 失败也是 RuntimeError ⇒ 归成 Locked，
        # 链式轮询才知道这是「稍后重试」而不是「永久被拒」。
        raise Locked(str(exc)) if "锁" in str(exc) or "正在跑" in str(exc) else exc
    audit.write("start", batch, {"command": shown, "pid": info["pid"],
                                 "fingerprint": fp, "steps": steps or "all"})
    return info


# ------------------------------------------------------------------ 链式自动提取
CHAIN_TICK = 10.0


def chain_tick() -> None:
    """归一 worker 结束后，按暂存目录里的 chain.json 自动发起六阶段跑批（round42k）。

    ★ 守卫拦下过目录而人没确认 ⇒ **不自动跑**，停在 awaiting_ack 等网页上点确认。
      红线⑦：不许为了「跑通」把守卫的人工确认这一步省掉。
    ★ 闸门用 Handler.gate（同一份），起进程用 spawn_run（同一份）。
    """
    for d in ingest.iter_stage_dirs():
        ch = ingest.read_chain(d)
        if not ch or ch.get("status") not in ("pending", "waiting_lock"):
            continue
        sid = d.name
        st = ingest.read_stage_status(sid)
        state = str(st.get("state") or "")
        if state == "running":
            continue
        if state not in ("done", "done_with_alerts"):
            ingest.write_chain(d, dict(ch, status="aborted",
                                       note=f"归一没完成（state={state or 'none'}）⇒ 不自动提取"))
            audit.write("chain_abort", sid, f"state={state}")
            continue
        alerts = int(st.get("alerts") or 0)
        if alerts > 0 and not ch.get("ack_alerts"):
            ingest.write_chain(d, dict(ch, status="awaiting_ack",
                                       note=f"守卫拦下 {alerts} 个散页目录，未人工确认 ⇒ 暂停在归一完成处"))
            audit.write("chain_await_ack", sid, f"alerts={alerts}")
            continue
        batch = str(ch.get("batch") or "")
        try:
            fmt = load_registry(str(ch.get("format") or "wide"))
        except UnknownTableFormat as exc:
            ingest.write_chain(d, dict(ch, status="rejected", note=str(exc)))
            audit.write("chain_reject", batch or sid, str(exc))
            continue
        tree = Path(str(st.get("tree") or ""))
        steps = [s for s in (ch.get("steps") or []) if s] or None
        res = Handler.gate(batch, tree, steps, fmt, stage_st=st, ack_alerts=True)
        if not res["ok"]:
            ingest.write_chain(d, dict(ch, status="rejected",
                                       note="；".join(res["errors"])[:600]))
            audit.write("chain_reject", batch, res["errors"][:3])
            continue
        ingest.write_chain(d, dict(ch, status="starting", note="归一完成 ⇒ 正在发起跑批"))
        try:
            info = spawn_run(batch, tree, steps, res["command"])
        except Locked as exc:
            ingest.write_chain(d, dict(ch, status="waiting_lock",
                                       note=f"单飞锁被占，稍后自动重试：{exc}"))
            audit.write("chain_wait_lock", batch, str(exc))
            continue
        except (OSError, RuntimeError, FileExistsError) as exc:
            ingest.write_chain(d, dict(ch, status="rejected",
                                       note=f"发起跑批失败：{type(exc).__name__}: {exc}"))
            audit.write("chain_start_fail", batch, str(exc))
            continue
        ingest.write_chain(d, dict(ch, status="started", run_pid=info["pid"],
                                   run_log=info["log"], note="已自动发起跑批"))
        audit.write("chain_start", batch, {"stage_id": sid, "pid": info["pid"],
                                           "tree": pii.scrub_text(str(tree))})


def chain_loop() -> None:
    while True:
        try:
            chain_tick()
        except Exception as exc:                    # noqa: BLE001 —— 轮询线程不许掀翻网页
            audit.write("chain_error", "", f"{type(exc).__name__}: {exc}")
        time.sleep(CHAIN_TICK)


def main() -> int:
    host, port, plain, viz_url = parse_args(sys.argv[1:])
    for k in jobs.PROXY_KEYS:
        os.environ.pop(k, None)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.host, httpd.port = host, port
    httpd.plain_allowed, httpd.viz_url = plain, viz_url
    threading.Thread(target=chain_loop, daemon=True, name="chain").start()
    print(f"medrec webui 监听 http://{host}:{port}（仅本机可访问，F5）"
          f"\n  入口 = {jobs.MEDREC}\n  停：Ctrl-C 或 kill {os.getpid()}"
          f"\n  产物写根 = {jobs.RUN_ROOT}（跑批日志 <写根>/<批次>/_webui/run.log）"
          f"\n  旧批次只读根 = {' | '.join(str(p) for p in jobs.RUN_ROOTS[1:])}"
          f"\n  明文（不打码）出口：{'开（每次访问都记审计）' if plain else '关（--disable-plain）'}"
          f"\n  进度看板代理 = {viz_url}（/viz/live · /viz/qc · /viz/replay）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl-C，关闭监听（正在跑的批次不受影响）。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
