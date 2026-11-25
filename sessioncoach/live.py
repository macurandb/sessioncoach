"""`sessioncoach live`: the running Claude sessions right now, fresh from their transcripts.

One JSON document for other tools (a Herdr mod consumes it first): the running sessions with
their context size, cache lifetime and today's cost, plus the plan limits and the newest alerts.
Read-only: it never writes metrics.db, never runs an ingest and never sends a notification.

Contract (``"version": 1`` lets consumers detect changes; fields are only ever added, so a
removal or a change of meaning bumps the version):

    {
      "version": 1,
      "now": 1795530913,
      "lang": "en",
      "limits": {"five_hour": {"pct": 22.0, "resets_at": 1795543800},
                 "seven_day": {"pct": 47.0, "resets_at": 1795737600}},
      "sessions": [
        {"pid": 30313, "session_id": "...", "cwd": "/abs/path", "project": "label", "status": "busy",
         "account": "personal", "account_id": "1f8ab3c9d0e2", "group": "acme",
         "model": "claude-opus-5-5", "context_tokens": 335000, "last_activity": 1795530900,
         "cache_ttl_s": 3600, "cache_expires_at": 1795534500, "rewrite_usd": 6.70, "turn_usd": 0.17,
         "today_usd": 12.40, "clear_handoff": true, "source": "transcript", "match": "pid"}
      ],
      "accounts": [
        {"id": "1f8ab3c9d0e2", "key": "personal", "label": "Personal · Max", "plan": "Max", "today_usd": 12.40, "sessions": 2,
         "five_hour": {"pct": 12.0, "resets_at": 1795543800, "window_usd": 18.0, "usd_per_pct": null, "eta": null},
         "seven_day": {"pct": 51.0, "resets_at": 1795737600, "window_usd": 1535.0, "usd_per_pct": 30.1,
                       "eta": null, "projected_pct": 78.0},
         "groups": [{"id": "acme", "five_hour_usd": 6.0, "seven_day_usd": 900.0}]}
      ],
      "groups": [
        {"id": "acme", "label": "Acme", "today_usd": 10.0, "five_hour_usd": 6.0,
         "week_usd": 900.0, "sessions": 1}
      ],
      "events": [
        {"id": 1234, "ts": 1795530000, "source": "context_tokens", "session_id": "...",
         "level": "warning", "title": "Claude · 500k tokens of context", "body": "Every turn re-reads ..."}
      ],
      "cursor": 1234
    }

Every field is described in docs/live-json.md.

Privacy: from each ``sessions/<pid>.json`` only ``pid``, ``sessionId``, ``cwd``, ``status``,
``updatedAt`` and ``procStart`` are read (the ``*.key`` files are never opened). From a
transcript only usage numbers, the model and the timestamp are read; message text never reaches
the output.
"""
import datetime as dt
import glob
import hashlib
import json
import math
import os
import re
import sqlite3
import time

from . import accounts, alertlog, config, cost, idleguard, limits, paths, pricing, projects, resume, store
from .collect import encode_dir
from .projects import UNASSIGNED, Labeler

