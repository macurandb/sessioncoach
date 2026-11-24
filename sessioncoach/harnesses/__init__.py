"""Read other coding harnesses (OpenCode 2 now, more later) into the same metrics.db.

Each adapter reads a harness's own local database and keeps only numbers and names:
token counts, cost, ids, provider/model/agent names. Prompt, response, reasoning and tool
text never leave the adapter. Adapters run one by one from the cursor each keeps in
`harness_status`; one failing never stops the others, and each one's writes go through a
savepoint so a failure leaves nothing half-written.
"""
import datetime as dt
import sqlite3

from .. import store
from . import opencode2

ADAPTERS = [opencode2]  # priority order; new adapters are appended here as they are added


def discover():
    """[(adapter, db_path)] for every adapter whose detect() found a database. Never raises."""
    out = []
    for adapter in ADAPTERS:
        try:
            path = adapter.detect()
        except Exception:
            continue
        if path:
            out.append((adapter, path))
    return out


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _describe(e):
    return "%s: %s" % (type(e).__name__, e)


def _is_busy(e):
    msg = str(e)
    return "locked" in msg or "busy" in msg


def _cursor_for(adapter, path, row, cursor):
    """The cursor to read from: None (start over) when the db is new/different or was replaced by an older one."""
    if not row or row["path"] != path or cursor is None:
        return None
    try:
        max_ts = adapter.max_time_updated(path)
    except Exception:  # cannot tell; the read itself surfaces any real problem
        return cursor
    if max_ts is not None:
        try:
            if int(cursor) > max_ts:
                return None
        except (TypeError, ValueError):
            pass
    return cursor


def ingest(con):
    """Read every detected harness since its cursor. -> {harness_name: rows added or changed}. Never raises."""
    added = {}
    now = _now()
    for adapter in ADAPTERS:
        name = adapter.NAME
        try:
            row = con.execute("SELECT * FROM harness_status WHERE harness = ?", (name,)).fetchone()
        except sqlite3.Error:  # a read-only or broken destination: carry on without the cursor
            row = None
        cursor = row["cursor"] if row else None
        path = row["path"] if row else None
        try:
            path = adapter.detect()
            if path is not None:
                cursor = _cursor_for(adapter, path, row, cursor)
                requests, sessions, new_cursor = adapter.read(path, cursor)
                added[name] = _store(con, name, path, requests, sessions, new_cursor, now, row)
                con.commit()
        except sqlite3.OperationalError as e:
            _record_error(con, name, path, cursor, row, "busy" if _is_busy(e) else _describe(e))
        except Exception as e:
            _record_error(con, name, path, cursor, row, _describe(e))
    return added


def _store(con, name, path, requests, sessions, new_cursor, now, row):
    """Write one adapter's rows inside a savepoint; on any error roll them back and re-raise."""
    con.execute("SAVEPOINT harness_ingest")
    try:
        added_rows = 0
        for r in requests:
            r["harness"] = name
            added_rows += store.add_external_request(con, r)
        for s in sessions:
            s["harness"] = name
            added_rows += store.add_external_session(con, s)
        total = con.execute("SELECT COUNT(*) FROM external_requests WHERE harness = ?", (name,)).fetchone()[0]
        store.set_harness_status(con, name, path, new_cursor, now, None, total)
        con.execute("RELEASE harness_ingest")
        return added_rows
    except Exception:
        con.execute("ROLLBACK TO harness_ingest")
        con.execute("RELEASE harness_ingest")
        raise


def _record_error(con, name, path, cursor, row, err):
    """Record an adapter failure (type and message only) and keep the previous success and counts.

    Best-effort: a read-only or broken destination must not stop the next adapter.
    """
    try:
        store.set_harness_status(con, name, path, cursor, row["last_ok"] if row else None, err,
                                 row["rows"] if row else None)
        con.commit()
    except sqlite3.Error:
        pass


def status(con):
    """Rows of `harness_status`, plus adapters detected now that have no row yet."""
    out = [dict(r) for r in con.execute("SELECT * FROM harness_status ORDER BY rowid")]
    seen = {r["harness"] for r in out}
    for adapter in ADAPTERS:
        if adapter.NAME in seen:
            continue
        try:
            path = adapter.detect()
        except Exception:
            path = None
        if path:
            out.append({"harness": adapter.NAME, "path": path, "cursor": None,
                        "last_ok": None, "last_error": None, "rows": None})
    return out
