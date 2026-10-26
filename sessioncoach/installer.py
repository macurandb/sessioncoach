"""Install, check and uninstall everything sessioncoach puts on the machine (`sessioncoach setup | check | uninstall`).

Pieces: the scheduled ingest, the chained status line, the resume-warning hook, the scheduled
daily review (LaunchAgent on macOS, systemd user timer or cron on Linux, Task Scheduler on Windows;
see scheduler.py), the /handoff command (handoff.py), the hook that loads the handoff after /clear
(resume.py) and, opt-in, the idle guard (alerts.json → nudge.block, plus the PreCompact hook of
idlecompact.py). Settings files are only edited with a backup first; data in the kit data dir is never
removed unless `--purge`.
"""
import collections
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap

from . import (
    __version__,
    analysis,
    config,
    handoff,
    hooks,
    idlecompact,
    idleguard,
    keepctx,
    limits,
    nudge,
    paths,
    resume,
    review,
    scheduler,
    store,
)
from . import settings as settings_mod

INGEST_LABEL = scheduler.launchd_label("ingest")
DEFAULT_INTERVAL_MIN = config.DEFAULT_INTERVAL_MIN
DEFAULT_RETENTION_DAYS = 30
SCOPES = ("global", "project", "company")
OK, WARN, BAD = "ok", "warn", "bad"
# what the scheduled ingest gives the user, said by `sessioncoach setup` before asking
INGEST_VALUE = ("Claude Code deletes its transcripts after %(retention)d days. A scheduled ingest keeps their numbers, so your "
                "history, the dashboard and the daily review outlive them, without you remembering to run it.")
MARK = {OK: "✓", WARN: "⚠", BAD: "✗"}


def is_mac():
    return sys.platform == "darwin"


# ---- settings scopes ----

def settings_for(scope, cwd=None):
    if scope == "global":
        return os.path.join(paths.HOME, ".claude", "settings.json")
    if scope == "project":
        return os.path.join(cwd or os.getcwd(), ".claude", "settings.local.json")
    if scope == "company":
        cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        if not cfg:
            raise ValueError("--scope company needs CLAUDE_CONFIG_DIR")
        return os.path.join(os.path.expanduser(cfg), "settings.json")
    raise ValueError("scope must be one of %s" % ", ".join(SCOPES))


def known_settings(cwd=None):
    """[(label, path)] of every settings file kit could live in: global, this project, config dirs."""
    out, seen = [], set()

    def add(label, path):
        key = os.path.realpath(path)
        if key not in seen:
            seen.add(key)
            out.append((label, path))
    add("global", settings_for("global"))
    add("project", settings_for("project", cwd))
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        add("company (CLAUDE_CONFIG_DIR)", os.path.join(os.path.expanduser(cfg), "settings.json"))
    for root in paths.transcript_roots():
        add("config dir " + os.path.dirname(root), os.path.join(os.path.dirname(root), "settings.json"))
    return out


def resume_visible(settings_path):
    """True when every session that sees the /handoff of `settings_path` also runs the resume hook, so the command needs no
    line to paste: the hook is in that settings file, or, for a project, in the project's other settings file or in the
    user-level settings Claude Code loads with it. Never depends on the directory sessioncoach runs from."""
    if resume.installed(settings_path):
        return True
    if os.path.basename(settings_path) != "settings.local.json":
        return False
    claude_dir = os.path.dirname(settings_path)
    others = [os.path.join(claude_dir, "settings.json")]
    others += [p for _, p in settings_mod.project_superiors(os.path.dirname(claude_dir), paths.HOME)]
    return any(resume.installed(p) for p in others)


def refresh_handoff(cwd=None):
    """Rewrite every installed /handoff for the resume hook's current state. -> [(label, result)]."""
    return [(label, r) for label, r in ((lb, handoff.refresh(p, resume_visible(p))) for lb, p in known_settings(cwd)) if r]


# ---- alerts.json (lives in config; the names stay here for older callers) ----

alerts_path = config.alerts_path
update_alerts = config.update_alerts
ingest_interval = config.ingest_interval


# ---- hooks (hooks.HOOKS) ----

# checked and removed per scope after the status line; the prompt hook goes with it (_scope_items, uninstall_all)
OTHER_HOOKS = tuple(s for s in hooks.HOOKS if s is not hooks.PROMPT)
GUARD_HOOKS = tuple(s for s in hooks.HOOKS if s.idle_guard)  # installed and removed with the idle guard


