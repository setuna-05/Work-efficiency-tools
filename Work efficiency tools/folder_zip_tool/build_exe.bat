@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -m pip install -r requirements.txt pyinstaller
py -m PyInstaller --noconsole --onefile --name FolderZipTool main.py
echo.
echo dist\FolderZipTool.exe ができました。
pause
