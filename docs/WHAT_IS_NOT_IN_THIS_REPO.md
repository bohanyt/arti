# What is not in this repository

This public tree is an export of the author's working repository. The export ships an explicit list of paths and
leaves everything else out. Code and tests sometimes name a file that is not here; it is one of the kinds below.

## 1. Private by design

| What | Read by | Replaced for a plain ARTI by |
|---|---|---|
| API keys and tokens (decision provider, voice, alerts, VTube Studio) | `arti_env.py`, the bridge, the alert readers | `.env.example`, your own keys |
| Her persona | `arti_bridge.load_soul_context` | `ARTI_SOUL.example.md` |
| The private config overlay | the bridge's config loader | `config_local.json.example` |
| Her self, wants, mind and mood | `arti_state.py`, `arti_mind.py`, the bridge | empty files; `ARTI_MOOD_STATE.example.json`, `ARTI_MIND_PREFS.example.jsonl` |
| Her long memory (the vault and its index) | `arti_vault_rag.py`, the bridge | none (an empty memory) |
| Viewers and stream data | the bridge, the alert readers | `ARTI_VIEWERS.example.md` |
| Game saves, among them her real farm | the game, the live path's backup command | a new test farm |

## 2. Recorded material

- Transcripts, chat logs, session logs and diaries of real streams.
- Journals, command journals, provider requests and replies, and decision pages captured from real or rehearsal
  runs. Many tests of the working repository replay such captures; those tests are left out here.
- Run evidence: soak and bench runs, receipts, consoles, screenshots.

## 3. Game-owned material

- Screenshots and image crops from the game.
- Test fixtures that carry the game's own text (dialogue, letters, notices, quest and item descriptions) or map
  layouts read from the game's files, and all C# test projects (they carried game text and need the game's
  assemblies and inputs that are not here).
- Larger tables read from the game's content files and offline ports of game routines (a crop table, festival
  dates, a fishing minigame model, a starter furniture layout, bundle rows). The crop table module ships as a stub
  without data.
- What remains: short identifiers the code needs to talk about the game (item ids, place and character names,
  menu class names), a few re-implemented game rules, and some short on-screen strings the code matches. You need
  your own copy of Stardew Valley.

## 4. Working notes

- The design records (ADRs), audits, run reports, plans and research notes, and the coordinator's runbooks. They
  quote sessions and the game, so this first release leaves them out. Code comments still cite them by number.
- One-off maintenance scripts that rewrote her memory, probe scripts with recorded dialogue, and the export tool.
- A clip-moment package taken over from another of the author's projects, and the coordinator's deployment
  worker.
- An XNB reader whose decompression code follows an upstream implementation whose licence was not checked
  (`scripts/m1_operator/xnb_items.py`). `stage_save.py` and `witness_isolated.py` import it and fail with an
  import error when it is missing; `stage_save.py` also accepts `--catalog FILE` in its place.
- One-off analysis scripts of private runs (`scripts/analysis`), and the free-play tests and seed recipes that
  replayed private runs (`tests/test_free_play_harness.py`, which does not, ships).
- Two probe scripts written for one vision server, and the avatar's expression presets and parameter map, which
  were tuned to a third-party Live2D model (`templates/` keeps only `IDLE_CONFIG.md`). The expression files the
  bridge names (`Arti*.exp3.json`) belong in your own model's folder. Two expression patch scripts tuned to that
  model are left out too.
- The rig's shell and PowerShell helpers: `scripts/free_play/Invoke-Soak.ps1` (bench dispatch and the witness run),
  `scripts/m1_operator/run_attempt.sh` and `run_manual.sh` (the cabin-exit recipe and manual reruns that the
  operator output suggests), and `scripts/livepath/audio_route.ps1` (the passive-mic route check). Code that calls
  them fails or refuses when they are missing.
- The witness tooling also needs inputs that are not here: the native map export of `witness_map_probe`, and the
  game-data decoder above (`xnb_items.py`) for the event export and for witness staging through `stage_save.py`.

## 5. Built and rig files

- Built binaries, the savestate library and the rig's local configuration. `scripts/rigbuild` and `docs/SETUP.md`
  describe how a machine is set up.
