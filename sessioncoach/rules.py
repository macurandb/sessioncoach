"""Recommendation rules: each criterion is written here once.

Two consumers:
- `analysis.recommendations` (dashboard and `report`) runs DASHBOARD over the requests of a window;
- the daily review adds the same per-request criteria up per session (`review._day_figures`) and ranks
  REVIEW_TIPS over those totals (`review.tips`).

Both use the same values:
- long context (R16): alerts.json → review.long_context_tokens (config.LONG_CONTEXT_TOKENS, 300k, by default);
- R3: only R3_SUBAGENT_TYPE (general-purpose) subagents on Opus, and only their own requests (r3_agent_files).

Only `pricing` and the leaf `config` are imported: analysis imports this module, so R1's resume detection
(analysis.ResumeDetector) reaches the rules already done, in `Window.resumes`.
"""
import collections

from . import config, pricing

LONG_CONTEXT = config.LONG_CONTEXT_TOKENS  # R16 default when alerts.json sets no review.long_context_tokens
R16_SHARE = 0.30       # dashboard R16 fires when long turns carry this share of the main-session list-price cost
R1_MIN, R1_HIGH = 3, 10               # dashboard R1: resumes to fire, and to say "high" confidence
R2_SHARE = 0.10                       # dashboard R2: share of the cost on 1M-context models
R3_MIN = 3                            # dashboard R3: Opus subagents (of R3_SUBAGENT_TYPE) to fire
R3_SUBAGENT_TYPE = "general-purpose"  # R3: the type that inherits the main model (the others pick theirs)
R4_SHARE = 0.30                       # dashboard R4: subagents' share of the list-price cost
TIP_MIN_USD = 0.5                     # review: a tip needs a savings ceiling above this
RANK = {"high": 3, "medium": 2, "low": 1}

# A window of activity for the dashboard rules: cost slices, request rows, subagent rows, the main-session
# resumes after the cache expired as [(request row, cache TTL in seconds)] in detection order, and the R16 threshold.
Window = collections.namedtuple("Window", "cur reqs subs resumes long_context", defaults=(LONG_CONTEXT,))
# One reviewed day for the review tips: its rounded causes (review._day_figures), the long-context threshold
# in force, and the cache TTL to name when the day saw no resume.
Day = collections.namedtuple("Day", "causes long_context idle_ttl")
# `detect(Window)` -> recommendation dict or None (dashboard); `detect(Day)` -> (savings ceiling, params) (review)
Rule = collections.namedtuple("Rule", "id detect")


# --- per-request criteria, shared by both consumers ---

def resume_usd(r):
    """R1: list-price USD of the cache a resume re-writes."""
    return pricing.token_cost_split(r["model"], r)["cache_write"]


def on_opus(model):
    """R3: does `model` run on Opus?"""
    return "opus" in (model or "")


def opus_agent(s, subagent_type=None):
    """R3: did subagent row `s` run on Opus (and is it of `subagent_type`, when one is given)?"""
    return (subagent_type is None or (s["subagent_type"] or "") == subagent_type) and on_opus(s["model"])


def r3_agent_files(subs, subagent_type=R3_SUBAGENT_TYPE):
    """R3: the transcripts (agent-<id>.jsonl) of the subagent rows in `subs` that count; None counts every one."""
    return {"agent-%s.jsonl" % s["agent_id"] for s in subs if opus_agent(s, subagent_type)} if subagent_type else None


def r3_request(r, files):
    """R3: is subagent request `r` on Opus and, unless `files` is None, from one of those transcripts?"""
    return on_opus(r["model"]) and (files is None or r["file"] in files)


def sonnet_saving(opus_usd):
    """R3 savings ceiling: the Opus-to-Sonnet price difference on `opus_usd`."""
    return opus_usd * (1 - pricing.sonnet_opus_ratio())


def is_long(r, threshold):
    """R16: does request `r` carry at least `threshold` tokens of context?"""
    return pricing.context_tokens(r) >= threshold


def long_excess_usd(r, threshold):
    """R16 savings ceiling of one long turn: re-reading only the context above `threshold` would not happen."""
    ctx = pricing.context_tokens(r)
    return pricing.token_cost_split(r["model"], r)["cache_read"] * (ctx - threshold) / ctx


# --- dashboard rules, over a Window ---

def r1_resumes(w):
    """Big cache rewrites after the cache expired (resumed sessions, long pauses)."""
    n = len(w.resumes)
    if n < R1_MIN:
        return None
    usd, ttls = 0.0, collections.Counter()
    for r, ttl in w.resumes:
        usd += resume_usd(r)
        ttls[ttl] += 1
    return {"id": "R1", "saving_usd": round(usd, 2), "confidence": "high" if n >= R1_HIGH else "medium",
            "effort": "habit", "params": {"resumes": n, "tokens": sum(r["cache_write"] or 0 for r, _ttl in w.resumes),
                                          "usd": round(usd, 2),
                                          "ttl": " / ".join(pricing.fmt_ttl(s) for s in sorted(ttls, reverse=True))},
            "verify_metric": "cache_resumes"}


