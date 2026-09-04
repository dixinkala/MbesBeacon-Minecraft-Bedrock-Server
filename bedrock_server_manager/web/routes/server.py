"""
server 相关路由处理方法
"""

import os
import time

from ...config import load_properties, properties_to_dict
from ...server import get_server_proc
from ...state import console
from ...utils import installed, resolve_server_dir
from ..route_decorator import register_route


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
                s.bind(("0.0.0.0", port))
                port_free = True
            except OSError:
                port_free = False
            finally:
                s.close()
            if not port_free:
                self._json({"ok": False, "error": f"端口 {port} 已被占用，请在「服务器配置」中修改 server-port 后重试"})
                return
        except Exception:
            pass
        try:
            p = get_server_proc()
            p.server_dir = d
            p.start()
            console.append("\n========== 正在启动服务器 ({}) ==========\n".format(time.strftime("%Y-%m-%d %H:%M:%S")))
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

    @register_route("/api/server/stop")
    def _route_server_stop(self, data):

        try:
            p = get_server_proc()
            console.append("\n[系统] 正在发送 stop 指令，等待服务器保存退出...\n")
            p.stop()
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

    @register_route("/api/server/restart")
    def _route_server_restart(self, data):

        try:
            p = get_server_proc()
            p.stop()
            time.sleep(1)
            p.start()
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

        # ---------------- 多世界管理 ----------------
