"""Install and remove the Herdr panel (`sessioncoach herdr-install | herdr-uninstall`).

Herdr is a desktop app for terminals and coding agents; its panels ("mods") are small JavaScript
programs kept in `<app home>/mods/<name>/`. This module copies sessioncoach's panel into
`<app home>/mods/sessioncoach/`, writes the `config.json` it reads, and records each shipped file's
sha256 in a manifest so a later run can tell a user's edit from an update.

The panel it installs runs `sessioncoach live --json` to show a decision view over the running
Claude Code sessions (am I OK this week, what should I do now), writes the `.groups-sync.json`
staging file and runs `sessioncoach group sync-herdr` on it (which edits `projects.json`), and keeps
its state in two shared keys (`sessioncoach.toasted`, `sessioncoach.folded`).
"""
import contextlib
import hashlib
import json
import os
import shutil
import sys

from . import __version__, config, paths

OK, WARN = "ok", "warn"  # installer.check statuses; herdr stays free of the heavy installer import
MOD_DIR = "sessioncoach"
MOD_FILES = ("main.js", "logic.js", "mod.json", "icon.svg")
MANIFEST = ".sessioncoach-manifest.json"
HERDR_WRITES = ("gpui-kit.d.ts", "jsconfig.json")  # type files Herdr itself writes into every mod folder
SYNC_FILE = ".groups-sync.json"  # the panel stages the Herdr groups it hands to `sessioncoach group sync-herdr` here
DEFAULT_HINT = config.LONG_CONTEXT_TOKENS  # 300000: the /compact hint threshold when alerts.json sets none


def app_home(home=None):
    """The Herdr app folder: `--app-home`, else `$HERDR_APP_HOME`, else `~/.config/herdr-app`."""
    if home:
        return os.path.expanduser(home)
    env = os.environ.get("HERDR_APP_HOME")
    if env:
        return os.path.expanduser(env)
    return os.path.join(paths.HOME, ".config", "herdr-app")


def mod_dir(home):
    return os.path.join(home, "mods", MOD_DIR)


def _shipped():
    """The directory holding the mod files shipped with this package."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "herdr_mod")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_hash(dest, name):
    """The sha256 of an installed file, or None when it is missing."""
    path = os.path.join(dest, name)
    return _sha256(path) if os.path.isfile(path) else None


def _read_manifest(path):
    """The manifest's dict, or None when it is missing, unreadable or not ours."""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("files"), dict) else None


def _command():
    """How the mod runs sessioncoach: the console script, else this interpreter with `-m`."""
    exe = shutil.which("sessioncoach")
    if exe:
        return {"command": os.path.abspath(exe)}
    return {"command": sys.executable, "args_prefix": ["-m", "sessioncoach"]}


def _hint_min_tokens(alerts=None):
    """The lowest `context_tokens` alert threshold (alerts.json), or 300000 when none is set."""
    tokens = (alerts or config.load_alerts()).get("context_tokens") or []
    tokens = [int(t) for t in tokens if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0]
    return min(tokens) if tokens else DEFAULT_HINT


def _load_config(dest):
    return paths.load_json_object(os.path.join(dest, "config.json"))


def _refresh_config(cfg):
    """The user's keys stay; the keys sessioncoach owns are refreshed to this install."""
    cmd = _command()
    cfg["command"] = cmd["command"]
    if cmd.get("args_prefix"):
        cfg["args_prefix"] = cmd["args_prefix"]
    else:
        cfg.pop("args_prefix", None)
    cfg["hint_min_tokens"] = _hint_min_tokens()


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def install(home=None, force=False):
    """Copy the panel into the app home and write its config. Returns 0, or 1 on refusal."""
    app = app_home(home)
    if not os.path.isdir(app):
        print("Herdr not found (no %s)" % app)
        return 1
    dest = mod_dir(app)
    manifest_path = os.path.join(dest, MANIFEST)
    if os.path.isdir(dest):
        manifest = _read_manifest(manifest_path) if os.path.isfile(manifest_path) else None
        if manifest is None and not force:
            print("refusing to install: %s already exists but was not installed by sessioncoach "
                  "(no %s); pass --force to overwrite it" % (dest, MANIFEST))
            return 1
        if manifest is not None:
            edited = [f for f in MOD_FILES if _file_hash(dest, f) != manifest["files"].get(f)]
            if edited and not force:
                print("refusing to install: %s was edited since install; pass --force to reinstall"
                      % ", ".join(edited))
                return 1
    os.makedirs(dest, exist_ok=True)
    for f in MOD_FILES:
        shutil.copyfile(os.path.join(_shipped(), f), os.path.join(dest, f))
    cfg = _load_config(dest)
    _refresh_config(cfg)
    _write_json(os.path.join(dest, "config.json"), cfg)
    _write_json(manifest_path, {"version": __version__, "files": {f: _file_hash(dest, f) for f in MOD_FILES}})
    print("installed the sessioncoach panel in %s" % dest)
    print("Herdr docks it on the right at its next start; to open it now:")
    print("  herdr-app do pane.split '{\"direction\":\"right\",\"mod\":\"sessioncoach\"}'")
    return 0


def uninstall(home=None):
    """Remove only what install put there: the manifest's files, config.json and the manifest,
    then the folder if empty. Returns 0."""
    app = app_home(home)
    dest = mod_dir(app)
    if not os.path.isdir(dest):
        print("not installed")
        return 0
    manifest = _read_manifest(os.path.join(dest, MANIFEST))
    if manifest is None:  # not ours (or the manifest is gone): leave the folder alone
        print("%s was not installed by sessioncoach (no %s); left as is" % (dest, MANIFEST))
        return 0
    # only names we ship: a hand-edited manifest must not point the removal anywhere else
    ours = [f for f in MOD_FILES if f in manifest["files"]]
    for name in [*ours, "config.json", SYNC_FILE, MANIFEST, *HERDR_WRITES]:
        path = os.path.join(dest, name)
        if os.path.isfile(path):
            os.remove(path)
    with contextlib.suppress(OSError):
        os.rmdir(dest)  # only when nothing else is left
    print("removed the sessioncoach panel from %s" % app)
    return 0


def check(home=None):
    """[(status, name, detail)] for the Herdr panel; empty when Herdr is not found."""
    app = app_home(home)
    if not os.path.isdir(app):
        return []
    dest = mod_dir(app)
    if not os.path.isdir(dest):
        return [(WARN, "Herdr panel", "not installed: sessioncoach herdr-install")]
    current = {f: _sha256(os.path.join(_shipped(), f)) for f in MOD_FILES}
    if all(_file_hash(dest, f) == current[f] for f in MOD_FILES):
        return [(OK, "Herdr panel", dest)]
    return [(WARN, "Herdr panel", "outdated: run sessioncoach herdr-install")]
