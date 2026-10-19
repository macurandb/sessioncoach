"""Tests on synthetic transcripts only (no real data). Run: python3 -m unittest discover -s tests"""
import json
import os
import plistlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contextlib
import datetime as dt
import io
import sqlite3

import isolate
from serverauth import session_cookie

from sessioncoach import accounts, analysis, collect, cost, limits, pricing, server, store
from sessioncoach.projects import Labeler

SECRET = "SECRET-DO-NOT-STORE-4711"


def usage_line(ts, msg_id, model="claude-opus-5-5", inp=10, out=100, cr=1000, cw=0, req=None):
    return {"type": "assistant", "timestamp": ts, "requestId": req or "r-" + msg_id, "sessionId": "s1",
            "message": {"id": msg_id, "model": model, "role": "assistant",
                        "content": [{"type": "text", "text": SECRET}],
                        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cw}}}


def cost_line(total, model="claude-opus-5-5"):
    return {"type": "cost-state", "totalCostUSD": total,
            "modelUsage": {model: {"costUSD": total, "inputTokens": 1, "outputTokens": 1}}}


def read_text(path):
    with open(path) as fh:
        return fh.read()


def read_json(path):
    with open(path) as fh:
        return json.load(fh)


def read_plist(path):
    with open(path, "rb") as fh:
        return plistlib.load(fh)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        self.pdir = os.path.join(self.projects, "-Users-someone-code-demo")
        os.makedirs(self.pdir)
        self.db = os.path.join(self.tmp, "metrics.db")

    def tearDown(self):
        isolate.rmtree(self.tmp)

    def write(self, name, lines):
        with open(os.path.join(self.pdir, name), "w") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")

    def ingest(self):
        con = store.connect(self.db)
        added = collect.ingest(con, [self.projects])
        cost.rebuild_slices(con)
        return con, added


class TestIngest(Base):
    def test_idempotent_and_dedup(self):
        line = usage_line("2026-10-01T10:00:00Z", "m1")
        self.write("s1.jsonl", [line, line, usage_line("2026-10-01T10:01:00Z", "m2")])
        con, added = self.ingest()
        self.assertEqual(added["req"], 2)
        added2 = collect.ingest(con, [self.projects])
        self.assertEqual(added2, {"req": 0, "cost": 0, "sub": 0})

    def test_tool_calls_names_only(self):
        line = usage_line("2026-10-01T10:00:00Z", "m1")
        line["message"]["content"] = [
            {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"file_path": SECRET}},
            {"type": "tool_use", "id": "tu2", "name": "mcp__codebase-memory-mcp__search_graph", "input": {"q": SECRET}}]
        self.write("s1.jsonl", [line, line])
        con, _ = self.ingest()
        collect.ingest(con, [self.projects])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM tool_calls WHERE msg_id = 'm1'").fetchone()[0], 2)
        v = analysis.tools_view(con, days=0)
        self.assertEqual(dict(v["per_day"])["2026-10-01"], {"exploration": 1, "mcp:codebase-memory-mcp": 1})
        con.close()
        with open(self.db, "rb") as fh:
            self.assertNotIn(SECRET.encode(), fh.read())

    def test_no_content_stored(self):
        # a task-notification read from a file (tool_result) or pasted by the user is text, not a notification
        fake = ("<task-notification><task-id>%s</task-id><usage><subagent_tokens>500</subagent_tokens>"
                "<tool_uses>3</tool_uses><duration_ms>1000</duration_ms></usage></task-notification>" % SECRET)
        read = {"type": "user", "timestamp": "2026-10-01T10:02:00Z",
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": fake}]}}
        pasted = {"type": "user", "timestamp": "2026-10-01T10:03:00Z", "origin": {"kind": "human"},
                  "message": {"role": "user", "content": fake}}
        quoted = {"type": "user", "timestamp": "2026-10-01T10:04:00Z", "message": {"role": "user", "content": "look: " + fake}}
        typed = {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-01T10:05:00Z", "content": fake}
        self.write("s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1"), cost_line(1.0), read, pasted, quoted, typed])
        con, _ = self.ingest()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM subagents").fetchone()[0], 0)
        con.close()
        with open(self.db, "rb") as fh:
            self.assertNotIn(SECRET.encode(), fh.read())

    def test_subagent_ids_and_types_must_look_like_names(self):
        free = "password=hunter2 client=ACME"
        note = ("<task-notification><task-id>%s</task-id><usage><subagent_tokens>500</subagent_tokens>"
                "<tool_uses>3</tool_uses><duration_ms>1000</duration_ms></usage></task-notification>")
        lines = [{"type": "assistant", "timestamp": "2026-10-01T10:00:00Z", "message": {"id": "ag", "role": "assistant", "content": [
                     {"type": "tool_use", "id": "tu-a", "name": "Agent", "input": {"subagent_type": free}},
                     {"type": "tool_use", "id": "tu-b", "name": "Agent", "input": {"subagent_type": "gsd:executor"}}]}},
                 {"type": "user", "timestamp": "2026-10-01T10:01:00Z",
                  "toolUseResult": {"agentId": "a1", "totalTokens": 10},
                  "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu-a"}]}},
                 {"type": "user", "timestamp": "2026-10-01T10:01:00Z",
                  "toolUseResult": {"agentId": "a2", "totalTokens": 20},
                  "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu-b"}]}},
                 {"type": "user", "timestamp": "2026-10-01T10:01:00Z",
                  "toolUseResult": {"agentId": free, "totalTokens": 30},
                  "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu-b"}]}},
                 {"type": "user", "timestamp": "2026-10-01T10:02:00Z", "origin": {"kind": "task-notification"},
                  "message": {"role": "user", "content": note % free}}]
        self.write("s1.jsonl", lines)
        con, _ = self.ingest()
        rows = {r["agent_id"]: r["subagent_type"] for r in con.execute("SELECT agent_id, subagent_type FROM subagents")}
        self.assertEqual(rows, {"a1": "?", "a2": "gsd:executor"})
        con.close()
        with open(self.db, "rb") as fh:
            self.assertNotIn(b"hunter2", fh.read())

    def test_real_notifications_count_in_every_shape(self):
        note = ("<task-notification>\n<task-id>%s</task-id>\n<usage><subagent_tokens>%d</subagent_tokens>"
                "<tool_uses>3</tool_uses><duration_ms>1000</duration_ms></usage>\n</task-notification>")
        lines = [{"type": "user", "timestamp": "2026-10-01T10:01:00Z", "origin": {"kind": "task-notification"},
                  "message": {"role": "user", "content": note % ("a1", 100)}},
                 {"type": "attachment", "timestamp": "2026-10-01T10:02:00Z",
                  "attachment": {"type": "queued_command", "commandMode": "task-notification", "prompt": note % ("a2", 200)}},
                 {"type": "attachment", "timestamp": "2026-10-01T10:02:00Z",  # a prompt the user queued
                  "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": note % ("a3", 300)}}]
        self.write("s1.jsonl", lines)
        con, _ = self.ingest()
        rows = {r["agent_id"]: r["tokens"] for r in con.execute("SELECT agent_id, tokens FROM subagents")}
        self.assertEqual(rows, {"a1": 100, "a2": 200})

    def test_subagent_notification_keeps_max(self):
        note = ("<task-notification><task-id>a1</task-id><usage><subagent_tokens>500</subagent_tokens>"
                "<tool_uses>3</tool_uses><duration_ms>1000</duration_ms></usage></task-notification>")
        later = note.replace("500", "800")
        lines = [usage_line("2026-10-01T10:00:00Z", "m1"),
                 {"type": "user", "timestamp": "2026-10-01T10:05:00Z", "message": {"role": "user", "content": note}},
                 {"type": "user", "timestamp": "2026-10-01T10:09:00Z", "message": {"role": "user", "content": later}}]
        self.write("s1.jsonl", lines)
        con, _ = self.ingest()
        row = con.execute("SELECT tokens FROM subagents").fetchone()
        self.assertEqual(row["tokens"], 800)


