"""Figures that must not lie: unknown-model pricing, the real cache TTL (1 h / 5 min) and one cache percentage.
Synthetic data only. Run: python3 -m unittest discover -s tests"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate
from test_kit import Base, cost_line, usage_line

from sessioncoach import accounts, analysis, cost, nudge, pricing, review
from sessioncoach.projects import Labeler


def line_ttl(ts, mid, ttl, cw=0, **kw):
    """usage_line whose cache write says which TTL it used (as the transcripts do)."""
    d = usage_line(ts, mid, cw=cw, **kw)
    d["message"]["usage"]["cache_creation"] = {"ephemeral_1h_input_tokens": cw if ttl == 3600 else 0,
                                               "ephemeral_5m_input_tokens": cw if ttl == 300 else 0}
    return d


class TestPricing(unittest.TestCase):
    def row(self, **kw):
        return dict({"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cache_write_5m": None, "cache_write_1h": None}, **kw)

    def test_listed_models_are_exact(self):
        for m in ("claude-opus-4-8", "claude-haiku-5-5", "claude-haiku-4-5-20251001", "claude-opus-5-5[1m]", "claude-sonnet-4-6"):
            self.assertTrue(pricing.price_info(m)[1], m)

    def test_one_invented_model_per_family_gets_its_family_and_is_flagged(self):
        cases = {"claude-haiku-9-9": "haiku-5-5", "claude-sonnet-9": "sonnet-5-5", "claude-opus-9-1": "opus-5-5"}
        for model, fallback in cases.items():
            prices, exact, key = pricing.price_info(model)
            self.assertFalse(exact, model)
            self.assertEqual(key, fallback)
            self.assertEqual(prices, pricing.PRICES[fallback])
        self.assertNotEqual(pricing.price("claude-haiku-9-9"), pricing.price("claude-opus-5"))  # no silent Opus price

    def test_unknown_version_is_not_taken_for_a_shorter_listed_one(self):
        # 'opus-4-9' must not match 'opus-4' ($15/$75): it is a newer Opus
        self.assertFalse(pricing.price_info("claude-opus-4-9")[1])
        self.assertEqual(pricing.price_info("claude-opus-4-9")[2], "opus-5-5")

    def test_unknown_family_is_flagged_too(self):
        self.assertFalse(pricing.price_info("gpt-x")[1])
        self.assertFalse(pricing.price_info(None)[1])

    def test_haiku_55_long_prompt_tier(self):
        short = pricing.weight("claude-haiku-5-5", self.row(input=100_000))   # up to 100k tokens: $0.10 per MTok
        long_ = pricing.weight("claude-haiku-5-5", self.row(input=100_001))    # over 100k tokens of prompt: $0.50 per MTok
        self.assertAlmostEqual(short, 0.01, places=4)
        self.assertAlmostEqual(long_, 0.0500005, places=5)

    def test_unpriced_lists_requests_and_usd(self):
        rows = [self.row(input=1_000_000) | {"model": "claude-haiku-9-9"}] * 2 + [self.row(input=5) | {"model": "claude-opus-5"}]
        out = pricing.unpriced(rows, {"haiku-9-9": 1.5, "opus-5": 9.0})
        self.assertEqual([(o["model"], o["requests"], o["usd"], o["fallback"]) for o in out], [("haiku-9-9", 2, 1.5, "haiku-5-5")])


class TestUnpricedReported(Base):
    def test_verify_and_build_list_the_model(self):
        self.write("s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1", model="claude-haiku-9-9", cr=5000),
                                usage_line("2026-10-01T10:01:00Z", "m2", model="claude-opus-5-5"), cost_line(0.5, "claude-haiku-9-9")])
        con, _ = self.ingest()
        v = cost.verify(con)
        self.assertEqual(v["pricing"]["status"], "CHECK")
        self.assertEqual([u["model"] for u in v["pricing"]["unpriced"]], ["haiku-9-9"])
        self.assertEqual(v["pricing"]["unpriced"][0]["requests"], 1)
        data = analysis.build(con, days=None, labeler=Labeler())
        self.assertEqual([u["model"] for u in data["pricing"]["unpriced"]], ["haiku-9-9"])

    def test_clean_when_every_model_is_listed(self):
        self.write("s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1"), cost_line(0.5)])
        con, _ = self.ingest()
        self.assertEqual(cost.verify(con)["pricing"], {"status": "PASS", "table_date": pricing.TABLE_DATE, "unpriced": []})


class TestCacheTtl(Base):
    """A resume is judged against the cache lifetime that session really used."""

    def setUp(self):
        super().setUp()
        self.claude_home = isolate.claude_home(self)

    def lines(self, ttl, gap_min=12, n=4):
        out = []
        for i in range(n):  # n pairs: a turn, then another `gap_min` minutes later that rewrites 200k tokens of cache
            day = "2026-10-0%d" % (i + 1)
            t1 = dt.datetime(2026, 10, i + 1, 8, 0) + dt.timedelta(minutes=gap_min)
            out.append(line_ttl(day + "T08:00:00Z", "a%d" % i, ttl, cw=1000))
            out.append(line_ttl(t1.strftime("%Y-%m-%dT%H:%M:%SZ"), "b%d" % i, ttl, cw=200_000))
        return out

    def r1(self, con):
        reqs = list(con.execute("SELECT * FROM requests"))
        return [r for r in analysis.recommendations([], reqs, []) if r["id"] == "R1"]

    def test_five_minute_session_counts_a_12_minute_pause_one_hour_session_does_not(self):
        self.write("five.jsonl", self.lines(300))
        con, _ = self.ingest()
        r1 = self.r1(con)
        self.assertEqual(r1[0]["params"]["resumes"], 4)
        self.assertEqual(r1[0]["params"]["ttl"], "5 min")
        con.close()
        os.remove(self.db)
        os.remove(os.path.join(self.pdir, "five.jsonl"))
        self.write("hour.jsonl", self.lines(3600))
        con, _ = self.ingest()
        self.assertEqual(self.r1(con), [])  # 12 min is inside the 1 h cache

    def test_no_breakdown_defaults_to_one_hour(self):
        self.write("old.jsonl", [usage_line("2026-10-01T08:00:00Z", "a"), usage_line("2026-10-01T08:30:00Z", "b", cw=200_000)] * 1
                   + [usage_line("2026-10-02T08:00:00Z", "c"), usage_line("2026-10-02T08:30:00Z", "d", cw=200_000)]
                   + [usage_line("2026-10-03T08:00:00Z", "e"), usage_line("2026-10-03T08:30:00Z", "f", cw=200_000)])
        con, _ = self.ingest()
        self.assertEqual(self.r1(con), [])

    def test_ttl_follows_the_last_cache_write_of_the_transcript(self):
        # 5m writes, then 1h writes: a 20-minute pause is a resume at first, not any more later
        det = analysis.ResumeDetector()
        t0 = dt.datetime(2026, 10, 1, 8, 0, tzinfo=dt.timezone.utc)
        def row(ttl, cw):
            return {"cache_write": cw, "cache_write_5m": cw if ttl == 300 else 0, "cache_write_1h": cw if ttl == 3600 else 0}

        self.assertEqual(det.feed("k", t0, row(300, 1000)), (False, 3600))
        hit, ttl = det.feed("k", t0 + dt.timedelta(minutes=20), row(300, 200_000))
        self.assertEqual((hit, ttl), (True, 300))
        _hit2, ttl2 = det.feed("k", t0 + dt.timedelta(minutes=45), row(3600, 200_000))
        self.assertEqual(ttl2, 300)       # the pause is judged by the TTL in force before the request
        hit3, _ = det.feed("k", t0 + dt.timedelta(minutes=70), row(3600, 200_000))
        self.assertFalse(hit3)            # 25 min later, now on a 1 h cache: not a resume

    def test_review_agrees_with_r1_on_a_five_minute_session(self):
        os.environ["KIT_DATA_DIR"] = self.tmp
        try:
            lines = [line_ttl("2026-10-09T%02d:%02d:00Z" % (h, m), "m%d%d" % (h, m), 300, cw=60000 if m == 20 else 1000)
                     for h in (10, 12, 14) for m in (0, 20)]
            self.write("s1.jsonl", [*lines, cost_line(1.0)])
            con, _ = self.ingest()
            res = accounts.AccountResolver([], {}, self.projects)
            r = review.build(con, dt.date(2026, 10, 10), review.load_config({"review": {}, "lang": "en"}), labeler=Labeler(), resolver=res)
            reqs = list(con.execute("SELECT * FROM requests"))
            r1 = [x for x in analysis.recommendations([], reqs, []) if x["id"] == "R1"]
            self.assertEqual(r["causes"]["resumes"], 3)
            self.assertEqual(r["causes"]["resume_ttl"], "5 min")
            resume_cost = 3 * pricing.token_cost_split("claude-opus-5-5", reqs[1])["cache_write"]
            self.assertAlmostEqual(r["causes"]["resume_usd"], resume_cost, places=2)
            self.assertTrue(r1 and r1[0]["params"]["resumes"] == 3)
            self.assertIn("5 min", review.texts(r, review.load_config({"review": {}, "lang": "en"}))[1])
        finally:
            del os.environ["KIT_DATA_DIR"]

    def test_limits_view_uses_the_real_ttl(self):
        now = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc).astimezone()
        lines = [line_ttl("2026-10-09T10:00:00Z", "a", 300, cw=1000), line_ttl("2026-10-09T10:20:00Z", "b", 300, cw=90000)]
        self.write("s1.jsonl", [*lines, cost_line(1.0)])
        con, _ = self.ingest()
        con.execute("INSERT INTO limits_samples VALUES (?,?,?,?,?,?,?,?,?)",
                    (int(now.timestamp()), "s", None, 40.0, int(now.timestamp()) + 3 * 3600, 10.0, int(now.timestamp()) + 86400, 10.0, "m"))
        v = analysis.limits_view(con, now=now, labeler=Labeler(), resolver=accounts.AccountResolver([], {}, self.projects))
        names = {d["name"] for d in v["window"]["by_driver"]}
        self.assertIn("cache_rewrite_after_idle", names)

    def test_nudge_five_minute_session_warns_after_six_minutes_one_hour_does_not(self):
        def transcript(ttl):
            self.write("t.jsonl", [line_ttl("2026-10-01T10:00:00Z", "m1", ttl, inp=0, cr=250000, cw=2000)])
            return {"transcript_path": os.path.join(self.pdir, "t.jsonl")}
        now = dt.datetime(2026, 10, 1, 10, 6, tzinfo=dt.timezone.utc)
        self.assertIsNotNone(nudge.check(transcript(300), {"lang": "en"}, now=now))
        self.assertIsNone(nudge.check(transcript(3600), {"lang": "en"}, now=now))
        late = dt.datetime(2026, 10, 1, 11, 5, tzinfo=dt.timezone.utc)
        self.assertIsNotNone(nudge.check(transcript(3600), {"lang": "en"}, now=late))

    def test_nudge_user_idle_min_wins_over_the_ttl(self):
        self.write("t.jsonl", [line_ttl("2026-10-01T10:00:00Z", "m1", 300, inp=0, cr=250000, cw=2000)])
        data = {"transcript_path": os.path.join(self.pdir, "t.jsonl")}
        now = dt.datetime(2026, 10, 1, 10, 20, tzinfo=dt.timezone.utc)
        self.assertIsNotNone(nudge.check(data, {"lang": "en"}, now=now))
        self.assertIsNone(nudge.check(data, {"lang": "en", "idle_min": 45}, now=now))

    def test_nudge_ttl_comes_from_the_last_write_when_the_last_turn_only_read(self):
        self.write("t.jsonl", [line_ttl("2026-10-01T09:59:00Z", "m0", 300, cw=2000),
                               line_ttl("2026-10-01T10:00:00Z", "m1", 300, inp=0, cr=250000, cw=0)])
        data = {"transcript_path": os.path.join(self.pdir, "t.jsonl")}
        self.assertEqual(nudge.last_turn_info(data["transcript_path"])[2], 300)


class TestCacheShare(Base):
    def test_one_definition_with_its_base(self):
        self.write("s1.jsonl", [usage_line("2026-10-01T10:00:00Z", "m1", cr=1_000_000, cw=100_000), cost_line(1.0)])
        con, _ = self.ingest()
        d = analysis.build(con, days=None, labeler=Labeler())
        split = {x["type"]: x["share"] for x in d["token_split"]}
        cs = d["cache_share"]
        self.assertEqual((cs["basis"], cs["scope"]), ("list_price", "all_activity"))
        self.assertAlmostEqual(cs["share"], split["cache_read"] + split["cache_write"], places=3)
        self.assertEqual(cs["read_share"], split["cache_read"])


if __name__ == "__main__":
    unittest.main()
