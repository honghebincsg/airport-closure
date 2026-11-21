@echo off
chcp 65001 >nul
title 机场关闭图生成器
cd /d "%~dp0"
echo ==========================================
echo   机场关闭图生成器
echo   浏览器会自动打开 http://127.0.0.1:8765
echo   用完直接关闭本窗口即可
echo ==========================================
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有找到 Python。
  echo 请先安装 Python 3.9 或更高版本（安装时勾选 Add Python to PATH），
  echo 或者直接运行打包好的 exe 版本。
  pause
  exit /b
)
python -c "import requests, networkx, matplotlib, curl_cffi" 2>nul
if errorlevel 1 (
  echo [提示] 缺少依赖，正在自动安装，请稍候...
  python -m pip install --upgrade pip -q
  python -m pip install requests networkx matplotlib curl_cffi -q
)
python airport_closure_app.py
pause
