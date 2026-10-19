"""Aggregates and recommendations for the report and the dashboard.

Every figure says where it comes from: `recorded` (Claude Code cost-state, spread by
activity), `estimated` (list price, after the last cost-state snapshot) or `list_price`
(token counts x list price, used for breakdowns that cost-state does not give).
"""
import collections
import datetime as dt

from . import accounts, config, cost, paths, pricing, rules
from .projects import UNASSIGNED, Labeler

TOP_PROJECTS = 7          # stacked chart keeps 7 projects + "other" (8 categorical slots)
RESUME_IDLE_S = 3600      # cache TTL assumed when a session's transcript does not say (subscription sessions: 1 h)
RESUME_MIN_WRITE = 50000  # a "big" cache rewrite
LONG_CONTEXT = rules.LONG_CONTEXT  # default R16 threshold (kept here for callers; alerts.json can change it)


class ResumeDetector:
    """Finds the requests that rewrite a big cache after the cache of their session expired.

    The cache lifetime is the real one of each transcript: 1 h when it writes `ephemeral_1h`,
    5 min when it writes `ephemeral_5m` (API key, usage credits, subagents), RESUME_IDLE_S when the
    transcript does not say. Feed the requests of each transcript in time order, one `key` per transcript.
    """

    def __init__(self):
        self._prev, self._ttl = {}, {}

    def feed(self, key, t, r):
        """-> (is_resume, ttl_s): is `r` a big cache rewrite after the cache expired, and the TTL applied."""
        tr = self._ttl.setdefault(key, pricing.TtlTracker(RESUME_IDLE_S))
        ttl, prev = tr.current(), self._prev.get(key)
        self._prev[key] = t
        tr.update(r)
        gap = (t - prev).total_seconds() if prev else 0
        return gap > ttl and (r["cache_write"] or 0) >= RESUME_MIN_WRITE, ttl

def resumes_since(con, days=7, now=None):
    """-> (count, list-price USD of their cache writes): main-session resumes after the cache expired in the last `days`.
    The day before the window is read too, so a pause that started before it (an evening to the next morning) is still found."""
    now = now or dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=days)
    lo = (start - dt.timedelta(days=1)).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    det, n, usd = ResumeDetector(), 0, 0.0
    for r in select(con, "requests", [ts_range(lo, None), ("kind = 'main'", [])], order="project_dir, file, ts, rowid"):
        t = cost.parse_ts(r["ts"])
        if t is None:
            continue
        hit, _ttl = det.feed((r["project_dir"], r["file"]), t, r)
        if hit and t >= start:
            n += 1
            usd += pricing.token_cost_split(r["model"], r)["cache_write"]
    return n, usd

fmt_ttl = pricing.fmt_ttl  # kept here for callers

def window(days, today=None):
    today = today or dt.date.today()
    if not days:
        return None, today
    return today - dt.timedelta(days=days - 1), today


def _in(day, start, end):
    return (start is None or day >= start.isoformat()) and day <= end.isoformat()


def _week(day):
    d = dt.date.fromisoformat(day)
    return (d - dt.timedelta(days=d.weekday())).isoformat()


MAX_IN = 500  # longest IN (...) list pushed to SQLite; above it the Python filter alone does the work
SLACK_DAYS = 2  # ts is UTC text and days are local: bound ts by this many days more on each side, Python decides the rest

def ts_bounds(start, end):
    """(lo, hi) text bounds for `ts >= lo AND ts < hi` that contain every request with a local day in [start, end]."""
    lo = (start - dt.timedelta(days=SLACK_DAYS)).isoformat() if start else None
    return lo, (end + dt.timedelta(days=SLACK_DAYS + 1)).isoformat()

def sql_where(*parts):
    """-> (' WHERE a AND b', params) from (sql, params) parts; parts whose sql is empty are skipped."""
    parts = [p for p in parts if p[0]]
    return (" WHERE " + " AND ".join(p[0] for p in parts) if parts else ""), [v for p in parts for v in p[1]]

def select(con, table, parts, order="rowid", cols="*"):
    """Rows of `table` narrowed by (sql, params) `parts`, in the order the old full read gave them.

    `order` keeps that order explicit: with a WHERE SQLite may pick an index and return rows
    sorted differently, and sums or ties downstream would change with it.
    """
    where, args = sql_where(*parts)
    return con.execute("SELECT %s FROM %s%s ORDER BY %s" % (cols, table, where, order), args)

