"""
软件更新检查模块：检查 MbesBeacon 本身的更新（与服务器更新区分开）。
从 GitHub API 获取最新 Release 信息，比较当前版本，提供下载链接。
"""

import json
import os
import time
import urllib.error
import urllib.request

from .app_logger import get_app_logger
from .constants import APP_VERSION, APP_DATA_DIR

# GitHub 仓库信息
GITHUB_REPO = "dixinkala/MbesBeacon-Bedrock-Server"
GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# 缓存文件和 TTL
_APP_UPDATE_CACHE_FILE = os.path.join(APP_DATA_DIR, "cache", "app_update_cache.json")
_APP_UPDATE_CACHE_TTL = 3600  # 缓存 1 小时，避免频繁请求 GitHub API

# 全局缓存
_app_update_cache = None
_app_update_cache_time = 0


def _parse_version(version_str):
    """
    解析版本字符串为元组，用于比较。
    支持 "v0.1.0"、"0.1.0"、"0.1.0.0" 等格式。
    """
    if not version_str:
        return (0, 0, 0)
    # 去掉前缀 v
    v = version_str.lstrip("vV")
    # 分割版本号
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    # 补齐到 3 位
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _is_newer(latest_version, current_version):
    """
    比较版本号，判断 latest 是否比 current 新。
    """
    latest = _parse_version(latest_version)
    current = _parse_version(current_version)
    return latest > current


def _load_cache():
    """从本地缓存文件加载更新检查结果。"""
    global _app_update_cache, _app_update_cache_time
    try:
        if os.path.isfile(_APP_UPDATE_CACHE_FILE):
            with open(_APP_UPDATE_CACHE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            _app_update_cache = data.get("result")
            _app_update_cache_time = data.get("timestamp", 0)
            return True
    except Exception as e:
        get_app_logger().debug(f"加载软件更新缓存失败: {e}")
    return False


def _save_cache(result):
    """保存更新检查结果到本地缓存。"""
    global _app_update_cache, _app_update_cache_time
    try:
        os.makedirs(os.path.dirname(_APP_UPDATE_CACHE_FILE), exist_ok=True)
        data = {
            "result": result,
            "timestamp": time.time(),
        }
        with open(_APP_UPDATE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _app_update_cache = result
        _app_update_cache_time = time.time()
    except Exception as e:
        get_app_logger().debug(f"保存软件更新缓存失败: {e}")


def check_app_update(force=False, ignore_ssl=False, timeout=10):
    """
    检查 MbesBeacon 软件更新。

    Args:
        force: 是否强制刷新（忽略缓存）
        ignore_ssl: 是否忽略 SSL 证书验证
        timeout: 请求超时时间（秒）

    Returns:
        dict: {
            "ok": bool,
            "has_update": bool,
            "current_version": str,
            "latest_version": str,
            "latest_title": str,
            "release_notes": str,
            "download_url": str,
            "release_url": str,
            "published_at": str,
            "error": str (仅失败时)
        }
    """
    global _app_update_cache, _app_update_cache_time

    # 检查缓存
    if not force:
        now = time.time()
        if _app_update_cache and (now - _app_update_cache_time) < _APP_UPDATE_CACHE_TTL:
            get_app_logger().debug("使用缓存的软件更新检查结果")
            return _app_update_cache
        # 尝试从文件加载缓存
        if _load_cache():
            now = time.time()
            if (now - _app_update_cache_time) < _APP_UPDATE_CACHE_TTL:
                return _app_update_cache

    current_version = APP_VERSION
    result = {
        "ok": False,
        "has_update": False,
        "current_version": current_version,
        "latest_version": "",
        "latest_title": "",
        "release_notes": "",
        "download_url": "",
        "release_url": "",
        "published_at": "",
    }

    try:
        # 构建请求
        req = urllib.request.Request(
            GITHUB_API_LATEST,
            headers={
                "User-Agent": "MbesBeacon-Update-Checker",
                "Accept": "application/vnd.github.v3+json",
            },
        )

        # SSL 上下文
        ctx = None
        if ignore_ssl:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        # 发送请求
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # 解析 Release 信息
        tag_name = data.get("tag_name", "")
        latest_version = tag_name.lstrip("vV") if tag_name else ""
        result["latest_version"] = latest_version
        result["latest_title"] = data.get("name", "")
        result["release_notes"] = data.get("body", "")
        result["release_url"] = data.get("html_url", "")
        result["published_at"] = data.get("published_at", "")

        # 查找 EXE 下载链接
        assets = data.get("assets", [])
        exe_asset = None
        for asset in assets:
            name = asset.get("name", "").lower()
            if name.endswith(".exe"):
                exe_asset = asset
                break
        if exe_asset:
            result["download_url"] = exe_asset.get("browser_download_url", "")
        else:
            # 如果没有 EXE 附件，使用 Release 页面
            result["download_url"] = result["release_url"]

        # 比较版本
        result["has_update"] = _is_newer(latest_version, current_version)
        result["ok"] = True

        get_app_logger().info(
            f"软件更新检查完成: 当前={current_version}, 最新={latest_version}, "
            f"有更新={result['has_update']}"
        )

        # 保存缓存
        _save_cache(result)

    except urllib.error.URLError as e:
        result["error"] = f"网络请求失败: {e.reason}"
        get_app_logger().warning(f"软件更新检查网络错误: {e}")
    except urllib.error.HTTPError as e:
        result["error"] = f"HTTP 错误: {e.code} {e.reason}"
        get_app_logger().warning(f"软件更新检查 HTTP 错误: {e.code} {e.reason}")
    except json.JSONDecodeError as e:
        result["error"] = f"解析响应失败: {e}"
        get_app_logger().warning(f"软件更新检查解析错误: {e}")
    except Exception as e:
        result["error"] = f"检查失败: {e}"
        get_app_logger().warning(f"软件更新检查异常: {e}")

    return result


def check_app_update_async(callback=None, force=False, ignore_ssl=False):
    """
    异步检查软件更新，不阻塞主线程。

    Args:
        callback: 检查完成后的回调函数，参数为 result dict
        force: 是否强制刷新
        ignore_ssl: 是否忽略 SSL 验证
    """
    import threading

    def _worker():
        try:
            result = check_app_update(force=force, ignore_ssl=ignore_ssl)
            if callback:
                callback(result)
        except Exception as e:
            get_app_logger().error(f"异步软件更新检查异常: {e}")
            if callback:
                callback({"ok": False, "error": str(e), "has_update": False})

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread
