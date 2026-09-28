"""
P2-4 导出流式化测试：验证 /api/worlds/export 分块流式发送（不整文件读入内存）。

测试覆盖：
- 导出路由已注册
- 分块发送：单块 ≤ 64KB、多块拼接等于 ZIP 文件内容、Content-Length 等于文件真实大小
- worlds 目录不存在 / server_dir 未设置时的错误响应
"""

import os
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock

CHUNK_SIZE = 64 * 1024


def _make_mock_handler():
    """创建一个 mock Handler 实例（参考 test_routes.py 的写法）。"""
    from bedrock_server_manager.web.handler import Handler

    handler = Handler.__new__(Handler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler._json = MagicMock()
    return handler


class TestWorldsExport(unittest.TestCase):
    """P2-4：worlds 导出流式化。"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old_server_dir = settings.get("server_dir", "")
        self.tmpdir = tempfile.mkdtemp(prefix="mbes_export_")
        self.worlds_dir = os.path.join(self.tmpdir, "worlds")
        os.makedirs(self.worlds_dir)

    def tearDown(self):
        import shutil

        from bedrock_server_manager import settings

        settings["server_dir"] = self._old_server_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_world_file(self, size):
        """在 worlds 目录下创建指定大小的文件。"""
        path = os.path.join(self.worlds_dir, "level.dat")
        with open(path, "wb") as f:
            f.write(os.urandom(size))
        return path

    def test_export_route_registered(self):
        """导出路由已注册到 GET 路由表。"""
        from bedrock_server_manager.web.handler import Handler

        self.assertIn("/api/worlds/export", Handler._get_routes)

    def test_export_streams_in_chunks(self):
        """导出分块流式发送：每块 ≤ 64KB，多块拼接等于文件内容。"""
        from bedrock_server_manager import settings
        from bedrock_server_manager.web.routes.worlds import WorldsRoutesMixin

        # 世界文件 200KB > 单块 64KB，必然触发多块
        self._make_world_file(200 * 1024)
        settings["server_dir"] = self.tmpdir

        handler = _make_mock_handler()
        WorldsRoutesMixin._route_worlds_export(handler, {})

        # 200 响应 + 正确 Content-Type
        handler.send_response.assert_called_once_with(200)
        header_calls = {c.args[0]: c.args[1] for c in handler.send_header.call_args_list}
        self.assertEqual(header_calls.get("Content-Type"), "application/zip")

        # Content-Length 等于实际 ZIP 文件大小
        self.assertIn("Content-Length", header_calls)
        written = b"".join(c.args[0] for c in handler.wfile.write.call_args_list)
        self.assertEqual(int(header_calls["Content-Length"]), len(written))

        # 分块：多次 write 且单块不超过 64KB
        writes = handler.wfile.write.call_args_list
        self.assertGreater(len(writes), 1, "大文件应触发多次分块写入")
        for call in writes:
            self.assertLessEqual(len(call.args[0]), CHUNK_SIZE, "单块超过 64KB")

        # 拼接结果应为合法 ZIP，且包含 world 文件
        zip_bytes = written
        self.assertTrue(zip_bytes.startswith(b"PK\x03\x04") or zip_bytes.startswith(b"PK\x05\x06"))
        with zipfile.ZipFile(__import__("io").BytesIO(zip_bytes), "r") as zf:
            names = zf.namelist()
        self.assertTrue(any("level.dat" in n for n in names), f"ZIP 缺少 level.dat，实际: {names}")

    def test_export_missing_worlds_dir(self):
        """worlds 目录不存在时返回错误 JSON。"""
        from bedrock_server_manager import settings
        from bedrock_server_manager.web.routes.worlds import WorldsRoutesMixin

        settings["server_dir"] = self.tmpdir
        os.rmdir(self.worlds_dir)  # 删除 worlds 目录

        handler = _make_mock_handler()
        WorldsRoutesMixin._route_worlds_export(handler, {})
        handler._json.assert_called_once()
        payload = handler._json.call_args.args[0]
        self.assertFalse(payload["ok"])

    def test_export_no_server_dir(self):
        """server_dir 未设置时返回错误 JSON。"""
        from bedrock_server_manager import settings
        from bedrock_server_manager.web.routes.worlds import WorldsRoutesMixin

        settings["server_dir"] = ""

        handler = _make_mock_handler()
        WorldsRoutesMixin._route_worlds_export(handler, {})
        handler._json.assert_called_once()
        payload = handler._json.call_args.args[0]
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
