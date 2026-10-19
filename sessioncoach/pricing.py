"""List prices, used only as WEIGHTS to spread recorded cost over time, or as a labelled estimate.

USD per million tokens: (input, output, cache_read, cache_write_1h, cache_write_5m).
Source: platform.claude.com/docs/en/about-claude/pricing, read 2026-10-15 (all rows below are on
that page: Fable/Mythos 5.x, Opus 4/4.1/4.5-4.8/5/5.5, Sonnet 4/4.5/4.6/5/5.5, Haiku 3.5/4.5/5.5).
Update here when prices change.

A model that is not in the table is never priced silently: `price_info()` falls back to the most
recent listed model of its family (haiku, sonnet, opus, fable, mythos), or to DEFAULT when the family
is unknown, and says the price is NOT exact. `sessioncoach verify`, `sessioncoach report` and the dashboard («Data
quality») list those models with the requests and USD they affect.
"""
import collections
import re

TABLE_DATE = "2026-10-15"  # when the table below was read from the official page
PRICES = {
    "fable-5-1": (10, 50, 0.25, 20, 12.5),
    "mythos-5-1": (10, 50, 0.25, 20, 12.5),
    "fable-5": (10, 50, 1, 20, 12.5),
    "mythos-5": (10, 50, 1, 20, 12.5),
    "opus-5-5": (4, 20, 0.20, 8, 5),
    "opus-5": (5, 25, 0.50, 10, 6.25),
    "opus-4-8": (5, 25, 0.50, 10, 6.25),
    "opus-4-7": (5, 25, 0.50, 10, 6.25),
    "opus-4-6": (5, 25, 0.50, 10, 6.25),
    "opus-4-5": (5, 25, 0.50, 10, 6.25),
    "opus-4-1": (15, 75, 1.50, 30, 18.75),
    "opus-4": (15, 75, 1.50, 30, 18.75),
    "sonnet-5-5": (2, 10, 0.10, 4, 2.5),
    "sonnet-5": (2, 10, 0.20, 4, 2.5),
    "sonnet-4-6": (3, 15, 0.30, 6, 3.75),
    "sonnet-4-5": (3, 15, 0.30, 6, 3.75),
    "sonnet-4": (3, 15, 0.30, 6, 3.75),
    "haiku-5-5": (0.10, 0.50, 0.01, 0.20, 0.125),  # prompts up to LONG_PROMPT tokens
    "haiku-4-5": (1, 5, 0.10, 2, 1.25),
    "haiku-3-5": (0.80, 4, 0.08, 1.6, 1),
}
# Haiku 5.5 is dearer for prompts over 100,000 tokens (input + cache read + cache write of one request).
LONG_PROMPT = 100_000
LONG_PRICES = {"haiku-5-5": (0.50, 2.50, 0.05, 1, 0.625)}
DEFAULT = "opus-5"  # last resort for a model of no known family (still reported as unpriced)
FAMILY_FALLBACK = {"haiku": "haiku-5-5", "sonnet": "sonnet-5-5", "opus": "opus-5-5",
                   "fable": "fable-5-1", "mythos": "mythos-5-1"}
_DATED = re.compile(r"-\d{8}$")

def base_model(model):
    """'claude-opus-5-5[1m]' -> 'opus-5-5'."""
    return (model or "").replace("claude-", "").split("[")[0]

def _listed(m):
    """The PRICES key for model name `m` (exact, or with a -YYYYMMDD snapshot suffix), or None.

    Not a prefix match: 'opus-4-9' must not be taken for 'opus-4'.
    """
    m = _DATED.sub("", m)
    return m if m in PRICES else None

def family(model):
    m = base_model(model)
    return next((f for f in FAMILY_FALLBACK if m.startswith(f + "-") or m == f), None)

def price_info(model):
    """-> (prices, exact, key): `exact` is False when the model is not listed and a fallback was used."""
    key = _listed(base_model(model))
    if key:
        return PRICES[key], True, key
    key = FAMILY_FALLBACK.get(family(model)) or DEFAULT
    return PRICES[key], False, key

def context_tokens(r):
    """Context size of one request row: input + cache read + cache write tokens."""
    return (r["input"] or 0) + (r["cache_read"] or 0) + (r["cache_write"] or 0)

