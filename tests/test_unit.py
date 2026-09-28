"""
单元测试 - Minecraft 基岩版服务器管理器
覆盖纯函数：校验、配置读写、路径安全、玩家名解析等
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bedrock_server_manager as bsm


class TestValidatePlayerName(unittest.TestCase):
    """玩家名校验"""

    def test_valid_names(self):
        for name in ["Player1", "test_user", "A.B.C", "a" * 16]:
            ok, err = bsm.validate_player_name(name)
            self.assertTrue(ok, f"{name} 应通过: {err}")

    def test_empty_name(self):
        ok, err = bsm.validate_player_name("")
        self.assertFalse(ok)
        self.assertIn("不能为空", err)

    def test_none_name(self):
        ok, err = bsm.validate_player_name(None)
        self.assertFalse(ok)

    def test_too_long(self):
        ok, err = bsm.validate_player_name("a" * 17)
        self.assertFalse(ok)
        self.assertIn("16", err)

    def test_invalid_chars(self):
        for name in ["player name", "player@name", "玩家", "player$"]:
            ok, err = bsm.validate_player_name(name)
            self.assertFalse(ok, f"{name} 应被拒绝")


class TestValidateCustomUrl(unittest.TestCase):
    """自定义下载URL校验"""

    def test_official_url(self):
        ok, msg, is_official = bsm.validate_custom_url(
            "https://www.minecraft.net/download/server/bedrock/bin-win/bedrock-server-1.21.0.03.zip"
        )
        self.assertTrue(ok)
        self.assertTrue(is_official)

    def test_non_official_url(self):
        ok, msg, is_official = bsm.validate_custom_url("https://mirror.example.com/bedrock-server.zip")
        self.assertTrue(ok)
        self.assertFalse(is_official)
        self.assertIn("非官方", msg)

    def test_invalid_scheme(self):
        ok, msg, _ = bsm.validate_custom_url("ftp://example.com/file.zip")
        self.assertFalse(ok)
        self.assertIn("http", msg)

    def test_empty_url(self):
        ok, msg, _ = bsm.validate_custom_url("")
        self.assertFalse(ok)

    def test_missing_domain(self):
        ok, msg, _ = bsm.validate_custom_url("https:///path")
        self.assertFalse(ok)


class TestValidateConfigUpdates(unittest.TestCase):
    """配置项取值范围校验"""

    def test_valid_config(self):
        ok, err, field = bsm.validate_config_updates({"max-players": "20", "view-distance": "32"})
        self.assertTrue(ok, err)

    def test_port_out_of_range(self):
        ok, err, field = bsm.validate_config_updates({"server-port": "99999"})
        self.assertFalse(ok)
        self.assertEqual(field, "server-port")

    def test_port_reserved(self):
        ok, err, field = bsm.validate_config_updates({"server-port": "80"})
        self.assertFalse(ok)
        self.assertIn("常用", err)

    def test_non_integer(self):
        ok, err, field = bsm.validate_config_updates({"max-players": "abc"})
        self.assertFalse(ok)
        self.assertEqual(field, "max-players")

    def test_max_players_exceed(self):
        ok, err, field = bsm.validate_config_updates({"max-players": "200"})
        self.assertFalse(ok)
        self.assertIn("100", err)

    def test_unknown_field_passes(self):
        ok, err, field = bsm.validate_config_updates({"unknown-field": "value"})
        self.assertTrue(ok)


class TestValidateCommand(unittest.TestCase):
    """指令校验"""

    def test_empty_command(self):
        ok, err, dangerous = bsm.validate_command("")
        self.assertFalse(ok)

    def test_safe_command(self):
        ok, err, dangerous = bsm.validate_command("list")
        self.assertTrue(ok)
        self.assertFalse(dangerous)

    def test_dangerous_stop(self):
        ok, err, dangerous = bsm.validate_command("stop")
        self.assertTrue(ok)
        self.assertTrue(dangerous)

    def test_dangerous_ban(self):
        ok, err, dangerous = bsm.validate_command("ban Player1 reason")
        self.assertTrue(ok)
        self.assertTrue(dangerous)

    def test_whitelist_remove_dangerous(self):
        ok, err, dangerous = bsm.validate_command("whitelist remove Player1")
        self.assertTrue(ok)
        self.assertTrue(dangerous)

    def test_whitelist_add_not_dangerous(self):
        ok, err, dangerous = bsm.validate_command("whitelist add Player1")
        self.assertTrue(ok)
        self.assertFalse(dangerous)


class TestExtractZipPathTraversal(unittest.TestCase):
    """ZIP解压路径遍历防护"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.dest = os.path.join(self.tmpdir, "dest")
        os.makedirs(self.dest)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_normal_zip(self):
        zip_path = os.path.join(self.tmpdir, "normal.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("file.txt", "hello")
            zf.writestr("subdir/file2.txt", "world")
        bsm.extract_zip(zip_path, self.dest)
        self.assertTrue(os.path.exists(os.path.join(self.dest, "file.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.dest, "subdir", "file2.txt")))

    def test_path_traversal_blocked(self):
        zip_path = os.path.join(self.tmpdir, "evil.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../evil.exe", "malicious")
        with self.assertRaises(RuntimeError):
            bsm.extract_zip(zip_path, self.dest)

    def test_absolute_path_blocked(self):
        zip_path = os.path.join(self.tmpdir, "abs.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("/tmp/evil.txt", "malicious")
        with self.assertRaises(RuntimeError):
            bsm.extract_zip(zip_path, self.dest)


class TestPropertiesIO(unittest.TestCase):
    """配置文件读写"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.props_path = os.path.join(self.tmpdir, "server.properties")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_properties(self):
        with open(self.props_path, "w", encoding="utf-8") as f:
            f.write("# 注释行\n")
            f.write("\n")
            f.write("server-name=Test Server\n")
            f.write("max-players=10\n")
        items = bsm.load_properties(self.props_path)
        d = dict(items)
        self.assertEqual(d["server-name"], "Test Server")
        self.assertEqual(d["max-players"], "10")
        self.assertNotIn("# 注释行", d)

    def test_load_nonexistent(self):
        items = bsm.load_properties(os.path.join(self.tmpdir, "nonexistent.properties"))
        self.assertEqual(items, [])

    def test_save_preserves_comments(self):
        with open(self.props_path, "w", encoding="utf-8") as f:
            f.write("# 服务器配置\n")
            f.write("server-name=Old\n")
            f.write("max-players=5\n")
            f.write("# 自定义项\n")
            f.write("custom-key=custom-value\n")
        bsm.save_properties(self.props_path, [("server-name", "New"), ("max-players", "20")])
        with open(self.props_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("New", content)
        self.assertIn("20", content)
        self.assertIn("custom-key=custom-value", content)
        self.assertIn("# 服务器配置", content)

    def test_properties_to_dict(self):
        items = [("a", "1"), ("b", "2")]
        d = bsm.properties_to_dict(items)
        self.assertEqual(d, {"a": "1", "b": "2"})

    def test_load_properties_cache_reuse(self):
        """文件未变化时复用缓存（同一对象，不重复读盘解析）"""
        with open(self.props_path, "w", encoding="utf-8") as f:
            f.write("server-name=Test Server\nmax-players=10\n")
        items1 = bsm.load_properties(self.props_path)
        items2 = bsm.load_properties(self.props_path)
        self.assertIs(items1, items2)

    def test_load_properties_cache_invalidated_on_change(self):
        """文件内容变化（mtime/size 变化）后缓存失效并重新解析"""
        with open(self.props_path, "w", encoding="utf-8") as f:
            f.write("server-name=Old\nmax-players=10\n")
        bsm.load_properties(self.props_path)
        with open(self.props_path, "w", encoding="utf-8") as f:
            f.write("server-name=New\nmax-players=20\n")
        # 显式更新 mtime，模拟外部修改（低精度文件系统上写入可能不改变 mtime）
        os.utime(self.props_path, ns=(1700000000123456789, 1700000000123456789))
        items = bsm.load_properties(self.props_path)
        d = bsm.properties_to_dict(items)
        self.assertEqual(d["server-name"], "New")
        self.assertEqual(d["max-players"], "20")

    def test_load_properties_cache_same_size_different_content(self):
        """相同 size 但 mtime 变化的文件仍能正确失效（mtime_ns 精度）"""
        with open(self.props_path, "w", encoding="utf-8") as f:
            f.write("server-name=AAA\nmax-players=10\n")
        bsm.load_properties(self.props_path)
        # 写入同长度不同内容，并显式修改 mtime（纳秒），不依赖文件系统时钟精度
        with open(self.props_path, "w", encoding="utf-8") as f:
            f.write("server-name=BBB\nmax-players=10\n")
        os.utime(self.props_path, ns=(1700000000123456789, 1700000000123456789))
        items = bsm.load_properties(self.props_path)
        self.assertEqual(bsm.properties_to_dict(items)["server-name"], "BBB")

    def test_save_properties_invalidates_cache(self):
        """save_properties 写入后 load 读到新值（mtime 变化自动失效）"""
        with open(self.props_path, "w", encoding="utf-8") as f:
            f.write("server-name=Old\nmax-players=10\n")
        bsm.load_properties(self.props_path)
        bsm.save_properties(self.props_path, [("server-name", "Updated"), ("max-players", "20")])
        items = bsm.load_properties(self.props_path)
        d = bsm.properties_to_dict(items)
        self.assertEqual(d["server-name"], "Updated")


class TestParseOnlinePlayers(unittest.TestCase):
    """在线玩家解析"""

    def test_standard_format(self):
        text = "There are 2/10 players online: Player1, Player2"
        players = bsm.parse_online_players(text)
        self.assertIn("Player1", players)
        self.assertIn("Player2", players)

    def test_no_players(self):
        text = "There are 0/10 players online:"
        players = bsm.parse_online_players(text)
        self.assertEqual(players, [])

    def test_empty_text(self):
        players = bsm.parse_online_players("")
        self.assertEqual(players, [])

    def test_none_text(self):
        players = bsm.parse_online_players(None)
        self.assertEqual(players, [])


class TestAllowlist(unittest.TestCase):
    """白名单管理"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.allowlist_path = os.path.join(self.tmpdir, "allowlist.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_to_empty(self):
        result = bsm.add_allowlist(self.tmpdir, "NewPlayer")
        self.assertTrue(result)
        with open(self.allowlist_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertTrue(any(item["name"] == "NewPlayer" for item in data))

    def test_add_duplicate(self):
        with open(self.allowlist_path, "w", encoding="utf-8") as f:
            json.dump([{"name": "Existing", "ignoresPlayerLimit": False}], f)
        result = bsm.add_allowlist(self.tmpdir, "Existing")
        self.assertTrue(result)
        with open(self.allowlist_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)

    def test_remove(self):
        with open(self.allowlist_path, "w", encoding="utf-8") as f:
            json.dump(
                [{"name": "Player1", "ignoresPlayerLimit": False}, {"name": "Player2", "ignoresPlayerLimit": False}], f
            )
        result = bsm.remove_allowlist(self.tmpdir, "Player1")
        self.assertTrue(result)
        with open(self.allowlist_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "Player2")

    def test_remove_nonexistent_file(self):
        result = bsm.remove_allowlist(self.tmpdir, "Nobody")
        self.assertTrue(result)


class TestBackupWorlds(unittest.TestCase):
    """世界备份"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.server_dir = os.path.join(self.tmpdir, "MyServer")
        self.worlds_dir = os.path.join(self.server_dir, "worlds")
        os.makedirs(self.worlds_dir)
        with open(os.path.join(self.worlds_dir, "level.dat"), "w") as f:
            f.write("fake world data")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_backup_creates_dir(self):
        result = bsm.backup_worlds(self.server_dir)
        self.assertTrue(result)
        self.assertTrue(os.path.isdir(result))
        self.assertTrue(os.path.exists(os.path.join(result, "level.dat")))

    def test_backup_no_worlds(self):
        shutil.rmtree(self.worlds_dir)
        result = bsm.backup_worlds(self.server_dir)
        self.assertEqual(result, "")

    def test_backup_invalid_dir(self):
        result = bsm.backup_worlds(os.path.join(self.tmpdir, "nonexistent"))
        self.assertEqual(result, "")

    def test_zip_backup(self):
        result = bsm.backup_worlds_zip(self.server_dir)
        self.assertTrue(result)
        self.assertTrue(result.endswith(".zip"))
        self.assertTrue(os.path.isfile(result))


class TestMakeDownloadUrl(unittest.TestCase):
    """下载URL生成"""

    def test_official_source(self):
        url = bsm.make_download_url("1.21.0.03", source_index=0)
        self.assertIn("1.21.0.03", url)
        self.assertIn("minecraft.net", url)

    def test_invalid_source_fallback(self):
        url = bsm.make_download_url("1.21.0.03", source_index=999)
        self.assertIn("1.21.0.03", url)


class TestDetectFileEncoding(unittest.TestCase):
    """文件编码检测"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_utf8_file(self):
        path = os.path.join(self.tmpdir, "utf8.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("server-name=测试服务器\n")
        enc = bsm.detect_file_encoding(path)
        self.assertIn(enc, ["utf-8", "UTF-8", "utf8"])

    def test_nonexistent_file(self):
        enc = bsm.detect_file_encoding(os.path.join(self.tmpdir, "nonexistent.txt"))
        self.assertEqual(enc, "utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
