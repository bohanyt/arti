"""Livestream test 2026-11-17 (V9): bring-up that names what is missing and what the owner must do."""
import json
import os
from types import SimpleNamespace

import pytest

from scripts.livepath import bringup, mabar


def _checks(**kw):
    base = dict(dirty_names=[], scratch_outside=True, scratch_path="x", tmp_ignored=True, vts_ok=True,
                vts_port=8002, no_vts=False, launcher=None, embed=dict(ok=True, using="primary", fallback="f"),
                watcher_present=True, alert_keys=[], alerts_enabled=False, dll_ok=True, dll_detail="ok")
    base.update(kw)
    return bringup.evaluate_checks(**base)


def test_missing_runtime_parts_block_with_the_step_to_run(tmp_path):
    (tmp_path / "venv" / "Scripts").mkdir(parents=True)
    (tmp_path / "venv" / "Scripts" / "python.exe").write_text("")
    gaps = bringup.runtime_missing(tmp_path)
    assert [n for n, _ in gaps] == ["venv312", "ARTI_SOUL.md"]
    [line] = [c for c in _checks(runtime_gaps=gaps) if c.name == "runtime"]
    assert line.status == "BLOCKER"
    assert "venv312 (Supertone, her voice)" in line.detail and "ARTI_SOUL.md (her soul)" in line.detail
    assert "mabar link-runtime" in line.detail


def test_runtime_present_is_ok_and_old_callers_get_no_line():
    assert [c.status for c in _checks(runtime_gaps=[]) if c.name == "runtime"] == ["OK"]
    assert not [c for c in _checks() if c.name == "runtime"]


def test_vts_off_names_the_owner_action_and_window1():
    [line] = [c for c in _checks(vts_ok=False, vts_window1_ok=False) if c.name == "vts"]
    assert line.status == "BLOCKER"
    assert "Window 1 API (8001) is off too" in line.detail
    assert "OWNER: in VTube Studio Window 2 open Settings" in line.detail and "port 8002" in line.detail


def test_run_preflight_passes_the_runtime_and_window1_probes(tmp_path):
    out = []
    a = SimpleNamespace(no_vts=False, vts_port=8002, no_alerts=True, api_voice=None, skip_memory=True)
    rc = bringup.run_preflight(
        a, root=tmp_path, print_fn=out.append, porcelain_fn=lambda r: "", ignored_fn=lambda r, p: True,
        scratch_fn=lambda r, e: tmp_path.parent / "scratch", vts_fn=lambda: False, launcher_fn=lambda r: None,
        embed_fn=lambda: dict(ok=True, using="primary", fallback="f"), watcher_fn=lambda: True,
        alert_fn=lambda: (), dll_fn=lambda: (True, "ok"), disk_fn=lambda: 50.0, voice_supply_fn=lambda: None,
        runtime_fn=lambda: [("venv312", "Supertone, her voice")], vts_window1_fn=lambda: True)
    assert rc == 1
    assert any("BLOCKER runtime" in l or l.startswith("BLOCKER") and "runtime" in l for l in out)
    assert any("Window 1 API (8001) answers" in l for l in out)


def test_link_and_unlink_runtime_remove_only_what_they_made(tmp_path):
    src, root, man = tmp_path / "live", tmp_path / "pkg", tmp_path / "links"
    for name in ("venv", "venv312"):
        (src / name / "Scripts").mkdir(parents=True)
        (src / name / "Scripts" / "python.exe").write_text("py")
    (src / "ARTI_SOUL.md").write_text("soul")
    root.mkdir()
    (root / "venv").mkdir()  # already present: never touched
    mabar.link_runtime(SimpleNamespace(), root=root, source=src, manifest_dir=man)
    assert (root / "venv312" / "Scripts" / "python.exe").read_text() == "py"
    assert os.path.samefile(root / "ARTI_SOUL.md", src / "ARTI_SOUL.md")
    made = json.loads((man / "pkg.json").read_text(encoding="utf-8"))["made"]
    assert [m["name"] for m in made] == ["venv312", "ARTI_SOUL.md"]
    mabar.unlink_runtime(SimpleNamespace(), root=root, manifest_dir=man)
    assert not (root / "venv312").exists() and not (root / "ARTI_SOUL.md").exists()
    assert (root / "venv").is_dir()                                  # not ours
    assert (src / "venv312" / "Scripts" / "python.exe").read_text() == "py"  # target intact
    assert (src / "ARTI_SOUL.md").read_text() == "soul"
    assert not (man / "pkg.json").exists()


def test_unlink_without_a_manifest_does_nothing(tmp_path):
    (tmp_path / "ARTI_SOUL.md").write_text("x")
    assert mabar.unlink_runtime(SimpleNamespace(), root=tmp_path, manifest_dir=tmp_path / "none") == 0
    assert (tmp_path / "ARTI_SOUL.md").exists()


def test_memory_report_tags_ours_and_the_owners():
    lines = mabar.memory_report_lines(
        dict(committed_gb=36.3, limit_now_gb=36.7),
        [dict(name="obs64.exe", gb=1.2, owner="owner"), dict(name="python.exe", gb=0.9, owner="ours"),
         dict(name="StardewModdingAPI.exe", gb=2.1, owner="ours", note="her farmhand")])
    assert lines[0] == "commit 36.3/36.7 GB (99%)"
    assert "her farmhand" in lines[1] and "ours" in lines[1]
    assert lines[-1].startswith("ours in total 3.0 GB")


def test_steps_exist():
    for step in ("link-runtime", "unlink-runtime", "memory-report"):
        assert step in mabar.STEPS
