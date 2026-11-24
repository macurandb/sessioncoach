# The Herdr panel

A small panel for [Herdr](https://herdr.dev) that shows the Claude Code sessions running right now —
their context size, how long their cache has left and today's cost, plus the newest alerts — from
`sessioncoach live --json`.

![The Herdr panel](img/herdr-panel.png)

## What it shows

Top to bottom:

- **One card per account.** The header names the account and its plan (`Personal · Max`), with a
  status dot — green under 75 %, amber under 90 %, red at or above — from the higher of the two
  windows, and a chevron that folds the card (the fold state is kept per account, in the shared key
  `sessioncoach.folded`). Unfolded, each window shows a 10-cell bar, the percentage, when it resets
  (5 h as `HH:MM`, 7 d as `ddd HH:MM`), and a line under it — `$18 in this window`, or
  `$1,535 this week · at this pace 78 %` — then `Today $84 · 11 sessions`, then that account's
  sessions (the active space's first, each ordered by recent activity).
- **Groups.** One row per client: its label and today, 5 h and week as three right-aligned columns.
- **Recent alerts.** The last five alert events, newest first.

A session row shows the project, context (`335k`), how long its cache has left (`42 min`, or
`expired`), and today's spend (`$12.40`); past your context threshold it adds a muted
`⚠ /compact or /handoff` line. Click a row to focus its terminal. With no Claude Code session
running, the panel says so; with no data yet it is empty.

## Install

```bash
sessioncoach herdr-install
```

It copies three files into `<app home>/mods/sessioncoach/` and writes a `config.json` next to them.
The app home is `$HERDR_APP_HOME` if set, otherwise `~/.config/herdr-app`; `--app-home DIR` overrides.
Herdr docks the panel on the right the next time it starts and finds the new folder. To open it
right away, or again after closing it, split it next to the current pane:

```bash
herdr-app do pane.split '{"direction":"right","mod":"sessioncoach"}'
```

It is a pane like any other: drag, resize or close it. There is no tab-bar button (Herdr's tab bar
only has Files and Changes).

```bash
sessioncoach herdr-uninstall
```

removes only what the installer put there — the panel's files, its `config.json` and its manifest —
and the folder, if nothing else is left. Re-running `herdr-install` is safe: it refreshes the
managed keys and keeps yours, and refuses (unless `--force`) to overwrite a panel that was edited or
not installed by sessioncoach.

`sessioncoach check` reports the panel too: OK when the files match this version, and a warning to
re-run `herdr-install` when they are outdated or missing.

## config.json

The installer writes the keys the panel needs and keeps yours:

- `command` — the `sessioncoach` console script; where there is none, your Python interpreter with
  `args_prefix: ["-m", "sessioncoach"]` prepended.
- `refresh_s` — seconds between polls (default 30).
- `toasts` — whether new warnings post a Herdr notification (default true).
- `hint_min_tokens` — the context size at which the `/compact or /handoff` hint appears: the lowest
  `context_tokens` threshold in `alerts.json`, or 300000.
- `args_extra` — extra arguments appended to the `live` command, e.g. `["--demo"]` to show the demo
  data instead of your own.
- `sync_herdr_groups` — whether the panel feeds the `herdr-space-groups` assignments back into
  sessioncoach (default true).

The panel runs `sessioncoach live --json`, with `--since <cursor>` after the first poll. A non-zero
exit, bad JSON or a `version` other than 1 shows a one-line error and keeps the last good data.

## Groups and sync

If you also run the community mod `herdr-space-groups`, it keeps the client each space belongs to in
Herdr's shared key `"space-groups"`. On each poll the panel turns that into the `group sync-herdr`
input — one `{name, folders}` per group, from the local spaces only — and, when it differs from what
it sent before, stages it in `<app home>/mods/sessioncoach/.groups-sync.json` and runs
`sessioncoach group sync-herdr --json <file>` (the `exec` helper has no stdin, hence the file).
Nothing is shown on success; a failure shows a one-line error. The panel never syncs while
`herdr-app check` runs.

Install `herdr-space-groups` the way you install any Herdr mod — copy its folder into
`<app home>/mods/` and let Herdr dock it — then assign spaces to clients in it. `group sync-herdr`
replaces only the rules the panel sourced; rules you wrote by hand or with `sessioncoach group
assign` are never touched (see [docs/groups.md](groups.md)).

## Toasts

Each **new** event with level `warning` posts a notification (when `toasts` is on): plan limits at
90 %+, pace alerts, cache expiry and context. Clicking a toast for a session that is still linked to
a terminal focuses that terminal. Limits are toasted only through their events — never a second
time. Several copies of the panel (another layout, a clone) never toast the same event twice: they
share the highest toasted id in one shared key. The cursor is kept in memory only, so the first poll
after each start lists the backlog without toasting it.

## How a session is linked to a terminal

Herdr's `terminals()` does not carry a process id, so the panel reads each agent's terminal with
`hc get <address> --json` and matches its `foreground.pid` to the session's `pid`. When two Claude
tabs share a repo, the pid tells them apart; a session is only linked by its working directory when
exactly one agent has that directory. Sessions that cannot be linked are shown but not clickable.

## Privacy

The panel is local only. It runs `sessioncoach live --json`, reads the panel's own `config.json`,
and writes one shared key (`sessioncoach.toasted`). No network, no other file writes. `live` reads
only session pids, working directories and usage numbers — never transcript text; see
[docs/live-json.md](live-json.md).

## Copying the mod by hand

If you don't use the installer (another machine, a shared home), copy the folder
`sessioncoach/herdr_mod/` from this package into `<app home>/mods/sessioncoach/` and write a
`config.json` yourself, for example:

```json
{ "command": "sessioncoach", "refresh_s": 30, "toasts": true, "hint_min_tokens": 300000 }
```

## Troubleshooting

- **The panel is empty.** No Claude Code session is running, or the first poll hasn't finished yet.
- **`sessioncoach not found`.** The `command` in `config.json` no longer starts: re-run
  `sessioncoach herdr-install`.
- **I closed the panel.** Open it again with
  `herdr-app do pane.split '{"direction":"right","mod":"sessioncoach"}'`.