def _remove_hooks(label, path, specs):
    """Uninstall each hook of `specs` found in `path` (keepctx: or its command file). -> lines."""
    out = []
    for spec in specs:
        mod = spec.module()
        if getattr(mod, "present", mod.installed)(path):
            out.append("%s [%s]: %s" % (spec.label, label, mod.uninstall(path)))
    return out


# ---- idle guard ----

def set_idle_guard(on, settings_path=None, kit_bin=None):
    """Turn the idle guard on or off: alerts.json → nudge.block, and the PreCompact hook that tells it when Claude Code
    prepared its idle summary (on: in `settings_path`; off: from every scope, with the guard's state files). -> lines."""
    lines = []
    if on:
        if not idleguard.enabled():
            update_alerts(lambda c: c.setdefault("nudge", {}).__setitem__("block", True))
        lines.append("idle guard: on (alerts.json → nudge.block)")
        lines += ["%s: %s" % (s.label, s.module().install(settings_path or settings_for("global"), kit_bin)) for s in GUARD_HOOKS]
        return lines
    nd = limits.load_alerts().get("nudge")
    if isinstance(nd, dict) and "block" in nd:
        update_alerts(lambda c: c["nudge"].pop("block", None))
    lines.append("idle guard: off")
    for label, path in known_settings():
        lines += _remove_hooks(label, path, GUARD_HOOKS)
    for mod in (idleguard, idlecompact, keepctx):
        if mod.remove():
            lines.append("removed %s" % mod.path())
    return lines


def idle_guard_problems(cwd=None):
    """What the idle guard needs and is not installed in any scope: [(check name, why — fix)]."""
    found = [p for _, p in known_settings(cwd)]
    return [s.guard_need for s in hooks.HOOKS if s.guard_need and not any(hooks.installed(s, p) for p in found)]


def idle_compact_threshold(cwd=None):
    """CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS as every settings scope and this shell set it: (value or None, [(warn, why)])."""
    files = [(label, _read_json(p) if os.path.exists(p) else None) for label, p in known_settings(cwd)]
    return idlecompact.min_tokens_report(files, os.environ)


# ---- ingest job ----

def ingest_plist_path(agents_dir=None):
    return scheduler.plist_path(INGEST_LABEL, agents_dir)


def ingest_job(interval_min):
    return scheduler.Job("ingest", ["ingest"], every_min=int(interval_min))

def ingest_install(interval_min=None, kit_bin=None, agents_dir=None, load=True):
    interval_min = interval_min or ingest_interval()
    log = os.path.join(paths.ensure_data_dir("logs"), "ingest.log")
    be = scheduler.backend()
    installed = scheduler.install(ingest_job(interval_min), kit_bin, log, unit_dir=agents_dir, load=load, be=be)
    if be == "launchd":
        return "%s: sessioncoach ingest every %d min (log %s)" % (installed, interval_min, log)
    return "%s [every %d min]" % (installed, interval_min)


def ingest_uninstall(agents_dir=None, load=True):
    return scheduler.uninstall(ingest_job(DEFAULT_INTERVAL_MIN), unit_dir=agents_dir, load=load)


launchd_loaded = scheduler.launchd_loaded  # kept: check(load_fn=...) and older callers


# ---- setup ----

def _ask(question, default, yes, inp):
    if yes:
        return default
    suffix = " [Y/n] " if default is True else " [y/N] " if default is False else " [%s] " % default
    try:
        ans = inp(question + suffix).strip()
    except EOFError:
        return default
    if isinstance(default, bool):
        return default if not ans else ans.lower() in ("y", "yes", "s", "si", "sí")
    return ans or default


def setup_figures(cwd=None):
    """Numbers of the user's own that setup quotes next to a piece: transcript retention, and the resumes after the cache
    expired in the last 7 days (read-only; absent before the first ingest)."""
    fig = {"retention": retention_days(cwd)}
    if os.path.exists(paths.db_path()):
        try:
            con = store.open_readonly(paths.db_path())
            try:
                fig["resumes"], fig["resume_usd"] = analysis.resumes_since(con, 7)
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return fig


def _explain(out, title, value, figure=None, fig=None):
    """A piece's title and what it gives the user, before its question (and with --yes, as the record of what was set up)."""
    text = value % (fig or {}) if "%(" in value else value
    if figure and fig and fig.get("resumes"):
        text += " " + figure % fig
    out("")
    out(title)
    for line in textwrap.wrap(text, 100):
        out("  " + line)


def _step(out, name, fn):
    try:
        out("  %s: %s" % (name, fn()))
        return True
    except Exception as e:  # keep going: each piece is independent
        out("  %s: FAILED (%s)" % (name, e))
        return False


