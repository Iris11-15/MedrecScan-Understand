#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.py —— 六阶段共用的地基：配置、哈希、审计、PII 打码、无代理 HTTP。

设计约束（来自任务书，改代码前先对齐）：
  * 阶段之间**用文件交接**，不在内存里串 —— 所以这里只提供读写/清单工具。
  * 审计三哈希：OCR 输入 / Prompt / 输出。所有哈希走 sha256，落 audit.jsonl。
  * PII（身份证号 / 电话 / 地址）不许进 stdout 与日志，日志里打码。
  * 输出文件权限 600。
  * 本机 127.0.0.1:7897 是无效代理 —— 发请求前必须清代理环境变量。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

CONFIG_DIR = Path("/home/ubuntu/medrec/config")
PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


# --------------------------------------------------------------- 配置
def load_yaml(path: Path) -> dict:
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_columns(path: Path | None = None) -> dict:
    cfg = load_yaml(path or CONFIG_DIR / "columns.yaml")
    ids = [c["id"] for c in cfg["columns"]]
    if len(set(ids)) != len(ids):
        raise SystemExit("columns.yaml: id 必须唯一（重名列靠 id 区分）")
    return cfg


def load_pipeline(path: Path | None = None) -> dict:
    return load_yaml(path or CONFIG_DIR / "pipeline.yaml")


def column_ids(cfg: dict) -> list[str]:
    return [c["id"] for c in cfg["columns"]]


def pii_ids(cfg_cols: dict) -> set[str]:
    return {c["id"] for c in cfg_cols["columns"] if c.get("pii")}


