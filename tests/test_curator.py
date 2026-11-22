"""Tests for arti_curator."""

from __future__ import annotations

import arti_curator as curator
import arti_observer_pipeline as pipe


def test_curate_rejects_noise():
    beat = pipe.BeatDraft(
        session_id="s",
        segment_index=0,
        t_start="00:00:00",
        t_end="00:10:00",
        event_count=1,
        summary="",
        noise_level="high",
    )
    result = curator.curate_beats([beat], {})
    assert result.rejected_count == 1
    assert beat.curator_status == "rejected"


def test_curate_rejects_misleading_fact():
    beat = pipe.BeatDraft(
        session_id="s",
        segment_index=0,
        t_start="00:00:00",
        t_end="00:10:00",
        event_count=1,
        summary="ok",
        facts=[{"text": "Arti baru mulai live stream hari ini", "confidence": 0.9}],
        worth_embed=True,
    )
    result = curator.curate_beats([beat], {"arti_debut_label": "1 Januari 2026"})
    assert beat.curator_status == "rejected"
    assert result.rejected_count == 1


# --- brain batch B (2026-10-21): noise rule false positive -------------------
# Synthetic beats only (no real summaries / viewer names).

_GOOD_NOISY = (
    "Streamer menguji sistem suara baru dengan pitch dinamis sambil memperbaiki "
    "bug lipsync, lalu Arti mengisi jeda dengan obrolan soal rencana konten minggu depan."
)


def _beat(summary, **kw):
    base = dict(session_id="s", segment_index=0, t_start="00:00:00", t_end="00:10:00",
                event_count=10, summary=summary)
    base.update(kw)
    return pipe.BeatDraft(**base)


def test_noise_high_worth_embed_good_summary_now_approved():
    beat = _beat(_GOOD_NOISY, noise_level="high", worth_embed=True)
    result = curator.curate_beats([beat], {})
    assert beat.curator_status == "approved"
    assert beat.curator_reason == "worth_embed_noisy"
    assert result.approved_count == 1


def test_noise_high_without_worth_embed_still_rejected():
    beat = _beat(_GOOD_NOISY, noise_level="high", worth_embed=False, worth_learning=True)
    curator.curate_beats([beat], {})
    assert (beat.curator_status, beat.curator_reason) == ("rejected", "noise_high")


import pytest  # noqa: E402


@pytest.mark.parametrize("summary,reason", [
    ("We need to output JSON only, with fields: summary (2-3 kalimat ID ringkas segmen), topics",
     "prompt_fragment"),
    ('{\n  "summary": "x", "noise_level": "high"}', "raw_model_output"),
    ("```json {} ```", "raw_model_output"),
    ("<think>hmm</think> Streamer ngobrol panjang soal banyak hal di stream malam ini", "raw_model_output"),
    ("(kosong)", "placeholder"),
    ("(observer gagal: timeout)", "placeholder"),
    ("Kamu Observer — ringkas segmen live stream Arti untuk vault ya", "prompt_fragment"),
    ("gitu, maksudku, pusing, santai, sendiri", "too_short"),
    ("hmm gitu sih ya kayak santai aja deh pusing sendiri maksudku gitu loh haha", "filler"),
    ("Arti ngobrol.", "too_short"),
])
def test_noisy_junk_stays_rejected(summary, reason):
    beat = _beat(summary, noise_level="high", worth_embed=True, worth_learning=True,
                 facts=[{"text": "fakta sintetis yang cukup panjang", "confidence": 0.9}])
    curator.curate_beats([beat], {})
    assert beat.curator_status == "rejected"
    assert beat.curator_reason == reason


def test_hard_junk_rejected_even_when_noise_low():
    beat = _beat("We need to output JSON with fields: summary, topics, facts",
                 noise_level="low", worth_embed=True)
    curator.curate_beats([beat], {})
    assert (beat.curator_status, beat.curator_reason) == ("rejected", "prompt_fragment")


def test_noise_low_short_summary_keeps_old_behaviour():
    """Soft checks only apply to the newly admitted noisy class."""
    beat = _beat("ringkasan 1", noise_level="low", worth_embed=True)
    curator.curate_beats([beat], {})
    assert (beat.curator_status, beat.curator_reason) == ("approved", "worth_embed")


def test_noisy_canon_conflict_still_rejected():
    beat = _beat(_GOOD_NOISY, noise_level="high", worth_embed=True,
                 facts=[{"text": "Arti baru mulai live stream hari ini", "confidence": 0.9}])
    curator.curate_beats([beat], {"arti_debut_label": "1 Januari 2026"})
    assert (beat.curator_status, beat.curator_reason) == ("rejected", "canon_conflict")


def test_noisy_approved_beat_never_writes_learnings(monkeypatch, tmp_path):
    monkeypatch.setattr(curator, "_ROOT", tmp_path)
    calls = []
    import arti_memory_quality as mq
    monkeypatch.setattr(mq, "append_learning", lambda *a, **k: calls.append(a) or True)
    beat = _beat(_GOOD_NOISY, noise_level="high", worth_embed=True, worth_learning=True,
                 facts=[{"text": "Streamer punya rencana konten mingguan yang tetap", "confidence": 0.9}])
    result = curator.curate_beats([beat], {})
    assert beat.curator_status == "approved"
    assert result.learnings_written == 0 and calls == []


def test_noise_low_learning_path_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(curator, "_ROOT", tmp_path)
    calls = []
    import arti_memory_quality as mq
    monkeypatch.setattr(mq, "append_learning", lambda *a, **k: calls.append(a) or True)
    beat = _beat(_GOOD_NOISY, noise_level="low", worth_learning=True,
                 facts=[{"text": "Streamer punya rencana konten mingguan yang tetap", "confidence": 0.9}])
    result = curator.curate_beats([beat], {})
    assert beat.curator_status == "approved"
    assert result.learnings_written == 1 and len(calls) == 1


def test_decide_is_pure(monkeypatch):
    import arti_memory_quality as mq
    monkeypatch.setattr(mq, "append_learning", lambda *a, **k: pytest.fail("decide wrote"))
    beat = _beat(_GOOD_NOISY, noise_level="low", worth_learning=True,
                 facts=[{"text": "Streamer punya rencana konten mingguan yang tetap", "confidence": 0.9}])
    assert curator.decide(beat, {}) == ("approved", "worth_learning")
    assert beat.curator_status == "pending"
