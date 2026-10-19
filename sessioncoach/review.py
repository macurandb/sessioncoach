"""Daily review: what was expensive on the last active day, one tip for today, and the day before.

`sessioncoach review` runs from the platform scheduler (LaunchAgent, systemd timer, cron or Task Scheduler;
see scheduler.py) at the time set in alerts.json → `review`; a run missed while the machine was off
or asleep happens on wake where the scheduler supports it, else the next ingest sends it. It reads only the numbers sessioncoach already
stores, so it spends no tokens. `build()` is read-only (the dashboard opens the database read-only); only
`deliver()` writes, to remember that a day was already reviewed.

alerts.json → "review": {
  "enabled": true, "time": "08:00", "days": ["mon", "tue", "wed", "thu", "fri"],
  "channels": ["notification", "dashboard"],   # "email" too, with email_command
  "lookback": "last_active",                    # or N: the day N days before the run
  "max_tips": 1, "long_context_tokens": 300000, "max_sessions": 8,
  "email_command": ["mail", "-s", "{title}", "me@example.com"]   # body on stdin, numbers only
}
"""
import collections
import datetime as dt
import os
import subprocess
import time

from . import analysis, config, cost, paths, pricing, rules, scheduler

DEFAULTS = config.REVIEW_DEFAULTS  # lives in config; kept here for older callers
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LABEL = scheduler.launchd_label("review")
# what the scheduled review gives the user, said by `sessioncoach setup` before asking
VALUE = ("Every morning, the most expensive habit of your last active day and one change to try today, worked out on "
         "this machine with no tokens spent.")

MSG = {
    "es": {"title": "Repaso del {day}: {usd}", "vs": " ({sign}{delta} vs {prev})",
           "R1": "Hoy: no retomes una sesión grande tras caducar la caché (pausa de más de {ttl}); abre una nueva con un traspaso. "
                 "Ese día costó ≈ {usd} ({n} veces).",
           "R16": "Hoy: una sesión por tarea y /compact al cerrar cada fase. Los turnos con más de {k}k tokens costaron ≈ {usd}.",
           "R3": "Hoy: lanza los subagentes de búsqueda en Sonnet (CLAUDE_CODE_SUBAGENT_MODEL=sonnet). En Opus costaron ≈ {usd}.",
           "MIX": "Hoy: una sesión por proyecto. {n} sesiones mezclaron proyectos o empresas (≈ {usd}).",
           "none": "Nada caro que destacar. Sigue así.",
           "email_body": "Repaso del {day}\nTotal: {usd} (día anterior con actividad {prev}: {prev_usd})\nSesiones: {sessions}\n"
                         "Reanudaciones con caché caducada: {r1_n} (≈ {r1_usd})\nTurnos de contexto largo: ≈ {long_usd}\n"
                         "Subagentes en Opus: ≈ {opus_usd}\nSesiones que mezclan proyectos o empresas: {mix_n}\n"},
    "en": {"title": "Review of {day}: {usd}", "vs": " ({sign}{delta} vs {prev})",
           "R1": "Today: don't resume a big session once its cache expired (idle over {ttl}); start a new one with a handoff. "
                 "That day it cost ≈ {usd} ({n} times).",
           "R16": "Today: one session per task and /compact when a phase ends. Turns over {k}k tokens of context cost ≈ {usd}.",
           "R3": "Today: run search subagents on Sonnet (CLAUDE_CODE_SUBAGENT_MODEL=sonnet). On Opus they cost ≈ {usd}.",
           "MIX": "Today: one session per project. {n} sessions mixed projects or companies (≈ {usd}).",
           "none": "Nothing expensive to point out. Keep it up.",
           "email_body": "Review of {day}\nTotal: {usd} (previous active day {prev}: {prev_usd})\nSessions: {sessions}\n"
                         "Resumes after the cache expired: {r1_n} (≈ {r1_usd})\nLong-context turns: ≈ {long_usd}\n"
                         "Subagents on Opus: ≈ {opus_usd}\nSessions mixing projects or companies: {mix_n}\n"},
}


