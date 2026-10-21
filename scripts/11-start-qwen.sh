#!/usr/bin/env bash
# =====================================================================
#  11-start-qwen.sh — Qwen3.8-27B（Q4/W4A16）后端：数据处理 + 对话
#
#  这是整条链路的"后端大脑"：
#    · 结构化：吃 compiled.md，吐 JSON（走 response_format / structured_outputs，默认 xgrammar）
#    · 对话：OpenAI 兼容 /v1/chat/completions，供 Web UI / 平台调用
#    · 仲裁：接 Laya 低置信的升级请求（见 22-score-pipeline.py）
#
#  用法： bash 11-start-qwen.sh          # 启动（幂等：已在跑则直接退出）
#         bash 11-start-qwen.sh stop
#         bash 11-start-qwen.sh status
#         bash 11-start-qwen.sh warm       # 预热（首次编译内核很慢，先打一发）
# =====================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/config.env"
medrec_clean_path
medrec_mkdirs

LOG="$MEDREC_LOGS/qwen.log"
PIDF="$MEDREC_RUN/qwen.pid"
ACTION="${1:-start}"

port_used() { ss -ltn 2>/dev/null | grep -c ":$PORT_QWEN " >/dev/null; }

case "$ACTION" in

stop)
  if [ -f "$PIDF" ]; then
    pid="$(cat "$PIDF")"
    if kill -0 "$pid" 2>/dev/null; then
      medrec_log "停 Qwen (pid=$pid)"; kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDF"
  fi
  pids="$(ss -ltnp 2>/dev/null | awk -v p=":$PORT_QWEN" '$4 ~ p {print $NF}' \
          | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)"
  [ -n "$pids" ] && echo "$pids" | xargs -r kill 2>/dev/null || true
  sleep 3
  nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader | sed 's/^/  GPU /'
  ;;

status)
  printf "  Qwen :%s  " "$PORT_QWEN"
  port_used && echo UP || echo down
  curl -sf -m 5 "http://127.0.0.1:$PORT_QWEN/v1/models" 2>/dev/null | head -c 300 || true
  echo
  [ -f "$LOG" ] && tail -3 "$LOG" || true
  ;;

warm)
  medrec_log "预热（首次请求要按真实输入编译 kernel，1~3 分钟的等待是正常的）"
  time curl -s -m 600 "http://127.0.0.1:$PORT_QWEN/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL_QWEN_NAME\",\"messages\":[{\"role\":\"user\",\"content\":\"回复：就绪\"}],
         \"max_tokens\":16,\"temperature\":0,
         \"chat_template_kwargs\":{\"enable_thinking\":false}}" | head -c 600
  echo
  ;;

start)
  [ -x "$PY_LLM" ] || medrec_die "qwen-env 不存在，先跑 01-setup-envs.sh"
  [ -d "$MODEL_QWEN_PATH" ] || medrec_die "缺权重 $MODEL_QWEN_PATH，先跑 02-download-models.sh qwen"
  if port_used; then medrec_log "端口 $PORT_QWEN 已在监听，认为已在跑；要用 stop 先停"; exit 0; fi

  # ---- 前置检查：可执行文件必须在（否则 setsid 会静默失败，日志还会是空的）----
  VLLM_BIN="$ENV_LLM/bin/vllm"
  [ -x "$VLLM_BIN" ] || medrec_die "找不到可执行文件 $VLLM_BIN —— 检查 config.env 的 ENV_LLM 与 01-setup-envs.sh 是否跑过"

  # ---- TP 校验：卡数必须整除【文本模型】的注意力头数 ----
  #  注意别拿官方 recipe 里“视觉编码器 16 头”当文本模型的值：
  #  cyankiwi/Qwen3.8-27B-AWQ-INT4 实测 num_attention_heads=24 → 有效 TP ∈ {1,2,3,4,6,8}
  read -r N_HEADS N_KV <<<"$("$PY_LLM" - "$MODEL_QWEN_PATH" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1] + "/config.json", encoding="utf-8"))