VERSION = 1
TAIL_BYTES = 256 * 1024  # read at most the last 256 KB of a transcript
DEFAULT_TTL_S = 3600  # the cache lifetime of subscription sessions, when the tail says nothing
LIMITS_STALE_S = limits.LIMITS_STALE_S  # a plan-limit sample older than this is not shown (owned by limits.py)
MIN_PCT_DELTA = 5  # pct is a whole number: a smaller sample delta is mostly rounding, not real spend
EVENT_WINDOW_S = 24 * 3600  # without `since`, the newest alerts of the last 24 h
MAX_EVENTS = 50
PS_TIMEOUT_S = 2  # how long `ps` may take to answer the process-start check
DB_TIMEOUT_S = 1  # how long `live` waits on a busy/locked metrics.db before degrading
_MAX_PID = 2 ** 31 - 1  # os.kill rejects anything larger; also never 0 or negative
_MAX_TOKENS = 10 ** 12  # token counts above this are invalid (nothing real is that large)
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
    """A token count as a non-negative int; bool, text, NaN/inf, negatives and absurd values
    (over 1e12, which would overflow the cost arithmetic) are 0."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    try:
        return int(v) if math.isfinite(v) and 0 <= v <= _MAX_TOKENS else 0
    except (OverflowError, ValueError):
        return 0


def _int_or_none(v):
    """A non-negative int, or None when the value is not a finite, non-negative number. Used for
    the cache-write breakdown columns, where None means "no breakdown" in `pricing.cache_ttl_s`."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return int(v) if math.isfinite(v) and 0 <= v <= _MAX_TOKENS else None
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
        except (ValueError, RecursionError):
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
    if info.get("model") and info.get("context") is not None:
        prices, _exact, key = pricing.price_info(info["model"])
        if info["context"] > pricing.LONG_PROMPT and key in pricing.LONG_PRICES:
            prices = pricing.LONG_PRICES[key]  # a long prompt pays the long-prompt tier, as _agg_weight does
        s["rewrite_usd"] = round(info["context"] * prices[3] / 1e6, 2)  # re-writing after expiry
        s["turn_usd"] = round(info["context"] * prices[2] / 1e6, 2)  # each further turn re-reads this


def _root_of(tpath, roots):
    """The transcript root `tpath` sits under, or None when it is under none of them."""
    for r in roots:
        prefix = os.path.normpath(r)
        if tpath.startswith(prefix + os.sep):
            return prefix
    return None


def _live_sessions(con=None, labeler=None):
    """The running sessions, newest first: one entry per live `sessions/*.json` across the
    config dirs, joined to its transcript tail (or, failing that, metrics.db) and filled with
    today's cost later. `con` is an optional read-only connection for the metrics fallback.
    Each entry also carries `_root` (its transcript root) and `_tenant` (its group); `view`
    turns them into the public `account` and `group` keys."""
    labeler = labeler or Labeler.load()
    roots = paths.transcript_roots()
    out = []
    handoff = {}  # (cfg_dir, cwd) -> resume.active_for(cwd, cfg_dir), computed once per distinct pair
    idle_guard = idleguard.enabled()  # once per view: a free handoff needs the idle guard on as well
    for cfg in _config_dirs():
        sdir = os.path.join(cfg, "sessions")
        for path in sorted(glob.glob(os.path.join(sdir, "*.json"))):
            try:
                with open(path, encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, ValueError, RecursionError):
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
            root = _root_of(tpath, roots) if tpath else None
            if root is None:  # no transcript: the account is still the one this config dir belongs to
                root = os.path.normpath(os.path.join(cfg, "projects"))
            if pdir:
                proj, tenant = labeler(pdir)
            else:
                proj, tenant = (os.path.basename(cwd) if cwd else None), UNASSIGNED
            info = _tail_info(tpath) if tpath else None
            s = {"pid": pid, "session_id": sid, "cwd": cwd, "project": proj,
                 "status": d.get("status") if isinstance(d.get("status"), str) else None,
                 "model": None, "context_tokens": None, "last_activity": None,
                 "cache_ttl_s": None, "cache_expires_at": None, "rewrite_usd": None,
                 "turn_usd": None, "today_usd": None, "clear_handoff": False,
                 "source": None, "match": "pid",
                 "_root": root, "_tenant": tenant, "_account_id": _identity(cfg)}
            if tpath and info:
                s["source"] = "transcript"
                _apply_info(s, info)
            elif tpath and con is not None:
                req = _requests_info(con, sid)
                if req:  # "metrics" only once a row actually supplied the numbers
                    s["source"] = "metrics"
                    _apply_info(s, req)
            if cwd:
                key = (cfg, cwd)
                if key not in handoff:
                    handoff[key] = resume.active_for(cwd, cfg_dir=cfg)
                s["clear_handoff"] = handoff[key] and idle_guard
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
    """Each window's current sample (any session) from the last 6 h, or None when neither has one.

    Read through `limits.window_sample`, so a stale redraw (an idle session re-recording an older,
    lower pct with a fresh ts) can never flap the shown percentage: within one window the real pct
    only grows, so the MAX over that reset's samples is the truth. A corrupt sample is skipped."""
    out = {}
    for key, window in (("five_hour", "five"), ("seven_day", "seven")):
        reset, pct, _ts = limits.window_sample(con, window, now=now)
        out[key] = {"pct": pct, "resets_at": reset}
    if all(v["pct"] is None for v in out.values()):
        return None
    return out


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


