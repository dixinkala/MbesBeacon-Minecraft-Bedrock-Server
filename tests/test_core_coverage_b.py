"""核心模块单文件覆盖率测试（B 组）。

覆盖 ``server.py`` / ``players.py`` / ``app_update.py`` / ``performance.py``：
不启动真实 BDS、不联网，所有进程/网络/psutil 调用均用桩替换。

本文件独立运行（不依赖其它测试文件），每个用例都会隔离 AppContext、
多实例注册表、更新缓存与性能监控全局状态。
"""

import json
import time
import urllib.error
from unittest import mock

import pytest

from bedrock_server_manager import app_update, performance, players, server
from bedrock_server_manager.app_context import AppContext
from bedrock_server_manager.console import ConsoleBuffer
from bedrock_server_manager.performance import PerformanceMonitor, performance_monitor

# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------


class _QuietLogger:
    """吞掉日志，避免测试写用户 APPDATA 日志文件。"""

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def _isolate():
    """隔离全局状态：AppContext、实例注册表、更新缓存、性能监控。"""
    ctx = AppContext.instance()
    saved_settings = dict(ctx.settings)
    saved_console = ctx._console
    saved_proc = ctx._server_proc

    ctx.settings.clear()
    ctx.settings.update({"server_dir": "", "instances": [], "server_versions": {}})
    ctx._console = None
    ctx._server_proc = None
    server._instances.clear()

    app_update._app_update_cache = None
    app_update._app_update_cache_time = 0.0
    performance_monitor.reset()

    yield

    ctx.settings.clear()
    ctx.settings.update(saved_settings)
    ctx._console = saved_console
    ctx._server_proc = saved_proc
    server._instances.clear()
    app_update._app_update_cache = None
    app_update._app_update_cache_time = 0.0
    performance_monitor.reset()


@pytest.fixture(autouse=True)
def _no_settings_disk():
    """避免多实例注册表真的写 settings.json。"""
    with mock.patch("bedrock_server_manager.utils.save_settings", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _quiet_logger():
    """所有模块的 get_app_logger 都返回静默 logger。"""
    quiet = _QuietLogger()
    with (
        mock.patch("bedrock_server_manager.app_logger.get_app_logger", return_value=quiet),
        mock.patch.object(app_update, "get_app_logger", return_value=quiet),
    ):
        yield


# ---------------------------------------------------------------------------
# server.py: 端口检测与配置读取
# ---------------------------------------------------------------------------


class _FakeSocket:
    """UDP 探测用的假 socket，可指定 bind 时抛出的异常。"""

    def __init__(self, exc=None):
        self.exc = exc
        self.bound = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def setsockopt(self, *args):
        pass

    def bind(self, addr):
        if self.exc is not None:
            raise self.exc
        self.bound = addr


class _WinErr(OSError):
    """模拟 Windows WSAEADDRINUSE（带 winerror，无 errno）。"""

    def __init__(self):
        super().__init__("in use")
        self.winerror = 10048


def _patch_socket(exc=None):
    return mock.patch.object(server.socket, "socket", side_effect=lambda *a, **k: _FakeSocket(exc))


def test_check_port_in_use_rejects_invalid_ports():
    assert server.check_port_in_use("19132") is True
    assert server.check_port_in_use(0) is True
    assert server.check_port_in_use(65536) is True
    assert server.check_port_in_use(-1) is True


def test_check_port_in_use_free_port():
    with _patch_socket() as sock_cls:
        assert server.check_port_in_use(19132) is False
    assert sock_cls.call_count == 1


def test_check_port_in_use_addr_in_use_errno():
    with _patch_socket(OSError(98, "address in use")):
        assert server.check_port_in_use(19132) is True


def test_check_port_in_use_addr_in_use_winerror():
    with _patch_socket(_WinErr()):
        assert server.check_port_in_use(19132) is True


def test_check_port_in_use_other_oserror_is_free():
    with _patch_socket(OSError(13, "permission denied")):
        assert server.check_port_in_use(19132) is False


def test_check_port_in_use_unexpected_exception_is_busy():
    with _patch_socket(RuntimeError("boom")):
        assert server.check_port_in_use(19132) is True


def test_get_server_port_reads_property(tmp_path):
    (tmp_path / "server.properties").write_text("server-port=12345\n", encoding="utf-8")
    assert server.get_server_port(str(tmp_path)) == 12345


def test_get_server_port_invalid_value_defaults(tmp_path):
    (tmp_path / "server.properties").write_text("server-port=abc\n", encoding="utf-8")
    assert server.get_server_port(str(tmp_path)) == 19132


def test_get_server_port_missing_property_defaults(tmp_path):
    (tmp_path / "server.properties").write_text("server-name=test\n", encoding="utf-8")
    assert server.get_server_port(str(tmp_path)) == 19132


def test_get_server_port_load_error_defaults(tmp_path):
    with mock.patch.object(server, "load_properties", side_effect=OSError("bad")):
        assert server.get_server_port(str(tmp_path)) == 19132


def test_module_helpers():
    ctx = AppContext.instance()
    ctx.settings["server_dir"] = "/tmp/x"
    ctx.settings["instances"] = []
    ctx.console = ConsoleBuffer()
    assert server._get_settings() is ctx.settings
    assert isinstance(server._get_constants(), tuple)
    proc, lock = server._get_server_proc_state()
    assert proc is None and lock is ctx.server_lock
    assert server._get_console() is ctx.console


# ---------------------------------------------------------------------------
# server.py: ServerProcess 进程生命周期
# ---------------------------------------------------------------------------


class _FakeStdin:
    def __init__(self, proc, stop_after_write=False):
        self.proc = proc
        self.stop_after_write = stop_after_write
        self.data = []

    def write(self, data):
        self.data.append(data)
        if self.stop_after_write:
            self.proc.returncode = 0
            self.proc.poll_calls = 0

    def flush(self):
        self.flushed = True


class _FakeStdout:
    def __init__(self, lines=None):
        self.lines = list(lines or [])

    def readline(self):
        return self.lines.pop(0) if self.lines else b""


class FakePopen:
    """替代 subprocess.Popen 的假进程。"""

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = None
        self.stdout = _FakeStdout()
        self.stdin = _FakeStdin(self)
        self.killed = False
        self.wait_calls = 0
        self.poll_calls = 0

    def poll(self):
        self.poll_calls += 1
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _fake_subprocess(popen_cls=FakePopen):
    return mock.MagicMock(
        CREATE_NO_WINDOW=0x08000000,
        PIPE=-1,
        STDOUT=-2,
        **{"Popen": mock.Mock(wraps=popen_cls)},
    )


class _ReaderProc:
    """_reader 专用假进程（可控制 wait / readline 异常）。"""

    def __init__(self, lines=None, returncode=0, read_exc=None, wait_exc=None):
        self._lines = list(lines or [])
        self.returncode = returncode
        self.read_exc = read_exc
        self.wait_exc = wait_exc
        self.stdout = self

    def readline(self):
        if self.read_exc is not None:
            raise self.read_exc
        return self._lines.pop(0) if self._lines else b""

    def wait(self, timeout=None):
        if self.wait_exc is not None:
            raise self.wait_exc
        return self.returncode

    def poll(self):
        return self.returncode


def _new_sp(tmp_path):
    out, states = [], []
    sp = server.ServerProcess(str(tmp_path), out.append, states.append)
    return sp, out, states


def test_server_process_running_property(tmp_path):
    sp, _, _ = _new_sp(tmp_path)
    assert sp.running is False
    fake = FakePopen(["x"])
    sp.proc = fake
    assert sp.running is True
    fake.returncode = 0
    assert sp.running is False


def test_start_already_running_returns(tmp_path):
    sp, _, _ = _new_sp(tmp_path)
    fake = FakePopen(["x"])
    sp.proc = fake
    with mock.patch.object(server, "subprocess", _fake_subprocess()) as sub:
        sp.start()
    sub.Popen.assert_not_called()


def test_start_missing_exe_raises(tmp_path):
    sp, _, _ = _new_sp(tmp_path)
    with pytest.raises(FileNotFoundError):
        sp.start()


def test_start_port_in_use_raises(tmp_path):
    sp, out, states = _new_sp(tmp_path)
    (tmp_path / "bedrock_server.exe").write_text("x", encoding="utf-8")
    with (
        mock.patch.object(server, "get_server_port", return_value=19132),
        mock.patch.object(server, "check_port_in_use", return_value=True),
    ):
        with pytest.raises(RuntimeError, match="端口 19132"):
            sp.start()
    assert any("已被其它程序占用" in line for line in out)
    assert any("修改 server-port" in line for line in out)
    assert states == []


def test_start_success_and_crash_counter_reset(tmp_path):
    sp, out, states = _new_sp(tmp_path)
    (tmp_path / "bedrock_server.exe").write_text("x", encoding="utf-8")
    sp._last_crash_time = time.time() - 10000
    sp._crash_restart_count = 3
    fake_mod = _fake_subprocess()
    with (
        mock.patch.object(server, "get_server_port", return_value=19132),
        mock.patch.object(server, "check_port_in_use", return_value=False),
        mock.patch.object(server, "subprocess", fake_mod),
    ):
        sp.start()
        if sp.reader_thread is not None:
            sp.reader_thread.join(5)
    assert sp.proc is not None
    assert sp._crash_restart_count == 0
    assert sp._user_stopped is False
    assert sp._start_time is not None
    assert True in states
    fake_mod.Popen.assert_called_once()


def test_start_permission_error_linux_is_runtime_error(tmp_path):
    sp, _, _ = _new_sp(tmp_path)
    (tmp_path / "bedrock_server").write_text("x", encoding="utf-8")
    fake_mod = _fake_subprocess(popen_cls=mock.MagicMock(side_effect=PermissionError("denied")))

    def popen_side_effect(*a, **k):
        raise PermissionError("denied")

    fake_mod.Popen = mock.MagicMock(side_effect=popen_side_effect)
    with (
        mock.patch.object(server, "_get_constants", return_value=("bedrock_server", False)),
        mock.patch.object(server, "get_server_port", return_value=19132),
        mock.patch.object(server, "check_port_in_use", return_value=False),
        mock.patch.object(server, "subprocess", fake_mod),
        pytest.raises(RuntimeError, match="可执行权限"),
    ):
        sp.start()


def test_start_permission_error_windows_is_wrapped(tmp_path):
    sp, _, _ = _new_sp(tmp_path)
    (tmp_path / "bedrock_server.exe").write_text("x", encoding="utf-8")

    def popen_side_effect(*a, **k):
        raise PermissionError("denied")

    fake_mod = _fake_subprocess()
    fake_mod.Popen = mock.MagicMock(side_effect=popen_side_effect)
    with (
        mock.patch.object(server, "_get_constants", return_value=("bedrock_server.exe", True)),
        mock.patch.object(server, "get_server_port", return_value=19132),
        mock.patch.object(server, "check_port_in_use", return_value=False),
        mock.patch.object(server, "subprocess", fake_mod),
        pytest.raises(RuntimeError, match="启动服务器失败"),
    ):
        sp.start()


def test_start_generic_exception_reraises(tmp_path):
    sp, _, _ = _new_sp(tmp_path)
    (tmp_path / "bedrock_server.exe").write_text("x", encoding="utf-8")

    def popen_side_effect(*a, **k):
        raise ValueError("weird")

    fake_mod = _fake_subprocess()
    fake_mod.Popen = mock.MagicMock(side_effect=popen_side_effect)
    with (
        mock.patch.object(server, "get_server_port", return_value=19132),
        mock.patch.object(server, "check_port_in_use", return_value=False),
        mock.patch.object(server, "subprocess", fake_mod),
        pytest.raises(ValueError, match="weird"),
    ):
        sp.start()


def test_start_oserror_is_wrapped(tmp_path):
    sp, _, _ = _new_sp(tmp_path)
    (tmp_path / "bedrock_server.exe").write_text("x", encoding="utf-8")

    def popen_side_effect(*a, **k):
        raise OSError("no exec")

    fake_mod = _fake_subprocess()
    fake_mod.Popen = mock.MagicMock(side_effect=popen_side_effect)
    with (
        mock.patch.object(server, "get_server_port", return_value=19132),
        mock.patch.object(server, "check_port_in_use", return_value=False),
        mock.patch.object(server, "subprocess", fake_mod),
        pytest.raises(RuntimeError, match="启动服务器失败"),
    ):
        sp.start()


def test_reader_normal_startup_failure_messages(tmp_path):
    sp, out, states = _new_sp(tmp_path)
    sp.proc = _ReaderProc([b"hello\n", b"world\n"], returncode=1)
    sp._start_time = time.time()
    sp._user_stopped = False
    sp._reader()
    assert states == [False]
    assert "hello" in "".join(out)
    assert any("启动失败" in line for line in out)
    assert any("请检查端口是否被占用" in line for line in out)
    assert sp._exit_code == 1


def test_reader_normal_unexpected_exit_messages(tmp_path):
    sp, out, states = _new_sp(tmp_path)
    sp.proc = _ReaderProc([], returncode=2)
    sp._start_time = time.time() - 120
    sp._user_stopped = False
    sp._reader()
    assert states == [False]
    assert any("意外退出" in line and "退出码: 2" in line for line in out)


def test_reader_exceptions_are_handled(tmp_path):
    sp, out, states = _new_sp(tmp_path)
    sp.proc = _ReaderProc(read_exc=OSError("read boom"), wait_exc=OSError("wait boom"), returncode=5)
    sp._start_time = time.time() - 120
    sp._user_stopped = True
    sp._reader()
    assert any("日志读取中断" in line for line in out)
    assert sp._exit_code == 5
    assert states == [False]


def _thread_capture():
    """捕获 server.threading.Thread 的构造，便于手动驱动后台线程。"""

    captured = []

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None, **kwargs):
            self.target = target
            self.args = args
            self.daemon = daemon
            captured.append(self)

        def start(self):
            pass

    return captured, _FakeThread


