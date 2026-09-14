@echo off
cd /d "%~dp0"
setlocal EnableExtensions
mode con: cols=100 lines=30 >nul 2>&1
cls
echo ========================================
echo      NP GESTAO AUTOMOTIVA
echo ========================================
echo.
echo Encerrando servidor anterior na porta 5000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1

where python >nul 2>&1
if %errorlevel%==0 (set "PY=python") else (set "PY=py")

%PY% -c "import flask,requests" >nul 2>&1
if errorlevel 1 (
  echo Instalando dependencias pela primeira vez...
  %PY% -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo ERRO ao instalar dependencias.
    pause
    exit /b 1
  )
)

echo Iniciando servidor...
start "NP Gestao Servidor" /min %PY% app.py
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:5000/login"
endlocal
