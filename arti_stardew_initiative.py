"""Stardew initiative material: a tiny fact set from ARTI's own Live7 journal.

Plan: an internal design note (v0).

While Stardew is live, the bridge's quiet-time initiative turn may get a
``stardew_note``: one or two short Indonesian facts about what ARTI herself has
been doing in the game. Sources, all read on a daemon thread only:

- the active Live7 cycle (``arti_stardew_driver_guard.active_live7_cycles``);
- the TAIL (last 64 KB) of its newest attempt's ``journal/g1_journal.jsonl``;
- ``part_live_row`` rows with ``verified: true`` only (UNCERTAIN/CLEAN_FAIL rows
  never become "aku udah..." facts), plus ``live7_moment_emitted`` rows so a kind
  the moments channel just voiced is not repeated.

The bridge main loop only calls ``pick_note()``, which reads the in-memory
snapshot: no file I/O, provider calls or embeddings on the main loop or the
voice path. A missing/corrupt journal or a stale snapshot means no note, never
a guess. The note never carries tile coordinates, raw skill ids or item ids, and
never turns into goals or to-dos (agency-first, #329).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import NamedTuple

TAIL_BYTES = 64 * 1024
MAX_FACTS = 5
MAX_NOTE_CHARS = 400      # whole snapshot fact set
FRESH_SEC = 120.0         # older snapshot -> no note
REFRESH_SEC = 30.0
RING_SIZE = 6             # last facts used: never repeated while in the ring
MOMENT_SKIP_SEC = 600.0   # kind voiced by the moments channel in the last 10 min
NOTE_FACTS = 2            # facts per note (1-2)
DEFAULT_GAME_RATIO = 0.5
RECENT_SEC = 600.0        # journal untouched longer -> no "barusan"/date/place
# "Barusan" = the last verified action is at most this many GAME minutes before
# the newest native stamp (10 game min ~ 7 s real: 180 ~ 2 real minutes).
BARUSAN_GAME_MIN = 180
ENABLE_KEY = "stardew_initiative_material_enabled"

# Skill id -> (fact kind, Indonesian verb phrase). Kinds that the moments
# channel also produces use ITS kind names (arti_stardew_moments.SITUATIONS),
# so a recently voiced moment suppresses the same fact here. Navigation/meta
# skills (pergi_ke, jalan_ke, cek_pagi, tutup_menu, pegang, siapkan_pancing)
# and tidur are not "things she did" facts and are absent on purpose.
SKILL_VERBS: dict[str, tuple[str, str]] = {
    "cangkul": ("tilled", "nyangkul tanah"),
    "tanam": ("planted", "nanam bibit"),
    "siram": ("watered_all", "nyiram tanaman"),
    "panen": ("harvested", "manen"),
    "tangkap_ikan": ("fished", "mancing dan dapet ikan"),
    "tebang": ("chopped", "nebang pohon"),
    "pecah_batu": ("mined", "mecahin batu"),
    "isi_gembor": ("refill", "ngisi gembor"),
    "beli": ("bought_seeds", "belanja"),
    "jual": ("sold", "jualan"),
    "kirim": ("shipped", "ngirim barang ke kotak jual"),
    "makan": ("ate", "makan"),
    "forage": ("forage", "mungut hasil alam"),
    "pungut_drop": ("pickup", "mungutin barang yang jatuh"),
    "simpan_ke_chest": ("stored", "nyimpen barang ke peti"),
    "elus": ("pet", "ngelus hewan"),
    "rawat_hewan": ("animals", "ngurus hewan ternak"),
    "beli_hewan": ("bought_animal", "beli hewan"),
    "naik_kuda": ("horse", "naik kuda"),
    "ngobrol_hadiah": ("gift", "ngobrol sama warga sambil ngasih hadiah"),
    "surat_quest": ("quest", "nganterin titipan warga"),
    "masak": ("cooked", "masak"),
    "buat": ("crafted", "bikin barang"),
    "pasang": ("placed", "masang barang"),
    "bersihkan": ("cleared", "bersihin lahan"),
    "donasi": ("donated", "nyumbang ke museum"),
    "crab_pot": ("crab_pot", "ngurus bubu kepiting"),
    "mesin": ("machine", "ngurus mesin"),
    "upgrade_alat": ("upgrade", "upgrade alat"),
}

_LOCATIONS = {
    "Farm": "di kebun", "FarmHouse": "di rumah utama HOST", "Cabin": "di rumahmu", "Town": "di kota",
    "Beach": "di pantai", "Forest": "di hutan", "Mountain": "di gunung",
    "BusStop": "di halte bus", "SeedShop": "di toko Pierre", "Saloon": "di Saloon",
    "Mine": "di depan tambang", "Backwoods": "di hutan belakang",
    "Railroad": "di dekat rel kereta", "Woods": "di hutan rahasia",
    "ScienceHouse": "di rumah Robin", "AnimalShop": "di peternakan Marnie",
    "Blacksmith": "di pandai besi", "ArchaeologyHouse": "di museum",
    "Hospital": "di klinik", "JojaMart": "di JojaMart", "FishShop": "di toko ikan Willy",
    "Greenhouse": "di rumah kaca", "Sewer": "di gorong-gorong", "Desert": "di gurun",
    "Coop": "di kandang ayam", "Barn": "di kandang", "Cellar": "di gudang bawah tanah",
}
_SEASONS = {"spring": "musim semi", "summer": "musim panas",
            "fall": "musim gugur", "winter": "musim dingin"}
_WEEKDAYS = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
# Last-line guard: a fact with a coordinate pair or a snake_case id never ships.
_RAW = re.compile(r"\d+\s*,\s*\d+|\b[a-z]+_[a-z_]+\b|\(O\)|\bt[fs]:")


class Fact(NamedTuple):
    kind: str
    text: str
    recent: bool = False  # "barusan"/"sekarang" fact: dropped once the journal is stale
    stale_text: str = ""  # what a stale recent fact still honestly says ("" = drop)


class Snapshot(NamedTuple):
    built_at: float
    facts: tuple = ()
    moments: tuple = ()  # ((kind, wall ts), ...) from live7_moment_emitted rows
    stamp_ts: float | None = None  # wall clock of the newest STAMPED row (None = unknown = stale)
    game_first_facts: tuple = ()  # separate opt-in projection; classic facts and golden bytes stay intact


# --- pure reading ---------------------------------------------------------

def read_tail(path, limit: int = TAIL_BYTES) -> tuple[list[dict], bool]:
    """(rows, truncated): JSON rows from the last ``limit`` bytes of ``path``.

    Reads at most ``limit`` bytes. A partial first line (tail started mid-line)
    and corrupt/partial lines are skipped. A missing file -> ([], False).
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - int(limit))
            handle.seek(start)
            data = handle.read(int(limit))
    except OSError:
        return [], False
    truncated = start > 0
    lines = data.split(b"\n")
    if truncated and lines:
        lines = lines[1:]  # first line is cut mid-way
    rows = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows, truncated