def test_reader_auto_restart_schedules_delayed_start(tmp_path):
    sp, out, states = _new_sp(tmp_path)
    sp.proc = _ReaderProc([], returncode=1)
    sp._start_time = time.time() - 120
    sp._user_stopped = False
    sp.start = mock.Mock()
    AppContext.instance().settings["auto_restart_on_crash"] = True
    captured, fake_thread = _thread_capture()
    with (
        mock.patch.object(server.threading, "Thread", fake_thread),
        mock.patch.object(server.time, "sleep"),
    ):
        sp._reader()
        assert len(captured) == 1
        captured[0].target(*captured[0].args)
    assert sp._crash_restart_count == 1
    assert sp._last_crash_time is not None
    sp.start.assert_called_once()
    assert any("自动重启" in line for line in out)
    assert states == []  # auto_restarting=True 时不通知已停止


def test_reader_auto_restart_cancelled_by_user_stop(tmp_path):
    sp, out, states = _new_sp(tmp_path)
    sp.proc = _ReaderProc([], returncode=1)
    sp._start_time = time.time() - 120
    sp._user_stopped = False
    sp.start = mock.Mock()
    AppContext.instance().settings["auto_restart_on_crash"] = True
    captured, fake_thread = _thread_capture()
    with (
        mock.patch.object(server.threading, "Thread", fake_thread),
        mock.patch.object(server.time, "sleep"),
    ):
        sp._reader()
        assert len(captured) == 1
        sp._user_stopped = True
        captured[0].target(*captured[0].args)
    sp.start.assert_not_called()
    assert any("自动重启已取消" in line for line in out)


def test_reader_auto_restart_max_reached(tmp_path):
    sp, out, states = _new_sp(tmp_path)
    sp.proc = _ReaderProc([], returncode=1)
    sp._start_time = time.time() - 120
    sp._user_stopped = False
    sp._crash_restart_count = sp.MAX_CRASH_RESTARTS
    AppContext.instance().settings["auto_restart_on_crash"] = True
    captured, fake_thread = _thread_capture()
    with mock.patch.object(server.threading, "Thread", fake_thread):
        sp._reader()
    assert captured == []
    assert sp._crash_restart_count == sp.MAX_CRASH_RESTARTS + 1
    assert any("已达到最大重启次数" in line for line in out)
    assert states == [False]  # 未安排自动重启，仍通知已停止


def test_decode_bytes_and_fallback():
    assert server.ServerProcess._decode(b"hello") == "hello"

    class _BadBytes:
        def decode(self, encoding="utf-8", errors="replace"):
            if encoding == "utf-8":
                raise ValueError("nope")
            return "gbk-ok"

    assert server.ServerProcess._decode(_BadBytes()) == "gbk-ok"


def test_emit_swallows_callback_errors(tmp_path):
    sp, _, _ = _new_sp(tmp_path)
    sp.on_output = mock.Mock(side_effect=RuntimeError("callback boom"))
    logger = mock.Mock()
    with mock.patch("bedrock_server_manager.app_logger.get_app_logger", return_value=logger):
        sp._emit("line")
    assert logger.warning.call_count >= 1


def test_emit_swallows_logger_errors(tmp_path):
    sp, _, _ = _new_sp(tmp_path)
    sp.on_output = mock.Mock(side_effect=RuntimeError("callback boom"))
    broken = mock.Mock()
    broken.warning.side_effect = RuntimeError("logger boom")
    with mock.patch("bedrock_server_manager.app_logger.get_app_logger", return_value=broken):
        sp._emit("line")


def test_send_requires_running_and_writes(tmp_path):
    sp, _, _ = _new_sp(tmp_path)
    with pytest.raises(RuntimeError, match="服务器未运行"):
        sp.send("list")
    fake = FakePopen(["x"])
    sp.proc = fake
    sp.send("list")
    assert fake.stdin.data == [b"list\n"]
    assert fake.stdin.flushed is True


def test_stop_not_running_marks_user_stopped(tmp_path):
    sp, _, _ = _new_sp(tmp_path)
    fake = FakePopen(["x"])
    fake.returncode = 0
    sp.proc = fake
    sp.stop(wait=0)
    assert sp._user_stopped is True


def test_stop_clean_shutdown(tmp_path):
    sp, _, _ = _new_sp(tmp_path)
    fake = FakePopen(["x"])
    fake.stdin.stop_after_write = True
    sp.proc = fake
    sp.stop(wait=0)
    assert fake.stdin.data == [b"stop\n"]
    assert fake.killed is False


