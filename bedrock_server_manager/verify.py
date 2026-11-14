"""
下载完整性校验模块：对下载的服务器文件进行多重校验，
包括 ZIP 完整性、SHA256 哈希、文件大小合理性、平台对应可执行文件校验（Windows: PE 签名 / Linux: ELF 格式）。
"""

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
import zipfile
from typing import Any

from .app_logger import safe_log_exception
from .constants import BDS_VERSIONS_JSON_URL, CACHE_DIR, IS_WINDOWS, PLATFORM_TAG, SERVER_EXE

# BDS 服务端文件大小合理范围（字节）
# 注意：BDS 版本持续更新，文件大小可能增长，上限设置较宽松以避免误判
MIN_BDS_SIZE = 30 * 1024 * 1024  # 30MB（历史版本可能较小）
MAX_BDS_SIZE = 500 * 1024 * 1024  # 500MB（预留未来版本增长空间）

# 已知的官方版本 SHA256 哈希（由 TOFU 记录或调用方显式提供；官方不公开哈希）
_KNOWN_HASHES: dict[str, str] = {}


# 版本清单缓存：避免每次安装都重试一个已经失败的请求（最坏情况是
# 每次安装多等一个 10 秒连接超时）。
_OFFICIAL_VERSIONS_CACHE: dict[str, Any] = {"data": None, "time": 0.0, "failed_at": 0.0}
_OFFICIAL_VERSIONS_TTL = 6 * 3600.0  # 成功缓存 6 小时
_OFFICIAL_VERSIONS_RETRY = 600.0  # 失败后 10 分钟内不再重试


