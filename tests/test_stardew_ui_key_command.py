"""WS2 ui_key codec: exact args, digest parity with the mod, publication, outcome mapping, CLI gate.

No game, no provider, no native folder: tmp_path session files only. UNIT_TESTED.
"""
import hashlib
import json

import pytest

from tests.test_stardew_leased_water_command import C, LEASE, write_session

RECEIVER = "rcv:mi12"


def token(*parts):
    return "uk:" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


TOKEN = token(RECEIVER, "LevelUpMenu", "level_up", "confirm", "s=2|l=1")
CONFIRM = dict(receiver_token=RECEIVER, menu_type="LevelUpMenu", kind="level_up", key="confirm", target_token=TOKEN)
ANSWER = dict(receiver_token=RECEIVER, menu_type="DialogueBox", kind="question", key="answer", target_token=TOKEN,
              option_index=1)
CHOOSE = dict(receiver_token=RECEIVER, menu_type="LevelUpMenu", kind="level_up_profession", key="choose",
              target_token=TOKEN, option_index=0, profession_id=12)
CANCEL = dict(receiver_token=RECEIVER, menu_type="DialogueBox", kind="question", key="cancel", target_token=TOKEN)


@pytest.mark.parametrize("args", [CONFIRM, ANSWER, CHOOSE, CANCEL,
                                  dict(CONFIRM, menu_type="DialogueBox", kind="dialogue", key="advance"),
                                  dict(CONFIRM, menu_type="ShippingMenu", kind="shipping_summary"),
                                  dict(CONFIRM, menu_type="LetterViewerMenu", kind="letter", key="close")])
def test_valid_ui_key_args(args):
    C.validate_ui_key_args(dict(args))


@pytest.mark.parametrize("base", [CONFIRM, ANSWER, CHOOSE])
def test_each_field_is_pinned_missing_wrong_type_empty_long_or_extra(base):
    for key in base:
        missing = dict(base)
        del missing[key]
        with pytest.raises(C.KesalahanPerintah):
            C.validate_ui_key_args(missing)
        wrong = dict(base, **{key: "1" if key in ("option_index", "profession_id") else 7})
        with pytest.raises(C.KesalahanPerintah):
            C.validate_ui_key_args(wrong)
        if key not in ("option_index", "profession_id"):
            for bad in ("", "  ", "x" * 129):
                with pytest.raises(C.KesalahanPerintah):
                    C.validate_ui_key_args(dict(base, **{key: bad}))
    with pytest.raises(C.KesalahanPerintah):
        C.validate_ui_key_args(dict(base, extra=1))


@pytest.mark.parametrize("change", [
    dict(receiver_token="mi12"),                 # raw serial, not the rcv: namespace
    dict(target_token="uk:XYZ"), dict(target_token="tok"),
    dict(kind="level_up_profession"),             # key/kind outside the table
    dict(menu_type="ShippingMenu"),
    dict(key="choose"),
    dict(option_index=0), dict(profession_id=1),  # confirm carries neither
])
def test_confirm_shapes_and_table(change):
    with pytest.raises(C.KesalahanPerintah):
        C.validate_ui_key_args(dict(CONFIRM, **change))


@pytest.mark.parametrize("args", [dict(ANSWER, option_index=8), dict(ANSWER, option_index=-1),
                                  dict(ANSWER, option_index=True), dict(ANSWER, profession_id=3),
                                  dict(CHOOSE, option_index=2), dict(CHOOSE, profession_id=30),
                                  {k: v for k, v in CHOOSE.items() if k != "profession_id"}])
def test_option_and_profession_rules(args):
    with pytest.raises(C.KesalahanPerintah):
        C.validate_ui_key_args(args)


def test_digest_matches_native_vector_and_pins_every_arg():
    facts = "r=Yes\x1fNo1|h=" + hashlib.sha256(b"Hello there").hexdigest()
    args = dict(receiver_token=RECEIVER, menu_type="DialogueBox", kind="question", key="answer",
                target_token=token(RECEIVER, "DialogueBox", "question", "answer", facts), option_index=1)
    payload = dict(protocol=1, op="input_once", request_id="tool-1", runtime_instance_id="runtime",
                   session_id="session", owner_id="owner", generation=1, control_seq=1, context_generation=7,
                   expected_player_id=42, expected_player_name="arti", expected_screen_id=0, expected_location_id="farm",
                   family="ui_key", primitive="ui_key", args=args, deadline_unix_ms=1000)
    # Printed by ProtocolTests (--ui-key): "ui_key digest vector: ..."
    assert C._journal_payload_digest(payload) == "a03a02b4be7f131122f862555c34bbd9fb55e62430c8fbb2e77c238bed544d01"
    digests = {C._journal_payload_digest(payload)}
    for change in (dict(receiver_token="rcv:mi13"), dict(kind="npc_question"), dict(option_index=0),
                   dict(target_token=TOKEN), dict(menu_type="DialogueBoxX")):
        digests.add(C._journal_payload_digest(dict(payload, args=dict(args, **change))))
    cancel = {k: v for k, v in args.items() if k != "option_index"}
    digests.add(C._journal_payload_digest(dict(payload, args=dict(cancel, key="cancel"))))
    assert len(digests) == 7


