"""
下载完整性校验模块：对下载的服务器文件进行多重校验，
包括 ZIP 完整性、SHA256 哈希、文件大小合理性、PE 签名校验。
"""

import hashlib
from .app_logger import get_app_logger, safe_log_exception
import json
import os
import zipfile
from typing import Any

# BDS 服务端文件大小合理范围（字节）
# 注意：BDS 版本持续更新，文件大小可能增长，上限设置较宽松以避免误判
MIN_BDS_SIZE = 30 * 1024 * 1024  # 30MB（历史版本可能较小）
MAX_BDS_SIZE = 500 * 1024 * 1024  # 500MB（预留未来版本增长空间）

# 已知的官方版本 SHA256 哈希（可从官方 API 获取，这里预留接口）
_KNOWN_HASHES: dict[str, str] = {}

# Bedrock-OSS/BDS-Versions 仓库版本信息 URL（包含官方下载链接和哈希）
BDS_VERSIONS_API = "https://raw.githubusercontent.com/Bedrock-OSS/BDS-Versions/main/versions.json"

# 官方哈希获取状态（避免重复请求）
_official_hashes_loaded = False


def fetch_official_hashes(timeout: int = 10) -> dict[str, str]:
    """从 Bedrock-OSS/BDS-Versions 仓库获取官方版本的 SHA256 哈希。

    该仓库维护了 BDS 各版本的下载链接和文件哈希，可用于验证下载文件的完整性。

    Args:
        timeout: 请求超时时间（秒）

    Returns:
        dict: 版本号 -> SHA256 哈希的字典
    """
    import urllib.request
    import urllib.error

    hashes = {}
    try:
        req = urllib.request.Request(
            BDS_VERSIONS_API,
            headers={"User-Agent": "Mozilla/5.0 MbesBeacon"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))

        # 解析版本数据（不同版本的 JSON 结构可能略有不同）
        if isinstance(data, dict):
            # 结构1: {"versions": {"1.21.0.03": {"win": {"sha256": "...", "url": "..."}}}}
            if "versions" in data and isinstance(data["versions"], dict):
                for version, platforms in data["versions"].items():
                    if isinstance(platforms, dict):
                        win_info = platforms.get("win") or platforms.get("windows") or {}
                        if isinstance(win_info, dict) and "sha256" in win_info:
                            hashes[version] = win_info["sha256"]
            # 结构2: {"1.21.0.03": {"win": {"sha256": "..."}}}
            else:
                for version, platforms in data.items():
                    if isinstance(platforms, dict):
                        win_info = platforms.get("win") or platforms.get("windows") or {}
                        if isinstance(win_info, dict) and "sha256" in win_info:
                            hashes[version] = win_info["sha256"]
        elif isinstance(data, list):
            # 结构3: [{"version": "1.21.0.03", "sha256": "...", "platform": "win"}]
            for item in data:
                if isinstance(item, dict):
                    version = item.get("version") or item.get("name")
                    sha256 = item.get("sha256") or item.get("hash")
                    platform = item.get("platform", "win")
                    if version and sha256 and platform in ("win", "windows", "win64"):
                        hashes[version] = sha256

    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        # 网络请求失败或解析失败，静默返回空字典
        pass
    except Exception as e:
        # 其他未知错误，记录日志后返回空字典
        safe_log_exception("verify.py", f"获取官方哈希失败: {e}", "warning")

    return hashes


def load_official_hashes(force: bool = False) -> int:
    """加载官方版本哈希到内存缓存中。

    Args:
        force: 是否强制重新加载（即使已加载过）

    Returns:
        int: 成功加载的哈希数量
    """
    global _official_hashes_loaded

    if _official_hashes_loaded and not force:
        return len(_KNOWN_HASHES)

    official_hashes = fetch_official_hashes()
    for version, sha256_hash in official_hashes.items():
        if version not in _KNOWN_HASHES:
            _KNOWN_HASHES[version] = sha256_hash

    _official_hashes_loaded = True
    return len(official_hashes)

# 本地哈希缓存文件路径（首次下载时记录，后续下载对比）
_HASH_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".bedrock_server_hashes.json")


