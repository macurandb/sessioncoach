"""The hooks' settings.json entries (hooks.py): odd shapes, old commands, the registry (synthetic data only)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate

from sessioncoach import cli, hooks, idlecompact, installer, keepctx, nudge, paths, resume

KB = "/k/bin/sessioncoach"
MODULES = [nudge, resume, idlecompact, keepctx]


def odd_shapes(event):
    return [[], {"hooks": []}, {"hooks": None}, {"hooks": {event: "odd"}}, {"hooks": {event: ["x"]}},
            {"hooks": {event: [{"hooks": "x"}]}}, {"hooks": {event: [{"hooks": ["x", None]}]}},
            {"hooks": {event: [{"hooks": [{"command": None}]}]}}]


class HooksTest(unittest.TestCase):
    def setUp(self):
        self.home = isolate.claude_home(self)
        self.dir = os.path.join(self.home, ".claude")
        os.makedirs(self.dir, exist_ok=True)

    def write(self, settings, name="settings.json"):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(settings, fh)
        return p

    def read(self, p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def test_odd_shapes_count_as_not_installed_in_every_hook(self):
        for mod in MODULES:
            ev = mod.SPEC.event
            for s in odd_shapes(ev):
                with self.subTest(hook=mod.SPEC.arg, settings=s):
                    p = self.write(s)
                    self.assertEqual(mod.our_commands(s), [])
                    self.assertFalse(mod.installed(p))
                    self.assertIsNone(mod.plan_uninstall(s))
                    self.assertIn("not installed", mod.uninstall(p))
                    self.assertEqual(self.read(p), s)  # left as it was
                    if hasattr(mod, "installed_in"):
                        self.assertFalse(mod.installed_in(s))

    def test_odd_entries_are_kept_and_ours_added_next_to_them(self):
        for mod in MODULES:
            ev = mod.SPEC.event
            for s in [{"hooks": {ev: ["x"]}}, {"hooks": {ev: [{"hooks": "x"}]}}]:
                with self.subTest(hook=mod.SPEC.arg, settings=s):
                    p = self.write(s)
                    self.assertIn("installed", mod.install(p, KB))
                    self.assertTrue(mod.installed(p))
                    self.assertEqual(self.read(p)["hooks"][ev][0], s["hooks"][ev][0])
                    self.assertIn("removed", mod.uninstall(p))
                    self.assertEqual(self.read(p), s)

    def test_a_container_of_the_wrong_type_is_not_replaced(self):
        for mod in MODULES:
            ev = mod.SPEC.event
            for s in [[], {"hooks": []}, {"hooks": None}, {"hooks": {ev: "odd"}}, {"hooks": {ev: {}}}]:
                with self.subTest(hook=mod.SPEC.arg, settings=s):
                    p = self.write(s)
                    with self.assertRaisesRegex(ValueError, "fix it by hand"):
                        mod.install(p, KB)
                    self.assertEqual(self.read(p), s)

    def test_old_commands_are_rewritten_in_place_by_every_hook(self):
        for mod in MODULES:
            spec = mod.SPEC
            for old in ("/usr/bin/python3 /old/bin/kit hook %s" % spec.arg, "/usr/bin/python3 -m kit hook %s" % spec.arg):
                with self.subTest(hook=spec.arg, old=old):
                    foreign = {"type": "command", "command": "echo mine", "timeout": 9}
                    entry = {"matcher": "m", "hooks": [{"type": "command", "command": old, "timeout": 7}, foreign]}
                    p = self.write({"hooks": {spec.event: [entry]}, "model": "x"})
                    self.assertTrue(mod.installed(p))
                    self.assertIn("updated to the current command (settings backup %s%s)" % (p, spec.backup_suffix),
                                  mod.install(p, KB))
                    new = self.read(p)
                    self.assertEqual(new["hooks"][spec.event], [{"matcher": "m", "hooks": [
                        {"type": "command", "command": mod._command(KB), "timeout": 7}, foreign]}])
                    self.assertEqual(new["model"], "x")
                    self.assertIn("already installed", mod.install(p, KB))
                    self.assertIn("removed", mod.uninstall(p))  # only ours goes: the user's hook keeps its entry
                    self.assertEqual(self.read(p)["hooks"][spec.event], [{"matcher": "m", "hooks": [foreign]}])

    def test_uninstall_keeps_other_hooks_of_the_entry_and_drops_only_emptied_ones(self):
        for mod in MODULES:
            spec = mod.SPEC
            with self.subTest(hook=spec.arg):
                ours = {"type": "command", "command": mod._command(KB), "timeout": 5}
                mine = {"type": "command", "command": "echo mine"}
                s = {"hooks": {spec.event: [{"matcher": "m", "hooks": [mine, ours, "x"]}, {"hooks": [ours]},
                                            {"hooks": [mine]}], "Stop": [{"hooks": [mine]}]}}
                new = mod.plan_uninstall(s)
                self.assertEqual(new["hooks"], {spec.event: [{"matcher": "m", "hooks": [mine, "x"]}, {"hooks": [mine]}],
                                                "Stop": [{"hooks": [mine]}]})
                self.assertEqual(s["hooks"][spec.event][0]["hooks"], [mine, ours, "x"])  # the input is not touched
                only = mod.plan_uninstall({"hooks": {spec.event: [{"hooks": [ours, ours]}], "Stop": []}})
                self.assertEqual(only, {"hooks": {"Stop": []}})

    def test_the_current_command_and_other_tools_are_left_alone(self):
        for mod in MODULES:
            spec = mod.SPEC
            with self.subTest(hook=spec.arg):
                other = "/usr/bin/python3 /else/bin/sessioncoach hook %s" % spec.arg  # ours, another clone: not old
                s = {"hooks": {spec.event: [{"hooks": [{"type": "command", "command": other, "timeout": 5}]}]}}
                self.assertIsNone(mod.plan_install(s, KB))
                self.assertEqual(mod.our_commands(s), [other])

    def test_contract_written_in_settings(self):
        for mod, arg, event, matcher, suffix in ((nudge, "prompt", "UserPromptSubmit", None, ".bak-hook"),
                                                 (resume, "session-start", "SessionStart", None, ".bak-resume"),  # apps' /clear: startup
                                                 (idlecompact, "pre-compact", "PreCompact", "auto", ".bak-precompact"),
                                                 (keepctx, "keep-context", "UserPromptExpansion", "keep-context", ".bak-keepctx")):
            with self.subTest(hook=arg):
                entry = mod.plan_install({}, KB)["hooks"][event]
                want = {"hooks": [{"type": "command", "command": mod._command(KB), "timeout": 5}]}
                self.assertEqual(entry, [dict({"matcher": matcher}, **want) if matcher else want])
                self.assertEqual(list(entry[0]), ["matcher", "hooks"] if matcher else ["hooks"])
                self.assertTrue(mod._command(KB).endswith("sessioncoach hook " + arg))
                self.assertEqual((mod.HOOK_EVENT, mod.SPEC.backup_suffix), (event, suffix))
                self.assertTrue(mod.is_ours("/usr/bin/python3 -m kit hook " + arg))

    def test_registry(self):
        self.assertEqual([s.arg for s in hooks.HOOKS], ["prompt", "session-start", "pre-compact", "keep-context"])
        self.assertEqual([s.module() for s in hooks.HOOKS], MODULES)
        self.assertEqual([m.SPEC for m in MODULES], list(hooks.HOOKS))
        self.assertEqual(repr(hooks.PROMPT), "HookSpec('prompt')")
        self.assertEqual(paths.user_settings(), os.path.join(paths.HOME, ".claude", "settings.json"))
        self.assertEqual(hooks.KEEP_CONTEXT.check_label, "keep-context hook")
        self.assertEqual(hooks.SESSION_START.check_label, "resume hook")

    def test_uninstall_all_goes_past_an_odd_settings_file(self):
        odd = os.path.join(self.home, "cfg")
        os.makedirs(odd)
        with open(os.path.join(odd, "settings.json"), "w") as fh:
            json.dump({"hooks": {"UserPromptSubmit": ["x"], "SessionStart": "odd"}}, fh)
        nudge.install(kit_bin=KB)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": odd, "KIT_DATA_DIR": os.path.join(self.home, "data")}):
            out = installer.uninstall_all(cwd=self.home, agents_dir=os.path.join(self.home, "agents"), load=False)
        self.assertIn("hook [global]: removed", out)
        self.assertFalse(nudge.installed())

    def test_the_cli_runs_each_hook_from_the_registry(self):
        for mod in MODULES:
            with self.subTest(hook=mod.SPEC.arg), mock.patch.object(mod, "hook_main") as hm:
                cli.main(["hook", mod.SPEC.arg])
                hm.assert_called_once_with()
        with self.assertRaises(SystemExit):
            cli.main(["hook", "nope"])

    def test_default_path_is_the_user_settings(self):
        for mod in MODULES:
            with self.subTest(hook=mod.SPEC.arg):
                self.assertFalse(mod.installed())
                self.assertIn("installed", mod.install(kit_bin=KB))
                self.assertTrue(mod.installed(paths.user_settings()))
                self.assertIn("removed", mod.uninstall())


if __name__ == "__main__":
    unittest.main()