def test_minta_ui_key_payload_is_exact_and_gated(tmp_path, monkeypatch):
    write_session(tmp_path, ui_key_enabled=True)
    seen = []

    def wait(folder, request_id, timeout):
        seen.append(json.loads((folder / C.NAMA_REQUEST).read_text()))
        return C.HasilPerintah(C.TIDAK_DIAMBIL, False, request_id)
    monkeypatch.setattr(C, "_tunggu_hasil", wait)
    C.minta_ui_key(**ANSWER, folder=tmp_path, **LEASE)
    assert len(seen) == 1
    payload = seen[0]
    assert payload["op"] == "input_once" and payload["family"] == "ui_key" and payload["primitive"] == "ui_key"
    assert payload["args"] == ANSWER
    assert payload["context_generation"] == LEASE["context_generation"]


@pytest.mark.parametrize("gate", [False, None, 1, "true"])
def test_session_gate_must_be_true(tmp_path, gate):
    write_session(tmp_path, ui_key_enabled=gate)
    assert C.minta_ui_key(**CONFIRM, folder=tmp_path, **LEASE).status == C.GAGAL_LOKAL
    assert not (tmp_path / C.NAMA_REQUEST).exists()
    sesi = C.baca_sesi(tmp_path)
    assert sesi.alasan_belum_siap(C.OP_INPUT_ONCE, primitive="ui_key") is not None


def test_invalid_args_never_publish(tmp_path):
    write_session(tmp_path, ui_key_enabled=True)
    with pytest.raises(C.KesalahanPerintah):
        C.minta_ui_key(**dict(CONFIRM, target_token="bad"), folder=tmp_path, **LEASE)
    assert not (tmp_path / C.NAMA_REQUEST).exists()


def _envelope(outcome, status="completed", success=True):
    return {"status": status, "success": success, "outcome": outcome, "op": "input_once", "primitive": "ui_key"}


def test_only_ui_key_applied_is_success():
    assert C._hasil_dari_envelope(_envelope("UiKeyApplied"), "r").sukses is True
    assert C._hasil_dari_envelope(_envelope("UiKeyNoEffect", success=False), "r").sukses is False
    assert C._hasil_dari_envelope(_envelope("UiKeyUncertain", "uncertain", False), "r").status == C.TIDAK_PASTI
    assert C._hasil_dari_envelope(_envelope("MenuExited"), "r").sukses is False  # another primitive's outcome


def test_cli_prints_block_and_refuses_to_send_without_yes(tmp_path, monkeypatch, capsys):
    write_session(tmp_path, ui_key_enabled=True)
    context = {"context_generation": 9, "runtime_instance_id": "runtime-water", "location_id": "farm",
               "capabilities": {"ui_key": {"implemented": True, "available": True, "reason": "ready"}},
               "ui_key": {"receiver_token": RECEIVER, "menu_type": "LevelUpMenu", "kind": "level_up", "ready": True,
                          "wait_only": False, "keys": [{"key": "confirm", "target_token": TOKEN}]}}
    monkeypatch.setattr(C, "minta_inspect_context", lambda **kw: C.HasilPerintah(
        C.SELESAI, True, "i", "ContextInspected", mentah={"context": context}))
    controls = []
    monkeypatch.setattr(C, "tulis_kontrol", lambda *a, **k: controls.append((a, k)))
    assert C.main(["--ui-key", "--folder", str(tmp_path)]) == 0
    assert '"receiver_token": "rcv:mi12"' in capsys.readouterr().out
    assert C.main(["--ui-key", "--folder", str(tmp_path), "--key", "confirm"]) == 2
    assert "DRY" in capsys.readouterr().out
    assert controls == [] and not (tmp_path / C.NAMA_REQUEST).exists()


@pytest.mark.parametrize("option,profession,code", [(1, 12, 2), (0, 13, 2), (0, 12, 2)])
def test_P1_cli_never_chooses_a_profession(tmp_path, monkeypatch, capsys, option, profession, code):
    write_session(tmp_path, ui_key_enabled=True)
    block = {"receiver_token": RECEIVER, "menu_type": "LevelUpMenu", "kind": "level_up_profession", "ready": True,
             "wait_only": False, "keys": [{"key": "choose", "target_token": TOKEN}], "skill": 2, "level": 5,
             "professions": [{"index": 0, "profession_id": 12, "name": "Forester"},
                             {"index": 1, "profession_id": 13, "name": "Gatherer"}]}
    context = {"context_generation": 9, "runtime_instance_id": "runtime-water", "location_id": "farm",
               "capabilities": {"ui_key": {"implemented": True, "available": True}}, "ui_key": block}
    monkeypatch.setattr(C, "minta_inspect_context", lambda **kw: C.HasilPerintah(
        C.SELESAI, True, "i", "ContextInspected", mentah={"context": context}))
    controls = []
    monkeypatch.setattr(C, "tulis_kontrol", lambda *a, **k: controls.append(a))
    rc = C.main(["--ui-key", "--folder", str(tmp_path), "--key", "choose", "--option", str(option),
                 "--profession", str(profession)])
    out = capsys.readouterr()
    assert rc == code and controls == []
    assert "profesi default" in out.err  # WP P1 (owner O4): no program pick, so the CLI never sends a choose
