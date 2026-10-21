#!/usr/bin/env bash
# install.sh —— medrec round52 部署包一键安装（目标机 2×L40）
#
# 只做四件事，顺序固定（brief §4.2）：
#   1 preflight        环境检查，任一不过就停
#   2 unpack + verify  解包 + 按 manifest 逐文件 sha256 校验（先 assets 后 code）
#   3 env setup        conda 环境（默认代跑）/ 权重（assets 或 --weights-online 走 modelscope）/
#                      Strata（安装时从 GitHub 抓，包里不含）/ 配置落实 brief §3.5 七个陷阱
#   4 start + smoke    起三个服务 + 跑 2 例端到端冒烟
#
# ★ 本脚本不内联任何压测 / 扫参 / 调参（brief §6）。调参是 tune.sh 的职责，它只写 workspace/tuning.json。
# ★ 包内路径 → 目标机落点的映射只在 pkg_tool.py 的 GROUPS 里写一份，这里不重复定义。
set -euo pipefail

PKG_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
TOOL="$PKG_DIR/pkg_tool.py"
# ★ 坑（实测踩过）：bash 的 printf "%c" 34 输出的是字符串 "34" 的首字符 3，不是 chr(34)。
#   要真双引号必须走八进制转义：printf "\\042"。写错不会报错，只会把 config.env 的值写成 3xxx3（静默错）。
Q="$(printf "\\042")"
TAB="$(printf "\\011")"

CODE_PKG="${CODE_PKG:-$PKG_DIR/medrec-code-round52.tar.gz}"
ASSET_PKG="${ASSET_PKG:-$PKG_DIR/medrec-assets-round52.tar.gz}"
CODE_MAN="${CODE_MAN:-$PKG_DIR/medrec-code-round52.manifest.json}"
ASSET_MAN="${ASSET_MAN:-$PKG_DIR/medrec-assets-round52.manifest.json}"

MEDREC_ROOT="${MEDREC_ROOT:-$HOME/medrec}"
STRATA_HOME="${STRATA_HOME:-$HOME/Strata}"
STRATA_DATA="${STRATA_DATA:-$MEDREC_ROOT/strata-data}"
STRATA_FAMILY="${STRATA_FAMILY:-qwen}"
# ★ 量化档位：默认 IQ3_S（与源机一致，抽取质量最好）。STRATA_MODEL=auto 才按本机内存挑档，
#   挑中哪档会明打一行；不 auto 时内存不够就直接停 —— 不偷偷降档（降档=改抽取质量，红线⑦）。
STRATA_MODEL="${STRATA_MODEL:-IQ3_S}"
STRATA_CFG_USER="${STRATA_CFG:-}"
strata_cfg_path() { printf "%s/strata-%s.json" "$STRATA_HOME" "$(printf "%s" "$1" | tr "A-Z" "a-z")"; }
STRATA_CFG="${STRATA_CFG:-$(strata_cfg_path "$STRATA_MODEL")}"
STRATA_GGUF_DIR="${STRATA_GGUF_DIR:-}"
STRATA_VERSION="${STRATA_VERSION:-v0.1.40}"
STRATA_COMMIT="${STRATA_COMMIT:-1735d6471df29b42c26170efaac1f1446a58640f}"
STRATA_GIT_URL="${STRATA_GIT_URL:-https://github.com/Niko1221/Strata.git}"

# —— 权重与环境怎么到位（人的决定：assets 那条路不通就走网络下载权重）——
# WEIGHTS_ONLINE=1 ⇒ 不 unpack assets 里的两个模型组（models/ 与 paddlex/），
# 改在第 3 步用 modelscope CLI 现下到同一落点。smoke/ 与 wheels/ 照常解（冒烟要用样本）。
WEIGHTS_ONLINE="${WEIGHTS_ONLINE:-0}"
WEIGHTS_GB="${WEIGHTS_GB:-5}"        # --weights-online 时 preflight ④ 额外预留的权重体积（实测 assets 模型组解压后 4.5 GB）
MODELSCOPE_CACHE_DIR="${MODELSCOPE_CACHE_DIR:-$MEDREC_ROOT/models}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"   # 国内镜像，例 https://pypi.tuna.tsinghua.edu.cn/simple；空 = pip 默认源
CONDA_PY="${CONDA_PY:-3.12}"         # 源机 vl-env / ocr-env / qwen-env 实测都是 Python 3.12.14

# —— brief §3.5 七个陷阱里，属于配置的那几条（2×L40 拓扑：卡0=OCR，卡1=Strata）——
OCR_GPU="${OCR_VLLM_GPU:-0}"
OCR_GMU="${OCR_VLLM_GMU:-0.22}"
OCR_MAX_SEQS="${OCR_MAX_SEQS:-64}"
OCR_BATCHED_TOKENS="${OCR_BATCHED_TOKENS:-16384}"
STRATA_GPU="${STRATA_GPU:-1}"
# round54 P1-5：preflight 的驱动下限。包内 wheel 钉死 CUDA 13.0（qwen-env）+ CUDA 12.9
# （vl-env paddle），取最严的一条。换更老的 wheel 时才需要下调这个键。
DRV_MIN="${DRV_MIN:-580.65}"

# ★ 三个状态文件可被环境覆盖：演练时每个假根各带一份记账，回滚才不会把别的根的记账
#   当成本次安装的动作（实测：一份共享 ledger 让回滚打出 18 条 [KEEP] 噪音）。
#   目标机单根，不覆盖就用默认路径，行为不变。
STATE="${STATE:-$PKG_DIR/install.state}"
LEDGER="${LEDGER:-$PKG_DIR/install.ledger}"
MARK="${MARK:-$PKG_DIR/.installed}"
BAK="$PKG_DIR/backup/$(date +%Y%m%d-%H%M%S)"
TMPD="$(mktemp -d /tmp/medrec-install.XXXXXX)"
trap "rm -rf $TMPD" EXIT

DRY=0
MODE=""
ACTION="install"
INSTALL_ENVS="${INSTALL_ENVS:-1}"   # ★ 默认代跑 conda create + pip install（一键装环境）；--no-env 只打印命令
INSTALL_STRATA=1
START_SERVICES=1
FORCE_ASSETS=0
WITH_CRON=1
SMOKE_BATCH="${SMOKE_BATCH:-r52smoke}"
SMOKE_RUN_ROOT="${SMOKE_RUN_ROOT:-$MEDREC_ROOT/run}"

say()  { printf "%s\n" "$*"; }
ok()   { printf "[OK] %s\n" "$*"; }
bad()  { printf "[FAIL] %s\n" "$*"; }
note() { printf "[..] %s\n" "$*"; }
warn() { printf "[WARN] %s\n" "$*"; }
dry()  { printf "[DRY] %s\n" "$*"; }
step() { printf "\n===== %s =====\n" "$*"; }

pick_py() {
  local c
  for c in "${MEDREC_PY:-}" /home/ubuntu/anaconda3/envs/vl-env/bin/python "$(command -v python3 || true)"; do
    if [ -n "$c" ] && [ -x "$c" ]; then printf "%s" "$c"; return 0; fi
  done
  return 1
}

ledger() { # ledger <KIND> <path> [path2]
  if [ "$DRY" = 1 ]; then dry "ledger $*"; return 0; fi
  mkdir -p "$(dirname "$LEDGER")"
  if [ "$#" -ge 3 ]; then printf "%s\t%s\t%s\n" "$1" "$2" "$3" >> "$LEDGER";
  else printf "%s\t%s\n" "$1" "$2" >> "$LEDGER"; fi
  chmod 600 "$LEDGER"
}

state_get() { [ -f "$STATE" ] && grep -m1 "^$1=" "$STATE" | cut -d= -f2- || true; }
state_set() {
  if [ "$DRY" = 1 ]; then dry "state $1=$2"; return 0; fi
  if [ -f "$STATE" ] && grep -q "^$1=" "$STATE"; then
    grep -v "^$1=" "$STATE" > "$TMPD/state.tmp" || true
    mv "$TMPD/state.tmp" "$STATE"
  fi
  printf "%s=%s\n" "$1" "$2" >> "$STATE"
  chmod 600 "$STATE"
}

