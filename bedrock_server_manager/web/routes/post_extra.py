"""
额外 POST 路由处理方法 mixin。
包含从 handler.py 的 do_POST 中迁移的硬编码 POST 路由。
"""
import contextlib
import os
import threading

from ..route_decorator import register_post_route


class PostExtraRoutesMixin:
    """额外 POST 路由处理方法 mixin。"""

    @register_post_route("/api/latest")
    def _post_latest(self, data):
        from ...install import get_latest_server_info
        from ...state import settings

        try:
            info = get_latest_server_info(ignore_ssl=settings.get("ignore_ssl", False))
            settings["_latest"] = info
            self._json({"ok": True, "info": info})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

    @register_post_route("/api/servers/select")
    def _post_servers_select(self, data):
        from ...install import select_server_dir

        ok, err, info = select_server_dir(data.get("dir", ""))
        if not ok:
            self._json({"ok": False, "error": err})
            return
        self._json({"ok": True, **info})

    @register_post_route("/api/install")
    def _post_install(self, data):
        from ...install import do_install
        from ...security import validate_custom_url
        from ...state import install_state, settings

        if install_state.busy:
            self._json({"ok": False, "error": "已有安装任务正在进行"})
            return
        d = (data.get("dir") or "").strip()
        if not d:
            self._json({"ok": False, "error": "请先选择安装目录"})
            return
        custom_url = (data.get("custom_url") or "").strip()
        if custom_url:
            ok, msg, is_official = validate_custom_url(custom_url)
            if not ok:
                self._json({"ok": False, "error": f"自定义下载地址无效：{msg}"})
                return
        settings["server_dir"] = d
        ignore_ssl = settings.get("ignore_ssl", False)
        threading.Thread(
            target=do_install,
            args=(
                d,
                (data.get("version") or "").strip(),
                bool(data.get("autostart", True)),
                custom_url,
                int(data.get("source_index", 0)),
            ),
            daemon=True,
        ).start()
        self._json({"ok": True})

    @register_post_route("/api/update")
    def _post_update(self, data):
        from ...backup import backup_worlds, delete_server
        from ...install import do_install, get_latest_server_info
        from ...server import get_server_proc, server_running
        from ...state import console, install_state, settings
        from ...utils import installed, resolve_server_dir

        if install_state.busy:
            self._json({"ok": False, "error": "已有安装/更新任务正在进行"})
            return
        d = resolve_server_dir()
        if not d or not installed():
            self._json({"ok": False, "error": "尚未安装服务器，无法更新"})
            return
        cur_ver = settings.get("installed_version", "")
        target_ver = (data.get("version") or "").strip()
        ignore_ssl = settings.get("ignore_ssl", False)
        if not target_ver:
            try:
                info = get_latest_server_info(ignore_ssl=ignore_ssl)
                settings["_latest"] = info
                target_ver = info["version"]
            except Exception as e:
                self._json({"ok": False, "error": f"获取最新版本失败：{e}"})
                return
        if target_ver == cur_ver:
            self._json({"ok": True, "already_latest": True, "current": cur_ver, "target": target_ver})
            return
        was_running = server_running()
        if was_running:
            with contextlib.suppress(Exception):
                get_server_proc().stop(wait=10)
        backup_worlds(d)
        try:
            delete_server(d, "server_only")
        except Exception as e:
            self._json({"ok": False, "error": f"清理旧版本失败：{e}"})
            return
        console.append("\n[系统] 正在更新服务器：{} → {}\n".format(cur_ver or "（未知）", target_ver))
        threading.Thread(target=do_install, args=(d, target_ver, False), daemon=True).start()
        self._json(
            {
                "ok": True,
                "already_latest": False,
                "current": cur_ver,
                "target": target_ver,
                "was_running": was_running,
            }
        )

    @register_post_route("/api/config")
    def _post_config(self, data):
        from ...config import backup_config_history, load_properties, properties_to_dict, save_properties, validate_config_updates
        from ...security import audit_log
        from ...utils import props_path

        p = props_path()
        if not p:
            self._json({"ok": False, "error": "尚未安装服务器"})
            return
        updates = data.get("data") or {}
        ok, err, failed_field = validate_config_updates(updates)
        if not ok:
            self._json({"ok": False, "error": f"配置校验失败：{err}", "failed_field": failed_field})
            return
        items = load_properties(p)
        cur = properties_to_dict(items)
        for k, v in updates.items():
            cur[k] = str(v)
        seen = set()
        merged = []
        for k, _v in items:
            if k in cur:
                merged.append((k, cur[k]))
                seen.add(k)
        for k in cur:
            if k not in seen:
                merged.append((k, cur[k]))
        backup_config_history(p)
        save_properties(p, merged)
        audit_log("CONFIG_SAVE", ",".join(updates.keys()))
        self._json({"ok": True})

    @register_post_route("/api/server/delete")
    def _post_server_delete(self, data):
        from ...backup import backup_worlds, delete_server
        from ...security import audit_log, check_dangerous_operation
        from ...state import console, settings
        from ...utils import save_settings

        ok, err = check_dangerous_operation("/api/server/delete", data)
        if not ok:
            self._json(
                {
                    "ok": False,
                    "error": err,
                    "need_confirm": True,
                    "confirm_key": "confirm_delete",
                    "confirm_value": "DELETE",
                }
            )
            return
        d = (data.get("dir") or settings.get("server_dir") or "").strip()
        mode = data.get("mode", "server_only")
        try:
            backup_worlds(d)
            delete_server(d, mode)
            if settings.get("server_dir") and os.path.normcase(
                os.path.abspath(settings["server_dir"])
            ) == os.path.normcase(os.path.abspath(d)):
                settings["server_dir"] = ""
                settings["installed_version"] = ""
            versions_map = settings.get("server_versions", {})
            if isinstance(versions_map, dict):
                key = os.path.normcase(os.path.abspath(d))
                versions_map.pop(key, None)
                settings["server_versions"] = versions_map
            history = settings.get("server_dir_history", [])
            if isinstance(history, list):
                abs_d = os.path.normcase(os.path.abspath(d))
                settings["server_dir_history"] = [
                    h for h in history if os.path.normcase(os.path.abspath(h)) != abs_d
                ]
            save_settings()
            console.append(
                "\n[系统] 服务器已删除：{}（{}）\n".format(
                    d, "仅删程序，保留世界存档与配置" if mode == "server_only" else "彻底删除，含世界存档"
                )
            )
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

    @register_post_route("/api/players/kick")
    def _post_players_kick(self, data):
        from ...players import validate_player_name
        from ...server import get_server_proc, server_running
        from ...state import console

        name = (data.get("name") or "").strip()
        reason = (data.get("reason") or "").strip()
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        if not server_running():
            self._json({"ok": False, "error": "服务器未运行"})
            return
        cmd = f"kick {name} {reason}" if reason else f"kick {name}"
        get_server_proc().send(cmd)
        console.append(f"\n[玩家管理] 踢出玩家：{name}\n")
        self._json({"ok": True})

    @register_post_route("/api/players/op")
    def _post_players_op(self, data):
        from ...players import validate_player_name
        from ...server import get_server_proc, server_running
        from ...state import console

        name = (data.get("name") or "").strip()
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        if not server_running():
            self._json({"ok": False, "error": "服务器未运行"})
            return
        get_server_proc().send(f"op {name}")
        console.append(f"\n[玩家管理] 设置管理员：{name}\n")
        self._json({"ok": True})

    @register_post_route("/api/players/deop")
    def _post_players_deop(self, data):
        from ...players import validate_player_name
        from ...server import get_server_proc, server_running
        from ...state import console

        name = (data.get("name") or "").strip()
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        if not server_running():
            self._json({"ok": False, "error": "服务器未运行"})
            return
        get_server_proc().send(f"deop {name}")
        console.append(f"\n[玩家管理] 取消管理员：{name}\n")
        self._json({"ok": True})

    @register_post_route("/api/players/permission")
    def _post_players_permission(self, data):
        from ...players import validate_player_name
        from ...server import get_server_proc, server_running
        from ...state import console

        name = (data.get("name") or "").strip()
        level = (data.get("level") or "member").strip()
        if level not in ("visitor", "member", "operator"):
            self._json({"ok": False, "error": "权限级别无效"})
            return
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        if not server_running():
            self._json({"ok": False, "error": "服务器未运行"})
            return
        get_server_proc().send(f"permission set {name} {level}")
        console.append(f"\n[玩家管理] 设置权限 {name} → {level}\n")
        self._json({"ok": True})

    @register_post_route("/api/players/ban")
    def _post_players_ban(self, data):
        from ...players import validate_player_name
        from ...server import get_server_proc, server_running
        from ...state import console

        name = (data.get("name") or "").strip()
        reason = (data.get("reason") or "").strip()
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        if not server_running():
            self._json({"ok": False, "error": "服务器未运行"})
            return
        cmd = f"ban {name} {reason}" if reason else f"ban {name}"
        get_server_proc().send(cmd)
        console.append(f"\n[玩家管理] 封禁玩家：{name}\n")
        self._json({"ok": True})

    @register_post_route("/api/players/pardon")
    def _post_players_pardon(self, data):
        from ...players import validate_player_name
        from ...server import get_server_proc, server_running
        from ...state import console

        name = (data.get("name") or "").strip()
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        if not server_running():
            self._json({"ok": False, "error": "服务器未运行"})
            return
        get_server_proc().send(f"pardon {name}")
        console.append(f"\n[玩家管理] 解封玩家：{name}\n")
        self._json({"ok": True})

    @register_post_route("/api/players/allowlist/add")
    def _post_players_allowlist_add(self, data):
        from ...players import add_allowlist, validate_player_name
        from ...state import console
        from ...utils import resolve_server_dir

        name = (data.get("name") or "").strip()
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        ok = add_allowlist(d, name)
        if ok:
            console.append(f"\n[玩家管理] 添加白名单：{name}\n")
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "添加失败"})

    @register_post_route("/api/players/allowlist/remove")
    def _post_players_allowlist_remove(self, data):
        from ...players import remove_allowlist, validate_player_name
        from ...state import console
        from ...utils import resolve_server_dir

        name = (data.get("name") or "").strip()
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        ok = remove_allowlist(d, name)
        if ok:
            console.append(f"\n[玩家管理] 移除白名单：{name}\n")
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "移除失败"})

    @register_post_route("/api/setdir")
    def _post_setdir(self, data):
        from ...install import select_server_dir

        ok, err, info = select_server_dir(data.get("dir", ""))
        if not ok:
            self._json({"ok": False, "error": err})
            return
        self._json({"ok": True, **info})
