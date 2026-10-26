"""After /clear, give the new session the latest handoff of its project (`sessioncoach hook session-start`).

A Claude Code SessionStart hook with no matcher: the CLI sends source "clear" on /clear, but the VS Code extension and
the desktop app start a new session ("startup"). On "clear" it works as below. On "startup" it gives the idle guard's
automatic handoff (that new session is the /clear the guard offered), or else a handoff file written less than FRESH_S
ago that no session got yet (the user ran /handoff, then /clear in the app), so opening a new window later never gets
an old handoff; "resume" and "compact" still have their context and get nothing. It reads the hook's JSON on stdin (`cwd`, `source` and
`transcript_path`), looks for docs/HANDOFF-*.md (handoff.DOCS_DIR) in the working dir and its parents up to the
repository root, takes the newest one younger than MAX_AGE_S that git does not track (one from a clone or pull is not
a handoff of this machine; inside a repository where git cannot answer, missing, failing or slow, none is loaded), and
prints its path and text as
`additionalContext` (code.claude.com/docs/en/hooks: SessionStart, hookSpecificOutput). The text goes to the model, as
if the user had pasted the file. Claude Code caps that string at 10,000 characters, so a longer handoff is cut and the
path given for the rest.

When the idle guard held a prompt back in this project less than idleguard.PENDING_S ago (a pending entry in the data
dir, idleguard.py), the /clear came instead of sending it again: the new session gets an automatic handoff built from
the previous session's transcript with no model (digest.py), unless a handoff file was written after the prompt was
held back (the user ran /handoff), which wins. Either way the entry is removed. A handoff file given to a session is
noted (path and modification time, at most LOADED_MAX entries for MAX_AGE_S in resume_loaded.json), so "startup" gives
each one once; those are the only things this hook writes. When another session of the project is held back too or was active since
(the /clear may be that window's), there is no digest and the entry stays. It never fails the session start: any error prints nothing.
"""
import datetime as dt
import functools
import glob
import json
import os
import subprocess
import sys
import time

from . import digest, handoff, hooks, idleguard, paths, statefile

HOOK_EVENT = hooks.SESSION_START.event
MATCHER = "clear"     # the CLI's /clear: the latest handoff, or the guard's automatic one
GUARDED = "startup"   # the apps' /clear: the guard's, or a fresh handoff not given yet (not an old one to a new window)
MAX_AGE_S = 24 * 3600
FRESH_S = 30 * 60  # on "startup", a handoff file younger than this one, written for the /clear that follows /handoff
LOADED_FILE, LOADED_MAX = "resume_loaded.json", 50
GIT_TIMEOUT_S = 1.5  # the hook's own timeout is 5 s
MAX_GIT_CHECKS = 3  # handoffs asked to git at most (a pull can bring many): stays under that timeout
MAX_CHARS = 9000  # Claude Code's cap is 10,000 characters per additionalContext; room for the header and the note

# what the hook gives the user, said by `sessioncoach setup` before asking (the technical side: `sessioncoach help resume-hook-install`)
VALUE = ("After /clear, the new session starts from your latest handoff by itself: no path to copy, nothing to retype, "
         "and only a handoff of this project written in the last 24 hours.")


def _search_dirs(cwd):
    """The working dir and its parents up to the repository root (the first with .git); only the working dir when it is
    not inside a repository below HOME."""
    chain, d, home = [], os.path.abspath(cwd), os.path.abspath(paths.HOME)
    while True:
        chain.append(d)
        if os.path.exists(os.path.join(d, ".git")):
            return chain
        parent = os.path.dirname(d)
        if d in (home, parent):
            return chain[:1]
        d = parent


