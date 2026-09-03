"""
console 相关路由处理方法
"""

from ..route_decorator import register_route


class ConsoleRoutesMixin:
    """console 相关路由处理方法 mixin。"""

    @register_route("/api/cmd")
    def _route_cmd(self, data):
        from ...security import validate_command
        from ...server import get_server_proc
        from ...state import console

        try:
            cmd = data.get("cmd", "")
            ok, err, is_dangerous = validate_command(cmd)
            if not ok:
                self._json({"ok": False, "error": err})
                return
            p = get_server_proc()
            p.send(cmd)
            console.append(f"\n[控制台] > {cmd}\n")
            self._json({"ok": True, "dangerous": is_dangerous})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

    @register_route("/api/console/clear")
    def _route_console_clear(self, data):
        from ...state import console

        console.clear()
        self._json({"ok": True})
