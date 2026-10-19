"""The idle guard's state: prompts held back after a long pause, and the pending handoff each one leaves for /clear.

With `nudge.block` on in alerts.json (off by default; `sessioncoach idle-guard-on`), the prompt hook (nudge.py) holds
back the first prompt after a pause longer than the cache and records one entry here, keyed by session id and the time
of the last turn before that pause. Sending again finds the entry and goes through. After /clear the SessionStart hook
(resume.py) takes the newest entry of the project that was not let through, builds a handoff from that session's
transcript (digest.py) and removes the entry. An entry holds the session id, that last turn's time, the transcript
path, the working dir and the time it was written: never the prompt or any text of the session. The file lives in the
data dir (mode 600), keeps at most MAX_ENTRIES younger than MAX_AGE_S, and a corrupt one counts as empty.
"""
import glob
import os
import time

from . import statefile

FILE = "idle_guard.json"
MAX_ENTRIES = 50
MAX_AGE_S = 48 * 3600
PENDING_S = 30 * 60  # a /clear later than this after the prompt was held back is another matter: no automatic handoff


def _valid(e):
    return isinstance(e.get("session_id"), str) and isinstance(e.get("last_ts"), str) and isinstance(e.get("ts"), (int, float))


STATE = statefile.JsonStateFile(FILE, MAX_ENTRIES, MAX_AGE_S, time_key="ts", valid=_valid, ensure_ascii=False)
path = STATE.path


def enabled(alerts=None):
    """True when alerts.json → nudge.block is true (exactly true: anything else is off)."""
    if alerts is None:
        from .config import load_alerts
        alerts = load_alerts()
    nd = alerts.get("nudge")
    return isinstance(nd, dict) and nd.get("block") is True


def load():
    """The entries, oldest first; [] when the file is missing or corrupt. Bad entries are dropped."""
    return STATE.load()


_save = STATE.save


def find(session_id, last_ts):
    """The entry of this session and this pause, or None."""
    return next((e for e in load() if e["session_id"] == session_id and e["last_ts"] == last_ts), None)


def hold(data, last_ts, now=None):
    """Record that the prompt of hook input `data` was held back after the turn at `last_ts`."""
    now = now or time.time()
    entry = {"session_id": data["session_id"], "last_ts": last_ts, "transcript_path": data.get("transcript_path"),
             "cwd": data.get("cwd"), "ts": now}
    STATE.update(lambda es: [e for e in es if not (e["session_id"] == entry["session_id"] and e["last_ts"] == last_ts)]
                 + [entry], now)
    return entry


def let_through(session_id, last_ts, now=None):
    """The prompt was sent again: the person chose to go on, so the entry no longer offers a handoff."""
    def mark(entries):
        for e in entries:
            if e["session_id"] == session_id and e["last_ts"] == last_ts:
                e["resent"] = True
        return entries
    STATE.update(mark, now)


def _same_project(e, cwd, transcript_path):
    tp = e.get("transcript_path")
    if transcript_path and tp and os.path.dirname(os.path.abspath(tp)) == os.path.dirname(os.path.abspath(transcript_path)):
        return True
    return bool(cwd and e.get("cwd")) and os.path.realpath(e["cwd"]) == os.path.realpath(cwd)


def _pending_all(cwd, transcript_path, now):
    if not os.path.exists(path()):
        return []
    now = now or time.time()
    return [e for e in load() if not e.get("resent") and 0 <= now - e["ts"] <= PENDING_S
            and _same_project(e, cwd, transcript_path)]


def pending(cwd, transcript_path=None, now=None):
    """The newest entry held back in this project less than PENDING_S ago and not sent again, or None. Read-only."""
    cands = _pending_all(cwd, transcript_path, now)
    return max(cands, key=lambda e: e["ts"]) if cands else None


def _other_window_active(entry, transcript_path):
    """True when another transcript of the entry's Claude project dir was written after the prompt was held back: the
    /clear may have come from that window. The new session's own transcript does not count."""
    tp = entry.get("transcript_path")
    if not tp:
        return False
    mine = {os.path.normcase(os.path.abspath(p)) for p in (tp, transcript_path) if p}
    for f in glob.glob(os.path.join(glob.escape(os.path.dirname(os.path.abspath(tp))), "*.jsonl")):
        try:
            if os.path.normcase(os.path.abspath(f)) not in mine and os.path.getmtime(f) > entry["ts"]:
                return True
        except OSError:
            continue
    return False


def pending_for_clear(cwd, transcript_path=None, now=None):
    """pending(), but only when the /clear surely came from the held session's window: None when another entry of the
    project is pending or another session of it was active since. Better no automatic handoff than another session's.
    Read-only."""
    cands = _pending_all(cwd, transcript_path, now)
    if len(cands) != 1 or _other_window_active(cands[0], transcript_path):
        return None
    return cands[0]


def consume(entry, now=None):
    """Remove `entry`: its handoff was given (or a newer handoff file took its place)."""
    STATE.update(lambda es: [e for e in es if not (e["session_id"] == entry["session_id"] and e["last_ts"] == entry["last_ts"])],
                 now)


remove = STATE.remove  # uninstall: -> True when there was a file
