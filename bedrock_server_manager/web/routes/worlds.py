"""
worlds 相关路由处理方法
"""

import contextlib
import os
import shutil
import time
import uuid

from ...app_context import AppContext
from ...app_logger import safe_log_exception
from ...security import audit_log, check_dangerous_operation
from ...utils import resolve_server_dir
from ...worlds import delete_world, duplicate_world, get_active_world_name, list_worlds, rename_world, set_active_world
from ..route_decorator import register_get_route, register_route

# 世界导入 ZIP 解压后的总大小上限（防 zip bomb）
MAX_WORLD_IMPORT_BYTES = 4 * 1024 * 1024 * 1024  # 4GB


def _looks_like_world(path: str) -> bool:
    """判断目录是否像一个世界（含 level.dat 或 db 目录）。"""
    return os.path.isdir(path) and (
        os.path.isfile(os.path.join(path, "level.dat")) or os.path.isdir(os.path.join(path, "db"))
    )


def _locate_imported_worlds(extract_root: str) -> str | None:
    """在解压目录中定位 world 内容所在目录。

    支持四种常见打包方式：
    1. ``worlds/<世界名>/...``          → 返回 ``<extract_root>/worlds``
    2. ``<世界名>/level.dat``（单层）   → 返回 ``extract_root``
    3. ``level.dat`` 位于根部            → 把根部内容包装成一个世界目录后返回其父目录
    4. 根部直接是多个世界目录 + 说明文件 → 返回 ``extract_root``

    第 4 种是用户手工打包最常见的样子（把几个世界文件夹 + 一个 readme 一起压缩），
    早期只认前三种，会直接报"未找到 worlds 目录"。

    Returns:
        str | None: 直接包含世界子目录的目录路径；无法识别返回 None
    """
    worlds = os.path.join(extract_root, "worlds")
    if os.path.isdir(worlds):
        return worlds
    if _looks_like_world(extract_root):
        return _wrap_as_world(extract_root)
    entries = [n for n in os.listdir(extract_root) if not n.startswith(".")]
    if len(entries) == 1:
        only = os.path.join(extract_root, entries[0])
        if _looks_like_world(only):
            return extract_root
        return None
    # 根部有多个条目：只要有任意一个"像世界"的目录，就把根当作世界容器
    if any(
        os.path.isdir(os.path.join(extract_root, n)) and _looks_like_world(os.path.join(extract_root, n))
        for n in entries
    ):
        return extract_root
    return None


