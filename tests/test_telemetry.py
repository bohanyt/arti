"""Tests for arti_api_telemetry."""

from __future__ import annotations

import json

import arti_api_telemetry as tel


def test_parse_openai_usage():
    body = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.001}}
    u = tel.parse_openai_usage(body)
    assert u.total_tokens == 15
    assert u.cost_usd == 0.001
    assert u.cost_source == "reported"


def test_session_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(tel, "_ROOT", tmp_path)
    cfg = {"telemetry_dir": str(tmp_path / "telemetry")}
    tel.set_session_id("test-sess")
    tel.record_call(
        subsystem="vision",
        provider="nvidia",
        model="m",
        latency_ms=100,
        usage=tel.UsageInfo(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        config=cfg,
    )
    tel.flush(cfg)
    summary = tel.session_summary("test-sess", cfg)
    assert summary["event_count"] >= 1
    assert summary["total_tokens"] >= 8
    md = tel.format_api_usage_markdown(summary)
    assert "## API Usage" in md


def test_reference_cost_free_model():
    u = tel.UsageInfo(prompt_tokens=1000, completion_tokens=200, total_tokens=1200)
    entry = {"free": True, "ref_input_per_1m": 0.30, "ref_output_per_1m": 0.90}
    ref, per_1m = tel.reference_cost_usd(u, entry)
    assert ref > 0
    assert per_1m > 0
    u = tel.UsageInfo(total_tokens=100)
    out = tel.estimate_cost("openrouter", "poolside/laguna-xs.2:free", u, {"telemetry_cost_table_path": "data/api_cost_table.json"})
    assert out.cost_source in ("free", "unknown", "estimated")
