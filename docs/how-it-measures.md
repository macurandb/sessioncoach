# How sessioncoach measures

## What you get

- **Cost per day, week, project, company, account and model.** API-equivalent at list price, split into *recorded*
  (Claude Code's own `cost-state`, spread over time by activity) and *estimated* (list price, for activity no
  `cost-state` covers). On subscription plans this is not an invoice.
- **Where the money goes**: cache reads, cache writes, output, input; 1M-context variants; subagents.
- **Recommendations** from deterministic rules over your data:
  - **R1** resuming sessions after the prompt cache expired (big cache rewrites);
  - **R2** heavy use of 1M-context variants;
  - **R3** general-purpose subagents running on Opus;
  - **R4** subagents' share of the cost;
  - **R16** long sessions: turns re-reading more than `review.long_context_tokens` of context (300k by default).

  The daily review uses the same criteria and the same threshold.
- **Data quality, shown next to the numbers**: a backtest (recorded vs list-price estimate per interval, 0.8–1.25)
  and a conservation check. Every figure has the command that reproduces it.
- Dashboard in English and Spanish, light and dark, works on a phone screen.

## How the cost is computed

Claude Code writes cumulative `cost-state` snapshots per session. Each increase is spread over the requests made
since the previous snapshot, in proportion to their list-price weight. The counter only covers the current process,
so when an interval holds more activity than its increase explains, the increase goes to the most recent activity it
covers and the older activity is estimated at list price instead of being lost. A counter drop (a resumed session)
counts from zero. Subagent totals come from the completion reports the parent receives, because subagent transcripts
under-count output tokens.

## What each figure means

- **Dollars**: API-equivalent. Recorded figures come from Claude Code's own counter, estimated ones from list prices;
  both are what the same tokens would cost on the API. On a Pro or Max plan, compare them with each other (which
  habit burns more of your limits), not with your invoice.
- **Cache percentage**: share of the *list-price* spend over *all* activity in the window that goes to the prompt
  cache (reads + writes). It is not computed from `cost-state` (which has no cost per token type), so do not compare
  it with a figure built from recorded cost only.
- **Cache lifetime (TTL)**: "cache expired" uses the TTL each transcript really wrote (`ephemeral_1h` = 1 h,
  `ephemeral_5m` = 5 min: API keys, usage credits, subagents). With no data, 1 h. R1, the daily review, the
  plan-window breakdown and the hook all use it; `nudge.idle_min` in `alerts.json`, if you set it, wins.
- **Prices**: list prices from platform.claude.com/docs/en/about-claude/pricing (table date in
  `sessioncoach/pricing.py`). A model not in the table is priced like the newest model of its family and listed as
  *without exact price* (requests and USD affected) in `sessioncoach verify`, `sessioncoach report` and the
  dashboard's "Data quality"; it is never silent.

## Reading transcripts

`ingest` reads only the transcripts that are new or changed since the last run (it remembers each file's size and
modification time); a file that grew is read again from its start. The result is the same as reading everything.
`sessioncoach ingest --full` reads every transcript again (use it if you edited the database by hand, or if a
transcript was restored with an older modification time). Claude Code's transcript format is internal and
undocumented; `sessioncoach verify` shows how much of it sessioncoach could read.

## Demo and anonymous mode

`sessioncoach demo [--port 8787] [--no-open] [--days 30] [--seed 7]` generates fictional Claude Code transcripts
(companies acme, globex and initech; long sessions, resumes after the cache expired on 1 h and 5 min caches,
subagents on Opus, sessions that mix projects, plan limits, budgets, several accounts) under `<data dir>/demo/`, runs
them through the real pipeline and opens the dashboard on them with a "Demo data" banner. Your own data, `~/.claude`
and the schedulers are not touched. Same seed and day, same data.

For screenshots of your own data, add `&anon=1` to the dashboard link or press **Anonymous**: projects, workspaces,
accounts and budgets become "Project A", "Workspace 1", "Account 1"… (ordered by spend, consistent across views) and
the database path is hidden. Check your screenshots anyway.
