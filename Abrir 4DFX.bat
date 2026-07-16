@echo off
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    set PY=python
) else (
    set PY=py
)

%PY% -c "import requests" >nul 2>nul
if not %errorlevel%==0 (
    echo Instalando dependencia 'requests'...
    %PY% -m pip install --user requests
)

%PY% fdfx_gui.py
pause
