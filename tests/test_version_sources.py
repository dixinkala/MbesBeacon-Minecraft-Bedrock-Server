"""版本清单来源链与内置快照的回归测试。

对应待办第 4 项：版本列表使用自建仓库（不再依赖 Bedrock-OSS），
获取失败有两层兜底（社区清单 -> 内置快照），内置快照需与清单同步。
"""

import importlib.util
import json
import pathlib
import re
import unittest
from unittest.mock import patch

from bedrock_server_manager import install, verify, version_sources
from bedrock_server_manager.builtin_versions import BUILTIN_VERSIONS, BUILTIN_VERSIONS_SNAPSHOT
from bedrock_server_manager.constants import (
    BDS_VERSIONS_JSON_URL,
    FALLBACK_VERSIONS_JSON_URL,
    VERSIONS_JSON_URL,
)

REPO = pathlib.Path(__file__).resolve().parent.parent


class TestSourceChainConfig(unittest.TestCase):
    def test_primary_is_self_hosted(self):
        self.assertIn("dixinkala/mbesbeacon-bds-versions", VERSIONS_JSON_URL)

    def test_community_is_only_fallback(self):
        self.assertIn("Bedrock-OSS/BDS-Versions", FALLBACK_VERSIONS_JSON_URL)
        self.assertEqual(BDS_VERSIONS_JSON_URL, FALLBACK_VERSIONS_JSON_URL, "旧名应等于兜底地址")

    def test_order_is_primary_then_fallback(self):
        self.assertEqual(version_sources.MANIFEST_URLS, (VERSIONS_JSON_URL, FALLBACK_VERSIONS_JSON_URL))


class TestFetchManifestOrder(unittest.TestCase):
    """按顺序尝试来源，前一个失败才用后一个"""

    def _response(self, payload):
        class _Resp:
            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Resp(json.dumps(payload).encode("utf-8"))

    def test_primary_success_skips_fallback(self):
        payload = {"windows": {"stable": "1.0.0.1"}, "linux": {"stable": "1.0.0.1"}}
        urls = []

        def fake_urlopen(req, timeout=None, context=None):
            urls.append(req.full_url)
            return self._response(payload)

        with patch.object(version_sources.urllib.request, "urlopen", side_effect=fake_urlopen):
            data, source = version_sources.fetch_manifest(urls=("https://primary/x", "https://fallback/x"))
        self.assertEqual(source, "https://primary/x")
        self.assertEqual(urls, ["https://primary/x"])
        self.assertEqual(data["windows"]["stable"], "1.0.0.1")

    def test_falls_back_when_primary_fails(self):
        payload = {"windows": {"stable": "1.0.0.2"}, "linux": {"stable": "1.0.0.2"}}

        def fake_urlopen(req, timeout=None, context=None):
            if "primary" in req.full_url:
                raise OSError("boom")
            return self._response(payload)

        with patch.object(version_sources.urllib.request, "urlopen", side_effect=fake_urlopen):
            data, source = version_sources.fetch_manifest(urls=("https://primary/x", "https://fallback/x"))
        self.assertEqual(source, "https://fallback/x")
        self.assertEqual(data["windows"]["stable"], "1.0.0.2")

    def test_all_sources_fail_returns_empty(self):
        with patch.object(version_sources.urllib.request, "urlopen", side_effect=OSError("offline")):
            data, source = version_sources.fetch_manifest(urls=("https://a/x", "https://b/x"))
        self.assertEqual(data, {})
        self.assertEqual(source, "")

    def test_malformed_manifest_is_skipped(self):
        payload = {"unexpected": True}

        def fake_urlopen(req, timeout=None, context=None):
            if "primary" in req.full_url:
                return self._response(payload)
            return self._response({"windows": {"stable": "1.0.0.3"}, "linux": {"stable": "1.0.0.3"}})

        with patch.object(version_sources.urllib.request, "urlopen", side_effect=fake_urlopen):
            data, source = version_sources.fetch_manifest(urls=("https://primary/x", "https://fallback/x"))
        self.assertEqual(source, "https://fallback/x")
        self.assertEqual(data["windows"]["stable"], "1.0.0.3")


class TestBuiltinSnapshot(unittest.TestCase):
    def test_snapshot_date_format(self):
        self.assertRegex(BUILTIN_VERSIONS_SNAPSHOT, r"^\d{4}-\d{2}-\d{2}$")

    def test_platforms_present_and_sorted(self):
        for platform in ("windows", "linux"):
            info = BUILTIN_VERSIONS[platform]
            self.assertTrue(info["versions"], f"{platform} 内置版本为空")
            self.assertIn(info["stable"], info["versions"])
            self.assertEqual(info["versions"], sorted(info["versions"], key=self._key))
            self.assertEqual(info["preview_versions"], sorted(info["preview_versions"], key=self._key))

    @staticmethod
    def _key(version):
        return tuple(int(p) for p in version.split("."))

    def test_recent_versions_included(self):
        """内置快照不能落后：至少包含 1.26.x 的版本"""
        self.assertTrue(any(v.startswith("1.26.") for v in BUILTIN_VERSIONS["windows"]["versions"]))


