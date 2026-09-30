"""
通用工具模块：设置管理、服务器检测、目录历史、浏览器打开等。
"""

import json
import os
import time
import webbrowser

from .app_context import AppContext
from .app_logger import safe_log_exception
from .constants import APP_DATA_DIR, SERVER_EXE
from .di import initialize_container, inject
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

# detect_servers 缓存（避免高频状态轮询时反复扫描磁盘）
_detect_cache = {"result": None, "time": 0}
DETECT_CACHE_TTL = 30  # 缓存有效期 30 秒


@inject("settings")
def load_settings(settings=None) -> dict:
    """从磁盘加载设置，合并到全局 settings 字典。

    使用依赖注入获取 settings，保持向后兼容。
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


@inject("settings")
def save_settings(settings=None) -> bool:
    """将全局 settings 字典保存到磁盘。

    使用依赖注入获取 settings，保持向后兼容。
    """
    if settings is None:
        settings = _get_settings()
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        safe_log_exception("utils", f"操作失败: {e}", "warning")


@inject("settings")
def resolve_server_dir(settings=None):
    """获取当前管理的服务器目录。

    使用依赖注入获取 settings，保持向后兼容。
    """
    if settings is None:
        settings = _get_settings()
    d = settings.get("server_dir", "").strip()
    return d


@inject("console")
def sync_log_file(console=None):
    """根据当前服务器目录同步控制台日志文件路径。

    使用依赖注入获取 console，保持向后兼容。
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

    # 3. 常见扫描位置
    home = os.path.expanduser("~")
    scan_roots = [
        os.getcwd(),
        home,
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Downloads"),
        "C:\\MinecraftServer",
        "D:\\MinecraftServer",
        "E:\\MinecraftServer",
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

        ps_script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            '$x=$t.GetElementsByTagName("text");'
            '$x.Item(0).AppendChild($t.CreateTextNode("Minecraft 基岩版服务器管理器"))|Out-Null;'
            '$x.Item(1).AppendChild($t.CreateTextNode("管理界面已启动，请访问: ' + url + '"))|Out-Null;'
            "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
            '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("MCServerManager").Show($n);'
        )
        encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
        subprocess.run(["powershell", "-EncodedCommand", encoded], capture_output=True, timeout=5)
    except Exception as e:
        safe_log_exception("utils", f"操作失败: {e}", "warning")
    return False


def browse_directory(initial_dir=""):
    """弹出系统原生文件夹选择对话框（使用 tkinter），返回选择的目录路径。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        selected = filedialog.askdirectory(
            title="选择文件夹", initialdir=initial_dir or os.path.expanduser("~"), parent=root
        )
        root.destroy()
        return selected or ""
    except Exception as e:
        print("browse_directory failed:", e)
        return ""


def get_lan_ip() -> str:
    """获取本机局域网IP。"""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            import socket as _s

            return _s.gethostbyname(_s.gethostname())
        except Exception:
            return "127.0.0.1"


def check_disk_space(target_dir: str, required_mb: int = 300) -> tuple[bool, int]:
    """检查目标目录所在磁盘剩余空间。"""
    import shutil

    try:
        usage = shutil.disk_usage(target_dir)
        free_mb = usage.free / (1024 * 1024)
        return free_mb >= required_mb, free_mb, required_mb
    except Exception:
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
        with open(THEME_FILE, "w", encoding="utf-8") as f:
            json.dump(theme, f, ensure_ascii=False)
        return True
    except Exception:
        return False


# 初始化依赖注入容器（确保在模块加载时完成）
initialize_container()
