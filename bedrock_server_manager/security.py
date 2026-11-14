"""
安全模块：指令校验、审计日志、自定义URL校验、API token。
"""

import contextlib
import ipaddress
import json
import os
import socket
import threading
import time
from urllib.parse import urlparse

from .app_context import AppContext
from .app_logger import safe_log_exception
from .config import AUDIT_LOG_NAME
from .constants import (
    DANGEROUS_MULTI_COMMANDS as _DANGEROUS_MULTI,
    DANGEROUS_SINGLE_COMMANDS as _DANGEROUS_SINGLE,
)
from .utils import resolve_server_dir

# 允许的官方下载域名（主机名必须**等于**其中之一或是其子域，
# 不能用 endswith 直接比字符串，否则 fakeminecraft.net 会被判为官方）
OFFICIAL_DOWNLOAD_DOMAINS = (
    "minecraft.net",
    "bedrock.dev",
    "minecraftservices.net",
    "xboxlive.com",
    "microsoft.com",
)

# 云元数据/链路本地等绝对禁止访问的地址段（SSRF 常见目标）
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # 链路本地，含 169.254.169.254 元数据
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),  # 组播
    ipaddress.ip_network("240.0.0.0/4"),  # 保留
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),  # 唯一本地地址
    ipaddress.ip_network("fe80::/10"),  # 链路本地
    ipaddress.ip_network("ff00::/8"),  # 组播
)

# 特殊主机名（不解析直接拒绝）
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)

# 自定义下载地址的最大长度（防止超长 URL 拖慢日志/解析）
MAX_CUSTOM_URL_LEN = 2048

# 安全指令白名单
SAFE_COMMANDS = {
    "list",
    "say",
    "whitelist",
    "op",
    "deop",
    "kick",
    "ban",
    "pardon",
    "permission",
    "allowlist",
    "gamemode",
    "give",
    "tp",
    "kill",
    "time",
    "weather",
    "difficulty",
    "save",
    "stop",
    "status",
    "getgameproperty",
    "setgameproperty",
    "changesetting",
}
# 危险指令（需要二次确认）：定义见 constants.DANGEROUS_*（单一事实来源）


def _normalize_command(cmd: str) -> str:
    """归一化指令文本：去首尾空白与可选的命令前缀斜杠。

    基岩版控制台同时接受 ``stop`` 与 ``/stop`` 两种写法，
    若只校验 ``stop`` 而把 ``/stop`` 原样放行，危险指令判定即可被绕过，
    故统一剥离前导 ``/`` 后再做白名单与危险判定。

    Args:
        cmd: 原始指令文本

    Returns:
        str: 归一化后的指令文本（保留大小写与参数）
    """
    text = cmd.strip()
    while text.startswith("/"):
        text = text[1:].lstrip()
    return text


def _is_dangerous_command(cmd_lower: str) -> bool:
    """精确判断指令是否危险。"""
    if not cmd_lower:
        return False
    parts = cmd_lower.split()
    if not parts:
        return False
    if parts[0] in _DANGEROUS_SINGLE:
        return True
    return any(cmd_lower == prefix or cmd_lower.startswith(prefix + " ") for prefix in _DANGEROUS_MULTI)


def validate_command(cmd: str) -> tuple:
    """校验服务器指令，返回 (ok, error_msg, is_dangerous)。

    拒绝包含换行/回车等控制字符的指令：指令会被原样写入服务端 stdin
    （``server.py`` 的 ``send``），一条含 ``\\n`` 的输入可在服务端执行多条指令，
    从而绕过危险指令的二次确认。
    """
    if not cmd or not cmd.strip():
        return False, "指令不能为空", False
    # 控制字符检查：换行/回车可注入多条指令；制表符之外的 C0 控制字符与 DEL 一律拒绝
    if any((ord(ch) < 32 and ch != "\t") or ord(ch) == 127 for ch in cmd):
        return False, "指令不能包含换行或控制字符", False
    normalized = _normalize_command(cmd)
    if not normalized:
        return False, "指令不能为空", False
    cmd_lower = normalized.lower()
    parts = cmd_lower.split()
    main_cmd = parts[0] if parts else ""
    if AppContext.instance().settings.get("command_whitelist", False) and main_cmd not in SAFE_COMMANDS:
        return False, f"指令 '{main_cmd}' 不在白名单中，已被拒绝", False
    is_dangerous = _is_dangerous_command(cmd_lower)
    return True, "", is_dangerous


