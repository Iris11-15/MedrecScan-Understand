#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
22-score-pipeline.py — NIHSS 打分的完整编排：Laya 出分 → 低置信升级 Qwen → 合并落证据

为什么要有"两个模型打分"这一层：
  · Laya 33ms、一次前向出全部 15 项、概率经过校准 —— 但它只是 System 1 的快判，
    在中文长查体上会错。
  · Qwen3.8-27B 慢得多（秒级）但读得懂上下文 —— 让它跑全量既不划算也不可复现。
  · 所以：**Laya 全量跑 + 只把低置信项升级给 Qwen**，两条都归档，分歧项标记人工复核。
    这正是 tech-stack.md 里"按变量路由、不按文档路由"的落地形态。

用法：
    # 单份文本
    python 22-score-pipeline.py --text "查体：神志清，… NIHSS 评分 1 分" --biz-id T001

    # 从文件读（compiled.md 或任意文本）
    python 22-score-pipeline.py --file ~/medrec/jobs/T001/compiled/compiled.md --biz-id T001

    # 只跑 Laya 不升级（快，用来做基线）
    python 22-score-pipeline.py --file x.md --no-escalate

    # 自检（不连服务，只验代码与量表）
    python 22-score-pipeline.py --selftest

输出：~/medrec/jobs/<biz-id>/scoring/nihss.json（含逐项证据与来源）+ 屏幕摘要
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

HERE = Path(__file__).resolve().parent
FORMS = json.loads((HERE / "laya_forms.json").read_text(encoding="utf-8"))

LAYAD_URL = os.environ.get("LAYAD_URL", "http://127.0.0.1:8111").rstrip("/")
QWEN_URL = os.environ.get("QWEN_URL", "http://127.0.0.1:8102/v1").rstrip("/")
QWEN_MODEL = os.environ.get("MODEL_QWEN_NAME", "qwen-27b")
CONF_MIN = float(os.environ.get("LAYAD_CONF_MIN", "0.70"))
OUT_ROOT = Path(os.environ.get("MEDREC_JOBS", str(Path.home() / "medrec/jobs")))


# --------------------------------------------------------------- Laya 一侧
def laya_score(text: str, scale: str = "nihss_15",
               items: Optional[List[str]] = None) -> Dict[str, Any]:
    r = requests.post(f"{LAYAD_URL}/score",
                      json={"text": text, "scale": scale, "items": items}, timeout=300)
    r.raise_for_status()
    return r.json()


def laya_classify(text: str) -> Dict[str, Any]:
    r = requests.post(f"{LAYAD_URL}/classify", json={"text": text}, timeout=300)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------- Qwen 一侧
ARBITER_SCHEMA = {
    "type": "object",
    "properties": {
        "level": {"type": "integer", "minimum": 0, "maximum": 4},
        "evidence": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["level", "evidence"],
    "additionalProperties": False,
}


def qwen_level(item_label: str, criteria: List[str], text: str,
               use_response_format: bool = True) -> Dict[str, Any]:
    """让 Qwen 在给定档位里选一档，并给出原文依据。"""
    levels = "\n".join(f"  {i} = {c}" for i, c in enumerate(criteria))
    prompt = (
        f"你是神经内科医师。下面是患者病历文本，请对 NIHSS 的「{item_label}」项定档。\n\n"
        f"可选档位（必须选其中一个整数）：\n{levels}\n\n"
        "要求：\n"
        "1) 只依据原文中明确写到的体征；未提及按 0 档处理，并在 evidence 里写明「原文未提及」。\n"
        "2) evidence 必须是原文里的一小段引用（<=30 字），不能是你自己的推理。\n"
        "3) 只输出 JSON，不要输出任何解释性文字。\n\n"
        f"病历文本：\n---\n{text}\n---"
    )
    body: Dict[str, Any] = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 512,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if use_response_format:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "nihss_item", "schema": ARBITER_SCHEMA, "strict": True},
        }
    r = requests.post(f"{QWEN_URL}/chat/completions", json=body, timeout=600)
    if r.status_code >= 400 and use_response_format:
        # 本机 vllm 的 response_format 支持情况随版本变，退回纯提示词约束
        return qwen_level(item_label, criteria, text, use_response_format=False)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    try:
        got = json.loads(content)
    except json.JSONDecodeError:
        s, e = content.find("{"), content.rfind("}")
        got = json.loads(content[s:e + 1]) if s >= 0 and e > s else {"parse_error": content}
    got["_raw"] = content
    return got


