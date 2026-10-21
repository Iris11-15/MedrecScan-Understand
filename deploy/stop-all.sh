#!/usr/bin/env bash
# stop-all.sh —— 反向顺序停：webui(8090) -> vLLM(8110) -> Strata(8080)。
# 只按 pidfile / 端口监听者定位进程，不用 pkill（pkill -f 会匹配到自己那条命令）。
set -euo pipefail

MEDREC_ROOT="${MEDREC_ROOT:-$HOME/medrec}"
LOGD="${LOG_DIR:-$MEDREC_ROOT/logs}"
P_STRATA="${PORT_STRATA:-8080}"
P_VLLM="${PORT_OCR_VL:-8110}"
P_WEB="${PORT_WEBUI:-8090}"
GRACE="${STOP_GRACE:-30}"   # PaddleX/vLLM 收 SIGTERM 后可能不退出，超时升 SIGKILL

pidfile() { printf "%s" "$LOGD/$1.pid"; }
listener_pid() { ss -lntpH "sport = :$1" 2>/dev/null | sed -n "s/.*pid=\([0-9]*\).*/\1/p" | head -1; }

stop_one() { # $1=名称 $2=端口 $3=pidfile 名
  local n="$1" p="$2" k="$3" pid i
  pid=""
  if [ -f "$(pidfile "$k")" ]; then pid="$(cat "$(pidfile "$k")" || true)"; fi
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then pid="$(listener_pid "$p")"; fi
  if [ -z "$pid" ]; then printf "[SKIP] %s 没在跑（端口 %s 无监听）\n" "$n" "$p"; return 0; fi
  printf "[..] 停 %s pid=%s（SIGTERM，最多等 %ss）\n" "$n" "$pid" "$GRACE"
  kill -TERM "$pid" 2>/dev/null || true
  for ((i=0; i<GRACE; i++)); do
    kill -0 "$pid" 2>/dev/null || { printf "[OK] %s 已退出（%ss）\n" "$n" "$i"; rm -f "$(pidfile "$k")" 2>/dev/null || true; return 0; }
    sleep 1
  done
  printf "[..] %s 收到 SIGTERM 后没退出，升 SIGKILL\n" "$n"
  kill -KILL "$pid" 2>/dev/null || true
  for ((i=0; i<10; i++)); do
    kill -0 "$pid" 2>/dev/null || { printf "[OK] %s 已 SIGKILL\n" "$n"; rm -f "$(pidfile "$k")" 2>/dev/null || true; return 0; }
    sleep 1
  done
  printf "[FAIL] %s pid=%s 停不掉（显存可能没释放，后续起服务会 Engine core initialization failed）\n" "$n" "$pid" >&2
  return 1
}

fails=0
stop_one webui "$P_WEB"   webui   || fails=$((fails+1))
stop_one vLLM  "$P_VLLM" vllm    || fails=$((fails+1))
stop_one Strata "$P_STRATA" strata || fails=$((fails+1))

nvidia-smi --query-gpu=index,memory.used --format=csv,noheader 2>/dev/null || true
[ "$fails" = 0 ] || { printf "stop-all: %d 个服务没停干净\n" "$fails" >&2; exit 1; }