def ts_range(lo, hi, col="ts"):
    sql = " AND ".join(c for c in ("%s >= ?" % col if lo else "", "%s < ?" % col if hi else "") if c)
    return sql, [v for v in (lo, hi) if v]

def day_range(lo, hi, col="day"):
    sql = " AND ".join(c for c in ("%s >= ?" % col if lo else "", "%s <= ?" % col if hi else "") if c)
    return sql, [v for v in (lo, hi) if v]

class Dims:
    """Project, tenant and Claude account of a row."""

    def __init__(self, con, labeler=None, resolver=None):
        self.labeler = labeler or Labeler.load()
        self.resolver = resolver or accounts.AccountResolver(
            list(con.execute("SELECT * FROM accounts_seen")), accounts.load_map(), paths.default_root())

    def __call__(self, row, ts=None):
        p, t = self.labeler(row["project_dir"])
        root = row["root"] if "root" in row.keys() else None  # noqa: SIM118 (sqlite3.Row: `in` would search values, not column names)
        return p, t, self.resolver(root, ts)

    def scope(self, con, tenant=None, project=None, account=None, tables=("cost_slices", "requests", "subagents")):
        """-> (sql, params): rows of `tables` whose project dir and root pass the filters, for a WHERE clause.

        Tenant and project depend only on the project dir, the account only on the root, so each side
        is resolved once per distinct value and pushed to SQLite as IN lists. Empty sql: no filter set,
        or too many values to list (the Python filter on the rows then does all the work).
        """
        sql, params = [], []
        if tenant or project:
            dirs = {r[0] for t in tables for r in con.execute("SELECT DISTINCT project_dir FROM %s" % t)}
            ok = sorted(d for d in dirs if (not tenant or self.labeler(d)[1] == tenant)
                        and (not project or self.labeler(d)[0] == project))
            if len(ok) <= MAX_IN:
                sql.append("project_dir IN (%s)" % ",".join("?" * len(ok)))
                params += ok
        if account:
            roots = {r[0] for t in tables for r in con.execute("SELECT DISTINCT root FROM %s" % t)}
            ok = sorted(r for r in roots if r is not None and self.resolver(r, None)["id"] == account)
            if len(ok) <= MAX_IN:
                null_ok = self.resolver(None, None)["id"] == account  # NULL root = the default transcript root
                sql.append("(root IN (%s)%s)" % (",".join("?" * len(ok)), " OR root IS NULL" if null_ok else ""))
                params += ok
        return " AND ".join(sql), params


def build(con, days=30, tenant=None, project=None, account=None, labeler=None, today=None, resolver=None):
    dims = Dims(con, labeler, resolver)
    labeler = dims.labeler
    start, end = window(days, today)

    def keep(row):
        p, t, a = dims(row)
        return (not tenant or t == tenant) and (not project or p == project) and (not account or a["id"] == account)

    # SQLite narrows by scope and date; the Python checks below stay as the exact rule
    scope = dims.scope(con, tenant, project, account)
    pstart, pend = (start - (end - start) - dt.timedelta(days=1), start - dt.timedelta(days=1)) if start else (None, None)
    slices = [s for s in select(con, "cost_slices", [scope, day_range((pstart or start).isoformat() if start else None,
                                                                      end.isoformat())]) if keep(s)]
    cur = [s for s in slices if _in(s["day"], start, end)]
    prev = [s for s in slices if _in(s["day"], pstart, pend)] if start else []

    parts = [scope, ("ts IS NOT NULL", []), ts_range(*ts_bounds(start, end))]
    reqs = [r for r in select(con, "requests", parts) if r["ts"] and _in(_local_day(r["ts"]), start, end) and keep(r)]
    subs = [s for s in select(con, "subagents", parts) if s["ts"] and _in(_local_day(s["ts"]), start, end) and keep(s)]

    data = {
        "window": {"days": days, "start": start.isoformat() if start else None, "end": end.isoformat()},
        "filters": {"tenant": tenant, "project": project, "account": account},
        "summary": _summary(cur, prev, reqs),
        "weekly": _weekly(cur, labeler),
        "projects": _projects(cur, reqs, labeler, dims),
        "accounts": _accounts(cur, dims, _window_days(start, end, cur)),
        "models": _models(cur),
        "token_split": _token_split(reqs),
        "cache_share": _cache_share(reqs),
        "pricing": {"unpriced": pricing.unpriced(reqs, _usd_by_model(cur)), "table_date": pricing.TABLE_DATE},
        "subagents": _subagents(subs),
        "recommendations": recommendations(cur, reqs, subs) + _account_mismatch(cur, dims, end),
        "alignment": _alignment(cur, dims),
        "options": _options(con, dims),
    }
    return data


