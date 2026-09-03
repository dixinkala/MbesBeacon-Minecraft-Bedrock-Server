"""
系统托盘模块：SystemTray，使用 Win32 API 实现托盘图标和菜单。
"""

import contextlib
from .app_logger import get_app_logger
import ctypes
import ctypes.wintypes
import os
import sys
import threading
import traceback
import webbrowser


def _log(msg):
    """简单的日志输出，便于调试托盘问题。同时写入文件和控制台。"""
    try:
        line = f"[Tray] {msg}\n"
        # 写入日志文件
        try:
            import os

            log_dir = os.path.join(os.path.expanduser("~"), ".bedrock_server_manager")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, "tray.log")
            # 首次创建文件时添加 UTF-8 BOM，确保 Windows 记事本正确识别编码
            file_exists = os.path.exists(log_file)
            with open(log_file, "a", encoding="utf-8") as f:
                if not file_exists:
                    f.write("\ufeff")  # UTF-8 BOM
                f.write(line)
        except Exception:
            pass
        # 控制台输出（开发模式可见）
        with contextlib.suppress(Exception):
            print(line, end="")
    except Exception:  # 已添加异常记录
        try:
            import sys
            get_app_logger().debug(f"tray.py 异常: {e}")
        except Exception:
            pass


class SystemTray:
    """Windows 系统托盘图标，使用 ctypes 调用 Win32 API 实现。
    托盘菜单：打开管理界面、启动/停止服务器、退出程序。"""

    WM_TRAYICON = 0x0400 + 20
    WM_COMMAND = 0x0111
    ID_OPEN = 1001
    ID_TOGGLE = 1002
    ID_EXIT = 1003

    def __init__(self, url, on_open=None, on_toggle=None, on_exit=None):
        self.url = url
        self.on_open = on_open or self._open_browser_with_retry
        self.on_toggle = on_toggle
        self.on_exit = on_exit
        self._init_tray_state()

    def _open_browser_with_retry(self):
        """打开浏览器，带重试机制和备选方案，确保用户能看到管理界面。"""
        import time
        import os
        import subprocess

        url = self.url
        _log(f"正在打开浏览器: {url}")

        # 方法1: webbrowser.open（带重试）
        for attempt in range(3):
            try:
                if webbrowser.open(url, new=2):
                    _log(f"浏览器打开成功 (尝试 {attempt+1}/3)")
                    return True
            except Exception as e:
                _log(f"webbrowser.open 失败 (尝试 {attempt+1}/3): {e}")
            time.sleep(0.5)

        # 方法2: os.startfile（Windows 专用）
        try:
            os.startfile(url)
            _log("使用 os.startfile 打开浏览器成功")
            return True
        except Exception as e:
            _log(f"os.startfile 失败: {e}")

        # 方法3: subprocess 调用默认浏览器
        try:
            if sys.platform == "win32":
                subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
            else:
                subprocess.Popen(["xdg-open", url])
            _log("使用 subprocess 打开浏览器成功")
            return True
        except Exception as e:
            _log(f"subprocess 打开浏览器失败: {e}")

        _log("所有浏览器打开方法均失败，请手动访问: " + url)
        return False

    def _init_tray_state(self):
        """初始化托盘状态属性。"""
        self._hwnd = None
        self._hicon = None
        self._running = False
        self._thread = None
        self._menu = None
        self._wnd_proc_ref = None
        self._user32 = None
        self._nid = None

    def _load_icon(self):
        """加载应用图标（优先自定义图标，失败回退到默认图标）。"""
        try:
            # 查找自定义图标文件
            icon_path = None
            if getattr(sys, "frozen", False):
                base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
                icon_path = os.path.join(base_dir, "assets", "mbesbeacon_icon.ico")
            else:
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                icon_path = os.path.join(base_dir, "assets", "mbesbeacon_icon.ico")

            if icon_path and os.path.exists(icon_path):
                IMAGE_ICON = 1
                LR_LOADFROMFILE = 0x00000010
                _log(f"图标文件存在，尝试加载: {icon_path}")
                # 先尝试加载 32x32 图标（系统托盘标准大小）
                hicon = ctypes.windll.user32.LoadImageW(
                    0, icon_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE
                )
                if hicon:
                    _log(f"加载自定义图标成功 (32x32): {icon_path}")
                    return hicon
                # 如果 32x32 失败，尝试默认大小
                hicon = ctypes.windll.user32.LoadImageW(
                    0, icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE
                )
                if hicon:
                    _log(f"加载自定义图标成功 (默认大小): {icon_path}")
                    return hicon
                _log("LoadImageW 两次尝试均失败，回退到默认图标")
        except Exception as e:
            _log(f"加载自定义图标失败: {e}")
        # 回退到默认应用程序图标
        try:
            hicon = ctypes.windll.user32.LoadIconW(0, ctypes.c_void_p(32512))
            if hicon:
                _log("加载默认图标成功")
            return hicon
        except Exception as e:
            _log(f"加载默认图标失败: {e}")
            return 0

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        """窗口过程函数，处理托盘消息和菜单命令。"""
        try:
            if msg == self.WM_TRAYICON:
                if lparam == 0x0205:  # WM_RBUTTONUP
                    self._show_menu()
                elif lparam == 0x0202:  # WM_LBUTTONUP
                    self.on_open()
                return 0
            elif msg == self.WM_COMMAND:
                cmd = wparam & 0xFFFF
                if cmd == self.ID_OPEN:
                    self.on_open()
                elif cmd == self.ID_TOGGLE:
                    if self.on_toggle:
                        self.on_toggle()
                elif cmd == self.ID_EXIT and self.on_exit:
                    self.on_exit()
                return 0
            elif msg == 0x0002:  # WM_DESTROY
                ctypes.windll.user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            _log(f"窗口过程异常: {e}")
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self):
        """显示右键菜单。"""
        try:
            from .server import server_running

            u = self._user32 if self._user32 else ctypes.windll.user32
            menu = u.CreatePopupMenu()
            if not menu:
                _log("创建弹出菜单失败")
                return
            u.AppendMenuW(menu, 0x0000, self.ID_OPEN, "打开管理界面")
            toggle_text = "停止服务器" if server_running() else "启动服务器"
            u.AppendMenuW(menu, 0x0000, self.ID_TOGGLE, toggle_text)
            u.AppendMenuW(menu, 0x00000800, 0, "")  # MF_SEPARATOR
            u.AppendMenuW(menu, 0x0000, self.ID_EXIT, "退出程序")
            pt = ctypes.wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            u.SetForegroundWindow(self._hwnd)
            result = u.TrackPopupMenu(menu, 0x0002, pt.x, pt.y, 0, self._hwnd, None)
            u.DestroyMenu(menu)
            _log(f"显示菜单完成, TrackPopupMenu 返回: {result}")
        except Exception as e:
            _log(f"显示菜单异常: {e}\n{traceback.format_exc()}")

    def _run(self):
        """托盘线程主循环：创建窗口、添加托盘图标、消息循环。"""
        try:
            _log("托盘线程启动")
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            shell32 = ctypes.windll.shell32
            self._user32 = user32

            class WNDCLASSEXW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_uint),
                    ("style", ctypes.c_uint),
                    ("lpfnWndProc", ctypes.c_void_p),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", ctypes.c_void_p),
                    ("hIcon", ctypes.c_void_p),
                    ("hCursor", ctypes.c_void_p),
                    ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName", ctypes.c_wchar_p),
                    ("lpszClassName", ctypes.c_wchar_p),
                    ("hIconSm", ctypes.c_void_p),
                ]

            class NOTIFYICONDATAW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_ulong),
                    ("hWnd", ctypes.c_void_p),
                    ("uID", ctypes.c_ulong),
                    ("uFlags", ctypes.c_ulong),
                    ("uCallbackMessage", ctypes.c_uint),
                    ("hIcon", ctypes.c_void_p),
                    ("szTip", ctypes.c_wchar * 128),
                    ("dwState", ctypes.c_ulong),
                    ("dwStateMask", ctypes.c_ulong),
                    ("szInfo", ctypes.c_wchar * 256),
                    ("uTimeoutOrVersion", ctypes.c_ulong),
                    ("szInfoTitle", ctypes.c_wchar * 64),
                    ("dwInfoFlags", ctypes.c_ulong),
                ]

            WNDPROC = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t
            )
            user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
            user32.RegisterClassExW.restype = ctypes.c_ushort
            user32.CreateWindowExW.argtypes = [
                ctypes.c_ulong,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_ulong,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            user32.CreateWindowExW.restype = ctypes.c_void_p
            user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            user32.LoadIconW.restype = ctypes.c_void_p
            shell32.Shell_NotifyIconW.argtypes = [ctypes.c_ulong, ctypes.POINTER(NOTIFYICONDATAW)]
            shell32.Shell_NotifyIconW.restype = ctypes.c_bool
            user32.GetMessageW.argtypes = [
                ctypes.POINTER(ctypes.wintypes.MSG),
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_uint,
            ]
            user32.GetMessageW.restype = ctypes.c_bool
            user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            user32.PostMessageW.restype = ctypes.c_bool
            user32.DestroyWindow.argtypes = [ctypes.c_void_p]
            user32.DestroyWindow.restype = ctypes.c_bool
            user32.CreatePopupMenu.argtypes = []
            user32.CreatePopupMenu.restype = ctypes.c_void_p
            user32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p]
            user32.AppendMenuW.restype = ctypes.c_bool
            user32.TrackPopupMenu.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            user32.TrackPopupMenu.restype = ctypes.c_bool
            user32.DestroyMenu.argtypes = [ctypes.c_void_p]
            user32.DestroyMenu.restype = ctypes.c_bool
            user32.GetCursorPos.argtypes = [ctypes.POINTER(ctypes.wintypes.POINT)]
            user32.GetCursorPos.restype = ctypes.c_bool
            user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            user32.SetForegroundWindow.restype = ctypes.c_bool
            user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            user32.DefWindowProcW.restype = ctypes.c_ssize_t

            wnd_proc = WNDPROC(self._wnd_proc)
            self._wnd_proc_ref = wnd_proc
            class_name = "MbesBeaconTray"
            hInstance = kernel32.GetModuleHandleW(None)

            # 提前加载图标（用于窗口类和托盘）
            self._hicon = self._load_icon()
            _log(f"图标句柄: {self._hicon}")

            # 注册窗口类（设置图标，确保任务栏等场景显示正确图标）
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(wc)
            wc.lpfnWndProc = ctypes.cast(wnd_proc, ctypes.c_void_p)
            wc.hInstance = hInstance
            wc.lpszClassName = class_name
            wc.hIcon = self._hicon
            wc.hIconSm = self._hicon
            atom = user32.RegisterClassExW(ctypes.byref(wc))
            _log(f"注册窗口类, atom={atom}, 错误码={kernel32.GetLastError()}")

            # 创建隐藏窗口
            self._hwnd = user32.CreateWindowExW(
                0, class_name, "MbesBeacon", 0, 0, 0, 0, 0, 0, 0, hInstance, None
            )
            _log(f"创建窗口, hwnd={self._hwnd}, 错误码={kernel32.GetLastError()}")
            if not self._hwnd:
                _log("创建窗口失败，退出托盘线程")
                return

            # 添加托盘图标
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = 0x00000001 | 0x00000002 | 0x00000004  # NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = self.WM_TRAYICON
            nid.hIcon = self._hicon
            nid.szTip = "MbesBeacon — Minecraft 基岩版服务器管理器"
            result = shell32.Shell_NotifyIconW(0x00000000, ctypes.byref(nid))  # NIM_ADD
            _log(f"添加托盘图标, Shell_NotifyIconW 返回={result}, 错误码={kernel32.GetLastError()}")
            if not result:
                _log("添加托盘图标失败！")
                # 尝试使用旧版结构大小
                nid.cbSize = 504  # NOTIFYICONDATAW_V3_SIZE for Vista+
                result = shell32.Shell_NotifyIconW(0x00000000, ctypes.byref(nid))
                _log(f"重试添加托盘图标(旧版大小), 返回={result}")

            self._nid = nid
            self._running = True
            _log("托盘初始化完成，进入消息循环")

            # 消息循环
            msg = ctypes.wintypes.MSG()
            while self._running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            _log("托盘消息循环结束")
        except Exception as e:
            _log(f"托盘线程异常: {e}\n{traceback.format_exc()}")
        finally:
            self._remove_icon()

    def _remove_icon(self):
        """移除托盘图标。"""
        try:
            if hasattr(self, "_nid") and self._nid:
                ctypes.windll.shell32.Shell_NotifyIconW(0x00000002, ctypes.byref(self._nid))
                _log("移除托盘图标")
        except Exception as e:
            _log(f"移除托盘图标异常: {e}")

    def start(self):
        """在后台线程启动托盘。"""
        if self._thread and self._thread.is_alive():
            _log("托盘线程已在运行")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _log("托盘线程已启动")

    def stop(self):
        """停止托盘并移除图标。"""
        _log("停止托盘")
        self._running = False
        try:
            if self._hwnd:
                ctypes.windll.user32.PostMessageW(self._hwnd, 0x0010, 0, 0)
        except Exception as e:
            _log(f"停止托盘异常: {e}")
        self._remove_icon()

    def notify(self, title, message):
        """显示托盘气泡通知。"""
        try:
            if hasattr(self, "_nid") and self._nid:
                self._nid.uFlags = 0x00000010  # NIF_INFO
                self._nid.szInfo = message
                self._nid.szInfoTitle = title
                self._nid.dwInfoFlags = 0x00000001  # NIIF_INFO
                result = ctypes.windll.shell32.Shell_NotifyIconW(0x00000001, ctypes.byref(self._nid))
                _log(f"显示通知, 返回={result}")
        except Exception as e:
            _log(f"显示通知异常: {e}")