CUT_DEST_HELP = {
    "local": "CLAUDE.local.md: personal, stays out of git (recommended); Claude Code loads it together with CLAUDE.md.",
    "shared": "CLAUDE.md: shared with your team through git.",
}

def known_dir_names():
    """Encoded working-dir names in the database (read-only), [] when there is none."""
    path = paths.db_path()
    if not os.path.exists(path):
        return []
    try:
        con = store.open_readonly(path)
        try:
            names = {r[0] for r in con.execute("SELECT DISTINCT dir FROM session_dirs")}
            names |= {r[0] for r in con.execute("SELECT DISTINCT project_dir FROM cost_slices")}
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return sorted(n for n in names if n)

_known_dir_names = known_dir_names  # the name it had before it was public

def _setup_cut_rule(proj, cut_dest, gitignore, yes, inp, out):
    """Project scope: write the cut rule where the user chooses; offer to ignore CLAUDE.local.md in git."""
    S = settings_mod
    out("  The «when to cut» rule can go in one of two files of this project:")
    for k in S.CUT_DESTS:
        out("    %-6s %s" % (k, CUT_DEST_HELP[k]))
    dest = cut_dest or _ask("  Where? local | shared", "local", yes, inp)
    if dest not in S.CUT_DESTS:
        out("  invalid choice %r; using local" % dest)
        dest = "local"
    lang = config.cut_rule_lang()
    lang = lang if lang in S.CUT_TEXT else config.CUT_RULE_LANG
    target = os.path.join(proj, "CLAUDE.local.md" if dest == "local" else "CLAUDE.md")
    try:
        text = S.read_text(target)
    except S.SettingsError:
        out("  cut rule: skipped — %s is not UTF-8; fix it by hand and rerun" % target)
        return "skipped"
    if not S.has_cut_rule(text):
        S.write_atomic(target, S.add_cut_rule(text, lang).encode("utf-8"))
        out("  cut rule: written to %s" % target)
    else:
        out("  cut rule: already in %s" % target)
    if dest == "local" and S.git_ignore_state(proj) == "not_ignored":
        gi = os.path.join(proj, ".gitignore")
        if gitignore is None:
            gitignore = False if yes else _ask("  CLAUDE.local.md is not ignored by git in this repo. Add it to %s?" % gi, True, yes, inp)
        if gitignore:
            try:
                S.write_atomic(gi, S.add_gitignore_line(S.read_bytes(gi)))
                out("  .gitignore: added CLAUDE.local.md (%s)" % gi)
            except UnicodeDecodeError:
                out("  .gitignore: skipped — %s is not UTF-8; add CLAUDE.local.md yourself" % gi)
        else:
            out("  note: CLAUDE.local.md is not ignored by git; add it to .gitignore yourself or rerun with --gitignore")
    return "done"