def _local_day(ts):
    t = cost.parse_ts(ts)
    return t.date().isoformat() if t else ""


def _summary(cur, prev, reqs):
    rec = sum(s["usd"] for s in cur if not s["estimated"])
    est = sum(s["usd"] for s in cur if s["estimated"])
    prev_total = sum(s["usd"] for s in prev)
    days = {s["day"] for s in cur}
    return {"recorded_usd": rec, "estimated_usd": est, "total_usd": rec + est,
            "prev_total_usd": prev_total if prev else None, "prev_active_days": len({s["day"] for s in prev}),
            "active_days": len(days),
            "sessions": len({(r["project_dir"], r["session_id"]) for r in reqs}), "requests": len(reqs)}


def _weekly(cur, labeler):
    by_proj = collections.Counter()
    for s in cur:
        by_proj[labeler(s["project_dir"])[0]] += s["usd"]
    top = [p for p, _ in by_proj.most_common(TOP_PROJECTS)]
    series = top + (["other"] if len(by_proj) > len(top) else [])
    weeks = collections.defaultdict(collections.Counter)
    for s in cur:
        p = labeler(s["project_dir"])[0]
        weeks[_week(s["day"])][p if p in top else "other"] += s["usd"]
    return {"series": series,
            "weeks": [{"week": w, "values": {k: round(v, 2) for k, v in c.items()}} for w, c in sorted(weeks.items())]}


def _projects(cur, reqs, labeler, dims):
    agg = collections.defaultdict(lambda: {"usd": 0.0, "estimated_usd": 0.0, "opus_usd": 0.0, "sessions": set(),
                                           "accounts": collections.Counter()})
    for s in cur:
        p, t, acc = dims(s)
        a = agg[(p, t)]
        a["accounts"][acc["label"]] += s["usd"]
        a["usd"] += s["usd"]
        a["estimated_usd"] += s["usd"] if s["estimated"] else 0.0
        a["opus_usd"] += s["usd"] if "opus" in s["model"] else 0.0
    for r in reqs:
        agg[labeler(r["project_dir"])]["sessions"].add(r["session_id"])
    rows = [{"project": p, "tenant": t, "account": (a["accounts"].most_common(1) or [("-", 0)])[0][0],
             "usd": round(a["usd"], 2), "estimated_usd": round(a["estimated_usd"], 2),
             "opus_share": round(a["opus_usd"] / a["usd"], 3) if a["usd"] else None, "sessions": len(a["sessions"])}
            for (p, t), a in agg.items() if a["usd"] or a["sessions"]]
    return sorted(rows, key=lambda r: -r["usd"])


def _models(cur):
    c = collections.Counter()
    for s in cur:
        c[s["model"]] += s["usd"]
    total = sum(c.values()) or 1.0
    return [{"model": m, "usd": round(v, 2), "share": round(v / total, 3)} for m, v in c.most_common()]


def _token_split(reqs):
    c = collections.Counter()
    for r in reqs:
        for k, v in pricing.token_cost_split(r["model"], r).items():
            c[k] += v
    total = sum(c.values()) or 1.0
    order = ("cache_read", "cache_write", "output", "input")
    return [{"type": k, "usd": round(c[k], 2), "share": round(c[k] / total, 3)} for k in order]


def _usd_by_model(slices):
    c = collections.Counter()
    for s in slices:
        c[s["model"]] += s["usd"]
    return c

def _cache_share(reqs):
    """THE cache percentage: share of the list-price USD of all activity in the window that goes to the
    prompt cache (reads + writes). Same base as `token_split`. It is not computed from cost-state
    (which has no cost per token type), so it can differ from any figure derived from recorded cost only."""
    split = _token_split(reqs)
    by = {x["type"]: x["share"] for x in split}
    return {"share": round(by["cache_read"] + by["cache_write"], 3), "read_share": by["cache_read"],
            "write_share": by["cache_write"], "basis": "list_price", "scope": "all_activity", "requests": len(reqs)}

