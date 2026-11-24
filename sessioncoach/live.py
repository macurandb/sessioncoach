"""`sessioncoach live`: the running Claude sessions right now, fresh from their transcripts.

One JSON document for other tools (a Herdr mod consumes it first): the running sessions with
their context size, cache lifetime and today's cost, plus the plan limits and the newest alerts.
Read-only: it never writes metrics.db, never runs an ingest and never sends a notification.

Contract (``"version": 1`` lets consumers detect changes; fields are only ever added, so a
removal or a change of meaning bumps the version):

    {
      "version": 1,
      "now": 1795530913,
      "limits": {"five_hour": {"pct": 22.0, "resets_at": 1795543800},
                 "seven_day": {"pct": 47.0, "resets_at": 1795737600}},
      "sessions": [
        {"pid": 30313, "session_id": "...", "cwd": "/abs/path", "project": "label", "status": "busy",
         "model": "claude-opus-5-5", "context_tokens": 335000, "last_activity": 1795530900,
         "cache_ttl_s": 3600, "cache_expires_at": 1795534500, "rewrite_usd": 6.70,
         "today_usd": 12.40, "source": "transcript", "match": "pid"}
      ],
      "events": [
        {"id": 1234, "ts": 1795530000, "source": "context_tokens", "session_id": "...",
         "level": "warning", "title": "Claude · 500k tokens of context", "body": "Every turn re-reads ..."}
      ],
      "cursor": 1234
    }

Privacy: from each ``sessions/<pid>.json`` only ``pid``, ``sessionId``, ``cwd``, ``status``,
``updatedAt`` and ``procStart`` are read (the ``*.key`` files are never opened). From a
transcript only usage numbers, the model and the timestamp are read; message text never reaches
the output.
"""
import datetime as dt
import glob
import json
import math
import os
import re
import sqlite3
import time

from . import alertlog, config, cost, paths, pricing, store
from .collect import encode_dir
from .projects import Labeler

VERSION = 1
TAIL_BYTES = 256 * 1024  # read at most the last 256 KB of a transcript
DEFAULT_TTL_S = 3600  # the cache lifetime of subscription sessions, when the tail says nothing
LIMITS_STALE_S = 6 * 3600  # a plan-limit sample older than this is not shown
EVENT_WINDOW_S = 24 * 3600  # without `since`, the newest alerts of the last 24 h
MAX_EVENTS = 50
PS_TIMEOUT_S = 2  # how long `ps` may take to answer the process-start check
DB_TIMEOUT_S = 1  # how long `live` waits on a busy/locked metrics.db before degrading
_MAX_PID = 2 ** 31 - 1  # os.kill rejects anything larger; also never 0 or negative
# A session id is only used in a glob after this: no `*`, `?`, `[` or `../` can reach the shell.
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _valid_pid(pid):
    """A session pid must be a plain int in the platform's range; bool and anything else is not."""
    return isinstance(pid, int) and not isinstance(pid, bool) and 1 <= pid <= _MAX_PID


def _pid_alive(pid):
    """True when a process with `pid` exists. On Windows `os.kill(pid, 0)` is not a liveness
    probe (it succeeds for many stale pids), so every pid counts as alive there: dead sessions
    are simply listed too (documented limitation)."""
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError, ValueError):  # EPERM: exists, just not ours; or a pid os.kill rejects
        return True
    return True


def _norm(s):
    return " ".join((s or "").split())


def _proc_matches(pid, proc_start):
    """True when the live process's start time matches `procStart`, or when it cannot be checked.

    After a reboot `sessions/<pid>.json` can point at a pid that now belongs to another process;
    `procStart` (UTC) and `ps -o lstart=` under TZ=UTC both name the process start, so a mismatch
    means the pid was reused. A missing or failing `ps` returns True, so sessions are never
    hidden by this check. Windows is skipped (documented limitation, as for the pid probe)."""
    if os.name == "nt":
        return True
    import subprocess
    try:
        res = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=PS_TIMEOUT_S, env=dict(os.environ, TZ="UTC"), check=False)
    except (OSError, subprocess.SubprocessError):
        return True
    if res.returncode != 0:
        return True  # ps could not tell: do not hide the session
    return _norm(res.stdout) == _norm(proc_start)


