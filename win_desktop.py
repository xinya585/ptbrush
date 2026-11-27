"""Windows 桌面集成：托盘图标、开机自启、单实例。

只在 Windows 桌面版（便携版 exe / 本机 python 跑）里用；容器是 Linux，main.py 有平台判断，
不会 import 到这里。
"""

import ctypes
import hashlib
import json
import os
import queue
import sys
import threading
import time
import webbrowser
from pathlib import Path

from loguru import logger

from paths import app_root, bundle_dir, data_dir
from version import get_version

APP_NAME = "PTBrush"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "PTBrush"
INSTANCE_FILE = "instance.json"

_MUTEX = None
_UPDATE_BUSY = threading.Lock()


# ---------------------------------------------------------------- 开机自启

_ERROR_ALREADY_EXISTS = 183


def _exe_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    # 开发态：用 pythonw/python 启动也没有意义，仍然指向解释器
    return Path(sys.executable).resolve()


def _run_command() -> str:
    """开机启动命令。打包态用 exe 自己，开发态用 python main.py。"""
    if getattr(sys, "frozen", False):
        return f'"{_exe_path()}"'
    script = str((app_root() / "main.py").resolve())
    return f'"{sys.executable}" "{script}"'


def is_autostart_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    """写/删 HKCU\\...\\Run，不需要管理员权限。"""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _run_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
        logger.info(f"开机自启已{'开启' if enabled else '关闭'}")
        return True
    except OSError as e:
        logger.warning(f"设置开机自启失败（不影响程序运行）: {e}")
        return False


# ---------------------------------------------------------------- 单实例


def acquire_single_instance() -> bool:
    """True：拿到锁；False：已经有一个实例在跑。"""
    global _MUTEX
    # 注意别用内置 hash()：Python 每个进程的字符串哈希都加盐，两次启动算出的名字不一样
    digest = hashlib.md5(str(data_dir()).lower().encode("utf-8")).hexdigest()[:12]
    kernel32 = ctypes.windll.kernel32
    # 同一数据目录才算同一个实例，用不同 data 目录跑第二份是允许的
    _MUTEX = kernel32.CreateMutexW(None, False, f"Local\\PTBrush-{digest}")
    return kernel32.GetLastError() != _ERROR_ALREADY_EXISTS


def remember_port(port: int) -> None:
    try:
        (data_dir() / INSTANCE_FILE).write_text(
            json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8"
        )
    except OSError:
        pass


