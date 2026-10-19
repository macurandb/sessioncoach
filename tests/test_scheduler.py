"""Schedulers other than launchd (systemd --user, cron, Task Scheduler) and the Windows command forms.

Windows cannot be executed from here: its pieces are tested as pure text generation (XML, command
strings) plus a fake `schtasks`. Nothing here touches the real systemd, crontab or Task Scheduler.
"""
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
from xml.dom import minidom

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # drops SESSIONCOACH_* left in the shell; no real scheduler

from sessioncoach import installer, limits, nudge, paths, review, scheduler

EXISTING = sys.executable  # a file on every OS: check verifies that the script in setup's commands exists


def read_json(path):
    with open(path) as fh:
        return json.load(fh)


class Result:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeSystem:
    """In-memory crontab + schtasks + systemctl (+ launchctl when passed as run=); records every call."""
    def __init__(self):
        self.crontab, self.calls, self.tasks = "", [], set()

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "crontab":
            if cmd[1] == "-l":
                return Result(0 if self.crontab else 1, self.crontab)
            if cmd[1] == "-":
                self.crontab = kw["input"]
            else:
                self.crontab = ""
            return Result()
        if cmd[0] == "schtasks":
            name = cmd[cmd.index("/TN") + 1]
            if cmd[1] == "/Create":
                self.tasks.add(name)
            elif cmd[1] == "/Delete":
                self.tasks.discard(name)
            elif cmd[1] == "/Query":
                return Result(0 if name in self.tasks else 1, "Next Run Time:  1/2/2026 8:00:00 AM\n")
            return Result()
        return Result()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = {k: os.environ.get(k) for k in ("KIT_DATA_DIR", "KIT_SCHEDULER", "CLAUDE_CONFIG_DIR", "KIT_PROJECTS_DIRS")}
        self.home = paths.HOME
        paths.HOME = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(paths.HOME, ".claude"))
        os.environ["KIT_DATA_DIR"] = os.path.join(self.tmp, "kit")
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("KIT_PROJECTS_DIRS", None)
        self.units = os.path.join(self.tmp, "units")
        self.settings = os.path.join(paths.HOME, ".claude", "settings.json")
        self.sys = FakeSystem()
        patch = mock.patch.object(scheduler, "_run", self.sys)  # systemctl/crontab/schtasks without run= also go to the fake
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        paths.HOME = self.home
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    ingest = scheduler.Job("ingest", ["ingest"], every_min=30)
    rev = scheduler.Job("review", ["review", "--scheduled"], hhmm="08:00", days=["mon", "tue", "fri"])


class Backend(Base):
    def test_platform_choice(self):
        os.environ.pop("KIT_SCHEDULER", None)
        self.assertEqual(scheduler.backend("darwin"), "launchd")
        self.assertEqual(scheduler.backend("win32"), "schtasks")

    def test_override(self):
        os.environ["KIT_SCHEDULER"] = "cron"
        self.assertEqual(scheduler.backend("darwin"), "cron")
        os.environ["KIT_SCHEDULER"] = "none"
        self.assertIsNone(scheduler.backend())

    def test_unknown_override_is_an_error_listing_the_valid_ones(self):
        for var in ("KIT_SCHEDULER", "SESSIONCOACH_SCHEDULER"):
            with self.subTest(var=var), mock.patch.dict(os.environ, {var: "off"}):
                with self.assertRaises(ValueError) as ctx:
                    scheduler.backend("darwin")
                msg = str(ctx.exception)
                self.assertIn("%s=off" % var, msg)
                for name in (*scheduler.BACKENDS, "none"):
                    self.assertIn(name, msg)
                with self.assertRaises(ValueError):  # never falls back to installing on the automatic one
                    scheduler.install(self.ingest, "/k/bin/sessioncoach", unit_dir=self.units)
        self.assertEqual(self.sys.calls, [])

    def test_unknown_override_never_blocks_removal_and_check_and_setup_say_why(self):
        os.environ["KIT_SCHEDULER"] = "cron"
        scheduler.install(self.ingest, "/k/bin/sessioncoach", load=True, be="cron")
        self.assertIn(scheduler.CRON_BEGIN, self.sys.crontab)
        os.environ["KIT_SCHEDULER"] = "off"
        with mock.patch.object(scheduler, "auto_backend", return_value="cron"):  # this OS's own scheduler is cleaned
            out = "\n".join(installer.uninstall_all(agents_dir=self.units, load=True))
        self.assertIn("ingest agent:", out)
        self.assertNotIn(scheduler.CRON_BEGIN, self.sys.crontab)
        items = {n: (st, d) for st, n, d in installer.check(agents_dir=self.units)}
        self.assertEqual(items["scheduler"][0], installer.BAD)
        self.assertIn("KIT_SCHEDULER=off", items["scheduler"][1])
        lines = []
        self.assertEqual(installer.setup(yes=True, settings_path=self.settings, out=lines.append), 1)
        self.assertIn("KIT_SCHEDULER=off", lines[0])

    def test_unknown_override_on_a_mac_still_removes_the_launch_agents(self):
        os.environ["KIT_SCHEDULER"] = "launchd"
        scheduler.install(self.ingest, "/k/bin/sessioncoach", unit_dir=self.units, load=False)
        plist = scheduler.plist_path(scheduler.launchd_label("ingest"), self.units)
        self.assertTrue(os.path.exists(plist))
        os.environ["KIT_SCHEDULER"] = "off"
        scheduler.uninstall(self.ingest, unit_dir=self.units, load=False, platform="darwin")
        self.assertFalse(os.path.exists(plist))

    def test_unknown_override_in_the_panel_and_the_install_commands_is_a_message(self):
        from sessioncoach import cli, settings
        os.environ["KIT_SCHEDULER"] = "off"
        panel = settings.Settings(kit_bin="/k/bin/sessioncoach", agents_dir=self.units)
        self.assertFalse(panel.state()["review_agent"])
        with self.assertRaises(settings.SettingsError) as ctx:
            panel.preview({"scope": "global", "review_agent": True})
        self.assertEqual(ctx.exception.code, 422)
        self.assertIn("KIT_SCHEDULER=off", ctx.exception.msg)
        for cmd in ("ingest-install", "review-install"):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main([cmd]), 2)
            self.assertIn("KIT_SCHEDULER=off", out.getvalue())

    def test_override_names_are_exact(self):
        os.environ["KIT_SCHEDULER"] = "Cron"
        with self.assertRaises(ValueError):
            scheduler.backend()
        for name in scheduler.BACKENDS:
            os.environ["KIT_SCHEDULER"] = name
            self.assertEqual(scheduler.backend(), name)


