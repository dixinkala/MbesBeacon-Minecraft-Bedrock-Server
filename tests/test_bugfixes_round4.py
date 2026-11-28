"""修复 4 个缺陷的回归测试。"""

import ctypes
import os
import tempfile
import unittest
from unittest import mock

from bedrock_server_manager import config, install, utils
from bedrock_server_manager.app_context import AppContext
from bedrock_server_manager.constants import IS_WINDOWS, SERVER_EXE


@unittest.skipUnless(IS_WINDOWS, "Win32 文件夹对话框仅 Windows")
class TestBrowseDirectoryWindows(unittest.TestCase):
    def _call(self, pidl, get_path):
        fake_windll = mock.MagicMock()
        fake_windll.shell32.SHBrowseForFolderW.return_value = pidl
        fake_windll.shell32.SHGetPathFromIDListW.side_effect = get_path
        with (
            mock.patch.object(ctypes, "windll", fake_windll),
            mock.patch.object(utils, "IS_WINDOWS", True),
        ):
            return utils.browse_directory()

    def test_success_returns_selected_path(self):
        def get_path(_pidl, path):
            path.value = r"C:\Chosen"
            return 1

        # 修复前 pszDisplayName = buf 会抛 TypeError（c_wchar_Array vs c_wchar_p）
        self.assertEqual(self._call(12345, get_path), r"C:\Chosen")

    def test_cancel_returns_none(self):
        self.assertIsNone(self._call(0, lambda *_: 1))

    def test_path_conversion_failure_returns_none(self):
        self.assertIsNone(self._call(12345, lambda *_: 0))


@unittest.skipUnless(IS_WINDOWS, "PE 版本资源仅 Windows")
class TestPeVersionResource(unittest.TestCase):
    def test_detect_server_version_reads_pe_resource(self):
        lang_arr = (ctypes.c_uint16 * 2)(0x0409, 0x04B0)
        value_buf = ctypes.create_unicode_buffer("1.21.0.03")
        version_api = mock.MagicMock()
        version_api.GetFileVersionInfoSizeW.return_value = 100
        version_api.GetFileVersionInfoW.return_value = 1

        def query(_buf, block, addr, length):
            if block == "\\VarFileInfo\\Translation":
                addr._obj.value = ctypes.addressof(lang_arr)
                length._obj.value = ctypes.sizeof(lang_arr)
                return 1
            if block.endswith("ProductVersion") or block.endswith("FileVersion"):
                addr._obj.value = ctypes.addressof(value_buf)
                length._obj.value = ctypes.sizeof(value_buf)
                return 1
            return 0

        version_api.VerQueryValueW.side_effect = query
        fake_ctx = mock.MagicMock()
        fake_ctx.settings = {"server_versions": {}}  # 需要真实 dict：后面会写回映射

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, SERVER_EXE), "wb") as handle:
                handle.write(b"MZ")
            with (
                mock.patch.object(ctypes, "windll", mock.MagicMock(version=version_api)),
                mock.patch.object(install, "IS_WINDOWS", True),
                mock.patch("bedrock_server_manager.utils.resolve_server_exe", return_value=SERVER_EXE),
                mock.patch.object(install, "save_settings"),
                mock.patch.object(AppContext, "instance", return_value=fake_ctx),
            ):
                version = install.detect_server_version(tmp)

        self.assertEqual(version, "1.21.0.03")
        self.assertEqual(list(fake_ctx.settings["server_versions"].values()), ["1.21.0.03"])


class TestIntegrityFailureAborts(unittest.TestCase):
    """校验失败必须直接失败，不能再进入"换源重试"后报 FileNotFoundError。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = os.path.join(self.tmp.name, "srv")
        os.makedirs(self.server)

    def tearDown(self):
        self.tmp.cleanup()

    def test_integrity_error_type_exists(self):
        self.assertTrue(issubclass(install.InstallIntegrityError, RuntimeError))

    def test_verify_failure_raises_integrity_error_without_extract(self):
        fake_state = mock.MagicMock()
        fake_state.cancel = mock.MagicMock()
        fake_state.cancel.is_set.return_value = False
        fake_ctx = mock.MagicMock()
        fake_ctx.install_state = fake_state
        fake_ctx.settings = {"server_dir": self.server, "installed_version": ""}

        def fake_download(url, dest, **kwargs):
            with open(dest, "wb") as handle:
                handle.write(b"zip")
            return 1

        verify_result = {"ok": False, "errors": ["sha256 mismatch"], "checks": {}, "warnings": []}

        with (
            mock.patch.object(AppContext, "instance", return_value=fake_ctx),
            mock.patch.object(install, "download_file", side_effect=fake_download) as download,
            mock.patch.object(install, "verify_download_full", return_value=verify_result),
            mock.patch.object(
                install,
                "get_official_version_info",
                return_value={"available": True, "known": True, "stable": "1.21.0.03"},
            ),
            mock.patch.object(install, "extract_zip") as extract,
            mock.patch("bedrock_server_manager.utils.check_disk_space", return_value=(True, 5000.0, 1200.0)),
        ):
            install.do_install(self.server, "1.21.0.03", autostart=False)

        # 修复后：首次校验失败即中止，不换源、不解压，错误信息是"校验失败"
        error_calls = [c for c in fake_state.set.call_args_list if c.kwargs.get("phase") == "error"]
        self.assertTrue(error_calls, "应进入 error 状态")
        final_error = error_calls[-1].kwargs.get("error", "")
        self.assertIn("sha256 mismatch", final_error)
        self.assertNotIn("FileNotFound", final_error)
        extract.assert_not_called()
        self.assertEqual(download.call_count, 1, "校验失败后不应继续尝试其他下载源")


class TestConfigBackupUniqueNames(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = os.path.join(self.tmp.name, "srv")
        os.makedirs(self.server)
        with open(os.path.join(self.server, "server.properties"), "w", encoding="utf-8") as handle:
            handle.write("server-name=A\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_second_backups_do_not_overwrite(self):
        first = config.backup_config_history(self.server)
        second = config.backup_config_history(self.server)
        self.assertNotEqual(os.path.basename(first), os.path.basename(second))
        self.assertEqual(len(config.list_config_history(self.server)), 2)
        self.assertTrue(os.path.isfile(first))
        self.assertTrue(os.path.isfile(second))

    def test_rollback_in_same_second_keeps_both_snapshots(self):
        first = config.backup_config_history(self.server)
        with open(os.path.join(self.server, "server.properties"), "w", encoding="utf-8") as handle:
            handle.write("server-name=B\n")
        ok, err = config.rollback_config(self.server, os.path.basename(first))
        self.assertTrue(ok, err)
        with open(os.path.join(self.server, "server.properties"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "server-name=A\n")
        # 回滚前的当前配置（B）也应留下独立备份，而不是覆盖 A
        contents = []
        for entry in config.list_config_history(self.server):
            with open(entry["path"], encoding="utf-8") as handle:
                contents.append(handle.read())
        self.assertIn("server-name=A\n", contents)
        self.assertIn("server-name=B\n", contents)


if __name__ == "__main__":
    unittest.main(verbosity=2)
