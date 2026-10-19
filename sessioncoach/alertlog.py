"""Warnings view: every warning shown, the numbers that made it fire and what happened next, plus the paused
sessions that were not warned and why. Computed from numbers already in the database, never from transcript text.

Outcomes (`outcome.code`), with a verdict the dashboard colours:
  hook          false_alarm (the next turn's context was far smaller: compacted before the prompt or on return, or
                smaller) · compacted (`by`: you with /compact, auto, idle = Claude Code's idle compaction) ·
                new_session · continued (the big context was re-written: costly) · no_response · pending
  cache_expiry  compacted · returned_in_time · returned_after_expiry (costly) · new_session · not_returned · pending
  others        info (plan limits, context, budgets: the numbers are the explanation)
A compaction Claude Code did on its own (auto, idle) is not counted as the person acting: its verdict is neutral.
Pauses not warned are split: `missed` (the warning failed: no ingest in the window, switched off, unknown) and
`not_applicable` counts by reason (compacted, subagent, replaced: there was nothing to warn about). `disabled` and
`notify_off` reflect the configuration now, not the one in force at the time.
"""
import datetime as dt
import json

from . import config, cost, expiry, limits, pricing
from .projects import Labeler

OUTCOME_WINDOW_S = 3600  # what the person did after a hook warning, looked for within this time
SMALLER = 0.5  # the next turn's context under this share of the announced one: the warning was wrong
# reasons a pause got no warning that are a failure of the warning; the others (compacted, subagent, replaced) mean
# the person had already moved on, so there was nothing to warn about
WARNING_FAILED = ("no_ingest", "unknown", "disabled", "notify_off")
VERDICT = {"false_alarm": "bad", "compacted": "good", "new_session": "good", "continued": "costly", "no_response": "neutral",
           "returned_in_time": "neutral", "returned_after_expiry": "costly", "not_returned": "neutral", "pending": "pending",
           "info": "info"}


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _epoch(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc)


def _ctx(r):
    return (r["input"] or 0) + (r["cache_read"] or 0) + (r["cache_write"] or 0)


def _write_usd(r):
    return round(pricing.token_cost_split(r["model"], r)["cache_write"], 2)


def _next_turn(con, sid, after_ts, strict=True, until="9999"):
    return con.execute("SELECT * FROM requests WHERE session_id = ? AND kind = 'main' AND ts %s ? AND ts <= ? "
                       "ORDER BY ts LIMIT 1" % (">" if strict else ">="), (sid, after_ts, until)).fetchone()


def _compaction(con, sid, lo, hi):
    return con.execute("SELECT * FROM compactions WHERE session_id = ? AND ts > ? AND ts <= ? ORDER BY ts LIMIT 1",
                       (sid, lo, hi)).fetchone()


def _by(comp):
    """Who compacted: 'you' (/compact), 'idle' (Claude Code's idle compaction, applied on return) or 'auto'."""
    return "you" if comp["trigger"] == "manual" else "idle" if comp["precomputed"] else "auto"


def _compacted(comp):
    return {"code": "compacted", "at": comp["ts"], "post_tokens": comp["post_tokens"], "by": _by(comp)}


def _hook_outcome(con, ev, d, now):
    sid, at = ev["session_id"], _epoch(ev["ts"])
    nxt = _next_turn(con, sid, _iso(at), strict=False, until=_iso(at + dt.timedelta(seconds=OUTCOME_WINDOW_S))) if sid else None
    if nxt is None:
        return {"code": "pending" if (now - at).total_seconds() < OUTCOME_WINDOW_S else "no_response"}
    real = _ctx(nxt)
    if d.get("context") and real < SMALLER * d["context"]:
        comp = _compaction(con, sid, d.get("last_ts") or "", nxt["ts"])  # before the prompt, or on return (idle compaction)
        return {"code": "false_alarm", "why": "compacted" if comp else "smaller", "real_context": real,
                "compacted_at": comp["ts"] if comp else None, "by": _by(comp) if comp else None}
    hi = _iso(at + dt.timedelta(seconds=OUTCOME_WINDOW_S))
    comp = _compaction(con, sid, _iso(at), hi)
    if comp:
        return _compacted(comp)
    if expiry.moved_on(con, sid, ev["project_dir"], _iso(at), until=hi, reasons=("replaced",)):
        return {"code": "new_session"}
    return {"code": "continued", "real_context": real, "usd": _write_usd(nxt)}


def _expiry_outcome(con, ev, d, now):
    sid, last_ts = ev["session_id"], d.get("last_ts")
    t = cost.parse_ts(last_ts)
    if not sid or t is None:
        return {"code": "info"}
    expire = t + dt.timedelta(seconds=d.get("ttl_s") or 3600)
    nxt = _next_turn(con, sid, last_ts)
    comp = _compaction(con, sid, last_ts, nxt["ts"] if nxt else _iso(now))
    if comp:
        return _compacted(comp)
    if nxt is not None:
        back = cost.parse_ts(nxt["ts"])
        if back < expire:
            return {"code": "returned_in_time", "at": nxt["ts"]}
        return {"code": "returned_after_expiry", "at": nxt["ts"], "usd": _write_usd(nxt)}
    if expiry.moved_on(con, sid, ev["project_dir"], last_ts, until=_iso(now), reasons=("replaced",)):
        return {"code": "new_session"}
    return {"code": "pending" if now < expire else "not_returned", "expires_at": _iso(expire)}


