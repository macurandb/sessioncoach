"""The command line, end to end in one process: every subcommand on synthetic data.

Everything runs against a temporary HOME and data dir; the scheduler is cron with a fake crontab, so
nothing can load a real LaunchAgent, timer or task. A class-level guard checks that the real
~/.claude/settings.json, ~/Library/LaunchAgents, ~/.sessioncoach and ~/.kit (old data dir) were not touched.
"""
import contextlib
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: F401  (drops SESSIONCOACH_* left in the shell)

from sessioncoach import __version__, cli, demo, installer, limits, paths, review, scheduler, server

REAL_HOME = os.path.expanduser("~")
GUARDED = [os.path.join(REAL_HOME, ".claude", "settings.json"), os.path.join(REAL_HOME, "Library", "LaunchAgents"),
           os.path.join(REAL_HOME, ".sessioncoach"), os.path.join(REAL_HOME, ".kit")]


# written by the developer's own scheduled ingest while the tests run: only their presence is compared
LIVE = ("metrics.db", "metrics.db-wal", "metrics.db-shm", "logs")


def _fingerprint():
    out = {}
    for p in GUARDED:
        if os.path.isdir(p):
            out[p] = sorted((f, None if f in LIVE else os.path.getmtime(os.path.join(p, f))) for f in os.listdir(p)
                            if not f.endswith(".lock"))  # state-file locks the real hooks create at any time
        elif os.path.exists(p):
            out[p] = os.path.getmtime(p)
    return out


class FakeCrontab:
    def __init__(self):
        self.text = ""

    def __call__(self, cmd, **kw):
        class R:
            returncode, stdout, stderr = 0, "", ""
        r = R()
        if cmd[:2] == ["crontab", "-l"]:
            r.stdout, r.returncode = self.text, 0 if self.text else 1
        elif cmd[:2] == ["crontab", "-"]:
            self.text = kw["input"]
        elif cmd[:2] == ["crontab", "-r"]:
            self.text = ""
        return r


class CliBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = _fingerprint()
        cls.tmp = tempfile.mkdtemp()
        cls.data = os.path.join(cls.tmp, "data")
        cls.db = demo.build(cls.data, days=6, seed=3)
        cls.roots = os.pathsep.join(os.path.join(cls.data, "accounts", a, "projects") for a in demo.ACCOUNTS)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        assert _fingerprint() == cls.before, "a CLI test touched the real ~/.claude, LaunchAgents, ~/.sessioncoach or ~/.kit"

    def setUp(self):
        self.home = tempfile.mkdtemp(dir=self.tmp)
        os.makedirs(os.path.join(self.home, ".claude"))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SESSIONCOACH_", "KIT_")) and k != "CLAUDE_CONFIG_DIR"}
        env.update(SESSIONCOACH_DATA_DIR=self.data, SESSIONCOACH_PROJECTS_DIRS=self.roots, SESSIONCOACH_SCHEDULER="cron")
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cron = FakeCrontab()
        self.addCleanup(setattr, paths, "HOME", paths.HOME)
        paths.HOME = self.home
        self.addCleanup(setattr, scheduler, "_run", scheduler._run)
        scheduler._run = self.cron
        notify = mock.patch.object(limits, "notify")  # demo sessions are recent: ingest may warn about their cache
        self.notified = notify.start()
        self.addCleanup(notify.stop)
        self.assertTrue(paths.data_dir().startswith(self.tmp))

    def run_cli(self, *argv, stdin=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin", io.StringIO(stdin or "")):
            rc = cli.main(list(argv))
        return rc, out.getvalue()

    def settings(self):
        p = os.path.join(self.home, ".claude", "settings.json")
        if not os.path.exists(p):
            return {}
        with open(p) as fh:
            return json.load(fh)


class Reports(CliBase):
    def test_version_and_help(self):
        self.assertEqual(self.run_cli("version"), (0, __version__ + "\n"))
        with contextlib.redirect_stderr(io.StringIO()):
            rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("usage: sessioncoach", out)

    def test_redirected_output_is_utf8_on_an_ansi_console(self):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp1252")  # Windows: `sessioncoach check > out.txt`
        with mock.patch.object(sys, "stdout", out), mock.patch.object(cli, "__version__", "≥ ≈ ✓ ⚠ —"):
            self.assertEqual(cli.main(["version"]), 0)
            out.flush()
        self.assertEqual(raw.getvalue().decode("utf-8").rstrip("\r\n"), "≥ ≈ ✓ ⚠ —")  # \r\n on Windows

    def test_every_command_says_what_it_gives_before_its_syntax(self):
        _p, sub = cli.build_parser()
        self.assertEqual(set(sub.choices), set(cli.VALUE))
        rc, listing = self.run_cli("help")
        self.assertEqual(rc, 0)
        for name, sp in sub.choices.items():
            value = cli.VALUE[name]
            self.assertTrue(value.strip(), name)
            block = listing[listing.index("\n  %s\n" % name):]
            self.assertLess(block.index(value), block.index("sessioncoach %s " % name), name)
            rc, one = self.run_cli("help", name)
            self.assertEqual(rc, 0)
            self.assertTrue(one.startswith(value + "\n\nusage: sessioncoach " + name), one)
            self.assertEqual(one, sp.format_help())
        for name in cli.DETAIL:
            self.assertIn(cli.DETAIL[name].split()[0], self.run_cli("help", name)[1])

    def test_the_readme_gives_every_command_its_value_line(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        for name, value in cli.VALUE.items():
            self.assertTrue("| `%s` | %s |" % (name, value) in readme, "README: regenerate the commands table (%s)" % name)

    def test_help_on_an_unknown_command(self):
        rc, out = self.run_cli("help", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("unknown command 'nope'", out)

    def test_report_with_and_without_filters(self):
        rc, out = self.run_cli("report", "--days", "0")
        self.assertEqual(rc, 0)
        self.assertIn("sessioncoach report", out)
        self.assertIn("where it goes", out)
        self.assertIn("recommendations", out)
        rc, out = self.run_cli("report", "--days", "7", "--tenant", "acme", "--project", "acme")
        self.assertIn("--tenant acme --project acme", out)

    def test_reading_commands_without_data_say_so_and_create_no_database(self):
        empty = tempfile.mkdtemp(dir=self.tmp)
        os.environ["SESSIONCOACH_DATA_DIR"] = empty
        for argv in (["report"], ["day"], ["tools"]):
            rc, out = self.run_cli(*argv)
            self.assertEqual(rc, 1, argv)
            self.assertEqual(out, "No database yet at %s. Run: sessioncoach ingest\n" % paths.db_path(), argv)
        self.assertEqual(os.listdir(empty), [])

    def test_day_says_never_before_the_first_ingest(self):
        from sessioncoach import store
        os.environ["SESSIONCOACH_DATA_DIR"] = tempfile.mkdtemp(dir=self.tmp)
        store.connect(paths.db_path()).close()  # a database with no ingest run in it
        rc, out = self.run_cli("day")
        self.assertEqual(rc, 0)
        self.assertIn("(last ingest never)", out)
        self.assertNotIn("None", out)

    def test_day_today_and_a_given_date(self):
        rc, out = self.run_cli("day")
        self.assertEqual(rc, 0)
        self.assertIn("sessioncoach day", out)
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        rc, out = self.run_cli("day", "--date", yesterday)
        self.assertIn(yesterday, out)
        self.assertNotIn(" at ", out.splitlines()[1])  # the same-time comparison is only for today

    def test_tools_and_verify(self):
        rc, out = self.run_cli("tools", "--days", "0", "--project", "acme")
        self.assertEqual(rc, 0)
        self.assertIn("project ~ acme", out)
        rc, out = self.run_cli("verify")
        self.assertEqual(json.loads(out)["backtest"], "PASS")

    def test_alerts_and_test_notification(self):
        with mock.patch.object(limits, "notify") as notify:
            rc, out = self.run_cli("alerts", "--test")
        self.assertEqual(rc, 0)
        self.assertIn("plan limits (last sample", out)
        self.assertIn("thresholds:", out)
        self.assertIn("test notification sent", out)
        notify.assert_called_once()

    def test_alerts_without_samples(self):
        empty = os.path.join(self.tmp, "empty")
        os.environ["SESSIONCOACH_DATA_DIR"] = empty
        os.makedirs(empty, exist_ok=True)
        _rc, out = self.run_cli("alerts")
        self.assertIn("no sample yet", out)

    def test_ingest_is_idempotent(self):
        rc, out = self.run_cli("ingest")
        self.assertEqual(rc, 0)
        first = json.loads(out)
        self.assertGreater(first["total"]["requests"], 0)
        rc, out = self.run_cli("ingest")
        self.assertEqual(json.loads(out)["total"], first["total"])


class IngestFull(CliBase):
    def test_ingest_skips_unchanged_transcripts_and_full_reads_them_all(self):
        rc, out = self.run_cli("ingest")
        first = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(first["files"]["scanned"], 0)  # demo.build already ingested them and nothing changed
        self.assertEqual(first["files"]["skipped"], first["files"]["files"])
        self.assertGreater(first["files"]["files"], 0)
        rc, out = self.run_cli("ingest", "--full")
        full = json.loads(out)
        self.assertEqual((full["files"]["scanned"], full["files"]["skipped"]), (first["files"]["files"], 0))
        self.assertEqual(full["total"], first["total"])
        self.assertEqual(full["added"], {"req": 0, "cost": 0, "sub": 0})


class Review(CliBase):
    def test_json_and_text(self):
        _rc, out = self.run_cli("review", "--json")
        data = json.loads(out)
        self.assertTrue(data["day"])
        _rc, out = self.run_cli("review")
        self.assertIn("resumes after idle", out)

    def test_no_activity_before_the_date(self):
        _rc, out = self.run_cli("review", "--date", "2000-01-01")
        self.assertIn("review: no activity before 2000-01-01", out)

    def test_scheduled_not_due_does_nothing(self):
        with mock.patch.object(review, "due", return_value=False), mock.patch.object(review, "build") as build:
            _rc, out = self.run_cli("review", "--scheduled")
        self.assertIn("not due", out)
        build.assert_not_called()

    def test_notify_delivers(self):
        with mock.patch.object(review, "deliver", return_value=["notification"]) as deliver:
            _rc, out = self.run_cli("review", "--notify")
        self.assertIn("sent: notification", out)
        self.assertTrue(deliver.call_args.kwargs["force"])


class Install(CliBase):
    def test_hook_and_statusline_install_uninstall(self):
        self.assertIn("installed", self.run_cli("hook-install")[1])
        self.assertIn("hook prompt", json.dumps(self.settings()))
        self.assertIn("removed", self.run_cli("hook-uninstall")[1])
        self.run_cli("statusline-install")
        self.assertIn("statusline --chain-file", self.settings()["statusLine"]["command"])
        self.run_cli("statusline-uninstall")
        self.assertNotIn("statusLine", self.settings())

    def test_scheduled_jobs_go_to_the_fake_crontab(self):
        self.run_cli("ingest-install", "--interval", "15")
        self.assertIn("# sessioncoach:ingest every:15", self.cron.text)
        self.run_cli("review-install")
        self.assertIn("# sessioncoach:review", self.cron.text)
        self.run_cli("review-uninstall")
        self.run_cli("ingest-uninstall")
        self.assertEqual(self.cron.text, "")

    def test_setup_check_uninstall(self):
        rc, out = self.run_cli("setup", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("hook prompt", json.dumps(self.settings()))
        rc, out = self.run_cli("check", "--json")
        names = {i["name"] for i in json.loads(out)["items"]}
        self.assertTrue({"ingest agent", "review agent", "hook [global]"} <= names, names)
        with mock.patch.object(installer, "run_claude_doctor", return_value=(True, "all good")):
            rc, out = self.run_cli("check", "--with-claude")
        self.assertIn("claude doctor:\n  all good", out)
        rc, out = self.run_cli("check")
        self.assertIn("Tip: run `claude doctor`", out)
        self.assertEqual(self.run_cli("uninstall")[0], 2)
        rc, out = self.run_cli("uninstall", "--all")
        self.assertIn("data kept in", out)
        self.assertNotIn("hook prompt", json.dumps(self.settings()))
        self.assertEqual(self.cron.text, "")

    def test_uninstall_purge_asks_and_respects_no(self):
        with mock.patch("builtins.input", return_value="no"):
            _rc, out = self.run_cli("uninstall", "--all", "--purge")
        self.assertIn("purge: cancelled", out)
        self.assertTrue(os.path.exists(self.db))
        with mock.patch("builtins.input", side_effect=EOFError):
            _rc, out = self.run_cli("uninstall", "--all", "--purge")
        self.assertIn("purge: cancelled", out)


class Runtime(CliBase):
    def test_hook_prompt_and_statusline_read_stdin(self):
        rc, _out = self.run_cli("hook", "prompt", stdin=json.dumps({"session_id": "x", "transcript_path": "/nonexistent"}))
        self.assertEqual(rc, 0)
        rc, _out = self.run_cli("statusline", stdin=json.dumps({"model": {"display_name": "Opus"}}))
        self.assertEqual(rc, 0)

    def test_dashboard_and_demo_serve_without_blocking(self):
        with mock.patch.object(server, "serve") as serve:
            self.run_cli("dashboard", "--no-open", "--port", "9")
            self.assertEqual(serve.call_args.kwargs, {"port": 9, "open_browser": False})
            _rc, out = self.run_cli("demo", "--no-open", "--days", "2", "--seed", "1")
        self.assertIn("synthetic data in", out)
        kw = serve.call_args.kwargs
        self.assertTrue(kw["demo"])
        self.assertTrue(kw["db"].startswith(os.path.join(self.data, "demo")))
        # demo re-points the data dir for the rest of its process; the test's environment patch undoes it
        self.assertEqual(paths.data_dir(), os.path.join(self.data, "demo"))


class IngestCatchUp(CliBase):
    def test_a_failing_catch_up_is_logged_to_stderr_and_ingest_still_succeeds(self):
        err = io.StringIO()
        with mock.patch.object(review, "catch_up", side_effect=RuntimeError("boom-in-review")), contextlib.redirect_stderr(err):
            rc, out = self.run_cli("ingest")
        self.assertEqual(rc, 0)
        self.assertIn("db", json.loads(out))
        self.assertIn("boom-in-review", err.getvalue())
        self.assertIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
