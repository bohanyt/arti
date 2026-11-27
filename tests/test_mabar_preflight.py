"""Bring-up preflight: every OK / BLOCKER / WARN branch, fakes only."""
from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.livepath import bringup, mabar


def _ok_kwargs(**over):
    base = dict(
        dirty_names=[], scratch_outside=True, scratch_path="C:/AppData/ARTI/livepath-scratch/preflight",
        tmp_ignored=True, vts_ok=True, vts_port=8002, no_vts=False, launcher=None,
        embed=dict(ok=True, using="primary", base="http://127.0.0.1:1234/v1", fallback=""),
        watcher_present=True, alert_keys=[("SAWERIA_STREAM_KEY", 24), ("STREAMLABS_SOCKET_TOKEN", 40),
                                          ("TRAKTEER_HASH", 16), ("TRAKTEER_STREAM_KEY", 16)],
        alerts_enabled=True, dll_ok=True, dll_detail="installed hash matches booking",
    )
    base.update(over)
    return base


def _ns(**kw):
    d = dict(run=Path("."), spec=Path("booking.json"), vts_port=8002, no_vts=False, no_alerts=False)
    d.update(kw)
    return SimpleNamespace(**d)


def test_parse_porcelain_names_modified_untracked_and_renames():
    text = " M scripts/livepath/mabar.py\n?? .tmp/\nR  old.py -> new.py\n"
    assert bringup.parse_porcelain(text) == ["scripts/livepath/mabar.py", ".tmp/", "new.py"]
    assert bringup.parse_porcelain("") == []


def test_source_blocker_names_the_dirty_paths():
    checks = bringup.evaluate_checks(**_ok_kwargs(dirty_names=[".tmp/", "notes.md"]))
    src = next(c for c in checks if c.name == "source")
    assert src.status == "BLOCKER" and ".tmp/" in src.detail and "notes.md" in src.detail
    assert "clean source required" in src.detail


def test_source_ok_when_clean():
    src = next(c for c in bringup.evaluate_checks(**_ok_kwargs()) if c.name == "source")
    assert src.status == "OK"


def test_scratch_blocker_when_inside_tree_or_tmp_not_ignored():
    inside = next(c for c in bringup.evaluate_checks(
        **_ok_kwargs(scratch_outside=False, scratch_path="C:/repo/.tmp", tmp_ignored=False)
    ) if c.name == "scratch")
    assert inside.status == "BLOCKER" and "inside" in inside.detail
    warn = next(c for c in bringup.evaluate_checks(
        **_ok_kwargs(scratch_outside=True, tmp_ignored=False)
    ) if c.name == "scratch")
    assert warn.status == "WARN" and "not gitignored" in warn.detail
    ok = next(c for c in bringup.evaluate_checks(**_ok_kwargs()) if c.name == "scratch")
    assert ok.status == "OK" and "outside" in ok.detail and "gitignored" in ok.detail


def test_vts_blocker_names_window_2_and_has_no_invented_launcher():
    checks = bringup.evaluate_checks(**_ok_kwargs(vts_ok=False))
    vts = next(c for c in checks if c.name == "vts")
    assert vts.status == "BLOCKER" and "Window 2" in vts.detail and "8002" in vts.detail
    assert "no VTS launcher" in vts.detail
    with_launch = next(c for c in bringup.evaluate_checks(
        **_ok_kwargs(vts_ok=False, launcher="scripts/livepath/start_vts.ps1")
    ) if c.name == "vts")
    assert "start_vts.ps1" in with_launch.detail
    skipped = next(c for c in bringup.evaluate_checks(**_ok_kwargs(no_vts=True, vts_ok=False)) if c.name == "vts")
    assert skipped.status == "WARN"
    assert bringup.vts_launcher(Path(__file__).resolve().parents[1]) is None


def test_embed_primary_ok_fallback_used_and_both_down():
    ok = next(c for c in bringup.evaluate_checks(**_ok_kwargs()) if c.name == "embed")
    assert ok.status == "OK"
    warn = next(c for c in bringup.evaluate_checks(**_ok_kwargs(
        embed=dict(ok=True, using="fallback", base="http://127.0.0.1:1234/v1", fallback="http://127.0.0.1:1234/v1"),
    )) if c.name == "embed")
    assert warn.status == "WARN" and "fallback" in warn.detail
    dead = next(c for c in bringup.evaluate_checks(**_ok_kwargs(embed=dict(ok=False, using=None, fallback=""))) if c.name == "embed")
    assert dead.status == "BLOCKER" and "unreachable" in dead.detail
    remote = next(c for c in bringup.evaluate_checks(**_ok_kwargs(
        embed=dict(ok=True, using="primary", base="http://192.0.2.50:1234/v1", fallback=""),
    )) if c.name == "embed")
    assert remote.status == "WARN" and "no fallback" in remote.detail