def _subagents(subs):
    c = collections.defaultdict(collections.Counter)
    for s in subs:
        key = (s["subagent_type"] or "?", pricing.base_model(s["model"]) or "?")
        c[key].update(agents=1, tokens=s["tokens"] or 0, duration_ms=s["duration_ms"] or 0)
    return [{"type": t, "model": m, "agents": v["agents"], "tokens": v["tokens"],
             "avg_min": round(v["duration_ms"] / v["agents"] / 60000, 1)} for (t, m), v in
            sorted(c.items(), key=lambda kv: -kv[1]["tokens"])]


def _resume_hits(reqs):
    """[(request, ttl)]: main-session requests that rewrite a big cache after it expired, per transcript in time order."""
    by_file = collections.defaultdict(list)
    for r in reqs:
        if r["kind"] == "main":
            t = cost.parse_ts(r["ts"])
            if t:
                by_file[(r["project_dir"], r["file"])].append((t, r))
    hits, det = [], ResumeDetector()
    for key, rows in by_file.items():
        rows.sort(key=lambda x: x[0])
        for t, r in rows:
            hit, ttl = det.feed(key, t, r)
            if hit:
                hits.append((r, ttl))
    return hits


def recommendations(cur, reqs, subs, long_context=None):
    """Deterministic rules over the window (rules.DASHBOARD). Savings are estimates and labelled as such.
    R16's threshold is `long_context`, else alerts.json → review.long_context_tokens (the daily review's too)."""
    long_context = long_context or config.review_config()["long_context_tokens"]
    return rules.recommend(rules.Window(cur, reqs, subs, _resume_hits(reqs), int(long_context)))


def _options(con, dims):
    tenants, projects, accts = set(), set(), {}
    for row in con.execute("SELECT DISTINCT project_dir, root FROM cost_slices"):
        p, t, a = dims(row)
        tenants.add(t)
        projects.add(p)
        accts[a["id"]] = a["label"]
    return {"tenants": sorted(tenants), "projects": sorted(projects),
            "accounts": [{"id": k, "label": v} for k, v in sorted(accts.items(), key=lambda kv: kv[1])]}


DAYS_PER_MONTH = 30.44


def _window_days(start, end, cur):
    if start:
        return (end - start).days + 1
    days = sorted({s["day"] for s in cur})
    return (dt.date.fromisoformat(days[-1]) - dt.date.fromisoformat(days[0])).days + 1 if days else 0


def _accounts(cur, dims, window_days=30):
    """Cost per Claude account, with the company it is tied to and spend from other companies.

    On a subscription the API-equivalent is the value consumed, not what is paid: what is paid is
    the plan fee (prorated to the window), plus extra usage only if the account has it enabled.
    """
    agg = {}
    for s in cur:
        p, t, a = dims(s)
        row = agg.setdefault(a["id"], {"id": a["id"], "label": a["label"], "company": a["company"],
                                       "org_type": a["org_type"], "seat_tier": a["seat_tier"], "usd": 0.0,
                                       "monthly_fee_usd": a["monthly_fee_usd"], "extra_usage": a["extra_usage"],
                                       "estimated_usd": 0.0, "projects": set(), "other_company_usd": 0.0})
        row["usd"] += s["usd"]
        row["estimated_usd"] += s["usd"] if s["estimated"] else 0.0
        row["projects"].add(p)
        if a["company"] and t not in (a["company"], UNASSIGNED):
            row["other_company_usd"] += s["usd"]
    out = []
    for r in agg.values():
        r["projects"] = len(r["projects"])
        for k in ("usd", "estimated_usd", "other_company_usd"):
            r[k] = round(r[k], 2)
        fee = r["monthly_fee_usd"]
        r["fee_period_usd"] = round(fee * window_days / DAYS_PER_MONTH, 2) if fee and window_days else None
        r["value_multiple"] = round(r["usd"] / r["fee_period_usd"], 1) if r["fee_period_usd"] else None
        out.append(r)
    return sorted(out, key=lambda r: -r["usd"])


