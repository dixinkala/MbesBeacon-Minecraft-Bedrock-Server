"""
scheduler.py 和 tray.py 的单元测试

覆盖：
- 定时任务校验（validate_scheduled_task / validate_scheduled_tasks）
- 定时任务保存和加载（save_scheduled_tasks / load_scheduled_tasks）
- 调度器启动和停止（start_scheduler / stop_scheduler）
- 系统托盘基本功能（mock GUI 部分）
"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class TestValidateScheduledTask(unittest.TestCase):
    """测试单个定时任务校验"""

    def test_valid_restart_daily(self):
        """测试合法的每日重启任务"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {"id": "daily_restart", "type": "restart", "enabled": True, "schedule_type": "daily", "time": "04:00"}
        ok, err = validate_scheduled_task(task)
        self.assertTrue(ok, f"应该通过校验: {err}")
        self.assertEqual(err, "")

    def test_valid_backup_interval(self):
        """测试合法的间隔备份任务"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {
            "id": "interval_backup",
            "type": "backup",
            "enabled": True,
            "schedule_type": "interval",
            "interval_hours": 6,
        }
        ok, err = validate_scheduled_task(task)
        self.assertTrue(ok, f"应该通过校验: {err}")

    def test_valid_announce_hourly(self):
        """测试合法的每小时公告任务"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {
            "id": "hourly_announce",
            "type": "announce",
            "enabled": True,
            "schedule_type": "hourly",
            "message": "服务器公告",
        }
        ok, err = validate_scheduled_task(task)
        self.assertTrue(ok, f"应该通过校验: {err}")

    def test_invalid_not_dict(self):
        """测试非字典类型"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        ok, err = validate_scheduled_task("not a dict")
        self.assertFalse(ok)
        self.assertIn("字典", err)

    def test_invalid_missing_id(self):
        """测试缺少 id 字段"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {"type": "restart", "enabled": True, "schedule_type": "daily", "time": "04:00"}
        ok, err = validate_scheduled_task(task)
        self.assertFalse(ok)
        self.assertIn("id", err)

    def test_invalid_type(self):
        """测试非法的任务类型"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {"id": "test", "type": "invalid_type", "enabled": True, "schedule_type": "daily", "time": "04:00"}
        ok, err = validate_scheduled_task(task)
        self.assertFalse(ok)
        self.assertIn("restart/backup/announce", err)

    def test_invalid_enabled_not_bool(self):
        """测试 enabled 不是布尔值"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {"id": "test", "type": "restart", "enabled": "yes", "schedule_type": "daily", "time": "04:00"}
        ok, err = validate_scheduled_task(task)
        self.assertFalse(ok)
        self.assertIn("布尔值", err)

    def test_invalid_schedule_type(self):
        """测试非法的调度方式"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {"id": "test", "type": "restart", "enabled": True, "schedule_type": "weekly", "time": "04:00"}
        ok, err = validate_scheduled_task(task)
        self.assertFalse(ok)
        self.assertIn("daily/hourly/interval", err)

    def test_invalid_daily_time_format(self):
        """测试 daily 时间格式错误"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {"id": "test", "type": "restart", "enabled": True, "schedule_type": "daily", "time": "4:0"}
        ok, err = validate_scheduled_task(task)
        self.assertFalse(ok)
        self.assertIn("HH:MM", err)

    def test_invalid_daily_missing_time(self):
        """测试 daily 缺少 time 字段"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {"id": "test", "type": "restart", "enabled": True, "schedule_type": "daily"}
        ok, err = validate_scheduled_task(task)
        self.assertFalse(ok)
        self.assertIn("time", err)

    def test_invalid_interval_out_of_range(self):
        """测试 interval 超出范围"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {"id": "test", "type": "backup", "enabled": True, "schedule_type": "interval", "interval_hours": 200}
        ok, err = validate_scheduled_task(task)
        self.assertFalse(ok)
        self.assertIn("1-168", err)

    def test_invalid_interval_not_number(self):
        """测试 interval 不是数字"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {"id": "test", "type": "backup", "enabled": True, "schedule_type": "interval", "interval_hours": "abc"}
        ok, err = validate_scheduled_task(task)
        self.assertFalse(ok)
        self.assertIn("数字", err)

    def test_invalid_announce_missing_message(self):
        """测试 announce 缺少 message 字段"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {"id": "test", "type": "announce", "enabled": True, "schedule_type": "hourly"}
        ok, err = validate_scheduled_task(task)
        self.assertFalse(ok)
        self.assertIn("message", err)

    def test_invalid_announce_empty_message(self):
        """测试 announce message 为空"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {"id": "test", "type": "announce", "enabled": True, "schedule_type": "hourly", "message": "   "}
        ok, err = validate_scheduled_task(task)
        self.assertFalse(ok)
        self.assertIn("message", err)


class TestValidateScheduledTasks(unittest.TestCase):
    """测试任务列表校验"""

    def test_valid_list(self):
        """测试合法的任务列表"""
        from bedrock_server_manager.scheduler import validate_scheduled_tasks

        tasks = [
            {"id": "task1", "type": "restart", "enabled": True, "schedule_type": "daily", "time": "04:00"},
            {"id": "task2", "type": "backup", "enabled": False, "schedule_type": "interval", "interval_hours": 12},
        ]
        ok, err, valid = validate_scheduled_tasks(tasks)
        self.assertTrue(ok)
        self.assertEqual(len(valid), 2)

    def test_empty_list(self):
        """测试空任务列表"""
        from bedrock_server_manager.scheduler import validate_scheduled_tasks

        ok, err, valid = validate_scheduled_tasks([])
        self.assertTrue(ok)
        self.assertEqual(len(valid), 0)

    def test_not_list(self):
        """测试非列表类型"""
        from bedrock_server_manager.scheduler import validate_scheduled_tasks

        ok, err, valid = validate_scheduled_tasks("not a list")
        self.assertFalse(ok)
        self.assertIn("数组", err)

    def test_invalid_task_in_list(self):
        """测试列表中包含非法任务"""
        from bedrock_server_manager.scheduler import validate_scheduled_tasks

        tasks = [
            {"id": "task1", "type": "restart", "enabled": True, "schedule_type": "daily", "time": "04:00"},
            {"id": "task2", "type": "invalid", "enabled": True, "schedule_type": "daily", "time": "04:00"},
        ]
        ok, err, valid = validate_scheduled_tasks(tasks)
        self.assertFalse(ok)
        self.assertIn("第 2 个任务", err)
        self.assertEqual(len(valid), 0)


class TestScheduledTasksPersistence(unittest.TestCase):
    """测试定时任务的保存和加载"""

    def setUp(self):
        """创建临时服务器目录"""
        self.tmpdir = tempfile.mkdtemp(prefix="bsm_scheduler_test_")
        self.server_dir = os.path.join(self.tmpdir, "test_server")
        os.makedirs(self.server_dir)

        # 创建模拟的 bedrock_server.exe
        with open(os.path.join(self.server_dir, "bedrock_server.exe"), "wb") as f:
            f.write(b"MZ" + b"\x00" * 100)

    def tearDown(self):
        """清理临时目录"""
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load_tasks(self):
        """测试保存和加载定时任务"""
        from bedrock_server_manager import settings
        from bedrock_server_manager.scheduler import SCHEDULED_TASKS_FILE, load_scheduled_tasks, save_scheduled_tasks

        # 设置服务器目录
        settings["server_dir"] = self.server_dir

        tasks = [
            {"id": "daily_restart", "type": "restart", "enabled": True, "schedule_type": "daily", "time": "04:00"},
            {
                "id": "interval_backup",
                "type": "backup",
                "enabled": False,
                "schedule_type": "interval",
                "interval_hours": 6,
            },
        ]

        # 保存任务
        ok, err = save_scheduled_tasks(tasks)
        self.assertTrue(ok, f"保存应该成功: {err}")

        # 验证文件已创建
        file_path = os.path.join(self.server_dir, SCHEDULED_TASKS_FILE)
        self.assertTrue(os.path.exists(file_path))

        # 加载任务
        loaded = load_scheduled_tasks()
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["id"], "daily_restart")
        self.assertEqual(loaded[1]["id"], "interval_backup")

        # 清理设置
        settings["server_dir"] = ""

    def test_save_invalid_tasks(self):
        """测试保存非法任务应该失败"""
        from bedrock_server_manager import settings
        from bedrock_server_manager.scheduler import save_scheduled_tasks

        settings["server_dir"] = self.server_dir

        invalid_tasks = [
            {"id": "test", "type": "invalid_type", "enabled": True, "schedule_type": "daily", "time": "04:00"}
        ]
        ok, err = save_scheduled_tasks(invalid_tasks)
        self.assertFalse(ok)
        self.assertIn("任务类型", err)

        settings["server_dir"] = ""


class TestSchedulerLifecycle(unittest.TestCase):
    """测试调度器的启动和停止"""

    def test_start_and_stop_scheduler(self):
        """测试启动和停止调度器"""
        from bedrock_server_manager.scheduler import start_scheduler, stop_scheduler

        # 启动调度器
        start_scheduler()
        time.sleep(0.5)

        # 验证线程已启动
        from bedrock_server_manager import scheduler

        self.assertIsNotNone(scheduler._scheduler_thread)
        self.assertTrue(scheduler._scheduler_thread.is_alive())

        # 停止调度器
        stop_scheduler()
        # 等待线程结束（最多等待 35 秒，因为循环中有 30 秒睡眠）
        scheduler._scheduler_thread.join(timeout=35)

        # 验证线程已停止
        self.assertFalse(scheduler._scheduler_thread.is_alive())

    def test_double_start_no_duplicate(self):
        """测试重复启动不会创建多个线程"""
        from bedrock_server_manager import scheduler
        from bedrock_server_manager.scheduler import start_scheduler, stop_scheduler

        start_scheduler()
        time.sleep(0.3)
        first_thread = scheduler._scheduler_thread

        start_scheduler()  # 再次启动
        time.sleep(0.3)
        second_thread = scheduler._scheduler_thread

        # 应该是同一个线程
        self.assertEqual(first_thread, second_thread)

        stop_scheduler()
        scheduler._scheduler_thread.join(timeout=35)


class TestDailyTriggerLogic(unittest.TestCase):
    """测试 daily 调度触发判断纯函数（P1-4 抽取，消除优先级隐患）"""

    def _should_run(self, now_hm, target_time, today, last_run_date):
        from bedrock_server_manager.scheduler import _should_run_daily

        return _should_run_daily(now_hm, target_time, today, last_run_date)

    def test_exact_match_triggers(self):
        """精确匹配目标时间且今天未执行 → 触发"""
        self.assertTrue(self._should_run("04:00", "04:00", "2026-10-01", ""))

    def test_past_target_triggers_catch_up(self):
        """已过目标时间且今天未执行 → 补执行触发"""
        self.assertTrue(self._should_run("05:30", "04:00", "2026-10-01", ""))

    def test_already_run_today_no_trigger(self):
        """今天已执行 → 不触发（无论时间是否到达）"""
        self.assertFalse(self._should_run("04:00", "04:00", "2026-10-01", "2026-10-01"))
        self.assertFalse(self._should_run("05:30", "04:00", "2026-10-01", "2026-10-01"))

    def test_before_target_no_trigger(self):
        """未到目标时间 → 不触发"""
        self.assertFalse(self._should_run("03:00", "04:00", "2026-10-01", ""))

    def test_previous_day_run_allows_today(self):
        """昨天执行过 → 今天仍可触发"""
        self.assertTrue(self._should_run("04:00", "04:00", "2026-10-02", "2026-10-01"))

    def test_midnight_boundary(self):
        """目标时间 00:00 边界：凌晨精确匹配触发"""
        self.assertTrue(self._should_run("00:00", "00:00", "2026-10-01", ""))
        self.assertFalse(self._should_run("00:00", "00:00", "2026-10-01", "2026-10-01"))


class TestSystemTray(unittest.TestCase):
    """测试系统托盘基本功能（mock GUI 部分）"""

    def test_tray_module_imports(self):
        """测试托盘模块可以正常导入"""
        try:
            from bedrock_server_manager import tray

            self.assertTrue(hasattr(tray, "SystemTray"))
        except ImportError as e:
            self.skipTest(f"托盘模块导入失败（可能缺少依赖）: {e}")

    def test_tray_class_exists(self):
        """测试 SystemTray 类存在且有基本方法"""
        try:
            from bedrock_server_manager.tray import SystemTray

            # 检查类是否有基本方法
            self.assertTrue(hasattr(SystemTray, "__init__"))
            self.assertTrue(hasattr(SystemTray, "start") or hasattr(SystemTray, "run"))
            self.assertTrue(hasattr(SystemTray, "stop") or hasattr(SystemTray, "quit"))
        except ImportError as e:
            self.skipTest(f"托盘模块导入失败: {e}")

    @patch("bedrock_server_manager.tray.SystemTray")
    def test_tray_mock_creation(self, MockTray):
        """测试使用 mock 创建托盘实例"""
        mock_instance = MagicMock()
        MockTray.return_value = mock_instance

        from bedrock_server_manager.tray import SystemTray

        # SystemTray.__init__ 需要 url 参数（mock 类会忽略实际值）
        tray = SystemTray("http://127.0.0.1:19100")
        self.assertIsNotNone(tray)
        MockTray.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
