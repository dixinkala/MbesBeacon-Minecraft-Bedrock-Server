"""
应用上下文模块：AppContext，封装全局状态，便于测试和重构。
这是一个底层模块，不依赖任何业务模块，避免循环依赖。

使用方式：
    from .app_context import AppContext
    ctx = AppContext.instance()
    ctx.log("服务器启动")
    server_dir = ctx.server_dir
    is_running = ctx.is_server_running()
"""

import threading
import time
from typing import Any, Optional


class AppContext:
    """应用全局状态容器。封装所有模块级全局变量，便于测试和未来重构。

    使用单例模式，通过 AppContext.instance() 访问全局实例。
    所有全局状态都通过此容器管理，减少模块间的隐式耦合。

    线程安全：所有状态访问都通过锁保护，确保多线程环境下的一致性。

    示例：
        >>> ctx = AppContext.instance()
        >>> ctx.server_dir = "C:\\MinecraftServer"
        >>> ctx.log("服务器启动")
        >>> ctx.is_server_dir_set
        True
    """

    _instance: Optional["AppContext"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        """初始化应用上下文，所有状态初始化为默认值。"""
        # 运行时状态
        # 延迟导入避免循环依赖，直接从 state 模块获取 API_TOKEN
        try:
            from .state import API_TOKEN as _state_api_token

            self._api_token: str = _state_api_token
        except (ImportError, AttributeError):
            import secrets

            self._api_token: str = secrets.token_hex(16)
        self._app_start_time: float = time.time()
        self._mutex_handle: Any = None

        # 全局单例状态（在各模块中创建后设置）
        self._console: Any = None
        self._install_state: Any = None
        # 直接引用 state.settings，确保两者是同一个对象
        try:
            from .state import settings as _state_settings

            self._settings: dict[str, Any] = _state_settings
        except (ImportError, AttributeError):
            self._settings: dict[str, Any] = {
                "server_dir": "",
                "ignore_ssl": False,
                "installed_version": "",
                "server_dir_history": [],
            }
        self._server_proc: Any = None
        self._server_lock = threading.Lock()
        self._httpd: Any = None
        self._tray: Any = None

        # 配置
        self._port: int = 19100
        self._initialized: bool = False
        self._init_lock = threading.Lock()

    # ---------------- 单例管理 ----------------

    @classmethod
    def instance(cls) -> "AppContext":
        """获取全局 AppContext 单例。线程安全。

        Returns:
            AppContext: 全局单例实例
        """
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """重置单例（用于测试）。"""
        with cls._instance_lock:
            cls._instance = None

    # ---------------- 运行时状态 ----------------

    @property
    def api_token(self) -> str:
        """API 安全 token。"""
        return self._api_token

    @api_token.setter
    def api_token(self, value: str) -> None:
        self._api_token = value

    @property
    def has_api_token(self) -> bool:
        """是否已设置 API token。"""
        return bool(self._api_token)

    @property
    def app_start_time(self) -> float:
        """应用启动时间戳。"""
        return self._app_start_time

    @property
    def uptime_seconds(self) -> float:
        """应用运行时长（秒）。"""
        return time.time() - self._app_start_time

    @property
    def uptime_minutes(self) -> float:
        """应用运行时长（分钟）。"""
        return self.uptime_seconds / 60

    @property
    def uptime_hours(self) -> float:
        """应用运行时长（小时）。"""
        return self.uptime_seconds / 3600

    @property
    def uptime_str(self) -> str:
        """应用运行时长（人类可读格式）。"""
        seconds = int(self.uptime_seconds)
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}小时{minutes}分{secs}秒"
        elif minutes > 0:
            return f"{minutes}分{secs}秒"
        else:
            return f"{secs}秒"

    @property
    def mutex_handle(self) -> Any:
        """单实例互斥量句柄。"""
        return self._mutex_handle

    @mutex_handle.setter
    def mutex_handle(self, value: Any) -> None:
        self._mutex_handle = value

    # ---------------- 全局单例状态 ----------------

    class _NullConsole:
        """空控制台对象：当 console 未初始化时使用，避免 NoneType 错误。"""

        def append(self, *args, **kwargs):
            pass

        def clear(self, *args, **kwargs):
            pass

        def read_since(self, *args, **kwargs):
            return "", 0

        def wait_for_new(self, *args, **kwargs):
            return "", 0

        def set_log_file(self, *args, **kwargs):
            pass

        @property
        def lock(self):
            import threading

            return threading.Lock()

        def __len__(self):
            return 0

        def __iter__(self):
            return iter([])

    class _NullInstallState:
        """空安装状态对象：当 install_state 未初始化时使用，避免 NoneType 错误。"""

        def snapshot(self):
            return {
                "busy": False,
                "done": False,
                "error": "",
                "phase": "idle",
                "percent": 0,
                "text": "",
                "log": [],
            }

        def set(self, **kwargs):
            pass

        def log_line(self, *args, **kwargs):
            pass

        @property
        def busy(self):
            return False

        @property
        def done(self):
            return False

        @property
        def error(self):
            return ""

        @property
        def cancel(self):
            import threading

            return threading.Event()

    @property
    def console(self) -> Any:
        """控制台缓冲区实例。如果未初始化，返回空对象以避免 NoneType 错误。"""
        if self._console is None:
            return self._NullConsole()
        return self._console

    @console.setter
    def console(self, value: Any) -> None:
        self._console = value

    @property
    def has_console(self) -> bool:
        """是否已设置控制台缓冲区。"""
        return self._console is not None

    @property
    def install_state(self) -> Any:
        """安装状态实例。如果未初始化，返回空对象以避免 NoneType 错误。"""
        if self._install_state is None:
            return self._NullInstallState()
        return self._install_state

    @install_state.setter
    def install_state(self, value: Any) -> None:
        self._install_state = value

    @property
    def has_install_state(self) -> bool:
        """是否已设置安装状态。"""
        return self._install_state is not None

    @property
    def is_installing(self) -> bool:
        """是否正在安装。"""
        if self._install_state is None:
            return False
        return getattr(self._install_state, "busy", False)

    @property
    def settings(self) -> dict[str, Any]:
        """全局设置字典。"""
        return self._settings

    @settings.setter
    def settings(self, value: dict[str, Any]) -> None:
        self._settings = value

    def get_setting(self, key: str, default: Any = None) -> Any:
        """获取设置值。

        Args:
            key: 设置键名
            default: 默认值

        Returns:
            设置值，如果不存在则返回默认值
        """
        return self._settings.get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        """设置设置值。

        Args:
            key: 设置键名
            value: 设置值
        """
        self._settings[key] = value

    def update_settings(self, updates: dict[str, Any]) -> None:
        """批量更新设置。

        Args:
            updates: 更新的键值对字典
        """
        self._settings.update(updates)

    @property
    def server_proc(self) -> Any:
        """服务器进程单例。"""
        return self._server_proc

    @server_proc.setter
    def server_proc(self, value: Any) -> None:
        self._server_proc = value

    @property
    def has_server_proc(self) -> bool:
        """是否已设置服务器进程。"""
        return self._server_proc is not None

    @property
    def is_server_running(self) -> bool:
        """服务器是否正在运行。"""
        if self._server_proc is None:
            return False
        return getattr(self._server_proc, "running", False)

    @property
    def server_lock(self) -> threading.Lock:
        """服务器进程锁。"""
        return self._server_lock

    @property
    def httpd(self) -> Any:
        """HTTP 服务器实例。"""
        return self._httpd

    @httpd.setter
    def httpd(self, value: Any) -> None:
        self._httpd = value

    @property
    def has_httpd(self) -> bool:
        """是否已设置 HTTP 服务器。"""
        return self._httpd is not None

    @property
    def tray(self) -> Any:
        """系统托盘实例。"""
        return self._tray

    @tray.setter
    def tray(self, value: Any) -> None:
        self._tray = value

    @property
    def has_tray(self) -> bool:
        """是否已设置系统托盘。"""
        return self._tray is not None

    # ---------------- 配置 ----------------

    @property
    def port(self) -> int:
        """管理端口。"""
        return self._port

    @port.setter
    def port(self, value: int) -> None:
        self._port = value

    @property
    def server_dir(self) -> str:
        """当前服务器目录。"""
        return self._settings.get("server_dir", "")

    @server_dir.setter
    def server_dir(self, value: str) -> None:
        self._settings["server_dir"] = value

    @property
    def is_server_dir_set(self) -> bool:
        """是否已设置服务器目录。"""
        return bool(self._settings.get("server_dir", ""))

    @property
    def installed_version(self) -> str:
        """已安装的服务器版本。"""
        return self._settings.get("installed_version", "")

    @installed_version.setter
    def installed_version(self, value: str) -> None:
        self._settings["installed_version"] = value

    @property
    def has_installed_version(self) -> bool:
        """是否有已安装的版本。"""
        return bool(self._settings.get("installed_version", ""))

    @property
    def ignore_ssl(self) -> bool:
        """是否忽略 SSL 证书验证。"""
        return self._settings.get("ignore_ssl", False)

    @ignore_ssl.setter
    def ignore_ssl(self, value: bool) -> None:
        self._settings["ignore_ssl"] = value

    @property
    def server_dir_history(self) -> list[str]:
        """服务器目录历史记录。"""
        return self._settings.get("server_dir_history", [])

    def add_server_dir_history(self, dir_path: str, max_history: int = 20) -> None:
        """添加服务器目录到历史记录。

        Args:
            dir_path: 服务器目录路径
            max_history: 最大历史记录数
        """
        if not dir_path:
            return
        history = self._settings.get("server_dir_history", [])
        # 去重
        history = [h for h in history if h != dir_path]
        history.insert(0, dir_path)
        # 限制数量
        self._settings["server_dir_history"] = history[:max_history]

    # ---------------- 便捷方法 ----------------

    def log(self, text: str) -> None:
        """向控制台追加日志。

        Args:
            text: 日志文本
        """
        if self._console is not None:
            self._console.append(text)

    def log_info(self, text: str) -> None:
        """向控制台追加信息级别日志。

        Args:
            text: 日志文本
        """
        self.log(f"[信息] {text}")

    def log_warning(self, text: str) -> None:
        """向控制台追加警告级别日志。

        Args:
            text: 日志文本
        """
        self.log(f"[警告] {text}")

    def log_error(self, text: str) -> None:
        """向控制台追加错误级别日志。

        Args:
            text: 日志文本
        """
        self.log(f"[错误] {text}")

    def is_initialized(self) -> bool:
        """检查应用是否已初始化。"""
        return self._initialized

    def mark_initialized(self) -> None:
        """标记应用已初始化。"""
        with self._init_lock:
            self._initialized = True

    def to_dict(self) -> dict[str, Any]:
        """将状态转换为字典（用于健康检查和调试）。

        Returns:
            状态字典
        """
        return {
            "api_token_set": self.has_api_token,
            "uptime_seconds": round(self.uptime_seconds, 2),
            "uptime_str": self.uptime_str,
            "port": self._port,
            "server_dir": self.server_dir,
            "server_dir_set": self.is_server_dir_set,
            "installed_version": self.installed_version,
            "ignore_ssl": self.ignore_ssl,
            "console_set": self.has_console,
            "install_state_set": self.has_install_state,
            "is_installing": self.is_installing,
            "server_proc_set": self.has_server_proc,
            "is_server_running": self.is_server_running,
            "httpd_set": self.has_httpd,
            "tray_set": self.has_tray,
            "initialized": self._initialized,
            "server_dir_history_count": len(self.server_dir_history),
        }

    def __repr__(self) -> str:
        """返回 AppContext 的字符串表示。"""
        return (
            f"AppContext(port={self._port}, "
            f"server_dir='{self.server_dir}', "
            f"is_server_running={self.is_server_running}, "
            f"uptime={self.uptime_str})"
        )


__all__ = ["AppContext"]
