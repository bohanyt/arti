"""A disposable rehearsal copy of a backed-up save: another farm to the game, the source untouched."""
import datetime
import json

import pytest

from scripts.livepath import rehearsal_copy as R

SAVE = "Kebun Uji_400000001"
MAIN = ('\ufeff<?xml version="1.0" encoding="utf-8"?><SaveGame><player><name>a</name><farmName>Kebun Uji</farmName>'
        "</player><farmhands><Farmer><name>b</name><farmName>Kebun Uji</farmName></Farmer></farmhands>"
        "<dailyLuck>0</dailyLuck><uniqueIDForThisGame>400000001</uniqueIDForThisGame></SaveGame>").encode()
INFO = ('\ufeff<?xml version="1.0" encoding="utf-8"?><Farmer><name>a</name><farmName>Kebun Uji</farmName>'
        "</Farmer>").encode()


@pytest.fixture
def world(tmp_path):
    game = tmp_path / "appdata" / "StardewValley"
    saves = game / "Saves"
    (saves / SAVE).mkdir(parents=True)
    (saves / SAVE / SAVE).write_bytes(MAIN)
    backup = tmp_path / "backups" / "mabar-1"
    folder = backup / "Saves" / SAVE
    folder.mkdir(parents=True)
    files = {SAVE: MAIN, "SaveGameInfo": INFO, SAVE + "_old": b"old"}
    for name, data in files.items():
        (folder / name).write_bytes(data)
    (backup / "MANIFEST.json").write_text(json.dumps(
        {"sha256": {"Saves/" + SAVE: {k: R.sha(v) for k, v in files.items()}}}), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    return backup, saves, out


def make(world, **kw):
    backup, saves, out = world
    args = dict(saves=saves, processes=lambda: {"explorer.exe"}, unique_id=500000002)
    args.update(kw)
    return R.make(args.pop("backup", backup), args.pop("save", SAVE), args.pop("name", "latihan"),
                  args.pop("out", out), **args)


def tree(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_the_copy_is_another_farm_and_differs_in_name_and_id_only(world):
    backup, saves, out = world
    before = tree(backup), tree(saves)
    receipt = make(world)
    folder = out / "latihan_500000002"
    assert receipt["folder"] == str(folder) and receipt["save"] == "latihan_500000002"
    assert sorted(p.name for p in folder.iterdir()) == ["SaveGameInfo", "latihan_500000002"]
    main = (folder / "latihan_500000002").read_bytes()
    assert main.replace(b">latihan<", b">Kebun Uji<").replace(b"500000002", b"400000001") == MAIN
    assert (folder / "SaveGameInfo").read_bytes().replace(b">latihan<", b">Kebun Uji<") == INFO
    assert receipt["sha256"]["latihan_500000002"] == R.sha(main)
    assert (tree(backup), tree(saves)) == before
    assert not [p for p in out.parent.iterdir() if p.name.startswith(".rehearsal-copy-")]


def test_place_puts_it_beside_the_real_save_and_changes_nothing_else(world):
    _, saves, _ = world
    before = tree(saves)
    receipt = make(world, place=True, out=None)
    assert receipt["folder"] == str(saves / "latihan_500000002")
    after = tree(saves)
    assert {k: v for k, v in after.items() if not k.startswith("latihan_")} == before


@pytest.mark.parametrize("process", ("StardewModdingAPI.exe", "Stardew Valley.exe"))
def test_place_refuses_while_a_game_runs(world, process):
    _, saves, _ = world
    with pytest.raises(ValueError, match="close the games"):
        make(world, place=True, out=None, processes=lambda: {process})
    assert [p.name for p in saves.iterdir()] == [SAVE]


def test_the_source_is_a_backup_and_equal_to_its_manifest(world):
    backup, saves, out = world
    with pytest.raises(ValueError, match="not the game's own"):
        make(world, backup=saves.parent)
    (backup / "Saves" / SAVE / "SaveGameInfo").write_bytes(INFO + b" ")
    with pytest.raises(ValueError, match="differs from its manifest"):
        make(world)
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("save", ("../x_1", "noid", "a/b_1", "other_400000009"))
def test_a_save_the_backup_does_not_hold_is_refused(world, save):
    with pytest.raises(ValueError):
        make(world, save=save)


@pytest.mark.parametrize("name", ("", "Latihan", "a b", "a" * 21, "x_1", "..", "kebun/uji"))
def test_the_new_name_is_plain(world, name):
    with pytest.raises(ValueError):
        make(world, name=name)


def test_never_the_game_folder_without_place_and_never_over_an_existing_save(world):
    _, saves, out = world
    with pytest.raises(ValueError, match="only with --place"):
        make(world, out=saves)
    with pytest.raises(ValueError, match="does not exist"):
        make(world, out=out / "missing")
    make(world)
    with pytest.raises(ValueError, match="exists already"):
        make(world)
    with pytest.raises(ValueError, match="exists already"):
        make(world, name="lain")  # another name, the same ID
    with pytest.raises(ValueError, match="another ID"):
        make(world, unique_id=400000001)


@pytest.mark.parametrize("empty", (b"<farmName />", b"<farmName></farmName>", b""))
def test_a_cabin_nobody_has_taken_stays_without_a_farm_name(world, empty):
    backup, saves, out = world
    data = MAIN.replace(b"</farmhands>", b"<Farmer><name></name>" + empty + b"</Farmer></farmhands>")
    (backup / "Saves" / SAVE / SAVE).write_bytes(data)
    (backup / "MANIFEST.json").write_text(json.dumps(
        {"sha256": {"Saves/" + SAVE: {SAVE: R.sha(data), "SaveGameInfo": R.sha(INFO)}}}), encoding="utf-8")
    make(world)
    copy = (out / "latihan_500000002" / "latihan_500000002").read_bytes()
    assert copy.count(b"<farmName>latihan</farmName>") == 2 and (not empty or empty in copy)


def test_place_reads_the_running_games_itself(world, monkeypatch):
    from scripts.livepath import mabar
    monkeypatch.setattr(mabar, "processes", lambda: {"stardewmoddingapi.exe"})
    with pytest.raises(ValueError, match="close the games"):
        make(world, place=True, out=None, processes=None)


@pytest.mark.parametrize("damage", (
    lambda m: m.replace(b"<uniqueIDForThisGame>400000001</uniqueIDForThisGame>", b""),
    lambda m: m.replace(b"</SaveGame>", b"<uniqueIDForThisGame>400000001</uniqueIDForThisGame></SaveGame>"),
    lambda m: m.replace(b"400000001", b"400000003"),
    lambda m: m.replace(b"<name>b</name><farmName>Kebun Uji", b"<name>b</name><farmName>Lain"),
))
def test_a_save_whose_identity_is_not_plain_is_refused(world, damage):
    backup, saves, out = world
    data = damage(MAIN)
    (backup / "Saves" / SAVE / SAVE).write_bytes(data)
    (backup / "MANIFEST.json").write_text(json.dumps(
        {"sha256": {"Saves/" + SAVE: {SAVE: R.sha(data), "SaveGameInfo": R.sha(INFO)}}}), encoding="utf-8")
    with pytest.raises(ValueError):
        make(world)
    assert list(out.iterdir()) == []


def test_a_failed_write_leaves_nothing_behind(world, monkeypatch):
    _, _, out = world

    def refuse(*_a):
        raise OSError("no")

    monkeypatch.setattr(R.os, "rename", refuse)
    with pytest.raises(OSError):
        make(world)
    assert list(out.iterdir()) == []
    assert not [p for p in out.parent.iterdir() if p.name.startswith(".rehearsal-copy-")]


def test_the_new_id_counts_like_the_game():
    assert R.new_id(datetime.datetime(2012, 6, 22, 0, 1, tzinfo=datetime.timezone.utc)) == 60


def test_the_command_line_names_one_destination(world, monkeypatch, capsys):
    backup, saves, out = world
    monkeypatch.setattr(R, "saves_root", lambda: saves)
    R.main(["--backup", str(backup), "--save", SAVE, "--name", "latihan", "--out", str(out)])
    assert json.loads(capsys.readouterr().out)["source"] == SAVE
    with pytest.raises(SystemExit):
        R.main(["--backup", str(backup), "--save", SAVE, "--name", "x"])
    with pytest.raises(SystemExit):
        R.main(["--backup", str(backup), "--save", SAVE, "--name", "x", "--out", str(out), "--place"])
