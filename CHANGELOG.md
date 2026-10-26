# Changelog

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