ensure_dir() { # 目录不存在时创建，并【逐级】记账（回滚只删自己建的）
  local d="$1" cur="$1" chain="" p
  [ -d "$d" ] && return 0
  if [ "$DRY" = 1 ]; then dry "mkdir -p $d"; ledger CREATED "$d"; return 0; fi
  # mkdir -p 会顺手建出父目录，只记叶子会漏 —— 假根实测：回滚后 src/ bin/ config/ 还在原地
  while [ ! -d "$cur" ]; do chain="$cur
$chain"; cur="$(dirname "$cur")"; done
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    mkdir -p "$p"; chmod 700 "$p"; ledger CREATED "$p"
  done <<< "$chain"
}

# ————————————————————————————— 1. preflight —————————————————————————————
preflight() {
  step "1/4 preflight（7 项，逐项 [OK]/[FAIL]）"
  local f=0 t=7 os_pretty os_id os_ver drv gpu_lines n_gpu n_l40 need avail_kb p who line

  # ① OS
  os_pretty=unknown; os_id=""; os_ver=""
  if [ -r /etc/os-release ]; then
    os_pretty="$(grep -m1 "^PRETTY_NAME=" /etc/os-release | cut -d= -f2- | tr -d "$Q" || true)"
    os_id="$(grep -m1 "^ID=" /etc/os-release | cut -d= -f2- | tr -d "$Q" || true)"
    os_ver="$(grep -m1 "^VERSION_ID=" /etc/os-release | cut -d= -f2- | tr -d "$Q" || true)"
  fi
  if [ "$os_id" = ubuntu ] && [ -n "$os_ver" ] && [ "$(printf "%s\n20.04\n" "$os_ver" | sort -V | head -1)" = "20.04" ]; then
    ok "① OS：$os_pretty（要求 Ubuntu 20.04+）"
  else
    f=$((f+1)); bad "① OS：$os_pretty / ID=$os_id VERSION_ID=$os_ver —— 只支持 Ubuntu 20.04+"
    say "       补救：换 Ubuntu 20.04/22.04；或明确在非 Ubuntu 上自测后再装"
  fi

  # ② 驱动
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    f=$((f+1)); bad "② 驱动：找不到 nvidia-smi"
    say "       补救：装 NVIDIA 驱动（本包钉死 CUDA 13.0/12.9 wheel，需要 >=${DRV_MIN}），装完重跑 preflight"
  else
    drv="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || true)"
    # round54 P1-5：以前只判「有没有版本号」——550.90.07 也算通过（DELIVERY §5 那个假
    # nvidia-smi 就是 550，preflight 7/7 绿），要到第 3 步建环境/起 vLLM 才炸。
    # 下限来自包内钉死的 wheel：env/install-qwen-env.txt:100 nvidia-cuda-runtime==13.0.96
    # （torch 2.13.0+cu130）与 env/install-vl-env.txt:219 nvidia-cuda-runtime-cu12==12.9.37
    # （paddlepaddle-gpu cu129）。取最严的那条：CUDA 13.0 ⇒ 驱动 >=580.65。
    if [ -z "$drv" ]; then
      f=$((f+1)); bad "② 驱动：nvidia-smi 存在但查不到版本号"
      say "       补救：确认 GPU 可被 nvidia-smi 枚举（lspci | grep -i nvidia）"
    elif [ "$(printf "%s\n%s\n" "$drv" "$DRV_MIN" | sort -V | head -1)" = "$DRV_MIN" ]; then
      ok "② 驱动：$drv（要求 >=$DRV_MIN，来自 CUDA 13.0 wheel）"
    else
      f=$((f+1)); bad "② 驱动：$drv 低于 $DRV_MIN —— 包内 wheel 是 CUDA 13.0/12.9，跑不起来"
      say "       补救：升到 >=$DRV_MIN（nvidia-driver-580-server 或更高），装完重跑 preflight"
      say "       为什么现在拦：不拦的话要到第 3 步 pip 装 wheel / 起 vLLM 才报"
      say "       「CUDA driver version is insufficient」，那时更难查"
    fi
  fi

  # ③ 卡数与型号：必须恰好 2 张、型号含 L40
  gpu_lines="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true)"
  n_gpu="$(printf "%s\n" "$gpu_lines" | grep -c "." || true)"
  n_l40="$(printf "%s\n" "$gpu_lines" | grep -c "L40" || true)"
  say "       GPU 列表：$(printf "%s" "$gpu_lines" | tr "\n" ";")"
  if [ "$n_gpu" = 2 ] && [ "$n_l40" = 2 ]; then
    ok "③ 卡：恰好 2 张且都是 L40 系列（brief §2.1 拓扑成立）"
  else
    f=$((f+1)); bad "③ 卡：检测到 $n_gpu 张，其中含 L40 字样 $n_l40 张 —— 本包按 2×L40 写死（卡0=OCR gmu 0.22，卡1=Strata）"
    say "       补救：确认就是 2 张 L40/L40S；其它配置请改 OCR_VLLM_GPU / OCR_VLLM_GMU / STRATA_GPU 环境变量后再装，并跑 tune.sh --sweep"
  fi

  # ④ 磁盘：需要 ≥（两个包体积 + 解压后体积）× 2 +（装 Strata 时）Strata 的落盘体积
  need=0
  for m in "$CODE_MAN" "$ASSET_MAN"; do
    if [ ! -f "$m" ]; then
      # ★ --weights-online 时允许没有 assets：权重改从 modelscope 下，磁盘按 WEIGHTS_GB 预留。
      #   不这么分就会「缺清单 = FAIL」，把本来能装的网络路线也一起堵死。
      if [ "$WEIGHTS_ONLINE" = 1 ] && [ "$m" = "$ASSET_MAN" ]; then
        warn "④ 磁盘：没有 assets 清单（$m）—— 按 --weights-online 处理，权重改从 modelscope 下"
        need=$((need + WEIGHTS_GB * 1024 * 1024))
        continue
      fi
      f=$((f+1)); bad "④ 磁盘：缺清单 $m"; say "       补救：把 manifest 和 tar.gz 放在同一目录；或走网络权重 bash install.sh --weights-online"; continue
    fi
    tb="$("$PY" "$TOOL" field "$m" total_bytes)"; ab="$("$PY" "$TOOL" field "$m" archive_bytes)"
    need=$((need + tb + ab))
  done
  # ★ Strata 不在包里（人的决定：安装时从 GitHub/HF 抓）⇒ 这块磁盘必须单独算，否则 preflight 放行、
  #   跑到第 3 步才在下载中途爆盘。源机实测 /raid5/strata-data = 86 GB
  #   （models 78 + mtp 6.5 + packs 1.5）；上游 setup.py 的 IQ3_S download_gb=83.6 ⇒ 默认预留 90 GB。
  # 单位纪律：need 是【字节】（manifest 的 total_bytes/archive_bytes），extra 直接算成【KB】，
  # 与 df 的 avail_kb 对齐。混单位实测翻出 1000 倍：15 GB 被打成 15,674 GB。
  extra=0
  [ "$INSTALL_STRATA" = 1 ] && extra=$(( ${STRATA_DISK_GB:-90} * 1024 * 1024 ))
  need_kb=$(( need / 1024 * 2 + extra ))
  need_gb=$(( need_kb / 1024 / 1024 )); pkg_gb=$(( need / 1024 * 2 / 1024 / 1024 )); strata_gb=$(( extra / 1024 / 1024 ))
  # ★ 别用 ${extra:+…}：extra=0 时它是【非空字符串 "0"】，条件成立 ⇒ 会打出「+ Strata 0 GB」
  strata_note=""; [ "$extra" -gt 0 ] && strata_note=" + Strata ${strata_gb} GB"
  probe_dir="$MEDREC_ROOT"
  # ★ 逐级向上找到【最近已存在】的祖先再 df。只退一级的旧写法在「MEDREC_ROOT 的父目录也还不存在」
  #   时直接 FAIL，报的是「df 查不到分区」—— 真机上没人会撞上，但解压后先 --dry-run 自检必然撞上。
  while [ ! -d "$probe_dir" ] && [ "$probe_dir" != "/" ]; do probe_dir="$(dirname "$probe_dir")"; done
  avail_kb="$(df -Pk "$probe_dir" 2>/dev/null | awk "NR==2 {print \$4}" || true)"
  if [ -z "$avail_kb" ]; then
    f=$((f+1)); bad "④ 磁盘：df 查不到 $MEDREC_ROOT 所在分区"
  elif [ "$avail_kb" -lt "$need_kb" ]; then
    f=$((f+1)); bad "④ 磁盘：$MEDREC_ROOT 可用 $((avail_kb / 1024 / 1024)) GB，需要 ≥ ${need_gb} GB（包+解压 ×2 = ${pkg_gb} GB${strata_note}）"
    say "       补救：换个大分区（MEDREC_ROOT=/data/medrec），或先清 run/ 与 workspace/ 的旧批次"
    [ "$extra" -gt 0 ] && say "       ★ Strata 那 ${strata_gb} GB 是因为它不在包里、安装时才下载；不要 Strata 就 --skip-strata（则只需 ${pkg_gb} GB），或 STRATA_DISK_GB= 改预留值"
  else
    ok "④ 磁盘：可用 $((avail_kb / 1024 / 1024)) GB ≥ 需要 ${need_gb} GB（包+解压 ×2 = ${pkg_gb} GB${strata_note}）"
  fi

  # ⑤ 端口
  # ★ 端口必须读 PORT_STRATA / PORT_OCR_VL / PORT_WEBUI（start-all.sh 与 config.env 用的就是这三个）；
  #   写死 8080/8110/8090 会让「换端口」的补救建议自相矛盾（改了照样 FAIL）。
  local busy=0
  P_STRATA_CHK="${PORT_STRATA:-8080}"
  P_VLLM_CHK="${PORT_OCR_VL:-8110}"
  P_WEB_CHK="${PORT_WEBUI:-8090}"
  for p in "$P_STRATA_CHK" "$P_VLLM_CHK" "$P_WEB_CHK"; do
    who="$(ss -lntpH "sport = :$p" 2>/dev/null | head -1 || true)"
    if [ -n "$who" ]; then busy=1; bad "⑤ 端口 $p 已被占用：$who"; fi
  done
  if [ "$busy" = 1 ]; then
    f=$((f+1)); say "       补救：停掉占用进程；或换端口 PORT_STRATA/PORT_OCR_VL/PORT_WEBUI（scripts/config.env 与 start-all.sh 都读这三个）"
  else
    ok "⑤ 端口：8080 / 8110 / 8090 都空闲"
  fi

  # ⑥ 外网探测 → 定 online / offline
  if curl -sI --max-time 5 https://www.modelscope.cn >/dev/null 2>&1; then
    MODE=online; ok "⑥ 外网：modelscope 可达 → online 模式"
  else
    MODE=offline; ok "⑥ 外网：modelscope 不可达 → offline 模式（模型走包里的 assets；Strata 例外，见下）"
  fi
  # ★ 权重路线要说人话：到底用包里的 4.5 GB，还是现下 4.5 GB。不写清就会出
  #   「装了半天没见模型」这种查半天的假象。
  if [ "$WEIGHTS_ONLINE" = 1 ]; then
    say "       权重路线：--weights-online ⇒ 不 unpack assets 的 models/ 与 paddlex/，第 3 步用 modelscope 现下（约 ${WEIGHTS_GB} GB）"
  elif [ -f "$ASSET_PKG" ]; then
    say "       权重路线：包内 assets（$(basename "$ASSET_PKG")）；装不上时改跑 bash install.sh --weights-online"
  else
    say "       权重路线：包里没有 assets ⇒ 必须 --weights-online（否则 ④ 会 FAIL）"
  fi
  # ★ 建 conda 环境要出 PyPI。不探它，第 3 步会卡在 pip 超时里，报错看着像网络问题其实是根本没通。
  if curl -sI --max-time 8 https://pypi.org/simple/ >/dev/null 2>&1; then
    say "       PyPI 可达：conda 环境可由 $([ -d "$PKG_DIR/env" ] && echo "$PKG_DIR/env/install-<env>.txt" || echo "wheels/freeze-<env>.txt") 装（默认代跑，--no-env 只打印命令）"
  else
    warn "PyPI 不可达：conda 环境装不出来（缺 vl-env/ocr-env/qwen-env 就跑不了产线）。补救：PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple，或另打离线 wheel 包"
  fi
  if curl -sI --max-time 5 https://github.com >/dev/null 2>&1; then
    say "       GitHub 可达：Strata 引擎与权重可在安装时抓取"
  else
    warn "GitHub 不可达：Strata 装不了（包里不含 Strata，按你的要求改成安装时抓）。补救：STRATA_GIT_URL 指镜像，或已有 GGUF 时 STRATA_GGUF_DIR=/path"
  fi

  # ⑦ conda
  if command -v conda >/dev/null 2>&1; then
    ok "⑦ conda：$(conda info --base 2>/dev/null || echo 未知base)"
  else
    f=$((f+1)); bad "⑦ conda：command -v conda 为空"
    say "       补救：预装 miniconda3（https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh），装完重开 shell 再跑"
  fi

  say ""
  say "preflight: $((t-f))/$t 项通过"
  [ "$f" = 0 ] || { say "preflight 未过 —— 停在这里，不解包（brief §4.2 第 1 步）"; return 1; }
  return 0
}

