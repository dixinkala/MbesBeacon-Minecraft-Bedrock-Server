"""路由层覆盖率测试（A 组）。

针对以下模块补充单文件覆盖率：

- ``bedrock_server_manager/web/routes/post_extra.py``
- ``bedrock_server_manager/web/routes/server.py``
- ``bedrock_server_manager/web/routes/config.py``
- ``bedrock_server_manager/web/routes/misc.py``
- ``bedrock_server_manager/web/routes/commands.py``

所有用例都通过真实的 :class:`ThreadingHTTPServer`（端口 0）走 HTTP，使用
``AppContext.instance().api_token`` 作为 ``X-API-Token``。路由涉及的外部副作用
（启动/停止 BDS、下载安装、备份删除、退出程序、UPnP/穿透/Loader 等）全部用
``unittest.mock.patch`` 打桩，不会真正启动服务端、联网或退出进程。
"""

import base64
import copy
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from bedrock_server_manager import builtin_tunnel, levilamina, remote, tunnel, upnp
from bedrock_server_manager.app_context import AppContext
from bedrock_server_manager.constants import SERVER_EXE
from bedrock_server_manager.ratelimit import get_rate_limiter
from bedrock_server_manager.web import handler as web_handler
from bedrock_server_manager.web.routes import (
    config as config_routes,
    misc as misc_routes,
    post_extra as post_extra_routes,
    server as server_routes,
)

_MISSING_TOKEN = object()

_SERVER_PROPERTIES = (
    "server-name=CoverageServer\n"
    "gamemode=survival\n"
    "difficulty=normal\n"
    "max-players=10\n"
    "server-port=19132\n"
    "online-mode=true\n"
    "white-list=false\n"
)