def _is_official_host(hostname: str) -> bool:
    """判断主机名是否属于官方下载域（相等或为子域）。

    必须按标签边界比较：简单 ``hostname.endswith("minecraft.net")``
    会把 ``fakeminecraft.net`` 误判为官方来源。
    """
    host = (hostname or "").lower().rstrip(".")
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOWNLOAD_DOMAINS)


def _normalize_ip(ip):
    """把 IPv4-mapped IPv6（``::ffff:127.0.0.1``）折叠成对应的 IPv4 地址。

    ``ipaddress`` 认为 ``::ffff:127.0.0.1`` 是 IPv6，用 ``in ip_network("127.0.0.0/8")``
    判断会得到 False，于是映射地址可以绕过内网拦截直连回环/元数据地址。
    """
    if getattr(ip, "version", 4) == 6:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            return mapped
    return ip


def _is_blocked_ip(ip) -> bool:
    """判断 IP 是否落在禁止访问的网段内（先折叠 IPv4-mapped IPv6）。"""
    ip = _normalize_ip(ip)
    return any(ip in net for net in _BLOCKED_NETWORKS)


def _resolve_host_ips(hostname: str) -> list:
    """解析主机名到 IP 列表（失败返回空列表）。"""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    ips = []
    for info in infos:
        addr = info[4][0]
        try:
            ips.append(ipaddress.ip_address(addr.split("%")[0]))
        except ValueError:
            continue
    return ips


def validate_custom_url(url: str, resolve_dns: bool = True) -> tuple:
    """校验自定义下载URL，返回 (ok, error_msg, is_official)。

    拦截：
    - 非 http/https 协议
    - 超长 URL
    - localhost / 云元数据等特殊主机名
    - 指向内网、回环、链路本地（含 169.254.169.254）等地址（SSRF）
    - 官方域名判定按主机名标签边界比较，不允许后缀伪造

    Args:
        url: 待校验的下载地址
        resolve_dns: 是否解析域名并检查解析结果（构造/单元测试可关闭）

    Returns:
        tuple: (ok, error_msg, is_official)
    """
    try:
        if not url or not isinstance(url, str):
            return False, "URL 不能为空", False
        if len(url) > MAX_CUSTOM_URL_LEN:
            return False, f"URL 长度不能超过 {MAX_CUSTOM_URL_LEN} 字符", False
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return False, "仅支持 http/https 协议", False
        if not u.netloc or not u.hostname:
            return False, "URL 格式不正确", False
        # 拒绝 URL 中的用户信息（可伪装主机，如 http://minecraft.net@evil.com/）
        if u.username or u.password:
            return False, "URL 不能包含用户信息（user:pass@）", False
        hostname = u.hostname.lower().rstrip(".")
        if hostname in _BLOCKED_HOSTNAMES:
            return False, f"禁止访问本地/特殊主机: {hostname}", False

        is_official = _is_official_host(hostname)

        # 字面量 IP 直接判定
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        if literal is not None:
            if _is_blocked_ip(literal):
                return False, f"禁止访问内网/保留地址: {hostname}", False
        elif resolve_dns:
            ips = _resolve_host_ips(hostname)
            # 只要解析结果里存在内网/保留地址就拒绝（下载时可能选中其中任意一个）
            if any(_is_blocked_ip(ip) for ip in ips):
                return False, f"域名解析到内网/保留地址，已拒绝: {hostname}", False

        return True, "ok" if is_official else f"非官方来源: {hostname}，请确认可信", is_official
    except (ValueError, TypeError, AttributeError) as e:
        safe_log_exception("security.py", f"URL解析失败: {e}", "warning")
        return False, "URL 解析失败", False


def validate_effective_url(url: str) -> tuple:
    """校验重定向后的最终地址（SSRF 兜底）。

    只校验用户填写的 URL 不够：服务端可能 302 到 169.254.169.254 等内网地址。
    下载器在拿到最终 URL 后应调用本函数再次确认。

    Args:
        url: 重定向后的实际请求地址

    Returns:
        tuple: (ok, error_msg)
    """
    ok, err, _official = validate_custom_url(url)
    if not ok:
        return False, f"重定向到不允许的地址，已中止: {err}"
    return True, ""


