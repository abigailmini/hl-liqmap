#!/usr/bin/env python3
"""hl-liqmap FORWARD TEST (paper trades). Week 2026-10-25 -> 2026-11-01 (KL).

  python3 forward_test.py scan [--coins BTC,SOL,ETH] [--force]   one scan: every strategy x coin -> 1 row (open|skip)
  python3 forward_test.py resolve                               mark open trades vs HL 1m candles (first touch)
  python3 forward_test.py report [--min-n 5] [--out FILE]       markdown report (default data/forward_report.md)
  python3 forward_test.py cron-scan                             scan + resolve + daily + json   (launchd 08/12/16/21)
  python3 forward_test.py cron-resolve                          resolve + daily + json          (launchd hourly :30)
  python3 forward_test.py daily | json | show [--scan ID]

Reads the latest collector snapshot from data/hl-liqmap.sqlite (no HL load). Only resolve hits HL:
1 candleSnapshot call per coin per run (incremental, cached in data/forward_test.sqlite table candles).

Strategy matrix (all rows logged, skips carry skip_reason):
  scenario  MAGNET | FADE | CASCADE
  band      S 0-1% (8h) | M 1-5% (24h) | L 5-10% (72h)      (band = where the clusters are searched + expiry)
  ratio_thr 1.5 | 2.0 | 3.0     dominant/other capped liq $ inside the band (other=0 -> inf)
  wallet    w3 (>=3 distinct wallets on dominant side in band) | w0 (no filter)
  rr        rr0 (none) | rr1 (skip if R:R < 1.0)
  strategy_id = SCN-BAND-rTHR-wW-rrX   e.g. MAG-M-r2.0-w3-rr1.0      -> 3*3*3*2*2 = 108 rows / coin / scan
Geometry (dominant side = side of the band with more capped liq $; long-liq below => "down magnet"):
  MAGNET  market @ mark, toward dominant side. TP = largest dominant bucket in band (centre).
          SL = 0.1% past nearest opposite obstacle (liq bucket >=25% of opp max within 0-5%, or HL book wall
          >=3x median 0.5-5%) within S:1% / M,L:5%; fallback S 0.5% / M 1.5% / L 3%.
  FADE    LIMIT @ nearest big dominant-side level in band (liq bucket >=25% of band max, or book wall), AGAINST the
          magnet (long-liq below -> buy the flush). TP = 50% retrace level->scan mark ("back to mid").
          SL = 0.1% past the level's far edge.
  CASCADE STOP-entry @ nearest big dominant-side liq bucket in band, WITH the magnet. TP = next liq bucket further
          out same side (>=25% of trigger $, <=10%). SL = 0.1% back through the trigger bucket's near edge.
Resolution (1m candles, candle open >= scan ts, until expiry):
  first touch; TP+SL same candle -> SL. Conditional entries: trigger candle never counts TP; SL counts in the trigger
  candle if SL lies beyond the entry in the approach direction (FADE: extreme), else only if the candle CLOSES past SL
  (CASCADE). Expiry: triggered -> exit at last close, R = signed move / risk ("expired"); untriggered ->
  "expired_untriggered" (R null, excluded from win%). Win R = +R:R, loss R = -1. Gross, no fees/slippage.
"""
import json, math, os, sqlite3, statistics, sys, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server as S  # noqa: E402  (snapshot reader, read-only)

DB = os.path.join(HERE, "data", "forward_test.sqlite")
DATA = os.path.join(HERE, "data")
HL = "https://api.hyperliquid.xyz/info"
KL = timezone(timedelta(hours=8))
COINS = ["BTC", "SOL", "ETH"]
WINDOW_START = int(datetime(2026, 10, 25, 0, 0, tzinfo=KL).timestamp())
WINDOW_END = int(datetime(2026, 11, 2, 0, 0, tzinfo=KL).timestamp())     # last scheduled scan Nov 1 21:00
MAX_SNAP_AGE = 1200
STEP = 0.25
SL_PAD = 0.001
BIG = 0.25
SCEN = {"MAGNET": "MAG", "FADE": "FAD", "CASCADE": "CAS"}
#        band: (lo%, hi%, expiry h, SL search range %, SL fallback %, model bands)
BANDS = {"S": (0, 1, 8, 1.0, 0.5, ("S",)), "M": (1, 5, 24, 5.0, 1.5, ("M-", "M")), "L": (5, 10, 72, 5.0, 3.0, ("L",))}
THRS = (1.5, 2.0, 3.0)
WALLETS = (3, 0)
RRF = (0.0, 1.0)
CLOSED = ("win", "loss", "expired")