def test_watcher_and_alert_keys_names_and_lengths_only():
    ok = {c.name: c for c in bringup.evaluate_checks(**_ok_kwargs())}
    assert ok["watcher"].status == "OK" and "ARTI_VIDEO_GEMINI_API_KEY" in ok["watcher"].detail
    assert "len=" in ok["alerts"].detail and ok["alerts"].status == "OK"
    missing_w = next(c for c in bringup.evaluate_checks(**_ok_kwargs(watcher_present=False)) if c.name == "watcher")
    assert missing_w.status == "BLOCKER" and "GEMINI_API_KEY" in missing_w.detail
    skipped = next(c for c in bringup.evaluate_checks(
        **_ok_kwargs(watcher_present=False, alerts_enabled=False)
    ) if c.name == "watcher")
    assert skipped.status == "WARN"
    none = next(c for c in bringup.evaluate_checks(**_ok_kwargs(
        alert_keys=[("SAWERIA_STREAM_KEY", 0), ("STREAMLABS_SOCKET_TOKEN", 0),
                    ("TRAKTEER_HASH", 0), ("TRAKTEER_STREAM_KEY", 0)],
    )) if c.name == "alerts")
    assert none.status == "BLOCKER" and "missing" in none.detail and "24" not in none.detail
    partial = next(c for c in bringup.evaluate_checks(**_ok_kwargs(
        alert_keys=[("SAWERIA_STREAM_KEY", 12), ("STREAMLABS_SOCKET_TOKEN", 0),
                    ("TRAKTEER_HASH", 0), ("TRAKTEER_STREAM_KEY", 0)],
    )) if c.name == "alerts")
    assert partial.status == "WARN" and "len=12" in partial.detail and "STREAMLABS_SOCKET_TOKEN missing" in partial.detail


def test_dll_match_and_mismatch():
    ok = next(c for c in bringup.evaluate_checks(**_ok_kwargs()) if c.name == "dll")
    assert ok.status == "OK"
    bad = next(c for c in bringup.evaluate_checks(
        **_ok_kwargs(dll_ok=False, dll_detail="installed abc != booking def")
    ) if c.name == "dll")
    assert bad.status == "BLOCKER" and "booking" in bad.detail


def test_run_preflight_prints_one_line_each_and_exits_nonzero(capsys, tmp_path):
    lines = []
    rc = bringup.run_preflight(
        _ns(), root=tmp_path, print_fn=lines.append,
        porcelain_fn=lambda root: "?? .tmp/\n",
        ignored_fn=lambda root, rel: False,
        scratch_fn=lambda root, env: tmp_path / ".tmp",
        vts_fn=lambda: False,
        launcher_fn=lambda root: None,
        embed_fn=lambda: dict(ok=False, using=None, fallback=""),
        watcher_fn=lambda: None,
        alert_fn=lambda: [("SAWERIA_STREAM_KEY", 0)],
        dll_fn=lambda: (False, "missing"),
        env={"LOCALAPPDATA": str(tmp_path / "ad")},
        memory_fn=lambda: 50.0,
        disk_fn=lambda: 40.0,
        memory_snapshot_fn=lambda: None,
    )
    assert rc == 1
    assert all(x.split()[0] in ("OK", "BLOCKER", "WARN") for x in lines)
    assert any(x.startswith("BLOCKER source:") and ".tmp/" in x for x in lines)
    assert any(x.startswith("BLOCKER vts:") and "8002" in x for x in lines)
    assert any(x.startswith("BLOCKER embed:") for x in lines)
    assert any(x.startswith("BLOCKER watcher:") for x in lines)
    assert any(x.startswith("BLOCKER dll:") for x in lines)


