"""GreenPrism Rescue — локальный анализатор крашей Minecraft."""

import os
import re
import time


KNOWN_LIB_NAMES = {
    "fabric-api": "Fabric API",
    "fabric": "Fabric API",
    "fabric-language-kotlin": "Fabric Language Kotlin",
    "kotlinforforge": "Kotlin for Forge",
    "cloth-config": "Cloth Config API",
    "cloth-config2": "Cloth Config API",
    "architectury": "Architectury API",
    "geckolib": "GeckoLib",
    "geckolib3": "GeckoLib",
    "curios": "Curios API",
    "trinkets": "Trinkets",
    "owo": "oωo (owo-lib)",
    "owo-lib": "oωo (owo-lib)",
    "yet_another_config_lib_v3": "YetAnotherConfigLib",
    "yacl": "YetAnotherConfigLib",
    "moonlight": "Moonlight Lib",
    "balm": "Balm",
    "puzzleslib": "Puzzles Lib",
    "collective": "Collective",
    "placebo": "Placebo",
    "bookshelf": "Bookshelf",
    "citadel": "Citadel",
    "flywheel": "Flywheel",
    "patchouli": "Patchouli",
    "sodium": "Sodium",
    "iris": "Iris Shaders",
    "indium": "Indium",
    "modmenu": "Mod Menu",
    "qsl": "Quilted Fabric API",
    "quilted_fabric_api": "Quilted Fabric API",
    "resourcefullib": "Resourceful Lib",
    "botarium": "Botarium",
    "iceberg": "Iceberg",
    "prism": "Prism",
    "creativecore": "CreativeCore",
    "coroutil": "CoroUtil",
    "playeranimator": "playerAnimator",
    "pehkui": "Pehkui",
    "cardinal-components": "Cardinal Components API",
    "cardinal-components-base": "Cardinal Components API",
    "forgeconfigapiport": "Forge Config API Port",
    "terrablender": "TerraBlender",
    "blueprint": "Blueprint",
    "autoreglib": "AutoRegLib",
    "selene": "Moonlight Lib",
    "libipn": "libIPN",
    "malilib": "MaLiLib",
}

IGNORED_MOD_IDS = {
    "minecraft", "java", "fabricloader", "fabric-loader", "fabric",
    "forge", "neoforge", "fml", "quilt_loader", "quilt-loader", "mixinextras",
}


def _read_tail_text(path, max_bytes=96 * 1024):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp1251", errors="replace")
    except Exception:
        return ""


def _find_latest_file(directory, prefix="", suffix="", min_mtime=0.0):
    if not directory or not os.path.isdir(directory):
        return None
    best_path = None
    best_mtime = min_mtime
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                name_low = entry.name.lower()
                if prefix and not name_low.startswith(prefix.lower()):
                    continue
                if suffix and not name_low.endswith(suffix.lower()):
                    continue
                try:
                    mt = entry.stat().st_mtime
                except OSError:
                    continue
                if mt >= best_mtime:
                    best_mtime = mt
                    best_path = entry.path
    except Exception:
        return None
    return best_path


