#!/usr/bin/env bash
# =====================================================================
#  01-setup-envs.sh — 建三套互不干扰的 conda 环境
#
#  为什么必须三套（这不是洁癖，是硬冲突）：
#    · qwen-env 要 transformers>=5.8（Qwen3.8 的 config.json 是它写的）
#    · ocr-env  的 PaddleX 3.x 对 transformers 版本另有一套要求，两者不能同住
#    · laya-env 要 torch 2.14，而 vLLM 对 torch 有自己钉死的版本 ——
#      把 laya 装进 qwen-env 极可能把 vLLM 的 torch 顶掉，vLLM 会以 kernel 不匹配挂掉
#
#  用法：bash 01-setup-envs.sh            （幂等，可重复跑）
#         bash 01-setup-envs.sh --recreate  （先删后建）
# =====================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/config.env"
medrec_clean_path
medrec_mkdirs

RECREATE=0
[ "${1:-}" = "--recreate" ] && RECREATE=1

PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PADDLE_INDEX="${PADDLE_INDEX:-https://www.paddlepaddle.org.cn/packages/stable/cu126/}"
PADDLE_VER="${PADDLE_VER:-3.2.1}"
PY_VER="${PY_VER:-3.12}"

command -v conda >/dev/null 2>&1 || medrec_die "找不到 conda。若 PATH 被 fsl 抢占，先执行： export PATH=$CONDA_BASE/bin:\$PATH"

# =====================================================================
#  conda 预检：三个历史卡点，先把它们挡在门外
#   ① 卡在 repo.anaconda.com（国内直连慢/超时）→ 默认源也换镜像
#   ② restore_free_channel / pkgs/free 在频道列表里 → 求解空间爆炸（经典卡死原因）
#   ③ 经典 solver 解 python=3.12 → 慢几十倍 → 换 libmamba
# =====================================================================
CONDA_LOG="$MEDREC_LOGS/conda.log"
CONDA_CREATE_TIMEOUT="${CONDA_CREATE_TIMEOUT:-300}"   # 单次建环境超时（秒）
mkdir -p "$MEDREC_LOGS"