def _account_mismatch(cur, dims, today=None):
    """R14: work of one company running on an account tied to another company.

    Accepted transitions (declared on the account, with an end date) are reported as acknowledged
    and ranked last; once the date passes they become warnings again.
    """
    today = (today or dt.date.today()).isoformat()
    agg, trans = collections.defaultdict(float), {}
    for s in cur:
        _p, t, a = dims(s)
        if a["company"] and t not in (a["company"], UNASSIGNED):
            agg[(t, a["label"], a["company"])] += s["usd"]
            for tr in a.get("transition") or []:
                if tr.get("tenant") == t:
                    trans[(t, a["label"], a["company"])] = tr.get("until")
    out = []
    for (t, lab, comp), v in sorted(agg.items(), key=lambda kv: (bool(trans.get(kv[0])) and today <= trans[kv[0]], -kv[1])):
        until = trans.get((t, lab, comp))
        ack = bool(until) and today <= until
        out.append({"id": "R14", "saving_usd": None, "confidence": "low" if ack else "high", "effort": "config",
                    "acknowledged": ack, "params": {"tenant": t, "account": lab, "company": comp, "usd": round(v, 2),
                                                    "until": until, "expired": bool(until) and not ack},
                    "verify_metric": "account_mismatch"})
    return out


def _alignment(cur, dims):
    """Share of each company's spend that runs on an account tied to that company."""
    agg = collections.defaultdict(lambda: [0.0, 0.0])
    for s in cur:
        _p, t, a = dims(s)
        if t == UNASSIGNED:
            continue
        agg[t][0] += s["usd"]
        if a["company"] == t:
            agg[t][1] += s["usd"]
    rows = [{"tenant": t, "usd": round(v[0], 2), "aligned_usd": round(v[1], 2),
             "pct": round(v[1] / v[0], 3) if v[0] else None} for t, v in sorted(agg.items(), key=lambda kv: -kv[1][0])]
    tot = sum(v[0] for v in agg.values())
    return {"overall_pct": round(sum(v[1] for v in agg.values()) / tot, 3) if tot else None, "tenants": rows}


def day(con, date=None, tenant=None, project=None, account=None, labeler=None, resolver=None, now=None):
    """One day compared with the previous one: totals, cumulative cost by hour, by account and project.

    Daily cost comes from cost_slices; it is spread over the hours of the day in proportion to the
    list-price weight of each session's requests, so the hourly curve adds up to the daily figure.
    """
    dims = Dims(con, labeler, resolver)
    now = now or dt.datetime.now().astimezone()
    date = date or now.date()
    prev_date = date - dt.timedelta(days=1)
    days = (date.isoformat(), prev_date.isoformat())

    def keep(row):
        p, t, a = dims(row)
        return (not tenant or t == tenant) and (not project or p == project) and (not account or a["id"] == account)

    slices = [s for s in select(con, "cost_slices", [dims.scope(con, tenant, project, account), ("day IN (?, ?)", days)],
                                order="day, rowid") if keep(s)]
    weights = collections.defaultdict(collections.Counter)  # (day, pdir, sid) -> {hour: weight}
    # weights are only read for the kept slices' project dirs; ts is UTC text, days local: Python decides below
    for r in select(con, "requests", [dims.scope(con, tenant, project),
                                      ts_range((prev_date - dt.timedelta(days=1)).isoformat(),
                                               (date + dt.timedelta(days=SLACK_DAYS + 1)).isoformat())], order="ts, rowid"):
        t = cost.parse_ts(r["ts"])
        if t and t.date().isoformat() in days:
            weights[(t.date().isoformat(), r["project_dir"], r["session_id"])][t.hour] += pricing.weight(r["model"], r) or 1e-9
    hourly = {d: [0.0] * 24 for d in days}
    totals = {d: {"usd": 0.0, "estimated_usd": 0.0} for d in days}
    by_acc, by_proj = collections.defaultdict(lambda: {d: 0.0 for d in days}), collections.defaultdict(lambda: {d: 0.0 for d in days})
    labels = {}
    for s in slices:
        d = s["day"]
        totals[d]["usd"] += s["usd"]
        totals[d]["estimated_usd"] += s["usd"] if s["estimated"] else 0.0
        w = weights.get((d, s["project_dir"], s["session_id"]))
        if w:
            tw = sum(w.values())
            for h, v in w.items():
                hourly[d][h] += s["usd"] * v / tw
        else:
            hourly[d][12] += s["usd"]
        p, t, a = dims(s)
        by_acc[a["id"]][d] += s["usd"]
        labels[a["id"]] = (a["label"], a["company"], a["monthly_fee_usd"], a["extra_usage"])
        by_proj[(p, t)][d] += s["usd"]
    cum = {d: [round(sum(hourly[d][:h + 1]), 2) for h in range(24)] for d in days}
    is_today = date == now.date()
    cut = now.hour
    same_time_prev = cum[days[1]][cut] if is_today else None
    return {
        "date": days[0], "prev_date": days[1], "is_today": is_today, "hour_now": cut if is_today else None,
        "totals": {"today": {k: round(v, 2) for k, v in totals[days[0]].items()},
                   "prev": {k: round(v, 2) for k, v in totals[days[1]].items()},
                   "prev_same_time_usd": same_time_prev},
        "cumulative": {"today": cum[days[0]][:cut + 1] if is_today else cum[days[0]], "prev": cum[days[1]]},
        "accounts": sorted([{"id": k, "label": labels[k][0], "company": labels[k][1],
                             "fee_day_usd": round(labels[k][2] / DAYS_PER_MONTH, 2) if labels[k][2] else None,
                             "extra_usage": labels[k][3], "today": round(v[days[0]], 2),
                             "prev": round(v[days[1]], 2)} for k, v in by_acc.items()], key=lambda r: -(r["today"] + r["prev"])),
        "projects": sorted([{"project": p, "tenant": t, "today": round(v[days[0]], 2), "prev": round(v[days[1]], 2)}
                            for (p, t), v in by_proj.items()], key=lambda r: -(r["today"] + r["prev"])),
        "last_ingest": (con.execute("SELECT MAX(ts) FROM ingest_runs").fetchone() or [None])[0],
    }


