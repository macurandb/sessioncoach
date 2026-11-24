"""projects.json: groups, rules and the Herdr feed, on a temp data dir.

Every function writes atomically and keeps the keys it does not own; `sync_herdr` only ever
replaces the rules it sourced itself, so cli/manual rules and hand-written keys survive.
"""
import json
import os
import tempfile
import unittest

import isolate

from sessioncoach import paths, projects
from sessioncoach.collect import encode_dir


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(isolate.rmtree, self.tmp)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        os.environ["SESSIONCOACH_DATA_DIR"] = self.data
        self.addCleanup(os.environ.pop, "SESSIONCOACH_DATA_DIR", None)

    def path(self):
        return paths.projects_map_path()

    def doc(self):
        return projects.load_doc()

    def write(self, obj):
        with open(self.path(), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)


class Doc(Base):
    def test_load_doc_and_save_keep_unknown_keys(self):
        self.write({"_comment": "mine", "custom": {"x": 1},
                    "rules": [{"match": "a", "project": "p", "tenant": "t"}]})
        projects.add_group("pretty", "Pretty")
        doc = self.doc()
        self.assertEqual(doc["_comment"], "mine")
        self.assertEqual(doc["custom"], {"x": 1})
        self.assertEqual(doc["groups"], {"pretty": {"label": "Pretty"}})
        self.assertEqual(doc["rules"], [{"match": "a", "project": "p", "tenant": "t"}])

    def test_add_group_defaults_id_to_a_slug(self):
        self.assertEqual(projects.add_group(None, "Pretty Co"), "pretty-co")
        self.assertEqual(self.doc()["groups"], {"pretty-co": {"label": "Pretty Co"}})

    def test_save_is_atomic_and_private(self):
        projects.add_group("x", "X")
        self.assertEqual(os.listdir(self.data), ["projects.json"])  # no .tmp or backup left behind
        if os.name != "nt":
            self.assertEqual(os.stat(self.path()).st_mode & 0o777, 0o600)

    def test_group_labels_fall_back_to_the_id(self):
        self.write({"groups": {"pretty": {"label": "Pretty"}, "bare": {}}})
        self.assertEqual(projects.group_labels(), {"pretty": "Pretty", "bare": "bare"})

    def test_group_id_resolves_a_label_to_its_slug(self):
        self.write({"groups": {"pretty": {"label": "Pretty"}}})
        self.assertEqual(projects._group_id(self.doc(), "Pretty"), "pretty")

    def test_groups_block_of_a_bad_shape_is_replaced(self):
        for fn, args in ((projects.add_group, ("x", "X")), (projects.assign, ("acme", "-Users-*")),
                         (projects.rename_group, ("acme", "Acme"))):
            self.write({"groups": "not-a-dict"})
            fn(*args)
            self.assertIsInstance(self.doc()["groups"], dict)

    def test_an_existing_group_entry_of_a_bad_shape_is_replaced(self):
        self.write({"groups": {"pretty": "just-a-string"}})
        projects.add_group("pretty", "Pretty")
        self.assertEqual(self.doc()["groups"]["pretty"], {"label": "Pretty"})
        self.write({"groups": {"pretty": "x"}})
        projects.rename_group("pretty", "P")
        self.assertEqual(self.doc()["groups"]["pretty"], {"label": "P"})

    def test_sync_groups_bad_shapes(self):
        self.assertEqual(projects._sync_groups("nope"), [])
        self.assertEqual(projects._sync_groups({"groups": "nope"}), [])
        self.assertEqual(projects._sync_groups({"groups": [7, {"name": ""}, {"name": "  "}]}), [])
        self.assertEqual(projects._sync_groups({"groups": [{"name": "  "}]}), [])

    def test_apply_sync_replaces_a_bad_groups_block(self):
        doc = {"groups": "x", "rules": [{"match": "a", "tenant": "t", "source": "herdr"}]}
        new = projects._apply_sync(doc, {"groups": [{"name": "Pretty", "folders": [os.path.join(self.tmp, "p")]}]})
        self.assertIsInstance(new["groups"], dict)
        self.assertEqual(new["groups"]["pretty"]["label"], "Pretty")


