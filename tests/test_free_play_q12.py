import json
from types import SimpleNamespace

import pytest

from scripts.free_play.q12_drill import verdict
from scripts.free_play import soak
from scripts.m1_operator import uipad_op


def receipt(before, after, **changes):
    return {"ui_pad_readback": json.dumps(dict(before={"inventory": before}, after={"inventory": after},
        delivered=True, released=True, **changes))}


def bag():
    return [dict(item=str(i), stack=i + 1, quality=0) for i in range(36)]


@pytest.mark.parametrize("button", ["Start", "LB", "RB"])
def test_no_change_is_negative_native_evidence(button):
    items = bag()
    assert verdict(button, receipt(items, items), {"ui_motor": {"active_menu": ""}}) == dict(
        opened=False, slot_shifted=False, delivered=True, released=True)


@pytest.mark.parametrize("button,shift", [("LB", 12), ("RB", -12)])
def test_only_complete_row_rotation_counts(button, shift):
    items = bag()
    after = items[shift:] + items[:shift]
    assert verdict(button, receipt(items, after), {"ui_motor": {"active_menu": ""}})["slot_shifted"]
    after[0] = dict(item="other", stack=1, quality=0)
    assert not verdict(button, receipt(items, after), {"ui_motor": {"active_menu": ""}})["slot_shifted"]


def test_native_receiver_is_menu_witness():
    items = bag()
    assert verdict("Start", receipt(items, items), {"ui_motor": {"active_menu": "GameMenu"}})["opened"]


@pytest.mark.parametrize("field", ["delivered", "released"])
def test_missing_delivery_or_release_is_not_unsupported_evidence(field):
    raw = receipt(bag(), bag())
    ledger = json.loads(raw["ui_pad_readback"])
    ledger[field] = False
    raw["ui_pad_readback"] = ledger
    with pytest.raises(RuntimeError, match="not capability evidence"):
        verdict("Start", raw, {"ui_motor": {"active_menu": "GameMenu"}})


def test_q12_staging_makes_toolbar_observable_only_on_disposable_copy(tmp_path):
    template = tmp_path / "World_99"
    template.mkdir()
    source = template / template.name
    source.write_text('<SaveGame><whichFarm>0</whichFarm><uniqueIDForThisGame>99</uniqueIDForThisGame>'
        '<player><UniqueMultiplayerID>11</UniqueMultiplayerID></player><farmhands><Farmer>'
        '<UniqueMultiplayerID>22</UniqueMultiplayerID><name>Arti</name><maxItems>12</maxItems>'
        '<items><Item><name>Axe</name></Item></items></Farmer></farmhands></SaveGame>')
    original = source.read_bytes()
    saves = tmp_path / "saves"
    saves.mkdir()
    result = soak.stage_template(template, saves, "Standard", tmp_path, q12=True)
    staged = soak.ET.parse(saves / template.name / template.name).find("farmhands/Farmer")
    assert staged.findtext("maxItems") == "36"
    assert len(staged.find("items")) == 36
    assert staged.findtext("items/Item/name") == "Axe"
    assert source.read_bytes() == original
    assert "Q12" in result["changes"][-1]


def test_native_receipt_session_without_v_marker_admits_only_its_idle_lease():
    context = dict(session_id="s", player_id=42, player_name="Arti", screen_id=0,
                   location_id="Farm", runtime_instance_id="r", context_generation=1)
    raw = dict(context, halt=None, input_idle={}, owner_id="q12", generation=1, control_seq=1,
        record_faulted=False, release_requested=False, release_acked=True, pad_mode_owned=False,
        pad_mode_restore_pending=False, pad_outstanding_count=0, lease_until_unix_ms=2000)
    session = SimpleNamespace(**context, mentah=raw, alasan_belum_siap=lambda *a, **k: None)
    identity = uipad_op._identity(session, context)
    clock = SimpleNamespace(time=lambda: 1)
    assert uipad_op._lease_admission(session, context, identity, "q12", 1, clock) == 1
    raw["pad_outstanding_count"] = 1
    with pytest.raises(ValueError, match="lease_not_ready"):
        uipad_op._lease_admission(session, context, identity, "q12", 1, clock)
