#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
laya_service.py — Laya 决策模型的微服务封装（打分前端 / 病历快判）

设计要点（都是踩过坑才知道的）：
  1. Laya 是"非自回归决策引擎"，一次前向出所有题的答案 —— 不是生成模型。
     所以它不会瞎编文本，但也**不能**用来做抽取式任务（500 列那种必须交 Qwen）。
  2. 它的 score 型答案返回的是**期望档位**（0..k-1 的浮点），不是整数档。
     本服务两种都给：`expected`（原生）+ `level`（取整档）+ `probs`（各档概率）。
  3. **置信度的取法是本服务最需要你确认的一处**：Laya 的 payload 里有校准概率，
     但字段名随版本可能变化。`_confidence()` 做了多路兜底并把「怎么算出来的」
     一起返回（confidence_source），既能直接上线，也方便你核对一次。
     拿不到置信度时返回 None —— 会被判为必须升级 Qwen，宁可贵一点不冒险。
  4. 中文病历：multilingual 检查点（mmBERT）比 english 强得多，默认用它；
     max_len=8192 才能吃下整段查体，默认 1024 会把长文本截断。
  5. 单实例串行 + 锁：Laya 33ms/问，串行足够；并发留给上层排队，
     避免 CUDA graph / 显存缓冲被并发覆盖。

启动：
    CUDA_VISIBLE_DEVICES=2 laya-env/bin/python laya_service.py --host 127.0.0.1 --port 8111

接口：
    GET  /health
    GET  /forms
    POST /score       {"text": "...", "scale": "nihss_15", "items": ["5_上肢运动"]}
    POST /classify    {"text": "..."}
    POST /score_batch {"texts": ["...", "..."], "scale": "nihss_15"}
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 必须在 import torch 之前固定可见卡
os.environ.setdefault("CUDA_VISIBLE_DEVICES", os.environ.get("GPU_LAYAD", "2"))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