def test_run_preflight_all_ok_is_zero(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = bringup.run_preflight(
        _ns(), root=repo, print_fn=lambda *_: None,
        porcelain_fn=lambda root: "",
        ignored_fn=lambda root, rel: True,
        scratch_fn=lambda root, env: tmp_path / "scratch",
        vts_fn=lambda: True,
        launcher_fn=lambda root: None,
        embed_fn=lambda: dict(ok=True, using="primary", base="http://127.0.0.1:1234/v1",
                              fallback="http://127.0.0.1:1234/v1"),
        watcher_fn=lambda: "x",
        alert_fn=lambda: [("SAWERIA_STREAM_KEY", 8)],
        dll_fn=lambda: (True, "installed hash matches booking"),
        env={"LOCALAPPDATA": str(tmp_path)},
        # Injected: the real machine's memory (85%+ during a stream) made this test fail (11-15).
        memory_fn=lambda: 50.0,
        disk_fn=lambda: 40.0,
    )
    assert rc == 0


def test_voice_and_rejoin_stop_on_preflight_blocker(monkeypatch, tmp_path):
    monkeypatch.setattr(mabar, "preflight", lambda a, **k: 1)
    monkeypatch.setattr(mabar, "smapi_count", lambda: "2")
    popped = []
    monkeypatch.setattr(mabar.subprocess, "Popen", lambda *a, **k: popped.append(1))
    a = argparse.Namespace(run=tmp_path, spec=tmp_path / "s.json", vts_port=8002, no_vts=False, no_alerts=False,
                           name="Arti")
    assert mabar.voice(a) == 1 and popped == []
    assert mabar.rejoin(a) == 1


def test_preflight_dll_helper_match_and_mismatch(tmp_path, monkeypatch):
    dll = tmp_path / "ArtiStardewBridge.dll"
    dll.write_bytes(b"pkg")
    monkeypatch.setattr(mabar, "MOD", tmp_path)
    monkeypatch.setattr(mabar.rehearse, "read_booking", lambda spec: {"dllSha256": mabar.sha(dll)})
    assert mabar._preflight_dll(_ns(spec=tmp_path / "b.json"))[0] is True
    monkeypatch.setattr(mabar.rehearse, "read_booking", lambda spec: {"dllSha256": "0" * 64})
    ok, detail = mabar._preflight_dll(_ns(spec=tmp_path / "b.json"))
    assert ok is False and "booking" in detail
    monkeypatch.setattr(mabar.rehearse, "read_booking", lambda spec: (_ for _ in ()).throw(KeyError("dllSha256")))
    assert mabar._preflight_dll(_ns())[0] is False


def test_preflight_is_a_mabar_step():
    assert "preflight" in mabar.STEPS


def test_preflight_memory_disk_voice_thresholds_and_unknown():
    mem_ok = next(c for c in bringup.evaluate_checks(**_ok_kwargs(memory_pct=40)) if c.name == "memory")
    assert mem_ok.status == "OK" and "40" in mem_ok.detail
    mem_warn = next(c for c in bringup.evaluate_checks(**_ok_kwargs(memory_pct=70)) if c.name == "memory")
    assert mem_warn.status == "WARN"
    mem_block = next(c for c in bringup.evaluate_checks(**_ok_kwargs(memory_pct=90)) if c.name == "memory")
    assert mem_block.status == "BLOCKER"
    mem_unk = next(c for c in bringup.evaluate_checks(**_ok_kwargs(memory_pct=None)) if c.name == "memory")
    assert mem_unk.status == "WARN" and "UNKNOWN" in mem_unk.detail
    disk_ok = next(c for c in bringup.evaluate_checks(**_ok_kwargs(disk_free_gib=20)) if c.name == "disk")
    assert disk_ok.status == "OK"
    disk_warn = next(c for c in bringup.evaluate_checks(**_ok_kwargs(disk_free_gib=15.0)) if c.name == "disk")
    assert disk_warn.status == "WARN" and "15.5" in disk_warn.detail
    disk_unk = next(c for c in bringup.evaluate_checks(**_ok_kwargs(disk_free_gib=None)) if c.name == "disk")
    assert disk_unk.status == "WARN" and "UNKNOWN" in disk_unk.detail
    voice = next(c for c in bringup.evaluate_checks(
        **_ok_kwargs(api_voice=False, voice_supply=dict(used_percent=40, left_percent=60))
    ) if c.name == "voice_supply")
    assert voice.status == "WARN" and "60" in voice.detail
    unk = next(c for c in bringup.evaluate_checks(
        **_ok_kwargs(api_voice=False, voice_supply=dict(error="no_session_log"))
    ) if c.name == "voice_supply")
    assert unk.status == "WARN" and "UNKNOWN" in unk.detail
    names = {c.name for c in bringup.evaluate_checks(**_ok_kwargs(api_voice=True, voice_supply=None))}
    assert "voice_supply" not in names


def test_run_preflight_unknown_voice_is_warn_not_blocker(tmp_path):
    lines = []
    rc = bringup.run_preflight(
        _ns(), root=tmp_path / "repo", print_fn=lines.append,
        porcelain_fn=lambda root: "",
        ignored_fn=lambda root, rel: True,
        scratch_fn=lambda root, env: tmp_path / "scratch",
        vts_fn=lambda: True,
        launcher_fn=lambda root: None,
        embed_fn=lambda: dict(ok=True, using="primary", base="http://127.0.0.1:1234/v1",
                              fallback="http://127.0.0.1:1234/v1"),
        watcher_fn=lambda: "x",
        alert_fn=lambda: [("SAWERIA_STREAM_KEY", 8)],
        dll_fn=lambda: (True, "installed hash matches booking"),
        memory_fn=lambda: None,
        disk_fn=lambda: None,
        voice_supply_fn=lambda: dict(error="unreadable"),
        env={"LOCALAPPDATA": str(tmp_path)},
    )
    assert rc == 0
    assert any(x.startswith("WARN memory:") and "UNKNOWN" in x for x in lines)
    assert any(x.startswith("WARN disk:") and "UNKNOWN" in x for x in lines)
    assert any(x.startswith("WARN voice_supply:") and "UNKNOWN" in x for x in lines)


def test_codex_weekly_parse_and_unknown(tmp_path):
    from scripts.livepath.codex_usage import read_codex_weekly
    assert read_codex_weekly(tmp_path / "missing")["error"]
    day = tmp_path / "2026" / "11" / "14"
    day.mkdir(parents=True)
    (day / "s.jsonl").write_text(
        '{"rate_limits":{"primary":{"used_percent":25,"resets_at":9999999999,"window_minutes":10080},"plan_type":"plus"}}\n',
        encoding="utf-8")
    row = read_codex_weekly(tmp_path)
    assert row["used_percent"] == 25 and row["left_percent"] == 75 and "error" not in row
    (day / "s.jsonl").write_text("not json\n", encoding="utf-8")
    assert read_codex_weekly(tmp_path)["error"] == "no_rate_limits"


# ------------------------------------------------------------------ machine preflight: gpu, disk_active, ears, save_date

def test_old_evaluate_checks_callers_get_exact_existing_lines():
    names = [c.name for c in bringup.evaluate_checks(**_ok_kwargs())]
    assert names == ["source", "scratch", "vts", "embed", "watcher", "alerts", "dll", "memory", "disk", "voice_supply"]


def test_gpu_preference_parser_and_verdicts():
    assert bringup.parse_gpu_preference("GpuPreference=2;") == 2
    assert bringup.parse_gpu_preference("AutoHDREnable=0;GpuPreference=1;") == 1
    assert bringup.parse_gpu_preference("GpuPreference=0") == 0
    assert bringup.parse_gpu_preference("GpuPreference=bad;") is None
    assert bringup.parse_gpu_preference("") is None
    assert bringup.parse_gpu_preference(None) is None

    game = r"C:\Games\Stardew Valley"
    both_high = {
        r"c:/games/stardew valley/StardewModdingAPI.exe": "GpuPreference=2;",
        r"C:\Games\Stardew Valley\Stardew Valley.exe": "SwapEffectUpgradeEnable=1;GpuPreference=2;",
    }
    ok_multi = bringup.gpu_check(bringup.parse_gpu_state(both_high, ["Adapter A", "Adapter B"], game))
    assert ok_multi.status == "OK" and "Adapter A" in ok_multi.detail and "Adapter B" in ok_multi.detail

    ok_single = bringup.gpu_check(bringup.parse_gpu_state({}, ["Adapter A"], game))
    assert ok_single.status == "OK" and "1 adapter" in ok_single.detail and "Adapter A" in ok_single.detail

    only_one = {r"C:\Games\Stardew Valley\StardewModdingAPI.exe": "GpuPreference=2;"}
    warn = bringup.gpu_check(bringup.parse_gpu_state(only_one, ["Adapter A", "Adapter B"], game))
    assert warn.status == "WARN"
    assert "StardewModdingAPI.exe=high performance" in warn.detail
    assert "Stardew Valley.exe=unset" in warn.detail
    assert "Adapter A" in warn.detail and "Adapter B" in warn.detail
    assert "High performance" in warn.detail and "Windows Settings" in warn.detail

    power_save = {
        r"C:\Games\Stardew Valley\StardewModdingAPI.exe": "GpuPreference=1;",
        r"C:\Games\Stardew Valley\Stardew Valley.exe": "GpuPreference=0;",
    }
    warn2 = bringup.gpu_check(bringup.parse_gpu_state(power_save, ["Adapter A", "Adapter B"], game))
    assert warn2.status == "WARN"
    assert "power saving" in warn2.detail and "system default" in warn2.detail

    for bad in (
        bringup.parse_gpu_state(None, ["Adapter A"], game),
        bringup.parse_gpu_state({}, [], game),
        bringup.parse_gpu_state({}, ["Adapter A"], ""),
        bringup.probe_gpu(game, prefs_fn=lambda: (_ for _ in ()).throw(OSError("reg denied")),
                          adapters_fn=lambda: ["Adapter A"]),
        bringup.probe_gpu(game, prefs_fn=lambda: {},
                          adapters_fn=lambda: (_ for _ in ()).throw(TimeoutError("slow"))),
        None,
    ):
        chk = bringup.gpu_check(bad)
        assert chk.status == "WARN" and "UNKNOWN" in chk.detail


def test_disk_active_parser_thresholds_and_no_console_window():
    import subprocess
    import sys

    idle_csv = (
        '\n"(PDH-CSV 4.0)","\\\\HOST\\LogicalDisk(C:)\\% Idle Time"\n'
        '"01/02/2030 03:00:00.000","35.0"\n'
        'Exiting, please wait...\n'
    )
    assert bringup.parse_disk_active_csv(idle_csv) == pytest.approx(65.0)

    disk_csv = (
        '"(PDH-CSV 4.0)","\\\\HOST\\LogicalDisk(C:)\\% Disk Time"\n'
        '"01/02/2030 03:00:00.000","18.5"\n'
        '"01/02/2030 03:00:01.000","21.5"\n'
    )
    assert bringup.parse_disk_active_csv(disk_csv) == pytest.approx(20.0)

    over_csv = (
        '"(PDH-CSV 4.0)","\\\\HOST\\LogicalDisk(C:)\\% Disk Time"\n'
        '"01/02/2030 03:00:00.000","145.0"\n'
    )
    assert bringup.parse_disk_active_csv(over_csv) == pytest.approx(100.0)
    assert bringup.parse_disk_active_csv('"Error: No valid counters."\n') is None
    assert bringup.parse_disk_active_csv("") is None
    assert bringup.parse_disk_active_csv(None) is None

    ok = bringup.disk_active_check(59.4)
    assert ok.status == "OK" and "59%" in ok.detail
    warn_edge = bringup.disk_active_check(60.0)
    assert warn_edge.status == "WARN" and "60%" in warn_edge.detail
    warn_busy = bringup.disk_active_check(100.0)
    assert warn_busy.status == "WARN" and "100%" in warn_busy.detail
    for bad in (None, True, "bad", -1.0, 120.0, float("nan"), float("inf")):
        unk = bringup.disk_active_check(bad)
        assert unk.status == "WARN" and "UNKNOWN" in unk.detail
    skipped = bringup.disk_active_check(dict(skipped=True))
    assert skipped.status == "OK" and "skipped" in skipped.detail

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        return SimpleNamespace(returncode=0, stdout=idle_csv)

    got = bringup.probe_disk_active(drive="C:", timeout=60.0, run_fn=fake_run)
    assert got == pytest.approx(65.0)
    assert seen["cmd"][0] == "typeperf"
    assert seen["kw"]["timeout"] == 10.0  # never longer, whatever the caller asks
    bringup.probe_disk_active(drive="C:", run_fn=fake_run)
    assert seen["kw"]["timeout"] == 10.0
    assert seen["kw"]["stdin"] is subprocess.DEVNULL
    assert seen["kw"]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if sys.platform == "win32":
        assert seen["kw"]["creationflags"] & subprocess.CREATE_NO_WINDOW

    assert bringup.probe_disk_active(run_fn=lambda *a, **k: SimpleNamespace(returncode=1, stdout="")) is None
    assert bringup.probe_disk_active(
        run_fn=lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("typeperf", 3.0))
    ) is None
    assert bringup.probe_disk_active(
        run_fn=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no typeperf"))
    ) is None


