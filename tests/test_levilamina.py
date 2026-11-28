"""LeviLamina 集成测试：版本映射、mods 列表/启停/删除、安装、启用/禁用、lip 调用。"""

import io
import json
import os
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import TestCase, main, mock

from bedrock_server_manager import levilamina as L
from bedrock_server_manager.constants import SERVER_EXE
from bedrock_server_manager.utils import MODDED_SERVER_EXE, resolve_server_exe


def _write(path: str, data: str = "x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(data)


def _manifest(name: str, version: str = "1.0.0", type_: str = "native") -> str:
    return json.dumps({"name": name, "version": version, "type": type_})


class TestVersionMapping(TestCase):
    def test_four_segment_trimmed(self):
        self.assertEqual(L.game_version_for_lip("1.26.60.6"), "1.26.60")

    def test_three_segment_kept(self):
        self.assertEqual(L.game_version_for_lip("1.21.0"), "1.21.0")

    def test_leading_v_and_spaces(self):
        self.assertEqual(L.game_version_for_lip(" v1.21.0.3 "), "1.21.0")

    def test_invalid(self):
        for bad in ("", "abc", "1.2", "1.x.0"):
            with self.assertRaises(L.LeviLaminaError):
                L.game_version_for_lip(bad)


class TestResolveServerExe(TestCase):
    def test_prefers_modded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, SERVER_EXE))
            _write(os.path.join(tmp, L.MODDED_EXE))
            self.assertEqual(resolve_server_exe(tmp), MODDED_SERVER_EXE)

    def test_falls_back_to_official(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, SERVER_EXE))
            self.assertEqual(resolve_server_exe(tmp), SERVER_EXE)

    def test_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(resolve_server_exe(tmp), "")


class TestState(TestCase):
    def test_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, SERVER_EXE))
            st = L.state(tmp)
        self.assertFalse(st["installed"])
        self.assertFalse(st["enabled"])

    def test_installed_and_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, L.MODDED_EXE))
            _write(os.path.join(tmp, ".lip", "x"), "")
            _write(os.path.join(tmp, L.MODS_DIR, "LeviLamina", "manifest.json"), _manifest("LeviLamina", "26.60.9"))
            st = L.state(tmp)
        self.assertTrue(st["installed"])
        self.assertTrue(st["enabled"])
        self.assertEqual(st["loader_version"], "26.60.9")

    def test_disabled_when_official_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, L.MODDED_EXE))
            _write(os.path.join(tmp, SERVER_EXE))
            st = L.state(tmp)
        self.assertTrue(st["installed"])
        self.assertFalse(st["enabled"])


class TestModsListing(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        _write(os.path.join(self.dir, L.MODS_DIR, "LeviLamina", "manifest.json"), _manifest("LeviLamina", "1.0"))
        _write(os.path.join(self.dir, L.MODS_DIR, "Alpha", "manifest.json"), _manifest("Alpha", "2.1.0", "native"))
        _write(os.path.join(self.dir, L.MODS_DIR, "loose.dll"), "dll")
        _write(os.path.join(self.dir, L.DISABLED_DIR, "Beta", "manifest.json"), _manifest("Beta", "0.3.0"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_enabled_and_disabled(self):
        mods = {m["entry"]: m for m in L.list_mods(self.dir)}
        self.assertNotIn("LeviLamina", mods)
        self.assertIn("Alpha", mods)
        self.assertTrue(mods["Alpha"]["enabled"])
        self.assertEqual(mods["Alpha"]["version"], "2.1.0")
        self.assertIn("loose.dll", mods)
        self.assertIn("Beta", mods)
        self.assertFalse(mods["Beta"]["enabled"])

    def test_toggle(self):
        result = L.toggle_mod(self.dir, "Alpha", enabled=False)
        self.assertFalse(result["enabled"])
        self.assertTrue(os.path.isdir(os.path.join(self.dir, L.DISABLED_DIR, "Alpha")))
        with self.assertRaises(L.LeviLaminaError):
            L.toggle_mod(self.dir, "Alpha", enabled=False)  # 已在禁用区
        L.toggle_mod(self.dir, "Alpha", enabled=True)
        self.assertTrue(os.path.isdir(os.path.join(self.dir, L.MODS_DIR, "Alpha")))

    def test_toggle_invalid_name(self):
        with self.assertRaises(L.LeviLaminaError):
            L.toggle_mod(self.dir, "../etc", enabled=False)

    def test_delete(self):
        L.delete_mod(self.dir, "loose.dll")
        self.assertFalse(os.path.exists(os.path.join(self.dir, L.MODS_DIR, "loose.dll")))
        with self.assertRaises(L.LeviLaminaError):
            L.delete_mod(self.dir, "missing")


class TestInstallModZip(TestCase):
    def _zip(self, path: str, members: dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)

    def test_extract_plain(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "MyMod.zip")
            self._zip(zip_path, {"manifest.json": _manifest("MyMod", "1.2.3"), "MyMod.dll": "dll"})
            result = L.install_mod_zip(tmp, zip_path)
            self.assertTrue(result["installed"])
            self.assertTrue(os.path.isfile(os.path.join(tmp, L.MODS_DIR, "MyMod", "MyMod.dll")))

    def test_display_name_overrides_temp_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "mod-abc123.zip")
            self._zip(zip_path, {"manifest.json": _manifest("Named")})
            result = L.install_mod_zip(tmp, zip_path, display_name="Named.zip")
            self.assertEqual(result["name"], "Named")
            self.assertTrue(os.path.isdir(os.path.join(tmp, L.MODS_DIR, "Named")))

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "evil.zip")
            self._zip(zip_path, {"../../evil.txt": "x"})
            with self.assertRaises(L.LeviLaminaError):
                L.install_mod_zip(tmp, zip_path)
            self.assertFalse(os.path.exists(os.path.join(tmp, "..", "evil.txt")))

    def test_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "Dup.zip")
            self._zip(zip_path, {"manifest.json": _manifest("Dup")})
            L.install_mod_zip(tmp, zip_path)
            with self.assertRaises(L.LeviLaminaError):
                L.install_mod_zip(tmp, zip_path)

    def test_invalid_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.zip")
            _write(bad, "not a zip")
            with self.assertRaises(L.LeviLaminaError):
                L.install_mod_zip(tmp, bad)

    def test_uses_lip_in_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, ".lip", "x"), "")
            zip_path = os.path.join(tmp, "Mod.zip")
            self._zip(zip_path, {"manifest.json": _manifest("Mod")})
            with (
                mock.patch.object(L, "lip_installed", return_value=True),
                mock.patch.object(L, "run_lip", return_value=(0, "ok")) as mocked,
            ):
                result = L.install_mod_zip(tmp, zip_path)
        self.assertEqual(result["via"], "lip")
        self.assertEqual(mocked.call_args[0][0][:1], ["install"])