HERE = Path(__file__).resolve().parent
FORMS_PATH = Path(os.environ.get("LAYA_FORMS", HERE / "laya_forms.json"))
DEVICE = os.environ.get("LAYA_DEVICE", "cuda")
PRELOAD = os.environ.get("LAYA_PRELOAD", "1") == "1"
DEFAULT_MODEL = os.environ.get("LAYA_ROUTER_MODEL", "multilingual")
MAX_LEN = int(os.environ.get("LAYA_MAX_LEN", "8192"))
CONF_MIN = float(os.environ.get("LAYAD_CONF_MIN", "0.70"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("laya-service")

FORMS: Dict[str, Any] = json.loads(FORMS_PATH.read_text(encoding="utf-8"))

app = FastAPI(title="Laya 决策服务", version="1.0")


# --------------------------------------------------------------------- 本地权重覆盖（离线）
# 用法：
#   export LAYA_ROUTER_MODELS='{"multilingual":"/home/ubuntu/laya-nihss-e8/ep7"}'
# 作用：改写 laya 包的 DEFAULT_MODELS，让 Router 直接加载本地目录（agent.py 对存在的目录不再走
#       snapshot_download），从而完全跳过 HuggingFace —— 这台机器不能上网。
def _install_local_models() -> None:
    raw = os.environ.get("LAYA_ROUTER_MODELS", "").strip()
    if not raw:
        return
    try:
        overrides = json.loads(raw)
    except Exception as e:
        log.warning("LAYA_ROUTER_MODELS 不是合法 JSON：%s", e)
        return
    try:
        import laya.router as _lr
        for k, v in overrides.items():
            _lr.DEFAULT_MODELS[k] = (v, None)
            _lr.STANDALONE_MODELS[k] = v
        log.info("本地权重覆盖已生效：%s", overrides)
    except Exception as e:
        log.warning("本地权重覆盖失败：%s", e)


_install_local_models()


# --------------------------------------------------------------------- /score 响应规范化
CONF_MIN_ENV = float(os.environ.get("LAYAD_CONF_MIN", str(CONF_MIN)))


def _normalize_answers(payload):
    """每项补 score_level（argmax 档位）与 low_conf（低于阈值 → 未判定）。"""
    if not isinstance(payload, dict):
        return payload
    ans = payload.get("answers")
    if not isinstance(ans, dict):
        return payload
    for _qid, it in ans.items():
        if not isinstance(it, dict):
            continue
        probs = it.get("probabilities")
        legend = it.get("legend") or {}
        lvl = None
        if isinstance(probs, dict) and probs:
            try:
                key = max(probs, key=lambda k: float(probs[k]))
                lvl = legend.get(key, key)
                lvl = str(key)
            except Exception:
                lvl = None
        it["score_level"] = lvl
        conf = it.get("answer_confidence", it.get("confidence"))
        try:
            it["low_conf"] = float(conf) < CONF_MIN_ENV
        except Exception:
            it["low_conf"] = None
    payload["conf_min"] = CONF_MIN_ENV
    payload["undetermined"] = sorted(
        k for k, v in ans.items()
        if isinstance(v, dict) and v.get("low_conf"))
    return payload
_router = None
_router_lock = None  # 懒建，避免 import 期就拉 torch


# --------------------------------------------------------------------- 引擎
def get_router():
    global _router, _router_lock
    if _router is None:
        import threading

        from laya import Router

        t0 = time.time()
        log.info("加载 Laya 检查点（device=%s, preload=%s）…", DEVICE, PRELOAD)
        try:
            _router = Router(device=DEVICE, preload=PRELOAD, default=DEFAULT_MODEL)
        except TypeError:
            # 老版本 Router 不接受 device= 关键字
            _router = Router(preload=PRELOAD, default=DEFAULT_MODEL)
        _router_lock = threading.Lock()
        log.info("检查点就绪，用时 %.1fs", time.time() - t0)
    return _router


def run_predict(text: str, questions: Dict[str, Any], model: Optional[str] = None) -> Dict[str, Any]:
    r = get_router()
    kwargs: Dict[str, Any] = {"max_len": MAX_LEN}
    if model:
        kwargs["model"] = model
    with _router_lock:
        return r.predict(text, questions, **kwargs)


# ------------------------------------------------------- 从 Laya payload 取值
def find_key(obj: Any, names: tuple, depth: int = 0) -> Any:
    """在嵌套 dict/list 里按字段名找第一个出现的值（Laya 的答案结构随版本有差异）。"""
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for n in names:
            if n in obj and obj[n] is not None:
                return obj[n]
        for v in obj.values():
            got = find_key(v, names, depth + 1)
            if got is not None:
                return got
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            got = find_key(v, names, depth + 1)
            if got is not None:
                return got
    return None


def first_scalar(obj: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(obj, (int, float, str, bool)):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            got = first_scalar(v, depth + 1)
            if got is not None:
                return got
    if isinstance(obj, (list, tuple)):
        for v in obj:
            got = first_scalar(v, depth + 1)
            if got is not None:
                return got
    return None


def get_probs(ans: Any) -> Optional[List[float]]:
    for key in ("probs", "probabilities", "distribution", "probs_per_level", "logits_softmax"):
        v = find_key(ans, (key,))
        if isinstance(v, (list, tuple)) and v and all(isinstance(x, (int, float)) for x in v):
            tot = float(sum(v)) or 1.0
            return [float(x) / tot for x in v]
    return None


def confidence_of(ans: Any, kind: str) -> tuple[Optional[float], str]:
    """返回 (置信度 0~1, 来源说明)。拿不到就 (None, 原因)。"""
    for key in ("confidence", "conf", "calibrated_confidence"):
        v = find_key(ans, (key,))
        if isinstance(v, (int, float)) and 0.0 <= float(v) <= 1.0:
            return float(v), f"payload.{key}"
    probs = get_probs(ans)
    if probs:
        if kind == "score":
            h = -sum(p * math.log(p) for p in probs if p > 0)
            h_max = math.log(len(probs)) if len(probs) > 1 else 1.0
            return max(0.0, 1.0 - h / h_max), "1-归一化熵(probs)"
        return max(probs), "max(probs)"
    if kind == "noul":
        p = find_key(ans, ("noul",))
        if isinstance(p, (int, float)):
            return abs(2.0 * float(p) - 1.0), "|2p-1|(noul)"
    return None, "无置信度字段（将触发升级）"


def normalize_answer(ans: Any, qdef: Dict[str, Any]) -> Dict[str, Any]:
    """把一个问题的原始答案整理成统一形态。"""
    kind = (qdef.get("type") or "choice").lower()
    crit = qdef.get("criteria")

    if kind == "noul":
        p = find_key(ans, ("noul", "prob_true", "probability", "p_true"))
        if p is None:
            p = find_key(ans, ("choice",))
            if isinstance(p, str) and isinstance(crit, dict):
                es = [k for k, v in crit.items() if v == p]
                p = True if (es and es[0] == "true") else False
        val = float(p) if isinstance(p, (int, float)) else None
        conf, src = confidence_of(ans, "noul")
        return {"kind": "noul", "prob_true": val, "label": (val >= 0.5 if val is not None else None),
                "confidence": conf, "confidence_source": src, "raw": ans}

    if kind == "score":
        n = len(crit) if isinstance(crit, list) else 0
        exp = find_key(ans, ("score", "expected", "expected_level", "value"))
        if exp is None:
            exp = first_scalar(ans)
        try:
            expf = float(exp)
        except (TypeError, ValueError):
            expf = None
        lvl = None if expf is None else max(0, min(n - 1, int(round(expf)))) if n else None
        probs = get_probs(ans)
        conf, src = confidence_of(ans, "score")
        return {"kind": "score", "expected": expf, "level": lvl, "n_levels": n,
                "probs": probs, "confidence": conf, "confidence_source": src, "raw": ans}

    choice = find_key(ans, ("choice", "label", "top_label"))
    if choice is None:
        choice = first_scalar(ans)
    conf, src = confidence_of(ans, "choice")
    return {"kind": "choice", "choice": choice, "confidence": conf,
            "confidence_source": src, "raw": ans}


# --------------------------------------------------------------------- 模型
class ScoreReq(BaseModel):
    text: str = Field(..., description="患者文本（查体/病历片段）")
    scale: str = "nihss_15"
    items: Optional[List[str]] = Field(None, description="只算指定项；None = 全部")
    model: Optional[str] = None


class ClassifyReq(BaseModel):
    text: str
    questions: Optional[List[str]] = None
    model: Optional[str] = None


class BatchReq(BaseModel):
    texts: List[str]
    scale: str = "nihss_15"


# --------------------------------------------------------------------- 组装
def build_questions(scale: str, only: Optional[List[str]] = None) -> tuple[Dict[str, Any], Dict[str, Any]]:
    sc = FORMS["scales"].get(scale)
    if not sc:
        raise HTTPException(400, f"未知量表 {scale}；可用：{list(FORMS['scales'])}")
    prefix = sc.get("instructions_prefix", "")
    suffix = sc.get("item_instructions_suffix", "")
    q: Dict[str, Any] = {}
    meta: Dict[str, Any] = {}
    for it in sc["items"]:
        if only and it["key"] not in only:
            continue
        q[it["key"]] = {"type": "score",
                        "instructions": f"{prefix} 【{it['label']}】 {it['instructions']} {suffix}".strip(),
                        "criteria": it["criteria"]}
        meta[it["key"]] = {"label": it["label"], "max": len(it["criteria"]) - 1}
    return q, meta


def score_text(text: str, scale: str, only: Optional[List[str]] = None,
               model: Optional[str] = None) -> Dict[str, Any]:
    q, meta = build_questions(scale, only)
    t0 = time.time()
    payload = run_predict(text, q, model=model)
    elapsed = (time.time() - t0) * 1000
    answers = (payload or {}).get("answers") or {}

    items: Dict[str, Any] = {}
    total_expected = 0.0
    total_level = 0
    escalate: List[str] = []
    for key, qdef in q.items():
        raw = answers.get(key)
        if raw is None:
            items[key] = {"error": "引擎未返回该题", **meta[key], "need_escalate": True}
            escalate.append(key)
            continue
        a = normalize_answer(raw, qdef)
        a.update(meta[key])
        a["need_escalate"] = (a.get("confidence") is None) or (a["confidence"] < CONF_MIN)
        if a["need_escalate"]:
            escalate.append(key)
        if a.get("expected") is not None:
            total_expected += a["expected"]
        if a.get("level") is not None:
            total_level += a["level"]
        items[key] = a

    return {
        "scale": scale,
        "total_expected": round(total_expected, 2),      # 与真实 NIHSS 可比的小数总分
        "total_level": total_level,                       # 取整档后的整数总分
        "max_possible": sum(m["max"] for m in meta.values()),
        "items": items,
        "need_escalate": escalate,
        "need_escalate_count": len(escalate),
        "routing": (payload or {}).get("routing"),
        "usage": (payload or {}).get("usage"),
        "elapsed_ms": round(elapsed, 1),
        "model": (payload or {}).get("routing", {}).get("model", DEFAULT_MODEL)
        if isinstance((payload or {}).get("routing"), dict) else DEFAULT_MODEL,
    }


# --------------------------------------------------------------------- 路由
@app.get("/health")
def health():
    return {"status": "ok", "device": DEVICE, "default_model": DEFAULT_MODEL,
            "max_len": MAX_LEN, "conf_min": CONF_MIN, "loaded": _router is not None,
            "forms": list(FORMS["scales"])}


@app.get("/forms")
def forms():
    return FORMS


@app.post("/score")
def score(req: ScoreReq):
    if not req.text.strip():
        raise HTTPException(400, "text 为空")
    return _normalize_answers(score_text(req.text, req.scale, req.items, req.model))


@app.post("/classify")
def classify(req: ClassifyReq):
    if not req.text.strip():
        raise HTTPException(400, "text 为空")
    spec = FORMS["record_classify"]["questions"]
    q = {k: v for k, v in spec.items() if not req.questions or k in req.questions}
    if not q:
        raise HTTPException(400, f"没有匹配的问题；可用：{list(spec)}")
    t0 = time.time()
    payload = run_predict(req.text, q, model=req.model)
    answers = (payload or {}).get("answers") or {}
    out: Dict[str, Any] = {}
    escalate: List[str] = []
    for k, qdef in q.items():
        raw = answers.get(k)
        if raw is None:
            out[k] = {"error": "引擎未返回", "need_escalate": True}
            escalate.append(k)
            continue
        a = normalize_answer(raw, qdef)
        a["need_escalate"] = (a.get("confidence") is None) or (a["confidence"] < CONF_MIN)
        if a["need_escalate"]:
            escalate.append(k)
        out[k] = a
    return {"answers": out, "need_escalate": escalate,
            "routing": (payload or {}).get("routing"),
            "elapsed_ms": round((time.time() - t0) * 1000, 1)}


@app.post("/score_batch")
def score_batch(req: BatchReq):
    if not req.texts:
        raise HTTPException(400, "texts 为空")
    return {"results": [score_text(t, req.scale) for t in req.texts]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8111)
    ap.add_argument("--no-preload", action="store_true")
    args = ap.parse_args()
    if args.no_preload:
        PRELOAD = False
    if PRELOAD:
        get_router()  # 启动即加载，避免第一发请求卡 7~10 秒
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
