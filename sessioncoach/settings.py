"""Settings panel backend: read the state of each practice by scope, compute the proposed change as a
unified diff WITHOUT writing, and apply exactly that change.

Scopes (where a practice is applied):
  global   ~/.claude/settings.json                         (+ ~/.claude/CLAUDE.md)
  project  <project>/.claude/settings.local.json            (+ the cut rule in <project>/CLAUDE.local.md, personal,
           or in <project>/CLAUDE.md, shared through git: the user chooses; CLAUDE.local.md can be added to the
           project's .gitignore as one more previewed change)
  company  <config dir>/settings.json of an account         (+ <config dir>/CLAUDE.md); the config dir is the
           parent of each root in accounts.json → accounts.*.roots

Practices: the resumed-session notice (UserPromptSubmit hook), the chained status line, the cut rule in
CLAUDE.md (a delimited block), the thresholds in alerts.json (merged key by key, private keys such as
budgets are never touched) and the daily review (alerts.json → review + its LaunchAgent).

Duplicates: Claude Code loads the user settings (~/.claude, or the account's own config dir) plus the project
settings, so the hook in a project AND in its superior scope (the company that owns the project, else global)
would run twice: that is refused. Global and company never load together, so they never duplicate.

Safety: the client never sends a path. A scope is an id that this module resolves itself, and a project is
only accepted if it is a directory whose encoded name is in the database (or the dashboard's launch dir).
Every write makes a timestamped backup, is atomic (tmp + os.replace) and keeps the file's permissions.
`apply` recomputes the plan and refuses (409) if any file changed since the preview.
"""
import copy
import difflib
import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time

from . import accounts, config, limits, nudge, paths, review, scheduler
from .collect import encode_dir
from .projects import UNASSIGNED, Labeler

CUT_OPEN = "<!-- sessioncoach:cut-rule -->"
CUT_CLOSE = "<!-- /sessioncoach:cut-rule -->"
# every pair ever written, current first: a block written under the old name is still found and removed
CUT_MARKERS = ((CUT_OPEN, CUT_CLOSE), ("<!-- daf-claude-kit:cut-rule -->", "<!-- /daf-claude-kit:cut-rule -->"))
CUT_TEXT = {
    "es": "## Cuándo cortar la sesión\nSi pasas a una tarea sin relación con la anterior y la sesión supera ~150k tokens "
          "de contexto, propón en una línea cerrar con un traspaso y abrir una sesión nueva.",
    "en": "## When to cut the session\nIf you switch to a task unrelated to the previous one and the session is over "
          "~150k tokens of context, suggest in one line closing with a handoff and opening a new session.",
}
# what the cut rule gives the user, said by `sessioncoach setup` before asking
CUT_VALUE = ("When you move to an unrelated task in a big session, Claude itself suggests a handoff and a fresh session, "
             "instead of carrying the old context, and paying for it, on every message of the new task.")
STATUSLINE_MARK = " statusline --chain-file "
SENSITIVE = re.compile(r'("[^"\n]*(?:token|secret|key|password|credential|auth)[^"\n]*"\s*:\s*)"[^"\n]*"', re.I)
REQ_KEYS = {"scope", "lang", "nudge_hook", "statusline", "cut_rule", "review_agent", "alerts", "files",
            "cut_dest", "gitignore", "remove_project_hooks"}
CUT_DESTS = ("local", "shared")
GITIGNORE_LINE = "CLAUDE.local.md"
ALERT_KEYS, NUDGE_KEYS, REVIEW_KEYS = config.ALERT_KEYS, config.NUDGE_KEYS, config.REVIEW_KEYS  # they live in config
CHANNELS = ("notification", "dashboard", "email")
LOCK = threading.Lock()

class SettingsError(Exception):
    def __init__(self, code, msg, **extra):
        super().__init__(msg)
        self.code, self.msg, self.extra = code, msg, extra


def _scheduler_backend():
    """scheduler.backend(), with a mistyped SESSIONCOACH_SCHEDULER as a 422 the panel shows, not a crash."""
    try:
        return scheduler.backend()
    except ValueError as e:
        raise SettingsError(422, str(e)) from e


def _review_installed(agents_dir):
    try:
        return review.installed(agents_dir)
    except ValueError:  # a mistyped SESSIONCOACH_SCHEDULER: shown as off; a change then says why (422)
        return False


def sha(b):
    return "absent" if b is None else hashlib.sha256(b).hexdigest()


