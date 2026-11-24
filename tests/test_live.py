"""`sessioncoach live`: the running sessions as JSON, on synthetic data only.

The fixture is a temp HOME with a Claude config dir (`sessions/` and `projects/`) and a temp data
dir (projects.json, alerts.json, metrics.db). Several distinct pids are marked alive by patching
`live._pid_alive`; the real probe is tested on its own (os.getpid is alive, a large pid is not,
and Windows treats every pid as alive).
"""
import datetime as dt
import io
import json
import os
import pathlib
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import isolate

from sessioncoach import alertlog, cli, live, store

NOW = 1_800_000_000
T_A = NOW - 60
T_B = NOW - 300
T_D1 = NOW - 120
DEAD_PID = 2 ** 30
ALIVE = {1001, 1002, 1003, 1004, 1005, 1006}
T_G = NOW - 400


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def isolated_home(test):
    """A temp HOME with the legacy env paths cleared, for tests that build their own home."""
    home = isolate.claude_home(test)
    for k in ("KIT_PROJECTS_DIRS", "KIT_DATA_DIR", "CLAUDE_CONFIG_DIR"):
        os.environ.pop(k, None)
    return home


class Live(unittest.TestCase):
    def setUp(self):
        self.home = isolated_home(self)
        self.claude = os.path.join(self.home, ".claude")
        self.sessions_dir = os.path.join(self.claude, "sessions")
        self.projects = os.path.join(self.claude, "projects")
        os.makedirs(self.sessions_dir)
        os.makedirs(self.projects)
        self.data = os.path.join(self.home, ".sessioncoach")
        os.makedirs(self.data, exist_ok=True)
        self.db = os.path.join(self.data, "metrics.db")
        with open(os.path.join(self.data, "projects.json"), "w") as fh:
            json.dump({"rules": [{"match": "-Users-x-code-shop", "project": "shop"}]}, fh)
        p = mock.patch.object(live, "_pid_alive", side_effect=lambda pid: pid in ALIVE)
        p.start()
        self.addCleanup(p.stop)
        self._sessions()
        self._transcripts()

    def _session(self, pid, sid, cwd, status="busy"):
        with open(os.path.join(self.sessions_dir, "%d.json" % pid), "w") as fh:
            json.dump({"pid": pid, "sessionId": sid, "cwd": cwd, "status": status, "updatedAt": iso(NOW),
                       "title": "PRIVATE-TITLE"}, fh)

    def _transcript(self, pdir, sid, lines):
        path = os.path.join(self.projects, pdir, sid + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        return path

    def _sessions(self):
        self._session(1001, "sid-a", "/Users/x/code/shop")
        self._session(1002, "sid-b", "/Users/x/code/api")
        self._session(1003, "sid-c", "/Users/x/code/ctx")
        self._session(1004, "sid-d", "/Users/x/code/big")
        self._session(DEAD_PID, "sid-e", "/Users/x/code/dead")
        self._session(1005, "sid-f", "/Users/x/code/fproj")
        self._session(1006, "sid-g", "/Users/x/code/nomodel")
        with open(os.path.join(self.sessions_dir, "999.json"), "w") as fh:
            fh.write("{not json")
        with open(os.path.join(self.sessions_dir, "777.json"), "w") as fh:
            fh.write('{"pid": "not-an-int", "sessionId": "sid-x"}')
        with open(os.path.join(self.sessions_dir, "776.json"), "w") as fh:
            fh.write("[]")  # a valid-JSON non-object session file
        with open(os.path.join(self.sessions_dir, "888.key"), "w") as fh:
            fh.write('{"secret": "KEY-SECRET"}')

    def _transcripts(self):
        usage_a = {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 320000,
                   "cache_creation_input_tokens": 14990,
                   "cache_creation": {"ephemeral_1h_input_tokens": 14990, "ephemeral_5m_input_tokens": 0}}
        self._transcript("-Users-x-code-shop", "sid-a",
                         [{"type": "assistant", "timestamp": iso(T_A), "message": {"model": "claude-opus-5-5", "usage": usage_a}}])
        usage_b = {"input_tokens": 0, "output_tokens": 50, "cache_read_input_tokens": 90000,
                   "cache_creation_input_tokens": 10000,
                   "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 10000}}
        self._transcript("-api", "sid-b",
                         [{"type": "assistant", "timestamp": iso(T_B), "message": {"model": "claude-sonnet-5-5", "usage": usage_b}}])
        self._transcript("-ctx", "sid-c",
                         [{"type": "user", "timestamp": iso(NOW - 200), "message": {"role": "user", "content": "hi"}}])
        first = {"type": "assistant", "timestamp": iso(NOW - 7200), "message": {"model": "claude-haiku-5-5", "usage": usage_a}}
        filler = {"type": "user", "timestamp": iso(NOW - 3600), "message": {"role": "user", "content": "x" * 300000}}
        last = {"type": "assistant", "timestamp": iso(T_D1),
                "message": {"model": "claude-haiku-5-5",
                            "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 1800,
                                      "cache_creation_input_tokens": 100,
                                      "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 100}}}}
        self._transcript("-big", "sid-d", [first, filler, last])
        self._transcript("-dead", "sid-e",
                         [{"type": "assistant", "timestamp": iso(T_B), "message": {"model": "claude-opus-5-5", "usage": usage_a}}])
        # no model: rewrite_usd stays null
        self._transcript("-nomodel", "sid-g",
                         [{"type": "assistant", "timestamp": iso(T_G),
                           "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 100,
                                                 "cache_creation_input_tokens": 5,
                                                 "cache_creation": {"ephemeral_1h_input_tokens": 5,
                                                                    "ephemeral_5m_input_tokens": 0}}}}])

    def view(self, **kw):
        return live.view(now=NOW, **kw)

    def build_db(self, now=NOW, events=()):
        con = store.connect(self.db)
        try:
            con.execute("INSERT INTO cost_slices (day, project_dir, session_id, model, usd, estimated) "
                        "VALUES (?,?,?,?,?,?)", (dt.date.fromtimestamp(now).isoformat(), "-Users-x-code-shop",
                                                 "sid-a", "opus-5-5", 12.40, 0))
            con.execute("INSERT INTO limits_samples (ts, session_id, root, five_pct, five_reset, seven_pct, "
                        "seven_reset, ctx_pct, model) VALUES (?,?,?,?,?,?,?,?,?)",
                        (now - 60, "sid-a", None, 22.0, now + 3600, 47.0, now + 86400 * 2, None, "claude-opus-5-5"))
            for e in events:
                store.add_alert_event(con, e["source"], e.get("data") or {}, session_id=e.get("session_id"),
                                      project_dir=e.get("project_dir"), ts=e.get("ts"))
            con.commit()
        finally:
            con.close()

    def sessions(self):
        return {s["session_id"]: s for s in self.view()["sessions"]}

    def test_fields_ordering_and_matching(self):
        self.build_db()
        v = self.view()
        self.assertEqual(v["version"], 1)
        self.assertEqual(v["now"], NOW)
        self.assertEqual([s["session_id"] for s in v["sessions"]],
                         ["sid-a", "sid-d", "sid-b", "sid-g", "sid-c", "sid-f"])
        a = self.sessions()["sid-a"]
        self.assertEqual(a["pid"], 1001)
        self.assertEqual(a["cwd"], "/Users/x/code/shop")
        self.assertEqual(a["project"], "shop")  # from projects.json, not the cwd basename
        self.assertEqual(a["status"], "busy")
        self.assertEqual(a["model"], "claude-opus-5-5")
        self.assertEqual(a["context_tokens"], 335000)
        self.assertEqual(a["last_activity"], T_A)
        self.assertEqual(a["cache_ttl_s"], 3600)
        self.assertEqual(a["cache_expires_at"], T_A + 3600)
        self.assertAlmostEqual(a["rewrite_usd"], 2.68, places=2)
        self.assertEqual(a["today_usd"], 12.40)
        self.assertEqual(a["source"], "transcript")
        self.assertEqual(a["match"], "pid")
        b = self.sessions()["sid-b"]
        self.assertEqual(b["model"], "claude-sonnet-5-5")
        self.assertEqual(b["cache_ttl_s"], 300)
        self.assertAlmostEqual(b["rewrite_usd"], 0.40, places=2)
        self.assertIsNone(b["today_usd"])  # no cost_slices for sid-b
        d = self.sessions()["sid-d"]
        self.assertEqual(d["model"], "claude-haiku-5-5")  # the tail's last usage line, not the first
        self.assertEqual(d["context_tokens"], 2000)
        self.assertEqual(d["last_activity"], T_D1)
        self.assertEqual(d["cache_ttl_s"], 300)
        g = self.sessions()["sid-g"]
        self.assertEqual(g["source"], "transcript")
        self.assertIsNone(g["model"])
        self.assertIsNone(g["rewrite_usd"])  # no model -> no cache-write price

    def test_tail_without_usage_and_missing_transcript(self):
        self.build_db()
        c = self.sessions()["sid-c"]  # transcript found, but its tail has no usage line
        self.assertEqual(c["project"], "ctx")
        self.assertIsNone(c["source"])  # no metrics row supplied the numbers, so source stays null
        for k in ("model", "context_tokens", "last_activity", "cache_ttl_s", "cache_expires_at", "rewrite_usd"):
            self.assertIsNone(c[k], k)
        f = self.sessions()["sid-f"]  # no transcript at all
        self.assertEqual(f["project"], "fproj")  # the cwd basename
        self.assertIsNone(f["source"])
        self.assertIsNone(f["context_tokens"])

    def test_tail_without_usage_falls_back_to_metrics(self):
        con = store.connect(self.db)
        try:
            store.add_request(con, {"project_dir": "-ctx", "file": "sid-c.jsonl", "msg_id": "m1", "request_id": "r1",
                                    "session_id": "sid-c", "kind": "main", "ts": iso(T_B), "model": "claude-opus-5-5",
                                    "input": 10, "output": 100, "cache_read": 30000, "cache_write": 5000,
                                    "cache_write_5m": 0, "cache_write_1h": 5000})
            con.commit()
        finally:
            con.close()
        c = self.sessions()["sid-c"]
        self.assertEqual(c["source"], "metrics")
        self.assertEqual(c["model"], "claude-opus-5-5")
        self.assertEqual(c["context_tokens"], 10 + 30000 + 5000)
        self.assertEqual(c["last_activity"], T_B)
        self.assertEqual(c["cache_ttl_s"], 3600)
        self.assertEqual(c["cache_expires_at"], T_B + 3600)

    def test_dead_and_malformed_and_key_never_listed(self):
        self.build_db()
        v = self.view()
        ids = [s["session_id"] for s in v["sessions"]]
        self.assertNotIn("sid-e", ids)
        out = json.dumps(v)
        self.assertNotIn("PRIVATE-TITLE", out)  # the session file's title was never read
        self.assertNotIn("KEY-SECRET", out)  # the .key file was never opened
        self.assertNotIn("not json", out)

    def test_invalid_session_id_never_globs(self):
        for i, bad in enumerate(("sid*", "../x", "[a]")):
            self._session(2000 + i, bad, "/Users/x/code/p%d" % i)
        with mock.patch.object(live, "_pid_alive", return_value=True):
            v = self.view()
        by_pid = {s["pid"]: s for s in v["sessions"]}
        for i in range(3):
            s = by_pid[2000 + i]
            self.assertIsNone(s["source"])
            self.assertIsNone(s["context_tokens"])

    def test_find_transcript_validates_the_id(self):
        self.assertIsNone(live._find_transcript("sid*", [self.projects]))
        self.assertIsNone(live._find_transcript("../x", [self.projects]))
        self.assertIsNone(live._find_transcript("[a]", [self.projects]))
        self.assertIsNone(live._find_transcript(123, [self.projects]))
        self.assertEqual(live._find_transcript("sid-a", [self.projects]),
                         os.path.join(self.projects, "-Users-x-code-shop", "sid-a.jsonl"))

    def test_proc_start_match_mismatch_and_failing(self):
        start = "Tue Nov 24 11:10:51 2026"
        with open(os.path.join(self.sessions_dir, "1002.json"), "w") as fh:
            json.dump({"pid": 1002, "sessionId": "sid-b", "cwd": "/Users/x/code/api", "status": "busy",
                       "procStart": start}, fh)
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="  %s  \n" % start, returncode=0)):
            self.assertIn("sid-b", self.sessions())  # match (whitespace-normalised)
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="Tue Nov 24 13:00:00 2026\n", returncode=0)):
            self.assertNotIn("sid-b", self.sessions())  # reused pid: mismatch
        with mock.patch("subprocess.run", side_effect=OSError(2, "no ps")):
            self.assertIn("sid-b", self.sessions())  # ps missing/failing: never hide

    def test_corrupt_event_does_not_break_the_view(self):
        con = store.connect(self.db)
        try:
            con.execute("INSERT INTO alert_events (ts, source, session_id, project_dir, data) VALUES (?,?,?,?,?)",
                        (NOW - 50, "context_tokens", "sid-a", None, "{bad json"))
            con.execute("INSERT INTO alert_events (ts, source, session_id, project_dir, data) VALUES (?,?,?,?,?)",
                        (NOW - 40, "context_tokens", "sid-a", None, "[1,2,3]"))
            con.commit()
        finally:
            con.close()
        evs = self.view()["events"]
        self.assertEqual(len(evs), 2)
        for e in evs:
            self.assertEqual((e["title"], e["body"], e["level"]), ("context_tokens", "", "info"))

    def test_cli_human_table_shows_expired(self):
        past = int(time.time()) - 7200
        self._transcript("-Users-x-code-shop", "sid-a",
                         [{"type": "assistant", "timestamp": iso(past),
                           "message": {"model": "claude-opus-5-5",
                                       "usage": {"input_tokens": 10, "cache_read_input_tokens": 320000,
                                                 "cache_creation_input_tokens": 14990,
                                                 "cache_creation": {"ephemeral_1h_input_tokens": 14990,
                                                                    "ephemeral_5m_input_tokens": 0}}}}])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["live"]), 0)
        shop = next(line for line in out.getvalue().splitlines() if "shop" in line)
        self.assertIn("expired", shop)  # past expiry: "expired", not "0 min"
        self.assertNotIn("min", shop)

    def test_locked_db_degrades_to_transcript_only(self):
        # a rollback-journal (non-WAL) metrics.db so an exclusive lock really blocks readers
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE requests (ts TEXT)")
        c.commit()
        c.execute("BEGIN EXCLUSIVE")
        self.addCleanup(c.close)
        start = time.time()
        v = self.view()
        self.assertLess(time.time() - start, 4.0)  # bounded by the 1 s timeout, not the old ~10 s
        self.assertIsNone(v["limits"])
        self.assertEqual(v["events"], [])
        self.assertEqual(v["cursor"], None)
        self.assertTrue(all(s["today_usd"] is None for s in v["sessions"]))
        self.assertTrue(any(s["model"] for s in v["sessions"]))  # transcript-only still works

    def test_live_passes_a_short_db_timeout(self):
        self.build_db()
        with mock.patch("sessioncoach.store.open_readonly", wraps=store.open_readonly) as oro:
            self.view()
        self.assertEqual(oro.call_args.kwargs["timeout"], 1)

    def test_unopenable_db_degrades(self):
        self.build_db()
        with mock.patch("sessioncoach.store.open_readonly", side_effect=sqlite3.OperationalError("locked")):
            v = self.view()
        self.assertIsNone(v["limits"])
        self.assertEqual(v["events"], [])
        self.assertEqual(v["cursor"], None)
        self.assertTrue(all(s["today_usd"] is None for s in v["sessions"]))
        self.assertTrue(any(s["model"] for s in v["sessions"]))

    def test_invalid_pids_are_skipped(self):
        for i, val in enumerate((0, -1, True, 10 ** 100, "123")):
            with open(os.path.join(self.sessions_dir, "400%d.json" % i), "w") as fh:
                json.dump({"pid": val, "sessionId": "sid-bad%d" % i, "cwd": "/tmp"}, fh)
        with mock.patch.object(live, "_pid_alive", return_value=True):
            v = self.view()
        sids = {s["session_id"] for s in v["sessions"]}
        for i in range(5):
            self.assertNotIn("sid-bad%d" % i, sids)

    def test_cache_expiry_event_gets_the_live_projects_name(self):
        self.build_db(events=[
            {"source": "cache_expiry", "data": {"context": 400000, "remaining_s": 600, "rewrite_usd": 3.2},
             "session_id": "sid-a", "ts": NOW - 100},
        ])
        e = self.view()["events"][0]
        self.assertTrue(e["body"].endswith(" — shop"), e["body"])

    def test_key_file_is_never_opened(self):
        opened = []
        real_open = open
        real_path_open = pathlib.Path.open

        def guard(file, *a, **kw):
            if str(file).endswith(".key"):
                opened.append(str(file))
            return real_open(file, *a, **kw)

        def path_guard(self, *a, **kw):
            if str(self).endswith(".key"):
                opened.append(str(self))
            return real_path_open(self, *a, **kw)

        with mock.patch("builtins.open", guard), mock.patch("io.open", guard), \
                mock.patch("pathlib.Path.open", path_guard):
            self.build_db()
            self.view()
        self.assertEqual(opened, [])

    def test_cli_prints_question_mark_for_null_project(self):
        with open(os.path.join(self.sessions_dir, "1005.json"), "w") as fh:
            json.dump({"pid": 1005, "sessionId": "sid-f", "status": "busy"}, fh)  # no cwd, no transcript
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["live"]), 0)
        self.assertIn("?", out.getvalue())

    def test_missing_db(self):
        v = self.view()
        self.assertIsNone(v["limits"])
        self.assertEqual(v["events"], [])
        self.assertEqual(v["cursor"], None)
        self.assertTrue(all(s["today_usd"] is None for s in v["sessions"]))

    def test_limits_fresh_and_stale(self):
        self.build_db()
        lim = self.view()["limits"]
        self.assertEqual(lim["five_hour"], {"pct": 22.0, "resets_at": NOW + 3600})
        self.assertEqual(lim["seven_day"], {"pct": 47.0, "resets_at": NOW + 86400 * 2})
        # a stale sample is dropped
        con = store.connect(self.db)
        con.execute("UPDATE limits_samples SET ts = ?", (NOW - 7 * 3600,))
        con.commit()
        con.close()
        self.assertIsNone(self.view()["limits"])

    def test_events_window_since_and_cursor(self):
        self.build_db(events=[
            {"source": "context_tokens", "data": {"context": 999000}, "session_id": "sid-a", "ts": NOW - 86400 * 2},
            {"source": "context_tokens", "data": {"context": 500000}, "session_id": "sid-a", "ts": NOW - 3600},
            {"source": "cache_expiry", "data": {"context": 400000, "remaining_s": 600, "rewrite_usd": 3.2},
             "session_id": "sid-b", "ts": NOW - 100},
        ])
        v = self.view()
        events = v["events"]
        self.assertEqual([e["source"] for e in events], ["context_tokens", "cache_expiry"])  # 24 h window drops the old one
        self.assertEqual([e["id"] for e in events], [2, 3])
        self.assertEqual(events[0]["session_id"], "sid-a")
        self.assertEqual(v["cursor"], 3)
        # poll with the cursor
        v2 = live.view(now=NOW, since=3, con=store.open_readonly(self.db))
        self.assertEqual(v2["events"], [])
        self.assertEqual(v2["cursor"], 3)

    def test_events_cap_at_50_and_cursor(self):
        events = [{"source": "context_tokens", "data": {"context": 300000}, "ts": NOW - 3600} for _ in range(60)]
        self.build_db(events=events)
        v = self.view()
        self.assertEqual(len(v["events"]), 50)
        self.assertEqual(v["cursor"], 50)
        v2 = live.view(now=NOW, since=50, con=store.open_readonly(self.db))
        self.assertEqual(len(v2["events"]), 10)
        self.assertEqual(v2["cursor"], 60)

    def test_events_language_and_unknown_source(self):
        with open(os.path.join(self.data, "alerts.json"), "w") as fh:
            json.dump({"lang": "es"}, fh)
        self.build_db(events=[
            {"source": "context_tokens", "data": {"context": 500000}, "ts": NOW - 100},
            {"source": "mystery", "data": {"x": 1}, "ts": NOW - 90},
        ])
        e = {x["source"]: x for x in self.view()["events"]}
        self.assertIn("tokens de contexto", e["context_tokens"]["title"])
        self.assertEqual(e["mystery"]["title"], "mystery")
        self.assertEqual(e["mystery"]["body"], "")

    def test_levels(self):
        self.assertEqual(live._level("five_hour", {"pct": 95}), "warning")
        self.assertEqual(live._level("five_hour", {"pct": 50}), "info")
        self.assertEqual(live._level("seven_day_pace", {}), "warning")
        self.assertEqual(live._level("cache_expiry", {}), "warning")
        self.assertEqual(live._level("context", {}), "warning")
        self.assertEqual(live._level("context_tokens", {}), "warning")
        self.assertEqual(live._level("budget", {}), "info")

    def test_cli_json_and_human(self):
        now = int(time.time())
        con = store.connect(self.db)
        try:
            con.execute("INSERT INTO limits_samples (ts, session_id, root, five_pct, five_reset, seven_pct, "
                        "seven_reset, ctx_pct, model) VALUES (?,?,?,?,?,?,?,?,?)",
                        (now - 60, "sid-a", None, 22.0, now + 3600, 47.0, now + 86400 * 2, None, "claude-opus-5-5"))
            con.commit()
        finally:
            con.close()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["live", "--json"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["version"], 1)
        self.assertIn("sid-a", [s["session_id"] for s in data["sessions"]])
        self.assertEqual(data["limits"]["five_hour"]["pct"], 22.0)
        out2 = io.StringIO()
        with redirect_stdout(out2):
            self.assertEqual(cli.main(["live"]), 0)
        self.assertIn("shop", out2.getvalue())
        self.assertIn("limits:", out2.getvalue())


