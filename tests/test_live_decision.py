"""`live --json` decision fields: turn_usd, usd_per_pct, accounts[].groups, clear_handoff and the
demo data. Everything runs on a temp HOME and data dir; the metrics database is built by hand so the
sample spans, the login switch, window resets and per-tenant sums are checked exactly."""
import datetime as dt
import json
import os
import unittest
from unittest import mock

import isolate

from sessioncoach import live, store

NOW = 1_800_000_000


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Base(unittest.TestCase):
    def setUp(self):
        self.home = isolate.claude_home(self)
        self.data = os.path.join(self.home, ".sessioncoach")
        os.makedirs(self.data, exist_ok=True)
        self.db = os.path.join(self.data, "metrics.db")
        self.ra = os.path.join(self.home, "acct-a", "projects")
        os.makedirs(os.path.join(os.path.dirname(self.ra), "sessions"))
        os.makedirs(self.ra)
        os.environ["SESSIONCOACH_PROJECTS_DIRS"] = self.ra
        self.addCleanup(os.environ.pop, "SESSIONCOACH_PROJECTS_DIRS", None)
        p = mock.patch.object(live, "_pid_alive", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def write_projects(self, doc):
        with open(os.path.join(self.data, "projects.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def connect(self):
        con = store.connect(self.db)
        self.addCleanup(con.close)
        return con

    def view(self):
        return live.view(now=NOW)

    def sample(self, con, root, ts, five_pct=None, five_reset=None, seven_pct=None, seven_reset=None):
        con.execute("INSERT INTO limits_samples (ts, session_id, root, five_pct, five_reset, seven_pct, seven_reset) "
                    "VALUES (?,?,?,?,?,?,?)", (ts, None, root, five_pct, five_reset, seven_pct, seven_reset))

    def login(self, con, root, switch):
        con.execute("INSERT INTO accounts_seen (root, seen_at, org_hash, org_type) VALUES (?,?,?,?)",
                    (root, dt.datetime.fromtimestamp(switch, dt.timezone.utc).isoformat(), "x", "claude_team"))

    def request(self, con, root, ts, project_dir="-a", model="claude-opus-5-5", input=250000):
        store.add_request(con, {"project_dir": project_dir, "file": "f.jsonl", "msg_id": "m%d" % ts,
                                "request_id": "r%d" % ts, "session_id": "s", "kind": "main",
                                "ts": iso(ts), "model": model, "input": input, "output": 0,
                                "cache_read": 0, "cache_write": 0, "cache_write_5m": 0,
                                "cache_write_1h": 0, "root": root})


class UsdPerPct(Base):
    def test_samples_across_a_login_switch(self):
        con = self.connect()
        switch = NOW - 2 * 86400
        self.login(con, self.ra, switch)
        seven_reset = NOW + 86400
        # a sample and a request before the login belong to the previous account
        self.sample(con, self.ra, switch - 3600, seven_pct=5.0, seven_reset=seven_reset)
        self.request(con, self.ra, switch - 3600)
        # the earliest sample of the *current* window after the login (older than 6 h on purpose)
        self.sample(con, self.ra, NOW - 10 * 3600, seven_pct=20.0, seven_reset=seven_reset)
        self.request(con, self.ra, NOW - 5 * 3600)  # the only request between t0 and t1 -> $1.00
        self.sample(con, self.ra, NOW - 60, seven_pct=50.0, seven_reset=seven_reset)
        con.commit()
        a = self.view()["accounts"][0]
        self.assertAlmostEqual(a["seven_day"]["usd_per_pct"], 0.03, places=2)  # $1 / 30 points

    def test_delta_below_5_is_null(self):
        con = self.connect()
        seven_reset = NOW + 86400
        self.sample(con, self.ra, NOW - 3600, seven_pct=10.0, seven_reset=seven_reset)
        self.request(con, self.ra, NOW - 1800)  # $1.00
        self.sample(con, self.ra, NOW - 60, seven_pct=13.0, seven_reset=seven_reset)
        con.commit()
        self.assertIsNone(self.view()["accounts"][0]["seven_day"]["usd_per_pct"])  # 3 points < 5

    def test_only_the_current_window_reset_counts(self):
        con = self.connect()
        old_reset = NOW - 3600
        new_reset = NOW + 86400
        self.sample(con, self.ra, NOW - 7200, seven_pct=30.0, seven_reset=old_reset)
        self.request(con, self.ra, NOW - 5400)  # the old window's request must not count
        self.sample(con, self.ra, NOW - 3600, seven_pct=10.0, seven_reset=new_reset)
        self.request(con, self.ra, NOW - 1800)  # $1 in the current window
        self.sample(con, self.ra, NOW - 60, seven_pct=40.0, seven_reset=new_reset)
        con.commit()
        a = self.view()["accounts"][0]
        self.assertAlmostEqual(a["seven_day"]["usd_per_pct"], 0.03, places=2)  # from 10 %, not the old window

    def test_zero_usd_is_null(self):
        con = self.connect()
        seven_reset = NOW + 86400
        self.sample(con, self.ra, NOW - 3600, seven_pct=10.0, seven_reset=seven_reset)
        self.sample(con, self.ra, NOW - 60, seven_pct=40.0, seven_reset=seven_reset)
        con.commit()
        self.assertIsNone(self.view()["accounts"][0]["seven_day"]["usd_per_pct"])  # no requests -> no spend


class TurnUsd(unittest.TestCase):
    def _session(self):
        return {"model": None, "context_tokens": None, "last_activity": None, "cache_ttl_s": None,
                "cache_expires_at": None, "rewrite_usd": None, "turn_usd": None}

    def test_turn_usd_is_the_cache_read_price(self):
        s = self._session()
        live._apply_info(s, {"model": "claude-opus-5-5", "context": 335000, "ts": NOW, "ttl": 3600})
        self.assertAlmostEqual(s["turn_usd"], 0.07, places=2)   # 335000 x $0.20 / 1e6
        self.assertAlmostEqual(s["rewrite_usd"], 2.68, places=2)  # cache-write price, unchanged

    def test_turn_usd_null_without_a_model(self):
        s = self._session()
        live._apply_info(s, {"model": None, "context": 335000, "ts": NOW, "ttl": 3600})
        self.assertIsNone(s["turn_usd"])
        self.assertIsNone(s["rewrite_usd"])


class AccountGroups(Base):
    def test_groups_sum_to_window_usd_and_order(self):
        self.write_projects({"groups": {"acme": {"label": "Acme"}, "globex": {"label": "Globex"}},
                             "rules": [{"match": "-acme", "tenant": "acme"},
                                       {"match": "-globex", "tenant": "globex"}]})
        con = self.connect()
        five_reset = NOW + 3600
        seven_reset = NOW + 2 * 86400
        self.sample(con, self.ra, NOW - 60, five_pct=10.0, five_reset=five_reset,
                    seven_pct=20.0, seven_reset=seven_reset)
        for i in range(2):
            self.request(con, self.ra, NOW - 3600 - i, project_dir="-acme")   # $2 total
        self.request(con, self.ra, NOW - 3600, project_dir="-globex")          # $1
        for i in range(5):
            self.request(con, self.ra, NOW - 3600 - i, project_dir="-other")   # $5, unassigned
        con.commit()
        a = self.view()["accounts"][0]
        self.assertAlmostEqual(a["five_hour"]["window_usd"], 8.0, places=2)
        self.assertAlmostEqual(a["seven_day"]["window_usd"], 8.0, places=2)
        self.assertEqual([g["id"] for g in a["groups"]], ["acme", "globex", "unassigned"])
        by = {g["id"]: g for g in a["groups"]}
        self.assertAlmostEqual(by["acme"]["seven_day_usd"], 2.0, places=2)
        self.assertAlmostEqual(by["globex"]["seven_day_usd"], 1.0, places=2)
        self.assertAlmostEqual(by["unassigned"]["seven_day_usd"], 5.0, places=2)
        self.assertAlmostEqual(sum(g["five_hour_usd"] for g in a["groups"]), a["five_hour"]["window_usd"], places=2)
        self.assertAlmostEqual(sum(g["seven_day_usd"] for g in a["groups"]), a["seven_day"]["window_usd"], places=2)

    def test_tenants_with_zero_spend_are_omitted(self):
        self.write_projects({"rules": [{"match": "-acme", "tenant": "acme"},
                                       {"match": "-empty", "tenant": "empty"}]})
        con = self.connect()
        self.sample(con, self.ra, NOW - 60, five_pct=10.0, five_reset=NOW + 3600,
                    seven_pct=20.0, seven_reset=NOW + 2 * 86400)
        self.request(con, self.ra, NOW - 1800, project_dir="-acme", input=250000)  # $1
        self.request(con, self.ra, NOW - 1800, project_dir="-empty", input=0)      # $0
        con.commit()
        a = self.view()["accounts"][0]
        self.assertEqual([g["id"] for g in a["groups"]], ["acme"])


class ClearHandoff(Base):
    def test_clear_handoff_reflects_resume_and_is_cached_per_cwd(self):
        cfg = os.path.dirname(self.ra)
        sdir = os.path.join(cfg, "sessions")
        for pid, sid, cwd in ((1001, "a", "/x/shared"), (1002, "b", "/x/shared"), (1003, "c", "/x/other")):
            with open(os.path.join(sdir, "%d.json" % pid), "w", encoding="utf-8") as fh:
                json.dump({"pid": pid, "sessionId": sid, "cwd": cwd, "status": "busy"}, fh)
        seen = []

        def fake_active(cwd):
            seen.append(cwd)
            return cwd == "/x/shared"

        with mock.patch.object(live.resume, "active_for", side_effect=fake_active):
            sessions = {s["session_id"]: s for s in live.view(now=NOW)["sessions"]}
        self.assertTrue(sessions["a"]["clear_handoff"])
        self.assertTrue(sessions["b"]["clear_handoff"])
        self.assertFalse(sessions["c"]["clear_handoff"])
        self.assertEqual(sorted(seen), ["/x/other", "/x/shared"])  # once per distinct cwd


class Demo(unittest.TestCase):
    def test_demo_has_the_new_fields_and_neutral_names(self):
        v = live.demo_view(now=NOW)
        for s in v["sessions"]:
            self.assertIn("turn_usd", s)
            self.assertIn("clear_handoff", s)
        self.assertTrue(any(s["turn_usd"] is not None for s in v["sessions"]))
        self.assertTrue(any(s["clear_handoff"] for s in v["sessions"]))
        self.assertTrue(any(not s["clear_handoff"] for s in v["sessions"]))
        ppc = [a["seven_day"].get("usd_per_pct") for a in v["accounts"]]
        self.assertTrue(any(x is not None for x in ppc))
        self.assertTrue(any(x is None for x in ppc))
        for a in v["accounts"]:
            self.assertAlmostEqual(sum(g["five_hour_usd"] for g in a["groups"]),
                                   a["five_hour"]["window_usd"], places=2)
            self.assertAlmostEqual(sum(g["seven_day_usd"] for g in a["groups"]),
                                   a["seven_day"]["window_usd"], places=2)
        blob = json.dumps(v)
        for bad in ("pretty", "daf", "Pretty", "DAF"):
            self.assertNotIn(bad, blob)


if __name__ == "__main__":
    unittest.main()
