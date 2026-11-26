import asyncio
import os
import queue
import re
import sys
import time

_BOOT_T0 = time.perf_counter()

import json
import threading
import webview

# Discord Rich Presence настройки
DISCORD_CLIENT_ID = "1573850750784774144"
DISCORD_LARGE_IMAGE = "greenprism_logo"

try:
    from pypresence import Presence
    HAVE_PYPRESENCE = True
except Exception:
    Presence = None
    HAVE_PYPRESENCE = False

# Лёгкие stdlib-модули, нужные только при действиях пользователя, импортируются лениво
# Тяжёлые requests и minecraft_launcher_lib (400-800 мс) загружаются ТОЛЬКО после показа окна
_mclib_mod = None
_http_session = None
_http_lock = threading.Lock()
_mclib_lock = threading.Lock()


def _parse_proxy_endpoint(raw_url):
    """Аккуратно разбирает прокси-строку Windows/env (в т.ч. битый формат 'http://127.0.0.1:10808')."""
    from urllib.parse import urlparse
    s = str(raw_url or "").strip()
    if not s:
        return None
    if "://" not in s:
        s = "http://" + s
    try:
        u = urlparse(s)
        host = (u.hostname or "").strip()
        port = u.port
        scheme = (u.scheme or "http").lower()
        if not host or not port or host.lower() in ("http", "https", "socks", "socks4", "socks5"):
            return None
        return (scheme, host, int(port))
    except Exception:
        return None


def _is_tcp_reachable(host, port, timeout=0.35):
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _detect_working_proxy():
    """Возвращает (scheme, host, port) рабочего системного прокси или None, если прокси нет/битый."""
    import urllib.request
    try:
        proxies = urllib.request.getproxies() or {}
    except Exception:
        return None
    for key in ("https", "http", "all", "socks", "socks5", "socks4"):
        raw = proxies.get(key)
        parsed = _parse_proxy_endpoint(raw)
        if parsed and _is_tcp_reachable(parsed[1], parsed[2]):
            return parsed
    return None


def _build_java_proxy_args():
    """Генерирует безопасные JVM-флаги сети, защищающие Java от бага 'Can't connect to SOCKS proxy:http'."""
    proxy = _detect_working_proxy()
    base = ["-Djava.net.useSystemProxies=false", "-Djava.net.preferIPv4Stack=true"]
    if not proxy:
        return base + ["-DsocksProxyHost=", "-Dhttp.proxyHost=", "-Dhttps.proxyHost="]
    scheme, host, port = proxy
    if scheme.startswith("socks"):
        return base + [f"-DsocksProxyHost={host}", f"-DsocksProxyPort={port}", "-Dhttp.proxyHost=", "-Dhttps.proxyHost="]
    return base + [
        f"-Dhttp.proxyHost={host}",
        f"-Dhttp.proxyPort={port}",
        f"-Dhttps.proxyHost={host}",
        f"-Dhttps.proxyPort={port}",
        "-DsocksProxyHost=",
    ]


def _clean_java_env(base_env=None):
    """Удаляет битые прокси-переменные окружения перед запуском Java-установщиков и игры."""
    env = dict(os.environ if base_env is None else base_env)
    for k in list(env.keys()):
        if k.upper() in ("ALL_PROXY", "SOCKS_PROXY", "SOCKS5_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY"):
            env.pop(k, None)
    for opt_key in ("_JAVA_OPTIONS", "JAVA_TOOL_OPTIONS"):
        val = env.get(opt_key)
        if val and any(tok in val.lower() for tok in ("proxy", "usesystemproxies")):
            cleaned = re.sub(r"-D(?:java\.net\.useSystemProxies|socksProxy\w+|https?\.proxy\w+)=\S*", "", val).strip()
            if cleaned:
                env[opt_key] = cleaned
            else:
                env.pop(opt_key, None)
    return env


def _inject_java_net_flags(cmd):
    """Подставляет безопасные сетевые флаги сразу после java/javaw, если их ещё нет в команде."""
    if not isinstance(cmd, (list, tuple)) or not cmd:
        return cmd
    exe_name = str(cmd[0]).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if exe_name not in ("java", "java.exe", "javaw", "javaw.exe"):
        return cmd
    joined = " ".join(str(x) for x in cmd[1:])
    if "-Djava.net.useSystemProxies" in joined or "-DsocksProxyHost" in joined:
        return list(cmd)
    return [cmd[0]] + _build_java_proxy_args() + list(cmd[1:])


def _ensure_jar_in_classpath(command, client_jar, target_version, mc_dir):
    """Гарантирует наличие реального клиентского JAR в аргументе -cp/-classpath команды запуска Java."""
    cmd = list(command)
    cp_idx = -1
    for i, tok in enumerate(cmd):
        if tok in ("-cp", "-classpath") and i + 1 < len(cmd):
            cp_idx = i + 1
            break
    if cp_idx == -1:
        return cmd, False

    sep = os.pathsep
    entries = [p for p in str(cmd[cp_idx]).split(sep) if p]
    norm_client = os.path.normcase(os.path.abspath(client_jar))
    if any(os.path.normcase(os.path.abspath(p)) == norm_client for p in entries):
        return cmd, False

    ghost_jar = os.path.normcase(os.path.abspath(os.path.join(mc_dir, "versions", str(target_version), f"{target_version}.jar")))
    replaced = False
    new_entries = []
    for p in entries:
        if os.path.normcase(os.path.abspath(p)) == ghost_jar and not os.path.isfile(p):
            if not replaced:
                new_entries.append(client_jar)
                replaced = True
        else:
            new_entries.append(p)
    if not replaced:
        new_entries.append(client_jar)
    cmd[cp_idx] = sep.join(new_entries)
    return cmd, True


def _maven_coord_to_relpath(coord):
    """Преобразует Maven-координату group:artifact:version[:classifier][@ext] в относительный путь."""
    s = str(coord or "").strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
    if not s:
        return ""
    ext = "jar"
    if "@" in s:
        s, ext = s.rsplit("@", 1)
        ext = ext.strip() or "jar"
    parts = s.split(":")
    if len(parts) < 3:
        return ""
    group, artifact, version = parts[0].strip(), parts[1].strip(), parts[2].strip()
    classifier = parts[3].strip() if len(parts) >= 4 and parts[3].strip() else ""
    if not (group and artifact and version):
        return ""
    fname = f"{artifact}-{version}-{classifier}.{ext}" if classifier else f"{artifact}-{version}.{ext}"
    return f"{group.replace('.', '/')}/{artifact}/{version}/{fname}"


def _lib_rules_allow_current_os(lib):
    """Проверяет правила Minecraft library.rules для текущей ОС."""
    rules = lib.get("rules")
    if not rules:
        return True
    cur_os = "windows" if sys.platform == "win32" else ("osx" if sys.platform == "darwin" else "linux")
    allowed = False
    for rule in rules:
        action = rule.get("action", "allow")
        os_info = rule.get("os")
        matched = True
        if isinstance(os_info, dict):
            name = os_info.get("name")
            if name and name != cur_os:
                matched = False
        if matched:
            allowed = (action == "allow")
    return allowed


def _get_mclib():
    global _mclib_mod
    if _mclib_mod is None:
        with _mclib_lock:
            if _mclib_mod is None:
                import minecraft_launcher_lib as _m
                import subprocess as _sub
                _orig_run = _sub.run
                _orig_popen = _sub.Popen

                def _patched_run(*args, **kwargs):
                    if args:
                        new_cmd = _inject_java_net_flags(args[0])
                        if new_cmd is not args[0]:
                            args = (new_cmd,) + args[1:]
                            if "env" not in kwargs or kwargs["env"] is None:
                                kwargs["env"] = _clean_java_env()
                    if sys.platform == "win32":
                        cf = kwargs.get("creationflags", 0)
                        kwargs["creationflags"] = cf | 0x08000000  # CREATE_NO_WINDOW
                        if "startupinfo" not in kwargs:
                            si = _sub.STARTUPINFO()
                            si.dwFlags |= _sub.STARTF_USESHOWWINDOW
                            si.wShowWindow = _sub.SW_HIDE
                            kwargs["startupinfo"] = si
                    return _orig_run(*args, **kwargs)

                def _patched_popen(*args, **kwargs):
                    if sys.platform == "win32":
                        cf = kwargs.get("creationflags", 0)
                        kwargs["creationflags"] = cf | 0x08000000  # CREATE_NO_WINDOW
                    return _orig_popen(*args, **kwargs)

                _sub.run = _patched_run
                _sub.Popen = _patched_popen
                if hasattr(_m, "subprocess"):
                    _m.subprocess.run = _patched_run
                    _m.subprocess.Popen = _patched_popen
                _mclib_mod = _m
    return _mclib_mod


def _get_http():
    global _http_session
    if _http_session is None:
        with _http_lock:
            if _http_session is None:
                import urllib.request
                import requests
                from requests.adapters import HTTPAdapter
                s = requests.Session()
                # Если в Windows прописан битый/мёртвый прокси — отключаем trust_env, чтобы запросы не падали
                try:
                    sys_proxies = urllib.request.getproxies() or {}
                    if sys_proxies and _detect_working_proxy() is None:
                        s.trust_env = False
                except Exception:
                    pass
                adapter = HTTPAdapter(pool_connections=12, pool_maxsize=20, max_retries=2)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                s.headers.update({"User-Agent": USER_AGENT})
                _http_session = s
    return _http_session


# Опциональный локальный файл с встроенным API-ключом CurseForge (в .gitignore, не попадает на GitHub).
# При сборке через 2_BUILD_EXE.bat PyInstaller вшивает его прямо в сборку.
try:
    import local_secrets as _local_secrets
    EMBEDDED_CF_API_KEY = str(getattr(_local_secrets, "CF_API_KEY", "") or "").strip()
except Exception:
    EMBEDDED_CF_API_KEY = ""
if not EMBEDDED_CF_API_KEY:
    EMBEDDED_CF_API_KEY = os.environ.get("GREENPRISM_CF_API_KEY", "").strip()

# Постоянная папка данных — НЕ меняется при обновлениях лаунчера
BASE_DIR = os.path.join(os.path.expanduser("~"), ".greenprism_launcher")
DEFAULT_MC_DIR = os.path.join(BASE_DIR, "minecraft")
INSTANCES_DIR = os.path.join(BASE_DIR, "instances")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
AUTHLIB_INJECTOR_PATH = os.path.join(BASE_DIR, "authlib-injector.jar")
VERSIONS_CACHE_PATH = os.path.join(BASE_DIR, "versions_cache.json")
META_CACHE_PATH = os.path.join(BASE_DIR, "meta_cache.json")
WEBVIEW_CACHE_DIR = os.path.join(BASE_DIR, "webview_cache")
VERSIONS_CACHE_TTL = 6 * 3600  # 6 часов

USER_AGENT = "GreenPrismLauncher/1.0 (github-open-source)"
CF_LOADER_MAP = {"Forge": 1, "Fabric": 4, "Quilt": 5, "NeoForge": 6}
CF_CLASS_MAP = {
    "mod": 6,
    "resourcepack": 12,
    "shader": 6552,
    "datapack": 6945,
}
CONTENT_FOLDERS = {
    "mod": "mods",
    "resourcepack": "resourcepacks",
    "shader": "shaderpacks",
    "datapack": "datapacks",
}
CF_SORT_MAP = {
    "relevance": 2,
    "downloads": 6,
    "updated": 3,
    "newest": 11,
}


