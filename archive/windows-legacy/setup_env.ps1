<#
.SYNOPSIS
    为 PaddleOCR-VL-1.6 构建独立 Python 环境（Windows + NVIDIA RTX 50 系 / Blackwell）。

.DESCRIPTION
    依据官方教程 PaddleOCR-VL RTX 50 环境配置教程：
    http://www.paddleocr.ai/v3.3.2/version3.x/pipeline_usage/PaddleOCR-VL-RTX50.html

    三处硬性约束（踩过就会失败）：
      1. Python 必须 3.8–3.12。当前机器的 base 是 3.14，cu129 索引没有 cp314 的 wheel。
      2. RTX 50 系（sm_120）必须走 cu129 源，且驱动需支持 CUDA 12.9 以上。
      3. 必须额外安装 special 版 safetensors；Windows 用官方给的 win_amd64 wheel。

    脚本幂等，可重复执行。

.EXAMPLE
    pwsh -File .\setup_env.ps1
    pwsh -File .\setup_env.ps1 -EnvPath D:\envs\paddleocr-vl -Mirror https://pypi.org/simple
#>
[CmdletBinding()]
param(
    [string]$EnvPath  = "D:\visonalbe\.venv_paddleocr",
    [string]$PyVersion = "3.12",
    [string]$Mirror   = "https://pypi.tuna.tsinghua.edu.cn/simple"
)

$ErrorActionPreference = 'Stop'
function Step($n, $t) { Write-Host "`n=== [$n] $t ===" -ForegroundColor Cyan }

# ---------------------------------------------------------------- 0. 前置检查
Step 0 "前置检查"
if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    throw "未找到 conda。请先安装 Miniconda，或改用 python.org 的 3.12 自行创建 venv。"
}

$drv = (nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>$null | Select-Object -First 1)
if ($drv) {
    Write-Host "NVIDIA 驱动版本: $drv"
    Write-Host "注意：RTX 50 系要求驱动支持 CUDA 12.9 以上。" -ForegroundColor Yellow
} else {
    Write-Warning "未检测到 nvidia-smi，GPU 加速可能不可用。"
}

# ---------------------------------------------------------------- 1. 创建环境
Step 1 "创建 Python $PyVersion 环境 -> $EnvPath"
if (Test-Path "$EnvPath\python.exe") {
    Write-Host "已存在，跳过创建。"
} else {
    & conda create -p $EnvPath "python=$PyVersion" -y
    if ($LASTEXITCODE -ne 0) { throw "conda create 失败" }
}

$py = Join-Path $EnvPath "python.exe"
if (-not (Test-Path $py)) { throw "找不到解释器: $py" }
Write-Host (& $py -c "import sys; print(sys.version)")

# ---------------------------------------------------------------- 2. 飞桨 GPU
Step 2 "安装 paddlepaddle-gpu 3.2.1 (cu129 / sm_120)"
# 坑：www.paddlepaddle.org.cn/packages/... 这个"索引页"地址直接当 wheel 链接用时，
# 会返回 HTTP 200 + 一个 HTML 页面，pip 下成 0 字节后报 "Wheel is invalid"。
# 真实文件在 paddle-whl.cdn.bcebos.com 上。
$paddleWheel = "https://paddle-whl.cdn.bcebos.com/stable/cu129/" +
               "paddlepaddle-gpu/paddlepaddle_gpu-3.2.1-cp312-cp312-win_amd64.whl"
# 用直链而非 -i 索引：paddle 的索引页不含依赖，pip 会解析失败或歧义。
& $py -m pip install --upgrade pip setuptools wheel -i $Mirror
& $py -m pip install $paddleWheel -i $Mirror
if ($LASTEXITCODE -ne 0) { throw "paddlepaddle-gpu 安装失败" }

# ---------------------------------------------------------------- 3. PaddleOCR
Step 3 "安装 paddleocr[doc-parser]"
& $py -m pip install -U "paddleocr[doc-parser]" -i $Mirror
if ($LASTEXITCODE -ne 0) { throw "paddleocr 安装失败" }

# ---------------------------------------------------------------- 4. safetensors
Step 4 "安装 special 版 safetensors (win_amd64)"
$stWheel = "https://xly-devops.cdn.bcebos.com/safetensors-nightly/" +
           "safetensors-0.6.2.dev0-cp38-abi3-win_amd64.whl"
& $py -m pip install --force-reinstall --no-deps $stWheel
if ($LASTEXITCODE -ne 0) { throw "safetensors 安装失败" }

# ---------------------------------------------------------------- 5. 校验
Step 5 "校验"
& $py -c "import paddle; print('paddle', paddle.__version__); print('cuda build:', paddle.device.is_compiled_with_cuda()); print('gpus:', paddle.device.cuda.device_count())"

Write-Host "`n环境就绪。" -ForegroundColor Green
Write-Host "激活方式:  conda activate $EnvPath"
Write-Host "自检:      & '$py' .\smoke_test.py"
Write-Host "跑一页:    & '$py' .\smoke_test.py --run-ocr"
Write-Host "`n提示：vLLM / SGLang 推理加速在 Windows 原生环境不可用（官方镜像与教程均为 Linux），需要 WSL2。" -ForegroundColor Yellow
