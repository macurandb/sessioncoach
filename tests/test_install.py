import datetime as dt
import json
import os
import plistlib
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: F401  (drops SESSIONCOACH_* left in the shell)

from sessioncoach import handoff, installer, limits, nudge, paths, resume, review, store
from sessioncoach import settings as settings_mod

EXISTING = sys.executable  # a file on every OS: check verifies that the script in setup's commands exists

# kit writes /usr/bin/python3 into commands when it is Python 3.9+ (and not the macOS stub), else the running
# interpreter; on Windows commands have their own form (test_scheduler.py), and file modes (600/700) do not apply
PY = paths._python()
POSIX = sys.platform != "win32"
posix_only = unittest.skipUnless(POSIX, "POSIX command form / file modes")


def read_text(path):
    with open(path) as fh:
        return fh.read()


def read_json(path):
    with open(path) as fh:
        return json.load(fh)


def read_plist(path):
    with open(path, "rb") as fh:
        return plistlib.load(fh)


def write_text(path, text):
    with open(path, "w") as fh:
        fh.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = {k: os.environ.get(k) for k in ("KIT_DATA_DIR", "CLAUDE_CONFIG_DIR", "KIT_PROJECTS_DIRS", "KIT_SCHEDULER")}
        self.home = paths.HOME
        paths.HOME = os.path.join(self.tmp, "home")
        os.environ["KIT_DATA_DIR"] = os.path.join(self.tmp, "kit")
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("KIT_PROJECTS_DIRS", None)
        os.environ["KIT_SCHEDULER"] = "launchd"  # these tests describe the macOS LaunchAgent files; other schedulers: test_scheduler.py
        os.makedirs(os.path.join(paths.HOME, ".claude"))
        self.settings = os.path.join(paths.HOME, ".claude", "settings.json")
        self.agents = os.path.join(self.tmp, "agents")
        self.cwd = os.path.join(self.tmp, "proj")
        os.makedirs(self.cwd)

    def tearDown(self):
        paths.HOME = self.home
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_settings(self, d, path=None):
        with open(path or self.settings, "w") as fh:
            json.dump(d, fh)


