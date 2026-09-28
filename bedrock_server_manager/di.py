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
from collections.abc import Callable
from typing import Any, cast


class DIContainer:
    """简单的依赖注入容器。

    支持单例和瞬态生命周期，与 AppContext 集成。
    线程安全，所有操作都通过锁保护。
    """

    def __init__(self) -> None:
        """初始化依赖注入容器。"""
        self._singletons: dict[str, Any] = {}
        self._factories: dict[str, Callable[[], Any]] = {}
        self._transient: set[str] = set()
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
            self._transient.discard(name)
            # 清除已缓存的单例（如果有）
            self._singletons.pop(name, None)

    def register_transient(self, name: str, factory: Callable[[], Any]) -> None:
        """注册瞬态依赖。

        瞬态依赖每次解析都会创建一个新的实例。

        Args:
            name: 依赖名称
            factory: 创建依赖的工厂函数
        """
        with self._lock:
            self._factories[name] = factory
            self._transient.add(name)
            # 清除已缓存的单例（如果有）
            self._singletons.pop(name, None)

    def register_instance(self, name: str, instance: Any) -> None:
        """直接注册一个实例（单例）。

        Args:
            name: 依赖名称
            instance: 要注册的实例
        """
        with self._lock:
            self._singletons[name] = instance
            self._transient.discard(name)
            self._factories.pop(name, None)

    def resolve(self, name: str) -> Any:
        """解析依赖。

        如果依赖未注册，返回 None。

        Args:
            name: 依赖名称

        Returns:
            解析到的依赖实例
        """
        with self._lock:
            # 瞬态依赖：每次解析都调用工厂
            if name in self._transient:
                factory = self._factories.get(name)
                if factory:
                    return factory()
                return None

            # 已缓存的单例实例
            if name in self._singletons:
                return self._singletons[name]

            # 单例工厂：首次解析时创建并缓存
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
            self._transient.clear()


# 全局依赖注入容器
container = DIContainer()


def inject(*dependency_names: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
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

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
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

        return cast(Callable[..., Any], wrapper)

    return decorator


def initialize_container() -> DIContainer:
    """初始化依赖注入容器，注册常用依赖。

    与 AppContext 集成，注册常用的全局状态依赖。
    全部注册为瞬态：每次解析从 AppContext 获取当前值，
    避免缓存 AppContext 属性快照（reset 或延迟初始化后失效）。

    Returns:
        初始化后的容器
    """
    from .app_context import AppContext

    container.register_transient("app_context", lambda: AppContext.instance())
    container.register_transient("settings", lambda: AppContext.instance().settings)
    container.register_transient("console", lambda: AppContext.instance().console)
    container.register_transient("install_state", lambda: AppContext.instance().install_state)
    container.register_transient("server_proc", lambda: AppContext.instance().server_proc)
    container.register_transient("server_lock", lambda: AppContext.instance().server_lock)
    container.register_transient("httpd", lambda: AppContext.instance().httpd)
    container.register_transient("api_token", lambda: AppContext.instance().api_token)
    container.register_transient("tray", lambda: AppContext.instance().tray)

    return container


# 导出
__all__ = [
    "DIContainer",
    "container",
    "inject",
    "initialize_container",
]
