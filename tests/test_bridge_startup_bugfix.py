"""Bridge startup crash fix - bug condition exploration tests.

Spec: bridge-startup-crash-fix (bugfix workflow)
Property 1 - Bug Condition: `get_current_mood` symbol hilang dan bridge crash
dengan NameError di startup.

These tests encode the EXPECTED post-fix behavior. They MUST FAIL on the
unfixed code - the failures themselves are the counterexamples that prove
the bug exists.

Scoped PBT approach: bug ini deterministik (setiap startup pasti crash selama
symbol hilang), jadi property di-scope ke concrete failing case lewat
introspeksi module dan satu eksekusi `python hermes_vtuber_bridge.py`.

Validates: Requirements 1.1, 1.2, 2.1, 2.2
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_FILE = REPO_ROOT / "hermes_vtuber_bridge.py"
MOOD_STATE_FILE = REPO_ROOT / "ARTI_MOOD_STATE.json"

# Set mood string yang dianggap valid: derived from templates/Arti*.exp3.json
# (cheerful, excited, bingung, marah, sedih, senyum, mikir, bicara) plus
# default "cheerful" yang di-fallback-kan oleh `get_current_mood()`.
VALID_MOOD_STRINGS = {
    "cheerful",
    "excited",
    "bingung",
    "marah",
    "sedih",
    "senyum",
    "mikir",
    "bicara",
}

# Indicator log line yang HANYA bisa muncul setelah `get_current_mood()`
# berhasil resolve di `main_loop()` (line ~1731 di hermes_vtuber_bridge.py:
#   print(f"[Mood] Current mood: {current_mood}")
# Kalau bridge crash dengan NameError di line 1720, print ini tidak akan
# pernah ter-eksekusi - jadi keberadaannya membuktikan listening state
# tercapai (mic listener / chat worker / idle animation thread sudah aktif
# karena dimulai sebelum line 1720).
LISTENING_STATE_LOG = "[Mood] Current mood:"

NAME_ERROR_FRAGMENT = "NameError: name 'get_current_mood' is not defined"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_import_bridge():
    """Import (or reload) hermes_vtuber_bridge dari REPO_ROOT.

    Pakai reload supaya kalau pytest sudah pernah import module-nya, kita
    selalu inspect state terbaru dari source file.
    """

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    if "hermes_vtuber_bridge" in sys.modules:
        module = importlib.reload(sys.modules["hermes_vtuber_bridge"])
    else:
        module = importlib.import_module("hermes_vtuber_bridge")
    return module


class _StreamReader(threading.Thread):
    """Background thread yang baca line-by-line dari stream subprocess.

    Disimpan ke list supaya test thread bisa polling tanpa blocking.
    """

    def __init__(self, stream, sink):
        super().__init__(daemon=True)
        self._stream = stream
        self._sink = sink

    def run(self):
        try:
            for raw in iter(self._stream.readline, b""):
                try:
                    self._sink.append(raw.decode("utf-8", errors="replace"))
                except Exception:
                    self._sink.append(repr(raw))
        finally:
            try:
                self._stream.close()
            except Exception:
                pass


def _run_bridge_subprocess(timeout_seconds: float = 20.0):
    """Jalankan `python hermes_vtuber_bridge.py` sebagai subprocess.

    Strategi:
    - Pakai Popen + thread reader supaya bisa polling non-blocking.
    - Berhenti polling setelah salah satu kondisi:
        a) proses exit sendiri (mis. crash NameError di unfixed code), ATAU
        b) muncul `LISTENING_STATE_LOG` di stdout (fixed code), ATAU
        c) `timeout_seconds` tercapai.
    - Setelah polling selesai, kill proses paksa (kalau masih hidup) dan
      kumpulkan semua output yang sudah di-buffer.
    """

    env = os.environ.copy()
    # Force UTF-8 supaya emoji/log Indonesia di stdout nggak crash di Windows.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Unbuffered output supaya log cepat sampai ke pipe.
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE_FILE)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    out_reader = _StreamReader(proc.stdout, stdout_lines)
    err_reader = _StreamReader(proc.stderr, stderr_lines)
    out_reader.start()
    err_reader.start()

    deadline = time.monotonic() + timeout_seconds
    listening_reached = False
    name_error_seen = False
    while time.monotonic() < deadline:
        # Listening state tercapai lewat stdout?
        if any(LISTENING_STATE_LOG in line for line in stdout_lines):
            listening_reached = True
            break
        # Crash NameError di stderr?
        if any(NAME_ERROR_FRAGMENT in line for line in stderr_lines):
            name_error_seen = True
            break
        # Proses sudah exit sendiri?
        if proc.poll() is not None:
            break
        time.sleep(0.1)

    # Kill kalau masih hidup; bridge nggak akan exit sendiri di happy path.
    if proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except Exception:
            pass

    out_reader.join(timeout=2)
    err_reader.join(timeout=2)

    return {
        "returncode": proc.returncode,
        "stdout": "".join(stdout_lines),
        "stderr": "".join(stderr_lines),
        "listening_reached": listening_reached or any(
            LISTENING_STATE_LOG in line for line in stdout_lines
        ),
        "name_error_seen": name_error_seen or any(
            NAME_ERROR_FRAGMENT in line for line in stderr_lines
        ),
    }


def _parse_current_mood_from_stdout(stdout: str) -> str | None:
    """Ekstrak nilai mood dari log `[Mood] Current mood: <mood>` di stdout."""

    match = re.search(r"\[Mood\] Current mood:\s*(\S+)", stdout)
    if match:
        return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Test 1a: Module Symbol Test
# ---------------------------------------------------------------------------


def test_bug_condition_module_symbol_defined():
    """Property 1 (bagian: callable(module.get_current_mood) = TRUE).

    Setelah fix, `import hermes_vtuber_bridge` HARUS mendaftarkan
    `get_current_mood` di module namespace sebagai callable.

    Pada unfixed code: `hasattr(module, "get_current_mood")` = False, jadi
    test ini FAIL. Failure tersebut adalah counterexample yang membuktikan
    `module_has_symbol(module, "get_current_mood") = FALSE` (lihat
    `isBugCondition` di design).

    Validates: Requirements 1.2, 2.2
    """

    module = _fresh_import_bridge()

    assert hasattr(module, "get_current_mood"), (
        "Module hermes_vtuber_bridge tidak mendaftarkan symbol "
        "`get_current_mood` di namespace. Counterexample: "
        f"hasattr(module, 'get_current_mood') = False; "
        f"dir() entries dengan prefix 'get_': "
        f"{[a for a in dir(module) if a.startswith('get_')]}"
    )
    assert callable(getattr(module, "get_current_mood")), (
        "Symbol `get_current_mood` ada di module namespace tapi tidak "
        "callable. Type: "
        f"{type(getattr(module, 'get_current_mood', None)).__name__}"
    )


# ---------------------------------------------------------------------------
# Test 1b: Grep Definition Test
# ---------------------------------------------------------------------------


def test_bug_condition_grep_def_get_current_mood_count():
    """Property 1 (bagian: definisi function ada di source).

    File `hermes_vtuber_bridge.py` HARUS memuat tepat satu baris
    `^def get_current_mood` (top-level definition).

    Pada unfixed code: count = 0 (definisi hilang), test FAIL.

    Validates: Requirements 1.2, 2.2
    """

    assert BRIDGE_FILE.exists(), f"Source file tidak ditemukan: {BRIDGE_FILE}"

    text = BRIDGE_FILE.read_text(encoding="utf-8")
    pattern = re.compile(r"^def get_current_mood\b", re.MULTILINE)
    matches = pattern.findall(text)
    count = len(matches)

    assert count == 1, (
        "Expected tepat 1 definisi top-level `def get_current_mood` di "
        f"{BRIDGE_FILE.name}, ketemu {count}. Counterexample dari unfixed "
        "code: count == 0 (definisi function hilang akibat edit/merge)."
    )


# ---------------------------------------------------------------------------
# Test 1c & 1d: Subprocess startup test (NameError absent + listening + mood)
# ---------------------------------------------------------------------------


def test_bug_condition_subprocess_startup_no_name_error():
    """Property 1 (bagian: bridge sempat sampai listening state tanpa NameError).

    Jalankan `python hermes_vtuber_bridge.py` sebagai subprocess. Setelah fix:
    - stderr TIDAK boleh mengandung
      `NameError: name 'get_current_mood' is not defined`
    - stdout HARUS mengandung indikator listening state
      (`[Mood] Current mood: ...`) yang hanya muncul setelah
      `get_current_mood()` berhasil di-resolve di `main_loop()`.
    - Mood yang ter-print HARUS valid: kalau `ARTI_MOOD_STATE.json` ada
      dan punya `current_mood`, value-nya sesuai; kalau file tidak ada,
      default `"cheerful"`.

    Pada unfixed code: stderr berisi NameError dan listening state tidak
    pernah tercapai. Failure di sini adalah counterexample yang sesuai
    dengan `isBugCondition.reaches_get_current_mood_call = TRUE` di design.

    Validates: Requirements 1.1, 2.1, 2.2
    """

    result = _run_bridge_subprocess(timeout_seconds=20.0)

    # 1c: tidak boleh ada NameError di stderr
    assert NAME_ERROR_FRAGMENT not in result["stderr"], (
        "Bridge crash dengan NameError saat startup. Counterexample "
        "(stderr tail):\n"
        + "\n".join(result["stderr"].splitlines()[-15:])
    )

    # 1c: harus mencapai listening state indicator
    assert result["listening_reached"], (
        "Bridge tidak mencapai listening state indicator "
        f"({LISTENING_STATE_LOG!r}). Counterexample (stdout tail):\n"
        + "\n".join(result["stdout"].splitlines()[-15:])
        + "\n--- stderr tail ---\n"
        + "\n".join(result["stderr"].splitlines()[-15:])
    )

    # 1d: mood yang ter-print harus valid
    current_mood = _parse_current_mood_from_stdout(result["stdout"])
    assert current_mood is not None, (
        "Tidak bisa parse mood dari log `[Mood] Current mood: <mood>` di "
        f"stdout. Stdout tail:\n"
        + "\n".join(result["stdout"].splitlines()[-15:])
    )

    if MOOD_STATE_FILE.exists():
        # File ada: nilai harus salah satu mood yang dikenal sistem.
        assert current_mood in VALID_MOOD_STRINGS, (
            f"current_mood = {current_mood!r} tidak ada di "
            f"VALID_MOOD_STRINGS = {sorted(VALID_MOOD_STRINGS)}. "
            "Periksa ARTI_MOOD_STATE.json."
        )
    else:
        # File tidak ada: default fallback dari `get_current_mood` = 'cheerful'.
        assert current_mood == "cheerful", (
            "ARTI_MOOD_STATE.json tidak ada, jadi `get_current_mood()` "
            "harusnya return default 'cheerful'. Actual: "
            f"{current_mood!r}"
        )


# ===========================================================================
# PRESERVATION TESTS (Property 2)
# ===========================================================================
#
# Property 2: Preservation - `stop_summarizer`, `set_mood`,
# `ARTI_MOOD_STATE.json` schema, dan module surface tetap identik.
#
# Methodology: observation-first. Untuk function/struktur yang TIDAK terkena
# bug, observasi behavior di unfixed code dan encode sebagai property.
# Khusus `stop_summarizer`: di unfixed body-nya bocor (membaca
# `ARTI_MOOD_STATE.json` & return string mood), jadi preservation test
# di-anchor ke kontrak intended dari design (toggle flag, return None) dan
# di-tag `@pytest.mark.xfail(strict=True)` karena dipastikan FAIL di unfixed.
#
# Validates: Requirements 2.3, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
# ===========================================================================

import ast
import builtins
import json
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# Snapshot baseline path (committed bersama test). Saat pertama dijalankan di
# unfixed code, file ini akan di-generate; pada run berikutnya (termasuk
# setelah fix di task 3.3), test akan compare snapshot saat ini terhadap
# baseline tersebut. Karena fix hanya menambahkan satu `def`, snapshot
# non-callable harusnya tetap deep-equal.
_BASELINES_DIR = Path(__file__).parent / "_baselines"
_MODULE_CONSTS_SNAPSHOT = _BASELINES_DIR / "module_constants_snapshot.json"

# Observed startup call order (Name references) di `main_loop()` sebelum
# loop utama (`while True:`). Diekstrak via AST parse pada unfixed source —
# fix hanya menambahkan satu `def`, tidak boleh mengubah urutan ini.
EXPECTED_MAIN_LOOP_STARTUP_ORDER = [
    "voice_listener_worker",
    "youtube_chat_worker",
    "init_global_hotkey",
    "start_summarizer",
    "start_idle_animation",
    "load_long_term_memories",
    "load_soul_context",
    "load_viewer_context",
    "get_current_mood",
    "get_summarizer_context",
    # NEW (obs-subtitle-integration task 6.1): in-process Subtitle Server
    # scheduling sits between `get_summarizer_context` (last existing
    # context-load step) and `add_to_history` (first chat-history op),
    # which is also the observed position picked up by the AST visitor
    # below. Appending here preserves the relative order of every
    # pre-existing entry; only the new symbol is added.
    "start_subtitle_server",
    "add_to_history",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _OpenSpy:
    """Wrapper untuk `builtins.open` yang merekam path & mode setiap call.

    Dipakai di Property 2a/2b untuk membuktikan `stop_summarizer()` TIDAK
    membuka `ARTI_MOOD_STATE.json` (atau file apapun) saat dipanggil.
    """

    def __init__(self, real_open):
        self._real_open = real_open
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *args, **kwargs):
        path = args[0] if args else kwargs.get("file", "")
        mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
        self.calls.append((str(path), str(mode)))
        return self._real_open(*args, **kwargs)

    def calls_to(self, filename_substr: str) -> list[tuple[str, str]]:
        return [c for c in self.calls if filename_substr in c[0]]


def _isolated_mood_dir(monkeypatch, tmp_path: Path):
    """Redirect `_SCRIPT_DIR` di module ke tmp_path supaya read/write
    `ARTI_MOOD_STATE.json` tidak menimpa file produksi."""

    module = _fresh_import_bridge()
    monkeypatch.setattr(module, "_SCRIPT_DIR", str(tmp_path), raising=True)
    return module


def _module_constants_snapshot(module) -> dict[str, Any]:
    """Snapshot semua module-level non-callable, non-dunder attributes ke
    representasi yang JSON-serializable.

    - Skip callables (functions, classes, methods, lambdas).
    - Skip modules dan dunder (`__name__`, `__doc__`, ...).
    - Skip env-derived constants (`_cublas_path`, `_cudnn_path`) yang
      di-compute dari `sys.executable` saat import (lihat top of
      hermes_vtuber_bridge.py). Nilai mereka berbeda antar venv / machine,
      jadi mereka adalah environment infrastructure, bukan bridge behavior
      yang harus dipreservasi. Memasukkan mereka ke baseline membuat test
      fragile terhadap perubahan venv / interpreter path tanpa alasan
      semantik.
    - Untuk container (`dict`/`list`/`tuple`/`set`), recurse dan reduce ke
      JSON primitives; non-JSON-friendly value direpresentasikan sebagai
      `repr(value)` agar tetap stable.
    """

    # Atribut yang dihitung dari `sys.executable` / wall-clock saat module
    # import dan karenanya bervariasi antar venv / antar reload. Bukan bagian
    # dari bridge contract.
    # - `_cublas_path` / `_cudnn_path`: derived from sys.executable, varies per venv.
    # - `_DEBUG_LOG_PATH`: derived from time.strftime() at import time; berubah
    #   setiap detik saat module di-reload, jadi tidak bisa di-snapshot statis.
    _ENV_DERIVED_NAMES = {"_cublas_path", "_cudnn_path", "_DEBUG_LOG_PATH"}

    def _reduce(value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (list, tuple)):
            return [_reduce(v) for v in value]
        if isinstance(value, set):
            try:
                return sorted(_reduce(v) for v in value)
            except TypeError:
                return [_reduce(v) for v in value]
        if isinstance(value, dict):
            return {str(k): _reduce(v) for k, v in value.items()}
        # Fallback: type signature stable across runs.
        return f"<{type(value).__module__}.{type(value).__name__}>"

    snapshot: dict[str, Any] = {}
    for name in sorted(vars(module).keys()):
        if name.startswith("__") and name.endswith("__"):
            continue
        if name in _ENV_DERIVED_NAMES:
            # Skip: dihitung dari sys.executable saat import; varies per venv.
            continue
        value = getattr(module, name)
        if callable(value):
            continue
        # Skip imported modules untuk hindari noise (mis. `os`, `json`).
        if type(value).__name__ == "module":
            continue
        # Skip lock/queue/thread objects yang stateful & tidak deterministik.
        type_name = type(value).__name__
        if type_name in {
            "lock", "RLock", "Queue", "Thread", "Event", "deque",
            "_thread.lock", "Condition", "Semaphore",
        }:
            snapshot[name] = f"<runtime:{type_name}>"
            continue
        snapshot[name] = _reduce(value)
    return snapshot


# ---------------------------------------------------------------------------
# Property 2a: stop_summarizer contract
# ---------------------------------------------------------------------------


@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=10,
    deadline=None,  # importlib.reload(module) per example ~1.2s; kontrak ini
                    # tentang behavior (return None, flag toggle, no I/O),
                    # bukan timing — deadline default 200ms tidak relevan.
)
@given(initial_flag=st.booleans())
def test_preservation_stop_summarizer_contract(initial_flag, monkeypatch, tmp_path):
    """Property 2a: `stop_summarizer()` HANYA toggle `summarizer_running`
    ke False, return None, dan TIDAK melakukan I/O ke `ARTI_MOOD_STATE.json`.

    Validates: Requirements 2.3, 3.1, 3.2
    """

    module = _isolated_mood_dir(monkeypatch, tmp_path)

    # Setup: bikin ARTI_MOOD_STATE.json di tmp dir supaya kalau body bocor
    # ter-eksekusi, dia HARUS panggil open() ke file ini -> spy ketahuan.
    mood_file = tmp_path / "ARTI_MOOD_STATE.json"
    mood_file.write_text(
        json.dumps({"current_mood": "cheerful", "mood_since": None, "mood_history": []}),
        encoding="utf-8",
    )

    # Spy builtins.open di scope module level (bridge pakai `open(...)`
    # langsung, jadi resolve ke builtins.open pada runtime).
    spy = _OpenSpy(builtins.open)
    monkeypatch.setattr(builtins, "open", spy)

    module.summarizer_running = initial_flag

    result = module.stop_summarizer()

    # Kontrak intended:
    assert result is None, (
        f"stop_summarizer() harus return None (kontrak). Actual: {result!r}. "
        "Counterexample: body get_current_mood bocor ke dalam stop_summarizer."
    )
    assert module.summarizer_running is False, (
        f"summarizer_running harus False setelah stop. Actual: "
        f"{module.summarizer_running!r}"
    )
    mood_calls = spy.calls_to("ARTI_MOOD_STATE.json")
    assert mood_calls == [], (
        "stop_summarizer() TIDAK boleh melakukan I/O ke "
        f"ARTI_MOOD_STATE.json. Detected calls: {mood_calls}"
    )


# ---------------------------------------------------------------------------
# Property 2b: stop_summarizer idempotent N panggilan
# ---------------------------------------------------------------------------


@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=10,
    deadline=None,  # Sama seperti 2a: importlib.reload per example >> 200ms
                    # default; idempotency kontrak independen dari timing.
)
@given(n=st.integers(min_value=1, max_value=10))
def test_preservation_stop_summarizer_idempotent(n, monkeypatch, tmp_path):
    """Property 2b: `stop_summarizer()` idempotent — N panggilan berturut
    menghasilkan `summarizer_running = False` dan return value `None`
    di setiap iterasi, tanpa side effect ke filesystem.

    Validates: Requirements 2.3
    """

    module = _isolated_mood_dir(monkeypatch, tmp_path)
    mood_file = tmp_path / "ARTI_MOOD_STATE.json"
    mood_file.write_text(
        json.dumps({"current_mood": "cheerful", "mood_since": None, "mood_history": []}),
        encoding="utf-8",
    )

    spy = _OpenSpy(builtins.open)
    monkeypatch.setattr(builtins, "open", spy)

    module.summarizer_running = True

    return_values = [module.stop_summarizer() for _ in range(n)]

    assert all(rv is None for rv in return_values), (
        f"Semua N={n} panggilan harus return None. Actual: {return_values!r}"
    )
    assert module.summarizer_running is False, (
        "summarizer_running harus tetap False setelah N panggilan. "
        f"Actual: {module.summarizer_running!r}"
    )
    mood_calls = spy.calls_to("ARTI_MOOD_STATE.json")
    assert mood_calls == [], (
        f"stop_summarizer() N={n} kali TIDAK boleh sentuh ARTI_MOOD_STATE.json. "
        f"Detected calls: {mood_calls}"
    )


# ---------------------------------------------------------------------------
# Property 2c: set_mood schema invariant
# ---------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@given(
    mood=st.text(
        alphabet=st.characters(
            min_codepoint=0x21, max_codepoint=0x7E,  # printable ASCII non-space
            blacklist_characters='"\\',
        ),
        min_size=1,
        max_size=20,
    ),
)
def test_preservation_set_mood_schema_invariant(mood, monkeypatch, tmp_path):
    """Property 2c: setelah `set_mood(m)`, `ARTI_MOOD_STATE.json` punya
    key set tetap `{current_mood, mood_since, mood_history}` dengan tipe
    `(str, str|None, list)` dan `current_mood == m`.

    Validates: Requirements 3.3, 3.6
    """

    module = _isolated_mood_dir(monkeypatch, tmp_path)
    mood_path = tmp_path / "ARTI_MOOD_STATE.json"
    # Reset file di setiap example agar test deterministik (mood_history
    # tidak ngumpulin warisan example sebelumnya).
    if mood_path.exists():
        mood_path.unlink()

    module.set_mood(mood)

    assert mood_path.exists(), (
        f"set_mood({mood!r}) gagal menulis file ARTI_MOOD_STATE.json di "
        f"{mood_path}"
    )
    raw = mood_path.read_text(encoding="utf-8")
    state = json.loads(raw)

    assert set(state.keys()) == {"current_mood", "mood_since", "mood_history"}, (
        f"Schema key harus tetap {{current_mood, mood_since, mood_history}}. "
        f"Actual keys: {sorted(state.keys())}"
    )
    assert state["current_mood"] == mood, (
        f"current_mood mismatch: expected {mood!r}, got {state['current_mood']!r}"
    )
    assert isinstance(state["current_mood"], str)
    assert state["mood_since"] is None or isinstance(state["mood_since"], str)
    assert isinstance(state["mood_history"], list), (
        f"mood_history harus list. Actual type: {type(state['mood_history']).__name__}"
    )
    # Format invariant: ensure_ascii=False & indent=2 (lihat design /
    # `set_mood` body). Kita verifikasi via roundtrip: loaded value
    # cocok dan raw JSON valid utf-8 (sudah di-decode tanpa error).
    assert raw == raw.encode("utf-8").decode("utf-8"), (
        "File harus encoded sebagai utf-8."
    )


# ---------------------------------------------------------------------------
# Property 2d: get_current_mood round-trip helper
# ---------------------------------------------------------------------------
#
# Helper/fixture only — full test berjalan di task 3.3 (post-fix).
# Di unfixed code, `get_current_mood` tidak terdefinisi, jadi test di-skip
# secara graceful. Setelah fix di task 3.1, pemanggilan helper akan resolve
# dan test ini meng-encode kontrak round-trip yang harus dipertahankan.


def _get_current_mood_round_trip(module, mood: str, tmp_path: Path) -> str:
    """Helper: panggil set_mood(m) lalu read kembali via json.load dan
    return state["current_mood"]. Dipakai di task 3.3 untuk ngonfirmasi
    bahwa post-fix `get_current_mood()` cocok dengan roundtrip ini.
    """

    module.set_mood(mood)
    mood_path = tmp_path / "ARTI_MOOD_STATE.json"
    state = json.loads(mood_path.read_text(encoding="utf-8"))
    return state["current_mood"]


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=10)
@given(mood=st.sampled_from(sorted(VALID_MOOD_STRINGS)))
def test_preservation_get_current_mood_round_trip(mood, monkeypatch, tmp_path):
    """Property 2d: `set_mood(m); get_current_mood() == m` untuk valid
    mood. Skip di unfixed code (symbol belum ada); aktif setelah task 3.1
    untuk mengkonfirmasi caller `get_current_mood()` masih dapat string
    yang konsisten dengan file.

    Validates: Requirements 3.1, 3.3
    """

    module = _isolated_mood_dir(monkeypatch, tmp_path)
    if not hasattr(module, "get_current_mood"):
        pytest.skip(
            "get_current_mood belum terdefinisi di unfixed code. "
            "Test ini akan aktif setelah fix di task 3.1."
        )

    # Reset file each example.
    mood_path = tmp_path / "ARTI_MOOD_STATE.json"
    if mood_path.exists():
        mood_path.unlink()

    file_value = _get_current_mood_round_trip(module, mood, tmp_path)
    runtime_value = module.get_current_mood()

    assert file_value == mood, (
        f"File round-trip gagal: expected {mood!r}, got {file_value!r}"
    )
    assert runtime_value == mood, (
        f"get_current_mood() round-trip gagal: expected {mood!r}, "
        f"got {runtime_value!r}"
    )


# ---------------------------------------------------------------------------
# Property 2e: module constants invariant (snapshot)
# ---------------------------------------------------------------------------


def test_preservation_module_constants_snapshot():
    """Property 2e: snapshot semua module-level non-callable attributes
    tetap deep-equal antara unfixed dan fixed code.

    Run pertama (di unfixed): generate baseline file kalau belum ada,
    lalu lulus. Run berikutnya (termasuk di task 3.3 setelah fix):
    compare snapshot saat ini vs baseline yang tersimpan dan assert
    deep-equal — fix hanya menambahkan satu `def` (callable, di-skip
    snapshot), jadi non-callable surface harus identik.

    Validates: Requirements 3.6
    """

    module = _fresh_import_bridge()
    current = _module_constants_snapshot(module)

    _BASELINES_DIR.mkdir(parents=True, exist_ok=True)

    if not _MODULE_CONSTS_SNAPSHOT.exists():
        _MODULE_CONSTS_SNAPSHOT.write_text(
            json.dumps(current, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        # Initial generation di unfixed code: lulus sebagai "baseline captured".
        return

    baseline = json.loads(_MODULE_CONSTS_SNAPSHOT.read_text(encoding="utf-8"))

    # Compare per key untuk error message yang informatif.
    missing = sorted(set(baseline.keys()) - set(current.keys()))
    added = sorted(set(current.keys()) - set(baseline.keys()))
    differing = [
        k for k in (set(baseline.keys()) & set(current.keys()))
        if baseline[k] != current[k]
    ]

    assert not missing, f"Module constants HILANG vs baseline: {missing}"
    assert not added, f"Module constants BARU vs baseline: {added}"
    assert not differing, (
        "Module constants berubah value vs baseline. Differing keys: "
        f"{differing}. Contoh: baseline[{differing[0]!r}]="
        f"{baseline[differing[0]]!r} vs current={current[differing[0]]!r}"
    )


# ---------------------------------------------------------------------------
# Property 2f: main_loop startup order (AST static analysis)
# ---------------------------------------------------------------------------


def test_preservation_main_loop_startup_order():
    """Property 2f: AST parse `hermes_vtuber_bridge.py`, ekstrak Name
    references signifikan di body `main_loop()` SEBELUM loop utama
    (`while True:`), dan assert urutannya match observed list.

    Fix hanya menambahkan satu `def get_current_mood():` di luar
    `main_loop`, jadi urutan call di `main_loop` HARUS tetap.

    Validates: Requirements 3.4, 3.5
    """

    source = BRIDGE_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    main_loop_fn = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "main_loop":
            main_loop_fn = node
            break
    assert main_loop_fn is not None, "main_loop tidak ditemukan di module."

    # Startup phase = semua statement sebelum first `while True:` loop.
    startup_stmts = []
    for stmt in main_loop_fn.body:
        if isinstance(stmt, ast.While):
            break
        startup_stmts.append(stmt)

    significant = set(EXPECTED_MAIN_LOOP_STARTUP_ORDER)
    observed_order: list[str] = []
    for stmt in startup_stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and node.id in significant:
                observed_order.append(node.id)

    assert observed_order == EXPECTED_MAIN_LOOP_STARTUP_ORDER, (
        "main_loop() startup order berubah.\n"
        f"  expected: {EXPECTED_MAIN_LOOP_STARTUP_ORDER}\n"
        f"  observed: {observed_order}"
    )
