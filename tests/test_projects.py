"""projects.json: groups, rules, the Herdr feed and the write lock, on a temp data dir.

Every mutation goes through `edit()` (an exclusive lock), writes atomically and keeps the keys it
does not own; `sync_herdr` only ever replaces the rules it sourced itself, so cli/manual rules and
hand-written keys survive.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import isolate

from sessioncoach import paths, projects, statefile
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
        left = sorted(f for f in os.listdir(self.data) if not f.endswith(".lock"))  # the OS lock's file may stay
        self.assertEqual(left, ["projects.json"])  # no .tmp or backup left behind
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

    def test_apply_sync_replaces_a_bad_groups_block(self):
        doc = {"groups": "x", "rules": [{"match": "a", "tenant": "t", "source": "herdr"}]}
        new = projects._apply_sync(doc, {"groups": [{"name": "Pretty", "folders": [os.path.join(self.tmp, "p")]}]})
        self.assertIsInstance(new["groups"], dict)
        self.assertEqual(new["groups"]["pretty"]["label"], "Pretty")


class InvalidFile(Base):
    """A present-but-broken projects.json is never silently overwritten."""

    def test_missing_file_is_an_empty_doc(self):
        self.assertEqual(projects._read_for_edit(self.path()), {})

    def test_invalid_json_refuses_every_mutation(self):
        for text in ("{broken", "[1, 2]", '"x"', "null"):
            with open(self.path(), "w", encoding="utf-8") as fh:
                fh.write(text)
            for fn, args in ((projects.add_group, ("x", "X")), (projects.assign, ("x", "-a")),
                             (projects.sync_herdr, ({"groups": []},))):
                with self.assertRaises(projects.ProjectsError, msg=text):
                    fn(*args)
            with open(self.path(), encoding="utf-8") as fh:  # the file is untouched
                self.assertEqual(fh.read(), text)

    def test_unreadable_file_refuses(self):
        with open(self.path(), "w", encoding="utf-8") as fh:
            fh.write('{"groups": {}}')
        with mock.patch("builtins.open", side_effect=PermissionError("nope")), self.assertRaises(projects.ProjectsError):
            projects.add_group("x", "X")

    def test_display_still_reads_a_bad_file(self):
        with open(self.path(), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        self.assertEqual(projects.load_doc(), {})
        self.assertEqual(projects.group_labels(), {})


class Lock(Base):
    def test_a_lock_file_left_behind_does_not_block(self):
        # the lock is the OS's, held by an open descriptor: a leftover file from a dead writer is harmless
        with open(self.path() + ".lock", "w", encoding="utf-8") as fh:
            fh.write("left by a crashed writer")
        projects.add_group("a", "A")
        self.assertEqual(self.doc()["groups"], {"a": {"label": "A"}})

    def test_a_held_lock_times_out(self):
        with statefile.locked(self.path() + ".lock", timeout=1) as held:
            self.assertTrue(held)
            with mock.patch.object(projects, "LOCK_TIMEOUT_S", 0.05), self.assertRaises(projects.ProjectsError):
                projects.add_group("a", "A")
        projects.add_group("a", "A")  # released with the block
        self.assertIn("a", self.doc()["groups"])

    def test_concurrent_writers_both_survive(self):
        # writer B must wait for writer A's lock, then read A's result and add its own
        held = threading.Event()
        seen = {}

        def writer_a():
            with projects.edit() as doc:
                projects._groups_of(doc)["a"] = {"label": "A"}
                projects.save_doc(doc)
                held.set()
                time.sleep(0.2)  # hold the lock long enough for B to start and block

        def writer_b():
            held.wait(5)
            with projects.edit() as doc:
                seen["b_saw_a"] = "a" in projects._groups_of(doc)
                projects._groups_of(doc)["b"] = {"label": "B"}
                projects.save_doc(doc)

        ta, tb = threading.Thread(target=writer_a), threading.Thread(target=writer_b)
        ta.start()
        tb.start()
        ta.join(10)
        tb.join(10)
        self.assertEqual(set(self.doc()["groups"]), {"a", "b"})
        self.assertTrue(seen["b_saw_a"])  # B read the document after A wrote it

    def test_a_failing_replace_leaves_the_old_file_intact(self):
        self.write({"groups": {"x": {"label": "X"}}})
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            projects.add_group("y", "Y")
        self.assertEqual(self.doc()["groups"], {"x": {"label": "X"}})

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

    def test_a_folder_with_glob_characters_is_a_folder(self):
        folder = os.path.join(self.tmp, "client [x]")
        projects.assign("acme", folder)
        enc = encode_dir(os.path.abspath(folder))
        self.assertEqual([r["match"] for r in self.doc()["rules"]], [enc, enc + "-*"])

    def test_an_existing_directory_with_glob_characters_is_a_folder(self):
        folder = os.path.join(self.tmp, "client [x]")
        os.makedirs(folder)
        projects.assign("acme", folder)  # a relative path that exists: still a folder
        enc = encode_dir(os.path.abspath(folder))
        self.assertEqual([r["match"] for r in self.doc()["rules"]], [enc, enc + "-*"])

    def test_a_tilde_path_is_a_folder(self):
        projects.assign("acme", "~/work/shop")
        enc = encode_dir(os.path.abspath(os.path.expanduser("~/work/shop")))
        self.assertEqual([r["match"] for r in self.doc()["rules"]], [enc, enc + "-*"])

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
        with self.assertRaises(projects.ProjectsError):
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

    def test_write_suppression_when_unchanged(self):
        folder = os.path.join(self.tmp, "pretty")
        mapping = {"groups": [{"name": "Pretty", "folders": [folder]}]}
        projects.sync_herdr(mapping)
        with mock.patch.object(projects, "save_doc") as save:
            out = projects.sync_herdr(mapping)
        self.assertIn("unchanged", out)
        save.assert_not_called()

    def test_an_empty_groups_list_removes_all_herdr_rules(self):
        folder = os.path.join(self.tmp, "pretty")
        projects.sync_herdr({"groups": [{"name": "Pretty", "folders": [folder]}]})
        projects.assign("manual", "-Users-x-*")
        projects.sync_herdr({"groups": []})
        doc = self.doc()
        self.assertEqual([r for r in doc["rules"] if r["source"] == "herdr"], [])
        self.assertEqual([r for r in doc["rules"] if r["source"] == "cli"], doc["rules"])  # the cli rule is the only one left

    def test_nested_groups_use_the_leaf_name(self):
        folder = os.path.join(self.tmp, "pretty")
        projects.sync_herdr({"groups": [{"name": "Clients/Pretty", "folders": [folder]}]})
        groups = self.doc()["groups"]
        self.assertIn("pretty", groups)
        self.assertEqual(groups["pretty"]["label"], "Pretty")

    def test_same_leaf_name_merges_into_one_group(self):
        a = os.path.join(self.tmp, "a")
        b = os.path.join(self.tmp, "b")
        projects.sync_herdr({"groups": [{"name": "Clients/Pretty", "folders": [a]},
                                        {"name": "Other/Pretty", "folders": [b]}]})
        groups = self.doc()["groups"]
        self.assertEqual(list(groups), ["pretty"])  # one client, not two
        self.assertEqual(groups["pretty"]["label"], "Pretty")

    def test_colliding_slugs_are_disambiguated(self):
        a = os.path.join(self.tmp, "a")
        b = os.path.join(self.tmp, "b")
        projects.sync_herdr({"groups": [{"name": "Pretty", "folders": [a]},
                                        {"name": "Pretty!", "folders": [b]}]})
        self.assertEqual(list(self.doc()["groups"]), ["pretty", "pretty-2"])

    def test_group_ids_are_stable_across_syncs(self):
        # the QA sequence: Pretty -> /a and Pretty! -> /b, then only Pretty! -> /b
        a = os.path.join(self.tmp, "a")
        b = os.path.join(self.tmp, "b")
        projects.sync_herdr({"groups": [{"name": "Pretty", "folders": [a]}, {"name": "Pretty!", "folders": [b]}]})
        self.assertEqual(self.doc()["groups"]["pretty-2"]["herdr_name"], "Pretty!")
        projects.sync_herdr({"groups": [{"name": "Pretty!", "folders": [b]}]})
        doc = self.doc()
        self.assertEqual(doc["groups"]["pretty-2"]["label"], "Pretty!")  # /b keeps its own id
        herdr = [r for r in doc["rules"] if r["source"] == "herdr"]
        self.assertEqual([r["tenant"] for r in herdr], ["pretty-2", "pretty-2"])

    def test_an_adopted_group_keeps_its_folders_after_a_rename(self):
        # the QA sequence: CLI group "Pretty", sync Pretty -> /a, rename the label, sync again
        projects.add_group("pretty", "Pretty")
        a = os.path.join(self.tmp, "a")
        feed = {"groups": [{"name": "Pretty", "folders": [a]}]}
        projects.sync_herdr(feed)
        self.assertEqual(self.doc()["groups"]["pretty"]["herdr_name"], "Pretty")
        projects.rename_group("pretty", "Pretty GmbH")
        projects.sync_herdr(feed)
        doc = self.doc()
        self.assertEqual(list(doc["groups"]), ["pretty"])  # no pretty-2
        self.assertEqual(doc["groups"]["pretty"]["label"], "Pretty GmbH")  # the label the person chose stays
        self.assertEqual({r["tenant"] for r in doc["rules"] if r["source"] == "herdr"}, {"pretty"})

    def test_feed_name_resolves_to_an_existing_group_by_label(self):
        projects.add_group("pretty", "Pretty")  # a CLI group, no herdr_name
        a = os.path.join(self.tmp, "a")
        projects.sync_herdr({"groups": [{"name": "Pretty", "folders": [a]}]})
        doc = self.doc()
        self.assertEqual(list(doc["groups"]), ["pretty"])  # reused, not re-created
        herdr = [r for r in doc["rules"] if r["source"] == "herdr"]
        self.assertEqual([r["tenant"] for r in herdr], ["pretty", "pretty"])

    def test_skips_the_home_folder(self):
        folder = os.path.join(self.tmp, "ok")
        mapping = {"groups": [{"name": "X", "folders": [paths.HOME, folder]}]}
        projects.sync_herdr(mapping)
        herdr = [r for r in self.doc()["rules"] if r.get("source") == "herdr"]
        enc = encode_dir(os.path.abspath(folder))
        self.assertEqual([r["match"] for r in herdr], [enc, enc + "-*"])

    def test_an_existing_group_label_is_not_overwritten(self):
        self.write({"groups": {"pretty": {"label": "Pretty (mine)"}}})
        projects.sync_herdr({"groups": [{"name": "Pretty", "folders": [os.path.join(self.tmp, "p")]}]})
        self.assertEqual(self.doc()["groups"]["pretty"]["label"], "Pretty (mine)")


class SyncValidation(Base):
    def assert_bad(self, mapping):
        with self.assertRaises(projects.SyncError):
            projects.sync_herdr(mapping)
        self.assertEqual(self.doc(), {})  # the file was never touched

    def test_shape_errors(self):
        self.assert_bad("nope")
        self.assert_bad({"groups": "nope"})
        self.assert_bad({"groups": [7]})
        self.assert_bad({"groups": [{"name": ""}]})
        self.assert_bad({"groups": [{"name": "x" * 101}]})
        self.assert_bad({"groups": [{"name": "x", "folders": "nope"}]})
        self.assert_bad({"groups": [{"name": "x", "folders": ["not-absolute"]}]})
        self.assert_bad({"groups": [{"name": "x", "folders": ["/" + "x" * 5000]}]})
        self.assert_bad({"groups": [{"name": "x", "folders": ["/a"] * 1001}]})
        self.assert_bad({"groups": [{"name": "g%d" % i} for i in range(201)]})

    def test_invalid_input_does_not_erase_existing_herdr_rules(self):
        folder = os.path.join(self.tmp, "p")
        projects.sync_herdr({"groups": [{"name": "Pretty", "folders": [folder]}]})
        before = json.dumps(self.doc(), sort_keys=True)
        with self.assertRaises(projects.SyncError):
            projects.sync_herdr({"groups": "bad"})
        self.assertEqual(json.dumps(self.doc(), sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
