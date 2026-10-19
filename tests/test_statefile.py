"""The hooks' JSON state files: bounds, format, mode 600 and the lock around read-modify-write."""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate

from sessioncoach import idlecompact, idleguard, keepctx, statefile


class StateFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(isolate.rmtree, self.tmp)
        env = mock.patch.dict(os.environ, {"KIT_DATA_DIR": os.path.join(self.tmp, "data")})
        env.start()
        self.addCleanup(env.stop)
        self.sf = statefile.JsonStateFile("t.json", 10000, 3600, valid=lambda e: isinstance(e.get("session_id"), str))

    def add(self, sid, now=None):
        self.sf.update(lambda es: [*es, {"session_id": sid, "at": now or time.time()}], now)

    def test_bounds_format_and_mode(self):
        sf = statefile.JsonStateFile("b.json", 3, 100, time_key="ts", ensure_ascii=False)
        sf.update(lambda es: [{"session_id": "old", "ts": 0}] + [{"session_id": "ä%d" % i, "ts": 1000 + i} for i in range(5)],
                  now=1010)
        self.assertEqual([e["session_id"] for e in sf.load()], ["ä2", "ä3", "ä4"])
        with open(sf.path(), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith('{\n "entries": [\n  {\n   "session_id": "ä2"'), text)
        if os.name != "nt":
            for p in (sf.path(), sf.lock_path()):
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600, p)

    def test_corrupt_or_missing_counts_as_empty_and_reading_creates_nothing(self):
        self.assertEqual(self.sf.load(), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "data")))
        os.makedirs(os.path.join(self.tmp, "data"))
        for raw in ("not json", "[]", '{"entries": "x"}', '{"entries": [1, {"session_id": 2}]}'):
            with open(self.sf.path(), "w") as fh:
                fh.write(raw)
            self.assertEqual(self.sf.load(), [], raw)
        self.assertFalse(os.path.exists(self.sf.lock_path()))

    def test_remove_takes_the_lock_file_too(self):
        self.assertFalse(self.sf.remove())
        self.add("a")
        self.assertTrue(os.path.exists(self.sf.lock_path()))
        self.assertTrue(self.sf.remove())
        self.assertFalse(os.path.exists(self.sf.path()) or os.path.exists(self.sf.lock_path()))

    def test_concurrent_updates_lose_nothing(self):
        def slow_add(sid):
            def change(es):
                time.sleep(0.002)  # widen the read-modify-write window
                return [*es, {"session_id": sid, "at": time.time()}]
            self.sf.update(change)

        def worker(n):
            for i in range(15):
                slow_add("w%d-%d" % (n, i))
        with mock.patch.object(statefile, "LOCK_TIMEOUT_S", 60):
            threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len({e["session_id"] for e in self.sf.load()}), 6 * 15)

    def test_a_lock_held_too_long_is_skipped_not_waited_for(self):
        os.makedirs(os.path.join(self.tmp, "data"))
        with statefile.locked(self.sf.lock_path()) as held:
            self.assertTrue(held)
            with mock.patch.object(statefile, "LOCK_TIMEOUT_S", 0.05):
                t0 = time.monotonic()
                with statefile.locked(self.sf.lock_path()) as second:
                    self.assertFalse(second)
                self.add("a")  # goes on without the lock
                self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual([e["session_id"] for e in self.sf.load()], ["a"])

    def test_locking_errors_never_stop_the_write(self):
        with mock.patch.object(statefile, "_try_lock", side_effect=OSError("no locks here")):
            self.add("a")
        with mock.patch.object(statefile.os, "open", side_effect=PermissionError("read-only")), \
                statefile.locked(self.sf.lock_path()) as held:
            self.assertFalse(held)
        with mock.patch.object(statefile, "_unlock", side_effect=OSError("gone")):
            self.add("b")
        self.assertEqual([e["session_id"] for e in self.sf.load()], ["a", "b"])

    def test_a_failing_change_releases_the_lock(self):
        with self.assertRaises(KeyError):
            self.sf.update(lambda es: {}["x"])
        with mock.patch.object(statefile, "LOCK_TIMEOUT_S", 0), statefile.locked(self.sf.lock_path()) as held:
            self.assertTrue(held)

    def test_the_hooks_files_keep_their_format(self):
        keepctx.keep("k", now=1000.0)
        idleguard.hold({"session_id": "g", "transcript_path": "/t/ä", "cwd": "/c"}, "t1", now=1000.0)
        with mock.patch("time.time", return_value=1001.0):
            idleguard.let_through("g", "t1")
        idlecompact.STATE.update(lambda es: [*es, {"session_id": "c", "at": 1000.0, "last_ts": "l"}], 1000.0)
        with open(keepctx.path(), encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.read()), {"entries": [{"session_id": "k", "at": 1000.0}]})
        with open(idleguard.path(), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('"/t/ä"', text)  # ensure_ascii=False, as before
        self.assertEqual(json.loads(text)["entries"][0]["resent"], True)
        self.assertEqual(idlecompact.load(), [{"session_id": "c", "at": 1000.0, "last_ts": "l"}])
        for mod in (idleguard, idlecompact, keepctx):
            self.assertTrue(mod.remove())
            self.assertFalse(os.path.exists(mod.path() + ".lock"))


if __name__ == "__main__":
    unittest.main()
