"""远程管理：密码认证、会话与局域网访问控制。

设计要点（默认安全）：

- **默认关闭**：只有用户在设置里启用并设置密码后，服务端才会监听 ``0.0.0.0``。
- **密码不明文保存**：PBKDF2-HMAC-SHA256（随机盐、20 万次迭代），只存哈希字符串。
- **会话只在内存**：程序重启即全部失效；12 小时滑动过期，最多 20 个并发会话。
- **登录限速**：同一 IP 5 分钟内失败 5 次后锁定 10 分钟，避免在线爆破。
- **Host 白名单**：启用远程后仍只接受"本机网卡 IP / 本机主机名 / 用户显式配置的主机名"，
  继续防御 DNS rebinding。
- **明文 HTTP**：仅建议在可信局域网使用；公网访问请配合内网穿透或反向代理加 TLS。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import socket
import threading
import time
from collections import defaultdict, deque

from .app_logger import safe_log_exception

#: 会话 Cookie 名
SESSION_COOKIE = "mbes_session"
#: 会话有效期（秒），每次通过校验后顺延
SESSION_TTL = 12 * 3600
#: 同时存在的会话上限
MAX_SESSIONS = 20
#: 密码哈希参数
PBKDF2_ITERATIONS = 200_000
PBKDF2_ALGO = "pbkdf2_sha256"
#: 密码最短长度
MIN_PASSWORD_LENGTH = 8
#: 登录失败窗口与锁定
LOGIN_WINDOW = 300.0
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 600.0

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"})


# --------------------------------------------------------------------------- 密码


def hash_password(password: str) -> str:
    """把明文密码转换成可存储的 PBKDF2 字符串。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "$".join(
        [
            PBKDF2_ALGO,
            str(PBKDF2_ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """校验明文密码是否匹配存储的哈希；任何格式异常都返回 False。"""
    if not password or not stored:
        return False
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if algo != PBKDF2_ALGO:
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_ok(password: str) -> bool:
    """密码强度：至少 8 位，且不是纯重复/纯数字这类明显弱口令。"""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        return False
    return len(set(password)) != 1


# --------------------------------------------------------------------------- 配置


def _settings():
    from .app_context import AppContext

    return AppContext.instance().settings


def enabled() -> bool:
    """远程管理是否真正启用（开关打开 **且** 已设置密码）。"""
    try:
        settings = _settings()
        return bool(settings.get("remote_enabled")) and bool(settings.get("remote_password_hash"))
    except Exception as exc:
        safe_log_exception("remote", f"读取远程配置失败: {exc}", "warning")
        return False


def configured() -> bool:
    """是否已配置密码（用于界面提示，不代表已启用）。"""
    try:
        return bool(_settings().get("remote_password_hash"))
    except Exception:
        return False


def extra_hosts() -> set[str]:
    """用户额外放行的主机名（如 DDNS 域名）。"""
    try:
        values = _settings().get("remote_hosts") or []
    except Exception:
        return set()
    if not isinstance(values, (list, tuple)):
        return set()
    return {str(v).strip().lower() for v in values if str(v).strip()}


def set_config(enabled_flag: bool, password: str | None = None, hosts: list[str] | None = None) -> dict:
    """更新远程管理配置并保存。

    Args:
        enabled_flag: 是否启用
        password: 新密码；None 表示保持不变
        hosts: 额外允许的主机名列表；None 表示保持不变

    Returns:
        dict: ``{"enabled": bool, "restart_required": bool}``

    Raises:
        ValueError: 密码不合法 / 未设置密码却试图启用
    """
    from .utils import save_settings

    settings = _settings()
    before_enabled = bool(settings.get("remote_enabled"))

    if password is not None:
        if not password_ok(password):
            raise ValueError(f"密码至少 {MIN_PASSWORD_LENGTH} 位，且不能是单一重复字符")
        settings["remote_password_hash"] = hash_password(password)
    if hosts is not None:
        settings["remote_hosts"] = [str(h).strip() for h in hosts if str(h).strip()][:20]

    if enabled_flag and not settings.get("remote_password_hash"):
        raise ValueError("启用远程管理前请先设置访问密码")

    settings["remote_enabled"] = bool(enabled_flag)
    save_settings(settings)
    if enabled_flag and before_enabled:
        sessions.clear()  # 重新启用时让旧会话失效
    return {"enabled": bool(enabled_flag), "restart_required": bool(enabled_flag) != before_enabled}


# --------------------------------------------------------------------------- 网络


_ip_cache: dict[str, object] = {"time": 0.0, "values": set()}


def local_ipv4_addresses(ttl: float = 60.0) -> set[str]:
    """本机所有 IPv4 地址（含回环），用于 Host 白名单与界面展示。"""
    now = time.time()
    if _ip_cache["values"] and now - float(_ip_cache["time"]) < ttl:  # type: ignore[arg-type]
        return set(_ip_cache["values"])  # type: ignore[arg-type]
    values: set[str] = {"127.0.0.1"}
    try:
        import psutil

        for name, addrs in psutil.net_if_addrs().items():
            if name.lower().startswith(("lo", "loopback")):
                continue
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address:
                    values.add(addr.address)
    except Exception as exc:
        safe_log_exception("remote", f"枚举网卡地址失败: {exc}", "debug")
    if len(values) == 1:
        try:
            hostname_ip = socket.gethostbyname(socket.gethostname())
            values.add(hostname_ip)
        except OSError as exc:
            # 主机名无法解析（离线/无 hosts 记录）：保留回环地址即可，
            # 远程访问仍可通过网卡 IP 进入，不必中断启动流程。
            safe_log_exception("remote", f"解析主机名 IP 失败: {exc}", "debug")
    _ip_cache["values"] = set(values)
    _ip_cache["time"] = now
    return values


def primary_lan_ip() -> str:
    """优先返回一个可给局域网使用的 IPv4 地址（无则回环）。

    排序：私有网段（192.168/10.x/172.16-31）> 其它非回环地址 > DNS 解析出的地址。
    ``169.254.x.x``（APIPA）和 ``0.0.0.0`` 不能用于端口映射，排在最后。
    """
    candidates = local_ipv4_addresses()

    def rank(addr: str) -> tuple:
        if addr.startswith("127.") or addr.startswith("0."):
            return (3, addr)
        if addr.startswith("169.254."):
            return (2, addr)
        private = addr.startswith("192.168.") or addr.startswith("10.") or _is_private_172(addr)
        return (0 if private else 1, addr)

    usable = [addr for addr in candidates if not addr.startswith(("127.", "0."))]
    if not usable:
        return "127.0.0.1"
    usable.sort(key=rank)
    return usable[0]


def _is_private_172(addr: str) -> bool:
    parts = addr.split(".")
    if len(parts) < 2 or parts[0] != "172":
        return False
    try:
        second = int(parts[1])
    except ValueError:
        return False
    return 16 <= second <= 31


def allowed_hostnames() -> set[str]:
    """允许出现在 Host 头里的主机名。"""
    names = set(_LOOPBACK)
    if enabled():
        names |= local_ipv4_addresses()
        hostname = socket.gethostname().strip().lower()
        if hostname:
            names.add(hostname)
            names.add(f"{hostname}.local")
        names |= extra_hosts()
    return names


def is_loopback_value(value: str) -> bool:
    """判断 IP/主机名是否属于回环。"""
    text = (value or "").strip().lower()
    if text in _LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


# --------------------------------------------------------------------------- 会话


class SessionStore:
    """内存会话表（线程安全）。"""

    def __init__(self, ttl: float = SESSION_TTL, max_sessions: int = MAX_SESSIONS):
        self._ttl = ttl
        self._max = max_sessions
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}

    def _purge_locked(self, now: float) -> None:
        expired = [token for token, info in self._sessions.items() if info["expires"] <= now]
        for token in expired:
            self._sessions.pop(token, None)

    def create(self, client_ip: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            while len(self._sessions) >= self._max:
                oldest = min(self._sessions, key=lambda k: self._sessions[k]["created"])
                self._sessions.pop(oldest, None)
            self._sessions[token] = {"created": now, "expires": now + self._ttl, "ip": client_ip}
        return token

    def validate(self, token: str) -> bool:
        """校验并顺延会话；过期/不存在返回 False。"""
        if not token:
            return False
        now = time.time()
        with self._lock:
            info = self._sessions.get(token)
            if not info:
                return False
            if info["expires"] <= now:
                self._sessions.pop(token, None)
                return False
            info["expires"] = now + self._ttl
            return True

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


sessions = SessionStore()


# --------------------------------------------------------------------------- 登录限速


class LoginLimiter:
    """按 IP 的登录失败限速（滑动窗口 + 锁定）。"""

    def __init__(
        self, window: float = LOGIN_WINDOW, max_failures: int = LOGIN_MAX_FAILURES, lock: float = LOGIN_LOCK_SECONDS
    ):
        self._window = window
        self._max = max_failures
        self._lock_seconds = lock
        self._lock = threading.Lock()
        self._failures: dict[str, deque] = defaultdict(deque)
        self._locked: dict[str, float] = {}

    def retry_after(self, client_ip: str) -> float:
        """返回剩余锁定秒数；0 表示可用。"""
        now = time.time()
        with self._lock:
            until = self._locked.get(client_ip)
            if not until:
                return 0.0
            if until <= now:
                self._locked.pop(client_ip, None)
                return 0.0
            return until - now

    def record_failure(self, client_ip: str) -> float:
        """记录一次失败，返回若被锁定时的剩余秒数。"""
        now = time.time()
        with self._lock:
            bucket = self._failures[client_ip]
            while bucket and now - bucket[0] > self._window:
                bucket.popleft()
            bucket.append(now)
            if len(bucket) >= self._max:
                self._locked[client_ip] = now + self._lock_seconds
                bucket.clear()
                return self._lock_seconds
            return 0.0

    def record_success(self, client_ip: str) -> None:
        with self._lock:
            self._failures.pop(client_ip, None)
            self._locked.pop(client_ip, None)


login_limiter = LoginLimiter()


# --------------------------------------------------------------------------- Cookie


def parse_cookie(header: str, name: str = SESSION_COOKIE) -> str:
    """从 Cookie 头里取出指定 cookie 的值。"""
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.strip()
    return ""


def session_cookie(token: str, max_age: int = SESSION_TTL) -> str:
    """生成 Set-Cookie 头（HttpOnly + SameSite=Strict）。

    未启用 HTTPS，因此不加 Secure；反向代理加 TLS 时由代理负责。
    """
    return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"


def clear_cookie() -> str:
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"


def audit(action: str, detail: str) -> None:
    """远程管理相关审计（失败不影响主流程）。"""
    try:
        from .security import audit_log

        audit_log(action, detail)
    except Exception as exc:
        safe_log_exception("remote", f"审计写入失败: {exc}", "debug")