COLS = ["scan_id", "scan_ts", "snap_ts", "coin", "strategy_id", "scenario", "band", "ratio_thr", "wallet_filter",
        "rr_filter", "direction", "entry", "tp", "sl", "rr", "expiry_ts", "status", "skip_reason", "snapshot_ratio",
        "dom_side", "cluster_usd", "cluster_px", "n_wallets", "mark", "conditional", "notes"]


def db():
    os.makedirs(DATA, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, scan_id TEXT, scan_ts INT, snap_ts INT,
        coin TEXT, strategy_id TEXT, scenario TEXT, band TEXT, ratio_thr REAL, wallet_filter INT, rr_filter REAL,
        direction TEXT, entry REAL, tp REAL, sl REAL, rr REAL, expiry_ts INT, status TEXT, skip_reason TEXT,
        snapshot_ratio REAL, dom_side TEXT, cluster_usd REAL, cluster_px REAL, n_wallets INT, mark REAL,
        conditional INT, notes TEXT, triggered_ts INT, exit_ts INT, exit_px REAL, r_realised REAL, tte_min REAL,
        resolved_at INT, UNIQUE(scan_id, coin, strategy_id))""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_status ON trades(status)")
    con.execute("CREATE TABLE IF NOT EXISTS candles(coin TEXT, t INT, o REAL, h REAL, l REAL, c REAL, PRIMARY KEY(coin, t))")
    con.execute("CREATE TABLE IF NOT EXISTS scans(scan_id TEXT PRIMARY KEY, scan_ts INT, kind TEXT, rows INT, live INT, note TEXT)")
    return con


def kl(ts, fmt="%Y-%m-%d %H:%M"):
    return datetime.fromtimestamp(ts, KL).strftime(fmt)


# ---------------------------------------------------------------- geometry helpers (bucket k = lower edge, % from mid)
def ctr(k): return k + STEP / 2
def kpx(mid, k, where="mid"): return mid * (1 + (k + {"lo": 0, "mid": STEP / 2, "hi": STEP}[where]) / 100)
def fkeys(m): return {float(k): v for k, v in (m or {}).items() if v}


