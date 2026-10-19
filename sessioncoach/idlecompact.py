"""Whether Claude Code already prepared a summary of a paused session (its idle compaction) (`sessioncoach hook pre-compact`).

Claude Code (2.1.299 and later) summarises a big session in memory about 54 min after its last turn while the app is
open and the machine awake, and applies that summary on the first prompt after the return: that prompt does not
re-read the whole context. Nothing reaches the transcript before that prompt, and the UserPromptSubmit hook runs
before the swap, so the transcript cannot tell. The summary is announced to PreCompact hooks with trigger "auto".
This PreCompact hook (matcher "auto", installed with the idle guard) records, when the compaction comes long after the
last turn (IDLE_S), the session id, the time, that last turn's time, the transcript path and its size, in the data
dir (mode 600; at most MAX_ENTRIES younger than MAX_AGE_S; no text). The prompt hook (nudge.py) then neither warns
nor holds back while the last turn is still that one. It always exits 0 and prints nothing (a blocking PreCompact
hook cancels the compaction), except for the idle summary of a session marked with /keep-context (keepctx.py), which
it blocks on purpose; an automatic compaction for a full context is never blocked. Known limit: if the prepared
summary later fails, that return was not warned.
"""
import contextlib
import datetime as dt
import functools
import json
import os
import sys
import time

from . import hooks, paths, statefile

HOOK_EVENT = hooks.PRE_COMPACT.event
MATCHER = hooks.PRE_COMPACT.matcher  # covers automatic and idle compactions; /compact (manual) has the person at the keyboard
FILE = "idle_compact.json"
IDLE_S = 45 * 60  # an automatic compaction this long after the last turn is the idle one
MAX_ENTRIES = 50
MAX_AGE_S = 24 * 3600


STATE = statefile.JsonStateFile(FILE, MAX_ENTRIES, MAX_AGE_S, valid=lambda e: isinstance(e.get("session_id"), str)
                                and isinstance(e.get("last_ts"), str) and isinstance(e.get("at"), (int, float)))
path, load, _save, remove = STATE.path, STATE.load, STATE.save, STATE.remove


def _ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _idle_last_ts(data, now):
    """The last turn's time when this is an automatic compaction IDLE_S or more after it (the idle one), else None."""
    from .nudge import last_turn_info
    tp, sid = data.get("transcript_path"), data.get("session_id")
    if data.get("trigger") != MATCHER or not sid or not tp or not os.path.exists(tp):
        return None
    last_ts = last_turn_info(tp)[0]
    return last_ts if last_ts and now - _ts(last_ts) >= IDLE_S else None


def record(data, now=None):
    """Keep an automatic compaction that came IDLE_S or more after the session's last turn. -> the entry or None."""
    now = now or time.time()
    last_ts = _idle_last_ts(data, now)
    if not last_ts:
        return None
    sid, tp = data["session_id"], data["transcript_path"]
    entry = {"session_id": sid, "at": now, "last_ts": last_ts, "transcript_path": tp, "transcript_size": os.path.getsize(tp)}
    STATE.update(lambda es: [e for e in es if e["session_id"] != sid] + [entry], now)
    return entry


def pending(session_id, last_ts, now=None):
    """True when an idle summary was prepared for this session after its turn at `last_ts` and not applied yet: once
    it is applied, or a new turn comes, the last turn the prompt hook sees is another one. Read-only."""
    if not session_id or not last_ts or not os.path.exists(path()):
        return False
    now = now or time.time()
    return any(e["session_id"] == session_id and e["last_ts"] == last_ts and now - e["at"] <= MAX_AGE_S for e in load())


def evaluate(data, now=None):
    """The JSON to print for one PreCompact input: a block only for the idle summary of a session marked with
    /keep-context (keepctx.py); otherwise None, after recording an idle one."""
    from . import keepctx
    now = now or time.time()
    if keepctx.kept(data.get("session_id"), now) and _idle_last_ts(data, now):
        return {"decision": "block", "reason": "sessioncoach: /keep-context — this session keeps its whole context"}
    record(data, now)
    return None


def hook_main():
    with contextlib.suppress(Exception):  # never in the way of any other compaction: no output, exit 0
        paths.utf8_stdio()
        out = evaluate(json.loads(sys.stdin.read() or "{}"))
        if out:
            print(json.dumps(out))
            sys.stdout.flush()


