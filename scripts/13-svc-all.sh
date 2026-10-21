#!/usr/bin/env bash
# =====================================================================
#  13-svc-all.sh — 五个常驻推理服务的统一开关（总控）
#
#  设计原则：**不重复实现启动逻辑**。谁有专职脚本就用谁 ——
#      ocr   (A 路 PaddleX)      -> 10-start-ocr.sh paddle
#      ocrvl (B 路 PaddleOCR-VL) -> 10-start-ocr.sh vllm
#      qwen  (Qwen3.8-27B)       -> 11-start-qwen.sh start
#      laya  (Laya 打分)          -> 12-start-laya.sh start
#  总控只补它们没有的四件：
#      ① 一次看全部（含那个**不在 config.env 里的遗留服务 qwen-small:8101**）
#      ② 等全部就绪（复用 config.env 的 wait_http）
#      ③ free-gpu0 —— 为训练腾 GPU 0，按「谁真的占着 GPU 0」来停，不靠猜
#      ④ **单个** OCR 通道的停止粒度（10-start-ocr.sh 的 stop 会同时停两条）
#
#  用法：
#     bash 13-svc-all.sh list
#     bash 13-svc-all.sh status
#     bash 13-svc-all.sh start                 # 全部（已在跑的不动）
#     bash 13-svc-all.sh start ocr laya
#     bash 13-svc-all.sh stop
#     bash 13-svc-all.sh stop ocrvl
#     bash 13-svc-all.sh restart qwen
#     bash 13-svc-all.sh wait                  # 等就绪（默认 300 秒）
#     bash 13-svc-all.sh refresh               # **接管**：把当前真正的监听进程写进 PID 记录
#                                              #   （服务被手工重启过、PID 记录过期时先用它）
#     bash 13-svc-all.sh free-gpu0             # 腾 GPU 0：停占着它的已知服务并报告其余
#
#  ⚠ qwen-small（8101）不在 config.env 里 —— 它来自第 4 个 conda 环境 llmsrv，
#    原始 cwd 是个已删除的目录（/home/ubuntu/f3-hc），属于**没人管理的遗留服务**。
#    本脚本能 start/stop/status 它（用 QWEN_SMALL_* 变量，见下），但请先确认它还有没有人用。
# =====================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/config.env"
medrec_clean_path
medrec_mkdirs

# config.env 管的四个
ALL_DOC="ocr ocrvl qwen laya"
# 不在 config.env 里的遗留服务（要改就改这里，别改脚本正文）
ORPHAN="qwen-small"
ALL="$ALL_DOC $ORPHAN"

# --- 遗留服务 qwen-small 的默认参数（原始启动命令反推出来的，见 F6分流改动说明.md） ---
export QWEN_SMALL_ENV="${QWEN_SMALL_ENV:-$CONDA_BASE/envs/llmsrv}"
export QWEN_SMALL_PORT="${QWEN_SMALL_PORT:-8101}"
export QWEN_SMALL_GPU="${QWEN_SMALL_GPU:-1}"
export QWEN_SMALL_MODEL="${QWEN_SMALL_MODEL:-$HOME/.cache/modelscope/models/Qwen--Qwen3-8B/snapshots/master}"
export QWEN_SMALL_GMU="${QWEN_SMALL_GMU:-0.60}"
export QWEN_SMALL_MAXLEN="${QWEN_SMALL_MAXLEN:-16384}"
# 下面两个是**从旧进程 cmdline 反推**出来的，不保证在每台机器上都对。
# 置成空串（QWEN_SMALL_QUANT= ）就不传该开关 —— start 里还会再探一次本机 vLLM
# 认不认这个开关，不认就跳过并记一条，绝不硬塞（硬塞会让 argparse 秒退到日志里，屏幕上看不见）。
export QWEN_SMALL_QUANT="${QWEN_SMALL_QUANT-fp8_per_channel}"   # 权重不是 fp8 → 置空
export QWEN_SMALL_ATTN="${QWEN_SMALL_ATTN-FLASH_ATTN}"          # WSL 时代的 workaround → 置空可关

