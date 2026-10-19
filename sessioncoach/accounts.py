"""Which Claude account each transcript root belongs to, and which company it is tied to.

A Claude Code config dir is logged into one account. Its `.claude.json` (or ~/.claude.json for
the default dir) holds the login. Only non-personal fields are read: a hash of the organization
id, the organization type (e.g. claude_max, claude_team), the seat tier and the rate tier.
Email and names are never read or stored.

An optional private file (<data dir>/accounts.json) names accounts and ties each to a company:

  {"accounts": {
     "personal": {"label": "Personal · Max", "company": "personal", "org_hash": "ab12…", "monthly_fee_usd": 100},
     "team-acme": {"label": "Team Acme", "company": "acme", "org_hash": "cd34…"}}}

An account may declare accepted transitions: work of another company that runs on it on purpose
until a date, e.g. "transition": [{"tenant": "acme", "until": "2026-10-31"}]. While a transition
is current the mismatch is reported as acknowledged, not as a warning.
"""
import datetime as dt
import hashlib
import os

from . import paths


def config_dir_of(root):
    return os.path.dirname(os.path.normpath(os.path.expanduser(root)))


def detect(root):
    """Non-personal login fields for the config dir that owns `root`, or {}."""
    cfg = config_dir_of(root)
    candidates = [os.path.join(cfg, ".claude.json")]
    if os.path.normpath(cfg) == os.path.normpath(os.path.join(paths.HOME, ".claude")):
        candidates.insert(0, os.path.join(paths.HOME, ".claude.json"))
    for c in candidates:
        a = paths.load_json_object(c).get("oauthAccount")
        if not isinstance(a, dict):
            continue
        org = a.get("organizationUuid") or a.get("accountUuid")
        if not org:
            continue
        return {"org_hash": hashlib.sha256(org.encode()).hexdigest()[:12], "org_type": a.get("organizationType"),
                "seat_tier": a.get("seatTier"), "rate_tier": a.get("organizationRateLimitTier") or a.get("userRateLimitTier"),
                "extra_usage": 1 if a.get("hasExtraUsageEnabled") else 0}
    return {}


def load_map(path=None):
    path = path or os.path.join(paths.data_dir(), "accounts.json")
    accs = paths.load_json_object(path).get("accounts")
    return {k: v for k, v in accs.items() if isinstance(v, dict)} if isinstance(accs, dict) else {}


class LoginClock:
    """(root, time) -> seen_at of the login a config dir had at that time, None before the first one.

    A config dir can switch accounts (/login). Each switch is recorded by the next ingest, so the
    time is that of the ingest, not of the login: activity in between counts for the old account.
    """

    def __init__(self, seen_rows, default_root=None):
        self.default_root = default_root
        self.history = {}
        for r in seen_rows:
            t = _parse(r["seen_at"])
            if t:
                self.history.setdefault(r["root"], []).append((t, r["seen_at"]))
        for h in self.history.values():
            h.sort()

    def __call__(self, root, t):
        out = None
        for at, seen_at in self.history.get(root or self.default_root) or []:
            if at > t:
                break
            out = seen_at
        return out


def _parse(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


class AccountResolver:
    """(root, ts) -> account dict {id, label, company, org_type, seat_tier}."""

    def __init__(self, seen_rows, mapping=None, default_root=None):
        self.mapping = mapping or {}
        self.by_hash = {v.get("org_hash"): k for k, v in self.mapping.items() if v.get("org_hash")}
        self.history = {}
        for r in seen_rows:  # rows from accounts_seen, any order
            self.history.setdefault(r["root"], []).append((r["seen_at"], dict(r)))
        for h in self.history.values():
            h.sort(key=lambda x: x[0])
        self.default_root = default_root

    def ids(self, root):
        """Every account id the config dir that owns `root` has been logged into."""
        h = self.history.get(root or self.default_root) or []
        return {self(root, at)["id"] for at, _row in h} if h else {self(root)["id"]}

    def __call__(self, root, ts=None):
        root = root or self.default_root
        h = self.history.get(root) or []
        if not h:
            return {"id": "unknown", "label": "unknown", "company": None, "org_type": None, "seat_tier": None,
                    "monthly_fee_usd": None, "extra_usage": False, "transition": []}
        seen = h[0][1]
        for at, row in h:  # latest login recorded at or before ts (earliest one for older rows)
            if ts and at <= ts:
                seen = row
        acc_id = self.by_hash.get(seen.get("org_hash"))
        m = self.mapping.get(acc_id, {}) if acc_id else {}
        label = m.get("label") or _auto_label(seen)
        return {"id": acc_id or ("auto:" + (seen.get("org_hash") or "?")), "label": label, "company": m.get("company"),
                "org_type": seen.get("org_type"), "seat_tier": seen.get("seat_tier"),
                "monthly_fee_usd": m.get("monthly_fee_usd"), "extra_usage": bool(seen.get("extra_usage")),
                "transition": m.get("transition") or []}


def _auto_label(seen):
    t = (seen.get("org_type") or "claude").replace("claude_", "").capitalize()
    tier = seen.get("seat_tier") or (seen.get("rate_tier") or "").replace("default_claude_", "").replace("_", " ")
    return ("%s · %s" % (t, tier)).strip(" ·")
