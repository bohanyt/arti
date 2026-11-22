"""Kesehatan provider per sesi: probe awal + circuit breaker bersama (2026-10-23).

Permintaan Bohan: "[recorded remark omitted]". Smoke 2026-10-23 01:31: Agy ditolak
(`--effort` tidak didukung model) dan DICOBA LAGI tiap giliran (~80x),
Gemini scouter 503 x13, Cursor observer breaker, OpenRouter 429 saat start.

Satu modul, dikonsultasikan semua titik rantai provider:

- ``is_available(name)``   -> boleh dicoba sekarang? (urutan rantai TIDAK diubah)
- ``record_success(name)`` / ``record_failure(name, err_class)``
- ``run_startup_probes(...)`` -> satu panggilan murah per provider, paralel,
  total <= ``provider_probe_budget_sec``; yang belum selesai = WARN/unknown.

Kelas galat:
- KERAS (``auth``, ``model_rejected``, ``init_error``): FAIL di probe startup =
  provider MATI untuk sisa sesi (dilog SEKALI, tanpa retry). Saat LIVE auth langsung mati; galat keras lain
  butuh N kejadian BERUNTUN (``provider_breaker_failures``).
- SEMENTARA (``rate_limit``, ``unavailable``, ``timeout``, ``network``) dan
  ``unknown``: di probe = WARN saja (tidak menggerbang). Saat live: N gagal
  beruntun -> breaker terbuka ``provider_breaker_open_sec``, lalu di-probe
  ulang SEKALI di latar. OK -> tutup, gagal -> buka lagi. Vonis mati selalu
  menang atas OK yang telat.
- ``cancelled``: bukan sukses, bukan gagal (lihat arti_executors: barge-in vs
  wait_for habis = "abandoned" -> dihitung timeout).
- Rantai boleh kosong: OPEN/dead tidak dicoba ulang; Codex tetap dilindungi.
- Probe murah: hanya 2 entri terdepan per rantai, endpoint gratis (/models,
  /key) bila ada, tanpa gambar.

Codex (ChatGPT login) = cadangan terakhir ("[recorded remark omitted]"). Nama ``codex`` DILINDUNGI di posisi mana pun dalam rantai: breaker /
dead-skip TIDAK pernah membuangnya. Kalau dia down, satu baris KERAS sekali
per episode: ``[Health] !!! Codex (last resort) DOWN: <kelas>``.

Mati total lewat ``provider_probe_enabled: False`` (probe + breaker + skip) =
perilaku lama. Modul DIAM (semua tersedia, record no-op) sampai ``configure``
dipanggil bridge -> skrip/tes lain tidak terpengaruh.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

HARD = frozenset({"auth", "model_rejected", "init_error"})
TRANSIENT = frozenset({"rate_limit", "unavailable", "timeout", "network"})
PROTECTED_BASE = frozenset({"codex"})
# Giliran DIBATALKAN (barge-in PTT / passive-voice, asyncio.CancelledError,
# telemetri outcome "cancelled") = bukan gagal, bukan sukses: tidak dicatat.
CANCELLED = "cancelled"

DEFAULT_BUDGET_SEC = 12.0
DEFAULT_FAILURES = 3
DEFAULT_OPEN_SEC = 600.0


@dataclass
class ProbeResult:
    status: str          # OK | WARN | FAIL
    err_class: str = ""  # kosong kalau OK
    detail: str = ""


@dataclass
class _State:
    status: str = "ok"          # ok | open | dead
    failures: int = 0
    err_class: str = ""
    opened_at: float = 0.0
    half_open: bool = False
    reprobe_inflight: bool = False
    loud_logged: bool = False
    hard_streak: int = 0        # galat keras BERUNTUN saat live (vonis mati butuh N)


_lock = threading.RLock()
_enabled = False
_fail_limit = DEFAULT_FAILURES
_open_sec = DEFAULT_OPEN_SEC
_states: dict[str, _State] = {}
_probes: dict[str, Callable[[], ProbeResult]] = {}

# Titik suntik untuk tes (jam palsu, thread sinkron, log tertangkap).
_clock: Callable[[], float] = time.monotonic
_log: Callable[[str], None] = print


def _spawn_default(fn: Callable[[], None], name: str) -> None:
    threading.Thread(target=fn, daemon=True, name=name).start()


_spawn: Callable[[Callable[[], None], str], None] = _spawn_default


# --------------------------------------------------------------------------- #
# Konfigurasi
# --------------------------------------------------------------------------- #


def configure(config: dict | None) -> bool:
    """Bridge memanggil ini sekali saat start. True = modul aktif."""
    global _enabled, _fail_limit, _open_sec
    cfg = config or {}
    with _lock:
        _enabled = bool(cfg.get("provider_probe_enabled", True))
        try:
            _fail_limit = int(cfg.get("provider_breaker_failures", DEFAULT_FAILURES))
        except (TypeError, ValueError):
            _fail_limit = DEFAULT_FAILURES
        try:
            _open_sec = float(cfg.get("provider_breaker_open_sec", DEFAULT_OPEN_SEC))
        except (TypeError, ValueError):
            _open_sec = DEFAULT_OPEN_SEC
    return _enabled


def enabled() -> bool:
    return _enabled


def reset() -> None:
    """Kosongkan semua status (tes / sesi baru). Modul kembali DIAM."""
    global _enabled, _fail_limit, _open_sec
    with _lock:
        _enabled = False
        _fail_limit = DEFAULT_FAILURES
        _open_sec = DEFAULT_OPEN_SEC
        _states.clear()
        _probes.clear()


def budget_sec(config: dict | None) -> float:
    try:
        return max(0.5, float((config or {}).get("provider_probe_budget_sec", DEFAULT_BUDGET_SEC)))
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_SEC


def is_protected(name: str) -> bool:
    """Codex dilindungi BERDASARKAN NAMA, di posisi mana pun dalam rantai."""
    base = str(name or "").split(":", 1)[0].strip().lower()
    return base in PROTECTED_BASE


def chain_key(name: str, config: dict | None = None, *, lane: str = "") -> str:
    """Kunci status untuk entri rantai. Cursor per role; vision/web punya lajur."""
    n = str(name or "").strip()
    if n == "cursor":
        role = str({"web": "lookup", "vision": "vision"}.get(lane)
                   or (config or {}).get("cursor_role") or "scout")
        return f"cursor:{role}"
    if is_protected(n):
        return n
    return f"{lane}:{n}" if lane else n


# --------------------------------------------------------------------------- #
# Klasifikasi galat
# --------------------------------------------------------------------------- #

_MODEL_PAT = (
    "invalid model", "not supported for model", "model_not_found", "model not found",
    "unknown model", "model_decommissioned", "has been decommissioned",
    "is not a valid model", "unsupported model",
    "does not exist",
)
# OpenRouter 404 "No endpoints found for <model>" = model :free sedang tidak
# punya penyedia (sementara), BUKAN model/akun mati. Dicek SEBELUM kode 404.
_TRANSIENT_404_PAT = ("no endpoints found",)
_AUTH_PAT = (
    "unauthorized", "invalid api key", "invalid_api_key", "api key not valid",
    "api_key_invalid", "incorrect api key", "authentication", "not logged in",
    "login required", "missing api key", "key kosong", "token kosong",
)
_RATE_PAT = ("rate limit", "rate_limit", "ratelimit", "too many requests",
             "resource_exhausted", "quota")
_UNAVAIL_PAT = ("unavailable", "overloaded", "bad gateway", "internal server error",
                "gateway timeout", "server error")
_TIMEOUT_PAT = ("timeout", "timed out", "deadline")
_NET_PAT = ("connection", "getaddrinfo", "name resolution", "nodename",
            "refused", "unreachable", "ssl", "network", "dns", "remote end closed")
_INIT_PAT = ("init gagal", "init timeout", "process exit", "tidak terpasang",
             "not installed", "no such file", "filenotfound", "scratch dir",
             "agy menolak", "pemanasan gagal")

_CODE_RE = re.compile(r"(?:http|status|code)[^0-9a-z]{0,4}([1-5]\d\d)\b")


def classify_status(code: Any) -> str:
    """Kelas dari kode HTTP (int/str). Tidak dikenal = 'unknown'."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "unknown"
    if c in (401, 403):
        return "auth"
    if c == 404:
        return "model_rejected"
    if c == 408:
        return "timeout"
    if c == 429:
        return "rate_limit"
    if 500 <= c <= 599:
        return "unavailable"
    return "unknown"


