@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
mode con: cols=100 lines=32 >nul 2>&1

:: Garante permissao do Windows Firewall para acesso do iPhone na rede local.
net session >nul 2>&1
if errorlevel 1 (
  echo Solicitando permissao de administrador do Windows...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

cls
echo ================================================================
echo              NP GESTAO AUTOMOTIVA - IPHONE
echo ================================================================
echo.
echo Configurando acesso do iPhone...
netsh advfirewall firewall delete rule name="NP Gestao Automotiva - iPhone" >nul 2>&1
netsh advfirewall firewall add rule name="NP Gestao Automotiva - iPhone" dir=in action=allow protocol=TCP localport=5000 profile=private >nul 2>&1
if errorlevel 1 (
  echo.
  echo AVISO: nao foi possivel criar a regra do Firewall automaticamente.
  echo O Windows pode estar bloqueando o acesso pela rede.
  echo.
)

where python >nul 2>&1
if %errorlevel%==0 (
  set "PY=python"
) else (
  where py >nul 2>&1
  if %errorlevel%==0 (set "PY=py") else (
    echo ERRO: Python nao foi encontrado neste computador.
    pause
    exit /b 1
  )
)

%PY% -c "import flask,requests" >nul 2>&1
if errorlevel 1 (
  echo Instalando dependencias pela primeira vez...
  %PY% -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo.
    echo ERRO ao instalar as dependencias.
    pause
    exit /b 1
  )
)

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1

echo Iniciando servidor...
start "NP Gestao Servidor" /min %PY% app.py

echo Aguardando o servidor ficar pronto...
set "READY="
for /l %%i in (1,1,20) do (
  powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5000/health -TimeoutSec 1; if($r.StatusCode -eq 200){exit 0}else{exit 1} } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 set "READY=1"
  if defined READY goto SERVIDOR_OK
  timeout /t 1 /nobreak >nul
)

echo.
echo ERRO: o servidor nao respondeu na porta 5000.
echo.
echo Feche esta janela e me envie uma foto desta tela.
pause
exit /b 1

:SERVIDOR_OK
:: Descobre o IP local da rede Wi-Fi usando o proprio Python.
:: Isso evita problemas com comandos do PowerShell em diferentes versoes do Windows.
set "IP="
for /f "delims=" %%a in ('%PY% -c "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()" 2^>nul') do set "IP=%%a"
if not defined IP (
  for /f "tokens=14" %%a in ('ipconfig ^| findstr /R /C:"IPv4"') do if not defined IP set "IP=%%a"
)
if not defined IP set "IP=127.0.0.1"

cls
echo ================================================================
echo              NP GESTAO AUTOMOTIVA - IPHONE
echo ================================================================
echo.
echo SERVIDOR: ONLINE
 echo.
echo No iPhone, conectado na MESMA rede Wi-Fi, abra no Safari:
echo.
echo     http://%IP%:5000/login
 echo.
echo ================================================================
echo.
echo IMPORTANTE:
echo - Deixe esta janela aberta enquanto usar o iPhone.
echo - O iPhone e o computador precisam estar na mesma Wi-Fi.
echo - Se o Safari nao abrir, confira se nao esta usando Wi-Fi de convidado.
echo.
echo Abrindo o sistema neste computador...
start "" "http://127.0.0.1:5000/login?novo=1"
echo.
echo Pressione qualquer tecla somente quando quiser encerrar o servidor.
pause >nul
endlocal
