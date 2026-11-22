"""Shutdown may preserve local artifacts without external summary inference."""
import json

import session_transcript as st


def _transcript(tmp_path, monkeypatch):
    sid = "2099-01-01-offline"
    tx = tmp_path / f"{sid}.jsonl"
    tx.write_text(json.dumps({"ts": "12:00:00", "kind": "streamer", "text": "hello"}) + "\n")
    monkeypatch.setattr(st, "_ROOT", tmp_path)
    monkeypatch.setattr(st, "_session_id", sid)
    monkeypatch.setattr(st, "_transcript_path", tx)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    monkeypatch.setattr(st, "_manifest_path", manifest)
    return sid, tx, manifest


def _forbid(*args, **kwargs):
    raise AssertionError("external summary or context requested while disabled")


def test_disabled_summary_preserves_local_finalization_without_external_work(tmp_path, monkeypatch):
    sid, tx, manifest = _transcript(tmp_path, monkeypatch)
    for name in ("groq_summarize_session", "openrouter_summarize_session",
                 "gemini_summarize_session", "fetch_rag_lite_context"):
        monkeypatch.setattr(st, name, _forbid)
    st.finalize_session_artifacts({"vault_summary_external_enabled": False,
                                   "observer_enabled": False}, None)
    text = (tmp_path / "vault" / "sessions" / f"{sid}.md").read_text(encoding="utf-8")
    assert "Ringkasan eksternal dinonaktifkan" in text
    assert tx.is_file()
    assert json.loads(manifest.read_text())["ended_at"]
    assert (tmp_path / "vault" / "sessions" / "index.md").exists()


def test_disabled_summary_never_attempts_fallback_providers(tmp_path, monkeypatch):
    _, tx, _ = _transcript(tmp_path, monkeypatch)
    calls = []
    for name in ("groq_summarize_session", "openrouter_summarize_session", "gemini_summarize_session"):
        monkeypatch.setattr(st, name, lambda *a, **k: calls.append(True) or "")
    result = st.summarize_session_for_vault(tx, {"vault_summary_external_enabled": False})
    assert calls == []
    assert "Ringkasan eksternal dinonaktifkan" in result


def test_default_summary_retains_existing_provider_route(tmp_path, monkeypatch):
    _, tx, _ = _transcript(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(st, "groq_summarize_session", lambda *a, **k: calls.append("groq") or "summary")
    assert st.summarize_session_for_vault(tx, {}) == "summary"
    assert calls == ["groq"]


def test_disabled_external_summary_still_accepts_local_observer_beats(tmp_path, monkeypatch):
    sid, tx, _ = _transcript(tmp_path, monkeypatch)
    beats = tmp_path / "vault" / "sessions" / f"{sid}_beats.jsonl"
    beats.parent.mkdir(parents=True)
    beats.write_text("{}\n")
    monkeypatch.setattr(st, "summarize_session_from_beats", lambda *a, **k: "local approved beats")
    monkeypatch.setattr(st, "groq_summarize_session", _forbid)
    assert st.summarize_session_for_vault(tx, {"vault_summary_external_enabled": False}) == "local approved beats"