MARK_MATCH=0

# ————————————————————————— 2. unpack + verify —————————————————————————
selfcheck_pkg() { # 解包前自查：包内绝不能有 run/ workspace/ logs/ __pycache__ .bak（红线1）
  local a="$1" hits
  if [ ! -f "$a" ]; then
    bad "包不存在：$a"
    say "       补救：把 tar.gz 与同名 manifest.json 一起放进 $PKG_DIR"
    return 1
  fi
  hits="$(tar -tzf "$a" 2>/dev/null | grep -nE "(^|/)(run|workspace|logs|__pycache__|\.git)(/|$)|\.bak|~$" | head -20 || true)"
  if [ -n "$hits" ]; then
    bad "解包前自查失败：$(basename "$a") 内含禁运路径 —— 拒绝安装"
    printf "%s\n" "$hits" | sed "s/^/       /"
    say "       补救：重新打包（work/pack.py 的 EXCLUDE），不要手改 tar"
    return 1
  fi
  ok "解包前自查通过：$(basename "$a") 无 run/ workspace/ logs/ __pycache__ .bak"
  return 0
}

unpack_group() { # unpack_group <code|assets> <manifest> <archive>
  local want="$1" man="$2" a="$3" n i kind prefix root repl members cnt
  local trepl
  n="$("$PY" "$TOOL" group-count)"
  i=0
  while [ "$i" -lt "$n" ]; do
    kind="$("$PY" "$TOOL" group-field "$i" kind)"
    if [ "$kind" != "$want" ]; then i=$((i+1)); continue; fi
    prefix="$("$PY" "$TOOL" group-field "$i" prefix)"
    root="$("$PY" "$TOOL" group-field "$i" root)"
    repl="$("$PY" "$TOOL" group-field "$i" repl)"
    if [ "$WEIGHTS_ONLINE" = 1 ]; then
      case "$prefix" in
        medrec-assets/models/|medrec-assets/paddlex/)
          note "[SKIP] --weights-online：整组不解 $prefix（第 3 步用 modelscope 现下到同一落点）"
          i=$((i+1)); continue ;;
      esac
    fi
    # prefix 带尾斜杠、repl 不带 —— transform 必须把斜杠补回替换串，
    # 否则 medrec-assets/wheels/README.md 会解成 wheelsREADME.md（假根真装实测）
    trepl="${repl:+$repl/}"
    members="$TMPD/members.$i.txt"
    "$PY" "$TOOL" members "$man" "$prefix" > "$members"
    cnt="$(grep -c "." "$members" || true)"
    say "  组 $prefix → $root${repl:+/}$repl（$cnt 个文件）"
    if [ "$cnt" = 0 ]; then
      bad "组 $prefix 在 manifest 里没有任何文件成员 —— 包与 pkg_tool.py 的 GROUPS 不一致"
      say "       补救：确认 $man 是本轮 work/pack.py 生成的，不要手改 manifest"
      return 1
    fi
    if [ "$MARK_MATCH" = 1 ] && [ "$FORCE_ASSETS" = 0 ]; then
      note "[SKIP] .installed 里两个包的 sha256 与当前包一致 —— 整组跳过（brief §4.6 幂等）"
      i=$((i+1)); continue
    fi
    if [ "$MARK_MATCH" = 1 ] && [ "$FORCE_ASSETS" = 1 ]; then
      note "[..] --force-assets：本组重解，会覆盖安装期改过的配置（原件在 $BAK，改前先确认）"
    fi
    ensure_dir "$root"
    # 目录必须在 tar 之前建并记账：tar 会顺手建出父目录，之后 ensure_dir 看见已存在就不记，
    # 回滚就漏（假根实测：回滚后 src/ bin/ config/ 原地不动）
    while read -r d; do ensure_dir "$d"; done < <("$PY" "$TOOL" mkdirs "$man" "$prefix")
    if [ "$DRY" = 1 ]; then
      dry "tar -xzf $a -C $root --transform s|^$prefix|$trepl| --anchored -T $members"
    else
      if [ "$want" = code ]; then
        "$PY" "$TOOL" backup "$man" "$prefix" "$BAK" | grep "^BACKUP" >> "$LEDGER" || true
        chmod 600 "$LEDGER" 2>/dev/null || true
      fi
      # 成员清单只含文件：目录成员会让 tar 递归吃掉子孙 + 把裸目录名按原样解出（垃圾目录），
      # 实测 161 次 Not found in archive / 7 个文件缺失。空目录由 mkdirs 补。
      if ! tar -xzf "$a" -C "$root" --transform "s|^$prefix|$trepl|" --anchored -T "$members"; then
        bad "解包失败：组 $prefix（tar 非零退出）—— 包 sha 已验过，问题在解包命令而不是包"
        say "       补救：bash $PKG_DIR/install.sh --rollback 清掉半成品，再重跑 install"
        return 1
      fi
      # 解完立刻按 manifest 逐条查落点 —— 不等后面的整包 verify，报错才指得准
      if ! miss="$("$PY" "$TOOL" checkdests "$man" "$prefix" 2>&1)"; then
        bad "组 $prefix 解完后有文件不在位：$(printf "%s" "$miss" | tail -1)"
        printf "%s\n" "$miss" | grep "^MISSING" | head -5 | sed "s/^/       /"
        say "       补救：bash $PKG_DIR/install.sh --rollback 清掉半成品再重跑（别用 --force-assets 掩盖）"
        return 1
      fi
      ok "解出 $cnt 个文件：$prefix"
    fi
    i=$((i+1))
  done
  return 0
}

