# AGENTS.md

Notes for anyone, human or coding agent, who changes this repository.

## Layout

- `arti_*.py`, `arti_runtime/`: the Python runtime (her decisions, memory, voice bridge, game runtime).
- `stardew-mod/`: the C# SMAPI mod (its C# test projects are not in this repository).
- `contracts/`: JSON schemas and vocabularies shared by the mod and the runtime.
- `scripts/`: operator and rig tools (bench, soak, live path, build helpers).
- `mc-bot/`, `mc-plugins/`: the older Minecraft integration.
- `tools/`: small command-line tools.
- `tests/`: the Python tests.
- `templates/`, `*.example.*`: shapes of the private files a real setup fills in.

## Tests

```powershell
py -3.12 -m venv venv          # the export's tests ran on Python 3.12
venv\Scripts\python -m pip install -r requirements.txt -r requirements-test.txt
venv\Scripts\python -m pytest tests -q
```

`docs/TESTING.md` has more. The Python tests use inputs made for them; some began as the shape of a real game
state or session, with the game's text and recorded speech replaced by invented text. Tests that replayed recorded
sessions or game captures, and the C# test projects, are not in this repository.

## Rules for contributions

- No recordings: no stream transcripts, chat logs, journals, provider requests or replies from real sessions,
  in code, comments, tests or docs. Write invented inputs of the same shape instead.
- No game text and no game assets: no dialogue, letters, quest or item descriptions, maps or screenshots copied
  from Stardew Valley. Describe them in your own words, or invent the text a test needs.
- No real people: no viewers' names or handles, no account names, no personal details.
- No secrets and no private machines: no keys, tokens, private addresses or host names; paths use a neutral
  account name.
- A change that passes its tests is not proven in the game. Say which one you have.