def _config_dirs():
    """The Claude config dirs to scan for `sessions/`: the configured one plus the parents of
    every transcript root, so extra accounts work."""
    dirs = [os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(paths.HOME, ".claude")]
    for r in paths.transcript_roots():
        dirs.append(os.path.dirname(os.path.normpath(r)))
    seen, out = set(), []
    for d in dirs:
        n = os.path.normpath(d)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _find_transcript(session_id, roots, preferred_root=None, cwd=None):
    """The transcript `<root>/*/<session_id>.jsonl`, or None. The id is validated first so a
    value with `*`, `?`, `[` or `../` can never reach the glob. The session's own config dir
    (and its cwd-derived project dir) is searched before the other roots, so two accounts with
    the same session id each read their own transcript."""
    if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
        return None
    cands = []
    if preferred_root is not None:
        if cwd:
            cands.append(os.path.join(preferred_root, encode_dir(cwd), session_id + ".jsonl"))
        cands.append(os.path.join(preferred_root, "*", session_id + ".jsonl"))
    cands.extend(os.path.join(root, "*", session_id + ".jsonl") for root in roots)
    for pattern in cands:
        for f in glob.glob(pattern):
            return f
    return None


def _epoch(ts):
    t = cost.parse_ts(ts)
    return int(t.timestamp()) if t is not None else None


def _num(v):
    """A token count as a non-negative int; bool, text, NaN/inf and negatives are 0."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    try:
        return int(v) if math.isfinite(v) and v >= 0 else 0
    except (OverflowError, ValueError):
        return 0


def _int_or_none(v):
    """A non-negative int, or None when the value is not a finite, non-negative number. Used for
    the cache-write breakdown columns, where None means "no breakdown" in `pricing.cache_ttl_s`."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return int(v) if math.isfinite(v) and v >= 0 else None
    except (OverflowError, ValueError):
        return None


def _ttl(u):
    """The cache lifetime a usage wrote with, via `pricing.cache_ttl_s` on a normalised row: the
    transcript's usage becomes the request columns pricing already understands, so the null-
    breakdown rule (positive total writes -> 1 h) matches exactly."""
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    return pricing.cache_ttl_s({
        "cache_write": _num(u.get("cache_creation_input_tokens")),
        "cache_write_1h": _int_or_none(cc.get("ephemeral_1h_input_tokens")),
        "cache_write_5m": _int_or_none(cc.get("ephemeral_5m_input_tokens")),
    })


def _tail_info(path):
    """Fresh numbers from the last 256 KB of one transcript, or None.

    The last assistant line with a `message.usage` gives `model`, `context` (input + cache read
    + cache write) and `ts` (epoch seconds); the cache TTL is that of the most recent line that
    wrote cache, defaulting to 1 h. Message text is never read into the output; one bad line
    never aborts the session or the view."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    info, ttl = None, None
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant":
            continue
        m = d.get("message") if isinstance(d.get("message"), dict) else {}
        u = m.get("usage")
        if not isinstance(u, dict) or not u or m.get("model") == "<synthetic>":
            continue
        model = m.get("model") if isinstance(m.get("model"), str) and len(m.get("model")) <= 100 else None
        ts = d.get("timestamp")
        epoch = _epoch(ts) if isinstance(ts, str) else None
        if epoch is None:
            continue
        if info is None:
            ctx = sum(_num(u.get(k)) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            info = {"model": model, "context": ctx, "ts": epoch}
        if ttl is None:
            ttl = _ttl(u)
        if info is not None and ttl is not None:
            break
    if info is None:
        return None
    info["ttl"] = ttl if ttl is not None else DEFAULT_TTL_S
    return info


def _requests_info(con, session_id):
    """The latest main request of a session: model, context, epoch ts and cache TTL, for the
    fallback when a transcript's tail has no usage line. None when there is no row or the
    database cannot be read."""
    try:
        row = con.execute("SELECT * FROM requests WHERE session_id = ? AND kind = 'main' "
                          "ORDER BY ts DESC, rowid DESC LIMIT 1", (session_id,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    t = cost.parse_ts(row["ts"])
    ttl = pricing.cache_ttl_s(row)
    return {"model": row["model"], "context": pricing.context_tokens(row),
            "ts": int(t.timestamp()) if t is not None else None,
            "ttl": ttl if ttl else DEFAULT_TTL_S}


def _apply_info(s, info):
    """Fill a session's transcript/metrics numbers from one info dict."""
    s["model"] = info["model"]
    s["context_tokens"] = info["context"]
    s["last_activity"] = info["ts"]
    s["cache_ttl_s"] = info["ttl"]
    s["cache_expires_at"] = info["ts"] + info["ttl"] if info["ts"] is not None else None
    if info.get("model"):
        s["rewrite_usd"] = round(info["context"] * pricing.price(info["model"])[3] / 1e6, 2)


