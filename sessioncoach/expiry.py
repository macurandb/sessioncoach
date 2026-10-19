"""Warn before the prompt cache of a paused session expires.

Runs at the end of every scheduled ingest, on the numbers just stored (never the transcript text). A large
main session whose last turn was a while ago keeps its cache for the session's TTL (1 h on subscription
plans, see pricing.cache_ttl_s); once it expires, the next prompt writes the whole context again. Asking
for a handoff or a /compact before that costs a cache read instead.

The ingest runs every `ingest.interval_min`, so the warning window is tied to it: a session is warned on
the first ingest where its cache has at most one interval (plus `slack_min`) left, so every pause gets one
warning whatever the phase of the ingest. 5-minute caches cannot be warned in time and are skipped.
One warning per pause (session + last turn). A session another one in the same project dir replaced
(a session started after its last turn), compacted, or still running a subagent is not warned.

alerts.json: {"cache_expiry": {"enabled": true, "min_context_tokens": 200000, "min_idle_min": 10,
                               "min_remaining_min": 3, "slack_min": 5}}
"""
import contextlib
import datetime as dt

from . import config as config_mod  # expiry.config() is this module's own reader of alerts.json → cache_expiry
from . import cost, handoff, limits, nudge, pricing, store
from .projects import Labeler

DEFAULTS = config_mod.EXPIRY_DEFAULTS  # lives in config; kept here for older callers
MIN_TTL_S = 1800  # shorter caches expire before the next ingest can warn about them
LOOKBACK_S = 2 * 3600  # sessions whose last turn is older than this already lost their cache
SINCE_KEY = "cacheexp:since"  # alerts_sent row of the first check: the warnings view counts missed pauses from there
TTL_ROWS = 50  # most recent turns looked at to find the TTL the session writes with

MSG = {
    "es": {"t": "Claude · la caché de {p} caduca en ~{n} min",
           "m": "~{k}k tokens: si vuelves después, reescribirlos costará ~${usd} (equivalente API). "
                "Si terminaste la fase, pide ahora un traspaso (docs/HANDOFF-{date}.md) o haz /compact."},
    "en": {"t": "Claude · the cache of {p} expires in ~{n} min",
           "m": "~{k}k tokens: coming back later re-writes them for ~${usd} (API-equivalent). "
                "If the phase is done, ask for a handoff now (docs/HANDOFF-{date}.md) or run /compact."},
}

# when /handoff is installed (globally: the ingest does not know the session's working dir)
MSG_HANDOFF = {
    "es": dict(MSG["es"], m="~{k}k tokens: si vuelves después, reescribirlos costará ~${usd} (equivalente API). "
                            "Si terminaste la fase, escribe /handoff ahora o haz /compact."),
    "en": dict(MSG["en"], m="~{k}k tokens: coming back later re-writes them for ~${usd} (API-equivalent). "
                            "If the phase is done, type /handoff now or run /compact."),
}


def config(alerts):
    """`cache_expiry` from alerts.json over DEFAULTS; a value of the wrong type keeps its default."""
    user = alerts.get("cache_expiry")
    cfg = dict(DEFAULTS)
    for k, v in (user if isinstance(user, dict) else {}).items():
        if k == "enabled":
            cfg[k] = v if isinstance(v, bool) else DEFAULTS[k]
        elif k in DEFAULTS:
            with contextlib.suppress(TypeError, ValueError):
                cfg[k] = max(0, int(v))
    return cfg


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def candidates(con, now, cfg, interval_min):
    """Paused sessions whose cache expires within the warning window, newest first:
    [{session_id, project_dir, last_ts, context, ttl_s, idle_s, remaining_s, model, rewrite_usd}]."""
    rows = con.execute("SELECT session_id, project_dir, MAX(ts) AS last_ts FROM requests "
                       "WHERE kind = 'main' AND ts >= ? GROUP BY session_id, project_dir ORDER BY last_ts DESC",
                       (_iso(now - dt.timedelta(seconds=LOOKBACK_S)),)).fetchall()
    out = []
    hi = (interval_min + cfg["slack_min"]) * 60
    for s in rows:
        reqs = con.execute("SELECT * FROM requests WHERE session_id = ? AND project_dir = ? AND kind = 'main' "
                           "ORDER BY ts DESC, rowid DESC LIMIT ?", (s["session_id"], s["project_dir"], TTL_ROWS)).fetchall()
        last = reqs[0]
        ctx = (last["input"] or 0) + (last["cache_read"] or 0) + (last["cache_write"] or 0)
        ttl = next((t for t in (pricing.cache_ttl_s(r) for r in reqs) if t), pricing.TtlTracker().current())
        t = cost.parse_ts(last["ts"])
        if t is None:
            continue
        idle = (now - t).total_seconds()
        remaining = ttl - idle
        if (ttl < MIN_TTL_S or ctx < cfg["min_context_tokens"] or idle < cfg["min_idle_min"] * 60
                or not cfg["min_remaining_min"] * 60 < remaining <= hi):
            continue
        if moved_on(con, s["session_id"], s["project_dir"], last["ts"]):
            continue
        out.append({"session_id": s["session_id"], "project_dir": s["project_dir"], "last_ts": last["ts"],
                    "context": ctx, "ttl_s": ttl, "idle_s": int(idle), "remaining_s": int(remaining),
                    "model": last["model"], "rewrite_usd": ctx * pricing.price(last["model"], last)[3] / 1e6})
    return out


