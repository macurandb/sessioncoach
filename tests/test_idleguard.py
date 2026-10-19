"""Idle guard: the prompt hook holding back the first prompt after a long pause (synthetic transcripts only)."""
import datetime as dt
import io
import json
import os
import stat
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate

from sessioncoach import cli, handoff, idlecompact, idleguard, installer, nudge, resume, store

PROMPT = "sigue con la siguiente fase por favor"


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Base(unittest.TestCase):
    def setUp(self):
        self.home = isolate.claude_home(self)
        self.data = os.path.join(self.home, "data")
        os.environ["KIT_DATA_DIR"] = self.data
        self.pdir = os.path.join(self.home, ".claude", "projects", "-p-demo")
        os.makedirs(self.pdir)
        self.cwd = os.path.join(self.home, "work")
        os.makedirs(self.cwd)
        self.last = iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=90, seconds=20))
        self.path = self.transcript("s1", self.last)

    def transcript(self, sid, ts, ctx=250000):
        path = os.path.join(self.pdir, sid + ".jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": ts, "sessionId": sid, "message": {
                "id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 0, "cache_read_input_tokens": ctx}}}) + "\n")
        return path

    def alerts(self, cfg):
        os.makedirs(self.data, exist_ok=True)
        with open(os.path.join(self.data, "alerts.json"), "w") as fh:
            json.dump(cfg, fh)

    def payload(self, **kw):
        d = {"session_id": "s1", "transcript_path": self.path, "cwd": self.cwd, "prompt": PROMPT}
        d.update(kw)
        return d

    def run_hook(self, **kw):
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(self.payload(**kw)))), \
                redirect_stdout(io.StringIO()) as out:
            nudge.hook_main()
        return out.getvalue()

    def events(self):
        con = store.connect(os.path.join(self.data, "metrics.db"))
        try:
            return [json.loads(r["data"]) for r in con.execute("SELECT data FROM alert_events")]
        finally:
            con.close()


class Disabled(Base):
    def test_off_is_the_plain_warning_byte_for_byte_and_writes_no_marker(self):
        want = json.dumps({"systemMessage": nudge.check(self.payload(), {})}, ensure_ascii=False) + "\n"
        self.assertEqual(self.run_hook(), want)
        for cfg in ({"nudge": {"block": False}}, {"nudge": {"block": "yes"}}, {"nudge": {"idle_min": 60}}):
            self.alerts(cfg)
            self.assertEqual(self.run_hook(), want, cfg)
        self.assertFalse(os.path.exists(idleguard.path()))

    def test_enabled_only_when_exactly_true(self):
        self.assertFalse(idleguard.enabled({}))
        self.assertFalse(idleguard.enabled({"nudge": "odd"}))
        self.assertFalse(idleguard.enabled({"nudge": {"block": 1}}))
        self.assertTrue(idleguard.enabled({"nudge": {"block": True}}))
        self.alerts({"nudge": {"block": True}})
        self.assertTrue(idleguard.enabled())

    def precompact(self, **kw):
        d = {"session_id": "s1", "transcript_path": self.path, "cwd": self.cwd, "hook_event_name": "PreCompact",
             "trigger": "auto"}
        d.update(kw)
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(d))), redirect_stdout(io.StringIO()) as out:
            cli.main(["hook", "pre-compact"])
        self.assertEqual(out.getvalue(), "")  # never anything that could block the compaction
        return idlecompact.load()

    def test_a_prepared_idle_summary_silences_the_warning_and_the_block(self):
        self.assertFalse(nudge.idle_summary_pending(self.payload(), self.last))
        e, = self.precompact()
        self.assertEqual((e["session_id"], e["last_ts"], e["transcript_path"]), ("s1", self.last, self.path))
        self.assertEqual(e["transcript_size"], os.path.getsize(self.path))
        self.assertTrue(nudge.idle_summary_pending(self.payload(), self.last))
        want = {"systemMessage": nudge.SUMMARY_MSG["es"].format(m=90)}
        for cfg in ({}, {"nudge": {"block": True}}):
            self.alerts(cfg)
            self.assertEqual(json.loads(self.run_hook()), want, cfg)
        self.assertFalse(os.path.exists(idleguard.path()))
        self.assertIn("summary it prepared", nudge.check(self.payload(prompt="please go on with the next step"), {}))
        # applied (compact_boundary) or a new turn: the last turn is another one, so it no longer counts
        self.transcript("s1", iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=80)))
        self.assertIn("block", self.run_hook())

    def test_the_pre_compact_hook_keeps_only_idle_automatic_compactions(self):
        self.assertEqual(self.precompact(trigger="manual"), [])
        self.assertEqual(self.precompact(session_id=None), [])
        self.assertEqual(self.precompact(transcript_path="/missing.jsonl"), [])
        self.transcript("s1", iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)))
        self.assertEqual(self.precompact(), [])  # a full context, not a pause
        self.assertFalse(os.path.exists(idlecompact.path()))
        with mock.patch.object(sys, "stdin", io.StringIO("not json")), redirect_stdout(io.StringIO()) as out:
            idlecompact.hook_main()
        self.assertEqual(out.getvalue(), "")

    def test_idle_summary_markers_are_bounded_and_tolerate_corruption(self):
        os.makedirs(self.data, exist_ok=True)
        with open(idlecompact.path(), "w") as fh:
            fh.write("{bad")
        self.assertFalse(idlecompact.pending("s1", self.last))
        old = {"session_id": "old", "at": time.time() - idlecompact.MAX_AGE_S - 1, "last_ts": "x"}
        many = [{"session_id": "s%d" % i, "at": time.time(), "last_ts": "x"} for i in range(idlecompact.MAX_ENTRIES + 5)]
        with open(idlecompact.path(), "w") as fh:
            json.dump({"entries": [old, 7, *many]}, fh)
        self.assertFalse(idlecompact.pending("old", "x"))
        self.assertTrue(idlecompact.pending("s3", "x"))
        self.precompact(session_id="s3")
        kept = idlecompact.load()
        self.assertEqual(len(kept), idlecompact.MAX_ENTRIES)
        self.assertEqual([e["session_id"] for e in kept].count("s3"), 1)
        self.assertFalse(idlecompact.pending(None, "x"))
        self.assertTrue(idlecompact.remove())
        self.assertFalse(idlecompact.remove())


