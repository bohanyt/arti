"""Startup cross-check + mic monitor untuk Arti bridge."""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import requests
import sounddevice as sd


@dataclass
class CheckRow:
    name: str
    status: str  # OK | WARN | FAIL | INFO | SKIP
    detail: str


@dataclass
class HealthReport:
    rows: list[CheckRow] = field(default_factory=list)
    mic_device_id: int | None = None
    mic_device_name: str = ""

    def add(self, name: str, status: str, detail: str) -> None:
        self.rows.append(CheckRow(name, status.upper(), detail))

    def counts(self) -> tuple[int, int, int]:
        ok = warn = fail = 0
        for r in self.rows:
            if r.status == "OK":
                ok += 1
            elif r.status == "WARN":
                warn += 1
            elif r.status == "FAIL":
                fail += 1
        return ok, warn, fail


def list_input_devices(config: dict | None = None) -> list[tuple[int, str, int]]:
    """(id, name, max_input_channels) untuk semua input."""
    out: list[tuple[int, str, int]] = []
    for i, dev in enumerate(sd.query_devices()):
        ch = int(dev.get("max_input_channels") or 0)
        if ch > 0:
            out.append((i, str(dev["name"]), ch))
    return out


def is_loopback_device_name(name: str, config: dict) -> bool:
    nl = name.lower()
    patterns = [p.lower() for p in (config.get("asr_skip_device_patterns") or [])]
    return any(p in nl for p in patterns)


def sample_mic_levels(
    device_id: int | None,
    seconds: float = 2.0,
    samplerate: int = 16000,
) -> tuple[float, float]:
    """Return (rms_avg, rms_peak) dari mic selama `seconds`."""
    chunks: list[np.ndarray] = []
    block = max(1, int(samplerate * 0.1))

    def _cb(indata, frames, time_info, status):
        chunks.append(indata.copy().flatten())

    kw: dict[str, Any] = {
        "samplerate": samplerate,
        "channels": 1,
        "blocksize": block,
        "callback": _cb,
    }
    if device_id is not None:
        kw["device"] = device_id

    with sd.InputStream(**kw):
        time.sleep(max(0.5, seconds))

    if not chunks:
        return 0.0, 0.0
    flat = np.concatenate(chunks)
    if flat.size == 0:
        return 0.0, 0.0
    win = max(1, int(samplerate * 0.05))
    peaks = []
    for i in range(0, len(flat), win):
        w = flat[i : i + win]
        if w.size:
            peaks.append(float(np.sqrt(np.mean(w ** 2))))
    rms_avg = float(np.sqrt(np.mean(flat ** 2)))
    rms_peak = float(max(peaks) if peaks else rms_avg)
    return rms_avg, rms_peak


def render_vu_bar(level: float, width: int = 24) -> str:
    """Bar level mic; ngomong normal ~setengah bar."""
    scaled = max(0.0, min(1.0, level * 5.0))
    filled = int(scaled * width)
    return "█" * filled + "░" * (width - filled)


def live_mic_level_preview(
    device_id: int | None,
    seconds: float = 2.5,
    samplerate: int = 16000,
) -> tuple[float, float]:
    """Tampilkan VU bar live di terminal; return (rms_last, rms_peak)."""
    block = max(1, int(samplerate * 0.05))
    peak_holder = [0.0]
    last_rms = [0.0]

    def _cb(indata, frames, time_info, status):
        rms = float(np.sqrt(np.mean(indata.flatten() ** 2)))
        last_rms[0] = rms
        peak_holder[0] = max(peak_holder[0], rms)
        bar = render_vu_bar(rms)
        sys.stdout.write(f"\r  🔊 [{bar}] peak={peak_holder[0]:.3f}   ")
        sys.stdout.flush()

    kw: dict[str, Any] = {
        "samplerate": samplerate,
        "channels": 1,
        "blocksize": block,
        "callback": _cb,
    }
    if device_id is not None:
        kw["device"] = device_id

    with sd.InputStream(**kw):
        time.sleep(max(0.5, seconds))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return last_rms[0], peak_holder[0]