class Accounts(unittest.TestCase):
    def setUp(self):
        self.home = isolated_home(self)
        self.a = os.path.join(self.home, "acct-a")
        self.b = os.path.join(self.home, "acct-b")
        for acct in (self.a, self.b):
            os.makedirs(os.path.join(acct, "sessions"))
            os.makedirs(os.path.join(acct, "projects"))
        os.environ["SESSIONCOACH_PROJECTS_DIRS"] = os.pathsep.join(
            [os.path.join(self.a, "projects"), os.path.join(self.b, "projects")])
        p = mock.patch.object(live, "_pid_alive", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def _session(self, root, pid, sid, cwd):
        with open(os.path.join(root, "sessions", "%d.json" % pid), "w") as fh:
            json.dump({"pid": pid, "sessionId": sid, "cwd": cwd, "status": "busy"}, fh)

    def _transcript(self, root, pdir, sid, model, ctx):
        path = os.path.join(root, "projects", pdir, sid + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": iso(NOW - 60),
                                 "message": {"model": model,
                                             "usage": {"input_tokens": ctx, "cache_read_input_tokens": 0,
                                                       "cache_creation_input_tokens": 0}}}) + "\n")

    def test_same_session_id_gets_its_own_account_numbers(self):
        self._session(self.a, 1001, "shared", "/Users/x/code/shop")
        self._session(self.b, 1002, "shared", "/Users/x/code/api")
        self._transcript(self.a, "-Users-x-code-shop", "shared", "claude-opus-5-5", 111)
        self._transcript(self.b, "-Users-x-code-api", "shared", "claude-sonnet-5-5", 222)
        by_pid = {s["pid"]: s for s in live.view(now=NOW)["sessions"]}
        self.assertEqual((by_pid[1001]["model"], by_pid[1001]["context_tokens"]), ("claude-opus-5-5", 111))
        self.assertEqual((by_pid[1002]["model"], by_pid[1002]["context_tokens"]), ("claude-sonnet-5-5", 222))