def read_bytes(path):
    """Bytes of `path`, None when absent; any other failure is a 422."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise SettingsError(422, "cannot read %s: %s" % (path, e.strerror)) from e


_read = read_bytes  # the name it had before it was shared between modules


def _json_obj(raw, what):
    """Parsed JSON object of a settings-like file (empty/absent -> {})."""
    if raw is None or not raw.strip():
        return {}
    try:
        d = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise SettingsError(422, "%s is not valid JSON; fix it by hand first" % what) from e
    if not isinstance(d, dict):
        raise SettingsError(422, "%s is not a JSON object" % what)
    return d


def read_text(path, what=None):
    """UTF-8 text of `path`, None when absent. A file that is not UTF-8 is refused (422): it is never rewritten lossy."""
    raw = read_bytes(path)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SettingsError(422, "%s is not UTF-8; fix it by hand first" % (what or path)) from e


def _dump(d):
    return (json.dumps(d, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _run_plans(before, steps, what):
    """Apply pure settings plans to a settings file's bytes; nothing is written.

    Each step takes the settings dict and returns (new dict or None for no change, chain file text or None).
    -> (settings bytes, chain file bytes or None). With no change the original bytes come back as they were."""
    doc, chain, changed = _json_obj(before, what), None, False
    try:
        for step in steps:
            new, text = step(doc)
            if new is not None:
                doc, changed = new, True
            if text is not None:
                chain = text.encode("utf-8")
    except SettingsError:
        raise
    except Exception as e:
        raise SettingsError(422, "cannot change the settings file: %s" % e) from e
    return (_dump(doc) if changed else before), chain


# ---------- validation ----------

def _int(v, lo, hi, name):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and (not math.isfinite(v) or not v.is_integer())):
        raise SettingsError(422, "%s must be an integer" % name)
    if not lo <= v <= hi:
        raise SettingsError(422, "%s must be between %s and %s" % (name, lo, hi))
    return int(v)


def _bool(v, name):
    if not isinstance(v, bool):
        raise SettingsError(422, "%s must be true or false" % name)
    return v


def _list(v, lo, hi, name):
    if not isinstance(v, list) or len(v) > 8:
        raise SettingsError(422, "%s must be a list of up to 8 numbers" % name)
    return sorted({_int(x, lo, hi, name) for x in v})


def validate_alerts(a, effective):
    """-> {"top": {}, "nudge": {}, "review": {}} with only the values that differ from `effective`."""
    if not isinstance(a, dict):
        raise SettingsError(422, "alerts must be an object")
    bad = set(a) - ALERT_KEYS
    if bad:
        raise SettingsError(422, "unknown alert keys: %s" % ", ".join(sorted(map(str, bad))))
    top, nd, rv = {}, {}, {}
    for k in ("five_hour", "seven_day", "context"):
        if k in a:
            top[k] = _list(a[k], 1, 100, k)
    if "context_tokens" in a:
        top["context_tokens"] = _list(a["context_tokens"], 1000, 10_000_000, "context_tokens")
    for k in ("projection", "notify"):
        if k in a:
            top[k] = _bool(a[k], k)
    if "lang" in a:
        if a["lang"] not in ("es", "en", None):
            raise SettingsError(422, "lang must be es, en or null")
        top["lang"] = a["lang"]
    n = a.get("nudge", {})
    if not isinstance(n, dict) or set(n) - NUDGE_KEYS:
        raise SettingsError(422, "invalid nudge settings")
    if "idle_min" in n:
        nd["idle_min"] = _int(n["idle_min"], 1, 1440, "nudge.idle_min")
    if "min_context_tokens" in n:
        nd["min_context_tokens"] = _int(n["min_context_tokens"], 1000, 10_000_000, "nudge.min_context_tokens")
    r = a.get("review", {})
    if not isinstance(r, dict) or set(r) - REVIEW_KEYS:
        raise SettingsError(422, "invalid review settings")
    if "enabled" in r:
        rv["enabled"] = _bool(r["enabled"], "review.enabled")
    if "time" in r:
        if not isinstance(r["time"], str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", r["time"]):
            raise SettingsError(422, "review.time must be HH:MM")
        rv["time"] = r["time"]
    if "days" in r:
        d = r["days"]
        if not isinstance(d, list) or not d or any(x not in review.WEEKDAYS for x in d):
            raise SettingsError(422, "review.days must be a non-empty list of mon..sun")
        rv["days"] = [x for x in review.WEEKDAYS if x in d]
    if "channels" in r:
        c = r["channels"]
        if not isinstance(c, list) or not c or any(x not in CHANNELS for x in c):
            raise SettingsError(422, "review.channels must be a non-empty list of %s" % ", ".join(CHANNELS))
        rv["channels"] = [x for x in CHANNELS if x in c]
        if "email" in rv["channels"] and not (effective.get("review") or {}).get("email_command"):
            raise SettingsError(422, "the email channel needs review.email_command in alerts.json")
    if "lookback" in r:
        lb = r["lookback"]
        rv["lookback"] = lb if lb == "last_active" else _int(lb, 0, 60, "review.lookback")
    if "max_tips" in r:
        rv["max_tips"] = _int(r["max_tips"], 1, 5, "review.max_tips")
    if "long_context_tokens" in r:
        rv["long_context_tokens"] = _int(r["long_context_tokens"], 10_000, 10_000_000, "review.long_context_tokens")
    cur_n, cur_r = effective.get("nudge") or {}, effective.get("review") or {}
    return {"top": {k: v for k, v in top.items() if effective.get(k) != v},
            "nudge": {k: v for k, v in nd.items() if cur_n.get(k) != v},
            "review": {k: v for k, v in rv.items() if cur_r.get(k) != v}}


# ---------- CLAUDE.md block ----------

def _cut_block(text):
    """(start, end) of the first complete cut-rule block under any marker pair, or None."""
    for o, c in CUT_MARKERS:
        s = text.find(o) if text is not None else -1
        if s != -1 and text.find(c, s) != -1:
            return s, text.find(c, s) + len(c)
    return None


def has_cut_rule(text):
    return _cut_block(text) is not None


def add_cut_rule(text, lang):
    if has_cut_rule(text):
        return text
    block = "%s\n%s\n%s\n" % (CUT_OPEN, CUT_TEXT[lang], CUT_CLOSE)
    if not text or not text.strip():
        return block
    return text + ("" if text.endswith("\n") else "\n") + "\n" + block


def remove_cut_rule(text):
    while has_cut_rule(text):
        s, e = _cut_block(text)
        before, after = text[:s], text[e:]
        if after.startswith("\n"):
            after = after[1:]
        if before.endswith("\n\n"):
            before = before[:-1]
        text = before + after
    return text


# ---------- file writing ----------

_backup = paths.timestamp_backup
_private_dirs = paths.private_dirs
write_atomic = paths.write_atomic  # lives in paths so limits and nudge can use it too


def _diff(path, before, after):
    a = (before or b"").decode("utf-8", "replace").splitlines(keepends=True)
    b = (after or b"").decode("utf-8", "replace").splitlines(keepends=True)
    out = list(difflib.unified_diff(a, b, fromfile="a/" + path.lstrip("/") if before is not None else "/dev/null",
                                    tofile="b/" + path.lstrip("/") if after is not None else "/dev/null", n=2))
    # last line without newline would glue to the next header
    return "".join(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n" for ln in out)


def _redact(diff):
    return "".join(SENSITIVE.sub(r'\1"[redacted]"', ln) if ln[:1] in " +-" and not ln.startswith(("+++", "---")) else ln
                   for ln in diff.splitlines(keepends=True))


# ---------- scopes ----------

def hook_installed(s):
    """True when the settings dict `s` has sessioncoach's hook, in any command format."""
    return bool(nudge.our_commands(s))