class Assign(Base):
    def test_folder_becomes_two_rules_and_new_rules_go_first(self):
        self.write({"rules": [{"match": "*", "tenant": "other", "source": "manual"}]})
        folder = os.path.join(self.tmp, "work", "shop")
        gid = projects.assign("acme", folder)
        enc = encode_dir(os.path.abspath(folder))
        self.assertEqual(gid, "acme")
        rules = self.doc()["rules"]
        self.assertEqual([r["match"] for r in rules[:2]], [enc, enc + "-*"])
        self.assertTrue(all(r["tenant"] == "acme" and r["source"] == "cli" for r in rules[:2]))
        self.assertEqual(rules[-1]["tenant"], "other")  # the older rule is kept, later

    def test_assign_a_pattern_is_one_rule(self):
        projects.assign("acme", "-Users-me-code-*")
        self.assertEqual(self.doc()["rules"], [{"match": "-Users-me-code-*", "tenant": "acme", "source": "cli"}])

    def test_assign_replaces_a_rule_with_the_same_match(self):
        folder = os.path.join(self.tmp, "work", "shop")
        enc = encode_dir(os.path.abspath(folder))
        self.write({"rules": [{"match": enc, "tenant": "old", "source": "cli"}]})
        projects.assign("acme", folder)
        rules = self.doc()["rules"]
        self.assertEqual([r["match"] for r in rules].count(enc), 1)
        self.assertEqual(next(r for r in rules if r["match"] == enc)["tenant"], "acme")

    def test_unassign_removes_a_folders_two_rules(self):
        folder = os.path.join(self.tmp, "work", "shop")
        projects.assign("acme", folder)
        self.assertEqual(projects.unassign(folder), 2)
        self.assertEqual(self.doc()["rules"], [])

    def test_rename_group(self):
        projects.add_group("acme", "Acme")
        projects.rename_group("acme", "Acme Corp")
        self.assertEqual(self.doc()["groups"]["acme"]["label"], "Acme Corp")

    def test_remove_refuses_while_in_use_then_force(self):
        projects.assign("acme", os.path.join(self.tmp, "work"))
        with self.assertRaises(ValueError):
            projects.remove_group("acme")
        projects.remove_group("acme", force=True)
        self.assertNotIn("acme", self.doc()["groups"])
        self.assertEqual(self.doc()["rules"], [])


class SyncHerdr(Base):
    def test_replaces_only_herdr_rules_and_is_idempotent(self):
        projects.assign("manual", "-Users-x-*")  # a cli rule: never touched
        folder = os.path.join(self.tmp, "pretty")
        mapping = {"groups": [{"name": "Pretty", "folders": [folder]}]}
        first = projects.sync_herdr(mapping)
        self.assertIn("1 group", first)
        doc = self.doc()
        self.assertEqual(doc["rules"][0]["source"], "cli")  # cli/manual rules keep first-match priority
        self.assertEqual(len([r for r in doc["rules"] if r["source"] == "herdr"]), 2)
        before = json.dumps(doc, sort_keys=True)
        self.assertIn("unchanged", projects.sync_herdr(mapping))
        self.assertEqual(json.dumps(self.doc(), sort_keys=True), before)  # the file was not rewritten

    def test_nested_groups_use_the_leaf_name(self):
        folder = os.path.join(self.tmp, "pretty")
        projects.sync_herdr({"groups": [{"name": "Clients/Pretty", "folders": [folder]}]})
        groups = self.doc()["groups"]
        self.assertIn("pretty", groups)
        self.assertEqual(groups["pretty"]["label"], "Pretty")

    def test_skips_empty_and_home_folders(self):
        folder = os.path.join(self.tmp, "ok")
        mapping = {"groups": [{"name": "X", "folders": ["", "  ", "~", paths.HOME, folder]}]}
        projects.sync_herdr(mapping)
        herdr = [r for r in self.doc()["rules"] if r.get("source") == "herdr"]
        enc = encode_dir(os.path.abspath(folder))
        self.assertEqual([r["match"] for r in herdr], [enc, enc + "-*"])

    def test_an_existing_group_label_is_not_overwritten(self):
        self.write({"groups": {"pretty": {"label": "Pretty (mine)"}}})
        projects.sync_herdr({"groups": [{"name": "Pretty", "folders": [os.path.join(self.tmp, "p")]}]})
        self.assertEqual(self.doc()["groups"]["pretty"]["label"], "Pretty (mine)")


if __name__ == "__main__":
    unittest.main()