def prompt_mic_selection(
    config: dict,
    *,
    resolve_mic_fn: Callable[[dict], tuple[int | None, str]],
    ask_input: Callable[[str], str] | None = None,
) -> bool:
    """Pilih mic input + test level. Update config['asr_input_device']. Return True jika berubah."""
    if ask_input is None:
        ask_input = lambda p: input(p).strip()

    explicit_before = config.get("asr_input_device")
    cur_id, cur_name = resolve_mic_fn(config)
    devices = list_input_devices(config)
    default_in = sd.default.device[0]

    print("\n  --- MIC INPUT ---")
    if not devices:
        print("  [FAIL] Tidak ada input audio di sistem!")
        return False

    for dev_id, name, _ch in devices:
        tags: list[str] = []
        if is_loopback_device_name(name, config):
            tags.append("⚠ loopback — jangan")
        if dev_id == cur_id:
            tags.append("✓ dipakai")
        if dev_id == default_in:
            tags.append("default Windows")
        tag_s = ("  | " + " | ".join(tags)) if tags else ""
        print(f"  [#{dev_id}] {name}{tag_s}")

    cur_label = cur_id if cur_id is not None else "auto"
    raw = ask_input(f"  >> Nomor mic (Enter=keep [{cur_label}]): ")

    if raw:
        if raw.isdigit():
            pick = int(raw)
            valid = {d[0] for d in devices}
            if pick not in valid:
                print(f"  [WARN] #{pick} tidak ada — tetap '{cur_name}'")
            else:
                pick_name = next(n for i, n, _ in devices if i == pick)
                if is_loopback_device_name(pick_name, config):
                    print(
                        f"  [WARN] '{pick_name}' = loopback (Stereo Mix dll), "
                        "bukan mic fisik — PTT bisa mati!"
                    )
                config["asr_input_device"] = pick
                if pick != cur_id or explicit_before != pick:
                    print(f"  [OK] Mic -> #{pick} {pick_name}")
        else:
            print("  [WARN] Ketik nomor device (# di kiri), atau Enter untuk keep")

    test_id, test_name = resolve_mic_fn(config)
    print(f"  Test mic: {test_name}")
    print("  Ngomong 'test test' (~2.5 detik):")
    try:
        _last, peak = live_mic_level_preview(test_id, seconds=2.5)
        if peak < 0.008:
            print(
                f"  [FAIL] peak={peak:.4f} — hampir mati! "
                "Coba mic lain, cek mute Windows, atau headset colok belum."
            )
        elif peak < 0.025:
            print(
                f"  [WARN] peak={peak:.4f} — lemah. "
                "Dekatkan mic / naikkan gain; saat live ngomong lebih keras."
            )
        else:
            print(f"  [OK] Mic hidup — peak={peak:.4f}")
    except Exception as e:
        print(f"  [FAIL] Tidak bisa buka mic: {e}")

    return config.get("asr_input_device") != explicit_before


def probe_groq_api(config: dict, timeout: int = 12) -> tuple[str, str]:
    key = (config.get("groq_api_key") or "").strip()
    if not key or key == "YOUR_GROQ_API_KEY" or not key.startswith("gsk_"):
        return "SKIP", "key kosong / invalid"
    model = config.get("groq_model_fast") or "llama-3.1-8b-instant"
    if model not in (config.get("groq_models") or []):
        model = (config.get("groq_models") or [model])[0]
    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "max_tokens": 4,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=timeout,
        )
        if res.status_code == 200:
            return "OK", f"HTTP 200 ({model})"
        return "FAIL", f"HTTP {res.status_code}: {res.text[:120]}"
    except Exception as e:
        return "FAIL", f"{type(e).__name__}: {e}"


def probe_openrouter_api(config: dict, timeout: int = 15) -> tuple[str, str]:
    key = (config.get("openrouter_api_key") or "").strip()
    if not key:
        return "SKIP", "OPENROUTER_API_KEY kosong"
    model = config.get("openrouter_live_model", "poolside/laguna-xs.2:free")
    try:
        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/YOUR_USER/YOUR_REPO",
                "X-Title": "Arti Health Check",
            },
            json={
                "model": model,
                "max_tokens": 4,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=timeout,
        )
        if res.status_code == 200:
            return "OK", f"HTTP 200 ({model})"
        return "WARN", f"HTTP {res.status_code}: {res.text[:120]}"
    except Exception as e:
        return "WARN", f"{type(e).__name__}: {e}"


