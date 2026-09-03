"""
依赖注入容器模块：提供简单但功能完整的依赖注入机制。

与 AppContext 集成，支持：
- 注册和解析依赖
- 单例和瞬态生命周期
- 便捷的注入装饰器
- 与现有 AppContext 的无缝集成

使用方式：
    from .di import container, inject

    # 注册依赖
    container.register_singleton("settings", lambda: AppContext.instance().settings)

    # 注入依赖
    @inject("settings", "console")
    def my_function(settings, console):
        settings.get("key")
        console.append("message")

    # 或者手动解析
    settings = container.resolve("settings")
"""

import functools
import inspect
import threading
from typing import Any, Callable, Dict, Optional, Type


class DIContainer:
    """简单的依赖注入容器。

    支持单例和瞬态生命周期，与 AppContext 集成。
    线程安全，所有操作都通过锁保护。
    """

    def __init__(self) -> None:
        """初始化依赖注入容器。"""
        self._singletons: Dict[str, Any] = {}
        self._factories: Dict[str, Callable[[], Any]] = {}
        self._lock = threading.Lock()

    def register_singleton(self, name: str, factory: Callable[[], Any]) -> None:
        """注册单例依赖。

        单例依赖只会在第一次解析时创建，之后每次解析都返回同一个实例。

        Args:
            name: 依赖名称
            factory: 创建依赖的工厂函数
        """
        with self._lock:
            self._factories[name] = factory
            # 清除已缓存的单例（如果有）
            if name in self._singletons:
                del self._singletons[name]

    def register_transient(self, name: str, factory: Callable[[], Any]) -> None:
        """注册瞬态依赖。

        瞬态依赖每次解析都会创建一个新的实例。

        Args:
            name: 依赖名称
            factory: 创建依赖的工厂函数
        """
        with self._lock:
            self._factories[name] = factory
            # 标记为瞬态（使用特殊前缀）
            transient_key = f"__transient__{name}"
            self._singletons[transient_key] = True

    def register_instance(self, name: str, instance: Any) -> None:
        """直接注册一个实例（单例）。

        Args:
            name: 依赖名称
            instance: 要注册的实例
        """
        with self._lock:
            self._singletons[name] = instance

    def resolve(self, name: str) -> Any:
        """解析依赖。

        如果依赖未注册，返回 None。

        Args:
            name: 依赖名称

        Returns:
            解析到的依赖实例
        """
        with self._lock:
            # 检查是否是瞬态依赖
            transient_key = f"__transient__{name}"
            if transient_key in self._singletons:
                factory = self._factories.get(name)
                if factory:
                    return factory()
                return None

            # 检查是否已经有单例实例
            if name in self._singletons:
                return self._singletons[name]

            # 检查是否有工厂函数
            factory = self._factories.get(name)
            if factory:
                instance = factory()
                self._singletons[name] = instance
                return instance

            return None

    def is_registered(self, name: str) -> bool:
        """检查依赖是否已注册。

        Args:
            name: 依赖名称

        Returns:
            是否已注册
        """
        with self._lock:
            return name in self._factories or name in self._singletons

    def clear(self) -> None:
        """清除所有注册的依赖（用于测试）。"""
        with self._lock:
            self._singletons.clear()
            self._factories.clear()


# 全局依赖注入容器
container = DIContainer()


def inject(*dependency_names: str) -> Callable:
    """依赖注入装饰器。

    将指定的依赖作为关键字参数注入到被装饰的函数中。
    如果函数已经提供了对应的关键字参数，则使用函数提供的值。

    Args:
        *dependency_names: 要注入的依赖名称列表

    Returns:
        装饰器函数

    示例:
        @inject("settings", "console")
        def my_function(settings, console, extra_param=None):
            settings.get("key")
            console.append("message")
    """

    def decorator(func: Callable) -> Callable:
        # 获取函数参数签名
        sig = inspect.signature(func)
        param_names = list(sig.parameters.keys())

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # 解析依赖，只注入函数没有提供的依赖
            for name in dependency_names:
                if name not in kwargs:
                    # 检查是否已经通过位置参数提供
                    param_index = param_names.index(name) if name in param_names else -1
                    if param_index >= 0 and param_index < len(args):
                        continue  # 已经通过位置参数提供
                    # 注入依赖
                    dependency = container.resolve(name)
                    if dependency is not None:
                        kwargs[name] = dependency
            return func(*args, **kwargs)

        return wrapper

    return decorator


def initialize_container() -> DIContainer:
    """初始化依赖注入容器，注册常用依赖。

    与 AppContext 集成，注册常用的全局状态依赖。

    Returns:
        初始化后的容器
    """
    from .app_context import AppContext

    # 注册单例依赖（从 AppContext 获取）
    container.register_singleton("app_context", lambda: AppContext.instance())
    container.register_singleton("settings", lambda: AppContext.instance().settings)
    container.register_singleton("console", lambda: AppContext.instance().console)
    container.register_singleton("install_state", lambda: AppContext.instance().install_state)
    container.register_singleton("server_proc", lambda: AppContext.instance().server_proc)
    container.register_singleton("server_lock", lambda: AppContext.instance().server_lock)
    container.register_singleton("httpd", lambda: AppContext.instance().httpd)
    container.register_singleton("api_token", lambda: AppContext.instance().api_token)
    container.register_singleton("tray", lambda: AppContext.instance().tray)

    return container


# 导出
__all__ = [
    "DIContainer",
    "container",
    "inject",
    "initialize_container",
]
