"""Provider agy persisten untuk giliran suara Arti.

Proses agy hidup lintas giliran agar setup lokal hanya dibayar sekali. Semua
izin berbahaya dikunci di sini (plan + sandbox + slash commands off), proses
berjalan dari direktori sementara kosong, dan setiap kegagalan membuang satu
generation penuh sebelum pemanasan ulang.
"""

from __future__ import annotations

from arti_screen_privacy import screen_privacy

_privacy_epoch = screen_privacy.epoch

import atexit
from dataclasses import dataclass, field
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from typing import Any


DEFAULT_BIN = os.path.expandvars(r"%LOCALAPPDATA%\agy\bin\agy.exe")


@dataclass(frozen=True)
class AgyResult:
    ok: bool
    text: str = ""
    reason: str = ""
    status: str = ""
    ttft_ms: int | None = None
    total_ms: int = 0
    init_ms: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    turn: int = 0
    recycled: bool = False
    model: str = ""
    effort: str = ""


_state_lock = threading.Lock()
_turn_lock = threading.Lock()
_events: queue.Queue[tuple[int, dict[str, Any], float]] = queue.Queue()
_proc = None
_tempdir: tempfile.TemporaryDirectory | None = None
_generation = 0
_ready = False
_warming = False
_turns = 0
_init_ms: int | None = None
_last_config: dict[str, Any] | None = None


def _cfg(config: dict | None, key: str, default):
    value = (config or {}).get(key)
    return default if value is None else value


def _real_process_factory(argv, **kwargs):
    return subprocess.Popen(argv, **kwargs)


_process_factory = _real_process_factory


def _dukung_effort(model: str) -> bool:
    """Hanya model Gemini yang menerima --effort.

    Ditemukan 27 Agu dengan cara yang mahal: memakai claude-sonnet-4-6
    membuat proses MATI di init dengan pesan yang tak pernah terlihat —

        "invalid model selection (--model \"claude-sonnet-4-6\"
         --effort \"medium\"): --effort is not supported for model..."

    ...karena agy mengirimnya sebagai event `result` ber-status ERROR, lalu
    keluar. Gerbang prewarm cuma menunggu event `init`, jadi yang terlihat
    dari luar cuma "init timeout". Tingkat penalaran model Gemini toh sudah
    melekat di NAMANYA (gemini-3.7-flash-low|medium|high), jadi flag ini
    memang cuma milik Gemini.
    """
    return "gemini" in (model or "").lower()


def _argv(config: dict) -> list[str]:
    """Safe flags are deliberately not configurable."""
    model = str(_cfg(config, "agy_model", "gemini-3.7-flash-low"))
    argv = [
        str(_cfg(config, "agy_bin", DEFAULT_BIN)),
        "--input-format=stream-json",
        "--output-format=stream-json",
        f"--model={model}",
    ]
    if _dukung_effort(model):
        argv.append(f"--effort={_cfg(config, 'agy_effort', 'low')}")
    argv += ["--mode=plan", "--sandbox", "--disable-slash-commands"]
    return argv


def _reader(proc, generation: int) -> None:
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            stamp = time.perf_counter()
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                _events.put((generation, event, stamp))
    finally:
        code = proc.poll()
        _events.put((generation, {"event": "__eof__", "returncode": code}, time.perf_counter()))


def _drain_stderr(proc) -> None:
    try:
        while proc.stderr.readline():
            pass
    except Exception:
        pass


