"""
security 模块单元测试 - Minecraft 基岩版服务器管理器
覆盖命令校验、API Token 生成等核心功能
"""

import re
import unittest

from bedrock_server_manager.security import (
    generate_api_token,
    validate_command,
)


class TestValidateCommand(unittest.TestCase):
    """命令校验测试"""

    def test_empty_command(self):
        """测试空命令"""
        ok, err, dangerous = validate_command("")
        self.assertFalse(ok)

    def test_whitespace_command(self):
        """测试仅包含空白字符的命令"""
        ok, err, dangerous = validate_command("   ")
        self.assertFalse(ok)

    def test_safe_command_list(self):
        """测试安全命令 list"""
        ok, err, dangerous = validate_command("list")
        self.assertTrue(ok)

    def test_safe_command_say(self):
        """测试安全命令 say"""
        ok, err, dangerous = validate_command("say Hello World")
        self.assertTrue(ok)

    def test_dangerous_command_stop(self):
        """测试危险命令 stop"""
        ok, err, dangerous = validate_command("stop")
        self.assertTrue(ok)
        self.assertTrue(dangerous)

    def test_command_with_slash_prefix(self):
        """测试带 / 前缀的命令"""
        ok, err, dangerous = validate_command("/list")
        self.assertTrue(ok)


class TestGenerateApiToken(unittest.TestCase):
    """API Token 生成测试"""

    def test_token_length(self):
        """测试 Token 长度"""
        token = generate_api_token()
        self.assertEqual(len(token), 32)  # 16 bytes = 32 hex chars

    def test_token_is_hex(self):
        """测试 Token 是十六进制"""
        token = generate_api_token()
        self.assertTrue(re.match(r"^[0-9a-f]{32}$", token))

    def test_tokens_are_unique(self):
        """测试多次生成的 Token 是唯一的"""
        tokens = {generate_api_token() for _ in range(100)}
        self.assertEqual(len(tokens), 100)


if __name__ == "__main__":
    unittest.main()
