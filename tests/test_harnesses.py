"""harnesses: read other coding harnesses (OpenCode 2) into metrics.db, numbers and names only.

Everything runs against a synthetic opencode.db in a temp dir and a temp HOME/data dir; the real
~/.local/share/opencode is never touched.
"""
import ast
import contextlib
import datetime as dt
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # drops SESSIONCOACH_* and XDG_DATA_HOME left in the shell

from sessioncoach import analysis, cli, harnesses, installer, paths, store
from sessioncoach.harnesses import opencode2

SECRET = "SECRET-DO-NOT-STORE-4711"


def ms(y, mo, d, h, mi, s=0):
    return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.timezone.utc).timestamp() * 1000)


def recent_ms(days_ago, hour=12):
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return int(base.replace(hour=hour, minute=0, second=0, microsecond=0).timestamp() * 1000)


def msg_data(provider="opencode-go", model="deepseek-v4-pro", agent="build", inp=2194, out=925, reason=258,
             cr=315392, cw=0, cost=0.0123, finish="stop", error=None):
    d = {"time": {}, "agent": agent, "model": {"id": model, "providerID": provider, "variant": "default"},
         "content": [{"type": "text", "text": SECRET}], "finish": finish, "cost": cost,
         "tokens": {"input": inp, "output": out, "reasoning": reason, "cache": {"read": cr, "write": cw}}}
    if error is not None:
        d["error"] = error
    return json.dumps(d)


def build_opencode(path, messages, sessions, with_session_v2=True):
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER, "
                      "time_created INTEGER, time_updated INTEGER, data TEXT);")
    if with_session_v2:
        con.executescript("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT, "
                          "directory TEXT, title TEXT, version INTEGER, time_created INTEGER, time_updated INTEGER);")
        con.executemany("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?)", sessions)
    con.executemany("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)", messages)
    con.commit()
    con.close()


class _Adapter:
    """A fake harness adapter for the driver-failure tests."""

    def __init__(self, name, path, fail=None, read_result=None):
        self.NAME = name
        self.path = path
        self.fail = fail
        self.read_result = read_result

    def detect(self):
        if self.fail == "detect":
            raise RuntimeError("boom-detect")
        return self.path

    def read(self, db_path, cursor):
        if self.fail == "read":
            raise RuntimeError("boom-read")
        return self.read_result if self.read_result is not None else ([], [], cursor)

    def max_time_updated(self, db_path):
        return None


_FAKE_REQ = {"request_id": "m1", "session_id": "s1", "ts": "2026-11-24T15:00:00.000Z",
             "provider": "p", "model": "m", "agent": "a", "input": 1, "output": 2, "reasoning": 0,
             "cache_read": 0, "cache_write": 0, "cost_usd": 0.1, "error": 0}
_FAKE_SESS = {"session_id": "s1", "parent_id": None, "cwd": "/w", "started": "2026-11-24T15:00:00.000Z",
              "ended": "2026-11-24T15:00:00.000Z", "compactions": 0}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = paths.HOME
        self.saved = {k: os.environ.get(k) for k in ("SESSIONCOACH_OPENCODE2_DIR", "XDG_DATA_HOME",
                                                     "SESSIONCOACH_DATA_DIR", "KIT_DATA_DIR")}
        for k in self.saved:
            os.environ.pop(k, None)
        paths.HOME = os.path.join(self.tmp, "home")
        os.makedirs(paths.HOME, exist_ok=True)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data, exist_ok=True)
        self.metrics = os.path.join(self.data, "metrics.db")
        # a cleanup, not tearDown: cleanups run last-in first-out after tearDown, so the connections the
        # tests close with addCleanup are closed before this removes their files (Windows cannot delete open files)
        self.addCleanup(self._restore)

    def _restore(self):
        paths.HOME = self.home
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        isolate.rmtree(self.tmp)

    def make_opencode(self, messages, sessions, with_session_v2=True):
        d = tempfile.mkdtemp(dir=self.tmp)
        db = os.path.join(d, "opencode.db")
        build_opencode(db, messages, sessions, with_session_v2)
        return d, db


class TestDetect(Base):
    def test_detect_via_env_default_and_none(self):
        d, db = self.make_opencode([], [])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        self.assertEqual(opencode2.detect(), db)
        del os.environ["SESSIONCOACH_OPENCODE2_DIR"]
        default = os.path.join(paths.HOME, ".local", "share", "opencode")
        os.makedirs(default)
        db2 = os.path.join(default, "opencode.db")
        with open(db2, "w"):
            pass
        self.assertEqual(opencode2.detect(), db2)
        os.remove(db2)
        self.assertIsNone(opencode2.detect())

    def test_xdg_data_home_is_a_candidate(self):
        xdg = os.path.join(self.tmp, "xdg")
        d = os.path.join(xdg, "opencode")
        os.makedirs(d)
        build_opencode(os.path.join(d, "opencode.db"), [], [])
        os.environ["XDG_DATA_HOME"] = xdg  # opencode.db lives at <XDG>/opencode/opencode.db
        self.assertEqual(opencode2.detect(), os.path.join(d, "opencode.db"))


