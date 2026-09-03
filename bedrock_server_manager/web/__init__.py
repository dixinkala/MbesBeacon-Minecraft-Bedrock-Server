"""
Web 子包：HTTP 处理和应用上下文。
"""

from .app import AppContext
from .handler import Handler
from .route_decorator import register_route

__all__ = ["Handler", "register_route", "AppContext"]
