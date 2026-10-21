#!/usr/bin/env bash
# start-all.sh —— 起序 Strata(8080) -> vLLM(8110) -> webui(8090)，每个等健康检查通过再起下一个。
# 已在跑的服务直接跳过（幂等）。停止用 stop-all.sh（反向顺序）。
set -euo pipefail

MEDREC_ROOT="${MEDREC_ROOT:-$HOME/medrec}"
STRATA_HOME="${STRATA_HOME:-$HOME/Strata}"
# round54 P1-4：档位文件名必须和 install.sh 同一个算法（install.sh:35 strata_cfg_path
# = 档位名整串小写）。写死 strata-iq3_s.json ⇒ QUICKSTART 里 STRATA_MODEL=IQ3_XXS
# 装完必挂在「找不到配置文件」。
STRATA_MODEL="${STRATA_MODEL:-IQ3_S}"
STRATA_CFG="${STRATA_CFG:-$STRATA_HOME/strata-$(printf "%s" "$STRATA_MODEL" | tr "A-Z" "a-z").json}"
# round54 P1-4：conda 位置必须动态发现（install.sh 用 conda info --base）。
# 写死 /home/ubuntu/anaconda3 ⇒ 目标机 conda 换个位置，三个服务全起不来。
CONDA_BASE_RESOLVED="$(conda info --base 2>/dev/null || true)"
[ -n "$CONDA_BASE_RESOLVED" ] || CONDA_BASE_RESOLVED="${CONDA_BASE:-$HOME/anaconda3}"
PY="${MEDREC_PY:-$CONDA_BASE_RESOLVED/envs/vl-env/bin/python}"
PY_VLLM="${PY_VLLM:-$CONDA_BASE_RESOLVED/envs/qwen-env/bin/python}"
OCR_MODEL="${OCR_MODEL:-$MEDREC_ROOT/models/models/PaddlePaddle--PaddleOCR-VL-1.6/snapshots/master}"
LOGD="${LOG_DIR:-$MEDREC_ROOT/logs}"

P_STRATA="${PORT_STRATA:-8080}"
P_VLLM="${PORT_OCR_VL:-8110}"
P_WEB="${PORT_WEBUI:-8090}"

[ -x "$PY" ] || { echo "[FAIL] 找不到 vl-env 的 python：$PY（conda base=$CONDA_BASE_RESOLVED）" >&2
                   echo "       补救：MEDREC_PY=/你的/conda/envs/vl-env/bin/python bash start-all.sh" >&2
                   exit 1; }

# —— brief 3.5 陷阱修正后的默认值（2xL40 拓扑：卡0=OCR，卡1=Strata）——
OCR_GPU="${OCR_VLLM_GPU:-0}"
GMU="${OCR_VLLM_GMU:-0.22}"
MAXSEQS="${OCR_MAX_NUM_SEQS:-64}"
MAXBATCHED="${OCR_MAX_NUM_BATCHED_TOKENS:-16384}"
MAXLEN="${OCR_MAX_MODEL_LEN:-8192}"

STRATA_WAIT="${STRATA_WAIT:-1500}"   # Strata 冷启动实测 ~556 s（46.8 GiB 专家 @3.85 GiB/s）
VLLM_WAIT="${VLLM_WAIT:-600}"
WEB_WAIT="${WEB_WAIT:-60}"

# DRY_RUN=1 —— 只打印将要执行的命令，不起任何进程、不等健康检查（给验收取证用）
DRY_RUN="${DRY_RUN:-0}"

mkdir -p "$LOGD"

launch() { # $1=工作目录 $2=日志文件 其余=argv
  local wd="$1" log="$2"; shift 2
  if [ "$DRY_RUN" = 1 ]; then
    printf "[DRY] (cd %s && setsid nohup" "$wd"
    printf " %q" "$@"
    printf " >> %s 2>&1 < /dev/null &)\\n" "$log"
    return 0
  fi
  ( cd "$wd" && setsid nohup "$@" >> "$log" 2>&1 < /dev/null & )
}

