"""/keep-context: keep the whole context of this session when you step away (`sessioncoach hook keep-context`).

Claude Code summarises a big paused session about 54 min after its last turn (its idle compaction, see idlecompact.py):
the first prompt after the return is cheap, but the detail of the conversation is gone. Typing /keep-context before
leaving marks the session; the PreCompact hook then blocks that idle summary for it (only that one: an automatic
compaction because the context is full is never blocked). Coming back costs one re-read of the context, knowingly.
`/keep-context off` undoes it.

Installed with the idle guard: a command file, commands/keep-context.md (so Claude Code knows the command), and a
UserPromptExpansion hook (matcher keep-context) that does the marking and blocks the expansion, so the model is never
called and the command costs nothing. The marks live in the data dir (session id and time only, mode 600, at most
MAX_ENTRIES younger than MAX_AGE_S). The command file carries a marker: a keep-context.md of your own is left alone.
"""
import contextlib
import functools
import json
import os
import sys
import time

from . import config, hooks, paths, statefile

NAME = "keep-context.md"
MARK = "<!-- sessioncoach:keep-context -->"
HOOK_EVENT = hooks.KEEP_CONTEXT.event
MATCHER = hooks.KEEP_CONTEXT.matcher
FILE = "keep_context.json"
MAX_ENTRIES = 50
MAX_AGE_S = 7 * 24 * 3600

# shown only when the hook is missing: Claude Code then expands the file and the model answers it
TEMPLATE = """---
description: Keep this session's whole context when you step away (no idle summary); "off" undoes it
argument-hint: [off]
---
%(mark)s
<!-- Written by `sessioncoach idle-guard-on`; `sessioncoach idle-guard-off` removes it. -->
sessioncoach's hook for this command is not installed, so nothing was changed. Reply only, in the language of this
conversation: "/keep-context needs sessioncoach's hook: run `sessioncoach idle-guard-on`."
"""

MSG = {
    ("es", True): "sessioncoach · /keep-context: esta sesión conserva todo su contexto. Si la dejas parada, Claude Code no "
                  "la resumirá; al volver, el primer mensaje releerá el contexto entero una vez. /keep-context off lo deshace.",
    ("es", False): "sessioncoach · /keep-context off: si dejas esta sesión parada, Claude Code podrá resumirla otra vez "
                   "(al volver sale barato, con menos detalle).",
    ("en", True): "sessioncoach · /keep-context: this session keeps its whole context. If you leave it idle, Claude Code "
                  "will not summarise it; on return the first prompt re-reads the whole context once. /keep-context off undoes it.",
    ("en", False): "sessioncoach · /keep-context off: if you leave this session idle, Claude Code may summarise it again "
                   "(a cheap return, with less detail).",
}


STATE = statefile.JsonStateFile(FILE, MAX_ENTRIES, MAX_AGE_S, valid=lambda e: isinstance(e.get("session_id"), str)
                                and isinstance(e.get("at"), (int, float)))
path, load, _save, remove = STATE.path, STATE.load, STATE.save, STATE.remove


def keep(session_id, on=True, now=None):
    now = now or time.time()
    STATE.update(lambda es: [e for e in es if e["session_id"] != session_id]
                 + ([{"session_id": session_id, "at": now}] if on else []), now)


def kept(session_id, now=None):
    if not session_id or not os.path.exists(path()):
        return False
    now = now or time.time()
    return any(e["session_id"] == session_id and now - e["at"] <= MAX_AGE_S for e in load())


def evaluate(data):
    """The JSON to print for one UserPromptExpansion input, or None when it is not /keep-context."""
    from .nudge import message_lang
    sid = data.get("session_id")
    if data.get("command_name") != MATCHER or not sid:
        return None
    on = str(data.get("command_args") or "").strip().lower() not in ("off", "no", "0")
    keep(sid, on)
    return {"decision": "block", "reason": MSG[(message_lang(data, config.load_alerts()), on)]}


def hook_main():
    with contextlib.suppress(Exception):  # a failure lets the command file answer instead
        paths.utf8_stdio()
        out = evaluate(json.loads(sys.stdin.read() or "{}"))
        if out:
            print(json.dumps(out, ensure_ascii=False))
            sys.stdout.flush()


# ---- install: the command file next to the settings file, and its own UserPromptExpansion entry ----

def render():
    return TEMPLATE % {"mark": MARK}


def command_path(settings_path=None):
    return os.path.join(os.path.dirname(settings_path or paths.user_settings()), "commands", NAME)


def _read(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, ValueError):
        return None


SPEC = hooks.KEEP_CONTEXT
_command = SPEC.command
is_ours = SPEC.is_ours
_load, _dump = hooks.load, hooks.dump
our_commands = functools.partial(hooks.our_commands, SPEC)
installed = functools.partial(hooks.installed, SPEC)
plan_install = functools.partial(hooks.plan_install, SPEC)
plan_uninstall = functools.partial(hooks.plan_uninstall, SPEC)


def install(settings_path=None, kit_bin=None):
    """The hook entry in `settings_path` and the command file next to it. -> one line."""
    settings_path = settings_path or paths.user_settings()
    hook = "hook " + hooks.install(SPEC, settings_path, kit_bin)
    cp = command_path(settings_path)
    text = _read(cp)
    if text is not None and MARK not in text:
        return "%s; command skipped — %s is your own" % (hook, cp)
    if text != render():
        paths.write_atomic(cp, render().encode("utf-8"), backup=False)
    return "%s; /keep-context (%s)" % (hook, cp)


def uninstall(settings_path=None):
    settings_path = settings_path or paths.user_settings()
    out = []
    if os.path.exists(settings_path) and hooks.uninstall(SPEC, settings_path) == "removed":
        out.append("hook removed")
    cp = command_path(settings_path)
    text = _read(cp)
    if text is not None and MARK in text:
        os.remove(cp)
        out.append("removed %s" % cp)
    return "; ".join(out) or "not installed"


def present(settings_path):
    """The hook or the command file is there (what uninstall would remove)."""
    return installed(settings_path) or os.path.exists(command_path(settings_path))
