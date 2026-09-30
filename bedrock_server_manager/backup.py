"""
备份模块：世界备份、备份列表、恢复备份、删除服务器。
"""

import contextlib
import os
import shutil
import time
import zipfile

from .app_context import AppContext
from .app_logger import safe_log_exception
from .server import get_server_proc
from .utils import resolve_server_dir


def _get_console():
    """获取 console 全局变量（通过 AppContext）。"""
    return AppContext.instance().console


def _get_server_proc():
    """获取 get_server_proc 函数。"""
    return get_server_proc


def backup_worlds_zip(dir_path: str, max_backups: int = 5) -> str | None:
    """创建ZIP压缩备份，返回备份路径。"""
    try:
        worlds_dir = os.path.join(dir_path, "worlds")
        if not os.path.isdir(worlds_dir):
            return None
        backup_root = get_backup_root(dir_path)
        os.makedirs(backup_root, exist_ok=True)
        server_name = os.path.basename(dir_path)
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_root, f"{server_name}_worlds_{ts}.zip")
        with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for root, _dirs, files in os.walk(worlds_dir):
                for file in files:
                    full = os.path.join(root, file)
                    arcname = os.path.relpath(full, dir_path)
                    zf.write(full, arcname)
        # 清理旧备份
        _cleanup_old_backups(backup_root, max_backups)
        return backup_path
    except Exception:
        return None


def _cleanup_old_backups(backup_root, max_backups):
    """清理旧备份，保留最近N个。"""
    try:
        if not os.path.isdir(backup_root):
            return
        backups = sorted(
            [f for f in os.listdir(backup_root) if f.endswith(".zip") or "_worlds_" in f],
            key=lambda x: os.path.getmtime(os.path.join(backup_root, x)),
            reverse=True,
        )
        for old in backups[max_backups:]:
            with contextlib.suppress(Exception):
                os.remove(os.path.join(backup_root, old))
    except Exception as e:
        safe_log_exception("backup", f"操作失败: {e}", "warning")


