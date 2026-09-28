"""
server 相关路由处理方法
"""

import os
import time

from ...app_context import AppContext
from ...config import load_properties, properties_to_dict
from ...server import get_server_proc
from ...utils import installed, resolve_server_dir
from ...web.app import AppContext as WebAppContext
from ..route_decorator import register_route


def _console():
    """获取控制台缓冲（经 AppContext，避免 state 顶层绑定快照）。"""
    return AppContext.instance().console


class ServerRoutesMixin:
    """server 相关路由处理方法 mixin。"""

    @register_route("/api/server/start")
    def _route_server_start(self, data):

        d = resolve_server_dir()
        if not installed():
            self._json({"ok": False, "error": "未找到服务器，请先安装"})
            return
        # 端口占用检测
        try:
            import socket as _socket

            props = properties_to_dict(load_properties(os.path.join(d, "server.properties")))
            port = int(props.get("server-port", "19132"))
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            try:
                # 绑定本地回环地址检测端口占用（Windows 下 UDP 端口占用检测有效）
                s.bind(("127.0.0.1", port))
                port_free = True
            except OSError:
                port_free = False
            finally:
                s.close()
            if not port_free:
                self._json({"ok": False, "error": f"端口 {port} 已被占用，请在「服务器配置」中修改 server-port 后重试"})
                return
        except Exception as e:
            # 端口检测失败不影响启动（实际启动时会再次检测）
            from ...app_logger import safe_log_exception

            safe_log_exception("routes/server", f"端口占用检测异常: {e}", "debug")
        try:
            p = get_server_proc()
            p.server_dir = d
            p.start()
            _console().append(
                "\n========== 正在启动服务器 ({}) ==========\n".format(time.strftime("%Y-%m-%d %H:%M:%S"))
            )
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

    @register_route("/api/server/stop")
    def _route_server_stop(self, data):

        try:
            p = get_server_proc()
            _console().append("\n[系统] 正在发送 stop 指令，等待服务器保存退出...\n")
            p.stop()
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

    @register_route("/api/server/restart")
    def _route_server_restart(self, data):

        ok, err = WebAppContext.instance().restart_server()
        if not ok:
            self._json({"ok": False, "error": str(err)})
            return
        self._json({"ok": True})

        # ---------------- 多世界管理 ----------------