def fetch_official_versions(timeout: int = 10, force: bool = False) -> dict:
    """获取官方的 BDS 版本清单（按平台分组）。

    Args:
        timeout: 请求超时时间（秒）
        force: 忽略缓存强制刷新

    Returns:
        dict: ``{"windows": {"stable": "1.26.63.1", "preview": ..., "versions": [...]}, "linux": {...}}``；
        获取失败返回空字典（失败会被短时缓存，避免每次安装都等超时）
    """
    import urllib.error
    import urllib.request

    now = time.time()
    cached = _OFFICIAL_VERSIONS_CACHE
    if not force:
        if cached["data"] and now - cached["time"] < _OFFICIAL_VERSIONS_TTL:
            return cached["data"]
        if cached["failed_at"] and now - cached["failed_at"] < _OFFICIAL_VERSIONS_RETRY:
            return {}

    result = {}
    try:
        req = urllib.request.Request(BDS_VERSIONS_JSON_URL, headers={"User-Agent": "Mozilla/5.0 MbesBeacon"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        if isinstance(data, dict):
            for platform in ("windows", "linux"):
                info = data.get(platform)
                if isinstance(info, dict):
                    versions = [str(v) for v in (info.get("versions") or []) if v]
                    previews = [str(v) for v in (info.get("preview_versions") or []) if v]
                    result[platform] = {
                        "stable": str(info.get("stable") or ""),
                        "preview": str(info.get("preview") or ""),
                        "versions": versions,
                        "preview_versions": previews,
                    }
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as e:
        safe_log_exception("verify", f"获取官方版本清单失败: {e}", "warning")
        result = {}
    except Exception as e:
        safe_log_exception("verify", f"获取官方版本清单异常: {e}", "warning")
        result = {}

    if result:
        cached["data"] = result
        cached["time"] = now
        cached["failed_at"] = 0.0
    else:
        cached["failed_at"] = now
    return result


def get_official_version_info(version: str | None = None) -> dict:
    """查询某个版本号在官方清单中的状态。

    Args:
        version: 待查询的版本号；None 表示只取当前平台的稳定版

    Returns:
        dict: ``{"available": bool, "known": bool|None, "stable": str, "preview": str}``
            - ``known`` 为 None 表示官方清单暂时取不到（无法判断，不是"非官方"）
    """
    platform = "windows" if IS_WINDOWS else "linux"
    info = fetch_official_versions().get(platform) or {}
    if not info:
        return {"available": False, "known": None, "stable": "", "preview": ""}
    known = None
    if version:
        all_versions = set(info.get("versions") or []) | set(info.get("preview_versions") or [])
        if info.get("stable"):
            all_versions.add(info["stable"])
        if info.get("preview"):
            all_versions.add(info["preview"])
        known = version in all_versions
    return {
        "available": True,
        "known": known,
        "stable": info.get("stable") or "",
        "preview": info.get("preview") or "",
    }


# 本地哈希缓存文件路径（首次下载时记录，后续下载对比）
# 统一使用应用数据目录（constants.CACHE_DIR）；旧版本遗留的
# ~/.bedrock_server_hashes.json 在首次读取缓存时自动迁移到新路径
_HASH_CACHE_FILE = os.path.join(CACHE_DIR, "hashes.json")
_LEGACY_HASH_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".bedrock_server_hashes.json")

# TOFU 信任库的 HMAC 签名：清单 + 签名 + 密钥三件套。
# 早期版本只有裸 JSON（无签名），任意本地进程改写清单即可让恶意包通过校验。
# 这里用仅属主可读的密钥做 HMAC，并把"清单"与"签名"分开存放，
# 篡改任一侧都会暴露；签名文件缺失（除旧格式）视为被删除并丢弃清单。
_HASH_CACHE_MAGIC = "mbesbeacon-tofu-v2"
_HASH_CACHE_MAC_SUFFIX = ".mac"
_HASH_CACHE_KEY_NAME = ".hashkey"


def _mac_file_path() -> str:
    """HMAC 签名文件路径（跟随 _HASH_CACHE_FILE）。"""
    return _HASH_CACHE_FILE + _HASH_CACHE_MAC_SUFFIX


def _key_file_path() -> str:
    """HMAC 密钥文件路径（与清单同目录）。"""
    return os.path.join(os.path.dirname(_HASH_CACHE_FILE), _HASH_CACHE_KEY_NAME)


def _env_flag(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量（``1/true/yes/on`` 视为真）。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _read_private_file(path: str) -> bytes | None:
    """读取仅属主可读的私有文件；不存在返回 None。"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _write_private_file(path: str, data: bytes) -> None:
    """以仅属主可读写的权限写入文件（崩溃安全：先写临时文件再替换）。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # 临时名唯一：并发写同一缓存（多线程安装/校验）时共用 ".tmp" 会互相覆盖
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=parent or ".")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.remove(tmp)


def _get_cache_key() -> bytes | None:
    """获取（必要时创建）TOFU 清单的 HMAC 密钥。

    密钥文件权限 0600，仅当前用户可读。无法创建时返回 None，
    调用方应退化为"仅内存信任"而不是接受未签名的磁盘清单。
    """
    existing = _read_private_file(_key_file_path())
    if existing and len(existing) >= 32:
        return existing
    try:
        key = secrets.token_bytes(32)
        _write_private_file(_key_file_path(), key)
        return key
    except Exception as e:
        safe_log_exception("verify", f"创建哈希校验密钥失败: {e}", "warning")
        return None


def _compute_cache_mac(key: bytes, cache: dict[str, str]) -> str:
    """计算哈希清单的 HMAC（含魔数与平台，避免跨平台/跨版本重放）。"""
    payload = json.dumps(
        {"magic": _HASH_CACHE_MAGIC, "platform": PLATFORM_TAG, "hashes": cache},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _load_hash_cache() -> dict[str, str]:
    """加载本地哈希缓存（带 HMAC 完整性校验）。

    优先读取统一的应用数据目录缓存；若该文件尚不存在而旧版遗留缓存
    （~/.bedrock_server_hashes.json）存在，则一次性迁移到新路径并清理旧文件。

    校验失败的清单会被丢弃（返回空字典），并在应用日志中记录警告：
    宁可退回"无历史信任"重新 TOFU，也不要信任一份可能被篡改的清单。
    """
    try:
        if os.path.isfile(_HASH_CACHE_FILE):
            with open(_HASH_CACHE_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            cache = raw.get("hashes") if isinstance(raw, dict) and "hashes" in raw else raw
            if not isinstance(cache, dict):
                return {}
            key = _get_cache_key()
            if key is None:
                return {}
            mac_data = _read_private_file(_mac_file_path())
            expected_mac = _compute_cache_mac(key, cache)
            if mac_data is None:
                # 只有"真正的旧版格式"（没有 magic 标记）才允许一次性升级重签。
                # 带 magic 却没有 .mac ⇒ 签名文件被删了，必须当作篡改丢弃；
                # 否则攻击者只要删掉 1 个文件就能让任意伪造清单被接受。
                is_legacy = not (isinstance(raw, dict) and raw.get("magic"))
                if is_legacy:
                    safe_log_exception("verify", "检测到未签名的旧版哈希清单，已升级为签名格式", "info")
                    _save_hash_cache(cache)
                    return cache
                safe_log_exception(
                    "verify",
                    "哈希清单的签名文件缺失（可能被删除以绕过校验），已忽略该清单并重新建立信任",
                    "warning",
                )
                return {}
            actual_mac = mac_data.decode("utf-8", "ignore").strip()
            if not hmac.compare_digest(expected_mac, actual_mac):
                safe_log_exception(
                    "verify",
                    "哈希清单签名不匹配（可能被篡改），已忽略该清单并重新建立信任",
                    "warning",
                )
                return {}
            return cache
        # 首次读取时迁移旧版遗留缓存（兜底：即使启动时迁移未执行也能收敛路径）
        if os.path.isfile(_LEGACY_HASH_CACHE_FILE):
            with open(_LEGACY_HASH_CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
            if isinstance(cache, dict) and cache:
                _save_hash_cache(cache)
            with contextlib.suppress(OSError):
                os.remove(_LEGACY_HASH_CACHE_FILE)
            return cache if isinstance(cache, dict) else {}
    except Exception as e:
        safe_log_exception("verify", f"操作失败: {e}", "warning")
    return {}


def _save_hash_cache(cache: dict[str, str]) -> None:
    """保存本地哈希缓存（清单与 HMAC 分开写，原子替换）。"""
    try:
        key = _get_cache_key()
        if key is None:
            safe_log_exception("verify", "无法获取哈希校验密钥，跳过持久化", "warning")
            return
        os.makedirs(os.path.dirname(_HASH_CACHE_FILE), exist_ok=True)
        payload = json.dumps({"magic": _HASH_CACHE_MAGIC, "hashes": cache}, ensure_ascii=False, indent=2)
        _write_private_file(_HASH_CACHE_FILE, payload.encode("utf-8"))
        _write_private_file(_mac_file_path(), _compute_cache_mac(key, cache).encode("ascii"))
    except Exception as e:
        safe_log_exception("verify", f"操作失败: {e}", "warning")


def register_known_hash(version: str, sha256_hash: str) -> None:
    """注册已知版本的 SHA256 哈希。

    Args:
        version: 版本号
        sha256_hash: SHA256 哈希值
    """
    _KNOWN_HASHES[version] = sha256_hash
    # 同时保存到本地缓存
    cache = _load_hash_cache()
    cache[version] = sha256_hash
    _save_hash_cache(cache)


def get_known_hash(version: str) -> str | None:
    """获取已知版本的 SHA256 哈希。

    优先从内存缓存获取，然后从本地文件缓存获取。

    Args:
        version: 版本号

    Returns:
        str: SHA256 哈希值，未找到返回 None
    """
    if version in _KNOWN_HASHES:
        return _KNOWN_HASHES[version]
    # 从本地缓存加载
    cache = _load_hash_cache()
    if version in cache:
        _KNOWN_HASHES[version] = cache[version]
        return cache[version]
    return None


def calculate_sha256(file_path: str, chunk_size: int = 8192) -> str | None:
    """计算文件的 SHA256 哈希值。

    Args:
        file_path: 文件路径
        chunk_size: 读取块大小

    Returns:
        str: SHA256 哈希值（十六进制字符串），失败返回 None
    """
    if not os.path.isfile(file_path):
        return None
    try:
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                sha256.update(chunk)
        return sha256.hexdigest()
    except (FileNotFoundError, PermissionError, OSError, ValueError) as e:
        safe_log_exception("verify.py", f"计算SHA256失败: {e}", "warning")
        return None


def verify_file_size(
    file_path: str, min_size: int = MIN_BDS_SIZE, max_size: int = MAX_BDS_SIZE
) -> tuple[bool, str, int]:
    """校验文件大小是否在合理范围内。

    Args:
        file_path: 文件路径
        min_size: 最小大小（字节）
        max_size: 最大大小（字节）

    Returns:
        tuple: (ok, message, file_size)
    """
    if not os.path.isfile(file_path):
        return False, "文件不存在", 0
    try:
        file_size = os.path.getsize(file_path)
        if file_size < min_size:
            return (
                False,
                f"文件过小（{file_size / (1024 * 1024):.1f} MB），可能下载不完整，预期至少 {min_size / (1024 * 1024):.1f} MB",
                file_size,
            )
        if file_size > max_size:
            return (
                False,
                f"文件过大（{file_size / (1024 * 1024):.1f} MB），可能不是有效的服务端文件，预期最多 {max_size / (1024 * 1024):.1f} MB",
                file_size,
            )
        return True, "文件大小正常（%.1f MB）" % (file_size / (1024 * 1024)), file_size
    except Exception as e:
        safe_log_exception("verify", f"verify_file_size 忽略异常: {e}", "warning")
        return False, f"文件大小检查失败: {str(e)}", 0


def verify_zip_integrity(zip_path: str) -> tuple[bool, str]:
    """校验 ZIP 文件完整性（使用 zipfile.testzip()）。

    Args:
        zip_path: ZIP 文件路径

    Returns:
        tuple: (ok, message)
    """
    if not os.path.isfile(zip_path):
        return False, "文件不存在"
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad = zf.testzip()
            if bad:
                return False, f"损坏的文件: {bad}"
        return True, "ZIP 文件完整性校验通过"
    except zipfile.BadZipFile:
        return False, "不是有效的 ZIP 文件"
    except Exception as e:
        safe_log_exception("verify", f"verify_zip_integrity 忽略异常: {e}", "warning")
        return False, f"ZIP 校验失败: {str(e)}"


def verify_sha256(
    file_path: str, expected_hash: str | None = None, version: str | None = None
) -> tuple[bool, str, str]:
    """校验文件的 SHA256 哈希。

    Args:
        file_path: 文件路径
        expected_hash: 预期的 SHA256 哈希值（如果提供）
        version: 服务器版本号（用于查找已知哈希）

    Returns:
        tuple: (ok, message, actual_hash)
    """
    actual_hash = calculate_sha256(file_path)
    if not actual_hash:
        return False, "无法计算文件哈希", ""

    # 如果提供了预期哈希，直接比较
    if expected_hash:
        if actual_hash.lower() == expected_hash.lower():
            return True, "SHA256 哈希校验通过", actual_hash
        else:
            return False, "SHA256 哈希不匹配，文件可能被篡改或下载损坏", actual_hash

    # 如果提供了版本号，查找已知哈希（首次信任机制）
    if version:
        known = get_known_hash(version)
        if known:
            if actual_hash.lower() == known.lower():
                return True, f"SHA256 哈希校验通过（版本 {version}，与历史记录一致）", actual_hash
            else:
                return False, f"SHA256 哈希与历史记录不匹配（版本 {version}），文件可能被篡改", actual_hash
        else:
            # 首次下载该版本，记录哈希到本地缓存
            register_known_hash(version, actual_hash)
            return True, f"首次下载版本 {version}，已记录 SHA256 哈希", actual_hash

    # 没有预期哈希和版本号，只返回实际哈希供参考
    return True, "已计算 SHA256 哈希（无预期值可对比）", actual_hash


def _find_signer_cert(crypt32, h_msg, h_store):
    """精确定位"实际签名者"的证书。

    不能简单取证书存储里的第一个证书：多签名（双签/交叉签名）的 PE 里
    存储可能包含多个候选、甚至以 CA 证书开头，取错会把签名者误判成非官方。

    做法（Windows 标准流程）：
    1. ``CryptMsgGetParam(CMSG_SIGNER_CERT_INFO_PARAM)`` 取第 0 个签名者的
       ``CERT_INFO``（含 Issuer + SerialNumber）；
    2. ``CertFindCertificateInStore(CERT_FIND_SUBJECT_CERT)`` 按 Issuer+Serial 精确匹配。

    Args:
        crypt32: ctypes.windll.crypt32
        h_msg: CryptQueryObject 返回的消息句柄
        h_store: CryptQueryObject 返回的证书存储句柄

    Returns:
        证书上下文指针；无法精确定位时返回 None（调用方回退到"取首个证书"）。
        返回的指针需由调用方 ``CertFreeCertificateContext`` 释放。
    """
    import ctypes.wintypes

    CMSG_SIGNER_CERT_INFO_PARAM = 7
    CERT_FIND_SUBJECT_CERT = 0x000B0000
    X509_ASN_ENCODING = 0x00000001
    PKCS_7_ASN_ENCODING = 0x00010000

    if not h_msg or not h_store:
        return None
    try:
        if not getattr(crypt32, "_mbes_argtypes_set", False):
            crypt32.CryptMsgGetParam.argtypes = [
                ctypes.wintypes.HANDLE,
                ctypes.wintypes.DWORD,
                ctypes.wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.wintypes.DWORD),
            ]
            crypt32.CryptMsgGetParam.restype = ctypes.c_bool
            crypt32.CertFindCertificateInStore.argtypes = [
                ctypes.wintypes.HANDLE,
                ctypes.wintypes.DWORD,
                ctypes.wintypes.DWORD,
                ctypes.wintypes.DWORD,
                ctypes.c_void_p,  # pvFindPara（CERT_FIND_SUBJECT_CERT 时是 PCERT_INFO）
                ctypes.c_void_p,  # pPrevCertContext
            ]
            crypt32.CertFindCertificateInStore.restype = ctypes.c_void_p
            crypt32._mbes_argtypes_set = True

        size = ctypes.wintypes.DWORD(0)
        if not crypt32.CryptMsgGetParam(h_msg, CMSG_SIGNER_CERT_INFO_PARAM, 0, None, ctypes.byref(size)):
            return None
        if size.value <= 0:
            return None
        buf = (ctypes.c_byte * size.value)()
        if not crypt32.CryptMsgGetParam(h_msg, CMSG_SIGNER_CERT_INFO_PARAM, 0, buf, ctypes.byref(size)):
            return None
        found = crypt32.CertFindCertificateInStore(
            h_store,
            X509_ASN_ENCODING | PKCS_7_ASN_ENCODING,
            0,
            CERT_FIND_SUBJECT_CERT,
            ctypes.cast(buf, ctypes.c_void_p),
            None,
        )
        return found or None
    except Exception as e:
        safe_log_exception("verify", f"定位签名者证书失败（回退到首个证书）: {e}", "warning")
        return None


def _verify_signer_identity(exe_path: str) -> tuple[bool, str]:
    """验证 PE 文件的数字签名者身份是否为 Microsoft/Mojang。

    使用 CryptQueryObject 获取签名者证书，然后检查证书主题。
    返回 (ok, message)：ok=True 表示签名者为官方，message 为签名者名称。
    """
    try:
        import ctypes.wintypes

        # 定义 CryptQueryObject 所需的常量和结构体
        CERT_QUERY_OBJECT_FILE = 0x00000001
        # CERT_QUERY_CONTENT_FLAG_XXX 是 (1 << CERT_QUERY_CONTENT_XXX)，
        # 而 CERT_QUERY_CONTENT_PKCS7_SIGNED_EMBED = 10 → flag 必须是 0x400。
        # 早期写成 0x10（= 1<<4，对应 SERIALIZED_STORE），于是对**真正带嵌入式
        # 签名的 exe** 也查不到证书，签名者身份校验从实现起就没成功过。
        CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED = 0x00000400
        CERT_QUERY_FORMAT_FLAG_BINARY = 0x00000002
        CERT_NAME_SIMPLE_DISPLAY_TYPE = 4

        class CERT_INFO(ctypes.Structure):
            _fields_ = [
                ("dwVersion", ctypes.wintypes.DWORD),
                ("SerialNumber", ctypes.c_void_p),
                ("SignatureAlgorithm", ctypes.c_void_p),
                ("Issuer", ctypes.c_void_p),
                ("NotBefore", ctypes.c_void_p),
                ("NotAfter", ctypes.c_void_p),
                ("Subject", ctypes.c_void_p),
                ("SubjectPublicKeyInfo", ctypes.c_void_p),
                ("IssuerUniqueId", ctypes.c_void_p),
                ("SubjectUniqueId", ctypes.c_void_p),
                ("Extension", ctypes.c_void_p),
            ]

        class CERT_CONTEXT(ctypes.Structure):
            _fields_ = [
                ("dwCertEncodingType", ctypes.wintypes.DWORD),
                ("pbCertEncoded", ctypes.POINTER(ctypes.c_byte)),
                ("cbCertEncoded", ctypes.wintypes.DWORD),
                ("pCertInfo", ctypes.POINTER(CERT_INFO)),
                ("hCertStore", ctypes.wintypes.HANDLE),
            ]

        crypt32 = ctypes.windll.crypt32
        # 显式声明签名，避免 64 位下指针参数被按 int 截断
        crypt32.CryptQueryObject.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.c_wchar_p,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(ctypes.wintypes.DWORD),
            ctypes.POINTER(ctypes.wintypes.DWORD),
            ctypes.POINTER(ctypes.wintypes.DWORD),
            ctypes.POINTER(ctypes.wintypes.HANDLE),
            ctypes.POINTER(ctypes.wintypes.HANDLE),
            ctypes.POINTER(ctypes.POINTER(CERT_CONTEXT)),
        ]
        crypt32.CryptQueryObject.restype = ctypes.c_bool
        crypt32.CertGetNameStringW.argtypes = [
            ctypes.POINTER(CERT_CONTEXT),
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.wintypes.DWORD,
        ]
        crypt32.CertGetNameStringW.restype = ctypes.wintypes.DWORD
        crypt32.CertEnumCertificatesInStore.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(CERT_CONTEXT)]
        crypt32.CertEnumCertificatesInStore.restype = ctypes.POINTER(CERT_CONTEXT)
        crypt32.CertCloseStore.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
        crypt32.CertCloseStore.restype = ctypes.c_bool
        # 必须显式声明为 c_void_p：默认按 c_int 转换会把 64 位指针
        # 判成 "int too long to convert"（OverflowError）。
        crypt32.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]
        crypt32.CertFreeCertificateContext.restype = ctypes.c_bool
        crypt32.CryptMsgClose.argtypes = [ctypes.wintypes.HANDLE]
        crypt32.CryptMsgClose.restype = ctypes.c_bool

        # 调用 CryptQueryObject 获取证书上下文
        h_store = ctypes.wintypes.HANDLE()
        h_msg = ctypes.wintypes.HANDLE()
        pdw_encoding = ctypes.wintypes.DWORD()
        pdw_content_type = ctypes.wintypes.DWORD()
        pdw_format_type = ctypes.wintypes.DWORD()
        pCertContext = ctypes.POINTER(CERT_CONTEXT)()

        success = crypt32.CryptQueryObject(
            CERT_QUERY_OBJECT_FILE,
            ctypes.c_wchar_p(exe_path),
            CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED,
            CERT_QUERY_FORMAT_FLAG_BINARY,
            0,
            ctypes.byref(pdw_encoding),
            ctypes.byref(pdw_content_type),
            ctypes.byref(pdw_format_type),
            ctypes.byref(h_store),
            ctypes.byref(h_msg),
            ctypes.byref(pCertContext),
        )

        if not success:
            return False, "无法获取签名证书"

        enum_cert = None
        exact_ptr = None
        try:
            exact_ptr = _find_signer_cert(crypt32, h_msg, h_store)
            if exact_ptr:
                # CertFindCertificateInStore 返回的是裸指针（整数），
                # 需转成 CERT_CONTEXT* 才能交给 CertGetNameStringW
                cert_ctx = ctypes.cast(exact_ptr, ctypes.POINTER(CERT_CONTEXT))
            elif pCertContext:
                cert_ctx = pCertContext
            elif h_store:
                # 精确匹配失败（多签名/结构异常）时回退：单签名 PE 的
                # 存储里第一个证书就是签名者（其后是中间/根 CA）。
                enum_cert = crypt32.CertEnumCertificatesInStore(h_store, None)
                if not enum_cert:
                    return False, "签名证书存储为空"
                cert_ctx = enum_cert
            else:
                return False, "无法获取签名证书"

            # 获取签名者名称（Subject）
            name_len = crypt32.CertGetNameStringW(
                cert_ctx,
                CERT_NAME_SIMPLE_DISPLAY_TYPE,
                0,
                None,
                None,
                0,
            )
            if name_len <= 1:
                return False, "无法获取签名者名称"

            name_buf = ctypes.create_unicode_buffer(name_len)
            crypt32.CertGetNameStringW(
                cert_ctx,
                CERT_NAME_SIMPLE_DISPLAY_TYPE,
                0,
                None,
                name_buf,
                name_len,
            )
            signer_name = name_buf.value

            # 检查签名者是否为 Microsoft 或 Mojang
            is_official = any(keyword in signer_name for keyword in ("Microsoft", "Mojang", "Minecraft"))
            return is_official, f"签名者: {signer_name}"
        finally:
            # 释放证书上下文与查询出来的存储/消息
            if exact_ptr:
                crypt32.CertFreeCertificateContext(exact_ptr)
            if pCertContext:
                crypt32.CertFreeCertificateContext(pCertContext)
            if enum_cert:
                # 枚举返回的最后一个证书需要显式释放（前序由下一次枚举自动释放）
                crypt32.CertFreeCertificateContext(enum_cert)
            if h_store:
                crypt32.CertCloseStore(h_store, 0)
            if h_msg:
                crypt32.CryptMsgClose(h_msg)
    except ImportError:
        return False, "非 Windows 平台，无法验证签名者身份"
    except Exception as e:
        safe_log_exception("verify", f"_verify_signer_identity 忽略异常: {e}", "warning")
        return False, f"签名者验证失败: {str(e)}"


