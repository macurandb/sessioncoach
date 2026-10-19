"""Plan limits (5-hour window, weekly cap) from Claude Code's status line, with alerts.

Claude Code passes `rate_limits.five_hour` / `rate_limits.seven_day` (percent used and reset
time) to the status line command on Pro and Max plans. `sessioncoach statusline` reads that JSON, keeps a
sample, prints a compact line, and sends a desktop notification when a threshold is crossed or
when the current pace would exhaust a window before it resets. It never fails the status line:
any error just prints whatever the chained command printed.

Only numbers are stored: percentages, reset times, context use, model id, session id.
"""
import contextlib
import copy
import json
import os
import subprocess
import sys
import time

from . import config, paths, store

DEFAULT_ALERTS = config.DEFAULT_ALERTS  # lives in config; kept here for older callers
SAMPLE_EVERY_S = 60
# what the status line gives the user, said by `sessioncoach setup` before asking
VALUE = ("See the 5-hour and weekly plan limits, and when they reset, on every turn: a limit does not catch you "
         "in the middle of a task. Your own status line keeps running first.")
STATUSLINE_BUSY_S = 2  # the status line must not hang behind a long write: it gives up and shows nothing

MSG = {
    "es": {"limit_t": "Claude · límite {w} al {p}%", "limit_m": "Se reinicia a las {r}. Mira qué lo está consumiendo en el dashboard.",
           "pace_t": "Claude · a este ritmo agotas el límite {w}", "pace_m": "Llegarías al 100% hacia las {e}; se reinicia a las {r}.",
           "ctx_t": "Claude · contexto al {p}%", "ctx_m": "Considera /compact o una sesión nueva para esta tarea.",
           "ctxtok_t": "Claude · {k}k tokens de contexto",
           "ctxtok_m": "Cada turno relee todo ese contexto. /compact o una sesión nueva por tarea abaratan mucho los siguientes turnos.",
           "budget_t": "Presupuesto «{n}» al {p}%", "budget_m": "{u} de {l} (equivalente API) en {per}.",
           "budget_proj_t": "Presupuesto «{n}»: a este ritmo lo superas",
           "budget_proj_m": "Proyección a fin de mes: {p} de {l} (equivalente API)."},
    "en": {"limit_t": "Claude · {w} limit at {p}%", "limit_m": "Resets at {r}. See what is consuming it in the dashboard.",
           "pace_t": "Claude · at this pace you'll hit the {w} limit", "pace_m": "You'd reach 100% around {e}; it resets at {r}.",
           "ctx_t": "Claude · context at {p}%", "ctx_m": "Consider /compact or a new session for this task.",
           "ctxtok_t": "Claude · {k}k tokens of context",
           "ctxtok_m": "Every turn re-reads that whole context. /compact or one session per task makes the next turns much cheaper.",
           "budget_t": "Budget “{n}” at {p}%", "budget_m": "{u} of {l} (API-equivalent) in {per}.",
           "budget_proj_t": "Budget “{n}”: on pace to exceed it",
           "budget_proj_m": "Month-end projection: {p} of {l} (API-equivalent)."},
}


def msg(key, **kw):
    return MSG.get(config.notice_lang(load_alerts()), MSG["en"])[key].format(**kw)


def hhmm(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))
PACE_LOOKBACK_S = 30 * 60


load_alerts = config.load_alerts  # lives in config; kept here for older callers


