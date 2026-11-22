"""Observer RAG at reply time (brain batch B): synthetic DBs, fake HTTP only."""
from __future__ import annotations

import pytest
import requests

import arti_vault_rag as rag

BEATS = "vault/sessions/2026-09-02-default_beats.md"


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def _insert(cfg: dict, rows: list[tuple[str, str, list[float]]]) -> None:
    rag.init_db(cfg)
    conn = rag._connect(cfg)
    for i, (source, content, vec) in enumerate(rows):
        cur = conn.execute(
            "INSERT INTO chunks (source_path,source_type,folder,chunk_index,heading,"
            "content,content_hash,mtime,char_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source, "vault_session", "vault/sessions", i, "", content,
             f"{cfg['vault_rag_db_path']}-{i}", 1, len(content), 1),
        )
        conn.execute("INSERT INTO embeddings VALUES (?,?,?,?)",
                     (cur.lastrowid, "synthetic", len(vec), rag._pack_vector(vec)))
        rag._sync_fts(conn, cur.lastrowid)
    conn.commit()
    conn.close()


VAULT_ROWS = [
    ("vault/concepts/arti_live_learnings.md", "Catatan tetap sintetis: lampu kamar Arti warna biru.", [1.0, 0.10]),
    ("vault/concepts/arti_self_knowledge.md", "Arti tahu lampu panggung sintetis bisa diganti.", [1.0, 0.20]),
    (BEATS, "Momen sintetis yang sudah ada di vault: lampu studio mati sebentar.", [1.0, 0.15]),
]
OBS_ROWS = [
    (BEATS, "Momen sintetis yang sudah ada di vault: lampu studio mati sebentar.", [1.0, 0.15]),
    ("vault/sessions/2026-09-13-default_beats.md",
     "Momen stream sintetis: streamer memasang lampu baru dan Arti bercanda soal silau.", [1.0, 0.05]),
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def unexpected(*a, **kw):
        pytest.fail("Real HTTP is forbidden")

    monkeypatch.setattr(requests.sessions.Session, "request", unexpected)
    rag.clear_query_embed_cache()
    monkeypatch.setattr(rag, "_embedding_breaker_until", 0.0)
    monkeypatch.setattr(rag, "_embedding_probe_active", False)
    monkeypatch.setattr(rag, "_rerank_breaker_until", 0.0)
    monkeypatch.setattr(rag, "_rerank_probe_active", False)
    monkeypatch.setattr(rag, "_observer_breaker_until", 0.0)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = {
        **rag.DEFAULT_CONFIG,
        "vault_rag_db_path": str(tmp_path / "vault.db"),
        "observer_db_path": str(tmp_path / "observer.db"),
        "lmstudio_embedding_model": "synthetic",
        "vault_rag_rerank_enabled": True,
        "vault_rag_rerank_base_url": "http://rerank.invalid",
        "vault_rag_top_k": 3,
        "vault_rag_max_per_source": 1,
    }
    _insert(cfg, VAULT_ROWS)
    _insert({**cfg, "vault_rag_db_path": cfg["observer_db_path"]}, OBS_ROWS)
    calls = {"embed": 0, "rerank": 0, "docs": []}

    def embed(texts, config, **kw):
        calls["embed"] += 1
        return [[1.0, 0.0] for _ in texts]

    def rerank(url, **kw):
        calls["rerank"] += 1
        docs = kw["json"]["documents"]
        calls["docs"].append(docs)
        # stream moments first, so the merged candidate is visibly used
        return Response({"results": [
            {"index": i, "relevance_score": 10.0 if "streamer memasang" in d else 1.0 - i / 100}
            for i, d in enumerate(docs)]})

    monkeypatch.setattr(rag, "embed_texts", embed)
    monkeypatch.setattr(rag._rerank_session, "post", rerank)
    return cfg, calls


def test_merged_retrieval_one_embed_one_rerank(setup):
    cfg, calls = setup
    out = rag.append_rag_to_system("BASE", "lampu", cfg)
    assert calls["embed"] == 1, "query must be embedded once for both DBs"
    assert calls["rerank"] == 1, "vault + observer candidates share ONE rerank call"
    assert any("streamer memasang" in d for d in calls["docs"][0])
    assert "[momen stream 2026-09-13]" in out
    assert "[catatan tetap]" in out, "vault learnings keep their own label"


def test_search_tags_observer_hits(setup):
    cfg, _ = setup
    hits = rag.search("lampu", cfg, include_observer=True)
    kinds = [h.get("memory_kind") for h in hits]
    assert kinds[0] == "observer"
    assert len(hits) <= cfg["vault_rag_top_k"]


def test_identical_beats_chunk_counted_once(setup):
    cfg, calls = setup
    rag.search("lampu", {**cfg, "vault_rag_top_k": 10, "vault_rag_max_per_source": 0},
               include_observer=True)
    docs = calls["docs"][0]
    assert sum("sudah ada di vault" in d for d in docs) == 1


def test_default_search_and_kill_switch_are_vault_only(setup):
    cfg, calls = setup
    assert all(h.get("memory_kind") is None for h in rag.search("lampu", cfg))
    rag.clear_query_embed_cache()
    off = rag.append_rag_to_system("BASE", "lampu", {**cfg, "observer_rag_live_enabled": False})
    assert "streamer memasang" not in off
    assert not any("streamer memasang" in d for docs in calls["docs"] for d in docs)


def test_missing_observer_db_behaves_exactly_as_before(setup, tmp_path):
    cfg, _ = setup
    missing = tmp_path / "nope" / "observer.db"
    baseline = rag.append_rag_to_system("BASE", "lampu", {**cfg, "observer_rag_live_enabled": False})
    out = rag.append_rag_to_system("BASE", "lampu", {**cfg, "observer_db_path": str(missing)})
    assert out == baseline
    assert not missing.exists() and not missing.parent.exists(), "must not create a DB"


def test_broken_observer_db_fails_open_and_trips_breaker(setup, tmp_path, capsys):
    cfg, _ = setup
    bad = tmp_path / "broken.db"
    bad.write_bytes(b"not a sqlite database at all" * 10)
    baseline = rag.append_rag_to_system("BASE", "lampu", {**cfg, "observer_rag_live_enabled": False})
    broken = {**cfg, "observer_db_path": str(bad)}
    assert rag.append_rag_to_system("BASE", "lampu", broken) == baseline
    assert rag.append_rag_to_system("BASE", "lampu", broken) == baseline
    assert capsys.readouterr().out.count("[Observer RAG] Live skip") == 1, "breaker skips retries"
    assert rag._observer_breaker_until > 0


def test_embedding_failure_keeps_vault_lexical_only(setup, monkeypatch):
    cfg, calls = setup

    def offline(*a, **kw):
        raise requests.ConnectTimeout("fake offline embedding")

    monkeypatch.setattr(rag, "embed_texts", offline)
    hits = rag.search("lampu", cfg, include_observer=True)
    assert hits and all(h.get("lexical_fallback") for h in hits)
    assert all(h.get("memory_kind") is None for h in hits)
    assert calls["rerank"] == 0


def test_rerank_breaker_open_still_merges_without_extra_calls(setup, monkeypatch):
    cfg, calls = setup
    monkeypatch.setattr(rag, "_rerank_breaker_until", 1e18)
    hits = rag.search("lampu", {**cfg, "vault_rag_top_k": 4}, include_observer=True)
    assert calls["rerank"] == 0 and calls["embed"] == 1
    assert any(h.get("memory_kind") == "observer" for h in hits)


def test_injected_text_stays_within_budget(setup):
    cfg, _ = setup
    cap = 420
    out = rag.append_rag_to_system("BASE", "lampu", {**cfg, "vault_rag_max_context_chars": cap})
    assert out.startswith("BASE\n\n")
    assert len(out) - len("BASE\n\n") <= cap


def test_observer_model_mismatch_is_skipped(setup, tmp_path):
    cfg, _ = setup
    other = {**cfg, "vault_rag_db_path": str(tmp_path / "other_obs.db")}
    rag.init_db(other)
    conn = rag._connect(other)
    cur = conn.execute(
        "INSERT INTO chunks (source_path,source_type,folder,chunk_index,heading,"
        "content,content_hash,mtime,char_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (BEATS, "vault_session", "vault/sessions", 0, "", "Momen lampu model lain", "h", 1, 22, 1))
    conn.execute("INSERT INTO embeddings VALUES (?,?,?,?)",
                 (cur.lastrowid, "old-model", 3, rag._pack_vector([1.0, 0.0, 0.0])))
    rag._sync_fts(conn, cur.lastrowid)
    conn.commit()
    conn.close()
    hits = rag.search("lampu", {**cfg, "observer_db_path": other["vault_rag_db_path"]},
                      include_observer=True)
    assert hits and all(h.get("memory_kind") is None for h in hits)


def test_hit_label():
    assert rag.hit_label({"source_path": BEATS}) == "momen stream 2026-09-02"
    assert rag.hit_label({"source_path": "vault/sessions/2026-09-02-default.md"}) == "2026-09-02"
    assert rag.hit_label({"source_path": "vault/concepts/x.md"}) == "catatan tetap"
