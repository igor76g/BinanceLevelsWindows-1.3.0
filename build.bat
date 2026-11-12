@echo off
rem Local build on Windows. Requires: Python 3.10+ and Inno Setup 6 (https://jrsoftware.org/isinfo.php)
setlocal

python -m pip install --upgrade pip || goto :error
pip install -r requirements.txt pyinstaller || goto :error

pyinstaller --noconfirm --windowed --name BinanceLevelsMonitor --hidden-import pystray._win32 run.py || goto :error

set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
%ISCC% installer.iss || goto :error

echo.
echo Done: installer_output\BinanceLevelsMonitor-Setup.exe
exit /b 0

:error
echo Build failed.
exit /b 1
