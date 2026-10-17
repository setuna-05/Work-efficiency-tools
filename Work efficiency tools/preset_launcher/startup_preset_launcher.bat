@echo off
rem Preset Launcher - auto start helper
rem Put a shortcut to this file in the Startup folder (Win+R -> shell:startup).
cd /d "%~dp0"
where pyw >nul 2>&1 && (start "" pyw "%~dp0preset_launcher.py" --startup & exit /b 0)
where pythonw >nul 2>&1 && (start "" pythonw "%~dp0preset_launcher.py" --startup & exit /b 0)
echo Python (pyw / pythonw) was not found in PATH.
echo Please install Python from https://www.python.org/ and tick "Add python.exe to PATH".
pause
exit /b 1