class TestEnableDisable(TestCase):
    def test_enable_requires_support_and_lip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(L, "supported", return_value=False),
                self.assertRaises(L.LeviLaminaError),
            ):
                L.enable_loader(tmp, "1.26.60.6")
            with (
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(L, "lip_installed", return_value=False),
                self.assertRaises(L.LeviLaminaError),
            ):
                L.enable_loader(tmp, "1.26.60.6")

    def test_enable_rejects_already_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, L.MODDED_EXE))
            with (
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(L, "lip_installed", return_value=True),
                self.assertRaises(L.LeviLaminaError),
            ):
                L.enable_loader(tmp, "1.26.60.6")

    def test_backup_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "server.properties"), "server-port=19132")
            target = L._backup_configs(tmp)
            self.assertTrue(os.path.isfile(os.path.join(target, "server.properties")))

    def test_enable_job_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, SERVER_EXE))

            def fake_run(argv, cwd, on_line, timeout=0):
                _write(os.path.join(cwd, L.MODDED_EXE))
                on_line("lip setup done")
                return 0, ["lip setup done"]

            with (
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(L, "lip_installed", return_value=True),
                mock.patch.object(L, "_run_stream", side_effect=fake_run),
            ):
                L.enable_loader(tmp, "1.26.60.6")
                deadline = time.time() + 5
                while time.time() < deadline and not L.job.status()["done"]:
                    time.sleep(0.05)
            status = L.job.status()
            self.assertTrue(status["ok"], status)
            self.assertFalse(status["running"])

    def test_enable_job_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, SERVER_EXE))
            with (
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(L, "lip_installed", return_value=True),
                mock.patch.object(L, "_run_stream", return_value=(1, ["error"])),
            ):
                L.enable_loader(tmp, "1.26.60.6")
                deadline = time.time() + 5
                while time.time() < deadline and not L.job.status()["done"]:
                    time.sleep(0.05)
            self.assertFalse(L.job.status()["ok"])
            self.assertIn("退出码", L.job.status()["error"])

    def test_disable_restores_official(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, L.MODDED_EXE))
            _write(os.path.join(tmp, L.OFFICIAL_EXE + ".bak"))
            L.disable_loader(tmp)
            self.assertTrue(os.path.isfile(os.path.join(tmp, L.OFFICIAL_EXE)))
            self.assertTrue(os.path.isfile(os.path.join(tmp, L.MODDED_EXE + ".disabled")))

    def test_disable_without_loader(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(L.LeviLaminaError):
            L.disable_loader(tmp)


class TestLipList(TestCase):
    def test_parses_json(self):
        payload = {"ExplicitInstalled": ["github.com/LiteLDev/LeviLamina@26.60.9"]}
        with mock.patch.object(L, "run_lip", return_value=(0, json.dumps(payload))):
            self.assertEqual(L.lip_list("/tmp"), payload)

    def test_bad_output(self):
        with (
            mock.patch.object(L, "run_lip", return_value=(0, "not json")),
            self.assertRaises(L.LeviLaminaError),
        ):
            L.lip_list("/tmp")


class _ZipHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", _manifest("Remote"))
        data = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestInstallModUrl(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _ZipHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_requires_https(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(L.LeviLaminaError):
            L.install_mod_url(tmp, "http://example.com/mod.zip")

    def test_download_and_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(L, "LIP_DIR", os.path.join(tmp, "tools")),
                mock.patch.object(L.urllib.request, "urlopen", side_effect=_fake_urlopen),
            ):
                result = L.install_mod_url(tmp, "https://example.com/Remote.zip")
            self.assertTrue(result["installed"])
            self.assertTrue(os.path.isdir(os.path.join(tmp, L.MODS_DIR, "Remote")))


def _fake_urlopen(url, timeout=None):  # noqa: ARG001
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", _manifest("Remote"))
    data = buf.getvalue()

    class _Resp:
        def __init__(self):
            self._stream = io.BytesIO(data)

        def read(self, n=-1):
            return self._stream.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return _Resp()


if __name__ == "__main__":
    main(verbosity=2)
