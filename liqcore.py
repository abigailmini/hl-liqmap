#!/usr/bin/env python3
"""hl-liqmap core (vendored from spikes/liq-map/liqmap.py, 2026-10-25).
Changes vs spike: keeps per-wallet HL positions (addr), raw HL l2Book levels and HL mark prices on the
snapshot dict so collector.py can build wallet-capped HL maps. Legacy tables (bands/buckets) are still written
to the spike DB (LEGACY_DB) unchanged so backtest.py keeps working on one continuous series.

Original docstring: cross-exchange liquidity + liquidation map (BTC/ETH/SOL).

Two datasets, bucketed by % distance from mid in 0.25% steps, summed per band:
  1) Order-book depth (resting bids/asks) - Binance fapi, OKX books-full, Bybit v5, Hyperliquid l2Book (multi-nSigFigs)
  2) Liquidation levels
     - HL REAL: clearinghouseState for a sample of leaderboard addresses (liquidationPx x positionValue)
     - CEX EST: model from Binance 1h OI history + price path + assumed leverage tiers, scaled to Binance+OKX+Bybit OI

usage: python3 liqmap.py            # print snapshot tables
       python3 liqmap.py --log      # also append to liqmap.sqlite
       python3 liqmap.py --hl-n 200 # fewer HL addresses (faster)
"""
import json, math, os, sys, time, sqlite3, threading
from concurrent.futures import ThreadPoolExecutor
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
SPIKE = os.path.expanduser("~/.openclaw/workspace-samantha/spikes/liq-map")
DB = os.environ.get("HL_LIQMAP_LEGACY_DB", os.path.join(SPIKE, "liqmap.sqlite"))
LB_CACHE = os.path.join(SPIKE, ".leaderboard_cache.json")
HL = "https://api.hyperliquid.xyz/info"
COINS = ["BTC", "ETH", "SOL"]
STEP = 0.25           # bucket width, % of mid
MAXD = 10.0           # ignore beyond +/-10%
BANDS = [("S 0-1%", 0, 1), ("M- 1-3%", 1, 3), ("M 3-5%", 3, 5), ("L 5-10%", 5, 10)]
# CEX model assumptions (documented in README)
LEV_TIERS = {5: .10, 10: .25, 20: .25, 25: .15, 50: .15, 100: .10}
MMR = 0.005
S = requests.Session()
S.headers["User-Agent"] = "hl-liqmap/0.2"


def get(url, **kw):
    for i in range(3):
        try:
            r = S.get(url, timeout=15, **kw)
            if r.status_code == 429:
                time.sleep(2 + 3 * i); continue
            r.raise_for_status(); return r.json()
        except requests.RequestException:
            if i == 2: raise
            time.sleep(1 + i)


class Limiter:
    def __init__(self, per_sec): self.dt, self.t, self.l = 1.0 / per_sec, 0.0, threading.Lock()
    def wait(self):
        with self.l:
            now = time.time(); self.t = max(self.t + self.dt, now); d = self.t - now
        if d > 0: time.sleep(d)


HL_LIM = Limiter(6)  # HL info: 1200 weight/min, l2Book & clearinghouseState = 2 weight -> ~10/s max; stay at 6


def hl(body):
    for i in range(4):
        HL_LIM.wait()
        try:
            r = S.post(HL, json=body, timeout=15)
            if r.status_code == 429:
                time.sleep(5 * (i + 1)); continue
            r.raise_for_status(); return r.json()
        except requests.RequestException:
            if i == 3: return None
            time.sleep(1 + i)
    return None


def bucket(px, mid):
    d = (px / mid - 1) * 100
    if abs(d) > MAXD: return None
    return round(math.floor(d / STEP) * STEP, 4)  # lower edge of bucket


def add(mp, k, v):
    if k is not None: mp[k] = mp.get(k, 0.0) + v


# ---------------- depth ----------------
def depth_binance(c):
    j = get(f"https://fapi.binance.com/fapi/v1/depth?symbol={c}USDT&limit=1000")
    return [(float(p), float(q)) for p, q in j["bids"]], [(float(p), float(q)) for p, q in j["asks"]]