def walls(book, rng):
    """book wall buckets: 0.5-5% bucket >= 3x median nonzero bucket (0.5-5%), restricted to |ctr| < rng"""
    bm = {k: v for k, v in book.items() if 0.5 <= abs(ctr(k)) < 5 and v > 0}
    if len(bm) < 3: return {}
    med = sorted(bm.values())[len(bm) // 2]
    return {k: v for k, v in bm.items() if v >= 3 * med and abs(ctr(k)) < rng}


def band_ctx(d, band):
    lo, hi = BANDS[band][0], BANDS[band][1]
    B = d.get("buckets") or {}
    inb = lambda k: lo <= abs(ctr(k)) < hi
    lc, sc = fkeys(B.get("long_cap")), fkeys(B.get("short_cap"))
    lng = {k: v for k, v in lc.items() if k < 0 and inb(k)}
    sht = {k: v for k, v in sc.items() if k >= 0 and inb(k)}
    L, Sh = sum(lng.values()), sum(sht.values())
    ctx = {"L": L, "S": Sh, "lc": lc, "sc": sc, "bid": fkeys(B.get("bid")), "ask": fkeys(B.get("ask"))}
    if L <= 0 and Sh <= 0:
        return {**ctx, "dom": None, "ratio": None}
    dom = "long" if L >= Sh else "short"
    a, b = (L, Sh) if dom == "long" else (Sh, L)
    ratio = float("inf") if b <= 0 else a / b
    bands = {x["band"]: x for x in d["bands"]}
    key = "n_long" if dom == "long" else "n_short"
    mb = BANDS[band][5]
    if band == "M":   # exact unique wallets across 1-5% when the model's signal computed it for the same side
        sg = d.get("signal") or {}
        sig_dom = None if not sg.get("long15") and not sg.get("short15") else ("long" if sg.get("long15", 0) >= sg.get("short15", 0) else "short")
        nw = sg["dom_wallets"] if sg.get("dom_wallets") is not None and sig_dom == dom else max(bands[x][key] for x in mb)
    else:
        nw = bands[mb[0]][key]
    return {**ctx, "dom": dom, "ratio": ratio, "domm": lng if dom == "long" else sht, "nw": nw}


def build(scn, band, ctx, mid):
    """-> dict(direction, entry, tp, sl, cluster_usd, cluster_px, conditional, notes) or {'skip': reason, ...}"""
    dom, domm = ctx["dom"], ctx["domm"]
    mag = "SHORT" if dom == "long" else "LONG"
    rng, fb = BANDS[band][3], BANDS[band][4]
    mx = max(domm.values())
    if scn == "MAGNET":
        tk = max(domm, key=domm.get)
        tp = kpx(mid, tk)
        opp = ctx["sc"] if mag == "SHORT" else ctx["lc"]
        opp = {k: v for k, v in opp.items() if (k >= 0) == (mag == "SHORT") and abs(ctr(k)) < 5}
        cands = []
        if opp:
            om = max(opp.values())
            cands += [(abs(ctr(k)), k, "liq", v) for k, v in opp.items() if v >= BIG * om and abs(ctr(k)) < rng]
        bw = walls(ctx["ask" if mag == "SHORT" else "bid"], rng)
        cands += [(abs(ctr(k)), k, "wall", v) for k, v in bw.items()]
        if cands:
            _, sk, kind, sv = min(cands)
            sl = kpx(mid, sk, "hi") * (1 + SL_PAD) if mag == "SHORT" else kpx(mid, sk, "lo") * (1 - SL_PAD)
            note = "SL past %s %s %+.2f%%" % (kind, fmu(sv), ctr(sk))
        else:
            sl = mid * (1 + fb / 100) if mag == "SHORT" else mid * (1 - fb / 100)
            note = "SL fallback %.1f%%" % fb
        return {"direction": mag, "entry": mid, "tp": tp, "sl": sl, "cluster_usd": domm[tk], "cluster_px": tp,
                "conditional": 0, "notes": "TP = max %s-liq bucket %s %+.2f%%; %s" % (dom, fmu(domm[tk]), ctr(tk), note)}
    # nearest big level on the dominant side, inside the band
    lv = [(abs(ctr(k)), k, "liq", v) for k, v in domm.items() if v >= BIG * mx]
    if scn == "FADE":
        lo, hi = BANDS[band][0], BANDS[band][1]
        bw = walls(ctx["bid" if dom == "long" else "ask"], hi)
        lv += [(abs(ctr(k)), k, "wall", v) for k, v in bw.items() if abs(ctr(k)) >= lo]
    _, k, kind, v = min(lv)
    entry = kpx(mid, k)
    below = k < 0
    if scn == "FADE":
        direction = "LONG" if below else "SHORT"
        sl = kpx(mid, k, "lo") * (1 - SL_PAD) if below else kpx(mid, k, "hi") * (1 + SL_PAD)
        return {"direction": direction, "entry": entry, "tp": (entry + mid) / 2, "sl": sl, "cluster_usd": v,
                "cluster_px": entry, "conditional": 1,
                "notes": "limit @ %s %s %+.2f%%; TP = 50%% retrace to scan mark" % (kind, fmu(v), ctr(k))}
    # CASCADE: next liq bucket further out on the same side (<=10%), >= BIG x trigger $
    side = ctx["lc"] if below else ctx["sc"]
    nxt = sorted((abs(ctr(j)), j) for j, w in side.items() if (j < 0) == below and abs(ctr(j)) > abs(ctr(k))
                 and abs(ctr(j)) < 10 and w >= BIG * v)
    if not nxt:
        return {"skip": "no next cluster beyond trigger %+.2f%%" % ctr(k), "direction": mag, "entry": entry,
                "cluster_usd": v, "cluster_px": entry, "conditional": 1}
    j = nxt[0][1]
    sl = kpx(mid, k, "hi") * (1 + SL_PAD) if below else kpx(mid, k, "lo") * (1 - SL_PAD)
    return {"direction": mag, "entry": entry, "tp": kpx(mid, j), "sl": sl, "cluster_usd": v, "cluster_px": entry,
            "conditional": 1, "notes": "stop-entry @ liq %s %+.2f%%; TP next liq %s %+.2f%%" % (fmu(v), ctr(k), fmu(side[j]), ctr(j))}


def fmu(x):
    return "-" if x is None else ("$%.1fM" % (x / 1e6) if x >= 1e6 else "$%.0fk" % (x / 1e3))


def strategy_id(scn, band, thr, w, rr):
    return "%s-%s-r%.1f-w%d-rr%.1f" % (SCEN[scn], band, thr, w, rr)


def all_strategies():
    return [(s, b, t, w, r) for s in SCEN for b in BANDS for t in THRS for w in WALLETS for r in RRF]


# ---------------------------------------------------------------- scan
def scan(coins=None, force=False, kind="manual"):
    coins = coins or COINS
    now = int(time.time())
    scan_id = kl(now, "%Y-%m-%d %H")            # one scan per KL hour slot
    con = db()
    if not force and con.execute("SELECT 1 FROM scans WHERE scan_id=?", (scan_id,)).fetchone():
        print(f"scan {scan_id} already done (use --force to add missing coins/rows)"); return scan_id
    snap = S.snapshot(coins)
    rows = []
    for c in coins:
        d = snap["coins"].get(c)
        stale = (d is None) or (now - d["ts"] > MAX_SNAP_AGE)
        mid = d["mid"] if d else None
        ctxs = {b: band_ctx(d, b) for b in BANDS} if not stale else {}
        for scn, band, thr, w, rrf in all_strategies():
            r = dict.fromkeys(COLS)
            r.update(scan_id=scan_id, scan_ts=now, snap_ts=d["ts"] if d else None, coin=c, mark=mid,
                     strategy_id=strategy_id(scn, band, thr, w, rrf), scenario=scn, band=band, ratio_thr=thr,
                     wallet_filter=w, rr_filter=rrf, expiry_ts=now + BANDS[band][2] * 3600, status="skip",
                     conditional=0 if scn == "MAGNET" else 1)
            rows.append(r)
            if stale:
                r["skip_reason"] = "stale/missing snapshot (%ss old)" % (now - d["ts"] if d else "-"); continue
            x = ctxs[band]
            if x["dom"] is None:
                r["skip_reason"] = "no capped liq in band"; continue
            r.update(snapshot_ratio=999.0 if x["ratio"] == float("inf") else round(x["ratio"], 3), dom_side=x["dom"],
                     n_wallets=x["nw"])
            if x["ratio"] < thr:
                r["skip_reason"] = "ratio %.2f < %.1f" % (x["ratio"], thr); continue
            if x["nw"] < w:
                r["skip_reason"] = "wallets %d < %d on dominant side" % (x["nw"], w); continue
            t = build(scn, band, x, mid)
            if "skip" in t:
                r["skip_reason"] = t.pop("skip"); r.update(t); continue
            r.update(t)
            sd = abs(t["entry"] - t["sl"]); td = abs(t["tp"] - t["entry"])
            r["rr"] = round(td / sd, 3) if sd > 0 else None
            if not sd or not td:
                r["skip_reason"] = "degenerate TP/SL"; continue
            if r["rr"] < rrf:
                r["skip_reason"] = "R:R %.2f < %.1f" % (r["rr"], rrf); continue
            r["status"] = "open"
    for r in rows:
        for k in ("entry", "tp", "sl", "cluster_px"):
            if r[k] is not None: r[k] = round(r[k], 6)
        if r["cluster_usd"] is not None: r["cluster_usd"] = round(r["cluster_usd"])
    con.executemany("INSERT OR IGNORE INTO trades(%s) VALUES (%s)" % (",".join(COLS), ",".join("?" * len(COLS))),
                    [[r[k] for k in COLS] for r in rows])
    live = sum(r["status"] == "open" for r in rows)
    con.execute("INSERT OR REPLACE INTO scans VALUES (?,?,?,?,?,?)", (scan_id, now, kind, len(rows), live, ",".join(coins)))
    con.commit(); con.close()
    per = {c: sum(r["status"] == "open" for r in rows if r["coin"] == c) for c in coins}
    print(f"scan {scan_id} ({kind}) rows={len(rows)} live={live} per-coin live={per}", flush=True)
    return scan_id


# ---------------------------------------------------------------- candles + resolve
def hl_post(body, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(HL, json.dumps(body).encode(), {"Content-Type": "application/json",
                                                                          "User-Agent": "hl-liqmap-fwd/0.1"})
            with urllib.request.urlopen(req, timeout=30) as f:
                return json.load(f)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1: time.sleep(5 * (i + 1)); continue
            raise
        except Exception:
            if i < tries - 1: time.sleep(3); continue
            raise


def fetch_candles(con, coin, since):
    last = con.execute("SELECT max(t) FROM candles WHERE coin=?", (coin,)).fetchone()[0]
    start = max(since - 120, (last or 0) - 120)       # small overlap: re-fetch the last (possibly partial) candles
    now = int(time.time())
    n = 0
    while start < now - 60:
        end = min(now, start + 4900 * 60)
        res = hl_post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1m",
                                                         "startTime": start * 1000, "endTime": end * 1000}}) or []
        con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?)",
                        [(coin, int(x["t"]) // 1000, float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"])) for x in res])
        n += len(res)
        start = end
        if start < now - 60: time.sleep(1)
    con.commit()
    return n


def walk(t, cs, now):
    """t: trade row; cs: list of (t,o,h,l,c) closed 1m candles. -> dict of resolution fields or None (still open)"""
    long_ = t["direction"] == "LONG"
    e, tp, sl, exp = t["entry"], t["tp"], t["sl"], t["expiry_ts"]
    risk = abs(e - sl)
    trig = None if t["conditional"] else t["scan_ts"]
    approach_down = e < t["mark"]                       # level below mark -> price must fall to it
    sl_beyond = (sl < e) == approach_down              # SL further along the approach (FADE) vs behind it (CASCADE)
    last = None
    for ct, o, h, l, c in cs:
        if ct < t["scan_ts"]: continue
        if ct >= exp: break
        last = (ct, c)
        if trig is None:
            if (l <= e) if approach_down else (h >= e):
                trig = ct
                hit_sl = ((l <= sl) if long_ else (h >= sl)) if sl_beyond else ((c <= sl) if long_ else (c >= sl))
                if hit_sl: return res_(t, "loss", ct + 60, sl, -1.0, trig)
            continue
        if (l <= sl) if long_ else (h >= sl): return res_(t, "loss", ct + 60, sl, -1.0, trig)
        if (h >= tp) if long_ else (l <= tp): return res_(t, "win", ct + 60, tp, t["rr"], trig)
    covered = last is not None and last[0] + 60 >= exp - 60
    if now >= exp + 180 and (covered or now >= exp + 6 * 3600):
        if trig is None: return res_(t, "expired_untriggered", exp, None, None, None)
        px = last[1] if last else e
        r = ((px - e) if long_ else (e - px)) / risk if risk else 0.0
        return res_(t, "expired", exp, px, round(r, 3), trig)
    if trig is not None and t["conditional"] and t["triggered_ts"] is None:
        return {"status": "open", "triggered_ts": trig}
    return None


def res_(t, status, ts, px, r, trig):
    return {"status": status, "exit_ts": ts, "exit_px": px, "r_realised": r, "triggered_ts": trig,
            "tte_min": round((ts - t["scan_ts"]) / 60, 1)}


def resolve(quiet=False):
    con = db()
    now = int(time.time())
    opens = con.execute("SELECT * FROM trades WHERE status='open'").fetchall()
    stats = {"open_before": len(opens), "closed": {}, "newly_triggered": 0, "candles_fetched": {}}
    for coin in sorted({t["coin"] for t in opens}):
        ts = [t for t in opens if t["coin"] == coin]
        since = min(t["scan_ts"] for t in ts)
        try:
            stats["candles_fetched"][coin] = fetch_candles(con, coin, since)
        except Exception as ex:
            print(f"[warn] candles {coin}: {ex}", file=sys.stderr, flush=True)
        cs = con.execute("SELECT t,o,h,l,c FROM candles WHERE coin=? AND t>=? AND t+60<=? ORDER BY t",
                         (coin, since - 60, now)).fetchall()
        cs = [tuple(x) for x in cs]
        memo = {}
        for t in ts:
            key = (t["scan_id"], t["direction"], t["entry"], t["tp"], t["sl"], t["expiry_ts"], t["conditional"])
            if key not in memo: memo[key] = walk(t, cs, now)
            u = memo[key]
            if not u: continue
            if u["status"] == "open":
                con.execute("UPDATE trades SET triggered_ts=? WHERE id=?", (u["triggered_ts"], t["id"]))
                stats["newly_triggered"] += 1
                continue
            con.execute("UPDATE trades SET status=?, exit_ts=?, exit_px=?, r_realised=?, triggered_ts=?, tte_min=?, "
                        "resolved_at=? WHERE id=?", (u["status"], u["exit_ts"], u["exit_px"], u["r_realised"],
                                                     u["triggered_ts"], u["tte_min"], now, t["id"]))
            stats["closed"][u["status"]] = stats["closed"].get(u["status"], 0) + 1
    con.commit()
    stats["open_after"] = con.execute("SELECT count(*) FROM trades WHERE status='open'").fetchone()[0]
    con.close()
    if not quiet: print(f"resolve {kl(now)} {json.dumps(stats)}", flush=True)
    return stats


# ---------------------------------------------------------------- stats / report
def wilson(k, n, z=1.96):
    if not n: return (0.0, 0.0)
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def agg(rows):
    cl = sorted([r for r in rows if r["status"] in CLOSED], key=lambda r: r["exit_ts"])
    n = len(cl); w = sum(r["status"] == "win" for r in cl)
    rs = [r["r_realised"] or 0.0 for r in cl]
    eq = pk = dd = 0.0
    for x in rs:
        eq += x; pk = max(pk, eq); dd = max(dd, pk - eq)
    cond = [r for r in rows if r["conditional"] and r["status"] != "skip" and r["status"] != "open"]
    trig = [r for r in cond if r["status"] in CLOSED]
    lo, hi = wilson(w, n)
    return {"n": n, "wins": w, "wr": w / n if n else None, "lo": lo, "hi": hi, "avgR": statistics.mean(rs) if rs else None,
            "sumR": sum(rs), "dd": dd, "med_h": statistics.median([r["tte_min"] for r in cl]) / 60 if cl else None,
            "open": sum(r["status"] == "open" for r in rows), "skip": sum(r["status"] == "skip" for r in rows),
            "untrig": sum(r["status"] == "expired_untriggered" for r in rows),
            "fill": (len(trig) / len(cond)) if cond else None}


def fmt_row(name, a):
    pct = lambda x: "-" if x is None else "%.0f%%" % (100 * x)
    f2 = lambda x: "-" if x is None else "%+.2f" % x
    return "| %s | %d | %d | %s | %s | %s | %s | %.2f | %s | %d | %d | %s |" % (
        name, a["n"], a["wins"], pct(a["wr"]), ("%s-%s" % (pct(a["lo"]), pct(a["hi"]))) if a["n"] else "-",
        f2(a["avgR"]), f2(a["sumR"]) if a["n"] else "-", a["dd"], "-" if a["med_h"] is None else "%.1fh" % a["med_h"],
        a["open"], a["skip"], pct(a["fill"]))


HDR = ("| %s | n closed | W | win%% | 95%% Wilson CI | exp R | sum R | maxDD R | med tte | open | skip | fill |\n"
       "|---|---|---|---|---|---|---|---|---|---|---|---|")


def table(title, groups, key):
    out = ["", "### " + title, HDR % key]
    for g in sorted(groups): out.append(fmt_row(g, agg(groups[g])))
    return out


def group(rows, f):
    g = {}
    for r in rows: g.setdefault(f(r), []).append(r)
    return g


def report(min_n=5, out=None):
    con = db()
    rows = [dict(r) for r in con.execute("SELECT * FROM trades")]
    scans = con.execute("SELECT * FROM scans ORDER BY scan_ts").fetchall()
    con.close()
    now = int(time.time())
    L = ["# HL Liq-Map forward test - report %s KL" % kl(now), "",
         "Window %s -> %s KL | scans %d | rows %d (live %d, skip %d) | closed %d | open %d | expired-untriggered %d" % (
             kl(WINDOW_START, "%b %d"), kl(WINDOW_END - 1, "%b %d"), len(scans), len(rows),
             sum(r["status"] != "skip" for r in rows), sum(r["status"] == "skip" for r in rows),
             sum(r["status"] in CLOSED for r in rows), sum(r["status"] == "open" for r in rows),
             sum(r["status"] == "expired_untriggered" for r in rows)), "",
         "_win% / exp R over closed trades (win, loss, expired-after-trigger). Win = +R:R, loss = -1R, gross (no fees). "
         "exp R = mean realised R per trade. Thresholds are nested (r3.0 in r2.0 in r1.5) and w3 in w0, so rows overlap - "
         "compare like with like. fill = share of finished conditional (FADE/CASCADE) entries that triggered._"]
    L += table("By scenario", group(rows, lambda r: r["scenario"]), "scenario")
    L += table("By scenario x coin", group(rows, lambda r: "%s %s" % (r["scenario"], r["coin"])), "scenario coin")
    L += table("By band", group(rows, lambda r: r["band"]), "band")
    L += table("By scenario x band", group(rows, lambda r: "%s-%s" % (r["scenario"][:3], r["band"])), "scn-band")
    L += table("By ratio threshold", group(rows, lambda r: ">=%.1f" % r["ratio_thr"]), "ratio")
    L += table("By wallet filter", group(rows, lambda r: "w%d" % r["wallet_filter"]), "wallets")
    L += table("By R:R filter", group(rows, lambda r: "rr>=%.1f" % r["rr_filter"]), "rr filter")
    L += table("By coin", group(rows, lambda r: r["coin"]), "coin")
    sg = group(rows, lambda r: r["strategy_id"])
    ag = {k: agg(v) for k, v in sg.items()}
    q = [k for k, a in ag.items() if a["n"] >= min_n]
    note = ""
    if not q:
        q = [k for k, a in ag.items() if a["n"] >= 1]; note = " (no strategy has n>=%d yet - showing n>=1)" % min_n
    rk = sorted(q, key=lambda k: (ag[k]["avgR"], ag[k]["n"]), reverse=True)
    L += ["", "### Top 5 strategy_id by expectancy (n>=%d)%s" % (min_n, note), HDR % "strategy_id"]
    L += [fmt_row(k, ag[k]) for k in rk[:5]] or ["| no closed trades yet |"]
    L += ["", "### Bottom 5 strategy_id by expectancy%s" % note, HDR % "strategy_id"]
    L += [fmt_row(k, ag[k]) for k in rk[::-1][:5]] or ["| no closed trades yet |"]
    L += table("All strategy_id (both coins pooled)", sg, "strategy_id")
    L += table("All strategy_id x coin", group(rows, lambda r: "%s %s" % (r["strategy_id"], r["coin"])), "strategy coin")
    sk = group([r for r in rows if r["status"] == "skip"], lambda r: " ".join((r["skip_reason"] or "").split(" ")[:2]))
    L += ["", "### Skip reasons", "| reason | rows |", "|---|---|"] + ["| %s | %d |" % (k, len(v)) for k, v in sorted(sk.items(), key=lambda x: -len(x[1]))]
    md = "\n".join(L) + "\n"
    out = out or os.path.join(DATA, "forward_report.md")
    with open(out, "w") as f: f.write(md)
    return md, out


# ---------------------------------------------------------------- dashboard json + daily
def export_json():
    con = db()
    rows = [dict(r) for r in con.execute("SELECT * FROM trades")]
    scans = [dict(r) for r in con.execute("SELECT * FROM scans ORDER BY scan_ts")]
    con.close()
    lite = lambda a: {k: (round(v, 3) if isinstance(v, float) else v) for k, v in a.items()}
    out = {"generated_ts": int(time.time()), "window": [WINDOW_START, WINDOW_END], "scans": len(scans),
           "last_scan": scans[-1] if scans else None,
           "counts": {"rows": len(rows), "open": sum(r["status"] == "open" for r in rows),
                      "pending": sum(r["status"] == "open" and r["conditional"] and r["triggered_ts"] is None for r in rows),
                      "closed": sum(r["status"] in CLOSED for r in rows),
                      "untriggered": sum(r["status"] == "expired_untriggered" for r in rows),
                      "skip": sum(r["status"] == "skip" for r in rows)},
           "scenario": {}, "setups": {}}
    for scn in SCEN:
        sr = [r for r in rows if r["scenario"] == scn]
        out["scenario"][scn] = {"ALL": lite(agg(sr)), **{c: lite(agg([r for r in sr if r["coin"] == c])) for c in COINS},
                                **{b: lite(agg([r for r in sr if r["band"] == b])) for b in BANDS}}
    if scans:
        ls = scans[-1]["scan_id"]
        for c in COINS:
            u = {}
            for r in rows:
                if r["scan_id"] != ls or r["coin"] != c or r["status"] == "skip": continue
                k = (r["scenario"], r["band"], r["direction"], r["entry"], r["tp"], r["sl"])
                x = u.setdefault(k, {"scenario": r["scenario"], "band": r["band"], "dir": r["direction"], "entry": r["entry"],
                                     "tp": r["tp"], "sl": r["sl"], "rr": r["rr"], "ratio": r["snapshot_ratio"],
                                     "nw": r["n_wallets"], "usd": r["cluster_usd"], "status": r["status"],
                                     "trig": r["triggered_ts"] is not None, "r": r["r_realised"], "n": 0})
                x["n"] += 1
            out["setups"][c] = list(u.values())
    return out


def write_json():
    p = os.path.join(DATA, "forward.json")
    with open(p + ".tmp", "w") as f: json.dump(export_json(), f, separators=(",", ":"))
    os.replace(p + ".tmp", p)
    return p


def daily():
    now = int(time.time()); day = kl(now, "%Y-%m-%d")
    d0 = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=KL).timestamp())
    con = db()
    rows = [dict(r) for r in con.execute("SELECT * FROM trades")]
    sc = con.execute("SELECT scan_id FROM scans WHERE scan_ts>=? ORDER BY scan_ts", (d0,)).fetchall()
    con.close()
    closed_today = sum(r["status"] in CLOSED and (r["exit_ts"] or 0) >= d0 for r in rows)
    def s(scn):
        a = agg([r for r in rows if r["scenario"] == scn])
        return "%s %s (n=%d, %sR)" % (scn, "-" if a["wr"] is None else "%.0f%%" % (100 * a["wr"]), a["n"],
                                      "-" if a["avgR"] is None else "%+.2f" % a["avgR"])
    lines = ["HL forward test %s (as of %s KL): scans today %d [%s] | live rows today %d | open %d | closed today %d / total %d" % (
                 day, kl(now, "%H:%M"), len(sc), ", ".join(x[0][-2:] + ":00" for x in sc),
                 sum(r["status"] != "skip" and r["scan_ts"] >= d0 for r in rows), sum(r["status"] == "open" for r in rows),
                 closed_today, sum(r["status"] in CLOSED for r in rows)),
             "Running win%% (all closed, gross): %s | %s | %s" % (s("MAGNET"), s("FADE"), s("CASCADE"))]
    sg = group(rows, lambda r: r["strategy_id"]); ag = {k: agg(v) for k, v in sg.items()}
    q = sorted([k for k in ag if ag[k]["n"] >= 5], key=lambda k: ag[k]["avgR"], reverse=True)
    if q: lines.append("Best n>=5: %s %.0f%% %+.2fR (n=%d) | worst: %s %.0f%% %+.2fR (n=%d)" % (
        q[0], 100 * ag[q[0]]["wr"], ag[q[0]]["avgR"], ag[q[0]]["n"], q[-1], 100 * ag[q[-1]]["wr"], ag[q[-1]]["avgR"], ag[q[-1]]["n"]))
    p = os.path.join(DATA, "forward_daily_%s.md" % day)
    with open(p, "w") as f: f.write("\n".join(lines) + "\n")
    return p