class _SetupRun:
    """What the pieces of one `setup` share: its arguments, the answers so far and whether a piece failed."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.failed = False
        self.dup = None  # superior scope that already has the resume-warning hook (the idle guard works through it)

    def ask(self, question, default):
        return _ask(question, default, self.yes, self.inp)

    def step(self, name, fn):
        self.failed |= not _step(self.out, name, fn)


# One piece of setup, asked in order. `when`: whether it is offered at all; `skip`: a note shown instead of the question
# (None asks); `answer`: an answer known without asking (None asks); `question`: (text, default); `run`: install it.
SetupStep = collections.namedtuple("SetupStep", "title value figure when skip answer question run")


def _always(_c):
    return True


def _none(_c):
    return None


def _run_ingest(c):
    n = c.interval or ingest_interval(c.cfg_now)
    if not c.yes and not c.interval:
        try:
            n = int(c.ask("  Ingest every how many minutes?", str(n)))
        except ValueError:
            c.out("  not a number; using %d" % n)
    if n != ingest_interval(c.cfg_now):
        update_alerts(lambda cfg: cfg.setdefault("ingest", {}).__setitem__("interval_min", n))
    c.step("ingest agent", lambda: ingest_install(n, c.kit_bin, c.agents_dir, c.load))


def _run_statusline(c):
    over = settings_mod.statusline_shadowed_by(c.sups)
    if over:
        c.out("  note: sessioncoach's status line is already in %s; the project's own line will take precedence over it" % over)
    c.step("status line", lambda: limits.install(c.settings_path, c.kit_bin, chain=limits.chain_path(c.settings_path)))


def _skip_hook(c):
    c.dup = settings_mod.hook_duplicated_in(c.sups)
    if c.dup:
        return "hook: skipped — already active in %s, which Claude Code also loads for this project (it would run twice)" % c.dup
    return None


def _run_hook(c):
    c.step("hook", lambda: nudge.install(c.settings_path, c.kit_bin))
    if c.scope not in ("global", "company"):
        return
    others = settings_mod.Settings(projects=known_dir_names).projects_with_hook_at(c.settings_path)
    if not others:
        return
    c.out("  note: these projects have their own copy of the hook, so it would run twice there:")
    for pj in others:
        c.out("    %s" % pj["label"])
    if c.ask("  Remove the hook from those projects?", False):
        for pj in others:  # a project left with its hook is not a failure of setup
            _step(c.out, "hook [%s]" % pj["label"], lambda pj=pj: nudge.uninstall(pj["settings"]))


def _run_cut_rule(c):
    c.step("cut rule", lambda: _setup_cut_rule(c.proj, c.cut_dest, c.gitignore, c.yes, c.inp, c.out))


def _run_review(c):
    default = c.rev_now["time"]
    t = c.time or c.ask("  At what time (HH:MM)?", default)
    try:
        h, m = review.parse_time(t)
        t = "%02d:%02d" % (h, m)
    except ValueError:
        c.out("  invalid time %r; using %s" % (t, default))
        t = default
    if t != (c.cfg_now.get("review") or {}).get("time"):
        update_alerts(lambda cfg: cfg.setdefault("review", {}).__setitem__("time", t))
    c.step("review agent", lambda: review.install(None, c.kit_bin, c.agents_dir, c.load))


def _skip_handoff(c):
    if c.scope == "project" and c.yes:  # like the cut rule: never written into a file git tracks without being asked
        return ("handoff command: skipped with --yes in project scope (.claude/commands/ goes to git); "
                "run setup without --yes, or sessioncoach handoff-install for your user commands")
    return None


def _run_handoff(c):
    msg = []
    c.step("handoff command", lambda: msg.append(handoff.install(c.settings_path, resume_visible(c.settings_path))) or msg[0])
    if msg and not msg[0].startswith("skipped"):
        if c.scope == "project":
            c.out("  note: .claude/commands/ of the project goes to git like the rest of .claude/ unless you ignore it")
        c.out("  " + handoff.cost_note())


def _skip_resume(c):
    above = next((lbl for lbl, p in c.sups if resume.installed(p)), None)
    return "resume hook: skipped — already active in %s, which Claude Code also loads for this project" % above if above else None


def _run_resume(c):
    c.step("resume hook", lambda: resume.install(c.settings_path, c.kit_bin))
    for label, r in refresh_handoff(c.cwd):
        c.out("  handoff command [%s]: %s" % (label, r))


def _skip_idle_guard(c):
    if idleguard.enabled():
        return "idle guard: already on (sessioncoach idle-guard-off turns it off)"
    if not (c.dup or nudge.installed(c.settings_path)):
        return "idle guard: skipped — it works through the resume-warning hook, which is not installed"
    if c.yes:  # it changes what happens to a prompt: only when asked
        return "idle guard: skipped with --yes (it holds prompts back); enable with: sessioncoach idle-guard-on"
    return None


def _run_idle_guard(c):
    c.step("idle guard", lambda: "on (alerts.json → nudge.block); " + "; ".join(set_idle_guard(True, c.settings_path, c.kit_bin)[1:]))


# the order of the questions is part of the interface: answers piped to setup are read in this order, and pieces added
# after the first release go last so that piped answers in the old order keep meaning the same
SETUP_STEPS = (
    SetupStep("Scheduled ingest", INGEST_VALUE, None, lambda c: bool(c.be), _none, _none,
              lambda c: ("Ingest transcripts periodically (%s)?" % c.what, True), _run_ingest),
    SetupStep("Status line", limits.VALUE, None, _always, _none, _none,
              lambda c: ("Chain sessioncoach's status line (plan limits + alerts) after your current one?", True), _run_statusline),
    SetupStep("Resume warning", nudge.VALUE, nudge.VALUE_FIGURE, _always, _skip_hook, _none,
              lambda c: ("Warn when you resume a big session after the cache expired (hook)?", True), _run_hook),
    SetupStep("«When to cut» rule", settings_mod.CUT_VALUE, None,
              lambda c: c.scope == "project" and bool(c.cut_dest or not c.yes), _none,
              lambda c: True if c.cut_dest else None,  # --cut-dest is the answer
              lambda c: ("Add the «when to cut» rule to this project?", True), _run_cut_rule),
    SetupStep("Daily review", review.VALUE, None, lambda c: bool(c.be), _none, _none,
              lambda c: ("Schedule the daily review (%s)?" % c.what, True), _run_review),
    SetupStep("/handoff command", handoff.VALUE, None, _always, _skip_handoff, _none,
              lambda c: ("Install the /handoff command?", True), _run_handoff),
    SetupStep("Load the handoff after /clear", resume.VALUE, None, _always, _skip_resume, _none,
              lambda c: ("After /clear, load the latest handoff automatically (SessionStart hook)?", True), _run_resume),
    SetupStep("Idle guard", nudge.BLOCK_VALUE, nudge.VALUE_FIGURE, _always, _skip_idle_guard, _none,
              lambda c: ("Hold back the first message after a pause longer than the cache (idle guard)?", False), _run_idle_guard),
)


def _run_steps(c, steps=SETUP_STEPS):
    """Each piece in order: what it gives, then a note instead of the question, or the question, then install it."""
    for st in steps:
        if not st.when(c):
            continue
        _explain(c.out, st.title, st.value, st.figure, c.fig)
        note = st.skip(c)
        if note:
            c.out("  " + note)
            continue
        answer = st.answer(c)
        if answer is None:
            answer = c.ask(*st.question(c))
        if answer:
            st.run(c)


def setup(yes=False, scope=None, interval=None, time=None, kit_bin=None, agents_dir=None, load=True,
          settings_path=None, inp=input, out=print, cwd=None, cut_dest=None, gitignore=None):
    """Interactive installer. Every question defaults to yes, except the idle guard's (no; never with --yes).
    Returns 0, or 1 if any step failed."""
    try:
        be = scheduler.backend()
    except ValueError as e:
        out("sessioncoach setup: %s" % e)
        return 1
    out("sessioncoach %s setup — nothing is sent anywhere; every change is reversible with `sessioncoach uninstall --all`" % __version__)
    if not be:
        out("  note: no scheduler found on this machine (no systemd user session, no crontab); skipping the scheduled ingest "
            "and review. Run `sessioncoach ingest` by hand or from your own scheduler.")
    cfg_now = config.load_alerts()  # one snapshot: what each piece compares its answer with
    rev_now = review.load_config(cfg_now)
    if scope is None:
        scope = _ask("Where do the status line and hook go? global | project | company", "global", yes, inp)
    if settings_path is None:
        try:
            settings_path = settings_for(scope, cwd)
        except ValueError as e:
            out("  %s" % e)
            return 1
    out("  settings file: %s" % settings_path)
    proj = os.path.abspath(cwd or os.getcwd())
    c = _SetupRun(yes=yes, scope=scope, interval=interval, time=time, kit_bin=kit_bin, agents_dir=agents_dir, load=load,
                  settings_path=settings_path, inp=inp, out=out, cwd=cwd, cut_dest=cut_dest, gitignore=gitignore,
                  be=be, what=scheduler.LABELS.get(be, ""), cfg_now=cfg_now, rev_now=rev_now, proj=proj,
                  sups=settings_mod.project_superiors(proj, paths.HOME) if scope == "project" else [], fig=setup_figures(cwd))
    _run_steps(c)
    out("")
    out("Done. Run `sessioncoach check` to verify, `sessioncoach ingest` to read your transcripts now, "
        "`sessioncoach dashboard` to look at them.")
    return 1 if c.failed else 0


# ---- check ----

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def retention_days(cwd=None):
    vals = []
    for _, p in known_settings(cwd):
        v = paths.load_json_object(p).get("cleanupPeriodDays")
        if isinstance(v, (int, float)) and v > 0:
            vals.append(int(v))
    return min(vals) if vals else DEFAULT_RETENTION_DAYS


def last_ingest():
    path = paths.db_path()
    if not os.path.exists(path):
        return None
    try:
        con = store.open_readonly(path)
        try:
            row = con.execute("SELECT MAX(ts) FROM ingest_runs").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    t = dt.datetime.fromisoformat(row[0])
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def command_problem(command):
    """Why a kit command in settings can't run (missing interpreter or script), or None."""
    try:
        parts = paths.split_command(command)
    except ValueError:
        return "unparseable command"
    if not parts or not (os.path.exists(parts[0]) or shutil.which(parts[0])):
        return "interpreter not found: %s" % (parts[0] if parts else "")
    exe = parts[0] if os.path.exists(parts[0]) else shutil.which(parts[0])
    if os.path.basename(exe).lower().startswith("python"):
        if paths.mac_stub(exe):
            return "interpreter %s is the macOS stub without Command Line Tools (it opens an install dialog)" % exe
        v = paths.python_version(exe)
        if v and v < (3, 9):
            return "interpreter %s is Python %d.%d; sessioncoach needs 3.9 or later" % (exe, v[0], v[1])
    console = parts[0].replace("\\", "/").rsplit("/", 1)[-1].lower() in ("sessioncoach", "sessioncoach.exe")
    if len(parts) > 1 and not console and not parts[1].startswith("-") and not os.path.exists(parts[1]):
        return "script not found: %s" % parts[1]  # python <script>; -m / -c and the console script name none
    return None


