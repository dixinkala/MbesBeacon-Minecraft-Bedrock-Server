"""
安装与下载模块：InstallState、下载、解压、版本检测、安装流程。
"""

import contextlib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import zipfile

from .app_context import AppContext
from .app_logger import safe_log_exception
from .constants import CDN_TEMPLATE, IS_WINDOWS, LINKS_API, PLATFORM_TAG, SERVER_EXE
from .server import get_server_proc, server_running
from .utils import add_server_dir_history, detect_servers, save_settings
from .verify import load_official_hashes, verify_download_full


# 通过 AppContext 访问全局状态（消除延迟导入）
def _get_settings():
    return AppContext.instance().settings


def _get_console():
    return AppContext.instance().console


def _get_install_state():
    return AppContext.instance().install_state


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


def make_download_url(version, source_index=0):
    """根据版本号生成下载 URL（按当前平台生成对应平台路径）。"""

    if source_index < len(DOWNLOAD_SOURCES):
        template = DOWNLOAD_SOURCES[source_index]["template"]
        return _format_download_template(template, version)
    return CDN_TEMPLATE.format(version=version)


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
                except Exception:
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
                try:
                    from .app_logger import get_app_logger

                    get_app_logger().warning(f"install.py 停止服务器异常: {e}")
                except Exception:
                    # 停止旧服务器失败不影响目录切换
                    pass
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
VERSION_CACHE_TTL = 3600


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
    except Exception:
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
BDS_VERSIONS_JSON_URL = "https://raw.githubusercontent.com/Bedrock-OSS/BDS-Versions/main/versions.json"


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
    except Exception:
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
                                except Exception:
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
    install_state.set(phase="start", percent=0, text="开始安装...")
    try:
        os.makedirs(dir_target, exist_ok=True)
        # 安装前磁盘空间检查（服务端压缩包约 60-120MB，解压后需约 300MB）
        try:
            from .utils import check_disk_space

            ok_space, free_mb, required_mb = check_disk_space(dir_target, required_mb=300)
            if not ok_space:
                install_state.log_line(f"错误: 磁盘空间不足（剩余 {free_mb:.0f} MB，需要 {required_mb} MB）")
                raise RuntimeError(f"磁盘空间不足：剩余 {free_mb:.0f} MB，需要 {required_mb} MB，请清理磁盘后重试")
        except RuntimeError:
            raise
        except Exception as e:
            install_state.log_line(f"警告: 磁盘空间检查失败，已跳过（{str(e)[:60]}）")
        ignore_ssl = settings.get("ignore_ssl", False)
        install_state.log_line(">>> 开始安装 Minecraft 基岩版服务器")
        urls_to_try = []
        if custom_url and custom_url.strip():
            urls_to_try.append(("自定义地址", custom_url.strip()))
            install_state.log_line("使用自定义下载地址")
        elif version:
            install_state.log_line(f"指定版本: {version}")
            for i in range(len(DOWNLOAD_SOURCES)):
                idx = (source_index + i) % len(DOWNLOAD_SOURCES)
                src = DOWNLOAD_SOURCES[idx]
                urls_to_try.append((src["name"], _format_download_template(src["template"], version)))
        else:
            if not (settings.get("_latest") or {}).get("version"):
                install_state.set(percent=1, text="正在获取最新版本信息...")
                install_state.log_line("正在获取最新版本信息...")
                settings["_latest"] = get_latest_server_info(ignore_ssl=ignore_ssl)
            latest = settings["_latest"]
            version = latest["version"]
            urls_to_try.append(("官方 API", latest["win_url"]))
            for src in DOWNLOAD_SOURCES[1:]:
                urls_to_try.append((src["name"], _format_download_template(src["template"], version)))
            install_state.log_line(f"最新稳定版: {version}")
        zip_path = os.path.join(dir_target, f"bedrock-server-{version}.zip")
        download_ok = False
        last_error = ""
        for src_name, url in urls_to_try:
            if install_state.cancel.is_set():
                break
            install_state.log_line(f"尝试下载源: {src_name}")
            install_state.log_line(f"下载地址: {url}")
            install_state.set(phase="download", percent=3, text=f"正在从 {src_name} 下载...")
            try:

                def cb(done, total):
                    if total > 0:
                        pct = 3 + int(done / total * 77)
                        install_state.set(
                            percent=pct,
                            text=f"下载中 {int(done / total * 100)}%  ({done // 1024} / {total // 1024} KB)",
                        )
                    else:
                        install_state.set(percent=3, text=f"下载中... {done // 1024} KB")

                download_file(url, zip_path, progress_cb=cb, cancel_flag=install_state.cancel, ignore_ssl=ignore_ssl)
                download_ok = True
                install_state.log_line(
                    f"下载完成 (源: {src_name}): {os.path.basename(zip_path)} ({os.path.getsize(zip_path) / 1048576:.1f} MB)"
                )
                # 下载完整性校验
                try:
                    # 尝试从 Bedrock-OSS/BDS-Versions 获取官方哈希（用于对比验证）
                    official_loaded = False
                    try:
                        official_count = load_official_hashes()
                        if official_count > 0:
                            install_state.log_line(f"已加载 {official_count} 个官方版本哈希用于校验")
                            official_loaded = True
                    except Exception as e:
                        install_state.log_line(f"警告: 官方哈希加载失败 ({str(e)[:60]})，将使用首次信任机制(TOFU)")

                    if not official_loaded:
                        install_state.log_line("提示: 无法获取官方哈希，本次下载将记录SHA256供后续对比（首次信任机制）")
                        install_state.log_line("建议: 如网络环境特殊，可在设置中检查网络连接或稍后重试")

                    verify_result = verify_download_full(zip_path, version=version, check_pe=True)
                    if verify_result["ok"]:
                        install_state.log_line(
                            "下载完整性校验通过（文件大小: {:.1f} MB, SHA256: {}...）".format(
                                verify_result["checks"]["file_size"]["size_mb"],
                                verify_result["checks"]["sha256"]["actual_hash"][:16],
                            )
                        )
                    else:
                        error_msg = "; ".join(verify_result["errors"])
                        install_state.log_line(f"错误: 下载完整性校验失败: {error_msg}")
                        # 校验失败时中止安装，删除损坏的文件
                        try:
                            if os.path.exists(zip_path):
                                os.remove(zip_path)
                        except OSError:
                            # 删除损坏的 ZIP 失败可忽略
                            pass
                        raise RuntimeError(
                            f"下载文件完整性校验失败: {error_msg}\n提示: 可尝试切换其他下载源或使用自定义地址"
                        )
                except RuntimeError:
                    raise  # 重新抛出校验失败的异常
                except Exception as ve:
                    install_state.log_line(f"下载完整性校验跳过: {str(ve)[:80]}")
                break
            except Exception as e:
                last_error = str(e)
                install_state.log_line(f"下载源 {src_name} 失败: {last_error[:100]}")
                try:
                    if os.path.exists(zip_path):
                        os.remove(zip_path)
                except OSError:
                    # 删除损坏的临时文件失败可忽略
                    pass
                continue
        if not download_ok:
            raise RuntimeError(
                f"所有下载源均失败。最后错误: {last_error}\n提示：可尝试在'下载源'中切换其他源，或使用'自定义下载地址'。"
            )
        install_state.set(phase="extract", percent=82, text="正在解压安装...")
        install_state.log_line(f"正在解压到: {dir_target}")

        def cb2(i, total):
            install_state.set(percent=82 + int(i / total * 15), text=f"解压中 {i}/{total}")

        extract_zip(zip_path, dir_target, progress_cb=cb2, cancel_flag=install_state.cancel)
        install_state.log_line("解压完成")
        with contextlib.suppress(OSError):
            os.remove(zip_path)
        exe = os.path.join(dir_target, SERVER_EXE)
        if not os.path.exists(exe):
            raise RuntimeError(f"安装失败：未在目录中找到 {SERVER_EXE}")
        # Linux 平台：确保服务端二进制具有可执行权限（部分镜像打包的 zip 可能丢失权限位）
        # 仅授予属主读写执行（0o700），避免 world-readable 权限面
        if not IS_WINDOWS:
            try:
                os.chmod(exe, 0o700)
            except OSError as e:
                install_state.log_line(f"警告: 设置可执行权限失败: {e}")
        install_state.set(phase="done", percent=100, text="安装完成", done=True)
        install_state.log_line(f"✔ 服务器安装完成！版本 {version}")
        install_state.log_line(f"  服务器程序: {exe}")
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
            install_state.log_line(">>> 自动启动服务器...")
            try:
                p = get_server_proc()
                p.server_dir = dir_target
                p.start()
            except Exception as e:
                install_state.log_line(f"自动启动失败: {e}")
    except InterruptedError:
        install_state.set(phase="idle", percent=0, text="已取消")
        install_state.log_line("已取消安装")
    except Exception as e:
        install_state.set(phase="error", error=str(e), text=f"安装失败: {e}")
        install_state.log_line(f"安装失败: {e}")
    finally:
        install_state.busy = False
