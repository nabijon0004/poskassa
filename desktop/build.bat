@echo off
rem Builds dist\PaprikaPOS.exe — the cash register app for Windows.
cd /d "%~dp0"

python -m pip install -r requirements.txt || goto :error
python make_icon.py || goto :error
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name PaprikaPOS --icon paprika.ico pos_app.py || goto :error

if not exist dist\pos-config.json copy pos-config.example.json dist\pos-config.json >nul
echo.
echo Done: %~dp0dist\PaprikaPOS.exe
exit /b 0

:error
echo.
echo Build failed.
exit /b 1
