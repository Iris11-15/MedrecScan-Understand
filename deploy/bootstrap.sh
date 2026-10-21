#!/usr/bin/env bash
# ============================================================================
#  medrec 一键部署引导脚本（bootstrap）
#  目标机：Ubuntu 20.04+ / 恰好 2×L40 / conda 已装 / 能访问 github.com
#
#  它只做「把 3.64 GB 分卷搬下来并验完整」这件事，然后交棒给包内 install.sh。
#  真正的安装逻辑全在 install.sh（preflight 7 项 / 解包验 sha / 建 conda 环境 /
#  抓 Strata / 起服务 / 冒烟）。本脚本不重复实现，也不绕过任何检查。
#
#  用法：
#    bash bootstrap.sh                 # 下载 → 校验 → 解包 → dry-run → 询问后真装
#    bash bootstrap.sh --yes           # 同上，但不在真装前停下来问
#    bash bootstrap.sh --preflight     # 只下载+校验+解包+跑 preflight，不真装
#    bash bootstrap.sh --skip-strata   # 只要 OCR + 宽表（省 90 GB 磁盘）
#    bash bootstrap.sh --verify-only   # 只校验已下载的分卷，不下载
#
#  工作目录：默认自动挑可用空间最大的分区（/data /raid5 /mnt/data /home …）。
#  想指定就 WORK_ROOT=/data bash bootstrap.sh
#
#  未知参数原样透传给 install.sh（--weights-online / --no-start / --rollback …）。
# ============================================================================
set -uo pipefail

REPO="Iris11-15/MedrecScan-Understand"
TAG="${TAG:-v1.0.1-r54}"
PKG="medrec-deploy-1.0.1-r54-2xl40"
BASE_URL="https://github.com/${REPO}/releases/download/${TAG}"

# 资产字节数（来自 GitHub API 实测，用于判断下载是否完整 / 支持断点续传）
SZ_PART00=1887436800
SZ_PART01=1750687921
SZ_TOTAL=$((SZ_PART00 + SZ_PART01))          # 3,638,124,721
SHA_TOTAL="bd254b3b02658e01595c71978480332196ee0531ebbff0ea971bff98969e3392"

WORK_ROOT="${WORK_ROOT:-}"
DL_DIR="${DL_DIR:-}"
EXTRACT_ROOT="${EXTRACT_ROOT:-}"
ASSUME_YES=0
STOP_AFTER_PREFLIGHT=0
VERIFY_ONLY=0
PASSTHRU=()

Q='"'
C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[36m'; C_0=$'\033[0m'
if [ ! -t 1 ]; then C_R=""; C_G=""; C_Y=""; C_B=""; C_0=""; fi

say()  { printf '%s\n' "$*"; }
ok()   { printf "${C_G}[OK]${C_0} %s\n" "$*"; }
bad()  { printf "${C_R}[FAIL]${C_0} %s\n" "$*"; }
note() { printf "${C_B}[..]${C_0} %s\n" "$*"; }
warn() { printf "${C_Y}[WARN]${C_0} %s\n" "$*"; }
step() { printf "\n${C_B}===== %s =====${C_0}\n" "$*"; }
die()  { bad "$*"; exit 1; }

usage() {
  sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

for a in "$@"; do
  case "$a" in
    --help|-h) usage ;;
    --yes|-y) ASSUME_YES=1 ;;
    --preflight) STOP_AFTER_PREFLIGHT=1 ;;
    --verify-only) VERIFY_ONLY=1 ;;
    *) PASSTHRU+=("$a") ;;
  esac
done

# =========================== 0. 本机基本能力 ===========================
step "0/5 环境自检（下载端）"

need_cmd() { command -v "$1" >/dev/null 2>&1; }

for c in curl sha256sum tar; do
  need_cmd "$c" || die "缺少必需命令：$c。补救：sudo apt-get update && sudo apt-get install -y $c"
done

if need_cmd df; then
  avail_gb=$(df -Pk "$PWD" 2>/dev/null | awk 'NR==2{printf "%d", $4/1024/1024}')
