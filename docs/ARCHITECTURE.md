# Architecture

```
re (typer CLI, cli.py)
 ├── logging commands ─────────► repo.py ─────────► SQLite (db.py migrations)
 ├── re advise ───► advisor.py ─┬─► guide.py   (knowledge/symptoms.yaml)
 │                              ├─► ranges.py  (knowledge/ranges.yaml + per-bike overrides)
 │                              └─► history.py (FTS5 search, learned effects)
 └── re engineer ─► agent/engineer.py (Claude tool-use loop)
                     └─► agent/tools.py ─► repo / guide / history / ranges
                         (offline.py: connectivity check; falls back to advisor)
```

All state lives in one SQLite file. The default location is `~/.race_engineer/race_engineer.db`; override it with `--db` or `RACE_ENGINEER_DB`. The AI never holds state: it reads the database through tools and writes back through the same repo functions the CLI uses.

## Modules (`src/race_engineer/`)

| Module | Role |
|---|---|
| `cli.py` | All commands. `_open()` opens the database and turns `RepoError` / `ValueError` into a red message and exit code 1. User-entered text is passed through `rich.markup.escape`. `ConsoleIO` is the engineer's terminal input/output. |
| `db.py` | Connection setup and the `MIGRATIONS` list, tracked with `PRAGMA user_version`. **Only ever append new migrations; never edit one that has shipped.** |
| `models.py` | Pydantic records returned by repo: Bike, Track, Corner, Setup, Session, Lap, Feedback, Change, Outcome, TireTarget. |
| `repo.py` | All database reads and writes. Each write function commits before it returns. Raises `RepoError`, `NotFound` or `RangeError`. |
| `setup_diff.py` | The parameter list (`PARAMETERS`: label, unit, whether it's a whole number, direction hint). Parses `key=value` and `key=+N`, applies changes, and builds diffs. `electronics.*` entries are free-form. `parse_pressure()` reads bar or psi (a bare number of 10 or more is psi). `MOVED` gives a helpful error for the retired `tire.*_hot` keys. |
| `timing.py` | Lap time parsing and formatting. Times are stored as integer milliseconds. |
| `tagging.py` | Keyword tagging of feedback: front/rear, corner phase, corners. Anything ambiguous is left empty. |
| `guide.py` | Loads and validates `knowledge/symptoms.yaml`. `match()` ranks symptoms by keyword hits, front/rear and phase overlap. `best_symptom()` returns one only if it is a clear winner. |
| `ranges.py` | Default ranges from `knowledge/ranges.yaml`, and `violations()`. |
| `history.py` | `search()` (FTS5, filtered by track, tire and track temperature) and `learned_effects()`, which groups outcomes per (symptom, parameter, direction). |
| `advisor.py` | Offline advice: the guide's causes turned into concrete changes from the current setup, flagged where they're out of range or the setting isn't recorded, plus the bike's track record and similar notes. |
| `offline.py` | `is_online()`: a socket check against the API host with a 3 s timeout. |
| `agent/prompts.py` | The system prompt. It never changes (no dates or ids in it), so it can be cached. |
| `agent/tools.py` | Tool schemas (`TOOLS`), pydantic models that validate tool input, and `Toolbox.run()`. Errors go back to the model as `is_error` results. `RiderIO` describes how tools and the engineer talk to the rider. `propose_changes`, `log_rider_change` and `record_outcome` ask the rider to confirm; `record_hot_pressures` only announces what it saved. |
| `agent/engineer.py` | The conversation loop, request settings, error mapping (`EngineerOffline` / `EngineerUnavailable`) and transcript saving. |

## Database (migrations)

- **v1:**
  - `state`: key/value for the current bike, current session and rider profile.
  - `bike`, `setup` (params as JSON, `parent_id` links each setup to the one before), `track`, `corner`.
  - `session` (records the setup it ran), `lap`, `feedback` (the `"end"` column needs quoting in SQL).
  - `change` (deltas as JSON, `proposed_by` rider/agent), `outcome` (verdict better/same/worse, lap delta, resolved flag).
- **v2:**
  - `param_range` (per-bike ranges).
  - `change.symptom`.
  - `notes_fts` (FTS5), kept in sync by triggers on feedback, change and outcome, and back-filled for existing rows.
- **v3:** `pending_analysis` (the offline queue) and `engineer_conversation` (transcripts as JSON).
- **v4:**
  - `session.front_hot` / `session.rear_hot`: hot pressures in bar, measured after the session. They used to be setup parameters (`tire.front_hot` / `tire.rear_hot`); the migration copies the last such change made after each session onto it.
  - `tire_target`: the tire maker's hot window per tire name (case-insensitive) and position.

## Changes made during a session

`repo.change_setup(..., ran_in_session_id=N)` (CLI: `re setup set --this-session`, engineer: `log_rider_change`) is for a change that was already on the bike when session N ran. It moves session N to the new setup and links the change to the session before N, so `re outcome` compares N against that one. It is refused unless N is the latest session on the bike's current setup.

## Engineer loop (`agent/engineer.py`)

1. The first user message is the feedback text plus the keyword tags (marked as guesses).
2. Each request is `client.beta.messages.create(...)` with:
   - `model`: `claude-opus-5-5`;
   - `output_config={"effort": ...}`;
   - `cache_control={"type": "ephemeral"}`;
   - `betas=["server-side-fallback-2026-07-01"]` and `fallbacks="default"`;
   - fixed `system` and `tools`.

   It does **not** pass `thinking`, `temperature` or a forced `tool_choice`: thinking is always on for this model, and the other two are rejected.
3. The assistant's `response.content` is appended exactly as received (the history is append-only). All tool results go back in one user message.
4. A `refusal` turn is not added to the history. On `max_tokens`, tool calls are never run.
5. Within one engineer turn there are at most 20 rounds of tool calls.
6. The transcript is saved after each round of tool calls and at the end, but only once the engineer has replied at least once.

## Testing

- Run `.\.venv\Scripts\python.exe -m pytest -q`.
- `tests/conftest.py` provides a temporary database (`conn`) and a `weekend` fixture: a bike, a track and an active session.
- `test_engineer.py` drives the loop with a `FakeClient` (scripted responses) and a `ScriptedIO`.
- `test_cli.py` runs the commands through typer's `CliRunner`, with the console widened to 200 columns.
- There are no tests against the live API; trying it for real is a manual step (see STATUS.md).
