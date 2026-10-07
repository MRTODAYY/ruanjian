@echo off
title Code Review Agent - 检查自己的代码
cd /d "%~dp0"

echo.
echo   ============================================================
echo      审查你自己的代码
echo   ============================================================
echo.

set /p TARGET=请把要审查的文件路径粘贴到这里，然后按回车: 
echo.
echo   正在审查: %TARGET%
echo   请耐心等待 30-60 秒...
echo.

python -m agent review "%TARGET%" --report

echo.
echo   ============================================================
echo   完成！报告已保存到 reports 文件夹。
echo.
echo   按任意键关闭窗口。
pause >nul
