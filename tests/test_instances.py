"""多实例测试：注册表、当前实例切换、进程/控制台隔离、启动停止路由。"""

import os
import tempfile
from unittest import TestCase, main, mock

from bedrock_server_manager import server
from bedrock_server_manager.app_context import AppContext
from bedrock_server_manager.console import ConsoleBuffer


class FakeProc:
    """替代 ServerProcess：不真的启动进程。"""

    instances = []

    def __init__(self, server_dir, on_output=None, on_state=None):
        self.server_dir = server_dir
        self.running = False
        self.started = 0
        self.stopped = 0
        self.sent = []
        self.on_output = on_output
        self.on_state = on_state
        FakeProc.instances.append(self)

    def start(self):
        self.started += 1
        self.running = True

    def stop(self):
        self.stopped += 1
        self.running = False

    def send(self, cmd):
        self.sent.append(cmd)


class InstanceTestBase(TestCase):
    def setUp(self):
        self.ctx = AppContext.instance()
        self._settings = dict(self.ctx.settings)
        self._console = self.ctx.console
        self._proc = self.ctx.server_proc
        self.ctx.settings["instances"] = []
        self.ctx.settings["server_dir"] = ""
        server._instances.clear()
        FakeProc.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.dir_a = os.path.join(self.tmp.name, "a")
        self.dir_b = os.path.join(self.tmp.name, "b")
        os.makedirs(self.dir_a)
        os.makedirs(self.dir_b)
        for d in (self.dir_a, self.dir_b):
            with open(os.path.join(d, "bedrock_server.exe"), "w", encoding="utf-8") as handle:
                handle.write("x")
        self.patcher = mock.patch.object(server, "ServerProcess", FakeProc)
        self.patcher.start()
        self.save_patcher = mock.patch("bedrock_server_manager.utils.save_settings", return_value=True)
        self.save_patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.save_patcher.stop()
        self.ctx.settings.clear()
        self.ctx.settings.update(self._settings)
        self.ctx.console = self._console
        self.ctx.server_proc = self._proc
        server._instances.clear()
        self.tmp.cleanup()


class TestRegistry(InstanceTestBase):
    def test_register_is_idempotent(self):
        e1 = server.register_instance(self.dir_a, "A")
        e2 = server.register_instance(self.dir_a, "A")
        self.assertEqual(e1["dir"], e2["dir"])
        self.assertEqual(len(server.instance_dirs()), 1)

    def test_register_requires_directory(self):
        with self.assertRaises(ValueError):
            server.register_instance(os.path.join(self.tmp.name, "missing"))

    def test_rename(self):
        server.register_instance(self.dir_a, "A")
        server.register_instance(self.dir_a, "Renamed")
        self.assertEqual(server.instance_dirs()[0]["name"], "Renamed")

    def test_unregister(self):
        server.register_instance(self.dir_a)
        server.unregister_instance(self.dir_a)
        self.assertEqual(server.instance_dirs(), [])

    def test_list_includes_running_and_port(self):
        server.register_instance(self.dir_a)
        server.activate_instance(self.dir_a)
        server.start_instance(self.dir_a)
        items = {i["dir"]: i for i in server.list_instances()}
        self.assertTrue(items[self.dir_a]["running"])
        self.assertTrue(items[self.dir_a]["active"])
        self.assertEqual(items[self.dir_a]["port"], 19132)


