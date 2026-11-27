"""外部穿透客户端管理（playit / ngrok / frp / 自定义命令）。

只负责"启动用户指定的命令行、读取输出、解析公网地址、停止进程"，
不内置任何第三方二进制，也不联网下载。命令来自用户配置，启动前校验可执行文件。

安全：
- 不使用 ``shell=True``，参数经 ``shlex`` 拆分
- 配置/启动/停止端点仅限本机回环（见 web 路由）
- 进程随程序退出被终止（atexit + 显式 stop）
"""

from __future__ import annotations

import atexit
import contextlib
import re
import shlex
import shutil
import subprocess
import threading
import time

from .app_logger import safe_log_exception
from .constants import IS_WINDOWS

#: 支持的客户端类型
PROVIDERS = ("playit", "ngrok", "frp", "custom")
#: 从输出里识别公网地址的正则（按 provider）
ADDRESS_PATTERNS = {
    "playit": (
        r"(?P<host>[a-z0-9][a-z0-9-]*\.(?:playit\.gg|at\.playit\.gg))(?::(?P<port>\d{2,5}))?",
        r"address\s*[:=]\s*(?P<host>[a-z0-9][a-z0-9-]*\.(?:playit\.gg|at\.playit\.gg))(?::(?P<port>\d{2,5}))?",
    ),
    "ngrok": (
        r"tcp://(?P<host>[^\s:/]+):(?P<port>\d{2,5})",
        r"url=tcp://(?P<host>[^\s:/]+):(?P<port>\d{2,5})",
        r"https?://(?P<host>[a-z0-9.-]+\.ngrok(?:-free)?\.(?:app|io))",
    ),
    "frp": (r"(?P<host>\d{1,3}(?:\.\d{1,3}){3}):(?P<port>\d{2,5})",),
    "custom": (),
}
#: 每个 provider 的默认可执行文件名（用于提示）
DEFAULT_BINARIES = {
    "playit": "playit",
    "ngrok": "ngrok",
    "frp": "frpc",
    "custom": "",
}
#: 默认命令模板（``{port}`` 会被替换为当前服务器端口）
DEFAULT_COMMANDS = {
    "playit": "playit",
    "ngrok": "ngrok tcp {port}",
    "frp": "frpc -c frpc.toml",
    "custom": "",
}
_MAX_LINES = 300


class TunnelError(Exception):
    """穿透客户端启动/停止失败。"""


def _settings():
    from .app_context import AppContext

    return AppContext.instance().settings


def config() -> dict:
    """读取穿透配置（带默认值）。"""
    raw = _settings().get("tunnel_config")
    data = raw if isinstance(raw, dict) else {}
    return {
        "provider": str(data.get("provider") or "playit"),
        "command": str(data.get("command") or DEFAULT_COMMANDS.get(str(data.get("provider") or "playit"), "")),
        "address_regex": str(data.get("address_regex") or ""),
        "public_address": str(data.get("public_address") or ""),
        "enabled": bool(data.get("enabled", False)),
    }


def save_config(
    provider: str, command: str, address_regex: str = "", public_address: str = "", enabled: bool = False
) -> dict:
    """保存穿透配置。"""
    from .utils import save_settings

    if provider not in PROVIDERS:
        raise TunnelError(f"未知的穿透客户端: {provider}")
    command = (command or "").strip()
    if command and not _command_ok(command):
        raise TunnelError("命令格式不正确（检查引号是否配对）")
    settings = _settings()
    settings["tunnel_config"] = {
        "provider": provider,
        "command": command,
        "address_regex": (address_regex or "").strip()[:200],
        "public_address": (public_address or "").strip()[:200],
        "enabled": bool(enabled),
    }
    save_settings(settings)
    return config()


def _command_ok(command: str) -> bool:
    try:
        return bool(shlex.split(command, posix=not IS_WINDOWS))
    except ValueError:
        return False


def _unquote(token: str) -> str:
    """去掉 token 两端成对的引号（Windows 下 shlex(posix=False) 会保留引号）。"""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[1:-1]
    return token