def _load_hash_cache() -> dict[str, str]:
    """加载本地哈希缓存。"""
    try:
        if os.path.isfile(_HASH_CACHE_FILE):
            with open(_HASH_CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        safe_log_exception("verify", f"操作失败: {e}", "warning")
    return {}


def _save_hash_cache(cache: dict[str, str]) -> None:
    """保存本地哈希缓存。"""
    try:
        os.makedirs(os.path.dirname(_HASH_CACHE_FILE), exist_ok=True)
        with open(_HASH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
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


def _verify_signer_identity(exe_path: str) -> tuple[bool, str]:
    """验证 PE 文件的数字签名者身份是否为 Microsoft/Mojang。

    使用 CryptQueryObject 获取签名者证书，然后检查证书主题。
    返回 (ok, message)：ok=True 表示签名者为官方，message 为签名者名称。
    """
    try:
        import ctypes
        from ctypes import wintypes

        # 定义 CryptQueryObject 所需的常量和结构体
        CERT_QUERY_OBJECT_FILE = 0x00000001
        CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED = 0x00000010
        CERT_QUERY_FORMAT_FLAG_BINARY = 0x00000002
        CERT_NAME_SIMPLE_DISPLAY_TYPE = 4

        class CERT_INFO(ctypes.Structure):
            _fields_ = [
                ("dwVersion", wintypes.DWORD),
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
                ("dwCertEncodingType", wintypes.DWORD),
                ("pbCertEncoded", ctypes.POINTER(ctypes.c_byte)),
                ("cbCertEncoded", wintypes.DWORD),
                ("pCertInfo", ctypes.POINTER(CERT_INFO)),
                ("hCertStore", wintypes.HANDLE),
            ]

        crypt32 = ctypes.windll.crypt32

        # 调用 CryptQueryObject 获取证书上下文
        h_store = wintypes.HANDLE()
        h_msg = wintypes.HANDLE()
        pdw_encoding = wintypes.DWORD()
        pdw_content_type = wintypes.DWORD()
        pdw_format_type = wintypes.DWORD()
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

        if not success or not pCertContext:
            return False, "无法获取签名证书"

        try:
            # 获取签名者名称（Subject）
            name_len = crypt32.CertGetNameStringW(
                pCertContext,
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
                pCertContext,
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
            # 释放证书上下文
            if pCertContext:
                crypt32.CertFreeCertificateContext(pCertContext)
    except ImportError:
        return False, "非 Windows 平台，无法验证签名者身份"
    except Exception as e:
        return False, f"签名者验证失败: {str(e)}"


def verify_pe_signature(exe_path: str) -> tuple[bool, str]:
    """校验 Windows PE 文件格式及数字签名状态。

    本函数执行以下检查：
    1. PE 文件格式验证（MZ 头 + PE 签名 + 机器类型）
    2. 数字签名存在性检查（通过 WinVerifyTrust API）

    注意：本函数不验证签名者是否为 Mojang/Microsoft，仅检查签名是否存在且有效。
    对于官方 Bedrock 服务端，bedrock_server.exe 通常带有 Microsoft 数字签名。

    Args:
        exe_path: EXE 文件路径

    Returns:
        tuple: (ok, message)
            ok=True 表示文件格式有效，message 中会说明签名状态
            ok=False 表示文件格式无效或存在严重问题
    """
    if not os.path.isfile(exe_path):
        return False, "文件不存在"
    try:
        import struct

        # 1. 检查 MZ 头（DOS 头）
        with open(exe_path, "rb") as f:
            mz_header = f.read(2)
            if mz_header != b"MZ":
                return False, "不是有效的 Windows PE 文件（缺少 MZ 头）"
            # 读取 e_lfanew（PE 头偏移）
            f.seek(0x3C)
            pe_offset = struct.unpack("<I", f.read(4))[0]
            # 检查 PE 签名
            f.seek(pe_offset)
            pe_signature = f.read(4)
            if pe_signature != b"PE\x00\x00":
                return False, "不是有效的 Windows PE 文件（缺少 PE 签名）"
            # 读取机器类型
            machine = struct.unpack("<H", f.read(2))[0]
            if machine not in (0x8664, 0x14C):  # x64 或 x86
                return False, f"不支持的 CPU 架构（机器类型: 0x{machine:04X}）"

        # 2. 尝试使用 WinVerifyTrust API 验证数字签名（Windows 专用）
        try:
            import ctypes
            from ctypes import wintypes

            # 定义 WinVerifyTrust 所需的结构体
            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            class WINTRUST_FILE_INFO(ctypes.Structure):
                _fields_ = [
                    ("cbStruct", wintypes.DWORD),
                    ("pcwszFilePath", wintypes.LPCWSTR),
                    ("hFile", wintypes.HANDLE),
                    ("pgKnownSubject", ctypes.POINTER(GUID)),
                ]

            class WINTRUST_DATA(ctypes.Structure):
                _fields_ = [
                    ("cbStruct", wintypes.DWORD),
                    ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p),
                    ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD),
                    ("dwUnionChoice", wintypes.DWORD),
                    ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)),
                    ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE),
                    ("pwszURLReference", wintypes.LPCWSTR),
                    ("dwProvFlags", wintypes.DWORD),
                    ("dwUIContext", wintypes.DWORD),
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
            action_guid = GUID(
                0xAAC56B14, 0xCD14, 0x4C17, (ctypes.c_ubyte * 8)(0xB3, 0x7A, 0x1E, 0x6F, 0x3C, 0xFF, 0x0B, 0x76)
            )
            result = wintrust.WinVerifyTrust(None, ctypes.byref(action_guid), ctypes.byref(wt_data))

            # 关闭状态句柄
            wt_data.dwStateAction = WTD_STATEACTION_CLOSE
            wintrust.WinVerifyTrust(None, ctypes.byref(action_guid), ctypes.byref(wt_data))

            if result == 0:
                # 签名验证通过，进一步验证签名者身份
                signer_ok, signer_msg = _verify_signer_identity(exe_path)
                if signer_ok:
                    return True, f"PE 文件格式有效，数字签名验证通过（{signer_msg}）"
                else:
                    return True, f"PE 文件格式有效，数字签名验证通过，但签名者非官方（{signer_msg}）"
            elif result == 0x800B0100:  # TRUST_E_NOSIGNATURE
                return True, "PE 文件格式有效，但文件未签名（无数字签名）"
            elif result == 0x800B0109:  # TRUST_E_BAD_DIGEST
                return False, "PE 文件数字签名无效（文件可能被篡改）"
            else:
                return True, f"PE 文件格式有效，签名验证结果: 0x{result:08X}"

        except ImportError:
            return True, "PE 文件格式有效（非 Windows 平台，无法验证数字签名）"
        except Exception as e:
            return True, f"PE 文件格式有效（签名验证跳过: {str(e)}）"

    except Exception as e:
        return False, f"PE 文件校验失败: {str(e)}"


