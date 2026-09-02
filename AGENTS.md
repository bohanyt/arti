# AGENTS.md — public ARTI contributor guide

This repository is ARTI's curated public product tree. Treat it as a distributable software repository, not as a mirror of the private development workspace.

## Scope

Work on product/runtime code, public-safe tests, configuration examples, and user-facing documentation. Do not reconstruct or import private development history.

## Privacy boundary

Never commit secrets or private runtime data. This includes `.env`, real `config_local.json`, API/VTS tokens, viewer profiles, private soul/mood state, transcripts, vault/RAG databases, telemetry/log captures, screenshots/dumps, local backup paths, raw donation/chat payloads, or private fine-tuning data.

Fixtures and examples must be synthetic. If you cannot prove a value is synthetic/public-safe, do not add it.

## Internal-development material

Do not add private handoffs, Control Tower notes, task queues, raw research archives, raw planning archives, or local-lab choreography. Public documentation should explain the product, setup, architecture, behavior, and verification status directly.

## Verification language

Use evidence precisely:

- deterministic/unit/cloud-safe tests: `UNIT_TESTED` / `CLOUD_VERIFIED`;
- real local application/hardware evidence: only claim `LOCAL_VERIFIED` / `VERIFIED LIVE` when that exact scope has separate evidence.

Passing CI does not prove VTube Studio, OBS, Steam/Stardew, audio hardware, GPU behavior, or a live Minecraft world.

## Code changes

- Keep optional integrations gated and fail-safe.
- Avoid adding new network/provider dependencies without documenting them.
- Keep secrets in environment/local configuration, never source literals.
- Add or update public-safe tests with behavior changes.
- Prefer deterministic tests over tests that require external services.
- Preserve the distinction between Python orchestration and external integrations such as `mc-bot/`.

## Before opening a PR

Run the strongest public-safe checks available, inspect the tracked tree for secrets/private artifacts, and make sure docs do not make stronger verification claims than the evidence supports.