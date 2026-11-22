"""CC mail staging on synthetic saves; no live save or bundle mutations."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from scripts.m1_operator import stage_save as stage
from tests.test_stardew_stage_save_farm_clear import Ops, save  # noqa: F401

RAW = stage.BOM + b'''<SaveGame><player><name>host</name><UniqueMultiplayerID>7</UniqueMultiplayerID>
<mailReceived><string>unrelated</string></mailReceived></player><farmhands><Farmer><name>Arti</name>
<UniqueMultiplayerID>-8</UniqueMultiplayerID><mailReceived /></Farmer></farmhands>
<bundles><boolean>false</boolean></bundles></SaveGame>'''
ARGS = ["edit", "--set", "mail_received=-8:canReadJunimoText"]


def test_selected_farmer_only_exact_bytes_idempotent_and_host_separate():
    new, row = stage.farmer_mail_received(RAW, "-8:canReadJunimoText")
    assert new == RAW.replace(b"<mailReceived />", b"<mailReceived><string>canReadJunimoText</string></mailReceived>")
    assert row["farmer_id"] == "-8" and row["added"] == ["canReadJunimoText"]
    assert stage.farmer_mail_received(new, "-8:canReadJunimoText")[0] == new
    final, _ = stage.farmer_mail_received(new, "7:ccDoorUnlock,canReadJunimoText")
    r = ET.fromstring(final)
    assert [n.text for n in r.findall("player/mailReceived/string")] == ["unrelated","ccDoorUnlock","canReadJunimoText"]
    assert [n.text for n in r.findall("farmhands/Farmer/mailReceived/string")] == ["canReadJunimoText"]
    assert r.findtext("bundles/boolean") == "false" and final.startswith(stage.BOM)


@pytest.mark.parametrize("value", ["Arti:canReadJunimoText", "9:ccDoorUnlock", "-8:allmail", "-8:seenJunimoNote",
    "-8:JojaMember", "-8:ccIsComplete", "-8:ccDoorUnlock,ccDoorUnlock", "-8:", "9223372036854775808:ccDoorUnlock"])
def test_nonallowlisted_or_ambiguous_selection_refuses(value):
    with pytest.raises(stage.Refused):
        stage.farmer_mail_received(RAW, value)


@pytest.mark.parametrize("raw", [RAW.replace(b"<mailReceived />", b""),
    RAW.replace(b"<UniqueMultiplayerID>-8</UniqueMultiplayerID>", b"<UniqueMultiplayerID>7</UniqueMultiplayerID>"),
    RAW.replace(b"<mailReceived />", b"<mailReceived><wrong /></mailReceived>"),
    RAW.replace(b"<mailReceived />", b"<mailReceived /><mailReceived />"),
    RAW.replace(b"<SaveGame>", b'<!DOCTYPE SaveGame [<!ENTITY x "bad">]><SaveGame>')])
def test_malformed_mail_or_duplicate_identity_refuses(raw):
    with pytest.raises(stage.Refused):
        stage.farmer_mail_received(raw, "7:ccDoorUnlock" if b"<UniqueMultiplayerID>-8" not in raw else "-8:canReadJunimoText")


@pytest.mark.parametrize("running", [[123], None])
def test_stage_mail_requires_games_closed_even_for_dry_run(save, running):
    save.write_bytes(RAW)
    ops = Ops(running)
    assert stage.main(ARGS,ops) == 2 and stage.main(ARGS+["--dry-run"],ops) == 2
    assert save.read_bytes() == RAW and not ops.writes and not stage.paths.PREP.exists()


def test_stage_mail_backup_atomic_write_exact_readback_and_noop(save,capsys):
    save.write_bytes(RAW)
    ops = Ops()
    assert stage.main(ARGS+["--dry-run"],ops) == 0
    capsys.readouterr()
    assert not ops.writes and not stage.paths.PREP.exists()
    assert stage.main(ARGS,ops) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verified"] and report["readback"][0]["matches_expected"]
    assert (Path(report["backup"])/save.name).read_bytes() == RAW
    assert ops.writes == [save] and save.with_name("SaveGameInfo").read_bytes() == b"preview untouched"
    assert stage.main(ARGS,ops) == 0 and ops.writes == [save]


def test_stage_mail_process_after_backup_and_corruption_are_detected(save,capsys):
    save.write_bytes(RAW)
    ops = Ops()
    checks = iter([(), [123]])
    ops.game_running = lambda: next(checks)
    assert stage.main(ARGS,ops) == 2 and not ops.writes and save.read_bytes() == RAW
    capsys.readouterr()
    ops = Ops()
    write = ops.write_atomic
    ops.write_atomic = lambda p,data: write(p,data.replace(b"<boolean>false", b"<boolean>true"))
    assert stage.main(ARGS,ops) == 4
    assert not json.loads(capsys.readouterr().out)["verified"]
