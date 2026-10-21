#!/usr/bin/env python3
"""medrec 阶段垫片 —— 只干一件事：把 paths.run_root 换成 $MEDREC_RUN_ROOT。

产线各阶段（wide.s1_ocr / wide.s3_extract / ...）都从 config/pipeline.yaml 读
paths.run_root，且**没有**环境变量覆盖（src/wide/common.py 里没有 run_root 的 env 读取）。
`medrec run --run-root DIR` 因此必须让每个阶段子进程也看到同一个 run_root，
否则 run_steps 记的是 DIR、阶段实际写进 run/wide/ —— 两边对不上。

用法: medrec_stage.py <module> [该阶段原来的所有命令行参数...]
只读产线代码，不改任何仓库文件。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import wide.common as C  # noqa: E402

RUN_ROOT = os.environ.get("MEDREC_RUN_ROOT", "").strip()
if RUN_ROOT:
    _orig_load = C.load_pipeline

    def load_pipeline(path=None):  # noqa: D103
        cfg = _orig_load(path)
        cfg["paths"]["run_root"] = RUN_ROOT
        return cfg

    C.load_pipeline = load_pipeline

if len(sys.argv) < 2:
    sys.exit("usage: medrec_stage.py <module> [args...]")

mod = importlib.import_module(sys.argv[1])
sys.argv = [sys.argv[0]] + sys.argv[2:]
raise SystemExit(getattr(mod, "main")())
