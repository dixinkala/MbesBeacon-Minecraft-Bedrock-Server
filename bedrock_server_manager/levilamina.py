"""LeviLamina 集成：lip 安装、加载器启用/禁用、插件（mods）管理。

设计原则：

- **不内置/不分发二进制**：lip 与 LeviLamina 都从官方 GitHub Releases 下载并校验 SHA256
- **长任务后台化**：``lip setup`` 要下载约 100 MB 并改写服务端文件，放到后台线程并轮询状态
- **可回退**：启用前备份配置文件；禁用只做"改名 + 恢复官方 exe"，不删除 mods
- **平台限制**：LeviLamina 仅提供 Windows x64 服务端包，非 Windows 只允许查看
- **许可**：LeviLamina 为 LGPL-3.0、lip 的许可见其仓库，均按需下载，不随程序分发
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from urllib.parse import urlparse

from .app_logger import safe_log_exception
from .constants import APP_DATA_DIR, IS_WINDOWS, SERVER_EXE

LIP_REPO = "LiteLDev/lip"
GITHUB_API = "https://api.github.com"
USER_AGENT = "MbesBeacon-LeviLamina"
LIP_DIR = os.path.join(APP_DATA_DIR, "tools", "lip")
MODDED_EXE = "bedrock_server_mod.exe"
OFFICIAL_EXE = SERVER_EXE
MODS_DIR = "mods"
DISABLED_DIR = "mods.disabled"
BACKUP_DIR = "mbes_backup"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_LOG_LINES = 400

_NAME_RE = re.compile(r"^[^/\\\x00]{1,120}$")


class LeviLaminaError(Exception):
    """操作失败（参数不合法、环境不满足、lip 报错等）。"""


# --------------------------------------------------------------------------- 平台


def supported() -> bool:
    """LeviLamina 目前只有 Windows x64 服务端构建。"""
    if not IS_WINDOWS:
        return False
    return os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64").lower() not in ("arm64", "arm")


def lip_exe_path() -> str:
    return os.path.join(LIP_DIR, "lip.exe")


def lip_installed() -> bool:
    return os.path.isfile(lip_exe_path())


# --------------------------------------------------------------------------- lip 安装


def _gitub_headers() -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_lip_release() -> dict:
    url = f"{GITHUB_API}/repos/{LIP_REPO}/releases/latest"
    req = urllib.request.Request(url, headers=_gitub_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - 固定 GitHub API
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise LeviLaminaError(f"获取 lip 最新版本失败: {exc}") from exc
    if not isinstance(data, dict) or "assets" not in data:
        raise LeviLaminaError("lip 发布信息格式异常")
    return data


def lip_asset_name() -> str:
    return "lip-win-x64.zip"


def install_lip(progress=None) -> dict:
    """下载 lip 并解出 lip.exe（校验 SHA256）。"""
    if not supported():
        raise LeviLaminaError("当前平台不支持 LeviLamina（仅 Windows x64）")
    release = fetch_lip_release()
    version = str(release.get("tag_name") or "")
    asset = None
    for item in release.get("assets") or []:
        if item.get("name") == lip_asset_name():
            asset = item
            break
    if asset is None:
        raise LeviLaminaError(f"发布里找不到资产: {lip_asset_name()}")
    digest = str(asset.get("digest") or "")
    expected = digest.split(":", 1)[1].strip().lower() if digest.startswith("sha256:") else ""
    os.makedirs(LIP_DIR, exist_ok=True)
    if progress:
        progress(f"下载 {lip_asset_name()} …")
    fd, tmp = tempfile.mkstemp(prefix="lip-", suffix=".zip", dir=LIP_DIR)
    os.close(fd)
    h = hashlib.sha256()
    written = 0
    try:
        req = urllib.request.Request(str(asset.get("browser_download_url")), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as handle:  # noqa: S310
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise LeviLaminaError("下载超过大小上限，已中止")
                h.update(chunk)
                handle.write(chunk)
        if expected and h.hexdigest() != expected:
            raise LeviLaminaError("lip 下载 SHA256 校验失败")
        with zipfile.ZipFile(tmp) as zf:
            for info in zf.infolist():
                if os.path.basename(info.filename) == "lip.exe" and not info.is_dir():
                    data = zf.read(info)
                    target = lip_exe_path()
                    with open(target, "wb") as handle:
                        handle.write(data)
                    break
            else:
                raise LeviLaminaError("lip 压缩包里没有 lip.exe")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    with open(os.path.join(LIP_DIR, "meta.json"), "w", encoding="utf-8") as handle:
        json.dump({"tool": "lip", "version": version, "installed_at": _now()}, handle, ensure_ascii=False, indent=2)
    return {"installed": True, "version": version}


def lip_version() -> str:
    try:
        with open(os.path.join(LIP_DIR, "meta.json"), encoding="utf-8") as handle:
            return str(json.load(handle).get("version") or "")
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- 版本


def game_version_for_lip(bds_version: str) -> str:
    """把服务端版本转成 lip 需要的三段式游戏版本（1.26.60.6 -> 1.26.60）。"""
    text = (bds_version or "").strip().lstrip("v")
    parts = text.split(".")
    if len(parts) < 3 or not all(part.isdigit() for part in parts[:3]):
        raise LeviLaminaError(f"无法识别的服务端版本: {bds_version!r}")
    return ".".join(parts[:3])


# --------------------------------------------------------------------------- 状态


def state(server_dir: str) -> dict:
    """返回当前服务器的 LeviLamina 状态。"""
    info = {
        "supported": supported(),
        "installed": False,
        "enabled": False,
        "lip_installed": lip_installed(),
        "lip_version": lip_version(),
        "modded_exe": False,
        "loader_version": "",
        "mods": [],
        "bds_version": "",
        "workspace": False,
    }
    if not server_dir or not os.path.isdir(server_dir):
        return info
    modded = os.path.join(server_dir, MODDED_EXE)
    official = os.path.join(server_dir, OFFICIAL_EXE)
    info["modded_exe"] = os.path.isfile(modded)
    info["workspace"] = os.path.isdir(os.path.join(server_dir, ".lip"))
    info["installed"] = info["modded_exe"] or info["workspace"]
    info["enabled"] = info["modded_exe"] and not os.path.isfile(official)
    info["loader_version"] = _loader_version(server_dir)
    info["mods"] = list_mods(server_dir)
    try:
        from .install import detect_server_version

        info["bds_version"] = detect_server_version(server_dir)
    except Exception:  # 版本检测失败不影响状态展示
        info["bds_version"] = ""
    return info


def _loader_version(server_dir: str) -> str:
    manifest = os.path.join(server_dir, MODS_DIR, "LeviLamina", "manifest.json")
    try:
        with open(manifest, encoding="utf-8") as handle:
            return str(json.load(handle).get("version") or "")
    except (OSError, json.JSONDecodeError):
        return ""


# --------------------------------------------------------------------------- 进程


def _run_stream(argv: list, cwd: str, on_line, timeout: float = 1800.0) -> tuple[int, list[str]]:
    """执行命令并逐行回调输出（用于后台任务日志）。"""
    flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
    lines: list[str] = []

    def emit(text: str) -> None:
        text = text.rstrip("\r\n")
        if not text:
            return
        lines.append(text)
        if len(lines) > MAX_LOG_LINES:
            del lines[: len(lines) - MAX_LOG_LINES]
        with contextlib.suppress(Exception):
            on_line(text)

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
    except OSError as exc:
        raise LeviLaminaError(f"启动命令失败: {exc}") from exc
    deadline = time.time() + timeout
    assert proc.stdout is not None
    while True:
        raw = proc.stdout.readline()
        if not raw:
            break
        emit(raw.decode("utf-8", "ignore"))
        if time.time() > deadline:
            with contextlib.suppress(OSError):
                proc.kill()
            raise LeviLaminaError("命令执行超时")
    code = proc.wait()
    return code, lines


def run_lip(args: list[str], cwd: str, timeout: float = 120.0) -> tuple[int, str]:
    """同步执行一次 lip 命令（短命令用）。"""
    exe = lip_exe_path()
    if not os.path.isfile(exe):
        raise LeviLaminaError("尚未安装 lip，请先下载")
    flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
    try:
        result = subprocess.run(
            [exe, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, creationflags=flags, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LeviLaminaError(f"执行 lip 失败: {exc}") from exc
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output.strip()


# --------------------------------------------------------------------------- 启用 / 禁用


class Job:
    """后台长任务（启用 LeviLamina）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.done = False
        self.ok = False
        self.error = ""
        self.lines: list[str] = []
        self.started_at = 0.0

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "done": self.done,
                "ok": self.ok,
                "error": self.error,
                "lines": list(self.lines[-40:]),
                "started_at": self.started_at,
            }

    def _append(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LOG_LINES:
                del self.lines[: len(self.lines) - MAX_LOG_LINES]


job = Job()


def enable_loader(server_dir: str, bds_version: str) -> dict:
    """启用 LeviLamina：备份配置 -> ``lip setup`` -> 校验产物。"""
    if not supported():
        raise LeviLaminaError("当前平台不支持 LeviLamina（仅 Windows x64）")
    if not lip_installed():
        raise LeviLaminaError("请先下载 lip")
    if not server_dir or not os.path.isdir(server_dir):
        raise LeviLaminaError("服务器目录不存在")
    if os.path.isfile(os.path.join(server_dir, MODDED_EXE)):
        raise LeviLaminaError("LeviLamina 已经启用")
    game = game_version_for_lip(bds_version)
    _backup_configs(server_dir)
    argv = [
        lip_exe_path(),
        "setup",
        "--game",
        game,
        "--build",
        "windows",
        "--loader",
        "levilamina",
        "--accept-eula",
    ]
    with job._lock:
        job.running = True
        job.done = False
        job.ok = False
        job.error = ""
        job.lines = []
        job.started_at = time.time()

    def worker():
        try:
            code, lines = _run_stream(argv, server_dir, job._append)
            if code != 0:
                raise LeviLaminaError(f"lip setup 退出码 {code}")
            if not os.path.isfile(os.path.join(server_dir, MODDED_EXE)):
                raise LeviLaminaError("lip setup 完成但未找到 bedrock_server_mod.exe")
            with job._lock:
                job.ok = True
        except Exception as exc:  # 后台线程需兜住所有异常
            safe_log_exception("levilamina", f"启用失败: {exc}", "warning")
            with job._lock:
                job.error = str(exc)
        finally:
            with job._lock:
                job.running = False
                job.done = True

    threading.Thread(target=worker, daemon=True).start()
    return {"started": True, "game": game}


def _backup_configs(server_dir: str) -> str:
    """把配置文件与官方 exe 复制到 mbes_backup/ll_时间戳/。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = os.path.join(server_dir, BACKUP_DIR, f"ll_{stamp}")
    os.makedirs(target, exist_ok=True)
    for name in ("server.properties", "permissions.json", "allowlist.json", "whitelist.json"):
        src = os.path.join(server_dir, name)
        if os.path.isfile(src):
            with contextlib.suppress(OSError):
                shutil.copy2(src, os.path.join(target, name))
    return target


def disable_loader(server_dir: str) -> dict:
    """禁用 LeviLamina：改走官方 exe，保留 mods 与 lip 元数据。"""
    modded = os.path.join(server_dir, MODDED_EXE)
    official = os.path.join(server_dir, OFFICIAL_EXE)
    if not os.path.isfile(modded):
        raise LeviLaminaError("LeviLamina 未启用")
    disabled_exe = modded + ".disabled"
    with contextlib.suppress(OSError):
        os.replace(modded, disabled_exe)
    if not os.path.isfile(official):
        backup = official + ".bak"
        if os.path.isfile(backup):
            os.replace(backup, official)
        else:
            raise LeviLaminaError("找不到官方 bedrock_server.exe，请重新安装服务端")
    return {"disabled": True}


# --------------------------------------------------------------------------- mods


def _mods_root(server_dir: str, disabled: bool = False) -> str:
    return os.path.join(server_dir, DISABLED_DIR if disabled else MODS_DIR)


def _read_manifest(path: str) -> dict:
    """读目录或 zip 里的 manifest.json。"""
    try:
        if os.path.isdir(path):
            manifest = os.path.join(path, "manifest.json")
            if os.path.isfile(manifest):
                with open(manifest, encoding="utf-8") as handle:
                    data = json.load(handle)
                return data if isinstance(data, dict) else {}
        elif path.lower().endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    if name.count("/") <= 1 and name.endswith("manifest.json"):
                        return json.loads(zf.read(name).decode("utf-8", "ignore"))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError):
        return {}
    return {}


def _mod_entry(root: str, name: str, disabled: bool) -> dict:
    path = os.path.join(root, name)
    manifest = _read_manifest(path)
    return {
        "name": str(manifest.get("name") or (os.path.splitext(name)[0] if not os.path.isdir(path) else name)),
        "entry": name,
        "version": str(manifest.get("version") or ""),
        "type": str(manifest.get("type") or ("dir" if os.path.isdir(path) else "zip")),
        "enabled": not disabled,
        "size": _dir_size(path) if os.path.isdir(path) else (os.path.getsize(path) if os.path.isfile(path) else 0),
        "path": path,
    }


def _dir_size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(base, name))
    return total


def list_mods(server_dir: str) -> list[dict]:
    """列出 mods/ 与 mods.disabled/ 下的插件（LeviLamina 自身除外）。"""
    entries: list[dict] = []
    for disabled, root in ((False, _mods_root(server_dir)), (True, _mods_root(server_dir, True))):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if disabled is False and name.lower() == "levilamina":
                continue
            if not _NAME_RE.match(name):
                continue
            entries.append(_mod_entry(root, name, disabled))
    return entries


def _safe_name(name: str) -> str:
    if not name or not _NAME_RE.match(name) or name in (".", ".."):
        raise LeviLaminaError(f"插件名不合法: {name!r}")
    return name


def toggle_mod(server_dir: str, name: str, enabled: bool) -> dict:
    """启用/禁用：在 mods/ 与 mods.disabled/ 之间移动。"""
    name = _safe_name(name)
    # enabled=True 表示"现在启用"，源目录应为 mods.disabled；反之为 mods
    src_root = _mods_root(server_dir, disabled=enabled)
    dst_root = _mods_root(server_dir, disabled=not enabled)
    src = os.path.join(src_root, name)
    if not os.path.exists(src):
        raise LeviLaminaError(f"找不到插件: {name}")
    os.makedirs(dst_root, exist_ok=True)
    dst = os.path.join(dst_root, name)
    if os.path.exists(dst):
        raise LeviLaminaError(f"目标位置已存在同名插件: {name}")
    os.replace(src, dst)
    return {"name": name, "enabled": enabled}


def delete_mod(server_dir: str, name: str) -> dict:
    name = _safe_name(name)
    for root in (_mods_root(server_dir), _mods_root(server_dir, True)):
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
        return {"name": name, "deleted": True}
    raise LeviLaminaError(f"找不到插件: {name}")


def install_mod_zip(server_dir: str, zip_path: str, display_name: str = "") -> dict:
    """安装本地插件包：lip 工作区优先走 lip，否则安全解压到 mods/<名>/。

    Args:
        display_name: 原始文件名（上传场景下临时文件名没有意义）。
    """
    zip_path = os.path.abspath(zip_path)
    if not os.path.isfile(zip_path) or not zipfile.is_zipfile(zip_path):
        raise LeviLaminaError("插件包不是有效的 zip")
    if os.path.isdir(os.path.join(server_dir, ".lip")) and lip_installed():
        code, output = run_lip(["install", zip_path, "--accept-eula"], server_dir, timeout=600)
        if code != 0:
            raise LeviLaminaError(f"lip install 失败: {output[-400:]}")
        return {"installed": True, "via": "lip"}
    target_root = _mods_root(server_dir)
    os.makedirs(target_root, exist_ok=True)
    stem = os.path.splitext(os.path.basename(display_name or zip_path))[0]
    name = _safe_name(re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]", "_", stem))
    target = os.path.join(target_root, name)
    if os.path.exists(target):
        raise LeviLaminaError(f"mods 下已存在同名插件: {name}")
    tmp_dir = target + ".tmp"
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                member = info.filename.replace("\\", "/")
                if member.startswith("/") or ".." in member.split("/"):
                    raise LeviLaminaError(f"插件包含非法路径: {info.filename}")
                dest = os.path.join(tmp_dir, member)
                if info.is_dir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        os.replace(tmp_dir, target)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return {"installed": True, "via": "unzip", "name": name}


def install_mod_url(server_dir: str, url: str) -> dict:
    """从 https URL 下载插件包后安装（文件名取自 URL，便于 mods 目录可读）。"""
    if not url.lower().startswith("https://"):
        raise LeviLaminaError("只支持 https 下载地址")
    os.makedirs(LIP_DIR, exist_ok=True)
    name = os.path.basename(urlparse(url).path) or "mod.zip"
    if not name.lower().endswith(".zip"):
        name += ".zip"
    name = re.sub(r"[^0-9A-Za-z._\\-\u4e00-\u9fff]", "_", name)[:120]
    tmpdir = tempfile.mkdtemp(prefix="mod-", dir=LIP_DIR)
    target = os.path.join(tmpdir, name)
    written = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp, open(target, "wb") as handle:  # noqa: S310 - 用户显式提供
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise LeviLaminaError("插件包超过大小上限")
                handle.write(chunk)
        return install_mod_zip(server_dir, target, display_name=name)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        raise LeviLaminaError(f"下载插件失败: {exc}") from exc
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def lip_list(server_dir: str) -> dict:
    """读取 lip 已安装包列表。"""
    code, output = run_lip(["list", "--json"], server_dir)
    if code != 0:
        raise LeviLaminaError(f"lip list 失败: {output[-300:]}")
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError as exc:
        # 输出不是 JSON（例如 lip 打印了提示行）：统一按"无法解析"处理
        raise LeviLaminaError(f"lip list 输出无法解析: {exc}") from exc
    raise LeviLaminaError("lip list 输出无法解析")


def lip_install(server_dir: str, package: str) -> dict:
    package = (package or "").strip()
    if not package or len(package) > 300:
        raise LeviLaminaError("请填写包名或 URL")
    if not re.match(r"^[A-Za-z0-9._/:\-#@+~%?=&\u4e00-\u9fff]+$", package):
        raise LeviLaminaError("包名包含非法字符")
    code, output = run_lip(["install", package, "--accept-eula"], server_dir, timeout=900)
    if code != 0:
        raise LeviLaminaError(f"lip install 失败: {output[-400:]}")
    return {"installed": True, "output": output[-2000:]}


def lip_uninstall(server_dir: str, package: str) -> dict:
    package = (package or "").strip()
    if not package or len(package) > 300:
        raise LeviLaminaError("请填写包名")
    code, output = run_lip(["uninstall", package], server_dir, timeout=300)
    if code != 0:
        raise LeviLaminaError(f"lip uninstall 失败: {output[-400:]}")
    return {"uninstalled": True}


def extract_member_names(path: str, limit: int = 50) -> list[str]:
    """列出 zip 内文件名（供界面预览/测试）。"""
    try:
        with zipfile.ZipFile(path) as zf:
            return [info.filename for info in zf.infolist()][:limit]
    except (OSError, zipfile.BadZipFile):
        return []


def tar_member_names(path: str, limit: int = 50) -> list[str]:
    try:
        with tarfile.open(path) as tf:
            return [member.name for member in tf.getmembers()][:limit]
    except (OSError, tarfile.TarError):
        return []
