#!/usr/bin/env python3
"""Backtest our own 5-min snapshots (liqmap.sqlite). Needs >= ~3-5 days of logging to mean anything.
For every snapshot x coin x band x signal (depth bid/ask, HL real liq, CEX est liq) and ratio threshold:
  barrier test : within H (4h/24h), does price hit +X% or -X% first? prediction = toward the bigger side
                 (liq: bigger long-liq below => down; depth: bigger bids below => down i.e. 'magnet' reading;
                  hit% << 50 for depth means walls act as support/resistance instead).
  cluster test : touch-rate of heaviest bucket on dominant side vs heaviest bucket on the other side (control).
Prices: Binance 1m klines (high/low) fetched for the logged window.
usage: python3 backtest.py [--min-gap 30]   (min minutes between samples used, default 30 to cut overlap)
"""
import sqlite3, json, sys, os, math, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from liqcore import get, DB, BANDS, STEP, band_of  # DB = spike liqmap.sqlite (legacy continuous series)

BARRIER = {"S 0-1%": 1.0, "M- 1-3%": 2.0, "M 3-5%": 4.0, "L 5-10%": 7.5}
HORIZ = {"4h": 240, "24h": 1440}
THR = (1.0, 1.5, 2.0, 3.0)
SIGNALS = {"depth": ("bid", "ask"), "hl_liq": ("hl_long", "hl_short"), "cex_liq": ("cex_long", "cex_short")}


def klines(coin, t0, t1):
    out, s = [], t0 * 1000
    while s < t1 * 1000:
        k = get(f"https://fapi.binance.com/fapi/v1/klines?symbol={coin}USDT&interval=1m&startTime={s}&limit=1500")
        if not k: break
        out += [(int(x[0]) // 1000, float(x[2]), float(x[3])) for x in k]
        s = int(k[-1][0]) + 60_000
        time.sleep(0.2)
    return out


def main():
    gap = int(sys.argv[sys.argv.index("--min-gap") + 1]) * 60 if "--min-gap" in sys.argv else 1800
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT ts, coin, mid, json FROM buckets ORDER BY ts").fetchall()
    if not rows: print("no snapshots yet"); return
    span_h = (rows[-1][0] - rows[0][0]) / 3600
    print(f"{len(rows)} coin-snapshots spanning {span_h:.1f}h")
    if span_h < 48: print("WARNING: <48h of data - results are noise. Target >= 72-120h.")
    stats = {}
    for coin in sorted({r[1] for r in rows}):
        snaps = [r for r in rows if r[1] == coin]
        kl = klines(coin, snaps[0][0], int(time.time()))
        idx = {t: i for i, (t, _, _) in enumerate(kl)}
        last = -1e18
        for ts, _, mid, js in snaps:
            if ts - last < gap: continue
            last = ts
            m = {k: {float(a): b for a, b in v.items()} for k, v in json.loads(js).items()}
            i0 = idx.get(ts - ts % 60)
            if i0 is None: continue
            for sig, (lo_k, hi_k) in SIGNALS.items():
                for b, _, _ in BANDS:
                    lo = {k: v for k, v in m[lo_k].items() if band_of(k) == b and k < 0}
                    hi = {k: v for k, v in m[hi_k].items() if band_of(k) == b and k >= 0}
                    A, B = sum(lo.values()), sum(hi.values())
                    if A == 0 or B == 0: continue
                    r = max(A / B, B / A); pred = "dn" if A > B else "up"
                    tgt = max(lo, key=lo.get) if pred == "dn" else max(hi, key=hi.get)
                    ctl = max(hi, key=hi.get) if pred == "dn" else max(lo, key=lo.get)
                    tp, cp = mid * (1 + (tgt + STEP / 2) / 100), mid * (1 + (ctl + STEP / 2) / 100)
                    X = BARRIER[b]; up, dn = mid * (1 + X / 100), mid * (1 - X / 100)
                    for hn, H in HORIZ.items():
                        if i0 + H >= len(kl): continue
                        fh, tt, ct = None, False, False
                        for j in range(i0 + 1, i0 + 1 + H):
                            _, h, l = kl[j]
                            tt = tt or (l <= tp <= h); ct = ct or (l <= cp <= h)
                            if fh is None:
                                if h >= up and l <= dn: fh = "both"
                                elif h >= up: fh = "up"
                                elif l <= dn: fh = "dn"
                        for t in THR:
                            if r < t: continue
                            s = stats.setdefault((sig, b, hn, t), dict(n=0, hit=0, miss=0, tt=0, ct=0))
                            s["n"] += 1; s["tt"] += tt; s["ct"] += ct
                            if fh == pred: s["hit"] += 1
                            elif fh in ("up", "dn"): s["miss"] += 1
    print(f"{'signal':8} {'band':8} {'H':>3} {'ratio>=':>7} {'n':>5} {'res':>4} {'hit%':>6} {'~z':>5} | {'tgtTouch%':>9} {'ctlTouch%':>9}")
    order = [x[0] for x in BANDS]
    for (sig, b, hn, t), s in sorted(stats.items(), key=lambda kv: (kv[0][0], order.index(kv[0][1]), kv[0][2], kv[0][3])):
        res = s["hit"] + s["miss"]; hp = s["hit"] / res if res else float("nan")
        neff = max(1, res * gap / 60 / HORIZ[hn])
        z = (hp - .5) / math.sqrt(.25 / neff) if res else float("nan")
        print(f"{sig:8} {b:8} {hn:>3} {t:>7} {s['n']:>5} {res:>4} {hp*100:6.1f} {z:5.2f} | {s['tt']/s['n']*100:9.1f} {s['ct']/s['n']*100:9.1f}")
    print("Edge = hit% well above 50 with |z|>2 AND tgtTouch% > ctlTouch%, stable across coins/thresholds. Else no edge.")


if __name__ == "__main__":
    main()
