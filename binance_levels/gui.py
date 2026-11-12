"""Tkinter UI: one window with two lists (1 = coins approaching a cascade, 2 = the coins closest to a cascade),
a settings dialog, a mini chart, a signals journal and an optional system tray icon.

Double-click a ticker to open the coin in Vataga.terminal; click "Copy" to copy the ticker;
right-click (or the Chart button) shows a mini chart with the cascades.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import __version__, notifier, popups, vataga
from .binance_api import BinanceApi
from .chart import build_scene
from .journal import OUTCOMES, Journal, summarize
from .monitor import Monitor
from .settings import (
    APP_NAME, LEVEL_INTERVALS, clear_alerts_log, TOLERANCE_MODES, Settings, alert_times_path, alerts_log_path, journal_path,
    load_settings, save_settings,
)
from .tray import Tray

MAX_LOG_LINES = 300
BOLD = ("Segoe UI", 10, "bold")

# Settings dialog: one tab per section.
# (settings key, label, python type, minimum allowed value); label None = tab title
SETTINGS_FIELDS = [
    ("Filters", None, None, None),
    ("min_volume_million", "Minimum 24h volume (million USDT)", float, 0.0),
    ("min_natr", "Minimum NATR of the last window (%, 0 = off)", float, 0.0),
    ("min_trades", "Minimum trades in the last window (0 = off)", int, 0),
    ("activity_window_minutes", "Window for NATR and trades: last N minutes", int, 1),
    ("Cascades", None, None, None),
    ("level_interval", "Candle size for finding extremes", str, ""),
    ("level_lookback_candles", "History to search (number of candles)", int, 20),
    ("swing_strength", "Extreme must beat N candles on its left", int, 1),
    ("swing_confirm_candles", "...and N candles on its right (confirms a fresh extreme)", int, 1),
    ("min_extremes", "Minimum extremes in a cascade", int, 2),
    ("tolerance_mode", "Tolerance: candles (adapts to the coin) / percent", str, ""),
    ("level_tolerance_candles", "Tolerance in average 1-min candles", float, 0.0),
    ("level_tolerance_percent", "Tolerance in percent (percent mode)", float, 0.0),
    ("level_break_candles", "Level is broken if a wick passes it by N avg candles", float, 0.0),
    ("min_level_score", "Minimum cascade score (0-100, 0 = off)", float, 0.0),
    ("levels_refresh_minutes", "Recalculate cascades every (minutes)", int, 1),
    ("Approach", None, None, None),
    ("approach_candles", "Notify when distance <= N average 1-min candles", float, 0.0),
    ("avg_candle_minutes", "Average 1-min candle measured over (minutes)", int, 5),
    ("approach_lookback_minutes", "Trend: compare with N minutes ago", int, 1),
    ("away_tolerance_candles", "Trend: change smaller than N candles = flat", float, 0.0),
    ("closest_count", "Coins in the 'closest' list", int, 1),
    ("closest_max_candles", "'Closest' list: not farther than N avg candles (0 = no limit)", float, 0.0),
    ("cooldown_minutes", "Notification cooldown per coin and side (minutes)", int, 0),
    ("check_interval_seconds", "Check interval (seconds, at least 15)", int, 15),
    ("Notifications and window", None, None, None),
    ("popup_auto_close_sec", "Close alert popups after (seconds, 0 = never)", int, 0),
    ("journal_check_minutes", "Journal: evaluate a signal after (minutes)", int, 5),
]

CHOICES = {"level_interval": LEVEL_INTERVALS, "tolerance_mode": TOLERANCE_MODES}

# (settings key, label); shown as check boxes on the last tab
BOOL_FIELDS = [
    ("approach_filter_enabled", "Do not notify when the price is moving away from the cascade"),
    ("sound_enabled", "Play a sound"),
    ("popup_enabled", "Popup window with buttons (always on top)"),
    ("toast_enabled", "Windows toast notification"),
    ("minimize_to_tray", "Minimizing the window hides it to the system tray"),
    ("close_to_tray", "The X button hides the window to the tray instead of exiting"),
    ("clear_alerts_log_on_exit", "Empty the alerts log when the app is closed"),
    ("autostart_monitoring", "Start monitoring automatically when the app opens"),
]

TREND_TEXT = {"closing": "Closing", "away": "Moving away", "flat": "Flat"}


def fmt_million(value: float) -> str:
    return f"{value / 1e6:,.1f}"


def side_text(side: str) -> str:
    return "Resistance" if side == "resistance" else "Support"


class SymbolTree(ttk.Frame):
    """Treeview with a vertical scrollbar. Two clickable cells ("Chart" and "Copy") follow the ticker in every row.

    Double-click on a row -> on_open(symbol); click "Copy" -> on_copy(symbol); click "Chart" (or right-click
    anywhere in the row) -> on_chart(symbol); selecting a row -> on_select(symbol)."""

    CHART_TEXT = "Chart"
    COPY_TEXT = "Copy"

    def __init__(self, master, columns, on_open, on_copy=None, on_select=None, on_chart=None, height=12) -> None:
        super().__init__(master)
        self.on_open = on_open
        self.on_copy = on_copy
        self.on_select = on_select
        self.on_chart = on_chart
        columns = list(columns)
        self._ticker_pos = [c[0] for c in columns].index("ticker")
        columns[self._ticker_pos + 1:self._ticker_pos + 1] = [("chart", "", 58), ("copy", "", 58)]
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings", height=height, selectmode="browse")
        for key, title, width in columns:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w" if key == "ticker" else ("center" if key in ("chart", "copy") else "e"))
        bar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._double_click)
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<Button-3>", self._right_click)
        self.tree.bind("<<TreeviewSelect>>", self._selected)

    def _column_name(self, event) -> str:
        """Name of the column under the mouse ('' if none)."""
        text = self.tree.identify_column(event.x)   # "#3"
        names = list(self.tree["columns"])
        try:
            return names[int(text[1:]) - 1]
        except (ValueError, IndexError):
            return ""

    def _click(self, event) -> str | None:
        """A click on the 'Chart' / 'Copy' cell acts on the coin of that row."""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        item = self.tree.identify_row(event.y)
        name = self._column_name(event)
        if not item or name not in ("chart", "copy"):
            return None
        symbol = str(self.tree.set(item, "ticker"))
        callback = self.on_chart if name == "chart" else self.on_copy
        if callback is not None:
            callback(symbol)
        return "break"

    def _double_click(self, event) -> None:
        if self._column_name(event) in ("chart", "copy"):
            return  # double-click on a button cell must not open Vataga
        item = self.tree.identify_row(event.y)
        if item:
            self.on_open(str(self.tree.set(item, "ticker")))

    def _right_click(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            if self.on_chart is not None:
                self.on_chart(str(self.tree.set(item, "ticker")))

    def _selected(self, _event) -> None:
        symbol = self.selected_symbol()
        if symbol and self.on_select is not None:
            self.on_select(symbol)

    def selected_symbol(self) -> str | None:
        selection = self.tree.selection()
        return str(self.tree.set(selection[0], "ticker")) if selection else None

    def set_rows(self, rows: list[tuple]) -> None:
        """Replace the rows; the selected coin stays selected if it is still in the list."""
        keep = self.selected_symbol()
        self.tree.delete(*self.tree.get_children())
        pos = self._ticker_pos + 1
        for row in rows:
            row = tuple(row)
            values = row[:pos] + (self.CHART_TEXT, self.COPY_TEXT) + row[pos:]
            item = self.tree.insert("", "end", values=values)
            if keep is not None and str(self.tree.set(item, "ticker")) == keep:
                self.tree.selection_set(item)


class ChartWindow(tk.Toplevel):
    """Chart of one coin: candles of the cascade interval (the whole searched history, including the candle that is
    still forming), cascades and the current price.

    A cascade is a ray starting at its oldest extreme; a circle marks every extreme it consists of, a light band
    spans from its lowest to its highest extreme, the line is the average price of the extremes (the level used for the
    distance). Label: R/S, price, number of extremes, score. The cascade nearest to the price is drawn thicker.
    Candles are loaded for this window only (one request every 15 s); cascades come from the running monitor."""

    REDRAW_MS = 3000
    FETCH_SEC = 15

    def __init__(self, app: "App", symbol: str) -> None:
        super().__init__(app)
        self.app = app
        self.symbol = symbol
        self.title(f"{symbol} - cascades")
        self.geometry("980x520+120+80")
        self.canvas = tk.Canvas(self, bg="#1e2329", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.info = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.info, wraplength=940, justify="left").pack(anchor="w", padx=8, pady=4)
        self.canvas.bind("<Configure>", lambda _e: self._draw_safe())
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._candles: list = []
        self._error = ""
        self._fetching = False
        self._fetched_at = 0.0
        self._job = None
        self._tick()

    def _close(self) -> None:
        if self._job is not None:
            self.after_cancel(self._job)
        self.app.charts.pop(self.symbol, None)
        self.destroy()

    def _tick(self) -> None:
        if time.time() - self._fetched_at >= self.FETCH_SEC and not self._fetching:
            self._start_fetch()
        self._draw_safe()
        self._job = self.after(self.REDRAW_MS, self._tick)

    def _start_fetch(self) -> None:
        self._fetching = True
        settings = load_settings()

        def work() -> None:
            try:
                candles = self.app.api.klines(
                    self.symbol, max(50, settings.level_lookback_candles) + 1, interval=settings.level_interval
                )
                self._candles, self._error = candles, ""
            except Exception as exc:
                self._error = str(exc)
            finally:
                self._fetched_at = time.time()
                self._fetching = False

        threading.Thread(target=work, daemon=True).start()

    def _draw_safe(self) -> None:
        try:
            self._draw()
        except Exception as exc:  # a drawing problem must never break the app
            self.info.set(f"Chart error: {exc}")

    def _message(self, width: float, height: float, text: str) -> None:
        self.canvas.create_text(width / 2, height / 2, fill="#9aa4af", text=text)

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        width, height = max(c.winfo_width(), 300), max(c.winfo_height(), 160)
        settings = load_settings()
        monitor = self.app.monitor
        if monitor is None or not monitor.is_alive():
            self._message(width, height, "Start monitoring to see the cascades.")
            self.info.set("")
            return
        if not self._candles:
            self._message(width, height, self._error or "Loading candles...")
            return
        _closed, levels, last_price = monitor.chart_data(self.symbol, 10)
        price = self._candles[-1].close or last_price
        scene = build_scene(self._candles, levels, price, width, height)
        if not scene.candles:
            self._message(width, height, "No data for this coin yet.")
            return
        right = width - 124
        for x, y_high, y_low, y_open, y_close, is_up in scene.candles:
            color = "#0ecb81" if is_up else "#f6465d"
            c.create_line(x, y_high, x, y_low, fill=color)
            half = scene.body_width / 2
            c.create_rectangle(x - half, min(y_open, y_close), x + half, max(y_open, y_close) + 1, fill=color, outline=color)
        for ln in scene.lines:
            color = "#f6465d" if ln.side == "resistance" else "#0ecb81"
            c.create_line(ln.x_start, ln.y, right, ln.y, fill=color, width=2 if ln.nearest else 1)
            for px, py in ln.points:
                c.create_oval(px - 3, py - 3, px + 3, py + 3, outline=color, fill="#1e2329", width=2)
            label = f"{'R' if ln.side == 'resistance' else 'S'} {ln.price:.6g}"
            if ln.nearest:
                label += f"  x{ln.touches}  {ln.score:.0f}"
            c.create_text(right + 6, ln.y, anchor="w", fill=color, font=("Segoe UI", 9, "bold" if ln.nearest else "normal"),
                          text=label)
        if scene.price_y is not None:
            c.create_line(8, scene.price_y, right, scene.price_y, fill="#f0b90b")
            c.create_text(right + 6, scene.price_y - 11, anchor="w", fill="#f0b90b", font=("Segoe UI", 9, "bold"),
                          text=f"{price:.6g}")
        hidden = sum(1 for lv in levels if not (scene.low <= lv.price <= scene.high))
        self.info.set(
            f"{settings.level_interval} candles, {len(self._candles)} shown (the whole searched history). "
            "Only levels the price has never traded through are drawn. Every extreme of a cascade is its own line, "
            "starting at that extreme (circle). R resistance / S support. Thick line = the line the price is approaching; "
            "x = extremes in the cascade, then the score."
            + (f" {hidden} cascade(s) outside the view are not drawn." if hidden else "")
        )


class JournalWindow(tk.Toplevel):
    """What the price did after every notification: statistics and the list of signals, CSV export."""

    COLUMNS = [
        ("time", "Time", 130), ("symbol", "Ticker", 100), ("side", "Level", 85), ("level", "Level price", 95),
        ("price", "Price", 95), ("score", "Score", 50), ("trend", "Trend", 80), ("outcome", "Outcome", 90),
    ]

    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Signals journal")
        self.geometry("820x520+140+100")
        self.summary_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.summary_var, justify="left", font=BOLD).pack(anchor="w", padx=10, pady=(10, 4))
        self.tree = ttk.Treeview(self, columns=[c[0] for c in self.COLUMNS], show="headings")
        for key, title, width in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10)
        ttk.Label(
            self, justify="left", wraplength=780,
            text=(f"After {{minutes}} minutes every signal gets an outcome: bounced = the price reached the cascade and moved "
                  "away by 2 average candles; broke = a candle closed beyond the cascade; touched = reached it, no decision yet; "
                  "not_reached = never got there. Bounce rate = bounced / (bounced + broke)."
                  ).replace("{minutes}", str(load_settings().journal_check_minutes)),
        ).pack(anchor="w", padx=10, pady=4)
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bottom, text="Export to CSV...", command=self.export).pack(side="right")
        ttk.Button(bottom, text="Refresh", command=self.refresh).pack(side="right", padx=6)
        self.refresh()

    def refresh(self) -> None:
        entries = self.app.journal.entries()
        stats = summarize(entries)
        lines = [f"Signals: {stats['total']}   (waiting for evaluation: {stats['pending']})"]
        for name, key in (("All", "all"), ("Resistance", "resistance"), ("Support", "support")):
            part = stats[key]
            rate = part["bounce_rate"]
            lines.append(
                f"{name}: " + ", ".join(f"{o} {part[o]}" for o in OUTCOMES)
                + (f"   |   bounce rate {rate:.0f}%" if rate is not None else "")
            )
        self.summary_var.set("\n".join(lines))
        self.tree.delete(*self.tree.get_children())
        for e in reversed(entries[-300:]):
            self.tree.insert("", "end", values=(
                time.strftime("%Y-%m-%d %H:%M", time.localtime(float(e.get("ts", 0)))),
                e.get("symbol", ""), side_text(str(e.get("side", ""))), f"{float(e.get('level', 0)):.8g}",
                f"{float(e.get('price', 0)):.8g}", f"{float(e.get('score', 0)):.0f}",
                TREND_TEXT.get(str(e.get("trend", "")), ""), e.get("outcome") or "pending",
            ))

    def export(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", initialfile="signals.csv", filetypes=[("CSV", "*.csv")]
        )
        if not path:
            return
        try:
            count = self.app.journal.export_csv(path)
            messagebox.showinfo(APP_NAME, f"{count} signal(s) saved to {path}", parent=self)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save the file: {exc}", parent=self)


class SettingsDialog(tk.Toplevel):
    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.resizable(False, False)
        self.transient(app)

        settings = load_settings()
        self.vars: dict[str, tk.StringVar] = {}
        self.types: dict[str, tuple] = {}
        self.labels: dict[str, str] = {}
        self.bool_vars: dict[str, tk.BooleanVar] = {}

        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)

        form = None
        row = 0
        for key, label, py_type, minimum in SETTINGS_FIELDS:
            if label is None:   # a new tab
                form = ttk.Frame(notebook, padding=10)
                form.columnconfigure(0, weight=1)
                notebook.add(form, text=key)
                row = 0
                continue
            var = tk.StringVar()
            value = getattr(settings, key)
            var.set(f"{value:g}" if isinstance(value, float) else str(value))
            self.vars[key] = var
            self.types[key] = (py_type, minimum)
            self.labels[key] = label
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 16))
            if key in CHOICES:
                ttk.Combobox(form, textvariable=var, values=CHOICES[key], width=10, state="readonly").grid(
                    row=row, column=1, sticky="e", pady=3)
            else:
                ttk.Entry(form, textvariable=var, width=12).grid(row=row, column=1, sticky="e", pady=3)
            row += 1

        # check boxes and Vataga go to the last tab
        row += 1
        for key, label in BOOL_FIELDS:
            var = tk.BooleanVar(value=bool(getattr(settings, key)))
            self.bool_vars[key] = var
            ttk.Checkbutton(form, text=label, variable=var).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
            row += 1

        ttk.Label(form, text="Vataga.terminal", font=BOLD).grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 2))
        row += 1
        vrow = ttk.Frame(form)
        vrow.grid(row=row, column=0, columnspan=2, sticky="we")
        self.vtype_var = tk.StringVar(value=settings.vataga_type)
        self.vport_var = tk.StringVar(value=str(settings.vataga_port))
        ttk.Label(vrow, text="Market").pack(side="left")
        ttk.Combobox(vrow, textvariable=self.vtype_var, values=["F", "S"], width=3, state="readonly").pack(side="left", padx=4)
        ttk.Label(vrow, text="(F = futures, S = spot)   Port").pack(side="left")
        ttk.Entry(vrow, textvariable=self.vport_var, width=7).pack(side="left", padx=4)
        ttk.Button(vrow, text="Test Vataga", command=self.test_vataga).pack(side="right")

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=6)

    def read(self) -> Settings | None:
        values: dict = {}
        for key, var in self.vars.items():
            py_type, minimum = self.types[key]
            raw = var.get().strip().replace(",", ".")
            if py_type is str:
                if key in CHOICES and raw not in CHOICES[key]:
                    messagebox.showerror(APP_NAME, f"'{self.labels[key]}': choose one of {', '.join(CHOICES[key])}.", parent=self)
                    return None
                values[key] = raw
                continue
            try:
                value = py_type(raw)
            except ValueError:
                messagebox.showerror(APP_NAME, f"'{self.labels[key]}' must be a valid number.", parent=self)
                return None
            if value < minimum:
                messagebox.showerror(APP_NAME, f"'{self.labels[key]}' must be at least {minimum:g}.", parent=self)
                return None
            values[key] = value
        if values.get("min_level_score", 0) > 100:
            messagebox.showerror(APP_NAME, "The cascade score is at most 100.", parent=self)
            return None
        try:
            port = int(self.vport_var.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Port must be a number from 1 to 65535.", parent=self)
            return None
        for key, var in self.bool_vars.items():
            values[key] = var.get()
        return Settings(
            vataga_type=self.vtype_var.get() or "F",
            vataga_port=port,
            vataga_partner_id=load_settings().vataga_partner_id,
            **values,
        )

    def save(self) -> None:
        settings = self.read()
        if settings is None:
            return
        try:
            save_settings(settings)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save settings: {exc}", parent=self)
            return
        self.app.add_log("Settings saved")
        self.app.settings_saved()
        self.destroy()

    def test_vataga(self) -> None:
        settings = self.read()
        if settings is None:
            return
        ok, message = vataga.open_symbol("BTCUSDT", settings)
        if ok:
            messagebox.showinfo(APP_NAME, "Request sent. BTCUSDT should open in the window linked to 'Screener'.", parent=self)
        else:
            messagebox.showerror(APP_NAME, message, parent=self)


class App(tk.Tk):
    NEAR_COLUMNS = [
        ("ticker", "Ticker", 105),
        ("level", "Level price", 95),
        ("dist_pct", "Dist %", 60),
        ("dist_c", "Dist, candles", 85),
        ("score", "Score", 50),
        ("touches", "Extremes", 65),
        ("vol24", "24h vol, M", 80),
        ("since", "Since", 55),
    ]
    CLOSEST_COLUMNS = [
        ("rank", "#", 28),
        ("ticker", "Ticker", 105),
        ("level", "Level price", 95),
        ("dist_pct", "Dist %", 60),
        ("dist_c", "Dist, candles", 85),
        ("score", "Score", 50),
        ("touches", "Extremes", 65),
        ("vol24", "24h vol, M", 80),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("1000x840+60+30")
        self.minsize(860, 660)

        self.api = BinanceApi()
        self.monitor: Monitor | None = None
        self.messages: queue.Queue = queue.Queue()
        self.last_settings = load_settings()
        self.journal = Journal(journal_path())
        self.charts: dict[str, ChartWindow] = {}
        self.selected_symbol: str | None = None
        self.tray = Tray(
            APP_NAME,
            on_show=lambda: self.messages.put(("tray", "show")),
            on_check=lambda: self.messages.put(("tray", "check")),
            on_exit=lambda: self.messages.put(("tray", "exit")),
        )

        self._build_widgets()
        notifier.alert_listener = lambda symbol, title, message, direction=0: self.messages.put(
            ("popup", (symbol, title, message, direction))
        )

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_unmap)
        self.after(200, self._drain_messages)
        if self.last_settings.autostart_monitoring:
            self.after(500, self.start_monitoring)

    # ------------------------------------------------------------------ UI

    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text="Binance Futures: cascades of levels", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        self.status_var = tk.StringVar(value="Stopped")
        ttk.Label(root, textvariable=self.status_var, wraplength=940).pack(anchor="w", pady=(2, 8))

        buttons = ttk.Frame(root)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="Start monitoring", command=self.start_monitoring)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop_monitoring, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        self.check_button = ttk.Button(buttons, text="Check now", command=self.check_now, state="disabled")
        self.check_button.pack(side="left")
        ttk.Button(buttons, text="Settings", command=self.open_settings).pack(side="right")
        ttk.Button(buttons, text="Journal", command=self.open_journal).pack(side="right", padx=6)

        paned = ttk.PanedWindow(root, orient="vertical")
        paned.pack(fill="both", expand=True, pady=(10, 4))

        near_frame = ttk.LabelFrame(paned, text=" 1. Approaching a cascade ", padding=6)
        self.heading_var = tk.StringVar(value="Waiting for the first check...")
        ttk.Label(near_frame, textvariable=self.heading_var, font=BOLD, wraplength=900).pack(anchor="w")
        self.updated_var = tk.StringVar(value="")
        ttk.Label(near_frame, textvariable=self.updated_var).pack(anchor="w")
        self.near_list = SymbolTree(
            near_frame, self.NEAR_COLUMNS, self.open_symbol, self.copy_symbol, self.select_symbol, self.open_chart, height=8
        )
        self.near_list.pack(fill="both", expand=True, pady=(4, 0))
        paned.add(near_frame, weight=1)

        closest_frame = ttk.LabelFrame(paned, text=" 2. Closest to a cascade ", padding=6)
        self.closest_heading_var = tk.StringVar(value="")
        ttk.Label(closest_frame, textvariable=self.closest_heading_var, font=BOLD, wraplength=900).pack(anchor="w")
        self.closest_list = SymbolTree(
            closest_frame, self.CLOSEST_COLUMNS, self.open_symbol, self.copy_symbol, self.select_symbol, self.open_chart, height=5
        )
        self.closest_list.pack(fill="both", expand=True, pady=(4, 0))
        paned.add(closest_frame, weight=1)

        self.hint_var = tk.StringVar(
            value="Double-click a ticker: open in Vataga.terminal. \"Chart\": chart with the cascades. "
                  "\"Copy\": copy the ticker."
        )
        ttk.Label(root, textvariable=self.hint_var, wraplength=940).pack(anchor="w")

        tools = ttk.Frame(root)
        tools.pack(fill="x", pady=(8, 2))
        ttk.Label(tools, text="Log", font=BOLD).pack(side="left")
        ttk.Button(tools, text="Test notification", command=self.test_notification).pack(side="right")
        ttk.Button(tools, text="Alerts log", command=self.open_alerts_log).pack(side="right", padx=6)
        ttk.Button(tools, text="Close all popups", command=popups.close_all).pack(side="right")
        self.log_widget = scrolledtext.ScrolledText(root, height=6, state="disabled", wrap="word")
        self.log_widget.pack(fill="both", expand=False)

    # ------------------------------------------------------------- actions

    def open_settings(self) -> None:
        SettingsDialog(self)

    def open_journal(self) -> None:
        JournalWindow(self)

    def select_symbol(self, symbol: str) -> None:
        self.selected_symbol = symbol

    def open_chart(self, symbol: str) -> None:
        window = self.charts.get(symbol)
        if window is not None:
            try:
                window.lift()
                return
            except tk.TclError:
                self.charts.pop(symbol, None)
        self.charts[symbol] = ChartWindow(self, symbol)

    def settings_saved(self) -> None:
        self.last_settings = load_settings()
        if self.monitor is not None and self.monitor.is_alive():
            self.monitor.check_now()  # apply the new conditions right away

    def start_monitoring(self) -> None:
        if self.monitor is not None and self.monitor.is_alive():
            return
        self.monitor = Monitor(
            api=self.api,
            load_settings=load_settings,
            on_results=lambda near, closest, since, now: self.messages.put(("results", (near, closest, since, now))),
            on_alert=lambda row, settings: notifier.show_level(
                row.symbol, row.side, row.level, row.price, row.distance_percent, row.distance_candles,
                row.touches, sound=settings.sound_enabled, score=row.score, trend=TREND_TEXT.get(row.trend, ""),
            ),
            log=lambda text: self.messages.put(("log", text)),
            set_status=lambda text: self.messages.put(("status", text)),
            warn=lambda title, text: notifier.show_warning(title, text, sound=load_settings().sound_enabled),
            state_path=alert_times_path(),
            journal=self.journal,
        )
        self.monitor.start()
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.check_button.config(state="normal")
        self.status_var.set("Starting: the first check downloads candles of all coins and takes a little longer...")

    def stop_monitoring(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
        self.stop_button.config(state="disabled")
        self.check_button.config(state="disabled")

    def check_now(self) -> None:
        if self.monitor is not None and self.monitor.is_alive():
            self.monitor.check_now()
            self.status_var.set("Checking now...")

    def copy_symbol(self, symbol: str) -> None:
        """Put the ticker (e.g. INITUSDT) on the clipboard."""
        try:
            self.clipboard_clear()
            self.clipboard_append(symbol)
            self.update_idletasks()
            self.hint_var.set(f"{symbol} copied to the clipboard")
        except tk.TclError as exc:
            self.hint_var.set(f"Could not copy {symbol}: {exc}")

    def open_symbol(self, symbol: str) -> None:
        """Open a coin in Vataga.terminal (called on double-click; the request runs in a thread)."""
        settings = load_settings()
        self.hint_var.set(f"Sending {symbol} to Vataga.terminal...")

        def work() -> None:
            ok, message = vataga.open_symbol(symbol, settings)
            self.messages.put(("hint", f"{symbol} opened in Vataga.terminal" if ok else message))

        threading.Thread(target=work, daemon=True).start()

    def test_notification(self) -> None:
        try:
            notifier.show_test(sound=load_settings().sound_enabled)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not show notification: {exc}")

    def open_alerts_log(self) -> None:
        path = alerts_log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]  (Windows only)
        except (OSError, AttributeError) as exc:
            messagebox.showerror(APP_NAME, f"Could not open {path}: {exc}")

    # ---------------------------------------------------------------- tray

    def _hide_to_tray(self) -> bool:
        """Hide the window and show the tray icon. False if the tray is not available (nothing is hidden)."""
        if not self.tray.start():
            return False
        self.withdraw()
        self.hint_var.set("The app keeps running in the system tray. Double-click the icon to show the window.")
        return True

    def _show_from_tray(self) -> None:
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def _on_unmap(self, event) -> None:
        if event.widget is self and load_settings().minimize_to_tray:
            self.after(100, self._minimized_check)

    def _minimized_check(self) -> None:
        if self.state() == "iconic":
            self._hide_to_tray()

    def _on_close(self) -> None:
        if load_settings().close_to_tray and self._hide_to_tray():
            return
        self._quit()

    def _quit(self) -> None:
        monitor = self.monitor
        if monitor is not None:
            monitor.stop()
            monitor.join(timeout=2.0)   # let it finish, so that nothing is written to the log after it is emptied
        self.tray.stop()
        if load_settings().clear_alerts_log_on_exit:
            clear_alerts_log()
        self.destroy()

    # ---------------------------------------------------------------- misc

    def add_log(self, text: str) -> None:
        self.log_widget.config(state="normal")
        self.log_widget.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        lines = int(self.log_widget.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_widget.delete("1.0", f"{lines - MAX_LOG_LINES + 1}.0")
        self.log_widget.see("end")
        self.log_widget.config(state="disabled")

    @staticmethod
    def _row_values(r) -> tuple:
        return (
            r.symbol, f"{r.level:.8g}", f"{r.distance_percent:.2f}", f"{r.distance_candles:.2f}",
            f"{r.score:.0f}", r.touches, fmt_million(r.volume_24h),
        )

    def _show_results(self, near, closest, since, now) -> None:
        s = load_settings()
        self.heading_var.set(
            f"Distance to a cascade <= {s.approach_candles:g} average 1-min candle(s)"
            + (", not moving away" if s.approach_filter_enabled else "")
            + f" (24h volume > {s.min_volume_million:g}M, NATR >= {s.min_natr:g}%, trades >= {s.min_trades}"
            + (f", score >= {s.min_level_score:g}" if s.min_level_score > 0 else "") + ")"
        )
        self.updated_var.set(f"Updated {time.strftime('%H:%M:%S', time.localtime(now))}; {len(near)} coin(s) match")
        self.near_list.set_rows(
            [self._row_values(r) + (time.strftime("%H:%M", time.localtime(since.get(r.key, now))),) for r in near]
        )
        tolerance = (
            f"tolerance {s.level_tolerance_candles:g} avg candles" if s.tolerance_mode == "candles"
            else f"tolerance {s.level_tolerance_percent:g}%"
        )
        self.closest_heading_var.set(
            f"The {s.closest_count} coins closest to a cascade of at least {s.min_extremes} extremes"
            + (f", not farther than {s.closest_max_candles:g} candles" if s.closest_max_candles > 0 else "") + " "
            f"({s.level_interval} candles, {tolerance})"
        )
        self.closest_list.set_rows([(i + 1,) + self._row_values(r) for i, r in enumerate(closest)])

    def _drain_messages(self) -> None:
        """Move messages from the monitor thread into the UI. One bad message never stops the loop."""
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "log":
                    self.add_log(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                    self.tray.set_title(f"{APP_NAME}: {payload}")
                elif kind == "hint":
                    self.hint_var.set(payload)
                elif kind == "results":
                    self._show_results(*payload)
                elif kind == "popup":
                    popups.show_popup(self, *payload)
                elif kind == "tray":
                    if payload == "show":
                        self._show_from_tray()
                    elif payload == "check":
                        self.check_now()
                    elif payload == "exit":
                        self._quit()
                        return
            except Exception as exc:
                try:
                    self.add_log(f"UI error ({kind}): {exc}")
                except Exception:
                    pass

        if self.monitor is not None and not self.monitor.is_alive():
            self.monitor = None
            self.start_button.config(state="normal")
            self.stop_button.config(state="disabled")
            self.check_button.config(state="disabled")
        self.after(200, self._drain_messages)


def main() -> None:
    # Toast button click: Windows starts the app with binancelevels://open?symbol=XXXUSDT
    if len(sys.argv) > 1 and vataga.handle_url(sys.argv[1]):
        return
    vataga.register_protocol()
    App().mainloop()
