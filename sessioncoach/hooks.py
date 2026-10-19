"""sessioncoach's Claude Code hooks: what each one writes in settings.json, and how it is installed and removed.

Every hook is its own entry under hooks.<event> (with its matcher, when it has one; an entry of ours with a matcher an
older version wrote, `old_matchers`, gets the current one) running
`<sessioncoach> hook <arg>` with a 5 s timeout; other entries are left untouched and settings.json is copied to
<settings>.<backup suffix> before each change. Commands written under any name the tool ever had are recognised
(paths.mentions_us), and installing again rewrites one in a form no longer written (bin/kit, -m kit, -m sessioncoach)
in place: same entry, same timeout. Odd shapes in settings.json count as no hook of ours; installing into a container that is not
a dict or a list raises ValueError instead of replacing what is there. HOOKS is the registry the installer, the
checks and `sessioncoach hook` walk; each module (nudge, resume, idlecompact, keepctx) keeps its own logic.
"""
import copy
import importlib
import json
import os

from . import paths


class HookSpec:
    """One hook: `arg` of `sessioncoach hook`, its event and matcher, the backup suffix, the module that runs it, and
    the names the installer shows (`label`; `check_label` in `check`, with `check_fix`; `guard_need`, when the idle
    guard needs it, says why)."""

    def __init__(self, arg, event, matcher, backup_suffix, module, label, check_label=None, check_fix=None,
                 guard_need=None, idle_guard=False, timeout=5, old_matchers=()):
        self.arg, self.event, self.matcher, self.backup_suffix = arg, event, matcher, backup_suffix
        self.module_name, self.label, self.check_label = module, label, check_label or label
        self.check_fix, self.guard_need, self.idle_guard, self.timeout = check_fix, guard_need, idle_guard, timeout
        self.old_matchers = old_matchers  # matchers earlier versions wrote; one the user chose is left alone

    def __repr__(self):
        return "HookSpec(%r)" % self.arg

    def module(self):
        return importlib.import_module("%s.%s" % (__package__, self.module_name))

    def command(self, kit_bin=None):
        return paths.kit_command_str("hook", self.arg, kit_bin=kit_bin)

    def is_ours(self, command):
        """Any format ever written (bin/sessioncoach, bin/kit, `python -m sessioncoach` or `python -m kit`)."""
        return (" hook %s" % self.arg) in (command or "") and paths.mentions_us(command)


PROMPT = HookSpec("prompt", "UserPromptSubmit", None, ".bak-hook", "nudge", "hook",
                  guard_need="nothing is held back without the resume-warning hook — run: sessioncoach hook-install")
# no matcher: the VS Code extension and the desktop app start a new session ("startup") on /clear, the CLI sends
# "clear"; resume.evaluate tells them apart (it was "clear" only, so no handoff ever reached those apps)
SESSION_START = HookSpec("session-start", "SessionStart", None, ".bak-resume", "resume", "resume hook",
                         check_fix="sessioncoach setup", old_matchers=("clear",),
                         guard_need="/clear gives no automatic handoff without the resume hook — run: sessioncoach "
                                    "resume-hook-install")
PRE_COMPACT = HookSpec("pre-compact", "PreCompact", "auto", ".bak-precompact", "idlecompact", "pre-compact hook",
                       check_fix="sessioncoach idle-guard-on", idle_guard=True,
                       guard_need="without the pre-compact hook it can hold back a prompt Claude Code would answer from "
                                  "its idle summary — run: sessioncoach idle-guard-on")
KEEP_CONTEXT = HookSpec("keep-context", "UserPromptExpansion", "keep-context", ".bak-keepctx", "keepctx", "keep-context",
                        check_label="keep-context hook", check_fix="sessioncoach idle-guard-on", idle_guard=True)
HOOKS = (PROMPT, SESSION_START, PRE_COMPACT, KEEP_CONTEXT)


def by_arg(arg):
    return next(s for s in HOOKS if s.arg == arg)


def _entries(settings, event):
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    return entries if isinstance(entries, list) else []


def _inner(entry):
    inner = entry.get("hooks") if isinstance(entry, dict) else None
    return [h for h in inner if isinstance(h, dict)] if isinstance(inner, list) else []


def _ours_in(spec, entry):
    return [h["command"] for h in _inner(entry) if spec.is_ours(h.get("command"))]


def our_commands(spec, settings):
    """Commands of sessioncoach's entries for `spec` in `settings`, in any format; odd shapes count as none."""
    return [c for e in _entries(settings, spec.event) for c in _ours_in(spec, e)]


def matcher_outdated(spec, settings):
    """True when an entry of ours for `spec` still has a matcher an earlier version wrote."""
    return any(e.get("matcher") in spec.old_matchers for e in _entries(settings, spec.event) if _ours_in(spec, e))


