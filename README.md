# Binance Levels Monitor 1.3.0 (Windows)

Scans all Binance USDT-M perpetual futures (public endpoints only, no API keys) and warns when the price
comes close to a **cascade of levels**.

## Definitions
- **Extreme** - a swing high / swing low: a candle whose high (low) is the highest (lowest) among N candles on its
  left (default 3) and M candles on its right ("confirm", default 3). The shorter right side lets a fresh extreme
  (e.g. a bounce 15 minutes ago) join a cascade without waiting for N more candles. Default: 5m candles, 288 of them (24 h); both are configurable.
- **Cascade** - at least 2 extremes (configurable) of the same kind close to each other. Highs form a
  *resistance* cascade, lows a *support* cascade. 
- **Lines of a cascade** - every extreme is its own line. A cascade is a stack of lines. The signal is based on the
  NEAREST line that is still ahead of the price (support: the highest low below the price; resistance: the lowest high
  above it), not on the most distant one. The most extreme line (lowest low / highest high) decides if the cascade is broken.
- **Broken levels are dropped.** A level is alive only if, after the OLDEST extreme of its cascade, no candle went beyond
  it - a wick counts, not only a close (a poke smaller than 0.5 average 1-minute candles is allowed; setting
  "Level is broken if a wick passes it by N avg candles"). So only levels the price has never traded through are shown.
- **Tolerance** (how close extremes must be to form a cascade): by default **3 average 1-minute candles**, so it
  adapts to the volatility of the coin (limited to 0.05 %...2 %). Mode "percent" = fixed % (default 0.2 %).
- **Score 0-100** of a cascade: up to 50 for the number of extremes (5+ = full), up to 20 for how long the cascade
  has existed, up to 30 for the volume on the extreme candles vs the usual candle (3x+ = full).
  "Minimum cascade score" (default 0 = off) hides weak cascades everywhere.
- **Average 1-minute candle** - mean (high - low) of the 1-minute candles of the last hour.
- **Approach** - distance from the price to the cascade <= 1 average 1-minute candle (configurable).
- **Trend** - the distance now vs 3 minutes ago: *Closing*, *Moving away* or *Flat* (a change smaller than
  0.25 average candles is Flat). With "Do not notify when moving away" (default on) a coin that is moving away
  is not put into list 1 and gives no notification. It is still shown in list 2.

## Window
1. **Approaching a cascade** - new entries trigger sound + popup + Windows toast (with score and trend).
   Cooldown 30 min per coin and side (saved on disk).
2. **Closest to a cascade** - the 5 nearest coins, but not farther than 5 average candles (setting "'Closest' list: not farther than N avg candles", 0 = no limit). No notifications.

Columns: Ticker, Chart, Copy, Level price, Dist %, Dist (candles), Score, Extremes, 24h volume, Since.

Double-click a ticker -> opens it in Vataga.terminal. Every row has two cells right after the ticker:
**Chart** (chart of the coin) and **Copy** (copies the ticker). Right-click on a row also opens the chart.

### Chart
Shows the whole searched history (default 288 candles of 5m), including the candle that is still forming
(candles are loaded for the chart only, 1 request every 15 s). A cascade is drawn so that you can see what it is made of:
- every extreme is **its own plain line** that starts at that extreme and runs to the right edge;
- a small **circle** marks the extreme the line starts from;
- the **thick** line is the one the price is approaching (nearest ahead of it).
Label: `R`/`S` (resistance / support) and the price; the thick line also shows `x3` = extremes in the cascade and the score.

## Journal (button "Journal")
Every notification is recorded; 30 minutes later (configurable) its outcome is decided on 1-minute candles:
- **bounced** - the price reached the cascade and moved away from it by 2 average candles;
- **broke** - a candle closed beyond the cascade;
- **touched** - reached it, but neither bounced nor broke;
- **not_reached** - never got there.
Statistics per side and the bounce rate (bounced / (bounced + broke)) are shown; "Export to CSV" saves the list.
Use it to tune the settings (tolerance, minimum score, distance) instead of guessing.

## System tray
Settings > "Minimizing the window hides it to the system tray" (default on) and
"The X button hides the window to the tray instead of exiting" (default off). Monitoring continues in the tray;
the icon menu: Show window / Check now / Exit; the tooltip shows the status. Notifications and popups still appear.
(If the tray package is missing, the window is just minimized to the taskbar.)

## Settings (tabs)
Filters: 24h volume (30M USDT), NATR (0 = off), trades in the last 15 min (0 = off).
Cascades: candle size, history, swing strength, minimum extremes, tolerance, minimum score.
Approach: distance, trend, cooldown, check interval. Notifications and window: popups, tray, chart, journal.

## Vataga.terminal
Settings > General > "Application integration" on; link the window to the "Screener" group.
Partner id `BinanceLevelsMonitor`, port 5005 (Settings -> Test Vataga).

## Build
Upload the folder (including hidden `.github`) to GitHub -> Actions -> "Build Windows installer" ->
download `BinanceLevelsMonitor-Setup`. Or locally: `build.bat` (Python + Inno Setup 6).

## Traffic
REST only. Every check: 1 request for all tickers + a few new 1-minute candles per coin. Cascades are recalculated
every 5 minutes (1 request per coin with more than 30M volume, weight 2). The journal adds 1 small request per signal.

## Alerts log
The button "Alerts log" opens alerts.log. The file is emptied when the app is closed (Settings > "Empty the alerts log when the app is closed", default on). The journal is separate and is kept.

## Data
`%APPDATA%\BinanceLevelsMonitor\` : settings.json, alerts.log, alert_times.json, journal.json.

## Tests
`python -m unittest discover -s tests`

## Tips for tuning
- **Tolerance** decides which extremes form one cascade. In "candles" mode it follows the volatility of the coin; if highs
  that are visibly far apart get merged, lower "Tolerance in average 1-min candles" (try 0.3-0.5).
- **Extreme must beat N candles on its left** (default 3): large values (e.g. 10) keep only major swings.
- **Level is broken if a wick passes it by N avg candles**: 0 is the strictest.
