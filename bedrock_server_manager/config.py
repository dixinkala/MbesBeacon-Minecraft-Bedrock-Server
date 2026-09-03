"""
配置模块：常量、PROP_META、配置校验、properties 文件读写。
"""

import contextlib
from .app_logger import get_app_logger, safe_log_exception
import os
import shutil
import threading
import time

PROP_META = [
    ("server-name", "服务器名称", "text", "Dedicated Server"),
    ("gamemode", "游戏模式", "choice", ["survival", "creative", "adventure", "spectator"]),
    ("difficulty", "难度", "choice", ["peaceful", "easy", "normal", "hard"]),
    ("allow-cheats", "允许作弊(指令)", "bool", "true"),
    ("max-players", "最大玩家数", "int", "10"),
    ("online-mode", "在线模式(正版验证)", "bool", "true"),
    ("allow-list", "允许名单(白名单)", "bool", "false"),
    ("server-port", "IPv4 端口", "int", "19132"),
    ("server-portv6", "IPv6 端口", "int", "19133"),
    ("level-name", "存档名称(世界名)", "text", "Bedrock level"),
    ("level-seed", "世界种子(留空随机)", "text", ""),
    ("view-distance", "视距(区块)", "int", "32"),
    ("tick-distance", "模拟距离(区块)", "int", "4"),
    ("default-player-permission-level", "新玩家默认权限", "choice", ["visitor", "member", "operator"]),
    ("force-gamemode", "强制统一游戏模式", "bool", "false"),
    ("spawn-protection", "出生点保护范围", "int", "16"),
    ("language", "语言(zh_CN=中文)", "text", "zh_CN"),
]
# ---------------- 新增功能常量 ----------------
AUDIT_LOG_NAME = "audit.log"
CONFIG_HISTORY_DIR = ".config_history"
VERSION_CACHE_FILE = ".versions_cache.json"
VERSION_CACHE_TTL = 3600  # 1小时
SCHEDULED_TASKS_FILE = "scheduled_tasks.json"
PACKS_CONFIG = "valid_known_packs.json"
PERMISSIONS_FILE = "permissions.json"
ALLOWLIST_FILE = "allowlist.json"
BANNED_IPS_FILE = "banned-ips.json"
BANNED_PLAYERS_FILE = "banned-players.json"

# 全量配置项元数据（高级模式）
FULL_PROP_META = PROP_META + [
    ("level-type", "世界类型", "choice", ["DEFAULT", "FLAT", "LEGACY"]),
    ("texturepack-required", "强制资源包", "bool", "false"),
    ("content-log-file-enabled", "启用内容日志", "bool", "false"),
    ("compression-threshold", "压缩阈值", "int", "1"),
    ("compression-algorithm", "压缩算法", "choice", ["zlib", "snappy"]),
    (
        "server-authoritative-movement",
        "服务端权威移动",
        "choice",
        ["client-auth", "server-auth", "server-auth-with-rewind"],
    ),
    ("player-movement-score-threshold", "移动异常分数阈值", "int", "20"),
    ("player-movement-distance-threshold", "移动距离阈值", "float", "0.3"),
    ("player-movement-duration-threshold-in-ms", "移动持续时间阈值(ms)", "int", "500"),
    ("correct-player-movement", "纠正玩家移动", "bool", "false"),
    ("disable-player-interaction", "禁用玩家交互", "bool", "false"),
    ("disable-interactive-block", "禁用交互方块", "bool", "false"),
    ("server-authoritative-block-breaking", "服务端权威破环", "bool", "false"),
    ("block-network-ids-are-hashes", "方块网络ID为哈希", "bool", "true"),
    ("disable-persona", "禁用角色", "bool", "false"),
    ("disable-custom-skins", "禁用自定义皮肤", "bool", "false"),
    ("allow-outbound-sorting", "允许出站排序", "bool", "true"),
    ("use-microsoft-graph-api-for-chat", "使用微软Graph API聊天", "bool", "false"),
    ("enable-lan-visibility", "启用局域网可见", "bool", "true"),
    ("live-reload-enabled", "启用热重载", "bool", "false"),
    ("enforce-whitelist", "强制白名单", "bool", "false"),
    ("enforce-secure-profile", "强制安全档案", "bool", "true"),
    ("emit-server-telemetry", "发送服务器遥测", "bool", "false"),
    ("overworld-height", "主世界高度", "int", "-64"),
    ("overworld-depth", "主世界深度", "int", "320"),
    ("nether-height", "下界高度", "int", "0"),
    ("nether-depth", "下界深度", "int", "128"),
    ("the-end-height", "末地高度", "int", "0"),
    ("the-end-depth", "末地深度", "int", "128"),
]

# 性能监控数据缓存
_perf_cache = {"cpu": 0, "memory": 0, "players": 0, "timestamp": 0}
_perf_lock = threading.Lock()

# 定时任务调度器
_scheduler_thread = None
_scheduler_stop = threading.Event()

