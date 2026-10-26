# Handoffs, the idle guard and /keep-context

Coming back to a big session after a long pause is the most expensive moment in Claude Code. These pieces let you
choose what to pay before you pay it. All of them are optional; `sessioncoach setup` asks about each one, and
`sessioncoach check` shows which are installed.

## What a pause costs

The prompt cache of a session lives 1 hour (5 minutes with an API key, usage credits and in subagents). After that,
the next message writes the whole context to the cache again. With 500k tokens of context on Opus 5.5, at list price:

| What you do after the cache expired | What it costs |
|---|---|
| Send a message in the same session | ≈ $4 once (cache write), then ≈ $0.10 per message to read 500k of context, which keeps growing |
| `/handoff`, then `/clear` | ≈ $4 once (the `/handoff` turn writes the cache too), then a small clean session |
| `/clear` with the idle guard's automatic handoff | $0: the handoff is built from the transcript, with no model |
| `/handoff` **before** the cache expires | ≈ $0.10: the cache is still warm (the cache-expiry warning gives you that chance) |

Figures are API-equivalent (see the README): on a Pro or Max plan, read them as how much of your plan's limits a
habit burns.

## `/handoff [topic]`

A Claude Code command (`sessioncoach handoff-install`, or the question in `setup`). Claude writes
`docs/HANDOFF-<date>-<topic>.md` at the root of the project (goal, state, decisions, next steps, files touched, how
to verify; in the language of the conversation; one per project if the session touched several) and ends by telling
you to type `/clear`.

The command file goes to `commands/handoff.md` next to the settings of the scope `setup` uses (`~/.claude`, the
project's `.claude`, which git tracks unless you ignore it, or `$CLAUDE_CONFIG_DIR`; `setup --yes` never writes it
into a project). It carries a `sessioncoach:handoff` marker: a `handoff.md` of your own is never overwritten or
removed.

## Loading the handoff after `/clear`

A `SessionStart` hook (`sessioncoach resume-hook-install`, or the question in `setup`), installed with no matcher:
the hook itself decides from the session's `source`.

- **CLI `/clear`** (source `clear`): the new session gets the path and text of the newest `docs/HANDOFF-*.md` of the
  project (the working dir up to the repository root) written in the last 24 hours.
- **VS Code extension and desktop app** (`/clear` there starts a new session, source `startup`): the same, but only
  for a handoff written in the last 30 minutes that no session got yet, so `/handoff` then `/clear` works there too
  and a window you open later never gets an old handoff. Claude Code does not tell a `/clear` from a new window, so
  any new session of the project in those 30 minutes gets it (once): open the new session from the one that wrote
  it.
- **`resume` and `compact`**: nothing; the context is still there.

The text is cut to 9,000 characters (Claude Code caps hook context at 10,000; the path is there for the rest) and
introduced as a file found at that path whose content is facts, not instructions. A handoff that git tracks is never
loaded: a clone or pull gives it a fresh date, so it may not be yours. The hook notes which handoffs it gave (path and
modification time, in `resume_loaded.json` in the data dir) and nothing else.

Without the hook, `/handoff` ends with the line to type after `/clear` ("read docs/HANDOFF-… and continue");
installing or removing the hook rewrites an installed `/handoff` to match.

With `/handoff` installed, the resume warning and the cache-expiry notification say "type /handoff". The
notification only sees the command in your user settings (`~/.claude/commands` or `$CLAUDE_CONFIG_DIR/commands`):
the ingest that sends it does not know the session's working dir.

Untested idea: switching to a cheaper model (`/model`) for the `/handoff` turn alone, if the context fits its window.

## The idle guard (opt-in)

The resume warning comes after the fact: the prompt is sent anyway. With the idle guard on
(`sessioncoach idle-guard-on`, or the last question in `setup`; never turned on by `setup --yes`), the first prompt
after a long pause is held back instead (the hook answers `{"decision": "block"}`): it is not sent, nothing is spent,
and Claude Code shows why, with your text under "Original prompt:" (in the terminal it also goes back to the input
box).

It holds back only pauses of 60 minutes or more (`nudge.block_min` in `alerts.json`) in sessions with at least 100k
tokens of context (`nudge.min_context_tokens`), so a 5-minute cache does not block you on every coffee break: shorter
pauses past a 5-minute cache get the plain warning. Then:

- **send it again** to go on in that session: the same pause goes through, with no second warning;
- **`/clear`** to start clean for free: with the resume hook installed, the new session gets an **automatic
  handoff** built from the previous session's transcript, with no model: Claude Code's own compaction summary when
  there is one, your last 8 messages, Claude's last reply, the files edited, the latest todo list, and the branch and
  `git status --short` of the working dir (at most 9,000 characters). Only within 30 minutes of the held-back prompt
  and in the same project; not when another session of the project is also held back or was active since, because
  the `/clear` may have come from that window. A `docs/HANDOFF-*.md` written after the prompt was held back wins;
- **`/handoff`** (when installed) for a better handoff: it writes the cache once, and is never held back.

Limits: the automatic handoff has the facts, not the reasons, so it is worse than `/handoff`. Claude Code shows no
Yes/No dialog for a held-back prompt: sending it again is the "yes".

When Claude Code already prepared its own summary of the paused session (its idle compaction, about 54 minutes after
the last turn, with the app open and the machine awake), coming back does not write the whole context again: a
`PreCompact` hook (matcher `auto`, added by `idle-guard-on`) notes it, and the prompt is then neither held back nor
warned about, only told that the summary will be applied. If that summary later fails, that return goes unwarned.

`idle-guard-off` and `uninstall --all` turn it off and remove its hook and state files.

## `/keep-context`

Claude Code's idle summary makes coming back cheap but drops detail. Type `/keep-context` before stepping away
(installed with the idle guard): a hook answers it without calling the model, and the `PreCompact` hook then blocks
the idle summary of that session only, never an automatic compaction because the context is full. You come back to
the whole context and pay its cache write once, knowingly; the guard does not hold that return back.
`/keep-context off` undoes it; marks last 7 days.

## Should the idle summary start earlier?

`CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` in `settings.json` → `env` sets the context size from which Claude Code
prepares its idle summary (200k by default; lower values are raised to 100k; anything but a whole number is
ignored). `sessioncoach idle-compact` weighs, from your own returns after a pause, how often the summary really
fired, what the 100k–200k returns paid to write their context again and what a summary costs instead, and what
raising it to 400k would lose. `sessioncoach check` warns when the variable is ignored, set twice with different
values, or turned off by `idleCompaction: false`.
