"""
P3 门禁：三类回归测试补齐（unit / integration / e2e）。

- TestUnitSecurityVerify（unit）：安全模块纯函数（命令校验、危险端点、文件校验）
- TestIntegrationRoutes（integration）：路由层跨模块流程（备份删除/恢复确认、配置保存、服务器状态）
- TestE2EAuthFlow（e2e）：Web 认证（token 缺失/错误拒绝、Origin 不匹配拒绝）
"""

import os
import shutil
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock


def _make_mock_handler():
    """构造 mock Handler 实例（参考 test_routes.py 的写法）。"""
    from bedrock_server_manager.web.handler import Handler

    handler = Handler.__new__(Handler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler._json = MagicMock()
    return handler


class TestUnitSecurityVerify(unittest.TestCase):
    """unit：安全模块纯函数回归。"""

    def test_validate_command_empty_rejected(self):
        """空指令被拒绝。"""
        from bedrock_server_manager.security import validate_command

        ok, err, _ = validate_command("")
        self.assertFalse(ok)
        self.assertIn("不能为空", err)
        ok, err, _ = validate_command("   ")
        self.assertFalse(ok)

    def test_validate_command_dangerous_classified(self):
        """危险指令被标记（stop/restart 单命令、whitelist remove 多词前缀）。"""
        from bedrock_server_manager.security import validate_command

        for cmd in ["stop", "restart", "whitelist remove Steve"]:
            ok, err, dangerous = validate_command(cmd)
            self.assertTrue(ok, f"{cmd}: {err}")
            self.assertTrue(dangerous, f"{cmd} 应被标记为危险")

    def test_validate_command_safe_classified(self):
        """安全指令不被标记（list 等）。"""
        from bedrock_server_manager.security import validate_command

        ok, err, dangerous = validate_command("list")
        self.assertTrue(ok, err)
        self.assertFalse(dangerous)

    def test_is_dangerous_endpoint(self):
        """危险端点识别。"""
        from bedrock_server_manager.security import is_dangerous_endpoint

        self.assertTrue(is_dangerous_endpoint("/api/backups/restore"))
        self.assertTrue(is_dangerous_endpoint("/api/server/delete"))
        self.assertFalse(is_dangerous_endpoint("/api/status"))

    def test_check_dangerous_operation_confirm(self):
        """危险操作二次确认：正确确认值放行、缺失/错误拒绝。"""
        from bedrock_server_manager.security import check_dangerous_operation

        ok, _ = check_dangerous_operation("/api/backups/restore", {"confirm_restore": "RESTORE"})
        self.assertTrue(ok)
        ok, err = check_dangerous_operation("/api/backups/restore", {})
        self.assertFalse(ok)
        self.assertIn("RESTORE", err)
        ok, err = check_dangerous_operation("/api/backups/restore", {"confirm_restore": "wrong"})
        self.assertFalse(ok)
        self.assertIn("确认值不正确", err)

    def test_check_dangerous_operation_optional(self):
        """可选确认端点：未传确认时放行（stop/restart）。"""
        from bedrock_server_manager.security import check_dangerous_operation

        ok, _ = check_dangerous_operation("/api/server/restart", {})
        self.assertTrue(ok)
        ok, _ = check_dangerous_operation("/api/server/stop", {})
        self.assertTrue(ok)

    def test_verify_file_size(self):
        """文件大小校验：存在/过小/缺失。"""
        from bedrock_server_manager.verify import verify_file_size

        tmpdir = tempfile.mkdtemp(prefix="mbes_vfs_")
        try:
            path = os.path.join(tmpdir, "f.bin")
            with open(path, "wb") as f:
                f.write(b"x" * 1024)
            ok, _, size = verify_file_size(path, min_size=100, max_size=10000)
            self.assertTrue(ok)
            self.assertEqual(size, 1024)
            ok, _, _ = verify_file_size(path, min_size=10000, max_size=20000)
            self.assertFalse(ok)
            ok, _, _ = verify_file_size(os.path.join(tmpdir, "missing.bin"))
            self.assertFalse(ok)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_verify_zip_integrity(self):
        """ZIP 完整性校验：合法通过、损坏拒绝。"""
        from bedrock_server_manager.verify import verify_zip_integrity

        tmpdir = tempfile.mkdtemp(prefix="mbes_vzi_")
        try:
            good = os.path.join(tmpdir, "good.zip")
            with zipfile.ZipFile(good, "w") as zf:
                zf.writestr("a.txt", "hello")
            ok, msg = verify_zip_integrity(good)
            self.assertTrue(ok, msg)
            bad = os.path.join(tmpdir, "bad.zip")
            with open(bad, "wb") as f:
                f.write(b"not a zip")
            ok, msg = verify_zip_integrity(bad)
            self.assertFalse(ok)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_calculate_sha256(self):
        """SHA256 计算与已知内容一致。"""
        import hashlib

        from bedrock_server_manager.verify import calculate_sha256

        tmpdir = tempfile.mkdtemp(prefix="mbes_sha_")
        try:
            path = os.path.join(tmpdir, "f.bin")
            content = b"MbesBeacon smoke content"
            with open(path, "wb") as f:
                f.write(content)
            actual = calculate_sha256(path)
            self.assertEqual(actual, hashlib.sha256(content).hexdigest())
            self.assertIsNone(calculate_sha256(os.path.join(tmpdir, "missing.bin")))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestIntegrationRoutes(unittest.TestCase):
    """integration：路由层跨模块流程回归。"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old_server_dir = settings.get("server_dir", "")
        self.tmpdir = tempfile.mkdtemp(prefix="mbes_p3int_")
        self.server_dir = os.path.join(self.tmpdir, "server")
        os.makedirs(os.path.join(self.server_dir, "worlds"))
        with open(os.path.join(self.server_dir, "worlds", "level.dat"), "w") as f:
            f.write("fake world data")
        settings["server_dir"] = self.server_dir

    def tearDown(self):
        from bedrock_server_manager import settings

        settings["server_dir"] = self._old_server_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_backups_delete_route_zip(self):
        """/api/backups/delete 可删除 zip 备份（B1 路由层回归）。"""
        from bedrock_server_manager.backup import backup_worlds_zip
        from bedrock_server_manager.web.routes.backups import BackupsRoutesMixin

        zip_path = backup_worlds_zip(self.server_dir)
        handler = _make_mock_handler()
        BackupsRoutesMixin._route_backup_delete(handler, {"path": zip_path})
        payload = handler._json.call_args.args[0]
        self.assertTrue(payload["ok"], payload.get("error"))
        self.assertFalse(os.path.exists(zip_path))

    def test_backups_restore_route_requires_confirm(self):
        """/api/backups/restore 未确认时返回 need_confirm。"""
        from bedrock_server_manager.web.routes.backups import BackupsRoutesMixin

        handler = _make_mock_handler()
        BackupsRoutesMixin._route_backup_restore(handler, {"path": "/whatever"})
        payload = handler._json.call_args.args[0]
        self.assertFalse(payload["ok"])
        self.assertTrue(payload.get("need_confirm"))
        self.assertEqual(payload.get("confirm_value"), "RESTORE")

    def test_backups_restore_route_with_confirm(self):
        """/api/backups/restore 正确确认后进入恢复流程（备份路径不存在报错而非确认错误）。"""
        from bedrock_server_manager.web.routes.backups import BackupsRoutesMixin

        handler = _make_mock_handler()
        BackupsRoutesMixin._route_backup_restore(handler, {"path": "/nonexistent", "confirm_restore": "RESTORE"})
        payload = handler._json.call_args.args[0]
        self.assertFalse(payload["ok"])
        self.assertNotIn("need_confirm", payload)

    def test_config_save_route(self):
        """/api/config 保存配置：写入 server.properties 并返回 ok。"""
        from bedrock_server_manager.web.routes.post_extra import PostExtraRoutesMixin

        with open(os.path.join(self.server_dir, "server.properties"), "w", encoding="utf-8") as f:
            f.write("server-name=Old Name\nserver-port=19132\n")
        handler = _make_mock_handler()
        PostExtraRoutesMixin._post_config(handler, {"data": {"server-name": "New Name"}})
        payload = handler._json.call_args.args[0]
        self.assertTrue(payload["ok"], payload.get("error"))
        # 直接读文件断言（避开 properties mtime 缓存：等长内容时 mtime 精度可能不足）
        with open(os.path.join(self.server_dir, "server.properties"), encoding="utf-8") as f:
            content = f.read()
        self.assertIn("server-name=New Name", content)

    def test_server_status_route(self):
        """/api/status 返回服务器状态（GET 路由，mock handler）。"""
        from bedrock_server_manager.web.routes.get_routes import GetRoutesMixin

        handler = _make_mock_handler()
        GetRoutesMixin._get_status(handler, {})
        payload = handler._json.call_args.args[0]
        self.assertIn("installed", payload)
        self.assertIn("server_running", payload)


class TestE2EAuthFlow(unittest.TestCase):
    """e2e：Web 认证流程（真实 HTTP 栈）。"""

    @classmethod
    def setUpClass(cls):
        import threading

        from bedrock_server_manager import settings
        from bedrock_server_manager.app_context import AppContext
        from bedrock_server_manager.web.handler import Handler

        cls.tmpdir = tempfile.mkdtemp(prefix="mbes_p3e2e_")
        cls.server_dir = os.path.join(cls.tmpdir, "server")
        os.makedirs(cls.server_dir)
        cls._old_server_dir = settings.get("server_dir", "")
        settings["server_dir"] = cls.server_dir
        ctx = AppContext.instance()
        cls.token = ctx.api_token
        cls.httpd = None
        import socket as _socket

        with _socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        # 使用线程启动真实 HTTP 服务
        from http.server import ThreadingHTTPServer

        cls._httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), Handler)
        cls._thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        import shutil

        from bedrock_server_manager import settings

        cls._httpd.shutdown()
        cls._httpd.server_close()
        settings["server_dir"] = cls._old_server_dir
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _request(self, path, token=None, origin=None):
        import urllib.error
        import urllib.request

        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url)
        if token:
            req.add_header("X-Api-Token", token)
        if origin:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def test_status_with_token(self):
        """携带正确 token 访问 /api/status 返回 200。"""
        code, body = self._request("/api/status", token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("installed", body)

    def test_status_without_token_rejected(self):
        """无 token 且无 Origin/Referer 时拒绝（403）。"""
        code, body = self._request("/api/status")
        self.assertEqual(code, 403)
        self.assertIn("来源", body)

    def test_status_wrong_token_rejected(self):
        """错误 token 拒绝（403）。"""
        code, body = self._request("/api/status", token="wrong-token")
        self.assertEqual(code, 403)

    def test_status_foreign_origin_rejected(self):
        """外来 Origin（非本地端口）拒绝（403）。"""
        code, body = self._request("/api/status", origin="https://evil.example.com")
        self.assertEqual(code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
