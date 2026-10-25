"""hl-liqmap model: HL-only liquidation map with per-wallet cap, band table, clusters, EXPERIMENTAL signal.
Pure functions, no I/O. Used by collector.py (per snapshot) and backfill.py.
Bucket key k = lower edge of 0.25% bucket in % distance from mid; bucket covers [k, k+STEP)%.
"""
import math

STEP = 0.25
MAXD = 10.0
BANDS = [("S", "S 0-1%", 0, 1), ("M-", "M- 1-3%", 1, 3), ("M", "M 3-5%", 3, 5), ("L", "L 5-10%", 5, 10)]
CAP = 0.10            # max share of a band's RAW total (long+short) any single wallet may contribute
MIN_WALLETS = 3       # signal needs >= this many distinct wallets on the dominant side in 1-5%
SIG_RATIO = 2.0       # dominant-side threshold on 1-5% capped ratio
SIG_BANDS = ("M-", "M")


def bkey(px, mid):
    d = (px / mid - 1) * 100
    if abs(d) > MAXD: return None
    return round(math.floor(d / STEP) * STEP, 4)


def band_of(k):
    ctr = abs(k + STEP / 2)
    for code, _, a, b in BANDS:
        if a <= ctr < b: return code
    return None


def kpx(mid, k, where="mid"):
    off = {"lo": 0, "mid": STEP / 2, "hi": STEP}[where]
    return mid * (1 + (k + off) / 100)


def ratio(a, b):
    if not a and not b: return None
    if not b: return float("inf")
    return a / b


def jr(x):
    """JSON-safe ratio"""
    if x is None: return None
    if x == float("inf"): return 999.0
    return round(x, 3)


def compute(coin, mid, positions, hl_bids, hl_asks, cap=CAP):
    """positions: iterable of (addr, side, liqPx|None, notional_usd). hl_bids/asks: [(px, sz)]."""
    raw = {"long": {}, "short": {}}
    wallets = {"long": {}, "short": {}}     # band -> list of (addr, v, k)
    noliq = 0.0; sample = 0.0; nw = set()
    for addr, side, lpx, v in positions:
        sample += v; nw.add(addr)
        if lpx is None: noliq += v; continue
        if side == "long" and not lpx < mid: continue
        if side == "short" and not lpx > mid: continue
        k = bkey(lpx, mid)
        if k is None: continue
        b = band_of(k)
        if b is None: continue
        raw[side][k] = raw[side].get(k, 0.0) + v
        wallets[side].setdefault(b, []).append((addr, v, k))
    capped = {"long": {}, "short": {}}
    bstat = {}
    band_tot = {}
    for side in ("long", "short"):
        for b, lst in wallets[side].items():
            band_tot[b] = band_tot.get(b, 0.0) + sum(v for _, v, _ in lst)
    for side in ("long", "short"):
        for b, lst in wallets[side].items():
            tot = sum(v for _, v, _ in lst)
            lim = cap * band_tot[b]
            top = max(v for _, v, _ in lst)
            for _, v, k in lst:
                capped[side][k] = capped[side].get(k, 0.0) + min(v, lim)
            bstat[(side, b)] = {"n": len({a for a, _, _ in lst}), "top_share": round(top / tot, 3) if tot else 0}
    book = {"bid": {}, "ask": {}}
    for p, q in hl_bids or []:
        k = bkey(p, mid)
        if k is not None and k < 0: book["bid"][k] = book["bid"].get(k, 0.0) + p * q
    for p, q in hl_asks or []:
        k = bkey(p, mid)
        if k is not None and k >= 0: book["ask"][k] = book["ask"].get(k, 0.0) + p * q

    def bsum(m, b): return sum(v for k, v in m.items() if band_of(k) == b)
    bands = []
    for code, label, a, bb in BANDS:
        lr, sr = bsum(raw["long"], code), bsum(raw["short"], code)
        lc, sc = bsum(capped["long"], code), bsum(capped["short"], code)
        bands.append({"band": code, "label": label,
                      "long_raw": round(lr), "short_raw": round(sr), "long_cap": round(lc), "short_cap": round(sc),
                      "ratio_raw": jr(ratio(lr, sr)), "ratio_cap": jr(ratio(lc, sc)),
                      "bid": round(bsum(book["bid"], code)), "ask": round(bsum(book["ask"], code)),
                      "n_long": bstat.get(("long", code), {}).get("n", 0), "n_short": bstat.get(("short", code), {}).get("n", 0),
                      "top_long_share": bstat.get(("long", code), {}).get("top_share", 0),
                      "top_short_share": bstat.get(("short", code), {}).get("top_share", 0)})
        bands[-1]["thin"] = bands[-1]["n_long"] + bands[-1]["n_short"] < 6

    def clusters(side, n=3):
        m = capped[side]
        out = []
        for k in sorted(m, key=m.get, reverse=True)[:n]:
            out.append({"k": k, "px": round(kpx(mid, k), 6), "usd_cap": round(m[k]), "usd_raw": round(raw[side].get(k, 0)),
                        "dist_pct": round(k + STEP / 2, 3), "band": band_of(k)})
        return out

    sig = signal(mid, capped, book, bands, wallets)
    r15_raw = ratio(sum(x["long_raw"] for x in bands if x["band"] in SIG_BANDS), sum(x["short_raw"] for x in bands if x["band"] in SIG_BANDS))
    return {
        "coin": coin, "mid": mid, "sample_usd": round(sample), "noliq_usd": round(noliq), "n_wallets": len(nw), "cap": cap,
        "bands": bands,
        "clusters": {"long": clusters("long"), "short": clusters("short")},
        "ratio15_cap": sig["ratio15"], "ratio15_raw": jr(r15_raw),
        "signal": sig,
        "buckets": {"long_raw": _r(raw["long"]), "short_raw": _r(raw["short"]), "long_cap": _r(capped["long"]),
                    "short_cap": _r(capped["short"]), "bid": _r(book["bid"]), "ask": _r(book["ask"])},
    }