def test_stop_send_failure_falls_back_to_kill(tmp_path):
    sp, _, states = _new_sp(tmp_path)
    fake = FakePopen(["x"])

    def boom(data):
        raise OSError("stdin closed")

    fake.stdin.write = boom
    sp.proc = fake
    sp.stop(wait=0)
    assert fake.killed is True
    assert states[-1] is False
    assert sp._user_stopped is True


def test_kill_no_process(tmp_path):
    sp, _, states = _new_sp(tmp_path)
    sp.kill()
    assert states == [False]


def test_kill_running_process(tmp_path):
    sp, _, states = _new_sp(tmp_path)
    fake = FakePopen(["x"])
    sp.proc = fake
    sp.kill()
    assert fake.killed is True
    assert sp._user_stopped is True
    assert states == [False]


def test_kill_swallows_errors(tmp_path):
    sp, _, states = _new_sp(tmp_path)
    fake = FakePopen(["x"])
    fake.kill = mock.Mock(side_effect=OSError("cannot kill"))
    sp.proc = fake
    sp.kill()
    assert states == [False]


# ---------------------------------------------------------------------------
# server.py: 多实例注册表
# ---------------------------------------------------------------------------


class FakeServerProc:
    instances = []

    def __init__(self, server_dir, on_output=None, on_state=None):
        self.server_dir = server_dir
        self.on_output = on_output
        self.on_state = on_state
        self.running = False
        self.started = 0
        self.stopped = 0
        self.sent = []
        FakeServerProc.instances.append(self)

    def start(self):
        self.started += 1
        self.running = True
        if self.on_state:
            self.on_state(True)

    def stop(self):
        self.stopped += 1
        self.running = False
        if self.on_state:
            self.on_state(False)

    def send(self, command):
        self.sent.append(command)


@pytest.fixture
def fake_proc_cls():
    FakeServerProc.instances = []
    with mock.patch.object(server, "ServerProcess", FakeServerProc):
        yield FakeServerProc


def test_instance_key():
    assert server._instance_key("") == ""
    assert server._instance_key("X") == server._instance_key("X")


def test_get_instance_requires_dir():
    with pytest.raises(ValueError, match="未设置服务器目录"):
        server._get_instance("")


def test_get_instance_creates_and_reuses(tmp_path, fake_proc_cls):
    inst = server._get_instance(str(tmp_path))
    assert inst["dir"] == str(tmp_path)
    assert isinstance(inst["console"], ConsoleBuffer)
    inst["proc"].on_output("hello")
    assert "hello" in inst["console"].lines
    inst["proc"].on_state(True)  # 空实现，不应抛异常
    assert server._get_instance(str(tmp_path)) is inst


def test_instance_dirs_filters_invalid_entries():
    ctx = AppContext.instance()
    ctx.settings["instances"] = [
        "bad",
        {"name": "no-dir"},
        {"dir": ""},
        {"dir": "/tmp/a", "name": "A"},
        {"dir": "/tmp/b"},
    ]
    assert server.instance_dirs() == [{"dir": "/tmp/a", "name": "A"}, {"dir": "/tmp/b", "name": ""}]
    ctx.settings["instances"] = "not-a-list"
    assert server.instance_dirs() == []


def test_save_instance_dirs_updates_settings():
    ctx = AppContext.instance()
    with mock.patch("bedrock_server_manager.utils.save_settings") as save:
        server._save_instance_dirs([{"dir": "/x", "name": "X"}])
    assert ctx.settings["instances"] == [{"dir": "/x", "name": "X"}]
    save.assert_called_once_with(ctx.settings)


def test_register_instance_errors_and_idempotent(tmp_path):
    with pytest.raises(ValueError, match="目录不存在"):
        server.register_instance(str(tmp_path / "missing"))

    entry = server.register_instance(str(tmp_path), "First")
    assert entry["name"] == "First"
    assert len(server.instance_dirs()) == 1

    renamed = server.register_instance(str(tmp_path), "Second")
    assert renamed["name"] == "Second"
    assert len(server.instance_dirs()) == 1

    again = server.register_instance(str(tmp_path))
    assert again["name"] == "Second"


def test_register_instance_default_name(tmp_path):
    d = tmp_path / "server-a"
    d.mkdir()
    entry = server.register_instance(str(d))
    assert entry["name"] == "server-a"


def test_unregister_instance_running_and_switches_active(tmp_path, fake_proc_cls):
    ctx = AppContext.instance()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    server.register_instance(str(dir_a))
    server.register_instance(str(dir_b))
    server.activate_instance(str(dir_a))

    inst = server._get_instance(str(dir_a))
    inst["proc"].running = True

    assert server.unregister_instance(str(dir_a)) is True
    assert inst["proc"].stopped == 1
    assert [e["dir"] for e in server.instance_dirs()] == [str(dir_b)]
    assert ctx.settings["server_dir"] == str(dir_b)
    assert server._instance_key(str(dir_a)) not in server._instances


def test_unregister_instance_last_active_clears_server_dir(tmp_path, fake_proc_cls):
    ctx = AppContext.instance()
    d = tmp_path / "only"
    d.mkdir()
    server.register_instance(str(d))
    server.activate_instance(str(d))
    server.unregister_instance(str(d))
    assert server.instance_dirs() == []
    assert ctx.settings["server_dir"] == ""


def test_unregister_instance_non_active(tmp_path, fake_proc_cls):
    ctx = AppContext.instance()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    server.register_instance(str(dir_a))
    server.register_instance(str(dir_b))
    server.activate_instance(str(dir_a))
    server.unregister_instance(str(dir_b))
    assert ctx.settings["server_dir"] == str(dir_a)


def test_instance_port_fallback():
    with mock.patch.object(server, "get_server_port", return_value=20000):
        assert server._instance_port("/x") == 20000
    with mock.patch.object(server, "get_server_port", side_effect=OSError("bad")):
        assert server._instance_port("/x") == 19132


def test_instance_version():
    assert server._instance_version("/x", {}) == ""
    key = server._instance_key("/x")
    assert server._instance_version("/x", {key: "1.2.3"}) == "1.2.3"
    assert server._instance_version("/x", "bad-map") == ""
    assert server._instance_version("/x", {key: None}) == ""


def test_list_instances_reports_state(tmp_path, fake_proc_cls):
    ctx = AppContext.instance()
    d = tmp_path / "srv"
    d.mkdir()
    (d / "server.properties").write_text("server-port=20001\n", encoding="utf-8")
    server.register_instance(str(d), "Srv")
    inst = server._get_instance(str(d))
    inst["proc"].running = True
    ctx.settings["server_versions"] = {server._instance_key(str(d)): "1.21.0"}
    server.activate_instance(str(d))
    items = server.list_instances()
    assert len(items) == 1
    item = items[0]
    assert item["name"] == "Srv"
    assert item["running"] is True
    assert item["port"] == 20001
    assert item["version"] == "1.21.0"
    assert item["active"] is True


def test_activate_instance_validation_and_fallback(tmp_path, fake_proc_cls):
    with pytest.raises(ValueError, match="目录不存在"):
        server.activate_instance(str(tmp_path / "nope"))
    d = tmp_path / "loose"
    d.mkdir()
    # 未注册：list_instances 为空，走兜底返回
    item = server.activate_instance(str(d))
    assert item["dir"] == str(d)
    assert item["active"] is True


def test_activate_instance_save_failure_is_suppressed(tmp_path, fake_proc_cls):
    d = tmp_path / "srv"
    d.mkdir()
    server.register_instance(str(d))
    with mock.patch("bedrock_server_manager.utils.save_settings", side_effect=RuntimeError("disk")):
        item = server.activate_instance(str(d))
    assert item["active"] is True


def test_sync_active_instance_registers_and_binds(tmp_path, fake_proc_cls):
    ctx = AppContext.instance()
    d = tmp_path / "srv"
    d.mkdir()
    ctx.settings["server_dir"] = str(d)
    server.sync_active_instance()
    assert len(server.instance_dirs()) == 1
    inst = server._instances[server._instance_key(str(d))]
    assert ctx.server_proc is inst["proc"]
    assert ctx.console is inst["console"]


def test_sync_active_instance_skips_missing_and_register_errors(tmp_path, fake_proc_cls):
    ctx = AppContext.instance()
    ctx.settings["server_dir"] = str(tmp_path / "missing")
    server.sync_active_instance()
    assert server.instance_dirs() == []

    d = tmp_path / "srv"
    d.mkdir()
    ctx.settings["server_dir"] = str(d)
    with mock.patch.object(server, "register_instance", side_effect=RuntimeError("boom")):
        server.sync_active_instance()
    assert ctx.server_proc is server._instances[server._instance_key(str(d))]["proc"]


def test_instance_running_and_start_stop_send(tmp_path, fake_proc_cls):
    d = tmp_path / "srv"
    d.mkdir()
    assert server.instance_running(str(d)) is False
    server.start_instance(str(d))
    inst = server._instances[server._instance_key(str(d))]
    assert inst["proc"].started == 1
    assert server.instance_running(str(d)) is True

    assert server.send_to_instance(str(d), "list") is True
    assert inst["proc"].sent == ["list"]

    server.stop_instance(str(d))
    assert inst["proc"].stopped == 1

    assert server.send_to_instance(str(tmp_path / "other"), "list") is False
    server.stop_instance(str(tmp_path / "other"))  # 不存在时不报错

    inst["proc"].running = False
    assert server.send_to_instance(str(d), "list") is False