# --------------------------------------------------------------- 哈希
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canon_json(obj: Any) -> str:
    """稳定序列化 —— 哈希要可复现，键序与空格必须固定。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------- PII
def mask_value(text: str, kind: str, cfg_pii: dict) -> str:
    """日志用打码。身份证保留前 3 后 2；其余一律只留长度。"""
    if text is None:
        return "null"
    s = str(text)
    if kind == "id_card":
        m = cfg_pii["mask_id_card"]
        head, tail = int(m["keep_head"]), int(m["keep_tail"])
        if len(s) <= head + tail:
            return "*" * len(s)
        return f"{s[:head]}{'*' * (len(s) - head - tail)}{s[-tail:]}"
    return f"<{len(s)} chars redacted>"


def scrub_for_log(obj: Any, cfg_cols: dict, cfg_pii: dict) -> Any:
    """把 dict 里的 PII 字段换成打码形式，用于日志/报告。"""
    pii = pii_ids(cfg_cols)
    never = set(cfg_pii.get("never_log_columns", []))
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in pii or k in never:
                out[k] = mask_value(_field_value(v), k, cfg_pii)
            else:
                out[k] = scrub_for_log(v, cfg_cols, cfg_pii)
        return out
    if isinstance(obj, list):
        return [scrub_for_log(x, cfg_cols, cfg_pii) for x in obj]
    return obj


def _field_value(v: Any) -> Any:
    """字段值可能是标量，也可能是 {value:…, source_label:…} 包装。"""
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return v


# --------------------------------------------------------------- 审计
class Audit:
    """审计日志：每阶段追加 JSONL 行（OCR 输入哈希 / Prompt 哈希 / 输出哈希）。"""

    def __init__(self, path: Path, stage: str):
        self.path = Path(path)
        self.stage = stage
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 审计里虽然只放哈希，但按 PII 同级处理：产物一律 600
        if not self.path.exists():
            self.path.touch()
        chmod(self.path)

    def emit(self, **fields: Any) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "stage": self.stage}
        rec.update(fields)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        chmod(self.path)


# --------------------------------------------------------------- 文件交接
def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, obj: Any, mode: str | None = "600") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _open_out(tmp, mode) as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    if mode:
        chmod(path, mode)
    return path


def write_text(path: Path, text: str, mode: str | None = "600") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _open_out(path, mode) as f:
        f.write(text)
    if mode:
        chmod(path, mode)
    return path


def write_marker(path: Path, text: str = "") -> Path:
    """.done / .incomplete 也算产物：目录名里带来源目录（可能含姓名），权限一律跟输出文件一致。"""
    return write_text(path, text)


def write_yaml(path: Path, obj: Any, mode: str | None = "600") -> Path:
    """映射表要给人看、给人改 —— 用 YAML 不用 JSON。"""
    import yaml
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _open_out(tmp, mode) as f:
        yaml.safe_dump(obj, f, allow_unicode=True, sort_keys=False, width=100)
    os.replace(tmp, path)
    if mode:
        chmod(path, mode)
    return path


def chmod(path: Path, mode: str = "600") -> None:
    os.chmod(path, int(str(mode), 8))


def _open_out(path: Path, mode: str | None):
    """权限必须在 **open 时**就带上，不能写完再 chmod。

    旧写法：tmp 按 umask 落地（现场 umask=002 ⇒ 664）→ os.replace 继承 664 →
    chmod 在 replace **之后**。进程恰好死在这两步之间（Ctrl-C / OOM / 手动停跑批）
    ⇒ 线上产物**永久 664**，而里面是整页 OCR 正文 + 内嵌姓名的 case_id（红线⑨）。
    实测复现 workspace/round45/probes/g4_writejson_window.py：B 段 0o664。
    mode=None 时保持原语义（不碰权限）。末尾的 chmod 仍保留：文件已存在时
    os.open 的 mode 参数会被忽略，必须靠 chmod 兜底。
    """
    if not mode:
        return open(path, "w", encoding="utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, int(str(mode), 8))
    return os.fdopen(fd, "w", encoding="utf-8")


def set_output_mode(path: Path, cfg_pii: dict) -> None:
    chmod(path, cfg_pii.get("output_file_mode", "600"))


def case_id_from_rel(rel: str | Path) -> str:
    """'奉化/20230728_008' -> '奉化__20230728_008'（与旧批次目录命名一致）。"""
    s = str(rel).strip().strip("/")
    if s.lower().endswith(".pdf"):
        s = s[:-4]
    s = re.sub(r"[\\/]+", "__", s)
    s = re.sub(r"\s+", "_", s)
    return re.sub(r"[^\w\u4e00-\u9fff.\-()]+", "_", s)


def stage_dir(run_root: Path, batch: str, stage: str) -> Path:
    p = Path(run_root) / batch / stage
    p.mkdir(parents=True, exist_ok=True)
    return p


def add_case_args(ap) -> None:
    """每个阶段都支持 --case / --case-file，批量跑法在各阶段一致。"""
    ap.add_argument("--case", action="append", help="只跑这些 case_id（可重复）")
    ap.add_argument("--case-file", default="", help="从文件读 case_id，一行一个")


def wanted_cases(case, case_file: str) -> set:
    want = list(case or [])
    if case_file:
        want += [x.strip() for x in Path(case_file).read_text(encoding="utf-8").splitlines()
                 if x.strip()]
    return {w for w in want if w}


# --------------------------------------------------------------- HTTP（无代理）
def clear_proxy() -> None:
    for k in PROXY_VARS:
        os.environ.pop(k, None)


def http_json(url: str, payload: dict, timeout: int, retries: int = 0,
              backoff: float = 5.0, auth_header: str | None = None) -> tuple[int, Any, str]:
    """POST JSON。返回 (status, parsed_or_None, err)。失败重试 retries 次。

    异常**不吞**：最后一次错误原文返回给调用方，由调用方决定怎么标失败。
    """
    clear_proxy()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdr = {"Content-Type": "application/json"}
    if auth_header:
        hdr["Authorization"] = auth_header
    last_err = ""
    status = 0
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                status = r.status
                try:
                    return status, json.loads(raw), ""
                except json.JSONDecodeError as e:
                    return status, None, f"响应不是 JSON: {e}"
        except urllib.error.HTTPError as e:
            status = e.code
            last_err = f"HTTPError {e.code}: {e.reason}"
        except Exception as e:                                   # noqa: BLE001
            status = 0
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    return status, None, last_err


def b64_file(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


# --------------------------------------------------------------- 裁剪安全（风险③）
def safe_crop(img, bbox: Iterable[float], pad: int = 0):
    """按 bbox 裁剪；框退化时**回退整页**，绝不抛异常、绝不静默丢页。

    旧产线的坑（已确认）：PIL.crop 遇到 right<=left 抛
    "Coordinate 'right' is less than 'left'"，异常被吞、--quiet 压掉、
    批次照样 touch .done —— 118 页只出 84 个产物。
    坏框两种形态：① 原始框退化；② 框整体越界，夹取到图幅后反而反演。
    所以必须「先夹取、再校验」。

    返回 (image, degraded_reason | None)。
    """
    W, H = img.size
    x1, y1, x2, y2 = [int(v) for v in list(bbox)[:4]]
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
        return img, {"bbox": [x1, y1, x2, y2], "clamped": [left, top, right, bottom],
                     "image": [W, H], "why": "；".join(why)}
    return img.crop((left, top, right, bottom)), None


# --------------------------------------------------------------- 日志
def log(msg: str, *, quiet: bool = False) -> None:
    """正常流水可被 --quiet 压掉；**异常与告警不许压**（调用方用 warn/err）。"""
    if not quiet:
        print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"[!] {msg}", file=sys.stderr, flush=True)


def err(msg: str) -> None:
    print(f"[x] {msg}", file=sys.stderr, flush=True)
