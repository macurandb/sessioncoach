# The settings panel

The **Settings** button in the dashboard header turns each practice on or off and tunes its
thresholds, with defaults that work out of the box. Pick where it applies: **Global**
(`~/.claude/settings.json`), **Project** (`<project>/.claude/settings.local.json`, kept out of git;
only projects the database knows) or **Company** (`settings.json` in the config dir of an account
from `accounts.json`, see [accounts](accounts-and-workspaces.md)). Practices: resumed-session notice (hook), chained status line, the
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
