"""Read Claude Code transcripts and keep only numbers and ids.

Claude Code deletes local transcripts after `cleanupPeriodDays` (30 by default), so the
collector is meant to run daily: every run only adds what is new, and data already
captured survives after its transcript is gone.
"""
import datetime as dt
import glob
import json
import math
import os
import re
import sqlite3
import sys

from . import accounts, paths, store

USAGE_RX = re.compile(
    r"<task-id>([^<]+)</task-id>.*?<usage>.*?<subagent_tokens>(\d+)</subagent_tokens>"
    r".*?<tool_uses>(\d+)</tool_uses>.*?<duration_ms>(\d+)</duration_ms>", re.S)
ID_RX = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")  # agent ids: anything else is text, never stored
TYPE_RX = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")  # subagent_type is chosen by the model: names only ("plugin:agent")
TOOL_RX = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")  # tool names: Read, mcp__server__tool
MODEL_RX = re.compile(r"[A-Za-z0-9_.:@/\[\]-]{1,100}\Z")  # claude-opus-5-5[1m], Bedrock / Vertex ids
NOTE_TAG = "<task-notification>"
COST_FIELDS = ("inputTokens", "outputTokens", "thinkingTokens", "cacheReadInputTokens",
               "cacheCreationInputTokens", "costUSD", "webSearchRequests")
# Bump when scan() starts extracting something new: files read by an older scan are then read again.
SCAN_VERSION = 2  # 2: compactions
COMMIT_EVERY = 25  # files between commits: a long ingest must not hold the write lock until the end
_INTERESTING = ('"usage"', '"tool_use"', '"cost-state"', '"timestamp"', "subagent_tokens", "toolUseResult")


def transcripts(roots):
    """Yield (projects_root, project_dir, path, kind, session_id) for main and subagent transcripts."""
    for projects_arg in roots:
        projects = os.path.normpath(os.path.expanduser(projects_arg))
        for root in sorted(glob.glob(os.path.join(projects, "*"))):
            pdir = os.path.basename(root)
            for f in glob.glob(os.path.join(root, "*.jsonl")):
                yield projects, pdir, f, "main", os.path.splitext(os.path.basename(f))[0]
            for f in glob.glob(os.path.join(root, "*", "subagents", "*.jsonl")):
                yield projects, pdir, f, "subagent", os.path.basename(os.path.dirname(os.path.dirname(f)))


def scan(pdir, path, kind, session):
    """Yield ('req'|'cost'|'sub', record) from one transcript."""
    seen, last_ts = set(), None
    types, meta, totals, dirs = {}, {}, {}, {}
    fname = os.path.basename(path)
    with open(path, errors="replace", encoding="utf-8") as fh:
        for line in fh:
            if not any(k in line for k in _INTERESTING):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):  # a list or a bare value: no record
                continue
            ts = d.get("timestamp") if isinstance(d.get("timestamp"), str) else None  # a number is no time
            if ts:
                last_ts = ts
                if kind == "main" and isinstance(d.get("cwd"), str) and d["cwd"]:
                    dirs.setdefault(encode_dir(d["cwd"]), ts)
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            u = m.get("usage")
            msg_id = m.get("id") if isinstance(m.get("id"), str) else None
            if isinstance(u, dict) and u and m.get("model") != "<synthetic>":
                req_id = d.get("requestId") if isinstance(d.get("requestId"), str) else None
                key = (msg_id, req_id)
                if key not in seen:
                    seen.add(key)
                    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
                    yield "req", {
                        "project_dir": pdir, "file": fname, "session_id": session, "kind": kind,
                        "ts": ts, "model": _name(m.get("model"), MODEL_RX, "?"),  # an odd model still counts
                        # "" for a missing id: SQLite takes NULLs in a primary key as distinct, so a re-read
                        # file would add the request again
                        "msg_id": msg_id or "", "request_id": req_id or "",
                        "input": _count(u.get("input_tokens")), "output": _count(u.get("output_tokens")),
                        "cache_read": _count(u.get("cache_read_input_tokens")),
                        "cache_write": _count(u.get("cache_creation_input_tokens")),
                        "cache_write_5m": _count(cc.get("ephemeral_5m_input_tokens"), None),  # None: no 5m/1h split
                        "cache_write_1h": _count(cc.get("ephemeral_1h_input_tokens"), None),
                    }
            for b in m.get("content") if isinstance(m.get("content"), list) else ():
                if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str) and b["id"] \
                        and b.get("name"):
                    yield "tool", {"project_dir": pdir, "tool_use_id": b["id"], "session_id": session,
                                   "kind": kind, "ts": ts, "name": _name(b["name"], TOOL_RX, "?"), "msg_id": msg_id}
            if kind != "main":
                continue
            if d.get("subtype") == "compact_boundary" and ts:
                cm = d.get("compactMetadata") if isinstance(d.get("compactMetadata"), dict) else {}
                trigger = cm.get("trigger") if cm.get("trigger") in ("manual", "auto") else None  # never free text
                yield "compact", {"project_dir": pdir, "session_id": session, "ts": ts,
                                  "trigger": trigger, "pre_tokens": _count(cm.get("preTokens"), None),
                                  "post_tokens": _count(cm.get("postTokens"), None), "precomputed": 1 if cm.get("precomputed") else 0}
            total = d.get("totalCostUSD")
            if d.get("type") == "cost-state" and last_ts and (total is None or _number(total) is not None):
                usage = d.get("modelUsage") if isinstance(d.get("modelUsage"), dict) else {}
                mu = {model: {f: _number(v.get(f)) if isinstance(v, dict) else None for f in COST_FIELDS}
                      for model, v in usage.items() if MODEL_RX.match(model)}  # keys: no text
                yield "cost", {"project_dir": pdir, "session_id": session, "ts": last_ts,
                               "total_cost_usd": total or 0.0, "model_usage": mu}
            _subagent_events(d, m, ts, line, types, meta, totals)
    for aid, (tok, tools, dur, src) in totals.items():
        typ, model, ts = meta.get(aid, ("?", None, None))
        yield "sub", {"project_dir": pdir, "session_id": session, "agent_id": aid, "subagent_type": typ,
                      "model": model, "ts": ts, "tokens": tok, "tool_uses": tools, "duration_ms": dur,
                      "source": src}
    for dname, ts in dirs.items():
        yield "dir", {"project_dir": pdir, "session_id": session, "dir": dname, "ts": ts}


