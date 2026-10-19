# sessioncoach — rules for agents working in this repo

Read `CONTRIBUTING.md` first; its rules are mandatory. In short:

- Tooling is **uv**: `uv sync`, `uv run ruff check`, `uv run coverage run -m pytest -q && uv run coverage report`.
- **Coverage of the `sessioncoach` package must stay ≥ 85 %** (branches included). Do not finish a change
  that lowers it: add tests. Do not use `# pragma: no cover` or `omit` to raise the figure.
- **Ruff must pass with zero errors.** Do not add a rule to `ignore` without a reason next to it.
- **Standard library only** at runtime; Python 3.9+.
- Tests never touch the real `~/.claude`, data dir, LaunchAgents, systemd, crontab or Task Scheduler.
- Keep backward compatibility with installs made under the old name (`kit`).
- Match the surrounding code: %-formatting, short comments, functions in the module that owns the responsibility.
