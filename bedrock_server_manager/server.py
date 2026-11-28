"""
服务器进程管理模块：ServerProcess、进程启停、状态检测。
"""

import contextlib
import os
import socket
import subprocess
import threading
import time

from .app_context import AppContext
from .app_logger import safe_log_exception
from .config import load_properties
from .console import ConsoleBuffer
from .constants import IS_WINDOWS, SERVER_EXE


# 通过 AppContext 访问全局状态（消除延迟导入）
def _get_settings():
    return AppContext.instance().settings


def _get_console():
    return AppContext.instance().console


def _get_server_proc_state():
    ctx = AppContext.instance()
    return ctx.server_proc, ctx.server_lock


def _get_constants():
    """获取服务器相关常量（平台相关：Windows / Linux）。"""
    return SERVER_EXE, IS_WINDOWS


def check_port_in_use(port: int, host: str = "0.0.0.0") -> bool:
    """检测指定 UDP 端口是否被占用。

    Bedrock 服务端使用 **UDP**（默认 19132）。早期实现用 TCP ``connect_ex``
    探测同一个端口号，对 UDP 服务恒为"未占用"，等于没有检测；
    这里改为 UDP ``bind``，与 ``web/routes/server.py`` 的启动前检测保持一致。

    Args:
        port: 端口号
        host: 绑定地址（默认 0.0.0.0，与官方服务端默认监听一致）

    Returns:
        bool: True 表示端口被占用，False 表示端口可用
    """
    # 端口范围校验：非法端口会让 bind 抛 OverflowError（不是 OSError），
    # 逃出下面的 except 直接把调用方打断。非法端口按"不可用"处理更安全。
    if not isinstance(port, int) or not (0 < port < 65536):
        return True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # Windows 下 UDP 默认允许复用，先关闭复用才能可靠判断占用
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                with contextlib.suppress(OSError):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((host, port))
        return False
    except OSError as e:
        # 只把"地址/端口已被占用"类错误当成占用；EADDRNOTAVAIL（本机没有这个
        # IP）、EACCES（Windows 保留端口段）等属于"探测环境问题"，
        # 误判成占用会让用户被永久拒绝启动且无从排查。
        errno_ = getattr(e, "errno", None)
        winerror = getattr(e, "winerror", None)
        addr_in_use = {98, 48}  # EADDRINUSE(linux) / WSAEADDRINUSE
        if errno_ in addr_in_use or winerror == 10048:
            return True
        safe_log_exception("server", f"端口探测异常（按可用处理）: {e}", "warning")
        return False
    except Exception as e:
        safe_log_exception("server", f"端口探测异常（按不可用处理）: {e}", "warning")
        return True


