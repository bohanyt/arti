"""Supertone 3 synthesis server (Python 3.12 subprocess).

This module is the persistent local synthesis server that runs in the
Python 3.12 virtual environment (``venv312``). It communicates with the
bridge (``hermes_vtuber_bridge.py``, Python 3.11) exclusively over its
inherited stdin/stdout pipes using newline-delimited JSON (NDJSON):

    stdin   <- one compact JSON request object per line
    stdout  -> one compact JSON response object per line (NDJSON only)
    stderr  -> human-readable logs only (never protocol data)

It never imports asyncio, sounddevice, or websocket libraries, and it never
creates, binds, or listens on any network socket. All synthesis happens
in-process and the model is kept loaded for the lifetime of the subprocess.

Scope of this module is incremental. This file currently provides the
protocol I/O helpers, module-level constants/state, temp-directory setup,
one-time model load with voice-style caching, the readiness banner, the
``serve_loop()`` framing/version/control-message dispatcher, and the full
``handle_synthesize()`` (validation, tag-preserving number preprocessing,
clamping, synthesis, and WAV save). A subsequent task (3.5) fleshes out the
temp-file pruning/cleanup helpers (``prune_temp_files()`` and
``cleanup_all_temp()`` are currently no-op placeholders).
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

# Pure, dependency-light helper (imports only `re`/`logging`), so it is safe to
# import at module top level in any environment -- including the Python 3.11
# bridge venv used for unit tests where ``supertonic`` is absent.
from text_preprocessor import preprocess_preserving_tags

# --------------------------------------------------------------------------- #
# Module-level constants & state
# --------------------------------------------------------------------------- #

# NDJSON protocol version. Bump on any breaking schema change. Both the bridge
# and this subprocess hardcode this value and reject mismatches.
PROTOCOL_VERSION = 1

# Default voice style id (F1-F5 / M1-M5). F1 is the recommended default.
DEFAULT_VOICE = "F1"

# Temporary working directory for synthesized WAVs. Created at startup.
TEMP_DIR = Path(tempfile.gettempdir()) / "supertone_tmp"

# The loaded ``supertonic.TTS`` instance (loaded exactly once by load_model()).
_tts = None

# Cache of voice-style objects keyed by voice name, so a style is created once
# and reused across requests.
_voice_cache: dict[str, object] = {}


# --------------------------------------------------------------------------- #
# Output helpers
#
# stdout is reserved EXCLUSIVELY for protocol JSON objects; every other
# diagnostic message goes to stderr via log(). Each protocol write is a single
# compact line terminated by "\n" and is flushed immediately because the bridge
# blocks on readline() and would otherwise hang on buffered output.
# --------------------------------------------------------------------------- #


def emit(obj: dict) -> None:
    """Write a single compact JSON protocol object to stdout, then flush.

    The object is serialized with ``json.dumps`` (no indentation, so no
    embedded newlines) and terminated by exactly one trailing newline. The
    stream is flushed after every write so the bridge receives the line
    without buffering delay.
    """
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()  # CRITICAL: bridge blocks on readline()


def emit_error(rid, code: str, message: str) -> None:
    """Emit a protocol error response echoing the originating request id."""
    emit(
        {
            "v": PROTOCOL_VERSION,
            "id": rid,
            "ok": False,
            "error": {"code": code, "message": message},
        }
    )


def log(msg: str) -> None:
    """Write a human-readable diagnostic line to stderr only (never stdout)."""
    print(f"[supertone_engine] {msg}", file=sys.stderr, flush=True)


def clamp(value, lo, hi):
    """Clamp ``value`` into the inclusive range ``[lo, hi]``.

    Values below ``lo`` are raised to ``lo`` and values above ``hi`` are
    lowered to ``hi``; values already inside the range are returned unchanged.
    Used to bound ``total_steps`` to ``[5, 12]`` and ``speed`` to ``[0.7, 2.0]``
    before synthesis (reqs 8.4, 8.5).
    """
    return max(lo, min(value, hi))


# --------------------------------------------------------------------------- #
# Model load & voice-style cache
#
# The Supertone model is loaded exactly once into the module global ``_tts``
# and kept resident for the lifetime of the subprocess (no cold start per
# utterance). ``supertonic`` is imported lazily inside load_model() so this
# module still imports cleanly in environments where ``supertonic`` is not
# installed (e.g. the Python 3.11 bridge venv used for unit tests).
# --------------------------------------------------------------------------- #


def load_model() -> None:
    """Load the Supertone model once and warm the default voice style.

    Imports ``supertonic`` lazily so the module imports cleanly without the
    dependency present. Constructs ``supertonic.TTS(auto_download=True)`` a
    single time into the module global ``_tts`` (the model is cached on disk
    after the first download). The configured default voice is then pre-cached
    via ``get_voice_style()`` so the first real synthesis is not slower than
    subsequent ones. Any failure propagates to the caller (``main()``), which
    reports it as a ``MODEL_LOAD_FAILED`` ready banner.
    """
    global _tts
    import supertonic  # lazy import: keeps module importable without the dep

    log("loading supertonic.TTS (auto_download=True)...")
    _tts = supertonic.TTS(auto_download=True)  # cached on disk after first run
    # Warm the default voice so the first synth isn't slower than the rest.
    get_voice_style(DEFAULT_VOICE)
    log(f"model loaded; default voice {DEFAULT_VOICE!r} warmed")


def get_voice_style(voice_name: str):
    """Return the voice-style object for ``voice_name``, caching per voice.

    On first request for a given voice the style is created via
    ``_tts.get_voice_style(voice_name=voice_name)`` and stored in
    ``_voice_cache``; subsequent requests for the same voice reuse the cached
    object without creating a new one.
    """
    if voice_name not in _voice_cache:
        _voice_cache[voice_name] = _tts.get_voice_style(voice_name=voice_name)
    return _voice_cache[voice_name]


# --------------------------------------------------------------------------- #
# Serve loop & control-message handling
#
# The serve loop is the heart of the subprocess: it reads NDJSON requests from
# stdin one line at a time and dispatches each to the right handler. The loop
# never terminates on a single bad request -- malformed JSON, version
# mismatches, type mismatches, and unknown types all produce an error response
# and keep the loop running (req 8.10, 10.x). Only an explicit "shutdown"
# request or stdin EOF ends the loop, after which best-effort temp cleanup runs
# and the process exits zero.
# --------------------------------------------------------------------------- #


def serve_loop() -> int:
    """Read and dispatch NDJSON requests from stdin until shutdown or EOF.

    Iterates stdin line-by-line. Whitespace-only/empty lines are skipped
    silently (req 5.6). A line that does not parse as JSON yields a
    ``BAD_REQUEST`` error with ``id=None`` and the loop continues (req 5.5).
    The request id and type are extracted (type defaulting to ``"synthesize"``).
    A ``v`` field that does not match :data:`PROTOCOL_VERSION` yields
    ``UNSUPPORTED_VERSION`` (req 6.5). ``shutdown`` breaks the loop (req 10.1);
    ``ping`` replies with a ``pong`` echoing the id (req 8.9); any other
    non-``synthesize`` type yields ``UNKNOWN_TYPE`` (req 8.8). Recognized
    ``synthesize`` requests are delegated to :func:`handle_synthesize`.

    A ``TypeError``/``ValueError`` raised while extracting fields (e.g. a field
    whose value type does not match what is expected) is mapped to
    ``BAD_REQUEST`` and the loop continues (req 18.4). On loop exit, best-effort
    temp cleanup runs (task 3.5) and 0 is returned.
    """
    for raw in sys.stdin:  # iterates line-by-line; EOF ends the loop (req 10.2)
        line = raw.strip()
        if not line:
            continue  # whitespace-only/empty line: skip without responding

        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            # Malformed protocol line: respond and keep serving (req 5.5, 18.5).
            emit_error(None, "BAD_REQUEST", f"json: {exc}")
            continue

        try:
            # A non-object payload (e.g. a bare JSON list/number/string) cannot
            # carry the expected fields -> treat as a malformed request.
            if not isinstance(req, dict):
                emit_error(None, "BAD_REQUEST", "expected a JSON object")
                continue

            rid = req.get("id")
            rtype = req.get("type", "synthesize")

            if req.get("v") != PROTOCOL_VERSION:
                emit_error(rid, "UNSUPPORTED_VERSION", f"want {PROTOCOL_VERSION}")
                continue
            if rtype == "shutdown":
                break
            if rtype == "ping":
                emit({"v": PROTOCOL_VERSION, "id": rid, "ok": True, "type": "pong"})
                continue
            if rtype != "synthesize":
                emit_error(rid, "UNKNOWN_TYPE", str(rtype))
                continue

            handle_synthesize(req, rid)
        except (TypeError, ValueError) as exc:
            # Field value with an unexpected type slipped through to a handler
            # (req 18.4): report BAD_REQUEST and keep the loop alive.
            emit_error(req.get("id") if isinstance(req, dict) else None,
                       "BAD_REQUEST", f"bad field: {exc}")
            continue

    cleanup_all_temp()  # best-effort on graceful shutdown (task 3.5)
    return 0


def handle_synthesize(req: dict, rid) -> None:
    """Validate, preprocess, synthesize, and save a synthesize request.

    Steps (reqs 8.1-8.7, 11.1, 11.2):

    1. Validate ``text``: it must be a ``str`` with at least one non-whitespace
       character and at most 5000 characters, else respond ``BAD_REQUEST`` and
       return without producing a WAV (req 8.3).
    2. Read the remaining fields with their defaults: ``voice``
       (:data:`DEFAULT_VOICE`), ``speed`` (1.0), ``lang`` ("id"),
       ``total_steps`` (8), ``preprocess_numbers`` (True).
    3. When ``preprocess_numbers`` is true, run the tag-preserving Indonesian
       number preprocessor over the text; otherwise use the text unchanged
       (reqs 11.1, 11.2).
    4. Synthesize with ``total_steps`` clamped to ``[5, 12]`` and ``speed``
       clamped to ``[0.7, 2.0]`` (reqs 8.4, 8.5). Any synthesis error maps to
       ``SYNTH_FAILED`` (req 8.6).
    5. Save a 48kHz mono WAV named ``temp_{id}_{epoch_ms}.wav`` and prune old
       temp files. Any save error maps to ``SAVE_FAILED`` (req 8.7).
    6. On success, respond with ``ok=True``, the absolute ``wav_path`` and the
       synthesized ``duration`` (reqs 8.1, 8.2).
    """
    # 1) Validate text: non-empty (non-whitespace) str, <= 5000 chars (req 8.3).
    text = req.get("text")
    if not isinstance(text, str) or not text.strip():
        emit_error(rid, "BAD_REQUEST", "empty text")
        return
    if len(text) > 5000:
        emit_error(rid, "BAD_REQUEST", "text exceeds 5000 characters")
        return

    # 2) Read fields with defaults.
    voice = req.get("voice", DEFAULT_VOICE)
    speed = float(req.get("speed", 1.0))
    lang = req.get("lang", "id")
    steps = int(req.get("total_steps", 8))
    do_pre = bool(req.get("preprocess_numbers", True))

    # 3) Tag-preserving number preprocessing (reqs 11.1, 11.2).
    spoken = preprocess_preserving_tags(text) if do_pre else text

    # 4) Synthesize (CPU-bound). Clamp total_steps/speed before use (reqs 8.4, 8.5).
    try:
        style = get_voice_style(voice)
        wav, dur = _tts.synthesize(
            spoken,
            voice_style=style,
            lang=lang,
            total_steps=clamp(steps, 5, 12),
            speed=clamp(speed, 0.7, 2.0),
        )
    except Exception as exc:  # synthesis failure -> SYNTH_FAILED (req 8.6)
        emit_error(rid, "SYNTH_FAILED", str(exc))
        return

    # 5) Save a 48kHz mono WAV; name is unique per request (req 16.1).
    try:
        wav_path = TEMP_DIR / f"temp_{rid}_{int(time.time() * 1000)}.wav"
        _tts.save_audio(wav, str(wav_path))
        prune_temp_files(max_keep=3)  # cap temp files (task 3.5 owns the logic)
    except Exception as exc:  # disk/save failure -> SAVE_FAILED (req 8.7)
        emit_error(rid, "SAVE_FAILED", str(exc))
        return

    # 6) Success response (reqs 8.1, 8.2).
    emit(
        {
            "v": PROTOCOL_VERSION,
            "id": rid,
            "ok": True,
            "wav_path": str(wav_path),
            "duration": float(dur[0]),
            "engine": "supertonic-3",
        }
    )


def prune_temp_files(max_keep: int = 3) -> None:
    """Cap the temp WAV working set at ``max_keep`` files, oldest removed first.

    Lists ``temp_*.wav`` in :data:`TEMP_DIR`, orders them by modification time
    (which tracks the embedded ``epoch_ms`` timestamp in the filename), and
    unlinks the oldest until at most ``max_keep`` files remain (reqs 16.2).
    Any failure to list, stat, or unlink a file is logged via :func:`log` as a
    warning and skipped; this function never raises (req 16.3). The bridge also
    deletes each WAV right after playback, so this subprocess-side cap is a
    safety net against orphaned files if the bridge dies mid-playback.
    """
    try:
        wavs = list(TEMP_DIR.glob("temp_*.wav"))
    except Exception as exc:  # listing failure: warn and continue (req 16.3)
        log(f"prune: could not list temp dir {TEMP_DIR}: {exc}")
        return None

    if len(wavs) <= max_keep:
        return None

    # Pair each file with its mtime (ascending == oldest first). Files that
    # cannot be stat'd (e.g. removed concurrently) are skipped with a warning.
    dated: list[tuple[float, Path]] = []
    for path in wavs:
        try:
            dated.append((path.stat().st_mtime, path))
        except Exception as exc:  # stat failure: warn and skip (req 16.3)
            log(f"prune: could not stat {path.name}: {exc}")

    if len(dated) <= max_keep:
        return None

    dated.sort(key=lambda item: item[0])  # oldest (smallest mtime) first
    for _mtime, path in dated[: len(dated) - max_keep]:
        try:
            path.unlink()
        except Exception as exc:  # unlink failure: warn and continue (req 16.3)
            log(f"prune: failed to unlink {path.name}: {exc}")
    return None


def cleanup_all_temp() -> None:
    """Best-effort remove the entire temp WAV working set on graceful shutdown.

    Called from :func:`serve_loop` when the loop exits (explicit ``shutdown``
    request or stdin EOF). Removes every ``temp_*.wav`` in :data:`TEMP_DIR`,
    logging a warning and continuing on any per-file failure. This function
    never raises so shutdown always proceeds to a clean, zero-status exit
    (reqs 10.3, 10.4).
    """
    try:
        wavs = list(TEMP_DIR.glob("temp_*.wav"))
    except Exception as exc:  # listing failure: warn and continue (req 10.4)
        log(f"cleanup: could not list temp dir {TEMP_DIR}: {exc}")
        return None

    for path in wavs:
        try:
            path.unlink()
        except Exception as exc:  # unlink failure: warn and continue (req 10.4)
            log(f"cleanup: failed to unlink {path.name}: {exc}")
    return None


# --------------------------------------------------------------------------- #
# Entry point
#
# main() loads the model and emits the readiness banner, then hands control to
# serve_loop(). Tasks 3.4/3.5 flesh out handle_synthesize() and the temp-file
# management helpers respectively.
# --------------------------------------------------------------------------- #


def main() -> int:
    """Process entry point.

    Ensures the temp working directory exists, loads the model exactly once,
    then emits the readiness banner. On model-load failure it emits a
    not-ready banner (error code ``MODEL_LOAD_FAILED``) and returns a non-zero
    exit status. The default voice is cached before the success banner is
    emitted. After the success banner, control passes to :func:`serve_loop`,
    whose exit code (0 on graceful shutdown/EOF) becomes the process exit code.
    """
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    log(f"temp dir ready at {TEMP_DIR}")

    try:
        load_model()  # blocks; may download ~260MB on first run
    except Exception as exc:
        emit(
            {
                "v": PROTOCOL_VERSION,
                "type": "ready",
                "ok": False,
                "error": {"code": "MODEL_LOAD_FAILED", "message": str(exc)},
            }
        )
        return 1

    emit(
        {
            "v": PROTOCOL_VERSION,
            "type": "ready",
            "ok": True,
            "voice_cached": list(_voice_cache.keys()),
            "engine": "supertonic-3",
        }
    )
    # Hand control to the serve loop; its return value is our exit code. On
    # loop exit serve_loop() runs cleanup_all_temp() (task 3.5) before returning.
    return serve_loop()


if __name__ == "__main__":
    sys.exit(main())