def notify(title, message):
    """Desktop notification. Never raises.

    Order: `notifier` from alerts.json (a command list with {title}/{message}); terminal-notifier
    if installed (optional: clicking opens the dashboard); otherwise osascript on macOS. Runs synchronously (about 0.15 s) so
    the notification is not lost when a short-lived parent process exits. Every alert is also
    appended to <data dir>/logs/alerts.log, so nothing is lost if notifications are blocked.
    """
    try:
        paths.ensure_data_dir("logs")
        with open(os.path.join(paths.data_dir(), "logs", "alerts.log"), "a", encoding="utf-8") as fh:
            fh.write("%s\t%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), title, message))
    except OSError:
        pass
    cfg = load_alerts()
    env = None
    custom = cfg.get("notifier")
    tn = _which("terminal-notifier")
    if custom:
        cmd = [c.replace("{title}", title).replace("{message}", message) for c in custom]
    elif tn and cfg.get("use_terminal_notifier", True):
        # optional: clicking opens the dashboard, and a newer alert replaces the previous one
        cmd = [tn, "-title", title, "-message", message, "-group", "sessioncoach",
               "-open", cfg.get("dashboard_url", "http://127.0.0.1:8787/")]
    elif sys.platform == "darwin":
        # text goes in as argv: inside the script AppleScript rejects non-ASCII escapes
        cmd = ["/usr/bin/osascript", "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
               "-e", "end run", title, message]
    elif sys.platform == "win32":
        cmd = _toast_cmd()
        env = dict(os.environ, KIT_TOAST_TITLE=title, KIT_TOAST_MESSAGE=message)
    elif _which("notify-send"):
        cmd = [_which("notify-send"), "--app-name=sessioncoach", title, message]
    else:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, env=env, check=False)


