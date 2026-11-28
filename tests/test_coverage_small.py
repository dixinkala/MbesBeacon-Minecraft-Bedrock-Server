"""补齐小模块的边界分支：版本清单、世界校验、包工具函数、备份入口。

这些模块此前已接近门槛，这里只补 85%/80% 还缺的那几条分支。
"""

import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from bedrock_server_manager import backup, packs, version_sources, worlds


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestVersionSources(unittest.TestCase):
    def test_malformed_manifest_is_skipped_then_success(self):
        bad = _FakeResponse(json.dumps({"note": "no platforms"}).encode())
        good = _FakeResponse(json.dumps({"windows": {"stable": "1.21.0.03", "versions": ["1.21.0.03"]}}).encode())
        with mock.patch.object(version_sources.urllib.request, "urlopen", side_effect=[bad, good]):
            manifest, url = version_sources.fetch_manifest(urls=["https://a/manifest.json", "https://b/manifest.json"])
        self.assertEqual(url, "https://b/manifest.json")
        self.assertIn("windows", manifest)

    def test_all_sources_fail_returns_empty(self):
        with mock.patch.object(
            version_sources.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("boom"),
        ):
            manifest, url = version_sources.fetch_manifest(urls=["https://a/manifest.json"], timeout=1)
        self.assertEqual(manifest, {})
        self.assertEqual(url, "")

    def test_generic_exception_is_swallowed(self):
        with mock.patch.object(version_sources.urllib.request, "urlopen", side_effect=ValueError("weird")):
            manifest, url = version_sources.fetch_manifest(urls=["https://a/manifest.json"])
        self.assertEqual((manifest, url), ({}, ""))

    def test_ignore_ssl_disables_verification(self):
        seen = {}

        def fake_urlopen(req, timeout=None, context=None):
            seen["check_hostname"] = context.check_hostname
            seen["verify_mode"] = context.verify_mode
            return _FakeResponse(json.dumps({"windows": {"stable": "1.0"}}).encode())

        with mock.patch.object(version_sources.urllib.request, "urlopen", side_effect=fake_urlopen):
            version_sources.fetch_manifest(urls=["https://a"], ignore_ssl=True)
        self.assertFalse(seen["check_hostname"])

    def test_builtin_manifest_is_deep_copy(self):
        first = version_sources.builtin_manifest()
        first.setdefault("windows", {})["stable"] = "tampered"
        second = version_sources.builtin_manifest()
        self.assertNotEqual(second.get("windows", {}).get("stable"), "tampered")
        self.assertTrue(version_sources.builtin_snapshot_date())

    def test_platform_info_invalid_payload(self):
        self.assertEqual(version_sources.platform_info({}), {})
        self.assertEqual(version_sources.platform_info({"windows": "not-a-dict"}), {})

    def test_manifest_stats_counts(self):
        stats = version_sources.manifest_stats(
            {"windows": {"stable": "1.0", "versions": [1, 2], "preview_versions": [3]}, "linux": None}
        )
        self.assertEqual(stats["windows"]["versions"], 2)
        self.assertEqual(stats["windows"]["preview_versions"], 1)
        self.assertEqual(stats["linux"]["stable"], "")


class TestWorldValidationExtra(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = os.path.join(self.tmp.name, "srv")
        os.makedirs(os.path.join(self.server, "worlds", "Alpha"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_control_character_rejected(self):
        ok, err = worlds.validate_world_name("bad\x01name", self.server)
        self.assertFalse(ok)
        self.assertTrue(err)

    def test_trailing_dot_and_reserved_name_rejected(self):
        self.assertFalse(worlds.validate_world_name("World.", self.server)[0])
        self.assertFalse(worlds.validate_world_name("CON", self.server)[0])
        self.assertFalse(worlds.validate_world_name("con.txt", self.server)[0])

    def test_blank_and_separator_rejected(self):
        self.assertFalse(worlds.validate_world_name("   ", self.server)[0])
        self.assertFalse(worlds.validate_world_name("../escape", self.server)[0])

    def test_list_worlds_handles_missing_dir_and_files(self):
        self.assertEqual(worlds.list_worlds(os.path.join(self.tmp.name, "nope")), [])
        with open(os.path.join(self.server, "worlds", "stray.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        names = [item["name"] for item in worlds.list_worlds(self.server)]
        self.assertEqual(names, ["Alpha"])

    def test_get_world_info_missing(self):
        self.assertIsNone(worlds.get_world_info(self.server, "Nope"))

    def test_active_world_helpers_without_properties(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.makedirs(os.path.join(empty, "worlds"))
        self.assertEqual(worlds.get_active_world_name(empty), "Bedrock level")
        ok, _err = worlds.set_active_world(empty, "Missing")
        self.assertFalse(ok)

    def test_validate_without_server_dir(self):
        self.assertTrue(worlds.validate_world_name("Alpha")[0])


class TestPackHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = os.path.join(self.tmp.name, "srv")
        os.makedirs(self.server)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_json_corrupted_returns_default(self):
        path = os.path.join(self.server, "broken.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(packs._read_json(path, []), [])
        self.assertEqual(packs._read_json("", {"a": 1}), {"a": 1})
        self.assertEqual(packs._read_json(os.path.join(self.server, "missing.json"), 7), 7)

    def test_write_json_failure_returns_false(self):
        blocker = os.path.join(self.server, "file")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        self.assertFalse(packs._write_json(os.path.join(blocker, "nested.json"), {"a": 1}))

    def test_validate_pack_name_edges(self):
        self.assertFalse(packs._validate_pack_name("")[0])
        self.assertFalse(packs._validate_pack_name("..")[0])
        self.assertFalse(packs._validate_pack_name("a" * 81)[0])
        self.assertFalse(packs._validate_pack_name("bad<>")[0])
        self.assertTrue(packs._validate_pack_name("Good Pack (1)")[0])

    def test_sanitize_component_edges(self):
        self.assertEqual(packs._sanitize_path_component(".."), "")
        self.assertEqual(packs._sanitize_path_component("Bad<Root>"), "Bad_Root_")

    def test_normalize_pack_type(self):
        self.assertIsNotNone(packs._normalize_pack_type("Resource"))
        self.assertIsNone(packs._normalize_pack_type("unknown"))

    def test_list_installed_packs_without_dirs(self):
        result = packs.list_installed_packs(self.server)
        self.assertEqual(result["resource_packs"], [])
        self.assertEqual(result["behavior_packs"], [])
        self.assertFalse(packs.set_pack_enabled(self.server, "resource", "Missing", True)[0])


class TestBackupEntryPoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = os.path.join(self.tmp.name, "srv")
        os.makedirs(self.server)

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_without_worlds_returns_empty(self):
        self.assertEqual(backup.backup_worlds(self.server), "")
        self.assertIsNone(backup.backup_worlds_zip(self.server))
        self.assertEqual(backup.list_backups(self.server), [])

    def test_backup_worlds_swallows_copy_errors(self):
        os.makedirs(os.path.join(self.server, "worlds", "Alpha"))
        with mock.patch.object(backup.shutil, "copytree", side_effect=OSError("disk full")):
            self.assertEqual(backup.backup_worlds(self.server), "")

    def test_cleanup_handles_missing_root(self):
        backup._cleanup_old_backups(os.path.join(self.tmp.name, "nope"), 2, "srv")


if __name__ == "__main__":
    unittest.main(verbosity=2)