class TestCost(Base):
    def test_conservation_and_tail_estimate(self):
        self.write("s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1"), usage_line("2026-10-01T10:30:00Z", "m2"),
                                cost_line(0.05), usage_line("2026-10-02T09:00:00Z", "m3")])
        con, _ = self.ingest()
        rec = con.execute("SELECT SUM(usd) FROM cost_slices WHERE estimated = 0").fetchone()[0]
        est = con.execute("SELECT SUM(usd) FROM cost_slices WHERE estimated = 1").fetchone()[0]
        self.assertAlmostEqual(rec, 0.05, places=6)
        self.assertGreater(est, 0)  # m3 came after the last snapshot
        self.assertEqual(cost.verify(con)["conservation"]["status"], "PASS")

    def test_old_activity_is_estimated_not_lost(self):
        # lots of old activity, then a small cost-state counter (new process): old part must be estimated
        old = [usage_line("2026-08-0%dT10:00:00Z" % d, "old%d" % d, cr=5_000_000) for d in range(1, 6)]
        self.write("s1.jsonl", [*old, usage_line("2026-10-01T10:00:00Z", "new1"), cost_line(0.01)])
        con, _ = self.ingest()
        aug = con.execute("SELECT SUM(usd) FROM cost_slices WHERE day LIKE '2026-08%' AND estimated = 1").fetchone()[0]
        self.assertGreater(aug or 0, 0)
        rec = con.execute("SELECT SUM(usd) FROM cost_slices WHERE estimated = 0").fetchone()[0]
        self.assertAlmostEqual(rec, 0.01, places=6)

    def test_counter_restart_counts_from_zero(self):
        self.assertEqual(cost.increment(5.0, 7.5), 2.5)
        self.assertEqual(cost.increment(7.5, 1.0), 1.0)


class TestRecommendations(Base):
    def test_r1_cache_resumes(self):
        lines = []
        for i in range(4):  # 4 resumes after >1h with a big cache write
            lines.append(usage_line("2026-10-0%dT08:00:00Z" % (i + 1), "a%d" % i))
            lines.append(usage_line("2026-10-0%dT10:30:00Z" % (i + 1), "b%d" % i, cw=200_000))
        self.write("s1.jsonl", [*lines, cost_line(5.0)])
        con, _ = self.ingest()
        data = analysis.build(con, days=None, labeler=Labeler(), today=None)
        ids = [r["id"] for r in data["recommendations"]]
        self.assertIn("R1", ids)
        r1 = next(r for r in data["recommendations"] if r["id"] == "R1")
        self.assertEqual(r1["params"]["resumes"], 4)


class TestLabeler(unittest.TestCase):
    def test_rules_and_default(self):
        lab = Labeler([{"match": "*code-app*", "project": "app", "tenant": "acme"}, {"match": "*", "tenant": "me"}])
        self.assertEqual(lab("-Users-x-code-app-wt1"), ("app", "acme"))
        self.assertEqual(lab("-Users-x-other")[1], "me")