conda_preflight() {
  local rc=1
  # 源可达性：默认源与镜像各测一次（各 8 秒）
  local d m
  d=$(curl -s -o /dev/null -w '%{http_code}' -m 8 https://repo.anaconda.com/pkgs/main/noarch/repodata.json || echo 000)
  m=$(curl -s -o /dev/null -w '%{http_code}' -m 8 https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main/noarch/repodata.json || echo 000)
  medrec_log "源可达性：repo.anaconda.com=$d  tuna镜像=$m"
  if [ "$m" = "200" ]; then rc=0; else
    medrec_log "⚠ 镜像不可达。若这台机必须走代理，先 source clear-proxy.sh / 配好 no_proxy 再跑"
  fi

  # 频道配置：报告 + 处理 free 频道
  if grep -qE 'restore_free_channel|pkgs/free' "$HOME/.condarc" 2>/dev/null; then
    medrec_log "检测到 free 频道配置（restore_free_channel 或 pkgs/free）—— 这是解 python=3.12 时卡死的头号原因"
    medrec_log "  → 备份为 ~/.condarc.bak.$(date +%m%d-%H%M%S) 并去掉 free 频道"
    cp "$HOME/.condarc" "$HOME/.condarc.bak.$(date +%m%d-%H%M%S)" 2>/dev/null || true
    if command -v conda >/dev/null 2>&1; then
      conda config --remove-key restore_free_channel >/dev/null 2>&1 || true
      conda config --remove channels 'https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/free/' >/dev/null 2>&1 || true
      conda config --remove channels 'pkgs/free' >/dev/null 2>&1 || true
    fi
  fi

  # 默认源换镜像（仅在当前不通时才动，避免破坏用户自定义）
  if [ "$d" != "200" ] && [ "$m" = "200" ]; then
    medrec_log "默认源不通 → 把 default_channels 换成 tuna 镜像"
    conda config --set default_channels \
      '["https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main","https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r"]' \
      >/dev/null 2>&1 || medrec_log "  (设置失败，可手动改 ~/.condarc)"
  fi

  # solver 换 libmamba（解 3.12 环境从几分钟 → 十几秒）
  local sv
  sv="$(conda config --show solver 2>/dev/null | awk '{print $2}')"
  if [ "$sv" != "libmamba" ]; then
    medrec_log "当前 solver=$sv → 换 libmamba（经典 solver 是慢的主因）"
    conda install -n base -y -q conda-libmamba-solver >>"$CONDA_LOG" 2>&1 \
      || medrec_log "⚠ libmamba 安装失败（不致命，会继续用经典 solver —— 那就可能很慢）"
    conda config --set solver libmamba >/dev/null 2>&1 || true
  else
    medrec_log "solver 已是 libmamba ✓"
  fi
  conda clean -i -y >>"$CONDA_LOG" 2>&1 || true
  return $rc
}

# 建环境：失败/超时不再「静默卡死」，而是报警 + 自动退到 python venv
mk_env() {
  local name="$1" path="$2"
  if [ "$RECREATE" = "1" ] && [ -d "$path" ]; then
    medrec_log "删除旧环境 $name"
    conda env remove -y -p "$path" >/dev/null 2>&1 || rm -rf "$path"
  fi
  if [ -x "$path/bin/python" ]; then
    medrec_log "$name 已存在，跳过创建"
    return 0
  fi

  medrec_log "创建 $name (python $PY_VER) → $path   [日志：$CONDA_LOG，最长等 ${CONDA_CREATE_TIMEOUT}s]"
  local rc=0
  timeout "$CONDA_CREATE_TIMEOUT" conda create -y -p "$path" "python=$PY_VER" >>"$CONDA_LOG" 2>&1 || rc=$?
  if [ "$rc" -eq 0 ]; then
    medrec_log "  ✓ $name 建好"
    return 0
  fi

  # ---- 失败判读 ----
  medrec_log "✗ conda 建环境失败（退出码 $rc），最后 15 行日志："
  tail -15 "$CONDA_LOG" | sed 's/^/    /'
  if [ "$rc" -eq 124 ]; then
    medrec_log "  原因：超时 ${CONDA_CREATE_TIMEOUT}s —— 基本就是求解太慢或源不通"
    medrec_log "  先手动跑一次看它到底停在哪： conda create -y -p /tmp/_probe python=$PY_VER -v"
  fi

  # ---- 兜底：用系统 python 建 venv（不需要 conda 源）----
  local syspy=""
  for c in "python$PY_VER" python3.12 python3 python; do
    command -v "$c" >/dev/null 2>&1 && { syspy="$c"; break; }
  done
  if [ -n "$syspy" ] && "$syspy" -c 'import venv' 2>/dev/null; then
    medrec_log "兜底：用 $syspy -m venv 重建 $name（不依赖 conda 频道）"
    "$syspy" -m venv "$path" >>"$CONDA_LOG" 2>&1 && {
      medrec_log "  ✓ $name 已用 venv 建好（后续 pip 安装完全一致）"
      return 0
    }
  fi
  medrec_die "$name 建不起来。先修 conda 源（见 15-check.sh s0/s1），或手动： $syspy -m venv $path"
}

pip_in() {  # pip_in <env路径> <参数...>
  local py="$1/bin/python"; shift
  # 不再用 -q 静默：pip 的进度是「它还在干活 vs 真卡死」的唯一线索，落盘到 conda.log 备查
  "$py" -m pip install --no-input -i "$PIP_INDEX" "$@" >>"$CONDA_LOG" 2>&1 \
    || { medrec_log "✗ pip 安装失败：$*"; tail -15 "$CONDA_LOG" | sed 's/^/    /'; return 1; }
}

echo "=============================================================="
echo " 环境建设开始（conda base: $CONDA_BASE）"
echo "=============================================================="
medrec_log "conda 预检（源可达性 / free 频道 / solver）…"
conda_preflight || medrec_log "⚠ 预检有告警，继续尝试（失败时会给出兜底方案）"

# ---------------------------------------------------------------- OCR
echo
echo "── [1/3] ocr-env · PaddleX + 快路径 ─────────────────────────"
mk_env ocr-env "$ENV_OCR"
pip_in "$ENV_OCR" --upgrade pip setuptools wheel
# 铁律：paddlepaddle-gpu 绝不能加 --no-deps。
# 加了会跳过 7 个 nvidia-*-cu12 轮子 → GPU 失效，静默跑 CPU（历史上真踩过）。
medrec_log "安装 paddlepaddle-gpu ${PADDLE_VER}（cu126）"
"$ENV_OCR/bin/python" -m pip install --no-input \
  -i "$PADDLE_INDEX" "paddlepaddle-gpu==${PADDLE_VER}" >>"$CONDA_LOG" 2>&1 \
  || { tail -15 "$CONDA_LOG" | sed 's/^/    /'; medrec_die "paddlepaddle-gpu 安装失败。先确认 $PADDLE_INDEX 可达；不要把 --no-deps 当解法。"; }
pip_in "$ENV_OCR" paddlex "paddleocr>=3.0" pypdfium2 pillow requests psutil

# ★ 服务化插件必须单独装 —— 它不是 pip extra，而是 PaddleX 自带的安装子命令。
#   漏了这一步，`paddlex --serve` 直接抛：
#     DependencyError: The serving plugin is not available. Please install it properly.
#   （官方 Serving Deployment Guide §1.1 Install the Serving Deployment Plugin）
medrec_log "安装 PaddleX 服务化插件（paddlex --install serving）"
if "$PY_OCR" -m paddlex --install serving >>"$CONDA_LOG" 2>&1; then
  medrec_log "  ✓ serving 插件装好"
else
  medrec_log "  ⚠ 插件安装器失败，改用 pip 兜底（版本可能与 paddlex 不完全对齐）"
  pip_in "$ENV_OCR" fastapi "uvicorn[standard]" python-multipart starlette \
    || medrec_die "服务化依赖装不上 → A 路 PaddleX 产线起不来。日志：$CONDA_LOG"
fi
# 立刻验证：别等到起服务时才炸
if "$PY_OCR" -c "from paddlex.utils.deps import require_serving_plugin; require_serving_plugin()" 2>>"$CONDA_LOG"; then
  medrec_log "  ✓ serving 插件自检通过"
else
  medrec_die "serving 插件装了但自检不过 —— 起服务必然失败，先修这个。日志：$CONDA_LOG"
fi

# ---------------------------------------------------------------- LLM
echo
echo "── [2/3] qwen-env · vLLM（Qwen3.8-27B + PaddleOCR-VL 共用）───"
mk_env qwen-env "$ENV_LLM"
pip_in "$ENV_LLM" --upgrade pip setuptools wheel
medrec_log "安装 vLLM（会自动带上匹配的 torch）"
pip_in "$ENV_LLM" vllm
medrec_log "安装客户端依赖"
pip_in "$ENV_LLM" openai transformers huggingface_hub modelscope ninja

# vLLM 与 transformers 的版本必须自洽，装完立刻验
"$PY_LLM" - <<'PY' || exit 1
import sys, importlib
import torch, vllm, transformers
print(f"  torch        {torch.__version__}  cuda={torch.cuda.is_available()}  gpus={torch.cuda.device_count()}")
print(f"  vllm         {vllm.__version__}")
print(f"  transformers {transformers.__version__}")
if not torch.cuda.is_available():
    print("  ✗ torch 看不到 CUDA —— 不要继续，先修这个")
    sys.exit(1)
from packaging.version import Version
if Version(transformers.__version__) < Version("5.8.0"):
    print("  ✗ transformers < 5.8.0：Qwen3.8-27B 的 config 解析会出问题，vLLM 需 >= 5.8.0")
    sys.exit(1)
print("  ✓ 版本约束满足（transformers>=5.8）")
PY

# ninja 必须让子进程看得见（引擎初始化要现场 JIT 时找不到 ninja 会直接崩）
[ -x "$ENV_LLM/bin/ninja" ] || medrec_log "警告：qwen-env 里没有 ninja，脚本里已把 env/bin 前置到 PATH"

# ---------------------------------------------------------------- Laya
echo
echo "── [3/3] laya-env · Laya 决策模型 ───────────────────────────"
mk_env laya-env "$ENV_LAYAD"
pip_in "$ENV_LAYAD" --upgrade pip setuptools wheel
pip_in "$ENV_LAYAD" torch --index-url https://download.pytorch.org/whl/cu126
medrec_log "安装 laya[serve]"
pip_in "$ENV_LAYAD" "laya[serve]"

"$PY_LAYAD" - <<'PY' || exit 1
import sys, torch
try:
    import laya
    print(f"  laya {laya.__version__}")
except Exception as e:
    print(f"  ✗ import laya 失败: {e}"); sys.exit(1)
print(f"  torch {torch.__version__}  cuda={torch.cuda.is_available()}")
print("  ✓ Laya 环境就绪（CPU 也能跑，GPU 约 33ms/问）")
PY

echo
echo "=============================================================="
echo " 三套环境建完。请把下面这行存下来（后续排错要用）："
echo "   $PY_OCR -c 'import paddle;print(paddle.__version__)'"
echo "   $PY_LLM --version"
echo "   $PY_LAYAD -c 'import laya;print(laya.__version__)'"
echo "=============================================================="
