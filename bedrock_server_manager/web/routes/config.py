"""
config 相关路由处理方法
"""

from ..route_decorator import register_route


class ConfigRoutesMixin:
    """config 相关路由处理方法 mixin。"""

    from ...config import list_config_history, rollback_config
    from ...utils import resolve_server_dir

    @register_route("/api/config/history")
    def _route_config_history(self, data):
        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未找到服务器"})
            return
        history = list_config_history(d)
        self._json({"ok": True, "history": history, "count": len(history)})

    @register_route("/api/config/rollback")
    def _route_config_rollback(self, data):
        import os

        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未找到服务器"})
            return
        backup_path = (data.get("path") or "").strip()
        if not backup_path:
            self._json({"ok": False, "error": "未指定备份路径"})
            return
        # backup_path 可能是完整路径或文件名
        backup_name = os.path.basename(backup_path)
        ok, err = rollback_config(d, backup_name)
        if ok:
            self._json({"ok": True, "message": "配置已回滚，需重启服务器生效"})
        else:
            self._json({"ok": False, "error": err})
