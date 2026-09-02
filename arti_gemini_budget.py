"""Rem kuota Gemini free tier — berhenti SEBELUM Google menolak.

Konteks 17 Agu 2026: sejak `google_gemini` naik ke posisi 1 rantai scouter
(14 Agu, karena nvidia timeout 45 dtk), sesi live 16 Agu menghasilkan 186x
HTTP 429 di terminal. Akar gandanya: free tier flash-lite cuma belasan RPM,
dan rantai tidak punya ingatan — provider yang baru saja ditolak tetap
ditembak lagi giliran berikutnya.

Tiga rem, semuanya per-MODEL (kuota Google memang per model per project):

1. RPM   — maksimal `gemini_rpm_budget` panggilan per 60 detik bergulir.
2. TPM   — maksimal `gemini_tpm_budget` token per 60 detik bergulir
           (input+output, dari `usageMetadata.totalTokenCount` balasan).
3. Istirahat — kalau TETAP kena 429 (kuota berubah, atau key dipakai
           proses lain), diam `gemini_429_cooldown_sec` tanpa menembak.

Selama rem menahan, rantai scouter/vision MELEWATI Gemini tanpa mencetak
error — log hanya satu baris saat mulai menahan dan satu saat jatah pulih
(bukan satu baris per panggilan; itu persis spam yang mau dihilangkan).

Angka limit resmi TIDAK dipublikasikan Google lagi di docs (dicek 17 Agu
2026) — angka nyata hanya terlihat di aistudio.google.com/rate-limit milik
akun pemegang key. Default di sini pagar konservatif di bawah angka
historis free tier flash-lite (15 RPM / 250k TPM); setel lewat
config_local kalau angka aslinya beda.
"""

from __future__ import annotations

import threading
import time
from collections import deque

_JENDELA_SEC = 60.0

DEFAULT_RPM_BUDGET = 12
DEFAULT_TPM_BUDGET = 200_000
DEFAULT_COOLDOWN_SEC = 60.0

_lock = threading.Lock()
# model -> deque[(ts, tokens, adalah_panggilan)]
#   adalah_panggilan=True  : satu request HTTP yang benar-benar ditembak
#                            (dihitung RPM, token 0 saat dicatat)
#   adalah_panggilan=False : token hasil balasan sukses (dihitung TPM saja)
_riwayat: dict[str, deque] = {}
_istirahat_sampai: dict[str, float] = {}
_sedang_menahan: dict[str, bool] = {}


class JatahPenuh(RuntimeError):
    """Panggilan di-skip oleh rem lokal — BUKAN error dari Google."""


def _now() -> float:
    # Fungsi sendiri supaya tes bisa memajukan jam tanpa tidur sungguhan.
    return time.monotonic()


def _ambil(config: dict | None, kunci: str, default: float) -> float:
    # Kunci yang ADA bernilai None tidak boleh mematikan rem —
    # config_local produksi pernah menyimpan None dan .get(k, default)
    # tidak menolong untuk kasus itu (aturan 7 CLAUDE.md).
    nilai = (config or {}).get(kunci)
    if nilai is None:
        return float(default)
    try:
        return float(nilai)
    except (TypeError, ValueError):
        return float(default)


def _pangkas(model: str, now: float) -> deque:
    dq = _riwayat.setdefault(model, deque())
    while dq and now - dq[0][0] > _JENDELA_SEC:
        dq.popleft()
    return dq


def perkiraan_token(prompt: str, max_tokens: int, *, ada_gambar: bool = False) -> int:
    """Perkiraan kasar SEBELUM menembak: ~4 huruf per token + jatah output.
    Gambar dihitung gepok 1000 token. Sengaja dibulatkan ke atas — rem ini
    pagar pengaman, meleset konservatif lebih baik daripada meleset bablas."""
    return len(prompt or "") // 3 + max(0, int(max_tokens)) + (1000 if ada_gambar else 0)


def _cek(model: str, est_tokens: int, config: dict | None, now: float) -> tuple[bool, str]:
    sampai = _istirahat_sampai.get(model, 0.0)
    if now < sampai:
        return False, f"istirahat pasca-429 ({int(sampai - now)} dtk lagi)"

    dq = _pangkas(model, now)
    rpm_budget = int(_ambil(config, "gemini_rpm_budget", DEFAULT_RPM_BUDGET))
    tpm_budget = int(_ambil(config, "gemini_tpm_budget", DEFAULT_TPM_BUDGET))

    panggilan = sum(1 for _, _, adalah in dq if adalah)
    if panggilan >= rpm_budget:
        return False, f"jatah panggilan/menit penuh ({panggilan}/{rpm_budget})"

    token = sum(t for _, t, _ in dq)
    if token + max(0, int(est_tokens)) > tpm_budget:
        return False, f"jatah token/menit penuh ({token}+~{est_tokens}/{tpm_budget})"

    return True, ""


def boleh(model: str, est_tokens: int, config: dict | None = None) -> tuple[bool, str]:
    """(True, "") kalau aman menembak; (False, alasan) kalau rem menahan.
    Log HANYA saat berganti keadaan — sekali waktu mulai menahan, sekali
    waktu jatah pulih."""
    with _lock:
        now = _now()
        ok, alasan = _cek(model, est_tokens, config, now)
        if not ok and not _sedang_menahan.get(model):
            _sedang_menahan[model] = True
            print(f"[GeminiBudget] {model}: {alasan} — Gemini dilewati dulu, rantai lanjut tanpa error")
        elif ok and _sedang_menahan.get(model):
            _sedang_menahan[model] = False
            print(f"[GeminiBudget] {model}: jatah pulih, Gemini dipakai lagi")
        return ok, alasan


def sedang_dibatasi(model: str, config: dict | None = None, est_tokens: int = 0) -> bool:
    """Untuk _resolve_chain: True = keluarkan Gemini dari rantai giliran ini."""
    ok, _ = boleh(model, est_tokens, config)
    return not ok


def catat_panggilan(model: str) -> None:
    """Panggil TEPAT sebelum request HTTP ditembak — masuk hitungan RPM
    apa pun hasil responsnya (Google menghitung request, bukan sukses)."""
    with _lock:
        now = _now()
        _pangkas(model, now).append((now, 0, True))


def catat_token(model: str, tokens: int) -> None:
    """Token nyata dari balasan sukses (usageMetadata.totalTokenCount)."""
    if tokens <= 0:
        return
    with _lock:
        now = _now()
        _pangkas(model, now).append((now, int(tokens), False))


def kena_429(model: str, config: dict | None = None) -> None:
    """Google tetap menolak walau rem lokal lolos → jatah nyata lebih ketat
    dari perkiraan kita. Diam dulu, jangan menambah spam."""
    cooldown = _ambil(config, "gemini_429_cooldown_sec", DEFAULT_COOLDOWN_SEC)
    with _lock:
        _istirahat_sampai[model] = _now() + max(0.0, cooldown)


def reset() -> None:
    """Untuk tes — kosongkan semua ingatan."""
    with _lock:
        _riwayat.clear()
        _istirahat_sampai.clear()
        _sedang_menahan.clear()
