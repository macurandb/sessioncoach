"""`live --json` decision fields: turn_usd, usd_per_pct, accounts[].groups, clear_handoff and the
demo data. Everything runs on a temp HOME and data dir; the metrics database is built by hand so the
sample spans, the login switch, window resets and per-tenant sums are checked exactly."""
import datetime as dt
import glob
import json
import os
import re
import unittest
from unittest import mock

import isolate

from sessioncoach import limits, live, store

NOW = 1_800_000_000


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Base(unittest.TestCase):
    def setUp(self):
        self.home = isolate.claude_home(self)
        self.data = os.path.join(self.home, ".sessioncoach")
        os.makedirs(self.data, exist_ok=True)
        # the view opens paths.db_path(), so pin the data dir explicitly (and drop the legacy
        # KIT_DATA_DIR a shell may still carry) instead of relying on the HOME fallback.
        os.environ["SESSIONCOACH_DATA_DIR"] = self.data
        os.environ.pop("KIT_DATA_DIR", None)
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

    def iso_at(self, epoch_s, micros=0):
        """An ISO ts at `epoch_s` plus `micros`, at millisecond precision, as the transcript rows use."""
        return dt.datetime.fromtimestamp(epoch_s + micros / 1e6, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def request_ts(self, con, root, ts, project_dir="-a", model="claude-opus-5-5", input=250000):
        store.add_request(con, {"project_dir": project_dir, "file": "f.jsonl", "msg_id": "m-%s" % ts,
                                "request_id": "r-%s" % ts, "session_id": "s", "kind": "main",
                                "ts": ts, "model": model, "input": input, "output": 0,
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

    def test_five_hour_window(self):
        con = self.connect()
        five_reset = NOW + 3600
        self.sample(con, self.ra, NOW - 3600, five_pct=10.0, five_reset=five_reset)
        self.request(con, self.ra, NOW - 1800)  # $1.00
        self.sample(con, self.ra, NOW - 60, five_pct=40.0, five_reset=five_reset)
        con.commit()
        a = self.view()["accounts"][0]
        self.assertAlmostEqual(a["five_hour"]["usd_per_pct"], 0.03, places=2)  # $1 / 30 points

    def test_single_sample_is_null(self):
        con = self.connect()
        seven_reset = NOW + 86400
        self.sample(con, self.ra, NOW - 60, seven_pct=40.0, seven_reset=seven_reset)
        self.request(con, self.ra, NOW - 1800)  # $1, but there is no earlier sample to span
        con.commit()
        self.assertIsNone(self.view()["accounts"][0]["seven_day"]["usd_per_pct"])

    def test_strict_lo_at_millisecond_precision(self):
        con = self.connect()
        seven_reset = NOW + 86400
        t0 = NOW - 3600
        self.sample(con, self.ra, t0, seven_pct=10.0, seven_reset=seven_reset)
        self.sample(con, self.ra, NOW - 60, seven_pct=40.0, seven_reset=seven_reset)
        self.request_ts(con, self.ra, self.iso_at(t0))          # exactly at t0: excluded (strict lo)
        self.request_ts(con, self.ra, self.iso_at(t0, 1000))    # t0 + 1 ms: included
        con.commit()
        a = self.view()["accounts"][0]
        self.assertAlmostEqual(a["seven_day"]["usd_per_pct"], 0.03, places=2)  # $1, not $2

    def test_a_bad_earliest_sample_is_skipped(self):
        con = self.connect()
        seven_reset = NOW + 86400
        self.sample(con, self.ra, NOW - 7200, seven_pct="50%", seven_reset=seven_reset)  # bad: skipped
        self.sample(con, self.ra, NOW - 3600, seven_pct=10.0, seven_reset=seven_reset)   # earliest valid
        self.request(con, self.ra, NOW - 1800)  # $1 between t0 and t1
        self.sample(con, self.ra, NOW - 60, seven_pct=40.0, seven_reset=seven_reset)
        con.commit()
        a = self.view()["accounts"][0]
        self.assertAlmostEqual(a["seven_day"]["usd_per_pct"], 0.03, places=2)  # from 10 %, not the bad row

    def test_bad_sample_values_do_not_raise(self):
        con = self.connect()
        seven_reset = NOW + 86400
        self.sample(con, self.ra, NOW - 3600, seven_pct=10.0, seven_reset=seven_reset)
        self.request(con, self.ra, NOW - 1800)  # $1
        for bad in ("50%", '{"pct": 50}', float("inf"), float("nan"), True):
            con.execute("INSERT INTO limits_samples (ts, session_id, root, seven_pct, seven_reset) "
                        "VALUES (?,?,?,?,?)", (NOW - 60, None, self.ra, bad, seven_reset))
        con.commit()
        a = self.view()["accounts"][0]  # must not raise
        self.assertIsNone(a["seven_day"]["usd_per_pct"])  # a bad latest -> null


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

    def test_turn_usd_long_prompt_tier(self):
        s = self._session()
        live._apply_info(s, {"model": "claude-haiku-5-5", "context": 200000, "ts": NOW, "ttl": 3600})
        # haiku-5-5 above LONG_PROMPT pays the long-prompt tier: cache read 0.05, cache write 1.0
        self.assertAlmostEqual(s["turn_usd"], 0.01, places=2)     # 200000 x 0.05 / 1e6
        self.assertAlmostEqual(s["rewrite_usd"], 0.20, places=2)  # 200000 x 1.0 / 1e6


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
    def test_clear_handoff_reflects_resume_and_idle_guard_per_cfg_and_cwd(self):
        cfg = os.path.dirname(self.ra)
        sdir = os.path.join(cfg, "sessions")
        for pid, sid, cwd in ((1001, "a", "/x/shared"), (1002, "b", "/x/shared"), (1003, "c", "/x/other")):
            with open(os.path.join(sdir, "%d.json" % pid), "w", encoding="utf-8") as fh:
                json.dump({"pid": pid, "sessionId": sid, "cwd": cwd, "status": "busy"}, fh)
        seen = []

        def fake_active(cwd, cfg_dir=None):
            seen.append((cfg_dir, cwd))
            return cwd == "/x/shared"

        with mock.patch.object(live.resume, "active_for", side_effect=fake_active), \
                mock.patch.object(live.idleguard, "enabled", return_value=True):
            sessions = {s["session_id"]: s for s in live.view(now=NOW)["sessions"]}
        self.assertTrue(sessions["a"]["clear_handoff"])
        self.assertTrue(sessions["b"]["clear_handoff"])
        self.assertFalse(sessions["c"]["clear_handoff"])
        # once per distinct (config dir, cwd); every session here shares the one config dir
        self.assertEqual({cwd for _cfg, cwd in seen}, {"/x/other", "/x/shared"})

    def test_clear_handoff_requires_the_idle_guard(self):
        cfg = os.path.dirname(self.ra)
        sdir = os.path.join(cfg, "sessions")
        with open(os.path.join(sdir, "1001.json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": 1001, "sessionId": "a", "cwd": "/x/shared", "status": "busy"}, fh)
        with mock.patch.object(live.resume, "active_for", return_value=True), \
                mock.patch.object(live.idleguard, "enabled", return_value=False):
            sessions = {s["session_id"]: s for s in live.view(now=NOW)["sessions"]}
        self.assertFalse(sessions["a"]["clear_handoff"])  # a resume hook alone is not a free handoff


class StaleSamples(Base):
    """Idle sessions re-record their last response's pct with a fresh ts, so `limits_samples` holds
    interleaved stale rows (same ts, lower pct). The current window's real pct never decreases, so
    every read takes the MAX over that reset's samples — limits, accounts, usd_per_pct and pace."""

    def test_stale_rows_do_not_flap_limits_or_accounts_pct(self):
        con = self.connect()
        seven_reset = NOW + 86400
        # real data: at the same second, rows with seven_pct 54, 53, 50, 44, 43, 38, 34
        for pct in (54.0, 53.0, 50.0, 44.0, 43.0, 38.0, 34.0):
            self.sample(con, self.ra, NOW - 60, seven_pct=pct, seven_reset=seven_reset)
        con.commit()
        v = self.view()
        self.assertEqual(v["limits"]["seven_day"]["pct"], 54.0)
        self.assertEqual(v["limits"]["seven_day"]["resets_at"], seven_reset)
        self.assertEqual(v["accounts"][0]["seven_day"]["pct"], 54.0)

    def test_stale_rows_do_not_flap_usd_per_pct(self):
        con = self.connect()
        seven_reset = NOW + 86400
        self.sample(con, self.ra, NOW - 3600, seven_pct=20.0, seven_reset=seven_reset)
        self.request(con, self.ra, NOW - 1800)  # $1 between the two samples
        self.sample(con, self.ra, NOW - 60, seven_pct=50.0, seven_reset=seven_reset)
        for pct in (50.0, 44.0, 38.0, 34.0):  # stale redraws re-recorded later, same reset
            self.sample(con, self.ra, NOW - 30, seven_pct=pct, seven_reset=seven_reset)
        con.commit()
        a = self.view()["accounts"][0]
        self.assertAlmostEqual(a["seven_day"]["usd_per_pct"], 0.03, places=2)  # $1 / 30 points, not 14

    def test_an_older_resets_rows_written_now_are_ignored(self):
        con = self.connect()
        old_reset, new_reset = NOW - 3600, NOW + 86400
        self.sample(con, self.ra, NOW - 60, seven_pct=99.0, seven_reset=old_reset)
        self.sample(con, self.ra, NOW - 60, seven_pct=40.0, seven_reset=new_reset)
        con.commit()
        a = self.view()["accounts"][0]
        self.assertEqual(a["seven_day"]["pct"], 40.0)
        self.assertEqual(a["seven_day"]["resets_at"], new_reset)

    def test_pace_stale_rows_do_not_change_the_eta(self):
        con = self.connect()
        reset = NOW + 3600
        self.sample(con, self.ra, NOW - 1200, five_pct=40.0, five_reset=reset)
        self.sample(con, self.ra, NOW - 60, five_pct=60.0, five_reset=reset)
        con.commit()
        eta = limits.pace(con, "five_hour", reset, NOW, 60.0, root=self.ra)
        self.assertIsNotNone(eta)
        for pct in (34.0, 38.0, 44.0):  # stale redraws re-recorded later: the eta must not move
            self.sample(con, self.ra, NOW - 30, five_pct=pct, five_reset=reset)
        con.commit()
        self.assertEqual(limits.pace(con, "five_hour", reset, NOW, 60.0, root=self.ra), eta)

    def test_pace_a_real_rise_still_projects(self):
        con = self.connect()
        reset = NOW + 3600
        self.sample(con, self.ra, NOW - 1200, five_pct=40.0, five_reset=reset)
        self.sample(con, self.ra, NOW - 60, five_pct=60.0, five_reset=reset)
        con.commit()
        eta = limits.pace(con, "five_hour", reset, NOW, 60.0, root=self.ra)
        self.assertIsNotNone(eta)
        self.assertLess(eta, reset)

    def test_pace_with_only_stale_rows_gives_no_false_eta(self):
        con = self.connect()
        reset = NOW + 3600
        # the genuine rise happened more than 30 min ago (outside the lookback); since then only
        # stale low rows land inside it, so the pace must not fake a "hits 100 %" projection.
        self.sample(con, self.ra, NOW - 3600, five_pct=73.0, five_reset=reset)
        for ago in (600, 400, 200):
            self.sample(con, self.ra, NOW - ago, five_pct=34.0, five_reset=reset)
        con.commit()
        self.assertIsNone(limits.pace(con, "five_hour", reset, NOW, 73.0, root=self.ra))


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


class NeutralNames(unittest.TestCase):
    """No real client or project name leaks into the docs, the README or live.py's docstring.

    Word-boundary and case-sensitive: `pretty` and `Pretty` are checked separately, and the
    capitalised names (Pretty, ITTI, Delfos, NewMe) only match that exact spelling.
    """
    WORDS = ("pretty", "Pretty", "phoenix", "kraken", "echo-last", "ITTI", "Delfos", "NewMe")

    def test_no_real_client_names_in_docs_and_live(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in self.WORDS) + r")\b")
        files = [os.path.join(root, "README.md"), os.path.join(root, "sessioncoach", "live.py")]
        files += sorted(glob.glob(os.path.join(root, "docs", "*.md")))
        hits = []
        for path in files:
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    m = pattern.search(line)
                    if m:
                        hits.append("%s:%d: %r" % (path, lineno, m.group(0)))
        self.assertEqual(hits, [], "real client names found:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