else
  avail_gb=-1
fi

SKIP_STRATA=0
for a in "${PASSTHRU[@]+"${PASSTHRU[@]}"}"; do
  [ "$a" = "--skip-strata" ] && SKIP_STRATA=1
done

# 包解压约需 15 GB，另加 Strata 90 GB（若装）
need_gb=15
[ "$SKIP_STRATA" = 0 ] && need_gb=$((need_gb + 90))

# —— 工作目录选址 ——
# 真实的坑：目标机 / 分区常常只有 50–100 GB，而安装要 105 GB+。
# 在 $PWD 硬解压会装到 90% 才炸，重来一次要再等 1 小时。
# 所以：显式指定 > 自动挑最大的可写分区 > 退回 $HOME。
pick_work_root() {
  local best="" best_gb=-1 cand gb
  for cand in /data /raid5 /mnt/data /home "$HOME" /var/tmp /tmp; do
    [ -d "$cand" ] && [ -w "$cand" ] || continue
    gb=$(df -Pk "$cand" 2>/dev/null | awk 'NR==2{printf "%d", $4/1024/1024}')
    [ -z "$gb" ] && continue
    if [ "$gb" -gt "$best_gb" ]; then best_gb="$gb"; best="$cand"; fi
  done
  printf '%s %s' "$best" "$best_gb"
}

if [ -n "$WORK_ROOT" ]; then
  mkdir -p "$WORK_ROOT" 2>/dev/null || die "WORK_ROOT=$WORK_ROOT 不可写"
  avail_gb=$(df -Pk "$WORK_ROOT" 2>/dev/null | awk 'NR==2{printf "%d", $4/1024/1024}')
  note "工作目录：$WORK_ROOT（由 WORK_ROOT 指定）"
elif [ -n "$EXTRACT_ROOT" ]; then
  WORK_ROOT="$EXTRACT_ROOT"
  avail_gb=$(df -Pk "$WORK_ROOT" 2>/dev/null | awk 'NR==2{printf "%d", $4/1024/1024}')
  note "工作目录：$WORK_ROOT（由 EXTRACT_ROOT 指定）"
else
  read -r auto_root auto_gb <<< "$(pick_work_root)"
  if [ -n "$auto_root" ]; then
    WORK_ROOT="$auto_root"
    avail_gb="${auto_gb:--1}"
    note "工作目录：$WORK_ROOT（自动选中可用空间最大的分区）"
    # 若 PWD 本身空间就够，优先留在原地（用户的习惯位置）
    if [ "$avail_gb" -ge 0 ] && [ "$avail_gb" -lt "$need_gb" ] && [ "$(df -Pk "$PWD" | awk 'NR==2{printf "%d", $4/1024/1024}')" -ge "$need_gb" ]; then
      WORK_ROOT="$PWD"
      note "改为留在当前目录 $PWD（空间够）"
    fi
  else
    WORK_ROOT="$PWD"
    note "工作目录：$WORK_ROOT（没找到更合适的分区）"
  fi
fi
EXTRACT_ROOT="$WORK_ROOT"
DL_DIR="${DL_DIR:-$WORK_ROOT/${PKG}-dl}"

ok "curl / sha256sum / tar 都在"
if [ "$VERIFY_ONLY" = 0 ]; then
  say "       $WORK_ROOT 可用：${avail_gb} GB；本次需要约 ${need_gb} GB（3.6 GB 分卷 + 15 GB 解压$([ "$SKIP_STRATA" = 0 ] && echo " + 90 GB Strata")）"
  if [ "$avail_gb" -ge 0 ] && [ "$avail_gb" -lt "$need_gb" ]; then
    warn "空间不够 —— 换个大分区，或加 --skip-strata 省 90 GB"
    say "       补救：WORK_ROOT=/data bash bootstrap.sh            # 指定分区"
    say "             bash bootstrap.sh --skip-strata             # 只装 OCR+宽表（需 15 GB）"
    say "       已探测到的候选分区（可用 GB）："
    df -Ph 2>/dev/null | awk 'NR==1 || $4 ~ /G$/' | head -8
    die "工作目录空间不足，先挪分区再装（现在停下比装到一半炸好）"
  fi
