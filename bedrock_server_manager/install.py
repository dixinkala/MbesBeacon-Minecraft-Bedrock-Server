"""
安装与下载模块：InstallState、下载、解压、版本检测、安装流程。
"""

import contextlib
import json
import os
import re
import shutil
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

from .app_context import AppContext
from .app_logger import safe_log_exception
from .constants import (
    BDS_VERSIONS_JSON_URL,
    CDN_TEMPLATE,
    IS_WINDOWS,
    LINKS_API,
    PLATFORM_TAG,
    SERVER_EXE,
    VERSION_CACHE_TTL,
)
from .server import get_server_proc, server_running
from .utils import add_server_dir_history, atomic_write_json, detect_servers, save_settings, tr_msg
from .verify import get_official_version_info, verify_download_full

# 版本号允许的字符集：数字开头，只含数字/字母/点/下划线/连字符。
# 版本号会被拼进下载 URL 与临时文件名，不校验则可写入安装目录之外
# （如 "../../../X" 会使 zip 落到上级目录）。
# 用 \Z 而不是 $：Python 的 $ 允许匹配"结尾换行"，"1.2\n" 会通过校验并把
# 换行拼进 URL/文件名，最终表现为"所有下载源都失败"这种难排查的错误。
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z._-]{0,31}\Z")

# 安装暂存目录（位于服务器目录内，安装成功后并入、失败后删除）
STAGING_DIRNAME = ".install_staging"

# 更新安装时必须**整份保留**的用户数据（官方 ZIP 里带有这些文件的默认版本，
# 直接解压覆盖会把用户自定义的配置/白名单/权限/存档全部重置为默认值）。
_PRESERVE_ON_MERGE = frozenset(
    {
        "worlds",
        "server.properties",
        "allowlist.json",
        "permissions.json",
        "banned-players.json",
        "banned-ips.json",
        "whitelist.json",
        "valid_known_packs.json",
        "structures",
        "development_behavior_packs",
        "development_resource_packs",
        "development_skin_packs",
        "premium_cache",
        "treatments",
        "skin_packs",
        "server_console.log",
        ".config_history",
        "audit.log",
        "scheduled_tasks.json",
        ".scheduler_state.json",
    }
)

# 官方内容目录：随服务端版本更新（否则会出现"新 exe + 旧 vanilla 包"），
# 但用覆盖式合并——只写入新包中存在的文件，不删除用户额外加进去的内容。
_OVERLAY_ON_MERGE = frozenset(
    {
        "resource_packs",
        "behavior_packs",
        "definitions",
    }
)


def validate_version(version: str) -> tuple[bool, str]:
    """校验版本号是否可用于拼 URL 与文件名。

    Args:
        version: 版本号字符串（如 ``1.21.0.03``）

    Returns:
        tuple: (ok, error_msg)
    """
    if not version:
        return True, ""
    if not _VERSION_RE.match(version):
        return False, f"版本号格式非法: {version!r}（仅允许数字、字母、点、下划线、连字符）"
    return True, ""


def _is_preserved_name(name: str) -> bool:
    """判断顶层条目是否属于"更新时保留"的用户数据。"""
    return name in _PRESERVE_ON_MERGE or name.startswith("server_console.")


def apply_staged_install(staging_dir: str, dir_target: str) -> list[str]:
    """把暂存目录中已校验的安装内容并入目标服务器目录。

    规则：
    - 用户数据（``_PRESERVE_ON_MERGE`` 中的名字）已存在 → 跳过，保留用户版本
    - 目标目录中不存在的条目 → 直接复制
    - 其余条目（可执行文件、DLL、文档等）→ 原子替换（见下）

    **原子替换**：单文件走「同目录临时名 → os.replace」，绝不先删后拷。
    早期实现是 ``os.remove(dst)`` 再 ``shutil.copy2``：一旦复制失败（磁盘满、
    杀软占用、exe 正在运行），旧的可执行文件已经没了、新的又没写上，
    服务器目录里直接**丢失 bedrock_server.exe**；而 ``finally`` 紧接着还会
    把暂存里那份已校验的副本删掉，等于没有任何恢复源。

    目录类：先整体复制到临时目录，成功后用 ``os.replace`` 换名；
    失败时删除临时目录，原目录保持不动。

    Args:
        staging_dir: 已解压并校验通过的暂存目录
        dir_target: 服务器目录

    Returns:
        list[str]: 被跳过的用户数据条目名
    """
    os.makedirs(dir_target, exist_ok=True)
    # 只允许在"暂存目录确实包含服务端可执行文件"时合并。
    # 否则一个不完整的 staging（解压中断/调用方传错目录）会把目标目录里的
    # 服务端文件当作"旧版本残留"清理掉，等于把服务器清空。
    if not os.path.isfile(os.path.join(staging_dir, SERVER_EXE)):
        raise RuntimeError(f"安装包不完整：暂存目录中缺少 {SERVER_EXE}，已中止合并（服务器目录未改动）")
    previous_owned = _load_install_manifest(dir_target)
    skipped = []
    for name in sorted(os.listdir(staging_dir)):
        src = os.path.join(staging_dir, name)
        dst = os.path.join(dir_target, name)
        if _is_preserved_name(name) and os.path.exists(dst):
            skipped.append(name)
            continue
        if name in _OVERLAY_ON_MERGE:
            # 官方内容目录：覆盖式合并（不删用户额外加的内容）
            shutil.copytree(src, dst, dirs_exist_ok=True)
            continue
        if os.path.isdir(src) and not os.path.islink(src):
            _atomic_replace_dir(src, dst)
        else:
            _atomic_replace_file(src, dst)
    # 清理"新版本已不再包含"的旧服务端文件。
    # 依据是**上次安装自己写下的清单**（_INSTALL_MANIFEST），而不是
    # "目标目录里所有不像新包的东西" —— 后者会删掉用户自己的文件
    # （实测会删掉 bedrock_server_backup.zip、steamclient64.dll、我的备注.txt）。
    _prune_stale_files(staging_dir, dir_target, previous_owned)
    _save_install_manifest(dir_target, os.listdir(staging_dir))
    return skipped


# 记录"上次安装由本程序放进去的顶层条目"，用于下次升级时安全清理旧文件。
# 只清理清单里出现过、本次安装包已不再包含的条目——用户自己放的文件
# 永远不在清单里，因此永远不会被清理。
_INSTALL_MANIFEST = ".install_manifest.json"


