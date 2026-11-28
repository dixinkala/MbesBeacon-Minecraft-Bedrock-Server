"""BDS 版本清单来源链。

优先级（逐层兜底）：

1. **自建清单**：``dixinkala/mbesbeacon-bds-versions``（由 GitHub Actions 每小时更新）
2. **社区清单**：``Bedrock-OSS/BDS-Versions``（自建清单不可达时的备份数据源）
3. **内置快照**：``builtin_versions.BUILTIN_VERSIONS``（完全离线时使用，随版本同步更新）

自建仓库是主来源；社区仓库只作为兜底，程序不再"依赖"它。
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

from .app_logger import safe_log_exception
from .builtin_versions import BUILTIN_VERSIONS, BUILTIN_VERSIONS_SNAPSHOT
from .constants import FALLBACK_VERSIONS_JSON_URL, IS_WINDOWS, VERSIONS_JSON_URL

#: 依次尝试的清单地址（顺序即优先级）
MANIFEST_URLS = (VERSIONS_JSON_URL, FALLBACK_VERSIONS_JSON_URL)

_USER_AGENT = "Mozilla/5.0 MbesBeacon"


def platform_key() -> str:
    """当前平台在清单中的键名。"""
    return "windows" if IS_WINDOWS else "linux"


def fetch_manifest(ignore_ssl: bool = False, timeout: int = 20, urls: tuple = MANIFEST_URLS) -> tuple[dict, str]:
    """按顺序请求版本清单。

    Args:
        ignore_ssl: 网络异常时跳过 SSL 校验（用户可在界面上勾选）
        timeout: 单次请求超时（秒）
        urls: 覆盖默认来源链（测试用）

    Returns:
        tuple: ``(manifest, source_url)``；全部失败时返回 ``({}, "")``
    """
    ctx = ssl.create_default_context()
    if ignore_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            if isinstance(data, dict) and any(isinstance(data.get(p), dict) for p in ("windows", "linux")):
                return data, url
            safe_log_exception("version_sources", f"清单格式异常，跳过: {url}", "warning")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            safe_log_exception("version_sources", f"获取清单失败 {url}: {exc}", "warning")
        except Exception as exc:
            safe_log_exception("version_sources", f"获取清单异常 {url}: {exc}", "warning")
    return {}, ""


def builtin_manifest() -> dict:
    """内置快照的深拷贝（调用方可以安全修改）。"""
    return json.loads(json.dumps(BUILTIN_VERSIONS))


def builtin_snapshot_date() -> str:
    """内置快照的生成日期（YYYY-MM-DD）。"""
    return BUILTIN_VERSIONS_SNAPSHOT


def platform_info(manifest: dict) -> dict:
    """取出当前平台那一段，缺失时返回空字典。"""
    info = manifest.get(platform_key())
    return info if isinstance(info, dict) else {}


def manifest_stats(manifest: dict) -> dict:
    """清单统计信息（用于日志/界面展示）。"""
    stats = {}
    for platform in ("windows", "linux"):
        info = manifest.get(platform) or {}
        stats[platform] = {
            "stable": str(info.get("stable") or ""),
            "preview": str(info.get("preview") or ""),
            "versions": len(info.get("versions") or []),
            "preview_versions": len(info.get("preview_versions") or []),
        }
    return stats
