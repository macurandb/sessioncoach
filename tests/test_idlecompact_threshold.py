"""CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS: is it set right (check), and would 100k pay off (idle-compact)? Synthetic data only."""
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate

from sessioncoach import analysis, cli, idlecompact, installer, keepctx, pricing, store

ENV = idlecompact.MIN_TOKENS_ENV
T0 = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
NOW = T0 + dt.timedelta(days=5)


def ts(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Parse(unittest.TestCase):
    def test_follows_the_documented_rules(self):
        self.assertEqual(idlecompact.parse_min_tokens("150000"), (150000, None))
        self.assertEqual(idlecompact.parse_min_tokens(" 100000 "), (100000, None))
        n, why = idlecompact.parse_min_tokens("50000")
        self.assertEqual(n, 100000)
        self.assertIn("raises it to 100k", why)
        for raw in ("100k", "0", "-5", "1e5", "", "\u0661\u0660\u0660\u0660\u0660\u0660"):
            n, why = idlecompact.parse_min_tokens(raw)
            self.assertIsNone(n, raw)
            self.assertIn("ignores it", why)


class Report(unittest.TestCase):
    def report(self, files, environ=None):
        return idlecompact.min_tokens_report(files, environ or {})

    def test_says_nothing_when_unset(self):
        self.assertEqual(self.report([("global", {"env": {}}), ("project", None), ("odd", {"env": "x"})]), (None, []))

    def test_a_good_value_in_settings(self):
        value, out = self.report([("global", {"env": {ENV: "100000"}})])
        self.assertEqual(value, 100000)
        self.assertEqual([w for w, _ in out], [False])
        self.assertIn("100k (global settings)", out[0][1])
        self.assertNotIn("VS Code", out[0][1])

    def test_only_in_the_shell_warns_that_vs_code_does_not_see_it(self):
        _value, out = self.report([], {ENV: "100000"})
        self.assertFalse(out[0][0])
        self.assertIn("VS Code extension does not read this shell", out[0][1])

    def test_ignored_raised_and_conflicting_values_warn(self):
        _v, out = self.report([("global", {"env": {ENV: "100k"}})])
        self.assertTrue(out[0][0])
        self.assertIn("global settings: '100k'", out[0][1])
        _v, out = self.report([("global", {"env": {ENV: 5000}})])
        self.assertIn("raises it", out[0][1])
        _v, out = self.report([("global", {"env": {ENV: "100000"}}), ("project", {"env": {ENV: "150000"}})])
        self.assertEqual([w for w, _ in out], [True])
        self.assertIn("different values (global settings 100k, project settings 150k)", out[0][1])

    def test_set_but_turned_off(self):
        _v, out = self.report([("global", {"env": {ENV: "100000", "DISABLE_AUTO_COMPACT": "1"}, "idleCompaction": False})],
                              {"DISABLE_COMPACT": "true", "DISABLE_AUTO_COMPACT": "0"})
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0][0])
        for what in ("idleCompaction is false in global settings", "DISABLE_AUTO_COMPACT is set in global settings",
                     "DISABLE_COMPACT is set in this shell's environment"):
            self.assertIn(what, out[0][1])
        self.assertNotIn("DISABLE_AUTO_COMPACT is set in this shell", out[0][1])


