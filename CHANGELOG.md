# Changelog

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
- PR #25 / OBS-2B2a terrain work.

Stardew Valley material is published only within paths explicitly cleared by publication review. Cloud-safe tests establish `UNIT_TESTED / CLOUD_VERIFIED` status only and are not represented as live/local verification.

## Earlier public snapshot

The repository previously contained the August 2026 ARTI public snapshot. Historical release details remain available in Git history; this changelog intentionally describes public product releases rather than private development archaeology.