def resource_path(relative_path):
    """Корректный путь к файлам UI как при запуске .py, так и внутри PyInstaller .exe"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.abspath(os.path.join(base, relative_path))

def _safe_join(base_dir, rel_path, allow_base=False):
    """Защита от path traversal при распаковке .mrpack, работе с мирами и файлами сборок."""
    base_abs = os.path.abspath(base_dir)
    rel_clean = str(rel_path or "").replace("\\", "/").lstrip("/")
    target = os.path.abspath(os.path.join(base_abs, rel_clean))
    if os.path.commonpath([base_abs, target]) != base_abs:
        raise ValueError(f"Unsafe path: {rel_path}")
    if not allow_base and target == base_abs:
        raise ValueError(f"Unsafe path (matches base): {rel_path}")
    return target


def _copy_image_to_clipboard_win32_gdi(filepath):
    """Копирует изображение в системный буфер Windows через GDI+ и User32 (CF_HDROP + PNG + CF_BITMAP/CF_DIB)."""
    try:
        import ctypes
        from ctypes import wintypes
        import time

        abs_path = os.path.abspath(filepath)
        if not os.path.isfile(abs_path):
            return False

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        gdi32 = ctypes.windll.gdi32
        gdiplus = ctypes.windll.gdiplus

        # Явно задаём 64-битные типы для исключения усечения указателей памяти (HGLOBAL)
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterClipboardFormatW.restype = wintypes.UINT
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL

        class _GdiplusStartupInput(ctypes.Structure):
            _fields_ = [
                ("GdiplusVersion", wintypes.DWORD),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wintypes.BOOL),
                ("SuppressExternalCodecs", wintypes.BOOL),
            ]

        class _DROPFILES(ctypes.Structure):
            _fields_ = [
                ("pFiles", wintypes.DWORD),
                ("pt", wintypes.POINT),
                ("fNC", wintypes.BOOL),
                ("fWide", wintypes.BOOL),
            ]

        # 1. Подготовка CF_HDROP (файловый список).
        # Discord, Telegram, Explorer и браузеры всегда безошибочно принимают изображение через CF_HDROP!
        GMEM_MOVEABLE = 0x0002
        GMEM_ZEROINIT = 0x0040
        h_drop = None
        try:
            path_utf16 = (abs_path + "\0\0").encode("utf-16-le")
            df_size = ctypes.sizeof(_DROPFILES)
            h_drop = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, df_size + len(path_utf16))
            if h_drop:
                p_drop = kernel32.GlobalLock(h_drop)
                if p_drop:
                    df = _DROPFILES()
                    df.pFiles = df_size
                    df.fWide = True
                    ctypes.memmove(p_drop, ctypes.byref(df), df_size)
                    ctypes.memmove(p_drop + df_size, path_utf16, len(path_utf16))
                    kernel32.GlobalUnlock(h_drop)
                else:
                    kernel32.GlobalFree(h_drop)
                    h_drop = None
        except Exception:
            h_drop = None

        # 2. Подготовка нативного буфера PNG (для Discord / Chromium)
        h_png = None
        png_format = user32.RegisterClipboardFormatW("PNG")
        try:
            with open(abs_path, "rb") as f:
                raw_png = f.read()
            if raw_png and png_format:
                h_png = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw_png))
                if h_png:
                    p_png = kernel32.GlobalLock(h_png)
                    if p_png:
                        ctypes.memmove(p_png, raw_png, len(raw_png))
                        kernel32.GlobalUnlock(h_png)
                    else:
                        kernel32.GlobalFree(h_png)
                        h_png = None
        except Exception:
            h_png = None

        # 3. Подготовка HBITMAP через GDI+ (CF_BITMAP синтезирует CF_DIB для Telegram и Paint)
        hbitmap = None
        token = ctypes.c_ulong()
        startup_in = _GdiplusStartupInput(1, None, False, False)
        gdi_started = (gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup_in), None) == 0)
        if gdi_started:
            try:
                bitmap = ctypes.c_void_p()
                if gdiplus.GdipCreateBitmapFromFile(ctypes.c_wchar_p(abs_path), ctypes.byref(bitmap)) == 0 and bitmap:
                    try:
                        hb = wintypes.HBITMAP()
                        if gdiplus.GdipCreateHBITMAPFromBitmap(bitmap, ctypes.byref(hb), 0) == 0 and hb:
                            hbitmap = hb
                    finally:
                        gdiplus.GdipDisposeImage(bitmap)
            finally:
                gdiplus.GdiplusShutdown(token)

        # 4. Открытие буфера обмена и запись данных
        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.04)

        if not opened:
            if h_drop:
                kernel32.GlobalFree(h_drop)
            if h_png:
                kernel32.GlobalFree(h_png)
            if hbitmap:
                gdi32.DeleteObject(hbitmap)
            return False

        try:
            user32.EmptyClipboard()
            success = False

            # Помещаем CF_HDROP (15) - в Discord при этом сразу крепится файл скриншота!
            if h_drop:
                if user32.SetClipboardData(15, h_drop):
                    success = True
                    h_drop = None

            # Помещаем PNG (зарегистрированный формат)
            if h_png and png_format:
                if user32.SetClipboardData(png_format, h_png):
                    success = True
                    h_png = None

            # Помещаем CF_BITMAP (2) - синтезирует CF_DIB (8) для Telegram и Paint
            if hbitmap:
                if user32.SetClipboardData(2, hbitmap):
                    success = True
                    hbitmap = None

            return success
        finally:
            user32.CloseClipboard()
            if h_drop:
                kernel32.GlobalFree(h_drop)
            if h_png:
                kernel32.GlobalFree(h_png)
            if hbitmap:
                gdi32.DeleteObject(hbitmap)
    except Exception:
        return False


def _copy_image_to_clipboard_powershell(filepath):
    """Резервное копирование изображения через встроенный PowerShell."""
    try:
        import subprocess
        clean_path = os.path.abspath(filepath).replace("'", "''")
        cmd = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            "$do = New-Object System.Windows.Forms.DataObject; "
            f"$img = [System.Drawing.Image]::FromFile('{clean_path}'); "
            "$do.SetImage($img); "
            "$files = New-Object System.Collections.Specialized.StringCollection; "
            f"$files.Add('{clean_path}'); "
            "$do.SetFileDropList($files); "
            "[System.Windows.Forms.Clipboard]::SetDataObject($do, $true); "
            "$img.Dispose()"
        )
        si = None
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        res = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", cmd],
            startupinfo=si,
            creationflags=creationflags,
            timeout=4,
        )
        return res.returncode == 0
    except Exception:
        return False


def _copy_image_to_clipboard(filepath):
    if not filepath or not os.path.isfile(filepath):
        return False
    if sys.platform == "win32":
        if _copy_image_to_clipboard_win32_gdi(filepath):
            return True
        return _copy_image_to_clipboard_powershell(filepath)
    elif sys.platform == "darwin":
        try:
            import subprocess
            cmd = f'set the clipboard to (read (POSIX file "{os.path.abspath(filepath)}") as «class PNGf»)'
            res = subprocess.run(["osascript", "-e", cmd], timeout=3)
            return res.returncode == 0
        except Exception:
            return False
    else:
        try:
            import subprocess
            with open(filepath, "rb") as f:
                res = subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png"], stdin=f, timeout=3)
            return res.returncode == 0
        except Exception:
            return False


def _file_sha1(path):
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_hashes(path):
    import hashlib
    h1 = hashlib.sha1()
    h512 = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h1.update(chunk)
            h512.update(chunk)
    return h1.hexdigest(), h512.hexdigest()


def _pack_varint(val):
    out = bytearray()
    val &= 0xFFFFFFFF
    while True:
        b = val & 0x7F
        val >>= 7
        if val:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Соединение закрыто сервером")
        buf.extend(chunk)
    return bytes(buf)


def _read_varint_sock(sock):
    num_read = 0
    result = 0
    while True:
        byte = _recv_exact(sock, 1)[0]
        result |= (byte & 0x7F) << (7 * num_read)
        num_read += 1
        if num_read > 5:
            raise ValueError("Некорректный VarInt")
        if (byte & 0x80) == 0:
            return result


def _strip_mc_motd(desc):
    if isinstance(desc, str):
        return re.sub(r"§.", "", desc).strip()
    if isinstance(desc, dict):
        parts = [str(desc.get("text") or "")]
        for ex in desc.get("extra") or []:
            parts.append(_strip_mc_motd(ex))
        return re.sub(r"§.", "", "".join(parts)).strip()
    if isinstance(desc, list):
        return re.sub(r"§.", "", "".join(_strip_mc_motd(x) for x in desc)).strip()
    return ""


def _analyze_mc_logs(lines):
    """Умный анализатор крашей Minecraft: находит вероятную причину и даёт понятный совет."""
    text = "\n".join(lines or [])
    if not text.strip():
        return []

    findings = []

    m_java = re.search(r"UnsupportedClassVersionError.*class file version (\d+)", text)
    if m_java or "UnsupportedClassVersionError" in text or "requires Java" in text:
        cv = int(m_java.group(1)) if m_java else 0
        need_java = "Java 21 (для 1.20.5+)" if cv >= 65 else ("Java 17 (для 1.18–1.20.4)" if cv >= 61 else "более новую Java (17 или 21)")
        findings.append({
            "severity": "error",
            "title": "Неподходящая версия Java",
            "detail": f"Требуется {need_java}. Укажите путь к javaw.exe в настройках или оставьте поле пустым для автовыбора.",
            "action": "settings",
            "action_label": "Открыть настройки",
        })

    if "OutOfMemoryError" in text or "GC overhead limit exceeded" in text:
        findings.append({
            "severity": "error",
            "title": "Недостаточно оперативной памяти (RAM)",
            "detail": "Выделенной памяти недостаточно для запуска. Увеличьте лимит RAM в настройках (для сборок с модами обычно требуется 4–8 ГБ).",
            "action": "settings",
            "action_label": "Настроить RAM",
        })

    m_fab_dep = re.search(r"Install\s+([a-zA-Z0-9 _-]+?),\s+(?:any version|version[^\n]+)", text)
    m_forge_dep = re.search(r"Missing or unsupported mandatory dependencies:[\s\S]{0,240}?Mod ID:\s*'([^']+)'", text)
    if "Incompatible mods found" in text or "ModResolutionException" in text or m_fab_dep or m_forge_dep:
        missing_mod = ""
        if m_fab_dep:
            missing_mod = m_fab_dep.group(1).strip()
        elif m_forge_dep:
            missing_mod = m_forge_dep.group(1).strip()
        extra = f" Отсутствует или несовместим мод: «{missing_mod}»." if missing_mod else ""
        findings.append({
            "severity": "error",
            "title": "Конфликт или нехватка зависимостей",
            "detail": f"Загрузчик не смог разрешить зависимости модов.{extra} Проверьте вкладку «Моды» и установите недостающую библиотеку.",
            "action": "mods",
            "action_label": "К списку модов",
        })

    m_mixin = re.search(r"Mixin(?:Apply|Transformer)Error.*?from mod ([a-zA-Z0-9_-]+)", text, re.I)
    if m_mixin or "MixinApplyError" in text or "MixinTransformerError" in text:
        culprit = f" Возможный источник: «{m_mixin.group(1)}»." if m_mixin else ""
        findings.append({
            "severity": "warn",
            "title": "Конфликт Mixin",
            "detail": f"Конфликт патчей между модами.{culprit} Отключите недавно добавленные моды во вкладке «Моды» для проверки.",
            "action": "mods",
            "action_label": "Открыть моды",
        })

    if "GLFW error" in text or "Pixel format not accelerated" in text or "WGL: The driver does not appear to support OpenGL" in text:
        findings.append({
            "severity": "error",
            "title": "Ошибка OpenGL / GLFW",
            "detail": "Не удалось инициализировать контекст OpenGL. Обновите драйвер видеокарты или отключите несовместимый мод рендеринга / шейдер.",
            "action": "none",
            "action_label": "",
        })

    if not findings and re.search(r"Процесс завершён с кодом (?!0\b)-?\d+", text):
        findings.append({
            "severity": "warn",
            "title": "Аварийное завершение игры",
            "detail": "Проверьте лог выше или папку crash-reports в каталоге сборки. При повреждении файлов используйте кнопку «Починить».",
            "action": "folder",
            "action_label": "Папка сборки",
        })

    if "couldn't locate the game" in text or "absent from the class path" in text:
        findings.append({
            "severity": "error",
            "title": "Не найден client.jar в classpath (Fabric)",
            "detail": "В профиле Fabric отсутствует ссылка на базовый client.jar или файл версии повреждён. Перезапустите сборку или нажмите «Починить».",
            "action": "none",
            "action_label": "",
        })

    if "Can't connect to SOCKS proxy" in text or "SocksSocketImpl" in text:
        findings.append({
            "severity": "error",
            "title": "Сетевая ошибка Java (системный прокси)",
            "detail": "В Windows задан некорректный системный прокси (часто остаётся после VPN-клиентов). Выполните переустановку загрузчика кнопкой «Починить».",
            "action": "none",
            "action_label": "",
        })

    if "Cannot find launch target" in text or "Invalid paths argument, contained no existing paths" in text:
        findings.append({
            "severity": "error",
            "title": "Неполная установка NeoForge / Forge",
            "detail": "Часть библиотек загрузчика или патченый клиент не были загружены. Нажмите «Починить» для чистой переустановки профиля.",
            "action": "none",
            "action_label": "",
        })

    if "WinError 2" in text or "FileNotFoundError" in text or "Не удается найти указанный файл" in text:
        findings.append({
            "severity": "error",
            "title": "Не найден исполняемый файл или Java (WinError 2)",
            "detail": "Не найден javaw.exe либо повреждены файлы версии. Очистите «Свой путь к Java» в настройках или нажмите «Починить».",
            "action": "settings",
            "action_label": "Открыть настройки",
        })

    return findings


def _neoforge_ver_to_mc(nf_ver):
    """Преобразует версию NeoForge (напр. 21.1.172, 21.0.167, 26.3.0.115, 26.1.2.129) в версию Minecraft."""
    s = str(nf_ver or "").strip()
    if not s or s.startswith("0."):
        return ""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", s)
    if not m:
        return ""
    g1, g2, g3, g4 = m.groups()
    maj = int(g1)
    if maj >= 26:
        if g4 is not None:
            return f"{g1}.{g2}.{g3}" if int(g3) != 0 else f"{g1}.{g2}"
        return f"{g1}.{g2}"
    if 20 <= maj <= 25:
        return f"1.{g1}" if int(g2) == 0 else f"1.{g1}.{g2}"
    return ""


def _mc_to_neoforge_prefixes(mc_ver):
    """Возвращает возможные префиксы артефакта NeoForge для указанной версии Minecraft."""
    s = str(mc_ver or "").strip()
    m1 = re.match(r"^1\.(\d+)(?:\.(\d+))?$", s)
    if m1:
        minor = int(m1.group(1))
        patch = int(m1.group(2) or 0)
        return [f"{minor}.{patch}."]
    m26 = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?$", s)
    if m26 and int(m26.group(1)) >= 26:
        if m26.group(3) is not None:
            return [f"{m26.group(1)}.{m26.group(2)}.{m26.group(3)}."]
        return [f"{m26.group(1)}.{m26.group(2)}.0.", f"{m26.group(1)}.{m26.group(2)}."]
    return []


def _format_mc_uuid(raw_uuid):
    """Нормализует 32-символьный или стандартный UUID к формату 8-4-4-4-12."""
    clean = re.sub(r"[^0-9a-fA-F]", "", str(raw_uuid or "")).lower()
    if len(clean) == 32:
        return f"{clean[0:8]}-{clean[8:12]}-{clean[12:16]}-{clean[16:20]}-{clean[20:32]}"
    return str(raw_uuid or "").strip()


def _supports_quick_play(version_str):
    """Minecraft 1.20+ поддерживает --quickPlayMultiplayer, более старые — --server / --port."""
    nums = [int(x) for x in re.findall(r"\d+", str(version_str or ""))]
    if not nums:
        return True
    if nums[0] >= 2:
        return True
    if nums[0] == 1 and len(nums) >= 2 and nums[1] >= 20:
        return True
    return False



def _parse_ver_tuple(ver_str):
    """Ключ для сортировки версий по убыванию (26.3 > 26.1.2 > 1.21.11 > 1.21.9 > 0.31.0 > 0.31.0-beta.6)."""
    s = str(ver_str or "").strip()
    m_pre = re.search(r"(?:[-._+]?(?:pre|rc|alpha|beta|snapshot)\b|\d+w\d+)", s, re.I)
    if m_pre:
        main_part = s[:m_pre.start()]
        pre_part = s[m_pre.start():]
        is_pre = 1
    else:
        main_part = s
        pre_part = ""
        is_pre = 0
    main_nums = tuple(int(x) for x in re.findall(r"\d+", main_part))
    pre_nums = tuple(int(x) for x in re.findall(r"\d+", pre_part))
    return (main_nums, -is_pre, pre_nums, s)


def sort_mc_versions(versions, mc_order_map=None, only_manifest=False):
    """Сортирует версии строго от новых к старым. При only_manifest=True скрывает несуществующие в Mojang версии (вроде 1.4.0)."""
    unique = list(dict.fromkeys(str(v).strip() for v in versions if v and str(v).strip()))
    if mc_order_map:
        if only_manifest:
            valid = [v for v in unique if v in mc_order_map]
            if valid:
                valid.sort(key=lambda v: mc_order_map[v])
                return valid
        # Если остались версии вне манифеста — сортируем всё вместе по числовому ключу, не сваливая в конец
        in_manifest = [v for v in unique if v in mc_order_map]
        extra = [v for v in unique if v not in mc_order_map]
        if not extra:
            in_manifest.sort(key=lambda v: mc_order_map[v])
            return in_manifest
    return sorted(unique, key=_parse_ver_tuple, reverse=True)


def _image_bytes_to_data_uri(raw_bytes, max_bytes=700 * 1024):
    import base64
    if not raw_bytes or len(raw_bytes) > max_bytes:
        return ""
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw_bytes.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw_bytes.startswith(b"RIFF") and raw_bytes[8:12] == b"WEBP":
        mime = "image/webp"
    elif raw_bytes.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    else:
        return ""
    return f"data:{mime};base64,{base64.b64encode(raw_bytes).decode('ascii')}"


def _image_file_to_data_uri(file_path, max_bytes=30 * 1024 * 1024):
    """Преобразует файл изображения в base64 data URL для надёжного отображения в WebView."""
    import base64
    if not os.path.isfile(file_path):
        return ""
    try:
        sz = os.path.getsize(file_path)
        if sz <= 0 or sz > max_bytes:
            return ""
        ext = os.path.splitext(file_path)[1].lower()
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }
        mime = mime_map.get(ext)
        with open(file_path, "rb") as f:
            raw = f.read()
        if not mime:
            if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif raw.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
                mime = "image/webp"
            elif raw.startswith((b"GIF87a", b"GIF89a")):
                mime = "image/gif"
            else:
                mime = "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except Exception:
        return ""


def _read_zip_icon(zf, candidates):
    names = zf.namelist()
    lower_map = {n.lower(): n for n in names}
    for cand in candidates:
        if not cand:
            continue
        clean = str(cand).strip().lstrip("/").replace("\\", "/")
        actual = clean if clean in names else lower_map.get(clean.lower())
        if actual:
            try:
                uri = _image_bytes_to_data_uri(zf.read(actual))
                if uri:
                    return uri
            except Exception:
                pass
    for n in names:
        low = n.lower()
        if low.endswith((".png", ".jpg", ".jpeg", ".webp")):
            base = os.path.basename(low)
            if base in ("icon.png", "logo.png", "pack.png") and low.count("/") <= 2:
                try:
                    uri = _image_bytes_to_data_uri(zf.read(n))
                    if uri:
                        return uri
                except Exception:
                    pass
    return ""


def _pick_icon_from_field(icon_field):
    if isinstance(icon_field, str):
        return [icon_field]
    if isinstance(icon_field, dict):
        def _k(item):
            try:
                return int(str(item[0]).replace("x", "").split()[0])
            except Exception:
                return 0
        return [v for _, v in sorted(icon_field.items(), key=_k, reverse=True) if isinstance(v, str)]
    return []


def extract_jar_metadata(full_path):
    """Извлекает реальное название, версию и иконку мода прямо из .jar архива."""
    import zipfile
    meta = {"name": "", "version": "", "icon": ""}
    try:
        with zipfile.ZipFile(full_path, "r") as zf:
            names = set(zf.namelist())
            icon_candidates = []

            if "fabric.mod.json" in names:
                raw = zf.read("fabric.mod.json").decode("utf-8", errors="ignore")
                data = json.loads(raw, strict=False)
                meta["name"] = str(data.get("name") or "").strip()
                meta["version"] = str(data.get("version") or "").strip()
                icon_candidates.extend(_pick_icon_from_field(data.get("icon")))

            elif "quilt.mod.json" in names:
                raw = zf.read("quilt.mod.json").decode("utf-8", errors="ignore")
                data = json.loads(raw, strict=False)
                ql = data.get("quilt_loader") or {}
                qmd = ql.get("metadata") or {}
                meta["name"] = str(qmd.get("name") or "").strip()
                meta["version"] = str(ql.get("version") or "").strip()
                icon_candidates.extend(_pick_icon_from_field(qmd.get("icon")))

            elif "META-INF/neoforge.mods.toml" in names or "META-INF/mods.toml" in names:
                toml_name = "META-INF/neoforge.mods.toml" if "META-INF/neoforge.mods.toml" in names else "META-INF/mods.toml"
                raw = zf.read(toml_name).decode("utf-8", errors="ignore")
                m_name = re.search(r'^\s*displayName\s*=\s*["\']([^"\']+)["\']', raw, re.MULTILINE)
                m_logo = re.search(r'^\s*logoFile\s*=\s*["\']([^"\']+)["\']', raw, re.MULTILINE)
                m_ver = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', raw, re.MULTILINE)
                if m_name:
                    meta["name"] = m_name.group(1).strip()
                if m_logo:
                    icon_candidates.append(m_logo.group(1).strip())
                if m_ver and "${" not in m_ver.group(1):
                    meta["version"] = m_ver.group(1).strip()

            elif "mcmod.info" in names:
                raw = zf.read("mcmod.info").decode("utf-8", errors="ignore")
                data = json.loads(raw, strict=False)
                entry = None
                if isinstance(data, list) and data:
                    entry = data[0]
                elif isinstance(data, dict):
                    ml = data.get("modList")
                    if isinstance(ml, list) and ml:
                        entry = ml[0]
                if isinstance(entry, dict):
                    meta["name"] = str(entry.get("name") or "").strip()
                    meta["version"] = str(entry.get("version") or "").strip()
                    if entry.get("logoFile"):
                        icon_candidates.append(entry["logoFile"])

            meta["icon"] = _read_zip_icon(zf, icon_candidates)
    except Exception:
        pass
    return meta


def extract_pack_metadata(full_path):
    """Извлекает pack.png и описание из .zip ресурспака / шейдера / датапака."""
    import zipfile
    meta = {"name": "", "description": "", "icon": ""}
    try:
        if os.path.isdir(full_path):
            png = os.path.join(full_path, "pack.png")
            if os.path.exists(png):
                with open(png, "rb") as f:
                    meta["icon"] = _image_bytes_to_data_uri(f.read())
            return meta

        with zipfile.ZipFile(full_path, "r") as zf:
            names = zf.namelist()
            meta["icon"] = _read_zip_icon(zf, ["pack.png", "logo.png", "icon.png"])
            mcmeta = next((n for n in names if n.lower() == "pack.mcmeta" or n.lower().endswith("/pack.mcmeta")), None)
            if mcmeta:
                raw = zf.read(mcmeta).decode("utf-8", errors="ignore")
                data = json.loads(raw, strict=False)
                desc = (data.get("pack") or {}).get("description", "")
                if isinstance(desc, dict):
                    desc = desc.get("text", "")
                elif isinstance(desc, list):
                    desc = " ".join(str(x.get("text", x) if isinstance(x, dict) else x) for x in desc)
                desc = re.sub(r"§.", "", str(desc)).replace("\n", " ").strip()
                meta["description"] = desc[:120]
    except Exception:
        pass
    return meta


class DiscordRPCManager:
    """Управление Discord Rich Presence в отдельном daemon-потоке через очередь команд."""

    def __init__(self, client_id, large_image, enabled=True):
        self.client_id = str(client_id or "").strip()
        self.large_image = str(large_image or "").strip()
        self.enabled = bool(enabled)
        self._queue = queue.Queue()
        self._current_activity = None
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="DiscordRPCWorker")
        self._thread.start()

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        try:
            self._queue.put(("SET_ENABLED", self.enabled))
        except Exception:
            pass

    def set_launcher_menu(self):
        act = {
            "details": "В меню лаунчера",
            "state": "GreenPrism",
        }
        if self.large_image:
            act["large_image"] = self.large_image
            act["large_text"] = "GreenPrism Launcher"
        self._current_activity = act
        try:
            self._queue.put(("SET_ACTIVITY", act))
        except Exception:
            pass

    def set_playing(self, inst_name, loader, version):
        start_ts = int(time.time())
        loader_str = str(loader or "").strip()
        ver_str = str(version or "").strip()
        state_parts = [p for p in (loader_str, ver_str) if p]
        state_str = " ".join(state_parts) if state_parts else "GreenPrism"
        act = {
            "details": f"Играет: {inst_name or 'Сборка'}",
            "state": state_str,
            "start": start_ts,
        }
        if self.large_image:
            act["large_image"] = self.large_image
            act["large_text"] = "GreenPrism Launcher"
        self._current_activity = act
        try:
            self._queue.put(("SET_ACTIVITY", act))
        except Exception:
            pass

    def clear(self):
        self._current_activity = None
        try:
            self._queue.put(("CLEAR",))
        except Exception:
            pass

    def close(self):
        try:
            self._stop_event.set()
            self._queue.put(("CLOSE",))
        except Exception:
            pass

    def _worker_loop(self):
        if not HAVE_PYPRESENCE or Presence is None:
            return

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        except Exception:
            pass

        rpc = None
        connected = False
        last_connect_time = 0.0
        connect_cooldown = 10.0

        def _is_placeholder(cid):
            return not cid or not cid.isdigit() or len(cid) < 10

        def _try_connect():
            nonlocal rpc, connected, last_connect_time
            if connected and rpc is not None:
                return True
            now = time.time()
            if (now - last_connect_time) < connect_cooldown:
                return False
            last_connect_time = now

            if _is_placeholder(self.client_id):
                return False

            try:
                if rpc is not None:
                    try:
                        rpc.close()
                    except Exception:
                        pass
                rpc = Presence(self.client_id)
                rpc.connect()
                connected = True
                return True
            except Exception:
                connected = False
                rpc = None
                return False

        current_act = None

        while not self._stop_event.is_set():
            try:
                cmd = self._queue.get(timeout=2.0)
            except queue.Empty:
                if self.enabled and current_act and not connected:
                    if _try_connect():
                        try:
                            clean_act = {k: v for k, v in current_act.items() if v is not None}
                            rpc.update(**clean_act)
                        except Exception:
                            connected = False
                continue

            try:
                action = cmd[0]
                if action == "CLOSE":
                    break
                elif action == "SET_ENABLED":
                    val = cmd[1]
                    if not val:
                        if connected and rpc is not None:
                            try:
                                rpc.clear()
                            except Exception:
                                pass
                    else:
                        if current_act:
                            if _try_connect():
                                try:
                                    clean_act = {k: v for k, v in current_act.items() if v is not None}
                                    rpc.update(**clean_act)
                                except Exception:
                                    connected = False
                elif action == "CLEAR":
                    current_act = None
                    if connected and rpc is not None:
                        try:
                            rpc.clear()
                        except Exception:
                            pass
                elif action == "SET_ACTIVITY":
                    act = cmd[1]
                    current_act = act
                    if self.enabled:
                        if _try_connect():
                            try:
                                clean_act = {k: v for k, v in act.items() if v is not None}
                                rpc.update(**clean_act)
                            except Exception:
                                connected = False
            except Exception:
                pass
            finally:
                self._queue.task_done()

        if rpc is not None:
            try:
                rpc.clear()
            except Exception:
                pass
            try:
                rpc.close()
            except Exception:
                pass
            rpc = None
            connected = False


class LauncherAPI:
    def __init__(self):
        os.makedirs(BASE_DIR, exist_ok=True)
        os.makedirs(INSTANCES_DIR, exist_ok=True)
        self._window = None
        self._window_started = False
        self._closing = False
        self._is_max = False
        self._is_busy = False
        self._running_inst_id = None
        self._running_proc = None
        self._user_stopped = False
        self._last_crash_report = None
        self._last_crash_full_log = ""
        self._max_progress = 100
        self._last_status = "Готов к запуску"
        self._config_lock = threading.Lock()
        self._meta_cache = None
        self._meta_lock = threading.Lock()
        self._meta_io_lock = threading.Lock()
        self._meta_save_timer = None
        self._cover_cache = {}
        self._icon_cache = {}
        self._skin_cache = {}
        self._mrpack_contents_cache = {}
        self._loader_versions_cache = {}
        self._installed_cache = (None, None, set())
        self._versions_cache_ts = 0
        self._logs = {}
        self._config = self._load_config()
        self._hwnd = None
        self._wndproc_ref = None
        self._old_wndproc = None
        self._wndproc_hwnd = None
        os.makedirs(self._get_mc_dir(), exist_ok=True)
        self._discord_rpc = DiscordRPCManager(
            client_id=DISCORD_CLIENT_ID,
            large_image=DISCORD_LARGE_IMAGE,
            enabled=bool(self._config.get("discord_rpc", True)),
        )
        self._discord_rpc.set_launcher_menu()

        self._versions = {
            "Vanilla": [
                {"id": "26.3", "type": "release"},
                {"id": "26.2", "type": "release"},
                {"id": "26.1.2", "type": "release"},
                {"id": "1.21.4", "type": "release"},
                {"id": "1.21.1", "type": "release"},
                {"id": "1.20.4", "type": "release"},
                {"id": "1.20.1", "type": "release"},
                {"id": "1.19.4", "type": "release"},
                {"id": "1.18.2", "type": "release"},
                {"id": "1.16.5", "type": "release"},
                {"id": "1.12.2", "type": "release"},
                {"id": "1.8.9", "type": "release"},
                {"id": "1.7.10", "type": "release"},
                {"id": "1.5.2", "type": "release"},
                {"id": "1.0", "type": "release"},
                {"id": "b1.7.3", "type": "old_beta"},
                {"id": "a1.2.6", "type": "old_alpha"},
            ],
            "Fabric": ["26.3", "26.2", "26.1.2", "26.1.1", "26.1", "1.21.4", "1.21.1", "1.20.4", "1.20.1", "1.19.4", "1.18.2", "1.16.5", "1.14.4"],
            "Quilt": ["26.3", "26.2", "26.1.2", "26.1.1", "26.1", "1.21.4", "1.21.1", "1.20.4", "1.20.1", "1.19.4", "1.18.2", "1.16.5", "1.14.4"],
            "Forge": ["26.3", "26.2", "26.1.2", "26.1.1", "26.1", "1.21.4", "1.21.1", "1.20.4", "1.20.1", "1.19.2", "1.18.2", "1.16.5", "1.12.2", "1.7.10"],
            "NeoForge": ["26.3", "26.2", "26.1.2", "26.1.1", "26.1", "1.21.4", "1.21.1", "1.21", "1.20.6", "1.20.4"],
        }
        self._neoforge_builds = []
        self._load_versions_cache()

    # ---------------- Нативное frameless-окно Windows 11 (DWM + WM_NCCALCSIZE) ----------------
    def _init_win32_bindings(self):
        """Инициализирует Win32/DWM ctypes-функции с корректными 64-битными argtypes/restype."""
        if getattr(self, "_win32_api", None) is not None:
            return self._win32_api
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        LONG_PTR = ctypes.c_ssize_t
        UINT_PTR = ctypes.c_size_t

        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.IsZoomed.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.ReleaseCapture.argtypes = []
        user32.ReleaseCapture.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, UINT_PTR, LONG_PTR]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, UINT_PTR, LONG_PTR]
        user32.SendMessageW.restype = LONG_PTR
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HMONITOR
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int

        get_wl = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_wl = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        get_wl.argtypes = [wintypes.HWND, ctypes.c_int]
        get_wl.restype = LONG_PTR
        set_wl.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
        set_wl.restype = LONG_PTR

        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, UINT_PTR, LONG_PTR]
        user32.CallWindowProcW.restype = LONG_PTR

        dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        dwmapi.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.c_void_p]
        dwmapi.DwmExtendFrameIntoClientArea.restype = ctypes.c_long

        shell32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.c_void_p]
        shell32.SHAppBarMessage.restype = UINT_PTR

        self._win32_api = {
            "user32": user32,
            "dwmapi": dwmapi,
            "shell32": shell32,
            "get_wl": get_wl,
            "set_wl": set_wl,
        }
        return self._win32_api

    def _get_hwnd_win32(self):
        if sys.platform != "win32":
            return None
        w32 = self._init_win32_bindings()
        user32 = w32["user32"]
        if getattr(self, "_hwnd", None) and user32.IsWindow(self._hwnd):
            return self._hwnd
        self._hwnd = None
        try:
            native = getattr(self._window, "native", None)
            if native is not None and hasattr(native, "Handle"):
                h = int(native.Handle.ToInt64())
                if h and user32.IsWindow(h):
                    self._hwnd = h
                    return h
        except Exception:
            pass
        try:
            import ctypes
            from ctypes import wintypes

            pid = os.getpid()
            found = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _cb(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                proc_id = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
                if proc_id.value == pid:
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if "GreenPrism" in buf.value:
                            found.append(hwnd)
                            return False
                return True

            user32.EnumWindows(_cb, 0)
            if found:
                self._hwnd = found[0]
                return found[0]
        except Exception:
            pass
        return None

    def _sync_max_state_async(self, is_max):
        """Асинхронно уведомляет фронтенд об изменении состояния Развёрнуто/Обычное (без дедлока WndProc)."""
        val_js = "true" if is_max else "false"
        threading.Thread(
            target=self._emit_js,
            args=(f"window.onWindowStateChanged && window.onWindowStateChanged({val_js})",),
            daemon=True,
        ).start()

    def _set_win11_corners(self, hwnd, rounded=True):
        if sys.platform != "win32" or not hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes
            w32 = self._init_win32_bindings()
            pref = wintypes.DWORD(2 if rounded else 1)  # DWMWCP_ROUND=2, DWMWCP_DONOTROUND=1
            w32["dwmapi"].DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception:
            pass

    def win_minimize(self):
        if not self._window:
            return False
        if sys.platform == "win32":
            hwnd = self._get_hwnd_win32()
            if hwnd:
                try:
                    w32 = self._init_win32_bindings()
                    WM_SYSCOMMAND = 0x0112
                    SC_MINIMIZE = 0xF020
                    w32["user32"].PostMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
                    return True
                except Exception:
                    pass
        try:
            self._window.minimize()
        except Exception:
            pass
        return True

    def win_toggle_max(self):
        if not self._window:
            return False
        if sys.platform == "win32":
            hwnd = self._get_hwnd_win32()
            if hwnd:
                try:
                    w32 = self._init_win32_bindings()
                    WM_SYSCOMMAND = 0x0112
                    SC_MAXIMIZE = 0xF030
                    SC_RESTORE = 0xF120
                    is_zoomed = bool(w32["user32"].IsZoomed(hwnd))
                    cmd = SC_RESTORE if is_zoomed else SC_MAXIMIZE
                    w32["user32"].PostMessageW(hwnd, WM_SYSCOMMAND, cmd, 0)
                    self._is_max = not is_zoomed
                    return self._is_max
                except Exception:
                    pass
        try:
            if self._is_max:
                self._window.restore()
                self._is_max = False
            else:
                self._window.maximize()
                self._is_max = True
        except Exception:
            pass
        return self._is_max

    def _run_on_ui_thread(self, fn):
        """Выполняет Win32-вызов в UI-потоке WinForms (BeginInvoke), иначе напрямую."""
        try:
            native = getattr(self._window, "native", None)
            if native is not None and hasattr(native, "BeginInvoke"):
                from System import Action
                native.BeginInvoke(Action(fn))
                return True
        except Exception:
            pass
        fn()
        return True

    def start_drag(self):
        """Нативное перетаскивание окна за шапку (ReleaseCapture + SendMessageW WM_NCLBUTTONDOWN HTCAPTION)."""
        if sys.platform != "win32":
            return False
        hwnd = self._get_hwnd_win32()
        if not hwnd:
            return False
        try:
            w32 = self._init_win32_bindings()
            WM_NCLBUTTONDOWN = 0x00A1
            HTCAPTION = 2

            def _do_drag():
                try:
                    w32["user32"].ReleaseCapture()
                    w32["user32"].SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0)
                except Exception:
                    pass

            return self._run_on_ui_thread(_do_drag)
        except Exception:
            return False

    def win_start_drag(self):
        return self.start_drag()

    def win_start_resize(self, edge):
        """Нативное изменение размера окна за края и углы через WM_SYSCOMMAND (SC_SIZE)."""
        if sys.platform != "win32":
            return False
        edge_map = {
            "w": 1,   # WMSZ_LEFT
            "e": 2,   # WMSZ_RIGHT
            "n": 3,   # WMSZ_TOP
            "nw": 4,  # WMSZ_TOPLEFT
            "ne": 5,  # WMSZ_TOPRIGHT
            "s": 6,   # WMSZ_BOTTOM
            "sw": 7,  # WMSZ_BOTTOMLEFT
            "se": 8,  # WMSZ_BOTTOMRIGHT
        }
        code = edge_map.get(edge)
        if not code:
            return False
        hwnd = self._get_hwnd_win32()
        if not hwnd:
            return False
        try:
            w32 = self._init_win32_bindings()
            if w32["user32"].IsZoomed(hwnd):
                return False
            WM_SYSCOMMAND = 0x0112
            SC_SIZE = 0xF000

            def _do_resize():
                try:
                    w32["user32"].ReleaseCapture()
                    w32["user32"].SendMessageW(hwnd, WM_SYSCOMMAND, SC_SIZE + code, 0)
                except Exception:
                    pass

            return self._run_on_ui_thread(_do_resize)
        except Exception:
            return False

    def win_close(self):
        self._closing = True
        try:
            self._flush_meta_cache_now()
        except Exception:
            pass
        try:
            if hasattr(self, "_discord_rpc"):
                self._discord_rpc.close()
        except Exception:
            pass
        if self._window:
            try:
                self._window.destroy()
            except Exception:
                os._exit(0)
        return True

    def _get_mc_dir(self):
        custom = (self._config.get("mc_dir") or "").strip()
        return custom if custom else DEFAULT_MC_DIR

    def _get_cf_api_key(self):
        user_key = (self._config.get("cf_api_key") or "").strip()
        return user_key if user_key else EMBEDDED_CF_API_KEY

    def _load_versions_cache(self):
        if not os.path.exists(VERSIONS_CACHE_PATH):
            return
        try:
            with open(VERSIONS_CACHE_PATH, "r", encoding="utf-8") as f:
                cached = json.load(f)
            vers = cached.get("versions")
            if isinstance(vers, dict) and vers.get("Vanilla") and vers.get("Fabric") and vers.get("Forge"):
                if not vers.get("NeoForge"):
                    vers["NeoForge"] = list(self._versions.get("NeoForge") or ["1.21.4", "1.21.1", "1.21", "1.20.6", "1.20.4"])
                if not vers.get("Quilt"):
                    vers["Quilt"] = list(self._versions.get("Quilt") or ["1.21.4", "1.21.1", "1.20.4", "1.20.1", "1.19.4", "1.18.2", "1.16.5"])
                self._versions = vers
                self._versions_cache_ts = float(cached.get("ts", 0))
        except Exception:
            pass

    def _save_versions_cache(self):
        tmp_path = f"{VERSIONS_CACHE_PATH}.{threading.get_ident()}.tmp"
        try:
            os.makedirs(BASE_DIR, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "versions": self._versions}, f, ensure_ascii=False)
                f.flush()
            os.replace(tmp_path, VERSIONS_CACHE_PATH)
        except Exception:
            pass
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def refresh_versions_now(self):
        """Принудительное обновление списка версий Mojang/Fabric/Forge из настроек."""
        threading.Thread(target=self._fetch_versions_async, daemon=True).start()
        return True

    def _background_warmup(self):
        """Отложенный прогрев сети после отрисовки окна — не тормозит старт."""
        time.sleep(0.55)
        try:
            _get_http()
        except Exception:
            pass
        if time.time() - self._versions_cache_ts > VERSIONS_CACHE_TTL:
            time.sleep(0.45)
            self._fetch_versions_async()

    def _apply_win11_dwm(self):
        """Настраивает нативное frameless-окно Windows 11: стили DWM, субклассинг WM_NCCALCSIZE, тень и скругление."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            # Ждём, пока HWND окна станет доступен после показа
            hwnd = None
            for _ in range(30):
                hwnd = self._get_hwnd_win32()
                if hwnd:
                    break
                time.sleep(0.05)
            if not hwnd:
                return

            w32 = self._init_win32_bindings()
            user32 = w32["user32"]
            dwmapi = w32["dwmapi"]
            get_wl = w32["get_wl"]
            set_wl = w32["set_wl"]

            def _apply_on_ui():
                # 1. Явно включаем нативные DWM-анимации сворачивания в панель задач и разворачивания
                #    (DWMWA_TRANSITIONS_FORCEDISABLED = 3, значение 0 = анимации включены)
                disable_trans = wintypes.DWORD(0)
                dwmapi.DwmSetWindowAttribute(hwnd, 3, ctypes.byref(disable_trans), ctypes.sizeof(disable_trans))

                # 2. Тёмный режим DWM (DWMWA_USE_IMMERSIVE_DARK_MODE = 20)
                dark = wintypes.DWORD(1)
                dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))

                # 3. Скруглённые углы Windows 11 (DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2)
                self._set_win11_corners(hwnd, rounded=not bool(user32.IsZoomed(hwnd)))

                # 4. Аккуратная тёмная 1px обводка окна Windows 11 (DWMWA_BORDER_COLOR = 34)
                border_color = wintypes.DWORD(0x00322D28)
                dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border_color), ctypes.sizeof(border_color))

                if not bool(self._config.get("frameless", True)):
                    return

                # 5. Снимаем WS_POPUP (0x80000000), который WinForms ставит для FormBorderStyle.None
                #    и который ОТКЛЮЧАЕТ плавные DWM-анимации сворачивания в панель задач / разворачивания!
                #    Возвращаем окну полноценные стили WS_OVERLAPPEDWINDOW + WS_EX_APPWINDOW.
                GWL_STYLE = -16
                GWL_EXSTYLE = -20
                GWLP_WNDPROC = -4
                WS_CAPTION = 0x00C00000
                WS_THICKFRAME = 0x00040000
                WS_MINIMIZEBOX = 0x00020000
                WS_MAXIMIZEBOX = 0x00010000
                WS_SYSMENU = 0x00080000
                WS_EX_APPWINDOW = 0x00040000
                WS_EX_TOOLWINDOW = 0x00000080

                cur_style = get_wl(hwnd, GWL_STYLE)
                new_style = (cur_style & 0x7FFFFFFF) | WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU
                if new_style != cur_style:
                    set_wl(hwnd, GWL_STYLE, new_style)

                cur_ex = get_wl(hwnd, GWL_EXSTYLE)
                new_ex = (cur_ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
                if new_ex != cur_ex:
                    set_wl(hwnd, GWL_EXSTYLE, new_ex)

                # 6. Субклассим WndProc: WM_NCCALCSIZE + WM_GETMINMAXINFO строго по rcWork монитора и синхронизация WM_SIZE
                if self._wndproc_hwnd != hwnd:
                    WM_GETMINMAXINFO = 0x0024
                    WM_NCDESTROY = 0x0082
                    WM_NCCALCSIZE = 0x0083
                    WM_SIZE = 0x0005
                    WM_EXITSIZEMOVE = 0x0232
                    SIZE_RESTORED = 0
                    SIZE_MAXIMIZED = 2
                    MONITOR_DEFAULTTONEAREST = 2
                    ABM_GETSTATE = 0x00000004
                    ABS_AUTOHIDE = 0x00000001

                    class POINT(ctypes.Structure):
                        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

                    class RECT(ctypes.Structure):
                        _fields_ = [
                            ("left", wintypes.LONG),
                            ("top", wintypes.LONG),
                            ("right", wintypes.LONG),
                            ("bottom", wintypes.LONG),
                        ]

                    class MONITORINFO(ctypes.Structure):
                        _fields_ = [
                            ("cbSize", wintypes.DWORD),
                            ("rcMonitor", RECT),
                            ("rcWork", RECT),
                            ("dwFlags", wintypes.DWORD),
                        ]

                    class APPBARDATA(ctypes.Structure):
                        _fields_ = [
                            ("cbSize", wintypes.DWORD),
                            ("hWnd", wintypes.HWND),
                            ("uCallbackMessage", wintypes.UINT),
                            ("uEdge", wintypes.UINT),
                            ("rc", RECT),
                            ("lParam", ctypes.c_ssize_t),
                        ]

                    class MINMAXINFO(ctypes.Structure):
                        _fields_ = [
                            ("ptReserved", POINT),
                            ("ptMaxSize", POINT),
                            ("ptMaxPosition", POINT),
                            ("ptMinTrackSize", POINT),
                            ("ptMaxTrackSize", POINT),
                        ]

                    class NCCALCSIZE_PARAMS(ctypes.Structure):
                        _fields_ = [("rgrc", RECT * 3), ("lppos", ctypes.c_void_p)]

                    WNDPROC = ctypes.WINFUNCTYPE(
                        ctypes.c_ssize_t,
                        wintypes.HWND,
                        wintypes.UINT,
                        ctypes.c_size_t,
                        ctypes.c_ssize_t,
                    )

                    shell32 = w32["shell32"]

                    def _get_monitor_work_area(h):
                        hmon = user32.MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST)
                        if not hmon:
                            return None
                        mi = MONITORINFO()
                        mi.cbSize = ctypes.sizeof(MONITORINFO)
                        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                            return None
                        wl, wt, wr, wb = mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom
                        ml, mt, mr, mb = mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom
                        # При автоскрытии панели задач оставляем 1px снизу, чтобы панель задач могла всплывать
                        if wl == ml and wt == mt and wr == mr and wb == mb:
                            try:
                                abd = APPBARDATA()
                                abd.cbSize = ctypes.sizeof(APPBARDATA)
                                if shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(abd)) & ABS_AUTOHIDE:
                                    wb -= 1
                            except Exception:
                                pass
                        return (wl, wt, wr, wb, ml, mt)

                    def _custom_wndproc(h, msg, wparam, lparam):
                        try:
                            if msg == WM_GETMINMAXINFO and lparam:
                                old_p = self._old_wndproc
                                if old_p:
                                    user32.CallWindowProcW(old_p, h, msg, wparam, lparam)
                                area = _get_monitor_work_area(h)
                                if area:
                                    wl, wt, wr, wb, ml, mt = area
                                    mmi = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
                                    mmi.ptMaxPosition.x = wl - ml
                                    mmi.ptMaxPosition.y = wt - mt
                                    mmi.ptMaxSize.x = wr - wl
                                    mmi.ptMaxSize.y = wb - wt
                                return 0

                            if msg == WM_NCCALCSIZE and wparam == 1 and lparam:
                                if user32.IsZoomed(h):
                                    area = _get_monitor_work_area(h)
                                    if area:
                                        wl, wt, wr, wb, _ml, _mt = area
                                        params = ctypes.cast(lparam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents
                                        params.rgrc[0].left = wl
                                        params.rgrc[0].top = wt
                                        params.rgrc[0].right = wr
                                        params.rgrc[0].bottom = wb
                                return 0

                            if msg == WM_SIZE:
                                if wparam == SIZE_MAXIMIZED and not self._is_max:
                                    self._is_max = True
                                    self._set_win11_corners(h, rounded=False)
                                    self._sync_max_state_async(True)
                                elif wparam == SIZE_RESTORED and self._is_max:
                                    self._is_max = False
                                    self._set_win11_corners(h, rounded=True)
                                    self._sync_max_state_async(False)

                            elif msg == WM_EXITSIZEMOVE:
                                threading.Thread(
                                    target=self._emit_js,
                                    args=("window.onResizeEnd && window.onResizeEnd()",),
                                    daemon=True,
                                ).start()

                            elif msg == WM_NCDESTROY:
                                old_p = self._old_wndproc
                                if old_p:
                                    set_wl(h, GWLP_WNDPROC, old_p)
                                    self._old_wndproc = None
                                    self._wndproc_hwnd = None
                                return user32.CallWindowProcW(old_p, h, msg, wparam, lparam) if old_p else 0
                        except Exception:
                            pass

                        old_p = self._old_wndproc
                        return user32.CallWindowProcW(old_p, h, msg, wparam, lparam) if old_p else 0

                    cb = WNDPROC(_custom_wndproc)
                    cb_addr = ctypes.cast(cb, ctypes.c_void_p).value
                    old_proc = set_wl(hwnd, GWLP_WNDPROC, cb_addr)
                    if old_proc:
                        self._wndproc_ref = cb
                        self._old_wndproc = old_proc
                        self._wndproc_hwnd = hwnd

                # 7. Минимальный DWM-отступ для нативной тени вокруг безрамочного окна
                class MARGINS(ctypes.Structure):
                    _fields_ = [
                        ("cxLeftWidth", ctypes.c_int),
                        ("cxRightWidth", ctypes.c_int),
                        ("cyTopHeight", ctypes.c_int),
                        ("cyBottomHeight", ctypes.c_int),
                    ]

                margins = MARGINS(0, 0, 1, 0)
                dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))

                # 8. Применяем изменения фрейма (вызывает WM_NCCALCSIZE и обновляет DWM-состояние)
                SWP_NOSIZE = 0x0001
                SWP_NOMOVE = 0x0002
                SWP_NOZORDER = 0x0004
                SWP_NOACTIVATE = 0x0010
                SWP_FRAMECHANGED = 0x0020
                user32.SetWindowPos(
                    hwnd, 0, 0, 0, 0, 0,
                    SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
                )

            self._run_on_ui_thread(_apply_on_ui)
        except Exception:
            pass

    def _attach_window(self, window):
        self._window = window
        threading.Thread(target=self._apply_win11_dwm, daemon=True).start()
        threading.Thread(target=self._background_warmup, daemon=True).start()

        def _on_window_loaded():
            self._window_started = True
            self._setup_dnd()

        try:
            window.events.loaded += _on_window_loaded
        except Exception:
            pass

    def _emit_js(self, call_str):
        if self._window and not self._closing:
            try:
                self._window.evaluate_js(call_str)
            except Exception:
                pass

    def _set_status(self, text, progress=None, active=None):
        self._last_status = text
        p_js = "null" if progress is None else str(round(max(0.0, min(1.0, progress)) * 100, 1))
        a_js = "null" if active is None else ("true" if active else "false")
        self._emit_js(f"window.onStatusUpdate && window.onStatusUpdate({json.dumps(text)}, {p_js}, {a_js})")

    @staticmethod
    def _format_mclib_status(raw_text):
        """Преобразует сырые английские статусы minecraft_launcher_lib в аккуратные русские подписи."""
        s = str(raw_text or "").strip()
        if not s:
            return "Подготовка файлов игры"
        # Если строка уже на русском (и без сырого префикса Download), оставляем как есть
        if re.search(r"[А-Яа-яЁё]", s) and not re.match(r"^\s*download\b", s, re.I):
            return s

        low = s.lower()
        # 1. Ассеты (40-символьный SHA-1 хеш или упоминание asset)
        if re.search(r"\b[0-9a-f]{40}\b", low) or "asset" in low:
            return "Загрузка ресурсов игры"
        # 2. Библиотеки / .jar / maven / natives
        if any(k in low for k in ("librar", ".jar", "maven", "natives")):
            return "Загрузка библиотек"
        # 3. Java / JVM runtime
        if any(k in low for k in ("runtime", "java", "jre", "jdk", "jvm")):
            return "Установка Java"
        # 4. Проверка/установка версии и прочие этапы подготовки
        if any(k in low for k in ("version", "install", "check", "client", "extract", "unpack", "logging", "forge", "fabric", "quilt", "complete", "finish")):
            return "Подготовка файлов игры"
        # 5. Любой оставшийся "Download ..."
        if low.startswith("download"):
            return "Загрузка файлов игры"
        return "Подготовка файлов игры"

    def _make_install_callback(self):
        """Создаёт колбэк прогресса для minecraft_launcher_lib с русским форматированием и счётчиком."""
        state = {"label": "Подготовка файлов игры", "max": 1, "cur": 0, "last_emit": 0.0}

        def _render_label():
            lbl = state["label"]
            mx = state["max"]
            cur = state["cur"]
            if mx > 1 and cur > 0:
                return f"{lbl}: {min(cur, mx)} / {mx}"
            return lbl

        def _set_status_cb(text):
            new_lbl = self._format_mclib_status(text)
            changed = (new_lbl != state["label"])
            state["label"] = new_lbl
            now = time.perf_counter()
            if changed or (now - state["last_emit"] >= 0.08):
                state["last_emit"] = now
                prog = (state["cur"] / state["max"]) if state["max"] > 1 else None
                self._set_status(_render_label(), prog, True)

        def _set_max_cb(val):
            try:
                mx = max(int(val), 1)
            except Exception:
                mx = 1
            self._max_progress = mx
            state["max"] = mx
            state["cur"] = 0

        def _set_progress_cb(val):
            try:
                cur = max(int(val), 0)
            except Exception:
                cur = 0
            state["cur"] = cur
            mx = state["max"]
            now = time.perf_counter()
            if cur >= mx or cur <= 1 or (now - state["last_emit"] >= 0.08):
                state["last_emit"] = now
                self._set_status(_render_label(), cur / mx, True)

        return {
            "setStatus": _set_status_cb,
            "setMax": _set_max_cb,
            "setProgress": _set_progress_cb,
        }

    def _push_log(self, inst_id, line):
        buf = self._logs.setdefault(inst_id, [])
        buf.append(line)
        if len(buf) > 500:
            del buf[: len(buf) - 500]
        self._emit_js(f"window.onLogLine && window.onLogLine({json.dumps(inst_id)}, {json.dumps(line)})")

    # ---------------- Конфиг ----------------
    def _load_config(self):
        default = {
            "username": "Player",
            "profiles": ["Player"],
            "ram_gb": 4,
            "ram_mb": 4096,
            "java_path": "",
            "cf_api_key": "",
            "mc_dir": DEFAULT_MC_DIR,
            "theme": "emerald",
            "frameless": True,
            "ambient_glow": True,
            "auto_install_deps": True,
            "hide_on_launch": False,
            "discord_rpc": True,
            "sidebar_collapsed": False,
            "custom_bg_file": "",
            "custom_bg_dim": 55,
            "custom_bg_blur": 4,
            "instances": [],
        }
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    default.update(loaded)
                else:
                    raise ValueError("Config root is not a JSON object")
            except Exception:
                try:
                    import shutil
                    bak_path = f"{CONFIG_PATH}.corrupt.{int(time.time())}.bak"
                    shutil.copy2(CONFIG_PATH, bak_path)
                except Exception:
                    pass
        else:
            legacy_mc = os.path.join(os.path.expanduser("~"), ".modrinth_prism_launcher", "minecraft")
            if os.path.isdir(os.path.join(legacy_mc, "versions")) and os.listdir(os.path.join(legacy_mc, "versions")):
                default["mc_dir"] = legacy_mc

        if default.get("theme") in ("nord", "gold", "nord_frost", "royal_gold"):
            default["theme"] = "emerald"

        loader_defaults = {
            "Fabric": ("bolt", "#1bd96a"),
            "Quilt": ("diamond", "#8b5cf6"),
            "Forge": ("anvil", "#f16436"),
            "NeoForge": ("flame", "#f97316"),
            "Vanilla": ("grass", "#22d3ee"),
        }
        for inst in default.get("instances", []):
            if inst.get("loader") == "Quilt" and "0.20." in str(inst.get("installed_version_id") or ""):
                inst["installed_version_id"] = ""
            d_icon, d_color = loader_defaults.get(inst.get("loader", "Vanilla"), ("grass", "#1bd96a"))
            inst.setdefault("icon", d_icon)
            inst.setdefault("color", d_color)
            inst.setdefault("playtime_sec", 0)
            inst.setdefault("last_played", "")
            inst.setdefault("cover_file", "")
            inst.setdefault("custom_icon_file", "")
            inst.setdefault("installed_projects", {})
            inst.setdefault("server_address", "")
            inst.setdefault("ram_mb", 0)
            inst.setdefault("java_path", "")
            inst.setdefault("jvm_args", "")
            inst.setdefault("win_width", 0)
            inst.setdefault("win_height", 0)

        cur_user = (default.get("username") or "Player").strip() or "Player"
        profs = [str(p).strip() for p in (default.get("profiles") or []) if str(p).strip()]
        if cur_user not in profs:
            profs.insert(0, cur_user)
        default["profiles"] = profs

        # Нормализация и миграция списка аккаунтов (Оффлайн + Ely.by)
        raw_accounts = default.get("accounts")
        accounts = []
        seen_ids = set()
        if isinstance(raw_accounts, list):
            for a in raw_accounts:
                if not isinstance(a, dict):
                    continue
                atype = "elyby" if a.get("type") == "elyby" else "offline"
                uname = str(a.get("username") or "").strip()
                if not uname:
                    continue
                if atype == "elyby":
                    uid = _format_mc_uuid(a.get("uuid") or "")
                    aid = str(a.get("id") or f"elyby:{uid or uname.lower()}")
                    if aid in seen_ids:
                        continue
                    seen_ids.add(aid)
                    accounts.append({
                        "id": aid,
                        "type": "elyby",
                        "username": uname,
                        "uuid": uid,
                        "access_token": str(a.get("access_token") or ""),
                        "client_token": str(a.get("client_token") or ""),
                    })
                else:
                    aid = f"offline:{uname.lower()}"
                    if aid in seen_ids:
                        continue
                    seen_ids.add(aid)
                    accounts.append({
                        "id": aid,
                        "type": "offline",
                        "username": uname,
                        "uuid": "",
                    })
        for p in profs:
            aid = f"offline:{p.lower()}"
            if aid not in seen_ids and not any(x["username"].lower() == p.lower() for x in accounts):
                seen_ids.add(aid)
                accounts.append({
                    "id": aid,
                    "type": "offline",
                    "username": p,
                    "uuid": "",
                })
        if not accounts:
            accounts = [{"id": "offline:player", "type": "offline", "username": "Player", "uuid": ""}]

        active_id = str(default.get("active_account_id") or "")
        if not any(x["id"] == active_id for x in accounts):
            match = next((x for x in accounts if x["username"].lower() == cur_user.lower()), accounts[0])
            active_id = match["id"]

        active_acc = next((x for x in accounts if x["id"] == active_id), accounts[0])
        default["accounts"] = accounts
        default["active_account_id"] = active_acc["id"]
        default["username"] = active_acc["username"]
        default["profiles"] = [x["username"] for x in accounts]

        # Миграция и нормализация RAM в мегабайтах (шаг 128 МБ, от 1024 до 16384 МБ)
        raw_mb = default.get("ram_mb")
        if raw_mb is None:
            raw_mb = int(float(default.get("ram_gb", 4)) * 1024)
        else:
            raw_mb = int(raw_mb)
        raw_mb = max(1024, min(16384, round(raw_mb / 128) * 128))
        default["ram_mb"] = raw_mb
        default["ram_gb"] = round(raw_mb / 1024, 2)

        if not default.get("mc_dir"):
            default["mc_dir"] = DEFAULT_MC_DIR
        return default

    def _save_config(self):
        with self._config_lock:
            os.makedirs(BASE_DIR, exist_ok=True)
            tmp_path = f"{CONFIG_PATH}.{threading.get_ident()}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self._config, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, CONFIG_PATH)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

    def _count_mods(self, inst_id):
        mods_dir = os.path.join(INSTANCES_DIR, inst_id, "mods")
        if not os.path.exists(mods_dir):
            return 0
        return sum(1 for f in os.listdir(mods_dir) if f.endswith(".jar"))

    def _get_cover_data_uri(self, inst):
        fname = inst.get("cover_file") or ""
        if not fname:
            return ""
        full = os.path.join(INSTANCES_DIR, inst["id"], fname)
        if not os.path.exists(full):
            return ""
        try:
            st = os.stat(full)
            key = (full, st.st_mtime_ns, st.st_size)
            if key in self._cover_cache:
                return self._cover_cache[key]
            with open(full, "rb") as f:
                uri = _image_bytes_to_data_uri(f.read(), max_bytes=4 * 1024 * 1024)
            self._cover_cache[key] = uri
            return uri
        except Exception:
            return ""

    def _get_icon_data_uri(self, inst):
        fname = inst.get("custom_icon_file") or ""
        if not fname:
            return ""
        full = os.path.join(INSTANCES_DIR, inst["id"], fname)
        if not os.path.exists(full):
            return ""
        try:
            st = os.stat(full)
            key = (full, st.st_mtime_ns, st.st_size)
            if key in self._icon_cache:
                return self._icon_cache[key]
            with open(full, "rb") as f:
                uri = _image_bytes_to_data_uri(f.read(), max_bytes=2 * 1024 * 1024)
            self._icon_cache[key] = uri
            return uri
        except Exception:
            return ""

    def _get_installed_ids(self, force=False):
        versions_dir = os.path.join(self._get_mc_dir(), "versions")
        if not os.path.isdir(versions_dir):
            self._installed_cache = (versions_dir, None, set())
            return set()
        try:
            mtime_ns = os.stat(versions_dir).st_mtime_ns
            c_dir, c_mtime, c_ids = self._installed_cache
            if not force and c_dir == versions_dir and c_mtime == mtime_ns:
                return c_ids
            ids = set()
            with os.scandir(versions_dir) as it:
                for entry in it:
                    if entry.is_dir() and os.path.isfile(os.path.join(entry.path, f"{entry.name}.json")):
                        ids.add(entry.name)
            self._installed_cache = (versions_dir, mtime_ns, ids)
            return ids
        except Exception:
            self._installed_cache = (None, None, set())
            return set()

    def _mark_version_ok(self, mc_dir, vid):
        try:
            vdir = os.path.join(mc_dir, "versions", str(vid))
            os.makedirs(vdir, exist_ok=True)
            with open(os.path.join(vdir, ".greenprism_ok"), "w", encoding="utf-8") as f:
                f.write("v8.5")
        except Exception:
            pass

    def _cleanup_broken_version(self, mc_dir, vid):
        """Удаляет недоустановленную папку версии, чтобы не отравлять кэш."""
        import shutil
        if not vid:
            return
        vdir = os.path.join(mc_dir, "versions", str(vid))
        if os.path.isdir(vdir):
            try:
                shutil.rmtree(vdir, ignore_errors=True)
            except Exception:
                pass
        self._installed_cache = (None, None, set())

    def _is_version_complete(self, mc_dir, vid, loader, mc_ver=None):
        """Проверяет, что профиль не сломан: JSON валиден, родительская Vanilla и ключевые библиотеки реально на диске."""
        if not vid:
            return False
        vdir = os.path.join(mc_dir, "versions", str(vid))
        jp = os.path.join(vdir, f"{vid}.json")
        if not os.path.isfile(jp):
            return False
        try:
            with open(jp, "r", encoding="utf-8") as f:
                vjson = json.load(f)
            if not isinstance(vjson, dict) or not vjson.get("mainClass"):
                return False
        except Exception:
            return False

        # Если передана базовая версия Minecraft — всегда проверяем наличие её JSON и JAR на диске в текущей mc_dir
        base_mc = str(mc_ver or "").strip()
        if base_mc:
            bdir = os.path.join(mc_dir, "versions", base_mc)
            bjp = os.path.join(bdir, f"{base_mc}.json")
            bjar = os.path.join(bdir, f"{base_mc}.jar")
            if not (os.path.isfile(bjp) and os.path.isfile(bjar) and os.path.getsize(bjar) > 10 * 1024):
                return False
            try:
                with open(bjp, "r", encoding="utf-8") as bf:
                    bjson = json.load(bf)
                if not isinstance(bjson, dict) or not (bjson.get("id") or bjson.get("mainClass")):
                    return False
            except Exception:
                return False

        # Проверяем базовое соответствие типа лоадера профилю
        vid_low = str(vid).lower()
        if loader == "Vanilla":
            if vjson.get("inheritsFrom") or vid_low.startswith("fabric-loader-") or vid_low.startswith("quilt-loader-") or "forge" in vid_low:
                return False
        elif loader == "Fabric":
            if not vid_low.startswith("fabric-loader-"):
                return False
        elif loader == "Quilt":
            if not vid_low.startswith("quilt-loader-"):
                return False
            m = re.match(r"quilt-loader-([^-]+(?:-[^-]+)?)-", vid_low)
            if m and _parse_ver_tuple(m.group(1)) < _parse_ver_tuple("0.26.0"):
                return False
        elif loader == "Forge":
            if "forge" not in vid_low or "neoforge" in vid_low:
                return False
        elif loader == "NeoForge":
            if "neoforge" not in vid_low:
                return False

        parent = vjson.get("inheritsFrom")
        if parent:
            pdir = os.path.join(mc_dir, "versions", str(parent))
            pjp = os.path.join(pdir, f"{parent}.json")
            pjar = os.path.join(pdir, f"{parent}.jar")
            if not (os.path.isfile(pjp) and os.path.isfile(pjar) and os.path.getsize(pjar) > 10 * 1024):
                return False
            try:
                with open(pjp, "r", encoding="utf-8") as pf:
                    pjson = json.load(pf)
                if not isinstance(pjson, dict) or not (pjson.get("id") or pjson.get("mainClass")):
                    return False
            except Exception:
                return False
        else:
            vjar = os.path.join(vdir, f"{vid}.jar")
            if not (os.path.isfile(vjar) and os.path.getsize(vjar) > 10 * 1024):
                return False

        libs_root = os.path.join(mc_dir, "libraries")
        # Для NeoForge/Forge обязательно проверяем маркер завершённой установки (лечит старые битые установки!)
        if loader in ("NeoForge", "Forge"):
            if not os.path.isfile(os.path.join(vdir, ".greenprism_ok")):
                return False

        # Для NeoForge дополнительно убеждаемся, что пропатченный neoforge-client или universal JAR существует
        if loader == "NeoForge" and str(vid).lower().startswith("neoforge-"):
            nf_ver = str(vid)[len("neoforge-"):]
            nf_lib_dir = os.path.join(libs_root, "net", "neoforged", "neoforge", nf_ver)
            c1 = os.path.join(nf_lib_dir, f"neoforge-{nf_ver}-client.jar")
            c2 = os.path.join(nf_lib_dir, f"neoforge-{nf_ver}-universal.jar")
            c3 = os.path.join(nf_lib_dir, f"neoforge-{nf_ver}.jar")
            if not any(os.path.isfile(c) and os.path.getsize(c) > 1024 for c in (c1, c2, c3)):
                return False

        # Проверяем наличие обязательных библиотек из JSON версии
        for lib in vjson.get("libraries") or []:
            if not isinstance(lib, dict) or not _lib_rules_allow_current_os(lib):
                continue
            dl = lib.get("downloads") or {}
            art = dl.get("artifact") if isinstance(dl, dict) else None
            rel = ""
            if isinstance(art, dict) and art.get("path"):
                rel = str(art["path"]).replace("\\", "/").lstrip("/")
            elif lib.get("name") and not dl.get("classifiers"):
                rel = _maven_coord_to_relpath(lib["name"])
            if rel:
                lp = os.path.join(libs_root, *rel.split("/"))
                if not (os.path.isfile(lp) and os.path.getsize(lp) > 0):
                    return False

        return True

    def _resolve_existing_version(self, inst, installed_ids, strict=True):
        mc_dir = self._get_mc_dir()
        versions_dir = os.path.join(mc_dir, "versions")
        version = inst["version"]
        loader = inst["loader"]

        def _ok(vid):
            if not vid or vid not in installed_ids:
                return False
            if not strict:
                return os.path.isfile(os.path.join(versions_dir, vid, f"{vid}.json"))
            return self._is_version_complete(mc_dir, vid, loader, mc_ver=version)

        saved_id = inst.get("installed_version_id")
        if saved_id:
            valid_for_loader = False
            sid_low = str(saved_id).lower()
            if loader == "Vanilla" and saved_id == version:
                valid_for_loader = True
            elif loader == "Fabric" and sid_low.startswith("fabric-loader-") and sid_low.endswith(f"-{version}".lower()):
                valid_for_loader = True
            elif loader == "Quilt" and sid_low.startswith("quilt-loader-") and sid_low.endswith(f"-{version}".lower()):
                m = re.match(r"quilt-loader-([^-]+(?:-[^-]+)?)-", sid_low)
                if m and _parse_ver_tuple(m.group(1)) >= _parse_ver_tuple("0.26.0"):
                    valid_for_loader = True
            elif loader == "Forge" and "forge" in sid_low and "neoforge" not in sid_low:
                v_low = version.lower()
                if sid_low.startswith(f"{v_low}-forge") or f"-{v_low}-" in sid_low or sid_low.endswith(f"-{v_low}"):
                    valid_for_loader = True
                else:
                    jp = os.path.join(versions_dir, saved_id, f"{saved_id}.json")
                    try:
                        with open(jp, "r", encoding="utf-8") as _f:
                            if json.load(_f).get("inheritsFrom") == version:
                                valid_for_loader = True
                    except Exception:
                        pass
            elif loader == "NeoForge" and "neoforge" in sid_low:
                prefixes = [f"neoforge-{p}".lower() for p in _mc_to_neoforge_prefixes(version)]
                if any(sid_low.startswith(px) for px in prefixes):
                    valid_for_loader = True
                else:
                    jp = os.path.join(versions_dir, saved_id, f"{saved_id}.json")
                    try:
                        with open(jp, "r", encoding="utf-8") as _f:
                            if json.load(_f).get("inheritsFrom") == version:
                                valid_for_loader = True
                    except Exception:
                        pass
            if valid_for_loader and _ok(saved_id):
                return saved_id

        if loader == "Vanilla":
            return version if _ok(version) else None

        if loader == "Fabric":
            suffix = f"-{version}"
            matches = sorted(
                [vid for vid in installed_ids if vid.startswith("fabric-loader-") and vid.endswith(suffix)],
                key=lambda vid: _parse_ver_tuple(vid[len("fabric-loader-"): -len(suffix)]),
                reverse=True,
            )
            for vid in matches:
                if _ok(vid):
                    return vid
            return None

        if loader == "Quilt":
            suffix = f"-{version}"
            matches = [vid for vid in installed_ids if vid.startswith("quilt-loader-") and vid.endswith(suffix)]
            valid_matches = []
            for vid in matches:
                m = re.match(r"quilt-loader-([^-]+(?:-[^-]+)?)-", vid.lower())
                if m and _parse_ver_tuple(m.group(1)) >= _parse_ver_tuple("0.26.0"):
                    valid_matches.append(vid)
            valid_matches.sort(
                key=lambda vid: _parse_ver_tuple(re.match(r"quilt-loader-([^-]+(?:-[^-]+)?)-", vid.lower()).group(1)),
                reverse=True
            )
            for vid in valid_matches:
                if _ok(vid):
                    return vid
            return None

        if loader == "Forge":
            v_low = version.lower()
            matches = sorted(
                [
                    vid for vid in installed_ids
                    if "forge" in vid.lower() and "neoforge" not in vid.lower()
                    and (vid.lower().startswith(f"{v_low}-forge") or f"-{v_low}-" in vid.lower() or vid.lower().endswith(f"-{v_low}"))
                ],
                key=_parse_ver_tuple,
                reverse=True,
            )
            for vid in matches:
                if _ok(vid):
                    return vid
            return None

        if loader == "NeoForge":
            prefixes = [f"neoforge-{p}" for p in _mc_to_neoforge_prefixes(version)]
            matches = [vid for vid in installed_ids if any(vid.startswith(px) for px in prefixes)]
            if not matches:
                for vid in installed_ids:
                    if vid.lower().startswith("neoforge-"):
                        jp = os.path.join(versions_dir, vid, f"{vid}.json")
                        try:
                            with open(jp, "r", encoding="utf-8") as _f:
                                if json.load(_f).get("inheritsFrom") == version:
                                    matches.append(vid)
                        except Exception:
                            pass
            matches.sort(key=lambda vid: _parse_ver_tuple(vid.replace("neoforge-", "")), reverse=True)
            for vid in matches:
                if _ok(vid):
                    return vid
            return None

        return None

    def _list_neoforge_builds(self, force=False):
        """Загружает список сборок NeoForge из официального Maven-репозитория."""
        if self._neoforge_builds and not force:
            return self._neoforge_builds
        try:
            import xml.etree.ElementTree as ET
            r = _get_http().get(
                "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml",
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            r.raise_for_status()
            root = ET.fromstring(r.text)
            builds = [el.text.strip() for el in root.findall(".//versioning/versions/version") if el.text and el.text.strip()]
            if builds:
                self._neoforge_builds = builds
        except Exception:
            pass
        return self._neoforge_builds

    def _find_latest_neoforge_build(self, mc_ver):
        """Находит новейший стабильный (или бета) билд NeoForge для указанной версии Minecraft."""
        builds = self._list_neoforge_builds()
        if not builds:
            builds = self._list_neoforge_builds(force=True)
        matching = [b for b in builds if _neoforge_ver_to_mc(b) == mc_ver]
        if not matching:
            prefixes = _mc_to_neoforge_prefixes(mc_ver)
            matching = [b for b in builds if any(b.startswith(p) for p in prefixes)]
        if not matching:
            return None
        stable = [b for b in matching if "beta" not in b.lower() and "alpha" not in b.lower()]
        pool = stable if stable else matching
        pool.sort(key=_parse_ver_tuple, reverse=True)
        return pool[0]

    def _ensure_jvm_runtime(self, mc_dir, target_version, mc_version, callback, inst_id=None):
        """Гарантирует наличие нужного JVM-рантайма в mc_dir/runtime и возвращает путь к java/javaw."""
        jvm_comp = None
        try:
            def _read_local_vjson(vid):
                jp = os.path.join(mc_dir, "versions", str(vid), f"{vid}.json")
                if os.path.isfile(jp):
                    with open(jp, "r", encoding="utf-8") as _f:
                        return json.load(_f)
                return {}

            vjson = _read_local_vjson(target_version) if target_version else {}
            jvm_comp = (vjson.get("javaVersion") or {}).get("component")
            if not jvm_comp and vjson.get("inheritsFrom"):
                pjson = _read_local_vjson(vjson["inheritsFrom"])
                jvm_comp = (pjson.get("javaVersion") or {}).get("component")
            if not jvm_comp:
                vstr = str(mc_version or "").strip()
                m_major = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", vstr)
                if m_major:
                    maj = int(m_major.group(1))
                    minor = int(m_major.group(2))
                    patch = int(m_major.group(3) or 0)
                    if maj >= 26:
                        jvm_comp = "java-runtime-epsilon"
                    elif maj == 1 and (minor >= 21 or (minor == 20 and patch >= 5)):
                        jvm_comp = "java-runtime-delta"
                    elif maj == 1 and minor >= 18:
                        jvm_comp = "java-runtime-gamma"
                    elif maj == 1 and minor == 17:
                        jvm_comp = "java-runtime-alpha"
                    else:
                        jvm_comp = "jre-legacy"
                elif vstr.startswith(("a", "b", "c", "rd-", "inf-")):
                    jvm_comp = "jre-legacy"

            mclib_mod = _get_mclib()
            rt_mod = getattr(mclib_mod, "runtime", None)
            if jvm_comp and rt_mod and hasattr(rt_mod, "install_jvm_runtime"):
                exe_from_rt = None
                if hasattr(rt_mod, "get_executable_path"):
                    try:
                        exe_from_rt = rt_mod.get_executable_path(jvm_comp, mc_dir)
                    except Exception:
                        exe_from_rt = None
                if not (exe_from_rt and os.path.isfile(exe_from_rt)):
                    self._set_status(f"Загрузка рантайма Java ({jvm_comp})...", 0.85, True)
                    if inst_id:
                        self._push_log(inst_id, f"[GreenPrism] Установка рантайма {jvm_comp}...")
                    rt_mod.install_jvm_runtime(jvm_comp, mc_dir, callback=callback)
                    if hasattr(rt_mod, "get_executable_path"):
                        try:
                            exe_from_rt = rt_mod.get_executable_path(jvm_comp, mc_dir)
                        except Exception:
                            exe_from_rt = None
                if exe_from_rt and os.path.isfile(exe_from_rt):
                    return exe_from_rt
        except Exception as _rt_err:
            if inst_id:
                self._push_log(inst_id, f"[GreenPrism] Рантайм {jvm_comp or 'auto'}: {_rt_err}")
        return None

    def _download_maven_libraries(self, libraries, mc_dir, extra_repos=None, inst_id=None, status_prefix="Загрузка библиотек"):
        """Параллельно скачивает список библиотек из version.json / install_profile.json в Python (обходит баги Java-прокси)."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        libs_root = os.path.join(mc_dir, "libraries")
        os.makedirs(libs_root, exist_ok=True)
        fallback_repos = list(extra_repos or []) + [
            "https://libraries.minecraft.net/",
            "https://repo1.maven.org/maven2/",
        ]

        tasks = []
        seen_paths = set()
        for lib in (libraries or []):
            if not isinstance(lib, dict) or not _lib_rules_allow_current_os(lib):
                continue
            dl = lib.get("downloads") or {}
            art = dl.get("artifact") if isinstance(dl, dict) else None

            rel_path = ""
            primary_url = ""
            expected_sha1 = ""

            if isinstance(art, dict):
                rel_path = str(art.get("path") or "").replace("\\", "/").lstrip("/")
                primary_url = str(art.get("url") or "").strip()
                expected_sha1 = str(art.get("sha1") or "").strip().lower()
            if not expected_sha1 and lib.get("sha1"):
                expected_sha1 = str(lib.get("sha1") or "").strip().lower()
            if not rel_path and lib.get("name"):
                rel_path = _maven_coord_to_relpath(lib["name"])
            if not rel_path or rel_path in seen_paths:
                continue
            seen_paths.add(rel_path)

            dst = os.path.join(libs_root, *rel_path.split("/"))
            if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                if not expected_sha1:
                    continue
                try:
                    if _file_sha1(dst).lower() == expected_sha1:
                        continue
                except Exception:
                    pass

            cand_urls = []
            if primary_url:
                cand_urls.append(primary_url)
            lib_repo = str(lib.get("url") or "").strip()
            if lib_repo:
                cand_urls.append(lib_repo.rstrip("/") + "/" + rel_path)
            for repo in fallback_repos:
                u = repo.rstrip("/") + "/" + rel_path
                if u not in cand_urls:
                    cand_urls.append(u)
            tasks.append((rel_path, dst, cand_urls, expected_sha1))

        if not tasks:
            return 0, []

        total = len(tasks)
        done = 0
        failed = []

        def _fetch_one(item):
            rel_path, dst, urls, sha1 = item
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = f"{dst}.{threading.get_ident()}.tmp"
            last_err = ""
            for u in urls:
                for _attempt in range(2):
                    try:
                        with _get_http().get(u, headers={"User-Agent": USER_AGENT}, stream=True, timeout=25) as r:
                            if r.status_code != 200:
                                last_err = f"HTTP {r.status_code}"
                                break
                            with open(tmp, "wb") as f:
                                for chunk in r.iter_content(chunk_size=32768):
                                    if chunk:
                                        f.write(chunk)
                        if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
                            last_err = "empty file"
                            continue
                        if sha1 and _file_sha1(tmp).lower() != sha1:
                            last_err = "sha1 mismatch"
                            continue
                        os.replace(tmp, dst)
                        return (True, rel_path, "")
                    except Exception as e:
                        last_err = str(e)
                    finally:
                        if os.path.exists(tmp):
                            try:
                                os.remove(tmp)
                            except Exception:
                                pass
            return (False, rel_path, last_err)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(_fetch_one, t) for t in tasks]
            for fut in as_completed(futs):
                ok, rel_path, err = fut.result()
                done += 1
                if not ok:
                    failed.append((rel_path, err))
                self._set_status(f"{status_prefix} ({done}/{total})...", 0.2 + 0.45 * (done / max(total, 1)), True)

        if failed and inst_id:
            sample = ", ".join(p for p, _ in failed[:4])
            self._push_log(inst_id, f"[GreenPrism] Не удалось предзагрузить {len(failed)} библ.: {sample}")
        return done - len(failed), failed

    def _write_json_atomic(self, dst_path, data):
        """Атомарно записывает JSON на диск через .tmp с проверкой размера > 0."""
        os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
        tmp_path = f"{dst_path}.{threading.get_ident()}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
            if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) <= 0:
                raise IOError(f"Empty JSON write: {os.path.basename(dst_path)}")
            os.replace(tmp_path, dst_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _cleanup_zero_byte_files(self, mc_dir):
        """Удаляет все 0-байтовые .jar/.json (и недокачанные .tmp/.part) в libraries и versions."""
        removed = 0
        for sub in ("libraries", "versions"):
            root_dir = os.path.join(mc_dir, sub)
            if not os.path.isdir(root_dir):
                continue
            for dirpath, _dirnames, filenames in os.walk(root_dir):
                for fn in filenames:
                    low = fn.lower()
                    if not low.endswith((".jar", ".json", ".tmp", ".part")):
                        continue
                    fp = os.path.join(dirpath, fn)
                    try:
                        if low.endswith((".tmp", ".part")) or os.path.getsize(fp) == 0:
                            os.remove(fp)
                            removed += 1
                    except Exception:
                        pass
        return removed

    @staticmethod
    def _extract_checksum_error_path(err_text):
        """Извлекает путь к повреждённому файлу из сообщения InvalidChecksum / 'has the wrong Checksum'."""
        msg = str(err_text or "").strip()
        if not msg:
            return None
        m = re.search(r"^(?:InvalidChecksum:\s*)?(.+?)\s+has\s+the\s+wrong\s+checksum\b", msg, re.I)
        if m:
            cand = m.group(1).strip().strip('"').strip("'")
            if cand and os.path.isfile(cand):
                return cand
        m2 = re.search(r"([A-Za-z]:\\[^\r\n\"'<>|]+?\.(?:jar|json))", msg, re.I)
        if m2:
            cand = m2.group(1).strip()
            if cand and os.path.isfile(cand):
                return cand
        return None

    def _run_with_checksum_retry(self, mc_dir, fn, inst_id=None, max_attempts=3):
        """Перед запуском очищает 0-байтовые файлы и при ошибке Checksum удаляет битый файл и повторяет установку."""
        self._cleanup_zero_byte_files(mc_dir)
        for attempt in range(1, max_attempts + 1):
            try:
                return fn()
            except Exception as e:
                err_str = str(e)
                is_checksum = (
                    type(e).__name__ == "InvalidChecksum"
                    or "wrong checksum" in err_str.lower()
                    or "invalidchecksum" in err_str.lower()
                )
                if not is_checksum or attempt >= max_attempts:
                    raise
                bad_path = self._extract_checksum_error_path(err_str)
                if bad_path and os.path.isfile(bad_path):
                    try:
                        os.remove(bad_path)
                        if inst_id:
                            self._push_log(inst_id, f"[GreenPrism] Удалён битый файл (Checksum): {os.path.basename(bad_path)}")
                    except Exception:
                        pass
                removed_zero = self._cleanup_zero_byte_files(mc_dir)
                if inst_id:
                    extra = f", удалено пустых файлов: {removed_zero}" if removed_zero else ""
                    self._push_log(
                        inst_id,
                        f"[GreenPrism] Ошибка контрольной суммы, повторная попытка ({attempt + 1}/{max_attempts}{extra})...",
                    )
                self._set_status(f"Повторная загрузка после сбоя Checksum ({attempt + 1}/{max_attempts})...", None, True)
                time.sleep(1.5)

    def _ensure_vanilla_version_json(self, mc_ver, mc_dir, inst_id=None):
        """Гарантирует наличие валидного versions/<mc_ver>/<mc_ver>.json на диске (скачивает из version_manifest_v2.json Mojang)."""
        ver_str = str(mc_ver or "").strip()
        if not ver_str:
            raise ValueError("Не указана версия Minecraft")
        vdir = os.path.join(mc_dir, "versions", ver_str)
        jp = os.path.join(vdir, f"{ver_str}.json")
        if os.path.isfile(jp) and os.path.getsize(jp) > 0:
            try:
                with open(jp, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict) and (existing.get("id") or existing.get("mainClass")):
                    return jp
            except Exception:
                pass

        if inst_id:
            self._push_log(inst_id, f"[GreenPrism] Загрузка манифеста Mojang для {ver_str}...")

        manifest = None
        last_err = None
        for m_url in (
            "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
            "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json",
        ):
            try:
                r = _get_http().get(m_url, headers={"User-Agent": USER_AGENT}, timeout=20)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and isinstance(data.get("versions"), list):
                    manifest = data
                    break
            except Exception as e:
                last_err = e

        if not manifest:
            raise RuntimeError(f"Не удалось загрузить version_manifest_v2.json Mojang: {last_err}")

        ver_url = ""
        for entry in manifest.get("versions") or []:
            if isinstance(entry, dict) and str(entry.get("id") or "").strip() == ver_str:
                ver_url = str(entry.get("url") or "").strip()
                break
        if not ver_url:
            raise RuntimeError(f"Версия Minecraft {ver_str} не найдена в манифесте Mojang")

        rv = _get_http().get(ver_url, headers={"User-Agent": USER_AGENT}, timeout=20)
        rv.raise_for_status()
        vdata = rv.json()
        if not isinstance(vdata, dict) or not (vdata.get("id") or vdata.get("mainClass")):
            raise RuntimeError(f"Mojang вернул некорректный JSON для версии {ver_str}")

        self._write_json_atomic(jp, vdata)
        return jp

    def _install_vanilla_version(self, mc_ver, mc_dir, callback, inst_id=None):
        """Устанавливает базовую версию Minecraft в mc_dir, гарантируя физическое наличие JSON, JAR и библиотек."""
        ver_str = str(mc_ver or "").strip()
        os.makedirs(mc_dir, exist_ok=True)
        jp = self._ensure_vanilla_version_json(ver_str, mc_dir, inst_id=inst_id)
        self._run_with_checksum_retry(
            mc_dir,
            lambda: _get_mclib().install.install_minecraft_version(ver_str, mc_dir, callback=callback),
            inst_id=inst_id,
        )

        if not os.path.isfile(jp):
            jp = self._ensure_vanilla_version_json(ver_str, mc_dir, inst_id=inst_id)

        with open(jp, "r", encoding="utf-8") as f:
            vjson = json.load(f)

        vjar = os.path.join(mc_dir, "versions", ver_str, f"{ver_str}.jar")
        if not (os.path.isfile(vjar) and os.path.getsize(vjar) > 10 * 1024):
            client_dl = (vjson.get("downloads") or {}).get("client") or {}
            if client_dl.get("url"):
                self._set_status(f"Скачивание клиента Minecraft {ver_str}...", 0.35, True)
                self._download_file(
                    client_dl["url"],
                    vjar,
                    emit_telemetry=True,
                    expected_sha1=str(client_dl.get("sha1") or ""),
                )
        if not (os.path.isfile(vjar) and os.path.getsize(vjar) > 10 * 1024):
            raise RuntimeError(f"Не удалось скачать клиентский файл {ver_str}.jar")

        self._download_maven_libraries(
            vjson.get("libraries") or [],
            mc_dir,
            inst_id=inst_id,
            status_prefix=f"Библиотеки Minecraft {ver_str}",
        )
        self._mark_version_ok(mc_dir, ver_str)
        self._installed_cache = (None, None, set())
        return ver_str

    def _install_fabric(self, mc_ver, mc_dir, callback, inst_id, loader_ver=None):
        """Чистая Python-установка Fabric через официальный Fabric Meta API (без fabric-installer.jar и Java-прокси)."""
        self._set_status(f"Подготовка базовой Minecraft {mc_ver}...", 0.08, True)
        self._install_vanilla_version(mc_ver, mc_dir, callback, inst_id)

        loader_ver = str(loader_ver or "").strip()
        if not loader_ver:
            try:
                rl = _get_http().get(
                    f"https://meta.fabricmc.net/v2/versions/loader/{mc_ver}",
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                if rl.status_code == 200:
                    llist = rl.json() or []
                    stable = next((x for x in llist if (x.get("loader") or {}).get("stable")), None)
                    pick = stable or (llist[0] if llist else None)
                    if pick:
                        loader_ver = str((pick.get("loader") or {}).get("version") or "").strip()
            except Exception:
                pass
        if not loader_ver:
            loader_ver = _get_mclib().fabric.get_latest_loader_version()
        if not loader_ver:
            raise RuntimeError(f"Не удалось получить версию Fabric Loader для Minecraft {mc_ver}")

        vid = f"fabric-loader-{loader_ver}-{mc_ver}"
        self._set_status(f"Загрузка профиля Fabric {loader_ver}...", 0.25, True)
        self._push_log(inst_id, f"[GreenPrism] Установка Fabric {loader_ver} для Minecraft {mc_ver} (прямая загрузка)...")

        rp = _get_http().get(
            f"https://meta.fabricmc.net/v2/versions/loader/{mc_ver}/{loader_ver}/profile/json",
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        rp.raise_for_status()
        profile_json = rp.json()
        if not isinstance(profile_json, dict) or not profile_json.get("mainClass"):
            raise RuntimeError("Fabric Meta API вернул некорректный профиль")
        vid = str(profile_json.get("id") or vid)
        if not profile_json.get("jar"):
            profile_json["jar"] = str(profile_json.get("inheritsFrom") or mc_ver)

        try:
            vdir = os.path.join(mc_dir, "versions", vid)
            self._write_json_atomic(os.path.join(vdir, f"{vid}.json"), profile_json)

            _ok_cnt, failed = self._download_maven_libraries(
                profile_json.get("libraries") or [],
                mc_dir,
                extra_repos=["https://maven.fabricmc.net/"],
                inst_id=inst_id,
                status_prefix=f"Библиотеки Fabric {mc_ver}",
            )
            if failed:
                raise RuntimeError(f"Не удалось скачать библиотеки Fabric ({len(failed)} шт.): {failed[0][0]}")

            self._mark_version_ok(mc_dir, vid)
            self._push_log(inst_id, f"[GreenPrism] Fabric {vid} успешно установлен!")
            return vid
        except Exception:
            self._cleanup_broken_version(mc_dir, vid)
            raise

    def _install_quilt(self, mc_ver, mc_dir, callback, inst_id, loader_ver=None):
        """Чистая Python-установка Quilt через официальный Quilt Meta API (без Java-прокси)."""
        self._set_status(f"Подготовка базовой Minecraft {mc_ver}...", 0.08, True)
        self._install_vanilla_version(mc_ver, mc_dir, callback, inst_id)

        loader_ver = str(loader_ver or "").strip()
        if not loader_ver:
            try:
                rl = _get_http().get(
                    f"https://meta.quiltmc.org/v3/versions/loader/{mc_ver}",
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                if rl.status_code == 200:
                    llist = rl.json() or []
                    v_candidates = []
                    for item in llist:
                        lv = str((item.get("loader") or {}).get("version") or "").strip()
                        if lv:
                            v_candidates.append(lv)
                    if v_candidates:
                        v_candidates.sort(key=_parse_ver_tuple, reverse=True)
                        loader_ver = v_candidates[0]
            except Exception:
                pass
        if not loader_ver:
            try:
                loader_ver = _get_mclib().quilt.get_latest_loader_version()
            except Exception:
                pass
        if not loader_ver:
            loader_ver = "0.28.1"

        vid = f"quilt-loader-{loader_ver}-{mc_ver}"
        self._set_status(f"Загрузка профиля Quilt {loader_ver}...", 0.25, True)
        self._push_log(inst_id, f"[GreenPrism] Установка Quilt {loader_ver} для Minecraft {mc_ver} (прямая загрузка)...")

        profile_json = None
        try:
            rp = _get_http().get(
                f"https://meta.quiltmc.org/v3/versions/loader/{mc_ver}/{loader_ver}/profile/json",
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            if rp.status_code == 200:
                profile_json = rp.json()
        except Exception:
            pass

        if not profile_json or not isinstance(profile_json, dict) or not profile_json.get("mainClass"):
            self._push_log(inst_id, f"[GreenPrism] Запасной вариант установки Quilt через minecraft-launcher-lib...")
            self._run_with_checksum_retry(
                mc_dir,
                lambda: _get_mclib().quilt.install_quilt(mc_ver, mc_dir, loader_version=loader_ver, callback=callback),
                inst_id=inst_id,
            )
            installed = self._get_installed_ids(force=True)
            cands = sorted(
                [cid for cid in installed if cid.startswith("quilt-loader-") and cid.endswith(f"-{mc_ver}")],
                key=lambda cid: _parse_ver_tuple(cid[len("quilt-loader-"): -len(f"-{mc_ver}")]),
                reverse=True,
            )
            if cands:
                cid = cands[0]
                self._mark_version_ok(mc_dir, cid)
                self._push_log(inst_id, f"[GreenPrism] Quilt {cid} успешно установлен!")
                return cid
            self._mark_version_ok(mc_dir, vid)
            return vid

        vid = str(profile_json.get("id") or vid)
        if not profile_json.get("jar"):
            profile_json["jar"] = str(profile_json.get("inheritsFrom") or mc_ver)

        try:
            vdir = os.path.join(mc_dir, "versions", vid)
            self._write_json_atomic(os.path.join(vdir, f"{vid}.json"), profile_json)

            _ok_cnt, failed = self._download_maven_libraries(
                profile_json.get("libraries") or [],
                mc_dir,
                extra_repos=[
                    "https://maven.quiltmc.org/repository/release/",
                    "https://maven.fabricmc.net/",
                ],
                inst_id=inst_id,
                status_prefix=f"Библиотеки Quilt {mc_ver}",
            )
            if failed:
                raise RuntimeError(f"Не удалось скачать библиотеки Quilt ({len(failed)} шт.): {failed[0][0]}")

            self._mark_version_ok(mc_dir, vid)
            self._push_log(inst_id, f"[GreenPrism] Quilt {vid} успешно установлен!")
            return vid
        except Exception:
            self._cleanup_broken_version(mc_dir, vid)
            raise

    def _install_neoforge(self, mc_ver, mc_dir, callback, inst_id, nf_ver=None):
        """Устанавливает NeoForge: предзагружает библиотеки и маппинги в Python, затем запускает патчинг без багов Java-прокси."""
        import shutil
        import subprocess
        import tempfile
        import zipfile

        if not nf_ver:
            nf_ver = self._find_latest_neoforge_build(mc_ver)
        if not nf_ver:
            raise RuntimeError(f"NeoForge не найден для Minecraft {mc_ver}")

        installed_id = f"neoforge-{nf_ver}"

        self._set_status(f"Подготовка базовой Minecraft {mc_ver}...", 0.08, True)
        self._install_vanilla_version(mc_ver, mc_dir, callback, inst_id)

        java_exe = (self._config.get("java_path") or "").strip().strip('"')
        if not (java_exe and os.path.isfile(java_exe)):
            java_exe = self._ensure_jvm_runtime(mc_dir, mc_ver, mc_ver, callback, inst_id)
        if not (java_exe and os.path.isfile(java_exe)):
            java_exe = shutil.which("java") or shutil.which("javaw")
        if not java_exe:
            raise FileNotFoundError("Не найдена Java для запуска установщика NeoForge.")

        if sys.platform == "win32" and java_exe.lower().endswith("javaw.exe"):
            cand_java = java_exe[:-9] + "java.exe"
            if os.path.isfile(cand_java):
                java_exe = cand_java

        lp_path = os.path.join(mc_dir, "launcher_profiles.json")
        if not os.path.exists(lp_path):
            with open(lp_path, "w", encoding="utf-8") as f:
                json.dump({"profiles": {}}, f)

        installer_url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{nf_ver}/neoforge-{nf_ver}-installer.jar"
        fd, tmp_jar = tempfile.mkstemp(prefix="neoforge_", suffix=".jar")
        os.close(fd)
        try:
            self._set_status(f"Скачивание NeoForge {nf_ver}...", 0.16, True)
            self._push_log(inst_id, f"[GreenPrism] Скачиваю установщик NeoForge {nf_ver}...")
            self._download_file(installer_url, tmp_jar)

            pre_libs = []
            mojang_maps_rel = ""
            try:
                with zipfile.ZipFile(tmp_jar, "r") as zf:
                    libs_root = os.path.join(mc_dir, "libraries")
                    for zname in zf.namelist():
                        if zname.startswith("maven/") and not zname.endswith("/"):
                            rel = zname[len("maven/"):]
                            dst = _safe_join(libs_root, rel)
                            if not (os.path.isfile(dst) and os.path.getsize(dst) > 0):
                                os.makedirs(os.path.dirname(dst), exist_ok=True)
                                with open(dst, "wb") as out_f:
                                    out_f.write(zf.read(zname))

                    if "install_profile.json" in zf.namelist():
                        ip = json.loads(zf.read("install_profile.json").decode("utf-8", errors="ignore"))
                        pre_libs.extend(ip.get("libraries") or [])
                        data_sec = ip.get("data") or {}
                        mm = data_sec.get("MOJANG_MAPS")
                        if isinstance(mm, dict) and mm.get("client"):
                            mojang_maps_rel = _maven_coord_to_relpath(mm["client"])
                        if ip.get("version"):
                            installed_id = str(ip["version"])

                    if "version.json" in zf.namelist():
                        vj = json.loads(zf.read("version.json").decode("utf-8", errors="ignore"))
                        pre_libs.extend(vj.get("libraries") or [])
                        if vj.get("id"):
                            installed_id = str(vj["id"])
            except Exception as _zip_err:
                self._push_log(inst_id, f"[GreenPrism] Предупреждение разбора установщика NeoForge: {_zip_err}")

            if mojang_maps_rel:
                try:
                    maps_dst = os.path.join(mc_dir, "libraries", *mojang_maps_rel.split("/"))
                    if not (os.path.isfile(maps_dst) and os.path.getsize(maps_dst) > 1024):
                        vjp = os.path.join(mc_dir, "versions", str(mc_ver), f"{mc_ver}.json")
                        if os.path.isfile(vjp):
                            with open(vjp, "r", encoding="utf-8") as _vf:
                                vd = json.load(_vf)
                            cm = (vd.get("downloads") or {}).get("client_mappings") or {}
                            if cm.get("url"):
                                os.makedirs(os.path.dirname(maps_dst), exist_ok=True)
                                self._download_file(cm["url"], maps_dst, emit_telemetry=False)
                except Exception:
                    pass

            if pre_libs:
                self._push_log(inst_id, f"[GreenPrism] Предзагрузка библиотек NeoForge {nf_ver} ({len(pre_libs)} записей)...")
                self._download_maven_libraries(
                    pre_libs,
                    mc_dir,
                    extra_repos=["https://maven.neoforged.net/releases/", "https://maven.minecraftforge.net/"],
                    inst_id=inst_id,
                    status_prefix=f"Библиотеки NeoForge {nf_ver}",
                )

            self._cleanup_broken_version(mc_dir, installed_id)

            self._set_status(f"Патчинг клиента NeoForge {nf_ver} (~20 сек)...", 0.72, True)
            self._push_log(inst_id, f"[GreenPrism] Запуск установщика NeoForge {nf_ver} в {mc_dir}...")

            kw = {"env": _clean_java_env()}
            if sys.platform == "win32":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
                kw["startupinfo"] = si
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW

            java_cmd = [java_exe] + _build_java_proxy_args() + ["-jar", tmp_jar, "--installClient", mc_dir]
            cp = subprocess.run(
                java_cmd,
                cwd=mc_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=360,
                **kw,
            )
            if cp.returncode != 0:
                tail = (cp.stdout or "").strip()[-1800:]
                self._push_log(inst_id, f"[NeoForge Installer]\n{tail}")
                self._cleanup_broken_version(mc_dir, installed_id)
                raise RuntimeError(f"Установщик NeoForge завершился с кодом {cp.returncode}")

            final_jp = os.path.join(mc_dir, "versions", installed_id, f"{installed_id}.json")
            if os.path.isfile(final_jp):
                try:
                    with open(final_jp, "r", encoding="utf-8") as _ff:
                        fj = json.load(_ff)
                    if not fj.get("jar"):
                        fj["jar"] = str(fj.get("inheritsFrom") or mc_ver)
                        with open(final_jp, "w", encoding="utf-8") as _ff:
                            json.dump(fj, _ff, ensure_ascii=False, indent=2)
                    self._download_maven_libraries(
                        fj.get("libraries") or [],
                        mc_dir,
                        extra_repos=["https://maven.neoforged.net/releases/"],
                        inst_id=inst_id,
                        status_prefix="Проверка библиотек NeoForge",
                    )
                except Exception:
                    pass
                self._mark_version_ok(mc_dir, installed_id)
                self._push_log(inst_id, f"[GreenPrism] NeoForge {nf_ver} успешно установлен!")
                return installed_id

            self._installed_cache = (None, None, set())
            ids = self._get_installed_ids(force=True)
            resolved = self._resolve_existing_version({"loader": "NeoForge", "version": mc_ver}, ids, strict=False)
            if resolved:
                r_jp = os.path.join(mc_dir, "versions", resolved, f"{resolved}.json")
                if os.path.isfile(r_jp):
                    try:
                        with open(r_jp, "r", encoding="utf-8") as _ff:
                            fj = json.load(_ff)
                        if not fj.get("jar"):
                            fj["jar"] = str(fj.get("inheritsFrom") or mc_ver)
                            with open(r_jp, "w", encoding="utf-8") as _ff:
                                json.dump(fj, _ff, ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                self._mark_version_ok(mc_dir, resolved)
                return resolved
            raise RuntimeError(f"После установки не найден профиль NeoForge ({installed_id})")
        except Exception:
            self._cleanup_broken_version(mc_dir, installed_id)
            raise
        finally:
            try:
                os.remove(tmp_jar)
            except Exception:
                pass

    def _install_forge(self, mc_ver, mc_dir, callback, inst_id, forge_ver=None):
        """Устанавливает Forge с защитой от битых прокси и откатом при ошибке."""
        import shutil
        req_fv = str(forge_ver or "").strip()
        resolved_fv = ""
        if req_fv:
            cand_fv = req_fv if "-" in req_fv else f"{mc_ver}-{req_fv}"
            try:
                all_builds = _get_mclib().forge.list_forge_versions()
                if cand_fv in all_builds:
                    resolved_fv = cand_fv
            except Exception:
                resolved_fv = cand_fv
        forge_ver = resolved_fv or self._find_latest_forge_build(mc_ver)
        if not forge_ver:
            raise RuntimeError(f"Forge не найден для Minecraft {mc_ver}")
        target_version = _get_mclib().forge.forge_to_installed_version(forge_ver)
        self._cleanup_broken_version(mc_dir, target_version)
        try:
            self._set_status(f"Подготовка базовой Minecraft {mc_ver}...", 0.1, True)
            self._install_vanilla_version(mc_ver, mc_dir, callback, inst_id)

            java_exe = (self._config.get("java_path") or "").strip().strip('"')
            if not (java_exe and os.path.isfile(java_exe)):
                java_exe = self._ensure_jvm_runtime(mc_dir, mc_ver, mc_ver, callback, inst_id)
            if not (java_exe and os.path.isfile(java_exe)):
                java_exe = shutil.which("java") or shutil.which("javaw")
            if not java_exe:
                raise FileNotFoundError("Не найдена Java для запуска установщика Forge.")
            if sys.platform == "win32" and java_exe.lower().endswith("javaw.exe"):
                cand_java = java_exe[:-9] + "java.exe"
                if os.path.isfile(cand_java):
                    java_exe = cand_java

            self._set_status(f"Установка Forge {forge_ver}...", 0.35, True)
            self._push_log(inst_id, f"[GreenPrism] Установка Forge {forge_ver}...")
            self._run_with_checksum_retry(
                mc_dir,
                lambda: _get_mclib().forge.install_forge_version(forge_ver, mc_dir, callback=callback, java=java_exe),
                inst_id=inst_id,
            )

            final_jp = os.path.join(mc_dir, "versions", target_version, f"{target_version}.json")
            if os.path.isfile(final_jp):
                try:
                    with open(final_jp, "r", encoding="utf-8") as _ff:
                        fj = json.load(_ff)
                    if not fj.get("jar"):
                        fj["jar"] = str(fj.get("inheritsFrom") or mc_ver)
                        with open(final_jp, "w", encoding="utf-8") as _ff:
                            json.dump(fj, _ff, ensure_ascii=False, indent=2)
                    self._download_maven_libraries(
                        fj.get("libraries") or [],
                        mc_dir,
                        extra_repos=["https://maven.minecraftforge.net/"],
                        inst_id=inst_id,
                        status_prefix="Проверка библиотек Forge",
                    )
                except Exception:
                    pass
                self._mark_version_ok(mc_dir, target_version)
                self._push_log(inst_id, f"[GreenPrism] Forge {target_version} успешно установлен!")
                return target_version
            raise RuntimeError(f"Профиль Forge не был создан ({target_version})")
        except Exception:
            self._cleanup_broken_version(mc_dir, target_version)
            raise

    def _find_latest_forge_build(self, mc_ver):
        """Находит новейший билд Forge для указанной версии MC (устойчиво к порядку в maven-metadata)."""
        try:
            all_builds = _get_mclib().forge.list_forge_versions()
            matching = [fv for fv in all_builds if fv.split("-")[0] == mc_ver]
            if matching:
                matching.sort(
                    key=lambda fv: _parse_ver_tuple(fv.split("-", 1)[1] if "-" in fv else fv),
                    reverse=True,
                )
                return matching[0]
        except Exception:
            pass
        return _get_mclib().forge.find_forge_version(mc_ver)

    def _fetch_versions_async(self):
        mc_order = {}
        try:
            all_mc = _get_mclib().utils.get_version_list()
            vanilla_full = [
                {"id": v["id"], "type": v.get("type", "release")}
                for v in all_mc
                if v.get("id")
            ]
            if vanilla_full:
                self._versions["Vanilla"] = vanilla_full
                mc_order = {v["id"]: idx for idx, v in enumerate(vanilla_full)}
        except Exception:
            pass

        try:
            fab = _get_mclib().fabric.get_stable_minecraft_versions()
            if fab:
                self._versions["Fabric"] = sort_mc_versions(fab, mc_order)
        except Exception:
            pass

        try:
            qlt = None
            try:
                qlt = _get_mclib().quilt.get_stable_minecraft_versions()
            except Exception:
                pass
            if not qlt:
                r = _get_http().get("https://meta.quiltmc.org/v3/versions/game", timeout=6)
                if r.status_code == 200:
                    data = r.json()
                    qlt = [item["version"] for item in data if item.get("stable")]
            if qlt:
                self._versions["Quilt"] = sort_mc_versions(qlt, mc_order)
        except Exception:
            pass

        try:
            forge_all = _get_mclib().forge.list_forge_versions()
            raw_mc_vers = []
            for fv in forge_all:
                mc_ver = fv.split("-")[0].strip()
                if mc_ver and "_" not in mc_ver:
                    raw_mc_vers.append(mc_ver)
            if raw_mc_vers:
                self._versions["Forge"] = sort_mc_versions(raw_mc_vers, mc_order, only_manifest=True)
        except Exception:
            pass

        try:
            nf_builds = self._list_neoforge_builds(force=True)
            nf_mc_vers = [_neoforge_ver_to_mc(b) for b in nf_builds]
            nf_mc_vers = [v for v in nf_mc_vers if v]
            if nf_mc_vers:
                self._versions["NeoForge"] = sort_mc_versions(nf_mc_vers, mc_order, only_manifest=bool(mc_order))
        except Exception:
            pass

        self._versions_cache_ts = time.time()
        self._save_versions_cache()
        self._emit_js(f"window.onVersionsLoaded && window.onVersionsLoaded({json.dumps(self._versions)})")

    # ---------------- Хелперы аккаунтов (Оффлайн / Ely.by) ----------------
    def _get_accounts(self):
        accs = self._config.get("accounts")
        if not isinstance(accs, list) or not accs:
            uname = (self._config.get("username") or "Player").strip() or "Player"
            accs = [{"id": f"offline:{uname.lower()}", "type": "offline", "username": uname, "uuid": ""}]
            self._config["accounts"] = accs
        return accs

    def _get_active_account(self):
        accs = self._get_accounts()
        aid = self._config.get("active_account_id") or ""
        for a in accs:
            if a.get("id") == aid:
                return a
        self._config["active_account_id"] = accs[0]["id"]
        self._config["username"] = accs[0]["username"]
        return accs[0]

    def _sync_profiles_legacy(self):
        accs = self._get_accounts()
        active = self._get_active_account()
        self._config["username"] = active["username"]
        self._config["profiles"] = [a["username"] for a in accs]

    def _public_accounts(self):
        """Безопасный список аккаунтов для UI (без токенов доступа)."""
        accs = self._get_accounts()
        active = self._get_active_account()
        out = []
        for a in accs:
            out.append({
                "id": a["id"],
                "type": a.get("type", "offline"),
                "username": a["username"],
                "uuid": a.get("uuid", ""),
                "active": a["id"] == active["id"],
            })
        return out

    # ---------------- Методы для JS ----------------
    def get_state(self):
        installed_ids = self._get_installed_ids()
        instances = []
        for inst in self._config.get("instances", []):
            item = dict(inst)
            item["mods_count"] = self._count_mods(inst["id"])
            item["is_installed"] = self._resolve_existing_version(inst, installed_ids) is not None
            item["is_running"] = self._running_inst_id == inst["id"]
            item["cover_uri"] = self._get_cover_data_uri(inst)
            item["icon_uri"] = self._get_icon_data_uri(inst)
            instances.append(item)
        ram_mb = int(self._config.get("ram_mb") or int(float(self._config.get("ram_gb", 4)) * 1024))
        ram_mb = max(1024, min(16384, round(ram_mb / 128) * 128))
        active_acc = self._get_active_account()
        return {
            "username": active_acc["username"],
            "active_account_id": active_acc["id"],
            "active_account_type": active_acc.get("type", "offline"),
            "accounts": self._public_accounts(),
            "profiles": self._config.get("profiles", [active_acc["username"]]),
            "ram_mb": ram_mb,
            "ram_gb": round(ram_mb / 1024, 2),
            "java_path": self._config.get("java_path", ""),
            "cf_api_key": self._config.get("cf_api_key", ""),
            "has_embedded_cf_key": bool(EMBEDDED_CF_API_KEY),
            "mc_dir": self._get_mc_dir(),
            "theme": self._config.get("theme", "emerald"),
            "frameless": bool(self._config.get("frameless", True)),
            "ambient_glow": bool(self._config.get("ambient_glow", True)),
            "auto_install_deps": bool(self._config.get("auto_install_deps", True)),
            "hide_on_launch": bool(self._config.get("hide_on_launch", False)),
            "discord_rpc": bool(self._config.get("discord_rpc", True)),
            "sidebar_collapsed": bool(self._config.get("sidebar_collapsed", False)),
            "custom_bg_uri": self._get_custom_bg_uri(),
            "custom_bg_dim": int(self._config.get("custom_bg_dim", 55)),
            "custom_bg_blur": int(self._config.get("custom_bg_blur", 4)),
            "cached_versions_count": len(installed_ids),
            "running_inst_id": self._running_inst_id,
            "boot_ms": round((time.perf_counter() - _BOOT_T0) * 1000),
            "instances": instances,
            "versions": self._versions,
        }

    def set_theme(self, theme_name, ambient_glow=None):
        if theme_name in ("nord", "gold", "nord_frost", "royal_gold"):
            theme_name = "emerald"
        self._config["theme"] = theme_name or "emerald"
        if ambient_glow is not None:
            self._config["ambient_glow"] = bool(ambient_glow)
        self._save_config()
        return self.get_state()

    def set_sidebar_collapsed(self, collapsed):
        self._config["sidebar_collapsed"] = bool(collapsed)
        self._save_config()
        return self._config["sidebar_collapsed"]

    def set_discord_rpc_enabled(self, enabled):
        self._config["discord_rpc"] = bool(enabled)
        self._save_config()
        if hasattr(self, "_discord_rpc"):
            self._discord_rpc.set_enabled(bool(enabled))
        return bool(enabled)

    def save_settings(self, username, ram_gb, cf_api_key, java_path, mc_dir="", theme="emerald", ambient_glow=True, frameless=True, auto_install_deps=True, hide_on_launch=False, discord_rpc=True):
        active_acc = self._get_active_account()
        clean_user = (username or "").strip() or active_acc["username"] or "Player"
        # Если активен Ely.by и ник не менялся (или пустой) — сохраняем Ely.by-сессию нетронутой
        if active_acc.get("type") == "elyby" and clean_user.lower() == active_acc["username"].lower():
            pass
        elif active_acc.get("type") != "elyby":
            accs = self._get_accounts()
            aid = f"offline:{clean_user.lower()}"
            existing = next((x for x in accs if x["id"] == aid), None)
            if existing:
                existing["username"] = clean_user
                self._config["active_account_id"] = existing["id"]
            else:
                new_acc = {"id": aid, "type": "offline", "username": clean_user, "uuid": ""}
                accs.insert(0, new_acc)
                self._config["active_account_id"] = new_acc["id"]
            self._sync_profiles_legacy()
        try:
            raw_ram = float(ram_gb)
        except Exception:
            raw_ram = 4096.0
        ram_mb = int(raw_ram * 1024) if raw_ram <= 64 else int(raw_ram)
        ram_mb = max(1024, min(16384, round(ram_mb / 128) * 128))
        self._config["ram_mb"] = ram_mb
        self._config["ram_gb"] = round(ram_mb / 1024, 2)
        self._config["cf_api_key"] = (cf_api_key or "").strip()
        self._config["java_path"] = (java_path or "").strip()
        old_mc_dir = os.path.normcase(os.path.abspath(self._get_mc_dir()))
        clean_mc = (mc_dir or "").strip()
        self._config["mc_dir"] = clean_mc if clean_mc else DEFAULT_MC_DIR
        new_mc_dir = os.path.normcase(os.path.abspath(self._get_mc_dir()))
        if new_mc_dir != old_mc_dir:
            self._installed_cache = (None, None, set())
            for inst in self._config.get("instances", []):
                if inst.get("installed_version_id"):
                    inst["installed_version_id"] = ""
        clean_theme = theme or "emerald"
        if clean_theme in ("nord", "gold", "nord_frost", "royal_gold"):
            clean_theme = "emerald"
        self._config["theme"] = clean_theme
        self._config["ambient_glow"] = bool(ambient_glow)
        self._config["frameless"] = bool(frameless)
        self._config["auto_install_deps"] = bool(auto_install_deps)
        self._config["hide_on_launch"] = bool(hide_on_launch)
        self._config["discord_rpc"] = bool(discord_rpc)
        if hasattr(self, "_discord_rpc"):
            self._discord_rpc.set_enabled(bool(discord_rpc))
        os.makedirs(self._get_mc_dir(), exist_ok=True)
        self._save_config()
        self._set_status("Настройки сохранены", 0, False)
        return self.get_state()

    def choose_java_file(self):
        """Открывает диалог выбора исполняемого файла Java (javaw.exe / java.exe)."""
        if not self._window:
            return None
        dialog_type = getattr(webview, "OPEN_DIALOG", None)
        if dialog_type is None and hasattr(webview, "FileDialog"):
            dialog_type = webview.FileDialog.OPEN
        try:
            ftypes = ("Java Executable (javaw.exe;java.exe;*.exe)", "Все файлы (*.*)") if sys.platform == "win32" else ("Все файлы (*.*)",)
            res = self._window.create_file_dialog(dialog_type, allow_multiple=False, file_types=ftypes)
            if res and len(res) > 0:
                return res[0]
        except Exception:
            pass
        return None

    def choose_mc_dir(self):
        if not self._window:
            return None
        dialog_type = getattr(webview, "FOLDER_DIALOG", None)
        if dialog_type is None and hasattr(webview, "FileDialog"):
            dialog_type = webview.FileDialog.FOLDER
        try:
            res = self._window.create_file_dialog(dialog_type, directory=self._get_mc_dir())
            if res and len(res) > 0:
                return res[0]
        except Exception:
            pass
        return None

    def choose_instance_cover(self, inst_id):
        import shutil
        inst = self._find_instance(inst_id)
        if not inst or not self._window:
            return {"ok": False}
        dialog_type = getattr(webview, "OPEN_DIALOG", None)
        if dialog_type is None and hasattr(webview, "FileDialog"):
            dialog_type = webview.FileDialog.OPEN
        try:
            res = self._window.create_file_dialog(
                dialog_type,
                allow_multiple=False,
                file_types=("Изображения (*.png;*.jpg;*.jpeg;*.webp)",),
            )
            if not res:
                return {"ok": False}
            src = res[0]
            if not os.path.exists(src) or os.path.getsize(src) > 4 * 1024 * 1024:
                return {"ok": False, "error": "Файл слишком большой (макс. 4 МБ)"}
            ext = os.path.splitext(src)[1].lower() or ".png"
            inst_dir = os.path.join(INSTANCES_DIR, inst_id)
            os.makedirs(inst_dir, exist_ok=True)
            old_cover = inst.get("cover_file") or ""
            dst_name = f"greenprism_cover{ext}"
            if old_cover and old_cover != dst_name:
                old_p = os.path.join(inst_dir, old_cover)
                if os.path.exists(old_p):
                    try:
                        os.remove(old_p)
                    except Exception:
                        pass
            shutil.copy2(src, os.path.join(inst_dir, dst_name))
            inst["cover_file"] = dst_name
            self._save_config()
            return {"ok": True, "state": self.get_state()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def clear_instance_cover(self, inst_id):
        inst = self._find_instance(inst_id)
        if inst:
            old = inst.get("cover_file")
            if old:
                p = os.path.join(INSTANCES_DIR, inst_id, old)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            inst["cover_file"] = ""
            self._save_config()
        return self.get_state()

    def pick_custom_icon_file(self):
        """Открывает диалог выбора своей иконки до создания сборки и возвращает превью."""
        if not self._window:
            return {"ok": False}
        dialog_type = getattr(webview, "OPEN_DIALOG", None)
        if dialog_type is None and hasattr(webview, "FileDialog"):
            dialog_type = webview.FileDialog.OPEN
        try:
            res = self._window.create_file_dialog(
                dialog_type,
                allow_multiple=False,
                file_types=("Изображения (*.png;*.jpg;*.jpeg;*.webp;*.gif)",),
            )
            if not res:
                return {"ok": False}
            src = res[0]
            if not os.path.exists(src) or os.path.getsize(src) > 2 * 1024 * 1024:
                return {"ok": False, "error": "Иконка слишком большая (макс. 2 МБ)"}
            with open(src, "rb") as f:
                uri = _image_bytes_to_data_uri(f.read(), max_bytes=2 * 1024 * 1024)
            if not uri:
                return {"ok": False, "error": "Неподдерживаемый формат изображения"}
            return {"ok": True, "path": src, "data_uri": uri}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def choose_instance_icon(self, inst_id):
        import shutil
        inst = self._find_instance(inst_id)
        if not inst or not self._window:
            return {"ok": False}
        dialog_type = getattr(webview, "OPEN_DIALOG", None)
        if dialog_type is None and hasattr(webview, "FileDialog"):
            dialog_type = webview.FileDialog.OPEN
        try:
            res = self._window.create_file_dialog(
                dialog_type,
                allow_multiple=False,
                file_types=("Изображения (*.png;*.jpg;*.jpeg;*.webp;*.gif)",),
            )
            if not res:
                return {"ok": False}
            src = res[0]
            if not os.path.exists(src) or os.path.getsize(src) > 2 * 1024 * 1024:
                return {"ok": False, "error": "Иконка слишком большая (макс. 2 МБ)"}
            with open(src, "rb") as f:
                uri = _image_bytes_to_data_uri(f.read(), max_bytes=2 * 1024 * 1024)
            if not uri:
                return {"ok": False, "error": "Неподдерживаемый формат изображения"}
            ext = os.path.splitext(src)[1].lower() or ".png"
            inst_dir = os.path.join(INSTANCES_DIR, inst_id)
            os.makedirs(inst_dir, exist_ok=True)
            old_icon = inst.get("custom_icon_file") or ""
            dst_name = f"greenprism_icon{ext}"
            if old_icon and old_icon != dst_name:
                old_p = os.path.join(inst_dir, old_icon)
                if os.path.exists(old_p):
                    try:
                        os.remove(old_p)
                    except Exception:
                        pass
            shutil.copy2(src, os.path.join(inst_dir, dst_name))
            inst["custom_icon_file"] = dst_name
            self._save_config()
            return {"ok": True, "state": self.get_state(), "data_uri": uri}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def clear_instance_icon(self, inst_id):
        inst = self._find_instance(inst_id)
        if inst:
            old = inst.get("custom_icon_file")
            if old:
                p = os.path.join(INSTANCES_DIR, inst_id, old)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            inst["custom_icon_file"] = ""
            self._save_config()
        return self.get_state()

    def _get_custom_bg_uri(self):
        fname = self._config.get("custom_bg_file") or ""
        if not fname:
            return ""
        full_path = os.path.join(BASE_DIR, fname)
        if not os.path.isfile(full_path):
            return ""
        try:
            st = os.stat(full_path)
            cache_key = (full_path, st.st_mtime_ns, st.st_size)
            if hasattr(self, "_custom_bg_cache") and self._custom_bg_cache and self._custom_bg_cache.get("key") == cache_key:
                return self._custom_bg_cache.get("uri", "")
            uri = _image_file_to_data_uri(full_path, max_bytes=30 * 1024 * 1024)
            self._custom_bg_cache = {"key": cache_key, "uri": uri}
            return uri
        except Exception:
            return ""

    def select_background_image(self):
        """Открывает диалог выбора фонового изображения лаунчера и сохраняет в BASE_DIR."""
        import shutil
        if not self._window:
            return {"ok": False, "error": "Окно не готово"}
        dialog_type = getattr(webview, "OPEN_DIALOG", None)
        if dialog_type is None and hasattr(webview, "FileDialog"):
            dialog_type = webview.FileDialog.OPEN
        try:
            file_types = (
                "Изображения (*.png;*.jpg;*.jpeg;*.webp;*.gif)",
                "Все файлы (*.*)",
            )
            res = self._window.create_file_dialog(
                dialog_type,
                allow_multiple=False,
                file_types=file_types,
            )
            if not res or not len(res):
                return {"ok": False}
            src = res[0]
            if not os.path.isfile(src):
                return {"ok": False, "error": "Файл не найден"}
            if os.path.getsize(src) > 30 * 1024 * 1024:
                return {"ok": False, "error": "Файл слишком большой (макс. 30 МБ)"}

            ext = os.path.splitext(src)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                ext = ".png"

            dst_name = f"custom_bg{ext}"
            dst_path = os.path.join(BASE_DIR, dst_name)
            tmp_path = os.path.join(BASE_DIR, f"custom_bg_tmp_{threading.get_ident()}{ext}.tmp")
            try:
                shutil.copy2(src, tmp_path)
                os.replace(tmp_path, dst_path)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

            try:
                for f in os.listdir(BASE_DIR):
                    if f.startswith("custom_bg.") and f != dst_name:
                        try:
                            os.remove(os.path.join(BASE_DIR, f))
                        except Exception:
                            pass
            except Exception:
                pass

            self._config["custom_bg_file"] = dst_name
            self._save_config()
            self._custom_bg_cache = None

            uri = self._get_custom_bg_uri()
            dim = int(self._config.get("custom_bg_dim", 55))
            blur = int(self._config.get("custom_bg_blur", 4))
            return {
                "ok": True,
                "bg_uri": uri,
                "dim": dim,
                "blur": blur,
                "state": self.get_state(),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reset_background_image(self):
        """Сбрасывает кастомный фон и удаляет сохранённый файл."""
        try:
            for f in os.listdir(BASE_DIR):
                if f.startswith("custom_bg."):
                    try:
                        os.remove(os.path.join(BASE_DIR, f))
                    except Exception:
                        pass
        except Exception:
            pass
        self._config["custom_bg_file"] = ""
        self._save_config()
        self._custom_bg_cache = None
        return {"ok": True, "bg_uri": "", "state": self.get_state()}

    def save_background_settings(self, dim, blur):
        """Сохраняет настройки затемнения и размытия фона."""
        try:
            dim_val = max(0, min(90, int(float(dim))))
        except Exception:
            dim_val = 55
        try:
            blur_val = max(0, min(20, int(float(blur))))
        except Exception:
            blur_val = 4
        self._config["custom_bg_dim"] = dim_val
        self._config["custom_bg_blur"] = blur_val
        self._save_config()
        return {"ok": True, "dim": dim_val, "blur": blur_val}

    def create_instance(self, name, loader, version, icon="grass", color="#1bd96a", custom_icon_path=""):
        import uuid
        import shutil
        clean_name = (name or "").strip() or f"{loader} {version}"
        inst_id = f"{loader.lower()}_{re.sub(r'[^a-zA-Z0-9._-]', '_', version)}_{uuid.uuid4().hex[:6]}"
        inst_dir = os.path.join(INSTANCES_DIR, inst_id)
        for sub in ("mods", "resourcepacks", "shaderpacks", "datapacks"):
            os.makedirs(os.path.join(inst_dir, sub), exist_ok=True)
        custom_icon_file = ""
        if custom_icon_path and os.path.isfile(custom_icon_path) and os.path.getsize(custom_icon_path) <= 2 * 1024 * 1024:
            try:
                ext = os.path.splitext(custom_icon_path)[1].lower() or ".png"
                dst_name = f"greenprism_icon{ext}"
                shutil.copy2(custom_icon_path, os.path.join(inst_dir, dst_name))
                custom_icon_file = dst_name
            except Exception:
                custom_icon_file = ""
        self._config["instances"].append({
            "id": inst_id,
            "name": clean_name,
            "loader": loader,
            "version": version,
            "icon": icon or "grass",
            "color": color or "#1bd96a",
            "playtime_sec": 0,
            "last_played": "",
            "cover_file": "",
            "custom_icon_file": custom_icon_file,
            "installed_projects": {},
            "server_address": "",
            "ram_mb": 0,
            "java_path": "",
            "jvm_args": "",
            "win_width": 0,
            "win_height": 0,
        })
        self._save_config()
        return self.get_state()

    def update_instance_appearance(self, inst_id, name, icon, color, server_address=None, ram_mb=None, java_path=None, jvm_args=None, win_width=None, win_height=None):
        inst = self._find_instance(inst_id)
        if inst:
            clean_name = (name or "").strip()
            if clean_name:
                inst["name"] = clean_name
            if icon:
                inst["icon"] = icon
            if color:
                inst["color"] = color
            if server_address is not None:
                inst["server_address"] = str(server_address).strip()
            if ram_mb is not None:
                try:
                    rv = int(ram_mb)
                except Exception:
                    rv = 0
                inst["ram_mb"] = 0 if rv <= 0 else max(1024, min(16384, round(rv / 128) * 128))
            if java_path is not None:
                inst["java_path"] = str(java_path).strip().strip('"')
            if jvm_args is not None:
                inst["jvm_args"] = str(jvm_args).strip()
            if win_width is not None:
                try:
                    w = int(win_width)
                except Exception:
                    w = 0
                inst["win_width"] = max(0, min(7680, w))
            if win_height is not None:
                try:
                    h = int(win_height)
                except Exception:
                    h = 0
                inst["win_height"] = max(0, min(4320, h))
            self._save_config()
        return self.get_state()

    def get_loader_versions(self, loader, mc_ver):
        """Возвращает список версий выбранного загрузчика под конкретную версию Minecraft (стабильные сверху)."""
        ldr = str(loader or "").strip()
        ver = str(mc_ver or "").strip()
        if ldr == "Vanilla" or not ver:
            return {"ok": True, "versions": []}
        if ldr not in ("Fabric", "Quilt", "Forge", "NeoForge"):
            return {"ok": False, "error": f"Неизвестный загрузчик: {ldr}", "versions": []}

        cache_key = (ldr, ver)
        cached = self._loader_versions_cache.get(cache_key)
        if cached is not None:
            return {"ok": True, "versions": cached}

        items = []
        try:
            if ldr == "Fabric":
                r = _get_http().get(
                    f"https://meta.fabricmc.net/v2/versions/loader/{ver}",
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                if r.status_code == 200:
                    seen = set()
                    for entry in (r.json() or []):
                        lo = entry.get("loader") or {}
                        lv = str(lo.get("version") or "").strip()
                        if lv and lv not in seen:
                            seen.add(lv)
                            items.append({"id": lv, "stable": bool(lo.get("stable"))})
            elif ldr == "Quilt":
                r = _get_http().get(
                    f"https://meta.quiltmc.org/v3/versions/loader/{ver}",
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                if r.status_code == 200:
                    seen = set()
                    for entry in (r.json() or []):
                        lo = entry.get("loader") or {}
                        lv = str(lo.get("version") or "").strip()
                        if lv and lv not in seen:
                            seen.add(lv)
                            is_st = not bool(re.search(r"(beta|alpha|pre|rc)", lv, re.I))
                            items.append({"id": lv, "stable": is_st})
            elif ldr == "Forge":
                all_builds = _get_mclib().forge.list_forge_versions() or []
                seen = set()
                for fv in all_builds:
                    if fv.split("-")[0] == ver:
                        b = fv.split("-", 1)[1] if "-" in fv else fv
                        if b and b not in seen:
                            seen.add(b)
                            items.append({"id": b, "stable": True})
            elif ldr == "NeoForge":
                builds = self._list_neoforge_builds()
                if not builds:
                    builds = self._list_neoforge_builds(force=True)
                matching = [b for b in builds if _neoforge_ver_to_mc(b) == ver]
                if not matching:
                    prefixes = _mc_to_neoforge_prefixes(ver)
                    matching = [b for b in builds if any(b.startswith(p) for p in prefixes)]
                seen = set()
                for b in matching:
                    if b and b not in seen:
                        seen.add(b)
                        is_st = ("beta" not in b.lower()) and ("alpha" not in b.lower())
                        items.append({"id": b, "stable": is_st})
        except Exception as e:
            return {"ok": False, "error": str(e), "versions": []}

        # Сортируем: сначала стабильные (от новых к старым), затем бета/пререлизы (от новых к старым)
        stable_list = sorted([x for x in items if x["stable"]], key=lambda x: _parse_ver_tuple(x["id"]), reverse=True)
        pre_list = sorted([x for x in items if not x["stable"]], key=lambda x: _parse_ver_tuple(x["id"]), reverse=True)
        ordered = (stable_list + pre_list)[:60]
        if ordered:
            self._loader_versions_cache[cache_key] = ordered
        return {"ok": True, "versions": ordered}

    def change_instance_loader(self, inst_id, new_loader, new_loader_version="", disable_mods=False, install_now=True):
        """Меняет загрузчик и его версию для существующей сборки без затрагивания миров, конфигов и ресурспаков."""
        if self._is_busy:
            return {"ok": False, "error": "Дождитесь завершения текущей операции"}
        inst = self._find_instance(inst_id)
        if not inst:
            return {"ok": False, "error": "Сборка не найдена"}
        if self._running_inst_id == inst["id"]:
            return {"ok": False, "error": "Нельзя менять загрузчик, пока сборка запущена"}

        ldr = str(new_loader or "").strip()
        if ldr not in ("Vanilla", "Fabric", "Quilt", "Forge", "NeoForge"):
            return {"ok": False, "error": f"Неподдерживаемый загрузчик: {ldr}"}

        lv = "" if ldr == "Vanilla" else str(new_loader_version or "").strip()
        mc_ver = str(inst.get("version") or "").strip()
        old_loader = inst.get("loader", "Vanilla")
        old_lv = str(inst.get("loader_version") or "")
        old_vid = str(inst.get("installed_version_id") or "")

        def _disable_active_mods():
            cnt = 0
            if not (disable_mods and ldr != old_loader):
                return 0
            try:
                inst_dir = _safe_join(INSTANCES_DIR, inst["id"])
                mods_dir = _safe_join(inst_dir, "mods")
                if os.path.isdir(mods_dir):
                    for fn in list(os.listdir(mods_dir)):
                        if not fn.lower().endswith(".jar"):
                            continue
                        src = _safe_join(mods_dir, fn)
                        if not os.path.isfile(src):
                            continue
                        dst_name = fn + ".disabled"
                        dst = _safe_join(mods_dir, dst_name)
                        if os.path.exists(dst):
                            base, ext = os.path.splitext(fn)
                            dst_name = f"{base}_{int(time.time())}{ext}.disabled"
                            dst = _safe_join(mods_dir, dst_name)
                        try:
                            os.rename(src, dst)
                            cnt += 1
                        except Exception:
                            pass
            except Exception:
                pass
            return cnt

        inst["loader"] = ldr
        inst["loader_version"] = lv
        inst["installed_version_id"] = ""

        if not install_now:
            disabled_count = _disable_active_mods()
            self._save_config()
            return {"ok": True, "disabled_mods": disabled_count, "state": self.get_state()}

        mc_dir = self._get_mc_dir()
        os.makedirs(mc_dir, exist_ok=True)

        # Проверяем, установлен ли уже точный профиль нужного загрузчика/версии
        expected_vid = ""
        if ldr == "Vanilla":
            expected_vid = mc_ver
        elif ldr == "Fabric" and lv:
            expected_vid = f"fabric-loader-{lv}-{mc_ver}"
        elif ldr == "Quilt" and lv:
            expected_vid = f"quilt-loader-{lv}-{mc_ver}"
        elif ldr == "NeoForge" and lv:
            expected_vid = f"neoforge-{lv}"
        elif ldr == "Forge" and lv:
            expected_vid = f"{mc_ver}-forge-{lv}"

        installed_ids = self._get_installed_ids(force=True)
        if expected_vid and expected_vid in installed_ids and self._is_version_complete(mc_dir, expected_vid, ldr, mc_ver=mc_ver):
            disabled_count = _disable_active_mods()
            inst["installed_version_id"] = expected_vid
            self._save_config()
            self._set_status(f"Загрузчик обновлён: {ldr} {lv}".strip(), 1.0, False)
            return {
                "ok": True,
                "installed_version_id": expected_vid,
                "disabled_mods": disabled_count,
                "state": self.get_state(),
            }

        self._is_busy = True
        try:
            callback = self._make_install_callback()
            pinned_lv = lv or None
            if ldr == "Fabric":
                self._set_status(f"Установка Fabric {lv or mc_ver}...", 0.08, True)
                target_version = self._install_fabric(mc_ver, mc_dir, callback, inst["id"], loader_ver=pinned_lv)
            elif ldr == "Quilt":
                self._set_status(f"Установка Quilt {lv or mc_ver}...", 0.08, True)
                target_version = self._install_quilt(mc_ver, mc_dir, callback, inst["id"], loader_ver=pinned_lv)
            elif ldr == "Forge":
                self._set_status(f"Установка Forge {lv or mc_ver}...", 0.08, True)
                target_version = self._install_forge(mc_ver, mc_dir, callback, inst["id"], forge_ver=pinned_lv)
            elif ldr == "NeoForge":
                self._set_status(f"Установка NeoForge {lv or mc_ver}...", 0.08, True)
                target_version = self._install_neoforge(mc_ver, mc_dir, callback, inst["id"], nf_ver=pinned_lv)
            else:
                self._set_status(f"Подготовка Vanilla {mc_ver}...", 0.08, True)
                target_version = self._install_vanilla_version(mc_ver, mc_dir, callback, inst["id"])

            disabled_count = _disable_active_mods()
            self._installed_cache = (None, None, set())
            inst["installed_version_id"] = target_version
            self._save_config()
            self._set_status(f"Загрузчик установлен: {ldr} {lv}".strip(), 1.0, False)
            return {
                "ok": True,
                "installed_version_id": target_version,
                "disabled_mods": disabled_count,
                "state": self.get_state(),
            }
        except Exception as e:
            # Откатываем метаданные при ошибке установки, чтобы не сломать сборку
            inst["loader"] = old_loader
            inst["loader_version"] = old_lv
            inst["installed_version_id"] = old_vid
            self._save_config()
            self._set_status("Ошибка смены загрузчика", 0, False)
            return {"ok": False, "error": str(e)}
        finally:
            self._is_busy = False

    # ---------------- Профили и аккаунты (Оффлайн + Ely.by) ----------------
    def switch_account(self, account_id):
        accs = self._get_accounts()
        target = next((a for a in accs if a["id"] == account_id), None)
        if not target:
            return self.get_state()
        accs = [a for a in accs if a["id"] != target["id"]]
        accs.insert(0, target)
        self._config["accounts"] = accs
        self._config["active_account_id"] = target["id"]
        self._sync_profiles_legacy()
        self._save_config()
        badge = " (Ely.by)" if target.get("type") == "elyby" else ""
        self._set_status(f"Профиль: {target['username']}{badge}", 0, False)
        return self.get_state()

    def switch_profile(self, username):
        clean = (username or "").strip()
        if not clean:
            return self.get_state()
        accs = self._get_accounts()
        target = next((a for a in accs if a["username"].lower() == clean.lower()), None)
        if target:
            return self.switch_account(target["id"])
        res = self.add_profile(clean)
        return res.get("state") or self.get_state()

    def add_profile(self, username):
        clean = (username or "").strip()
        if not clean:
            return {"ok": False, "error": "Введи никнейм"}
        if len(clean) > 24:
            return {"ok": False, "error": "Никнейм слишком длинный (макс. 24 символа)"}
        accs = self._get_accounts()
        aid = f"offline:{clean.lower()}"
        existing = next((a for a in accs if a["id"] == aid), None)
        if existing:
            existing["username"] = clean
            accs = [a for a in accs if a["id"] != aid]
            accs.insert(0, existing)
        else:
            accs.insert(0, {"id": aid, "type": "offline", "username": clean, "uuid": ""})
        self._config["accounts"] = accs
        self._config["active_account_id"] = aid
        self._sync_profiles_legacy()
        self._save_config()
        return {"ok": True, "state": self.get_state()}

    def delete_account(self, account_id):
        accs = self._get_accounts()
        target = next((a for a in accs if a["id"] == account_id), None)
        if target and target.get("type") == "elyby" and target.get("access_token"):
            # Аккуратно отзываем токен Ely.by в фоне (не блокируем UI при ошибках сети)
            try:
                _get_http().post(
                    "https://authserver.ely.by/auth/invalidate",
                    json={
                        "accessToken": target.get("access_token", ""),
                        "clientToken": target.get("client_token", ""),
                    },
                    headers={"User-Agent": USER_AGENT},
                    timeout=4,
                )
            except Exception:
                pass
        accs = [a for a in accs if a["id"] != account_id]
        if not accs:
            accs = [{"id": "offline:player", "type": "offline", "username": "Player", "uuid": ""}]
        self._config["accounts"] = accs
        if self._config.get("active_account_id") == account_id:
            self._config["active_account_id"] = accs[0]["id"]
        self._sync_profiles_legacy()
        self._save_config()
        return self.get_state()

    def delete_profile(self, username):
        clean = (username or "").strip()
        accs = self._get_accounts()
        target = next((a for a in accs if a["username"].lower() == clean.lower()), None)
        if target:
            return self.delete_account(target["id"])
        return self.get_state()

    def elyby_login(self, login, password, totp_code=""):
        """Вход в аккаунт Ely.by через официальный Yggdrasil API (с поддержкой 2FA)."""
        import uuid
        clean_login = (login or "").strip()
        raw_pass = str(password or "")
        clean_totp = re.sub(r"\s+", "", str(totp_code or ""))
        if not clean_login or not raw_pass:
            return {"ok": False, "error": "Введи логин/email и пароль от Ely.by"}

        client_token = uuid.uuid4().hex
        full_pass = f"{raw_pass}:{clean_totp}" if clean_totp else raw_pass
        payload = {
            "username": clean_login,
            "password": full_pass,
            "clientToken": client_token,
            "requestUser": True,
        }
        try:
            r = _get_http().post(
                "https://authserver.ely.by/auth/authenticate",
                json=payload,
                headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
                timeout=15,
            )
            data = {}
            try:
                data = r.json() or {}
            except Exception:
                data = {}

            if r.status_code != 200:
                err_msg = str(data.get("errorMessage") or data.get("error") or f"HTTP {r.status_code}")
                low = err_msg.lower()
                if "two factor" in low or "totp" in low or "2fa" in low:
                    return {
                        "ok": False,
                        "need_2fa": True,
                        "error": "Аккаунт защищён 2FA. Введи 6-значный код из приложения.",
                    }
                if "invalid credentials" in low or r.status_code in (401, 403):
                    return {"ok": False, "error": "Неверный логин, пароль или код 2FA."}
                return {"ok": False, "error": f"Ely.by: {err_msg}"}

            access_token = str(data.get("accessToken") or "").strip()
            resp_client = str(data.get("clientToken") or client_token).strip()
            sel = data.get("selectedProfile") or {}
            uname = str(sel.get("name") or "").strip()
            uid = _format_mc_uuid(sel.get("id") or "")
            if not access_token or not uname or not uid:
                return {"ok": False, "error": "Ely.by вернул неполный ответ авторизации"}

            aid = f"elyby:{uid}"
            accs = [a for a in self._get_accounts() if a["id"] != aid]
            accs.insert(0, {
                "id": aid,
                "type": "elyby",
                "username": uname,
                "uuid": uid,
                "access_token": access_token,
                "client_token": resp_client,
            })
            self._config["accounts"] = accs
            self._config["active_account_id"] = aid
            self._sync_profiles_legacy()
            self._save_config()

            # Сбрасываем кэш скина для этого ника, чтобы сразу подтянулся скин Ely.by
            for k in list(self._skin_cache.keys()):
                if k == uname or (isinstance(k, tuple) and k[1] == uname.lower()):
                    self._skin_cache.pop(k, None)

            self._set_status(f"Вход выполнен: {uname} (Ely.by)", 0, False)
            return {"ok": True, "username": uname, "state": self.get_state()}
        except Exception as e:
            return {"ok": False, "error": f"Ошибка подключения к Ely.by: {e}"}

    def _refresh_elyby_account(self, acc, inst_id=None):
        """Проверяет и при необходимости обновляет токен Ely.by перед запуском игры."""
        access_token = str(acc.get("access_token") or "").strip()
        client_token = str(acc.get("client_token") or "").strip()
        if not access_token:
            raise RuntimeError("Сессия Ely.by пуста. Войди заново в окне «Профили и 3D-скин».")

        try:
            rv = _get_http().post(
                "https://authserver.ely.by/auth/validate",
                json={"accessToken": access_token, "clientToken": client_token},
                headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
                timeout=8,
            )
            if rv.status_code == 204:
                return acc
        except Exception as net_err:
            if inst_id:
                self._push_log(inst_id, f"[Ely.by] Не удалось проверить токен ({net_err}), запускаю с кэшированной сессией...")
            return acc

        # Токен не прошёл validate — пробуем refresh
        try:
            rr = _get_http().post(
                "https://authserver.ely.by/auth/refresh",
                json={"accessToken": access_token, "clientToken": client_token, "requestUser": True},
                headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
                timeout=10,
            )
            if rr.status_code == 200:
                rdata = rr.json() or {}
                new_token = str(rdata.get("accessToken") or "").strip()
                if new_token:
                    acc["access_token"] = new_token
                if rdata.get("clientToken"):
                    acc["client_token"] = str(rdata["clientToken"]).strip()
                sel = rdata.get("selectedProfile") or {}
                if sel.get("name"):
                    acc["username"] = str(sel["name"]).strip()
                if sel.get("id"):
                    acc["uuid"] = _format_mc_uuid(sel["id"])
                self._sync_profiles_legacy()
                self._save_config()
                if inst_id:
                    self._push_log(inst_id, f"[Ely.by] Токен сессии обновлён ({acc['username']}).")
                return acc
            if rr.status_code in (401, 403):
                raise RuntimeError("Сессия Ely.by истекла. Открой «Профили» и войди в Ely.by заново.")
        except RuntimeError:
            raise
        except Exception as ref_err:
            if inst_id:
                self._push_log(inst_id, f"[Ely.by] Ошибка обновления токена ({ref_err}), пробую запуск с текущим токеном...")
        return acc

    def _ensure_authlib_injector(self, inst_id=None):
        """Гарантирует наличие authlib-injector.jar для работы скинов и авторизации Ely.by."""
        if os.path.isfile(AUTHLIB_INJECTOR_PATH) and os.path.getsize(AUTHLIB_INJECTOR_PATH) > 100 * 1024:
            return AUTHLIB_INJECTOR_PATH

        self._set_status("Загрузка authlib-injector (Ely.by)...", 0.88, True)
        if inst_id:
            self._push_log(inst_id, "[Ely.by] Скачиваю authlib-injector.jar для поддержки скинов и авторизации...")

        dl_urls = []
        meta_endpoints = [
            "https://authlib-injector.yushi.moe/artifact/latest.json",
            "https://bmclapi2.bangbang93.com/mirrors/authlib-injector/artifact/latest.json",
        ]
        for murl in meta_endpoints:
            try:
                rm = _get_http().get(murl, headers={"User-Agent": USER_AGENT}, timeout=10)
                if rm.status_code == 200:
                    mdata = rm.json() or {}
                    du = str(mdata.get("download_url") or "").strip()
                    if du and du not in dl_urls:
                        dl_urls.append(du)
            except Exception:
                continue
        dl_urls.append("https://authlib-injector.yushi.moe/artifact/53/authlib-injector-1.2.5.jar")

        tmp_path = AUTHLIB_INJECTOR_PATH + ".tmp"
        last_err = None
        for du in dl_urls:
            try:
                self._download_file(du, tmp_path)
                is_valid = False
                if os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 100 * 1024:
                    try:
                        with open(tmp_path, "rb") as f:
                            is_valid = (f.read(2) == b"PK")
                    except Exception:
                        is_valid = False

                if is_valid:
                    try:
                        os.replace(tmp_path, AUTHLIB_INJECTOR_PATH)
                    except (PermissionError, OSError) as _replace_err:
                        if os.path.isfile(AUTHLIB_INJECTOR_PATH) and os.path.getsize(AUTHLIB_INJECTOR_PATH) > 0:
                            if inst_id:
                                self._push_log(inst_id, f"[Ely.by] Файл занят ({_replace_err}), использую существующий authlib-injector.jar")
                            try:
                                if os.path.exists(tmp_path):
                                    os.remove(tmp_path)
                            except Exception:
                                pass
                            return AUTHLIB_INJECTOR_PATH
                        raise _replace_err

                    if inst_id:
                        self._push_log(inst_id, f"[Ely.by] authlib-injector готов: {AUTHLIB_INJECTOR_PATH}")
                    return AUTHLIB_INJECTOR_PATH
            except Exception as e:
                last_err = e
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        if os.path.isfile(AUTHLIB_INJECTOR_PATH) and os.path.getsize(AUTHLIB_INJECTOR_PATH) > 0:
            if inst_id:
                self._push_log(inst_id, "[Ely.by] Не удалось обновить authlib-injector, использую имеющийся файл.")
            return AUTHLIB_INJECTOR_PATH

        raise RuntimeError(f"Не удалось скачать authlib-injector для Ely.by: {last_err}")

    # ---------------- Клонирование сборки ----------------
    def duplicate_instance(self, inst_id):
        import uuid
        import shutil
        src = self._find_instance(inst_id)
        if not src:
            return {"ok": False, "error": "Сборка не найдена"}
        try:
            new_id = f"{src['loader'].lower()}_{re.sub(r'[^a-zA-Z0-9._-]', '_', src['version'])}_{uuid.uuid4().hex[:6]}"
            src_dir = os.path.join(INSTANCES_DIR, src["id"])
            dst_dir = os.path.join(INSTANCES_DIR, new_id)
            self._set_status(f"Клонирование «{src['name']}»...", 0.4, True)
            if os.path.exists(src_dir):
                shutil.copytree(
                    src_dir,
                    dst_dir,
                    ignore=shutil.ignore_patterns("backups", "logs", "crash-reports"),
                )
            else:
                for sub in ("mods", "resourcepacks", "shaderpacks", "datapacks"):
                    os.makedirs(os.path.join(dst_dir, sub), exist_ok=True)

            clone = dict(src)
            clone["id"] = new_id
            clone["name"] = f"{src['name']} (копия)"
            clone["playtime_sec"] = 0
            clone["last_played"] = ""
            clone["installed_projects"] = dict(src.get("installed_projects") or {})
            self._config["instances"].append(clone)
            self._save_config()
            self._set_status(f"Создана копия: {clone['name']}", 1.0, False)
            return {"ok": True, "new_id": new_id, "state": self.get_state()}
        except Exception as e:
            self._set_status("Ошибка клонирования", 0, False)
            return {"ok": False, "error": str(e)}

    # ---------------- Проверка и обновление модов в 1 клик (Modrinth SHA-1) ----------------
    def check_mod_updates(self, inst_id):
        inst = self._find_instance(inst_id)
        if not inst or inst["loader"] == "Vanilla":
            return {"ok": False, "error": "Обновление модов недоступно для Vanilla"}

        mods_dir = os.path.join(INSTANCES_DIR, inst["id"], "mods")
        if not os.path.exists(mods_dir):
            return {"ok": True, "updates": {}, "checked": 0}

        hash_to_file = {}
        for fname in os.listdir(mods_dir):
            if fname.endswith(".jar"):
                full = os.path.join(mods_dir, fname)
                if os.path.isfile(full):
                    try:
                        h = _file_sha1(full)
                        hash_to_file[h] = fname
                    except Exception:
                        pass

        if not hash_to_file:
            return {"ok": True, "updates": {}, "checked": 0}

        try:
            self._set_status(f"Проверка обновлений для {len(hash_to_file)} модов...", 0.35, True)
            loader_low = inst["loader"].lower()
            query_loaders = ["quilt", "fabric"] if loader_low == "quilt" else [loader_low]
            r = _get_http().post(
                "https://api.modrinth.com/v2/version_files/update",
                json={
                    "hashes": list(hash_to_file.keys()),
                    "algorithm": "sha1",
                    "loaders": query_loaders,
                    "game_versions": [inst["version"]],
                },
                headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json() or {}

            updates = {}
            for old_hash, ver_obj in data.items():
                old_fname = hash_to_file.get(old_hash)
                if not old_fname or not isinstance(ver_obj, dict):
                    continue
                files = ver_obj.get("files") or []
                if not files:
                    continue
                primary = next((f for f in files if f.get("primary")), files[0])
                new_hash = (primary.get("hashes") or {}).get("sha1", "")
                new_url = primary.get("url", "")
                new_fname = primary.get("filename", "")
                if new_url and new_fname and new_hash and new_hash.lower() != old_hash.lower():
                    updates[old_fname] = {
                        "old_filename": old_fname,
                        "new_filename": new_fname,
                        "new_version": ver_obj.get("version_number") or "новая",
                        "download_url": new_url,
                        "project_id": ver_obj.get("project_id") or "",
                    }

            if updates:
                self._set_status(f"Найдено обновлений модов: {len(updates)}", 1.0, False)
            else:
                self._set_status("Все моды уже актуальной версии", 1.0, False)
            return {"ok": True, "updates": updates, "checked": len(hash_to_file)}
        except Exception as e:
            self._set_status("Ошибка проверки обновлений", 0, False)
            return {"ok": False, "error": str(e)}

    def apply_mod_updates(self, inst_id, updates_list):
        inst = self._find_instance(inst_id)
        if not inst:
            return {"ok": False, "error": "Сборка не найдена"}
        mods_dir = os.path.join(INSTANCES_DIR, inst["id"], "mods")
        os.makedirs(mods_dir, exist_ok=True)

        total = len(updates_list or [])
        updated = 0
        errors = []
        for idx, upd in enumerate(updates_list or [], start=1):
            try:
                old_fn = upd["old_filename"]
                new_fn = upd["new_filename"]
                url = upd["download_url"]
                self._set_status(f"Обновление ({idx}/{total}): {new_fn}...", idx / max(total, 1), True)

                tmp_path = os.path.join(mods_dir, new_fn + ".tmp")
                final_path = os.path.join(mods_dir, new_fn)
                self._download_file(url, tmp_path)

                old_path = os.path.join(mods_dir, old_fn)
                if os.path.exists(old_path) and os.path.abspath(old_path) != os.path.abspath(final_path):
                    os.remove(old_path)
                os.replace(tmp_path, final_path)

                if upd.get("project_id"):
                    inst.setdefault("installed_projects", {})[f"Modrinth:{upd['project_id']}"] = new_fn
                updated += 1
            except Exception as e:
                errors.append(f"{upd.get('old_filename', '?')}: {e}")

        self._save_config()
        self._set_status(f"Обновлено модов: {updated}", 1.0, False)
        return {"ok": True, "updated": updated, "errors": errors}

    # ---------------- Миры (saves/) и бэкапы в .zip ----------------
    def get_instance_worlds(self, inst_id):
        from datetime import datetime
        saves_dir = os.path.join(INSTANCES_DIR, inst_id, "saves")
        if not os.path.exists(saves_dir):
            return []
        worlds = []
        for entry in sorted(os.listdir(saves_dir), key=str.lower):
            full = os.path.join(saves_dir, entry)
            if not os.path.isdir(full):
                continue
            total_bytes = 0
            latest_mtime = os.path.getmtime(full)
            for root, _dirs, files in os.walk(full):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        st = os.stat(fp)
                        total_bytes += st.st_size
                        if st.st_mtime > latest_mtime:
                            latest_mtime = st.st_mtime
                    except Exception:
                        pass
            icon_uri = ""
            icon_path = os.path.join(full, "icon.png")
            if os.path.exists(icon_path):
                try:
                    with open(icon_path, "rb") as f:
                        icon_uri = _image_bytes_to_data_uri(f.read())
                except Exception:
                    pass
            worlds.append({
                "folder": entry,
                "size_mb": round(total_bytes / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(latest_mtime).strftime("%d.%m.%Y %H:%M"),
                "mtime": latest_mtime,
                "icon": icon_uri,
            })
        worlds.sort(key=lambda w: w["mtime"], reverse=True)
        return worlds

    def backup_world(self, inst_id, world_folder):
        import zipfile
        from datetime import datetime
        try:
            saves_dir = os.path.join(INSTANCES_DIR, inst_id, "saves")
            world_path = _safe_join(saves_dir, world_folder)
            if not os.path.isdir(world_path):
                return {"ok": False, "error": "Папка мира не найдена"}

            backups_dir = os.path.join(INSTANCES_DIR, inst_id, "backups")
            os.makedirs(backups_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            safe_name = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ._-]", "_", world_folder)
            zip_name = f"{safe_name}_{stamp}.zip"
            zip_path = os.path.join(backups_dir, zip_name)

            self._set_status(f"Архивация мира «{world_folder}»...", 0.5, True)
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _dirs, files in os.walk(world_path):
                    for fn in files:
                        fp = os.path.join(root, fn)
                        rel = os.path.relpath(fp, saves_dir)
                        try:
                            zf.write(fp, rel)
                        except Exception:
                            pass

            size_mb = round(os.path.getsize(zip_path) / (1024 * 1024), 2)
            self._set_status(f"Бэкап создан: {zip_name} ({size_mb} МБ)", 1.0, False)
            return {"ok": True, "filename": zip_name, "size_mb": size_mb}
        except Exception as e:
            self._set_status("Ошибка создания бэкапа", 0, False)
            return {"ok": False, "error": str(e)}

    def delete_world(self, inst_id, world_folder):
        import shutil
        try:
            inst_dir = _safe_join(INSTANCES_DIR, inst_id)
            saves_dir = os.path.join(inst_dir, "saves")
            world_path = _safe_join(saves_dir, world_folder)
            if os.path.isdir(world_path):
                shutil.rmtree(world_path)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- Галерея скриншотов (screenshots/) ----------------
    def get_instance_screenshots(self, inst_id, offset=0, limit=24):
        from datetime import datetime
        shot_dir = os.path.join(INSTANCES_DIR, inst_id, "screenshots")
        if not os.path.exists(shot_dir):
            return {"items": [], "total": 0, "has_more": False}
        entries = []
        with os.scandir(shot_dir) as it:
            for de in it:
                if de.is_file() and de.name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    try:
                        st = de.stat()
                        entries.append((st.st_mtime, st.st_size, de.name, de.path))
                    except Exception:
                        pass
        entries.sort(key=lambda x: x[0], reverse=True)
        total = len(entries)
        offset = max(0, int(offset or 0))
        limit = max(1, min(48, int(limit or 24)))
        chunk = entries[offset : offset + limit]
        items = []
        for mtime, size, fn, fp in chunk:
            try:
                with open(fp, "rb") as f:
                    uri = _image_bytes_to_data_uri(f.read(), max_bytes=8 * 1024 * 1024)
                if uri:
                    items.append({
                        "filename": fn,
                        "date": datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M"),
                        "size_kb": round(size / 1024),
                        "uri": uri,
                    })
            except Exception:
                pass
        return {
            "items": items,
            "total": total,
            "next_offset": offset + len(chunk),
            "has_more": offset + len(chunk) < total,
        }

    def delete_screenshot(self, inst_id, filename):
        try:
            shot_dir = os.path.join(INSTANCES_DIR, inst_id, "screenshots")
            fp = _safe_join(shot_dir, filename)
            if os.path.isfile(fp):
                os.remove(fp)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def copy_screenshot_to_clipboard(self, inst_id, filename):
        try:
            shot_dir = os.path.join(INSTANCES_DIR, inst_id, "screenshots")
            fp = _safe_join(shot_dir, filename)
            if not os.path.isfile(fp):
                return {"ok": False, "error": "Файл скриншота не найден"}
            if _copy_image_to_clipboard(fp):
                return {"ok": True}
            return {"ok": False, "error": "Не удалось поместить изображение в буфер обмена"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- Модпаки (Modrinth .mrpack поиск + установка + локальный импорт) ----------------
    def search_modpacks(self, query="", loader_filter="all", offset=0, sort="downloads"):
        offset = max(0, int(offset or 0))
        limit = 20
        clean_query = (query or "").strip()
        try:
            facets = [["project_type:modpack"]]
            if loader_filter in ("fabric", "forge", "neoforge", "quilt"):
                facets.append([f"categories:{loader_filter}"])
            else:
                # Показываем модпаки, совместимые с поддерживаемыми загрузчиками (Fabric / Quilt / Forge / NeoForge)
                facets.append(["categories:fabric", "categories:quilt", "categories:forge", "categories:neoforge"])

            mr_index = sort if sort in ("relevance", "downloads", "updated", "newest", "follows") else "downloads"
            if mr_index == "relevance" and not clean_query:
                mr_index = "downloads"

            r = _get_http().get(
                "https://api.modrinth.com/v2/search",
                params={
                    "query": clean_query,
                    "limit": limit,
                    "offset": offset,
                    "index": mr_index,
                    "facets": json.dumps(facets),
                },
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            r.raise_for_status()
            payload = r.json()
            hits = payload.get("hits") or []
            total = int(payload.get("total_hits", 0))
            items = []
            for h in hits:
                cats = [c.lower() for c in (h.get("categories") or [])]
                loader_name_map = {"fabric": "Fabric", "quilt": "Quilt", "forge": "Forge", "neoforge": "NeoForge"}
                loaders = [loader_name_map[x] for x in ("fabric", "quilt", "forge", "neoforge") if x in cats]
                items.append({
                    "id": h["project_id"],
                    "slug": h.get("slug", ""),
                    "title": h.get("title", "Без названия"),
                    "author": h.get("author", "unknown"),
                    "description": h.get("description", ""),
                    "downloads": h.get("downloads", 0),
                    "icon_url": h.get("icon_url") or "",
                    "loaders": loaders,
                    "latest_version": h.get("latest_version", ""),
                    "web_url": f"https://modrinth.com/modpack/{h.get('slug', h['project_id'])}",
                })
            next_offset = offset + len(items)
            return {
                "ok": True,
                "items": items,
                "total": total,
                "next_offset": next_offset,
                "has_more": len(items) > 0 and next_offset < total,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _install_mrpack_archive(self, mrpack_path, fallback_name="Modpack", icon_url=""):
        import uuid
        import shutil
        import zipfile
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with zipfile.ZipFile(mrpack_path, "r") as zf:
            if "modrinth.index.json" not in zf.namelist():
                return {"ok": False, "error": "Файл не является корректным .mrpack (нет modrinth.index.json)"}
            index = json.loads(zf.read("modrinth.index.json").decode("utf-8", errors="ignore"))

            deps = index.get("dependencies") or {}
            mc_ver = str(deps.get("minecraft") or "").strip()
            if not mc_ver:
                return {"ok": False, "error": "В модпаке не указана версия Minecraft"}

            loader_version = ""
            if "fabric-loader" in deps:
                loader = "Fabric"
                loader_version = str(deps.get("fabric-loader") or "").strip()
                icon_key, color = "bolt", "#1bd96a"
            elif "forge" in deps:
                loader = "Forge"
                loader_version = str(deps.get("forge") or "").strip()
                icon_key, color = "anvil", "#f16436"
            elif "neoforge" in deps:
                loader = "NeoForge"
                loader_version = str(deps.get("neoforge") or "").strip()
                icon_key, color = "flame", "#f97316"
            elif "quilt-loader" in deps:
                loader = "Quilt"
                loader_version = str(deps.get("quilt-loader") or "").strip()
                icon_key, color = "diamond", "#8b5cf6"
            else:
                loader = "Vanilla"
                icon_key, color = "grass", "#22d3ee"

            pack_name = str(index.get("name") or fallback_name).strip() or f"{loader} {mc_ver}"
            inst_id = f"{loader.lower()}_{re.sub(r'[^a-zA-Z0-9._-]', '_', mc_ver)}_{uuid.uuid4().hex[:6]}"
            inst_dir = os.path.join(INSTANCES_DIR, inst_id)
            for sub in ("mods", "resourcepacks", "shaderpacks", "datapacks"):
                os.makedirs(os.path.join(inst_dir, sub), exist_ok=True)

            # Фильтруем только клиентские файлы
            files_to_dl = []
            for fentry in index.get("files") or []:
                env = fentry.get("env") or {}
                if env.get("client") == "unsupported":
                    continue
                downloads = fentry.get("downloads") or []
                rel_path = fentry.get("path") or ""
                hashes = fentry.get("hashes") or {}
                expected_sha1 = str(hashes.get("sha1") or "").strip().lower()
                if downloads and rel_path:
                    files_to_dl.append((rel_path, downloads, expected_sha1))

            total = len(files_to_dl)
            done = 0
            failed = 0
            lock = threading.Lock()

            def _dl_one(entry):
                rel_path, urls, expected_sha1 = entry
                try:
                    dst = _safe_join(inst_dir, rel_path)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                except Exception:
                    return False
                for u in urls:
                    try:
                        self._download_file(u, dst, emit_telemetry=False, expected_sha1=expected_sha1)
                        return True
                    except Exception:
                        pass
                return False

            if total > 0:
                self._set_status(f"Скачивание модпака «{pack_name}» (0/{total})...", 0.1, True)
                with ThreadPoolExecutor(max_workers=6) as pool:
                    futures = [pool.submit(_dl_one, item) for item in files_to_dl]
                    for fut in as_completed(futures):
                        ok = fut.result()
                        with lock:
                            done += 1
                            if not ok:
                                failed += 1
                            if done % 3 == 0 or done == total:
                                frac = 0.1 + 0.78 * (done / total)
                                self._set_status(f"Модпак «{pack_name}»: {done}/{total} файлов...", frac, True)

            # Распаковка overrides/, затем client-overrides/ поверх (по спецификации .mrpack)
            self._set_status(f"Распаковка конфигов «{pack_name}»...", 0.92, True)
            for prefix in ("overrides/", "client-overrides/"):
                for member in zf.infolist():
                    if member.is_dir() or not member.filename.startswith(prefix):
                        continue
                    rel = member.filename[len(prefix):]
                    if not rel:
                        continue
                    try:
                        dst = _safe_join(inst_dir, rel)
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        with zf.open(member, "r") as src_f, open(dst, "wb") as out_f:
                            shutil.copyfileobj(src_f, out_f)
                    except Exception:
                        pass

            # Если передана иконка модпака — сохраняем её как обложку сборки
            cover_file = ""
            if icon_url:
                try:
                    r_ic = _get_http().get(icon_url, headers={"User-Agent": USER_AGENT}, timeout=10)
                    if r_ic.status_code == 200 and len(r_ic.content) < 2 * 1024 * 1024:
                        ext = ".png"
                        if r_ic.content.startswith(b"\xff\xd8\xff"):
                            ext = ".jpg"
                        elif r_ic.content.startswith(b"RIFF") and r_ic.content[8:12] == b"WEBP":
                            ext = ".webp"
                        cover_file = f"greenprism_cover{ext}"
                        with open(os.path.join(inst_dir, cover_file), "wb") as cf:
                            cf.write(r_ic.content)
                except Exception:
                    cover_file = ""

            self._config["instances"].append({
                "id": inst_id,
                "name": pack_name,
                "loader": loader,
                "loader_version": loader_version,
                "version": mc_ver,
                "icon": icon_key,
                "color": color,
                "playtime_sec": 0,
                "last_played": "",
                "cover_file": cover_file,
                "custom_icon_file": "",
                "installed_projects": {},
                "server_address": "",
                "ram_mb": 0,
                "java_path": "",
                "jvm_args": "",
                "win_width": 0,
                "win_height": 0,
            })
            self._save_config()

            msg = f"Модпак «{pack_name}» установлен ({done - failed}/{total} файлов)"
            self._set_status(msg, 1.0, False)
            return {"ok": True, "inst_id": inst_id, "name": pack_name, "failed": failed, "state": self.get_state()}

    def get_modpack_details(self, pack):
        """Загружает полную страницу модпака на Modrinth: описание, галерею и список версий."""
        pid = pack.get("id") or pack.get("slug") or ""
        if not pid:
            return {"ok": False, "error": "Не указан ID модпака"}
        try:
            r = _get_http().get(
                f"https://api.modrinth.com/v2/project/{pid}",
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            r.raise_for_status()
            p = r.json()

            gallery = []
            for g in sorted(p.get("gallery") or [], key=lambda x: (not x.get("featured", False), x.get("ordering", 0))):
                if g.get("url") or g.get("raw_url"):
                    gallery.append({
                        "url": g.get("url") or g.get("raw_url"),
                        "raw_url": g.get("raw_url") or g.get("url"),
                        "title": g.get("title") or "",
                        "description": g.get("description") or "",
                    })

            versions = []
            try:
                rv = _get_http().get(
                    f"https://api.modrinth.com/v2/project/{pid}/version",
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                if rv.status_code == 200:
                    loader_map = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}
                    for v in (rv.json() or [])[:45]:
                        files = v.get("files") or []
                        if not files:
                            continue
                        primary = next((f for f in files if f.get("primary")), files[0])
                        raw_loaders = [str(x).lower() for x in (v.get("loaders") or [])]
                        nice_loaders = [loader_map.get(x, x.capitalize()) for x in raw_loaders]
                        supported = any(x in ("fabric", "forge", "neoforge", "quilt") for x in raw_loaders) or not raw_loaders
                        dp = str(v.get("date_published") or "")[:10]
                        if len(dp) == 10 and dp[4] == "-":
                            dp = f"{dp[8:10]}.{dp[5:7]}.{dp[0:4]}"
                        versions.append({
                            "id": v.get("id", ""),
                            "name": v.get("name") or v.get("version_number") or "Версия",
                            "version_number": v.get("version_number") or "",
                            "channel": (v.get("version_type") or "release").lower(),
                            "date": dp,
                            "downloads": int(v.get("downloads") or 0),
                            "game_versions": list(v.get("game_versions") or [])[:6],
                            "loaders": nice_loaders,
                            "supported": supported,
                            "changelog": (v.get("changelog") or "").strip(),
                            "file_url": primary.get("url", ""),
                            "filename": primary.get("filename", ""),
                            "file_size": int(primary.get("size") or 0),
                        })
            except Exception:
                pass

            lic = p.get("license") or {}
            upd = str(p.get("updated") or "")[:10]
            if len(upd) == 10 and upd[4] == "-":
                upd = f"{upd[8:10]}.{upd[5:7]}.{upd[0:4]}"

            slug = p.get("slug") or pack.get("slug") or pid
            return {
                "ok": True,
                "id": p.get("id") or pid,
                "slug": slug,
                "title": p.get("title") or pack.get("title", "Модпак"),
                "description": p.get("description") or pack.get("description", ""),
                "body": p.get("body") or "",
                "icon_url": p.get("icon_url") or pack.get("icon_url", ""),
                "downloads": int(p.get("downloads") or pack.get("downloads", 0)),
                "followers": int(p.get("followers") or 0),
                "categories": [c for c in (p.get("categories") or []) if c],
                "license": lic.get("name") or lic.get("id") or "",
                "updated": upd,
                "gallery": gallery,
                "versions": versions,
                "links": {
                    "web": f"https://modrinth.com/modpack/{slug}",
                    "source": p.get("source_url") or "",
                    "wiki": p.get("wiki_url") or "",
                    "discord": p.get("discord_url") or "",
                },
            }
        except Exception as e:
            return {"ok": False, "error": f"Не удалось загрузить модпак: {e}"}

    def get_modpack_version_contents(self, file_url, version_id=""):
        """Читает modrinth.index.json из .mrpack и подтягивает названия/иконки модов через API Modrinth."""
        import io
        import zipfile

        cache_key = version_id or file_url
        if cache_key and cache_key in self._mrpack_contents_cache:
            return self._mrpack_contents_cache[cache_key]

        if not file_url:
            return {"ok": False, "error": "У выбранной версии нет ссылки на .mrpack"}

        try:
            buf = io.BytesIO()
            total_read = 0
            max_bytes = 25 * 1024 * 1024
            with _get_http().get(file_url, headers={"User-Agent": USER_AGENT}, timeout=25, stream=True) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total_read += len(chunk)
                    if total_read > max_bytes:
                        return {"ok": False, "error": "Архив .mrpack слишком большой (>25 МБ) для предпросмотра"}
                    buf.write(chunk)
            buf.seek(0)

            with zipfile.ZipFile(buf, "r") as zf:
                if "modrinth.index.json" not in zf.namelist():
                    return {"ok": False, "error": "Внутри архива не найден modrinth.index.json"}
                index = json.loads(zf.read("modrinth.index.json").decode("utf-8", errors="ignore"))

                def _classify_path(rel_path):
                    low = str(rel_path or "").replace("\\", "/").lstrip("/").lower()
                    if low.startswith("mods/"):
                        return "mod"
                    if low.startswith("resourcepacks/"):
                        return "resourcepack"
                    if low.startswith("shaderpacks/"):
                        return "shader"
                    if low.startswith("datapacks/"):
                        return "datapack"
                    return "other"

                raw_entries = []
                sha1_list = []
                seen_paths = set()

                for fentry in index.get("files") or []:
                    env = fentry.get("env") or {}
                    if env.get("client") == "unsupported":
                        continue
                    rel = str(fentry.get("path") or "").replace("\\", "/").lstrip("/")
                    if not rel:
                        continue
                    seen_paths.add(rel.lower())
                    ctype = _classify_path(rel)
                    if ctype == "other":
                        continue
                    hashes = fentry.get("hashes") or {}
                    sha1 = str(hashes.get("sha1") or "").strip().lower()
                    fname = os.path.basename(rel)
                    raw_entries.append({
                        "path": rel,
                        "filename": fname,
                        "type": ctype,
                        "sha1": sha1,
                        "file_size": int(fentry.get("fileSize") or 0),
                        "is_override": False,
                    })
                    if sha1:
                        sha1_list.append(sha1)

                # Также учитываем встроенные файлы в overrides/mods, overrides/resourcepacks, overrides/shaderpacks
                for zname in zf.namelist():
                    norm_z = zname.replace("\\", "/")
                    for pref in ("overrides/", "client-overrides/"):
                        if norm_z.startswith(pref) and not norm_z.endswith("/"):
                            rel = norm_z[len(pref):]
                            if rel.lower() in seen_paths:
                                continue
                            ctype = _classify_path(rel)
                            if ctype == "other":
                                continue
                            fname = os.path.basename(rel)
                            if not fname.lower().endswith((".jar", ".zip")):
                                continue
                            seen_paths.add(rel.lower())
                            raw_entries.append({
                                "path": rel,
                                "filename": fname,
                                "type": ctype,
                                "sha1": "",
                                "file_size": int(zf.getinfo(zname).file_size or 0),
                                "is_override": True,
                            })

            # Пробиваем SHA-1 через Modrinth /v2/version_files батчами по 100
            hash_to_proj = {}
            uniq_hashes = list(dict.fromkeys(sha1_list))
            for i in range(0, len(uniq_hashes), 100):
                batch = uniq_hashes[i:i + 100]
                try:
                    rvf = _get_http().post(
                        "https://api.modrinth.com/v2/version_files",
                        json={"hashes": batch, "algorithm": "sha1"},
                        headers={"User-Agent": USER_AGENT},
                        timeout=15,
                    )
                    if rvf.status_code == 200:
                        data = rvf.json() or {}
                        for h, vobj in data.items():
                            if isinstance(vobj, dict) and vobj.get("project_id"):
                                hash_to_proj[h.lower()] = vobj["project_id"]
                except Exception:
                    continue

            # Подтягиваем названия, иконки и описания проектов батчами по 100
            proj_meta = {}
            uniq_pids = list(dict.fromkeys(hash_to_proj.values()))
            for i in range(0, len(uniq_pids), 100):
                batch = uniq_pids[i:i + 100]
                try:
                    rp = _get_http().get(
                        "https://api.modrinth.com/v2/projects",
                        params={"ids": json.dumps(batch)},
                        headers={"User-Agent": USER_AGENT},
                        timeout=15,
                    )
                    if rp.status_code == 200:
                        for pobj in (rp.json() or []):
                            if isinstance(pobj, dict) and pobj.get("id"):
                                proj_meta[pobj["id"]] = pobj
                except Exception:
                    continue

            type_order = {"mod": 0, "resourcepack": 1, "shader": 2, "datapack": 3}
            url_seg = {"mod": "mod", "resourcepack": "resourcepack", "shader": "shader", "datapack": "datapack"}
            counts = {"mod": 0, "resourcepack": 0, "shader": 0, "datapack": 0, "total": 0}
            items = []

            for e in raw_entries:
                pid = hash_to_proj.get(e["sha1"], "")
                meta = proj_meta.get(pid) if pid else None
                ctype = e["type"]
                counts[ctype] = counts.get(ctype, 0) + 1
                counts["total"] += 1

                if meta:
                    slug = meta.get("slug") or pid
                    pt = meta.get("project_type") or url_seg.get(ctype, "mod")
                    items.append({
                        "title": meta.get("title") or e["filename"],
                        "filename": e["filename"],
                        "type": ctype,
                        "description": meta.get("description") or "",
                        "icon_url": meta.get("icon_url") or "",
                        "downloads": int(meta.get("downloads") or 0),
                        "file_size": e["file_size"],
                        "web_url": f"https://modrinth.com/{pt}/{slug}",
                        "source": "Modrinth",
                    })
                else:
                    clean_title = re.sub(r"\.(jar|zip)$", "", e["filename"], flags=re.I)
                    items.append({
                        "title": clean_title,
                        "filename": e["filename"],
                        "type": ctype,
                        "description": "Встроенный файл из overrides" if e["is_override"] else "Файл из манифеста сборки",
                        "icon_url": "",
                        "downloads": 0,
                        "file_size": e["file_size"],
                        "web_url": "",
                        "source": "Override" if e["is_override"] else "Файл",
                    })

            items.sort(key=lambda x: (type_order.get(x["type"], 9), x["title"].lower()))
            result = {
                "ok": True,
                "items": items,
                "counts": counts,
                "dependencies": index.get("dependencies") or {},
            }
            if cache_key:
                self._mrpack_contents_cache[cache_key] = result
            return result
        except Exception as e:
            return {"ok": False, "error": f"Не удалось прочитать состав модпака: {e}"}

    def install_modpack(self, pack, version_id=None):
        import tempfile
        if self._is_busy:
            return {"ok": False, "error": "Дождитесь завершения текущей операции"}
        self._is_busy = True
        tmp_path = None
        try:
            self._set_status(f"Поиск версии модпака «{pack['title']}»...", 0.05, True)
            chosen = None
            if version_id:
                rv = _get_http().get(
                    f"https://api.modrinth.com/v2/version/{version_id}",
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                if rv.status_code == 200:
                    chosen = rv.json()
            if not chosen:
                r = _get_http().get(
                    f"https://api.modrinth.com/v2/project/{pack['id']}/version",
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                r.raise_for_status()
                versions = r.json() or []
                chosen = next(
                    (v for v in versions if any(l in ("fabric", "quilt", "forge", "neoforge") for l in (v.get("loaders") or []))),
                    versions[0] if versions else None,
                )
            if not chosen:
                return {"ok": False, "error": "У модпака нет доступных файлов"}
            files = chosen.get("files") or []
            primary = next((f for f in files if f.get("primary")), files[0])
            url = primary["url"]

            fd, tmp_path = tempfile.mkstemp(suffix=".mrpack")
            os.close(fd)
            self._set_status(f"Скачивание архива {primary['filename']}...", 0.08, True)
            self._download_file(url, tmp_path)
            return self._install_mrpack_archive(tmp_path, fallback_name=pack.get("title", "Modpack"), icon_url=pack.get("icon_url", ""))
        except Exception as e:
            self._set_status("Ошибка установки модпака", 0, False)
            return {"ok": False, "error": str(e)}
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            self._is_busy = False

    def import_local_mrpack(self):
        if self._is_busy:
            return {"ok": False, "error": "Дождитесь завершения текущей операции"}
        if not self._window:
            return {"ok": False, "error": "Окно не готово"}
        dialog_type = getattr(webview, "OPEN_DIALOG", None)
        if dialog_type is None and hasattr(webview, "FileDialog"):
            dialog_type = webview.FileDialog.OPEN
        try:
            res = self._window.create_file_dialog(
                dialog_type,
                allow_multiple=False,
                file_types=("Modrinth Modpack (*.mrpack)",),
            )
            if not res:
                return {"ok": False, "cancelled": True}
            mrpack_path = res[0]
        except Exception as e:
            return {"ok": False, "error": str(e)}

        self._is_busy = True
        try:
            fallback_name = os.path.splitext(os.path.basename(mrpack_path))[0]
            return self._install_mrpack_archive(mrpack_path, fallback_name=fallback_name)
        except Exception as e:
            self._set_status("Ошибка импорта .mrpack", 0, False)
            return {"ok": False, "error": str(e)}
        finally:
            self._is_busy = False

    def _setup_dnd(self):
        """Регистрирует перехват Drag-and-Drop на стороне pywebview."""
        try:
            from webview.dom import DOMEventHandler
            if hasattr(self._window, "dom") and self._window.dom:
                self._window.dom.document.events.drop += DOMEventHandler(
                    self._on_pywebview_drop, prevent_default=True
                )
        except Exception as e:
            pass

    def _extract_dropped_file_path(self, event):
        """Извлекает реальный путь к перетащенному файлу без передачи через JS."""
        import urllib.parse
        if isinstance(event, dict):
            dt = event.get("dataTransfer") or {}
            files = dt.get("files") or event.get("files") or []
            for f in files:
                if isinstance(f, dict):
                    p = f.get("pywebviewFullPath") or f.get("path")
                    if p and os.path.exists(p):
                        return p
                elif isinstance(f, str) and os.path.exists(f):
                    return f

        try:
            from webview.dom import _dnd_state
            paths = _dnd_state.get("paths") or []
            if paths:
                for item in list(paths):
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        fullpath = urllib.parse.unquote(str(item[1]))
                        if os.path.exists(fullpath):
                            try:
                                paths.remove(item)
                            except Exception:
                                pass
                            return fullpath
                    elif isinstance(item, str) and os.path.exists(item):
                        try:
                            paths.remove(item)
                        except Exception:
                            pass
                        return item
        except Exception:
            pass

        return None

    def _on_pywebview_drop(self, event):
        """Обработчик события drop от pywebview DOM."""
        try:
            if isinstance(event, dict):
                dt = event.get("dataTransfer") or {}
                types = dt.get("types") or []
                files = dt.get("files") or event.get("files") or []
                if not files and "Files" not in types:
                    return
            file_path = self._extract_dropped_file_path(event)
            if not file_path:
                self._emit_js("showToast('Не удалось определить путь к перетащенному файлу', 'error')")
                return
            threading.Thread(target=self.process_archive_import, args=(file_path,), daemon=True).start()
        except Exception as e:
            self._emit_js(f"showToast({json.dumps(f'Ошибка Drag-and-Drop: {e}')}, 'error')")

    def import_archive_dialog(self):
        """Открывает системный диалог выбора архива сборки (.zip или .mrpack)."""
        if self._is_busy:
            return {"ok": False, "error": "Дождитесь завершения текущей операции"}
        if not self._window:
            return {"ok": False, "error": "Окно не готово"}
        dialog_type = getattr(webview, "OPEN_DIALOG", None)
        if dialog_type is None and hasattr(webview, "FileDialog"):
            dialog_type = webview.FileDialog.OPEN
        try:
            res = self._window.create_file_dialog(
                dialog_type,
                allow_multiple=False,
                file_types=("Сборка Minecraft (*.zip;*.mrpack)", "Все файлы (*.*)"),
            )
            if not res:
                return {"ok": False, "cancelled": True}
            file_path = res[0]
        except Exception as e:
            return {"ok": False, "error": str(e)}

        threading.Thread(target=self.process_archive_import, args=(file_path,), daemon=True).start()
        return {"ok": True, "path": file_path}

    def _inspect_archive_file(self, file_path):
        """Определяет тип архива: mrpack, curseforge, raw_zip или unknown."""
        import zipfile
        import collections
        with zipfile.ZipFile(file_path, "r") as zf:
            namelist = zf.namelist()

            # 1. Modrinth .mrpack
            if "modrinth.index.json" in namelist:
                return "mrpack", {"path": file_path}

            # 2. CurseForge modpack (manifest.json)
            manifest_name = None
            for n in namelist:
                if n.lower() == "manifest.json" or n.lower().endswith("/manifest.json"):
                    manifest_name = n
                    break
            if manifest_name:
                try:
                    manifest_data = json.loads(zf.read(manifest_name).decode("utf-8", errors="ignore"))
                    if isinstance(manifest_data, dict) and (
                        "minecraft" in manifest_data
                        or manifest_data.get("manifestType") == "minecraftModpack"
                        or "files" in manifest_data
                    ):
                        return "curseforge", {"path": file_path, "manifest": manifest_data}
                except Exception:
                    pass

            # 3. Raw ZIP (mods / config / saves)
            known_dirs = {"mods", "config", "shaderpacks", "resourcepacks", "saves", "datapacks"}
            found_dirs = set()
            root_prefix = ""
            mods_count = 0
            mod_filenames = []

            for name in namelist:
                norm = name.replace("\\", "/").strip("/")
                parts = norm.split("/")
                for i, part in enumerate(parts):
                    p_lower = part.lower()
                    if p_lower in known_dirs:
                        found_dirs.add(p_lower)
                        if root_prefix == "" and i > 0:
                            root_prefix = "/".join(parts[:i]) + "/"
                    if p_lower == "mods" and i < len(parts) - 1 and parts[-1].lower().endswith((".jar", ".zip")):
                        mods_count += 1
                        if len(mod_filenames) < 40:
                            mod_filenames.append(parts[-1])

            if "mods" in found_dirs or "config" in found_dirs:
                has_neoforge = any("neoforge" in fn.lower() for fn in mod_filenames)
                has_quilt = any("quilt" in fn.lower() for fn in mod_filenames)
                has_fabric = any("fabric" in fn.lower() for fn in mod_filenames)
                has_forge = any("forge" in fn.lower() for fn in mod_filenames)

                detected_loader = "Fabric"
                if has_quilt and not has_fabric:
                    detected_loader = "Quilt"
                elif has_neoforge:
                    detected_loader = "NeoForge"
                elif has_forge and not has_fabric:
                    detected_loader = "Forge"
                elif has_quilt:
                    detected_loader = "Quilt"
                elif has_fabric:
                    detected_loader = "Fabric"

                detected_version = "1.20.1"
                ver_matches = []
                for fn in mod_filenames:
                    m = re.findall(r"1\.\d+(?:\.\d+)?", fn)
                    ver_matches.extend(m)
                if ver_matches:
                    detected_version = collections.Counter(ver_matches).most_common(1)[0][0]

                default_name = os.path.splitext(os.path.basename(file_path))[0]
                return "raw_zip", {
                    "path": file_path,
                    "default_name": default_name,
                    "folders": sorted(list(found_dirs)),
                    "mods_count": mods_count,
                    "root_prefix": root_prefix,
                    "detected_loader": detected_loader,
                    "detected_version": detected_version,
                }

            return "unknown", {"path": file_path}

    def process_archive_import(self, file_path):
        """Главный маршрутизатор импорта любого типа архива."""
        if not file_path or not os.path.isfile(file_path):
            self._emit_js("showToast('Файл не найден на диске', 'error')")
            return

        lower_ext = os.path.splitext(file_path)[1].lower()
        if lower_ext not in (".zip", ".mrpack"):
            self._emit_js("showToast('Неподдерживаемый формат: поддерживаются только .zip и .mrpack', 'warn')")
            return

        if self._is_busy:
            self._emit_js("showToast('Дождитесь завершения текущей операции', 'warn')")
            return

        try:
            archive_type, info = self._inspect_archive_file(file_path)
        except Exception as e:
            self._emit_js(f"showToast({json.dumps(f'Не удалось прочитать архив: {e}')}, 'error')")
            return

        if archive_type == "mrpack":
            fallback_name = os.path.splitext(os.path.basename(file_path))[0]
            self._is_busy = True
            try:
                res = self._install_mrpack_archive(file_path, fallback_name=fallback_name)
                if res.get("ok"):
                    msg_js = json.dumps(f"Импортирован модпак «{res['name']}»!")
                    id_js = json.dumps(res["inst_id"])
                    self._emit_js(
                        f"window.onStateChanged && window.onStateChanged(); showToast({msg_js}); openInstanceDetail({id_js});"
                    )
                else:
                    self._emit_js(f"showToast({json.dumps(res.get('error') or 'Ошибка импорта .mrpack')}, 'error')")
            finally:
                self._is_busy = False

        elif archive_type == "curseforge":
            self._is_busy = True
            try:
                res = self._install_curseforge_archive(file_path, manifest=info.get("manifest"))
                if res.get("ok"):
                    msg_js = json.dumps(f"Импортирована сборка CurseForge «{res['name']}»!")
                    id_js = json.dumps(res["inst_id"])
                    self._emit_js(
                        f"window.onStateChanged && window.onStateChanged(); showToast({msg_js}); openInstanceDetail({id_js});"
                    )
                    if res.get("skipped_mods"):
                        self._emit_js(
                            f"window.onCurseForgeSkippedMods && window.onCurseForgeSkippedMods({json.dumps(res['inst_id'])}, {json.dumps(res['name'])}, {json.dumps(res['skipped_mods'])})"
                        )
                else:
                    self._emit_js(f"showToast({json.dumps(res.get('error') or 'Ошибка импорта CurseForge')}, 'error')")
            finally:
                self._is_busy = False

        elif archive_type == "raw_zip":
            self._emit_js(f"window.onRawZipDetected && window.onRawZipDetected({json.dumps(info)})")

        else:
            self._emit_js(
                "showToast('В архиве не найдены файлы сборки (нет manifest.json, modrinth.index.json или папки mods/config)', 'error')"
            )

    def _install_curseforge_archive(self, zip_path, manifest=None):
        """Устанавливает модпак CurseForge: парсинг manifest.json, скачивание модов и распаковка overrides."""
        import uuid
        import shutil
        import zipfile
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not manifest:
            with zipfile.ZipFile(zip_path, "r") as zf:
                manifest_name = None
                for n in zf.namelist():
                    if n.lower() == "manifest.json" or n.lower().endswith("/manifest.json"):
                        manifest_name = n
                        break
                if not manifest_name:
                    return {"ok": False, "error": "Файл manifest.json не найден в архиве"}
                manifest = json.loads(zf.read(manifest_name).decode("utf-8", errors="ignore"))

        mc_info = manifest.get("minecraft") or {}
        mc_ver = str(mc_info.get("version") or "").strip()
        if not mc_ver:
            return {"ok": False, "error": "В манифесте CurseForge не указана версия Minecraft"}

        loaders = mc_info.get("modLoaders") or []
        loader = "Vanilla"
        for l in loaders:
            lid = (l.get("id") or "").lower()
            if "fabric" in lid:
                loader = "Fabric"
                break
            elif "quilt" in lid:
                loader = "Quilt"
                break
            elif "neoforge" in lid:
                loader = "NeoForge"
                break
            elif "forge" in lid:
                loader = "Forge"
                break

        if loader == "Fabric":
            icon_key, color = "bolt", "#1bd96a"
        elif loader == "Quilt":
            icon_key, color = "diamond", "#8b5cf6"
        elif loader == "Forge":
            icon_key, color = "anvil", "#f16436"
        elif loader == "NeoForge":
            icon_key, color = "flame", "#f97316"
        else:
            icon_key, color = "grass", "#22d3ee"

        cf_key = self._get_cf_api_key()
        if not cf_key:
            return {"ok": False, "error": "Не указан CurseForge API Key в настройках"}

        pack_name = str(manifest.get("name") or os.path.splitext(os.path.basename(zip_path))[0]).strip()
        inst_id = f"{loader.lower()}_{re.sub(r'[^a-zA-Z0-9._-]', '_', mc_ver)}_{uuid.uuid4().hex[:6]}"
        inst_dir = os.path.join(INSTANCES_DIR, inst_id)
        for sub in ("mods", "config", "resourcepacks", "shaderpacks", "datapacks", "saves"):
            os.makedirs(os.path.join(inst_dir, sub), exist_ok=True)

        self._set_status(f"Распаковка файлов сборки «{pack_name}»...", 0.08, True)
        override_dir_name = str(manifest.get("overrides") or "overrides").strip().rstrip("/\\")
        cover_file = ""

        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                fn = member.filename.replace("\\", "/")
                rel = None
                if fn.startswith(f"{override_dir_name}/"):
                    rel = fn[len(f"{override_dir_name}/"):]
                elif f"/{override_dir_name}/" in fn:
                    rel = fn.split(f"/{override_dir_name}/", 1)[1]
                elif fn.startswith("client-overrides/"):
                    rel = fn[len("client-overrides/"):]
                elif "/client-overrides/" in fn:
                    rel = fn.split("/client-overrides/", 1)[1]

                if rel and not rel.startswith(("/", "\\")):
                    try:
                        dst = _safe_join(inst_dir, rel)
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        with zf.open(member, "r") as sf, open(dst, "wb") as df:
                            shutil.copyfileobj(sf, df)
                    except Exception:
                        pass

                if not cover_file and fn.lower().endswith(("icon.png", "logo.png", "modpack.png")):
                    try:
                        dst_cov = os.path.join(inst_dir, "greenprism_cover.png")
                        with zf.open(member, "r") as sf, open(dst_cov, "wb") as df:
                            shutil.copyfileobj(sf, df)
                        cover_file = "greenprism_cover.png"
                    except Exception:
                        pass

        headers = {
            "x-api-key": cf_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

        files_entries = manifest.get("files") or []
        total_files = len(files_entries)
        file_ids = [entry["fileID"] for entry in files_entries if entry.get("fileID")]

        self._set_status(f"Получение ссылок на моды CurseForge ({total_files} шт.)...", 0.15, True)
        file_info_map = {}

        for i in range(0, len(file_ids), 50):
            chunk = file_ids[i:i + 50]
            try:
                r = _get_http().post(
                    "https://api.curseforge.com/v1/mods/files",
                    headers=headers,
                    json={"fileIds": chunk},
                    timeout=15,
                )
                if r.status_code == 200:
                    for item in r.json().get("data") or []:
                        file_info_map[item["id"]] = item
            except Exception:
                pass

        for entry in files_entries:
            fid = entry.get("fileID")
            pid = entry.get("projectID")
            if fid and fid not in file_info_map and pid:
                try:
                    r = _get_http().get(
                        f"https://api.curseforge.com/v1/mods/{pid}/files/{fid}",
                        headers=headers,
                        timeout=10,
                    )
                    if r.status_code == 200:
                        fitem = r.json().get("data") or {}
                        if fitem:
                            file_info_map[fid] = fitem
                except Exception:
                    pass

        download_queue = []
        skipped_mods = []
        mods_dir = os.path.join(inst_dir, "mods")

        for entry in files_entries:
            fid = entry.get("fileID")
            pid = entry.get("projectID")
            finfo = file_info_map.get(fid) or {}
            dl_url = finfo.get("downloadUrl")
            fname = finfo.get("fileName") or (f"mod_{pid}_{fid}.jar" if not dl_url else os.path.basename(dl_url))

            if dl_url:
                download_queue.append((dl_url, os.path.join(mods_dir, fname), pid, fid, fname))
            else:
                skipped_mods.append({
                    "name": fname or f"Мод #{pid}",
                    "url": f"https://www.curseforge.com/projects/{pid}",
                    "projectId": pid,
                    "fileId": fid,
                })

        total_dl = len(download_queue)
        done_dl = 0
        failed_dl = 0
        dl_lock = threading.Lock()

        def _dl_cf_item(item):
            url, dst_path, pid, fid, fn = item
            try:
                self._download_file(url, dst_path, emit_telemetry=False)
                return True, None
            except Exception:
                return False, {
                    "name": fn or f"Мод #{pid}",
                    "url": f"https://www.curseforge.com/projects/{pid}",
                    "projectId": pid,
                    "fileId": fid,
                }

        if total_dl > 0:
            self._set_status(f"Скачивание модов «{pack_name}» (0/{total_dl})...", 0.2, True)
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(_dl_cf_item, item) for item in download_queue]
                for fut in as_completed(futures):
                    ok, skipped_item = fut.result()
                    with dl_lock:
                        done_dl += 1
                        if not ok:
                            failed_dl += 1
                            if skipped_item:
                                skipped_mods.append(skipped_item)
                        if done_dl % 3 == 0 or done_dl == total_dl:
                            frac = 0.2 + 0.72 * (done_dl / total_dl)
                            self._set_status(f"Моды «{pack_name}»: {done_dl}/{total_dl}...", frac, True)

        self._config["instances"].append({
            "id": inst_id,
            "name": pack_name,
            "loader": loader,
            "version": mc_ver,
            "icon": icon_key,
            "color": color,
            "playtime_sec": 0,
            "last_played": "",
            "cover_file": cover_file,
            "custom_icon_file": "",
            "installed_projects": {},
            "server_address": "",
        })
        self._save_config()

        status_msg = f"Сборка «{pack_name}» установлена ({done_dl - failed_dl}/{total_dl} модов)"
        if skipped_mods:
            status_msg += f", пропущено: {len(skipped_mods)}"
        self._set_status(status_msg, 1.0, False)

        return {
            "ok": True,
            "inst_id": inst_id,
            "name": pack_name,
            "skipped_mods": skipped_mods,
            "state": self.get_state(),
        }

    def finish_raw_zip_import(self, zip_path, name, loader, version, root_prefix=""):
        """Завершает импорт Raw ZIP сборки после подтверждения пользователем."""
        if self._is_busy:
            return {"ok": False, "error": "Дождитесь завершения текущей операции"}
        if not zip_path or not os.path.exists(zip_path):
            return {"ok": False, "error": "Файл архива не найден"}

        self._is_busy = True
        try:
            return self._finish_raw_zip_worker(zip_path, name, loader, version, root_prefix)
        finally:
            self._is_busy = False

    def _finish_raw_zip_worker(self, zip_path, name, loader, version, root_prefix=""):
        import uuid
        import shutil
        import zipfile

        pack_name = str(name or os.path.splitext(os.path.basename(zip_path))[0]).strip()
        loader = str(loader or "Fabric").strip()
        if loader not in ("Fabric", "Quilt", "Forge", "NeoForge", "Vanilla"):
            loader = "Fabric"
        if isinstance(version, dict):
            version = str(version.get("id") or "").strip()
        else:
            version = str(version or "").strip()
        if not version or version == "[object Object]":
            cand_list = self._versions.get(loader) or self._versions.get("Vanilla") or []
            picked = ""
            for item in cand_list:
                if isinstance(item, dict):
                    if item.get("type", "release") == "release" and item.get("id"):
                        picked = str(item["id"]).strip()
                        break
                elif isinstance(item, str) and item.strip():
                    picked = item.strip()
                    break
            version = picked or "1.20.1"

        if loader == "Fabric":
            icon_key, color = "bolt", "#1bd96a"
        elif loader == "Quilt":
            icon_key, color = "diamond", "#8b5cf6"
        elif loader == "Forge":
            icon_key, color = "anvil", "#f16436"
        elif loader == "NeoForge":
            icon_key, color = "flame", "#f97316"
        else:
            icon_key, color = "grass", "#22d3ee"

        inst_id = f"{loader.lower()}_{re.sub(r'[^a-zA-Z0-9._-]', '_', version)}_{uuid.uuid4().hex[:6]}"
        inst_dir = os.path.join(INSTANCES_DIR, inst_id)
        for sub in ("mods", "config", "resourcepacks", "shaderpacks", "datapacks", "saves"):
            os.makedirs(os.path.join(inst_dir, sub), exist_ok=True)

        self._set_status(f"Распаковка сборки «{pack_name}»...", 0.2, True)

        known_dirs = {"mods", "config", "shaderpacks", "resourcepacks", "saves", "datapacks"}
        cover_file = ""

        with zipfile.ZipFile(zip_path, "r") as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            total = len(members)
            done = 0

            for member in members:
                fn = member.filename.replace("\\", "/")
                rel = None
                if root_prefix and fn.startswith(root_prefix):
                    rel = fn[len(root_prefix):]
                elif not root_prefix:
                    rel = fn
                else:
                    continue

                if not rel or rel.startswith(("/", "\\")):
                    continue

                first_seg = rel.split("/")[0].lower()
                if first_seg in known_dirs:
                    dst = _safe_join(inst_dir, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    try:
                        with zf.open(member, "r") as sf, open(dst, "wb") as df:
                            shutil.copyfileobj(sf, df)
                    except Exception:
                        pass

                if not cover_file and fn.lower().endswith(("icon.png", "logo.png", "modpack.png")):
                    try:
                        dst_cov = os.path.join(inst_dir, "greenprism_cover.png")
                        with zf.open(member, "r") as sf, open(dst_cov, "wb") as df:
                            shutil.copyfileobj(sf, df)
                        cover_file = "greenprism_cover.png"
                    except Exception:
                        pass

                done += 1
                if done % 20 == 0 or done == total:
                    frac = 0.2 + 0.75 * (done / max(total, 1))
                    self._set_status(f"Распаковка «{pack_name}» ({done}/{total})...", frac, True)

        self._config["instances"].append({
            "id": inst_id,
            "name": pack_name,
            "loader": loader,
            "version": version,
            "icon": icon_key,
            "color": color,
            "playtime_sec": 0,
            "last_played": "",
            "cover_file": cover_file,
            "custom_icon_file": "",
            "installed_projects": {},
            "server_address": "",
        })
        self._save_config()

        self._set_status(f"Сборка «{pack_name}» готова к запуску!", 1.0, False)
        msg_js = json.dumps(f"Сборка «{pack_name}» создана")
        id_js = json.dumps(inst_id)
        self._emit_js(
            f"window.onStateChanged && window.onStateChanged(); showToast({msg_js}); openInstanceDetail({id_js});"
        )

        return {
            "ok": True,
            "inst_id": inst_id,
            "name": pack_name,
            "state": self.get_state(),
        }

    def delete_instance(self, inst_id):
        import shutil
        sid = str(inst_id or "").strip()
        if not sid or "/" in sid or "\\" in sid or sid in (".", ".."):
            return self.get_state()
        try:
            inst_dir = _safe_join(INSTANCES_DIR, sid)
        except ValueError:
            return self.get_state()
        self._config["instances"] = [i for i in self._config["instances"] if i["id"] != sid]
        self._save_config()
        if os.path.isdir(inst_dir):
            shutil.rmtree(inst_dir, ignore_errors=True)
        return self.get_state()

    def open_instance_folder(self, inst_id, subfolder=""):
        import subprocess
        sid = str(inst_id or "").strip()
        if not sid or "/" in sid or "\\" in sid or sid in (".", ".."):
            return False
        try:
            inst_dir = _safe_join(INSTANCES_DIR, sid)
            target = _safe_join(inst_dir, subfolder, allow_base=True) if subfolder else inst_dir
        except ValueError:
            return False
        os.makedirs(target, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(target)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return True

    def open_url(self, url):
        import webbrowser
        from urllib.parse import urlparse
        s = str(url or "").strip()
        parsed = urlparse(s)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            return False
        webbrowser.open(s)
        return True

    def get_instance_logs(self, inst_id):
        return self._logs.get(inst_id, [])

    def clear_instance_logs(self, inst_id):
        self._logs[inst_id] = []
        return True

    # ---------------- Контент внутри сборки ----------------
    def _ensure_meta_cache_loaded(self):
        if self._meta_cache is not None:
            return
        with self._meta_lock:
            if self._meta_cache is not None:
                return
            loaded = {}
            if os.path.exists(META_CACHE_PATH):
                try:
                    with open(META_CACHE_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        loaded = data
                except Exception:
                    pass
            self._meta_cache = loaded

    def _flush_meta_cache_now(self):
        with self._meta_lock:
            if self._meta_save_timer is not None:
                try:
                    self._meta_save_timer.cancel()
                except Exception:
                    pass
                self._meta_save_timer = None
            if self._meta_cache is None:
                return
            if len(self._meta_cache) > 600:
                keys = list(self._meta_cache.keys())[-600:]
                self._meta_cache = {k: self._meta_cache[k] for k in keys}
            snapshot = dict(self._meta_cache)
        with self._meta_io_lock:
            tmp = f"{META_CACHE_PATH}.{threading.get_ident()}.tmp"
            try:
                os.makedirs(BASE_DIR, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f, ensure_ascii=False)
                    f.flush()
                os.replace(tmp, META_CACHE_PATH)
            except Exception:
                pass
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

    def _schedule_meta_cache_save(self):
        with self._meta_lock:
            if self._meta_save_timer is not None:
                self._meta_save_timer.cancel()
            t = threading.Timer(1.2, self._flush_meta_cache_now)
            t.daemon = True
            t.start()
            self._meta_save_timer = t

    def _get_cached_meta(self, full_path, content_type):
        self._ensure_meta_cache_loaded()
        try:
            st = os.stat(full_path)
            key = f"{content_type}|{os.path.basename(full_path)}|{st.st_mtime_ns}|{st.st_size}"
        except Exception:
            key = None

        if key:
            with self._meta_lock:
                hit = self._meta_cache.get(key)
            if hit is not None:
                return hit

        meta = extract_jar_metadata(full_path) if content_type == "mod" else extract_pack_metadata(full_path)
        if key:
            with self._meta_lock:
                self._meta_cache[key] = meta
            self._schedule_meta_cache_save()
        return meta

    def get_instance_content(self, inst_id, content_type="mod"):
        folder_name = CONTENT_FOLDERS.get(content_type, "mods")
        target_dir = os.path.join(INSTANCES_DIR, inst_id, folder_name)
        os.makedirs(target_dir, exist_ok=True)
        self._ensure_meta_cache_loaded()

        valid_exts = (".jar", ".jar.disabled") if content_type == "mod" else (".zip", ".zip.disabled")
        entries = []
        with os.scandir(target_dir) as it:
            for de in it:
                fname = de.name
                is_dir = de.is_dir()
                if content_type == "mod" and not fname.endswith(valid_exts):
                    continue
                if content_type != "mod" and not (fname.endswith(valid_exts) or is_dir):
                    continue
                try:
                    st = de.stat()
                    size_kb = 0 if is_dir else round(st.st_size / 1024)
                    cache_key = f"{content_type}|{fname}|{st.st_mtime_ns}|{st.st_size}"
                except Exception:
                    size_kb = 0
                    cache_key = None
                entries.append((fname, de.path, is_dir, size_kb, cache_key))

        entries.sort(key=lambda x: x[0].lower())

        # Определяем, какие файлы ещё не в дисковом кэше, и парсим их параллельно
        missing = []
        meta_results = {}
        with self._meta_lock:
            for fname, full, _is_dir, _sz, ckey in entries:
                if ckey and ckey in self._meta_cache:
                    meta_results[full] = self._meta_cache[ckey]
                else:
                    missing.append((full, ckey))

        if missing:
            parse_fn = extract_jar_metadata if content_type == "mod" else extract_pack_metadata
            if len(missing) >= 4:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=6) as pool:
                    parsed = list(pool.map(lambda p: parse_fn(p[0]), missing))
            else:
                parsed = [parse_fn(p[0]) for p in missing]

            with self._meta_lock:
                for (full, ckey), meta in zip(missing, parsed):
                    meta_results[full] = meta
                    if ckey:
                        self._meta_cache[ckey] = meta
            self._schedule_meta_cache_save()

        items = []
        for fname, full, is_dir, size_kb, _ckey in entries:
            enabled = not fname.endswith(".disabled")
            meta = meta_results.get(full) or {}

            clean_file = fname
            for suf in (".jar.disabled", ".jar", ".zip.disabled", ".zip", ".disabled"):
                if clean_file.endswith(suf):
                    clean_file = clean_file[:-len(suf)]
                    break

            display_name = meta.get("name") or clean_file
            subtitle = meta.get("version") or meta.get("description") or ""
            items.append({
                "filename": fname,
                "display_name": display_name,
                "raw_name": clean_file,
                "subtitle": subtitle,
                "icon": meta.get("icon", ""),
                "enabled": enabled,
                "size_kb": size_kb,
                "is_dir": is_dir,
            })
        return items

    def toggle_content_item(self, inst_id, content_type, filename):
        folder_name = CONTENT_FOLDERS.get(content_type, "mods")
        fn = str(filename or "").strip()
        if not fn or "/" in fn or "\\" in fn or fn in (".", ".."):
            return self.get_instance_content(inst_id, content_type)
        try:
            inst_dir = _safe_join(INSTANCES_DIR, inst_id)
            target_dir = _safe_join(inst_dir, folder_name)
            src = _safe_join(target_dir, fn)
            dst_name = fn[:-9] if fn.endswith(".disabled") else fn + ".disabled"
            dst = _safe_join(target_dir, dst_name)
        except ValueError:
            return self.get_instance_content(inst_id, content_type)
        if not os.path.exists(src):
            return self.get_instance_content(inst_id, content_type)
        os.rename(src, dst)
        return self.get_instance_content(inst_id, content_type)

    def delete_content_item(self, inst_id, content_type, filename):
        import shutil
        folder_name = CONTENT_FOLDERS.get(content_type, "mods")
        fn = str(filename or "").strip()
        if not fn or "/" in fn or "\\" in fn or fn in (".", ".."):
            return self.get_instance_content(inst_id, content_type)
        try:
            inst_dir = _safe_join(INSTANCES_DIR, inst_id)
            target_dir = _safe_join(inst_dir, folder_name)
            target = _safe_join(target_dir, fn)
        except ValueError:
            return self.get_instance_content(inst_id, content_type)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.exists(target):
            os.remove(target)

        inst = self._find_instance(inst_id)
        if inst and isinstance(inst.get("installed_projects"), dict):
            base_fn = fn[:-9] if fn.endswith(".disabled") else fn
            to_del = [k for k, v in inst["installed_projects"].items() if v == base_fn or v == fn]
            if to_del:
                for k in to_del:
                    del inst["installed_projects"][k]
                self._save_config()

        return self.get_instance_content(inst_id, content_type)

    def toggle_all_content(self, inst_id, content_type, enable=True):
        """Включает или выключает все моды/паки в выбранной папке сборки за один клик."""
        folder_name = CONTENT_FOLDERS.get(content_type, "mods")
        target_dir = os.path.join(INSTANCES_DIR, inst_id, folder_name)
        if not os.path.isdir(target_dir):
            return {"ok": True, "changed": 0}

        ext = ".jar" if content_type == "mod" else ".zip"
        changed = 0
        for fname in list(os.listdir(target_dir)):
            src = os.path.join(target_dir, fname)
            if not os.path.isfile(src):
                continue
            if enable and fname.endswith(ext + ".disabled"):
                dst = os.path.join(target_dir, fname[:-9])
                if not os.path.exists(dst):
                    try:
                        os.rename(src, dst)
                        changed += 1
                    except Exception:
                        pass
            elif (not enable) and fname.endswith(ext):
                dst = os.path.join(target_dir, fname + ".disabled")
                if not os.path.exists(dst):
                    try:
                        os.rename(src, dst)
                        changed += 1
                    except Exception:
                        pass
        return {"ok": True, "changed": changed}

    def import_local_content(self, inst_id, content_type="mod"):
        """Копирует выбранные пользователем .jar/.zip файлы с ПК прямо в папку сборки."""
        import shutil
        if not self._window:
            return {"ok": False, "error": "Окно не готово"}
        folder_name = CONTENT_FOLDERS.get(content_type, "mods")
        target_dir = os.path.join(INSTANCES_DIR, inst_id, folder_name)
        os.makedirs(target_dir, exist_ok=True)

        dialog_type = getattr(webview, "OPEN_DIALOG", None)
        if dialog_type is None and hasattr(webview, "FileDialog"):
            dialog_type = webview.FileDialog.OPEN
        file_types = (
            ("Моды Minecraft (*.jar)",)
            if content_type == "mod"
            else ("Архивы Minecraft (*.zip)",)
        )
        try:
            res = self._window.create_file_dialog(
                dialog_type,
                allow_multiple=True,
                file_types=file_types,
            )
            if not res:
                return {"ok": False, "cancelled": True}
            copied = 0
            for src in res:
                if os.path.isfile(src):
                    dst = os.path.join(target_dir, os.path.basename(src))
                    shutil.copy2(src, dst)
                    copied += 1
            self._set_status(f"Добавлено файлов в {folder_name}: {copied}", 1.0, False)
            return {"ok": True, "copied": copied}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- Поиск и определение уже установленных модов ----------------
    def _find_instance(self, inst_id):
        return next((i for i in self._config["instances"] if i["id"] == inst_id), None)

    def _get_installed_signatures(self, inst, content_type):
        folder_name = CONTENT_FOLDERS.get(content_type, "mods")
        target_dir = os.path.join(INSTANCES_DIR, inst["id"], folder_name)
        files_lower = []
        if os.path.exists(target_dir):
            files_lower = [f.lower() for f in os.listdir(target_dir)]
        tracked = inst.get("installed_projects") or {}
        existing_set = set(files_lower)
        tracked_keys = {
            k for k, fname in tracked.items()
            if str(fname).lower() in existing_set or (str(fname).lower() + ".disabled") in existing_set
        }
        return files_lower, tracked_keys

    def _is_item_installed(self, item, files_lower, tracked_keys):
        proj_key = f"{item['source']}:{item['id']}"
        if proj_key in tracked_keys:
            return True
        slug = (item.get("slug") or "").lower().strip()
        if len(slug) >= 4:
            slug_nodash = slug.replace("-", "").replace("_", "")
            for fn in files_lower:
                base = re.sub(r"\.(jar|zip)(\.disabled)?$", "", fn)
                if base == slug or base.startswith(slug + "-") or base.startswith(slug + "_"):
                    return True
                if len(slug_nodash) >= 5 and slug_nodash in base.replace("-", "").replace("_", ""):
                    return True
        return False

    def search_content(self, content_type, platform, query, inst_id, offset=0, sort="relevance"):
        inst = self._find_instance(inst_id)
        if not inst:
            return {"ok": False, "error": "Сборка не найдена"}

        if content_type not in CONTENT_FOLDERS:
            content_type = "mod"

        if platform == "CurseForge" and not self._get_cf_api_key():
            return {"ok": False, "need_key": True, "error": "Не указан CurseForge API Key"}

        offset = max(0, int(offset or 0))
        limit = 24
        clean_query = (query or "").strip()
        files_lower, tracked_keys = self._get_installed_signatures(inst, content_type)

        try:
            if platform == "Modrinth":
                facets = [[f"project_type:{content_type}"]]
                if content_type == "mod":
                    facets.append([f"versions:{inst['version']}"])
                    if inst["loader"] == "Quilt":
                        facets.append(["categories:quilt", "categories:fabric"])
                    else:
                        facets.append([f"categories:{inst['loader'].lower()}"])
                else:
                    if not inst["version"].startswith(("a", "b", "c", "rd-", "inf-")):
                        facets.append([f"versions:{inst['version']}"])

                mr_index = sort if sort in ("relevance", "downloads", "updated", "newest", "follows") else "relevance"
                if mr_index == "relevance" and not clean_query:
                    mr_index = "downloads"

                r = _get_http().get(
                    "https://api.modrinth.com/v2/search",
                    params={
                        "query": clean_query,
                        "limit": limit,
                        "offset": offset,
                        "index": mr_index,
                        "facets": json.dumps(facets),
                    },
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                r.raise_for_status()
                payload = r.json()
                hits = payload.get("hits", [])
                total = int(payload.get("total_hits", 0))
                items = []
                for h in hits:
                    it = {
                        "source": "Modrinth",
                        "content_type": content_type,
                        "id": h["project_id"],
                        "slug": h.get("slug", ""),
                        "title": h.get("title", "Без названия"),
                        "author": h.get("author", "unknown"),
                        "description": h.get("description", ""),
                        "downloads": h.get("downloads", 0),
                        "icon_url": h.get("icon_url") or "",
                        "web_url": f"https://modrinth.com/{content_type}/{h.get('slug', h['project_id'])}",
                    }
                    it["installed"] = self._is_item_installed(it, files_lower, tracked_keys)
                    items.append(it)
                next_offset = offset + len(items)
                return {
                    "ok": True,
                    "items": items,
                    "total": total,
                    "next_offset": next_offset,
                    "has_more": len(items) > 0 and next_offset < total,
                }

            else:
                if offset + limit > 10000:
                    return {"ok": True, "items": [], "total": 10000, "next_offset": offset, "has_more": False}

                params = {
                    "gameId": 432,
                    "classId": CF_CLASS_MAP.get(content_type, 6),
                    "sortField": CF_SORT_MAP.get(sort, 2),
                    "sortOrder": "desc",
                    "pageSize": limit,
                    "index": offset,
                }
                if not inst["version"].startswith(("a", "b", "c", "rd-", "inf-")):
                    params["gameVersion"] = inst["version"]
                if content_type == "mod":
                    params["modLoaderType"] = CF_LOADER_MAP.get(inst["loader"], 0)
                if clean_query:
                    params["searchFilter"] = clean_query

                r = _get_http().get(
                    "https://api.curseforge.com/v1/mods/search",
                    params=params,
                    headers={
                        "x-api-key": self._get_cf_api_key(),
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=15,
                )
                if r.status_code == 403:
                    return {"ok": False, "error": "CurseForge отклонил API-ключ (403). Проверь ключ в настройках."}
                r.raise_for_status()
                payload = r.json()
                data = payload.get("data", [])
                pagination = payload.get("pagination") or {}
                total = min(int(pagination.get("totalCount", len(data))), 10000)

                items = []
                for item in data:
                    authors = item.get("authors") or []
                    logo = item.get("logo") or {}
                    links = item.get("links") or {}
                    it = {
                        "source": "CurseForge",
                        "content_type": content_type,
                        "id": item["id"],
                        "slug": item.get("slug", ""),
                        "title": item.get("name", "Без названия"),
                        "author": authors[0]["name"] if authors else "unknown",
                        "description": item.get("summary", ""),
                        "downloads": item.get("downloadCount", 0),
                        "icon_url": logo.get("thumbnailUrl") or logo.get("url") or "",
                        "web_url": links.get("websiteUrl") or f"https://www.curseforge.com/minecraft/mc-mods/{item.get('slug', '')}",
                    }
                    it["installed"] = self._is_item_installed(it, files_lower, tracked_keys)
                    items.append(it)
                next_offset = offset + len(items)
                return {
                    "ok": True,
                    "items": items,
                    "total": total,
                    "next_offset": next_offset,
                    "has_more": len(items) > 0 and next_offset < total and next_offset + limit <= 10000,
                }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _emit_dl_speed(self, text=""):
        self._emit_js(f"window.onDownloadSpeed && window.onDownloadSpeed({json.dumps(text)})")

    def _download_file(self, url, dst_path, emit_telemetry=True, expected_sha1=""):
        tmp_path = f"{dst_path}.{threading.get_ident()}.tmp"
        t0 = time.perf_counter()
        last_emit = t0
        try:
            with _get_http().get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or 0)
                downloaded = 0
                os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=32768):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if emit_telemetry:
                                now = time.perf_counter()
                                if now - last_emit >= 0.12:
                                    elapsed = max(now - t0, 0.001)
                                    bps = downloaded / elapsed
                                    if bps >= 1024 * 1024:
                                        spd = f"{bps / (1024 * 1024):.1f} МБ/с"
                                    else:
                                        spd = f"{bps / 1024:.0f} КБ/с"
                                    done_mb = downloaded / (1024 * 1024)
                                    if total > 0:
                                        tot_mb = total / (1024 * 1024)
                                        self._emit_dl_speed(f"{spd} • {done_mb:.1f} / {tot_mb:.1f} МБ")
                                        pct = min(0.99, downloaded / total)
                                        self._emit_js(f"window.onStatusUpdate && window.onStatusUpdate({json.dumps(self._last_status)}, {round(pct * 100, 1)}, true)")
                                    else:
                                        self._emit_dl_speed(f"{spd} • {done_mb:.1f} МБ")
                                    last_emit = now
            if total > 0 and downloaded < total:
                raise IOError(f"Incomplete download: {downloaded}/{total} bytes")
            if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) <= 0:
                raise IOError(f"Empty download: {os.path.basename(dst_path)}")
            if expected_sha1:
                actual_sha1 = _file_sha1(tmp_path).lower()
                if actual_sha1 != expected_sha1.lower():
                    raise ValueError(f"SHA-1 mismatch for {os.path.basename(dst_path)}")
            os.replace(tmp_path, dst_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            if emit_telemetry:
                self._emit_dl_speed("")

    def _resolve_modrinth_deps(self, inst, dependencies, target_dir, visited, installed_deps, installed_keys, files_lower):
        """Рекурсивно скачивает обязательные (required) зависимости мода с Modrinth."""
        for dep in dependencies or []:
            if len(installed_deps) >= 15:
                break
            if dep.get("dependency_type") != "required":
                continue

            dep_proj_id = dep.get("project_id")
            dep_ver_id = dep.get("version_id")
            if not dep_proj_id and not dep_ver_id:
                continue

            ver_obj = None
            try:
                if not dep_proj_id and dep_ver_id:
                    rv = _get_http().get(
                        f"https://api.modrinth.com/v2/version/{dep_ver_id}",
                        headers={"User-Agent": USER_AGENT},
                        timeout=15,
                    )
                    if rv.status_code == 200:
                        ver_obj = rv.json()
                        dep_proj_id = ver_obj.get("project_id")

                if not dep_proj_id:
                    continue

                dep_key = f"Modrinth:{dep_proj_id}"
                if dep_key in visited or dep_key in installed_keys:
                    continue
                visited.add(dep_key)

                # Подбираем совместимую версию зависимости под текущий загрузчик и версию MC
                if ver_obj is None:
                    loader_low = inst["loader"].lower()
                    dep_loaders = ["quilt", "fabric"] if loader_low == "quilt" else [loader_low]
                    r = _get_http().get(
                        f"https://api.modrinth.com/v2/project/{dep_proj_id}/version",
                        params={
                            "loaders": json.dumps(dep_loaders),
                            "game_versions": json.dumps([inst["version"]]),
                        },
                        headers={"User-Agent": USER_AGENT},
                        timeout=15,
                    )
                    if r.status_code == 200 and r.json():
                        ver_obj = r.json()[0]
                    elif dep_ver_id:
                        rv = _get_http().get(
                            f"https://api.modrinth.com/v2/version/{dep_ver_id}",
                            headers={"User-Agent": USER_AGENT},
                            timeout=15,
                        )
                        if rv.status_code == 200:
                            ver_obj = rv.json()

                if not ver_obj:
                    continue

                files = ver_obj.get("files") or []
                if not files:
                    continue
                primary = next((f for f in files if f.get("primary")), files[0])
                dep_url = primary.get("url")
                dep_fname = primary.get("filename")
                if not dep_url or not dep_fname:
                    continue

                # Не скачиваем повторно, если файл уже лежит в папке mods
                if dep_fname.lower() in files_lower or (dep_fname.lower() + ".disabled") in files_lower:
                    inst.setdefault("installed_projects", {})[dep_key] = dep_fname
                    installed_keys.add(dep_key)
                else:
                    self._set_status(f"Зависимость: {dep_fname}...", 0.8, True)
                    self._download_file(dep_url, os.path.join(target_dir, dep_fname))
                    inst.setdefault("installed_projects", {})[dep_key] = dep_fname
                    installed_keys.add(dep_key)
                    files_lower.append(dep_fname.lower())
                    installed_deps.append({"id": dep_proj_id, "source": "Modrinth", "filename": dep_fname})

                # Рекурсивно проверяем зависимости самой библиотеки
                sub_deps = ver_obj.get("dependencies") or []
                if sub_deps:
                    self._resolve_modrinth_deps(
                        inst, sub_deps, target_dir, visited, installed_deps, installed_keys, files_lower
                    )
            except Exception:
                continue

    def _resolve_curseforge_deps(self, inst, dependencies, target_dir, visited, installed_deps, manual_deps, installed_keys, files_lower):
        """Рекурсивно скачивает обязательные (relationType == 3) зависимости мода с CurseForge."""
        api_key = self._get_cf_api_key()
        if not api_key:
            return
        headers = {
            "x-api-key": api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        for dep in dependencies or []:
            if len(installed_deps) >= 15:
                break
            if dep.get("relationType") != 3:  # 3 = RequiredDependency
                continue
            dep_mod_id = dep.get("modId")
            if not dep_mod_id:
                continue

            dep_key = f"CurseForge:{dep_mod_id}"
            if dep_key in visited or dep_key in installed_keys:
                continue
            visited.add(dep_key)

            try:
                params = {"pageSize": 5}
                if not inst["version"].startswith(("a", "b", "c", "rd-", "inf-")):
                    params["gameVersion"] = inst["version"]
                params["modLoaderType"] = CF_LOADER_MAP.get(inst["loader"], 0)

                r = _get_http().get(
                    f"https://api.curseforge.com/v1/mods/{dep_mod_id}/files",
                    params=params,
                    headers=headers,
                    timeout=15,
                )
                if r.status_code != 200:
                    continue
                files = r.json().get("data") or []
                if not files:
                    continue

                target = files[0]
                dep_url = target.get("downloadUrl")
                dep_fname = target.get("fileName") or f"cf_dep_{dep_mod_id}.jar"

                if not dep_url:
                    manual_deps.append(str(dep_mod_id))
                    continue

                if dep_fname.lower() in files_lower or (dep_fname.lower() + ".disabled") in files_lower:
                    inst.setdefault("installed_projects", {})[dep_key] = dep_fname
                    installed_keys.add(dep_key)
                else:
                    self._set_status(f"Зависимость: {dep_fname}...", 0.8, True)
                    self._download_file(dep_url, os.path.join(target_dir, dep_fname))
                    inst.setdefault("installed_projects", {})[dep_key] = dep_fname
                    installed_keys.add(dep_key)
                    files_lower.append(dep_fname.lower())
                    installed_deps.append({"id": dep_mod_id, "source": "CurseForge", "filename": dep_fname})

                sub_deps = target.get("dependencies") or []
                if sub_deps:
                    self._resolve_curseforge_deps(
                        inst, sub_deps, target_dir, visited, installed_deps, manual_deps, installed_keys, files_lower
                    )
            except Exception:
                continue

    def install_content(self, item, inst_id):
        inst = self._find_instance(inst_id)
        if not inst:
            return {"status": "error", "error": "Сборка не найдена"}

        ctype = item.get("content_type", "mod")
        folder_name = CONTENT_FOLDERS.get(ctype, "mods")
        auto_deps = bool(self._config.get("auto_install_deps", True)) and ctype == "mod"

        try:
            target_dir = os.path.join(INSTANCES_DIR, inst["id"], folder_name)
            os.makedirs(target_dir, exist_ok=True)
            self._set_status(f"Поиск файла {item['title']}...", 0.25, True)

            raw_deps = []
            if item["source"] == "Modrinth":
                params = {}
                if ctype == "mod":
                    if inst["loader"].lower() == "quilt":
                        params["loaders"] = json.dumps(["quilt", "fabric"])
                    else:
                        params["loaders"] = json.dumps([inst["loader"].lower()])
                    params["game_versions"] = json.dumps([inst["version"]])
                elif ctype == "datapack":
                    params["loaders"] = json.dumps(["datapack"])
                    params["game_versions"] = json.dumps([inst["version"]])
                else:
                    params["game_versions"] = json.dumps([inst["version"]])

                r = _get_http().get(
                    f"https://api.modrinth.com/v2/project/{item['id']}/version",
                    params=params,
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                r.raise_for_status()
                versions = r.json()

                if not versions and ctype in ("shader", "resourcepack"):
                    r2 = _get_http().get(
                        f"https://api.modrinth.com/v2/project/{item['id']}/version",
                        headers={"User-Agent": USER_AGENT},
                        timeout=15,
                    )
                    r2.raise_for_status()
                    versions = r2.json()

                if not versions:
                    return {"status": "error", "error": "Нет совместимого файла на Modrinth"}
                chosen_ver = versions[0]
                raw_deps = chosen_ver.get("dependencies") or []
                files = chosen_ver.get("files", [])
                primary = next((f for f in files if f.get("primary")), files[0])
                download_url, filename = primary["url"], primary["filename"]
            else:
                cf_key = self._get_cf_api_key()
                if not cf_key:
                    return {"status": "error", "error": "Не указан CurseForge API Key"}
                params = {"pageSize": 5}
                if not inst["version"].startswith(("a", "b", "c", "rd-", "inf-")):
                    params["gameVersion"] = inst["version"]
                if ctype == "mod":
                    params["modLoaderType"] = CF_LOADER_MAP.get(inst["loader"], 0)

                headers = {
                    "x-api-key": cf_key,
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                }
                r = _get_http().get(
                    f"https://api.curseforge.com/v1/mods/{item['id']}/files",
                    params=params,
                    headers=headers,
                    timeout=15,
                )
                r.raise_for_status()
                files = r.json().get("data", [])
                if not files and ctype in ("shader", "resourcepack"):
                    r2 = _get_http().get(
                        f"https://api.curseforge.com/v1/mods/{item['id']}/files",
                        params={"pageSize": 5},
                        headers=headers,
                        timeout=15,
                    )
                    r2.raise_for_status()
                    files = r2.json().get("data", [])

                if not files:
                    return {"status": "error", "error": "Нет совместимого файла на CurseForge"}
                target = files[0]
                raw_deps = target.get("dependencies") or []
                download_url = target.get("downloadUrl")
                ext = ".jar" if ctype == "mod" else ".zip"
                filename = target.get("fileName", f"{item['slug']}{ext}")

                if not download_url:
                    self._set_status("Автор ограничил загрузку — скачай вручную", 0, False)
                    return {"status": "manual", "url": item["web_url"], "subfolder": folder_name}

            self._set_status(f"Скачивание {filename}...", 0.55, True)
            self._download_file(download_url, os.path.join(target_dir, filename))

            main_key = f"{item['source']}:{item['id']}"
            inst.setdefault("installed_projects", {})[main_key] = filename

            installed_deps = []
            manual_deps = []
            if auto_deps and raw_deps:
                files_lower, tracked_keys = self._get_installed_signatures(inst, "mod")
                if filename.lower() not in files_lower:
                    files_lower.append(filename.lower())
                tracked_keys.add(main_key)
                visited = {main_key}

                if item["source"] == "Modrinth":
                    self._resolve_modrinth_deps(
                        inst, raw_deps, target_dir, visited, installed_deps, tracked_keys, files_lower
                    )
                else:
                    self._resolve_curseforge_deps(
                        inst, raw_deps, target_dir, visited, installed_deps, manual_deps, tracked_keys, files_lower
                    )

            self._save_config()

            dep_suffix = f" (+{len(installed_deps)} завис.)" if installed_deps else ""
            self._set_status(f"Установлено в {folder_name}: {filename}{dep_suffix}", 1.0, False)
            return {
                "status": "ok",
                "filename": filename,
                "folder": folder_name,
                "deps_count": len(installed_deps),
                "deps": installed_deps,
                "manual_deps": manual_deps,
            }
        except Exception as e:
            self._set_status("Ошибка установки", 0, False)
            return {"status": "error", "error": str(e)}

    # ---------------- 3D-скин (текстура без CORS + локальный .png) ----------------
    def get_skin_texture(self, username, account_type=None):
        """Загружает сырую текстуру скина (64x64 / 64x32 PNG) как data URI для 3D-вьювера (Mojang + Ely.by)."""
        from urllib.parse import quote
        clean = (username or "").strip() or "Player"
        atype = account_type
        if not atype:
            active = self._get_active_account()
            if active["username"].lower() == clean.lower():
                atype = active.get("type", "offline")
            else:
                matched = next((a for a in self._get_accounts() if a["username"].lower() == clean.lower()), None)
                atype = matched.get("type", "offline") if matched else "offline"

        cache_key = (atype, clean.lower())
        if cache_key in self._skin_cache:
            cached = self._skin_cache[cache_key]
            return {"ok": True, "uri": cached["uri"], "slim": cached.get("slim", False), "source": cached.get("source", atype), "username": clean}

        qname = quote(clean, safe="")
        # 1. Для Ely.by-аккаунтов сначала тянем оригинальный скин и модель (classic/slim) с skinsystem.ely.by
        if atype == "elyby":
            try:
                rt = _get_http().get(
                    f"https://skinsystem.ely.by/textures/{qname}",
                    headers={"User-Agent": USER_AGENT},
                    timeout=8,
                )
                if rt.status_code == 200:
                    tdata = rt.json() or {}
                    skin_info = tdata.get("SKIN") or {}
                    surl = str(skin_info.get("url") or "").strip()
                    is_slim = str((skin_info.get("metadata") or {}).get("model") or "").lower() == "slim"
                    if surl:
                        rs = _get_http().get(surl, headers={"User-Agent": USER_AGENT}, timeout=8)
                        if rs.status_code == 200 and rs.content.startswith(b"\x89PNG\r\n\x1a\n"):
                            uri = _image_bytes_to_data_uri(rs.content, max_bytes=512 * 1024)
                            if uri:
                                self._skin_cache[cache_key] = {"uri": uri, "slim": is_slim, "source": "elyby"}
                                return {"ok": True, "uri": uri, "slim": is_slim, "source": "elyby", "username": clean}
            except Exception:
                pass
            try:
                rs2 = _get_http().get(
                    f"https://skinsystem.ely.by/skins/{qname}.png",
                    headers={"User-Agent": USER_AGENT},
                    timeout=8,
                )
                if rs2.status_code == 200 and rs2.content.startswith(b"\x89PNG\r\n\x1a\n"):
                    uri = _image_bytes_to_data_uri(rs2.content, max_bytes=512 * 1024)
                    if uri:
                        self._skin_cache[cache_key] = {"uri": uri, "slim": False, "source": "elyby"}
                        return {"ok": True, "uri": uri, "slim": False, "source": "elyby", "username": clean}
            except Exception:
                pass

        # 2. Стандартные источники (Mojang / оффлайн / фоллбэк)
        urls = [
            f"https://mc-heads.net/skin/{qname}",
            f"https://minotar.net/skin/{qname}",
        ]
        for u in urls:
            try:
                r = _get_http().get(u, headers={"User-Agent": USER_AGENT}, timeout=8)
                if r.status_code == 200 and r.content.startswith(b"\x89PNG\r\n\x1a\n"):
                    uri = _image_bytes_to_data_uri(r.content, max_bytes=512 * 1024)
                    if uri:
                        self._skin_cache[cache_key] = {"uri": uri, "slim": False, "source": "mojang"}
                        return {"ok": True, "uri": uri, "slim": False, "source": "mojang", "username": clean}
            except Exception:
                continue
        return {"ok": False, "uri": "", "slim": False, "source": atype, "username": clean}

    def pick_local_skin_file(self):
        """Позволяет примерить локальный .png скин в 3D-вьювере."""
        if not self._window:
            return {"ok": False}
        dialog_type = getattr(webview, "OPEN_DIALOG", None)
        if dialog_type is None and hasattr(webview, "FileDialog"):
            dialog_type = webview.FileDialog.OPEN
        try:
            res = self._window.create_file_dialog(
                dialog_type,
                allow_multiple=False,
                file_types=("Скин Minecraft (*.png)",),
            )
            if not res:
                return {"ok": False, "cancelled": True}
            src = res[0]
            with open(src, "rb") as f:
                uri = _image_bytes_to_data_uri(f.read(), max_bytes=512 * 1024)
            if not uri:
                return {"ok": False, "error": "Выбери корректный PNG-файл скина"}
            return {"ok": True, "uri": uri}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- Полноценная страница проекта (галерея, описание, чейнджлог) ----------------
    def get_project_details(self, item, inst_id):
        """Загружает полную карточку мода/ресурспака/шейдера с галереей, описанием и чейнджлогом."""
        source = item.get("source", "Modrinth")
        ctype = item.get("content_type", "mod")
        inst = self._find_instance(inst_id)

        try:
            if source == "Modrinth":
                r = _get_http().get(
                    f"https://api.modrinth.com/v2/project/{item['id']}",
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                r.raise_for_status()
                p = r.json()

                gallery = []
                for g in sorted(p.get("gallery") or [], key=lambda x: (not x.get("featured", False), x.get("ordering", 0))):
                    if g.get("url") or g.get("raw_url"):
                        thumb = g.get("url") or g.get("raw_url")
                        full = g.get("raw_url") or g.get("url")
                        gallery.append({
                            "url": thumb,
                            "raw_url": full,
                            "title": g.get("title") or "",
                            "description": g.get("description") or "",
                            "featured": bool(g.get("featured")),
                        })

                # Несколько последних версий с чейнджлогами
                changelogs = []
                try:
                    vparams = {}
                    if inst and ctype == "mod":
                        l_low = inst["loader"].lower()
                        vparams["loaders"] = json.dumps(["quilt", "fabric"] if l_low == "quilt" else [l_low])
                        vparams["game_versions"] = json.dumps([inst["version"]])
                    rv = _get_http().get(
                        f"https://api.modrinth.com/v2/project/{item['id']}/version",
                        params=vparams,
                        headers={"User-Agent": USER_AGENT},
                        timeout=12,
                    )
                    if rv.status_code == 200:
                        vlist = rv.json() or []
                        if not vlist and vparams:
                            rv2 = _get_http().get(
                                f"https://api.modrinth.com/v2/project/{item['id']}/version",
                                headers={"User-Agent": USER_AGENT},
                                timeout=12,
                            )
                            if rv2.status_code == 200:
                                vlist = rv2.json() or []
                        for v in vlist[:10]:
                            dp = str(v.get("date_published") or "")[:10]
                            if len(dp) == 10 and dp[4] == "-":
                                dp = f"{dp[8:10]}.{dp[5:7]}.{dp[0:4]}"
                            changelogs.append({
                                "name": v.get("name") or v.get("version_number") or "",
                                "version_number": v.get("version_number") or "",
                                "channel": (v.get("version_type") or "release").lower(),
                                "date": dp,
                                "changelog": (v.get("changelog") or "").strip(),
                                "game_versions": (v.get("game_versions") or [])[:5],
                            })
                except Exception:
                    pass

                lic = p.get("license") or {}
                upd = str(p.get("updated") or "")[:10]
                if len(upd) == 10 and upd[4] == "-":
                    upd = f"{upd[8:10]}.{upd[5:7]}.{upd[0:4]}"

                return {
                    "ok": True,
                    "source": "Modrinth",
                    "title": p.get("title") or item.get("title", ""),
                    "description": p.get("description") or item.get("description", ""),
                    "body_format": "markdown",
                    "body": p.get("body") or "",
                    "icon_url": p.get("icon_url") or item.get("icon_url", ""),
                    "downloads": int(p.get("downloads") or item.get("downloads", 0)),
                    "followers": int(p.get("followers") or 0),
                    "categories": p.get("categories") or [],
                    "client_side": p.get("client_side") or "",
                    "server_side": p.get("server_side") or "",
                    "license": lic.get("name") or lic.get("id") or "",
                    "updated": upd,
                    "links": {
                        "web": item.get("web_url") or f"https://modrinth.com/{ctype}/{p.get('slug', item['id'])}",
                        "source": p.get("source_url") or "",
                        "wiki": p.get("wiki_url") or "",
                        "issues": p.get("issues_url") or "",
                        "discord": p.get("discord_url") or "",
                    },
                    "gallery": gallery,
                    "changelogs": changelogs,
                }
            else:
                cf_key = self._get_cf_api_key()
                if not cf_key:
                    return {"ok": False, "error": "Не указан CurseForge API Key"}
                headers = {
                    "x-api-key": cf_key,
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                }
                r = _get_http().get(f"https://api.curseforge.com/v1/mods/{item['id']}", headers=headers, timeout=15)
                if r.status_code == 403:
                    return {"ok": False, "error": "CurseForge отклонил API-ключ (403)."}
                r.raise_for_status()
                p = r.json().get("data") or {}

                body_html = ""
                try:
                    rd = _get_http().get(f"https://api.curseforge.com/v1/mods/{item['id']}/description", headers=headers, timeout=12)
                    if rd.status_code == 200:
                        body_html = rd.json().get("data") or ""
                except Exception:
                    pass

                gallery = []
                for sc in p.get("screenshots") or []:
                    if sc.get("url"):
                        gallery.append({
                            "url": sc["url"],
                            "title": sc.get("title") or "",
                            "description": sc.get("description") or "",
                            "featured": False,
                        })

                changelogs = []
                chan_map = {1: "release", 2: "beta", 3: "alpha"}
                for f in (p.get("latestFiles") or [])[:8]:
                    dp = str(f.get("fileDate") or "")[:10]
                    if len(dp) == 10 and dp[4] == "-":
                        dp = f"{dp[8:10]}.{dp[5:7]}.{dp[0:4]}"
                    changelogs.append({
                        "name": f.get("displayName") or f.get("fileName") or "",
                        "version_number": f.get("fileName") or "",
                        "channel": chan_map.get(int(f.get("releaseType") or 1), "release"),
                        "date": dp,
                        "changelog": "",
                        "game_versions": [g for g in (f.get("gameVersions") or []) if re.match(r"^\d+\.\d+", str(g))][:5],
                    })

                links = p.get("links") or {}
                upd = str(p.get("dateModified") or "")[:10]
                if len(upd) == 10 and upd[4] == "-":
                    upd = f"{upd[8:10]}.{upd[5:7]}.{upd[0:4]}"
                logo = p.get("logo") or {}

                return {
                    "ok": True,
                    "source": "CurseForge",
                    "title": p.get("name") or item.get("title", ""),
                    "description": p.get("summary") or item.get("description", ""),
                    "body_format": "html",
                    "body": body_html,
                    "icon_url": logo.get("url") or logo.get("thumbnailUrl") or item.get("icon_url", ""),
                    "downloads": int(p.get("downloadCount") or item.get("downloads", 0)),
                    "followers": int(p.get("thumbsUpCount") or 0),
                    "categories": [c.get("name", "") for c in (p.get("categories") or []) if c.get("name")],
                    "client_side": "",
                    "server_side": "",
                    "license": "",
                    "updated": upd,
                    "links": {
                        "web": links.get("websiteUrl") or item.get("web_url", ""),
                        "source": links.get("sourceUrl") or "",
                        "wiki": links.get("wikiUrl") or "",
                        "issues": links.get("issuesUrl") or "",
                        "discord": "",
                    },
                    "gallery": gallery,
                    "changelogs": changelogs,
                }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- Выбор конкретной версии мода / пака ----------------
    def get_project_versions(self, item, inst_id, show_all=False):
        """Возвращает список версий проекта (Modrinth / CurseForge) для выбора пользователем."""
        inst = self._find_instance(inst_id)
        if not inst:
            return {"ok": False, "error": "Сборка не найдена"}

        ctype = item.get("content_type", "mod")
        source = item.get("source", "Modrinth")
        mc_ver = inst["version"]
        loader_low = inst["loader"].lower()
        show_all = bool(show_all)

        try:
            if source == "Modrinth":
                params = {}
                if not show_all:
                    if ctype == "mod":
                        if loader_low == "quilt":
                            params["loaders"] = json.dumps(["quilt", "fabric"])
                        else:
                            params["loaders"] = json.dumps([loader_low])
                        params["game_versions"] = json.dumps([mc_ver])
                    elif ctype == "datapack":
                        params["loaders"] = json.dumps(["datapack"])
                        params["game_versions"] = json.dumps([mc_ver])
                    elif not mc_ver.startswith(("a", "b", "c", "rd-", "inf-")):
                        params["game_versions"] = json.dumps([mc_ver])

                r = _get_http().get(
                    f"https://api.modrinth.com/v2/project/{item['id']}/version",
                    params=params,
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                r.raise_for_status()
                raw_list = r.json() or []

                out = []
                for v in raw_list[:60]:
                    files = v.get("files") or []
                    if not files:
                        continue
                    primary = next((f for f in files if f.get("primary")), files[0])
                    gvers = v.get("game_versions") or []
                    vloaders = [str(l).lower() for l in (v.get("loaders") or [])]

                    if ctype == "mod":
                        allowed_loaders = {"quilt", "fabric"} if loader_low == "quilt" else {loader_low}
                        compat = (mc_ver in gvers) and bool(allowed_loaders.intersection(vloaders))
                    else:
                        compat = (mc_ver in gvers) if gvers else True

                    dp = str(v.get("date_published") or "")[:10]
                    if len(dp) == 10 and dp[4] == "-":
                        dp = f"{dp[8:10]}.{dp[5:7]}.{dp[0:4]}"

                    out.append({
                        "source": "Modrinth",
                        "version_id": v.get("id", ""),
                        "name": v.get("name") or v.get("version_number") or primary.get("filename", ""),
                        "version_number": v.get("version_number") or "",
                        "channel": (v.get("version_type") or "release").lower(),
                        "date": dp,
                        "downloads": int(v.get("downloads") or 0),
                        "game_versions": gvers[:6],
                        "loaders": v.get("loaders") or [],
                        "filename": primary.get("filename", ""),
                        "size_kb": round(int(primary.get("size") or 0) / 1024),
                        "download_url": primary.get("url", ""),
                        "dependencies": v.get("dependencies") or [],
                        "compatible": compat,
                    })
                return {"ok": True, "versions": out}

            else:
                cf_key = self._get_cf_api_key()
                if not cf_key:
                    return {"ok": False, "error": "Не указан CurseForge API Key"}

                params = {"pageSize": 50}
                if not show_all:
                    if not mc_ver.startswith(("a", "b", "c", "rd-", "inf-")):
                        params["gameVersion"] = mc_ver
                    if ctype == "mod":
                        params["modLoaderType"] = CF_LOADER_MAP.get(inst["loader"], 0)

                r = _get_http().get(
                    f"https://api.curseforge.com/v1/mods/{item['id']}/files",
                    params=params,
                    headers={
                        "x-api-key": cf_key,
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=15,
                )
                if r.status_code == 403:
                    return {"ok": False, "error": "CurseForge отклонил API-ключ (403)."}
                r.raise_for_status()
                raw_files = r.json().get("data") or []

                chan_map = {1: "release", 2: "beta", 3: "alpha"}
                out = []
                for f in raw_files[:60]:
                    gvers = f.get("gameVersions") or []
                    gvers_low = [str(g).lower() for g in gvers]
                    mc_tags = [g for g in gvers if re.match(r"^\d+\.\d+", str(g))]
                    loader_tags = [g for g in gvers if str(g).lower() in ("fabric", "forge", "neoforge", "quilt")]

                    if ctype == "mod":
                        compat = (mc_ver in gvers) and (loader_low in gvers_low or not loader_tags)
                    else:
                        compat = (mc_ver in gvers) if mc_tags else True

                    dp = str(f.get("fileDate") or "")[:10]
                    if len(dp) == 10 and dp[4] == "-":
                        dp = f"{dp[8:10]}.{dp[5:7]}.{dp[0:4]}"

                    out.append({
                        "source": "CurseForge",
                        "version_id": str(f.get("id", "")),
                        "name": f.get("displayName") or f.get("fileName", ""),
                        "version_number": "",
                        "channel": chan_map.get(int(f.get("releaseType") or 1), "release"),
                        "date": dp,
                        "downloads": int(f.get("downloadCount") or 0),
                        "game_versions": mc_tags[:6],
                        "loaders": loader_tags,
                        "filename": f.get("fileName") or "",
                        "size_kb": round(int(f.get("fileLength") or 0) / 1024),
                        "download_url": f.get("downloadUrl") or "",
                        "dependencies": f.get("dependencies") or [],
                        "compatible": compat,
                    })
                return {"ok": True, "versions": out}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def install_specific_version(self, item, inst_id, version_entry):
        """Устанавливает конкретную выбранную версию мода/пака (+ обязательные зависимости)."""
        inst = self._find_instance(inst_id)
        if not inst:
            return {"status": "error", "error": "Сборка не найдена"}

        ctype = item.get("content_type", "mod")
        folder_name = CONTENT_FOLDERS.get(ctype, "mods")
        auto_deps = bool(self._config.get("auto_install_deps", True)) and ctype == "mod"

        try:
            target_dir = os.path.join(INSTANCES_DIR, inst["id"], folder_name)
            os.makedirs(target_dir, exist_ok=True)

            download_url = (version_entry.get("download_url") or "").strip()
            ext = ".jar" if ctype == "mod" else ".zip"
            filename = (version_entry.get("filename") or f"{item.get('slug', 'file')}{ext}").strip()

            if not download_url:
                self._set_status("Автор ограничил загрузку — скачай вручную", 0, False)
                return {"status": "manual", "url": item.get("web_url", ""), "subfolder": folder_name}

            self._set_status(f"Скачивание {filename}...", 0.55, True)
            self._download_file(download_url, os.path.join(target_dir, filename))

            main_key = f"{item['source']}:{item['id']}"
            inst.setdefault("installed_projects", {})[main_key] = filename

            installed_deps = []
            manual_deps = []
            raw_deps = version_entry.get("dependencies") or []
            if auto_deps and raw_deps:
                files_lower, tracked_keys = self._get_installed_signatures(inst, "mod")
                if filename.lower() not in files_lower:
                    files_lower.append(filename.lower())
                tracked_keys.add(main_key)
                visited = {main_key}

                if item["source"] == "Modrinth":
                    self._resolve_modrinth_deps(
                        inst, raw_deps, target_dir, visited, installed_deps, tracked_keys, files_lower
                    )
                else:
                    self._resolve_curseforge_deps(
                        inst, raw_deps, target_dir, visited, installed_deps, manual_deps, tracked_keys, files_lower
                    )

            self._save_config()

            dep_suffix = f" (+{len(installed_deps)} завис.)" if installed_deps else ""
            self._set_status(f"Установлено в {folder_name}: {filename}{dep_suffix}", 1.0, False)
            return {
                "status": "ok",
                "filename": filename,
                "folder": folder_name,
                "deps_count": len(installed_deps),
                "deps": installed_deps,
                "manual_deps": manual_deps,
            }
        except Exception as e:
            self._set_status("Ошибка установки версии", 0, False)
            return {"status": "error", "error": str(e)}

    # ---------------- Живой пинг Minecraft-сервера (SLP-протокол) ----------------
    def ping_mc_server(self, address):
        """Опрашивает Minecraft-сервер по стандартному TCP SLP-протоколу (онлайн, макс, пинг, MOTD, иконка)."""
        import socket
        import struct

        raw = (address or "").strip()
        if not raw:
            return {"ok": False, "error": "Адрес не указан"}

        host = raw
        port = 25565
        if ":" in raw and not raw.startswith("["):
            h_part, p_part = raw.rsplit(":", 1)
            if p_part.isdigit():
                host = h_part.strip()
                port = int(p_part)

        sock = None
        try:
            t0 = time.perf_counter()
            sock = socket.create_connection((host, port), timeout=4.0)
            sock.settimeout(4.0)
            connect_ms = round((time.perf_counter() - t0) * 1000)

            host_bytes = host.encode("utf-8")
            handshake_data = (
                b"\x00"
                + _pack_varint(767)
                + _pack_varint(len(host_bytes))
                + host_bytes
                + struct.pack(">H", port)
                + _pack_varint(1)
            )
            sock.sendall(_pack_varint(len(handshake_data)) + handshake_data)
            sock.sendall(b"\x01\x00")

            _pkt_len = _read_varint_sock(sock)
            pkt_id = _read_varint_sock(sock)
            if pkt_id != 0:
                return {"ok": False, "error": "Сервер вернул нестандартный ответ"}

            str_len = _read_varint_sock(sock)
            payload = json.loads(_recv_exact(sock, str_len).decode("utf-8", errors="replace"))

            ping_ms = connect_ms
            try:
                t_ping = time.perf_counter()
                ts_bytes = struct.pack(">q", int(time.time() * 1000))
                sock.sendall(_pack_varint(1 + len(ts_bytes)) + b"\x01" + ts_bytes)
                _pong_len = _read_varint_sock(sock)
                _pong_id = _read_varint_sock(sock)
                _recv_exact(sock, 8)
                ping_ms = max(1, round((time.perf_counter() - t_ping) * 1000))
            except Exception:
                pass

            players = payload.get("players") or {}
            ver = (payload.get("version") or {}).get("name") or ""
            motd = _strip_mc_motd(payload.get("description") or "")
            favicon = payload.get("favicon") or ""
            if favicon and not str(favicon).startswith("data:image/"):
                favicon = ""

            return {
                "ok": True,
                "online": int(players.get("online") or 0),
                "max": int(players.get("max") or 0),
                "ping_ms": ping_ms,
                "version": re.sub(r"§.", "", str(ver)).strip()[:40],
                "motd": motd.replace("\n", " ").strip()[:140],
                "favicon": favicon,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    # ---------------- Умный анализатор крашей в логах ----------------
    def analyze_instance_logs(self, inst_id):
        lines = self._logs.get(inst_id, [])
        return _analyze_mc_logs(lines)

    # ---------------- Экспорт сборки в .mrpack (поделиться с другом) ----------------
    def export_instance_mrpack(self, inst_id, out_path=None):
        """Экспортирует сборку в стандартный .mrpack (Modrinth index + overrides)."""
        import zipfile

        inst = self._find_instance(inst_id)
        if not inst:
            return {"ok": False, "error": "Сборка не найдена"}

        if not out_path:
            if not self._window:
                return {"ok": False, "error": "Окно не готово"}
            dialog_type = getattr(webview, "SAVE_DIALOG", None)
            if dialog_type is None and hasattr(webview, "FileDialog"):
                dialog_type = webview.FileDialog.SAVE
            safe_fn = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ._ -]", "_", inst["name"]).strip() or "modpack"
            try:
                res = self._window.create_file_dialog(
                    dialog_type,
                    save_filename=f"{safe_fn}.mrpack",
                    file_types=("Modrinth Modpack (*.mrpack)",),
                )
                if not res:
                    return {"ok": False, "cancelled": True}
                out_path = res[0] if isinstance(res, (list, tuple)) else str(res)
                if not out_path.lower().endswith(".mrpack"):
                    out_path += ".mrpack"
            except Exception as e:
                return {"ok": False, "error": str(e)}

        try:
            inst_dir = os.path.join(INSTANCES_DIR, inst["id"])
            mods_dir = os.path.join(inst_dir, "mods")
            self._set_status(f"Подготовка экспорта «{inst['name']}»...", 0.15, True)

            mod_entries = []
            sha1_list = []
            if os.path.isdir(mods_dir):
                for fn in sorted(os.listdir(mods_dir)):
                    fp = os.path.join(mods_dir, fn)
                    if fn.endswith(".jar") and os.path.isfile(fp):
                        s1, s512 = _file_hashes(fp)
                        mod_entries.append((fn, fp, s1, s512, os.path.getsize(fp)))
                        sha1_list.append(s1)

            mr_lookup = {}
            if sha1_list:
                self._set_status(f"Поиск {len(sha1_list)} модов в базе Modrinth...", 0.35, True)
                try:
                    r = _get_http().post(
                        "https://api.modrinth.com/v2/version_files",
                        json={"hashes": sha1_list, "algorithm": "sha1"},
                        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
                        timeout=20,
                    )
                    if r.status_code == 200:
                        mr_lookup = r.json() or {}
                except Exception:
                    mr_lookup = {}

            index_files = []
            override_mods = []
            for fn, fp, s1, s512, fsize in mod_entries:
                vobj = mr_lookup.get(s1)
                dl_url = ""
                if isinstance(vobj, dict):
                    for fitem in vobj.get("files") or []:
                        f_s1 = (fitem.get("hashes") or {}).get("sha1", "").lower()
                        if f_s1 == s1.lower() and fitem.get("url"):
                            dl_url = fitem["url"]
                            break
                if dl_url:
                    index_files.append({
                        "path": f"mods/{fn}",
                        "hashes": {"sha1": s1, "sha512": s512},
                        "env": {"client": "required", "server": "optional"},
                        "downloads": [dl_url],
                        "fileSize": fsize,
                    })
                else:
                    override_mods.append((fn, fp))

            deps = {"minecraft": inst["version"]}
            if inst["loader"] == "Fabric":
                lver = "0.16.9"
                vid = inst.get("installed_version_id") or ""
                m = re.match(r"fabric-loader-([^-]+)-", vid)
                if m:
                    lver = m.group(1)
                deps["fabric-loader"] = lver
            elif inst["loader"] == "Quilt":
                qver = "0.26.0"
                vid = inst.get("installed_version_id") or ""
                m = re.match(r"quilt-loader-([^-]+)-", vid)
                if m:
                    qver = m.group(1)
                deps["quilt-loader"] = qver
            elif inst["loader"] == "Forge":
                fver = ""
                vid = inst.get("installed_version_id") or ""
                if "-forge-" in vid:
                    fver = vid.split("-forge-", 1)[1]
                deps["forge"] = fver or "latest"
            elif inst["loader"] == "NeoForge":
                nfver = ""
                vid = inst.get("installed_version_id") or ""
                if vid.startswith("neoforge-"):
                    nfver = vid[len("neoforge-"):]
                deps["neoforge"] = nfver or "latest"

            manifest = {
                "formatVersion": 1,
                "game": "minecraft",
                "versionId": "1.0.0",
                "name": inst["name"],
                "summary": f"Exported from GreenPrism Launcher ({inst['loader']} {inst['version']})",
                "files": index_files,
                "dependencies": deps,
            }

            self._set_status("Упаковка архива .mrpack...", 0.7, True)
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("modrinth.index.json", json.dumps(manifest, ensure_ascii=False, indent=2))

                for fn, fp in override_mods:
                    zf.write(fp, f"overrides/mods/{fn}")

                for single in ("options.txt", "servers.dat"):
                    sp = os.path.join(inst_dir, single)
                    if os.path.isfile(sp):
                        zf.write(sp, f"overrides/{single}")

                for folder in ("config", "resourcepacks", "shaderpacks", "datapacks"):
                    fdir = os.path.join(inst_dir, folder)
                    if os.path.isdir(fdir):
                        for root, _dirs, files in os.walk(fdir):
                            for fn in files:
                                fp = os.path.join(root, fn)
                                rel = os.path.relpath(fp, inst_dir).replace("\\", "/")
                                try:
                                    zf.write(fp, f"overrides/{rel}")
                                except Exception:
                                    pass

            size_mb = round(os.path.getsize(out_path) / (1024 * 1024), 2)
            self._set_status(f"Экспорт завершён: {os.path.basename(out_path)} ({size_mb} МБ)", 1.0, False)
            return {
                "ok": True,
                "path": out_path,
                "filename": os.path.basename(out_path),
                "size_mb": size_mb,
                "linked_mods": len(index_files),
                "bundled_mods": len(override_mods),
            }
        except Exception as e:
            self._set_status("Ошибка экспорта .mrpack", 0, False)
            return {"ok": False, "error": str(e)}

    # ---------------- GreenPrism Rescue (анализ крашей и безопасные действия) ----------------
    def rescue_increase_ram_and_relaunch(self, inst_id, new_ram_mb):
        inst = self._find_instance(inst_id)
        if not inst:
            return {"ok": False, "error": "Сборка не найдена"}
        try:
            mb = int(new_ram_mb)
        except Exception:
            return {"ok": False, "error": "Некорректное значение RAM"}
        mb = max(1024, min(16384, round(mb / 128) * 128))
        inst["ram_mb"] = mb
        self._save_config()
        res = self.launch_instance(inst["id"])
        res["ram_mb"] = mb
        res["state"] = self.get_state()
        return res

    def rescue_disable_mods(self, inst_id, filenames):
        inst = self._find_instance(inst_id)
        if not inst:
            return {"ok": False, "error": "Сборка не найдена"}
        try:
            inst_dir = _safe_join(INSTANCES_DIR, inst["id"])
            mods_dir = _safe_join(inst_dir, "mods")
        except ValueError:
            return {"ok": False, "error": "Некорректный путь сборки"}
        disabled = []
        errors = []
        for raw_fn in (filenames or []):
            fn = str(raw_fn or "").strip()
            if not fn or "/" in fn or "\\" in fn or fn in (".", ".."):
                continue
            try:
                src = _safe_join(mods_dir, fn)
                if not os.path.exists(src):
                    continue
                if fn.endswith(".disabled"):
                    disabled.append(fn)
                    continue
                dst_name = fn + ".disabled"
                dst = _safe_join(mods_dir, dst_name)
                if os.path.exists(dst):
                    base, ext = os.path.splitext(fn)
                    dst_name = f"{base}_{int(time.time())}{ext}.disabled"
                    dst = _safe_join(mods_dir, dst_name)
                os.rename(src, dst)
                disabled.append(fn)
            except Exception as e:
                errors.append(f"{fn}: {e}")
        return {"ok": bool(disabled), "disabled": disabled, "errors": errors, "state": self.get_state()}

    def get_last_crash_report(self):
        return self._last_crash_report

    def rescue_upload_mclogs(self, inst_id=None):
        import crash_rescue
        content = self._last_crash_full_log or ""
        if not content and inst_id:
            try:
                inst_dir = _safe_join(INSTANCES_DIR, inst_id)
                src = crash_rescue.collect_crash_sources(inst_dir, live_lines=self._logs.get(inst_id, []))
                content = src.get("full_log") or ""
            except Exception:
                content = "\n".join(self._logs.get(inst_id, []))
        return crash_rescue.upload_to_mclogs(content, http_session=_get_http(), user_agent=USER_AGENT)

    # ---------------- Запуск Minecraft + Логи + Время игры ----------------
    def launch_instance(self, inst_id, force_verify=False):
        if self._is_busy:
            return {"ok": False, "error": "Сейчас уже идёт установка или запуск игры"}
        inst = self._find_instance(inst_id)
        if not inst:
            return {"ok": False, "error": "Сборка не найдена"}

        self._is_busy = True
        self._user_stopped = False
        self._last_crash_report = None
        self._last_crash_full_log = ""
        self._running_inst_id = inst_id
        self._logs[inst_id] = [f"[GreenPrism] Запуск сборки «{inst['name']}» ({inst['loader']} {inst['version']})..."]
        self._emit_js("window.onStateChanged && window.onStateChanged()")
        threading.Thread(target=self._launch_worker, args=(inst, bool(force_verify)), daemon=True).start()
        return {"ok": True}

    def stop_game(self):
        proc = self._running_proc
        if proc and proc.poll() is None:
            try:
                self._user_stopped = True
                proc.terminate()
                self._set_status("Остановка процесса Minecraft...", 0, False)
                return True
            except Exception:
                pass
        return False

    def _launch_worker(self, inst, force_verify=False):
        import uuid
        import subprocess
        from datetime import datetime
        started_at = None
        was_hidden = False
        crash_payload = None
        try:
            mc_dir = self._get_mc_dir()
            os.makedirs(mc_dir, exist_ok=True)
            version = inst["version"]
            loader = inst["loader"]
            inst_dir = os.path.join(INSTANCES_DIR, inst["id"])
            for sub in ("mods", "resourcepacks", "shaderpacks", "datapacks"):
                os.makedirs(os.path.join(inst_dir, sub), exist_ok=True)

            callback = self._make_install_callback()

            installed_ids = self._get_installed_ids(force=True)
            if force_verify and loader in ("Fabric", "Forge", "NeoForge", "Quilt"):
                old_vid = self._resolve_existing_version(inst, installed_ids, strict=False)
                if old_vid:
                    self._push_log(inst["id"], f"[GreenPrism] Очистка старого профиля {old_vid} перед переустановкой...")
                    self._cleanup_broken_version(mc_dir, old_vid)
                    inst["installed_version_id"] = ""

            target_version = None if force_verify else self._resolve_existing_version(inst, installed_ids, strict=True)
            if target_version and not self._is_version_complete(mc_dir, target_version, loader, mc_ver=version):
                target_version = None

            if target_version:
                self._set_status(f"Быстрый запуск {inst['name']} ({loader} {version})...", 0.85, True)
                self._push_log(inst["id"], f"[GreenPrism] Найдена кэшированная версия: {target_version}")
            else:
                pinned_lv = str(inst.get("loader_version") or "").strip() or None
                if loader == "Fabric":
                    self._set_status(f"Установка Fabric {version}...", 0.05, True)
                    target_version = self._install_fabric(version, mc_dir, callback, inst["id"], loader_ver=pinned_lv)
                elif loader == "Quilt":
                    self._set_status(f"Установка Quilt {version}...", 0.05, True)
                    target_version = self._install_quilt(version, mc_dir, callback, inst["id"], loader_ver=pinned_lv)
                elif loader == "Forge":
                    self._set_status(f"Установка Forge {version}...", 0.05, True)
                    target_version = self._install_forge(version, mc_dir, callback, inst["id"], forge_ver=pinned_lv)
                elif loader == "NeoForge":
                    self._set_status(f"Установка NeoForge {version}...", 0.05, True)
                    target_version = self._install_neoforge(version, mc_dir, callback, inst["id"], nf_ver=pinned_lv)
                else:
                    self._set_status(f"Установка Minecraft {version}...", 0.05, True)
                    target_version = self._install_vanilla_version(version, mc_dir, callback, inst["id"])

            self._installed_cache = (None, None, set())
            if target_version and inst.get("installed_version_id") != target_version:
                inst["installed_version_id"] = target_version
            inst["last_played"] = datetime.now().isoformat(timespec="seconds")
            self._save_config()

            self._set_status("Подготовка запуска...", 1.0, True)
            import shutil
            import shlex
            active_acc = self._get_active_account()
            is_elyby = active_acc.get("type") == "elyby"
            if is_elyby:
                active_acc = self._refresh_elyby_account(active_acc, inst["id"])

            username = active_acc["username"]

            # Индивидуальная RAM сборки (если задана > 0), иначе глобальная
            inst_ram = int(inst.get("ram_mb") or 0)
            if inst_ram > 0:
                ram_mb = max(1024, min(16384, round(inst_ram / 128) * 128))
            else:
                ram_mb = int(self._config.get("ram_mb") or int(float(self._config.get("ram_gb", 4)) * 1024))
                ram_mb = max(1024, min(16384, round(ram_mb / 128) * 128))
            xms_mb = min(ram_mb, 2048)

            jvm_args = []
            if is_elyby:
                injector_jar = self._ensure_authlib_injector(inst["id"])
                jvm_args.append(f"-javaagent:{injector_jar}=ely.by")
                self._push_log(inst["id"], f"[Ely.by] Активен аккаунт {username} (authlib-injector подключён)")

            extra_low = (inst.get("jvm_args") or "").lower()
            if "usesystemproxies" not in extra_low and "proxyhost" not in extra_low:
                jvm_args.extend(_build_java_proxy_args())
            jvm_args.extend([f"-Xmx{ram_mb}M", f"-Xms{xms_mb}M"])
            extra_jvm_raw = (inst.get("jvm_args") or "").strip()
            if extra_jvm_raw:
                try:
                    extra_tokens = shlex.split(extra_jvm_raw, posix=(sys.platform != "win32"))
                except Exception:
                    extra_tokens = extra_jvm_raw.split()
                for tok in extra_tokens:
                    t = tok.strip()
                    if t and not t.startswith(("-Xmx", "-Xms")):
                        jvm_args.append(t)

            if is_elyby:
                player_uuid = _format_mc_uuid(active_acc.get("uuid") or "") or str(uuid.uuid3(uuid.NAMESPACE_DNS, f"OfflinePlayer:{username}"))
                auth_token = str(active_acc.get("access_token") or "")
            else:
                player_uuid = str(uuid.uuid3(uuid.NAMESPACE_DNS, f"OfflinePlayer:{username}"))
                auth_token = ""

            options = {
                "username": username,
                "uuid": player_uuid,
                "token": auth_token,
                "jvmArguments": jvm_args,
                "gameDirectory": inst_dir,
                "launcherName": "GreenPrism",
                "launcherVersion": "1.0",
            }

            win_w = int(inst.get("win_width") or 0)
            win_h = int(inst.get("win_height") or 0)
            if win_w >= 320 and win_h >= 240:
                options["customResolution"] = True
                options["resolutionWidth"] = str(win_w)
                options["resolutionHeight"] = str(win_h)
                self._push_log(inst["id"], f"[GreenPrism] Разрешение окна: {win_w}x{win_h}")

            # Индивидуальный путь к Java сборки имеет приоритет над глобальным
            _inst_java = (inst.get("java_path") or "").strip().strip('"')
            _cfg_java = _inst_java if _inst_java else (self._config.get("java_path") or "").strip().strip('"')
            if _cfg_java:
                if os.path.isfile(_cfg_java):
                    options["executablePath"] = _cfg_java
                    src_lbl = "сборки" if _inst_java else "настроек"
                    self._push_log(inst["id"], f"[GreenPrism] Используется Java из {src_lbl}: {_cfg_java}")
                else:
                    self._push_log(inst["id"], f"[GreenPrism] Указанный путь к Java не найден ({_cfg_java}), пробую рантайм по умолчанию...")

            srv = (inst.get("server_address") or "").strip()
            if srv:
                if _supports_quick_play(version):
                    options["quickPlayMultiplayer"] = srv
                else:
                    if ":" in srv:
                        host, port = srv.rsplit(":", 1)
                        options["server"] = host.strip()
                        options["port"] = port.strip()
                    else:
                        options["server"] = srv
                self._push_log(inst["id"], f"[GreenPrism] Автоподключение к серверу: {srv}")

            # Автоматически подтягиваем нужный JVM-рантайм (включая Java 21 для 1.20.5+/26.x)
            if not options.get("executablePath"):
                exe_from_rt = self._ensure_jvm_runtime(mc_dir, target_version, version, callback, inst["id"])
                if exe_from_rt:
                    options["executablePath"] = exe_from_rt

            # Авто-фикс профилей версий без поля "jar" (лечит отсутствие клиентского .jar в classpath для Fabric, Forge, NeoForge)
            if target_version:
                try:
                    fjp = os.path.join(mc_dir, "versions", str(target_version), f"{target_version}.json")
                    if os.path.isfile(fjp):
                        with open(fjp, "r", encoding="utf-8") as _ff:
                            fj = json.load(_ff)
                        parent_mc = str(fj.get("inheritsFrom") or version)
                        if not fj.get("jar") and parent_mc:
                            fj["jar"] = parent_mc
                            with open(fjp, "w", encoding="utf-8") as _ff:
                                json.dump(fj, _ff, ensure_ascii=False, indent=2)
                            self._push_log(inst["id"], f"[GreenPrism] Автоисправление профиля {loader}: jar={parent_mc}")
                except Exception:
                    pass

            command = _get_mclib().command.get_minecraft_command(target_version, mc_dir, options)
            if not command or not isinstance(command, (list, tuple)):
                raise RuntimeError("Не удалось сформировать команду запуска Minecraft.")

            command = list(command)

            # Гарантируем, что базовый клиентский JAR (versions/<parent_mc>/<parent_mc>.jar) реально присутствует в -cp для всех сборок
            parent_mc = version
            try:
                fjp = os.path.join(mc_dir, "versions", str(target_version), f"{target_version}.json")
                if os.path.isfile(fjp):
                    with open(fjp, "r", encoding="utf-8") as _ff:
                        fj = json.load(_ff)
                    parent_mc = str(fj.get("inheritsFrom") or version)
            except Exception:
                pass
            client_jar = os.path.join(mc_dir, "versions", str(parent_mc), f"{parent_mc}.jar")
            if os.path.isfile(client_jar) and os.path.getsize(client_jar) > 10 * 1024:
                command, cp_fixed = _ensure_jar_in_classpath(command, client_jar, target_version, mc_dir)
                if cp_fixed:
                    self._push_log(inst["id"], f"[GreenPrism] Исправлен classpath: добавлен {client_jar}")
            exe_path = command[0]
            if not os.path.isfile(exe_path):
                resolved = shutil.which(exe_path) or shutil.which("javaw") or shutil.which("java")
                if resolved and os.path.isfile(resolved):
                    command[0] = resolved
                    exe_path = resolved
                else:
                    raise FileNotFoundError(
                        f"Не найден файл запуска Java ({exe_path}). Используйте «Починить» или проверьте путь к Java."
                    )

            self._push_log(inst["id"], f"[GreenPrism] RAM: {ram_mb} МБ • Java: {exe_path}")

            self._set_status(f"Запущено: {inst['name']} ({loader} {version})", 1.0, True)
            self._emit_js("window.onStateChanged && window.onStateChanged()")

            kwargs = {"env": _clean_java_env()}
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                )
            else:
                kwargs["start_new_session"] = True

            # Пишем вывод игры в файл и читаем его хвост (tail), чтобы закрытие лаунчера
            # не обрывало pipe и не роняло запущенный процесс Minecraft.
            log_file_path = os.path.join(inst_dir, "greenprism_latest.log")
            started_at = time.time()
            self._running_started_at = started_at
            self._running_inst_ref = inst
            log_fh = None
            try:
                log_fh = open(log_file_path, "wb")
                proc = subprocess.Popen(
                    command,
                    cwd=inst_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    **kwargs,
                )
            except FileNotFoundError as _fnf:
                _cmd0 = command[0] if command else "<empty>"
                self._push_log(inst["id"], f"[GreenPrism] Детали WinError 2: exe={_cmd0}, exists={os.path.isfile(_cmd0) if command else False}, cwd={inst_dir}, cwd_exists={os.path.isdir(inst_dir)}")
                raise FileNotFoundError(f"{_fnf} (файл: {_cmd0})")
            finally:
                if log_fh is not None:
                    try:
                        log_fh.close()
                    except Exception:
                        pass
            self._running_proc = proc
            try:
                self._discord_rpc.set_playing(inst.get("name", "Сборка"), loader, version)
            except Exception:
                pass

            if bool(self._config.get("hide_on_launch", False)) and self._window:
                try:
                    self._window.hide()
                    was_hidden = True
                    self._push_log(inst["id"], "[GreenPrism] Окно лаунчера скрыто на время игры.")
                except Exception:
                    was_hidden = False

            try:
                with open(log_file_path, "r", encoding="utf-8", errors="replace") as rf:
                    while not self._closing:
                        line = rf.readline()
                        if line:
                            self._push_log(inst["id"], line.rstrip("\r\n"))
                        elif proc.poll() is not None:
                            for rem in rf:
                                self._push_log(inst["id"], rem.rstrip("\r\n"))
                            break
                        else:
                            time.sleep(0.06)
            except Exception:
                pass

            if self._closing:
                return

            code = proc.wait()
            self._push_log(inst["id"], f"[GreenPrism] Процесс завершён с кодом {code}")
            if code != 0 and not self._user_stopped and not self._closing:
                self._set_status(f"Игра завершилась с ошибкой (код {code})", 0, False)
                try:
                    import crash_rescue
                    report = crash_rescue.analyze_crash(
                        inst_dir,
                        inst,
                        code,
                        live_lines=list(self._logs.get(inst["id"], [])),
                        launch_ts=started_at,
                        global_ram_mb=int(self._config.get("ram_mb", 4096)),
                    )
                    self._last_crash_full_log = report.pop("full_log", "")
                    self._last_crash_report = report
                    crash_payload = report
                except Exception as _ce:
                    self._push_log(inst["id"], f"[GreenPrism] Ошибка анализатора крашей: {_ce}")
                    crash_payload = None
            else:
                self._set_status("Игра закрыта. Готов к запуску.", 0, False)
                crash_payload = None
        except Exception as e:
            crash_payload = None
            self._push_log(inst["id"], f"[ERROR] {e}")
            self._set_status("Ошибка запуска", 0, False)
            self._emit_js(f"window.showToast && window.showToast({json.dumps('Ошибка: ' + str(e))}, 'error')")
        finally:
            try:
                self._discord_rpc.set_launcher_menu()
            except Exception:
                pass
            if was_hidden and self._window and not self._closing:
                try:
                    self._window.show()
                except Exception:
                    pass
            if started_at and not self._closing:
                elapsed = int(max(0, time.time() - started_at))
                inst["playtime_sec"] = int(inst.get("playtime_sec", 0)) + elapsed
                self._save_config()
            self._running_started_at = None
            self._running_inst_ref = None
            self._running_proc = None
            self._running_inst_id = None
            self._is_busy = False
            self._emit_js("window.onStateChanged && window.onStateChanged()")
            if crash_payload is not None and not self._closing:
                # Передаём отчёт через RPC get_last_crash_report(), избегая экранирования eval() в pywebview
                self._emit_js("window.triggerCrashRescue && window.triggerCrashRescue()")


def main():
    import inspect

    api = LauncherAPI()
    html_path = resource_path(os.path.join("ui", "index.html"))
    use_frameless = bool(api._config.get("frameless", True))
    window = webview.create_window(
        "GreenPrism Launcher",
        url=html_path,
        js_api=api,
        width=1220,
        height=760,
        min_size=(960, 620),
        background_color="#14161a",
        frameless=use_frameless,
        easy_drag=False,
        resizable=True,
    )
    api._attach_window(window)

    # Постоянный кэш WebView2 + прямой выбор edgechromium на Windows ускоряют холодный старт окна
    start_kwargs = {"debug": False}
    try:
        sig_params = inspect.signature(webview.start).parameters
        if "private_mode" in sig_params:
            start_kwargs["private_mode"] = False
        if "storage_path" in sig_params:
            os.makedirs(WEBVIEW_CACHE_DIR, exist_ok=True)
            # Сбрасываем дисковый HTTP-кэш WebView2 при обновлении ui/index.html,
            # так как локальный Bottle-сервер pywebview не выставляет Cache-Control на static_file.
            try:
                import shutil
                st_ui = os.stat(html_path)
                ui_stamp = f"{st_ui.st_size}:{st_ui.st_mtime_ns}"
                stamp_file = os.path.join(WEBVIEW_CACHE_DIR, ".ui_stamp")
                old_stamp = ""
                if os.path.isfile(stamp_file):
                    with open(stamp_file, "r", encoding="utf-8") as sf:
                        old_stamp = sf.read().strip()
                if old_stamp != ui_stamp:
                    shutil.rmtree(WEBVIEW_CACHE_DIR, ignore_errors=True)
                    os.makedirs(WEBVIEW_CACHE_DIR, exist_ok=True)
                    with open(stamp_file, "w", encoding="utf-8") as sf:
                        sf.write(ui_stamp)
            except Exception:
                pass
            start_kwargs["storage_path"] = WEBVIEW_CACHE_DIR
        if sys.platform == "win32" and "gui" in sig_params:
            start_kwargs["gui"] = "edgechromium"
    except Exception:
        pass

    try:
        try:
            webview.start(**start_kwargs)
        except Exception:
            if not api._window_started and start_kwargs.get("gui") == "edgechromium":
                start_kwargs.pop("gui", None)
                webview.start(**start_kwargs)
            else:
                raise
    finally:
        api._closing = True
        try:
            if getattr(api, "_running_started_at", None) and getattr(api, "_running_inst_ref", None):
                elapsed = int(max(0, time.time() - api._running_started_at))
                api._running_inst_ref["playtime_sec"] = int(api._running_inst_ref.get("playtime_sec", 0)) + elapsed
                api._running_started_at = None
                api._save_config()
        except Exception:
            pass
        try:
            api._flush_meta_cache_now()
        except Exception:
            pass
        try:
            api._discord_rpc.close()
        except Exception:
            pass


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
