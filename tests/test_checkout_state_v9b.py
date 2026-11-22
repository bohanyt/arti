"""V9b (2026-11-18): a bridge run from a release checkout leaves no private state in it.

pkg110 stopped NOT READY on its privacy guard: the 11-17 livestream test had run the bridge from the package
checkout and left her viewer file, mood, vault session note, telemetry, the unknown-Streamlabs sample and her VTS
token there. Synthetic files only; temp folders only.
"""
import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts.livepath import bringup, mabar, package_guard


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def places(tmp_path, monkeypatch):
    root, run, live, store = (tmp_path / n for n in ("checkout", "run", "live", "store"))
    for p in (root, run, live, store):
        p.mkdir()
    write(live / "ARTI_VIEWERS.md", "viewers v1")
    write(live / "ARTI_MOOD_STATE.json", "{}")
    monkeypatch.setattr(mabar, "log", lambda run, event, **kw: None)
    return SimpleNamespace(root=root, run=run, live=live, store=store)


def test_private_state_is_found_by_name_only(places):
    write(places.root / "vault" / "sessions" / "x.md", "note")
    write(places.root / "vts_token_8002.txt", "secret")
    write(places.root / "arti_bridge.py", "code")
    assert bringup.private_state_in(places.root) == ["vault", "vts_token_8002.txt"]
    assert set(bringup.private_files(places.root)) == {"vault/sessions/x.md", "vts_token_8002.txt"}


def test_restore_moves_what_the_run_wrote_and_keeps_what_was_there(places):
    write(places.root / "arti-state" / "mind" / "now.json", "older run")        # there before this run
    mabar.snapshot_checkout_state(places.run, root=places.root)
    mabar.copy_live_self_files(places.run, dest=places.root, src=places.live)  # prepare's copies
    write(places.root / "ARTI_VIEWERS.md", "viewers v2: a note from the night")  # her write during the run
    write(places.root / "vault" / "sessions" / "mabar-x.md", "session note")
    write(places.root / "vault" / "sessions" / "index.md", "| row |")
    write(places.root / "data" / "telemetry" / "unknown.jsonl", "{}")
    write(places.root / "vts_token_8002.txt", "token")
    out = mabar.move_checkout_state(places.run, root=places.root, live=places.live, token_store=places.store)
    assert sorted(out["moved"]) == ["ARTI_MOOD_STATE.json", "ARTI_VIEWERS.md", "data/telemetry/unknown.jsonl",
                                    "vault/sessions/index.md", "vault/sessions/mabar-x.md"]
    assert out["tokens"] == ["vts_token_8002.txt"] and (places.store / "vts_token_8002.txt").read_text() == "token"
    assert sorted(out["to_merge"]) == ["ARTI_VIEWERS.md", "vault/sessions/mabar-x.md"]  # mood equals live: no merge
    assert bringup.private_state_in(places.root) == ["arti-state/mind"]                 # the older run's stays
    sums = json.loads((places.run / "checkout-state" / "SHA256SUMS.json").read_text(encoding="utf-8"))
    for rel, digest in sums.items():
        assert hashlib.sha256((places.run / "checkout-state" / rel).read_bytes()).hexdigest() == digest
    assert not (places.root / "vault").exists() and not (places.root / "data" / "telemetry").exists()


def test_without_a_manifest_everything_private_moves(places):
    write(places.root / "vault" / "sessions" / "a.md", "note")
    out = mabar.move_checkout_state(places.run, root=places.root, live=places.live, token_store=places.store)
    assert out["moved"] == ["vault/sessions/a.md"] and bringup.private_state_in(places.root) == []


def test_link_runtime_puts_the_token_back(places, tmp_path):
    write(places.store / "vts_token_8002.txt", "token")
    src = tmp_path / "src"
    src.mkdir()
    mabar.link_runtime(SimpleNamespace(), root=places.root, source=src, manifest_dir=places.store)
    assert (places.root / "vts_token_8002.txt").read_text() == "token"


def test_package_guard_refuses_and_names_paths_only(places, capsys):
    write(places.root / "ARTI_VIEWERS.md", "private words")
    assert package_guard.main([str(places.root)]) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "ARTI_VIEWERS.md" in out and "private words" not in out
    (places.root / "ARTI_VIEWERS.md").unlink()
    assert package_guard.main([str(places.root)]) == 0


def test_preflight_warns_on_private_state_and_old_callers_get_no_line():
    base = dict(dirty_names=[], scratch_outside=True, scratch_path="x", tmp_ignored=True, vts_ok=True,
                vts_port=8002, no_vts=False, launcher=None, embed=dict(ok=True, using="primary", fallback="f"),
                watcher_present=True, alert_keys=[], alerts_enabled=False, dll_ok=True, dll_detail="ok")
    [line] = [c for c in bringup.evaluate_checks(**base, private_state=["vault"]) if c.name == "private_state"]
    assert line.status == "WARN" and "vault" in line.detail and "package build refuses" in line.detail
    assert not [c for c in bringup.evaluate_checks(**base) if c.name == "private_state"]
