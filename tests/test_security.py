"""
security 模块单元测试 - Minecraft 基岩版服务器管理器
覆盖命令校验、API Token 生成等核心功能
"""

import unittest

from bedrock_server_manager.commands import COMMANDS, is_dangerous_command
from bedrock_server_manager.constants import (
    DANGEROUS_COMMANDS,
    DANGEROUS_MULTI_COMMANDS,
    DANGEROUS_SINGLE_COMMANDS,
)
from bedrock_server_manager.security import (
    validate_command,
    validate_custom_url,
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

    def test_slash_prefixed_dangerous_detected(self):
        """前导 / 不应成为绕过危险判定的手段"""
        for cmd in ("/stop", "/restart", "  /kill @e", "//stop"):
            ok, _err, dangerous = validate_command(cmd)
            self.assertTrue(ok, cmd)
            self.assertTrue(dangerous, f"{cmd} 应判定为危险指令")

    def test_reject_newline_injection(self):
        """换行符可向 stdin 注入多条指令，必须拒绝"""
        for cmd in ("list\nstop", "list\r\nop Steve", "say hi\nstop", "list\rstop"):
            ok, err, _dangerous = validate_command(cmd)
            self.assertFalse(ok, f"{cmd!r} 不应通过校验")
            self.assertIn("控制字符", err)

    def test_reject_other_control_chars(self):
        """其余不可打印控制字符同样拒绝"""
        for cmd in ("list\x00", "list\x1b[2J", "say a\x7fb"):
            ok, _err, _dangerous = validate_command(cmd)
            self.assertFalse(ok, f"{cmd!r} 不应通过校验")

    def test_allow_tab_and_unicode_payload(self):
        """制表符与正常 Unicode 内容不应被误杀"""
        ok, _err, _dangerous = validate_command("say 你好\t世界")
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


class TestValidateCustomUrl(unittest.TestCase):
    """自定义下载地址校验（SSRF 与官方域名判定）"""

    def test_official_domains_detected(self):
        for url in (
            "https://www.minecraft.net/x.zip",
            "https://minecraft.net/x.zip",
            "https://bedrock.dev/x.zip",
            "https://example.minecraftservices.net/x.zip",
        ):
            ok, _err, official = validate_custom_url(url)
            self.assertTrue(ok, url)
            self.assertTrue(official, f"{url} 应判定为官方来源")

    def test_lookalike_domain_rejected_as_official(self):
        """后缀伪造域名不得被判为官方（endswith 漏洞）"""
        for url in (
            "https://fakeminecraft.net/x.zip",
            "https://evil-minecraft.net/x.zip",
            "https://notminecraft.net/x.zip",
            "https://minecraft.net.evil.com/x.zip",
        ):
            ok, _err, official = validate_custom_url(url)
            self.assertTrue(ok, url)
            self.assertFalse(official, f"{url} 不应判定为官方来源")

    def test_ssrf_internal_addresses_rejected(self):
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8080/x.zip",
            "http://localhost/x.zip",
            "http://[::1]/x.zip",
            "http://10.0.0.5/x",
            "http://192.168.1.10/x",
            "http://172.16.0.1/x",
            "http://0.0.0.0/x",
        ):
            ok, _err, _official = validate_custom_url(url)
            self.assertFalse(ok, f"{url} 应被拒绝")

    def test_userinfo_disguise_rejected(self):
        ok, _err, _official = validate_custom_url("http://minecraft.net@evil.com/x.zip")
        self.assertFalse(ok)

    def test_non_http_scheme_rejected(self):
        for url in ("file:///C:/windows/win.ini", "ftp://example.com/x.zip", "javascript:alert(1)"):
            ok, _err, _official = validate_custom_url(url)
            self.assertFalse(ok, f"{url} 应被拒绝")

    def test_overlong_url_rejected(self):
        ok, _err, _official = validate_custom_url("https://example.com/" + "a" * 3000)
        self.assertFalse(ok)

    def test_effective_url_after_redirect(self):
        """重定向到内网地址必须被兜底拒绝"""
        from bedrock_server_manager.security import validate_effective_url

        ok, err = validate_effective_url("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(ok)
        self.assertIn("重定向", err)
        ok, _err = validate_effective_url("https://example.com/x.zip")
        self.assertTrue(ok)
