"""全模块复查（第三轮）修复项的回归测试。

每个用例对应审查报告里一个已复现的问题，防止回退。
"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import MagicMock, patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestAtomicWriteConcurrency(unittest.TestCase):
    """atomic_write_text：同进程多线程写同一文件不得丢目标文件"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rv_atomic_")
        self.target = os.path.join(self.tmp, "settings.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_writers_never_lose_file(self):
        from bedrock_server_manager.utils import atomic_write_text

        threads_n, rounds = 6, 12
        lost, errors = 0, 0
        for r in range(rounds):
            barrier = threading.Barrier(threads_n)

            def worker(i, _barrier=barrier, _round=r):
                nonlocal errors
                _barrier.wait()
                try:
                    atomic_write_text(self.target, json.dumps({"r": _round, "w": i}))
                except Exception:
                    errors += 1

            ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            if not os.path.exists(self.target):
                lost += 1
            else:
                with open(self.target, encoding="utf-8") as f:
                    json.load(f)  # 必须是完整 JSON

        self.assertEqual(lost, 0, f"{lost}/{rounds} 轮目标文件消失")
        self.assertEqual(errors, 0, f"{errors} 次写入异常")
        leftovers = [n for n in os.listdir(self.tmp) if n != "settings.json"]
        self.assertEqual(leftovers, [], f"临时文件残留: {leftovers}")

    def test_existing_file_kept_on_encode_failure(self):
        from bedrock_server_manager.utils import atomic_write_text

        with open(self.target, "w", encoding="utf-8") as f:
            f.write("ORIGINAL")
        with self.assertRaises(UnicodeEncodeError):
            atomic_write_text(self.target, "中文", encoding="ascii")
        with open(self.target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "ORIGINAL")
        self.assertEqual([n for n in os.listdir(self.tmp) if n != "settings.json"], [])


class TestPathHelpers(unittest.TestCase):
    """is_path_within 根目录边界 & is_dangerous_target 子目录"""

    def test_root_base_includes_children(self):
        from bedrock_server_manager.utils import is_path_within

        root = os.path.abspath(os.sep)
        child = os.path.join(os.path.abspath(os.getcwd()), "x.txt")
        self.assertTrue(is_path_within(child, root), "根目录作为 base 时子路径应判为在内部")

    def test_dangerous_target_covers_subdirs(self):
        from bedrock_server_manager.utils import is_dangerous_target

        if os.name == "nt":
            self.assertTrue(is_dangerous_target(r"C:\Windows\System32"))
            self.assertTrue(is_dangerous_target(r"C:\Windows"))
        else:
            self.assertTrue(is_dangerous_target("/etc/ssh"))

    def test_dangerous_prefix_not_matched_without_separator(self):
        """C:\\WindowsFoo 不能因为前缀相同被判成 C:\\Windows 的子目录"""
        from bedrock_server_manager.utils import is_dangerous_target

        if os.name == "nt":
            self.assertFalse(is_dangerous_target(r"C:\WindowsFoo"))


class TestAuditLogConcurrency(unittest.TestCase):
    """审计日志：并发轮转不得丢条目"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old = settings.get("server_dir", "")
        self.tmp = tempfile.mkdtemp(prefix="rv_audit_")
        settings["server_dir"] = self.tmp

    def tearDown(self):
        from bedrock_server_manager import settings

        settings["server_dir"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rotated_generations_are_read(self):
        """/api/audit 必须能读到轮转后的历史（否则轮转瞬间界面变空）"""
        from bedrock_server_manager import security

        orig = security.AUDIT_LOG_MAX_BYTES
        try:
            security.AUDIT_LOG_MAX_BYTES = 200
            for i in range(30):
                security.audit_log(f"A{i}", "x" * 10, self.tmp)
            # 确认确实发生了轮转（否则本用例没有意义）
            self.assertTrue(os.path.exists(os.path.join(self.tmp, "audit.1.log")), "未发生轮转")
            logs = security.read_audit_log(self.tmp, limit=10)
        finally:
            security.AUDIT_LOG_MAX_BYTES = orig
        self.assertGreater(len(logs), 2, "只读到了 audit.log，没跨代际读取")
        self.assertEqual(logs[-1]["action"], "A29", "读到的最新一条不对")
        # 顺序必须是旧 → 新
        actions = [item["action"] for item in logs]
        self.assertEqual(actions, sorted(actions, key=lambda a: int(a[1:])))

    def test_tail_lines_keeps_complete_first_line(self):
        """start 恰好落在行首时不得丢掉完整的一行"""
        from bedrock_server_manager.security import _tail_lines

        path = os.path.join(self.tmp, "fixed.log")
        # 用二进制写，避免 Windows 文本模式把 \n 变成 \r\n 使行长变成 13
        with open(path, "wb") as f:
            for i in range(1000):
                f.write(f"line{i:07d}\n".encode())
        # 每行 12 字节；max_read=120 → start 正好落在第 990 行行首
        got = _tail_lines(path, 10, max_read=120)
        self.assertEqual(len(got), 10, f"丢了完整行: {got[:3]}")
        self.assertEqual(got[-1], "line0000999")
        self.assertEqual(got[0], "line0000990")


class TestTofuMacDeletionBypass(unittest.TestCase):
    """删掉 .mac 不应让伪造的清单被信任"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rv_tofu_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_mac_with_magic_is_rejected(self):
        from bedrock_server_manager import verify as V

        cache_file = os.path.join(self.tmp, "hashes.json")
        with (
            patch.object(V, "_HASH_CACHE_FILE", cache_file),
            patch.object(V, "_get_cache_key", return_value=b"k" * 32),
        ):
            # 伪造：带 magic（说明是签名格式）但没有任何 .mac
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"magic": V._HASH_CACHE_MAGIC, "hashes": {"1.0": "evil"}}, f)
            self.assertEqual(V._load_hash_cache(), {}, "签名缺失的清单被接受了")

    def test_legacy_unsigned_cache_is_upgraded(self):
        from bedrock_server_manager import verify as V

        cache_file = os.path.join(self.tmp, "hashes.json")
        mac_file = os.path.join(self.tmp, "hashes.json.mac")
        with (
            patch.object(V, "_HASH_CACHE_FILE", cache_file),
            patch.object(V, "_mac_file_path", return_value=mac_file),
            patch.object(V, "_get_cache_key", return_value=b"k" * 32),
        ):
            # 真正的旧版格式：裸 dict，没有 magic
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"1.0": "abc"}, f)
            self.assertEqual(V._load_hash_cache(), {"1.0": "abc"})
            self.assertTrue(os.path.exists(mac_file), "旧格式未被升级为签名格式")


