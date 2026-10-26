"""`kit demo`: synthetic data through the real pipeline, isolated from the real data dir. Run: python3 -m unittest discover -s tests"""
import datetime as dt
import json
import os
import sqlite3
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: F401  (drops SESSIONCOACH_* left in the shell)
from serverauth import session_cookie

from sessioncoach import analysis, cost, demo, limits, review, server
from sessioncoach.projects import Labeler


class TestDemo(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 14, 15, 30).astimezone()

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.real = os.path.join(self.tmp, "real-kit")
        os.makedirs(self.real)
        with open(os.path.join(self.real, "metrics.db"), "wb") as fh:
            fh.write(b"REAL")
        self.dest = os.path.join(self.real, "demo")
        self._env = {k: os.environ.get(k) for k in ("KIT_DATA_DIR", "KIT_PROJECTS_DIRS")}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def snapshot(self, root):
        out = {}
        for d, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(d, f)
                out[p] = (os.path.getsize(p), os.path.getmtime(p))
        return out

    def con(self, db):
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c

    def test_demo_does_not_touch_the_real_data_dir(self):
        before = {p: v for p, v in self.snapshot(self.real).items()}
        db = demo.build(self.dest, days=10, seed=3, now=self.NOW)
        self.assertTrue(db.startswith(self.dest + os.sep))
        after = self.snapshot(self.real)
        self.assertEqual({p: v for p, v in after.items() if not p.startswith(self.dest + os.sep)}, before)
        with open(os.path.join(self.real, "metrics.db"), "rb") as fh:
            self.assertEqual(fh.read(), b"REAL")
        self.assertTrue(os.path.exists(os.path.join(self.dest, demo.MARKER)))

    def test_demo_dir_made_before_the_rename_is_reused(self):
        os.makedirs(self.dest)
        open(os.path.join(self.dest, ".kit-demo"), "w").close()
        demo.build(self.dest, days=3, seed=1, now=self.NOW)
        self.assertTrue(os.path.exists(os.path.join(self.dest, demo.MARKER)))

    def test_refuses_a_directory_that_is_not_a_demo(self):
        os.makedirs(self.dest)
        with open(os.path.join(self.dest, "precious.txt"), "w") as fh:
            fh.write("x")
        with self.assertRaises(SystemExit):
            demo.build(self.dest, days=3, seed=1, now=self.NOW)
        self.assertTrue(os.path.exists(os.path.join(self.dest, "precious.txt")))

    def test_deterministic_for_a_seed_and_different_for_another(self):
        def counts(seed):
            db = demo.build(self.dest, days=8, seed=seed, now=self.NOW)
            c = self.con(db)
            try:
                counts = c.execute("SELECT COUNT(*), SUM(output) FROM requests").fetchone()[:]
                return (counts, round(c.execute("SELECT SUM(usd) FROM cost_slices").fetchone()[0], 4))
            finally:
                c.close()
        a, b, other = counts(5), counts(5), counts(6)
        self.assertEqual(a, b)
        self.assertNotEqual(a, other)

    def test_the_live_session_passes_the_backtest_at_any_hour(self):
        # its cost snapshots were dated after the next burst's first request: a third of the hours of a day gave
        # CHECK (the CLI test of verify failed from 16:07 UTC on)
        for now in (dt.datetime(2026, 10, 26, 16, 7, tzinfo=dt.timezone.utc), dt.datetime(2026, 10, 26, 18, 7, tzinfo=dt.timezone.utc),
                    dt.datetime(2026, 10, 26, 23, 37, tzinfo=dt.timezone.utc), self.NOW):
            c = self.con(demo.build(self.dest, days=6, seed=3, now=now))
            try:
                self.assertEqual(cost.verify(c)["backtest"], "PASS", now)
            finally:
                c.close()

    def test_goes_through_the_real_pipeline_and_fills_every_view(self):
        db = demo.build(self.dest, days=30, seed=7, now=self.NOW)
        os.environ["KIT_DATA_DIR"] = self.dest
        c = self.con(db)
        try:
            lab = Labeler.load()
            d = analysis.build(c, days=30, labeler=lab, today=self.NOW.date())
            self.assertGreater(d["summary"]["total_usd"], 100)
            self.assertGreaterEqual(len({p["tenant"] for p in d["projects"]}), 4)
            self.assertTrue(all(p["project"].split("-")[0] in ("acme", "globex", "initech", "dotfiles") for p in d["projects"]))
            ids = {r["id"] for r in d["recommendations"]}
            self.assertTrue({"R1", "R3"} <= ids, ids)
            self.assertEqual(d["pricing"]["unpriced"], [])
            self.assertEqual(cost.verify(c)["backtest"], "PASS")
            self.assertGreaterEqual(len(d["accounts"]), 4)
            self.assertTrue(any(a["value_multiple"] for a in d["accounts"]))
            self.assertGreater(c.execute("SELECT COUNT(*) FROM requests WHERE kind='subagent' AND model LIKE '%opus%'").fetchone()[0], 0)
            self.assertGreater(c.execute("SELECT COUNT(*) FROM limits_samples").fetchone()[0], 20)
            # sessions with resumes after the cache expired, on both TTLs
            r1 = next(r for r in d["recommendations"] if r["id"] == "R1")
            self.assertEqual(sorted(r1["params"]["ttl"].split(" / ")), ["1 h", "5 min"])
            rv = review.build(c, self.NOW.date(), review.load_config(), labeler=lab)
            self.assertTrue(rv["day"] and rv["totals"]["usd"] > 0)
            lv = analysis.limits_view(c, now=self.NOW, labeler=lab)
            self.assertIsNotNone(lv["last"])
            self.assertTrue(limits.evaluate_budgets(c, limits.load_alerts(), today=self.NOW.date(), dry=True))
            mixed = c.execute(
                "SELECT COUNT(*) FROM (SELECT session_id FROM session_dirs GROUP BY session_id HAVING COUNT(*) > 1)").fetchone()[0]
            self.assertGreater(mixed, 0)
        finally:
            c.close()

    def test_server_in_demo_mode_says_so_and_closes_settings_and_refresh(self):
        db = demo.build(self.dest, days=5, seed=2, now=self.NOW)
        os.environ["KIT_DATA_DIR"] = self.dest
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(db, token="t", demo=True))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        auth = {"Cookie": session_cookie(httpd)}
        try:
            with urllib.request.urlopen(urllib.request.Request(base + "/api/data?days=0", headers=auth)) as r:
                self.assertTrue(json.loads(r.read())["demo"])
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(urllib.request.Request(base + "/api/settings", headers=dict(auth, **{"X-Kit-Token": "t"})))
            self.assertEqual(ctx.exception.code, 403)
            req = urllib.request.Request(base + "/api/refresh", data=b"", method="POST", headers=dict(auth, **{"X-Kit-Token": "t"}))
            with urllib.request.urlopen(req) as r:
                self.assertEqual(json.loads(r.read())["added"], {"req": 0, "cost": 0, "sub": 0})
            with self.assertRaises(urllib.error.HTTPError) as ctx:  # same login as the dashboard; the page names the demo
                urllib.request.urlopen(base + "/")
            self.assertEqual(ctx.exception.code, 401)
            page = ctx.exception.read().decode("utf-8")
            self.assertIn("sessioncoach demo", page)
            self.assertNotIn('content="t"', page)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_normal_server_is_not_demo(self):
        db = demo.build(self.dest, days=3, seed=2, now=self.NOW)
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(db))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d/api/data?days=0" % httpd.server_address[1],
                                                        headers={"Cookie": session_cookie(httpd)})) as r:
                self.assertFalse(json.loads(r.read())["demo"])
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