def backup_worlds(dir_path: str, max_backups: int = 5) -> str:
    """自动备份服务器 worlds 文件夹到同级目录的 _worlds_backups 文件夹。
    返回备份路径，失败或无 worlds 时返回空字符串。只保留最近 max_backups 个备份。"""
    console = _get_console()
    if not dir_path or not os.path.isdir(dir_path):
        return ""
    worlds_dir = os.path.join(dir_path, "worlds")
    if not os.path.isdir(worlds_dir):
        return ""
    try:
        backup_root = os.path.join(os.path.dirname(dir_path), "_worlds_backups")
        os.makedirs(backup_root, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        server_name = os.path.basename(dir_path)
        backup_dir = os.path.join(backup_root, f"{server_name}_worlds_{timestamp}")
        shutil.copytree(worlds_dir, backup_dir)
        # 清理旧备份，只保留最近 max_backups 个
        try:
            backups = sorted(
                [
                    os.path.join(backup_root, d)
                    for d in os.listdir(backup_root)
                    if d.startswith(server_name + "_worlds_") and os.path.isdir(os.path.join(backup_root, d))
                ],
                key=os.path.getmtime,
                reverse=True,
            )
            for old in backups[max_backups:]:
                shutil.rmtree(old, ignore_errors=True)
        except Exception as e:
            safe_log_exception("backup", f"操作失败: {e}", "warning")
        console.append(f"\n[系统] 已自动备份世界存档到: {backup_dir}\n")
        return backup_dir
    except Exception as e:
        console.append(f"\n[系统] 世界存档备份失败: {e}\n")
        return ""


def get_backup_root(dir_path: str) -> str:
    """获取服务器的备份根目录路径。"""
    if not dir_path:
        return ""
    return os.path.join(os.path.dirname(dir_path), "_worlds_backups")


def list_backups(dir_path: str) -> list[dict]:
    """列出指定服务器的所有世界备份，返回 [{name, path, size, time}, ...]，按时间倒序。"""
    if not dir_path or not os.path.isdir(dir_path):
        return []
    backup_root = get_backup_root(dir_path)
    if not os.path.isdir(backup_root):
        return []
    server_name = os.path.basename(dir_path)
    prefix = server_name + "_worlds_"
    result = []
    try:
        for name in os.listdir(backup_root):
            if not name.startswith(prefix):
                continue
            full = os.path.join(backup_root, name)
            # 支持目录型备份和 ZIP 压缩备份
            is_dir = os.path.isdir(full)
            is_zip = name.endswith(".zip") and os.path.isfile(full)
            if not (is_dir or is_zip):
                continue
            # 计算大小
            total_size = 0
            if is_dir:
                try:
                    for root, _dirs, files in os.walk(full):
                        for f in files:
                            with contextlib.suppress(Exception):
                                total_size += os.path.getsize(os.path.join(root, f))
                except Exception as e:
                    safe_log_exception("backup", f"操作失败: {e}", "warning")
            else:
                # ZIP 文件直接获取文件大小
                try:
                    total_size = os.path.getsize(full)
                except Exception as e:
                    safe_log_exception("backup", f"操作失败: {e}", "warning")
            mtime = os.path.getmtime(full)
            result.append(
                {
                    "name": name,
                    "path": full,
                    "size": total_size,
                    "size_mb": round(total_size / (1024 * 1024), 2),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                    "timestamp": mtime,
                    "type": "directory" if is_dir else "zip",
                }
            )
    except Exception as e:
        safe_log_exception("backup", f"操作失败: {e}", "warning")
    result.sort(key=lambda x: x["timestamp"], reverse=True)
    return result


def restore_backup(backup_path: str, server_dir: str) -> tuple[bool, str]:
    """从备份恢复世界存档。会先停止服务器，备份当前 worlds（如果存在），然后恢复。
    支持目录型备份（_worlds_backups/<name>）与 ZIP 压缩备份（<name>.zip）。
    返回 (ok, error_msg)。"""
    console = _get_console()
    get_server_proc = _get_server_proc()
    if not backup_path or not os.path.exists(backup_path):
        return False, "备份不存在"
    is_zip = backup_path.lower().endswith(".zip") and os.path.isfile(backup_path)
    if not (os.path.isdir(backup_path) or is_zip):
        return False, "备份目录不存在"
    if not server_dir or not os.path.isdir(server_dir):
        return False, "服务器目录不存在"
    # 安全校验：备份路径必须在备份根目录内
    backup_root = get_backup_root(server_dir)
    backup_abs = os.path.normpath(os.path.abspath(backup_path))
    root_abs = os.path.normpath(os.path.abspath(backup_root))
    if not (backup_abs == root_abs or backup_abs.startswith(root_abs + os.sep)):
        return False, "非法路径，只能恢复备份目录内的存档"
    # 额外校验：路径必须包含 _worlds_backups
    if "_worlds_backups" not in backup_abs:
        return False, "非法路径，只能恢复备份目录内的存档"
    # 停止服务器
    try:
        p = get_server_proc()
        if p.running:
            p.stop(wait=10)
    except Exception as e:
        safe_log_exception("backup", f"操作失败: {e}", "warning")
    worlds_dir = os.path.join(server_dir, "worlds")
    tmp_dir = None
    extract_dir = None
    try:
        # ZIP 型备份：先安全解压到临时目录，校验其中包含 worlds 目录
        if is_zip:
            import tempfile

            extract_dir = tempfile.mkdtemp(prefix="mbesbeacon_restore_")
            from .install import extract_zip

            extract_zip(backup_path, extract_dir)
            extracted_worlds = os.path.join(extract_dir, "worlds")
            if not os.path.isdir(extracted_worlds):
                return False, "备份中未找到 worlds 目录，无法恢复"
            source_worlds = extracted_worlds
        else:
            source_worlds = backup_path

        # 如果当前 worlds 存在，先重命名为临时备份
        if os.path.isdir(worlds_dir):
            tmp_dir = worlds_dir + "_restore_tmp_" + str(int(time.time()))
            os.rename(worlds_dir, tmp_dir)
        # 复制备份到 worlds
        shutil.copytree(source_worlds, worlds_dir)
        # 删除本次创建的临时备份
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        # 安全兜底：清理所有残留的临时备份目录
        for d in os.listdir(server_dir):
            if d.startswith("worlds_restore_tmp_"):
                shutil.rmtree(os.path.join(server_dir, d), ignore_errors=True)
        console.append(f"\n[系统] 世界存档已从备份恢复: {os.path.basename(backup_path)}\n")
        return True, ""
    except Exception as e:
        # 恢复失败，尝试回滚
        try:
            if tmp_dir and os.path.isdir(tmp_dir) and not os.path.isdir(worlds_dir):
                os.rename(tmp_dir, worlds_dir)
            else:
                tmp_dirs = [d for d in os.listdir(server_dir) if d.startswith("worlds_restore_tmp_")]
                if tmp_dirs and not os.path.isdir(worlds_dir):
                    os.rename(os.path.join(server_dir, tmp_dirs[0]), worlds_dir)
        except Exception as e:
            safe_log_exception("backup", f"操作失败: {e}", "warning")
        return False, f"恢复失败: {str(e)}"
    finally:
        # 清理解压临时目录
        if extract_dir and os.path.isdir(extract_dir):
            import shutil as _sh

            _sh.rmtree(extract_dir, ignore_errors=True)


def delete_backup(backup_path: str, server_dir: str | None = None) -> tuple[bool, str]:
    """删除指定备份（支持目录型与 ZIP 文件型）。返回 (ok, error_msg)。

    安全校验：
    1. 路径必须包含 _worlds_backups 关键词
    2. 路径必须在备份根目录内（绝对路径边界检查）
    """
    console = _get_console()
    if not backup_path or not (os.path.isdir(backup_path) or os.path.isfile(backup_path)):
        return False, "备份不存在"
    # 安全校验1：必须在 _worlds_backups 目录下
    if "_worlds_backups" not in backup_path:
        return False, "非法路径，只能删除备份目录"
    # 安全校验2：绝对路径边界检查（与 restore_backup 一致）
    try:
        if server_dir is None:
            server_dir = resolve_server_dir()
        if server_dir:
            backup_root = get_backup_root(server_dir)
            backup_abs = os.path.normpath(os.path.abspath(backup_path))
            root_abs = os.path.normpath(os.path.abspath(backup_root))
            if not (backup_abs == root_abs or backup_abs.startswith(root_abs + os.sep)):
                return False, "非法路径，只能删除备份目录内的存档"
    except Exception as e:
        safe_log_exception("backup", f"操作失败: {e}", "warning")
    try:
        if os.path.isdir(backup_path):
            shutil.rmtree(backup_path, ignore_errors=True)
        else:
            os.remove(backup_path)
        console.append(f"\n[系统] 已删除备份: {os.path.basename(backup_path)}\n")
        return True, ""
    except Exception as e:
        return False, f"删除失败: {str(e)}"


def delete_server(dir_target, mode):
    """删除服务器。mode='server_only' 仅删服务端程序保留世界与配置；'full' 彻底删除整个目录。"""
    console = _get_console()
    get_server_proc = _get_server_proc()
    if not dir_target or not os.path.isdir(dir_target):
        raise RuntimeError("目录不存在：%s" % (dir_target or "（未设置）"))
    # 若正在运行且目录匹配，先停止服务器
    p = get_server_proc()
    if p.running and os.path.normcase(os.path.abspath(p.server_dir)) == os.path.normcase(os.path.abspath(dir_target)):
        console.append("\n[系统] 删除前正在停止服务器...\n")
        p.stop(wait=10)
    if mode == "server_only":
        # 保留用户数据：世界存档、配置文件、权限/黑白名单、资源包等
        keep = {
            "worlds",
            "server.properties",
            "allowlist.json",
            "permissions.json",
            "banned-players.json",
            "banned-ips.json",
            "whitelist.json",
            "valid_known_packs.json",
            "release-notes.txt",
            "behavior_packs",
            "resource_packs",
            "definitions",
            "structures",
            "development_behavior_packs",
            "development_resource_packs",
            "development_skin_packs",
            "premium_cache",
            "treatments",
            "skin_packs",
        }
        for name in os.listdir(dir_target):
            if name in keep:
                continue
            full = os.path.join(dir_target, name)
            try:
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
                else:
                    os.remove(full)
            except Exception as e:
                safe_log_exception("backup", f"操作失败: {e}", "warning")
    else:
        shutil.rmtree(dir_target, ignore_errors=True)
    return True