def validate_reason(reason: str, max_len: int = 200) -> tuple:
    """校验原因/公告消息文本：禁止换行与控制字符，限制长度。

    kick/ban/ban-ip 的 reason 与定时公告 message 会被拼接到服务器命令中，
    若包含换行可注入多条命令（如 "say x\\nstop"），必须拒绝。

    Args:
        reason: 待校验文本（可为空字符串或 None）
        max_len: 最大允许长度

    Returns:
        tuple: (ok, error_msg)
    """
    if reason is None:
        return True, ""
    r = str(reason).strip()
    if len(r) > max_len:
        return False, f"文本长度不能超过 {max_len} 字符"
    if "\n" in r or "\r" in r:
        return False, "文本不能包含换行符"
    return True, ""


# 审计日志轮转参数：超过阈值时 audit.log → audit.1.log → …，最多保留 AUDIT_LOG_MAX_FILES-1 个历史文件。
# 审计日志只追加、不轮转时，长期运行会无限增长（每次启停/改配置/管玩家都写一行）。
AUDIT_LOG_MAX_BYTES = 2 * 1024 * 1024  # 2MB
AUDIT_LOG_MAX_FILES = 3

# 轮转是"删除 + 多次 rename"的组合操作，不是原子的：
# 多线程同时判定超阈值会各自轮转一次，把刚提升的代际整个删掉；
# rename 期间另一个线程的 append 还会拿到 PermissionError 而被静默丢弃。
# 用一把锁把「轮转 + 追加」串起来，保证审计链完整。
_audit_lock = threading.Lock()


def _rotate_audit_log(log_path: str) -> None:
    """按大小轮转审计日志（调用方需持有 ``_audit_lock``）。"""
    try:
        if not os.path.exists(log_path) or os.path.getsize(log_path) < AUDIT_LOG_MAX_BYTES:
            return
        base, ext = os.path.splitext(log_path)
        oldest = f"{base}.{AUDIT_LOG_MAX_FILES - 1}{ext}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(AUDIT_LOG_MAX_FILES - 2, -1, -1):
            src = f"{base}.{i}{ext}" if i > 0 else log_path
            dst = f"{base}.{i + 1}{ext}"
            if os.path.exists(src):
                if os.path.exists(dst):
                    os.remove(dst)
                os.rename(src, dst)
    except OSError as err:
        safe_log_exception("security.py", f"审计日志轮转失败: {err}", "warning")


def audit_log(action, detail="", server_dir=None):
    """记录操作审计日志（追加 JSON 行，按大小轮转）。

    轮转与追加在同一把锁内完成，避免并发下丢条目或误删整代历史文件。
    """
    try:
        d = server_dir or resolve_server_dir()
        if not d:
            return
        log_path = os.path.join(d, AUDIT_LOG_NAME)
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "detail": detail,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _audit_lock:
            _rotate_audit_log(log_path)
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                # 追加失败（文件被短暂占用等）重试一次；仍失败则抛给外层写 stderr，
                # 绝不静默丢弃审计记录。
                time.sleep(0.05)
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line)
    except Exception as e:
        # 审计日志写入失败时输出到控制台，便于排查（不能静默吞掉审计记录）
        try:
            import sys

            print(f"[警告] 审计日志写入失败（该操作未留痕）: {e}", file=sys.stderr)
        except Exception as e2:
            safe_log_exception("security", f"audit_log 打印失败: {e2}", "warning")


def _audit_log_generations(log_path: str) -> list[str]:
    """按「最新 → 最旧」返回审计日志代际文件（含当前 audit.log）。

    轮转后的编号越大越旧：``audit.log`` 最新，``audit.1.log`` 次之。
    早期这里把编号倒着遍历，返回的是 [audit.log, audit.2, audit.1]，
    拼接后时间顺序错乱（读回来是 A27,A28,A29,A21…）。
    """
    base, ext = os.path.splitext(log_path)
    paths = []
    if os.path.exists(log_path):
        paths.append(log_path)
    for i in range(1, AUDIT_LOG_MAX_FILES):
        candidate = f"{base}.{i}{ext}"
        if os.path.exists(candidate):
            paths.append(candidate)
    return paths


def _tail_lines(path: str, limit: int, max_read: int = 512 * 1024) -> list[str]:
    """从文件尾部读取最多 limit 行（只读末尾一段，不整文件读入）。

    Args:
        path: 文件路径
        limit: 需要的行数
        max_read: 最多回读的字节数（避免超大文件一次读入内存）

    Returns:
        list[str]: 尾部行（可能少于 limit）
    """
    if limit <= 0:
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_read)
        f.seek(start)
        data = f.read()
        # 只有当前一段确实从半行中间开始时才丢弃首个片段。
        # 早期无条件丢弃：start 恰好落在行首时会白丢一条**完整**记录
        # （概率约 1/平均行长，表现为 /api/audit 偶尔少一条）。
        drop_first = False
        if start > 0:
            f.seek(start - 1)
            drop_first = f.read(1) != b"\n"
    lines = data.decode("utf-8", errors="replace").splitlines()
    if drop_first and lines:
        lines = lines[1:]
    return lines[-limit:]


