@echo off
title SGPP - Sistema de Gestao e Planejamento Pedagogico
cd /d "%~dp0"
echo ========================================================
echo   Iniciando o SGPP - Planejamento Pedagogico Universal
echo ========================================================
echo.
.\venv\Scripts\python.exe iniciar_servidor.py
pause
