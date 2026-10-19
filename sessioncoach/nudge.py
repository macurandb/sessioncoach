"""Habit nudge: warn before a prompt re-reads a large context whose cache already expired, or hold that prompt back.

`sessioncoach hook prompt` runs as a Claude Code UserPromptSubmit hook. It reads the tail of the session
transcript, takes the last turn's time and context size, and if the pause was longer than the
cache TTL of that session (1 h, or 5 min when the transcript shows 5-minute cache writes) and the
context is large it prints a `systemMessage`: shown to the person, never added to the model's context, never
blocking the prompt. From the transcript it reads only timestamps and token counts. Only when it is about to warn, it
looks at the words of the prompt just typed to answer in the same language (es/en), in memory; then Claude Code's
`language` setting decides. When it warns, it then keeps the warning's numbers (pause, context size, TTL, session and
project dir ids) for the dashboard's warnings view; the prompt's text is never stored. It never fails the prompt:
any error prints nothing.

Idle guard (opt-in: alerts.json → nudge.block, `sessioncoach idle-guard-on`): instead of the warning it prints
`{"decision": "block"}`, so that first prompt is not sent and costs nothing, and records the pause in idleguard.py
(session id, the last turn's time, transcript path, working dir; no text). Sending again in the same pause goes
through with no second warning; /clear instead gets a handoff built from this transcript (resume.py, digest.py).
`/handoff`, `/clear` and `/compact` are never held back. When the pause cannot be recorded it falls back to the
warning, so a prompt is never blocked twice for the same pause. When Claude Code prepared its own idle summary
(idlecompact.py, the PreCompact hook installed with the idle guard), the return does not re-read the context: in both
modes it only says so.
"""
import datetime as dt
import functools
import json
import os
import sys

from . import config, handoff, hooks, idleguard, paths, resume

HOOK_EVENT = hooks.PROMPT.event
TAIL_BYTES = 512 * 1024
# idle_min here is only the value the settings panel shows. Unless the user sets nudge.idle_min in
# alerts.json, the threshold is the real cache TTL of the session (see check()).
DEFAULTS = config.NUDGE_DEFAULTS
DEFAULT_TTL_S = 3600  # when the transcript does not say which TTL it used
# what the hook gives the user, said by `sessioncoach setup` before asking (the technical side: `sessioncoach help hook-install`)
VALUE = ("Coming back to a big session after its cache expired writes the whole context again, at the price of a cache write. "
         "The warning shows before that prompt is answered, while you can still choose /compact or a fresh session.")
VALUE_FIGURE = "In the last 7 days that happened %(resumes)d times, ≈ $%(resume_usd).2f (API-equivalent at list price)."

# The steps are literal so they can be copied as they are; {date} names today's handoff file.
MSG = {
    "es": "sessioncoach · {m} min sin actividad: la caché caducó y este mensaje releerá ~{k}k tokens de contexto.\n"
          "Si cambias de tarea o terminaste una fase, elige una, en ESTA sesión (la única que tiene el contexto):\n"
          "  · seguir aquí con menos contexto: /compact conserva las decisiones, el estado y los próximos pasos\n"
          "  · sesión nueva con traspaso: 1) aquí, pide «escribe un traspaso en docs/HANDOFF-{date}.md»; "
          "2) cuando lo escriba, /clear; 3) escribe «lee docs/HANDOFF-{date}.md y continúa»",
    "en": "sessioncoach · {m} min idle: the cache expired and this prompt will re-read ~{k}k tokens of context.\n"
          "If you are switching tasks or a phase is done, pick one, in THIS session (the only one with the context):\n"
          "  · stay here with less context: /compact keep the decisions, the current state and the next steps\n"
          "  · new session with a handoff: 1) here, ask \"write a handoff to docs/HANDOFF-{date}.md\"; "
          "2) once it is written, /clear; 3) type \"read docs/HANDOFF-{date}.md and continue\"",
}

# the same, when /handoff is installed: one command instead of the three steps
MSG_HANDOFF = {
    "es": MSG["es"].split("  · sesión nueva")[0] + "  · sesión nueva con traspaso: escribe /handoff (deja el traspaso en docs/ "
          "y te dice cuándo hacer /clear)",
    "en": MSG["en"].split("  · new session")[0] + "  · new session with a handoff: type /handoff (it writes the handoff to docs/ "
          "and tells you when to /clear)",
}


# what the idle guard gives the user, said by `sessioncoach setup` before asking (off unless the user turns it on)
BLOCK_VALUE = ("Hold back the first message after a long pause, so it costs nothing until you choose: send it again to "
               "go on (it re-reads the context once), or /clear to start clean with an automatic handoff of the session.")

