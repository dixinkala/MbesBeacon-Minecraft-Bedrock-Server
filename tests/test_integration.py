"""
集成测试 - Minecraft 基岩版服务器管理器
覆盖：认证校验逻辑、配置校验、服务器目录选择、状态管理、纯函数集成
HTTP层由 test_e2e.py 通过真实服务器覆盖
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bedrock_server_manager as bsm


class FakeHeaders(dict):
    """模拟HTTP请求头"""

    def get(self, key, default=""):
        return super().get(key, default)


class FakeHandler:
    """模拟Handler，仅用于测试_check_auth"""

    def __init__(self, headers=None):
        self.headers = FakeHeaders(headers or {})


class TestAuthCheck(unittest.TestCase):
    """认证校验逻辑"""

    def test_valid_token(self):
        handler = FakeHandler({"X-API-Token": bsm.API_TOKEN})
        self.assertTrue(bsm.Handler._check_auth(handler))

    def test_invalid_token(self):
        handler = FakeHandler({"X-API-Token": "wrong-token"})
        self.assertFalse(bsm.Handler._check_auth(handler))

    def test_no_token_no_origin(self):
        handler = FakeHandler({})
        self.assertFalse(bsm.Handler._check_auth(handler))

    def test_valid_origin_127(self):
        handler = FakeHandler({"Origin": "http://127.0.0.1:19100"})
        self.assertTrue(bsm.Handler._check_auth(handler))

    def test_valid_origin_localhost(self):
        handler = FakeHandler({"Origin": "http://localhost:19100"})
        self.assertTrue(bsm.Handler._check_auth(handler))

    def test_invalid_origin(self):
        handler = FakeHandler({"Origin": "http://evil.com"})
        self.assertFalse(bsm.Handler._check_auth(handler))

    def test_valid_referer(self):
        handler = FakeHandler({"Referer": "http://127.0.0.1:19100/somepage"})
        self.assertTrue(bsm.Handler._check_auth(handler))

    def test_invalid_referer(self):
        handler = FakeHandler({"Referer": "http://evil.com/page"})
        self.assertFalse(bsm.Handler._check_auth(handler))

    def test_token_takes_precedence(self):
        """无效token但有效origin仍应通过（origin作为后备）"""
        handler = FakeHandler({"X-API-Token": "wrong", "Origin": "http://127.0.0.1:19100"})
        self.assertTrue(bsm.Handler._check_auth(handler))


class TestServerDirSelection(unittest.TestCase):
    """服务器目录选择集成"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.valid_dir = os.path.join(self.tmpdir, "valid")
        os.makedirs(self.valid_dir)
        with open(os.path.join(self.valid_dir, "bedrock_server.exe"), "wb") as f:
            f.write(b"MZ")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_select_valid_dir(self):
        with patch.object(bsm, "save_settings"), patch.object(bsm, "detect_server_version", return_value="1.21.0.03"):
            ok, err, info = bsm.select_server_dir(self.valid_dir)
        self.assertTrue(ok, err)
        self.assertEqual(info["dir"], self.valid_dir)

    def test_select_nonexistent_dir(self):
        ok, err, info = bsm.select_server_dir(os.path.join(self.tmpdir, "nonexistent"))
        self.assertFalse(ok)
        self.assertIn("不存在", err)

    def test_select_dir_without_exe(self):
        empty_dir = os.path.join(self.tmpdir, "empty")
        os.makedirs(empty_dir)
        ok, err, info = bsm.select_server_dir(empty_dir)
        self.assertFalse(ok)
        self.assertIn("bedrock_server.exe", err)

    def test_select_empty_dir(self):
        ok, err, info = bsm.select_server_dir("")
        self.assertFalse(ok)

    def test_select_none_dir(self):
        ok, err, info = bsm.select_server_dir(None)
        self.assertFalse(ok)