def build_command(command: str, port: int) -> list[str]:
    """把命令模板拆成 argv，并替换 ``{port}`` 占位符。"""
    try:
        parts = shlex.split(command, posix=not IS_WINDOWS)
    except ValueError as exc:
        raise TunnelError(f"命令解析失败: {exc}") from exc
    if not parts:
        raise TunnelError("命令为空")
    expanded = [_unquote(part.replace("{port}", str(port))) for part in parts]
    executable = expanded[0]
    if not (shutil.which(executable) or _is_path_like(executable)):
        raise TunnelError(f"找不到可执行文件: {executable}（请填写完整路径或加入 PATH）")
    return expanded


def _is_path_like(value: str) -> bool:
    return ("/" in value) or ("\\" in value) or value.lower().endswith((".exe", ".bat", ".cmd", ".sh"))


def detect_address(lines: list[str], provider: str, extra_pattern: str = "") -> str:
    """从输出行里提取公网地址（``host`` 或 ``host:port``）。"""
    patterns: list[str] = list(ADDRESS_PATTERNS.get(provider, ()))
    if extra_pattern:
        patterns.insert(0, extra_pattern)
    for line in reversed(lines):
        for pattern in patterns:
            try:
                match = re.search(pattern, line, re.IGNORECASE)
            except re.error:
                continue
            if not match:
                continue
            groups = match.groupdict()
            host = groups.get("host") or ""
            port = groups.get("port") or ""
            if not host:
                continue
            return f"{host}:{port}" if port else host
    return ""


class TunnelManager:
    """单例式的穿透客户端进程管理器。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lines: list[str] = []
        self._provider = ""
        self._address = ""
        self._started_at = 0.0
        self._error = ""
        atexit.register(self.stop)

    # ---- 状态 ----
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self.running(),
                "provider": self._provider,
                "address": self._address,
                "started_at": self._started_at,
                "error": self._error,
                "lines": list(self._lines[-40:]),
            }

    # ---- 生命周期 ----
    def start(self, port: int, provider: str | None = None, command: str | None = None) -> dict:
        """启动穿透客户端。"""
        cfg = config()
        provider = provider or cfg["provider"]
        command = (command if command is not None else cfg["command"]).strip()
        if provider not in PROVIDERS:
            raise TunnelError(f"未知的穿透客户端: {provider}")
        if not command:
            raise TunnelError("请先填写穿透客户端命令")
        if self.running():
            raise TunnelError("穿透客户端已在运行，请先停止")
        argv = build_command(command, port)
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        except OSError as exc:
            raise TunnelError(f"启动失败: {exc}") from exc
        with self._lock:
            self._proc = proc
            self._lines = []
            self._provider = provider
            self._address = ""
            self._started_at = time.time()
            self._error = ""
        self._reader = threading.Thread(
            target=self._read_loop, args=(proc, provider, cfg["address_regex"]), daemon=True
        )
        self._reader.start()
        safe_log_exception("tunnel", f"穿透客户端已启动: {provider} pid={proc.pid}", "info")
        return self.status()

    def _read_loop(self, proc: subprocess.Popen, provider: str, extra_pattern: str) -> None:
        stream = proc.stdout
        if stream is None:
            return
        while True:
            chunk = stream.readline()
            if not chunk:
                break
            line = chunk.decode("utf-8", "ignore").rstrip("\r\n")
            if not line:
                continue
            with self._lock:
                self._lines.append(line)
                if len(self._lines) > _MAX_LINES:
                    self._lines = self._lines[-_MAX_LINES:]
                if not self._address:
                    found = detect_address(self._lines[-20:], provider, extra_pattern)
                    if found:
                        self._address = found
                        safe_log_exception("tunnel", f"识别到公网地址: {found}", "info")
        code = proc.poll()
        if code not in (0, None):
            with self._lock:
                if not self._error:
                    self._error = f"客户端已退出（code={code}）"

    def stop(self, timeout: float = 5.0) -> dict:
        """停止穿透客户端（先 terminate，超时后 kill）。"""
        proc = self._proc
        if proc is None:
            with self._lock:
                self._error = ""
            return self.status()
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=timeout)
            except OSError as exc:
                # 进程可能已自行退出（terminate 失败属正常情况），
                # 后续 status 查询会反映真实状态，无需中断停止流程。
                safe_log_exception("tunnel", f"停止穿透客户端时出错: {exc}", "debug")
        with self._lock:
            self._proc = None
            self._address = ""
            self._provider = ""
            self._started_at = 0.0
        safe_log_exception("tunnel", "穿透客户端已停止", "info")
        return self.status()


manager = TunnelManager()