# 完整性类错误码：文件被改过/证书被伪造，任何模式都必须判失败。
_INTEGRITY_ERRORS = {
    0x80096010: "数字签名的摘要不匹配（文件已被篡改）",
    0x80096004: "签名证书自身签名无效（证书被伪造或损坏）",
    0x80096019: "签名证书不满足基本约束（可能被用于非签名用途，属可疑证书）",
    0x800B0111: "该签名证书已被系统显式 distrust",
}

# 信任/身份类错误码：可能是合法的自签名或过期证书，strict 才硬失败。
_TRUST_ERRORS = {
    0x800B0100: "PE 文件未签名，无法证明来源可信",
    0x800B0003: "PE 文件签名结构无法解析，无法证明来源可信",
    0x800B0001: "PE 文件签名提供程序无法识别",
    0x800B0109: "PE 文件签名证书的根证书不受信任",
    0x800B010A: "PE 文件签名证书链无法验证到受信任根",
    0x800B0101: "PE 文件签名证书已过期",
    0x800B0004: "PE 文件签名证书未被信任",
}


def _is_integrity_violation_message(msg: str) -> bool:
    """判断签名校验的失败原因是否属于"文件被改过"这类硬失败。"""
    if not msg:
        return False
    return any(reason in msg for reason in _INTEGRITY_ERRORS.values())


