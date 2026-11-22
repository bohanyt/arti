"""Tests for pipeline_timer — Fase 1 latency instrumentation."""

import time

import pipeline_timer
from pipeline_timer import PipelineTimer, format_latency_line


def test_stages_ms_computes_deltas():
    timer = PipelineTimer(extra={"asr_ms": 120, "vad_tail_ms": 2000})
    timer.mark("after_mikir")
    time.sleep(0.01)
    timer.mark("after_rag")
    time.sleep(0.01)
    timer.mark("after_llm")
    pipeline_timer.note_tts_synth_ms(300)
    pipeline_timer.note_tts_play_ms(150)
    timer.mark("after_tts")

    stages = timer.stages_ms()

    assert stages["asr_ms"] == 120
    assert stages["vad_tail_ms"] == 2000
    assert stages["vts_mikir_ms"] >= 0
    assert stages["rag_ms"] >= 5
    assert stages["llm_ms"] >= 5
    assert stages["tts_synth_ms"] == 300
    assert stages["tts_play_ms"] == 150
    assert stages["tts_ms"] == 450
    assert stages["total_ms"] >= 20


def test_format_latency_line_omits_missing_keys():
    line = format_latency_line({"asr_ms": 500, "llm_ms": 800, "total_ms": 3500})
    assert line.startswith("[Latency]")
    assert "asr=500ms" in line
    assert "llm=800ms" in line
    assert "total=3500ms" in line
    assert "rag=" not in line


def test_tts_multi_kalimat_dijumlah_bukan_ditimpa():
    """Regresi 12 Agu 2026: `speak()` dipanggil per kalimat, jadi versi lama
    yang menimpa global hanya melaporkan kalimat TERAKHIR — 7,7 detik/giliran
    hilang dari log."""
    pipeline_timer.consume_tts_stages()  # bersihkan sisa test lain
    for synth, play in ((300, 1200), (250, 900), (400, 1500)):
        pipeline_timer.note_tts_synth_ms(synth)
        pipeline_timer.note_tts_play_ms(play)

    stages = pipeline_timer.consume_tts_stages()

    assert stages["tts_synth_ms"] == 950
    assert stages["tts_play_ms"] == 3600
    assert stages["tts_chunks"] == 3


def test_consume_tts_stages_mereset_hitungan_chunk():
    pipeline_timer.consume_tts_stages()
    pipeline_timer.note_tts_synth_ms(100)
    pipeline_timer.note_tts_synth_ms(100)
    assert pipeline_timer.consume_tts_stages()["tts_chunks"] == 2
    assert pipeline_timer.consume_tts_stages() == {}


def test_sisa_waktu_tak_terjelaskan_diberi_nama():
    """`lain_ms` = bagian `total` yang bukan mikir/rag/llm/tts. Dulu raib."""
    pipeline_timer.consume_tts_stages()
    timer = PipelineTimer()
    timer.mark("after_mikir")
    timer.mark("after_rag")
    timer.mark("after_llm")
    pipeline_timer.note_tts_synth_ms(1)
    pipeline_timer.note_tts_play_ms(1)
    time.sleep(0.05)  # koreografi/nod: waktu nyata yang bukan sintesis & bukan play
    timer.mark("after_tts")

    stages = timer.stages_ms()

    assert stages["lain_ms"] >= 40
    assert stages["lain_ms"] <= stages["total_ms"]


def test_sisa_negatif_tetap_dicetak_bukan_disembunyikan():
    """Tahapan mengaku lebih lama dari total = mustahil = ada yang bocor.
    Alat ukur tidak boleh bungkam justru saat angkanya aneh."""
    pipeline_timer.consume_tts_stages()
    timer = PipelineTimer()
    timer.mark("after_llm")
    pipeline_timer.note_tts_synth_ms(9_000)   # jauh lebih besar dari waktu nyata
    pipeline_timer.note_tts_play_ms(9_000)
    timer.mark("after_tts")

    stages = timer.stages_ms()

    assert stages["lain_ms"] < 0
    assert "lain=" in format_latency_line(stages)


def test_dirasa_menghitung_vad_tail_yang_di_luar_total():
    pipeline_timer.consume_tts_stages()
    timer = PipelineTimer(extra={"asr_ms": 600, "vad_tail_ms": 10099})
    timer.mark("after_tts")

    stages = timer.stages_ms()

    assert stages["dirasa_ms"] == 10699 + stages["total_ms"]
    assert stages["dirasa_ms"] > stages["total_ms"]


def test_sisa_giliran_mati_tidak_bocor_ke_giliran_berikutnya():
    """Giliran yang gugur sebelum stages_ms() (exception, cancel) meninggalkan
    sisa di global TTS. Giliran baru harus mulai dari nol, bukan mewarisi."""
    pipeline_timer.note_tts_synth_ms(9999)
    pipeline_timer.note_tts_play_ms(9999)

    timer = PipelineTimer()  # giliran baru
    pipeline_timer.note_tts_synth_ms(200)
    pipeline_timer.note_tts_play_ms(500)
    timer.mark("after_tts")

    stages = timer.stages_ms()

    assert stages["tts_synth_ms"] == 200
    assert stages["tts_play_ms"] == 500
    assert "tts_chunks" not in stages  # satu kalimat saja


def test_format_hitungan_chunk_tanpa_akhiran_ms():
    line = format_latency_line(
        {"tts_ms": 4500, "tts_chunks": 3, "lain_ms": 7700, "total_ms": 21734}
    )
    assert "tts_chunks=3" in line
    assert "tts_chunks=3ms" not in line
    assert "lain=7700ms" in line


def test_pop_asr_stages_clears_pending():
    pipeline_timer.set_pending_asr_stages({"asr_ms": 99, "vad_tail_ms": 2000})
    assert pipeline_timer.pop_asr_stages() == {"asr_ms": 99, "vad_tail_ms": 2000}
    assert pipeline_timer.pop_asr_stages() == {}
