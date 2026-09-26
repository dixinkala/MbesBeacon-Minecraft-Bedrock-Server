"""
软件更新检查模块：检查 MbesBeacon 本身的更新（与服务器更新区分开）。

实现说明：
- 主路径使用 GitHub Releases 网页（/releases/latest 重定向 + expanded_assets 页面），
  不消耗 GitHub API 匿名配额（60 次/小时/IP），避免 "rate limit exceeded"。
- 后备路径使用 GitHub API（字段更丰富），仅在网页解析失败时回退。
- 检查结果本地缓存 1 小时，避免频繁请求。
"""

import html as html_lib
import json
import os
import re
import time
import urllib.error
import urllib.request

from .app_logger import get_app_logger
from .constants import APP_DATA_DIR, APP_VERSION

# GitHub 仓库信息
GITHUB_REPO = "dixinkala/MbesBeacon-Minecraft-Bedrock-Server"
GITHUB_RELEASES_LATEST = f"https://github.com/{GITHUB_REPO}/releases/latest"
GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# 缓存文件和 TTL
_APP_UPDATE_CACHE_FILE = os.path.join(APP_DATA_DIR, "cache", "app_update_cache.json")
_APP_UPDATE_CACHE_TTL = 3600  # 缓存 1 小时，避免频繁请求 GitHub

# 全局缓存
_app_update_cache = None
_app_update_cache_time = 0

# HTML 解析正则
_TAG_URL_RE = re.compile(r"/releases/tag/([^/?#]+)")
_ASSET_HREF_RE = re.compile(r'href="([^"]*/releases/download/[^"]+)"')
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_RELATIVE_TIME_RE = re.compile(r"<relative-time[^>]*datetime=\"([^\"]+)\"")
_MARKDOWN_BODY_RE = re.compile(r'<div[^>]*class="[^"]*markdown-body[^"]*"[^>]*>(.*?)</div>\s*</div>', re.S)
_USER_AGENT = "MbesBeacon-Update-Checker/1.0"


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


def _http_get(url, ignore_ssl=False, timeout=10):
    """
    GET 请求，返回 (最终 URL, 响应文本)。

    Raises:
        urllib.error.URLError: 网络错误 / HTTP 错误
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
    )
    ctx = None
    if ignore_ssl:
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        # resp.geturl() 返回重定向后的最终 URL（releases/latest -> releases/tag/xxx）
        final_url = resp.geturl()
        text = resp.read().decode("utf-8", errors="replace")
        return final_url, text


def _strip_tags(text):
    """去除 HTML 标签并反转义实体，返回纯文本。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _check_via_html(ignore_ssl=False, timeout=10):
    """
    通过 GitHub Releases 网页检查最新版本（不消耗 API 配额）。

    Returns:
        dict: 与 check_app_update 返回结构一致的字段子集
    """
    # 1. releases/latest -> 302 到具体 tag 页面，从最终 URL 提取版本号
    final_url, _ = _http_get(GITHUB_RELEASES_LATEST, ignore_ssl=ignore_ssl, timeout=timeout)
    m = _TAG_URL_RE.search(final_url)
    if not m:
        raise ValueError(f"无法从响应 URL 解析版本: {final_url}")
    tag_name = m.group(1)
    latest_version = tag_name.lstrip("vV")

    # 2. 抓取 tag 页面，提取标题 / 发布日期 / 发布说明
    _, page = _http_get(final_url, ignore_ssl=ignore_ssl, timeout=timeout)
    title = ""
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    if m:
        title = html_lib.unescape(m.group(1)).strip()
        # 去掉 GitHub 页面前缀 "Release " 与后缀 " · 用户名/仓库名"
        if title.startswith("Release "):
            title = title[len("Release ") :]
        idx = title.find(" · ")
        if idx != -1:
            title = title[:idx]
    published_at = ""
    m = _RELATIVE_TIME_RE.search(page)
    if m:
        published_at = m.group(1).replace("Z", "+00:00")
    release_notes = ""
    m = _MARKDOWN_BODY_RE.search(page)
    if m:
        release_notes = _strip_tags(m.group(1))[:2000]

    # 3. 抓取 expanded_assets 页面，提取 EXE 下载链接（无需登录、无 API 配额）
    assets_url = f"https://github.com/{GITHUB_REPO}/releases/expanded_assets/{tag_name}"
    try:
        _, assets_html = _http_get(assets_url, ignore_ssl=ignore_ssl, timeout=timeout)
        exe_url = ""
        for href in _ASSET_HREF_RE.findall(assets_html):
            if href.lower().endswith(".exe"):
                exe_url = f"https://github.com{href}" if href.startswith("/") else href
                break
    except Exception as e:
        get_app_logger().debug(f"获取 Release 附件列表失败: {e}")
        exe_url = ""

    return {
        "tag_name": tag_name,
        "latest_version": latest_version,
        "latest_title": title,
        "release_notes": release_notes,
        "release_url": final_url,
        "published_at": published_at,
        "download_url": exe_url,
    }