def _iso(epoch):
    """An epoch as a UTC ISO string with the `Z` suffix the transcript rows carry."""
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _identity(path):
    """First 12 hex of sha256 of a normalised path: a stable, unique identity for one account's
    config dir (used for both `accounts[].id` and `sessions[].account_id`, so the panel can match
    them)."""
    return hashlib.sha256(os.path.normpath(path).encode("utf-8")).hexdigest()[:12]


def _plan_word(det):
    """"Max" | "Team" | "Pro" from an account's org type, else None."""
    ot = (det.get("org_type") or "").lower()
    for plan in ("max", "team", "pro"):
        if ot.endswith(plan):
            return plan.capitalize()
    return None


def _account_info(root, accs=None):
    """-> (key, label, plan) for a transcript root. The label is the accounts.json label, else a
    plan word, else the config dir's basename — never an email or a person's name."""
    accs = accounts.load_map() if accs is None else accs
    norm = os.path.normpath(os.path.expanduser(root))
    det = accounts.detect(root)
    org = det.get("org_hash")
    # the account logged in there now wins; the static `roots` mapping only names a dir whose login is unknown
    for k, v in accs.items():
        if org and v.get("org_hash") == org:
            return k, v.get("label") or k, _plan_word(det)
    for k, v in accs.items():
        extra = v.get("roots")
        if isinstance(extra, list) and any(
                isinstance(r, str) and os.path.normpath(os.path.expanduser(r)) == norm for r in extra):
            if org and v.get("org_hash") and v.get("org_hash") != org:
                continue  # that dir is logged into another account now
            return k, v.get("label") or k, _plan_word(det)
    plan = _plan_word(det)
    key = "auto:" + org if org else "unknown"
    if plan:
        return key, plan, plan
    label = os.path.basename(accounts.config_dir_of(root))
    return key, label or "?", None


def _login_since(con, root, tables):
    """(epoch, seen_at) of the login a config dir has now (the latest `accounts_seen` row), or (None, None).

    A config dir can switch accounts (/login): limits, window spend and today's cost of the account
    shown count from that switch only, so another account's usage is never added to it."""
    if "accounts_seen" not in tables:
        return None, None
    row = con.execute("SELECT seen_at FROM accounts_seen WHERE root = ? ORDER BY seen_at DESC LIMIT 1",
                      (root,)).fetchone()
    t = cost.parse_ts(row["seen_at"]) if row and row["seen_at"] else None
    return (int(t.timestamp()), row["seen_at"]) if t else (None, None)


def _latest_sample(con, root, window, now, since=None):
    """One root's current sample of `window`, as `_limits` does but by root: the current window's
    `reset`, the MAX valid `pct` across its samples (stale redraws cannot flap it) and the `ts` of
    the earliest sample that reached that max, so `usd_per_pct` can span the two samples."""
    reset, pct, ts = limits.window_sample(con, window, root=root, since=since, now=now)
    return {"ts": ts, "pct": pct, "reset": reset}


def _projected_pct(pct, resets_at, now):
    """The 7-day percentage extrapolated to the full window, once 6 h of it have elapsed."""
    if pct is None or not resets_at:
        return None
    elapsed = now - (resets_at - 7 * 86400)
    if elapsed < 6 * 3600:
        return None
    return round(pct * 7 * 86400 / elapsed, 1)