def _load_install_manifest(dir_target: str) -> set[str]:
    """读取上次安装清单（缺失/损坏时返回空集，视为"不清理"）。"""
    path = os.path.join(dir_target, _INSTALL_MANIFEST)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("files"), list):
            return {str(n) for n in data["files"]}
        if isinstance(data, list):
            return {str(n) for n in data}
    except (OSError, ValueError, TypeError) as e:
        safe_log_exception("install", f"读取安装清单失败（本次不清理旧文件）: {e}", "warning")
    return set()


def _save_install_manifest(dir_target: str, names) -> None:
    """保存本次安装放进去的顶层条目清单。"""
    try:
        payload = {"version": 1, "files": sorted(str(n) for n in names)}
        atomic_write_json(os.path.join(dir_target, _INSTALL_MANIFEST), payload)
    except Exception as e:
        safe_log_exception("install", f"保存安装清单失败（下次将不清理旧文件）: {e}", "warning")


# 判定"服务端自带产物"的模式：只有这类文件才会在更新时被清理。
# 绝不能按"新包里没有就删"来清理——用户可能自己在服务器根目录放
# 脚本、笔记、备份包，那样等于把用户文件当垃圾删掉。
#
# 匹配规则刻意收窄到"**以**这些扩展名结尾"：
#   - 不能写成"包含 .dll" —— 否则 `my.dll.txt`、`a.dll.bak` 都会被误判
#   - `.so.1` / `.so.1.1` 这类版本化共享库要单独允许
#   - 用户手动备份的 `libCrypto.dll.bak` / `.old` 必须放过
_SERVER_ARTIFACT_NAMES = frozenset(
    {
        "bedrock_server",  # Linux 无可执行文件扩展名
        "bedrock_server.exe",
        "bedrock_server.pdb",
        "bedrock_server_how_to.html",
        "release-notes.txt",
    }
)
_SERVER_ARTIFACT_RE = re.compile(r"(\.(dll|dylib|pdb|exe|lib|exp)$)|(\.so(\.\d+)*$)", re.IGNORECASE)
_BACKUP_SUFFIX_RE = re.compile(r"\.(bak|old|orig|backup|save|tmp|disabled|txt|log)$", re.IGNORECASE)
# _atomic_replace_file 用的临时文件名（崩溃后会残留）
_TEMP_FILE_RE = re.compile(r"\.install_[0-9a-z]+\.tmp$", re.IGNORECASE)


def _is_server_artifact(name: str) -> bool:
    """判断顶层条目是否属于"服务端自带产物"（更新时可安全清理）。

    注意：Linux 的 ``bedrock_server`` 没有扩展名，所以在
    ``_SERVER_ARTIFACT_NAMES`` 里单独列出；**不能**用
    ``name.startswith("bedrock_server")`` —— 那会把用户自己的
    ``bedrock_server_backup.zip`` 也判成产物。
    """
    low = (name or "").lower()
    if low in _SERVER_ARTIFACT_NAMES:
        return True
    if _BACKUP_SUFFIX_RE.search(low):
        # 用户手动备份（a.dll.bak / libCrypto.dll.old）不能当残留清理
        return False
    return bool(_SERVER_ARTIFACT_RE.search(low))


def _prune_stale_files(staging_dir: str, dir_target: str, previous_owned: set[str]) -> list[str]:
    """删除"上次安装由本程序放进去、本次安装包已不再包含"的服务端文件。

    判定依据是 ``previous_owned``（上次安装写下的 ``.install_manifest.json``）
    **而不是**"目标目录里所有不在新包中的东西"。后者会删用户自己的文件：
    实测会删掉用户放在根目录的 ``bedrock_server_backup.zip``（名字以
    bedrock_server 开头）和 ``steamclient64.dll``（任意 .dll）。

    三重闸门（全部满足才删）：
    1. 在 ``previous_owned`` 里（确实是我们装的）；
    2. 不在新安装包里（``wanted``）；
    3. 不是用户数据（``_PRESERVE_ON_MERGE`` / ``_OVERLAY_ON_MERGE``），
       且名字仍匹配服务端产物模式（``_is_server_artifact``，纵深防御）。

    另外顺手清理本程序自己的残留：``*.install_new``（半成品副本）、
    ``*.install_old``（仅当原目录已就位）、``*.install_*.tmp``（崩溃残留）。

    Args:
        staging_dir: 已解压的安装包目录（"应该存在什么"的清单）
        dir_target: 服务器目录
        previous_owned: 上次安装清单里的顶层条目名

    Returns:
        list[str]: 被删除的条目名
    """
    try:
        wanted = set(os.listdir(staging_dir))
        removed = []
        for name in sorted(os.listdir(dir_target)):
            full = os.path.join(dir_target, name)
            # ---- 本程序自己的残留：无条件清理（与清单无关） ----
            if name.endswith(".install_new"):
                with contextlib.suppress(OSError):
                    shutil.rmtree(full, ignore_errors=True)
                continue
            if name.endswith(".install_old"):
                # 旧目录备份：只有在对应的目标目录**已经存在**时才删。
                # 若目标缺失，说明上次合并失败、这份 old 可能是唯一副本。
                origin = os.path.join(dir_target, name[: -len(".install_old")])
                if os.path.exists(origin):
                    with contextlib.suppress(OSError):
                        shutil.rmtree(full, ignore_errors=True)
                else:
                    safe_log_exception(
                        "install",
                        f"发现残留的旧目录备份但目标缺失，已保留以便人工恢复: {full}",
                        "warning",
                    )
                continue
            if _TEMP_FILE_RE.search(name):
                with contextlib.suppress(OSError):
                    os.remove(full)
                continue

            # ---- 旧服务端文件：三重闸门 ----
            if name in wanted or name in _PRESERVE_ON_MERGE or name in _OVERLAY_ON_MERGE:
                continue
            if _is_preserved_name(name):
                continue
            if os.path.isdir(full):
                continue  # 目录一律不动（可能是用户目录）
            if name not in previous_owned:
                continue  # 不是我们装的 → 用户文件，绝不碰
            if not _is_server_artifact(name):
                continue
            try:
                os.remove(full)
                removed.append(name)
            except OSError as e:
                safe_log_exception("install", f"清理旧文件失败（可忽略）: {name}: {e}", "warning")
        if removed:
            safe_log_exception("install", f"更新时清理了旧版本残留文件: {removed}", "info")
        return removed
    except Exception as e:
        safe_log_exception("install", f"清理旧版本文件失败（不影响安装）: {e}", "warning")
        return []


