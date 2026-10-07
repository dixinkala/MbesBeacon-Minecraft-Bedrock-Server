"""
端到端测试 - Minecraft 基岩版服务器管理器
覆盖完整工作流：创建模拟服务器 → API交互 → 配置修改 → 备份 → 玩家管理 → 清理

更新内容：
- 修复 /api/scheduled/list 和 /api/ipban/list 的请求方法（GET 而非 POST）
- 新增世界管理 API 测试
- 新增命令自动补全 API 测试
- 新增版本列表 API 测试
- 新增局域网信息 API 测试
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bedrock_server_manager as bsm


def api_request(method, path, data=None, token=""):
    """发送API请求"""
    url = f"http://127.0.0.1:{bsm.DEFAULT_PORT}{path}"
    headers = {}
    if token:
        headers["X-API-Token"] = token
    if method == "POST":
        body = json.dumps(data or {}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        req.add_header("Content-Type", "application/json")
    else:
        req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return -1, {"error": str(e)}


class TestE2EFullWorkflow(unittest.TestCase):
    """完整工作流端到端测试"""

    @classmethod
    def setUpClass(cls):
        """启动HTTP服务器"""
        cls.tmpdir = tempfile.mkdtemp(prefix="bsm_e2e_")
        cls.server_dir = os.path.join(cls.tmpdir, "test_server")
        os.makedirs(cls.server_dir)

        # 创建模拟服务器文件
        with open(os.path.join(cls.server_dir, "server.properties"), "w", encoding="utf-8") as f:
            f.write("server-name=E2E Test Server\n")
            f.write("gamemode=survival\n")
            f.write("difficulty=normal\n")
            f.write("max-players=10\n")
            f.write("server-port=19132\n")
            f.write("view-distance=32\n")
            f.write("white-list=false\n")
            f.write("online-mode=true\n")

        with open(os.path.join(cls.server_dir, bsm.SERVER_EXE), "wb") as f:
            f.write(b"MZ" + b"\x00" * 100 if bsm.IS_WINDOWS else b"\x7fELF" + b"\x00" * 100)

        # 创建worlds目录
        worlds_dir = os.path.join(cls.server_dir, "worlds", "Bedrock level")
        os.makedirs(worlds_dir)
        with open(os.path.join(worlds_dir, "level.dat"), "wb") as f:
            f.write(b"\x00" * 512)

        # 创建第二个世界用于测试切换
        worlds_dir2 = os.path.join(cls.server_dir, "worlds", "TestWorld")
        os.makedirs(worlds_dir2)
        with open(os.path.join(worlds_dir2, "level.dat"), "wb") as f:
            f.write(b"\x00" * 512)

        # 创建白名单
        with open(os.path.join(cls.server_dir, "allowlist.json"), "w", encoding="utf-8") as f:
            json.dump([{"name": "AdminPlayer", "ignoresPlayerLimit": False}], f)

        # 设置服务器目录
        bsm.settings["server_dir"] = cls.server_dir
        bsm.settings["installed_version"] = "1.21.0.03"

        # 启动HTTP服务器
        cls.httpd = bsm.ThreadingHTTPServer(("127.0.0.1", bsm.DEFAULT_PORT), bsm.Handler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.5)

        cls.token = bsm.API_TOKEN

    @classmethod
    def tearDownClass(cls):
        """关闭HTTP服务器并清理"""
        cls.httpd.shutdown()
        cls.httpd.server_close()
        time.sleep(0.3)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_01_status_endpoint(self):
        """测试状态端点"""
        code, data = api_request("GET", "/api/status", token=self.token)
        self.assertEqual(code, 200)
        self.assertTrue(data["installed"])
        self.assertEqual(data["installed_version"], "1.21.0.03")
        self.assertFalse(data["server_running"])

    def test_02_get_config(self):
        """测试读取配置"""
        code, data = api_request("GET", "/api/config", token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual(data["data"]["server-name"], "E2E Test Server")
        self.assertEqual(data["data"]["max-players"], "10")

    def test_03_save_config(self):
        """测试保存配置"""
        code, data = api_request(
            "POST", "/api/config", {"data": {"max-players": "20", "server-name": "Updated Server"}}, token=self.token
        )
        self.assertEqual(code, 200)
        # 验证已保存
        code, data = api_request("GET", "/api/config", token=self.token)
        self.assertEqual(data["data"]["max-players"], "20")
        self.assertEqual(data["data"]["server-name"], "Updated Server")

    def test_04_save_config_invalid(self):
        """测试保存无效配置（应返回ok:false）"""
        code, data = api_request("POST", "/api/config", {"data": {"max-players": "999"}}, token=self.token)
        self.assertEqual(code, 200)
        self.assertFalse(data.get("ok", True))

    def test_05_whitelist_add(self):
        """测试添加白名单玩家"""
        code, data = api_request("POST", "/api/players/allowlist/add", {"name": "TestPlayer"}, token=self.token)
        self.assertEqual(code, 200)
        # 验证文件已更新
        with open(os.path.join(self.server_dir, "allowlist.json"), encoding="utf-8") as f:
            allowlist = json.load(f)
        self.assertTrue(any(p["name"] == "TestPlayer" for p in allowlist))

    def test_06_whitelist_remove(self):
        """测试移除白名单玩家"""
        code, data = api_request("POST", "/api/players/allowlist/remove", {"name": "TestPlayer"}, token=self.token)
        self.assertEqual(code, 200)
        # 验证文件已更新
        with open(os.path.join(self.server_dir, "allowlist.json"), encoding="utf-8") as f:
            allowlist = json.load(f)
        self.assertFalse(any(p["name"] == "TestPlayer" for p in allowlist))

    def test_07_whitelist_invalid_name(self):
        """测试无效玩家名（应返回ok:false）"""
        code, data = api_request("POST", "/api/players/allowlist/add", {"name": "invalid name!"}, token=self.token)
        self.assertEqual(code, 200)
        self.assertFalse(data.get("ok", True))

    def test_08_backup_worlds(self):
        """测试创建世界备份"""
        code, data = api_request("POST", "/api/backups/create", {}, token=self.token)
        self.assertEqual(code, 200)
        # 验证备份目录存在
        backup_root = os.path.join(self.tmpdir, "_worlds_backups")
        self.assertTrue(os.path.isdir(backup_root))
        backups = [d for d in os.listdir(backup_root) if d.startswith("test_server_worlds_")]
        self.assertGreater(len(backups), 0)

    def test_09_list_backups(self):
        """测试列出备份"""
        code, data = api_request("GET", "/api/backups", token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("backups", data)

    def test_10_console_log(self):
        """测试控制台日志端点"""
        # 通过API发送指令产生日志
        api_request("POST", "/api/cmd", {"cmd": "say e2e test"}, token=self.token)
        code, data = api_request("GET", "/api/console", token=self.token)
        self.assertEqual(code, 200)
        # 验证响应结构（可能是text或lines字段）
        self.assertTrue(
            any(k in data for k in ["text", "lines", "log"]), f"响应应包含日志字段，实际字段: {list(data.keys())}"
        )

    def test_11_theme_get(self):
        """测试获取主题设置"""
        code, data = api_request("GET", "/api/theme", token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("theme", data)

    def test_12_performance(self):
        """测试性能监控端点"""
        code, data = api_request("GET", "/api/performance", token=self.token)
        self.assertEqual(code, 200)

    def test_13_auth_required(self):
        """测试未认证请求被拒绝"""
        code, data = api_request("GET", "/api/status", token="")
        self.assertEqual(code, 403)

    def test_14_scheduled_tasks(self):
        """测试计划任务保存和读取"""
        tasks = [{"id": "test_backup", "type": "backup", "schedule_type": "daily", "time": "04:00", "enabled": True}]
        code, data = api_request("POST", "/api/scheduled/save", {"tasks": tasks}, token=self.token)
        self.assertEqual(code, 200)
        # 修复：使用 GET 请求获取计划任务列表
        code, data = api_request("GET", "/api/scheduled/list", token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual(len(data.get("tasks", [])), 1)

    def test_15_ip_ban(self):
        """测试IP封禁"""
        code, data = api_request("POST", "/api/ipban/add", {"ip": "10.0.0.99", "reason": "e2e test"}, token=self.token)
        self.assertEqual(code, 200)
        # /api/ipban/list 是 POST 路由
        code, data = api_request("POST", "/api/ipban/list", {}, token=self.token)
        self.assertEqual(code, 200)
        self.assertTrue(any(b.get("ip") == "10.0.0.99" for b in data.get("banned", [])))
        # 解除封禁
        code, data = api_request("POST", "/api/ipban/remove", {"ip": "10.0.0.99"}, token=self.token)
        self.assertEqual(code, 200)

    def test_16_health_endpoint(self):
        """测试健康检查端点"""
        # 健康检查端点不需要认证
        code, data = api_request("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("status"), "healthy")
        self.assertIn("version", data)
        self.assertIn("uptime_seconds", data)
        self.assertIn("memory_mb", data)
        self.assertIn("cpu_percent", data)
        self.assertIn("server_running", data)
        self.assertIn("installed", data)

    def test_17_worlds_list(self):
        """测试世界列表端点"""
        code, data = api_request("GET", "/api/worlds/list", token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("worlds", data)
        self.assertGreaterEqual(len(data["worlds"]), 2)  # 应该有两个世界

    def test_18_commands_list(self):
        """测试命令列表端点"""
        code, data = api_request("POST", "/api/commands/list", {}, token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("commands", data)
        self.assertGreater(len(data["commands"]), 0)

    def test_19_commands_autocomplete(self):
        """测试命令自动补全端点"""
        code, data = api_request("POST", "/api/commands/autocomplete", {"prefix": "gi"}, token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("suggestions", data)

    def test_20_laninfo(self):
        """测试局域网信息端点"""
        code, data = api_request("GET", "/api/laninfo", token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("lan_ip", data)
        self.assertIn("port", data)
        self.assertIn("address", data)

    def test_21_versions(self):
        """测试版本列表端点"""
        code, data = api_request("GET", "/api/versions", token=self.token)
        self.assertEqual(code, 200)
        # 版本列表可能从网络获取，也可能使用内置列表
        self.assertIn("versions", data)

    def test_22_packs_list(self):
        """测试包列表端点"""
        code, data = api_request("GET", "/api/packs", token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("packs", data)
        self.assertIn("resource_packs", data["packs"])
        self.assertIn("behavior_packs", data["packs"])

    def test_23_players_list(self):
        """测试玩家列表端点"""
        code, data = api_request("GET", "/api/players", token=self.token)
        self.assertEqual(code, 200)
        # 服务器未运行，玩家列表应为空
        self.assertIn("players", data)

    def test_24_console_clear(self):
        """测试清空控制台端点"""
        code, data = api_request("POST", "/api/console/clear", {}, token=self.token)
        self.assertEqual(code, 200)

    def test_25_config_full(self):
        """测试完整配置端点"""
        code, data = api_request("GET", "/api/config/full", token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("data", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
