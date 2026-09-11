@echo off
REM zenn-book2pdf.cmd -- Windows の PATH から PowerShell ラッパーを呼ぶ
REM 例: zenn-book2pdf --epub https://zenn.dev/USER/books/BOOK-SLUG
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0zenn-book2pdf.ps1" %*
exit /b %ERRORLEVEL%