def _atomic_replace_file(src: str, dst: str) -> None:
    """用 src 原子替换单文件 dst（同目录临时名 → os.replace）。"""
    parent = os.path.dirname(os.path.abspath(dst))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(dst) + ".install_", suffix=".tmp", dir=parent)
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        # Windows 上目标被占用时 os.replace 抛 PermissionError，重试几次
        last_err = None
        for delay in (0.0, 0.1, 0.3):
            if delay:
                time.sleep(delay)
            try:
                os.replace(tmp, dst)
                return
            except PermissionError as e:
                last_err = e
        raise last_err if last_err else OSError(f"替换失败: {dst}")
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.remove(tmp)


def _atomic_replace_dir(src: str, dst: str) -> None:
    """用 src 目录原子替换 dst 目录。

    先完整复制到 ``dst.install_new``，再 ``os.replace`` 换名进入；成功后删除旧目录。
    失败时清理临时目录，**原 dst 保持不动**。
    """
    parent = os.path.dirname(os.path.abspath(dst))
    os.makedirs(parent, exist_ok=True)
    new_dir = dst + ".install_new"
    if os.path.isdir(new_dir):
        shutil.rmtree(new_dir, ignore_errors=True)
    try:
        shutil.copytree(src, new_dir)
    except Exception:
        shutil.rmtree(new_dir, ignore_errors=True)
        raise
    # os.replace 不能直接替换非空目录（Windows 尤其严格）：
    # 先把旧目录改名绕开，再把新目录归位，最后删旧目录。
    old_dir = dst + ".install_old"
    if os.path.isdir(old_dir):
        shutil.rmtree(old_dir, ignore_errors=True)
    if os.path.exists(dst):
        os.replace(dst, old_dir)
    try:
        os.replace(new_dir, dst)
    except Exception:
        # 归位失败：把旧目录还原，避免目标目录消失
        with contextlib.suppress(OSError):
            if os.path.isdir(old_dir) and not os.path.exists(dst):
                os.replace(old_dir, dst)
        raise
    if os.path.isdir(old_dir):
        shutil.rmtree(old_dir, ignore_errors=True)


def cleanup_staging(dir_target: str) -> None:
    """删除安装暂存目录（幂等）。"""
    shutil.rmtree(os.path.join(dir_target, STAGING_DIRNAME), ignore_errors=True)


# 通过 AppContext 访问全局状态（消除延迟导入）
def _get_settings():
    return AppContext.instance().settings


def _get_console():
    return AppContext.instance().console


def _get_constants():
    return SERVER_EXE, IS_WINDOWS, LINKS_API, CDN_TEMPLATE


def _format_download_template(template: str, version: str) -> str:
    """将下载源模板格式化为实际 URL（填充 {version} 与 {platform}）。"""
    return template.replace("{version}", version).replace("{platform}", PLATFORM_TAG)


# 下载源配置
# 注意：官方 CDN 支持所有历史版本下载，URL 格式为模板化（{platform} 为 win/linux）
# 已移除不可用的 "官方备用 (minecraft.net/en-us)" 下载源（所有版本均返回404）
DOWNLOAD_SOURCES = [
    {
        "name": "官方 CDN (www.minecraft.net)",
        "template": "https://www.minecraft.net/bedrockdedicatedserver/bin-{platform}/bedrock-server-{version}.zip",
        "type": "official",
        "description": "Mojang 官方 CDN，支持所有历史版本下载，推荐使用",
    },
    {
        "name": "官方 CDN (minecraft.net)",
        "template": "https://minecraft.net/bedrockdedicatedserver/bin-{platform}/bedrock-server-{version}.zip",
        "type": "official",
        "description": "Mojang 官方 CDN 备用域名（不带 www），部分网络环境下可能更稳定",
    },
]

# 已验证可下载的基岩版服务端版本（历史版本列表）
# 官方 API 现在只返回最新版本，历史版本需从此列表获取
# 下载使用 CDN 模板 URL: https://www.minecraft.net/bedrockdedicatedserver/bin-{platform}/bedrock-server-{version}.zip
VERIFIED_VERSIONS = [
    # 1.26.x
    "1.26.44.3",
    "1.26.30.5",
    "1.26.20.4",
    "1.26.10.4",
    "1.26.0.2",
    # 1.21.x
    "1.21.130.3",
    "1.21.120.4",
    "1.21.110.2",
    "1.21.100.6",
    "1.21.90.3",
    "1.21.80.3",
    "1.21.60.10",
    "1.21.50.4",
    "1.21.40.3",
    "1.21.30.4",
    "1.21.20.3",
    "1.21.10.3",
    "1.21.0.3",
    # 1.20.x
    "1.20.80.3",
    "1.20.70.3",
    "1.20.60.3",
    "1.20.50.3",
    "1.20.40.3",
    "1.20.30.3",
    "1.20.20.3",
    "1.20.10.3",
    "1.20.0.3",
    # 1.19.x
    "1.19.80.3",
    "1.19.70.3",
    "1.19.60.3",
    "1.19.50.3",
    "1.19.40.3",
    "1.19.30.3",
    "1.19.20.3",
    "1.19.10.3",
    "1.19.0.3",
    # 1.18.x
    "1.18.30.3",
    "1.18.20.3",
    "1.18.10.3",
    "1.18.0.3",
    # 1.17.x
    "1.17.40.3",
    "1.17.30.3",
    "1.17.20.3",
    "1.17.10.3",
    "1.17.0.3",
]
FALLBACK_VERSIONS = list(VERIFIED_VERSIONS)


