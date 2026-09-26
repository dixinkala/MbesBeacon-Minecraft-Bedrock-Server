"""
应用日志系统：使用标准 logging 模块，设置 DEBUG/INFO/WARNING/ERROR 分级。
应用日志与服务器控制台日志分离存储，日志文件按大小和天数轮转，避免无限增长。
"""

import contextlib
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .constants import APP_DATA_DIR

# 日志级别映射
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# 全局日志器实例
_app_logger = None


def get_app_logger():
    """获取应用日志器实例（单例）。"""
    global _app_logger
    if _app_logger is not None:
        return _app_logger

    _app_logger = logging.getLogger("bedrock_server_manager")
    _app_logger.setLevel(logging.INFO)

    # 确保日志目录存在
    log_dir = os.path.join(APP_DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)

    # 应用日志文件（按大小轮转，最大 5MB，保留 5 个备份）
    log_file = os.path.join(log_dir, "app.log")
    try:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_formatter)
        _app_logger.addHandler(file_handler)
    except Exception as e:
        # 文件日志初始化失败时不影响程序运行
        print(f"[警告] 应用日志文件初始化失败: {e}", file=sys.stderr)

    # 控制台输出（仅在开发模式下，避免干扰服务器控制台）
    # 注意：在 windowed 模式下没有控制台，所以这里不添加 StreamHandler

    return _app_logger


def set_log_level(level):
    """设置日志级别。

    Args:
        level: 日志级别字符串（DEBUG/INFO/WARNING/ERROR/CRITICAL）
    """
    logger = get_app_logger()
    if level in LOG_LEVELS:
        logger.setLevel(LOG_LEVELS[level])
        logger.info("日志级别已设置为: %s", level)
    else:
        logger.warning("无效的日志级别: %s，使用默认 INFO", level)


def log_debug(msg, *args, **kwargs):
    """记录 DEBUG 级别日志。"""
    get_app_logger().debug(msg, *args, **kwargs)


def log_info(msg, *args, **kwargs):
    """记录 INFO 级别日志。"""
    get_app_logger().info(msg, *args, **kwargs)


def log_warning(msg, *args, **kwargs):
    """记录 WARNING 级别日志。"""
    get_app_logger().warning(msg, *args, **kwargs)


def log_error(msg, *args, **kwargs):
    """记录 ERROR 级别日志。"""
    get_app_logger().error(msg, *args, **kwargs)


def log_critical(msg, *args, **kwargs):
    """记录 CRITICAL 级别日志。"""
    get_app_logger().critical(msg, *args, **kwargs)


def log_exception(msg, *args, **kwargs):
    """记录异常信息（包含堆栈跟踪）。"""
    get_app_logger().exception(msg, *args, **kwargs)


def get_log_file_path():
    """获取应用日志文件路径。"""
    return os.path.join(APP_DATA_DIR, "logs", "app.log")


def get_logs_dir():
    """获取日志目录路径。"""
    return os.path.join(APP_DATA_DIR, "logs")


def read_recent_logs(lines=100):
    """读取最近的日志行。

    Args:
        lines: 要读取的行数

    Returns:
        list: 日志行列表
    """
    log_file = get_log_file_path()
    if not os.path.exists(log_file):
        return []
    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return [line.rstrip() for line in all_lines[-lines:]]
    except Exception:
        return []


def safe_log_exception(module_name, exception, level="debug"):
    """
    安全地记录异常信息，避免日志记录失败导致程序崩溃。

    这是一个通用的异常日志记录辅助函数，用于替代重复的异常处理模板：
        except Exception as e:
            try:
                get_app_logger().debug(f"xxx.py 异常: {e}")
            except Exception:
                pass

    Args:
        module_name: 模块名称（如 "utils.py"、"install.py"）
        exception: 异常对象
        level: 日志级别（debug / info / warning / error）
    """
    try:
        logger = get_app_logger()
        msg = f"{module_name} 异常: {exception}"
        if level == "debug":
            logger.debug(msg)
        elif level == "info":
            logger.info(msg)
        elif level == "warning":
            logger.warning(msg)
        elif level == "error":
            logger.error(msg)
        else:
            logger.debug(msg)
    except Exception:
        # 日志记录失败时静默处理，避免二次异常
        pass


# 初始化日志器（在模块导入时）
with contextlib.suppress(Exception):
    get_app_logger()


__all__ = [
    "get_app_logger",
    "set_log_level",
    "log_debug",
    "log_info",
    "log_warning",
    "log_error",
    "log_critical",
    "log_exception",
    "safe_log_exception",
    "get_log_file_path",
    "get_logs_dir",
    "read_recent_logs",
    "LOG_LEVELS",
]
