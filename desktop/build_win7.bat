@echo off
rem Builds dist\PaprikaPOS-Cloud-Win7.exe — the cloud cash register app for Windows 7.
rem Needs Python 3.8 (the last Python for Windows 7): https://www.python.org/downloads/release/python-3810/
cd /d "%~dp0"

if not exist .venv-win7\Scripts\python.exe (
  py -3.8 -m venv .venv-win7 || goto :error
)
set PY=.venv-win7\Scripts\python.exe
%PY% -m pip install -r requirements-win7.txt || goto :error
%PY% make_icon.py || goto :error
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name PaprikaPOS-Cloud-Win7 --icon paprika.ico pos_cloud.py || goto :error

echo.
echo Done: %~dp0dist\PaprikaPOS-Cloud-Win7.exe
exit /b 0

:error
echo.
echo Build failed.
exit /b 1
