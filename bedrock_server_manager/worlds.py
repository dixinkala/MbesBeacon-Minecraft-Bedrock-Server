"""
多世界管理模块：列出、切换、备份、重命名、删除服务器中的世界。
"""

import contextlib
import os
import shutil
import time

from .app_logger import safe_log_exception
from .config import backup_config_history


def list_worlds(server_dir: str) -> list[dict[str, any]]:
    """列出服务器目录下的所有世界。

    Args:
        server_dir: 服务器目录路径

    Returns:
        list: 世界信息列表，每个元素包含 name, path, size, modified, is_active
    """
    if not server_dir or not os.path.isdir(server_dir):
        return []
    worlds_dir = os.path.join(server_dir, "worlds")
    if not os.path.isdir(worlds_dir):
        return []

    # 获取当前活动世界名
    active_world = get_active_world_name(server_dir)

    worlds = []
    try:
        for name in os.listdir(worlds_dir):
            world_path = os.path.join(worlds_dir, name)
            if not os.path.isdir(world_path):
                continue
            # 计算世界大小
            total_size = 0
            for root, _dirs, files in os.walk(world_path):
                for f in files:
                    with contextlib.suppress(OSError):
                        total_size += os.path.getsize(os.path.join(root, f))
            # 获取修改时间
            try:
                modified = os.path.getmtime(world_path)
            except OSError:
                modified = 0

            worlds.append(
                {
                    "name": name,
                    "path": world_path,
                    "size_bytes": total_size,
                    "size_mb": round(total_size / (1024 * 1024), 2),
                    "modified": modified,
                    "modified_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(modified)) if modified else "",
                    "is_active": name == active_world,
                }
            )
    except Exception as e:
        safe_log_exception("worlds", f"操作失败: {e}", "warning")

    # 按修改时间排序，最新的在前
    worlds.sort(key=lambda w: w["modified"], reverse=True)
    return worlds