class TestPeSignature(unittest.TestCase):
    """PE 签名：GUID/错误码常量修复后必须能真正区分有效/篡改"""

    @unittest.skipUnless(os.name == "nt", "仅 Windows")
    def test_tampered_signed_binary_fails_in_default_mode(self):
        """篡改必须失败——早期错误码常量写错，篡改落进"未知"分支被放行"""
        import sys

        from bedrock_server_manager.verify import verify_pe_signature

        if not os.path.isfile(sys.executable):
            self.skipTest("找不到可复制的已签名可执行文件")
        tmp = tempfile.mkdtemp(prefix="rv_pe_")
        try:
            dst = os.path.join(tmp, "tampered.exe")
            shutil.copy2(sys.executable, dst)
            with open(dst, "r+b") as f:
                f.seek(0x1000)
                b = f.read(1)
                f.seek(0x1000)
                f.write(bytes([b[0] ^ 0xFF]))
            ok, msg = verify_pe_signature(dst)
            self.assertFalse(ok, f"篡改后仍判通过: {msg}")
            self.assertIn("篡改", msg)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipUnless(os.name == "nt", "仅 Windows")
    def test_signer_identity_resolves_for_embedded_signed(self):
        """签名者解析必须真的能拿到名字（早期 CryptQueryObject 标志位写错）"""
        import sys

        from bedrock_server_manager.verify import _verify_signer_identity

        ok, msg = _verify_signer_identity(sys.executable)
        self.assertIn("签名者: ", msg, f"未能解析签名者: {msg}")

    def test_unsigned_is_advisory_in_default_mode(self):
        from bedrock_server_manager.verify import verify_pe_signature

        path = os.path.join(tempfile.mkdtemp(prefix="rv_pe2_"), "fake.exe")
        try:
            with open(path, "wb") as f:
                f.write(b"NOT-A-PE-FILE")
            ok, msg = verify_pe_signature(path)
            self.assertFalse(ok, "非 PE 文件必须失败")
            self.assertIn("MZ", msg)
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_strict_flag_via_env(self):
        from bedrock_server_manager.verify import _env_flag

        with patch.dict(os.environ, {"MBES_REQUIRE_SIGNATURE": "1"}):
            self.assertTrue(_env_flag("MBES_REQUIRE_SIGNATURE"))
        with patch.dict(os.environ, {"MBES_REQUIRE_SIGNATURE": "0"}):
            self.assertFalse(_env_flag("MBES_REQUIRE_SIGNATURE"))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_env_flag("MBES_REQUIRE_SIGNATURE"))


