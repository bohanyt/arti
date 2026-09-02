# Expression and motion system

Public technical reference for ARTI's VTube Studio integration. This document describes the shipped architecture without assuming the maintainer's model files, local paths, hotkey IDs, audio devices, or live-session evidence.

For initial setup, read [`WIRING.md`](WIRING.md) and [`VTS-ANIMATION.md`](VTS-ANIMATION.md) first.

## Scope and verification

The public repository contains the runtime code that drives VTube Studio, but it does **not** ship a specific Live2D model or the maintainer's expression/motion assets. Cloud CI can validate Python behavior and deterministic tests; it cannot prove that a particular VTube Studio model, hotkey, tracking parameter, microphone, or audio route works on another machine.

Treat model/application behavior as locally verified only after testing it with your own VTube Studio setup.

## Runtime surfaces

The relevant public code is split across:

- [`hermes_vtuber_bridge.py`](../hermes_vtuber_bridge.py) — conversation lifecycle, VTube Studio connection, expression-state transitions, idle/motion orchestration, and TTS coordination;
- [`arti_expression_runtime.py`](../arti_expression_runtime.py) — emotion-tag parsing, mood overlays, expression cleanup, and optional linger behavior;
- [`arti_nod.py`](../arti_nod.py) — CONFIG-gated nod behavior during speech.

The exact implementation may evolve, so prefer function/config names over historical line numbers.

## Conversation expression lifecycle

A normal turn can move through model-specific equivalents of these states:

```text
idle/default
    ↓ user turn starts
aware/listening
    ↓ request is being processed
thinking
    ↓ reply begins speaking
talking (+ optional mood overlay / nod)
    ↓ speech ends
default → idle
```

The public code commonly refers to the logical states `aware`, `mikir`, `bicara`, and `default`. The expression files that implement those states are model-specific. If your model uses different files or hotkeys, map them in local configuration/model setup rather than editing private paths into tracked source.

## Emotion overlays

`arti_expression_runtime.py` recognizes hidden response tags in this form:

```text
[EMOTION:senang|sedih|marah|bingung|neutral]
```

The tag is stripped before speech. When emotion behavior is enabled, the runtime may overlay a configured mood expression while the talking state is active.

Important constraints:

- emotion behavior is CONFIG-gated;
- an explicit user request for a facial expression can take priority over an LLM-selected mood;
- mood overlays must not take over lip-sync/mouth parameters that belong to the talking expression;
- the runtime can keep a mood visible briefly after speech through configured linger/fade behavior;
- missing model files should fail visibly in logs rather than being treated as proof that the public code is broken.

See [`SPEC-arti-emotion.md`](SPEC-arti-emotion.md) and [`SMOKE-TEST-emotion.md`](SMOKE-TEST-emotion.md) for the public contract and local smoke test.

## Nod behavior

`arti_nod.py` provides optional head-nod behavior during speech. It is kept separate from the mood-expression module so a model can use emotion overlays without enabling nods, or vice versa.

Nod behavior is model/application dependent because tracking parameter ranges and VTube Studio mappings vary between models. Keep it disabled until your own model has been tested locally.

## Idle motion and parameter injection

The bridge also contains idle/motion orchestration for VTube Studio. Depending on local configuration and model assets, this can include:

- triggering configured VTube Studio motion hotkeys;
- reading configured pose/expression targets;
- injecting supported VTube Studio tracking parameters for smooth head movement;
- pausing or suppressing idle movement while ARTI is actively speaking;
- restoring a neutral/default state after a turn or reconnect.

Do not copy hotkey IDs from another machine. VTube Studio IDs and model assets are discovered/configured per local setup.

### Tracking vs model parameters

VTube Studio distinguishes injectable tracking/input parameters from arbitrary Live2D model parameters. A value stored in a model expression file is not automatically a valid target for `InjectParameterDataRequest`.

When using parameter injection:

1. identify the tracking/input parameter that VTube Studio exposes;
2. map your model's desired pose onto that tracking parameter;
3. keep ranges conservative;
4. test neutral cleanup and reconnect behavior locally.

## Model assets are intentionally not published

The public repository does not include the maintainer's:

- Live2D model directory;
- `.exp3.json` expression collection;
- `.motion3.json` motion collection;
- generated VTube Studio hotkey IDs;
- device IDs or audio-routing configuration;
- VTube Studio authentication token.

Use your own assets and keep machine-specific paths/tokens out of Git.

A typical local model directory can be supplied through `vts_model_dir` in `config_local.json`. The real path stays local.

## Failure behavior to test

Before using the animation stack on stream, verify at least:

- VTube Studio connects and authorizes the plugin;
- the default/listening/thinking/talking states map to valid local assets;
- a missing optional mood does not crash a turn;
- talking/lip-sync behavior is not disabled by a mood overlay;
- nod/parameter injection returns to neutral;
- idle motion pauses appropriately while speaking;
- reconnect/restart does not leave a stale expression active.

Cloud CI is not evidence for these local VTube Studio checks.

## Privacy and publication boundary

When documenting or reporting a VTube Studio issue, do not publish real tokens, private screenshots, local user paths, device inventories, captured stream transcripts, or model assets you do not have permission to distribute. Replace local identifiers with synthetic placeholders.
