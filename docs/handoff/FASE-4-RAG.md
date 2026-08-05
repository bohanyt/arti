# Fase 4 — Vault RAG (LM Studio + SQLite)

## Apa ini

- **Index offline:** semua `vault/**/*.md`, `docs/handoff`, `ARTI_*.md`, `transcripts/*.jsonl`
- **Embed:** LM Studio `POST /v1/embeddings` (mxbai embed large)
- **Store:** `data/vault_rag.db` (SQLite + vector BLOB + FTS5 hybrid)
- **Live RAG:** top-5 chunk → inject `[VAULT RAG ...]` ke system prompt Arti
- **RAG lite:** chunk relevan juga masuk prompt **reflection** post-stream

## Setup (sekali)

1. LM Studio → load **text-embedding-mxbai-embed-large-v1**
2. **Start Local Server** (port 1234)
3. Reindex historis:

```powershell
cd "C:\Users\<user>\Documents\hermes-vtuber-host"
python arti_vault_rag.py --reindex-all
```

4. Tes search:

```powershell
python arti_vault_rag.py --query "lampu Param178"
```

## CONFIG (hermes_vtuber_bridge.py)

- `vault_rag_enabled` / `vault_rag_live_enabled` / `vault_rag_lite_enabled`
- `lmstudio_embedding_base_url`, `lmstudio_embedding_model`
- `vault_rag_top_k`, `vault_rag_max_context_chars`

## Setelah stream baru

Jalankan ulang (incremental, skip file tidak berubah):

```powershell
python arti_vault_rag.py --reindex-all
```

Atau `--force` untuk re-embed semua.