port_of() {
  case "$1" in
    ocr)        echo "$PORT_OCR_PADDLE" ;;
    ocrvl)      echo "$PORT_OCR_VL" ;;
    qwen)       echo "$PORT_QWEN" ;;
    laya)       echo "$PORT_LAYAD" ;;
    qwen-small) echo "$QWEN_SMALL_PORT" ;;
  esac
}
pidf_of() {
  case "$1" in
    ocr)        echo "$MEDREC_RUN/ocr_paddle.pid" ;;
    ocrvl)      echo "$MEDREC_RUN/ocr_vllm.pid" ;;
    qwen)       echo "$MEDREC_RUN/qwen.pid" ;;
    laya)       echo "$MEDREC_RUN/laya.pid" ;;
    qwen-small) echo "$MEDREC_RUN/qwen_small.pid" ;;
  esac
}
# 探测路径**不统一**：8100/8111 是自建服务（根路径 404 是正常的），8101/8102/8110 是 vLLM
probe_of() {
  case "$1" in
    laya) echo "/health" ;;
    ocr)  echo "/docs" ;;
    *)    echo "/v1/models" ;;
  esac
}
desc_of() {
  case "$1" in
    ocr)        echo "PaddleX 产线（A 路）" ;;
    ocrvl)      echo "PaddleOCR-VL（B 路 vLLM）" ;;
    qwen)       echo "Qwen3.8-27B AWQ（TP=$QWEN_TP）" ;;
    laya)       echo "Laya 打分微服务" ;;
    qwen-small) echo "⚠ 遗留：Qwen3-8B fp8（不在 config.env）" ;;
  esac
}

# ★ `port_up` 曾经有个会让 status 说假话的 bug：写成
#     code="$(curl ... -w '%{http_code}' URL 2>/dev/null || echo 000)"
#   而 **curl 连接被拒时本来就会输出 `000`**，`|| echo 000` 再追加一个，
#   字符串变成 "000\n000" → `[ "$code" != "000" ]` 成立 → 判成「活着」。
#   实测后果：qwen/laya 已停（GPU 2/3 = 18 MiB）却显示「就绪」，
#   并且 qwen-small 的 start 会因此以为「已在跑」而跳过启动。
port_up() {   # 「不是 000 就算活着」—— 8100/8111 对 /v1/models 会返回 404，那也是活着
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
          "http://127.0.0.1:$(port_of "$1")$(probe_of "$1")" 2>/dev/null)"
  [ -n "$code" ] && [ "$code" != "000" ]
}

# 「有没有进程在听这个端口」是另一件事：直接问 ss，别拿 HTTP 探测代替
has_listener() { [ -n "$(listener_pids "$(port_of "$1")")" ]; }

# 「这段文本里有没有这个 token」—— 纯 bash case 匹配的通用子串测试。
# 用途一：本机 vLLM 认不认某个开关（拿 `serve --help` 的输出过一遍）
# 用途二：某 pid 的 cmdline 里有没有本服务的端口（停止前的安全闸）
#
# ⚠ **不要**改成 `printf '%s' "$text" | grep -q -- "$tok"`
# 因为脚本是 `set -o pipefail`，而 `grep -q` 命中即退出会让上游 printf 吃
# SIGPIPE(141)，于是管道整体返回非 0 —— 把「命中」误报成「没命中」。
# 这是**竞态**：短字符串常常在 grep 退出前就写完了，所以平时看不出来，
# 偶尔才咬人。同一个坑 20-smoke.sh 的 listen() 里记录过一次
# （那次把「在监听」误报成「没监听」，害得人去重复启动服务）。
has_opt_in() {   # has_opt_in <文本> <token>
  case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------- 代理预检
# ★ 实测（2026-09-27）：`all_proxy` 若是 httpx 认不出的 scheme（如 socks://），
#   `huggingface_hub` 会在**构造 httpx.Client 时**就抛
#       ValueError: Unknown scheme for proxy URL URL('socks://127.0.0.1:7897/')
#   于是 Laya 的 Router 一访问 HF 就退出，服务根本起不来。
#   ⚠ **不要**建议 `clear-proxy.sh --clear`：这台机器直连 tuna/pypi/modelscope
#     全是 000（没代理就没网），清掉全部代理变量会断网。
#   正解是只去掉 `all_proxy`/`ALL_PROXY` —— `http_proxy`/`https_proxy` 已经覆盖
#   HTTP/HTTPS，而 all_proxy 的 socks scheme 是 httpx 解析不了的。
#   （端口 7897 是 mixed 口，既收 HTTP 也收 SOCKS，所以 http://127.0.0.1:7897 可用。）
check_proxy() {
  local v val bad=""
  for v in all_proxy ALL_PROXY; do
    val="$(eval "printf '%s' \"\${$v:-}\"")"
    case "$val" in socks*|socks4*|socks5*) bad="$bad $v=$val" ;; esac
  done
  if [ -n "$bad" ]; then
    echo "  ⚠ httpx 不支持的 socks 代理（会让 huggingface_hub 直接抛 ValueError）：$bad"
    echo "    → 只去掉它，别清全部代理（本机直连不通）："
    echo "        unset all_proxy ALL_PROXY"
    echo "      http_proxy/https_proxy 保留即可，网络照走代理。"
  fi
  # 顺带体检：http_proxy 在、但直连探测不通 —— 提醒别误清
  if [ -n "$(eval "printf '%s' \"\${http_proxy:-}\"")" ]; then
    if ! curl -s -o /dev/null --max-time 3 https://hf-mirror.com 2>/dev/null; then
      echo "  ! 经 http_proxy 访问 hf-mirror 也不通 —— 先确认代理进程还在（本机直连是 000）"
    fi
  fi
}