def _number(v):
    """`v` if it is a finite number (not a bool), else None."""
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    return v if ok and abs(v) < 2 ** 63 else None


def _count(v, default=0):
    """A token count or duration as an int ("15" and 1.5 too); text, a list, NaN or a size SQLite cannot hold
    is `default`."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return default
    try:
        n = v if isinstance(v, int) else int(float(v))
    except (ValueError, OverflowError):
        return default
    return n if abs(n) < 2 ** 63 else default


def _name(v, rx, odd):
    """`v` if it is a name `rx` accepts; None stays None; anything else (free text, too long) is `odd`."""
    if v is None:
        return None
    return v if isinstance(v, str) and rx.match(v) else odd


def encode_dir(cwd):
    """'/Users/me/code/app' -> '-Users-me-code-app', the way Claude Code names project dirs."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _subagent_events(d, m, ts, line, types, meta, totals):
    """Subagent totals as reported back to the parent (subagent transcripts under-count output)."""
    content = m.get("content")
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task"):
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                typ = inp.get("subagent_type") or "general-purpose"
                types[b.get("id") if isinstance(b.get("id"), str) else None] = typ if isinstance(typ, str) and TYPE_RX.match(typ) else "?"
    r = d.get("toolUseResult")
    if isinstance(r, dict) and isinstance(r.get("agentId"), str) and ID_RX.match(r["agentId"]):
        first = content[0] if isinstance(content, list) and content and isinstance(content[0], dict) else {}
        # an odd model is None, not "?": add_subagent fills in only a NULL model
        tid = first.get("tool_use_id") if isinstance(first.get("tool_use_id"), str) else None
        meta[r["agentId"]] = (types.get(tid, "?"), _name(r.get("resolvedModel"), MODEL_RX, None), ts)
        if r.get("totalTokens") is not None:
            totals[r["agentId"]] = (_count(r.get("totalTokens")), _count(r.get("totalToolUseCount")),
                                    _count(r.get("totalDurationMs")), "foreground")
    text = _notification(d, content) if "subagent_tokens" in line else None
    if text:
        for tid, tok_s, tools_s, dur_s in USAGE_RX.findall(text):
            if not ID_RX.match(tid):
                continue
            tok, tools, dur = _count(tok_s), _count(tools_s), _count(dur_s)
            if tid in totals:  # later notifications: keep max tokens/tools, first duration
                pt, pto, pd, src = totals[tid]
                totals[tid] = (max(pt, tok), max(pto, tools), pd, src)
            else:
                totals[tid] = (tok, tools, dur, "notification")


