import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate

from sessioncoach import cli, handoff, installer, paths


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Command(unittest.TestCase):
    def setUp(self):
        self.home = isolate.claude_home(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["KIT_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["KIT_SCHEDULER"] = "none"
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        self.path = os.path.join(self.home, ".claude", "commands", "handoff.md")

    def test_the_command_asks_for_the_handoff_and_ends_with_clear(self):
        text = handoff.render()
        self.assertTrue(text.startswith("---\ndescription: "))
        for part in (handoff.MARK, "$ARGUMENTS", "docs/HANDOFF-<today, YYYY-MM-DD>-<topic", "exactly: /clear",
                     "Next steps", "How to verify", "Files touched", "«read <path of the handoff> and continue», all of it translated"):
            self.assertIn(part, text)
        with_hook = handoff.render(resume_hook=True)
        self.assertNotIn("and continue»", with_hook)
        self.assertIn("gives the new session this handoff by itself", with_hook)

    def test_install_goes_next_to_the_settings_of_the_scope(self):
        self.assertEqual(handoff.command_path(), self.path)
        proj = os.path.join(self.tmp, "proj", ".claude", "settings.local.json")
        self.assertEqual(handoff.command_path(proj), os.path.join(self.tmp, "proj", ".claude", "commands", "handoff.md"))
        self.assertEqual(handoff.state(self.path), "missing")
        self.assertIn("installed: /handoff", handoff.install())
        self.assertEqual(read(self.path), handoff.render())
        self.assertEqual(handoff.state(self.path), "current")
        self.assertIn("already installed", handoff.install())

    def test_a_handoff_command_of_your_own_is_never_touched(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as fh:
            fh.write("my own handoff\n")
        self.assertEqual(handoff.state(self.path), "foreign")
        self.assertFalse(handoff.installed())
        self.assertIn("skipped", handoff.install())
        self.assertIn("left alone", handoff.uninstall())
        self.assertIsNone(handoff.refresh(self.settings, True))
        self.assertEqual(read(self.path), "my own handoff\n")

    def test_stale_copy_is_rewritten_with_a_backup_and_refresh_follows_the_resume_hook(self):
        handoff.install()
        self.assertEqual(handoff.state(self.path, resume_hook=True), "stale")
        self.assertIn("updated", handoff.refresh(self.settings, True))
        self.assertEqual(read(self.path), handoff.render(True))
        self.assertEqual(read(self.path + ".bak-handoff"), handoff.render(False))
        self.assertIn("removed", handoff.uninstall())
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(handoff.uninstall(), "not installed")

    def test_cost_note_uses_the_list_price(self):
        self.assertAlmostEqual(handoff.rewrite_cost(1000000, "claude-opus-5-5"), 2 * handoff.rewrite_cost(500000))
        self.assertIn("$%.2f" % handoff.rewrite_cost(), handoff.cost_note())

    def test_its_version_follows_the_hooks_that_see_it_not_the_directory_check_runs_from(self):
        from sessioncoach import resume
        proj = os.path.join(self.tmp, "proj")
        psettings = installer.settings_for("project", proj)
        handoff.install()
        handoff.install(psettings)
        resume.install(psettings, kit_bin="/k/bin/kit")  # the hook in this project only
        installer.refresh_handoff(proj)
        self.assertEqual(read(handoff.command_path(psettings)), handoff.render(True))
        self.assertEqual(read(self.path), handoff.render(False))  # other projects see the global one without the hook
        for cwd in (proj, self.tmp):
            items = {n: s for s, n, _ in installer.check(cwd=cwd)}
            self.assertEqual(items["handoff command [global]"], installer.OK, cwd)
        resume.uninstall(psettings)
        resume.install(self.settings, kit_bin="/k/bin/kit")  # global: every project sees it
        self.assertTrue(installer.resume_visible(psettings))
        self.assertTrue(installer.resume_visible(self.settings))
        self.assertFalse(installer.resume_visible(os.path.join(self.tmp, "company", "settings.json")))

    def test_check_reports_it_and_uninstall_all_removes_it(self):
        handoff.install()
        items = {n: (s, d) for s, n, d in installer.check(cwd=self.tmp)}
        self.assertEqual(items["handoff command [global]"], (installer.OK, self.path))
        with open(self.path, "a") as fh:
            fh.write("edited\n")
        items = {n: (s, d) for s, n, d in installer.check(cwd=self.tmp)}
        self.assertEqual(items["handoff command [global]"][0], installer.WARN)
        out = installer.uninstall_all(cwd=self.tmp, load=False)
        self.assertTrue(any(line.startswith("handoff command [global]: removed") for line in out), out)
        self.assertFalse(os.path.exists(self.path))

    def test_cli_install_says_what_the_turn_costs_and_setup_installs_it_in_the_scope(self):
        lines = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: lines.append(" ".join(map(str, a)))):
            cli.main(["handoff-install"])
            cli.main(["handoff-uninstall"])
        self.assertIn("note: after a pause", lines[1])
        self.assertIn("removed", lines[2])
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj)
        out = []
        rc = installer.setup(yes=True, scope="project", cwd=proj, kit_bin="/k/bin/kit", load=False, out=out.append)
        self.assertEqual(rc, 0, out)
        cmd = os.path.join(proj, ".claude", "commands", "handoff.md")
        self.assertFalse(os.path.exists(cmd))  # --yes never writes into .claude/commands, which git tracks
        self.assertIn("handoff command: skipped with --yes in project scope", "\n".join(out))
        out = []
        installer.setup(scope="project", cwd=proj, kit_bin="/k/bin/kit", load=False, out=out.append, inp=lambda q: "")
        self.assertTrue(os.path.exists(cmd))
        text = "\n".join(out)
        self.assertIn("goes to git", text)
        self.assertLess(text.index("Pick up a paused session"), text.index("handoff command: installed"))
        self.assertEqual(set(json.loads(read(os.path.join(proj, ".claude", "settings.local.json")))["hooks"]),
                         {"UserPromptSubmit", "SessionStart"})
        # installed before the resume hook, then rewritten for it: no line to paste after /clear
        self.assertEqual(read(os.path.join(proj, ".claude", "commands", "handoff.md")), handoff.render(resume_hook=True))
        self.assertFalse(paths.data_dir().startswith(self.home))


if __name__ == "__main__":
    unittest.main()
