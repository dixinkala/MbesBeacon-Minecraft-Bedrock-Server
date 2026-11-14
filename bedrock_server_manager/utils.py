"""
通用工具模块：设置管理、服务器检测、目录历史、浏览器打开等。
"""

import contextlib
import json
import os
import tempfile
import time
import webbrowser

from .app_context import AppContext
from .app_logger import safe_log_exception
from .constants import APP_DATA_DIR, IS_WINDOWS, SERVER_EXE
from .state import DEFAULT_THEME, THEME_FILE


# 通过 AppContext 访问全局状态（消除延迟导入）
def _get_settings():
    return AppContext.instance().settings


def _get_console():
    return AppContext.instance().console


def _get_server_proc():
    ctx = AppContext.instance()
    return ctx.server_proc, ctx.server_lock


# 设置文件路径（统一到 APP_DATA_DIR，与 state.migrate_legacy_data 迁移目标一致）
SETTINGS_FILE = os.path.join(APP_DATA_DIR, "settings.json")


def atomic_write_text(path: str, text: str, encoding: str = "utf-8", newline: str = "") -> None:
    """原子写入文本文件：先写同目录临时文件，fsync 后 os.replace 覆盖目标。

    直接用 ``open(path, "w")`` 时，进程崩溃/断电会在磁盘上留下截断到一半的文件；
    对 settings.json、server.properties、权限/白名单这类"唯一副本"是致命的数据丢失。

    并发安全：
    - 临时文件名必须**每次唯一**。早期用 ``f"{path}.tmp{os.getpid()}"``，
      同进程多线程写同一文件时共用同一个 tmp，一个线程 replace 走它、
      另一个线程的 finally 再把它删掉，结果目标文件直接消失。
    - ``finally`` 里只在**本线程仍持有**该临时文件时才删除（replace 成功后
      那个路径已不存在，不会误删别人的）。

    Args:
        path: 目标文件路径
        text: 待写入的文本内容
        encoding: 文件编码
        newline: 换行处理，默认 ""（不做转换，保持内容原样）

    Raises:
        OSError: 目录不可写、磁盘满等
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    # 同目录 + 唯一后缀：replace 才是同卷原子操作
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory or ".")
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # Windows 上目标被其它进程/杀软短暂占用时 os.replace 会抛 PermissionError，
        # 重试几次；仍失败则向上抛出不吞掉，避免"看起来保存成功其实没写"。
        last_err = None
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                os.replace(tmp, path)
                replaced = True
                break
            except PermissionError as e:
                last_err = e
        if not replaced:
            raise last_err if last_err else OSError("原子替换失败")
    finally:
        # 只有还没成功 replace 时才清理自己的临时文件
        if not replaced:
            with contextlib.suppress(OSError):
                os.remove(tmp)


def atomic_write_json(path: str, obj) -> None:
    """原子写入 JSON 文件（UTF-8、2 空格缩进、保留中文）。"""
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def is_path_within(path: str, base: str) -> bool:
    """判断 path 是否位于 base 目录内（含 base 本身）。

    所有删除/移动/恢复类操作都应先通过此函数做边界检查，
    避免 ``..``、分隔符拼接或绝对路径导致越界操作。

    Args:
        path: 待校验的路径
        base: 允许的根目录

    Returns:
        bool: True 表示 path 在 base 内（或与 base 相同）
    """
    if not path or not base:
        return False
    try:
        p = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        b = os.path.normcase(os.path.realpath(os.path.abspath(base)))
    except (OSError, ValueError):
        return False
    if p == b:
        return True
    # base 本身是根目录时（C:\ 或 /）它已带分隔符，再加 os.sep 会变成 "C:\\"、
    # "//"，导致前缀比较永远失败（把根目录内的合法路径判成越界）。
    if b.endswith(os.sep):
        return p.startswith(b)
    return p.startswith(b + os.sep)


def is_dangerous_target(path: str) -> bool:
    """判断路径是否为不应被整体删除的危险目标（盘符根目录、用户主目录、系统目录）。"""
    try:
        p = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    except (OSError, ValueError):
        return True
    if not p:
        return True
    # 盘符根目录（C:\）或文件系统根（/）
    if os.path.dirname(p) == p:
        return True
    if len(p) <= 3 and p[1:3] == ":\\":
        return True
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    if p == home:
        return True
    for sys_dir in (
        "c:\\windows",
        "c:\\program files",
        "c:\\program files (x86)",
        "c:\\programdata",
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/boot",
        "/var",
        "/dev",
        "/proc",
        "/sys",
        "/root",
    ):
        try:
            s = os.path.normcase(os.path.realpath(os.path.abspath(sys_dir)))
        except (OSError, ValueError):
            continue
        # 系统目录**及其子目录**都危险（C:\Windows\System32 之类）。
        # 必须按分隔符边界比较，避免 "C:\WindowsFoo" 被误判成 "C:\Windows" 的子路径。
        if p == s or p.startswith(s + os.sep):
            return True
    return False


# detect_servers 缓存（避免高频状态轮询时反复扫描磁盘）
_detect_cache = {"result": None, "time": 0}
DETECT_CACHE_TTL = 30  # 缓存有效期 30 秒


def load_settings(settings=None) -> dict:
    """从磁盘加载设置，合并到全局 settings 字典。

    ``settings`` 参数保留用于显式传入（测试/脚本），默认取全局 AppContext。
    """
    if settings is None:
        settings = _get_settings()
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                for k, v in d.items():
                    settings[k] = v
    except Exception as e:
        safe_log_exception("utils", f"操作失败: {e}", "warning")


def save_settings(settings=None) -> bool:
    """将全局 settings 字典保存到磁盘（原子写）。

    ``settings`` 参数保留用于显式传入（测试/脚本），默认取全局 AppContext。
    """
    if settings is None:
        settings = _get_settings()
    try:
        atomic_write_json(SETTINGS_FILE, settings)
    except Exception as e:
        safe_log_exception("utils", f"操作失败: {e}", "warning")


def resolve_server_dir(settings=None):
    """获取当前管理的服务器目录。

    ``settings`` 参数保留用于显式传入（测试/脚本），默认取全局 AppContext。
    """
    if settings is None:
        settings = _get_settings()
    d = settings.get("server_dir", "").strip()
    return d


def sync_log_file(console=None):
    """根据当前服务器目录同步控制台日志文件路径。

    ``console`` 参数保留用于显式传入（测试/脚本），默认取全局 AppContext。
    """
    if console is None:
        console = _get_console()
    d = resolve_server_dir()
    if d and os.path.isdir(d):
        console.set_log_file(os.path.join(d, "server_console.log"))
    else:
        console.set_log_file(None)


def props_path():
    """获取 server.properties 文件路径。"""
    d = resolve_server_dir()
    return os.path.join(d, "server.properties") if d else ""


def installed() -> bool:
    """检查当前目录是否已安装服务器（包含 bedrock_server.exe）。"""
    d = resolve_server_dir()
    return bool(d) and os.path.isfile(os.path.join(d, SERVER_EXE))


def detect_servers(force_refresh: bool = False) -> list[dict]:
    """自动扫描常见目录，查找所有已安装的 Minecraft 基岩版服务器（包含 bedrock_server.exe 的目录）。
    返回服务器目录列表（绝对路径，去重）。
    force_refresh=True 时绕过缓存重新扫描。"""

    global _detect_cache
    now = time.time()
    if not force_refresh and _detect_cache["result"] is not None and (now - _detect_cache["time"]) < DETECT_CACHE_TTL:
        return _detect_cache["result"]
    found = []
    seen = set()

    def _check_dir(d):
        """检查目录是否包含 bedrock_server.exe，是则加入列表。"""
        try:
            if not d or not os.path.isdir(d):
                return
            exe = os.path.join(d, SERVER_EXE)
            if os.path.isfile(exe):
                abs_d = os.path.normcase(os.path.abspath(d))
                if abs_d not in seen:
                    seen.add(abs_d)
                    found.append(os.path.abspath(d))
        except Exception as e:
            safe_log_exception("utils", f"操作失败: {e}", "warning")

    def _scan_subdirs(parent, max_depth=2):
        """扫描父目录下的子目录（最多 max_depth 层），查找服务器。"""
        try:
            if not parent or not os.path.isdir(parent):
                return
            _check_dir(parent)
            if max_depth <= 0:
                return
            for name in os.listdir(parent):
                full = os.path.join(parent, name)
                if os.path.isdir(full) and not name.startswith("."):
                    _check_dir(full)
                    if max_depth > 1:
                        _scan_subdirs(full, max_depth - 1)
        except Exception as e:
            safe_log_exception("utils", f"操作失败: {e}", "warning")

    # 1. 当前管理的服务器目录
    cur = resolve_server_dir()
    if cur:
        _check_dir(cur)

    # 2. settings 中保存的历史目录
    settings = _get_settings()
    for d in settings.get("server_dir_history", []):
        _check_dir(d)

    # 3. 常见扫描位置（平台相关：Windows 扫描盘符目录，Linux 扫描常见服务器目录）
    home = os.path.expanduser("~")
    scan_roots = [
        os.getcwd(),
        home,
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Downloads"),
    ]
    if IS_WINDOWS:
        scan_roots += [
            "C:\\MinecraftServer",
            "D:\\MinecraftServer",
            "E:\\MinecraftServer",
        ]
    else:
        scan_roots += [
            os.path.join(home, "MinecraftServer"),
            os.path.join(home, "minecraft-server"),
            os.path.join(home, "mcserver"),
            "/srv/minecraft",
            "/opt/minecraft",
        ]
    for root in scan_roots:
        _scan_subdirs(root, max_depth=2)

    _detect_cache["result"] = found
    _detect_cache["time"] = time.time()
    return found


def add_server_dir_history(d):
    """将服务器目录加入历史记录（用于下次启动时快速检测）。"""
    if not d:
        return
    settings = _get_settings()
    history = settings.get("server_dir_history", [])
    abs_d = os.path.normcase(os.path.abspath(d))
    history = [h for h in history if os.path.normcase(os.path.abspath(h)) != abs_d]
    history.insert(0, d)
    settings["server_dir_history"] = history[:20]  # 最多保留20条
    save_settings()


def open_browser_with_retry(url, max_attempts=3):
    """尝试打开浏览器，失败后重试，最终失败时弹出 Windows 通知告知用户手动访问。"""
    for attempt in range(max_attempts):
        try:
            if webbrowser.open(url):
                return True
        except Exception as e:
            safe_log_exception("utils", f"操作失败: {e}", "warning")
        if attempt < max_attempts - 1:
            time.sleep(1.2)
    # 所有尝试失败，弹出通知
    try:
        import base64
        import subprocess

        if get_ui_lang() == "en":
            toast_title = "MbesBeacon — Minecraft Bedrock Server Manager"
            toast_body = f"Manager is running. Open: {url}"
        else:
            toast_title = "Minecraft 基岩版服务器管理器"
            toast_body = f"管理界面已启动，请访问: {url}"
        ps_script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            '$x=$t.GetElementsByTagName("text");'
            f'$x.Item(0).AppendChild($t.CreateTextNode("{toast_title}"))|Out-Null;'
            f'$x.Item(1).AppendChild($t.CreateTextNode("{toast_body}"))|Out-Null;'
            "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
            '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("MCServerManager").Show($n);'
        )
        encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
        subprocess.run(["powershell", "-EncodedCommand", encoded], capture_output=True, timeout=5)
    except Exception as e:
        safe_log_exception("utils", f"操作失败: {e}", "warning")
    return False


def browse_directory(initial_dir="", title=None):
    """弹出系统原生文件夹选择对话框，返回选择的目录路径。

    返回值语义（供路由层区分三种情况）：
    - 成功选择：返回目录路径字符串
    - 用户取消：返回 None
    - 调用失败（无图形环境 / 对话框不可用）：抛出异常，由调用方返回失败，
      前端据此回退到内置目录弹窗，避免“点了没反应”。

    Windows 使用 Win32 SHBrowseForFolder API（ctypes 实现，无第三方依赖，
    打包时不依赖 tkinter/tcl 数据）；Linux 回退到 tkinter，无桌面环境时抛异常。
    """
    if title is None:
        title = "选择文件夹"
    if IS_WINDOWS:
        return _browse_directory_windows(initial_dir, title)
    return _browse_directory_tk(initial_dir, title)


def _browse_directory_windows(initial_dir="", title="选择文件夹"):
    """Windows：通过 SHBrowseForFolder 弹出系统文件夹选择对话框。"""
    from ctypes import (
        POINTER,
        Structure,
        byref,
        c_int,
        c_void_p,
        create_unicode_buffer,
        windll,
        wintypes,
    )

    BIF_RETURNONLYFSDIRS = 0x0001
    BIF_NEWDIALOGSTYLE = 0x0040

    class BROWSEINFO(Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", c_int),
        ]

    shell32 = windll.shell32
    SHBrowseForFolderW = shell32.SHBrowseForFolderW
    SHBrowseForFolderW.argtypes = [POINTER(BROWSEINFO)]
    SHBrowseForFolderW.restype = c_void_p
    SHGetPathFromIDListW = shell32.SHGetPathFromIDListW
    SHGetPathFromIDListW.argtypes = [c_void_p, wintypes.LPWSTR]
    SHGetPathFromIDListW.restype = wintypes.BOOL

    buf = create_unicode_buffer(260)
    bi = BROWSEINFO()
    bi.pszDisplayName = buf
    bi.lpszTitle = title
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
    pidl = SHBrowseForFolderW(byref(bi))
    if not pidl:
        return None  # 用户取消
    try:
        path = create_unicode_buffer(260)
        if SHGetPathFromIDListW(pidl, path):
            return path.value
        return None
    finally:
        windll.ole32.CoTaskMemFree(pidl)


def _browse_directory_tk(initial_dir="", title="选择文件夹"):
    """Linux / 其他平台：通过 tkinter 弹出文件夹选择对话框（无桌面环境时抛异常）。"""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    selected = filedialog.askdirectory(title=title, initialdir=initial_dir or os.path.expanduser("~"), parent=root)
    root.destroy()
    return selected or None


def get_lan_ip() -> str:
    """获取本机局域网IP。"""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        safe_log_exception("utils", f"get_lan_ip 忽略异常: {e}", "warning")
        try:
            import socket as _s

            return _s.gethostbyname(_s.gethostname())
        except Exception as e:
            safe_log_exception("utils", f"get_lan_ip 忽略异常: {e}", "warning")
            return "127.0.0.1"


def check_disk_space(target_dir: str, required_mb: int = 300) -> tuple[bool, int]:
    """检查目标目录所在磁盘剩余空间。"""
    import shutil

    try:
        usage = shutil.disk_usage(target_dir)
        free_mb = usage.free / (1024 * 1024)
        return free_mb >= required_mb, free_mb, required_mb
    except Exception as e:
        safe_log_exception("utils", f"check_disk_space 忽略异常: {e}", "warning")
        return True, -1, required_mb


def load_theme() -> str:
    """加载主题设置。"""
    theme = dict(DEFAULT_THEME)
    try:
        with open(THEME_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            for k in ("preset", "accent", "mode"):
                if k in d:
                    theme[k] = d[k]
    except Exception as e:
        safe_log_exception("utils", f"操作失败: {e}", "warning")
    return theme


def save_theme(theme: str) -> bool:
    """保存主题设置。"""
    try:
        atomic_write_json(THEME_FILE, theme)
        return True
    except Exception as e:
        safe_log_exception("utils", f"save_theme 忽略异常: {e}", "warning")
        return False


def get_system_lang() -> str:
    """检测系统 UI 语言（平台相关）。

    中文系统返回 'zh'，其他（含英文、日文等）返回 'en'。
    Windows: 优先读取 Windows 用户界面语言（GetUserDefaultUILanguage）。
    Linux: 读取 locale 环境变量（LC_ALL / LC_MESSAGES / LANG）。
    """
    if IS_WINDOWS:
        try:
            from ctypes import windll

            lang_id = windll.kernel32.GetUserDefaultUILanguage()
            # PRIMARYLANGID(lang_id) = lang_id & 0x3FF，0x04 为 LANG_CHINESE
            if (lang_id & 0x3FF) == 0x04:
                return "zh"
            return "en"
        except Exception as e:
            safe_log_exception("utils", f"系统语言检测失败(API): {e}")
    try:
        import locale

        lang_code = ""
        for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
            val = os.environ.get(var)
            if val:
                lang_code = val
                break
        if not lang_code:
            lang_code, _ = locale.getdefaultlocale()
        if lang_code and lang_code.lower().startswith("zh"):
            return "zh"
    except Exception as e:
        safe_log_exception("utils", f"系统语言检测失败(locale): {e}")
    return "en"


def get_ui_lang() -> str:
    """当前 UI 语言：用户手动选择（settings.lang）优先，否则跟随 Windows 系统语言。"""
    s = _get_settings()
    if s and s.get("lang") in ("zh", "en"):
        return s["lang"]
    return get_system_lang()


def set_ui_lang(lang: str) -> bool:
    """持久化用户手动选择的 UI 语言到 settings（供托盘/通知等后端读取）。

    仅接受 'zh' / 'en'，非法值返回 False 不写入。
    """
    if lang not in ("zh", "en"):
        return False
    s = _get_settings()
    s["lang"] = lang
    save_settings()
    return True


def tr_msg(zh: str, en: str, *args) -> str:
    """按当前 UI 语言返回后端消息文案（安装日志 / 备份输出等）。

    zh 与 en 为对应语言的模板，占位符需一致（%s / %d / %.Nf 等）；
    args 以 % 格式化填充模板；无参数时直接返回模板。
    语言取 get_ui_lang()（用户手动选择优先，否则跟随系统语言）。
    """
    tpl = en if get_ui_lang() == "en" else zh
    if not args:
        return tpl
    try:
        return tpl % args
    except Exception as e:
        safe_log_exception("utils", f"tr_msg 忽略异常: {e}", "warning")
        return tpl
