"""Human labels for Claude Code project dirs ('-Users-me-code-app' -> 'code/app').

An optional private file (<data dir>/projects.json) maps dirs to a project and a tenant
(workspace), e.g. to merge git worktrees into one project:

  {"rules": [{"match": "-Users-me-code-app*", "project": "app", "tenant": "acme"}]}

Rules are fnmatch patterns over the dir name; the first match wins.

The same file also names **groups** (clients): a tenant with a display name, kept in insertion
order. A rule may carry ``"source": "cli" | "herdr"`` (absent = written by hand = "manual").
Every function here writes the file atomically (temp file + ``os.replace``) and keeps ``_comment``
and any key it does not own.
"""
import copy
import fnmatch
import json
import os
import re

from . import paths
from .collect import encode_dir

UNASSIGNED = "unassigned"
# what a session started inside a Claude Code scratchpad leaves: /tmp/claude-<uid>/<project dir>/<session uuid>/...
SCRATCHPAD = re.compile(r"^-(?:private-)?tmp-claude-\d+-(-.+?)-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?:-.*)?$")

_SLUG = re.compile(r"[^a-z0-9]+")  # a group id is a slug: lowercase letters, digits and dashes
_GLOB_CHARS = ("*", "?", "[")


def slug(label):
    """'Pretty Co' -> 'pretty-co'. A label with no alphanumeric character gives ''."""
    return _SLUG.sub("-", (label or "").lower()).strip("-")


def owner_dir(project_dir):
    """The project a scratchpad's dir belongs to; any other dir stays as it is."""
    m = SCRATCHPAD.match(project_dir)
    return m.group(1) if m else project_dir


# ---- the projects.json document ----------------------------------------------------------------

def load_doc(path=None):
    """The whole projects.json document (every key kept, ``_comment`` included); {} when absent."""
    return paths.load_json_object(path or paths.projects_map_path())


def save_doc(doc, path=None):
    """Write `doc` atomically (temp file + os.replace), keeping the file's mode (600 when new)."""
    path = path or paths.projects_map_path()
    data = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    paths.write_atomic(path, data, default_mode=0o600, backup=False)


def group_labels(doc=None):
    """-> {group id: label} in insertion order. A group whose label is missing shows its id."""
    doc = load_doc() if doc is None else doc
    groups = doc.get("groups")
    if not isinstance(groups, dict):
        return {}
    return {k: (v.get("label") if isinstance(v, dict) else None) or k for k, v in groups.items()}


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
    """True when `target` is an fnmatch pattern over the encoded dir name, not a folder path."""
    return isinstance(target, str) and any(c in target for c in _GLOB_CHARS)


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
    doc = load_doc(path)
    groups = doc.setdefault("groups", {})
    if not isinstance(groups, dict):
        groups = {}
        doc["groups"] = groups
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
    doc = load_doc(path)
    groups = doc.setdefault("groups", {})
    if not isinstance(groups, dict):
        groups = {}
        doc["groups"] = groups
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
    doc = load_doc(path)
    matches = _target_matches(target)
    rules = [r for r in (doc.get("rules") or []) if isinstance(r, dict)]
    doc["rules"] = [r for r in rules if r.get("match") not in matches]
    save_doc(doc, path)
    return len(rules) - len(doc["rules"])


def rename_group(id, label, path=None):
    """Give a group a new display label."""
    doc = load_doc(path)
    groups = doc.setdefault("groups", {})
    if not isinstance(groups, dict):
        groups = {}
        doc["groups"] = groups
    gid = _group_id(doc, id)
    entry = groups.get(gid)
    if not isinstance(entry, dict):
        entry = {}
        groups[gid] = entry
    entry["label"] = label
    save_doc(doc, path)
    return gid


def remove_group(id, force=False, path=None):
    """Drop a group; refuses (ValueError) while rules use it unless `force` also drops those rules."""
    doc = load_doc(path)
    groups = doc.get("groups") if isinstance(doc.get("groups"), dict) else {}
    gid = _group_id(doc, id)
    rules = [r for r in (doc.get("rules") or []) if isinstance(r, dict)]
    in_use = [r for r in rules if r.get("tenant") == gid]
    if in_use and not force:
        raise ValueError("group %r is used by %d rule%s; pass --force to remove them too"
                         % (gid, len(in_use), "" if len(in_use) == 1 else "s"))
    doc["groups"] = {k: v for k, v in groups.items() if k != gid}
    if force:
        doc["rules"] = [r for r in rules if r.get("tenant") != gid]
    save_doc(doc, path)
    return gid


# ---- the Herdr feed ---------------------------------------------------------------------------

def _sync_groups(mapping):
    """[(group id, leaf label, folder, ...)] from a sync-herdr input, empty for a bad shape."""
    groups = mapping.get("groups") if isinstance(mapping, dict) else None
    if not isinstance(groups, list):
        return []
    out = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = g.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        leaf = name.split("/")[-1].strip()  # nested groups use the leaf name
        out.append((slug(leaf) or slug(name) or "group", leaf, g.get("folders")))
    return out


def _apply_sync(doc, mapping):
    """A new doc with the herdr rules replaced by `mapping`'s set; cli/manual rules and unknown keys kept."""
    new = copy.deepcopy(doc)
    groups = new.setdefault("groups", {})
    if not isinstance(groups, dict):
        groups = {}
        new["groups"] = groups
    herdr = []
    for gid, leaf, folders in _sync_groups(mapping):
        if gid not in groups:  # only missing groups are created: an existing label is never overwritten
            groups[gid] = {"label": leaf}
        for folder in folders if isinstance(folders, list) else ():
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
    touched); create missing groups and skip empty or home folders. Idempotent: the same input
    changes nothing, so the file is not rewritten. Returns a one-line summary."""
    doc = load_doc(path)
    new = _apply_sync(doc, mapping)
    herdr = [r for r in new.get("rules") if isinstance(r, dict) and r.get("source") == "herdr"]
    if new == doc:
        return "herdr groups: unchanged (%d rules)" % len(herdr)
    save_doc(new, path)
    return "herdr groups: %d group%s, %d rules" % (len(_sync_groups(mapping)),
                                                   "" if len(_sync_groups(mapping)) == 1 else "s", len(herdr))


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
