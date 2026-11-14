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
from .utils import atomic_write_json, resolve_server_dir

# 调度任务缓存状态：tasks=内存任务列表，stat=(st_mtime_ns, st_size) 文件未变化时复用
_scheduled_tasks_state = {"tasks": [], "stat": None}
_scheduler_stop = threading.Event()
_scheduler_thread = None

# 上次执行时间持久化文件：仅在内存里记录会导致进程重启后
# hourly/interval 任务立即再跑一次、daily 任务重复补执行。
SCHEDULER_STATE_FILE = ".scheduler_state.json"
# 任务被删除后，其历史记录最多保留多久（秒）
_STATE_MAX_AGE = 30 * 24 * 3600


def _state_path():
    """返回上次执行时间文件的路径（无服务器目录时返回空串）。"""
    d = resolve_server_dir()
    return os.path.join(d, SCHEDULER_STATE_FILE) if d else ""


def load_scheduler_state() -> dict:
    """加载任务上次执行时间记录（task_id -> unix 时间戳）。"""
    p = _state_path()
    if not p or not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        now = time.time()
        result = {}
        for tid, ts in data.items():
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            if ts > 0 and now - ts < _STATE_MAX_AGE:
                result[str(tid)] = ts
        return result
    except (json.JSONDecodeError, PermissionError, OSError) as e:
        safe_log_exception("scheduler.py", f"加载调度状态失败: {e}", "warning")
        return {}


def save_scheduler_state(state: dict) -> None:
    """保存任务上次执行时间（原子写）。"""
    p = _state_path()
    if not p:
        return
    try:
        atomic_write_json(p, state)
    except (PermissionError, OSError, TypeError) as e:
        safe_log_exception("scheduler.py", f"保存调度状态失败: {e}", "warning")


def _prune_state_for(tasks) -> None:
    """删除已不存在的任务的执行状态记录。

    Args:
        tasks: 当前的定时任务列表
    """
    try:
        live_ids = {str(t.get("id")).strip() for t in (tasks or []) if isinstance(t, dict) and t.get("id")}
        state = load_scheduler_state()
        if not isinstance(state, dict) or not state:
            return
        pruned = {k: v for k, v in state.items() if str(k).strip() in live_ids}
        if len(pruned) != len(state):
            save_scheduler_state(pruned)
    except Exception as e:
        safe_log_exception("scheduler.py", f"清理调度状态失败: {e}", "warning")


def load_scheduled_tasks():
    """加载定时任务配置。

    P2-3：基于文件 mtime（纳秒）+ size 缓存——调度循环每 30 秒调用一次，
    文件未变化时复用内存缓存，避免重复读盘解析。
    """
    try:
        d = resolve_server_dir()
        if not d:
            return []
        p = os.path.join(d, SCHEDULED_TASKS_FILE)
        if not os.path.exists(p):
            _scheduled_tasks_state["tasks"] = []
            _scheduled_tasks_state["stat"] = None
            return []
        try:
            stat = os.stat(p)
            stat_key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return _scheduled_tasks_state["tasks"]
        if _scheduled_tasks_state["stat"] == stat_key:
            return _scheduled_tasks_state["tasks"]
        with open(p, encoding="utf-8") as f:
            loaded = json.load(f)
        _scheduled_tasks_state["tasks"] = loaded
        _scheduled_tasks_state["stat"] = stat_key
        return loaded
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

        # 必须是真实存在的 24 小时制时间：早期只校验 \d{2}:\d{2}，
        # "99:99" 能保存成功但永远不会触发，用户会以为任务坏了。
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", task["time"]):
            return False, "时间格式必须是 HH:MM（00:00-23:59）"
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
    if task["type"] == "announce" and isinstance(task.get("message"), str):
        # 公告消息会被拼接到 say 命令，禁止换行（防止注入多条命令）
        from .security import validate_reason

        msg_ok, msg_err = validate_reason(task["message"])
        if not msg_ok:
            return False, f"公告消息无效：{msg_err}"
    return True, ""


def validate_scheduled_tasks(tasks):
    """校验所有定时任务的格式。返回 (ok, error_msg, valid_tasks)。

    另外校验 id 唯一性：重复 id 会让调度器的"上次执行时间"串到同一个键上，
    导致其中一个任务被永久跳过。
    """
    if not isinstance(tasks, list):
        return False, "任务列表必须是数组", []
    valid_tasks = []
    seen_ids = set()
    for i, task in enumerate(tasks):
        ok, err = validate_scheduled_task(task)
        if not ok:
            return False, f"第 {i + 1} 个任务: {err}", []
        tid = task["id"].strip()
        if tid in seen_ids:
            return False, f"第 {i + 1} 个任务: id '{tid}' 重复", []
        seen_ids.add(tid)
        valid_tasks.append(task)
    return True, "", valid_tasks


