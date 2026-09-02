"""Scouter provider chain — semantic digest of streamer speech + YT chat (no Groq)."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import arti_cloudflare_vision
import arti_gemini_budget
import arti_gemini_vision
import arti_github_vision
import arti_nvidia_client
import arti_ollama_vision
import arti_openrouter
import arti_text_openai as text_oai
import arti_zai_vision
from arti_vision_openai import is_rate_limit_error

_scouter_lock = threading.Lock()
_last_uptime_log_ts = 0.0

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Urutan dari log 9 jam [date removed]: Gemini murah saat 429, OpenRouter
# 201/205 (98%). Cloudflare 0/205 karena thinking Gemma menghabiskan output,
# diperbaiki [date removed] dan probe JSON lulus 2,1 dtk; tetap di belakang incumbent
# sampai punya sampel live baru. Ollama 4/4, Z.ai 0/4, NVIDIA tetap terakhir.
DEFAULT_CHAIN = [
    "google_gemini",
    "openrouter",
    "cloudflare",
    "ollama",
    "zai",
    "nvidia",
]

# GitHub Models pensiun TOTAL 30 Juli 2026 (diumumkan 1 Juli, brownout 16 & 23
# Juli). Dibuang dari semua rantai [date removed] — sudah dua minggu mati sementara
# Arti masih memanggilnya. Nama ini ditolak SECARA PAKSA, bukan sekadar dihapus
# dari default: `config_local.json` tidak dilacak git dan hidup per-mesin, jadi
# salinan basi bisa menghidupkan kembali provider mati tanpa suara. Lihat
# docs/MODEL-REGISTRY.md §2.1.
PROVIDER_PENSIUN = {"github"}

# "lihat/liat" POLOS bukan pertanyaan layar. Live [date removed]: "aku mau liat
# [mukamu]" membuka vision window -> turn memblokir 80-85 dtk (nvidia timeout
# + fallback). Verba lihat hanya dihitung kalau ada objek layar di dekatnya
# atau berbentuk "apa yang kamu liat".
SCREEN_KEYWORDS = re.compile(
    r"\b(layar|screen|monitor|tampilan|scene|di layar|on screen"
    r"|apa yang (kamu )?(terlihat|keliatan|lihat|liat)"
    r"|(lihat|liat|liatin|cek) ?(in)? (layar|screen|monitor|game|video|browser|tab)"
    r"|(lihat|liat|nonton) (video|game) apa"
    r"|nonton video|video apa)\b",
    re.IGNORECASE,
)

SCOUTER_JSON_SCHEMA = """{
  "summary": "1-2 kalimat ID — omongan streamer + chat",
  "emotion": "senang|sedih|marah|bingung|excited|neutral",
  "topic": "1-3 kata",
  "important_facts": ["HANYA fakta tahan lama — biasanya [] "],
  "screen_relevant": false,
  "screen_hint": null,
  "curious_worthy": false,
  "curious_hook": null
}"""


@dataclass
class ScouterUptime:
    last_ok_ts: float = 0.0
    last_provider: str = ""
    consecutive_failures: int = 0
    total_ok: int = 0
    total_fail: int = 0
    chain_fallback_count: int = 0


@dataclass
class ScouterResult:
    summary: str = ""
    emotion: str = "neutral"
    topic: str = ""
    important_facts: list[str] = field(default_factory=list)
    screen_relevant: bool = False
    screen_hint: str | None = None
    curious_worthy: bool = False
    curious_hook: str | None = None
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "emotion": self.emotion,
            "topic": self.topic,
            "important_facts": list(self.important_facts),
            "screen_relevant": self.screen_relevant,
            "screen_hint": self.screen_hint,
            "curious_worthy": self.curious_worthy,
            "curious_hook": self.curious_hook,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, provider: str = "") -> ScouterResult:
        facts = data.get("important_facts") or []
        if not isinstance(facts, list):
            facts = [str(facts)]
        return cls(
            summary=str(data.get("summary") or "").strip(),
            emotion=str(data.get("emotion") or "neutral").strip() or "neutral",
            topic=str(data.get("topic") or "").strip(),
            important_facts=[str(f).strip() for f in facts if str(f).strip()],
            screen_relevant=_as_bool(data.get("screen_relevant")),
            screen_hint=_null_str(data.get("screen_hint")),
            curious_worthy=_as_bool(data.get("curious_worthy")),
            curious_hook=_null_str(data.get("curious_hook")),
            provider=provider,
        )


scouter_uptime = ScouterUptime()


def _as_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "ya")
    return bool(val)


def _null_str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("null", "none", "n/a", "-"):
        return None
    return s


def _scouter_params(config: dict) -> tuple[int, float, float]:
    max_tokens = int(config.get("scouter_max_tokens", 350))
    temperature = float(config.get("scouter_temperature", 0.2))
    timeout = float(config.get("scouter_timeout_sec", 45))
    return max_tokens, temperature, timeout


def has_screen_keywords(text: str) -> bool:
    """Cheap pre-gate for timer priority — not a substitute for LLM."""
    return bool(SCREEN_KEYWORDS.search(text or ""))


def build_scouter_prompt(context_text: str) -> str:
    return f"""Analisis percakapan live stream VTuber (15 kejadian terakhir):