def get_server_port(server_dir: str) -> int:
    """从 server.properties 读取服务器端口，默认 19132。

    Args:
        server_dir: 服务器目录

    Returns:
        int: 服务器端口号
    """
    try:
        props = load_properties(os.path.join(server_dir, "server.properties"))
        for key, value in props:
            if key == "server-port":
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return 19132
    except Exception as e:
        # 配置文件不存在或解析失败时返回默认端口 19132
        safe_log_exception("server", f"get_server_port 忽略异常: {e}", "debug")
    return 19132


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
            try:
                from .utils import resolve_server_exe

                exe_name = resolve_server_exe(self.server_dir) or SERVER_EXE
                exe = os.path.join(self.server_dir, exe_name)
                if not os.path.exists(exe):
                    raise FileNotFoundError(f"未找到 {SERVER_EXE}，请先安装服务器。")
                # 端口占用检测：所有启动入口（Web 路由 / 托盘 / 安装后自动启动 /
                # 调度重启）都会走到这里，所以在这里拒绝一次即可避免
                # "启动后立刻退出 → 崩溃重启退避"的循环。
                # 早期只有 Web 路由做预检，其它入口直接 Popen，用户只会看到
                # 服务器反复启动又退出，且日志里没有任何端口冲突提示。
                server_port = get_server_port(self.server_dir)
                if check_port_in_use(server_port):
                    self._emit(f"\n[系统] ✗ 无法启动：端口 {server_port}（UDP）已被其它程序占用。\n")
                    self._emit(
                        f"[系统] 请关闭占用端口 {server_port} 的程序，或在「服务器配置」中修改 server-port 后重试。\n"
                    )
                    raise RuntimeError(
                        f"端口 {server_port} 已被占用，无法启动服务器（请关闭占用该端口的程序，或修改 server-port）"
                    )
                flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0  # Windows: 不创建控制台窗口；Linux: 无此标志
                self._user_stopped = False
                self._exit_code = None
                self._start_time = time.time()
                # 如果距离上次崩溃超过 CRASH_RESET_SECONDS，重置崩溃计数器
                if self._last_crash_time and (time.time() - self._last_crash_time) > self.CRASH_RESET_SECONDS:
                    self._crash_restart_count = 0
                try:
                    self.proc = subprocess.Popen(
                        [exe],
                        cwd=self.server_dir,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        creationflags=flags,
                    )
                except PermissionError:
                    # Linux 平台：二进制缺少可执行权限时给出明确提示
                    if not IS_WINDOWS:
                        raise RuntimeError(
                            f"无法启动 {SERVER_EXE}：缺少可执行权限。请执行: chmod +x {exe}"
                        ) from PermissionError
                    raise
                self.reader_thread = threading.Thread(target=self._reader, daemon=True)
                self.reader_thread.start()
                self.on_state(True)
            except FileNotFoundError:
                raise
            except (PermissionError, OSError) as e:
                safe_log_exception("server.py", f"启动服务器失败: {e}", "error")
                raise RuntimeError(f"启动服务器失败: {e}") from e
            except Exception as e:
                safe_log_exception("server.py", f"启动服务器时发生未知异常: {e}", "error")
                raise

    def _reader(self):
        proc = self.proc
        settings = _get_settings()
        # 标记是否已安排自动重启（若为 True，则不触发 on_state(False)，由重启线程接管）
        auto_restarting = False
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                self._emit(self._decode(line))
        except Exception as e:
            safe_log_exception("server", f"_reader 忽略异常: {e}", "warning")
            self._emit(f"[日志读取中断: {e}]\n")
        finally:
            # 等待进程完全退出并获取退出码
            try:
                proc.wait(timeout=5)
                self._exit_code = proc.returncode
            except Exception as e:
                safe_log_exception("server", f"_reader 忽略异常: {e}", "warning")
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
                            f"[系统] ⚠ 连续崩溃 {self._crash_restart_count} 次，已达到最大重启次数限制，停止自动重启。\n"
                        )
                        self._emit("[系统] 请检查服务器配置、端口占用或世界存档是否损坏。\n")
                    else:
                        # 指数退避：第n次重启等待 2^n 秒
                        backoff = min(self.CRASH_BACKOFF_BASE**self._crash_restart_count, 60)
                        self._emit(
                            f"[系统] 第 {self._crash_restart_count}/{self.MAX_CRASH_RESTARTS} 次自动重启，{backoff} 秒后重试...\n"
                        )

                        # 在单独线程中执行延迟重启，避免阻塞 _reader 线程
                        def _delayed_restart(delay):
                            time.sleep(delay)
                            # 等待期间用户已点击"停止"，取消自动重启（防止服务器"复活"）
                            if self._user_stopped:
                                self._emit("[系统] 自动重启已取消（用户已停止服务器）\n")
                            else:
                                try:
                                    self.start()
                                except Exception as e:
                                    safe_log_exception("server", f"_delayed_restart 忽略异常: {e}", "warning")
                                    self._emit(f"[系统] 自动重启失败: {e}\n")

                        threading.Thread(target=_delayed_restart, args=(backoff,), daemon=True).start()
                        auto_restarting = True
        # 若已安排自动重启，则由重启线程接管状态，不再通知"已停止"
        if not auto_restarting:
            self.on_state(False)

    @staticmethod
    def _decode(buf):
        try:
            return buf.decode("utf-8", errors="replace")
        except Exception as e:
            safe_log_exception("server", f"_decode 忽略异常: {e}", "warning")
            return buf.decode("gbk", errors="replace")

    def _emit(self, line):
        try:
            self.on_output(line)
        except Exception as e:
            safe_log_exception("server", f"_emit 忽略异常: {e}", "warning")
            try:
                from .app_logger import get_app_logger

                get_app_logger().warning(f"server.py on_output 异常: {e}")
            except Exception as e:
                # 应用日志不可用时静默，避免二次异常
                safe_log_exception("server", f"_emit 忽略异常: {e}", "warning")

    def send(self, cmd):
        if not self.running:
            raise RuntimeError("服务器未运行")
        self.proc.stdin.write((cmd + "\n").encode("utf-8", errors="replace"))
        self.proc.stdin.flush()

    def stop(self, wait=15):
        # 无论进程是否在运行，都标记用户停止意图——
        # 崩溃退避等待期间用户点击"停止"时，进程可能已退出，
        # 此标记用于取消尚未触发的延迟自动重启（防止服务器"复活"）
        self._user_stopped = True
        if not self.running:
            return
        try:
            self.send("stop")
        except Exception as e:
            safe_log_exception("server", f"stop 忽略异常: {e}", "warning")
            try:
                from .app_logger import get_app_logger

                get_app_logger().warning(f"server.py 发送 stop 命令异常: {e}")
            except Exception as e:
                # 应用日志不可用时静默，之后会走 kill 兜底
                safe_log_exception("server", f"stop 忽略异常: {e}", "warning")
        t0 = time.time()
        while self.running and time.time() - t0 < wait:
            time.sleep(0.2)
        if self.running:
            self.kill()

    def kill(self):
        with self._lock:
            if self.proc and self.running:
                self._user_stopped = True
                try:
                    self.proc.kill()
                except Exception as e:
                    safe_log_exception("server", f"kill 忽略异常: {e}", "warning")
                    try:
                        from .app_logger import get_app_logger

                        get_app_logger().warning(f"server.py 杀死进程异常: {e}")
                    except Exception as e:
                        # 应用日志不可用时静默，进程退出由系统兜底
                        safe_log_exception("server", f"kill 忽略异常: {e}", "warning")
        self.on_state(False)


