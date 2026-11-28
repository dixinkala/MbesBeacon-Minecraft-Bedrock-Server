"""内置穿透工具：自动下载并托管 playit / frpc。

设计：

- **只从官方 GitHub Releases 下载**（playit-cloud/playit-agent、fatedier/frp），
  校验 GitHub API 返回的 ``sha256:`` 摘要，失败即删除临时文件
- **权限最小**：只解压 frp 压缩包里的 ``frpc``，拒绝路径穿越
- **进程随程序退出**：atexit + 显式 stop
- **许可**：playit 为 BSD-2-Clause，frp 为 Apache-2.0（见 README 致谢）

平台支持：Windows x86_64 / x86、Linux amd64 / arm64；其它架构提示手动配置。
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import platform
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

from .app_logger import safe_log_exception
from .constants import APP_DATA_DIR, IS_WINDOWS

PLAYIT_REPO = "playit-cloud/playit-agent"
FRP_REPO = "fatedier/frp"
GITHUB_API = "https://api.github.com"
USER_AGENT = "MbesBeacon-builtin-tunnel"
DOWNLOAD_TIMEOUT = 300
MAX_DOWNLOAD_BYTES = 120 * 1024 * 1024
RELEASE_CACHE_TTL = 3600.0

TOOLS_DIR = os.path.join(APP_DATA_DIR, "tools")
PLAYIT_DIR = os.path.join(TOOLS_DIR, "playit")
FRP_DIR = os.path.join(TOOLS_DIR, "frp")


class BuiltinTunnelError(Exception):
    """内置穿透工具安装/运行失败。"""


# --------------------------------------------------------------------------- 平台


def platform_tag() -> tuple[str, str]:
    """返回 (平台, 架构) —— 与 GitHub 资产命名对应。"""
    system = "windows" if IS_WINDOWS else "linux"
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        arch = "x86_64" if IS_WINDOWS else "amd64"
    elif machine in ("i386", "i686", "x86"):
        arch = "x86"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64" if IS_WINDOWS else "arm64"
    else:
        arch = machine or "unknown"
    return system, arch


def _exe(name: str) -> str:
    return f"{name}.exe" if IS_WINDOWS else name


def playit_asset_names() -> tuple[str, str]:
    """playit 客户端与守护进程的资产名。"""
    system, arch = platform_tag()
    if system == "windows" and arch in ("x86_64", "x86"):
        return (f"playit-windows-{arch}-signed.exe", f"playitd-windows-{arch}-signed.exe")
    if system == "linux" and arch in ("amd64", "arm64"):
        return (f"playit-cli-linux-{arch}", f"playit-linux-{arch}")
    raise BuiltinTunnelError(f"playit 暂不支持当前平台: {system}/{arch}")


def frp_asset_name(version: str) -> str:
    """frp 发布包资产名。

    注意 frp 的命名与 playit 不同：Windows 也用 ``amd64`` / ``386``，
    Linux 用 ``amd64`` / ``arm64`` / ``arm``。
    """
    system, arch = platform_tag()
    frp_arch = {"x86_64": "amd64", "x86": "386"}.get(arch, arch)
    if system == "windows" and frp_arch in ("amd64", "386"):
        return f"frp_{version}_windows_{frp_arch}.zip"
    if system == "linux" and frp_arch in ("amd64", "arm64", "arm"):
        return f"frp_{version}_linux_{frp_arch}.tar.gz"
    raise BuiltinTunnelError(f"frp 暂不支持当前平台: {system}/{arch}")


# --------------------------------------------------------------------------- 网络


def _settings():
    from .app_context import AppContext

    return AppContext.instance().settings


_release_cache: dict[str, dict] = {}


def github_headers() -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_release(repo: str, force: bool = False) -> dict:
    """获取仓库最新 release（带 1 小时缓存）。"""
    now = time.time()
    cached = _release_cache.get(repo)
    if cached and not force and now - cached.get("time", 0) < RELEASE_CACHE_TTL:
        return cached["data"]
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers=github_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - 固定 GitHub API
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise BuiltinTunnelError(f"获取 {repo} 最新版本失败: {exc}") from exc
    if not isinstance(data, dict) or "assets" not in data:
        raise BuiltinTunnelError(f"{repo} 响应格式异常")
    _release_cache[repo] = {"time": now, "data": data}
    return data


def find_asset(release: dict, name: str) -> dict:
    for asset in release.get("assets") or []:
        if asset.get("name") == name:
            return asset
    raise BuiltinTunnelError(f"发布里找不到资产: {name}")


def asset_digest(asset: dict) -> str:
    """取 ``sha256:...`` 摘要（GitHub 新字段），没有则返回空串。"""
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1].strip().lower()
    return ""


def download_file(url: str, dest_dir: str, expected_sha256: str = "", timeout: int = DOWNLOAD_TIMEOUT) -> str:
    """下载到临时文件 -> 校验 -> 原子改名；失败删除临时文件。"""
    os.makedirs(dest_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="dl-", dir=dest_dir)
    os.close(fd)
    digest = hashlib.sha256()
    written = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as handle:  # noqa: S310
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise BuiltinTunnelError("下载超过大小上限，已中止")
                digest.update(chunk)
                handle.write(chunk)
        actual = digest.hexdigest()
        if expected_sha256 and actual != expected_sha256.lower():
            raise BuiltinTunnelError(f"SHA256 校验失败（期望 {expected_sha256[:12]}…，实际 {actual[:12]}…）")
        return tmp
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _atomic_move(tmp: str, target: str) -> None:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with contextlib.suppress(OSError):
        os.unlink(target)
    os.replace(tmp, target)


def _make_executable(path: str) -> None:
    if IS_WINDOWS:
        return
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_meta(directory: str, meta: dict) -> None:
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)


def _read_meta(directory: str) -> dict:
    try:
        with open(os.path.join(directory, "meta.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------- 安装


def playit_paths() -> dict:
    return {
        "cli": os.path.join(PLAYIT_DIR, "playit.exe" if IS_WINDOWS else "playit"),
        "daemon": os.path.join(PLAYIT_DIR, "playitd.exe" if IS_WINDOWS else "playitd"),
        "log": os.path.join(PLAYIT_DIR, "playitd.log"),
    }


def secret_candidates() -> list[str]:
    """playit 默认 secret 位置（按可能性排序）。"""
    if IS_WINDOWS:
        program_data = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
        return [
            os.path.join(program_data, "playit_gg", "playit.toml"),
            os.path.join(program_data, "playit_gg", "secret.toml"),
        ]
    home = os.path.expanduser("~")
    return [
        "/etc/playit/playit.toml",
        os.path.join(home, ".config", "playit_gg", "playit.toml"),
        os.path.join(home, ".local", "share", "playit_gg", "playit.toml"),
    ]


def playit_installed() -> bool:
    paths = playit_paths()
    return os.path.isfile(paths["cli"]) and os.path.isfile(paths["daemon"])


def install_playit(progress=None) -> dict:
    """下载并安装 playit（客户端 + 守护进程）。"""
    cli_name, daemon_name = playit_asset_names()
    release = fetch_release(PLAYIT_REPO)
    version = str(release.get("tag_name") or "")
    paths = playit_paths()
    os.makedirs(PLAYIT_DIR, exist_ok=True)
    for name, target in ((cli_name, paths["cli"]), (daemon_name, paths["daemon"])):
        asset = find_asset(release, name)
        if progress:
            progress(f"下载 {name} …")
        tmp = download_file(str(asset.get("browser_download_url")), PLAYIT_DIR, asset_digest(asset))
        try:
            _atomic_move(tmp, target)
        finally:
            if os.path.exists(tmp):
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
        _make_executable(target)
    _write_meta(
        PLAYIT_DIR,
        {"tool": "playit", "version": version, "installed_at": _now(), "source": f"https://github.com/{PLAYIT_REPO}"},
    )
    return {"installed": True, "version": version}


def frp_installed() -> bool:
    return os.path.isfile(os.path.join(FRP_DIR, _exe("frpc")))


def extract_frpc(archive_path: str, target_dir: str) -> str:
    """从 frp 发布包里只取出 frpc 可执行文件（拒绝路径穿越）。

    下载落地的是无后缀临时文件，因此按内容判断是 zip 还是 tar，
    不能看文件名后缀。
    """
    wanted = _exe("frpc")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, wanted)
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                name = os.path.basename(info.filename)
                if name != wanted or info.is_dir():
                    continue
                _atomic_move_from_zip(zf, info, target)
                return target
    else:
        with tarfile.open(archive_path) as tf:
            for member in tf.getmembers():
                name = os.path.basename(member.name)
                if name != wanted or not member.isfile():
                    continue
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                data = extracted.read()
                with open(target, "wb") as handle:
                    handle.write(data)
                _make_executable(target)
                return target
    raise BuiltinTunnelError("发布包里没有找到 frpc")


def _atomic_move_from_zip(zf: zipfile.ZipFile, info: zipfile.ZipInfo, target: str) -> None:
    data = zf.read(info)
    dir_name = os.path.dirname(target)
    fd, tmp = tempfile.mkstemp(prefix="frpc-", dir=dir_name)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    _atomic_move(tmp, target)
    _make_executable(target)


def install_frp(progress=None) -> dict:
    """下载 frp 发布包并取出 frpc。"""
    release = fetch_release(FRP_REPO)
    version = str(release.get("tag_name") or "").lstrip("v")
    asset_name = frp_asset_name(version)
    asset = find_asset(release, asset_name)
    os.makedirs(FRP_DIR, exist_ok=True)
    if progress:
        progress(f"下载 {asset_name} …")
    tmp = download_file(str(asset.get("browser_download_url")), FRP_DIR, asset_digest(asset))
    try:
        extract_frpc(tmp, FRP_DIR)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    _write_meta(
        FRP_DIR,
        {"tool": "frp", "version": version, "installed_at": _now(), "source": f"https://github.com/{FRP_REPO}"},
    )
    return {"installed": True, "version": version}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- 进程工具


def _run_once(argv: list, timeout: float = 30.0, cwd: str | None = None) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        creationflags=flags,
        check=False,
    )


class _LineBuffer:
    """线程安全的行缓冲（最多保留 N 行）。"""

    def __init__(self, limit: int = 300):
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._limit = limit
        self.address = ""

    def add(self, line: str) -> None:
        line = line.rstrip("\r\n")
        if not line:
            return
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > self._limit:
                self._lines = self._lines[-self._limit :]
            if not self.address:
                from .tunnel import detect_address

                found = detect_address(self._lines[-15:], "playit")
                if found:
                    self.address = found

    def tail(self, count: int = 40) -> list[str]:
        with self._lock:
            return list(self._lines[-count:])


def _tail_file(path: str, buffer: _LineBuffer, stop_event: threading.Event, poll: float = 0.5) -> None:
    """跟随日志文件追加内容（文件不存在时等待）。"""
    handle = None
    inode = None
    while not stop_event.is_set():
        if handle is None:
            try:
                # 长生命周期读取器：循环里持续追加读取，不能用 with 块；
                # 关闭时机由 stop 事件控制（函数末尾统一 close）。
                handle = open(path, encoding="utf-8", errors="ignore")  # noqa: SIM115
                inode = os.fstat(handle.fileno()).st_ino
                handle.seek(0, os.SEEK_END)
            except OSError:
                time.sleep(poll)
                continue
        line = handle.readline()
        if line:
            buffer.add(line)
            continue
        with contextlib.suppress(OSError):
            if inode is not None and os.stat(path).st_ino != inode:
                handle.close()
                handle = None
                continue
        time.sleep(poll)
    if handle is not None:
        with contextlib.suppress(OSError):
            handle.close()


# --------------------------------------------------------------------------- playit 管理


class PlayitManager:
    """playit 领取与守护进程管理。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._tail_stop = threading.Event()
        self._tail_thread: threading.Thread | None = None
        self._buffer = _LineBuffer()
        self._error = ""
        self._started_at = 0.0
        atexit.register(self.stop)

    # ---- 状态 ----
    def installed_version(self) -> str:
        return str(_read_meta(PLAYIT_DIR).get("version") or "")

    def secret_path(self) -> str:
        """定位 playit secret 文件。

        优先看各平台的默认存储位置（领取后 playit 会把 secret 写在这里），
        再尝试 ``playit secret-path`` —— 该命令需要 playitd 正在运行，所以只作补充。
        """
        for candidate in secret_candidates():
            if candidate and os.path.isfile(candidate):
                return candidate
        cli = playit_paths()["cli"]
        if not os.path.isfile(cli):
            return ""
        try:
            result = _run_once([cli, "secret-path"], timeout=20)
        except (OSError, subprocess.SubprocessError):
            return ""
        for line in (result.stdout or "").splitlines():
            text = line.strip()
            if text.lower().endswith(".toml") and os.path.isfile(text):
                return text
        return ""

    def claimed(self) -> bool:
        path = self.secret_path()
        return bool(path) and os.path.isfile(path)

    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def status(self) -> dict:
        return {
            "installed": playit_installed(),
            "version": self.installed_version(),
            "claimed": self.claimed(),
            "running": self.running(),
            "address": self._buffer.address,
            "started_at": self._started_at,
            "error": self._error,
            "lines": self._buffer.tail(),
            "claim": dict(_settings().get("playit_claim") or {}),
        }

    # ---- 领取 ----
    def claim_start(self) -> dict:
        """生成领取码与领取链接。"""
        cli = playit_paths()["cli"]
        if not os.path.isfile(cli):
            raise BuiltinTunnelError("请先安装 playit")
        try:
            gen = _run_once([cli, "claim", "generate"], timeout=20)
            code = (gen.stdout or "").strip().splitlines()[-1].strip() if gen.stdout else ""
            if not code:
                raise BuiltinTunnelError(f"生成领取码失败: {(gen.stderr or gen.stdout or '').strip()[:200]}")
            url_res = _run_once([cli, "claim", "url", code, "--name", "MbesBeacon"], timeout=20)
            url = (url_res.stdout or "").strip().splitlines()[-1].strip() if url_res.stdout else ""
            if not url.startswith("http"):
                raise BuiltinTunnelError(f"生成领取链接失败: {(url_res.stderr or url_res.stdout or '').strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as exc:
            raise BuiltinTunnelError(f"调用 playit 失败: {exc}") from exc
        settings = _settings()
        settings["playit_claim"] = {"code": code, "url": url, "created_at": _now()}
        self._save()
        return {"code": code, "url": url}

    def claim_check(self, wait: float = 8.0) -> dict:
        """检查领取是否完成（会短轮询一次 exchange）。"""
        cli = playit_paths()["cli"]
        if not os.path.isfile(cli):
            raise BuiltinTunnelError("请先安装 playit")
        claim = _settings().get("playit_claim") or {}
        code = str(claim.get("code") or "")
        if not code:
            raise BuiltinTunnelError("请先生成领取链接")
        try:
            result = _run_once([cli, "claim", "exchange", code, "--wait", str(int(wait))], timeout=wait + 30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise BuiltinTunnelError(f"检查领取状态失败: {exc}") from exc
        claimed = self.claimed() or result.returncode == 0
        if claimed:
            settings = _settings()
            claim["claimed_at"] = _now()
            settings["playit_claim"] = claim
            self._save()
        return {
            "claimed": claimed,
            "output": (result.stdout or "").strip().splitlines()[-3:],
            "secret_path": self.secret_path() if claimed else "",
        }

    # ---- 进程 ----
    def start(self) -> dict:
        if not playit_installed():
            raise BuiltinTunnelError("请先安装 playit")
        if self.running():
            raise BuiltinTunnelError("playit 已在运行")
        secret = self.secret_path()
        if not secret or not os.path.isfile(secret):
            raise BuiltinTunnelError("尚未完成 playit 领取，请先生成领取链接并在网页确认")
        paths = playit_paths()
        os.makedirs(PLAYIT_DIR, exist_ok=True)
        if os.path.exists(paths["log"]):
            with contextlib.suppress(OSError):
                os.unlink(paths["log"])
        argv = [paths["daemon"], "--log-path", paths["log"]]
        if secret:
            argv.extend(["--secret-path", secret])
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except PermissionError as exc:
            raise BuiltinTunnelError(
                f"playitd 无法启动（{exc}）：可能被安全软件拦截，请把 tools\\playit 目录加入排除项"
            ) from exc
        except OSError as exc:
            raise BuiltinTunnelError(f"启动 playitd 失败: {exc}") from exc
        with self._lock:
            self._proc = proc
            self._error = ""
            self._started_at = time.time()
            self._buffer = _LineBuffer()
            self._tail_stop = threading.Event()
        self._tail_thread = threading.Thread(
            target=_tail_file,
            args=(paths["log"], self._buffer, self._tail_stop),
            daemon=True,
        )
        self._tail_thread.start()
        return self.status()

    def stop(self) -> dict:
        self._tail_stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
            except OSError as exc:
                safe_log_exception("builtin_tunnel", f"停止 playitd 失败: {exc}", "debug")
        with self._lock:
            self._proc = None
            self._started_at = 0.0
        return self.status()

    def _save(self) -> None:
        from .utils import save_settings

        save_settings(_settings())


# --------------------------------------------------------------------------- frp 管理


DEFAULT_FRP_CONFIG = {
    "server_addr": "",
    "server_port": 7000,
    "token": "",
    "remote_port": 19132,
    "local_port": 19132,
}


def frp_config() -> dict:
    raw = _settings().get("frp_config")
    data = dict(DEFAULT_FRP_CONFIG)
    if isinstance(raw, dict):
        data.update({key: raw[key] for key in DEFAULT_FRP_CONFIG if key in raw})
    return data


def save_frp_config(**kwargs) -> dict:
    """保存 frp 配置（只接受已知字段并做范围校验）。"""
    from .utils import save_settings

    data = frp_config()
    if "server_addr" in kwargs:
        addr = str(kwargs.get("server_addr") or "").strip()
        if addr and not all(ch.isalnum() or ch in ".-:_" for ch in addr):
            raise BuiltinTunnelError("服务器地址格式不正确")
        data["server_addr"] = addr[:255]
    for key in ("server_port", "remote_port", "local_port"):
        if key in kwargs:
            try:
                value = int(kwargs[key])
            except (TypeError, ValueError) as exc:
                raise BuiltinTunnelError(f"{key} 必须是数字") from exc
            if not 1 <= value <= 65535:
                raise BuiltinTunnelError(f"{key} 超出范围: {value}")
            data[key] = value
    if "token" in kwargs:
        data["token"] = str(kwargs.get("token") or "").strip()[:128]
    settings = _settings()
    settings["frp_config"] = data
    save_settings(settings)
    return data


def render_frp_toml(cfg: dict | None = None) -> str:
    """生成 frpc TOML 配置（frp >= 0.52 语法）。"""
    cfg = cfg or frp_config()
    server_addr = str(cfg.get("server_addr") or "").strip()
    if not server_addr:
        raise BuiltinTunnelError("请先填写 frp 服务器地址")
    auth = ""
    if cfg.get("token"):
        auth = f'auth.token = "{_escape_toml(str(cfg["token"]))}"\n'
    return (
        f'serverAddr = "{_escape_toml(server_addr)}"\n'
        f"serverPort = {int(cfg.get('server_port') or 7000)}\n"
        f"{auth}"
        "\n"
        "[[proxies]]\n"
        'name = "mbesbeacon-bds"\n'
        'type = "udp"\n'
        'localIP = "127.0.0.1"\n'
        f"localPort = {int(cfg.get('local_port') or 19132)}\n"
        f"remotePort = {int(cfg.get('remote_port') or 19132)}\n"
    )


def _escape_toml(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class FrpManager:
    """frpc 进程管理（公网地址 = 服务器地址:远程端口）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._buffer = _LineBuffer()
        self._reader: threading.Thread | None = None
        self._error = ""
        self._started_at = 0.0
        atexit.register(self.stop)

    def installed_version(self) -> str:
        return str(_read_meta(FRP_DIR).get("version") or "")

    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def address(self) -> str:
        cfg = frp_config()
        if not cfg.get("server_addr"):
            return ""
        return f"{cfg['server_addr']}:{int(cfg.get('remote_port') or 19132)}"

    def status(self) -> dict:
        return {
            "installed": frp_installed(),
            "version": self.installed_version(),
            "running": self.running(),
            "address": self.address() if self.running() else "",
            "config": frp_config(),
            "started_at": self._started_at,
            "error": self._error,
            "lines": self._buffer.tail(),
        }

    def start(self) -> dict:
        if not frp_installed():
            raise BuiltinTunnelError("请先安装 frpc")
        if self.running():
            raise BuiltinTunnelError("frpc 已在运行")
        cfg = frp_config()
        if not cfg.get("server_addr"):
            raise BuiltinTunnelError("请先填写 frp 服务器地址")
        config_path = os.path.join(FRP_DIR, "frpc.toml")
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(render_frp_toml(cfg))
        exe = os.path.join(FRP_DIR, _exe("frpc"))
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        try:
            proc = subprocess.Popen(
                [exe, "-c", config_path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                cwd=FRP_DIR,
            )
        except PermissionError as exc:
            raise BuiltinTunnelError(
                f"frpc 无法启动（{exc}）：Windows Defender 等杀毒软件常拦截 frp，"
                "请把 tools\\frp\\frpc.exe 加入排除项后重试"
            ) from exc
        except OSError as exc:
            raise BuiltinTunnelError(f"启动 frpc 失败: {exc}") from exc
        with self._lock:
            self._proc = proc
            self._error = ""
            self._started_at = time.time()
            self._buffer = _LineBuffer()
        self._reader = threading.Thread(target=self._read_loop, args=(proc,), daemon=True)
        self._reader.start()
        return self.status()

    def _read_loop(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            self._buffer.add(raw.decode("utf-8", "ignore"))
        code = proc.poll()
        if code not in (0, None):
            with self._lock:
                if not self._error:
                    self._error = f"frpc 已退出（code={code}）"

    def stop(self) -> dict:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()
            except OSError as exc:
                safe_log_exception("builtin_tunnel", f"停止 frpc 失败: {exc}", "debug")
        with self._lock:
            self._proc = None
            self._started_at = 0.0
        return self.status()


playit = PlayitManager()
frp = FrpManager()
