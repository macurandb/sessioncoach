"""Run sessioncoach on a schedule, on any OS: one small abstraction over the platform's own scheduler.

  macOS    launchd LaunchAgent (plist in ~/Library/LaunchAgents)
  Linux    systemd --user timer (.service + .timer in ~/.config/systemd/user); without a user
           systemd (containers, WSL without systemd) a marked block in the user's crontab
  Windows  Task Scheduler (`schtasks /Create /XML`), per user, no admin rights

A job is either "every N minutes" (ingest) or "on these weekdays at HH:MM" (daily review). All four
backends live here, one Backend subclass each, registered in BACKENDS; install/uninstall/status/installed
only pick one (installer.py, review.py, settings.py and cli.py call them). SESSIONCOACH_SCHEDULER (or
KIT_SCHEDULER)=launchd|systemd|cron|schtasks|none forces a backend (tests, odd setups).
Every job records its schedule as a short key (`every:30`, `cal:08:00:mon,tue`) inside its own
artifact, so `sessioncoach check` can tell when alerts.json no longer matches what is installed.

The tool was called daf-claude-kit before: jobs installed under that name (LaunchAgent label, systemd
unit, scheduled task, crontab block) are still recognised, and installing a job removes its old copy
so it never runs twice.
"""
import contextlib
import datetime as dt
import glob
import os
import plistlib
import re
import subprocess
import sys
import time

from . import paths