def limits_view(con, now=None, labeler=None, resolver=None):
    """Latest plan-limit sample, recent history, and what consumed the current 5-hour window.

    The window share is computed from list-price weights of the requests made since the window
    started (reset time - 5 h). It shows what is eating the window; the percentage itself is
    Anthropic's, read from the status line.
    """
    now = now or dt.datetime.now().astimezone()
    last = con.execute("SELECT * FROM limits_samples ORDER BY ts DESC LIMIT 1").fetchone()
    hist = [dict(r) for r in con.execute(
        "SELECT ts, five_pct, five_reset, seven_pct, seven_reset FROM limits_samples WHERE ts >= ? ORDER BY ts",
        (int(now.timestamp()) - 7 * 86400,))]
    out = {"last": dict(last) if last else None, "history": hist, "window": None, "eta": {}}
    if last:
        from . import limits
        for key, col in (("five_hour", "five"), ("seven_day", "seven")):
            if last[col + "_pct"] is not None and last[col + "_reset"]:
                eta = limits.pace(con, key, last[col + "_reset"], last["ts"], last[col + "_pct"])
                if eta and eta < last[col + "_reset"]:
                    out["eta"][key] = eta
    if not last or not last["five_reset"]:
        return out
    start = dt.datetime.fromtimestamp(last["five_reset"] - 5 * 3600).astimezone()
    end = dt.datetime.fromtimestamp(last["five_reset"]).astimezone()
    dims = Dims(con, labeler, resolver)
    by = {"project": collections.Counter(), "session": collections.Counter(), "model": collections.Counter(),
          "driver": collections.Counter()}
    total = 0.0
    det = ResumeDetector()
    rows = [r for r in con.execute("SELECT * FROM requests WHERE ts >= ?",
                                   ((start - dt.timedelta(hours=2)).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),))]
    rows.sort(key=lambda r: r["ts"])
    for r in rows:
        t = cost.parse_ts(r["ts"])
        if not t:
            continue
        resumed, _ttl = det.feed((r["project_dir"], r["file"]), t, r)
        if not start <= t <= end:
            continue
        w = pricing.weight(r["model"], r)
        split = pricing.token_cost_split(r["model"], r)
        total += w
        p, _ten, _a = dims(r)
        by["project"][p] += w
        by["session"]["%s · %s" % (p, (r["session_id"] or "")[:8])] += w
        by["model"][pricing.base_model(r["model"])] += w
        if r["kind"] == "subagent":
            by["driver"]["subagents"] += w
        elif resumed:
            by["driver"]["cache_rewrite_after_idle"] += split["cache_write"]
            by["driver"]["main_other"] += w - split["cache_write"]
        else:
            by["driver"]["main_cache_read"] += split["cache_read"]
            by["driver"]["main_other"] += w - split["cache_read"]

    def top(c, n=6):
        return [{"name": k, "usd": round(v, 2), "share": round(v / total, 3) if total else 0} for k, v in c.most_common(n)]
    out["window"] = {"start": start.isoformat(), "end": end.isoformat(), "list_price_usd": round(total, 2),
                     "by_project": top(by["project"]), "by_session": top(by["session"]), "by_model": top(by["model"]),
                     "by_driver": top(by["driver"])}
    return out