# the idle guard's reason, shown when it holds a prompt back; {clear} and {handoff} are the lines below
BLOCK_MSG = {
    "es": "sessioncoach · {m} min sin actividad: la caché caducó y Claude Code no preparó un resumen mientras no estabas "
          "(equipo en reposo, contexto < 200k o la app se reinició). Este mensaje se retuvo y NO se envió: no se gastó nada. "
          "Tu texto está abajo, en «Original prompt:» (en la terminal vuelve también al cuadro de entrada).\n"
          "Elige una:\n"
          "  · seguir aquí: envíalo otra vez (releerá ~{k}k tokens una vez)\n"
          "{clear}{handoff}",
    "en": "sessioncoach · {m} min idle: the cache expired and Claude Code did not prepare a summary while you were away "
          "(Mac asleep, context < 200k or the app restarted). This message was held back and NOT sent: nothing spent. "
          "Your text is below, under \"Original prompt:\" (in the terminal it also goes back to the input box).\n"
          "Pick one:\n"
          "  · go on here: send it again (it re-reads ~{k}k tokens once)\n"
          "{clear}{handoff}",
}
# when Claude Code prepared its idle summary: the return does not re-read the context, in either mode
SUMMARY_MSG = {
    "es": "sessioncoach · {m} min sin actividad: Claude Code aplicará el resumen que preparó mientras no estabas "
          "(no relee todo el contexto).",
    "en": "sessioncoach · {m} min idle: Claude Code will apply the summary it prepared while you were away "
          "(it does not re-read the whole context).",
}
BLOCK_CLEAR = {  # True: the resume hook is active, so /clear gets the automatic handoff
    ("es", True): "  · empezar limpio gratis: /clear — la sesión nueva recibe un traspaso automático hecho con esta sesión "
                  "(sin coste de modelo)",
    ("es", False): "  · empezar limpio: /clear (sin traspaso: con sessioncoach resume-hook-install la sesión nueva recibiría "
                   "uno automático)",
    ("en", True): "  · start clean for free: /clear — the new session gets an automatic handoff built from this one "
                  "(no model cost)",
    ("en", False): "  · start clean: /clear (no handoff: with sessioncoach resume-hook-install the new session would get an "
                   "automatic one)",
}
BLOCK_HANDOFF = {"es": "\n  · mejor traspaso: /handoff (relee el contexto una vez)",
                 "en": "\n  · better handoff: /handoff (re-reads the context once)"}


# the language of each message is decided in config; these names are kept for older callers
_WORDS, _NOISE = config._WORDS, config._NOISE
prompt_lang, _lang_of, settings_lang, message_lang = config.prompt_lang, config.lang_of, config.settings_lang, config.message_lang


