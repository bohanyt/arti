"""Fallback logs describe attempts; the returned provider owns the reply."""
import asyncio
from types import SimpleNamespace as NS


def test_empty_fallback_then_codex_does_not_claim_groq_served(monkeypatch, capsys):
    import arti_bridge as b
    import arti_cursor_agent as cursor
    import arti_codex_agent as codex
    monkeypatch.setattr(cursor, "prewarm", lambda cfg: False)
    monkeypatch.setattr(codex, "prewarm", lambda cfg: True)
    answers = iter([None, "Recovered Codex reply"])
    monkeypatch.setattr(codex, "send_turn", lambda *a: next(answers))
    monkeypatch.setattr(b, "groq_chat_completion", lambda *a: (None, None))
    cfg = {**b.CONFIG, "codex_primary_voice": True, "codex_agent_enabled": True,
           "_codex_primary_tes": True, "agy_agent_enabled": False, "agy_primary_voice": False}
    reply, _, source = asyncio.run(b._cursor_reply_with_fallback("sys", "hi", "hi", cfg, "mic"))
    assert reply == "Recovered Codex reply" and source == "codex"
    log = capsys.readouterr().out
    assert "jatuh ke Groq" not in log and "turn ini lewat Groq" not in log
    assert "mencoba fallback Groq" in log


def test_codex_timeout_leaves_fallback_choice_to_caller(monkeypatch, capsys):
    import threading
    import arti_codex_agent as codex
    from tests.test_codex_agent import _pasang
    release, finished = threading.Event(), threading.Event()

    def run(*a, **kw):
        release.wait(1)
        finished.set()
        return NS(final_response="late", usage=None)

    _pasang(monkeypatch, NS(run=run))
    monkeypatch.setattr(codex, "_tutup", lambda *a, **kw: None)
    try:
        assert codex.send_turn("sys", "hi", {"codex_agent_enabled": True,
            "codex_timeout_sec": .01, "codex_cold_timeout_factor": 1}) is None
        log = capsys.readouterr().out
        assert "pemanggil memilih fallback" in log and "jatuh ke Groq" not in log
    finally:
        release.set()
        assert finished.wait(1)


