"""API-equivalent cost over time, from Claude Code's `cost-state` records.

cost-state snapshots are cumulative per session (subagents included) but written only now
and then. Each jump between two snapshots is spread over the requests made between them, in
proportion to their list-price weight, so a day only gets the share of a jump that happened
on it. Requests after a session's last snapshot are estimated at list price and flagged.

On subscription plans this is an API-equivalent figure, not an invoice.
"""
import collections
import datetime as dt
import json
import statistics

from . import pricing


def parse_ts(s):
    """An ISO time as a local datetime; None for anything else (None, a number, text that is no time)."""
    if not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def _sessions(con):
    """{(project_dir, session_id): (snapshots, activity)} for sessions with requests."""
    acts = collections.defaultdict(list)
    for r in con.execute("SELECT * FROM requests"):
        t = parse_ts(r["ts"])
        if t:
            acts[(r["project_dir"], r["session_id"])].append((t, r["model"], pricing.weight(r["model"], r)))
    snaps = collections.defaultdict(list)
    for s in con.execute("SELECT * FROM cost_state"):
        t = parse_ts(s["ts"])
        if t:
            snaps[(s["project_dir"], s["session_id"])].append((t, s["total_cost_usd"], json.loads(s["model_usage"] or "{}")))
    for key, act in acts.items():
        act.sort(key=lambda x: x[0])
        yield key, sorted(snaps.get(key, []), key=lambda x: x[0]), act


COVER_TOLERANCE = 1.25  # same band as the backtest: list price may exceed recorded cost by 25 %


def increment(c0, c1):
    """Cost-state increase between two snapshots. A drop means the counter restarted
    (a resumed session runs in a new process that counts from 0), so the increase is c1."""
    return c1 - c0 if c1 >= c0 else c1


def session_roots(con):
    """{(project_dir, session_id): transcript root} (the root most of its requests came from)."""
    best = {}
    for r in con.execute("SELECT project_dir, session_id, root, COUNT(*) AS n FROM requests "
                         "GROUP BY project_dir, session_id, root"):
        k = (r["project_dir"], r["session_id"])
        if k not in best or r["n"] > best[k][1]:
            best[k] = (r["root"], r["n"])
    return {k: v[0] for k, v in best.items()}


def rebuild_slices(con):
    """Recompute cost_slices (day, project, session, model, usd, estimated) from scratch.

    cost-state counts only what the current Claude Code process spent. When an interval holds
    more activity than its increase can explain (the session started before cost-state was
    written, or the counter restarted), the increase is spread over the most recent activity
    that it covers, and the older activity is estimated at list price instead of being lost.
    """
    con.execute("DELETE FROM cost_slices")
    out = collections.defaultdict(float)

    def estimate(items, pdir, sid):
        for t, model, w in items:
            if w:
                out[(t.date().isoformat(), pdir, sid, pricing.base_model(model), 1)] += w

    root_of = session_roots(con)
    for (pdir, sid), snaps, act in _sessions(con):
        chain = [(act[0][0] - dt.timedelta(seconds=1), 0.0, {}), *snaps]
        for (t0, c0, m0), (t1, c1, m1) in zip(chain, chain[1:]):
            jump = increment(c0, c1)
            inside = [a for a in act if t0 < a[0] <= t1]
            if jump <= 0 or not inside:
                continue
            covered, older = _cover(jump, inside)
            estimate(older, pdir, sid)
            base = m0 if c1 >= c0 else {}
            for (day, label), usd in _spread(jump, covered, base, m1).items():
                out[(day, pdir, sid, label, 0)] += usd
        estimate([a for a in act if a[0] > chain[-1][0]], pdir, sid)
    con.executemany("INSERT INTO cost_slices (day, project_dir, session_id, model, usd, estimated, root) "
                    "VALUES (?,?,?,?,?,?,?)",
                    [(k[0], k[1], k[2], k[3], v, k[4], root_of.get((k[1], k[2]))) for k, v in out.items() if v])
    con.commit()


def _cover(jump, inside):
    """Split the interval's activity into (covered by the increase, older and not covered)."""
    if sum(a[2] for a in inside) <= jump * COVER_TOLERANCE:
        return inside, []
    acc, i = 0.0, len(inside)
    while i > 0 and acc < jump:
        i -= 1
        acc += inside[i][2]
    return inside[i:], inside[:i]