# --------------------------------------------------------------- 编排
def pipeline(text: str, biz_id: str, scale: str = "nihss_15",
             escalate: bool = True, do_classify: bool = True) -> Dict[str, Any]:
    t0 = time.time()
    out: Dict[str, Any] = {"biz_id": biz_id, "scale": scale, "started_at": time.strftime("%F %T")}

    sc = FORMS["scales"][scale]
    crit_by_key = {it["key"]: (it["label"], it["criteria"]) for it in sc["items"]}

    # ① Laya 全量
    laya = laya_score(text, scale)
    out["laya"] = {k: v for k, v in laya.items() if k != "items"}
    out["items"] = {}

    # ② 低置信 / 无置信 → 升级 Qwen
    escalated = set(laya.get("need_escalate") or []) if escalate else set()

    for key, item in laya["items"].items():
        rec: Dict[str, Any] = {
            "label": item.get("label"),
            "laya": {k: item.get(k) for k in
                     ("expected", "level", "probs", "confidence", "confidence_source")},
            "final_level": item.get("level"),
            "final_source": "laya",
            "need_escalate": key in escalated,
        }
        if key in escalated and escalate:
            label, criteria = crit_by_key.get(key, (key, []))
            try:
                q = qwen_level(label, criteria, text)
                rec["qwen"] = {"level": q.get("level"), "evidence": q.get("evidence"),
                               "confidence": q.get("confidence")}
                rec["final_level"] = q.get("level")
                rec["final_source"] = "qwen"
                if item.get("level") is not None and q.get("level") is not None:
                    rec["disagree"] = int(item["level"]) != int(q["level"])
                    if rec["disagree"]:
                        rec["final_source"] = "qwen(分歧)"
                        rec["needs_human_review"] = True
            except Exception as e:  # 服务不可用时不能把整单打挂
                rec["qwen"] = {"error": f"{type(e).__name__}: {e}"}
                rec["final_source"] = "laya(升级失败)"
                rec["needs_human_review"] = True
        out["items"][key] = rec

    # ③ 汇总
    tot_laya = sum(v["laya"].get("level") or 0 for v in out["items"].values())
    tot_final = sum(v.get("final_level") or 0 for v in out["items"].values())
    out["total_laya"] = tot_laya
    out["total_final"] = tot_final
    out["n_escalated"] = len(escalated)
    out["n_disagree"] = sum(1 for v in out["items"].values() if v.get("disagree"))
    out["n_review"] = sum(1 for v in out["items"].values() if v.get("needs_human_review"))

    # ④ 病历自动识别（Laya 快判）
    if do_classify:
        try:
            out["record_classify"] = laya_classify(text)
        except Exception as e:
            out["record_classify"] = {"error": f"{type(e).__name__}: {e}"}

    out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
    return out


