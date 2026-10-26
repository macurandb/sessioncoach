# Privacy and security

sessioncoach has no server, no account and no telemetry. Its own network traffic is the dashboard on 127.0.0.1 and,
only if you turn it on, the daily-review email sent by a command you choose.

## What it stores

In `~/.sessioncoach/` (`%USERPROFILE%\.sessioncoach` on Windows; `SESSIONCOACH_DATA_DIR` to change it), outside any
repository, with private permissions:

- `metrics.db`, a SQLite file with **metadata of your sessions, never their content**: token counts, models,
  timestamps, costs; session, request and message ids; Claude Code project dir names (your working dirs, encoded
  like Claude Code's own `~/.claude/projects`; worktree names may include branch or ticket names) and transcript
  file paths; tool names (including MCP server and tool names, never their inputs or outputs) and subagent types;
  the working dirs of each session, to find sessions that mixed projects or companies; per login, a hash of the
  organization id and the plan type (never the email or names); and every warning shown, with the numbers that
  fired it. A test checks that transcript text never reaches the database.
- your configuration: `alerts.json`, and the optional `projects.json` and `accounts.json`;
- `statusline-chain.sh`: a copy of the status line command you had before `statusline-install`, run before
  sessioncoach's own;
- the hooks' small state files, never with any prompt or text: `idle_guard.json` (per held-back prompt: session id,
  last turn's time, transcript path, working dir, time; at most 50 entries for 48 h), `idle_compact.json` (session
  id, times, transcript path and size), `keep_context.json` (session id and time), `resume_loaded.json` (path and
  modification time of each handoff given to a session);
- `logs/` (scheduled runs and alerts) and, on Linux with cron, `backups/` (your crontab before sessioncoach
  changed it). Settings files it changes get a `.bak-*` copy next to them.

## What reaches the model

Two optional hooks add text to a new session's context, so it goes to Claude like anything else you type there:

- the resume hook gives the new session the text of your latest handoff file, introduced as a file found at that
  path whose content is facts, not instructions. Handoffs tracked by git (from a clone, a pull or a commit) are never
  loaded;
- with the idle guard, the automatic handoff after `/clear` carries your last messages, Claude's last reply, file
  paths, the todo list and `git status` of the previous session: content you already had in that session.

sessioncoach stores none of that text. The resume warning is shown to you, never to the model.

## Language

Messages are in English or Spanish, chosen the same way everywhere: the words of the prompt you just typed (hooks
only, in memory, never stored), then `SESSIONCOACH_LANG`, then `lang` in `alerts.json`, then Claude Code's
`language` setting (project over user), then `LANG`, else English.

## The dashboard

- It binds to 127.0.0.1 and rejects foreign `Host` headers (DNS-rebinding guard). Its views open the database
  read-only; `POST /api/refresh` writes to it (an incremental ingest), and the settings panel writes the files it
  shows in its diff.
- 127.0.0.1 does not keep out the other users of the same machine, so the dashboard serves nothing without a session
  cookie. `sessioncoach dashboard` (and `demo`) prints and opens a link with a per-start secret,
  `http://127.0.0.1:8787/?k=<secret>`; opening it sets an `HttpOnly`, `SameSite=Strict` cookie and drops the secret
  from the address bar. Without the cookie the page and every `/api/*` call answer 401. The link works until the
  server stops and is worthless after a restart. Keep it private.
- On Linux and Windows, opening the browser passes the link on a command line that `ps` shows; use `--no-open` on a
  shared machine. The cookie is sent to any port of 127.0.0.1, so do not open pages served by other users of the
  machine in the same browser.
- Writes also need a per-start token (`X-Kit-Token`, embedded only in the page served with the cookie); a foreign
  `Origin` is refused. Bodies are JSON only, size-limited and strictly validated, and the client never sends paths.

To report a vulnerability, see [SECURITY.md](../SECURITY.md).
