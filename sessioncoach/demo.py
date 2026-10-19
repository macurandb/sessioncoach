"""`sessioncoach demo`: a full dashboard on SYNTHETIC data, for screenshots, posts and a first look.

It writes fictional Claude Code transcripts (companies "acme", "globex", "initech"; projects such as
acme-shop or globex-api) under <data dir>/demo/ and feeds them through the real pipeline: ingest,
cost-state spreading, limits samples. Nothing is written to the database by hand, and nothing
outside the demo directory is touched: the real database, ~/.claude and the schedulers stay as they are.

Deterministic for a given seed and clock. The demo directory is marked with `.sessioncoach-demo` (`.kit-demo` before the rename); only a
directory with that marker is ever emptied and regenerated.
"""
import datetime as dt
import hashlib
import json
import os
import random
import shutil

from . import collect, cost, limits, pricing, store
from .collect import encode_dir

MARKER = ".sessioncoach-demo"
MARKERS = (MARKER, ".kit-demo")  # .kit-demo: demo dirs made before the rename
CODE_DIR = "/Users/demo/code"      # fictional working directories
SYNTHETIC_LINE = "synthetic demo transcript"

ACCOUNTS = {   # id: (label, company, org type, seat tier, monthly fee USD, cache TTL seconds written by its sessions)
    "personal": ("Personal · Max", "personal", "claude_max", "max_5x", 100, 3600),
    "team-acme": ("Team Acme", "acme", "claude_team", "premium", 30, 3600),
    "team-globex": ("Team Globex", "globex", "claude_team", "premium", 30, 3600),
    "api-initech": ("API · Initech", "initech", "api", None, None, 300),   # API key: 5-minute cache
}
PROJECTS = (   # name, tenant, account, model, probability of working on it on a weekday
    ("acme-shop", "acme", "team-acme", "claude-opus-5-5", 0.95),
    ("acme-admin", "acme", "team-acme", "claude-sonnet-5-5", 0.55),
    ("globex-api", "globex", "team-globex", "claude-opus-5-5[1m]", 0.95),
    ("globex-mobile", "globex", "personal", "claude-opus-5-5", 0.6),   # Globex work on the personal account
    ("initech-etl", "initech", "api-initech", "claude-opus-5", 0.7),
    ("dotfiles-tools", "personal", "personal", "claude-sonnet-5-5", 0.4),
)
SUBAGENTS = (("general-purpose", "claude-opus-5-5", 0.45), ("Explore", "claude-haiku-5-5", 0.3),
             ("Explore", "claude-sonnet-5-5", 0.15), ("code-reviewer", "claude-sonnet-5-5", 0.10))
TOOLS = ("Read", "Read", "Grep", "Bash", "Bash", "Edit", "Glob", "mcp__demo-docs__search")


def _hex(rng, n=8):
    return "%0*x" % (n, rng.getrandbits(4 * n))


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Session:
    def __init__(self, rng, project, mixed=False):
        self.proj = project
        self.sid = "%s-%s-%s-%s-%s" % (_hex(rng), _hex(rng, 4), _hex(rng, 4), _hex(rng, 4), _hex(rng, 12))
        self.ctx = rng.randint(14000, 26000)           # system prompt + CLAUDE.md
        self.last_t = None
        self.items = []                                 # (time, seq, line)
        self.sub_files = {}                             # agent id -> [(time, seq, line)]
        self.cost = 0.0
        self.factor = 1.0                               # recorded cost vs list price, fixed per session
        self.by_model = {}
        self.mixed = mixed
        self.seq = 0

    def add(self, t, line, sub=None):
        self.seq += 1
        (self.sub_files.setdefault(sub, []) if sub else self.items).append((t, self.seq, line))

    def bill(self, model, usd):
        self.cost += usd
        self.by_model[model] = self.by_model.get(model, 0.0) + usd


