"""OpenCode 2 adapter: read session_message/session_v2, keep only numbers and names.

`session_message.data` holds JSON with the prompt, response and reasoning text; only the
declared fields (model/agent/tokens/cost/finish/error) are extracted, with SQLite's
json_extract so the text never reaches Python. On an OpenCode build without JSON1 the
same fields are picked out of a parsed dict instead. Every field is validated before it
is stored, so nothing but a short name, a finite non-negative number or a 0/1 flag can
reach the database.
"""
import datetime as dt
import json
import math
import os
import sqlite3
import time
import urllib.parse

from .. import paths

NAME = "opencode2"
ENV = "SESSIONCOACH_OPENCODE2_DIR"

# A time_updated more than this far in the future is a corrupt value, not a real cursor.
_DAY_MS = 24 * 3600 * 1000

# Seconds a read waits on the harness's own database before reporting it busy (patched short in tests).
TIMEOUT = 2

# SQLite allows at most 999 variables on older builds; query session ids in chunks well below it
# (the same figure as analysis.MAX_IN), so a first ingest with many sessions cannot fail.
_CHUNK = 500

# the `data` fields kept, as (name, JSON path, allowed json_type kinds); anything else is dropped
_FIELDS = (
    ("provider", "$.model.providerID", "text"),
    ("model", "$.model.id", "text"),
    ("agent", "$.agent", "text"),
    ("input", "$.tokens.input", ("integer", "real")),
    ("output", "$.tokens.output", ("integer", "real")),
    ("reasoning", "$.tokens.reasoning", ("integer", "real")),
    ("cache_read", "$.tokens.cache.read", ("integer", "real")),
    ("cache_write", "$.tokens.cache.write", ("integer", "real")),
    ("cost", "$.cost", ("integer", "real")),
)


def candidates():
    """Directories that may hold opencode.db, in priority order."""
    out = []
    env = os.environ.get(ENV)
    if env:
        out.append(os.path.expanduser(env))
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        out.append(os.path.join(os.path.expanduser(xdg), "opencode"))
    out.append(os.path.join(paths.HOME, ".local", "share", "opencode"))
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            out.append(os.path.join(os.path.expanduser(local), "opencode"))
    return out


def detect():
    """Path of opencode.db in the first candidate that has it, else None."""
    for d in candidates():
        db = os.path.join(d, "opencode.db")
        if os.path.isfile(db):
            return db
    return None