# ---------------------------------------------------------------- 单个启动：委托
start_one() {
  local d
  case "$1" in
    ocr|ocrvl|qwen|laya)
      case "$1" in
        ocr)   d="10-start-ocr.sh paddle" ;;
        ocrvl) d="10-start-ocr.sh vllm" ;;
        qwen)  d="11-start-qwen.sh start" ;;
        laya)  d="12-start-laya.sh start" ;;
      esac
      medrec_log "[$1] 委托 ${d%% *} ${d#* }"
      bash "$HERE/${d%% *}" ${d#* } || medrec_log "[$1] 启动脚本返回非零（看上面输出）"
      ;;
    qwen-small)
      if port_up qwen-small; then
        medrec_log "[$1] 已在跑（端口 $QWEN_SMALL_PORT），跳过"; return 0
      fi

      # ---- 前置检查：一律**探测**，不写死版本号 ----
      #  原来写死 `$QWEN_SMALL_ENV/bin/python3.12`。llmsrv 的 python 只要不是
      #  3.12，这里就直接 return 1，而屏幕上只有一句「缺 …/python3.12」，
      #  既没说是版本问题，也没说该改哪个变量。
      local py="" bin="$QWEN_SMALL_ENV/bin"
      for c in python3 python3.13 python3.12 python3.11 python3.10 python; do
        [ -x "$bin/$c" ] && { py="$bin/$c"; break; }
      done
      if [ -z "$py" ]; then
        medrec_log "[$1] ✗ $bin 下找不到任何 python —— $QWEN_SMALL_ENV 这个环境存在吗？"
        medrec_log "      改指向： QWEN_SMALL_ENV=/path/to/env bash $0 start qwen-small"
        return 1
      fi
      if [ ! -x "$bin/vllm" ]; then
        medrec_log "[$1] ✗ 找不到 $bin/vllm —— 该环境里没装 vLLM？"
        return 1
      fi
      if [ ! -d "$QWEN_SMALL_MODEL" ]; then
        medrec_log "[$1] ✗ 缺权重目录 $QWEN_SMALL_MODEL"
        medrec_log "      这是 Qwen3-8B 的 modelscope 缓存路径。确认下过，或用 QWEN_SMALL_MODEL= 指到别处"
        return 1
      fi
      medrec_log "[$1] python=$py  $("$py" -V 2>&1)"
      medrec_log "[$1] vllm=$bin/vllm  权重=$QWEN_SMALL_MODEL"
      medrec_log "[$1] 卡=$QWEN_SMALL_GPU  端口=$QWEN_SMALL_PORT  GMU=$QWEN_SMALL_GMU  maxlen=$QWEN_SMALL_MAXLEN"
      if command -v nvidia-smi >/dev/null 2>&1; then
        # 它和 ocrvl / laya 共卡，先看一眼余量：GMU 是按**总量**算的，不感知同卡别人
        medrec_log "[$1] GPU$QWEN_SMALL_GPU 现状(memory.used,total)：$(nvidia-smi -i "$QWEN_SMALL_GPU" \
          --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null)"
      fi

      # ---- 可选参数：先问本机 vllm 支不支持，绝不硬塞 ----
      local help; help="$("$bin/vllm" serve --help 2>&1)"
      if [ -z "$help" ]; then
        medrec_log "[$1] ✗ 拿不到 vllm serve --help 输出 —— 这个 vLLM 装坏了"
        return 1
      fi
      local opt=() q="$QWEN_SMALL_QUANT" at="$QWEN_SMALL_ATTN"
      if [ -n "$q" ]; then
        if has_opt_in "$help" --quantization; then opt+=(--quantization "$q")
        else medrec_log "[$1] 跳过 --quantization（本机 vLLM 不支持该开关）"; fi
      fi
      if [ -n "$at" ]; then
        if has_opt_in "$help" --attention-backend; then opt+=(--attention-backend "$at")
        else medrec_log "[$1] 跳过 --attention-backend（本机 vLLM 不支持该开关）"; fi
      fi
      has_opt_in "$help" --enable-prefix-caching && opt+=(--enable-prefix-caching)
      has_opt_in "$help" --default-chat-template-kwargs \
        && opt+=(--default-chat-template-kwargs '{"enable_thinking": false}')
      medrec_log "[$1] 本次可选参数：${opt[*]:-（无）}"

      # ---- 参数预检：拿假模型路径空跑一遍 argparse，毫秒级报错 ----
      #  没有这一步，参数写错要等权重加载完才炸，而日志在 $log 里。
      local arglog="$MEDREC_RUN/qwen_small.args.log"
      if ! "$bin/vllm" serve /nonexistent-probe --served-model-name qwen-small \
             --host 127.0.0.1 --port "$QWEN_SMALL_PORT" \
             --gpu-memory-utilization "$QWEN_SMALL_GMU" \
             --max-model-len "$QWEN_SMALL_MAXLEN" "${opt[@]}" --help \
             >/dev/null 2>"$arglog"; then
        medrec_log "[$1] ✗ 启动参数被 vllm 拒绝（这就是秒退的真因）："
        tail -6 "$arglog" | sed 's/^/      /'
        rm -f "$arglog"
        return 1
      fi
      rm -f "$arglog"

      local log="$MEDREC_LOGS/qwen_small.log"
      : >"$log"                       # 清空，避免分不清是这次还是上次的
      ( cd "$MEDREC_SRC" 2>/dev/null || cd "$HOME"
        # ★ /usr/bin 前置：这台机器 PATH 里 fsl 的 gcc/g++ 排在前面，
        #   任何 JIT（flashinfer/ninja）都会挑到 FSL 的老编译器而失败。
        export PATH="$bin:/usr/bin:/bin:$PATH"
        export CC=/usr/bin/gcc CXX=/usr/bin/g++
        export CUDA_VISIBLE_DEVICES="$QWEN_SMALL_GPU"
        export HF_HOME="$MEDREC_MODELS"
        export VLLM_USE_FLASHINFER_SAMPLER=0
        setsid "$bin/vllm" serve "$QWEN_SMALL_MODEL" \
          --served-model-name qwen-small \
          --host 127.0.0.1 --port "$QWEN_SMALL_PORT" \
          --gpu-memory-utilization "$QWEN_SMALL_GMU" \
          --max-model-len "$QWEN_SMALL_MAXLEN" \
          "${opt[@]}" \
          >>"$log" 2>&1 &
        echo $! > "$(pidf_of qwen-small)"
      )
      local pid; pid="$(cat "$(pidf_of qwen-small)" 2>/dev/null)"

      # ---- ★ 起完必须**验证**。原来这里无条件打印「已拉起 pid=…」——
      #      参数错 / 显存不够 / 权重缺，全都看着像成功，然后你只能自己猜。
      if [ -z "${pid:-}" ] || [ "$pid" -le 1 ] 2>/dev/null; then
        medrec_log "[$1] ✗ 进程没起来（pid=${pid:-空}）"; return 1
      fi
      sleep 3
      if ! kill -0 "$pid" 2>/dev/null; then
        medrec_log "[$1] ✗ 进程 3 秒内就退出了 —— 日志尾部："
        tail -20 "$log" 2>/dev/null | sed 's/^/      /'
        medrec_log "      完整日志： $log"
        return 1
      fi

      medrec_log "[$1] pid=$pid，等就绪（8B 冷启动约 1~3 分钟）…"
      for i in $(seq 1 90); do
        if port_up qwen-small; then
          medrec_log "[$1] ✓ 已就绪  http://127.0.0.1:$QWEN_SMALL_PORT/v1   pid=$pid"
          return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
          medrec_log "[$1] ✗ 等待中进程退出了（第 ${i} 次，共约 $((i*2)) 秒）—— 日志判读："
          # 关键判读：`Engine core initialization failed` 只是外壳，真因在它上面几行；
          #           Mamba/混合注意力模型的 max_num_seqs 超块数也是在这里现形。
          grep -n -B2 -A12 -E 'EngineCore|ERROR|Traceback|ValueError|No available memory|Mamba|cache blocks|No such file|out of memory' \
            "$log" 2>/dev/null | tail -40 | sed 's/^/      /' || tail -30 "$log" | sed 's/^/      /'
          medrec_log "      完整日志： $log"
          return 1
        fi
        sleep 2
      done
      medrec_log "[$1] ✗ 180 秒仍未就绪（进程还活着）—— 日志尾部："
      tail -20 "$log" 2>/dev/null | sed 's/^/      /'
      return 1
      ;;
    *) medrec_log "不认识的服务：$1 （可选：$ALL）"; return 1 ;;
  esac
}

