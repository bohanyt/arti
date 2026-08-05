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


def test_pop_asr_stages_clears_pending():
    pipeline_timer.set_pending_asr_stages({"asr_ms": 99, "vad_tail_ms": 2000})
    assert pipeline_timer.pop_asr_stages() == {"asr_ms": 99, "vad_tail_ms": 2000}
    assert pipeline_timer.pop_asr_stages() == {}