verify_pkg() { # 按 manifest 逐文件 sha256（任务书 §2）
  local man="$1" label="$2" allow out rc=0
  # 安装期被 patch_config 改写过的文件（config.env）按 PATCHED 计 —— 否则第二趟会把它报成包坏了
  allow="$TMPD/patched.$label.txt"; : > "$allow"
  if [ -f "$LEDGER" ]; then grep "^PATCHED" "$LEDGER" | cut -f2 > "$allow" || true; fi
  out="$("$PY" "$TOOL" verify "$man" "$allow" 2>&1)" || rc=$?
  printf "%s\n" "$out" | head -20 | sed "s/^/       /"
  if [ "$rc" = 0 ]; then ok "verify $label：$(printf "%s" "$out" | tail -1)"; return 0; fi
  bad "verify $label 未过：$(printf "%s" "$out" | tail -1)"
  say "       补救：先看上面是 MISSING 还是 SHA-DIFF —— MISSING=没解到位（--rollback 后重跑 install）；"
  say "             SHA-DIFF 只在安装期改过的键上=正常（已按 PATCHED 计）；其余才是包损坏，重传 tar.gz"
  return 1
}

link_models() { # brief §3.2：HF 路径必须到 snapshots/master 这一层
  local tgt="$MEDREC_ROOT/models/models/PaddlePaddle--PaddleOCR-VL-1.6/snapshots/master"
  local lnk="$MEDREC_ROOT/models/PaddleOCR-VL-1.6"
  # ★ --dry-run 一个文件都没解出来 ⇒ 存在性检查在 dry 里【必然】失败，报出来的「模型目录不存在」是假的，
  #   还会把整条 --dry-run 自检带成 rc=1。真装时才做这个检查。
  if [ "$DRY" = 1 ]; then dry "ln -s models/PaddlePaddle--PaddleOCR-VL-1.6/snapshots/master $lnk"; ledger CREATED "$lnk"; note "（--dry-run：模型目录存在性检查留到真装时做）"; return 0; fi
  if [ ! -d "$tgt" ]; then
    bad "OCR 模型目录不存在：$tgt"
    say "       vLLM 会报 HFValidationError: Repo id must be in the form... —— 这个报错等于「目录不存在」，不是模型名写错"
    say "       补救：确认 assets 已解到 $MEDREC_ROOT/models/models/，路径必须到 snapshots/master 这一层"
    return 1
  fi
  if [ ! -f "$tgt/config.json" ]; then
    bad "$tgt 里没有 config.json —— 十有八九是层级指到上一级目录（brief §3.2）"
    return 1
  fi
  if [ -e "$lnk" ] || [ -L "$lnk" ]; then note "软链已存在，跳过：$lnk"; return 0; fi
  ( cd "$MEDREC_ROOT/models" && ln -s "models/PaddlePaddle--PaddleOCR-VL-1.6/snapshots/master" "PaddleOCR-VL-1.6" )
  ledger CREATED "$lnk"
  ok "软链 $lnk → models/PaddlePaddle--PaddleOCR-VL-1.6/snapshots/master"
  return 0
}

apply_perms() { # 产物权限：文件 600 / 目录 700；原本可执行的脚本保留 700
  local dirs=("$MEDREC_ROOT/src" "$MEDREC_ROOT/bin" "$MEDREC_ROOT/scripts" "$MEDREC_ROOT/config" \
              "$MEDREC_ROOT/webui" "$MEDREC_ROOT/docs" "$MEDREC_ROOT/models" "$MEDREC_ROOT/smoke" \
              "$MEDREC_ROOT/wheels" "$HOME/.paddlex")
  local d n=0
  for d in "${dirs[@]}"; do
    [ -d "$d" ] || continue
    n=$((n+1))
    if [ "$DRY" = 1 ]; then dry "chmod -R（目录 700 / 文件 600，可执行文件保留）$d"; continue; fi
    find "$d" -type d -exec chmod 700 {} + 2>/dev/null || true
    find "$d" -type f -perm /111 -exec chmod 700 {} + 2>/dev/null || true
    find "$d" -type f ! -perm /111 -exec chmod 600 {} + 2>/dev/null || true
  done
  ok "权限已设：$n 个目录（文件 600 / 目录 700，可执行位保留）；未碰 run/ workspace/ logs/"
  return 0
}

unpack_verify() {
  step "2/4 unpack + verify（顺序：先 assets 后 code，brief §4.1）"
  local cs as csha asha
  cs="$("$PY" "$TOOL" check "$CODE_MAN" "$CODE_PKG" 2>&1)" || { bad "code 包自检不过：$cs"; return 1; }
  ok "code 包 sha256：$cs"
  as="$("$PY" "$TOOL" check "$ASSET_MAN" "$ASSET_PKG" 2>&1)" || { bad "assets 包自检不过：$as"; return 1; }
  ok "assets 包 sha256：$as"

  csha="$("$PY" "$TOOL" field "$CODE_MAN" archive_sha256)"
  asha="$("$PY" "$TOOL" field "$ASSET_MAN" archive_sha256)"
  MARK_MATCH=0
  if [ -f "$MARK" ] && grep -q "^code=$csha$" "$MARK" && grep -q "^assets=$asha$" "$MARK"; then
    MARK_MATCH=1
    note "已装过同样的两个包（$MARK）—— 解包整组跳过；要重解用 --force-assets"
  fi

  selfcheck_pkg "$ASSET_PKG" || return 1
  selfcheck_pkg "$CODE_PKG" || return 1

  unpack_group assets "$ASSET_MAN" "$ASSET_PKG" || return 1
  unpack_group code "$CODE_MAN" "$CODE_PKG" || return 1

  if [ "$DRY" = 1 ]; then dry "verify 两组 manifest（逐文件 sha256）"; else
    verify_pkg "$ASSET_MAN" assets || return 1
    verify_pkg "$CODE_MAN" code || return 1
  fi
  # --weights-online 时模型还没下，软链必须留到第 3 步之后，否则这里必 FAIL 且报错指向「目录不存在」
  if [ "$WEIGHTS_ONLINE" = 1 ]; then
    note "软链留到第 3 步权重下完之后再建（--weights-online）"
  else
    link_models || return 1
  fi
  apply_perms

  if [ "$DRY" = 1 ]; then dry "写 $MARK"; return 0; fi
  { printf "code=%s\n" "$csha"; printf "assets=%s\n" "$asha"; printf "at=%s\n" "$(date -Iseconds)"; \
    printf "medrec_root=%s\n" "$MEDREC_ROOT"; } > "$MARK"
  chmod 600 "$MARK"
  ok "已写幂等标记 $MARK"
  state_set unpack done
  return 0
}

