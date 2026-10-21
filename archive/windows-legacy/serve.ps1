<#
.SYNOPSIS
    启动 PaddleOCR-VL-1.6 的 HTTP 服务（PaddleX 服务化部署）。

.DESCRIPTION
    服务起来之后，你可以用 curl / Postman / Python / Java / C# 任何语言调用，
    不需要我在场。客户端代码不必与服务端同语言。

    关键：产线注册名必须用 PaddleOCR-VL-1.6。
    直接用 PaddleOCR-VL 会指向默认（旧）模型组合 —— PP-DocLayoutV2 + PaddleOCR-VL，
    会额外下载约 2 GB，且不是 1.6。

    1.6 需要的两个模型（PP-DocLayoutV3 + PaddleOCR-VL-1.6-0.9B）如果已经跑过
    paddleocr doc_parser，就已经在本地了，启动不会再有下载。

.EXAMPLE
    pwsh -File .\serve.ps1
    pwsh -File .\serve.ps1 -Port 9000 -Device cpu
    pwsh -File .\serve.ps1 -Open          # 就绪后自动打开 Swagger UI
#>
[CmdletBinding()]
param(
    [int]$Port      = 8080,
    [string]$BindHost = "127.0.0.1",   # 想被局域网访问就填 0.0.0.0
    [string]$Device = "gpu:0",
    [string]$EnvPath = "D:\visonalbe\.venv_paddleocr",
    [switch]$Open                        # 服务就绪后自动打开浏览器
)

$py = Join-Path $EnvPath "python.exe"
if (-not (Test-Path $py)) { throw "找不到解释器: $py" }

# 端口预检：必须在加载 1.8 GB 权重之前失败，否则白等 1-2 分钟才报 10048
$busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
if ($busy) {
    $owner = Get-Process -Id $busy.OwningProcess -ErrorAction SilentlyContinue
    Write-Host "端口 $Port 已被占用" -ForegroundColor Red
    Write-Host "  占用进程 : pid=$($busy.OwningProcess)  $($owner.ProcessName)" -ForegroundColor Red
    if ($owner.Path) { Write-Host "  路径     : $($owner.Path)" -ForegroundColor DarkGray }
    Write-Host ""
    Write-Host "三个选择：" -ForegroundColor Yellow
    Write-Host "  1) 换端口       pwsh -File .\serve.ps1 -Port 8081"
    Write-Host "  2) 杀掉占用者   Stop-Process -Id $($busy.OwningProcess) -Force"
    Write-Host "  3) 看看是谁     Get-Process -Id $($busy.OwningProcess) | Select-Object Name,Path"
    exit 1
}

Write-Host "启动 PaddleOCR-VL-1.6 服务" -ForegroundColor Cyan
Write-Host ""
Write-Host "  浏览器打开这个（带 /docs，根路径是 404）:" -ForegroundColor Green
Write-Host "    http://${BindHost}:$Port/docs" -ForegroundColor White -BackgroundColor DarkGreen
Write-Host ""
Write-Host "  主接口 : POST http://${BindHost}:$Port/layout-parsing"
Write-Host "  健康   : GET  http://${BindHost}:$Port/health"
Write-Host "  设备   : $Device"
Write-Host ""
Write-Host "首次启动要加载约 1.8 GB 权重，看到 'Application startup complete' 才接受请求。" -ForegroundColor Yellow
Write-Host "按 Ctrl+C 停止。" -ForegroundColor Yellow
Write-Host ""

# 在后台等就绪，然后自动开浏览器（-Open 时）
if ($Open) {
    $docsUrl = "http://${BindHost}:$Port/docs"
    $healthUrl = "http://${BindHost}:$Port/health"
    $openJob = Start-Job -ArgumentList $docsUrl, $healthUrl -ScriptBlock {
        param($docs, $health)
        for ($i = 0; $i -lt 150; $i++) {          # 最多等 5 分钟
            Start-Sleep -Seconds 2
            try {
                Invoke-RestMethod -Uri $health -TimeoutSec 3 | Out-Null
                Start-Process $docs
                return
            } catch { }
        }
    }
    Write-Host "已安排：服务就绪后自动打开 $docsUrl" -ForegroundColor Cyan
}

& $py -m paddlex --serve --pipeline PaddleOCR-VL-1.6 `
     --host $BindHost --port $Port --device $Device

if ($Open -and $openJob) {
    Stop-Job $openJob -ErrorAction SilentlyContinue
    Remove-Job $openJob -Force -ErrorAction SilentlyContinue
}