def test_get_server_proc_and_server_running(tmp_path, fake_proc_cls):
    ctx = AppContext.instance()
    proc = server.get_server_proc()
    assert isinstance(proc, FakeServerProc)
    assert ctx.settings["server_dir"] == ""

    d = tmp_path / "srv"
    d.mkdir()
    ctx.settings["server_dir"] = str(d)
    assert server.get_server_proc() is server._instances[server._instance_key(str(d))]["proc"]
    assert server.server_running() is False
    server._instances[server._instance_key(str(d))]["proc"].running = True
    assert server.server_running() is True


# ---------------------------------------------------------------------------
# players.py
# ---------------------------------------------------------------------------


def test_validate_player_name():
    assert players.validate_player_name("Steve") == (True, "")
    assert players.validate_player_name("Steve_01.x") == (True, "")
    assert players.validate_player_name("") == (False, "玩家名不能为空")
    assert players.validate_player_name(None) == (False, "玩家名不能为空")
    assert players.validate_player_name("a" * 17) == (False, "玩家名长度不能超过16个字符")
    assert players.validate_player_name("bad name") == (False, "玩家名只能包含字母、数字、下划线和点号")


class _CaptureConsole:
    def __init__(self, first=("", 0), second=("", 0), read_exc=None):
        self.first = first
        self.second = second
        self.read_exc = read_exc
        self.read_calls = []
        self.wait_calls = []

    def read_since(self, idx):
        if self.read_exc:
            raise self.read_exc
        self.read_calls.append(idx)
        return ("before", 0)

    def wait_for_new(self, idx, timeout=None):
        self.wait_calls.append((idx, timeout))
        return self.first if len(self.wait_calls) == 1 else self.second


def test_parse_online_players():
    assert players.parse_online_players("") == []
    assert players.parse_online_players("no players here") == []
    assert players.parse_online_players("There are 0/10 players online:") == []
    assert players.parse_online_players("There are 2/10 players online: Steve, Alex") == ["Steve", "Alex"]
    assert players.parse_online_players("There are 2/10 players online: 小明, Alex_01") == ["小明", "Alex_01"]


def test_send_command_capture_not_running():
    with mock.patch.object(players, "server_running", return_value=False):
        assert players.send_command_capture("list") is None


def test_send_command_capture_success_with_extra():
    ctx = AppContext.instance()
    console = _CaptureConsole(first=("There are 1/10 players online: Steve", 3), second=("extra line", 4))
    ctx.console = console
    proc = mock.Mock()
    with (
        mock.patch.object(players, "server_running", return_value=True),
        mock.patch.object(players, "get_server_proc", return_value=proc),
    ):
        text = players.send_command_capture("list", timeout=1.0)
    assert text == "There are 1/10 players online: Steveextra line"
    proc.send.assert_called_once_with("list")
    assert console.wait_calls == [(0, 1.0), (3, 0.3)]


def test_send_command_capture_no_output_and_send_error():
    ctx = AppContext.instance()
    ctx.console = _CaptureConsole(first=("", 0))
    proc = mock.Mock()
    with (
        mock.patch.object(players, "server_running", return_value=True),
        mock.patch.object(players, "get_server_proc", return_value=proc),
    ):
        assert players.send_command_capture("list") == ""
        proc.send.side_effect = OSError("closed")
        assert players.send_command_capture("list") is None


def _write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_read_permissions(tmp_path):
    assert players.read_permissions("") == {}
    assert players.read_permissions(str(tmp_path)) == {}

    _write_json(
        tmp_path / "permissions.json",
        [{"name": "Steve", "permission": "operator"}, {"permission": "member"}, {"name": "Alex"}],
    )
    assert players.read_permissions(str(tmp_path)) == {"Steve": "operator", "Alex": "member"}

    (tmp_path / "permissions.json").write_text("{broken", encoding="utf-8")
    assert players.read_permissions(str(tmp_path)) == {}

    _write_json(tmp_path / "permissions.json", {"not": "a-list"})
    assert players.read_permissions(str(tmp_path)) == {}


def test_read_permissions_open_error(tmp_path):
    (tmp_path / "permissions.json").mkdir()
    assert players.read_permissions(str(tmp_path)) == {}


def test_read_banlist(tmp_path):
    assert players.read_banlist("") == []
    assert players.read_banlist(str(tmp_path)) == []
    _write_json(
        tmp_path / "banned-players.json", [{"name": "Steve", "reason": "cheat"}, {"reason": "x"}, {"name": "Alex"}]
    )
    assert players.read_banlist(str(tmp_path)) == [
        {"name": "Steve", "reason": "cheat"},
        {"name": "Alex", "reason": ""},
    ]
    (tmp_path / "banned-players.json").write_text("not json", encoding="utf-8")
    assert players.read_banlist(str(tmp_path)) == []


def test_read_allowlist(tmp_path):
    assert players.read_allowlist("") == []
    assert players.read_allowlist(str(tmp_path)) == []
    _write_json(tmp_path / "allowlist.json", [{"name": "Steve"}, "Alex", {"other": 1}])
    assert players.read_allowlist(str(tmp_path)) == [{"name": "Steve"}, {"name": "Alex"}]
    (tmp_path / "allowlist.json").write_text("not json", encoding="utf-8")
    assert players.read_allowlist(str(tmp_path)) == []


def test_add_allowlist_empty_name(tmp_path):
    assert players.add_allowlist(str(tmp_path), "") is False
    assert players.add_allowlist(str(tmp_path), None) is False


def test_add_allowlist_running_sends_command(tmp_path):
    proc = mock.Mock()
    with (
        mock.patch.object(players, "server_running", return_value=True),
        mock.patch.object(players, "get_server_proc", return_value=proc),
    ):
        assert players.add_allowlist(str(tmp_path), "Steve") is True
    proc.send.assert_called_once_with("whitelist add Steve")
    assert not (tmp_path / "allowlist.json").exists()


def test_add_allowlist_file_operations(tmp_path):
    path = tmp_path / "allowlist.json"
    assert players.add_allowlist(str(tmp_path), "Steve") is True
    assert json.loads(path.read_text(encoding="utf-8")) == [{"name": "Steve", "ignoresPlayerLimit": False}]

    # 重复添加不产生重复项
    assert players.add_allowlist(str(tmp_path), "Steve") is True
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 1

    # 非列表内容会被重置
    path.write_text(json.dumps({"x": 1}), encoding="utf-8")
    assert players.add_allowlist(str(tmp_path), "Alex") is True
    assert json.loads(path.read_text(encoding="utf-8")) == [{"name": "Alex", "ignoresPlayerLimit": False}]


def test_add_allowlist_exception(tmp_path):
    (tmp_path / "allowlist.json").mkdir()
    assert players.add_allowlist(str(tmp_path), "Steve") is False


def test_remove_allowlist(tmp_path):
    assert players.remove_allowlist(str(tmp_path), "") is False
    assert players.remove_allowlist(str(tmp_path), "Steve") is True  # 文件不存在

    path = tmp_path / "allowlist.json"
    _write_json(path, [{"name": "Steve"}, {"name": "Alex"}])
    assert players.remove_allowlist(str(tmp_path), "Steve") is True
    assert json.loads(path.read_text(encoding="utf-8")) == [{"name": "Alex"}]

    path.write_text(json.dumps({"x": 1}), encoding="utf-8")
    assert players.remove_allowlist(str(tmp_path), "Alex") is True


def test_remove_allowlist_running_and_error(tmp_path):
    proc = mock.Mock()
    with (
        mock.patch.object(players, "server_running", return_value=True),
        mock.patch.object(players, "get_server_proc", return_value=proc),
    ):
        assert players.remove_allowlist(str(tmp_path), "Steve") is True
    proc.send.assert_called_once_with("whitelist remove Steve")

    (tmp_path / "allowlist.json").mkdir()
    with mock.patch.object(players, "server_running", return_value=False):
        assert players.remove_allowlist(str(tmp_path), "Steve") is False


def test_load_banned_ips(tmp_path):
    with mock.patch.object(players, "resolve_server_dir", return_value=""):
        assert players.load_banned_ips() == []
    assert players.load_banned_ips(str(tmp_path)) == []
    _write_json(tmp_path / "banned-ips.json", [{"ip": "1.2.3.4"}])
    assert players.load_banned_ips(str(tmp_path)) == [{"ip": "1.2.3.4"}]
    AppContext.instance().settings["server_dir"] = str(tmp_path)
    assert players.load_banned_ips() == [{"ip": "1.2.3.4"}]

    (tmp_path / "banned-ips.json").write_text("{bad", encoding="utf-8")
    assert players.load_banned_ips(str(tmp_path)) == []


def test_load_banned_ips_open_error(tmp_path):
    (tmp_path / "banned-ips.json").mkdir()
    assert players.load_banned_ips(str(tmp_path)) == []


def test_validate_ip():
    assert players.validate_ip("127.0.0.1") == (True, "")
    assert players.validate_ip("::1") == (True, "")
    assert players.validate_ip("") == (False, "IP 地址不能为空")
    assert players.validate_ip(None) == (False, "IP 地址不能为空")
    assert players.validate_ip(123) == (False, "IP 地址不能为空")
    ok, err = players.validate_ip("999.1.1.1")
    assert ok is False and "无效的 IP" in err


