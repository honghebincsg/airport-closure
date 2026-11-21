@echo off
chcp 65001 >nul
title 打包成单文件 exe
cd /d "%~dp0"
echo ==========================================
echo   把 机场关闭图生成器 打成单文件 exe
echo   产物：dist\机场关闭图.exe
echo ==========================================
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有找到 Python，无法打包。
  echo 请先安装 Python 3.9+（勾选 Add Python to PATH）。
  pause
  exit /b
)
echo [1/3] 安装依赖与 PyInstaller...
python -m pip install --upgrade pip -q
python -m pip install requests networkx matplotlib curl_cffi pyinstaller -q
if errorlevel 1 (
  echo [错误] 依赖安装失败，检查网络或公司代理设置。
  pause
  exit /b
)
echo [2/3] 开始打包（首次约需 2-4 分钟，会自动下载打包运行库）...
python -m PyInstaller ^
  --onefile ^
  --noconsole ^
  --name "机场关闭图" ^
  --add-data "runways.csv;." ^
  --collect-data matplotlib ^
  --collect-all curl_cffi ^
  --collect-all certifi ^
  airport_closure_app.py
if errorlevel 1 (
  echo [错误] 打包失败，请看上面的报错信息。
  pause
  exit /b
)
echo [3/3] 完成！
echo 产物在 dist\机场关闭图.exe
echo 注意：exe 首次运行会在同目录下创建 cache 与 output 文件夹。
pause
