"""User-editable settings, stored as JSON in %APPDATA%\\BinanceLevelsMonitor\\settings.json."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_NAME = "Binance Levels Monitor"
DATA_DIR_NAME = "BinanceLevelsMonitor"

LEVEL_INTERVALS = ["1m", "3m", "5m", "15m", "30m", "1h"]
TOLERANCE_MODES = ["candles", "percent"]


@dataclass
class Settings:
    """Defaults match the requested conditions."""

    # --- Coin filters ---
    min_volume_million: float = 30.0          # 24h quote volume must be MORE than this (million USDT)
    min_natr: float = 0.0                     # NATR of the last activity window, at least (%); 0 = off
    min_trades: int = 0                       # trades within the last activity window, at least; 0 = off
    activity_window_minutes: int = 15         # "last N minutes" for NATR and the number of trades

    # --- Cascades (levels) ---
    level_interval: str = "5m"                # candle size used to find extremes
    level_lookback_candles: int = 288         # how many candles of history to search (288 x 5m = 24 h)
    swing_strength: int = 3                   # an extreme must beat this many candles on its left
    swing_confirm_candles: int = 3            # ...and this many candles on its right (a fresh extreme is recognised after N candles)
    min_extremes: int = 2                     # at least this many extremes make a cascade
    tolerance_mode: str = "candles"           # "candles" = tolerance adapts to the coin, "percent" = fixed percent
    level_tolerance_candles: float = 3.0      # mode "candles": extremes within N average 1-minute candles are one cascade
    level_tolerance_percent: float = 0.2      # mode "percent": extremes within this distance (%) are one cascade
    level_break_candles: float = 0.5          # a level is broken if a wick goes beyond it by more than N average 1-min candles
    min_level_score: float = 0.0              # ignore cascades weaker than this (0-100; 0 = off)
    levels_refresh_minutes: int = 5           # how often the cascades are recalculated

    # --- Approach ---
    approach_candles: float = 1.0             # notify when the distance <= N x the average 1-minute candle
    avg_candle_minutes: int = 60              # the average 1-minute candle is measured over this period
    approach_filter_enabled: bool = True      # skip coins whose price is moving AWAY from the cascade
    approach_lookback_minutes: int = 3        # compare the distance now with the distance this many minutes ago
    away_tolerance_candles: float = 0.25      # distance must grow by more than this (average candles) to be "away"

    # --- Notifications / lists ---
    closest_count: int = 5                    # size of the "closest to a cascade" list
    closest_max_candles: float = 5.0          # the "closest" list shows only coins not farther than N avg candles (0 = no limit)
    cooldown_minutes: int = 30                # don't re-notify the same coin and side within this time
    check_interval_seconds: int = 60          # how often prices are re-checked
    autostart_monitoring: bool = False
    sound_enabled: bool = True
    popup_enabled: bool = True                # always-on-top popup with buttons
    toast_enabled: bool = True                # Windows toast notification
    popup_auto_close_sec: int = 0             # 0 = popups stay until closed

    # --- Window, tray, journal ---
    minimize_to_tray: bool = True             # minimizing the window hides it to the tray (monitoring continues)
    close_to_tray: bool = False               # the X button hides the window instead of exiting
    clear_alerts_log_on_exit: bool = True     # empty alerts.log when the app is closed
    journal_check_minutes: int = 30           # how long after a signal its outcome is evaluated

    # --- Vataga.terminal (double-click a ticker) ---
    vataga_type: str = "F"                    # F = futures, S = spot
    vataga_port: int = 5005
    vataga_partner_id: str = "BinanceLevelsMonitor"

    @property
    def min_volume_usdt(self) -> float:
        return self.min_volume_million * 1_000_000.0


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / DATA_DIR_NAME


def settings_path() -> Path:
    return data_dir() / "settings.json"


def alerts_log_path() -> Path:
    """Text file where every alert (and notification problem) is recorded."""
    return data_dir() / "alerts.log"


def clear_alerts_log() -> None:
    """Empty alerts.log (never raises)."""
    try:
        path = alerts_log_path()
        if path.exists():
            path.write_text("", encoding="utf-8")
    except OSError:
        pass


def journal_path() -> Path:
    """Journal of signals and their outcomes."""
    return data_dir() / "journal.json"


def alert_times_path() -> Path:
    """When each coin was last notified (keeps the cooldown across stop/start and restarts)."""
    return data_dir() / "alert_times.json"


def load_settings() -> Settings:
    """Load settings from disk. Missing or broken values fall back to the defaults."""
    defaults = Settings()
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    if not isinstance(data, dict):
        return defaults

    values = {}
    for field in fields(Settings):
        if field.name not in data:
            continue
        expected_type = type(getattr(defaults, field.name))
        try:
            values[field.name] = expected_type(data[field.name])
        except (TypeError, ValueError):
            pass
    if values.get("level_interval", defaults.level_interval) not in LEVEL_INTERVALS:
        values["level_interval"] = defaults.level_interval
    if values.get("tolerance_mode", defaults.tolerance_mode) not in TOLERANCE_MODES:
        values["tolerance_mode"] = defaults.tolerance_mode
    return Settings(**values)


def save_settings(settings: Settings) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
