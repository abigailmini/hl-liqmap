#!/usr/bin/env python3
"""hl-liqmap collector. One run = one snapshot (launchd StartInterval 300).
1) liqcore.snapshot(): HL leaderboard-wallet positions, HL l2Book (+ Binance/OKX/Bybit depth + CEX model for legacy)
2) liqcore.log(): legacy spike tables -> spikes/liq-map/liqmap.sqlite (keeps backtest.py series continuous)
3) model.compute() per coin vs HL mark -> data/hl-liqmap.sqlite (summary + raw 0.25% buckets, raw & capped)
usage: python3 collector.py [--hl-n 600] [--no-legacy] [--print]
"""
import json, os, sqlite3, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import liqcore as L
import model as M

DB = os.path.join(HERE, "data", "hl-liqmap.sqlite")


def db():
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS snap(ts INT, coin TEXT, mid REAL, binance_mid REAL, hl_oi_usd REAL, hl_cov REAL,
                   sample_usd REAL, n_wallets INT, ratio15_cap REAL, ratio15_raw REAL, signal TEXT, summary TEXT, src TEXT,
                   PRIMARY KEY(ts, coin))""")
    con.execute("CREATE TABLE IF NOT EXISTS buckets(ts INT, coin TEXT, json TEXT, PRIMARY KEY(ts, coin))")
    con.execute("CREATE TABLE IF NOT EXISTS runs(ts INT PRIMARY KEY, secs REAL, addr_ok INT, addr_n INT, note TEXT)")
    return con


def store(con, ts, coin, res, binance_mid, hl_oi, hl_cov, src="live"):
    b = res.pop("buckets")
    sig = res["signal"]
    con.execute("INSERT OR REPLACE INTO snap VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, coin, res["mid"], binance_mid, hl_oi, hl_cov, res["sample_usd"], res["n_wallets"],
                 res["ratio15_cap"], res["ratio15_raw"], json.dumps(sig), json.dumps(res), src))
    con.execute("INSERT OR REPLACE INTO buckets VALUES (?,?,?)", (ts, coin, json.dumps(b)))


def main():
    n = int(sys.argv[sys.argv.index("--hl-n") + 1]) if "--hl-n" in sys.argv else 600
    s = L.snapshot(hl_n=n)
    if "--no-legacy" not in sys.argv:
        try: L.log(s)
        except Exception as e: print(f"[warn] legacy log failed: {e}", file=sys.stderr)
    con = db()
    for c in L.COINS:
        m = s["maps"][c]
        mid = s["hl_marks"].get(c) or s["mids"][c]
        pos = [(a, side, lpx, v) for coin, side, lpx, v, a, _ in s["positions"] if coin == c]
        hb = m.get("hl_book", {})
        res = M.compute(c, mid, pos, hb.get("bids"), hb.get("asks"))
        res["hl_oi_usd"] = round(m.get("hl_oi_usd", 0)); res["hl_cov"] = round(m.get("hl_cov", 0), 4)
        res["depth_cov_pct"] = m.get("coverage", {}).get("hl")
        if "--print" in sys.argv:
            print(c, mid, json.dumps({k: res[k] for k in ("bands", "clusters", "signal")}, indent=1))
        store(con, s["ts"], c, res, s["mids"][c], m.get("hl_oi_usd", 0), m.get("hl_cov", 0))
    con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)", (s["ts"], s["secs"], s["hl_addr_ok"], s["hl_addr_n"], ""))
    con.commit(); con.close()
    print(f"logged {time.strftime('%F %T')} ts={s['ts']} ({s['secs']}s, addrs {s['hl_addr_ok']}/{s['hl_addr_n']})", flush=True)
    if "--no-publish" not in sys.argv:
        try:  # GitHub Pages export + force-push; never fatal
            import publish
            print(f"published {publish.publish()}", flush=True)
        except Exception as e:
            print(f"[warn] publish failed: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