def _game(row: dict) -> dict:
    game = row.get("game")
    return game if isinstance(game, dict) else {}


def _date(row: dict):
    date = _game(row).get("date")
    if (isinstance(date, list) and len(date) == 3 and type(date[0]) is int
            and date[1] in _SEASONS and type(date[2]) is int and 1 <= date[2] <= 28):
        return tuple(date)
    return None


def _minutes(clock):
    """Stardew clock (e.g. 1430) -> minutes since midnight; None if unknown."""
    return (clock // 100) * 60 + clock % 100 if type(clock) is int and clock >= 0 else None


def _date_text(date, clock) -> str:
    year, season, day = date
    tahun = f" tahun ke-{year}" if year > 1 else ""
    text = (f"Di game sekarang {_SEASONS[season]}{tahun} hari ke-{day}, "
            f"hari {_WEEKDAYS[(day - 1) % 7]}")
    if type(clock) is int and 600 <= clock <= 2600:
        part = ("pagi" if clock < 1100 else "siang" if clock < 1500
                else "sore" if clock < 1800 else "malam")
        text += f", udah {part}"
    return text + "."


def _verified(row: dict) -> bool:
    return (row.get("ev") == "part_live_row" and row.get("verified") is True
            and row.get("outcome_class") == "VERIFIED")


def facts_from_rows(rows: list[dict], *, truncated: bool = False) -> tuple[Fact, ...]:
    """At most MAX_FACTS facts / MAX_NOTE_CHARS chars.

    "Now" (date, clock, place) is the NEWEST row carrying a native game stamp
    (part_*_row, skill_*, location_changed, day_start_detected ...), not the
    last verified row, so a day rollover is never reported with yesterday's
    date. Only VERIFIED rows dated today count; "barusan" needs the last one
    within BARUSAN_GAME_MIN game minutes of that stamp.
    """
    stamped = [r for r in rows if _date(r) is not None]
    if not stamped:
        return ()
    now_game = _game(stamped[-1])
    today = _date(stamped[-1])
    verified = [r for r in rows if _verified(r) and _date(r) == today]

    counts: dict[str, int] = {}
    order: list[str] = []  # skills by most recent first
    newest: dict[str, dict] = {}
    for row in reversed(verified):
        skill = row.get("skill")
        if skill in SKILL_VERBS:
            counts[skill] = counts.get(skill, 0) + 1
            if skill not in order:
                order.append(skill)
                newest[skill] = row

    def count_text(skill):
        verb, n = SKILL_VERBS[skill][1], counts[skill]
        if n <= 1:
            return f"Hari ini kamu sempat {verb}."
        return f"Hari ini kamu udah {verb} {n if not truncated else 'beberapa'} kali."

    facts: list[Fact] = []
    if order:
        skill = order[0]
        kind, verb = SKILL_VERBS[skill]
        then, now_min = _minutes(_game(newest[skill]).get("time")), _minutes(now_game.get("time"))
        if then is not None and now_min is not None and 0 <= now_min - then <= BARUSAN_GAME_MIN:
            n = counts[skill]
            extra = "" if n <= 1 else f"; hari ini udah {n if not truncated else 'beberapa'} kali"
            facts.append(Fact(kind, f"Barusan kamu berhasil {verb}{extra}.", True, count_text(skill)))
        else:
            facts.append(Fact(kind, count_text(skill)))
    facts.append(Fact("date", _date_text(today, now_game.get("time")), True))
    location = str(now_game.get("location") or "")
    place = _LOCATIONS.get(location)
    if place is None and location.startswith("UndergroundMine"):
        place = "di dalam tambang"
    if place:
        kind = "went_to_town" if place == "di kota" else "location"
        facts.append(Fact(kind, f"Terakhir kamu ada {place}.", True))
    for skill in order[1:]:
        facts.append(Fact(SKILL_VERBS[skill][0], count_text(skill)))
    out: list[Fact] = []
    total = 0
    for fact in facts:
        if len(out) >= MAX_FACTS or _RAW.search(fact.text):
            continue
        if total + len(fact.text) > MAX_NOTE_CHARS:
            continue
        if _RAW.search(fact.stale_text):
            fact = fact._replace(stale_text="")
        out.append(fact)
        total += len(fact.text)
    return tuple(out)


def moments_from_rows(rows: list[dict]) -> tuple:
    seen: dict[str, float] = {}
    for row in rows:
        if row.get("ev") != "live7_moment_emitted":
            continue
        kind, ts = row.get("kind"), row.get("ts")
        if isinstance(kind, str) and type(ts) in (int, float):
            seen[kind] = max(float(ts), seen.get(kind, 0.0))
    return tuple(seen.items())


def find_journal(config: dict, now: float | None = None) -> Path | None:
    """The newest attempt journal of the newest active Live7 cycle (thread only)."""
    import arti_stardew_driver_guard as guard

    cycles = guard.active_live7_cycles(config, now)
    if not cycles:
        return None
    try:
        # day-NNNN-attempt-NNN folders are zero-padded: name order = time order.
        attempts = sorted(p for p in Path(cycles[0].cycle_dir).glob("day-*-attempt-*") if p.is_dir())
        if not attempts:
            return None
        # Never fall back to an older attempt: a fresh attempt without its
        # journal yet means "nothing known now", not yesterday's facts.
        journal = attempts[-1] / "journal" / "g1_journal.jsonl"
        return journal if journal.is_file() else None
    except OSError:
        return None


def _wall_ts(row: dict) -> float | None:
    """A row's own wall-clock time (``ts`` or ``at``, unix seconds), else None."""
    for key in ("ts", "at"):
        value = row.get(key)
        if type(value) in (int, float) and math.isfinite(value) and value > 0:
            return float(value)
    return None


def newest_stamped(rows: list[dict]) -> dict | None:
    """The row that defines "now" (date/clock/place) in facts_from_rows."""
    for row in reversed(rows):
        if _date(row) is not None:
            return row
    return None


class StampClock:
    """Wall-clock age of the newest stamped row, judged on the refresher thread.

    Staleness follows the newest STAMPED row, not the file's mtime: during a
    long host_wait the journal keeps growing with unstamped rows while the
    game clock stands still. A row's own ``ts``/``at`` wins. Today's runtime
    journal rows carry none, so a stamped row is timed when a scan first sees
    it (error <= one refresh period). A row already present at the FIRST scan
    has an unknown age -> None -> stale (no "barusan", date or place).
    """

    def __init__(self):
        self.key = None
        self.seen_at = None
        self.primed = False

    def observe(self, row: dict | None, now: float) -> float | None:
        primed, self.primed = self.primed, True
        if row is None:
            return None
        own = _wall_ts(row)
        if own is not None:
            return own
        key = hashlib.sha1(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        if key != self.key:
            self.key = key
            self.seen_at = now if primed else None
        return self.seen_at


def build_snapshot(journal, now: float | None = None, clock: StampClock | None = None) -> Snapshot:
    """Snapshot of ``journal``'s tail. Without a ``clock`` only a row's own ts counts."""
    now = time.time() if now is None else now
    if journal is None:
        if clock is not None:
            clock.observe(None, now)
        return Snapshot(now)
    rows, truncated = read_tail(journal)
    stamp = newest_stamped(rows)
    stamp_ts = clock.observe(stamp, now) if clock is not None else (
        _wall_ts(stamp) if stamp is not None else None)
    return Snapshot(now, facts_from_rows(rows, truncated=truncated), moments_from_rows(rows), stamp_ts)


# --- picking (main loop: memory only) -------------------------------------

class Alternator:
    """Deterministic game/other mix: ratio 0.5 -> game, other, game, ..."""

    def __init__(self):
        self.credit = None

    def take(self, ratio: float) -> bool:
        ratio = min(1.0, max(0.0, float(ratio)))
        if ratio <= 0.0:
            return False
        if self.credit is None:
            self.credit = 1.0 - ratio  # the first eligible turn is a game turn
        self.credit += ratio
        if self.credit >= 1.0 - 1e-9:
            self.credit -= 1.0
            return True
        return False


class InitiativeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot: Snapshot | None = None
        self.ring: deque = deque(maxlen=RING_SIZE)
        self.rotation = 0
        self.alternator = Alternator()
        self.stamp_clock = StampClock()  # refresher thread only
        self.verified_clock = StampClock()  # time the newest verified row, not arbitrary journal writes

    def store(self, snapshot: Snapshot) -> None:
        with self.lock:
            self.snapshot = snapshot

    def pick(self, config: dict | None = None, now: float | None = None) -> str:
        config = config or {}
        now = time.time() if now is None else now
        with self.lock:
            snap = self.snapshot
            if config.get("stardew_initiative_game_first"):
                if snap is None or now - snap.built_at > FRESH_SEC:
                    return ""
                cands = [t for t in snap.game_first_facts if t and t not in self.ring]
                if not cands:
                    return ""
                # The own-topic lane never spends an "other" slot while playing.
                self.alternator.take(1.0)
                picked = cands[:NOTE_FACTS]
                self.ring.extend(picked)
                return " ".join(picked)
            if snap is None or not snap.facts or now - snap.built_at > FRESH_SEC:
                return ""
            voiced = {k for k, ts in snap.moments if now - ts < MOMENT_SKIP_SEC}
            # Newest stamped row older than RECENT_SEC, or of unknown age (host
            # wait, pause, crash, fresh start): no "barusan"/"sekarang"; a
            # recent fact falls back to its plain text.
            stale = snap.stamp_ts is None or now - snap.stamp_ts > RECENT_SEC
            texts = [(f.stale_text if (stale and f.recent) else f.text, f.kind) for f in snap.facts]
            cands = [t for t, kind in texts if t and t not in self.ring and kind not in voiced]
            if not cands:
                return ""
            try:
                ratio = float(config.get("stardew_initiative_game_ratio", DEFAULT_GAME_RATIO))
            except (TypeError, ValueError):
                ratio = DEFAULT_GAME_RATIO
            if not self.alternator.take(ratio):
                return ""
            start = self.rotation % len(cands)
            self.rotation += 1
            picked = [cands[(start + i) % len(cands)] for i in range(min(NOTE_FACTS, len(cands)))]
            self.ring.extend(picked)
            return " ".join(picked)


_state = InitiativeState()
_refresher: threading.Thread | None = None
_refresher_stop = threading.Event()
_refresher_lock = threading.Lock()
_mic_turns = deque(maxlen=2)


def note_mic_turn(text, now=None):
    """Called only when an admitted mic trigger actually starts its model turn."""
    from arti_live_safety import public_context
    line = public_context(str(text))
    if line:
        _mic_turns.append((time.time() if now is None else now, line))


def heard_turn_note(now=None):
    now = time.time() if now is None else now
    return " ".join(line for ts, line in tuple(_mic_turns) if 0 <= now - ts <= 180)[:200]


def pick_note(config: dict | None = None, now: float | None = None) -> str:
    """The note for this initiative turn, or "". Memory only, never raises."""
    try:
        return _state.pick(config, now)
    except Exception:  # noqa: BLE001 — optional material
        return ""


def refresh(config: dict, now: float | None = None) -> Snapshot:
    """One scan + atomic snapshot store (refresher thread; tests call it directly)."""
    try:
        import arti_stardew_driver_guard as guard

        if not guard.live7_cycle_active(config):
            snap = Snapshot(time.time() if now is None else now)
        else:
            journal = find_journal(config)
            snap = build_snapshot(journal, now, _state.stamp_clock)
            if config.get("stardew_initiative_game_first"):
                snap = snap._replace(game_first_facts=game_first_facts(config, journal, now, _state.verified_clock))
    except Exception:  # noqa: BLE001 — unreadable = empty note
        snap = Snapshot(time.time() if now is None else now)
    _state.store(snap)
    return snap


def game_first_facts(config, journal, now=None, clock=None):
    """Background-only bounded reads: current page, newest verified row, THIS farm's ledger.

    Unscoped goals/journey prose are deliberately excluded: their game key alone
    cannot prove which farm they describe. Scoped discovery journey rows and
    native progress/quest records retain their original date.
    """
    import arti_mind
    from arti_runtime.event_framing import safe_fact
    from arti_runtime.fact_names import projection
    now = time.time() if now is None else now
    base = arti_mind.mind_dir(config)
    row = arti_mind.read_now(base, now=now) or {}
    if row and row.get("game") != "stardew":
        return ()
    native = row.get("native_facts") or {}
    facts = []
    stamp = native.get("play_page_stamp") or {}
    game = row.get("game_time") or {}
    if (type(stamp.get("observed_ts")) in (float, int) and 0 <= now - stamp["observed_ts"] <= 45
            and stamp.get("location") == game.get("place") and stamp.get("date") == game.get("date")):
        for key in ("di_sini", "kunjungan", "quest_lanjut", "festival_hari", "bisa_di_sini"):
            value = native.get(key)
            if key == "bisa_di_sini" and isinstance(value, str):
                value = value.split(";", 1)[0]
            if value:
                facts.append(safe_fact(key + ": " + str(value), 400))
    completed = native.get("completed_action")
    if isinstance(completed, dict) and type(completed.get("observed_ts")) in (int, float) and 0 <= now - completed["observed_ts"] <= RECENT_SEC:
        facts.append(safe_fact(json.dumps(projection(completed), ensure_ascii=False), 400))
    rows, _ = read_tail(journal) if journal is not None else ([], False)
    latest = next((r for r in reversed(rows) if _verified(r)), None)
    ts = clock.observe(latest, now) if clock is not None else (_wall_ts(latest) if latest else None)
    if latest is not None:
        if ts is not None and 0 <= now - ts <= RECENT_SEC:
            from arti_live7_status_facts import _outcome_text
            facts.append(safe_fact(_outcome_text(latest), 400))
    scope = native.get("farm_scope")
    if isinstance(scope, str) and scope:
        for memory in arti_mind.recent_discoveries(base, "stardew", scope=scope, limit=4):
            facts.append(safe_fact(str(memory.get("day_label") or memory.get("date") or "") + ": " + memory["text"], 400))
        for memory in arti_mind.ledger_rows(base, "stardew", scope=scope)[-3:]:
            text = " ".join(str(memory[k]) for k in ("status_label", "first_label", "title", "text", "status") if memory.get(k))
            if text:
                facts.append(safe_fact(text, 400))
        previous = arti_mind.progress_days(base, "stardew", scope=scope)
        if previous:
            memory = previous[-1]
            facts.append(safe_fact(json.dumps(projection({k: memory.get(k) for k in ("day_label", "start", "latest")}),
                                              ensure_ascii=False), 400))
    return tuple(dict.fromkeys(facts))


def _enabled(config: dict) -> bool:
    try:
        return bool((config or {}).get(ENABLE_KEY, False))
    except Exception:  # noqa: BLE001
        return False


def _loop(config: dict, stop: threading.Event) -> None:
    # The kill switch is re-read every tick: switched off at runtime, the
    # thread forgets its facts and exits; ensure_refresher restarts it later.
    while not stop.is_set() and _enabled(config):
        refresh(config)
        stop.wait(REFRESH_SEC)
    _state.store(Snapshot(time.time()))


def ensure_refresher(config: dict) -> None:
    """Start the daemon refresher once (no file I/O on the caller's thread)."""
    global _refresher
    with _refresher_lock:
        if (not _enabled(config) or _refresher_stop.is_set()
                or (_refresher is not None and _refresher.is_alive())):
            return
        _refresher = threading.Thread(target=_loop, args=(config, _refresher_stop),
                                      name="stardew-initiative-refresh", daemon=True)
        _refresher.start()


def stop_refresher(timeout: float = 2.0) -> None:
    """Bridge shutdown: stop for good. Never raises."""
    try:
        _refresher_stop.set()
        t = _refresher
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)
    except Exception:  # noqa: BLE001
        pass


def reset() -> None:
    """Tests: fresh state, refresher stopped and restartable."""
    global _state, _refresher
    stop_refresher()
    with _refresher_lock:
        _refresher = None
        _refresher_stop.clear()
    _state = InitiativeState()
    _mic_turns.clear()