class Block(Base):
    def setUp(self):
        super().setUp()
        self.alerts({"nudge": {"block": True}})
        store.connect(os.path.join(self.data, "metrics.db")).close()

    def test_first_prompt_is_held_back_with_the_ways_out(self):
        out = json.loads(self.run_hook())
        self.assertEqual(out["decision"], "block")
        r = out["reason"]
        self.assertIn("90 min sin actividad", r)
        self.assertIn("NO se envió", r)
        self.assertIn("Original prompt:", r)
        self.assertIn("no preparó un resumen", r)
        self.assertIn("~250k tokens", r)
        self.assertIn("/clear (sin traspaso", r)  # no resume hook: no automatic handoff promised
        self.assertNotIn("/handoff", r)
        self.assertNotIn("systemMessage", out)
        e, = idleguard.load()
        self.assertEqual((e["session_id"], e["last_ts"], e["transcript_path"], e["cwd"]), ("s1", self.last, self.path, self.cwd))
        with open(idleguard.path()) as fh:
            self.assertNotIn("sigue", fh.read())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(idleguard.path()).st_mode), 0o600)
        ev, = self.events()
        self.assertEqual((ev["blocked"], ev["context"]), (1, 250000))

    def test_english_and_the_free_clear_and_handoff_lines_when_they_work(self):
        resume.install(os.path.join(self.home, ".claude", "settings.json"), kit_bin="/k/bin/sessioncoach")
        handoff.install(os.path.join(self.home, ".claude", "settings.json"), resume_hook=True)
        r = json.loads(self.run_hook(prompt="please go on with the next step"))["reason"]
        self.assertIn("held back and NOT sent", r)
        self.assertIn("start clean for free: /clear", r)
        self.assertIn("better handoff: /handoff", r)

    def test_sending_again_in_the_same_pause_goes_through_silently(self):
        self.assertIn("block", self.run_hook())
        self.assertEqual(self.run_hook(), "")
        self.assertEqual(self.run_hook(prompt="otra cosa distinta"), "")
        e, = idleguard.load()
        self.assertTrue(e["resent"])
        self.assertEqual(len(self.events()), 1)

    def test_a_new_pause_or_another_session_is_held_back_again(self):
        self.run_hook()
        self.run_hook()
        later = iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=70))
        self.transcript("s1", later)
        self.assertIn("block", self.run_hook())
        p2 = self.transcript("s2", self.last)
        self.assertIn("block", self.run_hook(session_id="s2", transcript_path=p2))
        self.assertEqual(len(idleguard.load()), 3)

    def test_handoff_always_goes_through(self):
        self.assertEqual(self.run_hook(prompt="  /handoff fase 2"), "")
        self.assertEqual(idleguard.load(), [])

    def test_clear_and_compact_go_through_and_leave_the_pending_handoff(self):
        self.run_hook()
        for p in ("/clear", "/compact keep the decisions"):
            self.assertEqual(self.run_hook(prompt=p), "")
        e, = idleguard.load()
        self.assertNotIn("resent", e)
        self.assertEqual(idleguard.pending(self.cwd)["session_id"], "s1")

    def test_falls_back_to_the_warning_when_the_pause_cannot_be_recorded(self):
        self.assertIn("systemMessage", self.run_hook(session_id=None))
        with mock.patch.object(idleguard, "hold", side_effect=OSError("read-only")):
            self.assertIn("systemMessage", self.run_hook())
        self.assertEqual(self.events()[-1].get("blocked"), None)

    def test_a_5_min_cache_is_warned_not_held_back_before_block_min(self):
        p = os.path.join(self.pdir, "s5.jsonl")
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)),
                                 "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {
                                     "input_tokens": 0, "cache_read_input_tokens": 250000, "cache_creation_input_tokens": 10,
                                     "cache_creation": {"ephemeral_5m_input_tokens": 10}}}}) + "\n")
        self.assertIn("systemMessage", self.run_hook(session_id="s5", transcript_path=p))
        self.assertEqual(idleguard.load(), [])
        for limit in (10, 10.5):
            self.alerts({"nudge": {"block": True, "block_min": limit}})
            idleguard.remove()
            self.assertIn("block", self.run_hook(session_id="s5", transcript_path=p))
        for bad in (0, -5, True, "10"):  # a nonsense value is the default, 60 min
            self.alerts({"nudge": {"block": True, "block_min": bad}})
            self.assertIn("systemMessage", self.run_hook(session_id="s5", transcript_path=p))

    def test_short_pause_or_small_context_is_not_held_back(self):
        self.transcript("s1", iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)))
        self.assertEqual(self.run_hook(), "")
        self.transcript("s1", self.last, ctx=20000)
        self.assertEqual(self.run_hook(), "")
        self.assertFalse(os.path.exists(idleguard.path()))


