"""
Web 层便捷上下文：``AppContext``。

历史上这里是一个**独立的单例**，在 ``__init__`` 时把全局 AppContext 的
console / install_state / settings / api_token 快照成自己的属性。这带来两个问题：

1. 快照会过期 —— 全局实例的 token 或 settings 被替换后，Web 层仍用旧值；
2. 两个同名 ``AppContext`` 并存，调用方很容易用错那个。

现在改为**无状态门面**：不再保存任何副本，所有属性都实时读取
``app_context.AppContext.instance()``。对外 API 保持不变，路由代码无需修改。
"""

from __future__ import annotations

import os
import time
from typing import Any

from ..app_context import AppContext as GlobalAppContext
from ..app_logger import safe_log_exception
from ..config import (
    backup_config_history,
    load_properties,
    properties_to_dict,
    save_properties,
    validate_config_updates,
)
from ..security import audit_log
from ..server import get_server_proc
from ..utils import props_path, resolve_server_dir


class AppContext:
    """Web 层门面：所有状态实时转发到全局 ``app_context.AppContext``。

    只提供便捷方法（启动/停止/备份/读写配置/发指令），不持有任何状态副本。
    """

    _instance: AppContext | None = None

    def __init__(self) -> None:
        """门面本身不持有状态；保留构造器以兼容 ``AppContext()`` 的写法。"""

    @classmethod
    def instance(cls) -> AppContext:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """重置单例（用于测试）。"""
        cls._instance = None

    # ---------------- 状态（实时转发，不缓存） ----------------

    @property
    def _ctx(self) -> GlobalAppContext:
        return GlobalAppContext.instance()

    @property
    def console(self) -> Any:
        return self._ctx.console

    @property
    def install_state(self) -> Any:
        return self._ctx.install_state

    @property
    def settings(self) -> dict:
        return self._ctx.settings

    @property
    def api_token(self) -> str:
        return self._ctx.api_token

    @property
    def port(self) -> int:
        return self._ctx.port

    @property
    def httpd(self) -> Any:
        return self._ctx.httpd

    @property
    def tray(self) -> Any:
        return self._ctx.tray

    @property
    def server_dir(self) -> str:
        return self._ctx.settings.get("server_dir", "") or resolve_server_dir()

    def get_server_proc(self):
        return get_server_proc()

    def restart_server(self) -> tuple[bool, str]:
        """重启服务器，返回 (ok, error_msg)。

        必须把当前服务器目录同步给 ServerProcess：切换/安装新服务器后
        ``p.server_dir`` 可能还指着上一个目录，直接 ``start()`` 会去启动
        旧目录里的 exe（或报"未找到 bedrock_server.exe"）。
        """
        try:
            d = self.server_dir
            if not d:
                return False, "未设置服务器目录"
            p = self.get_server_proc()
            p.server_dir = d
            p.stop()
            time.sleep(1)
            p.start()
            return True, ""
        except Exception as e:
            safe_log_exception("app", f"restart_server 忽略异常: {e}", "warning")
            return False, str(e)

    def save_config(self, updates) -> tuple[bool, str, str]:
        """保存配置更新，返回 (ok, error_msg, failed_field)。

        统一封装：校验 → 合并写入 → 配置历史备份 → 审计日志。
        /api/config 路由与内部调用共用，避免重复实现。

        Args:
            updates: 配置更新字典

        Returns:
            tuple: (ok, error_msg, failed_field)
        """
        ok, err, field = validate_config_updates(updates)
        if not ok:
            return False, err, field
        p = props_path()
        if not p:
            return False, "尚未安装服务器", ""
        items = load_properties(p)
        cur = properties_to_dict(items)
        for k, v in updates.items():
            cur[k] = str(v)
        seen = set()
        merged = []
        for k, _v in items:
            if k in cur:
                merged.append((k, cur[k]))
                seen.add(k)
        for k in cur:
            if k not in seen:
                merged.append((k, cur[k]))
        backup_config_history(os.path.dirname(p))
        save_properties(p, merged)
        audit_log("CONFIG_SAVE", ",".join(updates.keys()))
        return True, "", ""
