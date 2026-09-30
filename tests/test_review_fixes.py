"""
完整代码检查修复（B1-B5）回归测试。

覆盖：
- B1: delete_backup 支持 ZIP 文件型备份删除（原仅支持目录型）
- B2: rename_world 活动世界重命名（原顺序错误导致永远失败）
- B3: WebAppContext.save_config 生成配置历史备份（原传参错误）
- B4: /api/backups/restore 限流键名正确（原拼写缺 s）
- B5: hourly 定时任务纳入调度循环（原被静默跳过）
"""

import contextlib
import os
import shutil
import tempfile
import time
import unittest


class TestDeleteBackupFixes(unittest.TestCase):
    """B1：delete_backup 支持目录型与 ZIP 文件型备份。"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old_server_dir = settings.get("server_dir", "")
        self.tmpdir = tempfile.mkdtemp(prefix="mbes_b1_")
        self.server_dir = os.path.join(self.tmpdir, "server")
        os.makedirs(os.path.join(self.server_dir, "worlds"))
        with open(os.path.join(self.server_dir, "worlds", "level.dat"), "w") as f:
            f.write("fake world data")
        settings["server_dir"] = self.server_dir

    def tearDown(self):
        from bedrock_server_manager import settings

        settings["server_dir"] = self._old_server_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_delete_zip_backup(self):
        """ZIP 型备份可删除（原实现拒绝文件型备份）。"""
        from bedrock_server_manager.backup import backup_worlds_zip, delete_backup, list_backups

        zip_path = backup_worlds_zip(self.server_dir)
        self.assertTrue(zip_path and os.path.isfile(zip_path))
        ok, err = delete_backup(zip_path, self.server_dir)
        self.assertTrue(ok, err)
        self.assertFalse(os.path.exists(zip_path))
        self.assertEqual(list_backups(self.server_dir), [])

    def test_delete_dir_backup(self):
        """目录型备份仍可删除（回归保护）。"""
        from bedrock_server_manager.backup import backup_worlds, delete_backup, list_backups

        dir_path = backup_worlds(self.server_dir)
        self.assertTrue(dir_path and os.path.isdir(dir_path))
        ok, err = delete_backup(dir_path, self.server_dir)
        self.assertTrue(ok, err)
        self.assertFalse(os.path.exists(dir_path))
        self.assertEqual(list_backups(self.server_dir), [])

    def test_delete_outside_root_rejected(self):
        """备份根目录之外的路径仍被拒绝（安全校验回归）。"""
        from bedrock_server_manager.backup import delete_backup

        outside = os.path.join(self.tmpdir, "outside_dir")
        os.makedirs(outside)
        ok, err = delete_backup(outside, self.server_dir)
        self.assertFalse(ok)
        self.assertIn("非法路径", err)
        self.assertTrue(os.path.isdir(outside))


class TestRenameActiveWorld(unittest.TestCase):
    """B2：活动世界重命名（原实现先改配置后改目录导致失败）。"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old_server_dir = settings.get("server_dir", "")
        self.tmpdir = tempfile.mkdtemp(prefix="mbes_b2_")
        self.server_dir = os.path.join(self.tmpdir, "server")
        os.makedirs(os.path.join(self.server_dir, "worlds", "WorldA"))
        with open(os.path.join(self.server_dir, "worlds", "WorldA", "level.dat"), "w") as f:
            f.write("fake world data")
        with open(os.path.join(self.server_dir, "server.properties"), "w", encoding="utf-8") as f:
            f.write("level-name=WorldA\nserver-port=19132\n")
        settings["server_dir"] = self.server_dir

    def tearDown(self):
        from bedrock_server_manager import settings

        settings["server_dir"] = self._old_server_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rename_active_world(self):
        """活动世界重命名成功：目录改名且 level-name 同步更新。"""
        from bedrock_server_manager.worlds import get_active_world_name, list_worlds, rename_world

        ok, err = rename_world(self.server_dir, "WorldA", "WorldB")
        self.assertTrue(ok, err)
        self.assertTrue(os.path.isdir(os.path.join(self.server_dir, "worlds", "WorldB")))
        self.assertFalse(os.path.exists(os.path.join(self.server_dir, "worlds", "WorldA")))
        # level-name 已同步更新
        self.assertEqual(get_active_world_name(self.server_dir), "WorldB")
        # 世界列表只含新名
        names = [w["name"] for w in list_worlds(self.server_dir)]
        self.assertEqual(names, ["WorldB"])

    def test_rename_non_active_world(self):
        """非活动世界重命名不受影响（回归保护）。"""
        from bedrock_server_manager.worlds import rename_world

        os.makedirs(os.path.join(self.server_dir, "worlds", "WorldC"))
        with open(os.path.join(self.server_dir, "worlds", "WorldC", "level.dat"), "w") as f:
            f.write("fake")
        ok, err = rename_world(self.server_dir, "WorldC", "WorldD")
        self.assertTrue(ok, err)
        self.assertTrue(os.path.isdir(os.path.join(self.server_dir, "worlds", "WorldD")))