{context_text}

Tugas:
- Ringkas omongan streamer DAN chat penonton.
- Deteksi apakah pembicaraan merujuk ke apa yang terlihat di layar (game, video, UI).
- Fakta kanon: Arti debut co-host 27 Mei 2026. Frasa "baru mulai live stream" di summary = sesi hari ini, bukan tanggal lahir karakter.

important_facts — DEFAULT-nya kosong `[]`. Ini masuk memori jangka panjang Arti
selamanya, jadi ambangnya tinggi. Isi HANYA kalau fakta itu masih berguna bulan depan:
  BOLEH  : cerita pribadi streamer, preferensi tetap, rencana/target, nama & detail
           penonton, keputusan yang diambil.
  DILARANG: apa pun yang cuma berlaku detik ini ("streamer lagi buka Google Docs",
           "ada beberapa tab terbuka", "Arti melihat ...", "streamer sedang live"),
           hal yang sudah pasti diketahui (tanggal debut Arti, nama streamer),
           dan pengulangan fakta yang sudah pernah kamu sebut dengan kalimat berbeda.
Kalau ragu, kosongkan. Memori penuh sampah bikin Arti menjawab generik.

curious_hook — sudut komentar proaktif, bukan laporan layar. Isi hanya kalau kamu punya
sesuatu yang spesifik dan bikin streamer PENGEN menanggapi: detail aneh, pilihan yang
mengejutkan, kontradiksi, atau hal yang baru berubah. Sebut hal konkret (nama, angka,
error, judul). DILARANG frasa kosong seperti "streamer sedang ...", "layar menampilkan
...", "kayaknya lagi ...", "sedang melihat ..." — hook seperti itu akan dibuang.
DILARANG juga hook dari log internal sistem Arti sendiri (baris [Scouter]/[Vision]/
screen_relevant/jumlah pesan sejarah yang dibaca) — itu dapur, bukan konten stream.
curious_worthy = true hanya kalau layar relevan DAN curious_hook lolos standar di atas.