def read_port() -> int | None:
    """上一个实例留下的端口，用来在二次启动时直接打开正确地址。"""
    try:
        data = json.loads((data_dir() / INSTANCE_FILE).read_text(encoding="utf-8"))
        return int(data.get("port"))
    except (OSError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------- 托盘


# ---------------------------------------------------------------- 控制台


def attach_console() -> bool:
    """窗口化打包后没有控制台：--console / --version 时按需接管一个。

    从 cmd/PowerShell 里调用时优先附着到父进程的控制台，输出能直接看见；
    双击 exe 时新建一个窗口，避免参数「按了没反应」。
    """
    kernel32 = ctypes.windll.kernel32
    if kernel32.GetConsoleWindow():
        return True
    attached = bool(kernel32.AttachConsole(-1))  # ATTACH_PARENT_PROCESS
    if not attached and not kernel32.AllocConsole():
        return False
    try:
        con = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = con
        sys.stderr = con
        kernel32.SetConsoleOutputCP(65001)
    except OSError:
        return attached
    return True


def _load_icon():
    from PIL import Image

    for path in (
        bundle_dir() / "assets" / "ptbrush.ico",
        Path(__file__).parent / "assets" / "ptbrush.ico",
        bundle_dir() / "web" / "static" / "favicon.ico",
    ):
        if path.exists():
            return Image.open(path)
    return Image.new("RGBA", (64, 64), (0, 0, 0, 0))


def _msgbox(text: str, title: str = APP_NAME, flags: int = 0x40) -> int:
    """0x40 = 信息图标，返回值见 MessageBox 常量。"""
    try:
        return ctypes.windll.user32.MessageBoxW(0, str(text), title, flags)
    except Exception:
        return 0


def _notify(icon, text: str, title: str = APP_NAME) -> bool:
    """优先用托盘气泡提示，弹完就消失，不需要用户手动关；失败再退回弹窗。"""
    try:
        if icon is not None:
            icon.notify(str(text), title)
            return True
    except Exception as e:
        logger.warning(f"托盘气泡不可用，改用弹窗: {e}")
    _msgbox(text, title=title)
    return False


# ---------------------------------------------------------------- 更新界面
#
# 自动升级原来只用托盘气泡 + 系统消息框：下载过程完全静默，网速慢时看不出在动，
# 失败也只有一闪而过的气泡，用户抓不到原因（实测卡在解压那步时毫无反馈）。
# 现在用 win_ui.SkinnedWindow 自绘一个赛博霓虹皮肤的窗口（纯 GDI，不引第三方 GUI 库）：
# 点「检查更新」就直接弹窗显示下载进度，下完校验通过后停在「确定」按钮上，
# 用户点一下才退出去安装，失败原因也留在窗口里。
#
# 1.1.5 起窗口是 640×384 的自绘面板：版式按「行」切分（品牌行 / 标题行 / 进度行 /
# 状态行 / 说明行 / 按钮行），进度区和按钮区各占一行、中间留白，从根上不会再出现
# 旧版 640×220 里进度条盖住按钮的问题；右上角还有自绘的最小化按钮。

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_kernel32 = ctypes.windll.kernel32

# 64 位下句柄是 8 字节指针，不声明 restype/argtypes 会被 ctypes 默认当 32 位 int 截断，
# CreateWindowExW 返回的窗口句柄直接就坏了。
_HP = ctypes.c_void_p
_user32.CreateWindowExW.restype = _HP
_user32.CreateWindowExW.argtypes = [
    ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    _HP, _HP, _HP, _HP,
]
_kernel32.GetModuleHandleW.restype = _HP
_kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
_user32.LoadCursorW.restype = _HP
_user32.LoadCursorW.argtypes = [_HP, _HP]
_user32.LoadImageW.restype = _HP
_user32.LoadImageW.argtypes = [_HP, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_user32.GetSysColorBrush.restype = _HP
_user32.GetSysColorBrush.argtypes = [ctypes.c_int]
_gdi32.CreateFontW.restype = _HP
_user32.SendMessageW.restype = ctypes.c_ssize_t
_user32.SendMessageW.argtypes = [_HP, ctypes.c_uint, _HP, _HP]
_user32.DefWindowProcW.restype = ctypes.c_ssize_t
_user32.DefWindowProcW.argtypes = [_HP, ctypes.c_uint, _HP, _HP]
_user32.DestroyWindow.argtypes = [_HP]
_user32.ShowWindow.argtypes = [_HP, ctypes.c_int]
_user32.UpdateWindow.argtypes = [_HP]
_user32.SetWindowTextW.argtypes = [_HP, ctypes.c_wchar_p]
_user32.SetTimer.argtypes = [_HP, ctypes.c_void_p, ctypes.c_uint, _HP]
_user32.KillTimer.argtypes = [_HP, ctypes.c_void_p]
_user32.GetClientRect.argtypes = [_HP, ctypes.c_void_p]
_user32.PostMessageW.argtypes = [_HP, ctypes.c_uint, _HP, _HP]
_user32.SetForegroundWindow.argtypes = [_HP]
_user32.SetFocus.argtypes = [_HP]
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.GetMessageW.argtypes = [ctypes.c_void_p, _HP, ctypes.c_uint, ctypes.c_uint]
_user32.TranslateMessage.argtypes = [ctypes.c_void_p]
_user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
_user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
_user32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, _HP]
_user32.EnableWindow.argtypes = [_HP, ctypes.c_int]
_user32.SetWindowTextW.restype = ctypes.c_int
_user32.GetClientRect.restype = ctypes.c_int
_user32.InvalidateRect.argtypes = [_HP, ctypes.c_void_p, ctypes.c_int]
_gdi32.CreateSolidBrush.restype = _HP
_gdi32.CreateSolidBrush.argtypes = [ctypes.c_uint]
_gdi32.SetBkMode.restype = ctypes.c_int
_gdi32.SetBkMode.argtypes = [_HP, ctypes.c_int]
_gdi32.SetTextColor.restype = ctypes.c_uint
_gdi32.SetTextColor.argtypes = [_HP, ctypes.c_uint]

from win_ui import SkinnedWindow as _NativeWindow  # 自绘赛博霓虹皮肤（方案 B）


def _show_info(title: str, message: str, ok_text: str = "确定", height: int = 200) -> None:
    """只有「确定」的提示窗口：失败原因停在这里，不会一闪而过。"""
    # 按行数把窗口撑到够高，别把最后一行「到官网下载」裁掉
    lines = len(str(message).split("\n"))
    need = 58 + lines * 18 + 13 + 26 + 22
    win = _NativeWindow(
        title,
        width=640,
        height=min(360, max(200, int(height), need)),
        text=message,
        ok_text=ok_text,
    )
    win.run()


def _run_update_flow(icon=None) -> None:
    """检查更新 → 进度条窗口下载 → 点「确定」才退出安装。"""
    import update as updater

    try:
        snap = updater.check()
    except Exception as e:
        logger.warning(f"检查更新失败: {e}")
        _show_info("检查更新失败", f"连不上版本清单：\n{updater.manifest_url()}\n\n{e}\n\n可以到官网手动下载：https://zta.cc.cd/")
        return

    if snap.get("error"):
        _show_info(
            "检查更新失败",
            f"读取版本清单失败：\n{updater.manifest_url()}\n\n{snap['error']}\n\n可以到官网手动下载：https://zta.cc.cd/",
        )
        return

    latest = snap.get("latest")
    current = snap.get("current")
    if not snap.get("has_update"):
        _show_info("检查更新", f"当前版本 {current}，已经是最新版本。", height=176)
        return

    size = snap.get("size")
    ver = str(latest)
    size_mb = (float(size) / 1048576.0) if size else None
    sub = f"当前 {current}" + (f"，安装包 {size_mb:.1f} MB" if size_mb else "") + " —— 下载完成后再点「确定」才会安装"
    try:
        from urllib.parse import urlparse

        host = urlparse(updater.manifest_url()).netloc
    except Exception:
        host = "zta.cc.cd"
    telemetry = f"{host} ｜ HTTPS ｜ 断点续传 ｜ SHA256 校验"

    win = _NativeWindow(
        f"PTBrush 更新到 {latest}",
        width=640,
        height=384,
        progress=True,
        version=ver,
        sub=sub,
        meta_left="准备中…",
        meta_right="正在计算剩余时间…",
        telemetry=telemetry,
        note="下载后自动校验 SHA256，安装前会备份旧版本，失败自动回滚",
        hint="下载中「确定」暂不可用 · 点「—」最小化后下载继续",
        badge=f"UPDATE · v{ver}",
        cancel_text="稍后再说",
    )
    state = {"install": False}

    def _download():
        last = {"t": 0.0, "done": 0, "speed": 0.0}

        try:
            def on_progress(done: int, total: int):
                win.pct = (done * 100.0 / total) if total else 0.0
                win.meta_left = (f"{done / 1048576:.1f} MB / {total / 1048576:.1f} MB"
                                 if total else f"{done / 1048576:.1f} MB")
                now = time.time()
                if not last["t"]:
                    last["t"] = now
                dt = now - last["t"]
                if dt >= 0.8 and done >= last["done"]:
                    inst = (done - last["done"]) / dt / 1048576.0
                    last["speed"] = inst if last["speed"] <= 0 else last["speed"] * 0.7 + inst * 0.3
                    last["t"], last["done"] = now, done
                    if total and last["speed"] > 0.02:
                        eta = int((total - done) / (last["speed"] * 1048576.0))
                        win.meta_right = (f"剩余约 {eta // 60} 分 {eta % 60:02d} 秒"
                                          if eta >= 60 else f"剩余约 {max(1, eta)} 秒")

            updater.download(progress=on_progress)
            snap2 = updater.state()
            pkg = snap2.get("downloaded")
            got = Path(pkg).stat().st_size if pkg else 0
            win.pct = 100.0
            win.state = "done"
            win.head = "更新包已就绪"
            win.sub = (f"当前 {current}" + (f"，安装包 {size_mb:.1f} MB" if size_mb else "")
                       + " —— 已下载并通过 SHA256 校验")
            win.meta_left = f"{got / 1048576:.1f} MB"
            win.meta_right = "校验通过"
            win.hint = "点「确定」开始安装：程序退出后替换文件，再自动重启"
            win.note = "安装约需 10 秒，data 目录不会被覆盖"
            win.finished = True
            state["install"] = True
        except Exception as e:
            logger.warning(f"下载更新失败: {e}")
            win.state = "error"
            win.head = "下载失败"
            win.version = ""
            win.sub = ""
            win.pct = 0.0
            win.meta_left = "已中止"
            win.meta_right = ""
            win.body = f"{e}\n可以稍后再试，或到官网 https://zta.cc.cd/ 手动下载 zip 覆盖"
            win.hint = "点「稍后再说」关闭窗口"
            win.finished = True

    threading.Thread(target=_download, name="ptbrush-update-download", daemon=True).start()
    result = win.run()
    if result != "ok" or not state["install"]:
        return

    try:
        updater.apply()
    except Exception as e:
        logger.warning(f"应用更新失败: {e}")
        _show_info(
            "安装失败",
            f"{e}\n\n可以到官网 https://zta.cc.cd/ 下载最新的 Windows 包，解压覆盖当前目录（data 目录会保留）。",
        )



def run_update_check() -> None:
    """命令行 / 托盘共用入口：在当前线程里把更新流程走完（窗口需要自己的消息循环）。"""
    _run_update_flow(None)



def check_updates(icon=None, item=None) -> None:
    """托盘的「检查更新」：立即返回，实际工作在后台线程里做，重复点击会被忽略。"""
    if not _UPDATE_BUSY.acquire(blocking=False):
        logger.info("更新流程正在进行，忽略重复点击")
        return

    def _worker():
        try:
            _run_update_flow(icon)
        finally:
            _UPDATE_BUSY.release()

    threading.Thread(target=_worker, name="ptbrush-update-check", daemon=True).start()


def start_tray(port: int) -> bool:
    """在后台线程里跑托盘，不要在调用线程里阻塞。"""
    try:
        import pystray
    except Exception as e:  # 没装依赖 / 无桌面环境
        logger.warning(f"托盘图标不可用: {e}")
        return False

    url = f"http://127.0.0.1:{port}"

    def open_ui(icon, item):
        webbrowser.open(url)

    def open_data(icon, item):
        try:
            os.startfile(str(data_dir()))
        except OSError as e:
            logger.warning(f"打开数据目录失败: {e}")

    def toggle_autostart(icon, item):
        set_autostart(not is_autostart_enabled())
        icon.update_menu()

    def quit_app(icon, item):
        logger.info("从托盘退出 PTBrush")
        icon.stop()
        # 主线程被 BlockingScheduler 占着，等日志落盘后直接退出进程
        threading.Timer(0.4, lambda: os._exit(0)).start()

    menu = pystray.Menu(
        pystray.MenuItem("打开界面", open_ui, default=True),
        pystray.MenuItem("打开数据目录", open_data),
        pystray.MenuItem("开机自启", toggle_autostart, checked=lambda item: is_autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"检查更新（当前 {get_version()}）", check_updates),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )

    try:
        icon = pystray.Icon(APP_NAME, _load_icon(), f"{APP_NAME} {get_version()} — {url}", menu)
        threading.Thread(target=icon.run, name="ptbrush-tray", daemon=True).start()
        logger.info(f"托盘图标已启动（右键图标可开关机自启）: {url}")
        return True
    except Exception as e:
        logger.warning(f"托盘图标启动失败: {e}")
        return False
