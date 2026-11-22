import json
from types import SimpleNamespace

from scripts.livepath.disk import DiskRecorder, GIB, summarize
from scripts.livepath import check


def test_records_start_sample_end_and_largest_prior_peak_fall(tmp_path):
    values = iter([30, 32, 25, 28])
    recorder = DiskRecorder(tmp_path / "disk.jsonl", usage=lambda drive: SimpleNamespace(free=next(values) * GIB),
                            clock=lambda: 1234)
    recorder.sample("start")
    recorder.sample()
    recorder.sample()
    recorder.stop()
    rows = [json.loads(s) for s in recorder.path.read_text().splitlines()]
    report = summarize(rows)
    assert report["status"] == "RECORDED"
    assert report["samples"] == 4
    assert report["start_free_gib"] == 30 and report["end_free_gib"] == 28
    assert report["lowest_free_gib"] == 25 and report["largest_fall_gib"] == 7
    assert "OBS" in report["note"]
    assert recorder.floor_crossed is False


def test_large_fall_is_recorded_without_failing_above_floor(tmp_path):
    rows = [dict(phase="start", free_bytes=40 * GIB), dict(phase="end", free_bytes=20 * GIB)]
    (tmp_path / "disk-space.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    report = check.evaluate(tmp_path)
    assert report["disk"]["largest_fall_gib"] == 20
    assert report["disk"]["status"] == "RECORDED"
    assert "disk_floor" not in report["checks"]


def test_floor_crossing_even_if_space_recovers_fails(tmp_path):
    rows = [dict(phase="start", free_bytes=30 * GIB), dict(phase="sample", free_bytes=14 * GIB),
            dict(phase="end", free_bytes=31 * GIB)]
    (tmp_path / "disk-space.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    report = check.evaluate(tmp_path)
    assert report["disk"]["status"] == "FLOOR_CROSSED"
    assert report["checks"]["disk_floor"]["status"] == "FAIL"
    assert report["verdict"] == "FAIL"


def test_missing_start_or_end_stays_unknown():
    assert summarize([])["status"] == "UNKNOWN"
    assert summarize([dict(phase="start", free_bytes=20 * GIB)])["status"] == "UNKNOWN"


def test_recorded_floor_is_inclusive():
    assert summarize([dict(phase="start", free_bytes=15 * GIB),
                      dict(phase="end", free_bytes=15 * GIB)])["status"] == "RECORDED"


def test_floor_check_uses_booking_value(tmp_path):
    (tmp_path / "rehearsal-request.json").write_text(json.dumps(dict(min_free_gib=20)))
    (tmp_path / "disk-space.jsonl").write_text(json.dumps(dict(phase="sample", free_bytes=19 * GIB)))
    assert check.evaluate(tmp_path)["checks"]["disk_floor"]["status"] == "FAIL"


def test_disk_in_short_text_and_json(tmp_path, capsys):
    rows = [dict(phase="start", free_bytes=30 * GIB), dict(phase="end", free_bytes=28 * GIB)]
    (tmp_path / "disk-space.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    check.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert "start=30.0" in out and "end=28.0" in out and "largest_fall=2.0" in out
    assert "OBS is closed" in out
    assert json.loads((tmp_path / "livepath-report.json").read_text())["disk"]["samples"] == 2