def test_ban_ip_validation(tmp_path):
    assert players.ban_ip("bad-ip", "x", str(tmp_path))[0] is False
    ok, err = players.ban_ip("1.2.3.4", "line1\nline2", str(tmp_path))
    assert ok is False and "换行" in err
    with mock.patch.object(players, "resolve_server_dir", return_value=""):
        assert players.ban_ip("1.2.3.4") == (False, "未设置服务器目录")


def test_ban_ip_already_banned(tmp_path):
    _write_json(tmp_path / "banned-ips.json", [{"ip": "1.2.3.4"}])
    ok, err = players.ban_ip("1.2.3.4", "", str(tmp_path))
    assert ok is False and "已在封禁列表" in err

    _write_json(tmp_path / "banned-ips.json", [{"address": "5.6.7.8"}])
    ok, err = players.ban_ip("5.6.7.8", "", str(tmp_path))
    assert ok is False and "已在封禁列表" in err


def test_ban_ip_not_running_writes_file(tmp_path):
    with mock.patch.object(players, "server_running", return_value=False):
        ok, err = players.ban_ip("1.2.3.4", "cheat", str(tmp_path))
    assert ok is True and err == ""
    data = json.loads((tmp_path / "banned-ips.json").read_text(encoding="utf-8"))
    assert data[0]["ip"] == "1.2.3.4"
    assert data[0]["reason"] == "cheat"
    assert data[0]["source"] == "manual"
    assert "created" in data[0]


def test_ban_ip_running_sends_command_and_writes(tmp_path):
    proc = mock.Mock()
    with (
        mock.patch.object(players, "server_running", return_value=True),
        mock.patch.object(players, "get_server_proc", return_value=proc),
    ):
        ok, _ = players.ban_ip("1.2.3.4", "cheat", str(tmp_path))
    assert ok is True
    proc.send.assert_called_once_with("ban-ip 1.2.3.4 cheat")
    assert json.loads((tmp_path / "banned-ips.json").read_text(encoding="utf-8"))[0]["ip"] == "1.2.3.4"


def test_ban_ip_running_send_failure(tmp_path):
    proc = mock.Mock()
    proc.send.side_effect = OSError("closed")
    with (
        mock.patch.object(players, "server_running", return_value=True),
        mock.patch.object(players, "get_server_proc", return_value=proc),
    ):
        ok, err = players.ban_ip("1.2.3.4", "cheat", str(tmp_path))
    assert ok is False and "发送封禁命令失败" in err
    assert not (tmp_path / "banned-ips.json").exists()


def test_ban_ip_generic_exception():
    with mock.patch.object(players, "resolve_server_dir", side_effect=RuntimeError("boom")):
        ok, err = players.ban_ip("1.2.3.4")
    assert ok is False and err == "boom"


def test_pardon_ip_validation(tmp_path):
    assert players.pardon_ip("bad-ip", str(tmp_path))[0] is False
    with mock.patch.object(players, "resolve_server_dir", return_value=""):
        assert players.pardon_ip("1.2.3.4") == (False, "未设置服务器目录")


def test_pardon_ip_not_running(tmp_path):
    with mock.patch.object(players, "server_running", return_value=False):
        ok, err = players.pardon_ip("1.2.3.4", str(tmp_path))
    assert ok is False and "不在封禁列表" in err

    _write_json(tmp_path / "banned-ips.json", [{"ip": "1.2.3.4"}, {"ip": "5.6.7.8"}])
    with mock.patch.object(players, "server_running", return_value=False):
        ok, err = players.pardon_ip("1.2.3.4", str(tmp_path))
    assert ok is True and err == ""
    assert json.loads((tmp_path / "banned-ips.json").read_text(encoding="utf-8")) == [{"ip": "5.6.7.8"}]


def test_pardon_ip_running(tmp_path):
    _write_json(tmp_path / "banned-ips.json", [{"ip": "1.2.3.4"}])
    proc = mock.Mock()
    with (
        mock.patch.object(players, "server_running", return_value=True),
        mock.patch.object(players, "get_server_proc", return_value=proc),
    ):
        ok, err = players.pardon_ip("1.2.3.4", str(tmp_path))
        assert (ok, err) == (True, "")
        # 运行中即使不在列表也返回成功（由服务端命令处理）
        ok, err = players.pardon_ip("8.8.8.8", str(tmp_path))
        assert (ok, err) == (True, "")
    assert proc.send.call_args_list[0] == mock.call("pardon-ip 1.2.3.4")

    proc.send.side_effect = OSError("closed")
    with (
        mock.patch.object(players, "server_running", return_value=True),
        mock.patch.object(players, "get_server_proc", return_value=proc),
    ):
        ok, err = players.pardon_ip("1.2.3.4", str(tmp_path))
    assert ok is False and "发送解封命令失败" in err


def test_pardon_ip_generic_exception():
    with mock.patch.object(players, "resolve_server_dir", side_effect=RuntimeError("boom")):
        ok, err = players.pardon_ip("1.2.3.4")
    assert ok is False and err == "boom"


def test_list_installed_packs_delegates():
    with mock.patch("bedrock_server_manager.packs.list_installed_packs", return_value=["pack"]) as fn:
        assert players.list_installed_packs("dir") == ["pack"]
    fn.assert_called_once_with("dir")


def test_get_all_player_status_merges_sources(tmp_path):
    _write_json(tmp_path / "permissions.json", [{"name": "Steve", "permission": "operator"}])
    _write_json(tmp_path / "allowlist.json", [{"name": "Steve"}, {"name": "Alex"}])
    _write_json(tmp_path / "banned-players.json", [{"name": "Steve", "reason": "x"}, {"name": "Bob", "reason": "y"}])
    status = {p["name"]: p for p in players.get_all_player_status(str(tmp_path))}
    assert status["Steve"]["permission"] == "operator"
    assert status["Steve"]["whitelisted"] is True
    assert status["Steve"]["banned"] is True
    assert status["Alex"]["permission"] == "member"
    assert status["Alex"]["whitelisted"] is True
    assert status["Bob"]["banned"] is True


def test_get_all_player_status_empty_and_error():
    with mock.patch.object(players, "resolve_server_dir", return_value=""):
        assert players.get_all_player_status() == []
    with mock.patch.object(players, "read_permissions", side_effect=OSError("bad")):
        assert players.get_all_player_status("/tmp/whatever") == []


# ---------------------------------------------------------------------------
# app_update.py
# ---------------------------------------------------------------------------

_RELEASES_URL = "https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/releases/tag/v9.9.9"
_PAGE_HTML = (
    "<html><head><title>Release 9.9.9 · dixinkala/MbesBeacon-Minecraft-Bedrock-Server</title></head>"
    '<body><relative-time datetime="2026-01-02T03:04:05Z">now</relative-time>'
    '<div class="markdown-body"><p>Fixes &amp; changes</p></div></div></body></html>'
)
_ASSETS_HTML = (
    '<a href="/releases/download/v9.9.9/app-9.9.9.zip">zip</a>'
    '<a href="/releases/download/v9.9.9/MbesBeacon-Setup.exe">exe</a>'
)


def _release_info(**overrides):
    info = {
        "tag_name": "v9.9.9",
        "latest_version": "9.9.9",
        "latest_title": "9.9.9",
        "release_notes": "notes",
        "release_url": _RELEASES_URL,
        "published_at": "2026-01-02T03:04:05+00:00",
        "download_url": "https://github.com/x/MbesBeacon-Setup.exe",
    }
    info.update(overrides)
    return info


def test_parse_version():
    assert app_update._parse_version(None) is None
    assert app_update._parse_version("") is None
    assert app_update._parse_version("v") is None
    assert app_update._parse_version("v1.2.3") == (1, 2, 3, 0)
    assert app_update._parse_version("1.0.22.1") == (1, 0, 22, 1)
    assert app_update._parse_version("1.2.3.4.5") == (1, 2, 3, 4)
    assert app_update._parse_version("1.0.25-beta") is None
    assert app_update._parse_version("1..2") is None
    assert app_update._parse_version("1.2rc1") is None


def test_is_newer():
    assert app_update._is_newer("1.2.3", "1.2.2") is True
    assert app_update._is_newer("1.2.2", "1.2.3") is False
    assert app_update._is_newer("1.0.25-beta", "1.0.24") is False
    assert app_update._is_newer("1.0.25", "1.0.25-beta") is False


def test_load_cache(tmp_path):
    cache_file = tmp_path / "cache.json"
    with mock.patch.object(app_update, "_APP_UPDATE_CACHE_FILE", str(cache_file)):
        assert app_update._load_cache() is False
        cache_file.write_text(json.dumps({"result": {"ok": True}, "timestamp": 123.0}), encoding="utf-8")
        assert app_update._load_cache() is True
        assert app_update._app_update_cache == {"ok": True}
        assert app_update._app_update_cache_time == 123.0

        cache_file.write_text("{bad", encoding="utf-8")
        assert app_update._load_cache() is False


def test_save_cache(tmp_path):
    cache_file = tmp_path / "sub" / "cache.json"
    with mock.patch.object(app_update, "_APP_UPDATE_CACHE_FILE", str(cache_file)):
        app_update._save_cache({"ok": True})
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert data["result"] == {"ok": True}
        assert isinstance(data["timestamp"], float)
        assert app_update._app_update_cache == {"ok": True}


