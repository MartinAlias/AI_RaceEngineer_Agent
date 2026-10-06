# Race Engineer

A virtual motorcycle race engineer for the terminal. Phase 1 covers logging: bikes, tracks, sessions, setups, lap times and rider feedback, all stored in a local SQLite database that works offline. The AI engineer (diagnosis, follow-up questions and setup recommendations) comes in phase 3.

## Install

Requires Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

The database lives at `~/.race_engineer/race_engineer.db`. Override it with `--db PATH` or the `RACE_ENGINEER_DB` environment variable.

## A practice session

```powershell
re init --profile "late braker, trail-brakes deep, likes a stiff front"
re bike add R6 --make Yamaha --model YZF-R6 --year 2019 --set fork.rebound=10 --set fork.compression=12 --set tire.front_cold=2.1
re track add Brno --length 5403 --direction cw --corner "3:Hairpin:slow hairpin"

re session start --track Brno --type FP1 --track-temp 38 --air-temp 24 --tire-front "SC1" --tire-rear "SC0"
re lap add 1:43.10 1:42.31 1:42.05
re feedback "front feels slippery mid corner T3, T7" --severity 3

re setup set fork.rebound=+2 --why "less push mid corner"   # creates a new setup; the old one is kept
re setup diff                                                # compare with the previous setup
re session start --track Brno --type FP2 --track-temp 41     # runs with the new setup
```

## Setup values

- `key=value` sets an absolute value; `key=+N` or `key=-N` changes it relative to the current value.
- Clickers are counted **clicks out from fully closed**.
- `re setup params` lists all keys. `electronics.<anything>` takes free-form values, for example `electronics.tc=4` or `electronics.map=B`.
- Setups are never edited in place. `re setup history` shows the lineage and `re setup revert <id>` goes back to an earlier setup.

## Commands

| Command | What it does |
|---|---|
| `re init`, `re profile` | create the database; show or set your rider profile |
| `re bike add / list / use / baseline` | manage bikes |
| `re track add / list / show / corner` | manage tracks and corners |
| `re session start / list / show / use` | sessions and the active session |
| `re setup show / set / diff / history / revert / params` | setups |
| `re lap add / list` | lap times (`--sectors`, `--invalid`) |
| `re feedback "..."` | rider feedback; front/rear, phase and corners are detected from the text unless given |