def _scope_items(label, path):
    items = []
    data = _read_json(path) if os.path.exists(path) else {}
    if data is None:
        return [(WARN, "settings " + label, "%s is not valid JSON" % path)]
    hook_cmds = hooks.our_commands(hooks.PROMPT, data)
    for name, cmd in (("hook", hook_cmds[-1] if hook_cmds else None), ("status line", limits.our_command(data))):
        if cmd:
            bad = command_problem(cmd)
            items.append((BAD, "%s [%s]" % (name, label), "%s — fix: sessioncoach setup (or uninstall and reinstall)" % bad) if bad
                         else (OK, "%s [%s]" % (name, label), path))
            old = paths.old_command_name(cmd)
            if old:
                items.append((WARN, "%s [%s]" % (name, label), "uses the old command %s — run: sessioncoach setup to update it" % old))
    return items


def _project_items(cwd=None):
    """Project-level findings: a double hook, and a CLAUDE.local.md with sessioncoach's block that git would commit."""
    S = settings_mod
    proj = os.path.abspath(cwd or os.getcwd())
    items = []
    psettings = settings_for("project", proj)
    if S.settings_have(psettings, "hook"):
        dup = S.hook_duplicated_in(S.project_superiors(proj, paths.HOME))
        if dup:
            items.append((WARN, "hook duplicated", "active in the project and in %s: it runs twice — "
                          "remove one (sessioncoach hook-uninstall in the project)" % dup))
    local = os.path.join(proj, "CLAUDE.local.md")
    try:
        with open(local, encoding="utf-8", errors="replace") as fh:
            has = S.has_cut_rule(fh.read())
    except OSError:
        has = False
    if has:
        st = S.git_ignore_state(proj)
        if st == "not_ignored":
            items.append((WARN, "CLAUDE.local.md", "has sessioncoach's cut rule but git does not ignore it — add `CLAUDE.local.md` "
                          "to .gitignore (the Settings panel or `sessioncoach setup --scope project --gitignore` does it)"))
        else:
            how = "ignored by git" if st == "ignored" else "not a git repo" if st == "no_repo" else "git unavailable"
            items.append((OK, "CLAUDE.local.md", "cut rule present (%s)" % how))
    return items


