"""
玩家管理模块：玩家名校验、在线玩家解析、权限/封禁/白名单管理。
"""

import json
import os
import re
import time

from .app_context import AppContext
from .app_logger import safe_log_exception
from .security import audit_log
from .server import get_server_proc, server_running
from .utils import resolve_server_dir

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
    """从 list 命令输出解析在线玩家名列表。"""
    if not text:
        return []
    players = []
    # 格式: There are X/Y players online: p1, p2
    m = re.search(r"players online:\s*(.+)", text, re.IGNORECASE)
    if m:
        names = m.group(1).strip()
        if names:
            players = [n.strip() for n in names.split(",") if n.strip()]
    # 兜底：逐行找可能的玩家名
    if not players:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            if any(k in low for k in ["player", "connect", "online", "there are", "level", "dimension", "xuid"]):
                continue
            if re.match(r"^[A-Za-z0-9_ .]+$", line) and 1 < len(line) <= 20:
                players.append(line)
    return players


def read_permissions(server_dir: str) -> list[dict]:
    """读取 permissions.json，返回 {name: level}。"""
    result = {}
    if not server_dir:
        return result
    path = os.path.join(server_dir, "permissions.json")
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
    path = os.path.join(server_dir, "banned-players.json")
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
    path = os.path.join(server_dir, "allowlist.json")
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
    path = os.path.join(server_dir, "allowlist.json")
    try:
        data = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = []
        if not any((item.get("name", "") if isinstance(item, dict) else str(item)) == name for item in data):
            data.append({"name": name, "ignoresPlayerLimit": False})
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def remove_allowlist(server_dir: str, name: str) -> bool:
    """从白名单移除玩家。服务器运行时通过命令，否则直接修改文件。"""

    name = (name or "").strip()
    if not name:
        return False
    if server_running():
        get_server_proc().send(f"whitelist remove {name}")
        return True
    path = os.path.join(server_dir, "allowlist.json")
    try:
        if not os.path.exists(path):
            return True
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            data = [item for item in data if (item.get("name", "") if isinstance(item, dict) else str(item)) != name]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


BANNED_IPS_FILE = "banned-ips.json"


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
        entry = {"ip": ip, "reason": reason, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "source": "manual"}
        banned.append(entry)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(banned, f, indent=2, ensure_ascii=False)
        if server_running():
            try:
                get_server_proc().send(f"ban-ip {ip} {reason}")
            except Exception as e:
                try:
                    from .app_logger import get_app_logger

                    get_app_logger().warning(f"players.py 发送 ban-ip 命令异常: {e}")
                except Exception:
                    # 应用日志记录失败时静默
                    pass
        audit_log("BAN_IP", f"{ip} {reason}", d)
        return True, ""
    except Exception as e:
        return False, str(e)


def pardon_ip(ip, server_dir=None):

    try:
        d = server_dir or resolve_server_dir()
        if not d:
            return False, "未设置服务器目录"
        p = os.path.join(d, BANNED_IPS_FILE)
        banned = load_banned_ips(d)
        new_banned = [e for e in banned if e.get("ip") != ip and e.get("address") != ip]
        if len(new_banned) == len(banned):
            return False, "IP不在封禁列表中"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(new_banned, f, indent=2, ensure_ascii=False)
        if server_running():
            try:
                get_server_proc().send(f"pardon-ip {ip}")
            except Exception as e:
                try:
                    from .app_logger import get_app_logger

                    get_app_logger().warning(f"players.py 发送 pardon-ip 命令异常: {e}")
                except Exception:
                    # 应用日志记录失败时静默
                    pass
        audit_log("PARDON_IP", ip, d)
        return True, ""
    except Exception as e:
        return False, str(e)


def list_installed_packs(server_dir=None):

    try:
        d = server_dir or resolve_server_dir()
        if not d:
            return {"resource_packs": [], "behavior_packs": [], "valid_known": []}
        result = {"resource_packs": [], "behavior_packs": [], "valid_known": []}
        for pack_type, dir_name in [("resource_packs", "resource_packs"), ("behavior_packs", "behavior_packs")]:
            pack_dir = os.path.join(d, dir_name)
            if os.path.isdir(pack_dir):
                for name in os.listdir(pack_dir):
                    full = os.path.join(pack_dir, name)
                    if os.path.isdir(full):
                        pack_info = {"name": name, "path": full, "enabled": False}
                        result[pack_type].append(pack_info)
        return result
    except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError) as e:
        safe_log_exception("players.py", f"读取包信息失败: {e}", "warning")
        return {"resource_packs": [], "behavior_packs": [], "valid_known": []}


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


def _player_action(name, command_template, log_msg, server_dir=None):
    """通用玩家操作：校验玩家名 → 检查服务器运行 → 发送命令 → 写日志 → 返回。"""

    ok, err = validate_player_name(name)
    if not ok:
        return False, err
    if not server_running():
        return False, "服务器未运行"
    try:
        cmd = command_template % name
        get_server_proc().send(cmd)
        audit_log(log_msg, name, server_dir)
        return True, ""
    except Exception as e:
        return False, str(e)