class KitCommand(Base):
    @posix_only
    def test_explicit_bin_keeps_old_format(self):
        self.assertEqual(paths.kit_command_str("hook", "prompt", kit_bin="/k/bin/kit"), PY + " /k/bin/kit hook prompt")

    # renamed from daf-claude-kit: new installs write bin/sessioncoach / -m sessioncoach (were bin/kit / -m kit)
    def test_clone_uses_bin_sessioncoach(self):
        self.assertTrue(paths.kit_command()[-1].endswith(os.path.join("bin", "sessioncoach")))

    def test_installed_package_uses_its_console_script_else_python_dash_c(self):
        # was `python -m sessioncoach`, which imports a sessioncoach.py from the project Claude Code runs the hook in
        real = os.path.isfile
        try:
            os.path.isfile = lambda p: False if str(p).endswith(os.path.join("bin", "sessioncoach")) else real(p)
            with mock.patch.object(paths, "console_script", return_value="/v/bin/sessioncoach"):
                self.assertEqual(paths.kit_command(), ["/v/bin/sessioncoach"])
                self.assertEqual(paths.kit_command_str("hook", "prompt"), "/v/bin/sessioncoach hook prompt")
            with mock.patch.object(paths, "console_script", return_value=None):
                self.assertEqual(paths.kit_command(), [sys.executable, "-c", paths.RUN_CODE])
                cmd = paths.kit_command_str("hook", "prompt")
                self.assertTrue(nudge.is_ours(cmd), cmd)
                self.assertIsNone(paths.old_command_name(cmd))
                self.assertIsNone(installer.command_problem(cmd))
        finally:
            os.path.isfile = real

    @posix_only
    def test_usr_bin_python3_only_when_it_is_python_3_9_or_later(self):
        def fake_python(ver):
            p = os.path.join(self.tmp, "python3-%s" % ver.replace(" ", "."))
            write_text(p, "#!/bin/sh\necho %s\n" % ver)
            os.chmod(p, 0o755)
            return p
        old, new = fake_python("3 6"), fake_python("3 12")
        with mock.patch.dict(paths._VERSIONS, clear=True):
            for stable, want in ((old, sys.executable), (new, new), (os.path.join(self.tmp, "none"), sys.executable)):
                with self.subTest(stable=stable), mock.patch.object(paths, "STABLE_PYTHON", stable):
                    self.assertEqual(paths._python(), want)
                    self.assertEqual(paths.kit_command("/k/bin/sessioncoach")[0], want)
            self.assertEqual(paths.python_version(old), (3, 6))
            self.assertEqual(paths.python_version(sys.executable), tuple(sys.version_info[:2]))
            self.assertIsNone(paths.python_version(os.path.join(self.tmp, "none")))
            bad = fake_python("odd")
            self.assertIsNone(paths.python_version(bad))
            self.assertIn("is Python 3.6; sessioncoach needs 3.9 or later",
                          installer.command_problem("%s %s hook prompt" % (old, EXISTING)))
            self.assertIsNone(installer.command_problem("%s %s hook prompt" % (new, EXISTING)))
            self.assertIsNone(installer.command_problem("%s %s hook prompt" % (bad, EXISTING)))  # unknown: not flagged

    def test_macos_stub_is_never_run_nor_written(self):
        def no_run(*_a, **_k):
            raise AssertionError("the macOS stub must not be run")
        with mock.patch.dict(paths._VERSIONS, clear=True), mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.dict(os.environ, {"DEVELOPER_DIR": ""}), mock.patch("subprocess.run", no_run), \
                mock.patch.object(paths, "STABLE_PYTHON", "/usr/bin/python3"), \
                mock.patch.object(paths.os.path, "exists", lambda p: p == "/usr/bin/python3"), \
                mock.patch.object(paths.os.path, "islink", lambda p: False):
            self.assertTrue(paths.mac_stub("/usr/bin/python3"))
            self.assertIsNone(paths.python_version("/usr/bin/python3"))
            self.assertEqual(paths._python(), sys.executable)
            self.assertIn("macOS stub", installer.command_problem("/usr/bin/python3 -m sessioncoach hook prompt"))
        dev = os.path.join(self.tmp, "dev")
        os.makedirs(os.path.join(dev, "usr", "bin"))
        write_text(os.path.join(dev, "usr", "bin", "python3"), "")
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.dict(os.environ, {"DEVELOPER_DIR": dev}):
            self.assertFalse(paths.mac_stub("/usr/bin/python3"))  # developer tools present: the shim runs them
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(paths.mac_stub("/usr/bin/python3"))

    def test_dash_c_status_line_gives_its_chain_file_back(self):
        # uninstall finds the scope's chain file in the command: the -c form must not hide it
        real = os.path.isfile
        try:
            os.path.isfile = lambda p: False if str(p).endswith(os.path.join("bin", "sessioncoach")) else real(p)
            with mock.patch.object(paths, "console_script", return_value=None):
                for win, chain in ((False, "/a b/c.sh"), (True, "C:/a b/c.sh")):
                    with self.subTest(win=win), mock.patch.object(paths, "is_windows", return_value=win):
                        cmd = paths.kit_command_str("statusline", "--chain-file", chain)
                        self.assertIn("-c", paths.split_command(cmd))
                        self.assertEqual(limits.installed_chain({"statusLine": {"type": "command", "command": cmd}}), chain)
        finally:
            os.path.isfile = real

    def test_console_script_is_looked_for_next_to_the_interpreter(self):
        d = os.path.join(self.tmp, "scripts")
        os.makedirs(d)
        name = "sessioncoach.exe" if sys.platform == "win32" else "sessioncoach"
        with mock.patch("sysconfig.get_path", return_value=d), mock.patch.object(sys, "executable", os.path.join(self.tmp, "py")):
            self.assertIsNone(paths.console_script())
            write_text(os.path.join(d, name), "")
            self.assertEqual(paths.console_script(), os.path.join(d, name))
        self.assertIsNone(installer.command_problem("%s hook prompt" % os.path.join(d, name)))
        self.assertIn("interpreter not found", installer.command_problem("/nonexistent/bin/sessioncoach hook prompt"))

    def test_windowless_job_on_windows_skips_the_console_script(self):
        real = os.path.isfile
        try:
            os.path.isfile = lambda p: False if str(p).endswith(os.path.join("bin", "sessioncoach")) else real(p)
            with mock.patch.object(paths, "console_script", return_value="C:/v/Scripts/sessioncoach.exe"), \
                    mock.patch.object(paths, "is_windows", return_value=True):
                self.assertEqual(paths.kit_command()[0], "C:/v/Scripts/sessioncoach.exe")
                self.assertEqual(paths.kit_command(windowless=True)[1:], ["-c", paths.RUN_CODE])
        finally:
            os.path.isfile = real

    def test_dash_c_launcher_ignores_a_sessioncoach_in_the_working_directory(self):
        import subprocess
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proj = os.path.join(self.tmp, "hostile")
        os.makedirs(os.path.join(proj, "sessioncoach"))
        marker = os.path.join(self.tmp, "hijacked")
        evil = "open(%r, 'w').close()\n" % marker
        write_text(os.path.join(proj, "sessioncoach.py"), evil)
        write_text(os.path.join(proj, "sessioncoach", "__init__.py"), evil)
        write_text(os.path.join(proj, "sessioncoach", "cli.py"), evil + "def main():\n    return 0\n")
        env = dict(os.environ, PYTHONPATH=repo, HOME=paths.HOME, KIT_DATA_DIR=os.path.join(self.tmp, "kit"))
        env.pop("PYTHONSAFEPATH", None)  # set, -m would not show the hijack this guards against
        out = subprocess.run([sys.executable, "-c", paths.RUN_CODE, "--help"], cwd=proj, env=env, capture_output=True,
                             text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("sessioncoach", out.stdout)
        self.assertFalse(os.path.exists(marker))
        # what the old `-m sessioncoach` did in that project
        subprocess.run([sys.executable, "-m", "sessioncoach", "--help"], cwd=proj, env=env, capture_output=True, check=False)
        self.assertTrue(os.path.exists(marker))

    def test_python_dash_m_sessioncoach_is_upgraded_in_place(self):
        old = "/usr/bin/python3 -m sessioncoach hook prompt"
        s = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": old, "timeout": 5}]}]}}
        new = nudge.plan_install(s, "/k/bin/sessioncoach")
        self.assertEqual(new["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
                         paths.kit_command_str("hook", "prompt", kit_bin="/k/bin/sessioncoach"))
        sl = {"statusLine": {"type": "command", "command": "/usr/bin/python3 -m sessioncoach statusline --chain-file /c.sh"}}
        new, chain_text = limits.plan_install(sl, "/other.sh", "/k/bin/sessioncoach")
        self.assertIsNone(chain_text)
        self.assertEqual(limits.installed_chain(new), "/c.sh")
        self.assertNotIn("-m sessioncoach", new["statusLine"]["command"])

    def test_legacy_bin_kit_still_runs(self):
        import subprocess
        bin_kit = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "kit")
        out = subprocess.run([sys.executable, bin_kit, "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("sessioncoach", out.stdout)

    def test_commands_recognised_under_every_name(self):
        for cmd in ("/usr/bin/python3 /old/bin/kit hook prompt", "C:/Py/python.exe -m kit hook prompt",
                    "/usr/bin/python3 /new/bin/sessioncoach hook prompt", "/usr/bin/python3 -m sessioncoach hook prompt"):
            self.assertTrue(nudge._is_ours(cmd), cmd)
            self.assertTrue(limits._is_ours(cmd.replace("hook prompt", "statusline --chain-file /c.sh")), cmd)
        self.assertFalse(nudge._is_ours("/usr/bin/python3 /x/other-tool hook prompt"))

    def test_mentions_us_matches_whole_words_of_every_shape_ever_written(self):
        ours = ["/usr/bin/python3 /path/bin/sessioncoach hook prompt", "/usr/bin/python3 /path/bin/kit hook prompt",
                "C:\\Py\\python.exe C:\\k\\bin\\kit hook prompt", "C:/Py/python.exe C:/k/bin/kit hook prompt",
                "/usr/bin/python3 -m sessioncoach hook prompt", "/usr/bin/python3 -m kit hook prompt",
                "C:/Py/python.exe -m kit hook prompt",
                "'/Users/John Smith/py/bin/python3' '/Users/John Smith/src/sessioncoach/bin/sessioncoach' hook prompt",
                '"/a b/bin/kit" hook prompt', "C:/Users/me/AppData/Local/Programs/Python/Python312/python.exe "
                "C:/Users/me/src/sessioncoach/bin/sessioncoach hook prompt",
                "/path/to/sessioncoach hook prompt", "C:\\Users\\me\\venv\\Scripts\\sessioncoach.exe hook prompt",
                "C:/Users/me/venv/Scripts/sessioncoach.exe hook prompt", "C:/Users/JOHNSM~1/Scripts/SESSIO~1.EXE hook prompt",
                "/usr/bin/python3 /x/daf-claude-kit/bin/sessioncoach hook prompt",
                "/usr/bin/python3 /Users/me/daf-claude-kit/bin/sessioncoach statusline --chain-file /Users/me/.kit/statusline-chain.sh",
                "/usr/bin/python3 /old/bin/kit statusline --chain-file /h/.kit/chains/ab12/statusline-chain.sh",
                "/usr/bin/python3 -m kit statusline --chain-file '/h/a b/.kit/statusline-chain.sh'",
                "/venv/bin/sessioncoach statusline --chain-file /h/.sessioncoach/statusline-chain.sh",
                "/usr/bin/python3 /old/bin/kit statusline --chain-file 'unterminated",
                "/venv/bin/python3 -c '%s' hook prompt" % paths.RUN_CODE,
                'C:/Py/python.exe "%s" hook prompt' % paths.RUN_CODE]
        theirs = ["/home/nikita/x.sh hook prompt", "toolkit hook prompt", "/usr/bin/toolkit hook prompt", "my-kit-tool hook prompt",
                  "kitty hook prompt", "/u/.kit/bin/kitten hook prompt", "sh ~/.kit/statusline-chain.sh hook prompt",
                  "/opt/sessioncoach/other.sh hook prompt", "sh /h/.sessioncoach/x.sh statusline --chain-file /c.sh",
                  "python3 -m kitchen hook prompt", "python3 -m sessioncoachx hook prompt", "bash ~/.claude/hooks/rtk-rewrite.sh",
                  "", None]
        for cmd in ours:
            with self.subTest(ours=cmd):
                self.assertTrue(paths.mentions_us(cmd))
                if "hook prompt" in cmd:
                    self.assertTrue(nudge.is_ours(cmd))
                    self.assertTrue(limits.is_ours(cmd.replace("hook prompt", "statusline --chain-file /c.sh")))
                else:
                    self.assertTrue(limits.is_ours(cmd))
        for cmd in theirs:
            with self.subTest(theirs=cmd):
                self.assertFalse(paths.mentions_us(cmd))
                self.assertFalse(nudge.is_ours(cmd))
                self.assertFalse(limits.is_ours((cmd or "").replace("hook prompt", "statusline --chain-file /c.sh")))

    def test_new_format_hook_not_duplicated_next_to_old(self):
        old = "/usr/bin/python3 /old/bin/kit hook prompt"
        self.write_settings({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": old}]}]}})
        # the old command is ours: it is updated in place, never duplicated next to a new one
        self.assertIn("updated to the current command", nudge.install(self.settings, kit_bin="/new/bin/sessioncoach"))
        entries = read_json(self.settings)["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(entries), 1)
        self.assertIn("/new/bin/sessioncoach hook prompt", entries[0]["hooks"][0]["command"])

    def test_old_format_hook_is_recognised_and_removable(self):
        old = "/usr/bin/python3 /old/bin/kit hook prompt"
        self.write_settings({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": old}]}]}})
        self.assertTrue(nudge.installed(self.settings))
        self.assertEqual(nudge.uninstall(self.settings), "removed")
        self.assertFalse(nudge.installed(self.settings))

    @posix_only
    def test_new_format_hook_roundtrip_and_backup_mode(self):
        self.write_settings({"model": "x"})
        nudge.install(self.settings, kit_bin="/k/bin/kit")
        self.assertEqual(stat.S_IMODE(os.stat(self.settings + ".bak-hook").st_mode), 0o600)
        self.assertEqual(nudge.install(self.settings, kit_bin="/k/bin/kit"), "already installed")
        nudge.uninstall(self.settings)
        self.assertEqual(read_json(self.settings)["model"], "x")

    @posix_only
    def test_settings_files_created_by_install_are_private_existing_ones_keep_their_mode(self):
        old = os.umask(0)
        try:
            for name, install in (("hook", lambda p: nudge.install(p, kit_bin="/k/bin/sessioncoach")),
                                  ("statusline", lambda p: limits.install(p, kit_bin="/k/bin/sessioncoach")),
                                  ("resume", lambda p: resume.install(p, kit_bin="/k/bin/sessioncoach"))):
                with self.subTest(name):
                    p = os.path.join(self.tmp, name, ".claude", "settings.json")
                    self.assertIn("installed", install(p))
                    self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
                    q = os.path.join(self.tmp, name, "shared.json")
                    write_text(q, "{}")
                    os.chmod(q, 0o644)
                    install(q)
                    self.assertEqual(stat.S_IMODE(os.stat(q).st_mode), 0o644)
        finally:
            os.umask(old)

    def test_statusline_keeps_the_users_other_keys_through_install_and_uninstall(self):
        line = {"type": "command", "command": "sh orca.sh", "padding": 2, "refreshInterval": 5}
        self.write_settings({"statusLine": line, "model": "x"})
        limits.install(self.settings, kit_bin="/k/bin/sessioncoach")
        sl = read_json(self.settings)["statusLine"]
        self.assertTrue(limits.is_ours(sl["command"]))
        self.assertEqual((sl["type"], sl["padding"], sl["refreshInterval"]), ("command", 2, 5))
        self.assertEqual(limits.uninstall(self.settings), "restored previous status line")
        self.assertEqual(read_json(self.settings), {"statusLine": line, "model": "x"})
        # none before: the whole object goes, whatever keys it had
        self.write_settings({"model": "x"})
        limits.install(self.settings, kit_bin="/k/bin/sessioncoach")
        self.assertEqual(sorted(read_json(self.settings)["statusLine"]), ["command", "type"])
        limits.uninstall(self.settings)
        self.assertEqual(read_json(self.settings), {"model": "x"})

    def test_statusline_of_an_odd_shape_is_not_replaced(self):
        for odd in ("sh x.sh", ["x"], 3):
            with self.subTest(odd=odd):
                self.write_settings({"statusLine": odd})
                with self.assertRaisesRegex(ValueError, "fix it by hand"):
                    limits.install(self.settings, kit_bin="/k/bin/sessioncoach")
                self.assertEqual(read_json(self.settings), {"statusLine": odd})
        _new, chain_text = limits.plan_install({"statusLine": {"command": 7}}, "/c.sh", "/k/bin/sessioncoach")
        self.assertEqual(chain_text, ":\n")
        self.assertEqual(limits.plan_uninstall({"statusLine": "odd"}, "sh prev.sh\n")["statusLine"],
                         {"type": "command", "command": "sh prev.sh"})

    def test_statusline_not_chained_after_itself(self):
        self.write_settings({"statusLine": {"type": "command", "command": "sh orca.sh"}})
        limits.install(self.settings, kit_bin="/k/bin/kit")
        self.assertTrue(limits.installed(self.settings))
        self.assertEqual(limits.install(self.settings, kit_bin="/k/bin/kit"), "already installed")
        # an old-format command is ours: never chained after itself, updated in place instead
        self.assertIn("updated to the current command", limits.install(self.settings, kit_bin="/new/bin/sessioncoach"))
        chain = os.path.join(paths.data_dir(), "statusline-chain.sh")
        self.assertEqual(read_text(chain).strip(), "sh orca.sh")
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(chain).st_mode), 0o600)

    def test_install_creates_missing_project_settings(self):
        p = os.path.join(self.cwd, ".claude", "settings.local.json")
        nudge.install(p, kit_bin="/k/bin/kit")
        self.assertTrue(nudge.installed(p))

    def test_review_plist_uses_kit_command(self):
        cfg = review.load_config({})
        p = plistlib.loads(review.plist(cfg, "/k/bin/kit", "/l"))
        self.assertEqual(p["ProgramArguments"], [PY, "/k/bin/kit", "review", "--scheduled"])


class Setup(Base):
    def run_setup(self, **kw):
        lines = []
        kw.setdefault("yes", True)
        rc = installer.setup(kit_bin="/k/bin/kit", agents_dir=self.agents, load=False, settings_path=self.settings,
                             out=lines.append, **kw)
        return rc, lines

    def test_yes_installs_everything_and_saves_config(self):
        self.write_settings({"statusLine": {"type": "command", "command": "sh orca.sh"}})
        rc, lines = self.run_setup(interval=15, time="07:45")
        mac = installer.is_mac()
        self.assertEqual(rc, 0, lines)
        self.assertTrue(nudge.installed(self.settings) and limits.installed(self.settings))
        if mac:
            p = read_plist(installer.ingest_plist_path(self.agents))
            self.assertEqual(p["StartInterval"], 900)
            self.assertEqual(p["ProgramArguments"], [PY, "/k/bin/kit", "ingest"])
            self.assertTrue(review.installed(self.agents))
            a = read_json(installer.alerts_path())
            self.assertEqual((a["ingest"]["interval_min"], a["review"]["time"]), (15, "07:45"))
            if POSIX:
                self.assertEqual(stat.S_IMODE(os.stat(installer.alerts_path()).st_mode), 0o600)

    def test_idempotent(self):
        self.run_setup()
        rc, lines = self.run_setup()
        self.assertEqual(rc, 0)
        self.assertIn("already installed", "\n".join(lines))

    def test_every_question_comes_after_what_the_piece_gives(self):
        from sessioncoach import settings as settings_mod
        values = {"Ingest": installer.INGEST_VALUE.split("%")[0], "Chain": limits.VALUE, "Warn": nudge.VALUE,
                  "Add the «when to cut»": settings_mod.CUT_VALUE, "Schedule": review.VALUE,
                  "Install the /handoff": handoff.VALUE, "After /clear": resume.VALUE}
        log, asked = [], []

        def inp(q):
            asked.append(q)
            if not q.startswith(" "):  # every piece's question; the indented ones are follow-ups of the piece just asked
                prefix = next((p for p in values if q.startswith(p)), None)
                self.assertIsNotNone(prefix, "a setup question without its value text: %r" % q)
                said = " ".join(" ".join(log).split())
                self.assertIn(" ".join(values[prefix][:60].split()), said[-len(values[prefix]) - 200:], q)
            log.append("<asked>")
            return "n"
        installer.setup(inp=inp, scope="project", cwd=self.cwd, kit_bin="/k/bin/kit", agents_dir=self.agents, load=False,
                        settings_path=self.settings, out=log.append)
        self.assertEqual(len([q for q in asked if any(q.startswith(p) for p in values)]), len(values), asked)

    def test_yes_still_prints_what_each_piece_gives_with_the_users_own_figure(self):
        _rc, lines = self.run_setup()
        text = " ".join(" ".join(lines).split())
        for value in (limits.VALUE, nudge.VALUE, review.VALUE):
            self.assertIn(" ".join(value.split()), text)
        self.assertIn("after 30 days", text)
        self.assertNotIn("In the last 7 days", text)  # no database yet: no figure
        store.connect(paths.db_path()).close()
        with mock.patch.object(installer.analysis, "resumes_since", return_value=(4, 12.5)):
            _rc, lines = self.run_setup()
        self.assertIn("happened 4 times, ≈ $12.50", " ".join(" ".join(lines).split()))
        with mock.patch.object(installer.analysis, "resumes_since", side_effect=installer.sqlite3.Error):
            self.assertNotIn("resumes", installer.setup_figures(self.cwd))

    def test_interactive_decline(self):
        answers = iter(["global", "n", "n", "n", "n", "n", "n"])  # ..., /handoff, resume hook
        lines = []
        installer.setup(inp=lambda q: next(answers), kit_bin="/k/bin/kit", agents_dir=self.agents, load=False,
                        settings_path=self.settings, out=lines.append)
        self.assertFalse(nudge.installed(self.settings) if os.path.exists(self.settings) else False)
        self.assertFalse(os.path.exists(installer.ingest_plist_path(self.agents)))

    def test_alerts_backup_before_write(self):
        os.makedirs(paths.data_dir())
        with open(installer.alerts_path(), "w") as fh:
            json.dump({"budgets": [{"name": "x"}]}, fh)
        installer.update_alerts(lambda c: c.setdefault("review", {}).__setitem__("time", "09:00"))
        self.assertEqual(read_json(installer.alerts_path() + ".bak-setup"), {"budgets": [{"name": "x"}]})
        self.assertEqual(read_json(installer.alerts_path())["budgets"], [{"name": "x"}])

    def test_alerts_written_in_utf8_whatever_the_locale(self):
        real = os.fdopen

        def ansi_fdopen(fd, mode="r", *a, **kw):  # Windows: text files default to the ANSI code page
            if "b" not in mode:
                kw.setdefault("encoding", "cp1252")
            return real(fd, mode, *a, **kw)
        with mock.patch("os.fdopen", ansi_fdopen):
            installer.update_alerts(lambda c: c.setdefault("budgets", []).append({"name": "café → x"}))
            installer.update_alerts(lambda c: c.__setitem__("lang", "es"))
        with open(installer.alerts_path(), "rb") as fh:
            cfg = json.loads(fh.read().decode("utf-8"))
        self.assertEqual(cfg, {"budgets": [{"name": "café → x"}], "lang": "es"})
        self.assertEqual(limits.load_alerts()["budgets"], [{"name": "café → x"}])
        self.assertEqual([f for f in os.listdir(paths.data_dir()) if f.endswith(".tmp")], [])
        if os.name != "nt":
            self.assertEqual(os.stat(installer.alerts_path()).st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt", "no POSIX modes on Windows")
    def test_alerts_back_to_600_when_created_looser_by_hand(self):
        os.makedirs(paths.data_dir(), exist_ok=True)
        with open(installer.alerts_path(), "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.chmod(installer.alerts_path(), 0o644)
        installer.update_alerts(lambda c: c.__setitem__("lang", "en"))
        self.assertEqual(os.stat(installer.alerts_path()).st_mode & 0o777, 0o600)

    def test_scope_paths(self):
        self.assertEqual(installer.settings_for("project", self.cwd), os.path.join(self.cwd, ".claude", "settings.local.json"))
        cfg = os.path.join(self.tmp, "cfg")
        os.environ["CLAUDE_CONFIG_DIR"] = cfg
        self.assertEqual(installer.settings_for("company"), os.path.join(cfg, "settings.json"))
        del os.environ["CLAUDE_CONFIG_DIR"]
        with self.assertRaises(ValueError):
            installer.settings_for("company")


class Check(Base):
    def status(self, items, name):
        return [s for s, n, _ in items if n.startswith(name)]

    def items(self, **kw):
        return installer.check(cwd=self.cwd, agents_dir=self.agents, load_fn=kw.pop("load_fn", lambda _label: True),
                               mac=kw.pop("mac", True), **kw)

    def test_nothing_installed_warns_without_failing(self):
        items = self.items()
        self.assertNotIn(installer.BAD, [s for s, _, _ in items])
        self.assertEqual(self.status(items, "ingest agent"), [installer.WARN])

    def test_after_setup_all_ok_and_drift_detected(self):
        lines = []
        installer.setup(yes=True, kit_bin=EXISTING, agents_dir=self.agents, load=False, settings_path=self.settings,
                        out=lines.append)
        items = self.items()
        self.assertNotIn(installer.BAD, [s for s, _, _ in items], items)
        self.assertEqual(self.status(items, "review agent config"), [])
        installer.update_alerts(lambda c: c.setdefault("review", {}).__setitem__("time", "10:15"))
        items = self.items()
        self.assertIn(installer.WARN, self.status(items, "review agent config"))

    def test_unloaded_agent_and_missing_script_fail(self):
        installer.setup(yes=True, kit_bin="/nonexistent/bin/kit", agents_dir=self.agents, load=False, settings_path=self.settings,
                        out=lambda s: None)
        items = self.items(load_fn=lambda _label: False)
        self.assertIn(installer.BAD, self.status(items, "ingest agent"))
        self.assertIn(installer.BAD, self.status(items, "hook"))

    def test_stale_ingest_vs_retention(self):
        os.makedirs(paths.data_dir())
        con = store.connect(paths.db_path())
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat(timespec="seconds")
        con.execute("INSERT INTO ingest_runs VALUES (?,?,?,?,?)", (old, "t", 0, 0, 0))
        con.commit()
        con.close()
        self.write_settings({"cleanupPeriodDays": 14})
        self.assertEqual(installer.retention_days(self.cwd), 14)
        self.assertEqual(self.status(self.items(), "last ingest"), [installer.BAD])

    @posix_only
    def test_permissions_warning(self):
        os.makedirs(paths.data_dir())
        os.chmod(paths.data_dir(), 0o755)
        self.assertEqual(self.status(self.items(), "permissions"), [installer.WARN])
        os.chmod(paths.data_dir(), 0o700)
        self.assertEqual(self.status(self.items(), "permissions"), [installer.OK])


class UninstallAll(Base):
    def test_removes_everything_keeps_data_and_is_idempotent(self):
        self.write_settings({"model": "x", "statusLine": {"type": "command", "command": "sh orca.sh"}})
        proj = installer.settings_for("project", self.cwd)
        installer.setup(yes=True, kit_bin="/k/bin/kit", agents_dir=self.agents, load=False, settings_path=self.settings, out=lambda s: None)
        nudge.install(proj, kit_bin="/k/bin/kit")
        out = installer.uninstall_all(cwd=self.cwd, agents_dir=self.agents, load=False, mac=True)
        d = read_json(self.settings)
        self.assertEqual(d["statusLine"]["command"], "sh orca.sh")
        self.assertFalse(nudge.installed(self.settings) or nudge.installed(proj))
        self.assertFalse(os.path.exists(installer.ingest_plist_path(self.agents)) or review.installed(self.agents))
        self.assertTrue(os.path.isdir(paths.data_dir()))
        again = installer.uninstall_all(cwd=self.cwd, agents_dir=self.agents, load=False, mac=True)
        self.assertIn("not installed", "\n".join(again))
        self.assertTrue(out)

    def test_removes_the_cut_rule_of_this_project_and_keeps_the_rest(self):
        local, shared = os.path.join(self.cwd, "CLAUDE.local.md"), os.path.join(self.cwd, "CLAUDE.md")
        with open(local, "w", encoding="utf-8", newline="") as fh:  # LF as setup writes it, also on Windows
            fh.write(settings_mod.add_cut_rule("# mine\n", "es"))
        with open(shared, "wb") as fh:
            fh.write(b"\xff not utf-8")
        out = "\n".join(installer.uninstall_all(cwd=self.cwd, agents_dir=self.agents, load=False, mac=True))
        self.assertIn("cut rule: removed from " + local, out)
        self.assertIn("not UTF-8", out)
        with open(local, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "# mine\n")
        self.assertNotIn("cut rule: removed",
                         "\n".join(installer.uninstall_all(cwd=self.cwd, agents_dir=self.agents, load=False, mac=True)))

    def test_foreign_statusline_untouched(self):
        self.write_settings({"statusLine": {"type": "command", "command": "sh mine.sh"}})
        installer.uninstall_all(cwd=self.cwd, agents_dir=self.agents, load=False, mac=True)
        self.assertEqual(read_json(self.settings)["statusLine"]["command"], "sh mine.sh")

    def test_statusline_uninstall_leaves_a_foreign_line_alone(self):
        self.write_settings({"statusLine": {"type": "command", "command": "~/my-line.sh"}})
        self.assertEqual(limits.uninstall(self.settings), "not installed")
        self.assertEqual(read_json(self.settings)["statusLine"]["command"], "~/my-line.sh")
        self.assertFalse(os.path.exists(self.settings + ".bak-statusline"))

    def test_statusline_uninstall_twice_never_brings_back_an_old_line(self):
        chain = os.path.join(paths.data_dir(), "statusline-chain.sh")
        self.write_settings({"statusLine": {"type": "command", "command": "sh old.sh"}})
        limits.install(self.settings, kit_bin="/k/bin/sessioncoach", chain=chain)
        self.assertEqual(limits.uninstall(self.settings), "restored previous status line")
        self.assertEqual(read_json(self.settings)["statusLine"]["command"], "sh old.sh")
        self.assertFalse(os.path.exists(chain))
        self.write_settings({"statusLine": {"type": "command", "command": "brand-new.sh"}})
        self.assertEqual(limits.uninstall(self.settings), "not installed")
        self.assertEqual(read_json(self.settings)["statusLine"]["command"], "brand-new.sh")

    def test_purge_needs_confirmation(self):
        os.makedirs(paths.data_dir())
        installer.uninstall_all(cwd=self.cwd, agents_dir=self.agents, load=False, mac=True, purge=True, confirm=lambda d: False)
        self.assertTrue(os.path.isdir(paths.data_dir()))
        installer.uninstall_all(cwd=self.cwd, agents_dir=self.agents, load=False, mac=True, purge=True, confirm=lambda d: True)
        self.assertFalse(os.path.exists(paths.data_dir()))


class ProjectDecisions(Base):
    """kit setup --scope project: cut-rule destination, .gitignore with consent, duplicated hook; kit check warnings."""

    def setup_project(self, **kw):
        lines = []
        kw.setdefault("yes", True)
        with mock.patch.dict(os.environ, {"KIT_SCHEDULER": "none"}):  # no scheduler questions: same on any OS
            rc = installer.setup(kit_bin="/k/bin/kit", agents_dir=self.agents, load=False, scope="project", cwd=self.cwd,
                                 out=lines.append, **kw)
        return rc, "\n".join(lines)

    def check_items(self):
        return installer.check(cwd=self.cwd, agents_dir=self.agents, mac=True, load_fn=lambda _label: True)

    def git_init(self):
        import shutil
        import subprocess
        if not shutil.which("git"):
            self.skipTest("git not installed")
        subprocess.run(["git", "init", "-q", self.cwd], check=True)

    def test_cut_rule_goes_where_the_user_chooses(self):
        from sessioncoach import settings as S
        rc, _ = self.setup_project(cut_dest="shared")
        self.assertEqual(rc, 0)
        self.assertIn(S.CUT_OPEN, read_text(os.path.join(self.cwd, "CLAUDE.md")))
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "CLAUDE.local.md")))
        os.remove(os.path.join(self.cwd, "CLAUDE.md"))
        self.setup_project(cut_dest="local")
        self.assertIn(S.CUT_OPEN, read_text(os.path.join(self.cwd, "CLAUDE.local.md")))

    def test_yes_without_cut_dest_writes_no_rule(self):
        self.setup_project()
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "CLAUDE.local.md")))
        self.assertTrue(nudge.installed(installer.settings_for("project", self.cwd)))

    def test_interactive_choice_and_gitignore_consent(self):
        self.git_init()
        answers = iter(["", "", "", "local", "n", "n", "n", "n"])  # statusline, hook, rule?, where, gitignore, handoff, resume, idle guard
        _rc, out = self.setup_project(yes=False, inp=lambda q: next(answers))
        self.assertIn("CLAUDE.local.md: personal", out)
        self.assertFalse(os.path.exists(os.path.join(self.cwd, ".gitignore")))
        self.assertIn("not ignored by git", out)

    def test_gitignore_flag_and_interactive_default_yes(self):
        self.git_init()
        self.setup_project(cut_dest="local", gitignore=True)
        self.assertEqual(read_text(os.path.join(self.cwd, ".gitignore")), "CLAUDE.local.md\n")
        os.remove(os.path.join(self.cwd, ".gitignore"))
        os.remove(os.path.join(self.cwd, "CLAUDE.local.md"))
        answers = iter(["", "", "", "", "", "", "", ""])  # all defaults, including .gitignore = yes; the idle guard's is no
        self.setup_project(yes=False, inp=lambda q: next(answers))
        self.assertEqual(read_text(os.path.join(self.cwd, ".gitignore")), "CLAUDE.local.md\n")

    def test_hook_skipped_in_project_if_global_has_it_but_statusline_only_notes(self):
        nudge.install(self.settings, kit_bin="/k/bin/kit")
        limits.install(self.settings, kit_bin="/k/bin/kit", chain=limits.chain_path(self.settings))
        rc, out = self.setup_project()
        self.assertEqual(rc, 0)
        self.assertIn("hook: skipped", out)
        self.assertFalse(nudge.installed(installer.settings_for("project", self.cwd)))
        self.assertIn("take precedence", out)
        self.assertTrue(limits.installed(installer.settings_for("project", self.cwd)))

    def test_global_setup_does_not_block_company_hook(self):
        cfg = os.path.join(self.tmp, "cfg-co")
        os.makedirs(cfg)
        nudge.install(self.settings, kit_bin="/k/bin/kit")
        rc, _out = installer.setup(yes=True, scope="company", kit_bin="/k/bin/kit", agents_dir=self.agents, load=False,
                                  settings_path=os.path.join(cfg, "settings.json"), out=lambda s: None), None
        self.assertEqual(rc, 0)
        self.assertTrue(nudge.installed(os.path.join(cfg, "settings.json")))

    def test_check_warns_when_claude_local_md_is_not_ignored(self):
        from sessioncoach import settings as S
        self.git_init()
        local = os.path.join(self.cwd, "CLAUDE.local.md")
        write_text(local, S.add_cut_rule("", "es"))
        items = {n: (st, d) for st, n, d in self.check_items()}
        self.assertEqual(items["CLAUDE.local.md"][0], installer.WARN)
        write_text(os.path.join(self.cwd, ".gitignore"), "CLAUDE.local.md\n")
        items = {n: (st, d) for st, n, d in self.check_items()}
        self.assertEqual(items["CLAUDE.local.md"][0], installer.OK)

    def test_check_is_silent_without_the_block_and_warns_about_double_hook(self):
        self.git_init()
        write_text(os.path.join(self.cwd, "CLAUDE.local.md"), "mine\n")
        names = [n for _s, n, _d in self.check_items()]
        self.assertNotIn("CLAUDE.local.md", names)
        nudge.install(self.settings, kit_bin="/k/bin/kit")
        nudge.install(installer.settings_for("project", self.cwd), kit_bin="/k/bin/kit")
        items = {n: st for st, n, _d in self.check_items()}
        self.assertEqual(items["hook duplicated"], installer.WARN)


class RenamedEnvAndDataDir(unittest.TestCase):
    """SESSIONCOACH_* wins over KIT_* (its name before the rename); an install made before keeps ~/.kit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = paths.HOME
        paths.HOME = self.tmp
        self.saved = {k: os.environ.pop(k, None) for k in ("SESSIONCOACH_DATA_DIR", "KIT_DATA_DIR", "SESSIONCOACH_LANG", "KIT_LANG")}

    def tearDown(self):
        paths.HOME = self.home
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_precedence(self):
        self.assertIsNone(paths.env("LANG"))
        os.environ["KIT_LANG"] = "es"
        self.assertEqual(paths.env("LANG"), "es")
        os.environ["SESSIONCOACH_LANG"] = "en"
        self.assertEqual(paths.env("LANG"), "en")

    def test_set_env_drops_the_old_name(self):
        os.environ["KIT_DATA_DIR"] = "/real"
        paths.set_env("DATA_DIR", "/demo")
        self.assertNotIn("KIT_DATA_DIR", os.environ)
        self.assertEqual(paths.data_dir(), "/demo")

    def test_data_dir_new_install_old_install_and_both(self):
        new, old = os.path.join(self.tmp, ".sessioncoach"), os.path.join(self.tmp, ".kit")
        self.assertEqual(paths.data_dir(), new)
        os.makedirs(old)
        self.assertEqual(paths.data_dir(), old)  # set up before the rename: data and chain files stay
        os.makedirs(new)
        self.assertEqual(paths.data_dir(), old)  # an empty new folder does not switch away from the data
        open(os.path.join(new, "metrics.db"), "w").close()
        self.assertEqual(paths.data_dir(), new)
        os.environ["KIT_DATA_DIR"] = os.path.join(self.tmp, "x")
        self.assertEqual(paths.data_dir(), os.path.join(self.tmp, "x"))

    def test_data_dir_from_the_environment_expands_the_home(self):
        with mock.patch.object(paths, "HOME", self.tmp):
            for name in ("KIT_DATA_DIR", "SESSIONCOACH_DATA_DIR"):
                for value, want in (("~/metrics", os.path.join(self.tmp, "metrics")), ("~", self.tmp),
                                    ("/abs/m~", "/abs/m~"), ("rel/x", "rel/x")):
                    with self.subTest(name=name, value=value), mock.patch.dict(os.environ, {name: value}):
                        self.assertEqual(os.path.normpath(paths.data_dir()), os.path.normpath(want))
            with mock.patch.dict(os.environ, {"KIT_DATA_DIR": "~/m"}):
                self.assertEqual(paths.db_path(), os.path.join(self.tmp, "m", "metrics.db"))
                self.assertFalse(os.path.exists(os.path.join(os.getcwd(), "~")))

FOREIGN_SETTINGS = json.dumps({"statusLine": {"type": "command", "command": "echo mine"},
                               "hooks": {nudge.HOOK_EVENT: [{"hooks": [{"type": "command", "command": "other.sh"}]}]},
                               "model": "opus"})


class PurePlans(unittest.TestCase):
    """limits/nudge plan_* compute the new settings without touching any file or the input dict."""

    @staticmethod
    def frozen():
        return json.loads(FOREIGN_SETTINGS)

    def test_statusline_install_chains_the_current_command(self):
        s = self.frozen()
        new, chain_text = limits.plan_install(s, "/d/chain.sh", "/k/bin/sessioncoach")
        self.assertEqual(s, self.frozen())
        self.assertEqual(chain_text, "echo mine\n")
        self.assertIn("statusline --chain-file /d/chain.sh", new["statusLine"]["command"])
        self.assertEqual(limits.installed_chain(new), "/d/chain.sh")
        self.assertEqual((new["model"], new["hooks"]), (s["model"], s["hooks"]))
        self.assertIsNone(limits.plan_install(new, "/other/chain.sh"))
        self.assertEqual(limits.plan_install({}, "/d/c.sh")[1], ":\n")

    def test_statusline_uninstall_restores_or_drops(self):
        s = {"statusLine": {"type": "command", "command": "x statusline --chain-file /d/c.sh"}, "a": [1]}
        self.assertEqual(limits.plan_uninstall(s, "echo mine\n")["statusLine"]["command"], "echo mine")
        for prev in (None, "", ":\n"):
            self.assertEqual(limits.plan_uninstall(s, prev), {"a": [1]})
        self.assertIn("statusLine", s)
        self.assertIsNone(limits.installed_chain({}))

    def test_hook_install_and_uninstall(self):
        s = self.frozen()
        new = nudge.plan_install(s, "/k/bin/sessioncoach")
        self.assertEqual(s, self.frozen())
        self.assertEqual(len(new["hooks"][nudge.HOOK_EVENT]), 2)
        self.assertIsNone(nudge.plan_install(new))
        back = nudge.plan_uninstall(new)
        self.assertEqual(back, self.frozen())
        self.assertEqual(len(new["hooks"][nudge.HOOK_EVENT]), 2)
        self.assertIsNone(nudge.plan_uninstall(back))
        alone = nudge.plan_install({})
        self.assertEqual(nudge.plan_uninstall(alone), {"hooks": {}})

    def test_importing_settings_leaves_data_dir_alone(self):
        from sessioncoach import settings  # noqa: F401
        self.assertEqual(paths.data_dir.__module__, "sessioncoach.paths")


if __name__ == "__main__":
    unittest.main()


class TestChainPerScope(unittest.TestCase):
    """Installing the status line in a project must not overwrite or restore another scope's command."""

    def test_project_and_global_keep_their_own_previous_command(self):
        import shutil
        import tempfile

        from sessioncoach import limits
        tmp = tempfile.mkdtemp()
        os.environ["KIT_DATA_DIR"] = os.path.join(tmp, "kit")
        try:
            g = os.path.join(tmp, "g", "settings.json")
            p = os.path.join(tmp, "proj", ".claude", "settings.local.json")
            for path, cmd in ((g, "global-line"), (p, "project-line")):
                os.makedirs(os.path.dirname(path))
                with open(path, "w") as fh:
                    json.dump({"statusLine": {"type": "command", "command": cmd}}, fh)
            limits.install(g, kit_bin="/k/bin/kit", chain=os.path.join(tmp, "kit", "statusline-chain.sh"))
            limits.install(p, kit_bin="/k/bin/kit", chain=limits.chain_path(p))
            self.assertNotEqual(limits.chain_path(p), limits.chain_path(g))
            limits.uninstall(p)
            limits.uninstall(g)
            self.assertEqual(read_json(p)["statusLine"]["command"], "project-line")
            self.assertEqual(read_json(g)["statusLine"]["command"], "global-line")
        finally:
            del os.environ["KIT_DATA_DIR"]
            shutil.rmtree(tmp)


@unittest.skipIf(os.name == "nt", "POSIX modes")
class TestPrivateDataDir(unittest.TestCase):
    """A fresh data dir and database are private whatever the umask (Linux defaults to 022)."""

    def test_fresh_dir_and_db_are_private(self):
        tmp = tempfile.mkdtemp()
        old = os.umask(0o022)
        try:
            d = os.path.join(tmp, "kit")
            with mock.patch.dict(os.environ, {"KIT_DATA_DIR": d}):
                store.connect(paths.db_path()).close()
                paths.ensure_data_dir("logs")
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "metrics.db")).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "logs")).st_mode), 0o700)
        finally:
            os.umask(old)
            shutil.rmtree(tmp)


