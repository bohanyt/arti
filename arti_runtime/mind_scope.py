"""Farm+farmer scoping for ARTI's mind stores and live voice state.

All behavior is opt-in behind ``live7_mind_scope`` (default False).
- One scope key: ``scope_hash = sha256(f"{save_id}:{player_id}")``, matching
  ``ledger.jsonl``, ``discoveries.jsonl``, ``progress.jsonl``, and ``experience_digest.json``.
- Read-only sidecar ``<mind>/<game>/legacy_scopes.json`` maps legacy unscoped
  row IDs/keys to a ``scope_hash`` (never written during reads; missing/torn/
  malformed sidecar degrades to an empty map without error).
- Active-cycle gating for voice readers of ``now.json``: shown only while a
  cycle of the same scope is active, in-world, and fresh; ignored at once on
  title, ``host_left``, stop, or another scope.
- ``experience_digest.json`` is what she lived, not where she is now: the voice
  reads it for the scope of the active cycle, and after play ends for the last
  scope this voice process saw active. Another farm's digest is never read.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping

SETTING = "live7_mind_scope"
LEGACY_SCOPES_FILE = "legacy_scopes.json"
LEGACY_SCOPES_SCHEMA = "arti.legacy_scopes/1"
LEGACY_SCOPES_MAX_BYTES = 2 * 1024 * 1024

# Cycle statuses (scripts/m1_operator/live_cycle.py) where the farmhand is in an
# active live cycle (excludes ready, host_left, stopped, safety_stop,
# not_recovered, rejoining, host_wait).
ACTIVE_IN_WORLD_STATUSES = frozenset({
    "starting",
    "running",
    "active",
    "waiting_night_save",
    "checkpoint",
    "night_saved",
})

_HEX_CHARS = frozenset("0123456789abcdef")
_CACHE_LOCK = threading.Lock()
_LEGACY_CACHE: dict[str, tuple[tuple[int, int], "LegacyScopeMap"]] = {}
_JSON_CACHE: dict[str, tuple[tuple[int, int], dict | None]] = {}
_CACHE_MAX = 64

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCAL = threading.local()


def enabled(config: Mapping | None) -> bool:
    return (config or {}).get(SETTING, False) is True


def is_scope_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX_CHARS for c in value)


def scope_hash(scope: str) -> str:
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("mind_scope_required")
    return hashlib.sha256(scope.encode()).hexdigest()


def make_scope(save_id: Any, player_id: Any) -> str | None:
    """Canonical ``save_id:player_id`` string, or None when either identity is absent/unbound."""
    if save_id is None or player_id is None:
        return None
    sid = str(save_id).strip()
    pid = str(player_id).strip()
    if not sid or not pid or pid == "0":
        return None
    return f"{sid}:{pid}"


@dataclass(frozen=True)
class LegacyScopeMap:
    """Read-only mapping from legacy unscoped row IDs/keys to ``scope_hash``."""

    goals: Mapping[str, str]
    journey: Mapping[str, str]
    likes: Mapping[str, str]

    def _explicit_scope(self, row: Mapping) -> tuple[bool, str | None]:
        if "scope_hash" in row and row.get("scope_hash") is not None:
            raw = row.get("scope_hash")
            return True, raw if is_scope_hash(raw) else None
        return False, None

    def goal_scope(self, row: Mapping) -> str | None:
        if not isinstance(row, Mapping):
            return None
        has_explicit, explicit = self._explicit_scope(row)
        if has_explicit:
            return explicit
        gid = row.get("id")
        return self.goals.get(gid) if isinstance(gid, str) else None

    def journey_scope(self, row: Mapping) -> str | None:
        if not isinstance(row, Mapping):
            return None
        has_explicit, explicit = self._explicit_scope(row)
        if has_explicit:
            return explicit
        found: set[str] = set()
        for key in (
            row.get("id"),
            row.get("date"),
            str(row["day"]) if type(row.get("day")) is int else None,
        ):
            if isinstance(key, str) and key in self.journey:
                found.add(self.journey[key])
        return next(iter(found)) if len(found) == 1 else None

    def like_scope(self, row: Mapping) -> str | None:
        if not isinstance(row, Mapping):
            return None
        has_explicit, explicit = self._explicit_scope(row)
        if has_explicit:
            return explicit
        found: set[str] = set()
        for key in (
            row.get("id"),
            str(row["day"]) if type(row.get("day")) is int else None,
        ):
            if isinstance(key, str) and key in self.likes:
                found.add(self.likes[key])
        return next(iter(found)) if len(found) == 1 else None


EMPTY_LEGACY_MAP = LegacyScopeMap(goals={}, journey={}, likes={})


def _clean_section(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        k: v
        for k, v in raw.items()
        if isinstance(k, str) and k and is_scope_hash(v)
    }


def legacy_scopes_path(base: Path, game: str = "stardew") -> Path:
    import arti_state

    slug = arti_state.slug(game, 24)
    if not slug:
        raise ValueError("mind_game_slug")
    return Path(base) / slug / LEGACY_SCOPES_FILE


def load_legacy_scopes(base: Path, game: str = "stardew") -> LegacyScopeMap:
    """Read ``<base>/<game>/legacy_scopes.json``. Never writes; missing/malformed -> empty map."""
    try:
        path = legacy_scopes_path(base, game)
        st = os.stat(path)
    except (OSError, ValueError):
        return EMPTY_LEGACY_MAP
    cache_key = os.path.normcase(os.path.abspath(str(path)))
    stamp = (st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        cached = _LEGACY_CACHE.get(cache_key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    parsed = EMPTY_LEGACY_MAP
    try:
        if st.st_size <= LEGACY_SCOPES_MAX_BYTES:
            from arti_runtime import ipc

            data = json.loads(ipc.read_text(path))
            if isinstance(data, dict) and data.get("schema") == LEGACY_SCOPES_SCHEMA:
                parsed = LegacyScopeMap(
                    goals=_clean_section(data.get("goals")),
                    journey=_clean_section(data.get("journey")),
                    likes=_clean_section(data.get("likes")),
                )
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        parsed = EMPTY_LEGACY_MAP
    with _CACHE_LOCK:
        if len(_LEGACY_CACHE) >= _CACHE_MAX:
            _LEGACY_CACHE.clear()
        _LEGACY_CACHE[cache_key] = (stamp, parsed)
    return parsed


def row_matches_goal_scope(
    row: Mapping,
    target_hash: str | None,
    legacy_map: LegacyScopeMap | None = None,
) -> bool:
    """True when ``row`` belongs to ``target_hash`` (or unconditionally when ``target_hash`` is None)."""
    if target_hash is None:
        return True
    if legacy_map is not None:
        return legacy_map.goal_scope(row) == target_hash
    return row.get("scope_hash") == target_hash


def _thread_lock_for(key: str) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


def _os_lock(fd: int, timeout_sec: float) -> None:
    deadline = time.monotonic() + max(0.1, float(timeout_sec))
    delay = 0.005
    if os.name == "nt":
        import msvcrt

        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("mind_file_lock_timeout")
                time.sleep(delay)
                delay = min(0.05, delay * 1.5)
    else:
        import fcntl

        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("mind_file_lock_timeout")
                time.sleep(delay)
                delay = min(0.05, delay * 1.5)


def _os_unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(path: Path, *, timeout_sec: float = 10.0):
    """Re-entrant per-thread + cross-process lock for read-modify-write mind files."""
    target = Path(path)
    lock_path = target.parent / ("." + target.name + ".lock")
    key = os.path.normcase(os.path.abspath(str(lock_path)))
    tlock = _thread_lock_for(key)
    depths = getattr(_THREAD_LOCAL, "depths", None)
    if depths is None:
        depths = _THREAD_LOCAL.depths = {}
    with tlock:
        depth = depths.get(key, 0)
        if depth > 0:
            depths[key] = depth + 1
            try:
                yield
            finally:
                depths[key] = depth
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
        fd = os.open(str(lock_path), flags, 0o666)
        locked = False
        try:
            if os.fstat(fd).st_size == 0:
                with contextlib.suppress(OSError):
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, b"\x00")
            _os_lock(fd, timeout_sec)
            locked = True
            depths[key] = 1
            yield
        finally:
            depths.pop(key, None)
            if locked:
                _os_unlock(fd)
            with contextlib.suppress(OSError):
                os.close(fd)


# --------------------------------------------------------------------------- active-cycle scope (voice readers)


def _read_json_cached(path: Path) -> dict | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    cache_key = os.path.normcase(os.path.abspath(str(path)))
    stamp = (st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        cached = _JSON_CACHE.get(cache_key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    value = None
    try:
        if st.st_size <= 512 * 1024:
            from arti_runtime import ipc

            loaded = json.loads(ipc.read_text(path))
            if isinstance(loaded, dict):
                value = loaded
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        value = None
    with _CACHE_LOCK:
        if len(_JSON_CACHE) >= _CACHE_MAX:
            _JSON_CACHE.clear()
        _JSON_CACHE[cache_key] = (stamp, value)
    return value


def cycle_scope_from_state(state: Mapping | None) -> str | None:
    if not isinstance(state, Mapping):
        return None
    save_id = state.get("save_id")
    if save_id is None and isinstance(state.get("save_identity"), Mapping):
        save_id = state["save_identity"].get("save_id")
    player_id = state.get("player_id")
    if player_id is None and isinstance(state.get("stop_scope"), Mapping):
        player_id = state["stop_scope"].get("player_id")
    raw = make_scope(save_id, player_id)
    return scope_hash(raw) if raw else None


def _cycle_stop_present(cycle_dir: Path, run_dir: Path | None = None, state: Mapping | None = None) -> bool:
    candidates = [
        cycle_dir / "LIVE7_STOP",
        cycle_dir / "WATCH_STOP",
        cycle_dir / "OPERATOR_STOP",
        cycle_dir.parent.parent / "LIVE7_STOP",
    ]
    if run_dir is not None:
        candidates.append(Path(run_dir) / "LIVE7_STOP")
    if isinstance(state, Mapping):
        stop_file = state.get("live7_stop_file")
        if isinstance(stop_file, str) and stop_file.strip():
            candidates.append(Path(stop_file))
    for p in candidates:
        try:
            if p.exists():
                return True
        except OSError:
            return True
    return False


def _session_on_title(state: Mapping, *, command_dir: Any = None) -> bool:
    """True when the cycle's native ``session.json`` explicitly reports title/unbound/other farmer."""
    cmd_dir = state.get("command_dir") or command_dir
    if not isinstance(cmd_dir, str) or not cmd_dir.strip():
        return False
    session_path = Path(cmd_dir) / "session.json"
    if not session_path.is_file():
        return False
    sess = _read_json_cached(session_path)
    if not isinstance(sess, dict):
        return False
    if sess.get("world_ready") is False:
        return True
    if "player_id" in sess:
        pid = sess.get("player_id")
        if pid is None or str(pid).strip() in ("", "0"):
            return True
        expected = state.get("player_id")
        if expected is not None and str(pid) != str(expected):
            return True
    return False