def run_startup_health_check(
    config: dict,
    *,
    resolve_mic_fn,
    vts=None,
    tts=None,
    hotkey_registered: bool = False,
    interactive_mic_test: bool = True,
) -> HealthReport:
    report = HealthReport()
    cfg = config

    # --- Audio inputs (visibility) ---
    devices = list_input_devices(cfg)
    if not devices:
        report.add("Mic devices", "FAIL", "Tidak ada input audio di sistem")
    else:
        lines = [f"#{i} {name}" for i, name, _ in devices[:8]]
        if len(devices) > 8:
            lines.append(f"... +{len(devices) - 8} lainnya")
        report.add("Mic devices", "INFO", " | ".join(lines))

    mic_id, mic_name = resolve_mic_fn(cfg)
    report.mic_device_id = mic_id
    report.mic_device_name = mic_name

    if is_loopback_device_name(mic_name, cfg):
        report.add(
            "Mic selected",
            "FAIL",
            f"{mic_name} = loopback (Stereo Mix), bukan mic fisik! "
            "Set asr_input_device di CONFIG.",
        )
    else:
        report.add(
            "Mic selected",
            "OK",
            f"{mic_name}" + (f" (device {mic_id})" if mic_id is not None else ""),
        )

    default_in = sd.default.device[0]
    if default_in is not None:
        def_name = sd.query_devices(default_in)["name"]
        if is_loopback_device_name(def_name, cfg) and not is_loopback_device_name(mic_name, cfg):
            report.add("Windows default", "WARN", f"Default masih '{def_name}' (bridge pakai mic lain)")

    # --- Live mic level ---
    if interactive_mic_test and mic_id is not None:
        print("\n[Health] Mic test 2 detik - boleh diam atau ngomong pelan...")
        try:
            rms_avg, rms_peak = sample_mic_levels(mic_id, seconds=2.0)
            if rms_peak < 0.008:
                report.add(
                    "Mic level",
                    "FAIL",
                    f"peak={rms_peak:.4f} avg={rms_avg:.4f} - hampir mati! "
                    "Cek mic Windows / asr_input_device / mute",
                )
            elif rms_peak < 0.025:
                report.add(
                    "Mic level",
                    "WARN",
                    f"peak={rms_peak:.4f} avg={rms_avg:.4f} - rendah, ngomong lebih keras saat PTT",
                )
            else:
                report.add(
                    "Mic level",
                    "OK",
                    f"peak={rms_peak:.4f} avg={rms_avg:.4f}",
                )
        except Exception as e:
            report.add("Mic level", "FAIL", f"Tidak bisa buka mic: {e}")

    # --- VTS ---
    if vts is not None and getattr(vts, "websocket", None):
        report.add("VTS API", "OK", f"port {cfg.get('vts_api_port', 8002)} connected")
    else:
        report.add("VTS API", "FAIL", "Tidak terhubung - ekspresi/idle bisa gagal")

    # --- TTS cable ---
    if tts is not None:
        dev_id = getattr(tts, "device_id", None)
        if dev_id is not None:
            try:
                tts_name = sd.query_devices(dev_id)["name"]
                report.add("TTS output", "OK", f"{tts_name} (device {dev_id})")
            except Exception:
                report.add("TTS output", "OK", f"virtual cable device {dev_id}")
        else:
            report.add("TTS output", "WARN", "Virtual cable tidak ketemu — suara ke default speaker")

    # --- APIs ---
    provider = (cfg.get("api_provider") or "groq").lower()
    report.add("LLM provider", "INFO", provider)

    if provider == "groq":
        st, det = probe_groq_api(cfg)
        report.add("Groq API", st, det)
    else:
        report.add("Groq API", "SKIP", f"provider={provider}")

    if cfg.get("openrouter_api_key"):
        st, det = probe_openrouter_api(cfg)
        report.add("OpenRouter", st, det)
    else:
        report.add("OpenRouter", "SKIP", "key kosong (fallback off)")

    # --- Hotkey / trigger mode ---
    mode = cfg.get("trigger_mode", "push_to_talk")
    hk = cfg.get("hotkey_key", "?")
    if hotkey_registered:
        report.add("PTT hotkey", "OK", f"{hk} registered")
    else:
        report.add("PTT hotkey", "FAIL", f"{hk} gagal — toggle mouse tidak jalan")

    if mode == "push_to_talk":
        report.add(
            "Trigger mode",
            "INFO",
            "push_to_talk - klik mouse ON dulu, baru ngomong (bukan cuma 'eh arti')",
        )
    else:
        report.add("Trigger mode", "INFO", f"{mode} - katakan 'arti' / wake word")

    report.add(
        "Pipeline expect",
        "INFO",
        "Toggle ON -> [ASR] Mendengar suara -> [Toggle ON] Hasil -> [Groq API]",
    )

    return report