class Marker(Base):
    def entry(self, sid, age_s, **kw):
        e = {"session_id": sid, "last_ts": "2026-10-01T10:00:00Z", "transcript_path": self.path, "cwd": self.cwd,
             "ts": time.time() - age_s}
        e.update(kw)
        return e

    def write_raw(self, text):
        os.makedirs(self.data, exist_ok=True)
        with open(idleguard.path(), "w") as fh:
            fh.write(text)

    def test_corrupt_file_counts_as_empty_and_is_replaced(self):
        for raw in ("not json", "[]", '{"entries": "x"}', '{"entries": [1, {"session_id": 2}, {"session_id": "a"}]}'):
            self.write_raw(raw)
            self.assertEqual(idleguard.load(), [], raw)
            self.assertIsNone(idleguard.find("a", "x"))
        idleguard.hold(self.payload(), "t1")
        self.assertEqual([e["last_ts"] for e in idleguard.load()], ["t1"])

    def test_bounded_by_age_and_count(self):
        old = [self.entry("old", idleguard.MAX_AGE_S + 60)]
        many = [self.entry("s%d" % i, 100 - i) for i in range(idleguard.MAX_ENTRIES + 10)]
        self.write_raw(json.dumps({"entries": old + many}))
        idleguard.hold(self.payload(session_id="new"), "t")
        kept = idleguard.load()
        self.assertEqual(len(kept), idleguard.MAX_ENTRIES)
        self.assertNotIn("old", [e["session_id"] for e in kept])
        self.assertEqual(kept[-1]["session_id"], "new")
        idleguard.hold(self.payload(session_id="new"), "t")  # the same pause again: replaced, not added
        self.assertEqual([e["session_id"] for e in idleguard.load()].count("new"), 1)

    def test_pending_is_the_newest_recent_one_of_the_project_not_sent_again(self):
        self.assertIsNone(idleguard.pending(self.cwd))  # no file: read-only, nothing created
        self.assertFalse(os.path.exists(self.data))
        other = os.path.join(self.home, "elsewhere")
        os.makedirs(other)
        self.write_raw(json.dumps({"entries": [
            self.entry("a", 600), self.entry("b", 300), self.entry("c", 60, resent=True),
            self.entry("d", idleguard.PENDING_S + 60), self.entry("e", 30, cwd=other, transcript_path="/x/y/e.jsonl")]}))
        self.assertEqual(idleguard.pending(self.cwd)["session_id"], "b")
        self.assertEqual(idleguard.pending(None, os.path.join(self.pdir, "new.jsonl"))["session_id"], "b")
        self.assertEqual(idleguard.pending(other)["session_id"], "e")
        self.assertIsNone(idleguard.pending(os.path.join(self.home, "nowhere")))
        idleguard.consume(idleguard.pending(self.cwd))
        self.assertEqual(idleguard.pending(self.cwd)["session_id"], "a")
        self.assertTrue(idleguard.remove())
        self.assertFalse(idleguard.remove())

    def test_pending_for_clear_only_when_no_other_session_of_the_project_could_have_cleared(self):
        self.assertIsNone(idleguard.pending_for_clear(self.cwd))  # no file
        t = time.time() - 3600
        os.utime(self.path, (t, t))
        self.write_raw(json.dumps({"entries": [self.entry("a", 600)]}))
        self.assertEqual(idleguard.pending_for_clear(self.cwd)["session_id"], "a")
        with open(os.path.join(self.pdir, "other.jsonl"), "w") as fh:
            fh.write("{}\n")
        self.assertIsNone(idleguard.pending_for_clear(self.cwd))  # another session written since the hold
        with mock.patch.object(os.path, "getmtime", side_effect=OSError("gone")):  # a transcript removed meanwhile
            self.assertEqual(idleguard.pending_for_clear(self.cwd)["session_id"], "a")
        self.write_raw(json.dumps({"entries": [self.entry("a", 600, transcript_path=None)]}))
        self.assertEqual(idleguard.pending_for_clear(self.cwd)["session_id"], "a")  # nothing to compare with
        self.write_raw(json.dumps({"entries": [self.entry("a", 600), self.entry("b", 300)]}))
        self.assertIsNone(idleguard.pending_for_clear(self.cwd))
        self.assertEqual(idleguard.pending(self.cwd)["session_id"], "b")  # pending itself is unchanged