def selftest() -> int:
    """量表自检：把满分口径算清楚并打印 —— 这个数字很容易算错，所以固化成断言。"""
    ok = True
    sc = FORMS["scales"]["nihss_15"]
    items = sc["items"]
    maxim = {it["key"]: len(it["criteria"]) - 1 for it in items}
    all_max = sum(maxim.values())          # 13 行逐项满配
    coma_ceiling = maxim.get("1a_意识水平", 0)   # 1a=3 → 1b/1c 记 0 且其余项不计

    n_scored = len(items) + 2   # 1a / 1b / 1c 挤在一行里写作 3 项，故 13 行 = 15 个计分项
    print(f"量表行数    : {len(items)} 行（1a/1b/1c 算 3 项 → 共 {n_scored} 个计分项，标准 NIHSS 为 15）")
    print(f"逐项满配合计 : {all_max}   ← 本配置 total_expected / total_level 的上界")
    print(f"昏迷特例上限 : {coma_ceiling}   ← 1a=3 时 1b/1c 记 0 且其余项不再计分")
    print( "满分口径说明 : 网上常写的「NIHSS 满分 42」用的是**左右肢体分别计分**的标准版")
    print( "               （左臂/右臂/左腿/右腿 各 0-4）—— 比合并计分的本配置多 4×2=8 分，")
    print( "               34 + 8 = 42，两个数字并不矛盾。")
    print( "               本配置对 13 行各自独立出分（保留信息，不把临床规则硬编码进模型）；")
    print( "               要落到标准分制：拆左右肢体后求和，且 1a>=1 时把 1b/1c 置 0。")

    if n_scored != 15:
        print(f"  ✗ 计分项应为 15（1a/1b/1c + 12 项），实际 {n_scored}"); ok = False
    if all_max != 34:
        print(f"  ✗ 逐项满配应为 34，实际 {all_max}（档位定义被改过？）"); ok = False
    if all_max + 8 != 42:
        print(f"  ✗ 左右肢体拆分后应为 42(=34+8)，实际 {all_max + 8}"); ok = False
    if coma_ceiling != 3:
        print(f"  ✗ 昏迷上限应为 3，实际 {coma_ceiling}"); ok = False
    keys = [it["key"] for it in items]
    if len(set(keys)) != len(keys):
        print("  ✗ 存在重复 key"); ok = False
    for it in items:
        if len(it["criteria"]) < 2:
            print(f"  ✗ {it['key']} 档位少于 2 档"); ok = False

    print("分类题目   :", list(FORMS["record_classify"]["questions"]))
    print("目标服务   : Laya %s | Qwen %s" % (LAYAD_URL, QWEN_URL))
    print("结果       :", "✓ 通过" if ok else "✗ 有错")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="直接给文本")
    ap.add_argument("--file", help="从文件读文本")
    ap.add_argument("--biz-id", default="adhoc")
    ap.add_argument("--scale", default="nihss_15")
    ap.add_argument("--no-escalate", action="store_true", help="只跑 Laya，不升级 Qwen")
    ap.add_argument("--no-classify", action="store_true")
    ap.add_argument("--out", help="输出路径（默认 <jobs>/<biz-id>/scoring/nihss.json）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    if a.file:
        text = Path(a.file).read_text(encoding="utf-8", errors="replace")
    elif a.text:
        text = a.text
    else:
        ap.error("需要 --text 或 --file（或用 --selftest）")

    res = pipeline(text, a.biz_id, a.scale,
                   escalate=not a.no_escalate, do_classify=not a.no_classify)

    out_path = Path(a.out) if a.out else OUT_ROOT / a.biz_id / "scoring" / "nihss.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 62)
    print(f"  biz_id        {res['biz_id']}")
    print(f"  Laya 总分     {res['total_laya']}")
    print(f"  最终总分      {res['total_final']}   (满分 {res['laya'].get('max_possible')})")
    print(f"  升级 Qwen     {res['n_escalated']} 项   分歧 {res['n_disagree']} 项   待人工复核 {res['n_review']} 项")
    print(f"  耗时          {res['elapsed_ms']} ms")
    print("-" * 62)
    for k, v in res["items"].items():
        flag = "!" if v.get("needs_human_review") else " "
        la = v["laya"].get("level")
        fi = v.get("final_level")
        cf = v["laya"].get("confidence")
        cfs = f"{cf:.2f}" if isinstance(cf, (int, float)) else " n/a"
        print(f" {flag} {v['label'][:22]:<24} laya={la} conf={cfs:<5} -> final={fi} [{v['final_source']}]")
    print("-" * 62)
    print(f"  产物：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