def _notification(d, content):
    """The text of a task-notification Claude Code delivered, or None. The same tags in a tool_result (a file read),
    in a prompt the user typed or pasted, or in a queued prompt are text: never parsed."""
    if d.get("type") == "user" and isinstance(content, str):
        origin = d.get("origin")
        if isinstance(origin, dict):
            return content if origin.get("kind") == "task-notification" else None
        return content if content.lstrip().startswith(NOTE_TAG) else None  # transcripts older than `origin`
    a = d.get("attachment")
    if d.get("type") == "attachment" and isinstance(a, dict) and a.get("type") == "queued_command" \
            and a.get("commandMode") == "task-notification" and isinstance(a.get("prompt"), str):
        return a["prompt"]  # a notification that arrived during a turn
    return None


def ingest(con, roots, full=False, stats=None):
    """Add new records from every transcript under `roots`. Returns counts added.

    Each record keeps the transcript root it came from; the account each root is logged into
    is recorded too (non-personal fields only), so cost can be split by Claude account.

    A transcript whose size and modification time are those of the last time it was read (by this
    version of scan) is skipped: its records are already stored. One that grew, shrank or was
    replaced is read again from the start, never from an offset, because scan() carries state across
    lines (cost-state times, which agent a result belongs to, duplicates). `full` reads everything.
    `stats`, if given, gets {"files", "scanned", "skipped"}.
    """
    added = {"req": 0, "cost": 0, "sub": 0}
    adders = {"req": store.add_request, "cost": store.add_cost_state, "sub": store.add_subagent}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for projects_arg in roots:
        projects = os.path.normpath(os.path.expanduser(projects_arg))
        acct = accounts.detect(projects)
        if acct:
            store.add_account_seen(con, projects, acct, now)
    known = store.files_seen(con)
    present, counts = set(), {"files": 0, "scanned": 0, "skipped": 0}
    for projects, pdir, path, kind, session in transcripts(roots):
        try:
            st = os.stat(path)  # before reading: what is appended meanwhile makes the next run read the file again
        except OSError:
            continue
        present.add(path)
        counts["files"] += 1
        sig = (st.st_size, st.st_mtime_ns, SCAN_VERSION)
        if not full and known.get(path) == sig:
            counts["skipped"] += 1
            continue
        try:
            for typ, rec in scan(pdir, path, kind, session):
                rec["root"] = projects
                if typ == "tool":  # counted in the tool_calls total, not in the returned dict
                    store.add_tool_call(con, rec)
                elif typ == "dir":  # working dirs per session: detects one session mixing projects or companies
                    store.add_session_dir(con, rec)
                elif typ == "compact":  # tells a paused session from one already compacted
                    store.add_compaction(con, rec)
                else:
                    added[typ] += adders[typ](con, rec)
        except OSError:
            continue
        except sqlite3.OperationalError:  # a locked or broken database is not this file's fault
            raise
        except Exception as e:  # one odd transcript must not stop the others
            # left unmarked, so the next run reads it again; its records so far dedupe then. Never the text
            sys.stderr.write("sessioncoach ingest: skipped %s (%s)\n" % (path, type(e).__name__))
            continue
        store.mark_file_seen(con, path, *sig, now)  # same transaction as its records: both or neither
        counts["scanned"] += 1
        if counts["scanned"] % COMMIT_EVERY == 0:
            con.commit()
    roots_n = tuple(os.path.normpath(os.path.expanduser(r)) + os.sep for r in roots)
    store.forget_files(con, [p for p in known if p.startswith(roots_n) and p not in present])  # transcripts Claude Code deleted
    backfill_roots(con)
    _log_run(con, "transcripts", added)
    if stats is not None:
        stats.update(counts)
    return added


def backfill_roots(con, default_root=None):
    """Rows captured before roots were recorded came from the default transcript root."""
    default_root = default_root or paths.default_root()
    for table in ("requests", "cost_state", "subagents", "tool_calls"):
        con.execute("UPDATE %s SET root = ? WHERE root IS NULL" % table, (default_root,))


def import_jsonl(con, directory):
    """Import an earlier JSONL snapshot (requests/cost_state/subagents .jsonl). Idempotent."""
    added = {"req": 0, "cost": 0, "sub": 0}
    files = (("req", "requests.jsonl", store.add_request), ("cost", "cost_state.jsonl", store.add_cost_state),
             ("sub", "subagents.jsonl", store.add_subagent))
    for typ, name, adder in files:
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            continue
        with open(path, errors="replace", encoding="utf-8") as fh:
            for line in fh:
                try:
                    added[typ] += adder(con, json.loads(line))
                except (ValueError, KeyError):
                    continue
    backfill_roots(con)
    _log_run(con, "jsonl:" + os.path.basename(os.path.normpath(directory)), added)
    return added


def _log_run(con, source, added):
    con.execute("INSERT INTO ingest_runs VALUES (?,?,?,?,?)",
                (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), source,
                 added["req"], added["cost"], added["sub"]))
    con.commit()
