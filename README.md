# sessioncoach

**Is your Claude Code setup worth it?** sessioncoach measures what Claude Code really costs you,
shows where the money goes, and tells you what to change — with the evidence, the expected
saving (always labelled as an estimate) and how to check afterwards that it worked.

> **Stop paying for a cold cache.** After a long pause, the next prompt re-reads the whole context at full price.
> sessioncoach warns before that happens (or holds the prompt back) and hands the next session a free handoff
> instead, so you decide before the money is spent.

> **Local only.** sessioncoach reads Claude Code's transcripts on your machine and keeps only
> numbers and ids (tokens, models, timestamps) in a local SQLite file. No prompts, outputs,
> file contents or commands are stored. Nothing is sent anywhere unless you turn on the optional
> daily-review email (totals only). No account.

![The dashboard on synthetic data: yesterday's review, with one change for today and the sessions behind it](docs/screenshots/overview.png)

<details><summary>More screenshots (synthetic data, <code>sessioncoach demo</code>)</summary>

![Warnings: what fired, why, and what happened next in that session](docs/screenshots/warnings.png)

![What to change: deterministic rules over your data, with the evidence, how, and how to verify](docs/screenshots/what-to-change.png)

</details>

> **Dollars are API-equivalent.** Every figure is what the same tokens cost at Anthropic's list price. On a Pro or
> Max subscription you do not pay that: read it as how much of your plan's limits a habit burns, and compare
> figures with each other, not with your invoice.

Status: early preview (0.1.0). Interfaces may change. sessioncoach reads Claude Code's local transcripts, an
internal format that Claude Code does not document and may change; `sessioncoach verify` shows how much it could read.

## Quickstart (2 minutes)

Requirements: Python 3.9+ (standard library only), Claude Code. Works on macOS and Linux (and WSL); Windows support is experimental
(covered by tests and CI, not yet used day to day: reports welcome).

Install `sessioncoach` with [pipx](https://pipx.pypa.io) (or run everything below as `bin/sessioncoach` from a clone):

| System | Install |
|---|---|
| macOS | `brew install pipx && pipx ensurepath && pipx install git+https://github.com/macurandb/sessioncoach` |
| Linux / WSL | `python3 -m pip install --user pipx && python3 -m pipx ensurepath && pipx install git+https://github.com/macurandb/sessioncoach` (or your distro's `pipx` package) |
| Windows (PowerShell) | `py -m pip install --user pipx; py -m pipx ensurepath; py -m pipx install git+https://github.com/macurandb/sessioncoach` — then open a new terminal |

```bash
sessioncoach setup            # guided install: ingest, status line, resume warning, daily review, /handoff (--yes for defaults)
sessioncoach check            # verify it all works (read-only; exit code 1 on a problem; --json, --with-claude)
sessioncoach uninstall --all  # undo setup (run it in each project set up with --scope project); data stays unless --purge
sessioncoach help             # every command: what it gives you first, its syntax after
```

`setup` asks about each piece separately and says first what it gives you, with a figure of your own when the
database already has one (for instance what resuming big sessions after the cache expired cost you in the last 7
days). How each piece works, which file it writes and how to remove it is in `sessioncoach help <command>` and below.

Or step by step from a clone:

```bash
git clone https://github.com/macurandb/sessioncoach && cd sessioncoach
bin/sessioncoach ingest             # read transcripts into ~/.sessioncoach/metrics.db (safe to re-run)
bin/sessioncoach dashboard          # opens http://127.0.0.1:8787/?k=<secret> (local only; the link is printed too)
bin/sessioncoach report             # the same figures in the terminal
bin/sessioncoach verify             # how trustworthy the cost figures are
bin/sessioncoach hook-install       # warn when you resume a big session after the cache expired (shown to you, not to the model)
bin/sessioncoach tools --project X  # tool calls per day (names only): exploration, Bash, each MCP server
bin/sessioncoach review             # yesterday's review: what was expensive and one change for today
```

Run `bin/sessioncoach ingest` **daily**: Claude Code deletes local transcripts after 30 days
(`cleanupPeriodDays`), and sessioncoach keeps the metrics after the transcripts are gone.
`sessioncoach setup` schedules it for you with your system's own scheduler (every 30 min by default; set
`ingest.interval_min` in `~/.sessioncoach/alerts.json`, then re-run `sessioncoach ingest-install`):

| System | Scheduler | Notes |
|---|---|---|
| macOS | LaunchAgent (launchd) | a review missed while asleep runs on wake |
| Linux | systemd user timers (`~/.config/systemd/user`) | `Persistent=true`: a review missed while off runs at the next boot/login |
| Linux without a user systemd (containers, WSL without systemd) | your crontab | only a block marked `sessioncoach` is added or removed; the previous crontab is saved in `~/.sessioncoach/backups/` |
| Windows (experimental) | Task Scheduler (`schtasks`), per user, no admin | runs hidden via `pythonw.exe`; missed starts run when the PC is back; no log file |

`ingest` reads only the transcripts that are new or changed since the last run (it remembers each file's size and
modification time in the `files_seen` table); a file that grew is read again from its start. The result is the same as
reading everything. `sessioncoach ingest --full` reads every transcript again (use it if you edited the database by hand, or
if a transcript was restored with an older modification time that equals the one stored).

On every system, each ingest also sends a review that was missed (back-up). No scheduler at all
(rare): `sessioncoach setup` says so and you run `sessioncoach ingest` yourself. `SESSIONCOACH_SCHEDULER=launchd|systemd|cron|schtasks|none` forces a choice.
Your data lives in `~/.sessioncoach` (`%USERPROFILE%\.sessioncoach` on Windows; override with `SESSIONCOACH_DATA_DIR`).
Transcripts are read from `~/.claude/projects` (`%USERPROFILE%\.claude\projects`), or `CLAUDE_CONFIG_DIR`. `sessioncoach setup --scope global|project|company`
chooses where the hook and status line go (`~/.claude/settings.json`, `<cwd>/.claude/settings.local.json`,
or `$CLAUDE_CONFIG_DIR/settings.json`). `sessioncoach check` warns when the last ingest is getting close to
Claude Code's transcript retention (`cleanupPeriodDays`, 30 by default). `contrib/launchd/` keeps a manual template.

## Commands

`sessioncoach help` prints this list; `sessioncoach help <command>` (or `<command> --help`) adds the options and the
technical detail: which files it writes, where, and how to undo it.

| Command | What it gives you | Usage |
|---|---|---|
| `ingest` | Keeps your history after Claude Code deletes its transcripts (30 days by default); setup schedules it. |
| `report` | Where your money goes and what to change first, in the terminal. |
| `day` | Catch an expensive day early: today against yesterday, per account and project. |
| `tools` | Find out whether exploration or one MCP server is eating your turns. |
| `verify` | How far to trust the cost figures before you act on them. |
| `statusline` | See a plan limit coming before it stops you: limits and alerts in the status line. |
| `statusline-install` | See a plan limit coming before it stops you: limits and alerts in the status line. |
| `statusline-uninstall` | Give the status line back to your own command alone. |
| `hook` | The entry point Claude Code runs for sessioncoach's hooks: the resume warning (prompt), the handoff after /clear (session-start), Claude Code's idle summary (pre-compact) and /keep-context (keep-context). |
| `hook-install` | Decide before a prompt re-reads a big context whose cache expired, not after paying for it. |
| `hook-uninstall` | Stop the resume warning. |
| `alerts` | How close you are to the 5-hour and weekly limits and to your budgets. |
| `alert-log` | Whether the warnings help: what each said, why, and what you did next. |
| `review` | One change for today from yesterday's numbers, with no tokens spent. |
| `review-install` | Get the daily review without remembering to ask for it. |
| `review-uninstall` | Stop the scheduled daily review. |
| `dashboard` | Cost, causes and recommendations at a glance, in your browser (local only). |
| `demo` | Try the dashboard or take screenshots without showing your own data. |
| `setup` | Everything working in two minutes; each piece is optional and reversible. |
| `check` | Know that what sessioncoach installed still works, and how to fix what does not. |
| `uninstall` | Undo what setup did, here and in every settings scope; your data stays unless you ask. |
| `ingest-install` | Never lose history to transcript retention: ingest runs on its own. |
| `ingest-uninstall` | Stop the scheduled ingest (your data stays). |
| `handoff-install` | Pick up a paused session without paying for its context on every message: /handoff writes the state down and you go on in a clean session. |
| `handoff-uninstall` | Remove the /handoff command. |
| `resume-hook-install` | After /clear, the new session starts from your latest handoff by itself: nothing to copy or retype. |
| `resume-hook-uninstall` | Stop loading the latest handoff after /clear. |
| `idle-guard-on` | The first message after a long pause costs nothing until you choose: send it again to go on, or /clear to start clean with a free automatic handoff; /keep-context keeps a session whole when you need it. |
| `idle-guard-off` | Back to the warning alone: a prompt after a long pause is sent as you typed it. |
| `idle-compact` | Whether CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS pays off lowered to 100k or raised to 400k for you, and whether it is set right. |
| `version` | Which version you run, for a bug report. |
| `help` | What each command saves you, before its syntax. |

## What you get

- **Cost per day, week, project and model.** API-equivalent at list price, split into
  *recorded* (Claude Code's own `cost-state`, spread over time by activity) and *estimated*
  (list price, for activity no `cost-state` covers). On subscription plans this is not an invoice.
- **Where the money goes**: cache reads, cache writes, output, input; 1M-context variants; subagents.
- **Recommendations** from deterministic rules over your data:
  - **R1** resuming sessions after the prompt cache expired (big cache rewrites);
  - **R2** heavy use of 1M-context variants;
  - **R3** general-purpose subagents running on Opus;
  - **R4** subagents' share of the cost;
  - **R16** long sessions: turns re-reading more than `review.long_context_tokens` of context (300k by default).

  The daily review uses the same criteria and the same threshold.
- **Data quality, shown next to the numbers**: a backtest (recorded vs list-price estimate per
  interval, 0.8–1.25) and a conservation check. Every figure has the command that reproduces it.
- Dashboard in English and Spanish, light and dark, works on a phone screen.

## Plan limits and alerts

On Pro and Max plans Claude Code passes the 5-hour window and weekly cap usage to the status
line. `bin/sessioncoach statusline-install` chains sessioncoach **after** your current status line (it is
kept verbatim and still runs first; `statusline-uninstall` restores it). You then get:

- `5h 42% ↻18:37 · 7d 61% ↻Sat 03:57 · ctx 12%` in the status line, yellow at 75 %, red at 90 %;
- a desktop notification (macOS, Linux, Windows) when a threshold is crossed, once per window, or when the
  current pace would exhaust a window before it resets;
- budgets in API-equivalent USD per day or month, for everything, a workspace, a project or an
  account, checked on every ingest;
- a warning **before the cache of a paused session expires** (checked on every ingest, so no status line needed):
  "the cache of shop expires in ~15 min", with the context size and what re-writing it would cost, while a handoff
  or `/compact` still reads the cache cheaply. Large main sessions only (`min_context_tokens`, 200k), once per
  pause, not for a session another one in the same project already replaced, never for 5-minute caches.
  Tune or switch off under `cache_expiry` in `alerts.json` (`enabled`, `min_context_tokens`, `min_idle_min`,
  `min_remaining_min`, `slack_min`; not in the settings panel yet);
- a **Warnings** card in the dashboard (and `sessioncoach alert-log`): what fired, why, and what happened next, plus
  the long pauses that got no warning and the reason, to tell a warning that helped from a false alarm;
- in the dashboard: the limits, the projection, and **what is eating the current window**
  (by cause, project and session).

Configure thresholds and budgets in `~/.sessioncoach/alerts.json`; `bin/sessioncoach alerts --test` sends a test
notification.
Notifications use `notify-send` on Linux (package `libnotify-bin`/`libnotify`), a PowerShell toast on Windows
(nothing to install; if it fails, sessioncoach stays silent), and `osascript` on macOS (no install; allow notifications for **Script Editor** in
System Settings → Notifications). If [`terminal-notifier`](https://github.com/julienXX/terminal-notifier)
is installed it is used instead: clicking a notification opens the dashboard and a newer alert
replaces the previous one. Every alert is also written to `~/.sessioncoach/logs/alerts.log`.

## Handoff in one command

**Pick up a paused session without paying for its context on every message.** After an hour away the prompt cache of
a big session has expired, and every message you send re-reads the whole context. Writing the state down and going
on in a clean session is cheaper, but it took three steps to type. Two optional pieces make it one:

- **`/handoff [topic]`**, a Claude Code command (`sessioncoach handoff-install`, or the question in `setup`). Claude
  writes `docs/HANDOFF-<date>-<topic>.md` at the root of the project (goal, state, decisions, next steps, files
  touched, how to verify; in the language of the conversation; one per project if the session touched several) and
  ends by telling you to type `/clear`. The file goes to `commands/handoff.md` next to the settings of the scope
  `setup` uses (`~/.claude`, the project's `.claude`, which git tracks unless you ignore it, or
  `$CLAUDE_CONFIG_DIR`; `setup --yes` never writes it into a project, only when asked). It carries a `sessioncoach:handoff` marker: a `handoff.md` of your own is never overwritten
  or removed.
- **Load the handoff after `/clear`**, a `SessionStart` hook with matcher `clear` (`sessioncoach resume-hook-install`,
  or the question in `setup`). The new session starts with the path and text of the newest `docs/HANDOFF-*.md` of the
  project (the working dir up to the repository root) written in the last 24 hours, cut to 9,000 characters (Claude
  Code caps hook context at 10,000; the path is there for the rest). A handoff that git tracks is skipped: a clone or
  pull gives it a fresh date, so it may not be yours. It never acts on `resume` or `compact`, where
  the context is still there, and it writes nothing (with the idle guard on, it only removes the guard's entry it used). Without it, `/handoff` also gives you the line to type after
  `/clear` ("read docs/HANDOFF-… and continue"); installing or removing it rewrites an installed `/handoff` to match.

With `/handoff` installed the resume warning and the cache-expiry notification say "type /handoff" instead of the
three steps. The notification only sees the command in your user settings (`~/.claude/commands` or
`$CLAUDE_CONFIG_DIR/commands`): the ingest that sends it does not know the session's working dir.

**What the handoff turn costs.** Once the cache has expired, the `/handoff` turn itself writes the whole context
again, once: at list price, context tokens × the 1-hour cache-write price of the model (500k tokens on Opus 5.5 ≈ $4,
API-equivalent). Only that session has the context, so it cannot be avoided; it is paid once instead of on every
message after coming back. Untested idea: switching to a cheaper model (`/model`) for that one turn, if the context
fits its window. `sessioncoach check` shows both pieces per scope, and `sessioncoach uninstall --all` removes them (they are not in the
settings panel yet).

## Daily review

`bin/sessioncoach review` looks at your **last active day** (on a Monday, the Friday) and tells you what
was expensive, session by session: resumes after the cache expired, turns with a very long
context, subagents on Opus, and sessions that worked on more than one project or company. It
picks **one** change for today (the biggest savings ceiling, an estimate) and compares with the active day
before. It reads only the numbers sessioncoach already stores, so it spends no tokens.

`bin/sessioncoach review-install` schedules it with your system's scheduler (see the table above; a run missed while the
machine was off is recovered where the scheduler supports it, and otherwise by the next ingest). Everything is set in
`~/.sessioncoach/alerts.json` → `review`; re-run `review-install` after changing `time` or `days`:

```json
{"review": {"enabled": true, "time": "08:00", "days": ["mon", "tue", "wed", "thu", "fri"],
            "channels": ["notification", "dashboard"], "lookback": "last_active", "max_tips": 1,
            "long_context_tokens": 300000}}
```

`lookback` can be a number of days instead (1 = yesterday). `long_context_tokens` is also the dashboard's R16
threshold. Email is optional and off by default:
add `"email"` to `channels` and an `email_command` (e.g. `["mail", "-s", "{title}", "you@example.com"]`);
the body goes on stdin and holds only totals, never project names. sessioncoach stores no mail credentials.

## Settings panel

The **Settings** button in the dashboard header turns each practice on or off and tunes its
thresholds, with defaults that work out of the box. Pick where it applies: **Global**
(`~/.claude/settings.json`), **Project** (`<project>/.claude/settings.local.json`, kept out of git;
only projects the database knows) or **Company** (`settings.json` in the config dir of an account
from `accounts.json`). Practices: resumed-session notice (hook), chained status line, the
"when to cut" rule in `CLAUDE.md` (a block between `<!-- sessioncoach:cut-rule -->` markers; removing
it deletes only that block), alert thresholds, and the daily review (its LaunchAgent is regenerated
when time or days change).

**Project scope.** The "when to cut" rule goes where you choose: `CLAUDE.local.md` (recommended: personal,
out of git; Claude Code loads it together with `CLAUDE.md`) or `CLAUDE.md` (shared with the team through
git). If you pick `CLAUDE.local.md` in a git repo that does not ignore it, the panel offers to add it to the
project's `.gitignore` as one more change in the previewed diff (never written without your confirmation).
`sessioncoach setup --scope project --cut-dest local|shared [--gitignore]` does the same from the terminal (interactive
setup asks, and the `.gitignore` question defaults to yes; with `--yes` it is only written if you pass
`--gitignore`). `sessioncoach check` warns when `CLAUDE.local.md` has sessioncoach's block and git does not ignore it. Removing
the rule deletes the block from whichever file holds it.

**No duplicated hook.** Claude Code loads the user settings (`~/.claude`, or the account's own config dir
when `CLAUDE_CONFIG_DIR` is set, in which case `~/.claude` is not loaded) plus the project's. So global and
company never duplicate each other, but a project hook duplicates its superior scope (the company that owns
the project per `projects.json` tenant + `accounts.json` company/roots, else global). The panel disables the
project hook (API: 422 `hook_duplicate_superior`; `sessioncoach setup`: step skipped) when the superior already has
it; the status line only warns, since the project's one wins. Installing in global or company warns about
projects with their own hook and offers to remove it from them (a checkbox in the panel, visible in the diff).
`sessioncoach check` flags a double hook.

"See changes" shows the exact diff and writes nothing; "Apply" writes exactly that, after a
`<file>.bak-kit-<timestamp>` backup, atomically, and answers 409 if the file changed in between.
Protections: the session cookie from the printed link (see Privacy and security), per-start token (`X-Kit-Token`,
only embedded in the page served with that cookie), loopback `Host`/`Origin` checks, JSON-only bodies with a size limit, strict validation, and no
client-supplied paths. The panel is hidden in anonymous mode. `POST /api/refresh` also needs the token now.

## Demo (synthetic data)

`bin/sessioncoach demo [--port 8787] [--no-open] [--days 30] [--seed 7]` generates fictional Claude Code transcripts
(companies acme, globex and initech; long sessions, resumes after the cache expired on 1 h and 5 min caches,
subagents on Opus, sessions that mix projects, plan limits, budgets, several accounts) under `<data dir>/demo/`,
runs them through the real pipeline (ingest, cost, limits) and opens the dashboard on them with a
"Demo data" banner. Your own data, `~/.claude` and the schedulers are not touched; the demo
dashboard cannot refresh from real transcripts or edit your settings. Same seed and day, same data.

## Anonymous mode (for screenshots and demos)

Add `&anon=1` to the link the command prints, or press **Anonymous** in the dashboard: projects, workspaces,
accounts and budgets become "Project A", "Workspace 1", "Account 1"… (ordered by spend, consistent
across views; filters keep working) and the database path is hidden. Check your screenshots anyway.

## Workspaces and projects (optional)

Map Claude Code project dirs to projects and workspaces (e.g. one per client, or to merge git
worktrees) in a private file `~/.sessioncoach/projects.json`:

```json
{"rules": [
  {"match": "*code-shop*", "project": "shop", "tenant": "client-a"},
  {"match": "*", "tenant": "personal"}
]}
```

If you keep one Claude Code config dir per workspace (`CLAUDE_CONFIG_DIR`), point sessioncoach at
all of them: `SESSIONCOACH_PROJECTS_DIRS=~/.claude-a/projects:~/.claude-b/projects bin/sessioncoach ingest`.

## How the cost is computed

Claude Code writes cumulative `cost-state` snapshots per session. Each increase is spread over
the requests made since the previous snapshot, in proportion to their list-price weight. The
counter only covers the current process, so when an interval holds more activity than its
increase explains, the increase goes to the most recent activity it covers and the older
activity is estimated at list price instead of being lost. A counter drop (a resumed session)
counts from zero. Subagent totals come from the completion reports the parent receives,
because subagent transcripts under-count output tokens.

## Figures: what each one means

- **Cache percentage**: one definition, shown with its base: share of the *list-price* spend over *all*
  activity in the window that goes to the prompt cache (reads + writes). It is not computed from
  `cost-state` (which has no cost per token type), so do not compare it with a figure built from recorded cost only.
- **Cache lifetime (TTL)**: "cache expired" uses the TTL each transcript really wrote (`ephemeral_1h` = 1 h,
  `ephemeral_5m` = 5 min: API keys, usage credits, subagents). With no data, 1 h. R1, the daily review,
  the plan-window breakdown and the hook all use it; `nudge.idle_min` in `alerts.json`, if you set it, wins.
- **Prices**: list prices from platform.claude.com/docs/en/about-claude/pricing (table date in `sessioncoach/pricing.py`).
  A model not in the table is priced like the newest model of its family and listed as *without exact price*
  (requests and USD affected) in `sessioncoach verify`, `sessioncoach report` and the dashboard's "Data quality"; it is never silent.

## Idle guard and the automatic handoff (opt-in)

**The first message after a long pause costs nothing until you choose.** The resume warning comes after the fact: the
prompt is sent anyway and the whole context is re-read (with 500k tokens on Opus, about $4 at list price, every time
you come back). With the idle guard on (`sessioncoach idle-guard-on`, or the last question in `setup`; off by
default, never turned on by `setup --yes`), that first prompt is held back instead (the hook answers
`{"decision": "block"}`): it is not sent, nothing is spent, and Claude Code shows why, with your text under
"Original prompt:" (in the terminal it also goes back to the input box). Only pauses of 60 min or more are held back (`nudge.block_min` in
`alerts.json`): with a 5-minute cache (API key) a shorter pause gets the plain warning, not a block on every coffee
break. Then:

- **send it again** to go on in that session: the same pause goes through, with no second warning, and re-reads the
  context once;
- **`/clear`** to start clean for free: with the resume hook installed, the new session gets an **automatic handoff**
  built by sessioncoach from the previous session's transcript, with no model: Claude Code's own compaction summary
  when there is one, your last 8 messages, Claude's last reply, the files edited, the latest todo list, and the branch
  and `git status --short` of the working dir (at most 9,000 characters). Only within 30 minutes of the held-back
  prompt, and only in the same project; not when another session of the project is also held back or was active
  since, because the `/clear` may have come from that window (the entry is kept). A `docs/HANDOFF-*.md` written after it wins;
- **`/handoff`** (when installed) for a better handoff: it re-reads the context once, and is never held back.

Limits: the automatic handoff has the facts, not the reasons: it is worse than `/handoff`. Claude Code shows no Yes/No
dialog for a held-back prompt; sending it again is the "yes". When Claude Code already prepared its own summary of the
paused session (its idle compaction, about 54 min after the last turn, with the app open and the machine awake), the
return does not re-read the context: a `PreCompact` hook (matcher `auto`, added by `idle-guard-on`) notes it, and
then the prompt is neither held back nor warned about, only told that the summary will be applied (this also applies
to the plain warning). If that summary later fails, that return goes unwarned. `sessioncoach check` shows the guard
and what it is missing; `idle-guard-off` and `uninstall --all` turn it off and remove its hook and state files.

**`/keep-context`: when you need the whole context after the pause.** Claude Code's idle summary makes the return
cheap but drops detail. Type `/keep-context` before stepping away (installed with the idle guard): the command is
answered by a hook, without calling the model, and the `PreCompact` hook then blocks the idle summary of that session
only, never an automatic compaction because the context is full. You come back to the whole context and pay its
re-read once, knowingly; the guard does not hold that return back. `/keep-context off` undoes it; marks last 7 days.
So the idle summary can stay on, and can even start earlier (`CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` in
`settings.json` → `env`: 200k by default, lower values are raised to 100k, anything but a whole number is ignored),
while the sessions you need whole are kept. Whether 100k pays off for you: `sessioncoach idle-compact` weighs, from
your own returns after a pause, how often Claude Code's summary really fired, what the 100k-200k returns paid to
re-read their context, and what a summary costs instead (also in sessions you never came back to), and the other way
round what raising it to 400k would lose in the 200k-400k sessions (their whole context re-written); `check` warns
when the variable is ignored, raised, set twice with different values, or turned off by `idleCompaction: false`.

## Privacy and security

- Stores only numbers and ids; a test asserts that transcript text never reaches the database.
- Messages are in English or Spanish, chosen the same way everywhere: the words of the prompt you just typed (hooks
  only, in memory, never stored), then `SESSIONCOACH_LANG`, then `lang` in `alerts.json`, then Claude Code's
  `language` setting (project over user), then `LANG`, else English. A value that is neither es nor en is skipped.
- The resume hook (`SessionStart`, after `/clear`) gives the model the text of your latest handoff file, as if you
  had pasted it, introduced as a file found at that path whose content is facts, not instructions. Handoffs tracked
  by git (from a clone, a pull or a commit) are never loaded. It reads that one file and stores nothing (with the idle guard on, see below). The `/handoff` command is a prompt file; the handoff it
  produces is written by Claude in your project, not by sessioncoach.
- The idle guard (opt-in) keeps, per held-back prompt, the session id, the last turn's time, the transcript path, the
  working dir and the time, in `idle_guard.json` in the data dir (mode 600, at most 50 entries, 48 h); its
  `PreCompact` hook keeps session id, times, transcript path and size in `idle_compact.json`. Never the prompt or any
  text. `/keep-context` keeps only the session id and the time, in `keep_context.json`. After `/clear`, the automatic handoff reads the previous transcript and gives its text (your messages, Claude's
  last reply, file paths, todos, git status) to the new session's model, on this machine: the same content you already
  had there. sessioncoach stores none of it.
- Every warning shown is kept with the numbers that fired it (times, token counts, percentages, session and project
  dir ids, a budget's name) in `alert_events`, for the dashboard's warnings view. Never the prompt or any text.
- The dashboard binds to 127.0.0.1 and rejects foreign `Host` headers (DNS-rebinding guard). Its views open the
  database read-only; `POST /api/refresh` does write to it (an incremental ingest), and the settings panel writes
  the files it shows in its diff.
- 127.0.0.1 does not keep out the other users of the same machine (macOS and Linux share loopback), so the dashboard
  serves nothing without a session cookie. `sessioncoach dashboard` (and `demo`) prints and opens a link with a
  per-start secret, `http://127.0.0.1:8787/?k=<secret>`; opening it sets an `HttpOnly`, `SameSite=Strict` cookie and
  drops the secret from the address bar. Without the cookie the page answers 401 and every `/api/*` call too. The
  link works until the server stops (reopen it in another tab or browser; with `--no-open`, copy it from the
  terminal) and is worthless after a restart. Keep it private. On Linux and Windows, opening the browser passes the
  link on a command line that `ps` shows (for the whole browser session if the browser was not running yet); use
  `--no-open` on a shared machine. The cookie is sent to any port of 127.0.0.1, so do not open pages served by other
  users of the machine in the same browser. Writes also need the per-start `X-Kit-Token`, and a foreign `Origin` is
  refused (settings require it to be present).
- Working directories per session are kept as encoded dir names (like Claude Code's own project dirs; worktree
  names may include branch or ticket names), only to detect sessions that mix projects or companies.
- Data lives in `~/.sessioncoach/` (override with `SESSIONCOACH_DATA_DIR`), outside any repo.

## Development

See `CONTRIBUTING.md` (uv, ruff, coverage ≥ 85 %; standard library only). Tests use synthetic data only and never
touch your `~/.claude`:

```bash
uv sync && uv run ruff check && uv run coverage run -m pytest -q && uv run coverage report
python3 -m unittest discover -s tests      # without uv
```

`scripts/leakcheck.py` checks that no string from a private list kept outside the repo got in
(`SESSIONCOACH_PRIVATE_STRINGS=/path/to/list`); without a list it does nothing.

## Roadmap

- Isolated workspaces for people working with several clients.
- Later: OpenTelemetry export; team view (metrics only); optional Docker image of the dashboard.

## License

Apache License 2.0 (see `LICENSE` and `NOTICE`). Copyright 2026 macurandb.

sessioncoach is an independent project, not affiliated with or endorsed by Anthropic. Claude and Claude Code are trademarks of Anthropic, PBC.

## Windows notes (experimental)

- Claude Code runs the `hooks` and `statusLine` commands through **Git Bash** when it is installed, otherwise **PowerShell**
  ([hooks](https://code.claude.com/docs/en/hooks), [status line → Windows configuration](https://code.claude.com/docs/en/statusline)).
  sessioncoach writes commands that work in both: the Python path with forward slashes and no quotes (8.3 short form if the path
  has spaces). The status line you had before is kept and run with the same shell.
- `sessioncoach check` cannot test POSIX permissions there; it checks that the data directory is inside your user profile.
- Scheduled runs use `pythonw.exe` (no console window).

## Upgrading from kit

An earlier private version was called `kit`. A machine set up under that name keeps working without changes:
`bin/kit` still runs, `KIT_*` variables are still read, the data stays in `~/.kit`, and `sessioncoach check` points
out jobs still installed under the old name. Running `sessioncoach setup` again replaces them (never both at once).
