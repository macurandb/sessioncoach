# Scheduling, platforms and upgrading

## Scheduled ingest

Run `sessioncoach ingest` **daily**: Claude Code deletes local transcripts after 30 days
(`cleanupPeriodDays`), and sessioncoach keeps the metrics after the transcripts are gone.
`sessioncoach setup` schedules it for you with your system's own scheduler (every 30 min by default; set
`ingest.interval_min` in `~/.sessioncoach/alerts.json`, then re-run `sessioncoach ingest-install`):

| System | Scheduler | Notes |
|---|---|---|
| macOS | LaunchAgent (launchd) | a review missed while asleep runs on wake |
| Linux | systemd user timers (`~/.config/systemd/user`) | `Persistent=true`: a review missed while off runs at the next boot/login |
| Linux without a user systemd (containers, WSL without systemd) | your crontab | only a block marked `sessioncoach` is added or removed; the previous crontab is saved in `~/.sessioncoach/backups/` |
| Windows (experimental) | Task Scheduler (`schtasks`), per user, no admin | runs hidden via `pythonw.exe`; missed starts run when the PC is back; no log file |

How `ingest` reads transcripts: [how it measures](how-it-measures.md#reading-transcripts).

On every system, each ingest also sends a review that was missed (back-up). No scheduler at all
(rare): `sessioncoach setup` says so and you run `sessioncoach ingest` yourself. `SESSIONCOACH_SCHEDULER=launchd|systemd|cron|schtasks|none` forces a choice.
Your data lives in `~/.sessioncoach` (`%USERPROFILE%\.sessioncoach` on Windows; override with `SESSIONCOACH_DATA_DIR`).
Transcripts are read from `~/.claude/projects` (`%USERPROFILE%\.claude\projects`), or `$CLAUDE_CONFIG_DIR/projects`,
plus the `roots` of [`accounts.json`](accounts-and-workspaces.md). `sessioncoach setup --scope global|project|company`
chooses where the hook and status line go (`~/.claude/settings.json`, `<cwd>/.claude/settings.local.json`,
or `$CLAUDE_CONFIG_DIR/settings.json`). `sessioncoach check` warns when the last ingest is more than a day old, and
fails once it is older than Claude Code's transcript retention (`cleanupPeriodDays`, 30 by default): transcripts are
then being lost. `contrib/launchd/` keeps a manual template.

## Windows notes (experimental)

- Claude Code runs the `hooks` and `statusLine` commands through **Git Bash** when it is installed, otherwise **PowerShell**
  ([hooks](https://code.claude.com/docs/en/hooks), [status line → Windows configuration](https://code.claude.com/docs/en/statusline)).
  sessioncoach writes commands that work in both: the Python path with forward slashes and no quotes (8.3 short form if the path
  has spaces). If that drive has 8.3 names turned off, the path is quoted, which Git Bash runs and PowerShell does
  not: then install Python in a path without spaces. The status line you had before is kept and run with the same shell.
- `sessioncoach check` cannot test POSIX permissions there; it checks that the data directory is inside your user profile.
- Scheduled runs use `pythonw.exe` (no console window).

## Upgrading from kit

An earlier private version was called `kit`. A machine set up under that name keeps working without changes:
`bin/kit` still runs, `KIT_*` variables are still read, the data stays in `~/.kit`, and `sessioncoach check` points
out jobs still installed under the old name. Running `sessioncoach setup` again replaces them (never both at once).