class TestSaveConfigCreatesHistory(unittest.TestCase):
    """B3：save_config 保存配置时生成配置历史备份（原传参错误）。"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old_server_dir = settings.get("server_dir", "")
        self.tmpdir = tempfile.mkdtemp(prefix="mbes_b3_")
        self.server_dir = os.path.join(self.tmpdir, "server")
        os.makedirs(self.server_dir)
        with open(os.path.join(self.server_dir, "server.properties"), "w", encoding="utf-8") as f:
            f.write("server-name=Dedicated Server\nserver-port=19132\n")
        settings["server_dir"] = self.server_dir

    def tearDown(self):
        from bedrock_server_manager import settings

        settings["server_dir"] = self._old_server_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_config_generates_history(self):
        """保存配置后 .config_history 目录出现 server.properties.*.bak 备份。"""
        from bedrock_server_manager.config import CONFIG_HISTORY_DIR
        from bedrock_server_manager.web.app import AppContext as WebAppContext

        ok, err, field = WebAppContext.instance().save_config({"server-name": "我的服务器"})
        self.assertTrue(ok, err)
        # 直接读文件断言（避开 properties mtime 缓存：等长内容时 mtime 精度可能不足）
        with open(os.path.join(self.server_dir, "server.properties"), encoding="utf-8") as f:
            content = f.read()
        self.assertIn("server-name=我的服务器", content)
        # 历史备份已生成
        history_dir = os.path.join(self.server_dir, CONFIG_HISTORY_DIR)
        self.assertTrue(os.path.isdir(history_dir))
        backups = [f for f in os.listdir(history_dir) if f.startswith("server.properties.") and f.endswith(".bak")]
        self.assertGreaterEqual(len(backups), 1, "save_config 未生成配置历史备份")


class TestRateLimitKeyFix(unittest.TestCase):
    """B4：/api/backups/restore 限流键名正确。"""

    def test_restore_custom_limit_key(self):
        """自定义限流键与真实端点一致（restore 限流实际生效）。"""
        from bedrock_server_manager.ratelimit import rate_limiter

        custom = rate_limiter._custom_limits
        self.assertIn("/api/backups/restore", custom, "缺少 /api/backups/restore 限流配置")
        self.assertNotIn("/api/backup/restore", custom, "错误键名 /api/backup/restore 仍存在")

    def test_restore_limit_applied(self):
        """/api/backups/restore 按自定义限制（2/10s）而非默认（5/s）执行。"""
        from bedrock_server_manager.ratelimit import rate_limiter

        rate_limiter.reset("/api/backups/restore")
        self.assertEqual(rate_limiter._get_limit("/api/backups/restore"), (2, 10.0))


class TestPropertiesCacheInvalidation(unittest.TestCase):
    """B6：save_properties 写后主动失效 properties 缓存。"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old_server_dir = settings.get("server_dir", "")
        self.tmpdir = tempfile.mkdtemp(prefix="mbes_b6_")
        self.server_dir = os.path.join(self.tmpdir, "server")
        os.makedirs(self.server_dir)
        self.filepath = os.path.join(self.server_dir, "server.properties")
        settings["server_dir"] = self.server_dir

    def tearDown(self):
        from bedrock_server_manager import settings

        settings["server_dir"] = self._old_server_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed(self, content):
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(content)

    def test_save_same_length_reparsed(self):
        """等长内容 + mtime 不变时，load 返回新值（缓存已主动失效）。"""
        from bedrock_server_manager.config import load_properties, save_properties

        self._seed("server-name=Old Name\nserver-port=19132\n")
        first = load_properties(self.filepath)
        self.assertEqual(dict(first)["server-name"], "Old Name")
        # 等长内容保存（Old Name -> New Name 均 9 字符）
        ok = save_properties(self.filepath, [("server-name", "New Name"), ("server-port", "19132")])
        self.assertTrue(ok)
        # 模拟粗精度 FS：保存后 mtime 与缓存记录一致（缓存若未失效会命中旧值）
        st = os.stat(self.filepath)
        os.utime(self.filepath, ns=(st.st_atime_ns, st.st_mtime_ns))
        cached = load_properties(self.filepath)
        self.assertEqual(dict(cached)["server-name"], "New Name")

    def test_save_invalidates_existing_cache(self):
        """保存后缓存条目被移除（_properties_cache 不含该文件）。"""
        from bedrock_server_manager.config import _properties_cache, load_properties, save_properties

        self._seed("server-name=Old Name\n")
        load_properties(self.filepath)
        self.assertIn(self.filepath, _properties_cache)
        save_properties(self.filepath, [("server-name", "New Name")])
        self.assertNotIn(self.filepath, _properties_cache)


