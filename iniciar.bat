@echo off
cd /d "%~dp0"
setlocal

echo ========================================
echo      NP GESTAO AUTOMOTIVA V16
echo ========================================
echo.
echo Encerrando servidor anterior na porta 5000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1

echo Instalando/verificando dependencias...
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo ERRO ao instalar as dependencias.
  pause
  exit /b 1
)

echo Iniciando servidor...
start "NP Gestao Servidor" /min py app.py
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:5000/login?novo=1"
endlocal
