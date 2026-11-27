"""tools/sync_wiki.py 的单元测试：页面名映射、链接转换、侧边栏与 dry-run。"""

import contextlib
import importlib.util
import io
import pathlib
import re
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "tools" / "sync_wiki.py"

spec = importlib.util.spec_from_file_location("sync_wiki", MODULE_PATH)
sync_wiki = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_wiki)

BLOB = "https://github.com/owner/repo/blob/main"


class TestPageName(unittest.TestCase):
    def test_readme_maps_to_home(self):
        self.assertEqual(sync_wiki.page_name("README.md"), "Home")

    def test_chapter_keeps_slug(self):
        self.assertEqual(sync_wiki.page_name("01-install-update.md"), "01-install-update")


class TestConvertLinks(unittest.TestCase):
    def test_readme_link_becomes_home(self):
        self.assertEqual(sync_wiki.convert_links("[目录](README.md)", BLOB), "[目录](Home)")

    def test_page_link_drops_extension(self):
        self.assertEqual(sync_wiki.convert_links("[配置](02-config.md)", BLOB), "[配置](02-config)")

    def test_page_link_keeps_anchor(self):
        self.assertEqual(
            sync_wiki.convert_links("[备份](06-backups.md#保留策略)", BLOB),
            "[备份](06-backups#保留策略)",
        )

    def test_repo_file_link_becomes_absolute(self):
        self.assertEqual(
            sync_wiki.convert_links("[许可证](../LICENSE)", BLOB),
            f"[许可证]({BLOB}/LICENSE)",
        )

    def test_unknown_relative_link_untouched(self):
        text = "[外部](https://example.com) [锚点](#目录)"
        self.assertEqual(sync_wiki.convert_links(text, BLOB), text)


class TestBuildSidebar(unittest.TestCase):
    def test_sidebar_lists_home_then_pages(self):
        sidebar = sync_wiki.build_sidebar(["01-a", "02-b"], "https://github.com/owner/repo")
        self.assertIn("- [首页](Home)", sidebar)
        self.assertIn("- [01-a](01-a)", sidebar)
        self.assertIn("- [02-b](02-b)", sidebar)
        self.assertLess(sidebar.index("(Home)"), sidebar.index("(01-a)"))
        self.assertIn("https://github.com/owner/repo/releases/latest", sidebar)


class TestRealDocs(unittest.TestCase):
    """docs/wiki 里的文档转换后不应残留仓库内相对链接"""

    def setUp(self):
        self.src = REPO / "docs" / "wiki"

    def test_no_relative_links_left(self):
        for path in sorted(self.src.glob("*.md")):
            text = sync_wiki.convert_links(path.read_text(encoding="utf-8"), BLOB)
            self.assertNotRegex(text, r"\]\(\.\./", f"{path.name} 仍含仓库相对链接")
            self.assertNotRegex(text, r"\]\(\d{2}-[a-z0-9-]+\.md", f"{path.name} 仍含 .md 页内链接")

    def test_every_link_target_exists_on_github_style(self):
        """页内链接的目标必须是 docs/wiki 里真实存在的页面"""
        pages = {p.stem for p in self.src.glob("*.md")}
        pages.add("Home")
        for path in self.src.glob("*.md"):
            text = sync_wiki.convert_links(path.read_text(encoding="utf-8"), BLOB)
            for target in re.findall(r"\]\((\d{2}-[a-z0-9-]+)(?:#[^)]*)?\)", text):
                self.assertIn(target, pages, f"{path.name} 链接到不存在的页面 {target}")


class TestDryRun(unittest.TestCase):
    def test_dry_run_reports_without_touching_remote(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = sync_wiki.main(["--dry-run"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("目标:", out)
        self.assertIn("README.md", out)
        self.assertIn("12", out)  # 12 章侧边栏条目

    def test_missing_source_dir_returns_error(self):
        original = sync_wiki.SRC_DIR
        sync_wiki.SRC_DIR = REPO / "docs" / "__not_exist__"
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(sync_wiki.main(["--dry-run"]), 2)
        finally:
            sync_wiki.SRC_DIR = original


class TestRemoveTree(unittest.TestCase):
    """同步结束后临时克隆必须删干净（Windows 上句柄释放有延迟）"""

    def test_removes_directory_with_files(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="mbes_wiki_test_"))
        (tmp / "a.txt").write_text("x", encoding="utf-8")
        (tmp / "sub").mkdir()
        (tmp / "sub" / "b.txt").write_text("y", encoding="utf-8")
        sync_wiki.remove_tree(tmp)
        self.assertFalse(tmp.exists())

    def test_missing_path_is_noop(self):
        sync_wiki.remove_tree(pathlib.Path(tempfile.gettempdir()) / "__mbes_no_such_dir__")


class TestOriginUrl(unittest.TestCase):
    def test_origin_points_to_github(self):
        url = sync_wiki.origin_url()
        self.assertTrue(url.startswith("https://github.com/"), url)
        self.assertFalse(url.endswith(".git"), url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
