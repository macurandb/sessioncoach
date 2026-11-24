# Groups (clients)

A **group** is a client — Pretty, ITTI, DAF, whoever you work for. It is the tenant that
`projects.json` rules already carried: the second half of `Labeler`'s `(project, tenant)`, which the
dashboard, budgets and the daily review already use. Groups do not add a second mechanism; they
name and order that existing tenant, and let the Herdr panel and `live --json` split today's, the
5-hour and the weekly spend by client.

A group is only ever a label over a tenant id, kept in the `"groups"` block of
`<data dir>/projects.json`:

```json
{
  "groups": {"pretty": {"label": "Pretty"}, "daf": {"label": "DAF"}},
  "rules": [
    {"match": "-Users-me-code-pretty-shop", "tenant": "pretty", "source": "cli"},
    {"match": "-Users-me-code-daf-etl", "tenant": "daf", "source": "herdr"}
  ]
}
```

Group ids are slugs (`[a-z0-9-]`); a label without an explicit id gets its slug. Insertion order is
the display order. A tenant used by a rule but absent from `"groups"` shows with its id as its label.

## The `sessioncoach group` command

```bash
sessioncoach group list                        # each group, its rules, and today / 7 / 30 day spend
sessioncoach group add "Pretty" [--id pretty]  # name a client (id defaults to the slug)
sessioncoach group assign pretty ~/work/pretty # a folder -> rules for itself and its subfolders
sessioncoach group assign pretty "-Users-me-code-*"  # ... or an fnmatch pattern over the dir name
sessioncoach group unassign ~/work/pretty      # drop a folder's (or a pattern's) rules
sessioncoach group rename pretty "Pretty GmbH" # change the display label
sessioncoach group remove pretty [--force]     # refuse while rules use it, unless --force
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
`"space-groups"` (`{groups: [...], spaces: {<space id>: <group id>}}`). The sessioncoach panel reads
that key, turns it into the `sync-herdr` input — one `{name, folders}` per group, from the local
spaces only — and runs `sessioncoach group sync-herdr` with it.

`sync-herdr` replaces **only** the rules with `"source": "herdr"`; rules you wrote by hand or with
the CLI are never touched. It creates a missing group (label = name; a nested name like
`Clients/Pretty` uses its leaf `Pretty`), skips empty or home folders, and is idempotent: the same
input changes nothing, so the file is not rewritten. See [docs/herdr.md](herdr.md) for installing
`herdr-space-groups`.

## Who uses groups

- **The Herdr panel** — one row per group: today, 5 hours and the week.
- **`live --json`** — the `"groups"` list, and each session's `"group"` key
  ([docs/live-json.md](live-json.md)).
- The dashboard, budgets and the daily review already used the tenant; naming it in `"groups"` only
  gives it a display label.