def _wrap_as_world(extract_root: str) -> str:
    """把"根部即世界内容"的导入包装成 ``<parent>/<世界名>`` 结构。

    原始内容通过 ``os.replace`` 直接改名，不做复制（世界存档可能很大）。

    Returns:
        str: 包含单个世界目录的父目录
    """

    parent = os.path.join(os.path.dirname(extract_root), f"mbes_world_parent_{uuid.uuid4().hex[:8]}")
    os.makedirs(parent, exist_ok=True)
    dst = os.path.join(parent, "ImportedWorld")
    try:
        os.replace(extract_root, dst)
    except OSError:
        import shutil

        shutil.copytree(extract_root, dst)
    return parent


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
            audit_log("WORLD_SWITCH", world_name, d)
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
            audit_log("WORLD_RENAME", f"{old_name} -> {new_name}", d)
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
            audit_log("WORLD_DELETE", world_name, d)
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
            audit_log("WORLD_DUPLICATE", f"{source_name} -> {new_name}", d)
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
            safe_log_exception("worlds", f"_route_worlds_export 忽略异常: {e}", "warning")
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

            # 一次导入一个会话目录：zip、解压目录、_wrap_as_world 生成的父目录
            # 全都在它里面，结束时一次 rmtree 即可。
            # 早期把 zip 与解压目录都放在系统 TEMP 根下，并用
            # "遍历 TEMP 删除所有 mbes_world_parent_*" 来收尾 —— 两个并发导入
            # 会互删对方的目录，失败分支还会把 zip 永久留在 TEMP（每个可达 384MB）。
            session_dir = tempfile.mkdtemp(prefix="mbes_world_import_")
            temp_path = os.path.join(session_dir, f"import_{os.path.basename(file_name)}")
            extract_root = os.path.join(session_dir, "extracted")
            try:
                with open(temp_path, "wb") as f:
                    f.write(zip_data)

                # 验证 ZIP 文件
                if not zipfile.is_zipfile(temp_path):
                    self._json({"ok": False, "error": "文件不是有效的 ZIP 格式"})
                    return

                # 解压到独立的临时目录，而不是直接解到服务器根目录。
                # 直接 extractall(d) 会覆盖 server.properties / allowlist.json /
                # bedrock_server.exe 等关键文件（ZIP 里只要带上同名文件即可）。
                os.makedirs(extract_root, exist_ok=True)
                # 严格路径校验 + 解压体积上限（防 zip bomb）
                total_bytes = 0
                with zipfile.ZipFile(temp_path, "r") as zf:
                    for info in zf.infolist():
                        name = info.filename
                        # 规范化成员路径：统一分隔符后逐段检查
                        norm = name.replace("\\", "/")
                        if norm.startswith("/") or ":" in norm:
                            self._json({"ok": False, "error": f"ZIP 文件包含非法路径: {name}"})
                            return
                        parts = [p for p in norm.split("/") if p not in ("", ".")]
                        if ".." in parts:
                            self._json({"ok": False, "error": f"ZIP 文件包含非法路径: {name}"})
                            return
                        total_bytes += max(0, info.file_size)
                        if total_bytes > MAX_WORLD_IMPORT_BYTES:
                            self._json(
                                {
                                    "ok": False,
                                    "error": f"ZIP 解压后超过上限（{MAX_WORLD_IMPORT_BYTES // (1024 * 1024)} MB）",
                                }
                            )
                            return
                    zf.extractall(extract_root)

                # 定位 worlds 目录：允许 ZIP 根部直接是 worlds/ 或某个世界目录
                source_worlds = _locate_imported_worlds(extract_root)
                if source_worlds is None:
                    self._json(
                        {
                            "ok": False,
                            "error": "ZIP 中未找到世界存档（需包含 worlds/ 目录，或直接包含世界文件夹/level.dat）",
                        }
                    )
                    return

                # 备份当前 worlds 目录（名字带毫秒+随机后缀：按秒命名时
                # 同一秒内连续导入两次会因目录已存在而直接失败）
                worlds_dir = os.path.join(d, "worlds")
                if os.path.isdir(worlds_dir):
                    backup_dir = os.path.join(
                        d, f"worlds_backup_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
                    )
                    shutil.copytree(worlds_dir, backup_dir)
                    _console().append(f"\n[系统] 导入存档前已备份当前 worlds 到: {backup_dir}\n")

                os.makedirs(worlds_dir, exist_ok=True)

                # 只把识别出的世界目录合并进 worlds/，不触碰服务器根目录其他文件
                for entry in sorted(os.listdir(source_worlds)):
                    src = os.path.join(source_worlds, entry)
                    dst = os.path.join(worlds_dir, entry)
                    if os.path.isdir(src):
                        # 容器目录里可能夹带 readme 之类：只合并"像世界"的目录
                        if _looks_like_world(src):
                            if os.path.isdir(dst):
                                shutil.rmtree(dst, ignore_errors=True)
                            shutil.copytree(src, dst)
                    elif _looks_like_world(source_worlds):
                        shutil.copy2(src, dst)
            finally:
                # 会话目录里包含 zip、解压树、_wrap_as_world 的父目录，
                # 无论成功失败都整目录清掉（不会误伤其他并发导入）
                with contextlib.suppress(Exception):
                    shutil.rmtree(session_dir, ignore_errors=True)

            _console().append(f"\n[系统] 世界存档已导入: {file_name}\n")
            self._json({"ok": True, "message": "导入成功"})
        except Exception as e:
            safe_log_exception("worlds", f"_route_worlds_import 忽略异常: {e}", "warning")
            self._json({"ok": False, "error": f"导入失败: {e}"})
