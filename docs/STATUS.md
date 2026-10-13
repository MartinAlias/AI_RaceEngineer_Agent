# Project status and roadmap

**This is the single source of truth for where the project stands.** Update it at the end of every piece of work: what was done, the commit, what's next.

Last updated: 2026-10-13 (phase 5b: `re web`; all phases done except the live API trial). Tests: 125 passing.

## Goal

A virtual race engineer for one rider and their motorcycle, used from a laptop terminal at the track. The rider feeds it:

- the bike, track, tires and pressures, and track temperature;
- lap times, suspension settings and gearing;
- what they feel ("front feels slippery mid corner").

The engineer then:

1. analyzes the complaint and identifies likely causes;
2. asks follow-up questions;
3. searches previous setup notes;
4. recommends one or two changes and explains why;
5. records each change and its result;
6. builds a history that fits this specific bike and riding style.

Decisions made with the rider at the start:

- **Stack:** Python, used as a desktop command-line tool.
- **Data entry:** typed in by hand first; importing from a data logger comes later.
- **Connectivity:** internet at the track is unreliable, so logging must work offline. The AI part runs when there is signal, and work is queued otherwise.

## Phases

| Phase | Status | Commit |
|---|---|---|
| 1. Logging foundation | done | `0c47910` |
| 2. Symptom guide, ranges, history search, offline advice | done | `2421a5f` |
| 4a. `re outcome` (pulled forward before phase 3) | done | `62d8f6e` |
| 3. AI engineer (`re engineer`, `re analyze-pending`) | done, **not yet tried against the live API** | `4c8ddce` |
| 3a. Fixes from the engineer dry run | done | see `git log` |
| 4b. Weekend report export (`re export`) | done | see `git log` |
| 5. Lap timer CSV import, sector analysis, rider-profile updates | done | see `git log` |
| 5b. Local web page (`re web`) | done | see `git log` |

### Phase 1: Logging foundation (done)
- SQLite database with migrations.
- Bikes, tracks with their corners, sessions (conditions, tires, tire age).
- Setups stored as snapshots that are never edited; every change creates a new setup.
- Lap times with optional sector times, and an invalid flag.
- Rider feedback, tagged by keyword: front/rear, corner phase, corners.
- CLI built with typer and rich.

### Phase 2: Knowledge and history (done)
- `knowledge/symptoms.yaml`: 22 symptoms (21 at first; `front_understeer_mid` added in 3a), each with keywords, follow-up questions, causes, checks and adjustments. It is validated when loaded.
- Feedback is automatically tagged with a symptom from the guide.
- `knowledge/ranges.yaml` holds default allowed ranges. Each bike can override them (`re bike range`), and setup changes outside a range are refused unless `--force` is given.
- Full-text search (SQLite FTS5) over feedback, change reasoning and outcome notes (`re history search`).
- Learned effects: results grouped by symptom, parameter and direction (`re history effects`).
- `re advise`: offline advice for a piece of feedback.

### Phase 4a: Outcomes (done)
- `re outcome better|same|worse [--resolved] [--notes]` records how the latest change (or `--change N`) worked.
- It computes the best-lap difference against the session the change was made after.
- `re setup history` shows each change's result.

### Phase 5b: Local web page (done)
- `re web` serves a read-only page on 127.0.0.1 (default port 8765): sessions, each session's laps with best lap and best sectors highlighted, pressures against target, feedback, changes with results, the setup history, and learned effects. Light and dark mode.
- Standard library only (`http.server`), no new dependencies. Logging stays in the CLI.

### Phase 5: Import, sectors, profile (done)
- `re lap import FILE`: lap and sector times from a phone lap timer's CSV (the rider uses a phone app). Built without a real sample file, so detection is generic: delimiter, decimal comma, header row, lap / time / sector / status columns, m:ss, h:mm:ss or seconds. Out laps and laps over 15% slower than the median are imported as invalid. `--dry-run`, `--append`, and explicit column names when detection fails.
- `re session sectors` and the engineer's `get_sector_analysis` tool: best, median and spread per sector, consistency, theoretical best, compared with the previous session on the same setup, or for the first session on a new setup, the session the change was made after.
- The engineer's `suggest_profile_update` tool proposes a new rider profile; the rider sees old and new and accepts or declines.

### Phase 4b: Weekend report (done)
- `re export` writes a Markdown report: a summary table, the starting setup, every change with its reasoning and result, and per session the conditions, tires, pressures, laps (with sectors) and feedback.
- `--format csv` gives one row per lap, with the session's conditions and setup id.
- The default weekend is the latest session plus the sessions at its track in the 3 days before. `--from`, `--to` and `--track` choose something else.

