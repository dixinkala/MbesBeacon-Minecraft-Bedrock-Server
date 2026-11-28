"""不可逆操作的回归测试：删除服务器、恢复/删除备份、世界重命名/复制/删除。

这些操作一旦出错就是数据丢失，测试重点是：

- 两种删除模式的保留/清理边界（server_only 不碰用户数据与审计/计划任务）
- 危险目录与非法模式的拦截
- 恢复失败必须回滚，不能同时丢新旧存档
- 世界重命名活动世界时配置同步、失败回滚
"""

import os
import tempfile
import zipfile
from pathlib import Path
from unittest import TestCase, main, mock

from bedrock_server_manager import backup, worlds
from bedrock_server_manager.config import (
    ALLOWLIST_FILE,
    AUDIT_LOG_NAME,
    BANNED_IPS_FILE,
    BANNED_PLAYERS_FILE,
    CONFIG_HISTORY_DIR,
    PACKS_CONFIG,
    PERMISSIONS_FILE,
    SCHEDULED_TASKS_FILE,
)
from bedrock_server_manager.constants import SERVER_EXE


class FakeProc:
    def __init__(self, server_dir="", running=False):
        self.server_dir = server_dir
        self.running = running
        self.stop_calls = 0

    def stop(self, wait=10):
        self.stop_calls += 1
        self.running = False


