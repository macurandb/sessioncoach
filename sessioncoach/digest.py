"""Automatic handoff: a digest of a session built from its transcript, with no model and no tokens spent.

After the idle guard held a prompt back (nudge.py, idleguard.py) and the person ran /clear, the resume hook
(resume.py) gives the new session this digest of the previous one: Claude Code's own compaction summary when the
transcript has one, the person's last prompts, Claude's last reply, the files edited, the latest todo list, and the
branch and `git status --short` of the working dir. It reads the tail of the transcript (TAIL_BYTES; bad lines are
skipped) and runs git read-only with a short timeout. The text goes only to the new session's model, on this
machine; sessioncoach stores none of it. It is worse than a handoff written by Claude (/handoff): it has the facts,
not the reasons.
"""
import json
import os
import subprocess

TAIL_BYTES = 4 * 1024 * 1024
MAX_PROMPTS, PROMPT_CHARS = 8, 400
REPLY_CHARS = 1500
SUMMARY_CHARS = 3500
MAX_FILES = 30
MAX_TODOS, TODO_CHARS = 20, 200
GIT_TIMEOUT_S = 1.5
GIT_LINES = 30
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# text Claude Code adds as user messages that the person did not type
NOISE = ("<task-notification", "<ide_selection", "<ide_opened_file", "<browser_instruction", "<local-command-caveat",
         "<command-name", "<command-message", "<command-args", "<local-command-stdout", "<local-command-stderr",
         "<artifact-content-authored-by-", "<objective", "<system-reminder", "<user-prompt-submit-hook", "[Request interrupted")


def _cut(text, n):
    text = text.strip()
    return text if len(text) <= n else text[:n].rstrip() + " […]"


def _lines(path):
    """The JSON objects of the transcript's tail, in order; bad or partial lines are skipped."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        raw = fh.read()
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]  # the first one starts mid-line
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            yield d


def _texts(content):
    """The text blocks of a message's content (a string or a list of blocks)."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"]


def _prompt(d, msg):
    """What the person typed in this user line, or None for tool results, meta lines and Claude Code's own text."""
    origin = d.get("origin")
    if d.get("isMeta") or (isinstance(origin, dict) and origin.get("kind") not in (None, "human")):
        return None
    content = msg.get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    parts = [t.strip() for t in _texts(content) if t.strip() and not t.lstrip().startswith(NOISE)]
    return "\n".join(parts) or None


def read(path):
    """-> {"summary", "prompts", "reply", "files", "todos"} from the transcript at `path` (main thread only)."""
    out = {"summary": None, "prompts": [], "reply": None, "files": [], "todos": None}
    reply_id, reply = None, []
    for d in _lines(path):
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        if d.get("isSidechain") or d.get("type") not in ("user", "assistant"):
            continue
        if d["type"] == "user":
            if d.get("isCompactSummary"):
                out["summary"] = "\n".join(_texts(msg.get("content"))).strip() or out["summary"]
                continue
            p = _prompt(d, msg)
            if p:
                out["prompts"].append(p)
            continue
        if msg.get("model") == "<synthetic>" or not isinstance(msg.get("content"), list):
            continue
        texts = [t for t in _texts(msg["content"]) if t.strip()]
        if texts:
            if msg.get("id") != reply_id or not reply:  # one reply can span several lines with the same message id
                reply_id, reply = msg.get("id"), []
            reply += texts
        for b in msg["content"]:
            if not isinstance(b, dict) or b.get("type") != "tool_use" or not isinstance(b.get("input"), dict):
                continue
            if b.get("name") in EDIT_TOOLS:
                f = b["input"].get("file_path") or b["input"].get("notebook_path")
                if isinstance(f, str) and f:
                    out["files"].append(f)
            elif b.get("name") == "TodoWrite" and isinstance(b["input"].get("todos"), list):
                out["todos"] = [t for t in b["input"]["todos"] if isinstance(t, dict)]
    out["reply"] = "\n".join(reply) or None
    seen, files = set(), []
    for f in reversed(out["files"]):  # most recent first, once each
        if f not in seen:
            seen.add(f)
            files.append(f)
    out["files"] = files
    return out


def git_argv(cwd, *args):
    """git run read-only in `cwd`: no optional locks and no fsmonitor (a repository's config could start a daemon)."""
    return ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", cwd, *args]


def _git(cwd, *args):
    r = subprocess.run(git_argv(cwd, *args), stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_S, check=False)
    if r.returncode != 0:
        raise OSError(r.stderr.strip())
    return r.stdout


def git_state(cwd):
    """(branch, `git status --short` lines) of `cwd`, or None when it is not a repository or git is missing or slow."""
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip()
        status = _git(cwd, "status", "--short").splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    return branch, status


def build(transcript_path, cwd=None, max_chars=9000):
    """The digest's text (facts, not orders), at most `max_chars`; None when the transcript cannot be read or has
    nothing in it."""
    try:
        d = read(transcript_path)
    except OSError:
        return None
    if not (d["summary"] or d["prompts"] or d["reply"] or d["files"]):
        return None
    parts = []
    if d["summary"]:
        parts.append("## Claude Code's compaction summary of that session\n" + _cut(d["summary"], SUMMARY_CHARS))
    if d["prompts"]:
        parts.append("## The user's last messages in it (oldest first, quoted)\n"
                     + "\n".join("- " + _cut(p, PROMPT_CHARS).replace("\n", "\n  ") for p in d["prompts"][-MAX_PROMPTS:]))
    if d["reply"]:
        parts.append("## Claude's last reply in it\n" + _cut(d["reply"], REPLY_CHARS))
    if d["files"]:
        more = len(d["files"]) - MAX_FILES
        parts.append("## Files edited or written (most recent first)\n" + "\n".join("- " + f for f in d["files"][:MAX_FILES])
                     + ("\n- … and %d more" % more if more > 0 else ""))
    if d["todos"]:
        parts.append("## Its latest todo list\n" + "\n".join(
            "- [%s] %s" % (t.get("status") or "?", _cut(str(t.get("content") or ""), TODO_CHARS)) for t in d["todos"][:MAX_TODOS]))
    g = git_state(cwd)
    if g:
        lines = g[1][:GIT_LINES] + (["… and %d more" % (len(g[1]) - GIT_LINES)] if len(g[1]) > GIT_LINES else [])
        parts.append("## Git now, in %s\nbranch %s\n%s" % (cwd, g[0], "\n".join(lines) or "(clean working tree)"))
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        note = "\n\n[cut here: the full previous session is in %s]" % transcript_path
        text = text[:max(0, max_chars - len(note))].rstrip() + note
    return text
