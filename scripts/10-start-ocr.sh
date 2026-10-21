#!/usr/bin/env bash
# =====================================================================
#  10-start-ocr.sh — OCR 前端识别（两条通道，二选一）
#
#    通道 A（默认） PaddleX 产线服务化部署    端口 $PORT_OCR_PADDLE
#    通道 B（可选） PaddleOCR-VL 权重走 vLLM  端口 $PORT_OCR_VL
#
#  通道 A 是本项目一直在用的原生产线，接口契约见下；layered.py 的
#  --vl-backend paddle 打的就是它。
#
#  ★ 2026-10-10（可复现轮·第 3 件）：启动路径末尾接**退化自检**。
#    /health 200 和 /v1/models 200 都不代表服务在 GPU 上干活 —— 实测两种
#    「活着但退化」：① 用 vl-env 起 PaddleX；② 同卡上再起第二个 PaddleX。
#    两种都是健康端点正常、推理退化到单线程 CPU（整页 12 min 不返回）。
#    自检 = 打一张合成 256×256 小图（无 PII）计时，超时/超阈值 → 非零退出 →
#    本脚本 medrec_die，不允许带着退化服务继续跑产线。
#    实现：src/wide/tools/ocr_healthcheck.py（0 正常/2 无响应/3 退化/4 空响应）
#
#  vLLM 通道支持多副本（每副本一个端口，各自自检一次）：
#      OCR_VLLM_PORTS="8110,8112" OCR_VLLM_GMU=0.35 bash 10-start-ocr.sh vllm
#
#  用法： bash 10-start-ocr.sh paddle    # 起 A 路（起完自检）
#         bash 10-start-ocr.sh vllm      # 起 B 路（起完每个副本各自检一次）
#         bash 10-start-ocr.sh check     # 只自检：在跑的 paddlex + 每个 vLLM 副本
#         bash 10-start-ocr.sh stop      # 停两路（含全部 vLLM 副本）
#         bash 10-start-ocr.sh status
# =====================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/config.env"
medrec_clean_path
medrec_mkdirs

MODE="${1:-paddle}"
LOG_A="$MEDREC_LOGS/ocr_paddle.log"
LOG_B="$MEDREC_LOGS/ocr_vllm.log"
PID_A="$MEDREC_RUN/ocr_paddle.pid"
PID_B="$MEDREC_RUN/ocr_vllm.pid"

# 自检用产线 Python（vl-env）。config.env 没有这个 env；别拿 PY_OCR（那是 ocr-env，
# 只是恰好也能 import wide）—— 产线代码一律 vl-env，见 AGENTS.md §二。
PY_WIDE="${PY_WIDE:-$CONDA_BASE/envs/vl-env/bin/python}"
# OCR_SELFCHK=0 只用于排障，正常部署不要关。
SELFCHK="${OCR_SELFCHK:-1}"
SELFCHK_MAX_S="${OCR_SELFCHK_MAX_S:-}"
# vLLM 副本：默认单副本（向后兼容）。两副本交付配置见 AGENTS.md §一。
VLLM_PORTS="${OCR_VLLM_PORTS:-$PORT_OCR_VL}"
# ★ 默认 0.7（不是 config.env 的 VL_GMU=0.20）：0.20 是「与 Qwen 共卡」时代的小值。
#   round 7 起 GPU0 专职 OCR、Strata 在 GPU1/2/3 → 单副本应吃 0.7
#   （实测 GPU0 占 17376 MiB、Available KV cache 14.23 GiB，workspace/round7/t3_vllm_8110.log:77）。
#   要回到共卡小值：OCR_VLLM_GMU=0.20 bash $0 vllm
VLLM_GMU="${OCR_VLLM_GMU:-0.7}"
VLLM_GPU="${OCR_VLLM_GPU:-$GPU_VL_TP}"
# 2026-10-11 人的裁决①：放弃位级确定性，并发是交付姿势
#   （config/pipeline.yaml:103 require_batch_invariant: false → concurrency_gate() 直接
#   gate:off，不再走 vllm_single_seq_replicas() 的 --max-num-seqs 1 校验）。
#   因此这里从「确定性开关」改成吞吐参数。16 = round8 扫出的拐点：客户端 conc 1→16
#   25.09 → 8.48 s = 2.96×（conc 8 只有 1.41×，workspace/round8/ladder_r8cc.out）。
#   服务端 mns 若仍是 1，客户端并发只能盖掉 HTTP/编码间隙（~1.2×），买不到这 2.96×。
#   回滚到 round7 位级确定性交付配置：
#     OCR_VLLM_DETERMINISM="--max-num-seqs 1 --no-enable-prefix-caching" bash $0 vllm
VLLM_DETERMINISM="${OCR_VLLM_DETERMINISM:---max-num-seqs 16 --no-enable-prefix-caching}"

