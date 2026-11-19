@echo off
REM ==========================================================================
REM  Controladoria automatica - chamada pelo Agendador do Windows.
REM  O que ela pode fazer sozinha vem de CONTROLADORIA_AUTO_NIVEL no config\.env:
REM    0 = so simula e gera a planilha | 1 = conclui ciencia pura | 2 = + cadastra prazos
REM  Planilhas: SISTEMA\backend\relatorios_controladoria\   Log: logs\controladoria-auto.log
REM ==========================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0..\.."
set "RAIZ=%CD%"
if not exist "%RAIZ%\logs" mkdir "%RAIZ%\logs"
set "ESC_CONFIG_ENV=%RAIZ%\config\.env"
set "PYTHONIOENCODING=utf-8"
"%RAIZ%\.venv\Scripts\python.exe" "%RAIZ%\SYNC\controladoria_auto.py" --aplicar >> "%RAIZ%\logs\controladoria-auto.log" 2>&1