def _pe_signature_verdict(exe_path: str, strict: bool = False) -> tuple[bool, str, bool, bool]:
    """校验 Windows PE 文件格式及数字签名状态。

    校验内容：
    1. PE 文件格式（MZ 头 + PE 签名 + 机器类型）
    2. 数字签名状态与签名者身份（Microsoft / Mojang / Minecraft）

    **strict 语义**（这是本函数与早期实现的关键差别）：

    - 篡改类错误（``TRUST_E_BAD_DIGEST``）、证书链/有效期错误 —— **任何模式都判失败**。
      这一类是明确的"文件被改过"信号，必须硬失败。
    - 未签名（``TRUST_E_NOSIGNATURE``）、无法解析、签名者非官方：
      ``strict=True`` 判失败；``strict=False``（默认）**告警放行**，
      并把原因写进返回值，由调用方结合 SHA256/官方哈希判断。

    为什么默认不是 strict：Mojang 发行的 ``bedrock_server.exe`` **是否带嵌入式
    Authenticode 签名无法离线确认**，而签名子系统本身此前是坏的（GUID 写错，
    对任何文件都返回"无法解析"）。在"校验器可能整体失效"的前提下把
    "未签名"当硬失败，会让官方源安装 100% 中断。因此默认只强制拦篡改，
    未签名降级为告警；需要强约束时通过 ``strict=True``（或环境变量
    ``MBES_REQUIRE_SIGNATURE=1``）开启。

    Args:
        exe_path: EXE 文件路径
        strict: 是否把"未签名/无法确认"也当作失败

    Returns:
        tuple: (ok, message)
            ok=True: 格式有效，且签名有效+签名者官方；或非严格模式下仅告警放行
            ok=False: 格式无效，或签名被篡改/证书链无效，或严格模式下的未签名
    """
    if not os.path.isfile(exe_path):
        return False, "文件不存在", False, False
    if not IS_WINDOWS:
        # 非 Windows 平台无 PE 文件（Linux 使用 ELF 格式，由 verify_elf_executable 校验）
        return False, "非 Windows 平台，无 PE 签名（Linux 使用 ELF 校验）", False, False
    try:
        import struct

        # 1. 检查 MZ 头（DOS 头）
        with open(exe_path, "rb") as f:
            mz_header = f.read(2)
            if mz_header != b"MZ":
                return False, "不是有效的 Windows PE 文件（缺少 MZ 头）", False, False
            # 读取 e_lfanew（PE 头偏移）
            f.seek(0x3C)
            pe_offset = struct.unpack("<I", f.read(4))[0]
            # 检查 PE 签名
            f.seek(pe_offset)
            pe_signature = f.read(4)
            if pe_signature != b"PE\x00\x00":
                return False, "不是有效的 Windows PE 文件（缺少 PE 签名）", False, False
            # 读取机器类型
            machine = struct.unpack("<H", f.read(2))[0]
            # 0x8664=x64, 0x14C=x86, 0xAA64=ARM64
            if machine not in (0x8664, 0x14C, 0xAA64):
                return False, f"不支持的 CPU 架构（机器类型: 0x{machine:04X}）", False, False

        # 2. 尝试使用 WinVerifyTrust API 验证数字签名（Windows 专用）
        try:
            import ctypes.wintypes

            # 定义 WinVerifyTrust 所需的结构体
            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", ctypes.wintypes.DWORD),
                    ("Data2", ctypes.wintypes.WORD),
                    ("Data3", ctypes.wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            class WINTRUST_FILE_INFO(ctypes.Structure):
                _fields_ = [
                    ("cbStruct", ctypes.wintypes.DWORD),
                    ("pcwszFilePath", ctypes.wintypes.LPCWSTR),
                    ("hFile", ctypes.wintypes.HANDLE),
                    ("pgKnownSubject", ctypes.POINTER(GUID)),
                ]

            class WINTRUST_DATA(ctypes.Structure):
                _fields_ = [
                    ("cbStruct", ctypes.wintypes.DWORD),
                    ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p),
                    ("dwUIChoice", ctypes.wintypes.DWORD),
                    ("fdwRevocationChecks", ctypes.wintypes.DWORD),
                    ("dwUnionChoice", ctypes.wintypes.DWORD),
                    ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)),
                    ("dwStateAction", ctypes.wintypes.DWORD),
                    ("hWVTStateData", ctypes.wintypes.HANDLE),
                    ("pwszURLReference", ctypes.wintypes.LPCWSTR),
                    ("dwProvFlags", ctypes.wintypes.DWORD),
                    ("dwUIContext", ctypes.wintypes.DWORD),
                ]

            WTD_UI_NONE = 2
            WTD_REVOKE_NONE = 0
            WTD_CHOICE_FILE = 1
            WTD_STATEACTION_VERIFY = 1
            WTD_STATEACTION_CLOSE = 2
            WTD_REVOCATION_CHECK_NONE = 0x10

            # 初始化文件信息
            file_info = WINTRUST_FILE_INFO()
            file_info.cbStruct = ctypes.sizeof(WINTRUST_FILE_INFO)
            file_info.pcwszFilePath = exe_path
            file_info.hFile = None
            file_info.pgKnownSubject = None

            # 初始化信任数据
            wt_data = WINTRUST_DATA()
            wt_data.cbStruct = ctypes.sizeof(WINTRUST_DATA)
            wt_data.dwUIChoice = WTD_UI_NONE
            wt_data.fdwRevocationChecks = WTD_REVOKE_NONE
            wt_data.dwUnionChoice = WTD_CHOICE_FILE
            wt_data.pFile = ctypes.pointer(file_info)
            wt_data.dwStateAction = WTD_STATEACTION_VERIFY
            wt_data.hWVTStateData = None
            wt_data.pwszURLReference = None
            wt_data.dwProvFlags = WTD_REVOCATION_CHECK_NONE
            wt_data.dwUIContext = 0

            # 调用 WinVerifyTrust
            wintrust = ctypes.windll.wintrust
            # WINTRUST_ACTION_GENERIC_VERIFY_V2 = {00AAC56B-CD44-11D0-8CC2-00C04FC295EE}
            # 早期这里写成了 {AAC56B14-CD14-4C17-B37A-1E6F3CFF0B76}（错位的 GUID），
            # WinVerifyTrust 对任何文件都返回 TRUST_E_SUBJECT_FORM_UNKNOWN，于是
            # “签名校验”实际上从未生效过（旧代码的 else 分支又把它当成通过）。
            action_guid = GUID(
                0x00AAC56B,
                0xCD44,
                0x11D0,
                (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
            )
            result = wintrust.WinVerifyTrust(None, ctypes.byref(action_guid), ctypes.byref(wt_data))
            # WinVerifyTrust 返回 HRESULT，ctypes 会把它解释为有符号 32 位整数，
            # 而错误码（0x800B0100 等）是无符号形式。不归一化会导致所有
            # 错误码比较恒为假，从而把"未签名/被篡改"误判为通过。
            result &= 0xFFFFFFFF

            # 关闭状态句柄
            wt_data.dwStateAction = WTD_STATEACTION_CLOSE
            wintrust.WinVerifyTrust(None, ctypes.byref(action_guid), ctypes.byref(wt_data))

            if result == 0:
                # 签名有效，进一步验证签名者身份
                signer_ok, signer_msg = _verify_signer_identity(exe_path)
                if signer_ok:
                    return True, f"PE 文件格式有效，数字签名验证通过（{signer_msg}）", False, False
                # 签名有效但签发者不是官方：可能是被替换过的二进制。
                # strict 模式下直接判失败；非 strict 模式只告警，交由上层结合
                # SHA256/官方哈希判断（避免误杀自签名的合法转发包）。
                msg = f"数字签名有效但签名者非官方（{signer_msg}）"
                if strict:
                    return False, msg, False, False
                return True, msg + "，已放行（非严格模式）", False, True

            # ---- 完整性类错误：文件被改过，任何模式都必须失败 ----
            # 注意常量取值：TRUST_E_* 多数是 0x80096xxx，CERT_E_* 才是 0x800Bxxxx。
            # 早期把 0x800B0109（其实是 CERT_E_UNTRUSTEDROOT）当成 TRUST_E_BAD_DIGEST，
            # 真正的篡改码 0x80096010 落入"未知"分支，于是篡改在默认模式下被放行。
            if result in _INTEGRITY_ERRORS:
                return False, f"PE 文件{_INTEGRITY_ERRORS[result]}", True, False

            # ---- 信任/身份类错误：可能是合法的自签名或过期证书 ----
            # strict 模式判失败；默认模式告警放行（最终仍由 SHA256/TOFU 兜底）。
            msg = _TRUST_ERRORS.get(result, f"数字签名验证未通过（WinVerifyTrust 返回 0x{result:08X}）")
            if strict:
                return False, msg, False, False
            return True, msg + "，已放行（非严格模式）", False, True

        except ImportError:
            return False, "无法加载 WinTrust 接口，未能验证数字签名", False, False
        except Exception as e:
            safe_log_exception("verify", f"verify_pe_signature 忽略异常: {e}", "warning")
            return False, f"数字签名验证异常: {str(e)}", False, False

    except Exception as e:
        safe_log_exception("verify", f"verify_pe_signature 忽略异常: {e}", "warning")
        return False, f"PE 文件校验失败: {str(e)}", False, False


def verify_pe_signature(exe_path: str, strict: bool = False) -> tuple[bool, str]:
    """校验 Windows PE 文件格式及数字签名状态，返回 (ok, message)。"""
    ok, msg, _critical, _advisory = _pe_signature_verdict(exe_path, strict=strict)
    return ok, msg


def verify_pe_signature_detail(exe_path: str, strict: bool = False) -> tuple[bool, str, bool]:
    """在 (ok, message) 之外给出 integrity_violation（篡改/伪造证书）。"""
    ok, msg, critical, _advisory = _pe_signature_verdict(exe_path, strict=strict)
    return ok, msg, critical


def verify_download_full(
    zip_path: str,
    version: str | None = None,
    expected_hash: str | None = None,
    check_pe: bool = True,
    require_signature: bool | None = None,
) -> dict[str, Any]:
    """对下载的服务器文件进行完整的多重校验。

    Args:
        zip_path: ZIP 文件路径
        version: 服务器版本号
        expected_hash: 预期的 SHA256 哈希值
        check_pe: 是否校验解压后的 PE/ELF 文件
        require_signature: 是否要求**强签名**（未签名/签名者非官方即判失败）。
            默认读环境变量 ``MBES_REQUIRE_SIGNATURE``（未设置时为 False）。

            为什么默认 False：Mojang 发行的 ``bedrock_server.exe`` 是否带
            嵌入式 Authenticode 签名无法离线确认，而签名子系统在本轮修复前
            是坏的（GUID 写错，对任何文件都返回"无法解析"）。把"未签名"当
            硬失败会让官方源安装 100% 中断。注意：**篡改检测不受此开关影响**，
            摘要不匹配永远判失败。

    Returns:
        dict: 校验结果，包含各项校验的详细信息
    """
    if require_signature is None:
        require_signature = _env_flag("MBES_REQUIRE_SIGNATURE")
    result = {
        "ok": True,
        "file": zip_path,
        "version": version,
        "checks": {},
        "errors": [],
        "warnings": [],
    }

    # 1. 文件大小校验
    size_ok, size_msg, file_size = verify_file_size(zip_path)
    result["checks"]["file_size"] = {
        "ok": size_ok,
        "message": size_msg,
        "size_bytes": file_size,
        "size_mb": round(file_size / (1024 * 1024), 2),
    }
    if not size_ok:
        result["ok"] = False
        result["errors"].append(size_msg)

    # 2. ZIP 完整性校验
    zip_ok, zip_msg = verify_zip_integrity(zip_path)
    result["checks"]["zip_integrity"] = {"ok": zip_ok, "message": zip_msg}
    if not zip_ok:
        result["ok"] = False
        result["errors"].append(zip_msg)

    # 3. SHA256 哈希校验（首次信任机制：首次记录，后续对比）
    hash_ok, hash_msg, actual_hash = verify_sha256(zip_path, expected_hash, version)
    result["checks"]["sha256"] = {
        "ok": hash_ok,
        "message": hash_msg,
        "actual_hash": actual_hash,
        "expected_hash": expected_hash or get_known_hash(version) or "",
    }
    if not hash_ok:
        result["ok"] = False
        result["errors"].append(hash_msg)

    # 4. 解压后校验平台对应的服务端可执行文件
    if check_pe and zip_ok:
        try:
            import shutil
            import tempfile

            temp_dir = tempfile.mkdtemp(prefix="bds_verify_")
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    # 查找平台对应的服务端可执行文件（Windows: bedrock_server.exe / Linux: bedrock_server）
                    exe_names = [n for n in zf.namelist() if n.lower().endswith(SERVER_EXE.lower())]
                    if exe_names:
                        exe_name = exe_names[0]
                        zf.extract(exe_name, temp_dir)
                        exe_path = os.path.join(temp_dir, exe_name)
                        integrity_violation = False
                        advisory = False
                        if IS_WINDOWS:
                            # strict 只影响"未签名/证书不受信"这类身份问题；
                            # 篡改（摘要不匹配）无论 strict 与否都判失败。
                            exe_ok, exe_msg, integrity_violation, advisory = _pe_signature_verdict(
                                exe_path, strict=require_signature
                            )
                            result["checks"]["pe_signature"] = {"ok": exe_ok, "message": exe_msg}
                        else:
                            exe_ok, exe_msg = verify_elf_executable(exe_path)
                            result["checks"]["executable"] = {"ok": exe_ok, "message": exe_msg}
                        if integrity_violation:
                            # 完整性违规（摘要不匹配/证书伪造）与 require_signature 无关，
                            # 必须无条件中止安装。早期这里和其它失败一样被
                            # require_signature=False 降级成 warning，等于"检测到篡改却照装"。
                            result["ok"] = False
                            result["errors"].append(exe_msg)
                        elif advisory:
                            # 非严格模式下"未签名/签名者非官方"会放行，但仍需留痕。
                            # 用结构化标志判断，不再靠 "已放行" 这段文案匹配 ——
                            # 改一句提示语就会改变安全行为。
                            result["warnings"].append(exe_msg)
                        elif not exe_ok:
                            # 签名/格式不可信：官方源应中止安装，自定义地址降级为告警
                            if require_signature:
                                result["ok"] = False
                                result["errors"].append(exe_msg)
                            else:
                                result["warnings"].append(exe_msg)
                    else:
                        result["checks"]["pe_signature"] = {
                            "ok": False,
                            "message": f"ZIP 中未找到 {SERVER_EXE}",
                        }
                        result["ok"] = False
                        result["errors"].append(f"ZIP 中未找到 {SERVER_EXE}")
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as e:
            safe_log_exception("verify", f"verify_download_full 忽略异常: {e}", "warning")
            result["checks"]["pe_signature"] = {"ok": False, "message": f"可执行文件校验异常: {str(e)}"}
            result["warnings"].append(f"可执行文件校验异常: {str(e)}")

    return result


def verify_elf_executable(exe_path: str) -> tuple[bool, str]:
    """校验 Linux 平台的服务端可执行文件是否为有效的 ELF 二进制。

    官方 Linux 版 bedrock_server 是 ELF 可执行文件（无数字签名概念），
    此处检查 ELF 魔数与文件头类型，替代 Windows 的 PE 签名校验。

    注意：不再检查 ``os.access(X_OK)``。zip 解压不会保留权限位，
    在临时目录里校验可执行位会恒为失败；真实安装时由 install 流程
    统一 ``chmod 0700``。

    Args:
        exe_path: 可执行文件路径

    Returns:
        tuple: (ok, message)
    """
    if not os.path.isfile(exe_path):
        return False, "文件不存在"
    try:
        with open(exe_path, "rb") as f:
            header = f.read(18)
        if header[:4] != b"\x7fELF":
            return False, f"不是有效的 ELF 可执行文件（{PLATFORM_TAG} 平台）"
        if len(header) < 18:
            return False, "ELF 文件头不完整"
        ei_class = header[4]  # 1=32 位, 2=64 位
        ei_data = header[5]  # 1=小端, 2=大端
        if ei_class not in (1, 2):
            return False, f"ELF 位宽标识非法（{ei_class}）"
        if ei_data not in (1, 2):
            return False, f"ELF 字节序标识非法（{ei_data}）"
        # e_type 位于偏移 16，2 或 3 表示可执行/动态库可执行
        e_type = int.from_bytes(header[16:18], "little" if ei_data == 1 else "big")
        if e_type not in (2, 3):
            return False, f"ELF 文件类型不是可执行文件（e_type={e_type}）"
        return True, "ELF 可执行文件格式有效"
    except (PermissionError, OSError) as e:
        return False, f"ELF 校验失败: {str(e)}"
