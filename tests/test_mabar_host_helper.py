"""Solo stream (owner 2026-11-15): the program runs the HOST through the booked ArtiHostHelper."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.livepath import mabar  # noqa: E402

SAVE = """<SaveGame><uniqueIDForThisGame>100000001</uniqueIDForThisGame>
<player><name>Bohan</name><UniqueMultiplayerID>111</UniqueMultiplayerID></player>
<locations><GameLocation><buildings><Building><indoors><farmhand><name>Arti</name>
<UniqueMultiplayerID>222</UniqueMultiplayerID></farmhand></indoors></Building></buildings></GameLocation></locations>
</SaveGame>"""


def _save(tmp_path, body=SAVE):
    d = tmp_path / "Saves" / "farmA_100000001"
    d.mkdir(parents=True)
    (d / "farmA_100000001").write_text(body, encoding="utf-8")
    return d


def test_ids_come_from_the_save_read_only(tmp_path):
    d = _save(tmp_path)
    before = (d / "farmA_100000001").read_bytes()
    ids = mabar.helper_save_ids(d)
    assert ids == dict(save_folder="farmA_100000001", save_id=100000001, host_id=111, farmhand_id=222,
                       farmhand_name="Arti")
    assert (d / "farmA_100000001").read_bytes() == before


def test_stream_config_has_no_gifts_and_passes_the_helper_checks(tmp_path):
    ids = mabar.helper_save_ids(_save(tmp_path))
    cfg = mabar.helper_host_config(tmp_path / "mabar-20261115", ids)
    assert cfg["Enabled"] is True and cfg["GiftTimes"] == [] and cfg["StageHostGiftCount"] == 0
    # ModEntry.Entry rejects these, so a bad config would stop the helper at start.
    assert len(cfg["RunId"]) >= 8 and Path(cfg["OutputDirectory"]).is_absolute()
    assert cfg["HostId"] != cfg["FarmhandId"] and cfg["SaveId"] != 0
    assert 0 <= cfg["HostDelaySeconds"] <= 60 and 0 < cfg["StartupPauseSeconds"] <= 300
    assert Path(cfg["SaveFolder"]).name == cfg["SaveFolder"]


def test_two_farmhands_refuse(tmp_path):
    body = SAVE.replace("</SaveGame>", "<farmhands><Farmer><name>X</name><UniqueMultiplayerID>333</UniqueMultiplayerID>"
                                        "</Farmer></farmhands></SaveGame>")
    import pytest
    with pytest.raises(RuntimeError):
        mabar.helper_save_ids(_save(tmp_path, body))


def test_install_copies_the_booked_helper_and_host_controller_follows(tmp_path, monkeypatch):
    _save(tmp_path)
    monkeypatch.setattr(mabar, "APPDATA", tmp_path)
    helper = tmp_path / "pkg" / "helper"
    helper.mkdir(parents=True)
    (helper / "ArtiHostHelper.dll").write_bytes(b"dll")
    (helper / "manifest.json").write_text(json.dumps({"Name": "ArtiHostHelper", "UniqueID": "arti.hosthelper", "EntryDll": "ArtiHostHelper.dll"}), encoding="utf-8")
    import hashlib
    booking = dict(helper=str(helper), helperSha256=hashlib.sha256(b"dll").hexdigest())
    run = tmp_path / "mabar-20261115"
    (run / "runtime/batch-host/host-mods").mkdir(parents=True)
    monkeypatch.setattr(mabar, "PREP", tmp_path / "prep")
    (tmp_path / "prep").mkdir()
    (run / "trial-config.json").write_text(json.dumps({"stardew_host_controller": "owner"}), encoding="utf-8")
    mabar.install_host_helper(run, booking, "farmA_100000001")
    cfg = json.loads((run / "runtime/batch-host/host-mods/ArtiHostHelper/config.json").read_text(encoding="utf-8"))
    assert cfg["SaveFolder"] == "farmA_100000001" and cfg["GiftTimes"] == []
    mabar.set_host_controller(run, "harness")
    assert json.loads((run / "trial-config.json").read_text())["stardew_host_controller"] == "harness"
    mabar.remove_host_helper(run)
    assert not (run / "runtime/batch-host/host-mods/ArtiHostHelper").exists()


def test_release_is_a_noop_without_a_gated_helper(tmp_path):
    assert mabar.release_helper_pause(tmp_path) is None
    cfg = tmp_path / "runtime/batch-host/host-mods/ArtiHostHelper"
    cfg.mkdir(parents=True)
    (cfg / "config.json").write_text(json.dumps({"StartupPauseSeconds": 0, "RunId": "mabar-x0000000"}), encoding="utf-8")
    assert mabar.release_helper_pause(tmp_path) is None


def test_release_publishes_the_run_id_like_soak(tmp_path, monkeypatch):
    cfg = tmp_path / "runtime/batch-host/host-mods/ArtiHostHelper"
    cfg.mkdir(parents=True)
    (cfg / "config.json").write_text(json.dumps({"StartupPauseSeconds": 300, "RunId": "mabar-20261115b"}), encoding="utf-8")
    (tmp_path / "host").mkdir()
    (tmp_path / "host/startup.json").write_text(json.dumps({"state": "held", "time": 600}), encoding="utf-8")
    seen = {}
    monkeypatch.setattr(mabar.soak, "startup_pause", lambda out, run_id, **kw: seen.update(out=out, run_id=run_id, **kw) or {"state": "released", "time": 600})
    monkeypatch.setattr(mabar, "log", lambda *a, **k: None)
    assert mabar.release_helper_pause(tmp_path)["state"] == "released"
    assert seen["run_id"] == "mabar-20261115b" and seen["release"] is True and seen["out"] == tmp_path / "host"


def test_release_skips_a_gate_that_is_not_held(tmp_path, monkeypatch):
    """11-15 14:08: the gate was already released on this HOST; the rejoin must not fail on it."""
    cfg = tmp_path / "runtime/batch-host/host-mods/ArtiHostHelper"
    cfg.mkdir(parents=True)
    (cfg / "config.json").write_text(json.dumps({"StartupPauseSeconds": 300, "RunId": "mabar-20261115b"}), encoding="utf-8")
    (tmp_path / "host").mkdir()
    (tmp_path / "host/startup.json").write_text(json.dumps({"state": "released", "time": 1240}), encoding="utf-8")
    called = []
    monkeypatch.setattr(mabar.soak, "startup_pause", lambda *a, **k: called.append(1))
    monkeypatch.setattr(mabar, "log", lambda *a, **k: None)
    assert mabar.release_helper_pause(tmp_path) is None and called == []


def test_voice_command_refuses_a_stop_from_its_pid_probe(tmp_path, monkeypatch):
    marker = tmp_path / "bridge-drained"
    monkeypatch.setattr(mabar, "inbox", lambda: tmp_path / "command")
    monkeypatch.setattr(mabar, "preflight", lambda *a, **k: 0)

    def bridge_pids():
        marker.touch()
        return []

    monkeypatch.setattr(mabar, "bridge_pids", bridge_pids)
    assert mabar.voice(argparse.Namespace(run=tmp_path)) == 1
    assert marker.is_file() and not list(tmp_path.glob("bridge-drained.*"))


def test_mabar_config_carries_the_solo_chat_tuning(tmp_path):
    cfg = mabar.mabar_bridge_config(tmp_path / "r", vts=False, mic=1, live={})
    assert cfg["yt_viewer_cooldown_sec"] == 15.0 and cfg["codex_thread_max_turns"] == 6 and cfg["curious_interval_sec"] == 180
