@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYCMD=py"
where py >nul 2>&1 || set "PYCMD=python"

%PYCMD% -c "import webview, minecraft_launcher_lib, requests" >nul 2>&1
if errorlevel 1 (
    echo [GreenPrism] Первая настройка: установка библиотек...
    %PYCMD% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [Ошибка] Не удалось установить зависимости. Проверь установку Python.
        pause
        exit /b 1
    )
)

echo [GreenPrism] Быстрый запуск лаунчера...
%PYCMD% -O main.py
if errorlevel 1 (
    echo.
    echo [Ошибка] Лаунчер завершился с ошибкой.
    pause
)
