"""SQLite store. Only numbers and ids: no prompts, outputs, file contents or commands."""
import json
import os
import sqlite3
import urllib.parse

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  project_dir TEXT NOT NULL, file TEXT NOT NULL, msg_id TEXT, request_id TEXT,
  session_id TEXT, kind TEXT, ts TEXT, model TEXT,
  input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER,
  cache_write_5m INTEGER, cache_write_1h INTEGER,
  PRIMARY KEY (project_dir, file, msg_id, request_id)
);
CREATE INDEX IF NOT EXISTS requests_session ON requests(session_id);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
CREATE TABLE IF NOT EXISTS cost_state (
  project_dir TEXT NOT NULL, session_id TEXT NOT NULL, ts TEXT NOT NULL,
  total_cost_usd REAL NOT NULL, model_usage TEXT,
  PRIMARY KEY (project_dir, session_id, total_cost_usd, ts)
);
CREATE TABLE IF NOT EXISTS subagents (
  project_dir TEXT NOT NULL, session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  subagent_type TEXT, model TEXT, ts TEXT, tokens INTEGER, tool_uses INTEGER,
  duration_ms INTEGER, source TEXT,
  PRIMARY KEY (project_dir, session_id, agent_id)
);
CREATE TABLE IF NOT EXISTS cost_slices (
  day TEXT NOT NULL, project_dir TEXT NOT NULL, session_id TEXT NOT NULL,
  model TEXT NOT NULL, usd REAL NOT NULL, estimated INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS cost_slices_day ON cost_slices(day);
CREATE TABLE IF NOT EXISTS ingest_runs (
  ts TEXT NOT NULL, source TEXT, added_requests INTEGER, added_cost INTEGER, added_subagents INTEGER
);
CREATE TABLE IF NOT EXISTS limits_samples (
  ts INTEGER NOT NULL, session_id TEXT, root TEXT, five_pct REAL, five_reset INTEGER,
  seven_pct REAL, seven_reset INTEGER, ctx_pct REAL, model TEXT
);
CREATE INDEX IF NOT EXISTS limits_samples_ts ON limits_samples(ts);
CREATE TABLE IF NOT EXISTS tool_calls (
  project_dir TEXT NOT NULL, tool_use_id TEXT NOT NULL, session_id TEXT, kind TEXT, ts TEXT,
  name TEXT NOT NULL, root TEXT, msg_id TEXT, PRIMARY KEY (project_dir, tool_use_id)
);
CREATE INDEX IF NOT EXISTS tool_calls_ts ON tool_calls(ts);
CREATE TABLE IF NOT EXISTS session_dirs (
  project_dir TEXT NOT NULL, session_id TEXT NOT NULL, dir TEXT NOT NULL, ts TEXT, root TEXT,
  PRIMARY KEY (project_dir, session_id, dir)
);
CREATE TABLE IF NOT EXISTS alerts_sent (key TEXT PRIMARY KEY, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS accounts_seen (
  root TEXT NOT NULL, seen_at TEXT NOT NULL, org_hash TEXT, org_type TEXT, seat_tier TEXT,
  rate_tier TEXT, PRIMARY KEY (root, seen_at)
);
CREATE TABLE IF NOT EXISTS compactions (
  project_dir TEXT NOT NULL, session_id TEXT NOT NULL, ts TEXT NOT NULL, trigger TEXT,
  pre_tokens INTEGER, post_tokens INTEGER, precomputed INTEGER, root TEXT,
  PRIMARY KEY (project_dir, session_id, ts)
);
CREATE TABLE IF NOT EXISTS alert_events (
  ts INTEGER NOT NULL, source TEXT NOT NULL, session_id TEXT, project_dir TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS alert_events_ts ON alert_events(ts);
CREATE TABLE IF NOT EXISTS files_seen (
  path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, version INTEGER NOT NULL,
  seen_at TEXT
);
"""
SCHEMA_VERSION = 1  # PRAGMA user_version: 1 = files_seen exists. Tables and columns are still added by SCHEMA and MIGRATIONS

REQ_COLS = ("project_dir", "file", "msg_id", "request_id", "session_id", "kind", "ts", "model",
            "input", "output", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h", "root")
TOOL_COLS = ("project_dir", "tool_use_id", "session_id", "kind", "ts", "name", "root", "msg_id")
SUB_COLS = ("project_dir", "session_id", "agent_id", "subagent_type", "model", "ts", "tokens",
            "tool_uses", "duration_ms", "source", "root")

# Additive migrations: columns added after the first release. Rows that predate a column keep
# NULL, which readers treat as the default transcript root.
MIGRATIONS = (("requests", "root", "TEXT"), ("cost_state", "root", "TEXT"), ("subagents", "root", "TEXT"),
              ("accounts_seen", "extra_usage", "INTEGER"), ("tool_calls", "msg_id", "TEXT"))


BUSY_TIMEOUT_S = 15  # how long a writer waits for another one (ingest, refresh, status line) before giving up


def _enable_wal(con, timeout_s):
    """Write-ahead log: readers (dashboard, report) and the one writer stop blocking each other.

    Switching needs the database to itself for an instant, so it waits at most a second and is retried by
    the next connection. A file system without shared memory (some network shares) keeps the old journal:
    this never fails the caller.
    """
    try:
        if con.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            con.execute("PRAGMA busy_timeout = 1000")
            try:
                con.execute("PRAGMA journal_mode = WAL")
            finally:
                con.execute("PRAGMA busy_timeout = %d" % int(timeout_s * 1000))
        if con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            con.execute("PRAGMA synchronous = NORMAL")  # safe in WAL; a crash can only lose the last commits, which ingest redoes
    except sqlite3.OperationalError:
        pass


def checkpoint(con):
    """Fold the write-ahead log back into the database file (best effort), so a copy of metrics.db alone is complete."""
    try:
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass


def open_readonly(path):
    """Connection that cannot write. `mode=ro` alone fails on a WAL database whose -wal/-shm files are gone
    (nobody holds it open), because it cannot create them; then a normal connection that refuses writes is used."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(path), uri=True)
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1")  # opening is lazy: the WAL files are touched here
    except sqlite3.OperationalError:
        if not os.path.exists(path):
            raise
        con = sqlite3.connect(path)
        con.execute("PRAGMA query_only = ON")
    con.row_factory = sqlite3.Row
    return con


def connect(path, timeout=BUSY_TIMEOUT_S):
    d = os.path.dirname(path)
    if not os.path.isdir(d):  # private from the start: metrics reveal projects and working hours
        os.makedirs(d, mode=0o700, exist_ok=True)
        if os.name != "nt":
            os.chmod(d, 0o700)
    new = not os.path.exists(path)
    con = sqlite3.connect(path, timeout=timeout)
    if new and os.name != "nt":
        os.chmod(path, 0o600)  # -wal and -shm inherit the mode of the database file
    con.row_factory = sqlite3.Row
    _enable_wal(con, timeout)
    con.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
        if col not in cols:
            con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
    slice_cols = {r[1] for r in con.execute("PRAGMA table_info(cost_slices)")}
    if "root" not in slice_cols:  # derived table: safe to recreate
        con.execute("DROP TABLE cost_slices")
        con.execute("CREATE TABLE cost_slices (day TEXT NOT NULL, project_dir TEXT NOT NULL, session_id TEXT NOT NULL, "
                    "model TEXT NOT NULL, usd REAL NOT NULL, estimated INTEGER NOT NULL, root TEXT)")
        con.execute("CREATE INDEX IF NOT EXISTS cost_slices_day ON cost_slices(day)")
    if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        con.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    con.commit()
    return con


def files_seen(con):
    """{path: (size, mtime_ns, version)} of the transcripts already read, as they were when read."""
    return {r["path"]: (r["size"], r["mtime_ns"], r["version"]) for r in con.execute("SELECT * FROM files_seen")}


def mark_file_seen(con, path, size, mtime_ns, version, seen_at):
    con.execute("INSERT OR REPLACE INTO files_seen (path, size, mtime_ns, version, seen_at) VALUES (?,?,?,?,?)",
                (path, size, mtime_ns, version, seen_at))


def forget_files(con, paths):
    con.executemany("DELETE FROM files_seen WHERE path = ?", [(p,) for p in paths])


def add_account_seen(con, root, acct, seen_at):
    """Record which account a config dir is logged into (non-personal fields only)."""
    last = con.execute("SELECT org_hash, seat_tier, extra_usage FROM accounts_seen WHERE root = ? ORDER BY seen_at DESC LIMIT 1",
                       (root,)).fetchone()
    if last and (last["org_hash"], last["seat_tier"], last["extra_usage"]) == (
            acct.get("org_hash"), acct.get("seat_tier"), acct.get("extra_usage")):
        return 0
    con.execute("INSERT OR IGNORE INTO accounts_seen (root, seen_at, org_hash, org_type, seat_tier, rate_tier, extra_usage) "
                "VALUES (?,?,?,?,?,?,?)", (root, seen_at, acct.get("org_hash"), acct.get("org_type"), acct.get("seat_tier"),
                                           acct.get("rate_tier"), acct.get("extra_usage")))
    return 1


def add_request(con, r):
    cur = con.execute(
        "INSERT OR IGNORE INTO requests (%s) VALUES (%s)" % (",".join(REQ_COLS), ",".join("?" * len(REQ_COLS))),
        [r.get(c) for c in REQ_COLS])
    return cur.rowcount


def add_cost_state(con, r):
    cur = con.execute(
        "INSERT OR IGNORE INTO cost_state (project_dir, session_id, ts, total_cost_usd, model_usage, root) "
        "VALUES (?,?,?,?,?,?)",
        (r["project_dir"], r["session_id"], r["ts"], r["total_cost_usd"], json.dumps(r.get("model_usage") or {}),
         r.get("root")))
    return cur.rowcount


def add_subagent(con, r):
    """Insert, or keep the larger token/tool totals if the agent notified again later."""
    before = con.total_changes
    con.execute(
        "INSERT INTO subagents (%s) VALUES (%s) ON CONFLICT(project_dir, session_id, agent_id) DO UPDATE SET "
        "tokens = MAX(tokens, excluded.tokens), tool_uses = MAX(tool_uses, excluded.tool_uses), "
        "subagent_type = COALESCE(NULLIF(subagents.subagent_type, '?'), excluded.subagent_type), "
        "model = COALESCE(subagents.model, excluded.model) "
        "WHERE excluded.tokens > subagents.tokens OR subagents.subagent_type = '?' OR subagents.model IS NULL"
        % (",".join(SUB_COLS), ",".join("?" * len(SUB_COLS))),
        [r.get(c) for c in SUB_COLS])
    return 1 if con.total_changes > before else 0


def add_tool_call(con, r):
    """Tool name only (e.g. Read, mcp__server__tool): never its input or result.

    msg_id links the call to the request that emitted it (cost of exploration); rows stored
    before that column existed get it filled in on the next ingest.
    """
    before = con.total_changes
    con.execute(
        "INSERT INTO tool_calls (%s) VALUES (%s) ON CONFLICT(project_dir, tool_use_id) DO UPDATE SET "
        "msg_id = excluded.msg_id WHERE tool_calls.msg_id IS NULL AND excluded.msg_id IS NOT NULL"
        % (",".join(TOOL_COLS), ",".join("?" * len(TOOL_COLS))),
        [r.get(c) for c in TOOL_COLS])
    return 1 if con.total_changes > before else 0


def add_compaction(con, r):
    """A /compact or automatic compaction of a session: when, what triggered it and the token counts."""
    cur = con.execute("INSERT OR IGNORE INTO compactions (project_dir, session_id, ts, trigger, pre_tokens, post_tokens, "
                      "precomputed, root) VALUES (?,?,?,?,?,?,?,?)",
                      (r["project_dir"], r["session_id"], r["ts"], r.get("trigger"), r.get("pre_tokens"), r.get("post_tokens"),
                       r.get("precomputed"), r.get("root")))
    return cur.rowcount

def add_alert_event(con, source, data, session_id=None, project_dir=None, ts=None):
    """A warning that was shown: which check (source) and the numbers that made it fire.

    Numbers only, plus timestamps (`*_ts`) and the budget `name` from alerts.json; any other text is dropped."""
    import time
    nums = {k: v for k, v in (data or {}).items()
            if isinstance(v, (int, float)) or v is None or k.endswith("_ts") or k == "name"}
    con.execute("INSERT INTO alert_events (ts, source, session_id, project_dir, data) VALUES (?,?,?,?,?)",
                (int(ts or time.time()), source, session_id, project_dir, json.dumps(nums, sort_keys=True)))

def add_session_dir(con, r):
    """A working directory a session used, encoded like a project dir. Keeps the earliest time seen."""
    before = con.total_changes
    con.execute("INSERT INTO session_dirs (project_dir, session_id, dir, ts, root) VALUES (?,?,?,?,?) "
                "ON CONFLICT(project_dir, session_id, dir) DO UPDATE SET ts = excluded.ts "
                "WHERE excluded.ts < session_dirs.ts", (r["project_dir"], r["session_id"], r["dir"], r.get("ts"), r.get("root")))
    return 1 if con.total_changes > before else 0
