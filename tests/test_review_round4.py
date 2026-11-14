"""第五轮复查修复项的回归测试。

对应审查报告：
- scheduler 切换服务器目录后状态串目录
- /api/server/restart 未同步 server_dir
- 启动入口的端口预检未集中（托盘/安装/调度绕过）
- README 声称的审计覆盖实际缺失
- 前端缺少"覆盖导入"入口
- verify 用文案判断"已放行"（结构化标志替代）
- CI 的 Node 版本与 glob 用法自相矛盾
"""

import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    """读取仓库内文件（UTF-8）。"""
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


class TestSchedulerStateIsolation(unittest.TestCase):
    """切换服务器目录后，执行状态必须跟随目录，不能串味"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old = settings.get("server_dir", "")
        self.tmp = tempfile.mkdtemp(prefix="rv_sched_")
        self.a = os.path.join(self.tmp, "A")
        self.b = os.path.join(self.tmp, "B")
        os.makedirs(self.a)
        os.makedirs(self.b)

    def tearDown(self):
        from bedrock_server_manager import settings

        settings["server_dir"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_state_file_follows_server_dir(self):
        from bedrock_server_manager import scheduler, settings

        settings["server_dir"] = self.a
        now = time.time()
        scheduler.save_scheduler_state({"t1": now})
        self.assertEqual(scheduler.load_scheduler_state(), {"t1": now})

        settings["server_dir"] = self.b
        self.assertEqual(scheduler.load_scheduler_state(), {}, "B 目录读到了 A 的状态")

    def test_loop_reloads_on_dir_switch(self):
        """调度循环必须检测 state 路径变化并重新加载（核心修复点）"""
        src = _read("bedrock_server_manager", "scheduler.py")
        self.assertIn("state_dir", src, "调度循环未记录状态所属目录")
        self.assertIn("current_state_path != state_dir", src, "未检测目录切换")
        # 写回时必须再确认一次目录没变
        self.assertIn("_state_path() == state_dir", src)


class TestRestartSyncsServerDir(unittest.TestCase):
    """/api/server/restart 必须把当前服务器目录同步给 ServerProcess"""

    def test_restart_sets_server_dir(self):
        from bedrock_server_manager.web.app import AppContext as WebAppContext

        app = WebAppContext.instance()
        proc = MagicMock()
        d = os.path.abspath(os.sep)  # 只要非空即可
        with (
            patch.object(type(app), "server_dir", new_callable=lambda: property(lambda self: d)),
            patch.object(app, "get_server_proc", return_value=proc),
            patch("bedrock_server_manager.web.app.time.sleep"),
        ):
            ok, err = app.restart_server()
        self.assertTrue(ok, err)
        self.assertEqual(proc.server_dir, d, "restart 没有同步 server_dir")
        proc.stop.assert_called_once()
        proc.start.assert_called_once()

    def test_restart_requires_dir(self):
        from bedrock_server_manager.web.app import AppContext as WebAppContext

        app = WebAppContext.instance()
        with patch.object(type(app), "server_dir", new_callable=lambda: property(lambda self: "")):
            ok, err = app.restart_server()
        self.assertFalse(ok)
        self.assertIn("未设置服务器目录", err)


class TestPortPrecheckCentralized(unittest.TestCase):
    """端口预检必须在 ServerProcess.start 内（所有入口都经过它）"""

    def test_start_raises_on_occupied_port(self):
        from bedrock_server_manager import server as S
        from bedrock_server_manager.constants import SERVER_EXE

        proc = S.ServerProcess(tempfile.mkdtemp(prefix="rv_srv_"), on_output=lambda _l: None, on_state=lambda _s: None)
        try:
            # 造一个存在的 exe，让流程走到端口检查
            open(os.path.join(proc.server_dir, SERVER_EXE), "wb").close()
            with (
                patch.object(S, "check_port_in_use", return_value=True),
                patch.object(S, "get_server_port", return_value=19132),
                self.assertRaises(RuntimeError) as ctx,
            ):
                proc.start()
            self.assertIn("19132", str(ctx.exception))
            self.assertIsNone(proc.proc, "端口占用时不应启动进程")
        finally:
            shutil.rmtree(proc.server_dir, ignore_errors=True)

    def test_source_has_no_warning_only_branch(self):
        """不能再是"只警告然后照常启动"（那会导致崩溃重启循环）"""
        src = _read("bedrock_server_manager", "server.py")
        idx = src.index("def start(self)")
        block = src[idx : idx + 2000]
        self.assertIn("raise RuntimeError", block, "端口占用时未拒绝启动")
        self.assertNotIn("服务器可能无法正常启动", block)


class TestAuditCoverage(unittest.TestCase):
    """README 声称的审计范围必须真的有对应调用"""

    REQUIRED = {
        "SERVER_START": "启动服务器",
        "SERVER_STOP": "停止服务器",
        "SERVER_RESTART": "重启服务器",
        "SERVER_DELETE": "删除服务器",
        "CONFIG_SAVE": "配置修改",
        "PLAYER_KICK": "踢出玩家",
        "PLAYER_BAN": "封禁玩家",
        "BACKUP_CREATE": "创建备份",
        "BACKUP_RESTORE": "恢复备份",
        "WORLD_DELETE": "删除世界",
    }

    def _all_source(self):
        parts = []
        pkg = os.path.join(REPO, "bedrock_server_manager")
        for dirpath, _dirs, files in os.walk(pkg):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                        parts.append(f.read())
        return "\n".join(parts)

    def test_required_actions_are_logged(self):
        src = self._all_source()
        missing = [
            f"{action}（{desc}）" for action, desc in self.REQUIRED.items() if f'audit_log("{action}"' not in src
        ]
        self.assertEqual(missing, [], f"审计覆盖缺失: {missing}")

    def test_readme_claim_matches(self):
        """README 的审计描述不应再超出实际覆盖"""
        repo_readme = _read("README.md")
        idx = repo_readme.index("**操作审计日志**")
        row = repo_readme[idx : idx + 400]
        for keyword in ("备份", "玩家", "配置"):
            self.assertIn(keyword, row, f"README 审计描述缺少 {keyword}")


class TestFrontendPackOverwrite(unittest.TestCase):
    """前端必须提供"覆盖导入"入口（后端提示了但 UI 没有）"""

    def _html(self):
        return _read("bedrock_server_manager", "web", "index.html")

    def test_sends_overwrite_after_confirm(self):
        html = self._html()
        idx = html.index("packs/import")
        block = html[max(0, idx - 1200) : idx + 1600]
        self.assertIn("overwrite", block, "前端未传 overwrite")
        self.assertIn("已存在同名包", block, "前端未处理同名冲突")
        self.assertIn("showConfirm", block, "冲突时未弹确认框")


class TestCiNodeVersion(unittest.TestCase):
    """CI 的 Node 版本必须支持 --test 的 glob 用法"""

    def test_node_version_supports_glob(self):
        ci = _read(".github", "workflows", "ci.yml")
        import re

        m = re.search(r'node-version:\s*"(\d+)"', ci)
        self.assertIsNotNone(m)
        ver = int(m.group(1))
        if 'node --test "tests/js/**/*.test.mjs"' in ci:
            self.assertGreaterEqual(ver, 22, f"Node {ver} 不支持 --test 的 glob，frontend job 会恒红")


class TestPeVerdictStructured(unittest.TestCase):
    """签名结论必须用结构化标志，不能靠"已放行"文案判断"""

    def test_verdict_returns_four_flags(self):
        import tempfile

        from bedrock_server_manager.verify import _pe_signature_verdict

        d = tempfile.mkdtemp(prefix="rv_verdict_")
        try:
            p = os.path.join(d, "x.exe")
            with open(p, "wb") as f:
                f.write(b"NOT-A-PE")
            ok, msg, critical, advisory = _pe_signature_verdict(p)
            self.assertFalse(ok)
            self.assertFalse(critical, "格式错误不等于篡改")
            self.assertFalse(advisory)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_download_full_no_longer_uses_message_matching(self):
        src = _read("bedrock_server_manager", "verify.py")
        idx = src.index("def verify_download_full(")
        block = src[idx : idx + 6000]
        self.assertNotIn('"已放行" in', block, "verify_download_full 仍在用文案匹配做安全分流")
        self.assertIn("advisory", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
