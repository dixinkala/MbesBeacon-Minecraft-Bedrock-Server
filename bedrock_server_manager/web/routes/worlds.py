"""
worlds 相关路由处理方法
"""

import time

from ...app_context import AppContext
from ...security import check_dangerous_operation
from ...utils import resolve_server_dir
from ...worlds import delete_world, duplicate_world, get_active_world_name, list_worlds, rename_world, set_active_world
from ..route_decorator import register_get_route, register_route


def _console():
    """获取控制台缓冲（经 AppContext，避免 state 顶层绑定快照）。"""
    return AppContext.instance().console


class WorldsRoutesMixin:
    """worlds 相关路由处理方法 mixin。"""

    @register_get_route("/api/worlds/list")
    def _route_worlds_list(self, q):

        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        worlds = list_worlds(d)
        self._json({"ok": True, "worlds": worlds, "active": get_active_world_name(d)})

    @register_route("/api/worlds/switch")
    def _route_worlds_switch(self, data):

        d = resolve_server_dir()
        world_name = (data.get("name") or "").strip()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        if not world_name:
            self._json({"ok": False, "error": "请指定世界名"})
            return
        ok, err = set_active_world(d, world_name)
        if ok:
            _console().append(f"\n[系统] 已切换到世界: {world_name}（重启服务器后生效）\n")
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": err})

    @register_route("/api/worlds/rename")
    def _route_worlds_rename(self, data):

        d = resolve_server_dir()
        old_name = (data.get("old_name") or "").strip()
        new_name = (data.get("new_name") or "").strip()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        ok, err = rename_world(d, old_name, new_name)
        if ok:
            _console().append(f"\n[系统] 世界已重命名: {old_name} -> {new_name}\n")
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": err})

    @register_route("/api/worlds/delete")
    def _route_worlds_delete(self, data):

        # 危险操作二次确认
        ok, err = check_dangerous_operation("/api/worlds/delete", data)
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
        world_name = (data.get("name") or "").strip()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        ok, err = delete_world(d, world_name)
        if ok:
            _console().append(f"\n[系统] 世界已删除: {world_name}\n")
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": err})

    @register_route("/api/worlds/duplicate")
    def _route_worlds_duplicate(self, data):

        d = resolve_server_dir()
        source_name = (data.get("source") or "").strip()
        new_name = (data.get("new_name") or "").strip()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        ok, err = duplicate_world(d, source_name, new_name)
        if ok:
            _console().append(f"\n[系统] 世界已复制: {source_name} -> {new_name}\n")
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": err})

        # ---------------- 命令自动补全 ----------------

    @register_get_route("/api/worlds/export")
    def _route_worlds_export(self, q):
        """导出世界存档为 ZIP 文件，提供下载。"""
        import os
        import time
        import zipfile

        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return
        worlds_dir = os.path.join(d, "worlds")
        if not os.path.isdir(worlds_dir):
            self._json({"ok": False, "error": "worlds 目录不存在"})
            return

        # 创建 ZIP 文件到临时目录
        import tempfile

        server_name = os.path.basename(d)
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{server_name}_worlds_{ts}.zip"
        temp_path = os.path.join(tempfile.gettempdir(), filename)

        try:
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for root, _dirs, files in os.walk(worlds_dir):
                    for file in files:
                        full = os.path.join(root, file)
                        arcname = os.path.relpath(full, d)
                        zf.write(full, arcname)

            # 发送文件（P2-4：分块流式发送，避免整文件读入内存——
            # 大世界 ZIP 不再一次性驻留内存，内存峰值降至单块 64KB）
            file_size = os.path.getsize(temp_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename={filename}")
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            with open(temp_path, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

            # 清理临时文件
            import contextlib

            with contextlib.suppress(Exception):
                os.remove(temp_path)
        except Exception as e:
            self._json({"ok": False, "error": f"导出失败: {e}"})

    @register_route("/api/worlds/import")
    def _route_worlds_import(self, data):
        """导入世界存档（base64 编码的 ZIP 文件）。"""
        import base64
        import os
        import tempfile
        import zipfile

        # 危险操作二次确认
        ok, err = check_dangerous_operation("/api/worlds/import", data)
        if not ok:
            self._json(
                {
                    "ok": False,
                    "error": err,
                    "need_confirm": True,
                    "confirm_key": "confirm_import",
                    "confirm_value": "IMPORT",
                }
            )
            return

        d = resolve_server_dir()
        if not d:
            self._json({"ok": False, "error": "未设置服务器目录"})
            return

        file_data = data.get("file_data") or ""
        file_name = data.get("file_name") or "worlds.zip"
        if not file_data:
            self._json({"ok": False, "error": "未提供文件数据"})
            return

        try:
            # 解码 base64
            zip_data = base64.b64decode(file_data)

            # 保存到临时文件
            temp_path = os.path.join(tempfile.gettempdir(), f"import_{os.path.basename(file_name)}")
            with open(temp_path, "wb") as f:
                f.write(zip_data)

            # 验证 ZIP 文件
            if not zipfile.is_zipfile(temp_path):
                os.remove(temp_path)
                self._json({"ok": False, "error": "文件不是有效的 ZIP 格式"})
                return

            # 备份当前 worlds 目录
            worlds_dir = os.path.join(d, "worlds")
            if os.path.isdir(worlds_dir):
                import shutil

                backup_dir = os.path.join(d, f"worlds_backup_{int(time.time())}")
                shutil.copytree(worlds_dir, backup_dir)
                _console().append(f"\n[系统] 导入存档前已备份当前 worlds 到: {backup_dir}\n")

            # 解压 ZIP 文件（先做严格路径校验，防止路径遍历）
            with zipfile.ZipFile(temp_path, "r") as zf:
                for name in zf.namelist():
                    # 规范化成员路径：统一分隔符后逐段检查
                    norm = name.replace("\\", "/")
                    if norm.startswith("/") or ":" in norm:
                        os.remove(temp_path)
                        self._json({"ok": False, "error": f"ZIP 文件包含非法路径: {name}"})
                        return
                    parts = [p for p in norm.split("/") if p not in ("", ".")]
                    if ".." in parts:
                        os.remove(temp_path)
                        self._json({"ok": False, "error": f"ZIP 文件包含非法路径: {name}"})
                        return
                zf.extractall(d)

            # 清理临时文件
            import contextlib

            with contextlib.suppress(Exception):
                os.remove(temp_path)

            _console().append(f"\n[系统] 世界存档已导入: {file_name}\n")
            self._json({"ok": True, "message": "导入成功"})
        except Exception as e:
            self._json({"ok": False, "error": f"导入失败: {e}"})
