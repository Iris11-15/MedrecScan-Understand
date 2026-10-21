#!/usr/bin/env bash
# ===== 网络可达性判定（在服务器上执行，只读）=====
# 目的：区分「DNS 不解析」与「完全没有出口路由」。这两种处置完全不同。

echo "===== 1) DNS 能否解析 ====="
getent hosts pypi.tuna.tsinghua.edu.cn || echo "  ✗ 域名不解析（DNS 问题）"
getent hosts mirrors.aliyun.com        || echo "  ✗ 域名不解析"
cat /etc/resolv.conf 2>/dev/null | grep -v '^#' | grep -v '^$' || true
echo "  ↑ 看看 nameserver 指向哪个 DNS（内网 DNS 拦外网域名很常见）"

echo
echo "===== 2) 有没有默认路由 / 网关 ====="
ip route 2>/dev/null | head -5 || route -n 2>/dev/null | head -5 || echo "  ✗ 取不到路由表"

echo
echo "===== 3) 用 IP 直连（绕过 DNS，判断路由是否存在）====="
# 清华 TUNA 的解析 IP（可能变，仅为探测路由用）
curl -s -o /dev/null -w "  pypi.tuna IP  %{http_code}  %{time_total}s\n" -m 8 https://101.6.15.130/simple/ \
  || echo "  ✗ IP 直连失败（无出口路由）"
# 223.5.5.5 是阿里公共 DNS
ping -c 2 -W 2 223.5.5.5 >/dev/null 2>&1 && echo "  ICMP 223.5.5.5 通" || echo "  ✗ ICMP 223.5.5.5 不通"

echo
echo "===== 4) 内网是否存在可用的 pip / conda 镜像 ====="
for u in http://mirrors.local http://pypi.mirrors.local http://nexus.local \
         http://mirrors.intra http://pypi.intra; do
  code=$(curl -s -o /dev/null -w "%{http_code}" -m 3 "$u" 2>/dev/null)
  [ "$code" != "000" ] && echo "  ★ 可能可用: $u -> $code" || echo "    $u -> 不可达"
done
echo "  提示：很多单位内网有 Nexus/Artifactory，问一下运维能省掉整套离线流程"

echo
echo "===== 5) 能否 ping 通网关 ====="
GW=$(ip route 2>/dev/null | awk '/default/ {print $3; exit}')
[ -n "$GW" ] && (ping -c 2 -W 2 "$GW" >/dev/null 2>&1 && echo "  ✓ 网关 $GW 可达（说明只是外网被封）" \
                 || echo "  ✗ 网关 $GW 不可达（链路/网卡问题）") || echo "  （无默认网关）"

echo
echo "===== 判定 ====="
echo "  · 第 1 步不解析 + 第 3 步 IP 能连   -> DNS 问题：改 /etc/hosts 或换 DNS，外网镜像还能用"
echo "  · 第 3 步 IP 也连不通               -> 无外网出口：走离线安装（情形 D）"
echo "  · 第 4 步发现内网镜像               -> 优先用内网镜像，比离线省事得多"
