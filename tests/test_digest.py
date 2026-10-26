"""Automatic handoff from a transcript (digest.py) and its use after /clear (resume.py), on synthetic transcripts."""
import io
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate

from sessioncoach import cli, digest, idleguard, resume


def user(text, **kw):
    d = {"type": "user", "message": {"role": "user", "content": text}}
    d.update(kw)
    return d


def assistant(mid, blocks, **kw):
    d = {"type": "assistant", "message": {"id": mid, "role": "assistant", "model": "claude-opus-5-5", "content": blocks}}
    d.update(kw)
    return d


def text(t):
    return {"type": "text", "text": t}


def tool(name, **inp):
    return {"type": "tool_use", "id": "t-" + name, "name": name, "input": inp}


def write_lines(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
    return path


SESSION = [
    user("first prompt about the parser"),
    assistant("a1", [text("Looking at it.")]),
    assistant("a1", [tool("Edit", file_path="/w/a.py", old_string="x", new_string="y")]),
    {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "TOOL-OUTPUT"}]}},
    user("<command-name>/model</command-name>"),
    user("caveat", isMeta=True),
    user("<task-notification> done </task-notification>", origin={"kind": "task-notification"}),
    user("REPORT FROM A PEER", origin={"kind": "peer"}),
    user("sidechain prompt", isSidechain=True),
    {"type": "api-request-blob", "message": {"role": "system", "content": [text("SYSTEM PROMPT")]}},
    "not json at all {",
    user([text("<ide_selection>junk</ide_selection>"), text("second prompt: add tests")], origin={"kind": "human"}),
    assistant("a2", [{"type": "thinking", "thinking": "THOUGHTS"}]),
    assistant("a2", [tool("Write", file_path="/w/b.py", content="SECRET CONTENT")]),
    assistant("a2", [tool("NotebookEdit", notebook_path="/w/n.ipynb", new_source="z")]),
    assistant("a2", [tool("Edit", file_path="/w/a.py", old_string="y", new_string="z")]),
    assistant("a2", [tool("TodoWrite", todos=[{"content": "write tests", "status": "in_progress", "activeForm": "x"},
                                              {"content": "update docs", "status": "pending"}])]),
    assistant("a3", [text("Done: tests added."), text("Next: docs.")]),
    assistant("a4", [text("API error")], **{"message": {"model": "<synthetic>", "content": [text("SYNTHETIC")]}}),
]


class Read(unittest.TestCase):
    def setUp(self):
        self.home = isolate.claude_home(self)
        self.path = write_lines(os.path.join(self.home, "p", "s1.jsonl"), SESSION)

    def test_prompts_reply_files_and_todos_without_the_noise(self):
        d = digest.read(self.path)
        self.assertEqual(d["prompts"], ["first prompt about the parser", "second prompt: add tests"])
        self.assertEqual(d["reply"], "Done: tests added.\nNext: docs.")
        self.assertEqual(d["files"], ["/w/a.py", "/w/n.ipynb", "/w/b.py"])
        self.assertEqual([t["content"] for t in d["todos"]], ["write tests", "update docs"])
        self.assertIsNone(d["summary"])
        out = digest.build(self.path)
        for noise in ("TOOL-OUTPUT", "SYSTEM PROMPT", "PEER", "sidechain", "THOUGHTS", "SECRET CONTENT", "SYNTHETIC", "junk",
                      "caveat", "/model"):
            self.assertNotIn(noise, out)
        self.assertIn("- [in_progress] write tests", out)
        self.assertLess(out.index("first prompt"), out.index("second prompt"))

    def test_compaction_summary_goes_first(self):
        write_lines(self.path, [user("old prompt"), user("SUMMARY OF THE WORK", isCompactSummary=True,
                                                          isVisibleInTranscriptOnly=True), user("after compact")])
        d = digest.read(self.path)
        self.assertEqual((d["summary"], d["prompts"]), ("SUMMARY OF THE WORK", ["old prompt", "after compact"]))
        out = digest.build(self.path)
        self.assertTrue(out.startswith("## Claude Code's compaction summary"))

    def test_caps_each_section_and_the_whole(self):
        lines = [user("p%d " % i + "x" * 1000) for i in range(20)]
        lines += [assistant("a", [text("r" * 5000)] + [tool("Write", file_path="/f%d" % i) for i in range(50)])]
        write_lines(self.path, lines)
        out = digest.build(self.path, max_chars=100000)
        self.assertNotIn("p11 ", out)
        self.assertIn("p12 ", out)
        self.assertIn("… and 20 more", out)
        self.assertIn("r" * 100 + " […]", out)
        self.assertNotIn("r" * (digest.REPLY_CHARS + 1), out)
        short = digest.build(self.path, max_chars=2000)
        self.assertLessEqual(len(short), 2000)
        self.assertTrue(short.endswith("[cut here: the full previous session is in %s]" % self.path))

    def test_only_the_tail_is_read_and_a_cut_first_line_is_skipped(self):
        with mock.patch.object(digest, "TAIL_BYTES", 200):
            write_lines(self.path, [user("EARLY " + "x" * 400), user("late prompt")])
            self.assertEqual(digest.read(self.path)["prompts"], ["late prompt"])

    def test_nothing_to_say_or_no_file(self):
        write_lines(self.path, [user("caveat", isMeta=True), "garbage", "[1]"])
        self.assertIsNone(digest.build(self.path))
        self.assertIsNone(digest.build(os.path.join(self.home, "missing.jsonl")))


