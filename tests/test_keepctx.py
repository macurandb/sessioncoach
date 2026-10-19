"""/keep-context: marking a session so the PreCompact hook blocks Claude Code's idle summary for it (synthetic data only)."""
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

from sessioncoach import cli, idlecompact, installer, keepctx, nudge


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Base(unittest.TestCase):
    def setUp(self):
        self.home = isolate.claude_home(self)
        os.environ["KIT_DATA_DIR"] = os.path.join(self.home, "data")
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        self.pdir = os.path.join(self.home, ".claude", "projects", "-p-demo")
        os.makedirs(self.pdir)

    def transcript(self, minutes_ago, sid="s1"):
        p = os.path.join(self.pdir, sid + ".jsonl")
        with open(p, "w") as fh:
            ts = iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago))
            usage = {"input_tokens": 0, "cache_read_input_tokens": 250000}
            fh.write(json.dumps({"type": "assistant", "timestamp": ts,
                                 "message": {"id": "m1", "model": "claude-opus-5-5", "usage": usage}}) + "\n")
        return p

    def expand(self, args="", sid="s1", name="keep-context"):
        d = {"session_id": sid, "hook_event_name": "UserPromptExpansion", "expansion_type": "slash_command",
             "command_name": name, "command_args": args, "prompt": ("/%s %s" % (name, args)).strip()}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(d))), redirect_stdout(io.StringIO()) as out:
            keepctx.hook_main()
        return out.getvalue()

    def precompact(self, path, trigger="auto", sid="s1"):
        d = {"session_id": sid, "transcript_path": path, "cwd": self.home, "hook_event_name": "PreCompact", "trigger": trigger}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(d))), redirect_stdout(io.StringIO()) as out:
            idlecompact.hook_main()
        return out.getvalue()


class Marking(Base):
    def test_the_command_marks_the_session_without_the_model_and_off_undoes_it(self):
        out = json.loads(self.expand())
        self.assertEqual(out["decision"], "block")  # the expansion never reaches the model
        self.assertIn("/keep-context", out["reason"])
        self.assertTrue(keepctx.kept("s1"))
        self.assertFalse(keepctx.kept("s2"))
        with open(keepctx.path()) as fh:
            self.assertEqual(set(json.load(fh)["entries"][0]), {"session_id", "at"})
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(keepctx.path()).st_mode), 0o600)
        self.assertIn("off", json.loads(self.expand(" OFF "))["reason"])
        self.assertFalse(keepctx.kept("s1"))

    def test_spanish_when_claude_code_is_set_to_spanish(self):
        os.makedirs(os.path.dirname(self.settings), exist_ok=True)
        with open(self.settings, "w") as fh:
            json.dump({"language": "spanish"}, fh)
        self.assertIn("conserva todo su contexto", self.expand())

    def test_spanish_when_alerts_json_says_so(self):
        # it passed {} before and never read alerts.json `lang`; now the same order as every other message
        os.makedirs(os.environ["KIT_DATA_DIR"], exist_ok=True)
        with open(os.path.join(os.environ["KIT_DATA_DIR"], "alerts.json"), "w", encoding="utf-8") as fh:
            json.dump({"lang": "es"}, fh)
        self.assertIn("conserva todo su contexto", self.expand())

    def test_other_commands_or_no_session_print_nothing_and_errors_stay_quiet(self):
        self.assertEqual(self.expand(name="handoff"), "")
        self.assertEqual(self.expand(sid=""), "")
        self.assertFalse(os.path.exists(keepctx.path()))
        with mock.patch.object(keepctx, "keep", side_effect=OSError("read-only")):
            self.assertEqual(self.expand(), "")
        with mock.patch.object(sys, "stdin", io.StringIO("not json")), redirect_stdout(io.StringIO()) as out:
            keepctx.hook_main()
        self.assertEqual(out.getvalue(), "")

    def test_marks_expire_are_bounded_and_survive_a_corrupt_file(self):
        now = time.time()
        keepctx.keep("old", now=now - keepctx.MAX_AGE_S - 10)
        self.assertFalse(keepctx.kept("old", now=now))
        for i in range(keepctx.MAX_ENTRIES + 5):
            keepctx.keep("s%d" % i, now=now)
        self.assertEqual(len(keepctx.load()), keepctx.MAX_ENTRIES)
        with open(keepctx.path(), "w") as fh:
            fh.write("{broken")
        self.assertFalse(keepctx.kept("s60"))
        self.assertEqual(keepctx.load(), [])
        self.assertTrue(keepctx.remove())
        self.assertFalse(keepctx.remove())

    def test_the_prompt_hook_never_holds_it_back_nor_the_return_of_a_kept_session(self):
        self.assertTrue("/keep-context off".startswith(nudge.WAYS_OUT))
        info = {"idle_s": 4000}
        self.assertTrue(nudge.holds({"block": True}, info, {"session_id": "s1"}))
        self.expand()
        self.assertFalse(nudge.holds({"block": True}, info, {"session_id": "s1"}))
        self.assertTrue(nudge.holds({"block": True}, info))


