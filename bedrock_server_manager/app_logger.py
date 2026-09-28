"""
应用日志系统：使用标准 logging 模块，设置 DEBUG/INFO/WARNING/ERROR 分级。
应用日志与服务器控制台日志分离存储，日志文件按大小轮转，避免无限增长。

P1-3 优化：路径统一引用 constants.LOGS_DIR；单例创建加锁；
read_recent_logs 尾部高效读取（不整文件读入）；提供内部重置函数便于测试隔离。
"""

import contextlib
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

from .constants import LOGS_DIR

# 日志级别映射
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# 日志目录与文件（统一引用 constants.LOGS_DIR，测试可替换为临时目录）
log_dir = LOGS_DIR
log_file = os.path.join(LOGS_DIR, "app.log")

# 全局日志器实例与创建锁
_app_logger = None
_logger_lock = threading.Lock()


def _reset_logger():
    """重置全局日志器（仅供测试使用，不对外导出）。"""
    global _app_logger
    with _logger_lock:
        _app_logger = None


def get_app_logger():
    """获取应用日志器实例（单例，线程安全）。"""
    global _app_logger
    if _app_logger is not None:
        return _app_logger

    with _logger_lock:
        if _app_logger is not None:
            return _app_logger

        logger = logging.getLogger("bedrock_server_manager")
        logger.setLevel(logging.INFO)
        # logging.getLogger 按名缓存同一实例，重建时清空旧 handler 避免累积
        logger.handlers.clear()

        # 确保日志目录存在
        os.makedirs(log_dir, exist_ok=True)

        # 应用日志文件（按大小轮转，最大 5MB，保留 5 个备份）
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
            logger.addHandler(file_handler)
        except Exception as e:
            # 文件日志初始化失败时不影响程序运行
            print(f"[警告] 应用日志文件初始化失败: {e}", file=sys.stderr)

        # 控制台输出（仅在开发模式下，避免干扰服务器控制台）
        # 注意：在 windowed 模式下没有控制台，所以这里不添加 StreamHandler

        _app_logger = logger
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
    return log_file


def get_logs_dir():
    """获取日志目录路径。"""
    return log_dir


def read_recent_logs(lines=100):
    """读取最近的日志行（从文件尾部高效读取，不整文件读入）。

    Args:
        lines: 要读取的行数

    Returns:
        list: 日志行列表
    """
    if not os.path.exists(log_file):
        return []
    try:
        with open(log_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            # 从尾部向前读取，最多覆盖预估的字节数（每行平均 120 字节 + 余量）
            read_bytes = min(file_size, max(lines * 120 + 4096, 8192))
            f.seek(max(0, file_size - read_bytes))
            tail_data = f.read()
        lines_list = tail_data.decode("utf-8", errors="replace").splitlines()
        return [line for line in lines_list[-lines:] if line.strip()]
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
        level_name = level.upper() if isinstance(level, str) else "DEBUG"
        log_method = getattr(logger, level_name.lower(), logger.debug)
        log_method(msg)
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
