#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""strata_probe.py —— Strata(:8080) 接入前的能力探测 + 并发实测。

两种模式：
  --selftest   不联网。把假响应灌进**产线自己的解析函数**，验证 reasoning_content
               会不会出事。沙箱里唯一能真跑的部分。
  --probe      联网（127.0.0.1）。B1~B6 能力探测。
  --replay     把 strata_rehearsal.py 录下来的真实请求体按并发 1/2/4 回放，
               报吞吐 / 失败率 / 空 content 数。

不改产线代码，不改任何禁区文件。
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

BASE = os.environ.get("STRATA_BASE", "http://127.0.0.1:8080/v1")
MODEL = os.environ.get("STRATA_MODEL", "qwen3.8-flash-next-iq3_s")
KEY = os.environ.get("STRATA_KEY", "sk-local")


def _no_proxy():
    for k in ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(k, None)


def http(url, body=None, timeout=60, auth=True):
    """裸 HTTP，返回 (status, parsed_json_or_text, err_str)。"""
    hdr = {"Content-Type": "application/json"}
    if auth:
        hdr["Authorization"] = "Bearer " + KEY
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw), ""
            except Exception:
                return r.status, raw[:600], ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:600], "HTTPError"
    except Exception as e:
        return 0, "", "%s: %s" % (type(e).__name__, e)


# ────────────────────────── selftest（离线） ──────────────────────────
class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_escalate_transport(payload):
    """把 f3_escalate 里的 urllib.request.urlopen 换成返回固定 payload。

    `call_qwen` 用的是 `urllib.request.urlopen(...)` 和 `urllib.error.HTTPError`，
    所以要换的是 E.urllib.request 这一层（只影响本进程，不碰产线文件）。
    """
    import f3_escalate as E
    real = E.urllib

    class _ReqShim:
        Request = real.request.Request

        @staticmethod
        def urlopen(req, timeout=None):
            return _FakeResp(payload)

    class _ErrShim:
        HTTPError = real.error.HTTPError

    class _Shim:
        request = _ReqShim
        error = _ErrShim

    E.urllib = _Shim
    return E


def selftest():
    print("== selftest：把假响应灌进产线解析函数 ==")
    schema = {"type": "object", "properties": {"血压(mmHg)": {
        "type": "object", "properties": {"值": {"type": "string"},
                                         "块": {"type": "string"},
                                         "状态": {"type": "string"}},
        "required": ["值", "块", "状态"], "additionalProperties": False}},
        "required": ["血压(mmHg)"], "additionalProperties": False}
    good = {"值": "120/80", "块": "入院记录", "状态": "有值"}

    cases = [
        ("A 无 reasoning_content（旧 vLLM 形态）",
         {"choices": [{"message": {"content": json.dumps({"血压(mmHg)": good},
                                                         ensure_ascii=False)},
                       "finish_reason": "stop"}], "usage": {}}),
        ("B 有 reasoning_content + 正常 content（Strata 预期形态）",
         {"choices": [{"message": {"content": json.dumps({"血压(mmHg)": good},
                                                         ensure_ascii=False),
                                   "reasoning_content": "让我先看看这段病历……" * 40},
                       "finish_reason": "stop"}], "usage": {}}),
        ("C 有 reasoning_content + content 为空（思维链吃光 max_tokens）",
         {"choices": [{"message": {"content": "",
                                   "reasoning_content": "思考" * 5000},
                       "finish_reason": "length"}], "usage": {}}),
        ("D message 里只有 reasoning_content，没有 content 键",
         {"choices": [{"message": {"reasoning_content": "思考" * 100},
                       "finish_reason": "length"}], "usage": {}}),
    ]
    E = None
    for name, payload in cases:
        E = _patch_escalate_transport(payload)
        try:
            ans, usage = E.call_qwen(MODEL, schema, "sys", "user", timeout=5)
            print(f"  {name}\n    → OK  ans={json.dumps(ans, ensure_ascii=False)[:90]}")
        except Exception as ex:
            print(f"  {name}\n    → RAISED {type(ex).__name__}: {str(ex)[:120]}")

    # f3_batch 阶段二用的是内联取值，逐字复刻那两行验证
    print("\n  f3_batch.py:422-427 取值式（逐字复刻）：")
    for name, payload in cases:
        ch = (payload.get("choices") or [{}])[0]
        content = str((ch.get("message") or {}).get("content") or "")
        try:
            json.loads(content)
            verdict = "json.loads OK"
        except Exception as ex:
            verdict = f"json.loads → {type(ex).__name__}"
        print(f"    {name}\n      len(content)={len(content)}  {verdict}")
    return 0


