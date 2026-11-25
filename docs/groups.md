# Groups (clients)

A **group** is a client — Acme, Initech, whoever you work for. It is the tenant that
`projects.json` rules already carried: the second half of `Labeler`'s `(project, tenant)`, which the
dashboard, budgets and the daily review already use. Groups do not add a second mechanism; they
name and order that existing tenant, and let the Herdr panel and `live --json` split today's, the
5-hour and the weekly spend by client.

A group is only ever a label over a tenant id, kept in the `"groups"` block of
`<data dir>/projects.json`:

```json
{
  "groups": {"acme": {"label": "Acme"}, "initech": {"label": "Initech"}},
  "rules": [
    {"match": "-Users-me-code-acme-shop", "tenant": "acme", "source": "cli"},
    {"match": "-Users-me-code-initech-etl", "tenant": "initech", "source": "herdr"}
  ]
}
```

Group ids are slugs (`[a-z0-9-]`); a label without an explicit id gets its slug. Insertion order is
the display order. A tenant used by a rule but absent from `"groups"` shows with its id as its label.

## The `sessioncoach group` command

```bash
sessioncoach group list                        # each group, its rules, and today / 7 / 30 day spend
sessioncoach group add "Acme" [--id acme]  # name a client (id defaults to the slug)
sessioncoach group assign acme ~/work/acme # a folder -> rules for itself and its subfolders
sessioncoach group assign acme "-Users-me-code-*"  # ... or an fnmatch pattern over the dir name
sessioncoach group unassign ~/work/acme    # drop a folder's (or a pattern's) rules
sessioncoach group rename acme "Acme GmbH" # change the display label
sessioncoach group remove acme [--force]   # refuse while rules use it, unless --force
sessioncoach group sync-herdr --json FILE|-    # feed the herdr-space-groups assignments in
```

- `assign` turns a folder into **two** rules — `encode_dir(abspath)` and `encode_dir(abspath) + "-*"`
  — so the folder and everything under it belong to the group. New rules go **first** (first match
  wins), and any existing rule with the same `match` is replaced. A pattern is one rule.
- Rules written by the CLI carry `"source": "cli"`; `"herdr"` marks a rule the Herdr panel fed in;
  an absent `"source"` means the rule was written by hand ("manual").

`group list` reads `cost_slices` through the same `Labeler` the dashboard uses, so its numbers are
the API-equivalent figure, not an invoice. The `unassigned` line names the five largest workspaces
that belong to no group, by their 30-day spend.

## Where Herdr groups feed it

The community mod `herdr-space-groups` keeps the client each space belongs to in Herdr's shared key
`"space-groups"` (`{groups: [...], spaces: {<space id>: <group id>}}`). Any tool that turns that key
into the `sync-herdr` input — one `{name, folders}` per group, from the local spaces only — can run
`sessioncoach group sync-herdr` with it; the sessioncoach Herdr panel will do it on its own in a
coming version.

`sync-herdr` replaces **only** the rules with `"source": "herdr"`; rules you wrote by hand or with
the CLI are never touched. It creates a missing group (label = name; a nested name like
`Clients/Acme` uses its leaf `Acme`), skips the home folder, and is idempotent: the same input
changes nothing, so the file is not rewritten. The **same leaf name** — `Clients/Acme` and
`Other/Acme` — is the same client and merges into one group; two different names whose slug
collides (`Acme` vs `Acme!`) get `-2`, `-3`… A group keeps its id across syncs: the leaf it
came from is stored as `herdr_name`, and a later feed resolves a name to an existing group by that
`herdr_name` (then by label) before minting a new slug. See [docs/herdr.md](herdr.md) for
installing `herdr-space-groups`.

`sync-herdr` validates the whole input before touching the file: `groups` must be a list (at most
200) of objects with a `name` (non-empty, at most 100 characters) and `folders` (a list of absolute
paths, at most 4096 characters each, at most 1000 per group). Anything else exits 2 with the file
untouched; `{"groups": []}` is valid and removes every `source: "herdr"` rule.

## How the file is written

Every mutation (the CLI and the panel both write this file) holds the operating system's exclusive
lock on `projects.json.lock` (flock, or msvcrt on Windows) so two writers never lose each other's
changes, then writes atomically (temp file + `os.replace`). A writer that dies releases the lock with
its process, so a leftover `projects.json.lock` file blocks nothing; a lock another writer holds for
more than 5 s makes the command stop with a clear error (exit 1). A `projects.json` that exists but is not a JSON object is
never overwritten: mutations refuse with "projects.json is not valid JSON; fix or move it" (exit 1),
while reading for display still works.

## Who uses groups

- **`live --json`** — the `"groups"` list, and each session's `"group"` key
  ([docs/live-json.md](live-json.md)).
- The dashboard, budgets and the daily review already used the tenant; naming it in `"groups"` only
  gives it a display label.