# ---------------------------------------------------------------- 单个停止
# qwen/laya 有专职 stop；ocr/ocrvl 需要**单通道**粒度，所以用 PID 文件自己停。
#
# ★ 「接管」逻辑（2026-09-27 实测必需）：这些服务被手工重启过多次，PID 文件里留的是
#   早就死掉的旧 PID，而端口上另有进程在听。原来只报「pid 已不存在」就放弃 →
#   `stop` 等于失效。现在：端口有人在听而 PID 过期时，从 ss 找出**真正的监听进程**，
#   校验它的 cmdline 含本服务端口后接管并停掉。仍然不猜、不按模式杀。
listener_pids() {   # -> 该端口上的监听 PID（可能多个）
  ss -ltnp 2>/dev/null | awk -v p=":$1" '$4 ~ p {print $NF}' \
    | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u
}

stop_by_pid() {
  local name="$1" pf pid want cmd uniq=()
  pf="$(pidf_of "$name")"; want="$(port_of "$name")"

  # ---- 1) PID 文件里的 pid 还活着 → 先按它停 ----
  if [ -f "$pf" ]; then
    pid="$(cat "$pf" 2>/dev/null || echo '')"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo '')"
      if ! has_opt_in "$cmd" "$want"; then
        medrec_log "[$name] pid $pid 的 cmdline 里没有端口 $want → **拒绝杀**"
        echo "      cmdline: $cmd"
        return 1
      fi
      medrec_log "[$name] kill $pid（PID 文件）"
      kill "$pid" 2>/dev/null
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -0 "$pid" 2>/dev/null && { medrec_log "[$name] 20 秒未退，补 SIGKILL"; kill -9 "$pid" 2>/dev/null; }
      rm -f "$pf"
      return 0
    fi
    medrec_log "[$name] PID 文件里的 '$pid' 已死（手工重启过、没写回）→ 转为接管真正的监听进程"
    rm -f "$pf"
  fi

  # ---- 2) 端口没人听：本来就没跑（用 ss 判，不用 HTTP 探测）----
  if ! has_listener "$name"; then medrec_log "[$name] 本来就没在跑"; return 0; fi

  # ---- 3) 接管：从 ss 找出监听 PID，逐个校验 cmdline ----
  local p
  while read -r p; do
    [ -n "$p" ] || continue
    kill -0 "$p" 2>/dev/null || continue
    cmd="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null || echo '')"
    if has_opt_in "$cmd" "$want"; then
      uniq+=("$p")
    else
      medrec_log "[$name] 监听 pid $p 的 cmdline 里没有 $want → 跳过（不杀）"
      echo "      cmdline: ${cmd:0:160}"
    fi
  done < <(listener_pids "$want")

  if [ "${#uniq[@]}" -eq 0 ]; then
    medrec_log "[$name] 端口 $want 在听，但没找到 cmdline 含该端口的进程 → **不代你杀**"
    ss -ltnp 2>/dev/null | grep ":$want " | sed 's/^/      /'
    return 1
  fi
  medrec_log "[$name] 接管 ${#uniq[@]} 个监听进程：${uniq[*]}"
  echo "${uniq[0]}" > "$pf"
  kill "${uniq[@]}" 2>/dev/null
  for _ in $(seq 1 20); do port_up "$name" || break; sleep 1; done
  if port_up "$name"; then
    medrec_log "[$name] 20 秒后端口仍在应答，补 SIGKILL"
    kill -9 "${uniq[@]}" 2>/dev/null; sleep 2
  fi
  rm -f "$pf"
  port_up "$name" && medrec_log "[$name] ⚠ 端口 $want 仍在应答" || medrec_log "[$name] 已停"
}

