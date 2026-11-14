"""
backups 相关路由处理方法
"""

from ...backup import (
    DEFAULT_MAX_BACKUPS,
    MAX_MAX_BACKUPS,
    backup_worlds,
    backup_worlds_zip,
    delete_backup,
    list_backups,
    restore_backup,
)
from ...security import audit_log, check_dangerous_operation
from ...utils import installed, resolve_server_dir
from ..route_decorator import register_route


def _parse_max_backups(data):
    """解析 max_backups 参数，非法值回退默认，并限制上限避免异常行为。"""
    try:
        value = int(data.get("max_backups", DEFAULT_MAX_BACKUPS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BACKUPS
    return max(1, min(value, MAX_MAX_BACKUPS))


class BackupsRoutesMixin:
    """backups 相关路由处理方法 mixin。"""

    @register_route("/api/backups/create")
    def _route_backup_create(self, data):

        d = resolve_server_dir()
        if not d or not installed():
            self._json({"ok": False, "error": "未找到服务器"})
            return
        max_backups = _parse_max_backups(data)
        path = backup_worlds(d, max_backups=max_backups)
        if path:
            audit_log("BACKUP_CREATE", f"{max_backups} {path}", d)
            self._json({"ok": True, "path": path, "backups": list_backups(d)})
        else:
            self._json({"ok": False, "error": "备份失败，请检查 worlds 目录是否存在"})

    @register_route("/api/backups/restore")
    def _route_backup_restore(self, data):

        # 危险操作二次确认
        ok, err = check_dangerous_operation("/api/backups/restore", data)
        if not ok:
            self._json(
                {
                    "ok": False,
                    "error": err,
                    "need_confirm": True,
                    "confirm_key": "confirm_restore",
                    "confirm_value": "RESTORE",
                }
            )
            return
        backup_path = (data.get("path") or "").strip()
        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        if not backup_path:
            self._json({"ok": False, "error": "未指定备份路径"})
            return
        ok, err = restore_backup(backup_path, d)
        if ok:
            audit_log("BACKUP_RESTORE", backup_path, d)
            self._json({"ok": True, "backups": list_backups(d)})
        else:
            audit_log("BACKUP_RESTORE_FAILED", f"{backup_path}: {err}", d)
            self._json({"ok": False, "error": err})

    @register_route("/api/backups/delete")
    def _route_backup_delete(self, data):

        backup_path = (data.get("path") or "").strip()
        if not backup_path:
            self._json({"ok": False, "error": "未指定备份路径"})
            return
        ok, err = delete_backup(backup_path)
        if ok:
            d = resolve_server_dir()
            audit_log("BACKUP_DELETE", backup_path, d)
            self._json({"ok": True, "backups": list_backups(d) if d else []})
        else:
            self._json({"ok": False, "error": err})

    @register_route("/api/backup/zip")
    def _route_backup_zip(self, data):

        d = resolve_server_dir()
        if not d or not installed():
            self._json({"ok": False, "error": "未找到服务器"})
            return
        max_backups = _parse_max_backups(data)
        path = backup_worlds_zip(d, max_backups=max_backups)
        if path:
            audit_log("BACKUP_ZIP", path, d)
            self._json({"ok": True, "path": path, "backups": list_backups(d)})
        else:
            self._json({"ok": False, "error": "备份失败"})
