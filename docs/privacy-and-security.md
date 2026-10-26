# Privacy and security

sessioncoach has no server, no account and no telemetry. Its own network traffic is the dashboard on 127.0.0.1 and,
only if you turn it on, the daily-review email sent by a command you choose.

## What it stores

In `~/.sessioncoach/` (`%USERPROFILE%\.sessioncoach` on Windows; `~/.kit` on machines set up under the old name;
`SESSIONCOACH_DATA_DIR` to change it), outside any repository. On macOS and Linux the directory and its files are
private to your user (mode 700/600); on Windows they rely on your user profile's permissions.

`metrics.db`, a SQLite file, holds **metadata of your sessions, never their content** (a test checks that transcript
text never reaches it):

- per request: token counts by type, model, time; session, request and message ids;
- Claude Code project dir names (your working dirs, encoded like Claude Code's own `~/.claude/projects`; worktree
  names may include branch or ticket names), transcript file paths and the transcript root they came from;
- Claude Code's own cost counter per session, and the cost slices computed from it;
- tool names (including MCP server and tool names, never their inputs or outputs); subagent types, models, token
  counts, tool-use counts and durations;
- the working dirs of each session, to find sessions that mixed projects or companies;
- per login, a hash of the organization id (or of the account id for a personal login), the plan type, seat and
  rate tiers and whether paid extra usage is on; never the email or names;
- plan-limit samples from the status line (5-hour, weekly and context percentages, reset times, model);
- compactions (time, trigger, tokens before and after);
- every warning shown, with the numbers that fired it (times, token counts, percentages, session and project dir
  ids, a budget's name from your `alerts.json`), and which alerts were already sent;
- bookkeeping: each ingest's counts, and each transcript's size and modification time.

Next to it:

- your configuration: `alerts.json`, and the optional `projects.json` and `accounts.json`;
- `statusline-chain.sh` (or one per scope under `chains/`): a copy of the status line command you had before
  `statusline-install`, run before sessioncoach's own;
- the hooks' small state files, never with any prompt or text: `idle_guard.json` (per held-back prompt: session id,
  last turn's time, transcript path, working dir, time; at most 50 entries for 48 h), `idle_compact.json` (session
  id, times, transcript path and size), `keep_context.json` (session id and time), `resume_loaded.json` (path and
  modification time of each handoff given to a session);
- `logs/` (scheduled runs and alerts); on Linux with cron, `backups/` (your crontab before sessioncoach changed it);
  on Windows, `scheduler/` (the scheduled tasks' definitions); `demo/` if you ran `sessioncoach demo` (synthetic data
  only); and side files: `*.lock`, SQLite's `-wal`/`-shm`, `.bak-*` copies of files sessioncoach changed.

## What reaches the model

Two optional hooks add text to a new session's context, so it goes to Claude like anything else you type there:

- the resume hook gives the new session the text of your latest handoff file, introduced as a file found at that
  path whose content is facts, not instructions. Handoffs tracked by git (from a clone, a pull or a commit) are never
  loaded;
- with the idle guard, the automatic handoff after `/clear` carries Claude Code's compaction summary when there is
  one, your last messages, Claude's last reply, file
  paths, the todo list and `git status` of the previous session: content you already had in that session.

sessioncoach stores none of that text. The resume warning is shown to you, never to the model.

## Language

Messages are in English or Spanish, chosen the same way everywhere: the words of the prompt you just typed (hooks
only, in memory, never stored), then `SESSIONCOACH_LANG`, then `lang` in `alerts.json`, then Claude Code's
`language` setting (project over user), then `LANG`, else English. The short header that introduces a handoff to
the model is always in English.

## The dashboard

- It binds to 127.0.0.1 and rejects foreign `Host` headers (DNS-rebinding guard). Its views open the database
  read-only; `POST /api/refresh` writes to it (an incremental ingest), and the settings panel writes the files it
  shows in its diff.
- 127.0.0.1 does not keep out the other users of the same machine, so the dashboard serves nothing without a session
  cookie. `sessioncoach dashboard` (and `demo`) prints and opens a link with a per-start secret,
  `http://127.0.0.1:8787/?k=<secret>`; opening it sets an `HttpOnly`, `SameSite=Strict` cookie and drops the secret
  from the address bar. Without the cookie the page and every `/api/*` call answer 401. The link works until the
  server stops and is worthless after a restart. Keep it private.
- Opening the browser passes the link on a command line that `ps` shows; use `--no-open` on a shared machine. The cookie is sent to any port of 127.0.0.1, so do not open pages served by other users of the
  machine in the same browser.
- Writes also need a per-start token (`X-Kit-Token`, embedded only in the page served with the cookie); a foreign
  `Origin` is refused. Bodies are JSON only, size-limited and strictly validated. The server decides which files
  a change touches: the paths in an apply request must match the plan it computed itself, or the request is
  refused.

To report a vulnerability, see [SECURITY.md](../SECURITY.md).