stop_one() {
  case "$1" in
    qwen) medrec_log "[qwen] 委托 11-start-qwen.sh stop"; bash "$HERE/11-start-qwen.sh" stop ;;
    laya) medrec_log "[laya] 委托 12-start-laya.sh stop"; bash "$HERE/12-start-laya.sh" stop ;;
    ocr|ocrvl|qwen-small) stop_by_pid "$1" ;;
    *) medrec_log "不认识的服务：$1 （可选：$ALL）"; return 1 ;;
  esac
}

# 只接管、不停服务：把当前真正的监听进程写进 PID 记录，让后面的 stop/status 准确
adopt_one() {
  local name="$1" pf want p cmd got=""
  pf="$(pidf_of "$name")"; want="$(port_of "$name")"
  if ! has_listener "$name"; then
    # 服务没在跑：顺手清掉过期记录（否则 status 的 ⚠ 会一直刷、而且 refresh 永远清不掉）
    if [ -f "$pf" ]; then
      medrec_log "[$name] 未在跑（端口没人听）；清掉过期记录 $pf → 该服务需要 start"
      rm -f "$pf"
    else
      medrec_log "[$name] 未在跑（端口没人听），无需接管"
    fi
    return 0
  fi
  while read -r p; do
    [ -n "$p" ] || continue
    kill -0 "$p" 2>/dev/null || continue
    cmd="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null || echo '')"
    if has_opt_in "$cmd" "$want"; then got="$p"; break; fi
  done < <(listener_pids "$want")
  if [ -n "$got" ]; then
    echo "$got" > "$pf"
    medrec_log "[$name] 已接管 pid $got → $pf"
  else
    medrec_log "[$name] 端口 $want 在听，但没找到 cmdline 含该端口的进程 → 不写记录"
    ss -ltnp 2>/dev/null | grep ":$want " | sed 's/^/      /'
  fi
}