TOAST_PS = (
    "[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime];"
    "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
    "$t = $x.GetElementsByTagName('text');"
    "[void]$t.Item(0).AppendChild($x.CreateTextNode($env:KIT_TOAST_TITLE));"
    "[void]$t.Item(1).AppendChild($x.CreateTextNode($env:KIT_TOAST_MESSAGE));"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('sessioncoach').Show("
    "[Windows.UI.Notifications.ToastNotification]::new($x))")

def _toast_cmd():
    """Windows toast through PowerShell's built-in WinRT types (no modules). Text goes in env vars, so it needs no quoting."""
    return ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", TOAST_PS]


def _which(name):
    """shutil.which plus Homebrew prefixes (launchd jobs run with a minimal PATH)."""
    import shutil
    found = shutil.which(name)
    if found:
        return found
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return None


def already_sent(con, key):
    """True when `key` was already alerted; otherwise records it. Safe with concurrent writers."""
    cur = con.execute("INSERT OR IGNORE INTO alerts_sent (key, ts) VALUES (?, ?)", (key, int(time.time())))
    return cur.rowcount == 0


def pace(con, window, resets_at, now, pct):
    """Projected epoch when `window` reaches 100 % at the recent pace, or None."""
    col = "five_pct" if window == "five_hour" else "seven_pct"
    rcol = "five_reset" if window == "five_hour" else "seven_reset"
    row = con.execute("SELECT ts, %s AS p FROM limits_samples WHERE %s = ? AND ts >= ? ORDER BY ts LIMIT 1"
                      % (col, rcol), (resets_at, now - PACE_LOOKBACK_S)).fetchone()
    if not row or row["p"] is None or now - row["ts"] < 300 or pct <= row["p"]:
        return None
    rate = (pct - row["p"]) / (now - row["ts"])  # percent per second
    return int(now + (100 - pct) / rate)


def record_and_alert(con, data, now=None, cfg=None, root=None):
    """Store a sample (throttled) and fire alerts. Returns a dict for the status line."""
    now = int(now or time.time())
    cfg = cfg or load_alerts()
    rl = data.get("rate_limits") or {}
    five, seven = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    cw = data.get("context_window") or {}
    ctx = cw.get("used_percentage")
    cu = cw.get("current_usage") or {}
    ctx_tokens = sum((cu.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")) or None
    sid = data.get("session_id")
    out = {"five": five, "seven": seven, "ctx": ctx, "ctx_tokens": ctx_tokens, "eta": {}}
    last = con.execute("SELECT ts, five_pct, seven_pct FROM limits_samples WHERE session_id IS ? ORDER BY ts DESC LIMIT 1",
                       (sid,)).fetchone()
    changed = not last or (last["five_pct"], last["seven_pct"]) != (five.get("used_percentage"), seven.get("used_percentage"))
    if (five or seven) and (changed or now - last["ts"] >= SAMPLE_EVERY_S):
        con.execute("INSERT INTO limits_samples VALUES (?,?,?,?,?,?,?,?,?)",
                    (now, sid, root, five.get("used_percentage"), five.get("resets_at"), seven.get("used_percentage"),
                     seven.get("resets_at"), ctx, (data.get("model") or {}).get("id")))
    for key, w, label in (("five_hour", five, "5 h"), ("seven_day", seven, "7 d")):
        pct, reset = w.get("used_percentage"), w.get("resets_at")
        if pct is None or not reset:
            continue
        for th in sorted(cfg.get(key) or [], reverse=True):
            if pct >= th:
                if not already_sent(con, "%s:%s:%s" % (key, reset, th)) and cfg.get("notify", True):
                    store.add_alert_event(con, key, {"pct": pct, "threshold": th, "reset": reset}, session_id=sid, ts=now)
                    notify(msg("limit_t", w=label, p=round(pct)), msg("limit_m", r=hhmm(reset)))
                break
        if cfg.get("projection", True):
            eta = pace(con, key, reset, now, pct)
            if eta and eta < reset:
                out["eta"][key] = eta
                if not already_sent(con, "%s:%s:pace" % (key, reset)) and cfg.get("notify", True):
                    store.add_alert_event(con, key + "_pace", {"pct": pct, "eta": eta, "reset": reset}, session_id=sid, ts=now)
                    notify(msg("pace_t", w=label), msg("pace_m", e=hhmm(eta), r=hhmm(reset)))
    if ctx is not None:
        for th in cfg.get("context") or []:
            if ctx >= th and sid and not already_sent(con, "ctx:%s:%s" % (sid, th)) and cfg.get("notify", True):
                store.add_alert_event(con, "context", {"pct": ctx, "threshold": th}, session_id=sid, ts=now)
                notify(msg("ctx_t", p=round(ctx)), msg("ctx_m"))
    if ctx_tokens:
        # every turn re-reads the whole context: past a few hundred k tokens each turn gets expensive
        for th in sorted(cfg.get("context_tokens") or [], reverse=True):
            if ctx_tokens >= th:
                if sid and not already_sent(con, "ctxtok:%s:%s" % (sid, th)) and cfg.get("notify", True):
                    store.add_alert_event(con, "context_tokens", {"context": ctx_tokens, "threshold": th}, session_id=sid, ts=now)
                    notify(msg("ctxtok_t", k=int(ctx_tokens / 1000)), msg("ctxtok_m"))
                break
    con.commit()
    return out


def render(info, color=True):
    def c(pct, s):
        if not color or pct is None:
            return s
        code = "31" if pct >= 90 else ("33" if pct >= 75 else "2")
        return "\033[%sm%s\033[0m" % (code, s)
    parts = []
    for key, label in (("five", "5h"), ("seven", "7d")):
        w = info[key]
        if w.get("used_percentage") is not None:
            s = "%s %d%%" % (label, round(w["used_percentage"]))
            if w.get("resets_at"):
                fmt = "%H:%M" if key == "five" else "%a %H:%M"
                s += " ↻%s" % time.strftime(fmt, time.localtime(w["resets_at"]))
            eta = info["eta"].get("five_hour" if key == "five" else "seven_day")
            if eta:
                s += " ⚠ 100%% ~%s" % time.strftime("%H:%M", time.localtime(eta))
            parts.append(c(w["used_percentage"], s))
    if info.get("ctx_tokens"):
        k = info["ctx_tokens"] / 1000
        level = 95 if k >= 500 else (80 if k >= 300 else 0)
        parts.append(c(level, "ctx %dk" % round(k)))
    elif info.get("ctx") is not None:
        parts.append(c(info["ctx"], "ctx %d%%" % round(info["ctx"])))
    return " · ".join(parts)


def git_bash():
    """Git for Windows' bash.exe, or None. Not `bash` from PATH: that can be the WSL launcher in System32."""
    import shutil
    git = shutil.which("git")
    cands = []
    if git:
        root = os.path.dirname(os.path.dirname(os.path.abspath(git)))  # ...\Git\cmd\git.exe -> ...\Git
        cands += [os.path.join(root, "bin", "bash.exe"), os.path.join(os.path.dirname(root), "bin", "bash.exe")]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(var)
        if base:
            cands.append(os.path.join(base, "Git", "bin", "bash.exe"))
            cands.append(os.path.join(base, "Programs", "Git", "bin", "bash.exe"))
    return next((c for c in cands if os.path.isfile(c)), None)

def _chain_argv(chain_file):
    """argv that runs the saved previous status line command the way Claude Code would have.

    POSIX: `sh <file>`. Windows: Claude Code uses Git Bash when installed, else PowerShell, so the
    saved command line is run by the same shell.
    """
    if sys.platform != "win32":
        return ["/bin/sh", chain_file]
    with open(chain_file, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    bash = git_bash()
    if bash:
        return [bash, "-c", text]
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", text]


def statusline_main(chain_file=None):
    paths.utf8_stdio()
    raw = sys.stdin.read()
    prefix = ""
    if chain_file and os.path.exists(chain_file):
        try:
            res = subprocess.run(_chain_argv(chain_file), input=raw, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=3, check=False)
            prefix = (res.stdout or "").rstrip("\n")
        except (OSError, subprocess.SubprocessError):
            prefix = ""
    line = ""
    try:
        data = json.loads(raw) if raw.strip() else {}
        con = store.connect(paths.db_path(), timeout=STATUSLINE_BUSY_S)
        try:
            cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(paths.HOME, ".claude")
            info = record_and_alert(con, data, root=os.path.normpath(os.path.join(cfg_dir, "projects")))
        finally:
            con.close()
        line = render(info)
    except Exception:  # never break the status line
        line = ""
    print(" │ ".join(p for p in (prefix, line) if p))


def is_ours(command):
    """Any format ever written (bin/sessioncoach, bin/kit, `python -m sessioncoach` or `python -m kit`)."""
    return "statusline --chain-file" in (command or "") and paths.mentions_us(command)

_is_ours = is_ours  # the name it had before it was shared between modules

def our_command(settings):
    """The status line command in `settings` when it is sessioncoach's (any format), else None."""
    line = settings.get("statusLine") if isinstance(settings, dict) else None
    cmd = line.get("command") if isinstance(line, dict) else None
    return cmd if isinstance(cmd, str) and is_ours(cmd) else None


def installed(settings_path=None):
    settings_path = settings_path or os.path.join(paths.HOME, ".claude", "settings.json")
    try:
        with open(settings_path, encoding="utf-8") as fh:
            return is_ours(((json.load(fh) or {}).get("statusLine") or {}).get("command"))
    except (OSError, ValueError, AttributeError):
        return False


def chain_path(settings_path):
    """Chain file for a settings file: the global one keeps statusline-chain.sh, every other scope
    (a project, a company config dir) gets its own, so installing in one never overwrites another."""
    glob = os.path.realpath(os.path.join(paths.HOME, ".claude", "settings.json"))
    if os.path.realpath(settings_path) == glob:
        return os.path.join(paths.data_dir(), "statusline-chain.sh")
    base = os.path.dirname(os.path.abspath(settings_path))
    if os.path.basename(settings_path) == "settings.local.json" and os.path.basename(base) == ".claude":
        base = os.path.dirname(base)  # <project>/.claude/settings.local.json -> the project
    import hashlib
    return os.path.join(paths.data_dir(), "chains", hashlib.sha1(os.path.realpath(base).encode()).hexdigest()[:10],
                        "statusline-chain.sh")


def _chain_of(command):
    """The --chain-file a sessioncoach status line command points to, or None."""
    try:
        parts = paths.split_command(command)
    except ValueError:
        return None
    return parts[parts.index("--chain-file") + 1] if "--chain-file" in parts[:-1] else None


def _dump(s):
    return (json.dumps(s, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def plan_install(settings, chain, kit_bin=None):
    """Pure: chain sessioncoach's status line after the current one.

    -> (new settings dict, chain file text), or None when it is already installed in the current format.
    One installed with the old command (bin/kit, -m kit) is rewritten in place to the current one, keeping its
    own --chain-file: -> (new settings dict, None), the chain file is not touched. `settings` is not modified.
    The status line's other keys (padding...) stay where they are; only type and command change."""
    line = settings.get("statusLine")
    if line is not None and not isinstance(line, dict):
        raise ValueError("statusLine in settings is a %s, not an object: fix it by hand" % type(line).__name__)
    cur = (line or {}).get("command") or ""
    cur = cur if isinstance(cur, str) else ""
    if is_ours(cur):  # any format: never chain sessioncoach after itself
        chain_cur = _chain_of(cur)
        cmd = paths.kit_command_str("statusline", "--chain-file", chain_cur, kit_bin=kit_bin) if chain_cur else cur
        if not paths.old_command_name(cur) or cmd == cur:
            return None
        new = copy.deepcopy(settings)
        new["statusLine"]["command"] = cmd
        return new, None
    new = copy.deepcopy(settings)
    ours = paths.kit_command_str("statusline", "--chain-file", chain, kit_bin=kit_bin)
    new["statusLine"] = dict({"type": "command", "command": ours}, **{k: v for k, v in (line or {}).items()
                                                                       if k not in ("type", "command")})
    return new, (cur + "\n" if cur else ":\n")


def installed_chain(settings):
    """The chain file the installed status line command names, or None."""
    return _chain_of(our_command(settings))


def plan_uninstall(settings, previous):
    """Pure: put back the status line kept in the chain file (`previous`: its text, or None if absent), with the other
    keys the line has now (install kept the user's). No previous line: the whole object goes.
    -> new settings dict; `settings` is not modified."""
    new = copy.deepcopy(settings)
    prev = (previous or "").strip()
    line = new.get("statusLine")
    if prev and prev != ":":
        new["statusLine"] = dict(line if isinstance(line, dict) else {}, type="command", command=prev)
    else:
        new.pop("statusLine", None)
    return new


def read_chain(chain):
    if not os.path.exists(chain):
        return None
    with open(chain, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def install(settings_path=None, kit_bin=None, chain=None):
    """Chain sessioncoach's status line after the current one (kept verbatim in a chain file)."""
    settings_path = settings_path or os.path.join(paths.HOME, ".claude", "settings.json")
    chain = chain or os.path.join(paths.data_dir(), "statusline-chain.sh")
    if os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8") as fh:
            s = json.load(fh)
    else:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        s = {}
    plan = plan_install(s, chain, kit_bin)
    if plan is None:
        return "already installed"
    new, chain_text = plan
    if chain_text is None:  # an old command rewritten in place: its chain file stays as it is
        backup = paths.backup_file(settings_path, ".bak-statusline")
        paths.write_atomic(settings_path, _dump(new), paths.SETTINGS_MODE, backup=False)
        return "updated to the current command (settings backup %s)" % backup
    os.makedirs(os.path.dirname(chain), mode=0o700, exist_ok=True)
    fd = os.open(chain, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:  # LF even on Windows: bash -c would choke on CR
        fh.write(chain_text)
    os.chmod(chain, 0o600)
    backup = paths.backup_file(settings_path, ".bak-statusline")
    paths.write_atomic(settings_path, _dump(new), paths.SETTINGS_MODE, backup=False)  # the .bak-statusline copy above is the backup
    return "installed (previous command kept in %s; settings backup %s)" % (chain, backup)


def uninstall(settings_path=None):
    settings_path = settings_path or os.path.join(paths.HOME, ".claude", "settings.json")
    with open(settings_path, encoding="utf-8") as fh:
        s = json.load(fh)
    if not our_command(s):  # someone else's status line (or none): not ours to remove or replace
        return "not installed"
    # restore what this scope had: the chain file named in the installed command, not another scope's
    chain = installed_chain(s) or os.path.join(paths.data_dir(), "statusline-chain.sh")
    new = plan_uninstall(s, read_chain(chain))
    paths.backup_file(settings_path, ".bak-statusline")
    paths.write_atomic(settings_path, _dump(new), paths.SETTINGS_MODE, backup=False)
    if installed_chain(s) == chain:  # restored: a later uninstall must not bring it back
        with contextlib.suppress(FileNotFoundError):
            os.remove(chain)
    return "restored previous status line"


def evaluate_budgets(con, cfg=None, today=None, notify_fn=None, dry=False):
    """Budgets in API-equivalent USD per day or month, for all / a tenant / a project / an account.

    alerts.json: {"budgets": [{"name": "shop-month", "scope": "project", "match": "shop",
                               "period": "month", "equiv_usd": 800, "thresholds": [80, 100]}]}
    Returns the status of every budget; notifies once per budget, period and threshold.
    """
    import datetime as dt

    from . import analysis
    cfg = cfg or load_alerts()
    notify_fn = notify_fn or notify
    today = today or dt.date.today()
    budgets = cfg.get("budgets") or []
    if not budgets:
        return []
    dims = analysis.Dims(con)
    out = []
    for b in budgets:
        period = b.get("period", "month")
        start = today if period == "day" else today.replace(day=1)
        used = 0.0
        scope, match = b.get("scope", "all"), b.get("match")
        # SQLite keeps the period and the budget's scope; the checks below stay as the exact rule
        narrow = dims.scope(con, tenant=match if scope == "tenant" else None, project=match if scope == "project" else None,
                            account=match if scope == "account" else None, tables=("cost_slices",))
        for s in analysis.select(con, "cost_slices", [narrow, analysis.day_range(start.isoformat(), today.isoformat())],
                                 order="day, rowid"):
            if s["day"] < start.isoformat() or s["day"] > today.isoformat():
                continue
            p, t, a = dims(s)
            if (scope == "all" or (scope == "project" and p == match) or (scope == "tenant" and t == match)
                    or (scope == "account" and a["id"] == match)):
                used += s["usd"]
        limit = float(b.get("equiv_usd") or 0)
        pct = used / limit * 100 if limit else 0.0
        proj = None
        if period == "month":
            import calendar
            month_days = calendar.monthrange(today.year, today.month)[1]
            proj = used / today.day * month_days
        out.append({"name": b.get("name"), "scope": b.get("scope", "all"), "match": b.get("match"), "period": period,
                    "used_usd": round(used, 2), "limit_usd": limit, "pct": round(pct, 1),
                    "projected_usd": round(proj, 2) if proj is not None else None,
                    "projected_pct": round(proj / limit * 100, 1) if proj is not None and limit else None})
        if (not dry and proj is not None and limit and proj > limit and today.day >= 3
                and not already_sent(con, "budget:%s:%s:proj" % (b.get("name"), start.isoformat())) and cfg.get("notify", True)):
            store.add_alert_event(con, "budget_projection", {"name": b.get("name"), "projected_usd": proj, "limit_usd": limit})
            notify_fn(msg("budget_proj_t", n=b.get("name")), msg("budget_proj_m", p="$%.0f" % proj, l="$%.0f" % limit))
        for th in ([] if dry else sorted(b.get("thresholds") or [80, 100], reverse=True)):
            if pct >= th:
                key = "budget:%s:%s:%s" % (b.get("name"), start.isoformat(), th)
                if not already_sent(con, key) and cfg.get("notify", True):
                    store.add_alert_event(con, "budget", {"name": b.get("name"), "used_usd": used, "limit_usd": limit, "threshold": th})
                    notify_fn(msg("budget_t", n=b.get("name"), p=round(pct)),
                              msg("budget_m", u="$%.0f" % used, l="$%.0f" % limit, per=period))
                break
    if not dry:
        con.commit()
    return out