tc = cfg.get("text_config") or cfg
print(tc.get("num_attention_heads") or 0, tc.get("num_key_value_heads") or 0)
PY
)"
  IFS=',' read -r -a QWEN_GPUS <<< "$GPU_QWEN_TP"
  TP="${QWEN_TP:-${#QWEN_GPUS[@]}}"
  N_GPUS="${#QWEN_GPUS[@]}"
  if [ "${N_HEADS:-0}" -gt 0 ] && [ $((N_HEADS % TP)) -ne 0 ]; then
    VALID=""
    for t in 1 2 3 4 6 8; do
      [ $((N_HEADS % t)) -eq 0 ] && [ "$t" -le "$N_GPUS" ] && VALID="$VALID $t"
    done
    medrec_die "TP=$TP 不整除 num_attention_heads=$N_HEADS → vLLM 会断言失败。可用（不超过你给的 $N_GPUS 张卡）：${VALID:- 无}（穷举 1..16 见下方提示）"
  fi
  VALID_ALL=""
  for t in $(seq 1 16); do [ $((N_HEADS % t)) -eq 0 ] && VALID_ALL="$VALID_ALL $t"; done
  medrec_log "模型 $MODEL_QWEN_ID  heads=$N_HEADS kv_heads=$N_KV  可选TP:${VALID_ALL}  本次 TP=$TP 卡=$GPU_QWEN_TP"

  # ---- 可选参数：一律「先问本机 vllm 支不支持」，绝不硬塞 ----
  #  教训：--guided-decoding-backend 在 vLLM 0.30 已被移除，硬塞会让整个进程
  #  以 `vllm: error: unrecognized arguments:` 秒退（日志只有 167 字节）。
  VLLM_HELP="$("$VLLM_BIN" serve --help 2>&1)"
  [ -n "$VLLM_HELP" ] || medrec_die "拿不到 vllm serve --help 输出 —— vllm 环境有问题"
  has_opt() { local f="${1#--}"; "$VLLM_BIN" serve --help="$f" 2>&1 | grep -qE "^ *--${f}([, =]|$)"; }

  OPT=(--reasoning-parser qwen3)
  add_opt() {  # add_opt <开关> <值...>  —— 开关不存在就跳过并记一条
    local flag="$1"; shift
    if has_opt -- "$flag"; then OPT+=("$flag" "$@"); else medrec_log "  跳过硬编码开关 $flag（本机 vllm 不支持）"; fi
  }

  add_opt --enable-prefix-caching
  # 结构化输出后端：新版 CLI 已移除该开关，结构化走 response_format（默认 xgrammar）
  if has_opt --guided-decoding-backend; then
    add_opt --guided-decoding-backend xgrammar
    medrec_log "  已启用 --guided-decoding-backend xgrammar"
  else
    medrec_log "  本机 vllm 无 --guided-decoding-backend → 结构化输出改用 response_format（默认 xgrammar），不影响 F3 抽取"
  fi
  # 该模型的 chat template 以 <think> 开头，reasoning 解析器基本必备
  if has_opt --reasoning-parser; then
    if printf '%s' "$VLLM_HELP" | grep -qE 'qwen3'; then add_opt --reasoning-parser qwen3
    else medrec_log "  跳过错：--reasoning-parser 不认识 qwen3（客户端侧已用 enable_thinking=false）"; fi
  fi
  if has_opt --enable-auto-tool-choice; then
    if has_opt --tool-call-parser; then
      printf '%s' "$VLLM_HELP" | grep -q 'qwen3_coder' && add_opt --enable-auto-tool-choice --tool-call-parser qwen3_coder \
        || add_opt --enable-auto-tool-choice --tool-call-parser qwen3_coder
    fi
  fi
  # 显存紧张时丢掉视觉塔，只做纯文本
  if [ "${QWEN_TEXT_ONLY:-0}" = "1" ]; then
    if has_opt --language-model-only; then
      add_opt --language-model-only
      medrec_log "  QWEN_TEXT_ONLY=1 → 只加载语言模型（省显存；OCR 已在上游完成）"
    else
      medrec_log "  本机 vllm 无 --language-model-only，忽略该开关（视觉塔会一起加载）"
    fi
  fi
  # ★ 混合结构（Mamba/线性注意力 + 全注意力）的硬约束，不设必炸：
  #   每个 decode 序列要独占一个 Mamba cache block，能分出多少块由 GMU 决定。
  #   GMU=0.80 时只有 234 块，而 vLLM 的 max_num_seqs 默认 256 → 超了：
  #     ValueError: max_num_seqs (256) exceeds available Mamba cache blocks (234).
  #     Each decode sequence requires one Mamba cache block, so CUDA graph
  #     capture cannot proceed.
  #   注意这个检查发生在 CUDA graph 捕获阶段（权重已加载完、日志已刷出
  #   'GPU KV cache size: ...' 之后）→ 看起来像"跑到一半莫名退出"。
  #   64 是原来稳定跑了两天的那个 27B 进程 cmdline 里的值，照抄；
  #   想提高并发上限就调 QWEN_MAX_SEQS（天花板 = 日志里那个 block 数）。
  add_opt --max-num-seqs "${QWEN_MAX_SEQS:-64}"

  medrec_log "最终可选参数：${OPT[*]:-（无）}"

  # =====================================================================
  #  显存账预检（★ 这是 "No available memory for the cache blocks" 的根治）
  #
  #  两个必须记住的事实：
  #   ① gpu-memory-utilization 是**占总量的比例**，不是"可用量的比例"
  #   ② 它**不感知卡上已被别的服务占用的部分** —— 同卡跑着 PaddleX / OCR-VL
  #      时，vLLM 照样按 total × GMU 算自己的预算，于是权重一装完就没 KV 了
  #  所以：先算出「权重 + 别的服务占用」还剩多少能当 KV，再决定 GMU。
  # =====================================================================
  WEIGHT_MIB=$(find "$MODEL_QWEN_PATH" -name '*.safetensors' -printf '%s\n' 2>/dev/null \
               | awk '{s+=$1} END{printf "%d", s/1048576}')
  WEIGHT_MIB="${WEIGHT_MIB:-0}"
  PER_CARD_MIB=$(( WEIGHT_MIB / TP ))
  # 每卡已被"别人"占用的量（PaddleX / OCR-VL / 僵尸进程）
  OTHERS_MIB=$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>/dev/null \
               | awk -F',' '{gsub(/ /,"",$2); s+=$2} END{printf "%d", s}')
  OTHERS_MIB="${OTHERS_MIB:-0}"
  TOTAL_MIB=$(nvidia-smi -i 0 --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null)
  TOTAL_MIB="${TOTAL_MIB:-24564}"
  BUDGET_MIB=$(awk -v t="$TOTAL_MIB" -v g="$QWEN_GMU" 'BEGIN{printf "%d", t*g}')
  # 安全垫 = CUDA context + 图捕获 + **profile 期的激活峰值**。
  # 血的教训：vLLM 是在 profile 跑完之后才判 "有没有地方放 KV"，
  # 所以 max_model_len 越大，峰值越高，能留给 KV 的就越少 —— 固定 700MiB 不够。
  # 经验式：1500 + maxlen×0.02（32K → 约 2155 MiB）
  RESERVE=$(awk -v m="$QWEN_MAXLEN" 'BEGIN{printf "%d", 1500 + m*0.02}')
  KV_MIB=$(( BUDGET_MIB - PER_CARD_MIB - RESERVE ))

  medrec_log "显存账：总量 ${TOTAL_MIB}MiB/卡 · GMU=${QWEN_GMU} → 预算 ${BUDGET_MIB}MiB"
  medrec_log "        权重 ${WEIGHT_MIB}MiB / TP=$TP = ${PER_CARD_MIB}MiB/卡 · 安全垫 ${RESERVE}MiB"
  medrec_log "        → 预计可给 KV：${KV_MIB}MiB/卡"

  if [ "$KV_MIB" -lt 512 ]; then
    echo
    echo "  ✗ KV 预算不足，这样起必然抛 'No available memory for the cache blocks'"
    echo "    当前占用者（含同卡的其他服务，vLLM 不会替你扣掉）："
    nvidia-smi --query-compute-apps=pid,used_memory,process_name --format=csv,noheader | sed 's/^/      /'
    NEED_GMU=$(awk -v p="$PER_CARD_MIB" -v r="$RESERVE" -v t="$TOTAL_MIB" 'BEGIN{printf "%.2f", (p+r+2048)/t}')
    echo
    echo "    三个可选做法（任选其一或组合）："
    echo "      a) 把同卡的其他服务挪走/停掉：  bash 10-start-ocr.sh stop"
    echo "      b) 提高 GMU：                   sed -i 's/^export QWEN_GMU=.*/export QWEN_GMU=$NEED_GMU/' $HERE/config.env"
    echo "      c) 缩短上下文：                 QWEN_MAXLEN=16384（当前 $QWEN_MAXLEN）"
    [ "$OTHERS_MIB" -gt 0 ] && echo "    提示：检测到同卡另有约 ${OTHERS_MIB}MiB 被占，这不会自动从 vLLM 预算里扣除"
    medrec_die "先解决显存账再起（本次未启动）"
  fi
  medrec_log "✓ 显存账可行（KV ${KV_MIB}MiB/卡）"

  nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader | sed 's/^/  GPU /'
  medrec_log "启动 vLLM: TP=$TP :$PORT_QWEN maxlen=$QWEN_MAXLEN gmu=$QWEN_GMU"
  : >"$LOG"                      # 每次启动清空日志，避免分不清是这一次还是上一次的

  # 启动前先让 vllm 自己校验一遍参数 —— 参数写错会在毫秒级报
  # `vllm: error: unrecognized arguments: ...`，比等 8 分钟强。
  # 用假模型路径做探针：既不加载 15GB 权重，也能真正走一遍 argparse。
  ARGLOG="$MEDREC_RUN/qwen.args.log"
  if ! "$VLLM_BIN" serve /nonexistent-probe \
        --served-model-name "$MODEL_QWEN_NAME" --host 127.0.0.1 --port "$PORT_QWEN" \
        --tensor-parallel-size "$TP" --gpu-memory-utilization "$QWEN_GMU" \
        --max-model-len "$QWEN_MAXLEN" "${OPT[@]}" --help >/dev/null 2>"$ARGLOG"; then
    medrec_log "✗ 启动参数被 vllm 拒绝（这就是秒退的真因）："
    tail -5 "$ARGLOG" | sed 's/^/    /'
    medrec_die "先修参数再起。查本机支持的开关： $VLLM_BIN serve --help | grep -i <关键词>"
  fi
  rm -f "$ARGLOG"

  # ⚠ $! 在 set -e 下是陷阱：若启动即失败，$! 为空 → echo > $PIDF 会返回非 0 → 脚本静默退出。
  #   这里用显式赋值 + 校验兜住。
  setsid env PATH="$ENV_LLM/bin:$PATH" \
       CUDA_VISIBLE_DEVICES="$GPU_QWEN_TP" \
       VLLM_USE_FLASHINFER_SAMPLER=0 \
    "$VLLM_BIN" serve "$MODEL_QWEN_PATH" \
      --served-model-name "$MODEL_QWEN_NAME" \
      --host 127.0.0.1 --port "$PORT_QWEN" \
      --tensor-parallel-size "$TP" \
      --gpu-memory-utilization "$QWEN_GMU" \
      --max-model-len "$QWEN_MAXLEN" \
      "${OPT[@]}" \
      >>"$LOG" 2>&1 &
  LAUNCH_PID=$!
  echo "$LAUNCH_PID" >"$PIDF" 2>/dev/null || true
  if [ -z "${LAUNCH_PID:-}" ] || [ "$LAUNCH_PID" -le 1 ]; then
    medrec_die "进程没起来（\$!=${LAUNCH_PID:-空}）—— 看 $LOG"
  fi
  medrec_log "pid=$LAUNCH_PID  日志：$LOG"

  # 启动后 3 秒必须已经在写日志；日志仍为空 = 进程根本没接上输出或已秒退
  sleep 3
  if [ ! -s "$LOG" ]; then
    if kill -0 "$LAUNCH_PID" 2>/dev/null; then
      medrec_log "⚠ 进程活着但日志为空（异常）—— 直接看终端输出："
      "$PY_LLM" -c "import vllm,sys;print('vllm import OK', vllm.__version__)" || true
    else
      wait "$LAUNCH_PID" 2>/dev/null; RC=$?
      medrec_die "进程 3 秒内就退出了（退出码 $RC），且日志为空 —— 通常是启动命令本身有问题：可执行文件/参数/PATH。手动复现： $VLLM_BIN serve --help | head"
    fi
  fi

  medrec_log "等待就绪（首次含 kernel 编译，最多 8 分钟）…"
  for i in $(seq 1 240); do
    if curl -sf -m 3 "http://127.0.0.1:$PORT_QWEN/v1/models" >/dev/null 2>&1; then
      medrec_log "✓ Qwen 就绪：http://127.0.0.1:$PORT_QWEN/v1"
      grep -E 'KV cache|Maximum concurrency|Available KV cache memory|graph' "$LOG" | tail -5 || true
      exit 0
    fi
    if ! kill -0 "$LAUNCH_PID" 2>/dev/null; then
      wait "$LAUNCH_PID" 2>/dev/null; RC=$?
      echo "──── 进程已退出（码 $RC），日志尾部 ────"
      # 关键判读：Engine core initialization failed 只是外壳，真因在上面几行
      grep -n -B2 -A15 -E 'EngineCore_0|ERROR|Traceback|No such file|nvcc|ninja|out of memory' "$LOG" | tail -60 \
        || tail -40 "$LOG"
      medrec_die "Qwen 进程已退出（码 $RC）"
    fi
    sleep 2
  done
  tail -40 "$LOG"; medrec_die "Qwen 8 分钟未就绪（进程还活着，看上面日志尾部）"
  ;;

*) medrec_die "用法：$0 {start|stop|status|warm}" ;;
esac