def r2_one_million(w):
    """Share of the cost on 1M-context models."""
    total = sum(s["usd"] for s in w.cur) or 0.0
    m1 = sum(s["usd"] for s in w.cur if "[1m]" in s["model"])
    if not total or m1 / total < R2_SHARE:
        return None
    return {"id": "R2", "saving_usd": None, "confidence": "medium", "effort": "habit",
            "params": {"share": round(m1 / total, 3), "usd": round(m1, 2)}, "verify_metric": "share_1m"}


def r3_opus_subagents(w, subagent_type=R3_SUBAGENT_TYPE):
    """Subagents running on Opus (search/exploration rarely needs it).

    With a `subagent_type`, the saving is over the requests of those agents only (their transcript is
    agent-<id>.jsonl), the same base that fires the rule; without one, over every Opus subagent."""
    agents = [s for s in w.subs if opus_agent(s, subagent_type)]
    if len(agents) < R3_MIN:
        return None
    files = r3_agent_files(agents, subagent_type)
    opus_usd = sum(pricing.weight(r["model"], r) for r in w.reqs if r["kind"] == "subagent" and r3_request(r, files))
    return {"id": "R3", "saving_usd": round(sonnet_saving(opus_usd), 2), "confidence": "medium",
            "effort": "config", "params": {"agents": len(agents), "tokens": sum(s["tokens"] or 0 for s in agents),
                                           "subagent_opus_usd": round(opus_usd, 2)},
            "verify_metric": "subagents_by_model"}


def r4_subagent_share(w):
    """Subagents' share of the list-price cost (delegation can cost more than doing it in sequence)."""
    w_all = sum(pricing.weight(r["model"], r) for r in w.reqs) or 0.0
    w_sub = sum(pricing.weight(r["model"], r) for r in w.reqs if r["kind"] == "subagent")
    if not w_all or w_sub / w_all < R4_SHARE:
        return None
    return {"id": "R4", "saving_usd": None, "confidence": "low", "effort": "habit",
            "params": {"share": round(w_sub / w_all, 3), "usd": round(w_sub, 2)}, "verify_metric": "subagent_share"}


def r16_long_context(w, threshold=None):
    """Long sessions: every turn re-reads the whole context, so cost grows with context size.
    The threshold is `w.long_context` unless one is given."""
    threshold = threshold or w.long_context
    main = [r for r in w.reqs if r["kind"] == "main"]
    w_main = sum(pricing.weight(r["model"], r) for r in main) or 0.0
    big = [r for r in main if is_long(r, threshold)]
    w_big = sum(pricing.weight(r["model"], r) for r in big)
    if not w_main or w_big / w_main < R16_SHARE:
        return None
    ctxs = sorted(pricing.context_tokens(r) for r in main)
    return {"id": "R16", "saving_usd": None, "confidence": "high", "effort": "habit",
            "params": {"share": round(w_big / w_main, 3), "usd": round(w_big, 2), "median_k": int(ctxs[len(ctxs) // 2] / 1000),
                       "threshold_k": int(threshold / 1000)}, "verify_metric": "context_per_turn"}


# in the order they are listed when their savings and confidence tie
DASHBOARD = (Rule("R1", r1_resumes), Rule("R2", r2_one_million), Rule("R3", r3_opus_subagents),
             Rule("R4", r4_subagent_share), Rule("R16", r16_long_context))


def recommend(w, table=DASHBOARD):
    """The recommendations of window `w`, biggest saving first, then the most confident."""
    recs = [rec for rec in (rule.detect(w) for rule in table) if rec]
    return sorted(recs, key=lambda r: (-(r["saving_usd"] or 0), -RANK[r["confidence"]]))


# --- review tips, over a Day ---

def r1_tip(d):
    return d.causes["resume_usd"], {"usd": d.causes["resume_usd"], "n": d.causes["resumes"],
                                    "ttl": d.causes.get("resume_ttl") or d.idle_ttl}


def r16_tip(d):
    return d.causes["long_excess_usd"], {"usd": d.causes["long_usd"], "k": int(d.long_context / 1000)}


def r3_tip(d):
    return sonnet_saving(d.causes["opus_usd"]), {"usd": d.causes["opus_usd"]}


# in the order they are listed when their ceilings tie
REVIEW_TIPS = (Rule("R1", r1_tip), Rule("R16", r16_tip), Rule("R3", r3_tip))


def tips(d, table=REVIEW_TIPS):
    """The tips of day `d` whose savings ceiling is above TIP_MIN_USD, biggest first."""
    cands = [(rule.id, *rule.detect(d)) for rule in table]
    cands = sorted((c for c in cands if c[1] > TIP_MIN_USD), key=lambda c: -c[1])
    return [{"id": i, "saving_usd": round(v, 2), "params": p} for i, v, p in cands]