def _agg_weight(r):
    """List-price USD of one aggregated (model, long-prompt) request group — the exact total
    `pricing.weight` gives for the group's requests, since all of them share the same price tier."""
    prices, _exact, key = pricing.price_info(r["model"])
    if r["long"] and key in pricing.LONG_PRICES:
        prices = pricing.LONG_PRICES[key]
    pin, pout, pcr, pcw1h, pcw5m = prices
    return ((r["input"] or 0) * pin + (r["output"] or 0) * pout + (r["cache_read"] or 0) * pcr
            + (r["cw1h"] or 0) * pcw1h + (r["cw5"] or 0) * pcw5m) / 1e6


def _window_rows(con, start_epoch, now, root=None, strict_lo=False):
    """(root, project_dir, weight) for the requests with `start_epoch <= ts <= now` (one root, or
    all), summed in SQL by (root, project_dir, model, long-prompt) and priced in Python: a handful
    of rows, never one per request. `julianday(ts)` compares at millisecond precision, the
    precision of Claude Code's timestamps, so a request even 1 ms after `now` is excluded, and `COALESCE` treats NULL token columns as 0
    exactly as `pricing.weight` does, so the long-prompt tier and the sums match per-row pricing.
    `strict_lo` excludes a request exactly at `start_epoch` (for a spend range `t0 < ts <= t1`)."""
    lo, hi = _iso(start_epoch), _iso(now)
    op = ">" if strict_lo else ">="
    sql = ("SELECT root, project_dir, model, "
           "CASE WHEN COALESCE(input, 0) + COALESCE(cache_read, 0) + COALESCE(cache_write, 0) > ? "
           "THEN 1 ELSE 0 END AS long, "
           "SUM(COALESCE(input, 0)) AS input, SUM(COALESCE(output, 0)) AS output, "
           "SUM(COALESCE(cache_read, 0)) AS cache_read, "
           "SUM(CASE WHEN cache_write_5m IS NULL AND cache_write_1h IS NULL THEN COALESCE(cache_write, 0) "
           "ELSE COALESCE(cache_write_1h, 0) END) AS cw1h, "
           "SUM(CASE WHEN cache_write_5m IS NULL AND cache_write_1h IS NULL THEN 0 "
           "ELSE COALESCE(cache_write_5m, 0) END) AS cw5 "
           "FROM requests WHERE julianday(ts) " + op + " julianday(?) AND julianday(ts) <= julianday(?)")
    args = [pricing.LONG_PROMPT, lo, hi]
    if root is not None:
        sql += " AND root IS ?"
        args.append(root)
    sql += " GROUP BY root, project_dir, model, long"
    return [(r["root"], r["project_dir"], _agg_weight(r)) for r in con.execute(sql, args)]


def _usd_per_pct(con, root, window, latest, since=None, now=None):
    """List-price USD one percentage point of `window` costs on `root`, or None.

    `latest` is `_latest_sample`'s dict for the window (`pct`, `reset`, `ts`): `p1` is the MAX valid
    pct of the current window (its envelope) and `t1` the earliest sample that reached it, so stale
    redraws cannot move either. `p0`/`t0` is the first observed point of the same window since the
    login (`since`), rows of one second collapsed to their max. Spend is the root's requests with
    `t0 < ts <= t1`, priced in SQL by `_window_rows`. None when a value is unusable, `t0` is not
    before `t1`, the percentage delta is under `MIN_PCT_DELTA` (pct is a whole number, so a smaller
    delta is mostly rounding), nothing was spent, or the rounded result is not > 0."""
    now = int(now if now is not None else time.time())
    reset, p1, t1 = latest.get("reset"), latest.get("pct"), latest.get("ts")
    if not (limits.valid_epoch(reset, now) and limits.observed(t1, now) and limits.valid_pct(p1)):
        return None
    lo = max(since or 0, reset - limits.WINDOW_S[window])
    points = limits.envelope_points(con, window, reset, lo, t1, now, root=root)
    if not points:
        return None
    t0 = min(points)
    p0 = points[t0]
    if not t0 < t1 or p1 - p0 < MIN_PCT_DELTA:
        return None
    usd = sum(w for _r, _p, w in _window_rows(con, t0, t1, root=root, strict_lo=True))
    if usd <= 0:
        return None
    value = round(usd / (p1 - p0), 2)
    return value if value > 0 else None


