"""
misc 相关路由处理方法
"""

import threading

from ..route_decorator import register_route, register_get_route


class MiscRoutesMixin:
    """misc 相关路由处理方法 mixin。"""

    @register_route("/api/cancel")
    def _route_cancel(self, data):
        from ...state import install_state

        install_state.cancel.set()
        self._json({"ok": True})

    @register_route("/api/exit")
    def _route_exit(self, data):
        self._json({"ok": True})
        threading.Thread(target=self._shutdown_later, daemon=True).start()

    @register_route("/api/theme")
    def _route_theme(self, data):
        from ...state import DEFAULT_THEME, settings
        from ...utils import save_theme

        t = data.get("theme") or {}
        new_theme = dict(DEFAULT_THEME)
        for k in ("preset", "accent", "mode"):
            if k in t:
                new_theme[k] = t[k]
        settings["theme"] = new_theme
        save_theme(new_theme)
        self._json({"ok": True})

    @register_route("/api/audit")
    def _route_audit(self, data):
        from ...security import read_audit_log

        limit = int(data.get("limit", 200))
        logs = read_audit_log(limit=limit)
        self._json({"ok": True, "logs": logs, "count": len(logs)})

    @register_get_route("/api/packs")
    def _route_packs_list(self, q):
        from ...players import list_installed_packs

        packs = list_installed_packs()
        self._json({"ok": True, "packs": packs})

    @register_get_route("/api/scheduled/list")
    def _route_scheduled_list(self, q):
        from ...scheduler import load_scheduled_tasks

        tasks = load_scheduled_tasks()
        self._json({"ok": True, "tasks": tasks, "count": len(tasks)})

    @register_route("/api/scheduled/save")
    def _route_scheduled_save(self, data):
        from ...scheduler import save_scheduled_tasks

        tasks = data.get("tasks", [])
        ok, err = save_scheduled_tasks(tasks)
        if ok:
            self._json({"ok": True, "tasks": tasks})
        else:
            self._json({"ok": False, "error": err or "保存失败"})
