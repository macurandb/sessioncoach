"""Performance work must not change a single figure: SQL filters give what the Python filters gave."""
import contextlib
import datetime as dt
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate

from sessioncoach import analysis, collect, cost, limits, review, store

ROOTS = ("a", "b")
DIRS = {"a": ("-Users-me-code-alpha", "-Users-me-code-beta"), "b": ("-Users-me-code-gamma", "-Users-me-code-alpha")}


def usage(ts, msg, cw=0, cr=1000):
    return {"type": "assistant", "timestamp": ts, "requestId": "r-" + msg, "sessionId": "s",
            "message": {"id": msg, "model": "claude-opus-5-5", "role": "assistant", "content": [],
                        "usage": {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cw}}}


class Synthetic(unittest.TestCase):
    """Two transcript roots (two accounts), three project dirs, two tenants, 40 days of activity."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(isolate.rmtree, self.tmp)
        self.prev_env = os.environ.get("KIT_DATA_DIR")
        os.environ["KIT_DATA_DIR"] = self.tmp
        self.addCleanup(self.restore_env)
        self.roots = {r: os.path.join(self.tmp, r, "projects") for r in ROOTS}
        self.today = dt.date(2026, 10, 15)
        n = 0
        for r, dirs in DIRS.items():
            for d in dirs:
                os.makedirs(os.path.join(self.roots[r], d))
                lines = []
                for back in range(0, 40, 2 if r == "a" else 3):
                    day = self.today - dt.timedelta(days=back)
                    for hour in (1, 9, 23):  # 01:00 and 23:00 UTC cross local midnight in some time zones
                        n += 1
                        lines.append(usage("%sT%02d:00:00.250Z" % (day, hour), "m%d" % n, cw=60000 if hour == 9 else 0))
                    if back % 4 == 0:
                        lines.append({"type": "cost-state", "totalCostUSD": 1.5 * (40 - back),
                                      "modelUsage": {"claude-opus-5-5": {"costUSD": 1.5 * (40 - back)}}})
                with open(os.path.join(self.roots[r], d, "%s.jsonl" % r), "w") as fh:
                    fh.writelines(json.dumps(line) + "\n" for line in lines)
        with open(os.path.join(self.tmp, "projects.json"), "w") as fh:
            json.dump({"rules": [{"match": "*alpha", "project": "alpha", "tenant": "acme"},
                                 {"match": "*beta", "project": "beta", "tenant": "acme"},
                                 {"match": "*gamma", "project": "gamma", "tenant": "globex"}]}, fh)
        with open(os.path.join(self.tmp, "accounts.json"), "w") as fh:
            json.dump({"accounts": {"p": {"label": "P", "company": "acme", "org_hash": "h1"},
                                    "q": {"label": "Q", "company": "globex", "org_hash": "h2"}}}, fh)
        self.con = store.connect(os.path.join(self.tmp, "metrics.db"))
        self.addCleanup(self.con.close)
        collect.ingest(self.con, list(self.roots.values()))
        for r, h in (("a", "h1"), ("b", "h2")):
            self.con.execute("INSERT INTO accounts_seen (root, seen_at, org_hash, org_type) VALUES (?,?,?,?)",
                             (os.path.normpath(self.roots[r]), "2026-01-01T00:00:00+00:00", h, "claude_max"))
        cost.rebuild_slices(self.con)

    def restore_env(self):
        if self.prev_env is None:
            os.environ.pop("KIT_DATA_DIR", None)
        else:
            os.environ["KIT_DATA_DIR"] = self.prev_env

    def python_only(self):
        """Patches that switch every SQL narrowing off, leaving the Python filters: the old behaviour."""
        return (mock.patch.object(analysis.Dims, "scope", lambda self, *a, **k: ("", [])),
                mock.patch.object(analysis, "ts_range", lambda *a, **k: ("", [])),
                mock.patch.object(analysis, "day_range", lambda *a, **k: ("", [])))

    def both(self, fn, *args, **kw):
        """(result with SQL narrowing, result with Python filters only)."""
        fast = fn(*args, **kw)
        a, b, c = self.python_only()
        with a, b, c:
            slow = fn(*args, **kw)
        return json.dumps(fast, sort_keys=True, default=str), json.dumps(slow, sort_keys=True, default=str)


class TestSqlFiltersEquivalent(Synthetic):
    FILTERS = ({}, {"tenant": "acme"}, {"tenant": "globex"}, {"project": "alpha"}, {"project": "nope"},
               {"account": "p"}, {"account": "q"}, {"tenant": "acme", "project": "alpha", "account": "q"},
               {"account": "auto:zz"})

    def test_data_is_there(self):
        self.assertGreater(self.con.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 100)
        self.assertEqual({r[0] for r in self.con.execute("SELECT DISTINCT root FROM requests")},
                         {os.path.normpath(p) for p in self.roots.values()})

    def test_build_same_for_every_window_and_filter(self):
        for days in (None, 1, 3, 7, 30, 90):
            for f in self.FILTERS:
                fast, slow = self.both(analysis.build, self.con, days=days, today=self.today, **f)
                self.assertEqual(fast, slow, "days=%s filters=%s" % (days, f))

    def test_build_filters_actually_filter(self):
        everything = analysis.build(self.con, days=None, today=self.today)
        acme = analysis.build(self.con, days=None, today=self.today, tenant="acme")
        self.assertLess(acme["summary"]["total_usd"], everything["summary"]["total_usd"])
        self.assertEqual({p["tenant"] for p in acme["projects"]}, {"acme"})
        self.assertEqual({a["id"] for a in analysis.build(self.con, days=None, today=self.today, account="q")["accounts"]}, {"q"})

    def test_day_same(self):
        for off in (0, 1, 2, 9, 30):
            for f in self.FILTERS:
                now = dt.datetime.combine(self.today, dt.time(15, 0)).astimezone()
                date = self.today - dt.timedelta(days=off)
                fast, slow = self.both(analysis.day, self.con, date=date, now=now, **f)
                self.assertEqual(fast, slow, "off=%s filters=%s" % (off, f))

    def test_tools_view_same(self):
        self.con.executemany("INSERT INTO tool_calls (project_dir, tool_use_id, session_id, kind, ts, name) VALUES (?,?,?,?,?,?)",
                             [("-Users-me-code-alpha", "t%d" % i, "s", "main",
                               "%sT12:00:00Z" % (self.today - dt.timedelta(days=i)), "Read" if i % 2 else "mcp__x__y")
                              for i in range(60)])
        for days in (None, 1, 7, 30):
            for proj in (None, "alpha", "gamma"):
                fast, slow = self.both(analysis.tools_view, self.con, days=days, project=proj, today=self.today)
                self.assertEqual(fast, slow, "days=%s project=%s" % (days, proj))

    def test_review_same(self):
        for off in (0, 1, 5, 20):
            run = self.today - dt.timedelta(days=off)
            fast, slow = self.both(review.build, self.con, run, cfg=review.load_config({}))
            self.assertEqual(fast, slow, "run=%s" % run)

    def test_budgets_same(self):
        budgets = [{"name": "all-m", "scope": "all", "period": "month", "equiv_usd": 50},
                   {"name": "all-d", "scope": "all", "period": "day", "equiv_usd": 5},
                   {"name": "t", "scope": "tenant", "match": "acme", "period": "month", "equiv_usd": 20},
                   {"name": "p", "scope": "project", "match": "gamma", "period": "month", "equiv_usd": 20},
                   {"name": "a", "scope": "account", "match": "q", "period": "month", "equiv_usd": 20},
                   {"name": "x", "scope": "account", "match": "nobody", "period": "day", "equiv_usd": 20},
                   {"name": "n", "scope": "project", "period": "month", "equiv_usd": 20}]
        for off in (0, 3, 20):
            today = self.today - dt.timedelta(days=off)
            fast, slow = self.both(limits.evaluate_budgets, self.con, {"budgets": budgets}, today=today, dry=True)
            self.assertEqual(fast, slow, "today=%s" % today)


class TestScope(Synthetic):
    def dims(self):
        return analysis.Dims(self.con)

    def test_no_filter_no_sql(self):
        self.assertEqual(self.dims().scope(self.con), ("", []))

    def test_tenant_and_project_become_dir_lists(self):
        sql, params = self.dims().scope(self.con, tenant="acme")
        self.assertEqual(sql, "project_dir IN (?,?)")
        self.assertEqual(params, ["-Users-me-code-alpha", "-Users-me-code-beta"])
        sql, params = self.dims().scope(self.con, tenant="acme", project="beta")
        self.assertEqual(params, ["-Users-me-code-beta"])

    def test_account_becomes_root_list(self):
        sql, params = self.dims().scope(self.con, account="q")
        self.assertEqual(params, [os.path.normpath(self.roots["b"])])
        self.assertNotIn("IS NULL", sql)

    def test_unknown_value_matches_nothing(self):
        sql, params = self.dims().scope(self.con, tenant="nobody")
        self.assertEqual((sql, params), ("project_dir IN ()", []))

    def test_null_root_is_the_default_root(self):
        d = self.dims()
        d.resolver.default_root = os.path.normpath(self.roots["a"])
        self.assertIn("root IS NULL", d.scope(self.con, account="p")[0])
        self.assertNotIn("root IS NULL", d.scope(self.con, account="q")[0])

    def test_long_lists_fall_back_to_python_filter(self):
        with mock.patch.object(analysis, "MAX_IN", 0):
            self.assertEqual(self.dims().scope(self.con, tenant="acme", account="p"), ("", []))
            fast, slow = self.both(analysis.build, self.con, days=30, today=self.today, tenant="acme", account="p")
            self.assertEqual(fast, slow)

    def test_ts_bounds_contain_the_window(self):
        lo, hi = analysis.ts_bounds(dt.date(2026, 10, 10), dt.date(2026, 10, 15))
        self.assertLess(lo, "2026-10-09")
        self.assertGreater(hi, "2026-10-17")
        self.assertEqual(analysis.ts_bounds(None, dt.date(2026, 10, 15))[0], None)


class FakeCon:
    """Stands in for a connection to see what _enable_wal asks of SQLite."""

    def __init__(self, mode="delete", fail=None):
        self.mode, self.fail, self.calls = mode, fail, []

    def execute(self, sql):
        self.calls.append(sql)
        if self.fail and self.fail in sql:
            raise sqlite3.OperationalError("database is locked")
        if sql.endswith("= WAL"):
            self.mode = self.wal_result
        return mock.Mock(fetchone=lambda: (self.mode,))

    wal_result = "wal"


class TestWal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(isolate.rmtree, self.tmp)
        self.db = os.path.join(self.tmp, "data", "metrics.db")

    def mode(self, con):
        return con.execute("PRAGMA journal_mode").fetchone()[0]

    def test_connect_turns_wal_on_and_keeps_files_private(self):
        con = store.connect(self.db)
        self.assertEqual(self.mode(con), "wal")
        con.execute("INSERT INTO alerts_sent VALUES ('k', 1)")
        con.commit()
        if os.name != "nt":
            self.assertEqual(os.stat(os.path.dirname(self.db)).st_mode & 0o777, 0o700)
            for suffix in ("", "-wal", "-shm"):
                self.assertEqual(os.stat(self.db + suffix).st_mode & 0o077, 0, suffix)
        con.close()

    def test_existing_rollback_journal_database_is_converted_without_losing_rows(self):
        os.makedirs(os.path.dirname(self.db))
        old = sqlite3.connect(self.db)
        old.executescript(store.SCHEMA)
        old.execute("INSERT INTO alerts_sent VALUES ('k', 1)")
        old.commit()
        self.assertEqual(old.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        old.close()
        con = store.connect(self.db)
        self.assertEqual(self.mode(con), "wal")
        self.assertEqual(con.execute("SELECT key FROM alerts_sent").fetchall()[0][0], "k")
        con.close()

    def test_reader_is_not_blocked_by_a_write_in_progress(self):
        con = store.connect(self.db)
        con.execute("INSERT INTO alerts_sent VALUES ('a', 1)")
        con.commit()
        con.execute("INSERT INTO alerts_sent VALUES ('b', 2)")  # open transaction, not committed
        reader = store.open_readonly(self.db)
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0], 1)
        con.commit()
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0], 2)
        reader.close()
        con.close()

    def test_open_readonly_works_when_the_wal_files_are_gone_and_never_writes(self):
        store.connect(self.db).close()
        for suffix in ("-wal", "-shm"):
            if os.path.exists(self.db + suffix):
                os.remove(self.db + suffix)
        ro = store.open_readonly(self.db)
        self.assertEqual(ro.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0], 0)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO alerts_sent VALUES ('x', 1)")
        ro.close()

    def test_open_readonly_does_not_create_a_missing_database(self):
        os.makedirs(os.path.dirname(self.db))
        with self.assertRaises(sqlite3.OperationalError):
            store.open_readonly(self.db)
        self.assertFalse(os.path.exists(self.db))

    def test_file_system_without_wal_keeps_the_old_journal_and_does_not_fail(self):
        con = FakeCon()
        con.wal_result = "delete"  # what SQLite answers when the file system cannot do shared memory
        store._enable_wal(con, 5)
        self.assertFalse([c for c in con.calls if "synchronous" in c])

    def test_busy_database_does_not_fail_connect(self):
        store._enable_wal(FakeCon(fail="= WAL"), 5)  # another connection holds it: stays as it is, retried next time
        store._enable_wal(FakeCon(fail="journal_mode"), 5)

    def test_already_wal_is_not_switched_again(self):
        con = FakeCon(mode="wal")
        store._enable_wal(con, 5)
        self.assertFalse([c for c in con.calls if c.endswith("= WAL")])
        self.assertTrue([c for c in con.calls if "synchronous" in c])

    def test_status_line_gives_up_sooner_than_batch_jobs(self):
        seen = {}

        def spy(path, timeout=None):
            seen["timeout"] = timeout
            raise sqlite3.OperationalError("database is locked")
        self.assertLess(limits.STATUSLINE_BUSY_S, store.BUSY_TIMEOUT_S)
        with mock.patch.object(limits.store, "connect", spy), mock.patch.object(sys, "stdin", io.StringIO("{}")), \
                contextlib.redirect_stdout(io.StringIO()):
            limits.statusline_main()
        self.assertEqual(seen["timeout"], limits.STATUSLINE_BUSY_S)

    def test_checkpoint_folds_the_log_into_the_database_file(self):
        con = store.connect(self.db)
        con.execute("INSERT INTO alerts_sent VALUES ('k', 1)")
        con.commit()
        self.assertGreater(os.path.getsize(self.db + "-wal"), 0)
        store.checkpoint(con)
        self.assertEqual(os.path.getsize(self.db + "-wal"), 0)
        con.close()
        copy = os.path.join(self.tmp, "copy.db")
        shutil.copy(self.db, copy)  # a plain copy of the database file has the row
        self.assertEqual(sqlite3.connect(copy).execute("SELECT key FROM alerts_sent").fetchone()[0], "k")

    def test_checkpoint_never_fails_the_caller(self):
        con = FakeCon()
        con.commit = lambda: None
        con.fail = "wal_checkpoint"
        store.checkpoint(con)


def sub_lines(n):
    """A main transcript where an Agent call, its result and cost-state lines sit far apart."""
    note = ("<task-notification><task-id>a1</task-id><usage><subagent_tokens>%d</subagent_tokens>"
            "<tool_uses>3</tool_uses><duration_ms>1000</duration_ms></usage></task-notification>")
    lines = [usage("2026-10-01T10:00:00Z", "m1"),
             {"type": "assistant", "timestamp": "2026-10-01T10:01:00Z", "message": {"id": "ag", "role": "assistant", "content": [
                 {"type": "tool_use", "id": "tu-agent", "name": "Agent", "input": {"subagent_type": "Explore"}}]}},
             {"type": "cost-state", "totalCostUSD": 1.0, "modelUsage": {"claude-opus-5-5": {"costUSD": 1.0}}},
             usage("2026-10-01T10:02:00Z", "m2"),
             {"type": "user", "timestamp": "2026-10-01T10:03:00Z", "cwd": "/Users/me/code/alpha",
              "toolUseResult": {"agentId": "a1", "resolvedModel": "claude-sonnet-5-5", "totalTokens": 400},
              "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu-agent"}]}},
             {"type": "cost-state", "totalCostUSD": 2.5, "modelUsage": {"claude-opus-5-5": {"costUSD": 2.5}}},
             usage("2026-10-01T10:30:00Z", "m3"),
             {"type": "user", "timestamp": "2026-10-01T10:31:00Z", "message": {"role": "user", "content": note % 900}}]
    return lines[:n]


class TestIncrementalIngest(unittest.TestCase):
    """The tables after any sequence of file changes equal those of reading everything every time."""

    SKIP = ("files_seen", "ingest_runs")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(isolate.rmtree, self.tmp)
        self.projects = os.path.join(self.tmp, "projects")
        self.pdir = os.path.join(self.projects, "-Users-me-code-alpha")
        os.makedirs(os.path.join(self.pdir, "s1", "subagents"))
        self.ref = self.con("ref.db")

    def con(self, name="metrics.db"):
        con = store.connect(os.path.join(self.tmp, name))
        self.addCleanup(con.close)
        return con

    def path(self, name="s1.jsonl"):
        return os.path.join(self.pdir, name)

    def write(self, name, lines, text=None):
        with open(self.path(name), "w") as fh:
            fh.write(text if text is not None else "".join(json.dumps(x) + "\n" for x in lines))

    def dump(self, con):
        out = {}
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            if name not in self.SKIP:
                out[name] = sorted(json.dumps([tuple(r)], default=str) for r in con.execute("SELECT * FROM %s" % name))
        return out

    def ingest(self, con, ref=True, **kw):
        """Incremental ingest of `con`; the reference database reads everything (full) after each one."""
        stats = {}
        added = collect.ingest(con, [self.projects], stats=stats, **kw)
        if ref:
            collect.ingest(self.ref, [self.projects], full=True)
        return added, stats

    def same_as_full(self, incremental):
        self.assertEqual(self.dump(incremental), self.dump(self.ref))

    def test_unchanged_files_are_skipped_and_nothing_changes(self):
        self.write("s1.jsonl", sub_lines(8))
        self.write("s2.jsonl", [usage("2026-10-02T10:00:00Z", "n1")])
        con = self.con()
        _added, st = self.ingest(con)
        self.assertEqual((st["files"], st["scanned"], st["skipped"]), (2, 2, 0))
        before = self.dump(con)
        added2, st2 = self.ingest(con)
        self.assertEqual((st2["scanned"], st2["skipped"]), (0, 2))
        self.assertEqual(added2, {"req": 0, "cost": 0, "sub": 0})
        self.assertEqual(self.dump(con), before)
        self.same_as_full(con)

    def test_growth_in_steps_that_split_agent_call_result_and_cost_state(self):
        con = self.con()
        for n in (1, 2, 3, 5, 6, 7, 8):  # each cut falls between the lines that depend on each other
            self.write("s1.jsonl", sub_lines(n))
            _added, st = self.ingest(con)
            self.assertEqual(st["scanned"], 1, n)
            self.same_as_full(con)
        sub = con.execute("SELECT subagent_type, model, tokens FROM subagents").fetchall()
        self.assertEqual([tuple(r) for r in sub], [("Explore", "claude-sonnet-5-5", 900)])

    def test_partial_last_line_then_completed(self):
        con = self.con()
        full = "".join(json.dumps(x) + "\n" for x in sub_lines(8))
        self.write("s1.jsonl", None, text=full[:len(full) - 40])  # cut in the middle of the last line
        self.ingest(con)
        self.write("s1.jsonl", None, text=full)
        _added, st = self.ingest(con)
        self.assertEqual(st["scanned"], 1)
        self.same_as_full(con)

    def test_truncated_or_replaced_file_is_read_again(self):
        con = self.con()
        self.write("s1.jsonl", sub_lines(8))
        self.ingest(con)
        self.write("s1.jsonl", [usage("2026-10-05T10:00:00Z", "z1")])  # rotated: shorter, other content
        _added, st = self.ingest(con)
        self.assertEqual(st["scanned"], 1)
        self.same_as_full(con)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM requests WHERE msg_id = 'z1'").fetchone()[0], 1)

    def test_same_size_but_touched_file_is_read_again(self):
        con = self.con()
        self.write("s1.jsonl", sub_lines(3))
        self.ingest(con)
        st = os.stat(self.path())
        os.utime(self.path(), ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.assertEqual(self.ingest(con)[1]["scanned"], 1)

    def test_deleted_file_keeps_its_rows_and_loses_its_marker(self):
        con = self.con()
        self.write("s1.jsonl", sub_lines(8))
        self.write("s2.jsonl", [usage("2026-10-02T10:00:00Z", "n1")])
        self.ingest(con)
        os.remove(self.path("s2.jsonl"))
        _added, st = self.ingest(con)
        self.assertEqual((st["files"], st["scanned"], st["skipped"]), (1, 0, 1))
        self.assertEqual([r[0] for r in con.execute("SELECT path FROM files_seen")], [self.path()])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM requests WHERE msg_id = 'n1'").fetchone()[0], 1)

    def test_new_and_subagent_files_are_picked_up(self):
        con = self.con()
        self.write("s1.jsonl", sub_lines(3))
        self.ingest(con)
        self.write("s3.jsonl", [usage("2026-10-03T10:00:00Z", "k1")])
        self.write("s1/subagents/agent-x.jsonl", [usage("2026-10-01T10:05:00Z", "sa1")])
        _added, st = self.ingest(con)
        self.assertEqual((st["files"], st["scanned"], st["skipped"]), (3, 2, 1))
        self.same_as_full(con)

    def test_full_reads_everything_and_refreshes_the_markers(self):
        con = self.con()
        self.write("s1.jsonl", sub_lines(8))
        self.ingest(con)
        _added, st = self.ingest(con, full=True)
        self.assertEqual((st["scanned"], st["skipped"]), (1, 0))
        self.assertEqual(self.ingest(con)[1]["skipped"], 1)

    def test_a_new_scan_version_reads_old_files_again(self):
        con = self.con()
        self.write("s1.jsonl", sub_lines(8))
        self.ingest(con)
        with mock.patch.object(collect, "SCAN_VERSION", collect.SCAN_VERSION + 1):
            self.assertEqual(self.ingest(con)[1]["scanned"], 1)
            self.assertEqual(self.ingest(con)[1]["skipped"], 1)

    def test_bytes_appended_while_reading_are_read_next_time(self):
        con = self.con()
        self.write("s1.jsonl", sub_lines(3))
        real_scan = collect.scan

        def scan_and_append(*a):
            yield from real_scan(*a)
            with open(self.path(), "a") as fh:  # the session writes a line while we were reading
                fh.write(json.dumps(usage("2026-10-01T11:00:00Z", "late")) + "\n")
        with mock.patch.object(collect, "scan", scan_and_append):
            self.ingest(con, ref=False)
        self.assertEqual(self.ingest(con, ref=False)[1]["scanned"], 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM requests WHERE msg_id = 'late'").fetchone()[0], 1)

    def test_unreadable_file_is_not_marked_and_is_retried(self):
        con = self.con()
        self.write("s1.jsonl", sub_lines(3))

        def broken(*a):
            raise OSError("disk went away")
            yield  # makes this a generator like scan
        with mock.patch.object(collect, "scan", broken):
            self.assertEqual(self.ingest(con, ref=False)[1]["scanned"], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM files_seen").fetchone()[0], 0)
        self.assertEqual(self.ingest(con)[1]["scanned"], 1)

    def test_file_that_vanishes_between_listing_and_reading_is_skipped(self):
        con = self.con()
        self.write("s1.jsonl", sub_lines(3))
        with mock.patch.object(collect.os, "stat", side_effect=FileNotFoundError):
            self.assertEqual(self.ingest(con, ref=False)[1]["files"], 0)

    def test_progress_is_committed_every_few_files(self):
        con = self.con()
        for i in range(5):
            self.write("s%d.jsonl" % (i + 1), [usage("2026-10-02T10:0%d:00Z" % i, "p%d" % i)])
        other = sqlite3.connect(os.path.join(self.tmp, "metrics.db"))
        self.addCleanup(other.close)
        with mock.patch.object(collect, "COMMIT_EVERY", 2), \
                mock.patch.object(collect, "_log_run", side_effect=RuntimeError("stop before the final commit")), \
                self.assertRaises(RuntimeError):
            self.ingest(con, ref=False)
        self.assertEqual(other.execute("SELECT COUNT(*) FROM files_seen").fetchone()[0], 4)  # 2 commits of 2 files
        self.assertEqual(other.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 4)  # rows and markers together


class TestMigrationOfAnOlderDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(isolate.rmtree, self.tmp)
        self.db = os.path.join(self.tmp, "metrics.db")
        self.root = os.path.join(self.tmp, "projects")
        projects = os.path.join(self.root, "-Users-me-code-alpha")
        os.makedirs(projects)
        with open(os.path.join(projects, "s1.jsonl"), "w") as fh:
            fh.writelines(json.dumps(x) + "\n" for x in sub_lines(8))

    def old_database(self):
        """A database as the previous version left it: no files_seen, user_version 0, journal mode delete."""
        con = sqlite3.connect(self.db)
        con.executescript(store.SCHEMA.split("CREATE TABLE IF NOT EXISTS files_seen")[0])
        con.execute("INSERT INTO alerts_sent VALUES ('review:2026-10-01', 1)")
        con.commit()
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 0)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("files_seen", tables)
        con.close()

    def test_adds_the_table_and_version_and_keeps_every_row(self):
        self.old_database()
        con = store.connect(self.db)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM files_seen").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT key FROM alerts_sent").fetchone()[0], "review:2026-10-01")
        store.connect(self.db).close()  # idempotent
        con.close()

    def test_first_ingest_reads_every_file_once_then_skips(self):
        self.old_database()
        con = store.connect(self.db)
        stats = {}
        collect.ingest(con, [self.root], stats=stats)
        self.assertEqual((stats["scanned"], stats["skipped"]), (1, 0))
        collect.ingest(con, [self.root], stats=stats)
        self.assertEqual((stats["scanned"], stats["skipped"]), (0, 1))
        fresh = store.connect(os.path.join(self.tmp, "fresh.db"))
        collect.ingest(fresh, [self.root])
        for t in ("requests", "cost_state", "subagents", "tool_calls", "session_dirs"):
            q = "SELECT * FROM %s ORDER BY 1, 2, 3" % t
            self.assertEqual([tuple(r) for r in con.execute(q)], [tuple(r) for r in fresh.execute(q)], t)
        con.close()
        fresh.close()

    def test_old_code_still_reads_the_new_database(self):
        """Going back is safe: the extra table and the version number are ignored by the previous SCHEMA."""
        con = store.connect(self.db)
        collect.ingest(con, [self.root])
        con.close()
        old = sqlite3.connect(self.db)
        old.executescript(store.SCHEMA.split("CREATE TABLE IF NOT EXISTS files_seen")[0])
        self.assertGreater(old.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)
        old.close()