def verify_download_full(
    zip_path: str, version: str | None = None, expected_hash: str | None = None, check_pe: bool = True
) -> dict[str, Any]:
    """对下载的服务器文件进行完整的多重校验。

    Args:
        zip_path: ZIP 文件路径
        version: 服务器版本号
        expected_hash: 预期的 SHA256 哈希值
        check_pe: 是否校验解压后的 PE 文件签名

    Returns:
        dict: 校验结果，包含各项校验的详细信息
    """
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

    # 4. 解压后校验 bedrock_server.exe
    if check_pe and zip_ok:
        try:
            import shutil
            import tempfile

            temp_dir = tempfile.mkdtemp(prefix="bds_verify_")
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    # 查找 bedrock_server.exe
                    exe_names = [n for n in zf.namelist() if n.lower().endswith("bedrock_server.exe")]
                    if exe_names:
                        exe_name = exe_names[0]
                        zf.extract(exe_name, temp_dir)
                        exe_path = os.path.join(temp_dir, exe_name)
                        pe_ok, pe_msg = verify_pe_signature(exe_path)
                        result["checks"]["pe_signature"] = {"ok": pe_ok, "message": pe_msg}
                        if not pe_ok:
                            result["warnings"].append(pe_msg)
                    else:
                        result["checks"]["pe_signature"] = {
                            "ok": False,
                            "message": "ZIP 中未找到 bedrock_server.exe",
                        }
                        result["ok"] = False
                        result["errors"].append("ZIP 中未找到 bedrock_server.exe")
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as e:
            result["checks"]["pe_signature"] = {"ok": False, "message": f"PE 校验异常: {str(e)}"}
            result["warnings"].append(f"PE 签名校验异常: {str(e)}")

    return result


def get_known_hashes() -> dict[str, str]:
    """获取所有已知版本的 SHA256 哈希。

    Returns:
        dict: 版本号到 SHA256 哈希的映射
    """
    return dict(_KNOWN_HASHES)


__all__ = [
    "calculate_sha256",
    "verify_file_size",
    "verify_zip_integrity",
    "verify_sha256",
    "verify_pe_signature",
    "verify_download_full",
    "register_known_hash",
    "get_known_hashes",
    "MIN_BDS_SIZE",
    "MAX_BDS_SIZE",
]
