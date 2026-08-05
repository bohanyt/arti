"""Internet fast lookup — pertanyaan yang butuh info recent dicek ke web dulu.

Fitur C rencana v0.7. Terukur di spike 2026-08-02:
  groq/compound-mini : 6,8 dtk, hasil web asli + sumber (provider UTAMA, gratis)
  cursor grok-4.5/low: 17,6 dtk dengan web search (FALLBACK)
  groq/compound besar: 413 request_too_large di free tier — jangan dipakai

Pemicu SENGAJA konservatif (latensi & kuota): hanya permintaan eksplisit
("cek google...") atau topik yang melekat-waktu (berita/harga/skor/...).
"Kamu lagi ngapain sekarang?" BUKAN pertanyaan web.

Hasil disuntik ke system prompt sebagai blok [INFO INTERNET] — dibatasi
`web_lookup_max_chars`, gagal apa pun = jawab biasa tanpa blok (jangan bisu).
"""

from __future__ import annotations

import re
import time

# Permintaan eksplisit untuk mencari
_ASK_SEARCH_RE = re.compile(
    r"(cek (di )?(internet|web|google)|googling|search(in| dulu)?\b|"
    r"cari (di )?(internet|web|google)|tolong cari)",
    re.IGNORECASE,
)
# Topik yang melekat-waktu — menjawab tanpa web hampir pasti halusinasi
_TIMEBOUND_RE = re.compile(
    r"\b(berita|harga|kurs|saham|skor|klasemen|jadwal|trending|viral|"
    r"rilis terbaru|baru rilis|update terbaru|patch terbaru|versi terbaru|"
    r"pemilu|pilpres|cuaca)\b",
    re.IGNORECASE,
)


def needs_web_lookup(text: str, config: dict | None = None) -> bool:
    """Perlu cek web? Pure — target unit test."""
    cfg = config or {}
    if not cfg.get("web_lookup_enabled", False):
        return False
    t = (text or "").strip()
    if len(t) < 8:
        return False
    return bool(_ASK_SEARCH_RE.search(t) or _TIMEBOUND_RE.search(t))


def _lookup_instruction(query: str) -> str:
    return (
        "Cari di web lalu jawab RINGKAS (maks 2 kalimat, Bahasa Indonesia) "
        "+ sebut nama sumbernya singkat. Kalau tidak ketemu, bilang tidak ketemu. "
        f"Pertanyaan: {query}"
    )


def _call_groq_compound(query: str, config: dict) -> str:
    import os  # noqa: PLC0415

    import requests  # noqa: PLC0415

    key = (config.get("groq_api_key") or os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("GROQ_API_KEY kosong")
    model = str(config.get("web_lookup_groq_model", "groq/compound-mini"))
    t0 = time.monotonic()
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": _lookup_instruction(query)}],
            "max_tokens": int(config.get("web_lookup_max_tokens", 220)),
        },
        timeout=float(config.get("web_lookup_timeout_sec", 10.0)),
    )
    r.raise_for_status()
    text = (r.json()["choices"][0]["message"].get("content") or "").strip()
    if not text:
        raise RuntimeError("jawaban kosong")
    _telemetry(config, "groq_compound", model, int((time.monotonic() - t0) * 1000))
    return text


def _call_cursor(query: str, config: dict) -> str:
    import arti_cursor_agent as ca  # noqa: PLC0415 — lazy: modul jalan tanpa SDK

    r = ca.send_task("lookup", "", _lookup_instruction(query), config)
    if not r.ok or not (r.text or "").strip():
        raise RuntimeError(f"cursor lookup gagal: {r.reason}")
    _telemetry(config, "cursor", r.model or "grok-4.5/low", r.latency_ms)
    return r.text.strip()


def _telemetry(config: dict, provider: str, model: str, ms: int) -> None:
    try:
        import arti_api_telemetry as tel  # noqa: PLC0415

        tel.record_call(subsystem="web_lookup", provider=provider, model=model,
                        latency_ms=ms, ok=True, config=config)
    except Exception:  # noqa: BLE001
        pass


_PROVIDERS = {
    "groq_compound": _call_groq_compound,
    "cursor": _call_cursor,
}


def lookup(query: str, config: dict) -> tuple[str, str] | tuple[None, str]:
    """(hasil, provider) atau (None, alasan). BLOCKING — bungkus to_thread."""
    chain = list(config.get("web_lookup_provider_chain") or ["groq_compound", "cursor"])
    last_err = ""
    for name in chain:
        fn = _PROVIDERS.get(name)
        if fn is None:
            continue
        try:
            print(f"[WebLookup] Trying {name}...")
            text = fn(query, config)
            cap = int(config.get("web_lookup_max_chars", 500))
            if len(text) > cap:
                text = text[:cap].rsplit(" ", 1)[0] + "…"
            return text, name
        except Exception as e:  # noqa: BLE001
            last_err = f"{name}: {type(e).__name__}: {e}"
            print(f"[WebLookup] {last_err}"[:160])
    return None, last_err or "chain kosong"


def lookup_block(query: str, config: dict) -> str:
    """Blok siap suntik ke system prompt; string kosong kalau gagal."""
    text, src = lookup(query, config)
    if not text:
        return ""
    print(f"[WebLookup] OK via {src} ({len(text)} char)")
    return (
        "\n\n[INFO INTERNET — dicek barusan:]\n"
        f"{text}\n"
        "(Pakai kalau relevan; sebut santai mis. 'barusan aku intip sekilas'. "
        "JANGAN menyebut nama mesin/model/API.)"
    )
