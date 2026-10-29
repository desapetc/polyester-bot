@echo off
cd /d "%~dp0"
python -m venv .venv
.venv\Scripts\pip.exe install -q -r requirements.txt
if not exist .env copy .env.example .env
echo Setup done. Isi accounts.csv, lalu jalankan run.bat
pause