port_busy() { local o; o=$(ss -ltn 2>/dev/null); printf '%s\\n' "$o" | grep -q ":$1 "; }

stop_one() {  # stop_one <pidfile> <名字> <端口>
  local pf="$1" name="$2" port="$3"
  if [ -f "$pf" ]; then
    local pid; pid="$(cat "$pf")"
    if kill -0 "$pid" 2>/dev/null; then
      medrec_log "停 $name (pid=$pid)"; kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pf"
  fi
  # 兜底：按端口清残留
  local pids; pids="$(ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $NF}' \
                     | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)"
  [ -n "$pids" ] && { echo "$pids" | xargs -r kill 2>/dev/null || true; medrec_log "兜底清理端口 $port"; }
}

check_alive() {  # 服务起来后必须看到什么
  local url="$1" name="$2"
  medrec_log "等待 $name 就绪…"
  wait_http "$url" 150 || medrec_die "$name 未就绪，看日志：$LOG_A / $LOG_B"
}

selfcheck_ocr() {  # selfcheck_ocr <paddlex|vllm> <base_url>
  local backend="$1" url="$2" rc=0
  if [ "$SELFCHK" != "1" ]; then
    medrec_log "⚠ 跳过退化自检（OCR_SELFCHK=0）：$url —— 自负其责"
    return 0
  fi
  [ -x "$PY_WIDE" ] || medrec_die "自检需要产线 Python $PY_WIDE（vl-env），它不存在"
  medrec_log "退化自检 $backend $url …"
  # 先落 rc 再判断：set -e 下裸调用会在失败时直接退出，拿不到退出码就打不出原因
  ( cd "$MEDREC_SRC" 2>/dev/null || cd "$HOME"
    "$PY_WIDE" -u -m wide.tools.ocr_healthcheck --backend "$backend" --url "$url" \
      ${SELFCHK_MAX_S:+--max-s "$SELFCHK_MAX_S"} ) || rc=$?
  if [ "$rc" -ne 0 ]; then
    local why
    case "$rc" in
      2) why="服务无响应/结构异常" ;;
      3) why="推理退化（CPU 推理 / 环境不对 / 同卡争用）" ;;
      4) why="响应成功但内容为空" ;;
      *) why="自检异常退出" ;;
    esac
    medrec_log "✗ 自检未通过 rc=$rc（$why）：$url —— 健康端点可能是活的，但推理不可用"
    # 启动路径默认直接 die；check 模式设 SELFCHK_DIE=0 → 返回 rc，让调用方汇总所有副本
    [ "${SELFCHK_DIE:-1}" = "1" ] && medrec_die "拒绝继续：$url 退化自检未通过（rc=$rc）"
    return "$rc"
  fi
  medrec_log "✓ 自检通过：$backend $url"
}

case "$MODE" in

