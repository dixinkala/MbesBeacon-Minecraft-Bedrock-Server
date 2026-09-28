"""
verify 模块单元测试 - Minecraft 基岩版服务器管理器
覆盖 SHA256 计算、文件大小校验、ZIP 完整性校验等核心功能
"""

import hashlib
import json
import os
import tempfile
import unittest
import unittest.mock as mock
import zipfile

from bedrock_server_manager import verify as verify_module
from bedrock_server_manager.verify import (
    calculate_sha256,
    verify_file_size,
    verify_zip_integrity,
)


class TestCalculateSHA256(unittest.TestCase):
    """SHA256 计算测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "test.txt")
        self.test_content = b"Hello, World! This is a test file for SHA256 calculation."
        with open(self.test_file, "wb") as f:
            f.write(self.test_content)
        self.expected_hash = hashlib.sha256(self.test_content).hexdigest()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_calculate_sha256_valid_file(self):
        """测试计算有效文件的 SHA256"""
        result = calculate_sha256(self.test_file)
        self.assertEqual(result, self.expected_hash)

    def test_calculate_sha256_empty_file(self):
        """测试计算空文件的 SHA256"""
        empty_file = os.path.join(self.tmpdir, "empty.txt")
        with open(empty_file, "wb"):
            pass
        result = calculate_sha256(empty_file)
        self.assertEqual(result, hashlib.sha256(b"").hexdigest())

    def test_calculate_sha256_nonexistent_file(self):
        """测试计算不存在文件的 SHA256"""
        result = calculate_sha256(os.path.join(self.tmpdir, "nonexistent.txt"))
        self.assertIsNone(result)


class TestVerifyFileSize(unittest.TestCase):
    """文件大小校验测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "test.zip")
        self.test_content = b"x" * 1024  # 1KB
        with open(self.test_file, "wb") as f:
            f.write(self.test_content)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_verify_file_size_valid(self):
        """测试校验有效文件大小"""
        ok, msg, size = verify_file_size(self.test_file, min_size=100, max_size=100000)
        self.assertTrue(ok)
        self.assertEqual(size, 1024)

    def test_verify_file_size_too_small(self):
        """测试校验过小文件"""
        ok, msg, size = verify_file_size(self.test_file, min_size=10000, max_size=100000)
        self.assertFalse(ok)

    def test_verify_file_size_too_large(self):
        """测试校验过大文件"""
        ok, msg, size = verify_file_size(self.test_file, min_size=100, max_size=500)
        self.assertFalse(ok)

    def test_verify_file_size_nonexistent(self):
        """测试校验不存在文件"""
        ok, msg, size = verify_file_size(os.path.join(self.tmpdir, "nonexistent.zip"))
        self.assertFalse(ok)
        self.assertEqual(size, 0)


class TestVerifyZipIntegrity(unittest.TestCase):
    """ZIP 完整性校验测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.valid_zip = os.path.join(self.tmpdir, "valid.zip")
        with zipfile.ZipFile(self.valid_zip, "w") as zf:
            zf.writestr("test.txt", "Hello, World!")
            zf.writestr("subdir/test2.txt", "Another file")

        self.invalid_zip = os.path.join(self.tmpdir, "invalid.zip")
        with open(self.invalid_zip, "wb") as f:
            f.write(b"This is not a valid ZIP file")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_verify_valid_zip(self):
        """测试校验有效 ZIP 文件"""
        ok, msg = verify_zip_integrity(self.valid_zip)
        self.assertTrue(ok)

    def test_verify_invalid_zip(self):
        """测试校验无效 ZIP 文件"""
        ok, msg = verify_zip_integrity(self.invalid_zip)
        self.assertFalse(ok)

    def test_verify_nonexistent_zip(self):
        """测试校验不存在 ZIP 文件"""
        ok, msg = verify_zip_integrity(os.path.join(self.tmpdir, "nonexistent.zip"))
        self.assertFalse(ok)


class TestHashCache(unittest.TestCase):
    """本地哈希缓存路径与旧路径迁移测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.new_path = os.path.join(self.tmpdir, "cache", "hashes.json")
        self.legacy_path = os.path.join(self.tmpdir, "legacy_hashes.json")
        self._patch = mock.patch.multiple(
            verify_module,
            _HASH_CACHE_FILE=self.new_path,
            _LEGACY_HASH_CACHE_FILE=self.legacy_path,
        )
        self._patch.start()
        verify_module._KNOWN_HASHES.clear()

    def tearDown(self):
        self._patch.stop()
        verify_module._KNOWN_HASHES.clear()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_register_known_hash_writes_new_path(self):
        """注册哈希后应写入统一的新路径缓存文件"""
        verify_module.register_known_hash("1.21.0", "abc123")
        self.assertTrue(os.path.isfile(self.new_path))
        with open(self.new_path, encoding="utf-8") as f:
            cache = json.load(f)
        self.assertEqual(cache.get("1.21.0"), "abc123")

    def test_get_known_hash_roundtrip(self):
        """注册后可读回，且内存与文件缓存一致"""
        verify_module.register_known_hash("1.21.0", "abc123")
        self.assertEqual(verify_module.get_known_hash("1.21.0"), "abc123")
        verify_module._KNOWN_HASHES.clear()
        # 清空内存后仍能从新路径缓存读回
        self.assertEqual(verify_module.get_known_hash("1.21.0"), "abc123")

    def test_load_migrates_legacy_cache(self):
        """新路径不存在、旧路径存在时，应读取旧缓存并迁移到新路径"""
        with open(self.legacy_path, "w", encoding="utf-8") as f:
            json.dump({"1.20.0": "def456"}, f)
        cache = verify_module._load_hash_cache()
        self.assertEqual(cache.get("1.20.0"), "def456")
        self.assertTrue(os.path.isfile(self.new_path))  # 已迁移到新路径
        self.assertFalse(os.path.isfile(self.legacy_path))  # 旧文件已清理

    def test_load_prefers_new_path(self):
        """新旧路径同时存在时，应优先读取新路径"""
        os.makedirs(os.path.dirname(self.new_path), exist_ok=True)
        with open(self.new_path, "w", encoding="utf-8") as f:
            json.dump({"1.22.0": "newhash"}, f)
        with open(self.legacy_path, "w", encoding="utf-8") as f:
            json.dump({"1.20.0": "oldhash"}, f)
        cache = verify_module._load_hash_cache()
        self.assertEqual(cache.get("1.22.0"), "newhash")
        self.assertNotIn("1.20.0", cache)
        self.assertTrue(os.path.isfile(self.legacy_path))  # 旧文件未被误删

    def test_load_no_cache_files(self):
        """无任何缓存文件时返回空字典"""
        self.assertEqual(verify_module._load_hash_cache(), {})


if __name__ == "__main__":
    unittest.main()
