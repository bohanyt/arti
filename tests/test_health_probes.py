"""Tests for bridge_health_probes."""

from __future__ import annotations

import bridge_health_probes as bhp


def test_tiny_jpeg_b64():
    b64 = bhp.tiny_jpeg_b64()
    assert len(b64) > 50


def test_vision_models_for_provider():
    cfg = {
        "vision_google_gemma_model": "gemma-a",
        "vision_google_gemma_fallback_model": "gemma-b",
    }
    models = bhp.vision_models_for_provider(cfg, "google_gemma")
    assert models == ["gemma-a", "gemma-b"]


def test_text_models_openrouter_cap():
    cfg = {"scouter_openrouter_models": ["m1", "m2", "m3"]}
    models = bhp.text_models_for_provider(cfg, "openrouter")
    assert models == ["m1", "m2"]


def test_probe_vision_model_skip_unknown():
    row = bhp.probe_vision_model("unknown_provider", "x", {})
    assert row.status == "SKIP"