class TestActivation(InstanceTestBase):
    def test_activate_sets_settings_and_context(self):
        server.register_instance(self.dir_a)
        server.register_instance(self.dir_b)
        server.activate_instance(self.dir_a)
        self.assertEqual(os.path.abspath(self.ctx.settings["server_dir"]), os.path.abspath(self.dir_a))
        console_a = self.ctx.console

        server.activate_instance(self.dir_b)
        self.assertEqual(os.path.abspath(self.ctx.settings["server_dir"]), os.path.abspath(self.dir_b))
        self.assertIsNot(self.ctx.console, console_a)
        self.assertIsInstance(self.ctx.console, ConsoleBuffer)

    def test_get_server_proc_follows_active(self):
        server.register_instance(self.dir_a)
        server.register_instance(self.dir_b)
        server.activate_instance(self.dir_a)
        proc_a = server.get_server_proc()
        server.activate_instance(self.dir_b)
        proc_b = server.get_server_proc()
        self.assertIsNot(proc_a, proc_b)
        self.assertEqual(os.path.abspath(proc_b.server_dir), os.path.abspath(self.dir_b))

    def test_activate_invalid_dir(self):
        with self.assertRaises(ValueError):
            server.activate_instance(os.path.join(self.tmp.name, "nope"))


class TestIsolation(InstanceTestBase):
    def test_consoles_are_separate(self):
        server.register_instance(self.dir_a)
        server.register_instance(self.dir_b)
        server.activate_instance(self.dir_a)
        inst_a = server._get_instance(self.dir_a)
        inst_b = server._get_instance(self.dir_b)
        inst_a["console"].append("[A] hello\n")
        inst_b["console"].append("[B] world\n")
        self.assertIn("[A] hello", "".join(inst_a["console"].lines))
        self.assertNotIn("[B] world", "".join(inst_a["console"].lines))

    def test_start_stop_independent(self):
        server.register_instance(self.dir_a)
        server.register_instance(self.dir_b)
        server.start_instance(self.dir_a)
        self.assertTrue(server.instance_running(self.dir_a))
        self.assertFalse(server.instance_running(self.dir_b))
        server.stop_instance(self.dir_a)
        self.assertFalse(server.instance_running(self.dir_a))

    def test_server_running_only_active(self):
        server.register_instance(self.dir_a)
        server.register_instance(self.dir_b)
        server.activate_instance(self.dir_a)
        server.start_instance(self.dir_a)
        self.assertTrue(server.server_running())
        server.activate_instance(self.dir_b)
        self.assertFalse(server.server_running())

    def test_send_to_instance(self):
        server.register_instance(self.dir_a)
        server.start_instance(self.dir_a)
        self.assertTrue(server.send_to_instance(self.dir_a, "list"))
        proc = server._instances[server._instance_key(self.dir_a)]["proc"]
        self.assertEqual(proc.sent, ["list"])
        self.assertFalse(server.send_to_instance(self.dir_b, "list"))

    def test_unregister_running_stops(self):
        server.register_instance(self.dir_a)
        server.start_instance(self.dir_a)
        server.unregister_instance(self.dir_a)
        proc = server._instances.get(server._instance_key(self.dir_a))
        self.assertIsNone(proc)

    def test_unregister_active_switches_to_remaining(self):
        server.register_instance(self.dir_a)
        server.register_instance(self.dir_b)
        server.activate_instance(self.dir_a)
        server.unregister_instance(self.dir_a)
        self.assertEqual(os.path.abspath(self.ctx.settings["server_dir"]), os.path.abspath(self.dir_b))


class TestSyncBootstrap(InstanceTestBase):
    def test_imports_current_dir_when_registry_empty(self):
        self.ctx.settings["server_dir"] = self.dir_a
        server.sync_active_instance()
        dirs = [e["dir"] for e in server.instance_dirs()]
        self.assertEqual([os.path.abspath(d) for d in dirs], [os.path.abspath(self.dir_a)])
        self.assertIsInstance(self.ctx.console, ConsoleBuffer)

    def test_keeps_existing_registry(self):
        server.register_instance(self.dir_b)
        self.ctx.settings["server_dir"] = self.dir_a
        server.sync_active_instance()
        self.assertEqual(len(server.instance_dirs()), 1)
        self.assertEqual(os.path.abspath(server.instance_dirs()[0]["dir"]), os.path.abspath(self.dir_b))


if __name__ == "__main__":
    main(verbosity=2)
