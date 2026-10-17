@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 必要なライブラリをインストールします...
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo インストールに失敗しました。Pythonがインストールされているか確認してください。
  pause
  exit /b 1
)
echo.
echo 完了しました。run.bat をダブルクリックして起動してください。
pause
