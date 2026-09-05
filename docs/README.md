# ARTI public documentation

This directory is the navigation hub for documentation intentionally shipped with the public ARTI repository. Use it to understand setup, runtime boundaries, optional integrations, and what can or cannot be verified from a public clone.

**Status:** these pages describe the checked-out public revision. External provider catalogs and local application behavior can change independently, so provider/model availability and hardware-dependent features still require local verification.

## Start here / setup

- [`WIRING.md`](WIRING.md) is the main setup guide for readers connecting ARTI to providers, VTube Studio, YouTube chat, TTS, local memory, and optional OBS-related runtime wiring.
- [`MINECRAFT-SETUP.md`](MINECRAFT-SETUP.md) is for readers who want to install and run the shipped Mineflayer-based Minecraft integration.

## Runtime & providers

- [`MODEL-REGISTRY.md`](MODEL-REGISTRY.md) explains how provider/model roles are represented in the public runtime and how to reason about model retirement without treating documentation as an evergreen catalog.
- [`OPENROUTER_MODELS.md`](OPENROUTER_MODELS.md) is for readers configuring OpenRouter fallbacks and checking whether a model is suitable for ARTI's token and latency budgets.
- [`OBSERVER.md`](OBSERVER.md) documents the optional post-stream Observer/Curator pipeline and the boundary between shipped code and local session data.

## Vision / screen context

- [`SCOUTER.md`](SCOUTER.md) explains the compact conversation-digest path that can mark screen context as relevant before heavier vision work is considered.
- [`VISION-APIS.md`](VISION-APIS.md) is for readers configuring the main screenshot/vision provider chain, fallback behavior, capture limits, and trust boundaries.

## Avatar / VTube Studio / OBS

- [`VTS-ANIMATION.md`](VTS-ANIMATION.md) is the practical guide for connecting ARTI to VTube Studio and mapping logical states to your own model assets.
- [`Expression-Motion-System.md`](Expression-Motion-System.md) is the technical reference for expression overlays, nods, idle motion, parameter injection, and local verification boundaries.
- [`WIRING.md`](WIRING.md) also covers the general OBS/runtime prerequisites; this public revision does not ship a separate private OBS setup notebook.

## Behavior / conversation

- [`SPEC-arti-emotion.md`](SPEC-arti-emotion.md) defines the public wake-word and CONFIG-gated emotion behavior contract.
- [`SPEC-latency-cowatch.md`](SPEC-latency-cowatch.md) defines latency instrumentation and optional screen/co-watch behavior, including fail-open and trust-boundary expectations.

## Testing / technical references

- [`SMOKE-TEST-emotion.md`](SMOKE-TEST-emotion.md) is a local verification checklist for emotion, nod, and VTube Studio behavior after deterministic unit tests pass.
- [`SMOKE-TEST-cowatch.md`](SMOKE-TEST-cowatch.md) is a local verification checklist for screen capture, provider fallback, unchanged/dark-screen gating, and latency observation.

## Public release boundary

The repository ships public-safe templates such as `ARTI_SOUL.example.md`, `ARTI_VIEWERS.example.md`, and `ARTI_MOOD_STATE.example.json`. Copy them to their runtime names locally when needed. Real viewer profiles, transcripts, vault/session data, credentials, local configuration, VTube Studio tokens, and private model assets are intentionally excluded from Git.

Some integrations and development artifacts used outside this public release are also intentionally absent. Their absence is a release boundary, not an incomplete checkout: public docs should only reference files and integrations that are actually shipped here. The Stardew Valley integration, for example, is not included in this release.

See the root [`README.md`](../README.md) for the repository-level feature and verification boundary.
