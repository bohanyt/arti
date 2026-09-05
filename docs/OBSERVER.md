# Observer and Curator post-stream pipeline

Observer and Curator turn completed session transcripts into reviewable summary beats and approved memory material after a stream.

**Status:** the pipeline code is shipped publicly, but real transcripts, generated databases, session notes, and vault contents are local runtime data and are intentionally not included in the repository.

## Flow

```text
session transcript
    → Observer (segmentation + draft beats)
    → Curator (verification)
    → local beat output / observer database
    → approved vault memory
```

When `observer_shutdown_blocking` is enabled, shutdown waits for the configured Observer/Curator work to finish instead of silently abandoning it.

## Local runtime data

| Local data | Default path | Purpose |
|---|---|---|
| Observer audit database | `data/observer_rag.db` | Stores Observer/Curator beat records, including material that may not be promoted. |
| Live vault database | `data/vault_rag.db` | Stores approved memory used by the live RAG path. |

These are runtime-local paths. They are not public fixtures and should not be attached to public bug reports.

## Relevant configuration

- `observer_enabled` — master switch for the post-stream pipeline.
- `observer_segment_minutes` — target transcript segment size.
- `observer_provider_chain` — ordered text-provider chain for Observer work.
- `observer_shutdown_blocking` — whether shutdown waits for the pipeline to complete.

Exact defaults belong to the checked-out runtime configuration.

## Manual re-run

From the repository root:

```bash
python -c "import arti_observer_shutdown as o; from hermes_vtuber_bridge import CONFIG; o.run_observer_shutdown(CONFIG)"
```

Run this only against local session data you are comfortable processing.

## Health check

```bash
python bridge_health.py --deep
```

The deep health check can exercise configured Observer text-provider availability. It does not prove that a private transcript or local database contains correct semantic summaries.

## Verification boundary

Public CI can validate deterministic Python behavior, imports, and repository hygiene. It does not publish or process real stream transcripts, private viewer data, or local vault databases.

When reporting an Observer problem publicly, reproduce with synthetic transcript content and include only the minimum provider/status information needed to diagnose the failure.