def classify(err: Any, default: str = "unknown") -> str:
    """Petakan exception/teks galat ke satu kelas. Urutan = paling spesifik dulu."""
    if err is None:
        return default
    if isinstance(err, BaseException):
        tname = type(err).__name__.lower()
        if "cancelled" in tname or "canceled" in tname:
            return CANCELLED
        if isinstance(err, TimeoutError) or "timeout" in tname:
            return "timeout"
        if isinstance(err, ConnectionError) or "connectionerror" in tname:
            return "network"
        text = f"{type(err).__name__}: {err}"
    else:
        text = str(err)
    t = text.lower()
    if t.strip() in ("cancelled", "canceled", "aborted") or "cancellederror" in t:
        return CANCELLED
    if any(p in t for p in _TRANSIENT_404_PAT):
        return "unavailable"
    if any(p in t for p in _MODEL_PAT):
        return "model_rejected"
    m = _CODE_RE.search(t)
    if m:
        by_code = classify_status(m.group(1))
        if by_code == "model_rejected" and "model" not in t:
            by_code = "unknown"
        if by_code != "unknown":
            return by_code
    if re.search(r"\b429\b", t) or any(p in t for p in _RATE_PAT):
        return "rate_limit"
    if re.search(r"\b(401|403)\b", t) or any(p in t for p in _AUTH_PAT):
        return "auth"
    if re.search(r"\b50[0234]\b", t) or any(p in t for p in _UNAVAIL_PAT):
        return "unavailable"
    if any(p in t for p in _TIMEOUT_PAT):
        return "timeout"
    if any(p in t for p in _NET_PAT):
        return "network"
    if any(p in t for p in _INIT_PAT):
        return "init_error"
    return default


