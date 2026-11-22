"""a48 T89: rain made the soil wet, and her crop line said she had watered it."""
from scripts.live7.live7_common import _crop_line


def test_a_wet_crop_line_does_not_claim_her_watering():
    today = {"year": 1, "season": "spring", "day": 12}
    record = {"state": "growing", "planted_date": {"year": 1, "season": "spring", "day": 5},
              "last_seen_date": today, "watered_date": today, "phase": 1}
    line = _crop_line("Farm 53,17 Parsnip", record, today)
    assert "basah hari ini" in line
    assert "sudah disiram" not in line


def test_a_dry_crop_line_is_unchanged():
    today = {"year": 1, "season": "spring", "day": 12}
    record = {"state": "growing", "planted_date": {"year": 1, "season": "spring", "day": 5},
              "last_seen_date": today, "phase": 1}
    assert "belum tentu disiram hari ini" in _crop_line("Farm 53,17 Parsnip", record, today)
