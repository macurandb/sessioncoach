"""sessioncoach codemode: sessions with Code Mode (REPL calls) against the rest. Synthetic database; nothing real."""
import contextlib
import datetime as dt
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401  (drops SESSIONCOACH_* left in the shell)

from sessioncoach import analysis, cli, paths, store

TODAY = dt.date(2026, 11, 24)
OPUS = "claude-opus-5-5"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ, {"KIT_DATA_DIR": self.tmp})
        patch.start()
        self.addCleanup(patch.stop)
        self.con = store.connect(paths.db_path())
        self.addCleanup(self.con.close)
        self.n = 0

    def req(self, sid, ts, ctx=40000, output=100, msg=None, file=None, model=OPUS, project="-p-app"):
        self.n += 1
        self.con.execute("INSERT INTO requests (project_dir, file, msg_id, request_id, session_id, kind, ts, model, input, "
                         "output, cache_read, cache_write, cache_write_5m, cache_write_1h) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (project, file or sid + ".jsonl", msg or "msg_%d" % self.n, "req_%d" % self.n, sid, "main", ts, model,
                          0, output, ctx, 0, 0, 0))

    def tool(self, sid, ts, name, msg="", project="-p-app"):
        self.n += 1
        self.con.execute("INSERT INTO tool_calls (project_dir, tool_use_id, session_id, kind, ts, name, root, msg_id) "
                         "VALUES (?,?,?,?,?,?,?,?)", (project, "tu_%d" % self.n, sid, "main", ts, name, None, msg))

    def code_mode_session(self, sid="cm", day="2026-11-24"):
        self.req(sid, day + "T15:00:00Z", msg="msg_cm1")
        self.tool(sid, day + "T15:00:01Z", "REPL", "msg_cm1")
        for _ in range(3):
            self.tool(sid, day + "T15:00:02Z", "Read", "a-uuid")  # inner calls: no message id of the request
        self.tool(sid, day + "T15:00:02Z", "Poll")
        self.req(sid, day + "T15:01:00Z", ctx=250000, msg="msg_cm2")
        self.tool(sid, day + "T15:01:01Z", "Write", "b-uuid")

    def group(self, v, name):
        return next(g for g in v["groups"] if g["group"] == name)

    def bucket(self, v, label, name):
        b = next(b for b in v["buckets"] if b["bucket"] == label)
        return next(g for g in b["groups"] if g["group"] == name)


