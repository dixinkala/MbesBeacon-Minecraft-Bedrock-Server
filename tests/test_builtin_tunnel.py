"""内置穿透工具测试：资产选择、下载校验、解压、配置渲染、领取与状态。"""

import hashlib
import io
import os
import tarfile
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import TestCase, main, mock

from bedrock_server_manager import builtin_tunnel as bt


class TestPlatformAndAssets(TestCase):
    def test_windows_assets(self):
        with mock.patch.object(bt, "IS_WINDOWS", True), mock.patch.object(bt.platform, "machine", return_value="AMD64"):
            self.assertEqual(bt.platform_tag(), ("windows", "x86_64"))
            self.assertEqual(
                bt.playit_asset_names(),
                ("playit-windows-x86_64-signed.exe", "playitd-windows-x86_64-signed.exe"),
            )
            self.assertEqual(bt.frp_asset_name("0.72.0"), "frp_0.72.0_windows_amd64.zip")

    def test_linux_amd64_assets(self):
        with (
            mock.patch.object(bt, "IS_WINDOWS", False),
            mock.patch.object(bt.platform, "machine", return_value="x86_64"),
        ):
            self.assertEqual(bt.platform_tag(), ("linux", "amd64"))
            self.assertEqual(bt.playit_asset_names(), ("playit-cli-linux-amd64", "playit-linux-amd64"))
            self.assertEqual(bt.frp_asset_name("0.72.0"), "frp_0.72.0_linux_amd64.tar.gz")

    def test_unsupported_platform(self):
        with (
            mock.patch.object(bt, "IS_WINDOWS", False),
            mock.patch.object(bt.platform, "machine", return_value="mips"),
            self.assertRaises(bt.BuiltinTunnelError),
        ):
            bt.playit_asset_names()

    def test_asset_digest(self):
        self.assertEqual(bt.asset_digest({"digest": "sha256:ABC123"}), "abc123")
        self.assertEqual(bt.asset_digest({"digest": ""}), "")
        self.assertEqual(bt.asset_digest({}), "")

    def test_find_asset(self):
        release = {"assets": [{"name": "a", "digest": "sha256:11"}, {"name": "b"}]}
        self.assertEqual(bt.find_asset(release, "b")["name"], "b")
        with self.assertRaises(bt.BuiltinTunnelError):
            bt.find_asset(release, "missing")


class _FileHandler(BaseHTTPRequestHandler):
    payload = b"hello-builtin"

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)


class TestDownload(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FileHandler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/file"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_download_ok(self):
        expected = hashlib.sha256(_FileHandler.payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = bt.download_file(self.url, tmp, expected)
            self.assertTrue(os.path.isfile(path))
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), _FileHandler.payload)

    def test_download_checksum_mismatch_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.download_file(self.url, tmp, "0" * 64)
            self.assertEqual(os.listdir(tmp), [])


class TestExtraction(TestCase):
    def test_extract_from_zip_ignores_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "frp.zip")
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr(f"frp_0.1/frpc{os.path.splitext(bt._exe('frpc'))[1]}", b"FAKE-FRPC")
                zf.writestr("frp_0.1/frps.exe", b"FAKE-FRPS")
                zf.writestr("../../evil.exe", b"EVIL")
            target_dir = os.path.join(tmp, "out")
            target = bt.extract_frpc(archive, target_dir)
            self.assertTrue(target.endswith(bt._exe("frpc")))
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"FAKE-FRPC")
            self.assertFalse(os.path.exists(os.path.join(target_dir, "frps.exe")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "..", "evil.exe")))

    def test_extract_from_tar(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "frp.tar.gz")
            data = b"FAKE-FRPC-TAR"
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo("frp_0.1_linux_amd64/frpc")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            with mock.patch.object(bt, "IS_WINDOWS", False):
                target_dir = os.path.join(tmp, "out")
                target = bt.extract_frpc(archive, target_dir)
                self.assertTrue(target.endswith("frpc"))
                with open(target, "rb") as handle:
                    self.assertEqual(handle.read(), data)

    def test_missing_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "empty.zip")
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("frp/readme.txt", b"x")
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.extract_frpc(archive, os.path.join(tmp, "out"))


class TestFrpConfig(TestCase):
    def setUp(self):
        from bedrock_server_manager.app_context import AppContext

        self.settings = AppContext.instance().settings
        self._saved = dict(self.settings)
        self.settings.pop("frp_config", None)

    def tearDown(self):
        self.settings.clear()
        self.settings.update(self._saved)

    def test_render_toml(self):
        toml = bt.render_frp_toml(
            {"server_addr": "1.2.3.4", "server_port": 7000, "token": 'a"b', "local_port": 19132, "remote_port": 19133}
        )
        self.assertIn('serverAddr = "1.2.3.4"', toml)
        self.assertIn("serverPort = 7000", toml)
        self.assertIn('auth.token = "a\\"b"', toml)
        self.assertIn('type = "udp"', toml)
        self.assertIn("localPort = 19132", toml)
        self.assertIn("remotePort = 19133", toml)

    def test_render_requires_server(self):
        with self.assertRaises(bt.BuiltinTunnelError):
            bt.render_frp_toml({"server_addr": ""})

    def test_save_validates(self):
        with mock.patch("bedrock_server_manager.utils.save_settings", return_value=True):
            cfg = bt.save_frp_config(server_addr="frp.example.com", server_port=7000, remote_port=19132)
        self.assertEqual(cfg["server_addr"], "frp.example.com")
        with mock.patch("bedrock_server_manager.utils.save_settings", return_value=True):
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.save_frp_config(server_port=70000)
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.save_frp_config(server_addr="bad host!")

    def test_defaults(self):
        self.assertEqual(bt.frp_config()["remote_port"], 19132)


