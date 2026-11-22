"""Session transcript JSONL, manifest, metrics, log rotation (Fase 1)."""
from __future__ import annotations

from arti_runtime import ipc
from arti_runtime import vault_farm
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any
from arti_buffered_writer import BatchedWriter

_ROOT = Path(__file__).resolve().parent
_lock = threading.Lock()
_turn_seq = 0
_trigger_count = 0
_session_id: str | None = None
_transcript_path: Path | None = None
_manifest_path: Path | None = None
_started_at: str | None = None
_file = None
# Transcript write-failure reporting (audit L01): at most one line per interval.
_write_fail_logged_at: float | None = None
_write_fail_seen = (0, 0)  # (failed batches, dropped lines) already reported


def get_session_id(config: dict) -> str:
    sid = (config.get("stream_session_id") or "").strip()
    if sid:
        return sid
    profile = config.get("active_profile", "default").lower()
    return f"{time.strftime('%Y-%m-%d')}-{profile}"


def current_session_id(config: dict) -> str:
    """Session id fixed at startup (init_session_artifacts); date-derived only before init.

    Audit L03: a stream started at 23:50 must keep its start-date id after
    midnight. Shutdown, telemetry and catch-up use this, not get_session_id().
    """
    return _session_id or get_session_id(config)


def rotate_session_logs(config: dict) -> None:
    keep = int(config.get("session_log_keep_n", 5))
    if keep < 1:
        return
    log_dir = _ROOT / "session_logs"
    archive_dir = _ROOT / "archive" / "v0.4" / "session_logs"
    if not log_dir.is_dir():
        return
    logs = sorted(log_dir.glob("*_bridge.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    aktif = str(config.get("session_log_active_path") or "")
    if aktif:
        # Log milik proses ini sendiri: Windows menolak memindah berkas yang
        # sedang dibuka (WinError 32 berisik tiap startup, tes 16 Agu 14.28) —
        # dan memang tidak boleh disapu; dia baru <20 KB karena BARU LAHIR.
        logs = [p for p in logs if str(p) != aktif]

    # SADAR-UKURAN (diagnosa 12 Agu 2026). Dulu jatah `keep` dihitung dari
    # SEMUA berkas, padahal tiap bridge yang gagal start / harness / (dulu)
    # pytest meninggalkan log ~293 byte. Log siaran Bohan yang 194 KB tersapu
    # ke arsip dalam hitungan menit, jadi waktu dia buka session_logs/ isinya
    # cuma cangkang kosong — dan dia berhenti percaya pada log otomatis lalu
    # menyalin terminal ke notepad secara manual berbulan-bulan.
    # Sekarang: hanya log substansial yang memakai jatah; sisanya langsung
    # diarsipkan (dipindah, TIDAK dihapus).
    min_bytes = int(config.get("session_log_min_bytes", 20000))
    try:
        substansial = [p for p in logs if p.stat().st_size >= min_bytes]
        remeh = [p for p in logs if p.stat().st_size < min_bytes]
    except OSError:
        substansial, remeh = logs, []
    buangan = remeh + substansial[keep:]
    if not buangan:
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    for p in buangan:
        dest = archive_dir / p.name
        try:
            shutil.move(str(p), str(dest))
            print(f"[LogRotation] {p.name} -> archive/v0.4/session_logs/")
        except Exception as e:
            print(f"[LogRotation] Gagal memindah {p.name}: {e}")


def init_session_artifacts(config: dict) -> None:
    global _session_id, _transcript_path, _manifest_path, _started_at, _file, _turn_seq, _trigger_count
    global _write_fail_logged_at, _write_fail_seen
    close_transcript_file()
    _write_fail_logged_at, _write_fail_seen = None, (0, 0)
    rotate_session_logs(config)
    _session_id = get_session_id(config)
    tdir = _ROOT / config.get("transcript_dir", "transcripts")
    _transcript_path = tdir / f"{_session_id}.jsonl"
    data_dir = _ROOT / "data"
    _manifest_path = data_dir / "session_manifest.json"
    _started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    _turn_seq = 0
    _trigger_count = 0

    manifest = {
        "session_id": _session_id,
        "started_at": _started_at,
        "ended_at": None,
        "transcript_path": str(_transcript_path.relative_to(_ROOT)).replace("\\", "/"),
        "profile": config.get("active_profile", "default"),
        "trigger_count": 0,
    }
    try:
        tdir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        ipc.write_json(_manifest_path, manifest, critical=False, fsync=False)

        _file = BatchedWriter(
            ipc.open_shared(_transcript_path, "a", encoding="utf-8"),
            fsync=config.get("transcript_flush_fsync", True),
            interval=config.get("transcript_flush_interval_sec", 1.0),
            lines=config.get("transcript_flush_lines", 32),
        )
    except OSError as exc:
        _file = None
        _report_write_failure(exc, None, None, config)
    print(f"[Transcript] Session {_session_id} -> {_transcript_path.name}")

    try:
        import arti_api_telemetry as tel

        tel.set_session_id(_session_id)
    except Exception:
        pass

    append_transcript(
        {
            "ts": time.strftime("%H:%M:%S"),
            "kind": "system",
            "text": f"Bridge session started ({_session_id})",
            "trigger": False,
        },
        config,
    )


def append_transcript(record: dict, config: dict) -> None:
    """Queue one transcript line. Never raises (audit L01): a disk error must
    not cost a trigger; failures are reported at most once per interval."""
    global _file
    if _file is None:
        return
    error = None
    try:
        line = json.dumps(record, ensure_ascii=False)
        with _lock:
            if _file is not None:
                _file.write(line + "\n")
                health = getattr(_file, "write_health", None)
                if health is not None:
                    error, failed, dropped = health()
                    if failed == _write_fail_seen[0]:
                        error = None
    except Exception as exc:  # noqa: BLE001 — transcript is best-effort
        error, failed, dropped = exc, None, None
    if error is not None:
        _report_write_failure(error, failed, dropped, config)


def _report_write_failure(error: BaseException, failed, dropped, config: dict) -> None:
    global _write_fail_logged_at, _write_fail_seen
    now = time.monotonic()
    every = float((config or {}).get("transcript_write_error_log_interval_sec", 300.0))
    with _lock:
        if _write_fail_logged_at is not None and now - _write_fail_logged_at < every:
            return
        _write_fail_logged_at = now
        if failed is not None:
            _write_fail_seen = (failed, dropped)
    print(
        f"[Transcript] Gagal menulis FAULT ({type(error).__name__}); "
        f"batch gagal={failed if failed is not None else '?'} "
        f"baris dibuang={dropped if dropped is not None else '?'}; "
        "trigger tetap diantre, penulisan dicoba lagi tiap batch"
    )


def _parse_source(source: str, message: str) -> dict[str, Any]:
    if source.startswith("Viewer"):
        m = re.search(r"Viewer\s+@?([^\s(]+)", source)
        name = m.group(1) if m else "unknown"
        return {"kind": "viewer", "channel": "youtube", "name": name, "text": message}
    if source == "Streamer":
        return {"kind": "streamer", "channel": "mic", "text": message}
    if "Arti" in source:
        return {"kind": "arti", "text": message}
    return {"kind": "system", "text": message}


def append_from_history(source: str, message: str, config: dict) -> None:
    rec = _parse_source(source, message)
    rec["ts"] = time.strftime("%H:%M:%S")
    rec.setdefault("trigger", False)
    append_transcript(rec, config)


def log_trigger(trigger_type: str, name: str | None, text: str, config: dict) -> str:
    global _turn_seq, _trigger_count
    with _lock:
        _turn_seq += 1
        _trigger_count += 1
        turn_id = f"t-{_turn_seq:04d}"
    rec = {
        "ts": time.strftime("%H:%M:%S"),
        "kind": "trigger",
        "trigger_type": trigger_type,
        "trigger": True,
        "turn_id": turn_id,
        "text": text,
    }
    if name:
        rec["name"] = name
    # Audit L01: logging is best-effort; the caller queues the trigger with
    # this turn id even if the disk is full.
    try:
        append_transcript(rec, config)
        _update_manifest_trigger_count(config)
    except Exception:  # noqa: BLE001
        pass
    return turn_id


def log_arti_reply(
    text: str,
    config: dict,
    *,
    turn_id: str | None = None,
    latency_ms: int | None = None,
    groq_model: str | None = None,
    stages: dict | None = None,
) -> None:
    rec: dict[str, Any] = {
        "ts": time.strftime("%H:%M:%S"),
        "kind": "arti",
        "text": text,
        "trigger": False,
    }
    if turn_id:
        rec["turn_id"] = turn_id
    if latency_ms is not None:
        rec["latency_ms"] = latency_ms
    if groq_model:
        rec["groq_model"] = groq_model
    if stages:
        rec["stages"] = stages
    append_transcript(rec, config)


def _update_manifest_trigger_count(config: dict) -> None:
    if _manifest_path is None or not _manifest_path.exists():
        return
    try:
        data = json.loads(ipc.read_text(_manifest_path, encoding="utf-8"))
        data["trigger_count"] = _trigger_count
        ipc.write_json(_manifest_path, data, critical=False, fsync=False)
    except Exception:
        pass


def get_transcript_path(config: dict | None = None) -> Path | None:
    """Path JSONL sesi aktif/terakhir (setelah close masih valid)."""
    cfg = config or {}
    if _transcript_path:
        return _transcript_path
    sid = _session_id or get_session_id(cfg)
    if not sid:
        return None
    return _ROOT / cfg.get("transcript_dir", "transcripts") / f"{sid}.jsonl"


def close_transcript_file() -> None:
    global _file
    with _lock:
        if _file is not None:
            try:
                _file.close()  # drains pending lines and fsyncs before closing
            except Exception:
                pass
            _file = None


def parse_transcript_metrics(transcript_path: Path) -> dict[str, Any]:
    counts = {"viewer": 0, "streamer": 0, "arti": 0, "trigger": 0}
    latencies: list[int] = []
    if not transcript_path.is_file():
        return {"counts": counts, "avg_latency_ms": None, "trigger_count": 0}

    for line in ipc.read_text(transcript_path, encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = row.get("kind", "")
        if kind in counts:
            counts[kind] += 1
        if row.get("kind") == "trigger" or row.get("trigger"):
            counts["trigger"] += int(row.get("kind") == "trigger")
        if row.get("kind") == "arti" and row.get("latency_ms") is not None:
            latencies.append(int(row["latency_ms"]))

    avg = int(sum(latencies) / len(latencies)) if latencies else None
    return {
        "counts": counts,
        "avg_latency_ms": avg,
        "trigger_count": _trigger_count,
        "arti_replies": counts.get("arti", 0),
    }


def scan_debug_log_errors(debug_log_path: Path | None, max_lines: int = 80) -> list[str]:
    if not debug_log_path or not debug_log_path.is_file():
        return []
    patterns = (
        "ERROR",
        "Error]",
        "Rate limit",
        "different loop",
        "Task was destroyed",
        "TimeoutError",
        "Supertone gagal",
        "Robot mode",
        "edge_tts",
    )
    hits: list[str] = []
    try:
        lines = ipc.read_text(debug_log_path, encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return hits
    for line in lines:
        if any(p in line for p in patterns):
            hits.append(line.strip())
    return hits[-max_lines:]


def _transcript_excerpt(transcript_path: Path, max_lines: int = 120) -> str:
    lines: list[str] = []
    if not transcript_path.is_file():
        return ""
    for line in ipc.read_text(transcript_path, encoding="utf-8").splitlines()[-400:]:
        try:
            row = json.loads(line)
            lines.append(
                f"[{row.get('ts', '')}] {row.get('kind', '')}: {str(row.get('text', ''))[:200]}"
            )
        except json.JSONDecodeError:
            continue
    return "\n".join(lines[-max_lines:])


def fetch_rag_lite_context(session_id: str, transcript_path: Path, config: dict) -> str:
    if not config.get("vault_rag_enabled", True) or not config.get("vault_rag_lite_enabled", True):
        return ""
    try:
        import arti_vault_rag

        excerpt = _transcript_excerpt(transcript_path, max_lines=80)
        block = arti_vault_rag.get_rag_context_for_reflection(session_id, excerpt, config)
        if block:
            print(f"[Vault RAG] Lite inject {len(block)} chars (reflection/summary)")
        return block
    except Exception as e:
        print(f"[Vault RAG] Lite skip: {e}")
        return ""


def groq_summarize_session(
    transcript_path: Path,
    config: dict,
    *,
    rag_context: str = "",
) -> str:
    """Primary Groq vault shutdown summary."""
    key = config.get("groq_api_key") or ""
    if not key or key == "YOUR_GROQ_API_KEY":
        return ""

    import arti_memory_quality
    import requests

    chat_logs = _transcript_excerpt(transcript_path)
    if not chat_logs:
        return ""

    model = config.get("groq_model_fast") or config.get("groq_models", ["llama-3.1-8b-instant"])[0]
    user_body = _vault_summary_user_body(chat_logs, rag_context)
    try:
        print(f"\n[Vault] Merangkum sesi via Groq ({model})...")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        data = {
            "model": model,
            "max_tokens": 400,
            "messages": [
                {
                    "role": "system",
                    "content": _VAULT_SUMMARY_SYSTEM,
                },
                {"role": "user", "content": user_body},
            ],
        }
        if "qwen" in model.lower():
            # "/no_think" diabaikan qwen3.6 — pakai reasoning_effort API param
            data["reasoning_effort"] = "none"
        t0 = time.perf_counter()
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=data,
            timeout=60,
        )
        ms = int((time.perf_counter() - t0) * 1000)
        if res.status_code == 200:
            body = res.json()
            raw = body["choices"][0]["message"]["content"].strip()
            try:
                import arti_api_telemetry as tel

                tel.record_openai_response(
                    subsystem="vault_summary",
                    provider="groq",
                    model=model,
                    body=body,
                    latency_ms=ms,
                    ok=bool(raw),
                    config=config,
                )
            except Exception:
                pass
            return arti_memory_quality.sanitize_model_text(raw)
        try:
            import arti_api_telemetry as tel

            tel.record_call(
                subsystem="vault_summary",
                provider="groq",
                model=model,
                latency_ms=ms,
                ok=False,
                config=config,
                extra={"http": res.status_code},
            )
        except Exception:
            pass
        print(f"[Vault] Groq ringkasan HTTP {res.status_code}")
        return ""
    except Exception as e:
        print(f"[Vault] Groq ringkasan error: {e}")
        return ""


_VAULT_SUMMARY_SYSTEM = (
    "Kamu asisten arsip stream VTuber. Ringkas dalam 2-3 paragraf bahasa Indonesia, "
    "santai. Jangan tampilkan chain-of-thought.\n\n"
    "Ringkasan ini masuk vault dan nanti diambil lagi lewat RAG saat Arti menjawab, "
    "jadi tulis hal yang berguna diingat berminggu-minggu kemudian:\n"
    "- Apa yang sebenarnya dikerjakan atau dibahas, dengan detail konkret "
    "(nama game/proyek/file, judul, keputusan yang diambil, masalah yang muncul).\n"
    "- Cerita pribadi, pendapat, atau rencana yang disebut streamer.\n"
    "- Penonton yang muncul dan apa yang mereka bawa.\n"
    "- Perubahan suasana yang jelas dan pemicunya.\n\n"
    "JANGAN tulis: bahwa sesi dimulai atau berakhir, jumlah trigger, bahwa Arti "
    "menjawab dengan ramah, deskripsi UI atau aplikasi yang terbuka, atau kalimat "
    "pengisi seperti 'streamer melanjutkan aktivitasnya'. Kalau sesinya memang sepi, "
    "tulis pendek saja — jangan dipanjang-panjangkan."
)


def _vault_summary_user_body(chat_logs: str, rag_context: str) -> str:
    user_body = f"LOG TRANSCRIPT:\n{chat_logs}"
    if rag_context:
        user_body += f"\n\nKONTEKS VAULT (RAG — gunakan jika relevan untuk ringkasan):\n{rag_context}"
    return user_body


def openrouter_summarize_session(
    transcript_path: Path,
    config: dict,
    *,
    rag_context: str = "",
) -> str:
    """Fallback vault summary via OpenRouter (smarter models)."""
    import arti_memory_quality
    import arti_openrouter

    key = config.get("openrouter_api_key") or ""
    if not key:
        return ""
    chat_logs = _transcript_excerpt(transcript_path)
    if not chat_logs:
        return ""

    models = [
        config.get("openrouter_reflection_fallback_model", "google/gemma-4-26b-a4b-it:free"),
        config.get("openrouter_reflection_model", "nvidia/nemotron-3-super-120b-a12b:free"),
        config.get("openrouter_summarizer_model", "nvidia/nemotron-3-super-120b-a12b:free"),
    ]
    user_body = _vault_summary_user_body(chat_logs, rag_context)
    for model in models:
        if not model:
            continue
        print(f"[Vault] Merangkum sesi via OpenRouter ({model})...")
        text = arti_openrouter.openrouter_chat(
            key,
            model,
            [
                {"role": "system", "content": _VAULT_SUMMARY_SYSTEM},
                {"role": "user", "content": user_body},
            ],
            max_tokens=500,
            timeout=int(config.get("openrouter_reflection_timeout_sec", 120)),
            title="Arti Vault Summary",
        )
        if text:
            return arti_memory_quality.sanitize_model_text(text.strip())
    return ""


def gemini_summarize_session(
    transcript_path: Path,
    config: dict,
    *,
    rag_context: str = "",
) -> str:
    """Last-resort vault summary via Google Gemini Flash Lite."""
    import arti_memory_quality
    import requests

    key = config.get("gemini_api_key") or ""
    if not key or key == "YOUR_GEMINI_API_KEY":
        return ""
    chat_logs = _transcript_excerpt(transcript_path)
    if not chat_logs:
        return ""

    model = config.get("vision_google_gemini_model", "gemini-3.1-flash-lite")
    user_body = _vault_summary_user_body(chat_logs, rag_context)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": _VAULT_SUMMARY_SYSTEM + "\n\n" + user_body},
                ]
            }
        ],
        "generationConfig": {"maxOutputTokens": 500, "temperature": 0.3},
    }
    try:
        print(f"[Vault] Merangkum sesi via Gemini ({model})...")
        res = requests.post(url, json=payload, timeout=90)
        if res.status_code != 200:
            print(f"[Vault] Gemini ringkasan HTTP {res.status_code}")
            return ""
        body = res.json()
        parts = body["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        return arti_memory_quality.sanitize_model_text(text) if text else ""
    except Exception as e:
        print(f"[Vault] Gemini ringkasan error: {e}")
        return ""


def summarize_session_from_beats(beats_path: Path, config: dict | None = None) -> str:
    """Build 2-paragraph session summary from approved beats."""
    if not beats_path.is_file():
        return ""
    approved: list[str] = []
    for line in ipc.read_text(beats_path, encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("curator_status") != "approved":
            continue
        summary = (row.get("summary") or "").strip()
        if summary:
            approved.append(f"[{row.get('t_start', '')}] {summary}")
    if not approved:
        return ""
    joined = "\n".join(approved[:24])
    return (
        "Ringkasan dari timeline beats (Observer):\n\n"
        + joined[:3500]
        + ("\n\n(…beats dipotong)" if len(joined) > 3500 else "")
    )


def _vault_dir(config, *parts) -> Path:
    """The vault folder the session note is written to: `vault_write_root` when set (a rehearsal scratch vault,
    2026-11-18), else this checkout's own vault/ (unchanged)."""
    base = (config or {}).get("vault_write_root")
    return (Path(base) if base else _ROOT / "vault").joinpath(*parts)


def summarize_session_for_vault(
    transcript_path: Path,
    config: dict,
    *,
    rag_context: str = "",
) -> str:
    """Vault shutdown summary: beats first, else Groq → OpenRouter → Gemini."""
    session_id = _session_id or get_session_id(config)
    beats_jsonl = _vault_dir(config, "sessions", f"{session_id}_beats.jsonl")
    if config.get("observer_enabled", True) and beats_jsonl.is_file():
        from_beats = summarize_session_from_beats(beats_jsonl, config)
        if from_beats:
            print("[Vault] Ringkasan OK via observer beats")
            return from_beats

    if config.get("vault_summary_external_enabled", True) is not True:
        return "Ringkasan eksternal dinonaktifkan; transcript dan metadata lokal tetap disimpan."

    if not _transcript_excerpt(transcript_path):
        return "Tidak ada data transcript."

    chain = [
        ("groq", groq_summarize_session),
        ("openrouter", openrouter_summarize_session),
        ("gemini", gemini_summarize_session),
    ]
    last_err = ""
    for name, fn in chain:
        try:
            summary = fn(transcript_path, config, rag_context=rag_context)
            if summary:
                print(f"[Vault] Ringkasan OK via {name}")
                return summary
            last_err = f"{name} returned empty"
        except Exception as e:
            last_err = f"{name}: {e}"
            print(f"[Vault] {name} fail: {e}")

    if not (config.get("groq_api_key") or config.get("openrouter_api_key") or config.get("gemini_api_key")):
        return "Ringkasan tidak dibuat (API keys kosong)."
    return f"Ringkasan gagal ({last_err or 'semua provider'})."


def _session_dates() -> list[str]:
    """Dates from session start through today (audit L03: post-midnight shutdown
    must still list pre-midnight learnings). Only today when start is unknown."""
    today = time.strftime("%Y-%m-%d")
    try:
        from datetime import date, timedelta

        start = date.fromisoformat((_started_at or "")[:10])
        end = date.fromisoformat(today)
        if start >= end or (end - start).days > 7:
            return [today]
        return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    except Exception:  # noqa: BLE001
        return [today]


def write_vault_session_md(
    config: dict,
    summary: str,
    metrics: dict[str, Any],
    error_snippets: list[str],
    *,
    rag_context: str = "",
) -> Path | None:
    session_id = _session_id or get_session_id(config)
    vault_dir = _vault_dir(config, "sessions")
    vault_dir.mkdir(parents=True, exist_ok=True)
    path = vault_dir / f"{session_id}.md"
    rel_tx = f"transcripts/{session_id}.jsonl"
    counts = metrics.get("counts", {})

    md = f"""# Live Stream Session: {session_id}

> Vault slim (Fase 1). Transkrip lengkap: `{rel_tx}`

## Ringkasan Sesi
{summary}

"""
    # SENGAJA TIDAK menulis `rag_context` ke berkas ini.
    #
    # `rag_context` adalah hasil retrieval RAG, dan tugasnya sudah selesai: ia dipakai
    # sebagai bahan untuk MEMBUAT `summary` di atas (lewat _vault_summary_user_body).
    # Kalau ikut ditulis ke sini, berkasnya di-embed lagi oleh `vault/**/*.md` — jadi
    # keluaran RAG masuk kembali sebagai masukan RAG. Kontaminasi berputar.
    #
    # Terpantau di 13 berkas sesi: seksi ini bahkan memuat potongan instruksi prompt
    # yang bocor ke transkrip, lengkap dengan meta-instruksi "[recorded remark omitted]" — yang lalu bisa dikembalikan RAG sebagai "ingatan".
    #
    # Kalau butuh melihat konteks apa yang dipakai saat meringkas, itu ada di
    # session_logs/, bukan di vault.
    md += f"""## Metrik
| Metrik | Nilai |
|--------|-------|
| Trigger Arti | {metrics.get('trigger_count', 0)} |
| Jawaban Arti | {metrics.get('arti_replies', 0)} |
| Chat viewer (baris) | {counts.get('viewer', 0)} |
| Streamer (baris) | {counts.get('streamer', 0)} |
| Rata-rata latency jawaban | {metrics.get('avg_latency_ms', 'n/a')} ms |

"""
    try:
        import arti_api_telemetry as tel

        summary = tel.session_summary(session_id, config)
        md += tel.format_api_usage_markdown(summary)
        for w in tel.check_quota_warnings(summary, config):
            print(w)
    except Exception:
        pass

    md += """## Cuplikan error (debug log)
"""
    if error_snippets:
        md += "```text\n" + "\n".join(error_snippets[-40:]) + "\n```\n"
    else:
        md += "- Tidak ada pola error terdeteksi di debug log.\n"

    profile = config.get("active_profile", "default").lower()
    suffix = "" if profile == "default" else f"_{profile}"
    learnings_path = _vault_dir(config, "concepts", f"arti_live_learnings{suffix}.md")
    date_tags = [f"[{d}]" for d in _session_dates()]
    md += "\n## Pengetahuan Baru (hari ini)\n"
    new_learnings: list[str] = []
    if learnings_path.is_file():
        for line in ipc.read_text(learnings_path, encoding="utf-8").splitlines():
            if any(tag in line for tag in date_tags) and line.strip().startswith("-"):
                new_learnings.append(line.strip())
    md += "\n".join(new_learnings) if new_learnings else "- Tidak ada bullet baru hari ini.\n"
    md += "\n"
    # ADR 238: one farm tag line under the title (setting `vault_farm_labels`; no farm known = unchanged).
    md = vault_farm.tag_note(md, config)

    path.write_text(md, encoding="utf-8")
    print(f"[Vault] Session slim -> {path}")
    return path


def update_vault_index(session_id: str, config: dict | None = None) -> None:
    index_path = _vault_dir(config, "sessions", "index.md")
    row = f"| {session_id.split('-')[0] if '-' in session_id else session_id} | `vault/sessions/{session_id}.md` | Fase 1 |"
    if index_path.is_file():
        text = ipc.read_text(index_path, encoding="utf-8")
        if session_id in text:
            return
        if "| Tanggal |" in text:
            text = text.rstrip() + "\n" + row + "\n"
        else:
            text += row + "\n"
    else:
        text = "# Arti Vault Sessions Index\n\n| Tanggal | File | Catatan |\n|---------|------|----------|\n" + row + "\n"
    index_path.write_text(text, encoding="utf-8")


def finalize_session_artifacts(config: dict, debug_log_path: str | Path | None = None) -> None:
    global _session_id, _manifest_path
    close_transcript_file()
    session_id = _session_id or get_session_id(config)
    tx_path = _transcript_path or (_ROOT / config.get("transcript_dir", "transcripts") / f"{session_id}.jsonl")
    dbg = Path(debug_log_path) if debug_log_path else None

    metrics = parse_transcript_metrics(tx_path)
    errors = scan_debug_log_errors(dbg)
    # Summary context can itself invoke an embedding service. Do not fetch it
    # when external summary work is disabled; local finalization still runs.
    rag_block = (fetch_rag_lite_context(session_id, tx_path, config)
                 if config.get("vault_summary_external_enabled", True) is True else "")
    summary = summarize_session_for_vault(tx_path, config, rag_context=rag_block)
    write_vault_session_md(config, summary, metrics, errors, rag_context=rag_block)
    update_vault_index(session_id, config)

    if _manifest_path and _manifest_path.is_file():
        try:
            data = json.loads(ipc.read_text(_manifest_path, encoding="utf-8"))
            data["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            data["trigger_count"] = _trigger_count
            data["vault_path"] = f"vault/sessions/{session_id}.md"
            ipc.write_json(_manifest_path, data, critical=False, fsync=False)
        except Exception as e:
            print(f"[Manifest] Gagal update: {e}")

    print(f"[Transcript] Finalized {session_id}")