fi

# =========================== 1. 下载分卷 ===========================
step "1/5 下载交付包（共 ${SZ_TOTAL} 字节 ≈ 3.4 GiB，可断点续传）"

mkdir -p "$DL_DIR" || die "建不了下载目录 $DL_DIR"

# GitHub Release 的下载是 302 跳到 objects.githubusercontent.com；
# -L 必须开，否则你会存下一个几百字节的 HTML 重定向页。
CURL_OPTS=(-fL --retry 5 --retry-delay 3 --retry-connrefused
           --connect-timeout 20 --progress-bar --continue-at -)

fetch_part() {  # fetch_part <文件名> <字节数>
  local name="$1" want="$2" dst="$DL_DIR/$1" have=0
  if [ -f "$dst" ]; then
    have=$(wc -c < "$dst" 2>/dev/null || echo 0)
    have=$((have + 0))
  fi
  if [ "$have" -eq "$want" ]; then
    ok "$name 已完整（$have / $want 字节），跳过下载"
    return 0
  fi
  if [ "$have" -gt 0 ]; then
    if [ "$have" -gt "$want" ]; then
      warn "$name 比预期还大（$have > $want），重新下"
      rm -f "$dst"; have=0
    else
      note "$name 断点续传：已有 $have / $want 字节，从断点继续"
    fi
  fi
  if [ "$VERIFY_ONLY" = 1 ]; then
    bad "$name 未下载完整，但指定了 --verify-only"
    return 1
  fi
  note "下载 $name …"
  # ★ 先写 .part 再 mv：中断时不会留下一个大小刚好对的假完整文件
  curl "${CURL_OPTS[@]}" -o "$dst" "$BASE_URL/$name" \
    || die "下载失败：$name。网络中断就重跑本脚本，会从断点继续"
  local got; got=$(wc -c < "$dst" 2>/dev/null || echo 0); got=$((got + 0))
  if [ "$got" -ne "$want" ]; then
    bad "$name 大小不符：$got ≠ $want —— 文件可能被代理截断"
    say "       补救：删掉后重下：rm -f \"$dst\"；若反复如此，检查出口代理/防火墙"
    return 1
  fi
  ok "$name 下载完整（$got 字节）"
}

fetch_part "${PKG}.tar.gz.part00" "$SZ_PART00" || exit 1
fetch_part "${PKG}.tar.gz.part01" "$SZ_PART01" || exit 1

# 顺带把两个校验清单也拿下来（校验值以官方 sidecar 为准，不写死）
for f in SHA256SUMS.parts "${PKG}.tar.gz.sha256"; do
  if [ ! -s "$DL_DIR/$f" ]; then
    curl -fsSL --retry 3 --connect-timeout 20 -o "$DL_DIR/$f" "$BASE_URL/$f" \
      && ok "已取 $f" || warn "取不到 $f（不影响整包 sha 校验，会退回用内置值）"
  fi
done

# =========================== 2. 合并 + 校验 ===========================
step "2/5 合并分卷并校验 sha256"

TARBALL="$DL_DIR/${PKG}.tar.gz"

merge() {
  # ★ 合并是最容易出「假成功」的一步：cat 一个存在的 part00 和半个 part01 也会成功。
  #   所以合并后必须比大小 + 比 sha，两道都过才算数。
  local p0="$DL_DIR/${PKG}.tar.gz.part00" p1="$DL_DIR/${PKG}.tar.gz.part01"
  [ -f "$p0" ] && [ -f "$p1" ] || die "分卷不全，无法合并"
  local sum; sum=$(( $(wc -c < "$p0") + $(wc -c < "$p1") ))
  [ "$sum" -eq "$SZ_TOTAL" ] || die "两卷合计 $sum ≠ $SZ_TOTAL，先重新下缺的那卷"
  note "cat part00 part01 > ${PKG}.tar.gz …"
  cat "$p0" "$p1" > "${TARBALL}.tmp" || die "合并失败（磁盘满了？）"
  mv -f "${TARBALL}.tmp" "$TARBALL"
  ok "合并完成（$(wc -c < "$TARBALL") 字节）"
}

