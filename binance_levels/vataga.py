"""Open a coin's order book in Vataga.terminal (local HTTP API, see Vataga integration doc v1.2).

Request: POST http://localhost:{port}/api/open/{partner_id}/{exchange}/{type}/{symbol}
  exchange = binance, type = F (futures) or S (spot), symbol = BTCUSDT.
Vataga.terminal must be running with Settings > General > "Application integration" turned on,
and the window to control must be linked to "Screener" (right-click window title > Link to window).

A toast button can only launch a link, not send a POST. So the app registers a per-user URL protocol
(binancelevels://open?symbol=BTCUSDT, no admin rights needed). When the toast button is clicked,
Windows starts this exe with that link, and handle_url() sends the POST and exits.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests

from .settings import APP_NAME, Settings, load_settings

PROTOCOL = "binancelevels"
EXCHANGE = "binance"


def build_api_url(settings: Settings, symbol: str) -> str:
    return (
        f"http://localhost:{settings.vataga_port}/api/open/"
        f"{quote(settings.vataga_partner_id, safe='')}/{EXCHANGE}/{settings.vataga_type.upper()}/{quote(symbol, safe='-_')}"
    )


def open_symbol(symbol: str, settings: Settings | None = None) -> tuple[bool, str]:
    """Ask Vataga.terminal to open `symbol`. Returns (ok, message)."""
    settings = settings or load_settings()
    url = build_api_url(settings, symbol)
    try:
        response = requests.post(url, timeout=3)
    except requests.RequestException as exc:
        return False, (
            f"Vataga.terminal is not reachable at localhost:{settings.vataga_port}. "
            f"Start the terminal and turn on Settings > General > Application integration. ({exc.__class__.__name__})"
        )
    if response.status_code != 200:
        return False, f"Vataga.terminal answered HTTP {response.status_code}: {response.text[:200]}"
    return True, response.text


def link_for(symbol: str) -> str:
    return f"{PROTOCOL}://open?symbol={quote(symbol, safe='-_')}"


def register_protocol() -> None:
    """Register binancelevels:// for the current user (HKCU). Safe to call on every start."""
    if sys.platform != "win32":
        return
    try:
        import winreg

        if getattr(sys, "frozen", False):
            command = f'"{sys.executable}" "%1"'
        else:
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            exe = pythonw if pythonw.exists() else Path(sys.executable)
            run_py = Path(__file__).resolve().parent.parent / "run.py"
            command = f'"{exe}" "{run_py}" "%1"'

        root = rf"Software\Classes\{PROTOCOL}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"URL:{APP_NAME}")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root + r"\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    except OSError:
        pass


def _error_box(message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)  # MB_ICONERROR
    except Exception:
        print(message)


def handle_url(url: str) -> bool:
    """If `url` is our protocol link, open the coin in Vataga and return True (no GUI is started)."""
    if not url.lower().startswith(PROTOCOL + "://"):
        return False
    symbol = (parse_qs(urlparse(url).query).get("symbol") or [""])[0].strip().upper()
    if symbol:
        ok, message = open_symbol(symbol)
        if not ok:
            _error_box(message)
    return True