# ————————————————————————— 3. env setup —————————————————————————
env_list_for() { # 优先用洗过的清单（包内 env/），没有就用 assets 解出来的 freeze 原样
  local e="$1"
  if [ -f "$PKG_DIR/env/install-$e.txt" ]; then printf '%s\n' "$PKG_DIR/env/install-$e.txt"; return 0; fi
  printf '%s\n' "$MEDREC_ROOT/wheels/freeze-$e.txt"
}

conda_envs() {
  local base e miss=0 list n
  base="$(conda info --base 2>/dev/null || true)"
  if [ -z "$base" ]; then warn "conda info --base 为空 —— 跳过建环境"; return 0; fi
  local pipargs=(--no-input --retries 5 --timeout 120)
  [ -n "$PIP_INDEX_URL" ] && pipargs+=(-i "$PIP_INDEX_URL")
  for e in vl-env qwen-env ocr-env; do
    if [ -x "$base/envs/$e/bin/python" ]; then ok "conda 环境 $e 已存在（$base/envs/$e）"; continue; fi
    miss=$((miss+1))
    list="$(env_list_for "$e")"
    bad "conda 环境 $e 不存在"
    say "       手工跑（两条）："
    say "         conda create -n $e -y python=$CONDA_PY"
    say "         conda run -n $e pip install -r $list"
  done
  if [ "$miss" = 0 ]; then state_set env done; return 0; fi
  if [ "$INSTALL_ENVS" = 1 ] && [ "$MODE" = online ]; then
    warn "缺 $miss 个环境：开始代跑（一键装环境；--no-env 改成只打印命令）。每个环境上百个包，慢是正常，别以为卡死"
    for e in vl-env qwen-env ocr-env; do
      [ -x "$base/envs/$e/bin/python" ] && continue
      list="$(env_list_for "$e")"
      if [ ! -f "$list" ]; then
        bad "清单不存在：$list"
        say "       补救：确认 assets 已解出 wheels/；或把包里的 env/install-$e.txt 与 install.sh 放在同级目录"
        return 1
      fi
      n="$(grep -cve '^[[:space:]]*#' -e '^[[:space:]]*$' "$list" || true)"
      if [ "$DRY" = 1 ]; then dry "conda create -n $e -y python=$CONDA_PY && conda run -n $e pip install ${pipargs[*]} -r $list（$n 条）"; continue; fi
      note "建 $e：python=$CONDA_PY，清单 $list（$n 条）$([ -n "$PIP_INDEX_URL" ] && echo "，镜像 $PIP_INDEX_URL")"
      conda create -n "$e" -y "python=$CONDA_PY" || { bad "conda create -n $e 失败"; return 1; }
      if ! conda run -n "$e" pip install "${pipargs[@]}" -r "$list"; then
        bad "pip install -r $list 失败（$e）—— 环境是半成品，产线跑不起来"
        say "       先试「只装清单、不解析依赖」（清单本身是源机 freeze 的闭包，通常能过）："
        say "         conda run -n $e pip install ${pipargs[*]} --no-deps -r $list"
        say "       再不通就换镜像：PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash $PKG_DIR/install.sh"
        return 1
      fi
      ledger CREATED "$base/envs/$e"
      ok "环境就绪：$e（$("$base/envs/$e/bin/python" -V 2>&1)）"
    done
    state_set env done
  else
    warn "缺 $miss 个 conda 环境：本次不代跑（--no-env，或 offline 模式）。按上面的命令手工装完再跑冒烟。"
  fi
  return 0
}

# ★ 权重走网络：assets 那条路不通时的第二条路。落点与 assets 完全一致 ⇒
#   link_models 的 snapshots/master 层级要求、vLLM 的模型路径都不用改。
weights_online() {
  if [ "$WEIGHTS_ONLINE" != 1 ]; then return 0; fi
  step "3b 权重下载（modelscope → $MEDREC_ROOT/models + $HOME/.paddlex）"
  if [ "$MODE" != online ]; then
    bad "--weights-online 但 ⑥ 探到 modelscope 不可达 —— 下不了权重"
    say "       补救：去掉 --weights-online 改用包内 assets；或确认能出 https://www.modelscope.cn"
    return 1
  fi
  local base msbin="" e pair id dst
  base="$(conda info --base 2>/dev/null || true)"
  for e in vl-env ocr-env qwen-env; do
    if [ -n "$base" ] && [ -x "$base/envs/$e/bin/modelscope" ]; then msbin="$base/envs/$e/bin/modelscope"; break; fi
  done
  if [ -z "$msbin" ]; then
    bad "找不到 modelscope CLI（三个 env 里都没有 envs/<env>/bin/modelscope）"
    say "       补救：先让第 3 步把 conda 环境建出来（去掉 --no-env），或 conda run -n vl-env pip install modelscope"
    return 1
  fi
  ok "modelscope CLI：$msbin"
  # id → 落点。前三个是 vLLM(8110) 与产线直接读的 HF 目录；后四个是 PaddleX 缓存
  # （缺了会在首次推理时现下：首请求慢几十秒且不报错 ⇒ 这里一次预热）。五个 id 都实测 GET 200。
  local pairs=(
    "PaddlePaddle/PaddleOCR-VL-1.6|$MEDREC_ROOT/models/models/PaddlePaddle--PaddleOCR-VL-1.6/snapshots/master"
    "PaddlePaddle/PP-DocLayoutV3|$MEDREC_ROOT/models/models/PaddlePaddle--PP-DocLayoutV3/snapshots/master"
    "PaddlePaddle/PP-OCRv5_mobile_rec|$MEDREC_ROOT/models/models/PaddlePaddle--PP-OCRv5_mobile_rec/snapshots/master"
    "PaddlePaddle/PP-DocLayoutV3|$HOME/.paddlex/official_models/PP-DocLayoutV3"
    "PaddlePaddle/PP-OCRv5_mobile_det|$HOME/.paddlex/official_models/PP-OCRv5_mobile_det"
    "PaddlePaddle/PP-LCNet_x1_0_doc_ori|$HOME/.paddlex/official_models/PP-LCNet_x1_0_doc_ori"
    "PaddlePaddle/PP-LCNet_x1_0_textline_ori|$HOME/.paddlex/official_models/PP-LCNet_x1_0_textline_ori"
  )
  for pair in "${pairs[@]}"; do
    id="${pair%%|*}"; dst="${pair##*|}"
    ensure_dir "$dst"
    if [ -d "$dst" ] && [ -n "$(ls -A "$dst" 2>/dev/null || true)" ]; then note "[SKIP] 已就位：$dst"; continue; fi
    if [ "$DRY" = 1 ]; then dry "$msbin download --model $id --local_dir $dst"; continue; fi
    note "下载 $id → $dst"
    if ! MODELSCOPE_CACHE="$MODELSCOPE_CACHE_DIR" "$msbin" download --model "$id" --local_dir "$dst"; then
      bad "权重下载失败：$id（$dst）"
      say "       补救：单独重跑 $msbin download --model $id --local_dir $dst；仍失败就改用包内 assets（去掉 --weights-online）"
      return 1
    fi
    find "$dst" -type d -exec chmod 700 {} + 2>/dev/null || true
    find "$dst" -type f -exec chmod 600 {} + 2>/dev/null || true
    ok "就位：$dst"
  done
  say "       未预热的 paddlex 缓存（PP-OCRv5_server_*、PP-OCRv6_*）：产线配置不用它们；真被调用时 PaddleX 自己下（要联网）"
  link_models || return 1
  state_set weights done
  return 0
}

strata_ram_gb() { # 上游 setup.py 的 MODELS 表（qwen 家族四档）：echo "<ram_gb> <download_gb>"
  case "$1" in
    Q2_0)    echo "48 66.4" ;;
    IQ2_XS)  echo "48 68.0" ;;
    IQ3_XXS) echo "60 75.8" ;;
    IQ3_S)   echo "62 83.6" ;;
    *)       echo "" ;;
  esac
}