# ---------------------------------------------------------------- 展示
cmd_list() {
  printf '  %-11s %-6s %-9s %-11s %s\n' 服务 端口 GPU env 说明
  printf '  %-11s %-6s %-9s %-11s %s\n' \
    ocr "$PORT_OCR_PADDLE" "$GPU_OCR_PADDLE" "$(basename "$ENV_OCR")" "$(desc_of ocr)"
  printf '  %-11s %-6s %-9s %-11s %s\n' \
    ocrvl "$PORT_OCR_VL" "$GPU_VL_TP" "$(basename "$ENV_LLM")" "$(desc_of ocrvl)"
  printf '  %-11s %-6s %-9s %-11s %s\n' \
    qwen "$PORT_QWEN" "$GPU_QWEN_TP" "$(basename "$ENV_LLM")" "$(desc_of qwen)"
  printf '  %-11s %-6s %-9s %-11s %s\n' \
    laya "$PORT_LAYAD" "$GPU_LAYAD" "$(basename "$ENV_LAYAD")" "$(desc_of laya)"
  printf '  %-11s %-6s %-9s %-11s %s\n' \
    "$ORPHAN" "$QWEN_SMALL_PORT" "$QWEN_SMALL_GPU" "$(basename "$QWEN_SMALL_ENV")" "$(desc_of qwen-small)"
  echo
  echo "  （config.env 只管前四个；$ORPHAN 是遗留服务，参数见脚本头部）"
}

