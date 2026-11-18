@echo off
REM Jalankan tool (PC / Wine). Butuh Python 3 lengkap (dengan tkinter).
where python >nul 2>nul
if %errorlevel%==0 ( python "%~dp0gow_model_tool.py" ) else ( py -3 "%~dp0gow_model_tool.py" )
pause
