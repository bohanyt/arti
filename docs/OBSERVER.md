# Observer + Kurator — Arti v0.6

Post-stream pipeline: segment full transcript → summarize beats → verify → vault.

## Flow

```
Ctrl+C → Observer (10min segments) → Kurator (verify) → beats.jsonl/md → observer_rag.db → vault_rag (approved)
```

Blocking shutdown with progress bar (`observer_shutdown_blocking: true`).

## Two databases

| DB | Path | Contents |
|----|------|----------|
| Observer (audit) | `data/observer_rag.db` | All beats including rejected |
| Live Arti | `data/vault_rag.db` | Approved vault + `*_beats.md` |

## CONFIG

- `observer_enabled` — master switch
- `observer_segment_minutes` — default 10
- `observer_provider_chain` — text LLM chain (no Groq)
- `observer_shutdown_blocking` — wait until pipeline done

## Manual re-run

```bash
python -c "import arti_observer_shutdown as o; from hermes_vtuber_bridge import CONFIG; o.run_observer_shutdown(CONFIG)"
```

## Health

```bash
python bridge_health.py --deep
```

Checks observer text models when `observer_provider_chain` is set.