def collect_crash_sources(inst_dir, live_lines=None, launch_ts=None):
    """Собирает свежий краш-репорт, hs_err, greenprism_latest.log, latest.log и вывод консоли."""
    min_mtime = (float(launch_ts) - 5.0) if launch_ts else (time.time() - 600.0)

    crash_report_path = _find_latest_file(
        os.path.join(inst_dir, "crash-reports"),
        prefix="crash-",
        suffix=".txt",
        min_mtime=min_mtime,
    )
    hs_err_path = _find_latest_file(
        inst_dir,
        prefix="hs_err_pid",
        suffix=".log",
        min_mtime=min_mtime,
    )

    # Проверяем свежесть logs/latest.log, чтобы не подхватить старый лог от прошлого запуска,
    # если игра упала до инициализации Log4j (JVM-флаг или ранняя ошибка зависимостей Fabric).
    latest_log_path = os.path.join(inst_dir, "logs", "latest.log")
    if os.path.isfile(latest_log_path):
        try:
            if os.path.getmtime(latest_log_path) < min_mtime:
                latest_log_path = None
        except OSError:
            latest_log_path = None
    else:
        latest_log_path = None

    gp_log_path = os.path.join(inst_dir, "greenprism_latest.log")
    if os.path.isfile(gp_log_path):
        try:
            if os.path.getmtime(gp_log_path) < min_mtime:
                gp_log_path = None
        except OSError:
            gp_log_path = None
    else:
        gp_log_path = None

    crash_text = _read_tail_text(crash_report_path, 96 * 1024) if crash_report_path else ""
    hs_err_text = _read_tail_text(hs_err_path, 64 * 1024) if hs_err_path else ""
    gp_text = _read_tail_text(gp_log_path, 96 * 1024) if gp_log_path else ""
    latest_text = _read_tail_text(latest_log_path, 96 * 1024) if latest_log_path else ""
    live_text = "\n".join(
        str(ln) for ln in (live_lines or []) if not str(ln).startswith("[GreenPrism]")
    )

    stdout_text = gp_text if gp_text.strip() else live_text

    # Текст для анализа (приоритет свежему краш-репорту, hs_err и прямому выводу процесса)
    combined_parts = [p for p in (crash_text, hs_err_text, stdout_text, latest_text) if p.strip()]
    combined_text = "\n\n".join(combined_parts)

    # Полный лог для загрузки на mclo.gs
    upload_parts = []
    if crash_text:
        upload_parts.append(f"=== CRASH REPORT ({os.path.basename(crash_report_path)}) ===\n{crash_text}")
    if hs_err_text:
        upload_parts.append(f"=== JVM CRASH ({os.path.basename(hs_err_path)}) ===\n{hs_err_text}")
    if stdout_text:
        upload_parts.append(f"=== PROCESS OUTPUT ===\n{stdout_text}")
    if latest_text and latest_text != stdout_text:
        upload_parts.append(f"=== logs/latest.log ===\n{latest_text}")
    full_log = "\n\n".join(upload_parts) or combined_text

    return {
        "combined": combined_text,
        "crash_text": crash_text,
        "hs_err_text": hs_err_text,
        "latest_text": stdout_text or latest_text,
        "live_text": live_text,
        "full_log": full_log,
        "crash_report_file": os.path.basename(crash_report_path) if crash_report_path else "",
        "hs_err_file": os.path.basename(hs_err_path) if hs_err_path else "",
    }


def _extract_primary_error(text):
    """Вытаскивает главную строчку ошибки из краш-репорта или лога."""
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # 1. Ошибка параметров JVM (Unrecognized VM option ...)
    for ln in lines:
        if re.search(r"Unrecognized VM option|Improperly specified VM option|Unrecognized option:", ln, re.I):
            return ln[:260]

    # 2. Явная строка Description из краш-репорта
    for ln in lines:
        if ln.startswith("Description:"):
            desc = ln[len("Description:"):].strip()
            if desc:
                return desc[:260]

    # 2. Последний Caused by: (обычно корневая причина в Java-стеке)
    caused = [ln for ln in lines if ln.startswith("Caused by:")]
    if caused:
        return caused[-1][:260]

    # 3. Java-исключение / Error
    exc_re = re.compile(
        r"([a-zA-Z0-9_.$]+(?:Exception|Error|Throwable)\s*:\s*.+|EXCEPTION_ACCESS_VIOLATION.*|Incompatible mods found!)"
    )
    for ln in reversed(lines):
        if ln.startswith("at ") or ln.startswith("[GreenPrism]"):
            continue
        m = exc_re.search(ln)
        if m:
            return m.group(1).strip()[:260]

    # 4. Последняя строка с FATAL / ERROR
    for ln in reversed(lines):
        if ln.startswith("[GreenPrism]"):
            continue
        if re.search(r"/(?:FATAL|ERROR)\]|\[FATAL\]|\[ERROR\]", ln):
            return ln[:260]

    # 5. Последняя осмысленная строка
    for ln in reversed(lines):
        if not ln.startswith("[GreenPrism]"):
            return ln[:260]
    return ""