def load_config(alerts=None):
    """alerts.json → review, merged over the defaults key by key (a partial block keeps the rest)."""
    return config.review_config(alerts)


def target_day(con, run_date, lookback="last_active"):
    """The day to review: the last day with cost strictly before `run_date`, or `run_date - N`."""
    if isinstance(lookback, int) and not isinstance(lookback, bool):
        return run_date - dt.timedelta(days=lookback)
    row = con.execute("SELECT MAX(day) FROM cost_slices WHERE day < ? AND usd > 0", (run_date.isoformat(),)).fetchone()
    return dt.date.fromisoformat(row[0]) if row and row[0] else None


def _utc_midnight(day):
    """Local midnight starting `day`, as a UTC ts string."""
    return (dt.datetime.combine(day, dt.time()).astimezone()
            .astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"))


def _day_sessions(con, d0):
    """{(project dir, session): figures} with the cost slices of local day `d0`."""
    sessions = collections.defaultdict(lambda: {"usd": 0.0, "estimated_usd": 0.0, "resumes": 0, "resume_usd": 0.0,
                                                "long_turns": 0, "long_usd": 0.0, "long_excess_usd": 0.0, "max_ctx": 0, "opus_agents": 0,
                                                "opus_usd": 0.0, "mixed": False})
    for s in con.execute("SELECT * FROM cost_slices WHERE day = ?", (d0,)):
        x = sessions[(s["project_dir"], s["session_id"])]
        x["usd"] += s["usd"]
        x["estimated_usd"] += s["usd"] if s["estimated"] else 0.0
        x["_row"] = s
    return sessions


def _add_turn(x, r, resumed, ttl, ttls, long_ctx):
    """Add main-session request `r` of the day to its session's figures (R1 resumes, R16 long turns)."""
    ctx = pricing.context_tokens(r)
    x["max_ctx"] = max(x["max_ctx"], ctx)
    if resumed:
        ttls[ttl] += 1
        x["resumes"] += 1
        x["resume_usd"] += rules.resume_usd(r)
    if rules.is_long(r, long_ctx):
        x["long_turns"] += 1
        x["long_usd"] += pricing.weight(r["model"], r)
        x["long_excess_usd"] += rules.long_excess_usd(r, long_ctx)


def _add_requests(con, day, until, sessions, long_ctx, r3_files):
    """Add the requests of `day` to `sessions`; R3 counts the subagent requests in `r3_files` only.
    -> Counter of the cache TTLs of its resumes."""
    reqs = collections.defaultdict(list)
    for r in analysis.select(con, "requests", [until], order="ts, rowid"):
        t = cost.parse_ts(r["ts"])
        if t:
            reqs[(r["project_dir"], r["file"], r["kind"])].append((t, r))
    det, ttls = analysis.ResumeDetector(), collections.Counter()
    for (pdir, _f, kind), rows in reqs.items():
        rows.sort(key=lambda x: x[0])
        for t, r in rows:
            resumed, ttl = det.feed((pdir, _f, kind), t, r)  # cache lifetime of that transcript (1 h / 5 min)
            if t.date() != day:
                continue
            x = sessions[(pdir, r["session_id"])]
            x.setdefault("_row", r)
            if kind != "subagent":
                _add_turn(x, r, resumed, ttl, ttls, long_ctx)
            elif rules.r3_request(r, r3_files):
                x["opus_usd"] += pricing.weight(r["model"], r)
    return ttls


def _session_dirs(con, day, sessions):
    """{(project dir, session): working dirs used up to the end of `day`}."""
    dirs = collections.defaultdict(set)
    end = _utc_midnight(day + dt.timedelta(days=1))
    # a dir counts from the first time the session used it: a multi-day session is not "mixing" before that
    for r in con.execute("SELECT project_dir, session_id, dir FROM session_dirs WHERE ts IS NULL OR ts < ?", (end,)):
        if (r["project_dir"], r["session_id"]) in sessions:
            dirs[(r["project_dir"], r["session_id"])].add(r["dir"])
    return dirs


def _session_rows(con, sessions, dirs, dims):
    """The sessions with cost or context, labelled and checked for mixing, most expensive first."""
    known = _known_projects(con, dims.labeler)
    out = []
    for key, x in sessions.items():
        row = x.pop("_row", None)
        if row is None or (not x["usd"] and not x["max_ctx"]):
            continue
        p, ten, _a = dims(row)
        others = {dims.labeler(d) for d in dirs.get(key, ()) if known(d, key[0], ten)} - {(p, ten)}
        x["mixed"] = any(op != p for op, _ in others)
        x["mixed_tenants"] = any(ot != ten for _, ot in others)
        x["project"], x["tenant"], x["session"] = p, ten, "%s · %s" % (p, (key[1] or "")[:8])
        out.append(x)
    out.sort(key=lambda x: -x["usd"])
    return out


def _causes(out, ttls):
    return {"resume_ttl": " / ".join(analysis.fmt_ttl(s) for s in sorted(ttls, reverse=True)),
            "resumes": sum(x["resumes"] for x in out), "resume_usd": sum(x["resume_usd"] for x in out),
            "long_turns": sum(x["long_turns"] for x in out), "long_usd": sum(x["long_usd"] for x in out),
            "long_excess_usd": sum(x["long_excess_usd"] for x in out),
            "opus_agents": sum(x["opus_agents"] for x in out), "opus_usd": sum(x["opus_usd"] for x in out),
            "mixed_sessions": sum(1 for x in out if x["mixed"]), "mixed_usd": sum(x["usd"] for x in out if x["mixed"])}


def _day_figures(con, day, cfg, dims):
    """Per-session cost and causes for one local day (criteria in rules.py; long context from the review config)."""
    sessions = _day_sessions(con, day.isoformat())
    # requests from the day before too: a resume's previous turn is often the evening before
    since = _utc_midnight(day - dt.timedelta(days=1))
    # later days never change what a day shows (rows are fed in time order): bound ts above too, Python decides below
    until = analysis.ts_range(since, (day + dt.timedelta(days=analysis.SLACK_DAYS + 1)).isoformat())
    subs = list(analysis.select(con, "subagents", [until]))
    ttls = _add_requests(con, day, until, sessions, int(cfg["long_context_tokens"]), rules.r3_agent_files(subs))
    for s in subs:
        t = cost.parse_ts(s["ts"])
        if t and t.date() == day and rules.opus_agent(s, rules.R3_SUBAGENT_TYPE):
            sessions[(s["project_dir"], s["session_id"])]["opus_agents"] += 1
    out = _session_rows(con, sessions, _session_dirs(con, day, sessions), dims)
    causes = _causes(out, ttls)
    totals = {"usd": sum(x["usd"] for x in out), "estimated_usd": sum(x["estimated_usd"] for x in out), "sessions": len(out)}
    return totals, {k: round(v, 2) if isinstance(v, float) else v for k, v in causes.items()}, out


def _known_projects(con, labeler):
    """Predicate: is working dir `d` another project, seen from a session of `pdir`?

    Only real projects count: a dir with an explicit project in projects.json, or a dir where
    sessions are also started. The home dir and hidden dirs under it (cd ~, ~/.claude ...) and
    subdirs or worktrees in the same company are just looking around, not mixing. A subdir in
    another company always counts (a session opened in ~ that works on two clients mixes them).
    """
    session_dirs = {r[0] for r in con.execute("SELECT DISTINCT project_dir FROM requests")}
    home = labeler._prefix[:-1]

    def known(d, pdir, tenant):
        if d in (pdir, home) or d.startswith(home + "--"):
            return False
        if not (d in session_dirs or labeler(d)[0] != labeler._default(d)):
            return False
        return not d.startswith(pdir + "-") or labeler(d)[1] != tenant
    return known


def tips(causes, cfg):
    """The changes with the largest savings ceiling, biggest first (list-price estimates).

    Each ceiling is what the change could save at most: R1 the cache rewrites, R16 re-reading the
    context above the threshold, R3 the Opus-to-Sonnet price difference (rules.REVIEW_TIPS). Mixing
    projects has no honest dollar figure, so it comes after them, only when there is room left.
    """
    out = rules.tips(rules.Day(causes, cfg["long_context_tokens"], analysis.fmt_ttl(analysis.RESUME_IDLE_S)))
    if causes["mixed_sessions"]:
        out.append({"id": "MIX", "saving_usd": None, "params": {"usd": causes["mixed_usd"], "n": causes["mixed_sessions"]}})
    return out[:max(0, int(cfg["max_tips"]))]


def build(con, run_date=None, cfg=None, labeler=None, resolver=None):
    """The review for `run_date` (default today). Read-only."""
    cfg = cfg or load_config()
    run_date = run_date or dt.date.today()
    day = target_day(con, run_date, cfg["lookback"])
    cfg_keys = ("time", "days", "channels", "lookback", "max_tips", "long_context_tokens")
    out = {"run_date": run_date.isoformat(), "day": None, "config": {k: cfg[k] for k in cfg_keys}}
    if day is None:
        return out
    dims = analysis.Dims(con, labeler, resolver)
    totals, causes, sessions = _day_figures(con, day, cfg, dims)
    prev = target_day(con, day, "last_active")
    p_totals, p_causes = (None, None)
    if prev:
        p_totals, p_causes, _ = _day_figures(con, prev, cfg, dims)
    keep = ("session", "project", "tenant", "usd", "estimated_usd", "resumes", "resume_usd", "long_turns", "long_usd",
            "max_ctx", "opus_agents", "opus_usd", "mixed", "mixed_tenants")
    out.update({"day": day.isoformat(), "prev_day": prev.isoformat() if prev else None,
                "totals": {k: round(v, 2) if isinstance(v, float) else v for k, v in totals.items()},
                "prev_totals": {k: round(v, 2) if isinstance(v, float) else v for k, v in p_totals.items()} if p_totals else None,
                "causes": causes, "prev_causes": p_causes,
                "sessions": [{k: round(x[k], 2) if isinstance(x[k], float) else x[k] for k in keep}
                             for x in sessions[:int(cfg["max_sessions"])]],
                "tips": tips(causes, cfg)})
    return out


_lang = config.notice_lang  # its name before the language rules moved to config


def _usd(v):
    return "$%.0f" % v if v >= 100 else "$%.2f" % v


def texts(data, cfg):
    """(title, message, email body) in the configured language."""
    m = MSG.get(_lang(cfg), MSG["en"])
    tot = data["totals"]
    title = m["title"].format(day=data["day"], usd=_usd(tot["usd"]))
    if data.get("prev_totals"):
        delta = tot["usd"] - data["prev_totals"]["usd"]
        sign = "+" if delta >= 0 else "−"  # noqa: RUF001 (typographic minus, deliberate in the visible title)
        title += m["vs"].format(sign=sign, delta=_usd(abs(delta)), prev=data["prev_day"])
    tip = data["tips"][0] if data["tips"] else None
    message = m[tip["id"]].format(**dict(tip["params"], usd=_usd(tip["params"]["usd"]))) if tip else m["none"]
    c = data["causes"]
    body = m["email_body"].format(day=data["day"], usd=_usd(tot["usd"]), prev=data.get("prev_day") or "-",
                                  prev_usd=_usd(data["prev_totals"]["usd"]) if data.get("prev_totals") else "-",
                                  sessions=tot["sessions"], r1_n=c["resumes"], r1_usd=_usd(c["resume_usd"]),
                                  long_usd=_usd(c["long_usd"]), opus_usd=_usd(c["opus_usd"]), mix_n=c["mixed_sessions"])
    if tip:
        body += "\n" + message + "\n"
    return title, message, body


def deliver(con, data, cfg, notify_fn=None, force=False):
    """Send the review once per reviewed day (`force` sends again). Returns the channels used."""
    from . import limits
    if not data.get("day"):
        return []
    # atomic: on wake the scheduled job and the ingest catch-up can run at the same moment
    cur = con.execute("INSERT OR IGNORE INTO alerts_sent (key, ts) VALUES (?, ?)", ("review:%s" % data["day"], int(time.time())))
    con.commit()
    if not cur.rowcount and not force:
        return []
    title, message, body = texts(data, cfg)
    sent = []
    channels = cfg.get("channels") or []
    if "notification" in channels:
        (notify_fn or limits.notify)(title, message)
        sent.append("notification")
    if "email" in channels and cfg.get("email_command"):
        cmd = [c.replace("{title}", title) for c in cfg["email_command"]]
        try:
            done = subprocess.run(cmd, input=body, text=True, encoding="utf-8", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=20, check=False)
            if done.returncode == 0:
                sent.append("email")
        except (OSError, subprocess.SubprocessError):
            pass
    return sent


def due(cfg, now=None):
    """Whether a scheduled run should send now: enabled, a review day, and past the review time."""
    now = now or dt.datetime.now()
    if not cfg.get("enabled", True) or WEEKDAYS[now.weekday()] not in [d[:3].lower() for d in cfg.get("days") or []]:
        return False
    h, m = parse_time(cfg["time"])
    return (now.hour, now.minute) >= (h, m)


def parse_time(s):
    h, m = str(s).split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("review.time must be HH:MM")
    return h, m


def calendar_intervals(cfg):
    """launchd StartCalendarInterval entries (Weekday: 0 = Sunday, 1 = Monday ... 6 = Saturday)."""
    h, m = parse_time(cfg["time"])
    out = []
    for d in cfg.get("days") or WEEKDAYS:
        i = WEEKDAYS.index(d[:3].lower())
        out.append({"Weekday": (i + 1) % 7, "Hour": h, "Minute": m})
    return out


def plist_path(agents_dir=None):
    return scheduler.plist_path(LABEL, agents_dir)


def plist(cfg, kit_bin, log):
    return scheduler.launchd_plist(job(cfg), kit_bin, log)


def job(cfg):
    """The scheduler job for the review."""
    return scheduler.Job("review", ["review", "--scheduled"], hhmm="%02d:%02d" % parse_time(cfg["time"]),
                         days=cfg.get("days") or WEEKDAYS)

def install(cfg=None, kit_bin=None, agents_dir=None, load=True):
    """Install the scheduled review from the current config (re-run after changing time or days)."""
    cfg = cfg or load_config()
    log = os.path.join(paths.ensure_data_dir("logs"), "review.log")
    be = scheduler.backend()
    days = ",".join(cfg.get("days") or WEEKDAYS)
    msg = scheduler.install(job(cfg), kit_bin, log, unit_dir=agents_dir, load=load, be=be)
    if be == "launchd":
        return "%s: %s at %s (log %s)" % (msg, days, cfg["time"], log)
    return "%s [%s at %s]" % (msg, days, cfg["time"])

def uninstall(agents_dir=None, load=True):
    return scheduler.uninstall(job(DEFAULTS), unit_dir=agents_dir, load=load)

def installed(agents_dir=None):
    return scheduler.installed(job(DEFAULTS), unit_dir=agents_dir)

def catch_up(con, cfg=None, now=None, notify_fn=None):
    """Backup for the scheduled job (e.g. the Mac was off at review time): called by every ingest.

    Sends only when the review job is installed, today is a review day, its time has passed,
    and the day it reviews was not reviewed yet.
    """
    cfg = cfg or load_config()
    now = now or dt.datetime.now()
    if not installed() or not due(cfg, now):
        return []
    data = build(con, now.date(), cfg)
    return deliver(con, data, cfg, notify_fn)
