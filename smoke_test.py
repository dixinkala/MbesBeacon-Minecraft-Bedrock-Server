"""
Windows 冒烟测试：在真实进程中启动 Web 服务并验证 /api/status。

由 CI 的 smoke job 调用（windows-latest），验证打包核心链路在真实
Windows 环境可用：Web 服务可启动、认证可通过、状态接口返回正常。
不依赖 pytest（独立可执行脚本）。

用法: python smoke_test.py
"""

import json
import shutil
import socket
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer


def _free_port() -> int:
    """获取一个空闲的本地端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    """启动 Web 服务并做冒烟验证，成功返回 0。"""
    from bedrock_server_manager import settings
    from bedrock_server_manager.app_context import AppContext
    from bedrock_server_manager.web.handler import Handler

    # 隔离环境：临时 server_dir
    tmpdir = tempfile.mkdtemp(prefix="mbes_smoke_")
    old_server_dir = settings.get("server_dir", "")
    settings["server_dir"] = tmpdir

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status")
        req.add_header("X-Api-Token", AppContext.instance().api_token)
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if resp.status != 200:
            print(f"[SMOKE FAIL] /api/status 状态码 {resp.status}")
            return 1
        if not isinstance(body, dict) or "installed" not in body:
            print(f"[SMOKE FAIL] /api/status 响应结构异常: {body}")
            return 1
        print(f"[SMOKE OK] /api/status 200 installed={body.get('installed')}")
        return 0
    except Exception as e:
        print(f"[SMOKE FAIL] {e}")
        return 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        settings["server_dir"] = old_server_dir
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
