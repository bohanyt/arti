"""PART_B_MINES_PLAN: `stage_save.py edit --set farm_clear=X,Y;...` on a small synthetic save (BOM + CRLF).

Offline only: temp files, injected process facts. Staging is never acceptance evidence."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from scripts.m1_operator import stage_save as stage

XSI = 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'


def tf(x, y, body):
    return (f"<item><key><Vector2><X>{x}</X><Y>{y}</Y></Vector2></key><value>{body}</value></item>")


def tree(x, y, stage_=4, kind="Tree"):
    return tf(x, y, f'<TerrainFeature xsi:type="{kind}"><growthStage>{stage_}</growthStage><treeType>2</treeType>'
                    f"<health>10</health><stump>false</stump></TerrainFeature>")


def obj(x, y, name, item_id, type_="Litter", big="false", xsi_type=None):
    attr = f' xsi:type="{xsi_type}"' if xsi_type else ""
    return tf(x, y, f"<Object{attr}><name>{name}</name><itemId>{item_id}</itemId><type>{type_}</type>"
                    f"<bigCraftable>{big}</bigCraftable></Object>")


TERRAIN = [tree(40, 7), tree(41, 7), tree(40, 8, 15), tf(43, 9, '<TerrainFeature xsi:type="Grass" />'),
           tf(50, 50, '<TerrainFeature xsi:type="HoeDirt"><crop /></TerrainFeature>'), tree(51, 51, 4, "FruitTree"),
           tree(38, 9)]
OBJECTS = [obj(39, 8, "Twig", "294"), obj(42, 8, "Twig", "295"), obj(45, 12, "Chest", "130", "Crafting", "true", "Chest"),
           obj(46, 12, "Fence", "322", "Crafting", "false", "Fence"), obj(43, 10, "Preserves Jar", "15", "Crafting", "true"),
           obj(36, 12, "Stone", "343")]


def make_save(terrain=TERRAIN, objects=OBJECTS):
    farm = ("<GameLocation><name>Farm</name>"
            "<buildings><Building><tileX>10</tileX><tileY>10</tileY><tilesWide>4</tilesWide><tilesHigh>3</tilesHigh>"
            "<buildingType>Coop</buildingType></Building></buildings>"
            f"<objects>{''.join(objects)}</objects>"
            "<resourceClumps><ResourceClump><width>2</width><height>2</height><parentSheetIndex>600</parentSheetIndex>"
            "<tile><X>20</X><Y>20</Y></tile></ResourceClump></resourceClumps>"
            '<largeTerrainFeatures><LargeTerrainFeature xsi:type="Bush"><size>2</size><tilePosition><X>30</X><Y>5</Y>'
            "</tilePosition></LargeTerrainFeature></largeTerrainFeatures>"
            f"<terrainFeatures>{''.join(terrain)}</terrainFeatures></GameLocation>")
    town = f"<GameLocation><name>Town</name><objects /><terrainFeatures>{tree(40, 7)}</terrainFeatures></GameLocation>"
    text = (f'<?xml version="1.0" encoding="utf-8"?>\r\n<SaveGame {XSI}><player><name>A</name></player>'
            f"<locations>{farm}{town}</locations><year>6</year></SaveGame>")
    return stage.BOM + text.encode("utf-8")


RAW = make_save()
CORRIDOR = ((40, 7), (41, 7), (40, 8), (39, 8))


def test_corridor_tiles_remove_exactly_three_trees_and_the_twig_bom_and_other_bytes_kept():
    new, rows = stage.farm_clear(RAW, CORRIDOR)
    assert [(r["domain"], r["kind"], tuple(r["tile"])) for r in rows] == [
        ("terrainFeatures", "Tree", (40, 7)), ("terrainFeatures", "Tree", (41, 7)),
        ("terrainFeatures", "Tree", (40, 8)), ("objects", "Twig", (39, 8))]
    assert new.startswith(stage.BOM) and new.count(b"\r\n") == RAW.count(b"\r\n")
    # Independent whole-byte oracle: the source minus exactly those four <item> spans.
    expected = RAW
    for chunk in (tree(40, 7), tree(41, 7), tree(40, 8, 15), obj(39, 8, "Twig", "294")):
        assert expected.count(chunk.encode()) == (2 if chunk == tree(40, 7) else 1)
        expected = expected.replace(chunk.encode(), b"", 1)  # the Farm copy comes first; Town keeps its tree
    assert new == expected
    check = stage.farm_clear_readback(RAW, new, CORRIDOR)
    assert check["rest_identical"] and check["bom_kept"] and len(check["removed"]) == 4
    town = [n for n in ET.fromstring(new).find("locations") if n.findtext("name") == "Town"][0]
    assert town.find("terrainFeatures/item/key/Vector2/X").text == "40"
    assert stage.farm_clear(new, ((42, 8),))[1] == [dict(domain="objects", kind="Twig", tile=[42, 8], itemId="295",
                                                         type="Litter")]


@pytest.mark.parametrize("tile,why", [((45, 12), "Chest"), ((46, 12), "Fence"), ((43, 10), "Preserves Jar"),
                                       ((50, 50), "HoeDirt"), ((51, 51), "FruitTree"), ((43, 9), "Grass"),
                                       ((11, 11), "building"), ((21, 21), "resource clump"), ((32, 5), "Bush"),
                                       ((60, 60), "nothing removable")])
def test_anything_but_a_wild_tree_or_litter_refuses_the_whole_edit(tile, why):
    with pytest.raises(stage.Refused, match=why):
        stage.farm_clear(RAW, CORRIDOR + (tile,))


def test_a_chest_sharing_a_listed_tile_with_a_tree_still_refuses():
    raw = make_save(objects=OBJECTS + [obj(40, 7, "Chest", "130", "Crafting", "true", "Chest")])
    with pytest.raises(stage.Refused, match="Chest"):
        stage.farm_clear(raw, CORRIDOR)


@pytest.mark.parametrize("value", ["", "40,7;40,7", "40;7", "40,7;-1,2", "40,7:41,7", "40-41,7",
                                   ";".join(f"{x},1" for x in range(17))])
def test_tile_list_is_explicit_unique_and_bounded(value):
    with pytest.raises(stage.Refused):
        stage.parse_farm_tiles(value)
    assert stage.parse_farm_tiles("40,7; 41,7") == ((40, 7), (41, 7))


class Ops(stage.LiveOps):
    def __init__(self, running=()):
        self.running, self.writes, self.logs = running, [], []

    def game_running(self):
        return self.running

    def write_atomic(self, path, data):
        self.writes.append(Path(path))
        super().write_atomic(path, data)

    def append_jsonl(self, path, row):
        self.logs.append(row)
        super().append_jsonl(path, row)


@pytest.fixture
def save(tmp_path, monkeypatch):
    monkeypatch.setattr(stage.paths, "SAVES", tmp_path / "Saves")
    monkeypatch.setattr(stage.paths, "SAVE_NAME", "Fixture_123")
    monkeypatch.setattr(stage.paths, "PREP", tmp_path / "prep")
    monkeypatch.setattr(stage.paths, "RUN", tmp_path / "run")
    p = stage.save_file()
    p.parent.mkdir(parents=True)
    p.write_bytes(RAW)
    p.with_name("SaveGameInfo").write_bytes(b"preview untouched")
    return p


ARGS = ["edit", "--set", "farm_clear=40,7;41,7;40,8;39,8;42,8"]


@pytest.mark.parametrize("running", [[101], None])
def test_edit_refuses_while_a_game_runs_or_is_unknown(save, running):
    ops = Ops(running)
    assert stage.main(ARGS, ops) == 2 and stage.main(ARGS + ["--dry-run"], ops) == 2
    assert not ops.writes and not ops.logs and save.read_bytes() == RAW and not stage.paths.PREP.exists()


def test_edit_dry_run_then_backup_first_write_and_staging_log(save, capsys):
    ops = Ops()
    assert stage.main(ARGS + ["--dry-run"], ops) == 0
    dry = json.loads(capsys.readouterr().out)
    assert not ops.writes and not stage.paths.PREP.exists() and save.read_bytes() == RAW
    assert stage.main(ARGS, ops) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verified"] and report["changes"] == dry["changes"]
    row = report["changes"][0]
    assert row["key"] == "farm_clear" and [tuple(r["tile"]) for r in row["removed"]] == list(CORRIDOR) + [(42, 8)]
    assert row["readback"]["rest_identical"] and row["readback"]["bom_kept"]
    backup = Path(report["backup"])
    assert (backup / save.name).read_bytes() == RAW  # ALWAYS backed up before the write
    assert ops.writes == [save] and save.with_name("SaveGameInfo").read_bytes() == b"preview untouched"
    assert save.read_bytes() == stage.farm_clear(RAW, stage.parse_farm_tiles(ARGS[2].split("=", 1)[1]))[0]
    assert len(ops.logs) == 1 and ops.logs[0]["tag"] == "STAGING" and ops.logs[0]["op"] == "edit"
    # A second run finds nothing removable at the listed tiles and refuses (no silent no-op, no second write).
    ops.writes.clear()
    assert stage.main(ARGS, ops) == 2 and not ops.writes


def test_refused_tile_writes_nothing_and_makes_no_backup(save):
    ops = Ops()
    assert stage.main(["edit", "--set", "farm_clear=40,7;45,12"], ops) == 2
    assert not ops.writes and not ops.logs and save.read_bytes() == RAW and not stage.paths.PREP.exists()