# ---------------------------------------------------------------------------
# 多实例注册表
#
# 每个服务器目录拥有独立的 ServerProcess 与 ConsoleBuffer；"当前实例"由
# settings["server_dir"] 表示（沿用原有语义，66 处调用无需改动），
# 切换实例时把 AppContext.console / server_proc 指向该实例的对象。
# 注册表持久化在 settings["instances"]。
# ---------------------------------------------------------------------------

_instances_lock = threading.RLock()
_instances: dict[str, dict] = {}


def _instance_key(dir_path: str) -> str:
    return os.path.normcase(os.path.abspath(dir_path)) if dir_path else ""


def _get_instance(server_dir: str) -> dict:
    """获取/创建某目录的实例运行对象（进程 + 独立控制台）。"""
    key = _instance_key(server_dir)
    if not key:
        raise ValueError("未设置服务器目录")
    with _instances_lock:
        inst = _instances.get(key)
        if inst is not None:
            return inst
        console = ConsoleBuffer()

        def on_out(line, _console=console):
            _console.append(line)

        def on_state(running):
            pass

        proc = ServerProcess(server_dir, on_output=on_out, on_state=on_state)
        inst = {"dir": server_dir, "console": console, "proc": proc}
        _instances[key] = inst
        return inst


def instance_dirs() -> list[dict]:
    """注册表内容（settings["instances"]）。"""
    raw = AppContext.instance().settings.get("instances")
    result: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("dir"):
                result.append({"dir": str(item["dir"]), "name": str(item.get("name") or "")})
    return result


def _save_instance_dirs(entries: list[dict]) -> None:
    from .utils import save_settings

    settings = AppContext.instance().settings
    settings["instances"] = entries
    save_settings(settings)


def register_instance(server_dir: str, name: str = "") -> dict:
    """把服务器目录加入注册表（幂等）。"""
    server_dir = os.path.abspath(server_dir)
    if not os.path.isdir(server_dir):
        raise ValueError("目录不存在")
    entries = instance_dirs()
    for entry in entries:
        if _instance_key(entry["dir"]) == _instance_key(server_dir):
            if name and name != entry["name"]:
                entry["name"] = name
                _save_instance_dirs(entries)
            return entry
    entry = {"dir": server_dir, "name": name or os.path.basename(server_dir.rstrip("\\/")) or server_dir}
    entries.append(entry)
    _save_instance_dirs(entries)
    return entry


