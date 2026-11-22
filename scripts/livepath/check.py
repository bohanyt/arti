"""Counts-only verdict from a finished rehearsal. Missing evidence is UNKNOWN."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.livepath.disk import summarize as summarize_disk

BASELINE = dict(p50_ms=2628, p90_ms=5209)
# Exact health-row names, not a wildcard for a provider or arbitrary FAIL line.
ALLOWED_STARTUP_FAILS = ("Mic devices", "Mic selected", "VTS API", "PTT hotkey")


def supertone_fallbacks(text):
    return len(re.findall(r"\[FAIL\]\s*Supertone:|fallback to edge[-_]tts|switching to edge[-_]tts|"
                         r"menggunakan edge_tts|edge_tts juga gagal|voice fallback", text, re.I))


def log_text(path):
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace").replace("\x00", "")
    # Native stderr can contain UTF-16LE bytes embedded in otherwise UTF-8 logs.
    return raw.replace(b"\x00", b"").decode("utf-8", errors="replace")


def percentile(values, fraction):
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lo, hi = math.floor(index), math.ceil(index)
    return round(values[lo] + (values[hi] - values[lo]) * (index - lo), 1)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def json_rows(path):
    if not path.is_file():
        return [], 0
    rows, invalid = [], 0
    for line in log_text(path).splitlines():
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError()
            rows.append(row)
        except ValueError:
            invalid += 1
    return rows, invalid


def evaluate(run, *, latency_multiplier=4, expect_minutes=45):
    if not math.isfinite(expect_minutes) or expect_minutes <= 0:
        raise ValueError("expect_minutes must be positive and finite")
    run = Path(run)
    logs = sorted((run / "session_logs").glob("*_bridge.log"))
    crashes = sorted((run / "session_logs").glob("*_crash.log"))
    text = "\n".join(log_text(p) for p in logs)
    checks = {}

    def result(name, status, **counts):
        checks[name] = dict(status=status, **counts)

    result("log_set", "PASS" if len(logs) == len(crashes) == 1 else "UNKNOWN",
           bridge_files=len(logs), crash_files=len(crashes))

    crash_bytes = sum(p.stat().st_size for p in crashes)
    result("crash", "FAIL" if crash_bytes else "PASS" if crashes else "UNKNOWN",
           files=len(crashes), bytes=crash_bytes)
    state = read_json(run / "realpath.json", {})
    console_path = run / "bridge/console.log"
    console = log_text(console_path) if console_path.is_file() else ""
    ended = "[Session ended " in text and "Sampai jumpa!" in console
    clean = state.get("stopped") and state.get("rc") == 0 and not state.get("forced_stop") and ended
    result("clean_stop", "PASS" if clean else "FAIL" if state.get("forced_stop") or state.get("rc") not in (None, 0) else "UNKNOWN")

    allowed, unexpected = 0, 0
    for line in text.splitlines():
        if "[FAIL]" not in line:
            continue
        rest = line.split("[FAIL]", 1)[1].strip()
        if any(re.match(re.escape(name) + r"\s{2,}\S", rest) for name in ALLOWED_STARTUP_FAILS):
            allowed += 1
        else:
            unexpected += 1
    health_present = "HEALTH CHECK" in text and "SISTEM SIAP!" in text
    result("startup", "FAIL" if unexpected else "PASS" if health_present else "UNKNOWN",
           allowed_failures=allowed, unexpected_failures=unexpected)
    fallback = supertone_fallbacks(text)
    ready = "[TTS] Supertone ready" in text
    result("supertone", "FAIL" if fallback else "PASS" if ready else "UNKNOWN", fallbacks=fallback)
    cable = "Jalur Virtual Cable ditemukan" in text
    unsafe = "DEFAULT OUTPUT" in text or "suara ke default speaker" in text
    result("playback_route", "FAIL" if unsafe else "PASS" if cable else "UNKNOWN")
    cpu = bool(re.search(r"cuda:.*(?:lanjut CPU|tidak diminta)|fall(?:ing)? back to CPU", text, re.I))
    cuda = "CUDAExecutionProvider" in text and "cuda: diminta" in text
    result("cuda_requested_and_available", "FAIL" if cpu else "PASS" if cuda else "UNKNOWN")

    latencies = []
    missing_latency = 0
    for line in text.splitlines():
        if "[Latency]" in line:
            match = re.search(r"\btts_synth=(\d+)ms\b", line)
            if match:
                latencies.append(int(match[1]))
            else:
                missing_latency += 1
    if latencies:
        p50, p90 = percentile(latencies, .5), percentile(latencies, .9)
        result("tts_synth", "FAIL" if p50 > BASELINE["p50_ms"] * latency_multiplier or
               p90 > BASELINE["p90_ms"] * latency_multiplier else "UNKNOWN" if missing_latency else "PASS", samples=len(latencies),
               p50_ms=p50, p90_ms=p90, baseline=BASELINE,
               missing_synth_lines=missing_latency,
               p50_ratio=round(p50 / BASELINE["p50_ms"], 3), p90_ratio=round(p90 / BASELINE["p90_ms"], 3),
               failure_multiplier=latency_multiplier)
    else:
        result("tts_synth", "UNKNOWN", samples=0, baseline=BASELINE)

    active, started, voiced, ended_ids = {}, {}, set(), set()
    overlaps = malformed = unmatched = incomplete = 0
    voiced_kinds = Counter()
    sources, source_by_turn = Counter(), {}
    probes, probe_errors = {}, 0
    for line in text.splitlines():
        if "[Livepath] " not in line:
            continue
        try:
            event = json.loads(line.split("[Livepath] ", 1)[1])
            if event.get("event") == "drained":
                continue
            tid, action = event["turn_id"], event["event"]
            if action == "groq_probe":
                required = ("chars_before", "chars_after", "system_before", "user_before", "system_after", "user_after")
                if tid in probes or any(type(event.get(k)) is not int or event[k] < 0 for k in required):
                    raise ValueError("bad probe counts")
                if type(event.get("size_forced_fast")) is not bool or type(event.get("current_survives")) is not bool:
                    raise ValueError("bad probe booleans")
                for key in ("native_facts", "kini", "stardew", "soul"):
                    if any(type(event.get(field, {}).get(key)) is not bool for field in ("present", "survives")):
                        raise ValueError("bad marker witness")
                probes[tid] = event
            elif action == "groq_probe_error":
                probe_errors += 1
            elif action == "start":
                overlaps += bool(active)
                if tid in started:
                    unmatched += 1
                active[tid] = event
                started[tid] = event
            elif action == "voiced":
                if tid not in active:
                    unmatched += 1
                voiced.add(tid)
                voiced_kinds[event["kind"]] += 1
                source = event.get("source", "unknown")
                if source not in {"codex", "cursor", "agy", "groq", "openrouter", "gemini", "sambanova", "incharacter", "local"}:
                    source = "unknown"
                sources[source] += 1
                source_by_turn[tid] = source
            elif action == "end":
                if tid not in active:
                    unmatched += 1
                active.pop(tid, None)
                if event.get("completed") is not True:
                    incomplete += 1
                else:
                    ended_ids.add(tid)
            else:
                malformed += 1
        except (ValueError, KeyError, TypeError):
            malformed += 1
    result("turn_serialization", "FAIL" if overlaps or malformed or unmatched or incomplete else
           "UNKNOWN" if active or not started else "PASS", starts=len(started), completed=len(ended_ids),
           overlaps=overlaps, malformed=malformed, unmatched=unmatched, incomplete=incomplete, open_turns=len(active))
    losses = sum(e.get("current_survives") is False or
                 (e["present"]["native_facts"] and not e["survives"]["native_facts"]) for e in probes.values())
    unforced = sum(not e["size_forced_fast"] for e in probes.values())
    result("groq_fallback_dry_run", "FAIL" if losses else "PASS" if probes and
           set(probes) == set(started) and not probe_errors else "UNKNOWN",
           turns=len(probes), errors=probe_errors, critical_losses=losses,
           reason="critical_prompt_loss" if losses else "probe_error" if probe_errors else
           "missing_probe_for_started_turn" if not probes or set(probes) != set(started) else "all_started_turns_probed",
           not_size_forced_turns=unforced, not_size_forced_share=round(unforced / len(probes), 4) if probes else None,
           models=dict(Counter(e.get("model", "unknown") for e in probes.values())),
           marker_present={k: sum(e["present"][k] for e in probes.values()) for k in ("native_facts", "kini", "stardew", "soul")},
           marker_survives={k: sum(e["survives"][k] for e in probes.values()) for k in ("native_facts", "kini", "stardew", "soul")})
    # Only a completed real playback witness counts, not enqueuing a status/moment.
    answered_ids = voiced & ended_ids & {t for t, source in source_by_turn.items() if source == "codex"}
    fallback_count = sum(n for source, n in sources.items() if source not in {"codex", "unknown"})
    result("voice_provider", "FAIL" if fallback_count else "UNKNOWN" if sources["unknown"] or not answered_ids else "PASS",
           sources=dict(sources), fallback_replies=fallback_count,
           reason="fallback_or_canned_reply" if fallback_count else "missing_source_or_answer" if sources["unknown"] or not answered_ids else "codex_answered")
    for kind in ("status", "moment"):
        count = sum(t in answered_ids and e.get("kind") == kind for t, e in started.items())
        result(kind + "_speech", "PASS" if count else "UNKNOWN", voiced_turns=count,
               reason="codex_playback_completed" if count else "no_completed_codex_" + kind + "; natural trigger not guaranteed (activity events disabled)")
    request = read_json(run / "rehearsal-request.json", {})
    disk_rows, disk_invalid = json_rows(run / "disk-space.jsonl")
    disk = summarize_disk(disk_rows, request.get("min_free_gib", 15))
    disk["malformed"] = disk_invalid
    if disk_invalid and disk["status"] == "RECORDED":
        disk["status"] = "UNKNOWN"
    if disk.get("floor_crossed"):
        result("disk_floor", "FAIL", crossings=1)
    expected = request.get("expected_owner_hashes", [])
    sent, invalid = json_rows(run / "owner-lines.jsonl")
    sent_counts = Counter(e.get("owner_sha256") for e in sent)
    answered_counts = Counter(e.get("owner_sha256") for t, e in started.items()
                              if e.get("kind") == "mic" and t in answered_ids)
    answered = sum(min(n, answered_counts[h], sent_counts[h]) for h, n in Counter(expected).items())
    result("scripted_owner", "FAIL" if invalid else "PASS" if expected and answered == len(expected)
           else "UNKNOWN", expected=len(expected), sent=len(sent), answered=answered, malformed=invalid,
           stale_mic_drops=text.count("[Queue] Drop stale mic"),
           expired_queue_drops=text.count("[Queue] Kedaluwarsa"))

    game, invalid_game = json_rows(run / "game-liveness.jsonl")
    duration = request.get("minutes", 0) * 60
    stamps = [r.get("elapsed_s") for r in game]
    valid = bool(game) and all(isinstance(t, (int, float)) and math.isfinite(t) for t in stamps)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])] if valid else []
    lost = sum(r.get("alive") is False or r.get("host_lost") is True for r in game)
    game_unknown = sum(r.get("alive") is not True or r.get("host_lost") is not False or
                       len(r.get("pids", [])) != 2 or not all(r.get("pids", [])) for r in game)
    covered = (valid and duration > 0 and stamps[0] <= 5 and stamps[-1] >= duration - 5
               and all(0 <= gap <= 5 for gap in gaps))
    played_s = stamps[-1] - stamps[0] if valid else None
    result("played_duration", "UNKNOWN" if played_s is None else "PASS" if duration >= expect_minutes * 60
           and played_s >= expect_minutes * 60 - 5 else "FAIL" if duration < expect_minutes * 60 else "UNKNOWN", expected_minutes=expect_minutes,
           planned_minutes=request.get("minutes"), played_minutes=round(played_s / 60, 3) if played_s is not None else None,
           sampling_tolerance_s=5)
    result("game_throughout", "FAIL" if lost else "PASS" if covered and not game_unknown and not invalid_game else "UNKNOWN",
           samples=len(game), lost_samples=lost, malformed=invalid_game,
           coverage_s=round(stamps[-1] - stamps[0], 2) if valid else 0,
           max_gap_s=round(max(gaps, default=0), 2),
           reason="observed_game_loss" if lost else "complete_sampled_coverage" if covered and not game_unknown and not invalid_game
           else "sampler_gap_over_5s" if any(gap > 5 for gap in gaps) else "missing_or_uncertain_process_coverage")
    restore = read_json(run / "restore.json", {})
    result("game_restore", "PASS" if all(restore.get(k) is True for k in
           ("saves_identical", "files_identical", "file_sets_identical")) else "UNKNOWN")
    from scripts.livepath.safety import budget_only, STOP_UNCERTAINTIES
    budget = read_json(run / "rehearsal-budget-stop.json", {})
    first_stop_files = list(run.glob("stop-reasons-*.json"))
    stop_files = [*first_stop_files, *run.glob("observed-stop-reasons-*.json")]
    stop_rows = [read_json(p, {}) for p in stop_files]
    stops_bad = any(row.get("reasons") != ["rehearsal_budget"] for row in stop_rows)
    budget_unknown = (budget.get("status") == "UNKNOWN" and budget.get("runner_rc") == 2
                      and budget.get("reason") in STOP_UNCERTAINTIES
                      and budget.get("first_reasons") == budget.get("observed_reasons") == ["rehearsal_budget"])
    if (run / "failure.json").exists() or (run / "UNSAFE_NOT_RESTORED").exists() or stops_bad or budget.get("status") == "FAIL" or (
            budget and (not budget_only(budget.get("first_reasons"), budget.get("runner_rc")) or
                        budget.get("observed_reasons") != ["rehearsal_budget"]) and not budget_unknown):
        result("runner", "FAIL", failures=1)
    else:
        result("runner", "PASS" if (run / "completed.json").is_file() and budget and first_stop_files and not budget_unknown else "UNKNOWN",
               reason=budget.get("reason", "missing_completion_or_stop_receipt") if budget_unknown or not budget else "budget_stop_recorded")
    identity = read_json(run / "request.json", {})
    code_sha, dll_sha = request.get("code_sha"), request.get("dll_sha256")
    identity_ok = (isinstance(code_sha, str) and re.fullmatch(r"[0-9a-f]{40}", code_sha) and
                   isinstance(dll_sha, str) and re.fullmatch(r"[0-9a-f]{64}", dll_sha) and
                   identity.get("code_sha") == code_sha and identity.get("dll_sha256") == dll_sha)
    result("artifact_identity", "PASS" if identity_ok else "UNKNOWN")
    verdict = "FAIL" if any(c["status"] == "FAIL" for c in checks.values()) else "UNKNOWN" if any(c["status"] == "UNKNOWN" for c in checks.values()) else "PASS"
    return dict(schema="arti.livepath.check/1", verdict=verdict, evidence="OFFLINE_LOG_REVIEW",
                bridge_logs=len(logs), checks=checks, disk=disk,
                code_sha=code_sha, dll_sha256=dll_sha, played_minutes=checks["played_duration"]["played_minutes"],
                limits=["Turn markers cover the main queued reply path, not acoustic output or disabled reflexes.",
                        "CUDA request and provider availability do not prove every ONNX node ran on GPU.",
                        "Game continuity is sampled at <=5 seconds; gaps are UNKNOWN.",
                        "No mic, VTS, YouTube, donations, OBS or keyed fallback verification."])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run", type=Path)
    p.add_argument("--expect-minutes", type=float, default=45, help="minimum played duration; default 45")
    a = p.parse_args(argv)
    report = evaluate(a.run, expect_minutes=a.expect_minutes)
    short = (f"{report['verdict']} played_minutes={report['played_minutes']} expected_minutes={a.expect_minutes} "
             f"code_sha={report['code_sha']} dll_sha256={report['dll_sha256']} " +
             ", ".join(k + "=" + v["status"] for k, v in report["checks"].items()))
    probe = report["checks"]["groq_fallback_dry_run"]
    short += f"\nGroq offline: not_size_forced_share={probe['not_size_forced_share']} critical_losses={probe['critical_losses']} turns={probe['turns']}"
    disk = report["disk"]
    short += (f"\nC: free GiB start={disk.get('start_free_gib', 'UNKNOWN')} "
              f"end={disk.get('end_free_gib', 'UNKNOWN')} lowest={disk.get('lowest_free_gib', 'UNKNOWN')} "
              f"largest_fall={disk.get('largest_fall_gib', 'UNKNOWN')} samples={disk['samples']} "
              f"floor={disk['min_free_gib']} status={disk['status']}. {disk['note']}")
    (a.run / "livepath-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (a.run / "livepath-report.txt").write_text(short + "\n", encoding="utf-8")
    print(short)
    return 0 if report["verdict"] == "PASS" else 2 if report["verdict"] == "UNKNOWN" else 1


if __name__ == "__main__":
    raise SystemExit(main())
