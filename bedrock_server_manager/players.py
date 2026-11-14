"""
玩家管理模块：玩家名校验、在线玩家解析、权限/封禁/白名单管理。
"""

import contextlib
import json
import os
import re
import time

from .app_context import AppContext
from .app_logger import safe_log_exception
from .config import ALLOWLIST_FILE, BANNED_IPS_FILE, BANNED_PLAYERS_FILE, PERMISSIONS_FILE
from .security import audit_log
from .server import get_server_proc, server_running
from .utils import atomic_write_json, resolve_server_dir

# 玩家名校验正则
_PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_.]+$")


def validate_player_name(name: str) -> tuple:
    """校验玩家名：只允许字母、数字、下划线、点号，长度1-16。返回 (ok, error_msg)。"""
    name = (name or "").strip()
    if not name:
        return False, "玩家名不能为空"
    if len(name) > 16:
        return False, "玩家名长度不能超过16个字符"
    if not _PLAYER_NAME_RE.match(name):
        return False, "玩家名只能包含字母、数字、下划线和点号"
    return True, ""


def send_command_capture(cmd, timeout=2.5):
    """发送服务器命令并捕获接下来的控制台输出，返回输出文本；服务器未运行返回 None。
    使用 ConsoleBuffer.wait_for_new 等待输出，服务器响应快时立即返回，
    不再固定 sleep 满 timeout，显著降低 /api/players 等接口的响应延迟。"""
    console = AppContext.instance().console

    if not server_running():
        return None
    _, before = console.read_since(0)
    p = get_server_proc()
    try:
        p.send(cmd)
    except (OSError, ValueError, AttributeError) as e:
        safe_log_exception("players.py", f"发送命令失败: {e}", "warning")
        return None
    # 等待第一行输出到达（最多 timeout 秒），有输出则提前返回
    text, new_count = console.wait_for_new(before, timeout=timeout)
    if text:
        # 已收到输出，再短暂等待捕获可能紧随其后的多行输出
        extra, _ = console.wait_for_new(new_count, timeout=0.3)
        if extra:
            text += extra
    return text


def parse_online_players(text: str) -> list:
    """从 list 命令输出解析在线玩家名列表。

    只认 BDS 的固定输出格式 ``There are X/Y players online: p1, p2``。
    早期还有一条"逐行猜玩家名"的兜底（匹配 ``[A-Za-z0-9_ .]{2,20}``），
    会把回显的命令行（``list`` / ``version``）和日志行误判成玩家，
    在玩家列表里凭空多出条目；已移除——格式固定时兜底只会带来误报。
    """
    if not text:
        return []
    # 格式: There are X/Y players online: p1, p2
    m = re.search(r"players online:\s*(.*)", text, re.IGNORECASE)
    if not m:
        return []
    names = m.group(1).strip()
    if not names:
        return []
    # 按逗号切分；基岩允许空格与中文名，故不用 ASCII 白名单过滤
    return [n.strip() for n in names.split(",") if n.strip()]


def read_permissions(server_dir: str) -> list[dict]:
    """读取 permissions.json，返回 {name: level}。"""
    result = {}
    if not server_dir:
        return result
    path = os.path.join(server_dir, PERMISSIONS_FILE)
    if not os.path.exists(path):
        return result
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for item in data:
                name = item.get("name", "")
                level = item.get("permission", "member")
                if name:
                    result[name] = level
    except Exception as e:
        safe_log_exception("players", f"操作失败: {e}", "warning")
    return result


def read_banlist(server_dir: str) -> list[dict]:
    """读取 banned-players.json，返回 [{"name":..., "reason":...}]。"""
    result = []
    if not server_dir:
        return result
    path = os.path.join(server_dir, BANNED_PLAYERS_FILE)
    if not os.path.exists(path):
        return result
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for item in data:
                name = item.get("name", "")
                if name:
                    result.append({"name": name, "reason": item.get("reason", "")})
    except Exception as e:
        safe_log_exception("players", f"操作失败: {e}", "warning")
    return result


def read_allowlist(server_dir: str) -> list[dict]:
    """读取 allowlist.json，返回 [{"name":...}]。"""
    result = []
    if not server_dir:
        return result
    path = os.path.join(server_dir, ALLOWLIST_FILE)
    if not os.path.exists(path):
        return result
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for item in data:
                name = item.get("name", "") if isinstance(item, dict) else str(item)
                if name:
                    result.append({"name": name})
    except Exception as e:
        safe_log_exception("players", f"操作失败: {e}", "warning")
    return result


def add_allowlist(server_dir: str, name: str) -> bool:
    """添加玩家到白名单。服务器运行时通过命令，否则直接修改文件。"""

    name = (name or "").strip()
    if not name:
        return False
    if server_running():
        get_server_proc().send(f"whitelist add {name}")
        return True
    path = os.path.join(server_dir, ALLOWLIST_FILE)
    try:
        data = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = []
        if not any((item.get("name", "") if isinstance(item, dict) else str(item)) == name for item in data):
            data.append({"name": name, "ignoresPlayerLimit": False})
            atomic_write_json(path, data)
        return True
    except Exception as e:
        safe_log_exception("players", f"add_allowlist 忽略异常: {e}", "warning")
        return False


