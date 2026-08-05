"""Tests for observer segment pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import arti_observer_pipeline as pipe


def test_ts_to_seconds():
    assert pipe.ts_to_seconds("00:05:30") == 330
    assert pipe.ts_to_seconds("bad") is None


def test_segment_by_minutes():
    rows = [
        {"ts": "00:01:00", "kind": "streamer", "text": "halo"},
        {"ts": "00:11:00", "kind": "arti", "text": "hai"},
    ]
    segs = pipe.segment_by_minutes(rows, minutes=10)
    assert len(segs) == 2
    assert segs[0].index == 0
    assert segs[1].index == 1


def test_write_beats_jsonl_md(tmp_path, monkeypatch):
    beats = [
        pipe.BeatDraft(
            session_id="2026-06-05-default",
            segment_index=0,
            t_start="00:00:00",
            t_end="00:10:00",
            event_count=2,
            summary="tes segmen",
            curator_status="approved",
        )
    ]
    jl = tmp_path / "beats.jsonl"
    md = tmp_path / "beats.md"
    pipe.write_beats_jsonl(beats, jl)
    pipe.write_beats_md(beats, md, "2026-06-05-default")
    assert jl.is_file()
    row = json.loads(jl.read_text(encoding="utf-8").strip())
    assert row["summary"] == "tes segmen"
    assert "tes segmen" in md.read_text(encoding="utf-8")


def test_summarize_session_from_beats(tmp_path, monkeypatch):
    import session_transcript as st

    jl = tmp_path / "beats.jsonl"
    jl.write_text(
        json.dumps(
            {
                "curator_status": "approved",
                "t_start": "00:00:00",
                "summary": "Beat satu",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(st, "_ROOT", tmp_path.parent)
    # beats path uses _ROOT / vault / sessions - place file there
    sess_dir = tmp_path / "vault" / "sessions"
    sess_dir.mkdir(parents=True)
    beats_in_vault = sess_dir / "2026-06-05-default_beats.jsonl"
    beats_in_vault.write_text(jl.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(st, "_session_id", "2026-06-05-default")
    monkeypatch.setattr(st, "_ROOT", tmp_path)
    out = st.summarize_session_from_beats(beats_in_vault, {})
    assert "Beat satu" in out

    tx = tmp_path / "t.jsonl"
    tx.write_text(
        json.dumps({"ts": "00:01:00", "kind": "streamer", "text": "hello"}) + "\n",
        encoding="utf-8",
    )

    def fake_summarize(block, config):
        return {"summary": "mock", "topics": ["test"], "facts": [], "worth_embed": True, "noise_level": "low", "provider": "mock"}

    monkeypatch.setattr("arti_observer_client.summarize_segment", fake_summarize)
    beats = pipe.run_observe("sess", tx, {"observer_segment_minutes": 10})
    assert len(beats) == 1
    assert beats[0].summary == "mock"


# --- parser observer ikut parser keras scouter (2026-08-04) ----------------------


def test_observer_parse_json_survives_composer_grok_styles():
    """Regex lama "{ pertama..} terakhir" pecah oleh dua objek / prosa
    ber-kurung -> fallback memasukkan TEKS MENTAH sebagai summary yang lolos
    kurasi ke vault. Kini pakai _parse_json_blob keras milik scouter."""
    import arti_observer_client as oc

    inner = '{"summary":"segmen ok","noise_level":"low"}'
    for name, raw in {
        "polos": inner,
        "fence": f"```json\n{inner}\n```",
        "prosa ber-kurung": "Catatan {penting}: hasil di bawah.\n" + inner,
        "dua objek": f"{inner}\n{{\"summary\":\"kedua\"}}",
    }.items():
        out = oc._parse_json(raw)
        assert out.get("summary") == "segmen ok", f"gagal: {name} -> {out}"

    # teks yang beneran bukan JSON tetap diselamatkan sebagai ringkasan mentah
    out = oc._parse_json("cuma prosa tanpa json sama sekali")
    assert out["summary"].startswith("cuma prosa")


def test_observer_cursor_call_records_exactly_one_telemetry_row(monkeypatch):
    """Audit ronde-3: satu panggilan observer via cursor tercatat DUA baris
    (di _call_cursor + di summarize_segment) -> jumlah call & latensi
    observer inflasi 2x. Kini provider yang mencatat, SEKALI, label benar."""
    import arti_api_telemetry as tel
    import arti_cursor_agent as ca
    import arti_observer_client as oc

    rec = []
    monkeypatch.setattr(tel, "record_call", lambda **kw: rec.append(kw))

    class _R:
        ok, latency_ms, model, reason = True, 42, "", ""
        text = '{"summary":"segmen","noise_level":"low"}'

    monkeypatch.setattr(ca, "send_task", lambda *a, **k: _R())
    out = oc.summarize_segment("transcript", {"observer_provider_chain": ["cursor"]})
    assert out["summary"] == "segmen"
    assert len(rec) == 1, f"harus SATU baris, dapat {len(rec)}: {rec}"
    assert rec[0]["subsystem"] == "observer"
    assert rec[0]["model"] == "grok-4.5/high"


def test_observer_fallback_nvidia_labeled_observer(monkeypatch):
    """Jalur fallback (cursor tumbang) dulu tetap tercatat 'scouter' —
    mencemari analisis biaya scouter persis di momen kuota habis."""
    import arti_observer_client as oc
    import arti_scouter_client as scl

    seen = {}

    def fake_nvidia(prompt, cfg):
        seen["subsystem"] = cfg.get("telemetry_subsystem")
        return '{"summary":"via nvidia","noise_level":"low"}', 9

    monkeypatch.setitem(scl._PROVIDERS, "nvidia", fake_nvidia)
    out = oc.summarize_segment("transcript", {"observer_provider_chain": ["nvidia"]})
    assert out["summary"] == "via nvidia"
    assert seen["subsystem"] == "observer", (
        "provider harus menerima telemetry_subsystem=observer via cfg"
    )
