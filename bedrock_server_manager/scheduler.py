"""
定时任务调度模块：定时重启、定时备份、定时公告。
"""

import contextlib
import json
import os
import threading
import time

from .app_logger import get_app_logger, safe_log_exception
from .backup import backup_worlds_zip
from .config import SCHEDULED_TASKS_FILE
from .security import audit_log
from .server import get_server_proc, server_running
from .utils import resolve_server_dir

_scheduled_tasks = []
_scheduled_tasks_stat = None  # (st_mtime_ns, st_size)：文件未变化时复用缓存
_scheduler_stop = threading.Event()
_scheduler_thread = None


def load_scheduled_tasks():
    """加载定时任务配置。

    P2-3：基于文件 mtime（纳秒）+ size 缓存——调度循环每 30 秒调用一次，
    文件未变化时复用内存缓存，避免重复读盘解析。
    """
    global _scheduled_tasks, _scheduled_tasks_stat
    try:
        d = resolve_server_dir()
        if not d:
            return []
        p = os.path.join(d, SCHEDULED_TASKS_FILE)
        if not os.path.exists(p):
            _scheduled_tasks = []
            _scheduled_tasks_stat = None
            return _scheduled_tasks
        try:
            stat = os.stat(p)
            stat_key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return _scheduled_tasks
        if _scheduled_tasks_stat == stat_key:
            return _scheduled_tasks
        with open(p, encoding="utf-8") as f:
            loaded = json.load(f)
        _scheduled_tasks = loaded
        _scheduled_tasks_stat = stat_key
        return _scheduled_tasks
    except (json.JSONDecodeError, PermissionError, OSError) as e:
        safe_log_exception("scheduler.py", f"加载定时任务失败: {e}", "warning")
        return []
    except Exception as e:
        safe_log_exception("scheduler.py", f"加载定时任务时发生未知异常: {e}", "error")
        return []


# 合法的任务类型和调度方式
_VALID_TASK_TYPES = {"restart", "backup", "announce"}
_VALID_SCHEDULE_TYPES = {"daily", "hourly", "interval"}


def validate_scheduled_task(task):
    """校验单个定时任务的格式。返回 (ok, error_msg)。"""
    if not isinstance(task, dict):
        return False, "任务必须是字典格式"
    # 校验必填字段
    if "id" not in task or not isinstance(task["id"], str) or not task["id"].strip():
        return False, "任务缺少有效的 id 字段"
    if "type" not in task or task["type"] not in _VALID_TASK_TYPES:
        return False, "任务类型必须是 restart/backup/announce 之一"
    if "enabled" not in task or not isinstance(task["enabled"], bool):
        return False, "任务 enabled 字段必须是布尔值"
    if "schedule_type" not in task or task["schedule_type"] not in _VALID_SCHEDULE_TYPES:
        return False, "调度方式必须是 daily/hourly/interval 之一"
    # 校验时间格式
    if task["schedule_type"] == "daily":
        if "time" not in task or not isinstance(task["time"], str):
            return False, "daily 调度需要 time 字段（如 04:00）"
        import re

        if not re.match(r"^\d{2}:\d{2}$", task["time"]):
            return False, "时间格式必须是 HH:MM"
    elif task["schedule_type"] == "interval":
        if "interval_hours" not in task:
            return False, "interval 调度需要 interval_hours 字段"
        try:
            interval = float(task["interval_hours"])
            if interval < 1 or interval > 168:  # 最大7天（168小时）
                return False, "interval_hours 必须在 1-168 之间"
        except (ValueError, TypeError):
            return False, "interval_hours 必须是数字"
    # 校验公告消息
    if task["type"] == "announce" and (
        "message" not in task or not isinstance(task["message"], str) or not task["message"].strip()
    ):
        return False, "announce 类型需要 message 字段"
    return True, ""


def validate_scheduled_tasks(tasks):
    """校验所有定时任务的格式。返回 (ok, error_msg, valid_tasks)。"""
    if not isinstance(tasks, list):
        return False, "任务列表必须是数组", []
    valid_tasks = []
    for i, task in enumerate(tasks):
        ok, err = validate_scheduled_task(task)
        if not ok:
            return False, f"第 {i + 1} 个任务: {err}", []
        valid_tasks.append(task)
    return True, "", valid_tasks


