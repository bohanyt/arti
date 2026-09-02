# Security policy

ARTI is a public software repository, but real streaming/runtime data is intentionally private.

## Reporting a vulnerability

Please do **not** open a public issue for a vulnerability that includes credentials, tokens, private viewer information, transcripts, donation/chat payloads, local configuration, machine paths, screenshots, or other sensitive runtime data.

If GitHub private vulnerability reporting is available for this repository, use the repository's **Security** tab to report the issue privately. Otherwise, contact the repository owner through the public GitHub profile without posting sensitive evidence publicly, and wait for a private channel before sharing secrets or private data.

For non-sensitive bugs, use the public bug-report form.

## Sensitive data boundary

Never attach or commit real instances of:

- `.env` or API/provider credentials;
- `config_local.json` or VTube Studio tokens;
- viewer profiles, transcripts, session logs, donation/chat captures, or private RAG/vault databases;
- screenshots, debug dumps, raw telemetry, machine-specific paths, or backup locations;
- private fine-tuning material, local evidence packets, or internal development handoffs.

When reproducing a bug, replace identities, URLs, IDs, paths, payloads, and credentials with synthetic placeholders.

## Supported public code

Security fixes should target the current public `main` branch. The public repository is a curated distribution and does not imply support for unpublished private development branches, private runtime data, or integrations explicitly marked as excluded/review-only.

## Verification

A security or privacy fix that passes public CI is `UNIT_TESTED` / `CLOUD_VERIFIED` only. Do not claim hardware/application/live-stream verification unless that exact scope has separate real-world evidence.