class TestRead(Base):
    def session_rows(self, t1, t2):
        return [("s1", "p1", None, "slug-1", "/work/app", "the title", 1, t1, t2),
                ("s2", "p1", "s1", "slug-2", "/work/other", "other title", 1, t1, t2)]

    def test_assistant_fields_non_assistant_ignored_errors_and_compactions(self):
        t1, t2 = ms(2026, 11, 24, 15, 0, 0), ms(2026, 11, 24, 16, 0, 0)
        messages = [
            ("m1", "s1", "assistant", 1, t1, t1, msg_data(inp=100, out=200, reason=50, cr=300, cw=0, cost=0.5)),
            ("m2", "s1", "user", 2, t1, t1, json.dumps({"type": "text", "text": SECRET})),
            ("m3", "s2", "assistant", 3, t2, t2, msg_data(inp=10, out=20, reason=5, cr=30, cw=5, cost=0.25, finish="error")),
            ("m4", "s2", "assistant", 4, t2, t2, msg_data(inp=1, out=2, reason=0, cr=3, cw=0, cost=0.0,
                                                          error={"code": "boom"})),
            ("m5", "s1", "compaction", 5, t2, t2, json.dumps({})),
        ]
        d, _ = self.make_opencode(messages, self.session_rows(t1, t2))
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        self.assertEqual(harnesses.ingest(con), {"opencode2": 5})  # 3 requests + 2 sessions

        reqs = {r["request_id"]: r for r in con.execute("SELECT * FROM external_requests")}
        self.assertEqual(set(reqs), {"m1", "m3", "m4"})
        self.assertEqual((reqs["m1"]["provider"], reqs["m1"]["model"], reqs["m1"]["agent"]),
                         ("opencode-go", "deepseek-v4-pro", "build"))
        self.assertEqual((reqs["m1"]["input"], reqs["m1"]["output"], reqs["m1"]["reasoning"],
                          reqs["m1"]["cache_read"], reqs["m1"]["cache_write"]), (100, 200, 50, 300, 0))
        self.assertAlmostEqual(reqs["m1"]["cost_usd"], 0.5)
        self.assertEqual(reqs["m1"]["error"], 0)
        self.assertEqual(reqs["m1"]["ts"], "2026-11-24T15:00:00.000Z")
        self.assertEqual((reqs["m3"]["error"], reqs["m4"]["error"]), (1, 1))  # finish='error' and error field

        sess = {s["session_id"]: s for s in con.execute("SELECT * FROM external_sessions")}
        self.assertEqual(set(sess), {"s1", "s2"})
        self.assertEqual((sess["s1"]["cwd"], sess["s1"]["compactions"]), ("/work/app", 1))
        self.assertEqual((sess["s2"]["parent_id"], sess["s2"]["compactions"]), ("s1", 0))
        self.assertEqual(sess["s1"]["started"], "2026-11-24T15:00:00.000Z")
        self.assertIsNone(sess["s1"]["parent_id"])

    def test_incremental_read_and_updated_message_replaces(self):
        t1, t2 = ms(2026, 11, 24, 15, 0, 0), ms(2026, 11, 24, 16, 0, 0)
        messages = [("m1", "s1", "assistant", 1, t1, t1, msg_data(out=1)),
                    ("m2", "s1", "assistant", 2, t2, t2, msg_data(out=2))]
        d, db = self.make_opencode(messages, self.session_rows(t1, t2))
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        self.assertEqual(harnesses.ingest(con), {"opencode2": 3})  # 2 requests + 1 session
        self.assertEqual(con.execute("SELECT rows FROM harness_status WHERE harness = 'opencode2'").fetchone()[0], 2)
        self.assertEqual(harnesses.ingest(con), {"opencode2": 0})  # nothing new
        self.assertEqual(con.execute("SELECT COUNT(*) FROM external_requests").fetchone()[0], 2)
        # the total stays put after a quiet run (never resets to 0)
        self.assertEqual(con.execute("SELECT rows FROM harness_status WHERE harness = 'opencode2'").fetchone()[0], 2)

        t3 = ms(2026, 11, 25, 10, 0, 0)
        c = sqlite3.connect(db)
        c.execute("UPDATE session_message SET time_updated = ?, data = ? WHERE id = 'm1'", (t3, msg_data(out=99)))
        c.commit()
        c.close()
        self.assertEqual(harnesses.ingest(con), {"opencode2": 1})  # m1 changed; its session re-read is unchanged
        self.assertEqual(con.execute("SELECT COUNT(*) FROM external_requests").fetchone()[0], 2)  # replaced, not added
        self.assertEqual(con.execute("SELECT output FROM external_requests WHERE request_id = 'm1'").fetchone()[0], 99)

    def test_boundary_row_with_the_same_time_updated_is_reread(self):
        t1 = ms(2026, 11, 24, 15, 0, 0)
        d, db = self.make_opencode([("m1", "s1", "assistant", 1, t1, t1, msg_data(out=1)),
                                    ("m2", "s1", "assistant", 2, t1, t1, msg_data(out=2))],
                                   [("s1", "p1", None, "s", "/w", "t", 1, t1, t1)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        self.assertEqual(harnesses.ingest(con), {"opencode2": 3})  # both messages share the boundary time
        c = sqlite3.connect(db)
        c.execute("UPDATE session_message SET data = ? WHERE id = 'm2'", (msg_data(out=99),))
        c.commit()
        c.close()
        # m2 still sits at the cursor (t1) but changed: `>=` re-reads it; m1 is unchanged and quiet
        self.assertEqual(harnesses.ingest(con), {"opencode2": 1})
        self.assertEqual(con.execute("SELECT output FROM external_requests WHERE request_id = 'm2'").fetchone()[0], 99)

    def test_sessions_are_queried_in_chunks(self):
        t1 = ms(2026, 11, 24, 15, 0, 0)
        n = 6
        messages = [("m%d" % i, "s%d" % i, "assistant", i, t1, t1, msg_data()) for i in range(n)]
        sessions = [("s%d" % i, "p1", None, "s", "/w%d" % i, "t", 1, t1, t1) for i in range(n)]
        _d, db = self.make_opencode(messages, sessions)
        with mock.patch.object(opencode2, "_CHUNK", 2):
            requests, sess, cursor = opencode2.read(db, None)
        self.assertEqual(len(requests), n)
        self.assertEqual(cursor, str(t1))
        self.assertEqual({s["session_id"] for s in sess}, {"s%d" % i for i in range(n)})

    def test_no_text_stored(self):
        t1 = ms(2026, 11, 24, 15, 0, 0)
        d, _ = self.make_opencode([("m1", "s1", "assistant", 1, t1, t1, msg_data())],
                                  [("s1", "p1", None, "slug", "/work", SECRET, 1, t1, t1)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        harnesses.ingest(con)
        store.checkpoint(con)
        con.close()
        with open(self.metrics, "rb") as fh:
            self.assertNotIn(SECRET.encode(), fh.read())

    def test_json1_fallback(self):
        t1 = ms(2026, 11, 24, 15, 0, 0)
        _d, db = self.make_opencode([("m1", "s1", "assistant", 1, t1, t1, msg_data(inp=7, out=8, cr=9, cw=1))],
                                   self.session_rows(t1, t1))
        with mock.patch.object(opencode2, "_requests_json1",
                               side_effect=sqlite3.OperationalError("no such function: json_extract")):
            requests, sessions, cursor = opencode2.read(db, None)
        self.assertEqual(cursor, str(t1))
        self.assertEqual(len(requests), 1)
        self.assertEqual((requests[0]["provider"], requests[0]["model"], requests[0]["agent"]),
                         ("opencode-go", "deepseek-v4-pro", "build"))
        self.assertEqual((requests[0]["input"], requests[0]["output"], requests[0]["cache_read"]), (7, 8, 9))
        self.assertEqual((sessions[0]["cwd"], sessions[0]["compactions"]), ("/work/app", 0))

    def test_broken_database_records_error_and_continues(self):
        d, _ = self.make_opencode([("m1", "s1", "assistant", 1, 1, 1, msg_data())], [], with_session_v2=False)
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        self.assertEqual(harnesses.ingest(con), {})  # returns, does not raise
        row = con.execute("SELECT * FROM harness_status WHERE harness = 'opencode2'").fetchone()
        self.assertIsNotNone(row)
        self.assertIn("opencode2: missing table session_v2", row["last_error"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM external_requests").fetchone()[0], 0)

    def test_busy_database_recorded_as_busy(self):
        d, _ = self.make_opencode([("m1", "s1", "assistant", 1, 1, 1, msg_data())], self.session_rows(1, 1))
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        with mock.patch.object(opencode2, "read", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(harnesses.ingest(con), {})
        row = con.execute("SELECT last_error FROM harness_status WHERE harness = 'opencode2'").fetchone()
        self.assertEqual(row["last_error"], "busy")

    def test_real_locked_source_is_busy(self):
        t1 = ms(2026, 11, 24, 15, 0, 0)
        d, db = self.make_opencode([("m1", "s1", "assistant", 1, t1, t1, msg_data())],
                                   [("s1", "p1", None, "s", "/w", "t", 1, t1, t1)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        locker = sqlite3.connect(db)
        locker.execute("BEGIN EXCLUSIVE")
        try:
            with mock.patch.object(opencode2, "TIMEOUT", 0.1):
                self.assertEqual(harnesses.ingest(con), {})
        finally:
            locker.rollback()
            locker.close()
        row = con.execute("SELECT last_error FROM harness_status WHERE harness = 'opencode2'").fetchone()
        self.assertEqual(row["last_error"], "busy")

    def test_cursor_resets_when_path_changes(self):
        t0, t1 = ms(2026, 11, 24, 14, 0, 0), ms(2026, 11, 24, 15, 0, 0)
        d1, _ = self.make_opencode([("m1", "s1", "assistant", 1, t1, t1, msg_data(out=1))],
                                   [("s1", "p1", None, "s", "/w", "t", 1, t1, t1)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d1
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        harnesses.ingest(con)  # cursor = t1
        d2, _ = self.make_opencode([("m2", "s2", "assistant", 1, t0, t0, msg_data(out=2))],
                                   [("s2", "p1", None, "s", "/w", "t", 1, t0, t0)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d2  # a different db, older than the cursor
        self.assertEqual(harnesses.ingest(con), {"opencode2": 2})  # m2 + s2, re-read from the start
        ids = {r["request_id"] for r in con.execute("SELECT request_id FROM external_requests")}
        self.assertEqual(ids, {"m1", "m2"})

    def test_cursor_resets_when_source_is_replaced(self):
        t0, t1 = ms(2026, 11, 24, 14, 0, 0), ms(2026, 11, 24, 15, 0, 0)
        d, db = self.make_opencode([("m1", "s1", "assistant", 1, t1, t1, msg_data(out=1))],
                                   [("s1", "p1", None, "s", "/w", "t", 1, t1, t1)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        harnesses.ingest(con)  # cursor = t1
        c = sqlite3.connect(db)  # replace the source with an older one at the same path
        c.executescript("DELETE FROM session_message; DELETE FROM session_v2;")
        c.execute("INSERT INTO session_message VALUES ('m2','s2','assistant',1,?,?,?)", (t0, t0, msg_data(out=2)))
        c.execute("INSERT INTO session_v2 VALUES ('s2','p1',NULL,'s','/w','t',1,?,?)", (t0, t0))
        c.commit()
        c.close()
        self.assertEqual(harnesses.ingest(con), {"opencode2": 2})  # max t0 < cursor t1: re-read
        ids = {r["request_id"] for r in con.execute("SELECT request_id FROM external_requests")}
        self.assertEqual(ids, {"m1", "m2"})

    def test_unusable_timestamp_does_not_advance_cursor(self):
        huge = 9223372036854775807
        d, db = self.make_opencode(
            [("m1", "s1", "assistant", 1, 1000, 1000, msg_data(out=1)),
             ("m2", "s1", "assistant", 2, huge, huge, msg_data(out=2))],
            [("s1", "p1", None, "s", "/w", "t", 1, 1000, 1000)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        self.assertEqual(harnesses.ingest(con), {"opencode2": 2})  # m1 + s1; the huge row is skipped
        self.assertEqual(con.execute("SELECT cursor FROM harness_status WHERE harness = 'opencode2'").fetchone()[0], "1000")
        c = sqlite3.connect(db)
        c.execute("INSERT INTO session_message VALUES ('m3','s1','assistant',3,2000,2000,?)", (msg_data(out=3),))
        c.commit()
        c.close()
        # a valid row later than the unusable one is still read (the cursor never jumped to `huge`)
        self.assertEqual(harnesses.ingest(con), {"opencode2": 1})
        ids = {r["request_id"] for r in con.execute("SELECT request_id FROM external_requests")}
        self.assertEqual(ids, {"m1", "m3"})

    def test_infinite_timestamp_is_skipped(self):
        d, _db = self.make_opencode(
            [("m1", "s1", "assistant", 1, 1000, 1000, msg_data(out=1)),
             ("m2", "s1", "assistant", 2, float("inf"), float("inf"), msg_data(out=2))],
            [("s1", "p1", None, "s", "/w", "t", 1, 1000, 1000)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        self.assertEqual(harnesses.ingest(con), {"opencode2": 2})  # m1 + s1; the infinite row is skipped
        self.assertEqual(con.execute("SELECT cursor FROM harness_status WHERE harness = 'opencode2'").fetchone()[0], "1000")
        ids = {r["request_id"] for r in con.execute("SELECT request_id FROM external_requests")}
        self.assertEqual(ids, {"m1"})

    def test_max_time_updated_ignores_unusable(self):
        huge = 9223372036854775807
        _d, db = self.make_opencode([("m1", "s1", "assistant", 1, 1000, 1000, msg_data()),
                                     ("m2", "s1", "assistant", 2, huge, huge, msg_data())], [])
        self.assertEqual(opencode2.max_time_updated(db), 1000)

    def test_unusable_timestamp_skipped_in_fallback(self):
        huge = 9223372036854775807
        _d, db = self.make_opencode([("m1", "s1", "assistant", 1, 1000, 1000, msg_data(out=1)),
                                     ("m2", "s1", "assistant", 2, huge, huge, msg_data(out=2))], [])
        with mock.patch.object(opencode2, "_requests_json1",
                               side_effect=sqlite3.OperationalError("no such function: json_extract")):
            requests, _sessions, cursor = opencode2.read(db, None)
        self.assertEqual([r["request_id"] for r in requests], ["m1"])
        self.assertEqual(cursor, "1000")

    def test_bad_field_values_are_not_stored(self):
        t1 = ms(2026, 11, 24, 15, 0, 0)
        data = {"agent": {"x": 1}, "model": {"id": "x" * 101, "providerID": {"p": 1}},
                "tokens": {"input": SECRET, "output": {"a": 1}, "reasoning": [1, 2],
                           "cache": {"read": True, "write": -5}},
                "cost": "0.5", "finish": "stop"}
        d, _ = self.make_opencode([("m1", "s1", "assistant", 1, t1, t1, json.dumps(data))],
                                  [("s1", "p1", None, "s", "/w", "t", 1, t1, t1)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        harnesses.ingest(con)
        req = con.execute("SELECT * FROM external_requests WHERE request_id = 'm1'").fetchone()
        for col in ("provider", "model", "agent", "input", "output", "reasoning", "cache_read", "cache_write", "cost_usd"):
            self.assertIsNone(req[col], col)
        store.checkpoint(con)
        with open(self.metrics, "rb") as fh:
            self.assertNotIn(SECRET.encode(), fh.read())
        v = analysis.harness_view(con, days=0, today=dt.date(2026, 12, 10))
        self.assertEqual(len(v["rows"]), 1)  # still one (opencode2, None, None) row, no crash


class TestViewAndCli(Base):
    def test_harness_view_figures(self):
        t1, t2 = ms(2026, 11, 24, 15, 0, 0), ms(2026, 11, 24, 16, 0, 0)
        messages = [
            ("m1", "s1", "assistant", 1, t1, t1, msg_data(inp=100, out=200, reason=50, cr=300, cw=0, cost=0.5)),
            ("m2", "s1", "assistant", 2, t2, t2, msg_data(inp=10, out=20, reason=5, cr=30, cw=5, cost=0.25,
                                                          finish="error")),
        ]
        d, _ = self.make_opencode(messages, [("s1", "p1", None, "slug", "/work/app", "t", 1, t1, t2)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        harnesses.ingest(con)
        v = analysis.harness_view(con, days=0, today=dt.date(2026, 12, 10))
        self.assertEqual(len(v["rows"]), 1)
        r = v["rows"][0]
        self.assertEqual((r["harness"], r["provider"], r["model"]), ("opencode2", "opencode-go", "deepseek-v4-pro"))
        self.assertEqual((r["sessions"], r["requests"]), (1, 2))
        self.assertEqual((r["input"], r["output"], r["reasoning"], r["cache_read"], r["cache_write"]), (110, 220, 55, 330, 5))
        self.assertAlmostEqual(r["cost_usd"], 0.75)
        self.assertEqual(r["cache_share"], 0.75)  # 330 / (110 + 330)
        self.assertEqual(r["context_per_request"], 222.5)  # (110 + 330 + 5) / 2
        self.assertEqual(r["error_rate"], 0.5)

    def test_status_includes_detected_before_ingest(self):
        d, _ = self.make_opencode([], [])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        st = harnesses.status(con)
        self.assertEqual(len(st), 1)
        self.assertEqual(st[0]["harness"], "opencode2")
        self.assertIsNone(st[0]["last_error"])

    def test_cli_with_data_and_with_none(self):
        t1, t2 = recent_ms(2), recent_ms(1)
        messages = [
            ("m1", "s1", "assistant", 1, t1, t1, msg_data(inp=100, out=200, reason=50, cr=300, cw=0, cost=0.5)),
            ("m2", "s1", "assistant", 2, t2, t2, msg_data(inp=10, out=20, reason=5, cr=30, cw=5, cost=0.25)),
        ]
        d, _ = self.make_opencode(messages, [("s1", "p1", None, "slug", "/work/app", "t", 1, t1, t2)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        os.environ["SESSIONCOACH_DATA_DIR"] = self.data
        con = store.connect(self.metrics)
        harnesses.ingest(con)
        con.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["harness"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("sessioncoach harness", text)
        self.assertIn("opencode-go", text)
        self.assertIn("deepseek-v4-pro", text)
        self.assertIn("opencode2 at", text)  # the status line

        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        store.connect(os.path.join(empty, "metrics.db")).close()
        os.environ["SESSIONCOACH_DATA_DIR"] = empty
        del os.environ["SESSIONCOACH_OPENCODE2_DIR"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["harness"])
        self.assertEqual(rc, 0)
        self.assertIn("no harness data yet", out.getvalue())
        self.assertIn("sessioncoach ingest", out.getvalue())

    def test_check_items(self):
        t1 = ms(2026, 11, 24, 15, 0, 0)
        d, _ = self.make_opencode([("m1", "s1", "assistant", 1, t1, t1, msg_data())],
                                  [("s1", "p1", None, "slug", "/work/app", "t", 1, t1, t1)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        os.environ["SESSIONCOACH_DATA_DIR"] = self.data
        con = store.connect(self.metrics)
        harnesses.ingest(con)
        con.close()
        items = {n: (s, de) for s, n, de in installer.check(cwd=self.tmp)}
        self.assertEqual(items["harness opencode2"][0], installer.OK)

        # a broken read leaves a warning instead
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = self.make_opencode(
            [("m1", "s1", "assistant", 1, t1, t1, msg_data())], [], with_session_v2=False)[0]
        con = store.connect(self.metrics)
        harnesses.ingest(con)
        con.close()
        items = {n: (s, de) for s, n, de in installer.check(cwd=self.tmp)}
        self.assertEqual(items["harness opencode2"][0], installer.WARN)
        self.assertIn("opencode2: missing table", items["harness opencode2"][1])

    def test_check_says_not_read_yet(self):
        t1 = ms(2026, 11, 24, 15, 0, 0)
        d, _ = self.make_opencode([("m1", "s1", "assistant", 1, t1, t1, msg_data())],
                                  [("s1", "p1", None, "slug", "/work/app", "t", 1, t1, t1)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        os.environ["SESSIONCOACH_DATA_DIR"] = self.data
        store.connect(self.metrics).close()  # a database, but never ingested
        items = {n: (s, de) for s, n, de in installer.check(cwd=self.tmp)}
        self.assertEqual(items["harness opencode2"][0], installer.WARN)
        self.assertIn("not read yet", items["harness opencode2"][1])
        # and with no database at all
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        os.environ["SESSIONCOACH_DATA_DIR"] = empty
        items = {n: (s, de) for s, n, de in installer.check(cwd=self.tmp)}
        self.assertEqual(items["harness opencode2"][0], installer.WARN)
        self.assertIn("not read yet", items["harness opencode2"][1])

    def test_check_says_not_read_yet_when_db_lacks_harness_status(self):
        t1 = ms(2026, 11, 24, 15, 0, 0)
        d, _ = self.make_opencode([("m1", "s1", "assistant", 1, t1, t1, msg_data())],
                                  [("s1", "p1", None, "slug", "/work/app", "t", 1, t1, t1)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        os.environ["SESSIONCOACH_DATA_DIR"] = self.data
        raw = sqlite3.connect(self.metrics)
        raw.execute("CREATE TABLE something (x)")
        raw.commit()
        raw.close()  # a database without harness_status (an install older than the harnesses feature)
        items = {n: (s, de) for s, n, de in installer.check(cwd=self.tmp)}
        self.assertEqual(items["harness opencode2"][0], installer.WARN)
        self.assertIn("not read yet", items["harness opencode2"][1])

    def test_status_line_shows_last_ok_and_error_independently(self):
        t1 = ms(2026, 11, 24, 15, 0, 0)
        d, _ = self.make_opencode([("m1", "s1", "assistant", 1, t1, t1, msg_data())],
                                  [("s1", "p1", None, "slug", "/work/app", "t", 1, t1, t1)])
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = d
        os.environ["SESSIONCOACH_DATA_DIR"] = self.data
        con = store.connect(self.metrics)
        harnesses.ingest(con)  # success -> last_ok
        con.close()
        os.environ["SESSIONCOACH_OPENCODE2_DIR"] = self.make_opencode(
            [("m1", "s1", "assistant", 1, t1, t1, msg_data())], [], with_session_v2=False)[0]
        con = store.connect(self.metrics)
        harnesses.ingest(con)  # failure -> last_error, last_ok kept
        con.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["harness"]), 0)
        text = out.getvalue()
        self.assertIn("last ok", text)
        self.assertIn("last error", text)


class TestAdapterEdgeCases(Base):
    def test_candidates_windows_localappdata(self):
        with mock.patch.object(os, "name", "nt"), mock.patch.dict(os.environ, {"LOCALAPPDATA": "/local"}):
            self.assertIn(os.path.join("/local", "opencode"), opencode2.candidates())

    def test_iso_and_num_reject_non_numbers(self):
        self.assertIsNone(opencode2._iso(None))
        self.assertIsNone(opencode2._iso("not-a-number"))
        self.assertIsNone(opencode2._num("x"))
        self.assertEqual(opencode2._num("123"), 123)

    def test_iso_rejects_unusable_times(self):
        self.assertIsNone(opencode2._iso(9223372036854775807))  # OverflowError from fromtimestamp
        self.assertIsNone(opencode2._iso(-9223372036854775808))
        self.assertIsNone(opencode2._iso(float("inf")))  # OverflowError from int()

    def test_number_validation(self):
        for bad in ("5", {"a": 1}, [1], True, -1, float("nan"), float("inf"), 2 ** 80, 10 ** 400):
            self.assertIsNone(opencode2._number(bad), bad)
        self.assertEqual(opencode2._number(0), 0)
        self.assertEqual(opencode2._number(5), 5)
        self.assertEqual(opencode2._number(0.5), 0.5)

    def test_name_validation(self):
        self.assertEqual(opencode2._name("opencode-go"), "opencode-go")
        self.assertIsNone(opencode2._name("x" * 101))
        self.assertIsNone(opencode2._name(123))
        self.assertIsNone(opencode2._name({"id": "x"}))
        self.assertIsNone(opencode2._name(["a"]))

    def test_json_rejects_bad_and_non_dict(self):
        self.assertEqual(opencode2._json("not json"), {})
        self.assertEqual(opencode2._json(42), {})
        self.assertEqual(opencode2._json("[1,2]"), {})

    def test_read_propagates_non_json1_operational_error(self):
        _d, db = self.make_opencode([("m1", "s1", "assistant", 1, 1, 1, msg_data())], [])
        with mock.patch.object(opencode2, "_requests_json1", side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertRaises(sqlite3.OperationalError):
            opencode2.read(db, None)

    def test_require_missing_column(self):
        _d, db = self.make_opencode([], [])
        con = sqlite3.connect(db)
        con.executescript("DROP TABLE session_v2; CREATE TABLE session_v2 "
                          "(id TEXT PRIMARY KEY, parent_id TEXT, time_created INTEGER, time_updated INTEGER);")
        con.commit()
        con.close()
        with self.assertRaisesRegex(ValueError, "opencode2: session_v2 has no directory column"):
            opencode2.read(db, None)

    def test_python_fallback_with_cursor(self):
        t1, t2 = ms(2026, 11, 24, 15, 0, 0), ms(2026, 11, 24, 16, 0, 0)
        _d, db = self.make_opencode([("m1", "s1", "assistant", 1, t1, t1, msg_data(out=1)),
                                     ("m2", "s1", "assistant", 2, t2, t2, msg_data(out=2))],
                                    [("s1", "p1", None, "s", "/w", "t", 1, t1, t2)])
        with mock.patch.object(opencode2, "_requests_json1",
                               side_effect=sqlite3.OperationalError("no such function: json_extract")):
            requests, _sessions, cursor = opencode2.read(db, str(t1))
        self.assertEqual([r["request_id"] for r in requests], ["m1", "m2"])  # `>=`: m1 is at the boundary
        self.assertEqual(cursor, str(t2))

    def test_session_row_gone_is_skipped(self):
        t1 = ms(2026, 11, 24, 15, 0, 0)
        _d, db = self.make_opencode([("m1", "gone", "assistant", 1, t1, t1, msg_data())], [])
        _requests, sessions, _cursor = opencode2.read(db, None)
        self.assertEqual(sessions, [])


class TestDriverFailure(Base):
    """One adapter failing must never stop the next, nor leave half-written rows behind."""

    def _good(self):
        return _Adapter("second", "/fake/opencode.db", read_result=([dict(_FAKE_REQ)], [dict(_FAKE_SESS)], "1000"))

    def _run(self, first, extra=None):
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        ctx = extra if extra else contextlib.nullcontext()
        with ctx, mock.patch.object(harnesses, "ADAPTERS", [first, self._good()]):
            return con, harnesses.ingest(con)

    def _assert(self, con, added):
        self.assertEqual(added, {"second": 2})
        self.assertEqual(con.execute("SELECT COUNT(*) FROM external_requests WHERE harness = 'second'").fetchone()[0], 1)
        err = con.execute("SELECT last_error FROM harness_status WHERE harness = 'first'").fetchone()["last_error"]
        self.assertIn("boom", err)

    def test_detect_failure_does_not_stop_the_next(self):
        con, added = self._run(_Adapter("first", "/x/opencode.db", fail="detect"))
        self._assert(con, added)

    def test_read_failure_does_not_stop_the_next(self):
        con, added = self._run(_Adapter("first", "/x/opencode.db", fail="read"))
        self._assert(con, added)

    def test_storage_failure_rolls_back_and_does_not_stop_the_next(self):
        real = store.add_external_request

        def add(con_, r):
            if r.get("harness") == "first":
                raise RuntimeError("boom-store")
            return real(con_, r)
        con, added = self._run(_Adapter("first", "/x/opencode.db", read_result=([dict(_FAKE_REQ)], [dict(_FAKE_SESS)], "1")),
                               extra=mock.patch.object(store, "add_external_request", side_effect=add))
        self._assert(con, added)
        # the failed first adapter left nothing behind
        self.assertEqual(con.execute("SELECT COUNT(*) FROM external_requests WHERE harness = 'first'").fetchone()[0], 0)

    def test_readonly_destination_still_continues(self):
        # a read-only destination makes the first adapter's storage fail; error recording must be
        # best-effort so the second adapter's detect() still runs and ingest returns without raising
        setup = store.connect(self.metrics)
        store.checkpoint(setup)
        setup.close()
        second = mock.Mock()
        second.NAME = "second"
        second.detect.return_value = None
        first = _Adapter("first", "/x/opencode.db", read_result=([dict(_FAKE_REQ)], [dict(_FAKE_SESS)], "1"))
        ro = store.open_readonly(self.metrics)
        try:
            with mock.patch.object(harnesses, "ADAPTERS", [first, second]):
                self.assertEqual(harnesses.ingest(ro), {})
        finally:
            ro.close()
        second.detect.assert_called_once()


class TestDriverHelpers(Base):
    def _adapters(self):
        return _Adapter("good", "/good.db"), _Adapter("bad", "/bad.db", fail="detect")

    def test_discover_skips_a_failing_detect(self):
        good, bad = self._adapters()
        with mock.patch.object(harnesses, "ADAPTERS", [bad, good]):
            self.assertEqual(harnesses.discover(), [(good, "/good.db")])

    def test_status_skips_a_failing_detect(self):
        good, bad = self._adapters()
        con = store.connect(self.metrics)
        self.addCleanup(con.close)
        with mock.patch.object(harnesses, "ADAPTERS", [bad, good]):
            st = harnesses.status(con)
        self.assertEqual([s["harness"] for s in st], ["good"])

    def test_cursor_for_keeps_cursor_when_max_unavailable(self):
        a = _Adapter("x", "/x.db")

        def raise_max(path):
            raise RuntimeError("boom")
        a.max_time_updated = raise_max
        self.assertEqual(harnesses._cursor_for(a, "/x.db", {"path": "/x.db"}, "1000"), "1000")

    def test_cursor_for_keeps_cursor_when_source_has_no_requests(self):
        a = _Adapter("x", "/x.db")
        a.max_time_updated = lambda path: None
        self.assertEqual(harnesses._cursor_for(a, "/x.db", {"path": "/x.db"}, "1000"), "1000")

    def test_cursor_for_keeps_cursor_when_it_is_not_a_number(self):
        a = _Adapter("x", "/x.db")
        a.max_time_updated = lambda path: 5
        self.assertEqual(harnesses._cursor_for(a, "/x.db", {"path": "/x.db"}, "garbage"), "garbage")


class TestPackaging(unittest.TestCase):
    def test_every_package_directory_is_shipped(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r"packages\s*=\s*(\[[^\]]*\])", text)
        packages = ast.literal_eval(m.group(1))
        pkg = os.path.join(root, "sessioncoach")
        for entry in sorted(os.listdir(pkg)):
            sub = os.path.join(pkg, entry)
            if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, "__init__.py")):
                self.assertIn("sessioncoach.%s" % entry, packages, entry)
        self.assertIn("sessioncoach", packages)


if __name__ == "__main__":
    unittest.main()
