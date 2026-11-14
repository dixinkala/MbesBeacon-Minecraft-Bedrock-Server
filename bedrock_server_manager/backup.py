"""
备份模块：世界备份、备份列表、恢复备份、删除服务器。
"""

import contextlib
import os
import re
import shutil
import time
import zipfile

from .app_context import AppContext
from .app_logger import safe_log_exception
from .server import get_server_proc
from .utils import is_dangerous_target, is_path_within, resolve_server_dir, tr_msg


def _get_console():
    """获取 console 全局变量（通过 AppContext）。"""
    return AppContext.instance().console


def _get_server_proc():
    """获取 get_server_proc 函数。"""
    return get_server_proc


# 默认保留的备份数量（UI 与文档均以 10 为准；早期底层默认 5 与界面显示不一致）
DEFAULT_MAX_BACKUPS = 10
# 允许的最大保留数量（防止误填超大值把磁盘占满）
MAX_MAX_BACKUPS = 100


def backup_worlds_zip(dir_path: str, max_backups: int = DEFAULT_MAX_BACKUPS) -> str | None:
    """创建ZIP压缩备份，返回备份路径。

    先写入 ``.part`` 临时文件，成功后再原子替换为目标名，
    避免压缩中途失败留下被当作合法备份的半成品 ZIP。
    """
    tmp_path = None
    try:
        worlds_dir = os.path.join(dir_path, "worlds")
        if not os.path.isdir(worlds_dir):
            return None
        backup_root = get_backup_root(dir_path)
        os.makedirs(backup_root, exist_ok=True)
        server_name = os.path.basename(dir_path)
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_root, f"{server_name}_worlds_{ts}.zip")
        tmp_path = backup_path + ".part"
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for root, _dirs, files in os.walk(worlds_dir):
                for file in files:
                    full = os.path.join(root, file)
                    arcname = os.path.relpath(full, dir_path)
                    zf.write(full, arcname)
        os.replace(tmp_path, backup_path)
        tmp_path = None
        # 清理旧备份（只清理本服务器的备份）
        _cleanup_old_backups(backup_root, max_backups, server_name)
        return backup_path
    except Exception as e:
        safe_log_exception("backup", f"backup_worlds_zip 忽略异常: {e}", "warning")
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            with contextlib.suppress(OSError):
                os.remove(tmp_path)


# 备份名里的时间戳格式（与 time.strftime("%Y%m%d_%H%M%S") 一致）。
# 用它区分"真的是备份"与"名字里恰好含 _worlds_ 的用户目录/文件"。
_BACKUP_TS_RE = re.compile(r"^\d{8}_\d{6}$")