def _outcome(con, ev, d, now):
    if ev["source"] == "hook":
        return _hook_outcome(con, ev, d, now)
    if ev["source"] == "cache_expiry":
        return _expiry_outcome(con, ev, d, now)
    return {"code": "info"}


def _ingest_times(con, lo, hi):
    out = []
    for r in con.execute("SELECT ts FROM ingest_runs WHERE source = 'transcripts' AND ts >= ? AND ts <= ?",
                         (_iso(lo - dt.timedelta(days=1)), _iso(hi + dt.timedelta(days=1)))):
        t = cost.parse_ts(r["ts"])
        if t is not None and lo <= t <= hi:
            out.append(t)
    return out


def missed(con, since, now, alerts, labeler):
    """Pauses whose cache expired without a cache_expiry warning, from `since` (a datetime), with the reason."""
    cfg = expiry.config(alerts)
    interval = config.ingest_interval(alerts)
    warned = {(r["session_id"], json.loads(r["data"] or "{}").get("last_ts"))
              for r in con.execute("SELECT session_id, data FROM alert_events WHERE source = 'cache_expiry' AND ts >= ?",
                                   (int(since.timestamp()),))}
    sessions = {}
    for r in con.execute("SELECT * FROM requests WHERE kind = 'main' AND ts >= ? ORDER BY session_id, ts, rowid",
                         (_iso(since - dt.timedelta(days=1)),)):  # a day before: the TTL in force at `since` is known
        sessions.setdefault(r["session_id"], []).append(r)
    out = []
    for sid, rows in sessions.items():
        tt = pricing.TtlTracker()
        for i, r in enumerate(rows):
            tt.update(r)
            ttl, t = tt.current(), cost.parse_ts(r["ts"])
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            if t is None or t < since or ttl < expiry.MIN_TTL_S or _ctx(r) < cfg["min_context_tokens"]:
                continue
            expire = t + dt.timedelta(seconds=ttl)
            back = cost.parse_ts(nxt["ts"]) if nxt else None
            if expire > now or (back is not None and back < expire) or (sid, r["ts"]) in warned:
                continue
            why = expiry.moved_on(con, sid, r["project_dir"], r["ts"], until=_iso(expire))
            if not why and not cfg.get("enabled", True):
                why = "disabled"
            elif not why and not alerts.get("notify", True):
                why = "notify_off"
            elif not why:
                lo = expire - dt.timedelta(minutes=interval + cfg["slack_min"])
                hi = expire - dt.timedelta(minutes=cfg["min_remaining_min"])
                why = "unknown" if _ingest_times(con, lo, hi) else "no_ingest"
            out.append({"ts": r["ts"], "project": labeler(r["project_dir"])[0], "session": sid[:8], "reason": why,
                        "context": _ctx(r), "ttl_s": ttl, "expired_at": _iso(expire),
                        "returned_at": nxt["ts"] if nxt else None, "usd": _write_usd(nxt) if nxt else None})
    return sorted(out, key=lambda x: x["ts"], reverse=True)


def view(con, days=7, now=None, labeler=None, alerts=None):
    """The warnings of the last `days` with their outcomes, and the pauses not warned. A database written by an
    older version (no alert_events or compactions yet, opened read-only) gives an empty view."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"alert_events", "compactions"} <= tables:
        return {"days": days, "events": [], "missed": [], "summary": {"missed": 0, "costly_usd": 0},
                "not_applicable": {}, "missed_since": None, "missed_since_first_check": False}
    now = now or dt.datetime.now(dt.timezone.utc)
    labeler = labeler or Labeler.load()
    alerts = limits.load_alerts() if alerts is None else alerts
    start = now - dt.timedelta(days=days)
    events = []
    for ev in con.execute("SELECT * FROM alert_events WHERE ts >= ? ORDER BY ts DESC, rowid DESC", (int(start.timestamp()),)):
        d = json.loads(ev["data"] or "{}")
        o = _outcome(con, ev, d, now)
        events.append({"ts": _iso(_epoch(ev["ts"])), "source": ev["source"],
                       "project": labeler(ev["project_dir"])[0] if ev["project_dir"] else None,
                       "session": (ev["session_id"] or "")[:8] or None, "data": d, "outcome": o,
                       "verdict": "neutral" if o.get("by") in ("auto", "idle") else VERDICT[o["code"]]})
    row = con.execute("SELECT ts FROM alerts_sent WHERE key = ?", (expiry.SINCE_KEY,)).fetchone()
    since = max(start, _epoch(row["ts"])) if row else None
    all_missed = missed(con, since, now, alerts, labeler) if since else []
    miss = [x for x in all_missed if x["reason"] in WARNING_FAILED]
    not_applicable = {}
    for x in all_missed:
        if x["reason"] not in WARNING_FAILED:
            not_applicable[x["reason"]] = not_applicable.get(x["reason"], 0) + 1
    summary = {}
    for e in events:
        summary[e["verdict"]] = summary.get(e["verdict"], 0) + 1
    summary["missed"] = len(miss)
    summary["costly_usd"] = round(sum(e["outcome"].get("usd") or 0 for e in events if e["verdict"] == "costly"), 2)
    return {"days": days, "events": events, "missed": miss, "not_applicable": not_applicable, "summary": summary,
            "missed_since": _iso(since) if since else None, "missed_since_first_check": bool(row) and since > start}