def moved_on(con, sid, pdir, last_ts, until=None, reasons=("compacted", "subagent", "replaced")):
    """Why the pause is not one, or None: 'compacted' (the session was compacted after its last turn),
    'subagent' (a subagent of it is still working) or 'replaced' (a session started after it in the same
    project dir: /clear, a new session after a handoff). Only events up to `until` (a ts) count when given, and
    only the `reasons` asked for are looked for (in that order of priority).
    A session that was already running in the same dir does not count; one opened in parallel during the pause
    does (it cannot be told from a replacement). A subagent left running with no later main turn silences the pause."""
    hi = until or "9999"
    queries = {
        "compacted": ("SELECT 1 FROM compactions WHERE session_id = ? AND project_dir = ? AND ts > ? AND ts <= ? LIMIT 1",
                      (sid, pdir, last_ts, hi)),
        "subagent": ("SELECT 1 FROM requests WHERE session_id = ? AND kind = 'subagent' AND ts > ? AND ts <= ? LIMIT 1",
                     (sid, last_ts, hi)),
        "replaced": ("SELECT 1 FROM requests WHERE project_dir = ? AND session_id != ? AND kind = 'main' "
                     "AND session_id IN (SELECT session_id FROM requests WHERE ts > ? AND ts <= ? AND project_dir = ?) "
                     "GROUP BY session_id HAVING MIN(ts) > ? LIMIT 1", (pdir, sid, last_ts, hi, pdir, last_ts))}
    return next((r for r in reasons if con.execute(*queries[r]).fetchone()), None)


def check(con, alerts=None, now=None, notify_fn=None, labeler=None, dry=False):
    """Notify once per pause for every session in the warning window. Returns the candidates."""
    alerts = limits.load_alerts() if alerts is None else alerts
    cfg = config(alerts)
    now = now or dt.datetime.now(dt.timezone.utc)
    if not dry:
        con.execute("INSERT OR IGNORE INTO alerts_sent (key, ts) VALUES (?, ?)", (SINCE_KEY, int(now.timestamp())))
        con.commit()
    if not cfg.get("enabled", True):
        return []
    found = candidates(con, now, cfg, config_mod.ingest_interval(alerts))
    if dry or not found:
        return found
    notify_fn = notify_fn or limits.notify
    labeler = labeler or Labeler.load()
    msg = (MSG_HANDOFF if handoff.available() else MSG)[nudge.message_lang({}, alerts)]
    for c in found:
        if limits.already_sent(con, "cacheexp:%s:%s" % (c["session_id"], c["last_ts"])) or not alerts.get("notify", True):
            continue
        store.add_alert_event(con, "cache_expiry", c, session_id=c["session_id"], project_dir=c["project_dir"],
                              ts=int(now.timestamp()))
        notify_fn(msg["t"].format(p=labeler(c["project_dir"])[0], n=max(1, round(c["remaining_s"] / 60))),
                  msg["m"].format(k=int(c["context"] / 1000), usd="%.2f" % c["rewrite_usd"],
                                  date=now.astimezone().date().isoformat()))
    con.commit()
    return found
