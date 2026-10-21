#!/usr/bin/env bash
# ===== conda 代理故障诊断（在 Ubuntu 服务器上执行，只读，不改配置）=====
echo "===== 1) 代理相关环境变量 ====="
env | grep -i -E 'proxy|_PROXY' | sort || echo "  （无任何 *_proxy 变量）"
echo "  注意：sudo 会清空这些变量，所以服务/sudo 场景要写进 sudoers 或 /etc/environment"

echo
echo "===== 2) conda / pip 自身配置里的代理 ====="
echo "--- conda config ---"
conda config --show proxy_servers ssl_verify 2>/dev/null || true
echo "--- ~/.condarc ---"
[ -f "$HOME/.condarc" ] && cat "$HOME/.condarc" || echo "  （无 ~/.condarc）"
echo "--- pip config ---"
pip config list 2>/dev/null || echo "  （pip config 为空）"
[ -f "$HOME/.pip/pip.conf" ] && cat "$HOME/.pip/pip.conf" || true
[ -f "$HOME/.config/pip/pip.conf" ] && cat "$HOME/.config/pip/pip.conf" || true

echo
echo "===== 3) .netrc（最常见的中毒源）====="
if [ -f "$HOME/.netrc" ]; then
  echo "  ⚠ 存在 ~/.netrc，内容如下（如有机器名/密码/奇怪的行要删干净）："
  sed -e 's/\(password[[:space:]]*\)[^[:space:]]*/\1***/I' "$HOME/.netrc"
  ls -l "$HOME/.netrc"      # conda 要求 0600，权限不对会报错
else
  echo "  （无 ~/.netrc，排除该原因）"
fi

echo
echo "===== 4) 直连 vs 经代理（curl 实测，关键一步）====="
BASE_TS="https://pypi.tuna.tsinghua.edu.cn/simple/"
BASE_BCE="https://paddle-whl.cdn.bcebos.com/"
echo "--- 直连清华源 ---"
curl -s -o /dev/null -w "  HTTP %{http_code}  用时 %{time_total}s\n" -m 8 "$BASE_TS" || echo "  直连失败"
echo "--- 直连 bcebos 轮子站 ---"
curl -s -o /dev/null -w "  HTTP %{http_code}  用时 %{time_total}s\n" -m 8 "$BASE_BCE" || echo "  直连失败"
if [ -n "$http_proxy$HTTP_PROXY" ]; then
  P="${http_proxy:-$HTTP_PROXY}"
  echo "--- 经代理 $P 访问清华源 ---"
  curl -s -x "$P" -o /dev/null -w "  HTTP %{http_code}  用时 %{time_total}s\n" -m 8 "$BASE_TS" || echo "  经代理失败"
fi

echo
echo "===== 5) conda 的信息源配置 ====="
conda config --show channels channel_alias default_channels 2>/dev/null || true

echo
echo "===== 6) 结论提示 ====="
echo "  · 第 4 步哪条返回 200，就用那条路走（直连 200 -> 清掉代理；只有代理 200 -> 保留代理但统一配置）"
echo "  · 第 3 步若有 .netrc，先改名：mv ~/.netrc ~/.netrc.bak  再重试 conda"
echo "  · sudo 场景代理会丢，需要 sudo -E 或写进 /etc/environment"
