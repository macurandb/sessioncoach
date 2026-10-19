"""Imported first by every test module: SESSIONCOACH_* variables win over the KIT_* ones the tests set,
so any left in the developer's shell are dropped (a real SESSIONCOACH_DATA_DIR must never be reached).

No scheduler by default (KIT_SCHEDULER=none, also for child processes): a test that wants one forces it
and fakes its commands. XDG_CONFIG_HOME is dropped so systemd units can only land under the temp HOME."""
import os
import subprocess

for _k in [k for k in os.environ if k.startswith("SESSIONCOACH_")]:
    del os.environ[_k]
os.environ["KIT_SCHEDULER"] = "none"  # not setdefault: a KIT_SCHEDULER=cron in the shell must not reach the real crontab
os.environ.pop("XDG_CONFIG_HOME", None)

SCHEDULER_TOOLS = ("schtasks", "crontab", "systemctl", "launchctl")


class RealSchedulerCall(BaseException):
    """A test tried to run a real scheduler command. BaseException: the code under test swallows
    OSError/SubprocessError (and some paths Exception), and this must still fail the test."""


def _tool(args):
    argv = args.split() if isinstance(args, str) else list(args or [])
    name = str(argv[0]).replace("\\", "/").rsplit("/", 1)[-1].lower() if argv else ""  # Windows paths too
    return name[:-4] if name.endswith(".exe") else name


def _guarded(real):
    def run(*a, **kw):
        args = a[0] if a else kw.get("args")
        if _tool(args) in SCHEDULER_TOOLS:
            raise RealSchedulerCall("a test ran the real %r; pass run= or patch scheduler._run with a fake" % (args,))
        return real(*a, **kw)
    run.isolate_guard = True
    return run


# Fakes are called directly and mock.patch replaces these wrappers while active, so both still work.
for _name in ("run", "call", "check_call", "check_output"):
    if not getattr(getattr(subprocess, _name), "isolate_guard", False):
        setattr(subprocess, _name, _guarded(getattr(subprocess, _name)))


def rmtree(path):
    """shutil.rmtree after closing the SQLite connections left in reference cycles: Windows cannot delete an open file."""
    import gc
    import shutil
    gc.collect()
    shutil.rmtree(path)


def claude_home(test):
    """Point paths.HOME at a temp dir and drop CLAUDE_CONFIG_DIR for one test, so nothing reads the
    developer's ~/.claude (e.g. the `language` setting the hook follows). Returns the temp HOME."""
    import shutil
    import tempfile
    from unittest import mock

    from sessioncoach import paths
    home = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, home, True)
    for p in (mock.patch.object(paths, "HOME", home), mock.patch.dict(os.environ)):
        p.start()
        test.addCleanup(p.stop)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    return home
