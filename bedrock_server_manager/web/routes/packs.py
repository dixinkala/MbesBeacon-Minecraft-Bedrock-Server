"""
packs 相关路由处理方法：包列表、启用/禁用、导入、删除。
"""

from ...app_context import AppContext
from ...packs import delete_pack, import_pack, list_installed_packs, set_pack_enabled
from ...security import check_dangerous_operation
from ...utils import resolve_server_dir
from ..route_decorator import register_get_route, register_route


def _console():
    """获取控制台缓冲（经 AppContext，避免 state 顶层绑定快照）。"""
    return AppContext.instance().console


class PacksRoutesMixin:
    """packs 相关路由处理方法 mixin。"""

    @register_get_route("/api/packs")
    def _route_packs_list(self, q):
        packs = list_installed_packs()
        self._json({"ok": True, "packs": packs})

    @register_route("/api/packs/toggle")
    def _route_packs_toggle(self, data):
        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        pack_type = (data.get("pack_type") or "").strip().lower()
        name = (data.get("name") or "").strip()
        enabled = bool(data.get("enabled"))
        if pack_type not in ("resource", "behavior"):
            self._json({"ok": False, "error": "包类型必须是 resource 或 behavior"})
            return
        if not name:
            self._json({"ok": False, "error": "请指定包名"})
            return
        ok, err = set_pack_enabled(d, pack_type, name, enabled)
        if ok:
            _console().append(
                f"\n[系统] 已{'启用' if enabled else '禁用'} {pack_type} 包: {name}（重启服务器后生效）\n"
            )
            self._json({"ok": True, "message": f"已{'启用' if enabled else '禁用'}，重启服务器后生效"})
        else:
            self._json({"ok": False, "error": err})

    @register_route("/api/packs/import")
    def _route_packs_import(self, data):
        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        pack_type = (data.get("pack_type") or "").strip().lower()
        if pack_type not in ("resource", "behavior"):
            self._json({"ok": False, "error": "包类型必须是 resource 或 behavior"})
            return
        ok, err, message = import_pack(d, pack_type, data.get("file_data") or "", data.get("file_name") or "pack.zip")
        if ok:
            _console().append(f"\n[系统] 已导入 {pack_type} 包: {data.get('file_name') or 'pack.zip'}\n")
            self._json({"ok": True, "message": message})
        else:
            self._json({"ok": False, "error": err})

    @register_route("/api/packs/delete")
    def _route_packs_delete(self, data):
        # 危险操作二次确认
        ok, err = check_dangerous_operation("/api/packs/delete", data)
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
        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        pack_type = (data.get("pack_type") or "").strip().lower()
        name = (data.get("name") or "").strip()
        if pack_type not in ("resource", "behavior"):
            self._json({"ok": False, "error": "包类型必须是 resource 或 behavior"})
            return
        if not name:
            self._json({"ok": False, "error": "请指定包名"})
            return
        ok, err = delete_pack(d, pack_type, name)
        if ok:
            _console().append(f"\n[系统] 已删除 {pack_type} 包: {name}\n")
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": err})
