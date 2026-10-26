# Projects, companies and Claude accounts

Everything here is optional. Without it, sessioncoach groups cost by Claude Code project dir and shows each login
it finds with an automatic label such as "Max · 20x".

## Projects and companies (`projects.json`)

Map Claude Code project dirs to projects and companies ("tenants": one per client, for instance), or merge git
worktrees into one project, in the private file `~/.sessioncoach/projects.json`. The first rule that matches wins:

```json
{"rules": [
  {"match": "*code-shop*", "project": "shop", "tenant": "client-a"},
  {"match": "*", "tenant": "personal"}
]}
```

`match` is a shell-style pattern over Claude Code's project dir name (the working dir with `/` and other symbols turned into
`-`, as in `~/.claude/projects`). The dashboard, `report --tenant` and `day` then show cost per project and company, and the
daily review flags sessions that mixed companies.

## Several Claude accounts

Claude Code keeps one login per config dir: `~/.claude` by default, or the dir in `CLAUDE_CONFIG_DIR`. Each config dir
has its own login, settings and transcripts. sessioncoach reads, from each config dir's `.claude.json`, a hash of the
organization id and the plan type, never the email or names, and splits cost by account. A config dir that switches
login (`/login`) is split at the switch, as seen by the next ingest.

To name accounts and tie each one to a company, create `~/.sessioncoach/accounts.json`:

```json
{"accounts": {
  "personal":  {"label": "Personal · Max", "company": "personal", "org_hash": "ab12cd34ef56",
                "monthly_fee_usd": 100},
  "team-acme": {"label": "Team Acme", "company": "acme", "org_hash": "0f1e2d3c4b5a",
                "roots": ["~/.claude-acme/projects"]}
}}
```

- `org_hash`: the first 12 hex characters of the SHA-256 of the organization id of that login. To print it for a
  config dir (here the default one; use `$CLAUDE_CONFIG_DIR/.claude.json` for another):

  ```bash
  python3 -c "import hashlib,json,os; a=json.load(open(os.path.expanduser('~/.claude.json')))['oauthAccount']; print(hashlib.sha256((a.get('organizationUuid') or a['accountUuid']).encode()).hexdigest()[:12])"
  ```

- `company`: the `tenant` of `projects.json` this account is meant for. Work of another company on it shows as a
  mismatch in the dashboard.
- `roots`: the `projects/` dir of each extra config dir. They are always read by `ingest`, scheduled runs included,
  so you do not need `SESSIONCOACH_PROJECTS_DIRS`.
- `monthly_fee_usd`: what the plan costs you, shown next to the API-equivalent figures.
- `transition`: work of another company that runs on this account on purpose until a date, reported as
  acknowledged instead of as a mismatch: `"transition": [{"tenant": "acme", "until": "2026-12-31"}]`.

`SESSIONCOACH_PROJECTS_DIRS` (paths separated by `:`, or `;` on Windows) replaces the default transcript root for one
run, for instance `SESSIONCOACH_PROJECTS_DIRS=~/.claude-a/projects:~/.claude-b/projects sessioncoach ingest`.

### One account per project

Claude Code chooses the account when it starts, from `CLAUDE_CONFIG_DIR`, so a command inside a session cannot switch
it. To keep one company's projects on its own account:

1. Log in to that account in its own config dir:
   `CLAUDE_CONFIG_DIR=~/.claude-acme claude`, then `/login`.
2. Start Claude Code with that `CLAUDE_CONFIG_DIR` in those projects: a shell alias or a tool such as
   [direnv](https://direnv.net) in the terminal. In VS Code, `claudeCode.environmentVariables` applies to every
   window (it is a machine setting), and `claudeCode.claudeProcessWrapper` can point to a script that sets
   `CLAUDE_CONFIG_DIR` from the working dir. A project's `.claude/settings.json` cannot set it.
3. Check which account a project gets: `claude auth status` there shows `configDirectory` and `orgName`.
4. Add the config dir's `projects/` to `roots` in `accounts.json`, and install sessioncoach's hooks there too:
   `CLAUDE_CONFIG_DIR=~/.claude-acme sessioncoach setup --scope company` (a config dir does not load
   `~/.claude/settings.json`).