def _account_groups(five, seven):
    """`accounts[].groups`: an account's window spend per tenant, as `{"id", "five_hour_usd",
    "seven_day_usd"}` rows, ordered by seven-day spend with `"unassigned"` last and tenants with no
    spend in either window omitted. The two windows match `five_hour.window_usd` /
    `seven_day.window_usd`, so the rows add up to them (modulo rounding)."""
    out = []
    for tenant in sorted(set(five) | set(seven)):
        f, s = round(five.get(tenant, 0.0), 2), round(seven.get(tenant, 0.0), 2)
        if f == 0 and s == 0:
            continue
        out.append({"id": tenant, "five_hour_usd": f, "seven_day_usd": s})
    out.sort(key=lambda g: (g["id"] == UNASSIGNED, -g["seven_day_usd"]))
    return out


def _accounts_groups(con, sessions, now, labeler):
    """The new `accounts` and `groups` keys, aggregated in SQL where possible so 20 000 requests
    still answer well under a second: a couple of GROUP BY passes, one pass over the window's
    requests for the list-price sums, and no per-row queries."""
    tables = _tables(con)
    has_slices = "cost_slices" in tables
    has_limits = "limits_samples" in tables
    has_reqs = "requests" in tables
    today = dt.date.fromtimestamp(now).isoformat()
    week_start = (dt.date.fromtimestamp(now) - dt.timedelta(days=6)).isoformat()

    # the roots that matter: a live session, or a limits sample in the last 6 h
    roots = {s["_root"] for s in sessions if s.get("_root")}
    if has_limits:
        for row in con.execute("SELECT DISTINCT root FROM limits_samples WHERE root IS NOT NULL AND ts >= ?",
                               (now - LIMITS_STALE_S,)):
            roots.add(row["root"])

    login = {r: _login_since(con, r, tables) for r in roots}
    samples = {r: {"five": _latest_sample(con, r, "five", now, login[r][0]),
                   "seven": _latest_sample(con, r, "seven", now, login[r][0])}
               for r in roots} if has_limits else {}

    root_today, group_today, group_week = {}, {}, {}
    if has_slices:
        cols = store.columns(con, "cost_slices")
        has_root, has_login = "root" in cols, "login_at" in cols
        sel = "project_dir, day, usd" + (", root" if has_root else "") + (", login_at" if has_login else "")
        for row in con.execute("SELECT %s FROM cost_slices WHERE day >= ?" % sel, (week_start,)):
            tenant = labeler(row["project_dir"])[1]
            group_week[tenant] = group_week.get(tenant, 0.0) + row["usd"]
            if row["day"] == today:
                group_today[tenant] = group_today.get(tenant, 0.0) + row["usd"]
                seen = login.get(row["root"], (None, None))[1] if has_root else None
                if has_root and (not has_login or seen is None or row["login_at"] == seen):
                    root_today[row["root"]] = root_today.get(row["root"], 0.0) + row["usd"]

    # window sums, aggregated in SQL by (root, project_dir, model, long-prompt) and priced in Python
    root_five, root_seven, group_five = {}, {}, {}
    root_five_tenants, root_seven_tenants = {}, {}  # per-root window spend split by tenant (accounts[].groups)
    five_ppc, seven_ppc = {}, {}  # USD per percentage point, from the earliest to the latest sample
    five_resets = {r: samples[r]["five"]["reset"] for r in roots if samples.get(r, {}).get("five", {}).get("reset")}
    seven_resets = {r: samples[r]["seven"]["reset"] for r in roots if samples.get(r, {}).get("seven", {}).get("reset")}
    global_five_start = min(five_resets.values()) - 5 * 3600 if five_resets else None
    if has_reqs:
        for r, reset in five_resets.items():
            start = max(reset - 5 * 3600, login.get(r, (None,))[0] or 0)
            tenants, total = {}, 0.0
            for _rr, project_dir, w in _window_rows(con, start, now, root=r):
                total += w
                tenant = labeler(project_dir)[1]
                tenants[tenant] = tenants.get(tenant, 0.0) + w
            root_five[r], root_five_tenants[r] = total, tenants
        for r, reset in seven_resets.items():
            start = max(reset - 7 * 86400, login.get(r, (None,))[0] or 0)
            tenants, total = {}, 0.0
            for _rr, project_dir, w in _window_rows(con, start, now, root=r):
                total += w
                tenant = labeler(project_dir)[1]
                tenants[tenant] = tenants.get(tenant, 0.0) + w
            root_seven[r], root_seven_tenants[r] = total, tenants
        for r in roots:
            login_epoch = login.get(r, (None,))[0]
            five_ppc[r] = _usd_per_pct(con, r, "five", samples.get(r, {}).get("five", {}), login_epoch, now)
            seven_ppc[r] = _usd_per_pct(con, r, "seven", samples.get(r, {}).get("seven", {}), login_epoch, now)
        if global_five_start is not None:
            for _rr, project_dir, w in _window_rows(con, global_five_start, now):
                tenant = labeler(project_dir)[1]
                group_five[tenant] = group_five.get(tenant, 0.0) + w

    accs = accounts.load_map()
    accounts_out = []
    for root in sorted(roots):
        key, label, plan = _account_info(root, accs)
        f = samples.get(root, {}).get("five", {})
        s = samples.get(root, {}).get("seven", {})
        login_epoch = login.get(root, (None,))[0]
        accounts_out.append({
            "id": _identity(os.path.dirname(os.path.normpath(root))),
            "key": key, "label": label, "plan": plan,
            "today_usd": round(root_today.get(root, 0.0), 2),
            "sessions": sum(1 for x in sessions if x.get("_root") == root),
            "five_hour": {"pct": f.get("pct"), "resets_at": f.get("reset"),
                          "window_usd": round(root_five[root], 2) if root in root_five else None,
                          "usd_per_pct": five_ppc.get(root),
                          "eta": limits.pace(con, "five_hour", f["reset"], now, f["pct"], root=root, since=login_epoch)
                          if f.get("reset") and f.get("pct") is not None else None},
            "seven_day": {"pct": s.get("pct"), "resets_at": s.get("reset"),
                          "window_usd": round(root_seven[root], 2) if root in root_seven else None,
                          "usd_per_pct": seven_ppc.get(root),
                          "eta": limits.pace(con, "seven_day", s["reset"], now, s["pct"], root=root, since=login_epoch)
                          if s.get("reset") and s.get("pct") is not None else None,
                          "projected_pct": _projected_pct(s.get("pct"), s.get("reset"), now)},
            "groups": _account_groups(root_five_tenants.get(root, {}), root_seven_tenants.get(root, {})),
        })

    labels = projects.group_labels()
    tenants = set(group_week) | set(group_today) | {x["_tenant"] for x in sessions}
    groups_out = []
    for tenant in tenants:
        groups_out.append({
            "id": tenant, "label": labels.get(tenant, tenant),
            "today_usd": round(group_today.get(tenant, 0.0), 2),
            "five_hour_usd": round(group_five.get(tenant, 0.0), 2) if global_five_start is not None else None,
            "week_usd": round(group_week.get(tenant, 0.0), 2),
            "sessions": sum(1 for x in sessions if x.get("_tenant") == tenant),
        })
    groups_out.sort(key=lambda g: (g["id"] == UNASSIGNED, -g["week_usd"]))
    return accounts_out, groups_out


