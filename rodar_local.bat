@echo off
chcp 65001 >nul
cd /d "%~dp0instalador"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY goto nopy
echo ============================================
echo   INSTALADOR LOCAL (roda no SEU PC)
echo   Conecta em qualquer servidor por SSH.
echo ============================================
echo.
echo Encerrando instalador anterior na porta 9000 (se houver)...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :9000 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>nul
echo Instalando/atualizando dependencia SSH (paramiko)...
%PY% -m pip install -U paramiko -q
set VPS_KEY=local123
echo.
echo  No ar:  http://127.0.0.1:9000/?key=local123
echo  (feche esta janela para parar)
echo.
start "" "http://127.0.0.1:9000/?key=local123"
%PY% server.py
goto end
:nopy
echo.
echo  Python nao encontrado no PC.
echo  Instale em https://www.python.org/downloads/
echo  IMPORTANTE: marque "Add Python to PATH" na instalacao.
echo  Depois e so rodar este arquivo de novo.
echo.
:end
pause
