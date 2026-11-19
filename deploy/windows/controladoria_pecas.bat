@echo off
REM ==========================================================================
REM  Producao automatica das pecas da controladoria - chamada pelo Agendador.
REM  Pega UMA peca da fila (o prazo mais curto primeiro), redige pelo Claude Code da
REM  assinatura, aplica o timbrado, salva em Y:\processos\<Cliente> (e no Google Drive
REM  quando houver credencial) e manda o card do Infinitum para "4. Revisao".
REM  Log: logs\controladoria-pecas.log   Fila: python SYNC\controladoria_pecas.py --listar
REM ==========================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0..\.."
set "RAIZ=%CD%"
if not exist "%RAIZ%\logs" mkdir "%RAIZ%\logs"
set "ESC_CONFIG_ENV=%RAIZ%\config\.env"
set "PYTHONIOENCODING=utf-8"
"%RAIZ%\.venv\Scripts\python.exe" "%RAIZ%\SYNC\controladoria_pecas.py" >> "%RAIZ%\logs\controladoria-pecas.log" 2>&1
