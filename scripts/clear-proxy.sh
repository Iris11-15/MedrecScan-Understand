#!/usr/bin/env bash
# =====================================================================
#  clear-proxy.sh — 代理体检 + 清理（默认只读，--clear 才动手）
#
#  为什么需要它：代理残留会让 conda/pip/模型下载全部变成「卡死」，
#  但报错往往只是 ProxyError 或者干脆没报错。这台机器的历史坑位里
#  conda-proxy-troubleshoot.md 已经写过一轮，这里是它的可执行版。
#
#  用法：
#    bash clear-proxy.sh              # 只诊断（只读，任何时刻可跑）
#    source clear-proxy.sh --clear    # 在当前 shell 里真清（★ 必须 source）
#    bash clear-proxy.sh --show       # 只看当前 shell 的 proxy 变量
#
#  ⚠️ --clear 必须在当前 shell 生效才有意义，所以要 `source`，
#     用 `bash clear-proxy.sh --clear` 清完就随子进程一起消失了。
# =====================================================================
set -uo pipefail

MODE="${1:---check}"
RC=0
ok()   { echo "  ✓ $*"; }
warn() { echo "  ! $*"; RC=1; }
bad()  { echo "  ✗ $*"; RC=2; }

# --------------------------------------------------------------- 诊断
show_env() {
  echo "── 1. 进程级环境变量（当前 shell）──"
  local found=0
  for v in http_proxy https_proxy ftp_proxy all_proxy no_proxy \
           HTTP_PROXY HTTPS_PROXY FTP_PROXY ALL_PROXY NO_PROXY; do
    local val; val="$(eval "printf '%s' \"\${$v:-}\"")"
    if [ -n "$val" ]; then printf '    %-12s = %s\n' "$v" "$val"; found=1; fi
  done
  [ "$found" -eq 0 ] && ok "没有任何 *_proxy 变量" || warn "存在代理变量（见上）"
}

show_files() {
  echo "── 2. 持久配置 ──"
  # conda
  local cv; cv="$(conda config --show proxy_servers 2>/dev/null | head -5)"
  if [ -n "$cv" ]; then
    if echo "$cv" | grep -qiE 'http|https'; then warn "conda 配了代理："; echo "$cv" | sed 's/^/      /'
    else ok "conda 未配代理"; fi
  fi
  # condarc 全文里的 proxy 行
  [ -f "$HOME/.condarc" ] && { grep -in proxy "$HOME/.condarc" 2>/dev/null | sed 's/^/      ~\/.condarc: /' || true; }
  # pip
  local pf=""
  for f in "$HOME/.config/pip/pip.conf" "$HOME/.pip/pip.conf" /etc/pip.conf; do
    [ -f "$f" ] && pf="$f"
  done
  if [ -n "$pf" ]; then
    if grep -qi 'proxy' "$pf"; then warn "pip 配了代理（$pf）："; grep -in proxy "$pf" | sed 's/^/      /'
    else ok "pip 未配代理（$pf）"; fi
  else
    echo "      （没有 pip.conf）"
  fi
  # git
  local gp; gp="$(git config --global --get http.proxy 2>/dev/null || true)"
  [ -n "$gp" ] && warn "git 配了代理：$gp" || ok "git 未配代理"
  # systemd 单元里的 Environment=*proxy*
  local su; su="$(grep -rl -i 'proxy' /etc/systemd/system/*.service 2>/dev/null | head -5 || true)"
  [ -n "$su" ] && { warn "systemd 单元里出现 proxy："; echo "$su" | sed 's/^/      /'; } || ok "systemd 单元无 proxy 配置"
}

show_netrc() {
  echo "── 3. ~/.netrc（conda 会把它当代理凭据读；损坏就报 ProxyError）──"
  if [ -f "$HOME/.netrc" ]; then
    warn "存在 ~/.netrc（权限 $(stat -c '%a' "$HOME/.netrc" 2>/dev/null)）"
    echo "      正常要求 0600；残留过期凭据时直接改名最省事： mv ~/.netrc ~/.netrc.bak"
  else
    ok "无 ~/.netrc"
  fi
}