def unregister_instance(server_dir: str) -> bool:
    """从注册表移除（运行中会先停止）。"""
    key = _instance_key(server_dir)
    inst = _instances.get(key)
    if inst is not None and inst["proc"].running:
        inst["proc"].stop()
    with _instances_lock:
        _instances.pop(key, None)
    entries = [e for e in instance_dirs() if _instance_key(e["dir"]) != key]
    _save_instance_dirs(entries)
    if _instance_key(AppContext.instance().settings.get("server_dir", "")) == key:
        if entries:
            activate_instance(entries[0]["dir"])
        else:
            AppContext.instance().settings["server_dir"] = ""
            _save_instance_dirs(entries)
    return True


def list_instances() -> list[dict]:
    """实例列表（含运行状态/端口/版本/是否为当前实例）。"""
    settings = AppContext.instance().settings
    active_key = _instance_key(settings.get("server_dir", ""))
    result: list[dict] = []
    for entry in instance_dirs():
        dir_path = entry["dir"]
        key = _instance_key(dir_path)
        inst = _instances.get(key)
        running = bool(inst and inst["proc"].running)
        port = 19132
        version = ""
        with contextlib.suppress(Exception):
            port = get_server_port(dir_path)
        with contextlib.suppress(Exception):
            from .install import detect_server_version

            version = detect_server_version(dir_path)
        result.append(
            {
                "dir": dir_path,
                "name": entry.get("name") or os.path.basename(dir_path),
                "running": running,
                "port": port,
                "version": version,
                "active": key == active_key,
            }
        )
    return result


def activate_instance(server_dir: str) -> dict:
    """切换当前实例：settings.server_dir、AppContext.console/server_proc 一起切。"""
    server_dir = os.path.abspath(server_dir)
    if not os.path.isdir(server_dir):
        raise ValueError("目录不存在")
    ctx = AppContext.instance()
    inst = _get_instance(server_dir)
    ctx.settings["server_dir"] = server_dir
    ctx.console = inst["console"]
    ctx.server_proc = inst["proc"]
    with contextlib.suppress(Exception):
        from .utils import save_settings

        save_settings(ctx.settings)
    for item in list_instances():
        if _instance_key(item["dir"]) == _instance_key(server_dir):
            return item
    return {"dir": server_dir, "name": os.path.basename(server_dir), "active": True}


def sync_active_instance() -> None:
    """启动时对齐：注册表为空则收录当前 server_dir，并让 ctx 指向当前实例。"""
    ctx = AppContext.instance()
    current = str(ctx.settings.get("server_dir") or "").strip()
    if current and os.path.isdir(current) and not instance_dirs():
        with contextlib.suppress(Exception):
            register_instance(current)
    if current and os.path.isdir(current):
        with contextlib.suppress(Exception):
            inst = _get_instance(current)
            ctx.console = inst["console"]
            ctx.server_proc = inst["proc"]


def instance_running(server_dir: str) -> bool:
    inst = _instances.get(_instance_key(server_dir))
    return bool(inst and inst["proc"].running)


def start_instance(server_dir: str) -> None:
    _get_instance(server_dir)["proc"].start()


def stop_instance(server_dir: str) -> None:
    inst = _instances.get(_instance_key(server_dir))
    if inst is not None:
        inst["proc"].stop()


def send_to_instance(server_dir: str, command: str) -> bool:
    inst = _instances.get(_instance_key(server_dir))
    if inst is None or not inst["proc"].running:
        return False
    inst["proc"].send(command)
    return True


def get_server_proc():
    """获取当前实例的 ServerProcess（保持原函数语义）。"""
    ctx = AppContext.instance()
    current = str(ctx.settings.get("server_dir") or "")
    if not current:
        return ServerProcess("", on_output=lambda line: None, on_state=lambda running: None)
    return _get_instance(current)["proc"]


def server_running() -> bool:
    """当前实例是否在运行。"""
    current = str(AppContext.instance().settings.get("server_dir") or "")
    if not current:
        return False
    inst = _instances.get(_instance_key(current))
    return bool(inst and inst["proc"].running)
