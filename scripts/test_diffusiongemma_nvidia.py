#!/usr/bin/env python3
"""POC: benchmark NVIDIA DiffusionGemma vs Groq on Arti-style ID prompts."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests

import arti_nvidia_client as nvidia

ARTI_PROMPTS = [
    "Tes RT kamu denger gak?",
    "Eh Arti, gimana kabarmu hari ini?",
    "Kak, jelasin singkat apa itu VTuber dalam 2 kalimat.",
    "Streamer lagi bingung nih — kasih semangat dong!",
    "Arti, kenapa langit biru? Jawab imut ya.",
]

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def groq_chat(prompt: str, api_key: str, model: str) -> tuple[str, int]:
    t0 = time.perf_counter()
    res = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "Kamu Arti, VTuber AI imut berbahasa Indonesia."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 150,
            "temperature": 1.0,
        },
        timeout=30,
    )
    ms = int((time.perf_counter() - t0) * 1000)
    if res.status_code != 200:
        raise RuntimeError(f"Groq HTTP {res.status_code}: {res.text[:300]}")
    text = res.json()["choices"][0]["message"]["content"]
    return str(text).strip(), ms


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark NVIDIA DiffusionGemma vs Groq")
    parser.add_argument("--groq-model", default="qwen/qwen3-32b")
    parser.add_argument("--skip-groq", action="store_true")
    args = parser.parse_args()

    nv_key = nvidia.resolve_api_key()
    groq_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not nv_key:
        print("[Error] Set NVIDIA_API_KEY or nvidia_api_key in CONFIG")
        return 1

    print(f"NVIDIA model: {nvidia.DEFAULT_NVIDIA_MODEL}")
    print(f"Prompts: {len(ARTI_PROMPTS)}\n")

    for i, prompt in enumerate(ARTI_PROMPTS, 1):
        print(f"--- Prompt {i}: {prompt[:60]}...")
        messages = [
            {"role": "system", "content": "Kamu Arti, VTuber AI imut berbahasa Indonesia."},
            {"role": "user", "content": prompt},
        ]
        try:
            reply, llm_ms = nvidia.chat_completion(
                messages, config={"nvidia_api_key": nv_key}
            )
            print(f"  NVIDIA llm_ms={llm_ms}")
            print(f"  Reply: {reply[:120]}...")
        except Exception as e:
            print(f"  NVIDIA error: {e}")

        if not args.skip_groq and groq_key:
            try:
                reply, llm_ms = groq_chat(prompt, groq_key, args.groq_model)
                print(f"  Groq llm_ms={llm_ms}")
                print(f"  Reply: {reply[:120]}...")
            except Exception as e:
                print(f"  Groq error: {e}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
