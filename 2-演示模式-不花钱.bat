@echo off
title Code Review Agent - 演示模式
cd /d "%~dp0"

echo.
echo   ============================================================
echo      演示模式（不消耗 API 额度）
echo   ============================================================
echo.
echo   下面会用假的大模型回复，把整个流程走一遍，
echo   让你看清 Agent 每一步在做什么。不会花你的钱。
echo.

python -m agent --mock --no-color review demo/sample_buggy.py

echo.
echo   ============================================================
echo   演示结束。按任意键关闭窗口。
pause >nul
