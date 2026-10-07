"""
包管理模块：资源包/行为包的列表、启用/禁用、导入、删除。

基岩版专用服务器（BDS）的包系统约定：
- 服务器根目录下 `resource_packs/`、`behavior_packs/` 存放包目录，每个包目录
  根部应包含 `manifest.json`（含 `header.pack_id`（UUID）与 `header.version`（int 数组））。
- 服务器根目录下 `valid_known_packs.json` 记录服务器「已知」的包清单
  （元素形如 `{"pack_id": "...", "version": [1, 0, 0]}`）。
- 各世界目录 `worlds/<世界名>/world_resource_packs.json` /
  `world_behavior_packs.json` 记录该世界「启用」的包清单（元素形如 `{"pack_id", "version"}`）。

启用/禁用实际写入当前活动世界（server.properties 中 level-name 对应世界）的
`world_*_packs.json`，并确保包已登记进 `valid_known_packs.json`。
"""

import base64
import contextlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile

from .app_logger import safe_log_exception
from .security import audit_log
from .utils import resolve_server_dir
from .worlds import get_active_world_name

VALID_KNOWN_PACKS_FILE = "valid_known_packs.json"

# pack_type -> (服务器目录下的包文件夹名, 活动世界内的启用清单文件名)
_PACK_TYPES = {
    "resource": ("resource_packs", "world_resource_packs.json"),
    "behavior": ("behavior_packs", "world_behavior_packs.json"),
}

# 包名安全校验：只允许字母/数字/下划线/点/中划线/空格/括号，长度 1-80
_PACK_NAME_RE = re.compile(r"^[A-Za-z0-9_.\- ()（）\u4e00-\u9fff]+$")

# 导入包文件大小上限（base64 解码后），防止超大文件耗尽内存
_IMPORT_MAX_BYTES = 200 * 1024 * 1024


def _read_json(path: str, default):
    """读取 JSON 文件，缺失/损坏时返回 default。"""
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError) as e:
        safe_log_exception("packs.py", f"读取 JSON 失败: {path} ({e})", "warning")
        return default


