#!/usr/bin/env python3
"""hl-liqmap -> GitHub Pages publisher.
Exports data/snapshot.json (all coins, latest) + data/history.json (rolling 24h, compact) from the sqlite DB
into ./pages/ (a git checkout of abigailmini/hl-liqmap, branch gh-pages) together with static/index.html,
then amends the single gh-pages commit and force-pushes. Repo never grows: gh-pages is always 1 commit.
Called at the end of collector.py (non-fatal) or manually: python3 publish.py [--no-push]
"""
import json, os, shutil, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server as S

PAGES = os.path.join(HERE, "pages")
REMOTE = "https://github.com/abigailmini/hl-liqmap.git"
BRANCH = "gh-pages"
ENV = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", GIT_TERMINAL_PROMPT="0")


def r3(v):
    return None if v is None else (round(v, 3) if abs(v) < 100 else round(v, 1))


def history_all(hours=24):
    out = {"hours": hours, "coins": {}}
    for c in S.COINS:
        pts = S.history(c, hours)["points"]
        out["coins"][c] = {"coin": c, "points": [{"ts": p["ts"], "mid": p["mid"], "r15_cap": r3(p["r15_cap"]),
                           "cap": {k: r3(v) for k, v in p["cap"].items()}, "sig": p["sig"]} for p in pts]}
    out["generated_ts"] = int(time.time())
    return out


def export(dst):
    os.makedirs(os.path.join(dst, "data"), exist_ok=True)
    snap = S.snapshot(S.COINS); snap["generated_ts"] = snap["server_ts"]
    for name, obj in (("snapshot.json", snap), ("history.json", history_all())):
        tmp = os.path.join(dst, "data", name + ".tmp")
        with open(tmp, "w") as f: json.dump(obj, f, separators=(",", ":"))
        os.replace(tmp, os.path.join(dst, "data", name))
    try:  # forward-test summary (refreshed here so Pages shows current open/closed counts)
        import forward_test
        forward_test.write_json()
        shutil.copyfile(os.path.join(HERE, "data", "forward.json"), os.path.join(dst, "data", "forward.json"))
    except Exception as e:
        print(f"[warn] forward.json export failed: {e}", file=sys.stderr)
    shutil.copyfile(os.path.join(HERE, "static", "index.html"), os.path.join(dst, "index.html"))
    open(os.path.join(dst, ".nojekyll"), "w").close()
    return snap


def git(*a, timeout=60):
    return subprocess.run(["git", "-C", PAGES, *a], env=ENV, capture_output=True, text=True, timeout=timeout)


def ensure_repo():
    if os.path.isdir(os.path.join(PAGES, ".git")): return
    os.makedirs(PAGES, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", BRANCH, PAGES], env=ENV, check=True)
    git("config", "user.name", "hl-liqmap-bot"); git("config", "user.email", "hl-liqmap-bot@users.noreply.github.com")
    git("remote", "add", "origin", REMOTE)


def publish(push=True):
    t0 = time.time()
    ensure_repo()
    snap = export(PAGES)
    git("add", "-A")
    ts = time.strftime("%F %T")
    has_head = git("rev-parse", "--verify", "-q", "HEAD").returncode == 0
    c = git("commit", "-q", "--amend", "--no-edit", "-m", f"data {ts}") if has_head else git("commit", "-q", "-m", f"data {ts}")
    if c.returncode: raise RuntimeError("commit failed: " + (c.stderr or c.stdout))
    if push:
        p = git("push", "-q", "-f", "origin", f"{BRANCH}:{BRANCH}", timeout=60)
        if p.returncode: raise RuntimeError("push failed: " + p.stderr.strip()[-300:])
    # keep local object store small (old amended commits are unreachable)
    if int(time.time()) % 86400 < 300: git("gc", "-q", "--prune=now", timeout=60)
    return {"age_s": snap.get("age_s"), "secs": round(time.time() - t0, 1)}


if __name__ == "__main__":
    print(publish(push="--no-push" not in sys.argv))
