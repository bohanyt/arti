"""Result envelope success is per input_once primitive (live M1 attempt 9w, 2026-10-06)."""
import arti_stardew_command as C


def _envelope(op, outcome, primitive=None, status="completed", success=True):
    isi = dict(status=status, success=success, outcome=outcome, op=op, request_id="r-1", accepted=True)
    if primitive is not None:
        isi["primitive"] = primitive
    return isi


def test_input_once_primitives_have_their_own_success_outcome():
    cases = {
        None: "PressCompleted", "move_cardinal": "PressCompleted", "use_tool": "InputCompleted",
        "select_item": "SelectionObserved", "world_interact": "ContainerOpened",
        "container_transfer": "TransferObserved", "container_exit": "MenuExited",
    }
    for primitive, outcome in cases.items():
        ok = C._hasil_dari_envelope(_envelope(C.OP_INPUT_ONCE, outcome, primitive), "r-1")
        assert ok.status == C.SELESAI and ok.sukses is True and ok.alasan is None, (primitive, outcome)
        wrong = C._hasil_dari_envelope(_envelope(C.OP_INPUT_ONCE, "PressCompleted" if outcome != "PressCompleted" else "InputCompleted", primitive), "r-1")
        assert wrong.status == C.SELESAI and wrong.sukses is False and wrong.alasan.startswith("outcome="), primitive


def test_other_ops_unchanged_and_uncertain_never_succeeds():
    water = C._hasil_dari_envelope(_envelope(C.OP_WATER_ONCE, "ObservedWatered"), "r-1")
    assert water.sukses is True
    uncertain = C._hasil_dari_envelope(_envelope(C.OP_INPUT_ONCE, "InputCompleted", "use_tool", status="uncertain"), "r-1")
    assert uncertain.status == C.TIDAK_PASTI and uncertain.sukses is False
    unknown = C._hasil_dari_envelope(_envelope(C.OP_INPUT_ONCE, "InputCompleted", "fishing_cast"), "r-1")
    assert unknown.status == C.SELESAI and unknown.sukses is False