def _scope_for_cycle_dir(
    cycle_dir: Path,
    *,
    run_dir: Path | None = None,
    now: float | None = None,
    command_dir: Any = None,
) -> str | None:
    import arti_stardew_driver_guard as guard

    state_path = cycle_dir / "cycle_state.json"
    try:
        st = os.stat(state_path)
    except OSError:
        return None
    ts_now = time.time() if now is None else float(now)
    if ts_now - st.st_mtime >= guard._MAX_STATE_AGE_SEC:
        return None
    state = _read_json_cached(state_path)
    if not isinstance(state, dict):
        return None
    if _cycle_stop_present(cycle_dir, run_dir=run_dir, state=state):
        return None
    status = str(state.get("status") or "").strip().lower()
    between_days = (status == "ready" and bool(state.get("days"))
                    and guard._lock_held(cycle_dir / "supervisor.lock", ts_now))
    if status not in ACTIVE_IN_WORLD_STATUSES and not between_days:
        return None
    if _session_on_title(state, command_dir=command_dir):
        return None
    return cycle_scope_from_state(state)


def active_cycle_scope(
    config: Mapping | None,
    *,
    now: float | None = None,
    runtime: Any = None,
    cycle_dir: Path | None = None,
) -> str | None:
    """Current active in-world cycle's ``scope_hash``, or None on title, ``host_left``, stop, or idle."""
    try:
        cfg = config or {}
        cmd_dir = cfg.get("stardew_command_dir")
        if runtime is not None:
            if getattr(runtime, "_stopped", False) is True:
                return None
            if getattr(runtime, "status", None) in ("STOPPED", "FAILED", "TITLE"):
                return None
            binding_view = getattr(runtime, "_binding_view", None)
            if callable(binding_view):
                try:
                    binding, _pid = binding_view()
                    if getattr(binding, "status", None) not in (None, "bound"):
                        return None
                except (AttributeError, TypeError, ValueError):
                    pass
            raw = getattr(runtime, "_live7_discovery_scope", None)
            return scope_hash(raw) if isinstance(raw, str) and raw.strip() else None
        if cycle_dir is not None:
            return _scope_for_cycle_dir(Path(cycle_dir), now=now, command_dir=cmd_dir)
        import arti_stardew_driver_guard as guard

        cycles = guard.active_live7_cycles(dict(cfg), now=now)
        if not cycles:
            return None
        chosen = cycles[0]
        return _scope_for_cycle_dir(chosen.cycle_dir, run_dir=chosen.run_dir, now=now, command_dir=cmd_dir)
    except Exception:  # noqa: BLE001 - fail closed: unknown scope suppresses stale live memory
        return None


