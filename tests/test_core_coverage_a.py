"""核心模块单文件覆盖率测试（A 组）：install / utils / config / state。

目标：只靠本文件把四个核心模块的语句覆盖率拉到
install ≥70%、utils ≥85%、config ≥85%、state ≥80%。

设计要点：
- 不触网：所有 HTTP / 版本清单 / 下载 / 校验入口全部打桩。
- 不碰真实用户数据：settings.json / theme.json 指向 tempfile 目录。
- 不弹窗：browse_directory 的 Win32 / tkinter 分支用 mock 替换。
- 断言不依赖界面语言：安装流程的 tr_msg 被替换为固定英文模板。
"""

import contextlib
import ctypes
import errno
import json
import operator
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import zipfile
from unittest import mock

from bedrock_server_manager import config, install, state, utils
from bedrock_server_manager.app_context import AppContext
from bedrock_server_manager.constants import SERVER_EXE

#: 仓库同时支持 Linux；Win32 API 专有分支在非 Windows 上跳过（不影响 Windows 覆盖率）
_IS_WINDOWS_PLATFORM = sys.platform == "win32"
win_only = unittest.skipUnless(_IS_WINDOWS_PLATFORM, "仅 Windows 平台可用（Win32 API）")


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------


def _fake_tr(zh, en, *args):
    """把 install.tr_msg 固定成英文模板，避免断言依赖系统语言。"""
    if not args:
        return en
    try:
        return en % args
    except Exception:
        return en


def _write(path, text, encoding="utf-8"):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(text)
    return path