alive_models() { local p="$1"; local b; b="$(curl -s --max-time 5 "http://127.0.0.1:${p}/v1/models" || true)"; [ -n "$b" ]; }
alive_http()     { local p="$1"; local c; c="$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:${p}/" || true)"; [ "$c" = 200 ] || [ "$c" = 302 ]; }

wait_for() { # $1=名称 $2=端口 $3=探测函数 $4=超时秒
  if [ "$DRY_RUN" = 1 ]; then printf "[DRY] 跳过等待 %s（端口 %s）\\n" "$1" "$2"; return 0; fi
  local n="$1" p="$2" f="$3" t="$4" e0; e0=$(date +%s)
  while ! "$f" "$p"; do
    if [ $(( $(date +%s) - e0 )) -ge "$t" ]; then
      printf "[FAIL] %s 在 %ss 内没起来（端口 %s）。日志：%s\n" "$n" "$t" "$p" "$LOGD" >&2
      return 1
    fi
    printf "  ... %s 等待中 %ss\n" "$n" "$(( $(date +%s) - e0 ))"
    sleep 10
  done
  printf "[OK] %s 就绪（端口 %s，用时 %ss）\n" "$n" "$p" "$(( $(date +%s) - e0 ))"
}

pidfile() { printf "%s" "$LOGD/$1.pid"; }
listener_pid() { ss -lntpH "sport = :$1" 2>/dev/null | sed -n "s/.*pid=\([0-9]*\).*/\1/p" | head -1; }
record_pid() { local p; p="$(listener_pid "$1")"; if [ -n "$p" ]; then printf "%s\n" "$p" > "$(pidfile "$2")"; chmod 600 "$(pidfile "$2")"; fi; }

echo "== 1/3 Strata ($P_STRATA) =="
if alive_models "$P_STRATA"; then
  echo "[SKIP] Strata 已在 $P_STRATA 上响应"
  record_pid "$P_STRATA" strata
else
  if [ ! -x "$STRATA_HOME/engine/strata" ] || [ ! -x "$STRATA_HOME/.venv/bin/python" ] || [ ! -f "$STRATA_CFG" ]; then
    echo "[FAIL] Strata 未安装：缺 $STRATA_HOME/engine/strata 或 .venv 或 $STRATA_CFG" >&2
    echo "       先跑 bash install.sh online —— 它的 strata 步骤从 GitHub 抓 Strata v0.1.40（引擎+权重+venv）" >&2
    echo "       已装在别处：STRATA_HOME=/your/path bash start-all.sh" >&2
    exit 1
  fi
  echo "[..] 启动 Strata：冷启动实测约 556 s，期间端口不响应是正常的，别以为卡死"
  launch "$STRATA_HOME" "$LOGD/strata.log" "$STRATA_HOME/.venv/bin/python" serve/server.py --engine strata --config "$STRATA_CFG" --port "$P_STRATA"
  wait_for Strata "$P_STRATA" alive_models "$STRATA_WAIT"
  record_pid "$P_STRATA" strata
fi

echo "== 2/3 vLLM OCR ($P_VLLM) =="
if alive_models "$P_VLLM"; then
  echo "[SKIP] vLLM 已在 $P_VLLM 上响应"
  record_pid "$P_VLLM" vllm
else
  if [ ! -d "$OCR_MODEL" ]; then
    echo "[FAIL] OCR 模型目录不存在：$OCR_MODEL" >&2
    echo "       vLLM 会报 HFValidationError: Repo id must be in the form... —— 这个报错等于「目录不存在」。" >&2
    echo "       路径必须指到 .../models/PaddlePaddle--PaddleOCR-VL-1.6/snapshots/master 这一层（brief 3.2）。" >&2
    exit 1
  fi
  if [ ! -f "$OCR_MODEL/config.json" ]; then
    echo "[FAIL] $OCR_MODEL 里没有 config.json —— 十有八九是指到了上一级目录（见 brief 3.2）" >&2
    exit 1
  fi
  VLLM_BIN="${VLLM_BIN:-$CONDA_BASE_RESOLVED/envs/qwen-env/bin/vllm}"
  [ -x "$VLLM_BIN" ] || { echo "[FAIL] 找不到 $VLLM_BIN（qwen-env 的 vllm 没装）" >&2; exit 1; }
  echo "[..] 启动 vLLM：GPU=$OCR_GPU gmu=$GMU max-num-seqs=$MAXSEQS max-num-batched-tokens=$MAXBATCHED"
  launch "$MEDREC_ROOT" "$LOGD/ocr_vllm.log" env "CUDA_VISIBLE_DEVICES=$OCR_GPU" "$PY_VLLM" "$VLLM_BIN" serve "$OCR_MODEL" \
    --served-model-name PaddleOCR-VL --trust-remote-code \
    --host 127.0.0.1 --port "$P_VLLM" --tensor-parallel-size 1 \
    --gpu-memory-utilization "$GMU" --max-model-len "$MAXLEN" \
    --limit-mm-per-prompt "{\"image\":1}" --attention-backend FLASH_ATTN \
    --compilation-config "{\"mode\":0,\"cudagraph_mode\":\"full\"}" \
    --max-num-seqs "$MAXSEQS" --no-enable-prefix-caching \
    --max-num-batched-tokens "$MAXBATCHED"
  wait_for vLLM "$P_VLLM" alive_models "$VLLM_WAIT"
  record_pid "$P_VLLM" vllm
fi

echo "== 3/3 webui ($P_WEB) =="
if alive_http "$P_WEB"; then
  echo "[SKIP] webui 已在 $P_WEB 上响应"
  record_pid "$P_WEB" webui
else
  [ -d "$MEDREC_ROOT/webui/static" ] || { echo "[FAIL] 找不到 $MEDREC_ROOT/webui/static —— server.py 用相对路径找 static/，必须 cd 进 webui 再起" >&2; exit 1; }
  # round54 P0-3：交付默认禁掉一切明文出口（含 /api/raw 逐格明文）。
  # 要临时看明文：WEBUI_DISABLE_PLAIN=0 bash start-all.sh（每次访问都进 _audit.jsonl）。
  WEBUI_ARGS=()
  if [ "${WEBUI_DISABLE_PLAIN:-1}" = "1" ]; then
    WEBUI_ARGS+=(--disable-plain)
    echo "[..] webui 以 --disable-plain 启动：明文出口全关（含 /api/raw）"
  else
    echo "[WARN] WEBUI_DISABLE_PLAIN=0 ⇒ 明文出口开启，任何能访问 $P_WEB 的人都能拖走全表 PII"
  fi
  launch "$MEDREC_ROOT/webui" "$LOGD/webui.log" "$PY" server.py ${WEBUI_ARGS[@]+"${WEBUI_ARGS[@]}"}
  wait_for webui "$P_WEB" alive_http "$WEB_WAIT"
  record_pid "$P_WEB" webui
fi

# —— 绑址自检：只允许 127.0.0.1，出现 0.0.0.0 直接判失败（红线7）——
bad="$(ss -lntH 2>/dev/null | awk '{print $4}' | grep -E "^(0\\.0\\.0\\.0|\\*):($P_STRATA|$P_VLLM|$P_WEB)$" || true)"
if [ -n "$bad" ]; then
  echo "[FAIL] 有服务绑到了非环回地址（红线7）：$bad" >&2
  exit 1
fi

if [ "$DRY_RUN" = 1 ]; then
  printf "[DRY] 结束：没有启动任何进程，也没跑 health.sh\\n"
elif [ -x "$(dirname "$0")/health.sh" ]; then "$(dirname "$0")/health.sh" || exit 1; fi
