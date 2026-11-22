"""Adopt run-local objects/ zips into the shared store (disk-dedupe)."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import zipfile

import pytest

from scripts.free_play import storage as S


def _old_run(tmp_path, monkeypatch, name, payload, filename="file"):
    monkeypatch.setenv("ARTI_SHARED_OBJECTS", "0")
    src = tmp_path / f"src-{name}"
    src.mkdir(parents=True, exist_ok=True)
    (src / filename).write_bytes(payload)
    run = tmp_path / "free-play-runs" / name
    S.capture(src, run / "packed", run)
    assert list((run / "objects").glob("*.zip"))
    assert not list(S.shared_store_for(run).glob("*.zip"))
    return run


def _zip_hashes(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.glob("*.zip"))}


def _corrupt(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("data", b"valid zip with wrong SHA256")


def test_dedupe_moves_first_copy_into_shared_store(tmp_path, monkeypatch):
    payload = b"unique-bytes\n" * 200
    run = _old_run(tmp_path, monkeypatch, "r1", payload)
    local = next((run / "objects").glob("*.zip"))
    digest = local.stem
    result = S.dedupe_run(run)
    assert result["moved"] == 1 and result["removed"] == 0 and result["kept"] == 0
    assert not local.exists()
    shared = S.shared_store_for(run) / (digest + ".zip")
    assert shared.is_file()
    S.verify_object_zip(shared, digest, len(payload))
    row = S.read_manifest(run)["objects"][digest]
    assert row["location"] == "shared" and row["bytes"] == len(payload)
    assert S.read_bytes(run / "packed/file") == payload
    facts = (run / S.FACTS).read_text(encoding="utf-8")
    assert "objects_deduped" in facts


def test_dedupe_removes_duplicate_after_shared_has_hash(tmp_path, monkeypatch):
    payload = b"shared-across-runs\n" * 100
    r1 = _old_run(tmp_path, monkeypatch, "r1", payload)
    r2 = _old_run(tmp_path, monkeypatch, "r2", payload)
    first = S.dedupe_run(r1)
    second = S.dedupe_run(r2)
    assert first["moved"] == 1 and second["removed"] == 1 and second["moved"] == 0
    store = tmp_path / "free-play-objects"
    assert len(list(store.glob("*.zip"))) == 1
    assert not list((r1 / "objects").glob("*.zip"))
    assert not list((r2 / "objects").glob("*.zip"))
    assert S.read_bytes(r1 / "packed/file") == S.read_bytes(r2 / "packed/file") == payload
    assert S.read_manifest(r2)["objects"][next(iter(S.read_manifest(r2)["objects"]))]["location"] == "shared"


def test_dedupe_keeps_corrupt_run_copy(tmp_path, monkeypatch):
    run = _old_run(tmp_path, monkeypatch, "r1", b"good-payload")
    local = next((run / "objects").glob("*.zip"))
    _corrupt(local)
    result = S.dedupe_run(run)
    assert result["kept"] == 1 and result["moved"] == 0
    assert local.is_file()
    assert not list(S.shared_store_for(run).glob("*.zip"))
    assert result["kept_reasons"][0]["file"] == local.name


def test_dedupe_promotes_corrupt_shared_from_good_run_copy(tmp_path, monkeypatch):
    payload = b"promote-me\n" * 50
    run = _old_run(tmp_path, monkeypatch, "r1", payload)
    local = next((run / "objects").glob("*.zip"))
    shared = S.shared_store_for(run)
    shared.mkdir(parents=True)
    shutil.copyfile(local, shared / local.name)
    _corrupt(shared / local.name)
    result = S.dedupe_run(run)
    assert result["promoted"] == 1 and result["kept"] == 0
    assert not local.exists()
    S.verify_object_zip(shared / local.name, local.stem, len(payload))
    assert S.read_bytes(run / "packed/file") == payload


def test_dedupe_keeps_run_copy_when_corrupt_shared_replace_fails(tmp_path, monkeypatch):
    payload = b"keep-if-replace-fails"
    run = _old_run(tmp_path, monkeypatch, "r1", payload)
    local = next((run / "objects").glob("*.zip"))
    shared = S.shared_store_for(run)
    shared.mkdir(parents=True)
    target = shared / local.name
    shutil.copyfile(local, target)
    _corrupt(target)
    real = os.replace

    def boom(src, dst):
        if Path(dst).resolve() == target.resolve():
            raise OSError("replace failed")
        return real(src, dst)

    monkeypatch.setattr(S.os, "replace", boom)
    result = S.dedupe_run(run)
    assert result["kept"] == 1
    assert local.is_file()
    assert target.is_file()  # corrupt shared left; never deleted a unique hash


def test_dedupe_interrupt_after_shared_before_unlink_then_retry(tmp_path, monkeypatch):
    payload = b"interrupt-unlink"
    run = _old_run(tmp_path, monkeypatch, "r1", payload)
    local = next((run / "objects").glob("*.zip"))
    real = Path.unlink

    def fail(self, *a, **kw):
        if self.name == local.name and self.parent.resolve() == (run / "objects").resolve():
            raise OSError("interrupted")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", fail)
    first = S.dedupe_run(run)
    assert first["kept"] == 1
    assert local.is_file()
    assert (S.shared_store_for(run) / local.name).is_file()
    monkeypatch.setattr(Path, "unlink", real)
    second = S.dedupe_run(run)
    assert second["removed"] == 1 and second["kept"] == 0
    assert not local.exists()
    assert S.read_bytes(run / "packed/file") == payload


def test_dedupe_interrupt_before_manifest_then_retry(tmp_path, monkeypatch):
    payload = b"interrupt-manifest"
    run = _old_run(tmp_path, monkeypatch, "r1", payload)
    local = next((run / "objects").glob("*.zip"))
    real = S.record_manifest

    def boom(*_a, **_k):
        raise OSError("manifest interrupted")

    monkeypatch.setattr(S, "record_manifest", boom)
    first = S.dedupe_run(run)
    assert first["kept"] == 1 and local.is_file()
    assert (S.shared_store_for(run) / local.name).is_file()
    monkeypatch.setattr(S, "record_manifest", real)
    second = S.dedupe_run(run)
    assert second["removed"] == 1
    assert not local.exists()
    assert S.read_manifest(run)["objects"][local.stem]["location"] == "shared"


def test_dedupe_unlinks_readonly_run_copy(tmp_path, monkeypatch):
    payload = b"readonly-zip-bytes"
    run = _old_run(tmp_path, monkeypatch, "r1", payload)
    local = next((run / "objects").glob("*.zip"))
    local.chmod(stat.S_IREAD)
    result = S.dedupe_run(run)
    assert result["moved"] == 1 and result["kept"] == 0
    assert not local.exists()
    assert S.read_bytes(run / "packed/file") == payload


def test_dedupe_idempotent_second_pass_is_noop(tmp_path, monkeypatch):
    run = _old_run(tmp_path, monkeypatch, "r1", b"once-only")
    first = S.dedupe_run(run)
    second = S.dedupe_run(run)
    assert first["moved"] == 1
    assert second["moved"] == second["removed"] == second["kept"] == 0
    assert len(list(S.shared_store_for(run).glob("*.zip"))) == 1


def test_dedupe_readers_resolve_and_export_round_trip(tmp_path, monkeypatch):
    payload_a, payload_b = b"alpha-bytes", b"beta-bytes-longer"
    monkeypatch.setenv("ARTI_SHARED_OBJECTS", "0")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a").write_bytes(payload_a)
    (src / "b").write_bytes(payload_b)
    run = tmp_path / "free-play-runs" / "r1"
    S.capture(src, run / "packed", run)
    before_dir = tmp_path / "before-objects"
    shutil.copytree(run / "objects", before_dir)
    before = _zip_hashes(before_dir)
    assert S.dedupe_run(run)["moved"] == 2
    assert S.read_bytes(run / "packed/a") == payload_a
    assert S.read_bytes(run / "packed/b") == payload_b
    dest = tmp_path / "export-dest"
    S.export_objects(run, dest)
    after = _zip_hashes(dest / "objects")
    assert after == before
    verify = S.verify_run(run)
    assert verify["ok"] and verify["leftover_local"] == 0
    assert set(S.read_manifest(run)["objects"]) == {name[:-4] for name in before}


def test_dedupe_dry_run_second_run_counts_duplicate_as_removed(tmp_path, monkeypatch):
    payload = b"same-across-dry-runs"
    r1 = _old_run(tmp_path, monkeypatch, "r1", payload)
    r2 = _old_run(tmp_path, monkeypatch, "r2", payload)
    adopted = set()
    first = S.dedupe_run(r1, dry_run=True, adopted=adopted)
    second = S.dedupe_run(r2, dry_run=True, adopted=adopted)
    assert first["moved"] == 1 and second["removed"] == 1 and second["moved"] == 0
    assert not list(S.shared_store_for(r1).glob("*.zip"))
    assert next((r1 / "objects").glob("*.zip")).is_file()
    assert next((r2 / "objects").glob("*.zip")).is_file()


def test_dedupe_dry_run_counts_without_writes(tmp_path, monkeypatch):
    run = _old_run(tmp_path, monkeypatch, "r1", b"dry-only")
    local = next((run / "objects").glob("*.zip"))
    facts_before = (run / S.FACTS).read_text(encoding="utf-8") if (run / S.FACTS).is_file() else ""
    location = S.read_manifest(run)["objects"][local.stem]["location"]
    result = S.dedupe_run(run, dry_run=True)
    assert result["moved"] == 1
    assert local.is_file()
    assert not list(S.shared_store_for(run).glob("*.zip"))
    facts_after = (run / S.FACTS).read_text(encoding="utf-8") if (run / S.FACTS).is_file() else ""
    assert facts_after == facts_before
    assert "objects_deduped" not in facts_after
    assert S.read_manifest(run)["objects"][local.stem]["location"] == location


def test_runs_for_dedupe_exclude_from_uses_timestamp(tmp_path):
    root = tmp_path / "free-play-runs"
    names = [
        "free-play-standard-20261110T022905Z",
        "free-play-standard-20261113T011340Z",
        "free-play-standard-20261113T023836Z",
        "free-play-standard-20261115T034957Z",
        "free-play-witness-geode-20261107T132455Z",
        "r1-patchfix-loader-20261027T073114Z",
        "livepath-rehearsal1-20261109",
    ]
    for name in names:
        (root / name).mkdir(parents=True)
    got = [p.name for p in S.runs_for_dedupe(root, exclude_from="free-play-standard-20261113T023836Z")]
    assert "free-play-standard-20261113T023836Z" not in got
    assert "free-play-standard-20261115T034957Z" not in got
    assert "free-play-witness-geode-20261107T132455Z" in got
    assert "free-play-standard-20261110T022905Z" in got
    assert "r1-patchfix-loader-20261027T073114Z" in got
    assert "livepath-rehearsal1-20261109" in got
    assert got[0] == "r1-patchfix-loader-20261027T073114Z"


def test_dedupe_cli_dry_run_and_verify(tmp_path, monkeypatch, capsys):
    payload = b"cli-payload"
    run = _old_run(tmp_path, monkeypatch, "free-play-standard-20261101T000000Z", payload)
    root = tmp_path / "free-play-runs"
    (root / "free-play-standard-20261113T023836Z").mkdir(exist_ok=True)
    monkeypatch.setattr(sys, "argv", [
        "storage.py", "dedupe", "--root", str(root),
        "--exclude-from", "free-play-standard-20261113T023836Z", "--dry-run",
    ])
    S.main()
    dry = capsys.readouterr().out.strip().splitlines()
    assert json.loads(dry[-1])["moved"] == 1
    assert next((run / "objects").glob("*.zip")).is_file()
    monkeypatch.setattr(sys, "argv", ["storage.py", "dedupe", str(run)])
    S.main()
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["storage.py", "dedupe", "--verify", str(run)])
    S.main()
    verify = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert verify["ok"] is True
    assert verify["leftover_local"] == 0
