"""Guard the signed native tick boundary used by the installed mod build."""

from pathlib import Path
import re


BRIDGE = Path(__file__).resolve().parents[1] / "stardew-mod" / "ArtiStardewBridge"


def test_native_ui_text_capture_preserves_signed_ticks_without_conversion():
    entry = (BRIDGE / "ModEntry.cs").read_text(encoding="utf-8")
    observation = (BRIDGE / "Protocol" / "UiTextObservation.cs").read_text(encoding="utf-8")

    # Game1.ticks is signed int in the installed game; widen at the private boundary.
    assert re.search(
        r"private\s+void\s+AdvanceUiTextNative\s*\(\s*ScreenState\s+screen\s*,\s*long\s+ticks\s*,",
        entry,
    ), "AdvanceUiTextNative must accept signed long ticks"
    assert re.search(
        r"this\.AdvanceUiTextNative\s*\(\s*screen\s*,\s*Game1\.ticks\s*,",
        entry,
    ), "Pass the native tick directly, without a cast or unsigned conversion"

    method = entry.split("private void AdvanceUiTextNative(", 1)[1].split("\n    }", 1)[0]
    assert re.search(
        r"UiTextTickSample\.FromCapturedMenu\s*\(\s*menu\s*,\s*menuInstanceToken\s*,\s*ticks\s*,",
        method,
    ), "Forward the same ticks into the existing capture contract"
    assert re.search(r"public\s+long\s+CaptureTicks\s*\{\s*get;\s*init;\s*\}", observation)
    assert re.search(
        r"FromCapturedMenu\s*\(\s*MenuInfo\?\s+menu\s*,\s*string\?\s+menuInstanceToken\s*,\s*long\s+captureTicks\s*,",
        observation,
    )
    assert re.search(r"CaptureTicks\s*=\s*captureTicks\s*,", observation)
