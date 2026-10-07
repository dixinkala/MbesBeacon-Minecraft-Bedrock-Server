"""
浏览目录路由测试：覆盖 browse-dir 取消语义与 listdir 盘符支持。

测试覆盖：
- /api/browse-dir 选中目录返回 ok+dir
- /api/browse-dir 用户取消返回 ok+cancelled（与"调用失败"区分）
- /api/listdir 空路径返回"我的电脑"盘符列表
- /api/listdir 盘根目录时其他盘符置顶，支持跨盘切换
"""

import unittest
from unittest.mock import MagicMock, patch

from bedrock_server_manager.constants import IS_WINDOWS


class TestBrowseDirRoutes(unittest.TestCase):
    """测试浏览目录相关路由。"""

    def _create_mock_handler(self):
        """创建一个 mock Handler 实例。"""
        from bedrock_server_manager.web.handler import Handler

        handler = Handler.__new__(Handler)
        handler._json = MagicMock()
        handler._send = MagicMock()
        handler._post_data = MagicMock(return_value={})
        handler._check_auth = MagicMock(return_value=True)
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler.rfile = MagicMock()
        handler.wfile = MagicMock()
        handler.path = "/api/browse-dir"
        return handler

    @patch("bedrock_server_manager.web.routes.get_routes.browse_directory")
    def test_browse_dir_selected_returns_dir(self, mock_browse):
        """选择目录成功时返回 ok:true 与目录路径。"""
        from bedrock_server_manager.web.routes.get_routes import GetRoutesMixin as Handler

        mock_browse.return_value = "D:\\MinecraftServer"
        handler = self._create_mock_handler()
        Handler._get_browse_dir(handler, {"initial": [""]})
        payload = handler._json.call_args[0][0]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["dir"], "D:\\MinecraftServer")
        self.assertNotIn("cancelled", payload)

    @patch("bedrock_server_manager.web.routes.get_routes.browse_directory")
    def test_browse_dir_cancelled_returns_cancelled(self, mock_browse):
        """用户取消选择时返回 ok:true + cancelled:true，供前端区分取消与失败。"""
        from bedrock_server_manager.web.routes.get_routes import GetRoutesMixin as Handler

        mock_browse.return_value = None
        handler = self._create_mock_handler()
        Handler._get_browse_dir(handler, {"initial": [""]})
        payload = handler._json.call_args[0][0]
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload.get("dir"))
        self.assertTrue(payload.get("cancelled"))

    @unittest.skipUnless(IS_WINDOWS, "Windows 专属：盘符视图")
    def test_listdir_empty_returns_drives(self):
        """空路径（我的电脑）应返回全部可用盘符。"""
        from bedrock_server_manager.web.routes.get_routes import GetRoutesMixin as Handler

        with patch("os.path.exists", side_effect=lambda p: p.lower() in ("c:\\", "d:\\")):
            handler = self._create_mock_handler()
            Handler._get_listdir(handler, {"path": [""]})
            payload = handler._json.call_args[0][0]
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["path"], "")
            self.assertIn("C:\\", payload["drives"])
            self.assertIn("D:\\", payload["drives"])
            self.assertEqual(payload["dirs"], payload["drives"])

    @unittest.skipUnless(not IS_WINDOWS, "Linux 专属：根目录起始")
    def test_listdir_empty_starts_at_root(self):
        """Linux 空路径应从文件系统根 / 开始。"""
        import os

        from bedrock_server_manager.web.routes.get_routes import GetRoutesMixin as Handler

        with (
            patch("os.path.isdir", return_value=True),
            patch("bedrock_server_manager.web.routes.get_routes.os.listdir", return_value=["home", "usr"]),
        ):
            handler = self._create_mock_handler()
            Handler._get_listdir(handler, {"path": [""]})
            payload = handler._json.call_args[0][0]
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["path"], os.path.abspath("/"))
            self.assertIn("home", payload["dirs"])

    @unittest.skipUnless(IS_WINDOWS, "Windows 专属：盘符切换")
    def test_listdir_drive_root_lists_other_drives(self):
        """盘根目录（如 C:\\）时其他盘符应置顶，方便跨盘切换。"""
        from bedrock_server_manager.web.routes.get_routes import GetRoutesMixin as Handler

        with (
            patch("os.path.exists", side_effect=lambda p: p.lower() in ("c:\\", "d:\\")),
            patch("os.path.isdir", return_value=True),
            patch(
                "bedrock_server_manager.web.routes.get_routes.os.listdir",
                return_value=["Users", "Windows"],
            ),
        ):
            handler = self._create_mock_handler()
            Handler._get_listdir(handler, {"path": ["C:\\"]})
            payload = handler._json.call_args[0][0]
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["path"], "C:\\")
            # 其他盘符置顶 + 子目录
            self.assertEqual(payload["dirs"], ["D:\\", "Users", "Windows"])
            self.assertIn("C:\\", payload["drives"])
            self.assertIn("D:\\", payload["drives"])

    def test_listdir_normal_dir_no_drive_prefix(self):
        """非盘根目录不应把盘符混入子目录列表。"""
        from bedrock_server_manager.web.routes.get_routes import GetRoutesMixin as Handler

        with (
            patch("os.path.exists", side_effect=lambda p: p.lower() in ("c:\\", "d:\\")),
            patch("os.path.isdir", return_value=True),
            patch(
                "bedrock_server_manager.web.routes.get_routes.os.listdir",
                return_value=["sub1", "sub2"],
            ),
        ):
            handler = self._create_mock_handler()
            Handler._get_listdir(handler, {"path": ["C:\\Users"]})
            payload = handler._json.call_args[0][0]
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["dirs"], ["sub1", "sub2"])


if __name__ == "__main__":
    unittest.main()
