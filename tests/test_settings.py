"""Settings panel: backend module and HTTP endpoints, always on temporary directories (never the real ~/.claude or ~/.kit)."""
import glob
import http.client
import json
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: F401  (drops SESSIONCOACH_* left in the shell)
from serverauth import session_cookie

from sessioncoach import paths, review, scheduler, server, settings, store
from sessioncoach.collect import encode_dir

TOKEN = "test-token-123"


POSIX = sys.platform != "win32"  # file modes (600/700) are a POSIX notion
# how a path is written into a settings command: shlex-quoted on POSIX, forward slashes on Windows (paths.win_arg)
Q = shlex.quote if POSIX else paths.win_arg
# kit writes /usr/bin/python3 into settings commands when it exists, else the running interpreter (Windows: see test_scheduler.py)
PY = Q(paths._python())


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.home = os.path.join(self.tmp, "home")
        self.kit = os.path.join(self.tmp, "kitdata")
        self.agents = os.path.join(self.tmp, "agents")
        os.makedirs(os.path.join(self.home, ".claude"))
        os.makedirs(self.kit)
        self._env = os.environ.get("KIT_DATA_DIR")
        self._sched = os.environ.get("KIT_SCHEDULER")
        os.environ["KIT_SCHEDULER"] = "launchd"  # LaunchAgent files are what these tests inspect; other schedulers: test_scheduler.py
        os.environ["KIT_DATA_DIR"] = self.kit
        self.proj = os.path.join(self.tmp, "work", "my-proj")
        self.other = os.path.join(self.tmp, "work", "other-proj")
        os.makedirs(self.proj)
        os.makedirs(self.other)
        self.S = settings.Settings(home=self.home, agents_dir=self.agents, load_agent=False, kit_bin="/opt/kit/bin/kit",
                                   projects=lambda: [encode_dir(self.proj)])
        self.gpath = os.path.join(self.home, ".claude", "settings.json")

    def tearDown(self):
        if self._env is None:
            os.environ.pop("KIT_DATA_DIR", None)
        else:
            os.environ["KIT_DATA_DIR"] = self._env
        if self._sched is None:
            os.environ.pop("KIT_SCHEDULER", None)
        else:
            os.environ["KIT_SCHEDULER"] = self._sched
        shutil.rmtree(self.tmp)

    def put(self, path, obj, mode=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj, indent=2) + "\n")
        if mode:
            os.chmod(path, mode)

    def get(self, path):
        with open(path) as fh:
            return fh.read()

    def apply(self, req):
        pv = self.S.preview(req)
        return pv, self.S.apply(dict(req, files=[{"path": f["path"], "before": f["before"], "after": f["after"]} for f in pv["files"]]))