OLD_HOOK_CMD = "/usr/bin/python3 /old/bin/kit hook prompt"


class LegacyCommands(Base):
    """settings.json written under the old name (bin/kit, -m kit) is updated in place, never duplicated or left to rot."""

    def old_statusline(self, form="/old/bin/kit", chain=None):
        chain = chain or os.path.join(paths.data_dir(), "statusline-chain.sh")
        return {"type": "command", "command": "/usr/bin/python3 %s statusline --chain-file %s" % (form, chain)}

    def test_old_command_name(self):
        for cmd, want in (("/usr/bin/python3 /old/bin/kit hook prompt", "bin/kit"), ("C:/Py/python.exe -m kit hook prompt", "-m kit"),
                          ("C:\\Py\\bin\\kit hook prompt", "bin/kit"), ('"/a b/bin/kit" hook prompt', "bin/kit"),
                          ("/usr/bin/python3 /new/bin/sessioncoach hook prompt", None),
                          ("/usr/bin/python3 -m sessioncoach hook", "-m sessioncoach"),  # was None: -m is no longer written
                          ("/v/bin/sessioncoach hook prompt", None), ("/py -c '%s' hook prompt" % paths.RUN_CODE, None),
                          ("/x/daf-claude-kit/bin/sessioncoach hook prompt", None), ("/u/.kit/bin/kitten hook", None),
                          ("", None), (None, None)):
            self.assertEqual(paths.old_command_name(cmd), want, cmd)

    def test_hook_plan_rewrites_in_place_keeping_position_and_timeout(self):
        mine = {"hooks": [{"type": "command", "command": "echo a"}, {"type": "command", "command": OLD_HOOK_CMD, "timeout": 9}]}
        s = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo first"}]}, mine,
                                            {"hooks": [{"type": "command", "command": "echo last"}]}], "Stop": []}, "model": "x"}
        frozen = json.loads(json.dumps(s))
        new = nudge.plan_install(s, "/k/bin/sessioncoach")
        self.assertEqual(s, frozen)  # the input is not touched
        hooks = new["hooks"]["UserPromptSubmit"]
        self.assertEqual([len(e["hooks"]) for e in hooks], [1, 2, 1])
        self.assertEqual(hooks[1]["hooks"][1], {"type": "command", "timeout": 9,
                                                "command": paths.kit_command_str("hook", "prompt", kit_bin="/k/bin/sessioncoach")})
        self.assertEqual((hooks[0], hooks[2], hooks[1]["hooks"][0]), (s["hooks"]["UserPromptSubmit"][0],
                                                                         s["hooks"]["UserPromptSubmit"][2], mine["hooks"][0]))
        self.assertEqual((new["model"], new["hooks"]["Stop"]), ("x", []))
        self.assertIsNone(nudge.plan_install(new, "/k/bin/sessioncoach"))  # already current
        # only ours goes: "echo a", in the same entry, stays
        self.assertEqual(nudge.plan_uninstall(new)["hooks"]["UserPromptSubmit"], [hooks[0], {"hooks": [mine["hooks"][0]]}, hooks[2]])

    def test_hook_plan_handles_python_dash_m_kit(self):
        s = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 -m kit hook prompt"}]}]}}
        new = nudge.plan_install(s, "/k/bin/sessioncoach")
        self.assertNotIn("-m kit", new["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"])
        self.assertTrue(nudge.installed_in(new))
        self.assertFalse(nudge.installed_in({"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "other.sh"}]}]}}))

    @posix_only
    def test_hook_install_updates_with_backup_and_uninstall_still_works(self):
        self.write_settings({"model": "x", "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": OLD_HOOK_CMD}]}]}})
        res = nudge.install(self.settings, kit_bin="/k/bin/sessioncoach")
        self.assertIn("updated to the current command (settings backup %s" % (self.settings + ".bak-hook"), res)
        self.assertEqual(read_json(self.settings + ".bak-hook")["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], OLD_HOOK_CMD)
        self.assertEqual(nudge.install(self.settings, kit_bin="/k/bin/sessioncoach"), "already installed")
        self.assertEqual(nudge.uninstall(self.settings), "removed")
        self.assertEqual(read_json(self.settings), {"model": "x", "hooks": {}})

    def test_statusline_plan_keeps_the_chain_file_and_other_keys(self):
        sl = dict(self.old_statusline(chain="/data/chains/ab/statusline-chain.sh"), padding=2)
        s = {"statusLine": sl, "model": "x"}
        new, chain_text = limits.plan_install(s, "/data/statusline-chain.sh", "/k/bin/sessioncoach")
        self.assertIsNone(chain_text)  # the chain file is not rewritten
        self.assertEqual(limits.installed_chain(new), "/data/chains/ab/statusline-chain.sh")
        self.assertEqual(new["statusLine"]["command"], paths.kit_command_str(
            "statusline", "--chain-file", "/data/chains/ab/statusline-chain.sh", kit_bin="/k/bin/sessioncoach"))
        self.assertEqual((new["statusLine"]["type"], new["statusLine"]["padding"], new["model"]), ("command", 2, "x"))
        self.assertEqual(s["statusLine"], sl)
        self.assertIsNone(limits.plan_install(new, "/data/statusline-chain.sh", "/k/bin/sessioncoach"))
        # an old command already equal to what would be written is left as it is
        cmd = paths.kit_command_str("statusline", "--chain-file", "/c.sh", kit_bin="/k/bin/kit")
        same = {"statusLine": {"type": "command", "command": cmd}}
        self.assertIsNone(limits.plan_install(same, "/c.sh", "/k/bin/kit"))

    def test_statusline_install_updates_without_touching_the_chain_and_uninstall_restores(self):
        chain = os.path.join(paths.data_dir(), "statusline-chain.sh")
        os.makedirs(paths.data_dir())
        write_text(chain, "sh orca.sh\n")
        os.chmod(chain, 0o640)
        self.write_settings({"statusLine": self.old_statusline("-m kit")})
        self.assertTrue(limits.installed(self.settings))
        res = limits.install(self.settings, kit_bin="/k/bin/sessioncoach", chain=chain)
        self.assertIn("updated to the current command (settings backup %s" % (self.settings + ".bak-statusline"), res)
        self.assertEqual(read_text(chain), "sh orca.sh\n")
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(chain).st_mode), 0o640)
        self.assertNotIn("-m kit", read_json(self.settings)["statusLine"]["command"])
        self.assertEqual(limits.install(self.settings, kit_bin="/k/bin/sessioncoach", chain=chain), "already installed")
        self.assertEqual(limits.uninstall(self.settings), "restored previous status line")
        self.assertEqual(read_json(self.settings)["statusLine"], {"type": "command", "command": "sh orca.sh"})

    def test_statusline_with_an_unreadable_chain_option_is_left_alone(self):
        s = {"statusLine": {"type": "command", "command": "/usr/bin/python3 /old/bin/kit statusline --chain-file 'unterminated"}}
        self.assertIsNone(limits.plan_install(s, "/c.sh", "/k/bin/sessioncoach"))

    def test_setup_updates_both_and_check_stops_warning(self):
        chain = os.path.join(paths.data_dir(), "statusline-chain.sh")
        os.makedirs(paths.data_dir())
        write_text(chain, ":\n")
        self.write_settings({"statusLine": self.old_statusline(), "hooks": {"UserPromptSubmit": [{"hooks": [{"command": OLD_HOOK_CMD}]}]}})
        sc = os.path.join(self.tmp, "clone", "bin", "sessioncoach")  # a clone's script, the shape setup writes
        os.makedirs(os.path.dirname(sc))
        write_text(sc, "")

        def warns():
            items = installer.check(cwd=self.cwd, agents_dir=self.agents, mac=True, load_fn=lambda _l: True)
            return {n: d for st, n, d in items if st == installer.WARN and "old command" in d}
        w = warns()
        self.assertEqual(sorted(w), ["hook [global]", "status line [global]"])
        self.assertIn("uses the old command bin/kit — run: sessioncoach setup to update it", w["hook [global]"])
        lines = []
        installer.setup(yes=True, kit_bin=sc, agents_dir=self.agents, load=False, settings_path=self.settings, out=lines.append)
        self.assertTrue(any(ln.startswith("  hook: updated to the current command") for ln in lines), lines)
        self.assertTrue(any(ln.startswith("  status line: updated to the current command") for ln in lines), lines)
        self.assertEqual(warns(), {})
        self.assertEqual(read_text(chain), ":\n")
        self.assertEqual(len(read_json(self.settings)["hooks"]["UserPromptSubmit"]), 1)
        installer.uninstall_all(cwd=self.cwd, agents_dir=self.agents, load=False, mac=True)
        self.assertEqual(read_json(self.settings), {"hooks": {}})
