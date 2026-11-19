@echo off
REM ==========================================================================
REM  Sobe o sistema SEM janela, para o Agendador do Windows (ao entrar no Windows).
REM  Com ele rodando, a Controladoria puxa do SYNC a cada SYNC_CACHE_SEGUNDOS.
REM  Se a porta 8000 ja estiver em uso (sistema aberto), nao faz nada.
REM ==========================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0..\.."
set "RAIZ=%CD%"
if not exist "%RAIZ%\logs" mkdir "%RAIZ%\logs"

netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul && exit /b 0

set "ESC_CONFIG_ENV=%RAIZ%\config\.env"
set "SISTEMA_DB=%RAIZ%\SISTEMA\backend\sistema_esc.db"
set "ESC_MEDIA_DIR=%RAIZ%\SISTEMA\backend\media"
set "SERVIR_FRONTEND=1"
set "DATABASE_URL=sqlite+aiosqlite:///%RAIZ:\=/%/SISTEMA/backend/sistema_esc.db"
set "PYTHONIOENCODING=utf-8"

cd /d "%RAIZ%\SISTEMA\backend"
"%RAIZ%\.venv\Scripts\python.exe" -m uvicorn app.main_vps:app --host 127.0.0.1 --port 8000 --workers 1 >> "%RAIZ%\logs\servidor.log" 2>&1
