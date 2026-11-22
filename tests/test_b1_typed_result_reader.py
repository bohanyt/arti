"""WP P1 (R0-09 §9.2): B1 typed-result reader on the legacy decoder path (SCH-11; R0-07 C2, §5, §13.2). L1, lab."""
import pytest

import arti_stardew_command as command
from tests import contracts_support as cs


def _hasil(isi):
    return command._hasil_dari_envelope(isi, isi["request_id"])


def _legacy(**changes):
    return dict(cs.build(cs.golden("SCH-11")["cases"][0]), **changes)


def test_SCH_11_b1_reader_takes_the_golden_decoder_path():
    base_docs = cs.bases()
    for case in cs.golden("SCH-11")["cases"]:
        assert command.decoder_path(cs.build(case, base_docs)) == case["decoder_path"], case["name"]


@pytest.mark.parametrize("reason", ["debt_active", "stop_latched", "lease_expired", "stale_control_seq",
                                    "interrupted_before_edge"])
def test_SCH_11_v3_refusal_is_typed_and_never_a_success(reason):
    doc = cs.bases()["verb_result_refused"]
    doc["not_executed"]["reason"] = reason
    assert cs.errors("verb_result.schema.json", doc) == []
    hasil = _hasil(doc)
    assert (hasil.status, hasil.sukses) == (command.DITOLAK, False)
    assert hasil.refusal["decoder_path"] == "v3" and hasil.refusal["code"] == reason
    assert hasil.alasan == f"{reason}:{doc['not_executed']['native_code']}"


def test_SCH_11_v3_unknown_code_is_unclassified_and_executed_v3_is_never_a_success():
    doc = cs.bases()["verb_result_refused"]
    doc["not_executed"]["reason"] = "made_up"
    hasil = _hasil(doc)
    assert hasil.refusal["code"] is None and hasil.alasan.startswith("defect-unclassified:") and not hasil.sukses
    hasil = _hasil(cs.bases()["verb_result_ok"])
    assert (hasil.status, hasil.sukses, hasil.refusal) == (command.TIDAK_PASTI, False, None)


@pytest.mark.parametrize("reject,arm,code", [
    ("StopLatched", None, "stop_latched"), ("LeaseExpired", None, "lease_expired"),
    ("StaleControlSeq", None, "stale_control_seq"), ("CommandBusy", None, "premotor_busy"),
    ("ArmRejected", "N0_NOT_CONSUMED", "interrupted_before_edge"), ("ArmRejected", "RejectedNoMenu", "backend_undeliverable"),
    ("NotInTheNativeEnum", None, None)])
def test_SCH_11_legacy_rejection_maps_through_native_map(reject, arm, code):
    hasil = _hasil(_legacy(status="rejected", accepted=False, success=False, reject_reason=reject, arm_status=arm))
    assert (hasil.status, hasil.sukses) == (command.DITOLAK, False)
    assert hasil.refusal["decoder_path"] == "legacy" and hasil.refusal["code"] == code
    assert hasil.alasan.startswith(reject)  # the legacy reason string callers match on is unchanged
    expected = cs.classify_native(hasil.refusal["native_code"])
    assert expected == (code or "defect-unclassified")


@pytest.mark.parametrize("changes", [dict(status="rejected", reject_reason="DuplicateRequestId"),
                                     dict(status="completed"), dict(status="interrupted"), dict(status="uncertain")])
def test_SCH_11_legacy_non_refusals_carry_no_typed_refusal(changes):
    assert _hasil(_legacy(**changes)).refusal is None
