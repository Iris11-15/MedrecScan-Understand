#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""strata_rehearsal.py —— 不联网，把**产线自己的** f3_batch 驱动跑一遍。

做法：只把 `f3_batch.post`（唯一的出网点）换成桩，其余一行产线代码都不改。
两个用途：
  1) 录下真实请求体 → requests.jsonl，联网后用 strata_probe.py --replay 原样回放；
  2) 端到端验证解析层对 Strata 形态（多 reasoning_content / content 为空）的反应。

⚠️ 桩返回的是合成答案，**不是模型输出**，产物只作链路验证，
   不得当作任何指标。输出只落在 run/ 下的 rehearsal_* 目录。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

REC = []
MODE = "ok"
FAKE_REASONING = "（桩：模拟 Strata 的 reasoning_content）" * 300


def stub_post(url, body, timeout):
    REC.append({"url": url, "body": body})
    schema = ((body.get("response_format") or {}).get("json_schema") or {}).get("schema") or {}
    props = schema.get("properties") or {}
    if MODE == "empty":
        content, fr = "", "length"
    else:
        content = json.dumps({k: {"值": "", "块": "", "状态": "未提及"} for k in props},
                             ensure_ascii=False)
        fr = "stop"
    n_chars = sum(len(m["content"]) for m in body["messages"])
    return {"choices": [{"message": {"content": content,
                                     "reasoning_content": FAKE_REASONING},
                         "finish_reason": fr}],
            "usage": {"prompt_tokens": n_chars // 2,
                      "completion_tokens": len(content) // 2}}


def inventory(cases_root, limit):
    """阶段一（规则层+切批）单独跑一遍，报真实请求清单。纯 CPU，不联网。"""
    import f3_batch as B
    import f3_escalate as E
    import route_f3 as R
    variables = json.loads((HERE / "variables_f3.json").read_text(encoding="utf-8"))
    alias = R.load_alias()
    cases = B.find_window(cases_root, 0, limit)
    print(f"== 请求清单（规则层+切批，真实数据）  {len(cases)} 例 ==")
    print(f"   MAX_MODEL_LEN={E.MAX_MODEL_LEN}  DEFAULT_BUDGET_CHARS={E.DEFAULT_BUDGET_CHARS}")
    out = []
    for case in cases:
        try:
            rule_out, reqs, sec, oversize = B.stage1(case, variables, alias)
        except Exception as ex:
            print(f"  [跳过] {case.name}: {type(ex).__name__}: {str(ex)[:110]}")
            continue
        print(f"  {case.name}: {len(rule_out)} 列，需升级 "
              f"{sum(len(r['cols']) for r in reqs)} 列 → {len(reqs)} 个请求"
              + (f"，{len(oversize)} 列单批装不下" if oversize else ""))
        for j, r in enumerate(reqs):
            info = r.get("info") or {}
            est = info.get("est_tokens")
            lim = info.get("max_model_len")
            room = (lim - est - 64) if (lim and est) else None
            mt = r.get("max_tokens")
            sent = None if room is None or mt is None else max(256, min(mt, room))
            print(f"    批{j}: {len(r['cols'])} 列 est_tokens={est} "
                  f"max_model_len={lim} 发出 max_tokens={sent}")
            out.append({"case": case.name, "batch": j, "cols": len(r["cols"]),
                        "est_tokens": est, "max_model_len": lim,
                        "max_tokens_sent": sent, "tier": r.get("tier")})
    return out


def main_run(cases_root, outdir, limit, workers, model):
    import f3_batch as B
    B.post = stub_post
    os.environ.setdefault("QWEN_URL", "http://127.0.0.1:8080/v1")
    sys.argv = ["f3_batch.py", "--cases", cases_root, "--outdir", outdir,
                "--workers", str(workers), "--limit", str(limit), "--model", model]
    t0 = time.time()
    B.main()
    print(f"\n== 桩驱动耗时 {time.time()-t0:.1f}s，拦截到 {len(REC)} 个请求 ==")
    for i, r in enumerate(REC):
        b = r["body"]
        print(f"  请求{i}: url={r['url']} model={b['model']} "
              f"max_tokens={b['max_tokens']} "
              f"enable_thinking={((b.get('chat_template_kwargs') or {}).get('enable_thinking'))} "
              f"response_format={b.get('response_format', {}).get('type')} "
              f"messages_chars={sum(len(m['content']) for m in b['messages'])}")
    return REC


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True, help="compiled 根目录（只读）")
    ap.add_argument("--outdir", required=True, help="产物目录（必须在 run/ 下）")
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--model", default=os.environ.get("STRATA_MODEL",
                                                     "qwen3.8-flash-next-iq3_s"))
    ap.add_argument("--mode", choices=["ok", "empty"], default="ok",
                    help="ok=桩返回正常 content；empty=桩返回空 content")
    ap.add_argument("--dump-requests", help="把真实请求体写到这里（jsonl）")
    a = ap.parse_args()
    MODE = a.mode
    Path(a.outdir).mkdir(parents=True, exist_ok=True)
    inventory(a.cases, a.limit)
    recs = main_run(a.cases, a.outdir, a.limit, a.workers, a.model)
    if a.dump_requests:
        with open(a.dump_requests, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r["body"], ensure_ascii=False) + "\n")
        print(f"已写 {len(recs)} 条请求体 → {a.dump_requests}")