# --------------------------------------------------------------------------- #
# Status runtime (breaker)
# --------------------------------------------------------------------------- #


def _st(name: str) -> _State:
    st = _states.get(name)
    if st is None:
        st = _State()
        _states[name] = st
    return st


def _loud_codex(name: str, st: _State, cls: str) -> None:
    if not st.loud_logged:
        st.loud_logged = True
        _log(f"[Health] !!! Codex (last resort) DOWN: {cls} ({name}) — tetap dicoba, "
             "maks sekali per giliran")


def _mark_dead(name: str, cls: str, why: str = "") -> None:
    st = _st(name)
    if st.status == "dead":
        return
    st.status, st.err_class, st.half_open = "dead", cls, False
    if is_protected(name):
        _loud_codex(name, st, cls)
        return
    tail = f" — {why}" if why else ""
    _log(f"[Health] {name} DEAD ({cls}) — dilewati sampai akhir sesi, tanpa retry{tail}")


def _mark_open(name: str, cls: str, *, reopen: bool = False) -> None:
    st = _st(name)
    was = st.status
    st.status, st.err_class = "open", cls
    st.opened_at = _clock()
    st.half_open = False
    if is_protected(name):
        if was == "ok":
            _loud_codex(name, st, cls)
        return
    if was != "open" or reopen:
        menit = max(1, int(round(_open_sec / 60.0)))
        kata = "OPEN lagi" if was == "open" else "OPEN"
        _log(f"[Health] {name} {kata} ({cls}) — dilewati {menit}m, lalu probe ulang di latar")


def _mark_closed(name: str) -> None:
    st = _st(name)
    was = st.status
    if was == "dead" and not is_protected(name):
        return  # vonis mati selalu menang
    st.status, st.failures, st.err_class = "ok", 0, ""
    st.hard_streak = 0
    st.half_open = False
    st.loud_logged = False
    if was != "ok":
        tag = "Codex (last resort)" if is_protected(name) else name
        _log(f"[Health] {tag} CLOSED — OK lagi, kembali ke rantai")