# ---- CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS: from what context size Claude Code summarises a paused session ----

MIN_TOKENS_ENV = "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS"
DEFAULT_MIN_TOKENS = 200000  # Claude Code's default (docs: env-vars)
FLOOR_MIN_TOKENS = 100000    # lower values are raised to this one, the lowest threshold
FIRE_S = 54 * 60             # the summary is prepared at 90 % of the 1 h cache lifetime
OFF_SETTINGS = ("idleCompaction", "autoCompactEnabled", "precomputeCompactionEnabled")  # false turns the summary off
OFF_ENV = ("DISABLE_COMPACT", "DISABLE_AUTO_COMPACT")


def parse_min_tokens(raw):
    """-> (threshold Claude Code applies, or None when it ignores `raw`; why it was ignored or raised, or None).
    The docs: a positive whole number, anything else is ignored; below FLOOR_MIN_TOKENS it is raised to it."""
    s = str(raw).strip()
    if not (s.isascii() and s.isdigit()) or int(s) == 0:
        return None, "%r is not a positive whole number, so Claude Code ignores it and uses %dk" % (raw, DEFAULT_MIN_TOKENS // 1000)
    n = int(s)
    if n < FLOOR_MIN_TOKENS:
        k = FLOOR_MIN_TOKENS // 1000
        return FLOOR_MIN_TOKENS, "%d is below %dk, so Claude Code raises it to %dk" % (n, k, k)
    return n, None


def _env_of(settings):
    env = settings.get("env") if isinstance(settings, dict) else None
    return env if isinstance(env, dict) else {}


def min_tokens_report(settings_files, environ):
    """Is the threshold set right? `settings_files`: [(scope label, parsed settings or None)]; `environ`: this shell's.
    -> (value in force or None for the default, [(is_warning, detail)]). Read-only; says nothing when it is not set."""
    sources = [("%s settings" % label, _env_of(s)[MIN_TOKENS_ENV]) for label, s in settings_files if MIN_TOKENS_ENV in _env_of(s)]
    if MIN_TOKENS_ENV in environ:
        sources.append(("this shell's environment", environ[MIN_TOKENS_ENV]))
    if not sources:
        return None, []
    out, values = [], {}
    for where, raw in sources:
        n, why = parse_min_tokens(raw)
        if why:
            out.append((True, "%s: %s" % (where, why)))
        values[where] = n or DEFAULT_MIN_TOKENS
    if len(set(values.values())) > 1:
        out.append((True, "different values (%s): keep one, in settings.json → env" % ", ".join(
            "%s %dk" % (w, v // 1000) for w, v in values.items())))
    off = ["%s is false in %s settings" % (k, label) for label, s in settings_files for k in OFF_SETTINGS
           if isinstance(s, dict) and s.get(k) is False]
    off += ["%s is set in %s" % (k, w) for k in OFF_ENV
            for w, env in [("%s settings" % label, _env_of(s)) for label, s in settings_files] + [("this shell's environment", environ)]
            if str(env.get(k) or "").strip() not in ("", "0", "false")]
    if off:
        out.append((True, "set, but Claude Code does not summarise paused sessions: " + "; ".join(off)))
    value = next(iter(values.values()))
    if not out:
        only_shell = all(w == "this shell's environment" for w in values)
        out.append((False, "%dk (%s): Claude Code summarises a paused session from that much context%s" % (
            value // 1000, ", ".join(values),
            "; the VS Code extension does not read this shell, put it in settings.json → env" if only_shell else "")))
    return value, out


# ---- install: its own PreCompact entry, matcher auto (hooks.py) ----

SPEC = hooks.PRE_COMPACT
_command = SPEC.command
is_ours = SPEC.is_ours
_load, _dump = hooks.load, hooks.dump
our_commands = functools.partial(hooks.our_commands, SPEC)
installed = functools.partial(hooks.installed, SPEC)
plan_install = functools.partial(hooks.plan_install, SPEC)
plan_uninstall = functools.partial(hooks.plan_uninstall, SPEC)
install = functools.partial(hooks.install, SPEC)
uninstall = functools.partial(hooks.uninstall, SPEC)
