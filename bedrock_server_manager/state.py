"""
核心状态模块：集中管理所有模块级全局变量，避免循环导入。
其他模块通过 `from .state import *` 或具体导入访问全局状态。
常量已移至 constants.py，此处保留向后兼容导入。

【P1-1 迁移说明（批 2）】
- API_TOKEN / app_start_time / settings 已由 AppContext 自行生成与管理，
  本模块通过模块级 __getattr__ 惰性代理到 AppContext.instance()，
  保证 reset 后仍与当前实例保持一致。
- console / install_state 由包 __init__ 创建后同步到此处（镜像）。
- server_proc / httpd / server_lock / mutex_handle 仅为向后兼容保留。
新代码一律使用 AppContext.instance()。
"""

import os
import re
import threading

from .app_logger import safe_log_exception

# 从 constants.py 导入常量（保持向后兼容）
from .constants import (
    APP_DATA_DIR,
    CACHE_DIR,
    LOGS_DIR,
)

# 单实例互斥量句柄，退出时释放
mutex_handle = None

# ---------------- 全局单例状态 ----------------
# 注意：以下对象在各模块中创建后赋值到此处，供全局访问
# console / install_state 由包 __init__ 创建后同步到 AppContext 与 state（镜像）
# server_proc / httpd / server_lock / mutex_handle 仅向后兼容保留（从未写入真实值）
console = None  # ConsoleBuffer 实例（在 console.py 中创建）
install_state = None  # InstallState 实例（在 install.py 中创建）
server_proc = None  # ServerProcess 单例（已由 AppContext 管理，此处兼容保留）
server_lock = threading.Lock()  # 服务器进程锁（已由 AppContext 管理，此处兼容保留）
httpd = None  # HTTP 服务器实例（已由 AppContext 管理，此处兼容保留）


def __getattr__(name: str):
    """P1-1 惰性代理：API_TOKEN / app_start_time / settings 转发到 AppContext。

    使用模块级 __getattr__（PEP 562）而非模块加载时绑定，确保
    AppContext.reset() 之后旧引用仍指向当前实例的状态。
    """
    if name in ("API_TOKEN", "app_start_time", "settings"):
        from .app_context import AppContext  # 函数内延迟导入，避免循环依赖

        ctx = AppContext.instance()
        if name == "API_TOKEN":
            return ctx.api_token
        if name == "app_start_time":
            return ctx.app_start_time
        return ctx.settings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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


# ---------------- 便捷函数 ----------------
# 以下便捷函数直接操作 AppContext 的全局状态，保持向后兼容。
# 新代码请使用 AppContext.instance() 访问全局状态。


def _ctx():
    """获取全局 AppContext 实例（函数内延迟导入，避免循环依赖）。"""
    from .app_context import AppContext

    return AppContext.instance()


def log(text: str) -> None:
    """向控制台追加日志（便捷函数）。

    Args:
        text: 日志文本
    """
    if console is not None:
        console.append(text)


def log_info(text: str) -> None:
    """向控制台追加信息级别日志（便捷函数）。

    Args:
        text: 日志文本
    """
    log(f"[信息] {text}")


def log_warning(text: str) -> None:
    """向控制台追加警告级别日志（便捷函数）。

    Args:
        text: 日志文本
    """
    log(f"[警告] {text}")


def log_error(text: str) -> None:
    """向控制台追加错误级别日志（便捷函数）。

    Args:
        text: 日志文本
    """
    log(f"[错误] {text}")


def get_setting(key: str, default=None):
    """获取设置值（便捷函数）。

    Args:
        key: 设置键名
        default: 默认值

    Returns:
        设置值，如果不存在则返回默认值
    """
    return _ctx().settings.get(key, default)


def set_setting(key: str, value) -> None:
    """设置设置值（便捷函数）。

    Args:
        key: 设置键名
        value: 设置值
    """
    _ctx().settings[key] = value


def get_state_dict() -> dict:
    """获取状态字典（用于健康检查和调试）。

    Returns:
        状态字典
    """
    ctx = _ctx()
    return {
        "api_token_set": bool(ctx.api_token),
        "uptime_seconds": round(ctx.uptime_seconds, 2),
        "server_dir": ctx.settings.get("server_dir", ""),
        "installed": bool(ctx.settings.get("installed_version", "")),
    }
