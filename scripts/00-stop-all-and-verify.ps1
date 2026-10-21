<#
  00-stop-all-and-verify.ps1  —  阶段 0：停掉 4 张卡上的旧服务并核查机器现状
  在 Windows 本机执行，通过 SSH 操作服务器。

  用法：
    .\00-stop-all-and-verify.ps1 -Ssh ubuntu@10.0.0.5
    .\00-stop-all-and-verify.ps1 -Ssh ubuntu@10.0.0.5 -Port 22

  这个脚本只做两件事：
    ① 停服务、收显存（不删权重、不删 job 数据）
    ② 打出全量环境事实，供后续步骤照着填 config.env
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Ssh,
    [int]$Port = 22,
    [string]$OutDir = "$PSScriptRoot\_reports"
)

$ErrorActionPreference = 'Stop'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$report = Join-Path $OutDir "inventory-$stamp.txt"

function Invoke-Remote {
    param([string]$Script)
    # 用 UTF-8 传脚本，避免中文日志在 Windows→Linux 之间乱码
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Script)
    $b64 = [Convert]::ToBase64String($bytes)
    & ssh -p $Port -o ConnectTimeout=15 $Ssh "echo $b64 | base64 -d | bash -s"
}

$remoteScript = @'
set -u
echo "==================== 0. 基本信息 ===================="
hostname; uname -a; whoami; date -Is
echo
echo "==================== 1. GPU 现状 ===================="
nvidia-smi --query-gpu=index,name,driver_version,memory.total,memory.used,memory.free,utilization.gpu --format=csv
echo "--- driver / CUDA ---"
nvidia-smi | head -4
echo
echo "==================== 2. 谁在占卡（旧服务清单） ===================="
nvidia-smi --query-compute-apps=pid,used_memory,process_name --format=csv
echo "--- 端口监听 ---"
(for p in 7862 8000 8001 8002 8003 8090 8091 8100 8101 8102 8110; do
   printf "  %-6s " "$p"
   ss -ltnp 2>/dev/null | grep -q ":$p " && echo "LISTEN" || echo "-"
 done)