# what check says per backend; launchd talks about plists and launchctl
_LAUNCHD_TEXT = {"legacy": "installed under the old name daf-claude-kit (%(label)s) — run: sessioncoach setup to rename it",
                 "inactive": "plist exists but the agent is not loaded — run: sessioncoach setup",
                 "unknown": "launchctl unavailable; plist present: %(where)s",
                 "active": "loaded (%(where)s)",
                 "drift": "plist differs from alerts.json — run: sessioncoach %(job)s-install"}
_SCHED_TEXT = {"legacy": "installed under the old name daf-claude-kit — run: sessioncoach setup to rename it",
               "inactive": "%(be)s entry exists but is not active — run: sessioncoach setup",
               "unknown": "%(be)s unavailable to query; entry present: %(where)s",
               "active": "active in %(be)s (%(where)s)%(next)s",
               "drift": "%(be)s differs from alerts.json — run: sessioncoach %(job)s-install"}


def _sched_items(job, name, be, unit_dir, run=None, load_fn=None):
    """check items for a scheduled job on any backend: installed, loaded, next run, config drift."""
    if not be:
        return [(WARN, name, "no scheduler found (no systemd user session, no crontab); run `sessioncoach ingest` by hand")]
    st = scheduler.status(job, unit_dir=unit_dir, be=be, run=run, loaded_fn=load_fn)
    if not st["installed"]:
        return [(WARN, name, "not installed (sessioncoach setup)")]
    text = _LAUNCHD_TEXT if be == "launchd" else _SCHED_TEXT
    fill = {"be": scheduler.LABELS[be], "where": st["where"], "label": st["label"], "job": job.name,
            "next": "; next: " + st["next"] if st["next"] else ""}
    items = []
    if st["legacy"]:
        items.append((WARN, name, text["legacy"] % fill))
    if st["loaded"] is False:
        items.append((BAD, name, text["inactive"] % fill))
    elif st["loaded"] is None:
        items.append((WARN, name, text["unknown"] % fill))
    else:
        items.append((OK, name, text["active"] % fill))
    if st["unreadable"]:
        items.append((WARN, name + " config", "cannot read %s" % st["where"]))
    elif st["key"] != job.key():
        items.append((WARN, name + " config", text["drift"] % fill))
    return items