def _write(path: str, data: str = "x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text(data, encoding="utf-8")


def _make_server(root: str, name: str = "srv") -> str:
    """构造一个"已安装服务器"目录。"""
    server = os.path.join(root, name)
    _write(os.path.join(server, SERVER_EXE), "exe")
    _write(os.path.join(server, "server.properties"), "level-name=World\nserver-port=19132\n")
    _write(os.path.join(server, "worlds", "World", "level.dat"), "world")
    return server


class TestDeleteServer(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.server = _make_server(self.root)
        self.console = mock.MagicMock()
        self.proc = FakeProc(self.server, running=False)
        self.patches = [
            mock.patch.object(backup, "_get_console", return_value=self.console),
            mock.patch.object(backup, "get_server_proc", return_value=self.proc),
            mock.patch.object(backup, "resolve_server_dir", return_value=""),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def test_rejects_unknown_mode(self):
        with self.assertRaises(RuntimeError):
            backup.delete_server(self.server, "delete_everything")
        self.assertTrue(os.path.isdir(self.server), "非法模式不能删任何东西")

    def test_rejects_missing_directory(self):
        with self.assertRaises(RuntimeError):
            backup.delete_server(os.path.join(self.root, "missing"), "server_only")

    def test_rejects_dangerous_target_in_both_modes(self):
        for mode in ("server_only", "full"):
            with (
                mock.patch.object(backup, "is_dangerous_target", return_value=True),
                self.assertRaises(RuntimeError),
            ):
                backup.delete_server(self.server, mode)
            self.assertTrue(os.path.isdir(self.server))

    def test_full_requires_server_or_configured_dir(self):
        plain = os.path.join(self.root, "plain")
        _write(os.path.join(plain, "notes.txt"))
        with self.assertRaises(RuntimeError):
            backup.delete_server(plain, "full")
        self.assertTrue(os.path.isdir(plain), "非服务器目录不能被 full 删除")

    def test_full_deletes_configured_directory(self):
        with mock.patch.object(backup, "resolve_server_dir", return_value=self.server):
            self.assertTrue(backup.delete_server(self.server, "full"))
        self.assertFalse(os.path.isdir(self.server))

    def test_server_only_keeps_user_data_and_removes_program(self):
        keep_files = [
            "server.properties",
            ALLOWLIST_FILE,
            PERMISSIONS_FILE,
            BANNED_PLAYERS_FILE,
            BANNED_IPS_FILE,
            PACKS_CONFIG,
            AUDIT_LOG_NAME,
            SCHEDULED_TASKS_FILE,
            "release-notes.txt",
        ]
        for name in keep_files:
            _write(os.path.join(self.server, name), "keep")
        os.makedirs(os.path.join(self.server, CONFIG_HISTORY_DIR, "snap1"), exist_ok=True)
        _write(os.path.join(self.server, "server_console.log"), "log")
        _write(os.path.join(self.server, "server_console.1.log"), "rotated")
        _write(os.path.join(self.server, "third_party.dll"), "remove me")
        os.makedirs(os.path.join(self.server, "junk_dir"), exist_ok=True)

        self.assertTrue(backup.delete_server(self.server, "server_only"))

        for name in keep_files:
            self.assertTrue(os.path.exists(os.path.join(self.server, name)), f"{name} 应保留")
        self.assertTrue(os.path.isdir(os.path.join(self.server, "worlds", "World")))
        self.assertTrue(os.path.isdir(os.path.join(self.server, CONFIG_HISTORY_DIR)))
        self.assertTrue(os.path.exists(os.path.join(self.server, "server_console.1.log")))
        self.assertFalse(os.path.exists(os.path.join(self.server, SERVER_EXE)), "服务端程序应删除")
        self.assertFalse(os.path.exists(os.path.join(self.server, "third_party.dll")))
        self.assertFalse(os.path.exists(os.path.join(self.server, "junk_dir")))

    def test_stops_running_server_before_delete(self):
        self.proc.running = True
        self.assertTrue(backup.delete_server(self.server, "server_only"))
        self.assertEqual(self.proc.stop_calls, 1)

    def test_does_not_stop_other_running_server(self):
        self.proc.running = True
        self.proc.server_dir = os.path.join(self.root, "other")
        self.assertTrue(backup.delete_server(self.server, "server_only"))
        self.assertEqual(self.proc.stop_calls, 0)


class TestRestoreBackup(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.server = _make_server(self.root)
        self.backup_root = backup.get_backup_root(self.server)
        os.makedirs(self.backup_root, exist_ok=True)
        self.console = mock.MagicMock()
        self.proc = FakeProc(self.server, running=False)
        self.patches = [
            mock.patch.object(backup, "_get_console", return_value=self.console),
            mock.patch.object(backup, "get_server_proc", return_value=self.proc),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def _dir_backup(self, name="srv_worlds_20260101_000000", world="BackupWorld"):
        target = os.path.join(self.backup_root, name)
        _write(os.path.join(target, world, "level.dat"), "restored")
        return target

    def test_missing_backup(self):
        ok, err = backup.restore_backup(os.path.join(self.backup_root, "nope"), self.server)
        self.assertFalse(ok)
        self.assertTrue(err)

    def test_rejects_path_outside_backup_root(self):
        outside = _make_server(self.root, "other_srv")  # 另一个目录
        ok, err = backup.restore_backup(outside, self.server)
        self.assertFalse(ok)
        self.assertTrue("非法路径" in err or "Invalid path" in err, err)

    def test_rejects_backup_root_itself(self):
        ok, _err = backup.restore_backup(self.backup_root, self.server)
        self.assertFalse(ok)

    def test_restore_directory_backup_replaces_worlds(self):
        backup_dir = self._dir_backup()
        ok, err = backup.restore_backup(backup_dir, self.server)
        self.assertTrue(ok, err)
        self.assertFalse(os.path.isdir(os.path.join(self.server, "worlds", "World")))
        self.assertTrue(os.path.isfile(os.path.join(self.server, "worlds", "BackupWorld", "level.dat")))
        leftovers = [d for d in os.listdir(self.server) if d.startswith("worlds_restore_tmp_")]
        self.assertEqual(leftovers, [], "恢复成功后不应留下临时目录")

    def test_restore_zip_backup(self):
        zip_path = os.path.join(self.backup_root, "srv_worlds_20260102_000000.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("worlds/ZipWorld/level.dat", "zip")
        ok, err = backup.restore_backup(zip_path, self.server)
        self.assertTrue(ok, err)
        self.assertTrue(os.path.isfile(os.path.join(self.server, "worlds", "ZipWorld", "level.dat")))

    def test_zip_without_worlds_fails(self):
        zip_path = os.path.join(self.backup_root, "srv_worlds_20260103_000000.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("other/readme.txt", "x")
        ok, err = backup.restore_backup(zip_path, self.server)
        self.assertFalse(ok)
        self.assertIn("worlds", err)

    def test_failure_rolls_back_original_worlds(self):
        backup_dir = self._dir_backup()
        original = os.path.join(self.server, "worlds", "World", "level.dat")
        self.assertTrue(os.path.isfile(original))

        with mock.patch.object(backup.shutil, "copytree", side_effect=OSError("disk full")):
            ok, err = backup.restore_backup(backup_dir, self.server)

        self.assertFalse(ok)
        self.assertTrue("恢复失败" in err or "Restore failed" in err, err)
        self.assertTrue(os.path.isfile(original), "恢复失败必须保留原存档")
        leftovers = [d for d in os.listdir(self.server) if d.startswith("worlds_restore_tmp_")]
        self.assertEqual(leftovers, [], "回滚后不应残留临时目录")

    def test_stops_running_server_before_restore(self):
        self.proc.running = True
        backup_dir = self._dir_backup()
        ok, _err = backup.restore_backup(backup_dir, self.server)
        self.assertTrue(ok)
        self.assertEqual(self.proc.stop_calls, 1)


class TestDeleteBackup(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.server = _make_server(self.root)
        self.backup_root = backup.get_backup_root(self.server)
        os.makedirs(self.backup_root, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_delete_directory_backup(self):
        target = os.path.join(self.backup_root, "srv_worlds_20260101_000000")
        _write(os.path.join(target, "World", "level.dat"), "x")
        ok, err = backup.delete_backup(target, self.server)
        self.assertTrue(ok, err)
        self.assertFalse(os.path.exists(target))

    def test_delete_zip_backup(self):
        target = os.path.join(self.backup_root, "srv_worlds_20260102_000000.zip")
        with zipfile.ZipFile(target, "w") as zf:
            zf.writestr("worlds/World/level.dat", "x")
        ok, err = backup.delete_backup(target, self.server)
        self.assertTrue(ok, err)
        self.assertFalse(os.path.exists(target))

    def test_missing_backup(self):
        ok, _err = backup.delete_backup(os.path.join(self.backup_root, "nope"), self.server)
        self.assertFalse(ok)

    def test_rejects_outside_root(self):
        outside = os.path.join(self.root, "outside")
        _write(os.path.join(outside, "keep.txt"), "x")
        ok, _err = backup.delete_backup(outside, self.server)
        self.assertFalse(ok)
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.txt")))


class TestWorldMutations(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = os.path.join(self.tmp.name, "srv")
        _write(os.path.join(self.server, "server.properties"), "level-name=Alpha\nserver-port=19132\n")
        _write(os.path.join(self.server, "worlds", "Alpha", "level.dat"), "alpha")
        _write(os.path.join(self.server, "worlds", "Beta", "level.dat"), "beta")
        _write(os.path.join(self.server, "worlds", "Beta", "db", "chunk.bin"), "chunk")

    def tearDown(self):
        self.tmp.cleanup()

    def test_delete_non_active_world(self):
        ok, err = worlds.delete_world(self.server, "Beta")
        self.assertTrue(ok, err)
        self.assertFalse(os.path.exists(os.path.join(self.server, "worlds", "Beta")))

    def test_cannot_delete_active_world(self):
        ok, err = worlds.delete_world(self.server, "Alpha")
        self.assertFalse(ok)
        self.assertIn("活动世界", err)
        self.assertTrue(os.path.isdir(os.path.join(self.server, "worlds", "Alpha")))

    def test_delete_rejects_path_traversal(self):
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(outside, exist_ok=True)
        ok, _err = worlds.delete_world(self.server, "../outside")
        self.assertFalse(ok)
        self.assertTrue(os.path.isdir(outside))

    def test_delete_missing_world(self):
        ok, _err = worlds.delete_world(self.server, "Nope")
        self.assertFalse(ok)

    def test_rename_active_world_updates_config(self):
        ok, err = worlds.rename_world(self.server, "Alpha", "Gamma")
        self.assertTrue(ok, err)
        self.assertTrue(os.path.isdir(os.path.join(self.server, "worlds", "Gamma")))
        self.assertEqual(worlds.get_active_world_name(self.server), "Gamma")

    def test_rename_to_existing_name_rejected(self):
        ok, _err = worlds.rename_world(self.server, "Alpha", "Beta")
        self.assertFalse(ok)
        self.assertTrue(os.path.isdir(os.path.join(self.server, "worlds", "Alpha")))

    def test_rename_invalid_new_name(self):
        ok, _err = worlds.rename_world(self.server, "Alpha", "../escape")
        self.assertFalse(ok)
        self.assertTrue(os.path.isdir(os.path.join(self.server, "worlds", "Alpha")))

    def test_duplicate_world_copies_contents(self):
        ok, err = worlds.duplicate_world(self.server, "Beta", "BetaCopy")
        self.assertTrue(ok, err)
        self.assertEqual(
            Path(os.path.join(self.server, "worlds", "BetaCopy", "db", "chunk.bin")).read_text(encoding="utf-8"),
            "chunk",
        )

    def test_duplicate_to_existing_name_rejected(self):
        ok, _err = worlds.duplicate_world(self.server, "Beta", "Alpha")
        self.assertFalse(ok)

    def test_duplicate_missing_source(self):
        ok, _err = worlds.duplicate_world(self.server, "Nope", "Copy")
        self.assertFalse(ok)


class TestInstallSafety(TestCase):
    """安装合并/清理不得误删用户文件，也不能在安装包不完整时动手。"""

    def setUp(self):
        from bedrock_server_manager.constants import SERVER_EXE

        self.exe = SERVER_EXE
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.target = os.path.join(self.root, "server")
        self.staging = os.path.join(self.root, "staging")
        os.makedirs(self.target)
        os.makedirs(self.staging)

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_refuses_incomplete_staging(self):
        from bedrock_server_manager import install

        _write(os.path.join(self.target, self.exe), "original")
        _write(os.path.join(self.staging, "random.dll"), "new")  # 缺 exe
        with self.assertRaises(RuntimeError):
            install.apply_staged_install(self.staging, self.target)
        self.assertEqual(
            Path(os.path.join(self.target, self.exe)).read_text(encoding="utf-8"),
            "original",
            "安装包不完整时不得改动服务器目录",
        )

    def test_prune_only_removes_previously_owned_artifacts(self):
        from bedrock_server_manager import install

        _write(os.path.join(self.staging, self.exe), "bin")
        _write(os.path.join(self.target, self.exe), "bin")
        _write(os.path.join(self.target, "old.dll"), "old")  # 上次安装的产物，本次没有
        _write(os.path.join(self.target, "keep_user.txt"), "user")  # 用户文件
        _write(os.path.join(self.target, "user_notes.zip"), "user")  # 用户文件，名字像 zip
        _write(os.path.join(self.target, "server.properties"), "level-name=World")  # 用户数据
        os.makedirs(os.path.join(self.target, "half.install_new"))  # 目录合并残留
        os.makedirs(os.path.join(self.target, "worlds"))
        os.makedirs(os.path.join(self.target, "worlds.install_old"))  # 原目录已就位 → 应清理
        os.makedirs(os.path.join(self.target, "packs.install_old"))  # 原目录缺失 → 唯一副本，应保留

        removed = install._prune_stale_files(
            self.staging,
            self.target,
            previous_owned={"old.dll", "user_notes.zip", "server.properties"},
        )

        self.assertIn("old.dll", removed)
        self.assertFalse(os.path.exists(os.path.join(self.target, "old.dll")))
        self.assertFalse(os.path.exists(os.path.join(self.target, "half.install_new")))
        self.assertTrue(os.path.isfile(os.path.join(self.target, "keep_user.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.target, "user_notes.zip")), "用户文件不能被清理")
        self.assertTrue(os.path.isfile(os.path.join(self.target, "server.properties")), "用户配置不能被清理")
        self.assertFalse(os.path.exists(os.path.join(self.target, "worlds.install_old")))
        self.assertTrue(os.path.isdir(os.path.join(self.target, "packs.install_old")), "唯一副本不能被清理")

    def test_extract_zip_rejects_path_traversal(self):
        from bedrock_server_manager import install

        zip_path = os.path.join(self.root, "evil.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../evil.txt", "evil")
        dest = os.path.join(self.root, "out")
        os.makedirs(dest)
        with self.assertRaises(RuntimeError):
            install.extract_zip(zip_path, dest)
        self.assertFalse(os.path.exists(os.path.join(self.root, "evil.txt")))

    def test_extract_zip_writes_normal_entries(self):
        from bedrock_server_manager import install

        zip_path = os.path.join(self.root, "ok.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("sub/file.txt", "ok")
        dest = os.path.join(self.root, "out_ok")
        os.makedirs(dest)
        install.extract_zip(zip_path, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "sub", "file.txt")))


class TestBackupNameGuard(TestCase):
    """备份根目录里的用户文件夹不能被列出/删除/恢复。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = _make_server(self.tmp.name)
        self.backup_root = backup.get_backup_root(self.server)
        os.makedirs(self.backup_root, exist_ok=True)
        self.user_dir = os.path.join(self.backup_root, "srv_worlds_notes")
        _write(os.path.join(self.user_dir, "important.txt"), "keep")

    def tearDown(self):
        self.tmp.cleanup()

    def test_user_folder_is_not_listed_as_backup(self):
        self.assertEqual(backup.list_backups(self.server), [])

    def test_delete_refuses_user_folder(self):
        ok, err = backup.delete_backup(self.user_dir, self.server)
        self.assertFalse(ok)
        self.assertTrue("非法路径" in err or "Invalid path" in err, err)
        self.assertTrue(os.path.isfile(os.path.join(self.user_dir, "important.txt")))

    def test_restore_refuses_user_folder(self):
        ok, err = backup.restore_backup(self.user_dir, self.server)
        self.assertFalse(ok)
        self.assertTrue("非法路径" in err or "Invalid path" in err, err)
        self.assertTrue(os.path.isfile(os.path.join(self.user_dir, "important.txt")))

    def test_delete_allows_real_backup(self):
        target = os.path.join(self.backup_root, "srv_worlds_20260101_000000")
        _write(os.path.join(target, "World", "level.dat"), "x")
        ok, err = backup.delete_backup(target, self.server)
        self.assertTrue(ok, err)
        self.assertFalse(os.path.exists(target))


class TestBackupRetention(TestCase):
    """保留策略只清理"本服务器的真备份"，不得误删用户目录或其他服务器的备份。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.server = _make_server(self.root)
        self.backup_root = backup.get_backup_root(self.server)
        os.makedirs(self.backup_root, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cleanup_keeps_only_recent_matching_backups(self):
        old_dir = os.path.join(self.backup_root, "srv_worlds_20200101_000000")
        old_zip = os.path.join(self.backup_root, "srv_worlds_20200102_000000.zip")
        user_dir = os.path.join(self.backup_root, "srv_worlds_notes")
        other_server = os.path.join(self.backup_root, "other_worlds_20200101_000000.zip")
        _write(os.path.join(old_dir, "worlds", "Old", "level.dat"), "old")
        with zipfile.ZipFile(old_zip, "w") as zf:
            zf.writestr("worlds/Old/level.dat", "old")
        _write(os.path.join(user_dir, "readme.txt"), "mine")
        with zipfile.ZipFile(other_server, "w") as zf:
            zf.writestr("worlds/Other/level.dat", "other")

        new_backup = backup.backup_worlds_zip(self.server, max_backups=1)

        self.assertIsNotNone(new_backup)
        self.assertTrue(os.path.isfile(new_backup))
        self.assertFalse(os.path.exists(old_dir), "旧备份应被清理")
        self.assertFalse(os.path.exists(old_zip), "旧备份应被清理")
        self.assertTrue(os.path.isdir(user_dir), "用户目录不能被当作备份删除")
        self.assertTrue(os.path.isfile(other_server), "其他服务器的备份不能被清理")
        self.assertEqual(len(backup.list_backups(self.server)), 1)

    def test_cleanup_keeps_newest_n(self):
        targets = []
        for index, stamp in enumerate(("20200101_000000", "20200102_000000", "20200103_000000")):
            target = os.path.join(self.backup_root, f"srv_worlds_{stamp}.zip")
            with zipfile.ZipFile(target, "w") as zf:
                zf.writestr("worlds/W/level.dat", "x")
            os.utime(target, (1_000 + index * 1_000, 1_000 + index * 1_000))
            targets.append(target)
        backup._cleanup_old_backups(self.backup_root, 2, "srv")
        remaining = sorted(name for name in os.listdir(self.backup_root) if name.startswith("srv_worlds_"))
        self.assertEqual(
            remaining,
            ["srv_worlds_20200102_000000.zip", "srv_worlds_20200103_000000.zip"],
            "应只保留最近（mtime 最新）的 2 个备份",
        )

    def test_directory_backup_cleanup_keeps_user_folder(self):
        user_dir = os.path.join(self.backup_root, "srv_worlds_notes")
        _write(os.path.join(user_dir, "important.txt"), "keep")
        old = os.path.join(self.backup_root, "srv_worlds_20200101_000000")
        _write(os.path.join(old, "World", "level.dat"), "old")

        result = backup.backup_worlds(self.server, max_backups=1)

        self.assertTrue(result)
        self.assertTrue(os.path.isfile(os.path.join(user_dir, "important.txt")), "用户文件夹不能被自动清理删除")
        self.assertFalse(os.path.exists(old), "旧目录备份应被清理")


if __name__ == "__main__":
    main(verbosity=2)
