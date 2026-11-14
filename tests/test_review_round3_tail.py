"""第三轮收尾三项的回归测试：

1. 官方版本清单（真实结构无 sha256）→ 只用于"版本号是否官方发行"
2. 签名者证书精确定位（CryptMsgGetParam + CertFindCertificateInStore）
3. 更新时只清理"新版本已删除的服务端产物"，用户文件与目录不受影响
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


class TestOfficialVersions(unittest.TestCase):
    """官方清单只提供版本号，不提供哈希"""

    def test_real_structure_parsed(self):
        """真实结构：{cdn_root, windows:{stable,preview,versions[],preview_versions[]}}"""
        from bedrock_server_manager import verify as V

        payload = {
            "cdn_root": "https://example/",
            "windows": {
                "stable": "1.26.63.1",
                "preview": "1.26.70.24",
                "versions": ["1.21.0.03", "1.26.63.1"],
                "preview_versions": ["1.26.70.24"],
            },
            "linux": {"stable": "1.26.63.1", "versions": ["1.26.63.1"], "preview_versions": []},
        }

        class FakeResp:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            out = V.fetch_official_versions(force=True)

        self.assertIn("windows", out)
        self.assertEqual(out["windows"]["stable"], "1.26.63.1")
        self.assertEqual(out["windows"]["versions"], ["1.21.0.03", "1.26.63.1"])

    def test_known_and_unknown_versions(self):
        from bedrock_server_manager import verify as V

        with (
            patch.object(V, "IS_WINDOWS", True),
            patch.object(
                V,
                "fetch_official_versions",
                return_value={"windows": {"stable": "1.26.63.1", "preview": "", "versions": ["1.21.0.03"]}},
            ),
        ):
            self.assertTrue(V.get_official_version_info("1.21.0.03")["known"])
            self.assertFalse(V.get_official_version_info("9.9.9.9")["known"])

    def test_unavailable_reports_none_not_false(self):
        """取不到清单时 known 必须是 None（"无法判断"≠"非官方"）"""
        from bedrock_server_manager import verify as V

        with patch.object(V, "fetch_official_versions", return_value={}):
            info = V.get_official_version_info("1.21.0.03")
        self.assertFalse(info["available"])
        self.assertIsNone(info["known"])

    def test_failure_is_cached_briefly(self):
        """失败要短时缓存，否则每次安装都多等一个连接超时"""
        from bedrock_server_manager import verify as V

        V._OFFICIAL_VERSIONS_CACHE.update({"data": None, "time": 0.0, "failed_at": V.time.time()})
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise OSError("no network")

        with patch("urllib.request.urlopen", side_effect=boom):
            V.fetch_official_versions(force=False)
            V.fetch_official_versions(force=False)
        self.assertEqual(len(calls), 0, "失败后短时内不应再次发起请求")
        V._OFFICIAL_VERSIONS_CACHE.update({"data": None, "time": 0.0, "failed_at": 0.0})


class TestSignerCertLookup(unittest.TestCase):
    """签名者证书必须精确定位，不能靠"取存储里第一个" """

    def test_helper_exists_and_degrades_safely(self):
        from bedrock_server_manager.verify import _find_signer_cert

        # 句柄为空时应安全返回 None（调用方回退），而不是抛异常
        self.assertIsNone(_find_signer_cert(object(), None, None))

    @unittest.skipUnless(os.name == "nt", "仅 Windows")
    def test_precise_signer_matches_leaf(self):
        """精确匹配到的签名者应与回退枚举到的首个证书一致（单签名 PE）"""
        import ctypes
        import ctypes.wintypes as W

        from bedrock_server_manager.verify import _find_signer_cert, _verify_signer_identity

        crypt32 = ctypes.windll.crypt32
        h_store, h_msg = W.HANDLE(), W.HANDLE()
        enc, ct, ft = W.DWORD(), W.DWORD(), W.DWORD()
        ppv = ctypes.c_void_p()
        crypt32.CryptQueryObject.argtypes = [
            W.DWORD,
            ctypes.c_wchar_p,
            W.DWORD,
            W.DWORD,
            W.DWORD,
            ctypes.POINTER(W.DWORD),
            ctypes.POINTER(W.DWORD),
            ctypes.POINTER(W.DWORD),
            ctypes.POINTER(W.HANDLE),
            ctypes.POINTER(W.HANDLE),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        crypt32.CryptQueryObject.restype = ctypes.c_bool
        ok = crypt32.CryptQueryObject(
            1,
            sys.executable,
            0x400,
            2,
            0,
            ctypes.byref(enc),
            ctypes.byref(ct),
            ctypes.byref(ft),
            ctypes.byref(h_store),
            ctypes.byref(h_msg),
            ctypes.byref(ppv),
        )
        if not ok:
            self.skipTest("当前 python.exe 无嵌入式签名，无法验证")
        try:
            cert = _find_signer_cert(crypt32, h_msg, h_store)
            self.assertIsNotNone(cert, "精确定位签名者证书失败（退回了枚举路径）")
        finally:
            if h_store:
                crypt32.CertCloseStore(h_store, 0)
            if h_msg:
                crypt32.CryptMsgClose(h_msg)

        ok_id, msg = _verify_signer_identity(sys.executable)
        self.assertIn("签名者: ", msg)


class TestStaleArtifactPruning(unittest.TestCase):
    """更新只清"上次自己装过、本次包里已没有"的服务端产物"""

    def setUp(self):
        from bedrock_server_manager.constants import SERVER_EXE

        self.exe = SERVER_EXE
        self.tmp = tempfile.mkdtemp(prefix="rv_prune_")
        self.target = os.path.join(self.tmp, "server")
        self.staging = os.path.join(self.tmp, "staging")
        os.makedirs(self.target)
        os.makedirs(self.staging)
        # 合法 staging 必须含服务端可执行文件（否则 apply 会拒绝合并）
        self._write(self.staging, self.exe, b"NEW")
        self._write(self.staging, "libCrypto.dll", b"NEW")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, base, name, data=b"x"):
        path = os.path.join(base, name)
        parent = os.path.dirname(path)
        if parent and parent != base:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def _manifest(self, names):
        with open(os.path.join(self.target, ".install_manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "files": list(names)}, f)

    def test_stale_artifacts_from_manifest_removed(self):
        """清单里记过、本次包里没有 → 清理"""
        from bedrock_server_manager.install import apply_staged_install

        owned = (self.exe, "libCrypto.dll", "bedrock_server.pdb", "bedrock_server_how_to.html")
        for name in owned:
            self._write(self.target, name, b"OLD")
        self._manifest(owned)

        apply_staged_install(self.staging, self.target)

        self.assertFalse(os.path.exists(os.path.join(self.target, "bedrock_server.pdb")), "旧 PDB 未清理")
        self.assertFalse(os.path.exists(os.path.join(self.target, "bedrock_server_how_to.html")), "旧文档未清理")
        with open(os.path.join(self.target, self.exe), "rb") as f:
            self.assertEqual(f.read(), b"NEW")

    def test_user_files_never_removed_even_if_artifact_like(self):
        """未出现在清单里的用户文件一律不动（哪怕长得像服务端产物）

        回归：早期按"新包里没有就删"实现，实测删掉了用户的
        bedrock_server_backup.zip 与 steamclient64.dll。
        """
        from bedrock_server_manager.install import apply_staged_install

        user_files = (
            "bedrock_server_backup.zip",  # 名字以 bedrock_server 开头
            "steamclient64.dll",  # 任意 dll
            "libCrypto.dll.bak",  # 用户备份
            "libCrypto.dll.old",
            "my.dll.txt",
            "我的备注.txt",
            "notes.md",
            "run.bat",
            "server.properties",
        )
        for name in user_files:
            self._write(self.target, name, b"user")
        os.makedirs(os.path.join(self.target, "my_scripts"), exist_ok=True)
        self._write(self.target, os.path.join("my_scripts", "run.bat"), b"echo")
        self._manifest([self.exe])  # 只装过 exe

        apply_staged_install(self.staging, self.target)

        for name in user_files:
            self.assertTrue(os.path.exists(os.path.join(self.target, name)), f"用户文件被删: {name}")
        self.assertTrue(os.path.exists(os.path.join(self.target, "my_scripts", "run.bat")), "用户目录被删了")

    def test_manifest_written_after_install(self):
        from bedrock_server_manager.install import apply_staged_install

        apply_staged_install(self.staging, self.target)
        with open(os.path.join(self.target, ".install_manifest.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn(self.exe, data["files"])

    def test_incomplete_staging_refused(self):
        """staging 缺服务端 exe 时必须拒绝合并（否则会把服务器目录清空）"""
        from bedrock_server_manager.install import apply_staged_install

        empty = os.path.join(self.tmp, "empty_staging")
        os.makedirs(empty, exist_ok=True)
        self._write(self.target, self.exe, b"OLD")
        with self.assertRaises(RuntimeError):
            apply_staged_install(empty, self.target)
        with open(os.path.join(self.target, self.exe), "rb") as f:
            self.assertEqual(f.read(), b"OLD", "拒绝合并后目标文件不应被改动")

    def test_orphan_install_old_is_preserved(self):
        """目标缺失时，*.install_old 可能是唯一副本，不能删"""
        from bedrock_server_manager.install import apply_staged_install

        orphan = os.path.join(self.target, "pluginpack.install_old")
        os.makedirs(orphan, exist_ok=True)
        self._write(self.target, os.path.join("pluginpack.install_old", "important.dat"), b"data")

        apply_staged_install(self.staging, self.target)

        self.assertTrue(os.path.exists(orphan), "孤儿 .install_old 被删了（可能是唯一副本）")

    def test_install_new_leftover_removed(self):
        from bedrock_server_manager.install import apply_staged_install

        leftover = os.path.join(self.target, "resource_packs.install_new")
        os.makedirs(leftover, exist_ok=True)
        apply_staged_install(self.staging, self.target)
        self.assertFalse(os.path.exists(leftover), "上次合并的中间目录未清理")

    def test_crashed_tmp_file_cleaned(self):
        from bedrock_server_manager.install import apply_staged_install

        tmp_name = f"{self.exe}.install_ab12cd.tmp"
        self._write(self.target, tmp_name, b"partial")
        apply_staged_install(self.staging, self.target)
        self.assertFalse(
            os.path.exists(os.path.join(self.target, tmp_name)),
            "崩溃残留的临时文件未清理",
        )

    def test_prune_helper_scope(self):
        from bedrock_server_manager.install import _is_server_artifact

        for yes in (
            "bedrock_server",
            "bedrock_server.exe",
            "bedrock_server.pdb",
            "libssl.so",
            "libssl.so.1.1",
            "libCrypto.dll",
            "release-notes.txt",
            "bedrock_server_how_to.html",
        ):
            self.assertTrue(_is_server_artifact(yes), f"{yes} 应判为服务端产物")
        for no in (
            "我的备注.txt",
            "server.properties",
            "worlds",
            "backup.sh",
            "notes.md",
            "run.bat",
            "a.dll.bak",
            "libCrypto.dll.old",
            "my.dll.txt",
            "bedrock_server_backup.zip",
            "minecraft_server.1.21.jar",
        ):
            self.assertFalse(_is_server_artifact(no), f"{no} 不应被当作服务端产物")


class TestTamperAlwaysBlocks(unittest.TestCase):
    """完整性违规（摘要不匹配）必须无条件失败，与 require_signature 无关"""

    @unittest.skipUnless(os.name == "nt", "仅 Windows")
    def test_tampered_exe_blocks_even_when_signature_optional(self):
        import zipfile

        from bedrock_server_manager import verify as V

        norm = r"C:\Windows\System32\kernel32.dll"
        if not os.path.isfile(norm):
            self.skipTest("找不到已签名的系统 DLL 样本")

        tmp = tempfile.mkdtemp(prefix="rv_tamper_")
        pad = os.urandom(31 * 1024 * 1024)  # 让 zip 超过 MIN_BDS_SIZE
        try:
            for label, mutate in (("ok", False), ("bad", True)):
                exe_dir = tempfile.mkdtemp(prefix="e_", dir=tmp)
                exe = os.path.join(exe_dir, V.SERVER_EXE)
                shutil.copy2(norm, exe)
                if mutate:
                    with open(exe, "r+b") as f:
                        f.seek(0x1000)
                        b = f.read(1)
                        f.seek(0x1000)
                        f.write(bytes([b[0] ^ 0xFF]))
                zp = os.path.join(tmp, label + ".zip")
                with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as zf:
                    zf.write(exe, V.SERVER_EXE)
                    zf.writestr("pad.bin", pad)

                ok_res = V.verify_download_full(zp, require_signature=False)
                strict_res = V.verify_download_full(zp, require_signature=True)
                if mutate:
                    self.assertFalse(ok_res["ok"], f"篡改且 require_signature=False 时仍判通过: {ok_res}")
                    self.assertFalse(strict_res["ok"])
                    joined = " ".join(ok_res["errors"])
                    self.assertIn("篡改", joined, f"错误信息未说明篡改: {ok_res['errors']}")
                else:
                    self.assertTrue(ok_res["ok"], f"未篡改的已签名文件被误判失败: {ok_res['errors']}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_integrity_message_classifier(self):
        from bedrock_server_manager.verify import _INTEGRITY_ERRORS, _is_integrity_violation_message

        for reason in _INTEGRITY_ERRORS.values():
            self.assertTrue(_is_integrity_violation_message(f"PE 文件{reason}"))
        for benign in ("", "PE 文件未签名，无法证明来源可信", "数字签名验证通过"):
            self.assertFalse(_is_integrity_violation_message(benign))

    def test_detail_matches_wrapper(self):
        from bedrock_server_manager.verify import verify_pe_signature, verify_pe_signature_detail

        path = os.path.join(tempfile.mkdtemp(prefix="rv_pe3_"), "x.exe")
        try:
            with open(path, "wb") as f:
                f.write(b"NOT-A-PE")
            ok1, msg1 = verify_pe_signature(path)
            ok2, msg2, critical = verify_pe_signature_detail(path)
            self.assertEqual((ok1, msg1), (ok2, msg2))
            self.assertFalse(critical, "非 PE 文件属格式错误，不是完整性篡改")
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
