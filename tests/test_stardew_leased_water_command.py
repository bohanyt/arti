from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import arti_stardew_command as C  # noqa: E402

SESSION = {
    "protocol": 1,
    "ev": "command_session",
    "session_id": "sesi-water",
    "runtime_instance_id": "runtime-water",
    "player_id": 100000000012,
    "player_name": "artihand",
    "screen_id": 2,
    "location_id": "FarmUnique",
    "is_main_player": False,
    "is_split_screen": False,
    "inbox_enabled": True,
    "menu_step_enabled": False,
    "water_once_enabled": True,
    "world_press_enabled": True,
    "world_ready": True,
    "inflight_request_id": None,
    "input_busy": False,
}
LEASE = dict(owner_id="owner-water", generation=7, control_seq=11, context_generation=17)


def write_session(folder: Path, **changes) -> None:
    value = dict(SESSION)
    value.update(changes)
    (folder / C.NAMA_SESSION).write_text(json.dumps(value), encoding="utf-8")


class FakeMod:
    def __init__(self, folder: Path, result: dict | None):
        self.folder = folder
        self.result = result
        self.requests: list[dict] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.stop.set()
        self.thread.join(timeout=2)

    def _run(self):
        path = self.folder / C.NAMA_REQUEST
        while not self.stop.is_set():
            if path.exists():
                request = json.loads(path.read_text(encoding="utf-8"))
                self.requests.append(request)
                path.unlink()
                if self.result is not None:
                    result = dict(self.result)
                    result.setdefault("request_id", request["request_id"])
                    tmp = self.folder / (C.NAMA_RESULT + ".tmp")
                    tmp.write_text(json.dumps(result), encoding="utf-8")
                    tmp.replace(self.folder / C.NAMA_RESULT)
                return
            time.sleep(0.005)


def result(**changes) -> dict:
    value = {
        "protocol": 1,
        "ev": "command_result",
        "op": "water_once",
        "session_id": SESSION["session_id"],
        "accepted": True,
        "status": "completed",
        "reject_reason": None,
        "arm_status": "Queued",
        "run_id": 3,
        "outcome": "ObservedWatered",
        "success": True,
        "completed_unix_ms": 1,
    }
    value.update(changes)
    return value


@pytest.fixture
def box(tmp_path: Path) -> Path:
    write_session(tmp_path)
    return tmp_path


def call(box: Path, **kwargs):
    return C.minta_water_once_leased(53, 17, folder=box, batas_detik=0.35, **LEASE, **kwargs)


@pytest.mark.parametrize("operation", ["move", "water"])
@pytest.mark.parametrize("mode", ["legacy", "guarded", "denied"])
def test_guarded_publication_wraps_actual_write_but_not_wait(box, monkeypatch, operation, mode):
    events = []
    in_guard = False
    actual_write = C._tulis_permintaan

    def write(folder, payload):
        assert in_guard == (mode == "guarded")
        events.append("publish")
        actual_write(folder, payload)

    def wait(folder, request_id, timeout):
        assert not in_guard
        events.append("wait")
        payload = json.loads((folder / C.NAMA_REQUEST).read_text(encoding="utf-8"))
        assert payload["request_id"] == request_id
        assert payload["op"] == (C.OP_INPUT_ONCE if operation == "move" else C.OP_WATER_ONCE)
        return C.HasilPerintah(C.SELESAI, True, request_id, "synthetic")

    def guard(publish):
        nonlocal in_guard
        events.append("guard")
        if mode == "denied":
            raise RuntimeError("superseded")
        in_guard = True
        try:
            publish()
        finally:
            in_guard = False
        events.append("unlock")

    monkeypatch.setattr(C, "_tulis_permintaan", write)
    monkeypatch.setattr(C, "_tunggu_hasil", wait)
    fn, args = (C.minta_input_once, ("right", 10)) if operation == "move" else (
        C.minta_water_once_leased, (53, 17))
    kwargs = {} if mode == "legacy" else {"guarded_publish": guard}
    if mode == "denied":
        with pytest.raises(RuntimeError, match="superseded"):
            fn(*args, folder=box, **LEASE, **kwargs)
        assert events == ["guard"] and not (box / C.NAMA_REQUEST).exists()
    else:
        assert fn(*args, folder=box, **LEASE, **kwargs).sukses
        assert events == (["publish", "wait"] if mode == "legacy"
                          else ["guard", "publish", "unlock", "wait"])