def _fix_matchers(spec, new):
    """Give our entries of `spec` in `new` that have an old matcher the current one; an entry shared with other hooks keeps its matcher and
    loses only ours, which goes into an entry of its own. -> True when something changed."""
    entries = _entries(new, spec.event)
    changed = False
    for e in list(entries):
        mine = [h for h in _inner(e) if spec.is_ours(h.get("command"))]
        if not mine or e.get("matcher") not in spec.old_matchers:
            continue
        changed = True
        if len(mine) == len(e["hooks"]):
            if spec.matcher is None:
                e.pop("matcher", None)
            else:
                e["matcher"] = spec.matcher
            continue
        e["hooks"] = [h for h in e["hooks"] if h not in mine]
        entry = {"matcher": spec.matcher} if spec.matcher is not None else {}
        entry["hooks"] = mine
        entries.append(entry)
    return changed


def installed_in(spec, settings):
    """True when `settings` (parsed) has the hook, in any command format."""
    return bool(our_commands(spec, settings))


def load(settings_path, create=False):
    if create and not os.path.exists(settings_path):
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        return {}
    with open(settings_path, encoding="utf-8") as fh:
        return json.load(fh)


def dump(settings):
    return (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def installed(spec, settings_path=None):
    try:
        return installed_in(spec, load(settings_path or paths.user_settings()))
    except (OSError, ValueError):
        return False


def _container(parent, key, kind, where):
    value = parent.setdefault(key, kind())
    if not isinstance(value, kind):
        raise ValueError("%s in settings is a %s, not a %s: fix it by hand" % (where, type(value).__name__, kind.__name__))
    return value


def plan_install(spec, settings, kit_bin=None):
    """Pure: add the hook as its own entry; when it is there with the old command, rewrite that command in place.
    -> new settings dict, or None when nothing changes. `settings` is not modified."""
    cmd = spec.command(kit_bin)
    new = copy.deepcopy(settings)
    if our_commands(spec, settings):
        changed = _fix_matchers(spec, new)
        for e in _entries(new, spec.event):
            for h in _inner(e):
                if spec.is_ours(h.get("command")) and paths.old_command_name(h["command"]) and h["command"] != cmd:
                    h["command"], changed = cmd, True
        return new if changed else None
    if not isinstance(new, dict):
        raise ValueError("settings is a %s, not an object: fix it by hand" % type(new).__name__)
    entries = _container(_container(new, "hooks", dict, "hooks"), spec.event, list, "hooks." + spec.event)
    entry = {"matcher": spec.matcher} if spec.matcher is not None else {}
    entry["hooks"] = [{"type": "command", "command": cmd, "timeout": spec.timeout}]
    entries.append(entry)
    return new


def plan_uninstall(spec, settings):
    """Pure: remove only sessioncoach's hooks. An entry that also holds someone else's hooks keeps them (and its
    matcher); one left empty goes, and so does the event when no entry is left. -> new settings dict, or None."""
    if not our_commands(spec, settings):
        return None
    new = copy.deepcopy(settings)
    kept = []
    for e in new["hooks"][spec.event]:
        if _ours_in(spec, e):
            e["hooks"] = [h for h in e["hooks"] if not (isinstance(h, dict) and spec.is_ours(h.get("command")))]
            if not e["hooks"]:
                continue
        kept.append(e)
    if kept:
        new["hooks"][spec.event] = kept
    else:
        new["hooks"].pop(spec.event)
    return new


def install(spec, settings_path=None, kit_bin=None):
    """Add the hook to `settings_path` (default: the user's), or bring its command up to date. -> one line."""
    settings_path = settings_path or paths.user_settings()
    cur = load(settings_path, create=True)
    new = plan_install(spec, cur, kit_bin)
    if new is None:
        return "already installed"
    backup = paths.backup_file(settings_path, spec.backup_suffix)
    paths.write_atomic(settings_path, dump(new), paths.SETTINGS_MODE, backup=False)  # the copy above is the backup
    if installed_in(spec, cur):
        return "updated to the current command (settings backup %s)" % backup
    return "installed (settings backup %s)" % backup if backup else "installed (new file %s)" % settings_path


def uninstall(spec, settings_path=None):
    """Remove only sessioncoach's entries for `spec`. -> 'removed' or 'not installed'."""
    settings_path = settings_path or paths.user_settings()
    new = plan_uninstall(spec, load(settings_path))
    if new is None:
        return "not installed"
    paths.backup_file(settings_path, spec.backup_suffix)
    paths.write_atomic(settings_path, dump(new), paths.SETTINGS_MODE, backup=False)
    return "removed"