def remove_allowlist(server_dir: str, name: str) -> bool:
    """从白名单移除玩家。服务器运行时通过命令，否则直接修改文件。"""

    name = (name or "").strip()
    if not name:
        return False
    if server_running():
        get_server_proc().send(f"whitelist remove {name}")
        return True
    path = os.path.join(server_dir, ALLOWLIST_FILE)
    try:
        if not os.path.exists(path):
            return True
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            data = [item for item in data if (item.get("name", "") if isinstance(item, dict) else str(item)) != name]
            atomic_write_json(path, data)
        return True
    except Exception as e:
        safe_log_exception("players", f"remove_allowlist 忽略异常: {e}", "warning")
        return False


def load_banned_ips(server_dir: str | None = None) -> list[dict]:

    try:
        d = server_dir or resolve_server_dir()
        if not d:
            return []
        p = os.path.join(d, BANNED_IPS_FILE)
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError) as e:
        safe_log_exception("players.py", f"读取JSON文件失败: {e}", "warning")
        return []


def validate_ip(ip: str) -> tuple[bool, str]:
    """校验 IP 地址格式（IPv4 或 IPv6）。返回 (ok, error_msg)。"""
    import ipaddress

    if not ip or not isinstance(ip, str):
        return False, "IP 地址不能为空"
    ip = ip.strip()
    try:
        # 尝试解析为 IPv4 或 IPv6
        ipaddress.ip_address(ip)
        return True, ""
    except ValueError:
        return False, f"无效的 IP 地址格式: {ip}"


def ban_ip(ip, reason="", server_dir=None):

    try:
        # 校验 IP 格式
        ip_ok, ip_err = validate_ip(ip)
        if not ip_ok:
            return False, ip_err
        # 校验原因文本（禁换行，防止注入多条命令）
        from .security import validate_reason

        reason_ok, reason_err = validate_reason(reason)
        if not reason_ok:
            return False, reason_err
        d = server_dir or resolve_server_dir()
        if not d:
            return False, "未设置服务器目录"
        p = os.path.join(d, BANNED_IPS_FILE)
        banned = load_banned_ips(d)
        for entry in banned:
            if entry.get("ip") == ip or entry.get("address") == ip:
                return False, "IP已在封禁列表中"
        running = server_running()
        if running:
            # 运行中先发命令让服务端立刻生效（BDS 自己会持久化，可能丢掉
            # created/source 字段）。命令失败就不写文件，避免"文件里有、实际没封"。
            try:
                get_server_proc().send(f"ban-ip {ip} {reason}")
            except Exception as e:
                safe_log_exception("players.py", f"发送 ban-ip 命令失败: {e}", "warning")
                return False, f"发送封禁命令失败: {e}"
        # 无论运行与否都补写文件：UI 的封禁列表读的是文件（load_banned_ips），
        # 只发命令会让"刚封的 IP 在界面上不显示、也无法解封"。
        # 备份后用 ip/address 去重，BDS 之后覆盖该文件也不会产生重复项。
        entry = {"ip": ip, "reason": reason, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "source": "manual"}
        banned.append(entry)
        with contextlib.suppress(OSError, TypeError, ValueError):
            atomic_write_json(p, banned)
        audit_log("BAN_IP", f"{ip} {reason}", d)
        return True, ""
    except Exception as e:
        safe_log_exception("players", f"ban_ip 忽略异常: {e}", "warning")
        return False, str(e)


def pardon_ip(ip, server_dir=None):

    try:
        ip_ok, ip_err = validate_ip(ip)
        if not ip_ok:
            return False, ip_err
        d = server_dir or resolve_server_dir()
        if not d:
            return False, "未设置服务器目录"
        p = os.path.join(d, BANNED_IPS_FILE)
        banned = load_banned_ips(d)
        new_banned = [e for e in banned if e.get("ip") != ip and e.get("address") != ip]
        removed = len(new_banned) != len(banned)
        running = server_running()
        if running:
            try:
                get_server_proc().send(f"pardon-ip {ip}")
            except Exception as e:
                safe_log_exception("players.py", f"发送 pardon-ip 命令失败: {e}", "warning")
                return False, f"发送解封命令失败: {e}"
        elif not removed:
            return False, "IP不在封禁列表中"
        if removed:
            with contextlib.suppress(OSError, TypeError, ValueError):
                atomic_write_json(p, new_banned)
        audit_log("PARDON_IP", ip, d)
        return True, ""
    except Exception as e:
        safe_log_exception("players", f"pardon_ip 忽略异常: {e}", "warning")
        return False, str(e)


def list_installed_packs(server_dir=None):
    """已迁移至 packs.py（增强版：解析 manifest/valid_known/世界启用状态）。"""
    from .packs import list_installed_packs as _list_installed_packs

    return _list_installed_packs(server_dir)


def get_all_player_status(server_dir=None):

    try:
        d = server_dir or resolve_server_dir()
        if not d:
            return []
        players = {}
        perms = read_permissions(d)
        for name, level in perms.items():
            players[name] = {"name": name, "permission": level, "whitelisted": False, "banned": False}
        allow = read_allowlist(d)
        for item in allow:
            name = item.get("name", "")
            if name:
                if name not in players:
                    players[name] = {"name": name, "permission": "member", "whitelisted": True, "banned": False}
                else:
                    players[name]["whitelisted"] = True
        banned = read_banlist(d)
        for item in banned:
            name = item.get("name", "")
            if name:
                if name not in players:
                    players[name] = {"name": name, "permission": "member", "whitelisted": False, "banned": True}
                else:
                    players[name]["banned"] = True
        return list(players.values())
    except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError) as e:
        safe_log_exception("players.py", f"读取玩家状态失败: {e}", "warning")
        return []
