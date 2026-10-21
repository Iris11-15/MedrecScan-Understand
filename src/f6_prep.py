#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
F1/F2 -> F6 的桥：从 compiled.md 里裁出神经系统查体段。
规则（13/13 例实测）：起点 = 「专科检查」标题；
                     终点 = 起点之后第一个标题中不以「颅神经」开头的那个。
两种模板都吃：
  普通入院记录   ## 专科检查： -> ## 颅神经： -> ## 体格检查表（二）
  转科/转院记录  ## 体格检查表（二） -> ## 专科检查： -> ## 颅神经： -> ## 诊断：
用法: python3 f6_prep.py <compiled 目录 或 单个 compiled.md> [输出目录]
"""
import re
import sys
from pathlib import Path

START = re.compile(r'^#{1,4}[ \t]*专科检查', re.M)
HEAD = re.compile(r'^#{1,4}[ \t]*(.+)$', re.M)
SKIP_PREFIX = ('颅神经',)
MAX_CHARS = 4000


def extract(text):
    m = START.search(text)
    if not m:
        return None, None
    for h in HEAD.finditer(text, m.end()):
        if h.group(1).strip().startswith(SKIP_PREFIX):
            continue
        return text[m.end():h.start()].strip(), h.group(1).strip()
    return text[m.end():].strip(), 'EOF'


def main():
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.parent / 'sections'
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(src.glob('*/compiled.md')) if src.is_dir() else [src]
    for f in files:
        case = f.parent.name if src.is_dir() else f.stem
        seg, stop = extract(f.read_text(encoding='utf-8', errors='replace'))
        if seg is None:
            print(f'  {case:30} 找不到「专科检查」')
            continue
        (out / (case + '.txt')).write_text(seg, encoding='utf-8')
        warn = ''
        if len(seg) > MAX_CHARS:
            warn = f'   <== 超 {MAX_CHARS} 字，终点可能不对'
        elif len(seg) < 200:
            warn = '   <== 太短，终点可能不对'
        print(f'  {case:30} {len(seg):>5} 字   止于「{stop}」{warn}')
    print(f'\n输出目录: {out}')


if __name__ == '__main__':
    main()
