"""Synthetic frames only; these tests never launch a game or OBS."""
import importlib.util
import binascii
import shutil
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("s3_cadence", Path(__file__).resolve().parents[1] / "scripts/stardew_s3_cadence.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)


def raster(packet, cell=8):
    return bytes(255 if (packet >> (63 - ((y // cell) * 8 + x // cell))) & 1 else 0
                 for y in range(cell * 8) for x in range(cell * 8))


def test_marker_golden_and_corruption():
    # CRC16-CCITT golden packet for D3 / nonce=0x1234 / draw=1.
    packet = 0xD31234000001D2A4  # independently checked with binascii.crc_hqx
    assert s3.decode_marker(raster(packet)) == (0x1234, 1)
    assert s3.decode_marker(raster(packet ^ (1 << 20))) is None
    assert s3.decode_marker(bytes([128]) * 4096) is None


def test_60_encoded_fps_with_30_unique_is_not_60():
    frames = [(i / 60, (7, i // 2 + 1)) for i in range(120)]
    result = s3.delivered_summary(frames, 7, set(range(1, 61)))
    assert result["encoded_cadence_in_span"]["rate_hz"] == pytest.approx(60)
    assert result["unique_delivered_cadence"]["rate_hz"] == pytest.approx(30)
    assert result["duplicate_frames"] == 60


def test_frozen_capture_has_one_unique_frame_and_no_rate():
    result = s3.delivered_summary([(i / 60, (7, 8)) for i in range(120)], 7, {8})
    assert result["unique_delivered_cadence"]["count"] == 1
    assert result["unique_delivered_cadence"]["rate_hz"] is None
    assert result["duplicate_frames"] == 119


def test_invalid_wrong_and_unwitnessed_markers_are_disclosed():
    frames = [(0, (7, 1)), (.1, None), (.2, (8, 2)), (.3, (7, 99)), (.4, (7, 3))]
    result = s3.delivered_summary(frames, 7, {1, 2, 3})
    assert not result["identity_and_marker_valid"]
    assert result["invalid_marker_frames_inside_span"] == 1
    assert result["wrong_nonce_frames_inside_span"] == 1
    assert result["unwitnessed_marker_frames"] == 1
    assert result["observed_draw_ids_not_delivered"] == 1


@pytest.mark.parametrize("frames", [[(0, (8, 1))], [(0, (7, 2)), (.1, (7, 1))], [(0, (7, 1)), (0, (7, 2))]])
def test_wrong_target_regression_and_bad_pts_fail(frames):
    with pytest.raises(ValueError):
        s3.delivered_summary(frames, 7, {1, 2})


def scene(kind="game_capture", exe="StardewModdingAPI.exe", extra=False):
    return {"current_scene": "S3", "sources": [
        {"name": "ARTI", "id": kind, "settings": {"capture_mode": "window", "priority": 2, "window": f"Stardew Valley:SDL_app:{exe}"}},
        {"name": "S3", "id": "scene", "settings": {"items": [{"name": "ARTI", "visible": True}] + ([{"name": "Desktop", "visible": True}] if extra else [])}}]}


def test_specific_game_capture_configuration():
    assert s3.check_game_capture(scene(), "ARTI", r"C:\game\StardewModdingAPI.exe")["configuration_only"]


@pytest.mark.parametrize("bad", [scene(kind="monitor_capture"), scene(exe="Stardew Valley.exe"), scene(extra=True)])
def test_display_capture_host_and_composite_are_rejected(bad):
    with pytest.raises(ValueError):
        s3.check_game_capture(bad, "ARTI", "StardewModdingAPI.exe")


@pytest.mark.parametrize("priority", [0, 1])
def test_shared_class_or_title_cannot_silently_bind_host(priority):
    bad = scene()
    bad["sources"][0]["settings"]["priority"] = priority
    with pytest.raises(ValueError):
        s3.check_game_capture(bad, "ARTI", "StardewModdingAPI.exe")


def test_observer_update_draw_present_remain_separate():
    records = [{"kind": "identity", "qpc": 0, "frequency": 600, "nonce": 7, "hwnd": 9}]
    for i in range(60):
        for kind in ["update"] + (["draw", "present"] if i % 2 == 0 else []):
            records.append({"kind": kind, "qpc": i * 10, "worldTicks": i, "hwnd": 9, "foreground": 8, "active": False, "minimized": False})
    records.append({"kind": "stop", "qpc": 600, "reason": "wall_deadline"})
    result = s3.observer_summary(records)
    assert result["update"]["rate_hz"] == pytest.approx(60)
    assert result["draw"]["rate_hz"] == pytest.approx(30)
    assert result["present"]["rate_hz"] == pytest.approx(30)
    records[1]["active"] = True
    with pytest.raises(ValueError):
        s3.observer_summary(records)


def test_encoded_synthetic_video_keeps_duplicates_visible(tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("optional local FFmpeg tools absent")
    frames = []
    for i in range(120):
        payload = (0xD3 << 40) | (7 << 24) | (i // 2 + 1)
        crc = binascii.crc_hqx(payload.to_bytes(6, "big"), 0xFFFF)
        frames.append(raster((payload << 16) | crc))
    video = tmp_path / "synthetic.mkv"
    subprocess.run([ffmpeg, "-v", "error", "-f", "rawvideo", "-pixel_format", "gray", "-video_size", "64x64",
                    "-framerate", "60", "-i", "pipe:0", "-c:v", "ffv1", str(video)],
                   input=b"".join(frames), check=True, capture_output=True, timeout=30)
    decoded = s3.video_frames(str(video), ffmpeg, ffprobe, 0, 0, 8)
    report = s3.delivered_summary(decoded, 7, set(range(1, 61)))
    assert report["duplicate_frames"] == 60
    assert report["unique_delivered_cadence"]["rate_hz"] == pytest.approx(30, abs=.05)
    assert report["identity_and_marker_valid"]
