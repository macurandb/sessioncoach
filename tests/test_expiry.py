import datetime as dt
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import isolate

from sessioncoach import analysis, cli, collect, expiry, store
from sessioncoach.projects import Labeler

T0 = dt.datetime(2026, 10, 15, 10, 0, tzinfo=dt.timezone.utc)
PDIR = "-Users-someone-code-shop"


def ts(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Expiry(unittest.TestCase):
    def setUp(self):
        isolate.claude_home(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.con = store.connect(os.path.join(self.tmp, "metrics.db"))
        self.sent = []
        self.n = 0

    def turn(self, t, sid="s1", pdir=PDIR, ctx=200000, w1h=5000, w5m=0, kind="main", breakdown=True):
        self.n += 1
        store.add_request(self.con, {
            "project_dir": pdir, "file": sid + ".jsonl", "msg_id": "m%d" % self.n, "request_id": "r%d" % self.n,
            "session_id": sid, "kind": kind, "ts": ts(t), "model": "claude-opus-5-5", "input": 10, "output": 100,
            "cache_read": ctx - 10 - w1h - w5m, "cache_write": w1h + w5m,
            "cache_write_1h": w1h if breakdown else None, "cache_write_5m": w5m if breakdown else None})

    def check(self, now, alerts=None, **kw):
        alerts = dict({"lang": "es", "ingest": {"interval_min": 30}}, **(alerts or {}))
        return expiry.check(self.con, alerts, now=now, notify_fn=lambda t, m: self.sent.append((t, m)),
                            labeler=Labeler(), **kw)

    def ticks(self, phase_min, ttl_min=60, interval=30):
        """Run every ingest of one pause (first one `phase_min` after the last turn) until the cache is gone."""
        t = phase_min
        while t < ttl_min + interval:
            self.check(T0 + dt.timedelta(minutes=t))
            t += interval

    def test_resumes_of_the_last_days_count_a_pause_that_began_before_the_window(self):
        self.turn(T0)
        self.turn(T0 + dt.timedelta(hours=2), w1h=150000)  # cache (1 h) expired: a resume
        self.turn(T0 + dt.timedelta(hours=2, minutes=5), w1h=150000)  # cache still alive
        n, usd = analysis.resumes_since(self.con, 7, now=T0 + dt.timedelta(hours=3))
        self.assertEqual(n, 1)
        self.assertGreater(usd, 0)
        # the window starts after the first turn and before the resume: the resume still counts
        self.assertEqual(analysis.resumes_since(self.con, 1, now=T0 + dt.timedelta(hours=25, minutes=30))[0], 1)
        self.assertEqual(analysis.resumes_since(self.con, 7, now=T0 + dt.timedelta(days=9)), (0, 0.0))

    def test_one_warning_per_pause_whatever_the_ingest_phase(self):
        for phase in range(30):
            with self.subTest(phase=phase):
                self.con.execute("DELETE FROM requests")
                self.con.execute("DELETE FROM alerts_sent")
                self.sent = []
                self.turn(T0)
                self.ticks(phase)
                self.assertEqual(len(self.sent), 1)

    def test_unknown_ttl_counts_as_one_hour(self):
        self.turn(T0, breakdown=False)
        self.ticks(7)
        self.assertEqual(len(self.sent), 1)

    def test_five_minute_cache_never_warned(self):
        self.turn(T0, w1h=0, w5m=5000)
        for m in range(0, 70, 2):
            self.check(T0 + dt.timedelta(minutes=m))
        self.assertEqual(self.sent, [])

    def test_ttl_from_the_last_turn_that_wrote_cache(self):
        self.turn(T0 - dt.timedelta(minutes=5), w1h=5000)
        self.turn(T0, w1h=0)  # only read cache: the 1 h TTL stays in force
        self.assertEqual(len(self.check(T0 + dt.timedelta(minutes=40), dry=True)), 1)

    def test_message(self):
        self.turn(T0)
        self.check(T0 + dt.timedelta(minutes=40))
        title, body = self.sent[0]
        self.assertEqual(title, "Claude · la caché de %s caduca en ~20 min" % Labeler()(PDIR)[0])
        self.assertIn("~200k tokens", body)
        self.assertIn("$1.60 (equivalente API)", body)  # 200k tokens at $8/M, the 1 h write price of Opus 5.5
        self.assertIn("docs/HANDOFF-2026-10-15.md", body)
        self.con.execute("DELETE FROM alerts_sent")
        self.check(T0 + dt.timedelta(minutes=40), alerts={"lang": "en"})
        self.assertTrue(self.sent[1][0].startswith("Claude · the cache of %s expires" % Labeler()(PDIR)[0]))

    def test_message_with_handoff_installed(self):
        from sessioncoach import handoff
        handoff.install()
        self.turn(T0)
        self.check(T0 + dt.timedelta(minutes=40))
        title, body = self.sent[0]
        self.assertTrue(title.startswith("Claude · la caché de "))
        self.assertIn("$1.60 (equivalente API)", body)
        self.assertIn("escribe /handoff ahora o haz /compact", body)
        self.assertNotIn("docs/HANDOFF-", body)

    def test_second_pause_of_the_same_session_warns_again(self):
        self.turn(T0)
        self.check(T0 + dt.timedelta(minutes=40))
        self.turn(T0 + dt.timedelta(minutes=45))
        self.check(T0 + dt.timedelta(minutes=90))
        self.assertEqual(len(self.sent), 2)

    def test_session_replaced_in_the_same_project_is_not_warned(self):
        self.turn(T0)
        self.turn(T0 + dt.timedelta(minutes=10), sid="s2", ctx=20000)
        self.check(T0 + dt.timedelta(minutes=40))
        self.assertEqual(self.sent, [])

    def test_parallel_session_in_the_same_project_does_not_silence_it(self):
        self.turn(T0 - dt.timedelta(minutes=30), sid="s2", ctx=20000)
        self.turn(T0)
        self.turn(T0 + dt.timedelta(minutes=10), sid="s2", ctx=20000)
        self.check(T0 + dt.timedelta(minutes=40))
        self.assertEqual(len(self.sent), 1)

    def test_running_subagent_means_no_pause(self):
        self.turn(T0)
        self.turn(T0 + dt.timedelta(minutes=30), kind="subagent", ctx=20000)
        self.check(T0 + dt.timedelta(minutes=40))
        self.assertEqual(self.sent, [])

    def test_compacted_after_the_last_turn_is_not_warned(self):
        self.turn(T0)
        store.add_compaction(self.con, {"project_dir": PDIR, "session_id": "s1", "ts": ts(T0 + dt.timedelta(minutes=9)),
                                        "trigger": "manual", "pre_tokens": 200000, "post_tokens": 50000})
        self.check(T0 + dt.timedelta(minutes=40))
        self.assertEqual(self.sent, [])
        self.turn(T0 + dt.timedelta(minutes=45), ctx=250000)  # works on after the compaction: a new pause counts again
        self.check(T0 + dt.timedelta(minutes=90))
        self.assertEqual(len(self.sent), 1)

    def test_other_project_does_not_replace_it(self):
        self.turn(T0)
        self.turn(T0 + dt.timedelta(minutes=10), sid="s2", pdir="-Users-someone-code-other", ctx=20000)
        self.check(T0 + dt.timedelta(minutes=40))
        self.assertEqual(len(self.sent), 1)

    def test_small_context_short_idle_and_subagents_ignored(self):
        self.turn(T0, ctx=50000)
        self.turn(T0, sid="s2", pdir="-Users-someone-code-sub", kind="subagent")
        self.check(T0 + dt.timedelta(minutes=40))
        self.assertEqual(self.sent, [])
        self.turn(T0, sid="s3", pdir="-Users-someone-code-busy")
        self.check(T0 + dt.timedelta(minutes=5), alerts={"ingest": {"interval_min": 60}})  # idle under min_idle_min
        self.assertEqual(self.sent, [])

    def test_bad_config_keeps_defaults(self):
        self.assertEqual(expiry.config({"cache_expiry": ["x"]}), expiry.DEFAULTS)
        cfg = expiry.config({"cache_expiry": {"enabled": "false", "min_idle_min": "15", "slack_min": "x", "other": 1}})
        self.assertEqual(cfg, dict(expiry.DEFAULTS, min_idle_min=15))

    def test_long_prompt_price(self):
        self.turn(T0)
        self.con.execute("UPDATE requests SET model = 'claude-haiku-5-5'")
        c = self.check(T0 + dt.timedelta(minutes=40), dry=True)[0]
        self.assertAlmostEqual(c["rewrite_usd"], 0.2)  # 200k at $1/M, haiku's long-prompt 1 h write price

    def test_disabled_and_notify_off(self):
        self.turn(T0)
        self.assertEqual(self.check(T0 + dt.timedelta(minutes=40), alerts={"cache_expiry": {"enabled": False}}), [])
        self.check(T0 + dt.timedelta(minutes=40), alerts={"notify": False})
        self.assertEqual(self.sent, [])

    def test_bad_timestamp_skipped(self):
        self.turn(T0)
        self.con.execute("UPDATE requests SET ts = '2026-10-15Tbad'")
        self.assertEqual(self.check(T0 + dt.timedelta(minutes=40)), [])

    def test_ingest_survives_a_failure(self):
        data = os.path.join(self.tmp, "data")
        with mock.patch.dict(os.environ, {"KIT_DATA_DIR": data, "KIT_PROJECTS_DIRS": os.path.join(self.tmp, "none")}), \
                mock.patch.object(expiry, "check", side_effect=RuntimeError("boom")) as chk, \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            cli.main(["ingest"])
        chk.assert_called_once()
        self.assertIn('"added"', out.getvalue())
        self.assertIn("boom", err.getvalue())


class Compactions(unittest.TestCase):
    def test_ingest_records_compactions_numbers_only(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        pdir = os.path.join(tmp, "projects", PDIR)
        os.makedirs(pdir)
        lines = [{"type": "system", "subtype": "compact_boundary", "timestamp": ts(T0), "content": "SECRET summary",
                  "compactMetadata": {"trigger": "manual", "preTokens": 745000, "postTokens": 57000}},
                 {"type": "system", "subtype": "compact_boundary", "timestamp": ts(T0 + dt.timedelta(hours=2)),
                  "compactMetadata": {"trigger": "free text", "preTokens": 293000, "precomputed": True}},
                 {"type": "system", "subtype": "compact_boundary", "timestamp": ts(T0 + dt.timedelta(hours=3))}]
        with open(os.path.join(pdir, "s1.jsonl"), "w") as fh:
            fh.write("".join(json.dumps(x) + "\n" for x in lines))
        con = store.connect(os.path.join(tmp, "metrics.db"))
        collect.ingest(con, [os.path.join(tmp, "projects")])
        collect.ingest(con, [os.path.join(tmp, "projects")], full=True)  # idempotent
        rows = [tuple(r) for r in con.execute("SELECT session_id, trigger, pre_tokens, post_tokens, precomputed FROM compactions "
                                              "ORDER BY ts")]
        self.assertEqual(rows, [("s1", "manual", 745000, 57000, 0), ("s1", None, 293000, None, 1), ("s1", None, None, None, 0)])
        self.assertNotIn("SECRET", repr([tuple(r) for r in con.execute("SELECT * FROM compactions")]))


if __name__ == "__main__":
    unittest.main()