# ────────────────────────── 能力探测（联网） ──────────────────────────
def probe():
    _no_proxy()
    print(f"== Strata 能力探测  base={BASE}  model={MODEL} ==")

    st, js, err = http(BASE.replace("/v1", "") + "/v1/models")
    ids = [m.get("id") for m in (js.get("data") or [])] if isinstance(js, dict) else []
    print(f"[B1] GET /v1/models  status={st} err={err}")
    print(f"     模型名: {ids}")
    print(f"     {'✓ 命中' if MODEL in ids else '✗ 未找到 ' + MODEL}")

    mini = {"model": MODEL, "max_tokens": 8,
            "messages": [{"role": "user", "content": "说一个字"}]}
    for auth in (True, False):
        st, js, err = http(BASE + "/chat/completions", mini, auth=auth)
        m = (js.get("choices") or [{}])[0].get("message") if isinstance(js, dict) else None
        print(f"[B2] chat/completions auth={auth} status={st} err={err}")
        if isinstance(m, dict):
            print(f"     keys={sorted(m.keys())} len(content)="
                  f"{len(str(m.get('content') or ''))} len(reasoning)="
                  f"{len(str(m.get('reasoning_content') or ''))}")
        else:
            print(f"     body={str(js)[:200]}")

    strict = dict(mini, response_format={"type": "json_schema", "json_schema": {
        "name": "f3_fields", "strict": True,
        "schema": {"type": "object", "properties": {"x": {"type": "string"}},
                   "required": ["x"], "additionalProperties": False}}})
    st, js, err = http(BASE + "/chat/completions", strict)
    print(f"[B3] json_schema strict status={st} err={err} body={str(js)[:300]}")

    print("[B4] enable_thinking 三态（思维链是否可控）")
    for tag, kw in (("thinking=False", {"chat_template_kwargs": {"enable_thinking": False}}),
                    ("thinking=True", {"chat_template_kwargs": {"enable_thinking": True}}),
                    ("不带该参数", {})):
        b = dict(mini, max_tokens=64)
        b.update(kw)
        st, js, err = http(BASE + "/chat/completions", b)
        ch = (js.get("choices") or [{}])[0] if isinstance(js, dict) else {}
        m = ch.get("message") or {}
        print(f"     {tag:16s} status={st} finish={ch.get('finish_reason')} "
              f"content={len(str(m.get('content') or ''))}字 "
              f"reasoning={len(str(m.get('reasoning_content') or ''))}字 err={err}")

    for path in ("/v1/responses", "/v1/messages"):
        st, js, err = http(BASE.replace("/v1", "") + path,
                           {"model": MODEL, "input": "hi", "max_output_tokens": 8})
        print(f"[B6] {path} status={st} err={err} body={str(js)[:160]}")
    return 0


# ────────────────────────── 并发回放 ──────────────────────────
def replay(path, levels):
    _no_proxy()
    reqs = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"== 并发回放  {len(reqs)} 个真实请求  base={BASE} model={MODEL} ==")
    for r in reqs:
        r["model"] = MODEL
    rows = []
    for n in levels:
        t0 = time.time()
        ok = fail = empty = 0
        tok_c = 0
        lat = []
        with ThreadPoolExecutor(max_workers=n) as ex:
            futs = [ex.submit(http, BASE + "/chat/completions", b, 900) for b in reqs]
            for f in as_completed(futs):
                st, js, err = f.result()
                s = time.time() - t0
                if st == 200 and isinstance(js, dict):
                    ch = (js.get("choices") or [{}])[0]
                    m = ch.get("message") or {}
                    c = str(m.get("content") or "")
                    u = js.get("usage") or {}
                    tok_c += u.get("completion_tokens", 0)
                    lat.append((u.get("completion_tokens", 0), s))
                    if c.strip():
                        ok += 1
                        try:
                            json.loads(c)
                        except Exception:
                            fail += 1
                            ok -= 1
                    else:
                        empty += 1
                else:
                    fail += 1
        wall = time.time() - t0
        thr = tok_c / wall if wall else 0
        rows.append((n, len(reqs), ok, fail, empty, tok_c, wall, thr))
        print(f"  并发 {n}: 请求 {len(reqs)} 成功 {ok} 失败 {fail} 空content {empty} "
              f"completion {tok_c} tok 墙钟 {wall:.1f}s 吞吐 {thr:.1f} tok/s")
        if lat:
            ls = sorted(x[1] for x in lat)
            print(f"          单请求延迟 min {ls[0]:.1f}s 中位 {ls[len(ls)//2]:.1f}s max {ls[-1]:.1f}s")
    print("\n  汇总（吞吐随并发的变化 → 判断该服务能不能并发）")
    if len(rows) >= 2 and rows[0][7] > 0:
        for r in rows:
            print(f"    并发 {r[0]}: 吞吐 {r[7]:.1f} tok/s = 单并发的 {r[7]/rows[0][7]:.2f}×")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--replay", help="strata_rehearsal.py 录的 requests.jsonl")
    ap.add_argument("--concurrency", default="1,2,4")
    a = ap.parse_args()
    rc = 0
    if a.selftest:
        rc |= selftest()
    if a.probe:
        rc |= probe()
    if a.replay:
        rc |= replay(a.replay, [int(x) for x in a.concurrency.split(",") if x.strip()])
    if not (a.selftest or a.probe or a.replay):
        print("给 --selftest / --probe / --replay 之一")
    sys.exit(rc)
