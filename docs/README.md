# Public documentation

This directory contains the documentation intentionally shipped with the public ARTI repository. Internal handoffs, private plans, session evidence, local telemetry, viewer data, and other development-only material are not part of this distribution.

## Start here

| Guide | What it covers |
|---|---|
| [`WIRING.md`](WIRING.md) | Connect the bridge to providers, VTube Studio, YouTube chat, TTS, local memory, and optional reflex audio. |
| [`MINECRAFT-SETUP.md`](MINECRAFT-SETUP.md) | Install and run the Mineflayer-based Minecraft integration. |

## Runtime and providers

| Guide | What it covers |
|---|---|
| [`MODEL-REGISTRY.md`](MODEL-REGISTRY.md) | Public source-of-truth for provider/model roles and retirement behavior. |
| [`OPENROUTER_MODELS.md`](OPENROUTER_MODELS.md) | OpenRouter-specific live/fallback notes. |
| [`OBSERVER.md`](OBSERVER.md) | Observer/runtime context pipeline. |
| [`SCOUTER.md`](SCOUTER.md) | Screen/context scouting behavior. |
| [`VISION-APIS.md`](VISION-APIS.md) | Supported vision-provider wiring. |

## Avatar, motion, and conversation behavior

| Guide | What it covers |
|---|---|
| [`VTS-ANIMATION.md`](VTS-ANIMATION.md) | VTube Studio animation setup. |
| [`Expression-Motion-System.md`](Expression-Motion-System.md) | Expression and motion architecture. |
| [`SPEC-arti-emotion.md`](SPEC-arti-emotion.md) | Emotion behavior contract. |
| [`SMOKE-TEST-emotion.md`](SMOKE-TEST-emotion.md) | Public-safe emotion smoke checks. |
| [`SPEC-latency-cowatch.md`](SPEC-latency-cowatch.md) | Co-watch latency expectations. |
| [`SMOKE-TEST-cowatch.md`](SMOKE-TEST-cowatch.md) | Public-safe co-watch smoke checks. |

## Local runtime state

The repository ships only safe templates such as `ARTI_SOUL.example.md`, `ARTI_VIEWERS.example.md`, and `ARTI_MOOD_STATE.example.json`. Copy them to their runtime names locally when needed. Real viewer profiles, transcripts, vault/session data, credentials, and local configuration are intentionally excluded from Git.

The Stardew Valley integration is not included in this public release. See the root [`README.md`](../README.md) for the current release boundary and verification language.