class Isolation(unittest.TestCase):
    """isolate.py: no backend unless a test forces one, and real scheduler commands fail the test."""

    def test_defaults(self):
        self.assertEqual(os.environ.get("KIT_SCHEDULER"), "none")
        self.assertIsNone(scheduler.backend())
        self.assertNotIn("XDG_CONFIG_HOME", os.environ)

    def test_real_scheduler_commands_are_refused(self):
        for cmd in (["crontab", "-l"], ["/usr/bin/systemctl", "--user", "is-active", "x"], "launchctl list",
                    [r"C:\Windows\System32\SCHTASKS.EXE", "/Query"]):
            for fn in (subprocess.run, subprocess.check_output):
                with self.subTest(cmd=cmd, fn=fn.__name__), self.assertRaises(isolate.RealSchedulerCall):
                    fn(cmd)
        self.assertEqual(subprocess.run([sys.executable, "-c", "pass"], check=False).returncode, 0)  # anything else runs


class Systemd(Base):
    def test_units(self):
        service, timer = scheduler.systemd_units(self.rev, ["/usr/bin/python3", "/k dir/bin/kit", "review", "--scheduled"], "/l/review.log")
        self.assertIn('ExecStart=/usr/bin/python3 "/k dir/bin/kit" review --scheduled', service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("OnCalendar=Mon,Tue,Fri *-*-* 08:00:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("kit-schedule: cal:08:00:mon,tue,fri", timer)
        _, t2 = scheduler.systemd_units(self.ingest, ["x"], "/l")
        self.assertIn("OnUnitActiveSec=30min", t2)
        self.assertIn("OnBootSec=", t2)

    def test_percent_and_dollar_are_escaped(self):
        service, _ = scheduler.systemd_units(self.ingest, ["/a%b/$c"], "/l")
        self.assertIn("/a%%b/$$c", service)

    def test_log_path_with_space_and_percent(self):
        # append: takes the rest of the line verbatim, only expanding specifiers: no quotes, % doubled
        service, _ = scheduler.systemd_units(self.ingest, ["x"], "/home/Ana María/50% off/ingest.log")
        self.assertIn("StandardOutput=append:/home/Ana María/50%% off/ingest.log\n", service)
        self.assertIn("StandardError=append:/home/Ana María/50%% off/ingest.log\n", service)
        with self.assertRaises(ValueError):
            scheduler.systemd_units(self.ingest, ["x"], "/l\n[Service]")

    def test_install_status_uninstall_without_loading(self):
        os.environ["KIT_SCHEDULER"] = "systemd"
        msg = scheduler.install(self.ingest, "/k/bin/kit", unit_dir=self.units, load=False)
        self.assertIn("sessioncoach-ingest.timer", msg)
        for ext in ("service", "timer"):
            self.assertTrue(os.path.exists(os.path.join(self.units, "sessioncoach-ingest." + ext)))
        st = scheduler.status(self.ingest, unit_dir=self.units, be="systemd", run=self.sys)
        self.assertTrue(st["installed"])
        self.assertEqual(st["key"], "every:30")
        self.assertIn("removed", scheduler.uninstall(self.ingest, unit_dir=self.units, load=False, bes=["systemd"]))
        self.assertFalse(scheduler.status(self.ingest, unit_dir=self.units, be="systemd")["installed"])
        self.assertEqual(scheduler.uninstall(self.ingest, unit_dir=self.units, load=False, bes=["systemd"]), "not installed")

    def test_systemctl_goes_through_the_injected_run(self):
        os.environ["KIT_SCHEDULER"] = "systemd"  # uninstall also looks in the active backend
        scheduler.install(self.ingest, "/k/bin/kit", unit_dir=self.units, load=True, run=self.sys, be="systemd")
        self.assertIn(["systemctl", "--user", "enable", "--now", "sessioncoach-ingest.timer"], self.sys.calls)
        st = scheduler.status(self.ingest, unit_dir=self.units, run=self.sys, be="systemd")
        self.assertEqual((st["installed"], st["loaded"]), (True, True))
        scheduler.uninstall(self.ingest, unit_dir=self.units, load=True, run=self.sys, bes=["systemd"])
        self.assertIn(["systemctl", "--user", "disable", "--now", "sessioncoach-ingest.timer"], self.sys.calls)
        self.assertTrue(all(c[0] == "systemctl" for c in self.sys.calls))


class Cron(Base):
    def test_block_only_touches_its_own_lines(self):
        self.sys.crontab = "MAILTO=me\n0 5 * * * backup.sh\n"
        scheduler.install(self.ingest, "/k/bin/kit", load=True, run=self.sys, be="cron")
        scheduler.install(self.rev, "/k/bin/kit", load=True, run=self.sys, be="cron")
        t = self.sys.crontab
        self.assertTrue(t.startswith("MAILTO=me\n0 5 * * * backup.sh\n"))
        self.assertIn(scheduler.CRON_BEGIN, t)
        self.assertIn("*/30 * * * * ", t)
        self.assertIn("0 8 * * 1,2,5 ", t)  # Mon, Tue, Fri
        # re-install replaces, never duplicates
        scheduler.install(scheduler.Job("ingest", ["ingest"], every_min=15), "/k/bin/kit", run=self.sys, be="cron")
        self.assertEqual(self.sys.crontab.count("# sessioncoach:ingest"), 1)
        self.assertIn("*/15 * * * * ", self.sys.crontab)
        self.assertEqual(scheduler.status(self.rev, run=self.sys, be="cron")["key"], "cal:08:00:mon,tue,fri")
        # uninstall one, then the other: the block disappears and the user's lines stay
        scheduler.uninstall(self.ingest, run=self.sys, bes=["cron"])
        self.assertIn("# sessioncoach:review", self.sys.crontab)
        scheduler.uninstall(self.rev, run=self.sys, bes=["cron"])
        self.assertEqual(self.sys.crontab, "MAILTO=me\n0 5 * * * backup.sh\n")

    def test_backup_before_write_and_percent_escaped(self):
        self.sys.crontab = "0 5 * * * x\n"
        scheduler.install(self.ingest, "/k/bin/kit", run=self.sys, be="cron")
        backups = os.listdir(os.path.join(paths.data_dir(), "backups"))
        self.assertEqual(len(backups), 1)
        line = scheduler.cron_line(self.ingest, ["/p%q/kit"], "/l")
        self.assertIn("/p\\%q/kit", line)

    def test_long_intervals(self):
        def j(n):
            # the 5 schedule fields only: a desktop session adds DBUS_SESSION_BUS_ADDRESS=... after them
            return " ".join(scheduler.cron_line(scheduler.Job("ingest", ["ingest"], every_min=n), ["k"], "/l").split()[:5])

        self.assertEqual(j(120), "0 */2 * * *")
        self.assertEqual(j(1440), "0 0 * * *")

    def test_only_intervals_cron_runs_evenly(self):
        def j(n):
            return " ".join(scheduler.cron_line(scheduler.Job("ingest", ["ingest"], every_min=n), ["k"], "/l").split()[:5])

        self.assertEqual(j(30), "*/30 * * * *")
        self.assertEqual(j(45), "*/30 * * * *")  # */45 runs at :00 and :45; a tie goes to the shorter
        self.assertEqual(j(50), "0 */1 * * *")
        self.assertEqual(j(7), "*/6 * * * *")
        self.assertEqual(j(90), "0 */1 * * *")
        self.assertEqual(j(300), "0 */4 * * *")  # 5 h does not divide a day: 4 h
        self.assertEqual(j(1000), "0 */12 * * *")
        self.assertEqual(j(2880), "0 0 * * *")
        line = scheduler.cron_line(scheduler.Job("ingest", ["ingest"], every_min=90), ["k"], "/l")
        self.assertTrue(line.endswith("# sessioncoach:ingest every:60"), line)  # the tag says what cron really runs
        self.assertEqual(scheduler.cron_note(self.ingest), "")
        self.assertIs(scheduler.cron_job(self.rev), self.rev)  # calendar jobs are as asked

    def test_an_inexpressible_interval_is_said_at_install_and_in_status_without_drift(self):
        job = scheduler.Job("ingest", ["ingest"], every_min=90)
        msg = scheduler.install(job, "/k/bin/kit", run=self.sys, be="cron")
        self.assertIn("every 60 min (cron cannot run every 90)", msg)
        st = scheduler.status(job, run=self.sys, be="cron")
        self.assertEqual(st["key"], job.key())  # what cron can do for alerts.json's value: reinstalling changes nothing
        self.assertEqual(st["next"], "every 60 min (cron cannot run every 90)")
        # another interval in alerts.json is drift, as before
        self.assertEqual(scheduler.status(scheduler.Job("ingest", ["ingest"], every_min=120), run=self.sys, be="cron")["key"],
                         "every:60")
        st = scheduler.status(self.ingest, run=self.sys, be="cron")
        self.assertEqual((st["key"], st["next"]), ("every:60", None))

    def test_a_line_tagged_by_an_older_version_with_an_inexpressible_interval_is_drift(self):
        self.sys.crontab = "%s\n0 */1 * * * /k/bin/kit ingest >> /l 2>&1 # sessioncoach:ingest every:90\n%s\n" % (
            scheduler.CRON_BEGIN, scheduler.CRON_END)
        job = scheduler.Job("ingest", ["ingest"], every_min=90)
        self.assertNotEqual(scheduler.status(job, run=self.sys, be="cron")["key"], job.key())
        scheduler.install(job, "/k/bin/kit", run=self.sys, be="cron")
        self.assertEqual(scheduler.status(job, run=self.sys, be="cron")["key"], job.key())  # reinstalling fixes it
        # an exact interval in an old line still matches
        self.sys.crontab = "%s\n*/30 * * * * /k/bin/kit ingest >> /l 2>&1 # kit:ingest every:30\n%s\n" % (
            scheduler.LEGACY_CRON_BEGIN, scheduler.LEGACY_CRON_END)
        self.assertEqual(scheduler.status(self.ingest, run=self.sys, be="cron")["key"], "every:30")


class JobEnvironment(Base):
    """The data dir, transcript roots and Claude config set in the installing shell reach the scheduled job."""

    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.tmp, "My data 100%")  # created by install: no quote, Windows refuses it
        self.cfg = '/c\\x "q"'
        self.ENV = {"SESSIONCOACH_DATA_DIR": self.data, "KIT_PROJECTS_DIRS": "/p/one:/p/$two", "CLAUDE_CONFIG_DIR": self.cfg}
        p = mock.patch.dict(os.environ, self.ENV)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("KIT_DATA_DIR")

    def test_only_the_set_ones_in_a_fixed_order(self):
        self.assertEqual(scheduler.scheduled_env(), [("SESSIONCOACH_DATA_DIR", self.data), ("KIT_PROJECTS_DIRS", "/p/one:/p/$two"),
                                                     ("CLAUDE_CONFIG_DIR", self.cfg)])
        self.assertEqual(scheduler.scheduled_env({"KIT_DATA_DIR": "/k", "SESSIONCOACH_PROJECTS_DIRS": "", "PATH": "/bin"}),
                         [("KIT_DATA_DIR", "/k")])

    def test_launchd(self):
        doc = plistlib.loads(scheduler.launchd_plist(self.ingest, "/k/bin/sessioncoach", "/l"))
        self.assertEqual(doc["EnvironmentVariables"], self.ENV)
        with mock.patch.dict(os.environ, {k: "" for k in self.ENV}):
            self.assertNotIn("EnvironmentVariables", scheduler.launchd_doc(self.ingest, "/k", "/l"))

    def test_systemd(self):
        service, _ = scheduler.systemd_units(self.ingest, ["x"], "/l")
        want = self.data.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        self.assertIn('Environment="SESSIONCOACH_DATA_DIR=%s"\n' % want, service)
        self.assertIn(' 100%%"\n', service)
        self.assertIn('Environment="KIT_PROJECTS_DIRS=/p/one:/p/$two"\n', service)  # $ is not expanded there
        self.assertIn('Environment="CLAUDE_CONFIG_DIR=/c\\\\x \\"q\\""\n', service)
        self.assertLess(service.index("[Service]"), service.index("Environment="))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/c\n[Install]"}), self.assertRaises(ValueError):
            scheduler.systemd_units(self.ingest, ["x"], "/l")

    def test_cron(self):
        import shlex
        line = scheduler.cron_line(self.ingest, ["/k/kit", "ingest"], "/l")
        self.assertNotIn("100%", line)  # cron reads a bare % as a line break
        cmd = line.split(" # sessioncoach:")[0].split(None, 5)[5].replace("\\%", "%")
        words = shlex.split(cmd)
        for k, v in self.ENV.items():
            self.assertIn("%s=%s" % (k, v), words)
        self.assertLess(words.index("CLAUDE_CONFIG_DIR=" + self.cfg), words.index("/k/kit"))
        msg = scheduler.install(self.ingest, "/k/bin/kit", run=self.sys, be="cron")
        self.assertIn("installed in crontab: */30 * * * * ", msg)
        self.assertNotIn("SESSIONCOACH_DATA_DIR", msg)

    def test_task_scheduler_says_the_task_will_not_see_them(self):
        msg = scheduler.install(self.rev, "C:/k/bin/kit", run=self.sys, be="schtasks")
        self.assertIn("SESSIONCOACH_DATA_DIR, KIT_PROJECTS_DIRS, CLAUDE_CONFIG_DIR", msg)
        self.assertIn("setx", msg)
        with mock.patch.dict(os.environ, {k: "" for k in self.ENV}):
            self.assertNotIn("setx", scheduler.install(self.rev, "C:/k/bin/kit", run=self.sys, be="schtasks"))


class Windows(Base):
    def test_task_xml_is_valid_and_complete(self):
        argv = ["C:/Py/pythonw.exe", "-m", "kit", "review", "--scheduled"]
        x = scheduler.task_xml(self.rev, argv)
        doc = minidom.parseString(x.replace('encoding="UTF-16"', ""))
        self.assertEqual(doc.getElementsByTagName("Command")[0].firstChild.data, "C:/Py/pythonw.exe")
        self.assertEqual(doc.getElementsByTagName("Arguments")[0].firstChild.data, "-m kit review --scheduled")
        for tag in ("Monday", "Tuesday", "Friday"):
            self.assertEqual(len(doc.getElementsByTagName(tag)), 1)
        self.assertEqual(len(doc.getElementsByTagName("Wednesday")), 0)
        self.assertIn("<StartWhenAvailable>true", x)
        self.assertIn("<DisallowStartIfOnBatteries>false", x)
        self.assertIn("<RunLevel>LeastPrivilege", x)
        self.assertIn("T08:00:00", x)
        x2 = scheduler.task_xml(self.ingest, argv)
        self.assertIn("<Interval>PT30M</Interval>", x2)

    def test_task_runs_the_installed_package_with_the_dash_c_launcher(self):
        real = os.path.isfile
        with mock.patch.object(paths, "is_windows", return_value=True), \
                mock.patch.object(paths, "console_script", return_value="C:/v/Scripts/sessioncoach.exe"), \
                mock.patch.object(os.path, "isfile", lambda p: False if str(p).endswith(os.path.join("bin", "sessioncoach")) else real(p)):
            argv = scheduler._argv(self.ingest, None, windowless=True)
            x = scheduler.task_xml(self.ingest, argv)
            doc = minidom.parseString(x.replace('encoding="UTF-16"', ""))
            args = doc.getElementsByTagName("Arguments")[0].firstChild.data
            self.assertEqual(paths.split_command(args), ["-c", paths.RUN_CODE, *self.ingest.args])
            self.assertNotIn("WorkingDirectory", x)

    def test_task_xml_escapes_paths(self):
        x = scheduler.task_xml(self.ingest, ["C:/A & B/py.exe", "-m", "kit"])
        minidom.parseString(x.replace('encoding="UTF-16"', ""))
        self.assertIn("A &amp; B", x)

    def test_install_status_uninstall(self):
        scheduler.install(self.rev, "C:/k/bin/kit", run=self.sys, be="schtasks")
        create = next(c for c in self.sys.calls if c[1] == "/Create")
        self.assertEqual(create[:5], ["schtasks", "/Create", "/F", "/TN", "sessioncoach-review"])
        self.assertTrue(os.path.exists(create[create.index("/XML") + 1]))
        st = scheduler.status(self.rev, run=self.sys, be="schtasks")
        self.assertEqual((st["installed"], st["loaded"], st["key"]), (True, True, "cal:08:00:mon,tue,fri"))
        self.assertIn("8:00", st["next"])
        scheduler.uninstall(self.rev, run=self.sys, bes=["schtasks"])
        self.assertFalse(scheduler.status(self.rev, run=self.sys, be="schtasks")["installed"])

    def test_task_deleted_by_hand_is_reported_not_loaded(self):
        scheduler.install(self.ingest, "C:/k/bin/kit", run=self.sys, be="schtasks")
        self.sys.tasks.clear()
        st = scheduler.status(self.ingest, run=self.sys, be="schtasks")
        self.assertEqual((st["installed"], st["loaded"]), (True, False))

    def test_settings_command_forms(self):
        def short(p):
            return p.replace("Program Files", "PROGRA~1").replace("John Doe", "JOHNDO~1")

        self.assertEqual(paths.win_arg("C:\\Users\\John Doe\\p\\python.exe", short=short), "C:/Users/JOHNDO~1/p/python.exe")
        self.assertEqual(paths.win_arg("-m"), "-m")
        self.assertEqual(paths.win_arg("C:\\Users\\me\\.kit\\statusline-chain.sh"), "C:/Users/me/.kit/statusline-chain.sh")
        self.assertEqual(paths.win_arg("C:/odd (x86)/k", short=lambda p: p), '"C:/odd (x86)/k"')  # unshortenable: quoted
        # the file keeps its long name, so the command is still recognised as sessioncoach's
        def short_all(p):
            return short(p).replace("sessioncoach.exe", "SESSIO~1.EXE")
        cmd = paths.win_arg("C:\\Users\\John Doe\\venv\\Scripts\\sessioncoach.exe", short=short_all)
        self.assertEqual(cmd, "C:/Users/JOHNDO~1/venv/Scripts/sessioncoach.exe")
        self.assertTrue(paths.mentions_us(cmd + " hook prompt"))
        self.assertEqual(paths.win_arg("C:/John Doe/a b", short=short), '"C:/JOHNDO~1/a b"')  # a spaced file name: whole path

    def test_windows_kit_command_uses_current_python(self):
        real = sys.platform
        try:
            sys.platform = "win32"
            self.assertEqual(paths.kit_command("C:/k/bin/kit")[0], sys.executable)
            self.assertEqual(paths.kit_command_str("hook", "prompt", kit_bin="C:/k/bin/kit"),
                             "%s C:/k/bin/kit hook prompt" % paths.win_arg(sys.executable))
        finally:
            sys.platform = real

    def test_chain_argv_on_windows_runs_saved_command_in_git_bash_or_powershell(self):
        chain = os.path.join(self.tmp, "chain.sh")
        with open(chain, "w") as fh:
            fh.write("node C:/s/line.js\n")
        real, gb = sys.platform, limits.git_bash
        try:
            sys.platform = "win32"
            limits.git_bash = lambda: "C:/Git/bin/bash.exe"
            self.assertEqual(limits._chain_argv(chain), ["C:/Git/bin/bash.exe", "-c", "node C:/s/line.js\n"])
            limits.git_bash = lambda: None
            self.assertEqual(limits._chain_argv(chain)[0], "powershell")
        finally:
            sys.platform, limits.git_bash = real, gb

    def test_toast_command_passes_text_through_env_not_quoting(self):
        cmd = limits._toast_cmd()
        self.assertEqual(cmd[0], "powershell")
        self.assertIn("$env:KIT_TOAST_TITLE", cmd[-1])

    def test_a_windows_command_keeps_its_backslashes(self):
        with mock.patch.object(paths, "is_windows", return_value=True):
            self.assertEqual(paths.split_command(r'C:\Py\python.exe -m kit statusline --chain-file "C:\Users\me\a b.sh"'),
                             ["C:\\Py\\python.exe", "-m", "kit", "statusline", "--chain-file", "C:\\Users\\me\\a b.sh"])
            self.assertEqual(limits._chain_of(r"C:\Py\python.exe -m kit statusline --chain-file C:\k\c.sh"), "C:\\k\\c.sh")
        with mock.patch.object(paths, "is_windows", return_value=False):
            self.assertEqual(paths.split_command("a 'b c'"), ["a", "b c"])

    def test_old_posix_commands_still_recognised(self):
        self.assertTrue(nudge._is_ours("/usr/bin/python3 /old/bin/kit hook prompt"))
        self.assertTrue(limits._is_ours("/usr/bin/python3 /old/bin/kit statusline --chain-file /h/.kit/c.sh"))
        self.assertTrue(limits._is_ours("C:/Py/python.exe -m kit statusline --chain-file C:/Users/me/.kit/c.sh"))
        self.assertEqual(limits._chain_of("C:/Py/python.exe -m kit statusline --chain-file C:/Users/me/.kit/c.sh"), "C:/Users/me/.kit/c.sh")
        self.assertIsNone(installer.command_problem("%s -m kit hook prompt" % sys.executable))

    def test_encode_dir_matches_claude_code_naming(self):
        from sessioncoach.collect import encode_dir
        self.assertEqual(encode_dir("C:\\Users\\me\\code\\app"), "C--Users-me-code-app")
        self.assertEqual(encode_dir("/Users/me/code/app"), "-Users-me-code-app")
        from sessioncoach.projects import Labeler
        old = paths.HOME
        try:
            paths.HOME = "C:\\Users\\me"
            self.assertEqual(Labeler()("C--Users-me-code-app"), ("code/app", "unassigned"))
        finally:
            paths.HOME = old


class Integration(Base):
    """setup / check / uninstall --all through each non-launchd scheduler, without loading anything."""

    def setup_with(self, be):
        os.environ["KIT_SCHEDULER"] = be
        lines = []
        rc = installer.setup(yes=True, kit_bin=EXISTING, agents_dir=self.units, load=False, settings_path=self.settings,
                             interval=20, time="07:30", out=lines.append)
        return rc, lines

    def test_systemd_setup_check_drift_uninstall(self):
        rc, lines = self.setup_with("systemd")
        self.assertEqual(rc, 0, lines)
        self.assertTrue(os.path.exists(os.path.join(self.units, "sessioncoach-review.timer")))
        self.assertTrue(review.installed(self.units))
        self.assertEqual(read_json(installer.alerts_path())["review"]["time"], "07:30")
        # `check` asks systemctl for state: not available here, so only the file-based parts are asserted
        st = scheduler.status(review.job(review.load_config()), unit_dir=self.units, be="systemd", run=self.sys)
        self.assertEqual(st["key"], review.job(review.load_config()).key())
        installer.update_alerts(lambda c: c.setdefault("review", {}).__setitem__("time", "10:15"))
        self.assertNotEqual(st["key"], review.job(review.load_config()).key())  # drift
        out = installer.uninstall_all(cwd=self.tmp, agents_dir=self.units, load=False)
        self.assertIn("ingest agent: removed", "\n".join(out))
        self.assertFalse(review.installed(self.units))

    def test_cron_check_reports_drift_and_ok(self):
        os.environ["KIT_SCHEDULER"] = "cron"
        installer.ingest_install(20, EXISTING, None, load=False)  # load=False: crontab untouched
        self.assertEqual(self.sys.crontab, "")
        scheduler.install(installer.ingest_job(20), EXISTING, run=self.sys, be="cron")
        scheduler.install(review.job(review.load_config()), EXISTING, run=self.sys, be="cron")
        orig = scheduler._run
        scheduler._run = self.sys
        try:
            items = installer.check(cwd=self.tmp)
            by = {n: s for s, n, _ in items}
            self.assertEqual(by["ingest agent config"], installer.WARN)  # alerts.json says 30, crontab says 20
            self.assertEqual(by["review agent"], installer.OK)
            self.assertNotIn("review agent config", by)
        finally:
            scheduler._run = orig

    def test_cron_check_says_what_cron_runs_for_an_inexpressible_interval(self):
        os.environ["KIT_SCHEDULER"] = "cron"
        installer.update_alerts(lambda c: c.setdefault("ingest", {}).__setitem__("interval_min", 90))
        scheduler.install(installer.ingest_job(90), EXISTING, run=self.sys, be="cron")
        with mock.patch.object(scheduler, "_run", self.sys):
            items = installer.check(cwd=self.tmp)
        by = {n: (s, d) for s, n, d in items}
        self.assertEqual(by["ingest agent"][0], installer.OK)
        self.assertIn("every 60 min (cron cannot run every 90)", by["ingest agent"][1])
        self.assertNotIn("ingest agent config", by)

    def test_no_scheduler_is_a_warning_not_a_failure(self):
        os.environ["KIT_SCHEDULER"] = "none"
        rc, lines = self.setup_with("none")
        self.assertEqual(rc, 0)
        self.assertIn("no scheduler", "\n".join(lines))
        items = installer.check(cwd=self.tmp)
        self.assertNotIn(installer.BAD, [s for s, _, _ in items])

    @unittest.skipUnless(sys.platform != "win32", "POSIX file modes")
    def test_permissions_check_posix(self):
        os.makedirs(paths.data_dir())
        os.environ["KIT_SCHEDULER"] = "none"
        os.chmod(paths.data_dir(), 0o755)
        self.assertEqual([s for s, n, _ in installer.check(cwd=self.tmp) if n == "permissions"], [installer.WARN])

    def test_permissions_check_windows_looks_inside_profile(self):
        os.environ["KIT_SCHEDULER"] = "none"
        os.environ["KIT_DATA_DIR"] = os.path.join(paths.HOME, ".kit")
        os.makedirs(paths.data_dir())
        os.chmod(paths.data_dir(), 0o777)  # irrelevant on Windows
        real = sys.platform
        try:
            sys.platform = "win32"
            got = [s for s, n, _ in installer.check(cwd=self.tmp) if n == "permissions"]
        finally:
            sys.platform = real
        self.assertEqual(got, [installer.OK])
        os.environ["KIT_DATA_DIR"] = os.path.join(self.tmp, "elsewhere")
        os.makedirs(paths.data_dir())
        try:
            sys.platform = "win32"
            got = [s for s, n, _ in installer.check(cwd=self.tmp) if n == "permissions"]
        finally:
            sys.platform = real
        self.assertEqual(got, [installer.WARN])


class LegacyName(Base):
    """Jobs installed while the tool was called daf-claude-kit: recognised, replaced on install, removed on uninstall."""

    def legacy_plist(self, name):
        path = scheduler.plist_path("com.example.daf-claude-kit.%s" % name, self.units)
        os.makedirs(self.units, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(plistlib.dumps({"Label": "com.example.daf-claude-kit.%s" % name}))
        return path

    def test_launchd_legacy_counts_as_installed_and_is_replaced(self):
        os.environ["KIT_SCHEDULER"] = "launchd"
        old_rev, old_ing = self.legacy_plist("review"), self.legacy_plist("ingest")
        self.assertTrue(review.installed(self.units))
        items = installer.check(cwd=self.tmp, agents_dir=self.units, load_fn=lambda label: True, mac=True)
        warned = [n for s, n, d in items if s == installer.WARN and "old name" in d]
        self.assertEqual(sorted(warned), ["ingest agent", "review agent"])
        review.install(review.load_config(), "/k/bin/sessioncoach", self.units, load=False)
        installer.ingest_install(30, "/k/bin/sessioncoach", self.units, load=False)
        self.assertFalse(os.path.exists(old_rev) or os.path.exists(old_ing))
        self.assertTrue(os.path.exists(review.plist_path(self.units)))
        self.assertTrue(os.path.exists(installer.ingest_plist_path(self.units)))
        items = installer.check(cwd=self.tmp, agents_dir=self.units, load_fn=lambda label: True, mac=True)
        self.assertFalse([d for s, n, d in items if "old name" in d])

    def test_launchd_uninstall_removes_both_names(self):
        os.environ["KIT_SCHEDULER"] = "launchd"
        old = self.legacy_plist("review")
        review.install(review.load_config(), "/k/bin/sessioncoach", self.units, load=False)
        self.legacy_plist("review")  # a stray old copy next to the new one
        msg = review.uninstall(self.units, load=False)
        self.assertIn(old, msg)
        self.assertFalse(os.listdir(self.units))
        self.assertEqual(review.uninstall(self.units, load=False), "not installed")

    def test_systemd_legacy_units(self):
        os.makedirs(self.units)
        for ext in ("service", "timer"):
            with open(os.path.join(self.units, "daf-claude-kit-ingest." + ext), "w") as fh:
                fh.write("# kit-schedule: every:30\n")
        st = scheduler.status(self.ingest, unit_dir=self.units, be="systemd", run=self.sys)
        self.assertEqual((st["installed"], st["legacy"], st["key"]), (True, True, "every:30"))
        scheduler.install(self.ingest, "/k/bin/sessioncoach", unit_dir=self.units, load=False, be="systemd")
        self.assertEqual(sorted(os.listdir(self.units)), ["sessioncoach-ingest.service", "sessioncoach-ingest.timer"])
        self.assertFalse(scheduler.status(self.ingest, unit_dir=self.units, be="systemd", run=self.sys)["legacy"])
        for ext in ("service", "timer"):  # stray old copy again: uninstall removes both
            open(os.path.join(self.units, "daf-claude-kit-ingest." + ext), "w").close()
        msg = scheduler.uninstall(self.ingest, unit_dir=self.units, load=False, bes=["systemd"])
        self.assertIn("daf-claude-kit-ingest.timer", msg)
        self.assertEqual(os.listdir(self.units), [])

    def test_cron_legacy_block_is_rewritten_once(self):
        old_line = "*/30 * * * * /usr/bin/python3 /k/bin/kit ingest >> /l 2>&1 # kit:ingest every:30"
        self.sys.crontab = "MAILTO=me\n%s\n%s\n%s\n" % (scheduler.LEGACY_CRON_BEGIN, old_line, scheduler.LEGACY_CRON_END)
        st = scheduler.status(self.ingest, run=self.sys, be="cron")
        self.assertEqual((st["installed"], st["legacy"], st["key"]), (True, True, "every:30"))
        scheduler.install(self.ingest, "/k/bin/sessioncoach", run=self.sys, be="cron")
        t = self.sys.crontab
        self.assertTrue(t.startswith("MAILTO=me\n"))
        # markers and job tags only: the log path comes from TMPDIR/the data dir and may contain anything
        self.assertNotIn(scheduler.LEGACY_CRON_BEGIN, t)
        self.assertNotIn(scheduler.LEGACY_CRON_END, t)
        self.assertEqual(t.count(scheduler.CRON_BEGIN), 1)
        line = scheduler.cron_line_of(t, "ingest")
        self.assertTrue(line.endswith("# sessioncoach:ingest every:30"), line)
        self.assertIn("/k/bin/sessioncoach ingest >> ", line)
        self.assertNotIn("# kit:", t)
        self.assertEqual(t.count("ingest every:30"), 1)
        self.assertFalse(scheduler.status(self.ingest, run=self.sys, be="cron")["legacy"])
        self.sys.crontab = "MAILTO=me\n%s\n%s\n%s\n" % (scheduler.LEGACY_CRON_BEGIN, old_line, scheduler.LEGACY_CRON_END)
        scheduler.uninstall(self.ingest, run=self.sys, bes=["cron"])
        self.assertEqual(self.sys.crontab, "MAILTO=me\n")

    def test_schtasks_legacy_task_is_deleted_on_install(self):
        d = os.path.join(paths.data_dir(), "scheduler")
        os.makedirs(d)
        with open(os.path.join(d, "daf-claude-kit-review.key"), "w") as fh:
            fh.write("cal:08:00:mon,tue,fri\n")
        self.sys.tasks.add("daf-claude-kit-review")
        st = scheduler.status(self.rev, run=self.sys, be="schtasks")
        self.assertEqual((st["installed"], st["legacy"]), (True, True))
        scheduler.install(self.rev, "C:/k/bin/sessioncoach", run=self.sys, be="schtasks")
        self.assertEqual(self.sys.tasks, {"sessioncoach-review"})
        self.assertFalse(os.path.exists(os.path.join(d, "daf-claude-kit-review.key")))
        self.assertFalse(scheduler.status(self.rev, run=self.sys, be="schtasks")["legacy"])


class Launchd(Base):
    """launchd is one more scheduler backend: same install/uninstall/status contract, never runs launchctl unloaded."""

    def setUp(self):
        super().setUp()
        os.environ["KIT_SCHEDULER"] = "launchd"
        patcher = mock.patch.object(subprocess, "run", side_effect=AssertionError("launchctl must not run with load=False"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, job, doc):
        path = scheduler.plist_path(scheduler.launchd_label(job.name), self.units)
        os.makedirs(self.units, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(doc if isinstance(doc, bytes) else plistlib.dumps(doc))

    def test_plists_match_the_job(self):
        ing = plistlib.loads(scheduler.launchd_plist(self.ingest, "/k/bin/sessioncoach", "/l"))
        self.assertEqual((ing["StartInterval"], ing["RunAtLoad"], ing["Label"]), (1800, True, "sessioncoach.ingest"))
        rev = plistlib.loads(scheduler.launchd_plist(self.rev, "/k/bin/sessioncoach", "/l"))
        self.assertEqual(rev["StartCalendarInterval"], [{"Weekday": w, "Hour": 8, "Minute": 0} for w in (1, 2, 5)])
        self.assertNotIn("RunAtLoad", rev)
        self.assertEqual(rev["ProgramArguments"][-2:], ["review", "--scheduled"])

    def test_install_status_uninstall(self):
        msg = scheduler.install(self.rev, "/k/bin/sessioncoach", "/l", unit_dir=self.units, load=False)
        self.assertIn("sessioncoach.review.plist", msg)
        self.assertTrue(scheduler.installed(self.rev, unit_dir=self.units))
        st = scheduler.status(self.rev, unit_dir=self.units, loaded_fn=lambda label: label == "sessioncoach.review")
        self.assertEqual((st["installed"], st["loaded"], st["key"], st["legacy"]), (True, True, self.rev.key(), False))
        self.assertIn("removed", scheduler.uninstall(self.rev, unit_dir=self.units, load=False))
        self.assertEqual(scheduler.uninstall(self.rev, unit_dir=self.units, load=False), "not installed")

    def test_drift_is_exact_and_unreadable_is_reported(self):
        self.write(self.ingest, {"StartInterval": 1801})
        st = scheduler.status(self.ingest, unit_dir=self.units, loaded_fn=lambda _l: True)
        self.assertNotEqual(st["key"], self.ingest.key())
        self.write(self.rev, {"StartCalendarInterval": {"Weekday": 1, "Hour": 8, "Minute": 0}})
        self.assertNotEqual(scheduler.status(self.rev, unit_dir=self.units, loaded_fn=lambda _l: True)["key"], self.rev.key())
        self.write(self.rev, b"garbage")
        self.assertTrue(scheduler.status(self.rev, unit_dir=self.units, loaded_fn=lambda _l: True)["unreadable"])

    def test_an_unknown_day_is_a_config_error(self):
        bad = scheduler.Job("review", ["review"], hhmm="08:00", days=["xyz"])
        with self.assertRaises(ValueError):
            scheduler.status(bad, unit_dir=self.units, loaded_fn=lambda _l: True)

    def test_launchctl_goes_through_the_injected_run(self):
        uid = mock.patch.object(os, "getuid", create=True, return_value=501)  # os.getuid does not exist on Windows
        uid.start()
        self.addCleanup(uid.stop)
        scheduler.install(self.ingest, "/k/bin/sessioncoach", unit_dir=self.units, load=True, run=self.sys)
        self.assertEqual([c[1] for c in self.sys.calls], ["bootout", "bootstrap"])
        self.assertTrue(scheduler.status(self.ingest, unit_dir=self.units, run=self.sys)["loaded"])
        scheduler.uninstall(self.ingest, unit_dir=self.units, load=True, run=self.sys)
        self.assertEqual([c[1] for c in self.sys.calls], ["bootout", "bootstrap", "print", "bootout"])

    def test_other_backends_never_touch_launch_agents(self):
        self.write(self.ingest, {"StartInterval": 1800})
        os.environ["KIT_SCHEDULER"] = "cron"
        scheduler.uninstall(self.ingest, unit_dir=self.units, load=False)
        scheduler.uninstall(self.ingest, unit_dir=self.units, load=False, bes=["systemd", "cron"])
        self.assertTrue(os.path.exists(scheduler.plist_path("sessioncoach.ingest", self.units)))


class Registry(Base):
    """BACKENDS: one class per scheduler; the public functions only pick one."""

    def test_one_backend_per_label(self):
        self.assertEqual(set(scheduler.BACKENDS), set(scheduler.LABELS))
        for name, cls in scheduler.BACKENDS.items():
            self.assertEqual(cls.name, name)
            self.assertTrue(issubclass(cls, scheduler.Backend))

    def test_run_is_looked_up_per_call(self):
        self.assertIs(scheduler.CronBackend(self.sys).run, self.sys)
        self.assertIs(scheduler.CronBackend().run, scheduler._run)  # the patched fake from Base
        self.assertIsNone(scheduler.LaunchdBackend().raw)  # launchctl falls through to subprocess.run

    def test_base_is_abstract(self):
        b = scheduler.Backend(self.sys)
        for call in (lambda: b.install(self.ingest, None, "/l", None, False), lambda: b.remove(self.ingest, None, False),
                     lambda: b.installed(self.ingest, None)):
            with self.assertRaises(NotImplementedError):
                call()

    def test_unknown_be_has_no_backend(self):
        with self.assertRaises(RuntimeError) as ctx:
            scheduler.install(self.ingest, "/k/bin/sessioncoach", unit_dir=self.units, be="bogus")
        self.assertIn("no scheduler available", str(ctx.exception))
        st = scheduler.status(self.ingest, unit_dir=self.units, be="bogus")
        self.assertEqual((st["installed"], st["loaded"], st["where"], st["label"]), (False, None, "", self.ingest.unit))
        self.assertFalse(scheduler.installed(self.ingest, unit_dir=self.units, be="bogus"))
        self.assertEqual(self.sys.calls, [])


if __name__ == "__main__":
    unittest.main()