# 配置项数值范围校验
_CONFIG_RANGES = {
    "server-port": (1, 65535),
    "server-portv6": (1, 65535),
    "max-players": (1, 99999),
    "view-distance": (3, 96),
    "tick-distance": (2, 12),
    "spawn-protection": (0, 100),
    "compression-threshold": (0, 65535),
    "player-movement-score-threshold": (0, 1000),
    "player-movement-duration-threshold-in-ms": (0, 10000),
    "overworld-height": (-512, 512),
    "overworld-depth": (0, 1024),
    "nether-height": (-512, 512),
    "nether-depth": (0, 1024),
    "the-end-height": (-512, 512),
    "the-end-depth": (0, 1024),
}

# 文本类型配置项长度限制
_CONFIG_TEXT_LENGTHS = {
    "server-name": (1, 100),
    "level-name": (1, 100),
    "level-seed": (0, 100),
    "language": (2, 10),
}


def detect_file_encoding(filepath: str) -> str:
    """检测文件编码，优先 UTF-8，失败则尝试 GBK。"""
    if not os.path.isfile(filepath):
        return "utf-8"
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            with open(filepath, encoding=enc) as f:
                f.read()
            return enc
        except (UnicodeDecodeError, UnicodeError):
            continue
    return "utf-8"


def load_properties(filepath: str) -> list[tuple[str, str]]:
    """加载 server.properties 文件，返回 [(key, value), ...]。
    注释和空行会被跳过。"""
    if not os.path.isfile(filepath):
        return []
    enc = detect_file_encoding(filepath)
    items = []
    try:
        with open(filepath, encoding=enc, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    items.append((key.strip(), value.strip()))
    except Exception:
        return []
    return items


def save_properties(filepath: str, items: list[tuple[str, str]]) -> bool:
    """保存 server.properties 文件。
    items 格式为 [(key, value), ...]。
    会保留原文件中的注释和未在 items 中列出的项。"""
    try:
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        # 读取原文件，保留注释和未列出的项
        original_lines = []
        if os.path.isfile(filepath):
            enc = detect_file_encoding(filepath)
            with open(filepath, encoding=enc, errors="replace") as f:
                original_lines = f.readlines()
        # 构建新的键值对字典
        new_dict = dict(items)
        # 写入文件：先写原文件中需要保留的行，再写新的或更新的项
        written_keys = set()
        with open(filepath, "w", encoding="utf-8", newline="\n") as f:
            for line in original_lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    f.write(line if line.endswith("\n") else line + "\n")
                    continue
                if "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    if key in new_dict:
                        f.write(f"{key}={new_dict[key]}\n")
                        written_keys.add(key)
                    else:
                        # 保留未列出的项
                        f.write(line if line.endswith("\n") else line + "\n")
                else:
                    f.write(line if line.endswith("\n") else line + "\n")
            # 写入新增的项
            for key, value in items:
                if key not in written_keys:
                    f.write(f"{key}={value}\n")
        return True
    except Exception:
        return False


def properties_to_dict(items):
    """将 properties 列表转换为字典。"""
    return dict(items)


# 保留端口列表（不建议使用）
_RESERVED_PORTS = {80, 443, 3389, 22, 21, 25, 53, 110, 143, 3306, 5432, 6379, 27017}


def _build_prop_meta_dict():
    """从 PROP_META 和 FULL_PROP_META 构建配置项元数据字典。

    元组格式说明：
    - 通用格式: (key, name, type, default)
    - choice 类型: (key, name, "choice", choices) 或 (key, name, "choice", default, choices)
    """
    meta_dict = {}
    for item in FULL_PROP_META:
        if len(item) >= 4:
            key, name, ptype = item[0], item[1], item[2]
            meta_dict[key] = {"name": name, "type": ptype}

            if ptype == "choice":
                # choice 类型：第4个元素可能是 choices 列表或 default
                if len(item) == 4:
                    # 格式: (key, name, "choice", choices)
                    meta_dict[key]["choices"] = item[3]
                    meta_dict[key]["default"] = item[3][0] if item[3] else ""
                elif len(item) >= 5:
                    # 格式: (key, name, "choice", default, choices)
                    meta_dict[key]["default"] = item[3]
                    meta_dict[key]["choices"] = item[4]
            else:
                # 其他类型：第4个元素是 default
                meta_dict[key]["default"] = item[3]
    return meta_dict


# 配置项元数据字典（运行时构建）
_PROP_META_DICT = None


def _get_prop_meta_dict():
    """获取配置项元数据字典（懒加载）。"""
    global _PROP_META_DICT
    if _PROP_META_DICT is None:
        _PROP_META_DICT = _build_prop_meta_dict()
    return _PROP_META_DICT


def validate_config_updates(updates: dict) -> tuple[bool, str, str]:
    """校验配置更新，返回 (ok, error_msg, field)。field 为出错的字段名。

    支持的校验类型：
    - int: 整数范围校验（使用 _CONFIG_RANGES）
    - float: 浮点数校验
    - bool: 布尔值校验（true/false）
    - choice: 枚举值校验（使用元数据中的可选列表）
    - text: 文本类型，不做特殊校验
    """
    if not isinstance(updates, dict):
        return False, "配置数据格式错误", ""

    meta_dict = _get_prop_meta_dict()

    for key, value in updates.items():
        # 获取配置项元数据
        meta = meta_dict.get(key)
        value_str = str(value).strip() if value is not None else ""

        # 1. 整数范围校验
        if key in _CONFIG_RANGES:
            try:
                n = int(value)
            except (ValueError, TypeError):
                return False, f"{key} 必须是整数", key
            lo, hi = _CONFIG_RANGES[key]
            if not (lo <= n <= hi):
                return False, "%s 取值范围 %d-%d" % (key, lo, hi), key
            # 端口保留检查
            if key in ("server-port", "server-portv6") and n in _RESERVED_PORTS:
                return False, "端口 %d 是常用端口，建议使用其他端口" % n, key
            continue

        # 2. 根据元数据类型校验
        if meta:
            ptype = meta.get("type", "text")

            if ptype == "bool":
                # 布尔值校验
                if value_str.lower() not in ("true", "false"):
                    return False, f"{key} 必须是 true 或 false", key

            elif ptype == "choice":
                # 枚举值校验
                choices = meta.get("choices", [])
                if choices and value_str not in choices:
                    return False, f"{key} 必须是以下值之一: {', '.join(choices)}", key

            elif ptype == "int":
                # 整数校验（不在 _CONFIG_RANGES 中的整数配置项）
                try:
                    int(value)
                except (ValueError, TypeError):
                    return False, f"{key} 必须是整数", key

            elif ptype == "float":
                # 浮点数校验
                try:
                    float(value)
                except (ValueError, TypeError):
                    return False, f"{key} 必须是数字", key

            elif ptype == "text":
                # 文本长度校验
                if key in _CONFIG_TEXT_LENGTHS:
                    min_len, max_len = _CONFIG_TEXT_LENGTHS[key]
                    if len(value_str) < min_len:
                        return False, f"{key} 长度不能少于 {min_len} 个字符", key
                    if len(value_str) > max_len:
                        return False, f"{key} 长度不能超过 {max_len} 个字符", key

    return True, "", ""


def backup_config_history(server_dir: str) -> str | None:
    """备份当前 server.properties 到历史目录。返回备份文件路径。"""
    props_path = os.path.join(server_dir, "server.properties")
    if not os.path.isfile(props_path):
        return None
    history_dir = os.path.join(server_dir, CONFIG_HISTORY_DIR)
    os.makedirs(history_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(history_dir, f"server.properties.{timestamp}.bak")
    try:
        shutil.copy2(props_path, backup_path)
        # 只保留最近 20 个备份
        backups = sorted(
            [f for f in os.listdir(history_dir) if f.startswith("server.properties.") and f.endswith(".bak")]
        )
        if len(backups) > 20:
            for old_backup in backups[:-20]:
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(history_dir, old_backup))
        return backup_path
    except Exception:
        return None


def list_config_history(server_dir: str) -> list[dict]:
    """列出配置历史备份。返回 [{name, path, time, size}, ...]"""
    history_dir = os.path.join(server_dir, CONFIG_HISTORY_DIR)
    if not os.path.isdir(history_dir):
        return []
    backups = []
    try:
        for f in sorted(os.listdir(history_dir), reverse=True):
            if f.startswith("server.properties.") and f.endswith(".bak"):
                full_path = os.path.join(history_dir, f)
                backups.append(
                    {
                        "name": f,
                        "path": full_path,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(full_path))),
                        "size": os.path.getsize(full_path),
                    }
                )
    except Exception as e:
        safe_log_exception("config", f"操作失败: {e}", "warning")
    return backups


def rollback_config(server_dir, backup_name):
    """从备份恢复 server.properties。返回 (ok, error_msg)。"""
    history_dir = os.path.join(server_dir, CONFIG_HISTORY_DIR)
    backup_path = os.path.join(history_dir, backup_name)
    # 安全校验：备份路径必须在历史目录内
    backup_abs = os.path.normpath(os.path.abspath(backup_path))
    history_abs = os.path.normpath(os.path.abspath(history_dir))
    if not (backup_abs == history_abs or backup_abs.startswith(history_abs + os.sep)):
        return False, "非法路径，只能恢复历史目录内的备份"
    if not os.path.isfile(backup_path):
        return False, "备份文件不存在"
    props_path = os.path.join(server_dir, "server.properties")
    try:
        # 先备份当前配置
        backup_config_history(server_dir)
        # 恢复备份
        shutil.copy2(backup_path, props_path)
        return True, ""
    except Exception as e:
        return False, f"恢复失败: {str(e)}"
