# `sessioncoach live --json`: the running sessions, for other tools

`sessioncoach live` is read-only. It reads the running Claude sessions and the tail of their
transcripts, plus plan limits and recent alerts from `metrics.db`, and prints them as one JSON
document. It never writes `metrics.db`, never runs an ingest and never sends a notification.

Like every reader of a WAL-mode SQLite database, `live` may create SQLite's `metrics.db-wal` and
`metrics.db-shm` side files next to `metrics.db` as it opens it (the WAL needs them to show
committed data). No other file is written.

Run it with `--json` for the machine-readable form:

```bash
sessioncoach live --json
sessioncoach live --json --since 1234      # only events newer than the last cursor
```

## Contract

The top level is an object with `"version": 1`.

```json
{
  "version": 1,
  "now": 1795530913,
  "limits": {
    "five_hour": {"pct": 22.0, "resets_at": 1795543800},
    "seven_day": {"pct": 47.0, "resets_at": 1795737600}
  },
  "sessions": [
    {
      "pid": 30313,
      "session_id": "…",
      "cwd": "/abs/path",
      "project": "label",
      "status": "busy",
      "model": "claude-opus-5-5",
      "context_tokens": 335000,
      "last_activity": 1795530900,
      "cache_ttl_s": 3600,
      "cache_expires_at": 1795534500,
      "rewrite_usd": 6.70,
      "today_usd": 12.40,
      "source": "transcript",
      "match": "pid"
    }
  ],
  "events": [
    {
      "id": 1234,
      "ts": 1795530000,
      "source": "context_tokens",
      "session_id": "…",
      "level": "warning",
      "title": "Claude · 500k tokens of context",
      "body": "Every turn re-reads …"
    }
  ],
  "cursor": 1234
}
```

### `version`

An integer that lets consumers detect a breaking change. Fields are only ever **added**; a field
removal or a change of a field's meaning bumps the version. Treat an unknown `version` as
"fields I understand may still be there, but check before relying on the rest".

### `now`

Epoch seconds of the moment the document was built. `cache_expires_at`, `last_activity` and
`ts` are comparable against it.

### `limits`

For each window, the latest `limits_samples` row (from any session) of the last 6 h that reports it:
the 5-hour and 7-day percentages and reset times, both epoch seconds. A sample can carry one window
only, so each window is looked up on its own. `null` when neither window has a sample in the last 6 h.
Both keys are always present; a window the status line did not report has `null` values.

### `sessions`

One object per running Claude session, newest `last_activity` first. A session is listed only if
its `pid` is alive (on Windows every `pid` counts as alive — a documented limitation, so dead
sessions may be listed there). Each object:

- `pid` — the process id of the running session.
- `session_id` — the session id, which also names its transcript (`<projects>/*/<session_id>.jsonl`).
- `cwd` — the working directory from the session file.
- `project` — the project label (`projects.Labeler`), or the `cwd` basename when there is no transcript.
- `status` — the session's status, as written by Claude Code.
- `model` — the model of the last turn that reported usage.
- `context_tokens` — input + cache read + cache write tokens of that turn.
- `last_activity` — epoch seconds of that turn.
- `cache_ttl_s` — the cache lifetime that turn wrote with (3600 or 300), or the 1 h default.
- `cache_expires_at` — `last_activity` + `cache_ttl_s`.
- `rewrite_usd` — `context_tokens` × the model's cache-write price, list price, as an estimate of
  what returning after expiry re-writes.
- `today_usd` — the session's recorded cost today (`cost_slices`, the local day), or `null`.
- `source` — where the numbers came from: `"transcript"` (the transcript tail), `"metrics"`
  (the tail had no usage, so the latest `requests` row was used) or `null` (no numbers
  available: no transcript, or neither source had a row).
- `match` — always `"pid"`: how the session was found to be live.

A session whose transcript is missing is still listed, with `source` `null` and `model`,
`context_tokens`, `last_activity`, `cache_ttl_s`, `cache_expires_at` and `rewrite_usd` set to
`null`. A session whose transcript tail has no usage line falls back to `metrics.db` (`source`
`"metrics"`); if there is no such row there either, the numbers stay `null`.

### `events`

Newest alerts, oldest first, at most 50. Without `--since`, the events of the last 24 h; with
`--since N`, every event with `rowid > N`. Each object:

- `id` — the event's `rowid`.
- `ts` — epoch seconds the event was recorded.
- `source` — the check that fired (`five_hour`, `seven_day`, `*_pace`, `context`,
  `context_tokens`, `budget`, `cache_expiry`, …).
- `session_id` — the session it fired for, or `null`.
- `level` — `"warning"` (limits at 90 %+, pace, cache expiry, context) or `"info"`.
- `title` / `body` — the event in the user's language, from the same templates the desktop
  notifications use. An unknown source has `title` = the source and `body` = `""`.

A `cache_expiry` event stores only numbers (no project or handoff/compact hint), so its `title`
is the short form — "cache expires in ~N min" — without the project or the handoff advice the
desktop notification adds. When the event's `session_id` still names a listed live session, that
session's `project` is appended to the `body` (after an em dash) so consumers can still see which
session it is about.

### `cursor`

The max event `id` returned (or the `--since` value when nothing is new). Pass it back as
`--since` on the next poll to fetch only what arrived since.

## Polling

```bash
# first call: everything running and the last 24 h of alerts
sessioncoach live --json > live.json

# `cursor` is null until the first event arrives. Pass --since once it is numeric; while it is
# still null, keep polling without --since so the first event is not missed.
cursor=$(python -c "import json;c=json.load(open('live.json'))['cursor'];print(c if isinstance(c,int) else '')")
if [ -n "$cursor" ]; then
  sessioncoach live --json --since "$cursor" > live.json
else
  sessioncoach live --json > live.json
fi
```

## Privacy

From each `<config>/sessions/<pid>.json` only `pid`, `sessionId`, `cwd`, `status`, `updatedAt` and
`procStart` are read; the `*.key` files are never opened. From a transcript only usage numbers,
the model and the timestamp are read — message text never reaches the output.
