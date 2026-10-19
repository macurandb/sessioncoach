import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import isolate

from sessioncoach import cli, config, expiry, installer, limits, nudge, paths, review, settings


class Config(unittest.TestCase):
    def setUp(self):
        isolate.claude_home(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {"KIT_DATA_DIR": os.path.join(self.tmp, "data")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        for k in ("SESSIONCOACH_LANG", "KIT_LANG", "LANG"):
            os.environ.pop(k, None)

    def write(self, d):
        os.makedirs(paths.data_dir(), exist_ok=True)
        with open(config.alerts_path(), "w", encoding="utf-8") as fh:
            json.dump(d, fh)

    def test_old_names_are_the_same_objects(self):
        self.assertIs(limits.load_alerts, config.load_alerts)
        self.assertIs(limits.DEFAULT_ALERTS, config.DEFAULT_ALERTS)
        self.assertIs(installer.update_alerts, config.update_alerts)
        self.assertIs(installer.alerts_path, config.alerts_path)
        self.assertIs(installer.ingest_interval, config.ingest_interval)
        self.assertEqual(installer.DEFAULT_INTERVAL_MIN, config.DEFAULT_INTERVAL_MIN)
        self.assertIs(nudge.DEFAULTS, config.NUDGE_DEFAULTS)
        self.assertIs(review.DEFAULTS, config.REVIEW_DEFAULTS)
        self.assertIs(expiry.DEFAULTS, config.EXPIRY_DEFAULTS)
        self.assertIs(settings.REVIEW_KEYS, config.REVIEW_KEYS)
        self.assertIs(nudge.message_lang, config.message_lang)

    def test_alerts_path_is_joined_for_the_platform(self):
        self.assertEqual(config.alerts_path(), os.path.join(paths.data_dir(), "alerts.json"))
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["alerts"])
        self.assertIn("config: %s\n" % os.path.join(paths.data_dir(), "alerts.json"), out.getvalue())

    def test_ingest_interval_stays_within_a_day(self):
        self.assertEqual(config.ingest_interval(), 30)
        for v, want in (("x", 30), (None, 30), (0, 1), (-3, 1), (5000, 1440), (15, 15), ("20", 20)):
            self.assertEqual(config.ingest_interval({"ingest": {"interval_min": v}}), want, v)
        self.write({"ingest": {"interval_min": 45}})
        self.assertEqual(config.ingest_interval(), 45)

    def test_review_config_merges_over_the_defaults(self):
        self.write({"lang": "en", "review": {"time": "07:00"}})
        cfg = config.review_config()
        self.assertEqual((cfg["time"], cfg["days"], cfg["lang"]), ("07:00", config.REVIEW_DEFAULTS["days"], "en"))
        self.assertEqual(review.load_config(), cfg)

    def test_one_language_order_for_every_message(self):
        # one order since the three were unified: prompt > SESSIONCOACH_LANG > alerts.json > Claude Code > LANG > en
        self.write({"lang": "en"})
        os.environ["SESSIONCOACH_LANG"] = "es"
        cfg = config.load_alerts()
        self.assertEqual((config.notice_lang(cfg), config.message_lang({}, cfg), config.cut_rule_lang()), ("es",) * 3)
        self.assertEqual(config.message_lang({"prompt": "please go on with the next step"}, cfg), "en")
        del os.environ["SESSIONCOACH_LANG"]
        self.assertEqual((config.notice_lang(cfg), config.message_lang({}, cfg), config.cut_rule_lang()), ("en",) * 3)
        self.write({})
        self.assertEqual((config.notice_lang({}), config.cut_rule_lang()), ("en", "en"))  # the cut rule no longer defaults to es
        os.environ["LANG"] = "es_ES.UTF-8"
        self.assertEqual((config.notice_lang({}), config.message_lang({}, {}), config.cut_rule_lang()), ("es",) * 3)

    def test_a_value_that_is_neither_es_nor_en_falls_through(self):
        os.environ["SESSIONCOACH_LANG"] = "fr"
        os.environ["LANG"] = "es_ES.UTF-8"
        self.assertEqual(config.lang({"lang": "de"}), "es")  # before, alerts.json "de" ended the hooks' search in en
        self.assertEqual(config.lang({"lang": "en"}), "en")

    def test_claude_code_setting_comes_after_alerts_json(self):
        settings = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(paths.HOME, ".claude"), "settings.json")
        os.makedirs(os.path.dirname(settings), exist_ok=True)
        with open(settings, "w", encoding="utf-8") as fh:
            json.dump({"language": "Español"}, fh)
        self.assertEqual(config.lang({"lang": "en"}), "en")
        self.assertEqual(config.lang({}), "es")


