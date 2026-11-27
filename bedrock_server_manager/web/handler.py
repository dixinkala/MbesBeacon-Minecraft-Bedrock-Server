"""
HTTP 请求处理模块：Handler 类、路由注册、认证校验。
"""

import contextlib
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .. import remote
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

# 允许访问本服务的回环主机名（含 IPv6 回环字面量）
_LOCAL_HOSTNAME = frozenset({"127.0.0.1", "localhost", "::1"})

# 请求体上限（字节）：世界/资源包导入走 base64 JSON，正常远小于此值
MAX_BODY_BYTES = 512 * 1024 * 1024

# SSE 控制台流的并发上限。
# 每个 SSE 连接独占一个 HTTP 线程且永不主动结束，无上限时多个标签页
# 或僵尸连接会持续堆积线程。
MAX_CONSOLE_STREAMS = 10
_stream_slots = threading.BoundedSemaphore(MAX_CONSOLE_STREAMS)


def _acquire_stream_slot() -> bool:
    """尝试占用一个 SSE 连接名额。"""
    return _stream_slots.acquire(blocking=False)


def _release_stream_slot() -> None:
    """释放一个 SSE 连接名额。

    用 ``BoundedSemaphore``：重复 release 会抛 ValueError 而不是把配额凭空
    加涨（普通 Semaphore 没有上界，多 release 一次就相当于放宽了并发上限）。
    """
    with contextlib.suppress(ValueError):
        _stream_slots.release()


def _client_ip(handler) -> str:
    """取客户端 IP（缺失时按回环处理，便于本地测试/单实例探测）。"""
    addr = getattr(handler, "client_address", None)
    return addr[0] if addr else "127.0.0.1"


def _is_loopback_client(handler) -> bool:
    return remote.is_loopback_value(_client_ip(handler))


