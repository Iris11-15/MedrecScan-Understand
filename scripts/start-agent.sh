#!/usr/bin/env bash
# =====================================================================
#  start-agent.sh — agent-strong（Qwen3.5-35B-A3B / GPTQ-Int4）@ 8102
#
#  这是 Codex 的主模型服务。参数是 2026-09-30 实测跑通的那一套，
#  改动任何一项前先读 ~/medrec/docs/interfaces.md 的 §1.2 参数要点。
#
#  用法： bash start-agent.sh [start|stop|status|warm]
#    start  幂等：端口已在听就直接退出（可挂 cron 当看门狗）
#    stop   只停 8102，不碰 8110/8111
#    status 判活 + KV 容量 + 卡占用
#    warm   打一发预热（首次按真实形状编译，可能等 1~3 分钟）
# =====================================================================
set -uo pipefail

VLLM=/home/ubuntu/anaconda3/envs/qwen-env/bin/vllm
MODEL=/home/ubuntu/medrec/models/Qwen3.5-35B-A3B-GPTQ-Int4/snapshots/master
NAME=agent-strong
PORT=8102
GPUS=1,2
RUN=/home/ubuntu/medrec/run
LOGDIR=/home/ubuntu/medrec/logs
LOG=$LOGDIR/agent.log
PIDF=$RUN/agent.pid

mkdir -p "$RUN" "$LOGDIR"

port_up() { curl -sf -m 3 "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; }

case "${1:-start}" in

stop)
  if [ -f "$PIDF" ]; then
    pid="$(cat "$PIDF")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "停 agent-strong (pid=$pid)"; kill "$pid" 2>/dev/null || true
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
  if port_up; then
    printf '  agent-strong :%s  UP\n' "$PORT"
    curl -s -m 5 "http://127.0.0.1:$PORT/v1/models" | head -c 220; echo
  else
    printf '  agent-strong :%s  down\n' "$PORT"
  fi
  grep -nE 'KV cache size|Maximum concurrency' "$LOG" 2>/dev/null | tail -2 || true
  nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
  ;;

warm)
  echo "预热中（首次按真实形状编译，1~3 分钟属正常）…"
  time curl -s -m 600 "http://127.0.0.1:$PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$NAME\",\"messages\":[{\"role\":\"user\",\"content\":\"回复：就绪\"}],\"max_tokens\":16,\"temperature\":0}" \
    | head -c 400
  echo
  ;;

start)
  if port_up; then echo "8102 已在跑（要重启先 stop）"; exit 0; fi
  [ -x "$VLLM" ] || { echo "✗ 找不到 $VLLM"; exit 1; }
  [ -d "$MODEL" ] || { echo "✗ 找不到权重 $MODEL"; exit 1; }
  : > "$LOG"

  setsid env -u http_proxy -u https_proxy -u all_proxy \
             -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    CUDA_VISIBLE_DEVICES="$GPUS" \
    PATH=/home/ubuntu/anaconda3/envs/qwen-env/bin:/usr/local/bin:/usr/bin:/bin \
    VLLM_USE_FLASHINFER_SAMPLER=0 \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    "$VLLM" serve "$MODEL" \
      --served-model-name "$NAME" \
      --host 127.0.0.1 --port "$PORT" \
      --tensor-parallel-size 2 \
      --gpu-memory-utilization 0.90 \
      --max-model-len 65536 \
      --attention-backend TRITON_ATTN \
      --enable-prefix-caching --max-num-seqs 4 \
      --enforce-eager \
      --enable-auto-tool-choice --tool-call-parser qwen3_coder \
      --reasoning-parser qwen3 \
      >>"$LOG" 2>&1 &
  LP=$!
  echo "$LP" > "$PIDF"
  echo "pid=$LP  日志=$LOG"
  echo "等待就绪（35B TP=2 首次加载 2~5 分钟）…"

  for _ in $(seq 1 60); do
    if port_up; then
      echo "✓ 就绪：http://127.0.0.1:$PORT/v1"
      grep -nE 'KV cache size|Maximum concurrency' "$LOG" | tail -2 || true
      nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
      exit 0
    fi
    if ! kill -0 "$LP" 2>/dev/null; then
      echo "✗ 进程已退出，日志尾部（真因常在 'Engine core initialization failed' 上面）："
      tail -30 "$LOG"
      exit 1
    fi
    sleep 5
  done
  echo "⚠ 5 分钟未就绪，日志尾部："; tail -20 "$LOG"
  ;;

*)
  echo "用法： bash $0 {start|stop|status|warm}"; exit 2 ;;
esac