_okx_ct = {}
def depth_okx(c):
    inst = f"{c}-USDT-SWAP"
    if inst not in _okx_ct:
        _okx_ct[inst] = float(get(f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={inst}")["data"][0]["ctVal"])
    ct = _okx_ct[inst]
    d = get(f"https://www.okx.com/api/v5/market/books-full?instId={inst}&sz=5000")["data"][0]
    return [(float(l[0]), float(l[1]) * ct) for l in d["bids"]], [(float(l[0]), float(l[1]) * ct) for l in d["asks"]]


def depth_bybit(c):
    d = get(f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={c}USDT&limit=500")["result"]
    return [(float(p), float(q)) for p, q in d["b"]], [(float(p), float(q)) for p, q in d["a"]]


def depth_hl(c):
    """HL l2Book returns only 20 levels/side; stitch nSigFigs 5->2 so coarser books fill the range beyond finer ones."""
    bids, asks, lo, hi = [], [], None, None
    for nsf in (5, 4, 3, 2):
        j = hl({"type": "l2Book", "coin": c, "nSigFigs": nsf})
        if not j: continue
        b = [(float(x["px"]), float(x["sz"])) for x in j["levels"][0]]
        a = [(float(x["px"]), float(x["sz"])) for x in j["levels"][1]]
        if lo is None:
            bids, asks = b, a
        else:
            # coarse bid level px aggregates [px, px+step); keep only levels fully outside the finer book's range
            sb = (b[0][0] - b[1][0]) if len(b) > 1 else 0
            sa = (a[1][0] - a[0][0]) if len(a) > 1 else 0
            bids += [(p, q) for p, q in b if p + sb <= lo]
            asks += [(p, q) for p, q in a if p - sa >= hi]
        if bids: lo = min(p for p, _ in bids)
        if asks: hi = max(p for p, _ in asks)
    return bids, asks


DEPTH_SRC = {"binance": depth_binance, "okx": depth_okx, "bybit": depth_bybit, "hl": depth_hl}


# ---------------- HL real liquidation levels ----------------
def hl_addresses(n_val=350, n_vol=250):
    if os.path.exists(LB_CACHE) and time.time() - os.path.getmtime(LB_CACHE) < 6 * 3600:
        return json.load(open(LB_CACHE))
    rows = get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")["leaderboardRows"]
    by_val = sorted(rows, key=lambda r: -float(r["accountValue"]))[:n_val]
    vol = lambda r: float(dict(r["windowPerformances"])["month"]["vlm"])
    by_vol = sorted(rows, key=lambda r: -vol(r))[:n_vol]
    addrs = list(dict.fromkeys([r["ethAddress"] for r in by_val + by_vol]))
    json.dump(addrs, open(LB_CACHE, "w"))
    return addrs


def hl_positions(addrs):
    out = []  # (coin, side, liqPx|None, notional, addr, leverage)
    lock = threading.Lock()
    def one(a):
        s = hl({"type": "clearinghouseState", "user": a})
        if not s: return 0
        for ap in s.get("assetPositions", []):
            p = ap["position"]
            if p["coin"] in COINS:
                lp = float(p["liquidationPx"]) if p.get("liquidationPx") else None
                with lock:
                    lev = (p.get("leverage") or {}).get("value")
                    out.append((p["coin"], "long" if float(p["szi"]) > 0 else "short", lp, float(p["positionValue"]), a, lev))
        return 1
    with ThreadPoolExecutor(6) as ex:
        ok = sum(ex.map(one, addrs))
    return out, ok


def hl_oi():
    meta, ctxs = hl({"type": "metaAndAssetCtxs"})
    res = {}
    for u, c in zip(meta["universe"], ctxs):
        if u["name"] in COINS:
            res[u["name"]] = float(c["openInterest"]) * float(c["markPx"])
            HL_MARKS[u["name"]] = float(c["markPx"])
    return res


HL_MARKS = {}


# ---------------- CEX liquidation estimate (model) ----------------
def binance_hist(c, limit=500, end_ms=None):
    q = f"&endTime={end_ms}" if end_ms else ""
    oi = get(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={c}USDT&period=1h&limit={limit}{q}")
    kl = get(f"https://fapi.binance.com/fapi/v1/klines?symbol={c}USDT&interval=1h&limit={min(1500, limit + 2)}{q}")
    k = {int(x[0]): (float(x[2]), float(x[3]), float(x[4])) for x in kl}  # open_time -> high, low, close
    rows = []
    for o in oi:  # OI stamp = end of hour -> pair with bar that opened 1h earlier
        t = int(o["timestamp"]) - 3600_000
        if t in k: rows.append((t, float(o["sumOpenInterest"]), *k[t]))
    return rows  # (t, oi_coins, high, low, close)


def cex_model(rows):
    """Coinglass-style heuristic. Each hourly OI increase opens equal long & short notional at the bar close,
    spread over LEV_TIERS; OI decreases close all cohorts pro-rata; cohorts whose liq px was traded through are removed.
    Returns cohorts: list of [side, liqPx, notional_usd]."""
    coh, prev = [], None
    for t, oi, h, l, c in rows:
        coh = [x for x in coh if not ((x[0] == "long" and l <= x[1]) or (x[0] == "short" and h >= x[1]))]
        if prev is not None:
            d = oi - prev
            if d > 0:
                for L, w in LEV_TIERS.items():
                    coh.append(["long", c * (1 - 1 / L + MMR), d * c * w / 2])
                    coh.append(["short", c * (1 + 1 / L - MMR), d * c * w / 2])
            elif d < 0:
                tot = sum(x[2] for x in coh)
                f = max(0.0, 1 + (d * c) / tot) if tot else 1.0
                for x in coh: x[2] *= f
        prev = oi
    return coh


def cex_oi_usd(c):
    b = get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={c}USDT")
    px = float(get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={c}USDT")["markPrice"])
    bin_usd = float(b["openInterest"]) * px
    okx = float(get(f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={c}-USDT-SWAP")["data"][0]["oiUsd"])
    byb = float(get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={c}USDT")["result"]["list"][0]["openInterestValue"])
    return bin_usd, bin_usd + okx + byb


# ---------------- assemble ----------------
def band_of(k):
    ctr = abs(k + STEP / 2)
    for name, a, b in BANDS:
        if a <= ctr < b: return name
    return None


def snapshot(hl_n=600):
    t0 = time.time()
    mids, maps = {}, {}
    for c in COINS:
        mids[c] = float(get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={c}USDT")["markPrice"])
        maps[c] = {k: {} for k in ("bid", "ask", "hl_long", "hl_short", "cex_long", "cex_short")}
        maps[c]["coverage"] = {}
    for c in COINS:
        mid = mids[c]
        for name, fn in DEPTH_SRC.items():
            try:
                bids, asks = fn(c)
            except Exception as e:
                print(f"[warn] depth {name} {c}: {e}", file=sys.stderr); continue
            if name == "hl": maps[c]["hl_book"] = {"bids": bids, "asks": asks}
            for p, q in bids: add(maps[c]["bid"], bucket(p, mid), p * q)
            for p, q in asks: add(maps[c]["ask"], bucket(p, mid), p * q)
            if bids and asks:
                maps[c]["coverage"][name] = round((max(p for p, _ in asks) / min(p for p, _ in bids) - 1) * 50, 2)
    addrs = hl_addresses()[:hl_n]
    pos, ok = hl_positions(addrs)
    oi = hl_oi()
    for c in COINS:
        mine = [x for x in pos if x[0] == c]
        tot = sum(x[3] for x in mine)
        maps[c]["hl_sample_pos_usd"] = tot
        maps[c]["hl_oi_usd"] = oi.get(c, 0)
        maps[c]["hl_cov"] = tot / (2 * oi[c]) if oi.get(c) else 0
        maps[c]["hl_noliq_usd"] = sum(x[3] for x in mine if x[2] is None)
        for _, side, lpx, v, *_ in mine:
            if lpx is None: continue
            k = bucket(lpx, mids[c])
            if side == "long" and lpx < mids[c]: add(maps[c]["hl_long"], k, v)
            elif side == "short" and lpx > mids[c]: add(maps[c]["hl_short"], k, v)
    for c in COINS:
        try:
            coh = cex_model(binance_hist(c))
            bin_usd, tot_usd = cex_oi_usd(c)
            scale = tot_usd / bin_usd
            for side, lpx, v in coh:
                k = bucket(lpx, mids[c])
                if side == "long" and lpx < mids[c]: add(maps[c]["cex_long"], k, v * scale)
                elif side == "short" and lpx > mids[c]: add(maps[c]["cex_short"], k, v * scale)
            maps[c]["cex_oi_usd"] = tot_usd
        except Exception as e:
            print(f"[warn] cex model {c}: {e}", file=sys.stderr)
    return {"ts": int(time.time()), "mids": mids, "maps": maps, "hl_addr_ok": ok, "hl_addr_n": len(addrs),
            "secs": round(time.time() - t0, 1), "positions": pos, "hl_marks": dict(HL_MARKS)}


KEYS = ("bid", "ask", "hl_long", "hl_short", "cex_long", "cex_short")
def bands(m):
    out = {name: {k: 0.0 for k in KEYS} for name, _, _ in BANDS}
    for key in KEYS:
        for k, v in m[key].items():
            b = band_of(float(k))
            if b: out[b][key] += v
    return out


def ratio(a, b):
    if a == 0 and b == 0: return None
    return float("inf") if b == 0 else a / b


def fm(x):
    for d, s in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if x >= d: return f"{x/d:.1f}{s}" if s != "k" else f"{x/d:.0f}k"
    return f"{x:.0f}"


def fr(r):
    return "  n/a" if r is None else ("  inf" if r == float("inf") else f"{r:5.2f}")


def print_snap(s):
    print(f"snapshot {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s['ts']))}  ({s['secs']}s, HL addrs ok {s['hl_addr_ok']}/{s['hl_addr_n']})")
    for c in COINS:
        m = s["maps"][c]; b = bands(m)
        print(f"\n== {c}  mid {s['mids'][c]:,.2f} | HL sample {fm(m['hl_sample_pos_usd'])} of {fm(2*m['hl_oi_usd'])} HL OI(2-sided) = {m['hl_cov']*100:.0f}% (no-liqPx {fm(m['hl_noliq_usd'])}) | CEX OI {fm(m.get('cex_oi_usd',0))}")
        print(f"   depth range covered (+/-%): {m['coverage']}")
        print(f"   {'band':8}| {'bid $':>7} | {'ask $':>7} | {'b/a':>5} | {'HL L-liq':>8} | {'HL S-liq':>8} | {'L/S':>5} | {'CEX L':>7} | {'CEX S':>7} | {'L/S':>5}")
        for name, _, _ in BANDS:
            x = b[name]
            print(f"   {name:8}| {fm(x['bid']):>7} | {fm(x['ask']):>7} | {fr(ratio(x['bid'],x['ask']))} | {fm(x['hl_long']):>8} | {fm(x['hl_short']):>8} | {fr(ratio(x['hl_long'],x['hl_short']))} | {fm(x['cex_long']):>7} | {fm(x['cex_short']):>7} | {fr(ratio(x['cex_long'],x['cex_short']))}")
        for key in ("hl_long", "hl_short", "cex_long", "cex_short"):
            if m[key]:
                k = max(m[key], key=m[key].get)
                print(f"   top {key:9} bucket {k:+.2f}% (~{s['mids'][c]*(1+(k+STEP/2)/100):,.1f}) {fm(m[key][k])}")


def log(s):
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS bands(ts INT, coin TEXT, mid REAL, band TEXT, bid REAL, ask REAL,
                   hl_long REAL, hl_short REAL, cex_long REAL, cex_short REAL, hl_cov REAL)""")
    con.execute("CREATE TABLE IF NOT EXISTS buckets(ts INT, coin TEXT, mid REAL, json TEXT)")
    for c in COINS:
        m = s["maps"][c]
        for name, x in bands(m).items():
            con.execute("INSERT INTO bands VALUES (?,?,?,?,?,?,?,?,?,?,?)", (s["ts"], c, s["mids"][c], name, x["bid"], x["ask"],
                        x["hl_long"], x["hl_short"], x["cex_long"], x["cex_short"], m["hl_cov"]))
        con.execute("INSERT INTO buckets VALUES (?,?,?,?)", (s["ts"], c, s["mids"][c],
                    json.dumps({k: {str(kk): round(vv) for kk, vv in m[k].items()} for k in KEYS})))
    con.commit(); con.close()


if __name__ == "__main__":
    n = int(sys.argv[sys.argv.index("--hl-n") + 1]) if "--hl-n" in sys.argv else 600
    s = snapshot(hl_n=n)
    if "--quiet" not in sys.argv: print_snap(s)
    if "--log" in sys.argv: log(s); print(f"logged {time.strftime('%F %T')} -> {DB} ({s['secs']}s)")
