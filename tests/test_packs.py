"""
包管理功能测试：资源包/行为包的列表、启用/禁用、导入、删除。

测试覆盖：
- list_installed_packs：解析 manifest.json / valid_known_packs.json / 世界启用清单
- set_pack_enabled：启用写入活动世界 world_*_packs.json 并登记 valid_known_packs.json；禁用移除
- import_pack：正常导入（含单根目录 ZIP）、路径遍历防护、无效 ZIP 拒绝、大小上限
- delete_pack：删除目录并清理已知包列表与世界配置；路径边界校验
"""

import base64
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile

import bedrock_server_manager.packs as packs


def _make_server(tmpdir: str) -> str:
    """构造一个带活动世界与 server.properties 的最小服务器目录。"""
    server_dir = os.path.join(tmpdir, "server")
    os.makedirs(os.path.join(server_dir, "worlds", "Bedrock level"))
    with open(os.path.join(server_dir, "server.properties"), "w", encoding="utf-8") as f:
        f.write("level-name=Bedrock level\n")
    return server_dir


def _make_pack(pack_root: str, pack_name: str, pack_id: str, version=None) -> str:
    """在 pack_root 下创建带 manifest.json 的包目录。"""
    version = version or [1, 0, 0]
    pack_dir = os.path.join(pack_root, pack_name)
    os.makedirs(pack_dir, exist_ok=True)
    manifest = {
        "format_version": 2,
        "header": {"name": pack_name, "pack_id": pack_id, "version": version},
        "modules": [{"type": "data", "uuid": "00000000-0000-0000-0000-000000000001", "version": version}],
    }
    with open(os.path.join(pack_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return pack_dir


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestListInstalledPacks(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mbes_packs_list_")
        self.server = _make_server(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_server_returns_empty_lists(self):
        result = packs.list_installed_packs(self.server)
        self.assertEqual(result["resource_packs"], [])
        self.assertEqual(result["behavior_packs"], [])
        self.assertEqual(result["active_world"], "Bedrock level")

    def test_lists_packs_with_manifest_metadata(self):
        _make_pack(os.path.join(self.server, "resource_packs"), "MyRes", "aaaa1111-0000-0000-0000-000000000001")
        _make_pack(
            os.path.join(self.server, "behavior_packs"), "MyBeh", "bbbb2222-0000-0000-0000-000000000002", [2, 1, 0]
        )
        result = packs.list_installed_packs(self.server)
        res = result["resource_packs"]
        beh = result["behavior_packs"]
        self.assertEqual(len(res), 1)
        self.assertEqual(len(beh), 1)
        self.assertEqual(res[0]["name"], "MyRes")
        self.assertEqual(res[0]["pack_id"], "aaaa1111-0000-0000-0000-000000000001")
        self.assertEqual(res[0]["version"], "1.0.0")
        self.assertFalse(res[0]["enabled"])
        self.assertFalse(res[0]["known"])
        self.assertEqual(beh[0]["version"], "2.1.0")

    def test_enabled_state_from_world_config(self):
        _make_pack(os.path.join(self.server, "resource_packs"), "MyRes", "aaaa1111-0000-0000-0000-000000000001")
        world_packs = [{"pack_id": "aaaa1111-0000-0000-0000-000000000001", "version": [1, 0, 0]}]
        path = os.path.join(self.server, "worlds", "Bedrock level", "world_resource_packs.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(world_packs, f)
        result = packs.list_installed_packs(self.server)
        self.assertTrue(result["resource_packs"][0]["enabled"])


class TestSetPackEnabled(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mbes_packs_toggle_")
        self.server = _make_server(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_enable_writes_world_config_and_known_packs(self):
        _make_pack(os.path.join(self.server, "resource_packs"), "MyRes", "aaaa1111-0000-0000-0000-000000000001")
        ok, err = packs.set_pack_enabled(self.server, "resource", "MyRes", True)
        self.assertTrue(ok, err)
        world_packs = _read_json(os.path.join(self.server, "worlds", "Bedrock level", "world_resource_packs.json"))
        self.assertEqual(len(world_packs), 1)
        self.assertEqual(world_packs[0]["pack_id"], "aaaa1111-0000-0000-0000-000000000001")
        valid_known = packs.read_valid_known_packs(self.server)
        self.assertEqual(len(valid_known), 1)
        self.assertEqual(valid_known[0]["pack_id"], "aaaa1111-0000-0000-0000-000000000001")

    def test_disable_removes_from_world_config(self):
        _make_pack(os.path.join(self.server, "resource_packs"), "MyRes", "aaaa1111-0000-0000-0000-000000000001")
        packs.set_pack_enabled(self.server, "resource", "MyRes", True)
        ok, err = packs.set_pack_enabled(self.server, "resource", "MyRes", False)
        self.assertTrue(ok, err)
        world_packs = _read_json(os.path.join(self.server, "worlds", "Bedrock level", "world_resource_packs.json"))
        self.assertEqual(world_packs, [])

    def test_missing_manifest_rejected(self):
        os.makedirs(os.path.join(self.server, "behavior_packs", "NoManifest"), exist_ok=True)
        ok, err = packs.set_pack_enabled(self.server, "behavior", "NoManifest", True)
        self.assertFalse(ok)
        self.assertIn("manifest", err)

    def test_unknown_pack_rejected(self):
        ok, err = packs.set_pack_enabled(self.server, "resource", "NotExist", True)
        self.assertFalse(ok)
        self.assertIn("不存在", err)

    def test_invalid_pack_type_and_name(self):
        _make_pack(os.path.join(self.server, "resource_packs"), "MyRes", "aaaa1111-0000-0000-0000-000000000001")
        ok, _ = packs.set_pack_enabled(self.server, "bogus", "MyRes", True)
        self.assertFalse(ok)
        ok, _ = packs.set_pack_enabled(self.server, "resource", "../evil", True)
        self.assertFalse(ok)


class TestImportPack(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mbes_packs_import_")
        self.server = _make_server(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _zip_bytes(self, files: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        return buf.getvalue()

    def test_import_single_root_dir(self):
        manifest = json.dumps(
            {"header": {"name": "P", "pack_id": "cccc3333-0000-0000-0000-000000000003", "version": [1, 0, 0]}}
        )
        data = self._zip_bytes({"MyPack/manifest.json": manifest, "MyPack/textures/a.png": b"x"})
        ok, err, msg = packs.import_pack(self.server, "resource", base64.b64encode(data).decode(), "MyPack.zip")
        self.assertTrue(ok, err)
        self.assertTrue(os.path.isfile(os.path.join(self.server, "resource_packs", "MyPack", "manifest.json")))
        self.assertIn("默认未启用", msg)

    def test_import_flat_zip_creates_subdir(self):
        manifest = json.dumps(
            {"header": {"name": "P", "pack_id": "cccc3333-0000-0000-0000-000000000003", "version": [1, 0, 0]}}
        )
        data = self._zip_bytes({"manifest.json": manifest, "pack_icon.png": b"x"})
        ok, err, _ = packs.import_pack(self.server, "behavior", base64.b64encode(data).decode(), "FlatPack.mcpack")
        self.assertTrue(ok, err)
        self.assertTrue(os.path.isfile(os.path.join(self.server, "behavior_packs", "FlatPack", "manifest.json")))

    def test_import_path_traversal_rejected(self):
        data = self._zip_bytes({"../../evil/manifest.json": "{}"})
        ok, err, _ = packs.import_pack(self.server, "resource", base64.b64encode(data).decode(), "Evil.zip")
        self.assertFalse(ok)
        self.assertIn("非法路径", err)
        self.assertFalse(os.path.exists(os.path.join(self.server, "resource_packs", "evil")))
        self.assertFalse(os.path.exists(os.path.join(self.server, "evil")))

    def test_import_invalid_zip_rejected(self):
        ok, err, _ = packs.import_pack(self.server, "resource", base64.b64encode(b"not a zip").decode(), "Bad.zip")
        self.assertFalse(ok)
        self.assertIn("ZIP", err)

    def test_import_without_manifest_rejected(self):
        data = self._zip_bytes({"readme.txt": "hello"})
        ok, err, _ = packs.import_pack(self.server, "resource", base64.b64encode(data).decode(), "NoManifest.zip")
        self.assertFalse(ok)
        self.assertIn("manifest", err)

    def test_import_missing_data_rejected(self):
        ok, err, _ = packs.import_pack(self.server, "resource", "", "x.zip")
        self.assertFalse(ok)
        self.assertIn("文件数据", err)


class TestDeletePack(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mbes_packs_delete_")
        self.server = _make_server(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_delete_removes_dir_and_cleans_configs(self):
        _make_pack(os.path.join(self.server, "resource_packs"), "MyRes", "aaaa1111-0000-0000-0000-000000000001")
        packs.set_pack_enabled(self.server, "resource", "MyRes", True)
        ok, err = packs.delete_pack(self.server, "resource", "MyRes")
        self.assertTrue(ok, err)
        self.assertFalse(os.path.exists(os.path.join(self.server, "resource_packs", "MyRes")))
        self.assertEqual(packs.read_valid_known_packs(self.server), [])
        world_packs = _read_json(os.path.join(self.server, "worlds", "Bedrock level", "world_resource_packs.json"))
        self.assertEqual(world_packs, [])

    def test_delete_missing_pack_rejected(self):
        ok, err = packs.delete_pack(self.server, "resource", "NotExist")
        self.assertFalse(ok)
        self.assertIn("不存在", err)

    def test_delete_path_traversal_rejected(self):
        ok, err = packs.delete_pack(self.server, "resource", "../outside")
        self.assertFalse(ok)
        self.assertNotIn("outside", os.listdir(self.tmpdir))


class TestPacksRoutesDispatch(unittest.TestCase):
    """HTTP 层分发测试：do_GET/do_POST 正确路由到 packs 方法。"""

    def _make_handler(self, body=None):
        from unittest.mock import MagicMock

        from bedrock_server_manager.web.handler import Handler

        handler = Handler.__new__(Handler)
        handler._json = MagicMock()
        handler._send = MagicMock()
        handler._post_data = MagicMock(return_value=body or {})
        handler._check_auth = MagicMock(return_value=True)
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler.rfile = MagicMock()
        handler.wfile = MagicMock()
        return handler

    def _run_post(self, handler, path, server_dir=None):
        from unittest.mock import patch
        from urllib.parse import ParseResult

        handler.path = path
        with (
            patch("bedrock_server_manager.web.handler.urlparse") as m,
            patch("bedrock_server_manager.web.routes.packs.resolve_server_dir", return_value=server_dir),
        ):
            m.return_value = ParseResult(
                scheme="http", netloc="127.0.0.1:19100", path=path, params="", query="", fragment=""
            )
            handler.do_POST()
        return handler._json.call_args[0][0]

    def test_get_packs_list_dispatch(self):
        from unittest.mock import patch
        from urllib.parse import ParseResult

        from bedrock_server_manager.web.handler import Handler

        handler = self._make_handler()
        handler.path = "/api/packs"
        with (
            patch("bedrock_server_manager.web.handler.urlparse") as m,
            patch(
                "bedrock_server_manager.web.routes.packs.list_installed_packs",
                return_value={"resource_packs": [], "behavior_packs": [], "valid_known": [], "active_world": ""},
            ),
        ):
            m.return_value = ParseResult(
                scheme="http", netloc="127.0.0.1:19100", path="/api/packs", params="", query="", fragment=""
            )
            handler.do_GET()
        resp = handler._json.call_args[0][0]
        self.assertTrue(resp["ok"])
        self.assertIn("packs", resp)
        self.assertTrue(hasattr(Handler, "_route_packs_list"))

    def test_toggle_requires_server_dir(self):
        resp = self._run_post(
            self._make_handler({"pack_type": "resource", "name": "P", "enabled": True}), "/api/packs/toggle"
        )
        self.assertFalse(resp["ok"])
        self.assertIn("目录", resp["error"])

    def test_toggle_invalid_pack_type(self):
        resp = self._run_post(
            self._make_handler({"pack_type": "bogus", "name": "P", "enabled": True}),
            "/api/packs/toggle",
            server_dir="C:\\fake",
        )
        self.assertFalse(resp["ok"])
        self.assertIn("包类型", resp["error"])

    def test_toggle_missing_name(self):
        resp = self._run_post(
            self._make_handler({"pack_type": "resource", "enabled": True}),
            "/api/packs/toggle",
            server_dir="C:\\fake",
        )
        self.assertFalse(resp["ok"])
        self.assertIn("包名", resp["error"])

    def test_delete_requires_confirmation(self):
        resp = self._run_post(
            self._make_handler({"pack_type": "resource", "name": "P"}), "/api/packs/delete", server_dir="C:\\fake"
        )
        self.assertFalse(resp["ok"])
        self.assertTrue(resp.get("need_confirm"))
        self.assertEqual(resp.get("confirm_value"), "DELETE")

    def test_import_rejects_empty_data(self):
        resp = self._run_post(
            self._make_handler({"pack_type": "resource", "file_data": "", "file_name": "x.zip"}),
            "/api/packs/import",
            server_dir="C:\\fake",
        )
        self.assertFalse(resp["ok"])


if __name__ == "__main__":
    unittest.main()
