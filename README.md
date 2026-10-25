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
`collector.py` snapshot+store · `forward_test.py` paper forward test · `model.py` cap/bands/clusters/signal (pure) · `liqcore.py` fetchers (vendored spike) ·
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

## GitHub Pages (phone access)
- Live: https://abigailmini.github.io/hl-liqmap/  (branch `gh-pages`, static)
- `collector.py` calls `publish.py` after each 5-min run: exports `data/snapshot.json` (latest, all coins) + `data/history.json` (rolling 24h, compact) into `./pages/` (gitignored checkout), amends the single gh-pages commit and force-pushes (timeout 60s, failures logged as `[warn] publish failed`, never fatal). gh-pages is always 1 commit → repo never grows.
- Dashboard fetches `data/*.json?t=<ms>` (relative); local `server.py` serves the same paths. Stale banner if snapshot > 15 min old.
- Expect ~1–3 min Pages propagation per update. Skip publishing: `collector.py --no-publish`.

## Forward test (paper trades, 2026-10-25 → 2026-11-01)
`forward_test.py` turns every scan into mechanical paper trades from a fixed strategy matrix, resolves them against the
real HL price path, and reports which strategies have edge. Coins BTC, SOL, ETH. Gross R, no fees/slippage.

| launchd label | what | schedule | log |
|---|---|---|---|
| `com.samantha.hl-forward-scan` | `forward_test.py cron-scan` = scan + resolve + daily + json | 08:00, 12:00, 16:00, 21:00 KL | `logs/forward.log` |
| `com.samantha.hl-forward-resolve` | `forward_test.py cron-resolve` = resolve + daily + json | hourly at :30 | `logs/forward.log` |

Scan reads the latest collector snapshot from local sqlite (no HL calls; skips with reason if snapshot >20 min old).
Resolve makes 1 HL `candleSnapshot` (1m) call per coin per run, incremental, cached in the `candles` table → no collision
with the collector's rate budget. Scans stop after Nov 1 (WINDOW_END); resolve keeps closing trades (L trades live 72h).

Files: `data/forward_test.sqlite` (tables `trades`, `scans`, `candles`) · `data/forward_report.md` (report) ·
`data/forward_daily_YYYY-MM-DD.md` (1–3 line daily summary, rewritten every scan/resolve — Abi's cron reads it) ·
`data/forward.json` (dashboard; also exported to Pages by publish.py) · dashboard section "Forward test" (bottom).

    python3 forward_test.py scan [--coins BTC,SOL] [--force]   # manual scan (1 per KL hour slot; --force adds missing rows)
    python3 forward_test.py resolve
    python3 forward_test.py report [--min-n 5]                 # -> data/forward_report.md
    python3 forward_test.py show [--scan "2026-10-25 16"]       # counts + live rows of a scan
    python3 forward_test.py daily

### Strategy matrix (108 rows / coin / scan, every row logged: status open | skip + skip_reason)
`strategy_id = SCN-BAND-rTHR-wW-rrX`, e.g. `MAG-M-r2.0-w3-rr1.0`
- **scenario**: MAGNET / FADE / CASCADE. **band** (where clusters are searched + expiry): S 0–1% (8h), M 1–5% (24h), L 5–10% (72h).
- **ratio_thr** 1.5 / 2.0 / 3.0 on capped dominant/other liq $ inside the band (other side 0 → ∞).
- **wallet** w3 (≥3 distinct wallets on dominant side in band) / w0 (no filter — added for all bands, not only S, because
  SOL rarely has ≥3 wallets per band and w0 is needed to get ≥10 live trades/coin). **rr** rr0 / rr1 (skip if R:R < 1.0).
- Dominant side = side of the band with more capped liq $ (long-liq below ⇒ down-magnet ⇒ SHORT).
- **MAGNET**: market @ mark toward dominant side. TP = largest dominant bucket in band. SL = 0.1% past nearest opposite
  obstacle (liq bucket ≥25% of opposite max, or HL book wall ≥3× median 0.5–5%) within S 1% / M,L 5%; fallback 0.5/1.5/3%.
- **FADE** (conditional limit): entry @ nearest big dominant-side level in band (liq bucket ≥25% of band max, or book wall),
  direction AGAINST the magnet. TP = 50% retrace to the scan mark. SL = 0.1% past the level's far bucket edge.
- **CASCADE** (conditional stop-entry): entry @ nearest big dominant-side liq bucket, WITH the magnet. TP = next liq bucket
  further out same side (≥25% of trigger $, ≤10%). SL = 0.1% back through the trigger bucket's near edge.
- Thresholds/filters are nested, so many rows share one setup — the report compares variants; the dashboard merges them.

### Resolution rules
1m candles with open ≥ scan time, until expiry. First touch; TP and SL in same candle → SL. Conditional entries: the trigger
candle never counts TP; it counts SL if SL lies beyond the entry along the approach (FADE, candle extreme) else only if the
candle *closes* past SL (CASCADE — the high/low before the trigger is ambiguous). Expiry: triggered → `expired`, exit at last
close, R = signed move/risk; never triggered → `expired_untriggered` (excluded from win%, shown as fill rate).
Win = +R:R, loss = −1R. Report: n, win%, Wilson 95% CI, exp R (mean R), ΣR, max DD (R, by exit time), median time-to-exit,
fill, plus marginals per scenario/band/threshold/wallet/rr/coin and top/bottom-5 strategy_id (n≥5).

### Extending strategies
1. Add a scenario: new key in `SCEN` (3-letter code) and a branch in `build(scn, band, ctx, mid)` returning
   `{direction, entry, tp, sl, cluster_usd, cluster_px, conditional, notes}` (or `{"skip": reason}`). `ctx` has the
   band's dominant side/map, all capped liq buckets (`lc`, `sc`) and book (`bid`, `ask`), keyed by 0.25% bucket lower edge.
   `conditional=1` makes the resolver wait for entry touch.
2. Add a dimension value: extend `BANDS` / `THRS` / `WALLETS` / `RRF` (strategy_id is generated from them). New ids start
   accumulating from the next scan; old rows are untouched (UNIQUE(scan_id, coin, strategy_id)).
3. Don't change the meaning of an existing id mid-test — add a new id instead, or the stats mix two rules.