def _cleanup_old_backups(backup_root, max_backups, server_name=None):
    """清理旧备份，保留最近 N 个。

    Args:
        backup_root: 备份根目录
        max_backups: 保留数量
        server_name: 仅清理该服务器的备份；为 None 时只处理本目录下形如
            ``<名字>_worlds_<时间戳>`` 的备份，绝不触碰其他文件。

    Notes:
        早期实现以 ``f.endswith(".zip") or "_worlds_" in f`` 判定备份，
        会把共享备份根目录下用户自己的任意 .zip 一并删除；此处改为
        严格匹配 ``<服务器名>_worlds_<14位时间戳>``。

        注意：时间戳校验必须**无条件**执行。只在 ``server_name is None`` 时校验
        会让生产路径下（永远带 server_name）任何 ``<服务器名>_worlds_notes``
        这类用户目录都被当成备份删除——而目录型备份的删除是 ``rmtree``，
        等于删掉用户数据，还会占用保留名额把真实备份挤出去。
    """
    try:
        if not os.path.isdir(backup_root):
            return
        prefix = f"{server_name}_worlds_" if server_name else None
        candidates = []
        for name in os.listdir(backup_root):
            if prefix is not None:
                if not name.startswith(prefix):
                    continue
            elif "_worlds_" not in name:
                continue
            full = os.path.join(backup_root, name)
            # 只认两种形态：<...>_worlds_<YYYYmmdd_HHMMSS>.zip 与 <...>_worlds_<YYYYmmdd_HHMMSS>/
            stem = name[:-4] if name.endswith(".zip") else name
            suffix = stem.split("_worlds_")[-1]
            if not _BACKUP_TS_RE.match(suffix):
                continue
            if name.endswith(".zip"):
                if not os.path.isfile(full):
                    continue
            elif not os.path.isdir(full):
                continue
            candidates.append(full)
        candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in candidates[max_backups:]:
            if os.path.isdir(old):
                shutil.rmtree(old, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    os.remove(old)
    except Exception as e:
        safe_log_exception("backup", f"操作失败: {e}", "warning")


def backup_worlds(dir_path: str, max_backups: int = DEFAULT_MAX_BACKUPS) -> str:
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
        console.append(
            tr_msg("\n[系统] 已自动备份世界存档到: %s\n", "\n[System] World save auto-backed up to: %s\n", backup_dir)
        )
        return backup_dir
    except Exception as e:
        safe_log_exception("backup", f"backup_worlds 忽略异常: {e}", "warning")
        console.append(tr_msg("\n[系统] 世界存档备份失败: %s\n", "\n[System] World save backup failed: %s\n", e))
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
        return False, tr_msg("备份不存在", "Backup does not exist")
    is_zip = backup_path.lower().endswith(".zip") and os.path.isfile(backup_path)
    if not (os.path.isdir(backup_path) or is_zip):
        return False, tr_msg("备份目录不存在", "Backup directory does not exist")
    if not server_dir or not os.path.isdir(server_dir):
        return False, tr_msg("服务器目录不存在", "Server directory does not exist")
    # 安全校验：备份路径必须在备份根目录内（规范化 + 真实路径，防符号链接逃逸）
    backup_root = get_backup_root(server_dir)
    if not backup_root or not is_path_within(backup_path, backup_root):
        return False, tr_msg(
            "非法路径，只能恢复备份目录内的存档", "Invalid path: can only restore backups inside the backup directory"
        )
    backup_abs = os.path.normpath(os.path.abspath(backup_path))
    root_abs = os.path.normpath(os.path.abspath(backup_root))
    if os.path.normcase(backup_abs) == os.path.normcase(root_abs):
        return False, tr_msg("非法路径，不能恢复备份根目录本身", "Invalid path: cannot restore the backup root itself")
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
                return False, tr_msg(
                    "备份中未找到 worlds 目录，无法恢复", "No worlds directory found in backup, cannot restore"
                )
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
        console.append(
            tr_msg(
                "\n[系统] 世界存档已从备份恢复: %s\n",
                "\n[System] World save restored from backup: %s\n",
                os.path.basename(backup_path),
            )
        )
        return True, ""
    except Exception as e:
        # 恢复失败，尝试回滚。
        #
        # 顺序很重要：先把原存档 rename 到**新名字**（先腾出 worlds 名字但不删数据），
        # 再删掉 copytree 留下的半成品，最后把原存档 rename 回 worlds。
        # 早期实现直接 rmtree(worlds) 再 rename，一旦 rename 失败（文件被占用、
        # 权限、磁盘错误）就同时失去新旧数据；也曾经要求 "worlds 不存在" 才回滚，
        # 等于从不回滚，旧数据滞留在 worlds_restore_tmp_* 后被下次成功恢复的
        # 兜底清理永久删除。
        rollback_note = ""
        try:
            source_tmp = None
            if tmp_dir and os.path.isdir(tmp_dir):
                source_tmp = tmp_dir
            else:
                stale = sorted(d for d in os.listdir(server_dir) if d.startswith("worlds_restore_tmp_"))
                if stale:
                    source_tmp = os.path.join(server_dir, stale[0])

            if source_tmp:
                # 1) 把原存档挪到安全名字（此时半成品还占着 worlds，但数据仍完整）
                stash = source_tmp + "_keep"
                with contextlib.suppress(OSError):
                    if os.path.isdir(stash):
                        shutil.rmtree(stash, ignore_errors=True)
                os.rename(source_tmp, stash)
                # 2) 删掉半成品
                if os.path.isdir(worlds_dir):
                    shutil.rmtree(worlds_dir, ignore_errors=True)
                # 3) 把原存档换回原名
                os.rename(stash, worlds_dir)
        except Exception as e2:
            # 回滚失败时明确告知用户原存档位置，避免被当成垃圾清理掉
            rollback_note = tr_msg(
                "\n注意：自动回滚失败（%s）。原世界存档仍保留在服务器目录下的 "
                "worlds_restore_tmp_* 中，请手动改名为 worlds 后再启动服务器。",
                "\nNote: automatic rollback failed (%s). Your original world is still in a "
                "worlds_restore_tmp_* folder; rename it back to 'worlds' before starting the server.",
                str(e2),
            )
            safe_log_exception("backup", f"恢复失败后回滚也失败: {e2}", "error")
        return False, tr_msg("恢复失败: %s", "Restore failed: %s", str(e)) + rollback_note
    finally:
        # 清理解压临时目录
        if extract_dir and os.path.isdir(extract_dir):
            import shutil as _sh

            _sh.rmtree(extract_dir, ignore_errors=True)


def delete_backup(backup_path: str, server_dir: str | None = None) -> tuple[bool, str]:
    """删除指定备份（支持目录型与 ZIP 文件型）。返回 (ok, error_msg)。

    安全校验：
    1. server_dir 未显式提供时回退到当前管理的服务器目录；两者都没有则直接拒绝
       （早期实现会在 server_dir 为空时跳过边界检查，只剩子串匹配，可删任意目录）
    2. 路径必须在备份根目录内（规范化后的绝对路径边界检查）
    3. 拒绝删除备份根目录本身
    """
    console = _get_console()
    if not backup_path or not (os.path.isdir(backup_path) or os.path.isfile(backup_path)):
        return False, tr_msg("备份不存在", "Backup does not exist")
    if server_dir is None:
        server_dir = resolve_server_dir()
    if not server_dir:
        return False, tr_msg(
            "未设置服务器目录，拒绝删除未知位置的备份",
            "Server directory is not set; refusing to delete a backup at an unknown location",
        )
    backup_root = get_backup_root(server_dir)
    if not backup_root or not is_path_within(backup_path, backup_root):
        return False, tr_msg(
            "非法路径，只能删除备份目录内的存档",
            "Invalid path: can only delete backups inside the backup directory",
        )
    backup_abs = os.path.normpath(os.path.abspath(backup_path))
    root_abs = os.path.normpath(os.path.abspath(backup_root))
    if os.path.normcase(backup_abs) == os.path.normcase(root_abs):
        return False, tr_msg("非法路径，不能删除备份根目录", "Invalid path: cannot delete the backup root itself")
    try:
        if os.path.isdir(backup_path):
            shutil.rmtree(backup_path, ignore_errors=True)
        else:
            os.remove(backup_path)
        console.append(
            tr_msg("\n[系统] 已删除备份: %s\n", "\n[System] Backup deleted: %s\n", os.path.basename(backup_path))
        )
        return True, ""
    except Exception as e:
        safe_log_exception("backup", f"delete_backup 忽略异常: {e}", "warning")
        return False, tr_msg("删除失败: %s", "Delete failed: %s", str(e))


def delete_server(dir_target, mode):
    """删除服务器。mode='server_only' 仅删服务端程序保留世界与配置；'full' 彻底删除整个目录。

    Raises:
        RuntimeError: 目录不存在、mode 非法，或目标为盘符根/用户主目录/系统目录。
    """
    console = _get_console()
    get_server_proc = _get_server_proc()
    if not dir_target or not os.path.isdir(dir_target):
        raise RuntimeError("目录不存在：%s" % (dir_target or "（未设置）"))
    # mode 白名单：任何未知值都不得退化为"整目录删除"
    if mode not in ("server_only", "full"):
        raise RuntimeError(f"无效的删除模式：{mode}（仅支持 server_only / full）")
    if mode == "full" and is_dangerous_target(dir_target):
        raise RuntimeError(f"拒绝删除危险目录：{dir_target}")
    # server_only 会删掉 keep 清单之外的一切（比 full 的破坏面更小，但同样不该
    # 作用在 C:\Windows 这类目录上）；早期只给 full 加了守卫。
    if mode == "server_only" and is_dangerous_target(dir_target):
        raise RuntimeError(f"拒绝在危险目录上执行 server_only 删除：{dir_target}")
    if mode == "full":
        from .constants import SERVER_EXE

        configured = resolve_server_dir()
        is_configured = bool(configured) and os.path.normcase(os.path.abspath(configured)) == os.path.normcase(
            os.path.abspath(dir_target)
        )
        looks_like_server = os.path.isfile(os.path.join(dir_target, SERVER_EXE))
        if not (is_configured or looks_like_server):
            raise RuntimeError(f"目标目录不是服务器目录：{dir_target}")
    # 若正在运行且目录匹配，先停止服务器
    p = get_server_proc()
    if p.running and os.path.normcase(os.path.abspath(p.server_dir)) == os.path.normcase(os.path.abspath(dir_target)):
        console.append("\n[系统] 删除前正在停止服务器...\n")
        p.stop(wait=10)
    if mode == "server_only":
        # 保留用户数据：世界存档、配置文件、权限/黑白名单、资源包，
        # 以及应用自己写在服务器目录下的审计日志/配置历史/计划任务
        # （早期清单漏了这三项，"更新服务器"会把审计与计划任务一并清空）。
        from .config import (
            ALLOWLIST_FILE,
            AUDIT_LOG_NAME,
            BANNED_IPS_FILE,
            BANNED_PLAYERS_FILE,
            CONFIG_HISTORY_DIR,
            PACKS_CONFIG,
            PERMISSIONS_FILE,
            SCHEDULED_TASKS_FILE,
            WHITELIST_FILE,
        )

        keep = {
            "worlds",
            "server.properties",
            ALLOWLIST_FILE,
            PERMISSIONS_FILE,
            BANNED_PLAYERS_FILE,
            BANNED_IPS_FILE,
            WHITELIST_FILE,
            PACKS_CONFIG,
            AUDIT_LOG_NAME,
            CONFIG_HISTORY_DIR,
            SCHEDULED_TASKS_FILE,
            ".scheduler_state.json",
            "server_console.log",
            "release-notes.txt",
            # 安装暂存目录：更新期间正被 do_install 使用，删掉会让安装中途失败
            ".install_staging",
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
            # 控制台日志轮转产生 server_console.1.log 等，一并保留
            if name in keep or name.startswith("server_console."):
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
