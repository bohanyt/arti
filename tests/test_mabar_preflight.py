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
