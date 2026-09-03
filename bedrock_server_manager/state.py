"""
核心状态模块：集中管理所有模块级全局变量，避免循环导入。
其他模块通过 `from .state import *` 或具体导入访问全局状态。
"""

import os
import re
import secrets as _secrets
import threading
import time

# ---------------- 应用常量 ----------------
APP_TITLE = "MbesBeacon"
APP_VERSION = "2.0.0"
APP_MARKER = "mbesbeacon"
DEFAULT_PORT = 19100

# API 安全 token：启动时随机生成，用于 CSRF 防护
API_TOKEN = _secrets.token_hex(16)

# 服务器相关常量（仅支持 Windows 平台）
SERVER_EXE = "bedrock_server.exe"
IS_WINDOWS = True  # 仅支持 Windows 平台

# 下载源 API（仅 Windows 版本）
LINKS_API = "https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"
CDN_TEMPLATE = "https://www.minecraft.net/bedrockdedicatedserver/bin-win/bedrock-server-{version}.zip"  # 仅 Windows 版本

# 应用数据目录
APP_DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "BedrockServerManager")

# 应用启动时间戳，用于健康检查
app_start_time = time.time()

# 单实例互斥量句柄，退出时释放
mutex_handle = None

# ---------------- 全局单例状态 ----------------
# 注意：以下对象在各模块中创建后赋值到此处，供全局访问
console = None  # ConsoleBuffer 实例（在 console.py 中创建）
install_state = None  # InstallState 实例（在 install.py 中创建）
settings = {  # 全局设置字典
    "server_dir": "",
    "ignore_ssl": False,
    "installed_version": "",
    "server_dir_history": [],
}
server_proc = None  # ServerProcess 单例（在 server.py 中管理）
server_lock = threading.Lock()  # 服务器进程锁
httpd = None  # HTTP 服务器实例（在 main.py 中创建）

# 玩家名校验正则

PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_.]+$")

# 主题配置
THEME_FILE = os.path.join(os.path.expanduser("~"), ".bedrock_server_manager_theme.json")
DEFAULT_THEME = {"preset": "minecraft_dark", "accent": "#3fb950", "mode": "dark"}
