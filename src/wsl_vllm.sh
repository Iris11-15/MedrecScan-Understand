#!/bin/bash
# WSL 侧启动 / 停止 vLLM 服务（PaddleOCR-VL-1.6）。
#
# 由 Windows 侧这样调用：
#   wsl -d Ubuntu-22.04 -u root -- bash /mnt/d/visonalbe/wsl_vllm.sh start
#
# 这套参数是踩了四个坑之后才跑通的，每一个都不能少：
#   ① VLLM_USE_V2_MODEL_RUNNER=0
#        vLLM 0.29 默认启用 V2 Model Runner，它需要 UVA 缓冲区；
#        而 WSL2 的 GPU 透传不支持 CUDA 统一内存 → "UVA is not available"
#   ② --attention-backend FLASH_ATTN
#        默认的 flashinfer 后端要调 nvcc JIT 编译内核，WSL 里没有 CUDA toolkit
#   ③ --enforce-eager
#        跳过 torch.compile / CUDA graph 捕获，减少出错面
#   ④ 系统依赖：gcc + python3-dev（Triton 要编译 C 扩展，需要 Python.h）
#        安装：apt-get install -y gcc g++ build-essential python3-dev
#
# 另外：首次请求要 100+ 秒（Triton 内核 JIT），之后每次约 0.2 秒。
# 所以"服务起来了"不等于"能快"，要打一次预热请求。

set -u

MODEL_DIR="${VLLM_MODEL_DIR:-/root/models/PaddleOCR-VL-1.6}"
PORT="${VLLM_PORT:-8118}"
SERVED_NAME="${VLLM_SERVED_NAME:-PaddleOCR-VL}"
MAX_LEN="${VLLM_MAX_LEN:-8192}"
GPU_UTIL="${VLLM_GPU_UTIL:-0.85}"
LOG="${VLLM_LOG:-/root/vllm.log}"
VLLM_BIN="${VLLM_BIN:-/root/vl-env/bin/vllm}"

ACTION="${1:-status}"

is_up() {
    curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1
}

case "$ACTION" in
  start)
    if is_up; then
        echo "vLLM 已在运行 (port ${PORT})"
        exit 0
    fi
    pkill -f "vllm serve" 2>/dev/null
    sleep 2
    : > "$LOG"
    export VLLM_USE_V2_MODEL_RUNNER=0
    export VLLM_USE_FLASHINFER_SAMPLER=0
    nohup "$VLLM_BIN" serve "$MODEL_DIR" \
        --served-model-name "$SERVED_NAME" \
        --trust-remote-code \
        --host 0.0.0.0 --port "$PORT" \
        --max-model-len "$MAX_LEN" \
        --gpu-memory-utilization "$GPU_UTIL" \
        --attention-backend FLASH_ATTN \
        --enforce-eager \
        >> "$LOG" 2>&1 &
    echo "已启动，PID=$!，日志 $LOG"
    echo "等待就绪 ..."
    for i in $(seq 1 100); do
        sleep 6
        if is_up; then
            echo "就绪，用时约 $((i*6))s"
            echo
            echo "注意：**首次真实请求会慢**（Triton 要按实际图像尺寸编译内核，约 100 秒），"
            echo "之后每次约 0.2 秒。预热必须用真实尺寸的图，用 1x1 占位图没用。"
            echo "让 layered.py 跑一次就会自然完成预热。"
            exit 0
        fi
        if ! pgrep -f "vllm serve" >/dev/null; then
            echo "进程已退出，错误尾部："
            grep -E "ERROR|RuntimeError|ImportError|nvcc|UVA" "$LOG" | tail -8
            exit 1
        fi
        echo "  ... $((i*6))s"
    done
    echo "超时（10 分钟）"; tail -10 "$LOG"; exit 1
    ;;

  stop)
    pkill -f "vllm serve" 2>/dev/null
    sleep 3
    if is_up; then echo "仍在运行"; exit 1; fi
    echo "已停止"
    ;;

  status)
    if is_up; then
        echo "运行中 (port ${PORT})"
        curl -s "http://127.0.0.1:${PORT}/v1/models" | head -c 200; echo
    else
        echo "未运行"
    fi
    ;;

  log)
    tail -"${2:-30}" "$LOG"
    ;;

  *)
    echo "用法: $0 {start|stop|status|log [行数]}"
    exit 1
    ;;
esac
