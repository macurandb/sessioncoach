import datetime as dt
import http.client
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from unittest import mock

import isolate
from serverauth import session_cookie

from sessioncoach import alertlog, cli, expiry, limits, nudge, server, store
from sessioncoach.projects import Labeler

T0 = dt.datetime(2026, 10, 15, 10, 0, tzinfo=dt.timezone.utc)
PDIR = "-Users-someone-code-shop"
ALERTS = {"lang": "es", "ingest": {"interval_min": 30}}


def ts(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def m(n):
    return T0 + dt.timedelta(minutes=n)


class Log(unittest.TestCase):
    def setUp(self):
        isolate.claude_home(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "metrics.db")
        self.con = store.connect(self.db)
        self.addCleanup(self.con.close)
        self.n = 0

    def turn(self, t, sid="s1", pdir=PDIR, ctx=400000, cw=5000, kind="main"):
        self.n += 1
        store.add_request(self.con, {
            "project_dir": pdir, "file": sid + ".jsonl", "msg_id": "m%d" % self.n, "request_id": "r%d" % self.n,
            "session_id": sid, "kind": kind, "ts": ts(t), "model": "claude-opus-5-5", "input": 10, "output": 100,
            "cache_read": ctx - 10 - cw, "cache_write": cw, "cache_write_1h": cw, "cache_write_5m": 0})

    def compact(self, t, sid="s1", post=13000, trigger="manual", precomputed=0):
        store.add_compaction(self.con, {"project_dir": PDIR, "session_id": sid, "ts": ts(t), "trigger": trigger,
                                        "pre_tokens": 400000, "post_tokens": post, "precomputed": precomputed})

    def ingest_run(self, t):
        self.con.execute("INSERT INTO ingest_runs VALUES (?,?,?,?,?)", (t.isoformat(timespec="seconds"), "transcripts", 0, 0, 0))

    def hook(self, at, last, ctx=400000, sid="s1"):
        store.add_alert_event(self.con, "hook", {"last_ts": ts(last), "idle_s": int((at - last).total_seconds()), "context": ctx,
                                                 "ttl_s": 3600}, session_id=sid, project_dir=PDIR, ts=int(at.timestamp()))

    def warn(self, now):
        return expiry.check(self.con, ALERTS, now=now, notify_fn=lambda t, msg: None, labeler=Labeler())

    def view(self, now, **kw):
        return alertlog.view(self.con, days=7, now=now, labeler=Labeler(), alerts=dict(ALERTS, **kw))

    def outcome(self, now):
        v = self.view(now)
        return v["events"][0]["outcome"], v["events"][0]["verdict"]

    # ---- hook warnings ----
    def test_hook_false_alarm_after_a_compact(self):
        self.turn(T0, ctx=844000)
        self.compact(m(30))
        self.hook(m(200), T0, ctx=844000)
        self.turn(m(200.1), ctx=61000)
        o, v = self.outcome(m(210))
        self.assertEqual((o["code"], o["why"], o["by"], o["real_context"], v), ("false_alarm", "compacted", "you", 61000, "bad"))
        self.assertEqual(o["compacted_at"], ts(m(30)))

    def test_hook_false_alarm_idle_compaction_on_return(self):
        self.turn(T0)
        self.hook(m(90), T0)
        self.compact(m(90.05), trigger="auto", precomputed=1)  # Claude Code applies it after the prompt
        self.turn(m(90.1), ctx=54000)
        o = self.outcome(m(100))[0]
        self.assertEqual((o["why"], o["by"]), ("compacted", "idle"))

    def test_compacted_by_whom(self):
        self.turn(T0)
        self.warn(m(40))
        for trig, pre, by in (("auto", 0, "auto"), ("auto", 1, "idle"), ("manual", 0, "you")):
            self.con.execute("DELETE FROM compactions")
            self.compact(m(50), trigger=trig, precomputed=pre)
            self.assertEqual(self.outcome(m(70))[0]["by"], by)

    def test_new_session_after_a_subagent_is_still_a_new_session(self):
        self.turn(T0)
        self.hook(m(70), T0)
        self.turn(m(70.1))
        self.turn(m(71), kind="subagent", ctx=20000)  # asking for the handoff ran a subagent
        self.turn(m(80), sid="s2", ctx=30000)
        self.assertEqual(self.outcome(m(90)), ({"code": "new_session"}, "good"))

    def test_hook_next_turn_days_later_is_not_its_outcome(self):
        self.turn(T0)
        self.hook(m(90), T0)
        self.turn(m(90 + 3 * 24 * 60))
        self.assertEqual(self.outcome(m(90 + 3 * 24 * 60 + 5))[0]["code"], "no_response")

    def test_compaction_by_claude_code_is_neutral(self):
        self.turn(T0)
        self.warn(m(40))
        self.compact(m(50), trigger="auto", precomputed=1)
        self.assertEqual(self.outcome(m(70))[1], "neutral")

    def test_expiry_event_time_is_the_checks(self):
        self.turn(T0)
        self.warn(m(40))
        self.assertEqual(self.con.execute("SELECT ts FROM alert_events").fetchone()[0], int(m(40).timestamp()))

    def test_anonymous_mode_covers_names_it_never_saw(self):
        self.turn(T0)
        self.hook(m(90), T0)
        store.add_alert_event(self.con, "budget", {"name": "old-secret-budget", "used_usd": 1}, ts=int(m(1).timestamp()))
        self.con.commit()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.db))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        conn.request("GET", "/api/alerts?days=3650&anon=1&lang=en", headers={"Cookie": session_cookie(httpd)})
        body = conn.getresponse().read().decode("utf-8")
        conn.close()
        self.assertNotIn("old-secret-budget", body)
        self.assertNotIn("shop", body)
        self.assertIn("Budget ?1", body)
        self.assertIn("Project ?1", body)

    def test_hook_false_alarm_smaller_context(self):
        self.turn(T0)
        self.hook(m(90), T0)
        self.turn(m(90.1), ctx=50000)
        self.assertEqual(self.outcome(m(100))[0]["why"], "smaller")

    def test_hook_compacted_new_session_continued_pending(self):
        self.turn(T0)
        self.hook(m(90), T0)
        self.assertEqual(self.outcome(m(91)), ({"code": "pending"}, "pending"))
        self.assertEqual(self.outcome(m(200))[0]["code"], "no_response")
        self.turn(m(90.1))
        o, v = self.outcome(m(100))
        self.assertEqual((o["code"], v), ("continued", "costly"))
        self.assertAlmostEqual(o["usd"], 0.04)  # 5k tokens written at $8/M
        self.turn(m(95), sid="s2", ctx=30000)
        self.assertEqual(self.outcome(m(100))[0]["code"], "new_session")
        self.compact(m(92))
        o, v = self.outcome(m(100))
        self.assertEqual((o["code"], o["post_tokens"], v), ("compacted", 13000, "good"))

    # ---- cache-expiry warnings ----
    def test_expiry_outcomes(self):
        self.turn(T0)
        self.warn(m(40))
        self.assertEqual(self.outcome(m(45))[0]["code"], "pending")
        self.assertEqual(self.outcome(m(70))[0]["code"], "not_returned")
        self.turn(m(55))
        self.assertEqual(self.outcome(m(70))[0]["code"], "returned_in_time")
        self.con.execute("UPDATE requests SET ts = ? WHERE ts = ?", (ts(m(75)), ts(m(55))))
        o, v = self.outcome(m(80))
        self.assertEqual((o["code"], v), ("returned_after_expiry", "costly"))
        self.compact(m(50))
        self.assertEqual(self.outcome(m(80))[0]["code"], "compacted")

    def test_expiry_new_session(self):
        self.turn(T0)
        self.warn(m(40))
        self.turn(m(45), sid="s2", ctx=20000)
        self.assertEqual(self.outcome(m(70)), ({"code": "new_session"}, "good"))

    def test_other_sources_are_info_and_numbers_only(self):
        store.add_alert_event(self.con, "budget", {"name": "shop-month", "used_usd": 81.5, "limit_usd": 100, "threshold": 80,
                                                   "note": "free text is dropped"}, ts=int(m(1).timestamp()))
        e = self.view(m(2))["events"][0]
        self.assertEqual((e["verdict"], e["data"]), ("info", {"name": "shop-month", "used_usd": 81.5, "limit_usd": 100, "threshold": 80}))
        self.assertIsNone(e["project"])

    # ---- pauses not warned ----
    def test_missed_needs_the_first_check(self):
        self.turn(T0)
        self.assertEqual((self.view(m(120))["missed"], self.view(m(120))["missed_since"]), ([], None))

    def test_missed_reasons(self):
        self.warn(m(-200))  # first check: missed pauses count from here
        cases = {"s-comp": "compacted", "s-sub": "subagent", "s-repl": "replaced", "s-noing": "no_ingest", "s-unk": "unknown"}
        for i, sid in enumerate(cases):
            pdir = "-Users-someone-code-p%d" % i
            self.turn(T0, sid=sid, pdir=pdir)
            if sid == "s-comp":
                store.add_compaction(self.con, {"project_dir": pdir, "session_id": sid, "ts": ts(m(20))})
            if sid == "s-sub":
                self.turn(m(20), sid=sid, pdir=pdir, kind="subagent")
            if sid == "s-repl":
                self.turn(m(20), sid="other", pdir=pdir, ctx=1000)
            if sid == "s-unk":
                self.turn(m(90), sid=sid, pdir=pdir)
        self.ingest_run(m(40))
        self.con.execute("UPDATE requests SET ts = ? WHERE session_id = 's-noing'", (ts(m(-120)),))
        self.ingest_run(m(-60))
        v = self.view(m(100))
        self.assertEqual({x["session"]: x["reason"] for x in v["missed"]}, {"s-noing": "no_ingest", "s-unk": "unknown"})
        self.assertEqual(v["not_applicable"], {"compacted": 1, "subagent": 1, "replaced": 1})
        self.assertEqual(v["summary"]["missed"], 2)
        unk = next(x for x in self.view(m(100))["missed"] if x["session"] == "s-unk")
        self.assertEqual((unk["returned_at"], unk["usd"]), (ts(m(90)), 0.04))

    def test_missed_skips_warned_returned_in_time_and_off_switches(self):
        self.warn(m(-60))
        self.turn(T0)
        self.warn(m(40))
        self.turn(T0, sid="s2", pdir="-Users-someone-code-b")
        self.turn(m(30), sid="s2", pdir="-Users-someone-code-b")  # came back in time: that pause did not expire
        self.ingest_run(m(80))
        self.assertEqual([x["session"] for x in self.view(m(100))["missed"]], ["s2"])  # only its last pause
        self.assertEqual(self.view(m(200), cache_expiry={"enabled": False})["missed"][0]["reason"], "disabled")
        self.assertEqual(self.view(m(200), notify=False)["missed"][0]["reason"], "notify_off")
        self.assertEqual(self.view(m(200))["summary"], {"neutral": 1, "missed": 1, "costly_usd": 0})

    def test_api_and_cli(self):
        self.turn(T0)
        self.hook(m(90), T0)
        self.con.commit()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.db))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        for path, status in (("/api/alerts?days=3650", 200), ("/api/alerts", 200), ("/api/alerts?days=-1", 400)):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
            conn.request("GET", path, headers={"Cookie": session_cookie(httpd)})
            r = conn.getresponse()
            body = json.loads(r.read().decode("utf-8"))
            conn.close()
            self.assertEqual(r.status, status, path)
            self.assertIn("events" if status == 200 else "error", body)
        with mock.patch.dict(os.environ, {"KIT_DATA_DIR": self.tmp}), redirect_stdout(io.StringIO()) as out:
            cli.main(["alert-log", "--days", "3650"])
        self.assertIn("hook", out.getvalue())
        self.assertIn("no_response", out.getvalue())

    def test_database_from_an_older_version(self):
        old = os.path.join(self.tmp, "old.db")
        c = sqlite3.connect(old)
        c.executescript("CREATE TABLE requests (ts TEXT); CREATE TABLE alerts_sent (key TEXT PRIMARY KEY, ts INTEGER);")
        c.close()
        con = store.open_readonly(old)
        try:
            v = alertlog.view(con, now=m(0))
        finally:
            con.close()
        self.assertEqual((v["events"], v["missed"], v["missed_since"]), ([], [], None))