class Check(unittest.TestCase):
    def setUp(self):
        self.home = isolate.claude_home(self)
        os.environ["KIT_DATA_DIR"] = os.path.join(self.home, "data")
        os.environ.pop(ENV, None)
        self.cwd = os.path.join(self.home, "proj")
        os.makedirs(os.path.join(self.home, ".claude"))
        os.makedirs(self.cwd)

    def items(self):
        return [(s, d) for s, n, d in installer.check(cwd=self.cwd) if n == "idle summary threshold"]

    def write_settings(self, s):
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as fh:
            json.dump(s, fh)

    def test_silent_when_unset_and_guard_off_shown_with_the_guard(self):
        self.assertEqual(self.items(), [])
        installer.update_alerts(lambda a: a.setdefault("nudge", {}).update(block=True))
        (status, detail), = self.items()
        self.assertEqual(status, installer.OK)
        self.assertIn("not set: Claude Code's default, 200k", detail)

    def test_warns_never_fails(self):
        self.write_settings({"env": {ENV: "lots"}})
        (status, detail), = self.items()
        self.assertEqual(status, installer.WARN)
        self.assertIn("sessioncoach idle-compact", detail)
        self.write_settings({"env": {ENV: "100000"}})
        self.assertEqual([s for s, _ in self.items()], [installer.OK])


