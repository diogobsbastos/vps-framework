@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   PUSH do VPS Framework  (envia pro GitHub)
echo ============================================
echo.
set /p msg="Mensagem do commit (Enter p/ 'update'): "
if "%msg%"=="" set msg=update
echo.
git add .
git commit -m "%msg%"
git push
echo.
echo ============================================
echo   PRONTO! Mudancas enviadas pro GitHub.
echo   Agora na VM: pkill + curl ^| bash p/ aplicar.
echo ============================================
pause
