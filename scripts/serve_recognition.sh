#!/usr/bin/env bash
set -euo pipefail
ENVDIR="${CONDA_ENV_DIR:-/home/ubuntu/anaconda3/envs/vl-env}"
PY="$ENVDIR/bin/python"
[ -x "$PY" ] || { echo "找不到 $PY"; exit 1; }
export CONDA_PREFIX="$ENVDIR"
export PATH="$ENVDIR/bin:$PATH"
export LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8 PYTHONUTF8=1
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
unset PYTHONPATH PYTHONHOME http_proxy https_proxy HTTP_PROXY HTTPS_PROXY 2>/dev/null || true
echo "解释器: $PY"; "$PY" -c "import sys;print('环境根:',sys.prefix)"
exec "$PY" -m paddlex --serve --pipeline "${PIPELINE:-PaddleOCR-VL-1.6}" \
  --host "${HOST:-127.0.0.1}" --port "${PORT:-8100}" --device "${DEVICE:-gpu:0}"