class TestModule(Base):
    def test_another_tools_chained_status_line_is_not_ours(self):
        other = {"statusLine": {"type": "command", "command": "/opt/toolkit/run statusline --chain-file /c.sh"}}
        self.assertFalse(settings.statusline_installed(other))
        self.put(self.gpath, other)
        self.assertEqual(self.S.preview({"scope": "global", "statusline": False})["files"], [])  # nothing of ours to remove
        with self.assertRaises(settings.SettingsError):
            self.S.apply({"scope": "global", "statusline": False, "files": []})
        self.assertEqual(json.loads(self.get(self.gpath)), other)
        self.assertTrue(settings.statusline_installed(
            {"statusLine": {"command": "/usr/bin/python3 /k/bin/kit statusline --chain-file /c.sh"}}))

    def test_preview_does_not_write(self):
        self.put(self.gpath, {"theme": "dark"})
        before = self.get(self.gpath)
        pv = self.S.preview({"scope": "global", "nudge_hook": True, "statusline": True, "cut_rule": True,
                             "alerts": {"five_hour": [50]}})
        self.assertEqual({f["role"] for f in pv["files"]}, {"settings", "chain", "claude_md", "alerts"})
        self.assertTrue(all(f["diff"].startswith(("---", "--- ")) for f in pv["files"]))
        self.assertEqual(self.get(self.gpath), before)
        self.assertEqual(os.listdir(os.path.join(self.home, ".claude")), ["settings.json"])
        self.assertEqual(os.listdir(self.kit), [])
        self.assertFalse(os.path.exists(self.agents))

    def test_apply_writes_exactly_the_preview_and_keeps_other_keys(self):
        other = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
                           "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]},
                 "permissions": {"allow": ["Bash(ls)"]}, "statusLine": {"type": "command", "command": "echo prev"}}
        self.put(self.gpath, other, mode=0o600)
        old = self.get(self.gpath)
        pv, res = self.apply({"scope": "global", "nudge_hook": True, "statusline": True})
        new = json.loads(self.get(self.gpath))
        self.assertEqual(new["permissions"], other["permissions"])
        self.assertEqual(new["hooks"]["Stop"], other["hooks"]["Stop"])
        cmds = [h["command"] for e in new["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
        self.assertEqual(cmds, ["echo mine", PY + " /opt/kit/bin/kit hook prompt"])
        chain = os.path.join(self.kit, "statusline-chain.sh")
        self.assertEqual(new["statusLine"]["command"], PY + " /opt/kit/bin/kit statusline --chain-file " + Q(chain))
        self.assertEqual(self.get(chain), "echo prev\n")
        by_role = {f["role"]: f for f in pv["files"]}
        self.assertEqual(settings.sha(self.get(self.gpath).encode()), by_role["settings"]["after"])
        self.assertEqual(settings.sha(self.get(chain).encode()), by_role["chain"]["after"])
        backups = glob.glob(self.gpath + ".bak-kit-*")
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.get(backups[0]), old)
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(self.gpath).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(chain).st_mode), 0o600)
        self.assertEqual(len(res["applied"]), 2)
        # idempotent: second time there is nothing to do
        self.assertEqual(self.S.preview({"scope": "global", "nudge_hook": True, "statusline": True})["files"], [])

    @unittest.skipUnless(POSIX, "file modes")
    def test_a_settings_file_the_panel_creates_is_private_and_an_existing_one_keeps_its_mode(self):
        old = os.umask(0)
        try:
            self.apply({"scope": "global", "nudge_hook": True})
            self.assertEqual(stat.S_IMODE(os.stat(self.gpath).st_mode), 0o600)
            os.chmod(self.gpath, 0o644)
            self.apply({"scope": "global", "statusline": True})
            self.assertEqual(stat.S_IMODE(os.stat(self.gpath).st_mode), 0o644)
        finally:
            os.umask(old)

    def test_uninstall_restores_previous_statusline_and_removes_only_our_hook(self):
        self.put(self.gpath, {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
                              "statusLine": {"type": "command", "command": "echo prev"}})
        self.apply({"scope": "global", "nudge_hook": True, "statusline": True})
        self.apply({"scope": "global", "nudge_hook": False, "statusline": False})
        s = json.loads(self.get(self.gpath))
        self.assertEqual(s["statusLine"], {"type": "command", "command": "echo prev"})
        self.assertEqual(s["hooks"]["UserPromptSubmit"], [{"hooks": [{"type": "command", "command": "echo mine"}]}])

    def test_the_panel_keeps_the_resume_hook(self):
        from sessioncoach import resume
        resume.install(self.gpath, kit_bin="/opt/kit/bin/sessioncoach")
        entry = json.loads(self.get(self.gpath))["hooks"]["SessionStart"]
        self.apply({"scope": "global", "nudge_hook": True, "statusline": True})
        self.apply({"scope": "global", "nudge_hook": False, "statusline": False})
        self.assertEqual(json.loads(self.get(self.gpath))["hooks"]["SessionStart"], entry)
        self.assertTrue(resume.installed(self.gpath))

    def test_the_panel_keeps_the_idle_guard_hooks(self):
        from sessioncoach import idlecompact, keepctx
        idlecompact.install(self.gpath, kit_bin="/opt/kit/bin/sessioncoach")
        keepctx.install(self.gpath, kit_bin="/opt/kit/bin/sessioncoach")
        hooks = json.loads(self.get(self.gpath))["hooks"]
        self.apply({"scope": "global", "nudge_hook": True, "statusline": True})
        self.apply({"scope": "global", "nudge_hook": False, "statusline": False})
        after = json.loads(self.get(self.gpath))["hooks"]
        for ev in ("PreCompact", "UserPromptExpansion"):
            self.assertEqual(after[ev], hooks[ev])

    def test_missing_settings_file_is_created_and_invalid_json_is_refused(self):
        self.apply({"scope": "global", "nudge_hook": True})
        self.assertIn("hook prompt", self.get(self.gpath))
        self.put(self.gpath, "{not json")
        with self.assertRaises(settings.SettingsError) as c:
            self.S.preview({"scope": "global", "nudge_hook": False})
        self.assertEqual(c.exception.code, 422)
        self.assertEqual(self.get(self.gpath), "{not json")

    def test_stale_file_gives_409(self):
        self.put(self.gpath, {"a": 1})
        req = {"scope": "global", "nudge_hook": True}
        pv = self.S.preview(req)
        self.put(self.gpath, {"a": 2})
        with self.assertRaises(settings.SettingsError) as c:
            self.S.apply(dict(req, files=[{"path": f["path"], "before": f["before"], "after": f["after"]} for f in pv["files"]]))
        self.assertEqual(c.exception.code, 409)
        self.assertEqual(json.loads(self.get(self.gpath)), {"a": 2})
        self.assertEqual(glob.glob(self.gpath + ".bak-kit-*"), [])

    def test_apply_requires_the_previewed_files(self):
        with self.assertRaises(settings.SettingsError) as c:
            self.S.apply({"scope": "global", "nudge_hook": True})
        self.assertEqual(c.exception.code, 400)
        self.put(self.gpath, {})
        pv = self.S.preview({"scope": "global", "nudge_hook": True})
        with self.assertRaises(settings.SettingsError) as c:  # a different change than the previewed one
            self.S.apply({"scope": "global", "statusline": True,
                          "files": [{"path": f["path"], "before": f["before"], "after": f["after"]} for f in pv["files"]]})
        self.assertEqual(c.exception.code, 409)

    def test_project_scope_only_known_projects(self):
        ids = {s["id"]: s for s in self.S.state()["scopes"] if s["kind"] == "project"}
        self.assertEqual(len(ids), 1)
        pid = next(iter(ids))
        self.apply({"scope": pid, "nudge_hook": True, "cut_rule": True, "lang": "en"})
        local = os.path.join(self.proj, ".claude", "settings.local.json")
        self.assertIn("hook prompt", self.get(local))
        self.assertIn(settings.CUT_OPEN, self.get(os.path.join(self.proj, "CLAUDE.local.md")))
        for bad in ("project:0000000000", self.other, "../../etc", "project:" + "a" * 10, 5, None, ["global"]):
            with self.assertRaises(settings.SettingsError) as c:
                self.S.preview({"scope": bad, "nudge_hook": True})
            self.assertEqual(c.exception.code, 400, bad)
        self.assertFalse(os.path.exists(os.path.join(self.other, ".claude")))

    def test_project_is_matched_by_encoded_name_not_by_client_path(self):
        # an existing directory whose encoded name is NOT in the database is never offered
        self.assertNotIn(self.other, [s["label"] for s in self.S.state()["scopes"]])
        self.assertFalse([s for s in self.S.state()["scopes"] if s["kind"] == "project" and "other-proj" in s["label"]])

    def test_an_encoded_name_two_directories_share_gets_no_scope(self):
        # /x/a/b-c and /x/a-b/c both encode to -x-a-b-c: the cut rule must not land in the wrong one
        one, two = os.path.join(self.tmp, "amb", "a", "b-c"), os.path.join(self.tmp, "amb", "a-b", "c")
        os.makedirs(one)
        os.makedirs(two)
        self.assertEqual(encode_dir(one), encode_dir(two))
        S = settings.Settings(home=self.home, agents_dir=self.agents, load_agent=False, kit_bin="/opt/kit/bin/kit",
                              projects=lambda: [encode_dir(one), encode_dir(self.proj)])
        self.assertEqual(sorted(S._project_dirs()), [self.proj])
        # the cwd is known exactly: it is offered even when its encoded name is ambiguous
        S = settings.Settings(home=self.home, agents_dir=self.agents, load_agent=False, kit_bin="/opt/kit/bin/kit",
                              projects=lambda: [encode_dir(one)], cwd=two)
        self.assertEqual(S._project_dirs(), {two: encode_dir(two)})
        if not POSIX:
            return
        # a symlink to the same directory is one candidate, not two
        os.rmdir(two)
        os.symlink(one, two)
        S = settings.Settings(home=self.home, agents_dir=self.agents, load_agent=False, kit_bin="/opt/kit/bin/kit",
                              projects=lambda: [encode_dir(one)])
        self.assertEqual(S._project_dirs(), {one: encode_dir(one)})

    def test_company_scope_uses_the_config_dir_from_accounts(self):
        cfg = os.path.join(self.tmp, "cfg-acme")
        os.makedirs(os.path.join(cfg, "projects"))
        self.put(os.path.join(self.kit, "accounts.json"),
                 {"accounts": {"acme": {"label": "Acme", "roots": [os.path.join(cfg, "projects")]}}})
        self.apply({"scope": "company:acme", "statusline": True, "cut_rule": True})
        self.assertIn("statusLine", self.get(os.path.join(cfg, "settings.json")))
        self.assertIn(settings.CUT_OPEN, self.get(os.path.join(cfg, "CLAUDE.md")))
        # its chain file is its own, not the global one
        self.assertFalse(os.path.exists(os.path.join(self.kit, "statusline-chain.sh")))
        self.assertTrue(glob.glob(os.path.join(self.kit, "chains", "*", "statusline-chain.sh")))
        self.assertFalse(os.path.exists(self.gpath))

    def test_alerts_merge_keeps_private_keys(self):
        self.put(os.path.join(self.kit, "alerts.json"),
                 {"budgets": [{"name": "b", "equiv_usd": 5}], "nudge": {"idle_min": 30, "custom": 1},
                  "review": {"email_command": ["mail", "x"], "max_sessions": 3}, "notifier": ["echo"]}, mode=0o600)
        self.apply({"scope": "global", "alerts": {"five_hour": [60, 80], "nudge": {"idle_min": 45},
                                                   "review": {"time": "07:30", "days": ["sat", "mon"]}}})
        a = json.loads(self.get(os.path.join(self.kit, "alerts.json")))
        self.assertEqual(a["budgets"], [{"name": "b", "equiv_usd": 5}])
        self.assertEqual(a["notifier"], ["echo"])
        self.assertEqual(a["five_hour"], [60, 80])
        self.assertEqual(a["nudge"], {"idle_min": 45, "custom": 1})
        self.assertEqual(a["review"], {"email_command": ["mail", "x"], "max_sessions": 3, "time": "07:30", "days": ["mon", "sat"]})
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.kit, "alerts.json")).st_mode), 0o600)
        self.assertEqual(len(glob.glob(os.path.join(self.kit, "alerts.json.bak-kit-*"))), 1)

    def test_alerts_save_keeps_the_idle_guard_on(self):
        self.put(os.path.join(self.kit, "alerts.json"), {"nudge": {"block": True, "block_min": 90}}, mode=0o600)
        self.assertNotIn("block", self.S.state()["nudge"])
        self.apply({"scope": "global", "alerts": {"nudge": {"idle_min": 45}}})
        self.assertEqual(json.loads(self.get(os.path.join(self.kit, "alerts.json")))["nudge"],
                         {"block": True, "block_min": 90, "idle_min": 45})

    def test_alerts_created_with_private_permissions_and_only_changed_keys(self):
        self.apply({"scope": "global", "alerts": {"five_hour": [75, 90], "context": [70]}})  # five_hour == default
        p = os.path.join(self.kit, "alerts.json")
        self.assertEqual(json.loads(self.get(p)), {"context": [70]})
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_validation(self):
        bad = [{"five_hour": [0]}, {"five_hour": [101]}, {"five_hour": ["75"]}, {"five_hour": [True]}, {"five_hour": 5},
               {"projection": "yes"}, {"lang": "fr"}, {"nope": 1}, {"nudge": {"idle_min": 0}}, {"nudge": {"idle_min": 1.5}},
               {"nudge": {"idle_min": float("nan")}}, {"nudge": {"x": 1}}, {"review": {"time": "25:00"}}, {"review": {"time": "8:00"}},
               {"review": {"days": []}}, {"review": {"days": ["funday"]}}, {"review": {"channels": ["sms"]}},
               {"review": {"channels": ["email"]}}, {"review": {"lookback": "yesterday"}}, {"review": {"max_tips": 99}},
               {"review": {"long_context_tokens": 5}}, {"review": {"enabled": 1}}, {"context_tokens": [10]}, []]
        for a in bad:
            with self.assertRaises(settings.SettingsError, msg=str(a)) as c:
                self.S.preview({"scope": "global", "alerts": a})
            self.assertEqual(c.exception.code, 422)
        for req in ({"nudge_hook": "yes"}, {"surprise": 1}, {"lang": "fr", "cut_rule": True}):
            with self.assertRaises(settings.SettingsError):
                self.S.preview(dict({"scope": "global"}, **req))
        self.assertEqual(os.listdir(self.kit), [])

    def test_bad_thresholds_are_refused_and_shown_as_their_defaults(self):
        for a in ({"review": {"long_context_tokens": "big"}}, {"review": {"long_context_tokens": 0}},
                  {"review": {"long_context_tokens": -1}}, {"five_hour": ["x"]}, {"context": [-5]}, {"seven_day": 80}):
            with self.assertRaises(settings.SettingsError, msg=str(a)) as c:
                self.S.preview({"scope": "global", "alerts": a})
            self.assertEqual(c.exception.code, 422)
        # written by hand: the panel shows (and compares against) the defaults, and leaves the file as it is
        self.put(os.path.join(self.kit, "alerts.json"),
                 json.dumps({"five_hour": "x", "context": [0], "review": {"long_context_tokens": "big", "time": "07:00"}}))
        st = self.S.state()
        self.assertEqual((st["alerts"]["five_hour"], st["alerts"]["context"]), ([75, 90], [85]))
        self.assertEqual((st["review"]["long_context_tokens"], st["review"]["time"]), (300000, "07:00"))
        pv = self.S.preview({"scope": "global", "alerts": {"review": {"long_context_tokens": 300000}}})
        self.assertEqual(pv["files"], [])  # already the effective value: nothing to write

    def test_cut_rule_block_is_idempotent_and_removable(self):
        md = os.path.join(self.home, ".claude", "CLAUDE.md")
        original = "# Mine\n\nKeep this.\n"
        self.put(md, original)
        self.apply({"scope": "global", "cut_rule": True, "lang": "es"})
        once = self.get(md)
        self.assertTrue(once.startswith(original))
        self.assertEqual(once.count(settings.CUT_OPEN), 1)
        self.assertIn("150k", once)
        self.assertEqual(self.S.preview({"scope": "global", "cut_rule": True})["files"], [])  # idempotent
        self.assertEqual(settings.add_cut_rule(once, "en"), once)
        self.apply({"scope": "global", "cut_rule": False})
        self.assertEqual(self.get(md), original)
        self.assertEqual(self.S.preview({"scope": "global", "cut_rule": False})["files"], [])
        self.assertEqual(settings.remove_cut_rule(original), original)
        self.assertEqual(settings.remove_cut_rule(settings.add_cut_rule("", "en")), "")
        self.assertEqual(settings.remove_cut_rule(settings.add_cut_rule("no newline", "en")), "no newline\n")
        # unclosed marker is not treated as our block
        broken = "x\n" + settings.CUT_OPEN + "\ntext\n"
        self.assertFalse(settings.has_cut_rule(broken))
        self.assertEqual(settings.remove_cut_rule(broken), broken)

    def test_cut_rule_written_under_the_old_name_is_recognised(self):
        old_open, old_close = settings.CUT_MARKERS[1]
        self.assertIn("daf-claude-kit", old_open)
        original = "# Mine\n"
        legacy = original + "\n%s\nold text\n%s\n" % (old_open, old_close)
        self.assertTrue(settings.has_cut_rule(legacy))
        self.assertEqual(settings.add_cut_rule(legacy, "es"), legacy)  # never a second block
        self.assertEqual(settings.remove_cut_rule(legacy), original)

    def test_review_schedule_regenerates_launchagent(self):
        self.apply({"scope": "global", "review_agent": True, "alerts": {"review": {"time": "09:15", "days": ["tue"]}}})
        plist = review.plist_path(self.agents)
        self.assertIn("<integer>9</integer>", self.get(plist))
        self.assertTrue(self.S.state()["review_agent"])
        pv, _ = self.apply({"scope": "global", "alerts": {"review": {"time": "06:45"}}})
        self.assertIn("plist", [f["role"] for f in pv["files"]])
        self.assertIn("<integer>6</integer>", self.get(plist))
        self.assertIn("<integer>45</integer>", self.get(plist))
        self.assertEqual(len(glob.glob(plist + ".bak-kit-*")), 1)
        # not installed and not asked: changing the time never installs it
        os.remove(plist)
        pv = self.S.preview({"scope": "global", "alerts": {"review": {"time": "05:00"}}})
        self.assertEqual([f["role"] for f in pv["files"]], ["alerts"])
        pv, _ = self.apply({"scope": "global", "review_agent": False}) if os.path.exists(plist) else (None, None)
        self.assertIsNone(pv)

    def test_review_agent_under_the_old_name_is_shown_and_backed_up(self):
        old = scheduler.plist_path("com.example.daf-claude-kit.review", self.agents)
        self.put(old, plistlib.dumps({"Label": "com.example.daf-claude-kit.review"}).decode())
        self.assertTrue(self.S.state()["review_agent"])
        # changing the time: the new plist is written and the old one retired, with a warning and a backup
        pv, _ = self.apply({"scope": "global", "alerts": {"review": {"time": "06:45"}}})
        self.assertEqual([w["code"] for w in pv["warnings"]], ["legacy_agent_retired"])
        self.assertFalse(os.path.exists(old))
        self.assertEqual(len(glob.glob(old + ".bak-kit-*")), 1)
        self.assertTrue(os.path.exists(review.plist_path(self.agents)))
        # turning it off while only the old one exists: the diff and the backup are about the old file
        os.remove(review.plist_path(self.agents))
        self.put(old, plistlib.dumps({"Label": "com.example.daf-claude-kit.review"}).decode())
        pv, _ = self.apply({"scope": "global", "review_agent": False})
        plist_items = [f for f in pv["files"] if f["role"] == "plist"]
        self.assertEqual([f["path"] for f in plist_items], [old])
        self.assertIsNotNone(plist_items[0]["before"])
        self.assertFalse(os.path.exists(old))
        self.assertFalse(self.S.state()["review_agent"])

    @unittest.skipIf(sys.platform == "win32", "creating symlinks needs a privilege on Windows")
    def test_symlinked_settings_keep_the_link(self):
        real = os.path.join(self.tmp, "dotfiles", "settings.json")
        self.put(real, {"a": 1})
        os.symlink(real, self.gpath)
        self.apply({"scope": "global", "nudge_hook": True})
        self.assertTrue(os.path.islink(self.gpath))
        self.assertIn("hook prompt", self.get(real))

    def test_diff_hides_secret_looking_values(self):
        self.put(self.gpath, {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-very-secret"}})
        pv = self.S.preview({"scope": "global", "nudge_hook": True})
        self.assertNotIn("sk-very-secret", pv["files"][0]["diff"])
        self.assertIn("[redacted]", pv["files"][0]["diff"])
        self.assertIn("sk-very-secret", self.get(self.gpath))  # the real file is untouched by the redaction

    def test_data_dir_override_does_not_leak(self):
        from sessioncoach import paths
        self.put(self.gpath, {})
        self.apply({"scope": "global", "statusline": True})
        self.assertEqual(paths.data_dir(), self.kit)


class TestHttp(Base):
    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.tmp, "metrics.db")
        store.connect(self.db).close()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.db, token=TOKEN, settings=self.S))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, method, path, body=None, token=TOKEN, origin="auto", host="auto", ctype="application/json", raw=None, cookie=True):
        h = {"Host": "127.0.0.1:%d" % self.port if host == "auto" else host}
        if cookie:
            h["Cookie"] = session_cookie(self.httpd)
        if token is not None:
            h["X-Kit-Token"] = token
        if origin == "auto":
            origin = "http://127.0.0.1:%d" % self.port if method == "POST" else None
        if origin is not None:
            h["Origin"] = origin
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None and ctype:
            h["Content-Type"] = ctype
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        try:
            return r.status, json.loads(out)
        except ValueError:
            return r.status, out

    def files(self, pv):
        return [{"path": f["path"], "before": f["before"], "after": f["after"]} for f in pv["files"]]

    def test_html_carries_the_token_and_is_not_frameable(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", "/", headers={"Host": "127.0.0.1:%d" % self.port, "Cookie": session_cookie(self.httpd)})
        r = c.getresponse()
        html = r.read().decode()
        self.assertIn('<meta name="kit-token" content="%s">' % TOKEN, html)
        self.assertNotIn("__KIT_TOKEN__", html)
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))

    def test_token_origin_host_guards(self):
        body = {"scope": "global", "nudge_hook": True}
        for p in ("/api/settings/preview", "/api/settings/apply"):
            self.assertEqual(self.call("POST", p, body, token=None)[0], 403)
            self.assertEqual(self.call("POST", p, body, token="wrong")[0], 403)
            self.assertEqual(self.call("POST", p, body, token="")[0], 403)
            self.assertEqual(self.call("POST", p, body, origin="https://evil.example")[0], 403)
            self.assertEqual(self.call("POST", p, body, origin="http://127.0.0.1:1")[0], 403)
            self.assertEqual(self.call("POST", p, body, origin="http://localhost.evil.example:%d" % self.port)[0], 403)
            self.assertEqual(self.call("POST", p, body, origin="null")[0], 403)
            self.assertEqual(self.call("POST", p, body, origin=None)[0], 403)  # Origin is mandatory
            self.assertEqual(self.call("POST", p, body, host="evil.example")[0], 403)
            self.assertEqual(self.call("POST", p, body, host="evil.example:%d" % self.port)[0], 403)
            self.assertEqual(self.call("POST", p, body, host="127.0.0.1:9")[0], 403)
        self.assertEqual(self.call("GET", "/api/settings", token=None)[0], 403)
        self.assertEqual(self.call("GET", "/api/settings", host="evil.example")[0], 403)
        self.assertEqual(self.call("GET", "/api/settings?anon=1")[0], 403)
        self.assertEqual(self.call("GET", "/api/settings")[0], 200)
        self.assertEqual(os.listdir(self.kit), [])  # nothing written by any rejected request
        self.assertFalse(os.path.exists(self.gpath))

    def test_refresh_requires_the_token(self):
        self.assertEqual(self.call("POST", "/api/refresh", token=None)[0], 403)
        self.assertEqual(self.call("POST", "/api/refresh", token="nope")[0], 403)
        self.assertEqual(self.call("POST", "/api/refresh", origin="https://evil.example")[0], 403)

    def test_content_type_size_and_json(self):
        p = "/api/settings/preview"
        self.assertEqual(self.call("POST", p, {"scope": "global"}, ctype="text/plain")[0], 415)
        self.assertEqual(self.call("POST", p, {"scope": "global"}, ctype="application/x-www-form-urlencoded")[0], 415)
        self.assertEqual(self.call("POST", p, raw=b"{bad", )[0], 400)
        self.assertEqual(self.call("POST", p, raw=b"[1]")[0], 400)
        self.assertEqual(self.call("POST", p, raw=b" " * (server.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.call("POST", p, ctype="application/json; charset=utf-8", raw=b'{"scope":"global"}')[0], 200)
        self.assertEqual(self.call("POST", "/api/settings/other", {"scope": "global"})[0], 404)

    def test_state_preview_apply_flow(self):
        self.put(self.gpath, {"keep": True})
        code, st = self.call("GET", "/api/settings")
        self.assertEqual(code, 200)
        g = next(s for s in st["scopes"] if s["id"] == "global")
        self.assertEqual((g["nudge_hook"], g["statusline"], g["cut_rule"]), (False, False, False))
        body = {"scope": "global", "nudge_hook": True, "lang": "en"}
        before = self.get(self.gpath)
        code, pv = self.call("POST", "/api/settings/preview", body)
        self.assertEqual(code, 200)
        self.assertEqual(self.get(self.gpath), before)
        self.assertIn("+", pv["files"][0]["diff"])
        # apply without the previewed hashes is refused; stale hash gives 409
        self.assertEqual(self.call("POST", "/api/settings/apply", body)[0], 400)
        stale = self.files(pv)
        stale[0]["before"] = "0" * 64
        self.assertEqual(self.call("POST", "/api/settings/apply", dict(body, files=stale))[0], 409)
        self.assertEqual(self.get(self.gpath), before)
        code, res = self.call("POST", "/api/settings/apply", dict(body, files=self.files(pv)))
        self.assertEqual(code, 200)
        self.assertEqual(settings.sha(self.get(self.gpath).encode()), pv["files"][0]["after"])
        self.assertTrue(res["applied"][0]["backup"])
        # the file changes between preview and apply -> 409
        body2 = {"scope": "global", "cut_rule": True}
        code, pv2 = self.call("POST", "/api/settings/preview", body2)
        md = os.path.join(self.home, ".claude", "CLAUDE.md")
        self.put(md, "changed meanwhile\n")
        self.assertEqual(self.call("POST", "/api/settings/apply", dict(body2, files=self.files(pv2)))[0], 409)
        self.assertEqual(self.get(md), "changed meanwhile\n")

    def test_unknown_project_over_http(self):
        for scope in ("project:" + "0" * 10, self.other):
            code, out = self.call("POST", "/api/settings/preview", {"scope": scope, "nudge_hook": True})
            self.assertEqual(code, 400, out)
        self.assertFalse(os.path.exists(os.path.join(self.other, ".claude")))

    def test_database_stays_read_only(self):
        import sqlite3
        before = read_bytes(self.db)
        self.call("POST", "/api/settings/preview", {"scope": "global", "alerts": {"five_hour": [50]}})
        self.assertEqual(read_bytes(self.db), before)
        con = sqlite3.connect("file:%s?mode=ro" % self.db, uri=True)
        con.close()


class TestScopeDecisions(Base):
    """Where the cut rule goes in a project, .gitignore with consent, and duplicated hooks."""

    def pid(self):
        return next(s["id"] for s in self.S.state()["scopes"] if s["kind"] == "project")

    def pstate(self):
        return next(s for s in self.S.state()["scopes"] if s["kind"] == "project")

    def git_init(self):
        import shutil
        if not shutil.which("git"):
            self.skipTest("git not installed")
        subprocess.run(["git", "init", "-q", self.proj], check=True)

    def test_default_destination_is_claude_local_md(self):
        self.apply({"scope": self.pid(), "cut_rule": True})
        self.assertIn(settings.CUT_OPEN, self.get(os.path.join(self.proj, "CLAUDE.local.md")))
        self.assertFalse(os.path.exists(os.path.join(self.proj, "CLAUDE.md")))
        self.assertEqual(self.pstate()["cut_rule_in"], ["local"])

    def test_shared_destination_writes_claude_md_only(self):
        self.apply({"scope": self.pid(), "cut_rule": True, "cut_dest": "shared"})
        self.assertIn(settings.CUT_OPEN, self.get(os.path.join(self.proj, "CLAUDE.md")))
        self.assertFalse(os.path.exists(os.path.join(self.proj, "CLAUDE.local.md")))
        st = self.pstate()
        self.assertEqual((st["cut_rule"], st["cut_rule_in"]), (True, ["shared"]))

    def test_changing_destination_moves_the_block_and_removal_clears_both(self):
        self.put(os.path.join(self.proj, "CLAUDE.md"), "# Team rules\n")
        self.apply({"scope": self.pid(), "cut_rule": True, "cut_dest": "local"})
        self.apply({"scope": self.pid(), "cut_rule": True, "cut_dest": "shared"})
        self.assertNotIn(settings.CUT_OPEN, self.get(os.path.join(self.proj, "CLAUDE.local.md")))
        self.assertIn("# Team rules", self.get(os.path.join(self.proj, "CLAUDE.md")))
        self.assertEqual(self.pstate()["cut_rule_in"], ["shared"])
        self.apply({"scope": self.pid(), "cut_rule": False})
        self.assertEqual(self.get(os.path.join(self.proj, "CLAUDE.md")), "# Team rules\n")
        self.assertEqual(self.pstate()["cut_rule_in"], [])

    def test_bad_destination_and_global_destination_are_refused(self):
        for req in ({"scope": self.pid(), "cut_dest": "everywhere"}, {"scope": "global", "cut_dest": "local"},
                    {"scope": "global", "gitignore": True}, {"scope": self.pid(), "remove_project_hooks": True}):
            with self.assertRaises(settings.SettingsError) as c:
                self.S.preview(dict(req, cut_rule=True))
            self.assertEqual(c.exception.code, 422, req)

    def test_gitignore_is_offered_but_only_written_with_consent(self):
        self.git_init()
        self.assertTrue(self.pstate()["gitignore_needed"])
        gi = os.path.join(self.proj, ".gitignore")
        # no consent: the rule is written, .gitignore is neither touched nor in the diff
        pv, _ = self.apply({"scope": self.pid(), "cut_rule": True})
        self.assertEqual([f["role"] for f in pv["files"]], ["claude_md"])
        self.assertFalse(os.path.exists(gi))
        self.assertTrue(self.pstate()["gitignore_needed"])
        # consent: it is one more previewed change, with the same sha at apply time
        self.put(gi, "node_modules/")
        pv = self.S.preview({"scope": self.pid(), "gitignore": True})
        self.assertEqual([f["role"] for f in pv["files"]], ["gitignore"])
        self.assertIn("+CLAUDE.local.md", pv["files"][0]["diff"])
        self.assertEqual(self.get(gi), "node_modules/")
        self.S.apply({"scope": self.pid(), "gitignore": True, "files": [{k: pv["files"][0][k] for k in ("path", "before", "after")}]})
        self.assertEqual(self.get(gi), "node_modules/\nCLAUDE.local.md\n")
        self.assertEqual(len(glob.glob(gi + ".bak-kit-*")), 1)
        self.assertFalse(self.pstate()["gitignore_needed"])
        self.assertEqual(self.S.preview({"scope": self.pid(), "gitignore": True})["files"], [])

    def test_gitignore_is_not_touched_for_shared_destination_or_without_repo(self):
        pv = self.S.preview({"scope": self.pid(), "cut_rule": True, "gitignore": True})  # no git repo
        self.assertEqual([f["role"] for f in pv["files"]], ["claude_md"])
        self.git_init()
        pv = self.S.preview({"scope": self.pid(), "cut_rule": True, "cut_dest": "shared", "gitignore": True})
        self.assertEqual([f["role"] for f in pv["files"]], ["claude_md"])
        self.assertTrue(pv["files"][0]["path"].endswith("CLAUDE.md"))

    def test_gitignore_apply_is_refused_if_it_changed_since_preview(self):
        self.git_init()
        req = {"scope": self.pid(), "cut_rule": True, "gitignore": True}
        pv = self.S.preview(req)
        self.put(os.path.join(self.proj, ".gitignore"), "changed\n")
        with self.assertRaises(settings.SettingsError) as c:
            self.S.apply(dict(req, files=[{k: f[k] for k in ("path", "before", "after")} for f in pv["files"]]))
        self.assertEqual(c.exception.code, 409)

    def test_hook_in_project_is_refused_when_global_has_it(self):
        self.apply({"scope": "global", "nudge_hook": True})
        with self.assertRaises(settings.SettingsError) as c:
            self.S.preview({"scope": self.pid(), "nudge_hook": True})
        self.assertEqual(c.exception.code, 422)
        self.assertEqual(c.exception.extra["reason"], "hook_duplicate_superior")
        self.assertEqual(self.pstate()["hook_blocked_by"], "global")
        self.assertFalse(os.path.exists(os.path.join(self.proj, ".claude")))
        # the status line is only warned about
        self.apply({"scope": "global", "statusline": True})
        pv = self.S.preview({"scope": self.pid(), "statusline": True})
        self.assertEqual([w["code"] for w in pv["warnings"]], ["statusline_shadows_superior"])

    def acme(self, with_project_hook=False):
        cfg = os.path.join(self.tmp, "cfg-acme")
        os.makedirs(os.path.join(cfg, "projects"))
        self.put(os.path.join(self.kit, "accounts.json"),
                 {"accounts": {"acme": {"label": "Acme", "company": "acme", "roots": [os.path.join(cfg, "projects")]}}})
        self.put(os.path.join(self.kit, "projects.json"), {"rules": [{"match": "*my-proj*", "tenant": "acme"}]})
        return cfg

    def test_hook_in_project_is_refused_when_its_company_has_it_but_not_for_global(self):
        self.acme()
        self.apply({"scope": "company:acme", "nudge_hook": True})
        with self.assertRaises(settings.SettingsError) as c:
            self.S.preview({"scope": self.pid(), "nudge_hook": True})
        self.assertIn("Acme", c.exception.msg)
        # global is not loaded for a company project: having it there does not block
        self.apply({"scope": "company:acme", "nudge_hook": False})
        self.apply({"scope": "global", "nudge_hook": True})
        self.assertIsNone(self.pstate()["hook_blocked_by"])
        self.apply({"scope": self.pid(), "nudge_hook": True})
        self.assertIn("hook prompt", self.get(os.path.join(self.proj, ".claude", "settings.local.json")))

    def test_global_and_company_do_not_block_each_other(self):
        self.acme()
        self.apply({"scope": "global", "nudge_hook": True})
        pv, _ = self.apply({"scope": "company:acme", "nudge_hook": True})
        self.assertEqual(pv["warnings"], [])
        self.assertTrue(next(s for s in self.S.state()["scopes"] if s["id"] == "company:acme")["nudge_hook"])

    def test_installing_in_global_warns_about_projects_and_can_remove_their_hooks(self):
        self.apply({"scope": self.pid(), "nudge_hook": True})
        local = os.path.join(self.proj, ".claude", "settings.local.json")
        g = next(s for s in self.S.state()["scopes"] if s["id"] == "global")
        self.assertEqual([p["id"] for p in g["hook_in_projects"]], [self.pid()])
        pv = self.S.preview({"scope": "global", "nudge_hook": True})
        self.assertEqual(pv["warnings"][0]["code"], "hook_in_projects")
        self.assertEqual([f["role"] for f in pv["files"]], ["settings"])
        self.assertIn("hook prompt", self.get(local))
        pv, _ = self.apply({"scope": "global", "nudge_hook": True, "remove_project_hooks": True})
        self.assertEqual(len(pv["files"]), 2)
        self.assertNotIn("hook prompt", self.get(local))
        self.assertIn("hook prompt", self.get(self.gpath))
        self.assertEqual(len(glob.glob(local + ".bak-kit-*")), 1)


class TestPanelKitBin(Base):
    def test_agent_without_kit_bin_lets_paths_decide_the_command(self):
        S = settings.Settings(home=self.home, agents_dir=self.agents, load_agent=False, kit_bin=None)
        with mock.patch.object(paths, "kit_command", return_value=["/py", "-m", "sessioncoach"]) as kc:
            S.preview({"scope": "global", "review_agent": True})
        self.assertTrue(kc.called)
        self.assertTrue(all(c[0][0] is None for c in kc.call_args_list))


class TestNotUtf8(Base):
    BAD = b"caf\xe9 \xff\n"

    def put_bytes(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    def test_panel_refuses_a_claude_md_that_is_not_utf8(self):
        md = os.path.join(self.home, ".claude", "CLAUDE.md")
        self.put_bytes(md, self.BAD)
        with self.assertRaises(settings.SettingsError) as ctx:
            self.S.preview({"scope": "global", "cut_rule": True})
        self.assertEqual(ctx.exception.code, 422)
        self.assertIn("is not UTF-8", ctx.exception.msg)
        self.assertIn("CLAUDE.md", ctx.exception.msg)
        self.assertEqual(read_bytes(md), self.BAD)

    def test_panel_refuses_a_gitignore_that_is_not_utf8(self):
        pid = next(s["id"] for s in self.S.state()["scopes"] if s["kind"] == "project")
        subprocess.run(["git", "init", "-q", self.proj], check=True)
        gi = os.path.join(self.proj, ".gitignore")
        self.put_bytes(gi, self.BAD)
        with self.assertRaises(settings.SettingsError) as ctx:
            self.S.preview({"scope": pid, "cut_rule": True, "gitignore": True})
        self.assertEqual(ctx.exception.code, 422)
        self.assertIn(".gitignore is not UTF-8", ctx.exception.msg)

    def test_setup_says_so_and_goes_on(self):
        from sessioncoach import installer
        md = os.path.join(self.proj, "CLAUDE.local.md")
        self.put_bytes(md, self.BAD)
        lines = []
        with mock.patch.dict(os.environ, {"KIT_SCHEDULER": "none"}), mock.patch.object(paths, "HOME", self.home):
            rc = installer.setup(yes=True, scope="project", cwd=self.proj, cut_dest="local", out=lines.append,
                                 settings_path=os.path.join(self.proj, ".claude", "settings.local.json"), kit_bin="/opt/kit/bin/kit")
        text = "\n".join(lines)
        self.assertIn("is not UTF-8", text)
        self.assertNotIn("FAILED", text)
        self.assertEqual(rc, 0)
        self.assertEqual(read_bytes(md), self.BAD)
        self.assertIn("hook prompt", self.get(os.path.join(self.proj, ".claude", "settings.local.json")))


class FakeCrontab:
    def __init__(self):
        self.text = ""

    def __call__(self, cmd, **kw):
        r = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["crontab", "-l"]:
            r.stdout, r.returncode = self.text, 0 if self.text else 1
        elif cmd[:2] == ["crontab", "-"]:
            self.text = kw["input"]
        elif cmd[:2] == ["crontab", "-r"]:
            self.text = ""
        return r


class TestPanelOtherSchedulers(Base):
    """The daily review agent is not a launchd plist on Linux or Windows: the panel plans and verifies the job itself."""

    def setUp(self):
        super().setUp()
        os.environ["KIT_SCHEDULER"] = "cron"
        self.cron = FakeCrontab()
        patch = mock.patch.object(scheduler, "_run", self.cron)
        patch.start()
        self.addCleanup(patch.stop)
        self.S.load_agent = True  # the fake crontab is the only thing "loaded"

    def test_installing_the_agent_plans_a_schedule_item_and_writes_the_job(self):
        pv, res = self.apply({"scope": "global", "review_agent": True})
        self.assertEqual([f["role"] for f in pv["files"]], ["schedule"])
        self.assertIn("cron", pv["files"][0]["display"])
        self.assertIn("cal:", pv["files"][0]["diff"])
        self.assertIn("sessioncoach-review", pv["files"][0]["display"])
        self.assertIn("review --scheduled", self.cron.text)
        self.assertFalse(os.path.exists(self.agents))
        self.assertEqual(len(res["applied"]), 1)
        self.assertTrue(self.S.state()["review_agent"])
        self.assertEqual(self.S.preview({"scope": "global", "review_agent": True})["files"], [])  # idempotent

    def test_changing_the_time_regenerates_the_job_and_removing_deletes_it(self):
        self.apply({"scope": "global", "review_agent": True})
        pv, _ = self.apply({"scope": "global", "alerts": {"review": {"time": "07:45"}}})
        self.assertEqual({f["role"] for f in pv["files"]}, {"alerts", "schedule"})
        self.assertIn("45 7 * * ", self.cron.text)
        pv, _ = self.apply({"scope": "global", "review_agent": False})
        self.assertEqual([f["role"] for f in pv["files"]], ["schedule"])
        self.assertNotIn("review --scheduled", self.cron.text)
        self.assertFalse(self.S.state()["review_agent"])

    def test_no_scheduler_is_a_422_not_a_500(self):
        os.environ["KIT_SCHEDULER"] = "none"
        with self.assertRaises(settings.SettingsError) as ctx:
            self.S.preview({"scope": "global", "review_agent": True})
        self.assertEqual(ctx.exception.code, 422)

    def test_failed_verification_keeps_what_was_already_applied(self):
        req = {"scope": "global", "review_agent": True, "alerts": {"five_hour": [50]}}
        pv = self.S.preview(req)
        files = [{k: f[k] for k in ("path", "before", "after")} for f in pv["files"]]
        with mock.patch.object(review, "install", return_value="noop"), self.assertRaises(settings.SettingsError) as ctx:
            self.S.apply(dict(req, files=files))
        self.assertEqual(ctx.exception.code, 500)
        self.assertEqual(len(ctx.exception.extra["applied"]), 1)
        self.assertTrue(ctx.exception.extra["applied"][0]["path"].endswith("alerts.json"))


class TestSimulatedStatuslineCommand(Base):
    def setUp(self):
        super().setUp()
        self.kit = os.path.join(self.tmp, "kit data")  # a space in the real chain path
        os.environ["KIT_DATA_DIR"] = self.kit
        self.chain = os.path.join(self.kit, "statusline-chain.sh")

    def statusline_command(self):
        pv = self.S.preview({"scope": "global", "statusline": True})
        after = next(f for f in pv["files"] if f["role"] == "settings")
        self.apply({"scope": "global", "statusline": True})
        return json.loads(self.get(self.gpath))["statusLine"]["command"], after

    @unittest.skipUnless(POSIX, "POSIX quoting; Windows commands are covered in test_scheduler.py")
    def test_path_with_spaces_is_quoted_on_posix(self):
        cmd, _ = self.statusline_command()
        self.assertEqual(shlex.split(cmd)[-2:], ["--chain-file", self.chain])
        self.assertEqual(cmd, paths.kit_command_str("statusline", "--chain-file", self.chain, kit_bin="/opt/kit/bin/kit"))

    def test_windows_form_uses_the_real_chain_path(self):
        with mock.patch.object(paths, "is_windows", return_value=True):
            cmd, _ = self.statusline_command()
            words = paths.split_command(cmd)
        # the 8.3 form when the chain file already existed, else the path in quotes: both run in Git Bash and PowerShell
        self.assertEqual(words[-2], "--chain-file")
        self.assertTrue(os.path.samefile(words[-1], self.chain), cmd)
        self.assertNotIn("kit-sim-", cmd)

    def test_preview_never_mentions_the_temporary_chain(self):
        pv = self.S.preview({"scope": "global", "statusline": True})
        self.assertNotIn("kit-sim-", "".join(f["diff"] for f in pv["files"]))


if __name__ == "__main__":
    unittest.main()


class TestLegacyCommandInPanel(Base):
    """A hook / status line written under the old name stays 'installed' in the panel; asking to install updates it."""
    OLD_HOOK = PY + " /old/bin/kit hook prompt"

    def old_settings(self, path):
        chain = os.path.join(self.kit, "statusline-chain.sh")
        self.put(chain, "echo prev\n", mode=0o600)
        self.put(path, {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": self.OLD_HOOK, "timeout": 7}]}]},
                        "statusLine": {"type": "command", "command": PY + " /old/bin/kit statusline --chain-file " + Q(chain)}})
        return chain

    def test_still_installed_and_preview_shows_the_update(self):
        chain = self.old_settings(self.gpath)
        st = next(s for s in self.S.state()["scopes"] if s["id"] == "global")
        self.assertTrue(st["nudge_hook"] and st["statusline"])
        self.S.kit_bin = "/new/bin/sessioncoach"
        pv = self.S.preview({"scope": "global", "nudge_hook": True, "statusline": True})
        self.assertEqual([f["role"] for f in pv["files"]], ["settings"])  # the chain file is not part of it
        self.assertIn("-" + '            "command": "%s",' % self.OLD_HOOK, pv["files"][0]["diff"])
        self.assertIn("+" + '            "command": "%s",' % (PY + " /new/bin/sessioncoach hook prompt"), pv["files"][0]["diff"])
        self.assertEqual(self.get(chain), "echo prev\n")

    def test_apply_keeps_timeout_and_chain_then_uninstall_restores(self):
        chain = self.old_settings(self.gpath)
        self.put(self.gpath, dict(json.loads(self.get(self.gpath)), model="x"))
        self.S.kit_bin = "/new/bin/sessioncoach"
        self.apply({"scope": "global", "nudge_hook": True, "statusline": True})
        s = json.loads(self.get(self.gpath))
        hook = s["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertEqual((len(s["hooks"]["UserPromptSubmit"]), hook["timeout"], s["model"]), (1, 7, "x"))
        self.assertEqual(hook["command"], PY + " /new/bin/sessioncoach hook prompt")
        self.assertEqual(s["statusLine"]["command"], PY + " /new/bin/sessioncoach statusline --chain-file " + Q(chain))
        self.assertEqual(self.get(chain), "echo prev\n")
        self.assertEqual(self.S.preview({"scope": "global", "nudge_hook": True, "statusline": True})["files"], [])
        self.apply({"scope": "global", "nudge_hook": False, "statusline": False})
        s = json.loads(self.get(self.gpath))
        self.assertEqual(s["statusLine"], {"type": "command", "command": "echo prev"})
        self.assertNotIn("UserPromptSubmit", s["hooks"])

    def test_not_asking_for_them_changes_nothing_and_project_hook_is_updated_even_with_a_superior(self):
        self.old_settings(self.gpath)
        self.assertEqual(self.S.preview({"scope": "global", "cut_rule": False})["files"], [])
        proj_settings = os.path.join(self.proj, ".claude", "settings.local.json")
        self.put(proj_settings, {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": self.OLD_HOOK}]}]}})
        pid = next(i for i, s in self.S.scopes().items() if s["kind"] == "project")
        pv = self.S.preview({"scope": pid, "nudge_hook": True})  # the superior has the hook too: updating is not a new duplicate
        self.assertEqual([f["path"] for f in pv["files"]], [proj_settings])


class TestSharedHelpers(Base):
    """The decisions `setup`, `check` and the panel share are public helpers; the old private names still resolve."""

    def test_old_private_names_still_resolve(self):
        from sessioncoach import installer, limits, nudge
        self.assertIs(nudge._is_ours, nudge.is_ours)
        self.assertIs(limits._is_ours, limits.is_ours)
        self.assertIs(settings._read, settings.read_bytes)
        self.assertIs(settings._hook_installed, settings.hook_installed)
        self.assertIs(settings._statusline_installed, settings.statusline_installed)
        self.assertIs(settings.Settings._projects_with_hook, settings.Settings.projects_with_hook)
        self.assertIs(installer._known_dir_names, installer.known_dir_names)

    def test_our_commands_tolerates_odd_shapes(self):
        from sessioncoach import limits, nudge
        odd_hooks = ({}, {"hooks": []}, {"hooks": {"UserPromptSubmit": "x"}},
                     {"hooks": {"UserPromptSubmit": ["x", {"hooks": "y"}, {"hooks": [7]}]}})
        for odd in odd_hooks:
            self.assertEqual(nudge.our_commands(odd), [])
            self.assertFalse(settings.hook_installed(odd))
        mine = PY + " /old/bin/kit hook prompt"
        s = {"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "x.sh"}, {"command": mine}]}, {"hooks": [{"command": mine + " "}]}]}}
        self.assertEqual(nudge.our_commands(s), [mine, mine + " "])
        for odd in ({}, {"statusLine": "x"}, {"statusLine": {"command": 5}}, {"statusLine": {"command": "echo hi"}}):
            self.assertIsNone(limits.our_command(odd))
        cmd = PY + " /old/bin/kit statusline --chain-file /c.sh"
        self.assertEqual(limits.our_command({"statusLine": {"command": cmd}}), cmd)

    def test_superior_decisions_and_projects_with_hook_at(self):
        self.put(self.gpath, {"hooks": {"UserPromptSubmit": [{"hooks": [{"command": PY + " /k/bin/kit hook prompt"}]}]}})
        sups = [("company Acme", os.path.join(self.tmp, "none.json")), ("global", self.gpath)]
        self.assertEqual(settings.hook_duplicated_in(sups), "global")
        self.assertIsNone(settings.statusline_shadowed_by(sups))
        self.assertIsNone(settings.hook_duplicated_in([]))
        self.put(os.path.join(self.proj, ".claude", "settings.local.json"),
                 {"hooks": {"UserPromptSubmit": [{"hooks": [{"command": PY + " /k/bin/kit hook prompt"}]}]}})
        self.assertEqual([p["dir"] for p in self.S.projects_with_hook_at(self.gpath)], [os.path.realpath(self.proj)])
        self.assertEqual(self.S.projects_with_hook_at(os.path.join(self.tmp, "elsewhere.json")), [])