echo "--- 相关进程 ---"
pgrep -af 'vllm|paddlex|paddle|webui|gradio|uvicorn|laya|batch_run|structure.py|layered.py' || echo "  (无)"
echo
echo "==================== 3. 已有资产（不要误删） ===================="
du -sh ~/medrec/models 2>/dev/null || echo "  ~/medrec/models 不存在"
du -sh ~/medrec/jobs   2>/dev/null || echo "  ~/medrec/jobs 不存在"
du -sh ~/medrec/src    2>/dev/null || echo "  ~/medrec/src 不存在"
ls -d ~/medrec/models/* 2>/dev/null | head -20
echo "--- 磁盘 ---"
df -hT /home /tmp / 2>/dev/null
echo
echo "==================== 4. 工具链（决定 vLLM 能不能起） ===================="
which gcc g++ nvcc ninja ptxas 2>/dev/null || true
echo "--- nvcc 版本 ---"
nvcc --version 2>/dev/null | tail -2 || echo "  无 nvcc（vLLM 走预编译 wheel 时可以不装；要 JIT 就必须装）"
echo "--- ptxas 是否支持 --compress-mode（0 = 不支持，FlashInfer JIT 必失败）---"
ptxas --help 2>&1 | grep -c -- '--compress-mode' || true
echo
echo "==================== 5. conda 环境 ===================="
ls -d ~/anaconda3/envs/* 2>/dev/null || ls -d ~/miniconda3/envs/* 2>/dev/null || echo "  未找到 conda envs"
echo "--- PATH 里是否被 fsl/freesurfer 抢占（本机历史坑）---"
echo "$PATH" | tr ':' '\n' | grep -n -E 'fsl|freesurfer' | head -5 || echo "  PATH 干净"
echo "--- 裸 python 解析到哪 ---"
which python python3 pip 2>/dev/null || true
echo
echo "==================== 6. 限额与网络 ===================="
echo "nofile: soft=$(ulimit -Sn) hard=$(ulimit -Hn)"
echo "--- pip 镜像可达性 ---"
curl -s -o /dev/null -w "  tuna: HTTP %{http_code} in %{time_total}s\n" -m 8 https://pypi.tuna.tsinghua.edu.cn/simple/ || echo "  tuna 不可达"
curl -s -o /dev/null -w "  modelscope: HTTP %{http_code} in %{time_total}s\n" -m 8 https://www.modelscope.cn/ || echo "  modelscope 不可达"
echo
echo "==================== 7. 停服务（保留权重与数据） ===================="
echo "--- systemd 单元 ---"
echo "  （注意：这台机上也可能是你在跑 OCR；不在本平台清单里的 unit 先人工确认再停）"
systemctl list-units --type=service --all --no-pager 2>/dev/null | grep -iE 'medrec|vllm|paddle|qwen|ocr' || echo "  (无匹配单元)"
for u in $(systemctl list-units --type=service --all --no-pager 2>/dev/null | grep -oE '(medrec|vllm|paddle|qwen|ocr)[a-z0-9_.-]*\.service' | sort -u); do
  echo "  stop+disable $u"; sudo systemctl stop "$u" 2>/dev/null; sudo systemctl disable "$u" 2>/dev/null
done
echo "--- 普通进程 ---"
pkill -f 'vllm serve' 2>/dev/null && echo "  killed vllm" || true
pkill -f 'paddlex --serve' 2>/dev/null && echo "  killed paddlex" || true
pkill -f 'webui.py' 2>/dev/null && echo "  killed webui" || true
pkill -f 'laya-serve' 2>/dev/null && echo "  killed laya" || true
sleep 5
echo "--- 收尾后 GPU ---"
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv
echo "--- 残留占卡进程（这些必须清掉，否则新服务会被预申请直接顶死）---"
nvidia-smi --query-compute-apps=pid,used_memory,process_name --format=csv
echo
echo "==================== 8. 迁移清单（Windows→Linux 必改项） ===================="
grep -rn 'wsl.exe\|/mnt/c\|C:\\\\' ~/medrec/src/*.py 2>/dev/null | head -20 || echo "  ~/medrec/src 下无 WSL 残留（或目录不存在）"
echo
echo "==================== 9. 显存体检与风险判定 ===================="
echo "  （这一节的阈值都能改：GMU_MIN/GMU_MAX/GMU_SAFE 环境变量）"
GMU_MIN="${GMU_MIN:-0.40}"; GMU_MAX="${GMU_MAX:-0.80}"; GMU_SAFE="${GMU_SAFE:-0.90}"

echo "--- 9.1 每卡与驱动基线 ---"
# 只取「数字」各列，让 awk 自己取总量；避免在 shell 里做复杂引号嵌套
# 输出格式：index, total, used, free, percent
GPU_TSV="$(nvidia-smi --query-gpu=index,memory.total,memory.used,memory.free --format=csv,noheader,nounits | tr -d ' ')"
echo "$GPU_TSV" | awk -F',' -v gmin="$GMU_MIN" -v gmax="$GMU_MAX" -v gsafe="$GMU_SAFE" '
{
  idx=$1; total=$2; used=$3; free=$4;
  base = total - used - free;              # 驱动/OS 基线：vLLM 拿不到，但它按总量算账
  if (base < 0) base = 0;
  ratio = (total > 0) ? used / total : 0;
  printf "  GPU%-4s 总 %d MiB | 已用 %d | 空闲 %d | 基线 %d MiB (%.1f%%)\n", idx, total, used, free, base, (total>0 ? base*100/total : 0);
  if (ratio >= 0.30) {
    printf "        ✗ 该卡已被占用 %.0f%% —— vLLM 的 gpu-memory-utilization 是预申请，不够就直接失败\n", ratio*100;
  } else {
    printf "        ✓ 卡基本空闲；建议 gpu-memory-utilization ∈ [%.2f, %.2f]（%.2f 时预申请约 %d MiB/卡）\n", gmin, gmax, gmin, total*gmin;
  }
  if (total*(1-gsafe) < base + 500) {
    printf "        ! 提醒：设到 %.2f 会只剩约 %d MiB 给 CUDA context/图捕获（不在该预算内），别顶这么满\n", gsafe, total-base-int(total*gsafe);
  }
}'

echo "--- 9.2 有没有东西在占卡（含看不见的占用者）---"
APPS="$(nvidia-smi --query-compute-apps=pid,used_memory,process_name --format=csv,noheader 2>/dev/null)"
if [ -z "$APPS" ]; then
  echo "  ✓ 无计算进程占卡"
else
  echo "$APPS" | sed 's/^/    /'
  ORPHAN="$(echo "$APPS" | awk -F', *' '$3 ~ /\[Not Found\]|N\/A/ {print $1}')"
  [ -n "$ORPHAN" ] && echo "  ✗ 有「查不到进程名」的占卡 pid（僵尸/已退出）：$ORPHAN  → 按 pid kill -9 清掉"
fi

echo "--- 9.3 Xorg / 图形会话占卡判定 ---"
XSUM="$(nvidia-smi 2>/dev/null | grep -iE 'xorg|gnome-shell|wayland|/usr/lib/xorg' | awk '{for(i=1;i<=NF;i++) if($i ~ /MiB$/){gsub(/MiB/,"",$i); s+=$i}} END{print s+0}')"
echo "  图形进程占卡合计：${XSUM} MiB"
if [ "${XSUM:-0}" -le 64 ]; then
  echo "  ✓ 无活跃图形会话（<=64 MiB 属无头 X 的帧缓冲）→ 不需要停桌面，也不会挡新服务"
elif [ "${XSUM:-0}" -le 1024 ]; then
  echo "  ! 有图形会话在跑（数百 MiB）→ 不用杀它，但给 vLLM 的 GMU 留出这部分余量"
else
  echo "  ✗ 图形侧占用 ${XSUM} MiB（可能有浏览器/IDE 在渲染）→ 优先关掉图形里的重负载程序，别动 systemd 显示管理器"
fi

echo "--- 9.4 systemd-oomd 判定（它会连整个 user session 一起杀）---"
if systemctl is-active --quiet systemd-oomd 2>/dev/null; then
  echo "  systemd-oomd: active"
  SW_TOTAL="$(free -m | awk '/^Swap:/{print $2}')"
  SW_USED="$(free -m | awk '/^Swap:/{print $3}')"
  echo "  swap: 共 ${SW_TOTAL:-0} MiB / 已用 ${SW_USED:-0} MiB"
  if [ "${SW_TOTAL:-0}" -eq 0 ]; then
    echo "  ✓ 无 swap → 默认 oomd 规则无从触发"
  else
    PCT=$(( SW_USED * 100 / SW_TOTAL ))
    if [ "$PCT" -lt 50 ]; then
      echo "  ✓ swap 仅用 ${PCT}%（触发线 90%）→ 加载 27B 权重（mmap 读盘，压力低）安全"
    else
      echo "  ✗ swap 已用 ${PCT}% → 加载前先腾内存，否则 oomd 可能连带杀掉会话/正在加载的 vLLM"
    fi
  fi
  echo "  压力门限（参考）：PSI some avg60 > 60% 持续 30s，或 swap 使用率 > 90%"
  echo "  内存：$(free -g | awk '/^Mem:/{printf "总 %sG / 可用 %sG", $2, $7}')"
  journalctl -u systemd-oomd --since "7 days ago" 2>/dev/null | grep -ci kill | sed 's/^/  近 7 天 oomd kill 记录条数: /'
else
  echo "  ✓ systemd-oomd 未启用（或不存在）→ 无此风险"
fi
echo
echo "==================== 完成 ===================="
'@

Write-Host "→ 正在 $Ssh 上停服并采集现状..." -ForegroundColor Cyan
Invoke-Remote -Script $remoteScript | Tee-Object -FilePath $report
Write-Host ""
Write-Host "核查报告已保存：$report" -ForegroundColor Green
Write-Host "下一步：按报告里的 GPU 空载情况，先把它停干净，再填 deploy-v2/config.env" -ForegroundColor Yellow
