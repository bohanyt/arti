"""OBS-2B1: bukti anggaran 64 KiB dari serializer C# sungguhan.

Angka byte di kontrak dihasilkan ProtocolTests (ProtocolJson.Options),
bukan dihitung ulang di Python. Tes ini mengunci: limit tidak naik,
newline CRLF dihitung, dan observed max muat di MAX_LINE_BYTES.
"""

from __future__ import annotations

import json
from pathlib import Path

from arti_stardew import MAX_LINE_BYTES
from arti_stardew_scene_delta import SCENE_DELTA_MAX_PATCH_OPS

ROOT = Path(__file__).resolve().parents[1]
KONTRAK = ROOT / "tests" / "data" / "stardew_wire_size_budget.json"
RECEIVER = ROOT / "arti_stardew.py"
CS_DELTA = ROOT / "stardew-mod" / "ArtiStardewBridge" / "Protocol" / "SceneDeltaLogic.cs"
CS_WIRE = ROOT / "stardew-mod" / "ArtiStardewBridge" / "Protocol" / "SceneSnapshotLogic.cs"
CS_CLIENT = ROOT / "stardew-mod" / "ArtiStardewBridge" / "Protocol" / "BridgeClient.cs"


def test_max_line_bytes_tidak_naik():
    assert MAX_LINE_BYTES == 64 * 1024
    assert SCENE_DELTA_MAX_PATCH_OPS == 48
    receiver = RECEIVER.read_text(encoding="utf-8")
    assert "MAX_LINE_BYTES = 64 * 1024" in receiver
    delta = CS_DELTA.read_text(encoding="utf-8")
    assert "MaxPatchOps = 48" in delta
    assert "ReceiverMaxLineBytes = SceneObservationCaps.ReceiverMaxLineBytes" in delta


def test_kontrak_wire_size_muat_termasuk_crlf():
    raw = json.loads(KONTRAK.read_text(encoding="utf-8"))
    assert raw["receiverMaxLineBytes"] == MAX_LINE_BYTES
    assert raw["maxPatchOps"] == SCENE_DELTA_MAX_PATCH_OPS
    assert raw["wireNewlineBytesCounted"] == 2
    assert raw["designGap"] is None
    assert raw["observedMaxJsonBytes"] < MAX_LINE_BYTES
    assert raw["observedMaxNdjsonCrlfBytes"] <= MAX_LINE_BYTES
    assert raw["observedMaxNdjsonCrlfBytes"] == raw["observedMaxJsonBytes"] + 2

    cases = raw["cases"]
    for name in (
        "snapshot_unicode",
        "snapshot_escape",
        "snapshot_mix_escape",
        "delta_unicode_48",
        "delta_escape_48",
        "delta_mix_escape_48",
    ):
        case = cases[name]
        assert case["jsonBytes"] < MAX_LINE_BYTES, name
        assert case["ndjsonCrlfBytes"] <= MAX_LINE_BYTES, name
        assert case["ndjsonCrlfBytes"] == case["jsonBytes"] + 2, name
        assert case["truncated"] is False, name

    assert cases["delta_unicode_48"]["kind"] == "delta"
    assert cases["snapshot_unicode"]["kind"] == "snapshot"
    assert raw["snapshotFamilyCaps"] == {
        "warps": 16,
        "doors": 16,
        "objects": 40,
        "terrain": 40,
        "npcs": 16,
        "monsters": 16,
        "clumps": 16,
    }


def test_fits_receiver_dan_bound_masih_protocoljson():
    delta = CS_DELTA.read_text(encoding="utf-8")
    assert "ProtocolJson.Options" in delta
    assert "long.MaxValue, long.MaxValue, int.MaxValue" in delta
    assert "GetByteCount(json) < SceneDeltaCaps.ReceiverMaxLineBytes" in delta
    wire = CS_WIRE.read_text(encoding="utf-8")
    assert "SerializedUtf8Bytes" in wire
    assert "JsonSerializer.Serialize(value, ProtocolJson.Options)" in wire
    client = CS_CLIENT.read_text(encoding="utf-8")
    assert "WriteLineAsync(line)" in client
    assert "NewLine" not in client