class Generator:
    def __init__(self, seed, days, now):
        self.rng = random.Random(seed)
        self.days, self.now = days, now
        self.sessions = []
        self.live = {}                  # project -> session still open across days

    # -- requests ---------------------------------------------------------------------------------
    def _usage(self, ttl, inp, out, cr, cw):
        u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}
        u["cache_creation"] = {"ephemeral_1h_input_tokens": cw if ttl == 3600 else 0,
                               "ephemeral_5m_input_tokens": cw if ttl != 3600 else 0}
        return u

    def _request(self, s, t, ttl, resumed, cwd=None, big=False):
        """One main-thread request; grows the session context. `resumed`: the cache expired, all of it is rewritten."""
        rng = self.rng
        cap = 640000 if s.proj[3].endswith("[1m]") else 185000
        grow = rng.randint(900, 4800) if not big else rng.randint(6000, 13000)
        out = int(min(6000, rng.lognormvariate(5.9, 0.7)))
        if resumed:
            cr, cw = 0, s.ctx
        else:
            cr, cw = s.ctx, grow
        s.ctx = s.ctx + grow + out if s.ctx + grow + out < cap else rng.randint(24000, 40000)  # past the cap: compacted
        model = s.proj[3]
        row = {"input": rng.randint(3, 40), "output": out, "cache_read": cr, "cache_write": cw,
               "cache_write_1h": cw if ttl == 3600 else 0, "cache_write_5m": cw if ttl != 3600 else 0}
        s.bill(model.replace("claude-", ""), pricing.weight(model, row))
        content = [{"type": "text", "text": SYNTHETIC_LINE}]
        if rng.random() < 0.6:
            content.append({"type": "tool_use", "id": "toolu_" + _hex(rng, 12), "name": rng.choice(TOOLS), "input": {}})
        mid = "msg_" + _hex(rng, 12)
        s.add(t, {"type": "assistant", "timestamp": _iso(t), "requestId": "req_" + _hex(rng, 12), "sessionId": s.sid,
                  "cwd": cwd or "%s/%s" % (CODE_DIR, s.proj[0]),
                  "message": {"id": mid, "model": model, "role": "assistant", "content": content,
                              "usage": self._usage(ttl, row["input"], out, cr, cw)}})
        return row

    def _subagent(self, s, t, ttl_sub=300):
        """A subagent run: its own transcript plus the completion report in the parent."""
        rng = self.rng
        typ, model, _ = self._pick_subagent()
        aid = "a" + _hex(rng, 10)
        n = rng.randint(5, 22)
        tokens, cur = 0, t
        ctx = rng.randint(6000, 12000)
        for _ in range(n):
            cur += dt.timedelta(seconds=rng.randint(8, 40))
            out = rng.randint(80, 1400)
            cw = rng.randint(2000, 9000)
            row = {"input": rng.randint(3, 12), "output": out, "cache_read": ctx, "cache_write": cw,
                   "cache_write_1h": 0, "cache_write_5m": cw}
            s.bill(model.replace("claude-", ""), pricing.weight(model, row))
            ctx += cw + out
            tokens += row["input"] + out + cw + (ctx - cw - out)
            s.add(cur, {"type": "assistant", "timestamp": _iso(cur), "requestId": "req_" + _hex(rng, 12), "sessionId": s.sid,
                        "message": {"id": "msg_" + _hex(rng, 12), "model": model, "role": "assistant",
                                    "content": [{"type": "text", "text": SYNTHETIC_LINE}],
                                    "usage": self._usage(ttl_sub, row["input"], out, row["cache_read"], cw)}}, sub=aid)
        tid = "toolu_" + _hex(rng, 12)
        s.add(t, {"type": "assistant", "timestamp": _iso(t), "sessionId": s.sid, "cwd": "%s/%s" % (CODE_DIR, s.proj[0]),
                  "message": {"role": "assistant", "content": [
                      {"type": "tool_use", "id": tid, "name": "Agent", "input": {"subagent_type": typ}}]}})
        s.add(cur + dt.timedelta(seconds=2), {
            "type": "user", "timestamp": _iso(cur + dt.timedelta(seconds=2)), "sessionId": s.sid,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": SYNTHETIC_LINE}]},
            "toolUseResult": {"agentId": aid, "resolvedModel": model, "totalTokens": tokens,
                              "totalToolUseCount": rng.randint(3, 30), "totalDurationMs": int((cur - t).total_seconds() * 1000)}})
        return cur

    def _pick_subagent(self):
        r = self.rng.random()
        acc = 0.0
        for typ, model, p in SUBAGENTS:
            acc += p
            if r < acc:
                return typ, model, p
        return SUBAGENTS[0]

    # -- sessions and days ---------------------------------------------------------------------------
    def _burst(self, s, start, ttl, end_limit, others):
        """A working period: requests every 15-100 s. The first one rewrites the cache if it expired."""
        rng = self.rng
        n = rng.randint(16, 64)
        t = start
        gap = (t - s.last_t).total_seconds() if s.last_t else 0
        switch_at = rng.randint(n // 3, n - 2) if s.mixed and others else None
        cwd = None
        for i in range(n):
            if t > end_limit:
                break
            resumed = i == 0 and s.last_t is not None and gap > ttl
            if switch_at is not None and i >= switch_at:
                cwd = "%s/%s" % (CODE_DIR, rng.choice(others))
            self._request(s, t, ttl, resumed, cwd, big=s.proj[3].endswith("[1m]"))
            s.last_t = t
            if rng.random() < 0.025:
                t = max(t, self._subagent(s, t))
                s.last_t = t
            t += dt.timedelta(seconds=rng.randint(15, 100))
        return t

    def _cost_state(self, s, t):
        factor = s.factor
        s.add(t + dt.timedelta(seconds=1), {"type": "cost-state", "totalCostUSD": round(s.cost * factor, 6),
                                            "modelUsage": {m: {"costUSD": round(v * factor, 6), "inputTokens": 1, "outputTokens": 1}
                                                           for m, v in s.by_model.items()}})

    def run_day(self, day, today):
        rng = self.rng
        weekend = day.weekday() >= 5
        for proj in PROJECTS:
            if rng.random() > (proj[4] * (0.18 if weekend else 1.0)):
                continue
            s = self.live.get(proj[0])
            if s is None or rng.random() < 0.3 or (s.ctx > 400000 and not proj[3].endswith("[1m]")):
                s = Session(rng, proj, mixed=rng.random() < 0.3)
                s.factor = rng.uniform(0.86, 1.08)
                self.sessions.append(s)
                self.live[proj[0]] = s
            ttl = ACCOUNTS[proj[2]][5]
            others = [p[0] for p in PROJECTS if p[0] != proj[0]]
            t = _local(day, rng.uniform(8.5, 11.5))
            end_of_day = _local(day, 19.8)
            for _ in range(rng.randint(1, 4)):
                if t > self.now or t > end_of_day:
                    break
                end = self._burst(s, t, ttl, min(self.now - dt.timedelta(minutes=2), end_of_day), others)
                if day != today or end < self.now - dt.timedelta(hours=1):
                    self._cost_state(s, end)       # today's tail stays "estimated"
                r = rng.random()
                gap = rng.uniform(7, 25) if r < 0.4 else rng.uniform(65, 170) if r < 0.75 else rng.uniform(30, 55)
                t = end + dt.timedelta(minutes=gap)

    def run_live(self):
        """A session working right now, so the plan window and today's curve are not empty."""
        rng = self.rng
        proj = PROJECTS[2]                                  # globex-api, 1M context
        s = Session(rng, proj)
        s.ctx, s.factor = 360000, 0.95
        self.sessions.append(s)
        ttl = ACCOUNTS[proj[2]][5]
        t = self.now - dt.timedelta(hours=3, minutes=20)
        others = [p[0] for p in PROJECTS if p[0] != proj[0]]
        while t < self.now - dt.timedelta(minutes=3):
            t = self._burst(s, t, ttl, self.now - dt.timedelta(minutes=2), others) + dt.timedelta(minutes=rng.uniform(1, 6))
            if rng.random() < 0.5:
                self._cost_state(s, t)


def _local(day, hour):
    h = int(hour)
    return dt.datetime.combine(day, dt.time(h, int((hour - h) * 60))).astimezone()


def _org(rng):
    return "%s-%s-%s-%s-%s" % (_hex(rng), _hex(rng, 4), _hex(rng, 4), _hex(rng, 4), _hex(rng, 12))


def build(dest, days=30, seed=7, now=None):
    """Generate the demo into `dest` (created, or regenerated if it carries the demo marker). Returns the database path.

    Only paths inside `dest` are written; the process environment is not changed.
    """
    dest = os.path.abspath(dest)
    if os.path.isdir(dest) and os.listdir(dest):
        if not any(os.path.exists(os.path.join(dest, m)) for m in MARKERS):
            raise SystemExit("sessioncoach demo: %s exists and is not a demo directory (no %s); not touching it." % (dest, MARKER))
        shutil.rmtree(dest)
    os.makedirs(dest, mode=0o700)
    with open(os.path.join(dest, MARKER), "w", encoding="utf-8") as fh:
        fh.write("synthetic data generated by `sessioncoach demo`; safe to delete\n")
    now = (now or dt.datetime.now().astimezone()).replace(microsecond=0)
    today = now.date()
    gen = Generator(seed, days, now)
    for i in range(days - 1, -1, -1):
        gen.run_day(today - dt.timedelta(days=i), today)
    gen.run_live()

    rng = random.Random(seed + 1)
    roots, acc_map = {}, {}
    for aid, (label, company, otype, seat, fee, _ttl) in ACCOUNTS.items():
        base = os.path.join(dest, "accounts", aid)
        os.makedirs(os.path.join(base, "projects"))
        org = _org(rng)
        with open(os.path.join(base, ".claude.json"), "w", encoding="utf-8") as fh:
            json.dump({"oauthAccount": {"organizationUuid": org, "organizationType": otype, "seatTier": seat,
                                        "hasExtraUsageEnabled": aid == "personal"}}, fh)
        roots[aid] = os.path.join(base, "projects")
        acc_map[aid] = {"label": label, "company": company, "org_hash": hashlib.sha256(org.encode()).hexdigest()[:12]}
        if fee:
            acc_map[aid]["monthly_fee_usd"] = fee
    for s in gen.sessions:
        pdir = os.path.join(roots[s.proj[2]], encode_dir("%s/%s" % (CODE_DIR, s.proj[0])))
        os.makedirs(pdir, exist_ok=True)
        _dump(os.path.join(pdir, s.sid + ".jsonl"), s.items)
        for aid, items in s.sub_files.items():
            sdir = os.path.join(pdir, s.sid, "subagents")
            os.makedirs(sdir, exist_ok=True)
            _dump(os.path.join(sdir, "agent-%s.jsonl" % aid), items)

    with open(os.path.join(dest, "projects.json"), "w", encoding="utf-8") as fh:
        json.dump({"rules": [{"match": "*" + encode_dir("%s/%s" % (CODE_DIR, p[0])), "project": p[0], "tenant": p[1]}
                             for p in PROJECTS]}, fh, indent=1)
    with open(os.path.join(dest, "accounts.json"), "w", encoding="utf-8") as fh:
        json.dump({"accounts": acc_map}, fh, indent=1)
    with open(os.path.join(dest, "alerts.json"), "w", encoding="utf-8") as fh:
        json.dump({"budgets": [
            {"name": "acme-month", "scope": "tenant", "match": "acme", "period": "month", "equiv_usd": 160},
            {"name": "globex-month", "scope": "tenant", "match": "globex", "period": "month", "equiv_usd": 420},
            {"name": "all-month", "scope": "all", "period": "month", "equiv_usd": 900}],
            "review": {"enabled": False}}, fh, indent=1)

    db = os.path.join(dest, "metrics.db")
    con = store.connect(db)
    try:
        collect.ingest(con, list(roots.values()))
        cost.rebuild_slices(con)
        _limit_samples(con, now, rng, os.path.join(next(iter(roots.values()))))
        _alert_events(con, now, days)
    finally:
        con.close()
    return db


def _dump(path, items):
    with open(path, "w", encoding="utf-8") as fh:
        for _t, _n, line in sorted(items, key=lambda x: (x[0], x[1])):
            fh.write(json.dumps(line) + "\n")


def _alert_events(con, now, days, limit=8):
    """Warnings for the dashboard's warnings view, as the real checks would have logged them on the synthetic pauses:
    a cache-expiry warning 40 min into every long pause of a big session, and the prompt hook's when the person came
    back after the cache expired. The outcomes are computed from the data like real ones."""
    from . import expiry
    start = now - dt.timedelta(days=days)
    con.execute("INSERT OR IGNORE INTO alerts_sent (key, ts) VALUES (?, ?)", (expiry.SINCE_KEY, int(start.timestamp())))
    rows = con.execute("SELECT * FROM requests WHERE kind = 'main' ORDER BY session_id, ts").fetchall()
    found = []
    for r, n in zip(rows, [*rows[1:], None]):
        ctx = (r["input"] or 0) + (r["cache_read"] or 0) + (r["cache_write"] or 0)
        t = cost.parse_ts(r["ts"])
        back = cost.parse_ts(n["ts"]) if n is not None and n["session_id"] == r["session_id"] else None
        idle = ((back or now) - t).total_seconds()
        if ctx >= expiry.DEFAULTS["min_context_tokens"] and idle >= 2700 and t + dt.timedelta(minutes=40) < now:
            found.append((r, ctx, t, back))
    for r, ctx, t, back in sorted(found, key=lambda x: x[2])[-limit:]:
        at = t + dt.timedelta(minutes=40)
        store.add_alert_event(con, "cache_expiry", {"last_ts": r["ts"], "context": ctx, "ttl_s": 3600, "idle_s": 2400,
                                                    "remaining_s": 1200}, session_id=r["session_id"],
                              project_dir=r["project_dir"], ts=int(at.timestamp()))
        if back is not None and (back - t).total_seconds() >= 3600:
            store.add_alert_event(con, "hook", {"last_ts": r["ts"], "context": ctx, "ttl_s": 3600, "limit_s": 3600,
                                                "idle_s": int((back - t).total_seconds())}, session_id=r["session_id"],
                                  project_dir=r["project_dir"], ts=int(back.timestamp()) - 1)
    con.commit()


def _limit_samples(con, now, rng, root):
    """Status-line samples through the real recorder (no notifications): a week of windows and the one running now."""
    cfg = dict(limits.DEFAULT_ALERTS, notify=False)
    sid = "demo-statusline"
    seven_reset = int((now + dt.timedelta(days=2, hours=3)).timestamp())
    samples = []
    for d in range(6, -1, -1):
        day = now.date() - dt.timedelta(days=d)
        if day.weekday() >= 5:
            continue
        start = _local(day, 9.0)
        for w in range(2):
            w0 = start + dt.timedelta(hours=5 * w + 0.5 * w)
            peak = rng.uniform(35, 92)
            for k in range(0, 60, 2):
                t = w0 + dt.timedelta(minutes=5 * k)
                if t >= now - dt.timedelta(hours=5):
                    continue
                samples.append((t, peak * (k / 59.0) ** 0.8, w0 + dt.timedelta(hours=5)))
    reset = now + dt.timedelta(hours=1, minutes=30)
    t0 = reset - dt.timedelta(hours=5)
    steps = int((now - t0).total_seconds() // 240)
    for k in range(steps + 1):
        samples.append((t0 + dt.timedelta(minutes=4 * k), 4 + 71 * (k / max(steps, 1)), reset))
    n = len(samples)
    for i, (t, pct, rst) in enumerate(samples):
        sev = 28 + 36 * i / max(n - 1, 1)
        limits.record_and_alert(con, {
            "session_id": sid, "model": {"id": "claude-opus-5-5"},
            "rate_limits": {"five_hour": {"used_percentage": round(pct, 1), "resets_at": int(rst.timestamp())},
                            "seven_day": {"used_percentage": round(sev, 1), "resets_at": seven_reset}},
            "context_window": {"used_percentage": 38, "current_usage": {"input_tokens": 10, "cache_read_input_tokens": 360000,
                                                                          "cache_creation_input_tokens": 4000}}},
            now=int(t.timestamp()), cfg=cfg, root=root)
    con.commit()
