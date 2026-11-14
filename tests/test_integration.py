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
from types import SimpleNamespace
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


class TestHostHeaderCheck(unittest.TestCase):
    """Host 头校验（防 DNS rebinding）"""

    def test_accepts_loopback_hosts(self):
        for host in ("127.0.0.1:19100", "localhost:19100", "127.0.0.1"):
            handler = FakeHandler({"Host": host})
            self.assertTrue(bsm.Handler._check_host(handler), host)

    def test_rejects_foreign_host(self):
        for host in ("evil.com:19100", "attacker.example.com", "127.0.0.1.evil.com:19100"):
            handler = FakeHandler({"Host": host})
            self.assertFalse(bsm.Handler._check_host(handler), host)

    def test_rejects_missing_host(self):
        handler = FakeHandler({})
        self.assertFalse(bsm.Handler._check_host(handler))

    def test_rejects_wrong_port(self):
        """端口不符说明连的不是本实例，拒绝"""
        handler = FakeHandler({"Host": "127.0.0.1:9999"})
        handler.server = SimpleNamespace(server_port=19100)
        self.assertFalse(bsm.Handler._check_host(handler))

    def test_accepts_matching_port(self):
        handler = FakeHandler({"Host": "127.0.0.1:19100"})
        handler.server = SimpleNamespace(server_port=19100)
        self.assertTrue(bsm.Handler._check_host(handler))


class TestServerDirSelection(unittest.TestCase):
    """服务器目录选择集成"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.valid_dir = os.path.join(self.tmpdir, "valid")
        os.makedirs(self.valid_dir)
        with open(os.path.join(self.valid_dir, bsm.SERVER_EXE), "wb") as f:
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
        self.assertIn(bsm.SERVER_EXE, err)

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
        self.assertIn("line1", text)
        buf.clear()
        text, count = buf.read_since(0)
        # 游标是单调递增的绝对计数：clear 只清空内容，不回退游标，
        # 否则已连接的客户端会把旧日志重放一遍。
        self.assertEqual(text, "")
        self.assertEqual(count, 2)
        # 清空后旧游标落在保留窗口之前，应能检测到"有日志被丢弃"
        self.assertTrue(buf.dropped_since(0))

    def test_max_lines_truncation(self):
        buf = bsm.ConsoleBuffer()
        original_max = buf.MAX_LINES
        buf.MAX_LINES = 5
        try:
            for i in range(10):
                buf.append(f"line{i}\n")
            text, count = buf.read_since(0)
            # 保留窗口受 MAX_LINES 限制，但游标必须继续增长；
            # 旧实现让 count 停在 MAX_LINES，客户端追平后永久读不到新日志。
            self.assertLessEqual(len(buf), 5)
            self.assertEqual(count, 10)
            self.assertIn("line9", text)
            self.assertNotIn("line0", text)
            self.assertTrue(buf.dropped_since(0))
        finally:
            buf.MAX_LINES = original_max

    def test_cursor_keeps_advancing_past_max_lines(self):
        """回归：缓冲区打满后客户端游标仍能持续读到新日志（原实现永久冻结）。"""
        buf = bsm.ConsoleBuffer()
        original_max = buf.MAX_LINES
        buf.MAX_LINES = 5
        try:
            for i in range(10):
                buf.append(f"line{i}\n")
            _, cursor = buf.read_since(0)
            self.assertEqual(cursor, 10)
            # 再写入新行，客户端用上一步游标继续读取
            for i in range(10, 13):
                buf.append(f"line{i}\n")
            text, cursor = buf.read_since(cursor)
            self.assertEqual(text, "line10\nline11\nline12\n")
            self.assertEqual(cursor, 13)
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
        with open(os.path.join(self.server_dir, bsm.SERVER_EXE), "wb") as f:
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


class TestAtomicWrite(unittest.TestCase):
    """原子写入：崩溃/断电不应留下截断的半文件"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mbes_atomic_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_atomic_write_text_replaces_content(self):
        from bedrock_server_manager.utils import atomic_write_text

        p = os.path.join(self.tmpdir, "a.txt")
        atomic_write_text(p, "first")
        atomic_write_text(p, "second")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "second")
        # 不应残留中间临时文件
        self.assertEqual(sorted(os.listdir(self.tmpdir)), ["a.txt"])

    def test_no_tmp_leftovers(self):
        from bedrock_server_manager.utils import atomic_write_json

        p = os.path.join(self.tmpdir, "data.json")
        atomic_write_json(p, {"k": "v"})
        leftovers = [n for n in os.listdir(self.tmpdir) if ".tmp" in n]
        self.assertEqual(leftovers, [], f"残留临时文件: {leftovers}")

    def test_atomic_write_json_roundtrip_chinese(self):
        import json as _json

        from bedrock_server_manager.utils import atomic_write_json

        p = os.path.join(self.tmpdir, "cn.json")
        atomic_write_json(p, {"名称": "我的世界", "值": 1})
        with open(p, encoding="utf-8") as f:
            self.assertEqual(_json.load(f)["名称"], "我的世界")

    def test_failed_write_keeps_original(self):
        """写入失败时原文件保持不变（不能先截断再失败）"""
        from unittest.mock import patch

        from bedrock_server_manager.utils import atomic_write_text

        p = os.path.join(self.tmpdir, "keep.txt")
        atomic_write_text(p, "original")
        with patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            atomic_write_text(p, "broken")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "original")


class TestStateCompatibilityExport(unittest.TestCase):
    """bsm.server_proc / httpd / server_lock 应转发到当前 AppContext，而不是恒 None"""

    def test_package_exports_follow_app_context(self):
        from bedrock_server_manager import app_context as ac

        ctx = ac.AppContext.instance()
        self.assertIs(bsm.server_proc, ctx.server_proc)
        self.assertIs(bsm.httpd, ctx.httpd)
        self.assertIs(bsm.server_lock, ctx.server_lock)

    def test_state_no_longer_shadows_with_none(self):
        """state 不应再暴露恒为 None 的 server_proc（会让调用方误以为能读）"""
        from bedrock_server_manager import state

        self.assertFalse(hasattr(state, "server_proc"))


class TestPortCheck(unittest.TestCase):
    """端口占用检测：Bedrock 是 UDP，必须按 UDP 判定"""

    def test_free_port_not_in_use(self):
        from bedrock_server_manager.server import check_port_in_use

        # 随机高位端口，几乎不可能被占用
        self.assertFalse(check_port_in_use(54321, host="127.0.0.1"))

    def test_occupied_udp_port_detected(self):
        import socket

        from bedrock_server_manager.server import check_port_in_use

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("0.0.0.0", 54322))
            self.assertTrue(check_port_in_use(54322, host="0.0.0.0"), "UDP 端口已绑定却未被检测到占用")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