def test_ears_probe_folders_and_verdicts(tmp_path):
    import sys

    venv = tmp_path / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    ct2 = venv / "Lib" / "site-packages" / "ctranslate2"
    ct2.mkdir(parents=True)
    (ct2 / "cudnn64_9.dll").write_bytes(b"")

    had_fw, had_ct2 = "faster_whisper" in sys.modules, "ctranslate2" in sys.modules
    missing = bringup.probe_ears(venv, env={"PATH": ""})
    if not had_fw:
        assert "faster_whisper" not in sys.modules
    if not had_ct2:
        assert "ctranslate2" not in sys.modules
    chk_cpu = bringup.ears_check(missing)
    assert chk_cpu.status == "WARN"
    assert "speech recognition will run on the CPU" in chk_cpu.detail
    assert "cublas64_12.dll" in chk_cpu.detail

    cublas = venv / "Lib" / "site-packages" / "nvidia" / "cublas" / "bin"
    cublas.mkdir(parents=True)
    (cublas / "cublas64_12.dll").write_bytes(b"")
    (cublas / "cublasLt64_12.dll").write_bytes(b"")
    ok_state = bringup.probe_ears(venv, env={"PATH": ""})
    chk_ok = bringup.ears_check(ok_state)
    assert chk_ok.status == "OK" and "cublas64_12.dll" in chk_ok.detail

    # Also found via PATH when not under site-packages/nvidia.
    venv2 = tmp_path / "venv2"
    (venv2 / "Scripts").mkdir(parents=True)
    (venv2 / "Scripts" / "python.exe").write_bytes(b"")
    (venv2 / "Lib" / "site-packages" / "ctranslate2").mkdir(parents=True)
    extra = tmp_path / "cuda_bin"
    extra.mkdir()
    for dll in ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn_ops_infer64_8.dll"):
        (extra / dll).write_bytes(b"")
    chk_path = bringup.ears_check(bringup.probe_ears(venv2, env={"PATH": str(extra)}))
    assert chk_path.status == "OK"

    for bad in (
        bringup.probe_ears(tmp_path / "no_venv", env={"PATH": ""}),
        bringup.probe_ears(None, env={"PATH": ""}),
        bringup.probe_ears(venv, env={"PATH": ""}, timeout=-1.0),
        None,
    ):
        unk = bringup.ears_check(bad)
        assert unk.status == "WARN" and "UNKNOWN" in unk.detail


