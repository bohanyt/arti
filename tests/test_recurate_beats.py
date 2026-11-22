"""scripts/recurate_beats.py — synthetic vault in tmp_path only (never real data)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_curator as curator  # noqa: E402
import arti_observer_pipeline as pipeline  # noqa: E402
import arti_observer_rag as obs_rag  # noqa: E402
import arti_vault_rag as vault_rag  # noqa: E402

_spec = importlib.util.spec_from_file_location("recurate_beats", ROOT / "scripts" / "recurate_beats.py")
recurate_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recurate_mod)

SID = "2026-01-01-default"
GOOD = ("Streamer menguji sistem suara baru dengan pitch dinamis sambil memperbaiki "
        "bug lipsync, lalu Arti mengisi jeda dengan obrolan soal rencana konten.")
GOOD_2 = ("Segmen penutup berisi evaluasi alat pencarian web dan rencana kebijakan "
          "browsing selektif untuk Arti pada sesi berikutnya.")


def _row(idx, summary, status, **kw):
    d = dict(session_id=SID, segment_index=idx, t_start=f"00:{idx}0:00",
             t_end=f"00:{idx + 1}0:00", event_count=20, summary=summary, topics=[],
             facts=[], worth_embed=False, worth_learning=False, noise_level="low",
             canon_conflicts=[], provider="fake", curator_status=status)
    d.update(kw)
    return d


def _vault(tmp_path: Path) -> dict:
    sessions = tmp_path / "vault" / "sessions"
    concepts = tmp_path / "vault" / "concepts"
    sessions.mkdir(parents=True)
    concepts.mkdir(parents=True)
    rows = [
        _row(0, GOOD_2, "approved", worth_embed=True),
        # the false positive: good summary, worth_embed, noise high
        _row(1, GOOD, "rejected", worth_embed=True, noise_level="high", worth_learning=True,
             facts=[{"text": "Streamer punya rencana konten mingguan yang tetap", "confidence": 0.9}]),
        # junk that must stay out
        _row(2, "We need to output JSON only, with fields: summary, topics, facts",
             "rejected", worth_embed=True, noise_level="high"),
        _row(3, "hmm gitu sih ya kayak santai aja deh pusing sendiri maksudku gitu loh",
             "rejected", worth_embed=True, noise_level="high"),
        _row(4, GOOD, "rejected", worth_embed=False, noise_level="high"),
        _row(5, "(kosong)", "rejected", noise_level="high", event_count=0),
    ]
    (sessions / f"{SID}_beats.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (sessions / f"{SID}_beats.md").write_text(f"# Timeline beats — {SID}\n\nlama\n", encoding="utf-8")
    (sessions / f"{SID}.md").write_text("# Sesi\n\nmetrik sintetis\n", encoding="utf-8")
    (concepts / "arti_live_learnings.md").write_text(
        "# Learnings\n\n## Memori Jangka Panjang\n\n- [2026-01-01] Stream fact: contoh lama\n",
        encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "observer_rag.db").write_bytes(b"synthetic-db")
    return {"observer_db_path": "data/observer_rag.db", "vault_rag_db_path": "data/vault_rag.db"}


def _snapshot(tmp_path: Path) -> dict:
    return {p.relative_to(tmp_path).as_posix(): p.read_bytes()
            for p in tmp_path.rglob("*") if p.is_file()}


@pytest.fixture
def reindex_calls(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(obs_rag, "reindex_beats_session",
                        lambda sid, cfg=None: calls.append(sid) or {"chunks_new": 1})
    monkeypatch.setattr(vault_rag, "fakta_sudah_ada", lambda *a, **k: False)
    return calls


def _load(tmp_path: Path) -> dict[int, dict]:
    p = tmp_path / "vault" / "sessions" / f"{SID}_beats.jsonl"
    return {d["segment_index"]: d for d in map(json.loads, p.read_text(encoding="utf-8").splitlines())}


def test_dry_run_writes_nothing(tmp_path, reindex_calls):
    cfg = _vault(tmp_path)
    before = _snapshot(tmp_path)
    r = recurate_mod.recurate(tmp_path, cfg)
    assert r["apply"] is False
    assert r["beats_promoted"] == 1
    assert r["per_session"][SID]["promoted_segments"] == [1]
    assert r["per_session"][SID]["reasons"] == {"worth_embed_noisy": 1}
    assert r["still_rejected_reasons"] == {"prompt_fragment": 1, "filler": 1, "noise_high": 2}
    assert _snapshot(tmp_path) == before
    assert reindex_calls == []
    assert not (tmp_path / "data" / "recurate_backups").exists()


def test_apply_promotes_backs_up_and_reindexes(tmp_path, reindex_calls):
    cfg = _vault(tmp_path)
    before = _snapshot(tmp_path)
    r = recurate_mod.recurate(tmp_path, cfg, apply=True, backup_dir=tmp_path / "bak")
    assert r["beats_promoted"] == 1 and r["sessions_changed"] == 1

    beats = _load(tmp_path)
    assert beats[1]["curator_status"] == "approved"
    assert beats[1]["curator_reason"] == "worth_embed_noisy"
    for idx in (2, 3, 4, 5):
        assert beats[idx]["curator_status"] == "rejected"
    assert beats[0]["curator_status"] == "approved"

    md = (tmp_path / "vault" / "sessions" / f"{SID}_beats.md").read_text(encoding="utf-8")
    assert "seg 1, approved" in md and "seg 0, approved" in md
    assert "We need to output" not in md and "(kosong)" not in md
    session_md = (tmp_path / "vault" / "sessions" / f"{SID}.md").read_text(encoding="utf-8")
    assert session_md.count("## Timeline beats") == 1
    assert GOOD in session_md

    # noisy beats never feed the learnings FIFO
    assert r["learnings_written"] == 0
    learn = "vault/concepts/arti_live_learnings.md"
    assert (tmp_path / learn).read_bytes() == before[learn]

    assert reindex_calls == [SID]
    backed = {Path(p).relative_to(tmp_path / "bak").as_posix() for p in r["backups"]}
    assert backed == {f"vault/sessions/{SID}_beats.jsonl", f"vault/sessions/{SID}_beats.md",
                      f"vault/sessions/{SID}.md", "data/observer_rag.db"}
    for rel in backed:
        assert (tmp_path / "bak" / rel).read_bytes() == before[rel]


def test_apply_twice_is_idempotent(tmp_path, reindex_calls):
    cfg = _vault(tmp_path)
    recurate_mod.recurate(tmp_path, cfg, apply=True, backup_dir=tmp_path / "bak1")
    after_first = _snapshot(tmp_path)
    r2 = recurate_mod.recurate(tmp_path, cfg, apply=True, backup_dir=tmp_path / "bak2")
    assert r2["beats_promoted"] == 0 and r2["sessions_changed"] == 0
    assert r2["backups"] == []
    assert not (tmp_path / "bak2").exists()
    assert _snapshot(tmp_path) == after_first
    assert reindex_calls == [SID]
    assert sum(1 for b in _load(tmp_path).values() if b["curator_status"] == "approved") == 2


def test_partial_session_skipped(tmp_path, reindex_calls):
    cfg = _vault(tmp_path)
    (tmp_path / "vault" / "sessions" / f"{SID}_beats.partial").write_text("", encoding="utf-8")
    r = recurate_mod.recurate(tmp_path, cfg, apply=True, backup_dir=tmp_path / "bak")
    assert r["sessions_skipped_partial"] == 1 and r["beats_promoted"] == 0
    assert reindex_calls == []


def test_learnings_flag_and_reindex_flag(tmp_path, reindex_calls, monkeypatch):
    """A noise-low beat that newly passes goes through promote_learnings; flags turn I/O off."""
    cfg = _vault(tmp_path)
    p = tmp_path / "vault" / "sessions" / f"{SID}_beats.jsonl"
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    # Simulate a beat rejected by an older, stricter rule that today's curator approves.
    rows.append(_row(6, GOOD_2, "rejected", worth_learning=True,
                     facts=[{"text": "Streamer memilih alat pencarian web untuk Arti", "confidence": 0.9}]))
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    dry = recurate_mod.recurate(tmp_path, cfg)
    assert dry["learning_candidates"] == 1
    r = recurate_mod.recurate(tmp_path, cfg, apply=True, learnings=False, reindex=False,
                              backup_dir=tmp_path / "bak")
    assert r["beats_promoted"] == 2 and r["learnings_written"] == 0
    assert reindex_calls == []
    assert "data/observer_rag.db" not in {
        Path(x).relative_to(tmp_path / "bak").as_posix() for x in r["backups"]}


def test_module_roots_restored(tmp_path, reindex_calls):
    cfg = _vault(tmp_path)
    before = (curator._ROOT, pipeline._ROOT, obs_rag._ROOT, vault_rag._ROOT)
    recurate_mod.recurate(tmp_path, cfg, apply=True, backup_dir=tmp_path / "bak")
    assert (curator._ROOT, pipeline._ROOT, obs_rag._ROOT, vault_rag._ROOT) == before
