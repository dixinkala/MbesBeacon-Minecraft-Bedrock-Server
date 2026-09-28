"""
入口模块：main、_main、open_browser_with_retry。
程序启动入口，负责单实例检测、HTTP服务器启动、系统托盘、浏览器打开。
"""

import contextlib
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

from .app_context import AppContext
from .app_logger import get_app_logger, safe_log_exception
from .app_update import check_app_update_async
from .constants import APP_MARKER, APP_TITLE, APP_VERSION, DEFAULT_PORT, LOGS_DIR
from .server import get_server_proc, server_running
from .state import migrate_legacy_data
from .tray import SystemTray
from .utils import installed, open_browser_with_retry, resolve_server_dir
from .web.handler import Handler


def _main():
    """程序主入口：单实例检测、启动HTTP服务器、系统托盘、浏览器打开。"""
    # 迁移旧版本分散的数据到统一目录（仅首次启动执行）
    try:
        migrated = migrate_legacy_data()
        if migrated:
            get_app_logger().info(f"已迁移旧数据: {', '.join(migrated)}")
    except Exception:
        # 日志记录失败时静默，避免掩盖主流程
        pass

    # 应用初始化状态：port 与初始化标记
    # 说明：api_token / console / install_state / settings 等状态在
    # AppContext.__init__ 与包 __init__ 中已与 state 保持同步，此处无需重复赋值
    with contextlib.suppress(Exception):
        ctx = AppContext.instance()
        ctx.port = DEFAULT_PORT
        ctx.mark_initialized()

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
                        probe = urllib.request.urlopen(f"http://127.0.0.1:{probe_port}/api/status", timeout=1)
                        data = json.loads(probe.read().decode("utf-8", "ignore"))
                        if data.get("app") == APP_MARKER:
                            open_browser_with_retry(f"http://127.0.0.1:{probe_port}/")
                            # 释放当前互斥量并退出
                            if mutex:
                                try:
                                    kernel32.ReleaseMutex(mutex)
                                    kernel32.CloseHandle(mutex)
                                except Exception as e:
                                    with contextlib.suppress(Exception):
                                        get_app_logger().debug(f"main.py 异常: {e}")
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
                with contextlib.suppress(Exception):
                    get_app_logger().info("检测到残留互斥量，已清理并重新创建")
            except Exception as e:
                with contextlib.suppress(Exception):
                    get_app_logger().debug(f"main.py 异常: {e}")
    except Exception as e:
        safe_log_exception("main", f"操作失败: {e}", "warning")

    # 回退方案：探测默认端口
    try:
        probe = urllib.request.urlopen(f"http://127.0.0.1:{DEFAULT_PORT}/api/status", timeout=2)
        data = json.loads(probe.read().decode("utf-8", "ignore"))
        if data.get("app") == APP_MARKER:
            open_browser_with_retry(f"http://127.0.0.1:{DEFAULT_PORT}/")
            return
    except Exception as e:
        safe_log_exception("main", f"操作失败: {e}", "warning")

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

    AppContext.instance().httpd = httpd

    # 异步检查软件更新（不阻塞启动，结果缓存供前端使用）
    with contextlib.suppress(Exception):
        check_app_update_async(force=False, ignore_ssl=AppContext.instance().settings.get("ignore_ssl", False))

    url = f"http://127.0.0.1:{port}/"
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
            except Exception as e:
                safe_log_exception("main", f"操作失败: {e}", "warning")

        def _tray_exit():
            """系统托盘退出：停止服务器、停止托盘、关闭 HTTP 服务器、退出程序。"""
            import os

            try:
                # 1. 停止服务器进程
                try:
                    from .server import get_server_proc, server_running

                    if server_running():
                        p = get_server_proc()
                        if p:
                            p.stop(wait=8)
                except Exception as e:
                    print(f"停止服务器进程失败: {e}")

                # 2. 停止系统托盘
                try:
                    if tray:
                        tray.stop()
                except Exception as e:
                    print(f"停止系统托盘失败: {e}")

                # 3. 关闭 HTTP 服务器
                try:
                    if httpd:
                        httpd.shutdown()
                except Exception as e:
                    print(f"关闭 HTTP 服务器失败: {e}")

                # 4. 强制退出程序
                os._exit(0)

            except Exception as e:
                safe_log_exception("main", f"关闭程序失败: {e}", "error")
                os._exit(1)

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
        # 用户按 Ctrl+C 退出，走 finally 清理流程
        pass
    finally:
        if tray:
            with contextlib.suppress(Exception):
                tray.stop()
        try:
            p = get_server_proc()
            if p.running:
                p.stop(wait=8)
        except Exception as e:
            safe_log_exception("main", f"操作失败: {e}", "warning")
        # 释放单实例互斥量
        if mutex:
            try:
                kernel32 = ctypes.windll.kernel32
                kernel32.ReleaseMutex(mutex)
                kernel32.CloseHandle(mutex)
            except Exception as e:
                safe_log_exception("main", f"操作失败: {e}", "warning")


def main():
    """程序入口，带崩溃日志和错误提示。"""

    try:
        _main()
    except Exception:
        import traceback

        log_path = os.path.join(LOGS_DIR, "crash.log")
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n===== {} =====\n".format(time.strftime("%Y-%m-%d %H:%M:%S")))
                f.write(f"App: {APP_TITLE} v{APP_VERSION}\n")
                f.write("Python: {}\n".format(sys.version.replace("\n", " ")))
                f.write("OS: Windows (win32 / nt)\n")  # 仅支持 Windows 平台
                f.write("Server dir: %s\n" % (AppContext.instance().settings.get("server_dir", "") or "(未设置)"))
                f.write(f"Installed: {installed()}\n")
                f.write(f"Server running: {server_running()}\n")
                f.write("---- Traceback ----\n")
                f.write(traceback.format_exc())
        except Exception as e:
            safe_log_exception("main", f"操作失败: {e}", "warning")
        try:
            err_msg = traceback.format_exc()
            err_lines = [line for line in err_msg.strip().split("\n") if line.strip()]
            err_summary = err_lines[-1] if err_lines else "未知错误"
            ctypes.windll.user32.MessageBoxW(
                0,
                f"程序启动失败：{err_summary}\n\n崩溃日志已保存至：\n{log_path}\n\n请将日志反馈给开发者。",
                "Minecraft 基岩版服务器管理器 - 启动失败",
                0x10,
            )
        except Exception as e:
            safe_log_exception("main", f"操作失败: {e}", "warning")
        raise


if __name__ == "__main__":
    main()
