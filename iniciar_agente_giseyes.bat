@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Agente Local GIS Eyes (clientetvt4.exe)
where python >nul 2>nul
if errorlevel 1 (
    echo [ERRO] Python nao encontrado neste computador.
    echo Instale o Python em https://www.python.org/downloads/ marcando "Add Python to PATH".
    pause
    exit /b 1
)
python -u agente_giseyes_local.py
pause
