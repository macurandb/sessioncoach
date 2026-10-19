"""Where data is read from and written to. Everything is local."""
import contextlib
import os
import re
import shlex
import shutil
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
ENV_PREFIXES = ("SESSIONCOACH_", "KIT_")  # KIT_*: the variables' names before the rename, still read
SETTINGS_MODE = 0o600  # a settings.json sessioncoach creates (its `env` can hold API keys); an existing one keeps its mode


def user_settings():
    """~/.claude/settings.json: where hooks and commands go by default (CLAUDE_CONFIG_DIR is a scope of its own)."""
    return os.path.join(HOME, ".claude", "settings.json")


def env(name):
    """Setting `name` from the environment: SESSIONCOACH_<name>, else KIT_<name>, else None."""
    for p in ENV_PREFIXES:
        v = os.environ.get(p + name)
        if v:
            return v
    return None


def set_env(name, value):
    """Set SESSIONCOACH_<name> and drop KIT_<name>, so env() returns `value` whatever was set before."""
    os.environ[ENV_PREFIXES[0] + name] = value
    for p in ENV_PREFIXES[1:]:
        os.environ.pop(p + name, None)


def data_dir():
    """Directory holding the metrics database and private config (never inside a repo).

    SESSIONCOACH_DATA_DIR (or KIT_DATA_DIR) wins; otherwise ~/.sessioncoach, except on a machine set up
    before the rename, where the data stays in ~/.kit (settings.json commands point at files inside it)
    until ~/.sessioncoach holds a database of its own (an empty or hand-made folder does not switch it).
    """
    d = env("DATA_DIR")
    if d:  # "~/metrics" is the user's folder, not a "~" dir in the cwd (a quoted value reaches us unexpanded)
        return os.path.normpath(HOME + d[1:]) if d == "~" or d.startswith(("~/", "~" + os.sep)) else os.path.expanduser(d)
    new, old = os.path.join(HOME, ".sessioncoach"), os.path.join(HOME, ".kit")
    return old if os.path.isdir(old) and not os.path.exists(os.path.join(new, "metrics.db")) else new


def ensure_data_dir(sub=None):
    """Create the data dir (and `sub` inside it) private to the user: 0700, whatever the umask."""
    d = data_dir()
    for path in (d, os.path.join(d, sub) if sub else None):
        if path and not os.path.isdir(path):
            os.makedirs(path, mode=0o700, exist_ok=True)
            if os.name != "nt":
                os.chmod(path, 0o700)
    return os.path.join(d, sub) if sub else d


def load_json_object(path):
    """The JSON object in `path`; {} when the file is missing, unreadable, corrupt or not an object."""
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def db_path():
    return os.path.join(data_dir(), "metrics.db")


def projects_map_path():
    """Optional private file mapping Claude Code project dirs to project/tenant labels."""
    return os.path.join(data_dir(), "projects.json")


def transcript_roots():
    """Claude Code `projects/` directories to read.

    SESSIONCOACH_PROJECTS_DIRS (or KIT_PROJECTS_DIRS; os.pathsep-separated) wins; otherwise
    CLAUDE_CONFIG_DIR/projects or ~/.claude/projects. Several roots allow one config dir per workspace.
    """
    listed = env("PROJECTS_DIRS")
    if listed:
        roots = [p for p in listed.split(os.pathsep) if p]
    else:
        cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
        roots = [os.path.join(cfg, "projects")]
    # roots declared in the private accounts file (one config dir per account) are always read
    accs = load_json_object(os.path.join(data_dir(), "accounts.json")).get("accounts")
    for acc in (accs.values() if isinstance(accs, dict) else []):
        extra = acc.get("roots") if isinstance(acc, dict) else None
        roots.extend(r for r in (extra if isinstance(extra, list) else []) if isinstance(r, str))
    seen, out = set(), []
    for r in roots:
        n = os.path.normpath(os.path.expanduser(r))
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def default_root():
    """The default Claude Code transcript root (where all history lives before per-account dirs)."""
    return os.path.normpath(os.path.join(HOME, ".claude", "projects"))


def utf8_stdio():
    """Claude Code speaks UTF-8 on stdin/stdout; on Windows Python would use the ANSI code page."""
    for f in (sys.stdin, sys.stdout):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            f.reconfigure(encoding="utf-8")

def is_windows():
    return sys.platform == "win32"


def split_command(command):
    """The words of a hook or status line command. On Windows a backslash is part of a path, not an escape (the
    POSIX rules would turn C:\\Py\\python.exe into C:Pypython.exe), and quotes only group. Raises ValueError."""
    import shlex
    if not is_windows():
        return shlex.split(command or "")
    return [w[1:-1] if len(w) > 1 and w[0] == w[-1] == '"' else w for w in shlex.split(command or "", posix=False)]

