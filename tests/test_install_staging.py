"""A1/A2/A3 回归测试：安装暂存目录、版本号净化、世界导入隔离。

放到独立文件避免与既有测试文件互相影响。
"""

import os
import shutil
import tempfile
import unittest
import zipfile


class TestValidateVersion(unittest.TestCase):
    """A2：版本号必须净化（会拼进下载 URL 与文件名）"""

    def test_accepts_normal_versions(self):
        from bedrock_server_manager.install import validate_version

        for v in ("1.21.0.03", "1.21.0", "1.20.80.05", "", "1.21.0-rc1", "1.21.0_beta"):
            ok, err = validate_version(v)
            self.assertTrue(ok, f"{v!r} 应通过: {err}")

    def test_rejects_traversal(self):
        from bedrock_server_manager.install import validate_version

        for v in ("../x", "../../../EVIL", "..", "a/b", "1.0;rm", "1.0\\x", "v1.0", "-1.0", " "):
            ok, _err = validate_version(v)
            self.assertFalse(ok, f"{v!r} 应被拒绝")

    def test_rejects_overlong(self):
        from bedrock_server_manager.install import validate_version

        ok, _err = validate_version("1" + "0" * 40)
        self.assertFalse(ok)


class TestApplyStagedInstall(unittest.TestCase):
    """A1：暂存目录并入服务器目录时保留用户数据"""

    def setUp(self):
        from bedrock_server_manager.constants import SERVER_EXE

        self.exe = SERVER_EXE
        self.tmp = tempfile.mkdtemp(prefix="mbes_stage_")
        self.target = os.path.join(self.tmp, "server")
        self.staging = os.path.join(self.tmp, "staged")
        os.makedirs(self.target)
        os.makedirs(self.staging)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, root, rel, content="x"):
        p = os.path.join(root, rel)
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)

    def test_preserves_user_data_on_update(self):
        """server.properties / 白名单 / 世界不应被官方默认文件覆盖"""
        from bedrock_server_manager.install import apply_staged_install

        # 用户已有数据
        self._write(self.target, "server.properties", "server-name=MyServer\n")
        self._write(self.target, "allowlist.json", '[{"name":"Steve"}]')
        os.makedirs(os.path.join(self.target, "worlds", "MyWorld"))
        self._write(self.target, "worlds/MyWorld/level.dat", "world")

        # 官方 ZIP 内容（含同名默认文件）
        self._write(self.staging, "server.properties", "server-name=Dedicated Server\n")
        self._write(self.staging, "allowlist.json", "[]")
        os.makedirs(os.path.join(self.staging, "worlds"))
        self._write(self.staging, self.exe, "new-binary")

        skipped = apply_staged_install(self.staging, self.target)

        with open(os.path.join(self.target, "server.properties"), encoding="utf-8") as f:
            self.assertIn("MyServer", f.read(), "用户配置被官方默认值覆盖了")
        with open(os.path.join(self.target, "allowlist.json"), encoding="utf-8") as f:
            self.assertIn("Steve", f.read(), "白名单被清空了")
        self.assertTrue(os.path.isfile(os.path.join(self.target, "worlds", "MyWorld", "level.dat")))
        # 二进制应更新
        with open(os.path.join(self.target, self.exe), encoding="utf-8") as f:
            self.assertEqual(f.read(), "new-binary")
        self.assertIn("server.properties", skipped)

    def test_fresh_install_copies_everything(self):
        from bedrock_server_manager.install import apply_staged_install

        self._write(self.staging, "server.properties", "server-name=Default\n")
        self._write(self.staging, self.exe, "bin")
        empty = os.path.join(self.tmp, "fresh")
        os.makedirs(empty)
        skipped = apply_staged_install(self.staging, empty)
        self.assertEqual(skipped, [])
        self.assertTrue(os.path.isfile(os.path.join(empty, "server.properties")))
        self.assertTrue(os.path.isfile(os.path.join(empty, self.exe)))

    def test_replaces_stale_dll(self):
        """非用户数据的旧文件应被替换"""
        from bedrock_server_manager.install import apply_staged_install

        # 合法 staging 必含服务端可执行文件（apply 有完整性守卫）
        self._write(self.staging, self.exe, "exe")
        self._write(self.target, "libCrypto.dll", "old")
        self._write(self.staging, "libCrypto.dll", "new")
        apply_staged_install(self.staging, self.target)
        with open(os.path.join(self.target, "libCrypto.dll"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "new")


class TestStagingCleanup(unittest.TestCase):
    """A1：失败/取消后暂存目录必须清掉，且原服务器文件仍在"""

    def test_cleanup_staging_idempotent(self):
        from bedrock_server_manager.install import STAGING_DIRNAME, cleanup_staging

        tmp = tempfile.mkdtemp(prefix="mbes_clean_")
        try:
            os.makedirs(os.path.join(tmp, STAGING_DIRNAME, "extracted"))
            cleanup_staging(tmp)
            self.assertFalse(os.path.exists(os.path.join(tmp, STAGING_DIRNAME)))
            cleanup_staging(tmp)  # 再调一次不应抛错
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestFailedInstallKeepsServer(unittest.TestCase):
    """A1 关键断言：下载失败时原服务器 exe 仍在"""

    def test_failed_download_preserves_existing_server(self):
        from unittest.mock import patch

        from bedrock_server_manager.install import do_install

        tmp = tempfile.mkdtemp(prefix="mbes_keep_")
        try:
            from bedrock_server_manager.constants import SERVER_EXE

            exe = os.path.join(tmp, SERVER_EXE)
            with open(exe, "w") as f:
                f.write("original-binary")
            with open(os.path.join(tmp, "server.properties"), "w", encoding="utf-8") as f:
                f.write("server-name=KeepMe\n")

            with (
                patch("bedrock_server_manager.install.download_file", side_effect=OSError("network down")),
                patch("bedrock_server_manager.install.server_running", return_value=False),
            ):
                do_install(tmp, "1.21.0.03", autostart=False)

            self.assertTrue(os.path.isfile(exe), "下载失败后服务器程序被删除了")
            with open(exe, encoding="utf-8") as f:
                self.assertEqual(f.read(), "original-binary")
            with open(os.path.join(tmp, "server.properties"), encoding="utf-8") as f:
                self.assertIn("KeepMe", f.read())
            # 暂存目录不应残留
            self.assertFalse(os.path.exists(os.path.join(tmp, ".install_staging")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestWorldImportIsolation(unittest.TestCase):
    """A3：世界导入不得覆盖服务器根目录文件"""

    def setUp(self):
        from bedrock_server_manager.constants import SERVER_EXE

        self.tmp = tempfile.mkdtemp(prefix="mbes_wimp_")
        self.server = os.path.join(self.tmp, "server")
        os.makedirs(os.path.join(self.server, "worlds"))
        with open(os.path.join(self.server, "server.properties"), "w", encoding="utf-8") as f:
            f.write("server-name=Original\n")
        with open(os.path.join(self.server, SERVER_EXE), "w") as f:
            f.write("real-binary")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _zip_with(self, members):
        zpath = os.path.join(self.tmp, "in.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        return zpath

    def test_locate_worlds_subdir(self):
        from bedrock_server_manager.web.routes.worlds import _locate_imported_worlds

        root = os.path.join(self.tmp, "x1")
        os.makedirs(os.path.join(root, "worlds", "W"))
        self.assertEqual(_locate_imported_worlds(root), os.path.join(root, "worlds"))

    def test_locate_single_world_dir(self):
        from bedrock_server_manager.web.routes.worlds import _locate_imported_worlds

        root = os.path.join(self.tmp, "x2")
        os.makedirs(os.path.join(root, "W"))
        with open(os.path.join(root, "W", "level.dat"), "w") as f:
            f.write("x")
        self.assertEqual(_locate_imported_worlds(root), root)

    def test_locate_bare_root_world(self):
        from bedrock_server_manager.web.routes.worlds import _locate_imported_worlds

        root = os.path.join(self.tmp, "x3")
        os.makedirs(root)
        with open(os.path.join(root, "level.dat"), "w") as f:
            f.write("x")
        located = _locate_imported_worlds(root)
        self.assertIsNotNone(located)
        self.assertTrue(any("ImportedWorld" in n for n in os.listdir(located)))

    def test_locate_returns_none_for_junk(self):
        from bedrock_server_manager.web.routes.worlds import _locate_imported_worlds

        root = os.path.join(self.tmp, "x4")
        os.makedirs(root)
        with open(os.path.join(root, "readme.txt"), "w") as f:
            f.write("nothing")
        self.assertIsNone(_locate_imported_worlds(root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