def test_save_cache_failure_is_swallowed(tmp_path):
    target_dir = tmp_path / "as_dir"
    target_dir.mkdir()
    with mock.patch.object(app_update, "_APP_UPDATE_CACHE_FILE", str(target_dir)):
        app_update._save_cache({"ok": False})  # open(目录) 抛异常，但被吞掉
    assert app_update._app_update_cache is None


class _FakeHTTPResponse:
    def __init__(self, text, url="https://example.com/final"):
        self.text = text
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def geturl(self):
        return self.url

    def read(self):
        return self.text.encode("utf-8")


def test_http_get():
    resp = _FakeHTTPResponse("body", "https://example.com/redirected")
    with mock.patch.object(app_update.urllib.request, "urlopen", return_value=resp) as urlopen:
        assert app_update._http_get("https://example.com") == ("https://example.com/redirected", "body")
    assert urlopen.call_args.kwargs["timeout"] == 10
    assert urlopen.call_args.kwargs["context"] is None

    with mock.patch.object(app_update.urllib.request, "urlopen", return_value=resp) as urlopen:
        app_update._http_get("https://example.com", ignore_ssl=True, timeout=3)
    ctx = urlopen.call_args.kwargs["context"]
    assert ctx is not None and ctx.check_hostname is False
    assert urlopen.call_args.kwargs["timeout"] == 3


def test_strip_tags():
    assert app_update._strip_tags("") == ""
    assert app_update._strip_tags("<p>Hello &amp;   world</p>") == "Hello & world"


def test_check_via_html_success():
    def fake_get(url, ignore_ssl=False, timeout=10):
        if url == app_update.GITHUB_RELEASES_LATEST:
            return _RELEASES_URL, ""
        if "expanded_assets" in url:
            return url, _ASSETS_HTML
        return url, _PAGE_HTML

    with mock.patch.object(app_update, "_http_get", side_effect=fake_get):
        info = app_update._check_via_html()
    assert info["tag_name"] == "v9.9.9"
    assert info["latest_version"] == "9.9.9"
    assert info["latest_title"] == "9.9.9"
    assert info["published_at"] == "2026-01-02T03:04:05+00:00"
    assert info["release_notes"] == "Fixes & changes"
    assert info["download_url"] == "https://github.com/releases/download/v9.9.9/MbesBeacon-Setup.exe"


def test_check_via_html_bad_tag_url():
    with mock.patch.object(app_update, "_http_get", return_value=("https://github.com/x", "")):
        with pytest.raises(ValueError, match="无法从响应 URL 解析版本"):
            app_update._check_via_html()


def test_check_via_html_assets_failure_and_absolute_url():
    html_no_title = "<html><body><div>plain</div></body></html>"

    def fake_get(url, ignore_ssl=False, timeout=10):
        if url == app_update.GITHUB_RELEASES_LATEST:
            return _RELEASES_URL, ""
        if "expanded_assets" in url:
            raise OSError("assets down")
        return url, html_no_title

    with mock.patch.object(app_update, "_http_get", side_effect=fake_get):
        info = app_update._check_via_html()
    assert info["download_url"] == ""
    assert info["latest_title"] == ""
    assert info["published_at"] == ""
    assert info["release_notes"] == ""

    absolute_assets = '<a href="https://github.com/a/b/releases/download/v9/MbesBeacon.exe">exe</a>'

    def fake_get_abs(url, ignore_ssl=False, timeout=10):
        if url == app_update.GITHUB_RELEASES_LATEST:
            return _RELEASES_URL, ""
        return url, absolute_assets

    with mock.patch.object(app_update, "_http_get", side_effect=fake_get_abs):
        info = app_update._check_via_html(ignore_ssl=True, timeout=5)
    assert info["download_url"] == "https://github.com/a/b/releases/download/v9/MbesBeacon.exe"


def test_check_via_html_title_without_prefix_or_separator():
    page = "<title>My Release</title>"
    with mock.patch.object(app_update, "_http_get", return_value=(_RELEASES_URL, page)):
        # expanded_assets 请求返回同一 page（无 exe），不影响标题分支
        info = app_update._check_via_html()
    assert info["latest_title"] == "My Release"


def test_check_via_api():
    payload = {
        "tag_name": "v2.0.0",
        "name": "Release 2.0.0",
        "body": "notes",
        "html_url": "https://github.com/x/releases/tag/v2.0.0",
        "published_at": "2026-02-03T00:00:00Z",
        "assets": [
            {"name": "a.zip", "browser_download_url": "https://x/a.zip"},
            {"name": "A.EXE", "browser_download_url": "https://x/a.exe"},
        ],
    }
    resp = _FakeHTTPResponse(json.dumps(payload))
    with mock.patch.object(app_update.urllib.request, "urlopen", return_value=resp):
        info = app_update._check_via_api()
    assert info["tag_name"] == "v2.0.0"
    assert info["latest_version"] == "2.0.0"
    assert info["latest_title"] == "Release 2.0.0"
    assert info["download_url"] == "https://x/a.exe"

    with mock.patch.object(app_update.urllib.request, "urlopen", return_value=resp):
        info = app_update._check_via_api(ignore_ssl=True, timeout=4)
    assert info["latest_version"] == "2.0.0"

    with mock.patch.object(app_update.urllib.request, "urlopen", return_value=_FakeHTTPResponse("{}")):
        info = app_update._check_via_api()
    assert info["tag_name"] == "" and info["latest_version"] == "" and info["download_url"] == ""


def test_platform_download_url():
    with mock.patch.object(app_update, "IS_WINDOWS", True):
        assert app_update._platform_download_url("https://x/a.exe", "https://x/rel") == "https://x/a.exe"
        assert app_update._platform_download_url("", "https://x/rel") == "https://x/rel"
    with mock.patch.object(app_update, "IS_WINDOWS", False):
        assert app_update._platform_download_url("https://x/a.exe", "https://x/rel") == ""


def test_check_app_update_memory_cache_hit():
    app_update._app_update_cache = {"ok": True, "cached": True}
    app_update._app_update_cache_time = time.time()
    with mock.patch.object(app_update, "_check_via_html", side_effect=AssertionError("should not call")):
        result = app_update.check_app_update()
    assert result["cached"] is True


def test_check_app_update_file_cache_fresh(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(
        json.dumps({"result": {"ok": True, "from_file": True}, "timestamp": time.time()}), encoding="utf-8"
    )
    with (
        mock.patch.object(app_update, "_APP_UPDATE_CACHE_FILE", str(cache_file)),
        mock.patch.object(app_update, "_check_via_html", side_effect=AssertionError("should not call")),
    ):
        result = app_update.check_app_update()
    assert result["from_file"] is True


def test_check_app_update_file_cache_stale_refreshes(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"result": {"ok": True, "stale": True}, "timestamp": 0}), encoding="utf-8")
    with (
        mock.patch.object(app_update, "_APP_UPDATE_CACHE_FILE", str(cache_file)),
        mock.patch.object(app_update, "_check_via_html", return_value=_release_info()),
        mock.patch.object(app_update, "_save_cache") as save,
    ):
        result = app_update.check_app_update()
    assert result["ok"] is True
    assert result["has_update"] is True
    assert result["latest_version"] == "9.9.9"
    assert result["download_url"] == _release_info()["download_url"]
    assert result["platform"] == "win"
    save.assert_called_once()


def test_check_app_update_force_ignores_cache():
    app_update._app_update_cache = {"ok": True, "cached": True}
    app_update._app_update_cache_time = time.time()
    with (
        mock.patch.object(app_update, "_check_via_html", return_value=_release_info()) as html,
        mock.patch.object(app_update, "_save_cache"),
    ):
        result = app_update.check_app_update(force=True)
    html.assert_called_once()
    assert result["ok"] is True
    assert "cached" not in result


def test_check_app_update_html_older_version():
    with (
        mock.patch.object(app_update, "_check_via_html", return_value=_release_info(latest_version="1.0.0")),
        mock.patch.object(app_update, "_save_cache"),
    ):
        result = app_update.check_app_update(force=True)
    assert result["has_update"] is False


def test_check_app_update_html_fails_api_succeeds():
    err = urllib.error.HTTPError("https://x", 500, "Server Error", {}, None)
    with (
        mock.patch.object(app_update, "_check_via_html", side_effect=err),
        mock.patch.object(app_update, "_check_via_api", return_value=_release_info()),
        mock.patch.object(app_update, "_save_cache"),
    ):
        result = app_update.check_app_update(force=True)
    assert result["ok"] is True
    assert result["has_update"] is True


def test_check_app_update_http_403_message():
    html_err = urllib.error.HTTPError("https://x", 404, "Not Found", {}, None)
    api_err = urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)
    with (
        mock.patch.object(app_update, "_check_via_html", side_effect=html_err),
        mock.patch.object(app_update, "_check_via_api", side_effect=api_err),
    ):
        result = app_update.check_app_update(force=True)
    assert result["ok"] is False
    assert "HTTP 404" in result["error"]
    assert "请求过于频繁" in result["error"]


