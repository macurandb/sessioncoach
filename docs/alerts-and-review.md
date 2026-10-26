# Plan limits, alerts and the daily review

## Status line and notifications

On Pro and Max plans Claude Code passes the 5-hour window and weekly cap usage to the status line.
`sessioncoach statusline-install` chains sessioncoach **after** your current status line (it is kept verbatim and
still runs first; `statusline-uninstall` restores it). You then get:

- `5h 42% ↻18:37 · 7d 61% ↻Sat 03:57 · ctx 12%` in the status line, yellow at 75 %, red at 90 %;
- a desktop notification (macOS, Linux, Windows) when a threshold is crossed, once per window, or when the current
  pace would exhaust a window before it resets;
- budgets in API-equivalent USD per day or month, for everything, a workspace, a project or an account, checked on
  every ingest;
- a warning **before the cache of a paused session expires** (checked on every ingest, so no status line needed):
  "the cache of shop expires in ~15 min", with the context size and what writing it again would cost, while a
  `/handoff` or `/compact` still reads the cache cheaply. Large main sessions only (`min_context_tokens`, 200k), once
  per pause, not for a session another one in the same project already replaced, never for 5-minute caches. Tune or
  switch off under `cache_expiry` in `alerts.json` (`enabled`, `min_context_tokens`, `min_idle_min`,
  `min_remaining_min`, `slack_min`; not in the settings panel yet);
- a **Warnings** card in the dashboard (and `sessioncoach alert-log`): what fired, why, and what happened next, plus
  the long pauses that got no warning and the reason, to tell a warning that helped from a false alarm;
- in the dashboard: the limits, the projection, and **what is eating the current window** (by cause, project and
  session).

Configure thresholds and budgets in `~/.sessioncoach/alerts.json`; `sessioncoach alerts --test` sends a test
notification. Every alert is also written to `~/.sessioncoach/logs/alerts.log`.

Notifications use `osascript` on macOS (no install; allow notifications for **Script Editor** in System Settings →
Notifications), `notify-send` on Linux (package `libnotify-bin` or `libnotify`), and a PowerShell toast on Windows
(nothing to install; if it fails, sessioncoach stays silent). If
[`terminal-notifier`](https://github.com/julienXX/terminal-notifier) is installed on macOS it is used instead: a newer
alert replaces the previous one, and clicking one opens `http://127.0.0.1:8787/`. That page needs the dashboard
running and opened once from its printed link in that browser (see
[privacy and security](privacy-and-security.md)); otherwise it asks you to open the link that
`sessioncoach dashboard` printed.

## The resume warning

`sessioncoach hook-install` (or `setup`) adds a `UserPromptSubmit` hook. Before a prompt makes Claude Code write a
big expired cache again, it tells you what that would cost and what to type instead. It is shown to you, never to the
model. Its thresholds are `nudge.idle_min` (the cache lifetime the transcript recorded, unless you set it) and
`nudge.min_context_tokens` (100k) in `alerts.json`. To hold the prompt back instead of only warning, see
[the idle guard](handoff-and-idle-guard.md#the-idle-guard-opt-in).

## Daily review

`sessioncoach review` looks at your **last active day** (on a Monday, the Friday) and tells you what was expensive,
session by session: resumes after the cache expired, turns with a very long context, subagents on Opus, and sessions
that worked on more than one project or company. It picks **one** change for today (the biggest savings ceiling, an
estimate) and compares with the active day before. It reads only the numbers sessioncoach already stores, so it
spends no tokens.

`sessioncoach review-install` schedules it with your system's scheduler (a run missed while the machine was off is
recovered where the scheduler supports it, and otherwise by the next ingest). Everything is set in
`~/.sessioncoach/alerts.json` → `review`; re-run `review-install` after changing `time` or `days`:

```json
{"review": {"enabled": true, "time": "08:00", "days": ["mon", "tue", "wed", "thu", "fri"],
            "channels": ["notification", "dashboard"], "lookback": "last_active", "max_tips": 1,
            "long_context_tokens": 300000}}
```

`lookback` can be a number of days instead (1 = yesterday). `long_context_tokens` is also the dashboard's R16
threshold.

Email is optional and off by default: add `"email"` to `channels` and an `email_command` (e.g.
`["mail", "-s", "{title}", "you@example.com"]`). The body goes on stdin and holds only totals, never project names.
sessioncoach stores no mail credentials; the command you set sends the mail.