# ------------------------------------------------------------------ A 路
paddle)
  [ -x "$PY_OCR" ] || medrec_die "ocr-env 不存在，先跑 01-setup-envs.sh"
  # 端口预检必须在加载 1.8GB 权重之前失败，否则要等好几分钟才报错
  port_busy "$PORT_OCR_PADDLE" && medrec_die "端口 $PORT_OCR_PADDLE 已被占用（通道 B 在跑？）"
  # 服务化插件预检：paddlex 的服务化插件是**单独装**的，不是 pip extra。
  # 漏装时 paddlex --serve 会直接抛 DependencyError: The serving plugin is not available.
  if ! "$PY_OCR" -c "from paddlex.utils.deps import require_serving_plugin; require_serving_plugin()" >/dev/null 2>&1; then
    medrec_log "检测到 PaddleX 服务化插件缺失，自动安装…"
    "$PY_OCR" -m paddlex --install serving >>"$LOG_A" 2>&1 \
      || medrec_die "插件安装失败。手动执行： $PY_OCR -m paddlex --install serving"
  fi
  medrec_log "✓ serving 插件在位"
  nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader | sed 's/^/  GPU /'
  # ⚠ PaddleX 的 --device 用的是「可见卡编号」，不是物理卡号。
  #   CUDA_VISIBLE_DEVICES=3 会把那张卡重新编号为 0，所以这里必须恒为 gpu:0 ——
  #   写 gpu:$GPU_OCR_PADDLE 在 GPU_OCR_PADDLE=0 时看着对，一旦换卡就指向不存在的设备。
  VIS_DEV="gpu:0"
  medrec_log "启动 PaddleX 产线：$MODEL_OCR_PADDLE  物理卡 $GPU_OCR_PADDLE(→$VIS_DEV)  :$PORT_OCR_PADDLE"
  cd "$MEDREC_SRC" 2>/dev/null || cd "$HOME"
  # PID 显式捕获：`echo $! > file` 在 set -e 下失败会静默退出
  setsid env CUDA_VISIBLE_DEVICES="$GPU_OCR_PADDLE" \
    "$PY_OCR" -m paddlex --serve \
      --pipeline "$MODEL_OCR_PADDLE" \
      --host 127.0.0.1 --port "$PORT_OCR_PADDLE" \
      --device "$VIS_DEV" \
      >>"$LOG_A" 2>&1 &
  LAUNCH_PID=$!
  echo "$LAUNCH_PID" > "$PID_A" 2>/dev/null || true
  [ -n "${LAUNCH_PID:-}" ] && [ "$LAUNCH_PID" -gt 1 ] || medrec_die "PaddleX 进程没起来，看 $LOG_A"
  # PaddleX 的就绪标志：/layout-parsing 能回 errorCode
  # 注意先落变量再 grep —— `curl | grep -q` 在 pipefail 下会被 SIGPIPE(141) 误判为失败
  for i in $(seq 1 150); do
    PROBE="$(curl -s -m 3 -X POST "http://127.0.0.1:$PORT_OCR_PADDLE/layout-parsing" \
             -H 'Content-Type: application/json' -d '{}' 2>/dev/null)"
    if printf '%s' "$PROBE" | grep -q errorCode; then
      READY=1; break
    fi
    kill -0 "$(cat "$PID_A")" 2>/dev/null || { tail -30 "$LOG_A"; medrec_die "A 路进程已退出，见上"; }
    sleep 2
  done
  [ -n "${READY:-}" ] || { tail -30 "$LOG_A"; medrec_die "A 路 5 分钟未就绪"; }
  medrec_log "✓ A 路就绪：http://127.0.0.1:$PORT_OCR_PADDLE/layout-parsing"
  echo "    Swagger: http://127.0.0.1:$PORT_OCR_PADDLE/docs  （根路径 404 是正常的）"
  # ★ 就绪 ≠ 可用：必须再打一次真实小图（第 3 件）
  selfcheck_ocr paddlex "http://127.0.0.1:$PORT_OCR_PADDLE"
  ;;

# ------------------------------------------------------------------ B 路
vllm)
  [ -x "$PY_LLM" ] || medrec_die "qwen-env 不存在，先跑 01-setup-envs.sh"
  [ -d "$MODEL_OCR_VL_PATH" ] || medrec_die "缺权重 $MODEL_OCR_VL_PATH，先跑 02-download-models.sh ocr"
  IFS=',' read -r -a VPORTS <<< "$VLLM_PORTS"
  for p in "${VPORTS[@]}"; do
    port_busy "$p" && medrec_die "端口 $p 已被占用（已有副本在跑？先 $0 stop）"
  done
  IFS=',' read -r -a VL_GPUS <<< "$VLLM_GPU"
  VL_TP="${#VL_GPUS[@]}"
  # PaddleOCR-VL-1.6 只有 0.9B（bf16 约 1.8GB）—— 单卡足够，别默认摊双卡把 Qwen 的地占了。
  if [ "$VL_TP" -gt 1 ]; then
    medrec_log "⚠ GPU_VL_TP=$GPU_VL_TP 用了 $VL_TP 张卡；这个模型 0.9B，建议改成单卡（如 GPU_VL_TP=\"3\"）把卡让给 Qwen"
  fi
  medrec_log "启动 PaddleOCR-VL (vLLM)：TP=$VL_TP 卡=$VLLM_GPU 端口=[${VLLM_PORTS}] gmu=$VLLM_GMU 确定性开关=[$VLLM_DETERMINISM]"
  # ⚠ 多副本共卡时 gmu 必须按副本数摊薄（2 副本 → 0.35/副本）。vLLM 的
  #   gpu-memory-utilization 按**卡的总量**算，不感知同卡别的副本 → 不摊薄必 OOM。
  for p in "${VPORTS[@]}"; do
    # ⚠ 必须显式指定 CUDA_VISIBLE_DEVICES，否则它会看到 4 张卡并自行挑，可能压到 Qwen 的卡上
    setsid env PATH="$ENV_LLM/bin:$PATH" \
         CUDA_VISIBLE_DEVICES="$VLLM_GPU" \
         VLLM_USE_FLASHINFER_SAMPLER=0 \
         VLLM_USE_V2_MODEL_RUNNER=0 \
      "$ENV_LLM/bin/vllm" serve "$MODEL_OCR_VL_PATH" \
        --served-model-name "$MODEL_OCR_VL_NAME" \
        --host 127.0.0.1 --port "$p" \
        --tensor-parallel-size "$VL_TP" \
        --gpu-memory-utilization "$VLLM_GMU" \
        --max-model-len "$VL_MAXLEN" \
        --limit-mm-per-prompt '{"image":1}' \
        --trust-remote-code \
        --attention-backend FLASH_ATTN \
        --compilation-config '{"mode":0,"cudagraph_mode":"full"}' \
        $VLLM_DETERMINISM \
        >>"$LOG_B" 2>&1 < /dev/null &
    LAUNCH_PID=$!
    echo "$LAUNCH_PID" > "$PID_B.$p" 2>/dev/null || true
    [ "$p" = "${VPORTS[0]}" ] && echo "$LAUNCH_PID" > "$PID_B" 2>/dev/null || true
    [ -n "${LAUNCH_PID:-}" ] && [ "$LAUNCH_PID" -gt 1 ] || medrec_die "PaddleOCR-VL(:$p) 进程没起来，看 $LOG_B"
  done
  medrec_log "B 路首次启动要 1~3 分钟（编译内核），不要中途 kill"
  for p in "${VPORTS[@]}"; do
    check_alive "http://127.0.0.1:$p/v1/models" "PaddleOCR-VL(:$p)"
  done
  # ★ 就绪 ≠ 可用：每个副本各打一次真实小图（第 3 件）。任一副本退化 → 整批拒绝。
  for p in "${VPORTS[@]}"; do
    selfcheck_ocr vllm "http://127.0.0.1:$p"
  done
  for p in "${VPORTS[@]}"; do
    echo "    难块调用接口：POST http://127.0.0.1:$p/v1/chat/completions"
  done
  ;;