def _build_excerpt(sources, primary_error=""):
    """Формирует компактный фрагмент лога (~35 строк) вокруг основной ошибки."""
    preferred = sources["crash_text"] or sources["hs_err_text"] or sources["latest_text"] or sources["live_text"]
    if not preferred:
        return ""
    lines = preferred.splitlines()
    if len(lines) <= 40:
        return "\n".join(lines)

    # Для краш-репорта берём шапку + стектрейс (первые 45 строк обычно содержат всё важное)
    if sources["crash_text"] or sources["hs_err_text"]:
        return "\n".join(lines[:45])

    # Для лога ищем строку с ошибкой и берём контекст вокруг неё
    anchor = -1
    if primary_error:
        needle = primary_error[:60]
        for idx in range(len(lines) - 1, -1, -1):
            if needle in lines[idx]:
                anchor = idx
                break
    if anchor == -1:
        for idx in range(len(lines) - 1, -1, -1):
            if re.search(r"Exception|Error|FATAL|Caused by|Incompatible mods", lines[idx]):
                anchor = idx
                break

    if anchor != -1:
        start = max(0, anchor - 8)
        end = min(len(lines), anchor + 28)
        return "\n".join(lines[start:end])
    return "\n".join(lines[-40:])


def _list_enabled_mods(inst_dir):
    mods_dir = os.path.join(inst_dir, "mods")
    if not os.path.isdir(mods_dir):
        return []
    out = []
    try:
        for fn in os.listdir(mods_dir):
            if fn.lower().endswith(".jar") and os.path.isfile(os.path.join(mods_dir, fn)):
                out.append(fn)
    except Exception:
        pass
    return sorted(out, key=str.lower)


