# Changelog

## Public hardening — 2026-09-02

This pass deepens the September 2 public refresh after a repository-wide publication audit.

Public-facing changes include:

- connected the public repository README to `https://artiberarti.com` and rebuilt the public documentation index around files that are actually shipped;
- replaced stale documentation references that pointed to private/excluded plans, handoffs, tests, benchmarks, and helper scripts;
- generalized maintainer-bound defaults/prompts/comments that had survived the first sanitization pass;
- corrected Minecraft, VTube Studio, vision, scouter, emotion, co-watch, provider, TTS, and reflex documentation so fresh-clone setup no longer depends on unpublished material;
- added the public-safe `scripts/build_reflex_cache.py` helper required by the shipped reflex feature;
- added stronger publication CI checks for privacy, tracked-file references, and repository-local Python imports;
- added public regression coverage for expression parsing and the OpenRouter live model chain;
- fixed a latent OpenRouter fallback bug where disabling `openrouter_live_fast_only` with empty explicit model overrides could reference retired undefined names and raise `NameError`;
- clarified that Stardew Valley remains outside this public release boundary.

The OpenRouter fix is a narrowly scoped runtime correction discovered by the publication audit. Other maintainer-name and documentation changes are behavior-preserving publication transforms. Hardware/application integrations still require separate local verification.

## Public refresh — 2026-09-02

This release refreshes the public ARTI repository from the frozen private product baseline `f61f2e21ca1f66eaa8e73520cf384d9c767a9ae6` while preserving the public repository's independent Git history.

Public-facing changes in this refresh include:

- refreshed runtime and integration surfaces that pass publication review;
- current approved Minecraft integration and deterministic checks;
- safer public configuration examples and repository hygiene;
- public CI/privacy checks designed to avoid local hardware/application claims;
- curated product documentation that separates public usage from internal development history.

Not included:

- private handoffs, task queues, raw research/plans, transcripts, viewer/vault data, runtime logs/telemetry, machine backups, or private fine-tuning material;
- real credentials or local configuration;
- product changes newer than the frozen source baseline unless explicitly documented under the public-hardening section above;
- Stardew Valley runtime/SMAPI/test/evidence material;
- PR #25 / OBS-2B2a terrain work.

Cloud-safe tests establish `UNIT_TESTED / CLOUD_VERIFIED` status only and are not represented as live/local verification.

## Earlier public snapshot

The repository previously contained the August 2026 ARTI public snapshot. Historical release details remain available in Git history; this changelog intentionally describes public product releases rather than private development archaeology.