def _login_page_html(server_port, error: str = "") -> str:
    """最小登录页：提交密码换取会话 Cookie。"""
    error_html = f'<p class="err">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MbesBeacon 远程登录</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#0e1116; color:#c9d1d9; font-family:system-ui,"Segoe UI",sans-serif; }}
  .box {{ width:min(360px,90vw); background:#161b22; border:1px solid #30363d; border-radius:12px;
          padding:24px; box-shadow:0 8px 30px rgba(0,0,0,.4); }}
  h1 {{ font-size:18px; margin:0 0 6px; }}
  p  {{ font-size:13px; color:#8b949e; margin:0 0 16px; }}
  input {{ width:100%; box-sizing:border-box; padding:10px 12px; border-radius:8px; border:1px solid #30363d;
           background:#0d1117; color:#c9d1d9; font-size:14px; }}
  button {{ width:100%; margin-top:12px; padding:10px 12px; border:0; border-radius:8px; cursor:pointer;
            background:#238636; color:#fff; font-size:14px; }}
  button:disabled {{ opacity:.6; cursor:default; }}
  .err {{ color:#f85149; }}
  .ok  {{ color:#3fb950; }}
</style>
</head>
<body>
  <form class="box" id="form">
    <h1>MbesBeacon 远程管理</h1>
    <p>请输入访问密码（端口 {server_port}）</p>
    <input type="password" id="pwd" placeholder="访问密码" autocomplete="current-password" autofocus>
    <button type="submit" id="btn">登录</button>
    {error_html}
    <p id="msg"></p>
  </form>
<script>
document.getElementById('form').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const btn = document.getElementById('btn');
  const msg = document.getElementById('msg');
  btn.disabled = true; msg.textContent = '';
  try {{
    const resp = await fetch('/api/auth/login', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ password: document.getElementById('pwd').value }}),
    }});
    const data = await resp.json();
    if (data.ok) {{ location.href = '/'; return; }}
    msg.className = 'err';
    msg.textContent = data.error || '登录失败';
  }} catch (err) {{
    msg.className = 'err';
    msg.textContent = '网络错误：' + err;
  }} finally {{
    btn.disabled = false;
  }}
}});
</script>
</body>
</html>"""


def _redirect(handler, location: str) -> None:
    handler.send_response(302)
    handler.send_header("Location", location)
    # 302 必须显式给 Content-Length: 0：否则 keep-alive 连接上客户端
    # 不知道响应体何时结束，会一直等到超时。
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()


def _handler_server_port(handler):
    """返回 handler 所属服务器实际监听的端口（无法取得时返回 None）。"""
    try:
        return handler.server.server_port
    except Exception as e:
        safe_log_exception("handler", f"_handler_server_port 忽略异常: {e}", "warning")
        return None


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
        except Exception as e:
            # 应用日志记录失败不影响 API 响应
            safe_log_exception("handler", f"_log 忽略异常: {e}", "warning")

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _post_data(self):
        """读取并解析 JSON 请求体。

        Returns:
            dict | None: 解析结果；``None`` 表示请求体不合格（超限或
            Content-Length 非法），调用方必须返回 4xx 并断开连接 ——
            不能像早期那样用 ``{}`` 冒充"解析成功"：
            未读的 body 会留在 socket 上，被同一 keep-alive 连接的下一个
            请求当成请求行解析（501 协议错位）；上层路由还会把空 dict
            当成"用户没填确认词"，给出完全误导的提示。
        """
        raw_length = self.headers.get("Content-Length") or 0
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            return None
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            safe_log_exception("handler", f"_post_data 忽略异常: {e}", "warning")
            return {}

    # ---- 路由处理方法 ----
    def do_GET(self):
        """GET 请求处理：使用路由表分发，保留静态文件和 SSE 流的特殊处理。"""

        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        # Host 头必须指向允许的地址，否则视为 DNS rebinding 攻击
        if not self._check_host():
            self._json({"ok": False, "error": "请求来源不合法，已拒绝"}, code=403)
            return
        # 远程登录页与登录状态端点：不需要认证（但受限速与 Host 校验）
        if path == "/login":
            self._serve_login()
            return
        if path == "/api/auth/status":
            self._json(
                {
                    "ok": True,
                    "remote_enabled": remote.enabled(),
                    "authenticated": self._check_auth(),
                    "loopback": _is_loopback_client(self),
                }
            )
            return
        # 放行静态首页和健康检查端点，其余 API 均需认证
        # （/ 与 /index.html 是同一个页面的两个入口：回环页面下发 token，
        #   远程页面依赖会话 Cookie）
        if path not in ("/", "/index.html", "/api/health") and not self._check_auth():
            self._json({"ok": False, "error": "请求来源不合法，已拒绝"}, code=403)
            return
        # GET 速率限制（重端点配额更严：导出世界、阻塞式玩家探测等）。
        # 早期实现只对 POST 限流，这些端点可被反复打满 CPU/磁盘。
        if path not in ("/", "/index.html", "/api/health"):
            client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
            allowed, retry_after = check_rate_limit(path, client_ip, method="GET")
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
        # SSE 流：控制台实时输出
        if path == "/api/console/stream":
            if not _acquire_stream_slot():
                self._json({"ok": False, "error": "控制台连接数已达上限，请关闭多余页面"}, code=429)
                return
            try:
                self._serve_console_stream(q)
            finally:
                _release_stream_slot()
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

        try:
            since = int(q.get("since", ["0"])[0])
        except (TypeError, ValueError):
            since = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        console = AppContext.instance().console

        def _write_console(text, new_idx, reset=False):
            payload = _json.dumps(
                {"lines": text, "count": new_idx, "running": server_running(), "reset": reset}, ensure_ascii=False
            )
            self.wfile.write(f"id: {new_idx}\ndata: {payload}\n\n".encode())
            self.wfile.flush()

        try:
            idx = max(0, since)
            # 客户端游标已落后于保留窗口：让其清空重建，避免日志缺段
            reset = idx > 0 and console.dropped_since(idx)
            text, idx = console.read_since(idx)
            if text or reset:
                _write_console(text, idx, reset=reset)
            while True:
                # 在等待前判断本次是否已经落后于保留窗口（期间被截断也要通知前端重建）
                dropped = console.dropped_since(idx)
                text, new_idx = console.wait_for_new(idx, timeout=1.0)
                if text:
                    idx = new_idx
                    _write_console(text, idx, reset=dropped)
                elif new_idx != idx:
                    idx = new_idx
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端断开连接，SSE 流结束
            pass
        except Exception as e:
            # 其他异常同样结束 SSE 流
            safe_log_exception("handler", f"_serve_console_stream 忽略异常: {e}", "warning")

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
                safe_log_exception("handler", f"_shutdown_later 忽略异常: {e}", "warning")
                self._log(f"停止定时任务调度器失败: {e}")

            # 1. 停止服务器进程
            try:
                server_proc = ctx.server_proc
                if server_proc and server_proc.running:
                    self._log("正在停止服务器进程...")
                    server_proc.stop(wait=8)
            except Exception as e:
                safe_log_exception("handler", f"_shutdown_later 忽略异常: {e}", "warning")
                self._log(f"停止服务器进程失败: {e}")

            # 2. 停止系统托盘
            try:
                tray = ctx.tray
                if tray:
                    self._log("正在停止系统托盘...")
                    tray.stop()
            except Exception as e:
                safe_log_exception("handler", f"_shutdown_later 忽略异常: {e}", "warning")
                self._log(f"停止系统托盘失败: {e}")

            # 3. 关闭 HTTP 服务器
            try:
                httpd = ctx.httpd
                if httpd:
                    self._log("正在关闭 HTTP 服务器...")
                    httpd.shutdown()
            except Exception as e:
                safe_log_exception("handler", f"_shutdown_later 忽略异常: {e}", "warning")
                self._log(f"关闭 HTTP 服务器失败: {e}")

            self._log("程序退出完成")

            # 4. 强制退出程序（确保所有线程都被终止）
            os._exit(0)

        except Exception as e:
            safe_log_exception("handler", f"关闭程序失败: {e}", "error")
            # 即使出错也要强制退出
            os._exit(1)

    def _check_host(self):
        """校验 Host 头只指向本机允许的地址。

        防御 DNS rebinding：攻击者把自己的域名解析到 127.0.0.1 后，
        浏览器会以该域名同源访问本服务；若只校验 Origin/Referer 而不校验 Host，
        攻击页面即可通过免认证的 ``GET /`` 读取页面里的 API token，进而控制全部接口。

        远程管理启用后放行范围扩大为"本机网卡 IP + 本机主机名 + 用户配置的主机名"，
        仍然拒绝任意域名。
        """
        host = self.headers.get("Host", "")
        if not host:
            # HTTP/1.1 要求 Host；缺失视为可疑来源
            return False
        try:
            parsed = urlparse(f"//{host}")
            hostname = (parsed.hostname or "").lower()
            port = parsed.port
        except Exception as e:
            safe_log_exception("handler", f"_check_host 忽略异常: {e}", "warning")
            return False
        if hostname not in remote.allowed_hostnames():
            return False
        server_port = _handler_server_port(self)
        return server_port is None or port is None or port == server_port

    def _check_auth(self):
        """校验请求来源：API token、回环 Origin/Referer，或远程会话 Cookie。

        安全增强：
        1. 优先使用 API Token 认证（常量时间比较），仅注入给回环页面
        2. Origin/Referer 必须匹配本程序实际监听的端口（防止本地其他服务的 XSS 攻击）
        3. 远程会话必须带 HttpOnly Cookie，且（若带 Origin）必须同源
        """

        token = self.headers.get("X-API-Token", "")
        expected = AppContext.instance().api_token
        # secrets.compare_digest 对 str 只支持 ASCII：请求头里塞非 ASCII 会抛
        # TypeError，被 socketserver 当成未捕获异常直接掐断连接（不给 403）。
        # 先按 ASCII 判定，任何非 ASCII token 一定不匹配。
        if token and expected and token.isascii() and expected.isascii() and secrets.compare_digest(token, expected):
            return True
        server_port = _handler_server_port(self)
        # 校验 Origin
        origin = self.headers.get("Origin", "")
        if origin:
            try:
                parsed = urlparse(origin)
                if parsed.hostname in _LOCAL_HOSTNAME and (server_port is None or parsed.port == server_port):
                    return True
            except Exception as e:
                safe_log_exception("handler", f"校验 Origin 失败: {e}", "warning")
        # 校验 Referer
        referer = self.headers.get("Referer", "")
        if referer:
            try:
                parsed = urlparse(referer)
                if parsed.hostname in _LOCAL_HOSTNAME and (server_port is None or parsed.port == server_port):
                    return True
            except Exception as e:
                safe_log_exception("handler", f"校验 Referer 失败: {e}", "warning")
        # 远程会话：Cookie 里的会话 token 有效即通过；带 Origin 时必须同源（防 CSRF）
        if remote.enabled():
            session_token = remote.parse_cookie(self.headers.get("Cookie", ""))
            if session_token and remote.sessions.validate(session_token):
                if origin:
                    try:
                        if urlparse(origin).netloc != self.headers.get("Host", ""):
                            return False
                    except Exception:
                        return False
                return True
        return False

    # ---- 远程登录 ----

    def _serve_login(self):
        """返回登录页；已登录则直接回首页。"""
        if remote.enabled() and self._check_auth():
            _redirect(self, "/")
            return
        self._send(200, _login_page_html(_handler_server_port(self)), "text/html; charset=utf-8")

    def _handle_login(self):
        """处理登录请求：限速 -> 校验密码 -> 下发会话 Cookie。"""
        ip = _client_ip(self)
        wait = remote.login_limiter.retry_after(ip)
        if wait > 0:
            self._json(
                {"ok": False, "error": f"尝试过于频繁，请 {int(wait) + 1} 秒后再试", "retry_after": round(wait, 1)},
                code=429,
            )
            return
        data = self._post_data() or {}
        password = data.get("password") if isinstance(data, dict) else None
        if not isinstance(password, str) or not password:
            self._json({"ok": False, "error": "请输入密码"}, code=400)
            return
        stored = AppContext.instance().settings.get("remote_password_hash", "")
        if not remote.verify_password(password, stored):
            locked = remote.login_limiter.record_failure(ip)
            remote.audit("REMOTE_LOGIN_FAIL", f"ip={ip}")
            message = "密码错误"
            if locked:
                message += f"，已锁定 {int(locked)} 秒"
            self._json({"ok": False, "error": message}, code=401)
            return
        remote.login_limiter.record_success(ip)
        token = remote.sessions.create(ip)
        remote.audit("REMOTE_LOGIN_OK", f"ip={ip}")
        payload = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Set-Cookie", remote.session_cookie(token))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _handle_logout(self):
        """注销当前会话。"""
        token = remote.parse_cookie(self.headers.get("Cookie", ""))
        if token:
            remote.sessions.revoke(token)
            remote.audit("REMOTE_LOGOUT", f"ip={_client_ip(self)}")
        payload = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Set-Cookie", remote.clear_cookie())
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        """POST 请求处理：使用路由表分发。"""

        parsed = urlparse(self.path)
        path = parsed.path
        if not self._check_host():
            self._json({"ok": False, "error": "请求来源不合法，已拒绝"}, code=403)
            return
        # 登录/注销不需要已有会话（登录本身受限速保护）
        if path == "/api/auth/login":
            self._handle_login()
            return
        if path == "/api/auth/logout":
            self._handle_logout()
            return
        if not self._check_auth():
            self._json({"ok": False, "error": "请求来源不合法，已拒绝"}, code=403)
            return
        # API 速率限制
        client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
        allowed, retry_after = check_rate_limit(path, client_ip, method="POST")
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
        if data is None:
            # 请求体超限或 Content-Length 非法：必须返回 413/400 并断开连接。
            # 早期这里用 {} 冒充"解析成功"，于是：
            # 1) 未读的 body 留在 socket 上，被同一 keep-alive 连接的下一个请求
            #    当成请求行解析（501 协议错位）；
            # 2) 路由层把空 dict 当成"用户没填确认词"，返回"请输入 IMPORT 确认"
            #    这种完全误导的提示（用户其实已经确认过，只是文件太大）。
            self.close_connection = True
            self._json(
                {
                    "ok": False,
                    "error": f"请求体过大或格式不正确（上限 {MAX_BODY_BYTES // (1024 * 1024)} MB）",
                },
                code=413,
            )
            return
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
