# SPEC — ARTI wake matching and emotion behavior

This document defines the public behavioral contract for wake-word matching and CONFIG-gated expression/emotion behavior.

## Wake-word matching

`is_arti_wake_call(text)` is the public source of truth for deciding whether a message explicitly calls ARTI.

Requirements:

1. Match `arti` as a word/call, not as an arbitrary substring.
2. Words such as `berarti`, `artinya`, and `mengartikan` must not trigger ARTI merely because they contain the letters `arti`.
3. YouTube/chat routing should call the helper rather than duplicate substring logic.
4. A passive sentence such as `berarti hasilnya bagus` must not be queued as an explicit ARTI wake call.

Public deterministic evidence:

```bash
python -m pytest -q tests/test_arti_wake.py
```

## Emotion system

Emotion and nod behavior are optional and CONFIG-gated.

The response model may append one hidden tag:

```text
[EMOTION:senang|sedih|marah|bingung|neutral]
```

Requirements:

1. the tag is removed before TTS/output;
2. unknown tags fail to `neutral` rather than becoming arbitrary model-file names;
3. an explicit face request from the user can override a conflicting model-selected mood;
4. mood expressions overlay the speaking lifecycle instead of replacing the entire conversation state machine;
5. nod behavior remains independently gated by `expression_nod_enabled`;
6. missing optional local model assets must not be represented as cloud verification.

Public deterministic evidence:

```bash
python -m pytest -q tests/test_expression_runtime.py tests/test_arti_nod.py
```

## Safety / publication constraints

- Do not auto-enable model/application-specific expression features for a fresh clone.
- Do not publish VTube Studio tokens, private model paths, device IDs, or captured session text as test fixtures.
- Keep fixtures synthetic.
- Do not equate passing unit tests with successful local VTube Studio animation.

For local application validation, follow [`SMOKE-TEST-emotion.md`](SMOKE-TEST-emotion.md).
