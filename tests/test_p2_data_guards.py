"""A4/A8/A10/A11 回归测试。"""

import json
import os
import shutil
import tempfile
import unittest
import zipfile


class TestPackImportOverwrite(unittest.TestCase):
    """A4：同名包不静默覆盖 + 解压体积上限"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mbes_pack_")
        self.server = os.path.join(self.tmp, "server")
        os.makedirs(os.path.join(self.server, "resource_packs"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pack_b64(self, root_name="MyPack"):
        import base64
        import io

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"{root_name}/manifest.json", json.dumps({"header": {"name": root_name}}))
            zf.writestr(f"{root_name}/pack_icon.png", b"x")
        return base64.b64encode(buf.getvalue()).decode()

    def test_refuses_overwrite_by_default(self):
        from bedrock_server_manager.packs import import_pack

        data = self._pack_b64()
        ok, err, _msg = import_pack(self.server, "resource", data, "MyPack.zip")
        self.assertTrue(ok, err)
        # 第二次导入同名包应被拒绝
        ok2, err2, _msg2 = import_pack(self.server, "resource", data, "MyPack.zip")
        self.assertFalse(ok2, "同名包被静默覆盖了")
        self.assertIn("已存在同名包", err2)

    def test_overwrite_when_requested(self):
        from bedrock_server_manager.packs import import_pack

        data = self._pack_b64()
        self.assertTrue(import_pack(self.server, "resource", data, "MyPack.zip")[0])
        ok, err, _ = import_pack(self.server, "resource", data, "MyPack.zip", overwrite=True)
        self.assertTrue(ok, err)

    def test_rejects_zip_bomb(self):
        """声明解压后体积超限时拒绝（不实际解压）"""
        import base64
        import io
        from unittest.mock import patch

        from bedrock_server_manager import packs

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Bomb/manifest.json", "{}")
        data = base64.b64encode(buf.getvalue()).decode()

        with patch.object(packs, "_IMPORT_MAX_UNPACKED_BYTES", 1):
            ok, err, _ = packs.import_pack(self.server, "resource", data, "bomb.zip")
        self.assertFalse(ok)
        self.assertIn("解压后体积过大", err)


class TestSchedulerTimeValidation(unittest.TestCase):
    """A8：拒绝不存在的时刻与重复 id"""

    def test_rejects_invalid_hour_minute(self):
        from bedrock_server_manager.scheduler import validate_scheduled_task

        for bad in ("99:99", "24:00", "12:60", "1:0", "ab:cd", "-1:00"):
            task = {"id": "t", "type": "backup", "enabled": True, "schedule_type": "daily", "time": bad}
            ok, _err = validate_scheduled_task(task)
            self.assertFalse(ok, f"{bad!r} 应被拒绝")

    def test_accepts_valid_times(self):
        from bedrock_server_manager.scheduler import validate_scheduled_task

        for good in ("00:00", "04:00", "23:59", "12:30"):
            task = {"id": "t", "type": "backup", "enabled": True, "schedule_type": "daily", "time": good}
            ok, err = validate_scheduled_task(task)
            self.assertTrue(ok, f"{good!r} 应通过: {err}")

    def test_rejects_duplicate_ids(self):
        from bedrock_server_manager.scheduler import validate_scheduled_tasks

        tasks = [
            {"id": "same", "type": "backup", "enabled": True, "schedule_type": "hourly"},
            {"id": "same", "type": "restart", "enabled": True, "schedule_type": "hourly"},
        ]
        ok, err, valid = validate_scheduled_tasks(tasks)
        self.assertFalse(ok)
        self.assertIn("重复", err)
        self.assertEqual(valid, [])

    def test_accepts_unique_ids(self):
        from bedrock_server_manager.scheduler import validate_scheduled_tasks

        tasks = [
            {"id": "a", "type": "backup", "enabled": True, "schedule_type": "hourly"},
            {"id": "b", "type": "restart", "enabled": True, "schedule_type": "hourly"},
        ]
        ok, err, valid = validate_scheduled_tasks(tasks)
        self.assertTrue(ok, err)
        self.assertEqual(len(valid), 2)


class TestRestoreRollbackCleansPartial(unittest.TestCase):
    """A10：恢复失败时先删半成品再回滚，避免旧存档滞留 tmp"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old = settings.get("server_dir", "")
        self.tmp = tempfile.mkdtemp(prefix="mbes_restore_")
        self.server = os.path.join(self.tmp, "server")
        os.makedirs(os.path.join(self.server, "worlds", "OldWorld"))
        with open(os.path.join(self.server, "worlds", "OldWorld", "level.dat"), "w") as f:
            f.write("old-data")
        # 造一个合法备份（目录型）
        self.backup_root = os.path.join(self.tmp, "_worlds_backups")
        self.backup = os.path.join(self.backup_root, "server_worlds_20260101_000000")
        os.makedirs(os.path.join(self.backup, "NewWorld"))
        with open(os.path.join(self.backup, "NewWorld", "level.dat"), "w") as f:
            f.write("new-data")
        settings["server_dir"] = self.server

    def tearDown(self):
        from bedrock_server_manager import settings

        settings["server_dir"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_failed_copy_restores_old_worlds(self):
        """copytree 中途失败后，旧 worlds 必须被恢复，且不留 tmp 目录"""
        from unittest.mock import MagicMock, patch

        from bedrock_server_manager.backup import restore_backup

        def boom(src, dst, **kw):
            # 先建出半成品目录，再抛错，模拟"复制到一半失败"
            os.makedirs(dst, exist_ok=True)
            with open(os.path.join(dst, "partial.txt"), "w") as f:
                f.write("partial")
            raise OSError("disk full")

        # 假的服务器进程对象：未运行，restore_backup 不会尝试停服
        fake_proc = MagicMock(running=False)
        with (
            patch("bedrock_server_manager.backup.shutil.copytree", side_effect=boom),
            patch("bedrock_server_manager.backup.get_server_proc", return_value=fake_proc),
        ):
            ok, _err = restore_backup(self.backup, self.server)

        self.assertFalse(ok)
        worlds = os.path.join(self.server, "worlds")
        self.assertTrue(os.path.isdir(worlds), "旧 worlds 未恢复")
        self.assertTrue(
            os.path.isfile(os.path.join(worlds, "OldWorld", "level.dat")),
            "旧世界数据丢失",
        )
        leftovers = [n for n in os.listdir(self.server) if n.startswith("worlds_restore_tmp_")]
        self.assertEqual(leftovers, [], f"残留临时目录: {leftovers}")


class TestParseOnlinePlayers(unittest.TestCase):
    """A11：不再把命令行/日志行误判为玩家"""

    def test_parses_standard_line(self):
        from bedrock_server_manager.players import parse_online_players

        self.assertEqual(parse_online_players("There are 2/10 players online: Steve, Alex"), ["Steve", "Alex"])

    def test_empty_list(self):
        from bedrock_server_manager.players import parse_online_players

        self.assertEqual(parse_online_players("There are 0/10 players online:"), [])

    def test_chinese_and_space_names(self):
        from bedrock_server_manager.players import parse_online_players

        got = parse_online_players("There are 2/10 players online: 玩家一, Notch Smith")
        self.assertEqual(got, ["玩家一", "Notch Smith"])

    def test_does_not_misread_commands(self):
        """回显的命令行不应被当作玩家名（原兜底逻辑的 bug）"""
        from bedrock_server_manager.players import parse_online_players

        text = "list\nversion\n[2026-01-01] Server started.\nThere are 0/10 players online:"
        self.assertEqual(parse_online_players(text), [])

    def test_no_format_returns_empty(self):
        from bedrock_server_manager.players import parse_online_players

        self.assertEqual(parse_online_players("random log line"), [])
        self.assertEqual(parse_online_players(""), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