def test_check_app_update_url_errors():
    html_err = urllib.error.URLError("offline")
    api_err = urllib.error.URLError("still offline")
    with (
        mock.patch.object(app_update, "_check_via_html", side_effect=html_err),
        mock.patch.object(app_update, "_check_via_api", side_effect=api_err),
    ):
        result = app_update.check_app_update(force=True)
    assert result["ok"] is False
    assert "offline" in result["error"]


def test_check_app_update_json_decode_error():
    with (
        mock.patch.object(app_update, "_check_via_html", side_effect=ValueError("html broken")),
        mock.patch.object(app_update, "_check_via_api", side_effect=json.JSONDecodeError("bad", "", 0)),
    ):
        result = app_update.check_app_update(force=True)
    assert result["ok"] is False
    assert "html broken" in result["error"]
    assert "解析响应失败" in result["error"]


def test_check_app_update_generic_api_exception():
    with (
        mock.patch.object(app_update, "_check_via_html", side_effect=ValueError("html broken")),
        mock.patch.object(app_update, "_check_via_api", side_effect=RuntimeError("api broken")),
    ):
        result = app_update.check_app_update(force=True)
    assert result["ok"] is False
    assert "api broken" in result["error"]


def test_check_app_update_linux_download_url():
    with (
        mock.patch.object(app_update, "IS_WINDOWS", False),
        mock.patch.object(app_update, "_check_via_html", return_value=_release_info()),
        mock.patch.object(app_update, "_save_cache"),
    ):
        result = app_update.check_app_update(force=True)
    assert result["download_url"] == ""
    assert result["platform"] == "linux"


def test_check_app_update_async():
    results = []
    with mock.patch.object(app_update, "check_app_update", return_value={"ok": True, "has_update": False}) as check:
        thread = app_update.check_app_update_async(callback=results.append, force=True)
        thread.join(5)
    assert results == [{"ok": True, "has_update": False}]
    check.assert_called_once_with(force=True, ignore_ssl=False)


def test_check_app_update_async_exception():
    results = []
    with mock.patch.object(app_update, "check_app_update", side_effect=RuntimeError("boom")):
        thread = app_update.check_app_update_async(callback=results.append)
        thread.join(5)
    assert results[0]["ok"] is False
    assert "boom" in results[0]["error"]


# ---------------------------------------------------------------------------
# performance.py
# ---------------------------------------------------------------------------


class FakeMemInfo:
    def __init__(self, rss):
        self.rss = rss


class FakePsutilProcess:
    def __init__(self, pid=None, cpu=12.34, rss=100 * 1024 * 1024, create_time=1000.0, create_exc=None, cpu_exc=None):
        self.pid = pid
        self._cpu = cpu
        self._rss = rss
        self._create_time = create_time
        self._create_exc = create_exc
        self._cpu_exc = cpu_exc

    def cpu_percent(self, interval=None):
        if self._cpu_exc:
            raise self._cpu_exc
        return self._cpu

    def memory_info(self):
        return FakeMemInfo(self._rss)

    def create_time(self):
        if self._create_exc:
            raise self._create_exc
        return self._create_time


class FakePerfConsole:
    def __init__(self, lines=None, read_exc=None, dropped=False, backwards=False):
        self.lines = list(lines or [])
        self.read_exc = read_exc
        self.dropped = dropped
        self.backwards = backwards
        self.reads = []

    def read_since(self, idx):
        if self.read_exc:
            raise self.read_exc
        self.reads.append(idx)
        if idx <= 0:
            return "".join(self.lines), len(self.lines)
        if self.backwards:
            return "", 0
        return "".join(self.lines[idx:]), len(self.lines)

    def dropped_since(self, idx):
        return self.dropped


class _PerfProc:
    """性能采集用的假 ServerProcess（真实对象，避免 Mock 自动生成属性）。"""

    def __init__(self, pid=1234, start_time=None, players=None):
        self.running = True
        self.proc = mock.Mock(pid=pid)
        if start_time is not None:
            self._start_time = start_time
        self._players = players
        self._use_players = players is not None

    def get_online_players(self):
        return self._players


class _PerfProcNoPlayers:
    """没有 get_online_players 属性的假进程。"""

    def __init__(self, pid=1234, start_time=None):
        self.running = True
        self.proc = mock.Mock(pid=pid)
        if start_time is not None:
            self._start_time = start_time


def _fake_server_proc(pid=1234, start_time=None, players=None):
    if players is None:
        return _PerfProcNoPlayers(pid=pid, start_time=start_time)
    return _PerfProc(pid=pid, start_time=start_time, players=players)


def test_performance_collect_not_running():
    monitor = PerformanceMonitor(max_history=10)
    data = monitor.collect(None, None)
    assert data["running"] is False
    assert len(monitor.get_history()) == 1


def test_performance_collect_proc_none():
    monitor = PerformanceMonitor(max_history=10)
    data = monitor.collect(mock.Mock(running=True, proc=None), None)
    assert data["running"] is True
    assert len(monitor.get_history()) == 1


def test_performance_collect_full(monkeypatch):
    monitor = PerformanceMonitor(max_history=10)
    console = FakePerfConsole(
        [
            "There are 2/10 players online: Steve, Alex",
            "TPS: 18.5",
        ]
    )
    proc = _fake_server_proc(start_time=time.time() - 100)
    with (
        mock.patch.object(performance.psutil, "Process", return_value=FakePsutilProcess()),
        mock.patch.object(performance.psutil, "virtual_memory", return_value=mock.Mock(total=8 * 1024 * 1024 * 1024)),
    ):
        data = monitor.collect(proc, console)
    assert data["running"] is True
    assert data["cpu_percent"] == 12.3
    assert data["memory_mb"] == 100.0
    assert data["memory_percent"] == 1.2
    assert data["players"] == 2
    assert data["tps"] == 18.5
    assert 99 <= data["uptime_seconds"] <= 101


def test_performance_collect_psutil_start_time_fallback():
    monitor = PerformanceMonitor(max_history=10)
    proc = _fake_server_proc()  # 无 _start_time
    with (
        mock.patch.object(performance.psutil, "Process", return_value=FakePsutilProcess(create_time=1000.0)),
        mock.patch.object(performance.psutil, "virtual_memory", return_value=mock.Mock(total=8 * 1024 * 1024 * 1024)),
    ):
        data = monitor.collect(proc, None)
    assert data["uptime_seconds"] > 0


def test_performance_collect_start_time_errors():
    monitor = PerformanceMonitor(max_history=10)
    proc = _fake_server_proc()
    fake = FakePsutilProcess(create_exc=OSError("gone"), cpu_exc=OSError("gone"))
    with mock.patch.object(performance.psutil, "Process", return_value=fake):
        data = monitor.collect(proc, None)
    assert data["uptime_seconds"] == 0
    assert data["cpu_percent"] == 0.0


def test_performance_collect_virtual_memory_error():
    monitor = PerformanceMonitor(max_history=10)
    proc = _fake_server_proc(start_time=time.time() - 5)
    with (
        mock.patch.object(performance.psutil, "Process", return_value=FakePsutilProcess()),
        mock.patch.object(performance.psutil, "virtual_memory", side_effect=performance.psutil.Error("no mem")),
    ):
        data = monitor.collect(proc, None)
    assert data["memory_mb"] == 100.0
    assert data["memory_percent"] == 0.0


def test_performance_collect_process_error():
    monitor = PerformanceMonitor(max_history=10)
    proc = _fake_server_proc(start_time=time.time() - 5)
    with mock.patch.object(performance.psutil, "Process", side_effect=performance.psutil.Error("no proc")):
        data = monitor.collect(proc, None)
    assert data["cpu_percent"] == 0.0
    assert data["memory_mb"] == 0.0


def test_performance_collect_console_read_error():
    monitor = PerformanceMonitor(max_history=10)
    proc = _fake_server_proc(start_time=time.time() - 5)
    console = FakePerfConsole(read_exc=OSError("console gone"))
    with (
        mock.patch.object(performance.psutil, "Process", return_value=FakePsutilProcess()),
        mock.patch.object(performance.psutil, "virtual_memory", return_value=mock.Mock(total=8 * 1024 * 1024 * 1024)),
    ):
        data = monitor.collect(proc, console)
    assert data["players"] == 0
    assert data["tps"] == 0.0


def test_performance_collect_cursor_backwards_rereads():
    monitor = PerformanceMonitor(max_history=10)
    monitor._last_console_idx = 10
    console = FakePerfConsole(["There are 1/10 players online: Steve"], backwards=True)
    proc = _fake_server_proc(start_time=time.time() - 5)
    with (
        mock.patch.object(performance.psutil, "Process", return_value=FakePsutilProcess()),
        mock.patch.object(performance.psutil, "virtual_memory", return_value=mock.Mock(total=8 * 1024 * 1024 * 1024)),
    ):
        data = monitor.collect(proc, console)
    assert console.reads == [10, 0]
    assert data["players"] == 1


def test_performance_collect_dropped_window_rereads():
    monitor = PerformanceMonitor(max_history=10)
    monitor._last_console_idx = 0
    console = FakePerfConsole(["There are 2/10 players online: A, B"], dropped=True)
    proc = _fake_server_proc(start_time=time.time() - 5)
    with (
        mock.patch.object(performance.psutil, "Process", return_value=FakePsutilProcess()),
        mock.patch.object(performance.psutil, "virtual_memory", return_value=mock.Mock(total=8 * 1024 * 1024 * 1024)),
    ):
        data = monitor.collect(proc, console)
    assert console.reads == [0, 0]
    assert data["players"] == 2


