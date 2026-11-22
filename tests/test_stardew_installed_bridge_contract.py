"""Source guards for the two installed-build defects (ModEntry is not SMAPI-free)."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "stardew-mod/ArtiStardewBridge/ModEntry.cs"


def source():
    # Comments must not satisfy executable ordering/clock guards.
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", ENTRY.read_text(encoding="utf-8"), flags=re.S)


def test_native_capture_tick_widens_into_existing_long_contract():
    entry = source()
    assert re.search(r"void\s+AdvanceUiTextNative\(\s*ScreenState screen,\s*long ticks,", entry)
    assert re.search(r"this\.AdvanceUiTextNative\(\s*screen,\s*Game1\.ticks,", entry)
    assert re.search(
        r"UiTextTickSample\.FromCapturedMenu\(\s*menu,\s*menuInstanceToken,\s*ticks,", entry
    )
    contract = (ROOT / "stardew-mod/ArtiStardewBridge/Protocol/UiTextObservation.cs").read_text()
    assert "public long CaptureTicks" in contract
    assert re.search(r"FromCapturedMenu\(\s*MenuInfo\? menu,\s*string\? menuInstanceToken,\s*long captureTicks,", contract)


def test_options_mapping_is_gated_without_skipping_owned_cleanup():
    entry = source()
    tick = entry.split("void TickUseToolNativeAdapter()", 1)[1].split("void ApplyUseToolNativePlan", 1)[0]
    eligibility = re.search(
        r"bool worldReady = Context\.IsWorldReady\s*&& Game1\.game1 is not null\s*&& Game1\.game1\.IsActive;", tick
    )
    gated_mapping = re.search(
        r"if\s*\(worldReady\)\s*\{\s*mapped = TryMapFirstConfiguredUseToolButton\(out mappedButton, out fingerprint\);\s*\}", tick
    )
    assert eligibility and gated_mapping and eligibility.end() < gated_mapping.start()
    assert "bool mapped = false;" in tick[:gated_mapping.start()]
    assert "SButton mappedButton = SButton.None;" in tick[:gated_mapping.start()]
    assert 'string fingerprint = "";' in tick[:gated_mapping.start()]
    assert tick.count("TryMapFirstConfiguredUseToolButton(") == 1
    assert "Game1.options" not in tick
    # False eligibility still reaches the same adapter/backend for original-pin cleanup.
    assert "return;" not in tick
    assert gated_mapping.end() < tick.index("this.useToolAdapter.Tick(")
    assert "mapped ? mappedButton.ToString() : null" in tick
    assert "mapped ? fingerprint : null" in tick