def _iso(ms):
    """Epoch ms -> ISO-8601 UTC with Z, or None for anything that is no usable time."""
    try:
        n = int(ms)
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        return dt.datetime.fromtimestamp(n / 1000.0, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _num(v):
    """`v` as an int (epoch ms stored as an int or numeric text), else None."""
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None


def _cursor_ms(ms):
    """The epoch ms for the cursor, or None when it is unusable (no time, or more than a day ahead)."""
    n = _num(ms)
    if n is None or _iso(n) is None:
        return None
    return None if n > int(time.time() * 1000) + _DAY_MS else n


def _number(v):
    """A non-negative, finite, SQLite-sized number (int/float, not bool), else None. Tokens and cost only."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        ok = math.isfinite(v)
    except (OverflowError, ValueError):
        return None
    return v if ok and 0 <= v < 2 ** 63 else None


def _name(v):
    """A provider/model/agent name: a str of at most 100 chars, else None. Never free text."""
    return v if isinstance(v, str) and len(v) <= 100 else None


def _json(data):
    """`data` as a dict: a JSON text column, a blob, or {} for anything else."""
    if isinstance(data, (str, bytes)):
        try:
            d = json.loads(data)
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}
    return data if isinstance(data, dict) else {}


def _extract(path, kinds):
    """CASE that yields json_extract(path) only when json_type(path) is one of `kinds`, else NULL."""
    cond = "= '%s'" % kinds if isinstance(kinds, str) else "IN (%s)" % ", ".join("'%s'" % k for k in kinds)
    return "CASE WHEN json_type(data, '%s') %s THEN json_extract(data, '%s') END" % (path, cond, path)


def read(db_path, cursor):
    """(requests, sessions, new_cursor) from `opencode.db`, incrementally since `cursor`.

    `cursor` is the max session_message.time_updated (epoch ms) already read, as text;
    None reads from the start. The boundary is inclusive (`>=`): a message written in the same
    millisecond as the cursor is still read, and the caller's upsert makes re-reading harmless.
    `new_cursor` is the new max, as text.
    """
    con = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(db_path), uri=True, timeout=TIMEOUT)
    con.row_factory = sqlite3.Row
    try:
        _require(con)
        cur = int(cursor) if cursor is not None else None
        try:
            requests, max_ms = _requests_json1(con, cur)
        except sqlite3.OperationalError as e:
            if "no such function" not in str(e):
                raise
            requests, max_ms = _requests_python(con, cur)
        return requests, _sessions(con, requests), (str(max_ms) if max_ms is not None else cursor)
    finally:
        con.close()


def max_time_updated(db_path):
    """The max usable assistant time_updated (epoch ms) in the source, or None when there are none.

    Unusable values (no time, or far in the future) are ignored, so a corrupt row cannot hide
    the real maximum. Used by the driver to notice a source replaced by an older one (its max
    below the stored cursor) and read it again from the start.
    """
    con = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(db_path), uri=True, timeout=TIMEOUT)
    try:
        rows = con.execute("SELECT time_updated FROM session_message WHERE type = 'assistant'").fetchall()
    finally:
        con.close()
    best = None
    for r in rows:
        n = _cursor_ms(r[0])
        if n is not None and (best is None or n > best):
            best = n
    return best


def _require(con):
    """A clear error for another OpenCode version's schema; the driver records it, it never crashes the ingest."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for t in ("session_message", "session_v2"):
        if t not in tables:
            raise ValueError("opencode2: missing table %s" % t)
    for t, cols in (("session_message", ("id", "session_id", "type", "time_updated", "data")),
                    ("session_v2", ("id", "parent_id", "directory", "time_created", "time_updated"))):
        have = {r[1] for r in con.execute("PRAGMA table_info(%s)" % t)}
        for c in cols:
            if c not in have:
                raise ValueError("opencode2: %s has no %s column" % (t, c))


def _request(r):
    """Map one extracted row to the external_requests columns, validating every field. `error` is 0 or 1."""
    return {"request_id": r["id"], "session_id": r["session_id"], "ts": _iso(r["time_updated"]),
            "provider": _name(r["provider"]), "model": _name(r["model"]), "agent": _name(r["agent"]),
            "input": _number(r["input"]), "output": _number(r["output"]), "reasoning": _number(r["reasoning"]),
            "cache_read": _number(r["cache_read"]), "cache_write": _number(r["cache_write"]),
            "cost_usd": _number(r["cost"]), "error": 1 if r["error"] else 0}


def _requests_json1(con, cur):
    """Assistant rows since `cur`, fields pulled out with json_extract (text never reaches Python)."""
    cols = ", ".join("%s AS %s" % (_extract(path, kinds), name) for name, path, kinds in _FIELDS)
    sql = ("SELECT id, session_id, time_updated, %s, "
           "CASE WHEN json_extract(data, '$.error') IS NOT NULL OR json_extract(data, '$.finish') = 'error' "
           "THEN 1 ELSE 0 END AS error "
           "FROM session_message WHERE type = 'assistant'" % cols)
    args = []
    if cur is not None:
        sql += " AND time_updated >= ?"
        args.append(cur)
    sql += " ORDER BY time_updated"
    requests, max_ms = [], cur
    for r in con.execute(sql, args):
        n = _cursor_ms(r["time_updated"])
        if n is None:  # an unusable timestamp: skip the row, do not store it nor advance the cursor
            continue
        requests.append(_request(r))
        if max_ms is None or n > max_ms:
            max_ms = n
    return requests, max_ms


def _requests_python(con, cur):
    """The JSON1 fallback: read data, parse, and keep exactly the same fields."""
    sql = "SELECT id, session_id, time_updated, data FROM session_message WHERE type = 'assistant'"
    args = []
    if cur is not None:
        sql += " AND time_updated >= ?"
        args.append(cur)
    sql += " ORDER BY time_updated"
    requests, max_ms = [], cur
    for r in con.execute(sql, args):
        n = _cursor_ms(r["time_updated"])
        if n is None:  # an unusable timestamp: skip the row, do not store it nor advance the cursor
            continue
        d = _json(r["data"])
        model = d.get("model") if isinstance(d.get("model"), dict) else {}
        tokens = d.get("tokens") if isinstance(d.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        requests.append(_request({
            "id": r["id"], "session_id": r["session_id"], "time_updated": r["time_updated"],
            "provider": model.get("providerID"), "model": model.get("id"), "agent": d.get("agent"),
            "input": tokens.get("input"), "output": tokens.get("output"), "reasoning": tokens.get("reasoning"),
            "cache_read": cache.get("read"), "cache_write": cache.get("write"),
            "cost": d.get("cost"), "error": 1 if (d.get("error") is not None or d.get("finish") == "error") else 0,
        }))
        if max_ms is None or n > max_ms:
            max_ms = n
    return requests, max_ms


def _sessions(con, requests):
    """session_v2 rows for the session ids seen, plus each one's compaction count."""
    ids = sorted({r["session_id"] for r in requests if r["session_id"]})
    if not ids:
        return []
    rows, comps = {}, {}
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i:i + _CHUNK]
        ph = ",".join("?" * len(chunk))
        for r in con.execute("SELECT id, parent_id, directory, time_created, time_updated FROM session_v2 "
                             "WHERE id IN (%s)" % ph, chunk):
            rows[r["id"]] = r
        for r in con.execute("SELECT session_id, COUNT(*) AS n FROM session_message WHERE type = 'compaction' "
                             "AND session_id IN (%s) GROUP BY session_id" % ph, chunk):
            comps[r["session_id"]] = r["n"]
    out = []
    for sid in ids:
        r = rows.get(sid)
        if r is None:  # the session row is gone but its messages remain
            continue
        out.append({"session_id": sid, "parent_id": r["parent_id"], "cwd": r["directory"],
                    "started": _iso(r["time_created"]), "ended": _iso(r["time_updated"]),
                    "compactions": comps.get(sid, 0)})
    return out