EXPLORATION_TOOLS = ("Read", "Grep", "Glob", "LS")


def tool_family(name):
    """'mcp__<server>__<tool>' → 'mcp:<server>'; built-in tools keep their name."""
    if name.startswith("mcp__"):
        return "mcp:" + name[5:].split("__", 1)[0]
    return name


def tools_view(con, days=30, project=None, today=None):
    """Tool calls per day for R6: exploration (Read/Grep/Glob) vs each MCP server. Counts only."""
    start, end = window(days, today)
    per_day, totals = {}, {}
    for r in select(con, "tool_calls", [("ts IS NOT NULL", []), ("project_dir LIKE ?", ["%" + project + "%"]) if project else ("", []),
                                        ts_range(*ts_bounds(start, end))], cols="project_dir, ts, name"):
        day = _local_day(r["ts"])
        if not day or not _in(day, start, end):
            continue
        fam = tool_family(r["name"])
        # Bash apart: it is where most exploration goes (cat/grep/sed), but it also builds and runs things
        bucket = ("exploration" if fam in EXPLORATION_TOOLS else "Bash" if fam == "Bash"
                  else fam if fam.startswith("mcp:") else None)
        totals[fam] = totals.get(fam, 0) + 1
        if bucket:
            row = per_day.setdefault(day, {})
            row[bucket] = row.get(bucket, 0) + 1
    return {"window": {"start": start, "end": end},
            "totals": sorted(totals.items(), key=lambda kv: -kv[1]),
            "per_day": sorted(per_day.items())}


IDLE_SUMMARY_OUT = 20000  # tokens of a summary when no compaction says (they ran 11k-22k here)
IDLE_MIN_CASES = 5        # fewer returns than this on either side and the answer is "not enough data"
IDLE_RAISE_TO = 400000    # the higher threshold weighed against the default