def _write_json(path: str, data) -> bool:
    """原子写入 JSON 文件（先写临时文件再替换）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except (OSError, PermissionError, TypeError) as e:
        safe_log_exception("packs.py", f"写入 JSON 失败: {path} ({e})", "warning")
        return False


def _normalize_pack_type(pack_type: str) -> tuple[str, str] | None:
    """校验 pack_type，返回 (包目录名, 世界启用清单文件名)；非法返回 None。"""
    return _PACK_TYPES.get((pack_type or "").strip().lower())


def _validate_pack_name(name: str) -> tuple[bool, str]:
    """校验包目录名：禁止路径分隔符/.. 等危险字符。"""
    name = (name or "").strip()
    if not name:
        return False, "包名不能为空"
    if name in (".", "..") or os.sep in name or (os.altsep and os.altsep in name):
        return False, "包名不合法"
    if not _PACK_NAME_RE.match(name):
        return False, "包名只能包含字母、数字、下划线、点、中划线、空格和括号"
    if len(name) > 80:
        return False, "包名过长"
    return True, ""


def _pack_dir_of(server_dir: str, pack_type: str, name: str) -> str | None:
    """校验并返回包目录绝对路径；任何一步不合法返回 None。"""
    mapped = _normalize_pack_type(pack_type)
    if not mapped:
        return None
    ok, err = _validate_pack_name(name)
    if not ok:
        return None
    pack_root = os.path.join(server_dir, mapped[0])
    target = os.path.abspath(os.path.join(pack_root, name))
    # 路径边界校验：target 必须位于 pack_root 之内
    if os.path.commonpath([os.path.abspath(pack_root), target]) != os.path.abspath(pack_root):
        return None
    return target


def _read_manifest(pack_dir: str) -> dict:
    """读取包目录根部 manifest.json，返回 {pack_id, version, name}；失败返回空 dict。"""
    if not pack_dir or not os.path.isdir(pack_dir):
        return {}
    manifest = _read_json(os.path.join(pack_dir, "manifest.json"), None)
    if not isinstance(manifest, dict):
        return {}
    header = manifest.get("header") or {}
    pack_id = header.get("pack_id") or ""
    version = header.get("version") or []
    if isinstance(version, int):
        version = [version]
    if not isinstance(version, list):
        version = []
    name = header.get("name") or os.path.basename(pack_dir)
    return {"pack_id": str(pack_id), "version": [int(v) for v in version if isinstance(v, int)], "name": str(name)}


def _version_str(version) -> str:
    """把版本数组转成 '1.2.3' 字符串；非法返回 '未知'。"""
    if isinstance(version, list) and version:
        return ".".join(str(v) for v in version)
    return "未知"


def read_valid_known_packs(server_dir: str) -> list[dict]:
    """读取服务器根目录 valid_known_packs.json。"""
    return _read_json(os.path.join(server_dir, VALID_KNOWN_PACKS_FILE), [])


def write_valid_known_packs(server_dir: str, entries: list[dict]) -> bool:
    """写回 valid_known_packs.json（保留非标准字段）。"""
    return _write_json(os.path.join(server_dir, VALID_KNOWN_PACKS_FILE), entries)


def read_world_packs(server_dir: str, world_name: str, pack_type: str) -> list[dict]:
    """读取指定世界指定类型的启用包清单。"""
    mapped = _normalize_pack_type(pack_type)
    if not mapped or not world_name:
        return []
    path = os.path.join(server_dir, "worlds", world_name, mapped[1])
    data = _read_json(path, [])
    return data if isinstance(data, list) else []


def write_world_packs(server_dir: str, world_name: str, pack_type: str, entries: list[dict]) -> bool:
    """写回世界启用包清单。"""
    mapped = _normalize_pack_type(pack_type)
    if not mapped or not world_name:
        return False
    path = os.path.join(server_dir, "worlds", world_name, mapped[1])
    return _write_json(path, entries)


def list_installed_packs(server_dir=None) -> dict:
    """列出已安装的包及其启用状态。

    每个包条目：
    - name: 目录名
    - path: 绝对路径
    - pack_id / version: 来自 manifest.json（缺失时为空/未知）
    - enabled: 是否在当前活动世界的启用清单中
    - known: 是否登记在 valid_known_packs.json
    """
    try:
        d = server_dir or resolve_server_dir()
        if not d:
            return {"resource_packs": [], "behavior_packs": [], "valid_known": [], "active_world": ""}
        active_world = get_active_world_name(d)
        valid_known = read_valid_known_packs(d)
        known_ids = {str(e.get("pack_id", "")).lower() for e in valid_known if isinstance(e, dict)}
        result = {"resource_packs": [], "behavior_packs": [], "valid_known": valid_known, "active_world": active_world}
        for pack_type in ("resource", "behavior"):
            mapped = _PACK_TYPES[pack_type]
            pack_root = os.path.join(d, mapped[0])
            world_packs = read_world_packs(d, active_world, pack_type)
            enabled_keys = {
                (str(e.get("pack_id", "")).lower(), _version_str(e.get("version")))
                for e in world_packs
                if isinstance(e, dict)
            }
            items = []
            if os.path.isdir(pack_root):
                for name in sorted(os.listdir(pack_root)):
                    full = os.path.join(pack_root, name)
                    if not os.path.isdir(full):
                        continue
                    manifest = _read_manifest(full)
                    pack_id = manifest.get("pack_id", "")
                    version = _version_str(manifest.get("version"))
                    items.append(
                        {
                            "name": name,
                            "path": full,
                            "pack_id": pack_id,
                            "version": version,
                            "enabled": bool(pack_id and (pack_id.lower(), version) in enabled_keys),
                            "known": bool(pack_id and pack_id.lower() in known_ids),
                        }
                    )
            result[f"{pack_type}_packs"] = items
        return result
    except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError) as e:
        safe_log_exception("packs.py", f"读取包信息失败: {e}", "warning")
        return {"resource_packs": [], "behavior_packs": [], "valid_known": [], "active_world": ""}


def set_pack_enabled(server_dir: str, pack_type: str, name: str, enabled: bool) -> tuple[bool, str]:
    """启用/禁用指定包（写入活动世界配置，并确保登记进 valid_known_packs.json）。"""
    try:
        d = server_dir or resolve_server_dir()
        if not d:
            return False, "未设置服务器目录"
        pack_dir = _pack_dir_of(d, pack_type, name)
        if not pack_dir or not os.path.isdir(pack_dir):
            return False, "包不存在"
        manifest = _read_manifest(pack_dir)
        pack_id = manifest.get("pack_id", "")
        if not pack_id:
            return False, "包缺少 manifest.json（含 pack_id），无法启用；请确认包文件完整"
        version = manifest.get("version") or []
        version_int = [int(v) for v in version if isinstance(v, int)]

        # 1) 确保登记进 valid_known_packs.json
        valid_known = read_valid_known_packs(d)
        known_ids = {str(e.get("pack_id", "")).lower() for e in valid_known if isinstance(e, dict)}
        if pack_id.lower() not in known_ids:
            entry = {"pack_id": pack_id, "version": version_int}
            if manifest.get("name"):
                entry["module_name"] = manifest["name"]
            valid_known.append(entry)
            if not write_valid_known_packs(d, valid_known):
                return False, "写入 valid_known_packs.json 失败"

        # 2) 修改活动世界启用清单
        active_world = get_active_world_name(d)
        if not active_world:
            return False, "无法确定活动世界（server.properties 缺少 level-name）"
        world_packs = read_world_packs(d, active_world, pack_type)
        if enabled:
            entry = {"pack_id": pack_id, "version": version_int}
            exists = any(
                isinstance(e, dict) and str(e.get("pack_id", "")).lower() == pack_id.lower() for e in world_packs
            )
            if not exists:
                world_packs.append(entry)
        else:
            world_packs = [
                e
                for e in world_packs
                if not (isinstance(e, dict) and str(e.get("pack_id", "")).lower() == pack_id.lower())
            ]
        if not write_world_packs(d, active_world, pack_type, world_packs):
            return False, "写入世界包配置失败"

        audit_log("TOGGLE_PACK", f"{pack_type} {name} -> {'enabled' if enabled else 'disabled'}", d)
        return True, ""
    except Exception as e:
        safe_log_exception("packs.py", f"设置包状态失败: {e}", "error")
        return False, str(e)


def _zip_has_single_root(names: list[str]) -> str | None:
    """若 ZIP 全部成员共享同一个顶层目录，返回该顶层目录名；否则返回 None。"""
    tops = set()
    for name in names:
        norm = name.replace("\\", "/").strip("/")
        if not norm:
            continue
        parts = [p for p in norm.split("/") if p not in ("", ".")]
        if not parts:
            continue
        tops.add(parts[0])
    if len(tops) == 1:
        return next(iter(tops))
    return None


def _validate_zip_paths(names: list[str]) -> str | None:
    """校验 ZIP 成员路径，防止路径遍历；非法时返回错误信息，合法返回 None。"""
    for name in names:
        norm = name.replace("\\", "/")
        if norm.startswith("/") or ":" in norm:
            return f"ZIP 文件包含非法路径: {name}"
        parts = [p for p in norm.split("/") if p not in ("", ".")]
        if ".." in parts:
            return f"ZIP 文件包含非法路径: {name}"
    return None


def import_pack(server_dir: str, pack_type: str, file_data: str, file_name: str) -> tuple[bool, str, str]:
    """导入包（.zip/.mcpack，base64 编码）到服务器对应目录。返回 (ok, error, message)。"""
    try:
        d = server_dir or resolve_server_dir()
        if not d:
            return False, "未设置服务器目录", ""
        mapped = _normalize_pack_type(pack_type)
        if not mapped:
            return False, "包类型必须是 resource 或 behavior", ""
        if not file_data:
            return False, "未提供文件数据", ""

        zip_bytes = base64.b64decode(file_data)
        if len(zip_bytes) > _IMPORT_MAX_BYTES:
            return False, "文件过大（超过 200MB）", ""
        if len(zip_bytes) == 0:
            return False, "文件内容为空", ""

        safe_name = re.sub(r"[^\w.\- ]+", "_", os.path.basename(file_name or "pack.zip")).strip()
        if not safe_name:
            safe_name = "pack.zip"
        temp_path = os.path.join(tempfile.gettempdir(), f"mbes_pack_import_{int(time.time())}_{safe_name}")
        try:
            with open(temp_path, "wb") as f:
                f.write(zip_bytes)
            if not zipfile.is_zipfile(temp_path):
                return False, "文件不是有效的 ZIP 格式", ""

            with zipfile.ZipFile(temp_path, "r") as zf:
                names = zf.namelist()
                err = _validate_zip_paths(names)
                if err:
                    return False, err, ""
                # 解压目标：ZIP 根为单一目录时直接解到包根（保留原目录名）；
                # 否则新建一个以文件名为名的子目录
                pack_root = os.path.join(d, mapped[0])
                os.makedirs(pack_root, exist_ok=True)
                single_root = _zip_has_single_root(names)
                if single_root:
                    zf.extractall(pack_root)
                else:
                    target_dir = os.path.join(pack_root, os.path.splitext(safe_name)[0])
                    os.makedirs(target_dir, exist_ok=True)
                    zf.extractall(target_dir)

            # 解压后应至少找到一个 manifest.json
            found = any(fn.replace("\\", "/").lower().endswith("manifest.json") for fn in names)
            if not found:
                return False, "包内未找到 manifest.json，不是有效的包文件", ""

            audit_log("IMPORT_PACK", f"{pack_type} {safe_name}", d)
            return True, "", "导入成功，新包默认未启用，可在列表中启用（需重启服务器生效）"
        finally:
            with contextlib.suppress(OSError):
                os.remove(temp_path)
    except (OSError, zipfile.BadZipFile, ValueError) as e:
        safe_log_exception("packs.py", f"导入包失败: {e}", "error")
        return False, f"导入失败: {e}", ""


def delete_pack(server_dir: str, pack_type: str, name: str) -> tuple[bool, str]:
    """删除包目录，并同步清理 valid_known_packs.json 与活动世界启用清单。"""
    try:
        d = server_dir or resolve_server_dir()
        if not d:
            return False, "未设置服务器目录"
        pack_dir = _pack_dir_of(d, pack_type, name)
        if not pack_dir:
            return False, "包不存在"
        if not os.path.isdir(pack_dir):
            return False, "包不存在"

        # 记录 pack_id 用于后续清理
        manifest = _read_manifest(pack_dir)
        pack_id = manifest.get("pack_id", "")

        # 1) 删除包目录
        shutil.rmtree(pack_dir, ignore_errors=True)
        if os.path.isdir(pack_dir):
            return False, "删除包目录失败（可能被占用），请先停止服务器"

        # 2) 从 valid_known_packs.json 移除
        if pack_id:
            valid_known = read_valid_known_packs(d)
            filtered = [
                e
                for e in valid_known
                if not (isinstance(e, dict) and str(e.get("pack_id", "")).lower() == pack_id.lower())
            ]
            if len(filtered) != len(valid_known):
                write_valid_known_packs(d, filtered)

            # 3) 从活动世界启用清单移除
            active_world = get_active_world_name(d)
            if active_world:
                world_packs = read_world_packs(d, active_world, pack_type)
                kept = [
                    e
                    for e in world_packs
                    if not (isinstance(e, dict) and str(e.get("pack_id", "")).lower() == pack_id.lower())
                ]
                if len(kept) != len(world_packs):
                    write_world_packs(d, active_world, pack_type, kept)

        audit_log("DELETE_PACK", f"{pack_type} {name}", d)
        return True, ""
    except (OSError, PermissionError) as e:
        safe_log_exception("packs.py", f"删除包失败: {e}", "error")
        return False, f"删除失败: {e}"