class TestPlayitHelpers(TestCase):
    def test_secret_path_parsing(self):
        manager = bt.PlayitManager()
        fake = mock.Mock(stdout="C:\\ProgramData\\playit_gg\\playit.toml\n", stderr="")
        with (
            mock.patch.object(bt, "secret_candidates", return_value=[]),
            mock.patch.object(bt, "playit_paths", return_value={"cli": "x", "daemon": "y", "log": "z"}),
            mock.patch.object(bt.os.path, "isfile", return_value=True),
            mock.patch.object(bt, "_run_once", return_value=fake),
        ):
            self.assertTrue(manager.secret_path().endswith("playit.toml"))

    def test_secret_path_prefers_existing_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = os.path.join(tmp, "playit.toml")
            with open(secret, "w", encoding="utf-8") as handle:
                handle.write("secret = 'x'")
            with mock.patch.object(bt, "secret_candidates", return_value=[secret]):
                self.assertEqual(bt.PlayitManager().secret_path(), secret)

    def test_claim_start_builds_url(self):
        manager = bt.PlayitManager()
        gen = mock.Mock(stdout="abc123\n", stderr="")
        url = mock.Mock(stdout="https://playit.gg/claim/abc123\n", stderr="")
        with (
            mock.patch.object(bt, "playit_paths", return_value={"cli": "x", "daemon": "y", "log": "z"}),
            mock.patch.object(bt.os.path, "isfile", return_value=True),
            mock.patch.object(bt, "_run_once", side_effect=[gen, url]),
            mock.patch.object(bt.PlayitManager, "_save"),
        ):
            result = manager.claim_start()
        self.assertEqual(result["code"], "abc123")
        self.assertEqual(result["url"], "https://playit.gg/claim/abc123")

    def test_line_buffer_detects_address(self):
        buffer = bt._LineBuffer()
        buffer.add("[INFO] starting")
        buffer.add("[INFO] tunnel available at demo.playit.gg:25565")
        self.assertEqual(buffer.address, "demo.playit.gg:25565")
        self.assertGreaterEqual(len(buffer.tail()), 2)

    def test_status_shape(self):
        manager = bt.PlayitManager()
        with mock.patch.object(bt, "playit_installed", return_value=False):
            status = manager.status()
        for key in ("installed", "claimed", "running", "address", "lines"):
            self.assertIn(key, status)

    def test_start_requires_claim(self):
        manager = bt.PlayitManager()
        with (
            mock.patch.object(bt, "playit_installed", return_value=True),
            mock.patch.object(manager, "secret_path", return_value=""),
            self.assertRaises(bt.BuiltinTunnelError),
        ):
            manager.start()


class TestFrpManager(TestCase):
    def test_start_requires_install(self):
        manager = bt.FrpManager()
        with (
            mock.patch.object(bt, "frp_installed", return_value=False),
            self.assertRaises(bt.BuiltinTunnelError),
        ):
            manager.start()

    def test_start_requires_server(self):
        manager = bt.FrpManager()
        with (
            mock.patch.object(bt, "frp_installed", return_value=True),
            mock.patch.object(bt, "frp_config", return_value={"server_addr": ""}),
            self.assertRaises(bt.BuiltinTunnelError),
        ):
            manager.start()

    def test_frp_permission_error_hint(self):
        """杀毒软件拦截时给出可操作提示，而不是裸 WinError 5。"""
        manager = bt.FrpManager()
        with (
            mock.patch.object(bt, "frp_installed", return_value=True),
            mock.patch.object(bt, "frp_config", return_value={"server_addr": "frp.example.com", "server_port": 7000}),
            mock.patch.object(bt.subprocess, "Popen", side_effect=PermissionError("Access is denied")),
            mock.patch.object(bt.os, "makedirs"),
            mock.patch("builtins.open", mock.mock_open()),
            self.assertRaises(bt.BuiltinTunnelError) as ctx,
        ):
            manager.start()
        self.assertIn("杀毒", str(ctx.exception))

    def test_address_derived_from_config(self):
        manager = bt.FrpManager()
        with (
            mock.patch.object(bt, "frp_config", return_value={"server_addr": "frp.example.com", "remote_port": 19133}),
            mock.patch.object(bt.FrpManager, "running", return_value=True),
        ):
            self.assertEqual(manager.address(), "frp.example.com:19133")


if __name__ == "__main__":
    main(verbosity=2)
