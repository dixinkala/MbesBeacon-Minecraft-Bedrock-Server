"""
路由层单文件覆盖率测试（B 组）。

目标模块（只提升这几个文件的单文件覆盖率，不改生产代码）：

- ``bedrock_server_manager/web/routes/get_routes.py``
- ``bedrock_server_manager/web/routes/worlds.py``
- ``bedrock_server_manager/web/routes/backups.py``
- ``bedrock_server_manager/web/routes/packs.py``
- ``bedrock_server_manager/web/routes/players.py``

实现要点：

1. 真实 HTTP 服务：``ThreadingHTTPServer(("127.0.0.1", 0), Handler)``，
   端口由内核分配（0），不占用固定端口；``http.client`` 发请求，
   请求头带 ``X-API-Token``（``AppContext.instance().api_token``）。
2. 外部副作用全部打桩：服务器进程/命令捕获/下载/备份等都在路由模块的
   导入命名空间里用 ``mock.patch.object(模块, "名字", ...)`` 替换，
   测试不会真的启动服务器、联网或执行危险删除。
3. 需要真实文件读写的端点（世界 / 备份 / 包列表 / 玩家文件）使用
   ``tempfile`` 建独立服务器目录，写 ``settings["server_dir"]``，
   每个用例结束时恢复 settings 并清理临时目录。
4. 覆盖正常路径 + 错误分支（未设置目录、不存在、非法名、缺参数、
   危险操作二次确认缺失、ZIP 非法路径 / 上限 / 非 ZIP 等）。
"""

import base64
import http.client
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch
from urllib.parse import quote

from bedrock_server_manager import levilamina, remote, upnp
from bedrock_server_manager.app_context import AppContext
from bedrock_server_manager.console import ConsoleBuffer
from bedrock_server_manager.constants import IS_WINDOWS, SERVER_EXE
from bedrock_server_manager.ratelimit import get_rate_limiter
from bedrock_server_manager.web import handler as handler_mod
from bedrock_server_manager.web.handler import Handler
from bedrock_server_manager.web.routes import (
    backups as backups_routes,
    get_routes as get_routes_routes,
    packs as packs_routes,
    players as players_routes,
    worlds as worlds_routes,
)


