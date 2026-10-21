#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
F6 仲裁层：把 route.py 标了 escalate 的题目打包交 Qwen 定档。
契约：一份文档 = 一次 Qwen 调用（所有升级题一个请求）。
题干来自 laya_forms.json（唯一真相源）；要求模型给出依据原话，落盘前逐条回查。
未接地的裁定不静默丢弃，也不归零，而是标记 pending 交人工 —— 与 F4 的「待确认」同一口径。
"""
import argparse
import json
import os
import re
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORMS = Path(os.environ.get("LAYA_FORMS", HERE / "laya_forms.json"))
def _chat_url():
    b = os.environ.get("QWEN_URL", "http://127.0.0.1:8102/v1").rstrip("/")
    return b if b.endswith("/chat/completions") else b + "/chat/completions"


QWEN_URL = _chat_url()
QWEN_MODEL = os.environ.get("QWEN_MODEL", "")
TIMEOUT = float(os.environ.get("QWEN_TIMEOUT", "900"))

SYS = (
    "你是卒中量表的定档仲裁员。逐题判断患者在该题上的实际得分档位。\n"
    "严格只依据病历原文中明确写到的内容：原文未提及该题、或该题被描述为正常时，一律选 0 分档，"
    "不得根据主诉、病情或其他题目推断。\n"
    "每题必须同时给出你在原文中依据的那一句原话（quote，不超过 40 字，必须逐字摘自原文）；"
    "若选 0 分档且原文没有相关描述，quote 填空字符串。\n"
    "只输出 JSON，不要输出任何解释。"
)


def load_forms():
    d = json.loads(FORMS.read_text(encoding="utf-8"))
    return {it["key"]: it for it in d["scales"]["nihss_15"]["items"]}


def build_prompt(text, keys, forms):
    lines = ["【病历原文】", text, "", "【需要定档的题目】"]
    props = {}
    for i, k in enumerate(keys, 1):
        it = forms[k]
        lines.append(f"{i}. {it['label']} —— {it['instructions']}")
        for j, c in enumerate(it["criteria"]):
            lines.append(f"    {j}. {c}")
        props[k] = {"type": "object",
                    "properties": {"level": {"type": "integer"},
                                   "quote": {"type": "string"}},
                    "required": ["level", "quote"], "additionalProperties": False}
    lines += ["", "输出 JSON，题目标识原样使用：" + ", ".join(keys)]
    return "\n".join(lines), props


def norm(s):
    return re.sub(r"\s+", "", s or "")


def call_qwen(prompt, props, keys):
    body = {
        "model": QWEN_MODEL,
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 4096,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "nihss_arbitration", "strict": True,
                            "schema": {"type": "object", "properties": props,
                                       "required": keys, "additionalProperties": False}},
        },
    }
    req = urllib.request.Request(
        QWEN_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise SystemExit("[QWEN %s] url=%s\nmodel=%r\nbody=%s"
                         % (e.code, QWEN_URL, QWEN_MODEL, body[:800]))
    return json.loads(d["choices"][0]["message"]["content"]), d.get("usage")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-file", required=True)
    ap.add_argument("--route", required=True, help="route.py 的输出")
    ap.add_argument("--out", required=True)
    ap.add_argument("--prov", default=None, help="provenance.jsonl 路径")
    a = ap.parse_args()

    text = Path(a.text_file).read_text(encoding="utf-8")
    route = json.loads(Path(a.route).read_text(encoding="utf-8"))
    forms = load_forms()
    ntext = norm(text)

    keys = [k for k, v in route["items"].items()
            if v["source"] == "laya" and v.get("escalate")]
    out = {"scale": route["scale"], "items": {}, "items_sent": keys,
           "n_qwen": len(keys), "qwen_usage": None, "qwen_model": QWEN_MODEL}

    qwen = {}
    if keys:
        prompt, props = build_prompt(text, keys, forms)
        qwen, usage = call_qwen(prompt, props, keys)
        out["qwen_usage"] = usage

    for k, v in route["items"].items():
        rec = {"final_level": v["level"] if v["level"] is not None else 0,
               "source": v["source"], "laya_level": v.get("level"),
               "laya_margin": v.get("margin"), "evidence": v.get("evidence"),
               "quote": None, "quote_ok": None, "pending": False}
        if k in qwen:
            q = qwen.get(k) or {}
            lv, quote = q.get("level"), (q.get("quote") or "")
            rec["final_level"] = int(lv) if isinstance(lv, int) else 0
            rec["quote"] = quote
            rec["source"] = "qwen_arbitrated"
            ok = (quote == "") or (norm(quote) in ntext)
            rec["quote_ok"] = ok
            if not ok:
                rec["source"] = "qwen_ungrounded"
                rec["pending"] = True
        out["items"][k] = rec

    out["total_final"] = sum(r["final_level"] for r in out["items"].values())
    out["n_pending"] = sum(1 for r in out["items"].values() if r["pending"])
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.prov:
        with open(a.prov, "w", encoding="utf-8") as f:
            for k, r in out["items"].items():
                f.write(json.dumps({"item": k, **r}, ensure_ascii=False) + "\n")

    print(json.dumps({"n_qwen": out["n_qwen"], "items_sent": keys,
                      "total_final": out["total_final"], "n_pending": out["n_pending"],
                      "qwen_usage": out["qwen_usage"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
