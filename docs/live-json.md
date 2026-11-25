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

### `lang`

`"es"` or `"en"`, from the same order every message uses (`config.notice_lang`): so a panel can
localise its own strings to match the user's language.

### `accounts`

One object per transcript root that has a running session or a plan-limit sample in the last 6 h,
each with:

```json
{
  "id": "1f8ab3c9d0e2",
  "key": "personal",
  "label": "Personal · Max",
  "plan": "Max",
  "today_usd": 12.40,
  "sessions": 2,
  "five_hour": {"pct": 12.0, "resets_at": 1795543800, "window_usd": 18.0, "usd_per_pct": 1.5, "eta": null},
  "seven_day": {"pct": 51.0, "resets_at": 1795737600, "window_usd": 1535.0, "usd_per_pct": 30.1, "eta": null,
                "projected_pct": 78.0},
  "groups": [
    {"id": "acme", "five_hour_usd": 17.2, "seven_day_usd": 1455.0},
    {"id": "unassigned", "five_hour_usd": 0.8, "seven_day_usd": 80.0}
  ]
}
```

- `id` — the first 12 hex of sha256 of the account's config dir: a stable, unique identity per
  account (two accounts are never the same `id`, even when their `key` falls back to `"unknown"`).
- `key` — the account key: the `accounts.json` id that owns the root, else `auto:<org hash>`, else
  `"unknown"`.
- `label` — the `accounts.json` label of that account, else a plan word from `accounts.detect`
  (`"Max"`, `"Team"`, `"Pro"`), else the config dir's basename. Never an email or a name.
- `plan` — that plan word, or `null`.
- `today_usd` — the root's `cost_slices` today.
- `sessions` — the number of running sessions on that root.
- `five_hour` / `seven_day` — `resets_at` is that root's current window (see *How samples are read*
  below) and `pct` its highest observed percentage, since the login (as `limits` is, but filtered by
  root); `window_usd` is the list-price USD
  of that root's `requests` whose `ts` falls in `[resets_at - window, now]` (5 hours, or 7 days);
  `eta` is `limits.pace(...)`. The seven-day window adds `projected_pct` — `pct` extrapolated to the
  full 7 days once at least 6 h of the window have elapsed, else `null`.
- `usd_per_pct` (each window) — list-price USD that one percentage point of that window costs on
  this account, so the panel can turn "a further turn re-reads X %" into dollars and back. It is
  **not** `window_usd / pct` (that would be wrong when the login switch happened inside the
  window): it is computed from the first observed point of the *current* window since the login
  (`accounts_seen`), not limited to the last 6 h (`t0`, `p0`), to the earliest sample that reached
  the window's current highest percentage (`t1`, `p1`), as `usd / (p1 − p0)`, where `usd` is the
  list-price USD of that root's `requests` with `t0 < ts <= t1`. It is `null` when the percentage
  delta is below 5 points (`pct` is a whole number, so a smaller delta is mostly rounding), when
  nothing was spent in that span, or when there are not two usable points.
- `groups` — the account's window spend per tenant, as `{"id", "five_hour_usd", "seven_day_usd"}`
  rows. The two windows are exactly the ones `five_hour.window_usd` / `seven_day.window_usd` sum
  over, so the rows add up to them (modulo rounding). Ordered by `seven_day_usd` descending, with
  `"unassigned"` last; tenants with no spend in either window are omitted.

### `groups`

One object per tenant that has spend or a running session, ordered by `week_usd` descending with
`"unassigned"` last:

```json
{"id": "acme", "label": "Acme", "today_usd": 10.0, "five_hour_usd": 6.0,
 "week_usd": 900.0, "sessions": 1}
```

- `label` is the group's label from `projects.json`'s `"groups"` block, else the tenant id itself.
- `today_usd` and `week_usd` are `cost_slices` (today, and the last 7 days).
- `five_hour_usd` is the list-price USD of all `requests` since the earliest current 5-hour window
  start across the accounts, or `null` when no account has a 5-hour sample.