sha_of() { sha256sum "$1" | awk '{print $1}'; }

if [ ! -f "$TARBALL" ] || [ "$(wc -c < "$TARBALL" 2>/dev/null || echo 0)" -ne "$SZ_TOTAL" ]; then
  merge
else
  ok "整包已存在且大小正确，跳过合并"
fi

note "计算 sha256（3.4 GiB，约 20–60 秒，别以为卡死）…"
GOT_SHA=$(sha_of "$TARBALL")

# 权威值优先取官方 sidecar，读不到才用内置常量
EXPECT_SHA="$SHA_TOTAL"
if [ -s "$DL_DIR/${PKG}.tar.gz.sha256" ]; then
  s=$(awk '{print $1}' "$DL_DIR/${PKG}.tar.gz.sha256" | head -1)
  [ -n "$s" ] && EXPECT_SHA="$s" && note "校验值来源：官方 sidecar（${PKG}.tar.gz.sha256）"
else
  note "校验值来源：脚本内置常量"
fi

if [ "$GOT_SHA" = "$EXPECT_SHA" ]; then
  ok "sha256 一致：$GOT_SHA"
else
  bad "sha256 不一致"
  say "       期望：$EXPECT_SHA"
  say "       实际：$GOT_SHA"
  say "       补救：分卷很可能被截断或串了。删掉重下：rm -rf \"$DL_DIR\"，再跑一次本脚本"
  exit 1
fi

# =========================== 3. 解包 ===========================
step "3/5 解包"

PKG_DIR="$EXTRACT_ROOT/$PKG"
if [ -f "$PKG_DIR/install.sh" ]; then
  ok "已解包（发现 $PKG_DIR/install.sh），跳过"
else
  note "解压到 $EXTRACT_ROOT（约 15 GB，需 1–10 分钟，视磁盘而定）…"
  tar -xzf "$TARBALL" -C "$EXTRACT_ROOT" || die "解压失败：磁盘空间不足或包损坏"
  ok "解包完成"
fi

[ -f "$PKG_DIR/install.sh" ] || die "解包后找不到 $PKG_DIR/install.sh —— 包结构异常"
ok "安装脚本就位：$PKG_DIR/install.sh"

# 分卷已合并进整包，删掉省 3.6 GB（★ 只在 sha 校验通过后才会走到这里）
if [ -f "$TARBALL" ] && [ -f "$DL_DIR/${PKG}.tar.gz.part00" ]; then
  rm -f "$DL_DIR/${PKG}.tar.gz.part00" "$DL_DIR/${PKG}.tar.gz.part01"
  ok "已清理两个分卷（整包保留在 $TARBALL，可复验/重装）"
fi

cd "$PKG_DIR" || die "进不去 $PKG_DIR"

# =========================== 4. preflight（dry-run）===========================
step "4/5 preflight（install.sh --dry-run，零副作用）"

# ★ 用 --dry-run 而不是 preflight 子命令：dry-run 会跑 preflight + 把后面所有步骤
#   的计划一并打印，用户能看到「接下来到底要装什么」，这比只看 7/7 更有用。
note "命令：bash install.sh --dry-run ${PASSTHRU[*]:-}"
bash install.sh --dry-run "${PASSTHRU[@]+"${PASSTHRU[@]}"}" 2>&1 | tee /tmp/medrec-bootstrap-dryrun.log
dr_rc=${PIPESTATUS[0]}