class TestServer(Base):
    def test_host_guard_and_api(self):
        self.write("s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1"), cost_line(0.02)])
        con, _ = self.ingest()
        con.close()
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.db))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/api/data?days=0" % port, headers={"Cookie": session_cookie(httpd)})
            with urllib.request.urlopen(req) as r:
                data = json.loads(r.read())
            self.assertIn("summary", data)
            req = urllib.request.Request("http://127.0.0.1:%d/api/data" % port, headers={"Host": "evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 403)
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestAccounts(Base):
    def _resolver(self, company="personal"):
        seen = [{"root": self.projects, "seen_at": "2026-01-01T00:00:00+00:00", "org_hash": "h1",
                 "org_type": "claude_max", "seat_tier": None, "rate_tier": "default_claude_max_20x"}]
        mapping = {"me": {"label": "Me · Max", "company": company, "org_hash": "h1"}}
        return accounts.AccountResolver(seen, mapping, self.projects)

    def test_resolve_and_auto_label(self):
        r = self._resolver()
        self.assertEqual(r(self.projects)["id"], "me")
        auto = accounts.AccountResolver(r.history and [dict(x[1]) for x in r.history[self.projects]], {}, self.projects)
        self.assertTrue(auto(self.projects)["label"].startswith("Max"))
        self.assertEqual(r("/elsewhere")["id"], "unknown")

    def test_detect_reads_only_non_personal_fields(self):
        cfg = os.path.join(self.tmp, "cfg")
        os.makedirs(os.path.join(cfg, "projects"))
        with open(os.path.join(cfg, ".claude.json"), "w") as fh:
            json.dump({"oauthAccount": {"organizationUuid": "org-1", "emailAddress": "x@y.z", "displayName": "X",
                                        "organizationType": "claude_team", "seatTier": "premium"}}, fh)
        a = accounts.detect(os.path.join(cfg, "projects"))
        self.assertEqual(a["org_type"], "claude_team")
        self.assertEqual(a["seat_tier"], "premium")
        self.assertNotIn("x@y.z", json.dumps(a))
        self.assertNotIn("org-1", json.dumps(a))

    def test_r14_mismatch(self):
        self.write("s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1"), cost_line(2.0)])
        con, _ = self.ingest()
        lab = Labeler([{"match": "*", "project": "demo", "tenant": "client-a"}])
        data = analysis.build(con, days=None, labeler=lab, resolver=self._resolver(company="personal"))
        r14 = [r for r in data["recommendations"] if r["id"] == "R14"]
        self.assertEqual(len(r14), 1)
        self.assertEqual(r14[0]["params"]["tenant"], "client-a")
        self.assertAlmostEqual(data["accounts"][0]["other_company_usd"], 2.0, places=2)


class TestTransition(Base):
    def test_acknowledged_until_date_then_warning(self):
        self.write("s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1"), cost_line(2.0)])
        con, _ = self.ingest()
        lab = Labeler([{"match": "*", "project": "demo", "tenant": "client-a"}])
        seen = [{"root": self.projects, "seen_at": "2026-01-01T00:00:00+00:00", "org_hash": "h1", "org_type": "claude_max",
                 "seat_tier": None, "rate_tier": None}]
        mapping = {"me": {"label": "Me", "company": "personal", "org_hash": "h1",
                          "transition": [{"tenant": "client-a", "until": "2026-10-31"}]}}
        res = accounts.AccountResolver(seen, mapping, self.projects)
        before = analysis.build(con, days=None, labeler=lab, resolver=res, today=dt.date(2026, 10, 15))
        r14 = next(r for r in before["recommendations"] if r["id"] == "R14")
        self.assertTrue(r14["acknowledged"])
        after = analysis.build(con, days=None, labeler=lab, resolver=res, today=dt.date(2026, 11, 2))
        r14 = next(r for r in after["recommendations"] if r["id"] == "R14")
        self.assertFalse(r14["acknowledged"])
        self.assertTrue(r14["params"]["expired"])
        self.assertEqual(before["alignment"]["overall_pct"], 0.0)


class TestDay(Base):
    def test_hourly_adds_up_and_prev_day(self):
        self.write("s1.jsonl", [usage_line("2026-10-01T09:00:00Z", "a"), usage_line("2026-10-01T15:00:00Z", "b"),
                                cost_line(3.0), usage_line("2026-10-02T10:00:00Z", "c"), cost_line(4.0)])
        con, _ = self.ingest()
        d2 = cost.parse_ts("2026-10-02T10:00:00Z").date()
        now = cost.parse_ts("2026-10-03T12:00:00Z")
        r = analysis.day(con, date=d2, labeler=Labeler(), resolver=accounts.AccountResolver([], {}, self.projects), now=now)
        self.assertFalse(r["is_today"])
        self.assertAlmostEqual(r["cumulative"]["today"][-1], r["totals"]["today"]["usd"], places=2)
        self.assertAlmostEqual(r["cumulative"]["prev"][-1], r["totals"]["prev"]["usd"], places=2)
        self.assertAlmostEqual(r["totals"]["today"]["usd"] + r["totals"]["prev"]["usd"], 4.0, places=2)


class TestMigration(Base):
    def test_old_db_gets_new_columns_without_losing_rows(self):
        con = sqlite3.connect(self.db)
        con.executescript("CREATE TABLE requests (project_dir TEXT NOT NULL, file TEXT NOT NULL, msg_id TEXT, "
                          "request_id TEXT, session_id TEXT, kind TEXT, ts TEXT, model TEXT, input INTEGER, output INTEGER, "
                          "cache_read INTEGER, cache_write INTEGER, cache_write_5m INTEGER, cache_write_1h INTEGER, "
                          "PRIMARY KEY (project_dir, file, msg_id, request_id));"
                          "INSERT INTO requests VALUES "
                          "('p','f','m','r','s','main','2026-10-01T10:00:00Z','claude-opus-5-5',1,1,1,0,NULL,NULL);"
                          "CREATE TABLE cost_slices "
                          "(day TEXT, project_dir TEXT, session_id TEXT, model TEXT, usd REAL, estimated INTEGER);")
        con.commit()
        con.close()
        con = store.connect(self.db)
        cols = {r[1] for r in con.execute("PRAGMA table_info(requests)")}
        self.assertIn("root", cols)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)
        collect.backfill_roots(con, "/default/root")
        self.assertEqual(con.execute("SELECT root FROM requests").fetchone()[0], "/default/root")


