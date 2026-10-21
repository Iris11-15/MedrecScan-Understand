#!/usr/bin/env bash
# =====================================================================
#  12-start-laya.sh — 起 Laya 打分前端（NIHSS 打分 + 病历快判）
#
#  Laya = 非自回归决策引擎（421M / 322M 编码器，一次前向出全部题目，33ms）
#  本脚本起的是本项目自己的微服务 laya_service.py（串行 + 锁 + 置信度兜底），
#  而不是 laya 自带的 laya-serve —— 因为我们需要"量表题面 + 分数归一 + 升级判据"。
#
#  用法： bash 12-start-laya.sh [start|stop|status|selftest]
# =====================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/config.env"
medrec_clean_path
medrec_mkdirs

SERVICE="$MEDREC_SRC/laya_service.py"
FORMS="$MEDREC_SRC/laya_forms.json"
LOG="$MEDREC_LOGS/laya.log"
PIDF="$MEDREC_RUN/laya.pid"
ACTION="${1:-start}"

port_used() { local o; o=$(ss -ltn 2>/dev/null); printf '%s\\n' "$o" | grep -q ":$PORT_LAYAD "; }

# 把交付物装到固定路径（服务只认 $MEDREC_SRC 下的副本）
install_files() {
  [ -f "$SERVICE" ] || { cp -f "$HERE/laya_service.py" "$SERVICE"; medrec_log "装入 $SERVICE"; }
  [ -f "$FORMS" ]   || { cp -f "$HERE/laya_forms.json" "$FORMS";  medrec_log "装入 $FORMS"; }
  [ -f "$FORMS" ] || medrec_die "缺 laya_forms.json"
  "$PY_LAYAD" -c "import json,sys;json.load(open(sys.argv[1],encoding='utf-8'))" "$FORMS" \
    || medrec_die "laya_forms.json 不是合法 JSON"
}

case "$ACTION" in

stop)
  if [ -f "$PIDF" ]; then
    pid="$(cat "$PIDF")"
    kill -0 "$pid" 2>/dev/null && { medrec_log "停 Laya (pid=$pid)"; kill "$pid"; sleep 3; kill -9 "$pid" 2>/dev/null || true; }
    rm -f "$PIDF"
  fi
  pids="$(ss -ltnp 2>/dev/null | awk -v p=":$PORT_LAYAD" '$4 ~ p {print $NF}' \
          | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)"
  [ -n "$pids" ] && echo "$pids" | xargs -r kill 2>/dev/null || true
  ;;

status)
  printf "  Laya :%s  " "$PORT_LAYAD"
  port_used && echo UP || echo down
  curl -sf -m 5 "http://127.0.0.1:$PORT_LAYAD/health" 2>/dev/null | head -c 400 || true
  echo
  ;;

selftest)
  install_files
  medrec_log "量表自检（不连服务）"
  "$PY_LLM" "$HERE/22-score-pipeline.py" --selftest || medrec_die "量表自检未通过"
  ;;

start)
  [ -x "$PY_LAYAD" ] || medrec_die "laya-env 不存在，先跑 01-setup-envs.sh"
  install_files
  port_used && { medrec_log "端口 $PORT_LAYAD 已在监听"; exit 0; }

  # 第一次启动会把检查点拉到 HF 缓存（路由用的 multilingual + typed-decisions）
  export HF_HOME="$MEDREC_MODELS"
  setsid env PATH="$ENV_LAYAD/bin:$PATH" \
       CUDA_VISIBLE_DEVICES="$GPU_LAYAD" \
       LAYA_DEVICE="${LAYA_DEVICE:-cuda}" \
       LAYA_PRELOAD="${LAYA_PRELOAD:-1}" \
       LAYA_ROUTER_MODEL="$LAYA_ROUTER_MODEL" \
       LAYA_MAX_LEN="${LAYA_MAX_LEN:-8192}" \
       LAYAD_CONF_MIN="$LAYAD_CONF_MIN" \
    "$PY_LAYAD" "$SERVICE" --host 127.0.0.1 --port "$PORT_LAYAD" \
    >>"$LOG" 2>&1 &
  echo $! > "$PIDF"
  medrec_log "pid=$(cat "$PIDF")  日志：$LOG"

  medrec_log "等待就绪（首次要下/加载检查点，CPU 约 10s，含下载则几分钟）…"
  for i in $(seq 1 300); do
    if curl -sf -m 3 "http://127.0.0.1:$PORT_LAYAD/health" >/dev/null 2>&1; then
      medrec_log "✓ Laya 就绪：http://127.0.0.1:$PORT_LAYAD"
      echo "    试一发打分："
      curl -s -m 120 "http://127.0.0.1:$PORT_LAYAD/score" -H 'Content-Type: application/json' \
        -d '{"text":"神志清，双侧瞳孔等大等圆，四肢肌力5级，NIHSS评分0分。","scale":"nihss_15","items":["1a_意识水平","5_上肢运动"]}' \
        | head -c 500 || true
      echo
      exit 0
    fi
    kill -0 "$(cat "$PIDF")" 2>/dev/null || { tail -30 "$LOG"; medrec_die "Laya 进程已退出"; }
    sleep 2
  done
  tail -30 "$LOG"; medrec_die "Laya 10 分钟未就绪"
  ;;

*) medrec_die "用法：$0 {start|stop|status|selftest}" ;;
esac
