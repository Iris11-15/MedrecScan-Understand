<#
.SYNOPSIS
    停止文档识别界面，并释放两个 VL 后端占用的显存。

.PARAMETER KeepBackends
    只停界面，保留后端（下次启动会快一些，但显存仍被占着）。

.EXAMPLE
    .\stop.ps1
    .\stop.ps1 -KeepBackends
#>
[CmdletBinding()]
param([switch]$KeepBackends)

$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot
function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }

Say ""
Say "  停止文档识别" 'Cyan'
Say "  ────────────────────────────────────────────" 'DarkGray'

# ---------------------------------------------------------------- 1. 界面进程
$killed = 0
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'webui\.py' } |
    ForEach-Object {
        Say "  停止界面  pid=$($_.ProcessId)" 'DarkGray'
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }
if ($killed -eq 0) { Say "  界面本来就没在跑" 'DarkGray' }

# ---------------------------------------------------------------- 2. 两个后端
if ($KeepBackends) {
    Say "  保留后端（-KeepBackends）" 'Yellow'
} else {
    $py = @(
        (Join-Path $Root '.venv_paddleocr\python.exe'),
        'D:\visonalbe\.venv_paddleocr\python.exe'
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if ($py) {
        foreach ($be in 'vllm', 'paddle') {
            try {
                & $py (Join-Path $Root 'vl_service.py') $be stop 2>&1 |
                    Select-Object -Last 1 |
                    ForEach-Object { Say "  $_" 'DarkGray' }
            } catch {
                Say "  停止 $be 时出错: $_" 'Red'
            }
        }
    } else {
        Say "  找不到解释器，跳过后端清理" 'Yellow'
    }
}

# ---------------------------------------------------------------- 3. 显存
$vram = (nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>$null)
Say ""
if ($vram) { Say "  当前显存  $vram" 'DarkGray' }
Say "  ────────────────────────────────────────────" 'DarkGray'
Say "  已停止" 'Green'
Say ""
