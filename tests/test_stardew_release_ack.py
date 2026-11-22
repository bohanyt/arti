"""Release handshake of the renewable lease: the ack wait is bounded (not one second) and an
unconfirmed release is journaled by branch and unmet-condition names (live 2026-10-06 9z)."""
import json
import threading
import time

import pytest

import arti_stardew_command as command
import arti_stardew_runtime as runtime_module
from tests.test_stardew_runtime_move import rig  # noqa: F401  (fixture)


def _pin(rig_):
    rt, _motor, _obs, raw, _calls, publish, folder = rig_
    raw.update(owner_id="owner-1", generation=1, control_seq=1, release_acked=False)
    publish()
    session = command.baca_sesi(folder)
    return rt, raw, publish, folder, runtime_module._WaterLeasePin(
        folder=folder, session=session, owner="owner-1", generation=1, confirmed_seq=1, attempted_seq=1)


def _write_control(folder, kind):
    # The move rig already wraps command.tulis_kontrol with a synchronous ack; these tests
    # need the raw write (a control.json that only the "mod" removes), so they write it here.
    assert kind == "release"
    (folder / command.NAMA_CONTROL).write_text(json.dumps({"kind": kind}), encoding="utf-8")


def _journal(rt):
    path = rt._journal_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_release_ack_arriving_after_one_second_still_confirms_cleanup(rig, monkeypatch):  # noqa: F811
    rt, raw, publish, folder, pin = _pin(rig)

    def late_ack(kind, **kw):
        _write_control(folder, kind)

        def ack():
            time.sleep(1.3)
            raw.update(owner_id=None, generation=None, control_seq=None, release_acked=True)
            publish()
            (folder / command.NAMA_CONTROL).unlink()
        threading.Thread(target=ack, daemon=True).start()

    monkeypatch.setattr(command, "tulis_kontrol", late_ack)
    started = time.monotonic()
    assert rt._cleanup_water(pin) is True
    assert time.monotonic() - started >= 1.2
    assert not [e for e in _journal(rt) if e.get("ev") == "release_cleanup_unconfirmed"]


def test_release_never_acked_is_unconfirmed_and_journaled_by_branch(rig, monkeypatch):  # noqa: F811
    rt, raw, publish, folder, pin = _pin(rig)
    monkeypatch.setattr(command, "tulis_kontrol", lambda kind, **kw: _write_control(folder, kind))
    monkeypatch.setattr(runtime_module, "_RELEASE_ACK_SECONDS", 0.15)
    monkeypatch.setattr(runtime_module, "_RELEASE_UNCONSUMED_GRACE_SECONDS", 0.1)  # unread control: bounded grace
    assert rt._cleanup_water(pin) is False
    assert [e["ev"] for e in _journal(rt)].count("release_ack_grace") == 1
    events = [e for e in _journal(rt) if e.get("ev") == "release_cleanup_unconfirmed"]
    assert len(events) == 1
    assert events[0]["branch"] == "ack_deadline"
    assert events[0]["pending"] == ["owner_retired_or_stopped", "release_acked", "control_consumed"]
    assert set(events[0]) == {"ev", "branch", "waited_ms", "pending", "domain", "cycle", "day_trial"}  # + WP4 tags


def test_foreign_owner_during_release_is_unconfirmed(rig, monkeypatch):  # noqa: F811
    rt, raw, publish, folder, pin = _pin(rig)

    def stolen(kind, **kw):
        _write_control(folder, kind)
        raw.update(owner_id="owner-2", generation=2, control_seq=1)
        publish()

    monkeypatch.setattr(command, "tulis_kontrol", stolen)
    assert rt._cleanup_water(pin) is False
    events = [e for e in _journal(rt) if e.get("ev") == "release_cleanup_unconfirmed"]
    assert [e["branch"] for e in events] == ["foreign_owner"]
