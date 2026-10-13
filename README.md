# Race Engineer

A virtual motorcycle race engineer for the terminal. Everything is stored in a local SQLite database and works offline:

- **Logging** of bikes, tracks, sessions, setups, lap times and rider feedback.
- **A symptom guide** (`re guide`) with about 20 common complaints, their likely causes, what to check and what to change.
- **Offline advice** (`re advise`) that combines the guide with your current setup, your bike's allowed ranges and your own history.
- **History** (`re history`): search past notes, and see which changes have worked for this bike.
- **Outcomes** (`re outcome`): record how each change worked. This builds your bike's history.
- **Tire pressures** (`re pressure`, `re tire target`): log hot pressures after each session, in bar or psi, and compare them with your tire maker's hot window.
- **The AI engineer** (`re engineer`): talk a complaint through with Claude. It reads your setup, conditions and history, asks follow-up questions, and proposes at most two changes at a time. It explains each proposal, and only applies it if you accept. Offline, it queues the feedback and shows the offline advice instead.

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
re feedback "front feels slippery mid corner T3, T7" --severity 3   # tagged as front_push_mid
re advise                                                    # likely causes, checks, what to try

re setup set fork.rebound=-2 --why "hold the front down"     # creates a new setup; the old one is kept
re setup diff                                                # compare with the previous setup
re session start --track Brno --type FP2 --track-temp 41     # runs with the new setup
re lap add 2:01.640 2:01.388
re pressure --front 36.5psi --rear 1.75                      # hot pressures right after the session
re outcome better --resolved --notes "front stays planted"   # best-lap gain vs FP1 is computed
```

Changed something in the pit lane after starting a session? Record it against that session:

```powershell
re setup set tire.front_cold=29psi --this-session --why "front too hot in FP1"
```

Or let the engineer drive it:

```powershell
re feedback "front feels slippery mid corner T3, T7"
re engineer          # asks follow-ups, proposes a change, applies it if you accept
                     # next session: run it again and tell it how the change felt
```

## The AI engineer

`re engineer` needs an Anthropic API key. Create one at https://platform.claude.com, then:

```powershell
setx ANTHROPIC_API_KEY sk-ant-...    # then open a new terminal
```

- **Model and effort:** it uses `claude-opus-5-5` at effort `high`. Change these with `--model` / `--effort`, or with the `RACE_ENGINEER_MODEL` / `RACE_ENGINEER_EFFORT` environment variables.
- **Its tools:** everything it knows comes from your local database, through these tools:
  - session context
  - setup history
  - history search
  - learned effects
  - the symptom guide
  - updating feedback tags
  - proposing changes
  - recording outcomes
  - saving hot pressures you tell it (bar or psi)
  - recording a change you made yourself
- **Your approval:** proposals are checked against your bike's ranges and shown to you before anything is applied. Every applied change is recorded as proposed by the engineer.
- **Saved conversations:** each conversation is stored in the database for later review.
- **Offline:** with no connection (or if it drops mid-conversation), the feedback is queued and you get `re advise` output instead. Run `re analyze-pending` when you have signal again.
- **Refusal fallback:** requests use Anthropic's server-side refusal fallback (`fallbacks: "default"`). If the model declines a request on safety grounds, it is retried on a fallback model.

## Setup values

- `key=value` sets an absolute value; `key=+N` or `key=-N` changes it relative to the current value.
- Clickers are counted **clicks out from fully closed**.
- `re setup params` lists all keys. `electronics.<anything>` takes free-form values, for example `electronics.tc=4` or `electronics.map=B`.
- Setups are never edited in place. `re setup history` shows the lineage and `re setup revert <id>` goes back to an earlier setup.
- Pressures are in bar, but you can type psi: `tire.front_cold=30psi`. A bare number of 10 or more is read as psi.
- Hot pressures are not setup values: they are measured after a session, so log them with `re pressure --front 2.45 --rear 1.9`. A reading that isn't above the cold pressure the session ran is refused as a likely misread (`--force` to save it anyway). Set the tire maker's hot window with `re tire target SC1 front 2.3 2.5`.
- Changes must stay inside the allowed range for each setting. The defaults in `knowledge/ranges.yaml` are wide sanity limits. Set your bike's real limits, such as clicker counts and your tire maker's pressures, with `re bike range fork.rebound 0 20`. Use `--force` to go outside a range.
- Each change records the symptom it targets: by default the latest feedback's symptom, or set it with `--symptom`. Once results are recorded, `re history effects` and `re advise` show how each kind of change has worked for this bike.

## Symptom guide

`src/race_engineer/knowledge/symptoms.yaml` is plain YAML you can edit: add symptoms, keywords that match how you describe things, causes and adjustments. It is checked when it loads, so a misspelled parameter name raises an error.

## Commands

| Command | What it does |
|---|---|
| `re init`, `re profile` | create the database; show or set your rider profile |
| `re bike add / list / use / baseline` | manage bikes |
| `re track add / list / show / corner` | manage tracks and corners |
| `re session start / list / show / use` | sessions and the active session |
| `re setup show / set / diff / history / revert / params` | setups |
| `re lap add / list` | lap times (`--sectors`, `--invalid`) |
| `re pressure --front X --rear Y` | hot pressures measured after the session (bar or psi) |
| `re tire target / list` | the tire maker's hot pressure window per tire |
| `re feedback "..."` | rider feedback; front/rear, phase, corners and symptom are detected from the text unless given |
| `re outcome better/same/worse` | record how the latest (or `--change`) change worked |
| `re engineer [feedback id]` | talk it through with the AI engineer |
| `re analyze-pending` | feedback queued while offline |
| `re advise [feedback id]` | offline advice for the latest (or given) feedback |
| `re guide [symptom]` | browse the symptom guide |
| `re history search / effects` | search past notes; what changes did for this bike |
| `re bike range [param min max]` | show or set allowed setup ranges for the current bike |

## Development

- [`docs/STATUS.md`](docs/STATUS.md): what's built (phase by phase), what's next, known gaps and the decision log.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): modules, database schema and migrations, the engineer loop, and testing.
- [`CLAUDE.md`](CLAUDE.md): working notes for Claude Code sessions on this project, including how to pick up where the last session left off.

Run the tests with `pytest` (102 tests). The engineer is tested against a scripted fake API client, so the tests make no network calls.