def test_save_date_probe_reads_only_continued_json(tmp_path):
    import json

    run = tmp_path / "run"
    run.mkdir()
    chk_new = bringup.save_date_check(bringup.probe_save_date(run))
    assert chk_new.status == "OK" and "continued.json" in chk_new.detail

    (run / "continued.json").write_text(
        json.dumps(dict(save="TestFarm_123", season="spring", day=13, year=1)),
        encoding="utf-8",
    )
    chk_date = bringup.save_date_check(bringup.probe_save_date(run))
    assert chk_date.status == "OK" and "Year 1 Spring 13" in chk_date.detail

    for bad_payload in (
        json.dumps(dict(save="TestFarm_123", season="?", day=0, year=0)),
        json.dumps(dict(save="TestFarm_123", season="spring", day=29, year=1)),
        json.dumps(dict(save="TestFarm_123", season="spring", day=True, year=1)),
        json.dumps(["not", "a", "dict"]),
        "{bad json",
    ):
        (run / "continued.json").write_text(bad_payload, encoding="utf-8")
        chk_unk = bringup.save_date_check(bringup.probe_save_date(run))
        assert chk_unk.status == "WARN" and "UNKNOWN" in chk_unk.detail

    assert bringup.save_date_check(bringup.probe_save_date(None)).status == "WARN"
    assert bringup.save_date_check(None).status == "WARN"


