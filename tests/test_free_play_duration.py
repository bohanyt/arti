"""Duration admission is shared by the harness CLIs; no game or task starts."""
import argparse
import json
import sys
from types import SimpleNamespace

import pytest

from scripts.free_play import soak, storage


VALID = [("5h", 5), ("3h", 3), ("1.5h", 1.5), ("60m", 1),
         ("90m", 1.5), ("1h30m", 1.5), ("0h45m", .75), ("1h0m", 1)]
INVALID = ["", "90", "1.5", "0h", "0m", "0h0m", "-1h", "+1h", "1H",
           "1.5m", "1.5h30m", "1h30", "30m1h", "1h 30m", " 1h", "1h\n",
           ".5h", "1.h", "1e2h", "nanh", "infh", "1hh", "1d", "\u0661h",
           "9" * 400 + "h"]


@pytest.mark.parametrize("duration,hours", VALID)
def test_shared_parser(duration, hours):
    from scripts.free_play.duration import parse_duration
    assert parse_duration(duration) == hours


@pytest.mark.parametrize("duration", INVALID)
def test_shared_parser_refuses(duration):
    from scripts.free_play.duration import parse_duration
    with pytest.raises(argparse.ArgumentTypeError, match="invalid duration.*Nh, N.Nh, Nm, or XhYm"):
        parse_duration(duration)


def soak_args(duration, output):
    args = ["soak.py", "soak", "--output", str(output)]
    for name in ("dll", "helper", "template", "host-template", "trial-config", "provenance",
                 "code-sha", "dll-sha256", "helper-sha256"):
        args += ["--" + name, "unused"]
    return args + ["--", duration, "Standard"]


@pytest.mark.parametrize("duration,hours", VALID)
def test_soak_passes_parsed_hours_to_runner(monkeypatch, tmp_path, duration, hours):
    called = []
    monkeypatch.setattr(soak, "run_soak", called.append)
    monkeypatch.setattr(sys, "argv", soak_args(duration, tmp_path / "no-run"))
    assert soak.main() == 0
    assert len(called) == 1 and called[0].hours == hours
    assert not (tmp_path / "no-run").exists()


@pytest.mark.parametrize("duration", INVALID)
def test_soak_refuses_before_runner_or_receipt(monkeypatch, tmp_path, capsys, duration):
    monkeypatch.setattr(soak, "run_soak", lambda _: pytest.fail("runner started"))
    monkeypatch.setattr(sys, "argv", soak_args(duration, tmp_path / "no-run"))
    with pytest.raises(SystemExit) as exc:
        soak.main()
    assert exc.value.code == 2
    assert "invalid duration" in capsys.readouterr().err
    assert not (tmp_path / "no-run").exists()


@pytest.mark.parametrize("duration,hours", VALID)
def test_disk_preflight_projects_parsed_hours(monkeypatch, tmp_path, capsys, duration, hours):
    free = 100 * 1024**3
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=free))
    monkeypatch.setattr(sys, "argv", ["storage.py", "preflight", "--root", str(tmp_path),
                                    "--hours", duration])
    storage.main()
    result = json.loads(capsys.readouterr().out)
    assert result == storage.admission(tmp_path, hours)


@pytest.mark.parametrize("duration", INVALID)
def test_disk_preflight_refuses_before_reading_spec(monkeypatch, tmp_path, capsys, duration):
    monkeypatch.setattr(storage, "admission", lambda *a: pytest.fail("projection started"))
    monkeypatch.setattr(sys, "argv", ["storage.py", "preflight", "--root", str(tmp_path),
                                    "--hours=" + duration, "--spec", str(tmp_path / "missing.json")])
    with pytest.raises(SystemExit) as exc:
        storage.main()
    assert exc.value.code == 2
    assert "invalid duration" in capsys.readouterr().err
