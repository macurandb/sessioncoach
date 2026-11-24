"""The `live --json` additions: accounts, groups, lang, and the session account/group keys.

Two transcript roots (two accounts), a groups block in projects.json, and a metrics database built
by hand, so the per-root samples, window sums at their boundaries, projections and ordering are
checked exactly. Everything runs against a temp HOME and data dir.
"""
import datetime as dt
import hashlib
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
        self.rb = os.path.join(self.home, "acct-b", "projects")
        for r in (self.ra, self.rb):
            os.makedirs(os.path.join(os.path.dirname(r), "sessions"))
            os.makedirs(r)
        os.environ["SESSIONCOACH_PROJECTS_DIRS"] = os.pathsep.join([self.ra, self.rb])
        self.addCleanup(os.environ.pop, "SESSIONCOACH_PROJECTS_DIRS", None)
        p = mock.patch.object(live, "_pid_alive", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def write_accounts(self, accounts):
        with open(os.path.join(self.data, "accounts.json"), "w", encoding="utf-8") as fh:
            json.dump({"accounts": accounts}, fh)

    def write_projects(self, doc):
        with open(os.path.join(self.data, "projects.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def write_alerts(self, doc):
        with open(os.path.join(self.data, "alerts.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def connect(self):
        con = store.connect(self.db)
        self.addCleanup(con.close)
        return con

    def view(self):
        return live.view(now=NOW)

    def sample(self, con, root, five_pct=None, five_reset=None, seven_pct=None, seven_reset=None):
        con.execute("INSERT INTO limits_samples (ts, session_id, root, five_pct, five_reset, seven_pct, seven_reset) "
                    "VALUES (?,?,?,?,?,?,?)", (NOW - 120, None, root, five_pct, five_reset, seven_pct, seven_reset))

    def request(self, con, root, ts, project_dir="-a", model="claude-opus-5-5", input=250000):
        store.add_request(con, {"project_dir": project_dir, "file": "f.jsonl", "msg_id": "m%d" % ts, "request_id": "r%d" % ts,
                                "session_id": "s", "kind": "main", "ts": iso(ts), "model": model,
                                "input": input, "output": 0, "cache_read": 0, "cache_write": 0,
                                "cache_write_5m": 0, "cache_write_1h": 0, "root": root})


class AccountInfo(Base):
    def test_roots_mapping_when_the_login_is_unknown(self):
        self.write_accounts({"personal": {"label": "Personal · Max", "org_hash": "h1", "roots": [self.ra]}})
        key, label, plan = live._account_info(self.ra)
        self.assertEqual((key, label), ("personal", "Personal · Max"))
        self.assertIsNone(plan)  # no .claude.json to detect a plan

    def test_org_hash_mapping(self):
        org = "org-b"
        h = hashlib.sha256(org.encode()).hexdigest()[:12]
        self.write_accounts({"team": {"label": "Team", "org_hash": h}})
        cfg = os.path.dirname(self.rb)
        with open(os.path.join(cfg, ".claude.json"), "w", encoding="utf-8") as fh:
            json.dump({"oauthAccount": {"organizationUuid": org, "organizationType": "claude_team"}}, fh)
        key, label, plan = live._account_info(self.rb)
        self.assertEqual((key, label, plan), ("team", "Team", "Team"))

    def test_falls_back_to_a_plan_word_then_the_basename(self):
        self.write_accounts({})
        cfg = os.path.dirname(self.ra)
        with open(os.path.join(cfg, ".claude.json"), "w", encoding="utf-8") as fh:
            json.dump({"oauthAccount": {"organizationUuid": "org-a", "organizationType": "claude_max"}}, fh)
        key, label, plan = live._account_info(self.ra)
        self.assertTrue(key.startswith("auto:"))
        self.assertEqual((label, plan), ("Max", "Max"))
        key, label, plan = live._account_info(os.path.join(self.home, "plain", "projects"))
        self.assertEqual((key, label, plan), ("unknown", "plain", None))

    def test_never_uses_email_or_name(self):
        self.write_accounts({})
        cfg = os.path.dirname(self.ra)
        with open(os.path.join(cfg, ".claude.json"), "w", encoding="utf-8") as fh:
            json.dump({"oauthAccount": {"organizationUuid": "org-a", "organizationType": "claude_team",
                                        "emailAddress": "person@example.com", "displayName": "A Person"}}, fh)
        _key, label, _plan = live._account_info(self.ra)
        self.assertNotIn("@", label)
        self.assertNotIn("Person", label)


class LoginSwitch(Base):
    """A config dir that switched accounts (/login) shows the account logged in now, counted from the switch."""

    def login(self, org, org_type):
        cfg = os.path.dirname(self.ra)
        with open(os.path.join(cfg, ".claude.json"), "w", encoding="utf-8") as fh:
            json.dump({"oauthAccount": {"organizationUuid": org, "organizationType": org_type}}, fh)
        return hashlib.sha256(org.encode()).hexdigest()[:12]

    def test_logged_in_account_wins_over_a_stale_roots_mapping(self):
        h = self.login("org-team", "claude_team")
        self.write_accounts({"personal": {"label": "Personal", "org_hash": "other", "roots": [self.ra]},
                             "team": {"label": "Team DAF", "org_hash": h}})
        self.assertEqual(live._account_info(self.ra), ("team", "Team DAF", "Team"))

    def test_roots_mapping_skipped_when_logged_into_another_account(self):
        self.login("org-team", "claude_team")
        self.write_accounts({"personal": {"label": "Personal", "org_hash": "other", "roots": [self.ra]}})
        key, label, plan = live._account_info(self.ra)
        self.assertTrue(key.startswith("auto:"))
        self.assertEqual((label, plan), ("Team", "Team"))

    def test_spend_and_samples_count_from_the_switch(self):
        self.login("org-team", "claude_team")
        con = self.connect()
        switch = NOW - 3 * 86400
        seen = dt.datetime.fromtimestamp(switch, dt.timezone.utc).isoformat()
        con.execute("INSERT INTO accounts_seen (root, seen_at, org_hash, org_type) VALUES (?,?,?,?)",
                    (self.ra, seen, "x", "claude_team"))
        seven_reset = NOW + 2 * 86400  # the 7-day window started 5 days ago, before the switch
        self.sample(con, self.ra, seven_pct=40.0, seven_reset=seven_reset)
        self.request(con, self.ra, switch - 3600)  # the previous account's request
        self.request(con, self.ra, switch + 3600)  # this account's
        today = dt.date.fromtimestamp(NOW).isoformat()
        for usd, login_at in ((5.0, seen), (7.0, "2026-01-01T00:00:00+00:00")):
            con.execute("INSERT INTO cost_slices (day, project_dir, session_id, model, usd, estimated, root, login_at) "
                        "VALUES (?,?,?,?,?,?,?,?)", (today, "-a", "s", "opus-5-5", usd, 0, self.ra, login_at))
        # a sample taken before the switch belongs to the previous account
        con.execute("INSERT INTO limits_samples (ts, session_id, root, five_pct, five_reset) VALUES (?,?,?,?,?)",
                    (NOW - 60, None, self.ra, 99.0, NOW + 3600))
        con.commit()
        a = self.view()["accounts"][0]
        self.assertAlmostEqual(a["seven_day"]["window_usd"], 1.0, places=2)  # only the request after the switch
        self.assertEqual(a["today_usd"], 5.0)  # only today's slices of the current login
        con.execute("UPDATE accounts_seen SET seen_at = ?",
                    (dt.datetime.fromtimestamp(NOW - 30, dt.timezone.utc).isoformat(),))
        con.commit()
        self.assertIsNone(self.view()["accounts"][0]["five_hour"]["pct"])  # sample older than the login


class Plan(unittest.TestCase):
    def test_plan_word(self):
        self.assertEqual(live._plan_word({"org_type": "claude_max"}), "Max")
        self.assertEqual(live._plan_word({"org_type": "claude_team"}), "Team")
        self.assertEqual(live._plan_word({"org_type": "claude_pro"}), "Pro")
        self.assertIsNone(live._plan_word({}))
        self.assertIsNone(live._plan_word({"org_type": "api"}))

    def test_projected_pct(self):
        # resets in 4 days: 3 of the 7 days elapsed -> 51% * 7 / 3 = 119%
        self.assertAlmostEqual(live._projected_pct(51.0, NOW + 4 * 86400, NOW), 119.0, places=0)
        self.assertIsNone(live._projected_pct(51.0, NOW + 7 * 86400 - 3600, NOW))  # < 6 h elapsed
        self.assertIsNone(live._projected_pct(None, NOW + 86400, NOW))


class View(Base):
    def test_accounts_two_roots_and_groups(self):
        self.write_accounts({
            "personal": {"label": "Personal · Max", "org_hash": "h1", "roots": [self.ra]},
            "team": {"label": "Team", "org_hash": "h2", "roots": [self.rb]},
        })
        self.write_projects({"groups": {"pretty": {"label": "Pretty"}},
                             "rules": [{"match": "-shop", "project": "shop", "tenant": "pretty"}]})
        con = self.connect()
        self.sample(con, self.ra, five_pct=12.0, five_reset=NOW + 3600, seven_pct=51.0, seven_reset=NOW + 86400 * 2)
        self.sample(con, self.rb, five_pct=3.0, five_reset=NOW + 5400, seven_pct=20.0, seven_reset=NOW + 86400)
        today = dt.date.fromtimestamp(NOW).isoformat()
        con.execute("INSERT INTO cost_slices (day, project_dir, session_id, model, usd, estimated, root) "
                    "VALUES (?,?,?,?,?,?,?)", (today, "-shop", "s1", "opus-5-5", 12.40, 0, self.ra))
        con.execute("INSERT INTO cost_slices (day, project_dir, session_id, model, usd, estimated, root) "
                    "VALUES (?,?,?,?,?,?,?)", (today, "-api", "s2", "sonnet-5-5", 3.10, 0, self.rb))
        con.commit()
        v = self.view()
        by_key = {a["key"]: a for a in v["accounts"]}
        self.assertEqual(set(by_key), {"personal", "team"})
        p = by_key["personal"]
        self.assertEqual(p["label"], "Personal · Max")
        self.assertEqual(p["today_usd"], 12.40)
        self.assertEqual(p["five_hour"]["pct"], 12.0)
        self.assertEqual(p["five_hour"]["resets_at"], NOW + 3600)
        self.assertEqual(p["seven_day"]["pct"], 51.0)
        self.assertEqual(by_key["team"]["label"], "Team")
        groups = {g["id"]: g for g in v["groups"]}
        self.assertEqual(groups["pretty"]["label"], "Pretty")
        self.assertEqual(groups["pretty"]["today_usd"], 12.40)
        self.assertEqual(groups["unassigned"]["today_usd"], 3.10)
        self.assertEqual([g["id"] for g in v["groups"]], ["pretty", "unassigned"])

    def test_window_sums_at_the_boundaries(self):
        self.write_accounts({"personal": {"label": "P", "roots": [self.ra]}})
        con = self.connect()
        five_reset = NOW + 3600
        self.sample(con, self.ra, five_pct=10.0, five_reset=five_reset)
        self.request(con, self.ra, five_reset - 5 * 3600 + 10)   # inside the 5-hour window
        self.request(con, self.ra, five_reset - 5 * 3600 - 10)   # just outside
        con.commit()
        a = self.view()["accounts"][0]
        self.assertAlmostEqual(a["five_hour"]["window_usd"], 1.0, places=2)  # only the inside request

    def test_eta_passthrough(self):
        self.write_accounts({"personal": {"label": "P", "roots": [self.ra]}})
        con = self.connect()
        # a rising pair of samples so `limits.pace` projects a real epoch
        self.sample(con, self.ra, five_pct=20.0, five_reset=NOW + 3600)
        con.execute("UPDATE limits_samples SET ts = ?", (NOW - 1200,))
        self.sample(con, self.ra, five_pct=50.0, five_reset=NOW + 3600)
        con.commit()
        a = self.view()["accounts"][0]
        self.assertIsNotNone(a["five_hour"]["eta"])

    def test_groups_ordering_unassigned_last(self):
        self.write_projects({"groups": {"pretty": {"label": "Pretty"}, "globex": {"label": "Globex"}},
                             "rules": [{"match": "-pretty", "tenant": "pretty"},
                                       {"match": "-globex", "tenant": "globex"}]})
        con = self.connect()
        today = dt.date.fromtimestamp(NOW).isoformat()
        for pdir, usd in (("-pretty", 1.0), ("-globex", 5.0), ("-unassigned", 99.0)):
            con.execute("INSERT INTO cost_slices (day, project_dir, session_id, model, usd, estimated, root) "
                        "VALUES (?,?,?,?,?,?,?)", (today, pdir, "s", "opus-5-5", usd, 0, self.ra))
        con.commit()
        v = self.view()
        self.assertEqual([g["id"] for g in v["groups"]], ["globex", "pretty", "unassigned"])

    def test_lang(self):
        self.write_alerts({"lang": "es"})
        self.assertEqual(self.view()["lang"], "es")

    def test_session_gets_account_and_group(self):
        self.write_accounts({"personal": {"label": "Personal · Max", "roots": [self.ra]}})
        self.write_projects({"groups": {"pretty": {"label": "Pretty"}},
                             "rules": [{"match": "-shop", "project": "shop", "tenant": "pretty"}]})
        cfg = os.path.dirname(self.ra)
        with open(os.path.join(cfg, "sessions", "1001.json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": 1001, "sessionId": "sid-a", "cwd": "/Users/x/code/shop"}, fh)
        os.makedirs(os.path.join(self.ra, "-shop"))
        with open(os.path.join(self.ra, "-shop", "sid-a.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": iso(NOW - 60),
                                 "message": {"model": "claude-opus-5-5",
                                             "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                                                       "cache_creation_input_tokens": 0}}}) + "\n")
        s = {x["session_id"]: x for x in self.view()["sessions"]}["sid-a"]
        self.assertEqual(s["account"], "personal")
        self.assertEqual(s["group"], "pretty")

    def test_metrics_error_degrades_accounts_and_groups(self):
        self.write_accounts({})
        con = self.connect()
        con.execute("DROP TABLE alert_events")
        con.execute("CREATE TABLE alert_events (source TEXT)")
        con.commit()
        v = self.view()
        self.assertEqual(v["accounts"], [])
        self.assertEqual(v["groups"], [])


class Identity(Base):
    def test_account_ids_are_unique_stable_12_hex(self):
        con = self.connect()
        self.sample(con, self.ra, five_pct=10.0, five_reset=NOW + 3600)
        self.sample(con, self.rb, five_pct=20.0, five_reset=NOW + 3600)
        con.commit()
        ids = [a["id"] for a in self.view()["accounts"]]
        self.assertEqual(len(set(ids)), 2)  # two roots -> two unique ids
        self.assertTrue(all(len(i) == 12 and all(c in "0123456789abcdef" for c in i) for i in ids))

    def test_session_account_id_comes_from_the_config_dir_not_the_transcript(self):
        self.write_accounts({"personal": {"label": "P", "roots": [self.ra]}})
        cfg = os.path.dirname(self.ra)
        with open(os.path.join(cfg, "sessions", "1001.json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": 1001, "sessionId": "sid-x", "cwd": "/Users/x/code"}, fh)
        s = {x["session_id"]: x for x in self.view()["sessions"]}["sid-x"]  # no transcript at all
        self.assertEqual(s["account_id"], live._identity(cfg))
        self.assertIsNone(s["account"])  # no transcript -> no root -> no account name

    def test_session_account_id_matches_the_account_card(self):
        self.write_accounts({"personal": {"label": "P", "roots": [self.ra]}})
        self.write_projects({"rules": [{"match": "-shop", "project": "shop", "tenant": "pretty"}]})
        cfg = os.path.dirname(self.ra)
        with open(os.path.join(cfg, "sessions", "1001.json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": 1001, "sessionId": "sid-a", "cwd": "/Users/x/code/shop"}, fh)
        os.makedirs(os.path.join(self.ra, "-shop"))
        with open(os.path.join(self.ra, "-shop", "sid-a.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": iso(NOW - 60),
                                 "message": {"model": "claude-opus-5-5",
                                             "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                                                       "cache_creation_input_tokens": 0}}}) + "\n")
        con = self.connect()
        self.sample(con, self.ra, five_pct=10.0, five_reset=NOW + 3600)
        con.commit()
        v = self.view()
        a = v["accounts"][0]
        s = {x["session_id"]: x for x in v["sessions"]}["sid-a"]
        self.assertEqual(a["id"], live._identity(cfg))
        self.assertEqual(s["account_id"], a["id"])


class Eta(Base):
    def rising(self, con, root, reset, lo, hi):
        con.execute("INSERT INTO limits_samples (ts, session_id, root, five_pct, five_reset) VALUES (?,?,?,?,?)",
                    (NOW - lo, None, root, 20.0, reset))
        con.execute("INSERT INTO limits_samples (ts, session_id, root, five_pct, five_reset) VALUES (?,?,?,?,?)",
                    (NOW - hi, None, root, 50.0, reset))

    def test_adding_root_b_does_not_change_root_as_eta(self):
        self.write_accounts({"a": {"label": "A", "roots": [self.ra]}, "b": {"label": "B", "roots": [self.rb]}})
        con = self.connect()
        reset = NOW + 3600
        self.rising(con, self.ra, reset, 1200, 120)
        con.commit()
        ra_id = live._identity(os.path.dirname(os.path.normpath(self.ra)))
        eta_a = {a["id"]: a for a in self.view()["accounts"]}[ra_id]["five_hour"]["eta"]
        self.assertIsNotNone(eta_a)
        # root B shares the reset time but moves at a different pace: it must not shift A's eta
        self.rising(con, self.rb, reset, 1200, 120)
        con.commit()
        self.assertEqual({a["id"]: a for a in self.view()["accounts"]}[ra_id]["five_hour"]["eta"], eta_a)

    def test_samples_before_the_login_are_ignored_by_pace(self):
        self.write_accounts({"a": {"label": "A", "roots": [self.ra]}})
        con = self.connect()
        switch = NOW - 1800
        con.execute("INSERT INTO accounts_seen (root, seen_at, org_hash, org_type) VALUES (?,?,?,?)",
                    (self.ra, dt.datetime.fromtimestamp(switch, dt.timezone.utc).isoformat(), "x", "claude_team"))
        con.execute("INSERT INTO limits_samples (ts, session_id, root, five_pct, five_reset) VALUES (?,?,?,?,?)",
                    (NOW - 3600, None, self.ra, 10.0, NOW + 3600))  # before the login
        con.execute("INSERT INTO limits_samples (ts, session_id, root, five_pct, five_reset) VALUES (?,?,?,?,?)",
                    (NOW - 120, None, self.ra, 50.0, NOW + 3600))   # after the login
        con.commit()
        a = self.view()["accounts"][0]
        self.assertIsNone(a["five_hour"]["eta"])  # the pre-login baseline must not seed the pace


class WindowBoundaries(Base):
    def test_exact_upper_and_lower_bounds(self):
        self.write_accounts({"personal": {"label": "P", "roots": [self.ra]}})
        con = self.connect()
        five_reset = NOW + 3600
        self.sample(con, self.ra, five_pct=10.0, five_reset=five_reset)
        start = five_reset - 5 * 3600
        self.request(con, self.ra, start)    # exactly at the window start -> included
        self.request(con, self.ra, NOW)      # exactly at now -> included
        self.request(con, self.ra, NOW + 1)  # one second after now -> excluded
        con.commit()
        a = self.view()["accounts"][0]
        self.assertAlmostEqual(a["five_hour"]["window_usd"], 2.0, places=2)  # start + now, not now + 1


class Aggregation(Base):
    def test_window_rows_equal_the_per_row_weight(self):
        import random

        from sessioncoach import pricing
        con = self.connect()
        rng = random.Random(1)
        models = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
        rows = []
        for i in range(120):
            model = rng.choice(models)
            split = rng.choice([True, False])
            row = {"project_dir": "-p%d" % (i % 3), "root": self.ra, "model": model,
                   "input": rng.randint(0, 200000), "output": rng.randint(0, 5000),
                   "cache_read": rng.randint(0, 200000), "cache_write": rng.randint(0, 200000),
                   "cache_write_5m": rng.randint(0, 8000) if split else None,
                   "cache_write_1h": rng.randint(0, 8000) if split else None}
            ts = NOW - rng.randint(0, 3600)
            store.add_request(con, dict(row, file="f", msg_id="m%d" % i, request_id="r%d" % i,
                                        session_id="s", kind="main", ts=iso(ts)))
            rows.append((ts, row))
        con.commit()
        start = NOW - 3600
        agg_total = sum(w for _r, _p, w in live._window_rows(con, start, NOW, root=self.ra))
        per = sum(pricing.weight(r["model"], r) for ts, r in rows if start <= ts <= NOW)
        self.assertAlmostEqual(agg_total, per, places=6)


if __name__ == "__main__":
    unittest.main()