def _check_via_api(ignore_ssl=False, timeout=10):
    """
    通过 GitHub API 检查最新版本（后备路径，字段更丰富）。
    可能受匿名配额限制（60 次/小时/IP）。
    """
    req = urllib.request.Request(
        GITHUB_API_LATEST,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github.v3+json",
        },
    )
    ctx = None
    if ignore_ssl:
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    tag_name = data.get("tag_name", "")
    latest_version = tag_name.lstrip("vV") if tag_name else ""
    exe_url = ""
    for asset in data.get("assets", []):
        name = asset.get("name", "").lower()
        if name.endswith(".exe"):
            exe_url = asset.get("browser_download_url", "")
            break
    return {
        "tag_name": tag_name,
        "latest_version": latest_version,
        "latest_title": data.get("name", ""),
        "release_notes": data.get("body", ""),
        "release_url": data.get("html_url", ""),
        "published_at": data.get("published_at", ""),
        "download_url": exe_url,
    }


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

    errors = []

    # 主路径：GitHub 网页（无 API 配额限制，避免 rate limit exceeded）
    try:
        info = _check_via_html(ignore_ssl=ignore_ssl, timeout=timeout)
        result.update(
            {
                "latest_version": info["latest_version"],
                "latest_title": info["latest_title"],
                "release_notes": info["release_notes"],
                "release_url": info["release_url"],
                "published_at": info["published_at"],
                "download_url": info["download_url"] or info["release_url"],
            }
        )
        result["has_update"] = _is_newer(info["latest_version"], current_version)
        result["ok"] = True
        get_app_logger().info(
            f"软件更新检查完成(网页): 当前={current_version}, 最新={info['latest_version']}, "
            f"有更新={result['has_update']}"
        )
        _save_cache(result)
        return result
    except urllib.error.HTTPError as e:
        errors.append(f"HTTP {e.code}: {e.reason}")
        get_app_logger().warning(f"软件更新检查(网页) HTTP 错误: {e.code} {e.reason}")
    except urllib.error.URLError as e:
        errors.append(str(e.reason))
        get_app_logger().warning(f"软件更新检查(网页)网络错误: {e.reason}")
    except Exception as e:
        errors.append(str(e))
        get_app_logger().warning(f"软件更新检查(网页)失败: {e}")

    # 后备路径：GitHub API
    try:
        info = _check_via_api(ignore_ssl=ignore_ssl, timeout=timeout)
        result.update(
            {
                "latest_version": info["latest_version"],
                "latest_title": info["latest_title"],
                "release_notes": info["release_notes"],
                "release_url": info["release_url"],
                "published_at": info["published_at"],
                "download_url": info["download_url"] or info["release_url"],
            }
        )
        result["has_update"] = _is_newer(info["latest_version"], current_version)
        result["ok"] = True
        get_app_logger().info(
            f"软件更新检查完成(API): 当前={current_version}, 最新={info['latest_version']}, "
            f"有更新={result['has_update']}"
        )
        _save_cache(result)
        return result
    except urllib.error.HTTPError as e:
        if e.code == 403:
            errors.append("更新服务请求过于频繁（API 限流），请稍后再试")
        else:
            errors.append(f"HTTP {e.code}: {e.reason}")
        get_app_logger().warning(f"软件更新检查(API) HTTP 错误: {e.code} {e.reason}")
    except urllib.error.URLError as e:
        errors.append(str(e.reason))
        get_app_logger().warning(f"软件更新检查(API)网络错误: {e.reason}")
    except json.JSONDecodeError as e:
        errors.append(f"解析响应失败: {e}")
        get_app_logger().warning(f"软件更新检查(API)解析错误: {e}")
    except Exception as e:
        errors.append(str(e))
        get_app_logger().warning(f"软件更新检查(API)异常: {e}")

    result["error"] = "；".join(errors) if errors else "更新检查失败"
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
