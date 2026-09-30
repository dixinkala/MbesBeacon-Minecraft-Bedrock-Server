"""
常量模块：集中管理所有应用常量，避免循环导入。
其他模块通过 `from .constants import *` 或具体导入访问常量。
此模块不依赖任何其他业务模块，是最底层的模块。
"""

import contextlib
import os

# ---------------- 应用常量 ----------------
APP_TITLE = "MbesBeacon"
APP_VERSION = "1.1.13"
APP_MARKER = "mbesbeacon"
DEFAULT_PORT = 19100

# 服务器相关常量（仅支持 Windows 平台）
SERVER_EXE = "bedrock_server.exe"
IS_WINDOWS = True  # 仅支持 Windows 平台

# 下载源 API（仅 Windows 版本）
LINKS_API = "https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"
CDN_TEMPLATE = "https://www.minecraft.net/bedrockdedicatedserver/bin-win/bedrock-server-{version}.zip"

# 应用数据目录（统一所有用户数据到此目录）
APP_DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "MbesBeacon")

# 子目录定义
LOGS_DIR = os.path.join(APP_DATA_DIR, "logs")
CACHE_DIR = os.path.join(APP_DATA_DIR, "cache")

# ---------------- 危险命令清单（单一事实来源） ----------------
# 服务器控制台指令层面需要二次确认的危险命令
# security.py（校验）、commands.py（命令元数据）、前端（确认弹窗）统一引用此处
DANGEROUS_SINGLE_COMMANDS = frozenset({"stop", "kill", "ban", "pardon", "deop", "restart"})
# 多词危险指令前缀（含子命令级别，如 whitelist remove <玩家>）
DANGEROUS_MULTI_COMMANDS = ("whitelist remove", "allowlist remove")
# 汇总集合
DANGEROUS_COMMANDS = DANGEROUS_SINGLE_COMMANDS | frozenset(DANGEROUS_MULTI_COMMANDS)

# 确保目录存在
for _d in (APP_DATA_DIR, LOGS_DIR, CACHE_DIR):
    with contextlib.suppress(Exception):
        os.makedirs(_d, exist_ok=True)

__all__ = [
    "APP_TITLE",
    "APP_VERSION",
    "APP_MARKER",
    "DEFAULT_PORT",
    "SERVER_EXE",
    "IS_WINDOWS",
    "LINKS_API",
    "CDN_TEMPLATE",
    "APP_DATA_DIR",
    "LOGS_DIR",
    "CACHE_DIR",
    "DANGEROUS_SINGLE_COMMANDS",
    "DANGEROUS_MULTI_COMMANDS",
    "DANGEROUS_COMMANDS",
]