def statusline_installed(s):
    """True when the settings dict `s` has a chained sessioncoach status line (any format; another tool's is not)."""
    return bool(limits.our_command(s))


_hook_installed, _statusline_installed = hook_installed, statusline_installed  # their names before they were shared


def git_ignore_state(directory, name=GITIGNORE_LINE):
    """'ignored' | 'not_ignored' (a git repo that does not ignore `name`) | 'no_repo' | 'unknown' (git unavailable)."""
    try:
        r = subprocess.run(["git", "-C", directory, "check-ignore", "-q", "--", name], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return {0: "ignored", 1: "not_ignored", 128: "no_repo"}.get(r.returncode, "unknown")

def add_gitignore_line(raw, line=GITIGNORE_LINE):
    text = (raw or b"").decode("utf-8")  # strict: replacing bytes would corrupt the user's file on rewrite
    return (text + ("" if not text or text.endswith("\n") else "\n") + line + "\n").encode("utf-8")

def project_superiors(project_dir, home):
    """[(label, settings path)] of the user-level settings Claude Code loads together with this project's.
    The company that owns the project (projects.json tenant -> accounts.json company -> config dir of its roots);
    when that cannot be determined (or its config dir is ~/.claude) the global one."""
    glob = os.path.join(home, ".claude")
    out = []
    try:
        tenant = Labeler.load()(encode_dir(project_dir))[1]
    except Exception:  # a projects.json rule of an odd shape: no tenant, the global settings
        tenant = UNASSIGNED
    if tenant and tenant != UNASSIGNED:
        for acc_id, m in sorted(accounts.load_map().items()):
            if not isinstance(m, dict) or m.get("company") != tenant:
                continue
            for root in (r for r in (m.get("roots") or []) if isinstance(r, str)):
                cfg = accounts.config_dir_of(root)
                if os.path.realpath(cfg) != os.path.realpath(glob) and not any(o[1] == os.path.join(cfg, "settings.json") for o in out):
                    out.append(("company " + (m.get("label") or acc_id), os.path.join(cfg, "settings.json")))
    return out or [("global", os.path.join(glob, "settings.json"))]

def settings_have(path, what):
    """True if the settings file at `path` has sessioncoach's hook / status line (unreadable counts as no)."""
    try:
        s = _json_obj(read_bytes(path), path)
    except SettingsError:
        return False
    return hook_installed(s) if what == "hook" else statusline_installed(s)


def hook_duplicated_in(superiors):
    """Label of the first superior scope (see project_superiors) that already has the hook, or None.
    Claude Code loads it together with the project's own settings, so a hook in the project would run twice."""
    return next((lbl for lbl, p in superiors if settings_have(p, "hook")), None)


def statusline_shadowed_by(superiors):
    """Label of the first superior scope that already has the status line, or None: the project's own line wins over it."""
    return next((lbl for lbl, p in superiors if settings_have(p, "statusline")), None)


# ---------- plan pieces ----------

PLAN_ORDER = {"chain": 0, "settings": 1, "claude_md": 2, "gitignore": 3, "alerts": 4, "plist": 5, "schedule": 5}  # write order


def _cut_rule_targets(want, present):
    """Where the cut rule will be ("local" / "shared"), given where it is now (`present`) and the request."""
    if "cut_rule" not in want:
        return present
    if not want["cut_rule"]:
        return set()
    if want.get("cut_dest"):
        return {want["cut_dest"]}
    return present or {"local"}


def _merge_alerts(cur, v):
    """alerts.json `cur` with the validated values `v` (validate_alerts) merged in; a top-level None removes the key."""
    new = copy.deepcopy(cur)
    for k, val in v["top"].items():
        if val is None:
            new.pop(k, None)
        else:
            new[k] = val
    for sub in ("nudge", "review"):
        if v[sub]:
            base = new.get(sub) or {}
            if not isinstance(base, dict):
                raise SettingsError(422, "alerts.json → %s is not an object" % sub)
            base = dict(base)
            base.update(v[sub])
            new[sub] = base
    return new


class Settings:
    def __init__(self, home=None, agents_dir=None, load_agent=True, kit_bin=None, projects=None, cwd=None):
        self.home = home or paths.HOME
        self.agents_dir = agents_dir
        self.load_agent = load_agent
        self.kit_bin = kit_bin
        self.projects = projects or (lambda: [])  # encoded dir names known to the database
        self.cwd = cwd
        self._cache = (0, None)

    # -- scopes
    def _short(self, p):
        return "~" + p[len(self.home):] if p == self.home or p.startswith(self.home + os.sep) else p

    def _project_dirs(self):
        """{real path: encoded name} of directories that exist and whose encoded name is known."""
        now = time.time()
        if self._cache[1] is not None and now - self._cache[0] < 30:
            return self._cache[1]
        wanted = {n for n in self.projects() if isinstance(n, str) and n}
        out = {}
        listing = {}

        def entries(d):
            if d not in listing:
                try:
                    listing[d] = sorted(os.listdir(d))
                except OSError:
                    listing[d] = []
            return listing[d]

        def walk(base, enc, found):
            """Every directory under `base` whose encoded name is `enc` (real paths), stopping at the second one."""
            for e in entries(base):
                if len(found) > 1:
                    return
                cand = os.path.join(base, e)
                ec = encode_dir(cand)
                if ec == enc and os.path.isdir(cand):
                    found.add(os.path.realpath(cand))
                elif enc.startswith(ec + "-") and os.path.isdir(cand):
                    walk(cand, enc, found)
        for enc in sorted(wanted)[:400]:
            found = set()
            if enc.startswith("-"):
                walk(os.sep, enc, found)
            elif os.name == "nt" and re.match(r"[A-Za-z]--", enc):  # C:\Users\me\app -> "C--Users-me-app"
                walk(enc[0] + ":" + os.sep, enc, found)
            # /a/b-c and /a-b/c both encode to -a-b-c: with two candidates the project is unknown, so it gets no
            # scope (nothing is written for it) unless it is the cwd, whose path is known
            if len(found) == 1:
                out[found.pop()] = enc
        if self.cwd and os.path.isabs(self.cwd) and os.path.isdir(self.cwd):
            out.setdefault(os.path.realpath(self.cwd), encode_dir(self.cwd))
        home_real = os.path.realpath(self.home)
        out = {p: e for p, e in out.items() if p not in (os.sep, home_real)}
        self._cache = (now, out)
        return out

    def scopes(self):
        """{id: scope dict}; paths come from here only, never from the client."""
        h = self.home
        out = {"global": {"id": "global", "kind": "global", "label": "~/.claude", "dir": os.path.join(h, ".claude"),
                          "settings": os.path.join(h, ".claude", "settings.json"),
                          "claude_md": os.path.join(h, ".claude", "CLAUDE.md"),
                          "chain": os.path.join(paths.data_dir(), "statusline-chain.sh")}}
        glob_real = os.path.realpath(os.path.join(h, ".claude"))
        for acc_id, m in sorted(accounts.load_map().items()):
            roots = (m or {}).get("roots") or []
            for i, root in enumerate(r for r in roots if isinstance(r, str)):
                cfg = accounts.config_dir_of(root)
                if not os.path.isdir(cfg) or os.path.realpath(cfg) == glob_real:
                    continue
                sid = "company:%s%s" % (acc_id, "" if len(roots) == 1 else "#%d" % i)
                out[sid] = {"id": sid, "kind": "company", "label": (m.get("label") or acc_id), "dir": cfg,
                            "settings": os.path.join(cfg, "settings.json"), "claude_md": os.path.join(cfg, "CLAUDE.md"),
                            "chain": limits.chain_path(os.path.join(cfg, "settings.json"))}
        for p in sorted(self._project_dirs()):
            sid = "project:" + hashlib.sha1(p.encode()).hexdigest()[:10]
            out[sid] = {"id": sid, "kind": "project", "label": self._short(p), "dir": p,
                        "settings": os.path.join(p, ".claude", "settings.local.json"),
                        "claude_md": os.path.join(p, "CLAUDE.local.md"), "claude_md_shared": os.path.join(p, "CLAUDE.md"),
                        "gitignore": os.path.join(p, ".gitignore"),
                        "chain": limits.chain_path(os.path.join(p, ".claude", "settings.local.json"))}
        return out

    def _superiors(self, sc):
        return project_superiors(sc["dir"], self.home) if sc["kind"] == "project" else []

    def projects_with_hook(self, sc):
        """Project scopes with their own hook whose superior scope is `sc` (global or company)."""
        if sc["kind"] == "project":
            return []
        mine = os.path.realpath(sc["settings"])
        out = []
        for p in self.scopes().values():
            if p["kind"] == "project" and settings_have(p["settings"], "hook") and \
                    any(os.path.realpath(path) == mine for _l, path in self._superiors(p)):
                out.append(p)
        return out

    _projects_with_hook = projects_with_hook  # the name it had before it was shared between modules

    def projects_with_hook_at(self, settings_path):
        """projects_with_hook for the global / company scope whose settings file is `settings_path` ([] if it is none)."""
        mine = os.path.realpath(settings_path)
        sc = next((x for x in self.scopes().values() if x["kind"] != "project" and os.path.realpath(x["settings"]) == mine), None)
        return self.projects_with_hook(sc) if sc else []

    # -- state
    def _scope_state(self, sc):
        st = {"id": sc["id"], "kind": sc["kind"], "label": sc["label"], "settings_file": self._short(sc["settings"]),
              "claude_md_file": self._short(sc["claude_md"]), "nudge_hook": None, "statusline": None, "cut_rule": None, "error": None}
        if sc["kind"] == "project":
            st.update(claude_md_shared_file=self._short(sc["claude_md_shared"]), cut_rule_in=[], gitignore_needed=False,
                      hook_blocked_by=None, statusline_shadows=None)
        else:
            st["hook_in_projects"] = [{"id": p["id"], "label": p["label"]} for p in self.projects_with_hook(sc)]
        try:
            s = _json_obj(read_bytes(sc["settings"]), self._short(sc["settings"]))
            st["nudge_hook"], st["statusline"] = hook_installed(s), statusline_installed(s)
        except SettingsError as e:
            st["error"] = e.msg
        try:
            md = read_bytes(sc["claude_md"])
            st["cut_rule"] = has_cut_rule(md.decode("utf-8", "replace")) if md is not None else False
            if sc["kind"] == "project":
                sh = read_bytes(sc["claude_md_shared"])
                in_shared = has_cut_rule(sh.decode("utf-8", "replace")) if sh is not None else False
                st["cut_rule_in"] = [k for k, on in (("local", st["cut_rule"]), ("shared", in_shared)) if on]
                st["cut_rule"] = bool(st["cut_rule_in"])
                st["gitignore_needed"] = git_ignore_state(sc["dir"]) == "not_ignored"
        except SettingsError as e:
            st["error"] = st["error"] or e.msg
        if sc["kind"] == "project":
            sup = self._superiors(sc)
            st["hook_blocked_by"] = hook_duplicated_in(sup)
            st["statusline_shadows"] = statusline_shadowed_by(sup)
        return st

    def _alerts_raw(self):
        raw = read_bytes(config.alerts_path())
        return raw, _json_obj(raw, "alerts.json")

    def state(self):
        eff = config.load_alerts()
        cfg = review.load_config(eff)
        nd = dict(config.NUDGE_DEFAULTS, **(eff.get("nudge") or {}))
        top_keys = ("five_hour", "seven_day", "context", "context_tokens", "projection", "notify")
        defaults_review = {k: config.REVIEW_DEFAULTS[k] for k in REVIEW_KEYS}
        return {
            "scopes": [self._scope_state(sc) for sc in self.scopes().values()],
            "alerts": dict({k: eff.get(k) for k in top_keys}, lang=eff.get("lang")),
            "nudge": {k: nd[k] for k in NUDGE_KEYS},
            "review": {k: cfg[k] for k in REVIEW_KEYS},
            "review_agent": _review_installed(self.agents_dir),
            "has_email_command": bool(cfg.get("email_command")),
            "defaults": {"alerts": {k: config.DEFAULT_ALERTS[k] for k in top_keys}, "nudge": dict(config.NUDGE_DEFAULTS),
                         "review": defaults_review},
            "cut_rule_text": CUT_TEXT,
            "weekdays": list(review.WEEKDAYS), "channels": list(CHANNELS),
        }

    # -- plan
    def _parse(self, req):
        if not isinstance(req, dict):
            raise SettingsError(400, "body must be a JSON object")
        bad = set(req) - REQ_KEYS
        if bad:
            raise SettingsError(422, "unknown keys: %s" % ", ".join(sorted(map(str, bad))))
        sid = req.get("scope", "global")
        scope = self.scopes().get(sid) if isinstance(sid, str) else None
        if not scope:
            raise SettingsError(400, "unknown scope")
        lang = req.get("lang") or config.cut_rule_lang()
        if lang not in CUT_TEXT:
            raise SettingsError(422, "lang must be es or en")
        want = {k: _bool(req[k], k) for k in ("nudge_hook", "statusline", "cut_rule", "review_agent", "gitignore",
                                              "remove_project_hooks") if k in req}
        if "cut_dest" in req:
            if scope["kind"] != "project":
                raise SettingsError(422, "cut_dest only applies to a project scope")
            if req["cut_dest"] not in CUT_DESTS:
                raise SettingsError(422, "cut_dest must be local or shared")
            want["cut_dest"] = req["cut_dest"]
        if want.get("gitignore") and scope["kind"] != "project":
            raise SettingsError(422, "gitignore only applies to a project scope")
        if want.get("remove_project_hooks") and scope["kind"] == "project":
            raise SettingsError(422, "remove_project_hooks only applies to the global or a company scope")
        return scope, lang, want

    def _plan(self, req):
        """-> (scope, items, warnings); items: dicts path/role/before/after/default_mode (nothing is written).
        One piece after another, in this order (it decides which error a request with several problems gets):
        settings file (hook, status line, projects' own hooks), cut rule, .gitignore, alerts.json, review job."""
        scope, lang, want = self._parse(req)
        items, warnings = [], []
        self._plan_settings(scope, want, items, warnings)
        desired = self._plan_cut_rule(scope, lang, want, items)
        self._plan_gitignore(scope, want, desired, items)
        new_cfg, changed = self._plan_alerts(req, items)
        touched_schedule = changed and any(k in ((req.get("alerts") or {}).get("review") or {}) for k in ("time", "days"))
        be = _scheduler_backend()
        agent_on = review.installed(self.agents_dir)
        if be != "launchd":
            self._plan_schedule(items, want.get("review_agent"), agent_on, touched_schedule, new_cfg)
        else:
            self._plan_launchagent(items, warnings, want.get("review_agent"), agent_on, touched_schedule, new_cfg)
        items.sort(key=lambda i: PLAN_ORDER[i["role"]])  # stable: same-role items keep the order they were planned in
        return scope, items, warnings

    def _plan_settings(self, scope, want, items, warnings):
        """The scope's settings file: hook and status line, planned in memory (limits/nudge plan_* are pure)."""
        s_before = read_bytes(scope["settings"])
        cur = _json_obj(s_before, self._short(scope["settings"]))
        steps = self._settings_steps(scope, want, cur)
        if want.get("statusline") and scope["kind"] == "project" and not statusline_installed(cur):
            over = statusline_shadowed_by(self._superiors(scope))
            if over:
                warnings.append({"code": "statusline_shadows_superior", "superior": over})
        self._plan_project_hooks(scope, want, want.get("nudge_hook", hook_installed(cur)), items, warnings)
        if not steps:
            return
        s_after, chain_after = _run_plans(s_before, steps, self._short(scope["settings"]))
        chain_before = read_bytes(scope["chain"])
        if s_after != s_before:
            items.append({"path": scope["settings"], "role": "settings", "before": s_before, "after": s_after,
                          "default_mode": paths.SETTINGS_MODE})
        if chain_after is not None and chain_after != chain_before:
            items.append({"path": scope["chain"], "role": "chain", "before": chain_before, "after": chain_after,
                          "default_mode": 0o600})

    def _settings_steps(self, scope, want, cur):
        """The settings plans (see _run_plans) for the hook and the status line the request names."""
        steps = []
        # asking for an installed practice still runs its plan: one with the old command is updated in place
        if "nudge_hook" in want and (want["nudge_hook"] or hook_installed(cur)):
            on = want["nudge_hook"]
            if on and not hook_installed(cur) and scope["kind"] == "project":
                self._refuse_duplicate_hook(scope)
            steps.append(lambda d: (nudge.plan_install(d, self.kit_bin) if on else nudge.plan_uninstall(d), None))
        if "statusline" in want and (want["statusline"] or statusline_installed(cur)):
            chain = scope["chain"]
            if want["statusline"]:
                steps.append(lambda d: limits.plan_install(d, chain, self.kit_bin) or (None, None))
            else:  # restore from the chain file the installed command names, as limits.uninstall does
                steps.append(lambda d: (limits.plan_uninstall(d, limits.read_chain(limits.installed_chain(d) or chain)), None))
        return steps

    def _refuse_duplicate_hook(self, scope):
        dup = hook_duplicated_in(self._superiors(scope))
        if dup:
            raise SettingsError(422, "the hook is already active in %s, which Claude Code also loads for this project: "
                                     "installing it here would run it twice" % dup, reason="hook_duplicate_superior",
                                superior=dup)

    def _plan_project_hooks(self, scope, want, hook_after, items, warnings):
        """Global or company scope that will have the hook: the projects under it with their own copy would run it
        twice. Removed when the request says so, else a warning."""
        if scope["kind"] == "project" or not hook_after:
            return
        others = self.projects_with_hook(scope)
        if others and want.get("remove_project_hooks"):
            for pj in others:
                pb = read_bytes(pj["settings"])
                pa, _c = _run_plans(pb, [lambda d: (nudge.plan_uninstall(d), None)], self._short(pj["settings"]))
                if pa != pb:
                    items.append({"path": pj["settings"], "role": "settings", "before": pb, "after": pa,
                                  "default_mode": paths.SETTINGS_MODE})
        elif others:
            warnings.append({"code": "hook_in_projects", "projects": [p["label"] for p in others]})

    def _plan_cut_rule(self, scope, lang, want, items):
        """The cut-rule block in CLAUDE.md (a project: CLAUDE.local.md and/or CLAUDE.md). -> where it will be."""
        files = {"local": scope["claude_md"]}
        if scope["kind"] == "project":
            files["shared"] = scope["claude_md_shared"]
        raws = {k: read_bytes(pth) for k, pth in files.items()}
        texts = {k: read_text(pth, self._short(pth)) for k, pth in files.items()}
        present = {k: has_cut_rule(t) for k, t in texts.items()}
        desired = _cut_rule_targets(want, {k for k, on in present.items() if on})
        if "cut_rule" in want:
            for k, pth in files.items():
                if (k in desired) != present[k]:
                    new = add_cut_rule(texts[k], lang) if k in desired else remove_cut_rule(texts[k])
                    items.append({"path": pth, "role": "claude_md", "before": raws[k], "after": new.encode("utf-8"),
                                  "default_mode": 0o644})
        return desired

    def _plan_gitignore(self, scope, want, desired, items):
        """CLAUDE.local.md added to the project's .gitignore, when asked and git does not ignore it yet."""
        if want.get("gitignore") and "local" in desired and git_ignore_state(scope["dir"]) == "not_ignored":
            read_text(scope["gitignore"], self._short(scope["gitignore"]))  # refuses a file that is not UTF-8
            gi = read_bytes(scope["gitignore"])
            items.append({"path": scope["gitignore"], "role": "gitignore", "before": gi, "after": add_gitignore_line(gi),
                          "default_mode": 0o644})

    def _plan_alerts(self, req, items):
        """alerts.json with the panel's values merged key by key. -> (config after, whether it changes)."""
        a_raw, a_cur = self._alerts_raw()
        if "alerts" not in req:
            return a_cur, False
        eff = config.load_alerts()
        eff["nudge"] = dict(config.NUDGE_DEFAULTS, **(eff.get("nudge") or {}))
        eff["review"] = {k: v for k, v in review.load_config(eff).items()}
        new_cfg = _merge_alerts(a_cur, validate_alerts(req["alerts"], eff))
        if new_cfg != a_cur:
            items.append({"path": config.alerts_path(), "role": "alerts", "before": a_raw,
                          "after": _dump(new_cfg), "default_mode": 0o600})
        return new_cfg, new_cfg != a_cur

    def _plan_launchagent(self, items, warnings, plan_agent, agent_on, touched_schedule, new_cfg):
        # a LaunchAgent installed under the old name (daf-claude-kit) is what an uninstall removes, and an install retires it
        label, installed_plist = scheduler.current_plist("review", self.agents_dir)
        legacy_plist = installed_plist if label != review.LABEL and os.path.exists(installed_plist) else None
        if plan_agent is False and agent_on:
            items.append({"path": installed_plist, "role": "plist", "before": read_bytes(installed_plist),
                          "after": None, "default_mode": 0o644, "agent": "uninstall"})
        elif plan_agent or (agent_on and touched_schedule and plan_agent is None):
            cfg = review.load_config(dict(config.DEFAULT_ALERTS, **new_cfg))
            log = os.path.join(paths.data_dir(), "logs", "review.log")
            after = review.plist(cfg, self.kit_bin, log)
            before = read_bytes(review.plist_path(self.agents_dir))
            if after != before:
                items.append({"path": review.plist_path(self.agents_dir), "role": "plist", "before": before, "after": after,
                              "default_mode": 0o644, "agent": "install", "cfg": cfg, "kit_bin": self.kit_bin, "legacy": legacy_plist})
                if legacy_plist:
                    warnings.append({"code": "legacy_agent_retired", "path": self._short(legacy_plist)})

    def _plan_schedule(self, items, plan_agent, agent_on, touched_schedule, new_cfg):
        """The review job on systemd, cron or Task Scheduler: before/after is its schedule key, not a file."""
        be = scheduler.backend()
        if plan_agent is False and agent_on:
            job = review.job(review.load_config(dict(config.DEFAULT_ALERTS, **new_cfg)))
            after, cfg = None, None
        elif plan_agent or (agent_on and touched_schedule and plan_agent is None):
            if not be:
                raise SettingsError(422, "no scheduler available on this machine for the daily review")
            cfg = review.load_config(dict(config.DEFAULT_ALERTS, **new_cfg))
            job = review.job(cfg)
            after = job.key()
        else:
            return
        st = scheduler.status(job, unit_dir=self.agents_dir, be=be)
        before = st["key"] if st["installed"] else None
        if before == after:
            return
        items.append({"path": "%s:%s" % (be, job.unit), "role": "schedule", "default_mode": 0o644,
                      "display": "%s · %s" % (scheduler.LABELS[be], job.unit), "be": be, "job": job, "cfg": cfg,
                      "before": None if before is None else ("schedule: %s\n" % before).encode(),
                      "after": None if after is None else ("schedule: %s\n" % after).encode(),
                      "after_key": after})

    # -- public
    def preview(self, req):
        scope, items, warnings = self._plan({k: v for k, v in req.items() if k != "files"})
        files = []
        for it in items:
            shown = it.get("display") or self._short(it["path"])
            files.append({"path": it["path"], "display": shown, "role": it["role"],
                          "exists": it["before"] is not None, "before": sha(it["before"]), "after": sha(it["after"]),
                          "diff": _redact(_diff(shown, it["before"], it["after"]))})
        return {"scope": scope["id"], "files": files, "warnings": warnings}

    def apply(self, req):
        given = req.get("files")
        if not isinstance(given, list) or not all(isinstance(f, dict) and {"path", "before", "after"} <= set(f) for f in given):
            raise SettingsError(400, "files (from the preview) are required")
        with LOCK:
            _scope, items, _w = self._plan({k: v for k, v in req.items() if k != "files"})
            if not items:
                raise SettingsError(409, "nothing to apply (the files already match, or changed since the preview)")
            by_path = {f["path"]: f for f in given if isinstance(f.get("path"), str)}
            if set(by_path) != {it["path"] for it in items} or len(by_path) != len(given):
                raise SettingsError(409, "the files changed since the preview; preview again")
            for it in items:
                f = by_path[it["path"]]
                if f["before"] != sha(it["before"]) or f["after"] != sha(it["after"]):
                    raise SettingsError(409, "%s changed since the preview; preview again" % self._short(it["path"]))
            applied = []
            for it in items:
                try:
                    backup = self._write(it)
                except SettingsError as e:
                    e.extra.setdefault("applied", applied)
                    raise
                except Exception as e:
                    raise SettingsError(500, "failed writing %s: %s" % (self._short(it["path"]), e), applied=applied) from e
                applied.append({"path": it["path"], "display": it.get("display") or self._short(it["path"]),
                                "backup": self._short(backup) if backup else None})
            return {"applied": applied}

    def _write(self, it):
        if it["role"] == "schedule":
            return self._write_schedule(it)
        if it["role"] != "plist":
            return write_atomic(it["path"], it["after"], it["default_mode"])
        backup = _backup(it["path"]) if os.path.exists(it["path"]) else None
        if it.get("legacy") and os.path.exists(it["legacy"]):
            _backup(it["legacy"])  # review.install removes it
        if it.get("agent") == "uninstall":
            review.uninstall(agents_dir=self.agents_dir, load=self.load_agent)
        else:
            review.install(cfg=it["cfg"], kit_bin=it["kit_bin"], agents_dir=self.agents_dir, load=self.load_agent)
            if read_bytes(it["path"]) != it["after"]:
                raise SettingsError(500, "the LaunchAgent written differs from the preview")
        return backup

    def _write_schedule(self, it):
        if it["after_key"] is None:
            review.uninstall(agents_dir=self.agents_dir, load=self.load_agent)
        else:
            review.install(cfg=it["cfg"], kit_bin=self.kit_bin, agents_dir=self.agents_dir, load=self.load_agent)
        if self.load_agent or it["be"] != "cron":  # unloaded, cron has nothing written to read back
            st = scheduler.status(it["job"], unit_dir=self.agents_dir, be=it["be"])
            if (st["key"] if st["installed"] else None) != it["after_key"]:
                raise SettingsError(500, "the scheduled review written differs from the preview")