class TestView(Base):
    def test_code_mode_counts_inner_tools_not_repl_or_poll(self):
        self.code_mode_session()
        v = analysis.code_mode_view(self.con, today=TODAY)
        cm = self.group(v, "code_mode")
        self.assertEqual((cm["sessions"], cm["requests"], cm["tools"], cm["repl"]), (1, 2, 4, 1))
        self.assertEqual(cm["tools_per_request"], 2.0)
        self.assertEqual(cm["cache_read_per_tool"], (40000 + 250000) / 4)
        self.assertEqual(v["since"], "2026-11-24")
        self.assertEqual(v["before"], {"start": "2026-11-10", "end": "2026-11-23"})

    def test_tools_go_to_the_context_size_of_their_request(self):
        self.code_mode_session()
        v = analysis.code_mode_view(self.con, today=TODAY)
        self.assertEqual(self.bucket(v, "<100k", "code_mode")["tools"], 3)
        self.assertEqual(self.bucket(v, "200k-300k", "code_mode")["tools"], 1)
        self.assertEqual(self.bucket(v, ">=500k", "code_mode")["requests"], 0)

    def test_groups_without_and_before(self):
        self.code_mode_session()
        self.req("plain", "2026-11-24T16:00:00Z")
        self.tool("plain", "2026-11-24T16:00:01Z", "Read", "x")
        self.req("old", "2026-11-20T15:00:00Z", msg="msg_old")
        self.req("old", "2026-11-20T15:00:00Z", msg="msg_old", file="resumed.jsonl")  # copied by a resume: once
        self.tool("old", "2026-11-20T15:00:01Z", "Bash", "msg_old")
        self.req("idle", "2026-11-20T15:00:00Z")  # no tool: left out
        self.req("ancient", "2026-10-01T15:00:00Z")  # outside both windows
        self.req("edge", "2026-11-08T15:00:00Z")  # read for the time-zone slack, then outside the window
        self.tool("edge", "2026-11-08T15:00:01Z", "Read", "w")
        self.tool("ancient", "2026-10-01T15:00:01Z", "Read", "y")
        v = analysis.code_mode_view(self.con, today=TODAY)
        self.assertEqual(self.group(v, "without")["tools"], 1)
        before = self.group(v, "before")
        self.assertEqual((before["sessions"], before["requests"], before["tools"]), (1, 1, 1))
        self.assertIsNotNone(before["median_usd_per_tool"])

    def test_tool_before_the_first_request_and_code_mode_tried_earlier(self):
        self.tool("early", "2026-11-24T09:00:00Z", "Read", "z")
        self.req("early", "2026-11-24T10:00:00Z")
        self.req("trial", "2026-11-20T15:00:00Z")
        self.tool("trial", "2026-11-20T15:00:01Z", "REPL", "m")
        self.tool("trial", "2026-11-20T15:00:02Z", "Read", "u")
        v = analysis.code_mode_view(self.con, since=TODAY, today=TODAY)
        self.assertEqual(self.group(v, "without")["sessions"], 0)
        self.assertEqual(self.group(v, "before")["sessions"], 0)

    def test_no_code_mode_yet_gives_the_baseline_only(self):
        self.req("plain", "2026-11-24T16:00:00Z")
        self.tool("plain", "2026-11-24T16:00:01Z", "Read", "x")
        v = analysis.code_mode_view(self.con, days=7, today=TODAY)
        self.assertIsNone(v["since"])
        self.assertIsNone(v["after"])
        self.assertEqual(v["before"], {"start": "2026-11-18", "end": "2026-11-24"})
        self.assertEqual(self.group(v, "before")["tools"], 1)
        cm = self.group(v, "code_mode")
        self.assertEqual(cm["sessions"], 0)
        self.assertIsNone(cm["usd_per_tool"])
        self.assertIsNone(cm["median_usd_per_tool"])
        self.assertFalse(cm["enough"])

    def test_enough_data_needs_the_minimum_sessions(self):
        for i in range(analysis.CODE_MODE_MIN_SESSIONS):
            self.code_mode_session("cm%d" % i)
        self.assertTrue(self.group(analysis.code_mode_view(self.con, today=TODAY), "code_mode")["enough"])

    def test_project_and_model_filters(self):
        self.code_mode_session()
        self.req("other", "2026-11-24T16:00:00Z", model="claude-haiku-5-5", project="-p-web")
        self.tool("other", "2026-11-24T16:00:01Z", "Read", "x", project="-p-web")
        v = analysis.code_mode_view(self.con, project="web", today=TODAY)
        self.assertEqual(self.group(v, "code_mode")["sessions"], 0)
        self.assertEqual(self.group(v, "without")["sessions"], 1)
        v = analysis.code_mode_view(self.con, model="opus", today=TODAY)
        self.assertEqual(self.group(v, "without")["sessions"], 0)
        self.assertEqual(self.group(v, "code_mode")["sessions"], 1)


class TestCli(Base):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(list(argv))
        return rc, out.getvalue()

    def test_prints_groups_and_buckets(self):
        self.code_mode_session()
        self.con.commit()
        rc, out = self.run_cli("codemode", "--project", "app")
        self.assertFalse(rc)
        self.assertIn("Code Mode since 2026-11-24", out)
        self.assertIn("project ~ app", out)
        self.assertIn("not enough data", out)
        self.assertIn("<100k Code Mode", out)
        self.assertIn("CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=0", out)

    def test_without_code_mode_and_with_since(self):
        self.con.commit()
        rc, out = self.run_cli("codemode", "--since", "2026-11-24", "--days", "7", "--model", "opus")
        self.assertFalse(rc)
        self.assertIn("before: 2026-11-17 → 2026-11-23", out)
        rc, out = self.run_cli("codemode")
        self.assertIn("no Code Mode session yet", out)

    def test_rejects_an_empty_window(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_cli("codemode", "--days", "0")[0], 2)
            self.assertEqual(self.run_cli("codemode", "--since", "2999-01-01")[0], 2)
        self.assertIn("--days must be 1 or more", err.getvalue())

    def test_no_database(self):
        self.con.close()  # Windows cannot remove a file that is still open
        os.remove(paths.db_path())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli("codemode")[0], 1)


if __name__ == "__main__":
    unittest.main()