class TestRefreshGuard(Base):
    def test_cross_origin_refresh_is_rejected(self):
        self.write("s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1")])
        con, _ = self.ingest()
        con.close()
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.db))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/api/refresh" % port, data=b"", method="POST",
                                         headers={"Origin": "https://evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 403)
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestLimits(Base):
    def setUp(self):
        super().setUp()
        self.sent = []
        self._orig = limits.notify
        limits.notify = lambda t, m: self.sent.append(t)

    def tearDown(self):
        limits.notify = self._orig
        super().tearDown()

    def payload(self, five, seven=10, reset=2_000_000_000, sid="s1"):
        return {"session_id": sid, "model": {"id": "claude-opus-5-5"}, "context_window": {"used_percentage": 20},
                "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": reset},
                                "seven_day": {"used_percentage": seven, "resets_at": reset + 86400}}}

    def test_threshold_alerts_once_per_window(self):
        con = store.connect(self.db)
        cfg = dict(limits.DEFAULT_ALERTS, projection=False)
        limits.record_and_alert(con, self.payload(70), now=1_999_990_000, cfg=cfg)
        self.assertEqual(self.sent, [])
        limits.record_and_alert(con, self.payload(80), now=1_999_990_100, cfg=cfg)
        limits.record_and_alert(con, self.payload(85), now=1_999_990_200, cfg=cfg)
        self.assertEqual(len(self.sent), 1)  # 75 % crossed once
        limits.record_and_alert(con, self.payload(92), now=1_999_990_300, cfg=cfg)
        self.assertEqual(len(self.sent), 2)  # 90 % crossed once

    def test_bad_thresholds_in_alerts_json_fall_back_to_the_defaults(self):
        # a text or a value <= 0 made the status line fail (TypeError comparing a percent with a string)
        os.environ["KIT_DATA_DIR"] = self.tmp
        self.addCleanup(os.environ.pop, "KIT_DATA_DIR", None)
        with open(os.path.join(self.tmp, "alerts.json"), "w") as fh:
            json.dump({"five_hour": ["x"], "seven_day": 80, "context": [0], "context_tokens": [True], "projection": False}, fh)
        cfg = limits.load_alerts()
        for k in ("five_hour", "seven_day", "context", "context_tokens"):
            self.assertEqual(cfg[k], limits.DEFAULT_ALERTS[k], k)
        self.assertFalse(cfg["projection"])
        con = store.connect(self.db)
        limits.record_and_alert(con, self.payload(80), now=1_999_990_000, cfg=cfg)
        self.assertEqual(len(self.sent), 1)  # the default 75 %
        with open(os.path.join(self.tmp, "alerts.json"), "w") as fh:
            json.dump({"five_hour": [], "seven_day": [60.5], "context_tokens": [1]}, fh)
        cfg = limits.load_alerts()  # valid ones stay: [] turns an alert off, decimals worked before
        self.assertEqual((cfg["five_hour"], cfg["seven_day"], cfg["context_tokens"]), ([], [60.5], [1]))
        with open(os.path.join(self.tmp, "alerts.json"), "w") as fh:
            json.dump({"five_hour": None, "seven_day": False, "context": 0, "context_tokens": ""}, fh)
        cfg = limits.load_alerts()  # an empty value also turned the alert off: it stays as written
        self.assertEqual([cfg[k] for k in ("five_hour", "seven_day", "context", "context_tokens")], [None, False, 0, ""])
        self.sent.clear()
        limits.record_and_alert(con, self.payload(99, reset=2_000_100_000), now=1_999_990_100, cfg=cfg)
        self.assertEqual(self.sent, [])

    def test_pace_projection_before_reset(self):
        con = store.connect(self.db)
        cfg = dict(limits.DEFAULT_ALERTS, five_hour=[], seven_day=[], context=[])
        reset = 1_999_999_999
        limits.record_and_alert(con, self.payload(40, reset=reset), now=reset - 4 * 3600, cfg=cfg)
        info = limits.record_and_alert(con, self.payload(60, reset=reset), now=reset - 4 * 3600 + 1200, cfg=cfg)
        self.assertIn("five_hour", info["eta"])  # +20 % in 20 min -> 100 % long before reset
        self.assertEqual(len(self.sent), 1)
        self.assertIn("⚠", limits.render(info, color=False))

    def test_statusline_keeps_chained_output(self):
        chain = os.path.join(self.tmp, "chain.sh")
        with open(chain, "w") as fh:
            fh.write("cat >/dev/null; echo ORCA-LINE\n")
        os.environ["KIT_DATA_DIR"] = self.tmp
        try:
            buf = io.StringIO()
            sys_stdin = sys.stdin
            sys.stdin = io.StringIO(json.dumps(self.payload(30)))
            with contextlib.redirect_stdout(buf):
                limits.statusline_main(chain)
        finally:
            sys.stdin = sys_stdin
            del os.environ["KIT_DATA_DIR"]
        out = buf.getvalue()
        self.assertTrue(out.startswith("ORCA-LINE"))
        self.assertIn("5h 30%", out)

    def test_install_uninstall_roundtrip(self):
        settings = os.path.join(self.tmp, "settings.json")
        with open(settings, "w") as fh:
            json.dump({"model": "opus", "statusLine": {"type": "command", "command": "sh orca.sh"}}, fh)
        os.environ["KIT_DATA_DIR"] = self.tmp
        try:
            limits.install(settings, kit_bin="/x/bin/kit")
            with open(settings) as fh:
                self.assertIn("statusline --chain-file", json.load(fh)["statusLine"]["command"])
            self.assertEqual(read_text(os.path.join(self.tmp, "statusline-chain.sh")).strip(), "sh orca.sh")
            limits.uninstall(settings)
            with open(settings) as fh:
                d = json.load(fh)
            self.assertEqual(d["statusLine"]["command"], "sh orca.sh")
            self.assertEqual(d["model"], "opus")
        finally:
            del os.environ["KIT_DATA_DIR"]

    def test_budget_alert(self):
        self.write("s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1"), cost_line(9.0)])
        con, _ = self.ingest()
        day = cost.parse_ts("2026-10-01T10:00:00Z").date()
        cfg = dict(limits.DEFAULT_ALERTS, budgets=[{"name": "all-day", "scope": "all", "period": "day", "equiv_usd": 10}])
        st = limits.evaluate_budgets(con, cfg, today=day, notify_fn=lambda t, m: self.sent.append(t))
        self.assertAlmostEqual(st[0]["pct"], 90.0, places=0)
        self.assertEqual(len(self.sent), 1)  # 80 % crossed
        limits.evaluate_budgets(con, cfg, today=day, notify_fn=lambda t, m: self.sent.append(t))
        self.assertEqual(len(self.sent), 1)  # not repeated


class TestAnon(Base):
    def test_names_replaced_and_filters_roundtrip(self):
        self.write("s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1"), cost_line(2.0)])
        con, _ = self.ingest()
        from sessioncoach import anon
        lab = Labeler([{"match": "*", "project": "acme-secret-shop", "tenant": "acme-corp"}])
        res = accounts.AccountResolver([], {}, self.projects)
        a = anon.Anonymizer(con, [{"name": "acme-budget"}], lang="en", labeler=lab, resolver=res)
        data = analysis.build(con, days=None, labeler=lab, resolver=res)
        text = json.dumps(a.apply(data))
        for secret in ("acme-secret-shop", "acme-corp", "-Users-someone-code-demo"):
            self.assertNotIn(secret, text)
        self.assertIn("Project A", text)
        self.assertEqual(a.unmap("Project A"), "acme-secret-shop")


