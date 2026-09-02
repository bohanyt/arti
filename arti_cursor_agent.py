"""Otak Cursor (Composer 2.5) untuk balasan chat YouTube Arti.

Modul ini TIDAK menyentuh bridge. Ia menyediakan satu API blocking —
:func:`send_turn` — yang menerima prompt jadi dan mengembalikan teks balasan.

KENAPA ADA
streamer langganan Cursor. Composer 2.5 ditarik dari "Cursor Models pool" yang sudah
termasuk langganan (beda dari API pool pihak ketiga, yang kuotanya sudah habis). Jadi
menjawab chat viewer lewat Composer memakai resource yang sudah dibayar.

FAKTA TERUKUR (historical private benchmark; retained here only as tuning context)
  sesi hangat, tanpa turn pertama : p50 2,66 dtk  p95 3,73 dtk
  turn pertama sesi               : 13,11 dtk   <- dibayar di luar siaran (pemanas)
  agen baru tiap turn (dingin)    : p50 4,62 dtk
  25/25 sukses, NOL tool_call, NOL berkas tersentuh di scratch

TIGA HAL YANG DIPELAJARI DENGAN MAHAL

1. SESI BISA MATI TOTAL DI TENGAH JALAN. Saat uji Standard, sesi mati di turn 15 dan
   lima turn berikutnya gagal INSTAN (InternalServerError, 0,02 dtk). Tanpa daur ulang
   otomatis, Arti bisu sampai bridge restart. Sisi baiknya: gagalnya instan, jadi
   fallback ke Groq nyaris tanpa biaya waktu.

2. DEADLINE PER-MESSAGE TIDAK CUKUP. Iterator yang menggantung tidak pernah sampai ke
   pengecekan deadline, jadi satu sampel lolos jadi 41 detik padahal timeout 30.
   `asyncio.wait_for` di sisi pemanggil adalah pengaman UTAMA, bukan cadangan.

3. `fast=false`. Fast berharga 6x ($3,00/$15,00 vs $0,50/$2,50 per juta token) untuk
   selisih ~0,5 detik. Model dan kualitasnya identik — Fast cuma hardware lebih mahal.

SDK di-import MALAS di dalam fungsi supaya modul ini tetap bisa di-import dan seluruh
helper murninya diuji tanpa `cursor-sdk` terpasang dan tanpa jaringan.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent

# Alasan kegagalan yang dikenali pemanggil.
REASON_OK = "ok"
REASON_TIMEOUT = "timeout"
REASON_EMPTY = "empty"
REASON_ERROR = "error"
REASON_TOOL_CALL = "tool_call"
REASON_BUSY = "busy"
REASON_RATELIMIT = "ratelimit"
REASON_UNAVAILABLE = "unavailable"
REASON_DISABLED = "disabled"


@dataclass
class RunCollect:
    """Hasil mentah dari mengkonsumsi satu stream `run.messages()`."""

    text: str = ""
    tool_calls: int = 0
    thinking_blocks: int = 0
    timed_out: bool = False
    first_text_ms: int | None = None


@dataclass
class CursorResult:
    text: str | None = None
    sentences: list[str] = field(default_factory=list)
    ok: bool = False
    reason: str = REASON_ERROR
    latency_ms: int = 0
    tool_calls: int = 0
    model: str = ""


# --------------------------------------------------------------------------- #
# Helper MURNI — target utama unit test (tanpa SDK, tanpa jaringan)
# --------------------------------------------------------------------------- #

def _msg_type(msg: Any) -> str:
    t = getattr(msg, "type", None)
    if t is None and isinstance(msg, dict):
        t = msg.get("type")
    return str(t or "")


def extract_text_blocks(msg: Any) -> str:
    """Ambil HANYA blok ``type == "text"`` dari satu message assistant.

    Defensif terhadap bentuk objek vs dict: dokumentasi SDK memperingatkan bahwa
    payload tool_call "reflect each tool's internal shape and can change".
    """
    inner = getattr(msg, "message", None)
    if inner is None and isinstance(msg, dict):
        inner = msg.get("message")
    if inner is None:
        return ""
    content = getattr(inner, "content", None)
    if content is None and isinstance(inner, dict):
        content = inner.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out: list[str] = []
    for block in content:
        bt = getattr(block, "type", None)
        if bt is None and isinstance(block, dict):
            bt = block.get("type")
        if bt != "text":
            continue
        txt = getattr(block, "text", None)
        if txt is None and isinstance(block, dict):
            txt = block.get("text")
        if txt:
            out.append(str(txt))
    return "".join(out)


def collect_run_messages(
    messages: Any,
    *,
    timeout_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> RunCollect:
    """Konsumsi stream message dan kumpulkan teks assistant saja.

    `clock` bisa diinjeksi supaya test deadline deterministik tanpa `sleep`.

    CATATAN PENTING: deadline di sini hanya menggigit ketika message BERIKUTNYA tiba.
    Kalau iterator menggantung tanpa mengirim apa-apa, loop ini ikut menggantung —
    terbukti pada sampel yang lolos jadi 41 detik dengan timeout 30. Pemanggil WAJIB
    membungkusnya dengan `asyncio.wait_for`.
    """
    out = RunCollect()
    start = clock()
    deadline = start + float(timeout_s)
    for msg in messages:
        if clock() > deadline:
            out.timed_out = True
            break
        mt = _msg_type(msg)
        if mt == "assistant":
            chunk = extract_text_blocks(msg)
            if chunk:
                if out.first_text_ms is None:
                    out.first_text_ms = int((clock() - start) * 1000)
                out.text += chunk
        elif mt == "tool_call":
            out.tool_calls += 1
        elif mt == "thinking":
            out.thinking_blocks += 1
    out.text = out.text.strip()
    return out


def validate_scratch_dir(path: str, repo_root: str | Path | None = None) -> tuple[bool, str]:
    """Folder kerja agen harus KOSONG dan DI LUAR repo.

    SDK tidak menyediakan cara mematikan tool — agen tetap bisa membaca dan menulis
    berkas di `cwd`-nya. Satu-satunya mitigasi adalah mengarahkan `cwd` ke tempat yang
    tidak berisi apa pun yang berharga. Spike mencatat nol berkas tersentuh setelah 45
    turn, tapi itu observasi, bukan jaminan.
    """
    root = Path(repo_root or _ROOT).resolve()
    if not (path or "").strip():
        return False, "cursor_scratch_dir kosong"
    p = Path(path).expanduser()
    if not p.is_absolute():
        return False, f"harus path absolut: {path}"
    try:
        p = p.resolve()
    except OSError as exc:
        return False, f"path tidak bisa di-resolve: {exc}"
    if not p.is_dir():
        return False, f"bukan direktori: {p}"
    if (p / ".git").exists():
        return False, f"berisi .git: {p}"
    try:
        if p == root or root in p.parents:
            return False, f"ada di dalam repo: {p}"
        if p in root.parents:
            return False, f"induk dari repo: {p}"
    except (OSError, ValueError):
        pass  # drive berbeda di Windows -> justru aman
    return True, str(p)


def should_recycle(
    turn_count: int,
    age_sec: float,
    dirty: bool,
    config: dict | None = None,
) -> tuple[bool, str]:
    """Perlukah sesi dibuang dan dibuat ulang? Dievaluasi di AWAL turn berikutnya.

    Sengaja tidak di akhir turn: teardown tidak boleh menambah latensi turn yang
    sedang berjalan.
    """
    cfg = config or {}
    if dirty:
        return True, "sesi ditandai rusak"
    max_turns = int(cfg.get("cursor_session_max_turns", 20))
    if max_turns > 0 and turn_count >= max_turns:
        return True, f"mencapai {max_turns} turn"
    max_age = float(cfg.get("cursor_session_max_age_sec", 1800))
    if max_age > 0 and age_sec > max_age:
        return True, f"umur {age_sec:.0f}s melebihi {max_age:.0f}s"
    return False, ""


# CATATAN SEJARAH: dulu di sini ada `cadangan_perlu_dipanaskan()` — predikat
# "panaskan cadangan hanya saat sesi aktif mendekati batas daur ulang". Dihapus
# [date removed] oleh KEBIJAKAN KEMBAR (log [time removed]: 24 timeout, breaker 7x, tukar
# panas 0x — sesi mati MUDA oleh timeout, jalur kematian yang tidak pernah
# disiapkan predikat itu). Sekarang cadangan dipanaskan SELALU; lihat prewarm().


KNOWN_ROLES = ("voice", "scout", "observer", "vision", "lookup", "catchup")
_warned_unknown_roles: set[str] = set()


def resolve_role_model(role: str, config: dict | None = None) -> tuple[str, str | None]:
    """(model_id, effort) untuk satu role. Pure — target unit test.

    Peran — SEMUA composer-2.5 NOT FAST (revisi streamer 2026-08-10: "biar
    hemat, harus composer 2.5 NOT FAST deh, grok 4.5 gausah dulu, i feel
    composer is smart enough" — MENGGANTIKAN keputusan 2026-08-03 yang masih
    menahan observer di grok-high dan lookup di grok-low; grok-high pernah
    49% konsumsi harian). NOT FAST dijamin global: cursor_fast_param default
    False -> fast="false" di tiap pembuatan sesi. Balik ke grok cukup lewat
    config_local: cursor_observer_model / cursor_lookup_model.
      voice    -> composer-2.5 (jawaban harian; param: fast saja)
      scout    -> composer-2.5 (scouter tiap menit)
      observer -> composer-2.5 (kurasi akhir live — dulu grok-4.5/high)
      catchup  -> composer-2.5 (backlog rangkuman; sesi di-reuse antar
                  segmen jadi cache Cursor kena. Role SENDIRI, bukan numpang
                  observer: sesi di-keyed per role, kalau numpang maka sesi
                  bisa saling tercampur antar tugas)
      lookup   -> composer-2.5 + web tool (dulu grok-4.5/low; kalau composer
                  ternyata malas memanggil web tool, primary chain lookup
                  tetap groq/compound — cursor cuma cadangan)
      vision   -> composer-2.5 (baca layar; terverifikasi bisa gambar)

    `effort` HANYA dikirim kalau non-kosong: composer-2.5 tidak punya param
    effort (list_models: parameters = fast saja) — mengirimnya bisa ditolak.
    grok-4.5 punya effort low/medium/high resmi.
    """
    cfg = config or {}
    if role == "voice":
        return str(cfg.get("cursor_model", "composer-2.5")), None
    default_model, default_effort = {
        "scout": ("composer-2.5", ""),      # scouter per menit — hemat pool
        "observer": ("composer-2.5", ""),   # kurasi akhir live (operator [date removed])
        "catchup": ("composer-2.5", ""),    # backlog rangkuman — hemat + cache
        "lookup": ("composer-2.5", ""),     # web search — hemat (operator [date removed])
    }.get(role, ("composer-2.5", ""))
    model = str(cfg.get(f"cursor_{role}_model", default_model))
    effort = str(cfg.get(f"cursor_{role}_effort", default_effort) or "").strip()
    return model, (effort or None)


def role_timeout_sec(role: str, config: dict | None = None) -> float:
    """Timeout per role. Voice ketat (penonton menunggu); scout/vision longgar."""
    cfg = config or {}
    if role == "voice":
        return float(cfg.get("cursor_timeout_sec", 5.0))
    # Vision 45: cold + gambar terukur 35,6 dtk (uji produksi [date removed]).
    # Timeout di bawah itu = jebakan dingin-timeout-recycle: panggilan pertama
    # selalu gagal, sesi dibuang, dingin lagi — Cursor tidak pernah terpakai
    # (persis cacat prewarm voice dulu). Biaya dinginnya dibayar pemanas startup.
    # Scout 45 (naik dari 30, spike_scouter_composer.py [date removed]): sesudah
    # scouter pindah ke composer-2.5, panggilan TERUKUR cold 27,2 dtk / warm
    # 11,6 dtk — margin 30 dtk cuma ~3 dtk. Sesi scout didaur ulang tiap
    # cursor_session_max_age_sec (1800) / max_turns (20 ~= 20 menit sekali
    # pada cadence scouter), jadi cold berulang sepanjang live; timeout mepet
    # = jatuh diam-diam ke chain gratis tiap daur ulang.
    # Observer 60: dulu grok-high terukur 12-32 dtk/segmen; kini composer
    # (cold 27,2 / warm 11,6) — 60 dipertahankan sebagai margin cold start
    # (tidak di-prewarm — cuma hidup saat shutdown).
    # Catchup 45: composer sama dengan scout (cold 27,2 / warm 11,6 terukur).
    default = {"scout": 45.0, "lookup": 30.0, "observer": 60.0,
               "catchup": 45.0}.get(role, 45.0)
    return float(cfg.get(f"cursor_{role}_timeout_sec", default))


def role_allows_tools(role: str, config: dict | None = None) -> bool:
    """Hanya role 'lookup' yang boleh tool call — web search ADALAH tugasnya.

    Role lain tetap dilarang total (TOOL_BAN + reject): agen yang menyentuh
    berkas/terminal adalah agen yang melenceng.
    """
    cfg = config or {}
    return bool(cfg.get(f"cursor_{role}_allow_tools", role == "lookup"))


def sdk_module_name() -> str | None:
    """Nama modul SDK yang benar, atau None kalau tidak terpasang.

    Paket PyPI bernama `cursor` BUKAN SDK ini — itu utilitas terminal yang tidak
    berhubungan. Yang benar `cursor-sdk` (modul `cursor_sdk`). Karena itu keberadaan
    atribut `Agent` yang dijadikan penentu, bukan sekadar nama modul.
    """
    for name in ("cursor_sdk", "cursor"):
        try:
            mod = __import__(name)
        except Exception:  # noqa: BLE001
            continue
        if hasattr(mod, "Agent"):
            return name
    return None


def is_available(config: dict | None = None) -> tuple[bool, str]:
    """Boleh mencoba jalur Cursor? Menyaring SEBELUM menyentuh jaringan."""
    cfg = config or {}
    if not cfg.get("cursor_agent_enabled", False):
        return False, "cursor_agent_enabled=False"
    if not (cfg.get("cursor_api_key") or os.environ.get("CURSOR_API_KEY") or "").strip():
        return False, "CURSOR_API_KEY kosong"
    if sdk_module_name() is None:
        return False, "cursor-sdk tidak terpasang"
    ok, why = validate_scratch_dir(str(cfg.get("cursor_scratch_dir", "")))
    if not ok:
        return False, f"scratch dir: {why}"
    return True, "siap"


# --------------------------------------------------------------------------- #
# Bridge SDK — workaround Windows
# --------------------------------------------------------------------------- #

def launch_sdk_bridge(sdk: Any, workspace: str, timeout: float = 45.0):
    """Nyalakan cursor-sdk-bridge sendiri dan kembalikan (Client, Popen, endpoint).

    KENAPA TIDAK PAKAI `Agent.create()` LANGSUNG:
    `cursor_sdk` 1.0.26 tidak bisa menyalakan bridge-nya sendiri di Windows.
    `Bridge.launch()` membaca baris discovery dari stderr memakai `os.get_blocking()`
    + `selectors` — dua-duanya POSIX-only:
        AttributeError: module 'os' has no attribute 'get_blocking'
    Bukan sekadar fungsi yang hilang: di Windows pipe memang tidak bisa dijadikan
    non-blocking lewat `os.set_blocking`, dan `selectors` tidak bisa memantau handle
    pipe (hanya socket). Menambal `os.get_blocking` saja tidak menolong.

    Kena SEMUA mode, bukan cuma agen lokal: `_default_client()` selalu memanggil
    `Bridge.launch()` kecuali CURSOR_SDK_BRIDGE_URL dan _TOKEN sudah ter-set, jadi
    cloud agent pun ikut gagal. Wheel win_amd64 tetap dipublikasikan, jadi ini bug
    hulu — bukan platform yang tidak didukung.

    Solusinya memakai jalur resmi SDK: `Agent.create(client=...)`. Tidak ada monkeypatch.
    Bridge siap ~0,7 detik.

    Cek ulang saat menaikkan versi cursor-sdk — kalau upstream sudah memperbaiki
    `_read_discovery`, seluruh fungsi ini bisa dihapus.
    """
    from cursor_sdk._bridge import (  # noqa: PLC0415
        READY_LINE_PREFIX,
        BridgeEndpoint,
        _bridge_subprocess_env,
    )
    from cursor_sdk._client import Client  # noqa: PLC0415
    from cursor_sdk._vendor import resolve_bridge_path  # noqa: PLC0415

    argv = [os.fspath(resolve_bridge_path()), "--workspace", workspace]
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(_bridge_subprocess_env()),
    )

    q: queue.Queue = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stderr:  # readline blocking — aman di Windows
                q.put(line)
        finally:
            q.put(None)

    threading.Thread(target=_reader, daemon=True, name="cursor-bridge-stderr").start()

    discovery = None
    seen: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = q.get(timeout=1.0)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        if line is None:
            break
        seen.append(line.rstrip())
        if line.startswith(READY_LINE_PREFIX):
            discovery = json.loads(line[len(READY_LINE_PREFIX):])
            break

    if not discovery:
        _terminate(proc)
        tail = "\n    ".join(seen[-12:]) or "(stderr kosong)"
        raise RuntimeError(f"cursor bridge tidak siap dalam {timeout}s:\n    {tail}")

    endpoint = BridgeEndpoint.from_discovery(discovery)
    return Client(endpoint, allow_api_key_env_fallback=True), proc, endpoint


def _terminate(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Sesi hangat
# --------------------------------------------------------------------------- #

TOOL_BAN_HEADER = (
    "PENTING: jawab dengan teks biasa saja. JANGAN memanggil tool apa pun. "
    "JANGAN membaca, membuat, atau menulis berkas. Jangan menjalankan perintah.\n\n"
)


class CursorSession:
    """Satu sesi hangat Cursor. Bukan thread-safe; dilindungi lock di level modul."""

    def __init__(self, config: dict, role: str = "voice") -> None:
        self.config = config
        self.role = role
        self._sdk: Any = None
        self._client: Any = None
        self._proc: subprocess.Popen | None = None
        self._agent: Any = None
        self.turn_count = 0
        self.created_at = 0.0
        self.dirty = False
        self.dirty_reason = ""
        # `warmed` BEDA dari "agen sudah dibuat". Membuat agen cepat (~6 detik), tapi
        # giliran PERTAMA-lah yang mahal (~15 detik total). Kalau penanda hangat dipasang
        # begitu agen ada, chat yang datang di sela itu berebut dengan pemanasan dan
        # tetap lambat — terukur 9,6 detik. Jadi penanda baru dipasang setelah giliran
        # pertama benar-benar selesai.
        self.warmed = False

    @property
    def age_sec(self) -> float:
        return time.monotonic() - self.created_at if self.created_at else 0.0

    def ensure(self) -> None:
        """Bangun sesi kalau belum ada. Blocking."""
        if self._agent is not None and not self.dirty:
            return
        self.close(detach=True)

        name = sdk_module_name()
        if name is None:
            raise RuntimeError("cursor-sdk tidak terpasang")
        sdk = __import__(name)
        self._sdk = sdk

        scratch_ok, scratch = validate_scratch_dir(str(self.config.get("cursor_scratch_dir", "")))
        if not scratch_ok:
            raise RuntimeError(f"scratch dir tidak valid: {scratch}")

        self._client, self._proc, _ = launch_sdk_bridge(sdk, scratch)

        model_id, effort = resolve_role_model(self.role, self.config)
        fast = "true" if self.config.get("cursor_fast_param", False) else "false"
        # SENGAJA tanpa fallback ke string polos. Terverifikasi list_models
        # (spike_grok_vision [date removed]): varian DEFAULT composer-2.5 adalah
        # fast=true — id polos berarti FAST, 6x lebih mahal ($3,00/$15,00 vs
        # $0,50/$2,50 per juta token) tanpa ketahuan. Keputusan eksplisit operator:
        # non-fast SELALU. Kalau bentuk API ModelSelection berubah setelah update
        # SDK, lebih baik sesi gagal dibangun (exception naik -> fallback chain
        # gratis) daripada diam-diam membakar kuota Fast.
        params = [sdk.ModelParameterValue(id="fast", value=fast)]
        if effort:
            # grok-4.5: effort low/medium/high (resmi di ModelParameterDefinition).
            params.append(sdk.ModelParameterValue(id="effort", value=effort))
        model = sdk.ModelSelection(id=model_id, params=params)

        key = (self.config.get("cursor_api_key") or os.environ.get("CURSOR_API_KEY") or "").strip()
        self._agent = sdk.Agent.create(
            client=self._client,
            model=model,
            api_key=key,
            # `setting_sources` SENGAJA dihilangkan: tanpa itu rules/plugin proyek tidak
            # ikut termuat, jadi agen tidak mewarisi konfigurasi Cursor milik operator.
            local=sdk.LocalAgentOptions(cwd=scratch),
        )
        self.turn_count = 0
        self.created_at = time.monotonic()
        self.dirty = False
        self.dirty_reason = ""
        self.warmed = False

    def send_collect(
        self,
        system_prompt: str,
        user_content: str,
        *,
        image_paths: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> CursorResult:
        """Kirim satu turn dan kumpulkan balasannya. BLOCKING."""
        cfg = self.config
        if timeout_s is None:
            timeout_s = role_timeout_sec(self.role, cfg)
        t0 = time.monotonic()

        need, why = should_recycle(self.turn_count, self.age_sec, self.dirty, cfg)
        if need:
            # `why` dulu dihitung lalu DIBUANG. Akibatnya log cuma memperlihatkan
            # akibatnya ("sesi belum hangat — turn ini lewat Groq") tanpa pernah
            # menyebut sebabnya, dan tiap pemanasan ulang memakan 13-20 detik di
            # mana SEMUA giliran jatuh ke Groq. Sesi [date removed]: 29x "belum hangat"
            # lawan 19 panggilan composer — mayoritas suara yang didengar penonton
            # ternyata BUKAN composer, dan tidak ada satu baris pun yang menjelaskan.
            print(f"[Cursor] daur ulang sesi {self.role}: {why}")
            self.close(detach=True)
        try:
            self.ensure()
        except Exception as exc:  # noqa: BLE001
            self.mark_dirty(f"ensure gagal: {type(exc).__name__}")
            return CursorResult(
                reason=REASON_ERROR,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        # Persona dikirim TIAP turn, bukan sekali di awal: `llm_system` berubah setiap
        # giliran (bridge menyuntik konteks layar / watch-party), jadi persona yang
        # di-prime sekali akan basi.
        if role_allows_tools(self.role, cfg):
            header = (
                "Boleh memakai WEB SEARCH untuk menjawab. Tetap DILARANG membaca, "
                "membuat, atau menulis berkas, dan dilarang menjalankan perintah.\n\n"
            )
        else:
            header = TOOL_BAN_HEADER
        prompt = f"{header}{system_prompt}\n\n{user_content}"

        try:
            send_opts = None
            try:
                # local force: hindari AgentBusyError dari run yatim setelah barge-in PTT
                send_opts = self._sdk.SendOptions(mode="agent", local={"force": True})
            except Exception:  # noqa: BLE001
                send_opts = None
            # Gambar lewat UserMessage(text, images=[SDKImage]) — Agent.send TIDAK
            # punya kwarg images (terverifikasi spike_grok_vision: TypeError).
            payload: Any = prompt
            if image_paths:
                imgs = [self._sdk.SDKImage.from_file(p) for p in image_paths]
                payload = self._sdk.UserMessage(text=prompt, images=imgs)
            run = self._agent.send(payload, send_opts) if send_opts else self._agent.send(payload)
            collected = collect_run_messages(run.messages(), timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            self.mark_dirty(name)
            reason = REASON_ERROR
            if "RateLimit" in name:
                reason = REASON_RATELIMIT
            elif "Busy" in name:
                reason = REASON_BUSY
            return CursorResult(
                reason=reason,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        self.turn_count += 1
        latency = int((time.monotonic() - t0) * 1000)

        if collected.timed_out:
            self.mark_dirty("timeout")
            return CursorResult(reason=REASON_TIMEOUT, latency_ms=latency,
                                tool_calls=collected.tool_calls)

        if (
            collected.tool_calls
            and cfg.get("cursor_reject_on_tool_call", True)
            and not role_allows_tools(self.role, cfg)
        ):
            # Tool call = agen melenceng dari tugasnya. Ditolak WALAU ada teks.
            # Pengecualian: role lookup — web search justru tugasnya.
            return CursorResult(reason=REASON_TOOL_CALL, latency_ms=latency,
                                tool_calls=collected.tool_calls)

        text = (collected.text or "").strip()
        if len(text) < 2:
            return CursorResult(reason=REASON_EMPTY, latency_ms=latency,
                                tool_calls=collected.tool_calls)

        sentences: list[str] = []
        try:
            from arti_groq_stream import split_indonesian_sentences  # noqa: PLC0415

            parts = split_indonesian_sentences(text)
            # Kontrak sama dengan jalur Groq (bridge baris ~5308): list hanya diisi
            # kalau benar-benar lebih dari satu kalimat.
            if len(parts) > 1:
                sentences = parts
        except Exception:  # noqa: BLE001
            pass

        self.warmed = True
        return CursorResult(
            text=text,
            sentences=sentences,
            ok=True,
            reason=REASON_OK,
            latency_ms=latency,
            tool_calls=collected.tool_calls,
            model="/".join(filter(None, resolve_role_model(self.role, cfg))),
        )

    def mark_dirty(self, reason: str) -> None:
        # Ikut dicetak: sesi kotor = giliran BERIKUTNYA membayar cold start
        # 13-20 detik lewat Groq. Tanpa baris ini, sebabnya tidak pernah kelihatan.
        self.dirty = True
        self.dirty_reason = reason
        print(f"[Cursor] sesi {getattr(self, 'role', '?')} ditandai kotor: {reason}")

    def close(self, detach: bool = False) -> None:
        """Tutup sesi. `detach=True` memindahkan teardown ke thread daemon.

        Turn yang gagal harus bisa jatuh ke Groq dalam milidetik, bukan menunggu
        proses bridge mati.
        """
        agent, proc = self._agent, self._proc
        self._agent = self._client = self._proc = None
        self.turn_count = 0
        self.created_at = 0.0
        self.warmed = False

        def _teardown() -> None:
            try:
                if agent is not None and hasattr(agent, "__exit__"):
                    agent.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            _terminate(proc)

        if agent is None and proc is None:
            return
        if detach:
            threading.Thread(target=_teardown, daemon=True, name="cursor-teardown").start()
        else:
            _teardown()


# --------------------------------------------------------------------------- #
# API level modul
# --------------------------------------------------------------------------- #

_session: CursorSession | None = None
# Cadangan yang dipanaskan di latar SEBELUM sesi aktif dibuang (tukar panas).
# Hidup paling lama beberapa menit: dipanaskan saat sesi aktif mendekati batas,
# lalu langsung naik takhta di giliran berikutnya.
_standby: CursorSession | None = None
_standby_warming = False
_session_lock = threading.Lock()
_consecutive_failures = 0
_breaker_open = False
_breaker_opened_at = 0.0
_warming = False


def is_warm() -> bool:
    """Sesi siap dipakai sekarang tanpa membayar cold start?

    acquire(timeout): lock yang digenggam thread send macet TIDAK boleh
    menyandera pemanggil — terukur live 2026-08-01: turn menunggu 75 detik
    (vts_mikir=75589ms) karena semua pintu lock memakai `with` blocking.
    Lock sibuk = sesi sedang dipakai = anggap belum siap, layani via Groq.
    """
    if not _session_lock.acquire(timeout=0.25):
        return False
    try:
        return (
            _session is not None
            and _session._agent is not None
            and _session.warmed
            and not _session.dirty
        )
    finally:
        _session_lock.release()


def prewarm(config: dict) -> bool:
    """Panaskan sesi di LATAR BELAKANG. Return True kalau sudah hangat sekarang.

    KENAPA WAJIB ADA — tanpa ini jalur Cursor tidak akan pernah terpakai:

        cursor_timeout_sec default 5 detik, tapi turn PERTAMA terukur 18 detik
        (menyalakan bridge SDK + Agent.create + send pertama). Jadi chat pertama
        timeout -> sesi ditandai rusak -> didaur ulang -> chat berikutnya dingin lagi
        -> timeout lagi. Sesi tidak pernah sempat hangat, dan setiap chat jatuh ke Groq.

    Setelah hangat, turn berikutnya terukur 3,4-3,5 detik — nyaman di bawah timeout.

    Fungsi ini TIDAK PERNAH memblokir pemanggil. Kalau sesi belum siap, ia menyalakan
    thread daemon lalu langsung return False supaya turn itu jatuh ke Groq tanpa
    menunggu. Chat berikutnya barulah memakai Cursor.
    """
    global _warming

    # WAJIB lewat pengecekan cooldown, bukan flag mentah: prewarm dipanggil SEBELUM
    # send_turn di bridge, jadi kalau di sini cuma `if _breaker_open: return False`,
    # turn selalu belok ke Groq sebelum send_turn sempat mengevaluasi cooldown —
    # breaker tidak akan pernah menutup kembali. Terdeteksi saat wiring cooldown.
    # acquire(timeout): jangan tersandera thread send yang macet (stall 75 dtk live).
    if not _session_lock.acquire(timeout=0.5):
        return False
    try:
        allowed = _breaker_allows_attempt(config)
    finally:
        _session_lock.release()
    if not allowed:
        return False
    ok, _why = is_available(config)
    if not ok:
        return False
    if is_warm():
        # KEBIJAKAN KEMBAR (operator [date removed], dari log [time removed]: 24 timeout, breaker
        # 7x, tukar panas 0x): cadangan dipanaskan SELALU, bukan cuma saat
        # sesi aktif mendekati batas daur ulang — sesi semalam mati MUDA oleh
        # timeout, jalur kematian yang tidak pernah disiapkan cadangannya.
        # panaskan_cadangan murah dipanggil berulang (return cepat kalau
        # cadangan sudah hangat / sedang dipanaskan).
        panaskan_cadangan(config)
        return True

    # Sesi aktif TIDAK hangat (kotor karena timeout / belum ada). Sebelum
    # membayar pemanasan penuh: kalau kembarannya hangat, naikkan takhta
    # SEKARANG — giliran INI tetap dilayani composer, nol jendela Groq.
    # (Inilah "tukar menukar antara 2 instance"-nya operator; dulu tukar panas
    # hanya terpasang di jalur daur ulang terhormat.)
    ganti = False
    if _session_lock.acquire(timeout=0.25):
        try:
            ganti = tukar_ke_cadangan("sesi aktif mati muda")
        finally:
            _session_lock.release()
    if ganti:
        # Bangun kembaran baru di latar untuk kematian berikutnya.
        panaskan_cadangan(config)
        return True

    with _session_lock:
        if _warming:
            return False
        _warming = True

    def _warm() -> None:
        global _session, _warming
        try:
            with _session_lock:
                if _session is None:
                    _session = CursorSession(config)
                else:
                    _session.config = config
                sess = _session
            t0 = time.monotonic()
            sess.ensure()
            # Satu pesan pemanas: `ensure()` cuma membangun agen; giliran PERTAMA-lah
            # yang mahal. Bayar di sini, di luar giliran penonton.
            try:
                run = sess._agent.send(
                    f"{TOOL_BAN_HEADER}Balas persis satu kata: siap"
                )
                collect_run_messages(run.messages(), timeout_s=60)
                sess.turn_count += 1
                sess.warmed = True
            except Exception:  # noqa: BLE001 — pemanasan gagal bukan alasan bisu
                sess.mark_dirty("prewarm send gagal")
            print(f"[Cursor] sesi hangat dalam {time.monotonic() - t0:.1f}s")
            # Kebijakan kembar: begitu sesi utama hangat, langsung siapkan
            # kembarannya — jangan tunggu giliran berikutnya.
            panaskan_cadangan(config)
        except Exception as exc:  # noqa: BLE001
            print(f"[Cursor] pemanasan gagal: {type(exc).__name__}: {exc}")
        finally:
            with _session_lock:
                _warming = False

    threading.Thread(target=_warm, daemon=True, name="cursor-prewarm").start()
    return False


def _cadangan_hangat() -> bool:
    """Cadangan siap naik takhta? Pemanggil sudah memegang lock ATAU tidak butuh presisi."""
    s = _standby
    return s is not None and s._agent is not None and s.warmed and not s.dirty


def panaskan_cadangan(config: dict) -> bool:
    """Panaskan sesi PENGGANTI di latar, selagi sesi aktif masih melayani.

    TIDAK PERNAH memblokir pemanggil (pola sama dengan `prewarm`). Return True
    kalau cadangan sudah siap sekarang.
    """
    global _standby, _standby_warming

    if not _session_lock.acquire(timeout=0.25):
        return False
    try:
        if _cadangan_hangat():
            return True
        if _standby_warming:
            return False
        _standby_warming = True
        cadangan = CursorSession(config)
        _standby = cadangan
    finally:
        _session_lock.release()

    def _warm_cadangan() -> None:
        global _standby, _standby_warming
        t0 = time.monotonic()
        try:
            cadangan.ensure()
            # Pesan pemanas yang sama dengan sesi utama: giliran PERTAMA yang mahal,
            # dan di sinilah tempatnya dibayar — bukan di giliran penonton.
            run = cadangan._agent.send(
                f"{TOOL_BAN_HEADER}Balas persis satu kata: siap"
            )
            collect_run_messages(run.messages(), timeout_s=60)
            cadangan.turn_count += 1
            cadangan.warmed = True
            print(f"[Cursor] cadangan hangat dalam {time.monotonic() - t0:.1f}s")
        except Exception as exc:  # noqa: BLE001
            print(
                f"[Cursor] pemanasan cadangan gagal: {type(exc).__name__}: {exc}"
            )
            with _session_lock:
                if _standby is cadangan:
                    _standby = None
            try:
                cadangan.close(detach=True)
            except Exception:  # noqa: BLE001
                pass
        finally:
            with _session_lock:
                _standby_warming = False

    threading.Thread(
        target=_warm_cadangan, daemon=True, name="cursor-standby"
    ).start()
    return False


def tukar_ke_cadangan(alasan: str) -> bool:
    """Naikkan cadangan jadi sesi aktif. PEMANGGIL WAJIB memegang `_session_lock`.

    Sesi lama ditutup `detach=True` — teardown-nya pindah ke thread daemon supaya
    tidak menambah satu milidetik pun ke giliran yang sedang berjalan.
    """
    global _session, _standby
    if not _cadangan_hangat():
        return False
    lama = _session
    _session, _standby = _standby, None
    print(f"[Cursor] tukar panas ({alasan}) — cadangan naik takhta, nol cold start")
    if lama is not None:
        lama.close(detach=True)
    return True


def breaker_state() -> dict:
    return {
        "open": _breaker_open,
        "consecutive_failures": _consecutive_failures,
        "session": _session is not None,
        "turn_count": _session.turn_count if _session else 0,
    }


def mark_dirty_global(reason: str) -> None:
    """Tandai sesi rusak dari luar (mis. saat turn dibatalkan barge-in PTT).

    TIDAK boleh blocking: dipanggil bridge persis SETELAH outer_timeout — saat
    thread send yang macet masih menggenggam lock. Kalau lock sibuk, penandaan
    dititipkan ke thread daemon yang menunggu lock bebas.
    """
    if _session_lock.acquire(timeout=0.5):
        try:
            if _session is not None:
                _session.mark_dirty(reason)
        finally:
            _session_lock.release()
        return

    def _later() -> None:
        with _session_lock:
            if _session is not None:
                _session.mark_dirty(reason)

    threading.Thread(target=_later, daemon=True, name="cursor-mark-dirty").start()


def reset_breaker() -> None:
    global _consecutive_failures, _breaker_open, _breaker_opened_at
    with _session_lock:
        _consecutive_failures = 0
        _breaker_open = False
        _breaker_opened_at = 0.0


def _breaker_allows_attempt(config: dict) -> bool:
    """Breaker half-open: setelah cooldown, izinkan mencoba lagi.

    Breaker permanen benar untuk live yang DITUNGGUI (operator bisa restart), tapi
    salah untuk dibiarkan seharian: satu gangguan sekejap di sisi Cursor (3 gagal
    beruntun — dan gagalnya instan, 0,02 dtk) mematikan Composer untuk SISA HARI,
    padahal layanannya mungkin pulih semenit kemudian.

    Setelah `cursor_breaker_cooldown_sec` (default 900 = 15 menit), breaker di-reset
    penuh dan jalur Cursor boleh dicoba lagi. Kalau masih rusak, tiga kegagalan
    berikutnya menutupnya lagi — biaya terburuknya ~3 percobaan gagal-cepat tiap 15
    menit, jauh lebih murah daripada kehilangan Composer 8 jam.

    Set 0 untuk kembali ke perilaku permanen (perlu restart bridge).
    """
    global _consecutive_failures, _breaker_open, _breaker_opened_at
    if not _breaker_open:
        return True
    cooldown = float(config.get("cursor_breaker_cooldown_sec", 900))
    if cooldown <= 0:
        return False
    if time.monotonic() - _breaker_opened_at < cooldown:
        return False
    _consecutive_failures = 0
    _breaker_open = False
    _breaker_opened_at = 0.0
    print(f"[Cursor] breaker cooldown {cooldown:.0f}s lewat — jalur Cursor dicoba lagi")
    return True


def send_turn(system_prompt: str, user_content: str, config: dict) -> CursorResult:
    """Satu-satunya API yang dipakai bridge. BLOCKING — bungkus `asyncio.to_thread`.

    Pemanggil WAJIB juga membungkus dengan `asyncio.wait_for`: deadline internal tidak
    bisa menghentikan iterator yang menggantung (lihat collect_run_messages).
    """
    global _session, _consecutive_failures, _breaker_open, _breaker_opened_at

    if not _session_lock.acquire(timeout=0.5):
        return CursorResult(reason=REASON_BUSY)
    try:
        allowed = _breaker_allows_attempt(config)
    finally:
        _session_lock.release()
    if not allowed:
        return CursorResult(reason=REASON_DISABLED)

    ok, why = is_available(config)
    if not ok:
        return CursorResult(reason=REASON_UNAVAILABLE, text=None, sentences=[], model=why)

    # acquire(timeout): thread turn sebelumnya yang macet masih bisa menggenggam
    # lock sampai iteratornya mati. Turn live TIDAK ikut menunggu (terukur 75
    # dtk stall) — REASON_BUSY membuat bridge langsung jatuh ke Groq.
    if not _session_lock.acquire(timeout=1.0):
        return CursorResult(reason=REASON_BUSY)
    try:
        if _session is None:
            _session = CursorSession(config)
        else:
            _session.config = config
            # Sudah waktunya didaur ulang? Kalau cadangan sudah hangat, tukar di
            # sini — giliran ini tetap dilayani sesi hangat. Kalau belum ada
            # cadangan, `send_collect` menempuh jalur lama (tutup lalu ensure
            # ulang) dan giliran ini membayar cold start seperti dulu.
            perlu, kenapa = should_recycle(
                _session.turn_count, _session.age_sec, _session.dirty, config
            )
            if perlu:
                tukar_ke_cadangan(kenapa)
        result = _session.send_collect(system_prompt, user_content)

        if result.ok:
            _consecutive_failures = 0
        else:
            _consecutive_failures += 1
            limit = int(config.get("cursor_max_consecutive_failures", 3))
            if limit > 0 and _consecutive_failures >= limit:
                _breaker_open = True
                _breaker_opened_at = time.monotonic()
                cooldown = float(config.get("cursor_breaker_cooldown_sec", 900))
                nanti = (
                    f"dicoba lagi dalam {cooldown:.0f}s"
                    if cooldown > 0
                    else "sampai bridge restart"
                )
                print(
                    f"[Cursor] {_consecutive_failures} kegagalan berturut-turut — "
                    f"jalur Cursor ditutup ({nanti}). Sementara semua chat lewat Groq."
                )
        return result
    finally:
        _session_lock.release()


# --------------------------------------------------------------------------- #
# Sesi per-role: scout (composer, scouter per menit), observer (grok-4.5/high,
# ringkas akhir live), vision (composer + gambar)
#
# TERPISAH dari sesi voice: breaker, lock, dan sesi sendiri per role — scouter
# yang ngambek tidak boleh mematikan jalur suara, dan sebaliknya. Konteksnya
# juga tidak boleh campur: sesi voice berisi persona + riwayat chat penonton,
# sesi scout berisi transcript mentah — saling mencemari kalau satu sesi.
# --------------------------------------------------------------------------- #

_role_sessions: dict[str, CursorSession] = {}
_role_locks: dict[str, threading.Lock] = {}
_role_breakers: dict[str, dict] = {}
_registry_lock = threading.Lock()


def _role_lock(role: str) -> threading.Lock:
    with _registry_lock:
        if role not in _role_locks:
            _role_locks[role] = threading.Lock()
        return _role_locks[role]


def _role_breaker(role: str) -> dict:
    with _registry_lock:
        if role not in _role_breakers:
            _role_breakers[role] = {"failures": 0, "open": False, "opened_at": 0.0}
        return _role_breakers[role]


def _role_breaker_allows(role: str, config: dict) -> bool:
    """Half-open per role — logika sama dengan breaker voice."""
    b = _role_breaker(role)
    if not b["open"]:
        return True
    cooldown = float(config.get("cursor_breaker_cooldown_sec", 900))
    if cooldown <= 0:
        return False
    if time.monotonic() - b["opened_at"] < cooldown:
        return False
    b.update(failures=0, open=False, opened_at=0.0)
    print(f"[Cursor:{role}] breaker cooldown lewat — dicoba lagi")
    return True


def send_task(
    role: str,
    system_prompt: str,
    user_content: str,
    config: dict,
    *,
    image_paths: list[str] | None = None,
) -> CursorResult:
    """Satu tugas non-voice (scout/vision/observer) lewat sesi role. BLOCKING.

    Dipanggil dari thread sinkron (scouter thread, observer shutdown, vision
    chain) — pengaman iterator-menggantung di sini memakai join-timeout thread,
    padanan sinkron dari `asyncio.wait_for` di jalur voice (pelajaran #2 di
    header modul: deadline internal tidak bisa menghentikan iterator macet).
    """
    if role == "voice":
        raise ValueError("role voice pakai send_turn()")
    if role not in KNOWN_ROLES and role not in _warned_unknown_roles:
        # Typo di config["cursor_role"] dulu jatuh diam-diam ke default
        # composer-2.5 — observer bisa turun kelas tanpa satu pun peringatan
        # (audit [date removed]). Tetap jalan (jangan matikan siaran); peringatan
        # SEKALI per role — di cadence scouter, tiap-call = ~60 baris/jam
        # (pelajaran banjir terminal 2/8).
        _warned_unknown_roles.add(role)
        print(
            f"[Cursor] role tidak dikenal: {role!r} — dilayani default "
            f"{resolve_role_model(role, config)[0]}. Cek config cursor_role."
        )

    if not _role_breaker_allows(role, config):
        return CursorResult(reason=REASON_DISABLED)
    ok, why = is_available(config)
    if not ok:
        return CursorResult(reason=REASON_UNAVAILABLE, model=why)

    timeout_s = role_timeout_sec(role, config)
    lock = _role_lock(role)
    if not lock.acquire(timeout=timeout_s):
        return CursorResult(reason=REASON_BUSY)
    try:
        with _registry_lock:
            sess = _role_sessions.get(role)
            if sess is None:
                sess = CursorSession(config, role=role)
                _role_sessions[role] = sess
            else:
                sess.config = config

        box: dict[str, CursorResult] = {}

        def _work() -> None:
            box["r"] = sess.send_collect(
                system_prompt, user_content,
                image_paths=image_paths, timeout_s=timeout_s,
            )

        t = threading.Thread(target=_work, daemon=True, name=f"cursor-{role}")
        t.start()
        t.join(timeout=timeout_s + 10.0)
        if t.is_alive():
            # Iterator macet: buang sesinya (teardown detached mematikan proses
            # bridge SDK -> iterator di thread yatim ikut mati), lapor timeout.
            sess.mark_dirty("hung iterator")
            sess.close(detach=True)
            result = CursorResult(reason=REASON_TIMEOUT,
                                  latency_ms=int(timeout_s * 1000))
        else:
            result = box.get("r") or CursorResult(reason=REASON_ERROR)

        b = _role_breaker(role)
        if result.ok:
            b["failures"] = 0
        else:
            b["failures"] += 1
            limit = int(config.get("cursor_max_consecutive_failures", 3))
            if limit > 0 and b["failures"] >= limit:
                b.update(open=True, opened_at=time.monotonic())
                print(
                    f"[Cursor:{role}] {b['failures']} kegagalan berturut — "
                    "role ini ditutup sementara; chain gratis mengambil alih."
                )
        return result
    finally:
        lock.release()


def shutdown_session() -> None:
    """Tutup semua sesi saat proses berakhir.

    Didaftarkan lewat `atexit` DI MODUL INI, bukan dipanggil dari `main_loop`:
    a private regression test mem-parse AST body `main_loop` dan mengunci
    urutan pemanggilannya.
    """
    global _session, _standby
    with _session_lock:
        if _session is not None:
            _session.close(detach=False)
            _session = None
        # Cadangan juga punya proses agen sendiri — tanpa baris ini dia jadi
        # yatim saat bridge ditutup.
        if _standby is not None:
            _standby.close(detach=False)
            _standby = None
    with _registry_lock:
        for sess in _role_sessions.values():
            sess.close(detach=False)
        _role_sessions.clear()


atexit.register(shutdown_session)