# ------------------------------------------------------------------ 停
stop)
  stop_one "$PID_A" "PaddleX 产线" "$PORT_OCR_PADDLE"
  IFS=',' read -r -a VPORTS <<< "$VLLM_PORTS"
  for p in "${VPORTS[@]}"; do
    stop_one "$PID_B.$p" "PaddleOCR-VL(vLLM, :$p)" "$p"
  done
  stop_one "$PID_B" "PaddleOCR-VL(vLLM)" "$PORT_OCR_VL"
  sleep 3
  nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader | sed 's/^/  GPU /'
  ;;

status)
  printf "  A 路 paddlex  :%s  " "$PORT_OCR_PADDLE"
  port_busy "$PORT_OCR_PADDLE" && echo UP || echo down
  IFS=',' read -r -a VPORTS <<< "$VLLM_PORTS"
  for p in "${VPORTS[@]}"; do
    printf "  B 路 vllm    :%s  " "$p"
    port_busy "$p" && echo UP || echo down
    curl -sf -m 3 "http://127.0.0.1:$p/v1/models" 2>/dev/null | head -c 200 || true
    echo
  done
  ;;

# ------------------------------------------------------ 只自检（不启停）
check)
  # 对**当前在监听**的服务各做一次退化自检：paddlex 一次 + 每个 vLLM 副本一次。
  # 任一 rc != 0 → 累积失败，最后统一非零退出（别只打警告）。
  SELFCHK_DIE=0
  IFS=',' read -r -a VPORTS <<< "$VLLM_PORTS"
  fails=0; ran=0
  if port_busy "$PORT_OCR_PADDLE"; then
    selfcheck_ocr paddlex "http://127.0.0.1:$PORT_OCR_PADDLE" || { fails=$((fails+1)); }
    ran=$((ran+1))
  else
    medrec_log "A 路 paddlex :$PORT_OCR_PADDLE 未在监听 —— 跳过"
  fi
  for p in "${VPORTS[@]}"; do
    if port_busy "$p"; then
      selfcheck_ocr vllm "http://127.0.0.1:$p" || { fails=$((fails+1)); }
      ran=$((ran+1))
    else
      medrec_log "B 路 vllm :$p 未在监听 —— 跳过"
    fi
  done
  [ "$ran" -gt 0 ] || medrec_die "没有任何 OCR 服务在监听，无自检对象"
  [ "$fails" -eq 0 ] || medrec_die "$fails/$ran 个服务退化自检未通过 —— 禁止在此状态上跑产线"
  medrec_log "✓ 全部 $ran 个服务自检通过"
  ;;

*) medrec_die "用法：$0 {paddle|vllm|check|stop|status}" ;;
esac