class InstallState:
    """服务器安装状态跟踪器。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.busy = False
        self.phase = "idle"
        self.percent = 0
        self.text = ""
        self.log = []
        self.done = False
        self.error = ""
        self.cancel = threading.Event()

    def snapshot(self):
        with self.lock:
            return {
                "busy": self.busy,
                "phase": self.phase,
                "percent": self.percent,
                "text": self.text,
                "log": list(self.log),
                "done": self.done,
                "error": self.error,
            }

    def set(self, phase=None, percent=None, text=None, done=None, error=None):
        with self.lock:
            if phase is not None:
                self.phase = phase
            if percent is not None:
                self.percent = percent
            if text is not None:
                self.text = text
            if done is not None:
                self.done = done
            if error is not None:
                self.error = error

    def log_line(self, line):
        with self.lock:
            self.log.append(line)
            if len(self.log) > 500:
                self.log = self.log[-500:]


def _http_get(url, timeout=30, ignore_ssl=False):
    """发送 HTTP GET 请求并返回响应内容（bytes）。"""
    ctx = ssl.create_default_context()
    if ignore_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MbesBeacon"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read()
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError:
        raise
    except Exception:
        raise


def get_latest_server_info(ignore_ssl=False):
    """从官方 API 获取最新版服务器下载信息（按当前平台返回对应下载地址）。"""
    SERVER_EXE, IS_WINDOWS, LINKS_API, CDN_TEMPLATE = _get_constants()
    # 官方 API 的 downloadType 字段：serverBedrockWindows / serverBedrockLinux（预览版带 Preview 后缀）
    target_type = "serverBedrockWindows" if IS_WINDOWS else "serverBedrockLinux"
    data = json.loads(_http_get(LINKS_API, timeout=30, ignore_ssl=ignore_ssl).decode("utf-8", "ignore"))
    links = data.get("result", {}).get("links", [])
    server_url = version = None
    for it in links:
        dt = it.get("downloadType", "")
        url = it.get("downloadUrl", "")
        m = re.search(r"bedrock-server-([0-9.]+)\.zip", url)
        if not m:
            continue
        if dt == target_type and not server_url:
            server_url, version = url, m.group(1)
    if not server_url:
        # 兜底：未能匹配平台类型时，尝试从任意平台链接提取版本号并回退到 CDN 模板
        raise RuntimeError(f"未能从官方 API 解析到 {'Windows' if IS_WINDOWS else 'Linux'} 服务端下载地址")
    return {"version": version, "win_url": server_url, "url": server_url}


def download_file(url, dest, progress_cb=None, cancel_flag=None, ignore_ssl=False, chunk=65536, resume=True):
    """下载文件到指定路径，支持进度回调、取消和断点续传。

    Args:
        url: 下载地址
        dest: 保存路径
        progress_cb: 进度回调函数 (done, total)
        cancel_flag: 取消标志 (threading.Event)
        ignore_ssl: 是否忽略 SSL 证书验证
        chunk: 下载块大小
        resume: 是否启用断点续传（默认 True）

    Returns:
        int: 已下载的总字节数
    """
    ctx = ssl.create_default_context()
    if ignore_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    tmp = dest + ".part"
    resume_pos = 0

    # 断点续传：检查已存在的临时文件并校验完整性
    if resume and os.path.exists(tmp):
        try:
            resume_pos = os.path.getsize(tmp)
            if resume_pos > 0:
                # 校验临时文件完整性：
                # 1. 文件大小必须大于 0
                # 2. ZIP 文件必须以 PK 头开头（前 2 字节为 0x50 0x4B）
                # 3. 如果校验失败，删除临时文件并重新下载
                tmp_valid = False
                try:
                    with open(tmp, "rb") as f_tmp:
                        header = f_tmp.read(4)
                        # ZIP 文件签名：PK
                        if (
                            len(header) >= 2
                            and header[0:2] == b"PK"
                            or len(header) >= 4
                            and header[0:4] == b"PK\x03\x04"
                        ):
                            tmp_valid = True
                except Exception as e:
                    safe_log_exception("install", f"download_file 忽略异常: {e}", "warning")
                    tmp_valid = False

                if not tmp_valid:
                    # 临时文件损坏，删除并重新下载
                    with contextlib.suppress(OSError):
                        os.remove(tmp)
                    resume_pos = 0
        except OSError:
            resume_pos = 0

    try:
        # 构建请求头
        headers = {"User-Agent": "Mozilla/5.0 MbesBeacon"}
        if resume_pos > 0:
            headers["Range"] = f"bytes={resume_pos}-"

        req = urllib.request.Request(url, headers=headers)

        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            # SSRF 兜底：服务端可能 302 到内网/元数据地址，按最终 URL 再校验一次
            effective = getattr(resp, "geturl", lambda: url)()
            if effective and effective != url:
                from .security import validate_effective_url

                ok_eff, err_eff = validate_effective_url(effective)
                if not ok_eff:
                    raise RuntimeError(err_eff)
            # 检查服务器是否支持断点续传
            content_range = resp.headers.get("Content-Range", "")
            accepts_ranges = resp.headers.get("Accept-Ranges", "") == "bytes"
            is_partial = resp.status == 206 or bool(content_range) or accepts_ranges

            # 获取文件总大小
            total = int(resp.headers.get("Content-Length") or 0)
            if is_partial and content_range:
                # Content-Range: bytes 100-199/200
                try:
                    total = int(content_range.split("/")[-1])
                except (ValueError, IndexError):
                    total = total + resume_pos if total else 0
            elif total and resume_pos > 0 and not is_partial:
                # 服务器不支持断点续传，从头开始
                resume_pos = 0

            # 打开文件（追加模式或写入模式）
            mode = "ab" if (resume_pos > 0 and is_partial) else "wb"
            if mode == "wb":
                resume_pos = 0  # 从头开始，重置位置

            with open(tmp, mode) as f:
                done = resume_pos
                last_t = 0.0
                while True:
                    if cancel_flag is not None and cancel_flag.is_set():
                        raise InterruptedError("下载已取消")
                    chunk_data = resp.read(chunk)
                    if not chunk_data:
                        break
                    f.write(chunk_data)
                    done += len(chunk_data)
                    now = time.time()
                    if progress_cb and (now - last_t >= 0.15 or done >= total):
                        last_t = now
                        progress_cb(done, total)

        os.replace(tmp, dest)
        return done
    except InterruptedError:
        # 用户取消，保留临时文件以便下次续传
        raise
    except Exception:
        # 下载失败，删除不完整的临时文件（除非是用户取消）
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.remove(tmp)
        raise


def extract_zip(zip_path, dest_dir, progress_cb=None, cancel_flag=None):
    """解压 ZIP 文件到指定目录，支持进度回调和取消。"""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        total = len(names)
        base = os.path.abspath(dest_dir)
        for i, name in enumerate(names):
            if cancel_flag is not None and cancel_flag.is_set():
                raise InterruptedError("解压已取消")
            target = os.path.join(dest_dir, name)
            abs_target = os.path.abspath(target)
            # 目录边界检查：防止路径遍历
            if abs_target != base and not abs_target.startswith(base + os.sep):
                raise RuntimeError("压缩包内包含非法路径，已中止")
            zf.extract(name, dest_dir)
            if progress_cb and (i % 5 == 0 or i == total - 1):
                progress_cb(i + 1, total)


def select_server_dir(d: str) -> tuple:
    """校验并切换当前管理的服务器目录，返回 (ok, error_msg, info_dict)。"""
    settings = AppContext.instance().settings

    d = (d or "").strip()
    if not d or not os.path.isdir(d):
        return False, "目录不存在：%s" % (d or "（未指定）"), None
    if not os.path.isfile(os.path.join(d, SERVER_EXE)):
        return False, "该目录未找到 bedrock_server.exe，不是有效的服务器目录", None
    if server_running():
        p = get_server_proc()
        if os.path.normcase(os.path.abspath(p.server_dir)) != os.path.normcase(os.path.abspath(d)):
            try:
                p.stop(wait=8)
            except Exception as e:
                safe_log_exception("install", f"select_server_dir 忽略异常: {e}", "warning")
                try:
                    from .app_logger import get_app_logger

                    get_app_logger().warning(f"install.py 停止服务器异常: {e}")
                except Exception as e:
                    # 停止旧服务器失败不影响目录切换
                    safe_log_exception("install", f"select_server_dir 忽略异常: {e}", "warning")
    settings["server_dir"] = d
    add_server_dir_history(d)
    detected_ver = detect_server_version(d)
    if detected_ver:
        settings["installed_version"] = detected_ver
    save_settings()
    return (
        True,
        "",
        {
            "dir": d,
            "installed_version": settings.get("installed_version", ""),
            "detected_servers": detect_servers(),
        },
    )


_versions_cache = {"stable": None, "preview": None, "timestamp": 0}


def get_cached_versions(include_preview=False):
    """获取缓存的版本列表，过期则重新获取。"""
    global _versions_cache
    key = "preview" if include_preview else "stable"
    now = time.time()
    if _versions_cache[key] and (now - _versions_cache["timestamp"]) < VERSION_CACHE_TTL:
        return _versions_cache[key], True
    try:
        versions = get_bedrock_versions(include_preview=include_preview)
        _versions_cache[key] = versions
        _versions_cache["timestamp"] = now
        return versions, False
    except Exception as e:
        safe_log_exception("install", f"get_cached_versions 忽略异常: {e}", "warning")
        if _versions_cache[key]:
            return _versions_cache[key], True
        return FALLBACK_VERSIONS, False


def _version_key(ver):
    """将版本号字符串转换为可比较的元组，用于正确排序。

    例如 "1.21.130.3" -> (1, 21, 130, 3)
    处理不完整版本号，缺失部分补0。
    """
    parts = []
    for p in ver.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    # 补齐到4段
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


# Bedrock-OSS/BDS-Versions 项目的 versions.json 地址
# 该项目维护了完整的基岩版服务端版本列表（188+稳定版 + 预览版）
# 版本清单 URL 与缓存时长统一来自 constants（此处不再重复定义）


def fetch_versions_from_bds_oss(ignore_ssl=False, include_preview=False):
    """从 Bedrock-OSS/BDS-Versions GitHub 项目获取完整版本列表。

    该项目维护了从 1.6.0.15 到最新版的所有基岩版服务端版本，
    下载使用官方 CDN: https://www.minecraft.net/bedrockdedicatedserver/bin-{platform}/bedrock-server-{version}.zip

    Returns:
        tuple: (stable_versions, preview_versions) 两个列表
    """
    try:
        data = json.loads(_http_get(BDS_VERSIONS_JSON_URL, timeout=30, ignore_ssl=ignore_ssl).decode("utf-8", "ignore"))
        # versions.json 按平台分组：windows / linux
        platform_data = data.get("windows" if IS_WINDOWS else "linux", {})
        stable = platform_data.get("versions", [])
        preview = platform_data.get("preview_versions", [])
        # 去重并排序
        stable = sorted(set(stable), key=_version_key, reverse=True)
        preview = sorted(set(preview), key=_version_key, reverse=True)
        return stable, preview
    except Exception as e:
        safe_log_exception("install", f"fetch_versions_from_bds_oss 忽略异常: {e}", "warning")
        return [], []


def get_bedrock_versions(ignore_ssl=False, include_preview=False):
    """获取基岩版服务端版本列表。

    版本来源优先级：
    1. Bedrock-OSS/BDS-Versions (GitHub) - 完整历史版本列表（188+稳定版）
    2. 官方 API - 最新版本（补充最新版，确保及时更新）
    3. 硬编码后备列表 - 网络不可用时使用

    所有版本均可通过官方 CDN 下载:
    https://www.minecraft.net/bedrockdedicatedserver/bin-{platform}/bedrock-server-{version}.zip
    """

    all_versions = []

    # 1. 从 Bedrock-OSS/BDS-Versions 获取完整历史版本列表
    stable_from_oss, preview_from_oss = fetch_versions_from_bds_oss(
        ignore_ssl=ignore_ssl, include_preview=include_preview
    )
    if stable_from_oss:
        all_versions.extend(stable_from_oss)
    if include_preview and preview_from_oss:
        all_versions.extend(preview_from_oss)

    # 2. 从官方 API 获取最新版本（补充可能的最新版，仅取当前平台对应条目）
    try:
        data = json.loads(_http_get(LINKS_API, timeout=15, ignore_ssl=ignore_ssl).decode("utf-8", "ignore"))
        links = data.get("result", {}).get("links", [])
        platform_key = "Windows" if IS_WINDOWS else "Linux"
        for it in links:
            url = it.get("downloadUrl", "")
            m = re.search(r"bedrock-server-([0-9.]+)\.zip", url)
            if m:
                ver = m.group(1)
                dt = it.get("downloadType", "")
                if platform_key not in dt:
                    continue
                is_preview = "preview" in dt.lower() or "beta" in dt.lower()
                if (include_preview or not is_preview) and ver not in all_versions:
                    all_versions.append(ver)
    except Exception as e:
        safe_log_exception("install", f"操作失败: {e}", "warning")

    # 3. 如果网络获取失败，使用硬编码后备列表
    if not all_versions:
        all_versions = list(VERIFIED_VERSIONS)
        if include_preview:
            all_versions.extend(
                [
                    "1.26.50.27",
                    "1.26.40.3",
                    "1.26.30.4",
                    "1.26.20.3",
                    "1.26.10.3",
                    "1.21.140.3",
                    "1.21.130.4",
                    "1.21.120.3",
                    "1.21.110.3",
                    "1.21.100.3",
                ]
            )

    # 去重并按版本号排序（最新版在最前面）
    all_versions = sorted(set(all_versions), key=_version_key, reverse=True)
    return all_versions


def detect_server_version(dir_path):
    """检测服务器已安装版本。优先从 settings 的目录->版本映射读取，
    后备方案：从 bedrock_server.exe 的 Windows 文件版本属性读取。"""
    settings = AppContext.instance().settings

    if not dir_path or not os.path.isdir(dir_path):
        return ""
    # 1. 从映射读取（最准确，由安装/更新时写入）
    versions_map = settings.get("server_versions", {})
    if isinstance(versions_map, dict):
        key = os.path.normcase(os.path.abspath(dir_path))
        if key in versions_map and versions_map[key]:
            return versions_map[key]
    # 2. 从服务端文件版本属性读取（仅 Windows：使用 Win32 API 读取 PE 文件版本资源）
    if IS_WINDOWS:
        exe_path = os.path.join(dir_path, SERVER_EXE)
        if os.path.isfile(exe_path):
            try:
                import ctypes.wintypes

                size = ctypes.windll.version.GetFileVersionInfoSizeW(exe_path, None)
                if size > 0:
                    buf = ctypes.create_string_buffer(size)
                    if ctypes.windll.version.GetFileVersionInfoW(exe_path, None, size, buf):

                        class LANGANDCODEPAGE(ctypes.Structure):
                            _fields_ = [("wLanguage", ctypes.wintypes.WORD), ("wCodePage", ctypes.wintypes.WORD)]

                        lang_addr = ctypes.c_uint()
                        lang_len = ctypes.c_uint()
                        lang_list = []
                        if ctypes.windll.version.VerQueryValueW(
                            buf, "\\VarFileInfo\\Translation", ctypes.byref(lang_addr), ctypes.byref(lang_len)
                        ):
                            lang_ptr = ctypes.cast(lang_addr, ctypes.POINTER(LANGANDCODEPAGE))
                            for i in range(lang_len.value // ctypes.sizeof(LANGANDCODEPAGE)):
                                lang_list.append((lang_ptr[i].wLanguage, lang_ptr[i].wCodePage))
                        lang_list.append((0x0409, 0x04B0))
                        lang_list.append((0x0804, 0x04B0))
                        for wlang, wcp in lang_list:
                            for field in ["ProductVersion", "FileVersion"]:
                                try:
                                    sub_block = f"\\StringFileInfo\\{wlang:04x}{wcp:04x}\\{field}"
                                    val_addr = ctypes.c_uint()
                                    val_len = ctypes.c_uint()
                                    if ctypes.windll.version.VerQueryValueW(
                                        buf, sub_block, ctypes.byref(val_addr), ctypes.byref(val_len)
                                    ):
                                        ver = ctypes.wstring_at(val_addr, val_len - 1).strip()
                                        if ver and re.match(r"^[\d.]+$", ver):
                                            if not isinstance(versions_map, dict):
                                                versions_map = {}
                                            versions_map[os.path.normcase(os.path.abspath(dir_path))] = ver
                                            settings["server_versions"] = versions_map
                                            save_settings()
                                            return ver
                                except Exception as e:
                                    safe_log_exception("install", f"detect_server_version 忽略异常: {e}", "warning")
                                    continue
            except Exception as e:
                safe_log_exception("install", f"操作失败: {e}", "warning")
    # 2.5 检查版本标识文件
    for vfile in ["release_notes.txt", "version.txt", "VERSION", "bedrock_server_version.txt"]:
        vpath = os.path.join(dir_path, vfile)
        if os.path.isfile(vpath):
            try:
                with open(vpath, encoding="utf-8", errors="ignore") as f:
                    content = f.read(2000)
                m = re.search(r"(\d+\.\d+\.\d+\.\d+)", content)
                if m:
                    ver = m.group(1)
                    if not isinstance(versions_map, dict):
                        versions_map = {}
                    versions_map[os.path.normcase(os.path.abspath(dir_path))] = ver
                    settings["server_versions"] = versions_map
                    save_settings()
                    return ver
            except Exception as e:
                safe_log_exception("install", f"操作失败: {e}", "warning")
    return ""


def do_install(dir_target, version, autostart, custom_url="", source_index=0):
    """执行服务器安装：下载 -> 解压 -> 配置 -> 可选启动。"""
    install_state = AppContext.instance().install_state
    settings = AppContext.instance().settings

    install_state.cancel.clear()
    install_state.busy = True
    install_state.done = False
    install_state.error = ""
    install_state.log = []
    install_state.set(phase="start", percent=0, text=tr_msg("开始安装...", "Starting installation..."))
    try:
        os.makedirs(dir_target, exist_ok=True)
        # 安装前磁盘空间检查（服务端压缩包约 60-120MB，解压后需约 300MB）
        try:
            from .utils import check_disk_space

            # 暂存目录化之后峰值占用 = 下载的 zip + 解压出的暂存树 + 旧安装目录，
            # 早期写死的 300MB 对这条链路明显偏小（官方 ZIP 解压后约 400-600MB）。
            ok_space, free_mb, required_mb = check_disk_space(dir_target, required_mb=1200)
            if not ok_space:
                install_state.log_line(
                    tr_msg(
                        "错误: 磁盘空间不足（剩余 %.0f MB，需要 %.0f MB）",
                        "Error: insufficient disk space (%.0f MB free, %.0f MB required)",
                        free_mb,
                        required_mb,
                    )
                )
                raise RuntimeError(
                    tr_msg(
                        "磁盘空间不足：剩余 %.0f MB，需要 %.0f MB，请清理磁盘后重试",
                        "Insufficient disk space: %.0f MB free, %.0f MB required; please free up space and retry",
                        free_mb,
                        required_mb,
                    )
                )
        except RuntimeError:
            raise
        except Exception as e:
            safe_log_exception("install", f"do_install 忽略异常: {e}", "warning")
            install_state.log_line(
                tr_msg(
                    "警告: 磁盘空间检查失败，已跳过（%.60s）",
                    "Warning: disk space check failed, skipped (%.60s)",
                    str(e),
                )
            )
        ignore_ssl = settings.get("ignore_ssl", False)
        install_state.log_line(
            tr_msg(">>> 开始安装 Minecraft 基岩版服务器", ">>> Starting Minecraft Bedrock Server installation")
        )
        # 版本号会被拼进下载 URL 与临时文件名，必须先净化
        ok_ver, ver_err = validate_version(version)
        if not ok_ver:
            raise RuntimeError(ver_err)
        urls_to_try = []
        if custom_url and custom_url.strip():
            urls_to_try.append(("自定义地址", custom_url.strip()))
            install_state.log_line(tr_msg("使用自定义下载地址", "Using custom download URL"))
        elif version:
            install_state.log_line(tr_msg("指定版本: %s", "Specified version: %s", version))
            for i in range(len(DOWNLOAD_SOURCES)):
                idx = (source_index + i) % len(DOWNLOAD_SOURCES)
                src = DOWNLOAD_SOURCES[idx]
                urls_to_try.append((src["name"], _format_download_template(src["template"], version)))
        else:
            if not (settings.get("_latest") or {}).get("version"):
                install_state.set(percent=1, text=tr_msg("正在获取最新版本信息...", "Fetching latest version info..."))
                install_state.log_line(tr_msg("正在获取最新版本信息...", "Fetching latest version info..."))
                settings["_latest"] = get_latest_server_info(ignore_ssl=ignore_ssl)
            latest = settings["_latest"]
            version = latest["version"]
            urls_to_try.append(("官方 API", latest["win_url"]))
            for src in DOWNLOAD_SOURCES[1:]:
                urls_to_try.append((src["name"], _format_download_template(src["template"], version)))
            install_state.log_line(tr_msg("最新稳定版: %s", "Latest stable version: %s", version))
        # 解析出的版本号同样要校验（来自网络，不能无条件信任）
        ok_ver, ver_err = validate_version(version)
        if not ok_ver:
            raise RuntimeError(ver_err)
        # 下载/解压全部在暂存目录完成：成功前不触碰现有服务器文件，
        # 失败时用户原有服务器保持可用（早期实现先删后端下载，失败即无服务端）。
        staging_dir = os.path.join(dir_target, STAGING_DIRNAME)
        staging_extract = os.path.join(staging_dir, "extracted")
        cleanup_staging(dir_target)
        os.makedirs(staging_extract, exist_ok=True)
        zip_path = os.path.join(staging_dir, f"bedrock-server-{version or 'download'}.zip")
        download_ok = False
        last_error = ""
        for src_name, url in urls_to_try:
            if install_state.cancel.is_set():
                break
            install_state.log_line(tr_msg("尝试下载源: %s", "Trying download source: %s", src_name))
            install_state.log_line(tr_msg("下载地址: %s", "Download URL: %s", url))
            install_state.set(
                phase="download", percent=3, text=tr_msg("正在从 %s 下载...", "Downloading from %s...", src_name)
            )
            try:

                def cb(done, total):
                    if total > 0:
                        pct = 3 + int(done / total * 77)
                        install_state.set(
                            percent=pct,
                            text=tr_msg(
                                "下载中 %d%%  (%d / %d KB)",
                                "Downloading %d%% (%d / %d KB)",
                                int(done / total * 100),
                                done // 1024,
                                total // 1024,
                            ),
                        )
                    else:
                        install_state.set(
                            percent=3, text=tr_msg("下载中... %d KB", "Downloading... %d KB", done // 1024)
                        )

                download_file(url, zip_path, progress_cb=cb, cancel_flag=install_state.cancel, ignore_ssl=ignore_ssl)
                download_ok = True
                install_state.log_line(
                    tr_msg(
                        "下载完成 (源: %s): %s (%.1f MB)",
                        "Download complete (source: %s): %s (%.1f MB)",
                        src_name,
                        os.path.basename(zip_path),
                        os.path.getsize(zip_path) / 1048576,
                    )
                )
                # 下载完整性校验
                try:
                    # 官方清单里没有 SHA256（实测只有版本号与下载链接），
                    # 所以这里能做的是"版本号是否官方发行"这一项额外信号；
                    # 完整性由 TOFU 信任库 + 签名校验保证。
                    try:
                        info = get_official_version_info(version)
                        if not info["available"]:
                            install_state.log_line(
                                tr_msg(
                                    "提示: 无法获取官方版本清单，跳过版本号核对（完整性仍由 SHA256/TOFU 保证）",
                                    "Note: official version list unavailable; skipping version check "
                                    "(integrity still covered by SHA256/TOFU)",
                                )
                            )
                        elif info["known"]:
                            install_state.log_line(
                                tr_msg(
                                    "版本 %s 确认为官方发行版（当前稳定版 %s）",
                                    "Version %s confirmed as an official release (current stable: %s)",
                                    version,
                                    info["stable"] or "-",
                                )
                            )
                        elif info["known"] is False and not (custom_url and custom_url.strip()):
                            install_state.log_line(
                                tr_msg(
                                    "警告: 版本 %s 不在官方发布列表中（当前稳定版 %s）",
                                    "Warning: version %s is not in the official release list (current stable: %s)",
                                    version,
                                    info["stable"] or "-",
                                )
                            )
                    except Exception as e:
                        safe_log_exception("install", f"版本号核对失败（不影响安装）: {e}", "warning")

                    # 签名策略见 verify.verify_download_full 的 docstring：
                    # 默认不把"未签名"当硬失败（Mojang 的 exe 是否带嵌入式签名
                    # 无法离线确认，强约束会导致官方源安装全断），但
                    # "摘要不匹配=被篡改"永远判失败。需要强约束时设
                    # 环境变量 MBES_REQUIRE_SIGNATURE=1。
                    verify_result = verify_download_full(
                        zip_path,
                        version=version,
                        check_pe=True,
                    )
                    if verify_result["ok"]:
                        install_state.log_line(
                            tr_msg(
                                "下载完整性校验通过（文件大小: %.1f MB, SHA256: %s...）",
                                "Download integrity check passed (size: %.1f MB, SHA256: %s...)",
                                verify_result["checks"]["file_size"]["size_mb"],
                                verify_result["checks"]["sha256"]["actual_hash"][:16],
                            )
                        )
                        for warn in verify_result.get("warnings") or []:
                            install_state.log_line(
                                tr_msg("警告（自定义地址）: %.120s", "Warning (custom URL): %.120s", warn)
                            )
                    else:
                        error_msg = "; ".join(verify_result["errors"])
                        install_state.log_line(
                            tr_msg(
                                "错误: 下载完整性校验失败: %s", "Error: download integrity check failed: %s", error_msg
                            )
                        )
                        # 校验失败时中止安装，删除损坏的文件
                        try:
                            if os.path.exists(zip_path):
                                os.remove(zip_path)
                        except OSError:
                            # 删除损坏的 ZIP 失败可忽略
                            pass
                        raise RuntimeError(
                            tr_msg(
                                "下载文件完整性校验失败: %s\n提示: 可尝试切换其他下载源或使用自定义地址",
                                "Download file integrity check failed: %s\nTip: try switching to another download source or use a custom URL",
                                error_msg,
                            )
                        )
                except RuntimeError:
                    raise  # 重新抛出校验失败的异常
                except Exception as ve:
                    safe_log_exception("install", f"do_install 忽略异常: {ve}", "warning")
                    install_state.log_line(
                        tr_msg("下载完整性校验跳过: %.80s", "Download integrity check skipped: %.80s", str(ve))
                    )
                break
            except Exception as e:
                safe_log_exception("install", f"do_install 忽略异常: {e}", "warning")
                last_error = str(e)
                install_state.log_line(
                    tr_msg("下载源 %s 失败: %.100s", "Download source %s failed: %.100s", src_name, last_error)
                )
                try:
                    if os.path.exists(zip_path):
                        os.remove(zip_path)
                except OSError:
                    # 删除损坏的临时文件失败可忽略
                    pass
                continue
        if not download_ok:
            raise RuntimeError(
                tr_msg(
                    "所有下载源均失败。最后错误: %s\n提示：可尝试在'下载源'中切换其他源，或使用'自定义下载地址'。",
                    "All download sources failed. Last error: %s\nTip: try switching sources in 'Download source' or use a custom download URL.",
                    last_error,
                )
            )
        install_state.set(phase="extract", percent=82, text=tr_msg("正在解压安装...", "Extracting..."))
        install_state.log_line(tr_msg("正在解压到暂存目录: %s", "Extracting to staging dir: %s", staging_extract))

        def cb2(i, total):
            install_state.set(
                percent=82 + int(i / total * 15), text=tr_msg("解压中 %d/%d", "Extracting %d/%d", i, total)
            )

        extract_zip(zip_path, staging_extract, progress_cb=cb2, cancel_flag=install_state.cancel)
        install_state.log_line(tr_msg("解压完成", "Extraction complete"))
        with contextlib.suppress(OSError):
            os.remove(zip_path)
        staged_exe = os.path.join(staging_extract, SERVER_EXE)
        if not os.path.exists(staged_exe):
            raise RuntimeError(f"安装失败：未在目录中找到 {SERVER_EXE}")
        # Linux 平台：确保服务端二进制具有可执行权限（部分镜像打包的 zip 可能丢失权限位）
        # 仅授予属主读写执行（0o700），避免 world-readable 权限面
        if not IS_WINDOWS:
            try:
                os.chmod(staged_exe, 0o700)
            except OSError as e:
                install_state.log_line(
                    tr_msg("警告: 设置可执行权限失败: %s", "Warning: failed to set executable permission: %s", e)
                )
        # 校验通过，才把暂存内容并入服务器目录（保留用户数据）
        install_state.set(phase="extract", percent=98, text=tr_msg("正在写入服务器目录...", "Applying files..."))
        skipped = apply_staged_install(staging_extract, dir_target)
        if skipped:
            install_state.log_line(
                tr_msg(
                    "已保留原有用户数据（未覆盖）: %s",
                    "Preserved existing user data (not overwritten): %s",
                    ", ".join(skipped),
                )
            )
        cleanup_staging(dir_target)
        exe = os.path.join(dir_target, SERVER_EXE)
        if not os.path.exists(exe):
            raise RuntimeError(f"安装失败：未在目录中找到 {SERVER_EXE}")
        install_state.set(phase="done", percent=100, text=tr_msg("安装完成", "Installation complete"), done=True)
        install_state.log_line(
            tr_msg("✔ 服务器安装完成！版本 %s", "✔ Server installation complete! Version %s", version)
        )
        install_state.log_line(tr_msg("  服务器程序: %s", "  Server program: %s", exe))
        settings["server_dir"] = dir_target
        settings["installed_version"] = version
        versions_map = settings.get("server_versions", {})
        if not isinstance(versions_map, dict):
            versions_map = {}
        versions_map[os.path.normcase(os.path.abspath(dir_target))] = version
        settings["server_versions"] = versions_map
        add_server_dir_history(dir_target)
        save_settings()
        if autostart:
            install_state.log_line(tr_msg(">>> 自动启动服务器...", ">>> Auto-starting server..."))
            try:
                p = get_server_proc()
                p.server_dir = dir_target
                p.start()
            except Exception as e:
                safe_log_exception("install", f"do_install 忽略异常: {e}", "warning")
                install_state.log_line(tr_msg("自动启动失败: %s", "Auto-start failed: %s", e))
    except InterruptedError:
        install_state.set(phase="idle", percent=0, text=tr_msg("已取消", "Cancelled"))
        install_state.log_line(tr_msg("已取消安装", "Installation cancelled"))
    except Exception as e:
        safe_log_exception("install", f"do_install 忽略异常: {e}", "warning")
        install_state.set(phase="error", error=str(e), text=tr_msg("安装失败: %s", "Installation failed: %s", e))
        install_state.log_line(tr_msg("安装失败: %s", "Installation failed: %s", e))
    finally:
        # 无论成功/取消/失败都清掉暂存目录，避免残留半成品占用磁盘；
        # 由于落盘发生在校验之后，失败时原服务器文件未被改动。
        with contextlib.suppress(Exception):
            cleanup_staging(dir_target)
        install_state.busy = False
