@echo off
cd /d "%~dp0"
py -m pip install -r requirements.txt
start "NP Gestao Servidor" /min py app.py
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:5000/login"
exit
