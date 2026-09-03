"""
定时任务调度模块：定时重启、定时备份、定时公告。
"""

import json
from .app_logger import get_app_logger
import os
import threading
import time

_scheduled_tasks = []
_scheduler_stop = threading.Event()
_scheduler_thread = None
SCHEDULED_TASKS_FILE = "scheduled_tasks.json"


def load_scheduled_tasks():
    """加载定时任务配置。"""
    global _scheduled_tasks
    try:
        from .utils import resolve_server_dir

        d = resolve_server_dir()
        if not d:
            return []
        p = os.path.join(d, SCHEDULED_TASKS_FILE)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                _scheduled_tasks = json.load(f)
        return _scheduled_tasks
    except Exception:
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
    if task["type"] == "announce":
        if "message" not in task or not isinstance(task["message"], str) or not task["message"].strip():
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
            return False, "第 %d 个任务: %s" % (i + 1, err), []
        valid_tasks.append(task)
    return True, "", valid_tasks


def save_scheduled_tasks(tasks):
    """保存定时任务配置（保存前校验格式）。"""
    global _scheduled_tasks
    # 保存前校验格式
    ok, err, valid_tasks = validate_scheduled_tasks(tasks)
    if not ok:
        return False, err
    try:
        from .utils import resolve_server_dir

        d = resolve_server_dir()
        if not d:
            return False, "未找到服务器目录"
        p = os.path.join(d, SCHEDULED_TASKS_FILE)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(valid_tasks, f, indent=2, ensure_ascii=False)
        _scheduled_tasks = valid_tasks
        return True, ""
    except Exception as e:
        return False, f"保存失败: {str(e)}"


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
            from .server import server_running
            from .utils import resolve_server_dir

            d = resolve_server_dir()
            if d and server_running():
                tasks = load_scheduled_tasks()
                for task in tasks:
                    if not task.get("enabled", True):
                        continue
                    tid = task.get("id", "")
                    task.get("type", "")
                    if task.get("schedule_type") == "daily":
                        target_time = task.get("time", "04:00")
                        last_run = last_check.get(tid, 0)
                        last_run_date = time.strftime("%Y-%m-%d", time.localtime(last_run)) if last_run else ""
                        # 触发条件1：精确匹配目标时间（正常触发）
                        # 触发条件2：已过目标时间且今天未执行（补执行机制）
                        should_run = False
                        if (
                            now_hm == target_time
                            and last_run_date != today
                            or now_hm > target_time
                            and last_run_date != today
                        ):
                            should_run = True
                        if should_run:
                            last_check[tid] = now
                            if last_run_date != today and now_hm > target_time:
                                _execute_scheduled_task(task, d, note="补执行")
                            else:
                                _execute_scheduled_task(task, d)
                    elif task.get("schedule_type") == "interval":
                        interval_hours = float(task.get("interval_hours", 24))
                        last_run = last_check.get(tid, 0)
                        if now - last_run >= interval_hours * 3600:
                            last_check[tid] = now
                            _execute_scheduled_task(task, d)
        except Exception:  # 已添加异常记录
            try:
                import sys
                get_app_logger().debug(f"scheduler.py 异常: {e}")
            except Exception:
                pass
        time.sleep(30)


def _execute_scheduled_task(task, server_dir):
    """执行定时任务。"""
    from .backup import backup_worlds_zip
    from .security import audit_log
    from .server import get_server_proc, server_running

    task_type = task.get("type", "")
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
    except Exception as e:
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
