#Requires -Version 5.1
<#
    Aegis 一键启动（真正的逻辑在这里；run.bat 只是 ASCII 外壳）

    用法：
        .\run.ps1               启动平台
        .\run.ps1 -DryRun       只报告环境与将要执行的步骤，不安装、不启动、不改文件
        .\run.ps1 -NoStart      准备好环境后停住（不启动服务）

    两条路径，自动选择：
      A. 包里带了 runtime\python314\python.exe（离线版）→ 直接用它，不装 Python、不装依赖、不联网
      B. 没有 runtime（常规版）→ 用本机 Python 建 .venv 并装依赖（需要联网一次）

    两个必须注意的兼容点（都是真实踩过的坑）：
    1) cmd.exe 用 OEM 代码页（中文 Windows 是 936/GBK）读 .bat，所以 run.bat 必须保持
       纯 ASCII，中文与逻辑都放在这个 .ps1（UTF-8 带 BOM）里；
    2) PowerShell 5.1 在 ErrorActionPreference='Stop' 时，**原生命令往 stderr 写任何东西
       都会被当成终止性错误**。空 venv 里 python -c "import streamlit" 必然报
       ModuleNotFoundError，就会把脚本打断。所以这里不设 Stop，一律显式判断 LASTEXITCODE。
#>
param(
    [switch]$DryRun,
    [switch]$NoStart
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'Continue'
Set-Location -Path (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Line($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Step($n, $text) { Write-Host ""; Write-Host "[$n/5] $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "       $text" -ForegroundColor DarkGreen }
function Warn($text) { Write-Host "       $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host ""; Write-Host "[错误] " -ForegroundColor Red -NoNewline; Write-Host $text -ForegroundColor Red; exit 1 }

function Test-Native($exe, [string[]]$argv) {
    try { & $exe @argv 2>&1 | Out-Null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

Line "================================================================" 'DarkCyan'
Line "  Aegis · 多 Agent 协作与可信交付 —— 一键启动" 'DarkCyan'
Line "================================================================" 'DarkCyan'
Write-Host ""
Line ("  工作目录: " + (Get-Location).Path)
Line ("  系统: " + [System.Environment]::OSVersion.VersionString + " / PowerShell " + $PSVersionTable.PSVersion.ToString())
if ($DryRun) { Line "  模式: 干跑（只检查，不安装、不启动、不改文件）" Yellow }

$venvPy = Join-Path (Get-Location) ".venv\Scripts\python.exe"
$bundledPy = Join-Path (Get-Location) "runtime\python314\python.exe"

# ---------------------------------------------------------------- 1 选解释器
Step 1 "确定 Python 解释器"
$pyExe = $null
$useBundled = $false

if (Test-Path $bundledPy) {
    $useBundled = $true
    $pyExe = $bundledPy
    $ver = (& $bundledPy -V) 2>&1
    Ok "使用包内便携运行时（离线版）：不需要安装 Python、不需要联网"
    Ok ("  " + $ver + "  →  " + $bundledPy)
} else {
    $baseExe = $null
    $baseArgs = @()
    if (Test-Path $venvPy) {
        $ver = (& $venvPy -V) 2>&1
        $pyExe = $venvPy
        Ok ("已有虚拟环境: .venv  ( " + $ver + " )")
    } else {
        foreach ($cand in @(@{ exe = 'python'; args = @() }, @{ exe = 'py'; args = @('-3') })) {
            if (Test-Native $cand.exe ($cand.args + '-V')) { $baseExe = $cand.exe; $baseArgs = $cand.args; break }
        }
        if (-not $baseExe) {
            Fail @"
没有找到 Python，包里也没有内置运行时。
  1) 安装 Python 3.11~3.14（安装第一步勾选 "Add python.exe to PATH"）后重试；
  2) 或用「离线版」提交包（内含 runtime\，无需安装 Python）；
  3) 或按 docs\HOW-TO-RUN.txt 手动执行。
"@
        }
        $ver = (& $baseExe @($baseArgs + '-V')) 2>&1
        Ok ("找到本机解释器: " + $baseExe + "  →  " + $ver)
    }
}

# ---------------------------------------------------------------- 2 虚拟环境（仅常规版）
Step 2 "准备虚拟环境"
if ($useBundled) {
    Ok "跳过：使用包内运行时，不需要 .venv"
} elseif (Test-Path $venvPy) {
    Ok "跳过创建（.venv 已存在）"
} elseif ($DryRun) {
    Ok ("将要执行: 本机 python -m venv .venv")
} else {
    & $baseExe @($baseArgs + @('-m', 'venv', '.venv'))
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) {
        Fail "创建虚拟环境失败。请确认 Python 安装完整（带 venv 模块），或按 docs\HOW-TO-RUN.txt 手动执行。"
    }
    $pyExe = $venvPy
    Ok "创建完成"
}

# ---------------------------------------------------------------- 3 依赖（仅常规版）
Step 3 "依赖"
if ($useBundled) {
    if (Test-Native $pyExe @('-c', 'import streamlit, openai, yaml, openpyxl')) {
        Ok "包内依赖已就绪（不需要 pip、不需要联网）"
    } else {
        Fail "包内运行时缺少依赖，包可能损坏。请重新解压，或换「常规版」按提示安装。"
    }
} else {
    $needInstall = $true
    if ((Test-Path $pyExe) -and (Test-Native $pyExe @('-c', 'import streamlit, openai, yaml'))) {
        Ok "依赖已就绪（streamlit / openai / pyyaml）"
        $needInstall = $false
    }
    if ($needInstall) {
        if ($DryRun) {
            Ok "将要执行: .venv\Scripts\python.exe -m pip install -r requirements.txt"
        } elseif (-not (Test-Path $pyExe)) {
            Warn "没有 .venv，跳过依赖检查"
        } else {
            Ok "安装中（首次约 1~3 分钟，需要联网）..."
            & $pyExe -m pip install --upgrade pip -q
            & $pyExe -m pip install -r requirements.txt
            if ($LASTEXITCODE -ne 0) {
                Warn "默认源安装失败，改用清华镜像重试 ..."
                & $pyExe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
                if ($LASTEXITCODE -ne 0) {
                    Fail @"
依赖安装失败（连续两次都失败）。
  1) 检查网络 / 代理 / 公司防火墙；
  2) 手动执行:
     .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  3) 若提示 SSL 相关错误，先升级 pip:
     .venv\Scripts\python.exe -m pip install --upgrade pip
  4) 实在装不上：换「离线版」提交包（内含 runtime\，不需要联网）
  5) 详见 docs\HOW-TO-RUN.txt 与 docs\提交说明-如何复刻.md
"@
                }
            }
            if (-not (Test-Native $pyExe @('-c', 'import streamlit, openai, yaml'))) {
                Fail "依赖装完了但仍导入失败。手动执行 .venv\Scripts\python.exe -c `"import streamlit`" 查看报错。"
            }
            Ok "安装完成"
        }
    }
}

# ---------------------------------------------------------------- 4 配置与演示数据
Step 4 "配置 .env 与演示数据"
$envFile = Join-Path (Get-Location) ".env"
if (Test-Path $envFile) {
    Ok ".env 已存在"
} elseif (Test-Path ".env.example") {
    if ($DryRun) {
        Ok "将要从 .env.example 生成 .env"
    } else {
        Copy-Item ".env.example" ".env"
        Ok "已从 .env.example 生成 .env（填 DEEPSEEK_API_KEY 才能真跑大模型；不填也能看演示数据）"
    }
}

$db = Join-Path (Get-Location) "aegis_platform.db"
$seed = Join-Path (Get-Location) "seed\aegis_platform.db"
$needSeed = $false
if ((Test-Path $db) -and ((Get-Item $db).Length -gt 0)) {
    Ok "运行库已存在（跳过演示数据安装）"
} elseif (Test-Path $seed) {
    $needSeed = $true
} else {
    Ok "没有 seed/ 演示数据（正常，包可能是不含演示数据的版本）"
}

if ($needSeed) {
    if ($DryRun) {
        Ok "将要执行: scripts\setup_demo_data.py（装入 10 条真实 trace + 交付物）"
    } elseif (-not (Test-Path $pyExe)) {
        Warn "没有可用解释器，跳过演示数据安装"
    } else {
        & $pyExe "scripts\setup_demo_data.py"
        if ($LASTEXITCODE -ne 0) {
            Warn "演示数据安装失败，平台仍可启动（库会是空的）"
        } else {
            Ok "演示数据已安装：10 条真实运行记录"
        }
    }
}

# ---------------------------------------------------------------- 5 启动
Step 5 "启动平台"
Line ("  解释器: " + $pyExe) DarkGray
Line "  地址: http://127.0.0.1:8507    （关闭本窗口即停止）" Green
Write-Host ""

if ($DryRun) {
    Line "干跑完成：去掉 -DryRun 即可真正启动。" Cyan
    exit 0
}
if ($NoStart) {
    Line "已按要求准备好环境但未启动（-NoStart）。" Cyan
    exit 0
}

& $pyExe -m streamlit run app.py --server.port 8507 --server.address 127.0.0.1
exit $LASTEXITCODE