def read_audit_log(server_dir=None, limit=200):
    """读取审计日志（跨轮转代际取最近的 limit 条）。

    轮转后的历史在 ``audit.1.log`` / ``audit.2.log`` 中，只读 ``audit.log``
    会在轮转瞬间让审计界面变空；这里按「旧 → 新」拼接后再取尾部。
    """
    try:
        d = server_dir or resolve_server_dir()
        if not d:
            return []
        log_path = os.path.join(d, AUDIT_LOG_NAME)
        generations = _audit_log_generations(log_path)
        if not generations:
            return []
        lines = []
        # 由旧到新**追加**，保证时间顺序（早期写成前置，最新一代被排到了最前面）
        for path in reversed(generations):
            need = limit - len(lines)
            if need <= 0:
                break
            lines = lines + _tail_lines(path, need)
        result = []
        for line in lines[-limit:]:
            with contextlib.suppress(Exception):
                result.append(json.loads(line.strip()))
        return result
    except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError) as e:
        safe_log_exception("security.py", f"读取审计日志失败: {e}", "warning")
        return []


# ---------------- 危险操作二次确认 ----------------
# 需要二次确认的危险操作端点
DANGEROUS_ENDPOINTS = {
    "/api/server/delete": {
        "confirm_key": "confirm_delete",
        "confirm_value": "DELETE",
        "message": "删除服务器是不可逆操作，请输入 DELETE 确认",
    },
    "/api/backups/restore": {
        "confirm_key": "confirm_restore",
        "confirm_value": "RESTORE",
        "message": "恢复备份会覆盖当前世界存档，请输入 RESTORE 确认",
    },
    "/api/worlds/delete": {
        "confirm_key": "confirm_delete",
        "confirm_value": "DELETE",
        "message": "删除世界是不可逆操作，请输入 DELETE 确认",
    },
    "/api/worlds/import": {
        "confirm_key": "confirm_import",
        "confirm_value": "IMPORT",
        "message": "导入世界会覆盖当前世界存档，请输入 IMPORT 确认",
    },
    "/api/packs/delete": {
        "confirm_key": "confirm_delete",
        "confirm_value": "DELETE",
        "message": "删除包是不可逆操作，请输入 DELETE 确认",
    },
    "/api/server/stop": {
        "confirm_key": "confirm_stop",
        "confirm_value": "STOP",
        "message": "停止服务器会断开所有玩家连接，请输入 STOP 确认",
        "optional": True,  # 可选确认，默认不强制
    },
    "/api/server/restart": {
        "confirm_key": "confirm_restart",
        "confirm_value": "RESTART",
        "message": "重启服务器会短暂断开所有玩家连接，请输入 RESTART 确认",
        "optional": True,
    },
}


def check_dangerous_operation(endpoint: str, data: dict) -> tuple:
    """检查危险操作是否已通过二次确认。

    Args:
        endpoint: API 端点路径
        data: 请求数据字典

    Returns:
        tuple: (ok, error_message)
            ok: 是否允许执行
            error_message: 如果不允许，返回错误信息
    """
    if endpoint not in DANGEROUS_ENDPOINTS:
        return True, ""

    config = DANGEROUS_ENDPOINTS[endpoint]
    confirm_key = config["confirm_key"]
    confirm_value = config["confirm_value"]
    message = config["message"]
    optional = config.get("optional", False)

    # 检查是否传入了确认参数
    user_confirm = data.get(confirm_key, "")
    if isinstance(user_confirm, str):
        user_confirm = user_confirm.strip().upper()

    if user_confirm == confirm_value:
        return True, ""

    # 如果是可选确认，且用户没有传入确认参数，则允许执行（但记录警告）
    if optional and not user_confirm:
        return True, ""

    # 必须确认但未确认或确认值错误
    if not user_confirm:
        return False, f"{message}（需要在请求中传入 {confirm_key}={confirm_value}）"
    else:
        return False, f"确认值不正确，预期为 '{confirm_value}'，实际为 '{user_confirm}'"
