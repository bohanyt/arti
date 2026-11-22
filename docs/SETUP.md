# Setting up ARTI

This is a short, public version of the author's bring-up notes. Nothing here is automated end to end, and parts of
it were only ever run on the author's own machines. Read it as a map, not a recipe.

## 0. Install by hand

| What | Used by |
|---|---|
| Git and Windows PowerShell | everything |
| Stardew Valley 1.6 with SMAPI 4.x in the game folder | the game, both instances (the host and her farmhand) |
| A .NET SDK that builds net6.0 projects (set `DOTNET_ROLL_FORWARD=Major` on a newer runtime) | the C# mod |
| Python 3.11 or 3.12 in a venv with `requirements.txt` and `requirements-test.txt` | the runtime and the tests |
| Python 3.12 in a second venv with `requirements-supertone.txt` | her TTS voice (optional) |
| VTube Studio with the plugin API on (port 8002) | the avatar (optional) |
| A decision provider: your own model account or API key | her decisions |

## 1. Clone and fill in the private files

```powershell
git clone https://github.com/bohanyt/arti.git arti
cd arti
py -3.11 -m venv venv
venv\Scripts\python -m pip install -r requirements.txt -r requirements-test.txt
copy .env.example .env                                   # your own keys
copy config_local.json.example config_local.json
copy ARTI_SOUL.example.md ARTI_SOUL.md                   # the example persona
copy ARTI_VIEWERS.example.md ARTI_VIEWERS.md
copy ARTI_MOOD_STATE.example.json ARTI_MOOD_STATE.json
```

Her mind starts empty; `ARTI_MIND_PREFS.example.jsonl` shows the row shape. There is no memory, no viewer data and
no save. `docs/WHAT_IS_NOT_IN_THIS_REPO.md` lists what is missing and why.

## 2. The tests

See `docs/TESTING.md`. The Python tests that ship use inputs made for them (some adapted from real game states,
with the game's text replaced); tests that replayed recorded sessions or game captures are not in this repository.

## 3. The mod

`stardew-mod/ArtiStardewBridge` is a SMAPI mod; build it with the .NET SDK against your own game install (SMAPI's
build package finds the game folder). Larger tables and offline ports read from the game's own files are not in this
repository (the crop table module is a stub without data), and neither are the C# test projects; short identifiers
and a few re-implemented rules remain. `scripts/rigbuild` holds the author's build helpers (`python -m
scripts.rigbuild check --config scripts\rigbuild\rigbuild.example.json`); copy the example config and fill in your
own paths. Its `native` and `package` steps need inputs that are not here: the C# test projects, two Python tests
of the author's (`tests/test_pm1_golden.py`, `tests/test_live7_trim_destinations.py`), the page-weight corpus and
baseline under `scripts/free_play/fixtures/`, `scripts/rigbuild/measure_page.py` and
`scripts/rigbuild/settings_defaults.json`. In this tree those steps refuse at the start and name what is missing.
`scripts/check_repo_rules.py` skips its two checks whose inputs (`docs/CURRENT.md` and the top-level allowlist) are
not here.

## 4. Playing

The game path joins a host game as a farmhand. The author's runners are `scripts/free_play` (unattended soak and
bench) and `scripts/livepath/mabar.py` (a session with voice). Both expect a local rig layout and private files that
are not here, so expect to read the code and adapt paths before anything runs. Two settings in `mabar.py` need your
own values: `vts_expected_model_name` is a placeholder (`MyModel`) that must match your VTube Studio model's name,
or set it to `""` to switch the model check off; and `--passive` (passive mic replies) asks
`scripts/livepath/audio_route.ps1`, a helper that is not in this repository, where the audio is routed, so without
it the route reads as unreadable and passive replies stay off.
