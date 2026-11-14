"""为原先 0 覆盖的两个关键门禁补测试：

1. ``verify_download_full`` —— 安装/更新的完整性门禁（此前整段未被覆盖）
2. ``_scheduler_loop`` —— 调度循环（此前同样 0 覆盖，且刚修过"跨目录串味"）

这两个函数是最危险的路径：一个决定"下下来的包能不能装"，一个决定
"什么时候重启/备份/发公告"。
"""

import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

import bedrock_server_manager as bsm
import bedrock_server_manager.verify as V


def _make_zip(path, exe_bytes=b"MZ-fake", pad=0, extra=None):
    """构造一个测试用 BDS zip（可带填充文件把体积撑过大小下限）。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(V.SERVER_EXE, exe_bytes)
        if pad:
            zf.writestr("pad.bin", os.urandom(pad))
        for name, data in (extra or {}).items():
            zf.writestr(name, data)


class TestVerifyDownloadFull(unittest.TestCase):
    """安装完整性门禁"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rv_vdf_")
        # 用临时目录隔离 TOFU 缓存，避免污染真实用户数据
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(self.cache, exist_ok=True)
        self._patches = [
            patch.object(V, "_HASH_CACHE_FILE", os.path.join(self.cache, "hashes.json")),
            patch.object(V, "_mac_file_path", return_value=os.path.join(self.cache, "hashes.json.mac")),
            patch.object(V, "_get_cache_key", return_value=b"k" * 32),
            patch.object(V, "_LEGACY_HASH_CACHE_FILE", os.path.join(self.tmp, "legacy.json")),
            patch.object(V, "_KNOWN_HASHES", {}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _zip(self, name="bds.zip", **kw):
        path = os.path.join(self.tmp, name)
        _make_zip(path, **kw)
        return path

    # ---------------- 基本结构 ----------------

    def test_missing_file_reports_error(self):
        res = V.verify_download_full(os.path.join(self.tmp, "nope.zip"), require_signature=False)
        self.assertFalse(res["ok"])
        self.assertTrue(res["errors"], "不存在的文件必须报错")
        self.assertFalse(res["checks"]["file_size"]["ok"])

    def test_not_a_zip_reports_error(self):
        path = os.path.join(self.tmp, "bad.zip")
        with open(path, "wb") as f:
            f.write(b"x" * 40 * 1024 * 1024)  # 够大，但不是 zip
        res = V.verify_download_full(path, require_signature=False)
        self.assertFalse(res["ok"])
        self.assertFalse(res["checks"]["zip_integrity"]["ok"])

    def test_too_small_file_rejected(self):
        path = self._zip()
        res = V.verify_download_full(path, require_signature=False)
        self.assertFalse(res["ok"], "远小于 30MB 的包必须拒绝")
        self.assertIn("过小", " ".join(res["errors"]))

    # ---------------- 哈希 / TOFU ----------------

    def test_first_download_records_hash(self):
        """首次下载：无预期哈希 → 记录 TOFU 并放行"""
        path = self._zip(pad=V.MIN_BDS_SIZE + 1024)
        res = V.verify_download_full(path, require_signature=False)
        self.assertTrue(res["checks"]["sha256"]["ok"])
        res2 = V.verify_download_full(path, require_signature=False)
        self.assertTrue(res2["ok"], res2["errors"])

    def test_hash_mismatch_rejected(self):
        path = self._zip(pad=V.MIN_BDS_SIZE + 1024)
        res = V.verify_download_full(path, version="1.0", expected_hash="0" * 64, require_signature=False)
        self.assertFalse(res["ok"], "哈希不匹配必须拒绝")
        self.assertFalse(res["checks"]["sha256"]["ok"])

    def test_expected_hash_match_passes(self):
        import hashlib

        path = self._zip(pad=V.MIN_BDS_SIZE + 1024)
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        res = V.verify_download_full(path, expected_hash=digest, require_signature=False)
        self.assertTrue(res["checks"]["sha256"]["ok"])

    # ---------------- 可执行文件 ----------------

    def test_missing_exe_in_zip_rejected(self):
        path = os.path.join(self.tmp, "noexe.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("readme.txt", "hi")
            zf.writestr("pad.bin", os.urandom(V.MIN_BDS_SIZE + 1024))
        res = V.verify_download_full(path, require_signature=False)
        self.assertFalse(res["ok"], "缺可执行文件的包必须拒绝")
        self.assertIn("未找到", " ".join(res["errors"]))

    def test_integrity_violation_fails_even_without_require_signature(self):
        """篡改类失败必须无条件中止（与 require_signature 无关）"""
        path = self._zip(pad=V.MIN_BDS_SIZE + 1024)
        with (
            patch.object(V, "IS_WINDOWS", True),
            patch.object(V, "_pe_signature_verdict", return_value=(False, "摘要不匹配（文件已被篡改）", True, False)),
        ):
            res = V.verify_download_full(path, require_signature=False)
        self.assertFalse(res["ok"], "篡改在非严格模式下也被放行了")
        self.assertIn("篡改", " ".join(res["errors"]))
        self.assertEqual(res["warnings"], [])

    def test_advisory_signature_becomes_warning(self):
        """非严格模式下"未签名"只告警，不阻断安装"""
        path = self._zip(pad=V.MIN_BDS_SIZE + 1024)
        with (
            patch.object(V, "IS_WINDOWS", True),
            patch.object(V, "_pe_signature_verdict", return_value=(True, "未签名…已放行", False, True)),
        ):
            res = V.verify_download_full(path, require_signature=False)
        self.assertTrue(res["ok"], res["errors"])
        self.assertTrue(res["warnings"], "放行时应留下告警")

    def test_strict_mode_rejects_unsigned(self):
        path = self._zip(pad=V.MIN_BDS_SIZE + 1024)
        with (
            patch.object(V, "IS_WINDOWS", True),
            patch.object(V, "_pe_signature_verdict", return_value=(False, "未签名", False, False)),
        ):
            res = V.verify_download_full(path, require_signature=True)
        self.assertFalse(res["ok"])
        self.assertTrue(res["errors"])

    def test_require_signature_defaults_from_env(self):
        path = self._zip(pad=V.MIN_BDS_SIZE + 1024)
        with (
            patch.dict(os.environ, {"MBES_REQUIRE_SIGNATURE": "1"}),
            patch.object(V, "IS_WINDOWS", True),
            patch.object(V, "_pe_signature_verdict", return_value=(False, "未签名", False, False)),
        ):
            res = V.verify_download_full(path)
        self.assertFalse(res["ok"], "环境变量未生效")

    def test_result_shape(self):
        path = self._zip(pad=V.MIN_BDS_SIZE + 1024)
        res = V.verify_download_full(path, require_signature=False)
        for key in ("ok", "file", "version", "checks", "errors", "warnings"):
            self.assertIn(key, res)
        for check in ("file_size", "zip_integrity", "sha256"):
            self.assertIn(check, res["checks"])


class TestSchedulerLoop(unittest.TestCase):
    """调度循环：按 daily/hourly/interval 触发任务，并保持状态隔离

    做法：用真实的调度线程跑起来（``start_scheduler``），短暂等待让第一轮
    迭代执行完，再 ``stop_scheduler`` 立刻中断 ``Event.wait``。
    这样测的是真实循环，而不是把循环体再抄一遍。
    """

    def setUp(self):
        from bedrock_server_manager import scheduler, settings

        self._old = settings.get("server_dir", "")
        self.tmp = tempfile.mkdtemp(prefix="rv_loop_")
        settings["server_dir"] = self.tmp
        self.scheduler = scheduler
        self.tasks_file = os.path.join(self.tmp, bsm.SCHEDULED_TASKS_FILE)

    def tearDown(self):
        from bedrock_server_manager import settings

        self.scheduler.stop_scheduler(timeout=2)
        settings["server_dir"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_tasks(self, tasks, directory=None):
        path = os.path.join(directory or self.tmp, bsm.SCHEDULED_TASKS_FILE)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tasks, f)

    def _run_loop(self, executed, *, running=True, server_dir=None, wait=0.35):
        """跑真实调度循环一小段时间，收集被执行的任务 id。"""

        def fake_execute(task, server_dir_arg, note=None):
            executed.append((task.get("id"), note))

        patches = [
            patch.object(self.scheduler, "server_running", return_value=running),
            patch.object(self.scheduler, "_execute_scheduled_task", side_effect=fake_execute),
        ]
        if server_dir is not None:
            patches.append(patch.object(self.scheduler, "resolve_server_dir", return_value=server_dir))
        try:
            for p in patches:
                p.start()
            self.scheduler.start_scheduler()
            time.sleep(wait)
        finally:
            self.scheduler.stop_scheduler(timeout=2)
            for p in reversed(patches):
                p.stop()

    # ---------------- daily ----------------

    def test_daily_task_fires_when_time_passed(self):
        now_hm = time.strftime("%H:%M")
        self.scheduler.save_scheduler_state({})
        self._write_tasks([{"id": "d1", "type": "backup", "enabled": True, "schedule_type": "daily", "time": now_hm}])
        executed = []
        self._run_loop(executed)
        self.assertEqual([e[0] for e in executed], ["d1"], "daily 任务未触发")

    def test_daily_task_not_refired_same_day(self):
        now_hm = time.strftime("%H:%M")
        self.scheduler.save_scheduler_state({"d1": time.time()})
        self._write_tasks([{"id": "d1", "type": "backup", "enabled": True, "schedule_type": "daily", "time": now_hm}])
        executed = []
        self._run_loop(executed)
        self.assertEqual(executed, [], "同一天重复执行了 daily 任务")

    def test_disabled_task_skipped(self):
        now_hm = time.strftime("%H:%M")
        self.scheduler.save_scheduler_state({})
        self._write_tasks([{"id": "d1", "type": "backup", "enabled": False, "schedule_type": "daily", "time": now_hm}])
        executed = []
        self._run_loop(executed)
        self.assertEqual(executed, [])

    # ---------------- hourly / interval ----------------

    def test_hourly_task_fires_after_one_hour(self):
        self.scheduler.save_scheduler_state({"h1": time.time() - 3700})
        self._write_tasks([{"id": "h1", "type": "announce", "enabled": True, "schedule_type": "hourly"}])
        executed = []
        self._run_loop(executed)
        self.assertEqual([e[0] for e in executed], ["h1"])

    def test_hourly_task_not_refired_within_hour(self):
        self.scheduler.save_scheduler_state({"h1": time.time() - 60})
        self._write_tasks([{"id": "h1", "type": "announce", "enabled": True, "schedule_type": "hourly"}])
        executed = []
        self._run_loop(executed)
        self.assertEqual(executed, [])

    def test_interval_task_fires_when_due(self):
        self.scheduler.save_scheduler_state({"i1": time.time() - 3 * 3600})
        self._write_tasks(
            [{"id": "i1", "type": "restart", "enabled": True, "schedule_type": "interval", "interval_hours": 2}]
        )
        executed = []
        self._run_loop(executed)
        self.assertEqual([e[0] for e in executed], ["i1"])

    def test_interval_task_not_due(self):
        self.scheduler.save_scheduler_state({"i1": time.time() - 600})
        self._write_tasks(
            [{"id": "i1", "type": "restart", "enabled": True, "schedule_type": "interval", "interval_hours": 2}]
        )
        executed = []
        self._run_loop(executed)
        self.assertEqual(executed, [])

    def test_server_not_running_skips_all(self):
        now_hm = time.strftime("%H:%M")
        self.scheduler.save_scheduler_state({})
        self._write_tasks([{"id": "d1", "type": "backup", "enabled": True, "schedule_type": "daily", "time": now_hm}])
        executed = []
        self._run_loop(executed, running=False)
        self.assertEqual(executed, [], "服务器未运行时不应执行任何任务")

    def test_state_persisted_after_run(self):
        now_hm = time.strftime("%H:%M")
        self.scheduler.save_scheduler_state({})
        self._write_tasks([{"id": "d1", "type": "backup", "enabled": True, "schedule_type": "daily", "time": now_hm}])
        self._run_loop([])
        self.assertIn("d1", self.scheduler.load_scheduler_state(), "执行后未持久化状态")

    def test_state_dir_switch_reloads(self):
        """切到另一个服务器目录后，必须重新加载该目录自己的状态

        回归：内存快照曾跨目录复用，导致新目录里同 id 任务被误判为
        "今天已执行"而跳过一整个周期。
        """
        other = tempfile.mkdtemp(prefix="rv_loop2_")
        try:
            now_hm = time.strftime("%H:%M")
            # 旧目录：记下"今天已执行"
            self.scheduler.save_scheduler_state({"d1": time.time()})
            # 新目录：有任务但没有任何状态文件
            self._write_tasks(
                [{"id": "d1", "type": "backup", "enabled": True, "schedule_type": "daily", "time": now_hm}],
                directory=other,
            )
            executed = []
            self._run_loop(executed, server_dir=other)
            self.assertTrue(executed, "切到新目录后没有重载状态（任务被误判为今天已执行）")
        finally:
            shutil.rmtree(other, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
