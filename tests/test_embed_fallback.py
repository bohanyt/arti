"""Vault embedding client: primary → fallback, both-down once, recovery. No network."""
from __future__ import annotations

import time

import pytest
import requests

import arti_vault_rag as rag
from arti_live7_status_facts import StatusFactsRelay


class _Resp:
    def __init__(self, status=200, embedding=(1.0,)):
        self.status_code = status
        self.text = "err"
        self._embedding = list(embedding)

    def json(self):
        return {"data": [{"index": 0, "embedding": self._embedding}]}


def _cfg(**over):
    return {
        "lmstudio_embedding_base_url": "http://192.0.2.50:1234/v1",
        "lmstudio_embedding_fallback_base_url": "http://127.0.0.1:1234/v1",
        "lmstudio_embedding_breaker_sec": 60.0,
        "lmstudio_embedding_timeout_sec": 2.0,
        "lmstudio_embedding_connect_timeout_sec": 0.5,
        **over,
    }


@pytest.fixture(autouse=True)
def _reset():
    rag.reset_embedding_client()
    yield
    rag.reset_embedding_client()


def test_primary_down_uses_fallback(monkeypatch, capsys):
    seen = []

    def post(url, **kw):
        seen.append(url)
        if "192.0.2.50" in url:
            raise requests.ConnectionError("primary down")
        return _Resp()

    monkeypatch.setattr(rag._embedding_session, "post", post)
    assert rag.embed_texts(["q"], _cfg()) == [[1.0]]
    assert any("192.0.2.50" in u for u in seen) and any("127.0.0.1" in u for u in seen)
    assert rag.embedding_status()["base"] == "http://127.0.0.1:1234/v1"
    assert rag.embedding_status()["available"] is True
    out = capsys.readouterr().out
    assert out.count("[Vault RAG] embed endpoint:") == 1
    assert "fallback" in out


def test_both_down_status_once_no_exception_storm(monkeypatch, capsys):
    calls = []

    def post(url, **kw):
        calls.append(url)
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(rag._embedding_session, "post", post)
    with pytest.raises(requests.ConnectionError):
        rag.embed_texts(["q"], _cfg())
    n = len(calls)
    assert n >= 2  # primary (+ retry) and fallback
    with pytest.raises(requests.ConnectionError, match="breaker open"):
        rag.embed_texts(["q"], _cfg())
    with pytest.raises(requests.ConnectionError, match="breaker open"):
        rag.embed_texts(["q"], _cfg())
    assert len(calls) == n  # no further posts
    st = rag.embedding_status()
    assert st["available"] is False and st["fails"] == 1
    assert capsys.readouterr().out.count("[Vault RAG] embed endpoint:") == 1


def test_recovery_after_backoff_reprobes(monkeypatch):
    state = {"up": False}

    def post(url, **kw):
        if not state["up"]:
            raise requests.ConnectionError("offline")
        if "192.0.2.50" in url:
            raise requests.ConnectionError("primary still down")
        return _Resp(embedding=(2.0,))

    monkeypatch.setattr(rag._embedding_session, "post", post)
    with pytest.raises(requests.ConnectionError):
        rag.embed_texts(["q"], _cfg())
    assert rag.embedding_breaker_open()
    with rag._embedding_breaker_lock:
        rag._embedding_breaker_until = time.monotonic() - 1.0
        rag._embedding_probe_active = False
    state["up"] = True
    assert rag.embed_texts(["q"], _cfg()) == [[2.0]]
    assert rag.embedding_status()["base"] == "http://127.0.0.1:1234/v1"
    assert rag.embedding_status()["available"] is True
    assert not rag.embedding_breaker_open()


def test_probe_primary_then_fallback_without_network():
    hits = []

    def get(url, **kw):
        hits.append(url)

        class R:
            status_code = 200 if "127.0.0.1" in url else 0
        if "192.0.2.50" in url:
            raise requests.ConnectionError("nope")
        return R()

    got = rag.probe_embedding_endpoints(_cfg(), get=get, timeout=0.2)
    assert got["ok"] is True and got["using"] == "fallback"
    assert any("192.0.2.50" in u for u in hits)
    dead = rag.probe_embedding_endpoints(_cfg(), get=lambda url, **kw: (_ for _ in ()).throw(requests.Timeout()),
                                         timeout=0.2)
    assert dead["ok"] is False


def test_session_status_carries_fail_count(monkeypatch):
    monkeypatch.setattr(rag, "embedding_breaker_open", lambda: True)
    monkeypatch.setattr(rag, "embedding_status", lambda: dict(base=None, fails=20, available=False))
    relay = StatusFactsRelay(dict(livepath_mode="mabar", vault_rag_enabled=True), None, clock=lambda: 0.0)
    relay._publish()
    block = relay.status_block()
    assert "Ingatan vault-mu lagi mati sementara" in block and "gagal 20 kali" in block
