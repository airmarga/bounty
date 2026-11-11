#!/usr/bin/env python3
"""Roblox Up-and-Coming top-10 forecaster (v2).

Model: the chart's own transition statistics. From the history we measure, for each
starting rank r and horizon h (24/48/72 hours), the empirical distribution over the
later top-10 position (or absence). Each game's probability of holding place k is a
mixture of those distributions weighted by the share of the scored window spent at
each horizon, conditioned on the game's rank in the last read. A game's order score
(log player count + momentum in log space) breaks the ranking of games outside the
last chart's naming and picks which 97 candidates get explicit rows.

Per-place row sums are capped at PLACE_CAP (0.8): the scorer rejects a place whose
rows add to more than 1, and leaving mass unnamed lets the unnamed share (spread
evenly by the scorer over the unnamed games actually on the chart) cover new entrants
the history could not name.

Everything is deterministic: no randomness; ties break by universe id.
"""
import bisect
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta

ALPHA = 1.5          # weight of log 24h growth in the order score
MAX_R = 97           # starting ranks tracked in the transition tables
TOPN = 97            # games given explicit probability rows
PLACE_CAP = 0.8      # each place's named rows add up to at most this
MIX = (0.15, 0.45, 0.4)   # weight of the 24h / 48h / 72h transition tables
HORIZONS = (24, 48, 72)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def pos_tables(reads, places=10):
    """Transition tables: start rank r -> {later place k: count}, per horizon."""
    times = [parse_ts(c["at"]) for c in reads]
    tables = {h: defaultdict(lambda: defaultdict(int)) for h in HORIZONS}
    for i, t in enumerate(times):
        for h in HORIZONS:
            j = bisect.bisect_left(times, t + timedelta(hours=h * 0.75))
            if j >= len(times) or (times[j] - t).total_seconds() / 3600 > h * 1.6:
                continue
            A = {g["universe_id"]: g["rank"] for g in reads[i]["up_and_coming"]}
            B = {g["universe_id"]: g["rank"] for g in reads[j]["up_and_coming"]}
            for uid, r in A.items():
                if r > MAX_R:
                    continue
                k = B.get(uid)
                kk = k if (k and k <= places) else 0
                tables[h][r][kk] += 1
    return tables


def main():
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(out_dir, exist_ok=True)

    task = json.load(open(os.path.join(in_dir, "task.json")))
    places = int(task.get("places", 10))

    snaps = []
    with open(os.path.join(in_dir, "history.ndjson")) as f:
        for line in f:
            line = line.strip()
            if line:
                s = json.loads(line)
                if s.get("ok") and s.get("up_and_coming"):
                    snaps.append(s)
    if not snaps:
        raise SystemExit("no snapshots")
    snaps.sort(key=lambda s: s["at"])

    tables = pos_tables(snaps, places)

    # per-game player-count series for momentum
    series = defaultdict(list)
    for s in snaps:
        t = parse_ts(s["at"])
        for g in s["up_and_coming"]:
            series[g["universe_id"]].append((t, g.get("playing")))
    t_last = parse_ts(snaps[-1]["at"])

    last = snaps[-1]
    H = {g["universe_id"]: g["rank"] for g in last["up_and_coming"]}

    scored = []
    for g in last["up_and_coming"]:
        uid = g["universe_id"]
        playing = g.get("playing") or 1
        past = [p for (t, p) in series[uid]
                if p and t <= t_last - timedelta(hours=18)]
        gr = (playing / past[-1]) if past and past[-1] else 1.0
        score = math.log(playing) + ALPHA * math.log(max(gr, 0.05))
        scored.append((uid, score))
    if not scored:
        raise SystemExit("final chart has no playable games")
    scored.sort(key=lambda x: (-x[1], x[0]))
    cands = scored[:TOPN]

    rows = defaultdict(lambda: defaultdict(float))
    for uid, s in cands:
        r = H.get(uid, MAX_R)
        for h, w in zip(HORIZONS, MIX):
            table = tables[h].get(min(r, MAX_R))
            if not table:
                continue
            tot = sum(table.values())
            if not tot:
                continue
            for k, c in table.items():
                if k == 0:
                    continue
                rows[k][uid] += w * c / tot

    probabilities = []
    for k in range(1, places + 1):
        d = dict(rows[k])
        ssum = sum(d.values())
        if ssum > PLACE_CAP and ssum > 0:
            f = PLACE_CAP / ssum
            for uid in d:
                d[uid] *= f
        for uid, p in d.items():
            if p > 0:
                probabilities.append(
                    {"universe_id": int(uid), "place": k, "p": round(p, 9)})

    top10 = [uid for uid, _ in cands[:places]]
    for uid, _ in cands:
        if len(top10) >= places:
            break
        if uid not in top10:
            top10.append(uid)

    forecast = {"probabilities": probabilities, "top10": top10}
    path = os.path.join(out_dir, task.get("output", "forecast.json"))
    with open(path, "w") as f:
        json.dump(forecast, f, separators=(",", ":"))
    print("wrote", path, "rows", len(probabilities))


if __name__ == "__main__":
    main()
