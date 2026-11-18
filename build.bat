@echo off
REM Build jadi .exe (sama seperti yang dijalankan GitHub Actions)
python -m pip install --upgrade pyinstaller
python -m PyInstaller --onedir --windowed --noconfirm --name GoWModelTool "%~dp0gow_model_tool.py"
echo Hasil: folder dist\GoWModelTool (bawa SELURUH folder)
if not defined CI pause
