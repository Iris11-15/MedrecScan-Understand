# -*- coding: utf-8 -*-
"""中文安全 I/O：全项目唯一的读写入口。

设计要点（见 encoding-policy.md）：
  * 所有文本一律 UTF-8、无 BOM、LF 行尾；
  * errors="strict"：宁可报错，绝不静默吞掉坏字节；
  * JSON 一律 ensure_ascii=False，中文保持可读原样。
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any

# OCR 换行连字 / 零宽字符 / BOM 残留 / 双向控制符，一律视为噪声
INVISIBLE = {0x00AD, 0x200B, 0x200C, 0x200D, 0xFEFF, 0x200E, 0x200F}
# 需要半角化或替换的标点
PUNCT_MAP = {
    "\u00A0": " ",       # 不换行空格
    "\u2018": "'", "\u2019": "'",
    "\u201C": "\"", "\u201D": "\"",
    "\uFF5E": "~",
    "\u3000": " ",       # 全角空格
}

CJK_LO, CJK_HI = 0x4E00, 0x9FFF


def read_text(p: str | Path) -> str:
    """读文本。strict：遇到非法 UTF-8 直接抛错。"""
    return Path(p).read_text(encoding="utf-8", errors="strict")


def write_text(p: str | Path, s: str) -> None:
    """写文本。统一 UTF-8 无 BOM + LF。"""
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)


def load_json(p: str | Path) -> Any:
    return json.loads(read_text(p))


def dump_json(p: str | Path, obj: Any, indent: int = 2) -> None:
    write_text(p, json.dumps(obj, ensure_ascii=False, indent=indent))


def normalize(s: str) -> str:
    """NFC 归一 + 去隐形字符 + 标点半角化。中文入库前过一遍。"""
    if not s:
        return s
    s = unicodedata.normalize("NFC", s)
    out = []
    for ch in s:
        if ord(ch) in INVISIBLE:
            continue
        out.append(PUNCT_MAP.get(ch, ch))
    return "".join(out)


def strip_control(s: str) -> str:
    """只去隐形字符，不动标点（用于不想改变原文形态的场景）。"""
    return "".join(ch for ch in s if ord(ch) not in INVISIBLE)


def count_cjk(s: str) -> int:
    return sum(1 for ch in s if CJK_LO <= ord(ch) <= CJK_HI)


def text_metrics(s: str) -> dict:
    """中文链路三判据：长度 / 汉字数 / sha 前 16 位（+U+FFFD 计数）。"""
    b = s.encode("utf-8")
    return {
        "len": len(s),
        "bytes": len(b),
        "cjk": count_cjk(s),
        "sha": hashlib.sha256(b).hexdigest()[:16],
        "broken": s.count("\ufffd"),
    }
