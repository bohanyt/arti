"""Vault RAG unit tests (no LM Studio required)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_vault_rag as rag


def test_embed_query_cached_reuses_vector(monkeypatch):
    calls: list[int] = []

    def fake_embed(texts, config):
        calls.append(1)
        return [[1.0, 0.0]]

    monkeypatch.setattr(rag, "embed_texts", fake_embed)
    rag.clear_query_embed_cache()
    cfg = dict(rag.DEFAULT_CONFIG)
    assert rag.embed_query_cached("lampu", cfg) == [1.0, 0.0]
    assert rag.embed_query_cached("lampu", cfg) == [1.0, 0.0]
    assert len(calls) == 1


def test_chunk_text_skips_junk_overlap_fragments():
    log = "[16:56:22] [Streamer] Terima kasih. " * 30
    chunks = rag.chunk_text(log, chunk_chars=200, overlap=60)
    for _, body in chunks:
        assert not rag._is_junk_chunk(body, 48)
        assert "asih." not in body or len(body) > 80


def test_chunk_text_splits_long_paragraph():
    text = "A" * 900
    chunks = rag.chunk_text(text, chunk_chars=200, overlap=20)
    assert len(chunks) >= 3
    assert all(len(c[1]) <= 220 for c in chunks)


def test_search_with_mock_embeddings(tmp_path, monkeypatch):
    db = tmp_path / "rag.db"
    cfg = {
        **rag.DEFAULT_CONFIG,
        "vault_rag_db_path": str(db),
        "vault_rag_min_score": 0.1,
    }
    rag.init_db(cfg)
    conn = rag._connect(cfg)
    conn.execute(
        """
        INSERT INTO chunks (
            source_path, source_type, folder, chunk_index, heading,
            content, content_hash, mtime, char_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "vault/concepts/test.md",
            "vault_concept",
            "vault/concepts",
            0,
            "Lampu",
            "Param178 mengatur lampu kepala Arti di VTS.",
            "hash1",
            1.0,
            40,
            1.0,
        ),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    conn.execute(
        "INSERT INTO embeddings (chunk_id, model, dim, vector) VALUES (?, ?, ?, ?)",
        (cid, "test", 3, rag._pack_vector(vec.tolist())),
    )
    rag._sync_fts(conn, cid)
    conn.commit()
    conn.close()

    def fake_embed(texts, config):
        if "lampu" in texts[0].lower():
            return [[1.0, 0.0, 0.0]]
        return [[0.0, 1.0, 0.0]]

    monkeypatch.setattr(rag, "embed_texts", fake_embed)
    hits = rag.search("lampu kepala", cfg, top_k=3)
    assert hits
    assert "Param178" in hits[0]["content"]


def test_format_hits_for_prompt():
    hits = [
        {
            "score": 0.9,
            "source_path": "vault/x.md",
            "source_type": "vault_concept",
            "heading": "H",
            "content": "isi penting",
        }
    ]
    out = rag.format_hits_for_prompt(hits, 500)
    assert "[VAULT RAG" in out
    assert "isi penting" in out


def test_enrich_rag_query_history():
    q = rag.enrich_rag_query("arti mulai ada sejak kapan")
    assert "27 Mei 2026" in q
    assert "vault sessions" in q


def test_enrich_rag_query_plain_unchanged():
    assert rag.enrich_rag_query("halo arti") == "halo arti"


def test_append_rag_history_fallback_injects_canon(monkeypatch):
    cfg = dict(rag.DEFAULT_CONFIG)
    cfg["vault_rag_enabled"] = True
    cfg["vault_rag_live_enabled"] = True
    cfg["vault_rag_history_min_score"] = 0.99

    monkeypatch.setattr(rag, "search", lambda q, c, top_k=None: [{"score": 0.1, "source_path": "vault/other.md", "source_type": "vault", "heading": "", "content": "x"}])
    monkeypatch.setattr(rag, "get_canon_origin_block", lambda c: "[VAULT RAG — ASAL USUL (kanon)]\nDebut 27 Mei 2026")

    out = rag.append_rag_to_system("BASE", "arti mulai sejak kapan", cfg)
    assert "ASAL USUL (kanon)" in out
    assert "27 Mei 2026" in out
