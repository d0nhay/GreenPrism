@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYCMD=py"
where py >nul 2>&1 || set "PYCMD=python"

echo [GreenPrism] Проверка зависимостей и PyInstaller...
%PYCMD% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo [Ошибка] Не удалось установить библиотеки.
    pause
    exit /b 1
)

set "VER_FLAG="
if exist "version_info.txt" (
    set "VER_FLAG=--version-file=version_info.txt"
)
set "SECRET_FLAG="
if exist "local_secrets.py" (
    echo [GreenPrism] Найден local_secrets.py — ключ CurseForge будет встроен!
    set "SECRET_FLAG=--hidden-import=local_secrets"
)

echo.
echo [GreenPrism] Сборка портативного ОДНОГО файла (--onefile, для GitHub Releases)...
%PYCMD% -m PyInstaller --noconfirm --clean --onefile --windowed --noupx --icon="icon.ico" --add-data "ui;ui" --hidden-import=certifi --hidden-import=requests --hidden-import=minecraft_launcher_lib --hidden-import=pypresence --hidden-import=crash_rescue --collect-data=certifi --collect-data=webview --collect-data=minecraft_launcher_lib --exclude-module tkinter --exclude-module _tkinter --exclude-module unittest --exclude-module pydoc --exclude-module doctest --exclude-module test --exclude-module PIL --exclude-module numpy --exclude-module matplotlib --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 --exclude-module PySide6 --exclude-module wx --exclude-module gi --exclude-module gtk %VER_FLAG% %SECRET_FLAG% --name "GreenPrism_Portable" main.py
if errorlevel 1 (
    echo [Ошибка] Сборка не удалась.
    pause
    exit /b 1
)

echo.
echo [Готово] Портативный файл собран: dist\GreenPrism_Portable.exe
explorer dist
pause
