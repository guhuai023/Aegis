@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ================================================================
echo   Aegis - 一键启动
echo   平台: 多 Agent 协作与可信交付
echo ================================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有找到 python。
  echo        请安装 Python 3.11 ~ 3.14（安装时勾选 "Add python.exe to PATH"），
  echo        然后重新双击本文件。
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] 创建虚拟环境 .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo [错误] 创建虚拟环境失败。
    pause
    exit /b 1
  )
  echo [2/4] 安装依赖  ^(首次约 1~3 分钟，需要联网^) ...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络或换用镜像:
    echo        .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
  )
) else (
  echo [1/4] 已存在 .venv，跳过创建
  echo [2/4] 已存在 .venv，跳过安装
)

if not exist ".env" (
  if exist ".env.example" (
    copy ".env.example" ".env" >nul
    echo [提示] 已生成 .env。填入 DEEPSEEK_API_KEY 才能真跑大模型；
    echo        不填也能启动：可查看演示数据、运行离线自检。
  )
)

if not exist "aegis_platform.db" (
  if exist "seed\aegis_platform.db" (
    echo [3/4] 首次启动：安装演示数据（10 条真实运行记录 + 交付物） ...
    ".venv\Scripts\python.exe" scripts\setup_demo_data.py
  )
) else (
  echo [3/4] 已存在运行库，跳过演示数据安装
)

echo [4/4] 启动平台： http://127.0.0.1:8507
echo        浏览器没自动打开时，手动访问上面的地址。
echo        关闭本窗口即停止服务。
echo.
".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8507 --server.address 127.0.0.1

pause