def check(cwd=None, agents_dir=None, now=None, load_fn=launchd_loaded, mac=None):
    """List of (status, name, detail). Read-only."""
    try:
        be, bad_be = "launchd" if mac else scheduler.backend(), None
    except ValueError as e:
        be, bad_be = None, str(e)
    now = now or dt.datetime.now(dt.timezone.utc)
    items = [(OK, "version", __version__)]
    if bad_be:
        items.append((BAD, "scheduler", bad_be + " — fix: unset it, or set a valid one"))
    alerts = limits.load_alerts()
    # ingest
    items += _sched_items(ingest_job(ingest_interval(alerts)), "ingest agent", be, agents_dir, load_fn=load_fn)
    last = last_ingest()
    ret = retention_days(cwd)
    if last is None:
        items.append((WARN, "last ingest", "no ingest yet — run: sessioncoach ingest"))
    else:
        age = (now - last).total_seconds() / 86400
        txt = "%s (%.1f days ago; Claude Code deletes transcripts after %d days)" % (last.astimezone().strftime("%Y-%m-%d %H:%M"), age, ret)
        items.append((BAD if age >= ret else WARN if age >= 1 else OK, "last ingest",
                      txt + (" — transcripts are being lost, run: sessioncoach ingest" if age >= ret
                             else " — run: sessioncoach ingest" if age >= 1 else "")))
    # review
    try:
        items += _sched_items(review.job(review.load_config(alerts)), "review agent", be, agents_dir, load_fn=load_fn)
    except (ValueError, KeyError) as e:
        items.append((WARN, "review agent", "invalid review config in alerts.json (%s)" % e))
    # settings: statusline + hook per scope
    found = False
    for label, path in known_settings(cwd):
        sub = _scope_items(label, path)
        found |= any(s == OK for s, _, _ in sub)
        items += sub
    for label, path in known_settings(cwd):
        data = (_read_json(path) or {}) if os.path.exists(path) else {}
        for spec in OTHER_HOOKS:
            cmds = hooks.our_commands(spec, data)
            if cmds:
                bad, name = command_problem(cmds[-1]), "%s [%s]" % (spec.check_label, label)
                items.append((BAD, name, "%s — fix: %s" % (bad, spec.check_fix)) if bad else (OK, name, path))
                if hooks.matcher_outdated(spec, data):
                    items.append((WARN, name, "installed with an older matcher (%s) — run: %s"
                                  % (spec.matcher or "every source", spec.check_fix)))
        st = handoff.state(handoff.command_path(path), resume_visible(path))
        if st == "current":
            items.append((OK, "handoff command [%s]" % label, handoff.command_path(path)))
        elif st == "stale":
            items.append((WARN, "handoff command [%s]" % label, "out of date (another version, or written before the resume "
                          "hook was added or removed) — run: sessioncoach handoff-install"))
    if not found:
        items.append((WARN, "status line / hook", "not installed in any scope — run: sessioncoach setup"))
    items += _project_items(cwd)
    if idleguard.enabled(alerts):
        problems = idle_guard_problems(cwd)
        items += [(WARN, "idle guard", "on, but " + p) for p in problems] or [
            (OK, "idle guard", "on: the first prompt after a pause longer than the cache is held back (sessioncoach idle-guard-off)")]
    else:
        items.append((OK, "idle guard", "off (opt-in: sessioncoach idle-guard-on)"))
    value, report = idle_compact_threshold(cwd)
    items += [(WARN if bad else OK, "idle summary threshold", d + " — whether it pays off: sessioncoach idle-compact")
              for bad, d in report]
    if value is None and idleguard.enabled(alerts):
        items.append((OK, "idle summary threshold", "not set: Claude Code's default, %dk — whether %dk pays off for you: "
                      "sessioncoach idle-compact" % (idlecompact.DEFAULT_MIN_TOKENS // 1000, idlecompact.FLOOR_MIN_TOKENS // 1000)))
    # permissions
    d = paths.data_dir()
    if os.path.isdir(d) and paths.is_windows():
        # no POSIX modes on Windows: the profile folder is private to the user by its ACLs, so check the data dir lives there
        prof = os.path.realpath(paths.HOME)
        inside = os.path.commonpath([os.path.normcase(os.path.realpath(d)), os.path.normcase(prof)]) == os.path.normcase(prof)
        items.append((OK, "permissions", "%s is inside your user profile (private by Windows ACLs)" % d) if inside
                     else (WARN, "permissions", "%s is outside your user profile (%s); other users may read it — "
                           "set SESSIONCOACH_DATA_DIR inside it" % (d, prof)))
    elif os.path.isdir(d):
        loose = []
        if os.stat(d).st_mode & 0o077:
            loose.append(d + " (want 700)")
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.stat(p).st_mode & 0o077:
                loose.append(p + " (want 600)")
        items.append((WARN, "permissions", "too open: " + ", ".join(loose) + " — fix: chmod 700 dir, 600 files") if loose
                     else (OK, "permissions", "%s is private" % d))
    else:
        items.append((WARN, "data dir", "%s does not exist yet — run: sessioncoach ingest" % d))
    return items


def run_claude_doctor(timeout=30):
    """(ok, text): `claude doctor` is non-interactive and prints free text, so it is shown, not parsed."""
    exe = shutil.which("claude")
    if not exe:
        return None, "claude not found in PATH"
    try:
        r = subprocess.run([exe, "doctor"], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "could not run claude doctor (%s)" % e
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def format_check(items):
    return "\n".join("%s %-26s %s" % (MARK[s], n, d) for s, n, d in items)


# ---- uninstall ----

def _remove_cut_rules(proj):
    """The «when to cut» block setup wrote in this project's CLAUDE.local.md or CLAUDE.md; the rest of the file stays.
    The CLAUDE.local.md line setup may have added to .gitignore stays too (harmless, and maybe yours)."""
    S, out = settings_mod, []
    for name in ("CLAUDE.local.md", "CLAUDE.md"):
        target = os.path.join(proj, name)
        try:
            text = S.read_text(target)
        except S.SettingsError:
            out.append("cut rule: %s is not UTF-8; remove the block by hand" % target)
            continue
        if text and S.has_cut_rule(text):
            S.write_atomic(target, S.remove_cut_rule(text).encode("utf-8"))
            out.append("cut rule: removed from %s" % target)
    return out


def uninstall_all(cwd=None, agents_dir=None, load=True, purge=False, confirm=None, mac=None):
    """Undo everything setup did. Idempotent. Returns the list of lines describing what was done."""
    try:
        be = "launchd" if mac else scheduler.backend()
    except ValueError:  # a mistyped SESSIONCOACH_SCHEDULER: still remove the jobs of every candidate scheduler
        be = "unknown"
    out = []
    for label, path in known_settings(cwd):
        if not os.path.exists(path):
            continue
        out += _remove_hooks(label, path, [hooks.PROMPT])
        if limits.installed(path):
            out.append("status line [%s]: %s" % (label, limits.uninstall(path)))
    for label, path in known_settings(cwd):
        for spec in OTHER_HOOKS:
            out += _remove_hooks(label, path, [spec])
            if spec is hooks.SESSION_START and handoff.installed(path):  # /handoff right after the resume hook, as before
                out.append("handoff command [%s]: %s" % (label, handoff.uninstall(path)))
    nd = limits.load_alerts().get("nudge")
    if isinstance(nd, dict) and "block" in nd:
        update_alerts(lambda c: c["nudge"].pop("block", None))
        out.append("idle guard: off")
    for mod in (idleguard, idlecompact, keepctx, resume):
        if mod.remove():
            out.append("removed %s" % mod.path())
    out += _remove_cut_rules(os.path.abspath(cwd or os.getcwd()))
    if be or agents_dir:
        out.append("ingest agent: " + ingest_uninstall(agents_dir, load and bool(be)))
        out.append("review agent: " + review.uninstall(agents_dir, load and bool(be)))
    if purge:
        d = os.path.realpath(paths.data_dir())
        if d in (os.path.realpath(paths.HOME), os.sep) or not os.path.isdir(d):
            out.append("purge: nothing to remove at %s" % d if not os.path.isdir(d) else "purge: refusing to remove %s" % d)
        elif confirm is None or confirm(d):
            shutil.rmtree(d)
            out.append("purge: removed %s" % d)
        else:
            out.append("purge: cancelled; data kept in %s" % d)
    else:
        out.append("data kept in %s (use --purge to delete it)" % paths.data_dir())
    return out
