"""
完整代码检查修复（B1-B5）回归测试。

覆盖：
- B1: delete_backup 支持 ZIP 文件型备份删除（原仅支持目录型）
- B2: rename_world 活动世界重命名（原顺序错误导致永远失败）
- B3: WebAppContext.save_config 生成配置历史备份（原传参错误）
- B4: /api/backups/restore 限流键名正确（原拼写缺 s）
- B5: hourly 定时任务纳入调度循环（原被静默跳过）
"""

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
