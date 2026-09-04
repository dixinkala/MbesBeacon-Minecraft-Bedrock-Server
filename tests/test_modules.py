"""
综合模块单元测试 - Minecraft 基岩版服务器管理器
覆盖 worlds、commands、app_logger 等模块的核心功能
"""

import os
import tempfile
import unittest

from bedrock_server_manager.app_logger import get_app_logger
from bedrock_server_manager.commands import (
    autocomplete_command,
    get_all_commands,
    get_categories,
    get_command_help,
)
from bedrock_server_manager.worlds import (
    get_active_world_name,
    list_worlds,
    set_active_world,
)


class TestWorlds(unittest.TestCase):
    """世界管理测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.server_dir = self.tmpdir
        # 创建 worlds 目录
        self.worlds_dir = os.path.join(self.server_dir, "worlds")
        os.makedirs(self.worlds_dir, exist_ok=True)
        # 创建测试世界
        self.world1 = os.path.join(self.worlds_dir, "World1")
        os.makedirs(self.world1, exist_ok=True)
        self.world2 = os.path.join(self.worlds_dir, "World2")
        os.makedirs(self.world2, exist_ok=True)
        # 创建 server.properties
        self.props_path = os.path.join(self.server_dir, "server.properties")
        with open(self.props_path, "w", encoding="utf-8") as f:
            f.write("level-name=World1\n")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_list_worlds(self):
        """测试列出世界"""
        worlds = list_worlds(self.server_dir)
        self.assertIsInstance(worlds, list)
        self.assertGreaterEqual(len(worlds), 2)

    def test_get_active_world_name(self):
        """测试获取当前活动世界名"""
        name = get_active_world_name(self.server_dir)
        self.assertEqual(name, "World1")

    def test_set_active_world(self):
        """测试设置活动世界"""
        ok, err = set_active_world(self.server_dir, "World2")
        self.assertTrue(ok)
        name = get_active_world_name(self.server_dir)
        self.assertEqual(name, "World2")

    def test_set_active_world_not_exist(self):
        """测试设置不存在的世界"""
        ok, err = set_active_world(self.server_dir, "NonExistent")
        self.assertFalse(ok)

    def test_list_worlds_no_worlds_dir(self):
        """测试无 worlds 目录时的世界列表"""
        import shutil

        shutil.rmtree(self.worlds_dir)
        worlds = list_worlds(self.server_dir)
        self.assertEqual(worlds, [])


class TestCommands(unittest.TestCase):
    """命令模块测试"""

    def test_get_all_commands(self):
        """测试获取所有命令"""
        commands = get_all_commands()
        self.assertIsInstance(commands, list)
        self.assertGreater(len(commands), 0)

    def test_get_categories(self):
        """测试获取命令分类"""
        categories = get_categories()
        self.assertIsInstance(categories, list)
        self.assertGreater(len(categories), 0)

    def test_autocomplete_command(self):
        """测试命令自动补全"""
        suggestions = autocomplete_command("li", limit=5)
        self.assertIsInstance(suggestions, list)
        names = [s["name"] for s in suggestions]
        self.assertIn("list", names)

    def test_get_command_help(self):
        """测试获取命令帮助"""
        help_text = get_command_help("list")
        self.assertIsInstance(help_text, str)
        self.assertGreater(len(help_text), 0)

    def test_get_command_help_all(self):
        """测试获取所有命令帮助"""
        help_text = get_command_help("")
        self.assertIsInstance(help_text, str)
        self.assertGreater(len(help_text), 0)


class TestAppLogger(unittest.TestCase):
    """应用日志测试"""

    def test_get_app_logger(self):
        """测试获取应用日志器"""
        logger = get_app_logger()
        self.assertIsNotNone(logger)

    def test_logger_singleton(self):
        """测试日志器单例"""
        logger1 = get_app_logger()
        logger2 = get_app_logger()
        self.assertIs(logger1, logger2)

    def test_logger_methods(self):
        """测试日志器方法"""
        logger = get_app_logger()
        # 这些方法应该存在且不报错
        logger.info("Test info message")
        logger.warning("Test warning message")
        logger.error("Test error message")
        logger.debug("Test debug message")


if __name__ == "__main__":
    unittest.main()
