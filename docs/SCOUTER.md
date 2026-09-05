# Scouter conversation and screen-relevance helper

Scouter produces a compact digest of recent streamer/chat context and can mark whether the conversation appears to require fresh screen context. It is a lightweight gating/summarization path, not the main screenshot-description pipeline.

**Status:** this document describes the provider chain and output contract shipped in the checked-out revision. Provider availability, quotas, and model catalogs remain external and must be verified locally.

The implementation lives in [`arti_scouter_client.py`](../arti_scouter_client.py). For the heavier screenshot/vision path, see [`VISION-APIS.md`](VISION-APIS.md).

## Default provider order

The public Scouter client currently defines this default chain:

```text
google_gemini
→ openrouter
→ cloudflare
→ ollama
→ zai
→ nvidia
```

The chain is ordered fallback, not a benchmark ranking. A provider can be skipped or fail forward when it is retired, unconfigured, quota-gated, times out, errors, or does not return a usable result. If no provider succeeds within the configured budget, callers should continue without pretending a fresh Scouter digest exists.

GitHub Models is treated as retired by the current Scouter resolver and is not part of the supported chain.

## Credentials

Configure only the providers you intend to use in `.env`. Common variables include:

```text
GEMINI_API_KEY
OPENROUTER_API_KEY
CLOUDFLARE_API_TOKEN
CLOUDFLARE_ACCOUNT_ID
ZAI_API_KEY
NVIDIA_API_KEY
OLLAMA_API_KEY
```

Not every provider requires every variable above, and local Ollama-compatible setups may use local endpoints. See [`.env.example`](../.env.example) for the public placeholders shipped by the repository.

Never commit the real `.env`.

## Local configuration

Provider order and model choices can be overridden by runtime configuration. Keep machine/account-specific choices in `config_local.json` rather than editing credentials into source.

The default chain should be read as fallback policy. Latency, quotas, model names, and free tiers can change independently of the repository.

## Output contract

Scouter returns a compact structured digest suitable for a live conversation path. The public result includes summary/topic/emotion fields plus screen-related signals such as `screen_relevant` and `screen_hint`.

Those screen signals can help the broader runtime decide whether fresh screenshot context is worth requesting. They are not themselves proof that the screen was captured or visually inspected.

Observed chat/screen text remains untrusted input and must not be treated as privileged system instructions.

## Relationship to the main vision path

[`arti_vision_client.py`](../arti_vision_client.py) owns the main screenshot/vision chain documented in [`VISION-APIS.md`](VISION-APIS.md).

Scouter and Vision intentionally use different provider identifiers/order because they solve different latency and context tasks. Both follow the same fallback principle: skip/fail forward while budget remains, then continue without fresh optional context if the chain is exhausted.

## Local smoke check

1. Configure one supported Scouter provider.
2. Keep later providers unset if you want to isolate that path.
3. Start ARTI locally and generate enough recent conversation context to trigger Scouter.
4. Confirm a compact digest is returned.
5. Use a conversation that clearly refers to the screen and confirm the screen-relevance fields behave sensibly.
6. Disable or break the first provider and confirm the chain can fail forward without crashing the bridge.

Public CI does not make live external-provider calls, so provider availability remains local verification.

## Privacy

Recent chat/context can contain private messages, account information, viewer identifiers, or personal data. Do not attach raw Scouter inputs/outputs to public issues unless the content is intentionally synthetic and safe to publish.
