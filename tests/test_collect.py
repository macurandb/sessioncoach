"""collect: transcripts of odd shapes never stop the ingest nor reach the database as text."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate

from sessioncoach import collect, cost, store

SECRET = "SECRET-DO-NOT-STORE-4711"


def usage_line(ts, msg_id, model="claude-opus-5-5", req=None):
    line = {"type": "assistant", "timestamp": ts, "sessionId": "s1",
            "message": {"id": msg_id, "model": model, "role": "assistant", "content": [{"type": "text", "text": SECRET}],
                        "usage": {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000,
                                  "cache_creation_input_tokens": 0}}}
    if req:
        line["requestId"] = req
    return line


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(isolate.rmtree, self.tmp)
        self.projects = os.path.join(self.tmp, "projects")
        self.db = os.path.join(self.tmp, "metrics.db")

    def write(self, project, name, lines, raw=()):
        d = os.path.join(self.projects, project)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "a") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
            for line in raw:
                fh.write(line + "\n")
        return path

    def ingest(self, con=None):
        con = con or store.connect(self.db)
        self.addCleanup(con.close)
        added = collect.ingest(con, [self.projects])
        cost.rebuild_slices(con)
        return con, added


class TestMissingIds(Base):
    def test_a_line_without_request_id_is_stored_once(self):
        self.write("-p", "s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1")])
        con, _ = self.ingest()
        self.write("-p", "s1.jsonl", [usage_line("2026-10-01T10:01:00Z", "m2", req="r2")])  # the file grows: read again
        self.ingest(con)
        rows = [tuple(r) for r in con.execute("SELECT msg_id, request_id FROM requests ORDER BY msg_id")]
        self.assertEqual(rows, [("m1", ""), ("m2", "r2")])


class TestTimestamps(Base):
    def test_parse_ts_of_anything_but_text_is_none(self):
        for v in (1760000000, 1.5, None, ["2026-10-01"], {"t": 1}, "", "yesterday"):
            self.assertIsNone(cost.parse_ts(v))
        self.assertEqual(cost.parse_ts("2026-10-01T10:00:00Z").year, 2026)

    def test_a_number_as_timestamp_is_not_stored(self):
        line = usage_line(1760000000, "m1", req="r1")
        line["cwd"] = "/Users/me/app"
        self.write("-p", "s1.jsonl", [line, usage_line("2026-10-01T10:01:00Z", "m2", req="r2")])
        con, added = self.ingest()  # rebuild_slices reads every ts: a number broke it
        self.assertEqual(added["req"], 2)
        rows = {r["msg_id"]: r["ts"] for r in con.execute("SELECT msg_id, ts FROM requests")}
        self.assertEqual(rows, {"m1": None, "m2": "2026-10-01T10:01:00Z"})
        self.assertEqual(con.execute("SELECT COUNT(*) FROM session_dirs").fetchone()[0], 0)


class TestNames(Base):
    FREE = "password=hunter2 " + "x" * 200

    def test_free_text_tool_names_and_models_never_reach_the_db(self):
        odd = usage_line("2026-10-01T10:00:00Z", "m1", model=self.FREE, req="r1")
        odd["message"]["content"] = [{"type": "tool_use", "id": "tu1", "name": self.FREE, "input": {}},
                                     {"type": "tool_use", "id": "tu2", "name": "mcp__srv__search", "input": {}},
                                     {"type": "tool_use", "id": "tu3", "name": "R" * 101, "input": {}}]
        ok = usage_line("2026-10-01T10:01:00Z", "m2", model="claude-opus-5-5[1m]", req="r2")
        cost_state = {"type": "cost-state", "totalCostUSD": 2.0,
                      "modelUsage": {self.FREE: {"costUSD": 1.0}, "claude-opus-5-5": {"costUSD": 1.0}}}
        agent = {"type": "user", "timestamp": "2026-10-01T10:02:00Z",
                 "toolUseResult": {"agentId": "a1", "totalTokens": 10, "resolvedModel": self.FREE},
                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu9"}]}}
        self.write("-p", "s1.jsonl", [odd, ok, cost_state, agent])
        con, added = self.ingest()
        self.assertEqual(added["req"], 2)  # a request with an odd model still counts, as "?"
        models = {r["msg_id"]: r["model"] for r in con.execute("SELECT msg_id, model FROM requests")}
        self.assertEqual(models, {"m1": "?", "m2": "claude-opus-5-5[1m]"})
        tools = {r["tool_use_id"]: r["name"] for r in con.execute("SELECT tool_use_id, name FROM tool_calls")}
        self.assertEqual(tools, {"tu1": "?", "tu2": "mcp__srv__search", "tu3": "?"})
        self.assertIsNone(con.execute("SELECT model FROM subagents").fetchone()["model"])
        self.assertEqual(json.loads(con.execute("SELECT model_usage FROM cost_state").fetchone()[0]).keys(),
                         {"claude-opus-5-5"})
        con.close()
        with open(self.db, "rb") as fh:
            self.assertNotIn(b"hunter2", fh.read())


class TestOddShapes(Base):
    ODD = ('["timestamp"]', '"timestamp"', "3", "null",
           '{"timestamp": "2026-10-01T09:00:00Z", "message": {"id": "x1", "usage": "n/a"}}',
           '{"timestamp": "2026-10-01T09:00:00Z", "usage": [1], "message": {"id": "x2", "usage": [1]}}',
           '{"timestamp": "2026-10-01T09:00:00Z", "requestId": {"a": 1}, "message": {"id": ["m"], "model": "m",'
           ' "usage": {"input_tokens": "1.5", "output_tokens": NaN, "cache_read_input_tokens": [1],'
           ' "cache_creation_input_tokens": 1e300, "cache_creation": "x"},'
           ' "content": [{"type": "tool_use", "id": {"a": 1}, "name": "Read"},'
           ' {"type": "tool_use", "id": "tu-a", "name": "Agent", "input": "text"}]}}',
           '{"type": "user", "timestamp": "2026-10-01T09:01:00Z",'
           ' "toolUseResult": {"agentId": "a1", "totalTokens": "1.5", "totalToolUseCount": [2], "totalDurationMs": "x"},'
           ' "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": {"a": 1}}]}}',
           '{"type": "cost-state", "totalCostUSD": "abc", "modelUsage": {"m": {"costUSD": 1}}}',
           '{"type": "cost-state", "totalCostUSD": 1.0, "modelUsage": [1]}',
           '{"type": "cost-state", "totalCostUSD": 2.0, "modelUsage": {"m": "n/a", "k": {"costUSD": "1", "inputTokens": 3}}}',
           '{"type": "system", "subtype": "compact_boundary", "timestamp": "2026-10-01T09:02:00Z",'
           ' "compactMetadata": {"trigger": ["auto"], "preTokens": "many", "postTokens": 10}}')

    def test_odd_lines_are_skipped_and_the_rest_is_ingested(self):
        bad = self.write("-a", "s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1", req="r1")], raw=self.ODD)
        good = self.write("-b", "s2.jsonl", [usage_line("2026-10-01T10:00:00Z", "m2", req="r2")])
        con, added = self.ingest()
        self.assertEqual(added, {"req": 3, "cost": 2, "sub": 1})  # m1, m2 and the one with odd fields
        self.assertEqual(set(store.files_seen(con)), {bad, good})
        r = con.execute("SELECT * FROM requests WHERE model = 'm'").fetchone()
        self.assertEqual((r["msg_id"], r["request_id"], r["input"], r["output"], r["cache_read"], r["cache_write"],
                          r["cache_write_5m"]), ("", "", 1, 0, 0, 0, None))
        self.assertEqual(tuple(con.execute("SELECT tokens, tool_uses, duration_ms FROM subagents").fetchone()), (1, 0, 0))
        mus = [json.loads(m[0]) for m in con.execute("SELECT model_usage FROM cost_state ORDER BY total_cost_usd")]
        self.assertEqual(mus[0], {})
        self.assertEqual(mus[1]["k"]["inputTokens"], 3)
        self.assertIsNone(mus[1]["k"]["costUSD"])
        self.assertEqual(mus[1]["m"]["costUSD"], None)
        c = con.execute("SELECT trigger, pre_tokens, post_tokens FROM compactions").fetchone()
        self.assertEqual(tuple(c), (None, None, 10))

    def test_a_file_that_still_fails_is_reported_and_read_again_next_time(self):
        bad = self.write("-a", "s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1", req="r1"),
                                            usage_line("2026-10-01T10:01:00Z", "m3", req="r3")])
        good = self.write("-b", "s2.jsonl", [usage_line("2026-10-01T10:00:00Z", "m2", req="r2")])
        real = collect.scan

        def scan(pdir, path, kind, session):
            for i, rec in enumerate(real(pdir, path, kind, session)):
                if path == bad and i == 1:
                    raise ValueError(SECRET)  # what int() or json put in a message: never printed
                yield rec

        err = io.StringIO()
        with mock.patch.object(collect, "scan", scan), contextlib.redirect_stderr(err):
            con, added = self.ingest()
        self.assertIn(bad, err.getvalue())
        self.assertIn("ValueError", err.getvalue())
        self.assertNotIn(SECRET, err.getvalue())
        self.assertEqual(set(store.files_seen(con)), {good})  # later projects still ingested
        self.assertEqual(added["req"], 2)
        _, added = self.ingest(con)  # read again; what was stored before the failure is not doubled
        self.assertEqual(added["req"], 1)
        self.assertEqual(set(store.files_seen(con)), {bad, good})
        self.assertEqual(con.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 3)

    def test_a_locked_database_still_stops_the_ingest(self):
        self.write("-a", "s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1", req="r1")])

        def scan(*a):
            raise sqlite3.OperationalError("database is locked")
            yield

        with mock.patch.object(collect, "scan", scan), self.assertRaises(sqlite3.OperationalError):
            self.ingest()


if __name__ == "__main__":
    unittest.main()
