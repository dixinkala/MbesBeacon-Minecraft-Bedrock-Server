"""
HTTP 请求处理模块：Handler 类、路由注册、认证校验。
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .. import (
    console as _console_mod,
    install as _install_mod,
    state,
    utils,
)
from ..app_context import AppContext
from ..app_logger import safe_log_exception
from ..ratelimit import check_rate_limit
from ..server import server_running
from .route_decorator import init_routes
from .routes import (
    BackupsRoutesMixin,
    CommandsRoutesMixin,
    ConfigRoutesMixin,
    ConsoleRoutesMixin,
    GetRoutesMixin,
    MiscRoutesMixin,
    PacksRoutesMixin,
    PlayersRoutesMixin,
    PostExtraRoutesMixin,
    ServerRoutesMixin,
    WorldsRoutesMixin,
)


# 延迟导入全局状态（避免循环导入）
def _g(name):
    """从各模块延迟导入全局变量。"""

    if hasattr(state, name):
        return getattr(state, name)

    if hasattr(utils, name):
        return getattr(utils, name)

    if hasattr(_console_mod, name):
        return getattr(_console_mod, name)

    if hasattr(_install_mod, name):
        return getattr(_install_mod, name)
    raise AttributeError(f"Cannot find {name} in any module")


class Handler(
    BaseHTTPRequestHandler,
    GetRoutesMixin,
    PostExtraRoutesMixin,
    MiscRoutesMixin,
    ConsoleRoutesMixin,
    CommandsRoutesMixin,
    ServerRoutesMixin,
    PlayersRoutesMixin,
    PacksRoutesMixin,
    BackupsRoutesMixin,
    ConfigRoutesMixin,
    WorldsRoutesMixin,
):
    protocol_version = "HTTP/1.1"
    _get_routes = {}
    _post_routes = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _log(self, message, level="info"):
        """Log message."""
        try:
            from ..app_logger import get_app_logger

            get_app_logger().log(level, message)
        except Exception:
            # 应用日志记录失败不影响 API 响应
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _post_data(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---- 路由处理方法 ----
    def do_GET(self):
        """GET 请求处理：使用路由表分发，保留静态文件和 SSE 流的特殊处理。"""

        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        # 放行静态首页和健康检查端点，其余 API 均需认证
        if path not in ("/", "/index.html", "/api/health") and not self._check_auth():
            self._json({"ok": False, "error": "请求来源不合法，已拒绝"}, code=403)
            return
        # SSE 流：控制台实时输出
        if path == "/api/console/stream":
            self._serve_console_stream(q)
            return
        # 路由表分发
        if path in self._get_routes:
            method_name = self._get_routes[path]
            handler = getattr(self, method_name, None)
            if handler:
                handler(q)
                return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def _serve_console_stream(self, q):
        """SSE 流：控制台实时输出。"""
        import json as _json

        since = int(q.get("since", ["0"])[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        idx = since if since > 0 else 0

        def _write_console(text, new_idx, reset=False):
            payload = _json.dumps(
                {"lines": text, "count": new_idx, "running": server_running(), "reset": reset}, ensure_ascii=False
            )
            self.wfile.write(f"id: {new_idx}\ndata: {payload}\n\n".encode())
            self.wfile.flush()

        client_closed = False
        try:
            text, idx = AppContext.instance().console.read_since(idx)
            if text or idx > 0:
                # 控制台被截断（MAX_LINES=2000）导致索引回退时，通知前端清空重建，
                # 避免重放客户端已读过的旧日志
                reset = idx < since if since > 0 else False
                _write_console(text, idx, reset=reset)
            while not client_closed:
                text, new_idx = AppContext.instance().console.wait_for_new(idx, timeout=1.0)
                if text or new_idx != idx:
                    reset = new_idx < idx  # 截断导致索引回退
                    idx = new_idx
                    _write_console(text, idx, reset=reset)
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端断开连接，SSE 流结束
            pass
        except Exception:
            # 其他异常同样结束 SSE 流
            pass

    def _shutdown_later(self):
        """延迟关闭程序：停止服务器、停止托盘、关闭 HTTP 服务器、退出程序。"""
        time.sleep(0.5)
        try:
            from ..app_context import AppContext
            from ..scheduler import stop_scheduler

            ctx = AppContext.instance()

            # 0. 停止定时任务调度器
            try:
                stop_scheduler()
            except Exception as e:
                self._log(f"停止定时任务调度器失败: {e}")

            # 1. 停止服务器进程
            try:
                server_proc = ctx.server_proc
                if server_proc and server_proc.running:
                    self._log("正在停止服务器进程...")
                    server_proc.stop(wait=8)
            except Exception as e:
                self._log(f"停止服务器进程失败: {e}")

            # 2. 停止系统托盘
            try:
                tray = ctx.tray
                if tray:
                    self._log("正在停止系统托盘...")
                    tray.stop()
            except Exception as e:
                self._log(f"停止系统托盘失败: {e}")

            # 3. 关闭 HTTP 服务器
            try:
                httpd = ctx.httpd
                if httpd:
                    self._log("正在关闭 HTTP 服务器...")
                    httpd.shutdown()
            except Exception as e:
                self._log(f"关闭 HTTP 服务器失败: {e}")

            self._log("程序退出完成")

            # 4. 强制退出程序（确保所有线程都被终止）
            os._exit(0)

        except Exception as e:
            safe_log_exception("handler", f"关闭程序失败: {e}", "error")
            # 即使出错也要强制退出
            os._exit(1)

    def _check_auth(self):
        """校验请求来源：API token 或 Origin/Referer 必须匹配本地地址和端口。

        安全增强：
        1. 优先使用 API Token 认证
        2. Origin/Referer 必须匹配本程序实际监听的端口（防止本地其他服务的 XSS 攻击）
        3. 只允许 127.0.0.1 和 localhost，不允许其他本地地址
        """

        token = self.headers.get("X-API-Token", "")
        if token and token == AppContext.instance().api_token:
            return True
        # 获取本程序实际监听的端口
        try:
            server_port = self.server.server_port
        except Exception:
            server_port = None
        # 校验 Origin
        origin = self.headers.get("Origin", "")
        if origin:
            try:
                parsed = urlparse(origin)
                if parsed.hostname in ("127.0.0.1", "localhost") and (
                    server_port is None or parsed.port == server_port
                ):
                    return True
            except Exception as e:
                safe_log_exception("handler", f"操作失败: {e}", "warning")
        # 校验 Referer
        referer = self.headers.get("Referer", "")
        if referer:
            try:
                parsed = urlparse(referer)
                if parsed.hostname in ("127.0.0.1", "localhost") and (
                    server_port is None or parsed.port == server_port
                ):
                    return True
            except Exception as e:
                safe_log_exception("handler", f"操作失败: {e}", "warning")
        return False

    def do_POST(self):
        """POST 请求处理：使用路由表分发。"""

        parsed = urlparse(self.path)
        path = parsed.path
        if not self._check_auth():
            self._json({"ok": False, "error": "请求来源不合法，已拒绝"}, code=403)
            return
        # API 速率限制
        client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
        allowed, retry_after = check_rate_limit(path, client_ip)
        if not allowed:
            self._json(
                {
                    "ok": False,
                    "error": "请求过于频繁，请稍后再试",
                    "retry_after": round(retry_after, 2),
                },
                code=429,
            )
            return
        data = self._post_data()
        # 路由表分发
        if path in self._post_routes:
            method_name = self._post_routes[path]
            handler = getattr(self, method_name, None)
            if handler:
                handler(data)
                return
        self._send(404, "not found", "text/plain; charset=utf-8")


# 初始化路由表（扫描所有 mixin 中的路由方法）
init_routes(Handler)


def get_index_html():
    """获取前端页面 HTML。优先从 web/index.html 加载，失败时回退到内嵌字符串。"""
    import sys

    # 尝试从外部文件加载
    try:
        if getattr(sys, "frozen", False):
            base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
            html_path = os.path.join(base_dir, "web", "index.html")
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            html_path = os.path.join(base_dir, "web", "index.html")
        if os.path.exists(html_path):
            with open(html_path, encoding="utf-8") as f:
                return f.read()
    except Exception as e:
        safe_log_exception("handler", f"操作失败: {e}", "warning")
    # 回退到最小错误页
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Bedrock Server Manager</title></head>
<body><h1>前端资源缺失</h1><p>请重新下载程序或检查 web/index.html 是否存在。</p></body></html>"""
