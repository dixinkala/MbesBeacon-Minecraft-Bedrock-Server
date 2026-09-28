"""
security 模块单元测试 - Minecraft 基岩版服务器管理器
覆盖命令校验、API Token 生成等核心功能
"""

import re
import unittest

from bedrock_server_manager.commands import COMMANDS, is_dangerous_command
from bedrock_server_manager.constants import (
    DANGEROUS_COMMANDS,
    DANGEROUS_MULTI_COMMANDS,
    DANGEROUS_SINGLE_COMMANDS,
)
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

    def test_dangerous_command_restart(self):
        """测试危险命令 restart（统一清单后应判定为危险）"""
        ok, err, dangerous = validate_command("restart")
        self.assertTrue(ok)
        self.assertTrue(dangerous)

    def test_dangerous_command_kill(self):
        """测试危险命令 kill"""
        ok, err, dangerous = validate_command("kill @e")
        self.assertTrue(ok)
        self.assertTrue(dangerous)

    def test_dangerous_multi_command(self):
        """测试多词危险指令 whitelist remove / allowlist remove"""
        ok, err, dangerous = validate_command("whitelist remove Steve")
        self.assertTrue(ok)
        self.assertTrue(dangerous)
        ok, err, dangerous = validate_command("allowlist remove Steve")
        self.assertTrue(ok)
        self.assertTrue(dangerous)

    def test_command_with_slash_prefix(self):
        """测试带 / 前缀的命令"""
        ok, err, dangerous = validate_command("/list")
        self.assertTrue(ok)


class TestDangerousCommandsConsistency(unittest.TestCase):
    """危险命令清单单一事实来源一致性测试"""

    def test_restart_in_unified_list(self):
        """统一清单应包含 restart（此前 security 与前端均缺失）"""
        self.assertIn("restart", DANGEROUS_SINGLE_COMMANDS)

    def test_single_and_multi_union(self):
        """汇总集合等于单次与多词清单的并集"""
        self.assertEqual(
            DANGEROUS_COMMANDS,
            DANGEROUS_SINGLE_COMMANDS | frozenset(DANGEROUS_MULTI_COMMANDS),
        )

    def test_commands_metadata_synced_with_list(self):
        """commands.COMMANDS 元数据中的危险标记与统一清单一致"""
        for name in DANGEROUS_COMMANDS:
            if name in COMMANDS:
                self.assertTrue(
                    COMMANDS[name].get("dangerous"),
                    f"COMMANDS['{name}'] 缺少 dangerous 标记",
                )

    def test_security_matches_commands_for_single(self):
        """security 与 commands 对统一单次危险命令的判断一致"""
        for name in DANGEROUS_SINGLE_COMMANDS:
            if name in COMMANDS:
                _, _, dangerous = validate_command(name)
                self.assertTrue(dangerous, f"security 未将 {name} 判定为危险")
                self.assertTrue(
                    is_dangerous_command(name),
                    f"commands 未将 {name} 判定为危险",
                )

    def test_security_matches_commands_for_multi(self):
        """security 对多词危险指令前缀精确判定；主词本身不误判"""
        for prefix in DANGEROUS_MULTI_COMMANDS:
            _, _, dangerous = validate_command(prefix + " Steve")
            self.assertTrue(dangerous, f"security 未将 '{prefix}' 判定为危险")
            self.assertIn(prefix, DANGEROUS_COMMANDS)
        # 主词单独使用（whitelist list / on / off）不应判定为危险
        for cmd in ("whitelist list", "allowlist list", "whitelist on"):
            _, _, dangerous = validate_command(cmd)
            self.assertFalse(dangerous, f"'{cmd}' 不应是危险命令")

    def test_safe_commands_not_dangerous(self):
        """普通命令不应被误判为危险"""
        for cmd in ("list", "say hi", "gamemode creative", "time set day"):
            _, _, dangerous = validate_command(cmd)
            self.assertFalse(dangerous, f"'{cmd}' 不应是危险命令")


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