strata_resolve_model() { # 定档 + 装得下判断。★ 只在 STRATA_MODEL=auto 时才自动降档，且明打一行
  local ram line want dl cand picked=""
  # MEMINFO 可覆盖：只为让「内存不够」这条分支能被测到（真机默认读 /proc/meminfo，行为不变）
  ram="$(awk "/MemTotal/ {print int(\$2 / 1024 / 1024)}" "${MEMINFO:-/proc/meminfo}" 2>/dev/null || echo 0)"
  [ -z "$ram" ] && ram=0
  if [ "$STRATA_MODEL" = auto ]; then
    for cand in IQ3_S IQ3_XXS IQ2_XS Q2_0; do
      line="$(strata_ram_gb "$cand")"; [ -z "$line" ] && continue
      want="${line% *}"; dl="${line#* }"
      if [ "$ram" -ge "$want" ]; then
        picked="$cand"
        note "Strata 自动定档：$cand（要 ${want} GB 内存 / ${dl} GB 下载；本机内存 ${ram} GB）"
        break
      fi
      say "       跳过 $cand：要 ${want} GB 内存，本机只有 ${ram} GB"
    done
    if [ -z "$picked" ]; then
      bad "Strata 四档都装不下：本机内存 ${ram} GB，最小的 Q2_0 也要 48 GB —— 停，不下权重"
      say "       补救：--skip-strata（只要 OCR+宽表），或加内存后重跑；档位表见 DELIVERY.md §1.6"
      return 1
    fi
    STRATA_MODEL="$picked"
  else
    line="$(strata_ram_gb "$STRATA_MODEL")"
    if [ -z "$line" ]; then
      warn "STRATA_MODEL=$STRATA_MODEL 不在已知四档里（Q2_0/IQ2_XS/IQ3_XXS/IQ3_S）—— 照原样交给 setup.sh"
      [ -z "$STRATA_CFG_USER" ] && STRATA_CFG="$(strata_cfg_path "$STRATA_MODEL")"
      return 0
    fi
    want="${line% *}"; dl="${line#* }"
    if [ "$ram" -lt "$want" ]; then
      bad "Strata 档位 $STRATA_MODEL 要 ${want} GB 主机内存、${dl} GB 下载，本机只有 ${ram} GB —— 停，不下那 ${dl} GB"
      say "       补救：STRATA_MODEL=IQ3_XXS bash install.sh（质量最接近的一档），或 STRATA_MODEL=auto 让它自己挑"
      return 1
    fi
    note "Strata 档位 $STRATA_MODEL：要 ${want} GB 内存 / ${dl} GB 下载（本机内存 ${ram} GB）"
  fi
  [ -z "$STRATA_CFG_USER" ] && STRATA_CFG="$(strata_cfg_path "$STRATA_MODEL")"
  return 0
}

strata_install() { # ★ 包里不含 Strata（人的决定）：安装时从 GitHub 抓
  if [ "$INSTALL_STRATA" = 0 ]; then note "--skip-strata：跳过 Strata"; return 0; fi
  strata_resolve_model || return 1
  if [ -x "$STRATA_HOME/engine/strata" ] && [ -f "$STRATA_CFG" ]; then
    ok "Strata 已存在：$STRATA_HOME（engine/strata + $(basename "$STRATA_CFG")）—— 跳过"
    state_set strata done
    return 0
  fi
  if [ "$MODE" != online ]; then
    bad "offline 模式装不了 Strata：包里按你的要求不含 Strata（引擎 + 46.84 GiB 权重都不在包内）"
    say "       补救 A：换到能出网的机器上跑 bash install.sh online（git clone + setup.sh 抓权重）"
    say "       补救 B：已有 GGUF 目录时 STRATA_GGUF_DIR=/path/to/gguf bash install.sh online（不再下载）"
    say "       补救 C：只要 OCR + 宽表、不要抽取 → STRATA 起不来时产线会在 s3 停，先别跑批"
    return 1
  fi
  note "抓取 Strata：$STRATA_GIT_URL（$STRATA_VERSION / $STRATA_COMMIT）→ $STRATA_HOME"
  if [ "$DRY" = 1 ]; then
    dry "git clone $STRATA_GIT_URL $STRATA_HOME && git -C $STRATA_HOME checkout $STRATA_VERSION"
    dry "cd $STRATA_HOME && bash setup.sh --family $STRATA_FAMILY --model $STRATA_MODEL --gpu $STRATA_GPU --data-dir $STRATA_DATA --context 131072 --kv int8 --yes --no-start --no-browser"
    return 0
  fi
  command -v git >/dev/null 2>&1 || { bad "找不到 git —— Strata 装不了（补救：apt-get install -y git）"; return 1; }
  if [ ! -d "$STRATA_HOME" ]; then
    git clone "$STRATA_GIT_URL" "$STRATA_HOME" || { bad "git clone 失败：$STRATA_GIT_URL（补救：STRATA_GIT_URL=https://ghfast.top/https://github.com/Niko1221/Strata.git）"; return 1; }
    ledger CREATED "$STRATA_HOME"
  fi
  git -C "$STRATA_HOME" fetch --all --tags >/dev/null 2>&1 || true
  git -C "$STRATA_HOME" checkout "$STRATA_VERSION" || { bad "checkout $STRATA_VERSION 失败"; return 1; }
  local got; got="$(git -C "$STRATA_HOME" rev-list -n1 HEAD 2>/dev/null || true)"
  if [ -n "$STRATA_COMMIT" ] && [ "$got" != "$STRATA_COMMIT" ]; then
    bad "Strata commit 不符：HEAD=$got 期望 $STRATA_COMMIT —— 停，不跑 setup.sh（版本漂移会静默改变引擎行为）"
    say "       补救：确认上游是否换了 tag；确认后 STRATA_COMMIT=<新值> 重跑"
    return 1
  fi
  ok "Strata 源码就位：$got"
  local args=(--family "$STRATA_FAMILY" --model "$STRATA_MODEL" --gpu "$STRATA_GPU" --data-dir "$STRATA_DATA" --context 131072 --kv int8 --yes --no-start --no-browser)
  [ -n "$STRATA_GGUF_DIR" ] && args+=(--gguf-dir "$STRATA_GGUF_DIR")
  note "setup.sh：下载 GGUF 权重（46.84 GiB @3.85 GiB/s）+ 建 .venv + 编引擎；架构 sm_89 与 L40 同族，预编译产物可直接用"
  ( cd "$STRATA_HOME" && bash setup.sh "${args[@]}" ) || { bad "Strata setup.sh 失败（日志在 $STRATA_HOME）"; return 1; }
  ledger CREATED "$STRATA_DATA"
  state_set strata done
  ok "Strata 安装完成"
  return 0
}

set_env_key() { # set_env_key <file> <KEY> <value>：有则替换，无则追加；改前必备份
  local f="$1" k="$2" v="$3"
  [ -f "$f" ] || { bad "配置文件不存在：$f"; return 1; }
  if [ "$DRY" = 1 ]; then dry "config.env: export $k=$v"; return 0; fi
  if grep -q "^export $k=" "$f"; then
    sed -i "s|^export $k=.*|export $k=$v|" "$f"
    note "config.env: $k ← $v"
  else
    printf "export %s=%s\n" "$k" "$v" >> "$f"
    note "config.env: 追加 $k=$v"
  fi
}

patch_config() { # brief §3.5 七个陷阱里属于配置的那几条
  local f="$MEDREC_ROOT/scripts/config.env"
  if [ ! -f "$f" ]; then warn "缺 $f —— 跳过配置落实"; return 0; fi
  if [ "$DRY" = 1 ]; then dry "备份 $f → $BAK/scripts/config.env"; else
    mkdir -p "$BAK/scripts"; cp -a "$f" "$BAK/scripts/config.env"; ledger BACKUP "$BAK/scripts/config.env" "$f"
  fi
  # 陷阱1：config.env:44 GPU_VL_TP=0 只是默认值，真钩子是 OCR_VLLM_GPU（10-start-ocr.sh:55）
  set_env_key "$f" OCR_VLLM_GPU "$OCR_GPU"
  # 陷阱2：GMU 0.7 是 24GB 卡（16.8GB）；48GB 的 L40 上 0.7 会吃到 33.6GB，挤掉同卡 Strata
  set_env_key "$f" OCR_VLLM_GMU "$OCR_GMU"
  # 陷阱4：线上 8110 实际是 --max-num-seqs 64 + batched-tokens 8192（AGENTS.md 写的 1 已过时）→ 本包按 round51 结论写 16384
  set_env_key "$f" OCR_VLLM_DETERMINISM "${Q}--max-num-seqs $OCR_MAX_SEQS --no-enable-prefix-caching --max-num-batched-tokens $OCR_BATCHED_TOKENS${Q}"
  # start-all.sh 自己拼 argv，读的是这两个键 —— 一并写死，防两条路径漂移
  set_env_key "$f" OCR_MAX_NUM_SEQS "$OCR_MAX_SEQS"
  set_env_key "$f" OCR_MAX_NUM_BATCHED_TOKENS "$OCR_BATCHED_TOKENS"
  ok "config.env 已落实陷阱 1/2/4（卡0=OCR、gmu $OCR_GMU、mns $OCR_MAX_SEQS、mbt $OCR_BATCHED_TOKENS）；备份 $BAK/scripts/config.env"
  ledger PATCHED medrec-code/scripts/config.env
  # 陷阱3：Strata 配置里的 gpu 数组（源机 [2,3] → 目标机 [1]）
  if [ -f "$STRATA_CFG" ]; then
    if [ "$DRY" = 1 ]; then dry "pkg_tool.py patchjson $STRATA_CFG gpu [$STRATA_GPU]"; else
      "$PY" "$TOOL" patchjson "$STRATA_CFG" gpu "[$STRATA_GPU]" || warn "改 $STRATA_CFG 的 gpu 失败（手工确认它是 [$STRATA_GPU]，brief §3.5 陷阱3）"
      ledger BACKUP "$STRATA_CFG.bak-round52" "$STRATA_CFG"
    fi
  else
    warn "陷阱3 未落实：$STRATA_CFG 不存在（Strata 没装成功）—— 装好后必须把 gpu 改成 [$STRATA_GPU]，否则它会去抢卡0"
  fi
  # 陷阱5/6：locate 先验键名不匹配 / 缓存无条件写 —— 只记账，不在安装时改代码（round51 草案待落地）
  warn "陷阱5（locate_pages.py:317-323 prior_row 用 case_id.split(__)[0] 精确匹配，拼音键 240/240 matched=False）与陷阱6（:624 只挡读、:1086 无条件写）本轮不改代码：见 DELIVERY §4"
  # 陷阱7：日志只增不轮转 → 见 log_rotation
  state_set config done
  return 0
}

