#!/usr/bin/env bash
# =====================================================================
#  start-ovis.sh — OvisOCR2（视觉语言 OCR）@ 8110  [GPU 0]
#
#  替代原来的 PaddleOCR-VL(vLLM) 通道：接口形态一致
#  （POST /v1/chat/completions + image_url base64 + "OCR:" 提示词），
#  所以 layered.py --vl-url http://127.0.0.1:8110 不用改。
#
#  用法： bash start-ovis.sh [start|stop|status|warm|smoke]
#    smoke  拿一页真图跑一遍（默认用奉化那张已知答案的护理单）
# =====================================================================
set -uo pipefail

VLLM=/home/ubuntu/anaconda3/envs/qwen-env/bin/vllm
MODEL=/home/ubuntu/medrec/models/OvisOCR2/snapshots/master
NAME=PaddleOCR-VL
PORT=8110
GPU=0
RUN=/home/ubuntu/medrec/run
LOGDIR=/home/ubuntu/medrec/logs
LOG=$LOGDIR/ovis.log
PIDF=$RUN/ovis.pid
PY=/home/ubuntu/anaconda3/envs/vl-env/bin/python
IMG=${IMG:-/home/ubuntu/medrec/jobs/batch_0928_1538/pages/奉化__20230728_009/page_0004.png}

mkdir -p "$RUN" "$LOGDIR"

port_up() { curl -sf -m 3 "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; }

case "${1:-start}" in

stop)
  if [ -f "$PIDF" ]; then
    pid="$(cat "$PIDF")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "停 $NAME (pid=$pid)"; kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDF"
  fi
  pids="$(ss -ltnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p {print $NF}' \
          | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)"
  [ -n "$pids" ] && echo "$pids" | xargs -r kill 2>/dev/null || true
  sleep 3
  echo "--- 停完 ---"
  nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
  ;;

status)
  if port_up; then printf '  %s :%s  UP\n' "$NAME" "$PORT"
  else printf '  %s :%s  down\n' "$NAME" "$PORT"; fi
  nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
  ;;

warm)
  curl -s -m 600 "http://127.0.0.1:$PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$NAME\",\"messages\":[{\"role\":\"user\",\"content\":\"回复：就绪\"}],\"max_tokens\":16,\"temperature\":0}" \
    | head -c 300
  echo
  ;;

smoke)
  [ -f "$IMG" ] || { echo "✗ 找不到图 $IMG（可用 IMG=... 指定）"; exit 1; }
  echo "图：$IMG"
  "$PY" - "$IMG" <<'PY'
import base64, json, sys, urllib.request
img = sys.argv[1]
b = base64.b64encode(open(img, "rb").read()).decode()
body = json.dumps({"model": "OvisOCR2", "temperature": 0, "max_tokens": 1024,
    "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b}},
        {"type": "text", "text": "OCR:"}]}]}).encode()
req = urllib.request.Request("http://127.0.0.1:8110/v1/chat/completions",
                             data=body, headers={"Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(req, timeout=600).read().decode()[:1200])
except Exception as e:
    print("调用失败:", type(e).__name__, e)
PY
  echo
  echo "对照（p4 护理单首行应有）：36.7 / 64 / 18 / 128-66"
  ;;

start)
  if port_up; then echo "8110 已在跑（要重启先 stop）"; exit 0; fi
  [ -x "$VLLM" ] || { echo "✗ 找不到 $VLLM"; exit 1; }
  [ -d "$MODEL" ] || { echo "✗ 找不到权重 $MODEL"; exit 1; }
  : > "$LOG"

  setsid env -u http_proxy -u https_proxy -u all_proxy \
             -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    CUDA_VISIBLE_DEVICES="$GPU" \
    PATH=/home/ubuntu/anaconda3/envs/qwen-env/bin:/usr/local/bin:/usr/bin:/bin \
    VLLM_USE_FLASHINFER_SAMPLER=0 \
    "$VLLM" serve "$MODEL" \
      --served-model-name "$NAME" \
      --host 127.0.0.1 --port "$PORT" \
      --gpu-memory-utilization 0.30 \
      --max-model-len 32768 \
      --limit-mm-per-prompt '{"image":1}' \
      --enforce-eager \
      --attention-backend TRITON_ATTN \
      >>"$LOG" 2>&1 &
  LP=$!
  echo "$LP" > "$PIDF"
  echo "pid=$LP  日志=$LOG  等待就绪…"

  for _ in $(seq 1 60); do
    if port_up; then
      echo "✓ 就绪：http://127.0.0.1:$PORT/v1"
      nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
      exit 0
    fi
    if ! kill -0 "$LP" 2>/dev/null; then
      echo "✗ 进程已退出，日志尾部："; tail -30 "$LOG"; exit 1
    fi
    sleep 5
  done
  echo "⚠ 5 分钟未就绪，日志尾部："; tail -20 "$LOG"
  ;;

*)
  echo "用法： bash $0 {start|stop|status|warm|smoke}"; exit 2 ;;
esac
