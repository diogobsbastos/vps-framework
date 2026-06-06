@echo off
chcp 65001 >nul
cd /d "%~dp0instalador"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY ( echo Python nao encontrado. Instale com "Add to PATH". & pause & exit /b )
echo ============================================
echo   BUILD do Instalador VPS (.exe pendrive)
echo ============================================
echo.
echo [1/2] Instalando PyInstaller + paramiko...
%PY% -m pip install -U pyinstaller paramiko -q
echo.
echo [2/2] Empacotando InstaladorVPS.exe (pode levar 1-2 min)...
%PY% -m PyInstaller --noconfirm --onefile --name InstaladorVPS ^
  --collect-all paramiko ^
  --add-data "server.py;instalador" ^
  --add-data "..\default_src;default_src" ^
  --add-data "..\override;override" ^
  --add-data "..\locks;locks" ^
  server.py
echo.
if exist "dist\InstaladorVPS.exe" (
  echo ============================================
  echo   PRONTO!  O executavel esta em:
  echo   %~dp0instalador\dist\InstaladorVPS.exe
  echo.
  echo   Copie esse .exe pro pendrive. Duplo-clique
  echo   nele em QUALQUER PC Windows (sem Python) e
  echo   ele abre o instalador no navegador sozinho.
  echo ============================================
) else (
  echo FALHOU - veja as mensagens acima.
)
echo.
pause