def is_available(name: str) -> bool:
    """Boleh dicoba sekarang? Dilindungi (codex) SELALU True. Modul diam = True."""
    if not _enabled or is_protected(name):
        return True
    with _lock:
        st = _states.get(name)
        if st is None or st.status == "ok":
            return True
        if st.status == "dead":
            return False
        if st.half_open:
            return True
        if _clock() - st.opened_at < _open_sec:
            return False
        probe = _probes.get(name)
        if probe is None:
            # Tanpa probe terdaftar: setengah-buka — satu percobaan berikutnya
            # menentukan (gagal sekali = buka lagi).
            st.half_open = True
            return True
        if not st.reprobe_inflight:
            st.reprobe_inflight = True
            _spawn(lambda: _reprobe(name, probe), f"health-reprobe-{name}")
        return False


def usable(names, key_fn: Callable[[str], str] = lambda n: n) -> set:
    """Entri rantai yang boleh dicoba giliran ini (urutan diurus pemanggil).

    OPEN/dead tetap dilewati meskipun tidak ada alternatif. Codex dilindungi.
    """
    names = list(names)
    if not _enabled:
        return set(names)
    boleh = {n for n in names if is_available(key_fn(n))}
    if boleh:
        return boleh
    # Empty is a valid result. Retrying an OPEN provider defeats the breaker.
    # The protected ChatGPT/Codex last resort remains available above.
    return set()


def _call_outcome() -> str | None:
    """Nasib panggilan executor yang sedang berjalan (lihat arti_executors):
    None = normal, "cancelled" = barge-in/PTT (bukan hasil), "abandoned" =
    ditinggal penunggunya (wait_for habis) -> dihitung timeout."""
    try:
        import arti_executors  # noqa: PLC0415

        return arti_executors.current_call_outcome()
    except Exception:  # noqa: BLE001
        return None


def record_success(name: str) -> None:
    if not _enabled:
        return
    nasib = _call_outcome()
    if nasib == CANCELLED:
        return
    if nasib == "abandoned":
        # Jawaban datang SETELAH penunggu menyerah (wait_for) = terlalu lambat.
        record_failure(name, "timeout")
        return
    with _lock:
        st = _states.get(name)
        if st is None:
            return
        if st.status == "dead" and not is_protected(name):
            return  # vonis mati menang
        if st.status != "ok":
            _mark_closed(name)
        else:
            st.failures = 0
            st.hard_streak = 0


def record_failure(name: str, err_class: str = "unknown") -> None:
    if not _enabled:
        return
    cls = str(err_class or "unknown")
    if cls == CANCELLED or _call_outcome() == CANCELLED:
        return  # dibatalkan: tidak menyentuh hitungan sama sekali
    with _lock:
        st = _st(name)
        if st.status == "dead":
            return
        st.failures += 1
        st.err_class = cls
        # Auth cannot recover without a credential change: dead for the session.
        # Other hard failures retain the configured consecutive-failure limit.
        st.hard_streak = st.hard_streak + 1 if cls in HARD else 0
        if cls == "auth" or (_fail_limit > 0 and st.hard_streak >= _fail_limit):
            _mark_dead(name, cls, f"{st.hard_streak} galat keras beruntun saat live")
            return
        if st.status == "open":
            if st.half_open:
                _mark_open(name, cls, reopen=True)
            return
        if _fail_limit > 0 and st.failures >= _fail_limit:
            _mark_open(name, cls)


def record_outcome(name: str, *, ok: bool, extra: dict | None = None,
                   err: Any = None) -> None:
    """Catat satu hasil berbentuk baris telemetri.

    Baris ``extra.outcome == "cancelled"`` (giliran mic_pasif dibatalkan PTT,
    bridge-passive-voice 57ebb939: ``ok=True``) dan ``asyncio.CancelledError``
    TIDAK dihitung sukses maupun gagal.
    """
    if isinstance(extra, dict) and str(extra.get("outcome") or "").lower() == CANCELLED:
        return
    if err is not None and classify(err) == CANCELLED:
        return
    if ok:
        record_success(name)
    else:
        record_failure(name, classify(err) if err is not None else "unknown")