def _python(windowless=False):
    if is_windows():
        exe = sys.executable
        if windowless:  # pythonw.exe runs without opening a console window (scheduled jobs)
            w = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(w):
                return w
        return exe
    v = python_version(STABLE_PYTHON) if os.path.exists(STABLE_PYTHON) else None
    return STABLE_PYTHON if v and v >= (3, 9) else sys.executable


# Written into commands when it is Python 3.9+: it outlives the venv or Homebrew Python setup ran from.
STABLE_PYTHON = "/usr/bin/python3"
_VERSIONS = {}  # interpreter -> (major, minor) or None, asked once per process


def mac_stub(exe):
    """True for macOS's /usr/bin/python3 on a machine without Command Line Tools or Xcode: it is only a shim, and
    running it opens an install dialog. It runs the python3 of the active developer dir, so one of those must exist."""
    if sys.platform != "darwin" or exe != "/usr/bin/python3":
        return False
    link = "/var/db/xcode_select_link"  # what `xcode-select -s` chose
    dev = [os.environ.get("DEVELOPER_DIR"), os.path.realpath(link) if os.path.islink(link) else None,
           "/Library/Developer/CommandLineTools", "/Applications/Xcode.app/Contents/Developer"]
    return not any(d and os.path.exists(os.path.join(d, "usr", "bin", "python3")) for d in dev)


def python_version(exe):
    """(major, minor) of the interpreter `exe`, or None when unknown. Never runs the macOS stub (mac_stub)."""
    if exe not in _VERSIONS:
        v = None
        if os.path.realpath(exe) == os.path.realpath(sys.executable):
            v = tuple(sys.version_info[:2])
        elif not mac_stub(exe):
            import subprocess
            try:
                out = subprocess.run([exe, "-I", "-c", "import sys;print(sys.version_info[0], sys.version_info[1])"],
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False).stdout.split()
                v = (int(out[0]), int(out[1])) if len(out) == 2 else None
            except (OSError, ValueError, subprocess.SubprocessError):
                v = None
        _VERSIONS[exe] = v
    return _VERSIONS[exe]

# Names this tool has had in settings.json commands: sessioncoach now, kit before the rename (bin/kit, -m kit).
COMMAND_NAMES = ("sessioncoach", "kit")


# Whole words only, the same on every OS (/ and \ separate path parts; quotes and spaces separate words):
# a script or console script named sessioncoach (also .exe, or its Windows 8.3 short name SESSIO~1), the old
# clone's bin/kit, `-m sessioncoach` / `-m kit`, or the `-c` launcher (RUN_CODE).
_US = re.compile(r"""(?:^|(?<=[\s"'/\\]))(?:sessioncoach|sessio~\d+)(?:\.exe)?(?=[\s"']|$)"""
                 r"""|(?:^|(?<=[\s"'/\\]))bin[/\\]kit(?=[\s"']|$)"""
                 r"""|(?<=\s)-m\s+(?:sessioncoach|kit)(?=[\s"']|$)"""
                 r"""|\bfrom sessioncoach\.cli import main\b""", re.IGNORECASE)


def mentions_us(command):
    """True when a settings.json command runs this tool under any name it ever had (see _US): `toolkit`,
    /home/nikita/... or a folder called .kit do not count."""
    return bool(_US.search(command or ""))


_OLD_COMMAND = re.compile(r"(?:(?<![\w-])(bin[/\\]kit)|(?<=\s)(-m (?:kit|sessioncoach)))(?=[\s\"']|$)")


def old_command_name(command):
    """'bin/kit', '-m kit' or '-m sessioncoach' when a settings.json command still runs the tool in a form setup no
    longer writes (the old name, or `-m`, which imports a sessioncoach.py in the project first), else None."""
    m = _OLD_COMMAND.search(command or "")
    return (m.group(1) or m.group(2)).replace("\\", "/") if m else None


# `python -c RUN_CODE <args>` runs the installed package like `-m sessioncoach`, but drops '' (the cwd, which for a
# hook is the project) from sys.path first, so a sessioncoach.py or sessioncoach/ in the project cannot run instead.
# No quotes, $ or %: it goes through shlex, win_arg, systemd and cron unchanged.
RUN_CODE = "import sys;sys.path[:]=[p for p in sys.path if p];from sessioncoach.cli import main;sys.exit(main())"


def console_script():
    """The `sessioncoach` console script pip installed for this interpreter (absolute path), or None."""
    import sysconfig
    name = "sessioncoach.exe" if is_windows() else "sessioncoach"
    dirs = [sysconfig.get_path("scripts"), os.path.dirname(sys.executable)]
    preferred = getattr(sysconfig, "get_preferred_scheme", None)  # 3.10+: where `pip install --user` puts scripts
    if preferred:
        dirs.append(sysconfig.get_path("scripts", preferred("user")))
    return next((os.path.join(d, name) for d in dirs if d and os.path.isfile(os.path.join(d, name))), None)


