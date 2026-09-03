"""
应用上下文模块：AppContext，封装全局状态，便于测试和重构。
"""

import os
import time


class AppContext:
    """应用全局状态容器。封装所有模块级全局变量，便于测试和未来重构。
    当前通过全局变量保持向后兼容，新代码可通过 AppContext.instance() 访问。
    提供常用操作的便捷方法，减少直接访问全局变量。"""

    _instance = None

    def __init__(self):
        from ..state import API_TOKEN, DEFAULT_PORT, console, install_state, settings

        self.console = console
        self.install_state = install_state
        self.settings = settings
        self._server_proc = None
        self.httpd = None
        self.api_token = API_TOKEN
        self.port = DEFAULT_PORT
        self.tray = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        """重置单例（用于测试）。"""
        cls._instance = None

    @property
    def server_dir(self):
        from ..state import settings
        from ..utils import resolve_server_dir

        return settings.get("server_dir", "") or resolve_server_dir()

    @property
    def server_running(self):
        from ..server import server_running

        return server_running()

    @property
    def installed(self):
        from ..utils import installed

        return installed()

    @property
    def installed_version(self):
        from ..state import settings

        return settings.get("installed_version", "")

    def get_server_proc(self):
        from ..server import get_server_proc

        return get_server_proc()

    def resolve_dir(self):
        from ..utils import resolve_server_dir

        return resolve_server_dir()

    def start_server(self):
        """启动服务器，返回 (ok, error_msg)。"""
        try:
            p = self.get_server_proc()
            p.server_dir = self.server_dir
            p.start()
            return True, ""
        except Exception as e:
            return False, str(e)

    def stop_server(self, wait=15):
        """停止服务器，返回 (ok, error_msg)。"""
        try:
            p = self.get_server_proc()
            p.stop(wait=wait)
            return True, ""
        except Exception as e:
            return False, str(e)

    def restart_server(self):
        """重启服务器，返回 (ok, error_msg)。"""
        try:
            p = self.get_server_proc()
            p.stop()
            time.sleep(1)
            p.start()
            return True, ""
        except Exception as e:
            return False, str(e)

    def backup_worlds(self, max_backups=5):
        """备份世界存档，返回备份路径。"""
        from ..backup import backup_worlds

        return backup_worlds(self.server_dir, max_backups=max_backups)

    def list_backups(self):
        """列出所有备份，返回列表。"""
        from ..backup import list_backups

        return list_backups(self.server_dir)

    def load_config(self):
        """加载 server.properties，返回字典。"""
        from ..config import load_properties, properties_to_dict
        from ..utils import props_path

        p = props_path()
        if not p or not os.path.exists(p):
            return {}
        return properties_to_dict(load_properties(p))

    def save_config(self, updates):
        """保存配置更新，返回 (ok, error_msg, failed_field)。"""
        from ..config import load_properties, properties_to_dict, save_properties, validate_config_updates
        from ..utils import props_path

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
        save_properties(p, merged)
        return True, "", ""

    def send_command(self, cmd):
        """发送服务器指令，返回 (ok, error_msg, is_dangerous)。"""
        from ..security import validate_command

        ok, err, is_dangerous = validate_command(cmd)
        if not ok:
            return False, err, is_dangerous
        try:
            p = self.get_server_proc()
            p.send(cmd)
            return True, "", is_dangerous
        except Exception as e:
            return False, str(e), is_dangerous

    def log(self, text):
        """向控制台追加日志。"""
        self.console.append(text)


__all__ = ["AppContext"]
