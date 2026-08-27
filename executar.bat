@echo off
chcp 65001 >nul
cd /d "%~dp0"
title iOS Location Studio

where python >nul 2>nul
if errorlevel 1 (
    echo Python nao encontrado no PATH.
    echo Instale em https://www.python.org/downloads/windows/ marcando "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Criando ambiente virtual...
    python -m venv .venv || goto :erro
)

call ".venv\Scripts\activate.bat"

python -c "import customtkinter, tkintermapview, pymobiledevice3" >nul 2>nul
if errorlevel 1 (
    echo Instalando dependencias, aguarde...
    python -m pip install --upgrade pip >nul
    python -m pip install -r requirements.txt || goto :erro
)

python main.py
goto :fim

:erro
echo.
echo Falha na preparacao do ambiente.
pause
exit /b 1

:fim