cmd_status() {
  local n pf pid st
  printf '  %-11s %-6s %-8s %-11s %s\n' 服务 端口 状态 PID 说明
  for n in $ALL; do
    pf="$(pidf_of "$n")"
    pid="$(cat "$pf" 2>/dev/null || echo -)"
    if [ "$pid" != "-" ] && ! kill -0 "$pid" 2>/dev/null; then pid="${pid} ✗记录过期"; fi
    if [ "$pid" = "-" ] && port_up "$n"; then pid="（未记录）"; fi
    if port_up "$n"; then st="就绪"; else st="未就绪"; fi
    printf '  %-11s %-6s %-8s %-13s %s\n' "$n" "$(port_of "$n")" "$st" "$pid" "$(desc_of "$n")"
  done
  echo
  echo "  pid 目录 $MEDREC_RUN   日志目录 $MEDREC_LOGS"
  # ⚠ 只对「PID 记录是死 pid **且** 端口有人在听」告警。
  #   原来只判前半句 → 服务本来就停着（端口没人听）时也会误报，而且 adopt_one
  #   在那种情况下不清记录，导致这行警告永远刷不掉。
  local stale="" p
  for n in $ALL; do
    p="$(cat "$(pidf_of "$n")" 2>/dev/null || echo '')"
    if [ -n "$p" ] && ! kill -0 "$p" 2>/dev/null && has_listener "$n"; then
      stale="$stale $n"
    fi
  done
  if [ -n "$stale" ]; then
    echo "  ⚠ 这些服务「PID 记录是死 pid、但端口有人在听」：$stale"
    echo "    → 跑一次 bash 13-svc-all.sh refresh 即可接管，之后 stop/status 才准"
  fi
  nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader | sed 's/^/  GPU /'
}

cmd_wait() {
  local secs="${WAIT_SECS:-300}" n
  for n in $ALL; do
    if port_up "$n"; then medrec_log "[$n] 已就绪"; continue; fi
    printf '  等 [%s] ' "$n"
    if wait_http "http://127.0.0.1:$(port_of "$n")$(probe_of "$n")" $((secs / 2)) >/dev/null 2>&1; then
      echo " 就绪"
    else
      echo " 超时（${secs}s）—— 看 $MEDREC_LOGS/$n.log"
    fi
  done
}

# ---------------------------------------------------------------- 腾 GPU 0
# 不靠配置猜：直接问 nvidia-smi「谁真的占着 GPU 0」，再用 PID 文件映射回服务名。
cmd_free_gpu0() {
  local gpu="${FREE_GPU:-0}" pids pid name found=""
  mapfile -t pids < <(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | tr -d ' ')
  # 需要「哪个 pid 在哪张卡」：用 gpu_uuid 关联
  local uuid
  uuid="$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader | awk -v i="$gpu" -F', ' '$1==i{print $2}')"
  local ongpu
  mapfile -t ongpu < <(nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader 2>/dev/null \
                       | awk -v u="$uuid" -F', ' '$1==u{print $2}')
  medrec_log "GPU $gpu 上现有 ${#ongpu[@]} 个进程；先看它们属于哪个已知服务"
  local n
  for n in $ALL; do
    pid="$(cat "$(pidf_of "$n")" 2>/dev/null || echo '')"
    [ -z "$pid" ] && continue
    for pid2 in "${ongpu[@]}"; do
      if [ "$pid" = "$pid2" ]; then
        medrec_log "  GPU $gpu 上的 pid $pid = [$n] → 停它"
        stop_one "$n"; found="$found $n"
      fi
    done
  done
  echo
  medrec_log "停掉:$found"
  echo "  停在 GPU $gpu 上的进程（若还有，说明它不在本脚本管理范围内，**不代你杀**）："
  nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader \
    | awk -v u="$uuid" -F', ' '$1==u{printf "    pid %s   %s MiB\n", $2, $3}'
  nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv
}

# ---------------------------------------------------------------- 入口
CMD="${1:-status}"; shift || true
if [ -n "${1:-}" ]; then NAMES="$*"; else NAMES="$ALL"; fi

# 只给已知名字，防打错字静默什么都没做
for n in $NAMES; do
  case " $ALL " in *" $n "*) ;; *) medrec_die "不认识的服务：$n （可选：$ALL）" ;; esac
done

case "$CMD" in
  list)    cmd_list ;;
  status)  check_proxy; cmd_status ;;
  refresh) check_proxy; for n in $NAMES; do adopt_one "$n"; done; echo; cmd_status ;;
  start)   check_proxy; for n in $NAMES; do start_one "$n"; done; sleep 2; echo; cmd_status ;;
  stop)    for n in $NAMES; do stop_one  "$n"; done; sleep 2; echo; cmd_status ;;
  restart) check_proxy; for n in $NAMES; do stop_one  "$n"; done
           for n in $NAMES; do start_one "$n"; done; sleep 2; echo; cmd_status ;;
  wait)    cmd_wait ;;
  free-gpu0) cmd_free_gpu0 ;;
  *)       sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
