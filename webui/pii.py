"""PII 打码（round37 F4）—— 做在**服务端**，页面与 API 一律不返回原始 PII。

规则（任务书 §四 F4 + config/pipeline.yaml pii:）：
  身份证号 → 前 3 后 2；电话（本人/联系人）→ 中间掩 *；姓名 → 首字 + *；
  地址 → 只到「市/区」一级。
另外 case 目录名里嵌着患者姓名（`208895-***-20240723` 形态，round36 实测 247 处/趟）
⇒ 日志/文本一律过 scrub_text()。

⚠ 这是**网页侧兜底**，产线侧（bin/medrec run 把 case 目录名原样打进 stdout）未修。
"""
from __future__ import annotations

import re
from pathlib import Path

COLUMNS_YAML = Path("/home/ubuntu/medrec/config/columns.yaml")

# config/pipeline.yaml: pii.mask_id_card = {keep_head: 3, keep_tail: 2}
ID_KEEP_HEAD, ID_KEEP_TAIL = 3, 2
PHONE_KEEP_HEAD, PHONE_KEEP_TAIL = 3, 2

ID_CARD_RE = re.compile(r"(?<![0-9Xx])[0-9]{17}[0-9Xx](?![0-9Xx])")
LONG_DIGITS_RE = re.compile(r"(?<![0-9])[0-9]{11,}(?![0-9])")
PHONE_RE = re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")
# 目录名/case_id 里的姓名段：208895-陈岩根-20240723
NAME_IN_DIR_RE = re.compile(r"(?<=-)[\u4e00-\u9fff]{2,4}(?=-20[0-9]{6})")

NAME_COLS = {"patient_name", "patient_name_dup", "contact_name", "姓名", "联系人姓名"}
ID_COLS = {"id_card", "身份证号"}
PHONE_COLS = {"patient_phone", "contact_phone", "本人电话号码", "联系人电话", "电话"}
ADDR_COLS = {"address", "地址", "native_place", "籍贯"}


def pii_columns(path: Path = COLUMNS_YAML) -> dict:
    """从 config/columns.yaml 读 pii: true 的列（只读，行扫描，不依赖 yaml 库）。"""
    ids, headers, cur = set(), set(), None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("- id:"):
                cur = s.split(":", 1)[1].strip()
            elif s.startswith("header:") and cur:
                h = s.split(":", 1)[1].strip()
                if h:
                    ids.add(cur)
                    headers.add(h)
            elif s.startswith("pii:") and cur:
                if s.split(":", 1)[1].strip().lower() == "true":
                    ids.add(cur)
                    headers.add(cur)
    except OSError:
        pass
    return {"ids": ids, "headers": headers}


def _keep_mask(s: str, head: int, tail: int) -> str:
    if not s:
        return s
    if len(s) <= head + tail:
        return s[0] + "*" * (len(s) - 1)
    return s[:head] + "*" * (len(s) - head - tail) + s[-tail:]


def mask_id_card(v: str) -> str:
    return _keep_mask(str(v).strip(), ID_KEEP_HEAD, ID_KEEP_TAIL)


def mask_phone(v: str) -> str:
    s = re.sub(r"[^0-9]", "", str(v))
    return _keep_mask(s, PHONE_KEEP_HEAD, PHONE_KEEP_TAIL) if s else str(v)


def mask_name(v: str) -> str:
    s = str(v).strip()
    if not s:
        return s
    return s[0] + "*" * max(1, len(s) - 1)


def mask_address(v: str) -> str:
    s = str(v).strip()
    m = re.search(r"[^,，\s]{0,14}?(?:市|区|县|旗)", s)
    if m:
        return m.group(0)
    return s[:2] + "***" if len(s) > 2 else s


def mask_value(col: str, value) -> str:
    """按列名（id 或表头）打码单个单元格值。"""
    if value is None:
        return ""
    s = str(value)
    if col in ID_COLS:
        return mask_id_card(s)
    if col in PHONE_COLS:
        return mask_phone(s)
    if col in NAME_COLS:
        return mask_name(s)
    if col in ADDR_COLS:
        return mask_address(s)
    return scrub_text(s)


def scrub_text(s: str) -> str:
    """通用文本打码（日志尾部、自由文本、审计 detail）。"""
    if not s:
        return s
    out = NAME_IN_DIR_RE.sub("***", str(s))
    out = ID_CARD_RE.sub(lambda m: mask_id_card(m.group(0)), out)
    out = PHONE_RE.sub(lambda m: mask_phone(m.group(0)), out)
    # 兜底：任何 ≥11 位连续数字（身份证 / 毫秒时间戳 / 带区号电话）都掩中间
    out = LONG_DIGITS_RE.sub(lambda m: _keep_mask(m.group(0), 3, 2), out)
    return out
