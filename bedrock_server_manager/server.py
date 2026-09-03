"""
服务器进程管理模块：ServerProcess、进程启停、状态检测。
"""

import contextlib
import os
import subprocess
import threading
import time


# 延迟导入全局状态（避免循环导入）
def _get_settings():
    from .state import settings

    return settings


def _get_console():
    from .state import console

    return console


def _get_server_proc_state():
    from .state import server_lock, server_proc

    return server_proc, server_lock


def _get_constants():
    """获取服务器相关常量（仅 Windows 平台）。"""
    from .state import IS_WINDOWS, SERVER_EXE

    return SERVER_EXE, IS_WINDOWS


class ServerProcess:
    """Bedrock 服务器进程管理器。
    负责启动/停止/重启 bedrock_server.exe，读取 stdout 日志，
    检测进程状态（运行中/已停止/启动失败），并通过回调通知状态变化。"""

    STARTUP_GRACE_SECONDS = 30  # 启动后 30 秒内退出视为启动失败

    def __init__(self, server_dir, on_output, on_state):
        self.server_dir = server_dir
        self.on_output = on_output
        self.on_state = on_state
        self.proc = None
        self.reader_thread = None
        self._lock = threading.Lock()
        self._user_stopped = False  # 用户主动停止标记
        self._start_time = None  # 启动时间戳
        self._exit_code = None  # 最近一次退出码
        self._crash_restart_count = 0  # 连续崩溃重启次数
        self._last_crash_time = None  # 上次崩溃时间
        self.MAX_CRASH_RESTARTS = 5  # 最大连续崩溃重启次数
        self.CRASH_RESET_SECONDS = 300  # 正常运行5分钟后重置崩溃计数器
        self.CRASH_BACKOFF_BASE = 2  # 退避基数（秒）

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        SERVER_EXE, IS_WINDOWS = _get_constants()
        with self._lock:
            if self.running:
                return
            exe = os.path.join(self.server_dir, SERVER_EXE)
            if not os.path.exists(exe):
                raise FileNotFoundError(f"未找到 {SERVER_EXE}，请先安装服务器。")
            flags = subprocess.CREATE_NO_WINDOW  # Windows 平台：不创建控制台窗口
            self._user_stopped = False
            self._exit_code = None
            self._start_time = time.time()
            # 如果距离上次崩溃超过 CRASH_RESET_SECONDS，重置崩溃计数器
            if self._last_crash_time and (time.time() - self._last_crash_time) > self.CRASH_RESET_SECONDS:
                self._crash_restart_count = 0
            self.proc = subprocess.Popen(
                [exe],
                cwd=self.server_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            self.reader_thread = threading.Thread(target=self._reader, daemon=True)
            self.reader_thread.start()
            self.on_state(True)

    def _reader(self):
        proc = self.proc
        settings = _get_settings()
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                self._emit(self._decode(line))
        except Exception as e:
            self._emit(f"[日志读取中断: {e}]\n")
        finally:
            # 等待进程完全退出并获取退出码
            try:
                proc.wait(timeout=5)
                self._exit_code = proc.returncode
            except Exception:
                self._exit_code = proc.poll()
            # 健康监控：检测非用户主动停止的意外退出
            if not self._user_stopped:
                elapsed = time.time() - (self._start_time or time.time())
                if elapsed < self.STARTUP_GRACE_SECONDS:
                    self._emit(f"\n[系统] ⚠ 服务器启动失败（运行 {elapsed:.1f} 秒后退出，退出码: {self._exit_code}）\n")
                    self._emit("[系统] 请检查端口是否被占用、防火墙设置或服务器配置。\n")
                else:
                    self._emit(f"\n[系统] ⚠ 服务器意外退出（运行 {elapsed:.1f} 秒，退出码: {self._exit_code}）\n")
                # 崩溃自动重启（默认关闭，需在设置中开启）
                if settings.get("auto_restart_on_crash", False):
                    self._crash_restart_count += 1
                    self._last_crash_time = time.time()
                    if self._crash_restart_count > self.MAX_CRASH_RESTARTS:
                        self._emit(
                            "[系统] ⚠ 连续崩溃 %d 次，已达到最大重启次数限制，停止自动重启。\n"
                            % self._crash_restart_count
                        )
                        self._emit("[系统] 请检查服务器配置、端口占用或世界存档是否损坏。\n")
                    else:
                        # 指数退避：第n次重启等待 2^n 秒
                        backoff = min(self.CRASH_BACKOFF_BASE**self._crash_restart_count, 60)
                        self._emit(
                            "[系统] 第 %d/%d 次自动重启，%d 秒后重试...\n"
                            % (self._crash_restart_count, self.MAX_CRASH_RESTARTS, backoff)
                        )

                        # 在单独线程中执行延迟重启，避免阻塞 _reader 线程
                        def _delayed_restart(delay):
                            time.sleep(delay)
                            try:
                                self.start()
                            except Exception as e:
                                self._emit(f"[系统] 自动重启失败: {e}\n")

                        threading.Thread(target=_delayed_restart, args=(backoff,), daemon=True).start()
                        return  # 不触发 on_state(False)，重启线程会处理
            self.on_state(False)

    @staticmethod
    def _decode(buf):
        try:
            return buf.decode("utf-8", errors="replace")
        except Exception:
            return buf.decode("gbk", errors="replace")

    def _emit(self, line):
        with contextlib.suppress(Exception):
            self.on_output(line)

    def send(self, cmd):
        if not self.running:
            raise RuntimeError("服务器未运行")
        self.proc.stdin.write((cmd + "\n").encode("utf-8", errors="replace"))
        self.proc.stdin.flush()

    def stop(self, wait=15):
        if not self.running:
            return
        self._user_stopped = True
        with contextlib.suppress(Exception):
            self.send("stop")
        t0 = time.time()
        while self.running and time.time() - t0 < wait:
            time.sleep(0.2)
        if self.running:
            self.kill()

    def kill(self):
        with self._lock:
            if self.proc and self.running:
                self._user_stopped = True
                with contextlib.suppress(Exception):
                    self.proc.kill()
        self.on_state(False)


def get_server_proc():
    """获取或创建 ServerProcess 单例。"""
    from .state import console, server_lock, server_proc, settings

    with server_lock:
        if server_proc is None:

            def on_out(line):
                console.append(line)

            def on_state(running):
                pass

            server_proc = ServerProcess(settings["server_dir"], on_output=on_out, on_state=on_state)
        return server_proc


def server_running():
    """检查服务器进程是否正在运行。"""
    from .state import server_lock, server_proc

    with server_lock:
        return server_proc is not None and server_proc.running