- `sessions` is the number of running sessions in the group.

### `limits`

For each window, the current window across all sessions (see *How samples are read*): its reset time
and highest observed percentage, both epoch seconds / percent. A sample can carry one window only, so
each window is looked up on its own. `null` when neither window has a live sample. Both keys are always
present; a window the status line did not report, or one that has ended, has `null` values.

#### How samples are read

Each running session's status line records the plan limits it last received from the API. A terminal
app that redraws idle sessions (attaching, resizing) makes them record their *last* response again,
with a fresh timestamp and an older, lower percentage. Within one window the real percentage never
goes down, so `live` reads the samples as an envelope instead of trusting the newest row:

- the current window is the greatest reset that is still in the future among the samples of the last
  6 h (a window whose reset has passed is over and is not reported);
- its percentage is the highest valid one among all of that window's samples (since the login, for
  an account), and its time the earliest sample that reached it;
- the pace (`eta`) starts from the envelope 30 minutes ago — the highest percentage seen up to then;
- rows written in the same second count as their highest value, samples dated in the future (beyond
  a 2-minute clock skew) are ignored, and an unusable value (text, NaN, an absurd epoch) is skipped.

### `sessions`

One object per running Claude session, newest `last_activity` first. A session is listed only if
its `pid` is alive (on Windows every `pid` counts as alive — a documented limitation, so dead
sessions may be listed there). Each object:

- `pid` — the process id of the running session.
- `session_id` — the session id, which also names its transcript (`<projects>/*/<session_id>.jsonl`).
- `cwd` — the working directory from the session file.
- `project` — the project label (`projects.Labeler`), or the `cwd` basename when there is no transcript.
- `account` — the account key of the account this session belongs to, derived from the config dir
  its session file came from (so it is set even for a session with no transcript).
- `account_id` — the account's `id`, derived from the config dir the session file came from (not
  from the transcript), so a session with no transcript still belongs to its account.
- `group` — the tenant (group) id, or `"unassigned"` when no rule assigns one.
- `status` — the session's status, as written by Claude Code.
- `model` — the model of the last turn that reported usage.
- `context_tokens` — input + cache read + cache write tokens of that turn.
- `last_activity` — epoch seconds of that turn.
- `cache_ttl_s` — the cache lifetime that turn wrote with (3600 or 300), or the 1 h default.
- `cache_expires_at` — `last_activity` + `cache_ttl_s`.
- `rewrite_usd` — `context_tokens` × the model's cache-write price, list price, as an estimate of
  what returning after expiry re-writes.
- `turn_usd` — `context_tokens` × the model's cache-read price, list price: what each further turn
  re-reads of that context. `null` when `context_tokens` is `null` (no model/usage read).
- `clear_handoff` — `true` when `/clear` in this session's working directory would really get
  sessioncoach's automatic handoff: the prompt hook and the resume hook are both active for that
  session's config dir (or its project settings) **and** the idle guard is on (`alerts.json` →
  `nudge.block` is true). That is the flow that gives it: the prompt hook holds the first prompt after
  a pause back and records it, the resume hook turns the following `/clear` into the automatic digest.
  `false` when any part is missing (a resume hook alone does not promise a free handoff). The panel only promises a free handoff when
  this is `true`.
- `today_usd` — the session's recorded cost today (`cost_slices`, the local day), or `null`.
- `source` — where the numbers came from: `"transcript"` (the transcript tail), `"metrics"`
  (the tail had no usage, so the latest `requests` row was used) or `null` (no numbers
  available: no transcript, or neither source had a row).
- `match` — always `"pid"`: how the session was found to be live.

A session whose transcript is missing is still listed, with `source` `null` and `model`,
`context_tokens`, `last_activity`, `cache_ttl_s`, `cache_expires_at`, `rewrite_usd` and `turn_usd`
set to `null`. A session whose transcript tail has no usage line falls back to `metrics.db` (`source`
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