class Helpers(unittest.TestCase):
    def test_num(self):
        self.assertEqual(live._num(12), 12)
        self.assertEqual(live._num(12.9), 12)
        self.assertEqual(live._num(None), 0)
        self.assertEqual(live._num("34"), 0)  # a string is not a number
        self.assertEqual(live._num(True), 0)  # a bool is not a number
        self.assertEqual(live._num(-5), 0)
        self.assertEqual(live._num(float("nan")), 0)

    def test_int_or_none(self):
        self.assertEqual(live._int_or_none(5), 5)
        self.assertEqual(live._int_or_none(0), 0)
        self.assertIsNone(live._int_or_none(None))
        self.assertIsNone(live._int_or_none(True))
        self.assertIsNone(live._int_or_none("abc"))
        self.assertIsNone(live._int_or_none(-1))

    def test_ttl(self):
        self.assertEqual(live._ttl({"cache_creation": {"ephemeral_1h_input_tokens": 1, "ephemeral_5m_input_tokens": 0}}), 3600)
        self.assertEqual(live._ttl({"cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 1}}), 300)
        self.assertEqual(live._ttl({"cache_creation_input_tokens": 7}), 3600)  # older format, no breakdown
        self.assertIsNone(live._ttl({}))
        self.assertIsNone(live._ttl({"cache_read_input_tokens": 9}))

    def test_ttl_null_breakdown_with_positive_write(self):
        # both breakdown columns null but a positive total cache write: matches pricing.cache_ttl_s (1 h)
        u = {"cache_creation_input_tokens": 5000,
             "cache_creation": {"ephemeral_1h_input_tokens": None, "ephemeral_5m_input_tokens": None}}
        self.assertEqual(live._ttl(u), 3600)

    def test_pid_alive_eperm_is_alive(self):
        with mock.patch("os.kill", side_effect=OSError(1, "EPERM")):
            self.assertTrue(live._pid_alive(123))

    def test_pid_alive_rejects_overflow_and_value_errors(self):
        with mock.patch("os.kill", side_effect=OverflowError):
            self.assertTrue(live._pid_alive(123))
        with mock.patch("os.kill", side_effect=ValueError):
            self.assertTrue(live._pid_alive(123))

    def test_tail_info_missing_file(self):
        self.assertIsNone(live._tail_info("/nonexistent/nope.jsonl"))

    def test_tail_info_edge_lines(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "s.jsonl")
        # oldest -> newest: the edge lines are scanned first (reversed), then the usage lines.
        lines = [
            json.dumps({"type": "assistant", "timestamp": iso(NOW - 20), "message": {"model": "claude-opus-5-5",
                        "usage": {"input_tokens": 5, "output_tokens": 6, "cache_read_input_tokens": 7,
                                  "cache_creation_input_tokens": 100,
                                  "cache_creation": {"ephemeral_1h_input_tokens": 100, "ephemeral_5m_input_tokens": 0}}}}),
            json.dumps({"type": "assistant", "timestamp": iso(NOW - 10), "message": {"model": "claude-opus-5-5",
                        "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30,
                                  "cache_creation_input_tokens": 0}}}),
            json.dumps({"type": "assistant", "timestamp": 12345,
                        "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 1}}}),
            json.dumps({"type": "assistant", "timestamp": iso(NOW - 5),
                        "message": {"model": "<synthetic>", "usage": {"input_tokens": 1}}}),
            '{"type":"assistant","timestamp":"bad","message":{"usage":',  # malformed, has "usage"
        ]
        with open(path, "w") as fh:
            for line in lines:
                fh.write(line + "\n")
        info = live._tail_info(path)
        self.assertEqual(info["model"], "claude-opus-5-5")
        self.assertEqual(info["context"], 40)  # the last usage line: input + cache read, no output
        self.assertEqual(info["ttl"], 3600)  # the earlier line that wrote cache

    def test_tail_info_record_validation(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "s.jsonl")
        good = {"type": "assistant", "timestamp": iso(NOW - 20),
                "message": {"model": "claude-opus-5-5",
                            "usage": {"input_tokens": 10, "cache_read_input_tokens": 20,
                                      "cache_creation_input_tokens": 30,
                                      "cache_creation": {"ephemeral_1h_input_tokens": 30,
                                                         "ephemeral_5m_input_tokens": 0}}}}
        with open(path, "w") as fh:
            for line in (json.dumps(good),
                         json.dumps(["usage"]),  # a list: not a record
                         json.dumps({"type": "user", "timestamp": iso(NOW - 10),
                                     "message": {"role": "user", "usage": {"input_tokens": 999999}}})):
                fh.write(line + "\n")
        info = live._tail_info(path)
        self.assertEqual(info["model"], "claude-opus-5-5")
        self.assertEqual(info["context"], 10 + 20 + 30)

        # string/bool token counts and a huge model name are sanitised, not fatal
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": iso(NOW - 20),
                                 "message": {"model": "x" * 200,
                                             "usage": {"input_tokens": "500", "cache_read_input_tokens": True,
                                                       "cache_creation_input_tokens": 40,
                                                       "cache_creation": {"ephemeral_1h_input_tokens": 40,
                                                                          "ephemeral_5m_input_tokens": 0}}}}) + "\n")
        info = live._tail_info(path)
        self.assertIsNone(info["model"])  # > 100 chars -> None
        self.assertEqual(info["context"], 40)  # "500" and True count as 0
        self.assertEqual(info["ttl"], 3600)

    def test_today_usd_no_session(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        con = store.connect(os.path.join(d, "metrics.db"))
        try:
            self.assertIsNone(live._today_usd(con, "2026-01-01", None))
        finally:
            con.close()

    def test_view_with_an_older_database(self):
        isolated_home(self)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        c = sqlite3.connect(os.path.join(d, "old.db"))
        c.executescript("CREATE TABLE requests (ts TEXT);")
        c.close()
        con = store.open_readonly(os.path.join(d, "old.db"))
        try:
            v = live.view(now=NOW, con=con)
        finally:
            con.close()
        self.assertIsNone(v["limits"])
        self.assertEqual(v["events"], [])
        self.assertTrue(all(s["today_usd"] is None for s in v["sessions"]))


class ProcStart(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(live._norm("  Tue   Nov 24  11:10:51 2026  \n"), "Tue Nov 24 11:10:51 2026")

    def test_match(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="Tue Nov 24 11:10:51 2026\n", returncode=0)) as ps:
            self.assertTrue(live._proc_matches(123, "Tue Nov 24 11:10:51 2026"))
        self.assertEqual(ps.call_args[0][0], ["ps", "-o", "lstart=", "-p", "123"])
        self.assertEqual(ps.call_args[1]["env"]["TZ"], "UTC")  # procStart is UTC

    def test_mismatch(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="Tue Nov 24 12:00:00 2026\n", returncode=0)):
            self.assertFalse(live._proc_matches(123, "Tue Nov 24 11:10:51 2026"))

    def test_ps_failing(self):
        with mock.patch("subprocess.run", side_effect=OSError(2, "no ps")):
            self.assertTrue(live._proc_matches(123, "Tue Nov 24 11:10:51 2026"))

    def test_ps_nonzero_is_kept(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="", returncode=1)):
            self.assertTrue(live._proc_matches(123, "Tue Nov 24 11:10:51 2026"))

    def test_windows_skips_check(self):
        with mock.patch("os.name", "nt"):
            self.assertTrue(live._proc_matches(123, "anything"))