def apply_probe(name: str, res: ProbeResult) -> None:
    """Terapkan hasil probe (startup atau re-probe) ke status."""
    """Hanya FAIL keras yang menggerbang (vonis mati instan). Galat sementara
    di probe = WARN: provider TETAP tertutup-normal (jaringan belum siap / 429
    sesaat saat start tidak boleh menyingkirkan provider 10 menit)."""
    if not _enabled:
        return
    with _lock:
        if res.status == "OK":
            st = _states.get(name)
            if st is not None and st.status == "open":
                _mark_closed(name)
            elif st is not None and st.status == "ok":
                st.failures = 0
            return
        if res.status != "FAIL":
            return  # WARN/unknown/sementara: tidak menggerbang
        cls = res.err_class or "unknown"
        if cls in HARD:
            _mark_dead(name, cls, "probe")


def _reprobe(name: str, probe: Callable[[], ProbeResult]) -> None:
    try:
        res = _safe_probe(probe)
        with _lock:
            st = _st(name)
            if st.status == "dead":
                pass  # vonis mati (mis. dari jalur lain) menang atas OK telat
            elif res.status == "OK":
                _mark_closed(name)
            elif res.status == "FAIL" and res.err_class in HARD:
                _mark_dead(name, res.err_class, "probe ulang")
            else:
                _mark_open(name, res.err_class or "unknown", reopen=True)
            st.reprobe_inflight = False
    except Exception:  # noqa: BLE001 — diagnostik tidak boleh menjatuhkan bridge
        with _lock:
            _st(name).reprobe_inflight = False


def register_probe(name: str, fn: Callable[[], ProbeResult]) -> None:
    with _lock:
        _probes[name] = fn


def state_of(name: str) -> str:
    st = _states.get(name)
    return st.status if st else "ok"


def summary_line() -> str:
    """'providers: codex OK, groq OK, google_gemini OPEN(unavailable) 7m, agy DEAD(model_rejected)'"""
    with _lock:
        names = list(dict.fromkeys(list(_probes) + list(_states)))
        parts = []
        now = _clock()
        for name in names:
            st = _states.get(name)
            if st is None or st.status == "ok":
                parts.append(f"{name} OK")
            elif st.status == "dead":
                parts.append(f"{name} DEAD({st.err_class})")
            else:
                sisa = max(0.0, _open_sec - (now - st.opened_at))
                parts.append(f"{name} OPEN({st.err_class}) {int((sisa + 59) // 60)}m")
    return "providers: " + (", ".join(parts) if parts else "-")


# --------------------------------------------------------------------------- #
# Probe startup
# --------------------------------------------------------------------------- #


def _verdict(cls: str, detail: str) -> ProbeResult:
    """Probe: hanya galat KERAS = FAIL. Sementara/unknown = WARN (tidak menggerbang)."""
    return ProbeResult("FAIL" if cls in HARD else "WARN", cls, detail)


def _safe_probe(fn: Callable[[], Any]) -> ProbeResult:
    try:
        res = fn()
    except Exception as exc:  # noqa: BLE001
        return _verdict(classify(exc), f"{type(exc).__name__}: {str(exc)[:120]}")
    if isinstance(res, ProbeResult):
        if res.status == "FAIL":
            return _verdict(res.err_class or "unknown", res.detail)
        return res
    return ProbeResult("OK", "", str(res or "")[:120])


def run_startup_probes(
    probes: dict[str, Callable[[], Any]],
    budget: float,
    *,
    apply: bool = True,
) -> dict[str, ProbeResult]:
    """Jalankan semua probe paralel, total <= ``budget`` detik.

    Yang belum selesai saat budget habis = WARN/unknown (tidak menggerbang).
    Hasil telat DIABAIKAN — thread probe-nya dibiarkan selesai sendiri.
    """
    results: dict[str, ProbeResult] = {}
    if not probes:
        return results
    # Thread DAEMON (bukan ThreadPoolExecutor): probe yang macet tidak boleh
    # menahan proses bridge saat keluar (executor di-join saat interpreter exit).
    selesai: dict[str, ProbeResult] = {}
    cv = threading.Condition()

    def _jalan(name: str, fn: Callable[[], Any]) -> None:
        res = _safe_probe(fn)
        with cv:
            selesai[name] = res
            cv.notify_all()

    for name, fn in probes.items():
        threading.Thread(target=_jalan, args=(name, fn), daemon=True,
                         name=f"provider-probe-{name}").start()
    deadline = time.monotonic() + max(0.0, float(budget))
    with cv:
        while len(selesai) < len(probes):
            sisa = deadline - time.monotonic()
            if sisa <= 0:
                break
            cv.wait(timeout=sisa)
        snapshot = dict(selesai)
    for name in probes:
        results[name] = snapshot.get(name) or ProbeResult(
            "WARN", "unknown", f"belum selesai dalam {budget:g}s")
    for name, fn in probes.items():
        register_probe(name, fn)
    if apply:
        for name, res in results.items():
            apply_probe(name, res)
    return results


