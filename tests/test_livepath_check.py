import json
import ast
import builtins
from functools import partial
import io
from pathlib import Path
import threading
import time

import pytest

from scripts.livepath import check
from arti_buffered_writer import BatchedWriter


def real_shutdown(run):
    """Execute the actual tee and final log-close/farewell AST, never import the bridge."""
    tree = ast.parse((check.ROOT / "arti_bridge.py").read_text(encoding="utf-8"))
    tee_node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_TeeOutput")
    final_try = next(n for n in ast.walk(tree) if isinstance(n, ast.Try) and
                     any(isinstance(v, ast.Constant) and isinstance(v.value, str) and
                         "[Session ended " in v.value for v in ast.walk(n)) and
                     any(isinstance(s, ast.Expr) and isinstance(s.value, ast.Call) and
                         isinstance(s.value.func, ast.Attribute) and s.value.func.attr == "close" for s in n.body))
    farewell = next(n for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and
                    isinstance(n.value.func, ast.Name) and n.value.func.id == "print" and
                    any(isinstance(a, ast.Constant) and a.value == "Sampai jumpa!" for a in n.value.args))
    scope = dict(threading=threading, time=time)
    exec(compile(ast.Module(body=[tee_node], type_ignores=[]), "actual-tee", "exec"), scope)
    console = io.StringIO()
    writer = BatchedWriter((run / "session_logs/one_bridge.log").open("a", encoding="utf-8"))
    tee = scope["_TeeOutput"](console, writer)
    scope.update(_log_fh=writer, print=partial(builtins.print, file=tee))
    exec(compile(ast.Module(body=[final_try, farewell], type_ignores=[]), "actual-shutdown", "exec"), scope)
    (run / "bridge").mkdir()
    (run / "bridge/console.log").write_text(console.getvalue(), encoding="utf-8")


def put(run, name, data):
    p = run / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def event(action, tid, kind="mic", **fields):
    return "[Livepath] " + json.dumps(dict(event=action, turn_id=tid, kind=kind, **fields))


