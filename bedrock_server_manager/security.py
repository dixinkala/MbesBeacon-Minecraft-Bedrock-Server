"""
安全模块：指令校验、审计日志、自定义URL校验、API token。
"""

import contextlib
import json
import os
import secrets
import time
from urllib.parse import urlparse

from .app_context import AppContext
from .app_logger import safe_log_exception
from .constants import (
    DANGEROUS_MULTI_COMMANDS as _DANGEROUS_MULTI,
    DANGEROUS_SINGLE_COMMANDS as _DANGEROUS_SINGLE,
)
from .utils import resolve_server_dir

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
    """校验服务器指令，返回 (ok, error_msg, is_dangerous)。"""
    if not cmd or not cmd.strip():
        return False, "指令不能为空", False
    cmd_lower = cmd.strip().lower()
    main_cmd = cmd_lower.split()[0] if cmd_lower.split() else ""
    if AppContext.instance().settings.get("command_whitelist", False) and main_cmd not in SAFE_COMMANDS:
        return False, f"指令 '{main_cmd}' 不在白名单中，已被拒绝", False
    is_dangerous = _is_dangerous_command(cmd_lower)
    return True, "", is_dangerous


def validate_custom_url(url: str) -> tuple:
    """校验自定义下载URL，返回 (ok, error_msg, is_official)。"""
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return False, "仅支持 http/https 协议", False
        if not u.netloc:
            return False, "URL 格式不正确", False
        official = (
            "minecraft.net",
            "www.minecraft.net",
            "bedrock.dev",
            "minecraftservices.net",
            "xboxlive.com",
            "microsoft.com",
        )
        is_official = any(u.netloc.endswith(d) for d in official)
        return True, "ok" if is_official else f"非官方来源: {u.netloc}，请确认可信", is_official
    except (ValueError, TypeError, AttributeError) as e:
        safe_log_exception("security.py", f"URL解析失败: {e}", "warning")
        return False, "URL 解析失败", False


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


def audit_log(action, detail="", server_dir=None):
    """记录操作审计日志。"""
    try:
        d = server_dir or resolve_server_dir()
        if not d:
            return
        log_path = os.path.join(d, "audit.log")
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "detail": detail,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        # 审计日志写入失败时输出到控制台，便于排查
        try:
            import sys

            print(f"[警告] 审计日志写入失败: {e}", file=sys.stderr)
        except Exception:
            # stderr 也不可用时静默（极少见）
            pass


def read_audit_log(server_dir=None, limit=200):
    """读取审计日志。"""
    try:
        d = server_dir or resolve_server_dir()
        if not d:
            return []
        log_path = os.path.join(d, "audit.log")
        if not os.path.exists(log_path):
            return []
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        result = []
        for line in lines[-limit:]:
            with contextlib.suppress(Exception):
                result.append(json.loads(line.strip()))
        return result
    except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError) as e:
        safe_log_exception("security.py", f"读取审计日志失败: {e}", "warning")
        return []


def generate_api_token():
    """生成随机 API token 用于 CSRF 防护。"""
    return secrets.token_hex(16)


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


def is_dangerous_endpoint(endpoint: str) -> bool:
    """检查端点是否为危险操作端点。"""
    return endpoint in DANGEROUS_ENDPOINTS


def get_dangerous_endpoints_info() -> dict:
    """获取所有危险操作端点的配置信息（供前端展示）。"""
    result = {}
    for endpoint, config in DANGEROUS_ENDPOINTS.items():
        result[endpoint] = {
            "confirm_key": config["confirm_key"],
            "confirm_value": config["confirm_value"],
            "message": config["message"],
            "optional": config.get("optional", False),
        }
    return result