if __name__ == "__main__":
    unittest.main()


class TestNudge(Base):

    def setUp(self):
        super().setUp()
        self.claude_home = isolate.claude_home(self)

    def _transcript(self, ts, ctx):
        self.write("s1.jsonl", [usage_line(ts, "m1", inp=0, cr=ctx, cw=0)])
        return {"transcript_path": os.path.join(self.pdir, "s1.jsonl")}

    def test_warns_after_idle_with_big_context(self):
        from sessioncoach import nudge
        data = self._transcript("2026-10-01T10:00:00Z", 250000)
        now = dt.datetime(2026, 10, 1, 11, 30, tzinfo=dt.timezone.utc)
        text = nudge.check(data, {"lang": "en"}, now=now)
        self.assertIn("90 min", text)
        self.assertIn("250k", text)
        self.assertNotIn(SECRET, text)

    def test_bad_nudge_numbers_in_alerts_json_are_ignored(self):
        # a text made the hook fail with TypeError on every prompt; a negative idle_min warned after any pause
        from sessioncoach import config, nudge
        os.environ["KIT_DATA_DIR"] = self.tmp
        self.addCleanup(os.environ.pop, "KIT_DATA_DIR", None)
        data = self._transcript("2026-10-01T10:00:00Z", 250000)
        for now_min, want in ((20, None), (90, "90 min")):  # the cache TTL (1 h) decides, as with nothing set
            now = dt.datetime(2026, 10, 1, 10, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=now_min)
            for bad in ("x", -5, True, None):
                with open(os.path.join(self.tmp, "alerts.json"), "w") as fh:
                    json.dump({"nudge": {"idle_min": bad, "min_context_tokens": bad, "block": True}, "lang": "en"}, fh)
                a = config.load_alerts()
                self.assertEqual(a["nudge"], {"block": True}, bad)
                text = nudge.check(data, dict(a["nudge"], lang="en"), now=now)
                if want:
                    self.assertIn(want, text, bad)
                else:
                    self.assertIsNone(text, bad)
        with open(os.path.join(self.tmp, "alerts.json"), "w") as fh:
            json.dump({"nudge": {"idle_min": 15, "min_context_tokens": 2000}}, fh)
        self.assertEqual(config.load_alerts()["nudge"], {"idle_min": 15, "min_context_tokens": 2000})

    def test_says_exactly_what_to_type(self):
        from sessioncoach import nudge
        data = self._transcript("2026-10-01T10:00:00Z", 250000)
        now = dt.datetime(2026, 10, 1, 11, 30, tzinfo=dt.timezone.utc)
        day = now.astimezone().date().isoformat()
        for lang, handoff in (("es", "«lee docs/HANDOFF-%s.md y continúa»"), ("en", '"read docs/HANDOFF-%s.md and continue"')):
            text = nudge.check(data, {"lang": lang}, now=now)
            self.assertIn("/compact ", text)
            self.assertIn("/clear", text)
            self.assertIn(handoff % day, text)
            self.assertEqual(text.count("docs/HANDOFF-%s.md" % day), 2)

    def test_with_handoff_installed_it_says_type_handoff(self):
        from sessioncoach import handoff, nudge
        data = self._transcript("2026-10-01T10:00:00Z", 250000)
        now = dt.datetime(2026, 10, 1, 11, 30, tzinfo=dt.timezone.utc)
        before = {lang: nudge.check(data, {"lang": lang}, now=now) for lang in ("es", "en")}
        proj = os.path.join(self.tmp, "proj")
        handoff.install(os.path.join(proj, ".claude", "settings.local.json"))  # this project only
        for lang, word in (("es", "escribe /handoff"), ("en", "type /handoff")):
            self.assertEqual(nudge.check(data, {"lang": lang}, now=now), before[lang])  # another project: unchanged
            text = nudge.check(dict(data, cwd=proj), {"lang": lang}, now=now)
            self.assertIn(word, text)
            self.assertIn("/compact ", text)
            self.assertNotIn("docs/HANDOFF-", text)
            self.assertEqual(text.split("\n")[:3], before[lang].split("\n")[:3])
        handoff.install()  # the user's own commands: every project
        self.assertIn("/handoff", nudge.check(data, {"lang": "en"}, now=now))

    def test_compacted_after_the_last_turn_is_not_a_big_context(self):
        from sessioncoach import nudge
        data = self._transcript("2026-10-01T10:00:00Z", 850000)
        with open(data["transcript_path"], "a") as fh:
            fh.write(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": "2026-10-01T10:05:00Z",
                                 "compactMetadata": {"trigger": "manual", "preTokens": 850000, "postTokens": 13000}}) + "\n")
        now = dt.datetime(2026, 10, 1, 13, 30, tzinfo=dt.timezone.utc)
        self.assertIsNone(nudge.check(data, {"lang": "es"}, now=now))
        self.assertEqual(nudge.last_turn_info(data["transcript_path"]), ("2026-10-01T10:05:00Z", 13000, None))

    def test_steps_happen_in_the_same_session(self):
        from sessioncoach import nudge
        data = self._transcript("2026-10-01T10:00:00Z", 250000)
        now = dt.datetime(2026, 10, 1, 11, 30, tzinfo=dt.timezone.utc)
        text = nudge.check(data, {"lang": "es"}, now=now)
        self.assertIn("en ESTA sesión", text)
        self.assertLess(text.index("1) aquí"), text.index("2) cuando lo escriba, /clear"))

    def test_prompt_language_from_its_words(self):
        from sessioncoach import nudge
        for text, want in (("tené en cuenta que el mensaje puede ser en inglés", "es"), ("Si por favor", "es"),
                           ("haz el merge", "es"), ("please fix the failing test", "en"), ("what is the next step?", "en"),
                           ("ok", None), ("", None), (None, None), ("`git status` /Users/x/the/and.py", None),
                           ("sigue con `the and of` y https://x.com/the/and", "es")):
            self.assertEqual(nudge.prompt_lang(text), want, text)

    def test_claude_code_language_setting_project_over_user(self):
        from sessioncoach import nudge
        user = os.path.join(self.claude_home, ".claude", "settings.json")
        proj = os.path.join(self.tmp, "proj")
        self.assertIsNone(nudge.settings_lang(proj))
        for value, want in (("Español", "es"), ("spanish", "es"), ("English", "en"), ("inglés", "en"), ("Français", None)):
            self.write_json(user, {"language": value})
            self.assertEqual(nudge.settings_lang(proj), want, value)
        self.write_json(os.path.join(proj, ".claude", "settings.json"), {"language": "English"})
        self.assertEqual(nudge.settings_lang(proj), "en")
        self.write_json(os.path.join(proj, ".claude", "settings.local.json"), {"language": "español"})
        self.assertEqual(nudge.settings_lang(proj), "es")
        cfg = os.path.join(self.tmp, "company")
        self.write_json(os.path.join(cfg, "settings.json"), {"language": "English"})
        os.environ["CLAUDE_CONFIG_DIR"] = cfg
        self.assertEqual(nudge.settings_lang(None), "en")

    def test_message_language_order(self):
        from sessioncoach import nudge
        self.write_json(os.path.join(self.claude_home, ".claude", "settings.json"), {"language": "Español"})
        self.assertEqual(nudge.message_lang({"prompt": "please go on with the next step"}, {"lang": "es"}), "en")
        # alerts.json `lang` now comes before Claude Code's setting (one order for every message, config.lang)
        self.assertEqual(nudge.message_lang({"prompt": "ok"}, {"lang": "en"}), "en")
        self.assertEqual(nudge.message_lang({"prompt": "ok"}, {}), "es")
        os.remove(os.path.join(self.claude_home, ".claude", "settings.json"))
        self.assertEqual(nudge.message_lang({"prompt": "ok"}, {"lang": "es"}), "es")
        with mock.patch.dict(os.environ, {"LANG": "C.UTF-8"}):
            self.assertEqual(nudge.message_lang({}, {}), "en")

    def test_warning_follows_the_prompt(self):
        from sessioncoach import nudge
        data = dict(self._transcript("2026-10-01T10:00:00Z", 250000), prompt="sigue con el siguiente paso por favor")
        now = dt.datetime(2026, 10, 1, 11, 30, tzinfo=dt.timezone.utc)
        self.assertIn("sin actividad", nudge.check(data, {"lang": "en"}, now=now))

    def write_json(self, path, d):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(d, fh)

    def test_quiet_when_recent_or_small(self):
        from sessioncoach import nudge
        now = dt.datetime(2026, 10, 1, 11, 30, tzinfo=dt.timezone.utc)
        self.assertIsNone(nudge.check(self._transcript("2026-10-01T11:00:00Z", 250000), now=now))
        self.assertIsNone(nudge.check(self._transcript("2026-10-01T09:00:00Z", 50000), now=now))
        self.assertIsNone(nudge.check({"transcript_path": "/nonexistent"}, now=now))

    def test_install_is_additive_and_reversible(self):
        from sessioncoach import nudge
        path = os.path.join(self.tmp, "settings.json")
        other = {"hooks": [{"type": "command", "command": "other-hook"}]}
        with open(path, "w") as fh:
            json.dump({"hooks": {"UserPromptSubmit": [other]}, "model": "x"}, fh)
        nudge.install(path, kit_bin="/k/bin/kit")
        self.assertEqual(nudge.install(path, kit_bin="/k/bin/kit"), "already installed")
        s = read_json(path)
        self.assertEqual(len(s["hooks"]["UserPromptSubmit"]), 2)
        self.assertEqual(s["hooks"]["UserPromptSubmit"][0], other)
        nudge.uninstall(path)
        s = read_json(path)
        self.assertEqual(s["hooks"]["UserPromptSubmit"], [other])
        self.assertEqual(s["model"], "x")