@pytest.fixture
def finished(tmp_path):
    run = tmp_path
    put(run, "realpath.json", dict(stopped=True, rc=0, forced_stop=False))
    identity = dict(code_sha="a" * 40, dll_sha256="b" * 64)
    put(run, "rehearsal-request.json", dict(minutes=45, expected_owner_hashes=["probe"], **identity))
    put(run, "request.json", identity)
    put(run, "restore.json", dict(saves_identical=True, files_identical=True, file_sets_identical=True))
    put(run, "completed.json", {})
    put(run, "rehearsal-budget-stop.json", dict(runner_rc=0, first_reasons=["rehearsal_budget"], observed_reasons=["rehearsal_budget"]))
    put(run, "stop-reasons-0.json", dict(reasons=["rehearsal_budget"]))
    (run / "owner-lines.jsonl").write_text(json.dumps(dict(owner_sha256="probe")) + "\n")
    with (run / "game-liveness.jsonl").open("w") as f:
        for i in range(2701):
            f.write(json.dumps(dict(elapsed_s=i, alive=True, host_lost=False, pids=[123, 456])) + "\n")
    folder = run / "session_logs"
    folder.mkdir()
    (folder / "one_crash.log").write_bytes(b"")
    lines = ["HEALTH CHECK", "[FAIL] VTS API            disconnected", "SISTEM SIAP!",
             "[TTS] Jalur Virtual Cable ditemukan di Device ID: 12", "[TTS] Supertone ready",
             "[Supertone stderr] cuda: diminta", "[Supertone stderr] providers=['CUDAExecutionProvider']"]
    for tid, kind in (("a", "mic"), ("b", "status"), ("c", "moment")):
        fields = dict(owner_sha256="probe") if kind == "mic" else {}
        markers = dict(native_facts=True, kini=True, stardew=True, soul=True)
        lines += [event("start", tid, kind, **fields),
                  event("groq_probe", tid, kind, model="test-model", chars_before=22000, chars_after=14000,
                        system_before=20000, user_before=2000, system_after=12000, user_after=2000,
                        size_forced_fast=False, current_survives=True, present=markers, survives=markers),
                  event("voiced", tid, kind, source="codex"),
                  "21:03:08.877 [Latency] vts_mikir=155ms rag=244ms llm=2163ms tts_synth=1838ms tts_play=6371ms tts=8209ms lain=347ms total=11118ms",
                  event("end", tid, kind, completed=True)]
    (folder / "one_bridge.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    real_shutdown(run)
    return run


def append(run, line):
    with (run / "session_logs/one_bridge.log").open("a", encoding="utf-8") as f:
        f.write("\n" + line)


def test_complete_witnesses_pass(finished):
    report = check.evaluate(finished)
    assert report["verdict"] == "PASS"
    assert report["checks"]["tts_synth"]["p50_ms"] == 1838
    assert report["checks"]["scripted_owner"]["answered"] == 1


def test_empty_run_is_unknown_not_pass(tmp_path):
    report = check.evaluate(tmp_path)
    assert report["verdict"] == "UNKNOWN"
    assert all(c["status"] == "UNKNOWN" for c in report["checks"].values())


@pytest.mark.parametrize("line,check_name", [
    ("[FAIL] Supertone: missing interpreter", "supertone"),
    ("[TTS] voice fallback to edge-tts", "supertone"),
    ("[TTS] switching to edge-tts", "supertone"),
    ("[FAIL] OpenRouter         bad", "startup"),
    ("[FAIL] VTS API: arbitrary failure", "startup"),
    ("VIRTUAL CABLE HILANG — DEFAULT OUTPUT", "playback_route"),
    ("[Supertone stderr] cuda: gagal menambal provider — lanjut CPU", "cuda_requested_and_available"),
])
def test_failures_not_hidden_by_other_good_evidence(finished, line, check_name):
    append(finished, line)
    assert check.evaluate(finished)["checks"][check_name]["status"] == "FAIL"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8"])
def test_binary_and_utf16_spaced_stderr(finished, encoding):
    p = finished / "session_logs/one_bridge.log"
    if encoding == "utf-16":
        p.write_bytes(p.read_text(encoding="utf-8").encode("utf-16"))
    with p.open("ab") as f:
        f.write(b"\n[Supertone stderr] " + "switching to edge-tts".encode("utf-16-le") + b"\xff\x00")
    assert check.evaluate(finished)["verdict"] == "FAIL"


def test_latency_generous_bound(finished):
    p = finished / "session_logs/one_bridge.log"
    p.write_text(p.read_text(encoding="utf-8").replace("tts_synth=1838", "tts_synth=30000"), encoding="utf-8")
    assert check.evaluate(finished)["checks"]["tts_synth"]["status"] == "FAIL"


def test_reply_starts_before_previous_ends(finished):
    append(finished, "\n".join([event("start", "d"), event("start", "e"),
                                event("end", "d", completed=True), event("end", "e", completed=True)]))
    assert check.evaluate(finished)["checks"]["turn_serialization"]["overlaps"] == 1


def test_missing_turn_end_is_unknown(finished):
    append(finished, event("start", "unfinished"))
    assert check.evaluate(finished)["checks"]["turn_serialization"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("name", ["one_crash.log", "one_bridge.log"])
def test_missing_log_never_passes(finished, name):
    (finished / "session_logs" / name).unlink()
    assert check.evaluate(finished)["verdict"] == "UNKNOWN"


def test_crash_is_raw_bytes_not_decoded_whitespace(finished):
    (finished / "session_logs/one_crash.log").write_bytes(b"\x00")
    assert check.evaluate(finished)["checks"]["crash"]["status"] == "FAIL"


def test_unanswered_script_is_unknown(finished):
    put(finished, "rehearsal-request.json", dict(minutes=2, expected_owner_hashes=["probe", "unanswered"]))
    row = check.evaluate(finished)["checks"]["scripted_owner"]
    assert row["status"] == "UNKNOWN" and row["answered"] == 1


def test_missing_game_interval_unknown(finished):
    p = finished / "game-liveness.jsonl"
    p.write_text("\n".join(p.read_text().splitlines()[::10]))
    assert check.evaluate(finished)["checks"]["game_throughout"]["status"] == "UNKNOWN"


def test_dead_game_fails(finished):
    with (finished / "game-liveness.jsonl").open("a") as f:
        f.write(json.dumps(dict(elapsed_s=121, alive=False, host_lost=True, pids=[123, 456])) + "\n")
    assert check.evaluate(finished)["checks"]["game_throughout"]["status"] == "FAIL"


def test_forced_stop_fails(finished):
    put(finished, "realpath.json", dict(stopped=True, forced_stop=True, rc=0))
    assert check.evaluate(finished)["checks"]["clean_stop"]["status"] == "FAIL"


def test_reports_never_echo_viewer_text(finished):
    append(finished, "Viewer PRIVATE_VIEWER says PRIVATE_WORDS\n[FAIL] Evil PRIVATE_WORDS")
    assert check.main([str(finished)]) == 1
    assert "PRIVATE" not in (finished / "livepath-report.json").read_text()
    assert "PRIVATE" not in (finished / "livepath-report.txt").read_text()


def test_percentile_interpolates():
    assert check.percentile([1000, 3000], .9) == 2800


def test_missing_synth_field_keeps_latency_unknown(finished):
    append(finished, "[Latency] total=1234ms")
    assert check.evaluate(finished)["checks"]["tts_synth"]["status"] == "UNKNOWN"


def test_shutdown_evidence_uses_real_sink_order(finished):
    assert "Sampai jumpa!" not in (finished / "session_logs/one_bridge.log").read_text(encoding="utf-8")
    assert "[Session ended " in (finished / "session_logs/one_bridge.log").read_text(encoding="utf-8")
    assert check.evaluate(finished)["checks"]["clean_stop"]["status"] == "PASS"
    (finished / "bridge/console.log").unlink()
    assert check.evaluate(finished)["checks"]["clean_stop"]["status"] == "UNKNOWN"


def test_two_minute_run_fails_default_floor(finished):
    p = finished / "rehearsal-request.json"
    request = json.loads(p.read_text())
    request["minutes"] = 2
    put(finished, "rehearsal-request.json", request)
    path = finished / "game-liveness.jsonl"
    path.write_text("\n".join(path.read_text().splitlines()[:121]) + "\n")
    assert check.evaluate(finished)["checks"]["played_duration"]["status"] == "FAIL"
    assert check.evaluate(finished, expect_minutes=2)["verdict"] == "PASS"


@pytest.mark.parametrize("failure", ["other_first", "later_fault", "runner_crash", "unsafe", "missing_budget", "missing_stop"])
def test_budget_cannot_hide_faults_or_missing_receipts(finished, failure):
    if failure == "other_first":
        put(finished, "stop-reasons-0.json", dict(reasons=["disk_projection"]))
    elif failure == "later_fault":
        put(finished, "observed-stop-reasons-0.json", dict(reasons=["rehearsal_budget", "window_not_drawable"]))
    elif failure == "runner_crash":
        put(finished, "rehearsal-budget-stop.json", dict(runner_rc=1, first_reasons=["rehearsal_budget"], observed_reasons=["rehearsal_budget"]))
    elif failure == "unsafe":
        (finished / "UNSAFE_NOT_RESTORED").touch()
    elif failure == "missing_budget":
        (finished / "rehearsal-budget-stop.json").unlink()
    else:
        (finished / "stop-reasons-0.json").unlink()
    assert check.evaluate(finished)["verdict"] == ("UNKNOWN" if failure in {"missing_budget", "missing_stop"} else "FAIL")


@pytest.mark.parametrize("source", ["incharacter", "groq", "openrouter", "cursor", "local", "unknown"])
def test_real_playback_from_wrong_or_missing_brain_never_answers(finished, source):
    path = finished / "session_logs/one_bridge.log"
    path.write_text(path.read_text(encoding="utf-8").replace('"source": "codex"', '"source": "' + source + '"'), encoding="utf-8")
    report = check.evaluate(finished)
    assert report["checks"]["scripted_owner"]["answered"] == 0
    assert report["checks"]["voice_provider"]["sources"][source] == 3
    assert report["verdict"] == ("UNKNOWN" if source == "unknown" else "FAIL")


def test_quiet_moment_and_sampler_gap_are_unknown_with_reasons(finished):
    path = finished / "session_logs/one_bridge.log"
    path.write_text("\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                              if '"kind": "moment"' not in line), encoding="utf-8")
    path = finished / "game-liveness.jsonl"
    path.write_text("\n".join(path.read_text().splitlines()[::10]))
    report = check.evaluate(finished)
    assert report["verdict"] == "UNKNOWN"
    assert "natural trigger not guaranteed" in report["checks"]["moment_speech"]["reason"]
    assert report["checks"]["game_throughout"]["reason"] == "sampler_gap_over_5s"
    assert check.main([str(finished)]) == 2


def test_budget_stop_uncertainty_never_forgives_other_fault(finished):
    put(finished, "rehearsal-budget-stop.json", dict(status="UNKNOWN", runner_rc=2,
        reason="day_start_pass_not_clean:owner_stop", first_reasons=["rehearsal_budget"], observed_reasons=["rehearsal_budget"]))
    assert check.evaluate(finished)["verdict"] == "UNKNOWN"
    append(finished, "[FAIL] real independent fault")
    assert check.evaluate(finished)["verdict"] == "FAIL"


@pytest.mark.parametrize("lost", ["current", "native_facts"])
def test_groq_retry_loses_current_or_facts_fails(finished, lost):
    path = finished / "session_logs/one_bridge.log"
    text = path.read_text(encoding="utf-8")
    if lost == "current":
        text = text.replace('"current_survives": true', '"current_survives": false', 1)
    else:
        text = text.replace('"survives": {"native_facts": true', '"survives": {"native_facts": false', 1)
    path.write_text(text, encoding="utf-8")
    assert check.evaluate(finished)["checks"]["groq_fallback_dry_run"]["status"] == "FAIL"


def test_groq_probe_error_never_passes(finished):
    append(finished, event("groq_probe_error", "a"))
    assert check.evaluate(finished)["checks"]["groq_fallback_dry_run"]["status"] == "UNKNOWN"
    assert check.evaluate(finished)["checks"]["groq_fallback_dry_run"]["reason"] == "probe_error"


def test_early_return_without_probe_reports_reason(finished):
    append(finished, "\n".join([event("start", "dropped", "curious"), event("end", "dropped", "curious", completed=True)]))
    report = check.evaluate(finished)
    assert report["verdict"] == "UNKNOWN"
    assert report["checks"]["groq_fallback_dry_run"]["reason"] == "missing_probe_for_started_turn"


def test_short_report_names_artifact_duration_and_routing(finished):
    assert check.main([str(finished)]) == 0
    text = (finished / "livepath-report.txt").read_text()
    assert "played_minutes=45.0" in text and "a" * 40 in text and "b" * 64 in text
    assert "not_size_forced_share=1.0" in text
