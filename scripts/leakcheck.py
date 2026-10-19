#!/usr/bin/env python3
"""Fail if the repo contains any private string (client names, private paths, ids).

The list lives OUTSIDE this repo (it is private by definition). Point to it with
SESSIONCOACH_PRIVATE_STRINGS=/path/to/private-strings.txt (one string per line, # comments;
KIT_PRIVATE_STRINGS, its name before the rename, is still read).
Usage: SESSIONCOACH_PRIVATE_STRINGS=... python3 scripts/leakcheck.py [repo_dir]
"""
import os
import subprocess
import sys

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}


def publishable_files(repo):
    """What a push could publish: tracked files plus new files git does not ignore (local caches such as
    .coverage or .ruff_cache hold this machine's paths and are never published). Outside git: every file."""
    try:
        r = subprocess.run(["git", "-C", repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, check=True, timeout=60)
        return sorted({p for p in r.stdout.decode("utf-8", "replace").split("\0") if p})
    except (OSError, subprocess.SubprocessError):
        out = []
        for root, dirs, names in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            out += [os.path.relpath(os.path.join(root, f), repo) for f in names]
        return out


def main():
    repo = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
    path = os.environ.get("SESSIONCOACH_PRIVATE_STRINGS") or os.environ.get("KIT_PRIVATE_STRINGS")
    if not path or not os.path.exists(path):
        print("leakcheck: SESSIONCOACH_PRIVATE_STRINGS not set or missing; nothing to check against", file=sys.stderr)
        return 2
    with open(path, encoding="utf-8") as fh:
        needles = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    files = publishable_files(repo)
    hits = []
    for rel in files:
        try:
            with open(os.path.join(repo, rel), encoding="utf-8", errors="ignore") as fh:
                text = fh.read().lower()
        except OSError:
            continue
        for n in needles:
            if n.lower() in text:
                hits.append((rel, n))
    for p, n in hits:
        print("LEAK: %s contains a private string (#%d)" % (p, needles.index(n) + 1))
    print("leakcheck: %d file(s) scanned against %d private strings: %s" % (len(files), len(needles), "FAIL" if hits else "PASS"))
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