class TestInstallFallbacks(unittest.TestCase):
    def test_manifest_used_when_available(self):
        payload = {
            "windows": {"stable": "1.0.0.9", "preview": "", "versions": ["1.0.0.8", "1.0.0.9"]},
            "linux": {"stable": "1.0.0.9", "preview": "", "versions": ["1.0.0.9"]},
        }
        with patch.object(version_sources.urllib.request, "urlopen") as mocked:
            mocked.return_value = type(
                "R",
                (),
                {
                    "read": lambda self: json.dumps(payload).encode(),
                    "__enter__": lambda s: s,
                    "__exit__": lambda *a: False,
                },
            )()
            stable, preview, source = install.fetch_versions_from_manifest()
        self.assertEqual(stable[:2], ["1.0.0.9", "1.0.0.8"])
        self.assertEqual(preview, [])
        self.assertTrue(source)

    def test_builtin_used_when_all_online_sources_fail(self):
        with (
            patch.object(install, "fetch_versions_from_manifest", return_value=([], [], "")),
            patch.object(install, "_http_get", side_effect=OSError("offline")),
        ):
            versions = install.get_bedrock_versions(include_preview=True)
        expected = set(BUILTIN_VERSIONS[version_sources.platform_key()]["versions"]) | set(
            BUILTIN_VERSIONS[version_sources.platform_key()]["preview_versions"]
        )
        self.assertEqual(set(versions), expected)

    def test_fallback_versions_come_from_snapshot(self):
        expected = sorted(
            set(BUILTIN_VERSIONS[version_sources.platform_key()]["versions"]),
            key=install._version_key,
            reverse=True,
        )
        self.assertEqual(install.FALLBACK_VERSIONS, expected)


class TestVerifyFallbacks(unittest.TestCase):
    def setUp(self):
        verify._OFFICIAL_VERSIONS_CACHE.update({"data": None, "time": 0.0, "failed_at": 0.0, "builtin": False})

    def test_builtin_used_when_manifest_unreachable(self):
        with patch.object(verify, "fetch_manifest", return_value=({}, "")):
            result = verify.fetch_official_versions(force=True)
        self.assertEqual(result["windows"]["stable"], BUILTIN_VERSIONS["windows"]["stable"])
        self.assertTrue(verify._OFFICIAL_VERSIONS_CACHE.get("builtin"))

    def test_known_check_works_offline(self):
        with patch.object(verify, "fetch_manifest", return_value=({}, "")):
            verify.fetch_official_versions(force=True)
            stable = BUILTIN_VERSIONS["windows"]["stable"]
            self.assertTrue(verify.get_official_version_info(stable)["known"])
            self.assertFalse(verify.get_official_version_info("9.9.9.9")["known"])

    def test_online_result_marks_not_builtin(self):
        payload = {"windows": {"stable": "1.0.0.5", "versions": ["1.0.0.5"]}, "linux": {"stable": "1.0.0.5"}}
        with patch.object(verify, "fetch_manifest", return_value=(payload, "https://primary/x")):
            result = verify.fetch_official_versions(force=True)
        self.assertEqual(result["windows"]["stable"], "1.0.0.5")
        self.assertFalse(verify._OFFICIAL_VERSIONS_CACHE.get("builtin"))


class TestFrontendFallbackList(unittest.TestCase):
    def test_frontend_uses_snapshot_versions(self):
        html = (REPO / "bedrock_server_manager" / "web" / "index.html").read_text(encoding="utf-8")
        match = re.search(r"const FALLBACK_VERSIONS=\[(.*?)\];", html, re.S)
        self.assertIsNotNone(match, "未找到 FALLBACK_VERSIONS")
        versions = re.findall(r'"([0-9.]+)"', match.group(1))
        self.assertGreaterEqual(len(versions), 10)
        snapshot = set(BUILTIN_VERSIONS["windows"]["versions"])
        for version in versions:
            self.assertIn(version, snapshot, f"前台兜底版本 {version} 不在内置快照中")
        self.assertEqual(versions[0], BUILTIN_VERSIONS["windows"]["versions"][-1], "应以最新版本开头")

    def test_frontend_text_mentions_self_hosted_manifest(self):
        html = (REPO / "bedrock_server_manager" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("来自自建版本清单", html)
        self.assertNotIn("Bedrock-OSS 社区仓库获取", html)


class TestDocsMentionSelfHosted(unittest.TestCase):
    def test_wiki_install_page(self):
        text = (REPO / "docs" / "wiki" / "01-install-update.md").read_text(encoding="utf-8")
        self.assertIn("自建清单仓库", text)
        self.assertIn("mbesbeacon-bds-versions", text)

    def test_wiki_faq(self):
        text = (REPO / "docs" / "wiki" / "12-faq.md").read_text(encoding="utf-8")
        self.assertIn("自建版本清单", text)

    def test_readme(self):
        for name in ("README.md", "README_EN.md"):
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertIn("mbesbeacon-bds-versions", text, f"{name} 未提到自建清单仓库")


class TestSyncTool(unittest.TestCase):
    """tools/sync_builtin_versions.py 必须能渲染出当前快照"""

    def test_render_matches_committed_snapshot(self):
        module_path = REPO / "tools" / "sync_builtin_versions.py"
        spec = importlib.util.spec_from_file_location("sync_builtin_versions", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manifest = {
            "windows": BUILTIN_VERSIONS["windows"],
            "linux": BUILTIN_VERSIONS["linux"],
        }
        rendered = module.render(manifest)
        committed = (REPO / "bedrock_server_manager" / "builtin_versions.py").read_text(encoding="utf-8")
        # 快照日期是生成当天，比较时忽略这一行
        strip = lambda text: re.sub(r'BUILTIN_VERSIONS_SNAPSHOT = "[^"]*"', "", text)  # noqa: E731
        self.assertEqual(strip(rendered), strip(committed))


if __name__ == "__main__":
    unittest.main(verbosity=2)
