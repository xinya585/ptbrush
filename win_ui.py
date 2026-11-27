"""PTBrush 自绘更新窗（方案 B · 赛博霓虹）。

为什么不用系统控件：旧版用 msctls_progress32 + STATIC + BUTTON 拼窗口，进度条、
明细、按钮挤在同一块区域里，640×220 的高度根本不够，进度条一路盖住按钮。这里整窗
GDI 自绘：所有元素都按「行」排版（品牌行 / 标题行 / 进度行 / 状态行 / 说明行 /
按钮行），每行高度固定、互不重叠，尺寸再小也不会压到按钮。

对外接口与旧 _NativeWindow 保持兼容：progress=False 就是普通提示框，progress=True
就是下载进度框；调用方在别的线程里写 pct / finished / head / sub / meta_* / body，
窗口自己的定时器负责重绘。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger(__name__)

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_kernel32 = ctypes.windll.kernel32
try:
    _msimg32 = ctypes.WinDLL("msimg32")
except OSError:  # pragma: no cover - 极老的系统
    _msimg32 = None

_HP = ctypes.c_void_p

# ---- 句柄必须先声明 restype，否则 64 位下会被截成 32 位
_user32.CreateWindowExW.restype = _HP
_user32.CreateWindowExW.argtypes = [
    ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    _HP, _HP, _HP, _HP,
]
_user32.DefWindowProcW.restype = ctypes.c_ssize_t
_user32.DefWindowProcW.argtypes = [_HP, ctypes.c_uint, _HP, _HP]
_user32.SendMessageW.restype = ctypes.c_ssize_t
_user32.DrawTextW.argtypes = [_HP, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
_user32.GetClientRect.argtypes = [_HP, ctypes.c_void_p]
_user32.FillRect.argtypes = [_HP, ctypes.c_void_p, _HP]
_user32.SetWindowRgn.argtypes = [_HP, _HP, ctypes.c_int]
_user32.BeginPaint.argtypes = [_HP, ctypes.c_void_p]
_user32.BeginPaint.restype = _HP
_user32.EndPaint.argtypes = [_HP, ctypes.c_void_p]
_user32.GetMessageW.argtypes = [ctypes.c_void_p, _HP, ctypes.c_uint, ctypes.c_uint]
_user32.DispatchMessageW.argtypes = [ctypes.c_void_p]

_gdi32.CreateFontW.restype = _HP
_gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_wchar_p]
_gdi32.CreateSolidBrush.restype = _HP
_gdi32.CreateSolidBrush.argtypes = [ctypes.c_uint]
_gdi32.CreatePen.restype = _HP
_gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_gdi32.CreateCompatibleDC.restype = _HP
_gdi32.CreateCompatibleDC.argtypes = [_HP]
_gdi32.CreateCompatibleBitmap.restype = _HP
_gdi32.CreateCompatibleBitmap.argtypes = [_HP, ctypes.c_int, ctypes.c_int]
_gdi32.SelectObject.restype = _HP
_gdi32.SelectObject.argtypes = [_HP, _HP]
_gdi32.DeleteObject.argtypes = [_HP]
_gdi32.DeleteDC.argtypes = [_HP]
_gdi32.GetStockObject.restype = _HP
_gdi32.GetStockObject.argtypes = [ctypes.c_int]
_gdi32.CreateRoundRectRgn.restype = _HP
_gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
_gdi32.SetBkMode.argtypes = [_HP, ctypes.c_int]
_gdi32.SetTextColor.argtypes = [_HP, ctypes.c_uint]
_gdi32.SaveDC.argtypes = [_HP]
_gdi32.RestoreDC.argtypes = [_HP, ctypes.c_int]
_gdi32.SelectClipRgn.argtypes = [_HP, _HP]
_gdi32.GetTextExtentPoint32W.argtypes = [_HP, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p]
_gdi32.MoveToEx.argtypes = [_HP, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_gdi32.LineTo.argtypes = [_HP, ctypes.c_int, ctypes.c_int]
_gdi32.BitBlt.argtypes = [_HP, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          _HP, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_user32.LoadCursorW.restype = _HP
_user32.SetCursor.restype = _HP
_user32.SetCursor.argtypes = [_HP]
_user32.SetTimer.argtypes = [_HP, ctypes.c_void_p, ctypes.c_uint, _HP]
_user32.SetTimer.restype = ctypes.c_uint
_user32.KillTimer.argtypes = [_HP, ctypes.c_void_p]
_user32.DestroyWindow.argtypes = [_HP]
_user32.ShowWindow.argtypes = [_HP, ctypes.c_int]
_user32.UpdateWindow.argtypes = [_HP]
_user32.InvalidateRect.argtypes = [_HP, ctypes.c_void_p, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [_HP]
_user32.ReleaseCapture.argtypes = []
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
_user32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, _HP]
_user32.TranslateMessage.argtypes = [ctypes.c_void_p]
_user32.IsIconic.argtypes = [_HP]
_user32.FlashWindow.argtypes = [_HP, ctypes.c_int]
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_gdi32.RoundRect.argtypes = [_HP, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]

if _msimg32:
    _msimg32.GradientFill.argtypes = [_HP, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
    _msimg32.GradientFill.restype = ctypes.c_int

# ---- Win32 常量
_WS_POPUP = 0x80000000
_WS_SYSMENU = 0x00080000
_WS_MINIMIZEBOX = 0x00020000
_WS_CLIPCHILDREN = 0x02000000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_APPWINDOW = 0x00040000

_WM_CREATE = 0x0001
_WM_DESTROY = 0x0002
_WM_PAINT = 0x000F
_WM_CLOSE = 0x0010
_WM_ERASEBKGND = 0x0014
_WM_TIMER = 0x0113
_WM_KEYDOWN = 0x0100
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_SYSCOMMAND = 0x0112
_WM_NCLBUTTONDOWN = 0x00A1
_WM_SETICON = 0x0080
_WM_GETMINMAXINFO = 0x0024

_SW_SHOW = 5
_SW_MINIMIZE = 6
_HTCAPTION = 2
_IDC_HAND = 32649
_IDC_ARROW = 32512

_DT_LEFT = 0x00000000
_DT_CENTER = 0x00000001
_DT_RIGHT = 0x00000002
_DT_VCENTER = 0x00000004
_DT_SINGLELINE = 0x00000020
_DT_WORDBREAK = 0x00000010
_DT_NOPREFIX = 0x00000800

_GRADIENT_FILL_RECT_H = 0
_GRADIENT_FILL_RECT_V = 1
_NULL_BRUSH = 5
_CLEARTYPE = 5
_SRCCOPY = 0x00CC0020

# ---- 方案 B 配色（RGB 三元组，用 _cr() 转 COLORREF）
_BG_TOP = (0x0B, 0x0C, 0x1E)
_BG_BOTTOM = (0x06, 0x06, 0x0F)
_STRIPE = (0x14, 0x14, 0x2E)
_PURPLE = (0x7C, 0x5C, 0xFF)
_CYAN = (0x00, 0xE5, 0xFF)
_TEXT = (0xF1, 0xEE, 0xFF)
_TEXT_SUB = (0x8B, 0x89, 0xAE)
_TEXT_LABEL = (0xB7, 0xB4, 0xDA)
_TEXT_DIM = (0x58, 0x56, 0x80)
_TEXT_META = (0xC9, 0xC6, 0xE6)
_TEXT_NOTE = (0x82, 0x80, 0xA6)
_TEXT_BTN_GHOST = (0x9B, 0xEF, 0xFF)
_TEXT_BTN_DIM = (0x6A, 0x68, 0x90)
_SEG_TRACK = (0x1C, 0x1A, 0x31)
_MARK_INK = (0x0A, 0x06, 0x20)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", _HP),
        ("fErase", ctypes.c_int),
        ("rcPaint", _RECT),
        ("fRestore", ctypes.c_int),
        ("fIncUpdate", ctypes.c_int),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", _HP),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", _HP),
        ("hIcon", _HP),
        ("hCursor", _HP),
        ("hbrBackground", _HP),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", _HP),
    ]


class _TRIVERTEX(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long),
                ("Red", ctypes.c_ushort), ("Green", ctypes.c_ushort),
                ("Blue", ctypes.c_ushort), ("Alpha", ctypes.c_ushort)]


class _GRADIENT_RECT(ctypes.Structure):
    _fields_ = [("UpperLeft", ctypes.c_uint), ("LowerRight", ctypes.c_uint)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, _HP, ctypes.c_uint, _HP, _HP)


def _cr(rgb) -> int:
    """RGB 三元组 → COLORREF（0x00BBGGRR）。"""
    r, g, b = rgb
    return (int(b) << 16) | (int(g) << 8) | int(r)


def _mix(a, b, t: float) -> tuple:
    """把 a 按 t 混进 b（t=0 → a，t=1 → b），用来做半透明感。"""
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _font(height: int, weight: int = 400, family: str = "Microsoft YaHei UI") -> int:
    try:
        return _gdi32.CreateFontW(-height, 0, 0, 0, weight, 0, 0, 0, 1, _CLEARTYPE, 0, 0, 0, family)
    except Exception:
        return 0


def _grad_rect(hdc, rect, rgb1, rgb2, vertical: bool = True) -> None:
    if not _msimg32:
        return
    l, t, r, b = rect
    v = (_TRIVERTEX * 2)()
    for i, (x, y, c) in enumerate(((l, t, rgb1), (r, b, rgb2))):
        v[i].x, v[i].y = int(x), int(y)
        v[i].Red, v[i].Green, v[i].Blue = c[0] << 8, c[1] << 8, c[2] << 8
        v[i].Alpha = 0
    gr = _GRADIENT_RECT(0, 1)
    _msimg32.GradientFill(hdc, ctypes.byref(v), 2, ctypes.byref(gr), 1,
                          _GRADIENT_FILL_RECT_V if vertical else _GRADIENT_FILL_RECT_H)


def _fill(hdc, rect, rgb) -> None:
    rc = _RECT(rect[0], rect[1], rect[2], rect[3])
    br = _gdi32.CreateSolidBrush(_cr(rgb))
    _user32.FillRect(hdc, ctypes.byref(rc), br)
    _gdi32.DeleteObject(br)


def _round(hdc, rect, radius: int, *, fill=None, grad=None, vertical=True,
           border=None, border_w=1) -> None:
    """圆角矩形：先用 region 夹住，画渐变或纯色，再描边。"""
    l, t, r, b = rect
    radius = max(1, int(radius))
    rgn = _gdi32.CreateRoundRectRgn(l, t, r + 1, b + 1, radius * 2, radius * 2)
    saved = _gdi32.SaveDC(hdc)
    _gdi32.SelectClipRgn(hdc, rgn)
    if grad:
        _grad_rect(hdc, rect, grad[0], grad[1], vertical)
    elif fill:
        _fill(hdc, rect, fill)
    _gdi32.RestoreDC(hdc, saved)
    if border:
        pen = _gdi32.CreatePen(0, border_w, _cr(border))
        old_pen = _gdi32.SelectObject(hdc, pen)
        null = _gdi32.SelectObject(hdc, _gdi32.GetStockObject(_NULL_BRUSH))
        _gdi32.RoundRect(hdc, l, t, r, b, radius * 2, radius * 2)
        _gdi32.SelectObject(hdc, old_pen)
        _gdi32.SelectObject(hdc, null)
        _gdi32.DeleteObject(pen)
    _gdi32.DeleteObject(rgn)


def _line(hdc, x1, y1, x2, y2, rgb, width=1) -> None:
    pen = _gdi32.CreatePen(0, width, _cr(rgb))
    old = _gdi32.SelectObject(hdc, pen)
    _gdi32.MoveToEx(hdc, int(x1), int(y1), None)
    _gdi32.LineTo(hdc, int(x2), int(y2))
    _gdi32.SelectObject(hdc, old)
    _gdi32.DeleteObject(pen)


class SkinnedWindow:
    """赛博霓虹皮肤的提示 / 进度二合一窗口。"""

    def __init__(
        self,
        title: str = "",
        *,
        width: int = 640,
        height: int = 384,
        progress: bool = False,
        text: str = "",
        detail: str = "",
        ok_text: str = "确定",
        cancel_text: str = "",
        version: str = "",
        sub: str = "",
        meta_left: str = "",
        meta_right: str = "",
        telemetry: str = "",
        note: str = "",
        hint: str = "",
        badge: str = "",
        body: str = "",
        app_name: str = "PTBRUSH",
        brand_sub: str = "PT SITE AUTO-SEED",
    ):
        self.title = title
        self.width = int(width)
        self.height = int(height)
        self.progress = bool(progress)
        self.ok_text = ok_text
        self.cancel_text = cancel_text
        self.app_name = app_name
        self.brand_sub = brand_sub
        self.badge = badge

        # 调用方会随时改这些（另一个线程写，定时器读）
        self.pct = 0.0
        self.finished = not progress
        self.state = "info" if not progress else "downloading"
        self.head = "发现新版本" if progress else ""
        self.version = version
        self.sub = sub
        self.meta_left = meta_left or detail
        self.meta_right = meta_right
        self.telemetry = telemetry
        self.note = note
        self.hint = hint
        self.body = body or (text if not progress else "")

        self.result = None
        self._hwnd = None
        self._cw = self.width
        self._ch = self.height
        self._hover = None
        self._pressed = None
        self._last = None
        self._icon = None
        self._rects = {}
        self._fonts = {}

    # ------------------------------------------------------------ 字体
    def _f(self, key, height, weight=400, family="Microsoft YaHei UI"):
        key = (key, height, weight, family)
        if key not in self._fonts:
            self._fonts[key] = _font(height, weight, family)
        return self._fonts[key]

    def _mono(self, key, height, weight=400):
        return self._f(key, height, weight, "Consolas")

    # ------------------------------------------------------------ 绘制小工具
    def _text(self, hdc, text, x, y, font, rgb, *, width=None, align=_DT_LEFT,
              single=True, glow=None, glow_radius=1):
        if not text:
            return 0
        _gdi32.SelectObject(hdc, font)
        _gdi32.SetBkMode(hdc, 1)
        flags = _DT_NOPREFIX | (_DT_SINGLELINE if single else _DT_WORDBREAK) | align | _DT_VCENTER
        sz = _SIZE()
        _gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(sz))
        w = width if width is not None else sz.cx + 4
        rc = _RECT(int(x), int(y), int(x + w), int(y + sz.cy + 4))
        if glow:
            _gdi32.SetTextColor(hdc, _cr(glow))
            for dx, dy in ((-glow_radius, 0), (glow_radius, 0), (0, -glow_radius), (0, glow_radius)):
                _user32.DrawTextW(hdc, text, -1,
                                  ctypes.byref(_RECT(rc.left + dx, rc.top + dy,
                                                     rc.right + dx, rc.bottom + dy)), flags)
        _gdi32.SetTextColor(hdc, _cr(rgb))
        _user32.DrawTextW(hdc, text, -1, ctypes.byref(rc), flags)
        return sz.cx

    def _measure(self, hdc, text, font):
        _gdi32.SelectObject(hdc, font)
        sz = _SIZE()
        _gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(sz))
        return sz.cx, sz.cy

    def _wrap(self, hdc, text, font, max_w):
        lines = []
        for raw in str(text).split("\n"):
            if not raw:
                lines.append("")
                continue
            buf = ""
            for ch in raw:
                cur = buf + ch
                if self._measure(hdc, cur, font)[0] > max_w and buf:
                    lines.append(buf)
                    buf = ch
                else:
                    buf = cur
            lines.append(buf)
        return lines

    # ------------------------------------------------------------ 各个零件
    def _paint_backdrop(self, hdc, w, h):
        _grad_rect(hdc, (0, 0, w, h), _BG_TOP, _BG_BOTTOM, True)
        # 斜向网格：很淡，只为质感
        stripe = _mix(_BG_TOP, _STRIPE, 0.45)
        step = 26
        for i in range(-h, w + h, step):
            _line(hdc, i, 0, i + h * 0.55, h, stripe)
        # 顶部/底部霓虹线
        _grad_rect(hdc, (0, 0, w // 2, 2), _BG_TOP, _PURPLE, False)
        _grad_rect(hdc, (w // 2, 0, w, 2), _PURPLE, _BG_TOP, False)
        _grad_rect(hdc, (0, h - 1, w // 2, h), _BG_TOP, _PURPLE, False)
        _grad_rect(hdc, (w // 2, h - 1, w, h), _PURPLE, _BG_TOP, False)

    def _paint_brand(self, hdc, w, pad, top):
        # PB 徽标
        _round(hdc, (pad, top, pad + 22, top + 22), 3, grad=(_CYAN, _PURPLE))
        _gdi32.SelectObject(hdc, self._mono("mark", 11, 700))
        _gdi32.SetBkMode(hdc, 1)
        _gdi32.SetTextColor(hdc, _cr(_MARK_INK))
        rc = _RECT(pad, top, pad + 22, top + 22)
        _user32.DrawTextW(hdc, "PB", -1, ctypes.byref(rc),
                          _DT_CENTER | _DT_VCENTER | _DT_SINGLELINE | _DT_NOPREFIX)
        self._text(hdc, self.app_name, pad + 30, top - 1,
                   self._f("brand", 13, 700), _TEXT)
        self._text(hdc, self.brand_sub, pad + 30, top + 15,
                   self._mono("brandsub", 8), (0x6E, 0x6C, 0x93))

        # 右上：徽标 + 最小化
        min_r = (w - pad - 21, top, w - pad, top + 17)
        text = self.badge
        bw = 0
        if text:
            bw = self._measure(hdc, text, self._mono("badge", 8, 600))[0] + 18
        if text:
            bx = min_r[0] - 10 - bw
            badge_r = (bx, top, bx + bw, top + 17)
            _round(hdc, badge_r, 3, fill=_mix(_CYAN, _BG_TOP, 0.94),
                   border=_mix(_CYAN, _BG_TOP, 0.55))
            self._text(hdc, text, badge_r[0], badge_r[1], self._mono("badge", 8, 600),
                       _TEXT_BTN_GHOST, width=bw, align=_DT_CENTER)
        self._rects["min"] = min_r
        hov = self._hover == "min"
        _round(hdc, min_r, 3,
               fill=_mix(_CYAN, _BG_TOP, 0.94 if not hov else 0.82),
               border=_mix(_CYAN, _BG_TOP, 0.55 if not hov else 0.25))
        cxm, cym = (min_r[0] + min_r[2]) // 2, (min_r[1] + min_r[3]) // 2
        _fill(hdc, (cxm - 4, cym - 1, cxm + 4, cym + 1), _TEXT_BTN_GHOST)

    def _paint_headline(self, hdc, w, pad, y):
        head_w = self._text(hdc, self.head, pad, y, self._f("head", 26, 700), _TEXT)
        if self.version:
            self._text(hdc, self.version, pad + head_w + 8, y,
                       self._mono("ver", 26, 700), (0x8F, 0xE9, 0xFF),
                       glow=_mix(_PURPLE, _BG_TOP, 0.3), glow_radius=2)

    def _paint_progress(self, hdc, w, pad, y, inner_w):
        label_h = 14
        self._text(hdc, "正在下载更新包" if self.state != "done" else "更新包已下载",
                   pad, y, self._f("plabel", 11, 600), _TEXT_LABEL)
        pct_txt = str(int(round(max(0.0, min(100.0, self.pct)))))
        unit_w = self._measure(hdc, "%", self._mono("pctunit", 11, 700))[0]
        unit_x = w - pad - unit_w
        pw = self._measure(hdc, pct_txt, self._mono("pct", 22, 700))[0]
        self._text(hdc, pct_txt, unit_x - 3 - pw, y - 6, self._mono("pct", 22, 700),
                   (0x8F, 0xE9, 0xFF), glow=_mix(_CYAN, _BG_TOP, 0.35))
        self._text(hdc, "%", unit_x, y + 4, self._mono("pctunit", 11, 700), _PURPLE)

        # 分段刻度进度条：40 段，填充段渐变；轨道段只留一圈暗边
        bar = (pad, y + 18, w - pad, y + 18 + 13)
        segs, gap = 40, 3
        seg_w = (inner_w - gap * (segs - 1)) / segs
        on = int(round(segs * max(0.0, min(100.0, self.pct)) / 100.0))
        for i in range(segs):
            x0 = bar[0] + i * (seg_w + gap)
            r = (int(round(x0)), bar[1], int(round(x0 + seg_w)), bar[3])
            if i < on:
                _round(hdc, r, 1, grad=(_CYAN, _PURPLE))
            else:
                _round(hdc, r, 1, fill=_SEG_TRACK, border=_mix(_SEG_TRACK, _TEXT_DIM, 0.35))
        if on:
            # 已填充区外圈一道极淡的青光，模仿设计稿的发光段
            _round(hdc, (bar[0] - 1, bar[1] - 1,
                         int(round(bar[0] + on * (seg_w + gap) - gap)) + 1, bar[3] + 1),
                   1, border=_mix(_CYAN, _BG_TOP, 0.72))

    def _paint_footer(self, hdc, w, h, pad, inner_w):
        # 说明行
        note_y = h - 13 - 26 - 20 - 16
        if self.note:
            _round(hdc, (pad, note_y + 2, pad + 11, note_y + 13), 2,
                   fill=_mix(_CYAN, _BG_BOTTOM, 0.9), border=_mix(_CYAN, _BG_BOTTOM, 0.55))
            _gdi32.SelectObject(hdc, self._mono("check", 7, 700))
            _gdi32.SetBkMode(hdc, 1)
            _gdi32.SetTextColor(hdc, _cr(_TEXT_BTN_GHOST))
            rc = _RECT(pad, note_y + 2, pad + 11, note_y + 13)
            _user32.DrawTextW(hdc, "v", -1, ctypes.byref(rc),
                              _DT_CENTER | _DT_VCENTER | _DT_SINGLELINE | _DT_NOPREFIX)
            self._text(hdc, self.note, pad + 18, note_y, self._f("note", 10), _TEXT_NOTE)

        # 按钮行（永远独立于进度区，中间隔着一整段空白）
        btn_h = 26
        btn_y = h - 13 - btn_h
        ok_w = max(64, self._measure(hdc, self.ok_text, self._f("btn", 11, 600))[0] + 34)
        ok_r = (w - pad - ok_w, btn_y, w - pad, btn_y + btn_h)
        cancel_r = None
        if self.cancel_text:
            cw_ = max(72, self._measure(hdc, self.cancel_text, self._f("btn", 11, 600))[0] + 34)
            cancel_r = (ok_r[0] - 10 - cw_, btn_y, ok_r[0] - 10, btn_y + btn_h)
            self._rects["cancel"] = cancel_r
        self._rects["ok"] = ok_r

        if self.hint:
            hint_w = (cancel_r[0] if cancel_r else ok_r[0]) - pad - 14
            self._text(hdc, self.hint, pad, btn_y + 7, self._f("hint", 9), _TEXT_DIM,
                       width=max(80, hint_w))

        self._paint_button(hdc, ok_r, self.ok_text, "primary" if self.finished else "dim",
                           self._hover == "ok")
        if cancel_r:
            self._paint_button(hdc, cancel_r, self.cancel_text, "ghost", self._hover == "cancel")

    def _paint_button(self, hdc, rect, text, kind, hover):
        l, t, r, b = rect
        if kind == "primary":
            _round(hdc, rect, 4, grad=(_CYAN, _PURPLE), border=_mix(_CYAN, _BG_TOP, 0.15))
            color = _MARK_INK
        elif kind == "dim":
            _round(hdc, rect, 4, fill=_mix((0x40, 0x3D, 0x62), _BG_TOP, 0.55),
                   border=_mix((0x6E, 0x6A, 0xA0), _BG_TOP, 0.45))
            color = _TEXT_BTN_DIM
        else:  # ghost
            _round(hdc, rect, 4,
                   fill=_mix(_CYAN, _BG_TOP, 0.94 if not hover else 0.86),
                   border=_mix(_CYAN, _BG_TOP, 0.55 if not hover else 0.25))
            color = _TEXT_BTN_GHOST
        dot_w = 16 if kind == "dim" else 0
        if kind == "dim":
            _fill(hdc, (l + 13, (t + b) // 2 - 1, l + 19, (t + b) // 2 + 1), (0x4C, 0x4A, 0x72))
        self._text(hdc, text, l + dot_w, t + 5, self._f("btn", 11, 600), color,
                   width=r - l - dot_w, align=_DT_CENTER)

    def _paint_info_body(self, hdc, w, h, pad):
        body_top = 58
        btn_h = 26
        body_bottom = h - 13 - btn_h - 22
        font = self._f("body", 11)
        lines = self._wrap(hdc, self.body or self.text, font, w - pad * 2)
        lh = 18
        max_lines = max(3, (body_bottom - body_top) // lh)
        for i, line in enumerate(lines[:max_lines]):
            self._text(hdc, line, pad, body_top + i * lh, font, _TEXT_LABEL)
        ok_w = max(70, self._measure(hdc, self.ok_text, self._f("btn", 11, 600))[0] + 34)
        ok_r = (w - pad - ok_w, h - 13 - btn_h, w - pad, h - 13)
        self._rects["ok"] = ok_r
        self._paint_button(hdc, ok_r, self.ok_text, "primary", self._hover == "ok")

    def _paint(self, hdc):
        w, h = self._cw, self._ch
        pad = 17
        inner_w = w - pad * 2
        self._paint_backdrop(hdc, w, h)
        self._paint_brand(hdc, w, pad, 13)

        if self.progress:
            self._paint_headline(hdc, w, pad, 90)
            if self.sub:
                self._text(hdc, self.sub, pad, 122, self._f("sub", 12), _TEXT_SUB,
                           width=inner_w, single=False)
            self._paint_progress(hdc, w, pad, 168, inner_w)
            if self.meta_left:
                self._text(hdc, self.meta_left, pad, 214, self._mono("meta", 10), _TEXT_META)
            if self.meta_right:
                mw = self._measure(hdc, self.meta_right, self._mono("meta", 10))[0]
                self._text(hdc, self.meta_right, w - pad - mw, 214, self._mono("meta", 10), _TEXT_DIM)
            if self.telemetry:
                self._text(hdc, self.telemetry, pad, 232, self._mono("tele", 9), _TEXT_DIM)
            if self.body:
                lines = self._wrap(hdc, self.body, self._f("note", 10), inner_w)
                for i, line in enumerate(lines[:4]):
                    self._text(hdc, line, pad, 254 + i * 15, self._f("note", 10),
                               _mix(_TEXT_NOTE, (0xE0, 0x9A, 0x9A), 0.5))
            # 中间留白处一条 HUD 刻度线，免得空得像漏了内容
            ry = h - 13 - 26 - 20 - 16 - 40
            _line(hdc, pad, ry, int(w * 0.55), ry, _mix(_PURPLE, _BG_BOTTOM, 0.72))
            for i in range(6):
                x = pad + int((w * 0.55 - pad) * i / 5)
                _line(hdc, x, ry - 3, x, ry + 1, _mix(_PURPLE, _BG_BOTTOM, 0.62))
            self._paint_footer(hdc, w, h, pad, inner_w)
        else:
            self._rects.pop("cancel", None)
            self._paint_info_body(hdc, w, h, pad)

    # ------------------------------------------------------------ 窗口过程
    def _state(self):
        return (round(self.pct, 2), bool(self.finished), self.state, self.head,
                self.version, self.sub, self.meta_left, self.meta_right, self.body, self.hint)

    def _invalidate(self):
        if self._hwnd:
            _user32.InvalidateRect(self._hwnd, None, 0)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == _WM_CREATE:
                self._hwnd = hwnd
                self._attach_icon(hwnd)
                rgn = _gdi32.CreateRoundRectRgn(0, 0, self.width + 1, self.height + 1, 16, 16)
                _user32.SetWindowRgn(hwnd, rgn, True)
                _user32.SetTimer(hwnd, 1, 120, None)
                return 0
            if msg == _WM_TIMER:
                st = self._state()
                if st != self._last:
                    was_finished = bool(self._last[1]) if self._last else False
                    self._last = st
                    self._invalidate()
                    if self.finished and not was_finished and _user32.IsIconic(hwnd):
                        # 最小化状态下下完了：任务栏闪一下，不强行抢前台
                        _user32.FlashWindow(hwnd, True)
                return 0
            if msg == _WM_PAINT:
                ps = _PAINTSTRUCT()
                hdc = _user32.BeginPaint(hwnd, ctypes.byref(ps))
                mem = _gdi32.CreateCompatibleDC(hdc)
                bmp = _gdi32.CreateCompatibleBitmap(hdc, self._cw, self._ch)
                old = _gdi32.SelectObject(mem, bmp)
                try:
                    self._paint(mem)
                    _gdi32.BitBlt(hdc, 0, 0, self._cw, self._ch, mem, 0, 0, _SRCCOPY)
                finally:
                    _gdi32.SelectObject(mem, old)
                    _gdi32.DeleteObject(bmp)
                    _gdi32.DeleteDC(mem)
                _user32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if msg == _WM_ERASEBKGND:
                return 1
            if msg == _WM_MOUSEMOVE:
                x, y = self._lp(lparam)
                hov = self._hit(x, y)
                if hov != self._hover:
                    self._hover = hov
                    _user32.SetCursor(_user32.LoadCursorW(None, _IDC_HAND if hov else _IDC_ARROW))
                    self._invalidate()
                return 0
            if msg == _WM_LBUTTONDOWN:
                x, y = self._lp(lparam)
                hit = self._hit(x, y)
                if hit:
                    self._pressed = hit
                elif y < 40:
                    _user32.ReleaseCapture()
                    _user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, _HTCAPTION, 0)
                return 0
            if msg == _WM_LBUTTONUP:
                x, y = self._lp(lparam)
                hit = self._hit(x, y)
                pressed, self._pressed = self._pressed, None
                if hit and hit == pressed:
                    self._activate(hit)
                return 0
            if msg == _WM_KEYDOWN:
                vk = int(wparam)
                if vk == 13 and self.finished:  # Enter
                    self.result = "ok"
                    self._close()
                elif vk == 27:  # Esc
                    self.result = "cancel"
                    self._close()
                return 0
            if msg == _WM_CLOSE:
                if self.result is None:
                    self.result = "cancel"
                self._close()
                return 0
            if msg == _WM_DESTROY:
                _user32.KillTimer(hwnd, 1)
                for f in self._fonts.values():
                    if f:
                        _gdi32.DeleteObject(f)
                self._fonts.clear()
                _user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            logger.warning(f"更新窗口消息处理出错: {e}")
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    @staticmethod
    def _lp(lparam):
        v = int(lparam)
        return v & 0xFFFF, (v >> 16) & 0xFFFF

    def _hit(self, x, y):
        for name, r in self._rects.items():
            if r and r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                if name == "ok" and not self.finished:
                    continue
                return name
        return None

    def _activate(self, hit):
        if hit == "min":
            _user32.ShowWindow(self._hwnd, _SW_MINIMIZE)
            return
        if hit == "ok" and self.finished:
            self.result = "ok"
            self._close()
            return
        if hit == "cancel":
            self.result = "cancel"
            self._close()

    def _attach_icon(self, hwnd):
        try:
            from paths import bundle_dir

            ico = bundle_dir() / "assets" / "ptbrush.ico"
            if not ico.exists():
                return
            _user32.LoadImageW.restype = _HP
            _user32.LoadImageW.argtypes = [_HP, ctypes.c_wchar_p, ctypes.c_uint,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            self._icon = _user32.LoadImageW(None, str(ico), 1, 32, 32, 0x10)
            if self._icon:
                _user32.SendMessageW(hwnd, _WM_SETICON, 1, self._icon)
                _user32.SendMessageW(hwnd, _WM_SETICON, 0, self._icon)
        except Exception:
            pass

    def _close(self):
        if self._hwnd:
            _user32.DestroyWindow(self._hwnd)

    # ------------------------------------------------------------ 对外
    def run(self) -> str:
        self._cls_name = f"PTBrushSkin{id(self)}"
        self._proc = _WNDPROC(self._wndproc)
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(self._proc, _HP)
        wc.hInstance = _kernel32.GetModuleHandleW(None)
        wc.hCursor = _user32.LoadCursorW(None, _IDC_ARROW)
        wc.hbrBackground = None
        wc.lpszClassName = self._cls_name
        _user32.RegisterClassExW(ctypes.byref(wc))

        sw, sh = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
        x = max(0, (sw - self.width) // 2)
        y = max(0, (sh - self.height) // 3)
        hwnd = _user32.CreateWindowExW(
            _WS_EX_TOPMOST | _WS_EX_APPWINDOW,
            self._cls_name, self.title,
            _WS_POPUP | _WS_SYSMENU | _WS_MINIMIZEBOX | _WS_CLIPCHILDREN,
            x, y, self.width, self.height,
            None, None, wc.hInstance, None,
        )
        if not hwnd:
            logger.warning("更新窗口创建失败")
            return "cancel"
        _user32.ShowWindow(hwnd, _SW_SHOW)
        _user32.UpdateWindow(hwnd)
        try:
            _user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        self._last = self._state()
        _user32.InvalidateRect(hwnd, None, 0)

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        try:
            _user32.UnregisterClassW(self._cls_name, wc.hInstance)
        except Exception:
            pass
        return self.result or "cancel"
