"""Human labels for Claude Code project dirs ('-Users-me-code-app' -> 'code/app').

An optional private file (<data dir>/projects.json) maps dirs to a project and a tenant
(workspace), e.g. to merge git worktrees into one project:

  {"rules": [{"match": "-Users-me-code-app*", "project": "app", "tenant": "acme"}]}

Rules are fnmatch patterns over the dir name; the first match wins.

The same file also names **groups** (clients): a tenant with a display name, kept in insertion
order. A rule may carry ``"source": "cli" | "herdr"`` (absent = written by hand = "manual").
Every mutation goes through ``edit()``: an exclusive lock file serialises the read-modify-write
(the CLI and the Herdr panel both write this file), the result is written atomically (temp file +
``os.replace``), and ``_comment`` and every key this module does not own are kept. A file that
exists but cannot be read as a JSON object is never overwritten.
"""
import contextlib
import copy
import fnmatch
import json
import os
import re
import time

from . import paths
from .collect import encode_dir

UNASSIGNED = "unassigned"
# what a session started inside a Claude Code scratchpad leaves: /tmp/claude-<uid>/<project dir>/<session uuid>/...
SCRATCHPAD = re.compile(r"^-(?:private-)?tmp-claude-\d+-(-.+?)-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?:-.*)?$")

_SLUG = re.compile(r"[^a-z0-9]+")  # a group id is a slug: lowercase letters, digits and dashes
_GLOB_CHARS = ("*", "?", "[")

LOCK_TIMEOUT_S = 5  # how long a mutation waits for another writer's lock before giving up
LOCK_STALE_S = 60  # a lock older than this was left behind by a crashed writer: break it

# sync-herdr input bounds (the Herdr feed)
MAX_GROUPS = 200
MAX_FOLDERS = 1000
MAX_NAME = 100
MAX_PATH = 4096


class ProjectsError(Exception):
    """projects.json could not be read or written as expected (locked, or not a JSON object)."""


class SyncError(Exception):
    """The `sync-herdr` input is not the documented shape; nothing was written."""


def slug(label):
    """'Pretty Co' -> 'pretty-co'. A label with no alphanumeric character gives ''."""
    return _SLUG.sub("-", (label or "").lower()).strip("-")


def owner_dir(project_dir):
    """The project a scratchpad's dir belongs to; any other dir stays as it is."""
    m = SCRATCHPAD.match(project_dir)
    return m.group(1) if m else project_dir


# ---- the projects.json document ----------------------------------------------------------------

def load_doc(path=None):
    """The whole projects.json document (every key kept, ``_comment`` included); {} when absent
    or not a JSON object. Reading for display never fails on a bad file."""
    return paths.load_json_object(path or paths.projects_map_path())


def _read_for_edit(path):
    """The document for a mutation: {} when the file is missing, but ``ProjectsError`` when it
    exists and is unreadable or not a JSON object — a bad file must never be silently overwritten."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as err:
        raise ProjectsError("projects.json is not valid JSON; fix or move it") from err
    if not isinstance(doc, dict):
        raise ProjectsError("projects.json is not valid JSON; fix or move it")
    return doc


def save_doc(doc, path=None):
    """Write `doc` atomically (temp file + os.replace), keeping the file's mode (600 when new)."""
    path = path or paths.projects_map_path()
    data = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    paths.write_atomic(path, data, default_mode=0o600, backup=False)


def _stale(lock):
    """True when the lock file is older than LOCK_STALE_S (or already gone): safe to break."""
    try:
        return time.time() - os.stat(lock).st_mtime > LOCK_STALE_S
    except OSError:
        return True


def _acquire_lock(lock):
    """Create the exclusive lock file, retrying for LOCK_TIMEOUT_S; a stale lock is broken.
    Returns the open fd. Raises ProjectsError when a fresh lock is still held after the timeout."""
    deadline = time.time() + LOCK_TIMEOUT_S
    while True:
        try:
            return os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _stale(lock):
                with contextlib.suppress(OSError):
                    os.remove(lock)
                continue
            if time.time() >= deadline:
                raise ProjectsError("projects.json is locked; another sessioncoach command is writing it") from None
            time.sleep(0.05)


@contextlib.contextmanager
def edit(path=None):
    """Serialize a read-modify-write on projects.json with an exclusive lock file, yield the
    document, and release the lock in `finally`. The body mutates the document and calls
    `save_doc`; a body that raises never writes (and the lock is still released)."""
    path = path or paths.projects_map_path()
    lock = path + ".lock"
    fd = _acquire_lock(lock)
    try:
        yield _read_for_edit(path)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.remove(lock)


