@echo off
title Code Review Agent - 代码审查智能体
cd /d "%~dp0"

echo.
echo   ============================================================
echo      代码审查 Agent  -  网页版
echo   ============================================================
echo.
echo   马上会自动打开浏览器。
echo   如果没自动打开，请手动访问: http://127.0.0.1:8000
echo.
echo   提示: 这个黑色窗口不要关闭，关了网页就用不了了。
echo         用完想退出，直接关掉这个窗口，或者按 Ctrl+C。
echo.

start "" http://127.0.0.1:8000
python -m agent.web --port 8000

echo.
echo   服务已停止。按任意键关闭窗口。
pause >nul
