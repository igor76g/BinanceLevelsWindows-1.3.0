"""System tray icon (optional: needs the `pystray` and `Pillow` packages).

If they are missing, `Tray.available` is False and the window is simply minimized to the taskbar.
The callbacks are called from the tray thread, so they must only put a message into a queue.
"""

from __future__ import annotations

from typing import Callable

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # not installed / no display
    pystray = None
    Image = None
    ImageDraw = None


def _make_image():
    image = Image.new("RGBA", (64, 64), (30, 35, 41, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((6, 6, 58, 58), outline=(240, 185, 11, 255), width=4)
    draw.line((12, 22, 52, 22), fill=(246, 70, 93, 255), width=4)    # resistance
    draw.line((12, 44, 52, 44), fill=(14, 203, 129, 255), width=4)   # support
    draw.line((12, 33, 52, 33), fill=(234, 236, 239, 255), width=2)  # price
    return image


class Tray:
    available = pystray is not None

    def __init__(self, title: str, on_show: Callable[[], None], on_check: Callable[[], None], on_exit: Callable[[], None]):
        self._icon = None
        self._title = title
        self._on_show = on_show
        self._on_check = on_check
        self._on_exit = on_exit

    def start(self) -> bool:
        """Show the icon. Returns False if the tray is not available."""
        if not self.available:
            return False
        if self._icon is not None:
            return True
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Show window", lambda icon, item: self._on_show(), default=True),
                pystray.MenuItem("Check now", lambda icon, item: self._on_check()),
                pystray.MenuItem("Exit", lambda icon, item: self._on_exit()),
            )
            self._icon = pystray.Icon("BinanceLevelsMonitor", _make_image(), self._title, menu)
            self._icon.run_detached()
            return True
        except Exception:
            self._icon = None
            return False

    def set_title(self, text: str) -> None:
        if self._icon is not None:
            try:
                self._icon.title = text[:120]
            except Exception:
                pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