def bridge_active_scope(bridge: Any, *, now: float | None = None) -> str | None:
    """Active in-world scope for the voice bridge, or None on stop, ``host_left``, title, or idle."""
    try:
        config = getattr(bridge, "CONFIG", None) or {}
        runtime = getattr(bridge, "_stardew_runtime", None)
        if runtime is not None:
            return active_cycle_scope(config, now=now, runtime=runtime)
        import arti_stardew_driver_guard as guard

        cycles = guard.active_live7_cycles(dict(config), now=now)
        if not cycles:
            return None
        chosen = cycles[0]
        relay = getattr(bridge, "_live7_help_relay", None)
        stopped_fn = getattr(bridge, "_live7_game_session_stopped", None)
        if callable(stopped_fn) and stopped_fn():
            resolver_cycle = getattr(getattr(relay, "_channel_resolver", None), "cycle_dir", None)
            if resolver_cycle is None or os.path.normcase(os.path.abspath(str(resolver_cycle))) == os.path.normcase(
                os.path.abspath(str(chosen.cycle_dir))
            ):
                return None
        return _scope_for_cycle_dir(
            chosen.cycle_dir,
            run_dir=chosen.run_dir,
            now=now,
            command_dir=config.get("stardew_command_dir"),
        )
    except Exception:  # noqa: BLE001
        return None


