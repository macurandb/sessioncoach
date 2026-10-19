"""alerts.json: where it lives, its defaults, reading it, writing it, and the language each message is shown in.

A leaf module: it imports only `paths`, so the hooks, the status line and the scheduled ingest can read their
configuration without loading the installer. The older homes keep their names as aliases (limits.load_alerts,
limits.DEFAULT_ALERTS, installer.update_alerts, installer.alerts_path, installer.ingest_interval, nudge.DEFAULTS,
review.DEFAULTS, expiry.DEFAULTS, settings.ALERT_KEYS...).

Language: one order for every message (`lang`), each step skipped when it gives neither es nor en:
  the prompt's own words (hooks answering a prompt) > SESSIONCOACH_LANG > alerts.json `lang` > Claude Code's
  `language` setting (the project's, when there is a cwd, then the user's) > LANG > en.
notice_lang (notifications, daily review), message_lang (hooks) and cut_rule_lang (the «when to cut» rule
written into CLAUDE.md) are that order with what each caller has.
"""
import math
import os
import re

from . import paths

LONG_CONTEXT_TOKENS = 300000  # R16, dashboard and daily review: a turn re-reading this much context is "long"
DEFAULT_ALERTS = {"five_hour": [75, 90], "seven_day": [75, 90], "context": [85], "context_tokens": [300000, 500000],
                  "projection": True,
                  "notify": True, "budgets": []}
NUDGE_DEFAULTS = {"idle_min": 60, "min_context_tokens": 100000}  # alerts.json → nudge
REVIEW_DEFAULTS = {"enabled": True, "time": "08:00", "days": ["mon", "tue", "wed", "thu", "fri"],
                   "channels": ["notification", "dashboard"], "lookback": "last_active", "max_tips": 1,
                   "long_context_tokens": LONG_CONTEXT_TOKENS, "max_sessions": 8, "email_command": None}  # alerts.json → review
EXPIRY_DEFAULTS = {"enabled": True, "min_context_tokens": 200000, "min_idle_min": 10, "min_remaining_min": 3,
                   "slack_min": 5}  # alerts.json → cache_expiry
DEFAULT_INTERVAL_MIN = 30  # alerts.json → ingest.interval_min
CUT_RULE_LANG = "en"  # the cut rule's language when nothing names one (lang() never returns another)
THRESHOLD_LISTS = ("five_hour", "seven_day", "context", "context_tokens")  # alert thresholds: lists of positive numbers
SECTIONS = ("ingest", "nudge", "review", "cache_expiry")  # blocks that must be objects

# keys the settings panel may change (settings.validate_alerts); any other key is left as the user wrote it
ALERT_KEYS = {"five_hour", "seven_day", "context", "context_tokens", "projection", "notify", "lang", "nudge", "review"}
NUDGE_KEYS = {"idle_min", "min_context_tokens"}
REVIEW_KEYS = {"enabled", "time", "days", "channels", "lookback", "max_tips", "long_context_tokens"}


# ---- alerts.json ----

def alerts_path():
    return os.path.join(paths.data_dir(), "alerts.json")


