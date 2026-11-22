"""One image, one audio, then filtered /v1/models names. Hard cap 1.00 USD.

Prints three status lines, then matching model ids. Never prints the key,
prompt text, or media bytes.

  venv/Scripts/python.exe -B scripts/livepath/openai_modality_probe.py
"""
from __future__ import annotations

import argparse
import base64
import io
import math
import struct
import sys
import wave
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arti_runtime.openai_voice import SCRIPT_CAP_USD, list_model_names, responses_post, voice_settings


def png_16():
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = (b"\x00" + b"\xff\x00\x00" * 16) * 16
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def wav_sine(seconds=5, rate=16000, freq=440.0):
    n = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        amp = 0.2 * 32767
        handle.writeframes(b"".join(
            struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(n)))
    return buf.getvalue()


def _cfg(state, model, tier):
    return dict(
        voice_api_model=model,
        voice_api_effort="low",
        voice_api_service_tier=tier,
        voice_api_session_cap_usd=str(SCRIPT_CAP_USD),
        voice_api_session_state_file=str(state),
        voice_api_max_output_tokens=32,
    )


def _image_body(model, tier):
    blob = base64.standard_b64encode(png_16()).decode("ascii")
    return dict(
        model=model, store=False, max_output_tokens=32,
        reasoning=dict(effort="low"), service_tier=tier,
        input=[dict(role="user", content=[
            dict(type="input_text", text="One word: color?"),
            dict(type="input_image", image_url="data:image/png;base64," + blob, detail="low"),
        ])])


def _audio_body(model, tier):
    blob = base64.standard_b64encode(wav_sine()).decode("ascii")
    return dict(
        model=model, store=False, max_output_tokens=32,
        reasoning=dict(effort="low"), service_tier=tier,
        input=[dict(role="user", content=[
            dict(type="input_text", text="One word: tone?"),
            dict(type="input_audio", input_audio=dict(data=blob, format="wav")),
        ])])


def _image_line(data, meta):
    if data is not None:
        return "image: yes (%s)" % (meta.get("response_status") or "completed")
    tag = meta.get("http_status")
    if tag is None:
        tag = meta.get("error_class") or "error"
    return "image: no (%s)" % tag


def _audio_line(data, meta, model):
    if data is not None:
        return "audio: yes (%s)" % (meta.get("model") or model)
    return "audio: no (%s)" % (meta.get("error_class") or "error")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=ROOT / ".tmp" / "openai-modality-probe-session.json")
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--tier", default="default", choices=("default", "priority"))
    args = parser.parse_args(argv)
    try:
        voice_settings(dict(voice_api_model=args.model, voice_api_effort="low",
                            voice_api_service_tier=args.tier,
                            voice_api_session_cap_usd=str(SCRIPT_CAP_USD)))
    except Exception as exc:
        print("image: no (%s)" % type(exc).__name__)
        print("audio: no (%s)" % type(exc).__name__)
        print("video-url: no (not an input type)")
        return 2
    args.state.parent.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(args.state, args.model, args.tier)
    image_data, image_meta = responses_post(_image_body(args.model, args.tier), cfg)
    audio_data, audio_meta = responses_post(_audio_body(args.model, args.tier), cfg)
    print(_image_line(image_data, image_meta))
    print(_audio_line(audio_data, audio_meta, args.model))
    print("video-url: no (not an input type)")
    names, err = list_model_names(cfg)
    if names:
        for name in names:
            print(name)
    elif err:
        print("models %s" % err)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
