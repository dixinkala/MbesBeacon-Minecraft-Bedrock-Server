"""
核心状态模块：集中管理所有模块级全局变量，避免循环导入。
其他模块通过 `from .state import *` 或具体导入访问全局状态。
常量已移至 constants.py，此处保留向后兼容导入。
"""

import os
import re
import secrets as _secrets
import threading
import time

from .app_logger import safe_log_exception

# 从 constants.py 导入常量（保持向后兼容）
from .constants import (
    APP_DATA_DIR,
    CACHE_DIR,
    DEFAULT_PORT,
    LOGS_DIR,
)

# API 安全 token：启动时随机生成，用于 CSRF 防护
API_TOKEN = _secrets.token_hex(16)

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
THEME_FILE = os.path.join(APP_DATA_DIR, "theme.json")
DEFAULT_THEME = {"preset": "minecraft_dark", "accent": "#3fb950", "mode": "dark"}


def migrate_legacy_data():
    """
    将旧版本分散在用户主目录和旧 AppData 目录的数据迁移到统一的 APP_DATA_DIR。
    仅在首次启动时执行一次，迁移完成后删除旧文件。
    """
    home = os.path.expanduser("~")
    migrated = []

    # 旧路径 → 新路径 的映射
    legacy_files = [
        # 设置文件
        (os.path.join(home, ".bedrock_server_manager_settings.json"), os.path.join(APP_DATA_DIR, "settings.json")),
        # 主题文件
        (os.path.join(home, ".bedrock_server_manager_theme.json"), os.path.join(APP_DATA_DIR, "theme.json")),
        # 崩溃日志
        (os.path.join(home, ".bedrock_server_manager_crash.log"), os.path.join(LOGS_DIR, "crash.log")),
        # 哈希缓存
        (os.path.join(home, ".bedrock_server_hashes.json"), os.path.join(CACHE_DIR, "hashes.json")),
        # 托盘日志
        (os.path.join(home, ".bedrock_server_manager", "tray.log"), os.path.join(LOGS_DIR, "tray.log")),
    ]

    # 旧 AppData 目录的应用日志
    old_appdata = os.path.join(os.environ.get("APPDATA", home), "BedrockServerManager", "logs", "app.log")
    new_applog = os.path.join(LOGS_DIR, "app.log")
    legacy_files.append((old_appdata, new_applog))

    for old_path, new_path in legacy_files:
        try:
            if os.path.isfile(old_path) and not os.path.isfile(new_path):
                os.makedirs(os.path.dirname(new_path), exist_ok=True)
                import shutil

                shutil.copy2(old_path, new_path)
                migrated.append(os.path.basename(old_path))
                # 迁移成功后删除旧文件
                try:
                    os.remove(old_path)
                except (PermissionError, OSError) as e:
                    safe_log_exception("state.py", f"删除旧文件失败: {e}", "debug")
        except (PermissionError, OSError) as e:
            safe_log_exception("state.py", f"迁移文件失败: {e}", "debug")

    # 清理旧的空目录
    old_dirs = [
        os.path.join(home, ".bedrock_server_manager"),
        os.path.join(os.environ.get("APPDATA", home), "BedrockServerManager"),
    ]
    for old_dir in old_dirs:
        try:
            if os.path.isdir(old_dir) and not os.listdir(old_dir):
                os.rmdir(old_dir)
        except (PermissionError, OSError) as e:
            safe_log_exception("state.py", f"删除空目录失败: {e}", "debug")

    return migrated


# ---------------- AppContext 全局单例集成 ----------------
# 新代码应通过 _ctx() 访问全局状态
# 此处保持向后兼容，将模块级全局变量同步到 AppContext


def _ctx():
    """获取 AppContext 单例（延迟导入，避免与 app_context 循环依赖）。"""
    from .app_context import AppContext

    return AppContext.instance()


def sync_to_app_context():
    """将模块级全局变量同步到 AppContext 单例。
    在应用启动时调用一次，确保 AppContext 与现有全局变量保持一致。
    """
    ctx = _ctx()
    ctx.api_token = API_TOKEN
    ctx.console = console
    ctx.install_state = install_state
    ctx.settings = settings
    ctx.server_proc = server_proc
    ctx.httpd = httpd
    ctx.port = DEFAULT_PORT
    ctx.mark_initialized()
    return ctx


def sync_from_app_context():
    """将 AppContext 单例的状态同步回模块级全局变量。
    在修改 AppContext 后调用，确保向后兼容。
    """
    ctx = _ctx()
    global console, install_state, settings, server_proc, httpd, API_TOKEN, mutex_handle
    API_TOKEN = ctx.api_token
    console = ctx.console
    install_state = ctx.install_state
    settings = ctx.settings
    server_proc = ctx.server_proc
    httpd = ctx.httpd
    mutex_handle = ctx.mutex_handle


def get_app_context():
    """获取 AppContext 单例（便捷函数）。

    Returns:
        AppContext: 全局单例实例
    """
    return _ctx()


def log(text: str) -> None:
    """向控制台追加日志（便捷函数）。

    Args:
        text: 日志文本
    """
    _ctx().log(text)


def log_info(text: str) -> None:
    """向控制台追加信息级别日志（便捷函数）。

    Args:
        text: 日志文本
    """
    _ctx().log_info(text)


def log_warning(text: str) -> None:
    """向控制台追加警告级别日志（便捷函数）。

    Args:
        text: 日志文本
    """
    _ctx().log_warning(text)


def log_error(text: str) -> None:
    """向控制台追加错误级别日志（便捷函数）。

    Args:
        text: 日志文本
    """
    _ctx().log_error(text)


def get_setting(key: str, default=None):
    """获取设置值（便捷函数）。

    Args:
        key: 设置键名
        default: 默认值

    Returns:
        设置值，如果不存在则返回默认值
    """
    return _ctx().get_setting(key, default)


def set_setting(key: str, value) -> None:
    """设置设置值（便捷函数）。

    Args:
        key: 设置键名
        value: 设置值
    """
    _ctx().set_setting(key, value)
    # 同步回模块级全局变量
    global settings
    settings = _ctx().settings


def get_state_dict() -> dict:
    """获取状态字典（用于健康检查和调试）。

    Returns:
        状态字典
    """
    return _ctx().to_dict()