PREFIX = "sessioncoach"
LAUNCHCTL_TIMEOUT_S = 30
LEGACY_PREFIX = "daf-claude-kit"
LAUNCHD_LABEL = "sessioncoach.%s"
LEGACY_LAUNCHD_GLOB = "*." + LEGACY_PREFIX + ".%s.plist"  # old labels were <reverse-DNS>.daf-claude-kit.<job>
LABELS = {"launchd": "LaunchAgent", "systemd": "systemd user timer", "cron": "cron", "schtasks": "Task Scheduler"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CRON_BEGIN, CRON_END = "# >>> sessioncoach (managed by sessioncoach; do not edit) >>>", "# <<< sessioncoach <<<"
LEGACY_CRON_BEGIN, LEGACY_CRON_END = "# >>> daf-claude-kit (managed by kit; do not edit) >>>", "# <<< daf-claude-kit <<<"
CRON_TAGS = ("sessioncoach", "kit")  # `# <tag>:<job> <key>` at the end of each line; kit is the old one


class Job:
    """name: 'ingest'|'review'; args: kit arguments; every_min or (time 'HH:MM', days)."""
    def __init__(self, name, args, every_min=None, hhmm=None, days=None):
        self.name, self.args, self.every_min, self.hhmm = name, list(args), every_min, hhmm
        self.days = [d[:3].lower() for d in (days or DAYS)]

    @property
    def unit(self):
        return "%s-%s" % (PREFIX, self.name)

    @property
    def legacy_unit(self):
        return "%s-%s" % (LEGACY_PREFIX, self.name)

    def key(self):
        if self.every_min:
            return "every:%d" % self.every_min
        return "cal:%s:%s" % (self.hhmm, ",".join(d for d in DAYS if d in self.days))

    def hm(self):
        h, m = self.hhmm.split(":")
        return int(h), int(m)


def backend(platform=None):
    """The scheduler this machine uses (see module doc), or None when there is none.

    A forced value that is not a backend nor "none" raises ValueError: falling back to the automatic
    choice would install real jobs for someone who wrote e.g. "off"."""
    forced = paths.env("SCHEDULER")
    if forced:
        if forced == "none":
            return None
        if forced not in BACKENDS:
            var = next(p + "SCHEDULER" for p in paths.ENV_PREFIXES if os.environ.get(p + "SCHEDULER"))
            raise ValueError("%s=%s is not a scheduler; use one of: %s, none" % (var, forced, ", ".join(BACKENDS)))
        return forced
    return auto_backend(platform)


def auto_backend(platform=None):
    """The scheduler this OS uses, ignoring any SESSIONCOACH_SCHEDULER."""
    platform = platform or sys.platform
    if platform == "darwin":
        return "launchd"
    if platform == "win32":
        return "schtasks"
    if _systemd_user():
        return "systemd"
    import shutil
    return "cron" if shutil.which("crontab") else None


def candidates(platform=None):
    """Every backend whose leftovers `uninstall` should look for on this OS."""
    platform = platform or sys.platform
    return {"darwin": ["launchd"], "win32": ["schtasks"]}.get(platform, ["systemd", "cron"])


def _systemd_user():
    import shutil
    if not shutil.which("systemctl"):
        return False
    try:
        return subprocess.run(["systemctl", "--user", "show-environment"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run(cmd, **kw):
    kw.setdefault("stdout", subprocess.PIPE)
    kw.setdefault("stderr", subprocess.PIPE)
    kw.setdefault("timeout", 30)
    return subprocess.run(cmd, text=True, check=kw.pop("check", False), **kw)


def _argv(job, kit_bin, windowless=False):
    return paths.kit_command(kit_bin, windowless=windowless) + job.args


# where the data and the transcripts are: set in the shell that installs a job, a scheduler would not pass them on
JOB_ENV = (*(p + n for n in ("DATA_DIR", "PROJECTS_DIRS") for p in paths.ENV_PREFIXES), "CLAUDE_CONFIG_DIR")


def scheduled_env(environ=None):
    """[(name, value)] of the JOB_ENV variables set (not empty) now, in JOB_ENV order: what a scheduled job needs to
    see the same data dir, transcripts and Claude config as the shell that installed it."""
    environ = os.environ if environ is None else environ
    return [(k, environ[k]) for k in JOB_ENV if environ.get(k)]


# ---- launchd ----

def launchd_label(name):
    return LAUNCHD_LABEL % name

def _agents_dir(agents_dir=None):
    return agents_dir or os.path.join(paths.HOME, "Library", "LaunchAgents")

def plist_path(label, agents_dir=None):
    return os.path.join(_agents_dir(agents_dir), label + ".plist")

def legacy_plists(name, agents_dir=None):
    """Paths of the LaunchAgents of job `name` installed under the old name, any reverse-DNS prefix."""
    return sorted(glob.glob(os.path.join(glob.escape(_agents_dir(agents_dir)), LEGACY_LAUNCHD_GLOB % name)))

def current_plist(name, agents_dir=None):
    """(label, path) of the LaunchAgent installed for job `name`: the current one, else the one under the
    old name, else the current one (not installed)."""
    path = plist_path(launchd_label(name), agents_dir)
    if not os.path.exists(path):
        old = legacy_plists(name, agents_dir)
        if old:
            return os.path.basename(old[0])[:-len(".plist")], old[0]
    return launchd_label(name), path

def launchd_doc(job, kit_bin, log):
    """The LaunchAgent for `job` as a dict (see launchd_plist)."""
    d = {"Label": launchd_label(job.name), "ProgramArguments": _argv(job, kit_bin), "StandardOutPath": log,
         "StandardErrorPath": log, "LowPriorityIO": True, "Nice": 10}
    env = scheduled_env()
    if env:
        d["EnvironmentVariables"] = dict(env)
    if job.every_min:
        d.update(StartInterval=int(job.every_min) * 60, RunAtLoad=True)
    else:
        h, m = job.hm()  # Weekday: 0 = Sunday, 1 = Monday ... 6 = Saturday
        d["StartCalendarInterval"] = [{"Weekday": (DAYS.index(day) + 1) % 7, "Hour": h, "Minute": m} for day in job.days]
    return d


def launchd_plist(job, kit_bin, log):
    """The LaunchAgent plist bytes for `job` (every_min -> StartInterval + RunAtLoad, else StartCalendarInterval)."""
    return plistlib.dumps(launchd_doc(job, kit_bin, log))


def _launchd_schedule(job, doc):
    """The schedule part of a LaunchAgent dict, in a form that compares equal for the same schedule."""
    if job.every_min:
        return doc.get("StartInterval")
    cal = doc.get("StartCalendarInterval")
    cal = cal if isinstance(cal, list) else [cal] if isinstance(cal, dict) else []
    return sorted(cal, key=lambda d: (d.get("Weekday", -1), d.get("Hour", 0), d.get("Minute", 0)))


def launchd_loaded(label, run=None):
    """True/False, or None when launchctl is unavailable. `run`: subprocess.run or a test fake."""
    try:
        r = (run or subprocess.run)(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, check=False)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


LAUNCHD_RETRY = {"ingest": "setup"}  # what a timed-out bootstrap tells you to run again (default: <job>-install)


def _launchd_reload(path, load, name, run=None):
    if not load:
        return
    run = run or subprocess.run  # looked up per call: tests patch subprocess.run
    dom = "gui/%d" % os.getuid()
    with contextlib.suppress(subprocess.TimeoutExpired):
        run(["launchctl", "bootout", dom, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=LAUNCHCTL_TIMEOUT_S, check=False)
    try:
        run(["launchctl", "bootstrap", dom, path], timeout=LAUNCHCTL_TIMEOUT_S, check=True)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("launchctl bootstrap timed out after %d s; run `sessioncoach %s` again"
                           % (LAUNCHCTL_TIMEOUT_S, LAUNCHD_RETRY.get(name, name + "-install"))) from e


def launchd_remove(path, load, run=None):
    """Unload and delete a LaunchAgent plist; True when there was one."""
    if not os.path.exists(path):
        return False
    if load:
        with contextlib.suppress(subprocess.TimeoutExpired):  # a stuck unload must not stop the file from being removed
            (run or subprocess.run)(["launchctl", "bootout", "gui/%d" % os.getuid(), path], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=LAUNCHCTL_TIMEOUT_S, check=False)
    os.remove(path)
    return True


# ---- systemd --user ----

def systemd_dir(unit_dir=None):
    return unit_dir or os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(paths.HOME, ".config"), "systemd", "user")

def _sd_quote(a):
    a = a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return '"%s"' % a if re.search(r'[\s"\']', a) else a

def _sd_path(p):
    """A path for StandardOutput=append:. systemd takes the rest of the line as is (no quotes, no C escapes: spaces
    are fine) and only expands specifiers, so % is doubled; a line break cannot be written at all."""
    if "\n" in p or "\r" in p:
        raise ValueError("a log path with a line break cannot go in a systemd unit: %r" % p)
    return p.replace("%", "%%")

def _sd_env(name, value):
    """One Environment= line. It unquotes and C-unescapes and expands specifiers, but not $ (unlike ExecStart=)."""
    if "\n" in value or "\r" in value:
        raise ValueError("%s has a line break: it cannot go in a systemd unit" % name)
    v = ("%s=%s" % (name, value)).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return 'Environment="%s"\n' % v

def systemd_units(job, argv, log):
    """(service text, timer text) for a job."""
    out = _sd_path(log)
    service = ("[Unit]\nDescription=sessioncoach %s\n\n[Service]\nType=oneshot\nExecStart=%s\nNice=10\nIOSchedulingClass=idle\n"
               "StandardOutput=append:%s\nStandardError=append:%s\n" % (job.name, " ".join(_sd_quote(a) for a in argv), out, out))
    service += "".join(_sd_env(k, v) for k, v in scheduled_env())
    if job.every_min:
        when = "OnBootSec=2min\nOnUnitActiveSec=%dmin\n" % job.every_min
    else:
        names = ",".join(d.capitalize() for d in DAYS if d in job.days)
        # Persistent=true: a run missed while the machine was off or asleep happens at the next boot/wake
        when = "OnCalendar=%s *-*-* %02d:%02d:00\nPersistent=true\n" % ((names, *job.hm()))
    timer = ("# kit-schedule: %s\n[Unit]\nDescription=sessioncoach %s timer\n\n[Timer]\n%s\n[Install]\nWantedBy=timers.target\n"
             % (job.key(), job.name, when))
    return service, timer

def _systemctl(*args, run=None):
    return (run or _run)(["systemctl", "--user", *args])

def _key_in(text):
    m = re.search(r"kit-schedule: (\S+)", text or "")
    return m.group(1) if m else None


# ---- cron ----

# the intervals cron runs evenly: minutes that divide an hour, hours that divide a day (*/45 would run at :00 and :45)
CRON_EVERY = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60, 120, 180, 240, 360, 480, 720, 1440)


def cron_job(job):
    """`job` as cron runs it: an interval it cannot express becomes the nearest one it can (on a tie the shorter:
    an ingest late is worse than one early)."""
    if not job.every_min or job.every_min in CRON_EVERY:
        return job
    n = min(CRON_EVERY, key=lambda v: (abs(v - job.every_min), v))
    return Job(job.name, job.args, every_min=n)


def cron_note(job):
    """What cron does instead of `job`'s interval, or "" when it runs it as asked."""
    eff = cron_job(job)
    return "" if eff is job else "every %d min (cron cannot run every %d)" % (eff.every_min, job.every_min)


def cron_line(job, argv, log):
    """One crontab line for a job, tagged so only this tool's own lines are ever touched. The tag carries the
    schedule cron really runs (cron_job)."""
    import shlex
    job = cron_job(job)
    if job.every_min:
        n = job.every_min
        sched = "*/%d * * * *" % n if n < 60 else ("0 */%d * * *" % (n // 60) if n < 1440 else "0 0 * * *")
    else:
        h, m = job.hm()
        sched = "%d %d * * %s" % (m, h, ",".join(str((DAYS.index(d) + 1) % 7) for d in DAYS if d in job.days))
    env = ""
    bus = "/run/user/%d/bus" % os.getuid() if hasattr(os, "getuid") else ""
    if bus and os.path.exists(bus):  # lets notify-send reach the desktop from cron
        env = "DBUS_SESSION_BUS_ADDRESS=unix:path=%s " % bus
    env += "".join("%s=%s " % (k, shlex.quote(v)) for k, v in scheduled_env())
    cmd = "%s%s >> %s 2>&1" % (env, " ".join(shlex.quote(a) for a in argv), shlex.quote(log))
    return "%s %s # %s:%s %s" % (sched, cmd.replace("%", "\\%"), CRON_TAGS[0], job.name, job.key())

def _cron_tagged(line, name):
    return re.search(r"# (?:%s):%s\s" % ("|".join(CRON_TAGS), re.escape(name)), line + " ")

def cron_merge(text, job=None, line=None):
    """crontab text with our block updated: `job`'s line replaced by `line`, or removed when line is None.

    A block left by the old name is read as ours and rewritten under the current markers."""
    lines = (text or "").splitlines()
    out, ours, inside = [], [], False
    for ln in lines:
        if ln in (CRON_BEGIN, LEGACY_CRON_BEGIN):
            inside = True
        elif ln in (CRON_END, LEGACY_CRON_END):
            inside = False
        elif inside:
            ours.append(ln)
        else:
            out.append(ln)
    if job is not None:
        ours = [ln for ln in ours if not _cron_tagged(ln, job.name)]
        if line:
            ours.append(line)
    if ours:
        out += [CRON_BEGIN, *ours, CRON_END]
    return "\n".join(out) + "\n" if out else ""

def cron_line_of(text, name):
    for ln in (text or "").splitlines():
        if _cron_tagged(ln, name):
            return ln
    return None

def _read_cron(run):
    r = run(["crontab", "-l"])
    return r.stdout if r.returncode == 0 else ""

def _write_cron(run, text):
    r = run(["crontab", "-"], input=text) if text.strip() else run(["crontab", "-r"])
    if r.returncode != 0 and text.strip():
        raise RuntimeError("crontab failed: %s" % (r.stderr or "").strip())

def _backup_cron(old):
    if old.strip():
        d = paths.ensure_data_dir("backups")
        with open(os.path.join(d, "crontab.%s.bak" % time.strftime("%Y%m%d-%H%M%S")), "w", encoding="utf-8") as fh:
            fh.write(old)


# ---- Windows Task Scheduler ----

def _xml_esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def task_xml(job, argv, now=None):
    """Task Scheduler XML: runs hidden, also on battery, and a missed start runs when the PC is back."""
    now = now or dt.datetime.now()
    exe, args = argv[0], subprocess.list2cmdline(argv[1:])
    if job.every_min:
        trig = ("<TimeTrigger><Repetition><Interval>PT%dM</Interval></Repetition><StartBoundary>%s</StartBoundary>"
                "<Enabled>true</Enabled></TimeTrigger>" % (job.every_min, now.strftime("%Y-%m-%dT%H:%M:00")))
    else:
        full = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
                "sat": "Saturday", "sun": "Sunday"}
        h, m = job.hm()
        trig = ("<CalendarTrigger><StartBoundary>%s</StartBoundary><Enabled>true</Enabled><ScheduleByWeek><WeeksInterval>1</WeeksInterval>"
                "<DaysOfWeek>%s</DaysOfWeek></ScheduleByWeek></CalendarTrigger>"
                % (now.strftime("%Y-%m-%dT") + "%02d:%02d:00" % (h, m), "".join("<%s />" % full[d] for d in DAYS if d in job.days)))
    return ('<?xml version="1.0" encoding="UTF-16"?>\n<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            "<RegistrationInfo><Description>sessioncoach %s</Description></RegistrationInfo>\n<Triggers>%s</Triggers>\n"
            '<Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType>'
            '<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n'
            "<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><Hidden>true</Hidden>"
            "<ExecutionTimeLimit>PT1H</ExecutionTimeLimit></Settings>\n"
            '<Actions Context="Author"><Exec><Command>%s</Command><Arguments>%s</Arguments></Exec></Actions>\n</Task>\n'
            % (job.name, trig, _xml_esc(exe), _xml_esc(args)))

def _state_path(job, unit=None):
    return os.path.join(paths.data_dir(), "scheduler", (unit or job.unit) + ".key")

def _read_state(job, unit=None):
    try:
        with open(_state_path(job, unit), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


# ---- backends: one class per scheduler, picked from BACKENDS by the public functions below ----

class Backend:
    """One platform scheduler. `run`: the injectable command runner (None: the module's _run, looked up per call)."""
    name = None

    def __init__(self, run=None):
        self.raw = run

    @property
    def run(self):
        return self.raw or _run  # looked up per call: tests patch scheduler._run

    def install(self, job, kit_bin, log, unit_dir, load):
        """Install or refresh `job`, removing its copy under the old name; a one-line description."""
        raise NotImplementedError

    def remove(self, job, unit_dir, load):
        """Remove `job` under its current and old names; the list of what was removed."""
        raise NotImplementedError

    def status(self, job, unit_dir, st, loaded_fn=None):
        """Fill `st` (see status()); loaded is None when the scheduler cannot be asked."""
        try:
            self._status(job, unit_dir, st)
        except (OSError, subprocess.SubprocessError):
            st["loaded"] = None

    def _status(self, job, unit_dir, st):
        raise NotImplementedError

    def installed(self, job, unit_dir):
        st = _blank_status(job)
        self.status(job, unit_dir, st)
        return st["installed"]


class LaunchdBackend(Backend):
    """macOS LaunchAgent. launchctl gets the raw `run`: _run would capture bootstrap's output."""
    name = "launchd"

    def install(self, job, kit_bin, log, unit_dir, load):
        path = plist_path(launchd_label(job.name), unit_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(launchd_plist(job, kit_bin, log))
        for old in legacy_plists(job.name, unit_dir):
            launchd_remove(old, load, self.raw)
        _launchd_reload(path, load, job.name, self.raw)
        return "installed %s" % path

    def remove(self, job, unit_dir, load):
        gone = [p for p in [plist_path(launchd_label(job.name), unit_dir), *legacy_plists(job.name, unit_dir)]
                if launchd_remove(p, load, self.raw)]
        return ["removed %s" % ", ".join(gone)] if gone else []

    def status(self, job, unit_dir, st, loaded_fn=None):
        want = _launchd_schedule(job, launchd_doc(job, None, ""))  # first: a bad job (unknown day) raises ValueError
        label, path = current_plist(job.name, unit_dir)
        st.update(where=path, label=label, legacy=label != launchd_label(job.name))
        if os.path.exists(path):
            st["installed"] = True
            st["loaded"] = loaded_fn(label) if loaded_fn else launchd_loaded(label, self.raw)
            try:
                with open(path, "rb") as fh:
                    have = _launchd_schedule(job, plistlib.load(fh))
                st["key"] = job.key() if have == want else "plist: %r" % (have,)
            except (OSError, ValueError, AttributeError):  # plistlib.InvalidFileException is a ValueError
                st["unreadable"] = True

    def installed(self, job, unit_dir):
        return os.path.exists(current_plist(job.name, unit_dir)[1])  # without asking launchd


class SystemdBackend(Backend):
    """systemd --user timer: <unit>.service + <unit>.timer in systemd_dir()."""
    name = "systemd"

    def install(self, job, kit_bin, log, unit_dir, load):
        d = systemd_dir(unit_dir)
        os.makedirs(d, exist_ok=True)
        self._remove(job.legacy_unit, d, load)
        service, timer = systemd_units(job, _argv(job, kit_bin), log)
        for ext, text in (("service", service), ("timer", timer)):
            with open(os.path.join(d, "%s.%s" % (job.unit, ext)), "w", encoding="utf-8") as fh:
                fh.write(text)
        if load:
            for args in (("daemon-reload",), ("enable", "--now", job.unit + ".timer")):
                r = _systemctl(*args, run=self.run)
                if r.returncode != 0:
                    raise RuntimeError("systemctl --user %s failed: %s" % (" ".join(args), (r.stderr or "").strip()))
        return "installed %s.timer in %s: %s (log %s)" % (job.unit, d, job.key(), log)

    def remove(self, job, unit_dir, load):
        return ["removed %s.timer" % unit for unit in (job.unit, job.legacy_unit)
                if self._remove(unit, systemd_dir(unit_dir), load)]

    def _remove(self, unit, d, load):
        files = [os.path.join(d, "%s.%s" % (unit, e)) for e in ("timer", "service")]
        if not any(os.path.exists(f) for f in files):
            return False
        if load:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                _systemctl("disable", "--now", unit + ".timer", run=self.run)
        for f in files:
            if os.path.exists(f):
                os.remove(f)
        if load:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                _systemctl("daemon-reload", run=self.run)
        return True

    def _status(self, job, unit_dir, st):
        d = systemd_dir(unit_dir)
        unit = job.unit
        if not os.path.exists(os.path.join(d, unit + ".timer")) and os.path.exists(os.path.join(d, job.legacy_unit + ".timer")):
            unit, st["legacy"] = job.legacy_unit, True
        f = os.path.join(d, unit + ".timer")
        st["where"] = f
        if os.path.exists(f):
            st["installed"] = True
            with open(f, encoding="utf-8") as fh:
                st["key"] = _key_in(fh.read())
            r = _systemctl("is-enabled", unit + ".timer", run=self.run)
            a = _systemctl("is-active", unit + ".timer", run=self.run)
            st["loaded"] = r.returncode == 0 and a.returncode == 0
            if st["loaded"]:
                n = _systemctl("show", unit + ".timer", "-p", "NextElapseUSecRealtime", "--value", run=self.run)
                st["next"] = (n.stdout or "").strip() or None


class CronBackend(Backend):
    """A marked block in the user's crontab (CRON_BEGIN/CRON_END); load=False never touches it."""
    name = "cron"

    def install(self, job, kit_bin, log, unit_dir, load):
        line = cron_line(job, _argv(job, kit_bin), log)
        if load:
            old = _read_cron(self.run)
            _backup_cron(old)
            _write_cron(self.run, cron_merge(old, job, line))
        note = cron_note(job)
        shown = " ".join(line.split()[:5] + _argv(job, kit_bin))[:60]  # the schedule and command, not the variables
        return "installed in crontab: %s (log %s)%s" % (shown, log, "; " + note if note else "")

    def remove(self, job, unit_dir, load):
        if not load:
            return []
        try:
            old = _read_cron(self.run)
        except (OSError, subprocess.SubprocessError):
            return []
        if not cron_line_of(old, job.name):
            return []
        _backup_cron(old)
        _write_cron(self.run, cron_merge(old, job, None))
        return ["removed %s from crontab" % job.name]

    def _status(self, job, unit_dir, st):
        text = _read_cron(self.run)
        line = cron_line_of(text, job.name)
        st["where"] = "crontab"
        if line:
            st["installed"], st["loaded"] = True, True
            key = re.split(r"# (?:%s):%s " % ("|".join(CRON_TAGS), re.escape(job.name)), line)[-1].strip()
            note = cron_note(job)
            if note and key == cron_job(job).key():  # what cron can do for this job: no drift, but say it
                key, st["next"] = job.key(), note
            elif note and key == job.key():  # tagged as asked by an older version, though cron runs something else
                key = "%s (as written by an older version)" % key
            st["key"] = key
            st["legacy"] = LEGACY_CRON_BEGIN in text.splitlines()


class SchtasksBackend(Backend):
    """Windows Task Scheduler; the schedule key is kept in <data>/scheduler/<unit>.key."""
    name = "schtasks"

    def install(self, job, kit_bin, log, unit_dir, load):
        self._remove(job, job.legacy_unit, load)
        xml = task_xml(job, _argv(job, kit_bin, windowless=True))
        path = os.path.join(paths.ensure_data_dir("scheduler"), job.unit + ".xml")
        with open(path, "w", encoding="utf-16") as fh:
            fh.write(xml)
        if load:
            r = self.run(["schtasks", "/Create", "/F", "/TN", job.unit, "/XML", path])
            if r.returncode != 0:
                raise RuntimeError("schtasks /Create failed: %s" % ((r.stderr or r.stdout) or "").strip())
        with open(_state_path(job), "w", encoding="utf-8") as fh:
            fh.write(job.key() + "\n")
        msg = "installed task %s: %s (no log: runs hidden)" % (job.unit, job.key())
        names = [k for k, _ in scheduled_env()]
        if names:  # an Exec action takes no environment; cmd /c would bring back the console window
            msg += ("; the task does not see this shell's %s: unless it is a user environment variable, set it with "
                    "`setx NAME value`" % ", ".join(names))
        return msg

    def remove(self, job, unit_dir, load):
        return ["removed task %s" % unit for unit in (job.unit, job.legacy_unit) if self._remove(job, unit, load)]

    def _remove(self, job, unit, load):
        had = _read_state(job, unit) is not None
        if load:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                had = self.run(["schtasks", "/Delete", "/F", "/TN", unit]).returncode == 0 or had
        for f in (_state_path(job, unit), os.path.join(paths.data_dir(), "scheduler", unit + ".xml")):
            if os.path.exists(f):
                os.remove(f)
        return had

    def _status(self, job, unit_dir, st):
        unit, key = job.unit, _read_state(job)
        if key is None and _read_state(job, job.legacy_unit) is not None:
            unit, key, st["legacy"] = job.legacy_unit, _read_state(job, job.legacy_unit), True
        st["where"] = "Task Scheduler: " + unit
        st["key"], st["installed"] = key, key is not None
        if st["installed"]:
            r = self.run(["schtasks", "/Query", "/TN", unit, "/FO", "LIST", "/V"])
            st["loaded"] = r.returncode == 0
            if st["loaded"]:
                m = re.search(r"^[^:\n]*(?:Next|Pr[oó]xim)[^:\n]*:\s*(.+)$", r.stdout or "", re.M)
                st["next"] = m.group(1).strip() if m else None


BACKENDS = {b.name: b for b in (LaunchdBackend, SystemdBackend, CronBackend, SchtasksBackend)}


# ---- public API: thin facades over BACKENDS ----

def _blank_status(job):
    return {"installed": False, "loaded": None, "key": None, "next": None, "where": "", "legacy": False, "label": job.unit,
            "unreadable": False}


def install(job, kit_bin=None, log=None, unit_dir=None, load=True, platform=None, run=None, be=None):
    """Install or refresh `job` in the platform scheduler (removing its copy under the old name). Returns a one-line description."""
    be = be or backend(platform)
    if log:
        os.makedirs(os.path.dirname(log), exist_ok=True)
    else:
        log = os.path.join(paths.ensure_data_dir("logs"), job.name + ".log")
    cls = BACKENDS.get(be)
    if cls is None:
        raise RuntimeError("no scheduler available on this machine (set SESSIONCOACH_SCHEDULER or run `sessioncoach ingest` by hand)")
    return cls(run).install(job, kit_bin, log, unit_dir, load)


def uninstall(job, unit_dir=None, load=True, platform=None, run=None, bes=None):
    """Remove `job` (under its current and old names) from every backend it may be in on this OS. Returns a description.

    LaunchAgents are only touched when launchd is the active backend and no `bes` are given, and then nothing else is."""
    done = []
    try:
        active = backend(platform)
    except ValueError:  # a mistyped SESSIONCOACH_SCHEDULER must not stop a removal: this OS's own one
        active = auto_backend(platform)
    if bes is None and active == "launchd":
        wanted, allowed = ["launchd"], ("launchd",)
    else:
        wanted, allowed = [*(bes or candidates(platform)), active], ("systemd", "cron", "schtasks")
    for be in [b for i, b in enumerate(wanted) if b in allowed and b not in wanted[:i]]:
        done += BACKENDS[be](run).remove(job, unit_dir, load)
    return "; ".join(done) or "not installed"


def installed(job, unit_dir=None, platform=None, run=None, be=None):
    """Whether `job` is installed (under its current or old name), without asking launchd."""
    cls = BACKENDS.get(be or backend(platform))
    return bool(cls) and cls(run).installed(job, unit_dir)


def status(job, unit_dir=None, platform=None, run=None, be=None, loaded_fn=None):
    """{'installed': bool, 'loaded': True|False|None, 'key': stored schedule key or None, 'next': str|None,
    'where': str, 'legacy': True when only the copy under the old name is installed, 'label': the unit/label found,
    'unreadable': True when our artifact exists but cannot be read}.

    `installed` reads our own artifact (no subprocess except crontab -l); `loaded` asks the scheduler
    (launchd: `loaded_fn(label)`, default launchd_loaded). A LaunchAgent stores no key: it is job.key() when its
    schedule matches the one launchd_plist(job) writes, else a description of what it has.
    """
    st = _blank_status(job)
    cls = BACKENDS.get(be or backend(platform))
    if cls:
        cls(run).status(job, unit_dir, st, loaded_fn)
    return st