log_rotation() { # 陷阱7：只能 copytruncate（执行器持 O_APPEND 的旧 fd，rename 会静默断流）
  local r="$MEDREC_ROOT/scripts/rotate-logs.sh"
  if [ "$DRY" = 1 ]; then dry "pkg_tool.py write-rotate $r"; dry "crontab 追加 rotate 行"; return 0; fi
  "$PY" "$TOOL" write-rotate "$r" || { warn "生成 $r 失败"; return 0; }
  [ -f "$r" ] && ledger CREATED "$r"
  ok "已生成 $r（copytruncate：cp → truncate -s 0 → gzip；阈值 MEDREC_LOG_MAX_BYTES 默认 512 MiB）"
  if [ "$WITH_CRON" = 0 ]; then note "--no-cron：不写 crontab（草案见 $PKG_DIR/drafts/logrotate-medrec.txt）"; return 0; fi
  # ★ crontab 按【uid】记账，不认 HOME 覆盖 —— 假根演练会把行写进真账号的 crontab
  local real_home; real_home="$(getent passwd "$(id -un)" 2>/dev/null | cut -d: -f6)"
  if [ -n "$real_home" ] && [ "${HOME%/}" != "${real_home%/}" ]; then
    note "演练根（HOME=$HOME ≠ 账号真实 home $real_home）：不写 crontab —— crontab 不认 HOME 覆盖，写了会污染本机"
    return 0
  fi
  if ! command -v crontab >/dev/null 2>&1; then warn "无 crontab 命令 —— 只出草案：$PKG_DIR/drafts/logrotate-medrec.txt"; return 0; fi
  mkdir -p "$PKG_DIR/drafts"
  printf "%s\n" "medrec 日志轮转草案（放 /etc/logrotate.d/medrec 需 root；本脚本不 sudo）" \
    "$MEDREC_ROOT/logs/*.log {" "    size 512M" "    rotate 12" "    copytruncate" "    compress" "    delaycompress" "    missingok" "    notifempty" "}" \
    > "$PKG_DIR/drafts/logrotate-medrec.txt"
  chmod 600 "$PKG_DIR/drafts/logrotate-medrec.txt"
  local cur="$TMPD/cron.cur" new="$TMPD/cron.new" line
  crontab -l > "$cur" 2>/dev/null || : > "$cur"
  if grep -q "rotate-logs.sh" "$cur"; then note "crontab 已有 rotate-logs.sh 行 —— 跳过"; return 0; fi
  cp -a "$cur" "$BAK/crontab.before" 2>/dev/null || : > "$BAK/crontab.before"
  line="*/30 * * * * MEDREC_LOG_MAX_BYTES=536870912 bash $MEDREC_ROOT/scripts/rotate-logs.sh $MEDREC_ROOT/logs/ocr_vllm.log $MEDREC_ROOT/logs/webui.log $MEDREC_ROOT/logs/strata.log >> $MEDREC_ROOT/logs/rotate.log 2>&1"
  cat "$cur" > "$new"
  printf "%s\n" "$line" >> "$new"
  if crontab "$new"; then ledger CRON "$BAK/crontab.before" "$new"; ok "已加 crontab（每 30 min 轮转）；原 crontab 备份 $BAK/crontab.before"; else warn "crontab 写入失败 —— 手工把 $PKG_DIR/drafts/logrotate-medrec.txt 或 rotate-logs.sh 挂上定时"; fi
  state_set rotate done
  return 0
}

# ————————————————————————— 4. start + smoke —————————————————————————
start_smoke() {
  step "4/4 start + smoke（起三服务 + 跑 2 例端到端）"
  if [ "$START_SERVICES" = 0 ]; then note "--no-start：跳过起服务与冒烟"; return 0; fi
  local rr="$SMOKE_RUN_ROOT" bd="$SMOKE_RUN_ROOT/$SMOKE_BATCH"
  if [ "$DRY" = 1 ]; then
    dry "bash $PKG_DIR/start-all.sh"
    dry "bash $MEDREC_ROOT/bin/medrec run $MEDREC_ROOT/smoke --batch $SMOKE_BATCH --run-root $rr --no-locate-cache"
    dry "pkg_tool.py csvshape $bd/06_table/wide.csv  → 期望 data_rows=2 cols=23"
    return 0
  fi
  bash "$PKG_DIR/start-all.sh" || { bad "start-all.sh 失败 —— 先看上面的 [FAIL] 与 $MEDREC_ROOT/logs/"; return 1; }
  if [ ! -d "$MEDREC_ROOT/smoke" ]; then warn "缺 $MEDREC_ROOT/smoke（冒烟样本没解出来）—— 跳过冒烟"; return 0; fi
  ensure_dir "$rr"
  note "冒烟：$MEDREC_ROOT/smoke（2 例合成假病历，非真实患者）→ 批次 $SMOKE_BATCH"
  bash "$MEDREC_ROOT/bin/medrec" run "$MEDREC_ROOT/smoke" --batch "$SMOKE_BATCH" --run-root "$rr" --no-locate-cache \
    || { bad "冒烟跑批非零退出 —— 产物在 $bd，先查 _pipeline_logs/"; return 1; }
  local csv="$bd/06_table/wide.csv"
  [ -f "$csv" ] || { bad "冒烟没产出 $csv"; say "       补救：medrec check $SMOKE_BATCH --run-root $rr 看阶段状态；日志 $bd/_pipeline_logs/"; return 1; }
  local shape; shape="$("$PY" "$TOOL" csvshape "$csv" 2>&1)"
  say "       $shape"
  if printf "%s" "$shape" | grep -q "data_rows=2" && printf "%s" "$shape" | grep -q "cols=23"; then
    ok "冒烟通过：wide.csv 行数=2 列数=23（brief §8 Part A 最后一条）"
  else
    bad "冒烟宽表不对：$shape（期望 data_rows=2 cols=23）"
    say "       补救：medrec doctor 看列定义版本；config/columns.yaml 若不是 23 列，说明 code 包版本不对"
    return 1
  fi
  state_set smoke done
  return 0
}

