# Scouter — screen context helper

Scouter is ARTI's compact screen-understanding path: capture a screen frame, ask a configured vision provider for a concise description, and expose that context to the conversation runtime.

The implementation lives in [`arti_scouter_client.py`](../arti_scouter_client.py). Provider availability changes over time, so this document describes the **shipped routing contract**, not a promise that every external provider is currently free or online.

## Default provider order

The public source currently defines this default Scouter chain:

```text
google_gemini
→ openrouter
→ cloudflare
→ ollama
→ zai
→ nvidia
```

A provider is skipped/fails forward when it is not configured, rejects the request, times out, or otherwise cannot return a usable result.

GitHub Models is intentionally treated as a retired Scouter provider in the current client; do not add it back to local configuration expecting the public default path to use it.

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

Not every provider requires every variable above, and local Ollama-compatible setups may use local endpoints instead of a hosted credential. See [`.env.example`](../.env.example) for the public placeholders actually shipped by the repository.

Never commit the real `.env`.

## Local configuration

Provider order and model choices can be overridden by the runtime configuration. Keep machine/account-specific choices in `config_local.json` rather than editing credentials into source.

The default chain should be understood as a fallback strategy, not a benchmark ranking. Latency, quotas, model names, and free tiers are external service properties and can change without a repository update.

## Output contract

Scouter is designed to return a small piece of context suitable for a live conversation turn rather than a long OCR/vision report. The bridge can then decide whether the result is fresh/relevant enough to inject into ARTI's prompt.

Screen context is untrusted external input. The runtime should treat text visible on screen as observed content, not as privileged system instructions.

## Relationship to the broader vision path

Scouter is not the only vision-related module in ARTI. The main vision path and its model/configuration keys are documented in [`VISION-APIS.md`](VISION-APIS.md).

The two paths may have different provider identifiers/order because they solve different latency/context tasks. Always inspect the corresponding public source before assuming they share a chain.

## Local smoke check

1. Put the credential for one supported provider in `.env`.
2. Keep the other providers unset if you want to isolate that path.
3. Start ARTI locally and trigger the feature that requests Scouter context.
4. Confirm a concise screen description is returned.
5. Disable/break that provider locally and confirm the chain fails forward rather than crashing the bridge.

Public CI does not send screenshots to external model APIs, so real provider calls remain local verification.

## Privacy

A captured desktop frame may contain private messages, account information, browser tabs, tokens, or personal data. Do not attach real captures or raw Scouter payloads to public issues unless they are intentionally synthetic and safe to publish.