def sonnet_opus_ratio():
    """Sonnet 5.5 input price over Opus 5.5's: what moving a turn from Opus to Sonnet keeps of its cost."""
    return PRICES["sonnet-5-5"][0] / PRICES["opus-5-5"][0]

def price(model, r=None):
    """Price tuple of `model`; `r` (a request row) selects the long-prompt tier where one exists."""
    prices, _exact, key = price_info(model)
    if r is not None and key in LONG_PRICES and context_tokens(r) > LONG_PROMPT:
        return LONG_PRICES[key]
    return prices

def _cache_writes(r):
    cw1h, cw5m = r["cache_write_1h"], r["cache_write_5m"]
    if cw1h is None and cw5m is None:
        cw1h, cw5m = r["cache_write"] or 0, 0
    return cw1h or 0, cw5m or 0

def weight(model, r):
    """List-price USD of one request row (dict-like with token columns)."""
    pin, pout, pcr, pcw1h, pcw5m = price(model, r)
    cw1h, cw5m = _cache_writes(r)
    return ((r["input"] or 0) * pin + (r["output"] or 0) * pout + (r["cache_read"] or 0) * pcr
            + cw1h * pcw1h + cw5m * pcw5m) / 1e6

def token_cost_split(model, r):
    """List-price USD of one request split by token type."""
    pin, pout, pcr, pcw1h, pcw5m = price(model, r)
    cw1h, cw5m = _cache_writes(r)
    return {"input": (r["input"] or 0) * pin / 1e6, "output": (r["output"] or 0) * pout / 1e6,
            "cache_read": (r["cache_read"] or 0) * pcr / 1e6,
            "cache_write": (cw1h * pcw1h + cw5m * pcw5m) / 1e6}

def cache_ttl_s(r, default=3600):
    """Cache lifetime this request wrote with (3600 / 300 seconds), or None when it wrote nothing.

    The transcript says it: usage.cache_creation.ephemeral_1h_input_tokens vs ephemeral_5m_input_tokens.
    Rows with no breakdown (older transcripts) count as `default`, the 1 h of subscription sessions.
    """
    cw1h, cw5m = r["cache_write_1h"], r["cache_write_5m"]
    if cw1h is None and cw5m is None:
        return default if (r["cache_write"] or 0) > 0 else None
    if (cw1h or 0) > 0:
        return 3600
    if (cw5m or 0) > 0:
        return 300
    return None

def fmt_ttl(seconds):
    """3600 -> '1 h', 300 -> '5 min'."""
    return "%d h" % (seconds // 3600) if seconds >= 3600 else "%d min" % round(seconds / 60)

class TtlTracker:
    """Follows the cache lifetime of one transcript: the TTL of the latest request that wrote cache."""

    def __init__(self, default=3600):
        self.default, self.ttl = default, None

    def current(self):
        """TTL (seconds) in force before the next request: the last written one, else the default."""
        return self.ttl or self.default

    def update(self, r):
        t = cache_ttl_s(r, self.default)
        if t:
            self.ttl = t

def unpriced(rows, usd_by_model=None):
    """Models without an exact price: [{model, fallback, requests, list_usd, usd}] biggest first.

    `rows` are request rows; `list_usd` is their list-price USD under the fallback price, `usd` the
    USD of `usd_by_model` ({model label: usd}, e.g. from cost_slices) for that model when given.
    """
    agg = collections.OrderedDict()
    for r in rows:
        _p, exact, key = price_info(r["model"])
        if exact:
            continue
        m = base_model(r["model"]) or "?"
        a = agg.setdefault(m, {"model": m, "fallback": key, "requests": 0, "list_usd": 0.0})
        a["requests"] += 1
        a["list_usd"] += weight(r["model"], r)
    usd = collections.Counter()
    for label, v in (usd_by_model or {}).items():
        usd[base_model(label)] += v
    out = [dict(a, list_usd=round(a["list_usd"], 2), usd=round(usd[a["model"]], 2) if usd_by_model else None)
           for a in agg.values()]
    return sorted(out, key=lambda a: -(a["usd"] or a["list_usd"]))