def _wait_for(predicate, timeout=3.0, interval=0.01):
    """等待 predicate 为真（用于等待路由里后台线程执行打桩函数）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _RouteCoverageBase(unittest.TestCase):
    """启动真实 HTTP 服务并准备临时服务器目录。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_handler.Handler)
        cls.port = cls.httpd.server_port
        cls._server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls._server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._server_thread.join(timeout=10)

    # ---- 环境准备 ----

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bsm_routes_cov_a_")
        self.server_dir = os.path.join(self.tmpdir, "server")
        os.makedirs(os.path.join(self.server_dir, "worlds", "Bedrock level"), exist_ok=True)
        os.makedirs(os.path.join(self.server_dir, ".config_history"), exist_ok=True)
        with open(os.path.join(self.server_dir, "server.properties"), "w", encoding="utf-8") as f:
            f.write(_SERVER_PROPERTIES)
        with open(os.path.join(self.server_dir, SERVER_EXE), "wb") as f:
            f.write(b"MZ" + b"\x00" * 64)

        self.ctx = AppContext.instance()
        self._settings_backup = copy.deepcopy(dict(self.ctx.settings))
        self._console_backup = self.ctx._console
        self._install_backup = self.ctx._install_state

        self.ctx.settings.clear()
        self.ctx.settings.update(
            {
                "server_dir": self.server_dir,
                "ignore_ssl": False,
                "installed_version": "1.21.0.03",
                "server_dir_history": [self.server_dir],
                "server_versions": {os.path.normcase(os.path.abspath(self.server_dir)): "1.21.0.03"},
            }
        )
        self.ctx.console = MagicMock(name="console")
        self.install_state = MagicMock(name="install_state")
        self.install_state.busy = False
        self.install_state.done = False
        self.ctx.install_state = self.install_state

    def tearDown(self):
        get_rate_limiter().reset()
        self.ctx.settings.clear()
        self.ctx.settings.update(self._settings_backup)
        self.ctx.console = self._console_backup
        self.ctx.install_state = self._install_backup
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- HTTP 辅助 ----

    def request(self, method, path, payload=None, token=_MISSING_TOKEN, host=None):
        """发送一次真实 HTTP 请求，返回 (status, json_or_text)。"""
        get_rate_limiter().reset()
        headers = {"Host": host if host is not None else f"127.0.0.1:{self.port}"}
        actual_token = self.ctx.api_token if token is _MISSING_TOKEN else token
        if actual_token:
            headers["X-API-Token"] = actual_token
        body = None
        if method == "POST":
            body = json.dumps({} if payload is None else payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        finally:
            conn.close()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = raw.decode("utf-8", "replace")
        return status, data

    def post(self, path, payload=None, **kwargs):
        return self.request("POST", path, payload, **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def assert_ok(self, status, data):
        self.assertEqual(status, 200, data)
        self.assertIsInstance(data, dict, data)
        self.assertTrue(data.get("ok"), data)

    def assert_failed(self, status, data, code=200):
        self.assertEqual(status, code, data)
        self.assertIsInstance(data, dict, data)
        self.assertFalse(data.get("ok"), data)


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------


class TestConfigRoutes(_RouteCoverageBase):
    def test_history_without_server_dir(self):
        with patch.object(config_routes, "resolve_server_dir", return_value=""):
            status, data = self.post("/api/config/history", {})
        self.assert_failed(status, data)
        self.assertIn("未找到服务器", data["error"])

    def test_history_lists_real_backups(self):
        hist_dir = os.path.join(self.server_dir, ".config_history")
        with open(os.path.join(hist_dir, "server.properties.20240101_010101.bak"), "w", encoding="utf-8") as f:
            f.write("server-name=Old\n")
        status, data = self.post("/api/config/history", {})
        self.assert_ok(status, data)
        self.assertEqual(data["count"], 1)
        self.assertEqual(len(data["history"]), 1)
        self.assertEqual(data["history"][0]["name"], "server.properties.20240101_010101.bak")
        self.assertIn("path", data["history"][0])

    def test_rollback_without_server_dir(self):
        with patch.object(config_routes, "resolve_server_dir", return_value=""):
            status, data = self.post("/api/config/rollback", {"path": "x.bak"})
        self.assert_failed(status, data)
        self.assertIn("未找到服务器", data["error"])

    def test_rollback_missing_path(self):
        status, data = self.post("/api/config/rollback", {"path": "   "})
        self.assert_failed(status, data)
        self.assertIn("未指定备份路径", data["error"])

    def test_rollback_nonexistent_backup(self):
        status, data = self.post("/api/config/rollback", {"path": "server.properties.19990101_000000.bak"})
        self.assert_failed(status, data)
        self.assertIn("备份文件不存在", data["error"])

    def test_rollback_success_accepts_full_path(self):
        hist_dir = os.path.join(self.server_dir, ".config_history")
        backup_name = "server.properties.20240101_010101.bak"
        backup_path = os.path.join(hist_dir, backup_name)
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write("server-name=RolledBack\n")
        status, data = self.post("/api/config/rollback", {"path": backup_path})
        self.assert_ok(status, data)
        self.assertIn("已回滚", data["message"])
        with open(os.path.join(self.server_dir, "server.properties"), encoding="utf-8") as f:
            self.assertIn("RolledBack", f.read())


# ---------------------------------------------------------------------------
# server.py
# ---------------------------------------------------------------------------


class TestServerRoutes(_RouteCoverageBase):
    def test_start_not_installed(self):
        with (
            patch.object(server_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(server_routes, "installed", return_value=False),
        ):
            status, data = self.post("/api/server/start", {})
        self.assert_failed(status, data)
        self.assertIn("未找到服务器", data["error"])

    def test_start_port_in_use(self):
        proc = MagicMock(name="proc")
        with (
            patch.object(server_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(server_routes, "installed", return_value=True),
            patch.object(server_routes, "get_server_port", return_value=19132),
            patch.object(server_routes, "check_port_in_use", return_value=True),
            patch.object(server_routes, "get_server_proc", return_value=proc),
        ):
            status, data = self.post("/api/server/start", {})
        self.assert_failed(status, data)
        self.assertIn("19132", data["error"])
        self.assertIn("已被占用", data["error"])
        proc.start.assert_not_called()

    def test_start_success(self):
        proc = MagicMock(name="proc")
        with (
            patch.object(server_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(server_routes, "installed", return_value=True),
            patch.object(server_routes, "get_server_port", return_value=19132),
            patch.object(server_routes, "check_port_in_use", return_value=False),
            patch.object(server_routes, "get_server_proc", return_value=proc),
        ):
            status, data = self.post("/api/server/start", {})
        self.assert_ok(status, data)
        self.assertEqual(proc.server_dir, self.server_dir)
        proc.start.assert_called_once()

    def test_start_port_check_exception_still_starts(self):
        proc = MagicMock(name="proc")
        with (
            patch.object(server_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(server_routes, "installed", return_value=True),
            patch.object(server_routes, "get_server_port", side_effect=RuntimeError("bad port file")),
            patch.object(server_routes, "get_server_proc", return_value=proc),
        ):
            status, data = self.post("/api/server/start", {})
        self.assert_ok(status, data)
        proc.start.assert_called_once()

    def test_start_process_error(self):
        """启动进程抛异常时返回 ok=False。

        注意：这里同时让端口检测抛异常。若端口检测正常、仅 ``p.start()`` 抛异常，
        ``server.py`` 会因为 except 块里的局部 ``from ...app_logger import
        safe_log_exception`` 触发 ``UnboundLocalError``（已记录为缺陷，不在此断言）。
        """
        proc = MagicMock(name="proc")
        proc.start.side_effect = RuntimeError("cannot spawn")
        with (
            patch.object(server_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(server_routes, "installed", return_value=True),
            patch.object(server_routes, "get_server_port", side_effect=RuntimeError("bad properties")),
            patch.object(server_routes, "get_server_proc", return_value=proc),
        ):
            status, data = self.post("/api/server/start", {})
        self.assert_failed(status, data)
        self.assertIn("cannot spawn", data["error"])

    def test_stop_success(self):
        proc = MagicMock(name="proc")
        with patch.object(server_routes, "get_server_proc", return_value=proc):
            status, data = self.post("/api/server/stop", {})
        self.assert_ok(status, data)
        proc.stop.assert_called_once()

    def test_stop_error(self):
        proc = MagicMock(name="proc")
        proc.stop.side_effect = RuntimeError("stop failed")
        with patch.object(server_routes, "get_server_proc", return_value=proc):
            status, data = self.post("/api/server/stop", {})
        self.assert_failed(status, data)
        self.assertIn("stop failed", data["error"])

    def test_restart_failure(self):
        with patch(
            "bedrock_server_manager.web.app.AppContext.restart_server",
            return_value=(False, "未设置服务器目录"),
        ):
            status, data = self.post("/api/server/restart", {})
        self.assert_failed(status, data)
        self.assertIn("未设置服务器目录", data["error"])

    def test_restart_success(self):
        with patch(
            "bedrock_server_manager.web.app.AppContext.restart_server",
            return_value=(True, ""),
        ):
            status, data = self.post("/api/server/restart", {})
        self.assert_ok(status, data)


# ---------------------------------------------------------------------------
# misc.py
# ---------------------------------------------------------------------------


class TestMiscRoutes(_RouteCoverageBase):
    def test_cancel_sets_install_cancel_event(self):
        self.install_state.cancel = MagicMock(name="cancel_event")
        status, data = self.post("/api/cancel", {})
        self.assert_ok(status, data)
        self.install_state.cancel.set.assert_called_once()

    def test_exit_defers_shutdown_to_background_thread(self):
        with patch.object(web_handler.Handler, "_shutdown_later", return_value=None) as shutdown:
            status, data = self.post("/api/exit", {})
            self.assertTrue(_wait_for(lambda: shutdown.called, timeout=3.0))
        self.assert_ok(status, data)

    def test_theme_success(self):
        with patch.object(misc_routes, "save_theme", return_value=True) as save_theme:
            status, data = self.post(
                "/api/theme",
                {"theme": {"preset": "custom", "accent": "#123456", "mode": "light"}},
            )
        self.assert_ok(status, data)
        self.assertEqual(
            self.ctx.settings["theme"],
            {"preset": "custom", "accent": "#123456", "mode": "light"},
        )
        save_theme.assert_called_once()

    def test_theme_defaults_when_not_provided(self):
        with patch.object(misc_routes, "save_theme", return_value=True):
            status, data = self.post("/api/theme", {})
        self.assert_ok(status, data)
        self.assertEqual(self.ctx.settings["theme"]["preset"], misc_routes.DEFAULT_THEME["preset"])
        self.assertEqual(self.ctx.settings["theme"]["mode"], misc_routes.DEFAULT_THEME["mode"])

    def test_lang_invalid(self):
        with patch.object(misc_routes, "set_ui_lang", return_value=False) as set_lang:
            status, data = self.post("/api/lang", {"lang": "fr"})
        self.assert_failed(status, data)
        self.assertIn("zh", data["error"])
        set_lang.assert_not_called()

    def test_lang_valid(self):
        with patch.object(misc_routes, "set_ui_lang", return_value=True) as set_lang:
            status, data = self.post("/api/lang", {"lang": "zh"})
        self.assert_ok(status, data)
        self.assertEqual(data["lang"], "zh")
        set_lang.assert_called_once_with("zh")

    def test_audit_limit_conversion_and_clamp(self):
        with patch.object(misc_routes, "read_audit_log", return_value=[{"a": 1}]) as read_log:
            for payload, expected in (
                ({}, 200),
                ({"limit": "not-a-number"}, 200),
                ({"limit": 0}, 1),
                ({"limit": 999999}, 5000),
                ({"limit": 7}, 7),
            ):
                status, data = self.post("/api/audit", payload)
                self.assert_ok(status, data)
                self.assertEqual(data["count"], 1)
                read_log.assert_called_with(limit=expected)

    def test_scheduled_list(self):
        with patch.object(misc_routes, "load_scheduled_tasks", return_value=[{"id": "t1"}]) as load:
            status, data = self.get("/api/scheduled/list")
        self.assert_ok(status, data)
        self.assertEqual(data["count"], 1)
        load.assert_called_once()

    def test_scheduled_save_success(self):
        tasks = [{"id": "t1", "type": "backup"}]
        with patch.object(misc_routes, "save_scheduled_tasks", return_value=(True, "")) as save:
            status, data = self.post("/api/scheduled/save", {"tasks": tasks})
        self.assert_ok(status, data)
        self.assertEqual(data["tasks"], tasks)
        save.assert_called_once_with(tasks)

    def test_scheduled_save_error_empty_message(self):
        with patch.object(misc_routes, "save_scheduled_tasks", return_value=(False, "")):
            status, data = self.post("/api/scheduled/save", {"tasks": []})
        self.assert_failed(status, data)
        self.assertIn("保存失败", data["error"])

    def test_scheduled_save_error_with_message(self):
        with patch.object(misc_routes, "save_scheduled_tasks", return_value=(False, "任务不合法")):
            status, data = self.post("/api/scheduled/save", {"tasks": []})
        self.assert_failed(status, data)
        self.assertIn("任务不合法", data["error"])


# ---------------------------------------------------------------------------
# commands.py
# ---------------------------------------------------------------------------


class TestCommandsRoutes(_RouteCoverageBase):
    def test_list_all_commands(self):
        status, data = self.post("/api/commands/list", {})
        self.assert_ok(status, data)
        self.assertGreater(len(data["commands"]), 0)
        self.assertGreater(len(data["categories"]), 0)
        self.assertIn("name", data["commands"][0])

    def test_list_by_category(self):
        from bedrock_server_manager.commands import get_categories

        category = get_categories()[0]
        status, data = self.post("/api/commands/list", {"category": category})
        self.assert_ok(status, data)
        self.assertGreater(len(data["commands"]), 0)
        for cmd in data["commands"]:
            self.assertEqual(cmd["category"], category)

    def test_list_unknown_category_is_empty(self):
        status, data = self.post("/api/commands/list", {"category": "no-such-category"})
        self.assert_ok(status, data)
        self.assertEqual(data["commands"], [])

    def test_dangerous_commands(self):
        status, data = self.post("/api/commands/dangerous", {})
        self.assert_ok(status, data)
        self.assertIn("stop", data["commands"])
        self.assertEqual(data["commands"], sorted(data["commands"]))

    def test_autocomplete_with_prefix(self):
        status, data = self.post("/api/commands/autocomplete", {"prefix": "gi"})
        self.assert_ok(status, data)
        self.assertIn("suggestions", data)
        self.assertTrue(any(item["name"] == "give" for item in data["suggestions"]))

    def test_autocomplete_with_input_and_players(self):
        status, data = self.post(
            "/api/commands/autocomplete",
            {"input": "give ", "players": ["Steve"]},
        )
        self.assert_ok(status, data)
        self.assertIn("suggestions", data)
        self.assertIn("command_info", data)
        self.assertIn("is_complete", data)

    def test_help(self):
        status, data = self.post("/api/commands/help", {"command": "help"})
        self.assert_ok(status, data)
        self.assertIn("help", data["help"])

    def test_help_empty_command(self):
        status, data = self.post("/api/commands/help", {})
        self.assert_ok(status, data)
        self.assertIsInstance(data["help"], str)


# ---------------------------------------------------------------------------
# post_extra.py：更新 / 选择目录 / 安装 / 删除 / 配置 / 远程 / 软件更新
# ---------------------------------------------------------------------------


class TestPostExtraCoreRoutes(_RouteCoverageBase):
    def test_latest_success(self):
        info = {"version": "1.21.1.01", "url": "https://example.invalid/bds.zip"}
        with patch.object(post_extra_routes, "get_latest_server_info", return_value=info) as latest:
            status, data = self.post("/api/latest", {})
        self.assert_ok(status, data)
        self.assertEqual(data["info"], info)
        self.assertEqual(self.ctx.settings["_latest"], info)
        latest.assert_called_once()

    def test_latest_error(self):
        with patch.object(post_extra_routes, "get_latest_server_info", side_effect=RuntimeError("network down")):
            status, data = self.post("/api/latest", {})
        self.assert_failed(status, data)
        self.assertIn("network down", data["error"])

    def test_servers_select_error(self):
        with patch.object(post_extra_routes, "select_server_dir", return_value=(False, "目录不存在", None)):
            status, data = self.post("/api/servers/select", {"dir": "Z:/nope"})
        self.assert_failed(status, data)
        self.assertEqual(data["error"], "目录不存在")

    def test_servers_select_success(self):
        info = {"dir": self.server_dir, "installed_version": "1.21.0.03", "detected_servers": []}
        with patch.object(post_extra_routes, "select_server_dir", return_value=(True, "", info)) as select:
            status, data = self.post("/api/servers/select", {"dir": self.server_dir})
        self.assert_ok(status, data)
        self.assertEqual(data["dir"], self.server_dir)
        select.assert_called_once_with(self.server_dir)

    def test_setdir_error(self):
        with patch.object(post_extra_routes, "select_server_dir", return_value=(False, "bad dir", None)):
            status, data = self.post("/api/setdir", {"dir": "Z:/nope"})
        self.assert_failed(status, data)
        self.assertEqual(data["error"], "bad dir")

    def test_setdir_success(self):
        info = {"dir": self.server_dir, "installed_version": "", "detected_servers": []}
        with patch.object(post_extra_routes, "select_server_dir", return_value=(True, "", info)):
            status, data = self.post("/api/setdir", {"dir": self.server_dir})
        self.assert_ok(status, data)

    def test_install_busy(self):
        self.install_state.busy = True
        try:
            status, data = self.post("/api/install", {"dir": self.server_dir})
        finally:
            self.install_state.busy = False
        self.assert_failed(status, data)
        self.assertIn("已有安装任务", data["error"])

    def test_install_missing_dir(self):
        status, data = self.post("/api/install", {"dir": "   "})
        self.assert_failed(status, data)
        self.assertIn("请先选择安装目录", data["error"])

    def test_install_invalid_custom_url(self):
        with patch.object(post_extra_routes, "validate_custom_url", return_value=(False, "host 不允许", False)):
            status, data = self.post(
                "/api/install",
                {"dir": self.server_dir, "custom_url": "http://evil.example/bds.zip"},
            )
        self.assert_failed(status, data)
        self.assertIn("自定义下载地址无效", data["error"])

    def test_install_success_with_invalid_source_index(self):
        with patch.object(post_extra_routes, "do_install") as install_mock:
            status, data = self.post(
                "/api/install",
                {
                    "dir": self.server_dir,
                    "version": "1.21.0.03",
                    "autostart": False,
                    "source_index": "not-an-int",
                },
            )
            self.assertTrue(_wait_for(lambda: install_mock.called))
        self.assert_ok(status, data)
        self.assertEqual(self.ctx.settings["server_dir"], self.server_dir)
        args = install_mock.call_args.args
        self.assertEqual(args[0], self.server_dir)
        self.assertEqual(args[1], "1.21.0.03")
        self.assertFalse(args[2])
        self.assertEqual(args[4], 0)

    def test_install_custom_url_success(self):
        with (
            patch.object(post_extra_routes, "validate_custom_url", return_value=(True, "", False)),
            patch.object(post_extra_routes, "do_install") as install_mock,
        ):
            status, data = self.post(
                "/api/install",
                {"dir": self.server_dir, "custom_url": "https://example.invalid/bds.zip"},
            )
            self.assertTrue(_wait_for(lambda: install_mock.called))
        self.assert_ok(status, data)
        self.assertEqual(install_mock.call_args.args[3], "https://example.invalid/bds.zip")

    def test_update_busy(self):
        self.install_state.busy = True
        try:
            status, data = self.post("/api/update", {})
        finally:
            self.install_state.busy = False
        self.assert_failed(status, data)
        self.assertIn("已有安装/更新任务", data["error"])

    def test_update_not_installed(self):
        with (
            patch.object(post_extra_routes, "resolve_server_dir", return_value=""),
            patch.object(post_extra_routes, "installed", return_value=False),
        ):
            status, data = self.post("/api/update", {})
        self.assert_failed(status, data)
        self.assertIn("尚未安装服务器", data["error"])

    def test_update_already_latest(self):
        with (
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(post_extra_routes, "installed", return_value=True),
        ):
            status, data = self.post("/api/update", {"version": "1.21.0.03"})
        self.assert_ok(status, data)
        self.assertTrue(data["already_latest"])
        self.assertEqual(data["target"], "1.21.0.03")

    def test_update_latest_fetch_error(self):
        with (
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(post_extra_routes, "installed", return_value=True),
            patch.object(post_extra_routes, "get_latest_server_info", side_effect=RuntimeError("dns fail")),
        ):
            status, data = self.post("/api/update", {})
        self.assert_failed(status, data)
        self.assertIn("获取最新版本失败", data["error"])

    def test_update_success_server_not_running(self):
        with (
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(post_extra_routes, "installed", return_value=True),
            patch.object(post_extra_routes, "get_latest_server_info", return_value={"version": "1.21.1.01"}),
            patch.object(post_extra_routes, "server_running", return_value=False),
            patch.object(post_extra_routes, "backup_worlds") as backup,
            patch.object(post_extra_routes, "do_install") as install_mock,
        ):
            status, data = self.post("/api/update", {})
            self.assertTrue(_wait_for(lambda: install_mock.called))
        self.assert_ok(status, data)
        self.assertFalse(data["was_running"])
        self.assertEqual(data["target"], "1.21.1.01")
        backup.assert_called_once_with(self.server_dir)
        self.assertEqual(install_mock.call_args.args[0], self.server_dir)
        self.assertEqual(install_mock.call_args.args[1], "1.21.1.01")

    def test_update_success_server_running_is_stopped_first(self):
        proc = MagicMock(name="proc")
        with (
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(post_extra_routes, "installed", return_value=True),
            patch.object(post_extra_routes, "server_running", return_value=True),
            patch.object(post_extra_routes, "get_server_proc", return_value=proc),
            patch.object(post_extra_routes, "backup_worlds"),
            patch.object(post_extra_routes, "do_install") as install_mock,
        ):
            status, data = self.post("/api/update", {"version": "1.21.2.02"})
            self.assertTrue(_wait_for(lambda: install_mock.called))
        self.assert_ok(status, data)
        self.assertTrue(data["was_running"])
        proc.stop.assert_called_once_with(wait=10)

    def test_config_success(self):
        status, data = self.post("/api/config", {"data": {"max-players": "20"}})
        self.assert_ok(status, data)
        with open(os.path.join(self.server_dir, "server.properties"), encoding="utf-8") as f:
            self.assertIn("max-players=20", f.read())

    def test_config_validation_failure_with_field(self):
        status, data = self.post("/api/config", {"data": {"max-players": "999"}})
        self.assert_failed(status, data)
        self.assertEqual(data.get("failed_field"), "max-players")
        self.assertIn("配置校验失败", data["error"])

    def test_config_validation_failure_without_field(self):
        status, data = self.post("/api/config", {"data": ["not", "a", "dict"]})
        self.assert_failed(status, data)
        self.assertNotIn("failed_field", data)
        self.assertIn("配置数据格式错误", data["error"])

    def test_server_delete_needs_confirm(self):
        for payload in (
            {"dir": self.server_dir},
            {"dir": self.server_dir, "confirm_delete": "WRONG"},
        ):
            status, data = self.post("/api/server/delete", payload)
            self.assert_failed(status, data)
            self.assertTrue(data.get("need_confirm"))
            self.assertEqual(data.get("confirm_key"), "confirm_delete")
            self.assertEqual(data.get("confirm_value"), "DELETE")

    def test_server_delete_success_clears_active_dir(self):
        versions_map = {os.path.normcase(os.path.abspath(self.server_dir)): "1.21.0.03"}
        self.ctx.settings["server_versions"] = dict(versions_map)
        self.ctx.settings["server_dir_history"] = [self.server_dir, os.path.join(self.tmpdir, "other")]
        with (
            patch.object(post_extra_routes, "backup_worlds") as backup,
            patch.object(post_extra_routes, "delete_server") as delete,
            patch.object(post_extra_routes, "save_settings") as save_settings,
        ):
            status, data = self.post(
                "/api/server/delete",
                {"dir": self.server_dir, "confirm_delete": "DELETE", "mode": "full"},
            )
        self.assert_ok(status, data)
        backup.assert_called_once_with(self.server_dir)
        delete.assert_called_once_with(self.server_dir, "full")
        save_settings.assert_called_once()
        self.assertEqual(self.ctx.settings["server_dir"], "")
        self.assertEqual(self.ctx.settings["installed_version"], "")
        self.assertEqual(self.ctx.settings["server_versions"], {})
        self.assertEqual(self.ctx.settings["server_dir_history"], [os.path.join(self.tmpdir, "other")])

    def test_server_delete_failure(self):
        with (
            patch.object(post_extra_routes, "backup_worlds"),
            patch.object(post_extra_routes, "delete_server", side_effect=OSError("file busy")),
            patch.object(post_extra_routes, "save_settings"),
        ):
            status, data = self.post(
                "/api/server/delete",
                {"dir": self.server_dir, "confirm_delete": "DELETE"},
            )
        self.assert_failed(status, data)
        self.assertIn("file busy", data["error"])

    def test_app_update_check_success(self):
        result = {"ok": True, "has_update": True, "latest_version": "1.0.27"}
        with patch.object(post_extra_routes, "check_app_update", return_value=result) as check:
            status, data = self.post("/api/app/update/check", {"force": True})
        self.assert_ok(status, data)
        self.assertTrue(data["has_update"])
        check.assert_called_once_with(force=True, ignore_ssl=False)

    def test_app_update_check_error(self):
        with patch.object(post_extra_routes, "check_app_update", side_effect=RuntimeError("github down")):
            status, data = self.post("/api/app/update/check", {})
        self.assert_failed(status, data)
        self.assertFalse(data["has_update"])
        self.assertIn("github down", data["error"])

    def test_remote_config_forbidden_for_non_loopback(self):
        with patch("bedrock_server_manager.remote.is_loopback_value", return_value=False):
            status, data = self.post("/api/remote/config", {"enabled": True})
        self.assert_failed(status, data, code=403)
        self.assertIn("只能在运行本程序的机器上修改", data["error"])

    def test_remote_config_password_type_error(self):
        status, data = self.post("/api/remote/config", {"password": 12345})
        self.assert_failed(status, data, code=400)
        self.assertIn("密码格式不正确", data["error"])

    def test_remote_config_hosts_type_error(self):
        status, data = self.post("/api/remote/config", {"hosts": "not-a-list"})
        self.assert_failed(status, data, code=400)
        self.assertIn("hosts 必须是数组", data["error"])

    def test_remote_config_value_error(self):
        with patch.object(remote, "set_config", side_effect=ValueError("密码至少 8 位")):
            status, data = self.post("/api/remote/config", {"enabled": True, "password": "x"})
        self.assert_failed(status, data, code=400)
        self.assertIn("密码至少 8 位", data["error"])

    def test_remote_config_success_restart_required(self):
        with patch.object(
            remote,
            "set_config",
            return_value={"enabled": True, "restart_required": True},
        ) as set_config:
            status, data = self.post(
                "/api/remote/config",
                {"enabled": True, "password": "goodpass123", "hosts": ["my.domain"]},
            )
        self.assert_ok(status, data)
        self.assertTrue(data["restart_required"])
        self.assertIn("重启程序后生效", data["message"])
        set_config.assert_called_once_with(True, password="goodpass123", hosts=["my.domain"])

    def test_remote_config_success_no_restart(self):
        with patch.object(
            remote,
            "set_config",
            return_value={"enabled": False, "restart_required": False},
        ):
            status, data = self.post("/api/remote/config", {"enabled": False})
        self.assert_ok(status, data)
        self.assertFalse(data["restart_required"])
        self.assertEqual(data["message"], "设置已保存")


# ---------------------------------------------------------------------------
# post_extra.py：玩家管理
# ---------------------------------------------------------------------------


class TestPostExtraPlayerRoutes(_RouteCoverageBase):
    def test_kick_invalid_name(self):
        status, data = self.post("/api/players/kick", {"name": "bad name!"})
        self.assert_failed(status, data)
        self.assertIn("玩家名", data["error"])

    def test_kick_invalid_reason_newline(self):
        status, data = self.post("/api/players/kick", {"name": "Steve", "reason": "x\nstop"})
        self.assert_failed(status, data)
        self.assertIn("换行", data["error"])

    def test_kick_reason_too_long(self):
        status, data = self.post("/api/players/kick", {"name": "Steve", "reason": "x" * 201})
        self.assert_failed(status, data)
        self.assertIn("长度", data["error"])

    def test_kick_server_not_running(self):
        with patch.object(post_extra_routes, "server_running", return_value=False):
            status, data = self.post("/api/players/kick", {"name": "Steve"})
        self.assert_failed(status, data)
        self.assertIn("服务器未运行", data["error"])

    def test_kick_success_without_reason(self):
        proc = MagicMock(name="proc")
        with (
            patch.object(post_extra_routes, "server_running", return_value=True),
            patch.object(post_extra_routes, "get_server_proc", return_value=proc),
        ):
            status, data = self.post("/api/players/kick", {"name": "Steve"})
        self.assert_ok(status, data)
        proc.send.assert_called_once_with("kick Steve")

    def test_kick_success_with_reason(self):
        proc = MagicMock(name="proc")
        with (
            patch.object(post_extra_routes, "server_running", return_value=True),
            patch.object(post_extra_routes, "get_server_proc", return_value=proc),
        ):
            status, data = self.post("/api/players/kick", {"name": "Steve", "reason": "afk"})
        self.assert_ok(status, data)
        proc.send.assert_called_once_with("kick Steve afk")

    def test_op_invalid_name_and_not_running_and_success(self):
        status, data = self.post("/api/players/op", {"name": "bad!"})
        self.assert_failed(status, data)
        with patch.object(post_extra_routes, "server_running", return_value=False):
            status, data = self.post("/api/players/op", {"name": "Steve"})
        self.assert_failed(status, data)
        proc = MagicMock(name="proc")
        with (
            patch.object(post_extra_routes, "server_running", return_value=True),
            patch.object(post_extra_routes, "get_server_proc", return_value=proc),
        ):
            status, data = self.post("/api/players/op", {"name": "Steve"})
        self.assert_ok(status, data)
        proc.send.assert_called_once_with("op Steve")

    def test_deop_not_running_and_success(self):
        with patch.object(post_extra_routes, "server_running", return_value=False):
            status, data = self.post("/api/players/deop", {"name": "Steve"})
        self.assert_failed(status, data)
        proc = MagicMock(name="proc")
        with (
            patch.object(post_extra_routes, "server_running", return_value=True),
            patch.object(post_extra_routes, "get_server_proc", return_value=proc),
        ):
            status, data = self.post("/api/players/deop", {"name": "Steve"})
        self.assert_ok(status, data)
        proc.send.assert_called_once_with("deop Steve")

    def test_deop_invalid_name(self):
        status, data = self.post("/api/players/deop", {"name": "bad name!"})
        self.assert_failed(status, data)
        self.assertIn("玩家名", data["error"])

    def test_permission_invalid_level(self):
        status, data = self.post("/api/players/permission", {"name": "Steve", "level": "admin"})
        self.assert_failed(status, data)
        self.assertIn("权限级别无效", data["error"])

    def test_permission_invalid_name(self):
        status, data = self.post("/api/players/permission", {"name": "bad name", "level": "member"})
        self.assert_failed(status, data)
        self.assertIn("玩家名", data["error"])

    def test_permission_not_running_and_success(self):
        with patch.object(post_extra_routes, "server_running", return_value=False):
            status, data = self.post("/api/players/permission", {"name": "Steve", "level": "operator"})
        self.assert_failed(status, data)
        proc = MagicMock(name="proc")
        with (
            patch.object(post_extra_routes, "server_running", return_value=True),
            patch.object(post_extra_routes, "get_server_proc", return_value=proc),
        ):
            status, data = self.post("/api/players/permission", {"name": "Steve", "level": "visitor"})
        self.assert_ok(status, data)
        proc.send.assert_called_once_with("permission set Steve visitor")

    def test_ban_invalid_name_and_reason(self):
        status, data = self.post("/api/players/ban", {"name": "bad name"})
        self.assert_failed(status, data)
        status, data = self.post("/api/players/ban", {"name": "Steve", "reason": "a\nb"})
        self.assert_failed(status, data)
        self.assertIn("换行", data["error"])

    def test_ban_not_running_and_success(self):
        with patch.object(post_extra_routes, "server_running", return_value=False):
            status, data = self.post("/api/players/ban", {"name": "Steve"})
        self.assert_failed(status, data)
        proc = MagicMock(name="proc")
        with (
            patch.object(post_extra_routes, "server_running", return_value=True),
            patch.object(post_extra_routes, "get_server_proc", return_value=proc),
        ):
            status, data = self.post("/api/players/ban", {"name": "Steve", "reason": "grief"})
        self.assert_ok(status, data)
        proc.send.assert_called_once_with("ban Steve grief")

    def test_pardon_not_running_and_success(self):
        with patch.object(post_extra_routes, "server_running", return_value=False):
            status, data = self.post("/api/players/pardon", {"name": "Steve"})
        self.assert_failed(status, data)
        proc = MagicMock(name="proc")
        with (
            patch.object(post_extra_routes, "server_running", return_value=True),
            patch.object(post_extra_routes, "get_server_proc", return_value=proc),
        ):
            status, data = self.post("/api/players/pardon", {"name": "Steve"})
        self.assert_ok(status, data)
        proc.send.assert_called_once_with("pardon Steve")

    def test_pardon_invalid_name(self):
        status, data = self.post("/api/players/pardon", {"name": ""})
        self.assert_failed(status, data)
        self.assertIn("玩家名", data["error"])

    def test_allowlist_add_invalid_name(self):
        status, data = self.post("/api/players/allowlist/add", {"name": "bad name!"})
        self.assert_failed(status, data)

    def test_allowlist_add_no_server_dir(self):
        with patch.object(post_extra_routes, "resolve_server_dir", return_value=""):
            status, data = self.post("/api/players/allowlist/add", {"name": "Steve"})
        self.assert_failed(status, data)
        self.assertIn("未设置服务器目录", data["error"])

    def test_allowlist_add_failure_and_success(self):
        with patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir):
            with patch.object(post_extra_routes, "add_allowlist", return_value=False):
                status, data = self.post("/api/players/allowlist/add", {"name": "Steve"})
            self.assert_failed(status, data)
            self.assertIn("添加失败", data["error"])
            with patch.object(post_extra_routes, "add_allowlist", return_value=True) as add:
                status, data = self.post("/api/players/allowlist/add", {"name": "Steve"})
        self.assert_ok(status, data)
        add.assert_called_once_with(self.server_dir, "Steve")

    def test_allowlist_remove_invalid_name(self):
        status, data = self.post("/api/players/allowlist/remove", {"name": "bad name!"})
        self.assert_failed(status, data)
        self.assertIn("玩家名", data["error"])

    def test_allowlist_remove_no_server_dir(self):
        with patch.object(post_extra_routes, "resolve_server_dir", return_value=""):
            status, data = self.post("/api/players/allowlist/remove", {"name": "Steve"})
        self.assert_failed(status, data)
        self.assertIn("未设置服务器目录", data["error"])

    def test_allowlist_remove_failure_and_success(self):
        with patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir):
            with patch.object(post_extra_routes, "remove_allowlist", return_value=False):
                status, data = self.post("/api/players/allowlist/remove", {"name": "Steve"})
            self.assert_failed(status, data)
            self.assertIn("移除失败", data["error"])
            with patch.object(post_extra_routes, "remove_allowlist", return_value=True) as remove:
                status, data = self.post("/api/players/allowlist/remove", {"name": "Steve"})
        self.assert_ok(status, data)
        remove.assert_called_once_with(self.server_dir, "Steve")


# ---------------------------------------------------------------------------
# post_extra.py：内网穿透（含 403 守卫）
# ---------------------------------------------------------------------------

_TUNNEL_GUARDED_ENDPOINTS = [
    "/api/tunnel/upnp/open",
    "/api/tunnel/upnp/close",
    "/api/tunnel/config",
    "/api/tunnel/start",
    "/api/tunnel/stop",
    "/api/tunnel/builtin/install",
    "/api/tunnel/playit/claim",
    "/api/tunnel/playit/start",
    "/api/tunnel/playit/stop",
    "/api/tunnel/frp/config",
    "/api/tunnel/frp/start",
    "/api/tunnel/frp/stop",
]

_LEVILAMINA_GUARDED_ENDPOINTS = [
    "/api/levilamina/lip/install",
    "/api/levilamina/enable",
    "/api/levilamina/disable",
    "/api/levilamina/mods/install",
    "/api/levilamina/mods/toggle",
    "/api/levilamina/mods/delete",
    "/api/levilamina/lip/install_pkg",
    "/api/levilamina/lip/uninstall_pkg",
]


class TestPostExtraTunnelRoutes(_RouteCoverageBase):
    def test_guarded_endpoints_reject_non_loopback(self):
        with patch("bedrock_server_manager.remote.is_loopback_value", return_value=False):
            for endpoint in _TUNNEL_GUARDED_ENDPOINTS + _LEVILAMINA_GUARDED_ENDPOINTS + ["/api/instances/add"]:
                status, data = self.post(endpoint, {})
                self.assertEqual(status, 403, (endpoint, data))
                self.assertFalse(data.get("ok"), (endpoint, data))

    def test_upnp_open_success(self):
        with (
            patch.object(remote, "primary_lan_ip", return_value="192.168.1.9"),
            patch.object(upnp, "add_mapping", return_value={"mapped": True}) as add_mapping,
            patch.object(upnp, "mapping_state", return_value={"active": True}),
        ):
            status, data = self.post("/api/tunnel/upnp/open", {"port": "19132"})
        self.assert_ok(status, data)
        self.assertEqual(data["port"], 19132)
        self.assertEqual(data["internal_ip"], "192.168.1.9")
        self.assertTrue(data["mapped"])
        self.assertEqual(data["state"], {"active": True})
        add_mapping.assert_called_once_with(19132, "192.168.1.9", protocol="UDP")

    def test_upnp_open_invalid_port(self):
        status, data = self.post("/api/tunnel/upnp/open", {"port": "70000"})
        self.assert_failed(status, data, code=400)
        self.assertIn("端口不合法", data["error"])

    def test_upnp_open_generic_error(self):
        with (
            patch.object(remote, "primary_lan_ip", return_value="192.168.1.9"),
            patch.object(upnp, "add_mapping", side_effect=RuntimeError("no gateway")),
        ):
            status, data = self.post("/api/tunnel/upnp/open", {"port": 19132})
        self.assert_failed(status, data, code=400)
        self.assertIn("no gateway", data["error"])

    def test_upnp_open_uses_configured_server_port(self):
        with (
            patch("bedrock_server_manager.server.get_server_port", return_value=19133),
            patch.object(remote, "primary_lan_ip", return_value="10.0.0.5"),
            patch.object(upnp, "add_mapping", return_value={}) as add_mapping,
            patch.object(upnp, "mapping_state", return_value={}),
        ):
            status, data = self.post("/api/tunnel/upnp/open", {"internal_ip": "10.0.0.6"})
        self.assert_ok(status, data)
        self.assertEqual(data["port"], 19133)
        add_mapping.assert_called_once_with(19133, "10.0.0.6", protocol="UDP")

    def test_upnp_close_success(self):
        with patch.object(upnp, "delete_mapping") as delete_mapping:
            status, data = self.post("/api/tunnel/upnp/close", {"port": 19132})
        self.assert_ok(status, data)
        self.assertEqual(data["port"], 19132)
        delete_mapping.assert_called_once_with(19132, protocol="UDP")

    def test_upnp_close_invalid_port(self):
        status, data = self.post("/api/tunnel/upnp/close", {"port": "0"})
        self.assert_failed(status, data, code=400)
        self.assertIn("端口不合法", data["error"])

    def test_upnp_close_generic_error(self):
        with patch.object(upnp, "delete_mapping", side_effect=RuntimeError("boom")):
            status, data = self.post("/api/tunnel/upnp/close", {"port": 19132})
        self.assert_failed(status, data, code=400)
        self.assertIn("boom", data["error"])

    def test_tunnel_config_success(self):
        cfg = {"provider": "playit", "command": "", "address_regex": "", "public_address": "", "enabled": True}
        with patch.object(tunnel, "save_config", return_value=cfg) as save_config:
            status, data = self.post(
                "/api/tunnel/config",
                {"provider": "playit", "command": "playit", "enabled": True},
            )
        self.assert_ok(status, data)
        self.assertEqual(data["config"], cfg)
        save_config.assert_called_once()

    def test_tunnel_config_error(self):
        with patch.object(tunnel, "save_config", side_effect=tunnel.TunnelError("bad provider")):
            status, data = self.post("/api/tunnel/config", {"provider": "nope"})
        self.assert_failed(status, data, code=400)
        self.assertIn("bad provider", data["error"])

    def test_tunnel_start_success(self):
        status_payload = {"provider": "playit", "running": True, "address": "x.example"}
        with patch.object(tunnel.manager, "start", return_value=status_payload) as start:
            status, data = self.post("/api/tunnel/start", {"port": 19132})
        self.assert_ok(status, data)
        self.assertEqual(data["port"], 19132)
        self.assertEqual(data["status"], status_payload)
        start.assert_called_once_with(19132)

    def test_tunnel_start_invalid_port_falls_back_to_server_port(self):
        with (
            patch("bedrock_server_manager.server.get_server_port", return_value=19132),
            patch.object(tunnel.manager, "start", return_value={"running": True}) as start,
        ):
            status, data = self.post("/api/tunnel/start", {"port": "abc"})
        self.assert_ok(status, data)
        self.assertEqual(data["port"], 19132)
        start.assert_called_once_with(19132)

    def test_tunnel_start_tunnel_error(self):
        with patch.object(tunnel.manager, "start", side_effect=tunnel.TunnelError("not installed")):
            status, data = self.post("/api/tunnel/start", {"port": 19132})
        self.assert_failed(status, data, code=400)
        self.assertIn("not installed", data["error"])

    def test_tunnel_start_port_out_of_range(self):
        status, data = self.post("/api/tunnel/start", {"port": 99999})
        self.assert_failed(status, data, code=400)
        self.assertIn("端口不合法", data["error"])

    def test_tunnel_stop_success(self):
        with patch.object(tunnel.manager, "stop", return_value={"running": False}) as stop:
            status, data = self.post("/api/tunnel/stop", {})
        self.assert_ok(status, data)
        self.assertEqual(data["status"], {"running": False})
        stop.assert_called_once()

    def test_builtin_install_playit_success(self):
        with patch.object(builtin_tunnel, "install_playit", return_value={"version": "1.0.0"}) as install:
            status, data = self.post("/api/tunnel/builtin/install", {"tool": "playit"})
        self.assert_ok(status, data)
        self.assertEqual(data["tool"], "playit")
        self.assertEqual(data["version"], "1.0.0")
        install.assert_called_once()

    def test_builtin_install_frp_success(self):
        with patch.object(builtin_tunnel, "install_frp", return_value={"version": "0.60"}) as install:
            status, data = self.post("/api/tunnel/builtin/install", {"tool": "frp"})
        self.assert_ok(status, data)
        self.assertEqual(data["tool"], "frp")
        install.assert_called_once()

    def test_builtin_install_invalid_tool(self):
        status, data = self.post("/api/tunnel/builtin/install", {"tool": "other"})
        self.assert_failed(status, data, code=400)
        self.assertIn("tool 只能是", data["error"])

    def test_builtin_install_tunnel_error(self):
        with patch.object(builtin_tunnel, "install_playit", side_effect=builtin_tunnel.BuiltinTunnelError("net")):
            status, data = self.post("/api/tunnel/builtin/install", {"tool": "playit"})
        self.assert_failed(status, data, code=400)
        self.assertIn("net", data["error"])

    def test_builtin_install_generic_error(self):
        with patch.object(builtin_tunnel, "install_frp", side_effect=RuntimeError("disk full")):
            status, data = self.post("/api/tunnel/builtin/install", {"tool": "frp"})
        self.assert_failed(status, data, code=500)
        self.assertIn("disk full", data["error"])

    def test_playit_claim_start(self):
        with patch.object(builtin_tunnel.playit, "claim_start", return_value={"url": "https://playit.gg/x"}):
            status, data = self.post("/api/tunnel/playit/claim", {"action": "start"})
        self.assert_ok(status, data)
        self.assertEqual(data["action"], "start")
        self.assertEqual(data["url"], "https://playit.gg/x")

    def test_playit_claim_check(self):
        with patch.object(builtin_tunnel.playit, "claim_check", return_value={"claimed": True}):
            status, data = self.post("/api/tunnel/playit/claim", {"action": "check"})
        self.assert_ok(status, data)
        self.assertEqual(data["action"], "check")
        self.assertTrue(data["claimed"])

    def test_playit_claim_invalid_action(self):
        status, data = self.post("/api/tunnel/playit/claim", {"action": "bogus"})
        self.assert_failed(status, data, code=400)
        self.assertIn("action 只能是", data["error"])

    def test_playit_claim_error(self):
        with patch.object(builtin_tunnel.playit, "claim_start", side_effect=builtin_tunnel.BuiltinTunnelError("no")):
            status, data = self.post("/api/tunnel/playit/claim", {"action": "start"})
        self.assert_failed(status, data, code=400)

    def test_playit_start_success(self):
        payload = {"running": True, "address": "abc.playit.gg"}
        with patch.object(builtin_tunnel.playit, "start", return_value=payload) as start:
            status, data = self.post("/api/tunnel/playit/start", {})
        self.assert_ok(status, data)
        self.assertEqual(data["status"], payload)
        start.assert_called_once()

    def test_playit_start_error(self):
        with patch.object(
            builtin_tunnel.playit, "start", side_effect=builtin_tunnel.BuiltinTunnelError("not installed")
        ):
            status, data = self.post("/api/tunnel/playit/start", {})
        self.assert_failed(status, data, code=400)

    def test_playit_stop_success(self):
        with patch.object(builtin_tunnel.playit, "stop", return_value={"running": False}) as stop:
            status, data = self.post("/api/tunnel/playit/stop", {})
        self.assert_ok(status, data)
        self.assertEqual(data["status"], {"running": False})
        stop.assert_called_once()

    def test_frp_config_success(self):
        cfg = {"server_addr": "frp.example", "remote_port": 25565}
        with patch.object(builtin_tunnel, "save_frp_config", return_value=cfg) as save_config:
            status, data = self.post(
                "/api/tunnel/frp/config",
                {"server_addr": "frp.example", "server_port": 7000, "token": "t", "remote_port": 25565},
            )
        self.assert_ok(status, data)
        self.assertEqual(data["config"], cfg)
        save_config.assert_called_once()

    def test_frp_config_error(self):
        with patch.object(builtin_tunnel, "save_frp_config", side_effect=builtin_tunnel.BuiltinTunnelError("bad")):
            status, data = self.post("/api/tunnel/frp/config", {"server_addr": ""})
        self.assert_failed(status, data, code=400)

    def test_frp_start_success(self):
        with patch.object(builtin_tunnel.frp, "start", return_value={"running": True}) as start:
            status, data = self.post("/api/tunnel/frp/start", {})
        self.assert_ok(status, data)
        self.assertEqual(data["status"], {"running": True})
        start.assert_called_once()

    def test_frp_start_error(self):
        with patch.object(builtin_tunnel.frp, "start", side_effect=builtin_tunnel.BuiltinTunnelError("boom")):
            status, data = self.post("/api/tunnel/frp/start", {})
        self.assert_failed(status, data, code=400)

    def test_frp_stop_success(self):
        with patch.object(builtin_tunnel.frp, "stop", return_value={"running": False}) as stop:
            status, data = self.post("/api/tunnel/frp/stop", {})
        self.assert_ok(status, data)
        stop.assert_called_once()


# ---------------------------------------------------------------------------
# post_extra.py：LeviLamina
# ---------------------------------------------------------------------------


class TestPostExtraLeviLaminaRoutes(_RouteCoverageBase):
    def test_lip_install_success(self):
        with patch.object(levilamina, "install_lip", return_value={"version": "0.25.0"}) as install:
            status, data = self.post("/api/levilamina/lip/install", {})
        self.assert_ok(status, data)
        self.assertEqual(data["version"], "0.25.0")
        install.assert_called_once()

    def test_lip_install_error(self):
        with patch.object(levilamina, "install_lip", side_effect=levilamina.LeviLaminaError("download fail")):
            status, data = self.post("/api/levilamina/lip/install", {})
        self.assert_failed(status, data, code=400)

    def test_enable_requires_confirm(self):
        status, data = self.post("/api/levilamina/enable", {})
        self.assert_failed(status, data, code=400)
        self.assertIn("请先确认", data["error"])

    def test_enable_requires_stopped_server(self):
        with (
            patch("bedrock_server_manager.server.server_running", return_value=True),
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
        ):
            status, data = self.post("/api/levilamina/enable", {"confirm": True})
        self.assert_failed(status, data, code=400)
        self.assertIn("请先停止服务器", data["error"])

    def test_enable_requires_server_dir(self):
        with (
            patch("bedrock_server_manager.server.server_running", return_value=False),
            patch.object(post_extra_routes, "resolve_server_dir", return_value=""),
        ):
            status, data = self.post("/api/levilamina/enable", {"confirm": True})
        self.assert_failed(status, data, code=400)
        self.assertIn("未设置服务器目录", data["error"])

    def test_enable_success(self):
        with (
            patch("bedrock_server_manager.server.server_running", return_value=False),
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
            patch("bedrock_server_manager.install.detect_server_version", return_value="1.21.0.03"),
            patch.object(levilamina, "enable_loader", return_value={"game": "bedrock_server_mod.exe"}) as enable,
        ):
            status, data = self.post("/api/levilamina/enable", {"confirm": True})
        self.assert_ok(status, data)
        self.assertEqual(data["game"], "bedrock_server_mod.exe")
        enable.assert_called_once_with(self.server_dir, "1.21.0.03")

    def test_enable_loader_error(self):
        with (
            patch("bedrock_server_manager.server.server_running", return_value=False),
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
            patch("bedrock_server_manager.install.detect_server_version", return_value="1.21.0.03"),
            patch.object(levilamina, "enable_loader", side_effect=levilamina.LeviLaminaError("not supported")),
        ):
            status, data = self.post("/api/levilamina/enable", {"confirm": True})
        self.assert_failed(status, data, code=400)

    def test_disable_requires_stopped_server(self):
        with patch("bedrock_server_manager.server.server_running", return_value=True):
            status, data = self.post("/api/levilamina/disable", {})
        self.assert_failed(status, data, code=400)
        self.assertIn("请先停止服务器", data["error"])

    def test_disable_success(self):
        with (
            patch("bedrock_server_manager.server.server_running", return_value=False),
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(levilamina, "disable_loader", return_value={"enabled": False}) as disable,
        ):
            status, data = self.post("/api/levilamina/disable", {})
        self.assert_ok(status, data)
        self.assertFalse(data["enabled"])
        disable.assert_called_once_with(self.server_dir)

    def test_disable_error(self):
        with (
            patch("bedrock_server_manager.server.server_running", return_value=False),
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(levilamina, "disable_loader", side_effect=levilamina.LeviLaminaError("boom")),
        ):
            status, data = self.post("/api/levilamina/disable", {})
        self.assert_failed(status, data, code=400)

    def test_mods_install_requires_server_dir(self):
        with patch.object(post_extra_routes, "resolve_server_dir", return_value=""):
            status, data = self.post("/api/levilamina/mods/install", {"url": "https://example.invalid/m.zip"})
        self.assert_failed(status, data, code=400)
        self.assertIn("未设置服务器目录", data["error"])

    def test_mods_install_requires_payload(self):
        with patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir):
            status, data = self.post("/api/levilamina/mods/install", {})
        self.assert_failed(status, data, code=400)
        self.assertIn("请提供插件包文件或 URL", data["error"])

    def test_mods_install_from_url(self):
        with (
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(levilamina, "install_mod_url", return_value={"name": "UrlMod"}) as install,
        ):
            status, data = self.post(
                "/api/levilamina/mods/install",
                {"url": "https://example.invalid/mod.zip", "file_name": "mod.zip"},
            )
        self.assert_ok(status, data)
        self.assertEqual(data["name"], "UrlMod")
        install.assert_called_once_with(self.server_dir, "https://example.invalid/mod.zip")

    def test_mods_install_from_url_error(self):
        with (
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(levilamina, "install_mod_url", side_effect=levilamina.LeviLaminaError("bad zip")),
        ):
            status, data = self.post(
                "/api/levilamina/mods/install",
                {"url": "https://example.invalid/mod.zip"},
            )
        self.assert_failed(status, data, code=400)

    def test_mods_install_from_base64(self):
        blob = base64.b64encode(b"PK\x03\x04fake-mod").decode("ascii")
        with (
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(levilamina, "install_mod_zip", return_value={"name": "LocalMod"}) as install,
        ):
            status, data = self.post(
                "/api/levilamina/mods/install",
                {"file_data": blob, "file_name": "local.zip"},
            )
        self.assert_ok(status, data)
        self.assertEqual(data["name"], "LocalMod")
        install.assert_called_once()
        self.assertEqual(install.call_args.args[0], self.server_dir)
        self.assertEqual(install.call_args.kwargs.get("display_name"), "local.zip")

    def test_mods_install_invalid_base64(self):
        with patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir):
            status, data = self.post("/api/levilamina/mods/install", {"file_data": "@@@not-base64@@@"})
        self.assert_failed(status, data, code=400)

    def test_mods_install_rejects_oversized_blob(self):
        class _HugeBlob:
            def __len__(self):
                return 121 * 1024 * 1024

        with (
            patch.object(post_extra_routes, "resolve_server_dir", return_value=self.server_dir),
            patch.object(base64, "b64decode", return_value=_HugeBlob()),
            patch.object(levilamina, "install_mod_zip") as install,
        ):
            status, data = self.post(
                "/api/levilamina/mods/install",
                {"file_data": "AAAA", "file_name": "huge.zip"},
            )
        self.assert_failed(status, data, code=400)
        self.assertIn("插件包过大", data["error"])
        install.assert_not_called()

    def test_mods_toggle_success(self):
        with patch.object(levilamina, "toggle_mod", return_value={"name": "m", "enabled": True}) as toggle:
            status, data = self.post("/api/levilamina/mods/toggle", {"name": "m", "enabled": True})
        self.assert_ok(status, data)
        self.assertTrue(data["enabled"])
        toggle.assert_called_once()

    def test_mods_toggle_error(self):
        with patch.object(levilamina, "toggle_mod", side_effect=levilamina.LeviLaminaError("missing")):
            status, data = self.post("/api/levilamina/mods/toggle", {"name": "m"})
        self.assert_failed(status, data, code=400)

    def test_mods_delete_requires_confirm(self):
        status, data = self.post("/api/levilamina/mods/delete", {"name": "m"})
        self.assert_failed(status, data, code=400)
        self.assertIn("需要确认", data["error"])

    def test_mods_delete_success(self):
        with patch.object(levilamina, "delete_mod", return_value={"name": "m"}) as delete:
            status, data = self.post("/api/levilamina/mods/delete", {"name": "m", "confirm": True})
        self.assert_ok(status, data)
        self.assertEqual(data["name"], "m")
        delete.assert_called_once()

    def test_mods_delete_error(self):
        with patch.object(levilamina, "delete_mod", side_effect=levilamina.LeviLaminaError("locked")):
            status, data = self.post("/api/levilamina/mods/delete", {"name": "m", "confirm": True})
        self.assert_failed(status, data, code=400)

    def test_lip_install_pkg_success_and_error(self):
        with patch.object(levilamina, "lip_install", return_value={"package": "x"}) as install:
            status, data = self.post("/api/levilamina/lip/install_pkg", {"package": "x"})
        self.assert_ok(status, data)
        install.assert_called_once()
        with patch.object(levilamina, "lip_install", side_effect=levilamina.LeviLaminaError("no lip")):
            status, data = self.post("/api/levilamina/lip/install_pkg", {"package": "x"})
        self.assert_failed(status, data, code=400)

    def test_lip_uninstall_pkg_success_and_error(self):
        with patch.object(levilamina, "lip_uninstall", return_value={"package": "x"}) as uninstall:
            status, data = self.post("/api/levilamina/lip/uninstall_pkg", {"package": "x"})
        self.assert_ok(status, data)
        uninstall.assert_called_once()
        with patch.object(levilamina, "lip_uninstall", side_effect=levilamina.LeviLaminaError("no lip")):
            status, data = self.post("/api/levilamina/lip/uninstall_pkg", {"package": "x"})
        self.assert_failed(status, data, code=400)


# ---------------------------------------------------------------------------
# post_extra.py：多实例
# ---------------------------------------------------------------------------


class TestPostExtraInstanceRoutes(_RouteCoverageBase):
    def test_add_missing_dir(self):
        status, data = self.post("/api/instances/add", {})
        self.assert_failed(status, data, code=400)
        self.assertIn("请提供服务器目录", data["error"])

    def test_add_not_installed_dir(self):
        with patch("bedrock_server_manager.utils.resolve_server_exe", return_value=""):
            status, data = self.post("/api/instances/add", {"dir": self.server_dir})
        self.assert_failed(status, data, code=400)
        self.assertIn("不是已安装的服务端", data["error"])

    def test_add_success(self):
        entry = {"dir": os.path.abspath(self.server_dir), "name": "main"}
        with (
            patch("bedrock_server_manager.utils.resolve_server_exe", return_value=SERVER_EXE),
            patch("bedrock_server_manager.server.register_instance", return_value=entry) as register,
            patch("bedrock_server_manager.server.list_instances", return_value=[entry]),
        ):
            status, data = self.post("/api/instances/add", {"dir": self.server_dir, "name": "main"})
        self.assert_ok(status, data)
        self.assertEqual(data["instance"], entry)
        self.assertEqual(data["instances"], [entry])
        register.assert_called_once()

    def test_add_value_error(self):
        with (
            patch("bedrock_server_manager.utils.resolve_server_exe", return_value=SERVER_EXE),
            patch("bedrock_server_manager.server.register_instance", side_effect=ValueError("目录不存在")),
        ):
            status, data = self.post("/api/instances/add", {"dir": self.server_dir})
        self.assert_failed(status, data, code=400)
        self.assertIn("目录不存在", data["error"])

    def test_remove_missing_dir(self):
        status, data = self.post("/api/instances/remove", {})
        self.assert_failed(status, data, code=400)
        self.assertIn("缺少 dir", data["error"])

    def test_remove_success(self):
        with (
            patch("bedrock_server_manager.server.unregister_instance", return_value=True) as unregister,
            patch("bedrock_server_manager.server.list_instances", return_value=[]),
        ):
            status, data = self.post("/api/instances/remove", {"dir": self.server_dir})
        self.assert_ok(status, data)
        self.assertEqual(data["instances"], [])
        unregister.assert_called_once()

    def test_remove_value_error(self):
        with patch("bedrock_server_manager.server.unregister_instance", side_effect=ValueError("unknown")):
            status, data = self.post("/api/instances/remove", {"dir": self.server_dir})
        self.assert_failed(status, data, code=400)

    def test_activate_success(self):
        entry = {"dir": os.path.abspath(self.server_dir), "name": "main", "active": True}
        with (
            patch("bedrock_server_manager.server.activate_instance", return_value=entry) as activate,
            patch("bedrock_server_manager.server.list_instances", return_value=[entry]),
        ):
            status, data = self.post("/api/instances/activate", {"dir": self.server_dir})
        self.assert_ok(status, data)
        self.assertEqual(data["instance"], entry)
        activate.assert_called_once()

    def test_activate_error(self):
        with patch("bedrock_server_manager.server.activate_instance", side_effect=OSError("disk")):
            status, data = self.post("/api/instances/activate", {"dir": self.server_dir})
        self.assert_failed(status, data, code=400)

    def test_start_success(self):
        with (
            patch("bedrock_server_manager.server.start_instance") as start,
            patch("bedrock_server_manager.server.list_instances", return_value=[]),
        ):
            status, data = self.post("/api/instances/start", {"dir": self.server_dir})
        self.assert_ok(status, data)
        start.assert_called_once_with(self.server_dir)

    def test_start_error(self):
        with patch(
            "bedrock_server_manager.server.start_instance",
            side_effect=FileNotFoundError("no exe"),
        ):
            status, data = self.post("/api/instances/start", {"dir": self.server_dir})
        self.assert_failed(status, data, code=400)
        self.assertIn("no exe", data["error"])

    def test_stop_success(self):
        with (
            patch("bedrock_server_manager.server.stop_instance") as stop,
            patch("bedrock_server_manager.server.list_instances", return_value=[]),
        ):
            status, data = self.post("/api/instances/stop", {"dir": self.server_dir})
        self.assert_ok(status, data)
        stop.assert_called_once_with(self.server_dir)

    def test_stop_error(self):
        with patch("bedrock_server_manager.server.stop_instance", side_effect=OSError("busy")):
            status, data = self.post("/api/instances/stop", {"dir": self.server_dir})
        self.assert_failed(status, data, code=400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
