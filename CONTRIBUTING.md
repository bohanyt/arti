# Contributing to ARTI

Thanks for helping improve the public ARTI project. This repository is a curated public distribution, not a mirror of the private development workspace.

## Good contribution areas

- bug fixes in the public runtime;
- public-safe tests and fixtures;
- setup and documentation improvements;
- compatibility fixes for supported integrations;
- small, well-scoped improvements to optional features.

For repository-specific agent guidance, also read [`AGENTS.md`](AGENTS.md).

## Before you start

1. Read the root [`README.md`](README.md) and relevant guide under [`docs/`](docs/README.md).
2. Keep optional integrations gated and fail-safe.
3. Do not introduce a new provider/network dependency without documenting why it is needed and how it is configured.
4. Keep real credentials and local identity/session values out of source.

## Privacy boundary

Do **not** attach or commit real:

- `.env` or `config_local.json` files;
- API keys, tokens, VTube Studio tokens, cookies, or credentials;
- viewer profiles, chat/donation captures, transcripts, stream/session logs, or private RAG/vault databases;
- screenshots/dumps containing private information;
- local machine paths or backup locations;
- private fine-tuning data or internal development handoffs.

Bug reports should use synthetic/minimized examples whenever possible. Redact secrets before posting logs or screenshots.

## Local setup

Create a Python 3.11 environment and install the core dependencies:

```bash
python -m pip install -r requirements.txt
```

For Minecraft:

```bash
cd mc-bot
npm ci
```

Feature-specific dependencies and wiring are documented in [`docs/WIRING.md`](docs/WIRING.md).

## Checks

Run the strongest public-safe checks relevant to your change. For Python changes, start with:

```bash
python -m compileall -q .
python -m pytest -q
```

For Minecraft JavaScript changes, run the checks exposed by `mc-bot/package.json` and at minimum syntax-check changed JavaScript files.

Some tests require local applications or hardware and are intentionally outside public cloud CI. A passing CI run should be described as `UNIT_TESTED` / `CLOUD_VERIFIED`, not as proof that OBS, VTube Studio, audio hardware, a GPU, or a live game works on a real machine.

## Pull requests

Keep PRs focused. In the description, include:

- what changed and why;
- which public-safe checks were run;
- any optional integration or compatibility impact;
- any behavior that still requires local/live validation.

Do not bundle unrelated private-workspace cleanup or unpublished development history into a public PR.

## Issues

Use the repository issue templates for bugs and feature requests. For suspected exposed credentials or sensitive private data, do not paste the sensitive value into a public issue; report only the minimum information needed to identify the affected public path/commit.