class PidAlive(unittest.TestCase):
    def test_posix(self):
        self.assertTrue(live._pid_alive(os.getpid()))
        self.assertFalse(live._pid_alive(DEAD_PID))

    def test_windows_treats_every_pid_as_alive(self):
        with mock.patch("os.name", "nt"):
            self.assertTrue(live._pid_alive(DEAD_PID))


class PidValidation(unittest.TestCase):
    def test_valid_pid(self):
        self.assertTrue(live._valid_pid(1))
        self.assertTrue(live._valid_pid(os.getpid()))
        self.assertTrue(live._valid_pid(2 ** 31 - 1))
        self.assertFalse(live._valid_pid(0))
        self.assertFalse(live._valid_pid(-1))
        self.assertFalse(live._valid_pid(True))
        self.assertFalse(live._valid_pid(10 ** 100))
        self.assertFalse(live._valid_pid("123"))
        self.assertFalse(live._valid_pid(2 ** 31))


class Describe(unittest.TestCase):
    def test_each_source(self):
        self.assertEqual(alertlog.describe("five_hour", {"pct": 95, "reset": 0}, "en")[0], "Claude · 5 h limit at 95%")
        self.assertTrue(alertlog.describe("five_hour", {"pct": 95, "reset": 0}, "en")[1].startswith("Resets at "))
        self.assertEqual(alertlog.describe("seven_day_pace", {"pct": 50, "eta": 100, "reset": 200}, "en")[0],
                         "Claude · at this pace you'll hit the 7 d limit")
        self.assertEqual(alertlog.describe("context", {"pct": 85}, "en")[0], "Claude · context at 85%")
        self.assertEqual(alertlog.describe("context_tokens", {"context": 500000}, "en"),
                         ("Claude · 500k tokens of context",
                          "Every turn re-reads that whole context. /compact or one session per task makes the next turns much cheaper."))
        self.assertEqual(alertlog.describe("budget", {"name": "shop", "used_usd": 80, "limit_usd": 100}, "en"),
                         ("Budget “shop” at 80%", "$80 of $100 (API-equivalent)."))
        self.assertEqual(alertlog.describe("cache_expiry", {"context": 400000, "remaining_s": 600, "rewrite_usd": 3.2}, "en"),
                         ("Claude · cache expires in ~10 min",
                          "~400k tokens: coming back later re-writes them for ~$3.20 (API-equivalent)."))
        self.assertEqual(alertlog.describe("unknown", {}, "en"), ("unknown", ""))

    def test_budget_period(self):
        self.assertEqual(alertlog.describe("budget", {"name": "shop", "used_usd": 80, "limit_usd": 100, "period": "day"}, "en"),
                         ("Budget “shop” at 80%", "$80 of $100 (API-equivalent) in the day."))
        self.assertEqual(alertlog.describe("budget", {"name": "shop", "used_usd": 80, "limit_usd": 100, "period": "month"}, "en"),
                         ("Budget “shop” at 80%", "$80 of $100 (API-equivalent) in the month."))
        self.assertEqual(alertlog.describe("budget", {"name": "shop", "used_usd": 80, "limit_usd": 100, "period": "day"}, "es"),
                         ("Presupuesto «shop» al 80%", "$80 de $100 (equivalente API) en el día."))
        self.assertEqual(alertlog.describe("budget", {"name": "shop", "used_usd": 80, "limit_usd": 100, "period": "month"}, "es"),
                         ("Presupuesto «shop» al 80%", "$80 de $100 (equivalente API) en el mes."))
        # an old event without `period` keeps the period-less body
        self.assertEqual(alertlog.describe("budget", {"name": "shop", "used_usd": 80, "limit_usd": 100}, "es"),
                         ("Presupuesto «shop» al 80%", "$80 de $100 (equivalente API)."))

    def test_spanish(self):
        self.assertEqual(alertlog.describe("context_tokens", {"context": 500000}, "es")[0],
                         "Claude · 500k tokens de contexto")
        self.assertEqual(alertlog.describe("five_hour", {"pct": 95, "reset": 0}, "es")[0], "Claude · límite 5 h al 95%")


if __name__ == "__main__":
    unittest.main()