def _terminate_process(proc) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except Exception:
                proc.kill()
                proc.wait(timeout=0.5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _discard(reason: str, *, expected_generation: int | None = None,
             rewarm: bool = False) -> bool:
    """Detach one process atomically, then terminate it outside the lock."""
    global _proc, _tempdir, _generation, _ready, _warming, _turns, _init_ms
    with _state_lock:
        if expected_generation is not None and expected_generation != _generation:
            return False
        old_generation = _generation
        proc, tmp = _proc, _tempdir
        config = dict(_last_config or {})
        _proc = None
        _tempdir = None
        _ready = False
        _warming = False
        _turns = 0
        _init_ms = None
        _generation += 1

    # Bangunkan send_turn lama sebelum generation baru mulai mengirim event.
    _events.put((old_generation, {"event": "__aborted__", "reason": reason}, time.perf_counter()))
    _terminate_process(proc)
    if tmp is not None:
        try:
            tmp.cleanup()
        except Exception:
            pass
    if proc is not None:
        print(f"[Agy] proses dibuang ({reason})")
    if rewarm and config:
        prewarm(config)
    return True


def _start_process(config: dict) -> tuple[Any, int]:
    global _proc, _tempdir, _generation, _last_config
    tmp = tempfile.TemporaryDirectory(prefix="arti-agy-")
    kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
        "cwd": tmp.name,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = _process_factory(_argv(config), **kwargs)
    except Exception:
        tmp.cleanup()
        raise

    with _state_lock:
        _generation += 1
        generation = _generation
        _proc = proc
        _tempdir = tmp
        _last_config = dict(config)
    threading.Thread(
        target=_reader, args=(proc, generation), daemon=True, name="agy-stdout"
    ).start()
    threading.Thread(
        target=_drain_stderr, args=(proc,), daemon=True, name="agy-stderr"
    ).start()
    return proc, generation


def is_warm() -> bool:
    with _state_lock:
        return bool(_ready and _proc is not None and _proc.poll() is None)


def _next_event(generation: int, deadline: float):
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return None
        try:
            item_generation, event, stamp = _events.get(timeout=remaining)
        except queue.Empty:
            return None
        if item_generation == generation:
            return event, stamp


def prewarm(config: dict) -> bool:
    """Mulai proses di thread latar; True hanya bila sudah hangat sekarang."""
    global _warming, _ready, _init_ms
    if not bool(_cfg(config, "agy_agent_enabled", False)):
        return False
    # Config produksi tidak boleh membuat pytest menyalakan agy asli. Fake
    # process tetap boleh dipakai untuk menguji lifecycle ini.
    if "PYTEST_CURRENT_TEST" in os.environ and _process_factory is _real_process_factory:
        return False
    if is_warm():
        return True
    with _state_lock:
        if _warming:
            return False
        _warming = True

    def _warm() -> None:
        global _warming, _ready, _init_ms
        t0 = time.perf_counter()
        generation = None
        try:
            proc, generation = _start_process(config)
            timeout = float(_cfg(config, "agy_init_timeout_sec", 15.0))
            deadline = time.perf_counter() + timeout
            while True:
                item = _next_event(generation, deadline)
                if item is None:
                    raise TimeoutError("init timeout")
                event, _stamp = item
                kind = str(event.get("event", "")).lower()
                if kind == "init":
                    break
                if kind == "result":
                    # agy melaporkan penolakan argumen lewat SATU event result
                    # ber-status ERROR lalu keluar. Tanpa cabang ini pesannya
                    # hilang dan yang terlihat cuma "init timeout" ([date removed]:
                    # satu jam habis untuk mencari sebab yang sudah dicetak
                    # agy sejak detik keempat).
                    payload = _result_payload(event)
                    pesan = str(payload.get("error") or payload.get("status") or "")
                    if pesan:
                        raise RuntimeError(f"agy menolak: {pesan[:200]}")
                if kind == "__eof__" or proc.poll() is not None:
                    raise RuntimeError(f"process exit {proc.poll()}")
                if _forbidden_event(event):
                    raise RuntimeError("forbidden event during init")
            elapsed = int((time.perf_counter() - t0) * 1000)
            with _state_lock:
                if generation == _generation and _proc is proc:
                    _ready = True
                    _init_ms = elapsed
            print(f"[Agy] init {elapsed}ms — proses hangat")
        except Exception as exc:
            print(f"[Agy] init gagal: {type(exc).__name__}: {exc}")
            if generation is not None:
                _discard("init gagal", expected_generation=generation)
        finally:
            with _state_lock:
                _warming = False

    threading.Thread(target=_warm, daemon=True, name="agy-prewarm").start()
    return False


# Kunci yang boleh dibaca sebagai LABEL jenis langkah. Sengaja bukan "semua
# string": kalau seluruh nilai string dipindai, satu penonton yang menulis
# kata "tool" di chat akan membunuh proses agy (teks balasan ikut lewat sini
# lewat `text_delta`).
#
# Bentuk NYATA-nya ditangkap dari agy v1.[time removed] pada [date removed], dan bentuk itu
# TIDAK terpakai oleh versi pertama gerbang ini:
#   {"event":"step_update","step_update":{"step_type":"tool",
#    "tool_name":"view_file","tool_info":{"name":"view_file",...}}}
# "step_type" dan "tool_name" tidak ada di {event,type,kind}, jadi panggilan
# tool SUNGGUHAN lolos tanpa suara. Tes lama hijau karena memakai bentuk
# karangan ("update"/"type"), bukan bentuk yang benar-benar dikirim agy.
def _kunci_label(key: str) -> bool:
    return key in {"event", "type", "kind", "name"} or key.endswith(
        ("_type", "_name")
    )


def _event_tags(value: Any, key: str = ""):
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _event_tags(child, str(child_key).lower())
    elif isinstance(value, list):
        for child in value:
            yield from _event_tags(child, key)
    elif isinstance(value, str) and _kunci_label(key):
        yield value.lower()


def _forbidden_event(event: dict[str, Any]) -> bool:
    for tag in _event_tags(event):
        if any(word in tag for word in ("tool", "permission", "approval")):
            return True
    return False


def _first_text(event: dict[str, Any]) -> str:
    kind = str(event.get("event", "")).lower()
    if kind == "result":
        result = event.get("result") or {}
        return str(result.get("response") or "").strip()
    # `step_update.text_delta` adalah tempat teks agy yang SEBENARNYA (bentuk
    # ditangkap [date removed]). Tanpa cabang ini `_first_text` tak pernah menemukan
    # apa pun sebelum event `result`, jadi ttft selalu SAMA PERSIS dengan
    # total — angka "TTFT" di dokumen sebelum tanggal itu sebenarnya total.
    langkah = event.get("step_update")
    if isinstance(langkah, dict):
        teks = langkah.get("text_delta")
        if isinstance(teks, str) and teks.strip():
            return teks.strip()
    update = event.get("update") or event.get("data") or {}
    if isinstance(update, dict):
        for key in ("text", "delta", "response"):
            value = update.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _result_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("result")
    if isinstance(payload, dict):
        return payload
    data = event.get("data")
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        return data["result"]
    return {}


def _failure(reason: str, started: float, *, model: str, effort: str) -> AgyResult:
    return AgyResult(
        ok=False,
        reason=reason,
        status="failed",
        total_ms=int((time.perf_counter() - started) * 1000),
        model=model,
        effort=effort,
    )


def send_turn(system_prompt: str, user_content: str, config: dict) -> AgyResult:
    """Kirim satu NDJSON turn. Hasil gagal berarti caller harus fallback."""
    global _turns, _privacy_epoch
    started = time.perf_counter()
    model = str(_cfg(config, "agy_model", "gemini-3.7-flash-low"))
    effort = str(_cfg(config, "agy_effort", "low"))
    if not bool(_cfg(config, "agy_agent_enabled", False)):
        return _failure("disabled", started, model=model, effort=effort)
    epoch = screen_privacy.epoch
    if not screen_privacy.current(config.get("_screen_privacy_epoch", epoch)):
        return _failure("screen_privacy", started, model=model, effort=effort)
    if _privacy_epoch != epoch:
        _discard("screen privacy boundary")
        _privacy_epoch = epoch
    if not is_warm():
        prewarm(config)
        return _failure("cold", started, model=model, effort=effort)
    if not _turn_lock.acquire(timeout=0.05):
        return _failure("busy", started, model=model, effort=effort)

    try:
        with _state_lock:
            proc = _proc
            generation = _generation
            init_ms = _init_ms
        if proc is None or proc.poll() is not None or not is_warm():
            prewarm(config)
            return _failure("cold", started, model=model, effort=effort)

        message = {
            "event": "user",
            "message": {
                "content": [{
                    "type": "text",
                    "text": f"{system_prompt}\n\n{user_content}",
                }]
            },
        }
        try:
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except Exception:
            _discard("broken pipe", expected_generation=generation, rewarm=True)
            return _failure("broken_pipe", started, model=model, effort=effort)

        timeout = float(_cfg(config, "agy_timeout_sec", 8.0))
        deadline = started + timeout
        ttft_ms = None
        while True:
            item = _next_event(generation, deadline)
            if item is None:
                _discard("timeout", expected_generation=generation, rewarm=True)
                print(f"[Agy] timeout {timeout:.0f}s — jatuh ke Luna")
                return _failure("timeout", started, model=model, effort=effort)
            event, stamp = item
            kind = str(event.get("event", "")).lower()
            if kind == "__aborted__":
                return _failure(
                    str(event.get("reason") or "aborted"), started,
                    model=model, effort=effort,
                )
            if kind == "__eof__" or proc.poll() is not None:
                _discard("crash", expected_generation=generation, rewarm=True)
                return _failure("crash", started, model=model, effort=effort)
            if _forbidden_event(event):
                _discard("event tool/permission", expected_generation=generation, rewarm=True)
                print("[Agy] event tool/permission ditolak — jatuh ke Luna")
                return _failure("forbidden_event", started, model=model, effort=effort)
            if ttft_ms is None and _first_text(event):
                ttft_ms = int((stamp - started) * 1000)
            if kind != "result":
                continue

            payload = _result_payload(event)
            text = str(payload.get("response") or "").strip()
            status = str(payload.get("status") or ("success" if text else "failed"))
            if not text or status.lower() not in {"success", "completed", "ok"}:
                _discard("hasil gagal/kosong", expected_generation=generation, rewarm=True)
                return _failure("empty_or_failed", started, model=model, effort=effort)

            with _state_lock:
                if generation != _generation:
                    return _failure("stale_generation", started, model=model, effort=effort)
                _turns += 1
                turn = _turns
            total_ms = int((time.perf_counter() - started) * 1000)
            usage = payload.get("usage")
            usage = dict(usage) if isinstance(usage, dict) else {}
            max_turns = max(1, int(_cfg(config, "agy_thread_max_turns", 20)))
            recycled = turn >= max_turns
            if recycled:
                print(f"[Agy] recycle setelah turn {turn}")
                _discard("mencapai batas turn", expected_generation=generation, rewarm=True)
            token_log = (
                f" in={usage.get('input_tokens', '?')}"
                f" out={usage.get('output_tokens', '?')}"
                f" cache={usage.get('cache_read_tokens', usage.get('cached_input_tokens', '?'))}"
            )
            print(
                f"[Agy] {model}/{effort} init={init_ms if init_ms is not None else '?'}ms "
                f"ttft={ttft_ms if ttft_ms is not None else '?'}ms total={total_ms}ms "
                f"status={status} turn={turn}{token_log}"
            )
            return AgyResult(
                ok=True,
                text=text,
                status=status,
                ttft_ms=ttft_ms,
                total_ms=total_ms,
                init_ms=init_ms,
                usage=usage,
                turn=turn,
                recycled=recycled,
                model=model,
                effort=effort,
            )
    finally:
        _turn_lock.release()


def abort_turn(reason: str) -> None:
    """Barge-in/cancel: hentikan proses aktif dan bangunkan waiter lama."""
    with _state_lock:
        generation = _generation
        config = dict(_last_config or {})
    _discard(reason or "aborted", expected_generation=generation)
    if config:
        prewarm(config)


def shutdown_session() -> None:
    """Matikan proses tanpa prewarm; aman dipanggil berulang saat shutdown."""
    global _last_config
    with _state_lock:
        generation = _generation
    _discard("shutdown", expected_generation=generation)
    with _state_lock:
        _last_config = None


atexit.register(shutdown_session)



# --- kuota -----------------------------------------------------------------
#
# `/usage` ternyata bekerja di mode --print, jadi bisa dibaca dari skrip.
# Bentuk keluarannya (ditangkap [date removed], TAB sebagai pemisah):
#
#   Gemini Models	Weekly Limit Remaining	98%	2026-09-02T13:49:59Z
#   Gemini Models	Five Hour Limit Remaining	92%	2026-08-27T05:20:08Z
#   Claude and GPT models	Weekly Limit Remaining	100%	...
#
# JANGAN menambahkan --disable-slash-commands di sini: flag itu justru
# mematikan /usage. Ini satu-satunya tempat di modul ini yang memang butuh
# slash command hidup.

def _ringkas_kuota(mentah: str) -> str:
    """Ubah tabel /usage jadi satu baris. String kosong = tidak terbaca."""
    sisa: dict[tuple[str, str], str] = {}
    for baris in (mentah or "").splitlines():
        kolom = [k.strip() for k in baris.split("	") if k.strip()]
        if len(kolom) < 3 or "%" not in kolom[2]:
            continue
        kolam = "gemini" if "gemini" in kolom[0].lower() else (
            "claude+gpt" if "claude" in kolom[0].lower() else kolom[0].lower()
        )
        jenis = "minggu" if "week" in kolom[1].lower() else (
            "5jam" if "five" in kolom[1].lower() else kolom[1].lower()
        )
        sisa[(kolam, jenis)] = kolom[2]
    if not sisa:
        return ""
    bagian = []
    for kolam in ("gemini", "claude+gpt"):
        angka = [f"{j} {sisa[(kolam, j)]}" for j in ("minggu", "5jam")
                 if (kolam, j) in sisa]
        if angka:
            bagian.append(f"{kolam} sisa " + " / ".join(angka))
    return " | ".join(bagian) or ""


def baca_kuota(config: dict | None = None, timeout: float = 60.0,
               *, alasan: list[str] | None = None) -> str:
    """Panggil `/usage` sekali-jalan. Aman gagal: balik string kosong.

    `alasan` (opsional): daftar yang diisi KENAPA gagal. Ditambahkan 27 Agu
    sesudah sesi live pertama — pencatat kuota tidak mencetak apa pun dan
    tidak ada satu pun petunjuk kenapa, karena seluruh kegagalan ditelan
    `except Exception: return ""`. Gagal diam itu persis kelas bug yang
    paling mahal di proyek ini.

    Timeout dinaikkan 25 -> 60 dtk: pembacaan ini terjadi di startup, saat
    Supertone, VTS, Whisper, dan proses agy PERSISTEN sedang naik bersamaan.
    Dua proses agy pada mesin sibuk gampang melewati 25 detik.
    """
    cfg = config or {}
    def _catat(x: str) -> str:
        if alasan is not None:
            alasan.append(x)
        return ""
    if "PYTEST_CURRENT_TEST" in os.environ and not cfg.get("_kuota_tes"):
        return _catat("pytest")
    binari = str(_cfg(cfg, "agy_bin", DEFAULT_BIN))
    if not os.path.isfile(binari):
        return _catat(f"agy.exe tidak ada di {binari}")
    kwargs: dict[str, Any] = {
        "capture_output": True, "text": True, "timeout": timeout,
        "encoding": "utf-8", "errors": "replace", "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        with tempfile.TemporaryDirectory(prefix="arti-agy-kuota-") as tmp:
            hasil = subprocess.run(
                [binari, "--print=/usage", "--output-format=text"],
                cwd=tmp, **kwargs,
            )
        ringkas = _ringkas_kuota(hasil.stdout or "")
        if not ringkas:
            mentah = (hasil.stdout or hasil.stderr or "")[:120]
            cuplik = " ".join(mentah.split())
            return _catat(f"keluaran tidak terbaca: {cuplik!r}")
        return ringkas
    except subprocess.TimeoutExpired:
        return _catat(f"timeout {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001 — kuota tak boleh menjatuhkan bridge
        return _catat(f"{type(exc).__name__}: {exc}")


def lapor_kuota(label: str, config: dict | None = None) -> str:
    """Cetak satu baris kuota. Dipakai bridge saat mulai dan saat pamit.

    Kalau gagal, ALASANNYA ikut dicetak. Versi pertama diam total, dan di
    sesi live 27 Agu baris kuota tidak pernah muncul tanpa satu pun petunjuk.
    """
    kenapa: list[str] = []
    ringkas = baca_kuota(config, alasan=kenapa)
    if ringkas:
        print(f"[Agy] kuota {label}: {ringkas}")
    elif kenapa and kenapa[0] != "pytest":
        print(f"[Agy] kuota {label} TIDAK terbaca — {kenapa[0]}")
    return ringkas