def last_turn_info(transcript_path):
    """(timestamp, context tokens, cache TTL in seconds or None) of the last assistant turn, from the file's tail.

    The TTL is that of the most recent turn that wrote cache (usage.cache_creation: ephemeral_1h
    or ephemeral_5m); turns that only read cache keep the TTL in force. None when the tail says nothing.
    A compaction (/compact or automatic) newer than the last turn replaced the context: its time and the
    tokens left after it (0 when not recorded) are returned instead.
    """
    with open(transcript_path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    ts, ctx, _ttl = None, 0, None
    for line in reversed(lines):
        if '"usage"' not in line and '"compact_boundary"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("subtype") == "compact_boundary":
            if ts is None and d.get("timestamp"):
                cm = d.get("compactMetadata") if isinstance(d.get("compactMetadata"), dict) else {}
                return d["timestamp"], cm.get("postTokens") or 0, None
            continue
        m = d.get("message") if isinstance(d.get("message"), dict) else {}
        u = m.get("usage")
        if not u or m.get("model") == "<synthetic>" or not d.get("timestamp"):
            continue
        if ts is None:
            ts = d["timestamp"]
            ctx = sum((u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        cc = u.get("cache_creation") or {}
        if (cc.get("ephemeral_1h_input_tokens") or 0) > 0:
            return ts, ctx, 3600
        if (cc.get("ephemeral_5m_input_tokens") or 0) > 0:
            return ts, ctx, 300
        if not cc and (u.get("cache_creation_input_tokens") or 0) > 0:  # no breakdown: older format
            return ts, ctx, None
    return ts, ctx, None

def check(data, cfg=None, now=None):
    """The message to show, or None.

    The pause that counts is the cache TTL of the session; `idle_min` in cfg (alerts.json → nudge)
    overrides it when the user set it.
    """
    return evaluate(data, cfg, now)[0]

def evaluate(data, cfg=None, now=None):
    """-> (message or None, numbers behind it or None): what check() decides and why."""
    user = dict(cfg or {})
    cfg = dict(DEFAULTS, **user)
    path = data.get("transcript_path")
    if not path or not os.path.exists(path):
        return None, None
    ts, ctx, ttl = last_turn_info(path)
    if not ts:
        return None, None
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    now = now or dt.datetime.now(dt.timezone.utc)
    idle_min = (now - t).total_seconds() / 60
    limit_min = user["idle_min"] if user.get("idle_min") else (ttl or DEFAULT_TTL_S) / 60
    if idle_min < limit_min or ctx < cfg["min_context_tokens"]:
        return None, None
    info = {"last_ts": ts, "idle_s": int(idle_min * 60), "context": ctx, "ttl_s": ttl, "limit_s": int(limit_min * 60)}
    if idle_summary_pending(data, ts):
        return SUMMARY_MSG[message_lang(data, cfg)].format(m=int(idle_min)), dict(info, idle_summary=1)
    msgs = MSG_HANDOFF if handoff.available(data.get("cwd")) else MSG
    return msgs[message_lang(data, cfg)].format(m=int(idle_min), k=int(ctx / 1000), date=now.astimezone().date().isoformat()), info

WAYS_OUT = ("/handoff", "/clear", "/compact", "/keep-context")  # never held back, in case Claude Code passes them to this hook


def idle_summary_pending(data, last_ts=None):
    """True when Claude Code already has a summary of this session ready to use on return (its own idle compaction),
    so the context is not re-read and there is nothing to warn about or hold back. Only the PreCompact hook can tell
    (idlecompact.py); without it, always False."""
    from . import idlecompact
    return idlecompact.pending(data.get("session_id"), last_ts)


def block_message(data, info, cfg, now=None):
    """The idle guard's reason for holding a prompt back (shown to the person, never to the model)."""
    lang = message_lang(data, cfg)
    cwd = data.get("cwd")
    return BLOCK_MSG[lang].format(m=info["idle_s"] // 60, k=int(info["context"] / 1000),
                                  clear=BLOCK_CLEAR[(lang, resume.active_for(cwd))],
                                  handoff=BLOCK_HANDOFF[lang] if handoff.available(cwd) else "")


def guard(data, text, info, cfg, now=None):
    """Idle guard: -> (output to print or None, numbers to log). The first prompt of a pause is held back and the pause
    recorded; the next one in the same pause goes through, silent. /handoff, /clear and /compact always go through and
    leave the record alone (they are the ways out; /clear must still find it). When the pause cannot be recorded (no
    session id, data dir not writable), the plain warning instead."""
    sid = data.get("session_id")
    if str(data.get("prompt") or "").lstrip().startswith(WAYS_OUT):
        return None, info
    if not sid:
        return {"systemMessage": text}, info
    if idleguard.find(sid, info["last_ts"]):
        idleguard.let_through(sid, info["last_ts"])
        return None, info
    try:
        idleguard.hold(data, info["last_ts"])
    except Exception:  # unrecorded, a block would come back on every resend
        return {"systemMessage": text}, info
    return {"decision": "block", "reason": block_message(data, info, cfg, now)}, dict(info, blocked=1)


BLOCK_MIN = 60  # 5-min caches expire on every coffee break: holding those back would get in the way, warn only


def holds(cfg, info, data=None):
    """Whether the idle guard holds this prompt back: it is on, the pause is at least nudge.block_min (default
    BLOCK_MIN) minutes, whatever the cache TTL (shorter pauses past a 5-min TTL still get the warning), and the session
    was not marked with /keep-context (that return pays its re-read knowingly)."""
    from . import keepctx
    limit = cfg.get("block_min")
    limit = limit if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0 else BLOCK_MIN
    return (cfg.get("block") is True and info["idle_s"] >= limit * 60
            and not keepctx.kept((data or {}).get("session_id")))


def hook_main():
    try:
        paths.utf8_stdio()
        from .config import load_alerts
        alerts = load_alerts()
        cfg = dict(alerts.get("nudge") or {}, lang=alerts.get("lang"))
        data = json.loads(sys.stdin.read() or "{}")
        text, info = evaluate(data, cfg)
        if text and info.get("idle_summary"):  # not a warning: shown, never held back or kept
            print(json.dumps({"systemMessage": text}, ensure_ascii=False))
            sys.stdout.flush()
        elif text:
            out = guard(data, text, info, cfg) if holds(cfg, info, data) else ({"systemMessage": text}, info)
            if out[0]:
                print(json.dumps(out[0], ensure_ascii=False))
                sys.stdout.flush()
                _log(data, out[1])
    except Exception:  # never get in the way of a prompt
        pass


def _log(data, info):
    """Keep the warning and its numbers for the dashboard's warnings view (after it was printed).

    A plain connection with a 1 s timeout: no schema, no migrations and no new database in the prompt's path. When
    the database or its alert_events table does not exist yet (the next ingest creates them), nothing is kept."""
    import sqlite3

    from . import store
    db = paths.db_path()
    if not os.path.exists(db):
        return
    con = sqlite3.connect(db, timeout=1)
    try:
        pdir = os.path.basename(os.path.dirname(data.get("transcript_path") or ""))
        store.add_alert_event(con, "hook", info, session_id=data.get("session_id"), project_dir=pdir or None)
        con.commit()
    finally:
        con.close()


# ---- install: its own UserPromptSubmit entry (hooks.py) ----

SPEC = hooks.PROMPT
_command = SPEC.command
is_ours = SPEC.is_ours
_is_ours = is_ours  # the name it had before it was shared between modules
_load, _dump = hooks.load, hooks.dump
our_commands = functools.partial(hooks.our_commands, SPEC)
installed_in = functools.partial(hooks.installed_in, SPEC)
installed = functools.partial(hooks.installed, SPEC)
plan_install = functools.partial(hooks.plan_install, SPEC)
plan_uninstall = functools.partial(hooks.plan_uninstall, SPEC)
install = functools.partial(hooks.install, SPEC)
uninstall = functools.partial(hooks.uninstall, SPEC)