def test_exact_flat_leased_envelope_and_current_pins(box: Path):
    with FakeMod(box, result()) as mod:
        outcome = call(box)
    assert outcome.status == C.SELESAI and outcome.sukses
    assert len(mod.requests) == 1
    request = mod.requests[0]
    assert set(request) == {
        "protocol", "op", "request_id", "runtime_instance_id", "session_id",
        "owner_id", "generation", "control_seq", "context_generation",
        "expected_player_id", "expected_player_name", "expected_screen_id",
        "expected_location_id", "tile_x", "tile_y", "deadline_unix_ms",
    }
    assert request["op"] == "water_once"
    assert request["runtime_instance_id"] == SESSION["runtime_instance_id"]
    assert request["session_id"] == SESSION["session_id"]
    assert request["owner_id"] == LEASE["owner_id"]
    assert request["generation"] == LEASE["generation"]
    assert request["control_seq"] == LEASE["control_seq"]
    assert request["context_generation"] == LEASE["context_generation"]
    assert request["expected_player_id"] == SESSION["player_id"]
    assert request["expected_player_name"] == SESSION["player_name"]
    assert request["expected_screen_id"] == SESSION["screen_id"]
    assert request["expected_location_id"] == SESSION["location_id"]
    assert (request["tile_x"], request["tile_y"]) == (53, 17)
    assert request["deadline_unix_ms"] > C._sekarang_ms() - 1000
    assert "args" not in request and "family" not in request and "primitive" not in request
    assert "request_seq" not in request
    assert not request["request_id"].startswith(C.JOURNAL_REQUEST_PREFIX)
    assert not (box / C.NAMA_CONTROL).exists() and not (box / C.NAMA_STOP).exists()


def test_journal_capability_adds_content_bound_monotonic_request_sequence(box: Path):
    write_session(box, journal_supported=True, journal_schema=C.JOURNAL_SCHEMA)

    with FakeMod(box, result()) as first_mod:
        first = call(box)
    with FakeMod(box, result()) as second_mod:
        second = call(box)

    assert first.sukses and second.sukses
    assert len(first_mod.requests) == len(second_mod.requests) == 1
    one = first_mod.requests[0]
    two = second_mod.requests[0]
    assert one["control_seq"] == two["control_seq"] == LEASE["control_seq"]
    assert isinstance(one["request_seq"], int) and one["request_seq"] > 0
    assert two["request_seq"] > one["request_seq"]
    assert one["request_seq"] != one["control_seq"]
    assert one["request_id"].startswith(C.JOURNAL_REQUEST_PREFIX)
    assert two["request_id"].startswith(C.JOURNAL_REQUEST_PREFIX)
    assert one["request_id"] != two["request_id"]

    digest = C._journal_payload_digest(one)
    identity = (
        C._journal_namespace_text(one)
        + f"\nrequest_seq={one['request_seq']}\npayload_sha256={digest}"
    )
    expected_id = C.JOURNAL_REQUEST_PREFIX + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()
    assert one["request_id"] == expected_id


def test_unknown_journal_schema_stays_legacy_v1(box: Path):
    write_session(box, journal_supported=True, journal_schema=999)
    with FakeMod(box, result()) as mod:
        outcome = call(box)
    assert outcome.sukses
    request = mod.requests[0]
    assert "request_seq" not in request
    assert not request["request_id"].startswith(C.JOURNAL_REQUEST_PREFIX)


def test_journal_uncertain_result_is_never_upgraded_or_retried(box: Path, monkeypatch):
    write_session(box, journal_supported=True, journal_schema=C.JOURNAL_SCHEMA)
    writes: list[dict] = []
    real_write = C._tulis_permintaan

    def counted(folder: Path, payload: dict) -> None:
        writes.append(dict(payload))
        real_write(folder, payload)

    monkeypatch.setattr(C, "_tulis_permintaan", counted)
    native = result(
        accepted=True,
        status="uncertain",
        success=False,
        outcome=None,
        reject_reason="JournalOutstandingUncertain",
    )
    with FakeMod(box, native) as mod:
        outcome = call(box)
    assert outcome.status == C.TIDAK_PASTI
    assert not outcome.pasti and not outcome.sukses
    assert len(writes) == len(mod.requests) == 1
    assert writes[0]["request_id"] == outcome.request_id
    assert writes[0]["request_seq"] == mod.requests[0]["request_seq"]


def test_explicit_runtime_location_screen_are_preserved(box: Path):
    with FakeMod(box, result()) as mod:
        outcome = call(
            box,
            runtime_instance_id="runtime-explicit",
            expected_location_id="LocationToken42",
            expected_screen_id=4,
        )
    assert outcome.sukses
    request = mod.requests[0]
    assert request["runtime_instance_id"] == "runtime-explicit"
    assert request["expected_location_id"] == "LocationToken42"
    assert request["expected_screen_id"] == 4


