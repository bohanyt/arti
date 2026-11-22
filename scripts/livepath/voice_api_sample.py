"""Offline latency sample for the raw OpenAI voice arm.

Bridge reconstruction (prepare_turn_context + RAG + history + live facts)
imports arti_bridge and is too entangled here. System is the soul file;
user turns are the 12 owner/chat/game fixture lines.

Prints numbers and model names only. Hard cap 1.00 USD.

  venv/Scripts/python.exe -B scripts/livepath/voice_api_sample.py --out-dir .tmp/voice-api-sample
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arti_runtime.openai_voice import SCRIPT_CAP_USD, openai_voice_reply

DEFAULT_SOUL = ROOT / "ARTI_SOUL.md"
DEFAULT_PROMPTS = ROOT / "tests" / "fixtures" / "livepath" / "voice_api_sample_prompts.json"
EFFORTS = ("low", "medium")


def write_gen_dist(path, samples, label):
    path.write_text(
        json.dumps({"samples_sec": samples, "label": label}, indent=2) + "\n",
        encoding="utf-8")


def load_lines(path, n):
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows = payload.get("lines") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError("prompts")
    out = []
    for row in rows[:n]:
        if not isinstance(row, dict):
            raise ValueError("prompts")
        kind, text = row.get("kind"), row.get("text")
        if not isinstance(kind, str) or not isinstance(text, str) or not text.strip():
            raise ValueError("prompts")
        out.append((kind.strip(), text.strip()))
    if not out:
        raise ValueError("prompts")
    return out


def _cfg(out_dir, model, tier):
    return dict(
        voice_api_model=model,
        voice_api_service_tier=tier,
        voice_api_session_cap_usd=str(SCRIPT_CAP_USD),
        voice_api_session_state_file=str(out_dir / "session.json"),
    )


async def run_effort(system, lines, effort, cfg):
    samples, rows, stopped = [], [], None
    for index, (kind, text) in enumerate(lines):
        reply, meta = await openai_voice_reply(system, f"{kind}: {text}", dict(cfg, voice_api_effort=effort))
        if (meta or {}).get("outcome") == "usd_cap":
            stopped = "usd_cap"
            break
        latency = (meta or {}).get("latency_sec")
        if reply is None or not isinstance(latency, (int, float)):
            continue
        samples.append(float(latency))
        rows.append(dict(
            effort=effort, index=index, kind=kind, latency_sec=float(latency),
            input_tokens=(meta or {}).get("input_tokens"),
            cached_input_tokens=(meta or {}).get("cached_input_tokens"),
            output_tokens=(meta or {}).get("output_tokens"),
            cost_usd=(meta or {}).get("cost_usd"),
            model=(meta or {}).get("model")))
    return samples, rows, stopped


async def sample(system, lines, cfg):
    all_rows, files, stopped = [], {}, None
    model = cfg.get("voice_api_model", "gpt-6-luna")
    for effort in EFFORTS:
        samples, rows, stop = await run_effort(system, lines, effort, cfg)
        all_rows.extend(rows)
        files[effort] = samples
        if stop:
            stopped = stop
            break
    return model, files, all_rows, stopped


def _print(model, files, rows, stopped):
    print(model)
    spent = 0.0
    for effort in EFFORTS:
        samples = files.get(effort) or []
        part = sum(r["cost_usd"] or 0 for r in rows if r["effort"] == effort)
        spent += part
        print("%s n=%d usd=%.6f" % (effort, len(samples), part))
        if samples:
            print(" ".join("%.3f" % s for s in samples))
    print("total_usd=%.6f cap_usd=%.2f" % (spent, float(SCRIPT_CAP_USD)))
    if stopped:
        print("stopped %s" % stopped)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--n", type=int, default=12)
    parser.add_argument("--soul", type=Path, default=DEFAULT_SOUL)
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--tier", default="default", choices=("default", "priority"))
    args = parser.parse_args(argv)
    if args.n < 1:
        print("error ValueError")
        return 2
    try:
        system = args.soul.read_text(encoding="utf-8-sig")
        lines = load_lines(args.prompts, args.n)
    except FileNotFoundError:
        print("error FileNotFoundError")
        return 2
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print("error %s" % type(exc).__name__)
        return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(args.out_dir, args.model, args.tier)
    model, files, rows, stopped = asyncio.run(sample(system, lines, cfg))
    for effort, samples in files.items():
        if samples:
            write_gen_dist(args.out_dir / ("api-%s-%s.json" % (args.model, effort)),
                           samples, "api-%s-%s" % (args.model, effort))
    (args.out_dir / "token_cost_summary.json").write_text(
        json.dumps(dict(model=model, cap_usd=float(SCRIPT_CAP_USD), replies=rows), indent=2) + "\n",
        encoding="utf-8")
    _print(model, files, rows, stopped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
