#!/usr/bin/env bash
set -uo pipefail
ENVDIR="${CONDA_ENV_DIR:-/home/ubuntu/anaconda3/envs/vl-env}"; PY="$ENVDIR/bin/python"
[ -x "$PY" ] || { echo "找不到解释器 $PY"; exit 1; }
export CONDA_PREFIX="$ENVDIR"; export PATH="$ENVDIR/bin:$PATH"
export LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8 PYTHONUTF8=1
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
unset PYTHONPATH PYTHONHOME http_proxy https_proxy HTTP_PROXY HTTPS_PROXY 2>/dev/null || true
GI="${GPU_INDEX:-0}"; NEED="${NEED_MIB:-20000}"
pgrep -af "paddlex --serve" && { echo "已有 paddlex 在跑，先 pkill -f 'paddlex --serve'"; exit 1; }
FREE=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits -i "$GI" | tr -d ' ')
echo "GPU $GI 空闲 ${FREE}MiB / 需要 ${NEED}MiB"
[ "${FREE:-0}" -ge "$NEED" ] || { nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader
  echo "✗ 显存不足：改用 MODE=cpu 或换 GPU_INDEX"; exit 1; }
export CUDA_VISIBLE_DEVICES="$GI"
echo "启动 PaddleOCR-VL-1.6 @ 127.0.0.1:8100 (物理GPU $GI)"
exec "$PY" -m paddlex --serve --pipeline PaddleOCR-VL-1.6 --host 127.0.0.1 --port 8100 --device gpu:0
