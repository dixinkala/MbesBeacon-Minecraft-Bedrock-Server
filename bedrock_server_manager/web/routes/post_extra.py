"""
额外 POST 路由处理方法 mixin。
包含从 handler.py 的 do_POST 中迁移的硬编码 POST 路由。
"""

import contextlib
import os
import threading

from ... import remote
from ...app_context import AppContext
from ...app_logger import safe_log_exception
from ...app_update import check_app_update
from ...backup import backup_worlds, delete_server
from ...install import do_install, get_latest_server_info, select_server_dir
from ...players import add_allowlist, remove_allowlist, validate_player_name
from ...security import audit_log, check_dangerous_operation, validate_custom_url, validate_reason
from ...server import get_server_proc, server_running
from ...utils import (
    installed,
    resolve_server_dir,
    save_settings,
)
from ...web.app import AppContext as WebAppContext
from ..route_decorator import register_post_route


class PostExtraRoutesMixin:
    """额外 POST 路由处理方法 mixin。"""

    @register_post_route("/api/latest")
    def _post_latest(self, data):

        try:
            info = get_latest_server_info(ignore_ssl=AppContext.instance().settings.get("ignore_ssl", False))
            AppContext.instance().settings["_latest"] = info
            self._json({"ok": True, "info": info})
        except Exception as e:
            safe_log_exception("post_extra", f"_post_latest 忽略异常: {e}", "warning")
            self._json({"ok": False, "error": str(e)})

    @register_post_route("/api/servers/select")
    def _post_servers_select(self, data):

        ok, err, info = select_server_dir(data.get("dir", ""))
        if not ok:
            self._json({"ok": False, "error": err})
            return
        audit_log("SERVER_SELECT", data.get("dir", ""), None)
        self._json({"ok": True, **info})

    @register_post_route("/api/install")
    def _post_install(self, data):

        if AppContext.instance().install_state.busy:
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
        AppContext.instance().settings["server_dir"] = d
        try:
            source_index = int(data.get("source_index", 0) or 0)
        except (TypeError, ValueError):
            source_index = 0
        threading.Thread(
            target=do_install,
            args=(
                d,
                (data.get("version") or "").strip(),
                bool(data.get("autostart", True)),
                custom_url,
                source_index,
            ),
            daemon=True,
        ).start()
        self._json({"ok": True})

    @register_post_route("/api/update")
    def _post_update(self, data):

        if AppContext.instance().install_state.busy:
            self._json({"ok": False, "error": "已有安装/更新任务正在进行"})
            return
        d = resolve_server_dir()
        if not d or not installed():
            self._json({"ok": False, "error": "尚未安装服务器，无法更新"})
            return
        cur_ver = AppContext.instance().settings.get("installed_version", "")
        target_ver = (data.get("version") or "").strip()
        ignore_ssl = AppContext.instance().settings.get("ignore_ssl", False)
        if not target_ver:
            try:
                info = get_latest_server_info(ignore_ssl=ignore_ssl)
                AppContext.instance().settings["_latest"] = info
                target_ver = info["version"]
            except Exception as e:
                safe_log_exception("post_extra", f"_post_update 忽略异常: {e}", "warning")
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
        # 不再在下载前 delete_server：do_install 现在会先下载并校验到暂存目录，
        # 通过后才并入服务器目录（并保留用户数据）。这样下载/校验失败时
        # 原服务器仍可正常启动，不会出现"更新失败且没有服务端"的状态。
        AppContext.instance().console.append(
            "\n[系统] 正在更新服务器：{} → {}\n".format(cur_ver or "（未知）", target_ver)
        )
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

        updates = data.get("data") or {}
        ok, err, failed_field = WebAppContext.instance().save_config(updates)
        if not ok:
            if failed_field:
                self._json({"ok": False, "error": f"配置校验失败：{err}", "failed_field": failed_field})
            else:
                self._json({"ok": False, "error": err})
            return
        self._json({"ok": True})

    @register_post_route("/api/server/delete")
    def _post_server_delete(self, data):

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
        d = (data.get("dir") or AppContext.instance().settings.get("server_dir") or "").strip()
        mode = data.get("mode", "server_only")
        try:
            backup_worlds(d)
            delete_server(d, mode)
            if AppContext.instance().settings.get("server_dir") and os.path.normcase(
                os.path.abspath(AppContext.instance().settings["server_dir"])
            ) == os.path.normcase(os.path.abspath(d)):
                AppContext.instance().settings["server_dir"] = ""
                AppContext.instance().settings["installed_version"] = ""
            versions_map = AppContext.instance().settings.get("server_versions", {})
            if isinstance(versions_map, dict):
                key = os.path.normcase(os.path.abspath(d))
                versions_map.pop(key, None)
                AppContext.instance().settings["server_versions"] = versions_map
            history = AppContext.instance().settings.get("server_dir_history", [])
            if isinstance(history, list):
                abs_d = os.path.normcase(os.path.abspath(d))
                AppContext.instance().settings["server_dir_history"] = [
                    h for h in history if os.path.normcase(os.path.abspath(h)) != abs_d
                ]
            save_settings()
            audit_log("SERVER_DELETE", f"{mode} {d}", None)
            AppContext.instance().console.append(
                "\n[系统] 服务器已删除：{}（{}）\n".format(
                    d, "仅删程序，保留世界存档与配置" if mode == "server_only" else "彻底删除，含世界存档"
                )
            )
            self._json({"ok": True})
        except Exception as e:
            safe_log_exception("post_extra", f"_post_server_delete 忽略异常: {e}", "warning")
            self._json({"ok": False, "error": str(e)})

    @register_post_route("/api/players/kick")
    def _post_players_kick(self, data):

        name = (data.get("name") or "").strip()
        reason = (data.get("reason") or "").strip()
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        ok, err = validate_reason(reason)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        if not server_running():
            self._json({"ok": False, "error": "服务器未运行"})
            return
        cmd = f"kick {name} {reason}" if reason else f"kick {name}"
        get_server_proc().send(cmd)
        AppContext.instance().console.append(f"\n[玩家管理] 踢出玩家：{name}\n")
        audit_log("PLAYER_KICK", f"{name} {reason}", resolve_server_dir())
        self._json({"ok": True})

    @register_post_route("/api/players/op")
    def _post_players_op(self, data):

        name = (data.get("name") or "").strip()
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        if not server_running():
            self._json({"ok": False, "error": "服务器未运行"})
            return
        get_server_proc().send(f"op {name}")
        AppContext.instance().console.append(f"\n[玩家管理] 设置管理员：{name}\n")
        audit_log("PLAYER_OP", name, resolve_server_dir())
        self._json({"ok": True})

    @register_post_route("/api/players/deop")
    def _post_players_deop(self, data):

        name = (data.get("name") or "").strip()
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        if not server_running():
            self._json({"ok": False, "error": "服务器未运行"})
            return
        get_server_proc().send(f"deop {name}")
        AppContext.instance().console.append(f"\n[玩家管理] 取消管理员：{name}\n")
        audit_log("PLAYER_DEOP", name, resolve_server_dir())
        self._json({"ok": True})

    @register_post_route("/api/players/permission")
    def _post_players_permission(self, data):

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
        AppContext.instance().console.append(f"\n[玩家管理] 设置权限 {name} → {level}\n")
        audit_log("PLAYER_PERMISSION", f"{name} {level}", resolve_server_dir())
        self._json({"ok": True})

    @register_post_route("/api/players/ban")
    def _post_players_ban(self, data):

        name = (data.get("name") or "").strip()
        reason = (data.get("reason") or "").strip()
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        ok, err = validate_reason(reason)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        if not server_running():
            self._json({"ok": False, "error": "服务器未运行"})
            return
        cmd = f"ban {name} {reason}" if reason else f"ban {name}"
        get_server_proc().send(cmd)
        AppContext.instance().console.append(f"\n[玩家管理] 封禁玩家：{name}\n")
        audit_log("PLAYER_BAN", f"{name} {reason}", resolve_server_dir())
        self._json({"ok": True})

    @register_post_route("/api/players/pardon")
    def _post_players_pardon(self, data):

        name = (data.get("name") or "").strip()
        ok, err = validate_player_name(name)
        if not ok:
            self._json({"ok": False, "error": err})
            return
        if not server_running():
            self._json({"ok": False, "error": "服务器未运行"})
            return
        get_server_proc().send(f"pardon {name}")
        AppContext.instance().console.append(f"\n[玩家管理] 解封玩家：{name}\n")
        audit_log("PLAYER_PARDON", name, resolve_server_dir())
        self._json({"ok": True})

    @register_post_route("/api/players/allowlist/add")
    def _post_players_allowlist_add(self, data):

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
            AppContext.instance().console.append(f"\n[玩家管理] 添加白名单：{name}\n")
            audit_log("ALLOWLIST_ADD", name, d)
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "添加失败"})

    @register_post_route("/api/players/allowlist/remove")
    def _post_players_allowlist_remove(self, data):

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
            AppContext.instance().console.append(f"\n[玩家管理] 移除白名单：{name}\n")
            audit_log("ALLOWLIST_REMOVE", name, d)
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "移除失败"})

    @register_post_route("/api/setdir")
    def _post_setdir(self, data):

        ok, err, info = select_server_dir(data.get("dir", ""))
        if not ok:
            self._json({"ok": False, "error": err})
            return
        audit_log("SERVER_SELECT", data.get("dir", ""), None)
        self._json({"ok": True, **info})

    @register_post_route("/api/app/update/check")
    def _post_app_update_check(self, data):
        """检查 MbesBeacon 软件本身的更新（与服务器更新区分开）。"""

        force = bool(data.get("force", False))
        ignore_ssl = AppContext.instance().settings.get("ignore_ssl", False)

        try:
            result = check_app_update(force=force, ignore_ssl=ignore_ssl)
            self._json(result)
        except Exception as e:
            safe_log_exception("post_extra", f"_post_app_update_check 忽略异常: {e}", "warning")
            self._json({"ok": False, "error": str(e), "has_update": False})

    @register_post_route("/api/remote/config")
    def _post_remote_config(self, data):
        """启用/关闭远程管理、设置访问密码（仅限本机操作）。"""
        from ...web.handler import _client_ip, _is_loopback_client

        if not _is_loopback_client(self):
            self._json({"ok": False, "error": "只能在运行本程序的机器上修改远程设置"}, code=403)
            return
        enabled_flag = bool(data.get("enabled", False))
        password = data.get("password")
        if password is not None and not isinstance(password, str):
            self._json({"ok": False, "error": "密码格式不正确"}, code=400)
            return
        hosts = data.get("hosts")
        if hosts is not None and not isinstance(hosts, list):
            self._json({"ok": False, "error": "hosts 必须是数组"}, code=400)
            return
        try:
            result = remote.set_config(enabled_flag, password=password or None, hosts=hosts)
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, code=400)
            return
        audit_log(
            "REMOTE_CONFIG",
            f"enabled={result['enabled']} password_set={bool(password)} ip={_client_ip(self)}",
            None,
        )
        self._json(
            {
                "ok": True,
                "enabled": result["enabled"],
                "restart_required": result["restart_required"],
                "message": "设置已保存，重启程序后生效" if result["restart_required"] else "设置已保存",
            }
        )

    # ---- 内网穿透 ----

    def _tunnel_loopback_guard(self):
        from ...web.handler import _is_loopback_client

        return _is_loopback_client(self)

    @staticmethod
    def _tunnel_port(data):
        from ...server import get_server_port

        raw = data.get("port") if isinstance(data, dict) else None
        try:
            port = int(raw) if raw else get_server_port(resolve_server_dir())
        except (TypeError, ValueError):
            port = get_server_port(resolve_server_dir())
        if not 1 <= port <= 65535:
            raise ValueError(f"端口不合法: {port}")
        return port

    @register_post_route("/api/tunnel/upnp/open")
    def _post_tunnel_upnp_open(self, data):
        """为游戏端口添加 UPnP 映射。"""
        from ... import remote, upnp

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "端口映射只能在运行本程序的机器上操作"}, code=403)
            return
        try:
            port = self._tunnel_port(data or {})
            internal_ip = (data or {}).get("internal_ip") or remote.primary_lan_ip()
            result = upnp.add_mapping(port, internal_ip, protocol="UDP")
            state = upnp.mapping_state(port)
            audit_log("TUNNEL_UPNP_OPEN", f"udp/{port} -> {internal_ip}", None)
            self._json({"ok": True, "port": port, "internal_ip": internal_ip, **result, "state": state})
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, code=400)
        except Exception as e:
            safe_log_exception("post_extra", f"UPnP 映射失败: {e}", "warning")
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/tunnel/upnp/close")
    def _post_tunnel_upnp_close(self, data):
        """删除游戏端口的 UPnP 映射。"""
        from ... import upnp

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "端口映射只能在运行本程序的机器上操作"}, code=403)
            return
        try:
            port = self._tunnel_port(data or {})
            upnp.delete_mapping(port, protocol="UDP")
            audit_log("TUNNEL_UPNP_CLOSE", f"udp/{port}", None)
            self._json({"ok": True, "port": port})
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, code=400)
        except Exception as e:
            safe_log_exception("post_extra", f"删除 UPnP 映射失败: {e}", "warning")
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/tunnel/config")
    def _post_tunnel_config(self, data):
        """保存外部穿透客户端配置。"""
        from ... import tunnel

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "穿透设置只能在运行本程序的机器上修改"}, code=403)
            return
        try:
            cfg = tunnel.save_config(
                provider=str((data or {}).get("provider") or "playit"),
                command=str((data or {}).get("command") or ""),
                address_regex=str((data or {}).get("address_regex") or ""),
                public_address=str((data or {}).get("public_address") or ""),
                enabled=bool((data or {}).get("enabled", False)),
            )
            audit_log("TUNNEL_CONFIG", f"provider={cfg['provider']}", None)
            self._json({"ok": True, "config": cfg})
        except tunnel.TunnelError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/tunnel/start")
    def _post_tunnel_start(self, data):
        """启动外部穿透客户端。"""
        from ... import tunnel

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "穿透客户端只能在运行本程序的机器上启动"}, code=403)
            return
        try:
            port = self._tunnel_port(data or {})
            status = tunnel.manager.start(port)
            audit_log("TUNNEL_START", f"provider={status.get('provider')} port={port}", None)
            self._json({"ok": True, "status": status, "port": port})
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, code=400)
        except tunnel.TunnelError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/tunnel/stop")
    def _post_tunnel_stop(self, data):
        """停止外部穿透客户端。"""
        from ... import tunnel

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "穿透客户端只能在运行本程序的机器上停止"}, code=403)
            return
        status = tunnel.manager.stop()
        audit_log("TUNNEL_STOP", "", None)
        self._json({"ok": True, "status": status})

    # ---- 内置穿透工具 ----

    @register_post_route("/api/tunnel/builtin/install")
    def _post_builtin_tunnel_install(self, data):
        """下载并安装内置穿透工具（playit / frp）。"""
        from ... import builtin_tunnel

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "内置穿透工具只能在运行本程序的机器上安装"}, code=403)
            return
        tool = str((data or {}).get("tool") or "")
        try:
            if tool == "playit":
                result = builtin_tunnel.install_playit()
            elif tool == "frp":
                result = builtin_tunnel.install_frp()
            else:
                self._json({"ok": False, "error": "tool 只能是 playit 或 frp"}, code=400)
                return
            audit_log("TUNNEL_INSTALL", f"{tool} {result.get('version', '')}", None)
            self._json({"ok": True, "tool": tool, **result})
        except builtin_tunnel.BuiltinTunnelError as e:
            self._json({"ok": False, "error": str(e)}, code=400)
        except Exception as e:
            safe_log_exception("post_extra", f"安装 {tool} 失败: {e}", "warning")
            self._json({"ok": False, "error": str(e)}, code=500)

    @register_post_route("/api/tunnel/playit/claim")
    def _post_playit_claim(self, data):
        """playit 领取：action=start 生成链接，action=check 检查是否已领取。"""
        from ... import builtin_tunnel

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        action = str((data or {}).get("action") or "start")
        try:
            if action == "start":
                result = builtin_tunnel.playit.claim_start()
                audit_log("TUNNEL_PLAYIT_CLAIM", result.get("url", ""), None)
                self._json({"ok": True, "action": "start", **result})
            elif action == "check":
                result = builtin_tunnel.playit.claim_check()
                self._json({"ok": True, "action": "check", **result})
            else:
                self._json({"ok": False, "error": "action 只能是 start 或 check"}, code=400)
        except builtin_tunnel.BuiltinTunnelError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/tunnel/playit/start")
    def _post_playit_start(self, data):
        from ... import builtin_tunnel

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        try:
            status = builtin_tunnel.playit.start()
            audit_log("TUNNEL_PLAYIT_START", status.get("address", ""), None)
            self._json({"ok": True, "status": status})
        except builtin_tunnel.BuiltinTunnelError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/tunnel/playit/stop")
    def _post_playit_stop(self, data):
        from ... import builtin_tunnel

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        status = builtin_tunnel.playit.stop()
        audit_log("TUNNEL_PLAYIT_STOP", "", None)
        self._json({"ok": True, "status": status})

    @register_post_route("/api/tunnel/frp/config")
    def _post_frp_config(self, data):
        from ... import builtin_tunnel

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        payload = data or {}
        try:
            cfg = builtin_tunnel.save_frp_config(
                server_addr=payload.get("server_addr", ""),
                server_port=payload.get("server_port", 7000),
                token=payload.get("token", ""),
                remote_port=payload.get("remote_port", 19132),
                local_port=payload.get("local_port", 19132),
            )
            audit_log("TUNNEL_FRP_CONFIG", f"{cfg['server_addr']}:{cfg['remote_port']}", None)
            self._json({"ok": True, "config": cfg})
        except builtin_tunnel.BuiltinTunnelError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/tunnel/frp/start")
    def _post_frp_start(self, data):
        from ... import builtin_tunnel

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        try:
            status = builtin_tunnel.frp.start()
            audit_log("TUNNEL_FRP_START", status.get("address", ""), None)
            self._json({"ok": True, "status": status})
        except builtin_tunnel.BuiltinTunnelError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/tunnel/frp/stop")
    def _post_frp_stop(self, data):
        from ... import builtin_tunnel

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        status = builtin_tunnel.frp.stop()
        audit_log("TUNNEL_FRP_STOP", "", None)
        self._json({"ok": True, "status": status})

    # ---- LeviLamina ----

    @register_post_route("/api/levilamina/lip/install")
    def _post_levilamina_lip_install(self, data):
        from ... import levilamina

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        try:
            result = levilamina.install_lip()
            audit_log("LEVILAMINA_LIP", f"version={result.get('version', '')}", None)
            self._json({"ok": True, **result})
        except levilamina.LeviLaminaError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/levilamina/enable")
    def _post_levilamina_enable(self, data):
        from ... import levilamina
        from ...install import detect_server_version
        from ...server import server_running

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        if not bool((data or {}).get("confirm")):
            self._json({"ok": False, "error": "请先确认：会按兼容版本重装服务端文件（已自动备份配置）"}, code=400)
            return
        if server_running():
            self._json({"ok": False, "error": "请先停止服务器"}, code=400)
            return
        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"}, code=400)
            return
        version = detect_server_version(d)
        try:
            result = levilamina.enable_loader(d, version)
            audit_log("LEVILAMINA_ENABLE", f"game={result.get('game')} version={version}", None)
            self._json({"ok": True, **result})
        except levilamina.LeviLaminaError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/levilamina/disable")
    def _post_levilamina_disable(self, data):
        from ... import levilamina
        from ...server import server_running

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        if server_running():
            self._json({"ok": False, "error": "请先停止服务器"}, code=400)
            return
        try:
            result = levilamina.disable_loader(resolve_server_dir())
            audit_log("LEVILAMINA_DISABLE", "", None)
            self._json({"ok": True, **result})
        except levilamina.LeviLaminaError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/levilamina/mods/install")
    def _post_levilamina_mod_install(self, data):
        import base64
        import tempfile

        from ... import levilamina

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"}, code=400)
            return
        url = str((data or {}).get("url") or "").strip()
        raw = (data or {}).get("file_data") or ""
        try:
            if url:
                result = levilamina.install_mod_url(d, url)
            elif raw:
                blob = base64.b64decode(raw, validate=True)
                if len(blob) > 120 * 1024 * 1024:
                    self._json({"ok": False, "error": "插件包过大"}, code=400)
                    return
                fd, tmp = tempfile.mkstemp(prefix="mod-", suffix=".zip")
                try:
                    with open(fd, "wb") as handle:
                        handle.write(blob)
                    result = levilamina.install_mod_zip(d, tmp, display_name=str((data or {}).get("file_name") or ""))
                finally:
                    import contextlib
                    import os

                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
            else:
                self._json({"ok": False, "error": "请提供插件包文件或 URL"}, code=400)
                return
            audit_log("LEVILAMINA_MOD_INSTALL", str((data or {}).get("file_name") or url)[:120], None)
            self._json({"ok": True, **result})
        except levilamina.LeviLaminaError as e:
            self._json({"ok": False, "error": str(e)}, code=400)
        except Exception as e:
            safe_log_exception("post_extra", f"安装插件失败: {e}", "warning")
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/levilamina/mods/toggle")
    def _post_levilamina_mod_toggle(self, data):
        from ... import levilamina

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        try:
            result = levilamina.toggle_mod(
                resolve_server_dir(), str((data or {}).get("name") or ""), bool((data or {}).get("enabled", True))
            )
            self._json({"ok": True, **result})
        except levilamina.LeviLaminaError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/levilamina/mods/delete")
    def _post_levilamina_mod_delete(self, data):
        from ... import levilamina

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        if not bool((data or {}).get("confirm")):
            self._json({"ok": False, "error": "删除插件需要确认"}, code=400)
            return
        try:
            result = levilamina.delete_mod(resolve_server_dir(), str((data or {}).get("name") or ""))
            audit_log("LEVILAMINA_MOD_DELETE", str(result.get("name")), None)
            self._json({"ok": True, **result})
        except levilamina.LeviLaminaError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/levilamina/lip/install_pkg")
    def _post_levilamina_lip_install_pkg(self, data):
        from ... import levilamina

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        try:
            result = levilamina.lip_install(resolve_server_dir(), str((data or {}).get("package") or ""))
            audit_log("LEVILAMINA_LIP_INSTALL", str((data or {}).get("package"))[:120], None)
            self._json({"ok": True, **result})
        except levilamina.LeviLaminaError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/levilamina/lip/uninstall_pkg")
    def _post_levilamina_lip_uninstall_pkg(self, data):
        from ... import levilamina

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "只能在运行本程序的机器上操作"}, code=403)
            return
        try:
            result = levilamina.lip_uninstall(resolve_server_dir(), str((data or {}).get("package") or ""))
            audit_log("LEVILAMINA_LIP_UNINSTALL", str((data or {}).get("package"))[:120], None)
            self._json({"ok": True, **result})
        except levilamina.LeviLaminaError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    # ---- 多实例 ----

    @register_post_route("/api/instances/add")
    def _post_instance_add(self, data):
        from ...server import list_instances, register_instance
        from ...utils import resolve_server_exe

        if not self._tunnel_loopback_guard():
            self._json({"ok": False, "error": "添加实例只能在运行本程序的机器上操作"}, code=403)
            return
        dir_path = str((data or {}).get("dir") or "").strip()
        if not dir_path:
            self._json({"ok": False, "error": "请提供服务器目录"}, code=400)
            return
        if not resolve_server_exe(dir_path):
            self._json({"ok": False, "error": "该目录不是已安装的服务端"}, code=400)
            return
        try:
            entry = register_instance(dir_path, str((data or {}).get("name") or ""))
            audit_log("INSTANCE_ADD", entry["dir"], None)
            self._json({"ok": True, "instance": entry, "instances": list_instances()})
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/instances/remove")
    def _post_instance_remove(self, data):
        from ...server import list_instances, unregister_instance

        dir_path = str((data or {}).get("dir") or "").strip()
        if not dir_path:
            self._json({"ok": False, "error": "缺少 dir"}, code=400)
            return
        try:
            unregister_instance(dir_path)
            audit_log("INSTANCE_REMOVE", dir_path, None)
            self._json({"ok": True, "instances": list_instances()})
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/instances/activate")
    def _post_instance_activate(self, data):
        from ...server import activate_instance, list_instances

        dir_path = str((data or {}).get("dir") or "").strip()
        try:
            entry = activate_instance(dir_path)
            audit_log("INSTANCE_ACTIVATE", entry["dir"], None)
            self._json({"ok": True, "instance": entry, "instances": list_instances()})
        except (ValueError, OSError) as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/instances/start")
    def _post_instance_start(self, data):
        from ...server import list_instances, start_instance

        dir_path = str((data or {}).get("dir") or "").strip()
        try:
            start_instance(dir_path)
            self._json({"ok": True, "instances": list_instances()})
        except (ValueError, OSError, FileNotFoundError) as e:
            self._json({"ok": False, "error": str(e)}, code=400)

    @register_post_route("/api/instances/stop")
    def _post_instance_stop(self, data):
        from ...server import list_instances, stop_instance

        dir_path = str((data or {}).get("dir") or "").strip()
        try:
            stop_instance(dir_path)
            self._json({"ok": True, "instances": list_instances()})
        except (ValueError, OSError) as e:
            self._json({"ok": False, "error": str(e)}, code=400)