# ————————————————————————— rollback —————————————————————————
in_whitelist() { # 只允许删安装期自己创建的路径
  case "$1" in
    "$MEDREC_ROOT"/*|"$HOME/.paddlex"*|"$STRATA_HOME"*|"$STRATA_DATA"*) : ;;
    *) return 1 ;;
  esac
  [ "$1" = "$HOME" ] && return 1
  [ "$1" = "$MEDREC_ROOT" ] && return 1
  return 0
}

rollback() {
  step "rollback（停服务 → 删自建目录 → 恢复配置备份；不碰用户原物）"
  if [ -x "$PKG_DIR/stop-all.sh" ]; then bash "$PKG_DIR/stop-all.sh" || warn "stop-all.sh 没停干净"; fi
  if [ ! -f "$LEDGER" ]; then note "无记账文件 $LEDGER —— 没有可回滚的动作"; return 0; fi
  local n i line kind a b skipped=0 done=0
  local out=0 rb=0
  n="$(grep -c "." "$LEDGER" 2>/dev/null || true)"; [ -z "$n" ] && n=0
  # 上一次针对【本次 MEDREC_ROOT】的回滚点：它之前的记账已经撤过，不再重复撤（幂等）
  rb="$(awk -F'\t' -v r="$MEDREC_ROOT" '$1 ~ /^ROLLED_BACK/ && $3==r {last=NR} END{print last+0}' "$LEDGER")"
  [ -z "$rb" ] && rb=0
  i="$n"
  while [ "$i" -ge 1 ]; do
    if [ "$i" -le "$rb" ]; then break; fi
    line="$(sed -n "${i}p" "$LEDGER")"
    kind="$(printf "%s" "$line" | cut -f1)"
    a="$(printf "%s" "$line" | cut -f2)"
    b="$(printf "%s" "$line" | cut -f3)"
    case "$kind" in
      CREATED)
        if ! in_whitelist "$a"; then skipped=$((skipped+1)); out=$((out+1)); i=$((i-1)); continue; fi
        if [ "$DRY" = 1 ]; then dry "rm -rf $a"; else rm -rf -- "$a" && note "[RM] $a"; fi
        done=$((done+1)) ;;
      BACKUP)
        if [ ! -f "$a" ]; then warn "[SKIP] 备份缺失：$a"; i=$((i-1)); continue; fi
        if ! in_whitelist "$b"; then skipped=$((skipped+1)); out=$((out+1)); i=$((i-1)); continue; fi
        if [ "$DRY" = 1 ]; then dry "cp -a $a $b"; else cp -a "$a" "$b" && note "[RESTORE] $b ← $a"; fi
        done=$((done+1)) ;;
      CRON)
        if [ "$DRY" = 1 ]; then dry "crontab $a"; else crontab "$a" && note "[RESTORE] crontab ← $a"; fi
        done=$((done+1)) ;;
      PATCHED|ROLLED_BACK) : ;;   # 记账用：配置改写由 BACKUP 恢复撤销，这里不重复动
      *) warn "[SKIP] 未知记账类型：$line" ;;
    esac
    i=$((i-1))
  done
  if [ "$DRY" = 1 ]; then dry "rm -f $MARK $STATE"; return 0; fi
  rm -f "$MARK" "$STATE"
  ledger ROLLED_BACK "$(date +%s)" "$MEDREC_ROOT"
  say "rollback 完成：撤销 $done 项，跳过 $skipped 项（其中 $out 项属于其它 MEDREC_ROOT 的历史记账，不碰）"
  say "  记账 $LEDGER 保留并追加 ROLLED_BACK 标记；备份目录 $PKG_DIR/backup/ 全部保留"
  [ "$out" -gt 0 ] && say "  ★ 演练（多假根）时把 LEDGER/STATE/MARK 按根分开，回滚才只对本次根生效"
  return 0
}

# ————————————————————————— usage / main —————————————————————————
usage() {
  say "install.sh —— medrec 部署包一键安装（目标机 2×L40）"
  say ""
  say "用法：bash install.sh [子命令] [开关]"
  say ""
  say "子命令（缺省 = install）"
  say "  install        preflight → unpack+verify → env setup → start+smoke（brief §4.2 四步，顺序固定）"
  say "  preflight      只跑 7 项环境检查（OS/驱动/卡数型号/磁盘/端口/外网/conda），不写任何东西"
  say "  online         同 install，但强制按外网可达处理（Strata 从 GitHub 抓）"
  say "  offline        同 install，但强制离线：模型走包内 assets，Strata 会 [FAIL] 并给补救方案"
  say "  --rollback     停服务 + 删安装期自建目录（按 install.ledger 记账）+ 恢复配置备份"
  say "  --help         本帮助"
  say ""
  say "开关"
  say "  --dry-run      零副作用跑通全流程：只打印将执行的命令，不解包/不写文件/不起服务"
  say "  --no-env       不代跑 conda create + pip install，只打印命令（默认是【代跑】，一键装环境）"
  say "  --with-env     显式要求代跑（默认已开，留着兼容旧调用）"
  say "  --weights-online  权重不走包内 assets：跳过 assets 的 models/ 与 paddlex/ 两组，"
  say "                    第 3 步用 modelscope CLI 现下到同一落点（smoke/ 与 wheels/ 照常解）"
  say "  --skip-strata  跳过 Strata 抓取（只要 OCR + 宽表时用）"
  say "  --no-start     跳过第 4 步（不起服务、不跑冒烟）"
  say "  --force-assets 即使 .installed 显示 sha 一致也重解包"
  say "  --no-cron      不写 crontab，只出 logrotate 草案"
  say ""
  say "可覆盖的环境变量：MEDREC_ROOT STRATA_HOME STRATA_DATA STRATA_GGUF_DIR STRATA_VERSION STRATA_COMMIT STRATA_GIT_URL"
  say "                  STRATA_MODEL（默认 IQ3_S；auto = 按本机内存自动挑档，见 DELIVERY §1.6）STRATA_FAMILY MEMINFO"
  say "                  INSTALL_ENVS WEIGHTS_ONLINE WEIGHTS_GB MODELSCOPE_CACHE_DIR CONDA_PY PIP_INDEX_URL"
  say "                  OCR_VLLM_GPU OCR_VLLM_GMU OCR_MAX_SEQS OCR_MAX_NUM_BATCHED_TOKENS STRATA_GPU"
  say "                  CODE_PKG ASSET_PKG CODE_MAN ASSET_MAN MEDREC_PY SMOKE_BATCH SMOKE_RUN_ROOT"
  say "                  STATE LEDGER MARK（演练多根时按根分开记账；目标机单根不用动）"
  say ""
  say "★ 本脚本不调参。压测/扫参是 tune.sh 的职责（它只写 workspace/tuning.json，不改配置、不重启服务）。"
}

PY=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --help|-h|help) usage; exit 0 ;;
    preflight) ACTION="preflight" ;;
    install) ACTION="install" ;;
    online) ACTION="install"; MODE=online ;;
    offline) ACTION="install"; MODE=offline ;;
    --rollback) ACTION="rollback" ;;
    --dry-run) DRY=1 ;;
    --with-env) INSTALL_ENVS=1 ;;
    --no-env) INSTALL_ENVS=0 ;;
    --weights-online) WEIGHTS_ONLINE=1 ;;
    --skip-strata) INSTALL_STRATA=0 ;;
    --no-start) START_SERVICES=0 ;;
    --force-assets) FORCE_ASSETS=1 ;;
    --no-cron) WITH_CRON=0 ;;
    *) say "未知参数：$1"; say ""; usage; exit 2 ;;
  esac
  shift
done

PY="$(pick_py || true)"
if [ -z "$PY" ]; then say "[FAIL] 没有可用 python：试 MEDREC_PY=/path/to/python bash install.sh ..."; exit 2; fi

say "install.sh（pkg=$PKG_DIR）（python=$PY）（MEDREC_ROOT=$MEDREC_ROOT）（DRY=$DRY）"
if [ -f "$PKG_DIR/VERSION" ]; then
  rel="$(grep -m1 "^release=" "$PKG_DIR/VERSION" | cut -d= -f2-)"
  bld="$(grep -m1 "^built=" "$PKG_DIR/VERSION" | cut -d= -f2-)"
  say "发布 $rel（构建 $bld）"
else
  note "包内没有 VERSION 文件 —— 按开发版处理（正式包由 make_bundle.sh 生成 VERSION）"
fi
say "Strata 档位：STRATA_MODEL=$STRATA_MODEL（auto = 按本机内存自动挑；默认固定 IQ3_S）"
case "$ACTION" in
  preflight) preflight ;;
  rollback) rollback ;;
  install)
    if [ "$DRY" = 1 ]; then
      preflight || note "（--dry-run：preflight 未过也继续把后面的计划打印完 —— 本模式零副作用，目的就是让人看全流程）"
    else
      preflight
    fi
    unpack_verify
    conda_envs
    weights_online || warn "权重步骤未过 —— OCR(8110) 起不来，其余照常；详见上面的补救"
    strata_install || warn "Strata 步骤未过 —— 抽取（s3）会不可用，其余照常；详见上面的补救"
    patch_config
    log_rotation
    start_smoke
    say ""
    say "安装结束。下一步：bash $PKG_DIR/tune.sh --level L0 L1 L2（只读探测，产出 workspace/tuning.json）"
    ;;
esac
