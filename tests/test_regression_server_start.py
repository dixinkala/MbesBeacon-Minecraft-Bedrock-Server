"""回归：/api/server/start 在启动失败时应返回 JSON，而不是因 UnboundLocalError 断连。"""

import unittest
from unittest import mock

from bedrock_server_manager.web.routes import server as server_routes


class FakeProc:
    def __init__(self, fail=False):
        self.server_dir = ""
        self.fail = fail
        self.started = 0

    def start(self):
        self.started += 1
        if self.fail:
            raise RuntimeError("启动失败")


class DummyHandler(server_routes.ServerRoutesMixin):
    def __init__(self):
        self.payloads = []

    def _json(self, data):
        self.payloads.append(data)


class TestServerStartRoute(unittest.TestCase):
    def setUp(self):
        self.handler = DummyHandler()

    def _call(self, proc, port_ok=True):
        with (
            mock.patch.object(server_routes, "installed", return_value=True),
            mock.patch.object(server_routes, "resolve_server_dir", return_value="/tmp/srv"),
            mock.patch.object(server_routes, "get_server_port", return_value=19132),
            mock.patch.object(server_routes, "check_port_in_use", return_value=not port_ok),
            mock.patch.object(server_routes, "get_server_proc", return_value=proc),
            mock.patch.object(server_routes, "_console", return_value=mock.MagicMock()),
            mock.patch.object(server_routes, "audit_log"),
        ):
            self.handler._route_server_start({})

    def test_start_failure_returns_json_error(self):
        self._call(FakeProc(fail=True))
        self.assertTrue(self.handler.payloads)
        self.assertFalse(self.handler.payloads[-1]["ok"])
        self.assertIn("启动失败", self.handler.payloads[-1]["error"])

    def test_start_failure_after_port_probe_error_still_returns_json(self):
        # 端口探测异常时曾把 safe_log_exception 绑成局部变量，导致外层 except 崩溃
        proc = FakeProc(fail=True)
        with (
            mock.patch.object(server_routes, "installed", return_value=True),
            mock.patch.object(server_routes, "resolve_server_dir", return_value="/tmp/srv"),
            mock.patch.object(server_routes, "get_server_port", side_effect=OSError("probe fail")),
            mock.patch.object(server_routes, "get_server_proc", return_value=proc),
            mock.patch.object(server_routes, "_console", return_value=mock.MagicMock()),
            mock.patch.object(server_routes, "audit_log"),
        ):
            self.handler._route_server_start({})
        self.assertFalse(self.handler.payloads[-1]["ok"])

    def test_start_success_returns_ok(self):
        proc = FakeProc()
        self._call(proc)
        self.assertTrue(self.handler.payloads[-1]["ok"])
        self.assertEqual(proc.started, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