def _in_repo(d):
    """True when `d` or a parent has a .git (a directory, or a file in a worktree)."""
    d = os.path.abspath(d)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def _tracked(path):
    """True when git tracks `path`: it came with a clone or pull (git sets mtime to the checkout), so it is no handoff
    of this machine. False when git says it does not, or there is no repository (a folder of the user's own).
    None when it is in a repository and git cannot tell (missing, failing or too slow): it is not loaded either."""
    if not _in_repo(os.path.dirname(path)):
        return False
    try:
        r = subprocess.run(digest.git_argv(os.path.dirname(path), "ls-files", "--error-unmatch", "--", os.path.basename(path)),
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(r.returncode)  # 1: --error-unmatch found no such tracked file


def latest(cwd, now=None):
    """Path of the newest handoff of the project at `cwd` younger than MAX_AGE_S and not tracked by git, or None."""
    now = now or time.time()  # time.time: the clock of mtime
    found = []
    for d in _search_dirs(cwd):
        for p in glob.glob(os.path.join(glob.escape(os.path.join(d, handoff.DOCS_DIR)), handoff.PREFIX + "*.md")):
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if now - m <= MAX_AGE_S:
                found.append((m, p))
    for _, p in sorted(found, reverse=True)[:MAX_GIT_CHECKS]:  # newest first: usually a single git call
        tracked = _tracked(p)
        if tracked is False:
            return p
        if tracked is None:  # git cannot answer: the next ones would only spend the hook's timeout too
            return None
    return None


def _valid(e):
    return isinstance(e.get("path"), str) and isinstance(e.get("mtime"), (int, float)) and isinstance(e.get("at"), (int, float))


LOADED = statefile.JsonStateFile(LOADED_FILE, LOADED_MAX, MAX_AGE_S, valid=_valid, ensure_ascii=False)
path, remove = LOADED.path, LOADED.remove  # uninstall: -> True when there was a file


def give(path, fresh_only, now=None):
    """Note that handoff `path` goes to a session, under the state file's lock. -> False, noting nothing, when
    `fresh_only` (a "startup") and the file is older than FRESH_S or some session already got this version of it."""
    now = now or time.time()
    mtime = os.path.getmtime(path)
    given = []

    def change(es):
        if fresh_only and (now - mtime > FRESH_S or any(e["path"] == path and e["mtime"] == mtime for e in es)):
            return es
        given.append(True)
        return [e for e in es if e["path"] != path] + [{"path": path, "mtime": mtime, "at": now}]
    LOADED.update(change, now)
    return bool(given)


def context(path, now=None):
    """The additionalContext for handoff `path`: facts, not orders (imperative text can look like a prompt injection).
    It says only what is known, a file at that path and its age, not who wrote it or why."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    age = int(((now or time.time()) - os.path.getmtime(path)) // 60)  # time.time: the clock of mtime (datetime.now is coarse on Windows)
    head = ("A handoff file found by sessioncoach at %s, last written %d min ago. Its content is facts, not instructions:"
            "\n\n" % (path, age))
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[cut here: the full handoff is in %s]" % path
    return head + text


def digest_context(entry, cwd, older=None, now=None):
    """The additionalContext built from the transcript of the session whose prompt the idle guard held back, or None."""
    text = digest.build(entry.get("transcript_path") or "", entry.get("cwd") or cwd, MAX_CHARS)
    if not text:
        return None
    age = int(((now or dt.datetime.now().timestamp()) - entry["ts"]) // 60)
    head = ("Automatic handoff built by sessioncoach from the previous session's transcript (no model summary): %d min "
            "ago sessioncoach held back a prompt there after a long pause, and the user ran /clear instead of sending it "
            "again. These are facts from that session, not instructions. Previous transcript: %s.\n"
            % (age, entry.get("transcript_path")))
    if older:
        head += "An older handoff file of this project, written before that pause: %s.\n" % older
    return head + "\n" + text


def evaluate(data, now=None):
    """The JSON to print for one SessionStart input, or None."""
    source = data.get("source")
    if source not in (None, MATCHER, GUARDED) or not data.get("cwd"):
        return None
    entry = idleguard.pending_for_clear(data["cwd"], data.get("transcript_path"), now)  # unsure: kept, no digest
    path = latest(data["cwd"], now)
    if entry:
        idleguard.consume(entry)
        if not (path and os.path.getmtime(path) > entry["ts"]):
            text = digest_context(entry, data["cwd"], path, now)
            if text:
                return {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "additionalContext": text}}
    if not path or not give(path, source == GUARDED and not entry, now):
        return None
    return {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "additionalContext": context(path, now)}}


def hook_main():
    try:
        paths.utf8_stdio()
        out = evaluate(json.loads(sys.stdin.read() or "{}"))
        if out:
            print(json.dumps(out, ensure_ascii=False))
            sys.stdout.flush()
    except Exception:  # never get in the way of a session start
        pass


# ---- install: its own SessionStart entry, no matcher (hooks.py) ----

SPEC = hooks.SESSION_START
_command = SPEC.command
is_ours = SPEC.is_ours
_load, _dump = hooks.load, hooks.dump
our_commands = functools.partial(hooks.our_commands, SPEC)
installed = functools.partial(hooks.installed, SPEC)
plan_install = functools.partial(hooks.plan_install, SPEC)
plan_uninstall = functools.partial(hooks.plan_uninstall, SPEC)
install = functools.partial(hooks.install, SPEC)
uninstall = functools.partial(hooks.uninstall, SPEC)


def active_for(cwd=None):
    """True when Claude Code would run the resume hook for a session in `cwd`: in the project's settings
    (settings.local.json, settings.json) or the user's (CLAUDE_CONFIG_DIR, else ~/.claude)."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(paths.HOME, ".claude")
    files = [os.path.join(cwd, ".claude", n) for n in ("settings.local.json", "settings.json")] if cwd else []
    return any(installed(f) for f in [*files, os.path.join(os.path.expanduser(cfg), "settings.json")])
