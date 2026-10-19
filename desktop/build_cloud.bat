@echo off
rem Builds dist\PaprikaPOS-Cloud.exe — the cash register app for https://pos.mypaprika.net.
cd /d "%~dp0"

python -m pip install -r requirements.txt || goto :error
python make_icon.py || goto :error
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name PaprikaPOS-Cloud --icon paprika.ico pos_cloud.py || goto :error

echo.
echo Done: %~dp0dist\PaprikaPOS-Cloud.exe
exit /b 0

:error
echo.
echo Build failed.
exit /b 1