@pytest.mark.parametrize(
    "lease_changes,session_changes,extra",
    [
        ({"owner_id": ""}, {}, {}),
        ({"generation": 0}, {}, {}),
        ({"generation": True}, {}, {}),
        ({"control_seq": 0}, {}, {}),
        ({"context_generation": 0}, {}, {}),
        ({}, {"runtime_instance_id": ""}, {}),
        ({}, {"location_id": ""}, {}),
        ({}, {"screen_id": -1}, {}),
        ({}, {"player_id": 0}, {}),
        ({}, {"player_name": ""}, {}),
        ({}, {}, {"runtime_instance_id": 123}),
        ({}, {}, {"expected_location_id": ""}),
        ({}, {}, {"expected_screen_id": True}),
        ({}, {}, {"admisi_detik": float("inf")}),
        ({}, {}, {"admisi_detik": float("nan")}),
        ({}, {}, {"admisi_detik": 0}),
    ],
)
def test_invalid_lease_or_binding_fails_before_request_write(
    box: Path, lease_changes: dict, session_changes: dict, extra: dict
):
    if session_changes:
        write_session(box, **session_changes)
    lease = dict(LEASE)
    lease.update(lease_changes)
    outcome = C.minta_water_once_leased(
        53, 17, folder=box, batas_detik=0.05, **lease, **extra
    )
    assert outcome.status == C.GAGAL_LOKAL and outcome.pasti and not outcome.sukses
    assert not (box / C.NAMA_REQUEST).exists()
    assert not (box / C.NAMA_CONTROL).exists() and not (box / C.NAMA_STOP).exists()


def test_pre_admission_busy_is_definite_zero_input(box: Path):
    write_session(box, input_busy=True)
    outcome = call(box)
    assert outcome.status == C.GAGAL_LOKAL and outcome.pasti and not outcome.sukses
    assert "input" in (outcome.alasan or "")
    assert not (box / C.NAMA_REQUEST).exists()


def test_legacy_helper_remains_lease_less(box: Path):
    with FakeMod(box, result()) as mod:
        outcome = C.minta_water_once(53, 17, folder=box, batas_detik=0.35)
    assert outcome.sukses
    request = mod.requests[0]
    assert set(request) == {
        "protocol", "op", "request_id", "session_id", "expected_player_id",
        "expected_player_name", "tile_x", "tile_y", "deadline_unix_ms",
    }
    for key in ("runtime_instance_id", "owner_id", "generation", "control_seq",
                "context_generation", "expected_screen_id", "expected_location_id",
                "request_seq"):
        assert key not in request


@pytest.mark.parametrize(
    "native_result,expected_status,expected_success",
    [
        ({}, C.SELESAI, True),
        ({"outcome": "AlreadyWatered", "success": True}, C.TIDAK_PASTI, False),
        ({"outcome": "UnknownBeginError", "success": False}, C.TIDAK_PASTI, False),
        ({"outcome": "ObservedWatered", "success": False}, C.TIDAK_PASTI, False),
    ],
)
def test_only_observed_watered_full_success_is_success(
    box: Path, native_result: dict, expected_status: str, expected_success: bool
):
    with FakeMod(box, result(**native_result)):
        outcome = call(box)
    assert outcome.status == expected_status
    assert outcome.sukses is expected_success


def test_native_pre_admission_rejection_stays_definite(box: Path):
    rejected = result(
        accepted=False,
        status="rejected",
        success=False,
        outcome=None,
        reject_reason="StaleLease",
        arm_status=None,
    )
    with FakeMod(box, rejected):
        outcome = call(box)
    assert outcome.status == C.DITOLAK and outcome.pasti and not outcome.sukses
    assert "StaleLease" in (outcome.alasan or "")


def test_taken_without_result_is_uncertain_one_write_no_retry(box: Path, monkeypatch):
    writes: list[dict] = []
    real_write = C._tulis_permintaan

    def counted(folder: Path, payload: dict) -> None:
        writes.append(dict(payload))
        real_write(folder, payload)

    monkeypatch.setattr(C, "_tulis_permintaan", counted)
    with FakeMod(box, None) as mod:
        outcome = call(box)
    assert outcome.status == C.TIDAK_PASTI and not outcome.pasti and not outcome.sukses
    assert "JANGAN ulangi otomatis" in (outcome.alasan or "")
    assert len(writes) == len(mod.requests) == 1
    assert writes[0]["request_id"] == mod.requests[0]["request_id"] == outcome.request_id
    assert not (box / C.NAMA_REQUEST).exists()


def test_interrupted_leased_water_is_uncertain_not_known_no_effect(box: Path):
    with FakeMod(
        box,
        result(status="interrupted", success=False, outcome=None, reject_reason="ReleaseRequested"),
    ):
        outcome = call(box)
    assert outcome.status == C.TIDAK_PASTI and not outcome.pasti and not outcome.sukses
    assert "tidak membuktikan nol world effect" in (outcome.alasan or "")
    assert "JANGAN ulangi otomatis" in (outcome.alasan or "")


def test_timeout_retains_request_and_never_claims_definite_zero_input(box: Path):
    outcome = call(box)
    assert outcome.status == C.TIDAK_PASTI and not outcome.pasti and not outcome.sukses
    assert "JANGAN ulangi otomatis" in (outcome.alasan or "")
    assert json.loads((box / C.NAMA_REQUEST).read_text())["request_id"] == outcome.request_id