class ResumeActive(Base):
    def test_project_or_user_settings(self):
        self.assertFalse(resume.active_for(self.cwd))
        resume.install(os.path.join(self.cwd, ".claude", "settings.local.json"), kit_bin="/k/bin/sessioncoach")
        self.assertTrue(resume.active_for(self.cwd))
        self.assertFalse(resume.active_for(None))
        resume.install(os.path.join(self.home, ".claude", "settings.json"), kit_bin="/k/bin/sessioncoach")
        self.assertTrue(resume.active_for(None))


class Install(Base):
    def setUp(self):
        super().setUp()
        os.environ["KIT_SCHEDULER"] = "none"
        self.settings = os.path.join(self.home, ".claude", "settings.json")

    def read_json(self, path):
        with open(path) as fh:
            return json.load(fh)

    def cli(self, *argv):
        with redirect_stdout(io.StringIO()) as out:
            rc = cli.main(list(argv))
        return rc, out.getvalue()

    def test_on_and_off_keep_the_rest_of_alerts_json_and_settings(self):
        self.alerts({"lang": "es", "nudge": {"idle_min": 30}})
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"PreCompact": [{"matcher": "manual", "hooks": [{"type": "command", "command": "mine"}]}]}}, fh)
        _rc, out = self.cli("idle-guard-on")
        self.assertIn("idle guard: on", out)
        self.assertIn("note: nothing is held back without the resume-warning hook", out)
        self.assertEqual(self.read_json(os.path.join(self.data, "alerts.json")),
                         {"lang": "es", "nudge": {"idle_min": 30, "block": True}})
        entries = self.read_json(self.settings)["hooks"]["PreCompact"]
        self.assertEqual([e["matcher"] for e in entries], ["manual", "auto"])
        self.assertTrue(entries[1]["hooks"][0]["command"].endswith(" hook pre-compact"))
        self.assertIn("pre-compact hook: already installed", self.cli("idle-guard-on")[1])
        idleguard.hold(self.payload(), "t")
        _rc, out = self.cli("idle-guard-off")
        self.assertIn("pre-compact hook [global]: removed", out)
        self.assertIn("removed %s" % idleguard.path(), out)
        self.assertEqual(self.read_json(os.path.join(self.data, "alerts.json")), {"lang": "es", "nudge": {"idle_min": 30}})
        self.assertEqual(self.read_json(self.settings)["hooks"]["PreCompact"], entries[:1])
        self.assertEqual(self.cli("idle-guard-off")[1], "idle guard: off\n")

    def test_pre_compact_hook_install_shapes(self):
        self.assertIn("new file", idlecompact.install(self.settings, kit_bin="/k/bin/sessioncoach"))
        self.assertTrue(idlecompact.installed(self.settings))
        self.assertTrue(os.path.exists(self.settings))
        self.assertEqual(idlecompact.uninstall(self.settings), "removed")
        self.assertNotIn("PreCompact", self.read_json(self.settings)["hooks"])
        self.assertEqual(idlecompact.uninstall(self.settings), "not installed")
        self.assertIn("settings backup", idlecompact.install(self.settings))
        self.assertFalse(idlecompact.installed(os.path.join(self.home, "missing.json")))
        self.assertEqual(idlecompact.our_commands({"hooks": {"PreCompact": "odd"}}), [])
        self.assertFalse(nudge.installed(self.settings))  # never taken for the other hooks
        self.assertFalse(resume.installed(self.settings))

    def check(self):
        return {n: (s, d) for s, n, d in installer.check(cwd=self.cwd) if n.startswith(("idle guard", "pre-compact"))}

    def test_check_says_off_or_what_is_missing(self):
        self.assertEqual(self.check(), {"idle guard": (installer.OK, "off (opt-in: sessioncoach idle-guard-on)")})
        self.alerts({"nudge": {"block": True}})
        warns = [d for s, n, d in installer.check(cwd=self.cwd) if n == "idle guard"]
        self.assertEqual(len(warns), 3)
        self.assertTrue(all(w.startswith("on, but ") for w in warns))
        nudge.install(self.settings, kit_bin="/k/bin/sessioncoach")
        resume.install(self.settings, kit_bin="/k/bin/sessioncoach")
        idlecompact.install(self.settings, kit_bin="/k/bin/sessioncoach")
        c = self.check()
        self.assertEqual(c["idle guard"][0], installer.OK)
        self.assertIn(c["pre-compact hook [global]"][0], (installer.OK, installer.BAD))
        idlecompact.uninstall(self.settings)
        idlecompact.install(self.settings, kit_bin="/nonexistent/bin/sessioncoach")
        self.assertEqual(self.check()["pre-compact hook [global]"][0], installer.BAD)

    def setup_run(self, yes, answers=None):
        out = []
        it = iter(answers or [])
        installer.setup(yes=yes, scope="global", kit_bin="/k/bin/sessioncoach", load=False, out=out.append,
                        inp=lambda q: next(it))
        return "\n".join(out)

    def test_setup_asks_last_never_enables_with_yes_and_needs_the_prompt_hook(self):
        out = self.setup_run(True)
        self.assertIn("idle guard: skipped with --yes", out)
        self.assertIn("enable with: sessioncoach idle-guard-on", out)
        self.assertGreater(out.index("Idle guard"), out.index("Load the handoff after /clear"))
        self.assertIn(nudge.BLOCK_VALUE[:40], out.replace("\n  ", " "))
        self.assertFalse(idleguard.enabled())
        nudge.uninstall(self.settings)
        out = self.setup_run(False, ["n", "n", "n", "n"])  # status line, hook (no), /handoff, resume hook
        self.assertIn("idle guard: skipped — it works through the resume-warning hook", out)
        out = self.setup_run(False, ["n", "y", "n", "n", "y"])
        self.assertIn("idle guard: on (alerts.json → nudge.block); pre-compact hook: installed", out)
        self.assertTrue(idleguard.enabled())
        self.assertTrue(idlecompact.installed(self.settings))
        self.assertIn("idle guard: already on", self.setup_run(True))

    def test_uninstall_all_turns_it_off_and_removes_its_files(self):
        self.assertNotIn("idle guard: off", installer.uninstall_all(cwd=self.cwd, load=False))
        self.assertFalse(os.path.exists(os.path.join(self.data, "alerts.json")))  # untouched when never set
        installer.set_idle_guard(True, self.settings, "/k/bin/sessioncoach")
        idleguard.hold(self.payload(), "t")
        out = installer.uninstall_all(cwd=self.cwd, load=False)
        self.assertIn("pre-compact hook [global]: removed", out)
        self.assertIn("idle guard: off", out)
        self.assertIn("removed %s" % idleguard.path(), out)
        self.assertFalse(idleguard.enabled())
        self.assertFalse(idlecompact.installed(self.settings))


if __name__ == "__main__":
    unittest.main()