def view(now=None, since=None, con=None):
    """The running Claude sessions and what they need, as one JSON document.

    `now` (epoch seconds) and `since` (a previous `cursor`) pin the output; `con` is an optional
    read-only metrics connection — when omitted, metrics.db is opened read-only if it exists. If
    metrics.db cannot be opened or any metrics query fails (locked, corrupt), the output degrades
    to exactly the transcript-only view: `today_usd` and `limits` null, `events` empty and
    `cursor` = `since`. A connection `live` opens itself is always closed; a caller-provided
    `con` is never closed."""
    now = int(now if now is not None else time.time())
    lang = config.notice_lang(config.load_alerts())
    limits = None
    events, cursor = [], since
    sessions = None
    accounts_out, groups_out = [], []
    owned = None  # the connection `live` opened, kept separate so `finally` always closes it
    labeler = Labeler.load()
    try:
        if con is None and os.path.exists(paths.db_path()):
            try:
                owned = store.open_readonly(paths.db_path(), timeout=DB_TIMEOUT_S)
            except sqlite3.Error:
                owned = None
        if con is None:
            con = owned
        tables = set()
        if con is not None:
            try:
                tables = _tables(con)
            except sqlite3.Error:
                con, tables = None, set()
        sessions = _live_sessions(con if "requests" in tables else None, labeler=labeler)
        if con is not None:
            try:
                today = dt.date.fromtimestamp(now).isoformat()
                if "cost_slices" in tables:
                    for s in sessions:
                        s["today_usd"] = _today_usd(con, today, s["session_id"])
                if "limits_samples" in tables:
                    limits = _limits(con, now)
                if "alert_events" in tables:
                    events, cursor = _events(con, since, now, lang)
                if "cost_slices" in tables or "limits_samples" in tables:
                    accounts_out, groups_out = _accounts_groups(con, sessions, now, labeler)
            except sqlite3.Error:
                # any metrics query failing must degrade to the transcript-only view: rebuild
                # without metrics (today_usd null, no fallback fields, source per transcript only)
                limits, events, cursor = None, [], since
                sessions = _live_sessions(None, labeler=labeler)
                accounts_out, groups_out = [], []
    finally:
        if owned is not None:
            owned.close()
    if sessions is None:  # an unexpected error in the metrics pass: transcript-only fallback
        sessions = _live_sessions(None, labeler=labeler)
    sid_proj = {s["session_id"]: s["project"] for s in sessions if s.get("project")}
    for e in events:
        if e["source"] == "cache_expiry" and e["session_id"] in sid_proj:
            e["body"] = "%s — %s" % (e["body"], sid_proj[e["session_id"]])
    accs = accounts.load_map()
    for s in sessions:
        root = s.pop("_root", None)
        s["group"] = s.pop("_tenant", None) or UNASSIGNED
        s["account_id"] = s.pop("_account_id", None)
        s["account"] = _account_info(root, accs)[0] if root else None
    return {"version": VERSION, "now": now, "lang": lang, "limits": limits, "sessions": sessions,
            "accounts": accounts_out, "groups": groups_out, "events": events, "cursor": cursor}


