<#
.SYNOPSIS
    一键启动文档识别界面。

.DESCRIPTION
    只需这一条命令。两个 VL 后端（native / vLLM）都由界面的代理层自动管理：
      - 用到哪个就自动拉起哪个（不用手动跑 serve.ps1）
      - 闲置 10 分钟自动释放显存
      - 两者互斥，启动一个会自动停掉另一个

    启动后浏览器会自动打开 http://127.0.0.1:8090

.PARAMETER Port
    界面端口，默认 8090。

.PARAMETER IdleTimeout
    VL 后端闲置多少秒后自动释放显存。0 = 不自动停。默认 600（10 分钟）。

.PARAMETER Prewarm
    启动时就把 vLLM 拉起来（后台进行），省掉第一次请求的等待。
    代价是它会一直占着约 7.5 GB 显存。

.PARAMETER NoOpen
    不自动打开浏览器。

.EXAMPLE
    .\start.ps1
    .\start.ps1 -Prewarm
    .\start.ps1 -Port 9000 -IdleTimeout 1800
#>
[CmdletBinding()]
param(
    [int]$Port = 8090,
    [int]$IdleTimeout = 600,
    [switch]$Prewarm,
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }

Say ""
Say "  文档识别 · 本地版" 'Cyan'
Say "  ────────────────────────────────────────────" 'DarkGray'

# ---------------------------------------------------------------- 1. 找解释器
$candidates = @(
    (Join-Path $Root '.venv_paddleocr\python.exe'),
    'D:\visonalbe\.venv_paddleocr\python.exe'
)
$py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) {
    Say "  ✗ 找不到虚拟环境里的 python.exe" 'Red'
    Say "    预期位置: $($candidates[0])" 'DarkGray'
    Say "    若环境已被删除，用 setup_env.ps1 重建。" 'DarkGray'
    exit 1
}
Say "  解释器   $py" 'DarkGray'

# ---------------------------------------------------------------- 2. 端口检查
$busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
if ($busy) {
    $proc = Get-Process -Id $busy.OwningProcess -ErrorAction SilentlyContinue
    $isOurs = $false
    try {
        $ci = Get-CimInstance Win32_Process -Filter "ProcessId=$($busy.OwningProcess)"
        $isOurs = $ci.CommandLine -match 'webui\.py'
    } catch { }

    if ($isOurs) {
        # 界面本来就在跑 —— 直接开浏览器，不重复启动
        Say "  界面已在运行（pid=$($busy.OwningProcess)），直接打开。" 'Yellow'
        if (-not $NoOpen) { Start-Process "http://127.0.0.1:$Port" }
        exit 0
    }
    Say "  ✗ 端口 $Port 被占用：pid=$($busy.OwningProcess) $($proc.ProcessName)" 'Red'
    Say "    换个端口:  .\start.ps1 -Port 8091" 'DarkGray'
    Say "    或结束它:  Stop-Process -Id $($busy.OwningProcess) -Force" 'DarkGray'
    exit 1
}

# ---------------------------------------------------------------- 3. 就绪后开浏览器
$url = "http://127.0.0.1:$Port"
if (-not $NoOpen) {
    Start-Job -ArgumentList $url -ScriptBlock {
        param($u)
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 2
            try {
                Invoke-RestMethod "$u/api/status" -TimeoutSec 3 | Out-Null
                Start-Process $u
                return
            } catch { }
        }
    } | Out-Null
}

# ---------------------------------------------------------------- 4. 启动
# 注意别用 $args —— 那是 PowerShell 的自动变量，覆盖它会踩坑
$cliArgs = @((Join-Path $Root 'webui.py'), '--port', $Port,
             '--vl-idle-timeout', $IdleTimeout)
if ($Prewarm) { $cliArgs += '--vl-prewarm' }

Say ""
Say "  界面地址 $url" 'Green'
Say "  空闲回收 $IdleTimeout 秒后自动释放显存" 'DarkGray'
Say "  停止方式 按 Ctrl+C" 'DarkGray'
Say ""
Say "  两个后端都会自动启停，不需要另开窗口跑 serve.ps1" 'DarkGray'
Say "  ⚠ native 与 vLLM 互斥（各需约 7.5 GB），启动一个会停掉另一个" 'DarkGray'
Say ""
Say "  ────────────────────────────────────────────" 'DarkGray'
Say ""

# 前台运行：日志直接打在这个窗口，Ctrl+C 即可停止
& $py @cliArgs
