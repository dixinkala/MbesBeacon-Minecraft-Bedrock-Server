"""
server 相关路由处理方法
"""

import time

from ...app_context import AppContext
from ...app_logger import safe_log_exception
from ...security import audit_log
from ...server import check_port_in_use, get_server_port, get_server_proc
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
        # 端口占用检测（统一走 server.check_port_in_use，避免两套探测口径不一致）
        try:
            port = get_server_port(d)
            if check_port_in_use(port):
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
            audit_log("SERVER_START", "", d)
            self._json({"ok": True})
        except Exception as e:
            safe_log_exception("server", f"_route_server_start 忽略异常: {e}", "warning")
            self._json({"ok": False, "error": str(e)})

    @register_route("/api/server/stop")
    def _route_server_stop(self, data):

        try:
            p = get_server_proc()
            _console().append("\n[系统] 正在发送 stop 指令，等待服务器保存退出...\n")
            p.stop()
            audit_log("SERVER_STOP", "", resolve_server_dir())
            self._json({"ok": True})
        except Exception as e:
            safe_log_exception("server", f"_route_server_stop 忽略异常: {e}", "warning")
            self._json({"ok": False, "error": str(e)})

    @register_route("/api/server/restart")
    def _route_server_restart(self, data):

        ok, err = WebAppContext.instance().restart_server()
        if not ok:
            self._json({"ok": False, "error": str(err)})
            return
        audit_log("SERVER_RESTART", "", resolve_server_dir())
        self._json({"ok": True})

        # ---------------- 多世界管理 ----------------