def _spread(jump, inside, m0, m1):
    """Split one cost-state jump over the requests inside it, per model, by weight."""
    res = collections.defaultdict(float)
    deltas = {}
    for label, mu in m1.items():
        d = (mu.get("costUSD") or 0) - ((m0.get(label) or {}).get("costUSD") or 0)
        if d > 0:
            deltas[label] = d
    total_delta = sum(deltas.values()) or 0.0
    scale = jump / total_delta if total_delta else 0.0
    for label, d in deltas.items():
        base = pricing.base_model(label)
        mine = [a for a in inside if pricing.base_model(a[1]) == base] or inside
        w_all = sum(a[2] for a in mine) or float(len(mine))
        for t, _, w in mine:
            share = d * scale * ((w or 0) / w_all if w_all else 1.0 / len(mine))
            res[(t.date().isoformat(), label.replace("claude-", ""))] += share
    if not deltas:  # no per-model data: spread the whole jump by weight, labelled by request model
        w_all = sum(a[2] for a in inside) or float(len(inside))
        for t, model, w in inside:
            res[(t.date().isoformat(), pricing.base_model(model))] += jump * ((w or 0) / w_all if w_all else 1.0 / len(inside))
    return res


def backtest(con, min_jump=0.5):
    """[(project_dir, session_id, t0, t1, real_usd, list_price_estimate)] per cost-state interval."""
    rows = []
    for (pdir, sid), snaps, act in _sessions(con):
        for (t0, c0, _), (t1, c1, _) in zip(snaps, snaps[1:]):
            if c1 - c0 > min_jump:
                est = sum(w for t, _, w in act if t0 < t <= t1)
                if est:
                    rows.append((pdir, sid, t0, t1, c1 - c0, est))
    return rows


def verify(con, lo=0.8, hi=1.25):
    """Backtest ratios and conservation check. Returns a dict with PASS/CHECK flags."""
    rows = backtest(con)
    ratios = [a / e for *_, a, e in rows]
    out = {"intervals": len(rows), "lo": lo, "hi": hi,
           "outliers": [{"project_dir": p, "session": s[:8], "from": t0.isoformat(timespec="minutes"),
                         "to": t1.isoformat(timespec="minutes"), "ratio": round(a / e, 2)}
                        for p, s, t0, t1, a, e in rows if not lo <= a / e <= hi]}
    if ratios:
        out.update(median=round(statistics.median(ratios), 2), min=round(min(ratios), 2), max=round(max(ratios), 2))
    if not ratios:
        out["backtest"] = "NO DATA"
    else:
        out["backtest"] = "CHECK" if out["outliers"] else "PASS"
    # Conservation: every recorded cost-state increase must end up in some day's slice.
    recorded = con.execute("SELECT COALESCE(SUM(usd), 0) FROM cost_slices WHERE estimated = 0").fetchone()[0]
    jumps = 0.0
    for _, snaps, act in _sessions(con):
        prev, prev_t = 0.0, act[0][0] - dt.timedelta(seconds=1)
        for t, c, _ in snaps:
            inc = increment(prev, c)
            if inc > 0 and any(prev_t < a[0] <= t for a in act):
                jumps += inc
            prev, prev_t = c, t
    out["conservation"] = {"slices_usd": round(recorded, 2), "cost_state_usd": round(jumps, 2),
                           "status": "PASS" if abs(recorded - jumps) <= max(0.01 * jumps, 0.05) else "CHECK"}
    # Pricing: a model without an exact list price is priced by its family and reported, never silently.
    reqs = list(con.execute("SELECT model, input, output, cache_read, cache_write, cache_write_5m, cache_write_1h FROM requests"))
    usd = {r["model"]: r["u"] for r in con.execute("SELECT model, SUM(usd) AS u FROM cost_slices GROUP BY model")}
    unpriced = pricing.unpriced(reqs, usd)
    out["pricing"] = {"status": "CHECK" if unpriced else "PASS", "table_date": pricing.TABLE_DATE, "unpriced": unpriced}
    return out