class TestReview(Base):
    """Daily review: last active day, one tip, read-only build, delivered once."""

    def setUp(self):
        super().setUp()
        os.environ["KIT_DATA_DIR"] = self.tmp
        self._sched = os.environ.get("KIT_SCHEDULER")
        os.environ["KIT_SCHEDULER"] = "launchd"  # the plist assertions below; other schedulers: test_scheduler.py
        self.res = accounts.AccountResolver([], {}, self.projects)

    def tearDown(self):
        del os.environ["KIT_DATA_DIR"]
        if self._sched is None:
            os.environ.pop("KIT_SCHEDULER", None)
        else:
            os.environ["KIT_SCHEDULER"] = self._sched
        super().tearDown()

    def cfg(self, **kw):
        from sessioncoach import review
        return review.load_config({"review": kw, "lang": "en"})

    def build(self, con, run_date, lab=None, **kw):
        from sessioncoach import review
        return review.build(con, run_date, self.cfg(**kw), labeler=lab or Labeler(), resolver=self.res)

    def test_monday_reviews_friday(self):
        self.write("s1.jsonl", [usage_line("2026-10-08T12:00:00Z", "m1"), usage_line("2026-10-09T12:00:00Z", "m2", cr=3000),
                                cost_line(0.5)])
        con, _ = self.ingest()
        r = self.build(con, dt.date(2026, 10, 12))  # a Monday
        self.assertEqual((r["day"], r["prev_day"]), ("2026-10-09", "2026-10-08"))
        self.assertGreater(r["totals"]["usd"], 0)
        self.assertEqual(self.build(con, dt.date(2026, 10, 12), lookback=1)["day"], "2026-10-11")
        self.assertIsNone(self.build(con, dt.date(2026, 10, 1))["day"])

    def test_bad_long_context_threshold_uses_the_default(self):
        # a text made the tips fail with TypeError; 0 or less counted a turn with no context as long and divided by zero
        self.write("s1.jsonl", [usage_line("2026-10-09T12:00:00Z", "m1", inp=0, cr=0),
                                usage_line("2026-10-09T13:00:00Z", "m2", cr=400000), cost_line(1.0)])
        con, _ = self.ingest()
        good = self.build(con, dt.date(2026, 10, 10))
        self.assertGreater(good["causes"]["long_turns"], 0)
        for bad in ("big", "300000", 0, -5, True, None, float("inf")):
            r = self.build(con, dt.date(2026, 10, 10), long_context_tokens=bad)
            self.assertEqual(r["config"]["long_context_tokens"], 300000, bad)
            self.assertEqual((r["causes"], r["tips"]), (good["causes"], good["tips"]), bad)
        self.assertEqual(self.cfg(long_context_tokens=150000)["long_context_tokens"], 150000)

    def test_dashboard_r16_uses_the_review_threshold(self):
        # one threshold since they were unified: alerts.json → review.long_context_tokens, 300k by default
        self.write("s1.jsonl", [usage_line("2026-10-09T12:00:00Z", "m1", cr=350000), cost_line(1.0)])
        con, _ = self.ingest()
        reqs = list(con.execute("SELECT * FROM requests"))

        def r16():
            return next((r["params"]["threshold_k"] for r in analysis.recommendations([], reqs, []) if r["id"] == "R16"), None)
        self.assertEqual(r16(), 300)
        with open(os.path.join(self.tmp, "alerts.json"), "w", encoding="utf-8") as fh:
            json.dump({"review": {"long_context_tokens": 400000}}, fh)
        self.assertIsNone(r16())  # 350k is no longer long
        self.assertEqual(analysis.recommendations([], reqs, [], long_context=200000)[0]["params"]["threshold_k"], 200)

    def test_r3_counts_the_dashboard_subagents_only(self):
        # same filter as the dashboard: general-purpose agents on Opus, and only the requests of their transcripts
        self.write("s1.jsonl", [usage_line("2026-10-09T12:00:00Z", "m1"), cost_line(1.0)])
        con, _ = self.ingest()
        pdir, sid = con.execute("SELECT project_dir, session_id FROM requests").fetchone()
        for aid, typ in (("gp1", "general-purpose"), ("ex1", "Explore")):
            con.execute("INSERT INTO subagents (project_dir, session_id, agent_id, subagent_type, model, ts, tokens) "
                        "VALUES (?, ?, ?, ?, 'claude-opus-5-5', '2026-10-09T12:30:00Z', 100)", (pdir, sid, aid, typ))
            con.execute("INSERT INTO requests (project_dir, file, msg_id, request_id, session_id, kind, ts, model, input, "
                        "output, cache_read, cache_write) VALUES (?, ?, ?, 'r', ?, 'subagent', '2026-10-09T12:31:00Z', "
                        "'claude-opus-5-5', 0, 1000000, 0, 0)", (pdir, "agent-%s.jsonl" % aid, aid, sid))
        con.commit()
        c = self.build(con, dt.date(2026, 10, 10))["causes"]
        one = pricing.weight("claude-opus-5-5", {"input": 0, "output": 1000000, "cache_read": 0, "cache_write": 0,
                                                 "cache_write_1h": None, "cache_write_5m": None})
        self.assertEqual((c["opus_agents"], c["opus_usd"]), (1, round(one, 2)))

    def test_config_merges_over_defaults(self):
        c = self.cfg(time="05:30")
        self.assertEqual(c["time"], "05:30")
        self.assertEqual(c["days"], ["mon", "tue", "wed", "thu", "fri"])
        self.assertEqual(c["max_tips"], 1)

    def test_tip_is_the_biggest_impact(self):
        from sessioncoach import review
        causes = {"resumes": 3, "resume_usd": 5.0, "long_turns": 10, "long_usd": 40.0, "long_excess_usd": 12.0,
                  "opus_agents": 2, "opus_usd": 8.0, "mixed_sessions": 1, "mixed_usd": 20.0}
        tips = review.tips(causes, self.cfg(max_tips=5))
        self.assertEqual([t["id"] for t in tips], ["R16", "R1", "R3", "MIX"])  # ranked by ceiling, mixing last
        self.assertEqual(tips[0]["saving_usd"], 12.0)
        self.assertEqual(len(review.tips(causes, self.cfg())), 1)
        none = dict(causes, resume_usd=0, long_excess_usd=0, opus_usd=0)
        self.assertEqual([t["id"] for t in review.tips(none, self.cfg())], ["MIX"])
        self.assertEqual(review.tips(dict(none, mixed_sessions=0), self.cfg()), [])

    def test_resumes_match_r1(self):
        # 4 resumes after > 1 h idle, each rewriting a big cache: same count as recommendation R1
        lines = [usage_line("2026-10-09T%02d:00:00Z" % h, "m%d" % h, cw=60000 if h > 10 else 0) for h in (10, 12, 14, 16, 18)]
        self.write("s1.jsonl", [*lines, cost_line(1.0)])
        con, _ = self.ingest()
        r = self.build(con, dt.date(2026, 10, 10))
        reqs = list(con.execute("SELECT * FROM requests"))
        r1 = next(x for x in analysis.recommendations([], reqs, []) if x["id"] == "R1")
        self.assertEqual(r["causes"]["resumes"], r1["params"]["resumes"])
        self.assertAlmostEqual(r["causes"]["resume_usd"], r1["params"]["usd"], places=2)
        self.assertEqual(r["tips"][0]["id"], "R1")

    def test_mixed_sessions(self):
        other = os.path.join(self.projects, "-Users-someone-code-billing")
        os.makedirs(other)
        with open(os.path.join(other, "s9.jsonl"), "w") as fh:
            fh.write(json.dumps(usage_line("2026-10-08T12:00:00Z", "o1")) + "\n")
        a = dict(usage_line("2026-10-09T12:00:00Z", "m1"), cwd="/Users/someone/code/demo")
        b = dict(usage_line("2026-10-09T12:05:00Z", "m2"), cwd="/Users/someone/code/demo/sub")  # own subdir: not mixing
        self.write("s1.jsonl", [a, b, cost_line(0.2)])
        con, _ = self.ingest()
        self.assertFalse(self.build(con, dt.date(2026, 10, 10))["sessions"][0]["mixed"])
        c = dict(usage_line("2026-10-09T12:10:00Z", "m3"), cwd="/Users/someone/code/billing")
        self.write("s1.jsonl", [a, b, c, cost_line(0.3)])
        collect.ingest(con, [self.projects])
        cost.rebuild_slices(con)
        lab = Labeler([{"match": "*demo*", "project": "demo", "tenant": "acme"},
                       {"match": "*billing*", "project": "billing", "tenant": "other"}])
        s = next(x for x in self.build(con, dt.date(2026, 10, 10), lab)["sessions"] if x["project"] == "demo")
        self.assertTrue(s["mixed"] and s["mixed_tenants"])

    def test_build_is_read_only_and_delivered_once(self):
        from sessioncoach import review
        self.write("s1.jsonl", [usage_line("2026-10-09T12:00:00Z", "m1"), cost_line(0.5)])
        con, _ = self.ingest()
        con.close()
        ro = server._ro(self.db)
        data = review.build(ro, dt.date(2026, 10, 10), self.cfg(), labeler=Labeler(), resolver=self.res)  # no write attempted
        ro.close()
        con = store.connect(self.db)
        sent = []
        cfg = self.cfg()
        self.assertEqual(review.deliver(con, data, cfg, notify_fn=lambda t, m: sent.append((t, m))), ["notification"])
        self.assertEqual(review.deliver(con, data, cfg, notify_fn=lambda t, m: sent.append((t, m))), [])
        self.assertEqual(review.deliver(con, data, cfg, notify_fn=lambda t, m: sent.append((t, m)), force=True), ["notification"])
        self.assertEqual(len(sent), 2)
        self.assertIn("2026-10-09", sent[0][0])
        self.assertNotIn(SECRET, json.dumps(sent))

    def test_schedule(self):
        from sessioncoach import review
        cfg = self.cfg(time="05:30", days=["mon", "sun"])
        self.assertEqual(review.calendar_intervals(cfg), [{"Weekday": 1, "Hour": 5, "Minute": 30}, {"Weekday": 0, "Hour": 5, "Minute": 30}])
        self.assertTrue(review.due(cfg, dt.datetime(2026, 10, 12, 5, 31)))       # Monday after 05:30
        self.assertFalse(review.due(cfg, dt.datetime(2026, 10, 12, 5, 29)))      # before the time
        self.assertFalse(review.due(cfg, dt.datetime(2026, 10, 13, 9, 0)))       # Tuesday: not a review day
        self.assertFalse(review.due(self.cfg(enabled=False), dt.datetime(2026, 10, 12, 9, 0)))
        out = review.install(cfg, kit_bin="/k/bin/kit", agents_dir=self.tmp, load=False)
        self.assertIn("05:30", out)
        p = read_plist(review.plist_path(self.tmp))
        self.assertEqual(p["ProgramArguments"][-2:], ["review", "--scheduled"])
        self.assertEqual(len(p["StartCalendarInterval"]), 2)
        review.uninstall(self.tmp, load=False)
        self.assertFalse(os.path.exists(review.plist_path(self.tmp)))

    def test_anonymous_review_hides_names(self):
        from sessioncoach import anon, review
        self.write("s1.jsonl", [usage_line("2026-10-09T12:00:00Z", "m1"), cost_line(0.5)])
        con, _ = self.ingest()
        lab = Labeler([{"match": "*", "project": "acme-secret-shop", "tenant": "acme-corp"}])
        data = review.build(con, dt.date(2026, 10, 10), self.cfg(), labeler=lab, resolver=self.res)
        self.assertIn("acme-secret-shop", json.dumps(data))
        out = json.dumps(anon.Anonymizer(con, [], lang="en", labeler=lab, resolver=self.res).apply(data))
        self.assertNotIn("acme", out.lower())

    def test_session_in_home_mixing_two_companies(self):
        home = os.path.join(self.projects, "-Users-someone")
        os.makedirs(home)
        lines = [dict(usage_line("2026-10-08T12:00:00Z", "h0"), cwd="/Users/someone"),
                 dict(usage_line("2026-10-09T12:00:00Z", "h1"), cwd="/Users/someone"),
                 dict(usage_line("2026-10-09T12:05:00Z", "h2"), cwd="/Users/someone/code/shop"),
                 dict(usage_line("2026-10-09T12:10:00Z", "h3"), cwd="/Users/someone/code/bank"), cost_line(0.3)]
        with open(os.path.join(home, "s5.jsonl"), "w") as fh:
            fh.write("".join(json.dumps(x) + "\n" for x in lines))
        con, _ = self.ingest()
        lab = Labeler([{"match": "*shop*", "project": "shop", "tenant": "client-a"},
                       {"match": "*bank*", "project": "bank", "tenant": "client-b"}])
        r = self.build(con, dt.date(2026, 10, 10), lab)
        s = next(x for x in r["sessions"] if x["session"].endswith("s5"))
        self.assertTrue(s["mixed_tenants"])
        # the day before it first went there, the session was not mixing yet
        before = self.build(con, dt.date(2026, 10, 9), lab)
        self.assertEqual(before["day"], "2026-10-08")
        self.assertEqual(len(before["sessions"]), 1)
        self.assertFalse(before["sessions"][0]["mixed"])

    def test_email_is_numbers_only(self):
        from sessioncoach import review
        out = os.path.join(self.tmp, "mail.txt")
        self.write("s1.jsonl", [usage_line("2026-10-09T12:00:00Z", "m1"), cost_line(0.5)])
        con, _ = self.ingest()
        lab = Labeler([{"match": "*", "project": "acme-secret-shop", "tenant": "acme-corp"}])
        cfg = self.cfg(channels=["email"], email_command=[sys.executable, "-c",
                       "import sys; open(%r, 'w').write(sys.argv[1] + '\\n' + sys.stdin.read())" % out, "{title}"])
        data = review.build(con, dt.date(2026, 10, 10), cfg, labeler=lab, resolver=self.res)
        self.assertEqual(review.deliver(con, data, cfg), ["email"])
        body = read_text(out)
        self.assertIn("2026-10-09", body)
        self.assertNotIn("acme", body)
        self.assertNotIn(SECRET, body)
