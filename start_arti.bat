@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1

REM --- Pilih Python (urutan prioritas) ---
set "PY="
if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"
if not defined PY set "PY=python"

echo.
echo  Arti - bridge + telemetry dashboard (paralel)
echo  Tutup window bridge = stop stream. Window telemetry bisa ditutup sendiri.
echo  Python: %PY%
echo.

REM --- Cek modul vision (PIL = Pillow) ---
"%PY%" -c "from PIL import Image; import mss, mouse" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Modul vision belum terpasang di Python di atas.
  echo.
  echo  PIL = Pillow ^(library gambar^) untuk screenshot vision / Mouse4.
  echo  Perlu juga: mss ^(capture layar^), mouse ^(hotkey^).
  echo.
  echo  Install sekali:
  echo    "%PY%" -m pip install pillow mss mouse
  echo.
  echo  Atau pakai venv project:
  echo    python -m venv venv
  echo    venv\Scripts\pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

start "Arti Telemetry" "%PY%" "%~dp0arti_telemetry_dashboard.py" --watch --open --interval 15

echo [Arti] Bridge starting di window ini...
"%PY%" "%~dp0arti_bridge.py"

echo.
echo [Arti] Bridge selesai. Dashboard window mungkin masih jalan - tutup manual kalau perlu.
pause
