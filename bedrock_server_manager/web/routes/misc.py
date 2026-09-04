"""
misc 相关路由处理方法
"""

import threading

from ...app_context import AppContext
from ...players import list_installed_packs
from ...scheduler import load_scheduled_tasks, save_scheduled_tasks
from ...security import read_audit_log
from ...state import DEFAULT_THEME
from ...utils import save_theme
from ..route_decorator import register_get_route, register_route


class MiscRoutesMixin:
    """misc 相关路由处理方法 mixin。"""

    @register_route("/api/cancel")
    def _route_cancel(self, data):

        AppContext.instance().install_state.cancel.set()
        self._json({"ok": True})

    @register_route("/api/exit")
    def _route_exit(self, data):
        self._json({"ok": True})
        threading.Thread(target=self._shutdown_later, daemon=True).start()

    @register_route("/api/theme")
    def _route_theme(self, data):

        t = data.get("theme") or {}
        new_theme = dict(DEFAULT_THEME)
        for k in ("preset", "accent", "mode"):
            if k in t:
                new_theme[k] = t[k]
        AppContext.instance().settings["theme"] = new_theme
        save_theme(new_theme)
        self._json({"ok": True})

    @register_route("/api/audit")
    def _route_audit(self, data):
        limit = int(data.get("limit", 200))
        logs = read_audit_log(limit=limit)
        self._json({"ok": True, "logs": logs, "count": len(logs)})

    @register_get_route("/api/packs")
    def _route_packs_list(self, q):
        packs = list_installed_packs()
        self._json({"ok": True, "packs": packs})

    @register_get_route("/api/scheduled/list")
    def _route_scheduled_list(self, q):
        tasks = load_scheduled_tasks()
        self._json({"ok": True, "tasks": tasks, "count": len(tasks)})

    @register_route("/api/scheduled/save")
    def _route_scheduled_save(self, data):
        tasks = data.get("tasks", [])
        ok, err = save_scheduled_tasks(tasks)
        if ok:
            self._json({"ok": True, "tasks": tasks})
        else:
            self._json({"ok": False, "error": err or "保存失败"})
