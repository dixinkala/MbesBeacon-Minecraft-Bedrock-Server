"""
入口模块：main、_main、open_browser_with_retry。
程序启动入口，负责单实例检测、HTTP服务器启动、系统托盘、浏览器打开。
"""

import contextlib
from .app_logger import get_app_logger
import ctypes
import io
import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer

from .utils import open_browser_with_retry


def _main():
    """程序主入口：单实例检测、启动HTTP服务器、系统托盘、浏览器打开。"""
    from .server import get_server_proc
    from .state import (
        APP_MARKER,
        DEFAULT_PORT,
    )
    from .tray import SystemTray
    from .utils import resolve_server_dir
    from .web.handler import Handler

    # windowed(exe无控制台)模式下 stdout/stderr 为 None，需重定向
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()

    # 单实例检测：命名互斥量
    mutex = None
    try:
        mutex_name = "Global\\MbesBeacon_Mutex"
        kernel32 = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, False, mutex_name)
        last_error = kernel32.GetLastError()
        if last_error == 183:  # ERROR_ALREADY_EXISTS
            # 检测到已有互斥量，先等待让首个实例完成 HTTP 服务器绑定（防止竞态条件）
            import time
            time.sleep(1.5)
            
            # 多次探测是否真有实例在运行（每次间隔 0.5 秒，共 3 次）
            found_running = False
            for retry in range(3):
                for probe_port in range(DEFAULT_PORT, DEFAULT_PORT + 20):
                    try:
                        probe = urllib.request.urlopen("http://127.0.0.1:%d/api/status" % probe_port, timeout=1)
                        data = json.loads(probe.read().decode("utf-8", "ignore"))
                        if data.get("app") == APP_MARKER:
                            open_browser_with_retry("http://127.0.0.1:%d/" % probe_port)
                            # 释放当前互斥量并退出
                            if mutex:
                                try:
                                    kernel32.ReleaseMutex(mutex)
                                    kernel32.CloseHandle(mutex)
                                except Exception as e:
                                    try:
                                        get_app_logger().debug(f"main.py 异常: {e}")
                                    except Exception:
                                        pass
                            return
                    except Exception:
                        continue
                if not found_running and retry < 2:
                    time.sleep(0.5)
            
            # 多次探测后仍未找到运行中的实例，说明是残留互斥量，释放旧的并继续启动
            try:
                # 关闭当前互斥量句柄（旧互斥量会在所有句柄关闭后被系统回收）
                kernel32.CloseHandle(mutex)
                mutex = None
                # 重新创建互斥量
                mutex = kernel32.CreateMutexW(None, False, mutex_name)
                # 记录日志
                try:
                    get_app_logger().info("检测到残留互斥量，已清理并重新创建")
                except Exception:
                    pass
            except Exception as e:
                try:
                    get_app_logger().debug(f"main.py 异常: {e}")
                except Exception:
                    pass
    except Exception:  # 已添加异常记录
        try:
            get_app_logger().debug(f"main.py 异常: {e}")
        except Exception:
            pass

    # 回退方案：探测默认端口
    try:
        probe = urllib.request.urlopen("http://127.0.0.1:%d/api/status" % DEFAULT_PORT, timeout=2)
        data = json.loads(probe.read().decode("utf-8", "ignore"))
        if data.get("app") == APP_MARKER:
            open_browser_with_retry("http://127.0.0.1:%d/" % DEFAULT_PORT)
            return
    except Exception:  # 已添加异常记录
        try:
            get_app_logger().debug(f"main.py 异常: {e}")
        except Exception:
            pass

    # 绑定端口
    httpd = None
    port = DEFAULT_PORT
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        print("无法绑定端口，程序退出")
        return

    # 保存到全局状态
    from . import state

    state.httpd = httpd

    url = "http://127.0.0.1:%d/" % port
    print(f"管理界面: {url}")

    # 系统托盘
    tray = None
    try:

        def _tray_toggle():
            try:
                p = get_server_proc()
                if p.running:
                    p.stop()
                else:
                    p.server_dir = resolve_server_dir()
                    p.start()
            except Exception:  # 已添加异常记录
                try:
                    get_app_logger().debug(f"main.py 异常: {e}")
                except Exception:
                    pass

        def _tray_exit():
            try:
                if httpd:
                    httpd.shutdown()
            except Exception:  # 已添加异常记录
                try:
                    get_app_logger().debug(f"main.py 异常: {e}")
                except Exception:
                    pass

        tray = SystemTray(url, on_open=lambda: webbrowser.open(url), on_toggle=_tray_toggle, on_exit=_tray_exit)
        tray.start()
        threading.Timer(0.8, lambda: open_browser_with_retry(url)).start()
        threading.Timer(
            1.5, lambda: tray.notify("Minecraft 基岩版服务器管理器", f"管理界面已启动：{url}") if tray else None
        ).start()
    except Exception:
        tray = None
        threading.Timer(0.8, lambda: open_browser_with_retry(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tray:
            with contextlib.suppress(Exception):
                tray.stop()
        try:
            p = get_server_proc()
            if p.running:
                p.stop(wait=8)
        except Exception:  # 已添加异常记录
            try:
                get_app_logger().debug(f"main.py 异常: {e}")
            except Exception:
                pass
        # 释放单实例互斥量
        if mutex:
            try:
                kernel32 = ctypes.windll.kernel32
                kernel32.ReleaseMutex(mutex)
                kernel32.CloseHandle(mutex)
            except Exception:  # 已添加异常记录
                try:
                    get_app_logger().debug(f"main.py 异常: {e}")
                except Exception:
                    pass


def main():
    """程序入口，带崩溃日志和错误提示。"""
    from .server import server_running
    from .state import APP_TITLE, APP_VERSION, settings
    from .utils import installed

    try:
        _main()
    except Exception as e:
        import traceback

        log_path = os.path.join(os.path.expanduser("~"), ".bedrock_server_manager_crash.log")
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n===== {} =====\n".format(time.strftime("%Y-%m-%d %H:%M:%S")))
                f.write(f"App: {APP_TITLE} v{APP_VERSION}\n")
                f.write("Python: {}\n".format(sys.version.replace("\n", " ")))
                f.write("OS: Windows (win32 / nt)\n")  # 仅支持 Windows 平台
                f.write("Server dir: %s\n" % (settings.get("server_dir", "") or "(未设置)"))
                f.write(f"Installed: {installed()}\n")
                f.write(f"Server running: {server_running()}\n")
                f.write("---- Traceback ----\n")
                f.write(traceback.format_exc())
        except Exception:  # 已添加异常记录
            try:
                get_app_logger().debug(f"main.py 异常: {e}")
            except Exception:
                pass
        try:
            err_msg = traceback.format_exc()
            err_lines = [l for l in err_msg.strip().split("\n") if l.strip()]
            err_summary = err_lines[-1] if err_lines else "未知错误"
            ctypes.windll.user32.MessageBoxW(
                0,
                f"程序启动失败：{err_summary}\n\n崩溃日志已保存至：\n{log_path}\n\n请将日志反馈给开发者。",
                "Minecraft 基岩版服务器管理器 - 启动失败",
                0x10,
            )
        except Exception:  # 已添加异常记录
            try:
                get_app_logger().debug(f"main.py 异常: {e}")
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
