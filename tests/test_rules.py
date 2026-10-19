"""Recommendation rules, one criterion each (rules.py), and the pricing helpers they share.
Synthetic rows only. Run: python3 -m unittest discover -s tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: F401  (drops SESSIONCOACH_* left in the shell)

from sessioncoach import pricing, rules


def req(kind="main", model="claude-opus-5-5", inp=0, out=0, cr=0, cw=0, ts="2026-10-01T00:00:00Z", f="a.jsonl"):
    return {"kind": kind, "ts": ts, "model": model, "input": inp, "output": out, "cache_read": cr, "cache_write": cw,
            "cache_write_1h": None, "cache_write_5m": None, "project_dir": "-p", "file": f, "session_id": "s"}


class TestPricingHelpers(unittest.TestCase):
    def test_context_tokens_adds_input_and_both_cache_sides(self):
        self.assertEqual(pricing.context_tokens(req(inp=1, cr=20, cw=300, out=4000)), 321)
        self.assertEqual(pricing.context_tokens({"input": None, "cache_read": None, "cache_write": 5}), 5)

    def test_sonnet_opus_ratio_is_the_input_price_ratio(self):
        self.assertEqual(pricing.sonnet_opus_ratio(), pricing.PRICES["sonnet-5-5"][0] / pricing.PRICES["opus-5-5"][0])
        self.assertEqual(pricing.sonnet_opus_ratio(), 0.5)

    def test_long_prompt_tier_follows_context_tokens(self):
        r = req(model="claude-haiku-5-5", inp=pricing.LONG_PROMPT, cr=1)
        self.assertEqual(pricing.price(r["model"], r), pricing.LONG_PRICES["haiku-5-5"])
        r["cr"] = r["cache_read"] = 0
        self.assertEqual(pricing.price(r["model"], r), pricing.PRICES["haiku-5-5"])


def window(reqs=(), subs=(), cur=(), resumes=()):
    return rules.Window(list(cur), list(reqs), list(subs), list(resumes))


class TestR16(unittest.TestCase):
    def test_is_long_at_the_threshold(self):
        self.assertTrue(rules.is_long(req(cr=399000, inp=1000), 400000))
        self.assertFalse(rules.is_long(req(cr=399000, inp=999), 400000))
        self.assertTrue(rules.is_long(req(cr=300000), 300000))  # the review passes its own threshold

    def test_long_excess_is_the_cache_read_above_the_threshold(self):
        r = req(cr=400000)
        full = pricing.token_cost_split(r["model"], r)["cache_read"]
        self.assertAlmostEqual(rules.long_excess_usd(r, 300000), full / 4)
        self.assertEqual(rules.long_excess_usd(r, 400000), 0)

    def test_fires_when_long_turns_carry_30_percent_of_main_cost(self):
        reqs = [req(cr=500000), req(cr=10000, out=100), req(kind="subagent", cr=900000)]
        rec = rules.r16_long_context(window(reqs))
        self.assertEqual(rec["id"], "R16")
        self.assertEqual(rec["params"]["threshold_k"], 300)  # the default, the daily review's since they were unified
        self.assertEqual(rec["params"]["median_k"], 500)  # median over main turns only
        self.assertGreater(rec["params"]["share"], 0.3)
        self.assertEqual(rules.r16_long_context(window(reqs), threshold=600000), None)
        self.assertEqual(rules.r16_long_context(window(reqs), threshold=200000)["params"]["threshold_k"], 200)

    def test_quiet_below_the_share_or_without_main_cost(self):
        self.assertIsNone(rules.r16_long_context(window([req(cr=500000), req(out=10 ** 6)])))
        self.assertIsNone(rules.r16_long_context(window([req(model="m")])))
        self.assertIsNone(rules.r16_long_context(window()))


def sub(t="general-purpose", model="claude-opus-5-5", tokens=100, agent_id=None):
    sub.n = getattr(sub, "n", 0) + 1
    return {"agent_id": agent_id or "a%d" % sub.n, "subagent_type": t, "model": model, "tokens": tokens, "duration_ms": 60000}


class TestCriteria(unittest.TestCase):
    def test_on_opus_is_a_substring_check(self):
        self.assertTrue(rules.on_opus("claude-opus-5-5[1m]"))
        self.assertFalse(rules.on_opus("claude-sonnet-5-5"))
        self.assertFalse(rules.on_opus(None))

    def test_opus_agent_filters_by_type_only_when_asked(self):
        self.assertTrue(rules.opus_agent(sub("Explore")))
        self.assertFalse(rules.opus_agent(sub("Explore"), "general-purpose"))
        self.assertTrue(rules.opus_agent(sub(), "general-purpose"))
        self.assertFalse(rules.opus_agent(sub(None), "general-purpose"))
        self.assertFalse(rules.opus_agent(sub(model=None)))

    def test_sonnet_saving_is_the_price_difference(self):
        self.assertEqual(rules.sonnet_saving(10.0), 10.0 * (1 - pricing.sonnet_opus_ratio()))
        self.assertEqual(rules.sonnet_saving(0), 0)

    def test_resume_usd_is_the_cache_write_at_list_price(self):
        r = req(cw=1000000)
        self.assertEqual(rules.resume_usd(r), pricing.token_cost_split(r["model"], r)["cache_write"])
        self.assertEqual(rules.resume_usd(req()), 0)

    def test_fmt_ttl(self):
        self.assertEqual(pricing.fmt_ttl(3600), "1 h")
        self.assertEqual(pricing.fmt_ttl(300), "5 min")


class TestDashboardRules(unittest.TestCase):
    def test_r1_from_three_resumes_high_from_ten(self):
        hits = [(req(cw=60000), 3600), (req(cw=60000), 300)]
        self.assertIsNone(rules.r1_resumes(window(resumes=hits)))
        rec = rules.r1_resumes(window(resumes=[*hits, (req(cw=None), 3600)]))
        self.assertEqual((rec["id"], rec["confidence"]), ("R1", "medium"))
        self.assertEqual(rec["params"]["resumes"], 3)
        self.assertEqual(rec["params"]["tokens"], 120000)
        self.assertEqual(rec["params"]["ttl"], "1 h / 5 min")
        self.assertEqual(rec["saving_usd"], rec["params"]["usd"])
        self.assertGreater(rec["saving_usd"], 0)
        self.assertEqual(rules.r1_resumes(window(resumes=hits * 5))["confidence"], "high")

    def test_r2_from_10_percent_on_1m(self):
        cur = [{"usd": 1.0, "model": "claude-opus-5-5[1m]"}, {"usd": 9.0, "model": "claude-opus-5-5"}]
        self.assertEqual(rules.r2_one_million(window(cur=cur))["params"], {"share": 0.1, "usd": 1.0})
        self.assertIsNone(rules.r2_one_million(window(cur=[cur[1], {"usd": 0.9, "model": "x[1m]"}])))
        self.assertIsNone(rules.r2_one_million(window(cur=[{"usd": 0.0, "model": "x[1m]"}])))
        self.assertIsNone(rules.r2_one_million(window()))

    def test_r3_counts_the_filtered_agents_and_prices_every_opus_subagent(self):
        reqs = [req(kind="subagent", out=10 ** 6), req(kind="subagent", model="claude-sonnet-5-5", out=10 ** 6),
                req(out=10 ** 6)]
        subs = [sub(), sub(), sub("Explore"), sub(model="claude-sonnet-5-5")]
        self.assertIsNone(rules.r3_opus_subagents(window(reqs, subs)))  # 2 general-purpose on Opus
        rec = rules.r3_opus_subagents(window(reqs, subs), subagent_type=None)  # the review's view: 3
        opus_usd = pricing.weight(reqs[0]["model"], reqs[0])
        self.assertEqual(rec["params"], {"agents": 3, "tokens": 300, "subagent_opus_usd": round(opus_usd, 2)})
        self.assertEqual(rec["saving_usd"], round(opus_usd * 0.5, 2))
        self.assertEqual(rules.r3_opus_subagents(window(reqs, [*subs, sub()]))["params"]["agents"], 3)

    def test_r3_dashboard_prices_only_the_agents_it_counts(self):
        gp = [sub(agent_id="g%d" % i) for i in range(3)]
        reqs = [req(kind="subagent", out=10 ** 6, f="agent-g%d.jsonl" % i) for i in range(3)]
        reqs.append(req(kind="subagent", out=9 * 10 ** 6, f="agent-x1.jsonl"))  # an Explore agent on Opus
        rec = rules.r3_opus_subagents(window(reqs, [*gp, sub("Explore", agent_id="x1")]))
        three = sum(pricing.weight(r["model"], r) for r in reqs[:3])
        self.assertEqual(rec["params"]["subagent_opus_usd"], round(three, 2))
        self.assertEqual(rec["saving_usd"], round(three * 0.5, 2))

    def test_r4_from_30_percent_on_subagents(self):
        reqs = [req(kind="subagent", out=3 * 10 ** 5), req(out=7 * 10 ** 5)]
        rec = rules.r4_subagent_share(window(reqs))
        self.assertEqual((rec["id"], rec["params"]["share"]), ("R4", 0.3))
        self.assertIsNone(rules.r4_subagent_share(window([*reqs[:1], req(out=8 * 10 ** 5)])))
        self.assertIsNone(rules.r4_subagent_share(window()))

    def test_recommend_runs_the_table_in_order_and_ranks(self):
        def fake(rid, saving, conf):
            return rules.Rule(rid, lambda w: {"id": rid, "saving_usd": saving, "confidence": conf})
        table = (fake("A", None, "low"), fake("B", 2.0, "medium"), rules.Rule("C", lambda w: None),
                 fake("D", None, "high"), fake("E", None, "low"))
        self.assertEqual([r["id"] for r in rules.recommend(window(), table)], ["B", "D", "A", "E"])
        self.assertEqual([r.id for r in rules.DASHBOARD], ["R1", "R2", "R3", "R4", "R16"])
        self.assertEqual(rules.recommend(window()), [])


CAUSES = {"resumes": 3, "resume_usd": 5.0, "resume_ttl": "5 min", "long_usd": 40.0, "long_excess_usd": 12.0, "opus_usd": 8.0}


class TestReviewTips(unittest.TestCase):
    def day(self, **kw):
        return rules.Day(dict(CAUSES, **kw), 300000, "1 h")

    def test_each_tip_gives_its_ceiling_and_params(self):
        self.assertEqual(rules.r1_tip(self.day()), (5.0, {"usd": 5.0, "n": 3, "ttl": "5 min"}))
        self.assertEqual(rules.r1_tip(self.day(resume_ttl=""))[1]["ttl"], "1 h")
        self.assertEqual(rules.r16_tip(self.day()), (12.0, {"usd": 40.0, "k": 300}))
        self.assertEqual(rules.r3_tip(self.day()), (4.0, {"usd": 8.0}))

    def test_tips_keep_ceilings_above_half_a_dollar_biggest_first(self):
        self.assertEqual([t["id"] for t in rules.tips(self.day())], ["R16", "R1", "R3"])
        self.assertEqual([t["id"] for t in rules.tips(self.day(resume_usd=0.5, opus_usd=1.02))], ["R16", "R3"])
        tie = rules.tips(self.day(resume_usd=2.0, long_excess_usd=2.0, opus_usd=4.0))
        self.assertEqual([t["id"] for t in tie], ["R1", "R16", "R3"])  # table order on a tie
        self.assertEqual(rules.tips(self.day(resume_usd=0, long_excess_usd=0, opus_usd=0)), [])


if __name__ == "__main__":
    unittest.main()
