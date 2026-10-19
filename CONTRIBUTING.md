# Contributing

## Setup

The package itself has **no runtime dependencies** (standard library only, Python 3.9+) and must keep it that way.
Development tools are managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync                              # creates .venv with pytest, coverage and ruff (versions in uv.lock)
uv run ruff check                    # lint: must pass with zero errors
uv run coverage run -m pytest -q     # tests
uv run coverage report               # coverage: fails under 85 %
```

Add a development tool with `uv add --dev <tool>` and commit `uv.lock`. Never add a runtime dependency.

Without uv, from the repo root (no install needed; `pythonpath` in `pyproject.toml` finds the package):

```bash
python3 -m unittest discover -s tests   # standard library only
python3 -m pytest                       # or `uvx pytest`, if pytest is available
```

## Rules for every change

1. **Coverage ≥ 85 %** of the `sessioncoach` package, branches included (`[tool.coverage]` in `pyproject.toml`).
   CI fails below it. A change that lowers coverage brings its tests. `# pragma: no cover` is only for
   `__main__` and code that runs on one platform only; never to raise the figure.
2. **Lint clean**: `uv run ruff check` with the rules in `pyproject.toml`. An ignored rule there carries its reason;
   a `# noqa` carries its reason on the same line.
3. **Tests never touch the real machine**: temporary HOME and data dir, a fake scheduler, no real
   `~/.claude`, `~/.kit`/`~/.sessioncoach`, LaunchAgents, timers, crontab or scheduled tasks.
   `tests/isolate.py` (imported first by every test module) sets `KIT_SCHEDULER=none`, drops
   `XDG_CONFIG_HOME` and fails any test that runs a real `schtasks`, `crontab`, `systemctl` or `launchctl`:
   a test that needs a scheduler forces it and passes a fake (`run=` or `scheduler._run`).
4. **Backward compatibility**: whatever an earlier version installed (hooks, status line, scheduled jobs,
   markers, data dir, environment variables) keeps working and is recognised. Pre-existing tests are not
   changed to make a change pass, unless the commit says why.
5. **Privacy**: only numbers and ids are stored; transcript text never reaches the database. Run
   `scripts/leakcheck.py` before every push (maintainers keep the private list outside the repo).
6. One cohesive change per commit.

## Known design debt

Ruff's size/complexity rules (`PLR09xx`) are switched off for now because some functions are too long or take
too many arguments. They are refactors to plan, not to hide: a pull request that splits one of them is welcome.

Already done: one hook registry (`hooks.py`), locked state files (`statefile.py`), one rules table (`rules.py`), one
class per scheduler, `config.py`, and `setup`, the settings panel's plan and `recommendations` split into steps
(average cyclomatic complexity B, 5.3, by `uvx radon cc -a sessioncoach`). Still above 30, the next ones to split:
`settings.validate_alerts` (49), `analysis.idle_compact_view` (45), `limits.record_and_alert` (44),
`limits.evaluate_budgets` (41), `collect.scan` (39), `installer.check` (38), `digest.read` (31).

Any refactor keeps the behaviour: compare the CLI and API outputs on `sessioncoach demo` data before and after,
not only the tests. The dashboard and the daily review share their criteria and thresholds (`rules.py`), and every
message picks its language with `config.lang`: keep it that way rather than adding a second value or order.