def group_labels(doc=None):
    """-> {group id: label} in insertion order. A group whose label is missing shows its id."""
    doc = load_doc() if doc is None else doc
    groups = doc.get("groups")
    if not isinstance(groups, dict):
        return {}
    return {k: (v.get("label") if isinstance(v, dict) else None) or k for k, v in groups.items()}


def _groups_of(doc):
    """doc["groups"] as a dict, created (and stored) when it is missing or not a dict."""
    groups = doc.get("groups")
    if not isinstance(groups, dict):
        groups = {}
        doc["groups"] = groups
    return groups


def _group_id(doc, group):
    """The id `group` names: itself when it is a group id, else its slug when that is one, else the slug."""
    groups = doc.get("groups") if isinstance(doc.get("groups"), dict) else {}
    if group in groups:
        return group
    s = slug(group)
    if s and s in groups:
        return s
    return s or group


def _is_pattern(target):
    """True when `target` is an fnmatch pattern over the encoded dir name. An absolute path, a
    `~` path, or an existing directory is a folder even when its name carries glob characters."""
    if not isinstance(target, str):
        return False
    if os.path.isabs(target) or target.startswith("~") or os.path.isdir(target):
        return False
    return any(c in target for c in _GLOB_CHARS)


def _target_matches(target):
    """The rule `match` strings a target stands for: one pattern, or a folder's two rules
    (itself and its subfolders)."""
    if _is_pattern(target):
        return (target,)
    enc = encode_dir(os.path.abspath(os.path.expanduser(target)))
    return (enc, enc + "-*")


def _skip_folder(folder):
    """True for a folder sync-herdr must not assign: empty, or the user's home."""
    if not isinstance(folder, str) or not folder.strip():
        return True
    p = os.path.normpath(os.path.expanduser(folder))
    return p == os.path.normpath(paths.HOME)


# ---- mutations --------------------------------------------------------------------------------

def add_group(id, label, path=None):
    """Add (or rename, if the id exists) a group. `id` defaults to the slug of `label`."""
    with edit(path) as doc:
        groups = _groups_of(doc)
        gid = id or slug(label)
        entry = groups.get(gid)
        if not isinstance(entry, dict):
            entry = {}
            groups[gid] = entry
        entry["label"] = label
        save_doc(doc, path)
    return gid


def assign(group, target, source="cli", path=None):
    """Assign `target` (a folder path or a pattern) to `group`; new rules go first and any rule
    with the same match is replaced. The group is created (id = slug, label = `group`) if missing."""
    with edit(path) as doc:
        groups = _groups_of(doc)
        gid = _group_id(doc, group)
        if gid not in groups:
            groups[gid] = {"label": group}
        matches = _target_matches(target)
        rules = [r for r in (doc.get("rules") or []) if isinstance(r, dict)]
        rules = [r for r in rules if r.get("match") not in matches]
        doc["rules"] = [{"match": m, "tenant": gid, "source": source} for m in matches] + rules
        save_doc(doc, path)
    return gid


def unassign(target, path=None):
    """Drop every rule `target` stands for (a folder's two rules, or one pattern). Returns the count."""
    with edit(path) as doc:
        matches = _target_matches(target)
        rules = [r for r in (doc.get("rules") or []) if isinstance(r, dict)]
        doc["rules"] = [r for r in rules if r.get("match") not in matches]
        save_doc(doc, path)
        return len(rules) - len(doc["rules"])


def rename_group(id, label, path=None):
    """Give a group a new display label."""
    with edit(path) as doc:
        groups = _groups_of(doc)
        gid = _group_id(doc, id)
        entry = groups.get(gid)
        if not isinstance(entry, dict):
            entry = {}
            groups[gid] = entry
        entry["label"] = label
        save_doc(doc, path)
    return gid


def remove_group(id, force=False, path=None):
    """Drop a group; refuses (ProjectsError) while rules use it unless `force` also drops those rules."""
    with edit(path) as doc:
        groups = _groups_of(doc)
        gid = _group_id(doc, id)
        rules = [r for r in (doc.get("rules") or []) if isinstance(r, dict)]
        in_use = [r for r in rules if r.get("tenant") == gid]
        if in_use and not force:
            raise ProjectsError("group %r is used by %d rule%s; pass --force to remove them too"
                                % (gid, len(in_use), "" if len(in_use) == 1 else "s"))
        doc["groups"] = {k: v for k, v in groups.items() if k != gid}
        if force:
            doc["rules"] = [r for r in rules if r.get("tenant") != gid]
        save_doc(doc, path)
    return gid


# ---- the Herdr feed ---------------------------------------------------------------------------