class PreCompact(Base):
    def test_blocks_only_the_idle_summary_of_a_kept_session(self):
        idle = self.transcript(55)
        self.assertEqual(self.precompact(idle), "")  # not kept: recorded, never blocked
        self.assertTrue(idlecompact.pending("s1", idlecompact.load()[0]["last_ts"]))
        idlecompact.remove()
        self.expand()
        self.assertEqual(json.loads(self.precompact(idle))["decision"], "block")
        self.assertEqual(idlecompact.load(), [])  # a blocked summary is not pending
        busy = self.transcript(2)  # the context is full while working: never blocked
        self.assertEqual(self.precompact(busy), "")
        self.assertEqual(self.precompact(idle, trigger="manual"), "")
        self.expand("off")
        self.assertEqual(self.precompact(self.transcript(55)), "")


    def test_the_hook_reads_stdin_as_utf8_on_an_ansi_console(self):
        self.pdir = os.path.join(self.home, "José")  # a Windows user name outside ASCII
        os.makedirs(self.pdir)
        tp = self.transcript(55)
        d = {"session_id": "s1", "transcript_path": tp, "hook_event_name": "PreCompact", "trigger": "auto"}
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(d, ensure_ascii=False).encode("utf-8")), encoding="cp1252")
        with mock.patch.object(sys, "stdin", stdin), redirect_stdout(io.StringIO()):
            idlecompact.hook_main()
        self.assertEqual([e["transcript_path"] for e in idlecompact.load()], [tp])


class Install(Base):
    def test_idle_guard_on_installs_it_and_off_and_uninstall_remove_it(self):
        kit = "/k/bin/sessioncoach"
        lines = installer.set_idle_guard(True, self.settings, kit)
        self.assertTrue(keepctx.installed(self.settings))
        cp = keepctx.command_path(self.settings)
        with open(cp) as fh:
            self.assertIn(keepctx.MARK, fh.read())
        self.assertIn("/keep-context", lines[-1])
        self.assertIn("hook already installed", keepctx.install(self.settings, kit))
        with open(self.settings) as fh:
            entry, = json.load(fh)["hooks"]["UserPromptExpansion"]
        self.assertEqual(entry["matcher"], "keep-context")
        self.assertIn("hook keep-context", entry["hooks"][0]["command"])
        self.expand()
        out = "\n".join(installer.set_idle_guard(False))
        self.assertIn("keep-context [global]: hook removed; removed", out)
        self.assertFalse(os.path.exists(cp) or os.path.exists(keepctx.path()))
        with open(self.settings) as fh:
            self.assertNotIn("UserPromptExpansion", json.load(fh).get("hooks", {}))
        installer.set_idle_guard(True, self.settings, kit)
        self.assertIn("keep-context [global]", "\n".join(installer.uninstall_all()))
        self.assertFalse(keepctx.installed(self.settings))

    def test_an_own_command_file_and_other_expansion_hooks_are_left_alone(self):
        cp = keepctx.command_path(self.settings)
        os.makedirs(os.path.dirname(cp))
        with open(cp, "w") as fh:
            fh.write("my own\n")
        os.makedirs(os.path.dirname(self.settings), exist_ok=True)
        mine = {"matcher": "deploy", "hooks": [{"type": "command", "command": "check-deploy"}]}
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"UserPromptExpansion": [mine]}}, fh)
        self.assertIn("command skipped", keepctx.install(self.settings, "/k/bin/sessioncoach"))
        self.assertEqual(keepctx.uninstall(self.settings), "hook removed")
        with open(cp) as fh:
            self.assertEqual(fh.read(), "my own\n")
        with open(self.settings) as fh:
            self.assertEqual(json.load(fh)["hooks"]["UserPromptExpansion"], [mine])
        self.assertEqual(keepctx.uninstall(self.settings), "not installed")
        self.assertIsNone(keepctx.plan_uninstall({"hooks": {"UserPromptExpansion": "odd"}}))
        self.assertFalse(keepctx.installed(os.path.join(self.home, "nowhere", "settings.json")))

    def test_check_shows_the_hook_and_the_cli_dispatches_it(self):
        installer.set_idle_guard(True, self.settings, "/k/bin/sessioncoach")
        text = "\n".join("%s %s" % (n, d) for s, n, d in installer.check(cwd=self.home))
        self.assertIn("keep-context hook [global]", text)
        with mock.patch.object(keepctx, "hook_main") as hm:
            cli.main(["hook", "keep-context"])
        hm.assert_called_once()


if __name__ == "__main__":
    unittest.main()