def kit_command(kit_bin=None, windowless=False):
    """argv prefix that runs the tool: the given script, the clone's bin/sessioncoach, the installed console script, or
    `python -c RUN_CODE`. Never `python -m sessioncoach`: Claude Code runs hooks in the project, and -m imports from there.

    `windowless` (Windows only) picks pythonw.exe for scheduled jobs, so no console window flashes.
    """
    py = _python(windowless)
    if kit_bin:
        return [py, kit_bin]
    clone = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "sessioncoach"))
    if os.path.isfile(clone):
        return [py, clone]
    script = None if windowless and is_windows() else console_script()  # sessioncoach.exe would open a console
    if script:
        return [script]
    return [py if is_windows() else sys.executable, "-c", RUN_CODE]

def _short_win(path):
    """Windows 8.3 short form of `path` (its existing part), or `path` unchanged. No-op elsewhere."""
    if not is_windows():
        return path
    try:
        import ctypes
        rest, cur = [], path
        while cur and not os.path.exists(cur):
            head, tail = os.path.split(cur)
            if head == cur:
                return path
            rest.insert(0, tail)
            cur = head
        buf = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetShortPathNameW(cur, buf, len(buf)):
            return path
        return os.path.join(buf.value, *rest)
    except Exception:  # no ctypes/windll or an API error: the long path still works quoted
        return path

def win_arg(a, short=_short_win):
    """One argument of a settings.json command on Windows, valid in Git Bash and in PowerShell.

    Claude Code runs hook and status line commands through Git Bash when installed, else PowerShell
    (code.claude.com/docs/en/hooks, /statusline "Windows configuration"). Both accept an unquoted
    path written with forward slashes (Git Bash eats backslashes); a path with spaces is shortened to its 8.3
    form so it needs no quotes (a leading quoted word would need `&` in PowerShell, a syntax error in bash).
    """
    if "\\" in a or a[1:2] == ":" or a.startswith("/"):
        a = a.replace("\\", "/")
        head, _, tail = a.rpartition("/")
        if " " in head and " " not in tail:  # the file keeps its long name (mentions_us looks for it)
            a = short(head).replace("\\", "/") + "/" + tail
        elif " " in a:
            a = short(a).replace("\\", "/")
    if not a or any(c in a for c in ' \t"\'&|<>()$`;'):
        return '"%s"' % a.replace('"', '\\"')
    return a

def kit_command_str(*args, kit_bin=None):
    """kit_command() plus args as one shell string (for settings.json commands):
    shlex-quoted on POSIX, Git Bash/PowerShell-safe on Windows."""
    argv = kit_command(kit_bin) + list(args)
    if is_windows():
        return " ".join(win_arg(a) for a in argv)
    return " ".join(shlex.quote(a) for a in argv)

def backup_file(path, suffix):
    """Copy `path` to path+suffix (mode 600: settings can hold tokens); returns the backup path or None.
    An earlier backup there with other content is moved to path+suffix-<time> first, so the copy from before
    sessioncoach first touched the file is never overwritten by a later run."""
    if not os.path.exists(path):
        return None
    dest = path + suffix
    if os.path.exists(dest) and _read(dest) != _read(path):
        base = "%s-%s" % (dest, time.strftime("%Y%m%d-%H%M%S"))
        old, n = base, 1
        while os.path.exists(old):
            n += 1
            old = "%s-%d" % (base, n)
        os.replace(dest, old)
    shutil.copyfile(path, dest)
    os.chmod(dest, 0o600)
    return dest


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def timestamp_backup(real):
    base = "%s.bak-kit-%s" % (real, time.strftime("%Y%m%d-%H%M%S"))
    path, n = base, 1
    while os.path.exists(path):
        n += 1
        path = "%s-%d" % (base, n)
    with open(real, "rb") as src:
        data = src.read()
    mode = os.stat(real).st_mode & 0o777
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as dst:
        dst.write(data)
    os.chmod(path, mode)
    return path


def private_dirs(d):
    """Create the data dir and the folders below it that lead to `d` as 0700, whatever the umask."""
    data = os.path.realpath(data_dir())
    if d != data and not d.startswith(data + os.sep):
        return
    cur = ensure_data_dir()
    for part in os.path.relpath(d, data).split(os.sep):
        if part != os.curdir:
            cur = os.path.join(cur, part)
            if not os.path.isdir(cur):
                os.mkdir(cur, 0o700)
                if os.name != "nt":
                    os.chmod(cur, 0o700)


def write_atomic(path, data, default_mode=0o644, backup=True):
    """Backup (if the file exists and `backup`) + tmp file in the same dir + os.replace. Follows symlinks; keeps the mode."""
    real = os.path.realpath(path)
    d = os.path.dirname(real)
    if default_mode == 0o600:
        private_dirs(d)
    os.makedirs(d, mode=0o700 if default_mode == 0o600 else 0o755, exist_ok=True)
    made = None
    mode = default_mode
    if os.path.exists(real):
        made = timestamp_backup(real) if backup else None
        mode = os.stat(real).st_mode & 0o777
    fd, tmp = tempfile.mkstemp(prefix=".kit-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return made