def positive(v):
    """True for a finite number above 0 (not a bool): what a threshold must be."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def load_alerts():
    """alerts.json over DEFAULT_ALERTS, top-level key by key; a missing or corrupt file gives the defaults.

    Thresholds are checked here, never rewritten in the file: a threshold list holding anything but positive numbers
    (text, 0, a bare number) gets its default, since it would break the status line; [] or another empty value
    (null, false, 0) stays, as it turns that alert off. nudge.idle_min / nudge.min_context_tokens that are not
    positive numbers are dropped: the hook then uses the cache TTL / NUDGE_DEFAULTS, as when they are not set.
    Budgets: see valid_budgets."""
    cfg = dict(DEFAULT_ALERTS)
    cfg.update(paths.load_json_object(alerts_path()))
    for k in SECTIONS:  # a block that is not an object (e.g. "ingest": 3) counts as not set
        if k in cfg and not isinstance(cfg[k], dict):
            del cfg[k]
    cfg["budgets"] = valid_budgets(cfg.get("budgets"))
    for k in THRESHOLD_LISTS:
        if cfg[k] and not (isinstance(cfg[k], list) and all(positive(x) for x in cfg[k])):
            cfg[k] = list(DEFAULT_ALERTS[k])
    nd = cfg.get("nudge")
    if isinstance(nd, dict) and any(k in nd and not positive(nd[k]) for k in NUDGE_KEYS):
        cfg["nudge"] = {k: v for k, v in nd.items() if k not in NUDGE_KEYS or positive(v)}
    return cfg


def valid_budgets(budgets):
    """alerts.json → budgets without what broke limits.evaluate_budgets (and with it the scheduled ingest).

    A budget that is not an object is dropped. An equiv_usd that is not a finite number of 0 or more is dropped
    (the budget then reports 0 % and never alerts); "800" stays, as float() always read it. thresholds that
    are not a list of positive numbers are dropped: the default [80, 100] applies. Other fields stay as written."""
    if not isinstance(budgets, list):
        return []
    out = []
    for raw in budgets:
        if not isinstance(raw, dict):
            continue
        b = dict(raw)  # a copy: what the file says is never rewritten
        if "equiv_usd" in b and b["equiv_usd"] is not None:
            try:
                usd = float(b["equiv_usd"])
            except (TypeError, ValueError):
                usd = -1.0
            if not (math.isfinite(usd) and usd >= 0):
                del b["equiv_usd"]
        th = b.get("thresholds")
        if th and not (isinstance(th, list) and all(positive(x) for x in th)):
            del b["thresholds"]
        out.append(b)
    return out


def update_alerts(mutate):
    """Apply mutate(dict) to alerts.json with a backup first; keeps mode 600. Refuses an unreadable file."""
    import json
    path = alerts_path()
    cfg = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh) or {}
        paths.backup_file(path, ".bak-setup")
    mutate(cfg)
    paths.ensure_data_dir()
    # UTF-8 bytes, not the locale's code page (cp1252 on Windows), through a unique tmp file
    paths.write_atomic(path, (json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                       default_mode=0o600, backup=False)
    if not paths.is_windows():
        os.chmod(path, 0o600)  # it may hold an email command: private even if it was created looser by hand
    return path


def ingest_interval(alerts=None):
    """Minutes between scheduled ingests (alerts.json → ingest.interval_min), within 1..1440."""
    alerts = load_alerts() if alerts is None else alerts
    try:
        n = int((alerts.get("ingest") or {}).get("interval_min", DEFAULT_INTERVAL_MIN))
    except (TypeError, ValueError):
        n = DEFAULT_INTERVAL_MIN
    return min(max(n, 1), 1440)


def review_config(alerts=None):
    """alerts.json → review, merged over REVIEW_DEFAULTS key by key (a partial block keeps the rest), plus `lang`.
    A long_context_tokens that is not a positive number gets its default (text broke the tips, 0 divided by zero)."""
    alerts = load_alerts() if alerts is None else alerts
    cfg = dict(REVIEW_DEFAULTS)
    cfg.update(alerts.get("review") or {})
    for k in ("long_context_tokens", "max_sessions"):
        if not positive(cfg[k]):
            cfg[k] = REVIEW_DEFAULTS[k]
    m = cfg["max_tips"]
    if not (isinstance(m, int) and not isinstance(m, bool) and m >= 0):  # 0 is valid: no tips
        cfg["max_tips"] = REVIEW_DEFAULTS["max_tips"]
    cfg["lang"] = alerts.get("lang")
    return cfg


# ---- language ----

# frequent words that are not words of the other language; enough to tell es from en in one prompt
_WORDS = {
    "es": frozenset({
        "el", "la", "los", "las", "del", "que", "de", "y", "en", "por", "para", "con", "una", "uno", "es", "son",
        "está", "esta", "este", "esto", "pero", "como", "cuando", "porque", "muy", "más", "sin", "sobre", "también",
        "hay", "donde", "qué", "cómo", "puedes", "haz", "quiero", "me", "mi", "tu", "se", "lo", "le", "ya", "si",
        "bien", "hacer", "favor", "ahora", "todo", "nada", "algo"
    }),
    "en": frozenset({
        "the", "and", "of", "to", "in", "for", "with", "is", "are", "was", "this", "that", "it", "its", "be", "can",
        "could", "would", "should", "do", "does", "what", "how", "please", "want", "my", "your", "we", "you", "not",
        "but", "if", "just", "now", "all", "any", "make", "use", "from", "have"
    }),
}
_NOISE = re.compile(r"```.*?```|`[^`]*`|https?://\S+|\S*[/\\]\S*", re.S)  # code, urls, paths


def lang(cfg=None, data=None):
    """'es' | 'en' for a message: the prompt in `data`, SESSIONCOACH_LANG, `cfg["lang"]` (alerts.json), Claude
    Code's `language` setting (data's cwd first), LANG, en; the first that names es or en wins."""
    cfg, data = cfg or {}, data or {}
    steps = (lambda: prompt_lang(data.get("prompt")), lambda: lang_of(paths.env("LANG")),
             lambda: lang_of(cfg.get("lang")), lambda: settings_lang(data.get("cwd")),
             lambda: lang_of(os.environ.get("LANG")))
    return next((v for v in (step() for step in steps) if v), "en")


def notice_lang(cfg):
    """Language of a notification or the daily review (no prompt): `lang(cfg)`."""
    return lang(cfg)


def prompt_lang(text):
    """'es' | 'en' from the words of a prompt, or None when it is short or mixed."""
    text = _NOISE.sub(" ", text or "").lower()
    words = re.findall(r"[a-záéíóúüñ]+", text)
    score = {k: sum(w in v for w in words) for k, v in _WORDS.items()}
    score["es"] += len(re.findall(r"[áéíóúñ¿¡]", text))
    for a, b in (("es", "en"), ("en", "es")):
        if score[a] >= 2 and score[a] >= 2 * score[b]:
            return a
    return None


def lang_of(value):
    """'es' | 'en' from a language name or locale (es_ES, Spanish, english...), None for any other."""
    v = str(value or "").strip().lower()
    if v.startswith(("es", "spa")):
        return "es"
    if v.startswith(("en", "ing")):
        return "en"
    return None


def settings_lang(cwd=None):
    """Claude Code's `language` setting as 'es'/'en': the project's (settings.local.json, then settings.json)
    over the user's; None when unset or another language."""
    files = []
    if cwd:
        files += [os.path.join(cwd, ".claude", n) for n in ("settings.local.json", "settings.json")]
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(paths.HOME, ".claude")
    files.append(os.path.join(cfg, "settings.json"))
    for f in files:
        v = paths.load_json_object(f).get("language")
        if v:
            return lang_of(v)
    return None


def message_lang(data, cfg):
    """Language of a hook's message: `lang(cfg, data)` (the hook input carries the prompt and the cwd)."""
    return lang(cfg, data)


def cut_rule_lang(alerts=None):
    """Language the cut rule is written in: `lang(alerts)` (alerts.json read when not given)."""
    return lang(load_alerts() if alerts is None else alerts)