show_no_proxy() {
  echo "── 4. no_proxy 是否放了本机地址（不放会把本机服务调用也塞进代理）──"
  local np; np="$(eval "printf '%s' \"\${no_proxy:-}\${NO_PROXY:-}\"")"
  if [ -z "$np" ]; then
    warn "no_proxy 为空 —— 若确实要用代理，必须含 localhost,127.0.0.1,::1"
    return
  fi
  local miss=0
  for h in localhost 127.0.0.1; do
    echo "$np" | grep -q "$h" || { bad "no_proxy 缺少 $h（当前：$np）"; miss=1; }
  done
  [ "$miss" -eq 0 ] && ok "no_proxy 含本机地址：$np"
}

probe() {
  echo "── 5. 直连探测（区分「代理捣乱」还是「网真的不通」）──"
  local targets=(
    "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main/noarch/repodata.json|tuna anaconda"
    "https://pypi.tuna.tsinghua.edu.cn/simple/|tuna pypi"
    "https://www.modelscope.cn/|modelscope"
  )
  for t in "${targets[@]}"; do
    local url="${t%%|*}" name="${t##*|}"
    local code; code="$(curl -s -o /dev/null -w '%{http_code}' -m 8 "$url" 2>/dev/null || echo 000)"
    if [ "$code" = "200" ]; then ok "$name 直连 $code"
    else warn "$name 直连 $code（若是 403，见本机 pip.ini 里清华被 BFD 拦过的记录）"; fi
  done
  echo "      口诀：直连 200 却报 ProxyError → 就是变量或 ~/.netrc，不是网"
}

# --------------------------------------------------------------- 清理
do_clear() {
  echo "── 清理（只影响当前 shell）──"
  unset http_proxy https_proxy ftp_proxy all_proxy
  unset HTTP_PROXY HTTPS_PROXY FTP_PROXY ALL_PROXY
  ok "已 unset 大小写共 10 个 *_proxy 变量"
  echo "      提示：no_proxy/NO_PROXY 保留无害；要一起清就 unset no_proxy NO_PROXY"
  echo
  echo "── 持久配置里的代理（按需手动执行，本脚本不自动改文件）──"
  echo "      conda : conda config --remove-key proxy_servers"
  echo "      pip   : pip config unset global.proxy   # 或注释 ~/.config/pip/pip.conf 的 proxy 行"
  echo "      git   : git config --global --unset http.proxy && git config --global --unset https.proxy"
  echo "      netrc : mv ~/.netrc ~/.netrc.bak"
  echo
  echo "── sudo / systemd 两个坑 ──"
  echo "      sudo 会清空所有 *_proxy → 用 sudo -E，或写 /etc/environment"
  echo "      systemd 不继承你的 shell → 在 unit 里显式 Environment=\"no_proxy=localhost,127.0.0.1,::1\""
}

# --------------------------------------------------------------- 主流程
case "$MODE" in
  --show)
    show_env ;;
  --check|"")
    echo "=============================================================="
    echo " 代理体检 · $(date '+%F %T') · $(hostname)"
    echo "=============================================================="
    show_env; show_files; show_netrc; show_no_proxy; probe
    echo
    echo "--------------------------------------------------------------"
    case "$RC" in
      0) echo " 结论：干净，无需处理" ;;
      1) echo " 结论：有代理配置。若下载/建环境正常就跑；若卡住，先 source clear-proxy.sh --clear" ;;
      2) echo " 结论：no_proxy 有问题或缺本机地址 → 必须修，否则连本机 8100/8102 都会被塞进代理" ;;
    esac
    echo " 清理：source clear-proxy.sh --clear"
    echo "--------------------------------------------------------------" ;;
  --clear)
    do_clear ;;
  *)
    echo "用法：bash clear-proxy.sh [--check|--show|--clear]（--clear 请用 source）"; exit 2 ;;
esac
exit "$RC"