class TestStagedInstallAtomic(unittest.TestCase):
    """合并阶段必须原子替换：失败时不得丢 bedrock_server.exe"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rv_stage_")
        self.target = os.path.join(self.tmp, "server")
        self.staging = os.path.join(self.tmp, "staging")
        os.makedirs(self.target)
        os.makedirs(self.staging)
        for name, data in (("bedrock_server.exe", b"OLD"), ("libCrypto.dll", b"OLDDLL")):
            with open(os.path.join(self.target, name), "wb") as f:
                f.write(data)
        for name, data in (("bedrock_server.exe", b"NEW"), ("libCrypto.dll", b"NEWDLL")):
            with open(os.path.join(self.staging, name), "wb") as f:
                f.write(data)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copy_failure_keeps_old_binary(self):
        from bedrock_server_manager.install import apply_staged_install

        real_copy2 = shutil.copy2

        def boom(src, dst, **kw):
            if str(src).endswith("bedrock_server.exe"):
                raise OSError(28, "No space left on device")
            return real_copy2(src, dst, **kw)

        with patch("bedrock_server_manager.install.shutil.copy2", side_effect=boom), self.assertRaises(OSError):
            apply_staged_install(self.staging, self.target)

        with open(os.path.join(self.target, "bedrock_server.exe"), "rb") as f:
            self.assertEqual(f.read(), b"OLD", "合并失败后旧 exe 丢了")
        with open(os.path.join(self.target, "libCrypto.dll"), "rb") as f:
            self.assertEqual(f.read(), b"OLDDLL")
        leftovers = [n for n in os.listdir(self.target) if ".install_" in n]
        self.assertEqual(leftovers, [], f"残留临时项: {leftovers}")

    def test_successful_merge_replaces_binaries(self):
        from bedrock_server_manager.install import apply_staged_install

        apply_staged_install(self.staging, self.target)
        with open(os.path.join(self.target, "bedrock_server.exe"), "rb") as f:
            self.assertEqual(f.read(), b"NEW")

    def test_official_content_dirs_overlay_not_frozen(self):
        """resource_packs 属官方内容，应随版本更新而不是整份冻结"""
        from bedrock_server_manager import install

        self.assertIn("resource_packs", install._OVERLAY_ON_MERGE)
        self.assertNotIn("resource_packs", install._PRESERVE_ON_MERGE)
        # 用户数据仍必须保留
        for name in ("worlds", "server.properties", "allowlist.json", "permissions.json"):
            self.assertIn(name, install._PRESERVE_ON_MERGE)

    def test_staging_dir_protected_from_server_only_delete(self):
        """delete_server(server_only) 不能删掉正在使用的安装暂存目录"""

        with open(os.path.join(REPO, "bedrock_server_manager", "backup.py"), encoding="utf-8") as f:
            self.assertIn(".install_staging", f.read())


class TestValidateVersionNewline(unittest.TestCase):
    """版本号校验不得接受结尾换行（$ 与 \\Z 的差别）"""

    def test_trailing_newline_rejected(self):
        from bedrock_server_manager.install import validate_version

        for bad in ("1.2\n", "1.2\r\n", "1.2\nX"):
            ok, _err = validate_version(bad)
            self.assertFalse(ok, f"{bad!r} 应被拒绝")

    def test_normal_versions_pass(self):
        from bedrock_server_manager.install import validate_version

        for good in ("1.21.0.03", "1.26.63.1", "1.0-rc1", ""):
            ok, err = validate_version(good)
            self.assertTrue(ok, f"{good!r} 应通过: {err}")


class TestPacksOverwriteHygiene(unittest.TestCase):
    """包覆盖导入：single-root 分支也要先清旧目录；落盘名要参与守卫"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rv_pack_")
        self.server = os.path.join(self.tmp, "server")
        os.makedirs(os.path.join(self.server, "resource_packs"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _zip_b64(files):
        import base64
        import io

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in files.items():
                zf.writestr(name, data)
        return base64.b64encode(buf.getvalue()).decode()

    def test_overwrite_removes_stale_files(self):
        from bedrock_server_manager.packs import import_pack

        v1 = self._zip_b64({"P/manifest.json": "{}", "P/old.png": "x", "P/stale.txt": "y"})
        v2 = self._zip_b64({"P/manifest.json": "{}", "P/new.png": "z"})
        self.assertTrue(import_pack(self.server, "resource", v1, "p.zip")[0])
        ok, err, _ = import_pack(self.server, "resource", v2, "p.zip", overwrite=True)
        self.assertTrue(ok, err)
        files = sorted(os.listdir(os.path.join(self.server, "resource_packs", "P")))
        self.assertEqual(files, ["manifest.json", "new.png"], f"旧文件残留: {files}")

    def test_sanitized_root_name_still_guarded(self):
        """ZIP 根名含非法字符时，落盘名也要参与"已存在则拒绝"判断"""
        from bedrock_server_manager.packs import import_pack

        data = self._zip_b64({"Bad<Root>|Name/manifest.json": "{}"})
        ok, err, _ = import_pack(self.server, "resource", data, "p.zip")
        self.assertTrue(ok, err)
        ok2, err2, _ = import_pack(self.server, "resource", data, "p.zip")
        self.assertFalse(ok2, "特殊字符根名绕过了覆盖守卫")
        self.assertIn("已存在同名包", err2)

    def test_sanitize_helper(self):
        from bedrock_server_manager.packs import _sanitize_path_component

        self.assertEqual(_sanitize_path_component("Bad<Root>|Name"), "Bad_Root__Name")
        self.assertEqual(_sanitize_path_component(".."), "")
        self.assertEqual(_sanitize_path_component("A" * 200)[:1], "A")
        self.assertLessEqual(len(_sanitize_path_component("A" * 200)), 80)


class TestBanIpConsistency(unittest.TestCase):
    """运行中封禁/解封必须与文件一致（UI 读的是文件）"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old = settings.get("server_dir", "")
        self.tmp = tempfile.mkdtemp(prefix="rv_ban_")
        settings["server_dir"] = self.tmp

    def tearDown(self):
        from bedrock_server_manager import settings

        settings["server_dir"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ban_while_running_writes_file_and_sends_command(self):
        from bedrock_server_manager import players

        proc = MagicMock()
        proc.send.return_value = None
        with (
            patch.object(players, "server_running", return_value=True),
            patch.object(players, "get_server_proc", return_value=proc),
        ):
            ok, err = players.ban_ip("1.2.3.4", "作弊", self.tmp)
        self.assertTrue(ok, err)
        proc.send.assert_called_once()
        self.assertEqual([e.get("ip") for e in players.load_banned_ips(self.tmp)], ["1.2.3.4"])

    def test_pardon_while_running_works_even_if_file_missing(self):
        from bedrock_server_manager import players

        proc = MagicMock()
        with (
            patch.object(players, "server_running", return_value=True),
            patch.object(players, "get_server_proc", return_value=proc),
        ):
            ok, err = players.pardon_ip("9.9.9.9", self.tmp)
        self.assertTrue(ok, err)
        proc.send.assert_called_once()

    def test_pardon_when_stopped_requires_existing(self):
        from bedrock_server_manager import players

        with patch.object(players, "server_running", return_value=False):
            ok, err = players.pardon_ip("9.9.9.9", self.tmp)
        self.assertFalse(ok)
        self.assertIn("不在封禁列表", err)


class TestBackupCleanupScope(unittest.TestCase):
    """清理只应作用于 <服务器名>_worlds_<14位时间戳> 形态"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rv_bk_")
        self.root = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_user_dir_with_similar_name_survives(self):
        from bedrock_server_manager.backup import _cleanup_old_backups

        os.makedirs(os.path.join(self.root, "srv_worlds_notes"))  # 用户目录
        os.makedirs(os.path.join(self.root, "srv_worlds_20260101_000000"))
        os.makedirs(os.path.join(self.root, "srv_worlds_20260102_000000"))
        with open(os.path.join(self.root, "user_backup.zip"), "wb") as f:
            f.write(b"x")

        _cleanup_old_backups(self.root, 1, "srv")

        self.assertTrue(os.path.isdir(os.path.join(self.root, "srv_worlds_notes")), "用户目录被删了")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "user_backup.zip")), "无关 zip 被删了")
        remaining = [n for n in os.listdir(self.root) if n.startswith("srv_worlds_2")]
        self.assertEqual(len(remaining), 1, f"保留数量不对: {remaining}")


