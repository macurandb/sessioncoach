"""sessioncoach — command line. `sessioncoach help` lists every command, what it gives you first and its syntax after;
`sessioncoach help <command>` (or `<command> --help`) adds its options and the technical detail."""
import argparse
import json
import os
import sys

from . import __version__, analysis, collect, cost, paths, store


def _fmt(n):
    n = float(n or 0)
    for unit, div in (("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return "%.1f%s" % (n / div, unit)
    return "%d" % n


def cmd_ingest(args):
    con = store.connect(paths.db_path())
    stats = {}
    added = collect.ingest(con, paths.transcript_roots(), full=args.full, stats=stats)
    if args.import_jsonl:
        imp = collect.import_jsonl(con, args.import_jsonl)
        added = {k: added[k] + imp[k] for k in added}
    cost.rebuild_slices(con)
    from . import limits
    limits.evaluate_budgets(con)
    try:  # a paused session whose cache is about to expire; never fails the ingest
        from . import expiry
        expiry.check(con)
    except Exception:
        import traceback
        traceback.print_exc()
    try:  # backup for the scheduled review (Mac off or asleep at review time)
        from . import review
        review.catch_up(con)
    except Exception:  # the ingest itself succeeded; the scheduled job redirects stderr to its log
        import traceback
        traceback.print_exc()
    counts = {t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("requests", "cost_state", "subagents", "tool_calls")}
    store.checkpoint(con)
    print(json.dumps({"db": paths.db_path(), "added": added, "total": counts, "files": stats}))


def _no_db():
    """True, after saying so, when there is no database yet: reading commands do not create an empty one."""
    if os.path.exists(paths.db_path()):
        return False
    print("No database yet at %s. Run: sessioncoach ingest" % paths.db_path())
    return True


def cmd_report(args):
    if _no_db():
        return 1
    con = store.connect(paths.db_path())
    d = analysis.build(con, days=args.days or None, tenant=args.tenant, project=args.project, account=args.account)
    s, w = d["summary"], d["window"]
    print("sessioncoach report — %s → %s%s" % (w["start"] or "all", w["end"],
                                            "  (API-equivalent at list price; on subscriptions not an invoice)"))
    prev = " (prev %.2f $)" % s["prev_total_usd"] if s["prev_total_usd"] is not None else ""
    print("  total %.2f $  = recorded %.2f + estimated %.2f%s   sessions %d   requests %d"
          % (s["total_usd"], s["recorded_usd"], s["estimated_usd"], prev, s["sessions"], s["requests"]))
    print("\n  where it goes (list price):  " + "  ".join("%s %d%%" % (t["type"], round(t["share"] * 100)) for t in d["token_split"]))
    cs = d["cache_share"]
    print("  cache (read + write): %d%% of the list-price spend over all activity (%d%% read, %d%% write)"
          % (round(cs["share"] * 100), round(cs["read_share"] * 100), round(cs["write_share"] * 100)))
    for u in d["pricing"]["unpriced"]:
        print("  ! no exact price for %s: %d requests, %.2f $ priced like %s (table of %s)"
              % (u["model"], u["requests"], u["usd"] if u["usd"] is not None else u["list_usd"], u["fallback"], d["pricing"]["table_date"]))
    print("\n  %-34s %-12s %10s %8s %9s" % ("project", "tenant", "USD", "opus", "sessions"))
    for p in d["projects"][:15]:
        share = "-" if p["opus_share"] is None else "%d%%" % round(p["opus_share"] * 100)
        print("  %-34s %-12s %10.2f %8s %9d" % (p["project"][:34], p["tenant"][:12], p["usd"], share, p["sessions"]))
    print("\n  %-30s %-12s %10s %16s" % ("account", "company", "USD", "other company $"))
    for a in d["accounts"]:
        print("  %-30s %-12s %10.2f %16.2f" % (a["label"][:30], (a["company"] or "-")[:12], a["usd"], a["other_company_usd"]))
    print("\n  models: " + "  ".join("%s %d%%" % (m["model"], round(m["share"] * 100)) for m in d["models"][:6]))
    if d["recommendations"]:
        print("\n  recommendations (savings are estimates):")
        for r in d["recommendations"]:
            saving = "≈ %.2f $" % r["saving_usd"] if r["saving_usd"] else "-"
            print("    %s  saving %-12s confidence %-6s %s" % (r["id"], saving, r["confidence"], json.dumps(r["params"])))
    print("\n  reproduce: sessioncoach report --days %s%s%s" % (args.days or 0, " --tenant " + args.tenant if args.tenant else "",
                                                    " --project " + args.project if args.project else ""))


def cmd_day(args):
    import datetime as dt
    if _no_db():
        return 1
    con = store.connect(paths.db_path())
    d = analysis.day(con, date=dt.date.fromisoformat(args.date) if args.date else None,
                     tenant=args.tenant, project=args.project, account=args.account)
    t, p = d["totals"]["today"], d["totals"]["prev"]
    print("sessioncoach day — %s vs %s   (last ingest %s)" % (d["date"], d["prev_date"], d["last_ingest"] or "never"))
    line = "  %s: %.2f $ (≈ %.2f estimated)   %s: %.2f $" % (d["date"], t["usd"], t["estimated_usd"], d["prev_date"], p["usd"])
    if d["is_today"]:
        line += "   %s at %02d:59: %.2f $" % (d["prev_date"], d["hour_now"], d["totals"]["prev_same_time_usd"] or 0)
    print(line)
    print("\n  %-30s %-12s %10s %10s %10s" % ("account", "company", d["date"][5:], d["prev_date"][5:], "Δ"))
    for a in d["accounts"]:
        print("  %-30s %-12s %10.2f %10.2f %+10.2f" % (
            a["label"][:30], (a["company"] or "-")[:12], a["today"], a["prev"], a["today"] - a["prev"]))
    print("\n  %-30s %-12s %10s %10s %10s" % ("project", "tenant", d["date"][5:], d["prev_date"][5:], "Δ"))
    for r in d["projects"][:15]:
        print("  %-30s %-12s %10.2f %10.2f %+10.2f" % (r["project"][:30], r["tenant"][:12], r["today"], r["prev"], r["today"] - r["prev"]))


def cmd_tools(args):
    if _no_db():
        return 1
    con = store.connect(paths.db_path())
    v = analysis.tools_view(con, days=args.days or None, project=args.project)
    w = v["window"]
    print("sessioncoach tools — %s → %s%s   (names only, no inputs)" % (w["start"] or "all", w["end"],
                                                                  "   project ~ " + args.project if args.project else ""))
    print("  " + "  ".join("%s %d" % kv for kv in v["totals"][:12]))
    cols = sorted({k for _, row in v["per_day"] for k in row if k not in ("exploration", "Bash")})
    print("\n  %-10s %12s %6s %s" % ("day", "exploration", "Bash", " ".join("%14s" % c[:14] for c in cols)))
    for day, row in v["per_day"]:
        print("  %-10s %12d %6d %s" % (day, row.get("exploration", 0), row.get("Bash", 0), " ".join("%14d" % row.get(c, 0) for c in cols)))


def cmd_alerts(args):
    import time

    from . import config, limits
    con = store.connect(paths.db_path())
    cfg = config.load_alerts()
    v = analysis.limits_view(con)
    last = v["last"]
    if last:
        print("plan limits (last sample %s):" % time.strftime("%Y-%m-%d %H:%M", time.localtime(last["ts"])))
        for k, label in (("five", "5-hour"), ("seven", "7-day")):
            if last[k + "_pct"] is not None:
                reset = time.strftime("%a %H:%M", time.localtime(last[k + "_reset"]))
                print("  %-7s %5.1f%%  resets %s" % (label, last[k + "_pct"], reset))
    else:
        print("plan limits: no sample yet (install the status line: sessioncoach statusline-install)")
    if v["window"]:
        print("  what consumed the current 5-hour window (list price):")
        for r in v["window"]["by_project"]:
            print("    %-30s %5.0f%%" % (r["name"][:30], r["share"] * 100))
    for b in limits.evaluate_budgets(con, cfg, dry=True):
        proj = "   → %.0f%% at period end" % b["projected_pct"] if b.get("projected_pct") is not None else ""
        print("  budget %-14s %8.2f / %8.2f $ (%s) %5.1f%%%s" % (
            b["name"], b["used_usd"], b["limit_usd"], b["period"], b["pct"], proj))
    print("thresholds: 5h %s · 7d %s · context %s · projection %s" % (
        cfg["five_hour"], cfg["seven_day"], cfg["context"], cfg["projection"]))
    print("config: %s" % config.alerts_path())
    if args.test:
        limits.notify("sessioncoach", limits.msg("limit_m", r=time.strftime("%H:%M")))
        print("test notification sent")


def cmd_alert_log(args):
    from . import alertlog
    if _no_db():
        return 1
    con = store.open_readonly(paths.db_path())
    try:
        v = alertlog.view(con, days=args.days)
    finally:
        con.close()
    print("sessioncoach alert-log — last %d days: %s" % (v["days"], ", ".join(
        "%s %s" % (n, k) for k, n in sorted(v["summary"].items()) if k != "costly_usd") or "nothing"))
    for e in v["events"]:
        o = e["outcome"]
        extra = " ".join("%s=%s" % (k, o[k]) for k in sorted(o) if k != "code" and o[k] is not None)
        print("  %s  %-14s %-24s %-12s %s %s" % (e["ts"][:16].replace("T", " "), e["source"], (e["project"] or "-")[:24],
                                               o["code"], extra, json.dumps(e["data"], sort_keys=True)))
    if v["missed"]:
        print("not warned (since %s):" % v["missed_since"][:16].replace("T", " "))
        for m in v["missed"]:
            print("  %s  %-24s %-10s %dk tokens, cache expired %s" % (m["ts"][:16].replace("T", " "), m["project"][:24],
                                                                   m["reason"], m["context"] // 1000, m["expired_at"][11:16]))


def cmd_review(args):
    import datetime as dt

    from . import limits, review
    cfg = review.load_config()
    if args.scheduled and not review.due(cfg):
        print("review: not due (disabled, not a review day, or before %s)" % cfg["time"])
        return
    con = store.connect(paths.db_path())
    if args.scheduled:  # fresh numbers first; the ingest job may not have run since waking up
        collect.ingest(con, paths.transcript_roots())
        cost.rebuild_slices(con)
        limits.evaluate_budgets(con)
    data = review.build(con, dt.date.fromisoformat(args.date) if args.date else None, cfg)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif not data["day"]:
        print("review: no activity before %s" % data["run_date"])
    else:
        title, message, _ = review.texts(data, cfg)
        c = data["causes"]
        print(title)
        print("  resumes after idle %d (≈ %.2f $)   long-context turns %d (≈ %.2f $)   opus subagents %d (≈ %.2f $)   mixed sessions %d"
              % (c["resumes"], c["resume_usd"], c["long_turns"], c["long_usd"], c["opus_agents"], c["opus_usd"], c["mixed_sessions"]))
        print("\n  %-40s %9s %8s %8s %6s %5s" % ("session", "USD", "resumes", "max ctx", "opus", "mixed"))
        for x in data["sessions"]:
            print("  %-40s %9.2f %8d %7dk %6d %5s" % (x["session"][:40], x["usd"], x["resumes"], x["max_ctx"] / 1000,
                                                    x["opus_agents"], "yes" if x["mixed"] else ""))
        print("\n  " + message)
    if args.notify or args.scheduled:
        sent = review.deliver(con, data, cfg, force=args.notify)
        print("sent: %s" % (", ".join(sent) or "nothing (already reviewed)"))


def cmd_review_install(args):
    from . import review
    try:
        print(review.install())
    except ValueError as e:  # a mistyped SESSIONCOACH_SCHEDULER
        print("sessioncoach review-install: %s" % e)
        return 2


def cmd_review_uninstall(args):
    from . import review
    print(review.uninstall())


def cmd_statusline(args):
    from . import limits
    limits.statusline_main(args.chain_file)


def cmd_statusline_install(args):
    from . import limits
    print(limits.install())


def cmd_statusline_uninstall(args):
    from . import limits
    print(limits.uninstall())


def cmd_hook(args):
    from . import hooks
    hooks.by_arg(args.event).module().hook_main()


def cmd_hook_install(args):
    from . import nudge
    print(nudge.install())


def cmd_hook_uninstall(args):
    from . import nudge
    print(nudge.uninstall())


def cmd_handoff_install(args):
    from . import handoff, installer
    msg = handoff.install(resume_hook=installer.resume_visible(installer.settings_for("global")))
    print(msg)
    if not msg.startswith("skipped"):
        print(handoff.cost_note())


def cmd_handoff_uninstall(args):
    from . import handoff
    print(handoff.uninstall())


def cmd_resume_hook_install(args):
    from . import installer, resume
    print(resume.install())
    installer.refresh_handoff()


def cmd_resume_hook_uninstall(args):
    from . import installer, resume
    print(resume.uninstall())
    installer.refresh_handoff()


def cmd_idle_guard_on(args):
    from . import installer
    for line in installer.set_idle_guard(True):
        print(line)
    for p in installer.idle_guard_problems():
        print("note: " + p)


def cmd_idle_guard_off(args):
    from . import installer
    for line in installer.set_idle_guard(False):
        print(line)


def cmd_idle_compact(args):
    from . import idlecompact, installer, keepctx
    value, report = installer.idle_compact_threshold()
    v = analysis.idle_compact_view(store.connect(paths.db_path()), days=args.days)
    marks = len(keepctx.load()) if os.path.exists(keepctx.path()) else 0
    if args.json:
        print(json.dumps(dict(v, setting={"value": value, "report": [{"warning": w, "detail": d} for w, d in report]},
                              keep_context_marks_7d=marks), indent=2))
        return 0
    lo, hi = idlecompact.FLOOR_MIN_TOKENS // 1000, idlecompact.DEFAULT_MIN_TOKENS // 1000
    print("sessioncoach idle-compact — %s: from what context Claude Code summarises a paused session" % idlecompact.MIN_TOKENS_ENV)
    print("\nsetting:")
    for warn, detail in report or [(False, "not set: Claude Code's default, %dk" % hi)]:
        print("  %s %s" % ("⚠" if warn else "✓", detail))
    c, b, left = v["covered"], v["between"], v["left"]
    print("\nlast %d days, returns after a pause of %d min or more on a 1 h cache, by the context before the pause:"
          % (v["days"], idlecompact.FIRE_S // 60))
    print("  ≥ %dk  %4d returns; Claude Code's summary was applied in %s" % (hi, c["returns"], (
        "%d of the %d since it first did (%s, %.0f %%)" % (c["applied"], c["counted"], c["since"][:10], 100 * c["rate"]))
        if c["since"] else "none (the Mac asleep at the 54th minute, an older Claude Code, or turned off)"))
    print("  %d-%dk %4d returns re-wrote their context for ≈ $%.2f; a summary instead ≈ $%.2f (%s tokens each)"
          % (lo, hi, b["returns"], b["rewrite_usd"], b["summary_usd"], _fmt(v["summary_tokens"])))
    print("  %d-%dk %4d sessions never resumed: their summary is paid for nothing, ≈ $%.2f"
          % (lo, hi, left["sessions"], left["summary_usd"]))
    up, top = v["above"], v["raise_to"] // 1000
    print("  %d-%dk %4d returns would re-write their whole context at %dk (≈ $%.2f) instead of a summary (≈ $%.2f); "
          "%d sessions left there pay ≈ $%.2f in summaries" % (hi, top, up["returns"], top, up["rewrite_usd"], up["summary_usd"],
                                                              up["left_sessions"], up["left_usd"]))
    print("  < %dk  %4d returns (no threshold reaches them)" % (lo, v["small"]))
    print("  list prices; a summary is paid and saves only when it fires, so the balance is weighted by that rate")
    print("\nverdict:")
    if v["verdict"] == "lower":
        print("  %d pays off: ≈ $%.2f in %d days. Those %d sessions come back summarised, with less detail (/keep-context "
              "before stepping away keeps one whole). In ~/.claude/settings.json → env: \"%s\": \"%d\""
              % (idlecompact.FLOOR_MIN_TOKENS, b["expected_usd"], v["days"], b["returns"], idlecompact.MIN_TOKENS_ENV,
                 idlecompact.FLOOR_MIN_TOKENS))
    elif v["verdict"] == "keep" and b["returns"]:
        print("  keep %dk (leave it unset): at %dk the summaries would cost ≈ $%.2f more than they save in %d days, and "
              "drop detail in %d sessions" % (hi, lo, -b["expected_usd"], v["days"], b["returns"]))
    elif v["verdict"] == "keep":
        print("  keep %dk (leave it unset): no return between %dk and %dk in %d days" % (hi, lo, hi, v["days"]))
    else:
        print("  not enough data yet (fewer than %d returns on one side, or no summary applied): ask again in a few days"
              % analysis.IDLE_MIN_CASES)
    if v["verdict_raise"] == "raise":
        print("  %dk pays off: ≈ $%.2f in %d days, and those %d sessions come back whole. In ~/.claude/settings.json → env: "
              "\"%s\": \"%d\"" % (top, up["expected_usd"], v["days"], up["returns"], idlecompact.MIN_TOKENS_ENV, v["raise_to"]))
    elif v["verdict_raise"] == "keep" and up["returns"]:
        print("  do not raise it to %dk: ≈ $%.2f more in %d days (the whole context re-written instead of a summary); "
              "/keep-context keeps one session whole instead" % (top, -up["expected_usd"], v["days"]))
    elif v["verdict_raise"] == "unknown":
        print("  raising it to %dk: not enough data yet" % top)
    print("  /keep-context marks in the last 7 days: %d" % marks)
    return 0


def cmd_ingest_install(args):
    from . import installer
    if args.interval:
        installer.update_alerts(lambda c: c.setdefault("ingest", {}).__setitem__("interval_min", args.interval))
    try:
        print(installer.ingest_install(args.interval))
    except ValueError as e:  # a mistyped SESSIONCOACH_SCHEDULER
        print("sessioncoach ingest-install: %s" % e)
        return 2


def cmd_ingest_uninstall(args):
    from . import installer
    print(installer.ingest_uninstall())


def cmd_setup(args):
    from . import installer
    return installer.setup(yes=args.yes, scope=args.scope, interval=args.interval, time=args.time,
                           cut_dest=args.cut_dest, gitignore=True if args.gitignore else None)


def cmd_check(args):
    from . import installer
    items = installer.check()
    claude = None
    if args.with_claude:
        claude = installer.run_claude_doctor()
    if args.json:
        data = {"version": __version__, "items": [{"status": s, "name": n, "detail": d} for s, n, d in items]}
        if claude:
            data["claude_doctor"] = {"ok": claude[0], "output": claude[1]}
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(installer.format_check(items))
        if claude:
            print("\nclaude doctor:\n" + "\n".join("  " + line for line in claude[1].splitlines()))
        else:
            print("\nTip: run `claude doctor` to check the Claude Code installation itself (or `sessioncoach check --with-claude`).")
    return 1 if any(s == installer.BAD for s, _, _ in items) else 0


def cmd_uninstall(args):
    from . import installer
    if not args.all:
        print("sessioncoach uninstall: pass --all (hooks, status line, /handoff and both scheduled jobs; add --purge to delete "
              "the data too)")
        return 2

    def confirm(d):
        if args.yes:
            return True
        try:
            return input("Delete ALL sessioncoach data in %s (metrics, config)? Type 'yes': " % d).strip() == "yes"
        except EOFError:
            return False
    for line in installer.uninstall_all(purge=args.purge, confirm=confirm):
        print(line)
    return 0


def cmd_verify(args):
    con = store.connect(paths.db_path())
    print(json.dumps(cost.verify(con), indent=2))


def cmd_dashboard(args):
    from . import server
    server.serve(port=args.port, open_browser=not args.no_open)


def cmd_demo(args):
    """Generate synthetic data under <data dir>/demo and serve the dashboard on it.

    From here on this process reads and writes only the demo directory: the data dir and the
    transcript roots are pointed at it, so nothing real can be reached by any code path.
    """
    import os

    from . import demo, server
    dest = os.path.join(paths.data_dir(), "demo")
    db = demo.build(dest, days=args.days, seed=args.seed)
    paths.set_env("DATA_DIR", dest)
    paths.set_env("PROJECTS_DIRS", os.pathsep.join(os.path.join(dest, "accounts", a, "projects") for a in demo.ACCOUNTS))
    print("sessioncoach demo: synthetic data in %s (seed %d, %d days). Your own data is not used." % (dest, args.seed, args.days))
    server.serve(port=args.port, open_browser=not args.no_open, db=db, demo=True)


# What each command gives you, in one line: the first thing `help` and `<command> --help` say. The syntax comes after it.
VALUE = {
    "ingest": "Keeps your history after Claude Code deletes its transcripts (30 days by default); setup schedules it.",
    "report": "Where your money goes and what to change first, in the terminal.",
    "day": "Catch an expensive day early: today against yesterday, per account and project.",
    "tools": "Find out whether exploration or one MCP server is eating your turns.",
    "verify": "How far to trust the cost figures before you act on them.",
    "statusline": "See a plan limit coming before it stops you: limits and alerts in the status line.",
    "statusline-install": "See a plan limit coming before it stops you: limits and alerts in the status line.",
    "statusline-uninstall": "Give the status line back to your own command alone.",
    "hook": "The entry point Claude Code runs for sessioncoach's hooks: the resume warning (prompt), the handoff after "
            "/clear (session-start), Claude Code's idle summary (pre-compact) and /keep-context (keep-context).",
    "hook-install": "Decide before a prompt re-reads a big context whose cache expired, not after paying for it.",
    "hook-uninstall": "Stop the resume warning.",
    "alerts": "How close you are to the 5-hour and weekly limits and to your budgets.",
    "alert-log": "Whether the warnings help: what each said, why, and what you did next.",
    "review": "One change for today from yesterday's numbers, with no tokens spent.",
    "review-install": "Get the daily review without remembering to ask for it.",
    "review-uninstall": "Stop the scheduled daily review.",
    "setup": "Everything working in two minutes; each piece is optional and reversible.",
    "check": "Know that what sessioncoach installed still works, and how to fix what does not.",
    "uninstall": "Undo what setup did, here and in every settings scope; your data stays unless you ask.",
    "ingest-install": "Never lose history to transcript retention: ingest runs on its own.",
    "ingest-uninstall": "Stop the scheduled ingest (your data stays).",
    "handoff-install": "Pick up a paused session without paying for its context on every message: /handoff writes the "
                       "state down and you go on in a clean session.",
    "handoff-uninstall": "Remove the /handoff command.",
    "resume-hook-install": "After /clear, the new session starts from your latest handoff by itself: nothing to copy or retype.",
    "resume-hook-uninstall": "Stop loading the latest handoff after /clear.",
    "idle-guard-on": "The first message after a long pause costs nothing until you choose: send it again to go on, or "
                     "/clear to start clean with a free automatic handoff; /keep-context keeps a session whole when you need it.",
    "idle-guard-off": "Back to the warning alone: a prompt after a long pause is sent as you typed it.",
    "idle-compact": "Whether CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS pays off lowered to 100k or raised to 400k for you, "
                    "and whether it is set right.",
    "dashboard": "Cost, causes and recommendations at a glance, in your browser (local only).",
    "demo": "Try the dashboard or take screenshots without showing your own data.",
    "version": "Which version you run, for a bug report.",
    "help": "What each command saves you, before its syntax.",
}

# The technical side, after the options: which files a command writes, where, and how to undo it.
DETAIL = {
    "ingest": "Reads ~/.claude/projects (or CLAUDE_CONFIG_DIR, SESSIONCOACH_PROJECTS_DIRS) into <data dir>/metrics.db. "
              "Only numbers and ids are kept. Each run also checks budgets, the cache-expiry warning and a missed review.",
    "statusline-install": "Edits the statusLine of ~/.claude/settings.json (backup .bak-statusline); your previous command keeps "
                          "running first, from a chain file in the data dir. Undo: sessioncoach statusline-uninstall.",
    "hook-install": "Adds a UserPromptSubmit hook to ~/.claude/settings.json (backup .bak-hook). It is shown to you, never to the "
                    "model, and never blocks a prompt unless you turn on the idle guard (idle-guard-on). "
                    "Thresholds: alerts.json → nudge. Undo: sessioncoach hook-uninstall.",
    "review-install": "Schedules `sessioncoach review --scheduled` with the OS scheduler (launchd, systemd, cron or Task Scheduler); "
                      "time and days from alerts.json → review. Undo: sessioncoach review-uninstall.",
    "ingest-install": "Schedules `sessioncoach ingest` with the OS scheduler; interval from alerts.json → ingest.interval_min. "
                      "Undo: sessioncoach ingest-uninstall.",
    "setup": "Asks about each piece and explains first what it gives you; --yes accepts every default and still prints that "
             "record. Settings files are backed up before every change. Undo: sessioncoach uninstall --all.",
    "check": "Read-only. Exit code 1 when something is broken.",
    "resume-hook-install": "Adds a SessionStart hook (acts on /clear) to ~/.claude/settings.json (backup .bak-resume). After "
                           "/clear it gives the new session the path and text of the newest docs/HANDOFF-*.md of the project "
                           "(working dir up to the repository root) written in the last 24 h, cut to 9,000 characters. That "
                           "text goes to the model; nothing is written or stored. With the idle guard on, a /clear right "
                           "after a held-back prompt gets an automatic handoff from the previous transcript instead, and "
                           "the guard's entry is removed. An installed /handoff is rewritten so it no "
                           "longer asks you to paste the path. Undo: sessioncoach resume-hook-uninstall.",
    "handoff-install": "Writes ~/.claude/commands/handoff.md (setup --scope puts it in the project's .claude/commands or "
                       "$CLAUDE_CONFIG_DIR/commands). In a session, /handoff [topic] makes Claude write "
                       "docs/HANDOFF-<date>-<topic>.md (goal, state, decisions, next steps, files touched, how to verify) and "
                       "end with /clear. A handoff.md of your own is never overwritten. After a pause longer than the cache, "
                       "that turn writes the context once (500k tokens on Opus ≈ $4, list price). "
                       "Undo: sessioncoach handoff-uninstall.",
    "uninstall": "Removes the hooks, the status line, /handoff and the scheduled jobs from every scope and turns the idle "
                 "guard off, and the «when to cut» block from this project's CLAUDE.local.md / CLAUDE.md; the data dir "
                 "stays unless --purge. Run it from each project where you ran setup --scope project. It leaves the "
                 "CLAUDE.local.md line setup may have added to .gitignore.",
    "hook": "Reads the hook's JSON on stdin. `prompt` (UserPromptSubmit) prints a warning for you, never for the model, "
            "or with the idle guard on holds the first prompt after a pause back; `session-start` (SessionStart, after "
            "/clear) gives the model your latest handoff, or the automatic one; `pre-compact` (PreCompact, auto) notes "
            "Claude Code's idle summary and prints nothing, or blocks it for a session marked with /keep-context; "
            "`keep-context` (UserPromptExpansion) marks the session and answers /keep-context without the model. "
            "None ever fails.",
    "idle-guard-on": "Sets alerts.json → nudge.block and adds a PreCompact hook (matcher auto) to ~/.claude/settings.json "
                     "(backup .bak-precompact), so the guard stays out of the way when Claude Code prepared its idle "
                     "summary. The prompt is held back with {\"decision\": \"block\"}: Claude Code shows the reason and "
                     "your text under \"Original prompt:\", with no Yes/No dialog; sending it again is the yes. After "
                     "/clear the resume hook gives the new session a handoff built from the transcript, with no model "
                     "(worse than /handoff: facts, not reasons). Needs hook-install; the free /clear needs "
                     "resume-hook-install. Also installs /keep-context (commands/keep-context.md and a UserPromptExpansion "
                     "hook, backup .bak-keepctx): typed before stepping away, Claude Code does not summarise that session "
                     "while idle, so you come back to the whole context and pay its re-read once. Undo: sessioncoach "
                     "idle-guard-off.",
    "dashboard": "Serves http://127.0.0.1:<port> to this machine only.",
    "demo": "Writes synthetic data under <data dir>/demo and serves the dashboard on it; your own data is never read.",
}


class _Parser(argparse.ArgumentParser):
    """A command's parser: its --help starts with the command's value line, before the usage."""
    value = None

    def format_help(self):
        return (self.value + "\n\n" if self.value else "") + super().format_help()


def _add(sub, name, **kw):
    sp = sub.add_parser(name, help=VALUE[name], epilog=DETAIL.get(name), **kw)
    sp.value = VALUE[name]
    return sp


def build_parser():
    p = argparse.ArgumentParser(prog="sessioncoach", description="Honest, local-only measurement for Claude Code.")
    sub = p.add_subparsers(dest="cmd", parser_class=_Parser)
    i = _add(sub, "ingest")
    i.add_argument("--import-jsonl", metavar="DIR", help="also import an earlier JSONL snapshot")
    i.add_argument("--full", action="store_true", help="read every transcript again, not only the ones that changed since the last ingest")
    r = _add(sub, "report")
    r.add_argument("--days", type=int, default=30, help="window in days (0 = all)")
    r.add_argument("--tenant")
    r.add_argument("--project")
    r.add_argument("--account", help="account id (see the dashboard or accounts.json)")
    dy = _add(sub, "day")
    dy.add_argument("--date", help="YYYY-MM-DD (default: today)")
    dy.add_argument("--tenant")
    dy.add_argument("--project")
    dy.add_argument("--account")
    tl = _add(sub, "tools")
    tl.add_argument("--days", type=int, default=30, help="window in days (0 = all)")
    tl.add_argument("--project", help="substring of the project directory")
    _add(sub, "verify")
    sl = _add(sub, "statusline")
    sl.add_argument("--chain-file", help="shell file with the previous status line command to run first")
    _add(sub, "statusline-install")
    _add(sub, "statusline-uninstall")
    hk = _add(sub, "hook")
    from . import hooks
    hk.add_argument("event", choices=[h.arg for h in hooks.HOOKS])
    _add(sub, "hook-install")
    _add(sub, "hook-uninstall")
    al = _add(sub, "alerts")
    al.add_argument("--test", action="store_true", help="send a test notification")
    alog = _add(sub, "alert-log")
    alog.add_argument("--days", type=int, default=7)
    rv = _add(sub, "review")
    rv.add_argument("--date", help="run date YYYY-MM-DD (default today; reviews the last active day before it)")
    rv.add_argument("--notify", action="store_true", help="send it now through the configured channels")
    rv.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    rv.add_argument("--json", action="store_true")
    _add(sub, "review-install")
    _add(sub, "review-uninstall")
    d = _add(sub, "dashboard")
    d.add_argument("--port", type=int, default=8787)
    d.add_argument("--no-open", action="store_true")
    dm = _add(sub, "demo")
    dm.add_argument("--port", type=int, default=8787)
    dm.add_argument("--no-open", action="store_true")
    dm.add_argument("--days", type=int, default=30)
    dm.add_argument("--seed", type=int, default=7)
    su = _add(sub, "setup")
    su.add_argument("--yes", action="store_true", help="accept every default, no questions")
    su.add_argument("--scope", choices=["global", "project", "company"], help="where the hook and status line go (default global)")
    su.add_argument("--interval", type=int, metavar="MIN", help="ingest every MIN minutes (saved in alerts.json)")
    su.add_argument("--time", metavar="HH:MM", help="daily review time (saved in alerts.json)")
    su.add_argument("--cut-dest", choices=["local", "shared"], help="project scope: put the «when to cut» rule in CLAUDE.local.md "
                    "(personal, out of git; recommended) or CLAUDE.md (shared via git)")
    su.add_argument("--gitignore", action="store_true",
                    help="project scope: add CLAUDE.local.md to the project's .gitignore if git does not ignore it")
    ck = _add(sub, "check")
    ck.add_argument("--json", action="store_true")
    ck.add_argument("--with-claude", action="store_true", help="also run `claude doctor` and show its output")
    un = _add(sub, "uninstall")
    un.add_argument("--all", action="store_true")
    un.add_argument("--purge", action="store_true", help="also delete the sessioncoach data directory (asks first)")
    un.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    ii = _add(sub, "ingest-install")
    ii.add_argument("--interval", type=int, metavar="MIN")
    _add(sub, "ingest-uninstall")
    _add(sub, "handoff-install")
    _add(sub, "handoff-uninstall")
    _add(sub, "resume-hook-install")
    _add(sub, "resume-hook-uninstall")
    _add(sub, "idle-guard-on")
    _add(sub, "idle-guard-off")
    ic = _add(sub, "idle-compact")
    ic.add_argument("--days", type=int, default=30, help="window in days")
    ic.add_argument("--json", action="store_true")
    _add(sub, "version")
    hp = _add(sub, "help")
    hp.add_argument("command", nargs="?", help="a command: its value first, then its options and technical detail")
    return p, sub


def cmd_help(args, sub):
    """Every command with its value line first and its syntax after; or one command's full help."""
    if args.command:
        if args.command not in sub.choices:
            print("sessioncoach help: unknown command %r (see `sessioncoach help`)" % args.command)
            return 2
        print(sub.choices[args.command].format_help(), end="")
        return 0
    print("sessioncoach — what each command gives you, then how to run it. `sessioncoach help <command>` for the detail.\n")
    for name, sp in sub.choices.items():
        print("  %s\n      %s\n      %s" % (name, VALUE[name], " ".join(sp.format_usage().split())[len("usage: "):]))
    return 0


def main(argv=None):
    paths.utf8_stdio()  # ≥ ✓ ⚠ — even when redirected to a file or a pipe on Windows (the ANSI code page)
    p, sub = build_parser()
    args = p.parse_args(argv)
    handlers = {"ingest": cmd_ingest, "report": cmd_report, "day": cmd_day, "alerts": cmd_alerts, "alert-log": cmd_alert_log,
                "tools": cmd_tools,
                "statusline": cmd_statusline, "statusline-install": cmd_statusline_install,
                "statusline-uninstall": cmd_statusline_uninstall, "verify": cmd_verify, "hook": cmd_hook,
                "hook-install": cmd_hook_install, "hook-uninstall": cmd_hook_uninstall, "dashboard": cmd_dashboard, "demo": cmd_demo,
                "review": cmd_review, "review-install": cmd_review_install, "review-uninstall": cmd_review_uninstall,
                "setup": cmd_setup, "check": cmd_check, "uninstall": cmd_uninstall,
                "ingest-install": cmd_ingest_install, "ingest-uninstall": cmd_ingest_uninstall,
                "idle-guard-on": cmd_idle_guard_on, "idle-guard-off": cmd_idle_guard_off,
                "idle-compact": cmd_idle_compact,
                "handoff-install": cmd_handoff_install, "handoff-uninstall": cmd_handoff_uninstall,
                "resume-hook-install": cmd_resume_hook_install, "resume-hook-uninstall": cmd_resume_hook_uninstall}
    if args.cmd == "version":
        print(__version__)
    elif args.cmd == "help":
        return cmd_help(args, sub)
    elif args.cmd in handlers:
        return handlers[args.cmd](args) or 0
    else:
        p.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