Output HANYA JSON:
{SCOUTER_JSON_SCHEMA}"""


def parse_scouter_response(raw: str) -> ScouterResult | None:
    data = _parse_json_blob(raw)
    if not data:
        return None
    result = ScouterResult.from_dict(data)
    if not result.summary:
        return None
    return result


def _balanced_objects(text: str) -> list[str]:
    """SEMUA potongan objek ber-kurung seimbang, urut — sadar string & escape.

    Perlu karena "dari { pertama sampai } terakhir" pecah kalau model menulis
    prosa ber-kurung, dua objek berturut-turut, atau menutup dengan kalimat.
    Semua kandidat dikembalikan (bukan yang pertama saja): kurung prosa
    seperti "Catatan {penting}:" boleh gagal parse, JSON di belakangnya tetap
    ketemu.
    """
    out: list[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            if depth > 0:
                in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start:i + 1])
                start = -1
    return out


def _parse_json_blob(text: str) -> dict | None:
    """JSON dari balasan model — toleran gaya keluaran yang berbeda-beda.

    Scouter kini dilayani composer-2.5 (revisi biaya 2026-08-03), model
    agen-koding yang lebih sering membungkus jawaban dengan fence markdown
    atau menambah kalimat pengantar/penutup ketimbang grok. Kegagalan parse
    di sini TIDAK berisik: parse_scouter_response mengembalikan None ->
    curious_worthy selamanya False -> curious layar mati diam-diam. Jadi
    urutannya: teks polos -> isi fence -> objek seimbang pertama -> cara
    lama ({ pertama .. } terakhir, untuk JSON yang terpotong rapi).
    """
    if not text:
        return None
    candidates: list[str] = [text.strip()]
    for m in re.finditer(r"```(?:json)?\s*(.+?)```", text, re.DOTALL | re.IGNORECASE):
        candidates.append(m.group(1).strip())
    candidates.extend(_balanced_objects(text))
    # Jangkar `{"` (audit ronde-3): kurung-buka HANTU di dalam kutipan prosa
    # ('Catatan "{" penting') membuat scan dari awal salah hitung depth dan
    # menelan JSON asli di belakangnya. Objek JSON nyata praktis selalu
    # diawali `{"`, jadi scan ulang dari tiap jangkar itu (dibatasi 8).
    for m in list(re.finditer(r'\{\s*"', text))[:8]:
        objs = _balanced_objects(text[m.start():])
        if objs:
            candidates.append(objs[0])
    start, end = text.find("{"), text.rfind("}") + 1
    if start >= 0 and end > start:
        candidates.append(text[start:end])

    for cand in candidates:
        if not cand:
            continue
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _resolve_chain(config: dict) -> list[str]:
    chain = list(config.get("scouter_provider_chain") or DEFAULT_CHAIN)
    out: list[str] = []
    for name in chain:
        if name == "groq":
            continue
        if name == "cursor" and not _cursor_ok(config):
            continue
        if name == "nvidia" and not arti_nvidia_client.resolve_api_key(config):
            continue
        if name == "cloudflare":
            if not arti_cloudflare_vision.resolve_token(config):
                continue
            if not arti_cloudflare_vision.resolve_account_id(config):
                continue
        if name == "openrouter" and not (
            config.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY")
        ):
            continue
        if name == "google_gemini":
            if not arti_gemini_vision.resolve_api_key(config):
                continue
            # Rem kuota ([date removed]): selagi jatah menit habis / istirahat
            # pasca-429, Gemini dikeluarkan dari rantai giliran ini — TANPA
            # baris error. Sesi [date removed]: 186x HTTP 429 di terminal karena
            # rantai menembak provider yang baru saja ditolak.
            model_scout = config.get("scouter_gemini_model") or config.get(
                "vision_google_gemini_model"
            ) or "gemini-3.1-flash-lite"
            if arti_gemini_budget.sedang_dibatasi(model_scout, config):
                continue
        if name in PROVIDER_PENSIUN:
            continue
        if name == "zai" and not arti_zai_vision.resolve_api_key(config):
            continue
        if name == "ollama" and not arti_ollama_vision.resolve_api_key(config):
            continue
        out.append(name)
    return out


def _record_success(provider: str) -> None:
    scouter_uptime.last_ok_ts = time.time()
    scouter_uptime.last_provider = provider
    scouter_uptime.consecutive_failures = 0
    scouter_uptime.total_ok += 1


def _record_failure() -> None:
    scouter_uptime.consecutive_failures += 1
    scouter_uptime.total_fail += 1


def _maybe_log_uptime(result: ScouterResult | None = None) -> None:
    global _last_uptime_log_ts
    now = time.time()
    if now - _last_uptime_log_ts < 60.0:
        return
    _last_uptime_log_ts = now
    u = scouter_uptime
    extra = ""
    if result:
        extra = (
            f" screen_relevant={result.screen_relevant}"
            f" curious_worthy={result.curious_worthy}"
        )
    print(
        f"[Scouter] uptime ok={u.total_ok} fail={u.total_fail} "
        f"provider={u.last_provider or '-'}{extra}"
    )


ProviderFn = Callable[[str, dict], tuple[str, int]]


def _messages(prompt: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": prompt}]


def _call_cursor(prompt: str, config: dict) -> tuple[str, int]:
    """Sesi Cursor per-role: default 'scout' (composer-2.5, revisi streamer
    2026-08-03 — grok tiap menit kemahalan); observer menimpa via
    config["cursor_role"]="observer" (grok-4.5/high, hanya akhir live).

    Keputusan streamer 2026-08-01: Cursor jadi provider utama (langganan setahun,
    pool Cursor Models), chain API gratis turun jadi fallback. Gagal apa pun ->
    raise, supaya loop chain lanjut ke provider berikutnya seperti biasa.
    """
    import arti_cursor_agent as ca  # noqa: PLC0415 — lazy: modul ini tanpa SDK tetap jalan

    role = str(config.get("cursor_role") or "scout")
    r = ca.send_task(role, "", prompt, config)
    if not r.ok:
        raise RuntimeError(f"cursor {role} gagal: {r.reason}")
    try:
        import arti_api_telemetry as tel  # noqa: PLC0415

        # Subsystem & model JUJUR (audit [date removed]): dulu semua dicatat
        # subsystem="scouter" — panggilan observer ikut nempel di sana
        # (89 baris dobel hari itu) dan `r.model or role` menaruh nama ROLE
        # di kolom model. Sesudah scouter/observer beda model, salah label
        # begini merusak analisis biaya berikutnya — analisis yang jadi
        # dasar keputusan pindah model.
        model, effort = ca.resolve_role_model(role, config)
        tel.record_call(
            subsystem=str(
                config.get("telemetry_subsystem")
                or ("observer" if role == "observer" else "scouter")
            ),
            provider="cursor",
            model=r.model or (f"{model}/{effort}" if effort else model),
            latency_ms=r.latency_ms, ok=True, config=config,
        )
    except Exception:  # noqa: BLE001
        pass
    return r.text or "", r.latency_ms


def _cursor_ok(config: dict) -> bool:
    try:
        import arti_cursor_agent as ca  # noqa: PLC0415

        ok, _why = ca.is_available(config)
        return ok
    except Exception:  # noqa: BLE001
        return False


def _call_nvidia(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    model = config.get("scouter_nvidia_model") or config.get("nvidia_model")
    return arti_nvidia_client.chat_completion(
        _messages(prompt),
        config=config,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        telemetry_subsystem=str(config.get("telemetry_subsystem") or "scouter"),
    )


def _call_cloudflare(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    return arti_cloudflare_vision.text_chat(
        _messages(prompt),
        config=config,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )


# Probe [date removed] (budget scouter asli 350): TANPA param ini semua Nemotron
# menghabiskan seluruh budget untuk chain-of-thought — finish=length, JSON tak
# pernah tertulis, dan incumbent 120B mengembalikan CoT Inggris mentah. Varian
# effort=low juga DIUJI dan diabaikan provider free (CoT tetap 666-700 token
# bahkan di budget 700), jadi satu-satunya setelan yang menjaga jawaban utuh
# di budget ketat adalah mematikan reasoning via param API terpadu OpenRouter.
OPENROUTER_TANPA_NALAR = {"reasoning": {"enabled": False}}


def _call_openrouter(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    key = (config.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    models = list(config.get("scouter_openrouter_models") or [])
    if not models:
        models = [
            config.get("openrouter_summarizer_model", "nvidia/nemotron-3-super-120b-a12b:free"),
            config.get("openrouter_summarizer_fallback", "nvidia/nemotron-3.5-lightning:free"),
            "google/gemma-4-26b-a4b-it:free",
        ]
    last_err = ""
    for model in models:
        if not model:
            continue
        try:
            text, ms = text_oai.text_chat(
                OPENROUTER_URL,
                key,
                model,
                _messages(prompt),
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                extra_headers={
                    "HTTP-Referer": "https://github.com/YOUR_USER/YOUR_REPO",
                    "X-Title": "Arti Scouter",
                },
                extra_payload=OPENROUTER_TANPA_NALAR,
                telemetry_subsystem=str(
                    config.get("telemetry_subsystem") or "scouter"
                ),
            )
            if text:
                return text, ms
        except Exception as e:
            last_err = str(e)
            if is_rate_limit_error(e):
                continue
            continue
    if last_err:
        raise RuntimeError(last_err)
    raise RuntimeError("OpenRouter scouter: all models failed")


def _call_google_gemini(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    return arti_gemini_vision.text_generate(
        prompt,
        config=config,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        telemetry_subsystem=str(config.get("telemetry_subsystem") or "scouter"),
    )


# PENSIUN 30 Juli 2026 — sengaja TIDAK didaftarkan di _PROVIDERS. Kodenya
# disimpan sebagai arsip (docs/MODEL-REGISTRY.md §2.1), bukan untuk dipakai.
def _call_github(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    return arti_github_vision.text_chat(
        _messages(prompt),
        config=config,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )


def _call_zai(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    return arti_zai_vision.text_chat(
        _messages(prompt),
        config=config,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )


def _call_ollama(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    return arti_ollama_vision.text_chat(
        _messages(prompt),
        config=config,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )


_PROVIDERS: dict[str, ProviderFn] = {
    "cursor": _call_cursor,
    "nvidia": _call_nvidia,
    "cloudflare": _call_cloudflare,
    "openrouter": _call_openrouter,
    "google_gemini": _call_google_gemini,
    # "github" SENGAJA TIDAK ADA DI SINI. Membuangnya dari rantai + PROVIDER_PENSIUN
    # saja TIDAK cukup: arti_observer_client memakai _PROVIDERS.get(name) LANGSUNG
    # tanpa lewat _resolve_chain, jadi guard di sana terlewat dan observer tetap
    # memanggil provider mati (dibuktikan dengan menjebak fungsinya, [date removed]).
    # Pintu dispatch adalah satu-satunya tempat yang menutup SEMUA pemanggil.
    "zai": _call_zai,
    "ollama": _call_ollama,
}


def run_chain(context_text: str, config: dict) -> ScouterResult | None:
    """Run scouter chain on history text; returns parsed result or None."""
    if not context_text.strip():
        return None

    prompt = build_scouter_prompt(context_text)
    chain = _resolve_chain(config)
    if not chain:
        print("[Scouter] No providers available (check API keys).")
        return None

    last_err = ""
    acquired = _scouter_lock.acquire(blocking=False)
    if not acquired:
        _scouter_lock.acquire()
        acquired = True

    try:
        for idx, name in enumerate(chain):
            fn = _PROVIDERS.get(name)
            if not fn:
                continue
            if idx > 0:
                scouter_uptime.chain_fallback_count += 1
            try:
                print(f"[Scouter] Trying {name}...")
                raw, ms = fn(prompt, config)
                result = parse_scouter_response(raw)
                if result is None:
                    last_err = f"{name}: bad JSON"
                    print(f"[Scouter] {name} parse fail ({ms}ms)")
                    continue
                result.provider = name
                _record_success(name)
                _maybe_log_uptime(result)
                print(
                    f"[Scouter] OK {name} ({ms}ms) "
                    f"screen_relevant={result.screen_relevant} "
                    f"curious_worthy={result.curious_worthy}"
                )
                return result
            except Exception as e:
                last_err = f"{name}: {e}"
                print(f"[Scouter] {name} fail: {type(e).__name__}: {e}")
                if is_rate_limit_error(e):
                    continue
                continue

        _record_failure()
        _maybe_log_uptime()
        if last_err:
            print(f"[Scouter] All providers failed — last: {last_err}")
        return None
    finally:
        if acquired:
            _scouter_lock.release()


def run(context_text: str, config: dict) -> dict | None:
    """Backward-compat dict return for summarizer callers."""
    result = run_chain(context_text, config)
    return result.to_dict() if result else None