class TestConfigIntegration(unittest.TestCase):
    """配置读写集成测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.server_dir = os.path.join(self.tmpdir, "server")
        os.makedirs(self.server_dir)
        self.props_path = os.path.join(self.server_dir, "server.properties")
        with open(self.props_path, "w", encoding="utf-8") as f:
            f.write("# 服务器配置\n")
            f.write("server-name=Test Server\n")
            f.write("max-players=10\n")
            f.write("view-distance=32\n")
            f.write("# 自定义项\n")
            f.write("custom-key=custom-value\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_and_save_roundtrip(self):
        items = bsm.load_properties(self.props_path)
        d = dict(items)
        self.assertEqual(d["server-name"], "Test Server")
        # 修改并保存
        bsm.save_properties(self.props_path, [("server-name", "Updated"), ("max-players", "20")])
        # 重新读取
        items2 = bsm.load_properties(self.props_path)
        d2 = dict(items2)
        self.assertEqual(d2["server-name"], "Updated")
        self.assertEqual(d2["max-players"], "20")
        # 验证自定义项保留
        self.assertEqual(d2["custom-key"], "custom-value")

    def test_save_preserves_comments(self):
        bsm.save_properties(self.props_path, [("max-players", "15")])
        with open(self.props_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("# 服务器配置", content)
        self.assertIn("# 自定义项", content)

    def test_validate_then_save(self):
        """先校验再保存的完整流程"""
        updates = {"max-players": "25", "view-distance": "48"}
        ok, err, field = bsm.validate_config_updates(updates)
        self.assertTrue(ok, err)
        bsm.save_properties(self.props_path, list(updates.items()))
        items = bsm.load_properties(self.props_path)
        d = dict(items)
        self.assertEqual(d["max-players"], "25")
        self.assertEqual(d["view-distance"], "48")

    def test_validate_rejects_invalid(self):
        """校验拒绝无效值，不保存"""
        updates = {"max-players": "999"}
        ok, err, field = bsm.validate_config_updates(updates)
        self.assertFalse(ok)
        # 验证文件未被修改
        items = bsm.load_properties(self.props_path)
        d = dict(items)
        self.assertEqual(d["max-players"], "10")


class TestPlayerManagementIntegration(unittest.TestCase):
    """玩家管理集成测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.server_dir = os.path.join(self.tmpdir, "server")
        os.makedirs(self.server_dir)
        self.allowlist_path = os.path.join(self.server_dir, "allowlist.json")
        with open(self.allowlist_path, "w", encoding="utf-8") as f:
            json.dump([{"name": "Existing", "ignoresPlayerLimit": False}], f)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_validate_then_add_allowlist(self):
        """校验玩家名后添加到白名单"""
        name = "NewPlayer"
        ok, err = bsm.validate_player_name(name)
        self.assertTrue(ok)
        result = bsm.add_allowlist(self.server_dir, name)
        self.assertTrue(result)
        with open(self.allowlist_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertTrue(any(p["name"] == name for p in data))

    def test_invalid_name_rejected(self):
        """无效玩家名被拒绝，不添加"""
        name = "invalid name!"
        ok, err = bsm.validate_player_name(name)
        self.assertFalse(ok)
        # 不调用add_allowlist，验证文件未变
        with open(self.allowlist_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)

    def test_add_then_remove_allowlist(self):
        """添加后移除的完整流程"""
        name = "TempPlayer"
        bsm.add_allowlist(self.server_dir, name)
        with open(self.allowlist_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 2)
        # 移除
        bsm.remove_allowlist(self.server_dir, name)
        with open(self.allowlist_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)


class TestBackupIntegration(unittest.TestCase):
    """备份集成测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.server_dir = os.path.join(self.tmpdir, "MyServer")
        self.worlds_dir = os.path.join(self.server_dir, "worlds", "Bedrock level")
        os.makedirs(self.worlds_dir)
        with open(os.path.join(self.worlds_dir, "level.dat"), "wb") as f:
            f.write(b"\x00" * 512)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_backup_and_list(self):
        """创建备份后列出备份"""
        result = bsm.backup_worlds(self.server_dir)
        self.assertTrue(result)
        backups = bsm.list_backups(self.server_dir)
        self.assertGreater(len(backups), 0)

    def test_backup_zip(self):
        """创建ZIP压缩备份"""
        result = bsm.backup_worlds_zip(self.server_dir)
        self.assertTrue(result)
        self.assertTrue(result.endswith(".zip"))
        self.assertTrue(os.path.isfile(result))

    def test_get_backup_root(self):
        root = bsm.get_backup_root(self.server_dir)
        self.assertIn("_worlds_backups", root)

    def test_backup_no_worlds(self):
        shutil.rmtree(os.path.join(self.server_dir, "worlds"))
        result = bsm.backup_worlds(self.server_dir)
        self.assertEqual(result, "")


class TestConsoleBufferIntegration(unittest.TestCase):
    """控制台缓冲区集成"""

    def test_append_read_clear(self):
        buf = bsm.ConsoleBuffer()
        buf.append("line1\n")
        buf.append("line2\n")
        text, count = buf.read_since(0)
        self.assertEqual(count, 2)
        buf.clear()
        text, count = buf.read_since(0)
        self.assertEqual(count, 0)

    def test_max_lines_truncation(self):
        buf = bsm.ConsoleBuffer()
        original_max = buf.MAX_LINES
        buf.MAX_LINES = 5
        try:
            for i in range(10):
                buf.append(f"line{i}\n")
            text, count = buf.read_since(0)
            self.assertLessEqual(count, 5)
        finally:
            buf.MAX_LINES = original_max

    def test_read_since_offset(self):
        buf = bsm.ConsoleBuffer()
        buf.append("old\n")
        _, offset = buf.read_since(0)
        buf.append("new\n")
        text, _ = buf.read_since(offset)
        self.assertIn("new", text)
        self.assertNotIn("old", text)

    def test_wait_for_new(self):
        import threading

        buf = bsm.ConsoleBuffer()
        result = []

        def waiter():
            text, count = buf.wait_for_new(0, timeout=1.0)
            result.append((text, count))

        t = threading.Thread(target=waiter)
        t.start()
        import time

        time.sleep(0.1)
        buf.append("delayed\n")
        t.join(timeout=2)
        self.assertEqual(len(result), 1)
        self.assertIn("delayed", result[0][0])


class TestInstallStateIntegration(unittest.TestCase):
    """安装状态集成"""

    def test_snapshot_keys(self):
        state = bsm.InstallState()
        snap = state.snapshot()
        for key in ["busy", "phase", "percent", "text", "log", "done", "error"]:
            self.assertIn(key, snap)

    def test_initial_state(self):
        state = bsm.InstallState()
        snap = state.snapshot()
        self.assertFalse(snap["busy"])
        self.assertEqual(snap["percent"], 0)
        self.assertEqual(snap["phase"], "idle")
        self.assertFalse(snap["done"])

    def test_cancel_flag(self):
        state = bsm.InstallState()
        self.assertFalse(state.cancel.is_set())
        state.cancel.set()
        self.assertTrue(state.cancel.is_set())


class TestDetectServersIntegration(unittest.TestCase):
    """服务器检测集成"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.server_dir = os.path.join(self.tmpdir, "test_server")
        os.makedirs(self.server_dir)
        with open(os.path.join(self.server_dir, "bedrock_server.exe"), "wb") as f:
            f.write(b"MZ")
        # 直接设置 settings 中的 server_dir（比 patch resolve_server_dir 更可靠）
        from bedrock_server_manager.state import settings

        self._orig_dir = settings.get("server_dir", "")
        settings["server_dir"] = self.server_dir

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        bsm._detect_cache = {"result": None, "time": 0}
        from bedrock_server_manager.state import settings

        settings["server_dir"] = self._orig_dir

    def test_detect_finds_server(self):
        servers = bsm.detect_servers(force_refresh=True)
        self.assertIn(os.path.abspath(self.server_dir), servers)

    def test_detect_cache(self):
        bsm.detect_servers(force_refresh=True)
        # 第二次调用应使用缓存
        servers = bsm.detect_servers()
        self.assertIn(os.path.abspath(self.server_dir), servers)


class TestCommandValidationIntegration(unittest.TestCase):
    """指令校验集成"""

    def test_safe_command_passes(self):
        ok, err, dangerous = bsm.validate_command("list")
        self.assertTrue(ok)
        self.assertFalse(dangerous)

    def test_dangerous_command_flagged(self):
        for cmd in ["stop", "ban Player1", "whitelist remove Player1"]:
            ok, err, dangerous = bsm.validate_command(cmd)
            self.assertTrue(ok, f"{cmd} should be valid")
            self.assertTrue(dangerous, f"{cmd} should be dangerous")

    def test_empty_command_rejected(self):
        ok, err, dangerous = bsm.validate_command("")
        self.assertFalse(ok)

    def test_whitelist_add_not_dangerous(self):
        ok, err, dangerous = bsm.validate_command("whitelist add Player1")
        self.assertTrue(ok)
        self.assertFalse(dangerous)


if __name__ == "__main__":
    unittest.main(verbosity=2)
