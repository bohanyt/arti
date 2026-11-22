"""Tests for arti_telemetry_dashboard."""

from __future__ import annotations

import arti_api_telemetry as tel
import arti_telemetry_dashboard as dash


def test_generate_dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(tel, "_ROOT", tmp_path)
    monkeypatch.setattr(dash, "_ROOT", tmp_path)
    cfg = {
        "telemetry_dir": str(tmp_path / "telemetry"),
        "telemetry_cost_table_path": str(tmp_path / "cost.json"),
        "telemetry_benchmarks_path": str(tmp_path / "bench.json"),
    }
    (tmp_path / "bench.json").write_text(
        '{"models":{"m":{"display":"Test M","weight_tier":"light","ref_blended_per_1m":0.1}}}',
        encoding="utf-8",
    )
    tel.set_session_id("dash-sess")
    tel.record_call(
        subsystem="scouter",
        provider="openrouter",
        model="m",
        latency_ms=50,
        usage=tel.UsageInfo(total_tokens=100),
        config=cfg,
    )
    tel.flush(cfg)
    data = dash.load_dashboard_data(cfg)
    assert data["event_count"] == 1
    assert data["models"][0]["subsystem"] == "scouter"
    out = dash.generate_dashboard(cfg)
    html = out.read_text(encoding="utf-8")
    assert "Arti API Telemetry" in html
    assert "Scribble Edition" in html
    assert "scouter" in html


def test_render_html_auto_refresh():
    data = dash.load_dashboard_data({"telemetry_dir": "data/telemetry"})
    html = dash.render_html(data, refresh_seconds=15)
    assert 'http-equiv="refresh"' in html
    assert 'content="15"' in html
