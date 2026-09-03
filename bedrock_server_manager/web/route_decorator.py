"""
路由装饰器模块。
单独定义 register_route 装饰器，避免 handler.py 与 routes 包之间的循环导入。
支持 GET 和 POST 路由注册。
"""


def register_route(path, method="POST"):
    """装饰器：注册路由处理方法到 Handler._get_routes 或 _post_routes。

    Args:
        path: 路由路径，如 "/api/status"
        method: HTTP 方法，"GET" 或 "POST"，默认 "POST"
    """

    def decorator(method_func):
        method_func._route_path = path
        method_func._route_method = method.upper()
        return method_func

    return decorator


def register_get_route(path):
    """便捷装饰器：注册 GET 路由。"""
    return register_route(path, method="GET")


def register_post_route(path):
    """便捷装饰器：注册 POST 路由。"""
    return register_route(path, method="POST")


def init_routes(cls):
    """扫描类中所有带 _route_path 的方法，注册到 _get_routes 和 _post_routes。"""
    if not hasattr(cls, "_get_routes"):
        cls._get_routes = {}
    if not hasattr(cls, "_post_routes"):
        cls._post_routes = {}
    for name in dir(cls):
        attr = getattr(cls, name, None)
        if callable(attr) and hasattr(attr, "_route_path"):
            route_method = getattr(attr, "_route_method", "POST")
            if route_method == "GET":
                cls._get_routes[attr._route_path] = name
            else:
                cls._post_routes[attr._route_path] = name
