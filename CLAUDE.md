# CLAUDE.md

A virtual motorcycle race engineer: a Python CLI (`re`) with a local SQLite database and a Claude tool-use agent. User docs are in `README.md`, the design in `docs/ARCHITECTURE.md`, and progress, next steps and decisions in `docs/STATUS.md`.

## "Continue where you left off"

When the user says this, or starts a new session without context:

1. Read `docs/STATUS.md`: start with "Where we left off" (current phase, changes we want next, open questions for the rider), then the phases, next steps, known gaps and decision log.
2. Run `git log --oneline -10` and `git status` to see recent work and anything uncommitted.
3. Run the tests: `.\.venv\Scripts\python.exe -m pytest -q`.
4. Summarize where things stand in a few lines and propose the next step from STATUS.md. Confirm with the user before starting larger work.

At the end of each piece of work, update `docs/STATUS.md`: "Where we left off", the phase table, the "Last updated" line, the next steps and any new decisions. Commit it together with the code.

## Environment (Windows 11, PowerShell 5.1)

- **Python:** use the project venv, `.\.venv\Scripts\python.exe`. After changing dependencies in `pyproject.toml`, run `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`. The CLI is `.\.venv\Scripts\re.exe`.
- **git and gh may not be on PATH in shells opened before they were installed.** Use full paths: `& "C:\Program Files\Git\cmd\git.exe"` and `& "C:\Program Files\GitHub CLI\gh.exe"`.
- **Commit messages:** PowerShell 5.1 breaks quoted multi-line `git commit -m` arguments. Write the message to a file in the scratchpad and use `git commit -F <file>`.
- **Remote:** `origin` is the private GitHub repo `MartinAlias/AI_RaceEngineer_Agent` on branch `main`. **Push only when the user asks.**
- **Never touch the rider's real database** (`~/.race_engineer/race_engineer.db`). For manual tests, set `RACE_ENGINEER_DB` to a file in the scratchpad.
- **No `ANTHROPIC_API_KEY` is set on this machine yet,** so `re engineer` can't be tried against the live API here.

## Conventions

- **Setups are never edited in place.** Changes go through `repo.change_setup` / `repo.revert_setup`, which insert a new setup and a `change` row.
- **Setting directions:** clickers are clicks out from fully closed (negative step = more damping). `fork.height`: more = front lower. `shock.ride_height`: more = rear higher. Pressures are in bar.
- **Database migrations:** append a new entry to `db.MIGRATIONS`; never edit one that has shipped. Update `docs/ARCHITECTURE.md` too.
- **All SQL goes in `repo.py`** (or `history.py` for search and aggregation). Repo writes commit before they return and raise `RepoError` subclasses.
- **CLI commands:** use `with _open() as conn:`. Escape user-entered text with `escape()` before printing it with rich.
- **The symptom guide** is `knowledge/symptoms.yaml`. Quote YAML flow-list items that contain `,` `?` `:` or `#`. `tests/test_guide.py` validates the file.
- **Every feature gets tests.** CLI tests go in `tests/test_cli.py` and engineer tests use the fake client in `tests/test_engineer.py`.
- **Before changing Claude API code** in `agent/`, load the `claude-api` skill. Keep:
  - the system prompt and tool list unchanged per request (so caching works);
  - the message history append-only;
  - no forced `tool_choice`, `thinking` config or `temperature` (Opus 5.5 rejects them).
