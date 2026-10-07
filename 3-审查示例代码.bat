@echo off
title Code Review Agent - 审查示例代码
cd /d "%~dp0"

echo.
echo   ============================================================
echo      正式审查示例（会调用真实 API，消耗少量额度）
echo   ============================================================
echo.
echo   正在审查 demo\sample_buggy.py
echo   这个文件是故意写坏的，里面有 10 个典型 Bug，
echo   看看 Agent 能不能全部找出来。
echo.
echo   大约需要 30-60 秒，请耐心等待...
echo.

python -m agent review demo/sample_buggy.py --report

echo.
echo   ============================================================
echo   审查完成！
echo   完整报告已保存到 reports 文件夹里，可以直接打开看。
echo.
echo   按任意键关闭窗口。
pause >nul
