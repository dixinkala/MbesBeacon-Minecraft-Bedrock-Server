"""
GET 路由处理方法 mixin。
包含所有 GET 请求的路由处理方法，从 handler.py 的 do_GET 中迁移而来。
"""
import contextlib
import json
import os
import time
from urllib.parse import parse_qs, urlparse

from ..route_decorator import register_get_route


class GetRoutesMixin:
    """GET 路由处理方法 mixin。"""

    @register_get_route("/")
    def _get_index(self, q):
        from ...state import API_TOKEN
        from ..handler import get_index_html

        html = get_index_html().replace("__API_TOKEN__", API_TOKEN)
        self._send(200, html, "text/html; charset=utf-8")

    @register_get_route("/api/health")
    def _get_health(self, q):
        from ...server import server_running
        from ...state import APP_MARKER, APP_VERSION, app_start_time
        from ...utils import installed

        mem_mb = 0
        cpu_percent = 0
        try:
            import psutil as _psutil

            proc = _psutil.Process(os.getpid())
            mem_mb = proc.memory_info().rss / (1024 * 1024)
            cpu_percent = proc.cpu_percent(interval=0.1)
        except ImportError:
            pass
        except Exception:
            pass
        uptime = time.time() - app_start_time
        from ...state import settings

        self._json(
            {
                "ok": True,
                "status": "healthy",
                "app": APP_MARKER,
                "version": APP_VERSION,
                "uptime_seconds": round(uptime, 2),
                "uptime_human": "%d:%02d:%02d"
                % (int(uptime // 3600), int((uptime % 3600) // 60), int(uptime % 60)),
                "memory_mb": round(mem_mb, 2),
                "cpu_percent": round(cpu_percent, 2),
                "server_running": server_running(),
                "server_dir": settings.get("server_dir", ""),
                "installed": installed(),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    @register_get_route("/api/status")
    def _get_status(self, q):
        from ...install import detect_server_version
        from ...server import server_running
        from ...state import APP_MARKER, APP_TITLE, APP_VERSION, install_state, settings
        from ...utils import detect_servers, installed, resolve_server_dir, save_settings, sync_log_file

        d = resolve_server_dir()
        sync_log_file()
        inst_ver = settings.get("installed_version", "")
        if not inst_ver and d and installed():
            inst_ver = detect_server_version(d)
            if inst_ver:
                settings["installed_version"] = inst_ver
                save_settings()
        self._json(
            {
                "app": APP_MARKER,
                "title": APP_TITLE,
                "version": APP_VERSION,
                "server_dir": d,
                "installed": installed(),
                "installed_version": inst_ver,
                "server_running": server_running(),
                "install": install_state.snapshot(),
                "latest": settings.get("_latest"),
                "detected_servers": detect_servers(),
            }
        )

    @register_get_route("/api/servers/detect")
    def _get_servers_detect(self, q):
        from ...utils import detect_servers

        servers = detect_servers()
        self._json({"ok": True, "servers": servers, "count": len(servers)})

    @register_get_route("/api/browse-dir")
    def _get_browse_dir(self, q):
        from ...utils import browse_directory

        initial = q.get("initial", [""])[0]
        selected = browse_directory(initial)
        if selected:
            self._json({"ok": True, "dir": selected})
        else:
            self._json({"ok": False, "error": "用户取消选择"})

    @register_get_route("/api/theme")
    def _get_theme(self, q):
        from ...state import settings
        from ...utils import load_theme

        self._json({"ok": True, "theme": settings.get("theme") or load_theme()})

    @register_get_route("/api/latest")
    def _get_latest(self, q):
        from ...install import get_latest_server_info
        from ...state import settings

        try:
            info = get_latest_server_info(ignore_ssl=settings.get("ignore_ssl", False))
            settings["_latest"] = info
            self._json({"ok": True, "info": info})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

    @register_get_route("/api/versions")
    def _get_versions(self, q):
        from ...install import FALLBACK_VERSIONS, get_cached_versions

        try:
            include_preview = q.get("preview", ["0"])[0] in ("1", "true", "yes")
            versions, from_cache = get_cached_versions(include_preview=include_preview)
            self._json(
                {
                    "ok": True,
                    "versions": versions,
                    "count": len(versions),
                    "preview": include_preview,
                    "from_cache": from_cache,
                }
            )
        except Exception as e:
            self._json({"ok": False, "error": str(e), "versions": FALLBACK_VERSIONS})

    @register_get_route("/api/sources")
    def _get_sources(self, q):
        from ...install import DOWNLOAD_SOURCES

        self._json(
            {
                "ok": True,
                "sources": [
                    {"index": i, "name": s["name"], "type": s["type"]} for i, s in enumerate(DOWNLOAD_SOURCES)
                ],
            }
        )

    @register_get_route("/api/progress")
    def _get_progress(self, q):
        from ...state import install_state

        self._json(install_state.snapshot())

    @register_get_route("/api/console")
    def _get_console(self, q):
        from ...server import server_running
        from ...state import console

        since = int(q.get("since", ["0"])[0])
        text, count = console.read_since(since)
        self._json({"lines": text, "count": count, "running": server_running()})

    @register_get_route("/api/console/export")
    def _get_console_export(self, q):
        from ...state import console

        text, _ = console.read_since(0)
        filename = "bedrock_console_{}.txt".format(time.strftime("%Y%m%d_%H%M%S"))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename={filename}")
        self.send_header("Content-Length", str(len(text.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    @register_get_route("/api/performance")
    def _get_performance(self, q):
        from ...performance import get_server_performance_full
        from ...server import get_server_proc
        from ...state import console

        p = get_server_proc()
        perf = get_server_performance_full(server_proc=p, console=console)
        self._json(perf)

    @register_get_route("/api/backups")
    def _get_backups(self, q):
        from ...backup import get_backup_root, list_backups
        from ...utils import resolve_server_dir

        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        backups = list_backups(d)
        self._json({"ok": True, "backups": backups, "count": len(backups), "backup_root": get_backup_root(d)})

    @register_get_route("/api/laninfo")
    def _get_laninfo(self, q):
        """获取服务器局域网连接信息（IP + 端口）。"""
        from ...config import load_properties, properties_to_dict
        from ...utils import get_lan_ip, props_path

        lan_ip = get_lan_ip()
        port = 19132
        p = props_path()
        if p and os.path.exists(p):
            with contextlib.suppress(Exception):
                items = load_properties(p)
                data = properties_to_dict(items)
                port = int(data.get("server-port", "19132"))
        self._json(
            {
                "ok": True,
                "lan_ip": lan_ip,
                "port": port,
                "address": "%s:%d" % (lan_ip, port),
            }
        )

    @register_get_route("/api/config")
    def _get_config(self, q):
        from ...config import PROP_META, load_properties, properties_to_dict
        from ...utils import get_lan_ip, props_path, resolve_server_dir

        p = props_path()
        if not p or not os.path.exists(p):
            self._json({"ok": False, "error": "尚未安装服务器", "path": p})
            return
        items = load_properties(p)
        data = properties_to_dict(items)
        d = resolve_server_dir()
        port = 19132
        if d:
            with contextlib.suppress(Exception):
                port = int(data.get("server-port", "19132"))
        lan_ip = get_lan_ip()
        self._json(
            {
                "ok": True,
                "path": p,
                "data": data,
                "meta": PROP_META,
                "lan_ip": lan_ip,
                "port": port,
                "address": "%s:%d" % (lan_ip, port),
            }
        )

    @register_get_route("/api/config/full")
    def _get_config_full(self, q):
        from ...config import FULL_PROP_META, load_properties, properties_to_dict
        from ...utils import props_path

        p = props_path()
        if not p or not os.path.exists(p):
            self._json({"ok": False, "error": "尚未安装服务器"})
            return
        items = load_properties(p)
        data = properties_to_dict(items)
        for key, _label, _typ, default in FULL_PROP_META:
            if key not in data:
                data[key] = str(default) if not isinstance(default, list) else default[0]
        self._json({"ok": True, "path": p, "data": data, "meta": FULL_PROP_META})

    @register_get_route("/api/listdir")
    def _get_listdir(self, q):
        from ...state import SERVER_EXE

        base = q.get("path", [""])[0]
        if not base:
            base = os.path.expanduser("~")
        if ".." in base.replace("\\", "/").split("/"):
            self._json({"ok": False, "error": "非法路径", "path": base, "parent": "", "dirs": [], "is_server": False})
            return
        base = os.path.abspath(base)
        _system_dirs = [
            "C:\\Windows",
            "C:\\Program Files",
            "C:\\Program Files (x86)",
            "C:\\ProgramData",
            "C:\\System Volume Information",
        ]
        base_lower = base.lower()
        for sd in _system_dirs:
            if base_lower.startswith(sd.lower()):
                self._json(
                    {
                        "ok": False,
                        "error": "无权访问系统目录",
                        "path": base,
                        "parent": "",
                        "dirs": [],
                        "is_server": False,
                    }
                )
                return
        _sensitive_user_dirs = (
            ".ssh",
            ".aws",
            ".gnupg",
            ".kube",
            "AppData\\Roaming\\Microsoft\\Credentials",
            "AppData\\Local\\Microsoft\\Credentials",
            "AppData\\Roaming\\Microsoft\\Crypto",
            "AppData\\Roaming\\Microsoft\\SystemCertificates",
        )
        home_lower = os.path.expanduser("~").lower()
        for sd in _sensitive_user_dirs:
            sensitive = os.path.normcase(os.path.join(home_lower, sd))
            if base_lower == sensitive or base_lower.startswith(sensitive + os.sep):
                self._json(
                    {
                        "ok": False,
                        "error": "无权访问敏感目录",
                        "path": base,
                        "parent": "",
                        "dirs": [],
                        "is_server": False,
                    }
                )
                return
        result = {
            "ok": True,
            "path": base,
            "parent": os.path.dirname(base) if base else "",
            "dirs": [],
            "is_server": False,
        }
        try:
            for name in sorted(os.listdir(base)):
                full = os.path.join(base, name)
                if os.path.isdir(full):
                    result["dirs"].append(name)
            result["is_server"] = os.path.isfile(os.path.join(base, SERVER_EXE))
            result["has_exe"] = result["is_server"]
        except Exception as e:
            result["ok"] = False
            result["error"] = str(e)
        self._json(result)

    @register_get_route("/api/players")
    def _get_players(self, q):
        from ...players import parse_online_players, read_permissions, send_command_capture
        from ...server import server_running
        from ...utils import resolve_server_dir

        d = resolve_server_dir()
        if not server_running():
            self._json({"ok": True, "running": False, "players": [], "permissions": read_permissions(d)})
            return
        text = send_command_capture("list", timeout=2.5)
        players = parse_online_players(text)
        self._json(
            {"ok": True, "running": True, "players": players, "permissions": read_permissions(d), "raw": text or ""}
        )

    @register_get_route("/api/players/banlist")
    def _get_players_banlist(self, q):
        from ...players import read_banlist
        from ...utils import resolve_server_dir

        d = resolve_server_dir()
        self._json({"ok": True, "banned": read_banlist(d)})

    @register_get_route("/api/players/allowlist")
    def _get_players_allowlist(self, q):
        from ...players import read_allowlist
        from ...utils import resolve_server_dir

        d = resolve_server_dir()
        self._json({"ok": True, "allowed": read_allowlist(d)})