def _validate_sync(mapping):
    """Validate the sync-herdr input; raise SyncError on anything off-schema, without writing."""
    if not isinstance(mapping, dict):
        raise SyncError('the input must be an object with a "groups" list')
    groups = mapping.get("groups")
    if not isinstance(groups, list):
        raise SyncError('"groups" must be a list')
    if len(groups) > MAX_GROUPS:
        raise SyncError("at most %d groups" % MAX_GROUPS)
    for g in groups:
        if not isinstance(g, dict):
            raise SyncError("each group must be an object")
        name = g.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME:
            raise SyncError("each group needs a non-empty name of at most %d characters" % MAX_NAME)
        folders = g.get("folders")
        if not isinstance(folders, list):
            raise SyncError('each group\'s "folders" must be a list of paths')
        if len(folders) > MAX_FOLDERS:
            raise SyncError("at most %d folders per group" % MAX_FOLDERS)
        for f in folders:
            if not isinstance(f, str) or len(f) > MAX_PATH:
                raise SyncError("each folder must be an absolute path of at most %d characters" % MAX_PATH)
            if not os.path.isabs(f):
                raise SyncError("folders must be absolute paths (%r is not)" % f)
    return mapping


def _sync_plan(mapping):
    """[(group id, leaf label, [folders])] from a validated input: the same leaf name merges into
    one group (one client), and two different leaves whose slug collides get `-2`, `-3`…"""
    out, by_leaf, assigned = [], {}, set()
    for g in mapping["groups"]:
        leaf = g["name"].split("/")[-1].strip()
        folders = g["folders"]
        if leaf in by_leaf:
            out[by_leaf[leaf]][2].extend(folders)
            continue
        base = slug(leaf) or "group"
        gid, n = base, 2
        while gid in assigned:
            gid = "%s-%d" % (base, n)
            n += 1
        assigned.add(gid)
        by_leaf[leaf] = len(out)
        out.append([gid, leaf, list(folders)])
    return out


def _apply_sync(doc, mapping):
    """A new doc with the herdr rules replaced by `mapping`'s set; cli/manual rules and unknown keys kept."""
    new = copy.deepcopy(doc)
    groups = _groups_of(new)
    herdr = []
    for gid, leaf, folders in _sync_plan(mapping):
        if gid not in groups:  # only missing groups are created: an existing label is never overwritten
            groups[gid] = {"label": leaf}
        for folder in folders:
            if _skip_folder(folder):
                continue
            enc = encode_dir(os.path.abspath(os.path.expanduser(folder)))
            herdr.append({"match": enc, "tenant": gid, "source": "herdr"})
            herdr.append({"match": enc + "-*", "tenant": gid, "source": "herdr"})
    rules = new.get("rules") if isinstance(new.get("rules"), list) else []
    new["rules"] = [r for r in rules if not (isinstance(r, dict) and r.get("source") == "herdr")] + herdr
    return new


def sync_herdr(mapping, path=None):
    """Replace every ``"source": "herdr"`` rule with `mapping`'s set (cli/manual rules are never
    touched); create missing groups and skip the home folder. Idempotent: when the resulting
    document equals the current one the file is not rewritten. Returns a one-line summary."""
    _validate_sync(mapping)
    with edit(path) as doc:
        new = _apply_sync(doc, mapping)
        herdr = [r for r in new.get("rules") if isinstance(r, dict) and r.get("source") == "herdr"]
        if new == doc:
            return "herdr groups: unchanged (%d rules)" % len(herdr)
        save_doc(new, path)
        return "herdr groups: %d group%s, %d rules" % (
            len(_sync_plan(mapping)), "" if len(_sync_plan(mapping)) == 1 else "s", len(herdr))


class Labeler:
    def __init__(self, rules=None):
        self.rules = rules or []
        self._cache = {}
        home = encode_dir(paths.HOME)  # Claude Code: every non-alphanumeric char becomes "-" (C:\Users\me -> C--Users-me)
        self._prefix = home + "-"

    @classmethod
    def load(cls, path=None):
        path = path or paths.projects_map_path()
        rules = paths.load_json_object(path).get("rules")
        return cls([r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else [])

    def __call__(self, project_dir):
        """-> (project, tenant)"""
        if project_dir not in self._cache:
            self._cache[project_dir] = self._resolve(project_dir)
        return self._cache[project_dir]

    def _resolve(self, project_dir):
        for r in self.rules:
            if fnmatch.fnmatch(owner_dir(project_dir), r.get("match", "")):
                # a rule without "project" keeps the automatic label and only sets the tenant
                return r.get("project") or self._default(project_dir), r.get("tenant") or UNASSIGNED
        return self._default(project_dir), UNASSIGNED

    def _default(self, project_dir):
        project_dir = owner_dir(project_dir)
        name = project_dir[len(self._prefix):] if project_dir.startswith(self._prefix) else project_dir.lstrip("-")
        return name.replace("-", "/") or project_dir
