"""Silence is inconclusive; an actual device-open failure is still FAIL."""
from types import SimpleNamespace

import pytest

import bridge_health as health


@pytest.mark.parametrize("failure, expected", [(False, "WARN"), (True, "FAIL")])
def test_startup_mic_level(monkeypatch, failure, expected, capsys):
    monkeypatch.setattr(health, "list_input_devices", lambda cfg: [(1, "synthetic mic", 1)])
    monkeypatch.setattr(health.sd, "default", SimpleNamespace(device=(None, None)))

    def sample(*a, **kw):
        if failure:
            raise OSError("synthetic device-open failure")
        return 0.0, 0.0

    monkeypatch.setattr(health, "sample_mic_levels", sample)
    report = health.run_startup_health_check(
        {"api_provider": "fake"}, resolve_mic_fn=lambda cfg: (1, "synthetic mic"),
        vts=SimpleNamespace(websocket=True), hotkey_registered=True,
    )
    row = next(row for row in report.rows if row.name == "Mic level")
    assert row.status == expected
    if not failure:
        assert "diam" in row.detail and "belum" in row.detail
        assert "boleh diam" not in capsys.readouterr().out


@pytest.mark.parametrize("failure, expected", [(False, "[WARN]"), (True, "[FAIL]")])
def test_wizard_mic_preview(monkeypatch, failure, expected, capsys):
    monkeypatch.setattr(health, "list_input_devices", lambda cfg: [(1, "synthetic mic", 1)])
    monkeypatch.setattr(health.sd, "default", SimpleNamespace(device=(None, None)))

    def sample(*a, **kw):
        if failure:
            raise OSError("synthetic device-open failure")
        return 0.0, 0.0

    monkeypatch.setattr(health, "live_mic_level_preview", sample)
    health.prompt_mic_selection({}, resolve_mic_fn=lambda cfg: (1, "synthetic mic"), ask_input=lambda p: "")
    assert expected in capsys.readouterr().out