class RoutesCoverageBTest(unittest.TestCase):
    """覆盖 get_routes / worlds / backups / packs / players 五个路由模块。"""

    # ------------------------------------------------------------------ 生命周期

    @classmethod
    def setUpClass(cls):
        # 端口 0：由内核挑选空闲端口，绝不占用 19100 等生产端口
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.token = AppContext.instance().api_token

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=10)

    def setUp(self):
        self._settings_snapshot = dict(AppContext.instance().settings)
        # 其他测试可能把全局 console 换成 _NullConsole；本套用例需要真实的缓冲区
        self._console_snapshot = AppContext.instance().console
        AppContext.instance().console = ConsoleBuffer()
        # 清空限流桶：同一用例内会连续打多个端点
        get_rate_limiter().reset()
        # 不要真的往磁盘写 server_console.log（sync_log_file 会设置它）
        AppContext.instance().console.set_log_file(None)

        self.tmp = tempfile.mkdtemp(prefix="bsm_routes_cov_b_")
        self.server_dir = os.path.join(self.tmp, "TestServer")
        os.makedirs(self.server_dir, exist_ok=True)
        self._write_props()
        self._write_exe()
        self._make_world("Bedrock level")
        self._make_world("SecondWorld")
        self._make_world("OldWorld")

        settings = AppContext.instance().settings
        settings["server_dir"] = self.server_dir
        settings["installed_version"] = "1.21.0.03"
        settings["remote_enabled"] = False
        settings.pop("remote_password_hash", None)

    def tearDown(self):
        settings = AppContext.instance().settings
        settings.clear()
        settings.update(self._settings_snapshot)
        AppContext.instance().console = self._console_snapshot
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ 工具方法

    def _write_props(self, **overrides):
        values = {
            "server-name": "Coverage Server",
            "level-name": "Bedrock level",
            "server-port": "19132",
            "max-players": "10",
            "gamemode": "survival",
        }
        values.update(overrides)
        with open(os.path.join(self.server_dir, "server.properties"), "w", encoding="utf-8") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")

    def _write_exe(self, name=SERVER_EXE):
        with open(os.path.join(self.server_dir, name), "wb") as f:
            f.write(b"MZ" + b"\x00" * 64)

    def _make_world(self, name, marker=b"\x00" * 16):
        world = os.path.join(self.server_dir, "worlds", name)
        os.makedirs(world, exist_ok=True)
        with open(os.path.join(world, "level.dat"), "wb") as f:
            f.write(marker)
        return world

    def _zip_b64(self, entries):
        """把 {zip 成员名: bytes} 打成 ZIP 并返回 base64 文本。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, payload in entries.items():
                zf.writestr(name, payload)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _request(self, method, path, data=None, token=None, raw_body=None, headers=None):
        """发一个 HTTP 请求，返回 (status, headers_dict, body_bytes)。

        ``token=None`` 表示使用默认 token；``token=False`` 表示不带 token。
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        if token is None:
            hdrs["X-API-Token"] = self.token
        elif token is not False:
            hdrs["X-API-Token"] = token
        if headers:
            hdrs.update(headers)
        body = raw_body
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, dict(resp.getheaders()), payload
        finally:
            conn.close()

    def _get(self, path, **kwargs):
        status, headers, body = self._request("GET", path, **kwargs)
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            data = {}
        return status, data, headers, body

    def _get_json(self, path, **kwargs):
        status, data, _headers, _body = self._get(path, **kwargs)
        return status, data

    def _post(self, path, data=None, **kwargs):
        status, headers, body = self._request("POST", path, data=data if data is not None else {}, **kwargs)
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            parsed = {}
        return status, parsed, headers, body

    def _post_json(self, path, data=None, **kwargs):
        status, parsed, _headers, _body = self._post(path, data, **kwargs)
        return status, parsed

    # ==================================================================
    # get_routes.py
    # ==================================================================

    def test_get_index_and_index_html(self):
        status, headers, body = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        # 回环页面必须注入 API token
        self.assertIn(self.token, body.decode("utf-8", "replace"))

        status, data, headers, _body = self._get("/index.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))

    def test_get_health(self):
        with patch.object(get_routes_routes, "server_running", return_value=False):
            status, data = self._get_json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "healthy")
        self.assertIn("version", data)
        self.assertIn("uptime_seconds", data)
        self.assertFalse(data["server_running"])

    def test_get_index_remote_redirect(self):
        """远程未登录访问首页 -> 302 到 /login（不下发 token）。"""
        with (
            patch.object(remote, "enabled", return_value=True),
            patch.object(remote, "is_loopback_value", return_value=False),
        ):
            status, headers, _body = self._request("GET", "/", token=False)
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/login")

    def test_get_health_degraded(self):
        """psutil 不可用 / 抛异常时健康检查降级，但仍然返回 200。"""
        with patch.dict(sys.modules, {"psutil": None}):
            status, data = self._get_json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["memory_mb"], 0)

        try:
            import psutil
        except ImportError:  # pragma: no cover - 当前环境已安装 psutil
            self.skipTest("psutil 未安装")
        with patch.object(psutil, "Process", side_effect=RuntimeError("无法读取进程信息")):
            status, data = self._get_json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_get_status_detects_version(self):
        settings = AppContext.instance().settings
        settings["installed_version"] = ""
        with (
            patch.object(get_routes_routes, "detect_servers", return_value=[]),
            patch.object(get_routes_routes, "detect_server_version", return_value="9.9.9"),
            patch.object(get_routes_routes, "save_settings") as mock_save,
        ):
            status, data = self._get_json("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["installed_version"], "9.9.9")
        self.assertEqual(data["server_dir"], self.server_dir)
        self.assertTrue(data["installed"])
        mock_save.assert_called()

    def test_get_status_existing_version(self):
        with (
            patch.object(get_routes_routes, "detect_servers", return_value=[{"dir": self.server_dir}]),
            patch.object(get_routes_routes, "server_running", return_value=True),
        ):
            status, data = self._get_json("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["installed_version"], "1.21.0.03")
        self.assertTrue(data["server_running"])
        self.assertEqual(len(data["detected_servers"]), 1)
        self.assertIn("install", data)

    def test_get_servers_detect(self):
        with patch.object(get_routes_routes, "detect_servers", return_value=[{"dir": "D:/A"}, {"dir": "D:/B"}]):
            status, data = self._get_json("/api/servers/detect")
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 2)

    def test_get_browse_dir_variants(self):
        with (
            patch.object(get_routes_routes, "get_ui_lang", return_value="zh"),
            patch.object(get_routes_routes, "browse_directory", return_value=self.tmp),
        ):
            status, data = self._get_json("/api/browse-dir")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["dir"], self.tmp)

        # 用户取消
        with patch.object(get_routes_routes, "browse_directory", return_value=None):
            status, data = self._get_json("/api/browse-dir")
        self.assertTrue(data["ok"])
        self.assertIsNone(data["dir"])
        self.assertTrue(data["cancelled"])

        # 对话框不可用：返回失败（/api/browse-dir 的 GET 配额是 2 次/秒，先清桶）
        get_rate_limiter().reset()
        with patch.object(get_routes_routes, "browse_directory", side_effect=RuntimeError("无图形环境")):
            status, data = self._get_json("/api/browse-dir")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("无图形环境", data["error"])
        self.assertIsNone(data["dir"])

    def test_get_theme_variants(self):
        settings = AppContext.instance().settings
        settings["theme"] = "dark"
        status, data = self._get_json("/api/theme")
        self.assertEqual(status, 200)
        self.assertEqual(data["theme"], "dark")

        # settings 无主题时回落到 load_theme()
        settings["theme"] = ""
        with patch.object(get_routes_routes, "load_theme", return_value={"preset": "ocean"}):
            status, data = self._get_json("/api/theme")
        self.assertEqual(data["theme"], {"preset": "ocean"})

    def test_get_lang(self):
        with patch.object(get_routes_routes, "get_ui_lang", return_value="en"):
            status, data = self._get_json("/api/lang")
        self.assertEqual(status, 200)
        self.assertEqual(data["lang"], "en")

    def test_get_latest_variants(self):
        info = {"version": "1.21.0.03", "url": "http://example.invalid/a.zip"}
        with patch.object(get_routes_routes, "get_latest_server_info", return_value=info) as mock_latest:
            status, data = self._get_json("/api/latest")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["info"], info)
        mock_latest.assert_called_once()
        self.assertIn("ignore_ssl", mock_latest.call_args.kwargs)

        with patch.object(get_routes_routes, "get_latest_server_info", side_effect=RuntimeError("网络不可用")):
            status, data = self._get_json("/api/latest")
        self.assertFalse(data["ok"])
        self.assertIn("网络不可用", data["error"])

    def test_get_versions_variants(self):
        with patch.object(get_routes_routes, "get_cached_versions", return_value=(["1.21.0.03"], True)) as mock_ver:
            status, data = self._get_json("/api/versions?preview=1")
        self.assertEqual(status, 200)
        self.assertTrue(data["preview"])
        self.assertTrue(data["from_cache"])
        self.assertEqual(data["versions"], ["1.21.0.03"])
        self.assertEqual(mock_ver.call_args.kwargs, {"include_preview": True})

        with patch.object(get_routes_routes, "get_cached_versions", side_effect=RuntimeError("缓存坏了")):
            status, data = self._get_json("/api/versions")
        self.assertFalse(data["ok"])
        self.assertIn("缓存坏了", data["error"])
        self.assertIn("versions", data)  # FALLBACK_VERSIONS 兜底

    def test_get_sources_and_progress(self):
        status, data = self._get_json("/api/sources")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertGreater(len(data["sources"]), 0)
        self.assertIn("index", data["sources"][0])

        status, data = self._get_json("/api/progress")
        self.assertEqual(status, 200)
        self.assertIn("busy", data)

    def test_get_console_and_reset(self):
        # since 非法 -> 回落到 0
        status, data = self._get_json("/api/console?since=abc")
        self.assertEqual(status, 200)
        self.assertIn("lines", data)
        self.assertIn("running", data)

        # 游标落后于保留窗口 -> reset=True
        console = AppContext.instance().console
        console.lines = []
        while console.total < 2:
            console.append("padding line")
        console.clear()
        status, data = self._get_json("/api/console?since=1")
        self.assertTrue(data["reset"])

    def test_get_console_export(self):
        status, headers, body = self._request("GET", "/api/console/export")
        self.assertEqual(status, 200)
        self.assertIn("text/plain", headers.get("Content-Type", ""))
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIsInstance(body, bytes)

    def test_get_performance(self):
        with (
            patch.object(get_routes_routes, "get_server_proc", return_value=None),
            patch.object(
                get_routes_routes, "get_server_performance_full", return_value={"ok": True, "players": 0}
            ) as mock_perf,
        ):
            status, data = self._get_json("/api/performance")
        self.assertEqual(status, 200)
        self.assertEqual(data["players"], 0)
        self.assertIsNone(mock_perf.call_args.kwargs["server_proc"])

    def test_get_backups_list(self):
        backup_root = os.path.join(self.tmp, "_worlds_backups")
        os.makedirs(backup_root, exist_ok=True)
        backup = os.path.join(backup_root, f"{os.path.basename(self.server_dir)}_worlds_20240101_010101")
        os.makedirs(backup, exist_ok=True)
        with open(os.path.join(backup, "level.dat"), "wb") as f:
            f.write(b"\x00" * 32)
        status, data = self._get_json("/api/backups")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["backups"][0]["type"], "directory")
        self.assertTrue(data["backup_root"].endswith("_worlds_backups"))

    def test_get_backups_without_server_dir(self):
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._get_json("/api/backups")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未设置服务器目录")

    def test_get_laninfo(self):
        with patch.object(get_routes_routes, "get_lan_ip", return_value="192.168.1.50"):
            status, data = self._get_json("/api/laninfo")
        self.assertEqual(status, 200)
        self.assertEqual(data["lan_ip"], "192.168.1.50")
        self.assertEqual(data["port"], 19132)
        self.assertEqual(data["address"], "192.168.1.50:19132")

        # 未设置服务器目录 -> 使用默认端口
        AppContext.instance().settings["server_dir"] = ""
        with patch.object(get_routes_routes, "get_lan_ip", return_value="10.0.0.2"):
            status, data = self._get_json("/api/laninfo")
        self.assertEqual(data["port"], 19132)
        self.assertEqual(data["address"], "10.0.0.2:19132")

    def test_get_config(self):
        with patch.object(get_routes_routes, "get_lan_ip", return_value="192.168.1.50"):
            status, data = self._get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["data"]["server-name"], "Coverage Server")
        self.assertEqual(data["data"]["max-players"], "10")
        self.assertIn("meta", data)
        self.assertEqual(data["port"], 19132)
        self.assertEqual(data["address"], "192.168.1.50:19132")

    def test_get_config_missing_server(self):
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "尚未安装服务器")

    def test_get_config_full(self):
        status, data = self._get_json("/api/config/full")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        # FULL_PROP_META 里的缺省键会被补上（列表默认取首个元素）
        self.assertEqual(data["data"]["level-type"], "DEFAULT")
        self.assertIn("meta", data)

    def test_get_config_full_missing_server(self):
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._get_json("/api/config/full")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])

    def test_get_listdir_illegal_path(self):
        status, data = self._get_json("/api/listdir?path=" + quote("../secret"))
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "非法路径")
        self.assertEqual(data["dirs"], [])

    def test_get_listdir_root_view(self):
        if IS_WINDOWS:
            # 空路径 = "我的电脑"
            status, data = self._get_json("/api/listdir?path=")
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertIn("drives", data)
            self.assertGreater(len(data["drives"]), 0)
            # 盘根目录：其他盘符排在最前面
            root = os.path.splitdrive(self.tmp)[0] + "\\"
            status, data = self._get_json("/api/listdir?path=" + quote(root))
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertTrue(os.path.isdir(data["path"]))
        else:  # pragma: no cover - 当前测试机为 Windows
            status, data = self._get_json("/api/listdir?path=")
            self.assertTrue(data["ok"])

    def test_get_listdir_system_dir(self):
        system_dir = "C:\\Windows" if IS_WINDOWS else "/etc"
        status, data = self._get_json("/api/listdir?path=" + quote(system_dir))
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("系统目录", data["error"])

    def test_get_listdir_sensitive_dir(self):
        sensitive = os.path.join(os.path.expanduser("~"), ".ssh")
        status, data = self._get_json("/api/listdir?path=" + quote(sensitive))
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("敏感目录", data["error"])

    def test_get_listdir_normal_dir(self):
        status, data = self._get_json("/api/listdir?path=" + quote(self.server_dir))
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("worlds", data["dirs"])
        self.assertTrue(data["is_server"])
        self.assertTrue(data["has_exe"])
        self.assertEqual(data["parent"], os.path.dirname(self.server_dir))

    def test_get_listdir_error_branch(self):
        # 读取目录过程中抛异常 -> ok=False + error
        with patch.object(get_routes_routes, "resolve_server_exe", side_effect=OSError("磁盘故障")):
            status, data = self._get_json("/api/listdir?path=" + quote(self.server_dir))
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("磁盘故障", data["error"])

    def test_get_players_stopped(self):
        with patch.object(get_routes_routes, "server_running", return_value=False):
            status, data = self._get_json("/api/players")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(data["running"])
        self.assertEqual(data["players"], [])
        self.assertIn("permissions", data)

    def test_get_players_running(self):
        with (
            patch.object(get_routes_routes, "server_running", return_value=True),
            patch.object(
                get_routes_routes, "send_command_capture", return_value="There are 1/10 players online: Steve"
            ),
        ):
            status, data = self._get_json("/api/players")
        self.assertEqual(status, 200)
        self.assertTrue(data["running"])
        self.assertEqual(data["players"], ["Steve"])
        self.assertIn("raw", data)

    def test_get_players_banlist_allowlist(self):
        with open(os.path.join(self.server_dir, "banned-players.json"), "w", encoding="utf-8") as f:
            json.dump([{"name": "BadBoy", "reason": "作弊"}], f)
        with open(os.path.join(self.server_dir, "allowlist.json"), "w", encoding="utf-8") as f:
            json.dump([{"name": "GoodBoy", "ignoresPlayerLimit": False}], f)

        status, data = self._get_json("/api/players/banlist")
        self.assertEqual(status, 200)
        self.assertEqual(data["banned"][0]["name"], "BadBoy")

        status, data = self._get_json("/api/players/allowlist")
        self.assertEqual(status, 200)
        self.assertEqual(data["allowed"][0]["name"], "GoodBoy")

    def test_get_remote_config(self):
        with (
            patch.object(remote, "primary_lan_ip", return_value="127.0.0.1"),
            patch.object(handler_mod, "_is_loopback_client", return_value=False),
        ):
            status, data = self._get_json("/api/remote/config")
        self.assertEqual(status, 403)
        self.assertFalse(data["ok"])

        with patch.object(remote, "primary_lan_ip", return_value="127.0.0.1"):
            status, data = self._get_json("/api/remote/config")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["port"], self.port)
        self.assertIn("sessions", data)
        self.assertEqual(data["bind_host"], "127.0.0.1")

    def test_get_tunnel_status(self):
        with (
            patch.object(remote, "primary_lan_ip", return_value="127.0.0.1"),
            patch.object(
                upnp,
                "mapping_state",
                return_value={"available": True, "mapped": True, "external_ip": "1.2.3.4", "error": ""},
            ) as mock_upnp,
        ):
            status, data = self._get_json("/api/tunnel/status?upnp=1")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["upnp"]["mapped"])
        mock_upnp.assert_called_once()

        # 不带 upnp 参数时不查询映射状态
        with patch.object(remote, "primary_lan_ip", return_value="127.0.0.1"):
            status, data = self._get_json("/api/tunnel/status")
        self.assertFalse(data["upnp"]["available"])
        self.assertIn("providers", data)
        self.assertIn("default_commands", data)

    def test_get_tunnel_status_upnp_error(self):
        with (
            patch.object(remote, "primary_lan_ip", return_value="127.0.0.1"),
            patch.object(upnp, "mapping_state", side_effect=RuntimeError("UPnP 服务不可用")),
        ):
            status, data = self._get_json("/api/tunnel/status?upnp=1")
        self.assertEqual(status, 200)
        self.assertFalse(data["upnp"]["available"])
        self.assertIn("UPnP 服务不可用", data["upnp"]["error"])

    def test_get_builtin_tunnel(self):
        status, data = self._get_json("/api/tunnel/builtin")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("platform", data)
        self.assertIn("playit", data)
        self.assertIn("frp", data)

    def test_get_levilamina_status(self):
        # 移除假 exe：避免走 Windows 版本资源读取分支
        os.remove(os.path.join(self.server_dir, SERVER_EXE))
        status, data = self._get_json("/api/levilamina/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("state", data)
        self.assertIn("job", data)
        self.assertTrue(data["state"]["supported"] in (True, False))

        # state() 抛异常时降级返回
        with patch.object(levilamina, "state", side_effect=RuntimeError("坏了")):
            status, data = self._get_json("/api/levilamina/status")
        self.assertTrue(data["ok"])
        self.assertIn("坏了", data["state"]["error"])

    def test_get_instances(self):
        status, data = self._get_json("/api/instances")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["instances"], list)

    def test_get_requires_token(self):
        status, data, _headers, _body = self._get("/api/status", token=False)
        self.assertEqual(status, 403)
        self.assertFalse(data["ok"])
        # GET / 与 /api/health 免认证
        status, _headers, _body = self._request("GET", "/api/health", token=False)
        self.assertEqual(status, 200)

    # ==================================================================
    # worlds.py
    # ==================================================================

    def test_worlds_list(self):
        status, data = self._get_json("/api/worlds/list")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        names = sorted(w["name"] for w in data["worlds"])
        self.assertEqual(names, ["Bedrock level", "OldWorld", "SecondWorld"])
        self.assertEqual(data["active"], "Bedrock level")
        self.assertTrue(any(w["is_active"] for w in data["worlds"]))

    def test_worlds_list_without_server_dir(self):
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._get_json("/api/worlds/list")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未设置服务器目录")

    def test_worlds_switch_validation(self):
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._post_json("/api/worlds/switch", {"name": "SecondWorld"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未设置服务器目录")

        AppContext.instance().settings["server_dir"] = self.server_dir
        status, data = self._post_json("/api/worlds/switch", {"name": "   "})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "请指定世界名")

    def test_worlds_switch_success(self):
        status, data = self._post_json("/api/worlds/switch", {"name": "SecondWorld"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        # 活动世界已写入配置
        status, listing = self._get_json("/api/worlds/list")
        self.assertEqual(listing["active"], "SecondWorld")

    def test_worlds_switch_missing_world(self):
        status, data = self._post_json("/api/worlds/switch", {"name": "NotThere"})
        self.assertFalse(data["ok"])
        self.assertIn("不存在", data["error"])

    def test_worlds_rename(self):
        status, data = self._post_json("/api/worlds/rename", {"old_name": "SecondWorld", "new_name": "RenamedWorld"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(os.path.isdir(os.path.join(self.server_dir, "worlds", "RenamedWorld")))

        # 非法新名 -> rename_world 返回错误
        status, data = self._post_json("/api/worlds/rename", {"old_name": "RenamedWorld", "new_name": "bad/name"})
        self.assertFalse(data["ok"])
        self.assertIn("非法字符", data["error"])

    def test_worlds_delete_requires_confirm(self):
        status, data = self._post_json("/api/worlds/delete", {"name": "OldWorld"})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertTrue(data["need_confirm"])
        self.assertEqual(data["confirm_key"], "confirm_delete")

    def test_worlds_delete(self):
        # 不存在的世界
        status, data = self._post_json("/api/worlds/delete", {"name": "NotThere", "confirm_delete": "DELETE"})
        self.assertFalse(data["ok"])
        self.assertIn("不存在", data["error"])

        # 活动世界不允许删除
        status, data = self._post_json("/api/worlds/delete", {"name": "Bedrock level", "confirm_delete": "DELETE"})
        self.assertFalse(data["ok"])
        self.assertIn("活动世界", data["error"])

        # 成功删除（重复调用会再次消耗限流令牌，这里用一次确认删除）
        status, data = self._post_json("/api/worlds/delete", {"name": "OldWorld", "confirm_delete": "DELETE"})
        self.assertTrue(data["ok"])
        self.assertFalse(os.path.isdir(os.path.join(self.server_dir, "worlds", "OldWorld")))

    def test_worlds_duplicate(self):
        status, data = self._post_json("/api/worlds/duplicate", {"source": "Bedrock level", "new_name": "CopyWorld"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(os.path.isdir(os.path.join(self.server_dir, "worlds", "CopyWorld")))

        # 目标已存在
        status, data = self._post_json("/api/worlds/duplicate", {"source": "Bedrock level", "new_name": "CopyWorld"})
        self.assertFalse(data["ok"])
        self.assertIn("已存在", data["error"])

    def test_worlds_export_success(self):
        status, headers, body = self._request("GET", "/api/worlds/export")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/zip")
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("_worlds_", headers.get("Content-Disposition", ""))
        self.assertTrue(body.startswith(b"PK\x03\x04"))
        # 解出的 ZIP 应包含 worlds/ 下的文件
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = zf.namelist()
        self.assertTrue(any("worlds" in name for name in names))

    def test_worlds_export_errors(self):
        # 未设置服务器目录
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._get_json("/api/worlds/export")
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未设置服务器目录")

        # worlds 目录不存在
        AppContext.instance().settings["server_dir"] = self.server_dir
        shutil.rmtree(os.path.join(self.server_dir, "worlds"))
        status, data = self._get_json("/api/worlds/export")
        self.assertFalse(data["ok"])
        self.assertIn("worlds 目录不存在", data["error"])

        # 打包过程抛异常
        self._make_world("Bedrock level")
        with patch.object(zipfile, "ZipFile", side_effect=OSError("磁盘已满")):
            status, data = self._get_json("/api/worlds/export")
        self.assertFalse(data["ok"])
        self.assertIn("导出失败", data["error"])

    def test_worlds_import_requires_confirm(self):
        status, data = self._post_json("/api/worlds/import", {"file_data": "eA=="})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertTrue(data["need_confirm"])
        self.assertEqual(data["confirm_key"], "confirm_import")

    def test_worlds_import_validation(self):
        # 已确认但未设置服务器目录
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT", "file_data": "eA=="})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未设置服务器目录")

        # 有目录但没给文件数据
        AppContext.instance().settings["server_dir"] = self.server_dir
        status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未提供文件数据")

        # 不是 ZIP
        bad_data = base64.b64encode(b"this is not a zip").decode("ascii")
        status, data = self._post_json(
            "/api/worlds/import", {"confirm_import": "IMPORT", "file_data": bad_data, "file_name": "bad.zip"}
        )
        self.assertFalse(data["ok"])
        self.assertIn("不是有效的 ZIP", data["error"])

    def test_worlds_import_illegal_paths(self):
        # 相对路径穿越
        zip_b64 = self._zip_b64({"../evil.txt": b"x"})
        status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT", "file_data": zip_b64})
        self.assertFalse(data["ok"])
        self.assertIn("非法路径", data["error"])

        # 绝对路径
        zip_b64 = self._zip_b64({"/abs.txt": b"x"})
        status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT", "file_data": zip_b64})
        self.assertFalse(data["ok"])
        self.assertIn("非法路径", data["error"])

    def test_worlds_import_zip_bomb_limit(self):
        zip_b64 = self._zip_b64({"worlds/Zone/level.dat": b"\x00" * 64})
        with patch.object(worlds_routes, "MAX_WORLD_IMPORT_BYTES", 10):
            status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT", "file_data": zip_b64})
        self.assertFalse(data["ok"])
        self.assertIn("超过上限", data["error"])

    def test_worlds_import_no_world_found(self):
        zip_b64 = self._zip_b64({"readme.txt": b"nothing here"})
        status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT", "file_data": zip_b64})
        self.assertFalse(data["ok"])
        self.assertIn("未找到世界存档", data["error"])

    def test_worlds_import_success_worlds_dir(self):
        zip_b64 = self._zip_b64(
            {
                "worlds/Zone/level.dat": b"\x00" * 32,
                "worlds/Zone/db/MANIFEST-000001": b"db",
            }
        )
        status, data = self._post_json(
            "/api/worlds/import", {"confirm_import": "IMPORT", "file_data": zip_b64, "file_name": "w.zip"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(os.path.isdir(os.path.join(self.server_dir, "worlds", "Zone")))
        # 导入前会备份现有 worlds
        self.assertTrue(any(name.startswith("worlds_backup_") for name in os.listdir(self.server_dir)))

    def test_worlds_import_success_root_level_dat(self):
        # ZIP 根部直接是 level.dat -> 包装成 ImportedWorld
        zip_b64 = self._zip_b64({"level.dat": b"\x00" * 32})
        status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT", "file_data": zip_b64})
        self.assertTrue(data["ok"])
        self.assertTrue(os.path.isdir(os.path.join(self.server_dir, "worlds", "ImportedWorld")))

    def test_worlds_import_success_container(self):
        # 根部是世界文件夹 + 说明文件（用户手工打包的常见形态）
        zip_b64 = self._zip_b64({"WorldA/level.dat": b"\x00" * 32, "readme.txt": b"note"})
        status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT", "file_data": zip_b64})
        self.assertTrue(data["ok"])
        self.assertTrue(os.path.isdir(os.path.join(self.server_dir, "worlds", "WorldA")))

    def test_worlds_import_exception(self):
        with patch("base64.b64decode", side_effect=ValueError("解码失败")):
            status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT", "file_data": "whatever"})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("导入失败", data["error"])

    def test_worlds_mutate_without_server_dir(self):
        """rename / duplicate / delete 在未设置服务器目录时的分支。"""
        AppContext.instance().settings["server_dir"] = ""
        cases = (
            ("/api/worlds/rename", {"old_name": "A", "new_name": "B"}),
            ("/api/worlds/duplicate", {"source": "A", "new_name": "B"}),
            ("/api/worlds/delete", {"name": "A", "confirm_delete": "DELETE"}),
        )
        for path, payload in cases:
            status, data = self._post_json(path, payload)
            self.assertEqual(status, 200)
            self.assertFalse(data["ok"], path)
            self.assertEqual(data["error"], "未设置服务器目录", path)

    def test_worlds_import_single_root_folder(self):
        """ZIP 只有一个世界文件夹（无 worlds/ 层）。"""
        zip_b64 = self._zip_b64({"SoloWorld/level.dat": b"\x00" * 32})
        status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT", "file_data": zip_b64})
        self.assertTrue(data["ok"])
        self.assertTrue(os.path.isdir(os.path.join(self.server_dir, "worlds", "SoloWorld")))

    def test_worlds_import_multiple_entries_no_world(self):
        """ZIP 根部多个条目但没有一个像世界 -> 报未找到。"""
        zip_b64 = self._zip_b64({"a.txt": b"a", "b.txt": b"b"})
        status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT", "file_data": zip_b64})
        self.assertFalse(data["ok"])
        self.assertIn("未找到世界存档", data["error"])

    def test_worlds_import_overwrites_existing_world(self):
        """导入同名世界时先删旧目录再复制。"""
        zip_b64 = self._zip_b64({"worlds/Bedrock level/level.dat": b"\x01" * 32})
        status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT", "file_data": zip_b64})
        self.assertTrue(data["ok"])
        with open(os.path.join(self.server_dir, "worlds", "Bedrock level", "level.dat"), "rb") as f:
            self.assertEqual(f.read(), b"\x01" * 32)

    def test_worlds_import_wrap_fallback(self):
        """根部即世界内容，且 os.replace 失败时回退到 copytree。"""
        zip_b64 = self._zip_b64({"level.dat": b"\x00" * 32})
        with patch.object(worlds_routes.os, "replace", side_effect=OSError("目录被占用")):
            status, data = self._post_json("/api/worlds/import", {"confirm_import": "IMPORT", "file_data": zip_b64})
        self.assertTrue(data["ok"])
        self.assertTrue(os.path.isdir(os.path.join(self.server_dir, "worlds", "ImportedWorld")))

    # ==================================================================
    # backups.py
    # ==================================================================

    def test_backups_create_success_and_clamp(self):
        with patch.object(backups_routes, "backup_worlds", return_value=os.path.join(self.tmp, "bk")) as mock_bk:
            status, data = self._post_json("/api/backups/create", {"max_backups": 7})
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(mock_bk.call_args.kwargs["max_backups"], 7)

            # 非法值 -> 默认 10
            self._post_json("/api/backups/create", {"max_backups": "abc"})
            self.assertEqual(mock_bk.call_args.kwargs["max_backups"], 10)

            # 超过上限 -> 100
            self._post_json("/api/backups/create", {"max_backups": 9999})
            self.assertEqual(mock_bk.call_args.kwargs["max_backups"], 100)

            # 负数 -> 1
            self._post_json("/api/backups/create", {"max_backups": -5})
            self.assertEqual(mock_bk.call_args.kwargs["max_backups"], 1)

    def test_backups_create_errors(self):
        # 未设置服务器目录
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._post_json("/api/backups/create", {})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未找到服务器")

        # 目录存在但没安装服务器（没有 exe）
        no_exe_dir = os.path.join(self.tmp, "NoExe")
        os.makedirs(no_exe_dir, exist_ok=True)
        AppContext.instance().settings["server_dir"] = no_exe_dir
        status, data = self._post_json("/api/backups/create", {})
        self.assertFalse(data["ok"])

        # backup_worlds 返回空 -> 失败
        AppContext.instance().settings["server_dir"] = self.server_dir
        with patch.object(backups_routes, "backup_worlds", return_value=""):
            status, data = self._post_json("/api/backups/create", {})
        self.assertFalse(data["ok"])
        self.assertIn("备份失败", data["error"])

    def test_backups_restore_requires_confirm(self):
        status, data = self._post_json("/api/backups/restore", {"path": "/bk"})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertTrue(data["need_confirm"])
        self.assertEqual(data["confirm_key"], "confirm_restore")

    def test_backups_restore_errors(self):
        # 已确认但未设置目录
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._post_json("/api/backups/restore", {"confirm_restore": "RESTORE", "path": "/bk"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未设置服务器目录")

        # 有目录但未指定备份路径
        AppContext.instance().settings["server_dir"] = self.server_dir
        status, data = self._post_json("/api/backups/restore", {"confirm_restore": "RESTORE"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未指定备份路径")

    def test_backups_restore_success_and_failure(self):
        with patch.object(backups_routes, "restore_backup", return_value=(True, "")) as mock_restore:
            status, data = self._post_json(
                "/api/backups/restore", {"confirm_restore": "RESTORE", "path": os.path.join(self.tmp, "bk")}
            )
        self.assertTrue(data["ok"])
        self.assertIn("backups", data)
        mock_restore.assert_called_once()

        with patch.object(backups_routes, "restore_backup", return_value=(False, "备份已损坏")):
            status, data = self._post_json(
                "/api/backups/restore", {"confirm_restore": "RESTORE", "path": os.path.join(self.tmp, "bk2")}
            )
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "备份已损坏")

    def test_backups_delete(self):
        # 未指定路径
        status, data = self._post_json("/api/backups/delete", {})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未指定备份路径")

        # 删除成功（有目录 -> 会刷新列表）
        with patch.object(backups_routes, "delete_backup", return_value=(True, "")):
            status, data = self._post_json("/api/backups/delete", {"path": os.path.join(self.tmp, "bk")})
        self.assertTrue(data["ok"])
        self.assertIn("backups", data)

        # 删除成功（无目录 -> 返回空列表）
        AppContext.instance().settings["server_dir"] = ""
        with patch.object(backups_routes, "delete_backup", return_value=(True, "")):
            status, data = self._post_json("/api/backups/delete", {"path": os.path.join(self.tmp, "bk")})
        self.assertTrue(data["ok"])
        self.assertEqual(data["backups"], [])

        # 删除失败
        with patch.object(backups_routes, "delete_backup", return_value=(False, "目录不存在")):
            status, data = self._post_json("/api/backups/delete", {"path": os.path.join(self.tmp, "nope")})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "目录不存在")

    def test_backups_zip(self):
        # 未找到服务器
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._post_json("/api/backup/zip", {})
        self.assertFalse(data["ok"])

        # 成功
        AppContext.instance().settings["server_dir"] = self.server_dir
        zip_path = os.path.join(self.tmp, "bk.zip")
        with patch.object(backups_routes, "backup_worlds_zip", return_value=zip_path) as mock_zip:
            status, data = self._post_json("/api/backup/zip", {"max_backups": 3})
        self.assertTrue(data["ok"])
        self.assertEqual(data["path"], zip_path)
        self.assertEqual(mock_zip.call_args.kwargs["max_backups"], 3)

        # 失败
        with patch.object(backups_routes, "backup_worlds_zip", return_value=""):
            status, data = self._post_json("/api/backup/zip", {})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "备份失败")

    # ==================================================================
    # packs.py
    # ==================================================================

    def _make_pack(self, pack_type="resource_packs", name="DemoPack", pack_id="demo-uuid", version=(1, 0, 0)):
        pack_dir = os.path.join(self.server_dir, pack_type, name)
        os.makedirs(pack_dir, exist_ok=True)
        manifest = {"header": {"pack_id": pack_id, "version": list(version), "name": name}}
        with open(os.path.join(pack_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)

    def test_packs_list(self):
        self._make_pack()
        status, data = self._get_json("/api/packs")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        names = [p["name"] for p in data["packs"]["resource_packs"]]
        self.assertIn("DemoPack", names)
        self.assertEqual(data["packs"]["behavior_packs"], [])
        self.assertEqual(data["packs"]["active_world"], "Bedrock level")

    def test_packs_toggle(self):
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._post_json("/api/packs/toggle", {"pack_type": "resource", "name": "X"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未设置服务器目录")

        AppContext.instance().settings["server_dir"] = self.server_dir
        status, data = self._post_json("/api/packs/toggle", {"pack_type": "mod", "name": "X"})
        self.assertFalse(data["ok"])
        self.assertIn("resource 或 behavior", data["error"])

        status, data = self._post_json("/api/packs/toggle", {"pack_type": "RESOURCE"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "请指定包名")

        with patch.object(packs_routes, "set_pack_enabled", return_value=(True, "")) as mock_toggle:
            status, data = self._post_json(
                "/api/packs/toggle", {"pack_type": "Resource", "name": "DemoPack", "enabled": True}
            )
        self.assertTrue(data["ok"])
        self.assertIn("已启用", data["message"])
        self.assertEqual(mock_toggle.call_args.args, (self.server_dir, "resource", "DemoPack", True))

        with patch.object(packs_routes, "set_pack_enabled", return_value=(False, "包不存在")):
            status, data = self._post_json(
                "/api/packs/toggle", {"pack_type": "behavior", "name": "DemoPack", "enabled": False}
            )
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "包不存在")

    def test_packs_import(self):
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._post_json("/api/packs/import", {"pack_type": "resource"})
        self.assertFalse(data["ok"])

        AppContext.instance().settings["server_dir"] = self.server_dir
        status, data = self._post_json("/api/packs/import", {"pack_type": "mod", "file_data": "eA=="})
        self.assertFalse(data["ok"])
        self.assertIn("resource 或 behavior", data["error"])

        with patch.object(packs_routes, "import_pack", return_value=(True, "", "已导入 1 个包")) as mock_import:
            status, data = self._post_json(
                "/api/packs/import", {"pack_type": "resource", "file_data": "eA==", "file_name": "p.zip"}
            )
        self.assertTrue(data["ok"])
        self.assertEqual(data["message"], "已导入 1 个包")
        self.assertEqual(mock_import.call_args.args[0], self.server_dir)
        self.assertEqual(mock_import.call_args.args[1], "resource")

        with patch.object(packs_routes, "import_pack", return_value=(False, "解压失败", "")):
            status, data = self._post_json("/api/packs/import", {"pack_type": "resource", "file_data": "eA=="})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "解压失败")

    def test_packs_delete_requires_confirm(self):
        status, data = self._post_json("/api/packs/delete", {"pack_type": "resource", "name": "X"})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertTrue(data["need_confirm"])
        self.assertEqual(data["confirm_key"], "confirm_delete")

    def test_packs_delete_validation(self):
        AppContext.instance().settings["server_dir"] = ""
        status, data = self._post_json("/api/packs/delete", {"confirm_delete": "DELETE"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未设置服务器目录")

        AppContext.instance().settings["server_dir"] = self.server_dir
        status, data = self._post_json("/api/packs/delete", {"confirm_delete": "DELETE", "pack_type": "mod"})
        self.assertFalse(data["ok"])
        self.assertIn("resource 或 behavior", data["error"])

        status, data = self._post_json("/api/packs/delete", {"confirm_delete": "DELETE", "pack_type": "resource"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "请指定包名")

    def test_packs_delete_exec(self):
        with patch.object(packs_routes, "delete_pack", return_value=(True, "")) as mock_delete:
            status, data = self._post_json(
                "/api/packs/delete",
                {"confirm_delete": "DELETE", "pack_type": "resource", "name": "DemoPack"},
            )
        self.assertTrue(data["ok"])
        self.assertEqual(mock_delete.call_args.args, (self.server_dir, "resource", "DemoPack"))

        with patch.object(packs_routes, "delete_pack", return_value=(False, "包不存在")):
            status, data = self._post_json(
                "/api/packs/delete",
                {"confirm_delete": "DELETE", "pack_type": "behavior", "name": "Gone"},
            )
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "包不存在")

    # ==================================================================
    # players.py
    # ==================================================================

    def test_ipban_list_and_add(self):
        with open(os.path.join(self.server_dir, "banned-ips.json"), "w", encoding="utf-8") as f:
            json.dump([{"ip": "10.1.1.1", "reason": "测试"}], f)

        status, data = self._post_json("/api/ipban/list", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 1)

        # 缺 IP
        status, data = self._post_json("/api/ipban/add", {})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "请输入IP地址")

        with patch.object(players_routes, "ban_ip", return_value=(True, "")) as mock_ban:
            status, data = self._post_json("/api/ipban/add", {"ip": "10.2.2.2", "reason": "test"})
        self.assertTrue(data["ok"])
        self.assertIn("banned", data)
        mock_ban.assert_called_once_with("10.2.2.2", "test")

    def test_ipban_add_failure(self):
        with patch.object(players_routes, "ban_ip", return_value=(False, "IP 格式不合法")):
            status, data = self._post_json("/api/ipban/add", {"ip": "not-an-ip"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "IP 格式不合法")

    def test_ipban_remove(self):
        # 缺 IP
        status, data = self._post_json("/api/ipban/remove", {})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "请输入IP地址")

        with patch.object(players_routes, "pardon_ip", return_value=(True, "")) as mock_pardon:
            status, data = self._post_json("/api/ipban/remove", {"ip": "10.3.3.3"})
        self.assertTrue(data["ok"])
        self.assertIn("banned", data)
        mock_pardon.assert_called_once_with("10.3.3.3")

        with patch.object(players_routes, "pardon_ip", return_value=(False, "IP不在封禁列表中")):
            status, data = self._post_json("/api/ipban/remove", {"ip": "10.4.4.4"})
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "IP不在封禁列表中")

    def test_players_all(self):
        fake = [
            {"name": "Steve", "permission": "operator", "whitelisted": True, "banned": False},
            {"name": "Alex", "permission": "member", "whitelisted": False, "banned": True},
        ]
        with patch.object(players_routes, "get_all_player_status", return_value=fake) as mock_all:
            status, data = self._post_json("/api/players/all", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 2)
        self.assertEqual([p["name"] for p in data["players"]], ["Steve", "Alex"])
        mock_all.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
