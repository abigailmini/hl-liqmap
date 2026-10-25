# hl-liqmap — Hyperliquid liquidation-map dashboard (BTC / ETH / SOL)

Local-only MVP promoted from `spikes/liq-map` (2026-10-25). **Signal is EXPERIMENTAL until the backtest gate passes.**

URL: **http://127.0.0.1:8790/**  (bound to 127.0.0.1 only — not public)
JSON: `GET /api/snapshot` (all coins; `?coin=BTC` for one) · `GET /api/history?coin=BTC&hours=24` · `GET /api/health`

## How it runs
| launchd label | what | schedule | log |
|---|---|---|---|
| `com.samantha.liqmap-spike` | `collector.py` — one snapshot per run (~100 s) | StartInterval 300 s + RunAtLoad | `logs/collector.log` |
| `com.samantha.hl-liqmap-web` | `server.py` — stdlib HTTP server on 127.0.0.1:8790 | KeepAlive | `logs/web.log` |

The collector job **kept the spike's label** (so the existing logger "keeps running") but now runs the project
collector instead of `spikes/liq-map/liqmap.py`. One process does all HL fetching (no doubled API load / 429s).
Original spike plist backed up at `data/com.samantha.liqmap-spike.plist.orig`. Plists live in `launchd/` and are copied
to `~/Library/LaunchAgents/`.

Each snapshot:
1. `liqcore.snapshot()` (vendored spike code): ~560 HL leaderboard wallets → `clearinghouseState` (liqPx × positionValue,
   per wallet), HL `l2Book` stitched nSigFigs 5→2, HL mark/OI, plus Binance/OKX/Bybit depth + CEX liq model (legacy only).
2. `liqcore.log()` → legacy tables in `spikes/liq-map/liqmap.sqlite` (same schema as the spike → backtest series is continuous).
3. `model.compute()` per coin vs **HL mark** → `data/hl-liqmap.sqlite`:
   - `snap` (ts, coin, mid, ratios, signal JSON, full summary JSON) — per-snapshot summary
   - `buckets` (ts, coin, JSON of raw 0.25% buckets: long_raw, short_raw, long_cap, short_cap, bid, ask)
   - `runs` (ts, secs, wallets ok)

### Wallet cap
Per band, a single wallet may contribute at most **10% of that band`s raw total (long+short)**. Dashboard shows capped by
default, raw via toggle; band table always shows the other one underneath, plus wallet count `Nw` and `top X%` when one
wallet is >30% of a band side. Bands with <6 wallets are dimmed as "thin".

### Signal (EXPERIMENTAL — rule-based, not validated)
- Dominant side = capped 1–5% (M- + M bands) long-liq vs short-liq, ratio ≥ 2, and ≥ 3 distinct wallets on the dominant side.
  Long-liq heavier below → SHORT bias (magnet down); short-liq heavier above → LONG bias. Otherwise "NO SIGNAL".
- TP = largest capped liq bucket on the dominant side within 1–10%.
- SL = 0.1% past the nearest opposite-side obstacle within 0–5% (liq bucket ≥25% of that side's max, or HL book bucket at 0.5–5% ≥3× median); fallback 1.5%.
- Tier = TP band: S (<3%), M (3–5%), L (5–10%). R:R shown.

Data caveats: sample covers ~21–28% of HL OI (leaderboard wallets only, no aggregate liq endpoint exists); HL book is 20
levels per nSigFigs so depth >1% is coarse; walls are spoofable. CEX liq estimate is kept only in the legacy DB.

## Stop / restart
    U=gui/$(id -u)
    launchctl kickstart -k $U/com.samantha.hl-liqmap-web       # restart web
    launchctl kickstart $U/com.samantha.liqmap-spike           # force a snapshot now
    launchctl bootout   $U/com.samantha.hl-liqmap-web           # stop web
    launchctl bootout   $U/com.samantha.liqmap-spike            # stop collector
    launchctl bootstrap $U ~/Library/LaunchAgents/<label>.plist # start again
    launchctl list | grep liqmap                                # status (PID / last exit)
Revert collector to the pure spike: copy `data/com.samantha.liqmap-spike.plist.orig` over the LaunchAgents plist, bootout+bootstrap.
Manual run: `python3 collector.py --print` (prints bands/clusters/signal). Note: do not run while the launchd job is mid-run (HL rate limit).

## Files
`collector.py` snapshot+store · `model.py` cap/bands/clusters/signal (pure) · `liqcore.py` fetchers (vendored spike) ·
`server.py` HTTP/API · `static/index.html` dashboard (vanilla JS + canvas) · `backtest.py` go/no-go · `data/` DBs · `logs/`

## Go / no-go backtest (run after 3–5 days of logging, i.e. ≥ 2026-10-28)
    cd ~/.openclaw/workspace-samantha/projects/hl-liqmap
    python3 backtest.py                # default --min-gap 30 (minutes between samples)
Reads the legacy continuous series (spikes/liq-map/liqmap.sqlite, logging since 2026-10-25 15:03). Look at rows
`signal=hl_liq`, bands `M- 1-3%` and `M 3-5%`, ratio ≥ 2 / ≥ 3, H = 4h and 24h:
- **GO** if hit% ≥ 58 with z > 2 on **≥ 2 coins**, AND tgtTouch% > ctlTouch% → drop the EXPERIMENTAL label, consider alerts.
- **NO-GO** otherwise → kill the signal box (keep the map as context only, or stop both jobs).
Caveat: legacy `hl_liq` is the **raw (uncapped)** series. A capped-series backtest (from `data/hl-liqmap.sqlite` buckets
long_cap/short_cap) is a deferred TODO — the dashboard signal uses capped values, so check both before trusting it.