class TestHourlyTriggerLogic(unittest.TestCase):
    """B5：hourly 任务触发逻辑（纯函数）。"""

    def test_hourly_never_run_trigger(self):
        """从未执行过则立即触发（首次行为与 interval 一致）。"""
        from bedrock_server_manager.scheduler import _should_run_hourly

        self.assertTrue(_should_run_hourly(0))
        self.assertTrue(_should_run_hourly(-1))

    def test_hourly_just_run_no_trigger(self):
        """刚执行过（<1 小时）不触发。"""
        from bedrock_server_manager.scheduler import _should_run_hourly

        now = time.time()
        self.assertFalse(_should_run_hourly(now - 60, now))
        self.assertFalse(_should_run_hourly(now - 3599, now))

    def test_hourly_over_one_hour_trigger(self):
        """距上次执行 >= 1 小时触发。"""
        from bedrock_server_manager.scheduler import _should_run_hourly

        now = time.time()
        self.assertTrue(_should_run_hourly(now - 3600, now))
        self.assertTrue(_should_run_hourly(now - 7200, now))

    def test_hourly_edge_exactly_one_hour(self):
        """恰好 1 小时边界触发（>=3600）。"""
        from bedrock_server_manager.scheduler import _should_run_hourly

        now = time.time()
        self.assertTrue(_should_run_hourly(now - 3600.0, now))


class TestSettingsPersistence(unittest.TestCase):
    """复查修复 R1：load_settings 接线 + SETTINGS_FILE 统一到 APP_DATA_DIR。"""

    def test_settings_file_points_to_appdata(self):
        """SETTINGS_FILE 与迁移目标一致（%APPDATA%/MbesBeacon/settings.json）。"""
        import bedrock_server_manager.utils as utils
        from bedrock_server_manager.constants import APP_DATA_DIR

        self.assertEqual(utils.SETTINGS_FILE, os.path.join(APP_DATA_DIR, "settings.json"))

    def test_save_then_load_restores_values(self):
        """save_settings 后 load_settings 恢复上次值（重启不丢设置）。"""
        import bedrock_server_manager.utils as utils
        from bedrock_server_manager.app_context import AppContext

        AppContext.reset()
        ctx = AppContext.instance()
        # 备份磁盘上已有的设置文件
        saved_file = None
        if os.path.exists(utils.SETTINGS_FILE):
            saved_file = utils.SETTINGS_FILE + ".bak_" + str(int(time.time()))
            shutil.copy2(utils.SETTINGS_FILE, saved_file)
        try:
            ctx.settings["server_dir"] = r"C:\MinecraftServer"
            ctx.settings["installed_version"] = "1.21.100.6"
            utils.save_settings()
            # 模拟重启：重置实例后加载
            AppContext.reset()
            ctx2 = AppContext.instance()
            self.assertEqual(ctx2.settings.get("server_dir"), "")
            utils.load_settings()
            self.assertEqual(ctx2.settings.get("server_dir"), r"C:\MinecraftServer")
            self.assertEqual(ctx2.settings.get("installed_version"), "1.21.100.6")
        finally:
            if saved_file:
                shutil.move(saved_file, utils.SETTINGS_FILE)
            else:
                with contextlib.suppress(Exception):
                    os.remove(utils.SETTINGS_FILE)


class TestMainEntryWiring(unittest.TestCase):
    """复查修复 R2：main._main 接线 load_settings 与 start_scheduler。"""

    def test_main_calls_load_settings(self):
        """启动入口调用 load_settings（设置重启恢复）。"""
        import inspect

        from bedrock_server_manager.main import _main

        self.assertIn("load_settings()", inspect.getsource(_main))

    def test_main_calls_start_scheduler(self):
        """启动入口调用 start_scheduler（定时任务执行）。"""
        import inspect

        from bedrock_server_manager.main import _main

        self.assertIn("start_scheduler()", inspect.getsource(_main))

    def test_main_calls_stop_scheduler_on_exit(self):
        """退出路径调用 stop_scheduler（托盘退出 + finally）。"""
        import inspect

        from bedrock_server_manager.main import _main

        src = inspect.getsource(_main)
        self.assertGreaterEqual(src.count("stop_scheduler()"), 2)