def bridge_memory_scope(bridge: Any, *, now: float | None = None) -> tuple[str | None, bool]:
    """(scope whose lived memory, the digest, the voice may read; whether play is active in it now).

    The active cycle's scope; after play ends (stop, ``host_left``, title) the last scope this voice process saw
    active, so she still remembers her day while she talks about it. None before the first active cycle."""
    active = bridge_active_scope(bridge, now=now)
    if active:
        with contextlib.suppress(AttributeError, TypeError):
            setattr(bridge, "_live7_memory_scope", active)
        return active, True
    last = getattr(bridge, "_live7_memory_scope", None)
    return (last if is_scope_hash(last) else None), False


def read_now_for_config(
    config: Mapping | None,
    *,
    now: float | None = None,
    max_age_sec: float | None = None,
    runtime: Any = None,
    cycle_dir: Path | None = None,
) -> dict | None:
    import arti_mind

    cfg = config or {}
    age = arti_mind.NOW_MAX_AGE_SEC if max_age_sec is None else float(max_age_sec)
    if not enabled(cfg):
        return arti_mind.read_now(arti_mind.mind_dir(cfg), now=now, max_age_sec=age)
    active = active_cycle_scope(cfg, now=now, runtime=runtime, cycle_dir=cycle_dir)
    if not active:
        return None
    return arti_mind.read_now(
        arti_mind.mind_dir(cfg),
        now=now,
        max_age_sec=age,
        scope_hash=active,
        require_scope=True,
    )


def bridge_read_now(bridge: Any, *, now: float | None = None, max_age_sec: float | None = None) -> dict | None:
    import arti_mind

    cfg = getattr(bridge, "CONFIG", None) or {}
    age = float(cfg.get("arti_mind_now_max_age_sec", arti_mind.NOW_MAX_AGE_SEC)) if max_age_sec is None else float(max_age_sec)
    active = bridge_active_scope(bridge, now=now)
    if not active:
        return None
    return arti_mind.read_now(
        arti_mind.mind_dir(cfg),
        now=now,
        max_age_sec=age,
        scope_hash=active,
        require_scope=True,
    )


def bridge_intent_block(bridge: Any, *, now: float | None = None, max_age_sec: float | None = None) -> str:
    import arti_mind

    cfg = getattr(bridge, "CONFIG", None) or {}
    age = float(cfg.get("arti_mind_now_max_age_sec", arti_mind.NOW_MAX_AGE_SEC)) if max_age_sec is None else float(max_age_sec)
    active = bridge_active_scope(bridge, now=now)
    if not active:
        return ""
    return arti_mind.intent_block(
        arti_mind.mind_dir(cfg),
        now=now,
        max_age_sec=age,
        scope_hash=active,
        require_scope=True,
    )


def clear_cycle_live_state(base: Path, *, scope: str | None = None, reason: str = "cycle_end") -> dict[str, bool]:
    """Clear ``now.json`` (where she is and what she does) at the end of a cycle. The digest stays: she lived it."""
    import arti_mind

    return {"now": arti_mind.clear_now(base, scope=scope, reason=reason)}
