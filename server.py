#!/usr/bin/env python3
"""hl-liqmap web server. Binds 127.0.0.1:8790 (env HL_LIQMAP_PORT). Stdlib only.
  GET /                      dashboard (static/index.html)
  GET /api/snapshot          latest snapshot all coins (summary + buckets + age/stale flag)   [?coin=BTC]
  GET /api/history?coin=BTC&hours=24   per-snapshot mid + band ratios (capped & raw)
  GET /api/health            last run info
  GET /data/snapshot.json, /data/history.json   same shape as the GitHub Pages static files
"""
import json, os, sqlite3, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "hl-liqmap.sqlite")
STATIC = os.path.join(HERE, "static")
PORT = int(os.environ.get("HL_LIQMAP_PORT", "8790"))
COINS = ["BTC", "ETH", "SOL"]
STALE_S = 900


def q(sql, args=()):
    if not os.path.exists(DB): return []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    try: return con.execute(sql, args).fetchall()
    finally: con.close()


def snapshot(coins):
    now = int(time.time()); out = {"server_ts": now, "stale_after_s": STALE_S, "coins": {}}
    for c in coins:
        r = q("SELECT s.ts, s.summary, b.json FROM snap s LEFT JOIN buckets b ON b.ts=s.ts AND b.coin=s.coin "
              "WHERE s.coin=? ORDER BY s.ts DESC LIMIT 1", (c,))
        if not r: out["coins"][c] = None; continue
        ts, summ, bj = r[0]
        d = json.loads(summ); d["ts"] = ts; d["age_s"] = now - ts; d["stale"] = now - ts > STALE_S
        d["buckets"] = json.loads(bj) if bj else {}
        out["coins"][c] = d
    ages = [v["age_s"] for v in out["coins"].values() if v]
    out["age_s"] = min(ages) if ages else None
    out["stale"] = (not ages) or min(ages) > STALE_S
    return out


def history(coin, hours):
    since = int(time.time() - hours * 3600)
    rows = q("SELECT ts, mid, ratio15_cap, ratio15_raw, summary FROM snap WHERE coin=? AND ts>=? ORDER BY ts", (coin, since))
    pts = []
    for ts, mid, r15c, r15r, summ in rows:
        s = json.loads(summ)
        pts.append({"ts": ts, "mid": mid, "r15_cap": r15c, "r15_raw": r15r,
                    "cap": {b["band"]: b["ratio_cap"] for b in s["bands"]},
                    "raw": {b["band"]: b["ratio_raw"] for b in s["bands"]},
                    "sig": (s["signal"].get("bias") if s["signal"].get("active") else None)})
    return {"coin": coin, "hours": hours, "points": pts}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, open(os.path.join(STATIC, "index.html"), "rb").read(), "text/html; charset=utf-8")
            if u.path == "/data/snapshot.json":  # same files the Pages build serves (see publish.py)
                return self.send(200, snapshot(COINS))
            if u.path == "/data/history.json":
                import publish
                return self.send(200, publish.history_all())
            if u.path == "/api/snapshot":
                c = qs.get("coin", [None])[0]
                return self.send(200, snapshot([c.upper()] if c else COINS))
            if u.path == "/api/history":
                c = qs.get("coin", ["BTC"])[0].upper(); h = float(qs.get("hours", ["24"])[0])
                return self.send(200, history(c, min(h, 24 * 14)))
            if u.path == "/api/health":
                r = q("SELECT ts, secs, addr_ok, addr_n FROM runs ORDER BY ts DESC LIMIT 1")
                n = q("SELECT count(*) FROM runs")
                return self.send(200, {"last": dict(zip(("ts", "secs", "addr_ok", "addr_n"), r[0])) if r else None,
                                       "runs": n[0][0] if n else 0, "now": int(time.time())})
            return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.send(500, {"error": str(e)})


if __name__ == "__main__":
    print(f"hl-liqmap on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
