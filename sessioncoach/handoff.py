"""The /handoff command: Claude writes the state of the session down, so you can /clear and go on in a clean session.

`sessioncoach handoff-install` writes a Claude Code command file, commands/handoff.md, next to the settings file of the
chosen scope (~/.claude/commands, <project>/.claude/commands or $CLAUDE_CONFIG_DIR/commands). Running /handoff [topic]
in a session makes Claude write docs/HANDOFF-<date>-<topic>.md (state, decisions, next steps, files touched, how to
verify) and end by telling you to type /clear. When the resume hook (resume.py) is installed, the new session gets the
handoff by itself; when it is not, the command also gives the line to type after /clear. The file carries a marker:
a handoff.md of your own (no marker) is never overwritten or removed.
"""
import os

from . import paths, pricing

NAME = "handoff.md"
DOCS_DIR = "docs"  # where /handoff writes and the resume hook looks, relative to the project
PREFIX = "HANDOFF-"
MARK = "<!-- sessioncoach:handoff -->"

# what the command gives the user, said by `sessioncoach setup` before asking (the technical side: `sessioncoach help handoff-install`)
VALUE = ("Pick up a paused session without paying for its context on every message: one command, /handoff, writes the "
         "state down and you go on in a clean session.")

_AFTER = {
    True: "After /clear the resume hook gives the new session this handoff by itself: the user only types /clear.",
    False: "Then add, on its own line, the exact message the user types in the new session after /clear: «read <path of the "
           "handoff> and continue», all of it translated into the language of this conversation.",
}

TEMPLATE = """---
description: Write a handoff of this session (state, decisions, next steps) so you can /clear and go on in a clean session
argument-hint: [topic or focus]
---
%(mark)s
<!-- Written by `sessioncoach handoff-install`; `sessioncoach handoff-uninstall` removes it. -->
Write a handoff of this session, so that a new session can continue the work without this conversation.

Topic or focus asked for: $ARGUMENTS
(If empty, take a short topic from the work of this session.)

1. Write it to `%(docs)s/%(prefix)s<today, YYYY-MM-DD>-<topic as a short kebab-case slug>.md` at the root of the project
   this session works on (create `%(docs)s/` if needed; if the file exists, add `-2`, `-3`, ...). Write it in the
   language of this conversation. If the session worked on several unrelated projects, write one handoff per project,
   in each project's own `%(docs)s/`. If that folder is committed to a public repository, ask the user where to save it first.
2. Use these sections, with their names in the language of this conversation; facts only, no transcript excerpts and no secrets:
   - Goal: what the work is for.
   - State: what is done and verified; what is half done (branch, last commit, uncommitted changes).
   - Decisions: what was decided and why, including what the user approved or turned down.
   - Next steps: numbered, concrete, in order.
   - Files touched: one line per path on what changed.
   - How to verify: the exact commands (tests, lint, checks) and what passing looks like.
3. Do nothing else in this turn: no code changes, no commits.
4. When it is written, end your reply with its path and then, on its own line, exactly: /clear
   %(after)s
"""


def render(resume_hook=False):
    """The command file's text; it depends on whether the resume hook is installed (see _AFTER)."""
    return TEMPLATE % {"mark": MARK, "docs": DOCS_DIR, "prefix": PREFIX, "after": _AFTER[bool(resume_hook)]}


def command_path(settings_path=None):
    """commands/handoff.md next to a settings file: the same scope as the hook and the status line."""
    settings_path = settings_path or paths.user_settings()
    return os.path.join(os.path.dirname(settings_path), "commands", NAME)


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def state(path, resume_hook=False):
    """'missing' | 'foreign' (a handoff.md that is not ours) | 'stale' (ours, written for the other resume-hook state
    or by an older version) | 'current'."""
    text = _read(path)
    if text is None:
        return "missing"
    if MARK not in text:
        return "foreign"
    return "current" if text == render(resume_hook) else "stale"


def installed(settings_path=None):
    return state(command_path(settings_path)) in ("current", "stale")


def available(cwd=None):
    """True when /handoff is installed where Claude Code would find it for a session in `cwd`: the project's
    .claude/commands, or the user's (CLAUDE_CONFIG_DIR, else ~/.claude). The warnings then say `/handoff`."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(paths.HOME, ".claude")
    dirs = ([os.path.join(cwd, ".claude")] if cwd else []) + [os.path.expanduser(cfg)]
    return any(state(os.path.join(d, "commands", NAME)) in ("current", "stale") for d in dirs)


def install(settings_path=None, resume_hook=False):
    """Write commands/handoff.md for the scope of `settings_path`; one of your own is left alone."""
    path = command_path(settings_path)
    st = state(path, resume_hook)
    if st == "foreign":
        return "skipped — %s is your own command (no sessioncoach marker); rename it to install /handoff" % path
    if st == "current":
        return "already installed (%s)" % path
    backup = paths.backup_file(path, ".bak-handoff") if st == "stale" else None
    paths.write_atomic(path, render(resume_hook).encode("utf-8"), backup=False)
    return ("updated (%s, backup %s)" % (path, backup)) if backup else "installed: /handoff (%s)" % path


def refresh(settings_path, resume_hook):
    """Rewrite an installed command for the resume hook's state; None when there is none of ours there."""
    return install(settings_path, resume_hook) if installed(settings_path) else None


def uninstall(settings_path=None):
    path = command_path(settings_path)
    st = state(path)
    if st == "missing":
        return "not installed"
    if st == "foreign":
        return "left alone — %s is not sessioncoach's" % path
    os.remove(path)
    return "removed (%s)" % path


def cost_note():
    """What the /handoff turn costs when the cache already expired, said once at install."""
    return ("note: after a pause longer than the cache (1 h), the /handoff turn writes the whole context once more, e.g. "
            "500k tokens on Opus ≈ $%.2f (API-equivalent at list price). Only that session has the context, so it cannot be "
            "avoided; it is paid once, instead of on every message after coming back." % rewrite_cost())


def rewrite_cost(tokens=500000, model="claude-opus-5-5"):
    """List-price USD of writing `tokens` of context once to a 1-hour cache: what the /handoff turn of a session whose
    cache already expired costs (example figure for the README and the install message)."""
    return tokens * pricing.price(model)[3] / 1e6