class Malformed(Config):
    def write(self, cfg):
        os.makedirs(os.path.dirname(config.alerts_path()), exist_ok=True)
        with open(config.alerts_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    def test_blocks_that_are_not_objects_count_as_not_set(self):
        self.write({"ingest": 3, "review": "notadict", "nudge": 3, "cache_expiry": [1], "budgets": "x", "lang": "en"})
        cfg = config.load_alerts()
        self.assertFalse({"ingest", "review", "nudge", "cache_expiry"} & set(cfg))
        self.assertEqual((cfg["budgets"], cfg["lang"]), ([], "en"))
        self.assertEqual(config.ingest_interval(), config.DEFAULT_INTERVAL_MIN)
        self.assertEqual(config.review_config()["time"], config.REVIEW_DEFAULTS["time"])

    def test_review_numbers_fall_back_but_zero_tips_is_valid(self):
        self.write({"review": {"max_tips": "3", "max_sessions": 0}})
        rc = config.review_config()
        self.assertEqual((rc["max_tips"], rc["max_sessions"]), (config.REVIEW_DEFAULTS["max_tips"], config.REVIEW_DEFAULTS["max_sessions"]))
        self.write({"review": {"max_tips": 0, "max_sessions": 3}})
        self.assertEqual((config.review_config()["max_tips"], config.review_config()["max_sessions"]), (0, 3))


    BAD_BUDGETS = ("x", 3, None, {"name": "a", "equiv_usd": "abc"}, {"name": "b", "equiv_usd": 10, "thresholds": ["80"]},
                   {"name": "c", "equiv_usd": 10, "thresholds": 80}, {"name": "d", "equiv_usd": float("inf")},
                   {"name": "e", "equiv_usd": [5], "thresholds": [50, 0]},
                   {"name": "f", "equiv_usd": "800", "thresholds": [50, 90.5], "scope": "project", "match": "shop"},
                   {"name": "g"})

    def test_budgets_drop_what_would_break_the_check(self):
        with open(os.path.join(self.tmp, "a.json"), "w", encoding="utf-8") as fh:  # json.dump would refuse inf
            fh.write(json.dumps({"budgets": self.BAD_BUDGETS}, allow_nan=True))
        os.makedirs(paths.data_dir(), exist_ok=True)
        os.replace(os.path.join(self.tmp, "a.json"), config.alerts_path())
        self.assertEqual(config.load_alerts()["budgets"], [
            {"name": "a"}, {"name": "b", "equiv_usd": 10}, {"name": "c", "equiv_usd": 10}, {"name": "d"}, {"name": "e"},
            {"name": "f", "equiv_usd": "800", "thresholds": [50, 90.5], "scope": "project", "match": "shop"},
            {"name": "g"}])

    def test_odd_budgets_no_longer_stop_the_scheduled_ingest(self):
        self.write({"budgets": self.BAD_BUDGETS[3:6]})
        env = {"KIT_PROJECTS_DIRS": os.path.join(self.tmp, "none")}
        with mock.patch.dict(os.environ, env), mock.patch.object(limits, "notify") as notify, \
                redirect_stdout(io.StringIO()) as out:
            cli.main(["ingest"])
        self.assertIn('"added"', out.getvalue())
        notify.assert_not_called()


class Imports(unittest.TestCase):
    def test_the_ingest_path_does_not_load_the_installer(self):
        import subprocess
        import sys
        code = ("import sys, sessioncoach.expiry, sessioncoach.alertlog, sessioncoach.config; "
                "print(sorted(m for m in ('sessioncoach.installer', 'sessioncoach.settings') if m in sys.modules))")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True)
        self.assertEqual(r.stdout.strip(), "[]")
        r = subprocess.run([sys.executable, "-c", "import sys, sessioncoach.config; print(sorted(m for m in sys.modules "
                            "if m.startswith('sessioncoach.')))"], cwd=root, capture_output=True, text=True, check=True)
        self.assertEqual(r.stdout.strip(), "['sessioncoach.config', 'sessioncoach.paths']")  # a leaf


if __name__ == "__main__":
    unittest.main()
