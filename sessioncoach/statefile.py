"""The hooks' small JSON state files in the data dir (idle_guard.json, idle_compact.json, keep_context.json).

Each one is {"entries": [...]}, oldest first, at most `max_entries` younger than `max_age_s` (by the entry's
`time_key`), written atomically with mode 600. A missing or corrupt file counts as empty; entries `valid` rejects are
dropped. update() reads, changes and writes under a short lock on a `<name>.lock` file next to it, so two sessions
writing at once do not lose one of the changes. When the lock cannot be taken in LOCK_TIMEOUT_S (or locking is not
possible there) it goes on without it: a hook must never fail or hang because of it.
"""
import contextlib
import json
import os
import time

from . import paths

LOCK_TIMEOUT_S = 0.25  # hooks run on every prompt: a short wait, then go on without the lock
LOCK_POLL_S = 0.01


def _try_lock(fd):
    """True when the exclusive lock on `fd` was taken, False when another holder has it. Other errors raise."""
    if os.name == "nt":  # Windows only (CI covers it there)
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd):
    if os.name == "nt":  # Windows only (CI covers it there)
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def locked(lock_path, timeout=None):
    """Hold an exclusive lock on `lock_path` (created 600) while the block runs; without it, silently, when it cannot
    be taken in `timeout` seconds or locking fails."""
    timeout = LOCK_TIMEOUT_S if timeout is None else timeout
    fd, held = None, False
    try:
        paths.private_dirs(os.path.dirname(os.path.realpath(lock_path)))
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        end = time.monotonic() + timeout
        while not held:
            held = _try_lock(fd)
            if held or time.monotonic() >= end:
                break
            time.sleep(LOCK_POLL_S)
    except Exception:  # no lock: go on without it
        held = False
    try:
        yield held
    finally:
        if fd is not None:
            with contextlib.suppress(Exception):
                if held:
                    _unlock(fd)
            with contextlib.suppress(OSError):
                os.close(fd)


class JsonStateFile:
    def __init__(self, name, max_entries, max_age_s, time_key="at", valid=None, ensure_ascii=True):
        self.name, self.max_entries, self.max_age_s = name, max_entries, max_age_s
        self.time_key, self.valid, self.ensure_ascii = time_key, valid, ensure_ascii

    def path(self):
        return os.path.join(paths.data_dir(), self.name)

    def lock_path(self):
        return self.path() + ".lock"

    def load(self):
        """The entries, oldest first; [] when the file is missing or corrupt. Read-only."""
        entries = paths.load_json_object(self.path()).get("entries")
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict) and (self.valid is None or self.valid(e))]

    def save(self, entries, now=None):
        now = now or time.time()
        kept = [e for e in entries if now - e[self.time_key] <= self.max_age_s][-self.max_entries:]
        data = json.dumps({"entries": kept}, indent=1, ensure_ascii=self.ensure_ascii) + "\n"
        paths.write_atomic(self.path(), data.encode("utf-8"), default_mode=0o600, backup=False)

    def update(self, change, now=None):
        """save(change(load()), now) under the lock."""
        with locked(self.lock_path()):
            self.save(change(self.load()), now)

    def remove(self):
        """Delete the file (and its lock). -> True when there was one."""
        with contextlib.suppress(OSError):
            os.remove(self.lock_path())
        try:
            os.remove(self.path())
        except FileNotFoundError:
            return False
        return True
