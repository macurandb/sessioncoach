"""`sessioncoach herdr-install | herdr-uninstall` and the Herdr panel's `live --demo`.

Everything runs against a temp HOME (paths.HOME) with `HERDR_APP_HOME` dropped, so the real
`~/.config/herdr-app` is never read or written. The shipped mod files live in the package and are
compared by sha256, so a user's edit is told apart from an update without touching any file.
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: F401  (drops SESSIONCOACH_* left in the shell)

from sessioncoach import __file__ as _pkg_init
from sessioncoach import cli, herdr, installer, live, paths

SESSION_KEYS = {"pid", "session_id", "cwd", "project", "status", "model", "context_tokens",
                "last_activity", "cache_ttl_s", "cache_expires_at", "rewrite_usd", "turn_usd",
                "today_usd", "clear_handoff", "source", "match", "account", "account_id", "group"}
VIEW_KEYS = {"version", "now", "lang", "limits", "sessions", "accounts", "groups", "events", "cursor"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved_home = paths.HOME
        self.saved_env = {k: os.environ.pop(k, None) for k in ("HERDR_APP_HOME",)}
        paths.HOME = os.path.join(self.tmp, "home")
        os.makedirs(paths.HOME, exist_ok=True)
        self.addCleanup(self._restore)

    def _restore(self):
        paths.HOME = self.saved_home
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def app(self):
        return os.path.join(paths.HOME, ".config", "herdr-app")

    def mod(self):
        return herdr.mod_dir(self.app())

    def read(self, name):
        with open(os.path.join(self.mod(), name), encoding="utf-8") as fh:
            return fh.read()

    def cfg(self):
        return json.loads(self.read("config.json"))


class Install(Base):
    def test_not_found(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(herdr.install(), 1)
        self.assertIn("Herdr not found (no %s)" % self.app(), out.getvalue())

    def test_install_fresh(self):
        os.makedirs(self.app())
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        for name in herdr.MOD_FILES:
            self.assertTrue(os.path.isfile(os.path.join(self.mod(), name)), name)
        self.assertTrue(os.path.isfile(os.path.join(self.mod(), "config.json")))
        manifest = json.loads(self.read(herdr.MANIFEST))
        self.assertEqual(manifest["version"], __import__("sessioncoach").__version__)
        for name in herdr.MOD_FILES:
            self.assertEqual(manifest["files"][name], herdr._file_hash(self.mod(), name))
        self.assertEqual(self.cfg()["command"], os.path.abspath("/usr/bin/sessioncoach"))  # a drive letter on Windows
        self.assertEqual(self.cfg()["hint_min_tokens"], 300000)

    def test_idempotent(self):
        os.makedirs(self.app())
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
            self.assertEqual(herdr.install(), 0)
        manifest = json.loads(self.read(herdr.MANIFEST))
        for name in herdr.MOD_FILES:
            self.assertEqual(manifest["files"][name], herdr._file_hash(self.mod(), name))

    def test_config_keys_kept_and_command_refreshed(self):
        os.makedirs(self.app())
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        cfg = self.cfg()
        cfg["refresh_s"] = 15
        cfg["toasts"] = False
        cfg["args_extra"] = ["--demo"]
        with open(os.path.join(self.mod(), "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        with mock.patch("shutil.which", return_value="/opt/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        new = self.cfg()
        self.assertEqual(new["command"], os.path.abspath("/opt/bin/sessioncoach"))
        self.assertEqual(new["refresh_s"], 15)
        self.assertIs(new["toasts"], False)
        self.assertEqual(new["args_extra"], ["--demo"])
        self.assertEqual(new["hint_min_tokens"], 300000)
        self.assertNotIn("args_prefix", new)

    def test_mod_files_include_logic_js(self):
        self.assertIn("logic.js", herdr.MOD_FILES)

    def test_upgrade_from_a_three_file_install_adds_logic_js(self):
        # an install made before logic.js shipped has no logic.js and no manifest entry for it:
        # re-running herdr-install must copy it in and rebuild the manifest, not refuse as "edited"
        os.makedirs(self.app())
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        os.remove(os.path.join(self.mod(), "logic.js"))
        manifest = json.loads(self.read(herdr.MANIFEST))
        del manifest["files"]["logic.js"]
        with open(os.path.join(self.mod(), herdr.MANIFEST), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        self.assertTrue(os.path.isfile(os.path.join(self.mod(), "logic.js")))
        self.assertEqual(sorted(json.loads(self.read(herdr.MANIFEST))["files"]), sorted(herdr.MOD_FILES))

    def test_which_missing_uses_sys_executable(self):
        os.makedirs(self.app())
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(herdr.install(), 0)
        cfg = self.cfg()
        self.assertEqual(cfg["command"], sys.executable)
        self.assertEqual(cfg["args_prefix"], ["-m", "sessioncoach"])

    def test_hint_min_tokens_from_alerts(self):
        os.makedirs(self.app())
        os.makedirs(paths.data_dir())
        with open(os.path.join(paths.data_dir(), "alerts.json"), "w", encoding="utf-8") as fh:
            json.dump({"context_tokens": [200000, 500000]}, fh)
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        self.assertEqual(self.cfg()["hint_min_tokens"], 200000)

    def test_refuse_foreign_folder(self):
        os.makedirs(self.app())
        os.makedirs(self.mod())
        with open(os.path.join(self.mod(), "mine.js"), "w", encoding="utf-8") as fh:
            fh.write("// my own mod\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(herdr.install(), 1)
        self.assertIn("refusing", out.getvalue())
        self.assertEqual(self.read("mine.js"), "// my own mod\n")
        self.assertFalse(os.path.isfile(os.path.join(self.mod(), "config.json")))

    def test_refuse_edited_file(self):
        os.makedirs(self.app())
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        with open(os.path.join(self.mod(), "main.js"), "w", encoding="utf-8") as fh:
            fh.write("// edited\n")
        self.assertEqual(herdr.install(), 1)
        self.assertEqual(self.read("main.js"), "// edited\n")

    def test_force_overwrites(self):
        os.makedirs(self.app())
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        with open(os.path.join(self.mod(), "main.js"), "w", encoding="utf-8") as fh:
            fh.write("// edited\n")
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(force=True), 0)
        with open(os.path.join(herdr._shipped(), "main.js"), encoding="utf-8") as fh:
            self.assertEqual(self.read("main.js"), fh.read())

    def test_force_overwrites_a_foreign_folder(self):
        os.makedirs(self.app())
        os.makedirs(self.mod())
        with open(os.path.join(self.mod(), "mine.js"), "w", encoding="utf-8") as fh:
            fh.write("// my own mod\n")
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(force=True), 0)
        self.assertTrue(os.path.isfile(os.path.join(self.mod(), "config.json")))

    def test_corrupt_manifest_is_treated_as_foreign(self):
        os.makedirs(self.app())
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        with open(os.path.join(self.mod(), herdr.MANIFEST), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(herdr.install(), 1)

    def test_herdr_app_home_env(self):
        custom = os.path.join(self.tmp, "custom")
        os.makedirs(custom)
        os.environ["HERDR_APP_HOME"] = custom
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        self.assertTrue(os.path.isfile(os.path.join(herdr.mod_dir(custom), "main.js")))
        self.assertFalse(os.path.exists(self.mod()))

    def test_app_home_override(self):
        os.environ["HERDR_APP_HOME"] = os.path.join(self.tmp, "env")
        override = os.path.join(self.tmp, "override")
        os.makedirs(override)
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(home=override), 0)
        self.assertTrue(os.path.isfile(os.path.join(herdr.mod_dir(override), "main.js")))
        self.assertFalse(os.path.exists(herdr.mod_dir(os.path.join(self.tmp, "env"))))


class Uninstall(Base):
    def test_uninstall_removes_only_ours(self):
        os.makedirs(self.app())
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        with open(os.path.join(self.mod(), "keep.txt"), "w", encoding="utf-8") as fh:
            fh.write("mine\n")
        with open(os.path.join(self.mod(), herdr.SYNC_FILE), "w", encoding="utf-8") as fh:
            fh.write("{}\n")  # staged by the panel: ours too
        for name in herdr.HERDR_WRITES:  # written by Herdr into every mod folder
            with open(os.path.join(self.mod(), name), "w", encoding="utf-8") as fh:
                fh.write("x\n")
        self.assertEqual(herdr.uninstall(), 0)
        self.assertEqual(self.read("keep.txt"), "mine\n")
        self.assertTrue(os.path.isdir(self.mod()))  # folder kept: not empty
        for name in (*herdr.MOD_FILES, "config.json", herdr.SYNC_FILE, herdr.MANIFEST, *herdr.HERDR_WRITES):
            self.assertFalse(os.path.isfile(os.path.join(self.mod(), name)), name)

    def test_uninstall_removes_the_folder_when_empty(self):
        os.makedirs(self.app())
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        self.assertEqual(herdr.uninstall(), 0)
        self.assertFalse(os.path.exists(self.mod()))

    def test_uninstall_leaves_a_foreign_folder_alone(self):
        os.makedirs(self.mod())
        with open(os.path.join(self.mod(), "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(herdr.uninstall(), 0)
        self.assertIn("left as is", out.getvalue())
        self.assertEqual(self.read("config.json"), "{}\n")

    def test_uninstall_ignores_names_we_do_not_ship(self):
        os.makedirs(self.app())
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            self.assertEqual(herdr.install(), 0)
        outside = os.path.join(self.app(), "settings.json")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        manifest = os.path.join(self.mod(), herdr.MANIFEST)
        with open(manifest, encoding="utf-8") as fh:
            m = json.load(fh)
        m["files"]["../../settings.json"] = "x"
        with open(manifest, "w", encoding="utf-8") as fh:
            json.dump(m, fh)
        self.assertEqual(herdr.uninstall(), 0)
        self.assertTrue(os.path.isfile(outside))
        self.assertFalse(os.path.exists(self.mod()))

    def test_uninstall_when_absent(self):
        os.makedirs(self.app())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(herdr.uninstall(), 0)
        self.assertIn("not installed", out.getvalue())


class Check(Base):
    def test_check_states(self):
        self.assertEqual(herdr.check(), [])  # Herdr not found: silent
        os.makedirs(self.app())
        items = herdr.check()
        self.assertEqual([(s, n) for s, n, _ in items], [(herdr.WARN, "Herdr panel")])
        self.assertIn("not installed", items[0][2])
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            herdr.install()
        items = herdr.check()
        self.assertEqual([(s, n) for s, n, _ in items], [(herdr.OK, "Herdr panel")])
        with open(os.path.join(self.mod(), "main.js"), "w", encoding="utf-8") as fh:
            fh.write("// edited\n")
        items = herdr.check()
        self.assertEqual([(s, n) for s, n, _ in items], [(herdr.WARN, "Herdr panel")])
        self.assertIn("outdated", items[0][2])

    def test_installer_check_includes_the_item(self):
        os.makedirs(self.app())
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"):
            herdr.install()
        names = {n for _, n, _ in installer.check()}
        self.assertIn("Herdr panel", names)


class Cli(Base):
    def test_herdr_install_uninstall_cli(self):
        os.makedirs(self.app())
        out = io.StringIO()
        with mock.patch("shutil.which", return_value="/usr/bin/sessioncoach"), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["herdr-install"]), 0)
        self.assertIn("installed the sessioncoach panel", out.getvalue())
        self.assertIn("pane.split", out.getvalue())
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            self.assertEqual(cli.main(["herdr-uninstall"]), 0)
        self.assertIn("removed", out2.getvalue())

    def test_live_demo_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["live", "--demo"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["sessions"]), 3)


class DemoView(Base):
    def test_demo_matches_the_view_contract(self):
        v = live.demo_view(now=1_800_000_000)
        self.assertEqual(set(v), VIEW_KEYS)
        self.assertEqual(v["version"], 1)
        self.assertIsInstance(v["now"], int)
        self.assertIsInstance(v["limits"], dict)
        self.assertIsInstance(v["sessions"], list)
        self.assertIsInstance(v["events"], list)
        self.assertEqual(v["cursor"], 2)
        # 3 neutral sessions, one past 300k, one with an expired cache; limits 22/47; two events
        self.assertEqual(len(v["sessions"]), 3)
        self.assertEqual({s["project"] for s in v["sessions"]}, {"shop", "api", "docs"})
        self.assertTrue(any(s["context_tokens"] > 300000 for s in v["sessions"]))
        self.assertTrue(any(s["cache_expires_at"] < v["now"] for s in v["sessions"]))
        self.assertEqual((v["limits"]["five_hour"]["pct"], v["limits"]["seven_day"]["pct"]), (22.0, 47.0))
        self.assertEqual(len(v["events"]), 2)
        for s in v["sessions"]:
            self.assertEqual(set(s), SESSION_KEYS)
            self.assertEqual(s["match"], "pid")
        for e in v["events"]:
            self.assertEqual(set(e), {"id", "ts", "source", "session_id", "level", "title", "body"})

    def test_demo_reads_nothing_from_disk(self):
        # a HOME with no data: the demo still produces a complete view
        live.demo_view(now=1_800_000_000)
        self.assertEqual(os.listdir(paths.HOME), [])

    def test_demo_accounts_and_groups(self):
        v = live.demo_view(now=1_800_000_000)
        self.assertEqual(v["lang"], "en")
        self.assertEqual([a["key"] for a in v["accounts"]], ["personal", "team"])
        self.assertEqual(v["accounts"][0]["label"], "Personal · Max")
        self.assertEqual((v["accounts"][0]["five_hour"]["pct"], v["accounts"][0]["seven_day"]["pct"]), (12.0, 51.0))
        self.assertEqual((v["accounts"][1]["five_hour"]["pct"], v["accounts"][1]["seven_day"]["pct"]), (3.0, 20.0))
        self.assertEqual([g["id"] for g in v["groups"]], ["acme", "globex", "initech", "unassigned"])
        self.assertEqual([s["group"] for s in v["sessions"]], ["acme", "globex", "unassigned"])


class PackageData(unittest.TestCase):
    def test_mod_files_are_packaged(self):
        pkg = os.path.dirname(os.path.abspath(_pkg_init))
        for name in herdr.MOD_FILES:
            self.assertTrue(os.path.isfile(os.path.join(pkg, "herdr_mod", name)), name)
        with open(os.path.join(os.path.dirname(pkg), "pyproject.toml"), encoding="utf-8") as fh:
            text = fh.read()
        for name in herdr.MOD_FILES:  # listed by name: `herdr-app check` writes other files there
            self.assertIn('"herdr_mod/%s"' % name, text, "pyproject.toml package-data must ship %s" % name)

    def test_staging_path_agrees_with_herdr_sync_file(self):
        # the panel stages its payload at appHome() + SYNC_FILE; herdr.py removes SYNC_FILE on
        # uninstall, so the two constants must name the same file inside the mod folder.
        pkg = os.path.dirname(os.path.abspath(_pkg_init))
        with open(os.path.join(pkg, "herdr_mod", "main.js"), encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r'SYNC_FILE\s*=\s*"([^"]+)"', text)
        self.assertIsNotNone(m, "main.js must define the SYNC_FILE constant")
        self.assertEqual(m.group(1), "/mods/sessioncoach/" + herdr.SYNC_FILE)


if __name__ == "__main__":
    unittest.main()