class Git(unittest.TestCase):
    def setUp(self):
        self.home = isolate.claude_home(self)

    def test_skipped_when_not_a_repo_or_git_is_missing(self):
        self.assertIsNone(digest.git_state(self.home))
        self.assertIsNone(digest.git_state(None))
        self.assertIsNone(digest.git_state(os.path.join(self.home, "nope")))
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(digest.git_state(self.home))
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 1)):
            self.assertIsNone(digest.git_state(self.home))

    def test_branch_and_status_of_a_repo(self):
        def fake(cmd, **kw):
            out = "feat/x\n" if "rev-parse" in cmd else "".join(" M f%d.py\n" % i for i in range(40))
            return subprocess.CompletedProcess(cmd, 0, out, "")
        with mock.patch.object(subprocess, "run", side_effect=fake) as run:
            self.assertEqual(digest.git_state(self.home)[0], "feat/x")
            for argv in [c[0][0] for c in run.call_args_list]:  # read-only, and no fsmonitor daemon from the repo's config
                self.assertEqual(argv[:6], ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", self.home])
            path = write_lines(os.path.join(self.home, "s.jsonl"), [user("hello there")])
            out = digest.build(path, self.home)
        self.assertIn("branch feat/x", out)
        self.assertIn("… and 10 more", out)


class AfterClear(unittest.TestCase):
    def setUp(self):
        self.home = isolate.claude_home(self)
        os.environ["KIT_DATA_DIR"] = os.path.join(self.home, "data")
        self.repo = os.path.join(self.home, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))  # an empty .git: git itself fails, so the git section is skipped
        # ...and resume would then load no handoff file; these tests are about the digest against the file
        p = mock.patch.object(resume, "_tracked", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.old = write_lines(os.path.join(self.home, ".claude", "projects", "-repo", "old.jsonl"), SESSION)
        self.new = os.path.join(os.path.dirname(self.old), "new.jsonl")

    def hold(self, age_s=60):
        e = idleguard.hold({"session_id": "old", "transcript_path": self.old, "cwd": self.repo}, "2026-10-01T10:00:00Z")
        if age_s:
            entries = idleguard.load()
            entries[-1]["ts"] = time.time() - age_s
            idleguard._save(entries)
        return e

    def handoff_file(self, age_s):
        p = os.path.join(self.repo, "docs", "HANDOFF-x.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write("WRITTEN BY CLAUDE")
        t = time.time() - age_s
        os.utime(p, (t, t))
        return p

    def run_hook(self, **kw):
        data = {"source": "clear", "cwd": self.repo, "session_id": "new", "transcript_path": self.new}
        data.update(kw)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))):
            cli.main(["hook", "session-start"])
        return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"] if out.getvalue() else None

    def test_a_held_back_prompt_gives_the_digest_once(self):
        self.hold()
        ctx = self.run_hook()
        self.assertTrue(ctx.startswith("Automatic handoff built by sessioncoach from the previous session's transcript"))
        self.assertIn("Previous transcript: %s." % self.old, ctx)
        self.assertIn("second prompt: add tests", ctx)
        self.assertNotIn("## Git now", ctx)
        self.assertLess(len(ctx), 10000)
        self.assertEqual(idleguard.load(), [])  # consumed
        self.assertIsNone(self.run_hook())

    def test_a_handoff_written_after_the_hold_wins_and_an_older_one_is_named(self):
        self.hold(age_s=600)
        p = self.handoff_file(age_s=60)
        self.assertIn("WRITTEN BY CLAUDE", self.run_hook())
        self.assertEqual(idleguard.load(), [])
        self.hold(age_s=60)
        self.handoff_file(age_s=600)
        ctx = self.run_hook()
        self.assertIn("An older handoff file of this project, written before that pause: %s." % p, ctx)
        self.assertNotIn("WRITTEN BY CLAUDE", ctx)

    def test_a_clear_from_another_window_of_the_project_gives_no_digest_and_keeps_the_entry(self):
        self.hold(age_s=600)
        other = write_lines(os.path.join(os.path.dirname(self.old), "b.jsonl"), [user("B is working")])  # written after the hold
        self.assertIsNone(self.run_hook())
        self.assertEqual([e["session_id"] for e in idleguard.load()], ["old"])
        t = time.time() - 3600  # B idle since before the hold: the /clear is A's
        os.utime(other, (t, t))
        os.makedirs(os.path.join(os.path.dirname(self.old), "old", "subagents"))
        write_lines(os.path.join(os.path.dirname(self.old), "old", "subagents", "x.jsonl"), [user("sub")])
        self.assertIn("Previous transcript: %s." % self.old, self.run_hook())
        self.assertEqual(idleguard.load(), [])

    def test_two_held_sessions_of_the_project_give_no_digest(self):
        self.hold(age_s=600)
        idleguard.hold({"session_id": "b", "transcript_path": os.path.join(os.path.dirname(self.old), "b.jsonl"),
                        "cwd": self.repo}, "2026-10-01T11:00:00Z")
        self.assertIsNone(self.run_hook())
        self.assertEqual(sorted(e["session_id"] for e in idleguard.load()), ["b", "old"])

    def test_the_new_sessions_own_fresh_transcript_does_not_block_the_digest(self):
        self.hold(age_s=600)
        write_lines(self.new, [user("hello")])  # Claude Code may create it before the hook runs
        self.assertIn("Previous transcript: %s." % self.old, self.run_hook())
        self.assertEqual(idleguard.load(), [])

    def test_the_apps_clear_is_a_startup_and_gets_the_digest_when_a_prompt_is_held(self):
        # the VS Code extension and the desktop app start a new session on /clear: source "startup", not "clear"
        self.handoff_file(age_s=resume.FRESH_S + 60)
        self.assertIsNone(self.run_hook(source="startup"))  # a plain new window gets no old handoff
        self.handoff_file(age_s=60)
        self.hold(age_s=120)
        ctx = self.run_hook(source="startup")
        self.assertIn("WRITTEN BY CLAUDE", ctx)  # written after the hold: it wins, as on the CLI's /clear
        self.assertEqual(idleguard.load(), [])
        self.hold(age_s=30)
        ctx = self.run_hook(source="startup", cwd=os.path.join(self.home, "elsewhere"))  # same Claude project dir
        self.assertIn("Previous transcript: %s." % self.old, ctx)
        self.assertIsNone(self.run_hook(source="startup"))

    def test_no_digest_without_a_fresh_entry_of_this_project(self):
        self.hold(age_s=idleguard.PENDING_S + 60)
        self.assertIsNone(self.run_hook())
        other = os.path.join(self.home, "other")
        os.makedirs(other)
        self.hold()
        self.assertIsNone(self.run_hook(cwd=other, transcript_path="/elsewhere/x.jsonl"))
        self.assertIsNone(self.run_hook(source="resume"))
        idleguard.let_through("old", "2026-10-01T10:00:00Z")
        self.assertIsNone(self.run_hook())

    def test_an_unreadable_transcript_falls_back_to_the_handoff_file(self):
        idleguard.hold({"session_id": "old", "transcript_path": "/missing.jsonl", "cwd": self.repo}, "t")
        self.assertIsNone(self.run_hook())
        idleguard.hold({"session_id": "old", "transcript_path": "/missing.jsonl", "cwd": self.repo}, "t")
        self.handoff_file(age_s=3600)
        self.assertIn("WRITTEN BY CLAUDE", self.run_hook())

    def test_without_an_entry_it_still_writes_nothing(self):
        before = sorted(os.path.join(d, f) for d, _, fs in os.walk(self.home) for f in fs)
        self.assertIsNone(resume.evaluate({"source": "clear", "cwd": self.repo}))
        self.assertEqual(sorted(os.path.join(d, f) for d, _, fs in os.walk(self.home) for f in fs), before)


if __name__ == "__main__":
    unittest.main()