class View(unittest.TestCase):
    def setUp(self):
        isolate.claude_home(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.con = store.connect(os.path.join(self.tmp, "metrics.db"))
        self.n = 0

    def turn(self, sid, t, ctx, w1h=5000, model="claude-opus-5-5", kind="main"):
        self.n += 1
        store.add_request(self.con, {
            "project_dir": "-p", "file": sid + ".jsonl", "msg_id": "m%d" % self.n, "request_id": "r%d" % self.n,
            "session_id": sid, "kind": kind, "ts": ts(t), "model": model, "input": 10, "output": 100,
            "cache_read": max(0, ctx - 10 - w1h), "cache_write": w1h, "cache_write_1h": w1h, "cache_write_5m": 0})

    def pause(self, sid, start, pre, applied=False, gap_min=70, ret_write=None):
        """A turn at `pre` context, a pause of `gap_min`, then the return (summarised or re-written)."""
        self.turn(sid, start, pre)
        back = start + dt.timedelta(minutes=gap_min)
        if applied:
            store.add_compaction(self.con, {"project_dir": "-p", "session_id": sid, "ts": ts(back - dt.timedelta(minutes=10)),
                                            "trigger": "auto", "pre_tokens": pre, "post_tokens": 15000, "precomputed": 1})
            self.turn(sid, back, 20000, w1h=15000)
        else:
            self.turn(sid, back, pre + 2000, w1h=ret_write or pre)

    def view(self, **kw):
        return analysis.idle_compact_view(self.con, days=kw.pop("days", 30), now=kw.pop("now", NOW))

    def test_empty_database_keeps_the_default(self):
        v = self.view()
        self.assertEqual(v["verdict"], "keep")
        self.assertIsNone(v["covered"]["rate"])
        self.assertEqual(v["summary_tokens"], analysis.IDLE_SUMMARY_OUT)

    def test_rate_counts_from_the_first_summary_and_splits_by_the_context_before_the_pause(self):
        self.pause("old", T0 - dt.timedelta(days=2), 300000)  # before the feature: not in the rate
        for i in range(6):
            self.pause("c%d" % i, T0 + dt.timedelta(hours=3 * i), 300000, applied=i % 2 == 0)
        self.pause("b", T0 + dt.timedelta(days=1), 150000)
        self.pause("s", T0 + dt.timedelta(days=1, hours=3), 50000)
        self.pause("short", T0 + dt.timedelta(days=1, hours=6), 150000, gap_min=40)  # cache still warm
        self.turn("x", T0 + dt.timedelta(days=1, hours=9), 150000, kind="subagent")
        v = self.view()
        self.assertEqual(v["covered"]["returns"], 7)
        self.assertEqual((v["covered"]["counted"], v["covered"]["applied"]), (6, 3))
        self.assertEqual(v["covered"]["rate"], 0.5)
        self.assertEqual(v["summary_tokens"], 15000)
        self.assertEqual(v["between"]["returns"], 1)
        self.assertEqual(v["small"], 1)
        self.assertEqual(v["verdict"], "unknown")  # one return between 100k and 200k is too few

    def test_lower_when_rewrites_cost_more_than_summaries(self):
        for i in range(6):
            self.pause("c%d" % i, T0 + dt.timedelta(hours=3 * i), 300000, applied=True)
        for i in range(6):
            self.pause("b%d" % i, T0 + dt.timedelta(days=1, hours=3 * i), 190000, ret_write=190000)
        v = self.view()
        self.assertEqual(v["verdict"], "lower")
        self.assertGreater(v["between"]["expected_usd"], 0)
        self.assertAlmostEqual(v["between"]["expected_usd"], v["between"]["net_usd"])  # rate 1

    def test_keep_when_abandoned_sessions_eat_the_saving(self):
        for i in range(6):
            self.pause("c%d" % i, T0 + dt.timedelta(hours=3 * i), 300000, applied=True)
        for i in range(6):
            self.pause("b%d" % i, T0 + dt.timedelta(days=1, hours=3 * i), 110000, ret_write=30000)
        for i in range(10):
            self.turn("left%d" % i, T0 + dt.timedelta(days=2, hours=i), 150000)
        self.turn("recent", NOW - dt.timedelta(minutes=10), 150000)  # not 54 min old yet: no summary so far
        v = self.view()
        self.assertEqual(v["left"]["sessions"], 16)  # the 6 "b" too: they end between 100k and 200k after their return
        self.assertLess(v["between"]["net_usd"], 0)
        self.assertEqual(v["verdict"], "keep")

    def test_a_return_already_summarised_below_200k_is_valued_as_the_rewrite_it_spared(self):
        self.pause("b", T0, 150000, applied=True)
        v = self.view()
        self.assertEqual((v["between"]["returns"], v["between"]["applied"]), (1, 1))
        self.assertAlmostEqual(v["between"]["rewrite_usd"], 150000 * pricing.price("claude-opus-5-5")[3] / 1e6)  # a 1 h write
        self.assertGreater(v["between"]["rewrite_usd"], v["between"]["summary_usd"])
        self.assertGreater(v["between"]["summary_usd"], 0)

    def test_already_at_100k_the_summaries_it_pays_for_are_not_called_a_loss(self):
        for i in range(8):
            self.pause("b%d" % i, T0 + dt.timedelta(hours=3 * i), 150000, applied=True)
            self.pause("c%d" % i, T0 + dt.timedelta(days=1, hours=3 * i), 250000, applied=True)
        for i in range(2):
            self.turn("left%d" % i, T0 + dt.timedelta(days=2, hours=i), 150000)
        v = self.view()
        self.assertEqual((v["between"]["returns"], v["between"]["applied"], v["left"]["sessions"]), (8, 8, 2))
        self.assertGreater(v["between"]["expected_usd"], 0)
        self.assertEqual(v["verdict"], "lower")  # 100k pays off: not "keep 200k"

    def test_raising_it_loses_the_summaries_that_spared_a_rewrite(self):
        for i in range(6):
            self.pause("c%d" % i, T0 + dt.timedelta(hours=3 * i), 350000, applied=True)
        self.pause("big", T0 + dt.timedelta(days=1), 500000)  # above 400k: not weighed
        v = self.view()
        up = v["above"]
        self.assertEqual((up["returns"], up["applied"], v["raise_to"]), (6, 6, 400000))
        self.assertGreater(up["rewrite_usd"], up["summary_usd"])
        self.assertLess(up["expected_usd"], 0)
        self.assertEqual(v["verdict_raise"], "keep")

    def test_raising_it_pays_off_when_summaries_are_wasted(self):
        for i in range(6):
            self.pause("c%d" % i, T0 + dt.timedelta(hours=3 * i), 210000, ret_write=1000)
        self.pause("a", T0 + dt.timedelta(days=1), 500000, applied=True)  # one summary seen: the rate is known
        for i in range(30):
            self.turn("left%d" % i, T0 + dt.timedelta(days=2, hours=i), 250000)
        v = self.view()
        self.assertEqual(v["above"]["left_sessions"], 36)  # the 6 "c" end at 211k after their return
        self.assertEqual(v["verdict_raise"], "unknown")  # one return counted for the rate is too few
        for i in range(5):
            self.pause("d%d" % i, T0 + dt.timedelta(days=3, hours=3 * i), 500000, applied=True)
        self.assertEqual(self.view()["verdict_raise"], "raise")

    def test_old_database_without_compactions(self):
        self.con.execute("DROP TABLE compactions")
        self.pause("b", T0, 150000)
        self.assertEqual(self.view()["between"]["returns"], 1)


class Command(unittest.TestCase):
    def setUp(self):
        self.home = isolate.claude_home(self)
        os.environ["KIT_DATA_DIR"] = os.path.join(self.home, "data")
        os.environ.pop(ENV, None)
        os.makedirs(os.path.join(self.home, ".claude"))

    def run_cli(self, *argv, view=None):
        out = io.StringIO()
        real = analysis.idle_compact_view
        if view:
            analysis.idle_compact_view = lambda con, days=30, now=None: view
            self.addCleanup(setattr, analysis, "idle_compact_view", real)
        with redirect_stdout(out):
            rc = cli.main(["idle-compact", *argv])
        return rc, out.getvalue()

    def fake(self, verdict, between=3, rate=0.5, expected=1.5, raise_="keep", up=-2.65):
        return {"days": 30, "small": 1, "summary_tokens": 20000,
                "covered": {"returns": 8, "since": "2026-10-15T00:00:00+00:00" if rate else None, "counted": 6,
                            "applied": 3, "rate": rate},
                "between": {"returns": between, "applied": 0, "rewrite_usd": 5.0, "summary_usd": 2.0, "net_usd": 3.0,
                            "expected_usd": expected},
                "left": {"sessions": 2, "summary_usd": 1.0}, "verdict": verdict, "raise_to": 400000,
                "above": {"returns": 4, "applied": 2, "rewrite_usd": 9.0, "summary_usd": 3.0, "left_sessions": 1,
                          "left_usd": 0.7, "net_usd": -5.3, "expected_usd": up}, "verdict_raise": raise_}

    def test_real_empty_database_and_json(self):
        rc, text = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("not set: Claude Code's default, 200k", text)
        self.assertIn("summary was applied in none", text)
        self.assertIn("no return between 100k and 200k", text)
        keepctx.keep("s1")
        rc, text = self.run_cli("--json")
        data = json.loads(text)
        self.assertEqual((data["verdict"], data["keep_context_marks_7d"], data["setting"]["value"]), ("keep", 1, None))

    def test_each_verdict(self):
        _rc, text = self.run_cli(view=self.fake("lower"))
        self.assertIn('100000 pays off: ≈ $1.50', text)
        self.assertIn('"%s": "100000"' % ENV, text)
        self.assertIn("3 of the 6 since it first did (2026-10-15, 50 %)", text)
        _rc, text = self.run_cli(view=self.fake("keep", expected=-0.75))
        self.assertIn("would cost ≈ $0.75 more than they save", text)
        self.assertIn("do not raise it to 400k: ≈ $2.65 more", text)
        _rc, text = self.run_cli(view=self.fake("unknown", rate=None, expected=None, raise_="unknown", up=None))
        self.assertIn("not enough data yet", text)
        self.assertIn("raising it to 400k: not enough data yet", text)
        _rc, text = self.run_cli(view=self.fake("keep", raise_="raise", up=1.25))
        self.assertIn('400k pays off: ≈ $1.25 in 30 days, and those 4 sessions come back whole', text)
        self.assertIn('"%s": "400000"' % ENV, text)

    def test_shows_a_warning_for_a_bad_setting(self):
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as fh:
            json.dump({"env": {ENV: "1e5"}}, fh)
        _rc, text = self.run_cli()
        self.assertIn("⚠ global settings: '1e5' is not a positive whole number", text)


if __name__ == "__main__":
    unittest.main()