def save_scheduled_tasks(tasks):
    """保存定时任务配置（保存前校验格式）。"""
    global _scheduled_tasks, _scheduled_tasks_stat
    # 保存前校验格式
    ok, err, valid_tasks = validate_scheduled_tasks(tasks)
    if not ok:
        return False, err
    try:
        d = resolve_server_dir()
        if not d:
            return False, "未找到服务器目录"
        p = os.path.join(d, SCHEDULED_TASKS_FILE)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(valid_tasks, f, indent=2, ensure_ascii=False)
        # 同步缓存：内容已知为 valid_tasks，写后更新 stat 避免下次重复读盘
        _scheduled_tasks = valid_tasks
        try:
            stat = os.stat(p)
            _scheduled_tasks_stat = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            _scheduled_tasks_stat = None
        return True, ""
    except (PermissionError, OSError, TypeError) as e:
        safe_log_exception("scheduler.py", f"保存定时任务失败: {e}", "warning")
        return False, f"保存失败（文件操作错误）: {str(e)}"
    except Exception as e:
        safe_log_exception("scheduler.py", f"保存定时任务时发生未知异常: {e}", "error")
        return False, f"保存失败: {str(e)}"


def _should_run_daily(now_hm, target_time, today, last_run_date):
    """判断 daily 任务是否应执行（纯函数，便于测试）。

    Args:
        now_hm: 当前时间（HH:MM 字符串）
        target_time: 任务目标时间（HH:MM 字符串）
        today: 当前日期（YYYY-MM-DD 字符串）
        last_run_date: 上次执行日期（YYYY-MM-DD 字符串，未执行为空）

    Returns:
        bool: 是否应执行（今天未执行且当前时间 >= 目标时间）
    """
    # 今天已执行过则不再触发
    if last_run_date == today:
        return False
    # 正常触发（now_hm == target_time）或补执行（now_hm > target_time）
    return now_hm >= target_time


def _scheduler_loop():
    """定时任务调度循环。

    支持补执行机制：如果程序在目标时间点未运行，启动后发现目标时间已过且今天未执行，会立即补执行。
    """
    last_check = {}  # tid -> 上次执行时间戳
    while not _scheduler_stop.is_set():
        try:
            now = time.time()
            now_hm = time.strftime("%H:%M")
            today = time.strftime("%Y-%m-%d")
            d = resolve_server_dir()
            if d and server_running():
                tasks = load_scheduled_tasks()
                for task in tasks:
                    if not task.get("enabled", True):
                        continue
                    tid = task.get("id", "")
                    if task.get("schedule_type") == "daily":
                        target_time = task.get("time", "04:00")
                        last_run = last_check.get(tid, 0)
                        last_run_date = time.strftime("%Y-%m-%d", time.localtime(last_run)) if last_run else ""
                        # 触发条件：今天未执行且当前时间 >= 目标时间（含补执行）
                        if _should_run_daily(now_hm, target_time, today, last_run_date):
                            last_check[tid] = now
                            # 目标时间已过才视为补执行（精确匹配时正常执行）
                            note = "补执行" if now_hm > target_time else None
                            _execute_scheduled_task(task, d, note=note)
                    elif task.get("schedule_type") == "interval":
                        interval_hours = float(task.get("interval_hours", 24))
                        last_run = last_check.get(tid, 0)
                        if now - last_run >= interval_hours * 3600:
                            last_check[tid] = now
                            _execute_scheduled_task(task, d)
        except (OSError, ValueError, AttributeError) as e:
            safe_log_exception("scheduler.py", f"调度循环异常: {e}", "warning")
        time.sleep(30)


def _execute_scheduled_task(task, server_dir, note=None):
    """执行定时任务。

    Args:
        task: 任务配置字典
        server_dir: 服务器目录路径
        note: 可选的执行备注（如"补执行"），用于日志记录
    """
    task_type = task.get("type", "")
    # 如果有备注，记录到日志（日志失败不影响任务执行）
    if note:
        with contextlib.suppress(Exception):
            get_app_logger().info(f"定时任务[{note}]: {task_type} - {server_dir}")
    try:
        if task_type == "restart":
            if server_running():
                p = get_server_proc()
                p.stop()
                time.sleep(3)
                p.server_dir = server_dir
                p.start()
                audit_log("SCHEDULED_RESTART", "", server_dir)
        elif task_type == "backup":
            backup_worlds_zip(server_dir, max_backups=task.get("max_backups", 10))
        elif task_type == "announce":
            msg = task.get("message", "服务器公告")
            if server_running():
                get_server_proc().send(f"say {msg}")
                audit_log("SCHEDULED_ANNOUNCE", msg, server_dir)
    except (PermissionError, OSError, RuntimeError) as e:
        safe_log_exception("scheduler.py", f"执行定时任务失败: {e}", "warning")
        audit_log("SCHEDULED_TASK_ERROR", f"{task_type}: {str(e)}", server_dir)
    except Exception as e:
        safe_log_exception("scheduler.py", f"执行定时任务时发生未知异常: {e}", "error")
        audit_log("SCHEDULED_TASK_ERROR", f"{task_type}: {str(e)}", server_dir)


def start_scheduler():
    """启动定时任务调度器。"""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()


def stop_scheduler():
    """停止定时任务调度器。"""
    _scheduler_stop.set()