def probe_vision_providers(config: dict) -> list[CheckRow]:
    """Lightweight key/endpoint probes for vision chain (no full describe)."""
    rows: list[CheckRow] = []
    cfg = config

    # mss capture
    try:
        import arti_vision_capture

        ok, detail = arti_vision_capture.probe_capture(cfg)
        rows.append(CheckRow("mss capture", "OK" if ok else "FAIL", detail))
    except Exception as e:
        rows.append(CheckRow("mss capture", "FAIL", str(e)))

    # NVIDIA
    key = (cfg.get("nvidia_api_key") or os.environ.get("NVIDIA_API_KEY") or "").strip()
    if key:
        rows.append(CheckRow("NVIDIA vision", "INFO", "key set (describe not probed)"))
    else:
        rows.append(CheckRow("NVIDIA vision", "SKIP", "NVIDIA_API_KEY kosong"))

    # Google Gemini
    gkey = (cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()
    if gkey and gkey != "YOUR_GEMINI_API_KEY":
        rows.append(CheckRow("Google vision", "INFO", "GEMINI_API_KEY set"))
    else:
        rows.append(CheckRow("Google vision", "SKIP", "GEMINI_API_KEY kosong"))

    # Cloudflare
    cf_tok = (cfg.get("cloudflare_api_token") or os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
    cf_acc = (cfg.get("cloudflare_account_id") or os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    if cf_tok and cf_acc:
        rows.append(CheckRow("Cloudflare vision", "INFO", "token + account set"))
    else:
        rows.append(CheckRow("Cloudflare vision", "SKIP", "CLOUDFLARE_* kosong"))

    # OpenRouter / Groq — reuse existing probes if keys present
    if cfg.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY"):
        st, det = probe_openrouter_api(cfg)
        rows.append(CheckRow("OpenRouter vision", st, det))
    if cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY"):
        st, det = probe_groq_api(cfg)
        rows.append(CheckRow("Groq vision", st, det))

    # GitHub
    if cfg.get("vision_github_enabled"):
        gh = (cfg.get("github_models_token") or os.environ.get("GITHUB_TOKEN") or "").strip()
        rows.append(
            CheckRow(
                "GitHub vision",
                "INFO" if gh else "SKIP",
                "GITHUB_TOKEN set" if gh else "token kosong",
            )
        )
    else:
        rows.append(CheckRow("GitHub vision", "SKIP", "vision_github_enabled=False"))

    # Z.ai
    zai = (cfg.get("zai_api_key") or os.environ.get("ZAI_API_KEY") or "").strip()
    rows.append(
        CheckRow("Z.ai vision", "INFO" if zai else "SKIP", "key set" if zai else "ZAI_API_KEY kosong")
    )

    # Ollama
    oll = (cfg.get("ollama_api_key") or os.environ.get("OLLAMA_API_KEY") or "").strip()
    rows.append(
        CheckRow(
            "Ollama vision",
            "INFO" if oll else "SKIP",
            "key set" if oll else "OLLAMA_API_KEY kosong",
        )
    )

    return rows


def probe_scouter_providers(config: dict) -> list[CheckRow]:
    """Lightweight key probes for scouter text chain (no Groq)."""
    rows: list[CheckRow] = []
    cfg = config
    chain = list(cfg.get("scouter_provider_chain") or [])

    if not cfg.get("scouter_enabled", True):
        rows.append(CheckRow("Scouter", "SKIP", "scouter_enabled=False"))
        return rows

    rows.append(CheckRow("Scouter chain", "INFO", " → ".join(chain) or "(empty)"))

    key = (cfg.get("nvidia_api_key") or os.environ.get("NVIDIA_API_KEY") or "").strip()
    rows.append(CheckRow("Scouter NVIDIA", "INFO" if key else "SKIP", "key set" if key else "NVIDIA_API_KEY kosong"))

    cf_tok = (cfg.get("cloudflare_api_token") or os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
    cf_acc = (cfg.get("cloudflare_account_id") or os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    if cf_tok and cf_acc:
        rows.append(CheckRow("Scouter Cloudflare", "INFO", "token + account set"))
    else:
        rows.append(CheckRow("Scouter Cloudflare", "SKIP", "CLOUDFLARE_* kosong"))

    if cfg.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY"):
        st, det = probe_openrouter_api(cfg)
        rows.append(CheckRow("Scouter OpenRouter", st, det))
    else:
        rows.append(CheckRow("Scouter OpenRouter", "SKIP", "OPENROUTER_API_KEY kosong"))

    gkey = (cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()
    if gkey and gkey != "YOUR_GEMINI_API_KEY":
        rows.append(CheckRow("Scouter Gemini", "INFO", "GEMINI_API_KEY set"))
    else:
        rows.append(CheckRow("Scouter Gemini", "SKIP", "GEMINI_API_KEY kosong"))

    if cfg.get("vision_github_enabled"):
        gh = (cfg.get("github_models_token") or os.environ.get("GITHUB_TOKEN") or "").strip()
        rows.append(
            CheckRow(
                "Scouter GitHub",
                "INFO" if gh else "SKIP",
                "GITHUB_TOKEN set" if gh else "token kosong",
            )
        )
    else:
        rows.append(CheckRow("Scouter GitHub", "SKIP", "vision_github_enabled=False"))

    zai = (cfg.get("zai_api_key") or os.environ.get("ZAI_API_KEY") or "").strip()
    rows.append(
        CheckRow("Scouter Z.ai", "INFO" if zai else "SKIP", "key set" if zai else "ZAI_API_KEY kosong")
    )

    oll = (cfg.get("ollama_api_key") or os.environ.get("OLLAMA_API_KEY") or "").strip()
    rows.append(
        CheckRow("Scouter Ollama", "INFO" if oll else "SKIP", "key set" if oll else "OLLAMA_API_KEY kosong")
    )

    rows.append(CheckRow("Scouter Groq", "SKIP", "Groq tidak dipakai scouter (voice only)"))
    return rows


def probe_observer_providers(config: dict) -> list[CheckRow]:
    """Alias: text deep probes for observer chain (keys only unless --deep)."""
    return probe_scouter_providers({**config, "scouter_provider_chain": config.get("observer_provider_chain")})


def probe_observer_db(config: dict) -> CheckRow:
    """Check observer_rag.db exists and is readable."""
    try:
        import arti_observer_rag as obs

        cfg = {**obs.DEFAULT_CONFIG, **config}
        if not cfg.get("observer_enabled", True):
            return CheckRow("Observer DB", "SKIP", "observer_enabled=False")
        stats = obs.index_stats(cfg)
        chunks = stats.get("chunks", 0)
        if chunks == 0:
            return CheckRow("Observer DB", "INFO", "DB kosong (belum ada beats)")
        return CheckRow("Observer DB", "OK", f"{chunks} chunks")
    except Exception as e:
        return CheckRow("Observer DB", "WARN", str(e)[:120])


def probe_rag_origin_canon(config: dict) -> CheckRow:
    """Smoke: timeline query should retrieve arti_origin from vault RAG index."""
    try:
        import arti_vault_rag as rag

        cfg = {**rag.DEFAULT_CONFIG, **config}
        if not cfg.get("vault_rag_enabled", True):
            return CheckRow("RAG origin canon", "SKIP", "vault_rag_enabled=False")
        stats = rag.index_stats(cfg)
        if stats.get("chunks", 0) == 0:
            return CheckRow("RAG origin canon", "WARN", "DB kosong — jalankan arti_vault_rag.py --reindex-all")
        q = "arti mulai sejak kapan debut"
        enriched = rag.enrich_rag_query(q, cfg)
        hits = rag.search(enriched, cfg, top_k=5)
        if not hits:
            return CheckRow("RAG origin canon", "WARN", "no hits for timeline query")
        paths = [str(h.get("source_path", "")) for h in hits]
        if any("arti_origin" in p for p in paths):
            return CheckRow(
                "RAG origin canon",
                "OK",
                f"arti_origin in top-{len(hits)} (score={hits[0].get('score')})",
            )
        return CheckRow(
            "RAG origin canon",
            "WARN",
            f"no arti_origin — top={paths[0][:48]} score={hits[0].get('score')}",
        )
    except Exception as e:
        return CheckRow("RAG origin canon", "FAIL", str(e)[:120])


def print_health_report(report: HealthReport) -> None:
    w = 62
    print("\n" + "=" * w)
    print("  ARTI BRIDGE — HEALTH CHECK")
    print("=" * w)
    for row in report.rows:
        tag = row.status.ljust(4)
        print(f"  [{tag}] {row.name:<16} {row.detail}")
    ok, warn, fail = report.counts()
    print("-" * w)
    if fail:
        print(f"  {ok} OK, {warn} WARN, {fail} FAIL - perbaiki FAIL sebelum stream!")
    elif warn:
        print(f"  {ok} OK, {warn} WARN - bisa jalan, tapi cek WARN (terutama mic)")
    else:
        print(f"  {ok} OK - semua hijau, siap stream")
    print("=" * w + "\n")


def mic_watch_after_toggle(
    device_id: int | None,
    device_name: str,
    seconds: float = 5.0,
    label: str = "PTT ON",
) -> None:
    """Dipanggil pas toggle ON — laporkan apakah mic benar-benar nerima suara."""
    try:
        print(f"[MicMonitor] Sampling {seconds:.0f}s pada '{device_name}' ({label})...")
        rms_avg, rms_peak = sample_mic_levels(device_id, seconds=seconds)
        if rms_peak < 0.008:
            print(
                f"[MicMonitor] FAIL peak={rms_peak:.4f} - mic mati/salah device! "
                "Ngomong tapi nggak ada sinyal. Cek asr_input_device / Windows mic."
            )
        elif rms_peak < 0.025:
            print(
                f"[MicMonitor] WARN peak={rms_peak:.4f} - lemah. "
                "Coba ngomong lebih keras; expect [ASR] Mendengar suara..."
            )
        else:
            print(
                f"[MicMonitor] OK peak={rms_peak:.4f} avg={rms_avg:.4f} - "
                "mic hidup. Ngomong -> tunggu [ASR] Mendengar suara..."
            )
    except Exception as e:
        print(f"[MicMonitor] ERROR: {e}")


if __name__ == "__main__":
    """Cek mic + API tanpa full bridge: python bridge_health.py [--deep] [--telemetry] [--dashboard]"""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Arti bridge health check")
    parser.add_argument("--deep", action="store_true", help="Probe 1-2 vision + text models per API key")
    parser.add_argument("--telemetry", action="store_true", help="Show API usage rollup from telemetry JSONL")
    parser.add_argument("--dashboard", action="store_true", help="Generate HTML telemetry dashboard (data/telemetry/dashboard.html)")
    parser.add_argument("--open-dashboard", action="store_true", help="With --dashboard, open in browser")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent

    import arti_env

    arti_env.load_project_env(root)

    if args.telemetry:
        try:
            import arti_api_telemetry as tel

            cfg = {"telemetry_dir": "data/telemetry"}
            print(tel.format_telemetry_report(cfg))
        except Exception as e:
            print(f"[Telemetry] {e}")
        raise SystemExit(0)

    if args.dashboard:
        try:
            import arti_telemetry_dashboard as dash

            cfg = {"telemetry_dir": "data/telemetry"}
            out = dash.generate_dashboard(cfg)
            print(f"[Telemetry] Dashboard -> {out}")
            if args.open_dashboard:
                import webbrowser

                webbrowser.open(out.as_uri())
        except Exception as e:
            print(f"[Telemetry] dashboard failed: {e}")
        raise SystemExit(0)

    try:
        import arti_bridge as bridge

        cfg = dict(bridge.CONFIG)
    except Exception:
        cfg = {
            "groq_api_key": os.environ.get("GROQ_API_KEY", ""),
            "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY", ""),
            "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
            "nvidia_api_key": os.environ.get("NVIDIA_API_KEY", ""),
            "cloudflare_api_token": os.environ.get("CLOUDFLARE_API_TOKEN", ""),
            "cloudflare_account_id": os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""),
            "zai_api_key": os.environ.get("ZAI_API_KEY", ""),
            "ollama_api_key": os.environ.get("OLLAMA_API_KEY", ""),
            "github_models_token": os.environ.get("GITHUB_TOKEN", ""),
            "groq_models": ["llama-3.1-8b-instant"],
            "groq_model_fast": "llama-3.1-8b-instant",
            "api_provider": "groq",
            "trigger_mode": "push_to_talk",
            "hotkey_key": "mouse_x2",
            "asr_input_device": None,
            "asr_skip_device_patterns": [
                "stereo mix", "wave out", "what u hear", "loopback", "virtual cable", "cable output",
            ],
            "vision_enabled": True,
            "vision_provider_chain": [
                "nvidia", "google_gemma", "google_gemini_lite", "cloudflare", "openrouter", "zai", "ollama",
            ],
            "scouter_provider_chain": [
                "nvidia", "cloudflare", "openrouter", "google_gemini", "zai", "ollama",
            ],
        }

    deep = args.deep or os.environ.get("BRIDGE_HEALTH_DEEP", "").strip() in ("1", "true", "yes")

    env_st = arti_env.env_key_status()
    loaded = (root / ".env").is_file()
    print(f"\n--- ENV KEYS ({'from .env + system' if loaded else 'system only'}) ---")
    for k, st in env_st.items():
        print(f"  {k}: {st}")
    if env_st.get("CLOUDFLARE_API_TOKEN") == "SET" and env_st.get("CLOUDFLARE_ACCOUNT_ID") == "EMPTY":
        print("  [WARN] CLOUDFLARE_ACCOUNT_ID kosong — tambah ke .env (token saja tidak cukup)")

    def _resolve(_cfg):
        import arti_bridge as bridge

        return bridge.resolve_asr_input_device(_cfg)

    report = run_startup_health_check(
        cfg,
        resolve_mic_fn=_resolve,
        vts=None,
        tts=None,
        hotkey_registered=False,
        interactive_mic_test=not deep,
    )
    print_health_report(report)

    print("\n--- VISION PROVIDERS (keys) ---")
    for row in probe_vision_providers(cfg):
        print(f"  [{row.status}] {row.name}: {row.detail}")

    print("\n--- SCOUTER PROVIDERS (keys) ---")
    for row in probe_scouter_providers(cfg):
        print(f"  [{row.status}] {row.name}: {row.detail}")

    rag_row = probe_rag_origin_canon(cfg)
    print(f"\n--- RAG ---\n  [{rag_row.status}] {rag_row.name}: {rag_row.detail}")

    if deep:
        import bridge_health_probes as bhp

        print("\n--- DEEP VISION MODEL PROBES ---")
        vrows = bhp.probe_vision_providers_deep(cfg)
        bhp.print_deep_probe_table(vrows)
        print("\n--- DEEP TEXT MODEL PROBES (scouter) ---")
        trows = bhp.probe_text_providers_deep(cfg, "scouter_provider_chain")
        bhp.print_deep_probe_table(trows)
        obs_chain = cfg.get("observer_provider_chain")
        if obs_chain:
            print("\n--- DEEP TEXT MODEL PROBES (observer) ---")
            orows = bhp.probe_text_providers_deep(cfg, "observer_provider_chain")
            bhp.print_deep_probe_table(orows)