def get_active_world_name(server_dir: str) -> str:
    """获取当前活动世界名（从 server.properties 读取 level-name）。

    Args:
        server_dir: 服务器目录路径

    Returns:
        str: 当前活动世界名，默认 "Bedrock level"
    """
    if not server_dir:
        return "Bedrock level"
    props_path = os.path.join(server_dir, "server.properties")
    if not os.path.isfile(props_path):
        return "Bedrock level"
    try:
        with open(props_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("level-name="):
                    return line.split("=", 1)[1].strip()
    except Exception as e:
        safe_log_exception("worlds", f"操作失败: {e}", "warning")
    return "Bedrock level"


def set_active_world(server_dir: str, world_name: str) -> tuple[bool, str]:
    """设置当前活动世界（修改 server.properties 中的 level-name）。

    Args:
        server_dir: 服务器目录路径
        world_name: 要设置的世界名

    Returns:
        tuple: (ok, error_message)
    """
    if not server_dir or not os.path.isdir(server_dir):
        return False, "服务器目录不存在"
    if not world_name or not world_name.strip():
        return False, "世界名不能为空"
    world_name = world_name.strip()

    # 检查世界是否存在
    world_path = os.path.join(server_dir, "worlds", world_name)
    if not os.path.isdir(world_path):
        return False, f"世界 '{world_name}' 不存在"

    props_path = os.path.join(server_dir, "server.properties")
    if not os.path.isfile(props_path):
        return False, "未找到 server.properties"

    try:
        # 备份配置

        backup_config_history(server_dir)

        # 读取并修改配置
        lines = []
        found = False
        with open(props_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip().startswith("level-name="):
                    lines.append(f"level-name={world_name}\n")
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append(f"level-name={world_name}\n")

        with open(props_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return True, ""
    except Exception as e:
        return False, f"设置活动世界失败: {str(e)}"


def rename_world(server_dir: str, old_name: str, new_name: str) -> tuple[bool, str]:
    """重命名世界。

    Args:
        server_dir: 服务器目录路径
        old_name: 原世界名
        new_name: 新世界名

    Returns:
        tuple: (ok, error_message)
    """
    if not server_dir or not os.path.isdir(server_dir):
        return False, "服务器目录不存在"
    if not old_name or not new_name:
        return False, "世界名不能为空"
    old_name = old_name.strip()
    new_name = new_name.strip()

    # 检查世界名合法性
    if any(c in new_name for c in '\\/:*?"<>|'):
        return False, "世界名包含非法字符"

    old_path = os.path.join(server_dir, "worlds", old_name)
    new_path = os.path.join(server_dir, "worlds", new_name)

    if not os.path.isdir(old_path):
        return False, f"世界 '{old_name}' 不存在"
    if os.path.exists(new_path):
        return False, f"世界 '{new_name}' 已存在"

    # 若是活动世界，先记下需更新配置（重命名成功后目录已存在再改配置）
    active_world = get_active_world_name(server_dir)
    is_active = active_world == old_name

    try:
        os.rename(old_path, new_path)
    except Exception as e:
        return False, f"重命名世界失败: {str(e)}"

    if is_active:
        # 此时新目录已存在，更新 server.properties 的 level-name
        ok, err = set_active_world(server_dir, new_name)
        if not ok:
            # 配置更新失败时回滚目录重命名，保持原状
            with contextlib.suppress(Exception):
                os.rename(new_path, old_path)
            return False, err
    return True, ""


def delete_world(server_dir: str, world_name: str) -> tuple[bool, str]:
    """删除世界（不可逆操作）。

    Args:
        server_dir: 服务器目录路径
        world_name: 要删除的世界名

    Returns:
        tuple: (ok, error_message)
    """
    if not server_dir or not os.path.isdir(server_dir):
        return False, "服务器目录不存在"
    if not world_name or not world_name.strip():
        return False, "世界名不能为空"
    world_name = world_name.strip()

    world_path = os.path.join(server_dir, "worlds", world_name)
    if not os.path.isdir(world_path):
        return False, f"世界 '{world_name}' 不存在"

    # 不允许删除活动世界
    active_world = get_active_world_name(server_dir)
    if active_world == world_name:
        return False, "不能删除当前活动世界，请先切换到其他世界"

    try:
        shutil.rmtree(world_path)
        return True, ""
    except Exception as e:
        return False, f"删除世界失败: {str(e)}"


def duplicate_world(server_dir: str, source_name: str, new_name: str) -> tuple[bool, str]:
    """复制世界（创建副本）。

    Args:
        server_dir: 服务器目录路径
        source_name: 源世界名
        new_name: 新世界名

    Returns:
        tuple: (ok, error_message)
    """
    if not server_dir or not os.path.isdir(server_dir):
        return False, "服务器目录不存在"
    if not source_name or not new_name:
        return False, "世界名不能为空"
    source_name = source_name.strip()
    new_name = new_name.strip()

    if any(c in new_name for c in '\\/:*?"<>|'):
        return False, "新世界名包含非法字符"

    source_path = os.path.join(server_dir, "worlds", source_name)
    new_path = os.path.join(server_dir, "worlds", new_name)

    if not os.path.isdir(source_path):
        return False, f"源世界 '{source_name}' 不存在"
    if os.path.exists(new_path):
        return False, f"世界 '{new_name}' 已存在"

    try:
        shutil.copytree(source_path, new_path)
        return True, ""
    except Exception as e:
        return False, f"复制世界失败: {str(e)}"


def get_world_info(server_dir: str, world_name: str) -> dict[str, any] | None:
    """获取单个世界的详细信息。

    Args:
        server_dir: 服务器目录路径
        world_name: 世界名

    Returns:
        dict: 世界信息，不存在返回 None
    """
    worlds = list_worlds(server_dir)
    for w in worlds:
        if w["name"] == world_name:
            return w
    return None


__all__ = [
    "list_worlds",
    "get_active_world_name",
    "set_active_world",
    "rename_world",
    "delete_world",
    "duplicate_world",
    "get_world_info",
]
