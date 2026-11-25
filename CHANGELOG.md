# Changelog

## Unreleased

- `sessioncoach group` names your clients and reports each one's spend today, this week and this
  month; `group sync-herdr` takes the clients made with the `herdr-space-groups` mod.
  See docs/groups.md. Its writes are serialised by an operating-system lock, a
  `projects.json` that is not a JSON object is never overwritten, and a bad `sync-herdr` feed is
  rejected before any change. A group keeps its id across syncs.
- `sessioncoach live --json` adds `lang`, per-session `account`, `account_id` and `group`, an
  `accounts` list (per transcript root: a unique `id`, plan, today and window spend, projections)
  and a `groups` list; the contract stays `"version": 1` (see docs/live-json.md).
- `sessioncoach herdr-install` puts the running sessions (context, cache expiry and new alerts) in a
  Herdr panel, with `herdr-uninstall` to remove it and `live --demo` for a synthetic view; see
  docs/herdr.md.
- `sessioncoach live` reads the running Claude sessions (fresh from their transcripts) with their context size, cache
  lifetime and today's cost, plus the plan limits and the newest alerts, as one read-only JSON document for other
  tools (`--json`; the contract is in `docs/live-json.md`).
- `sessioncoach harness` measures the other coding harnesses you run (OpenCode 2 first), per provider and model:
  the same token, cache and error figures as Claude Code, read from `opencode.db` into `metrics.db` (numbers and
  names only, never prompts or output). `sessioncoach ingest` reads them incrementally, and `sessioncoach check`
  reports when one is found but could not be read.
- The status line says `⚠ /compact o /handoff` once a session passes the lowest context alert, so the advice reaches
  terminals where desktop notifications do not (herdr, ssh). `"statusline_hint": false` in `alerts.json` turns it off.
- `sessioncoach --version` works, as `sessioncoach version` already did.
- The dashboard server reads the body of a POST it rejects, so a Windows client gets the error instead of a reset
  connection.
- `sessioncoach check` warns when plan-limit samples stopped while sessions go on, which is what sessions that do not
  draw the status line (VS Code extension, desktop app) look like: its limit alerts do not see them.
- `sessioncoach ingest` prints when it ran (`at`), so each line of `ingest.log` has its date.
- The context-size alert (`context_tokens`) also fires from the prompt hook, so it reaches sessions that do not draw
  the status line, at the next prompt. One alert per session and threshold, whichever fires first. Installing only
  the hook now brings these desktop notifications too; `"context_tokens": []` in `alerts.json` turns them off.
- `sessioncoach report` words its recommendations instead of printing their numbers as JSON.
- `sessioncoach tools` estimates the context each tool adds per turn (total, median, p90, and what the largest 1 % of
  turns carry), from token counts only. Compare tools with it: it also holds what arrives with a result, such as hook
  output.
- Sessions started inside a Claude Code scratchpad count for the project that owns it, not as a project of their own.
- `sessioncoach codemode` compares sessions with Code Mode (they call REPL) against the rest: tools per request, list
  price per tool and context re-read per tool, overall and at equal context size per request, against the same days
  and the days before Code Mode. Tools run inside a REPL block count; REPL and Poll do not.
- On macOS, an alert that terminal-notifier cannot show (its notifications not allowed yet in System Settings) goes
  out through osascript instead of being lost.

## 0.1.1 - 2026-10-26

- `sessioncoach demo`: the live session's cost snapshots came after the next burst's first request, so a third of
  the hours of a day its "Data quality" said CHECK instead of PASS (and a test of `verify` failed in CI).

## 0.1.0 - 2026-10-26

First public release.

### Measurement
- `sessioncoach ingest` reads Claude Code's local transcripts into a private SQLite file (metadata only: token counts,
  models, times, ids, project and tool names; never session content), keeps the history after Claude Code deletes old transcripts, and skips files that did not change.
- Cost per day, week, project, company (tenant) and Claude account, split into *recorded* (Claude Code's own
  `cost-state`) and *estimated* (list price), with a backtest and a conservation check (`sessioncoach verify`).
- A config dir that switches accounts (`/login`) splits its cost at the switch, as seen by the next ingest.
- Local dashboard (`sessioncoach dashboard`, 127.0.0.1 only, secret link + session cookie + CSRF token), `report`,
  `day` and `tools` in the terminal. English and Spanish.
- Recommendations from deterministic rules: resumes after the prompt cache expired (R1), 1M-context variants (R2),
  general-purpose subagents on Opus (R3), subagents' share (R4), long sessions (R16, `review.long_context_tokens`,
  300k by default). The dashboard and the daily review use the same criteria and thresholds.
- Daily review (`sessioncoach review`, scheduled with `review-install`): one change for today from the last active day,
  by its savings ceiling, with no tokens spent.

### Acting before the money is spent
- Resume warning (`hook-install`, a `UserPromptSubmit` hook): before a prompt re-reads a big context whose cache
  expired, says what it would cost and what to type instead.
- Idle guard (opt-in, `idle-guard-on`): the first prompt after a pause of an hour or more is held back, so nothing is
  spent until you choose: send it again, `/clear` for a free automatic handoff built from the previous transcript, or
  `/handoff`.
- Handoffs: `/handoff [topic]` (`handoff-install`) has Claude write `docs/HANDOFF-<date>-<topic>.md`; the resume hook
  (`resume-hook-install`, `SessionStart`) gives the session after `/clear` the newest handoff of its project from the
  last 24 h (never one tracked by git). In the VS Code extension and the desktop app, whose `/clear` starts a new
  session, it gives a handoff written in the last 30 min that no session got yet (once), so an old one never reaches
  a new window.
- `/keep-context [off]` keeps one session's whole context through Claude Code's idle summary; `sessioncoach
  idle-compact` tells, from your own returns, whether `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` pays off.
- Plan limits and alerts in the status line (`statusline-install`, chained after your own status line), notifications
  for the 5-hour and weekly limits, context size and budgets, and a warning before a paused session's cache expires.
- Warnings view (`alert-log`, dashboard card): every warning with the numbers that fired it and what happened next.

### Setup
- `sessioncoach setup` installs each piece after explaining it; `check` verifies everything read-only; `uninstall --all`
  removes it all (data stays unless `--purge`). Settings panel in the dashboard, per scope (global, project, company).
- Scheduled jobs on launchd (macOS), systemd user timers or cron (Linux), and Task Scheduler (Windows, experimental).
- Messages pick their language the same way everywhere: the prompt's words (hooks), `SESSIONCOACH_LANG`, `lang` in
  `alerts.json`, Claude Code's `language` setting, `LANG`, English.

### Compatibility
- Python 3.9+, standard library only. Tested on macOS, Linux and Windows with Python 3.9 and 3.13.
- An earlier private version was called `kit`: `bin/kit`, `KIT_*` variables, the `~/.kit` data directory and jobs or
  hooks installed under that name are still recognised, and `setup` replaces them.