def idle_compact_view(con, days=30, now=None):
    """Would CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS at 100k pay off? Main-session returns after a pause of FIRE_S or more on
    a 1 h cache, in the last `days`, by the context of the turn before the pause (what Claude Code compares):
    - `covered` (≥ the 200k default): how often its summary was applied (a precomputed compaction between the pause and
      the return), counted from the first one seen, so days before the feature reached this machine do not dilute it;
    - `between` (100k-200k): what they paid to re-write the context, and what a summary would have cost instead
      (re-read at cache-read price, written, and cached once more);
    - `left` (100k-200k, never resumed): the summary is prepared anyway, and paid for nothing.
    The saving is `rate` x (the difference minus that waste), negative when it costs more: a summary is paid only when
    it fires, and saves only then. `above` weighs raising it to IDLE_RAISE_TO the other way round: the 200k-400k
    returns would re-write their whole context (estimated at the 1 h write price when a summary spared it), and their
    summaries, also those of the sessions left there, would no longer be paid."""
    from .idlecompact import DEFAULT_MIN_TOKENS, FIRE_S, FLOOR_MIN_TOKENS
    now = now or dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=days)
    lo = (start - dt.timedelta(days=1)).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    comps, outs = {}, []
    if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'compactions'").fetchone():
        for c in select(con, "compactions", [ts_range(lo, None), ("precomputed = 1", [])], order="ts"):
            t = cost.parse_ts(c["ts"])
            if t is not None:
                comps.setdefault(c["session_id"], []).append(t)
                outs += [c["post_tokens"]] if c["post_tokens"] else []
    out_tokens = sorted(outs)[len(outs) // 2] if outs else IDLE_SUMMARY_OUT
    prev, ttls, covered = {}, {}, []
    between = {"returns": 0, "applied": 0, "rewrite_usd": 0.0, "summary_usd": 0.0}
    above = {"returns": 0, "applied": 0, "rewrite_usd": 0.0, "summary_usd": 0.0, "left_sessions": 0, "left_usd": 0.0}
    small = 0

    def summary_usd(model, pre):
        return pricing.weight(model, {"input": 0, "output": out_tokens, "cache_read": pre, "cache_write": out_tokens,
                                      "cache_write_1h": out_tokens, "cache_write_5m": 0})
    for r in select(con, "requests", [ts_range(lo, None), ("kind = 'main'", [])], order="project_dir, file, ts, rowid"):
        t = cost.parse_ts(r["ts"])
        if t is None:
            continue
        key = (r["project_dir"], r["file"])
        tr = ttls.setdefault(key, pricing.TtlTracker(RESUME_IDLE_S))
        ttl, p = tr.current(), prev.get(key)
        tr.update(r)
        prev[key] = (t, pricing.context_tokens(r), r["model"])
        if not p or t < start or ttl != 3600 or (t - p[0]).total_seconds() < FIRE_S:
            continue
        pre, applied = p[1], any(p[0] < c <= t for c in comps.get(r["session_id"], ()))
        if pre < FLOOR_MIN_TOKENS:
            small += 1
        elif pre >= DEFAULT_MIN_TOKENS:
            covered.append((t, applied))
            if pre < IDLE_RAISE_TO:
                above["returns"] += 1
                above["applied"] += applied
                above["summary_usd"] += summary_usd(r["model"], pre)
                above["rewrite_usd"] += (pre * pricing.price(r["model"], r)[3] / 1e6 if applied
                                         else pricing.token_cost_split(r["model"], r)["cache_write"])
        elif applied:  # the threshold was already lower that day: the rewrite it spared, as for `above`
            between["returns"] += 1
            between["applied"] += 1
            between["rewrite_usd"] += pre * pricing.price(r["model"], r)[3] / 1e6
            between["summary_usd"] += summary_usd(r["model"], pre)
        else:
            between["returns"] += 1
            between["rewrite_usd"] += pricing.token_cost_split(r["model"], r)["cache_write"]
            between["summary_usd"] += summary_usd(r["model"], pre)
    left = {"sessions": 0, "summary_usd": 0.0}
    for key, (t, pre, model) in prev.items():
        if not (t >= start and (now - t).total_seconds() >= FIRE_S and ttls[key].current() == 3600):
            continue
        if FLOOR_MIN_TOKENS <= pre < DEFAULT_MIN_TOKENS:
            left["sessions"] += 1
            left["summary_usd"] += summary_usd(model, pre)
        elif DEFAULT_MIN_TOKENS <= pre < IDLE_RAISE_TO:
            above["left_sessions"] += 1
            above["left_usd"] += summary_usd(model, pre)
    since = min((t for t, a in covered if a), default=None)
    seen = [a for t, a in covered if since and t >= since]
    rate = sum(seen) / len(seen) if seen else None
    net = between["rewrite_usd"] - between["summary_usd"] - left["summary_usd"]  # < 0: lowering it costs more
    expected = net * rate if rate is not None else None
    if not between["returns"]:
        verdict = "keep"
    elif rate is None or len(seen) < IDLE_MIN_CASES or between["returns"] < IDLE_MIN_CASES:
        verdict = "unknown"
    else:
        verdict = "lower" if expected > 0 else "keep"
    up = above["summary_usd"] + above["left_usd"] - above["rewrite_usd"]  # < 0: raising it costs more
    up_expected = up * rate if rate is not None else None
    if not above["returns"] and not above["left_sessions"]:
        verdict_raise = "keep"
    elif rate is None or len(seen) < IDLE_MIN_CASES or above["returns"] < IDLE_MIN_CASES:
        verdict_raise = "unknown"
    else:
        verdict_raise = "raise" if up_expected > 0 else "keep"
    return {"days": days, "small": small, "summary_tokens": out_tokens, "raise_to": IDLE_RAISE_TO,
            "above": dict(above, net_usd=up, expected_usd=up_expected), "verdict_raise": verdict_raise,
            "covered": {"returns": len(covered), "since": since.isoformat() if since else None, "counted": len(seen),
                        "applied": sum(seen), "rate": rate},
            "between": dict(between, net_usd=net, expected_usd=expected), "left": left,
            "verdict": verdict}
