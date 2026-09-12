from __future__ import annotations

import ctypes
import os
import threading
import webbrowser

import uvicorn
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

from . import autostart


def _enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_enable_dpi_awareness()


def _icon_image() -> Image.Image:
    size = 512
    grad = Image.new("RGBA", (size, size))
    top, bottom = (64, 132, 255), (38, 196, 190)
    px = grad.load()
    for y in range(size):
        t = y / (size - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b, 255)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((28, 28, size - 28, size - 28), radius=116, fill=255)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    ax, ay, bx, by = 150, 172, 362, 340
    steps = 48
    points = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * ax + 2 * (1 - t) * t * 372 + t**2 * bx
        y = (1 - t) ** 2 * ay + 2 * (1 - t) * t * 150 + t**2 * by
        points.append((x, y))
    d.line(points, fill=white, width=74, joint="curve")
    d.ellipse((ax - 62, ay - 62, ax + 62, ay + 62), fill=white)
    d.ellipse((bx - 62, by - 62, bx + 62, by + 62), fill=white)

    return img.resize((64, 64), Image.LANCZOS)


def msgbox(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, "Model Gateway", 0x40)


class TrayApp:
    def __init__(self, server: uvicorn.Server, port: int) -> None:
        self._server = server
        self._port = port
        self._quit_requested = False
        self.icon: Icon | None = None

    def run(self) -> None:
        menu = Menu(
            MenuItem(lambda item: f"运行中 :{self._port}" if not self._server.should_exit else "已停止", None, enabled=False),
            MenuItem("打开管理页", self._open_ui, default=True),
            MenuItem("开机自启", self._toggle_autostart, checked=lambda item: autostart.is_enabled()),
            MenuItem("退出", self.begin_shutdown),
        )
        self.icon = Icon("model-gateway", _icon_image(), "Model Gateway", menu)
        self.icon.run()

    def begin_shutdown(self) -> None:
        if self._quit_requested:
            return
        self._quit_requested = True
        self._server.should_exit = True
        watchdog = threading.Timer(8.0, os._exit, args=(0,))
        watchdog.daemon = True
        watchdog.start()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass

    def _open_ui(self) -> None:
        webbrowser.open(f"http://127.0.0.1:{self._port}/")

    def _toggle_autostart(self) -> None:
        try:
            autostart.toggle()
        except OSError as exc:
            msgbox(f"设置开机自启失败: {exc}")


def acquire_single_instance() -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = k32.CreateMutexW(None, False, "ModelGateway_SingleInstance_Mutex")
    if not handle:
        # 互斥体都建不出来（句柄耗尽之类）时别拦着启动：宁可双开也别打不开
        return True
    return ctypes.get_last_error() != 183