def _r(m): return {f"{k:.2f}": round(v) for k, v in sorted(m.items())}


def signal(mid, capped, book, bands, wallets=None):
    """EXPERIMENTAL rule set (not validated - see README go/no-go).
    dominant side: 1-5% capped long-liq vs short-liq, ratio >= 2.
      long-liq heavier (below) -> magnet down -> SHORT bias; short-liq heavier (above) -> LONG bias.
    TP  = largest capped liq bucket on the dominant side within 1-10% (bucket centre).
    SL  = just past (far edge + 0.10%) the nearest significant opposite-side obstacle within 0-5%:
          liq bucket >= 25% of that side's max bucket, or HL book wall (0.5-5% only) >= 3x median nonzero book bucket.
          fallback: 1.5% if nothing qualifies.
    tier = by TP band: S if TP < 3% away (M- band), M if 3-5%, L if 5-10%.
    gate: >= MIN_WALLETS distinct wallets on dominant side in 1-5% (one whale != signal).
    """
    L = sum(x["long_cap"] for x in bands if x["band"] in SIG_BANDS)
    Sh = sum(x["short_cap"] for x in bands if x["band"] in SIG_BANDS)
    r = ratio(L, Sh)
    base = {"experimental": True, "ratio15": jr(r), "long15": round(L), "short15": round(Sh), "threshold": SIG_RATIO}
    if r is None:
        return {**base, "active": False, "reason": "no HL liq in 1-5%"}
    dom_r = max(L, Sh) / min(L, Sh) if min(L, Sh) > 0 else float("inf")
    base["dom_ratio"] = jr(dom_r)
    if dom_r < SIG_RATIO:
        return {**base, "active": False, "reason": "1-5%% ratio %.2f < %.1f" % (dom_r, SIG_RATIO)}
    bias = "SHORT" if L > Sh else "LONG"
    tp_side, opp_side = ("long", "short") if bias == "SHORT" else ("short", "long")
    if wallets is not None:
        nw = len({a for b in SIG_BANDS for a, _, _ in wallets[tp_side].get(b, [])})
        base["dom_wallets"] = nw
        if nw < MIN_WALLETS:
            return {**base, "active": False, "reason": "dominant side only %d wallet(s) in 1-5%% (need %d)" % (nw, MIN_WALLETS)}
    tpm = {k: v for k, v in capped[tp_side].items() if 1 <= abs(k + STEP / 2) < 10}
    if not tpm:
        return {**base, "active": False, "reason": "no TP cluster 1-10%"}
    tk = max(tpm, key=tpm.get)
    tp = kpx(mid, tk)
    tdist = abs(tk + STEP / 2)
    om = {k: v for k, v in capped[opp_side].items() if abs(k + STEP / 2) < 5}
    cands = []
    if om:
        mx = max(om.values())
        cands += [(abs(k + STEP / 2), k, "liq", v) for k, v in om.items() if v >= 0.25 * mx]
    # book walls: only 0.5-5% (the 0-0.5% buckets are always the fattest because HL's finest book sits there)
    bm = {k: v for k, v in book["ask" if bias == "SHORT" else "bid"].items() if 0.5 <= abs(k + STEP / 2) < 5 and v > 0}
    if bm:
        med = sorted(bm.values())[len(bm) // 2]
        cands += [(abs(k + STEP / 2), k, "wall", v) for k, v in bm.items() if v >= 3 * med]
    if cands:
        _, sk, kind, sv = min(cands)
        sl = kpx(mid, sk, "hi") * 1.001 if bias == "SHORT" else kpx(mid, sk, "lo") * 0.999
        sl_note = "past %s %.1fM USD" % ("liq cluster" if kind == "liq" else "book wall", sv / 1e6)
    else:
        sl = mid * (1.015 if bias == "SHORT" else 0.985); sl_note = "fallback 1.5% (no obstacle found)"
    sdist = abs(sl / mid - 1) * 100
    tier = "S" if tdist < 3 else ("M" if tdist < 5 else "L")
    return {**base, "active": True, "bias": bias, "tp": round(tp, 6), "tp_dist_pct": round(tdist, 3), "tp_usd": round(tpm[tk]),
            "sl": round(sl, 6), "sl_dist_pct": round(sdist, 3), "sl_note": sl_note, "rr": round(tdist / sdist, 2) if sdist else None,
            "tier": tier}