def save_scheduled_tasks(tasks):
    """保存定时任务配置（保存前校验格式）。"""
    # 保存前校验格式
    ok, err, valid_tasks = validate_scheduled_tasks(tasks)
    if not ok:
        return False, err
    try:
        d = resolve_server_dir()
        if not d:
            return False, "未找到服务器目录"
        p = os.path.join(d, SCHEDULED_TASKS_FILE)
        atomic_write_json(p, valid_tasks)
        # 同步清理已删除任务残留的执行状态：
        # 残留的 "上次执行时间" 会串到同 id 的新任务上，让新任务等满一整个
        # 周期才首次执行（例如删掉一个 interval=1h 的任务后重建同 id，1 小时内不动）。
        _prune_state_for(valid_tasks)
        # 同步缓存：内容已知为 valid_tasks，写后更新 stat 避免下次重复读盘
        _scheduled_tasks_state["tasks"] = valid_tasks
        try:
            stat = os.stat(p)
            _scheduled_tasks_state["stat"] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            _scheduled_tasks_state["stat"] = None
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


def _should_run_hourly(last_run, now=None):
    """判断 hourly 任务是否应执行（纯函数，便于测试）。

    hourly 语义：每小时执行一次，距上次执行 >= 1 小时即触发；
    从未执行过（last_run=0）则立即执行一次（与 interval 首次行为一致）。

    Args:
        last_run: 上次执行时间戳（秒），0 表示从未执行
        now: 当前时间戳（秒），默认取 time.time()

    Returns:
        bool: 是否应执行
    """
    if last_run <= 0:
        return True
    now = now if now is not None else time.time()
    return now - last_run >= 3600


def _scheduler_loop():
    """定时任务调度循环。

    支持补执行机制：如果程序在目标时间点未运行，启动后发现目标时间已过且今天未执行，会立即补执行。
    上次执行时间持久化到 ``.scheduler_state.json``，避免进程重启后
    hourly/interval 任务立刻重跑、daily 任务当天重复执行。
    """
    # 执行时间状态是"按服务器目录"存的（_state_path 由当前 server_dir 派生），
    # 所以必须记住这份内存快照属于哪个目录。切换/安装新服务器后若不重新加载，
    # 就会把 A 目录的时间戳写进 B 目录，导致 B 里的同名任务被"以为今天跑过了"
    # 而跳过一整个周期。
    state_dir = _state_path()
    last_check = load_scheduler_state()  # tid -> 上次执行时间戳（跨进程持久化）
    while not _scheduler_stop.is_set():
        try:
            now = time.time()
            now_hm = time.strftime("%H:%M")
            today = time.strftime("%Y-%m-%d")
            d = resolve_server_dir()
            current_state_path = _state_path()
            if current_state_path != state_dir:
                # 服务器目录变了：丢弃旧目录的状态，按新目录重新加载
                state_dir = current_state_path
                last_check = load_scheduler_state()
            if d and server_running():
                tasks = load_scheduled_tasks()
                changed = False
                for task in tasks:
                    if not task.get("enabled", True):
                        continue
                    tid = task.get("id", "")
                    if not tid:
                        continue
                    if task.get("schedule_type") == "daily":
                        target_time = task.get("time", "04:00")
                        last_run = last_check.get(tid, 0)
                        last_run_date = time.strftime("%Y-%m-%d", time.localtime(last_run)) if last_run else ""
                        # 触发条件：今天未执行且当前时间 >= 目标时间（含补执行）
                        if _should_run_daily(now_hm, target_time, today, last_run_date):
                            last_check[tid] = now
                            changed = True
                            # 目标时间已过才视为补执行（精确匹配时正常执行）
                            note = "补执行" if now_hm > target_time else None
                            _execute_scheduled_task(task, d, note=note)
                    elif task.get("schedule_type") == "hourly":
                        last_run = last_check.get(tid, 0)
                        # 每小时执行一次（距上次执行 >= 1 小时）
                        if _should_run_hourly(last_run):
                            last_check[tid] = now
                            changed = True
                            _execute_scheduled_task(task, d)
                    elif task.get("schedule_type") == "interval":
                        interval_hours = float(task.get("interval_hours", 24))
                        last_run = last_check.get(tid, 0)
                        if now - last_run >= interval_hours * 3600:
                            last_check[tid] = now
                            changed = True
                            _execute_scheduled_task(task, d)
                if changed and _state_path() == state_dir:
                    # 目录在本次迭代内又变了就别写：那份时间戳属于旧目录
                    save_scheduler_state(last_check)
        except (OSError, ValueError, AttributeError) as e:
            safe_log_exception("scheduler.py", f"调度循环异常: {e}", "warning")
        # 用 Event.wait 代替 sleep：stop_scheduler 可立即中断，
        # 退出时不再固定阻塞最长 30 秒。
        _scheduler_stop.wait(30)


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


def stop_scheduler(timeout: float = 5.0):
    """停止定时任务调度器。

    调度循环改用 ``Event.wait`` 后可在毫秒级响应停止信号。
    这里额外 join 一小段时间，确保调用返回时线程确实已退出
    （等待超时也不阻塞程序退出，因为线程是 daemon）。

    Args:
        timeout: 等待线程退出的最长时间（秒）
    """
    _scheduler_stop.set()
    thread = _scheduler_thread
    if thread and thread.is_alive():
        thread.join(timeout=timeout)
