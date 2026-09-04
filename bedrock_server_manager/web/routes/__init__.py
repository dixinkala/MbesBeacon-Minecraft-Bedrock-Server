"""
路由 mixin 类集合。
按功能域拆分 Handler 类的路由处理方法。
"""

from .backups import BackupsRoutesMixin
from .commands import CommandsRoutesMixin
from .config import ConfigRoutesMixin
from .console import ConsoleRoutesMixin
from .get_routes import GetRoutesMixin
from .misc import MiscRoutesMixin
from .players import PlayersRoutesMixin
from .post_extra import PostExtraRoutesMixin
from .server import ServerRoutesMixin
from .worlds import WorldsRoutesMixin

__all__ = [
    "BackupsRoutesMixin",
    "CommandsRoutesMixin",
    "ConfigRoutesMixin",
    "ConsoleRoutesMixin",
    "GetRoutesMixin",
    "MiscRoutesMixin",
    "PlayersRoutesMixin",
    "PostExtraRoutesMixin",
    "ServerRoutesMixin",
    "WorldsRoutesMixin",
]
