"""Memory hooks use synthetic documents and fake HTTP only."""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest
import requests

import arti_observer_rag as observer
import arti_vault_rag as rag


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


@pytest.fixture(autouse=True)
def isolated_http(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Real HTTP is forbidden in memory hook tests")

    monkeypatch.setattr(requests.sessions.Session, "request", unexpected)
    rag.clear_query_embed_cache()
    monkeypatch.setattr(rag, "_embedding_breaker_until", 0.0)
    monkeypatch.setattr(rag, "_embedding_probe_active", False)
    # raising=False also lets the tests demonstrate the missing hook on the base.
    monkeypatch.setattr(rag, "_rerank_breaker_until", 0.0, raising=False)
    monkeypatch.setattr(rag, "_rerank_probe_active", False, raising=False)


@pytest.fixture
def index(tmp_path, monkeypatch):
    cfg = {
        **rag.DEFAULT_CONFIG,
        "vault_rag_db_path": str(tmp_path / "synthetic.db"),
        "lmstudio_embedding_model": "synthetic",
        "vault_rag_rerank_enabled": True,
        "vault_rag_rerank_base_url": "http://rerank.invalid",
        "vault_rag_live_timeout_sec": 8.0,
        "vault_rag_max_per_source": 1,
    }
    rag.init_db(cfg)
    conn = rag._connect(cfg)
    for i in range(24):
        content = f"synthetic lampu document {i}"
        source = "same.md" if i in (1, 2) else f"doc-{i}.md"
        cur = conn.execute(
            "INSERT INTO chunks (source_path,source_type,folder,chunk_index,heading,"
            "content,content_hash,mtime,char_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source, "note", "test", i, "", content, f"hash-{i}", 1, len(content), 1),
        )
        conn.execute("INSERT INTO embeddings VALUES (?,?,?,?)",
                     (cur.lastrowid, "synthetic", 2, rag._pack_vector([1.0, i / 25])))
        rag._sync_fts(conn, cur.lastrowid)
    conn.commit()
    conn.close()

    def embed(*args, **kwargs):
        assert not rag._rag_search_lock.locked()
        return Response({"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    monkeypatch.setattr(rag._embedding_session, "post", embed)
    return cfg


def rerank_reply(payload, scores=None):
    n = payload["top_n"]
    return Response({"results": [{"index": i, "relevance_score":
                                  scores[i] if scores is not None else float(i)}
                                 for i in reversed(range(n))]})


def test_rerank_raw_question_top_twenty_before_diversity(index, monkeypatch, caplog):
    # Disable diversity for the baseline so document indexes match HTTP indexes.
    baseline = rag.search("lampu", {**index, "vault_rag_rerank_enabled": False,
                                     "vault_rag_max_per_source": 0}, top_k=24)
    seen = []

    def post(url, **kw):
        assert not rag._rag_search_lock.locked()
        seen.append((url, kw))
        return rerank_reply(kw["json"], [0, 10, 9] + [-1] * 17)

    monkeypatch.setattr(rag._rerank_session, "post", post)
    with caplog.at_level(logging.DEBUG, logger=rag.__name__):
        hits = rag.search("lampu", index, top_k=2, rerank_query="private raw question")
    url, kw = seen[0]
    assert url == "http://rerank.invalid/v1/rerank"
    assert kw["json"] == {"query": "private raw question", "documents":
                           [h["content"] for h in baseline[:20]], "top_n": 20}
    assert kw["timeout"] == (2.0, 1.5)
    assert hits[0]["content"] == baseline[1]["content"]
    assert hits[1]["source_path"] != "same.md"
    assert hits[0]["score"] == baseline[1]["score"]
    assert hits[0]["rerank_score"] == 10
    assert "rerank top_score=10.0000" in caplog.text
    assert "private raw question" not in caplog.text
    assert rag._rerank_session is not rag._embedding_session


@pytest.mark.parametrize("overlay", [{"vault_rag_rerank_enabled": False},
                                     {"vault_rag_rerank_base_url": ""}])
def test_rerank_disabled_or_unconfigured_keeps_hybrid(index, overlay):
    expected = rag.search("lampu", {**index, "vault_rag_rerank_enabled": False})
    assert rag.search("lampu", {**index, **overlay}) == expected


def test_rerank_top_n_and_tail_preserve_hybrid_scores(index, monkeypatch):
    cfg = {**index, "vault_rag_rerank_top_n": 3, "vault_rag_max_per_source": 0}
    baseline = rag.search("lampu", {**cfg, "vault_rag_rerank_enabled": False}, top_k=24)

    def post(url, **kw):
        assert kw["json"]["query"] == "lampu"
        assert kw["json"]["top_n"] == 3
        return rerank_reply(kw["json"])

    monkeypatch.setattr(rag._rerank_session, "post", post)
    hits = rag.search("lampu", cfg, top_k=24)
    assert [h["content"] for h in hits] == [h["content"] for h in baseline[:3]][::-1] + [h["content"] for h in baseline[3:]]
    assert hits[0]["score"] == baseline[2]["score"]
    assert all("rerank_score" not in h for h in hits[3:])


@pytest.mark.parametrize("threshold, count", [(None, 24), (0.0, 2), (0.5, 1), (2, 0)])
def test_optional_rerank_gate_does_not_backfill_unscored_tail(index, monkeypatch, threshold, count):
    cfg = {**index, "vault_rag_rerank_top_n": 3, "vault_rag_rerank_min_score": threshold}
    monkeypatch.setattr(rag._rerank_session, "post",
                        lambda url, **kw: rerank_reply(kw["json"], [-1, 0, 1]))
    assert len(rag.search("lampu", cfg, top_k=24)) == count


@pytest.mark.parametrize("failure, expected_calls", [
    (requests.ConnectTimeout("fake connect"), 1),
    (requests.ReadTimeout("fake read"), 1),
    (requests.ConnectionError("fake dropped connection"), 2),
    (ValueError("fake JSON"), 1),
    (RuntimeError("fake error"), 1),
])
def test_rerank_failure_opens_own_breaker_and_fails_open(index, monkeypatch, failure, expected_calls):
    expected = rag.search("lampu", {**index, "vault_rag_rerank_enabled": False})
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        raise failure

    monkeypatch.setattr(rag._rerank_session, "post", post)
    cfg = {**index, "vault_rag_rerank_min_score": 100}
    assert rag.search("lampu", cfg) == expected
    assert rag.search("lampu", cfg) == expected
    assert len(calls) == expected_calls
    assert rag._rerank_breaker_until > time.monotonic()
    assert not rag.embedding_breaker_open()


@pytest.mark.parametrize("body", [None, {}, {"results": []},
    {"results": [{"index": 0, "relevance_score": 2}] * 3},
    {"results": [{"index": i, "relevance_score": float("nan")} for i in range(3)]},
    {"results": [{"index": i + 1, "relevance_score": 1} for i in range(3)]},
    {"results": [{"index": i, "relevance_score": "oops"} for i in range(3)]},
])
def test_malformed_rerank_response_keeps_exact_order(index, monkeypatch, body):
    cfg = {**index, "vault_rag_rerank_top_n": 3}
    expected = rag.search("lampu", {**cfg, "vault_rag_rerank_enabled": False})
    monkeypatch.setattr(rag._rerank_session, "post", lambda *a, **kw: Response(body))
    assert rag.search("lampu", cfg) == expected


@pytest.mark.parametrize("status", [400, 429, 500, 503])
def test_rerank_http_failure_keeps_order(index, monkeypatch, status):
    expected = rag.search("lampu", {**index, "vault_rag_rerank_enabled": False})
    response = Response({})
    response.status_code = status
    monkeypatch.setattr(rag._rerank_session, "post", lambda *a, **kw: response)
    assert rag.search("lampu", index) == expected


def test_rerank_stale_keepalive_retries_once_and_closes_probe(index, monkeypatch):
    calls = []
    monkeypatch.setattr(rag, "_rerank_breaker_until", time.monotonic() - 1)

    def post(url, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise requests.ConnectionError("RemoteDisconnected")
        return rerank_reply(kw["json"])

    monkeypatch.setattr(rag._rerank_session, "post", post)
    assert "rerank_score" in rag.search("lampu", index)[0]
    assert len(calls) == 2
    assert rag._rerank_breaker_until == 0
    assert not rag._rerank_probe_active


def test_only_one_half_open_rerank_probe(index, monkeypatch):
    baseline = rag.search("lampu", {**index, "vault_rag_rerank_enabled": False})
    monkeypatch.setattr(rag, "_rerank_breaker_until", time.monotonic() - 1)

    def post(url, **kw):
        assert rag._rerank_probe_active
        assert rag.search("lampu", index) == baseline
        return rerank_reply(kw["json"])

    monkeypatch.setattr(rag._rerank_session, "post", post)
    assert "rerank_score" in rag.search("lampu", index)[0]


@pytest.mark.parametrize("embed_elapsed, calls", [(0.2, 1), (0.95, 0), (1.1, 0)])
def test_live_budget_shared_by_embedding_and_rerank(index, monkeypatch, embed_elapsed, calls):
    clock = [100.0]
    monkeypatch.setattr(rag, "time", SimpleNamespace(monotonic=lambda: clock[0],
                                                    perf_counter=lambda: clock[0]))
    seen = []

    def embed(url, **kw):
        assert sum(kw["timeout"]) < 1.0
        clock[0] += embed_elapsed
        return Response({"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    def post(url, **kw):
        seen.append(kw)
        assert sum(kw["timeout"]) < 1.0 - embed_elapsed
        return rerank_reply(kw["json"])

    monkeypatch.setattr(rag._embedding_session, "post", embed)
    monkeypatch.setattr(rag._rerank_session, "post", post)
    hits = rag.search("lampu", {**index, "vault_rag_live_timeout_sec": 1.0})
    assert hits
    assert len(seen) == calls


def test_rerank_retry_uses_remaining_budget(index, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(rag, "time", SimpleNamespace(monotonic=lambda: clock[0],
                                                    perf_counter=lambda: clock[0]))
    calls = []

    def post(url, **kw):
        calls.append(kw["timeout"])
        if len(calls) == 1:
            clock[0] += 0.6
            raise requests.ConnectionError("dropped keepalive")
        assert sum(kw["timeout"]) < 0.4
        return rerank_reply(kw["json"])

    monkeypatch.setattr(rag._rerank_session, "post", post)
    assert "rerank_score" in rag.search("lampu", {**index, "vault_rag_live_timeout_sec": 1.0})[0]
    assert len(calls) == 2


def test_embedding_retry_uses_remaining_live_budget(index, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(rag, "time", SimpleNamespace(monotonic=lambda: clock[0],
                                                    perf_counter=lambda: clock[0]))
    calls = []

    def post(url, **kw):
        calls.append(kw["timeout"])
        if len(calls) == 1:
            clock[0] += 0.6
            raise requests.ConnectionError("dropped keepalive")
        assert sum(kw["timeout"]) < 0.4
        return Response({"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    monkeypatch.setattr(rag._embedding_session, "post", post)
    assert rag.search("lampu", {**index, "vault_rag_rerank_enabled": False,
                                "vault_rag_live_timeout_sec": 1.0})
    assert len(calls) == 2


def test_query_prefix_and_cache_identity(monkeypatch):
    inputs = []

    def post(url, **kw):
        inputs.append(kw["json"]["input"])
        return Response({"data": [{"index": 0, "embedding": [len(inputs), 0]}]})

    monkeypatch.setattr(rag._embedding_session, "post", post)
    cfg = {"lmstudio_embedding_query_prefix": "task: search result | query: "}
    assert rag.embed_query_cached("lampu", cfg) == [1, 0]
    assert rag.embed_query_cached("lampu", cfg) == [1, 0]
    assert rag.embed_query_cached("lampu", {}) == [2, 0]
    assert inputs == [["task: search result | query: lampu"], ["lampu"]]


@pytest.mark.parametrize("observer_path", [False, True])
@pytest.mark.parametrize("prefix", ["", "title: none | text: "])
def test_document_prefix_only_on_wire_and_observer_query_prefix(tmp_path, monkeypatch, observer_path, prefix):
    monkeypatch.setattr(rag, "_ROOT", tmp_path)
    monkeypatch.setattr(observer, "_ROOT", tmp_path)
    source = tmp_path / "vault" / "sessions" / "synthetic_beats.md"
    source.parent.mkdir(parents=True)
    content = "Lampu sintetis bersinar biru ketika pengujian fiktif dimulai di panggung kosong."
    source.write_text("# Synthetic\n" + content, encoding="utf-8")
    wire = []

    def post(url, **kw):
        wire.extend(kw["json"]["input"])
        return Response({"data": [{"index": i, "embedding": [1.0, 0.0]}
                                   for i, _ in enumerate(kw["json"]["input"])]})

    monkeypatch.setattr(rag._embedding_session, "post", post)
    cfg = {"vault_rag_db_path": str(tmp_path / "vault.db"),
           "observer_db_path": str(tmp_path / "observer.db"),
           "vault_rag_index_globs": ["vault/**/*.md"],
           "lmstudio_embedding_doc_prefix": prefix,
           "lmstudio_embedding_query_prefix": "query: "}
    if observer_path:
        stats = observer.reindex_beats_session("synthetic", cfg)
        cfg = observer._observer_cfg(cfg)
    else:
        stats = rag.reindex_all(cfg, verbose=False)
    assert stats["chunks_new"] == 1
    assert wire == [prefix + content]
    conn = rag._connect(cfg)
    assert conn.execute("SELECT content FROM chunks").fetchone()[0] == content
    assert conn.execute("SELECT content FROM chunks_fts").fetchone()[0] == content
    conn.close()
    assert rag.search("lampu", cfg)
    assert wire[-1] == "query: lampu"


@pytest.mark.parametrize("question, enrich", [
    ("arti mulai ada sejak kapan", True),
    ("arti mulai sejak kapan", True),
    ("Kapan ARTI debut?", True),
    ("ceritain asal-usul arti dong", True),
    ("sejarah Arti gimana?", True),
    ("Arti, kamu lahir kapan?", True),
    ("sejak kapan kamu ada?", True),
    ("arti pertama kali siaran kapan", True),
    ("arti kapan pertama kali siaran?", True),
    ("kapan pertama kali arti siaran?", True),
    ("arti mulai siaran sejak kapan?", True),
    ("kapan kamu mulai jadi co-host?", True),
    ("berarti mulai sejak kapan", False),
    ("berarti kapan debutnya", False),
    ("arti kapanpun bisa main", False),
    ("arti adalah AI", False),
    ("arti ada kucing di rumah?", False),
    ("arti kapan kita main minecraft lagi", False),
    ("arti mulai main stardew sejak kapan", False),
    ("arti mulai stream jam berapa hari ini?", False),
    ("arti mulai kapan main minecraft?", False),
    ("arti masih ingat portal kemarin?", False),
    ("arti itu kapan tuh", False),
    ("kapan terakhir kita main elden ring", False),
    ("hari apa kita nonton anime", False),
    ("pertama kali kita ke nether kapan", False),
    ("arti, kapan Bohan lahir?", False),
    ("arti ingat nggak debut Kobo?", False),
    ("halo arti", False),
    ("", False),
])
def test_origin_enrichment_requires_tokens_and_origin_intent(question, enrich):
    result = rag.enrich_rag_query(question)
    assert (result != question) is enrich


def test_append_passes_raw_question_to_rerank(monkeypatch):
    calls = []

    def search(query, config, **kwargs):
        calls.append((query, kwargs))
        return []

    monkeypatch.setattr(rag, "search", search)
    monkeypatch.setattr(rag, "get_canon_origin_block", lambda cfg: "synthetic canon")
    rag.append_rag_to_system("BASE", "arti mulai sejak kapan", {})
    assert "arti_origin" in calls[0][0]
    # include_observer: observer RAG joins the reply retrieval (brain batch B).
    # include_wants: ARTI's open wants join the same retrieval (self+motivation, 2026-10-22).
    assert calls[0][1] == {"rerank_query": "arti mulai sejak kapan", "include_observer": True,
                           "include_wants": True}


@pytest.mark.parametrize("question", ["arti masih ingat portal kemarin?",
                                     "hari apa kita nonton anime", "arti itu kapan tuh"])
def test_general_memory_question_does_not_inject_origin_fallback(monkeypatch, question):
    monkeypatch.setattr(rag, "search", lambda *a, **kw: [])
    monkeypatch.setattr(rag, "get_canon_origin_block", lambda cfg: pytest.fail("Unrelated origin fallback"))
    assert rag.append_rag_to_system("BASE", question, {}) == "BASE"


def test_embedding_failure_keeps_lexical_fallback_without_rerank(index, monkeypatch):
    def offline(*a, **kw):
        raise requests.ConnectTimeout("fake offline embedding")

    monkeypatch.setattr(rag._embedding_session, "post", offline)
    hits = rag.search("lampu", index)
    assert hits and all(h["lexical_fallback"] for h in hits)


def test_rerank_response_after_deadline_is_discarded(index, monkeypatch):
    expected = rag.search("lampu", {**index, "vault_rag_rerank_enabled": False})
    clock = [100.0]
    monkeypatch.setattr(rag, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def post(url, **kw):
        clock[0] += 9.0
        return rerank_reply(kw["json"])

    monkeypatch.setattr(rag._rerank_session, "post", post)
    assert rag.search("lampu", index) == expected