### Phase 3a: Fixes from the engineer dry run (done)
On 2026-10-13 Claude Code played the engineer against a scratch database, using the real tool code. The engineer worked well (it called `get_session_context` first, asked 3 focused questions, made one in-range proposal, and refused to record an outcome when the rider's answer contradicted the records). The gaps it found are fixed:
- **Hot pressures are session measurements** (migration v4), logged with `re pressure` or the engineer's `record_hot_pressures` tool. A hot reading that is implausible or not above the cold pressure the session ran is refused unless forced.
- **Tire hot targets:** `re tire target SC1 front 2.3 2.5`. The engineer sees cold, hot and target per session.
- **psi:** pressures accept `30psi`; a bare number of 10 or more is read as psi.
- **Changes the rider made themselves:** `re setup set --this-session` and the engineer's `log_rider_change` tool record a pit-lane change against the session it ran in.
- **Symptom guide:** new `front_understeer_mid` (vague, runs wide mid corner); "vague" no longer tags as `front_push_mid`.
- **Change dates:** the engineer sees the date and type of the session a change followed, not the time it was typed in.
- **Prompt:** one change when one cause clearly leads; pressure, psi and rider-change guidance; clear up contradictions before recording anything.

### Phase 3: AI engineer (done, needs a live trial)
- `re engineer` runs a Claude tool-use loop with the rider in the terminal (`src/race_engineer/agent/`).
- Tools: session context, setup history, history search, learned effects, the symptom guide, updating feedback tags, `propose_changes` and `record_outcome`.
  - `propose_changes` allows at most 2 changes. They are checked against ranges and applied only if the rider says y.
  - `record_outcome` asks the rider to confirm before saving.
- Offline, or if the connection drops, the feedback is queued and the offline advice is shown. `re analyze-pending` works through the queue.
- Each conversation is saved in the `engineer_conversation` table.
- Tested against a scripted fake client (`tests/test_engineer.py`). **No API key is set on the dev machine, so the engineer has not yet talked to the real API.**

## Next steps (in order)

1. **Get a real CSV export from the rider's phone app** (which app: RaceChrono or Harry's LapTimer?) and add it as a test fixture; adjust `lapimport.py` detection if needed.
2. **Try it at the track** (CLI, import, `re web`, `re export`) and collect what's awkward. Ideas waiting for that feedback:
   - a lap time chart on the session page;
   - mapping sectors to corners, so sector analysis can talk about T3 instead of S2;
   - editing or deleting a wrongly logged lap, feedback or session.
3. **Last: first live trial of `re engineer`** (the rider chose to leave this until the end). The rider needs an API key (`setx ANTHROPIC_API_KEY ...`). Then read the transcripts in `engineer_conversation` and tune `agent/prompts.py`. Things to check:
   - Does it call `get_session_context` first?
   - Are its follow-up questions sensible, and how many does it ask?
   - Are its `propose_changes` arguments correct?
   - Does it record outcomes for pending changes?
   - Cost per conversation (consider logging `response.usage`).
   - Does it use the newer tools sensibly: `record_hot_pressures`, `log_rider_change`, `get_sector_analysis`, and (rarely) `suggest_profile_update`?

## Known gaps and ideas
- **Can't edit or delete logged entries.** Laps, feedback and sessions can be added but not corrected. Feedback tags can be corrected only by the engineer, via `repo.update_feedback`.
- **The symptom guide hasn't been reviewed by the rider.** It was written from general suspension-setup practice.
- **Search matching is crude.** It matches by word prefix ("slippery" becomes "slipp*") with no synonyms.
- **One rider only.** There is no concept of separate riders; the rider profile is a single piece of text.

## Decision log
- **Setups are never edited in place.** Every change inserts a new setup row and a `change` row, so the full history can be traced and reverted.
- **Clickers are counted in clicks out from fully closed.** A negative step means more damping. `fork.height` is the fork tube height above the top clamp (more = front lower). `shock.ride_height`: more = rear higher.
- **The engineer learns from data, not from model training.** It uses structured outcomes, search and the learned-effects summary.
- **`re outcome` was built before phase 3,** so history accumulates from the first real use.
- **The engineer loop is written by hand** rather than using the SDK's beta tool runner. This lets it pause for the rider, survive a dropped connection, and save transcripts.
- **API settings:**
  - Model `claude-opus-5-5` at effort `high`.
  - Server-side refusal fallback (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`).
  - Prompt caching: automatic top-level `cache_control`, with a fixed system prompt and tool list.
  - Message history is append-only.
  - No forced `tool_choice`; the model rejects it.
- **The live API trial is the last step,** at the rider's request. Until then, prompt and tool behavior is checked by dry runs in Claude Code.
- **Hot pressures are measurements on the session, not setup parameters** (migration v4). `tire.front_hot` / `tire.rear_hot` are no longer accepted by `re setup set`; old setups that contain them are left as they are.
- **Pressures can be entered in psi** and are stored in bar. A bare number of 10 or more is taken as psi, since no tire runs at 10 bar.
- **The web page is read-only** and uses only the standard library. Logging stays in the CLI, so there is one place where data is entered and validated.
- **Lap import is generic, not per-app.** It detects columns by name rather than hard-coding one app's format, because the exact export format wasn't available. A real sample should become a test fixture.
- **No `ask_rider` tool** (it was in the original plan). The engineer asks questions in its normal reply and the rider answers in the chat.
- **Repo:** GitHub `MartinAlias/AI_RaceEngineer_Agent` (private). Commits are authored as `martinhorciak <martinhorciak@gmail.com>`, set in the repo's local git config.