def test_run_preflight_new_probes_warn_on_failure_and_skip_disk_active_when_game_up(tmp_path):
    lines = []
    boom = lambda: (_ for _ in ()).throw(RuntimeError("probe crashed"))
    rc = bringup.run_preflight(
        _ns(), root=tmp_path / "repo", print_fn=lines.append,
        porcelain_fn=lambda root: "",
        ignored_fn=lambda root, rel: True,
        scratch_fn=lambda root, env: tmp_path / "scratch",
        vts_fn=lambda: True,
        launcher_fn=lambda root: None,
        embed_fn=lambda: dict(ok=True, using="primary", base="http://127.0.0.1:1234/v1",
                              fallback="http://127.0.0.1:1234/v1"),
        watcher_fn=lambda: "x",
        alert_fn=lambda: [("SAWERIA_STREAM_KEY", 8)],
        dll_fn=lambda: (True, "installed hash matches booking"),
        memory_fn=lambda: 40.0,
        disk_fn=lambda: 30.0,
        voice_supply_fn=lambda: dict(used_percent=20, left_percent=80),
        gpu_fn=boom,
        disk_active_fn=boom,
        ears_fn=boom,
        save_date_fn=boom,
        env={"LOCALAPPDATA": str(tmp_path)},
    )
    assert rc == 0
    for name in ("gpu", "disk_active", "ears", "save_date"):
        assert any(x.startswith("WARN %s:" % name) and "UNKNOWN" in x for x in lines), (name, lines)

    called = []
    skip_lines = []
    rc2 = bringup.run_preflight(
        _ns(), root=tmp_path / "repo", print_fn=skip_lines.append,
        porcelain_fn=lambda root: "",
        ignored_fn=lambda root, rel: True,
        scratch_fn=lambda root, env: tmp_path / "scratch",
        vts_fn=lambda: True,
        launcher_fn=lambda root: None,
        embed_fn=lambda: dict(ok=True, using="primary", base="http://127.0.0.1:1234/v1",
                              fallback="http://127.0.0.1:1234/v1"),
        watcher_fn=lambda: "x",
        alert_fn=lambda: [("SAWERIA_STREAM_KEY", 8)],
        dll_fn=lambda: (True, "installed hash matches booking"),
        memory_fn=lambda: 40.0,
        disk_fn=lambda: 30.0,
        voice_supply_fn=lambda: dict(used_percent=20, left_percent=80),
        disk_active_fn=lambda: called.append(1) or 90.0,
        skip_disk_active=True,
        env={"LOCALAPPDATA": str(tmp_path)},
    )
    assert rc2 == 0 and called == []
    assert any(x.startswith("OK disk_active:") and "skipped" in x for x in skip_lines)