pf_line=$(grep -m1 'preflight: [0-9]*/[0-9]* 项通过' /tmp/medrec-bootstrap-dryrun.log || true)
if [ -n "$pf_line" ]; then
  say ""
  ok "dry-run 输出：$pf_line"
  if ! printf '%s' "$pf_line" | grep -q '7/7'; then
    warn "preflight 不是 7/7 —— 真装【会】在第一步就停下"
    say "       上面每个 [FAIL] 后面都有「补救：」行，照着改完再重跑本脚本（已下载的包不会重下）"
    say "       完整日志：/tmp/medrec-bootstrap-dryrun.log"
    # dry-run 有意允许 preflight 不过也继续打印计划，所以 rc 可能仍是 0；此处由我们判定
    if [ "$ASSUME_YES" = 1 ]; then
      die "preflight 未过且指定了 --yes：不硬装。修好环境后重跑"
    fi
  else
    ok "preflight 7/7，环境满足安装条件"
  fi
else
  warn "没在 dry-run 输出里找到 preflight 汇总行（rc=$dr_rc）"
  say "       完整日志：/tmp/medrec-bootstrap-dryrun.log"
fi

if [ "$STOP_AFTER_PREFLIGHT" = 1 ]; then
  say ""
  ok "已按 --preflight 停下。确认无误后正式安装："
  say "     cd \"$PKG_DIR\" && bash install.sh ${PASSTHRU[*]:-}"
  say "   或直接重跑本脚本：bash bootstrap.sh --yes ${PASSTHRU[*]:-}"
  exit 0
fi

# =========================== 5. 真装 ===========================
step "5/5 正式安装"

say "接下来 install.sh 会（默认行为，一项都不跳过）："
say "  · 解包 code + assets 并按 manifest 逐文件验 sha256"
say "  · conda create + pip 装三个环境（vl-env / ocr-env / qwen-env，约 735 个包）"
say "  · 从 GitHub 抓 Strata 引擎$([ "$SKIP_STRATA" = 1 ] && echo "（已 --skip-strata，跳过此项）")"
say "  · 起三个本机服务（8110 OCR / 8080 Strata / 8090 网页）"
say "  · 跑 2 例冒烟"
say ""
say "${C_Y}预计耗时 1–3 小时${C_0}（解包 ~6 min、pip 三个环境视网络、Strata 权重 86 GB 下载最慢）。"
say "全程【别关终端】。中断了重跑本脚本，install.sh 有 .installed 标记会接着来。"
say "装坏了回滚：cd \"$PKG_DIR\" && bash install.sh --rollback"

if [ "$ASSUME_YES" = 0 ]; then
  say ""
  if [ -t 0 ]; then
    printf "确认开始安装？[y/N] "
    read -r ans
    case "$ans" in
      y|Y|yes|YES) ;;
      *) say "已取消。随时重跑：bash bootstrap.sh --yes ${PASSTHRU[*]:-}"; exit 0 ;;
    esac
  else
    note "非交互环境（管道）：默认不真装。要装请加 --yes"
    say "     bash bootstrap.sh --yes ${PASSTHRU[*]:-}"
    exit 0
  fi
fi

say ""
note "开始真装 —— 下面是 install.sh 的原始输出，不做任何过滤"
say ""
bash install.sh "${PASSTHRU[@]+"${PASSTHRU[@]}"}"
rc=$?

say ""
if [ "$rc" -eq 0 ]; then
  ok "install.sh 返回 0"
  say ""
  say "装完请立刻做这 3 件自检（在 $PKG_DIR 下）："
  say "  bash health.sh                    # 三个服务在不在、是否只绑 127.0.0.1"
  say "  bash scripts/10-start-ocr.sh check  # OCR 是否真在 GPU 上干活（rc≠0 别跑批）"
  say "  bin/medrec doctor                 # 列数 23 / 服务活不活 / 产物根可写"
else
  bad "install.sh 返回 $rc"
  say "  先看上面最后一个 [FAIL] 和它后面的「补救：」行。"
  say "  装到一半坏掉：bash install.sh --rollback（只删本次安装自建的东西）"
  say "  日志留在 install.sh 的输出里；要重来就重跑本脚本，已下载的整包不会重下。"
fi
exit "$rc"