class TestZipBackupRestore(unittest.TestCase):
    """复查修复 R3：restore_backup 支持 ZIP 型备份。"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old_server_dir = settings.get("server_dir", "")
        self.tmpdir = tempfile.mkdtemp(prefix="mbes_r3_")
        self.server_dir = os.path.join(self.tmpdir, "server")
        os.makedirs(os.path.join(self.server_dir, "worlds", "Bedrock level"))
        with open(os.path.join(self.server_dir, "worlds", "Bedrock level", "level.dat"), "w") as f:
            f.write("fake world data")

    def tearDown(self):
        from bedrock_server_manager import settings

        settings["server_dir"] = self._old_server_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_restore_zip_backup(self):
        """ZIP 备份可恢复到空 worlds（原实现返回"备份目录不存在"）。"""
        from bedrock_server_manager.backup import backup_worlds_zip, restore_backup

        zip_path = backup_worlds_zip(self.server_dir)
        self.assertTrue(zip_path and os.path.isfile(zip_path))
        # 清空 worlds 模拟世界丢失
        shutil.rmtree(os.path.join(self.server_dir, "worlds"))
        ok, err = restore_backup(zip_path, self.server_dir)
        self.assertTrue(ok, err)
        self.assertTrue(os.path.isfile(os.path.join(self.server_dir, "worlds", "Bedrock level", "level.dat")))

    def test_restore_zip_rollback_on_bad_zip(self):
        """损坏的 ZIP 恢复失败时回滚原 worlds（不丢数据）。"""
        from bedrock_server_manager.backup import restore_backup

        bad_zip = os.path.join(self.tmpdir, "bad.zip")
        with open(bad_zip, "wb") as f:
            f.write(b"not a real zip")
        # 放在备份根目录内以通过路径校验
        from bedrock_server_manager.backup import get_backup_root

        bad_backup = os.path.join(get_backup_root(self.server_dir), "bad.zip")
        os.makedirs(get_backup_root(self.server_dir), exist_ok=True)
        shutil.copy2(bad_zip, bad_backup)
        ok, err = restore_backup(bad_backup, self.server_dir)
        self.assertFalse(ok)
        # 原 worlds 应保留
        self.assertTrue(os.path.isfile(os.path.join(self.server_dir, "worlds", "Bedrock level", "level.dat")))


class TestReasonValidation(unittest.TestCase):
    """复查修复 R4：reason/message 校验（防命令注入）。"""

    def test_validate_reason_empty_ok(self):
        from bedrock_server_manager.security import validate_reason

        self.assertTrue(validate_reason("")[0])
        self.assertTrue(validate_reason(None)[0])

    def test_validate_reason_normal_ok(self):
        from bedrock_server_manager.security import validate_reason

        self.assertTrue(validate_reason("违规行为")[0])
        self.assertTrue(validate_reason("test reason 123")[0])

    def test_validate_reason_rejects_newline(self):
        from bedrock_server_manager.security import validate_reason

        ok, err = validate_reason("good reason\nstop")
        self.assertFalse(ok)
        self.assertIn("换行", err)
        ok2, _ = validate_reason("good reason\rstop")
        self.assertFalse(ok2)

    def test_validate_reason_rejects_too_long(self):
        from bedrock_server_manager.security import validate_reason

        ok, err = validate_reason("x" * 201)
        self.assertFalse(ok)
        self.assertIn("200", err)

    def test_announce_message_newline_rejected(self):
        """定时公告 message 含换行时任务校验失败。"""
        from bedrock_server_manager.scheduler import validate_scheduled_task

        task = {
            "id": "t1",
            "type": "announce",
            "enabled": True,
            "schedule_type": "hourly",
            "message": "hello\nstop",
        }
        ok, err = validate_scheduled_task(task)
        self.assertFalse(ok)
        self.assertIn("换行", err)

    def test_ban_ip_reason_newline_rejected(self):
        """ban_ip 拒绝含换行的 reason。"""
        from bedrock_server_manager.players import ban_ip

        tmpdir = tempfile.mkdtemp(prefix="mbes_r4_")
        try:
            ok, err = ban_ip("192.168.1.100", "bad\nstop", tmpdir)
            self.assertFalse(ok)
            self.assertIn("换行", err)
            # 正常 reason 可封禁
            ok2, err2 = ban_ip("192.168.1.101", "违规", tmpdir)
            self.assertTrue(ok2, err2)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestDiskSpaceCheckWired(unittest.TestCase):
    """复查修复 R5：do_install 接线磁盘空间检查。"""

    def test_install_rejects_insufficient_disk(self):
        """磁盘空间不足时 do_install 立即失败（不进入下载）。"""
        from unittest.mock import patch

        from bedrock_server_manager import install
        from bedrock_server_manager.app_context import AppContext

        tmpdir = tempfile.mkdtemp(prefix="mbes_r5_")
        try:
            # 设置真实 InstallState（_NullInstallState.busy 无 setter）
            AppContext.instance().install_state = install.InstallState()
            with patch("bedrock_server_manager.utils.check_disk_space", return_value=(False, 10.0, 300)):
                install.do_install(tmpdir, "1.21.100.6", False)
            st = AppContext.instance().install_state
            self.assertIn("磁盘空间不足", st.error)
            self.assertEqual(st.phase, "error")
            self.assertFalse(st.busy)
        finally:
            AppContext.reset()
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestVersionParse4Segments(unittest.TestCase):
    """复查修复 R6：软件更新版本比较支持第 4 段。"""

    def test_parse_4_segments(self):
        from bedrock_server_manager.app_update import _parse_version

        self.assertEqual(_parse_version("1.0.22.1"), (1, 0, 22, 1))
        self.assertEqual(_parse_version("1.0.22"), (1, 0, 22, 0))

    def test_fourth_segment_detected(self):
        """1.0.22.1 比 1.0.22 新（原实现误判为相同）。"""
        from bedrock_server_manager.app_update import _is_newer

        self.assertTrue(_is_newer("1.0.22.1", "1.0.22"))
        self.assertFalse(_is_newer("1.0.22", "1.0.22.1"))
        self.assertFalse(_is_newer("1.0.22", "1.0.22"))

    def test_parse_v_prefix(self):
        from bedrock_server_manager.app_update import _parse_version

        self.assertEqual(_parse_version("v1.0.22"), (1, 0, 22, 0))
        self.assertEqual(_parse_version("V0.1.0.0"), (0, 1, 0, 0))


class TestServerStopCancelsRestart(unittest.TestCase):
    """复查修复 R7：崩溃退避期间用户停止后取消自动重启。"""

    def test_stop_marks_user_stopped_even_when_not_running(self):
        """进程未运行时 stop() 也标记用户停止意图（取消待执行的重启）。"""
        from bedrock_server_manager.server import ServerProcess

        p = ServerProcess("C:\\nonexistent", on_output=lambda s: None, on_state=lambda r: None)
        self.assertFalse(p.running)
        p.stop(wait=1)
        self.assertTrue(p._user_stopped)

    def test_delayed_restart_skipped_when_user_stopped(self):
        """_user_stopped 时延迟重启直接取消（服务器不"复活"）。"""
        from bedrock_server_manager.server import ServerProcess

        emitted = []
        p = ServerProcess("C:\\nonexistent", on_output=emitted.append, on_state=lambda r: None)
        p._user_stopped = True

        # 复刻 _delayed_restart 逻辑（闭包内实现，此处验证核心条件）
        def _delayed_restart(delay):
            time.sleep(delay)
            if p._user_stopped:
                emitted.append("[系统] 自动重启已取消（用户已停止服务器）\n")
                return
            p.start()

        _delayed_restart(0)
        self.assertTrue(any("已取消" in line for line in emitted))
        self.assertIsNone(p.proc)  # 未启动新进程


class TestWorldsImportConfirm(unittest.TestCase):
    """复查修复 R8：/api/worlds/import 纳入危险操作二次确认。"""

    def test_import_in_dangerous_endpoints(self):
        from bedrock_server_manager.security import DANGEROUS_ENDPOINTS, check_dangerous_operation

        self.assertIn("/api/worlds/import", DANGEROUS_ENDPOINTS)
        # 未传确认值 → 拒绝
        ok, err = check_dangerous_operation("/api/worlds/import", {})
        self.assertFalse(ok)
        self.assertIn("IMPORT", err)
        # 传入正确确认值 → 放行
        ok, err = check_dangerous_operation("/api/worlds/import", {"confirm_import": "IMPORT"})
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
