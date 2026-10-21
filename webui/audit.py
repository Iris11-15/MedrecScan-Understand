"""审计日志（round37 F9）：run/wide/_ingest/_audit.jsonl，append-only，权限 600。

记录 {ts, action, batch, actor, detail}；detail 里不许有任何 PII 值（写前过 pii.scrub_text）。
「本地查看原文」（F4）只记 时间 + 列名 + 行号，**不记值**。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pii

INGEST_ROOT = Path("/home/ubuntu/medrec/run/wide/_ingest")
AUDIT_PATH = INGEST_ROOT / "_audit.jsonl"

_lock = threading.Lock()


def actor() -> str:
    return f"uid:{os.getuid()}"


def write(action: str, batch: str = "", detail: object = None) -> dict:
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "epoch": int(time.time()),
        "action": str(action),
        "batch": pii.scrub_text(str(batch or "")),
        "actor": actor(),
        "detail": pii.scrub_text(_flat(detail)),
    }
    line = json.dumps(rec, ensure_ascii=False)
    with _lock:
        try:
            AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(AUDIT_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            os.chmod(AUDIT_PATH, 0o600)
        except OSError:
            pass
    return rec


def _flat(detail: object) -> str:
    if detail is None:
        return ""
    if isinstance(detail, (str, int, float)):
        return str(detail)
    if isinstance(detail, dict):
        return " ".join(f"{k}={_flat(v)}" for k, v in detail.items())
    if isinstance(detail, (list, tuple)):
        return ",".join(_flat(v) for v in detail)
    return str(detail)


def tail(n: int = 50) -> list[dict]:
    """只读：取审计尾巴（给页面排错用）。"""
    try:
        lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out
