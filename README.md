# GreenPrism Launcher

Лёгкий кастомный лаунчер Minecraft для Windows на Python (`pywebview` + `minecraft-launcher-lib`) с современным интерфейсом, поддержкой всех популярных загрузчиков, каталогами **Modrinth** и **CurseForge**, авторизацией **Ely.by** и встроенным анализатором крашей **GreenPrism Rescue**.

## Основные возможности

- **Все загрузчики из коробки**: Vanilla, Fabric, Quilt, Forge и NeoForge — создание сборок и переключение загрузчика/версии у уже существующей сборки без потери миров, конфигов, ресурспаков и шейдеров.
- **Каталоги модов и модпаков**: поиск и установка модов, ресурспаков, шейдеров, датапаков и сборок с **Modrinth** и **CurseForge**, выбор конкретной версии, автоматическая докачка обязательных зависимостей и обновление всех модов в один клик по SHA-1.
- **Импорт и экспорт сборок**: поддержка `.mrpack` (Modrinth), архивов CurseForge (`manifest.json`) и обычных `.zip` с модами (включая Drag & Drop), а также экспорт своей сборки в `.mrpack`.
- **GreenPrism Rescue**: автоматический локальный анализ крашей после аварийного завершения игры (нехватка RAM, версия Java, недостающие зависимости Fabric/Forge/NeoForge, конфликтующие моды, ошибки OpenGL/драйвера), безопасное отключение проблемного мода (`.disabled`) и загрузка лога на `mclo.gs` в один клик.
- **Аккаунты и 3D-скины**: оффлайн-профили и вход через **Ely.by** (включая 2FA и автоматический `authlib-injector`), встроенный 3D-просмотр скина.
- **Управление сборками**: клонирование сборок, менеджер миров с бэкапами в `.zip`, галерея скриншотов с копированием в буфер, живой пинг привязанного сервера, индивидуальные настройки RAM / Java / JVM-аргументов / разрешения окна.
- **Интерфейс и интеграции**: нативное frameless-окно Windows 11 (DWM), несколько цветовых тем, кастомный фон (включая GIF) с настройкой затемнения и размытия, **Discord Rich Presence**.

## Требования

- Windows 10 / 11 (x64) с установленным Microsoft Edge WebView2 Runtime (встроен в Windows 11)
- Для запуска из исходников: **Python 3.10+**

## Запуск из исходников

```bash
git clone https://github.com/<your-username>/GreenPrism.git
cd GreenPrism
```

Запустите **`1_START.bat`** — скрипт сам установит зависимости из `requirements.txt` и откроет лаунчер.

Либо вручную:

```bash
pip install -r requirements.txt
python -O main.py
```

## Сборка `.exe`

- **`2_BUILD_EXE.bat`** — быстрая сборка `--onedir` в `dist\GreenPrism\GreenPrism.exe` (рекомендуется для повседневного использования и установки через Inno Setup).
- **`3_BUILD_PORTABLE_ONEFILE.bat`** — портативный один файл `dist\GreenPrism_Portable.exe`.
- **`installer_inno.iss`** — скрипт установщика для Inno Setup 6 (собирается из `dist\GreenPrism\`).

## Ключ CurseForge API

В готовых сборках из **GitHub Releases** каталоги Modrinth и CurseForge работают сразу из коробки.

При запуске **из исходников** поиск по Modrinth работает без ключей, а для CurseForge можно:
- указать свой ключ прямо в настройках лаунчера, **или**
- задать переменную окружения `GREENPRISM_CF_API_KEY`, **или**
- скопировать `local_secrets.example.py` в `local_secrets.py` и вписать ключ туда (`local_secrets.py` добавлен в `.gitignore` и не попадает в репозиторий).

## Лицензия

Проект распространяется под лицензией [MIT](LICENSE).

---

*Not an official Minecraft product. Not approved by or associated with Mojang or Microsoft.*
