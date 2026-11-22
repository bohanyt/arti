"""Mabar runs as her live self: allowlisted live settings, read-only vault recall, visible memory failure."""
import hashlib
import json
from pathlib import Path

import pytest

from scripts.livepath import mabar, rehearse


def tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


LIVE = dict(streamer_name="Owner", owner_yt_handles=["@owner"], arti_debut_date="2026-01-01", vts_model_dir="X:/m",
            idle_motion_hotkeys=["a"], codex_model="m", hotkey_key="k", lmstudio_embedding_model="e",
            vision_enabled=True, youtube_video_id="secretish", observer_db_path="live.db", web_lookup_enabled=True)


def test_overlay_takes_only_allowlisted_live_keys(tmp_path):
    path = tmp_path / "config_local.json"
    path.write_text(json.dumps(LIVE), encoding="utf-8")
    got = mabar.live_overlay(path)
    assert got["streamer_name"] == "Owner" and got["vts_model_dir"] == "X:/m" and got["hotkey_key"] == "k"
    for excluded in ("vision_enabled", "youtube_video_id", "observer_db_path", "web_lookup_enabled"):
        assert excluded not in got
    with pytest.raises(RuntimeError, match="live private config not found"):
        mabar.live_overlay(tmp_path / "missing.json")


def test_mabar_config_is_her_live_self_but_keeps_mabar_rules(tmp_path):
    live = mabar.live_overlay(_write(tmp_path))
    cfg = mabar.mabar_bridge_config(tmp_path / "r", vts=True, mic=1, ssn="app", live=live)
    assert cfg["streamer_name"] == "Owner" and cfg["idle_motion_hotkeys"] == ["a"] and cfg["codex_model"] == "m"
    assert cfg["vault_rag_enabled"] and cfg["vault_rag_live_enabled"] and not cfg["vault_rag_reindex_on_startup"]
    assert not cfg["vault_rag_reindex_on_shutdown"]
    assert cfg["vault_rag_root"] == str(mabar.STREAM) and cfg["vault_rag_db_path"] == str(mabar.VAULT_INDEX)
    assert cfg["expression_nod_enabled"] and cfg["reflex_enabled"] and cfg["provider_probe_enabled"]
    assert cfg["codex_web_search"] == "disabled" and cfg["web_lookup_enabled"] is False  # owner: no internet
    assert cfg["vision_enabled"] is False and cfg["observer_enabled"] is False  # live-writing peripherals stay off
    assert cfg["vts_api_port"] == 8002  # her VTS window, not the live value
    plain = rehearse.bridge_config(tmp_path / "p", tmp_path / "p" / "runtime")
    assert plain["vault_rag_enabled"] is False and "vault_rag_root" not in plain  # plain rehearsal unchanged
    from scripts.livepath import bridge as lp_bridge
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    lp_bridge.load_config(p)


def _write(tmp_path):
    path = tmp_path / "config_local.json"
    path.write_text(json.dumps(LIVE), encoding="utf-8")
    return path


def test_vault_index_reads_the_live_notes_and_never_writes_there(tmp_path, monkeypatch):
    import arti_vault_rag
    live = tmp_path / "stream-folder"
    (live / "vault/sessions").mkdir(parents=True)
    (live / "vault/sessions/2026-11-09-mabar.md").write_text(
        "# Mabar\n\n" + "Aku main Stardew bareng Bohan di farm baru, kita ketemu Lewis dan Robin hari pertama. " * 6,
        encoding="utf-8")
    (live / "ARTI_VIEWERS.md").write_text("# Viewers\n\n" + "Penonton setia suka nanya soal kopi dan farm. " * 5,
                                          encoding="utf-8")
    (live / "data").mkdir()
    (live / "data/vault_rag.db").write_bytes(b"live db must stay untouched")
    before = tree(live)
    monkeypatch.setattr(arti_vault_rag, "embed_texts", lambda texts, cfg, **kw: [[0.1] * 8 for _ in texts])
    db = tmp_path / "arti-workspace/vault-index/vault_rag.db"
    cfg = dict(vault_rag_enabled=True, vault_rag_root=str(live), vault_rag_db_path=str(db))
    stats = arti_vault_rag.reindex_all(cfg, force=False, verbose=False)
    assert stats["files"] == 2 and stats["chunks_new"] > 0 and db.is_file()
    assert tree(live) == before  # nothing written, moved or reindexed inside the live folder
    again = arti_vault_rag.reindex_all(cfg, force=False, verbose=False)
    assert again["chunks_new"] == 0  # an up-to-date index is reused
    with pytest.raises(PermissionError):
        arti_vault_rag.init_db(dict(cfg, vault_rag_db_path=str(live / "data/vault_rag.db")))
    assert tree(live) == before


def test_soft_failure_is_visible_and_she_keeps_playing(tmp_path):
    run = tmp_path / "run"
    run.mkdir()

    def down(cfg, **kw):
        raise ConnectionError("Embedding circuit breaker open")

    result = mabar.build_vault_index(run, {}, reindex=down)
    assert result["ok"] is False and result["why"] == "LM Studio tidak menjawab"
    assert mabar.vault_status(run) == "LM Studio tidak menjawab"
    cfg = mabar.mabar_bridge_config(run, vts=True, mic=1, live={}, vault_why=mabar.vault_status(run))
    assert any("Ingatan vault-mu" in f and "LM Studio tidak menjawab" in f for f in cfg["live7_session_facts"])
    assert mabar.vault_status(tmp_path / "never-prepared") == "index belum dibuat"


def test_status_line_says_memory_is_down_when_the_breaker_opens(monkeypatch):
    import arti_vault_rag
    from arti_live7_status_facts import StatusFactsRelay
    relay = StatusFactsRelay(dict(livepath_mode="mabar", vault_rag_enabled=True), None, clock=lambda: 0.0)
    monkeypatch.setattr(arti_vault_rag, "embedding_breaker_open", lambda: True)
    relay._publish()
    assert "Ingatan vault-mu lagi mati sementara" in relay.status_block()
    monkeypatch.setattr(arti_vault_rag, "embedding_breaker_open", lambda: False)
    relay._publish()
    assert "Ingatan vault" not in relay.status_block()
    live = StatusFactsRelay(dict(vault_rag_enabled=True), None, clock=lambda: 0.0)  # live: no new line
    monkeypatch.setattr(arti_vault_rag, "embedding_breaker_open", lambda: True)
    live._publish()
    assert "Ingatan vault" not in live.status_block()


def test_viewer_memory_and_mood_are_copies(tmp_path):
    src, dest, run = tmp_path / "live", tmp_path / "checkout", tmp_path / "run"
    for p in (src, dest, run):
        p.mkdir()
    (src / "ARTI_VIEWERS.md").write_text("viewers", encoding="utf-8")
    (src / "ARTI_MOOD_STATE.json").write_text("{}", encoding="utf-8")
    assert mabar.copy_live_self_files(run, dest=dest, src=src) == ["ARTI_VIEWERS.md", "ARTI_MOOD_STATE.json"]
    (dest / "ARTI_VIEWERS.md").write_text("her edit", encoding="utf-8")
    assert (src / "ARTI_VIEWERS.md").read_text(encoding="utf-8") == "viewers"