def _live_sessions(con=None):
    """The running sessions, newest first: one entry per live `sessions/*.json` across the
    config dirs, joined to its transcript tail (or, failing that, metrics.db) and filled with
    today's cost later. `con` is an optional read-only connection for the metrics fallback."""
    labeler = Labeler.load()
    roots = paths.transcript_roots()
    out = []
    for cfg in _config_dirs():
        sdir = os.path.join(cfg, "sessions")
        for path in sorted(glob.glob(os.path.join(sdir, "*.json"))):
            try:
                with open(path, encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(d, dict):
                continue
            pid = d.get("pid")
            if not _valid_pid(pid):
                continue
            if not _pid_alive(pid):
                continue
            proc_start = d.get("procStart") if isinstance(d.get("procStart"), str) else None
            if proc_start is not None and not _proc_matches(pid, proc_start):
                continue
            sid = d.get("sessionId") if isinstance(d.get("sessionId"), str) else None
            cwd = d.get("cwd") if isinstance(d.get("cwd"), str) else None
            tpath = _find_transcript(sid, roots, preferred_root=os.path.join(cfg, "projects"), cwd=cwd) if sid else None
            pdir = os.path.basename(os.path.dirname(tpath)) if tpath else None
            info = _tail_info(tpath) if tpath else None
            s = {"pid": pid, "session_id": sid, "cwd": cwd,
                 "project": labeler(pdir)[0] if pdir else (os.path.basename(cwd) if cwd else None),
                 "status": d.get("status") if isinstance(d.get("status"), str) else None,
                 "model": None, "context_tokens": None, "last_activity": None,
                 "cache_ttl_s": None, "cache_expires_at": None, "rewrite_usd": None,
                 "today_usd": None, "source": None, "match": "pid"}
            if tpath and info:
                s["source"] = "transcript"
                _apply_info(s, info)
            elif tpath and con is not None:
                req = _requests_info(con, sid)
                if req:  # "metrics" only once a row actually supplied the numbers
                    s["source"] = "metrics"
                    _apply_info(s, req)
            out.append(s)
    out.sort(key=lambda s: -(s["last_activity"] or 0))
    return out


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _today_usd(con, day, session_id):
    """Today's recorded USD for one session (cost_slices for the local day), or None."""
    if not session_id:
        return None
    row = con.execute("SELECT COALESCE(SUM(usd), 0) AS usd, COUNT(*) AS n FROM cost_slices "
                      "WHERE day = ? AND session_id = ?", (day, session_id)).fetchone()
    return round(row["usd"], 2) if row and row["n"] else None


def _limits(con, now):
    """The latest plan-limit sample (any session), or None when there is none or it is stale."""
    row = con.execute("SELECT ts, five_pct, five_reset, seven_pct, seven_reset FROM limits_samples "
                      "ORDER BY ts DESC LIMIT 1").fetchone()
    if not row or row["ts"] is None or now - row["ts"] > LIMITS_STALE_S:
        return None
    return {"five_hour": {"pct": row["five_pct"], "resets_at": row["five_reset"]},
            "seven_day": {"pct": row["seven_pct"], "resets_at": row["seven_reset"]}}


def _level(source, data):
    """"warning" for plan limits at 90 %+, pace, cache expiry and context; else "info"."""
    if source in ("five_hour_pace", "seven_day_pace", "cache_expiry", "context", "context_tokens"):
        return "warning"
    if source in ("five_hour", "seven_day"):
        return "warning" if (data.get("pct") or 0) >= 90 else "info"
    return "info"


def _events(con, since, now, lang):
    """Newest-first-window of alert events: `rowid > since` (or the last 24 h), oldest first,
    at most 50. Returns (events, cursor)."""
    if since is None:
        rows = con.execute("SELECT rowid, * FROM alert_events WHERE ts >= ? ORDER BY rowid LIMIT ?",
                           (now - EVENT_WINDOW_S, MAX_EVENTS)).fetchall()
    else:
        rows = con.execute("SELECT rowid, * FROM alert_events WHERE rowid > ? ORDER BY rowid LIMIT ?",
                           (since, MAX_EVENTS)).fetchall()
    out, cursor = [], since
    for r in rows:
        source = r["source"]
        try:
            data = json.loads(r["data"] or "{}")
            title, body = alertlog.describe(source, data, lang)
            level = _level(source, data)
        except Exception:  # one corrupt event must not break the whole view
            title, body, level = source, "", "info"
        out.append({"id": r["rowid"], "ts": r["ts"], "source": source, "session_id": r["session_id"],
                    "level": level, "title": title, "body": body})
        cursor = r["rowid"]
    return out, cursor


def view(now=None, since=None, con=None):
    """The running Claude sessions and what they need, as one JSON document.

    `now` (epoch seconds) and `since` (a previous `cursor`) pin the output; `con` is an optional
    read-only metrics connection — when omitted, metrics.db is opened read-only if it exists. If
    metrics.db cannot be opened or queried (locked, corrupt), the output degrades to transcript-
    only: `today_usd` and `limits` null, `events` empty and `cursor` = `since`."""
    now = int(now if now is not None else time.time())
    own = con is None
    limits = None
    events, cursor = [], since
    sessions = None
    try:
        if con is None and os.path.exists(paths.db_path()):
            try:
                con = store.open_readonly(paths.db_path(), timeout=DB_TIMEOUT_S)
            except sqlite3.Error:
                con = None
        tables = set()
        if con is not None:
            try:
                tables = _tables(con)
            except sqlite3.Error:
                con, tables = None, set()
        sessions = _live_sessions(con if "requests" in tables else None)
        if con is not None:
            try:
                today = dt.date.fromtimestamp(now).isoformat()
                if "cost_slices" in tables:
                    for s in sessions:
                        s["today_usd"] = _today_usd(con, today, s["session_id"])
                if "limits_samples" in tables:
                    limits = _limits(con, now)
                if "alert_events" in tables:
                    events, cursor = _events(con, since, now, config.notice_lang(config.load_alerts()))
            except sqlite3.Error:
                limits, events, cursor = None, [], since
    finally:
        if own and con is not None:
            con.close()
    if sessions is None:  # an unexpected error in the metrics pass: transcript-only fallback
        sessions = _live_sessions(None)
    sid_proj = {s["session_id"]: s["project"] for s in sessions if s.get("project")}
    for e in events:
        if e["source"] == "cache_expiry" and e["session_id"] in sid_proj:
            e["body"] = "%s — %s" % (e["body"], sid_proj[e["session_id"]])
    return {"version": VERSION, "now": now, "limits": limits, "sessions": sessions,
            "events": events, "cursor": cursor}
