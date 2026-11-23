@echo off
REM ==========================================================================
REM  Sync diario ADVBOX -> Infinitum (clientes, processos, lancamentos, andamentos dos
REM  ultimos 15 dias) - chamado pelo Agendador do Windows. Mesmo comando do crontab da VPS.
REM  Cria o que falta, atualiza so campos do ADVBOX e preenche vazios; nunca apaga.
REM  Log: logs\advbox-infinitum.log
REM ==========================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0..\.."
set "RAIZ=%CD%"
if not exist "%RAIZ%\logs" mkdir "%RAIZ%\logs"
set "ESC_CONFIG_ENV=%RAIZ%\config\.env"
set "PYTHONIOENCODING=utf-8"
echo ===== %DATE% %TIME% >> "%RAIZ%\logs\advbox-infinitum.log"
"%RAIZ%\.venv\Scripts\python.exe" "%RAIZ%\SYNC\advbox_infinitum.py" --executar >> "%RAIZ%\logs\advbox-infinitum.log" 2>&1