def test_performance_collect_get_online_players():
    monitor = PerformanceMonitor(max_history=10)
    proc = _fake_server_proc(start_time=time.time() - 5, players=["a", "b", "c"])
    with (
        mock.patch.object(performance.psutil, "Process", return_value=FakePsutilProcess()),
        mock.patch.object(performance.psutil, "virtual_memory", return_value=mock.Mock(total=8 * 1024 * 1024 * 1024)),
    ):
        data = monitor.collect(proc, None)
        assert data["players"] == 3
        proc._players = []
        data = monitor.collect(proc, None)
    assert data["players"] == 3  # 空列表沿用快照


def test_performance_collect_outer_exception():
    monitor = PerformanceMonitor(max_history=10)

    class _BadProc:
        @property
        def running(self):
            raise OSError("running boom")

    data = monitor.collect(_BadProc(), None)
    assert data["running"] is False
    assert len(monitor.get_history()) == 1


def test_performance_parse_tps():
    monitor = PerformanceMonitor(max_history=10)
    monitor._current_tps = 7.0
    assert monitor._parse_tps(None) == 7.0
    assert monitor._parse_tps("") == 7.0
    assert monitor._parse_tps("TPS: 20.0") == 20.0
    assert monitor._parse_tps("ticks per second: 15.5") == 15.5
    assert monitor._parse_tps("Average tick time: 50ms") == 20.0
    assert monitor._parse_tps("Average tick time: 0ms") == 0.0
    assert monitor._parse_tps("TPS: 99") == 20.0
    with mock.patch.object(performance.re, "findall", return_value=["bad"]):
        assert monitor._parse_tps("TPS: bad") == 0.0


def test_performance_history_summary_current():
    monitor = PerformanceMonitor(max_history=10)
    assert monitor.get_current()["running"] is False
    monitor._history.extend(
        [
            {"cpu_percent": 10.0, "memory_mb": 100.0, "players": 2, "tps": 19.0},
            {"cpu_percent": 20.0, "memory_mb": 200.0, "players": 4, "tps": 20.0},
            {"cpu_percent": 0.0, "memory_mb": 0.0, "players": 0, "tps": 0.0},
        ]
    )
    assert len(monitor.get_history(points=2)) == 2
    assert len(monitor.get_history(points=100)) == 3
    summary = monitor.get_summary()
    assert summary["cpu_avg"] == 15.0
    assert summary["cpu_max"] == 20.0
    assert summary["cpu_min"] == 10.0
    assert summary["mem_avg"] == 150.0
    assert summary["mem_max"] == 200.0
    assert summary["mem_min"] == 100.0
    assert summary["players_avg"] == 2.0
    assert summary["players_max"] == 4
    assert summary["tps_avg"] == 19.5
    assert summary["tps_min"] == 19.0
    assert summary["data_points"] == 3
    current = monitor.get_current()
    assert current["cpu_percent"] == 0.0
    assert current is not monitor._history[-1]  # 返回副本


def test_performance_summary_all_zero():
    monitor = PerformanceMonitor(max_history=10)
    monitor._history.append({"cpu_percent": 0.0, "memory_mb": 0.0, "players": 0, "tps": 0.0})
    summary = monitor.get_summary()
    assert summary["cpu_avg"] == 0
    assert summary["tps_avg"] == 0
    assert summary["data_points"] == 1


def test_performance_reset_and_module_helpers():
    monitor = PerformanceMonitor(max_history=10)
    monitor._history.append({"cpu_percent": 1.0, "memory_mb": 1.0, "players": 1, "tps": 1.0})
    monitor._current_tps = 5.0
    monitor._last_console_idx = 3
    monitor._players_snapshot = 2
    monitor.reset()
    assert monitor.get_history() == []
    assert monitor._current_tps == 0.0
    assert monitor._last_console_idx == 0
    assert monitor._players_snapshot == 0

    assert performance.get_performance_monitor() is performance_monitor
    performance.reset_performance_monitor()
    payload = performance.get_server_performance_full(None, None)
    assert payload["current"]["running"] is False
    assert payload["max_history"] == performance_monitor.max_history
    assert payload["history"] == performance_monitor.get_history(points=60)
    assert payload["summary"]["data_points"] == 1


# ---------------------------------------------------------------------------
# 补充：剩余分支（日志不可用 / API 非 403 / 停止轮询 / 在线人数异常）
# ---------------------------------------------------------------------------


def test_check_app_update_api_http_error_non_403():
    html_err = urllib.error.HTTPError("https://x", 500, "HTML down", {}, None)
    api_err = urllib.error.HTTPError("https://x", 500, "API down", {}, None)
    with (
        mock.patch.object(app_update, "_check_via_html", side_effect=html_err),
        mock.patch.object(app_update, "_check_via_api", side_effect=api_err),
    ):
        result = app_update.check_app_update(force=True)
    assert result["ok"] is False
    assert "HTTP 500: HTML down" in result["error"]
    assert "HTTP 500: API down" in result["error"]


def test_reader_auto_restart_start_failure(tmp_path):
    sp, out, states = _new_sp(tmp_path)
    sp.proc = _ReaderProc([], returncode=1)
    sp._start_time = time.time() - 120
    sp._user_stopped = False
    sp.start = mock.Mock(side_effect=RuntimeError("cannot restart"))
    AppContext.instance().settings["auto_restart_on_crash"] = True
    captured, fake_thread = _thread_capture()
    with (
        mock.patch.object(server.threading, "Thread", fake_thread),
        mock.patch.object(server.time, "sleep"),
    ):
        sp._reader()
        captured[0].target(*captured[0].args)
    assert any("自动重启失败" in line and "cannot restart" in line for line in out)
    assert states == []


def test_stop_polls_until_exit(tmp_path):
    sp, _, _ = _new_sp(tmp_path)

    class _SlowStopProc:
        def __init__(self):
            self.returncode = None
            self.polls = 0
            self.stdin = mock.Mock()

        def poll(self):
            self.polls += 1
            # stop() 自身 1 次、send() 内部 1 次、while 两次 Running 判定后才退出
            return None if self.polls <= 4 else 0

        def kill(self):
            self.returncode = -9

    fake = _SlowStopProc()
    sp.proc = fake
    with mock.patch.object(server.time, "sleep"):
        sp.stop(wait=5)
    assert fake.polls >= 3
    assert sp._user_stopped is True


def test_stop_and_kill_when_app_logger_fails(tmp_path):
    sp, _, states = _new_sp(tmp_path)
    fake = FakePopen(["x"])

    def boom(data):
        raise OSError("stdin closed")

    fake.stdin.write = boom
    sp.proc = fake

    broken = mock.Mock()
    broken.warning.side_effect = RuntimeError("logger boom")
    with mock.patch("bedrock_server_manager.app_logger.get_app_logger", return_value=broken):
        sp.stop(wait=0)
    assert fake.killed is True
    assert states[-1] is False

    sp2, _, states2 = _new_sp(tmp_path)
    fake2 = FakePopen(["x"])
    fake2.kill = mock.Mock(side_effect=OSError("cannot kill"))
    sp2.proc = fake2
    with mock.patch("bedrock_server_manager.app_logger.get_app_logger", return_value=broken):
        sp2.kill()
    assert states2 == [False]


def test_performance_collect_get_online_players_error():
    monitor = PerformanceMonitor(max_history=10)

    class _BadPlayers(_PerfProcNoPlayers):
        def get_online_players(self):
            raise OSError("players boom")

    proc = _BadPlayers(start_time=time.time() - 5)
    with (
        mock.patch.object(performance.psutil, "Process", return_value=FakePsutilProcess()),
        mock.patch.object(performance.psutil, "virtual_memory", return_value=mock.Mock(total=8 * 1024 * 1024 * 1024)),
    ):
        data = monitor.collect(proc, None)
    assert data["players"] == 0


def test_performance_parse_tps_outer_exception():
    monitor = PerformanceMonitor(max_history=10)
    with mock.patch.object(performance.re, "findall", side_effect=OSError("regex boom")):
        assert monitor._parse_tps("TPS: 20") == 0.0


def test_performance_summary_empty():
    monitor = PerformanceMonitor(max_history=10)
    summary = monitor.get_summary()
    assert summary == {
        "cpu_avg": 0,
        "cpu_max": 0,
        "cpu_min": 0,
        "mem_avg": 0,
        "mem_max": 0,
        "mem_min": 0,
        "players_avg": 0,
        "players_max": 0,
        "tps_avg": 0,
        "tps_min": 0,
        "data_points": 0,
    }


def test_performance_collect_start_time_attribute_error():
    monitor = PerformanceMonitor(max_history=10)

    class _BadStartTime(_PerfProcNoPlayers):
        @property
        def _start_time(self):
            raise OSError("start time boom")

    proc = _BadStartTime()
    with (
        mock.patch.object(performance.psutil, "Process", return_value=FakePsutilProcess(cpu_exc=OSError("gone"))),
        mock.patch.object(performance.psutil, "virtual_memory", return_value=mock.Mock(total=8 * 1024 * 1024 * 1024)),
    ):
        data = monitor.collect(proc, None)
    assert data["uptime_seconds"] == 0
    assert data["running"] is True