def demo_view(now=None):
    """A synthetic `view` with the same contract as `view()`, for screenshots and for trying the
    panel without real data (`sessioncoach live --demo`). It reads nothing from disk: three neutral
    sessions (one big with a live cache, one big with an expired cache), two accounts (one with a
    `usd_per_pct`, one without), per-account `groups`, four groups, the plan limits at 22/47 and two
    events. Client names stay neutral (acme, globex, initech)."""
    now = int(now if now is not None else time.time())
    sessions = [
        {"pid": 4242, "session_id": "demo-shop", "cwd": "/home/you/shop", "project": "shop", "status": "busy",
         "account": "personal", "account_id": "000000000001", "group": "acme",
         "model": "claude-opus-5-5", "context_tokens": 335000, "last_activity": now - 60,
         "cache_ttl_s": 3600, "cache_expires_at": now - 60 + 3600, "rewrite_usd": 2.68, "turn_usd": 0.07,
         "today_usd": 12.40, "clear_handoff": True, "source": "transcript", "match": "pid"},
        {"pid": 4243, "session_id": "demo-api", "cwd": "/home/you/api", "project": "api", "status": "busy",
         "account": "team", "account_id": "000000000002", "group": "globex",
         "model": "claude-sonnet-5-5", "context_tokens": 908000, "last_activity": now - 46 * 3600,
         "cache_ttl_s": 3600, "cache_expires_at": now - 46 * 3600 + 3600, "rewrite_usd": 3.63, "turn_usd": 0.09,
         "today_usd": 3.10, "clear_handoff": False, "source": "transcript", "match": "pid"},
        {"pid": 4244, "session_id": "demo-docs", "cwd": "/home/you/docs", "project": "docs", "status": "idle",
         "account": "personal", "account_id": "000000000001", "group": "unassigned",
         "model": "claude-haiku-5-5", "context_tokens": 42000, "last_activity": now - 1500,
         "cache_ttl_s": 3600, "cache_expires_at": now - 1500 + 3600, "rewrite_usd": 0.01, "turn_usd": 0.0,
         "today_usd": 0.80, "clear_handoff": True, "source": "transcript", "match": "pid"},
    ]
    limits = {"five_hour": {"pct": 22.0, "resets_at": now + 3600},
              "seven_day": {"pct": 47.0, "resets_at": now + 2 * 86400}}
    accounts = [
        {"id": "000000000001", "key": "personal", "label": "Personal · Max", "plan": "Max", "today_usd": 13.20, "sessions": 2,
         "five_hour": {"pct": 12.0, "resets_at": now + 3600, "window_usd": 18.0, "usd_per_pct": 1.5, "eta": None},
         "seven_day": {"pct": 51.0, "resets_at": now + 2 * 86400, "window_usd": 1535.0, "usd_per_pct": 30.1,
                       "eta": None, "projected_pct": 78.0},
         "groups": [{"id": "acme", "five_hour_usd": 17.2, "seven_day_usd": 1455.0},
                    {"id": "unassigned", "five_hour_usd": 0.8, "seven_day_usd": 80.0}]},
        {"id": "000000000002", "key": "team", "label": "Team", "plan": "Team", "today_usd": 3.10, "sessions": 1,
         "five_hour": {"pct": 3.0, "resets_at": now + 5400, "window_usd": 4.0, "usd_per_pct": None, "eta": None},
         "seven_day": {"pct": 20.0, "resets_at": now + 86400, "window_usd": 300.0, "usd_per_pct": None,
                       "eta": None, "projected_pct": 35.0},
         "groups": [{"id": "globex", "five_hour_usd": 4.0, "seven_day_usd": 300.0}]},
    ]
    groups = [
        {"id": "acme", "label": "Acme", "today_usd": 12.40, "five_hour_usd": 12.40, "week_usd": 900.0, "sessions": 1},
        {"id": "globex", "label": "Globex", "today_usd": 3.10, "five_hour_usd": 3.10, "week_usd": 500.0, "sessions": 1},
        {"id": "initech", "label": "Initech", "today_usd": 0.0, "five_hour_usd": 0.0, "week_usd": 300.0, "sessions": 0},
        {"id": "unassigned", "label": "unassigned", "today_usd": 0.80, "five_hour_usd": 0.80, "week_usd": 80.0, "sessions": 1},
    ]
    events = [
        {"id": 1, "ts": now - 3600, "source": "five_hour", "session_id": None, "level": "info",
         "title": "Claude · 5 h limit at 22%", "body": "Resets at %s." % time.strftime("%H:%M", time.localtime(now + 3600))},
        {"id": 2, "ts": now - 60, "source": "context_tokens", "session_id": "demo-shop", "level": "warning",
         "title": "Claude · 335k tokens of context",
         "body": "Every turn re-reads that whole context. /compact or one session per task makes the next turns much cheaper."},
    ]
    return {"version": VERSION, "now": now, "lang": "en", "limits": limits, "sessions": sessions,
            "accounts": accounts, "groups": groups, "events": events, "cursor": 2}