class TestRequestBodyOversize(unittest.TestCase):
    """超大请求体必须 413 + 断开连接，不能返回 {} 假装成功"""

    def test_post_data_returns_none_when_oversize(self):
        from bedrock_server_manager.web import handler as H

        h = H.Handler.__new__(H.Handler)
        h.headers = {"Content-Length": str(H.MAX_BODY_BYTES + 1)}
        self.assertIsNone(h._post_data())

    def test_post_data_returns_none_on_bad_length(self):
        from bedrock_server_manager.web import handler as H

        h = H.Handler.__new__(H.Handler)
        h.headers = {"Content-Length": "not-a-number"}
        self.assertIsNone(h._post_data())

    def test_non_ascii_token_does_not_raise(self):
        """secrets.compare_digest 对非 ASCII 会抛 TypeError，必须安全失败"""
        from bedrock_server_manager.web import handler as H

        h = H.Handler.__new__(H.Handler)
        h.headers = {"X-API-Token": "令牌"}
        h.path = "/api/status"
        h.command = "GET"
        try:
            result = h._check_auth()
        except TypeError as e:
            self.fail(f"非 ASCII token 触发 TypeError: {e}")
        self.assertFalse(result)


class TestSchedulerStatePruning(unittest.TestCase):
    """删除任务后其执行状态应被清理（否则同 id 新任务被吃掉一个周期）"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old = settings.get("server_dir", "")
        self.tmp = tempfile.mkdtemp(prefix="rv_sched_")
        settings["server_dir"] = self.tmp

    def tearDown(self):
        from bedrock_server_manager import settings

        settings["server_dir"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_state_pruned_on_save(self):
        from bedrock_server_manager import scheduler

        now = __import__("time").time()
        scheduler.save_scheduler_state({"gone": now, "keep": now})
        ok, err = scheduler.save_scheduled_tasks(
            [{"id": "keep", "type": "backup", "enabled": True, "schedule_type": "hourly"}]
        )
        self.assertTrue(ok, err)
        state = scheduler.load_scheduler_state()
        self.assertIn("keep", state)
        self.assertNotIn("gone", state, f"已删除任务的状态没被清理: {state}")

    def test_invalid_time_rejected(self):
        from bedrock_server_manager.scheduler import validate_scheduled_task

        for bad in ("99:99", "24:00", "12:60"):
            ok, _ = validate_scheduled_task(
                {"id": "x", "type": "backup", "enabled": True, "schedule_type": "daily", "time": bad}
            )
            self.assertFalse(ok, f"{bad} 应被拒绝")


class TestPortCheckRobustness(unittest.TestCase):
    """端口探测不得因非法端口抛异常，也不得把环境错误误判为占用"""

    def test_invalid_port_returns_true(self):
        from bedrock_server_manager.server import check_port_in_use

        for p in (70000, -1, 0):
            self.assertTrue(check_port_in_use(p), f"非法端口 {p} 应返回占用")

    def test_free_port_returns_false(self):
        import socket

        from bedrock_server_manager.server import check_port_in_use

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(check_port_in_use(port), f"空闲端口 {port} 被判占用")


class TestFrontendConsoleClear(unittest.TestCase):
    """清空控制台必须同时清本地缓存，否则搜索框会把旧日志渲染回来"""

    @staticmethod
    def _index_html():
        with open(os.path.join(REPO, "bedrock_server_manager", "web", "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_console_plain_cleared(self):
        html = self._index_html()
        idx = html.index("btnClearLog').onclick")
        block = html[idx : idx + 700]
        self.assertIn("consolePlain=''", block, "清空日志未清 consolePlain")

    def test_escape_uses_confirm_cancel(self):
        """Esc 关闭确认弹窗必须走取消回调，否则确认被绕过"""
        html = self._index_html()
        idx = html.index("Escape 键关闭弹窗")
        block = html[idx : idx + 800]
        self.assertIn("confirmCancel", block, "Esc 未走取消回调")


if __name__ == "__main__":
    unittest.main(verbosity=2)
