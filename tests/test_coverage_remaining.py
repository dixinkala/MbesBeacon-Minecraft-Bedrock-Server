"""补齐最后 3 个未达门槛模块的分支：worlds / verify / scheduler。"""

import os
import tempfile
import unittest
import zipfile
from unittest import mock

from bedrock_server_manager import scheduler, verify, worlds


class TestWorldsBranches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = os.path.join(self.tmp.name, "srv")
        os.makedirs(os.path.join(self.server, "worlds", "Alpha"))
        with open(os.path.join(self.server, "worlds", "Alpha", "level.dat"), "w", encoding="utf-8") as handle:
            handle.write("x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_worlds_mtime_failure_uses_zero(self):
        real_getmtime = os.path.getmtime

        def fake_getmtime(path):
            if "Alpha" in path:
                raise OSError("no mtime")
            return real_getmtime(path)

        with mock.patch.object(worlds.os.path, "getmtime", side_effect=fake_getmtime):
            result = worlds.list_worlds(self.server)
        self.assertEqual(result[0]["modified"], 0)

    def test_list_worlds_outer_failure_returns_empty(self):
        with mock.patch.object(worlds.os, "listdir", side_effect=OSError("listing failed")):
            self.assertEqual(worlds.list_worlds(self.server), [])

    def test_get_active_world_name_parse_failure_falls_back(self):
        with open(os.path.join(self.server, "server.properties"), "w", encoding="utf-8") as handle:
            handle.write("level-name=Alpha\n")
        with mock.patch.object(worlds, "load_properties", side_effect=OSError("bad file")):
            self.assertEqual(worlds.get_active_world_name(self.server), "Bedrock level")

    def test_duplicate_world_copy_failure(self):
        with mock.patch.object(worlds.shutil, "copytree", side_effect=OSError("disk full")):
            ok, err = worlds.duplicate_world(self.server, "Alpha", "Copy")
        self.assertFalse(ok)
        self.assertIn("复制世界失败", err)


class TestVerifyHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "file.bin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_size_missing_and_bounds(self):
        self.assertFalse(verify.verify_file_size(os.path.join(self.tmp.name, "nope"))[0])
        with open(self.path, "wb") as handle:
            handle.write(b"x")
        self.assertFalse(verify.verify_file_size(self.path, min_size=10)[0])
        self.assertFalse(verify.verify_file_size(self.path, max_size=0)[0])
        self.assertTrue(verify.verify_file_size(self.path, min_size=1, max_size=10)[0])

    def test_file_size_oserror(self):
        with open(self.path, "wb") as handle:
            handle.write(b"x")
        with mock.patch.object(verify.os.path, "getsize", side_effect=OSError("boom")):
            ok, message, size = verify.verify_file_size(self.path)
        self.assertFalse(ok)
        self.assertEqual(size, 0)
        self.assertIn("失败", message)

    def test_zip_integrity_cases(self):
        self.assertFalse(verify.verify_zip_integrity(os.path.join(self.tmp.name, "nope"))[0])
        with open(self.path, "wb") as handle:
            handle.write(b"not a zip")
        self.assertFalse(verify.verify_zip_integrity(self.path)[0])
        good = os.path.join(self.tmp.name, "good.zip")
        with zipfile.ZipFile(good, "w") as zf:
            zf.writestr("a.txt", "ok")
        self.assertTrue(verify.verify_zip_integrity(good)[0])
        with mock.patch.object(verify.zipfile.ZipFile, "testzip", return_value="a.txt"):
            ok, message = verify.verify_zip_integrity(good)
        self.assertFalse(ok)
        self.assertIn("损坏", message)

    def test_sha256_branches(self):
        self.assertFalse(verify.verify_sha256(os.path.join(self.tmp.name, "nope"))[0])
        with open(self.path, "wb") as handle:
            handle.write(b"data")
        ok, _message, digest = verify.verify_sha256(self.path, expected_hash="deadbeef")
        self.assertFalse(ok)
        ok, _message, _digest = verify.verify_sha256(self.path, expected_hash=digest)
        self.assertTrue(ok)
        with mock.patch.object(verify, "get_known_hash", return_value="deadbeef"):
            self.assertFalse(verify.verify_sha256(self.path, version="1.0")[0])
        with (
            mock.patch.object(verify, "get_known_hash", return_value=None),
            mock.patch.object(verify, "register_known_hash") as register,
        ):
            ok, _message, _digest = verify.verify_sha256(self.path, version="1.0")
        self.assertTrue(ok)
        register.assert_called_once()

    def test_elf_executable(self):
        self.assertFalse(verify.verify_elf_executable(os.path.join(self.tmp.name, "nope"))[0])
        with open(self.path, "wb") as handle:
            handle.write(b"plain text")
        self.assertFalse(verify.verify_elf_executable(self.path)[0])
        elf = os.path.join(self.tmp.name, "bedrock_server")
        payload = bytearray(b"\x7fELF" + b"\x00" * 64)
        payload[4] = 2  # 64 位
        payload[5] = 1  # 小端
        payload[16:18] = b"\x02\x00"  # e_type = ET_EXEC
        with open(elf, "wb") as handle:
            handle.write(bytes(payload))
        ok, message = verify.verify_elf_executable(elf)
        self.assertTrue(ok, message)


class FakeProc:
    def __init__(self):
        self.server_dir = ""
        self.stopped = 0
        self.started = 0
        self.sent = []

    def stop(self):
        self.stopped += 1

    def start(self):
        self.started += 1

    def send(self, command):
        self.sent.append(command)


class TestSchedulerExecution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = self.tmp.name
        self.proc = FakeProc()
        self.patches = [
            mock.patch.object(scheduler, "server_running", return_value=True),
            mock.patch.object(scheduler, "get_server_proc", return_value=self.proc),
            mock.patch.object(scheduler, "backup_worlds_zip"),
            mock.patch.object(scheduler, "audit_log"),
            mock.patch.object(scheduler.time, "sleep"),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def test_restart_task(self):
        scheduler._execute_scheduled_task({"type": "restart"}, self.server, note="补执行")
        self.assertEqual(self.proc.stopped, 1)
        self.assertEqual(self.proc.started, 1)
        self.assertEqual(self.proc.server_dir, self.server)

    def test_backup_task(self):
        scheduler._execute_scheduled_task({"type": "backup", "max_backups": 3}, self.server)
        scheduler.backup_worlds_zip.assert_called_once_with(self.server, max_backups=3)

    def test_announce_task(self):
        scheduler._execute_scheduled_task({"type": "announce", "message": "hello"}, self.server)
        self.assertEqual(self.proc.sent, ["say hello"])

    def test_known_error_is_logged(self):
        scheduler.backup_worlds_zip.side_effect = OSError("disk full")
        scheduler._execute_scheduled_task({"type": "backup"}, self.server)
        scheduler.audit_log.assert_called()

    def test_unknown_error_is_logged(self):
        scheduler.backup_worlds_zip.side_effect = ValueError("weird")
        scheduler._execute_scheduled_task({"type": "backup"}, self.server)
        scheduler.audit_log.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
