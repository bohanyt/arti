"""CLI -> batch -> v1 -> StateStore, with only external game IO simulated.

No Stardew/SMAPI execution. Reuse existing receiver/inbox fixtures; do not
replace the batch or v1 orchestration with synthetic driver results.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_stardew_farm_cli as H
from arti_stardew_farm_batch import jalankan_farm_batch

cli = H.cli
TARGETS = ((10, 10), (9, 11), (11, 11), (10, 12))


@pytest.fixture(autouse=True)
def reset_receivers():
    H.ReceiverPalsu.instances.clear()
    yield
    H.ReceiverPalsu.instances.clear()


def facts(*targets):
    return {
        f"tf:Farm:{x},{y}:HoeDirt": {
            "kind": "terrain", "terrain_type": "HoeDirt",
            "tile": {"x": x, "y": y}, "passable": True,
            "crop": {"qualified_item_id": "(O)24", "phase": 1,
                     "dead": False, "watered": False,
                     "ready_for_harvest": False},
        }
        for x, y in targets
    }


def inspect_envelope(*targets):
    return H.envelope_inspect(
        session_id=H.SESSION_ID, player_id=H.P2, location_id="Farm",
        center_x=H.FARMER[0], center_y=H.FARMER[1], radius=1,
        facts=facts(*targets),
    )


class World:
    """External facts/commands only; production orchestration stays real."""

    def __init__(self, folder, targets=TARGETS[:2]):
        self.folder = folder
        H.tulis_sesi(folder)
        self.store = H.StardewStateStore()
        H.isi_store(self.store)
        self.seq = 2
        self.dry = list(targets)
        self.inspect_calls = []
        self.water_calls = []
        self.batch_calls = []
        self.after_inspect = None
        self.after_water = None
        self.interrupt_water = None
        self.fail_water = None
        self.keep_dry = False
        self.fresh = True
        self.now = 0.0

    def state(self, **payload):
        self.seq += 1
        self.store.catat(H._state(seq=self.seq, **payload))

    def inspect(self, x, y, radius, **kw):
        assert (x, y) == H.FARMER and radius == 1
        assert kw["sesi"].session_id == H.SESSION_ID
        self.inspect_calls.append((x, y, radius))
        raw = inspect_envelope(*self.dry)
        if self.fresh:
            self.state()
        if self.after_inspect:
            self.after_inspect()
        return H.HasilPerintah(
            status=H.SELESAI, sukses=True,
            request_id=f"inspect-{len(self.inspect_calls)}",
            outcome="Inspected", mentah=raw,
        )

    def water(self, x, y, **kw):
        assert kw["sesi"].session_id == H.SESSION_ID
        self.water_calls.append((x, y))
        n = len(self.water_calls)
        if self.interrupt_water == n:
            raise KeyboardInterrupt()
        success = self.fail_water != n
        if success and not self.keep_dry:
            self.dry.remove((x, y))
        result = H.HasilPerintah(
            status=H.SELESAI if success else H.TIDAK_PASTI,
            sukses=success, request_id=f"water-{n}",
            outcome="ObservedWatered" if success else "Unknown",
            alasan=None if success else "simulated missing terminal result",
        )
        if self.after_water:
            self.after_water()
        return result

    def batch(self, request, **kw):
        self.batch_calls.append(request)
        return jalankan_farm_batch(request, **kw)

    def run(self, *options, execute=True, batch=True, **kw):
        argv = ["--folder", str(self.folder), "--wait", "0", "--max-age", "5",
                "--state-timeout", "0", "--json", str(self.folder / "out.json")]
        if execute:
            argv.append("--execute")
        if batch:
            argv.append("--batch")
        argv.extend(options)
        dependencies = dict(
            receiver_cls=H.ReceiverPalsu, store=self.store,
            minta_inspect_tiles=self.inspect, minta_water_once=self.water,
            jalankan_batch=self.batch, jam=lambda: self.now,
        )
        dependencies.update(kw)
        return H.jalankan_cli(argv, **dependencies)

    def report(self):
        return json.loads((self.folder / "out.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("batch", [False, True])
def test_preflight_never_calls_drivers(tmp_path, batch):
    w = World(tmp_path)
    rc, _, _ = w.run(execute=False, batch=batch)
    data = w.report()
    assert rc == cli.EXIT_OK
    assert data["driver_mode"] == ("batch" if batch else "single")
    assert not data["driver_invoked"] and not data["command_attempted"]
    assert w.batch_calls == w.inspect_calls == w.water_calls == []
    assert data["receiver"]["stopped"]


def test_help_batch_has_no_receiver_or_command(tmp_path):
    w = World(tmp_path)
    with pytest.raises(SystemExit) as exc:
        w.run("--help")
    assert exc.value.code == 0
    assert not H.ReceiverPalsu.instances
    assert w.batch_calls == w.inspect_calls == w.water_calls == []


@pytest.mark.parametrize("options", [
    ["--batch", "--max-actions", "0"],
    ["--batch", "--max-actions", "5"],
    ["--max-actions", "2"],
    ["--batch-deadline", "1"],
    ["--batch", "--batch-deadline", "0"],
    ["--batch", "--batch-deadline", "31"],
    ["--batch", "--batch-deadline=nan"],
    ["--batch", "--batch-deadline=inf"],
    ["--batch", "--batch-deadline=-inf"],
    ["--batch", "--batch-deadline=-1"],
])
def test_bad_batch_configuration_before_session_or_receiver(options):
    def forbidden(*a, **kw):
        raise AssertionError("invalid configuration reached IO")
    rc, out, _ = H.jalankan_cli(
        ["--execute", *options], receiver_cls=H.ReceiverPalsu,
        baca_sesi=forbidden, jalankan_batch=forbidden, jalankan_v1=forbidden,
    )
    assert rc == cli.EXIT_REFUSED and "CONFIG_INVALID" in out
    assert not H.ReceiverPalsu.instances


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_real_composition_caps_distinct_targets_and_serializes(tmp_path, count):
    w = World(tmp_path, TARGETS)
    rc, out, _ = w.run("--max-actions", str(count), "--batch-deadline", "30")
    data = w.report()
    assert rc == cli.EXIT_OK, out
    assert len(w.batch_calls) == 1
    assert w.batch_calls[0].max_actions == count
    assert w.batch_calls[0].deadline_seconds == 30.0
    assert w.water_calls == list(TARGETS[:count])
    assert len(w.inspect_calls) == count
    assert data["driver"]["status"] == "LIMIT_REACHED"
    assert data["driver"]["completed_targets"] == [list(t) for t in TARGETS[:count]]
    assert data["driver"]["pin_tile"] == list(H.FARMER)
    assert data["driver"]["pin_session_id"] == H.SESSION_ID
    assert data["state_before"]["tile"] == data["state_after"]["tile"]
    assert data["state_after"]["revision"] > data["state_before"]["revision"]
    assert len(data["command_calls"]) == 2 * count <= 8
    assert len({c["request_id"] for c in data["command_calls"]}) == 2 * count
    assert data["water_confirmed"] and data["ok"]
    assert data["batch_progress"]["result_returned"]
    assert data["receiver"]["stopped"] and not data["verified_live"]
    assert len(H.ReceiverPalsu.instances) == 1
    json.dumps(data, allow_nan=False)


def test_execute_without_batch_remains_single_shot(tmp_path):
    w = World(tmp_path)
    rc, _, _ = w.run(batch=False)
    assert rc == cli.EXIT_OK
    assert w.batch_calls == []
    assert len(w.inspect_calls) == len(w.water_calls) == 1
    data = w.report()
    assert data["driver_mode"] == "single"
    assert data["driver"]["status"] == "SUCCESS"
    assert "batch_progress" not in data


@pytest.mark.parametrize("count", [0, 2])
def test_no_target_before_or_after_success_is_not_reinterpreted(tmp_path, count):
    w = World(tmp_path, TARGETS[:count])
    rc, _, _ = w.run()
    data = w.report()
    assert data["driver"]["status"] == "NO_TARGET"
    assert data["driver"]["success"] is bool(count)
    assert rc == (cli.EXIT_OK if count else cli.EXIT_DRIVER)
    assert len(w.inspect_calls) == count + 1
    assert len(w.water_calls) == count
    assert data["driver"]["completed_targets"] == [list(t) for t in TARGETS[:count]]
    assert w.batch_calls[0].max_actions == 4
    assert w.batch_calls[0].deadline_seconds == 15.0


def test_duplicate_is_stopped_by_real_batch_before_second_water(tmp_path):
    w = World(tmp_path)
    w.keep_dry = True
    rc, _, _ = w.run()
    data = w.report()
    assert rc == cli.EXIT_DRIVER
    assert data["driver"]["status"] == "DUPLICATE_TARGET"
    assert data["driver"]["completed_targets"] == [list(TARGETS[0])]
    assert len(w.inspect_calls) == 2 and len(w.water_calls) == 1
    assert not data["ok"]


@pytest.mark.parametrize("kind", ["session", "player", "location", "tile"])
def test_pin_change_after_first_water_preserves_partial_result(tmp_path, kind):
    w = World(tmp_path)
    def change():
        if kind == "tile":
            w.state(tile={"x": 12, "y": 11}, pixel={"x": 768.0, "y": 704.0})
        else:
            values = {"session": {"session_id": "sesi-B"},
                      "player": {"player_id": H.P_LAIN},
                      "location": {"location_id": "Cabin"}}
            H.tulis_sesi(tmp_path, **values[kind])
    w.after_water = change
    rc, _, _ = w.run()
    data = w.report()
    assert rc == cli.EXIT_DRIVER
    assert data["driver"]["status"] == ("FARMER_MOVED" if kind == "tile" else "SESSION_UNREADABLE")
    assert data["driver"]["pin_session_id"] == H.SESSION_ID
    assert data["session"]["session_id"] == H.SESSION_ID
    assert data["driver"]["completed_targets"] == [list(TARGETS[0])]
    assert len(w.water_calls) == len(w.inspect_calls) == 1
    assert data["water_confirmed"] and not data["ok"]


def test_original_cli_pin_cannot_rebind_at_batch_entry(tmp_path):
    w = World(tmp_path)
    reads = 0
    def read(folder=None):
        nonlocal reads
        reads += 1
        if reads == 3:  # initial CLI read, post-startup read, batch entry
            H.tulis_sesi(tmp_path, session_id="sesi-B")
        return H.baca_sesi_asli(folder)
    rc, _, _ = w.run(baca_sesi=read)
    assert rc == cli.EXIT_DRIVER and len(w.batch_calls) == 1
    assert w.inspect_calls == w.water_calls == []
    assert w.report()["driver"]["status"] == "SESSION_UNREADABLE"


def test_newer_state_is_still_required(tmp_path):
    w = World(tmp_path)
    w.fresh = False
    rc, _, _ = w.run()
    assert rc == cli.EXIT_DRIVER
    assert w.report()["driver"]["status"] == "STATE_TIMEOUT"
    assert len(w.inspect_calls) == 1 and w.water_calls == []


def test_uncertain_second_water_preserves_first_without_retry(tmp_path):
    w = World(tmp_path)
    w.fail_water = 2
    rc, _, _ = w.run()
    data = w.report()
    assert rc == cli.EXIT_DRIVER and not data["ok"]
    assert data["driver"]["status"] == "WATER_UNCERTAIN"
    assert data["driver"]["completed_targets"] == [list(TARGETS[0])]
    assert data["driver"]["iterations"][-1]["water_outcome"] == "Unknown"
    assert data["water_confirmed"]  # at least one, not all
    assert len(w.batch_calls) == 1 and len(w.water_calls) == 2


@pytest.mark.parametrize("how", ["cancel", "deadline"])
def test_batch_admission_limits_are_forwarded(tmp_path, how):
    w = World(tmp_path)
    if how == "deadline":
        w.after_water = lambda: setattr(w, "now", 16.0)
    rc, _, _ = w.run(cek_batal=lambda: how == "cancel" and bool(w.water_calls))
    data = w.report()
    assert rc == cli.EXIT_DRIVER
    assert data["driver"]["status"] == ("CANCELLED" if how == "cancel" else "DEADLINE_EXCEEDED")
    assert data["driver"]["completed_targets"] == [list(TARGETS[0])]
    assert len(w.water_calls) == len(w.inspect_calls) == 1


def test_cancel_during_inspect_prevents_water(tmp_path):
    w = World(tmp_path)
    cancelled = False
    def cancel():
        nonlocal cancelled
        cancelled = True
    w.after_inspect = cancel
    rc, _, _ = w.run(cek_batal=lambda: cancelled)
    data = w.report()
    assert rc == cli.EXIT_DRIVER and data["driver"]["status"] == "CANCELLED"
    assert len(w.inspect_calls) == 1 and not w.water_calls
    assert data["driver"]["iterations"][0]["inspect_outcome"] == "Inspected"


def test_interrupted_second_water_has_partial_evidence_not_fabricated_result(tmp_path):
    w = World(tmp_path)
    w.interrupt_water = 2
    rc, out, _ = w.run()
    data = w.report()
    assert rc == cli.EXIT_DRIVER and data["cli_status"] == "CANCELLED"
    assert data["driver"] is None and not data["ok"]
    progress = data["batch_progress"]
    assert not progress["result_returned"]
    assert progress["source"] == "returned_command_calls"
    assert progress["completed_targets"] == [list(TARGETS[0])]
    assert data["water_confirmed"] and data["command_attempted"]
    assert len(data["command_calls"]) == 4
    uncertain = data["command_calls"][-1]
    assert uncertain["op"] == "water_once" and not uncertain["returned"]
    assert uncertain["request_id"] is None and uncertain["outcome"] is None
    assert data["receiver"]["stopped"] and "completed=1" in out
    assert len(w.batch_calls) == 1 and len(w.water_calls) == 2


@pytest.mark.parametrize("interrupt", [False, True])
def test_json_failure_after_partial_batch_never_reexecutes(tmp_path, interrupt):
    w = World(tmp_path)
    if interrupt:
        w.interrupt_water = 2
    else:
        w.fail_water = 2
    captured = []
    def fail(path, report):
        captured.append(json.loads(json.dumps(report)))
        raise OSError("disk full")
    rc, out, _ = w.run(tulis_json=fail)
    assert rc == cli.EXIT_OUTPUT and "TIDAK diulang" in out
    assert len(w.batch_calls) == 1 and len(w.water_calls) == 2
    assert len(captured) == 1 and not captured[0]["ok"]
    assert captured[0]["batch_progress"]["completed_targets"] == [list(TARGETS[0])]
    assert captured[0]["receiver"]["stopped"]


def test_busy_port_prevents_batch_and_preserves_other_listener(tmp_path):
    w = World(tmp_path)
    rc, _, _ = w.run(receiver_cls=H.ReceiverSibuk)
    assert rc == cli.EXIT_REFUSED
    assert w.report()["cli_status"] == "PORT_BUSY"
    assert w.batch_calls == w.inspect_calls == w.water_calls == []


def test_stop_failure_not_reported_as_stopped(tmp_path):
    w = World(tmp_path)
    rc, _, _ = w.run("--max-actions", "2", receiver_cls=H.ReceiverStopGagal)
    data = w.report()
    assert rc == cli.EXIT_OK  # command success and cleanup are separate
    assert not data["receiver"]["stopped"] and data["receiver"]["stop_error"]
    assert len(w.batch_calls) == 1 and len(w.water_calls) == 2


def test_batch_real_loopback_receiver_and_file_inbox(tmp_path):
    """Only the game/mod responder is simulated. All product layers are real."""
    H.tulis_sesi(tmp_path)
    socket_box = {}
    seq = 2
    def on_op(request):
        nonlocal seq
        if request["op"] == "inspect_tiles":
            seq += 1
            H._kirim(socket_box["sock"], H._state(seq=seq))
    def on_listener(host, port):
        socket_box["sock"] = socket.create_connection((host, port), timeout=2)
        H._kirim(socket_box["sock"], H._hello())
        H._kirim(socket_box["sock"], H._state())
    responses = [inspect_envelope(*TARGETS[:2]), H.envelope_water(session_id=H.SESSION_ID),
                 inspect_envelope(TARGETS[1]), H.envelope_water(session_id=H.SESSION_ID)]
    output = tmp_path / "batch.json"
    with H.ModBerantai(tmp_path, responses, on_op=on_op) as mod:
        try:
            rc, out, _ = H.jalankan_cli(
                ["--folder", str(tmp_path), "--port", "0", "--wait", "2",
                 "--max-age", "5", "--state-timeout", "2", "--execute", "--batch",
                 "--max-actions", "2", "--json", str(output)],
                on_listener=on_listener,
            )
        finally:
            if "sock" in socket_box:
                socket_box["sock"].close()
    data = json.loads(output.read_text(encoding="utf-8"))
    assert rc == cli.EXIT_OK, out
    assert data["driver"]["completed_targets"] == [list(t) for t in TARGETS[:2]]
    assert data["driver"]["status"] == "LIMIT_REACHED"
    assert [r["op"] for r in mod.permintaan] == ["inspect_tiles", "water_once"] * 2
    assert len({r["request_id"] for r in mod.permintaan}) == 4
    assert [(r["tile_x"], r["tile_y"]) for r in mod.permintaan if r["op"] == "water_once"] == list(TARGETS[:2])
    assert data["receiver"]["host"] == "127.0.0.1" and data["receiver"]["stopped"]
    assert not data["receiver"]["lost_events"] and not data["verified_live"]


def test_batch_result_after_module_reload_uses_selected_driver_mode(tmp_path):
    # The existing import/no-IO test reloads this module in the combined suite.
    # A returned batch must not fall through to the single-shot serializer.
    import importlib
    import arti_stardew_farm_batch as engine
    importlib.reload(engine)
    w = World(tmp_path)
    rc, out, _ = w.run("--max-actions", "2")
    data = w.report()
    assert rc == cli.EXIT_OK, out
    assert data["driver"]["status"] == "LIMIT_REACHED"
    assert data["batch_progress"]["result_returned"]
    assert data["water_confirmed"] and len(w.water_calls) == 2