def format_probe_lines(results: dict[str, ProbeResult]) -> list[str]:
    lines = ["", "  --- PROVIDER PROBE (sekali di awal) ---"]
    for name, r in results.items():
        cls = f" {r.err_class}" if r.err_class else ""
        nasib = ""
        if r.status == "FAIL" and r.err_class in HARD:
            nasib = " -> DEAD sesi ini" if not is_protected(name) else " -> last resort, tetap dicoba"
        elif r.status == "WARN" and r.err_class in TRANSIENT:
            nasib = " -> tetap dipakai (breaker biasa)"
        lines.append(f"  [{r.status:<4}] {name:<22}{cls}{nasib}  {r.detail[:90]}".rstrip())
    return lines


# --------------------------------------------------------------------------- #
# Probe per provider (murah; semua jaringan lazy-import)
# --------------------------------------------------------------------------- #


def _http_probe(method: str, url: str, headers: dict, timeout: float, json_body=None) -> ProbeResult:
    import requests  # noqa: PLC0415

    r = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
    if 200 <= r.status_code < 300:
        return ProbeResult("OK", "", f"HTTP {r.status_code}")
    body = (r.text or "")[:200]
    cls = classify(f"HTTP {r.status_code}: {body}")
    if cls == "unknown":
        cls = classify_status(r.status_code)
    return _verdict(cls, f"HTTP {r.status_code}")


def _wait_warm_probe(prewarm: Callable[[], Any], is_warm: Callable[[], bool],
                     fail_seq: Callable[[], int], last_err: Callable[[], str],
                     timeout: float, *, retry_generic: bool = True) -> ProbeResult:
    """Probe proses persisten (agy/codex): panaskan, tunggu hangat atau gagal init."""
    deadline = time.monotonic() + timeout
    attempts = 2 if retry_generic else 1
    for attempt in range(attempts):
        seq0 = fail_seq()
        prewarm()
        while time.monotonic() < deadline:
            if is_warm():
                return ProbeResult("OK", "", "proses hangat")
            if fail_seq() > seq0:
                break
            time.sleep(0.1)
        else:
            return ProbeResult("WARN", "unknown", "init belum selesai dalam budget")
        err = last_err() or "init gagal"
        cls = classify(err, default="init_error")
        # init_error generik bisa balapan (privacy-discard -> "process exit"):
        # coba SEKALI lagi sebelum vonis mati. Penolakan model/auth langsung vonis.
        if cls == "init_error" and attempt + 1 < attempts:
            continue
        return ProbeResult("FAIL", cls, err[:120])
    return ProbeResult("WARN", "unknown", "init tidak tuntas")