def show(scan_id=None):
    con = db()
    scan_id = scan_id or con.execute("SELECT scan_id FROM scans ORDER BY scan_ts DESC LIMIT 1").fetchone()[0]
    for r in con.execute("SELECT coin, status, count(*) n FROM trades WHERE scan_id=? GROUP BY coin, status", (scan_id,)):
        print(scan_id, r["coin"], r["status"], r["n"])
    for r in con.execute("SELECT * FROM trades WHERE scan_id=? AND status='open' ORDER BY coin, scenario, band, ratio_thr", (scan_id,)):
        print("%-4s %-22s %-5s entry %-10g tp %-10g sl %-10g rr %-6s ratio %-6s nw %-3s %s" % (
            r["coin"], r["strategy_id"], r["direction"], r["entry"], r["tp"], r["sl"], r["rr"], r["snapshot_ratio"],
            r["n_wallets"], r["notes"]))
    con.close()


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    coins = arg("--coins", ",".join(COINS)).upper().split(",")
    if cmd == "scan":
        scan(coins, force="--force" in sys.argv); write_json(); daily()
    elif cmd == "cron-scan":
        if time.time() >= WINDOW_END: print("forward-test window over; scan skipped")
        else: scan(coins, kind="cron")
        resolve(); write_json(); print("daily ->", daily(), flush=True)
    elif cmd in ("resolve", "cron-resolve"):
        resolve(); write_json(); daily()
    elif cmd == "report":
        md, p = report(int(arg("--min-n", 5)), arg("--out")); print(md); print("->", p)
    elif cmd == "daily": print(open(daily()).read())
    elif cmd == "json": print(write_json())
    elif cmd == "show": show(arg("--scan"))
    else: print(__doc__)
