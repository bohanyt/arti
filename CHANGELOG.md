# Changelog

## Public documentation polish — 2026-09-02

- connected the public repository README to `https://artiberarti.com` and the public documentation index;
- replaced stale documentation references that pointed to private/excluded plans and handoffs;
- corrected the public wiring guide so it references the actual shipped bridge/config paths and no longer points to unpublished local voice-lab artifacts;
- clarified that Stardew Valley is not included in this public release.

No runtime behavior changed in this documentation-only maintenance pass.

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
- product changes newer than the frozen source baseline;
- Stardew Valley runtime/SMAPI/test/evidence material;
- PR #25 / OBS-2B2a terrain work.

Cloud-safe tests establish `UNIT_TESTED / CLOUD_VERIFIED` status only and are not represented as live/local verification.

## Earlier public snapshot

The repository previously contained the August 2026 ARTI public snapshot. Historical release details remain available in Git history; this changelog intentionally describes public product releases rather than private development archaeology.