def build_probes(config: dict) -> dict[str, Callable[[], ProbeResult]]:
    """Daftar probe HANYA untuk yang benar-benar dinyalakan config."""
    import os  # noqa: PLC0415

    cfg = dict(config or {})
    budget = budget_sec(cfg)
    probes: dict[str, Callable[[], ProbeResult]] = {}

    # --- Otak suara ---
    if cfg.get("codex_agent_enabled", False):
        def _codex() -> ProbeResult:
            import arti_codex_agent as cx  # noqa: PLC0415
            return _wait_warm_probe(
                lambda: cx.prewarm(cfg), cx.is_warm,
                lambda: int(getattr(cx, "_health_warm_fail_seq", 0)),
                lambda: str(getattr(cx, "_health_last_warm_error", "")),
                budget, retry_generic=False)
        probes["codex"] = _codex

    if cfg.get("agy_agent_enabled", False):
        def _agy() -> ProbeResult:
            import arti_agy_agent as ag  # noqa: PLC0415
            return _wait_warm_probe(
                lambda: ag.prewarm(cfg, _health_probe=True), ag.is_warm,
                lambda: int(getattr(ag, "_health_init_fail_seq", 0)),
                lambda: str(getattr(ag, "_health_last_init_error", "")),
                budget)
        probes["agy"] = _agy

    groq_key = (cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY") or "").strip()
    if groq_key and not groq_key.startswith("YOUR_"):
        def _groq() -> ProbeResult:
            return _http_probe("GET", "https://api.groq.com/openai/v1/models",
                               {"Authorization": f"Bearer {groq_key}"}, budget)
        probes["groq"] = _groq

    or_key = (cfg.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if or_key and cfg.get("openrouter_live_fallback_enabled", True):
        def _or_voice() -> ProbeResult:
            # /key = cek auth gratis, tidak memakan jatah model :free.
            return _http_probe("GET", "https://openrouter.ai/api/v1/key",
                               {"Authorization": f"Bearer {or_key}"}, budget)
        probes["openrouter:voice"] = _or_voice

    if cfg.get("cursor_agent_enabled", False):
        probes["cursor:voice"] = lambda: _cursor_probe(cfg)

    # --- Rantai scouter / observer / vision: HANYA 1-2 entri terdepan ---
    # Murah dulu (review 21cb06ff): endpoint gratis (/models, /key) bila ada;
    # tanpa gambar sama sekali. codex di rantai = probe suara di atas.
    text_chains = []
    if cfg.get("scouter_enabled", True):
        text_chains.append(("scout", list(cfg.get("scouter_provider_chain") or [])))
    text_chains.append(("observer", list(cfg.get("observer_provider_chain") or [])))
    for role, chain in text_chains:
        role_cfg = {**cfg, "cursor_role": str(cfg.get("observer_cursor_role") or "observer")} \
            if role == "observer" else cfg
        for name in _chain_head(chain):
            key = chain_key(name, role_cfg)
            if key in probes:
                continue
            if name == "cursor":
                if cfg.get("cursor_agent_enabled", False):
                    probes[key] = (lambda c=role_cfg: _cursor_probe(c))
                continue
            gratis = _free_probe(name, cfg, budget)
            probes[key] = gratis or (lambda n=name: _scouter_text_probe(n, cfg, budget))

    if cfg.get("vision_enabled", cfg.get("screen_context_enabled", False)):
        for name in _chain_head(list(cfg.get("vision_provider_chain") or [])):
            key = chain_key(name, cfg, lane="vision")
            if key in probes:
                continue
            if name == "cursor":
                if cfg.get("cursor_agent_enabled", False):
                    probes[key] = (lambda: _cursor_probe(cfg))
                continue
            gratis = _free_probe(name, cfg, budget)
            if gratis is not None:  # vision TANPA endpoint gratis = tidak di-probe
                probes[key] = gratis

    if cfg.get("web_lookup_enabled", False):
        for name in list(cfg.get("web_lookup_provider_chain") or ["groq_compound", "cursor"]):
            key = chain_key(name, cfg, lane="web")
            if key in probes:
                continue
            if name == "groq_compound" and groq_key:
                probes[key] = (lambda: _http_probe(
                    "GET", "https://api.groq.com/openai/v1/models",
                    {"Authorization": f"Bearer {groq_key}"}, budget))
            elif name == "cursor" and cfg.get("cursor_agent_enabled", False):
                probes[key] = (lambda: _cursor_probe({**cfg, "cursor_role": "lookup"}))
    return probes


def _cursor_probe(cfg: dict) -> ProbeResult:
    # Sesi Cursor dingin ~18 detik (> budget); probe murah = prasyarat lokal
    # (kunci, SDK, scratch dir). Tanpa jaringan, tanpa kuota.
    import arti_cursor_agent as ca  # noqa: PLC0415

    ok, why = ca.is_available(cfg)
    if ok:
        return ProbeResult("OK", "", "prasyarat siap")
    cls = "auth" if "KEY" in str(why).upper() else "init_error"
    return ProbeResult("FAIL", cls, str(why)[:120])


_PROBE_PROMPT = 'Balas persis JSON ini saja: {"ok": true}'


def _scouter_text_probe(name: str, cfg: dict, budget: float) -> ProbeResult:
    import arti_scouter_client as sc  # noqa: PLC0415

    fn = sc._PROVIDERS.get(name)
    if fn is None:
        return ProbeResult("WARN", "unknown", "provider tidak terdaftar")
    pcfg = {**cfg, "scouter_max_tokens": 16, "scouter_temperature": 0.0,
            "scouter_timeout_sec": budget, "telemetry_subsystem": "provider_probe"}
    _raw, ms = fn(_PROBE_PROMPT, pcfg)
    return ProbeResult("OK", "", f"{ms}ms")


CHAIN_PROBE_DEPTH = 2


def _chain_head(chain: list) -> list:
    """2 entri terdepan yang memang bisa di-probe (tanpa groq, tanpa codex)."""
    out = []
    for name in chain:
        if name == "groq" or is_protected(name):
            continue
        out.append(name)
        if len(out) >= CHAIN_PROBE_DEPTH:
            break
    return out


def _free_probe(name: str, cfg: dict, budget: float) -> Callable[[], ProbeResult] | None:
    """Probe GRATIS (metadata, kredensial yang sama dengan jalur live) bila ada."""
    import os  # noqa: PLC0415

    if name in ("google_gemini", "google_gemini_lite", "google_gemma"):
        def _gemini() -> ProbeResult:
            import arti_gemini_vision as gv  # noqa: PLC0415

            key = gv.resolve_api_key(cfg)
            if not key or key == "YOUR_GEMINI_API_KEY":
                return ProbeResult("FAIL", "auth", "GEMINI_API_KEY kosong")
            if name == "google_gemma":
                model = cfg.get("vision_google_gemma_model") or "gemma-4-26b-a4b-it"
            elif name == "google_gemini_lite":
                model = cfg.get("vision_google_gemini_model") or "gemini-3.1-flash-lite"
            else:
                model = (cfg.get("scouter_gemini_model") or cfg.get("vision_google_gemini_model")
                         or "gemini-3.1-flash-lite")
            # GET metadata model: gratis, tanpa kuota generate; 404 = model ditolak.
            return _http_probe("GET", f"{gv.GEMINI_API_BASE}/{model}",
                               {"x-goog-api-key": key}, budget)
        return _gemini
    if name == "openrouter":
        key = (cfg.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
        if not key:
            return None
        return lambda: _http_probe("GET", "https://openrouter.ai/api/v1/key",
                                   {"Authorization": f"Bearer {key}"}, budget)
    if name == "cloudflare":
        def _cf() -> ProbeResult:
            import arti_cloudflare_vision as cf  # noqa: PLC0415

            tok, acc = cf.resolve_token(cfg), cf.resolve_account_id(cfg)
            if not tok or not acc:
                return ProbeResult("FAIL", "auth", "CLOUDFLARE token/account kosong")
            return _http_probe(
                "GET",
                f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/models/search?per_page=1",
                {"Authorization": f"Bearer {tok}"}, budget)
        return _cf
    if name == "nvidia":
        def _nv() -> ProbeResult:
            import arti_nvidia_client as nv  # noqa: PLC0415

            key = nv.resolve_api_key(cfg)
            if not key:
                return ProbeResult("FAIL", "auth", "NVIDIA_API_KEY kosong")
            return _http_probe("GET", f"{nv.NVIDIA_API_BASE}/models",
                               {"Authorization": f"Bearer {key}"}, budget)
        return _nv
    return None


def start_startup_probe(config: dict, *, log: Callable[[str], None] | None = None,
                        probes: dict[str, Callable[[], Any]] | None = None,
                        background: bool = True) -> threading.Thread | None:
    """Konfigurasi + jalankan probe di thread latar. Startup lain jalan terus."""
    if not configure(config):
        return None
    out = log or _log

    def _run() -> None:
        try:
            plist = probes if probes is not None else build_probes(config)
            res = run_startup_probes(plist, budget_sec(config))
            out("\n".join(format_probe_lines(res)))
            out(f"[Health] {summary_line()}")
        except Exception as exc:  # noqa: BLE001
            out(f"[Health] provider probe gagal jalan ({type(exc).__name__}) — rantai lama")

    if not background:
        _run()
        return None
    t = threading.Thread(target=_run, daemon=True, name="provider-probe")
    t.start()
    return t
