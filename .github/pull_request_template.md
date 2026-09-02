## What changed

Describe the public-facing change and why it is needed.

## Validation

List the public-safe checks you ran, for example:

- [ ] `python -m compileall -q .`
- [ ] relevant `pytest` tests
- [ ] relevant Minecraft/Node checks
- [ ] documentation links/commands checked

## Verification scope

- [ ] I am not describing cloud/unit CI as `LOCAL_VERIFIED` or `VERIFIED LIVE`.
- [ ] Any behavior that still needs a real microphone, VTube Studio, OBS, GPU, or live game is called out explicitly.

## Privacy / publication check

- [ ] No credentials, tokens, cookies, or real `.env` / `config_local.json` values are included.
- [ ] No private viewer profiles, transcripts, chat/donation captures, session logs, RAG/vault data, telemetry dumps, or private fine-tuning data are included.
- [ ] Examples and fixtures are synthetic/public-safe.
- [ ] This PR does not import private development handoffs, task queues, raw research archives, or unpublished Git history.

## Optional integration impact

Note any provider, VTube Studio, OBS, Minecraft, or other integration compatibility impact. Write `None` if not applicable.
