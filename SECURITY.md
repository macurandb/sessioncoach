# Security

sessioncoach writes to `~/.claude` (hooks, status line, commands, the "when to cut" block in `CLAUDE.md`), schedules
jobs with your system's scheduler and serves a dashboard on 127.0.0.1 with a write API for its settings panel. A bug
in any of these can matter, so please report it privately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: the **Security** tab of
[macurandb/sessioncoach](https://github.com/macurandb/sessioncoach/security), then **Report a vulnerability**.
Please do not open a public issue. Include the version (`sessioncoach version`), your OS and the steps to reproduce.

You get an answer within 7 days. A fix is released as soon as it is ready, and the report is credited unless you
prefer otherwise.

## Supported versions

Only the latest release gets fixes while the project is in 0.x.

## Scope

In scope: anything that lets another local user, a web page or a crafted transcript read your data, change your
Claude Code settings, run commands or reach the dashboard's write API. The privacy promises in the README (only
numbers and ids are stored; nothing is sent anywhere unless you turn on the daily-review email) are in scope too.
