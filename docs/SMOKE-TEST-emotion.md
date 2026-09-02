# Smoke test — ARTI emotion system

Use this checklist before enabling emotion/nod behavior with a real VTube Studio model. Unit tests prove parser/policy behavior only; this checklist is for local application verification.

## 1. Public-safe unit checks

From the repository root:

```bash
python -m pytest -q tests/test_arti_wake.py tests/test_expression_runtime.py tests/test_arti_nod.py
```

Expected: all tests pass.

## 2. Prepare local model configuration

1. Start VTube Studio and load your own model.
2. Copy `config_local.json.example` to `config_local.json` if you have not already.
3. Set `vts_model_dir` to your local model directory if the enabled expression checks require it.
4. Keep the real path/token/config local and gitignored.

## 3. Wake-word false-trigger check

Send a passive chat/message such as:

```text
berarti hasilnya bagus
```

Expected: the substring inside `berarti` does **not** count as an explicit ARTI wake call.

Then send:

```text
arti, menurut kamu gimana?
```

Expected: the explicit call is recognized normally.

## 4. Emotion tag parsing

With emotion behavior enabled locally, exercise replies that resolve to several supported moods.

Verify:

- `[EMOTION:*]` is never spoken or shown as normal reply text;
- the correct local mood expression is applied when its asset exists;
- an unknown/missing mood cannot become an arbitrary file request;
- an explicit request such as `coba pasang muka sedih` takes priority over a conflicting model-selected mood.

## 5. Talking overlay

During TTS, verify the talking/lip-sync state still works while a mood overlay is active. A mood expression must not accidentally leave ARTI with a permanently disabled talking/default state.

## 6. Nod gating

Test once with nod behavior disabled, then once enabled.

Expected:

- disabled: no CONFIG-driven nod behavior;
- enabled: nod behavior appears only while appropriate and returns to neutral afterward.

Parameter amplitude/speed are model-specific; judge them on your own model rather than copying another setup's calibration.

## 7. Linger / cleanup

If emotion linger/fade is enabled:

1. complete a short emotional reply;
2. verify the mood remains only for the configured short linger period;
3. trigger another turn before linger finishes;
4. verify the old cleanup task does not erase the new turn's mood;
5. verify the model eventually returns to default/neutral.

## 8. Restart / reconnect

Restart the bridge or temporarily reconnect VTube Studio.

Expected: no stale mood is left permanently active and the normal default → listening/thinking → talking lifecycle still works.

## Evidence language

Passing the public unit tests is `UNIT_TESTED` / `CLOUD_VERIFIED` only. Mark VTube Studio behavior as locally verified only after running the application/model checks above on the exact local setup.

Do not attach private model assets, VTube Studio tokens, real stream captures, private transcripts, or local user paths to a public issue.