class Recording(unittest.TestCase):
    def setUp(self):
        isolate.claude_home(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"KIT_DATA_DIR": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        store.connect(os.path.join(self.tmp, "metrics.db")).close()  # the hook never creates the database

    def events(self):
        con = store.connect(os.path.join(self.tmp, "metrics.db"))
        try:
            rows = con.execute("SELECT * FROM alert_events").fetchall()
            return [(r["source"], r["session_id"], r["project_dir"], json.loads(r["data"])) for r in rows]
        finally:
            con.close()

    def test_hook_records_what_it_warned(self):
        pdir = os.path.join(self.tmp, "projects", PDIR)
        os.makedirs(pdir)
        path = os.path.join(pdir, "s1.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": "2026-10-01T10:00:00Z", "message": {
                "id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 0, "cache_read_input_tokens": 250000}}}) + "\n")
        payload = json.dumps({"session_id": "s1", "transcript_path": path, "prompt": "sigue con esto por favor"})
        with mock.patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(io.StringIO()) as out:
            nudge.hook_main()
        self.assertIn("systemMessage", out.getvalue())
        (src, sid, pd, data), = self.events()
        self.assertEqual((src, sid, pd, data["context"], data["last_ts"]), ("hook", "s1", PDIR, 250000, "2026-10-01T10:00:00Z"))
        self.assertNotIn("sigue", json.dumps(data))

    def test_hook_without_database_or_table_keeps_nothing(self):
        info = {"context": 1, "last_ts": "x"}
        os.remove(os.path.join(self.tmp, "metrics.db"))
        nudge._log({"session_id": "s1"}, info)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "metrics.db")))
        sqlite3.connect(os.path.join(self.tmp, "metrics.db")).close()  # an empty file: no alert_events table yet
        with self.assertRaises(sqlite3.OperationalError):
            nudge._log({"session_id": "s1"}, info)  # hook_main swallows it: the warning was already printed

    def test_cli_without_database(self):
        os.remove(os.path.join(self.tmp, "metrics.db"))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["alert-log"]), 1)
        self.assertIn("No database yet", out.getvalue())

    def test_hook_quiet_records_nothing(self):
        with mock.patch.object(sys, "stdin", io.StringIO("{}")), redirect_stdout(io.StringIO()) as out:
            nudge.hook_main()
        self.assertEqual((out.getvalue(), self.events()), ("", []))

    def test_limits_and_budgets_record(self):
        con = store.connect(os.path.join(self.tmp, "metrics.db"))
        with mock.patch.object(limits, "notify"):
            limits.record_and_alert(con, {"session_id": "s1", "context_window": {"used_percentage": 90, "current_usage": {
                "input_tokens": 0, "cache_read_input_tokens": 600000}}, "rate_limits": {"five_hour": {
                    "used_percentage": 80, "resets_at": 2_000_000_000}}}, now=1_999_990_000,
                cfg=dict(limits.DEFAULT_ALERTS, projection=False))
        con.close()
        self.assertEqual(sorted(e[0] for e in self.events()), ["context", "context_tokens", "five_hour"])


if __name__ == "__main__":
    unittest.main()