def _norm_token(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _match_mod_jars(mod_hint, jar_files):
    """Находит .jar файлы в папке mods/, соответствующие mod_id, имени файла или миксину."""
    if not mod_hint or not jar_files:
        return []
    hint = str(mod_hint).strip().strip("'\"`()[]")
    hint_low = hint.lower()
    if hint_low in IGNORED_MOD_IDS or len(hint_low) < 2:
        return []

    # Точное совпадение имени файла
    for fn in jar_files:
        if fn.lower() == hint_low or fn.lower() == f"{hint_low}.jar":
            return [fn]

    # Нормализованное совпадение
    tok = _norm_token(hint.replace(".mixins.json", "").replace(".jar", ""))
    if len(tok) < 3 or tok in IGNORED_MOD_IDS:
        return []

    exact_prefix = []
    contains = []
    for fn in jar_files:
        base = os.path.splitext(fn)[0]
        fn_tok = _norm_token(base)
        if fn_tok.startswith(tok):
            exact_prefix.append(fn)
        elif tok in fn_tok:
            contains.append(fn)
    return exact_prefix or contains


def _detect_duplicate_jars(jar_files):
    """Ищет очевидные дубликаты одного мода в mods/ (например, sodium-0.5.8.jar и sodium-0.5.11.jar)."""
    groups = {}
    for fn in jar_files:
        base = os.path.splitext(fn)[0]
        # Отрезаем версию начиная с первого -<цифра> или _<цифра> или +<цифра>
        stem = re.split(r"[-_+](?:v?\d|mc\d|fabric|forge|neoforge|quilt)", base, maxsplit=1, flags=re.I)[0]
        key = _norm_token(stem)
        if len(key) >= 4:
            groups.setdefault(key, []).append(fn)
    dups = []
    for _k, files in groups.items():
        if len(files) >= 2:
            dups.append(sorted(files))
    return dups


def _pretty_dep_name(dep_id):
    clean = str(dep_id or "").strip().strip("'\"`")
    low = clean.lower()
    if low in KNOWN_LIB_NAMES:
        return KNOWN_LIB_NAMES[low]
    return clean


def analyze_crash(inst_dir, inst_meta, exit_code, live_lines=None, launch_ts=None, global_ram_mb=4096):
    """Анализирует краш Minecraft и возвращает структурированный отчёт для GreenPrism Rescue."""
    sources = collect_crash_sources(inst_dir, live_lines=live_lines, launch_ts=launch_ts)
    text = sources["combined"]
    primary_error = _extract_primary_error(text)
    excerpt = _build_excerpt(sources, primary_error=primary_error)
    jar_files = _list_enabled_mods(inst_dir)

    inst_id = str((inst_meta or {}).get("id") or "")
    inst_name = str((inst_meta or {}).get("name") or "Сборка")
    loader = str((inst_meta or {}).get("loader") or "Vanilla")
    mc_ver = str((inst_meta or {}).get("version") or "")
    inst_ram = int((inst_meta or {}).get("ram_mb") or 0)
    cur_ram_mb = inst_ram if inst_ram >= 1024 else max(1024, int(global_ram_mb or 4096))

    base_report = {
        "inst_id": inst_id,
        "inst_name": inst_name,
        "loader": loader,
        "version": mc_ver,
        "exit_code": int(exit_code),
        "crash_report_file": sources["crash_report_file"] or sources["hs_err_file"],
        "primary_error": primary_error,
        "excerpt": excerpt,
        "full_log": sources["full_log"],
        "culprit_files": [],
    }

    # 1. Нехватка оперативной памяти (RAM)
    if re.search(r"OutOfMemoryError|GC overhead limit exceeded|Java heap space|insufficient memory for the Java Runtime", text, re.I):
        suggested_mb = min(16384, max(cur_ram_mb + 2048, 4096))
        if cur_ram_mb >= 16384:
            detail = (
                f"Игре не хватило выделенной памяти (сейчас {cur_ram_mb // 1024} ГБ). "
                "Попробуйте закрыть тяжёлые программы в фоне, уменьшить дальность прорисовки или отключить ресурсоёмкие моды."
            )
            action = {"type": "open_settings", "label": "Открыть настройки RAM"}
        else:
            cur_gb = round(cur_ram_mb / 1024, 1)
            sug_gb = round(suggested_mb / 1024, 1)
            cur_str = str(int(cur_gb)) if cur_gb.is_integer() else str(cur_gb)
            sug_str = str(int(sug_gb)) if sug_gb.is_integer() else str(sug_gb)
            detail = (
                f"Процесс Java исчерпал выделенный лимит памяти ({cur_str} ГБ). "
                f"Рекомендуется увеличить объём RAM для этой сборки до {sug_str} ГБ и перезапустить игру."
            )
            action = {
                "type": "increase_ram",
                "new_ram_mb": suggested_mb,
                "label": f"Увеличить RAM до {sug_str} ГБ и перезапустить",
            }
        return {
            **base_report,
            "kind": "oom",
            "severity": "error",
            "title": "Недостаточно оперативной памяти (RAM)",
            "detail": detail,
            "action": action,
        }

    # 2. Неподходящая версия Java
    m_cv = re.search(r"UnsupportedClassVersionError.*?class file version\s*(\d+)", text, re.I)
    m_req_j = re.search(r"requires\s+Java\s*(\d+)", text, re.I)
    if m_cv or m_req_j or "UnsupportedClassVersionError" in text or "Incompatible Java version" in text:
        cv = int(m_cv.group(1)) if m_cv else 0
        req_j = int(m_req_j.group(1)) if m_req_j else 0
        if req_j:
            need_java = f"Java {req_j}"
        elif cv >= 69:
            need_java = "Java 25"
        elif cv >= 65:
            need_java = "Java 21 (для Minecraft 1.20.5+)"
        elif cv >= 61:
            need_java = "Java 17 (для Minecraft 1.18–1.20.4)"
        elif cv >= 52:
            need_java = "Java 8 (для Minecraft 1.16.5 и ниже)"
        else:
            need_java = "Java 17 или Java 21"
        return {
            **base_report,
            "kind": "java_version",
            "severity": "error",
            "title": "Неподходящая версия Java",
            "detail": (
                f"Для этой сборки требуется {need_java}. "
                "Укажите подходящий путь к javaw.exe в настройках или очистите поле Java, чтобы лаунчер выбрал нужную версию автоматически."
            ),
            "action": {"type": "open_settings", "label": "Открыть настройки Java"},
        }

    # 3. Некорректные аргументы запуска JVM (Unrecognized VM option / Could not create the Java Virtual Machine)
    m_vm_opt = (
        re.search(r"Unrecognized VM option\s+'([^']+)'", text, re.I)
        or re.search(r"Improperly specified VM option\s+'([^']+)'", text, re.I)
        or re.search(r"Unrecognized option:\s*(\S+)", text, re.I)
    )
    if m_vm_opt or "Could not create the Java Virtual Machine" in text:
        bad_flag = m_vm_opt.group(1).strip() if m_vm_opt else ""
        flag_info = f" (параметр «{bad_flag}»)" if bad_flag else ""
        return {
            **base_report,
            "kind": "jvm_args",
            "severity": "error",
            "title": "Некорректный аргумент запуска JVM",
            "detail": (
                f"Виртуальная машина Java не смогла запуститься из-за неподдерживаемого флага{flag_info}. "
                "Проверьте или очистите поле «Доп. аргументы JVM» в настройках запуска сборки."
            ),
            "action": {"type": "open_jvm_args", "label": "Изменить аргументы JVM"},
        }

    # 4. Дубликат мода
    m_dup_id = (
        re.search(r"Duplicate mods? found[\s\S]{0,260}?Mod ID:\s*'([^']+)'", text, re.I)
        or re.search(r"provides the same mod id\s*'([^']+)'", text, re.I)
        or re.search(r"Duplicate mod\s+'?([a-zA-Z0-9_-]+)'?", text, re.I)
        or re.search(r"Mod ID\s+'([^']+)'\s+appears multiple times", text, re.I)
    )
    if m_dup_id or "DuplicateModsFoundException" in text or "Duplicate mods found" in text:
        dup_id = m_dup_id.group(1).strip() if m_dup_id else ""
        matched = _match_mod_jars(dup_id, jar_files) if dup_id else []
        if len(matched) < 2:
            disk_dups = _detect_duplicate_jars(jar_files)
            if disk_dups:
                matched = disk_dups[0]
        if len(matched) >= 2:
            # Предлагаем безопасно отключить первую (обычно более старую по имени) копию
            to_disable = matched[0]
            return {
                **base_report,
                "kind": "duplicate_mod",
                "severity": "error",
                "title": "Обнаружен дубликат мода",
                "detail": (
                    f"В папке mods найдено несколько копий одного мода: {', '.join(matched)}. "
                    f"Можно безопасно отключить «{to_disable}» (файл будет переименован в .disabled без удаления)."
                ),
                "culprit_files": [to_disable],
                "action": {
                    "type": "disable_mod",
                    "filenames": [to_disable],
                    "label": f"Отключить {to_disable}",
                },
            }
        extra = f" (ID: {dup_id})" if dup_id else ""
        return {
            **base_report,
            "kind": "duplicate_mod",
            "severity": "error",
            "title": "Обнаружен дубликат мода",
            "detail": f"В папке mods установлено две версии одного и того же мода{extra}. Отключите лишнюю копию во вкладке «Моды».",
            "action": {"type": "open_mods", "label": "Открыть список модов"},
        }

    # 5. Не хватает библиотеки или зависимости (должно срабатывать раньше общего FormattedException)
    m_fab_dep = (
        re.search(
            r"(?:^|[\r\n])\s*(?:-\s*)?Install\s+'?([a-zA-Z0-9 _./-]+?)'?\s*,\s*[^\r\n]+",
            text,
            re.I,
        )
        or re.search(
            r"requires\s+[^\r\n]+?\bof\s+(?:mod\s+)?'?([a-zA-Z0-9 _.-]+?)'?(?:\s*\([^)]+\))?\s*,\s*which\s+is\s+missing",
            text,
            re.I,
        )
    )
    m_forge_dep = (
        re.search(r"Missing or unsupported mandatory dependencies:[\s\S]{0,300}?Mod ID:\s*'([^']+)'", text)
        or re.search(r"Mod\s+([a-zA-Z0-9_-]+)\s+requires\s+([a-zA-Z0-9_-]+)\s+\d", text)
    )
    dep_raw = ""
    if m_fab_dep:
        dep_raw = m_fab_dep.group(1).strip()
    elif m_forge_dep:
        dep_raw = (m_forge_dep.group(2) if m_forge_dep.lastindex and m_forge_dep.lastindex >= 2 else m_forge_dep.group(1)).strip()

    if not dep_raw and re.search(r"NoClassDefFoundError|ClassNotFoundException", text):
        m_cls = re.search(r"(?:NoClassDefFoundError|ClassNotFoundException):\s*([^\r\n]+)", text)
        if m_cls:
            cls_path = m_cls.group(1).lower()
            for k, pretty in KNOWN_LIB_NAMES.items():
                if k.replace("-", "") in cls_path.replace("/", "").replace(".", ""):
                    dep_raw = pretty
                    break

    if dep_raw or "Incompatible mods found" in text or "ModResolutionException" in text:
        dep_name = _pretty_dep_name(dep_raw)
        if dep_name and dep_name.lower() not in IGNORED_MOD_IDS:
            return {
                **base_report,
                "kind": "missing_dep",
                "severity": "error",
                "title": f"Не хватает зависимости: {dep_name}",
                "detail": (
                    f"Одному из установленных модов требуется библиотека «{dep_name}» "
                    f"(совместимая с {loader} {mc_ver}). Можно сразу найти и установить её через Modrinth."
                ),
                "action": {
                    "type": "search_modrinth",
                    "query": dep_name,
                    "label": f"Найти «{dep_name}» на Modrinth",
                },
            }
        return {
            **base_report,
            "kind": "missing_dep",
            "severity": "error",
            "title": "Конфликт или нехватка зависимостей модов",
            "detail": "Загрузчик не смог собрать совместимый набор модов. Проверьте версии модов и наличие необходимых библиотек во вкладке «Моды».",
            "action": {"type": "open_mods", "label": "Открыть список модов"},
        }

    # 5. Краш драйвера видеокарты / OpenGL / GLFW
    gpu_dll_map = {
        "nvoglv64.dll": "NVIDIA",
        "nvoglv32.dll": "NVIDIA",
        "atio6axx.dll": "AMD",
        "atioglxx.dll": "AMD",
        "igxelpicd64.dll": "Intel",
        "ig9icd64.dll": "Intel",
        "ig11icd64.dll": "Intel",
    }
    found_vendor = ""
    found_dll = ""
    text_low = text.lower()
    for dll, vendor in gpu_dll_map.items():
        if dll in text_low:
            found_vendor = vendor
            found_dll = dll
            break
    if found_vendor or re.search(
        r"GLFW error|Pixel format not accelerated|WGL: The driver does not appear to support OpenGL|EXCEPTION_ACCESS_VIOLATION.*(?:opengl|glfw)",
        text,
        re.I,
    ):
        vendor_part = f" ({found_vendor}, модуль {found_dll})" if found_vendor else ""
        return {
            **base_report,
            "kind": "gpu_driver",
            "severity": "error",
            "title": f"Сбой графического драйвера или OpenGL{vendor_part}",
            "detail": (
                "Видеодрайвер завершил процесс игры аварийно или не смог создать контекст OpenGL. "
                "Обновите драйвер видеокарты с официального сайта, проверьте запуск на дискретной видеокарте "
                "или временно отключите шейдеры и моды рендеринга (Iris / OptiFine / Sodium)."
            ),
            "action": {"type": "open_mods", "label": "Открыть моды"},
        }

    # 6. Конкретный виновный мод (Mixin / Forge Mod File / Suspected Mods)
    culprit_candidates = []
    for pat in (
        r"Mod File:\s*([^\r\n]+?\.jar)",
        r"Mixin(?:Apply|Transformer)Error.*?from mod\s+([a-zA-Z0-9_.-]+)",
        r"InvalidMixinException.*?in\s+([a-zA-Z0-9_.-]+)\.mixins\.json",
        r"Failed to load mod\s+'?([a-zA-Z0-9_.-]+)'?",
        r"Suspected Mods?:\s*([^\r\n]+)",
    ):
        for m in re.finditer(pat, text, re.I):
            raw_val = m.group(1).strip()
            # Из строки вида "Sodium (sodium)" вытаскиваем оба токена
            for piece in re.split(r"[,;()]+", raw_val):
                p = os.path.basename(piece.strip())
                if p and p.lower() not in IGNORED_MOD_IDS:
                    culprit_candidates.append(p)

    matched_jar = ""
    culprit_label = ""
    for cand in culprit_candidates:
        hits = _match_mod_jars(cand, jar_files)
        if hits:
            matched_jar = hits[0]
            culprit_label = cand
            break

    if matched_jar:
        is_mixin = bool(re.search(r"Mixin(?:Apply|Transformer)Error|InvalidMixinException", text, re.I))
        title = "Конфликт Mixin — найден проблемный мод" if is_mixin else "Сбой вызван конкретным модом"
        return {
            **base_report,
            "kind": "culprit_mod",
            "severity": "error",
            "title": title,
            "detail": (
                f"По логу сбой связан с модом «{matched_jar}». "
                "Можно безопасно отключить его (файл будет переименован в .disabled, без удаления) и попробовать запустить игру снова."
            ),
            "culprit_files": [matched_jar],
            "action": {
                "type": "disable_mod",
                "filenames": [matched_jar],
                "label": f"Отключить {matched_jar}",
            },
        }

    if re.search(r"Mixin(?:Apply|Transformer)Error|InvalidMixinException", text, re.I):
        extra = f" Возможный мод: «{culprit_candidates[0]}»." if culprit_candidates else ""
        return {
            **base_report,
            "kind": "culprit_mod",
            "severity": "warn",
            "title": "Конфликт Mixin между модами",
            "detail": f"Два мода попытались изменить один и тот же участок кода игры.{extra} Отключите недавно добавленные моды во вкладке «Моды».",
            "action": {"type": "open_mods", "label": "Открыть список модов"},
        }

    # 7. Повреждённый профиль загрузчика / прокси / WinError 2
    if re.search(
        r"couldn't locate the game|absent from the class path|Cannot find launch target|Can't connect to SOCKS proxy|WinError 2",
        text,
        re.I,
    ):
        return {
            **base_report,
            "kind": "broken_profile",
            "severity": "error",
            "title": "Повреждены файлы версии или загрузчика",
            "detail": (
                "Не удалось найти часть файлов загрузчика, клиентский JAR или Java. "
                "Нажмите «Починить сборку» — лаунчер перепроверит и переустановит профиль."
            ),
            "action": {"type": "repair_instance", "label": "Починить сборку"},
        }

    # 8. Неизвестная причина — выводим главную строку ошибки
    return {
        **base_report,
        "kind": "unknown",
        "severity": "warn",
        "title": f"Игра завершилась с ошибкой (код {exit_code})",
        "detail": (
            "Точную причину автоматически определить не удалось. "
            "Ниже выделена основная строка ошибки из лога — вы можете посмотреть фрагмент лога или загрузить его на mclo.gs."
        ),
        "action": {"type": "open_logs", "label": "Открыть полную консоль"},
    }


def upload_to_mclogs(content, http_session=None, user_agent="GreenPrismLauncher"):
    """Загружает текст лога на https://api.mclo.gs/1/log и возвращает короткую ссылку."""
    text = str(content or "").strip()
    if not text:
        return {"ok": False, "error": "Лог пуст — нечего загружать"}
    # mclo.gs ограничивает размер ~10 МБ; 500 КБ хватает с запасом для любого краша
    if len(text) > 500_000:
        text = text[-500_000:]
    try:
        if http_session is not None:
            r = http_session.post(
                "https://api.mclo.gs/1/log",
                data={"content": text},
                headers={"User-Agent": user_agent},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json() or {}
        else:
            import json
            import urllib.parse
            import urllib.request

            body = urllib.parse.urlencode({"content": text}).encode("utf-8")
            req = urllib.request.Request(
                "https://api.mclo.gs/1/log",
                data=body,
                headers={"User-Agent": user_agent, "Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if data.get("success") and data.get("url"):
            return {"ok": True, "url": str(data["url"]), "raw": str(data.get("raw") or "")}
        return {"ok": False, "error": str(data.get("error") or "Сервис mclo.gs вернул ошибку")}
    except Exception as e:
        return {"ok": False, "error": f"Не удалось загрузить лог на mclo.gs: {e}"}