def test_mabar_preflight_wiring_and_rejoin_skips_disk_active_with_games_up(monkeypatch, tmp_path):
    import json

    run = tmp_path / "run"
    run.mkdir()
    (run / "trial-config.json").write_text(json.dumps(dict(game_dir=str(tmp_path / "custom-game"))), encoding="utf-8")
    assert mabar._preflight_game_dir(_ns(run=run)) == tmp_path / "custom-game"
    assert mabar._preflight_game_dir(_ns(run=tmp_path / "empty")) == mabar.GAME

    captured = {}

    def fake_run_preflight(a, **kw):
        captured["a_skip_disk"] = getattr(a, "skip_disk_active", False)
        captured.update(kw)
        return 1

    monkeypatch.setattr(bringup, "run_preflight", fake_run_preflight)
    assert mabar.preflight(_ns(run=run)) == 1
    for key in ("gpu_fn", "disk_active_fn", "ears_fn", "save_date_fn"):
        assert callable(captured[key]), key

    captured.clear()
    a1 = argparse.Namespace(run=run, spec=tmp_path / "s.json", vts_port=8002, no_vts=False, no_alerts=False, name="Arti")
    assert mabar.rejoin(a1, count_fn=lambda: "1") == 1
    assert captured["a_skip_disk"] is True and captured["skip_memory"] is False
    assert not hasattr(a1, "skip_disk_active")

    captured.clear()
    a2 = argparse.Namespace(run=run, spec=tmp_path / "s.json", vts_port=8002, no_vts=False, no_alerts=False, name="Arti")
    assert mabar.rejoin(a2, count_fn=lambda: "2") == 1
    assert captured["a_skip_disk"] is True and captured["skip_memory"] is True
    assert not hasattr(a2, "skip_disk_active")

    captured.clear()
    a0 = argparse.Namespace(run=run, spec=tmp_path / "s.json", vts_port=8002, no_vts=False, no_alerts=False, name="Arti")
    assert mabar.rejoin(a0, count_fn=lambda: "0") == 1
    assert captured["a_skip_disk"] is False and captured["skip_memory"] is False
    assert not hasattr(a0, "skip_disk_active")