def _make_zip(path, files):
    """用 zipfile 造一个真实 ZIP（不联网）。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


class FakeResponse:
    """最小可用的 urlopen 返回值替身。"""

    def __init__(self, chunks, headers=None, status=200, url="http://example.invalid/file.zip"):
        self._chunks = list(chunks)
        self.headers = dict(headers or {})
        self.status = status
        self._url = url
        self.read_calls = 0

    def read(self, n=-1):
        self.read_calls += 1
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _Base(unittest.TestCase):
    """所有用例的隔离基类：临时目录 + 临时 settings/theme + AppContext 重置。"""

    def setUp(self):
        AppContext.reset()
        self.ctx = AppContext.instance()
        self.tmp = tempfile.mkdtemp(prefix="mbes_core_a_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(AppContext.reset)

        self.orig_settings_file = utils.SETTINGS_FILE
        self.settings_file = os.path.join(self.tmp, "settings.json")
        self.theme_file = os.path.join(self.tmp, "theme.json")
        self._patch(utils, "SETTINGS_FILE", self.settings_file)
        self._patch(utils, "THEME_FILE", self.theme_file)
        self._patch(state, "THEME_FILE", self.theme_file)

        # 缓存类全局状态：置空并在用例结束后还原，避免污染其他文件
        self._reset_global(utils, "_detect_cache", {"result": None, "time": 0})
        self._reset_global(install, "_versions_cache", {"stable": None, "preview": None, "timestamp": 0})
        self._reset_global(config, "_properties_cache", {})

    # -- patch helpers ------------------------------------------------------
    def _patch(self, obj, name, value):
        patcher = mock.patch.object(obj, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def _reset_global(self, obj, name, value):
        original = getattr(obj, name)
        patcher = mock.patch.object(obj, name, value)
        patcher.start()

        def _restore():
            patcher.stop()
            setattr(obj, name, original)

        self.addCleanup(_restore)


# ===========================================================================
# install.py —— 纯函数 / 小工具
# ===========================================================================


class TestInstallHelpers(_Base):
    def test_validate_version(self):
        for good in ("1.21.0.03", "1.21.0", "", "1.21.0-rc1", "1.21.0_beta", "1" * 32):
            ok, err = install.validate_version(good)
            self.assertTrue(ok, f"{good!r} 应通过: {err}")
        for bad in ("../x", "..", "a/b", "1.0;rm", "v1.0", " ", "1" * 40):
            ok, err = install.validate_version(bad)
            self.assertFalse(ok, f"{bad!r} 应被拒绝")
            self.assertTrue(err)

    def test_preserved_and_artifact_names(self):
        self.assertTrue(install._is_preserved_name("worlds"))
        self.assertTrue(install._is_preserved_name("server_console.log"))
        self.assertTrue(install._is_preserved_name("server_console.txt"))
        self.assertFalse(install._is_preserved_name("libCrypto.dll"))
        self.assertTrue(install._is_server_artifact("bedrock_server"))
        self.assertTrue(install._is_server_artifact("bedrock_server.exe"))
        self.assertTrue(install._is_server_artifact("LIBcrypto.DLL"))
        self.assertTrue(install._is_server_artifact("libssl.so.1.1"))
        # 用户备份不能被当成残留
        self.assertFalse(install._is_server_artifact("libCrypto.dll.bak"))
        self.assertFalse(install._is_server_artifact("bedrock_server_backup.zip"))
        self.assertFalse(install._is_server_artifact("my.dll.txt"))
        self.assertFalse(install._is_server_artifact(""))

    def test_format_download_template_and_version_key(self):
        from bedrock_server_manager.constants import PLATFORM_TAG

        url = install._format_download_template("https://x/{platform}/{version}.zip", "1.2.3")
        self.assertEqual(url, f"https://x/{PLATFORM_TAG}/1.2.3.zip")
        self.assertEqual(install._version_key("1.21.130.3"), (1, 21, 130, 3))
        self.assertEqual(install._version_key("1.21"), (1, 21, 0, 0))
        self.assertEqual(install._version_key("1.x.3.4.5"), (1, 0, 3, 4))

    def test_get_settings_console_constants(self):
        self.ctx.settings = {"server_dir": "/x"}
        self.assertIs(install._get_settings(), self.ctx.settings)
        sentinel = object()
        self.ctx.console = sentinel
        self.assertIs(install._get_console(), sentinel)
        consts = install._get_constants()
        self.assertEqual(len(consts), 4)
        self.assertIn(SERVER_EXE, consts)


class TestInstallStateTracker(_Base):
    def test_snapshot_set_and_log(self):
        st = install.InstallState()
        snap = st.snapshot()
        self.assertFalse(snap["busy"])
        self.assertEqual(snap["phase"], "idle")
        self.assertEqual(snap["log"], [])
        st.set(phase="download", percent=42, text="t", done=True, error="e")
        snap = st.snapshot()
        self.assertEqual(snap["phase"], "download")
        self.assertEqual(snap["percent"], 42)
        self.assertEqual(snap["text"], "t")
        self.assertTrue(snap["done"])
        self.assertEqual(snap["error"], "e")
        # 全 None 的调用来覆盖"跳过赋值"分支
        st.set()
        st.log_line("a")
        st.log_line("b")
        self.assertEqual(st.snapshot()["log"], ["a", "b"])

    def test_log_is_capped_at_500(self):
        st = install.InstallState()
        for i in range(520):
            st.log_line(str(i))
        log = st.snapshot()["log"]
        self.assertEqual(len(log), 500)
        self.assertEqual(log[0], "20")
        self.assertEqual(log[-1], "519")


# ===========================================================================
# install.py —— 暂存合并 / 原子替换 / 残留清理
# ===========================================================================


class TestApplyStagedInstall(_Base):
    def setUp(self):
        super().setUp()
        self.target = os.path.join(self.tmp, "server")
        self.staging = os.path.join(self.tmp, "staged")
        os.makedirs(self.target)
        os.makedirs(self.staging)

    def test_fresh_install_and_manifest(self):
        _write(os.path.join(self.staging, SERVER_EXE), "bin")
        _write(os.path.join(self.staging, "server.properties"), "server-name=D\n")
        _write(os.path.join(self.staging, "libCrypto.dll"), "dll")
        skipped = install.apply_staged_install(self.staging, self.target)
        self.assertEqual(skipped, [])
        self.assertTrue(os.path.isfile(os.path.join(self.target, SERVER_EXE)))
        self.assertTrue(os.path.isfile(os.path.join(self.target, "server.properties")))
        manifest = json.loads(open(os.path.join(self.target, install._INSTALL_MANIFEST), encoding="utf-8").read())
        self.assertIn(SERVER_EXE, manifest["files"])

    def test_update_preserves_user_data_and_merges_overlay(self):
        _write(os.path.join(self.target, "server.properties"), "server-name=Keep\n")
        _write(os.path.join(self.target, "allowlist.json"), '[{"name":"Steve"}]')
        _write(os.path.join(self.target, "worlds", "W", "level.dat"), "world")
        _write(os.path.join(self.target, "server_console.log"), "old-log")
        _write(os.path.join(self.target, SERVER_EXE), "old-bin")
        # 覆盖式合并目录：用户额外内容必须保留
        _write(os.path.join(self.target, "resource_packs", "user.txt"), "mine")

        _write(os.path.join(self.staging, SERVER_EXE), "new-bin")
        _write(os.path.join(self.staging, "server.properties"), "server-name=Default\n")
        _write(os.path.join(self.staging, "allowlist.json"), "[]")
        _write(os.path.join(self.staging, "worlds", "W", "level.dat"), "default-world")
        _write(os.path.join(self.staging, "server_console.log"), "new-log")
        _write(os.path.join(self.staging, "resource_packs", "official.txt"), "official")
        _write(os.path.join(self.staging, "definitions", "d.json"), "{}")

        skipped = install.apply_staged_install(self.staging, self.target)

        self.assertIn("server.properties", skipped)
        self.assertIn("worlds", skipped)
        self.assertIn("server_console.log", skipped)
        self.assertEqual(
            open(os.path.join(self.target, "server.properties"), encoding="utf-8").read(), "server-name=Keep\n"
        )
        self.assertEqual(open(os.path.join(self.target, SERVER_EXE), encoding="utf-8").read(), "new-bin")
        self.assertTrue(os.path.isfile(os.path.join(self.target, "worlds", "W", "level.dat")))
        # resource_packs 覆盖式合并：两边都在
        self.assertTrue(os.path.isfile(os.path.join(self.target, "resource_packs", "user.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.target, "resource_packs", "official.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.target, "definitions", "d.json")))

    def test_directory_entry_replaces_target_dir(self):
        _write(os.path.join(self.target, "misc", "old.txt"), "old")
        _write(os.path.join(self.staging, SERVER_EXE), "bin")
        _write(os.path.join(self.staging, "misc", "new.txt"), "new")
        install.apply_staged_install(self.staging, self.target)
        self.assertTrue(os.path.isfile(os.path.join(self.target, "misc", "new.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.target, "misc", "old.txt")))

    def test_incomplete_staging_aborts_without_touching_target(self):
        _write(os.path.join(self.target, SERVER_EXE), "original")
        _write(os.path.join(self.staging, "server.properties"), "x=1\n")
        with self.assertRaises(RuntimeError):
            install.apply_staged_install(self.staging, self.target)
        self.assertEqual(open(os.path.join(self.target, SERVER_EXE), encoding="utf-8").read(), "original")

    def test_load_manifest_variants(self):
        path = os.path.join(self.target, install._INSTALL_MANIFEST)
        _write(path, json.dumps({"version": 1, "files": ["a.dll", 2]}))
        self.assertEqual(install._load_install_manifest(self.target), {"a.dll", "2"})
        _write(path, json.dumps(["x.dll"]))
        self.assertEqual(install._load_install_manifest(self.target), {"x.dll"})
        _write(path, "{not json")
        self.assertEqual(install._load_install_manifest(self.target), set())
        _write(path, json.dumps({"nope": 1}))
        self.assertEqual(install._load_install_manifest(self.target), set())
        os.remove(path)
        self.assertEqual(install._load_install_manifest(self.target), set())

    def test_save_manifest_failure_is_swallowed(self):
        with mock.patch.object(install, "atomic_write_json", side_effect=OSError("boom")):
            install._save_install_manifest(self.target, ["a"])
        self.assertFalse(os.path.exists(os.path.join(self.target, install._INSTALL_MANIFEST)))

    def test_cleanup_staging_idempotent(self):
        os.makedirs(os.path.join(self.target, install.STAGING_DIRNAME, "extracted"))
        install.cleanup_staging(self.target)
        self.assertFalse(os.path.exists(os.path.join(self.target, install.STAGING_DIRNAME)))
        install.cleanup_staging(self.target)

    def test_extract_zip_cancel_and_progress(self):
        files = {f"f{i}.txt": "x" for i in range(7)}
        zip_path = _make_zip(os.path.join(self.tmp, "pkg.zip"), files)
        dest = os.path.join(self.tmp, "extract1")
        seen = []
        install.extract_zip(zip_path, dest, progress_cb=lambda i, total: seen.append((i, total)))
        self.assertEqual(len(os.listdir(dest)), 7)
        self.assertEqual(seen[-1], (7, 7))

        flag = threading.Event()
        flag.set()
        with self.assertRaises(InterruptedError):
            install.extract_zip(zip_path, os.path.join(self.tmp, "extract2"), cancel_flag=flag)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "extract2", "f0.txt")))

    def test_extract_zip_rejects_path_traversal(self):
        evil = _make_zip(os.path.join(self.tmp, "evil.zip"), {"../evil.txt": "x"})
        with self.assertRaises(RuntimeError):
            install.extract_zip(evil, os.path.join(self.tmp, "extract3"))


class TestPruneStaleFiles(_Base):
    def setUp(self):
        super().setUp()
        self.target = os.path.join(self.tmp, "server")
        self.staging = os.path.join(self.tmp, "staged")
        os.makedirs(self.target)
        os.makedirs(self.staging)

    def test_removes_only_manifest_owned_artifacts(self):
        _write(os.path.join(self.staging, SERVER_EXE), "bin")
        _write(os.path.join(self.target, "oldlib.dll"), "old")
        _write(os.path.join(self.target, "user_note.txt"), "mine")
        _write(os.path.join(self.target, "my.dll.bak"), "backup")
        _write(os.path.join(self.target, "bedrock_server_backup.zip"), "zip")
        os.makedirs(os.path.join(self.target, "userdir"))
        _write(os.path.join(self.target, "userdir", "keep.dll"), "keep")

        removed = install._prune_stale_files(
            self.staging,
            self.target,
            {"oldlib.dll", "user_note.txt", "my.dll.bak", "bedrock_server_backup.zip", "userdir"},
        )
        self.assertEqual(removed, ["oldlib.dll"])
        self.assertFalse(os.path.exists(os.path.join(self.target, "oldlib.dll")))
        for kept in ("user_note.txt", "my.dll.bak", "bedrock_server_backup.zip", "userdir"):
            self.assertTrue(os.path.exists(os.path.join(self.target, kept)), kept)

    def test_never_removes_names_still_in_package(self):
        _write(os.path.join(self.staging, SERVER_EXE), "bin")
        _write(os.path.join(self.staging, "shared.dll"), "new")
        _write(os.path.join(self.target, "shared.dll"), "old")
        _write(os.path.join(self.target, "server.properties"), "x")
        removed = install._prune_stale_files(self.staging, self.target, {"shared.dll", "server.properties"})
        self.assertEqual(removed, [])

    def test_cleans_own_residue(self):
        _write(os.path.join(self.staging, SERVER_EXE), "bin")
        os.makedirs(os.path.join(self.target, "somedir.install_new"))
        os.makedirs(os.path.join(self.target, "somedir.install_old"))
        os.makedirs(os.path.join(self.target, "somedir"))
        _write(os.path.join(self.target, "orphan.install_old", "x"), "x")
        _write(os.path.join(self.target, "bedrock_server.exe.install_abcd.tmp"), "t")

        removed = install._prune_stale_files(self.staging, self.target, set())
        self.assertFalse(os.path.exists(os.path.join(self.target, "somedir.install_new")))
        self.assertFalse(os.path.exists(os.path.join(self.target, "somedir.install_old")))
        self.assertFalse(os.path.exists(os.path.join(self.target, "bedrock_server.exe.install_abcd.tmp")))
        # 目标缺失的 .install_old 必须保留（可能是唯一副本）
        self.assertTrue(os.path.isdir(os.path.join(self.target, "orphan.install_old")))
        self.assertEqual(removed, [])

    def test_preserved_prefixes_are_never_removed(self):
        _write(os.path.join(self.staging, SERVER_EXE), "bin")
        _write(os.path.join(self.target, "server_console.2.log"), "rotated")
        removed = install._prune_stale_files(self.staging, self.target, {"server_console.2.log"})
        self.assertEqual(removed, [])
        self.assertTrue(os.path.isfile(os.path.join(self.target, "server_console.2.log")))

    def test_remove_error_is_logged_and_skipped(self):
        _write(os.path.join(self.staging, SERVER_EXE), "bin")
        _write(os.path.join(self.target, "old.dll"), "x")
        with mock.patch("os.remove", side_effect=OSError("locked")):
            removed = install._prune_stale_files(self.staging, self.target, {"old.dll"})
        self.assertEqual(removed, [])

    def test_outer_exception_returns_empty_list(self):
        _write(os.path.join(self.staging, SERVER_EXE), "bin")
        _write(os.path.join(self.target, "old.dll"), "x")
        with mock.patch("os.listdir", side_effect=OSError("boom")):
            self.assertEqual(install._prune_stale_files(self.staging, self.target, {"old.dll"}), [])


class TestAtomicReplace(_Base):
    def test_replace_file_success(self):
        src = _write(os.path.join(self.tmp, "src.bin"), "new")
        dst = _write(os.path.join(self.tmp, "out", "dst.bin"), "old")
        install._atomic_replace_file(src, dst)
        self.assertEqual(open(dst, encoding="utf-8").read(), "new")
        self.assertEqual([n for n in os.listdir(os.path.dirname(dst)) if n.endswith(".tmp")], [])

    def test_replace_file_retries_permission_error(self):
        src = _write(os.path.join(self.tmp, "src.bin"), "new")
        dst = _write(os.path.join(self.tmp, "dst.bin"), "old")
        real_replace = os.replace
        calls = []

        def _flaky(a, b):
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError("busy")
            return real_replace(a, b)

        with mock.patch("os.replace", _flaky), mock.patch("time.sleep"):
            install._atomic_replace_file(src, dst)
        self.assertEqual(len(calls), 3)
        self.assertEqual(open(dst, encoding="utf-8").read(), "new")

    def test_replace_file_gives_up(self):
        src = _write(os.path.join(self.tmp, "src.bin"), "new")
        dst = os.path.join(self.tmp, "dst.bin")
        with mock.patch("os.replace", side_effect=PermissionError("busy")), mock.patch("time.sleep"):
            with self.assertRaises(PermissionError):
                install._atomic_replace_file(src, dst)
        # 临时文件被清理，目标未被破坏
        self.assertEqual([n for n in os.listdir(self.tmp) if n.endswith(".tmp")], [])

    def test_replace_dir_success(self):
        src = os.path.join(self.tmp, "srcdir")
        _write(os.path.join(src, "a.txt"), "new")
        dst = os.path.join(self.tmp, "dstdir")
        _write(os.path.join(dst, "old.txt"), "old")
        install._atomic_replace_dir(src, dst)
        self.assertTrue(os.path.isfile(os.path.join(dst, "a.txt")))
        self.assertFalse(os.path.exists(os.path.join(dst, "old.txt")))
        self.assertFalse(os.path.exists(dst + ".install_new"))
        self.assertFalse(os.path.exists(dst + ".install_old"))

    def test_replace_dir_copy_failure_keeps_original(self):
        src = os.path.join(self.tmp, "srcdir")
        _write(os.path.join(src, "a.txt"), "new")
        dst = os.path.join(self.tmp, "dstdir")
        _write(os.path.join(dst, "old.txt"), "old")
        with mock.patch.object(install.shutil, "copytree", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                install._atomic_replace_dir(src, dst)
        self.assertTrue(os.path.isfile(os.path.join(dst, "old.txt")))
        self.assertFalse(os.path.exists(dst + ".install_new"))

    def test_replace_dir_cleans_previous_residue(self):
        src = os.path.join(self.tmp, "srcdir")
        _write(os.path.join(src, "a.txt"), "new")
        dst = os.path.join(self.tmp, "dstdir")
        _write(os.path.join(dst, "old.txt"), "old")
        # 上一次合并崩溃留下的临时目录
        _write(os.path.join(dst + ".install_new", "stale.txt"), "stale")
        _write(os.path.join(dst + ".install_old", "stale.txt"), "stale")
        install._atomic_replace_dir(src, dst)
        self.assertTrue(os.path.isfile(os.path.join(dst, "a.txt")))
        self.assertFalse(os.path.exists(dst + ".install_new"))
        self.assertFalse(os.path.exists(dst + ".install_old"))

    def test_replace_dir_swap_failure_restores_original(self):
        src = os.path.join(self.tmp, "srcdir")
        _write(os.path.join(src, "a.txt"), "new")
        dst = os.path.join(self.tmp, "dstdir")
        _write(os.path.join(dst, "old.txt"), "old")
        real_replace = os.replace

        def _flaky(a, b):
            if str(a).endswith(".install_new"):
                raise OSError("cannot swap")
            return real_replace(a, b)

        with mock.patch("os.replace", _flaky), self.assertRaises(OSError):
            install._atomic_replace_dir(src, dst)
        self.assertTrue(os.path.isfile(os.path.join(dst, "old.txt")))


# ===========================================================================
# install.py —— 下载 / HTTP / 版本查询
# ===========================================================================


class TestDownloadFile(_Base):
    def setUp(self):
        super().setUp()
        self.dest = os.path.join(self.tmp, "out", "bds.zip")
        os.makedirs(os.path.dirname(self.dest), exist_ok=True)

    def test_fresh_download_with_progress(self):
        resp = FakeResponse([b"PK\x03\x04abc", b"def"], headers={"Content-Length": "10"})
        seen = []
        with mock.patch("urllib.request.urlopen", return_value=resp):
            done = install.download_file("http://x/y.zip", self.dest, progress_cb=lambda d, t: seen.append((d, t)))
        self.assertEqual(done, 10)
        self.assertEqual(open(self.dest, "rb").read(), b"PK\x03\x04abcdef")
        self.assertTrue(seen)
        self.assertEqual(seen[-1], (10, 10))
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_resume_with_valid_part_file(self):
        part = self.dest + ".part"
        _write(part, "x" * 10)
        with open(part, "wb") as f:
            f.write(b"PK\x03\x04" + b"a" * 6)
        resp = FakeResponse([b"bbbb"], headers={"Content-Length": "4", "Accept-Ranges": "bytes"})
        with mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
            done = install.download_file("http://x/y.zip", self.dest)
        self.assertEqual(done, 14)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 30)
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_header("Range"), "bytes=10-")

    def test_corrupt_part_file_restarts_from_zero(self):
        part = self.dest + ".part"
        _write(part, "NOTAZIP" * 3)
        resp = FakeResponse([b"PK\x03\x04zz"], headers={"Content-Length": "6"})
        with mock.patch("urllib.request.urlopen", return_value=resp):
            install.download_file("http://x/y.zip", self.dest)
        self.assertEqual(open(self.dest, "rb").read(), b"PK\x03\x04zz")

    def test_empty_part_file_is_ignored(self):
        _write(self.dest + ".part", "")
        resp = FakeResponse([b"PK\x03\x04"], headers={"Content-Length": "4"})
        with mock.patch("urllib.request.urlopen", return_value=resp):
            install.download_file("http://x/y.zip", self.dest)
        self.assertEqual(open(self.dest, "rb").read(), b"PK\x03\x04")

    def test_part_file_stat_error_resets(self):
        _write(self.dest + ".part", "PK\x03\x04data")
        real_getsize = os.path.getsize

        def _getsize(path):
            if str(path).endswith(".part"):
                raise OSError("stat failed")
            return real_getsize(path)

        resp = FakeResponse([b"PK\x03\x04nn"], headers={"Content-Length": "6"})
        with mock.patch("os.path.getsize", _getsize), mock.patch("urllib.request.urlopen", return_value=resp):
            install.download_file("http://x/y.zip", self.dest)
        self.assertEqual(open(self.dest, "rb").read(), b"PK\x03\x04nn")

    def test_part_file_read_error_falls_back_to_full_download(self):
        with open(self.dest + ".part", "wb") as f:
            f.write(b"PK\x03\x04partial")
        real_open = open

        def _open(path, mode="r", *a, **k):
            if str(path).endswith(".part") and "r" in mode and "b" in mode:
                raise OSError("cannot read part file")
            return real_open(path, mode, *a, **k)

        resp = FakeResponse([b"PK\x03\x04nn"], headers={"Content-Length": "6"})
        with mock.patch("builtins.open", _open), mock.patch("urllib.request.urlopen", return_value=resp):
            install.download_file("http://x/y.zip", self.dest)
        self.assertEqual(open(self.dest, "rb").read(), b"PK\x03\x04nn")

    def test_content_range_defines_total(self):
        with open(self.dest + ".part", "wb") as f:
            f.write(b"PK\x03\x04" + b"a" * 6)
        headers = {"Content-Length": "90", "Content-Range": "bytes 10-99/100"}
        resp = FakeResponse([b"b" * 90], headers=headers, status=206)
        seen = []
        with mock.patch("urllib.request.urlopen", return_value=resp):
            done = install.download_file("http://x/y.zip", self.dest, progress_cb=lambda d, t: seen.append((d, t)))
        self.assertEqual(done, 100)
        self.assertEqual(seen[-1], (100, 100))

    def test_unparsable_content_range_falls_back(self):
        with open(self.dest + ".part", "wb") as f:
            f.write(b"PK\x03\x04" + b"a" * 6)
        headers = {"Content-Length": "5", "Content-Range": "bytes ?"}
        resp = FakeResponse([b"ccccc"], headers=headers, status=206)
        with mock.patch("urllib.request.urlopen", return_value=resp):
            done = install.download_file("http://x/y.zip", self.dest)
        self.assertEqual(done, 15)

    def test_server_without_range_support_restarts(self):
        with open(self.dest + ".part", "wb") as f:
            f.write(b"PK\x03\x04" + b"a" * 6)
        resp = FakeResponse([b"PK\x03\x04", b"zz"], headers={"Content-Length": "6"})
        with mock.patch("urllib.request.urlopen", return_value=resp):
            install.download_file("http://x/y.zip", self.dest)
        self.assertEqual(open(self.dest, "rb").read(), b"PK\x03\x04zz")

    def test_cancel_keeps_part_file(self):
        resp = FakeResponse([b"PK\x03\x04", b"more"])
        flag = threading.Event()
        flag.set()
        with mock.patch("urllib.request.urlopen", return_value=resp), self.assertRaises(InterruptedError):
            install.download_file("http://x/y.zip", self.dest, cancel_flag=flag)
        self.assertTrue(os.path.exists(self.dest + ".part"))

    def test_read_error_removes_part_file(self):
        class _Boom(FakeResponse):
            def read(self, n=-1):
                raise OSError("connection reset")

        with mock.patch("urllib.request.urlopen", return_value=_Boom([], headers={"Content-Length": "10"})):
            with self.assertRaises(OSError):
                install.download_file("http://x/y.zip", self.dest)
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_redirect_to_forbidden_target_is_rejected(self):
        resp = FakeResponse([b"PK\x03\x04"], headers={"Content-Length": "4"}, url="http://169.254.169.254/x")
        with (
            mock.patch("urllib.request.urlopen", return_value=resp),
            mock.patch("bedrock_server_manager.security.validate_effective_url", return_value=(False, "内网地址")),
        ):
            with self.assertRaises(RuntimeError):
                install.download_file("http://x/y.zip", self.dest)
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_redirect_to_public_target_is_accepted(self):
        resp = FakeResponse([b"PK\x03\x04s"], headers={"Content-Length": "5"}, url="http://cdn.example/y.zip")
        with (
            mock.patch("urllib.request.urlopen", return_value=resp),
            mock.patch("bedrock_server_manager.security.validate_effective_url", return_value=(True, "")),
        ):
            done = install.download_file("http://x/y.zip", self.dest)
        self.assertEqual(done, 5)

    def test_ignore_ssl_branch(self):
        resp = FakeResponse([b"PK\x03\x04"], headers={"Content-Length": "4"})
        with mock.patch("urllib.request.urlopen", return_value=resp):
            install.download_file("http://x/y.zip", self.dest, ignore_ssl=True)
        self.assertTrue(os.path.isfile(self.dest))


class TestHttpGetAndLatestInfo(_Base):
    def test_http_get_returns_bytes(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse([b'{"a": 1}'])):
            self.assertEqual(install._http_get("http://x"), b'{"a": 1}')

    def test_http_get_ignore_ssl(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse([b"ok"])):
            self.assertEqual(install._http_get("http://x", ignore_ssl=True), b"ok")

    def test_http_get_error_propagates(self):
        err = urllib.error.HTTPError("http://x", 404, "nf", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err), self.assertRaises(urllib.error.HTTPError):
            install._http_get("http://x")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(urllib.error.URLError):
                install._http_get("http://x")

    def test_http_get_unexpected_error_propagates(self):
        with mock.patch("urllib.request.urlopen", side_effect=ValueError("bad url")):
            with self.assertRaises(ValueError):
                install._http_get("http://x")

    def test_latest_server_info_success(self):
        payload = {
            "result": {
                "links": [
                    {"downloadType": "serverBedrockLinux", "downloadUrl": "https://x/bedrock-server-1.20.0.1.zip"},
                    {"downloadType": "serverBedrockWindows", "downloadUrl": "https://x/bedrock-server-1.21.0.03.zip"},
                    {"downloadType": "serverBedrockWindows", "downloadUrl": "https://x/not-a-zip"},
                ]
            }
        }
        with mock.patch.object(install, "_http_get", return_value=json.dumps(payload).encode()):
            info = install.get_latest_server_info(ignore_ssl=True)
        self.assertEqual(info["version"], "1.21.0.03")
        self.assertEqual(info["win_url"], "https://x/bedrock-server-1.21.0.03.zip")

    def test_latest_server_info_without_matching_link(self):
        payload = {"result": {"links": [{"downloadType": "other", "downloadUrl": "https://x/a.zip"}]}}
        with mock.patch.object(install, "_http_get", return_value=json.dumps(payload).encode()):
            with self.assertRaises(RuntimeError):
                install.get_latest_server_info()


class TestVersionQueries(_Base):
    def test_fetch_versions_from_manifest(self):
        data = {
            "windows": {
                "versions": ["1.20.0.1", "1.21.10.1", "1.9.0.1"],
                "preview_versions": ["1.22.0.1", "1.21.50.1"],
            }
        }
        with (
            mock.patch.object(install, "platform_key", return_value="windows"),
            mock.patch.object(install, "fetch_manifest", return_value=(data, "https://src")),
        ):
            stable, preview, source = install.fetch_versions_from_manifest(include_preview=True)
        self.assertEqual(stable, ["1.21.10.1", "1.20.0.1", "1.9.0.1"])
        self.assertEqual(preview, ["1.22.0.1", "1.21.50.1"])
        self.assertEqual(source, "https://src")

    def test_fetch_versions_from_manifest_empty(self):
        with mock.patch.object(install, "fetch_manifest", return_value=({}, "")):
            self.assertEqual(install.fetch_versions_from_manifest(), ([], [], ""))

    def test_fetch_versions_missing_platform_key(self):
        with (
            mock.patch.object(install, "platform_key", return_value="windows"),
            mock.patch.object(install, "fetch_manifest", return_value=({"linux": {}}, "u")),
        ):
            self.assertEqual(install.fetch_versions_from_manifest(), ([], [], "u"))

    def test_get_bedrock_versions_merges_manifest_and_api(self):
        links = {
            "result": {
                "links": [
                    {"downloadType": "serverBedrockWindows", "downloadUrl": "https://x/bedrock-server-1.21.20.1.zip"},
                    {
                        "downloadType": "serverBedrockWindowsPreview",
                        "downloadUrl": "https://x/bedrock-server-1.22.0.1.zip",
                    },
                    {"downloadType": "serverBedrockLinux", "downloadUrl": "https://x/bedrock-server-1.30.0.1.zip"},
                    {"downloadType": "serverBedrockLinux", "downloadUrl": "https://x/nope.zip"},
                ]
            }
        }
        with (
            mock.patch.object(install, "platform_key", return_value="windows"),
            mock.patch.object(
                install, "fetch_versions_from_manifest", return_value=(["1.20.0.1"], ["1.22.0.1"], "src")
            ),
            mock.patch.object(install, "_http_get", return_value=json.dumps(links).encode()),
        ):
            stable_only = install.get_bedrock_versions()
            with_preview = install.get_bedrock_versions(include_preview=True)
        self.assertEqual(stable_only, ["1.21.20.1", "1.20.0.1"])
        self.assertIn("1.22.0.1", with_preview)
        self.assertNotIn("1.30.0.1", with_preview)

    def test_get_bedrock_versions_api_failure_is_tolerated(self):
        with (
            mock.patch.object(install, "platform_key", return_value="windows"),
            mock.patch.object(install, "fetch_versions_from_manifest", return_value=(["1.20.0.1"], [], "src")),
            mock.patch.object(install, "_http_get", side_effect=OSError("offline")),
        ):
            self.assertEqual(install.get_bedrock_versions(), ["1.20.0.1"])

    def test_get_bedrock_versions_builtin_fallback(self):
        builtin = {"windows": {"versions": ["1.19.0.1", "1.18.0.1"], "preview_versions": ["1.19.50.1"]}}
        with (
            mock.patch.object(install, "platform_key", return_value="windows"),
            mock.patch.object(install, "fetch_versions_from_manifest", return_value=([], [], "")),
            mock.patch.object(install, "_http_get", return_value=b"{}"),
            mock.patch.object(install, "builtin_manifest", return_value=builtin),
        ):
            self.assertEqual(install.get_bedrock_versions(), ["1.19.0.1", "1.18.0.1"])
            with_preview = install.get_bedrock_versions(include_preview=True)
        self.assertIn("1.19.50.1", with_preview)

    def test_cached_versions_miss_then_hit(self):
        with mock.patch.object(install, "get_bedrock_versions", return_value=["1.21.0.1"]) as fetch:
            versions, cached = install.get_cached_versions()
            self.assertEqual(versions, ["1.21.0.1"])
            self.assertFalse(cached)
            versions2, cached2 = install.get_cached_versions()
        self.assertEqual(versions2, ["1.21.0.1"])
        self.assertTrue(cached2)
        self.assertEqual(fetch.call_count, 1)

    def test_cached_versions_exception_uses_previous_cache(self):
        install._versions_cache.update({"stable": ["1.20.0.1"], "preview": None, "timestamp": time.time() - 10_000})
        with mock.patch.object(install, "get_bedrock_versions", side_effect=RuntimeError("offline")):
            versions, cached = install.get_cached_versions()
        self.assertEqual(versions, ["1.20.0.1"])
        self.assertTrue(cached)

    def test_cached_versions_exception_without_cache_uses_fallback(self):
        with mock.patch.object(install, "get_bedrock_versions", side_effect=RuntimeError("offline")):
            versions, cached = install.get_cached_versions(include_preview=True)
        self.assertEqual(versions, install.FALLBACK_VERSIONS)
        self.assertFalse(cached)


# ===========================================================================
# install.py —— 版本检测 / 目录选择
# ===========================================================================


class TestDetectServerVersion(_Base):
    def setUp(self):
        super().setUp()
        self.server = os.path.join(self.tmp, "server")
        os.makedirs(self.server)

    def test_missing_or_invalid_dir(self):
        self.assertEqual(install.detect_server_version(""), "")
        self.assertEqual(install.detect_server_version(os.path.join(self.tmp, "nope")), "")

    def test_settings_map_hit(self):
        key = os.path.normcase(os.path.abspath(self.server))
        self.ctx.settings["server_versions"] = {key: "1.21.0.04"}
        self.assertEqual(install.detect_server_version(self.server), "1.21.0.04")

    def test_version_file_detection(self):
        _write(os.path.join(self.server, "release_notes.txt"), "Bedrock Server 1.21.0.03 released")
        self.assertEqual(install.detect_server_version(self.server), "1.21.0.03")
        key = os.path.normcase(os.path.abspath(self.server))
        self.assertEqual(self.ctx.settings["server_versions"][key], "1.21.0.03")

    def test_version_file_without_version_and_unreadable_file(self):
        _write(os.path.join(self.server, "version.txt"), "no version here")
        self.assertEqual(install.detect_server_version(self.server), "")
        # 读文件抛异常时安全返回空串
        _write(os.path.join(self.server, "VERSION"), "1.2.3.4")
        with mock.patch("builtins.open", side_effect=OSError("denied")):
            self.assertEqual(install.detect_server_version(self.server), "")

    def test_executable_missing_on_windows_branch(self):
        # 目录里没有 exe：跳过 Win32 版本资源读取，落到文件检测
        self.assertEqual(install.detect_server_version(self.server), "")

    def test_version_file_repairs_non_dict_versions_map(self):
        self.ctx.settings["server_versions"] = "broken"
        _write(os.path.join(self.server, "VERSION"), "1.21.0.05")
        self.assertEqual(install.detect_server_version(self.server), "1.21.0.05")
        self.assertIsInstance(self.ctx.settings["server_versions"], dict)

    @win_only
    def test_pe_version_resource_repairs_non_dict_map(self):
        _write(os.path.join(self.server, SERVER_EXE), "fake-pe")
        self.ctx.settings["server_versions"] = "broken"

        class _SubUInt(ctypes.c_uint):
            def __sub__(self, other):
                return int(self.value) - other

        def _ver_query(buf, block, addr, length):
            if block.startswith("\\VarFileInfo"):
                length._obj.value = 0
            else:
                length._obj.value = 11
                addr._obj.value = 4096
            return True

        fake_version = mock.MagicMock()
        fake_version.GetFileVersionInfoSizeW.return_value = 1024
        fake_version.GetFileVersionInfoW.return_value = True
        fake_version.VerQueryValueW.side_effect = _ver_query
        fake_windll = mock.MagicMock()
        fake_windll.version = fake_version

        with (
            mock.patch.object(ctypes, "windll", fake_windll, create=True),
            mock.patch.object(ctypes, "wstring_at", return_value="1.21.0.03"),
            mock.patch.object(ctypes, "cast", lambda *a, **k: mock.MagicMock()),
            mock.patch.object(ctypes, "c_uint", _SubUInt),
        ):
            self.assertEqual(install.detect_server_version(self.server), "1.21.0.03")
        key = os.path.normcase(os.path.abspath(self.server))
        self.assertEqual(self.ctx.settings["server_versions"][key], "1.21.0.03")

    @win_only
    def test_pe_version_field_exceptions_are_swallowed(self):
        _write(os.path.join(self.server, SERVER_EXE), "fake-pe")

        def _ver_query(buf, block, addr, length):
            if block.startswith("\\VarFileInfo"):
                length._obj.value = 0
                return True
            length._obj.value = 11
            addr._obj.value = 4096
            return True

        fake_version = mock.MagicMock()
        fake_version.GetFileVersionInfoSizeW.return_value = 1024
        fake_version.GetFileVersionInfoW.return_value = True
        fake_version.VerQueryValueW.side_effect = _ver_query
        fake_windll = mock.MagicMock()
        fake_windll.version = fake_version

        with (
            mock.patch.object(ctypes, "windll", fake_windll, create=True),
            mock.patch.object(ctypes, "wstring_at", side_effect=TypeError("wrong type")),
            mock.patch.object(ctypes, "cast", lambda *a, **k: mock.MagicMock()),
        ):
            self.assertEqual(install.detect_server_version(self.server), "")

    @win_only
    def test_pe_version_resource_mocked(self):
        """覆盖 Win32 版本资源读取分支（用 mock 提供 API 返回）。"""
        _write(os.path.join(self.server, SERVER_EXE), "fake-pe")

        class _SubUInt(ctypes.c_uint):
            def __sub__(self, other):
                return int(self.value) - other

        class _Lang:
            def __init__(self):
                self.wLanguage = 0x0409
                self.wCodePage = 0x04B0

        class _LangPtr:
            def __getitem__(self, i):
                return _Lang()

        def _ver_query(buf, block, addr, length):
            if block.startswith("\\VarFileInfo"):
                length._obj.value = 4
            else:
                length._obj.value = 11
                addr._obj.value = 4096
            return True

        fake_version = mock.MagicMock()
        fake_version.GetFileVersionInfoSizeW.return_value = 1024
        fake_version.GetFileVersionInfoW.return_value = True
        fake_version.VerQueryValueW.side_effect = _ver_query
        fake_windll = mock.MagicMock()
        fake_windll.version = fake_version

        with (
            mock.patch.object(ctypes, "windll", fake_windll, create=True),
            mock.patch.object(ctypes, "wstring_at", return_value="1.21.0.03", create=True),
            mock.patch.object(ctypes, "cast", lambda *a, **k: _LangPtr()),
            mock.patch.object(ctypes, "c_uint", _SubUInt),
        ):
            self.assertEqual(install.detect_server_version(self.server), "1.21.0.03")
        key = os.path.normcase(os.path.abspath(self.server))
        self.assertEqual(self.ctx.settings["server_versions"][key], "1.21.0.03")

    @win_only
    def test_pe_version_resource_real_types(self):
        """记录真实行为：ctypes.wstring_at(c_uint, c_uint - 1) 会抛 TypeError。

        生产代码此处被内层 except 吞掉，所以 Win32 版本资源探测实际永远拿不到版本号
        （见最终报告的 bug 说明）。本用例锁定"不崩溃 + 返回空串"的可观测行为。
        """
        _write(os.path.join(self.server, SERVER_EXE), "fake-pe")

        def _ver_query(buf, block, addr, length):
            return bool(block.startswith("\\VarFileInfo"))

        fake_version = mock.MagicMock()
        fake_version.GetFileVersionInfoSizeW.return_value = 1024
        fake_version.GetFileVersionInfoW.return_value = True
        fake_version.VerQueryValueW.side_effect = _ver_query
        fake_windll = mock.MagicMock()
        fake_windll.version = fake_version

        with (
            mock.patch.object(ctypes, "windll", fake_windll, create=True),
            mock.patch.object(ctypes, "cast", side_effect=TypeError("argument 1: wrong type")),
        ):
            self.assertEqual(install.detect_server_version(self.server), "")

    @win_only
    def test_pe_api_exception_is_swallowed(self):
        _write(os.path.join(self.server, SERVER_EXE), "fake-pe")

        fake_windll = mock.MagicMock()
        fake_windll.version.GetFileVersionInfoSizeW.side_effect = OSError("no version dll")
        with mock.patch.object(ctypes, "windll", fake_windll, create=True):
            self.assertEqual(install.detect_server_version(self.server), "")


class TestSelectServerDir(_Base):
    def setUp(self):
        super().setUp()
        self.server = os.path.join(self.tmp, "server")
        os.makedirs(self.server)
        _write(os.path.join(self.server, SERVER_EXE), "bin")
        self._patch(install, "detect_servers", mock.MagicMock(return_value=[]))

    def test_success_sets_settings_and_history(self):
        self._patch(install, "detect_server_version", mock.MagicMock(return_value="1.21.0.03"))
        self._patch(install, "server_running", mock.MagicMock(return_value=False))
        ok, err, info = install.select_server_dir(self.server)
        self.assertTrue(ok, err)
        self.assertEqual(info["dir"], self.server)
        self.assertEqual(info["installed_version"], "1.21.0.03")
        self.assertEqual(self.ctx.settings["server_dir"], self.server)
        self.assertEqual(self.ctx.settings["server_dir_history"][0], self.server)
        self.assertTrue(os.path.isfile(self.settings_file))

    def test_detected_version_empty_keeps_old_value(self):
        self.ctx.settings["installed_version"] = "1.20.0.1"
        self._patch(install, "detect_server_version", mock.MagicMock(return_value=""))
        self._patch(install, "server_running", mock.MagicMock(return_value=False))
        ok, _err, info = install.select_server_dir(self.server)
        self.assertTrue(ok)
        self.assertEqual(info["installed_version"], "1.20.0.1")

    def test_rejects_empty_and_missing_dir(self):
        self.assertFalse(install.select_server_dir("")[0])
        self.assertFalse(install.select_server_dir(None)[0])
        self.assertFalse(install.select_server_dir(os.path.join(self.tmp, "nope"))[0])

    def test_rejects_dir_without_executable(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        ok, err, info = install.select_server_dir(empty)
        self.assertFalse(ok)
        self.assertIn(SERVER_EXE, err)
        self.assertIsNone(info)

    def test_running_server_on_other_dir_is_stopped(self):
        proc = mock.MagicMock()
        proc.server_dir = os.path.join(self.tmp, "other")
        self._patch(install, "server_running", mock.MagicMock(return_value=True))
        self._patch(install, "get_server_proc", mock.MagicMock(return_value=proc))
        self._patch(install, "detect_server_version", mock.MagicMock(return_value=""))
        ok, _err, _info = install.select_server_dir(self.server)
        self.assertTrue(ok)
        proc.stop.assert_called_once()

    def test_running_server_on_same_dir_is_not_stopped(self):
        proc = mock.MagicMock()
        proc.server_dir = self.server
        self._patch(install, "server_running", mock.MagicMock(return_value=True))
        self._patch(install, "get_server_proc", mock.MagicMock(return_value=proc))
        self._patch(install, "detect_server_version", mock.MagicMock(return_value=""))
        ok, _err, _info = install.select_server_dir(self.server)
        self.assertTrue(ok)
        proc.stop.assert_not_called()

    def test_stop_failure_does_not_block_switch(self):
        proc = mock.MagicMock()
        proc.server_dir = os.path.join(self.tmp, "other")
        proc.stop.side_effect = OSError("cannot stop")
        self._patch(install, "server_running", mock.MagicMock(return_value=True))
        self._patch(install, "get_server_proc", mock.MagicMock(return_value=proc))
        self._patch(install, "detect_server_version", mock.MagicMock(return_value=""))
        ok, _err, _info = install.select_server_dir(self.server)
        self.assertTrue(ok)
        self.assertEqual(self.ctx.settings["server_dir"], self.server)

    def test_stop_and_logging_failure_still_switches(self):
        """停止服务器失败、连日志都写不了时，目录切换仍然要继续。"""
        proc = mock.MagicMock()
        proc.server_dir = os.path.join(self.tmp, "other")
        proc.stop.side_effect = OSError("cannot stop")
        self._patch(install, "server_running", mock.MagicMock(return_value=True))
        self._patch(install, "get_server_proc", mock.MagicMock(return_value=proc))
        self._patch(install, "detect_server_version", mock.MagicMock(return_value=""))
        with mock.patch("bedrock_server_manager.app_logger.get_app_logger", side_effect=RuntimeError("no logger")):
            ok, _err, _info = install.select_server_dir(self.server)
        self.assertTrue(ok)
        self.assertEqual(self.ctx.settings["server_dir"], self.server)


# ===========================================================================
# install.py —— do_install 主流程
# ===========================================================================


class TestDoInstall(_Base):
    def setUp(self):
        super().setUp()
        self.target = os.path.join(self.tmp, "server")
        self.ctx.install_state = install.InstallState()
        self.ctx.settings = {
            "server_dir": "",
            "ignore_ssl": False,
            "installed_version": "",
            "server_dir_history": [],
        }

    # -- helpers ------------------------------------------------------------
    def _payload(self, **over):
        base = {
            SERVER_EXE: "new-binary",
            "server.properties": "server-name=Default\n",
            "libCrypto.dll": "new-dll",
            "release-notes.txt": "1.21.0.03",
            "definitions/defs.json": "{}",
        }
        base.update(over)
        return {k: v for k, v in base.items() if v is not None}

    def _download_writing(self, payload):
        def _download(url, dest, progress_cb=None, cancel_flag=None, ignore_ssl=False):
            _make_zip(dest, payload)
            if progress_cb:
                progress_cb(1, 2)
                progress_cb(2, 2)
                progress_cb(3, 0)
            return 1024

        return _download

    def _verify_ok(self, *a, **k):
        return {
            "ok": True,
            "checks": {"file_size": {"size_mb": 1.5}, "sha256": {"actual_hash": "f" * 64}},
            "warnings": ["some warning"],
        }

    def _run(
        self,
        version="1.21.0.03",
        payload=None,
        autostart=False,
        custom_url="",
        source_index=0,
        download=None,
        verify=None,
        official=None,
        disk_result=(True, 100000, 1200),
        disk_error=None,
        extra=(),
        target=None,
    ):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(install, "tr_msg", _fake_tr))
        if disk_error is not None:
            stack.enter_context(mock.patch("bedrock_server_manager.utils.check_disk_space", side_effect=disk_error))
        else:
            stack.enter_context(mock.patch("bedrock_server_manager.utils.check_disk_space", return_value=disk_result))
        stack.enter_context(
            mock.patch.object(install, "download_file", download or self._download_writing(payload or self._payload()))
        )
        stack.enter_context(mock.patch.object(install, "verify_download_full", verify or self._verify_ok))
        stack.enter_context(
            mock.patch.object(
                install,
                "get_official_version_info",
                official or (lambda v: {"available": True, "known": True, "stable": "1.21.0.03"}),
            )
        )
        for patcher in extra:
            stack.enter_context(patcher)
        install.do_install(target or self.target, version, autostart, custom_url=custom_url, source_index=source_index)
        return self.ctx.install_state.snapshot()

    def _logs(self):
        return "\n".join(self.ctx.install_state.snapshot()["log"])

    # -- success paths ------------------------------------------------------
    def test_success_fresh_install(self):
        snap = self._run()
        self.assertTrue(snap["done"], snap)
        self.assertEqual(snap["percent"], 100)
        self.assertFalse(snap["busy"])
        self.assertTrue(os.path.isfile(os.path.join(self.target, SERVER_EXE)))
        self.assertEqual(self.ctx.settings["server_dir"], self.target)
        self.assertEqual(self.ctx.settings["installed_version"], "1.21.0.03")
        key = os.path.normcase(os.path.abspath(self.target))
        self.assertEqual(self.ctx.settings["server_versions"][key], "1.21.0.03")
        self.assertEqual(self.ctx.settings["server_dir_history"][0], self.target)
        self.assertFalse(os.path.exists(os.path.join(self.target, install.STAGING_DIRNAME)))
        self.assertIn("installation complete", self._logs().lower())
        self.assertIn("warning (custom url)", self._logs().lower())

    def test_success_update_preserves_user_data(self):
        os.makedirs(self.target)
        _write(os.path.join(self.target, SERVER_EXE), "old-binary")
        _write(os.path.join(self.target, "server.properties"), "server-name=KeepMe\n")
        _write(os.path.join(self.target, "allowlist.json"), '[{"name":"Alex"}]')
        _write(os.path.join(self.target, "worlds", "W", "level.dat"), "world")
        _write(
            os.path.join(self.target, install._INSTALL_MANIFEST),
            json.dumps({"version": 1, "files": ["oldlib.dll", SERVER_EXE]}),
        )
        _write(os.path.join(self.target, "oldlib.dll"), "stale")

        snap = self._run()
        self.assertTrue(snap["done"], snap)
        self.assertEqual(
            open(os.path.join(self.target, "server.properties"), encoding="utf-8").read(), "server-name=KeepMe\n"
        )
        self.assertEqual(
            open(os.path.join(self.target, "allowlist.json"), encoding="utf-8").read(), '[{"name":"Alex"}]'
        )
        self.assertTrue(os.path.isfile(os.path.join(self.target, "worlds", "W", "level.dat")))
        self.assertFalse(os.path.exists(os.path.join(self.target, "oldlib.dll")))
        self.assertIn("preserved existing user data", self._logs().lower())

    def test_autostart_starts_server(self):
        proc = mock.MagicMock()
        self._run(
            autostart=True, extra=[mock.patch.object(install, "get_server_proc", mock.MagicMock(return_value=proc))]
        )
        self.assertEqual(proc.server_dir, self.target)
        proc.start.assert_called_once()
        self.assertIn("auto-starting", self._logs().lower())

    def test_autostart_failure_is_logged(self):
        proc = mock.MagicMock()
        proc.start.side_effect = OSError("cannot start")
        snap = self._run(
            autostart=True, extra=[mock.patch.object(install, "get_server_proc", mock.MagicMock(return_value=proc))]
        )
        self.assertTrue(snap["done"])
        self.assertIn("auto-start failed", self._logs().lower())

    def test_custom_url_is_used(self):
        download = mock.MagicMock(side_effect=self._download_writing(self._payload()))
        self._run(custom_url="  https://mirror.example/bds.zip  ", download=download)
        self.assertEqual(download.call_args.args[0], "https://mirror.example/bds.zip")
        self.assertIn("using custom download url", self._logs().lower())

    def test_empty_version_fetches_latest(self):
        download = mock.MagicMock(side_effect=self._download_writing(self._payload()))
        latest = mock.MagicMock(return_value={"version": "1.21.0.03", "win_url": "https://api.example/bds.zip"})
        self._run(version="", download=download, extra=[mock.patch.object(install, "get_latest_server_info", latest)])
        latest.assert_called_once()
        self.assertEqual(download.call_args.args[0], "https://api.example/bds.zip")
        self.assertEqual(self.ctx.settings["_latest"]["version"], "1.21.0.03")
        self.assertIn("latest stable version", self._logs().lower())

    def test_empty_version_uses_cached_latest(self):
        self.ctx.settings["_latest"] = {"version": "1.21.0.03", "win_url": "https://cache.example/bds.zip"}
        download = mock.MagicMock(side_effect=self._download_writing(self._payload()))
        latest = mock.MagicMock()
        self._run(version="", download=download, extra=[mock.patch.object(install, "get_latest_server_info", latest)])
        latest.assert_not_called()
        self.assertEqual(download.call_args.args[0], "https://cache.example/bds.zip")

    def test_source_index_fallback_uses_next_mirror(self):
        download = mock.MagicMock(side_effect=self._download_writing(self._payload()))
        self._run(source_index=1, download=download)
        self.assertIn("minecraft.net", download.call_args.args[0])

    def test_official_version_unavailable_and_unknown(self):
        download = mock.MagicMock(side_effect=self._download_writing(self._payload()))
        self._run(
            download=download,
            official=lambda v: {"available": False, "known": None, "stable": ""},
        )
        self.assertIn("official version list unavailable", self._logs().lower())

        self.ctx.install_state = install.InstallState()
        target2 = os.path.join(self.tmp, "server2")
        self._run(
            payload=self._payload(),
            target=target2,
            official=lambda v: {"available": True, "known": False, "stable": "1.20.0.1"},
        )
        self.assertIn("not in the official release list", self._logs().lower())

    def test_official_version_check_exception_is_ignored(self):
        self._run(official=mock.MagicMock(side_effect=RuntimeError("offline")))
        self.assertEqual(self.ctx.install_state.snapshot()["percent"], 100)

    def test_disk_check_failure_is_tolerated(self):
        self._run(disk_error=OSError("no stat"))
        self.assertTrue(self.ctx.install_state.snapshot()["done"])
        self.assertIn("disk space check failed", self._logs().lower())

    def test_linux_chmod_is_applied(self):
        calls = []
        real_chmod = os.chmod

        def _chmod(path, mode):
            calls.append((path, mode))
            return real_chmod(path, mode)

        self._run(extra=[mock.patch.object(install, "IS_WINDOWS", False), mock.patch("os.chmod", _chmod)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 0o700)

    def test_linux_chmod_failure_is_logged(self):
        real_chmod = os.chmod

        def _chmod(path, mode, **kwargs):
            # 只让"给服务端二进制授权"这一步失败，避免影响 shutil.copy2 的元数据复制
            if str(path).endswith(SERVER_EXE):
                raise OSError("denied")
            return real_chmod(path, mode, **kwargs)

        self._run(
            extra=[
                mock.patch.object(install, "IS_WINDOWS", False),
                mock.patch("os.chmod", _chmod),
            ]
        )
        self.assertTrue(self.ctx.install_state.snapshot()["done"])
        self.assertIn("failed to set executable permission", self._logs().lower())

    # -- failure paths ------------------------------------------------------
    def test_all_sources_fail(self):
        download = mock.MagicMock(side_effect=OSError("network down"))
        snap = self._run(download=download)
        self.assertEqual(snap["phase"], "error")
        self.assertIn("network down", snap["error"])
        self.assertFalse(snap["done"])
        self.assertFalse(os.path.exists(os.path.join(self.target, install.STAGING_DIRNAME)))
        self.assertEqual(download.call_count, len(install.DOWNLOAD_SOURCES))

    def test_cancel_during_download_stops_retries(self):
        def _cancel(url, dest, **kwargs):
            self.ctx.install_state.cancel.set()
            raise OSError("cancelled by user")

        snap = self._run(download=_cancel)
        self.assertEqual(snap["phase"], "error")
        self.assertIn("all download sources failed", self._logs().lower())

    def test_invalid_version_rejected_before_download(self):
        download = mock.MagicMock()
        snap = self._run(version="../evil", download=download)
        self.assertEqual(snap["phase"], "error")
        download.assert_not_called()

    def test_invalid_version_from_api_rejected(self):
        download = mock.MagicMock()
        latest = mock.MagicMock(return_value={"version": "../evil", "win_url": "http://x"})
        snap = self._run(
            version="", download=download, extra=[mock.patch.object(install, "get_latest_server_info", latest)]
        )
        self.assertEqual(snap["phase"], "error")
        download.assert_not_called()

    def test_verify_failure_aborts_without_installing(self):
        """校验失败必须中止安装（不落盘）。

        已知缺陷（见报告）：失败后 download_ok 仍为 True 且 RuntimeError 被
        "换源重试"循环吞掉，最终以 FileNotFoundError（ZIP 已删除）结束，
        而不是把"完整性校验失败"作为最终错误抛给界面。
        """

        def _verify_fail(*a, **k):
            return {"ok": False, "errors": ["sha256 mismatch"], "checks": {}, "warnings": []}

        snap = self._run(verify=_verify_fail)
        self.assertEqual(snap["phase"], "error")
        self.assertFalse(os.path.exists(os.path.join(self.target, SERVER_EXE)))
        self.assertIn("download integrity check failed", self._logs().lower())
        self.assertIn("sha256 mismatch", self._logs())
        self.assertFalse(os.path.exists(os.path.join(self.target, install.STAGING_DIRNAME)))

    def test_verify_exception_is_skipped(self):
        self._run(verify=mock.MagicMock(side_effect=ValueError("verify broke")))
        self.assertTrue(self.ctx.install_state.snapshot()["done"])
        self.assertIn("integrity check skipped", self._logs().lower())

    def test_missing_executable_in_package(self):
        snap = self._run(payload=self._payload(**{SERVER_EXE: None}))
        self.assertEqual(snap["phase"], "error")
        self.assertIn(SERVER_EXE, snap["error"])
        self.assertFalse(os.path.exists(os.path.join(self.target, install.STAGING_DIRNAME)))

    def test_insufficient_disk_space(self):
        download = mock.MagicMock()
        snap = self._run(download=download, disk_result=(False, 10.0, 1200.0))
        self.assertEqual(snap["phase"], "error")
        self.assertIn("insufficient disk space", snap["error"].lower())
        download.assert_not_called()

    def test_extract_cancel_marks_cancelled(self):
        snap = self._run(extra=[mock.patch.object(install, "extract_zip", side_effect=InterruptedError("stop"))])
        self.assertEqual(snap["phase"], "idle")
        self.assertIn("cancelled", self._logs().lower())
        self.assertFalse(self.ctx.install_state.snapshot()["busy"])

    def test_zip_removed_before_apply(self):
        """下载的 ZIP 在解压后立即删除，不留在服务器目录里。"""
        seen = {}

        def _download(url, dest, **kwargs):
            _make_zip(dest, self._payload())
            seen["zip"] = dest
            return 1

        self._run(download=_download)
        self.assertTrue(seen["zip"].endswith(".zip"))
        self.assertFalse(os.path.exists(seen["zip"]))

    def test_missing_executable_after_apply_fails(self):
        """防御性检查：合并后仍没有 exe 时必须报错（不留下半成品）。"""
        snap = self._run(extra=[mock.patch.object(install, "apply_staged_install", lambda *a, **k: [])])
        self.assertEqual(snap["phase"], "error")
        self.assertIn(SERVER_EXE, snap["error"])

    def test_verify_failure_zip_delete_failure_is_ignored(self):
        def _verify_fail(*a, **k):
            return {"ok": False, "errors": ["sha256 mismatch"], "checks": {}, "warnings": []}

        real_remove = os.remove
        removed = []

        def _remove(path):
            if str(path).endswith(".zip"):
                removed.append(path)
                if len(removed) == 1:
                    raise OSError("zip is locked")
            return real_remove(path)

        with mock.patch("os.remove", _remove):
            snap = self._run(verify=_verify_fail)
        self.assertEqual(snap["phase"], "error")
        self.assertGreaterEqual(len(removed), 2)
        self.assertFalse(os.path.exists(os.path.join(self.target, SERVER_EXE)))

    def test_source_failure_zip_delete_failure_is_ignored(self):
        def _download(url, dest, progress_cb=None, cancel_flag=None, ignore_ssl=False):
            _make_zip(dest, self._payload())
            raise OSError("connection reset")

        real_remove = os.remove

        def _remove(path):
            if str(path).endswith(".zip"):
                raise OSError("zip is locked")
            return real_remove(path)

        with mock.patch("os.remove", _remove):
            snap = self._run(download=_download)
        self.assertEqual(snap["phase"], "error")
        self.assertIn("all download sources failed", self._logs().lower())

    def test_existing_versions_map_is_replaced_when_not_dict(self):
        self.ctx.settings["server_versions"] = "corrupted"
        snap = self._run()
        self.assertEqual(snap["phase"], "done", snap)
        key = os.path.normcase(os.path.abspath(self.target))
        self.assertEqual(self.ctx.settings["server_versions"][key], "1.21.0.03")


# ===========================================================================
# utils.py —— 设置读写 / 原子写 / 路径判断
# ===========================================================================


class TestUtilsSettings(_Base):
    def test_atomic_write_text_and_json(self):
        path = os.path.join(self.tmp, "deep", "a.txt")
        utils.atomic_write_text(path, "hello 中文")
        self.assertEqual(open(path, encoding="utf-8").read(), "hello 中文")
        utils.atomic_write_json(path, {"k": "中文"})
        self.assertEqual(json.loads(open(path, encoding="utf-8").read()), {"k": "中文"})
        # 无目录部分（相对当前目录）也要能写
        cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            utils.atomic_write_text("rel.txt", "x")
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, "rel.txt")))
        finally:
            os.chdir(cwd)

    def test_atomic_write_transient_error_is_retried(self):
        path = os.path.join(self.tmp, "b.txt")
        real_replace = os.replace
        calls = []

        def _flaky(a, b):
            calls.append(1)
            if len(calls) == 1:
                raise OSError(errno.EACCES, "busy")
            return real_replace(a, b)

        with mock.patch("os.replace", _flaky), mock.patch("time.sleep"):
            utils.atomic_write_text(path, "data")
        self.assertEqual(open(path, encoding="utf-8").read(), "data")
        self.assertEqual(len(calls), 2)

    def test_atomic_write_non_transient_error_raises(self):
        path = os.path.join(self.tmp, "c.txt")
        with mock.patch("os.replace", side_effect=OSError(errno.ENOSPC, "full")), self.assertRaises(OSError):
            utils.atomic_write_text(path, "data")
        self.assertEqual([n for n in os.listdir(self.tmp) if n.endswith(".tmp")], [])

    def test_atomic_write_gives_up_after_retries(self):
        path = os.path.join(self.tmp, "d.txt")
        with mock.patch("os.replace", side_effect=PermissionError("busy")), mock.patch("time.sleep"):
            with self.assertRaises(PermissionError):
                utils.atomic_write_text(path, "data")
        self.assertEqual([n for n in os.listdir(self.tmp) if n.endswith(".tmp")], [])

    def test_is_transient_replace_error(self):
        self.assertTrue(utils._is_transient_replace_error(PermissionError("x")))
        self.assertTrue(utils._is_transient_replace_error(OSError(errno.EACCES, "x")))
        self.assertTrue(utils._is_transient_replace_error(OSError(errno.EBUSY, "x")))
        self.assertFalse(utils._is_transient_replace_error(OSError(errno.ENOENT, "x")))
        fake = mock.MagicMock(winerror=32, errno=0)
        self.assertTrue(utils._is_transient_replace_error(fake))

    def test_load_settings(self):
        self.assertEqual(utils.load_settings(), None)
        _write(self.settings_file, json.dumps({"lang": "zh", "server_dir": "/srv"}))
        utils.load_settings()
        self.assertEqual(self.ctx.settings["lang"], "zh")
        self.assertEqual(self.ctx.settings["server_dir"], "/srv")
        # 非 dict 的 JSON 被忽略
        _write(self.settings_file, json.dumps(["bad"]))
        utils.load_settings()
        self.assertEqual(self.ctx.settings["lang"], "zh")
        # 非法 JSON 不抛异常
        _write(self.settings_file, "{oops")
        utils.load_settings()

    def test_load_settings_uses_explicit_dict(self):
        _write(self.settings_file, json.dumps({"a": 1}))
        target = {}
        utils.load_settings(target)
        self.assertEqual(target, {"a": 1})

    def test_save_settings_roundtrip(self):
        self.ctx.settings["lang"] = "en"
        utils.save_settings()
        self.assertEqual(json.loads(open(self.settings_file, encoding="utf-8").read())["lang"], "en")
        utils.save_settings({"x": 1})

    def test_save_settings_failure_is_swallowed(self):
        with mock.patch.object(utils, "atomic_write_json", side_effect=OSError("full")):
            utils.save_settings()

    def test_settings_file_points_to_appdata(self):
        from bedrock_server_manager.constants import APP_DATA_DIR

        self.assertEqual(self.orig_settings_file, os.path.join(APP_DATA_DIR, "settings.json"))


class TestUtilsPaths(_Base):
    def test_is_path_within(self):
        base = os.path.join(self.tmp, "base")
        os.makedirs(base)
        self.assertTrue(utils.is_path_within(base, base))
        self.assertTrue(utils.is_path_within(os.path.join(base, "a", "b.txt"), base))
        self.assertFalse(utils.is_path_within(os.path.join(self.tmp, "other"), base))
        self.assertFalse(utils.is_path_within("", base))
        self.assertFalse(utils.is_path_within(base, ""))
        with mock.patch("os.path.realpath", side_effect=OSError("boom")):
            self.assertFalse(utils.is_path_within(base, base))

    def test_is_path_within_root_base(self):
        root = os.path.abspath(os.sep)
        self.assertTrue(utils.is_path_within(os.path.join(root, "anything"), root))

    def test_is_dangerous_target(self):
        self.assertTrue(utils.is_dangerous_target(os.path.abspath(os.sep)))
        self.assertFalse(utils.is_dangerous_target(self.tmp))
        # 用户主目录
        with mock.patch("os.path.expanduser", return_value=self.tmp):
            self.assertTrue(utils.is_dangerous_target(self.tmp))
        # realpath 抛异常时按"危险"处理
        with mock.patch("os.path.realpath", side_effect=OSError("boom")):
            self.assertTrue(utils.is_dangerous_target(self.tmp))

    @win_only
    def test_is_dangerous_target_windows_system_dirs(self):
        sysroot = os.environ.get("SYSTEMROOT", "C:\\Windows")
        self.assertTrue(utils.is_dangerous_target(os.path.join(sysroot, "System32")))
        # 遍历系统目录时单条路径异常不应影响结论
        real_realpath = os.path.realpath

        def _realpath(p):
            if str(p).lower() in {"c:\\windows", "c:\\program files"}:
                raise OSError("denied")
            return real_realpath(p)

        with mock.patch("os.path.realpath", _realpath):
            self.assertFalse(utils.is_dangerous_target(self.tmp))

    def test_resolve_server_dir_and_props_path(self):
        self.ctx.settings["server_dir"] = "  /srv/mc  "
        self.assertEqual(utils.resolve_server_dir(), "/srv/mc")
        self.assertEqual(utils.props_path(), os.path.join("/srv/mc", "server.properties"))
        self.assertEqual(utils.resolve_server_dir({}), "")
        self.ctx.settings["server_dir"] = ""
        self.assertEqual(utils.props_path(), "")

    def test_sync_log_file(self):
        console = mock.MagicMock()
        self.ctx.settings["server_dir"] = self.tmp
        utils.sync_log_file(console)
        console.set_log_file.assert_called_once_with(os.path.join(self.tmp, "server_console.log"))
        console.reset_mock()
        self.ctx.settings["server_dir"] = os.path.join(self.tmp, "missing")
        utils.sync_log_file(console)
        console.set_log_file.assert_called_once_with(None)

    def test_sync_log_file_uses_context_console(self):
        console = mock.MagicMock()
        self.ctx.console = console
        self.ctx.settings["server_dir"] = ""
        utils.sync_log_file()
        console.set_log_file.assert_called_once_with(None)

    def test_resolve_server_exe_and_installed(self):
        self.assertEqual(utils.resolve_server_exe(""), "")
        self.assertEqual(utils.resolve_server_exe(self.tmp), "")
        _write(os.path.join(self.tmp, SERVER_EXE), "x")
        self.assertEqual(utils.resolve_server_exe(self.tmp), SERVER_EXE)
        _write(os.path.join(self.tmp, utils.MODDED_SERVER_EXE), "x")
        self.assertEqual(utils.resolve_server_exe(self.tmp), utils.MODDED_SERVER_EXE)

        self.ctx.settings["server_dir"] = self.tmp
        self.assertTrue(utils.installed())
        self.ctx.settings["server_dir"] = os.path.join(self.tmp, "missing")
        self.assertFalse(utils.installed())
        self.ctx.settings["server_dir"] = ""
        self.assertFalse(utils.installed())


class TestUtilsDetectServers(_Base):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(self.root)
        self.srv = os.path.join(self.root, "srv1")
        os.makedirs(self.srv)
        _write(os.path.join(self.srv, SERVER_EXE), "bin")
        # 限定扫描范围到临时目录，避免真的扫描用户主目录 / C:\MinecraftServer 等
        self._patch(os.path, "expanduser", lambda p: self.root)
        self._patch(os, "getcwd", lambda: self.root)
        self._patch(utils, "IS_WINDOWS", False)

    def test_detects_current_and_history_dirs(self):
        self.ctx.settings["server_dir"] = self.srv
        found = utils.detect_servers(force_refresh=True)
        self.assertIn(os.path.abspath(self.srv), found)

        # 历史目录 + 去重
        self.ctx.settings["server_dir"] = ""
        other = os.path.join(self.root, "srv2")
        os.makedirs(other)
        _write(os.path.join(other, SERVER_EXE), "bin")
        self.ctx.settings["server_dir_history"] = [other, other]
        found = utils.detect_servers(force_refresh=True)
        self.assertEqual(sorted(found), sorted([os.path.abspath(other), os.path.abspath(self.srv)]))

    def test_cache_is_used_until_forced(self):
        self.ctx.settings["server_dir"] = self.srv
        first = utils.detect_servers(force_refresh=True)
        os.remove(os.path.join(self.srv, SERVER_EXE))
        self.assertEqual(utils.detect_servers(), first)
        self.assertEqual(utils.detect_servers(force_refresh=True), [])

    def test_ignores_hidden_and_missing_dirs(self):
        os.makedirs(os.path.join(self.root, ".hidden"))
        _write(os.path.join(self.root, ".hidden", SERVER_EXE), "bin")
        self.ctx.settings["server_dir"] = os.path.join(self.root, "does-not-exist")
        self.ctx.settings["server_dir_history"] = ["", None]
        found = utils.detect_servers(force_refresh=True)
        # 只扫到 root/srv1；隐藏目录与不存在的目录都不算
        self.assertEqual(found, [os.path.abspath(self.srv)])

    def test_check_dir_exception_is_swallowed(self):
        with mock.patch("os.path.isfile", side_effect=OSError("boom")):
            self.assertEqual(utils.detect_servers(force_refresh=True), [])

    def test_scan_subdirs_exception_is_swallowed(self):
        with mock.patch("os.listdir", side_effect=OSError("denied")):
            self.assertEqual(utils.detect_servers(force_refresh=True), [])

    def test_add_server_dir_history_dedup_and_cap(self):
        self.ctx.settings["server_dir_history"] = [f"/old/{i}" for i in range(25)]
        utils.add_server_dir_history(self.srv)
        history = self.ctx.settings["server_dir_history"]
        self.assertEqual(history[0], self.srv)
        self.assertEqual(len(history), 20)
        utils.add_server_dir_history(self.srv)
        self.assertEqual(self.ctx.settings["server_dir_history"].count(self.srv), 1)
        utils.add_server_dir_history("")
        self.assertEqual(self.ctx.settings["server_dir_history"][0], self.srv)
        self.assertTrue(os.path.isfile(self.settings_file))


class TestUtilsMisc(_Base):
    def test_open_browser_with_retry_success(self):
        with mock.patch.object(utils.webbrowser, "open", return_value=True):
            self.assertTrue(utils.open_browser_with_retry("http://x"))

    def test_open_browser_with_retry_retries(self):
        with (
            mock.patch.object(utils.webbrowser, "open", side_effect=[False, True]) as opener,
            mock.patch("time.sleep"),
        ):
            self.assertTrue(utils.open_browser_with_retry("http://x"))
        self.assertEqual(opener.call_count, 2)

    def test_open_browser_with_retry_exception_then_success(self):
        with (
            mock.patch.object(utils.webbrowser, "open", side_effect=[OSError("nope"), True]),
            mock.patch("time.sleep"),
        ):
            self.assertTrue(utils.open_browser_with_retry("http://x"))

    def test_open_browser_all_attempts_fail(self):
        with (
            mock.patch.object(utils.webbrowser, "open", return_value=False),
            mock.patch("time.sleep"),
            mock.patch.object(utils, "get_ui_lang", return_value="en"),
            mock.patch("subprocess.run") as run,
        ):
            self.assertFalse(utils.open_browser_with_retry("http://127.0.0.1:1"))
        run.assert_called_once()

    def test_open_browser_toast_with_chinese(self):
        with (
            mock.patch.object(utils.webbrowser, "open", return_value=False),
            mock.patch("time.sleep"),
            mock.patch.object(utils, "get_ui_lang", return_value="zh"),
            mock.patch("subprocess.run") as run,
        ):
            self.assertFalse(utils.open_browser_with_retry("http://127.0.0.1:1", max_attempts=1))
        script = run.call_args.args[0][2]
        self.assertTrue(script)

    def test_open_browser_toast_failure_is_swallowed(self):
        with (
            mock.patch.object(utils.webbrowser, "open", side_effect=OSError("no browser")),
            mock.patch("time.sleep"),
            mock.patch("subprocess.run", side_effect=OSError("no powershell")),
        ):
            self.assertFalse(utils.open_browser_with_retry("http://x", max_attempts=1))

    @win_only
    def test_browse_directory_dispatch_windows(self):
        fake_windll = mock.MagicMock()
        fake_windll.shell32.SHBrowseForFolderW.return_value = 42
        fake_windll.shell32.SHGetPathFromIDListW.return_value = 1
        with (
            mock.patch.object(utils, "IS_WINDOWS", True),
            mock.patch.object(ctypes, "windll", fake_windll, create=True),
            mock.patch.object(ctypes, "create_unicode_buffer", lambda n: ctypes.c_wchar_p("C:\\Picked")),
        ):
            self.assertEqual(utils.browse_directory(), "C:\\Picked")
            self.assertEqual(utils.browse_directory(self.tmp, "标题"), "C:\\Picked")
        fake_windll.ole32.CoTaskMemFree.assert_called()

    @win_only
    def test_browse_directory_windows_cancel(self):
        fake_windll = mock.MagicMock()
        fake_windll.shell32.SHBrowseForFolderW.return_value = 0
        with (
            mock.patch.object(utils, "IS_WINDOWS", True),
            mock.patch.object(ctypes, "windll", fake_windll, create=True),
            mock.patch.object(ctypes, "create_unicode_buffer", lambda n: ctypes.c_wchar_p("")),
        ):
            self.assertIsNone(utils.browse_directory())

    @win_only
    def test_browse_directory_windows_path_conversion_failure(self):
        fake_windll = mock.MagicMock()
        fake_windll.shell32.SHBrowseForFolderW.return_value = 7
        fake_windll.shell32.SHGetPathFromIDListW.return_value = 0
        with (
            mock.patch.object(utils, "IS_WINDOWS", True),
            mock.patch.object(ctypes, "windll", fake_windll, create=True),
            mock.patch.object(ctypes, "create_unicode_buffer", lambda n: ctypes.c_wchar_p("")),
        ):
            self.assertIsNone(utils.browse_directory())

    def test_browse_directory_tk(self):
        class _Root:
            def withdraw(self):
                pass

            def attributes(self, *a):
                pass

            def update(self):
                pass

            def destroy(self):
                pass

        fake_tk = types.ModuleType("tkinter")
        fake_tk.Tk = _Root
        fake_fd = types.ModuleType("tkinter.filedialog")
        fake_fd.askdirectory = lambda **kwargs: "C:\\TkPicked"
        fake_tk.filedialog = fake_fd

        with (
            mock.patch.object(utils, "IS_WINDOWS", False),
            mock.patch.dict(sys.modules, {"tkinter": fake_tk, "tkinter.filedialog": fake_fd}),
        ):
            self.assertEqual(utils.browse_directory("/tmp", "选择"), "C:\\TkPicked")

    def test_browse_directory_tk_cancel(self):
        class _Root:
            def withdraw(self):
                pass

            def attributes(self, *a):
                pass

            def update(self):
                pass

            def destroy(self):
                pass

        fake_tk = types.ModuleType("tkinter")
        fake_tk.Tk = _Root
        fake_fd = types.ModuleType("tkinter.filedialog")
        fake_fd.askdirectory = lambda **kwargs: ""
        fake_tk.filedialog = fake_fd

        with (
            mock.patch.object(utils, "IS_WINDOWS", False),
            mock.patch.dict(sys.modules, {"tkinter": fake_tk, "tkinter.filedialog": fake_fd}),
        ):
            self.assertIsNone(utils.browse_directory())

    def test_get_lan_ip_success(self):
        class _Sock:
            def connect(self, addr):
                self.addr = addr

            def getsockname(self):
                return ("192.168.1.7", 1234)

            def close(self):
                pass

        with mock.patch.object(socket, "socket", return_value=_Sock()):
            self.assertEqual(utils.get_lan_ip(), "192.168.1.7")

    def test_get_lan_ip_fallback_to_hostname(self):
        with (
            mock.patch.object(socket, "socket", side_effect=OSError("no route")),
            mock.patch.object(socket, "gethostbyname", return_value="10.0.0.9"),
        ):
            self.assertEqual(utils.get_lan_ip(), "10.0.0.9")

    def test_get_lan_ip_final_fallback(self):
        with (
            mock.patch.object(socket, "socket", side_effect=OSError("no route")),
            mock.patch.object(socket, "gethostbyname", side_effect=OSError("no dns")),
        ):
            self.assertEqual(utils.get_lan_ip(), "127.0.0.1")

    def test_check_disk_space(self):
        ok, free_mb, required = utils.check_disk_space(self.tmp, required_mb=1)
        self.assertTrue(ok)
        self.assertGreater(free_mb, 0)
        self.assertEqual(required, 1)
        with mock.patch("shutil.disk_usage", side_effect=OSError("boom")):
            self.assertEqual(utils.check_disk_space(self.tmp, required_mb=5), (True, -1, 5))

    def test_theme_load_and_save(self):
        self.assertEqual(utils.load_theme(), state.DEFAULT_THEME)
        utils.save_theme({"preset": "custom", "accent": "#fff", "mode": "light", "extra": 1})
        theme = utils.load_theme()
        self.assertEqual(theme["preset"], "custom")
        self.assertEqual(theme["accent"], "#fff")
        self.assertEqual(theme["mode"], "light")
        _write(self.theme_file, json.dumps(["not", "a", "dict"]))
        self.assertEqual(utils.load_theme(), state.DEFAULT_THEME)
        _write(self.theme_file, "{bad json")
        self.assertEqual(utils.load_theme(), state.DEFAULT_THEME)

    def test_save_theme_failure(self):
        with mock.patch.object(utils, "atomic_write_json", side_effect=OSError("full")):
            self.assertFalse(utils.save_theme({"preset": "x"}))

    @win_only
    def test_get_system_lang_windows_api(self):
        fake_windll = mock.MagicMock()

        fake_windll.kernel32.GetUserDefaultUILanguage.return_value = 0x0804  # 中文
        with mock.patch.object(ctypes, "windll", fake_windll, create=True):
            self.assertEqual(utils.get_system_lang(), "zh")

        fake_windll.kernel32.GetUserDefaultUILanguage.return_value = 0x0409  # 英文
        with mock.patch.object(ctypes, "windll", fake_windll, create=True):
            self.assertEqual(utils.get_system_lang(), "en")

    def test_get_system_lang_env_fallback(self):
        fake_windll = mock.MagicMock()
        fake_windll.kernel32.GetUserDefaultUILanguage.side_effect = OSError("no api")
        with (
            mock.patch.object(ctypes, "windll", fake_windll, create=True),
            mock.patch.dict(os.environ, {"LANG": "zh_CN.UTF-8"}, clear=True),
        ):
            self.assertEqual(utils.get_system_lang(), "zh")

    def test_get_system_lang_locale_fallback(self):
        fake_windll = mock.MagicMock()
        fake_windll.kernel32.GetUserDefaultUILanguage.side_effect = OSError("no api")
        with (
            mock.patch.object(ctypes, "windll", fake_windll, create=True),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("locale.getdefaultlocale", return_value=("zh_CN", "UTF-8")),
        ):
            self.assertEqual(utils.get_system_lang(), "zh")

    def test_get_system_lang_english(self):
        fake_windll = mock.MagicMock()
        fake_windll.kernel32.GetUserDefaultUILanguage.side_effect = OSError("no api")
        with (
            mock.patch.object(ctypes, "windll", fake_windll, create=True),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("locale.getdefaultlocale", return_value=("en_US", "UTF-8")),
        ):
            self.assertEqual(utils.get_system_lang(), "en")

    def test_get_system_lang_all_failures(self):
        fake_windll = mock.MagicMock()
        fake_windll.kernel32.GetUserDefaultUILanguage.side_effect = OSError("no api")
        with (
            mock.patch.object(ctypes, "windll", fake_windll, create=True),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("locale.getdefaultlocale", side_effect=OSError("no locale")),
        ):
            self.assertEqual(utils.get_system_lang(), "en")

    def test_ui_lang(self):
        self.ctx.settings["lang"] = "en"
        self.assertEqual(utils.get_ui_lang(), "en")
        self.ctx.settings["lang"] = "zh"
        self.assertEqual(utils.get_ui_lang(), "zh")
        self.ctx.settings.pop("lang", None)
        with mock.patch.object(utils, "get_system_lang", return_value="en"):
            self.assertEqual(utils.get_ui_lang(), "en")

    def test_set_ui_lang(self):
        self.assertTrue(utils.set_ui_lang("en"))
        self.assertEqual(self.ctx.settings["lang"], "en")
        self.assertTrue(os.path.isfile(self.settings_file))
        self.assertFalse(utils.set_ui_lang("fr"))
        self.assertEqual(self.ctx.settings["lang"], "en")

    def test_tr_msg(self):
        with mock.patch.object(utils, "get_ui_lang", return_value="zh"):
            self.assertEqual(utils.tr_msg("中文", "english"), "中文")
            self.assertEqual(utils.tr_msg("值=%s", "value=%s", "x"), "值=x")
        with mock.patch.object(utils, "get_ui_lang", return_value="en"):
            self.assertEqual(utils.tr_msg("中文", "english"), "english")
            self.assertEqual(utils.tr_msg("值=%d", "value=%d", 3), "value=3")
            # 占位符不匹配时回退模板而不是抛异常
            self.assertEqual(utils.tr_msg("值=%d", "value=%d", "not-int"), "value=%d")

    def test_get_console_helper(self):
        sentinel = object()
        self.ctx.console = sentinel
        self.assertIs(utils._get_console(), sentinel)

    def test_get_server_proc_helper(self):
        self.ctx.server_proc = "proc"
        self.assertEqual(utils._get_server_proc(), ("proc", self.ctx.server_lock))


# ===========================================================================
# config.py
# ===========================================================================


class TestConfigEncodingAndProperties(_Base):
    def setUp(self):
        super().setUp()
        self.props = os.path.join(self.tmp, "server.properties")

    def test_detect_file_encoding(self):
        self.assertEqual(config.detect_file_encoding(os.path.join(self.tmp, "missing")), "utf-8")
        utf8 = _write(os.path.join(self.tmp, "u.properties"), "server-name=中文\n")
        self.assertEqual(config.detect_file_encoding(utf8), "utf-8")
        gbk_path = os.path.join(self.tmp, "g.properties")
        with open(gbk_path, "wb") as f:
            f.write("server-name=中文\n".encode("gbk"))
        self.assertEqual(config.detect_file_encoding(gbk_path), "gbk")

    def test_load_properties_parsing_and_comments(self):
        _write(self.props, "# comment\n\nserver-name=My Server\nserver-port=19132\n  \nno-equals-line\n")
        items = config.load_properties(self.props)
        self.assertEqual(items, [("server-name", "My Server"), ("server-port", "19132")])
        self.assertEqual(config.load_properties(os.path.join(self.tmp, "none")), [])

    def test_load_properties_cache_hit_and_invalidation(self):
        _write(self.props, "server-name=A\n")
        first = config.load_properties(self.props)
        second = config.load_properties(self.props)
        self.assertIs(first, second)  # 命中缓存
        _write(self.props, "server-name=BB\n")  # 大小变化 -> 缓存失效
        third = config.load_properties(self.props)
        self.assertEqual(third, [("server-name", "BB")])
        self.assertIsNot(third, first)

    def test_load_properties_stat_error(self):
        _write(self.props, "server-name=A\n")
        with (
            mock.patch.object(config.os.path, "isfile", lambda p: True),
            mock.patch("os.stat", side_effect=OSError("stat failed")),
        ):
            self.assertEqual(config.load_properties(self.props), [])

    def test_load_properties_read_error(self):
        _write(self.props, "server-name=A\n")
        real_open = open
        calls = []

        def _open(path, *a, **k):
            calls.append(path)
            if len(calls) == 1:
                return real_open(path, *a, **k)
            raise OSError("read failed")

        with mock.patch("builtins.open", _open):
            self.assertEqual(config.load_properties(self.props), [])

    def test_save_properties_preserves_comments_and_unknown(self):
        _write(self.props, "# 注释\nserver-name=Old\nunknown-key=keep\nno-equals\n")
        ok = config.save_properties(self.props, [("server-name", "New"), ("max-players", "20")])
        self.assertTrue(ok)
        text = open(self.props, encoding="utf-8").read()
        self.assertIn("# 注释", text)
        self.assertIn("server-name=New", text)
        self.assertIn("unknown-key=keep", text)
        self.assertIn("no-equals", text)
        self.assertIn("max-players=20", text)

    def test_save_properties_to_new_file(self):
        path = os.path.join(self.tmp, "new", "server.properties")
        self.assertTrue(config.save_properties(path, [("a", "1")]))
        self.assertEqual(open(path, encoding="utf-8").read(), "a=1\n")
        # 写入后缓存必须失效
        self.assertEqual(config.load_properties(path), [("a", "1")])

    def test_save_properties_failure(self):
        with mock.patch.object(config, "atomic_write_text", side_effect=OSError("full")):
            self.assertFalse(config.save_properties(self.props, [("a", "1")]))

    def test_save_properties_invalid_dir(self):
        blocker = _write(os.path.join(self.tmp, "blocker"), "x")
        self.assertFalse(config.save_properties(os.path.join(blocker, "a.properties"), [("a", "1")]))

    def test_properties_to_dict(self):
        self.assertEqual(config.properties_to_dict([("a", "1"), ("b", "2")]), {"a": "1", "b": "2"})


class TestConfigValidation(_Base):
    def test_rejects_non_dict(self):
        ok, err, field = config.validate_config_updates(["x"])
        self.assertFalse(ok)
        self.assertEqual(field, "")

    def test_int_range_and_reserved_port(self):
        ok, err, field = config.validate_config_updates({"server-port": "abc"})
        self.assertFalse(ok)
        self.assertEqual(field, "server-port")
        ok, _err, field = config.validate_config_updates({"max-players": 0})
        self.assertFalse(ok)
        self.assertEqual(field, "max-players")
        ok, _err, field = config.validate_config_updates({"server-port": 80})
        self.assertFalse(ok)
        self.assertEqual(field, "server-port")
        ok, _err, _f = config.validate_config_updates({"server-port": 19132, "max-players": "10"})
        self.assertTrue(ok)

    def test_bool_and_choice(self):
        ok, _err, field = config.validate_config_updates({"allow-cheats": "maybe"})
        self.assertFalse(ok)
        self.assertEqual(field, "allow-cheats")
        ok, _err, field = config.validate_config_updates({"gamemode": "nightmare"})
        self.assertFalse(ok)
        self.assertEqual(field, "gamemode")
        ok, _err, _f = config.validate_config_updates({"allow-cheats": "TRUE", "gamemode": "creative"})
        self.assertTrue(ok)

    def test_text_length_constraints(self):
        ok, _err, field = config.validate_config_updates({"server-name": ""})
        self.assertFalse(ok)
        self.assertEqual(field, "server-name")
        ok, _err, field = config.validate_config_updates({"server-name": "x" * 200})
        self.assertFalse(ok)
        self.assertEqual(field, "server-name")
        ok, _err, _f = config.validate_config_updates({"language": "zh_CN", "level-seed": ""})
        self.assertTrue(ok)

    def test_unknown_key_is_accepted(self):
        ok, _err, _f = config.validate_config_updates({"totally-unknown": "whatever"})
        self.assertTrue(ok)

    def test_int_and_float_meta_types(self):
        meta = {
            "custom-int": {"type": "int"},
            "custom-float": {"type": "float"},
        }
        with mock.patch.object(config, "_PROP_META_DICT", meta):
            ok, _err, field = config.validate_config_updates({"custom-int": "abc"})
            self.assertFalse(ok)
            self.assertEqual(field, "custom-int")
            ok, _err, field = config.validate_config_updates({"custom-float": "abc"})
            self.assertFalse(ok)
            self.assertEqual(field, "custom-float")
            ok, _err, _f = config.validate_config_updates({"custom-int": 3, "custom-float": "1.5"})
            self.assertTrue(ok)

    def test_full_prop_meta_and_lazy_dict(self):
        self.assertGreater(len(config.FULL_PROP_META), len(config.PROP_META))
        meta = config._build_prop_meta_dict()
        self.assertEqual(meta["gamemode"]["choices"][0], "survival")
        self.assertEqual(meta["gamemode"]["default"], "survival")
        self.assertEqual(meta["server-name"]["default"], "Dedicated Server")
        with mock.patch.object(
            config, "FULL_PROP_META", config.FULL_PROP_META + [("extra-choice", "扩展", "choice", "a", ["a", "b"])]
        ):
            meta2 = config._build_prop_meta_dict()
        self.assertEqual(meta2["extra-choice"]["default"], "a")
        self.assertEqual(meta2["extra-choice"]["choices"], ["a", "b"])

        with mock.patch.object(config, "_PROP_META_DICT", None):
            first = config._get_prop_meta_dict()
            second = config._get_prop_meta_dict()
        self.assertIs(first, second)


class TestConfigHistory(_Base):
    def setUp(self):
        super().setUp()
        self.server = os.path.join(self.tmp, "server")
        os.makedirs(self.server)
        self.props = _write(os.path.join(self.server, "server.properties"), "server-name=A\n")

    def test_backup_without_properties(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        self.assertIsNone(config.backup_config_history(empty))

    def test_backup_creates_history_entry(self):
        path = config.backup_config_history(self.server)
        self.assertTrue(path)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.basename(path).startswith("server.properties."))
        self.assertEqual(open(path, encoding="utf-8").read(), "server-name=A\n")

    def test_backup_prunes_to_20(self):
        history = os.path.join(self.server, config.CONFIG_HISTORY_DIR)
        os.makedirs(history)
        for i in range(25):
            _write(os.path.join(history, f"server.properties.2020010{i:02d}_000000.bak"), "x")
        config.backup_config_history(self.server)
        backups = [f for f in os.listdir(history) if f.endswith(".bak")]
        self.assertEqual(len(backups), 20)

    def test_backup_failure_returns_none(self):
        with mock.patch("shutil.copy2", side_effect=OSError("denied")):
            self.assertIsNone(config.backup_config_history(self.server))

    def test_list_history(self):
        self.assertEqual(config.list_config_history(self.server), [])
        config.backup_config_history(self.server)
        entries = config.list_config_history(self.server)
        self.assertEqual(len(entries), 1)
        self.assertEqual(set(entries[0]), {"name", "path", "time", "size"})
        self.assertGreater(entries[0]["size"], 0)
        # 非备份文件被忽略
        _write(os.path.join(self.server, config.CONFIG_HISTORY_DIR, "notes.txt"), "x")
        self.assertEqual(len(config.list_config_history(self.server)), 1)

    def test_list_history_exception(self):
        config.backup_config_history(self.server)
        with mock.patch("os.listdir", side_effect=OSError("denied")):
            self.assertEqual(config.list_config_history(self.server), [])

    def test_rollback_rejects_outside_path(self):
        ok, err = config.rollback_config(self.server, os.path.join("..", "evil.bak"))
        self.assertFalse(ok)
        self.assertIn("非法路径", err)

    def test_rollback_missing_backup(self):
        ok, err = config.rollback_config(self.server, "server.properties.20200101_000000.bak")
        self.assertFalse(ok)
        self.assertIn("不存在", err)

    def test_rollback_restores_and_backs_up_current(self):
        """回滚会先备份当前配置再恢复旧备份。

        注意：用受控时间戳，避免"同一秒内两次备份同名互相覆盖"的已知缺陷
        影响断言（该缺陷见最终报告）。
        """
        timestamps = iter(["20260101_000000", "20260101_000001"])
        with mock.patch.object(config.time, "strftime", lambda *a, **k: next(timestamps)):
            backup_path = config.backup_config_history(self.server)
            _write(self.props, "server-name=B\n")
            name = os.path.basename(backup_path)
            ok, err = config.rollback_config(self.server, name)
        self.assertTrue(ok, err)
        self.assertEqual(open(self.props, encoding="utf-8").read(), "server-name=A\n")
        # 回滚前已再次备份（当前 B 版本）
        entries = config.list_config_history(self.server)
        self.assertEqual(len(entries), 2)

    def test_rollback_copy_failure(self):
        backup_path = config.backup_config_history(self.server)
        name = os.path.basename(backup_path)
        real_copy = shutil.copy2
        calls = []

        def _copy(src, dst, *a, **k):
            calls.append((src, dst))
            if len(calls) == 1:
                return real_copy(src, dst, *a, **k)
            raise OSError("denied")

        with mock.patch("shutil.copy2", _copy):
            ok, err = config.rollback_config(self.server, name)
        self.assertFalse(ok)
        self.assertIn("恢复失败", err)


# ===========================================================================
# state.py
# ===========================================================================


class TestStateProxies(_Base):
    def test_proxy_attributes(self):
        self.ctx.api_token = "token-abc"
        self.ctx.settings["server_dir"] = "/srv"
        console = object()
        self.ctx.console = console
        install_state = install.InstallState()
        self.ctx.install_state = install_state

        self.assertEqual(state.API_TOKEN, "token-abc")
        self.assertEqual(state.app_start_time, self.ctx.app_start_time)
        self.assertIs(state.settings, self.ctx.settings)
        self.assertIs(state.console, console)
        self.assertIs(state.install_state, install_state)

    def test_proxy_follows_new_instance_after_reset(self):
        self.ctx.settings["marker"] = 1
        old_settings = self.ctx.settings
        AppContext.reset()
        new_ctx = AppContext.instance()
        self.assertIsNot(state.settings, old_settings)
        self.assertIs(state.settings, new_ctx.settings)
        self.assertEqual(state.API_TOKEN, new_ctx.api_token)
        self.assertEqual(state.app_start_time, new_ctx.app_start_time)

    def test_proxy_default_console_and_install_state(self):
        # 未初始化时返回空对象而不是 None
        self.assertIsNotNone(state.console)
        self.assertIsNotNone(state.install_state)
        self.assertFalse(state.install_state.busy)
        self.assertEqual(state.install_state.snapshot()["phase"], "idle")

    def test_unknown_attribute_raises(self):
        with self.assertRaises(AttributeError):
            operator.attrgetter("totally_missing")(state)

    def test_theme_constants(self):
        self.assertEqual(state.THEME_FILE, self.theme_file)
        self.assertEqual(state.DEFAULT_THEME["preset"], "minecraft_dark")
        self.assertEqual(state.DEFAULT_THEME["mode"], "dark")


class TestMigrateLegacyData(_Base):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        self.appdata = os.path.join(self.tmp, "appdata")
        self.data = os.path.join(self.tmp, "data")
        for d in (self.home, self.appdata, self.data):
            os.makedirs(d)
        self.logs = os.path.join(self.data, "logs")
        self.cache = os.path.join(self.data, "cache")
        self._patch(state, "APP_DATA_DIR", self.data)
        self._patch(state, "LOGS_DIR", self.logs)
        self._patch(state, "CACHE_DIR", self.cache)
        self._patch(os.path, "expanduser", lambda p: self.home)
        env = mock.patch.dict(os.environ, {"APPDATA": self.appdata})
        env.start()
        self.addCleanup(env.stop)

    def test_migrates_all_legacy_files(self):
        legacy = {
            os.path.join(self.home, ".bedrock_server_manager_settings.json"): (
                os.path.join(self.data, "settings.json"),
                "s",
            ),
            os.path.join(self.home, ".bedrock_server_manager_theme.json"): (os.path.join(self.data, "theme.json"), "t"),
            os.path.join(self.home, ".bedrock_server_manager_crash.log"): (
                os.path.join(self.logs, "crash.log"),
                "c",
            ),
            os.path.join(self.home, ".bedrock_server_hashes.json"): (os.path.join(self.cache, "hashes.json"), "h"),
            os.path.join(self.home, ".bedrock_server_manager", "tray.log"): (os.path.join(self.logs, "tray.log"), "y"),
            os.path.join(self.appdata, "BedrockServerManager", "logs", "app.log"): (
                os.path.join(self.logs, "app.log"),
                "a",
            ),
        }
        for old, (_new, content) in legacy.items():
            _write(old, content)
        # 空的旧目录应被清理
        os.makedirs(os.path.join(self.appdata, "BedrockServerManager"), exist_ok=True)

        migrated = state.migrate_legacy_data()

        self.assertEqual(len(migrated), 6)
        for old, (new, content) in legacy.items():
            self.assertFalse(os.path.exists(old), old)
            self.assertTrue(os.path.isfile(new), new)
            self.assertEqual(open(new, encoding="utf-8").read(), content)
        # 迁移后 tray.log 所在旧目录变空 -> 被 rmdir
        self.assertFalse(os.path.exists(os.path.join(self.home, ".bedrock_server_manager")))

    def test_skips_when_target_exists(self):
        old = os.path.join(self.home, ".bedrock_server_manager_settings.json")
        target = os.path.join(self.data, "settings.json")
        _write(old, "old")
        _write(target, "new")
        migrated = state.migrate_legacy_data()
        self.assertEqual(migrated, [])
        self.assertEqual(open(target, encoding="utf-8").read(), "new")
        self.assertTrue(os.path.exists(old))

    def test_copy_failure_is_logged(self):
        _write(os.path.join(self.home, ".bedrock_server_manager_settings.json"), "old")
        with mock.patch("shutil.copy2", side_effect=OSError("denied")):
            self.assertEqual(state.migrate_legacy_data(), [])

    def test_remove_failure_still_reports_migration(self):
        _write(os.path.join(self.home, ".bedrock_server_manager_settings.json"), "old")
        with mock.patch("os.remove", side_effect=PermissionError("locked")):
            migrated = state.migrate_legacy_data()
        self.assertEqual(migrated, [".bedrock_server_manager_settings.json"])

    def test_rmdir_failure_is_swallowed(self):
        os.makedirs(os.path.join(self.home, ".bedrock_server_manager"))
        with mock.patch("os.rmdir", side_effect=OSError("denied")):
            state.migrate_legacy_data()
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".bedrock_server_manager")))

    def test_non_empty_old_dir_is_kept(self):
        _write(os.path.join(self.home, ".bedrock_server_manager", "other.log"), "x")
        state.migrate_legacy_data()
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".bedrock_server_manager")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
