"""Live7 policy over the existing skill session, native offers and owner ingress.

No day script, input/brain/start/time quota, new executor, or inferred success.
Clock estimates are admission reserves, never a limit on a live session.
"""
from __future__ import annotations

from arti_runtime import wait_ledger as W
from arti_runtime.offer_cache import offer_build_scope as _offer_build_scope

from arti_runtime.page_budget import PageBudget
from arti_runtime.native_rows import native_rows

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import json
import time
from arti_stardew_pause import game_now, wait_resume
import re
from types import SimpleNamespace

import arti_stardew_skills as S
from arti_stardew_live7_help import Live7HelpMixin
from arti_stardew_moments import Live7MomentsMixin
from scripts.live7.live7_common import CropMemory, RAIN_WEATHER, RECONCILED_FROM_UNCERTAIN
from arti_runtime.chest_memory import ChestMemory
from arti_runtime.forage_memory import ForageMemory
from arti_runtime.seen_objects import SeenObjects, WALK_KIND as SEEN_WALK_KIND, stands as seen_stands
from arti_runtime.ground_items import GroundItems

# Rehearsal 1 (2026-10-20): the old "[recorded remark omitted]" read as "[recorded remark omitted]" and ARTI slept at 10:40. Mind slice 1 / owner D2 (2026-10-28): "[recorded remark omitted]" read as an order. A neutral session line plus the safety fact (the reserve is kept).
GOAL = ("Arti, main mandiri Live7: main bebas satu hari penuh, kegiatanmu pilihanmu sendiri. "
        "Fakta: jam 02:00 karakter pingsan kalau belum tidur, cadangan waktu pulang tetap dijaga.")
# No ";" here: owner ingress reads ";" as an episode-plan separator (arti_stardew_episode.parse_episode_plan).
# Live-day terminal contract shared by the trial report (stardew_m1_trial.py) and the cycle supervisor
# (scripts/m1_operator/live_cycle.py). A verified bed plus the fresh native next morning is a successful
# day even when rows failed (rows keep their own honest classes). An honest lost day (recovery loop,
# PLAN 2.1) ends the trial without a bed; the cycle may continue at the native next morning. Every
# other terminal is a refusal (safety stop) for the supervisor.
DAY_VERIFIED, DAY_LOST = "LIVE7_DAY_VERIFIED", "LIVE7_DAY_LOST"
SLEEP_VERIFIED_REASON = "live7_sleep_verified"
DAY_LOST_REASON = "live7:recovery_loop_day_lost"
PASSED_OUT_REASON = "live7:passed_out"
HOME_FAILED_REASON = "live7:home_sleep_retries_exhausted"
DAY_LOST_REASONS = (DAY_LOST_REASON, PASSED_OUT_REASON, HOME_FAILED_REASON)
MIND_GAME = "stardew"
REFLECTION_PENDING_DIR = "reflection_pending"  # fix54H: bedtime facts awaiting the next runner
REFLECTION_MAX_ATTEMPTS = 3  # fix55C / ADR 78 (REVIEW2 #6): failed deferred reflections retried by later runners


HOME_SYNONYMS = frozenset(("kabin", "kabinmu", "kabinku", "cabin", "rumah", "rumahku", "pulang"))


def day_lost_fact(reason, clock=None):
    """The honest bedtime-reflection fact for a day that ended without a verified bed sleep (fix10e)."""
    if reason == PASSED_OUT_REASON:
        t = clock if type(clock) is int and clock >= PASS_OUT_CLOCK else PASS_OUT_CLOCK
        return "pingsan jam %02d:%02d, tidak tidur di kasur" % (t // 100 % 24, t % 100)
    if reason == HOME_FAILED_REASON:
        return "gagal pulang ke kasur, hari berakhir tanpa tidur di kasur"
    return "hari berakhir karena macet berulang, tanpa tidur di kasur"
MIND_BLOCKED_AFTER_SEC = 60.0   # gameplay seconds without world progress before now.json says "blocked" (fix10c)
MIND_HEARTBEAT_SEC = 30.0       # wall seconds between unchanged controller heartbeats on now.json
_MIND_SEASONS = ("spring", "summer", "fall", "winter")


def mind_day(date):
    """The game-day ordinal arti_mind uses for TTLs (Y1 spring 1 = 1), or None for an unknown date."""
    try:
        year, season, day = date
        return (int(year) - 1) * 112 + _MIND_SEASONS.index(str(season).casefold()) * 28 + int(day)
    except (TypeError, ValueError):
        return None


def mind_label(date):
    try:
        return "Y%s %s %s" % tuple(date)
    except TypeError:
        return None


def trial_status(verdict, reason):
    """The trial report status for a finished Live7 skill session, or None when it is not a day terminal."""
    if verdict == "achieved" and reason == SLEEP_VERIFIED_REASON:
        return DAY_VERIFIED
    if verdict == "partial" and reason in DAY_LOST_REASONS:
        return DAY_LOST
    return None


def day_terminal(report):
    """'verified' | 'lost' | None from a trial report (supervisor side of the contract).

    Accepts the 87b2a0c4 report shape too (status HELD, reason live7_sleep_verified, session
    verdict achieved), so rehearsal-1 reads the same. Never upgrades anything else.
    """
    if not isinstance(report, Mapping):
        return None
    session = ((report.get("skills") or {}).get("session") or {}) if isinstance(report.get("skills"), Mapping) else {}
    status, reason = report.get("status"), report.get("reason")
    verdict = session.get("verdict")
    if reason == SLEEP_VERIFIED_REASON and (status == DAY_VERIFIED or status == "HELD" and verdict == "achieved"):
        return "verified"
    if status == DAY_LOST and reason in DAY_LOST_REASONS:
        return "lost"
    return None
# Public S1--S31 catalogue. These are choices, not scheduled rows. S6 has two
# resource skills; S3 includes the ordinary pet subcase; S9 includes tilling.
TASKS = {
    "S1": ("tidur",), "S2": ("siram", "isi_gembor"), "S3": ("rawat_hewan", "elus"),
    "S4": ("simpan_ke_chest",), "S5": ("makan",), "S6": ("tebang", "pecah_batu", "pungut_drop"),
    "S7": ("pergi_ke", "jalan_ke"), "S8": ("ikuti_festival",), "S9": ("tanam", "cangkul"),
    "S10": ("panen",), "S11": ("beli",), "S12": ("jual", "kirim"), "S13": ("buat",),
    "S14": ("tangkap_ikan",), "S15": ("lawan_mine", "lawan"), "S16": ("ngobrol_hadiah",),
    "S17": ("bersihkan",), "S18": ("pasang",), "S19": ("pohon_buah",), "S20": ("mesin",),
    "S21": ("forage",), "S22": ("gali_artefak",), "S23": ("tambang_bijih",),
    "S24": ("crab_pot",), "S25": ("upgrade_alat",), "S26": ("beli_hewan",),
    "S27": ("donasi",), "S28": ("surat_quest",), "S29": ("cek_pagi",),
    "S30": ("masak",), "S31": ("siapkan_pancing",),
}
NAMES = frozenset(n for names in TASKS.values() for n in names)
NAMES |= frozenset({"aksi", "pakai", "pegang_slot", "hadap", "inventori", "cek_jurnal", "langkah"})
VERIFIED, CLEAN_FAIL, UNCERTAIN = "VERIFIED", "CLEAN_FAIL", "UNCERTAIN"
NOT_READY, PRECONDITION, SAFETY_STOP = "NOT_READY", "PRECONDITION", "SAFETY_STOP"
NO_EFFECT = "NO_EFFECT"  # a released step whose native receipt proves no displacement or turn
INTERRUPTED = "INTERRUPTED"  # existing outcome vocabulary (status_facts); people-page pause is this, not CLEAN_FAIL
# ADR 167: return is a safety net relative to native 02:00, not a midnight schedule.
# A configured earlier target remains an explicit preference. Travel is measured;
# the separate safety margin and local bed/unknown-exit allowances remain visible.
HOME_TARGET = 2600
HOME_MARGIN = 60  # game minutes reserved after the measured home/bed ETA
HOME_RETRIES = 3  # failed approaches, never a quota on normal gameplay
PASS_OUT_CLOCK = 2600  # vanilla 02:00 pass-out; a fact for her choice, never a new bedtime rule
EMPTY_FALLBACK_REASON = "no_progress:unusable_model_choice"
FINISH_RETRIES = 3  # consecutive task finishes without choosing another action
STALE_REASKS = 1  # refresh an offer changed during model latency before counting an unusable answer
EMPTY_FALLBACK_REASKS = 3  # consecutive unusable logical choices, then help; never select another activity
READ_TODAY_CHARS = 300  # per channel/letter; the newest native text she read, as a day fact
SHOP_RETRY_MINUTES = 30  # one later model choice after a cleanly failed trip
SHOP_FAILURES_MAX = 2  # initial trip plus at most one retry, across origins on this native day
FORAGE_LOOK_INPUTS = 120  # legs of one walk to a real visible target; each leg is one decision of up to six presses
FORAGE_WALK_READS = 3  # reads in a row with no leg offered before the walk ends
SEEN_WALK_INPUTS = 160  # ADR 136: legs of one walk to a sighting, the trip's own hop budget (ADR 125)
SEED_LOOK_STANDS = 24  # taken soil-approach probes per day; merely offering one spends nothing
SEED_SURVEY_PROBES = 4  # no soil hint: at most four spaced looks instead of a day of shuffling
SEED_OWN_LOOKS = 2  # negative local looks / taken approaches beside occupied own crops before searching beyond them
# Conservative legacy bounds ONLY when the native route is unavailable (ADR 167).
HOME_MINUTES = dict(Farm=100, BusStop=129, Town=199, Forest=226, Mountain=160,
                    Backwoods=125, Beach=230, Mine=180)
from arti_runtime.mines_offer import RUN_GAME_MINUTES
WORK_MINUTES = dict(lawan=30, lawan_mine=RUN_GAME_MINUTES, tambang_bijih=180, tangkap_ikan=100, forage=90,
                    tebang=100, rawat_hewan=180, cek_pagi=60, pergi_ke=90)
SETTLE_SECONDS = 3.0  # a readback attempt, not a live wall-time cap
# Sat gift (live7 smoke 2026-10-24 day-0005): a native UI ARTI did not open and that clears on its own, with the pad
# released. The owner's HOST gift opened an Accept / Decline box mid walk (closed HOST-side ~75 s later); never
# answered for ARTI, only waited for. A hold-up / busy animation (no menu) clears within seconds.
QUESTION_SETTLE_SECONDS = 120.0
BUSY_SETTLE_SECONDS = 10.0
# ADR 189: an unconfirmed hand is re-observed this long (game seconds) before the session continues with a FAULT; a stop
# needs HELD_ITEM_PUBLISHED_FAILS published selections that each left the object in her hand.
HELD_ITEM_REOBSERVE_S = 30.0
HELD_ITEM_PUBLISHED_FAILS = 3


def held_item_attempts(run):
    """The food/hand cleanup attempts of this row: published-and-failed vs refused-before-input."""
    rows = [a for a in run.state.get("terminal_cleanup_attempts", ()) if a.get("phase") == "food"]
    return dict(published_failed=sum(1 for a in rows if a.get("status") == "failed" and a.get("request_id")),
                refused=sum(1 for a in rows if a.get("status") == "failed" and not a.get("request_id")))


def held_item_danger(run):
    """ADR 189: positive evidence that her hand cannot be put back to a tool, never a refusal or an unknown read."""
    return held_item_attempts(run)["published_failed"] >= HELD_ITEM_PUBLISHED_FAILS
# Sat gift: one native answer per box; failed chooser requests have bounded retries.
GIFT_SKILLS = ("terima_hadiah", "tolak_hadiah")
GIFT_VERIFY_SECONDS = 3.0
DIALOGUE_TEXT_MAX = 600  # journal bound for a recovery-pressed DialogueBox text (Sat T5)
# Unknown topology keeps the farthest legacy bound; it is not labeled measured.
UNKNOWN_HOME_MINUTES = max(max(HOME_MINUTES.values()), HOME_MINUTES["Mine"] + 90)
LOW_HP = 80  # the existing makan admission threshold; survival food ignores the clock reserve
# Very low vitals (the existing "only food/home" thresholds below): a real reason to offer bed early.
VERY_LOW_ENERGY = 20
ENERGY_TOOLS = frozenset({"tebang", "pecah_batu", "cangkul", "bersihkan", "siram"})
# Smoke 2026-10-23 #12 (day 1, Farm 20:00-21:30): bats / a RockGolem / a slime took HP 85 -> 18 while ARTI
# worked and walked home; Live7 never showed the attack. Self-defence reads the mod's ui.context combat block
# (hp, max_hp, monsters with distance) only; nothing here creates a monster target or an input.
THREAT_NEAR_TILES = 8.0      # = the mod reflex radius: a monster this close counts after a native HP loss
THREAT_CONTACT_TILES = 3.0   # a monster this close is in contact (bats close ~3 tiles in about a second)
DAMAGE_RECENT_MIN = 30       # game minutes an observed native HP loss still means "just attacked"
CRITICAL_HP_PCT = 40         # = the mod reflex HP floor default: combat is never offered at or below it
RETREAT_HP_PCT = 60          # attacked below this: only food / the way home / bed are offered
# Review FIX-FIRST (172cd580): only these field-work skills are ended by an attack (never a menu / chest /
# shipping / dialogue / animal / fishing / travel / food / bed row, whose cleanup an interrupt could break).
ATTACK_INTERRUPT_SKILLS = ENERGY_TOOLS | frozenset({"tanam", "panen", "pungut_drop", "forage", "jalan_ke"})
UNDER_ATTACK_REASON = "under_attack"
# Owner 2026-10-23 ("killaura"): attacked outside the Mines and able to fight (a weapon, else the scythe; HP above
# CRITICAL_HP_PCT), Live7 starts `lawan` itself (a safety reflex, no model call): after a work row ended
# under_attack, a fresh native hit, or a monster within REFLEX_CONTACT_TILES. At most REFLEX_AUTO_MAX automatic
# starts per REFLEX_WINDOW_MIN game minutes; after each one ARTI chooses (lawan / pergi_ke home) with the facts.
REFLEX_CONTACT_TILES = 2.0
REFLEX_AUTO_MAX = 2
REFLEX_WINDOW_MIN = 60
# Review r1 #4: these lawan endings hold the offer until a NEW native hit or a NEW monster inside the lawan radius.
LAWAN_HOLD_REASONS = ("no_monster_near", "lawan_leg_cap", "lawan_swing_cap", "lawan_on_warp")
# M12 (2026-10-23, Wilderness farm night): a knockout wakes ARTI in the clinic (Hospital) inside a native event.
# Recoverable: the existing G3 event advance (text_event OBJ_EVENT plan) presses its plain lines, bounded.
KNOCKOUT_REASON = "knocked_out"
KNOCKOUT_LOCATION = "Hospital"
KNOCKOUT_PRESSES = 12
KNOCKOUT_SECONDS = 90.0
# Evening fallback timing only; bed availability has no clock gate (owner 2026-10-23).
BED_OFFER_FROM = 2200
BED_RETURN_CHOICE_FROM = 2030  # optional bed after a model-chosen evening return (offer - 90 min); never default
CLOCK_KEYS = ("stardew_live7_home_target", "stardew_live7_bed_offer_from")
DEBRIS_CHOICES = 3  # representative current targets; don't bury other action families in rectangles
BED_DEADLINE = 2400  # vanilla: in bed before midnight; passing out comes at 02:00
# fix54I: honest names for a leg that ended without its own reason (see _live7_leg_end_reason); still NOT_READY.
LEG_END_NOT_READY = ("recovered_after:", "leg_ended_no_reason")
HOME_LOOP_RELEASE_MIN = 60  # game minutes before pending Farm crop work is offered again from home
SEED_RESERVE_G = 100
SEED_BATCH = 5
CHEAPEST_SEED_G = 20  # vanilla Parsnip; only a trip/request bound, native stock still owns prices
CHEST = "(BC)130"
CHEST_SUFFIXES = (":(BC)130", ":(BC)232")  # = arti_stardew_economy.STORE_CHEST_SUFFIXES (placed ordinary chests)
STORE_MISS_NOTE = "simpan ke peti gagal: belum sampai/menghadap peti; tas tidak berubah; peti masih ada"
# Categories only, never an empty-slot gate (ADR 137). ADR 105: a cast takes no slot.
COLLECTING = frozenset({"tebang", "pecah_batu", "pungut_drop", "bersihkan", "panen", "beli", "forage",
                        "gali_artefak", "tambang_bijih", "pohon_buah", "rawat_hewan"})
# Native goal_route.weather: sun|rain|storm|snow|wind|green_rain. Green Rain waters crops like rain.
BAD_WEATHER = (*sorted(RAIN_WEATHER), "raining", "stormy", "lightning")

DESTINATION_WORK_MINUTES = 0  # ADR 167: a chosen destination owes no minimum stay
MINES_OPEN = (1, "spring", 5)  # vanilla: the landslide clears on Y1 Spring 5 (HYPOTHESIS until S29 mail)
# Daytime festival dates (Egg Festival, Flower Dance, Luau, Fair, Festival of Ice, Winter Star; vanilla
# 1.6.15 AreStoresClosedForFestival). Public in-game knowledge (the calendar on Pierre's board, the TV and
# villagers announce festivals), so it stays a trip gate: on a festival day Town is the festival map and the
# shut shop could never be discovered there. Wednesday is NOT public: it is discovered (ShopMemory).
SEED_SHOP_FESTIVALS = frozenset({("spring", 13), ("spring", 24), ("summer", 11), ("fall", 16),
                                 ("winter", 8), ("winter", 25)})
SHOP_MEMORY_DATES = 8
WEEKDAY_NAMES = dict(mon="Senin", tue="Selasa", wed="Rabu", thu="Kamis", fri="Jumat", sat="Sabtu", sun="Minggu")


def festival_day(date):
    """A daytime festival date from the public in-game calendar (stores closed)."""
    try:
        return (str(date[1]).casefold(), int(date[2])) in SEED_SHOP_FESTIVALS
    except (TypeError, ValueError, IndexError):
        return False


def _day_key(date):
    try:
        return [int(date[0]), str(date[1]).casefold(), int(date[2])]
    except (TypeError, ValueError, IndexError):
        return None


@dataclass
class ShopMemory:
    """What ARTI found herself at Pierre's: dates she saw it shut and the weekdays that taught her.

    Her own experience, never a rule table: the first closed Wednesday costs one trip, later Wednesdays
    she remembers. Carried across days by the cycle (journal ``live7_shop_memory`` -> resume).
    """
    weekdays: dict = field(default_factory=dict)  # "wed" -> [year, season, day] first seen closed
    dates: list = field(default_factory=list)     # [[year, season, day], ...] seen closed
    counters: dict = field(default_factory=dict)  # ADR 105: location -> {"absent"|"present": [year, season, day, clock]}, any shop
    schedules: dict = field(default_factory=dict)  # ADR 206: only ranges actually published by a native counter

    @classmethod
    def load(cls, raw):
        memory = cls()
        if isinstance(raw, Mapping):
            for wd, seen in (raw.get("weekdays") or {}).items():
                if str(wd).casefold()[:3] in WEEKDAY_NAMES and _day_key(seen):
                    memory.weekdays[str(wd).casefold()[:3]] = _day_key(seen)
            memory.dates = [d for d in map(_day_key, raw.get("dates") or ()) if d][-SHOP_MEMORY_DATES:]
            for shop, seen in (raw.get("counters") or {}).items():
                if isinstance(seen, Mapping):
                    memory.counters[str(shop)] = {k: list(seen[k]) for k in ("absent", "present") if isinstance(seen.get(k), (list, tuple))}
            for shop, row in (raw.get('schedules') or {}).items():
                if isinstance(row, Mapping) and all(type(row.get(k)) is int for k in ('opens_at', 'closes_at')):
                    memory.schedules[str(shop)] = dict(row)
        return memory

    def dump(self):
        return dict(weekdays={k: list(v) for k, v in self.weekdays.items()}, dates=[list(d) for d in self.dates],
                    counters={k: dict(v) for k, v in self.counters.items()}, schedules={k: dict(v) for k, v in self.schedules.items()})

    def note_hours(self, shop, date, clock, targets):
        changed = False
        for target in targets:
            if not isinstance(target, Mapping):
                continue
            availability = target.get('availability') or {}
            if target.get('kind') != 'shop_counter':
                continue
            start, end = availability.get('opens_at'), availability.get('closes_at')
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= 2800
                    or start % 100 >= 60 or end % 100 >= 60):
                continue
            row = dict(opens_at=start, closes_at=end, basis=availability.get('hours_basis'), date=list(date), time=clock)
            previous = self.schedules.get(shop) or {}
            if any(previous.get(k) != row[k] for k in ('opens_at', 'closes_at', 'basis', 'date')):
                self.schedules[shop] = row
                changed = True
        return changed

    def note_counter(self, shop, date, clock, present):
        """One native keeper sighting. True when it is new: the first of its kind at this shop on this date."""
        today, kind = _day_key(date), "present" if present else "absent"
        seen = self.counters.setdefault(shop, {})
        if today is None or type(clock) is not int or list(seen.get(kind, ())[:3]) == list(today):
            return False
        seen[kind] = [*today, clock]
        return True

    def counter_facts(self):
        """ADR 105: her own dated sightings of a shop with no keeper at the counter, for her next page and later days."""
        from arti_stardew_world import location_arg

        def when(seen):
            return "Y%s %s %s %02d:%02d" % (seen[0], seen[1], seen[2], seen[3] // 100, seen[3] % 100)
        return ["%s: penjaga tidak ada di konter %s%s" % (location_arg(shop), when(seen["absent"]),
                                                         "; ada " + when(seen["present"]) if "present" in seen else "")
                for shop, seen in self.counters.items() if "absent" in seen]

    def closed(self, date, weekday):
        """'seen_closed_today' | 'remembered_<wd>' | None."""
        if (today := _day_key(date)) is not None and today in self.dates:
            return "seen_closed_today"
        wd = str(weekday or "").casefold()[:3]
        return "remembered_" + wd if wd in self.weekdays else None

    def record(self, date, weekday, *, learn_weekday=True):
        """A closure ARTI just saw. True when this is new knowledge.

        ``learn_weekday`` only for the shop's own all-day predicate (goal_route.shop_open False): a locked
        door alone may be a one-off (an event, Pierre away) and teaches just this date."""
        today = _day_key(date)
        if today is None or today in self.dates:
            return False
        self.dates = (self.dates + [today])[-SHOP_MEMORY_DATES:]
        wd = str(weekday or "").casefold()[:3]
        if learn_weekday and (today[1], today[2]) not in SEED_SHOP_FESTIVALS and wd in WEEKDAY_NAMES:
            self.weekdays.setdefault(wd, today)
        return True

    def hint(self, date, weekday):
        why = self.closed(date, weekday)
        if why is None:
            return None
        if why == "seen_closed_today":
            return "Tadi kamu lihat sendiri Pierre's / SeedShop tutup hari ini."
        wd = why.removeprefix("remembered_")
        seen = self.weekdays[wd]
        return ("Kamu ingat Pierre's / SeedShop tutup hari %s (kamu lihat sendiri Y%s %s %s). Hari ini %s." %
                (WEEKDAY_NAMES[wd], seen[0], seen[1], seen[2], WEEKDAY_NAMES[wd]))


def shop_closed_seen(facts, context):
    """'route' | 'door' | None: ARTI is in Town during Pierre's hours and the shop is natively shut now.

    'route' = the shop's own all-day predicate (goal_route.shop_open False, its record inside the hours);
    'door' = only every SeedShop door fact locked. The caller adds the trip condition (she tried a
    seed-shop trip today); this only reads the present."""
    from arti_stardew_skills_quest import locked_doors
    now = facts.time_of_day
    if facts.location != "Town" or type(now) is not int or not 900 <= now < 1700:
        return None
    route = ((context or {}).get("mines_ui") or {}).get("goal_route")
    if (isinstance(route, Mapping) and route.get("shop_open") is False
            and type(route.get("time_of_day")) is int and 900 <= route["time_of_day"] < 1700):
        return "route"
    return "door" if locked_doors((context or {}).get("targets"), "SeedShop") else None


def shop_observation(facts, context, date, weekday, memory=None):
    """Observed access/counter or her existing refusal memory, never the admission schedule.

    One open/closed entrance does not teach a daily opening range. Actual sign/dialogue text stays
    on its existing presented-text surface; no timetable is filled in from DESTINATIONS.
    """
    # AUDIT_UNDERSTANDING1 (ADR 93): the native door hours of the catalog place, when the map publishes them, replace
    # the "unknown" that contradicted the same page's arrival token; no Python timetable is filled in.
    from arti_runtime.day_facts import native_entry_hours
    from arti_stardew_world import places
    hours = native_entry_hours(places(context), "SeedShop")
    unknown = "jam pintu native %s" % hours if hours else "jam buka belum diketahui"
    doors = [f.get("enterable") for f in context.get("targets", {}).values()
             if f.get("kind") in ("door", "building_door", "warp") and f.get("target_location") == "SeedShop"]
    if facts.location == "SeedShop" and facts.shop_open:
        return "Terlihat toko buka sekarang; " + unknown
    if any(value is True for value in doors):
        return "Terlihat akses terbuka sekarang; " + unknown
    if doors and all(value is False for value in doors):
        return "Terlihat akses tertutup sekarang; " + unknown
    if memory is not None and (hint := memory.hint(date, weekday)):
        return hint + " " + unknown
    return unknown


# Model boundaries without a VERIFIED row while the game clock does not move
# (HOST paused, frozen world): ask for help and stop calling the model until
# either changes. A safety guard, never a work/brain quota.
NO_PROGRESS_BOUNDARIES = 6
# ADR 99 (a46: 141 of 537 decisions had no knowledge frame, among them 95 of 162 first decisions after a warp and 26 of 26
# first of a day: no quests, bag fits or game facts, median 1 destination). The mod captures once per 60 ticks and her
# provider call takes seconds, so one decision waits this long for the frame of the place she stands in, then decides as before.
CATALOG_WAIT_SEC = 2.5
# Exact runtime codes (or colon-separated tokens of a wrapped reason) that end a
# live session. Target diagnostics such as ``animal_identity_changed`` or
# ``tool_target_identity_mismatch`` are ordinary row failures, never STOP.
SAFETY_CODES = frozenset({
    "stop", "safety_stop", "input_leak", "identity_mismatch", "identity_unavailable",
    "crash_or_identity_unavailable", "identity_changed", "play_identity_changed", "native_identity_changed",
    "motor_identity_changed", "actor_identity_changed", "domain_identity_changed", "epoch_changed",
    "unsafe_held_item", "uncontrolled_input", "no_progress_600s", "failfast", "crash",
    "owner_play_stopped", "worker_stopped", "journalcapacityreached", "journalpersistencefault"})
JOURNAL_WARNING_COUNT = 48
JOURNAL_STOPS = ("JournalCapacityReached", "JournalPersistenceFault")
SAFETY_TOKEN_PREFIXES = ("monitor_violation", "pad_watch")
CLEAN_LIFECYCLES = ("completed", "rejected", "not_taken")
# scripts/m1_operator/pad_watch.py EXIT table (the runtime never imports operator scripts).
PAD_WATCH_CODES = {"warp_unblocked": 5, "pad_held_after_terminal": 6, "owned_while_focused": 7,
                   "cursor_jump": 8, "prefs_force_on": 9, "pad_mode_after_release": 10,
                   "pad_mode_cleanup_fault": 11}
# ADR 195: a world walk/talk that hit a menu mid-move resumes after the generic UI path, this many times.
WORLD_UI_RESUME_MAX = 3


def destination_map(arg):
    """Where ``pergi_ke`` is going: owner aliases, or a generic ``@Location`` token. None = unknown / home."""
    from arti_stardew_interact import _LOCATION_ALIASES
    if not isinstance(arg, str) or not arg:
        return None
    if arg in _LOCATION_ALIASES:
        return _LOCATION_ALIASES[arg]
    if arg.startswith("@") and len(arg) > 1:
        return arg[1:]
    return None


def prefer_paused_destination(calls, policy, location):
    """ADR 155: on the people page the paused destination is offered first and still startable."""
    pause = getattr(policy, "route_pause", None)
    dest = pause.get("destination") if isinstance(pause, dict) and pause.get("map") == location else None
    if not dest:
        return calls
    lead = [c for c in calls if c.name == "pergi_ke" and c.arg == dest]
    if not lead:
        return calls
    return lead + [c for c in calls if not (c.name == "pergi_ke" and c.arg == dest)]


def configure_clock(cfg):
    """Live7 evening defaults from the trial config (one process per day; unset keys restore the defaults)."""
    global HOME_TARGET, BED_OFFER_FROM, BED_RETURN_CHOICE_FROM
    cfg = cfg or {}
    target = cfg.get("stardew_live7_home_target", 2600)
    offer = cfg.get("stardew_live7_bed_offer_from", 2200)
    for name, value, low, high in (("home_target", target, 2200, 2600), ("bed_offer_from", offer, 1800, 2400)):
        if type(value) is not int or not low <= value <= high or value % 100 >= 60:
            raise ValueError("stardew_live7_%s must be an HHMM int in %d..%d" % (name, low, high))
    HOME_TARGET, BED_OFFER_FROM = target, offer
    back = minutes(offer) - 90
    BED_RETURN_CHOICE_FROM = back // 60 * 100 + back % 60
    return HOME_TARGET, BED_OFFER_FROM


def safety_reason(reason):
    """Hard safety only; missing cleanup/readback and permission refusals are recoverable."""
    tokens = [t for t in re.split(r"[:\s]+", str(reason or "").casefold()) if t]
    return any(t in SAFETY_CODES or t.endswith("_crash") or t.startswith(SAFETY_TOKEN_PREFIXES) for t in tokens)


def journal_refusal(reason):
    tokens = re.split(r"[:\s]+", str(reason or "").casefold())
    return next((code for code in JOURNAL_STOPS if code.casefold() in tokens), None)


def recognizes(text):
    from arti_stardew_part_a import recognizes_goal
    return recognizes_goal(text, GOAL)


def minutes(hhmm):
    return hhmm // 100 * 60 + hhmm % 100


def legacy_home_eta(location):
    from arti_stardew_skills_attempts import INTERIOR_PARENT
    location = str(location or "")
    if location.startswith(("Cabin", "FarmHouse")):
        return 15
    if location == "Temp":
        return HOME_MINUTES["Farm"]  # fix63A: leaving a festival puts her on the Farm porch (Event.exitEvent)
    if location.startswith("UndergroundMine"):
        return HOME_MINUTES["Mine"] + 90  # native ladder/Leave before travel
    return HOME_MINUTES.get(dict(INTERIOR_PARENT, SeedShop="Town").get(location, location), UNKNOWN_HOME_MINUTES)


def home_eta(location, policy=None):
    from arti_runtime.travel_time import home
    return home(location, policy)["minutes"]


def outward_minutes(here, destination):
    """Game minutes to reach a destination: the cabin door opens onto the Farm (no Farm-sized walk)."""
    if destination == "Farm" and str(here or "").startswith(("Cabin", "FarmHouse")):
        return home_eta(here)
    return home_eta(destination)


def observed_homeward_exit(location, exits):
    """The homeward hop where NO shipped route (ITINERARIES, FARM_NEIGHBOURS, INTERIOR_PARENT) names one.

    Review cand-g 2026-10-24: in FishShop, Saloon, JojaMart, AdventureGuild, other shops and houses the known hop is
    None, so `pergi_ke home` was offered and failed exit_not_offered; at the reserve that spent the home retries and
    lost the day. The hop is the room's CURRENTLY OFFERED native exit (enterable, not npc_only), never an invented
    warp. Several exits: a target with a shipped route to the Farm first, then the shorter home ETA, then the name.
    Only destinations the travel grammar names (interact._LOCATION_ALIASES) qualify; else None (home stays offered).
    """
    from arti_stardew_interact import _LOCATION_ALIASES
    from arti_stardew_skills_attempts import _hop
    if location == "Farm" or str(location or "").startswith(("Cabin", "FarmHouse")):
        return None  # cand-g integration 2026-10-24: on the Farm the home leg exists; a Farm exit leads AWAY from home
    named = set(_LOCATION_ALIASES.values())
    # Never an exit farther from the Farm than here (the shipped ETA table); unknown places rank as the farthest.
    options = [t for t in exits if isinstance(t, str) and t in named and t != location
               and home_eta(t) <= home_eta(location)]
    if not options:
        return None
    return min(options, key=lambda t: (t != "Farm" and _hop(t, "Farm") is None, home_eta(t), t))


def _plain_talk(call):
    # The existing S16 gift grammar is "item ke villager"; its transaction
    # uncertainty and retry fences are distinct from an ordinary conversation.
    return call.name == "ngobrol_hadiah" and " ke " not in str(call.arg or "").casefold()


def admission(call, facts, policy=None):
    if call.name == "tidur" or _plain_talk(call) or call == S.SkillCall("pergi_ke", "home"):
        return None
    if call.name == "lawan_mine":
        from arti_runtime.mines_rules import energy_due
        if energy_due(facts.energy):
            return "skipped_energy:mines_exit_reserve"
    if type(facts.time_of_day) is not int:
        return "skipped_time:home_eta_unknown"
    from arti_runtime.travel_time import admission_budget
    if minutes(facts.time_of_day) > admission_budget(call, facts, policy)["cutoff_min"]:
        return "skipped_time:home_reserve"
    return None


def home_due(facts, policy=None):
    from arti_runtime.travel_time import reserve
    return reserve(facts.location, facts.time_of_day, policy)["active"]


def home_call(call):
    return call.name == "tidur" or call == S.SkillCall("pergi_ke", "home")


def reserve_choice(calls, facts):
    """ADR 190 rule 1: available packing choices are not an automatic home plan."""
    calls = list(calls)
    for c in calls:
        if c.name == "tidur":
            return c
    if str(facts.location or "").startswith("UndergroundMine"):
        from arti_runtime.mines_contact import exit_call
        return exit_call(calls, facts.location)
    # Under sleep_only the planner exposes only native homeward travel hops.
    hop = next((c for c in calls if c.name == "pergi_ke" and c.arg != "home"), None)
    return hop or next((c for c in calls if c == S.SkillCall("pergi_ke", "home")), None)


def sighting_walk(call, policy, location):
    """ADR 136 (pkg76): a `jalan_ke` to something she has seen is a walk, never crop work: it neither hides the soil approach
    nor reads as the crop's next step."""
    return call.name == "jalan_ke" and policy is not None and policy.seen_objects.at(location, call.tile) is not None


def home_unreachable_fact(facts, home_failures, last_failed, calls, blocked=None):
    """FIX_TRAVEL_DEADLINE (smoke cand-o d3a0 L2431/L2432): the reserve's automatic home retries are spent.
    Facts for her own choice: where she is, whether a bed is offered here, how long until the native pass-out."""
    now = minutes(facts.time_of_day) if type(facts.time_of_day) is int else None
    if facts.location == "Temp":
        # fix58A (ADR 84): a festival map has no door and no native 02:00 pass-out (Game1.UpdateOther !eventUp).
        return dict(location=facts.location, time_of_day=facts.time_of_day, home_attempts_failed=home_failures,
                    last_failed=list(last_failed) if last_failed else None,
                    sleep_here_offered=any(call.name == "tidur" for call in calls),
                    pass_out_at=None, minutes_until_pass_out=None,
                    note=("Percobaan pulang otomatis sudah habis. Ini peta festival: tidak ada pintu dan tidak ada "
                          "pingsan jam 02:00 selama festival. pergi_ke home menanyakan lewat game, sama seperti di tepi peta, apakah mau meninggalkan "
                          "festival. Pilih sendiri: coba pulang lagi, kegiatan lain, atau ask_owner."))
    return dict(location=facts.location, time_of_day=facts.time_of_day, home_attempts_failed=home_failures,
                last_failed=list(last_failed) if last_failed else None,
                sleep_here_offered=any(call.name == "tidur" for call in calls),
                **({"route_blocked": dict(blocker=blocked["blocker"], needs=blocked["needs"], route="none")}
                   if blocked else {}),
                pass_out_at=PASS_OUT_CLOCK,
                minutes_until_pass_out=None if now is None else max(0, minutes(PASS_OUT_CLOCK) - now),
                note=("Percobaan pulang otomatis sudah habis. Tidur hanya bisa lewat kasur yang ditawarkan di skills. "
                      "Jam 02:00 ARTI pingsan otomatis: bangun besok dengan energi berkurang dan bisa kehilangan uang. "
                      "Pilih sendiri: coba pulang lagi, kegiatan lain, atau ask_owner."))


def _vitals(combat, health=None):
    """(hp, max_hp) from the mod's combat block (the lower of motor/combat HP), or None."""
    if not isinstance(combat, Mapping):
        return None
    hp = min((v for v in (health, combat.get("hp")) if type(v) is int), default=None)
    max_hp = combat.get("max_hp")
    if hp is None or type(max_hp) is not int or max_hp <= 0:
        return None
    return hp, max_hp


def observe_hp(policy, combat, health, now):
    """Remember the last native HP; an observed loss stamps ``damaged_at`` (game clock). Advisory memory only."""
    vitals = _vitals(combat, health) or ((health, None) if type(health) is int else None)
    if vitals is None:
        return
    hp = vitals[0]
    if policy.hp_seen is not None and hp < policy.hp_seen and type(now) is int:
        policy.damaged_at = now
    policy.hp_seen = hp
    if vitals[1] is not None:
        policy.max_hp_seen = vitals[1]


def threat(combat, health=None, *, damaged_at=None, now=None):
    """The native attack picture, or None. Outside the Mines only: Mines floors keep lawan_mine's own rules.

    Applies when a monster is in contact, or ARTI took native damage recently with a monster near.
    """
    vitals = _vitals(combat, health)
    if vitals is None or combat.get("in_mines") is True:
        return None
    monsters = sorted((m for m in combat.get("monsters") or () if isinstance(m, Mapping)
                       and isinstance(m.get("distance"), (int, float)) and m["distance"] <= THREAT_NEAR_TILES),
                      key=lambda m: m["distance"])
    if not monsters:
        return None
    hp, max_hp = vitals
    recent = (type(damaged_at) is int and type(now) is int
              and 0 <= minutes(now) - minutes(damaged_at) <= DAMAGE_RECENT_MIN)
    contact = monsters[0]["distance"] <= THREAT_CONTACT_TILES
    if not (contact or recent):
        return None
    pct = 100 * hp // max_hp
    return dict(hp=hp, max_hp=max_hp, hp_pct=pct, took_damage=recent, contact=contact,
                level="critical" if pct <= CRITICAL_HP_PCT else "low" if pct < RETREAT_HP_PCT else "ok",
                monsters=[dict(type=m.get("type"), distance=round(float(m["distance"]), 1)) for m in monsters[:3]])


def weapon_of(combat, inventory):
    """(qualified id, "pedang" | "sabit") a fight would use, or (None, None).

    A real weapon first (the held one, else any owned); else the scythe, which counts as a weapon (owner 2026-10-23)
    when the deployed mod admits it to reflex_combat (``combat.scythe_held`` published, see scythe_reflex).
    """
    from arti_stardew_combat import SCYTHES, is_weapon, scythe_reflex
    owned = sorted(q for q, n in (inventory or {}).items() if n)
    held = combat.get("weapon") if isinstance(combat, Mapping) and combat.get("weapon_held") is True else None
    real = [q for q in [held] + owned if is_weapon(q)]
    if real:
        return real[0], "pedang"
    scythes = [q for q in [held] + owned if q in SCYTHES]
    if scythes and scythe_reflex(combat):
        return scythes[0], "sabit"
    return None, None


def defend_refusal(combat, inventory, t, location=None):
    """None when ARTI can fight back now (Live7 offers `lawan`), else why not (docs/stardew/live7/SELF_DEFENSE.md)."""
    from arti_stardew_combat import SCYTHES, time_rule
    if t is None:
        return "no_threat"
    if S.farm_interior(location):
        return "farm_building"  # review r1 #2: the HOST's Slime Hutch slimes are Monsters too
    if t["level"] == "critical":
        return "hp_critical"  # safety: at/below the mod reflex floor only the way home / food / bed
    if weapon_of(combat, inventory)[0] is None:
        owned = [q for q, n in (inventory or {}).items() if n]
        return "no_weapon:scythe_needs_new_mod" if any(q in SCYTHES for q in owned) else "no_weapon"
    return time_rule(combat)  # outside the Mines only the mod's 01:00 hard stop


def attack_interrupt(call, run_state, combat, health, policy, now):
    """UNDER_ATTACK_REASON when a running work skill should end cleanly at this decision boundary, else None.

    The skill's start HP is pinned in ``run_state``; a native HP loss during the run with a monster near, or a
    monster in contact below the retreat line, ends it as a recoverable row (the next offer leads with home).
    """
    before = policy.hp_seen  # the last HP read before this check (the offer boundary for a run's first check)
    observe_hp(policy, combat, health, now)
    vitals = _vitals(combat, health)
    if not attack_work(call, run_state) or vitals is None:
        return None
    hp = vitals[0]
    start = run_state.get("live7_attack_hp")
    if type(start) is not int:
        start = before if type(before) is int and before > hp else hp  # a hit before the first check counts
    elif hp > start:
        start = hp  # food during the run raises the baseline
    run_state["live7_attack_hp"] = start
    t = threat(combat, health, damaged_at=policy.damaged_at, now=now)
    if t is None:
        return None
    # A monster behind a fence / wall is "in contact" by distance only: contact alone needs a recent native hit.
    if hp < start or t["contact"] and t["took_damage"] and t["level"] != "ok":
        return UNDER_ATTACK_REASON
    return None


def attack_work(call, state):
    # A travel parent's automatic obstruction-clearing leg is tool work. The
    # plain retreat remains travel and must never be stopped to start a fight.
    return call.name in ATTACK_INTERRUPT_SKILLS or call.name == "pergi_ke" and bool(state.get("route_clear"))


def danger_context(combat, health, inventory, policy, now, *, energy=None, location=None):
    """The offer's ``danger`` block (facts for ARTI's own lawan / pulang choice), or None without a threat."""
    t = threat(combat, health, damaged_at=policy.damaged_at, now=now)
    if t is None:
        return None
    why = defend_refusal(combat, inventory, t, location)
    qid, kind = weapon_of(combat, inventory)
    held = combat.get("weapon") if combat.get("weapon_held") is True and combat.get("weapon") else None
    names = ", ".join("%s %.1f" % (m["type"] or "monster", m["distance"]) for m in t["monsters"][:2])
    near = Counter(str(m.get("type") or "monster") for m in combat.get("monsters") or ()
                   if isinstance(m, Mapping) and isinstance(m.get("distance"), (int, float))
                   and m["distance"] <= THREAT_NEAR_TILES)
    count = ", ".join("%d %s" % (n, k) for k, n in near.most_common(3))
    home = home_eta(location, policy) if location else None
    vitals = "HP %d/%d%s" % (t["hp"], t["max_hp"], ", energi %d" % energy if type(energy) in (int, float) else "")
    if why is None:
        fight = "kamu bisa membalas dengan %s" % kind
    else:
        fight = "kamu belum bisa membalas (%s)" % {
            "no_weapon:scythe_needs_new_mod": "sabit belum bisa dipakai", "no_weapon": "belum ada senjata",
            "hp_critical": "HP kritis", "farm_building": "di dalam bangunan farm"}.get(why, why)
    # Review (near-budget render): one compact block; the note carries every fact (only it survives trimming).
    # Owner principle 2026-10-23 ("[recorded remark omitted]"): facts only, no directive; she decides.
    return dict(level=t["level"], took_damage=t["took_damage"], can_fight=why is None, why_not=why,
                weapon=dict(qualified_item_id=qid, kind=kind, held=held) if qid else None, monsters_near=dict(near),
                home_minutes=home, options=(["lawan"] if why is None else []) + ["pergi_ke home"],
                note=("Lagi diserang (%s petak), %s%s. Monster dekat: %s; %s. Rumah %s menit game. Di rumah tidak "
                      "ada monster; makan memulihkan HP."
                      % (names, vitals, ", baru kena serang" if t["took_damage"] else "", count, fight,
                         "±%d" % home if home is not None else "?")))


def reflex_due(policy, t, refusal, now):
    """True when Live7 starts `lawan` itself (the owner's safety reflex), see REFLEX_CONTACT_TILES.

    Needs a threat ARTI can answer (``refusal`` None). Triggers: a work row just ended under_attack
    (``policy.reflex_due``), a native hit newer than the last automatic start, or a monster within
    REFLEX_CONTACT_TILES with no automatic start in the window. Bounded by REFLEX_AUTO_MAX per window.
    """
    if t is None or refusal is not None or type(now) is not int:
        return False
    last = policy.reflex_starts[-1] if policy.reflex_starts else None
    hit = (t["took_damage"] and type(policy.damaged_at) is int
           and (last is None or minutes(policy.damaged_at) > last))
    close = (bool(t["monsters"]) and t["monsters"][0]["distance"] <= REFLEX_CONTACT_TILES
             and (last is None or minutes(now) - last >= REFLEX_WINDOW_MIN))
    if not (policy.reflex_due or hit or close):
        return False
    return sum(0 <= minutes(now) - s < REFLEX_WINDOW_MIN for s in policy.reflex_starts) < REFLEX_AUTO_MAX


def attacks_today(policy, health):
    """S1 fact (replaces the at-home food/bed filter): today's attack interrupts, or None. Never an offer rule."""
    if not policy.attack_interrupts:
        return None
    times = ", ".join("%s %s" % (loc, _clock(t)) for loc, t in policy.attack_log[-3:])
    hp = ("HP %d/%d" % (health, policy.max_hp_seen) if type(health) is int and policy.max_hp_seen
          else "HP %s" % health if type(health) is int else "HP ?")
    return "Diserang monster %dx hari ini (%s), %s; HP pulih lewat makan atau tidur." % (
        policy.attack_interrupts, times or "?", hp)


def knockout_fact(policy, facts, context):
    """M12 fact: "Tadi pingsan ..., bangun di klinik, kehilangan ..." once the wake-up event is over, else None.

    The loss is the row baseline (money, bag) minus the first fresh read after the event; never an offer rule.
    """
    k = policy.knockout
    if k is not None and k.get("native_event"):
        return None  # ADR 191: only the released native wake read settles this event
    if k is not None and (str(facts.location or "") != KNOCKOUT_LOCATION or not facts.menu_open):
        before = k["before"]
        money = context.get("money")
        lost_money = before.get("money") - money if type(before.get("money")) is int and type(money) is int else None
        lost = {q: n - facts.inventory.get(q, 0) for q, n in (before.get("inventory") or {}).items()
                if type(n) is int and n > facts.inventory.get(q, 0)}
        policy.knockouts.append(dict(clock=k["clock"], from_location=k["from_location"],
                                     money=lost_money if lost_money and lost_money > 0 else 0, items=lost))
        policy.knockout = None
    if not policy.knockouts:
        return None
    last = policy.knockouts[-1]
    if "after" in last:
        from arti_runtime.knockout import fact
        return fact(last)
    loss = ", ".join((["%dg" % last["money"]] if last["money"] else [])
                     + ["%s x%d" % (q, n) for q, n in sorted(last["items"].items())][:5]) or "tidak ada yang hilang"
    return "Pingsan terakhir (tanggal tidak diketahui; %s %s), bangun di klinik, kehilangan %s." % (
        last["from_location"] or "?", _clock(last["clock"]) if type(last["clock"]) is int else "?", loss)


def _seasonal(date):
    seasons = ("spring", "summer", "fall", "winter")
    try:
        return int(date[0]), seasons.index(str(date[1]).casefold()), int(date[2])
    except (TypeError, ValueError, IndexError):
        return None


def mines_open(date):
    d, o = _seasonal(date), _seasonal(MINES_OPEN)
    return d is not None and d >= o


def hard_dispose_refusal(row) -> str | None:
    from arti_stardew_economy import hard_dispose_refusal as refuse
    return refuse(row)


def goods_facts(context):
    return _goods_facts(dict(inventory=context.get('inventory'), location_id=context.get('location_id'), mines_ui=dict(ui_pad=dict(economy=dict(sell=(((context.get('mines_ui') or {}).get('ui_pad') or {}).get('economy') or {}).get('sell'))), shipping_bin=(context.get('mines_ui') or {}).get('shipping_bin'))))


def _goods_facts(context):
    """Join current inventory to already-observed menu prices/quest refusals, never estimate a price."""
    ui = context.get("mines_ui") or {}
    sell = ((ui.get("ui_pad") or {}).get("economy") or {}).get("sell") or ()
    shipping = (ui.get("shipping_bin") or {}).get("actor_slots") or ()
    out = []
    for original in context.get("inventory", ()):
        row = dict(original)
        for source, price_source in [(s, 'shop') for s in sell] + [(s, 'shipping_bin') for s in shipping]:
            item = source.get("item")
            qid = item.get("qualified_item_id") if isinstance(item, Mapping) else item
            quality = item.get("quality") if isinstance(item, Mapping) else source.get("quality")
            if (qid != row.get("qualified_item_id") or quality != row.get("quality")
                    or source.get("slot") != row.get("slot")):
                continue
            for key in ("unit_price", "policy", "ship_policy"):
                if key in source:
                    row[key] = source[key]
            if type(source.get('unit_price')) is int:
                row.update(price_source=price_source, price_location=context.get('location_id'))
        if "unit_price" not in row and type(row.get("price")) is int:
            row["unit_price"] = row["price"]
        out.append(row)
    return out


def disposable(context, policy, action):
    """Owner B: every observed owned stack except tools and quest items; native gates still revalidate.

    fix10e (a14 d3:J913/J985, a13 #4): a sell offer leaves out what THIS shop's own native sell predicate was seen
    to refuse (Pierre: Stone, Wood); unobserved shops still offer everything (a denylist, never a guess)."""
    shop_refused = context.get("shop_refused_items", ()) if action == "sell" else ()
    return [row for row in goods_facts(context)
            if hard_dispose_refusal(row) is None
            and row.get("qualified_item_id") not in context.get("quest_refused_items", ())
            and row.get("qualified_item_id") not in shop_refused
            and type(row.get("stack")) is int and row["stack"] > 0
            and isinstance(row.get("name"), str) and row.get("qualified_item_id")]


def economy_context(facts, context):
    money = context.get("money")
    tight = type(facts.free_slots) is int and facts.free_slots <= 2
    low = type(money) is int and money < SEED_RESERVE_G + SEED_BATCH * CHEAPEST_SEED_G
    return dict(free_slots=facts.free_slots, bag_tight=tight, bag_full=facts.free_slots == 0,
                seed_reserve_g=SEED_RESERVE_G, income_preferred=low or tight)


def _requirement(req, facts, context, weather, date, item_policy=None):
    from arti_stardew_economy import ItemPolicy
    item_policy = item_policy or ItemPolicy()
    if req is None:
        return True
    seeds = bool(seasonal_seeds(date, facts, context))
    return {
        "hoe": S._has(facts, "(T)Hoe"), "axe": S._has(facts, S.AXE), "pickaxe": S._has(facts, *S.PICKAXES),
        "rod": S._has(facts, *S.ROD_QIDS), "seeds": seeds,
        "can_dry": S._has(facts, "(T)WateringCan") and weather not in BAD_WEATHER,
        "can_not_full": facts.can_water is not None and facts.can_water[0] < facts.can_water[1],
        "money": (facts.free_slots != 0 or bool(stackable_seeds(facts, context))) and bool(seed_quantities(context, item_policy)),
        "sell": bool(disposable(context, item_policy, "sell")),
        "ship": bool(disposable(context, item_policy, "ship")),
        "weapon_pickaxe": S._has(facts, *S.PICKAXES) and any(str(q).startswith("(W)") for q in facts.inventory),
        "mines_open": mines_open(date),
    }.get(req, False)


def shop_travel_minutes(facts, *, earliest=False):
    """R7 outward walk estimates; the shorter bound leaves room for a fast trip."""
    here = str(facts.location or "")
    if here.startswith(("Cabin", "FarmHouse")):
        return 60 if earliest else 140
    estimates = dict(Farm=40, BusStop=20, Town=0) if earliest else dict(Farm=110, BusStop=80, Town=45)
    return estimates.get(here, 0 if earliest else home_eta(here))


def seed_shop_closed(date, weekday, context=None, now=None, memory=None):
    """Why ARTI believes Pierre's (the only seed seller the skills know) is closed today, else None.

    A festival day (public calendar, SEED_SHOP_FESTIVALS) closes it. Otherwise only her own ShopMemory decides
    (smoke 2026-10-23 item 11: no Wednesday foresight). A closed day she has not discovered yet costs one trip;
    the native route then shows it shut and she remembers. ``context``/``now`` are kept for callers.
    """
    if festival_day(date):
        return "festival"
    return memory.closed(date, weekday) if memory is not None else None


def shop_trip_allowed(facts, weekday, date=None, context=None, memory=None):
    """R7: bracket arrival, rather than using a home/bed ETA as the shop's outward walk.

    Estimates from the rehearsal's ordinary itinerary; admission only, never arrival proof.
    Unknown origins must already be in opening hours. Every door still needs a native offer.
    A day ARTI knows is closed (seed_shop_closed, her memory) is no trip: no other seed seller exists.
    """
    if (type(facts.time_of_day) is not int
            or seed_shop_closed(date, weekday, context, facts.time_of_day, memory) is not None):
        return False
    now = minutes(facts.time_of_day)
    return (now + shop_travel_minutes(facts, earliest=True) >= minutes(900)
            and now + shop_travel_minutes(facts) + 20 < minutes(1700))


def seed_buy_policy(policy, money):
    """Narrow the existing buy policy for Live7; native prices/stock still own purchases."""
    return replace(policy, buy_max_total=min(policy.buy_max_total,
                   max(0, money - seed_reserve(money)) if type(money) is int else 0))


def seed_reserve(money):
    """Keep 100g for other needs, including when the purse is already below that floor."""
    return SEED_RESERVE_G


def stackable_seeds(facts, context):
    """PM-1 fix10d (a13 d2 SeedShop 10:50-13:50, 12/12 bag): seed stacks already in the bag that a purchase of the
    same seed can grow. beli is a COLLECTING skill, so a full bag hid it although the bag held Parsnip Seeds x1; native
    stock, price and fit still revalidate the purchase."""
    if facts.free_slots != 0 or not context.get("inventory_complete", True):
        return ()
    return tuple(sorted({f["name"] for f in context.get("inventory", ()) if f.get("category") == -74
                         and isinstance(f.get("name"), str) and type(f.get("stack")) is int
                         and 0 < f["stack"] < (f.get("max_stack") if type(f.get("max_stack")) is int else 999)}))


def seed_quantities(context, policy):
    """(arg, default, maximum, price-known) from current native stock, or a bounded stock request.

    Before the menu opens use the cheapest known seed as a request bound; native prices still revalidate.
    Never label that request ceiling as an observed affordable maximum.
    """
    from arti_stardew_economy import MAX_QTY
    money = context.get("money")
    budget = seed_buy_policy(policy, money).buy_max_total
    if budget <= 0:
        return []
    econ = context.get("economy") or {}
    rows = []
    for row in econ.get("rows", ()):
        price = row.get("price")
        if (not re.search(r"\bseeds?$", str(row.get("name", "")), re.I) or row.get("fits") is not True
                or type(price) is not int or price <= 0 or row.get("tool") or row.get("trade_item")):
            continue
        maximum = min(MAX_QTY, budget // price, row.get("capacity", 0))
        if type(row.get("stock")) is int and row["stock"] >= 0:
            maximum = min(maximum, row["stock"])
        if maximum > 0:
            rows.append((row["name"], min(SEED_BATCH, maximum), maximum, True))
    if econ.get("kind") == "shop" and econ.get("ready") is True:
        return rows
    # The generic binder selects the cheapest permitted native seed after opening the counter.
    maximum = min(SEED_BATCH, budget // CHEAPEST_SEED_G)
    return [("benih", maximum, maximum, False)] if maximum else []


def travel_options(date, weekday, weather, facts, context, *, item_policy=None, memory=None):
    """Native graph destinations. No Python hours, activity requirements or itinerary allowlist."""
    from arti_stardew_world import travel_options as native_options
    options = native_options(context, str(facts.location or ""), now=facts.time_of_day)
    for option in options:
        schedule = getattr(memory, 'schedules', {}).get(option['location'])
        if schedule and (option['arrival'].get('counter') or {}).get('opens_at') is None:
            from arti_runtime.travel_facts import remembered_counter
            option['arrival'] = remembered_counter(option['arrival'], schedule)
    return options


def _clock(hhmm):
    return None if type(hhmm) is not int else "%02d:%02d" % (hhmm // 100 % 24, hhmm % 100)


def planting_soil(context):
    return _planting_soil(dict(local_window=context.get('local_window'),targets=context.get('targets',{})))


def _planting_soil(context):
    """Bare soil the planting driver can use now, rather than old full-scene dirt.

    The independent local window refreshes after each tool action. Outside it,
    M1's full-scene copies still described previously planted tiles as bare.
    No coordinates or remembered crop list can authorize another planting.
    """
    from arti_stardew_tool_work import _has_crop
    window = context.get("local_window")
    center = S._wave1_xy((window or {}).get("center"))
    return [f for f in context.get("targets", {}).values()
            if f.get("terrain_type") == "HoeDirt" and "crop" in f and not _has_crop(f)
            and (tile := S._wave1_xy(f.get("tile"))) is not None
            and center is not None and max(abs(a - b) for a, b in zip(tile, center)) <= 1]


BAG_NAMES_SHOWN = 16


def day_context(date, weekday, weather, facts, context, *, sleep_only=False, offered_calls=None, memory=None):
    """Native day facts and advisory own planting tiles for the model's next choice."""
    from arti_runtime.day_facts import crop_line, season_fact, seed_lines, stored_seed_lines, SEASON_RULE, food_fact
    names = {f.get("qualified_item_id"): f.get("name") for f in context.get("inventory", ())
             if isinstance(f, Mapping) and f.get("qualified_item_id")}
    held = sorted(((q, n) for q, n in facts.inventory.items() if n), key=lambda x: (-x[1], str(x[0])))
    from arti_runtime.fact_names import item_name
    tools = [item_name(q, names) for q, _ in held if str(q).startswith(("(T)", "(W)"))]
    items = ["%s x%d" % (item_name(q, names), n) for q, n in held if not str(q).startswith(("(T)", "(W)"))]
    if len(items) > BAG_NAMES_SHOWN:  # fix61: a single Diamond under ten bigger stacks never reached her page (ADR 88)
        items = items[:BAG_NAMES_SHOWN] + ["+%d lagi" % (len(items) - BAG_NAMES_SHOWN)]
    now = facts.time_of_day
    left = None if type(now) is not int else max(0, minutes(BED_DEADLINE) - minutes(now))
    y, s, d = (list(date) + [None] * 3)[:3] if date else (None, None, None)
    from arti_stardew_tool_work import _has_crop, crop_needs_water
    soil = [f for f in context.get("targets", {}).values() if f.get("terrain_type") == "HoeDirt"]
    dry = sum(crop_needs_water(f) for f in soil)
    ripe = sum(1 for f in soil if isinstance(f.get("crop"), Mapping) and f["crop"].get("ready_for_harvest") is True
               and f["crop"].get("dead") is not True)
    plantable = seasonal_seeds(date, facts, context)
    seeds = sum(f.get("stack", 0) for f in plantable)
    offered = None if offered_calls is None else {c.name for c in offered_calls}
    next_step = next((name for name, ready in (
        ("panen", ripe),  # ADR 45: a crop read ready in the scene is the one step that never expires
        ("tanam", plantable and planting_soil(context)),
        # Smoke 10-24 d5/d6: an empty can made "siram" the priority beside "gembor kosong" (3x needs_refill).
        # siram stays offered; it is only not the crop priority until the can holds water.
        ("siram", dry and weather not in BAD_WEATHER and (facts.can_water or (1,))[0] != 0),
        ("jalan_ke", context.get("planting_walk_pending")
                     or context.get("remembered_crops") and weather not in BAD_WEATHER),
        ("cangkul", plantable and context.get("tillable_tiles")))
        if ready and (offered is None or name in offered)), None)
    result = {
        "date": "Y%s %s %s" % (y, s, d) if date else None, "weekday": weekday,
        "weather": weather or "unknown", "clock": _clock(now), "minutes_until_midnight": left,
        "weather_tomorrow": ((context.get("mines_ui") or {}).get("goal_route") or {}).get("weather_tomorrow") or "unknown",
        "energy": None if facts.energy is None else int(facts.energy), "hp": facts.health,
        "money": context.get("money"), "inventory_highlights": items, "tools": tools,
        "economy": economy_context(facts, context),
        "day_rules": "Fakta: jam 02:00 karakter pingsan kalau belum tidur; cadangan waktu pulang tetap berlaku. Kapan tidur, itu pilihanmu. "
                     + SEASON_RULE,  # ADR 93: the season-change fact beside the pass-out fact
        "crops": dict(seeds=seeds, stored_seeds=sum(r["stack"] for r in context.get("stored_seeds", ())),
                      hoed_tiles=len(soil), planted_tiles=sum(_has_crop(f) for f in soil),
                      dry_tiles=dry, ready_tiles=ripe, plantable_tiles=len(planting_soil(context)),
                      counts_scope="scene counts may lag outside local tool window; unseen tiles unknown", next_step=next_step,
                      needs="Benih perlu tanah dicangkul: cangkul, tanam, lalu siram. Tanaman hidup yang kering perlu disiram kecuali hujan.",
                      buy="Benih: Pierre's / SeedShop; " + shop_observation(facts, context, date, weekday, memory)),
        "pergi_ke": "pergi_ke can go to any listed place; after_travel lists what you can do there.",
        # Mind slice 1 render reclaim: finish_means / end_day repeated the Live7 system rule word for word.
        "sleep_only": bool(sleep_only),
    }
    if mines := mines_rules_fact(facts.location, offered):
        result["aturan_mines"] = mines  # ADR 149: beside day_rules; staged like pintu_nama so it displaces no watched fact
    if remembered := context.get("remembered_crops"):
        result["crops"].update(remembered=[dict(r) for r in list(remembered.values())[:4]],
            remembered_count=len(remembered),
            memory_hint="Tanaman yang kutanam VERIFIED belum diketahui basah hari ini: boleh jalan_ke petaknya, lalu amati lagi sebelum siram. Ingatan bukan bukti tanaman masih ada.")
    if own := context.get("own_crops"):
        result["crops"]["own"] = dict(own)  # ADR 45: her own plantings as dated facts across days
    result["kebun"] = crop_line(result["crops"])  # ADR 99: the crop state stays on the page when the block leaves it
    from arti_runtime.experience_facts import place_line, experienced, view
    catalog = view(context.get("world_knowledge")) or {}
    if line := place_line(catalog):
        result["tempat"] = line
    if not experienced(catalog, "seed shop"):
        # A current door/counter or remembered refusal is experience too. Keep
        # that observation without teaching an unseen shop's services/hours.
        observed = shop_observation(facts, dict(context, world_knowledge=catalog), date, weekday, memory)
        if observed != "jam buka belum diketahui":
            result["crops"]["buy"] = observed
        else:
            result["crops"].pop("buy", None)
    if season := season_fact(date):
        result["musim"] = season  # ADR 93: where the season stands, from the native date alone; fix64: no key when unknown
    # ADR 113: days to ripen against days left, beside the choice to plant; FIX72D: for the seeds she put away too
    if seeds := seed_lines(context.get("inventory"), date) + stored_seed_lines(context.get("stored_seeds"), date):
        result["benih_tumbuh"] = seeds
    if food := food_fact(context.get("mines_ui"), context.get("inventory")):
        result["makanan"] = food  # ADR 93: the mod's own cheap-food rows, by name; `makan` stays her choice
    return result


def mines_rules_fact(location, offered=None, policy=None):
    """ADR 149: on a Mines page, the rules that still decide for her there, as one plain fact (else None)."""
    from arti_runtime.mines_rules import mines_page, rules_line
    return rules_line(policy, location) if mines_page(location, offered or ()) else None


def key(call):
    return call.name, call.arg


def seasonal_seeds(date, facts, context):
    """Use the existing native Data/Crops table, not the shared tree/crop category alone.

    This season also applies to crop stock carried in town or at home. Native
    placement admission still owns the actual soil, location and planting input.
    """
    from arti_stardew_seeds import CROP_SEEDS, seed_in_season
    season = date[1] if date and len(date) > 1 else None
    return [f for f in context.get("inventory", ()) if f.get("category") == -74
            and f.get("stack", 1) > 0
            and (f.get("qualified_item_id") in CROP_SEEDS or f.get("qualified_item_id") == "(O)770" and season != "winter")
            and seed_in_season(f.get("qualified_item_id"), season) is not False]


def bedtime_default(facts, policy, calls):
    """An observed evening with no admitted productive work; reserve safety is separate."""
    if type(facts.time_of_day) is not int or facts.time_of_day < BED_OFFER_FROM:
        return False
    non_work = {"tidur", "pergi_ke", "makan", "cek_pagi"}
    home = str(facts.location or "").startswith(("Cabin", "FarmHouse"))
    if home:
        # Cabin filler stays selectable, but cannot postpone the default after a home choice.
        non_work.update(("siapkan_pancing", "masak", "simpan_ke_chest"))
    if any(c.name not in non_work and not (home and c.name == "buat" and c.arg in ("peti", "Chest"))
           for c in calls):
        return False
    farm = policy.work_offers.get("Farm")
    return not (S.SkillCall("pergi_ke", "farm") in calls and farm and farm[0])


def choice_priority(call, next_crop, *, bedtime=False):
    """Advisory ordering and invalid-pick fallback only; a valid model pick always wins."""
    if call.name == "makan":
        return -2
    if call.name == "tidur":
        return -1 if bedtime else 6
    if call.name == "lawan":
        return 5.5  # her choice beside the way home; never the invalid-pick fallback while other work is offered
    if call.name == next_crop:
        return 0
    return {"siram": 1, "forage": 2, "pungut_drop": 2, "bersihkan": 4, "pergi_ke": 5,
            "cek_pagi": 5}.get(call.name, 3)


def structural_refusal(row):
    """Cycle-scoped geometry refusals only; temporary stalls never survive the night."""
    return (row.get("outcome_class") == CLEAN_FAIL and row.get("reason") == "scythe_sweep_protected"
            and row.get("skill") == "bersihkan" and isinstance(row.get("arg"), str)
            and bool(row.get("arg")) and isinstance(row.get("location"), str) and bool(row.get("location")))


def classify(outcome, cleanup, action=None, *, status="READY", stopped=False, identity_ok=True,
             mutation_unresolved=False, native_mines=False):
    """Safety precedence is independent of how a skill labels its result.

    A possibly-applied input is UNCERTAIN before it can be NOT_READY: runtime
    readiness never launders an action whose lifecycle/cleanup is not clean.
    """
    reason = str(outcome[1] or "").casefold()
    if journal_refusal(S._g(action, "reason", "")):
        return SAFETY_STOP
    if stopped or not identity_ok or status in ("OFF", "FAIL_CLOSED") or safety_reason(reason):
        return SAFETY_STOP
    if (not isinstance(cleanup, Mapping) or cleanup.get("input_released") is not True
            or cleanup.get("menu") is not None or cleanup.get("free") is not True
            or cleanup.get("food_clear") is False):
        return UNCERTAIN  # release/reobserve before another input; recovery owns escalation
    from arti_runtime.action_outcomes import unreadable
    if (mutation_unresolved or unreadable(reason)
            or any(s in reason for s in ("uncertain", "unconfirmed", "possible_mutation", "unreconciled", "released_before_latch",
                                         "cancel"))
            or S._g(action, "lifecycle") in ("uncertain", "interrupted")
            or S._g(action, "world_effect_status") in ("uncertain", "possible_mutation", "unverified")):
        return UNCERTAIN
    # Only a clean lifecycle is a clean failure (or a clean readiness pause).
    unclean = action is not None and (S._g(action, "cleanup_confirmed") is not True
                                      or S._g(action, "lifecycle") not in CLEAN_LIFECYCLES)
    if unclean and (outcome[0] != "done" or status != "READY"):
        return UNCERTAIN
    if (S._g(action, "reason") == "native_no_input_refusal_reconciled"
            and S._g(action, "lifecycle") == "not_taken" and S._g(action, "cleanup_confirmed") is True
            and S._g(action, "world_effect_status") == "zero_effect"):
        return PRECONDITION  # the next fresh route offer has no world effect to quarantine
    if ((status != "READY" and not native_mines)
            or reason.startswith(LEG_END_NOT_READY) or "runtime_not_ready" in reason):
        return NOT_READY
    if outcome[0] == "done":
        return VERIFIED
    if reason.startswith(("precondition", "skipped_time")):
        return PRECONDITION
    from arti_runtime.route_pause import pause_reason
    if pause_reason(outcome[1]):
        return INTERRUPTED  # ADR 195: the people page is not a failed walk
    return CLEAN_FAIL


def mail_quest_text(q):
    """One Indonesian fact line for a quest a mail letter filed on close (S29 plan_input ``mail_quests`` entry)."""
    title = q.get("title") if isinstance(q.get("title"), str) and q["title"] else None
    target = q.get("target") if isinstance(q.get("target"), str) and q["target"] else None
    item = q.get("item") if isinstance(q.get("item"), str) and q["item"] else None
    head = "surat quest" + (" dari %s" % target if target else "") + ": " + (title or "quest baru")
    if item:
        head += " (%s%s)" % (item, " x%d" % q["number"] if isinstance(q.get("number"), int) and q["number"] > 1 else "")
    return head + "; sudah masuk jurnal quest, kamu yang pilih mau dikerjakan atau tidak."


# fix10b #4: native exits and synthetic route stands are navigation, not work targets. A failed or uncertain leg is
# a fact about that attempt; the still-published exit stays offerable (CONTROLLER_CONTRACT_v1 rules 1/9).
NAVIGATION_REF_PREFIXES = ("warp:", "door:", "bdoor:", "home_waypoint:", "part_a_waypoint:")


def way_out_trip(call, location) -> bool:
    """ADR 239: a trip that leaves this place through a door, warp or map edge.

    A trip from a Mines floor leaves by its ladder; the floor's own way-out memory
    (``arti_runtime.mines_exit``) owns it, including the F7 recovery quarantine.
    """
    return call.name == "pergi_ke" and not str(location or "").startswith("UndergroundMine")


def navigation_exit(ref) -> bool:
    return isinstance(ref, str) and (
        ref.startswith(NAVIGATION_REF_PREFIXES) or ref.startswith("xt:mine:"))


@dataclass
class DayPolicy:
    crop_memory: CropMemory = field(default_factory=CropMemory)
    # FIX81B (ADR 145): {map id: {place, date, clock, names}} villagers she saw on that map today (social_facts.remember_seen).
    villagers_seen: dict = field(default_factory=dict)
    ground_items: GroundItems = field(default_factory=GroundItems)  # fix59A (ADR 86): items left on the ground
    chest_memory: ChestMemory = field(default_factory=ChestMemory)  # FIX72D: what her chests held when she last looked
    item_names: dict = field(default_factory=dict)  # FIX71C, FIX72D: qid -> native name, every bag and shop row she has read
    catch_totals: dict = field(default_factory=dict)  # FIX71C: place -> landed catches by kind, across all days
    catch_kinds: dict = field(default_factory=dict)  # FIX73D: item -> landed catches of that kind, across all days
    press_results: dict = field(default_factory=dict)  # spot -> repeated press answer, across days of this run
    failures: Counter = field(default_factory=Counter)
    quarantine: set = field(default_factory=set)  # (skill, arg, location); day-scoped
    # ADR 239: (skill, arg, location) -> a failed trip out of a place, bound to its place, tile and clock.
    way_out_failures: dict = field(default_factory=dict)
    refs: set = field(default_factory=set)
    uncertain_items: set = field(default_factory=set)
    quest_refused_items: set = field(default_factory=set)  # observed quest refusals, retained through cycle resume
    # fix10e: per shop location, qualified ids its open ShopMenu's native sell predicate refused (accepts=false).
    shop_refused: dict = field(default_factory=dict)
    last_failed: tuple | None = None
    sleep_only: bool = False
    bedtime_return: bool = False  # model chose home at an observed evening clock, never an ETA/fallback
    home_return_chosen: bool = False  # optional bed after an explicit late-afternoon home choice
    recoveries: int = 0
    wait_since: float | None = None
    catalog_wait_since: float | None = None  # ADR 99: monotonic start of this decision's wait for its knowledge frame
    crop_read: tuple | None = None  # ADR 99: (date, crop item ids) of today's latest Farm scene; never persisted
    release_after: float | None = None
    help_basis: tuple | None = None
    help_hints: list = field(default_factory=list)  # newest five advisory-only HelpHint fields
    alarmed: bool = False
    # F5: model boundaries since the last VERIFIED row or game-clock change.
    idle_boundaries: int = 0
    progress_clock: int | None = None
    recovery_history: list = field(default_factory=list)  # bounded facts for her next activity choice
    busy_since: float | None = None
    # Deferred settle work that needs the fresh post-release read.
    pending_reconcile: list = field(default_factory=list)
    pending_sleep: dict | None = None
    last_wallet: dict | None = None  # last fresh wallet on the campaign date, before rollover
    # Sat gift provenance: qualified id -> sender of a VERIFIED accepted gift (a fact only; selling stays her choice).
    gift_from: dict = field(default_factory=dict)
    # The gather fence is attributed to a quarantined (or natively verified) row.
    fence_settled: bool = False
    floor_retry: set = field(default_factory=set)
    mines_exit_recovery: dict = field(default_factory=dict)  # ADR 160: failed native routes, not cached permissions
    deepest_mine_floor: int | None = None  # ADR 196: max UndergroundMineN observed this campaign
    mines_last_encounter: str | None = None
    mines_native_baseline: dict | None = None
    mines_ko_fact: str | None = None
    home_failures: int = 0
    history: list = field(default_factory=list)  # last eight terminal tasks, including honest no-ops
    no_effect: dict = field(default_factory=dict)  # fix55B: repeated choices with no native change (ADR 79)
    travel_edges: Counter = field(default_factory=Counter)
    last_trips: dict = field(default_factory=dict)  # ADR 167 measured trips
    travel_home: str | None = None
    travel_topology: dict = field(default_factory=dict)  # native topology; never movement permission
    crop_routes: dict = field(default_factory=dict)  # existing rolling-window route memory, per crop
    crop_walk_targets: dict = field(default_factory=dict)  # offered waypoint -> crop ref (not model input)
    seed_looked: set = field(default_factory=set)  # taken seed-walk destinations, including failed probes
    seed_probed: set = field(default_factory=set)  # observed positions; chore stops never spend the budget
    seed_surveyed: set = field(default_factory=set)  # taken fallback destinations, within seed_looked
    seed_survey_targets: set = field(default_factory=set)  # offered keys that spend the fallback allowance
    seed_own_soil: set = field(default_factory=set)  # today's VERIFIED cangkul tiles; advisory only
    seed_own_order: list = field(default_factory=list)  # the same tiles, oldest first (tanam prefers the newest)
    seed_walk_targets: dict = field(default_factory=dict)  # separate from remembered watering trips
    # fix42 (a33 d6:J493, d7:J232): native CanRefillWateringCanOnTile sightings, location -> tile, today only. An empty
    # can out of sight of water left her guessing "home" (d7: the host's house); a sighting is a fact and a walk.
    water_seen: dict = field(default_factory=dict)
    fishing_seen: list = field(default_factory=list)  # fix55B: places with a native fishing edge today (fact only)
    catches: list = field(default_factory=list)  # ADR 115: today's landed catches, for her count (fact only)
    travel_loop: bool = False
    work_offers: dict = field(default_factory=dict)
    empty_visits: dict = field(default_factory=dict)
    productive_visit: set = field(default_factory=set)
    finish_streak: int = 0  # independent of the game clock: waiting is not a choice
    stale_reasks: int = 0  # consecutive boundaries whose model pick vanished during provider latency
    interrupted_travel: dict | None = None  # advisory, never executable cached input (ADR 59)
    # ADR 155: (date, map) where a route already paused today (one pause per map per game day), and the latest pause,
    # a page fact until her next activity starts (never executable cached input).
    route_pauses: set = field(default_factory=set)
    route_pause: dict | None = None
    # ADR 161: {villager name casefolded: game date} of her verified talks (social_facts.mark_greeted); the scene's native
    # talked_today lags a verified talk by up to a minute, so offers and pauses read both.
    greeted: dict = field(default_factory=dict)
    forage_looked: set = field(default_factory=set)
    forage_sightings: dict = field(default_factory=dict)  # advisory only, never cached executable offers
    forage_memory: ForageMemory = field(default_factory=ForageMemory)  # FIX73E: forage she has seen, as dated native sightings
    seen_objects: SeenObjects = field(default_factory=SeenObjects)  # FIX77C (ADR 136): what she has seen and can walk back to, by map
    cc_seen: str | None = None  # FIX75B (ADR 129): the Community Center state her page last showed in full (bundle_knowledge.signature)
    page_location: str | None = None  # FIX75B: the map of her last decision page; `forage_ingat` rides on a change only
    read_today: dict = field(default_factory=dict)  # newest native TV/mail text read today (fact only)
    store_names: dict = field(default_factory=dict)  # fix63B: listed simpan_ke_chest name -> its offered arg
    items_told: set = field(default_factory=set)  # fix61: item tooltips already on her page today (ADR 88)
    items_seen: set = field(default_factory=set)  # fix61: bag items seen today, so a new one is told first
    empty_fallbacks: int = 0  # consecutive boundaries with an unusable answer and no fallback choice
    loop_since: int | None = None  # first observed clock of the current unproductive travel loop
    farm_crop_pending: bool = False  # the Farm's last offer had siram/tanam before the last_failed filter
    stranded_release: int | None = None  # clock of the last early release for a Cabin that offered only bed
    loop_help_asked: bool = False  # review 2026-10-23: the shuttle's "buntu" help is asked at most once a day
    home_help_asked: bool = False  # review cand-g 2026-10-24: the reserve's "buntu" help, once a day, at the last retry
    festival_stuck_recoveries: int = 0  # fix63A: bounded no-progress recoveries on a festival map
    home_unreachable: dict | None = None  # reserve retries spent: facts handed to her choice instead of ending the day
    # fix10e (a14 spring 12, 42x pergi_ke home): a proven route block (complete read, no route) with what it needs.
    # While she stays in that location the way home is not offered; her other activities are (a fact, not a mode).
    home_blocked: dict | None = None
    shop_failures: int = 0  # first clean failure plus every retry outcome
    shop_retry_at: int | None = None  # game minutes; only CLEAN_FAIL can arm a retry
    hp_seen: int | None = None  # last native HP read (self-defence; advisory memory, never a target)
    damaged_at: int | None = None  # game clock of the last observed native HP loss
    attack_interrupts: int = 0  # work rows ended cleanly by UNDER_ATTACK_REASON today
    max_hp_seen: int | None = None  # the mod's published max_hp (combat block), for the attacks_today fact
    attack_log: list = field(default_factory=list)  # [location, clock] per under_attack row today (fact only)
    reflex_due: bool = False  # a work row ended under_attack; the next boundary may start the lawan reflex
    reflex_starts: list = field(default_factory=list)  # game minutes of today's automatic lawan starts
    reflex_pending: bool = False  # the selected lawan was started by the reflex (journal attribution only)
    lawan_hold: dict | None = None  # r1 #4: {damaged_at, ids} after a lawan that found nothing to fight
    knockout: dict | None = None  # M12: the clinic wake-up being recovered (at, clock, from, before, presses)
    knockouts: list = field(default_factory=list)  # today's knockouts (fact for her offer)
    previous_night: dict | None = None  # native saved lost night; factual, never a new goal
    shop_memory: ShopMemory = field(default_factory=ShopMemory)  # closures ARTI saw herself (cross-day)

    def remember_shop_acceptance(self, location, components):
        """fix10e: learn a shop's native sell predicate from its open ShopMenu's bag slots (accepts_item)."""
        if not isinstance(location, str) or not location:
            return False
        from arti_stardew_economy import _qualify
        refused = set(self.shop_refused.get(location, ()))
        before = set(refused)
        for component in components or ():
            if not isinstance(component, Mapping) or component.get("kind") != "slot":
                continue
            item = _qualify((component.get("value") or {}).get("item"))
            accepts = component.get("accepts_item")
            if item is None or type(accepts) is not bool:
                continue
            (refused.add if accepts is False else refused.discard)(item)
        self.shop_refused[location] = refused
        return refused != before

    def remember_quest_refusals(self, context):
        """Learn only explicit native quest evidence; closing a menu cannot erase a known refusal."""
        from arti_stardew_economy import _qualify
        ui = context.get("mines_ui") or {}
        sell = ((ui.get("ui_pad") or {}).get("economy") or {}).get("sell") or ()
        bin_slots = (ui.get("shipping_bin") or {}).get("actor_slots") or ()
        observed = {r.get("qualified_item_id") for r in goods_facts(context)
                    if hard_dispose_refusal(r) == "quest_item"}
        for row in (*sell, *bin_slots):
            if row.get("policy") == "refused:quest_item" or row.get("ship_policy") == "refused:quest_item":
                item = row.get("item")
                observed.add(item.get("qualified_item_id") if isinstance(item, Mapping) else item)
        added = {qid for raw in observed if (qid := _qualify(raw)) is not None} - self.quest_refused_items
        self.quest_refused_items.update(added)
        return bool(added)

    def shop_retry_ready(self, now):
        return (self.shop_retry_at is not None and self.shop_failures < SHOP_FAILURES_MAX
                and type(now) is int and minutes(now) >= self.shop_retry_at)

    def release_home_loop(self, now, *, stranded=False):
        """A Farm<->home loop hides the Farm for HOME_LOOP_RELEASE_MIN game minutes, not the whole day.

        Only while the Farm's last native offer still had planting/watering (recorded before the
        last_failed filter: a failed siram right before home still counts). The edges are cleared, so
        one more unproductive cycle re-arms the loop: at most one retry per window. ``stranded``: the
        Cabin would offer only bed; release early, at most once per window (review M4 residual b).
        Returns True when released.
        """
        if not self.travel_loop or type(now) is not int:
            self.loop_since = None
            return False
        if self.loop_since is None:
            self.loop_since = now
            if not stranded:
                return False
        home_loop = any(a == "Farm" and str(b).startswith(("Cabin", "FarmHouse"))
                        and count and self.travel_edges[(b, a)] for (a, b), count in self.travel_edges.items())
        farm = self.work_offers.get("Farm") or ()
        crop_work = bool(farm and farm[0] & {("siram", None), ("tanam", None)}) or self.farm_crop_pending
        elapsed = minutes(now) - minutes(self.loop_since) >= HOME_LOOP_RELEASE_MIN
        early = stranded and (self.stranded_release is None
                              or minutes(now) - minutes(self.stranded_release) >= HOME_LOOP_RELEASE_MIN)
        if home_loop and crop_work and (elapsed or early):
            if not elapsed:
                self.stranded_release = now
            self.travel_edges.clear()
            self.travel_loop = False
            self.empty_visits.pop("Farm", None)
            self.loop_since = None
            return True
        return False

    def travel_filter(self, calls, location, context):
        """An empty visit stays unattractive until a fresh native offer changes there."""
        from arti_stardew_interact import _LOCATION_ALIASES
        work = frozenset(key(c) for c in calls if c.name not in ("pergi_ke", "tidur", "cek_pagi", "makan"))
        # New native targets count as new opportunities even for skills without
        # an argument (e.g. tebang). Clock, pose, exit revisions do not.
        refs = frozenset(ref for ref, f in context.get("targets", {}).items() if work
                         if isinstance(f, Mapping) and f.get("kind") not in
                         ("warp", "door", "building_door", "location") and ref not in self.refs)
        seeds = frozenset((f.get("qualified_item_id"), f.get("stack")) for f in context.get("inventory", ())
                          if f.get("category") == -74 and f.get("stack", 0) > 0)
        self.work_offers[location] = (work, refs, seeds)
        if self.sleep_only:
            return calls  # never suppress an escape or bedtime hop
        def new_work(destination):
            # Owner 2026-10-23 ("[recorded remark omitted]"): live7-game-m10 D4 19:30 the Cabin offered only bed
            # because the Farm (store / till still offered there) was hidden as a home<->Farm "empty visit". A place
            # whose last look offered real work is an option, never hidden; she chooses (recent_actions shows the
            # shuttle). Only a place that offered nothing stays out until something there changes.
            old, current = self.empty_visits[destination], self.work_offers.get(destination)
            if current and (current[0] or current[1]):
                return True
            if destination == "Farm" and seeds and (old is None or len(old) < 3 or old[2] != seeds):
                return True  # bought seeds change what a previously empty Farm visit can achieve
            return False
        return [c for c in calls if c.name != "pergi_ke" or home_call(c)
                or (dest := _LOCATION_ALIASES.get(c.arg)) not in self.empty_visits or new_work(dest)]

    def shuttle(self):
        """(place, place, round trips) of the busiest back-and-forth since the last productive row, or None.
        travel_edges are cleared by every productive row, so the count is trips with no work done in between."""
        best = None
        for (a, b), n in self.travel_edges.items():
            cycles = min(n, self.travel_edges.get((b, a), 0))
            if cycles and a < b and (best is None or cycles > best[2]):
                best = (a, b, cycles)
        return best

    def shuttle_fact(self) -> str | None:
        """Owner 2026-10-23: the shuttle is told as a fact, never used to hide a destination."""
        trip = self.shuttle()
        if trip is None:
            return None
        name = lambda loc: "Kabin" if str(loc).startswith(("Cabin", "FarmHouse")) else str(loc)  # noqa: E731
        return "%d kali bolak-balik %s↔%s, 0 kerja selesai" % (trip[2], name(trip[0]), name(trip[1]))

    def home_blocked_here(self, location):
        b = self.home_blocked
        return b if b is not None and location is not None and b.get("location") == location else None

    def blocked(self, call, location, facts=None, tile=None):
        if home_call(call) and (self.sleep_only or location == "Temp"):
            return False  # quarantine the failed target/approach, never the route home at the bedtime reserve or out of a festival
        bound = self.way_out_failures.get((*key(call), location))
        if bound is not None:
            # ADR 239: without current facts the old answer stands; with them, only unchanged facts hold it.
            from arti_runtime import way_out
            if facts is None or way_out.holds(bound, place=location, clock=getattr(facts, "time_of_day", None),
                                              tile=tile):
                return True
        return (*key(call), location) in self.quarantine or (*key(call), None) in self.quarantine

    def note_read(self, plan_input):
        """S29 native text she read today, newest per channel/letter, bounded; never a gate."""
        if not isinstance(plan_input, Mapping):
            return
        for k in ("weather", "fortune"):
            if isinstance(plan_input.get(k), str) and plan_input[k]:
                self.read_today[k] = plan_input[k][:READ_TODAY_CHARS]
        mail = [m for m in plan_input.get("mail") or () if isinstance(m, str) and m]
        if mail:
            self.read_today["mail"] = mail[-1][:READ_TODAY_CHARS]
        for q in plan_input.get("mail_quests") or ():
            if isinstance(q, Mapping):  # a quest letter's close filed this quest natively: her fact for the next offer
                self.read_today["mail_quest"] = mail_quest_text(q)[:READ_TODAY_CHARS]

    def record(self, call, outcome_class, reason, *, target=None, location=None, items=(),
               start_location=None, productive=None, time_of_day=None, tile=None, route_refused=False):
        if outcome_class == CLEAN_FAIL and reason == UNDER_ATTACK_REASON:
            # A monster ended this row at a clean boundary: not the target's or the skill's failure. No quarantine,
            # failure count or retired target; the next offer carries the attack facts (danger, attacks_today).
            self.attack_interrupts += 1
            self.attack_log.append([location, time_of_day])
            self.history.append(dict(skill=call.name, arg=call.arg, outcome_class=outcome_class, reason=reason,
                                     location=location, from_location=start_location, productive=False,
                                     **({"n": call.n} if call.n is not None else {})))
            del self.history[:-8]
            self.last_failed = None
            return False
        from arti_runtime.route_pause import pause_reason
        if pause_reason(reason):
            # ADR 155/195: a route paused on a new map where people are. Not the trip's failure: no failure
            # count, quarantine, home retry or travel loop; picking the same destination continues the trip.
            self.history.append(dict(skill=call.name, arg=call.arg, outcome_class=outcome_class, reason=reason,
                                     location=location, from_location=start_location, productive=False))
            del self.history[:-8]
            self.last_failed = None
            return False
        if outcome_class == CLEAN_FAIL and (call.name == "lawan" or call.name in S.MINES_SKILLS and reason in (
                "partial:mines_combat_timeout", "partial:mines_low_hp_exit", "partial:mines_combat_disengaged",
                "partial:mines_curfew_exit", "partial:mines_budget_exit", "partial:mines_energy_exit")):  # stated safety exit
            # Self-defence that ended cleanly (monster gone, low-HP retreat, caps): never quarantined, so the next
            # attack can still be answered; the row stays in history as a fact. ADR 146: these exact Mines reasons
            # require verified alive exit or safe floor disengagement; unverified results keep their failure fence.
            self.history.append(dict(skill=call.name, arg=call.arg, outcome_class=outcome_class, reason=reason,
                                     location=location, from_location=start_location, productive=False))
            del self.history[:-8]
            self.last_failed = None
            return False
        if key(call) in self.seed_walk_targets:
            self.seed_looked.add(self.seed_walk_targets[key(call)])  # failures also retire this search stand
            if key(call) in self.seed_survey_targets:
                self.seed_surveyed.add(self.seed_walk_targets[key(call)])
        if call == S.SkillCall("pergi_ke", "seed shop") and (
                outcome_class in (CLEAN_FAIL, UNCERTAIN) or self.shop_retry_at is not None):
            self.shop_failures += 1
            self.shop_retry_at = (minutes(time_of_day) + SHOP_RETRY_MINUTES
                                  if outcome_class == CLEAN_FAIL and self.shop_failures < SHOP_FAILURES_MAX
                                  and type(time_of_day) is int else None)
        self.history.append(dict(skill=call.name, arg=call.arg, outcome_class=outcome_class,
                                 reason=reason, location=location, from_location=start_location,
                                 **({"n": call.n} if call.n is not None else {})))
        del self.history[:-8]
        if productive is None:
            if call.name == "langkah":
                productive = outcome_class == VERIFIED and reason == "observed_movement"
            else:
                productive = (outcome_class == VERIFIED and (bool(items) or
                    call.name not in ("pergi_ke", "jalan_ke", "tidur", "cek_pagi")
                    and not any(token in reason for token in ("_no_", "no_effect", "already_"))))
        self.history[-1]["productive"] = bool(productive)
        travel_loop = False
        if productive:
            self.travel_edges.clear()
            self.travel_loop = False
            self.productive_visit.add(location)
            self.empty_visits.pop(location, None)
        elif (call.name == "pergi_ke" and outcome_class == VERIFIED
              and start_location and location and start_location != location):
            if start_location not in self.productive_visit:
                self.empty_visits[start_location] = self.work_offers.get(start_location)
            self.productive_visit.discard(location)
            edge = (start_location, location)
            self.travel_edges[edge] += 1
            cycles = min(self.travel_edges[edge], self.travel_edges[(location, start_location)])
            farm_work = self.work_offers.get("Farm") or ()
            seeded_shuttle = ({start_location, location} == {"Farm", "SeedShop"}
                              and len(farm_work) > 2 and bool(farm_work[2]))
            home_shuttle = ("Farm" in edge and any(str(loc).startswith(("Cabin", "FarmHouse")) for loc in edge))
            travel_loop = cycles >= (1 if seeded_shuttle or home_shuttle else 3) and not self.travel_loop
            self.travel_loop |= travel_loop
        if route_refused:
            self.last_failed = None  # the walk already withheld this ref, not the whole skill or its waypoint.
            return False
        # The loop signature is structural; free-text reasons/targets vary per attempt.
        signature = (call.name, call.arg, location, outcome_class)
        if outcome_class in (CLEAN_FAIL, UNCERTAIN, NOT_READY):
            self.failures[signature] += 1
            # ADR 165/196: a native Mines ladder ACTION stays choosable after a failed press.
            self.last_failed = None if call.name == "aksi" and navigation_exit(call.arg) else key(call)
        else:
            self.last_failed = None
        if outcome_class in (CLEAN_FAIL, NOT_READY) and way_out_trip(call, location) and not home_call(call):
            # ADR 239: one clean failed trip out of a place is withheld only while its place, tile and clock hold
            # (the offer filter's one-step last_failed hide no longer applies to trips).
            from arti_runtime import way_out
            self.way_out_failures[(*key(call), location)] = way_out.bind(
                place=location, clock=time_of_day, tile=tile, reason=reason)
        if outcome_class == VERIFIED:
            self.idle_boundaries = 0
            self.home_blocked = None  # fix10e: after any verified row (she may stand elsewhere) home is re-planned
        count = self.failures[signature] if outcome_class in (CLEAN_FAIL, UNCERTAIN, NOT_READY) else 0
        if home_call(call) or (self.sleep_only and call.name == "pergi_ke"
                               and not str(location).startswith("UndergroundMine")):
            if outcome_class in (CLEAN_FAIL, UNCERTAIN, NOT_READY, PRECONDITION):
                self.last_failed = key(call)
                # Smoke 2026-10-24 D2 (J958-J1357): a noon `pergi_ke home` (AnimalShop, exit_not_offered) latched
                # sleep_only, every later hop counted as a home retry and the day ended at 14:10. Owner: a failed
                # trip is a fact, not a mode; only the bedtime reserve (home_due -> sleep_only) spends home retries.
                from arti_runtime.travel_time import reserve
                if self.sleep_only or reserve(location, time_of_day, self)["active"]:
                    self.home_failures += 1
                    self.sleep_only = True
                from arti_stardew_debris_route import blocker_needs
                needs = blocker_needs(reason) if home_call(call) and outcome_class == CLEAN_FAIL else None
                if needs is not None:
                    self.home_blocked = dict(location=location, blocker=str(reason).rpartition(":")[2], needs=needs,
                                             route="none", time_of_day=time_of_day)
            elif outcome_class == VERIFIED:
                self.home_failures = 0
                self.home_unreachable = None  # verified progress: the reserve's own retries apply again
                self.home_blocked = None
        # CLEAN_FAIL on an identified work target is final for this day. A
        # native refusal is not a reason to repeat a protected sweep after a trip.
        refused_target = (outcome_class == CLEAN_FAIL and call.name != "pergi_ke"
                          and not (call.name == "aksi" and navigation_exit(call.arg))
                          and (target is not None or call.name == "bersihkan" and call.arg is not None))
        if refused_target:
            if target and not navigation_exit(target):
                self.refs.add(target)
            if call.arg is not None:  # a target argument, never a whole arg-less skill
                self.quarantine.add((*key(call), location))
        no_effect_loop = outcome_class == CLEAN_FAIL and reason == "no_effect_progress"
        clean_cancel = call.name == "tidur" and outcome_class == CLEAN_FAIL and reason in (
            "sleep_readiness_cancelled", "sleep_not_started")
        if (outcome_class == UNCERTAIN or count >= 3 or no_effect_loop) and not clean_cancel:
            # Home: only a repeated daytime failure here (never one interrupted/uncertain row); blocked() lifts it
            # at the bedtime reserve.
            if (not home_call(call) or count >= 3 and not self.sleep_only) and not refused_target:
                if way_out_trip(call, location) and outcome_class != UNCERTAIN:
                    # ADR 239: a clean failed trip out of a place is a fact bound to where she stood
                    # and the clock, never a day-long wall. An uncertain trip keeps its fence.
                    from arti_runtime import way_out
                    self.way_out_failures[(*key(call), location)] = way_out.bind(
                        place=location, clock=time_of_day, tile=tile, reason=reason)
                else:
                    self.quarantine.add((*key(call), location))
            # PM-1 fix6 #1 (attempt 6: one refused home door -> exit_not_offered x29): a trip's target is its still-valid
            # exit or route stand; a failed trip is a fact, never a retired exit. Work targets keep their retirement.
            # fix10b #4 (a11 d3: cek_pagi mail's walk to the cabin warp ended UNCERTAIN -> warp retired -> 15x
            # exit_not_offered, day spent indoors): the same holds when another skill's navigation leg used the exit.
            if target and call.name != "pergi_ke" and not navigation_exit(target):
                self.refs.add(target)
        if outcome_class == UNCERTAIN:
            self.uncertain_items.update(items)
        return count == 3 or travel_loop or no_effect_loop


def _entries(context):
    return [e for e in context.get("extras", ()) if isinstance(e, Mapping)]


def forage_offers(snapshot, context, excluded=()):
    """Use S21/forage-on-way's native classifier even while the current goal is travel."""
    from arti_stardew_interact import forage_entries, InteractObjective, blocked_tiles
    from arti_runtime.collection_facts import pickup_refusals
    excluded = set(excluded) | pickup_refusals(context, context.get("bag_free_slots") == 0)
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    if here is None:
        return []
    targets = {ref: f for ref, f in context.get("targets", {}).items() if ref not in excluded}
    entries = forage_entries(targets, here, InteractObjective("forage_pickup"), blocked_tiles(targets))
    from arti_runtime.collection_facts import approach_refused
    entries += [e for e in S._g(snapshot, "approach_targets", ()) or ()
                if e.get("kind") == "forage" and e.get("ref") not in excluded
                and not approach_refused(context, e, context.get("bag_free_slots") == 0)]
    return list({e["ref"]: e for e in entries}.values())


def visible_forage(snapshot, context, excluded=()):
    """Scene objects beyond S21's local radius remain discoverable through observed open paths.

    No name/id allowlist: vanilla spawned/grabbable facts own admission. A sighting is a
    reason to approach, never evidence of pickup or permission to cross an unseen cell.
    """
    from arti_stardew_interact import forage_entries, InteractObjective, blocked_tiles, is_forage_fact
    from arti_runtime.collection_facts import pickup_refusals
    excluded = set(excluded) | pickup_refusals(context, context.get("bag_free_slots") == 0)
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    if here is None:
        return []
    targets = {r: f for r, f in context.get("targets", {}).items() if r not in excluded}
    blocked = blocked_tiles(targets)
    return [e for ref, f in targets.items() if is_forage_fact(f)
            for e in forage_entries({ref: f}, here, InteractObjective("forage_pickup"), blocked, keep_ref=ref)]


def seen_walk_offers(policy, location, here, route_refs=()):
    return policy.seen_objects.offers(location, here, refused=policy.refs.union(route_refs))


def refill_targets(snapshot, context):
    """Only native CanRefillWateringCanOnTile facts, with a reachable cardinal stand.

    Includes vanilla wells, water and eligible kitchen sink tiles without guessing from
    a location/building name. The existing tool verifier still owns the actual refill.
    """
    from arti_stardew_goal_routes import open_paths
    from arti_stardew_interact import blocked_tiles
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    window = context.get("local_window") or {}
    if here is None or S._wave1_xy(window.get("center")) != here:
        return []
    blocked = blocked_tiles(context.get("targets", {}))
    grid = {t: v for t, v in (context.get("nav_grid") or {}).items() if t not in blocked}
    parents = open_paths(grid, here)
    out = []
    # Refill sightings are in this same fresh, scoped scene fact. They propose
    # approach only; actual input still requires the adjacent native tool offer.
    sources = [dict(f, water_refill=True, villager=False) for f in window.get("refill_sources", ())]
    for f in [*window.get("tiles", ()), *sources]:
        tile = S._wave1_xy(f)
        if tile is None or f.get("water_refill") is not True or f.get("villager") is not False:
            continue
        stands = [t for dx, dy in S._FACING if (t := (tile[0] + dx, tile[1] + dy)) == here or t in parents]
        if stands:
            out.append((tile, stands))
    return out


def refill_sightings(snapshot, context):
    """fix42: native refill tiles in the same fresh, scoped window as refill_targets, reachable stand or not."""
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    window = context.get("local_window") or {}
    if here is None or S._wave1_xy(window.get("center")) != here:
        return []
    tiles = [S._wave1_xy(f) for f in window.get("refill_sources", ())]
    tiles += [S._wave1_xy(f) for f in window.get("tiles", ()) if f.get("water_refill") is True]
    return sorted({t for t in tiles if t is not None}, key=lambda t: abs(t[0] - here[0]) + abs(t[1] - here[1]))


def till_targets(snapshot, context):
    """S9 only works adjacent ground; match its current native window exactly."""
    from arti_stardew_tool_work import _tile_entry, _facts_on
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    window = context.get("local_window") or {}
    if here is None or S._wave1_xy(window.get("center")) != here:
        return []
    out = []
    for dx, dy in S._FACING:
        tile = (here[0] + dx, here[1] + dy)
        entry = _tile_entry(window, *tile)
        if (entry is not None and entry.get("tillable") is True and entry.get("villager") is False
                and not _facts_on(window, *tile)):
            out.append(tile)
    return out


def seed_ground_path(snapshot, context, policy, *, hoe_owned):
    """Approach soil sightings or inspect open Farm stands, never authorize remote planting.

    M6 D2 J1555/J1694: the cabin's local window admitted neither crop skill while
    bare HoeDirt was reachable farther away. Keep the adjacent till and fresh-soil
    gates intact; use the existing model-chosen walk to get a new local read first.
    """
    from arti_stardew_goal_routes import open_paths, open_path
    from arti_stardew_interact import blocked_tiles
    from arti_stardew_tool_work import _facts_on, _has_crop
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    window = context.get("local_window") or {}
    if here is None or S._wave1_xy(window.get("center")) != here:
        return None
    policy.seed_probed.add(here)
    if len(policy.seed_looked) >= SEED_LOOK_STANDS:
        return None
    targets = context.get("targets", {})
    blocked = blocked_tiles(targets)
    parents = open_paths({t: v for t, v in (context.get("nav_grid") or {}).items() if t not in blocked}, here)
    if not parents:
        return None
    # Own hoe receipts / verified planting lead; unknown occupied crops are never
    # search hints. Unknown bare soil is a fallback, since vanilla gives it no owner.
    # All sightings guide travel only; planting_soil still owns fresh admission.
    own_crops = [tile for ref, f in targets.items()
                 if (tile := S._wave1_xy(f.get("tile"))) is not None and f.get("terrain_type") == "HoeDirt"
                 and (tile in policy.seed_own_soil or ref in policy.crop_memory.crops)
                 and (_has_crop(f) or ref in policy.crop_memory.crops)]
    own_looks = sum(any(max(abs(a-b) for a, b in zip(tile, old)) <= 1 for tile in own_crops)
                    for old in policy.seed_probed | policy.seed_looked)
    hints = []
    for ref, f in targets.items():
        tile = S._wave1_xy(f.get("tile"))
        if (ref in policy.refs or tile is None or f.get("terrain_type") != "HoeDirt"
                or not isinstance(f.get("crop"), Mapping)):
            continue
        own = tile in policy.seed_own_soil or ref in policy.crop_memory.crops
        occupied = _has_crop(f) or ref in policy.crop_memory.crops
        # Occupied own crops justify two local looks, not every stand around
        # the patch. Native bare / tillable hints survive this advisory bound.
        if own and (not occupied or hoe_owned and own_looks < SEED_OWN_LOOKS):
            hints.append((2 if occupied else 0, tile))
        elif not occupied:
            hints.append((3, tile))
    if hoe_owned:
        hints += [(1, tile) for f in window.get("tiles", ())
                  if (tile := S._wave1_xy(f)) is not None and f.get("tillable") is True
                  and f.get("villager") is False and not _facts_on(window, *tile)]
    choices = []
    for rank, tile in hints:
        if tile is None:
            continue
        for dx, dy in S._FACING:
            stand = (tile[0] + dx, tile[1] + dy)
            if stand != here and stand not in policy.seed_looked and (path := open_path(parents, stand)):
                leg = path[:9]
                if leg[-1] != here and leg[-1] not in policy.seed_looked:
                    choices.append((rank, len(path), stand, leg))
    survey = not choices
    if survey and hoe_owned and len(policy.seed_surveyed) < SEED_SURVEY_PROBES:
        # Diggable is only available in the 3x3 probe. Search observed-open cells
        # beyond prior 3x3 looks, preferring the full eight-step leg. Four chosen
        # probes bound exploration even when every native read remains non-Diggable.
        probed = policy.seed_probed | policy.seed_looked
        for stand in parents:
            path = open_path(parents, stand)
            leg = path[:9]
            if all(max(abs(a-b) for a, b in zip(leg[-1], old)) >= 3 for old in probed):
                choices.append((4, -len(leg), leg[-1], leg))
    if not choices:
        return None
    path = min(choices)[3]
    if survey:
        policy.seed_survey_targets.add(("jalan_ke", f"{path[-1][0]},{path[-1][1]}"))
    return path  # existing walk: at most eight native-open steps


EXIT_CLEARANCE = 2  # Chebyshev tiles kept free around a native exit (its arrival/front tile and sides)


MENU_DOES = "Buka menu game-mu: tas (angkat, taruh, jatuhkan, buang), crafting, tab lain."  # PM-1 fix13b: short; the page itself has the facts
CRAFT_DOES = "Buka halaman crafting; resepnya kamu pilih di sana."


def craftable_recipes(context):
    return _craftable_recipes(dict(mines_ui=dict(crafting_known=(context.get('mines_ui') or {}).get('crafting_known'))))


def _craftable_recipes(context):
    """PM-1 fix13b: native crafting_known rows (CraftingFacts.Known), the game's own craft-now verdict. Facts only."""
    block = (context.get("mines_ui") or {}).get("crafting_known") or {}
    return [r for r in native_rows(block.get("craftable")) if isinstance(r, Mapping) and isinstance(r.get("recipe"), str)
            and r["recipe"] and isinstance(r.get("needs"), list)]


def chest_tiles(snapshot, context):
    """Untilled native-tillable ground only, never hoed dirt, an exit approach or a path cut.

    ``till_targets`` is the native Tillable predicate (Diggable, no object/terrain feature,
    passable), so paths, floors and prepared soil are already excluded. Exit tiles come from
    the current native warp/door facts; the cut check uses the current native open grid when
    it covers ARTI's tile. No coordinates are assumed.
    """
    from arti_stardew_goal_routes import open_paths
    from arti_stardew_interact import blocked_tiles
    tiles = till_targets(snapshot, context)
    if not tiles:
        return []
    targets = context.get("targets", {})
    exits = {t for f in targets.values() if isinstance(f, Mapping)
             and f.get("kind") in ("warp", "door", "building_door")
             for t in (S._wave1_xy(f.get("tile")), S._wave1_xy(f.get("source_tile"))) if t is not None}
    tiles = [t for t in tiles if not any(max(abs(t[0] - e[0]), abs(t[1] - e[1])) <= EXIT_CLEARANCE
                                         for e in exits)]
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    blocked = blocked_tiles(targets)
    grid = {t: v for t, v in (context.get("nav_grid") or {}).items() if t not in blocked}
    reach = open_paths(grid, here) if here is not None else {}
    if not reach:
        return tiles  # no current grid here: the native place gate still owns the tile
    out = []
    for tile in tiles:
        if tile in reach:
            # The chest's own cell is the only cell it may remove from ARTI's reachable area.
            cut = open_paths({t: v for t, v in grid.items() if t != tile}, here)
            if len(cut) != len(reach) - 1:
                continue
        out.append(tile)
    return out


def forage_look_paths(snapshot, context):
    """Only fresh native open cells, excluding exits; no unseen map destination."""
    from arti_stardew_goal_routes import open_paths
    from arti_stardew_interact import blocked_tiles
    motor = S._g(snapshot, "motor")
    here = S._wave1_xy(S._g(motor, "tile"))
    outdoors = ((context.get("mines_ui") or {}).get("goal_route") or {}).get("outdoors")
    if outdoors is None:
        outdoors = next((f.get("is_outdoors") for f in context.get("targets", {}).values()
                         if f.get("kind") == "location"), None)
    from arti_stardew_world import places
    forage_data = any(p["id"] == S._g(motor, "location") and any(str(f).startswith("forage:") for f in p.get("facts", ()))
                      for p in places(context))
    if (here is None or outdoors is False and not forage_data
            or S._g(motor, "status") != "current" or S._g(motor, "is_player_free") is not True
            or S._g(motor, "can_player_move") is not True or S._g(motor, "active_menu") is not None
            or not context.get("ui_complete") or context.get("location_id") != S._g(motor, "location")):
        return {}
    blocked = blocked_tiles(context.get("targets", {}))
    grid = {t: value for t, value in (context.get("nav_grid") or {}).items() if t not in blocked}
    return open_paths(grid, here)


def crop_waypoint(policy, ref, crop, context, here):
    """Use the shared rolling-window route planner; remembered crops authorize no cells."""
    from arti_stardew_goal_routes import route_waypoint
    x, y = crop["tile"]
    stands = [(x+dx, y+dy) for dx, dy in S._FACING]
    return route_waypoint(context.get("nav_grid"), here, stands, policy.crop_routes.setdefault(ref, {}))


def bin_known(context) -> bool:
    """The mod's ``shipping_bins`` (any distance in this location); the ship goal walks to it (FIXB34). The bin being
    off screen does not make shipping unavailable."""
    from arti_stardew_economy import bin_entries
    return bool(bin_entries(context.get("mines_ui")))


def bin_approachable(context, policy, grid, origin, kinds=()) -> bool:
    """Whether ``kirim`` is a live offer: a bin ARTI can reach now, or after the bounded natural-debris repair.

    Smoke 2026-10-24 (c2 d6-d11): ``bin_known`` offered kirim all day while the bin at 71,14 was walled off from
    the crop patch (bin_not_offered 12x on d11). Rules, all observations: the mod's approach catalog lists the bin
    (reachable now); otherwise a published bin whose ref was not refused today. Its stand must be inside the
    observed nav window, open
    from here or openable by clearing stone/weeds/twig only (tree_cap 0, same planner as the trip repair); a stand
    outside the window has unknown reachability and is not offered."""
    if context.get("stardew_live7_ship_on_the_way") is True:
        from arti_runtime.ship_pause import confirmed_bin_approachable
        return confirmed_bin_approachable(context, policy, grid, origin)
    if "shipping_bin" in kinds:
        return True
    from arti_stardew_economy import bin_entries
    entries = [e for e in bin_entries(context.get("mines_ui")) if e["ref"] not in policy.refs]
    if not entries:
        return False
    stands_known = bool(grid) and origin is not None and grid.get(origin) == "."
    reach = None
    if stands_known:
        from arti_stardew_goal_routes import open_paths
        reach = open_paths(grid, origin)
    for entry in entries:
        stand = tuple(entry["reach_tiles"][0])
        if getattr(policy, "stardew_live7_ship_on_the_way", False) is True and stands_known and stand not in grid:
            # ADR 231 part A (arti_runtime.ship_pause.box_calls): beyond the observed window is unknown, not blocked.
            return True
        if not stands_known or stand not in grid:
            continue
        if stand in reach:
            return True
        from arti_stardew_debris_route import blocking_path
        tools = [f.get("qualified_item_id") for f in context.get("inventory", ()) if isinstance(f, Mapping)]
        plan = blocking_path(grid, origin, [stand], context.get("targets", {}), tools, tree_cap=0)
        if plan and plan.get("target") and not plan.get("blocker"):
            return True
    return False


def day_plan(date, weekday, weather, facts, context, *, policy=None, snapshot=None, item_policy=None):
    """fix10e: where the way home is proven blocked (``policy.home_blocked_here``), the bedtime reserve does not
    narrow her offer to that way home; she gets her ordinary offer without it, and the block is a fact."""
    policy = policy or DayPolicy()
    policy.harvest_exclusions = []
    reserve = policy.sleep_only
    home_blocked = policy.home_blocked_here(facts.location) is not None
    if home_blocked:
        policy.sleep_only = False
    try:
        calls = _day_plan(date, weekday, weather, facts, context, policy=policy, snapshot=snapshot,
                          item_policy=item_policy)
    finally:
        if home_blocked:
            policy.sleep_only = reserve
    if home_blocked:
        calls = [c for c in calls if not home_call(c)]
    calls = prefer_paused_destination(calls, policy, facts.location)
    from arti_runtime.harvest_facts import describe
    policy.harvest_fact = describe(facts, context, calls, policy.harvest_exclusions)
    return calls


def at_home_base(facts, context):
    from arti_stardew_world import at_own_home
    owned = at_own_home(context)
    return (facts.location == "Farm" or owned is True
            or owned is None and str(facts.location or "").startswith(("Cabin", "FarmHouse")))


def _day_plan(date, weekday, weather, facts, context, *, policy=None, snapshot=None, item_policy=None):
    """Current native preconditions only; date/weekday are observations, never a day-1 script.

    Complex skills are offered only when their current native interface exposes
    the required target/recipe/order. Missing unlocks are absence, not failure.
    """
    from arti_stardew_economy import ItemPolicy
    from arti_stardew_interact import _LOCATION_ALIASES
    policy = policy or DayPolicy()
    item_policy = item_policy or ItemPolicy()
    # Record every applicable gate, including branches which return before add().
    harvest = S.SkillCall("panen")
    exclusions = policy.harvest_exclusions
    if not facts.inventory_current or not context.get("inventory_complete"):
        exclusions.append("inventory_not_current_or_complete")
    if not context.get("ui_complete"):
        exclusions.append("ui_not_complete")
    native_harvest = [e for e in _entries(context) if e.get("kind") == "harvest_crop"]
    if facts.free_slots == 0 and native_harvest and not any(e.get("inventory_accepts") is True for e in native_harvest):
        exclusions.append("inventory_cannot_accept_or_unread")
    if not native_harvest:
        exclusions.append("native_harvest_target_not_offered")
    elif not any(e.get("ref") not in policy.refs for e in native_harvest):
        exclusions.append("native_harvest_targets_quarantined")
    exclusions.extend("precondition:" + str(r) for r in S.precondition_failures(harvest, facts))
    if policy.blocked(harvest, facts.location):
        exclusions.append("skill_quarantined")
    if policy.last_failed == key(harvest):
        exclusions.append("last_failed")
    if reason := admission(harvest, facts):
        if policy.home_blocked_here(facts.location) is None:
            exclusions.append("admission:" + str(reason))
    if policy.sleep_only:
        exclusions.append("sleep_reserve")
    if not facts.player_free or facts.menu_open:
        exclusions.append("player_not_free_or_menu_open")
    if str(facts.location or "").startswith("UndergroundMine"):
        exclusions.append("mine_exit_branch")
    if ((facts.health is not None and facts.health < 30)
            or (facts.energy is not None and facts.energy < VERY_LOW_ENERGY)):
        exclusions.append("low_vitals_branch")
    if context.get("ui_complete"):
        policy.remember_quest_refusals(context)
    if not facts.inventory_current or not context.get("ui_complete") or not context.get("inventory_complete"):
        return []
    from arti_runtime.travel_time import observe
    observe(policy, facts.location, context)
    context = dict(context, quest_refused_items=policy.quest_refused_items,
                   shop_refused_items=policy.shop_refused.get(facts.location, set()),
                   excluded_scene_pickups=policy.refs)
    # Dependent work cannot consume an uncertain transaction's item deltas.
    # This view narrows the existing facts; it does not create a world store.
    if policy.uncertain_items:
        context = dict(context, inventory=[f for f in context.get("inventory", ())
                       if f.get("qualified_item_id") not in policy.uncertain_items])
        facts = replace(facts, inventory={q: n for q, n in facts.inventory.items() if q not in policy.uncertain_items},
                        toolbar=tuple(q for q in facts.toolbar if q not in policy.uncertain_items),
                        equipped=None if facts.equipped in policy.uncertain_items else facts.equipped,
                        edible=False if policy.uncertain_items else facts.edible)
    from arti_runtime.collection_facts import harvest_refused
    # pkg77: each harvest row is judged on its own fact; a refused one never takes an unrelated row with it.
    entries = [e for e in _entries(context) if e.get("ref") not in policy.refs
               and not harvest_refused(e, facts.free_slots == 0)]
    targets = {ref: f for ref, f in context.get("targets", {}).items() if ref not in policy.refs}
    ui = context.get("mines_ui") or {}
    kinds = {e.get("kind") for e in entries}
    stackable = stackable_seeds(facts, context)
    from arti_runtime.collection_facts import pickup_refusals
    pickup_blocked = pickup_refusals(context, facts.free_slots == 0)
    candidates = []
    from arti_runtime.room_facts import destinations as room_destinations
    room_trips = room_destinations(context, facts, policy)
    if snapshot is not None and facts.location and (sighted := refill_sightings(snapshot, context)):
        policy.water_seen[facts.location] = list(sighted[0])  # fix42: a native fact, recorded before any branch

    policy.time_withheld = {}

    def add(name, arg=None, *, n=None, tile=None, reserve_path=False):
        from arti_runtime.travel_time import admission_budget
        candidate = S.SkillCall(name, arg, n=n, tile=tile)
        if (not reserve_path and policy.home_blocked_here(facts.location) is None
                and admission(candidate, facts, policy) == "skipped_time:home_reserve"):
            policy.time_withheld[name + (" " + str(arg) if arg else "")] = admission_budget(candidate, facts, policy)
        call = S.SkillCall(name, arg, n=n, tile=tile)
        # ADR 137: native item/skill preflight owns acceptance. Tools which leave drops on the
        # ground, stack merges, and native receipts need no empty slot.
        if name == "pergi_ke" and arg == "seed shop":
            if (policy.shop_failures and not policy.shop_retry_ready(facts.time_of_day)):
                return  # retain the existing bounded retry policy independently of map knowledge
            if context.get("world_knowledge") is None and (
                    not shop_trip_allowed(facts, weekday, date, context, policy.shop_memory) or not (
                        _requirement("money", facts, context, weather, date, item_policy)
                        or disposable(context, item_policy, "sell"))):
                return  # direct exits must not bypass the budget or vendor-hours gate
        if (name in NAMES and not S.precondition_failures(call, facts)
                and (_plain_talk(call) or not policy.blocked(call, facts.location, facts, context.get("actor_tile")))
                and arg not in policy.uncertain_items and (reserve_path or admission(call, facts, policy) is None
                                                          # fix10e: no home reserve when no way home exists
                                                          or policy.home_blocked_here(facts.location) is not None)):
            candidates.append(call)

    from arti_stardew_world import at_own_home
    home = at_own_home(context)  # fix42: native ownership first; the name prefix also matched the host's house
    if home is None:
        home = str(facts.location or "").startswith(("Cabin", "FarmHouse")) or (
            ((context.get("world_knowledge") or {}).get("world_graph") or {}).get("home") == facts.location)
    inside = str(facts.location or "").startswith("UndergroundMine")
    # Visiting another map ends the old floor stay; fresh reads/goals on this floor do not.
    for floor in list(policy.mines_exit_recovery):
        if not inside or floor != facts.location:
            del policy.mines_exit_recovery[floor]
            if policy.help_basis and policy.help_basis[0] == "mines_exit_attempts_exhausted:" + floor:
                policy.help_basis = None  # another visit may need its own help request
    policy.floor_retry.intersection_update({facts.location} if inside else set())
    low_hp = facts.health is not None and facts.health < LOW_HP
    if inside:
        from arti_runtime.mines_rules import energy_due
        from arti_runtime.mines_survival import escape_offered, meal
        from arti_stardew_skills_mines import survival_low
        combat = context.get("combat") or {}
        low_hp = low_hp or survival_low(combat) or (
            type(combat.get("hp")) is int and combat["hp"] < LOW_HP)
        low_energy = energy_due(facts.energy)
        # Survival food is never refused by the clock reserve on a floor.
        if facts.edible and (low_hp or low_energy):
            if meal(context) and not S.precondition_failures(S.SkillCall("makan"), facts):
                candidates.append(S.SkillCall("makan"))  # a fresh safe survival meal bypasses work quarantine too
            else:
                add("makan", reserve_path=True)
        if facts.player_free and not facts.menu_open and escape_offered(context, facts.location):
            # Escape combat is part of leaving, not another mining trip. Its
            # native admission and each settled batch still own permission.
            from arti_stardew_combat import is_weapon
            if any(is_weapon(q) and n for q, n in facts.inventory.items()):
                candidates.append(S.SkillCall("lawan_mine"))
        # ADR 146/156: every UndergroundMine* floor keeps descent/fight under the existing reserve.
        if (type(combat.get("hp")) is int and type(combat.get("max_hp")) is int
                and combat["max_hp"] > 0 and combat["hp"] >= LOW_HP
                and not low_hp and not low_energy and not survival_low(combat) and not policy.sleep_only):
            from arti_stardew_generic_world import calls as generic_calls
            if snapshot is not None:
                candidates.extend(generic_calls(snapshot, context))
            add("pecah_batu")
            add("pungut_drop")
            from arti_stardew_combat import is_weapon
            if S._has(facts, *S.PICKAXES) and any(is_weapon(q) and n for q, n in facts.inventory.items()):
                add("lawan_mine")
            if (context.get("mines_ui") or {}).get("mine_ore_nodes") and S._has(facts, *S.PICKAXES):
                add("tambang_bijih")
        # Inter-map travel still starts with the native floor exit.
        # ADR 239: a remembered failure withholds her trips out only while its facts and clock hold.
        from arti_runtime.mines_exit import closed_now
        exit_blocker = closed_now(policy, context, facts.location)
        # ADR 165: a fresh native ACTION remains her choice after automatic
        # recovery is spent. Its own route/HP/press checks still apply.
        from arti_runtime.mines_exit import ups
        from arti_runtime.mines_down import downs, note_depth
        note_depth(policy, facts.location)
        if facts.player_free and not facts.menu_open:
            for e in ups(context, facts.location):
                candidates.append(S.SkillCall("aksi", e["ref"], tile=(e["x"], e["y"])))
            # ADR 196: a listed way down is its own choice, same startability as up.
            # HP/energy/sleep_only still withhold descent (the existing reserve); that is safety.
            if not low_hp and not low_energy and not survival_low(combat) and not policy.sleep_only:
                for e in downs(context, facts.location):
                    candidates.append(S.SkillCall("aksi", e["ref"], tile=(e["x"], e["y"])))
        if not exit_blocker:
            candidates.append(S.SkillCall("pergi_ke", "mine"))
            # ADR 194: home starts with the same native ladder/Leave controller,
            # then resumes navigation from its independently verified arrival.
            if ups(context, facts.location) and facts.player_free and not facts.menu_open:
                candidates.append(S.SkillCall("pergi_ke", "home"))
                candidates.extend(S.SkillCall("pergi_ke", o["arg"]) for o in room_trips)
        if snapshot is not None:
            # Even at low HP/return time the native inventory can expose food or
            # an escape item. Opening it grants no use/discard permission.
            from arti_stardew_generic_world import menu_key_published
            if menu_key_published(snapshot, context):
                candidates.append(S.SkillCall("inventori"))
        # ADR 160: failed exit geometry is a fact, not a day-long quarantine.
        # Changed native geometry can offer the exit again; meals keep their gate.
        return list(dict.fromkeys(candidates))
    native_bed = any(f.get("kind") == "bed" and f.get("owned_by_actor") is True and f.get("entry_tiles")
                     for f in targets.values())
    if home or native_bed:
        # Preference belongs in the prompt; native preconditions still own the bed.
        add("tidur")
    if not home:
        if context.get("world_knowledge") is not None:
            from arti_stardew_world import route
            graph = context["world_knowledge"].get("world_graph") or {}
            home_route = route(context, graph.get("home"))
            from arti_stardew_event_control import festival_free_roam
            if not home_route and festival_free_roam(S._g(snapshot, "event_movement")):
                # fix63A (ADR 94, a45 D20:J786): the native graph has no edges from Temp, so she had no way to choose
                # leaving; `home` only appeared on ticks without a fresh catalog (J774, J1139). Her choice, always offered.
                add("pergi_ke", "home", reserve_path=True)
            if home_route:
                add("pergi_ke", "home", reserve_path=True)
                if policy.sleep_only or policy.last_failed == ("pergi_ke", "home"):
                    from arti_stardew_world import location_arg
                    hop = home_route[0]
                    observed = any(f.get("target_location") == hop and f.get("kind") in ("warp", "door", "building_door")
                                   and f.get("enterable") is not False and f.get("npc_only") is not True
                                   for f in targets.values())
                    if hop != graph.get("home") and observed:
                        add("pergi_ke", location_arg(hop), reserve_path=True)
        else:
            from arti_stardew_goal_routes import next_hop
            from arti_stardew_skills_attempts import _hop, INTERIOR_PARENT
            exits = {f.get("target_location") for f in targets.values()
                     if f.get("kind") in ("warp", "door", "building_door")
                     and f.get("enterable") is not False and f.get("npc_only") is not True}
            hop = _hop(facts.location, "Farm") or INTERIOR_PARENT.get(facts.location)
            # Smoke 2026-10-24 D2 J958-J985: from AnimalShop (only a Forest exit) the HOME leg admits only a Farm exit
            # or the itinerary hop (interact.exit_offered / goal_routes live_home), so `pergi_ke home` failed with
            # exit_not_offered and 0 inputs. Offer home where a current native exit can start it; elsewhere the
            # observed homeward hop is the way home (never an invented warp).
            home_leg = (facts.location == "Farm" or "Farm" in exits
                        or next_hop(facts.location, "Farm") in exits - {None})
            if hop is None and not home_leg:
                # Review cand-g 2026-10-24: no shipped hop and no home leg (FishShop, Saloon, JojaMart, houses...) -> the
                # observed exit. Never where `pergi_ke home` can start: on the Farm (sim 22:00) it chose Backwoods over home.
                hop = observed_homeward_exit(facts.location, exits)
            hop_arg = next((a for a, loc in _LOCATION_ALIASES.items() if loc == hop), None) if hop in exits else None
            if home_leg or not hop_arg:
                add("pergi_ke", "home")
            # Home reserve (or a refused direct objective) selects the shipped hop,
            # admitted only by a CURRENT native exit. It never invents a warp.
            if hop_arg and (policy.sleep_only or policy.last_failed == ("pergi_ke", "home") or not home_leg):
                add("pergi_ke", hop_arg, reserve_path=True)
    observe_hp(policy, context.get("combat"), facts.health, facts.time_of_day)
    danger = threat(context.get("combat"), facts.health, damaged_at=policy.damaged_at, now=facts.time_of_day)
    near = S.lawan_near(context.get("combat"))
    fight = (danger is not None and not home and not policy.sleep_only and bool(near)
             and defend_refusal(context.get("combat"), facts.inventory, danger, facts.location) is None)
    if fight and policy.lawan_hold is not None:
        # Review r1 #4: after a lawan that found nothing to fight, no re-offer until a new hit or a new monster in range.
        hold = policy.lawan_hold
        if ((type(policy.damaged_at) is int and policy.damaged_at != hold.get("damaged_at"))
                or {m.get("id") for m in near} - set(hold.get("ids") or ())):
            policy.lawan_hold = None
        else:
            fight = False
    if danger is not None and danger["level"] != "ok" and not home:
        exclusions.append("danger_retreat_branch")
        # Smoke #12: attacked below the retreat line (60%). The way home (its hop) leads, then bed and, with no
        # monster in contact, food: no work, no other travel (the Mines keep their own rules). Owner 2026-10-23:
        # above the critical line (40%, the mod reflex floor) `lawan` stays her choice after the way home, and the
        # safety reflex (reflex_due) still answers an attacker at 40-60% HP; at/below 40% neither (home only).
        if facts.edible and not danger["contact"]:
            add("makan", reserve_path=True)
        if fight:
            add("lawan")
        safe = [c for c in candidates if c.name in ("makan", "tidur", "lawan") or home_call(c)
                or c.name == "pergi_ke" and (policy.sleep_only or policy.last_failed == ("pergi_ke", "home"))]
        return sorted(safe, key=lambda c: (not home_call(c), c.name != "makan"))
    if facts.player_free and not facts.menu_open:
        # ADR 186: returning and departing are opportunities even with room left.
        # These local menu/storage choices do not spend a travel/home reserve.
        from arti_stardew_generic_world import calls as native_calls
        from scripts.live7.interactions import ordinary_targets
        facilities = {r["ref"] for r in ordinary_targets(context)
                      if r["kind"] in ("chest", "shipping_bin", "shop_counter")}
        for room in native_calls(snapshot, context) if snapshot is not None else ():
            if room.name == "inventori" or room.name == "aksi" and room.arg in facilities:
                add(room.name, room.arg, tile=room.tile, reserve_path=True)
        if getattr(policy, "stardew_live7_ship_on_the_way", False) is True:
            from arti_runtime import ship_pause  # ADR 231 part A: the box beside the chest and its own aksi row
            for arg, n in ship_pause.box_calls(context, policy, snapshot, kinds, item_policy):
                add("kirim", arg, n=n, reserve_path=True)
        for qid, quality, count in facts.store_stacks if facts.chests else ():
            if qid not in policy.uncertain_items:
                call = S.SkillCall("simpan_ke_chest", qid, count, quality=quality)
                if not S.precondition_failures(call, facts) and not policy.blocked(call, facts.location):
                    candidates.append(call)
    if facts.player_free and not facts.menu_open:
        for option in room_trips:
            add("pergi_ke", option["arg"], reserve_path=True)
    if policy.sleep_only:
        if facts.edible and low_hp:
            add("makan", reserve_path=True)
        # Prefer native bed, then a concrete homeward hop, then the exact home
        # door. This bounds each goal independently (Mine->home can exceed the
        # ordinary three-transition objective allowance).
        candidates.sort(key=lambda c: (c.name != "tidur", home_call(c)))
        return [c for c in candidates if home_call(c) or key(c) != policy.last_failed]
    if not facts.player_free or facts.menu_open:
        return []
    if fight:
        add("lawan")  # owner 2026-10-23: monsters near and she can fight back; ordinary order (choice_priority)
    if facts.edible:  # FIX71C (a52: Parsnip x10 on a full bag, `makan` offered 0): eating is hers at any energy
        add("makan")
    if ((facts.health is not None and facts.health < 30)
            or (facts.energy is not None and facts.energy < VERY_LOW_ENERGY)):
        return [c for c in candidates if key(c) != policy.last_failed]
    from arti_stardew_economy import sentimental_facts
    keepsakes = {r["qualified_item_id"] for r in goods_facts(context) if sentimental_facts(r)}
    if facts.chests and facts.free_slots is not None:
        for qid, quality, count in facts.store_stacks:
            if qid not in policy.uncertain_items and (facts.free_slots <= 2 or qid in keepsakes):
                call = S.SkillCall("simpan_ke_chest", qid, count, quality=quality)
                if not policy.blocked(call, facts.location) and admission(call, facts, policy) is None:
                    candidates.append(call)
    from arti_stardew_tool_work import crop_needs_water
    dry = sum(crop_needs_water(f) for f in targets.values())
    if weather not in BAD_WEATHER and dry:
        facts = replace(facts, dry_soil=max(facts.dry_soil, dry))  # same fresh native targets, beyond coarse view radius
        add("siram")
    # Remembered tiles only propose travel. Each short leg uses the current native grid;
    # no remembered crop is inserted into targets or used to authorize a watering input.
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    policy.crop_walk_targets = {}
    trips = []
    if weather not in BAD_WEATHER and not policy.blocked(S.SkillCall("siram"), facts.location):
        trips += [(ref, crop) for ref, crop in context.get("remembered_crops", {}).items()
                  if not (ref in targets and crop_needs_water(targets[ref]))]
    # ADR 45: a crop she planted that last read ready is the same kind of trip in any weather; once the
    # fresh scene shows it harvestable, panen itself is on offer and the walk is not repeated.
    trips += [(ref, crop) for ref, crop in context.get("ready_crops", {}).items()
              if not (ref in targets and isinstance(targets[ref].get("crop"), Mapping)
                      and targets[ref]["crop"].get("ready_for_harvest") is True)]
    if trips and snapshot is not None:
        if here is not None:
            for ref, crop in trips:
                if crop["location"] != facts.location or ref in policy.refs:
                    continue
                # Same existing helper adopted by M4's home-route fix; no second route algorithm.
                tile = crop_waypoint(policy, ref, crop, context, here)
                if tile is not None:
                    add("jalan_ke", f"{tile[0]},{tile[1]}", tile=tile)
                    policy.crop_walk_targets.setdefault(("jalan_ke", f"{tile[0]},{tile[1]}"), ref)
    if refill_targets(snapshot, context):
        add("isi_gembor")
    elif snapshot is not None and facts.can_water is not None and facts.can_water[0] == 0:
        seen = policy.water_seen.get(facts.location)
        if here is not None and seen is not None:
            # Same rolling-window planner as remembered crops; the sighting authorizes no cell or refill input.
            from arti_stardew_goal_routes import route_waypoint
            stands = [(seen[0] + dx, seen[1] + dy) for dx, dy in S._FACING]
            tile = route_waypoint(context.get("nav_grid"), here, stands,
                                  policy.crop_routes.setdefault("water:" + str(facts.location), {}))
            if tile is not None:
                add("jalan_ke", f"{tile[0]},{tile[1]}", tile=tile)
    if snapshot is not None and (left := policy.ground_items.here(facts.location)):
        # fix59A (ADR 86): an item she left on the ground here is an ordinary walk target (stepping beside it lets the
        # game pull it into a free slot). Same open-path planner as every other jalan_ke; never a steer.
        if here is not None:
            from arti_stardew_goal_routes import open_paths, open_path
            parents = open_paths(context.get("nav_grid") or {}, here)
            for row in left[:2]:
                x, y = (row.get("seen") or {}).get("tile") or row["tile"]
                paths = [p for p in (open_path(parents, t) for t in ((x, y), (x, y + 1), (x - 1, y), (x, y - 1), (x + 1, y))
                                     if t in parents) if p and len(p) > 1]
                if paths:
                    tile = tuple(min(paths, key=lambda p: (tuple(p[-1]) != (x, y), len(p), p[-1]))[-1])  # onto it first
                    add("jalan_ke", "%d,%d" % tile, tile=tile)
    if snapshot is not None:
        # FIX77C (ADR 136): something she has seen on this map, at any distance. The arg is the object's own tile, read from a dated sighting.
        for row in seen_walk_offers(policy, facts.location, here, context.get("route_refusals", ())):
            add("jalan_ke", "%d,%d" % tuple(row["tile"]), tile=tuple(row["tile"]))
    add("tebang")
    add("pecah_batu")
    for arg in S.arg_choices(S.REGISTRY["pungut_drop"], facts):
        add("pungut_drop", arg)
    if till_targets(snapshot, context):
        add("cangkul")
    if planting_soil(dict(context, targets=targets)) and seasonal_seeds(date, facts, context):
        add("tanam")
    policy.seed_walk_targets = {}
    policy.seed_survey_targets = set()
    if (facts.location == "Farm" and seasonal_seeds(date, facts, context)
            and not S.precondition_failures(S.SkillCall("tanam"), facts)
            and not policy.blocked(S.SkillCall("tanam"), facts.location)
            and not any(c.name in ("cangkul", "tanam", "siram", "jalan_ke") and not sighting_walk(c, policy, facts.location)
                        for c in candidates)):
        path = seed_ground_path(snapshot, dict(context, targets=targets), policy,
                                hoe_owned=not S.precondition_failures(S.SkillCall("cangkul"), facts)
                                          and not policy.blocked(S.SkillCall("cangkul"), facts.location))
        if path and len(path) > 1:
            tile = path[-1]
            add("jalan_ke", f"{tile[0]},{tile[1]}", tile=tile)
            if any(c.name == "jalan_ke" and c.tile == tile for c in candidates):
                policy.seed_walk_targets[("jalan_ke", f"{tile[0]},{tile[1]}")] = tile
    context = dict(context, planting_walk_pending=bool(policy.seed_walk_targets))
    if "harvest_crop" in kinds:
        add("panen")
    if facts.pet_available:
        add("elus", "peliharaan")
    if kinds & {"animal_pet", "animal_milk", "animal_shear", "hay_hopper", "hay_place"}:
        add("rawat_hewan")
    forage_excluded = policy.refs.union(pickup_blocked, context.get("route_refusals", ()))
    from arti_runtime.collection_facts import pickup_accepted
    accepted = pickup_accepted(context)

    def forage_row(e):
        # ADR 137 (pkg77): on a full bag a row counts only with its own native acceptance; a row the native fit list
        # does not name is unknown, so refused, like a scene target without one (pickup_refusals, approach_refused).
        return e.get("ref") not in forage_excluded and (facts.free_slots != 0 or e.get("ref") in accepted)
    if (forage_offers(snapshot, context, forage_excluded)
            or any(e.get("kind") in ("forage", "forage_pickup") and forage_row(e) for e in entries) or any(
                e.get("kind") == "forage" and forage_row(e)
                for e in S._g(snapshot, "approach_targets", ()) or ())
            or visible_forage(snapshot, context, forage_excluded)):
        add("forage")
    from arti_runtime.social_facts import ANY_TALK, talk_args, talk_open, talk_route_open
    # FIX79C (ADR 141, a58: talk on 3 of 607 pages): the npc_gift rows ride `extras_targets`, published within 24 tiles and
    # shed by the mod's wire budget on most a58 publications. The scene holds every villager on her map; Live7's talk
    # routine walks to one at any distance (interact_b_runtime reach=None). A villager she has not talked to today opens it.
    # ADR 195: the named offer is only on the page when a native route exists at this moment (or the grid cannot prove
    # otherwise). Presence without a route stays a fact on `day.warga` / `warga_terlihat`, not a start that cannot begin.
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile")) if snapshot is not None else None
    grid = context.get("nav_grid")
    if talk_open(targets):  # ADR 156: extras rows alone cannot grant a talk the routine refuses.
        names = [n for n in talk_args(targets) if talk_route_open(targets, here, grid, n)]
        if names:
            add("ngobrol_hadiah", reserve_path=True)
            # FIX81B (ADR 145, a59 J1637): whom she talks to is her choice; each villager the routine would reach is one arg.
            # The argless call stays admitted, and rides the page as the listed arg `warga` (an arg list hides an argless
            # choice); both run the same any-villager routine. Nothing is removed.
            add("ngobrol_hadiah", ANY_TALK, reserve_path=True)
            for who in names:
                add("ngobrol_hadiah", who, reserve_path=True)
            # FIX84B (ADR 149, a58-a61: 0 gift offers in ten sessions): a held giftable item to a villager she may still talk
            # to today, every pair in bag order; which, and whether at all, is hers. Birthdays: the mod refuses (page says so).
            from arti_runtime.social_facts import gift_args
            for arg in gift_args(names, context.get("inventory"), birthdays=context.get("live7_birthdays") or ())[0]:
                add("ngobrol_hadiah", arg)
    if "artifact_spot" in kinds and S._has(facts, "(T)Hoe"):
        add("gali_artefak")
    from scripts.live7.capabilities import fishing_path
    if context.get("fishing", {}).get("water_edges") or fishing_path(snapshot, context):
        if facts.location and facts.location not in policy.fishing_seen:
            policy.fishing_seen.append(facts.location)  # fix55B: a native fact, recorded before any offer filter
        add("tangkap_ikan")
    from arti_runtime.mines_rules import entry_open, energy_due
    # ADR 149: no 09:00-18:00 window; an entry is offered while a floor could still be left before the 02:00 pass-out.
    if (facts.location == "Mine" and entry_open(facts.time_of_day, policy)
            and kinds & {"ladder_down", "mine_elevator"}):
        combat = context.get("combat") or {}
        from arti_stardew_combat import is_weapon  # the mod's reflex refuses the scythe (scythe_not_weapon)
        if (S._has(facts, *S.PICKAXES) and any(is_weapon(q) and n for q, n in facts.inventory.items())
                and combat.get("hp", 0) >= 80 and not energy_due(facts.energy)):
            add("lawan_mine")
        if ui.get("mine_ore_nodes") and S._has(facts, *S.PICKAXES):
            add("tambang_bijih")
    if "tv" in kinds or "television" in kinds or any(f.get("name") == "TV" for f in targets.values()):
        add("cek_pagi", "weather")
        add("cek_pagi", "fortune")
    if (type(context.get("mailbox_count")) is int and context["mailbox_count"] > 0
            and (home or "mailbox" in kinds)):
        add("cek_pagi", "mail")
    # S17's rectangle is derived from an actual removable native target.
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    from arti_stardew_animal_routine import actor_cell_open
    from arti_stardew_goal_routes import open_paths
    grid = actor_cell_open(context.get("nav_grid"), here)
    origin = here
    if grid and here is not None and grid.get(here) != ".":
        # A warp/bed row can be occupied without being a walkable routing cell.
        # Use an adjacent observed-open origin for this offer filter only.
        x, y = here
        origin = min((t for t in ((x,y-1), (x-1,y), (x+1,y), (x,y+1)) if grid.get(t) == "."), default=None)
    stands = (open_paths(grid, origin) if origin is not None else {}) if grid and here is not None else None
    debris = []
    for ref, f in targets.items():
        tile = S._wave1_xy(f.get("tile"))
        if (tile is not None and ref not in policy.refs and f.get("kind") == "object"
                and f.get("name") in ("Weeds", "Twig", "Twigs", "Stone")):
            x, y = tile
            # M5 D1 L877-L1271: stale scene debris ~25 tiles away was offered
            # despite no stand in the current nav window (clear_stand_not_offered).
            if stands is None or any(t in stands for t in ((x,y+1), (x-1,y), (x,y-1), (x+1,y))):
                debris.append(tile)
    debris.sort(key=lambda t: (abs(t[0]-here[0]) + abs(t[1]-here[1]) if here else 0, t))
    count = 0
    for tile in debris:
        before = len(candidates)
        add("bersihkan", f"{tile[0]},{tile[1]}:{tile[0]},{tile[1]}")
        count += len(candidates) > before
        if count >= DEBRIS_CHOICES:
            break
    # The native graph owns destination eligibility, including unknown/locked places in context.
    travel = travel_options(date, weekday, weather, facts, context, item_policy=item_policy,
                            memory=policy.shop_memory)
    for option in travel:
        add("pergi_ke", option["arg"], reserve_path=True)
    # Old mod compatibility: only freshly observed direct exits, with no curated destinations.
    if context.get("world_knowledge") is None:
        from arti_stardew_world import location_arg
        for f in targets.values():
            destination = f.get("target_location")
            if (f.get("kind") in ("warp", "door", "building_door") and f.get("enterable") is not False
                    and isinstance(destination, str) and destination != facts.location and f.get("npc_only") is not True):
                add("pergi_ke", location_arg(destination), reserve_path=True)
    for crop in (*context.get("remembered_crops", {}).values(), *context.get("ready_crops", {}).values()):
        if context.get("world_knowledge") is None and crop["location"] != facts.location:
            arg = next((a for a, loc in _LOCATION_ALIASES.items() if loc == crop["location"]), None)
            if arg:
                add("pergi_ke", arg)
    # PM-1 fix13b: every known recipe the game would craft now (native crafting_known: ingredients have/need and room
    # for the output after the craft), never a Chest-only readiness gate. Crafting opens her own crafting page.
    for row in craftable_recipes(context):
        if item_policy.refuse_craft(row["recipe"]) is None:
            add("buat", "peti" if row["recipe"] == "Chest" else row["recipe"])
    econ = context.get("economy") or {}
    for row in econ.get("recipes", ()):
        if (isinstance(row.get("recipe"), str) and row.get("craftable") is True and row.get("known") is True
                and item_policy.refuse_craft(row["recipe"]) is None):
            add("masak" if econ.get("cooking") is True else "buat", row.get("recipe"))
    if (facts.shop_open and "shop_counter" in kinds
            and type(context.get("money")) is int and context["money"] > 0):
        for arg, default, maximum, known in seed_quantities(context, item_policy):
            # A full bag: the generic pre-menu request names each seed stack it can grow (fix10d).
            for name in (sorted(stackable) if facts.free_slots == 0 and not known else (arg,)):
                for qty in dict.fromkeys([default, *range(1, maximum + 1)]):
                    add("beli", name, n=qty)  # existing S11/native stock, price, fit and ledger revalidate
    elif facts.shop_open:
        for arg, default, maximum, known in seed_quantities(context, item_policy):
            if known:
                for qty in dict.fromkeys([default, *range(1, maximum + 1)]):
                    add("beli", arg, n=qty)
    for row in goods_facts(context):
        qid = row.get("qualified_item_id")
        if qid in policy.uncertain_items:
            continue
        for name, action, enabled in (("jual", "sell", facts.shop_open),
                                       ("kirim", "ship", bin_approachable(context, policy, grid, origin, kinds))):
            if enabled and row in disposable(context, item_policy, action):
                add(name, row.get("name"), n=min(999, row["stack"]))
    # Remaining catalogue skills use their existing pure preflight: only a
    # concrete native first step admits them, never a missing/unlocked guess.
    for name, arg, available in (
        ("pohon_buah", None, any((f.get("fruit_tree") or {}).get("has_fruit") is True for f in targets.values())),
        ("mesin", None, bool(kinds & {"machine_collect", "machine_insert"})),
        ("gali_artefak", None, any(f.get("name") == "Artifact Spot" for f in targets.values())),
        ("crab_pot", "ambil", any(p.get("own") is True and p.get("catch") for p in ui.get("crab_pots", ()))),
        ("ikuti_festival", None, bool(ui.get("festival"))),
    ):
        if available:
            add(name, arg)
    # Parametrized native attempt interfaces. All names/items come from these
    # existing structured blocks, and their skill programs revalidate before input.
    for row in econ.get("rows", ()) if item_policy.allow_tool_upgrade and facts.location == "Blacksmith" and 900 <= (facts.time_of_day or 0) < 1600 else ():
        if (facts.inventory.get(row.get("required_tool"), 0) and
                facts.inventory.get(row.get("trade_item"), 0) >= row.get("trade_count", 5)
                and type(context.get("money")) is int and context["money"] >= row.get("price", float("inf"))):
            add("upgrade_alat", row.get("name"))
    for row in ui.get("quests", ()):
        if (row.get("type") == "ItemDelivery" and row.get("completed") is False
                and type(row.get("number")) is int and facts.inventory.get(row.get("item"), 0) >= row["number"]
                and row.get("target")):
            item = next((f.get("name") for f in context.get("inventory", ())
                         if f.get("qualified_item_id") == row["item"]), row["item"])
            add("surat_quest", item + " ke " + row["target"])
    for e in entries:
        if e.get("kind") == "place_item" and facts.inventory.get("(O)599", 0) > 0:
            tile = S._wave1_target_tile(e)
            if tile is not None:
                add("pasang", "sprinkler", tile=tile)
    if facts.location == "Farm" and facts.inventory.get(CHEST, 0) > 0:
        # Native empty adjacent ground is a candidate, never placement proof. S18 selects the
        # chest and waits for the native place_item gate before publishing any placement input.
        # Farm place_item entries are empty hoed dirt (seed plots), so they are never chest tiles.
        if (tile := next(iter(chest_tiles(snapshot, context)), None)) is not None:
            add("pasang", "peti", tile=tile)
    # S31 reuses the same native attachment parser and preflight as its executor.
    from arti_stardew_skills_attempts import _rod_pick
    if _rod_pick(S.SkillCall("siapkan_pancing"), {"attempt": {}}, context) is None:
        add("siapkan_pancing")
    # One concrete call per (name, arg): the model can only name (name, arg), so
    # several S18 tiles / S4 quality stacks resolve to the planner's first one.
    unique = {}
    for c in candidates:
        unique.setdefault((*key(c), c.n if c.name == "beli" else None), c)
    unique = list(unique.values())
    # A clean failure changes task immediately; a later reattempt still feeds
    # the signature detector. Uncertain targets never return during this day.
    if facts.location == "Farm":
        # Review M4 residual (a): crop work counts before the last_failed filter hides a failed siram.
        policy.farm_crop_pending = any(c.name in ("siram", "tanam", "jalan_ke") and not sighting_walk(c, policy, facts.location)
                                       for c in unique)
    out = [c for c in unique if home_call(c) or _plain_talk(c) or key(c) != policy.last_failed
           or way_out_trip(c, facts.location)  # ADR 239: add() already withheld it while its failure's facts hold
           or c == S.SkillCall("pergi_ke", "seed shop") and policy.shop_retry_ready(facts.time_of_day)]
    policy.release_home_loop(facts.time_of_day)
    unfiltered = out
    out = policy.travel_filter(out, facts.location, context)
    if home:
        # Keep the pending-Farm-work release independent of the optional bed offer.
        def stranded(calls):
            work = [c for c in calls if c.name not in ("tidur", "cek_pagi", "makan", "pergi_ke")]
            reachable_travel = any(c.name == "pergi_ke" and any(o["arg"] == c.arg for o in travel) for c in calls)
            return not work and (not reachable_travel or weather in BAD_WEATHER and not travel)
        if stranded(out) and policy.release_home_loop(facts.time_of_day, stranded=True):
            # Review M4 residual (b): pending Farm crop work, not an early bed, when the loop lock left only bed.
            out = policy.travel_filter(unfiltered, facts.location, context)
        if stranded(out) and not any(c.name == "tidur" for c in out):
            add("tidur")
            out += [c for c in candidates[-1:] if c.name == "tidur" and key(c) != policy.last_failed]
    next_crop = day_context(date, weekday, weather, facts, dict(context, targets=targets),
                            offered_calls=[c for c in out if not sighting_walk(c, policy, facts.location)])["crops"]["next_step"]
    economy = economy_context(facts, context)
    # Planting and watering lead while seeds/dry crops give them an offered step; income and
    # storage come right after them (before chores), and lead only when no crop step is offered.
    crop_lead = next_crop is not None or any(c.name == "siram" for c in out)
    def priority(call):
        rank = choice_priority(call, next_crop, bedtime=home and bedtime_default(facts, policy, out))
        income = economy["income_preferred"] and call.name in ("jual", "kirim")
        storage = economy["bag_tight"] and (call.name == "simpan_ke_chest"
                                           or call.name in ("buat", "pasang") and call.arg == "peti")
        if rank >= 0 and (income or storage):
            return (1.5 if crop_lead else -.75) + (.25 if storage else 0)
        return rank
    from arti_stardew_generic_world import calls as generic_calls
    # Generic controls remain choices with a full bag and alongside failed skills.
    # Only native freshness and input safety gate them; no task policy chooses their use.
    generic = generic_calls(snapshot, context) if snapshot is not None else []
    return list(dict.fromkeys(generic + sorted(out, key=priority)))


def choice(decision, offered_calls, offered_arg_choices=None, *, goods_names=None):
    """Exact catalogue calls only, including native derived rectangle/placement args.

    ``goods_names`` (fix63B, ADR 95): simpan_ke_chest lists {arg: "(O)388", name: "Wood"} and its batch text says "nama";
    a45 rejected "Wood"/"Parsnip"/"Stone" 5 of 11 times. An exact listed name means that listed stack, nothing else."""
    from arti_runtime.choice_contract import normalize
    decision, _ = normalize(decision, offered_calls, goods_names=goods_names)
    c = decision.choice
    if getattr(c, "kind", None) in ("finish", "ask_owner"):
        return c.kind
    args = getattr(c, "args", None)
    operation = getattr(c, "operation", None)
    if (operation != S.SKILL_OPERATION and operation in {call.name for call in offered_calls}
            and isinstance(args, Mapping) and "name" not in args):
        # An unambiguous skills-only spelling, seen in candidate S. It names
        # exactly the same offered call; never translate arbitrary world ops.
        args = dict(args, name=operation)
        operation = S.SKILL_OPERATION
    if (getattr(c, "kind", None) != "act" or operation != S.SKILL_OPERATION
            or getattr(c, "target_ref", None) is not None or not isinstance(args, Mapping)
            or not set(args) <= {"name", "arg", "n"}):
        return None
    if goods_names and args.get("name") == "simpan_ke_chest" and isinstance(args.get("arg"), str):
        args = dict(args, arg=";".join(goods_names.get(part.strip().casefold(), part.strip())
                                       for part in args["arg"].split(";")))
    if (args.get("name") in S.GOODS_SKILLS and isinstance(args.get("arg"), str)
            and (";" in args["arg"] or args["arg"] == "semua")):
        names = [name.strip() for name in args["arg"].split(";")]
        if "n" in args or not names or any(not isinstance(n, str) or not n for n in names):
            return None
        named = [call for call in offered_calls if call.name == args["name"]]
        if not named or names != ["semua"] and any(n not in {c.arg for c in named} for n in names):
            return None
        from dataclasses import replace
        return replace(named[0], batch=tuple(dict.fromkeys(names)))
    matches = [call for call in offered_calls if call.name == args.get("name") and call.arg == args.get("arg")]
    if not matches and args.get("name") in ("pergi_ke", "ngobrol_hadiah"):
        from arti_runtime.experience_facts import explore_arg
        from arti_runtime.choice_contract import folded
        matches = [call for call in offered_calls if call.name == args["name"] and explore_arg(call.arg) == folded(args.get("arg"))]
    if (not matches and args.get("name") == "pergi_ke" and "n" not in args
            and str(args.get("arg") or "").strip().casefold() in HOME_SYNONYMS):
        # fix10e (a14 d8 J2562-J3447: pergi_ke kabin/kabinmu/cabin rejected 6x, 5 detail pages): her own word for
        # the offered way home names that call, only while it is offered.
        matches = [call for call in offered_calls if call == S.SkillCall("pergi_ke", "home")]
    if not matches and args.get("arg") in (None, ""):
        # Dress #2 day 1 (provider 003/004): `beli` was offered only as args ["benih"] and the model sent the bare
        # name twice; both were rejected and the plan ran ngobrol_hadiah instead. A bare name with exactly one
        # listed arg names that call; several listed args stay ambiguous (invalid), never a guess.
        named = [call for call in offered_calls if call.name == args.get("name")]
        if len({call.arg for call in named}) == 1:
            matches = named
    if "n" in args:
        if args.get("name") not in ("beli", "simpan_ke_chest") or type(args["n"]) is not int:
            return None
        quantities = [call.n for call in matches if type(call.n) is int]
        exact = [call for call in matches if call.n == args["n"]]
        # The page advertises bounded integer quantities. Intermediate amounts
        # use the same purchase executor and native money/stock/fit checks.
        if args.get("name") == "beli" and not exact and quantities and min(quantities) <= args["n"] <= max(quantities):
            from dataclasses import replace
            return replace(matches[0], n=args["n"])
        matches = exact
    return matches[0] if matches else None  # omitted n uses the first, explicitly advertised default


def choice_feedback(decision, offered_calls):
    """Explain a rejected choice without guessing another action or changing admission."""
    c = decision.choice
    args = getattr(c, "args", {})
    operation = getattr(c, "operation", None)
    name = args.get("name", operation) if isinstance(args, Mapping) else operation
    named = [call for call in offered_calls if call.name == name]
    submitted = dict(kind=getattr(c, "kind", None), operation=operation)
    if isinstance(args, Mapping):
        submitted["args"] = {str(k)[:40]: str(v)[:160] for k, v in args.items()}
    code = ("choice_shape" if getattr(c, "kind", None) != "act" or not isinstance(args, Mapping)
            or getattr(c, "target_ref", None) is not None or set(args) - {"name", "arg", "n"}
            else "skill_not_offered" if not named
            else "arg_required" if args.get("arg") in (None, "") and len({x.arg for x in named}) > 1
            else "arg_not_offered" if args.get("arg") not in {x.arg for x in named}
            else "quantity_not_offered" if "n" in args
            else "choice_shape")
    field = ("args.n" if code == "quantity_not_offered" else "args.arg"
             if code in ("arg_required", "arg_not_offered") else "args.name" if code == "skill_not_offered" else "choice")
    feedback = dict(code=code, field=field, submitted=submitted)
    if code == "quantity_not_offered":
        feedback["valid"] = sorted({x.n for x in named if type(x.n) is int
            and (name != 'simpan_ke_chest' or x.arg == args.get('arg'))}) or "omit n (one invocation)"
    if code in ("arg_required", "arg_not_offered"):
        # PM-1 3b F#5 (J1:2241): the retry sees the exact offered values, still choosing itself.
        # fix63B (a45 D19:J1668, "B" on a GameMenu): whole-menu args first; the sorted cut hid press:B behind 12 slot presses.
        # fix64 (ADR 95): the exits (press:<letter> without a slot, wait) lead, then whole-menu args, then slot presses.
        feedback["offered_args"] = sorted({str(x.arg) for x in named if x.arg is not None},
                                          key=lambda a: (0 if a == "wait" or re.fullmatch(r"press:[A-Za-z]+", a) else
                                                         2 if re.fullmatch(r"press:[AXY]:\d+(:shift)?", a) else 1, a))
    elif code == "skill_not_offered":
        # PM-1 a7 #1 (d4:J577): the rejection names the admissible set; she still chooses.
        feedback["offered"] = sorted({call.name for call in offered_calls})
    return feedback


from arti_stardew_generic_ui import GenericUiMixin, unplanned_ui_reason
from arti_stardew_live7_recovery import Live7RecoveryMixin


class Live7CampaignMixin(Live7RecoveryMixin, GenericUiMixin, Live7HelpMixin, Live7MomentsMixin):
    def _live7_remember_quest_refusals(self, context):
        """Capture current-session native quest evidence before an economy menu is closed."""
        c = getattr(getattr(self, "_skill_session", None), "campaign", None)
        if (c is None or getattr(c, "part", None) != "live"
                or tuple((context.get("identity") or ())[:5]) != c.epoch
                or not context.get("ui_complete")):
            return context
        if c.live.remember_quest_refusals(context):
            self._journal(dict(ev="live7_quest_refusals", items=sorted(c.live.quest_refused_items)))
        return dict(context, quest_refused_items=c.live.quest_refused_items)

    def _live7_stop_pending(self, folder, session):
        """Match native runtime/session semantics; never retire a file on a read."""
        from arti_stardew_command import NAMA_STOP
        from arti_stardew_stop import pending
        from arti_runtime.pad_recovery import blocked
        if blocked(self._config):
            return True  # stop acquiring/renewing; not a session STOP
        return pending(folder / NAMA_STOP, dict(session.mentah, session_id=session.session_id))

    def _live7_water_view(self, view, snapshot):
        if not self._live7_active():
            return view
        from arti_stardew_tool_work import crop_needs_water
        context, _, _ = self._live7_context(snapshot)
        tiles = tuple(tile for f in context.get("targets", {}).values()
                      if crop_needs_water(f) and (tile := S._wave1_xy(f.get("tile"))) is not None)
        if tiles:
            # Use S9's established can selection/cardinal stand/turn program.
            # The native tool gate and wet-crop verifier still authorize X.
            view.crop_target_tiles = tiles
            view.crop_enrollment_current = True
            if context.get("nav_grid") and context.get("local_window"):
                view.crop_stand_context = context  # current native cells/facts, never remembered crops
            view.approach_targets = tuple(e for e in S._g(view, "approach_targets", ())
                                          if S._wave1_target_tile(e) in tiles)
        return view

    def _live7_own_soil(self):
        """Today's verified own hoe tiles, newest first (advisory ordering only; the native place gate decides)."""
        if not self._live7_active():
            return []
        c = getattr(self._skill_session, "campaign", None)
        return list(reversed(getattr(getattr(c, "live", None), "seed_own_order", None) or []))

    def _live7_active(self):
        s = getattr(self, "_skill_session", None)
        return getattr(s, "status", None) == "active" and getattr(s, "mode", None) == "part_live"

    def _live7_context(self, snapshot):
        context = self._skill_wave1_context(snapshot)
        # ADR 170: expose native safety facts to the file-only session harness.
        # Telemetry only; admission, recovery and game inputs remain unchanged.
        motor = snapshot.motor
        combat = context.get("combat") or {}
        # MotorView intentionally omits health. Read the same native motor that
        # supplied this snapshot, retaining its revision/location/staleness pin.
        native = self._state.latest(snapshot.binding.player_id)
        health = (S._g(native, "health") if native is not None
                  and not self._state.is_stale(snapshot.binding.player_id)
                  and S._g(native, "revisi") == S._g(motor, "revisi")
                  and S._g(native, "location") == S._g(motor, "location") else None)
        vitals = _vitals(combat, health)
        hp = vitals[0] if vitals else health
        pin = (snapshot.binding.player_id, S._g(motor, "revisi"),
               S._g(motor, "location"), hp, combat.get("max_hp"))
        if type(hp) is int and S._g(motor, "status") == "current":
            from arti_runtime.mines_contact import note_hp, memory
            note_hp(self, S._g(motor, "location"), hp)  # ADR 174: every native read feeds the contact guard
            from arti_runtime.mines_body import body_guard_seen
            body_guard_seen(self, combat, S._g(motor, "location"), context)  # ADR 179/180: told afterwards
            mx = combat.get("max_hp")
            if (str(S._g(motor, "location") or "").startswith("UndergroundMine") and type(mx) is int and mx > 0
                    and hp * 10 <= mx and not memory(self).get("hp_critical_recorded")):
                memory(self)["hp_critical_recorded"] = True  # ADR 179: what the floor looked like at the HP stop line
                from arti_runtime.mines_body import floor_record
                floor_record(self, context, S._g(motor, "location"), "hp_critical", here=S._g(motor, "tile"))
        if type(hp) is int and S._g(motor, "status") == "current" and pin != getattr(self, "_harness_vitals_pin", None):
            self._harness_vitals_pin = pin
            self._journal(dict(ev="native_vitals", at=time.time(), hp=hp,
                               max_hp=vitals[1] if vitals else None,
                               player_id=snapshot.binding.player_id,
                               game=dict(date=self._date_tuple(motor), time=S._g(motor, "time_of_day"),
                                         location=S._g(motor, "location"),
                                         location_id=S._g(native, "location_id")),
                               active_menu=S._g(motor, "active_menu")))
        ui = context.get("mines_ui") or {}
        route = ui.get("goal_route") or {}
        view = self._ui_pad_view(snapshot.binding.player_id)
        context["economy"] = S._g(view, "economy", {}) or {}
        # Weekday follows the observed vanilla date; no predicted weather is
        # promoted from tomorrow's native forecast to today's weather. Both are independent of TV.
        # Native EnrichGoalRouteContext publishes today's weather (sun|rain|storm|snow|wind|
        # green_rain) plus is_raining; an older mod without the field leaves it None.
        date = self._date_tuple(snapshot.motor)
        weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[(date[2] - 1) % 7] if date else None
        weather = route.get("weather")
        if not isinstance(weather, str) and route.get("is_raining") is True:
            weather = "rain"
        c = getattr(getattr(self, "_skill_session", None), "campaign", None)
        if (c is not None and getattr(c, "part", None) == "live" and date == c.date
                and tuple((context.get("identity") or ())[:5]) == c.epoch
                and context.get("ui_complete") and context.get("inventory_complete")):
            memory = c.live.crop_memory
            before = {ref: dict(r) for ref, r in memory.crops.items()}
            absent = memory.observe(date, S._g(snapshot.motor, "location"), context.get("targets", {}),
                                    context.get("local_window"), now=S._g(snapshot.motor, "time_of_day"),
                                    inventory=context.get("inventory"))  # ADR 76: one read, crops and bag together
            reconciled = sorted(ref for ref in memory.crops if ref not in before
                                and memory.crops[ref].get("provenance") == RECONCILED_FROM_UNCERTAIN)
            if reconciled:
                self._journal(dict(ev="live7_crop_reconciled", read="decision", replay=False,
                                   session_id=getattr(self._skill_session, "session_id", None),
                                   game=dict(date=list(date)), refs=reconciled,
                                   crops={ref: dict(memory.crops[ref]) for ref in reconciled}))
            if before != memory.crops:
                self._journal(dict(ev="live7_crop_memory", at=time.time(), game=dict(date=list(date)),
                                   crops={ref: dict(r) for ref, r in memory.crops.items()}))
            skipped = memory.skipped(date)
            targets = {ref: f for ref, f in context.get("targets", {}).items() if ref not in absent | skipped}
            for ref, record in memory.crops.items():
                # Preserve known native wet state across a rejoin without inventing a crop.
                fact = targets.get(ref) or {}
                if (tuple(record.get("watered_date") or ()) == date and isinstance(fact.get("crop"), dict)):
                    targets[ref] = dict(fact, crop=dict(fact["crop"], watered=True))
            # ADR 45: her own plantings as dated facts for the decision (ready/growing/dead), beside the
            # watering trips (remembered_crops) and ready-crop trips (ready_crops). Facts and buttons, no orders.
            context = dict(context, targets=targets, remembered_crops=memory.pending(date, weather),
                           ready_crops=memory.ready(date), own_crops=memory.facts(date))
            location = S._g(snapshot.motor, "location")
            from arti_runtime.day_facts import stored_seed_rows
            context.update(known_chests=c.live.chest_memory.here(location),
                           seen_chest_places={r["location"] for r in c.live.chest_memory.rows.values()},
                           stored_seeds=stored_seed_rows(c.live.chest_memory.counts(), self._live7_known_names(context)))
        if c is not None and getattr(c, "part", None) == "live" and date == c.date:
            from arti_runtime.social_facts import greeted_names, with_greeted
            from arti_runtime.offer_withhold import route_refs
            context["excluded_scene_pickups"] = c.live.refs
            context["route_refusals"] = route_refs(self, self._skill_session, S._g(motor, "location"),
                                                   S._wave1_xy(S._g(motor, "tile")), context.get("nav_grid"))
            # ADR 161: her verified talks today count as greeted while the native flag lags
            if names := greeted_names(getattr(c.live, "greeted", None), date):
                context = dict(context, targets=with_greeted(context.get("targets", {}), names))
        if (getattr(self, "_config", None) or {}).get("live7_fact_line_enabled") is True:
            context["describe_affordances"] = True
        return self._live7_remember_quest_refusals(context), weekday, weather

    def _live7_greeted_names(self):
        """ADR 161: villagers (casefolded) she verified talking to today, for the talk routine's `warga` resolution."""
        c = getattr(getattr(self, "_skill_session", None), "campaign", None)
        if c is None or getattr(c, "part", None) != "live" or not hasattr(c, "live"):
            return frozenset()
        from arti_runtime.social_facts import greeted_names
        return greeted_names(getattr(c.live, "greeted", None), c.date)

    @_offer_build_scope
    @W.span('offer_build', 1, runtime=True)
    def _live7_enrich_offer(self, session, snapshot, facts, calls, offer):
        """Rehearsal 1: the boundary request carried no clock or day context. Adds the day facts and,
        on the pergi_ke entry, what each offered destination allows (offered args only)."""
        from arti_runtime.page_fact_selection import enabled as small_page
        if small_page(getattr(self, '_config', None)):
            self._live7_page_deferred = {}
        # ADR fix115a: reuse the existing telemetry switch; disabled means no clocks or journal work.
        from arti_runtime.offer_tempo import OfferSpans
        trace = OfferSpans() if self._config.get("telemetry_enabled", True) else None
        from arti_stardew_economy import ItemPolicy
        from arti_stardew_generic_ui import prior_ui_delta
        from scripts.live7.interactions import interrupted_travel_fact
        interrupted = interrupted_travel_fact(session.campaign.live, session.campaign.date, calls)
        if interrupted is not None:
            offer["interrupted_travel"] = interrupted
        if (ui_delta := prior_ui_delta(session)) is not None:
            offer["observed_ui_delta"] = ui_delta
        shipped = next((r.state["shipping_result"] for r in reversed(session.runs)
                        if "shipping_result" in r.state), None)
        if shipped is not None:
            offer["shipping_result"] = dict(shipped)
        notices = [n for r in session.runs[-3:] for n in r.state.get("native_ui_notices", ())]
        if notices:
            offer["native_ui_notices"] = notices[-4:]
        gift = getattr(self, "_live7_gift_offer", None)
        if gift is not None:
            offer["gift"] = dict(gift["facts"])  # Sat gift: facts only; the two offered skills are her choice
        if trace: trace.mark("prior_outcomes")
        context, weekday, weather = self._live7_context(snapshot)
        if trace: trace.mark("context")
        from scripts.live7.whole_state import publish
        publish(self, session, facts, snapshot.motor, context)
        if trace: trace.mark("whole_state")
        # ADR 168: presentation only. Admission/planning keep their native catalogue.
        from arti_runtime.experience_facts import view
        catalog = context.get("world_knowledge")
        self._live7_raw_knowledge = catalog if isinstance(catalog, Mapping) else None
        if isinstance(catalog, Mapping):
            context = dict(context, world_knowledge=view(self._live7_experience_catalog(catalog, observations=context.get("targets"))))
            if (getattr(self, "_config", None) or {}).get("stardew_live7_goal_ladder", False) is True:
                from arti_runtime.goal_ladder_runtime import presentation_catalog
                context['world_knowledge'] = presentation_catalog(self, session, context['world_knowledge'])
        if trace: trace.mark("experience_view")
        if self._config.get("live7_fact_line_enabled") is True and (
                context.get("scene_map_affordances") or (context.get("mines_ui") or {}).get("map_actions")):
            from arti_runtime.fact_line import Witnesses
            from arti_runtime.map_affordances import choices as affordance_choices
            here = S._wave1_xy(S._g(snapshot.motor, "tile"))
            evidence = dict(context.get("world_knowledge") or {})
            # The public catalogue removes graph edges. Use the pinned native
            # door geometry here, with the page's display witnesses for names.
            if isinstance(catalog, Mapping):
                evidence["world_graph"] = catalog.get("world_graph")
            labels, seen, offered, omitted = affordance_choices(calls, context, here, Witnesses(evidence))
            if labels or seen:
                actions = {c.arg: c for c in calls if c.name == "aksi" and c.arg in labels}
                calls[:] = [actions[ref] for ref in labels] + [c for c in calls
                    if c.name != "aksi" or c.arg not in offered]
                offer["_map_action_labels"] = labels
                offer["facts"]["map_affordances"] = dict(ditampilkan=len(labels) + len(seen), belum_ditampilkan=omitted,
                    **({"terlihat": seen} if seen else {}))
        self._live7_world_offer(session, context, offer)
        if trace: trace.mark("world_offer")
        from scripts.live7.capabilities import capability_facts
        offer["capabilities"] = capability_facts(facts, context)
        self._live7_native_latest = self._live7_native_facts(session, facts)
        if self._live7_native_latest.get("peer_sleep"):
            offer["peer_sleep"] = self._live7_native_latest["peer_sleep"]
        npcs = [dict(ref=ref, name=f.get("display_name") or f.get("internal_name"), internal_name=f.get("internal_name"), tile=f.get("tile"),
                     talked_today=f.get("talked_today"), villager=f.get("villager"))
                for ref, f in context.get("targets", {}).items() if isinstance(f, Mapping) and f.get("kind") == "npc"]
        offer["social_availability"] = dict(observed_npcs=npcs,
            status="observed" if npcs else "absent_in_current_location" if context.get("scene_complete") else "unknown",
            held_item=S._g(snapshot, "held_item"),
            **({"action_notice": "ACTION dengan Object di tangan langsung memberi hadiah tanpa konfirmasi. "
                                 "Untuk ngobrol, pilih sendiri slot kosong atau alat dahulu. TalkedToToday adalah fakta native."}
               # Mind slice 1 render reclaim: the gift hazard exists only while a giftable item is held.
               if not str(S._g(snapshot, "held_item") or "(T)").startswith(("(T)", "(W)")) else {}))
        c = session.campaign
        from arti_runtime.travel_facts import supersede_failures
        if corrected := supersede_failures(c.live,facts.location):
            self._journal(dict(ev="travel_fact_corrected",date=list(c.date),time=facts.time_of_day,
                               location=facts.location,prior=corrected))
            from arti_runtime.travel_time import export
            self._journal(dict(ev="live7_travel_memory",trips=export(c.live)))
        counter = (context.get("mines_ui") or {}).get("shop_counter_status")
        if isinstance(counter, Mapping):
            offer["counter_status"] = dict(counter)
        if containers := (context.get("mines_ui") or {}).get("openable_containers"):
            offer["openable_containers"] = containers
        from scripts.live7.interactions import ordinary_targets
        if interactions := ordinary_targets(context):
            offer["interact_targets"] = interactions
            if self._config.get("live7_fact_line_enabled") is True:
                remaining = [row for row in interactions if row["kind"] not in ("map_action", "scene_pick_up")]
                if remaining:
                    offer["interact_targets"] = remaining
                else:
                    offer.pop("interact_targets")
        if S._g(snapshot, "generic_world_targets", ()):
            offer["world_controls"] = dict(
                action="aksi = right-click / ACTION; pakai = left-click / USE held item",
                resolves_at=[dict(button=t["kind"], tile=[t["tile_x"], t["tile_y"]]) for t in snapshot.generic_world_targets],
                held=snapshot.held_item, inventory_slots=list(snapshot.generic_inventory),
                nearby=[dict(ref=ref, **f) for ref, f in context.get("targets", {}).items()
                        if isinstance(f, Mapping)][:24],
                instruction="Pilih item, dekati/hadap petak, lalu pilih tombol. Menu memakai kontrol UI umum. Delta adalah fakta, bukan bukti tujuan selesai.")
            recent = [r.state.get("generic_readback") for r in session.runs if r.state.get("generic_readback")]
            if recent:
                offer["observed_world_delta"] = recent[-1]
        for entry in offer["skills"]:
            entry["args"] = list(dict.fromkeys(call.arg for call in calls
                                  if call.name == entry["name"] and call.arg is not None))
        offer["skills"] = list({e["name"]: e for e in offer["skills"]}.values())
        planning = dict(context, targets={ref: f for ref, f in context.get("targets", {}).items() if ref not in c.live.refs},
                        inventory=[f for f in context.get("inventory", ()) if f.get("qualified_item_id") not in c.live.uncertain_items],
                        planting_walk_pending=any(key(call) in c.live.seed_walk_targets for call in calls))
        if trace: trace.mark("native_capabilities")
        offer["day"] = day_context(c.date, weekday, weather, facts, planning,
                                   sleep_only=c.live.sleep_only, memory=c.live.shop_memory,
                                   offered_calls=[call for call in calls if not sighting_walk(call, c.live, facts.location)])
        if (getattr(self, "_config", None) or {}).get("stardew_live7_local_walks") is True:
            _lw_keys = getattr(self, "_live7_local_walk_keys", None)
            if _lw_keys and (offer["day"].get("crops") or {}).get("next_step") == "jalan_ke":
                offer["day"] = day_context(
                    c.date, weekday, weather, facts, planning,
                    sleep_only=c.live.sleep_only, memory=c.live.shop_memory,
                    offered_calls=[call for call in calls
                                   if not sighting_walk(call, c.live, facts.location) and key(call) not in _lw_keys])
        if trace: trace.mark("day_context")
        if harvest := getattr(c.live, "harvest_fact", None):
            if harvest["own_ripe"] or harvest["scene_ripe"]:
                offer["day"]["crops"]["harvest"] = harvest
        if c.live.previous_night is not None:
            offer["day"]["previous_night"] = dict(c.live.previous_night)
            from arti_runtime.day_facts import night_fact
            if night := night_fact(c.live.previous_night):
                offer["day"]["semalam"] = night  # ADR 93: the wallet across the night, one line on every page
        from arti_runtime.plan_dates import upcoming_line
        if soon := upcoming_line(getattr(self, "_live7_calendar", ()), game=MIND_GAME, day=mind_day(c.date)):
            offer["day"]["kalender"] = soon  # ADR 93: festivals and birthdays today or within a week
        from arti_runtime.place_names import current_place_label
        from arti_runtime.plan_dates import calendar_context
        from arti_runtime.place_page import here_name, actionable_line
        cal_ctx = calendar_context(getattr(self, "_live7_calendar", ()), game=MIND_GAME, day=mind_day(c.date))
        here = here_name(facts.location, context.get("world_knowledge"), cal_ctx, mind_label(c.date),
                         getattr(self, "_live7_native_place_names", None))
        offer["day"]["di_sini"] = here  # ADR 110 + ADR 197: always a name, even when unknown
        offer["day"]["bisa_di_sini"] = actionable_line(calls, context)
        if (getattr(self, '_config', None) or {}).get('live7_fact_line_enabled'):
            # ADR 232 part D (a session: a ripe count and a bare harvest choice; she asked which ones, more than once):
            # what is ripe on this map and near which building or way out, by the names her page uses.
            from arti_runtime.harvest_facts import ripe_where
            from arti_runtime.place_page import UNKNOWN
            names = getattr(self, "_live7_native_place_names", None)
            labels = {}
            for loc in {t.get("target_location") for t in (context.get("targets") or {}).values() if isinstance(t, Mapping)}:
                if isinstance(loc, str) and loc:
                    label = here_name(loc, context.get("world_knowledge"), None, None, names)
                    labels[loc] = None if label == UNKNOWN else label
            ripe = ripe_where(context, c.live.item_names, labels.get)
            if ripe:
                offer["day"]["siap_panen"] = ripe
        from arti_runtime.festival_facts import day_line as festival_day
        if fest := festival_day(getattr(self, "_live7_calendar", ()), mind_label(c.date),
                                clock=facts.time_of_day, names=getattr(self, "_live7_native_place_names", None),
                                catalog=getattr(self, "_live7_raw_knowledge", None), here=facts.location, policy=c.live):
            offer["day"]["festival_hari"] = fest
        from arti_runtime.quest_next import lines as quest_next_lines
        if nxt := quest_next_lines(getattr(self, "_live7_quest_records", ()), getattr(self, "_live7_raw_knowledge", None),
                                   facts.location, context=context, chests=c.live.chest_memory.rows):
            offer["day"]["quest_lanjut"] = nxt if len(nxt) > 1 else nxt[0]
        from arti_runtime.social_facts import here_line, remember_seen, seen_line
        from arti_runtime.social_facts import talk_args, talk_route_open
        pose = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
        talk_routes = {}
        for name in talk_args(planning.get("targets")):
            if not talk_route_open(planning.get("targets"), pose, context.get("nav_grid"), name):
                talk_routes[name] = "jalan teramati tertutup sekarang"
            elif not any(call.name == "ngobrol_hadiah" and call.arg == name for call in calls):
                reason = admission(S.SkillCall("ngobrol_hadiah", name), facts, c.live)
                reason = ("menu terbuka" if facts.menu_open else "aksi/event native sedang berlangsung" if not facts.player_free
                          else "mode pulang aktif" if c.live.sleep_only else "HP rendah" if facts.health is not None and facts.health < 30
                          else "energi rendah" if facts.energy is not None and facts.energy < VERY_LOW_ENERGY else reason)
                talk_routes[name] = "belum bisa: " + str(reason or "ancaman monster membatasi aksi saat ini")
        if people := here_line(planning.get("targets"), (getattr(self, "_live7_progress_last", None) or {}).get("friends"),
                               calendar=getattr(self, "_live7_calendar", ()), day=mind_day(c.date), game=MIND_GAME,
                               place=here or None, clock=facts.time_of_day, routes=talk_routes):
            offer["day"]["warga"] = people  # FIX79C (ADR 141): who is on her map now; whom she visits is her choice
        # FIX81B (ADR 145): where she last saw villagers today, dated, for the maps she is not on (a farmhand cannot see them).
        remember_seen(c.live.villagers_seen, facts.location, here or None, c.date, facts.time_of_day, planning.get("targets"))
        if seen := seen_line(c.live.villagers_seen, facts.location, c.date):
            offer["day"]["warga_terlihat"] = seen
        pause = c.live.route_pause
        if (pause is not None and pause.get("map") == facts.location
                and tuple(pause.get("date") or ()) == tuple(c.date)):
            # ADR 155: where she was going and whether continuing is offered, as facts beside who is here.
            from arti_runtime.route_pause import pause_line
            offer["day"]["perjalanan_jeda"] = pause_line(pause, here or None, any(
                call.name == "pergi_ke" and call.arg == pause.get("destination") for call in calls))
        # ADR 93: the same day facts reach speech (now.json) as a dated memory, not a 45 s pose read.
        self._live7_day_facts = dict(date=offer["day"].get("date"), weather=weather or "unknown",
                                     weather_tomorrow=offer["day"].get("weather_tomorrow"),
                                     musim=offer["day"].get("musim"), kalender=soon, observed_ts=time.time())
        offer["day"]["bedtime_return"] = c.live.bedtime_return
        blockage = getattr(self, '_route_blockage', None)
        if blockage and blockage.get('location') == facts.location:
            offer.setdefault('facts', {})['jalan_terhalang'] = blockage['line']
        elif blockage:
            self._route_blockage = None
        from arti_runtime import way_out
        held = [(k, b) for k, b in sorted((getattr(c.live, "way_out_failures", None) or {}).items(), key=lambda kv: str(kv[0]))
                if k[2] == facts.location and way_out.holds(b, place=facts.location, clock=facts.time_of_day,
                                                            tile=context.get("actor_tile"))]
        if held:
            # ADR 239: a trip withheld by its own failure says so once, in the game's words, with what reopens it.
            line = " ".join("%s gagal%s dari tempatmu sekarang: %s; ditawarkan lagi begitu jam atau posisimu berubah."
                            % (k[0] + (" " + k[1] if k[1] else ""), way_out.said_clock(b.get("clock")), b.get("reason"))
                            for k, b in held)
            old = offer.setdefault("facts", {}).get("jalan_terhalang")
            offer["facts"]["jalan_terhalang"] = (old + " " + line).strip() if old else line
        if c.live.home_unreachable is not None:
            offer["day"]["home_unreachable"] = dict(c.live.home_unreachable)  # facts for her choice, never a filter
        from arti_runtime.festival_facts import leave_line, inside_line
        if self._config.get("live7_fact_line_enabled") is True:
            from arti_runtime.festival_facts import event_choices, event_page
            actions = S._g(snapshot, "event_actions")
            if isinstance(actions, Mapping) and "seen" in actions:
                # ADR 240: on an event map, people by the names the game shows and press cells by where they
                # are (ADR 236 B), nearer first; the rest are counted for a closer look.
                page = event_page(snapshot)
                offered = {call.arg for call in calls if call.name in ("aksi", "ngobrol_hadiah")}
                if people := {arg: name for arg, name in page["people"].items() if arg in offered and name}:
                    offer["_event_choices"] = people
                if things := {arg: label for arg, label in page["things"].items() if arg in offered}:
                    offer["_map_action_labels"] = dict(offer.get("_map_action_labels") or {}, **things)
                offer.setdefault("facts", {})["map_affordances"] = page["counts"]
            elif choices := event_choices(snapshot):
                offer["_event_choices"] = {arg: row["label"] for arg, row in choices.items() if row["label"]}
        from arti_stardew_event_control import festival_seated
        seated = festival_seated(S._g(snapshot, "event_actions"))
        if seated and self._config.get("live7_fact_line_enabled") is True:
            from arti_runtime.festival_facts import STAND_LABEL, event_choices
            # The one control while she sits is named for what it does.
            stand = {arg: STAND_LABEL for arg, row in event_choices(snapshot).items() if row["kind"] == "festival_stand"
                     and any(call.name == "aksi" and call.arg == arg for call in calls)}
            if stand:
                offer["_map_action_labels"] = dict(offer.get("_map_action_labels") or {}, **stand)
        from arti_runtime.human_host import is_owner_hosted_config
        offered = self._live7_festival_leave_offered(snapshot)
        if leave := leave_line(facts.location, calls, S._g(snapshot, "event_movement"), seated=seated, offered=offered,
                               owner_hosted=offered and is_owner_hosted_config(self._config)):
            offer["day"]["festival_leave"] = leave
        if inside := inside_line(facts.location, S._g(snapshot, "event_movement"), S._g(snapshot, "event_actions"),
                                 getattr(self, "_live7_calendar", ()), mind_label(c.date),
                                 getattr(self, "_live7_native_place_names", None)):
            host = inside[inside.find("yang memulai"):] if "yang memulai" in inside else ""
            if host and host not in (offer["day"].get("di_sini") or ""):
                offer["day"]["di_sini"] = ((offer["day"].get("di_sini") or inside) + "; " + host)
        if (home_blocked := c.live.home_blocked_here(facts.location)) is not None:
            offer["day"]["route_home_blocked"] = dict(home_blocked, note=(
                "Jalan pulang dari sini terhalang %s (%s); tidak ada rute lain yang terlihat. "
                "Pilih kegiatan lain; jam 02:00 ARTI pingsan otomatis." % (home_blocked["blocker"], home_blocked["needs"])))
        if c.live.read_today:
            offer["day"]["read_today"] = dict(c.live.read_today)  # survives outcome trimming (render_skill_messages)
        if facts.can_water is not None:
            left, maximum = facts.can_water
            offer["day"]["watering_can"] = dict(water_left=left, water_max=maximum,
                fact="gembor kosong" if left == 0 else "air gembor: %s/%s" % (left, maximum))
            if left == 0:
                # fix42: `facts` rides on every presentation page (the day block can be pages away). Native
                # sightings only; no location or building name is guessed to hold water.
                seen = c.live.water_seen
                offer["facts"]["gembor"] = "kosong; isi ulang hanya di air yang game tawarkan (isi_gembor). " + (
                    "Air isi ulang terlihat hari ini: " + "; ".join(
                        "%s %d,%d" % (loc, t[0], t[1]) for loc, t in list(seen.items())[:3])
                    if seen else "Belum ada air isi ulang yang terlihat hari ini.")
        from arti_stardew_world import location_arg, other_homes
        if houses := sorted(other_homes(context))[:4]:
            # fix42 (a33 d7:J232): "@FarmHouse" was read as her home. Native ownership, on every page.
            offer["facts"]["rumah"] = ("pergi_ke home = rumahmu sendiri. Rumah pemain lain, bukan rumahmu: %s; "
                                       "kasurnya bukan milikmu." % ", ".join(location_arg(h) for h in houses))
        if (attacked := attacks_today(c.live, facts.health)) is not None:
            offer["day"]["attacks_today"] = attacked  # S1: a fact for her choice, never an offer filter
        if (knocked := knockout_fact(c.live, facts, context)) is not None:
            offer["day"]["knocked_out_today"] = knocked  # M12: a fact, never an offer rule
            offer.setdefault("facts", {})["knocked_out"] = knocked  # follows every page, including the next morning
        danger = danger_context(context.get("combat"), facts.health, facts.inventory, c.live, facts.time_of_day,
                                energy=facts.energy, location=facts.location)
        if danger is not None:
            offer["danger"] = danger  # smoke #12: framing for her own choice; the offers themselves come from day_plan
        from arti_runtime.mines_floor_facts import floor_offer_fact
        from arti_stardew_skills_mines import SURVIVAL_HP_PCT
        if (floor_fact := floor_offer_fact(facts.location, context.get("combat"), facts.health, edible=bool(facts.edible),
                                           offered={(x.name, x.arg) for x in calls}, low_hp=LOW_HP,
                                           reserve_pct=SURVIVAL_HP_PCT)) is not None:
            offer.setdefault("facts", {})["lantai_tambang"] = floor_fact  # ADR 153: what the floor rule does, never a filter
        if getattr(c.live, "time_withheld", None):
            offer.setdefault("facts", {})["batas_waktu"] = dict(c.live.time_withheld)
        if rules := mines_rules_fact(facts.location, [x.name for x in calls], c.live):
            offer["day"]["aturan_mines"] = rules
        from arti_runtime.mines_offer import offer_fact, label_rows
        if mines_fact := offer_fact(facts, context, c.live, calls):
            offer.setdefault("facts", {})["lawan_mine"] = mines_fact
        from arti_runtime.mines_survival import observe as observe_mines, risk_fact
        observe_mines(c.live, facts, context)
        if str(facts.location or "").startswith("UndergroundMine") or facts.location == "Mine":
            offer.setdefault("facts", {})["risiko_tambang"] = "%s %s: %s" % (mind_label(c.date), _clock(facts.time_of_day),
                risk_fact(context, getattr(c.live, "mines_last_encounter", None)))
            from arti_runtime.mines_down import note_depth, depth_line, listed_downs as listed_ways
            note_depth(c.live, facts.location)
            catalog_deepest = ((getattr(self, "_live7_progress_last", None) or {}).get("deepest_mine_level")
                               if isinstance(getattr(self, "_live7_progress_last", None), Mapping) else None)
            if line := depth_line(facts.location, catalog_deepest, getattr(c.live, "deepest_mine_floor", None)):
                offer.setdefault("facts", {})["kedalaman_tambang"] = line
                progress = offer.get("progress")
                if isinstance(progress, dict):
                    progress["tambang"] = line
            ways = listed_ways(context, facts.location)
            if ways and not any(x.name == "aksi" and any(x.arg == e.get("ref") for e in ways) for x in calls):
                old = offer.setdefault("facts", {}).get("lantai_tambang", "")
                extra = ("Tangga turun teramati; tidak ditawarkan "
                         "(cadangan HP/energi/tidur, atau pemain belum bebas).")
                offer["facts"]["lantai_tambang"] = (old + " " + extra).strip()
        if getattr(c.live, "mines_ko_fact", None):
            offer.setdefault("facts", {})["ko_tambang"] = c.live.mines_ko_fact
        # ADR 166/167/207: the measured time fact remains visible even though
        # a currently reachable manual conversation is no longer withheld.
        from arti_runtime.social_facts import talk_open
        from arti_runtime.travel_time import admission_budget
        budget=admission_budget(S.SkillCall("ngobrol_hadiah"),facts,c.live)
        if (talk_open(planning.get("targets")) and type(facts.time_of_day) is int
                and minutes(facts.time_of_day)>budget["cutoff_min"]):
            offer.setdefault("facts", {})["ngobrol_hadiah"] = (
                "Warga terlihat di peta ini; fakta cadangan waktu pulang untuk ngobrol: "
                "%dm perkiraan ngobrol + %dm perjalanan pulang + %dm margin, sekarang %s, "
                "muat sampai %s, batas %s. %s." % (
                    budget["work_min"], budget["home_min"], budget["margin_min"],
                    _clock(facts.time_of_day), budget["until"], budget["deadline"], budget["basis"]))
        from arti_runtime.mines_exit import fact as mine_exit_fact
        if exit_fact := mine_exit_fact(c.live, context, facts.location, calls):
            old = offer.setdefault("facts", {}).get("lantai_tambang", "")
            offer["facts"]["lantai_tambang"] = (old + " " + exit_fact).strip()
        if str(facts.location or "").startswith("UndergroundMine"):
            old = offer.setdefault("facts", {}).get("lantai_tambang", "")
            offer["facts"]["lantai_tambang"] = (old + " Jalan keluar memakai peta/monster terkini; "
                "jarak kontak 1,5 tile tidak dimasuki, dari kontak jarak tidak berkurang. "
                "HP turun tidak menghentikan jalan keluar; saat disentuh monster tanpa batch tempur berjalan, "
                "tubuhmu menjauh dan naik lewat jalan keluar yang ditawarkan.").strip()
            # ADR 174: what touches her, how fast HP falls, what her last batch did, food, co-op menus, way up.
            from arti_runtime import mines_contact
            from arti_runtime.mines_exit import ups as mine_ups
            here = S._wave1_xy(S._g(snapshot.motor, "tile")) if snapshot is not None else None
            ladders = [abs(e["x"] - here[0]) + abs(e["y"] - here[1]) for e in mine_ups(context, facts.location)
                       if here is not None and type(e.get("x")) is int and type(e.get("y")) is int]
            combat_now = context.get("combat") or {}
            if (attacked := mines_contact.page_fact(
                    self, combat_now, min((v for v in (facts.health, combat_now.get("hp")) if type(v) is int), default=None),
                    facts.location, inventory_food=bool(facts.edible),
                    way_up=dict(distance=min(ladders)) if ladders else None)) is not None:
                offer["facts"]["diserang"] = attacked
        else:
            # ADR 177: after the body left the floor, her first page states what it did there and where she is now.
            from arti_runtime import mines_contact
            if (after := mines_contact.page_fact(self, context.get("combat") or {}, facts.health,
                                                 facts.location)) is not None:
                offer.setdefault("facts", {})["diserang"] = after
        offer["recent_actions"] = label_rows(c.live.history)
        offer["outcomes"] = label_rows(offer.get("outcomes", ()))
        if c.live.recovery_history:
            offer["recovery"] = dict(count=c.live.recoveries, recent=[dict(r) for r in c.live.recovery_history],
                note="Input sudah dilepas dan fakta dibaca ulang; hasil yang tidak pasti tetap belum terverifikasi. "
                     "Kegagalan berulang tidak mengharuskan pulang atau tidur.")
        offer["task_finishes_without_choice"] = c.live.finish_streak
        offer["forage_here"] = [dict(ref=e["ref"], name=e.get("name", "forage"))
                                for e in forage_offers(snapshot, context, c.live.refs.union(context.get("route_refusals", ())))[:4]]
        if type(facts.time_of_day) is int:
            # Keep the established current-open-place/full-bag surface, without the old all-day return countdown.
            from arti_runtime.home_night import home_line, bed_line, observed_line
            offer['day']['bedtime_facts'] = [observed_line(planning, facts)]
            if line := home_line(facts.location, facts.time_of_day, c.live):
                offer.setdefault("facts", {})["cadangan_pulang"] = line
            if (getattr(self, '_config', None) or {}).get('live7_fact_line_enabled'):
                # ADR 234: evening body facts do not wait for the route's home reserve.
                if facts.time_of_day < 1800:
                    offer['day'].pop('bedtime_facts', None)
            if line := bed_line(context, facts, c.live, calls, S._g(snapshot.motor, "own_state") or
                                dict(energy=dict(max=S._g(snapshot.motor, "max_energy")))):
                offer.setdefault("facts", {})["malam"] = line
        from arti_runtime import loop_facts
        if trace: trace.mark("calendar_quest_social")
        from arti_runtime.collection_facts import acceptance, describe as collection_description
        if action_facts := acceptance(context):
            offer["action_facts"] = action_facts
        if collection := collection_description(context):
            offer["facts"]["collection"] = collection
            offer["day"]["collection_details"] = list(context.get("collection_fit", ()))
        self._live7_note_names(c.live, context)
        if facts.free_slots == 0:
            # A full bag's observed sinks may be elsewhere. Facts only. fix55B (a40: 2 chests crafted, 0 placed):
            # an unplaced chest she carries is one of those sinks.
            from arti_runtime.experience_facts import experienced
            via = [arg for arg in next((e["args"] for e in offer["skills"] if e["name"] == "pergi_ke"), [])
                   if arg == "seed shop" and experienced(context.get("world_knowledge") or {}, arg)]
            offered_names = {c.name for c in calls} | {"%s %s" % (c.name, c.arg) for c in calls if c.arg}
            offer["day"]["bag"] = loop_facts.space_fact(context.get("inventory"), location=facts.location,
                                                        offered=offered_names)[:-1] + (
                "; toko benih (jual) bisa dituju." if via else ".")
            used = len({r["slot"] for r in context.get("inventory", ()) if type(r.get("slot")) is int})
            if used:
                offer["day"]["bag"] = offer["day"]["bag"].replace("tas penuh;", "tas penuh (%d/%d);" % (used, used), 1)
        if at_home_base(facts, context):
            from arti_runtime.harvest_facts import home_bag_fact
            if bag := home_bag_fact(facts, context, calls):
                offer["day"]["bag"] = bag + " " + offer["day"].get("bag", "")
        from arti_runtime.room_facts import counters as counter_page, page as room_page
        if room := room_page(context, facts, c.live, calls):
            offer.setdefault("facts", {})["ruang_tas"] = room
        from arti_runtime.room_facts import bag_summary
        if any(call.name == "inventori" for call in calls):
            if line := bag_summary(context, facts, (S._g(snapshot.motor, 'own_state') or {}).get('inventory')):
                offer["day"]["bag"] = line
        if counter := counter_page(context):
            offer.setdefault("facts", {})["konter"] = counter
        if repeated := loop_facts.repeat_fact(c.live.no_effect, context):
            offer["facts"]["tanpa_efek"] = repeated  # fix55B (a40: inventori 165x, 156 unchanged): a count, never a filter
        if (getattr(self, "_config", None) or {}).get("stardew_live7_press_results"):
            from arti_runtime import press_results  # what a spot has answered every time she pressed it
            try:
                press_line, named_spots = press_results.page_fact(
                    c.live.press_results, facts=facts, context=context, snapshot=snapshot, calls=calls)
            except Exception as e:  # a fact never stops a page
                press_line, named_spots = None, set()
                self._journal(dict(ev="live7_press_results_error", at="page", error=type(e).__name__))
            if press_line:
                offer["facts"]["hasil_berulang"] = press_line
                if repeated := loop_facts.repeat_fact(c.live.no_effect, context, exclude_spots=named_spots):
                    offer["facts"]["tanpa_efek"] = repeated
                else:
                    offer["facts"].pop("tanpa_efek", None)
        fishing_offered = any(call.name == "tangkap_ikan" for call in calls)
        if fishing := loop_facts.fishing_fact(context.get("inventory"), offered=fishing_offered,
                                              free_slots=facts.free_slots,
                                              water_edge=bool((context.get("fishing") or {}).get("water_edges")),
                                              water_seen=c.live.water_seen,
                                              water_places=[location_arg(loc) for loc in c.live.fishing_seen
                                                            if loc != facts.location]):
            offer["facts"]["mancing"] = fishing  # fix55B: rod owned (slot), water edge, the catch needs a slot
        if landed := loop_facts.catch_fact(c.live.catches, c.live.catch_totals):
            offer["facts"]["hasil_pancing"] = landed  # ADR 115: a count by place and kind, never a filter on the offer
        if keepers := c.live.shop_memory.counter_facts():
            offer["facts"]["konter_teramati"] = keepers  # ADR 105: dated, her own sightings; never a filter on any trip
        ground = c.live.ground_items
        if ground.rows:
            # fix59A (ADR 86): items she left on the ground. fix58B priority: a goal names it or it is a tool she no
            # longer carries -> `facts` (every page); otherwise a paged `day` detail.
            hot, cold = ground.facts(location=facts.location, inventory=dict(facts.inventory) if facts.inventory_current else None,
                                     texts=list(getattr(self, "_live7_goal_texts", ())))
            if hot:
                offer["facts"]["barang_jatuh"] = "; ".join(hot[:2])[:240]  # MINIMAL_PAGE_FACTS bound
            if cold:
                offer["day"]["barang_jatuh"] = cold[:4]
        if stored := c.live.chest_memory.line(self._live7_known_names(context), context.get("world_knowledge")):
            # FIX72D (a53: 0 of 132 pages after d6 J2296 named her 16 seeds): where her chests are and what they held, a dated
            # sighting. `aksi` on a chest opens its menu; a fact beside the choice, never a filter on it.
            offer["day"]["peti"] = stored
        from arti_runtime.transfer_outcomes import lines as move_lines
        if moves := move_lines(session):
            offer["facts"]["pindah_barang"] = moves[0][:240]
            offer["day"]["item_move_outcomes"] = moves
        from arti_runtime.menu_recovery import fact as recovery_fact
        if recovered := recovery_fact(session, self):
            offer["facts"]["menu_terakhir"] = recovered
        if recovered := getattr(session, "_pad_recovery_fact", None):
            offer["facts"]["pad_terakhir"] = recovered
        if trace: trace.mark("bag_room_memory")
        self._live7_bag_facts(c.live, facts, context, offer)
        self._live7_forage_facts(c, snapshot, facts, context, offer)
        if trace: trace.mark("bag_forage")
        self._live7_bundle_weight(c, facts, context, offer)
        if trace: trace.mark("bundle_weight")
        offer["forage_seen_on_way"] = dict(rule="Petunjuk perjalanan saja; amati lagi sebelum memungut.",
                                           locations=c.live.forage_sightings.copy())
        offer["unproductive_travel_loop"] = c.live.travel_loop
        if fact := c.live.shuttle_fact():
            offer["travel_shuttle"] = fact
        offered = {call.arg for call in calls if call.name == "pergi_ke"}
        after = {o["arg"]: dict(location=o["location"], label=o.get("label", o["location"]),
                                can_do=[n for _, n in o["activities"]], route=o.get("route", []),
                                **({"arrival": o["arrival"]} if isinstance(o["arrival"]["native_entry_hours"], list)
                                   or o["arrival"].get("counter") or o["arrival"]["entry_status"] or o["location"] == "Mine"
                                   or (facts.location, o['arg']) in c.live.last_trips else {}),
                                **({'last_trip': c.live.last_trips[(facts.location, o['arg'])]}
                                   if (facts.location, o['arg']) in c.live.last_trips else {}))
                 for o in travel_options(c.date, weekday, weather, facts, context,
                      item_policy=ItemPolicy.from_config(self._config), memory=c.live.shop_memory)
                 if o["arg"] in offered}
        if trace: trace.mark("travel_options")
        from arti_runtime.travel_facts import arrival_facts
        for row in after.values():
            row["arrival"] = arrival_facts({}, facts.location, facts.time_of_day, row["route"], policy=c.live, base=row.get("arrival"))
        from arti_runtime.page_fact_selection import enabled as small_page, LazyDetail
        if small_page(self._config):
            from arti_runtime.page_snapshot import plain_snapshot
            self._live7_page_deferred['travel'] = LazyDetail('travel',dict(after=after))
            after = {arg:{k:plain_snapshot(v,path='travel.'+str(arg)+'.'+k) for k,v in row.items() if k in ('location','label','can_do','arrival')}
                     for arg,row in after.items()}
        for entry in offer.get("skills", ()):
            spec = S.REGISTRY.get(entry["name"])
            if entry["name"] == "aksi":
                from arti_runtime.festival_facts import labels as festival_labels
                entry["nama"] = {r["ref"]: r.get("label") or r["kind"] for r in ordinary_targets(context)
                                 if r["ref"] in entry.get("args", ())}
                if self._config.get("live7_fact_line_enabled") is True:
                    entry["nama"] = {k: v for k, v in entry["nama"].items() if v not in ("map_action", "scene_pick_up")}
                entry["nama"].update({k: v for k, v in festival_labels(snapshot).items()
                                      if k in entry.get("args", ())})
            if spec is not None and spec.family == S.F_GENERIC:
                entry["does"] = spec.canonical
            entry["does"] = S.SKILLS_V1_GOALS.get(entry["name"], {
                "tidur": "Tidur di tempat tidur sendiri sampai pagi berikutnya.",
                "isi_gembor": "Isi air gembor.", "elus": "Elus peliharaan yang terlihat.",
                "cangkul": "Cangkul tanah yang ditawarkan game.",
                "rawat_hewan": "Rawat hewan dan pungut hasil yang ditawarkan game.",
                "tebang": "Tebang satu pohon dan pungut kayunya.",
                "pecah_batu": "Pecah satu batu dan pungut hasilnya.",
                "pungut_drop": "Pungut barang jatuh yang terlihat.",
                "tangkap_ikan": "Pancing ikan di air yang ditawarkan game.",
                "ikuti_festival": "Ikuti kegiatan festival yang sedang ditawarkan game.",
                "buat": CRAFT_DOES, "inventori": MENU_DOES, "kirim": "Kirim satu barang yang diizinkan lewat kotak pengiriman.",
            }.get(entry["name"], entry.get("does", "")))
            if entry["name"] == "ngobrol_hadiah":
                from arti_runtime.social_facts import GIFT_DOES, TALK_DOES
                # FIX81B (a59 T7): the name read as "[recorded remark omitted]" after a talk; the page says what this choice does.
                entry["does"] = TALK_DOES + (GIFT_DOES if any(" ke " in str(a) for a in entry.get("args") or ()) else "")
            if entry["name"] == "aksi" and entry.get("nama"):
                entry["does"] = "; ".join("%s = %s" % item for item in entry["nama"].items())
            if entry["name"] == "pergi_ke":
                from arti_runtime.mines_offer import up_label
                if label := up_label("pergi_ke", "mine", facts.location):
                    if "mine" in entry.get("args", ()):
                        entry["nama"] = dict(entry.get("nama") or {}, mine=label)
                        entry["does"] = "mine = %s." % label
            if entry["name"] == "cek_pagi":
                entry["does"] = "TV atau surat, sesuai arg."
                options = {"weather": "TV: cuaca besok.", "fortune": "TV: keberuntungan hari ini.",
                           "mail": "Surat: %s di kotak surat; surat quest dibaca lalu quest masuk jurnal (kamu yang pilih dikerjakan "
                                   "atau tidak); surat berisi barang ditahan untuk pemilik."
                                   % context.get("mailbox_count", "unknown")}
                for channel in ("weather", "fortune"):
                    options[channel] += (" sudah ditonton hari ini (isi sama)." if channel in c.live.read_today
                                         else " belum ditonton hari ini.")
                entry["options"] = {arg: options[arg] for arg in entry["args"] if arg in options}
                entry["tv_channels"] = ("TV juga memiliki resep (Minggu, ulangan Rabu) dan tips; "
                                        "skill ini baru mendukung cuaca dan keberuntungan.")
            if entry["name"] == "isi_gembor":
                entry["does"] = ("Isi air gembor; bila air belum di jangkauan, dekati tepi air yang terlihat "
                                 "lewat jalan terbuka, lalu periksa apakah game menawarkan isi ulang.")
            if entry["name"] == "forage":
                entry["does"] = "Dekati forage asli yang terlihat lewat jalan terbuka, lalu pungut jika game menawarkan."
            if entry["name"] in ("jual", "kirim", "simpan_ke_chest"):
                from arti_stardew_skills_goods import MAX_STACKS
                from arti_stardew_economy import sentimental_facts
                entry["batch"] = dict(max_stacks=MAX_STACKS,
                    arg="Satu nama dari args, nama-nama dipisah ;, atau semua. Semua = barang non-tool yang tidak langka, bukan quest, bukan pemberian Bohan.")
                entry["does"] = ("Pindahkan tumpukan yang dipilih; tiap tumpukan diperiksa hasilnya. "
                                  "Berhenti saat STOP, perubahan menu, atau hasil tidak terverifikasi.")
                goods = goods_facts(context)
                entry["items"] = []
                if entry["name"] == "simpan_ke_chest":
                    # fix63B (ADR 95): a name naming two different stacks stays ambiguous and is not aliased.
                    named = {}
                    for row in goods:
                        if isinstance(row.get("name"), str) and row.get("qualified_item_id") in entry["args"]:
                            named.setdefault(row["name"].casefold(), set()).add(row["qualified_item_id"])
                    c.live.store_names = {k: next(iter(v)) for k, v in named.items() if len(v) == 1}
                    # FIX72D (a53 d6 J2147: "[recorded remark omitted]", the page's row bare, her words "[recorded remark omitted]"):
                    # a store row's args are IDs, so their names ride with them when the row sheds its detail.
                    entry["nama"] = {r["qualified_item_id"]: r["name"] for r in goods
                                     if r.get("qualified_item_id") in entry["args"] and isinstance(r.get("name"), str)}
                    from arti_runtime.chest_memory import store_chest_ref
                    if (chest_ref := store_chest_ref(context, blocked=c.live.refs)) and (
                            full := c.live.chest_memory.full_note(chest_ref, location=facts.location)):
                        entry["peti"] = full
                    if self._config.get("live7_fact_line_enabled") is True:
                        entry["_item_names"] = {arg: {} for arg in entry["args"]}
                for row in goods:
                    arg = row.get("qualified_item_id") if entry["name"] == "simpan_ke_chest" else row.get("name")
                    if arg not in entry["args"]:
                        continue
                    item = dict(arg=arg, name=row.get("name"), stack=row.get("stack"))
                    if type(row.get("quality")) is int:
                        item["quality"] = row["quality"]
                    if type(row.get("unit_price")) is int:
                        item["unit_price_g"] = row["unit_price"]
                    if notes := sentimental_facts(row):
                        item["facts"] = notes
                    if sender := c.live.gift_from.get(row.get("qualified_item_id")):
                        item["asal"] = "dari " + sender  # Sat gift provenance: a fact, not a keep rule (no taste trigger)
                    for native_key in ("policy", "ship_policy"):
                        if row.get(native_key) not in (None, "ok"):
                            item[native_key] = row[native_key]  # observed native restriction; DLL unchanged
                    entry["items"].append(item)
                    if (entry["name"] == "simpan_ke_chest" and self._config.get("live7_fact_line_enabled") is True
                            and isinstance(row.get("name"), str) and row["name"] and row["name"] != arg):
                        if not entry["_item_names"][arg]:
                            entry["_item_names"][arg] = dict(name=row["name"], quality=row.get("quality"),
                                count=row.get("stack"), slot=row.get("slot"))
            if entry["name"] == "buat":
                # PM-1 fix13b: the game's own recipe facts per arg (makes, have/need); compact, never an order.
                rows = {("peti" if r["recipe"] == "Chest" else r["recipe"]): r for r in craftable_recipes(context)}
                if self._config.get("live7_fact_line_enabled") is True:
                    entry["_item_names"] = {arg: dict(name=row["name"], count=row.get("n", 1)) for arg, row in rows.items()
                                            if isinstance(row.get("name"), str) and row["name"]}
                from arti_runtime.page_fact_selection import enabled as small_page, LazyDetail
                if small_page(self._config):
                    self._live7_page_deferred['recipes'] = LazyDetail('recipes', dict(rows=
                        {arg:rows[arg] for arg in entry['args'] if arg in rows}))
                else:
                    entry["recipes"] = {arg: "%s x%s: %s" % (rows[arg].get("name") or arg, rows[arg].get("n", 1), ", ".join(
                        "%s %s/%s" % tuple(n) for n in rows[arg]["needs"] if isinstance(n, list) and len(n) == 3))
                        for arg in entry["args"] if arg in rows}
            if entry["name"] == "pasang" and "peti" in entry["args"]:
                entry["does"] = "Pasang peti yang kubawa di petak yang ditawarkan game, lalu pilih simpan_ke_chest."
            if entry["name"] == "cangkul" and seasonal_seeds(c.date, facts, planning):
                entry["does"] = "Langkah awal benih di tas: cangkul tanah native → pilih tanam → siram."
            if entry["name"] == "jalan_ke":
                entry["does"] = ("Dekati petak di samping target yang terlihat lewat jalan native; amati lagi sebelum memilih ACTION/USE."
                                 if S._g(snapshot, "generic_world_targets", ()) else
                                 "Jalan menuju tanaman yang kutanam VERIFIED; baca native lagi sebelum siram. Perjalanan belum berarti tanaman disiram.")
                if planning["planting_walk_pending"]:
                    entry["does"] = "Dekati tanah kebun lewat jalan terbuka; amati lagi agar cangkul/tanam bisa ditawarkan."
                self._live7_seen_page(c, snapshot, facts, entry, context.get("route_refusals", ()))
            if entry["name"] == "kirim":
                entry["does"] += " Kiriman dibayar besok."
            if entry["name"] == "tidur" and c.live.bedtime_return and bedtime_default(facts, c.live, calls):
                entry["does"] += " Sudah memilih pulang untuk malam; lanjut tidur, hindari bolak-balik Farm/kabin."
            if entry["name"] == "beli":
                entry["quantity"] = {arg: dict(default=default, min=1, max_request=maximum,
                    max_affordable=maximum if known else None, reserve_g=seed_reserve(context.get("money")))
                    for arg, default, maximum, known in seed_quantities(context, ItemPolicy.from_config(self._config))}
                entry["quantity_rule"] = "Optional args.n; omit for default. Unknown stock/price: native S11 rechecks quantity and reserve before buying."
            if entry.get("name") == "pergi_ke" and after:
                from arti_runtime.travel_facts import door_names
                if names := door_names(after):
                    # FIX81B (ADR 145, a59 J2034): "@Trailer" said nothing of whose home it is; the game's own place name
                    # ("[recorded remark omitted]") for the door args whose id does not already say it. A day line staged like
                    # FIX79C's meaning lines: it leaves a full page first and returns after what she is doing.
                    offer.setdefault("day", {})["pintu_nama"] = "; ".join("%s = %s" % kv for kv in names.items())
                entry["after_travel"] = after
                from arti_runtime.travel_facts import compact_arrivals
                if arrivals := compact_arrivals(after):
                    entry["arrival_facts"] = arrivals
        if (getattr(self, "_config", None) or {}).get("stardew_live7_local_walks") is True:
            _lw_eyes, _lw_fact = getattr(self, "_live7_local_walks_meta", (None, None))
            if _lw_fact:
                offer.setdefault("facts", {})["lantai"] = _lw_fact
            if _lw_eyes:
                for _lw_entry in offer["skills"]:
                    if _lw_entry.get("name") == "jalan_ke":
                        _lw_shown = {a: dict(_lw_eyes[a]) for a in (_lw_entry.get("args") or ()) if a in _lw_eyes}
                        if _lw_shown:
                            _lw_entry["lantai"] = _lw_shown
                            _lw_entry["does"] = "Berjalan ke petak lantai terbuka lewat jalur teramati."
        order = {call.name: i for i, call in reversed(list(enumerate(calls)))}
        offer["skills"].sort(key=lambda entry: order.get(entry["name"], len(calls)))
        self._live7_crop_read(session.campaign, facts, context)
        from arti_runtime.place_page import arrival_line, visit_days
        scope = (context.get("world_knowledge") or {}).get("scope")
        if scope and getattr(self, "_live7_visit_scope", None) != scope:
            self._live7_visit_scope, self._live7_visits = scope, visit_days(self._live7_mind_base(),scope)
        if not hasattr(self, "_live7_visits"):
            self._live7_visits = {}
        next_visits=dict(self._live7_visits) if scope else {}
        sample = None
        visit_place = facts.location
        if self._config.get("live7_fact_line_enabled") is True and scope:
            from arti_runtime.place_visit_result import capture, observe
            pin = S._g(snapshot, "water_context") or S._g(snapshot, "movement_context")
            motor = self._state.latest(snapshot.binding.player_id)
            candidate = capture(self, pin, c, motor)
            visit_place = context.get("location_id") if not S._g(motor, "location_id") else None
            if (candidate is not None and candidate["place"] == context.get("location_id")
                    and candidate["clock"] == facts.time_of_day
                    and candidate["witness"]["motor"] == S._g(snapshot.motor, "revisi")):
                sample = candidate
                visit_place = sample["place"]
                observe(self, sample, context=context, scope=scope)
            next_visits = dict(self._live7_visits)
        self._live7_place_records(session, context)
        if (getattr(self, '_config', None) or {}).get('stardew_live7_plan_on_page', False) is True:
            from arti_runtime.goal_ladder_runtime import page_plan
            if saved := page_plan(self, session, snapshot, offer):
                offer['_live7_day_plan'] = saved
        self._live7_mind_offer(session, facts, calls, offer)
        if arrived := arrival_line(offer.get("mind"), visit_place, mind_label(c.date),
                                   offer.get("day", {}).get("bisa_di_sini"),
                                   getattr(self, "_live7_native_place_names", None),
                                   visits=next_visits, clock=facts.time_of_day, display_name=facts.location):
            offer.setdefault("day", {})["kunjungan"] = arrived
            self._live7_visit_pending=dict(scope=scope,location=visit_place,date=mind_label(c.date),
                                          clock=facts.time_of_day,line=arrived,visits=next_visits)
            if sample is not None:
                from arti_runtime.place_visit_result import findings
                self._live7_visit_pending.update(sample=sample, findings=findings(self, sample, context))
        from arti_runtime.experience_facts import travel_page
        travel_page(offer, getattr(self, "_live7_raw_knowledge", None) or context.get("world_knowledge"))
        if (getattr(self, "_config", None) or {}).get("stardew_live7_ship_on_the_way"):
            from arti_runtime import ship_pause  # ADR 228 part D: what kirim really does, no payment words
            ship_pause.page_text(offer)
        if trace:
            trace.mark("choice_descriptions")
            trace.emit(self._journal)
        return offer

    # ---------------------------------------------------------------- mind slice 1: thin glue over arti_mind
    @W.span('catalog', 2, runtime=True)
    def _live7_catalog_wait(self, live, snapshot, context):
        """ADR 99: True while this decision waits for the knowledge frame of the place she stands in, bounded. The offer
        and its facts are rebuilt from fresh observations on the next tick; no earlier frame is ever carried over."""
        since = live.catalog_wait_since
        if context.get("world_knowledge") is not None:
            if since is not None:
                live.catalog_wait_since = None
                self._journal(dict(ev="live7_catalog_wait", got=True, waited_ms=int((time.monotonic() - since) * 1000)))
            return False
        if CATALOG_WAIT_SEC <= 0:
            return False
        now = time.monotonic()
        if since is None:
            live.catalog_wait_since = now
        elif now - since >= CATALOG_WAIT_SEC:
            from arti_stardew_world import read_catalog
            live.catalog_wait_since = None
            self._journal(dict(ev="live7_catalog_wait", got=False, waited_ms=int((now - since) * 1000),
                               reason=read_catalog(self, snapshot.water_context or snapshot.movement_context)[1]))
            return False
        with self._lock:  # a tick spent waiting for a frame is not one of her idle boundaries (like the provider wait)
            live.idle_boundaries = max(0, live.idle_boundaries - 1)
        return True

    def _live7_reserve_due_after_read(self, session):
        """ADR 108 (a48 d9 J682-J838): reads stay inside one decision and each spent 50-100 game minutes, so one decision ran
        from 19:30 to 26:00 and `home_due` was never asked; the reserve due at 21:20 fired at 26:00. A read ends the decision
        when the reserve has come due, and the ordinary tick applies it. She keeps every choice until then."""
        if session.campaign.live.sleep_only:
            return False
        facts, _, _, held = self._live7_read(session)
        if held or facts is None or not home_due(facts, session.campaign.live):
            return False
        self._journal(dict(ev="live7_reads_ended_by_reserve", time_of_day=facts.time_of_day, location=facts.location,
                           reads=session.boundary_reads))
        return True

    def _live7_frame_for_pick(self, session, facts, motor, context):
        """ADR 108 (a48 d3 J1570, d4 J218): her pick was offered before and after, and read stale in between. A frame without
        world knowledge narrows the destinations (ADR 99), so wait for it as the decision did, bounded, then judge her pick
        on that. No earlier frame is carried over; a frame that never comes keeps the old re-ask."""
        deadline = time.monotonic() + CATALOG_WAIT_SEC
        while context.get("world_knowledge") is None and time.monotonic() < deadline and not self._worker_stop.is_set():
            W.call('catalog', 6, time.sleep, 0.1)
            again = self._live7_read(session)
            if again[3]:
                break
            facts, motor, context = again[:3]
        return facts, motor, context

    def _live7_experience_catalog(self, catalog, *, menu_type=None, read_text=None, observations=None):
        from arti_runtime.experience_facts import with_memory
        if not isinstance(catalog, Mapping):
            return catalog
        base = self._live7_mind_base()
        # These are native menu classes, independent of place, villager or tile.
        read = {"Billboard": ("board_read", "calendar"), "JunimoNoteMenu": ("system_read", "cc"),
                "QuestLog": ("text_read", "journal"), "DialogueBox": ("dialogue_read", "dialogue"),
                "LetterViewerMenu": ("text_read", "letter")}.get(menu_type)
        if menu_type == "JunimoNoteMenu" and ((catalog.get("progress") or {}).get("cc") or {}).get("can_read_junimo_text") is False:
            read = ("text_read", "cc_note")  # seeing unreadable glyphs does not reveal bundle requirements/rewards
        if read and read[0] in ("text_read", "dialogue_read") and not read_text:
            read = None  # opening a menu alone does not supply its unread text
        scoped = vars(self).setdefault("_live7_read_experiences", {})
        scope = catalog.get("scope")
        unscoped_read = []
        for ref, row in (observations or {}).items():
            if (isinstance(row, Mapping) and row.get("kind") in ("door", "building_door")
                    and isinstance(row.get("target_location"), str) and isinstance(ref, str) and scope):
                event = dict(id="door_seen:" + ref, kind="door_seen", subject=row["target_location"],
                             observation_ref=ref, source="native_scene:" + ref, text="pintu terlihat",
                             date=catalog.get("date"), event_place=(catalog.get("world_graph") or {}).get("current"))
                scoped[(scope, event["id"])] = event
        if read:
            kind, subject = read
            from arti_stardew_screen_text import game_text
            text = game_text(read_text) if read_text else "dibaca dari menu game " + menu_type
            import hashlib
            event = dict(id=kind + ":" + subject + ":" + hashlib.sha256(text.encode()).hexdigest()[:12], kind=kind, subject=subject,
                         text=text, source="native_menu:" + menu_type, read_time=time.time(),
                         date=catalog.get("date"), event_place=(catalog.get("world_graph") or {}).get("current"))
            if scope:
                scoped[(scope, event["id"])] = event
            else:
                unscoped_read.append(event)  # visible now; never persist/reuse an unbound read
            if base is not None and scope:
                import arti_mind
                day = mind_day(getattr(getattr(getattr(self, "_skill_session", None), "campaign", None), "date", None))
                if day is not None:
                    try:
                        if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
                            arti_mind.record_discoveries(base, MIND_GAME, scope=scope, discoveries=[event],
                                                       day=day, day_label=str(catalog.get("date")), scoped=True)
                        else:
                            arti_mind.record_discoveries(base, MIND_GAME, scope=scope, discoveries=[event],
                                                       day=day, day_label=str(catalog.get("date")))
                    except (OSError, ValueError) as exc:
                        self._journal(dict(ev="discovery_memory_error", error=type(exc).__name__))
        current = list(catalog.get("discoveries") or ())
        if unscoped_read:
            catalog = dict(catalog, experience_records=list(catalog.get("experience_records") or ()) + unscoped_read)
        current.extend(r for (s, _), r in scoped.items() if s == scope)
        # Ordinary relayed speech is evidence too; it never becomes a goal. These help-channel rows live in
        # memory only: experience_facts.view/fact_line.witnessed_catalog drop kind said_to_you before the
        # record_discoveries call below, and goal_ladder.remember refuses the kind. The one persisted path for
        # what is said to her is ADR 216's inbox -> stream_notes.ingest -> record_line (ADR 228 part B).
        for r in getattr(self, "_said_to_you", ()):
            if r.get("scope") == scope and 0 <= time.time() - r["at"] <= 900:
                current.append(dict(id=r["ref"], kind="said_to_you", text=r["text"],
                                    source=r["who"] + ":" + r["platform"], date=catalog.get("date")))
        catalog = dict(catalog, discoveries=current, experience_view=False)
        from arti_runtime.place_page import visit_events
        visits = visit_events(catalog)
        if visits:
            catalog = dict(catalog, experience_records=list(catalog.get("experience_records") or ()) + visits)
        catalog = with_memory(catalog, base, vars(self).setdefault("_live7_experience_cache", {}))
        if base is not None and scope and visits:
            import arti_mind
            day = mind_day(getattr(getattr(getattr(self, "_skill_session", None), "campaign", None), "date", None))
            if day is not None:
                try:
                    if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
                        arti_mind.record_discoveries(base, MIND_GAME, scope=scope, discoveries=visits,
                                                   day=day, day_label=str(catalog.get("date")), scoped=True)
                    else:
                        arti_mind.record_discoveries(base, MIND_GAME, scope=scope, discoveries=visits,
                                                   day=day, day_label=str(catalog.get("date")))
                except (OSError, ValueError) as exc:
                    self._journal(dict(ev="discovery_memory_error", error=type(exc).__name__))
        from arti_runtime.experience_facts import place_sources
        for place, source in place_sources(catalog).items():
            if scope and not source.startswith("location_entered:"):
                event = dict(id="place_learned:" + place, kind="place_learned", subject=place,
                             source=source, text="tempat diketahui dari " + source, date=catalog.get("date"))
                scoped.setdefault((scope, event["id"]), event)
        while len(scoped) > 512:
            scoped.pop(next(iter(scoped)))
        catalog = dict(catalog, discoveries=list(catalog.get("discoveries") or ()) +
                       [r for (s, _), r in scoped.items() if s == scope and r.get("kind") == "place_learned"])
        from arti_runtime.fact_line import runtime_catalog
        return runtime_catalog(self, catalog)

    def _live7_flush_visit(self, campaign):
        if not (getattr(self, "_live7_visit_open", None) or getattr(self, "_live7_visit_closing", None)):
            return
        from arti_runtime.place_visit_result import capture, flush, note_crossing
        from arti_stardew_world import read_catalog
        pin = self._ui_key_context(campaign.epoch[3])
        with self._lock:
            note_crossing(self, campaign, pin)
        if getattr(self, "_live7_visit_closing", None):
            if capture(self, pin, campaign) is None:
                flush(self, reason="place_read_unjoined")
            else:
                catalog, reason = read_catalog(self, pin)
                # Recheck the publications after reading the catalog.
                if capture(self, pin, campaign) is None:
                    catalog, reason = None, "place_read_unjoined"
                flush(self, scope=(catalog or {}).get("scope"), reason=reason)

    def _live7_place_records(self, session, context):
        if (getattr(self, "_config", None) or {}).get("live7_fact_line_enabled") is not True:
            return
        from arti_runtime.place_page import mark_clock_rewind, mark_reload, visit_records, with_game_words
        from arti_runtime.save_reload import reloads
        base = self._live7_mind_base()
        scope = (context.get("world_knowledge") or {}).get("scope")
        if "_live7_visit_records" not in vars(self) or getattr(self, "_live7_visit_records_scope", None) != scope:
            self._live7_visit_records_scope = scope
            self._live7_visit_records = visit_records(base, scope)
        if scope and getattr(self, "_live7_visit_rewinds_scope", None) != scope:
            self._live7_visit_rewinds_scope = scope
            self._live7_visit_rewinds = {}
        records = mark_reload(with_game_words(base, scope, self._live7_visit_records),
                              [row["facts"] for row in reloads(base, scope)])
        rewinds = vars(self).setdefault("_live7_visit_rewinds", {})
        session.campaign.live.place_visits = (mark_clock_rewind(
            records, mind_label(session.campaign.date), context.get("clock"), session.campaign.epoch[2], rewinds)
            if scope else records)

    def _live7_fact_line_offer(self, session, snapshot, offer):
        """ADR 220: shared presentation policy, including generic UI boundaries."""
        from arti_runtime.fact_line import enabled, present, runtime_catalog
        context, _, weather = self._live7_context(snapshot)
        from arti_runtime.self_hud import current, protect
        protect(offer, current(self, session, snapshot, context, weather))
        if not enabled(self._config):
            self._live7_fact_counts = {}
            return offer
        catalog = self._live7_experience_catalog(context.get("world_knowledge"))
        catalog = runtime_catalog(self, catalog, game_day=mind_day(session.campaign.date),
                                  clock=S._g(snapshot.motor, "time_of_day"))
        counts = {}
        self._live7_place_records(session, context)
        if hasattr(self, "_live7_quest_records"):
            # Which quests the native journal still holds open; a frame without its log keeps the last one.
            context = dict(context, open_quests=frozenset(
                str(q.get("id")) for q in self._live7_quest_records if q.get("status") in (None, "active")))
        projected = present(offer, catalog, config=self._config, policy=session.campaign.live, context=context, counts=counts)
        self._live7_fact_counts = counts
        # The same fact sources feed speech and the later bedtime call.
        self._live7_calendar = list((catalog or {}).get("calendar") or ())
        from arti_runtime.fact_line import witnessed_catalog
        self._live7_calendar = list((witnessed_catalog(catalog, self._config) or {}).get("calendar") or ())
        prior_day = getattr(self, "_live7_day_facts", None)
        if isinstance(prior_day, dict):
            self._live7_day_facts = {k: v for k, v in prior_day.items()
                                    if k not in ("weather_tomorrow", "musim", "kalender")}
            for key in ("weather_tomorrow", "kalender"):
                if key in (projected.get("day") or {}):
                    self._live7_day_facts[key] = projected["day"][key]
        from arti_runtime.fact_line import Witnesses, quest_lines
        read_quests = quest_lines(Witnesses(catalog), context.get("open_quests"), mind_day(session.campaign.date))
        self._live7_quest_status = dict(value=read_quests, source="native_quest_entry_read") if read_quests else None
        if isinstance(getattr(self, "_live7_progress_lines", None), dict):
            self._live7_progress_lines = dict(self._live7_progress_lines,
                value={k: v for k, v in (projected.get("progress") or {}).items()
                       if k in ("uang_dihasilkan", "skill", "alat", "teman", "tamat", "tambang")})
        return projected

    @W.span('offer_build', 3, runtime=True)
    def _live7_world_offer(self, session, context, offer):
        import arti_mind
        from arti_stardew_world import render, fact_lines, discovery_references
        from arti_runtime.experience_facts import view
        catalog = view(self._live7_experience_catalog(context.get("world_knowledge")))
        if (getattr(self, "_config", None) or {}).get("stardew_live7_goal_ladder", False) is True and isinstance(catalog, dict):
            from arti_runtime.goal_ladder_runtime import presentation_catalog
            catalog = presentation_catalog(self, session, catalog)
        from arti_runtime.experience_facts import experiences
        if catalog is not None and not offer.get("facts", {}).get("knocked_out"):
            from arti_runtime.knockout import latest, remembered
            prior_ko = latest([r for r in experiences(catalog) if r.get("kind") == "knocked_out" and r.get("text")])
            if prior_ko:
                offer.setdefault("facts", {})["knocked_out"] = remembered(prior_ko, mind_label(session.campaign.date))
        from arti_runtime.place_names import source_place
        from arti_runtime.fact_names import source_names
        self._live7_native_place_names = {p["id"]: source_place(p["id"], catalog)
            for p in native_rows(((catalog or {}).get("world_graph") or {}).get("places", ())) if p.get("id")}
        self._live7_native_names = source_names(context)
        if catalog is not None:
            catalog = dict(catalog, discoveries=[dict(e, place_name=source_place(e.get("event_place"), catalog))
                                                for e in catalog.get("discoveries", ())])
        # Rebuilt from this boundary's scoped observations; no previous-world fallback.
        self._live7_goal_targets = ()
        self._live7_goal_inventory = context.get("inventory") if context.get("inventory_complete") else None
        self._live7_goal_wallet = context.get("money") if context.get("ui_complete") else None
        raw = getattr(self, "_live7_raw_knowledge", None)
        self._live7_calendar = list((raw if isinstance(raw, Mapping) else catalog or {}).get("calendar") or ())
        from arti_runtime.fact_line import enabled
        if enabled(getattr(self, "_config", None)):
            self._live7_calendar = list((catalog or {}).get("calendar") or ())
        if (getattr(self, "_config", None) or {}).get("stardew_live7_goal_ladder", False) is True:
            from arti_runtime import goal_ladder
            self._live7_calendar = goal_ladder.calendar_seen(
                goal_ladder.facts(self._live7_mind_base(), str((catalog or {}).get('scope') or '')),
                (catalog or {}).get('calendar') or (), mind_day(session.campaign.date))
        from arti_runtime.plan_dates import calendar_context
        calendar = calendar_context(self._live7_calendar, game=MIND_GAME, day=mind_day(session.campaign.date))
        if calendar:
            offer["calendar"] = calendar
        epoch=tuple(getattr(session.campaign,"epoch",()) or ())
        if getattr(self,"_live7_discovery_epoch",None)!=epoch:
            self._live7_discovery_epoch=epoch
            self._live7_discovery_scope=None
        scope = str((catalog or {}).get("scope", ""))
        if catalog is not None:
            self._live7_discovery_scope = scope
            self._live7_native_story_scope = scope
        if (getattr(self, '_config', None) or {}).get('live7_fact_line_enabled') and catalog is not None and scope:
            # ADR 232 part H: a save loaded back to an earlier day is one dated row of her ledger, and one line here.
            from arti_runtime import save_reload
            base, today = self._live7_mind_base(), mind_day(session.campaign.date)
            if getattr(self, "_live7_reload_checked", None) != (scope, today):
                self._live7_reload_checked = (scope, today)
                try:
                    if (row := save_reload.note(base, scope, today)) is not None:
                        self._journal(dict(ev="save_reload_noted", **{k: row[k] for k in (
                            "from_label", "to_label", "utc")}))
                except (OSError, ValueError) as exc:
                    self._journal(dict(ev="save_reload_error", error=type(exc).__name__))
            if reloaded := save_reload.line(base, scope, today):
                offer.setdefault("facts", {})["muat_ulang"] = reloaded
        if (getattr(self, "_config", None) or {}).get("voice_game_memory_enabled") and catalog is not None:
            from arti_runtime import experience_digest  # ADR 228: another save never keeps this save's file
            experience_digest.live7_scope(self, session, scope)
        scale = (getattr(session, "page_budget", None) or PageBudget.from_config(getattr(self, "_config", None) or {})).scale
        if (getattr(self, "_config", None) or {}).get("stream_game_notes"):
            from arti_runtime import stream_notes
            if isinstance((catalog or {}).get("scope"), str) and catalog["scope"]:
                if stream_notes.live7_ingest(self, scope, dict(offer, world=render(catalog, max_bytes=4096 * scale))):
                    catalog = view(self._live7_experience_catalog(context.get("world_knowledge")))
        from arti_runtime.page_fact_selection import enabled as small_page, small_world, LazyDetail
        if small_page(getattr(self, '_config', None)):
            vars(self).setdefault('_live7_page_deferred', {})['world'] = LazyDetail('world', dict(catalog=catalog))
            offer['world'] = small_world(catalog, offer)
            # Items admitted by the witness layer are narrowed to the held/native choice subjects.
            held = {r.get('qualified_item_id') for r in context.get('inventory', ())}
            held.update(a for entry in offer.get('skills', ()) for a in entry.get('args') or () if isinstance(a,str))
            relevant = [r for r in (catalog or {}).get('discoveries', ())
                        if r.get('subject') in held and r.get('kind') in ('item_seen','item_obtained')]
            offer['game_facts'] = [str(r.get('text')) for r in relevant if r.get('text')]
        else:
            offer["world"] = render(catalog, max_bytes=4096 * scale)
            offer["game_facts"] = fact_lines(catalog)
        from arti_runtime.quest_facts import indicator_line
        for entry in offer.get("skills", ()):
            if entry.get("name") == "cek_jurnal":
                entry["jurnal"] = indicator_line(catalog)
                from arti_runtime.journal_read import facts as journal_facts
                entry["jurnal_baca"] = journal_facts(self, catalog)
        from arti_stardew_live7_help import Live7HelpMixin
        offer["game_facts"].extend(Live7HelpMixin._live7_said_lines(self, scope))
        journal_reads = [r for r in (catalog or {}).get("discoveries", ())
                         if r.get("kind") == "text_read" and r.get("subject") == "journal"]
        if journal_reads:
            r = journal_reads[-1]
            offer["game_facts"].append("Jurnal yang kubaca: %s (sumber: %s; waktu baca: %s)" % (
                str(r.get("text") or "")[:360], r.get("source"), r.get("read_time", "unknown")))
        if ((getattr(self, "_config", None) or {}).get("live7_world_origin") == "new_farm"
                and tuple(session.campaign.date) == (1, "spring", 1)
                and not getattr(self, "_live7_world_origin_shown", False)):
            offer["game_facts"].append("Ini farm baru; aku baru pertama berada di dunia farm ini (sumber: awal save co-op baru).")
            self._live7_world_origin_shown = True
        base, day = self._live7_mind_base(), mind_day(session.campaign.date)
        from arti_runtime.native_story import recent as story_recent
        story_scope = scope or getattr(self, "_live7_native_story_scope", None) or getattr(self, "_live7_discovery_scope", None)
        if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
            story = story_recent(base, story_scope, day, calendar=self._live7_calendar, mind_scoped=True)
        else:
            story = story_recent(base, story_scope, day, calendar=self._live7_calendar)
        if story:
            offer["world"]["native_story"] = story
        if base is None or day is None:
            # Disabling preferences/reflection does not suppress the adapter's generic event channel.
            events = list((catalog or {}).get("discoveries", ()))
            seen = self.__dict__.setdefault("_live7_discovery_seen", set())
            for event in events:
                identity = (scope, event.get("id"))
                if identity not in seen:
                    seen.add(identity)
                    self._journal(dict(ev="discovery",game=MIND_GAME,**event))
                    self._live7_discovery_event(session,event)
            offer["discoveries"] = [dict(id=e.get("id"), text=str(e.get("text", "discovery: unknown"))[:180 * scale],
                                          date=e.get("date")) for e in events[-3 * scale:]]
            if events and not small_page(getattr(self, "_config", None)):
                offer["world"].setdefault("world_details", {})["discoveries"] = events
            return
        try:
            if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
                fresh = arti_mind.record_discoveries(base, MIND_GAME, scope=scope,
                    discoveries=[discovery_references(e, wiki_cache=getattr(self, "_wiki_cache", None))
                                 for e in (catalog or {}).get("discoveries", ())],
                    day=day, day_label=mind_label(session.campaign.date), scoped=True)
            else:
                fresh = arti_mind.record_discoveries(base, MIND_GAME, scope=scope,
                    discoveries=[discovery_references(e, wiki_cache=getattr(self, "_wiki_cache", None))
                                 for e in (catalog or {}).get("discoveries", ())],
                    day=day, day_label=mind_label(session.campaign.date))
            if (getattr(self, "_config", None) or {}).get("stardew_live7_goal_ladder", False) is True and fresh:
                from arti_runtime.goal_ladder import remember
                remember(base, scope, [dict(e, newly_recorded=True) for e in fresh], day,
                         mind_scoped=(getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True)
            for event in fresh:
                self._journal(dict(ev="discovery", game=MIND_GAME, **event))
                self._live7_discovery_event(session, event)
            known_scope=getattr(self,"_live7_discovery_scope",None)
            recent = arti_mind.recent_discoveries(base, MIND_GAME,scope=known_scope, limit=3 * scale) if known_scope is not None else []
            from arti_runtime.experience_facts import witnessed_event
            earned = {e.get("id") for e in list((catalog or {}).get("experience_records", ())) + list(fresh)
                      if witnessed_event(e)}
            recent = [dict(event, references=[]) for event in recent if event["id"] in earned]
            offer["discoveries"] = [dict(id=event["id"], text=event["text"][:180 * scale], date=event.get("date"),
                sumber="dari pengamatan di " + str(event.get("place_name") or event.get("event_place") or "game")) for event in recent]
            if isinstance(offer.get("world"), dict) and recent and not small_page(getattr(self, "_config", None)):
                offer["world"].setdefault("world_details", {})["discoveries"] = recent
        except (OSError, ValueError) as exc:
            self._journal(dict(ev="discovery_memory_error", error=type(exc).__name__))
            offer["discoveries"] = list((catalog or {}).get("discoveries", ()))
        if (getattr(self, "_config", None) or {}).get("stardew_live7_goal_ladder", False) is True:
            from arti_runtime.goal_ladder_runtime import observe
            observe(self, session, catalog)
        self._live7_progress_offer(session, catalog, offer)

    def _live7_item_text(self, catalog):
        """ADR 88: the game's own item text: her persisted discovery rows plus this boundary's native rows."""
        import arti_mind
        from arti_runtime.item_facts import native_descriptions
        base, scope = self._live7_mind_base(), getattr(self, "_live7_discovery_scope", None)
        known = {}
        if base is not None and scope is not None:
            try:
                known = arti_mind.item_descriptions(base, MIND_GAME, scope=scope)
            except (OSError, ValueError) as exc:
                self._journal(dict(ev="discovery_memory_error", error=type(exc).__name__))
        return dict(native_descriptions((catalog or {}).get("discoveries", ())), **known)

    def _live7_bag_facts(self, live, facts, context, offer):
        """fix61 (ADR 88): a human's item tooltip. Which bag items fit the museum or a bundle, and a few native item
        descriptions she has not seen today. Facts only; the whole bag is described on the open menu."""
        from arti_runtime.item_facts import bag_uses, next_descriptions
        if not facts.inventory_current:
            return
        catalog = context.get("world_knowledge")
        progress = catalog.get("progress") if isinstance(catalog, Mapping) else None
        if isinstance(catalog, Mapping):
            from arti_runtime.item_facts import readable_progress
            progress = readable_progress(progress, vars(self).setdefault("_live7_bag_match_memory", {}))
            from arti_runtime.experience_facts import progress_view
            progress = progress_view(progress, catalog)
        rows = [f for f in context.get("inventory", ()) if isinstance(f, Mapping) and f.get("qualified_item_id")]
        rows.sort(key=lambda f: f["slot"] if type(f.get("slot")) is int else 999)
        held = [f["qualified_item_id"] for f in rows if facts.inventory.get(f["qualified_item_id"])]
        held = held or [q for q, n in facts.inventory.items() if n]
        if uses := bag_uses(progress, set(held)):
            offer["day"]["tas_kegunaan"] = uses
        from arti_runtime.bundle_knowledge import open_room_line, stored_uses
        stacks = {f["name"]: facts.inventory[f["qualified_item_id"]] for f in rows
                  if f.get("name") and facts.inventory.get(f["qualified_item_id"])}
        chests = getattr(live, "chest_memory", None)
        stored = chests.stored(self._live7_known_names(context)) if chests is not None else None
        native_cc = progress.get("cc") if isinstance(progress, Mapping) else None
        if line := open_room_line(native_cc, stacks, stored):
            offer["day"]["cc_terbuka"] = line  # FIX71D: which room is open and what it asks for, not only the fit she holds
        if line := stored_uses(native_cc, stored):
            offer["day"]["peti_kegunaan"] = line  # FIX72D: a stored stack against an open or shut room, as her bag's line says it
        if described := next_descriptions(held, self._live7_item_text(catalog), live.items_told, live.items_seen):
            offer["day"]["tas_info"] = described

    def _live7_forage_facts(self, c, snapshot, facts, context, offer):
        """FIX73E: forage on this map with its distance, whether the bag is full, what fits an open bundle, and where she has seen it
        this season. Written whatever the `forage` offer does: a full bag hides the offer (a press is refused), never the ground."""
        from arti_runtime.bundle_knowledge import open_fits
        from arti_runtime.place_names import source_place
        memory, catalog = c.live.forage_memory, context.get("world_knowledge")
        if facts.location and context.get("location_id", facts.location) == facts.location:
            self._live7_forage_note(c, facts.location, context.get("targets"))
        progress = catalog.get("progress") if isinstance(catalog, Mapping) else None
        if isinstance(catalog, Mapping):
            from arti_runtime.item_facts import readable_progress
            progress = readable_progress(progress, vars(self).setdefault("_live7_bag_match_memory", {}))
            from arti_runtime.experience_facts import progress_view
            progress = progress_view(progress, catalog)
        fits = open_fits(progress.get("cc") if isinstance(progress, Mapping) else None)
        place = lambda where: source_place(where, catalog)
        here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
        if line := memory.here_line(facts.location, here, c.date, full=facts.free_slots == 0, fits=fits, place=place(facts.location)):
            offer["day"]["forage_terlihat"] = line
        if line := memory.seen_line(facts.location, c.date, fits=fits, place=place):
            offer["day"]["forage_ingat"] = line
        seen = c.live.seen_objects
        if facts.location and context.get("location_id", facts.location) == facts.location:
            self._live7_seen_note(c, facts.location, context.get("targets"), here, grid=context.get("nav_grid"))
        refused = context.get("route_refusals", set()) - c.live.refs
        walks = {r["ref"] for r in seen_walk_offers(c.live, facts.location, here, refused)}
        if refused:
            if context.get("describe_affordances"):
                from arti_runtime.fact_line import visible_targets
                targets = visible_targets(context)
            else:
                targets = context.get("targets", {})
            names = [(targets[ref].get("display_name") or targets[ref].get("name")) for ref in sorted(refused & targets.keys())
                     if isinstance(targets[ref], Mapping)]
            names = list(dict.fromkeys(n for n in names if isinstance(n, str) and 0 < len(n) <= 40))
            target = " ke " + ", ".join(names[:4]) if names else ""
            line = f"Saat berjalan tadi, aku tidak menemukan jalan terbuka{target}."
            prior = offer["facts"].get("jalan_terhalang")
            offer["facts"]["jalan_terhalang"] = f"{prior} {line}" if prior else line
        if line := seen.read_line(facts.location, c.date, place=place, here=here, offered=walks):
            offer["day"]["benda_terlihat"] = line  # ADR 136: one read away; the walks on the page are at most three of these
        if line := seen.take_gone(c.date):
            offer["day"]["benda_hilang"] = line

    def _live7_bundle_weight(self, c, facts, context, offer):
        """FIX75B (ADR 129; a54: the bundle was named on up to 5 lines of one page and `forage_ingat` rode 255 of 261 replay pages):
        outside the Community Center and a goal of her own that names it, one `day` line at most names a bundle; the room line
        rides in full when the native state changed since she last saw it; `forage_ingat` rides on a map change. The rest moves
        to `day.bundle_rinci`, which the renderer keeps one read away. Facts move, none is dropped; nothing here chooses."""
        from arti_runtime import bundle_knowledge as B
        catalog = context.get("world_knowledge")
        progress = catalog.get("progress") if isinstance(catalog, Mapping) else None
        if isinstance(catalog, Mapping):
            from arti_runtime.item_facts import readable_progress
            progress = readable_progress(progress, vars(self).setdefault("_live7_bag_match_memory", {}))
        cc = progress.get("cc") if isinstance(progress, Mapping) else None
        free = facts.location == "CommunityCenter" or B.own_goal_names(getattr(self, "_live7_goal_texts", ()), cc)
        seen = B.signature(cc)
        changed = seen is not None and seen != c.live.cc_seen
        moved_on = facts.location != c.live.page_location
        moved = B.settle(offer["day"], free=free, changed=changed, moved_on=moved_on)
        if seen is not None and "cc_terbuka" in offer["day"] and seen != c.live.cc_seen:
            c.live.cc_seen = seen
            self._journal(dict(ev="live7_cc_seen", at=time.time(), game=dict(date=list(c.date)), signature=seen))
        c.live.page_location = facts.location
        if moved:
            self._journal(dict(ev="live7_bundle_weight", at=time.time(), free=free, changed=changed, moved_on=moved_on,
                               moved=sorted(moved)))

    def _live7_forage_note(self, c, location, targets):
        """One scene read into her forage memory; only a read carrying its own location row speaks for the whole map."""
        complete = any(isinstance(f, Mapping) and f.get("kind") == "location" and f.get("location_id") == location
                       for f in (targets or {}).values())
        if c.live.forage_memory.observe(location, targets, date=c.date, complete=complete):
            self._journal(dict(ev="live7_forage_memory", at=time.time(), game=dict(date=list(c.date)), memory=c.live.forage_memory.dump()))

    def _live7_seen_note(self, c, location, targets, here, grid=None):
        """ADR 136: one scene read into what she has seen on this map, whatever it holds or has dropped off its nearest 40."""
        if c.live.seen_objects.observe(location, targets, here=here, date=c.date, grid=grid):
            self._journal(dict(ev="live7_seen_objects", at=time.time(), game=dict(date=list(c.date)), memory=c.live.seen_objects.dump()))

    def _live7_seen_page(self, c, snapshot, facts, entry, route_refs=()):
        """ADR 136: what each walk's arg is, beside the arg. A fact about her own sighting; the choice is unchanged."""
        here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
        args = entry.get("args") or ()
        rows = [r for r in seen_walk_offers(c.live, facts.location, here, route_refs) if "%d,%d" % tuple(r["tile"]) in args]
        if rows:
            entry["nama"] = c.live.seen_objects.names(rows, here, c.date)
            seen = "Yang ada di nama: benda yang pernah kulihat di sana dan kapan; amati lagi setibanya, bisa sudah tidak ada."
            # pkg76: a soil or crop walk beside them keeps what it does; the sighting text only adds what the named args are.
            entry["does"] = "Jalan ke petak itu. " + seen if len(rows) == len(args) else entry["does"] + " Arg " + seen

    def _live7_note_names(self, live, context):
        """FIX71C, FIX72D: the native names she has read on a bag row or a shop row, carried across days. Stock names a tool
        before she owns it, so a tool at the blacksmith keeps the name the counter gave it (no name table, no C#)."""
        from arti_runtime import loop_facts
        from arti_runtime.fact_names import display_name
        seen = {r["qualified_item_id"]: r["name"] for r in loop_facts.bag_rows(context.get("inventory"))
                if isinstance(r.get("name"), str) and r["name"]}
        for row in (context.get("economy") or {}).get("rows", ()):
            if isinstance(row, Mapping) and isinstance(row.get("item"), str) and (name := display_name(row.get("name"))):
                seen[row["item"]] = name[:40]
        new = {q: n for q, n in seen.items() if live.item_names.get(q) != n}
        if new:
            live.item_names.update(new)
            self._journal(dict(ev="live7_item_names", names=new))

    def _live7_known_names(self, context):
        """Native names for an item not in her hands now: discoveries she has read, then this run's bag and shop rows."""
        from arti_runtime.fact_names import source_names
        live = getattr(getattr(getattr(self, "_skill_session", None), "campaign", None), "live", None)
        return {**source_names(dict(world_knowledge=context.get("world_knowledge"))), **getattr(live, "item_names", {})}

    def _live7_progress_offer(self, session, catalog, offer):
        """fix13c (PROGRESS_GAP_AUDIT_2026-10-29 ranked 4/5): the native progress surface, letters and quest log as one
        compact ``progress`` block. Letters and quests persist in her mind ledger across menus, days and sessions, so a
        letter read at 06:10 is still a fact at 15:00 and tomorrow. Facts only: nothing is adopted as a goal here."""
        import arti_mind
        from arti_stardew_world import progress_block, progress_snapshot, letter_records, quest_log_read, quest_records
        if not isinstance(catalog, Mapping):
            return
        c = session.campaign
        if progress_snapshot(catalog) is None:
            return
        letters, quests = letter_records(catalog), quest_records(catalog)
        base, day, label = self._live7_mind_base(), mind_day(c.date), mind_label(c.date)
        scope, start = str(catalog.get("scope") or ""), None
        from arti_runtime.progress_counts import harvested_items
        # ADR 36: each real-path day has a fresh Campaign. Anchor its receipts to the last scoped total,
        # including same-day restarts, instead of publishing a daily counter as a lifetime counter.
        anchor = getattr(self, "_live7_harvest_anchor", None)
        if anchor is None or anchor[0] is not c or anchor[1] != scope:
            previous = arti_mind.progress_days(base, MIND_GAME, scope=scope) if base is not None and scope else []
            latest = [((r.get("latest") or {}).get("crops") or {}).get("harvested") for r in previous
                      if day is not None and r["day"] <= day]
            offset = max((n for n in latest if type(n) is int), default=0)
            anchor = self._live7_harvest_anchor = (c, scope, offset)
        harvested = anchor[2] + harvested_items(getattr(c, "days", None))
        snap = progress_snapshot(catalog, crops=dict(harvested=harvested))
        # A rotating knowledge frame can contain the quest log before the
        # chunked progress fact. That is not the day's numeric baseline, nor a
        # replacement for the last numeric observation used at bedtime.
        numeric = isinstance(snap.get("skills"), Mapping) and type(snap.get("money_earned_self")) is int
        if not numeric:
            unavailable = (c.date, repr(catalog.get("progress")),
                           repr(catalog.get("read_failures", {}).get("progress")))
            if getattr(self, "_live7_progress_unavailable", None) != unavailable:
                self._live7_progress_unavailable = unavailable
                self._journal(dict(ev="progress_snapshot_unavailable", date=label,
                    fields=[k for k in ("skills", "money_earned_self") if snap.get(k) is None],
                    source=catalog.get("read_failures", {}).get("progress")
                           or (catalog.get("progress") or {}).get("unknown", "progress fact absent or incomplete")))
        quest_changes = []  # fix60A (ADR 87): native status changes seen at this read (ledger upsert result)
        if base is not None and day is not None and scope:
            try:
                if numeric:
                    row = arti_mind.save_progress(base, MIND_GAME, scope=scope, day=day, day_label=label, snapshot=snap)
                    start = row["start"]
                present = {q["id"] for q in quests}
                # ADR 102 (a47 d20 J83-J89): a frame without its quest log says nothing about any quest.
                gone = [dict(id=r["item_id"], status="not_in_quest_log") for r in
                        arti_mind.ledger_rows(base, MIND_GAME, scope=scope, kind="quest")
                        if r.get("status") == "active" and r.get("item_id") not in present] if quest_log_read(catalog) else []
                from arti_runtime.quest_facts import requirements as quest_requirements
                for kind, items in (("letter", letters), ("quest", quests + gone)):
                    for changed in arti_mind.record_items(base, MIND_GAME, scope=scope, kind=kind, items=items,
                                                          day=day, day_label=label):
                        row = dict(ev=kind + "_record", game=MIND_GAME, day=day, date=label,
                                   item_id=changed["item_id"], status=changed["status"],
                                   title=changed.get("title"), text=changed.get("text"))
                        if kind == "letter":
                            row.update({k: (changed.get("facts") or {}).get(k) for k in
                                        ("read_place", "read_place_name", "read_time")})
                        if kind == "quest":
                            if isinstance(changed.get("facts"), Mapping):  # fix60A: what it still needs, natively
                                row["needs"] = quest_requirements(changed["facts"])
                            quest_changes.append(dict(item_id=changed["item_id"], title=changed.get("title"),
                                                      status=changed["status"]))
                        self._journal(row)
                # Durable view: every letter with its text she has had, newest status last; the live log for quests.
                ledger = sorted(arti_mind.ledger_rows(base, MIND_GAME, scope=scope, kind="letter"),
                                key=lambda r: (r.get("status_day", 0), r.get("first_day", 0)))
                letters = [dict(id=r["item_id"], status=r.get("status"), title=r.get("title", ""), text=r.get("text", ""), facts=r.get("facts"))
                           for r in ledger if r.get("text")]
                # ADR 45: the absolute date she first saw each quest in her log rides with its progress.
                since = {r["item_id"]: r.get("first_label") for r in
                         arti_mind.ledger_rows(base, MIND_GAME, scope=scope, kind="quest") if r.get("first_label")}
                for q in quests:
                    if q["id"] in since:
                        q["facts"] = dict(q.get("facts") or {}, since=since[q["id"]])
            except (OSError, ValueError) as exc:
                self._journal(dict(ev="progress_memory_error", error=type(exc).__name__))
        if numeric and getattr(self, "_live7_progress_day", None) != c.date:
            self._live7_progress_day = c.date
            self._journal(dict(ev="progress_snapshot", game=MIND_GAME, day=day, date=label, phase="morning", progress=snap))
        if numeric:
            self._live7_progress_last = snap
        # fix60A (ADR 87): the speech side gets each native quest's requirement and status, refreshed from the newest
        # native quest log read; status changes keep their reception stamp so a completed action can be dated against them.
        from arti_runtime.quest_facts import status_fact as quest_status_fact
        quest_stamp = time.time()
        self._live7_quest_changes = (list(getattr(self, "_live7_quest_changes", ()))
                                     + [dict(ch, observed_ts=quest_stamp) for ch in quest_changes])[-8:]
        self._live7_quest_status = quest_status_fact(quests, observed_ts=quest_stamp, date=label,
                                                     changes=self._live7_quest_changes)
        from arti_runtime.goal_facts import measurements
        from arti_runtime.place_page import goal_places
        history, goal_quests = [], list(quests)
        if base is not None and scope:
            history = arti_mind.progress_days(base, MIND_GAME, scope=scope)
            goal_quests = [dict(r, id=r["item_id"]) for r in
                           arti_mind.ledger_rows(base, MIND_GAME, scope=scope, kind="quest")]
        visited = goal_places(catalog, label)
        self._live7_goal_targets_stamp = time.time()
        self._live7_goal_targets = [dict(f, observed_ts=self._live7_goal_targets_stamp) for f in
            measurements(snap, date=label, history=history, quests=goal_quests, places=visited)]
        native_log = catalog.get("quest_log")
        if isinstance(native_log, Mapping) and isinstance(native_log.get("quests"), (list,tuple)):
            # A rotating frame without its log is no evidence that a held quest
            # disappeared. A complete later log supersedes the prior one.
            if native_log.get("complete", True) is False:
                prior = {q["id"]: q for q in getattr(self,"_live7_quest_records",())}
                prior.update({q["id"]: q for q in quests})
                quests = list(prior.values())
            self._live7_quest_records = list(quests)
        from arti_runtime.page_fact_selection import enabled as small_page, LazyDetail
        arguments = dict(catalog=catalog, start=start, letters=letters, quests=quests,
                         names=getattr(getattr(c, 'live', None), 'item_names', None))
        if small_page(getattr(self, '_config', None)):
            vars(self).setdefault('_live7_page_deferred', {})['progress'] = LazyDetail('progress', arguments)
        block = progress_block(**arguments, include_details=not small_page(getattr(self, '_config', None)))
        if block:
            self._live7_progress_lines = dict(  # ADR 93: the compact native progress lines reach speech too
                value={k: block[k] for k in ("uang_dihasilkan", "skill", "alat", "teman", "tamat", "tambang") if k in block},
                date=label, observed_ts=quest_stamp, source="native_progress", relation="memory")
            from arti_runtime.travel_facts import arrival_facts
            from scripts.live7.native_facts import bound_pose
            pose = bound_pose(self)
            here, now = getattr(pose, 'location', None), getattr(pose, 'time_of_day', None)
            place = next((p for p in native_rows((catalog.get("world_graph") or {}).get("places", ()))
                          if p.get("id") == "CommunityCenter"), None)
            if place and block.get("donation_facts"):
                route = place.get("motor_route", place.get("route"))
                if isinstance(route, list):
                    arrival = arrival_facts(place, here, now, route)
                    block["donation_facts"]["cc_arrival"] = {k: arrival[k] for k in (
                        "current_game_time", "from_location", "travel_estimate_min", "arrival_game_time_estimate",
                        "native_entry_hours", "entry_status")}
            self._live7_bundle_knowledge = (block.get("donation_facts"),
                                          dict(date=label, time=now),
                                          ((block.get("progress_details") or {}).get("donations") or {}).get("cc"))
            offer["progress"] = block

    def _live7_mind_base(self):
        import arti_mind
        cfg = getattr(self, "_config", None) or {}
        return None if cfg.get("arti_mind_enabled", True) is False else arti_mind.mind_dir(cfg)

    def _live7_crop_read(self, c, facts, context):
        """ADR 99: which crops today's latest Farm scene held, for the fact beside a goal that names one."""
        if facts.location == "Farm" and context.get("targets") is not None:
            c.live.crop_read = (list(c.date), [f["crop"].get("qualified_item_id") for f in context["targets"].values()
                                               if isinstance(f.get("crop"), Mapping) and f["crop"].get("dead") is not True])

    def _live7_crop_goal_facts(self, c, texts):
        from arti_runtime.goal_facts import crop_facts
        read = c.live.crop_read
        today = read is not None and read[0] == list(c.date)
        return crop_facts(texts, c.live.crop_memory.crops.values(), read[1] if today else None, mind_label(c.date),
                          seen_date=mind_label(c.date), season=c.date[1] if len(c.date) > 1 else None)

    def _live7_mind_offer(self, session, facts, calls, offer):
        """Her prefs/goals as the bounded ``mind`` block; relevance tags are the offered names/args + place."""
        import arti_mind
        base, day = self._live7_mind_base(), mind_day(session.campaign.date)
        if base is None or day is None:
            return
        tags = [c.name for c in calls] + [str(c.arg) for c in calls if c.arg is not None] + [str(facts.location or "")]
        tags += arti_mind.moment_tags(facts.time_of_day)  # FIX90B: the part of her day, for trait relevance only
        try:
            if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
                _mind_scope = getattr(self, "_live7_discovery_scope", None)
                goals = arti_mind.load_goals(base, MIND_GAME, scope=_mind_scope, scoped=True) if _mind_scope else []
                _unscoped = arti_mind.unscoped_counts(base, MIND_GAME)
                if any(_unscoped.values()):
                    _ukey = (_mind_scope, tuple(sorted(_unscoped.items())))
                    if getattr(self, "_live7_unscoped_logged", None) != _ukey:
                        self._live7_unscoped_logged = _ukey
                        self._journal(dict(ev="mind_unscoped_rows", game=MIND_GAME, day=day, counts=_unscoped))
            else:
                goals = arti_mind.load_goals(base, MIND_GAME)
            from arti_runtime.fact_line import enabled, plan_views
            if enabled(self._config):
                goals = plan_views(goals, getattr(self, "_live7_calendar", ()))
            from arti_runtime.route_pause import carried_goal_line
            if ('_live7_day_plan' not in offer and isinstance(offer.get("day"), dict)
                    and (carried := carried_goal_line(goals, day=day, game=MIND_GAME))):
                # The world ladder shows one dated today want. Keep the legacy summary on other pages.
                offer["day"]["tujuan_dibawa"] = carried  # ADR 155: a carried today goal's age, a fact; staged first
                tags.append(arti_mind.CARRIED_TAG)
            from arti_runtime.goal_facts import measurements
            # Inventory IDs/counts are from SkillFacts; names come only from native item rows.
            targets = list(getattr(self, "_live7_goal_targets", ()))
            targets = [f for f in targets if f["kind"] not in ("item", "sleep")
                       and not (f["kind"] == "money" and f["name"] == "wallet")]
            night = session.campaign.live.previous_night or {}
            from arti_runtime.goal_facts import sleep_measurement
            if slept := sleep_measurement(night, session.campaign.date):
                targets.append(slept)
            stamp = time.time()
            own = measurements(date=mind_label(session.campaign.date), wallet=getattr(self, "_live7_goal_wallet", None),
                               inventory=getattr(self, "_live7_goal_inventory", None) if facts.inventory_current else None)
            known = {(f["kind"], f["name"]) for f in targets}
            targets.extend(dict(f, observed_ts=stamp) for f in own if (f["kind"], f["name"]) not in known)
            if facts.inventory_current:
                known_items = {f["name"] for f in targets if f["kind"] == "item"}
                targets.extend(dict(kind="item", name=qid, value=n, aliases=[], date=mind_label(session.campaign.date), observed_ts=stamp)
                               for qid, n in facts.inventory.items() if qid not in known_items)
            targets = [f for f in targets if f["kind"] != "ground"] + [  # fix59A (ADR 86): items left on the ground
                dict(f, observed_ts=stamp) for f in session.campaign.live.ground_items.goal_facts(
                    mind_label(session.campaign.date))]
            from arti_runtime.native_story import letter_facts
            targets = [f for f in targets if f["kind"] != "letter"] + [  # FIX73D: letters she has read, by sender
                dict(f, observed_ts=stamp) for f in letter_facts(
                    base, getattr(self, "_live7_native_story_scope", None) or getattr(self, "_live7_discovery_scope", None),
                    day, date=mind_label(session.campaign.date))]
            self._live7_goal_texts = [g["text"] + " " + str(g.get("done_when") or "") for g in goals]
            live_texts = [g["text"] + " " + str(g.get("done_when") or "") for g in goals if g.get("status") in ("open", "expired")]
            targets = [f for f in targets if f["kind"] != "crop"] + [
                dict(f, observed_ts=stamp) for f in self._live7_crop_goal_facts(session.campaign, live_texts)]
            self._live7_goal_targets = targets
            self._live7_goal_targets_stamp = stamp
            scale = (getattr(session, "page_budget", None) or PageBudget.from_config(getattr(self, "_config", None) or {})).scale
            if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
                if (getattr(self, "_config", None) or {}).get("stardew_live7_goal_ladder", False) is True:
                    from arti_runtime.goal_ladder_runtime import page
                    block = page(self, session, facts, calls, offer, arti_mind.load_prefs(base), goals)
                else:
                    _mind_scope = getattr(self, "_live7_discovery_scope", None)
                    block = arti_mind.render_block(arti_mind.load_prefs(base), goals,
                                               game=MIND_GAME, day=day, tags=tags,
                                               max_bytes=arti_mind.BLOCK_MAX_BYTES * scale,
                                               projection_scale=scale,
                                               yesterday=arti_mind.yesterday_line(base, MIND_GAME, day, scope=_mind_scope, scoped=True) if _mind_scope else None,
                                               yesterday_dates=arti_mind.yesterday_dates(base, MIND_GAME, day, scope=_mind_scope, scoped=True) if _mind_scope else (),
                                               calendar=getattr(self, "_live7_calendar", ()), native_targets=targets)
            else:
                if (getattr(self, "_config", None) or {}).get("stardew_live7_goal_ladder", False) is True:
                    from arti_runtime.goal_ladder_runtime import page
                    block = page(self, session, facts, calls, offer, arti_mind.load_prefs(base), goals)
                else:
                    block = arti_mind.render_block(arti_mind.load_prefs(base), goals,
                                               game=MIND_GAME, day=day, tags=tags,
                                               max_bytes=arti_mind.BLOCK_MAX_BYTES * scale,
                                               projection_scale=scale,
                                               yesterday=arti_mind.yesterday_line(base, MIND_GAME, day),
                                               yesterday_dates=arti_mind.yesterday_dates(base, MIND_GAME, day),
                                               calendar=getattr(self, "_live7_calendar", ()), native_targets=targets)
        except Exception as exc:  # noqa: BLE001 - the mind never blocks a boundary
            self._journal(dict(ev="mind_block_error", error=type(exc).__name__))
            return
        if block is not None:
            offer["mind"] = block
            if (getattr(self, '_config', None) or {}).get('stream_game_notes'):
                from arti_runtime import stream_notes  # ADR 232 part A: one line, one place on her page
                stream_notes.drop_help_duplicates(offer, getattr(self, "_said_to_you", ()),
                                                  getattr(self, "_said_lines", None))

    def _live7_mind_decision(self, session, decision, choice, request):
        """Journal fields for one model decision; overwrites mind/now.json. A missing why stays null."""
        if (getattr(self, '_config', None) or {}).get('live7_fact_line_enabled'):
            # ADR 232 part C: the fact line trims the arrival line's tail, so the exact compare below never matched
            # (a session: every page said it was the first visit today); the same arrival is the same visit.
            from arti_runtime.place_page import consume_visit
            consume_visit(self, request, choice)
        pending=getattr(self,"_live7_visit_pending",None)
        if (pending and not pending.get("sample") and pending["scope"]==getattr(self,"_live7_visit_scope",None)
                and ((request.skill_offer or {}).get("day") or {}).get("kunjungan")==pending["line"]):
            # Enrichment/preflight/telemetry may rebuild a page. Consume its visit
            # only after the model has received it and returned a decision.
            self._live7_visits=pending["visits"]
            self._live7_visit_pending=None
            from arti_runtime.place_page import remember_visit
            try:
                remember_visit(self._live7_mind_base(),pending["scope"],pending["location"],pending["date"],pending["clock"])
            except (OSError,ValueError) as exc:
                self._journal(dict(ev="visit_memory_error",error=type(exc).__name__))
        import arti_mind
        why, clipped = arti_mind.clip_why(getattr(decision, "speech", None))
        goal_ref = getattr(decision, "goal_ref", None)
        mind = (request.skill_offer or {}).get("mind") or {}
        today = (mind.get("tujuan") or {}).get("today") or []
        known = {g["id"] for rows in (mind.get("tujuan") or {}).values() for g in rows}
        if (saved := getattr(request, 'live7_day_plan', None)) is not None and goal_ref == f'rencana:{saved.target_day}':
            # Only the id on the actual provider page is eligible.
            try:
                render = getattr(self._provider, '_render_messages', None)
                messages = render(request) if callable(render) else S.render_skill_messages(request)
                shown = (json.loads(messages[1]['content']).get('mind') or {}).get('rencana_hari_ini')
                if shown and shown['id'] == goal_ref:
                    known.add(goal_ref)
            except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                pass
        fields = dict(why=why, goal_ref=goal_ref, why_src="model")
        if self._config.get("live7_reactive_play_enabled") or self._config.get("stardew_initiative_game_first"):
            from arti_runtime.reactive_play import capture_page, page_values
            capture_page(self, session, request)
        if self._config.get("live7_reactive_play_enabled"):
            offer = request.skill_offer or {}
            entry = next((r for r in offer.get("skills", ()) if r.get("name") == getattr(choice, "name", None)), {})
            Live7MomentsMixin._live7_intent_moment(self, session, why,
                skill=getattr(choice, "name", None), arg=getattr(choice, "arg", None),
                eta=getattr(choice, "eta_sec", entry.get("eta_sec", entry.get("eta"))), goal_ref=goal_ref,
                page=page_values(offer), place=(getattr(getattr(request, "snapshot", None), "motor", None)
                                               and request.snapshot.motor.location))
        else:
            Live7MomentsMixin._live7_intent_moment(self, session, why)
        if clipped:
            fields["why_clipped"] = True
        if goal_ref is not None:
            fields["goal_ref_known"] = goal_ref in known
        self._live7_mind_last = dict(fields)
        if why:
            whys = getattr(self, "_live7_mind_whys", None)
            if whys is None:
                whys = self._live7_mind_whys = []
            whys.append(why)
            del whys[:-24]
        base = self._live7_mind_base()
        if base is not None:
            from scripts.live7.native_facts import pose_readback
            game_time, observed_ts, fresh = pose_readback(self, session)
            try:
                if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
                    _mind_scope = getattr(self, "_live7_discovery_scope", None)
                    if isinstance(_mind_scope, str) and _mind_scope.strip():
                        goals = arti_mind.load_goals(base, MIND_GAME, scope=_mind_scope, scoped=True)
                        from arti_runtime.fact_line import enabled, plan_views
                        if enabled(self._config):
                            goals = plan_views(goals, getattr(self, "_live7_calendar", ()))
                        authored = {g["id"]: g["text"] for g in goals}
                        arti_mind.write_now(base, game=MIND_GAME, why=why, goal_ref=goal_ref,
                                            goal_today=[authored.get(g["id"], g["text"].split(" [", 1)[0]) for g in today],
                                            goal_dates=[arti_mind.dated_goal(g, MIND_GAME, getattr(self, "_live7_calendar", ()))
                                                        for g in goals if g["id"] in known],
                                            game_time=game_time, observed_ts=observed_ts,
                                            intent_date=mind_label(session.campaign.date),
                                            calendar=getattr(self, "_live7_calendar", ()),
                                            native_facts=dict(self._live7_native_facts(session,
                                                self._skill_current_facts() if fresh else None, fresh=fresh),
                                                action=dict(status="pending", skill=getattr(choice, "name", None))),
                                            scope=_mind_scope, scoped=True)
                        if (getattr(self, '_config', None) or {}).get('live7_fact_line_enabled'):
                            arti_mind.note_doing(base, game=MIND_GAME, chosen_at=game_time,
                                                 doing=dict(skill=getattr(choice, "name", None), arg=getattr(choice, "arg", None)),
                                                 scope=_mind_scope, scoped=True)
                else:
                    goals = arti_mind.load_goals(base, MIND_GAME)
                    from arti_runtime.fact_line import enabled, plan_views
                    if enabled(self._config):
                        goals = plan_views(goals, getattr(self, "_live7_calendar", ()))
                    authored = {g["id"]: g["text"] for g in goals}
                    arti_mind.write_now(base, game=MIND_GAME, why=why, goal_ref=goal_ref,
                                        # The request can outlive its source file. Recover only
                                        # our rendering suffix; authored goal text forbids '['.
                                        goal_today=[authored.get(g["id"], g["text"].split(" [", 1)[0]) for g in today],
                                        goal_dates=[arti_mind.dated_goal(g, MIND_GAME, getattr(self, "_live7_calendar", ()))
                                                    for g in goals if g["id"] in known],
                                        game_time=game_time, observed_ts=observed_ts,
                                        intent_date=mind_label(session.campaign.date),
                                        calendar=getattr(self, "_live7_calendar", ()),
                                        native_facts=dict(self._live7_native_facts(session,
                                            self._skill_current_facts() if fresh else None, fresh=fresh),
                                            action=dict(status="pending", skill=getattr(choice, "name", None))))
                    if (getattr(self, '_config', None) or {}).get('live7_fact_line_enabled'):
                        # What she chose, and where a chosen trip goes, beside where her body is.
                        arti_mind.note_doing(base, game=MIND_GAME, chosen_at=game_time,
                                             doing=dict(skill=getattr(choice, "name", None), arg=getattr(choice, "arg", None)))
            except OSError as exc:
                fields["now_write_error"] = type(exc).__name__
            if (getattr(self, "_config", None) or {}).get("voice_game_memory_enabled"):
                from arti_runtime import experience_digest  # ADR 228: after each answered decision page
                experience_digest.live7_write(self, session, place=getattr(getattr(getattr(
                    request, "snapshot", None), "motor", None), "location", None))
        return fields

    def _live7_native_facts(self, session, facts=None, *, fresh=True, pending=None):
        from scripts.live7.native_facts import readback, state_readback, STATE_FACT_KEYS
        from arti_runtime.plan_dates import calendar_context
        result = readback(session, facts, fresh=fresh, pending=pending)
        last = next((r for d in reversed(getattr(session.campaign, "days", ())) for r in reversed(d.get("skills", ()))), {})
        result["names"] = dict(getattr(self, "_live7_native_names", {}), **((last.get("evidence") or {}).get("names") or {}))
        result["place_names"] = dict(getattr(self, "_live7_native_place_names", {}))
        result["scene_state"] = getattr(session, "_persona_scene_state", None) if fresh else None
        from arti_runtime.goal_facts import selected
        target_stamp = getattr(self, "_live7_goal_targets_stamp", 0)
        targets = [dict(f) for f in getattr(self, "_live7_goal_targets", ())]
        texts = list(getattr(self, "_live7_goal_texts", ())) + [getattr(self, "_live7_mind_last", {}).get("why") or ""]
        result.update(state_readback(self) if fresh else dict.fromkeys(STATE_FACT_KEYS))
        own = result.get("own_state") or {}
        for target in targets:
            if target["kind"] == "money" and target["name"] == "wallet":
                wallet = own.get("money") or {}
                if type(wallet.get("value")) is int:
                    target.update(value=wallet["value"], observed_ts=wallet["observed_ts"])
            elif target["kind"] == "item" and fresh and getattr(facts, "inventory_current", False):
                target.update(value=facts.inventory.get(target["name"], 0),
                              observed_ts=(own.get("inventory") or {}).get("observed_ts", target_stamp))
            elif target["kind"] == "harvest":
                anchor = getattr(self, "_live7_harvest_anchor", None)
                if anchor is not None and anchor[0] is session.campaign:
                    from arti_runtime.progress_counts import harvested_items
                    target.update(value=anchor[2] + harvested_items(session.campaign.days))
        current_targets = [f for f in targets if 0 <= time.time() - f.get("observed_ts", target_stamp) <= 45]
        result["goal_targets"] = dict(value=selected(texts, current_targets), observed_ts=target_stamp,
                                      source="native_scoped_progress") if fresh else None
        if result["menu_container"] is not None:
            session._persona_last_container = dict(result["menu_container"], relation="past_observation")
        result["last_container"] = getattr(session, "_persona_last_container", None)
        calendar = calendar_context(getattr(self, "_live7_calendar", ()), game=MIND_GAME,
                                    day=mind_day(session.campaign.date) if fresh else None)
        if calendar:
            result["calendar"] = calendar
        from arti_runtime.native_story import recent as story_recent
        if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
            result["native_story"] = story_recent(self._live7_mind_base(),
                getattr(self, "_live7_native_story_scope", None) or getattr(self, "_live7_discovery_scope", None), mind_day(session.campaign.date),
                calendar=getattr(self, "_live7_calendar", ()), mind_scoped=True)
        else:
            result["native_story"] = story_recent(self._live7_mind_base(),
                getattr(self, "_live7_native_story_scope", None) or getattr(self, "_live7_discovery_scope", None), mind_day(session.campaign.date),
                calendar=getattr(self, "_live7_calendar", ()))
        from arti_runtime.fact_line import enabled, Witnesses
        if enabled(getattr(self, "_config", None)):
            witnessed = Witnesses(getattr(self, "_live7_fact_line_catalog", None))
            result["native_story"] = [dict(text=witnessed.line(r), source=r["source"])
                                      for r in witnessed.matching(("event_watched", "dialogue_read"))]
        result["harvest"] = getattr(session, "_persona_harvest", None) if fresh else None
        quest_status = getattr(self, "_live7_quest_status", None)
        if isinstance(quest_status, dict):  # fix60A (ADR 87): a dated memory of the newest native quest log
            result["quest_status"] = dict(quest_status)
        # ADR 93: the day facts of the newest decision (weather, forecast, season, dated events) and the native progress
        # lines (money earned, skills, tools, Community Center, mines) as dated memories, like quest_status.
        day_facts = getattr(self, "_live7_day_facts", None)
        if isinstance(day_facts, dict):
            result["weather"] = dict(value={k: day_facts.get(k) for k in ("weather", "weather_tomorrow", "musim", "kalender")
                                            if day_facts.get(k) is not None},
                                     date=day_facts.get("date"), observed_ts=day_facts.get("observed_ts"),
                                     source="native_day_context", relation="memory")
        progress_lines = getattr(self, "_live7_progress_lines", None)
        if isinstance(progress_lines, dict):
            result["progress_memory"] = dict(progress_lines)
        read = {k: v for k, v in (getattr(getattr(session.campaign, "live", None), "read_today", None) or {}).items()
                if k in ("weather", "fortune")}
        if read:  # fix63B (a45 T141): what the TV showed her reaches speech; held to its game date (ADR 95)
            result["morning_read"] = dict(value=read, date=mind_label(session.campaign.date),
                                          source="native_tv_text", relation="memory")
        ground = getattr(getattr(session.campaign, "live", None), "ground_items", None)
        if ground is not None and ground.rows:
            hot, cold = ground.facts(inventory=dict(facts.inventory) if fresh and getattr(facts, "inventory_current", False)
                                     else None, texts=texts)
            # fix59A (ADR 86): a memory of verified receipts, not a fresh reading; each line states when it was seen.
            result["ground_items"] = dict(value=hot + cold, source="native_ground_receipts", relation="memory")
        chests = getattr(getattr(session.campaign, "live", None), "chest_memory", None)
        if chests is not None and (line := chests.line()):
            # FIX76A (a55 T135, T183 "[recorded words omitted]", both geodes in the chest): what her chests held never reached speech. A dated memory.
            result["chest_memory"] = dict(value="di peti: " + line, source="native_chest_read", relation="memory")
        if self._config.get("live7_reactive_play_enabled") or self._config.get("stardew_initiative_game_first"):
            from arti_runtime.reactive_play import native_page
            result.update(native_page(self, session, facts, fresh=fresh))
        return result

    def _live7_mind_heartbeat(self, session, stalled_s, facts, motor):
        """Controller state on now.json (fix10c, a12): game clock + a generic blocked flag, never a decision.

        Written on a change of the clock/place/blocked state or every MIND_HEARTBEAT_SEC wall seconds."""
        import arti_mind
        base = self._live7_mind_base()
        if base is None:
            return
        wall = time.time()
        blocked = None
        if stalled_s >= MIND_BLOCKED_AFTER_SEC:
            blocked = dict(since_ts=wall - stalled_s, menu_open=getattr(motor, "active_menu", None) is not None)
        from scripts.live7.native_facts import pose_readback
        game_time, observed_ts, fresh = pose_readback(self, session, motor or facts, wall=wall)
        if not fresh and blocked is not None:
            blocked["menu_open"] = False
        native = self._live7_native_facts(session, facts, fresh=fresh)
        from arti_runtime.time_grounding import fact_values
        key = (json.dumps([game_time, fact_values(native)], sort_keys=True, default=str), blocked is None, (blocked or {}).get("menu_open"))
        if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
            _mind_scope = getattr(self, "_live7_discovery_scope", None)
            if not (isinstance(_mind_scope, str) and _mind_scope.strip()):
                return
            key = (_mind_scope, key)
        last = getattr(self, "_live7_mind_beat", None)
        if last is not None and last[0] == key and wall - last[1] < MIND_HEARTBEAT_SEC:
            return
        try:
            if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
                arti_mind.mark_state(base, game=MIND_GAME, game_time=game_time, blocked=blocked, observed_ts=observed_ts,
                                     native_facts=native, scope=_mind_scope, scoped=True)
            else:
                arti_mind.mark_state(base, game=MIND_GAME, game_time=game_time, blocked=blocked, observed_ts=observed_ts, native_facts=native)
            self._live7_mind_beat = (key, wall)
        except Exception as exc:  # noqa: BLE001 - the mind never blocks the watchdog
            self._journal(dict(ev="mind_heartbeat_error", error=type(exc).__name__))

    def live7_bedtime_reflection(self, complete=None, *, defer=False):
        """One reflection per day change, after the session ended (she is asleep; latency is free).

        fix10e (a14 d9-d10, 0/34 goal_refs): a verified bed sleep and every honest lost day (pass-out, recovery
        loop, home retries exhausted) reflect; a lost day carries its honest fact, e.g. "pingsan jam 02:00".

        ``complete(messages, schema) -> str`` defaults to the decision provider's reflection lane. Returns the
        journal row; a failure journals ``mind_reflection_failed`` (watcher alert) and invents no goals.
        ``defer`` (fix54H) persists the collected facts instead; the next day's runner reflects them."""
        import arti_mind
        session = getattr(self, "_skill_session", None)
        c = getattr(session, "campaign", None)
        base = self._live7_mind_base()
        reason = getattr(session, "reason", None)
        if (base is None or c is None or getattr(c, "part", None) != "live"
                or reason not in (SLEEP_VERIFIED_REASON, *DAY_LOST_REASONS) or getattr(self, "_live7_reflected", None) == c.date):
            return None
        day = mind_day(c.date)
        if day is None:
            return None
        self._live7_reflected = c.date
        if (getattr(self, "_config", None) or {}).get("stardew_live7_goal_ladder", False) is True:
            if reason != SLEEP_VERIFIED_REASON:
                return None  # no invented night plan after an unverified sleep
            from arti_runtime.goal_ladder_runtime import night
            return night(self, session, complete)
        rows = list(c.days[-1].get("skills", ())) if c.days else []  # this day's ledger rows only
        # ADR 99 (a46 d17 J47: the night after her last parsnip, the row read "[recorded remark omitted]"): what was picked.
        from arti_runtime.progress_counts import harvest_names
        verified = [dict(ref="r%d" % i, text="%s%s%s: %s" % (r.get("skill"), " " + str(r["arg"]) if r.get("arg") is not None else "",
                                                         harvest_names(r), r.get("reason")))
                    for i, r in enumerate(rows, 1) if r.get("verified") is True]
        known_scope=getattr(self,"_live7_discovery_scope",None)
        discoveries=(arti_mind.recent_discoveries(base,MIND_GAME,limit=16,day=day,scope=known_scope)
                     if known_scope is not None else [])
        for discovery in discoveries:
            verified.append(dict(ref=discovery["id"], text=discovery["text"]))
        from arti_runtime.native_story import reflection_facts as story_reflection
        if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
            verified.extend(story_reflection(base, getattr(self, "_live7_native_story_scope", None) or known_scope,
                                            day, calendar=getattr(self, "_live7_calendar", ()), mind_scoped=True))
        else:
            verified.extend(story_reflection(base, getattr(self, "_live7_native_story_scope", None) or known_scope,
                                            day, calendar=getattr(self, "_live7_calendar", ())))
        effects = dict(Counter(r.get("skill") for r in rows if r.get("verified") is True))
        places = list(dict.fromkeys(h.get("location") for h in c.live.history if h.get("location")))
        wallet = c.live.last_wallet or {}
        # fix13c: the day's progress surface, quests and letters as citable facts; a goal is done only with one of
        # these refs or a verified row (arti_mind_reflect), and unfinished goals carry forward with their evidence.
        from arti_stardew_world import reflection_facts
        snap, start, letters, quests = getattr(self, "_live7_progress_last", None), None, [], []
        anchor = getattr(self, "_live7_harvest_anchor", None)
        if snap is not None and anchor is not None and anchor[0] is c:
            from arti_runtime.progress_counts import harvested_items
            snap = dict(snap, crops=dict(snap.get("crops") or {},
                                        harvested=anchor[2] + harvested_items(c.days)))
            try:
                arti_mind.save_progress(base, MIND_GAME, scope=anchor[1], day=day,
                                        day_label=mind_label(c.date), snapshot=snap)
            except (OSError, ValueError) as exc:
                self._journal(dict(ev="progress_memory_error", error=type(exc).__name__))
            self._live7_progress_last = snap
        if known_scope:
            try:
                start = next((r.get("start") for r in arti_mind.progress_days(base, MIND_GAME, scope=known_scope)
                              if r.get("day") == day), None)
                records = lambda kind: [dict(r, id=r["item_id"]) for r in sorted(  # noqa: E731
                    arti_mind.ledger_rows(base, MIND_GAME, scope=known_scope, kind=kind),
                    key=lambda r: (r.get("status_day", 0), r.get("first_day", 0)))]
                letters = [r for r in records("letter") if r.get("text")]
                quests = [r for r in records("quest") if r.get("status") != "not_in_quest_log" or r.get("status_day") == day]
            except (OSError, ValueError) as exc:
                self._journal(dict(ev="progress_memory_error", error=type(exc).__name__))
        progress = reflection_facts(snap, start, letters=letters, quests=quests,
                                    wallet=wallet.get("money") if type(wallet.get("money")) is int else None)
        # FIX72D (a53 d5 J1358 -> d6 J1767: "[recorded remark omitted]", in the chest that evening, no row the night after):
        # what she put away. FIX73E: what she walked past today. FIX75B (ADR 129): the Community Center rows come last and
        # bounded (bundle_knowledge.BEDTIME_ROWS), so PROGRESS_MAX (tail) and the byte trim (head) take them before quests and crops.
        chests, names = getattr(c.live, "chest_memory", None), dict(getattr(c.live, "item_names", ()))
        if chests is not None:  # a night's reflection never fails over a fact it can do without
            progress.extend(chests.reflection(names))
        forage = getattr(c.live, "forage_memory", None)
        if forage is not None:  # FIX73E: goals are born at bedtime, and what she walked past today is a citable row there
            from arti_runtime.place_names import place_label
            label = lambda where: place_label(where) or where  # noqa: E731
            progress.extend(forage.reflection(c.date, place=label) + forage.season_row(c.date, place=label))
        # ADR 45: her own plantings (ready / growing / dead, absolute dates) are citable bedtime facts, so an
        # unfinished harvest can carry into tomorrow's goals by her own choice.
        from scripts.live7.live7_common import own_crop_reflection_facts
        progress.extend(own_crop_reflection_facts(c.live.crop_memory.crops, c.date)
                        or [dict(ref="c:none", text="tanaman yang kutanam sendiri: tidak ada catatan")])  # ADR 99: absence is a fact
        from arti_runtime.bundle_knowledge import reflection as bundle_reflection
        known = tuple(getattr(self, "_live7_bundle_knowledge", ())) + (None, None, None)
        progress.extend(bundle_reflection(known[0], known[1], known[2], chests.stored(names) if chests is not None else None))
        # Final verified harvest receipts can postdate the last decision boundary.
        final_targets = [dict(f) for f in getattr(self, "_live7_goal_targets", ())]
        for fact in final_targets:
            if fact["kind"] == "harvest":
                fact["value"] = ((snap or {}).get("crops") or {}).get("harvested")
            elif fact["kind"] == "money" and fact["name"] == "wallet":
                fact["value"] = wallet.get("money")
        if reason != SLEEP_VERIFIED_REASON:
            progress.insert(0, dict(ref="p:day_lost", text=day_lost_fact(reason, getattr(self, "_live7_passout_clock", None))))
        if snap is not None:
            self._journal(dict(ev="progress_snapshot", game=MIND_GAME, day=day, date=mind_label(c.date), phase="bedtime",
                               progress=snap))
        inputs = dict(game=MIND_GAME, day=day, day_label=mind_label(c.date),
                      verified=verified, whys=list(getattr(self, "_live7_mind_whys", None) or ()),
                      progress=progress, places=places, effects=effects, snapshot=snap,
                      native_targets=final_targets,
                      calendar=list(getattr(self, "_live7_calendar", ()) or ()),
                      records=dict(letters=[r["id"] for r in letters][-8:],
                                   quests=[[r["id"], r.get("status")] for r in quests][-12:]))
        if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
            inputs["scope"] = known_scope
        from arti_runtime.fact_line import enabled, reflection, witnessed_catalog
        if enabled(self._config):
            catalog = getattr(self, "_live7_fact_line_catalog", None)
            inputs["progress"] = reflection(inputs["progress"], catalog)
            # Native discovery descriptions/story diffs are not observations of
            # their prose. Verified motor outcomes above remain her own history.
            inputs["verified"] = [r for r in verified if re.fullmatch(r"r\d+", r["ref"])]
            inputs["calendar"] = list((witnessed_catalog(catalog, self._config) or {}).get("calendar") or ())
        if defer:
            # ADR 72 (a39 wake -> first admit 36-121 s): the facts are collected now; the provider call runs
            # in the next day's runner, off its decision lanes (live7_start_pending_reflection).
            return self._live7_defer_reflection(base, inputs)
        return self._live7_run_reflection(base, inputs, complete)

    def _live7_run_reflection(self, base, inputs, complete=None, **extra):
        import arti_mind_reflect
        from arti_runtime.fact_line import enabled, reflection, witnessed_catalog
        if enabled(getattr(self, "_config", None)):
            # Deferred inputs may have been produced before the trial was on.
            catalog = getattr(self, "_live7_fact_line_catalog", None)
            inputs = dict(inputs, progress=reflection(inputs.get("progress", ()), catalog),
                verified=[r for r in inputs.get("verified", ()) if re.fullmatch(r"r\d+", r["ref"])],
                calendar=list((witnessed_catalog(catalog, self._config) or {}).get("calendar") or ()))
            inputs["seen_calendar"] = True
        if complete is None:
            provider = getattr(self, "_provider", None)
            complete = getattr(provider, "complete_reflection", None)
        if complete is None:
            row = dict(ev="mind_reflection_failed", game=inputs["game"], day=inputs["day"], date=inputs["day_label"],
                       code="no_reflection_lane", alert=True, **extra)
            self._journal(row)
            return row
        if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
            _mind_scope = inputs.get("scope") or (
                None if extra.get("deferred") else getattr(self, "_live7_discovery_scope", None))
            if not (isinstance(_mind_scope, str) and _mind_scope.strip()):
                row = dict(ev="mind_reflection_failed", game=inputs["game"], day=inputs["day"], date=inputs["day_label"],
                           code="scope_unavailable", alert=True, **extra)
                self._journal(row)
                return row
            _scoped_inputs = {k: v for k, v in inputs.items() if k != "scope"}
            row = dict(arti_mind_reflect.reflect(complete, base, scope=_mind_scope, scoped=True, **_scoped_inputs), **extra)
        else:
            row = dict(arti_mind_reflect.reflect(complete, base, **inputs), **extra)
        self._journal(row)
        if (getattr(self, "_config", None) or {}).get("voice_game_memory_enabled"):
            from arti_runtime import experience_digest  # ADR 228: the day's end, after her own summary
            experience_digest.live7_write(self, getattr(self, "_skill_session", None))
        return row

    def _live7_defer_reflection(self, base, inputs):
        import arti_mind
        from arti_runtime import ipc
        folder = arti_mind.game_dir(base, inputs["game"]) / REFLECTION_PENDING_DIR
        name = "day-%06d.json" % inputs["day"]
        if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
            _mind_scope = inputs.get("scope") or getattr(self, "_live7_discovery_scope", None)
            if isinstance(_mind_scope, str) and _mind_scope.strip():
                from arti_runtime import mind_scope
                name = "day-%06d-%s.json" % (inputs["day"], mind_scope.scope_hash(_mind_scope)[:12])
        try:
            folder.mkdir(parents=True, exist_ok=True)
            ipc.write_json(folder / name, dict(v=1, deferred_at=time.time(), inputs=inputs), critical=True, fsync=True)
        except (OSError, TypeError, ValueError) as exc:
            # Never lose the reflection to a write failure: run it now (the old, slower morning).
            self._journal(dict(ev="mind_reflection_defer_error", day=inputs["day"], error=type(exc).__name__))
            return self._live7_run_reflection(base, inputs)
        row = dict(ev="mind_reflection_deferred", game=inputs["game"], day=inputs["day"], date=inputs["day_label"],
                   pending=name)
        self._journal(row)
        return row

    def live7_start_pending_reflection(self, complete=None):
        """fix54H: run the previous nights' deferred reflections on a daemon thread after this day's first
        admitted decision. The provider's reflection lane never holds a decision lane (voice-critical isolation)."""
        if (getattr(self, "_config", None) or {}).get("stardew_live7_goal_ladder", False) is True:
            return None  # retain legacy pending files; never race two goal writers
        import arti_mind
        import threading
        base = self._live7_mind_base()
        if base is None or getattr(self, "_live7_reflection_thread", None) is not None:
            return None
        folder = arti_mind.game_dir(base, MIND_GAME) / REFLECTION_PENDING_DIR
        if not folder.is_dir() or not any(folder.glob("day-*.json")):
            return None
        pending = sorted(folder.glob("day-*.json"))

        def work():
            # fix55C / ADR 78 (REVIEW2 #6): the pending file is deleted only after a successful reflection. Any failure
            # (rate limit, hourly cap, physical_start_guard, timeout, closed lane) keeps it with an attempt count;
            # after REFLECTION_MAX_ATTEMPTS it is set aside (kept, never retried) with a named alert.
            for path in pending:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    inputs = data["inputs"]
                    deferred_at = float(data["deferred_at"])
                    attempts = int(data.get("attempts", 0))
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    self._journal(dict(ev="mind_reflection_failed", game=MIND_GAME, code="deferred_unreadable",
                                       error=type(exc).__name__, pending=path.name, alert=True))
                    self._live7_set_aside_pending(path, "unreadable")
                    continue
                if self._live7_already_reflected(base, inputs):
                    # A success whose delete failed: never reflect the same night twice.
                    self._journal(dict(ev="mind_reflection_deferred_skipped", game=MIND_GAME, day=inputs.get("day"),
                                       pending=path.name, reason="already_reflected"))
                    path.unlink(missing_ok=True)
                    continue
                attempts += 1
                try:
                    row = self._live7_run_reflection(base, inputs, complete, deferred=True, attempt=attempts,
                                                     deferred_s=round(max(0.0, time.time() - deferred_at), 1))
                except Exception as exc:  # noqa: BLE001 - a reflection never stops the day
                    row = dict(ev="mind_reflection_failed", game=MIND_GAME, day=inputs.get("day"),
                               code=type(exc).__name__, deferred=True, attempt=attempts, alert=True)
                    self._journal(row)
                if row.get("ev") == "mind_reflection":
                    path.unlink(missing_ok=True)
                    continue
                if attempts >= REFLECTION_MAX_ATTEMPTS:
                    self._journal(dict(ev="mind_reflection_failed", game=MIND_GAME, day=inputs.get("day"),
                                       code="deferred_attempts_exhausted", attempts=attempts, pending=path.name,
                                       alert=True))
                    self._live7_set_aside_pending(path, "exhausted")
                    continue
                try:
                    from arti_runtime import ipc
                    ipc.write_json(path, dict(data, attempts=attempts, last_code=row.get("code")),
                                   critical=True, fsync=True)
                except (OSError, TypeError, ValueError) as exc:
                    self._journal(dict(ev="mind_reflection_defer_error", day=inputs.get("day"),
                                       error=type(exc).__name__, phase="attempt_count"))
                if getattr(getattr(self, "_provider", None), "_closed", False) is True:
                    return  # shutdown closed the lane: the next runner retries the rest

        thread = threading.Thread(target=work, name="live7-mind-reflection", daemon=True)
        self._live7_reflection_thread = thread
        thread.start()
        return thread

    def _live7_already_reflected(self, base, inputs) -> bool:
        import arti_mind
        if (getattr(self, "_config", None) or {}).get("live7_mind_scope", False) is True:
            _mind_scope = inputs.get("scope")
            if not (isinstance(_mind_scope, str) and _mind_scope.strip()):
                return False
            try:
                rows = arti_mind.load_journey(base, inputs.get("game") or MIND_GAME, scope=_mind_scope, scoped=True)
            except Exception:  # noqa: BLE001 - unknown means reflect
                return False
            return any((r.get("day") == inputs.get("day") or r.get("date") == str(inputs.get("day_label"))
                        or r.get("id") == str(inputs.get("day_label"))) and isinstance(r.get("reflection"), dict)
                       for r in rows)
        try:
            rows = arti_mind.load_journey(base, inputs.get("game") or MIND_GAME)
        except Exception:  # noqa: BLE001 - unknown means reflect
            return False
        return any(r.get("id") == str(inputs.get("day_label")) and isinstance(r.get("reflection"), dict)
                   for r in rows)

    def _live7_set_aside_pending(self, path, why):
        """Keep the night's facts on disk for the record, out of the ``day-*.json`` retry glob."""
        try:
            path.replace(path.with_name(path.name + "." + why))
        except OSError as exc:
            self._journal(dict(ev="mind_reflection_defer_error", pending=path.name, error=type(exc).__name__,
                               phase="set_aside"))

    def live7_join_pending_reflection(self, timeout):
        thread = getattr(self, "_live7_reflection_thread", None)
        if thread is None:
            return True
        W.call('poll', 127, thread.join, timeout)
        return not thread.is_alive()

    def _live7_observe_forage(self, pin, targets, motor):
        """Called at the existing fresh scene gate; sightings guide ARTI, never authorize pickup."""
        if not self._live7_active():
            return
        c = self._skill_session.campaign
        epoch = tuple(getattr(pin, k, None) for k in (
            "runtime_instance_id", "command_session_id", "observation_session_id", "player_id", "screen_id"))
        if c is None or epoch != c.epoch or self._date_tuple(motor) != c.date or motor.location != pin.location:
            return
        self._live7_forage_note(c, pin.location, targets)  # FIX73E: the walk is where she passes it (a54 d19 J420, d20 J1066)
        self._live7_seen_note(c, pin.location, targets, tuple(motor.tile) if motor.tile is not None else None)
        entries = visible_forage(SimpleNamespace(motor=motor), {"targets": targets}, c.live.refs)
        if entries:
            c.live.forage_sightings.pop(pin.location, None)
            c.live.forage_sightings[pin.location] = [dict(ref=e["ref"], name=e.get("name", "forage")) for e in entries[:4]]
            while len(c.live.forage_sightings) > 8:
                del c.live.forage_sightings[next(iter(c.live.forage_sightings))]

    def _live7_forage_walk(self, pin, motor, facts):
        """FIX73F: the hop a chosen forage walks, or None when no walk is open. Same plan, legs and repair as a trip (ADR 125).

        a54 d12: an own walker of 8-tick presses counted tile reads, and stopped at a wall corner a press before it cleared (J1052).
        The target stays pinned: what she walks to does not change with each window. Gone only when read near; the scene ends at 40 tiles."""
        goal, session = self._active, self._skill_session
        run = session.current if session is not None else None
        if (run is None or not self._live7_active() or session.campaign is None or run.call.name != "forage"
                or run.state.get("wave1_driver") or run.state.get("route_clear") or not self._run_owns_goal(run, goal)):
            return None  # S21's own leg and a clear leg keep their own targets
        from arti_stardew_interact import FORAGE_REACH
        from arti_runtime.offer_withhold import route_refs
        look = run.state.setdefault("live7_forage_look", dict(location=pin.location, inputs=0))
        here = tuple(motor.tile)
        grid = self._nav_grid(pin)
        excluded = session.campaign.live.refs | route_refs(self, session, pin.location, here, grid)
        seen = {e["ref"]: e for e in visible_forage(SimpleNamespace(motor=motor), dict(targets=facts), excluded)}
        pinned = look.get("target")
        if pinned is not None and pinned["ref"] in seen:
            pinned = look["target"] = seen[pinned["ref"]]
        elif pinned is None or max(abs(a - b) for a, b in zip(pinned["tile"], here)) <= FORAGE_REACH:
            if not seen:
                look.pop("target", None)
                return []
            near = lambda e: (abs(e["tile"][0] - here[0]) + abs(e["tile"][1] - here[1]), e["ref"])
            pinned = look["target"] = min(seen.values(), key=near)
        goal.home_route_state["route_reaches"] = list(pinned["reach_tiles"])
        return self._hop_entries(goal, pin, here, facts, grid, [pinned], (pin.location, pinned["ref"]))

    def _live7_seen_target(self, run, location, here=None, grid=None):
        """ADR 136: the sighting a chosen `jalan_ke` walks to, pinned at its first read, or None for a walk to a plain tile."""
        session = self._skill_session
        if run.call.name != "jalan_ke" or not run.state.get("live7") or session is None or session.campaign is None:
            return None
        pinned = run.state.get("route_seen_walk")
        if pinned is None:
            from arti_runtime.offer_withhold import route_refs
            row = session.campaign.live.seen_objects.at(location, run.call.tile)
            if row is None or row["ref"] in route_refs(self, session, location, here, grid):
                return None
            pinned = run.state["route_seen_walk"] = dict(ref=row["ref"], tile=list(row["tile"]), location=location, inputs=0, reads=0)
        return pinned

    def _live7_seen_walk(self, pin, motor, facts):
        """ADR 136: a walk to a sighting takes the trip's hop (ADR 125, as FIX73F's forage): same plan, legs, reads and caps."""
        goal, session = self._active, self._skill_session
        run = session.current if session is not None else None
        if (run is None or run.call.name != "jalan_ke" or goal is None or not self._live7_active()
                or session.campaign is None or run.state.get("route_clear")
                or not self._run_owns_goal(run, goal)):
            return None
        here = S._wave1_xy(S._g(motor, "tile"))
        if here is None:
            return None
        grid = self._nav_grid(pin)
        pinned = self._live7_seen_target(run, pin.location, here, grid)
        if pinned is None:
            return None
        if pinned["location"] != pin.location:
            return []
        from arti_stardew_interact import blocked_tiles
        blocked = blocked_tiles(facts)
        reaches = [t for t in seen_stands(pinned["tile"]) if tuple(t) not in blocked] or seen_stands(pinned["tile"])
        goal.home_route_state["route_reaches"] = reaches
        entry = dict(ref="seen:%s:%d,%d" % (pin.location, *pinned["tile"]), tile=list(pinned["tile"]), kind=SEEN_WALK_KIND,
                     reach_tiles=reaches, revision=pinned["ref"])
        return self._hop_entries(goal, pin, here, facts, grid, [entry], (pin.location, pinned["ref"]))

    def _live7_seen_step(self, run, snap):
        """The walk's step, or None when this `jalan_ke` is not to a sighting. Beside the object is arrival: what is there is the next read's to say."""
        motor = snap.motor
        pin = S._g(snap, "water_context") or S._g(snap, "movement_context")
        grid = self._nav_grid(pin) if pin is not None else None
        pinned = self._live7_seen_target(run, motor.location, S._wave1_xy(S._g(motor, "tile")), grid)
        if pinned is None:
            return None
        last = S._g(S._g(snap, "working_memory"), "last_action")
        if last is not None and (S._g(last, "cleanup_confirmed") is False or S._g(last, "lifecycle") in ("uncertain", "interrupted")):
            return S.Step("fail", reason="action_unreconciled")
        if motor.location != pinned["location"]:
            return S.Step("fail", reason="seen_walk_left_map")
        if S._g(S._g(snap, "working_memory"), "last_inspect_status") != "current":
            return S.Step("decision", S.INSPECT)
        if list(motor.tile) in seen_stands(pinned["tile"]):
            return S.Step("done", reason="observed_approach_arrived")
        # ADR 175: once every stand has been read and refused by the repair
        # planner, another map survey cannot make this sighting reachable.
        # A newly observed open stand invalidates that old refusal.
        route = getattr(self._active, "home_route_state", {}).get("route_memory", {})
        closed = {tuple(t) for t in route.get("closed", ())}
        observed = {**route.get("seen", {}), **(grid or {})}
        if all(tuple(t) in closed and observed.get(tuple(t)) in ("#", "s", "b")
               for t in seen_stands(pinned["tile"])):
            return S.Step("fail", reason="seen_walk_no_open_path")
        if pinned["inputs"] >= SEEN_WALK_INPUTS:
            return S.Step("fail", reason="seen_walk_inputs_spent")
        move = S._approach(snap, lambda e: e.get("kind") in ("home_route_stand", SEEN_WALK_KIND))
        if move is None:
            # A villager on the leg hides it for a moment; a read, no input, before the walk is given up.
            if pinned["reads"] < FORAGE_WALK_READS:
                pinned["reads"] += 1
                return S.Step("decision", S.INSPECT)
            return S.Step("fail", reason="seen_walk_no_open_path")
        pinned["reads"] = 0
        pinned["inputs"] += 1
        return S.Step("decision", move)

    def _live7_forage_step(self, run, snapshot, context):
        """A model-chosen S21 walks to what she saw, then hands real offers to S21's unchanged verifier.
        Near is not reached: forage across a wall stays the walk's until a stand of it is open from here."""
        offers = forage_offers(snapshot, context, self._skill_session.campaign.live.refs)
        parents = forage_look_paths(snapshot, context)
        if offers and (not parents or any(tuple(t) in parents for e in offers for t in e["reach_tiles"])):
            return None
        motor = snapshot.motor
        state = run.state.setdefault("live7_forage_look", dict(location=motor.location, inputs=0))
        self._skill_session.campaign.live.forage_looked.add(state["location"])
        if motor.location != state["location"] or state["inputs"] >= FORAGE_LOOK_INPUTS:
            return S.Step("fail", reason="forage_not_offered:look_around_exhausted")
        move = S._approach(snapshot, lambda e: e.get("kind") == "home_route_stand")
        if move is None:
            if not visible_forage(snapshot, context, self._skill_session.campaign.live.refs) and "target" not in state:
                return S.Step("fail", reason="forage_not_offered:no_open_look_path")
            # A villager on the leg hides it for a moment (Part A's A23): a read, no input, before the walk is given up.
            if state.setdefault("reads", 0) < FORAGE_WALK_READS:
                state["reads"] += 1
                return S.Step("decision", S.INSPECT)
            return S.Step("fail", reason="forage_not_offered:no_open_look_path")
        state["reads"] = 0
        state["inputs"] += 1
        return S.Step("decision", move)

    def _live7_fish_step(self, run, snapshot):
        """Chosen fishing reaches local water with the existing rod staging and movement primitives."""
        if not run.state.get("live7") or run.call.name != "tangkap_ikan":
            return None
        memory = S._g(snapshot,"working_memory")
        last = S._g(memory,"last_action")
        if (S._g(memory,"last_inspect_status") != "current" or S._g(memory,"progress_marker") == "terminal"
                or S.menu_up(snapshot) or last is not None and (S._g(last,"cleanup_confirmed") is False
                or S._g(last,"lifecycle") in ("uncertain","interrupted"))):
            return None  # normal skill/UI uncertainty handling retains ownership
        if self._skill_finish_ready(self._active, SimpleNamespace(snapshot=snapshot)):
            return None
        context = self._skill_wave1_context(snapshot)
        if not context.get("ui_complete") or not context.get("inventory_complete"):
            return S.Step("fail", reason="fishing_facts_unavailable")
        from scripts.live7.capabilities import owned_rods
        rods = owned_rods(context) or S.ROD_QIDS  # legacy observations retain their proven path
        run.state["native_rod_qids"] = rods
        ready = self._skill_item_ready(run,self._active,snapshot,context,rods,"tool")
        if ready is not None:
            return ready
        if (context.get("fishing") or {}).get("water_edges") or S._g(snapshot,"reflex_fish_target"):
            return None  # existing approach, facing, native cast and catch readback
        from scripts.live7.capabilities import fishing_path
        path = fishing_path(snapshot,context,run.state.setdefault("fishing_search",{}))
        if not path or len(path)<2:
            return S.Step("fail",reason="no_reachable_native_fishing_edge")
        delta = tuple(b-a for a,b in zip(path[0],path[1]))
        return S.Step("decision",S.act("world_key.move_cardinal",dict(direction=S._DIRS[delta],ticks=8)))

    def _live7_refill_step(self, run, snapshot):
        """Reach/facing preparation from native water facts; use_tool and refill evidence stay native."""
        if not run.state.get("live7") or run.call.name != "isi_gembor":
            return None
        progress = S._g(S._g(snapshot, "working_memory"), "tool_work")
        if S._g(progress, "status") == "EFFECT_OBSERVED":
            return None
        if S._g(snapshot, "tool_work_target") is not None:
            return None
        context = self._skill_wave1_context(snapshot)
        targets = refill_targets(snapshot, context)
        if not targets:
            return S.Step("fail", reason="precondition:no_reachable_native_refill")
        if S._equipped(snapshot) != "(T)WateringCan":
            selection = S._select(snapshot, ("(T)WateringCan",))
            return S.Step("decision", selection) if selection else S.Step("fail", reason="precondition:can_not_selectable")
        here = S._wave1_xy(snapshot.motor.tile)
        adjacent = next((tile for tile, stands in targets if here in stands), None)
        if adjacent is not None:
            turn = S._face(snapshot, adjacent)
            pose = (here, S._g(S._g(snapshot, "equipped_tool"), "facing"), adjacent)
            if turn and run.state.get("live7_refill_pose") != pose:
                run.state["live7_refill_pose"] = pose
                return S.Step("decision", turn)
            return S._wait(run.state, "refill", "native_refill_target_not_offered", "Amati target isi air native", S.WAIT_S)
        from arti_stardew_goal_routes import open_path, open_paths
        from arti_stardew_interact import blocked_tiles
        blocked = blocked_tiles(context.get("targets", {}))
        parents = open_paths({t: v for t, v in context.get("nav_grid", {}).items() if t not in blocked}, here)
        paths = [path for _, stands in targets for tile in stands if (path := open_path(parents, tile)) and len(path) > 1]
        if not paths:
            return S.Step("fail", reason="precondition:no_reachable_native_refill")
        path = min(paths, key=len)
        delta = tuple(b - a for a, b in zip(here, path[1]))
        return S.Step("decision", S.act("world_key.move_cardinal", dict(direction=S._DIRS[delta], ticks=8)))

    def _live7_begin(self, session):
        from arti_stardew_part_a import Campaign
        facts = self._skill_current_facts()
        with self._lock:
            binding, pid = self._binding_view()
            motor = self._state.latest(pid) if pid is not None else None
            if (binding.status != "bound" or motor is None or self._active is None or facts is None
                    or not facts.inventory_current or self._date_tuple(motor) is None):
                return self._skill_end_locked(session, "held", SAFETY_STOP, "live7:identity_unavailable")
            c = Campaign(session.goal_text, self._date_tuple(motor), dict(facts.inventory),
                         tuple(self._active.play_identity), part="live")
            session.campaign = c
            c.morning()
            resume = self._config.get("stardew_live7_resume") or {}
            c.live.knockouts = list(resume.get("knockouts") or ())[-8:]
            c.live.recovery_history = [dict(r) for r in resume.get("recovery_history", [])[-8:]
                if isinstance(r, Mapping) and r.get("disposition") == "RECOVER"]
            night = resume.get("previous_night")
            if isinstance(night, dict) and tuple(night.get("next_date") or ()) == c.date:
                context, _, _ = self._live7_context(self._build_decision_snapshot(self._active))
                before = night.get("money_before")
                after = context.get("money") if (context.get("ui_complete") and context.get("inventory_complete")
                    and tuple((context.get("identity") or ())[:5]) == c.epoch) else None
                # ADR 93/198: a verified night carries the same wallet facts as a lost one; native bed evidence names the kind.
                from arti_runtime.sleep_truth import night_kind, page_line
                kind = night.get("kind") or night_kind(
                    knockout=night.get("reason") == "knocked_out", observed=night.get("observed"),
                    collapse_clock=night.get("outcome") != "verified" and night.get("reason") == PASSED_OUT_REASON
                    and type(night.get("time_of_day")) is int and night["time_of_day"] >= 2600)
                if night.get("outcome") == "verified":
                    kind = "slept"
                c.live.previous_night = dict(night, money_after=after, kind=kind,
                    energy_after=facts.energy,
                    gold_change=after - before if type(before) is int and type(after) is int else None,
                    gold_lost=max(0, before - after) if type(before) is int and type(after) is int else None,
                    gold_loss_basis="observed net wallet change; fee vs overnight income not separable",
                    instruction=page_line(kind))
                self._journal(dict(ev="live7_previous_night", **c.live.previous_night))
            c.live.crop_memory = CropMemory(resume.get("crop_memory"))
            c.live.ground_items = GroundItems(resume.get("ground_items"))
            c.live.chest_memory = ChestMemory(resume.get("chest_memory"))
            c.live.forage_memory = ForageMemory(resume.get("forage_memory"))
            c.live.seen_objects = SeenObjects(resume.get("seen_objects"))
            c.live.cc_seen = resume.get("cc_seen") if isinstance(resume.get("cc_seen"), str) else None  # FIX75B
            if isinstance(resume.get("item_names"), Mapping):
                c.live.item_names = {str(q): n[:40] for q, n in resume["item_names"].items() if isinstance(n, str)}
            from arti_runtime.loop_facts import load_totals, load_kinds
            c.live.catch_totals = load_totals(resume.get("catch_totals"))
            c.live.catch_kinds = load_kinds(resume.get("catch_kinds"))  # FIX73D: per-kind count across days
            if (getattr(self, "_config", None) or {}).get("stardew_live7_press_results"):
                from arti_runtime.press_results import load as load_press_results
                c.live.press_results = load_press_results(resume.get("press_results"))
            from arti_runtime.travel_time import restore
            restore(c.live, resume.get("travel_trips"))
            c.live.shop_memory = ShopMemory.load(resume.get("shop_memory"))
            from arti_stardew_economy import _ids
            c.live.quest_refused_items = set(_ids(resume.get("quest_refused_items"), ()))
            try:
                if any(k in self._config for k in CLOCK_KEYS):
                    configure_clock(self._config)
            except ValueError as exc:  # a bad evening key never blocks the day: defaults, and say so
                configure_clock({})
                self._journal(dict(ev="live7_config_invalid", reason=str(exc), applied="defaults"))
            for row in resume.get("structural_quarantine", []):
                if structural_refusal(row):
                    c.live.quarantine.add((row["skill"], row["arg"], row["location"]))
            for row in resume.get("quarantine", []):
                c.live.record(S.SkillCall(row["skill"], row.get("arg")), UNCERTAIN,
                              "interrupted_before_rejoin", target=row.get("target"))
            session.blocked_refs |= c.live.refs
            c.live.rejoin_baseline = resume.get("before") if resume else None
            # Preserve ordinary session configuration for the next A/B/C run.
            c.live_limits = (self._action_session_limit, self._trial_deadline_unix_ms)
            self._action_session_limit = self._trial_deadline_unix_ms = None
            self._journal(dict(ev="part_live_started", session_id=session.session_id, date=c.date,
                               policy="model_choices;no_numeric_caps;automatic_recovery"))
        return self._live7_advance(session)

    @W.span('settle_readback', 4, runtime=True)
    def _live7_read(self, session):
        """A single fresh read; no waits/IO while holding the runtime lock."""
        c = session.campaign
        facts = self._skill_current_facts()
        binding, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        if binding.status != "bound" or motor is None or self._active is None:
            return facts, motor, None, "crash_or_identity_unavailable"
        granted_identity = tuple(self._active.play_identity or ())
        # PAUSED_RECOVERY revokes the old play grant (including play_identity).
        # Revalidate the pinned campaign against CURRENT native identity below;
        # never treat the deliberately revoked grant as a changed native actor.
        if (granted_identity and granted_identity != c.epoch
                or not granted_identity and self._status != "PAUSED_RECOVERY"):
            return facts, motor, None, "identity_mismatch"
        snapshot = self._build_decision_snapshot(self._active)
        context, _, _ = self._live7_context(snapshot)
        identity = context.get("identity")
        if identity and tuple(identity[:5]) != c.epoch:
            return facts, motor, context, "identity_mismatch"
        journal_stop = self._live7_journal_status(session)
        if journal_stop:
            return facts, motor, context, journal_stop
        missing = [name for name, failed in (
            ("motor_stale", self._state.is_stale(pid)), ("facts", facts is None),
            ("inventory_current", facts is not None and not facts.inventory_current),
            ("ui_complete", not context.get("ui_complete")), ("inventory_complete", not context.get("inventory_complete")),
            ("identity", not identity), ("time_of_day", facts is not None and type(facts.time_of_day) is not int),
            ("date", self._date_tuple(motor) is None)) if failed]
        if missing:
            # ADR 43 (a27 D18J2104-2116: held for 600 s, predicate never logged). One row per change, not per poll.
            key = (session.session_id, tuple(missing))
            if self.__dict__.get("_live7_read_missing") != key:
                self._live7_read_missing = key
                self._journal(dict(ev="live7_read_hold", hold="runtime_not_ready:fresh_read_required",
                                   missing=missing, menu=getattr(motor, "active_menu", None)))
            return facts, motor, context, "runtime_not_ready:fresh_read_required"
        from arti_runtime.travel_time import observe
        observe(c.live, facts.location, context)
        self.__dict__.pop("_live7_read_missing", None)
        if c.live.release_after is not None:
            # A merely young cache may still predate release. Require both the
            # motor and the complete bag to have arrived after this settle began.
            since = c.live.release_after
            bag = self._obs.get(pid, "progress.inventory")
            if (bag is None or bag.received_monotonic <= since
                    or getattr(motor, "diterima_pada", 0) <= since):
                return facts, motor, context, "runtime_not_ready:post_release_read_required"
        self._live7_note_wallet(session, motor, context)
        from scripts.live7.whole_state import publish
        publish(self, session, facts, motor, context)
        return facts, motor, context, None

    def _live7_note_wallet(self, session, motor, context):
        if (self._date_tuple(motor) == session.campaign.date
                and type(motor.time_of_day) is int and motor.time_of_day < PASS_OUT_CLOCK
                and type((context or {}).get("money")) is int
                and context.get("ui_complete") and context.get("inventory_complete")
                and tuple((context.get("identity") or ())[:5]) == session.campaign.epoch):
            session.campaign.live.last_wallet = dict(money=context["money"],
                date=list(session.campaign.date), time_of_day=motor.time_of_day)
            # ADR 93: the cycle carries the last wallet of the day across the night (one row per change, not per read).
            if getattr(self, "_live7_wallet_journaled", None) != (session.campaign.date, context["money"]):
                self._live7_wallet_journaled = (session.campaign.date, context["money"])
                self._journal(dict(ev="live7_wallet", **session.campaign.live.last_wallet))

    def _live7_journal_alert(self, session, reason, *, count=None, limit=None):
        scope = tuple(session.campaign.epoch)
        key = (scope, reason)
        if getattr(self, "_live7_journal_alerted", None) == key:
            return
        self._live7_journal_alerted = key
        self._journal(dict(ev="live7_journal_alert", reason=reason, outcome_class=SAFETY_STOP,
                           journal_unresolved_count=count, journal_unresolved_limit=limit,
                           channels=["owner_voice_chat"], action="stop_no_retry"))
        from arti_stardew_runtime import OwnerTurnResult
        self._skill_emit("live7_journal_alert", OwnerTurnResult(admitted=True, skip_reply_tags=True,
            reason=reason, speech="Jurnal pengaman perintah bermasalah. Aku berhenti bermain; pemilik perlu memeriksanya."))

    def _live7_journal_status(self, session):
        commands = getattr(self, "_commands", None)
        if commands is None:
            return None
        from arti_stardew_command import KesalahanPerintah
        try:
            native = commands.baca_sesi(commands.folder_perintah(self._config.get("stardew_command_dir")))
        except (OSError, ValueError, KesalahanPerintah):
            return None  # Existing readiness gates still own missing/unreadable session state.
        raw = native.mentah
        scope = (raw.get("runtime_instance_id"), native.session_id, native.player_id, native.screen_id)
        epoch = session.campaign.epoch
        if scope != (epoch[0], epoch[1], epoch[3], epoch[4]):
            return None  # Foreign or stale files cannot alert or retire this campaign's fences.
        count, limit = raw.get("journal_unresolved_count"), raw.get("journal_unresolved_limit")
        reason = journal_refusal(raw.get("journal_blocked_reason"))
        if reason:
            self._live7_journal_alert(session, reason, count=count, limit=limit)
            return reason
        if type(count) is int and count >= JOURNAL_WARNING_COUNT and getattr(self, "_live7_journal_warned_scope", None) != scope:
            self._live7_journal_warned_scope = scope
            self._journal(dict(ev="live7_journal_capacity_warning", journal_unresolved_count=count,
                               journal_unresolved_limit=limit, channels=["owner_voice_chat"], action="notify_only"))
            from arti_stardew_runtime import OwnerTurnResult
            self._skill_emit("live7_journal_capacity_warning", OwnerTurnResult(admitted=True, skip_reply_tags=True,
                reason="live7_journal_capacity_warning",
                speech="Jurnal pengaman perintah mulai penuh. Tolong periksa sebelum kapasitasnya habis."))
        return None

    def _live7_calls(self, session, snapshot, facts):
        if (getattr(self, "_config", None) or {}).get("stardew_live7_local_walks") is True:
            self._live7_local_walks_meta = ({}, None)
            self._live7_local_walk_keys = set()
        def noted(calls, harvest):
            session.campaign.live.harvest_fact = harvest
            session._persona_harvest = dict(value=harvest, observed_ts=time.time(), date=list(session.campaign.date))
            self._journal(dict(ev="live7_harvest_offer", **session._persona_harvest))
            return calls
        def gated(calls, reason):
            # Native menu/gift choices deliberately need no world snapshot.
            session._persona_harvest = None
            session.campaign.live.harvest_fact = None
            self._journal(dict(ev="live7_harvest_offer", value=dict(offered=False, exclusion_causes=[reason])))
            return calls
        generic = getattr(self, "_live7_ui_offer", None)
        if generic is not None:
            calls = [S.SkillCall("ui_action", arg) for arg in generic["actions"]]
            calls.extend(generic.get("world_calls") or ())
            return gated(calls, "native_ui_choices_active")
        if getattr(self, "_live7_gift_offer", None) is not None:
            calls = [S.SkillCall(name) for name in self._live7_gift_offer.get("choices", GIFT_SKILLS)]
            return gated(calls, "native_gift_choices_active")
        from arti_stardew_economy import ItemPolicy
        self._live7_ground_observe(session, snapshot, facts)
        context, weekday, weather = self._live7_context(snapshot)
        from arti_runtime.social_facts import _birthdays_today
        context = dict(context, live7_birthdays=_birthdays_today(  # ADR 149: the gift offer leaves out today's birthdays
            getattr(self, "_live7_calendar", ()), mind_day(session.campaign.date), MIND_GAME))
        if "stardew_live7_ship_on_the_way" in (getattr(self, "_config", None) or {}):
            # ADR 231 part A: the box setting reaches the page rules (DayPolicy is not persisted with it).
            session.campaign.live.stardew_live7_ship_on_the_way = self._config["stardew_live7_ship_on_the_way"] is True
            if session.campaign.live.stardew_live7_ship_on_the_way and self._config.get("live7_fact_line_enabled") is True:
                context["stardew_live7_ship_on_the_way"] = True
        calls = day_plan(session.campaign.date, weekday, weather, facts, context,
                         policy=session.campaign.live, snapshot=snapshot, item_policy=ItemPolicy.from_config(self._config))
        if (getattr(self, "_config", None) or {}).get("stardew_live7_local_walks") is True:
            from arti_runtime import local_walks as _local_walks
            _lw_calls, _lw_eyes, _lw_fact = _local_walks.plan(
                snapshot, facts, context, session.campaign.live, calls,
                native_event=self._live7_native_event())
            self._live7_local_walks_meta = (_lw_eyes, _lw_fact)
            self._live7_local_walk_keys = {key(c) for c in _lw_calls}
            if _lw_calls:
                calls = list(calls) + _lw_calls
        from arti_runtime import raw_hands
        if raw_hands.enabled(self._config):
            # ADR 243: her raw hands on every page she can move on, after every branch of the day plan.
            raw, self._live7_raw_use_why = raw_hands.world_block(snapshot, facts, context)
            calls = raw_hands.merge(calls, raw)
        from arti_stardew_event_control import festival_dead_calls, festival_seated
        # The fresh native seated state, not only a published stand (a held object withholds it). Read only on a
        # festival map (the game's "Temp"), so every other page reads nothing new.
        seated = festival_seated(S._g(snapshot, "event_actions")) or (
            getattr(facts, "location", None) == "Temp" and self._live7_festival_leave_offered(snapshot, seated=True))
        if (S._g(snapshot, "event_movement") or {}).get("is_festival") is True or seated:
            from arti_stardew_event_control import festival_walk_calls
            if not seated:
                here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
                size = self._festival_map_size(S._g(snapshot, "movement_context"))
                calls = festival_walk_calls(calls, here, size)
            # No offer the event motor always refuses on a festival map.
            calls = festival_dead_calls(calls, seated=seated,
                                        fishing=S._g(S._g(snapshot, "festival_reflex"), "state") in ("ready", "live"))
        self._live7_last_world_calls = [c for c in calls if c.name not in ("cek_jurnal", "inventori", "ui_action")]
        return noted(calls, session.campaign.live.harvest_fact)

    def _live7_festival_leave_offered(self, snapshot, *, seated=False):
        """The game's own festival leave question can be asked now (the ui.context pad Select cell).
        ``seated``: that cell refuses it because she sits on a festival seat."""
        from arti_stardew_event_control import festival_leave_offered, festival_pad_seated
        pid = getattr(getattr(snapshot, "binding", None), "player_id", None)
        rec = self._ui_key_record(pid)[0] if pid is not None and hasattr(self, "_ui_key_record") else None
        return rec is not None and (festival_pad_seated if seated else festival_leave_offered)(getattr(rec, "facts", None))

    def _live7_chest_observe(self, session, snapshot, facts):
        """FIX72D: a chest native must list here and does not (she or the HOST took it up) leaves the memory."""
        memory = session.campaign.live.chest_memory
        pid = getattr(getattr(snapshot, "binding", None), "player_id", None)
        if not memory.rows or facts is None or not facts.location or pid is None or getattr(self, "_obs", None) is None:
            return
        rec = self._obs.get(pid, "ui.context")
        if (rec is None or not getattr(rec, "is_complete", False) or not getattr(rec, "authoritative", False)
                or getattr(rec, "stale", True) or not isinstance(getattr(rec, "facts", None), Mapping)):
            return
        if memory.note_published(facts.location, S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile")), rec.facts.get("store_chests")):
            self._journal(dict(ev="live7_chest_memory", at=time.time(), game=dict(date=list(session.campaign.date)),
                               memory=memory.dump()))

    def _live7_ground_observe(self, session, snapshot, facts):
        """fix59A (ADR 86): her bag and a complete local drop scan update the items she left on the ground."""
        self._live7_chest_observe(session, snapshot, facts)
        memory = session.campaign.live.ground_items
        if not memory.rows or facts is None or not facts.location:
            return
        window = None
        scan_at = None
        pid = getattr(getattr(snapshot, "binding", None), "player_id", None)
        pin = self._water_context(pid) if pid is not None and callable(getattr(self, "_water_context", None)) else None
        if pin is not None and getattr(self, "_obs", None) is not None:
            from arti_stardew_scene import gather_window
            record = self._obs.get(pid, "scene.current_location")
            window = gather_window(record, session_id=pin.observation_session_id,
                                   screen_id=pin.screen_id, location_id=pin.location)
            if window is not None:
                scan_at = time.time() - max(0, time.monotonic() - record.received_monotonic)
        here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
        if memory.observe(location=facts.location, here=here, date=session.campaign.date, clock=facts.time_of_day,
                          window=window, inventory=dict(facts.inventory) if facts.inventory_current else None, scan_at=scan_at):
            self._journal(dict(ev="live7_ground_items", at=time.time(), game=dict(date=list(session.campaign.date)),
                               items=memory.dump()))

    def _live7_help(self, session, reason):
        with self._lock:
            p = session.campaign.live
            basis = (reason, session.campaign.date)
            if p.help_basis != basis:
                p.help_basis = basis
                self._journal(dict(ev="minta_tolong", session_id=session.session_id, reason=reason,
                                   channels=["owner_voice_chat", "viewers"], verified=False))
                self._skill_emit("minta_tolong", self._live7_help_speech())
                self._live7_queue_help(session, reason)

    def _live7_event_holds_night(self, motor):
        """An event is up for her right now: the festival copy map (`Temp`) or a fresh ui_event fact."""
        if getattr(motor, "location", None) == "Temp":
            return True  # Event.temporaryLocation (Event.cs:2892): only an event owns this map
        pid = getattr(motor, "player_id", None)
        rec = self._obs.get(pid, "ui.context") if pid is not None else None
        return bool(rec is not None and getattr(rec, "authoritative", False) and not getattr(rec, "stale", True)
                    and not getattr(rec, "requires_resync", True)
                    and 0 <= time.monotonic() - getattr(rec, "received_monotonic", -1) <= 2
                    and isinstance(rec.facts.get("ui_event"), Mapping))

    def _live7_guard_day(self, session, motor, *, allow_answered_sleep=False, context=None):
        """Fresh native rollover is a lost day unless this run already answered its bed question."""
        from arti_stardew_part_a import next_date
        c = session.campaign
        date = self._date_tuple(motor) if motor is not None else None
        now = getattr(motor, "time_of_day", None)
        if date is None or type(now) is not int:
            return None  # freshness/identity admission remains the caller's responsibility
        self._live7_note_wallet(session, motor, context)
        if c.finished and date == next_date(c.date):
            # The independently verified bed row may finish before this outer boundary.
            return self._skill_end_locked(session, "finished", "achieved", SLEEP_VERIFIED_REASON)
        if date == c.date and now < 2600:
            return None
        if date == c.date and self._live7_event_holds_night(motor):
            # fix58A (ADR 84, a43 D9:J1377): Game1.UpdateOther starts the 02:00 pass-out only when
            # !eventUp, and the clock stops at 2600. Inside a festival no native pass-out or new morning
            # comes, so a lost-day verdict here waited for a morning that never came (native_wake_timeout).
            # The day is still hers: she can leave (festival edge -> festivalEnd -> Farm), and the native
            # pass-out then happens outside the event and takes the ordinary path below.
            basis = (c.date, getattr(motor, "location", None))
            if getattr(self, "_live7_event_night_basis", None) != basis:
                self._live7_event_night_basis = basis
                self._journal(dict(ev="live7_event_night_hold", date=list(c.date), time_of_day=now,
                                   location=basis[1], native="no pass-out while an event is up"))
            return None
        run = session.current
        routine = getattr(self._active, "routine_objective", None)
        if (allow_answered_sleep and run is not None and run.call.name == "tidur"
                and getattr(routine, "answered", False) and tuple(routine.start_date or ()) == c.date):
            return None  # the existing bed + fresh morning verifier must judge this transition
        from arti_runtime.sleep_truth import bed_evidence, last_bed_observed
        observed = last_bed_observed(c)
        if date != c.date and bed_evidence(observed) and tuple(observed.get("start_date") or ()) == c.date:
            if not c.live.pending_sleep:
                row = next((r for r in reversed((c.days[-1] if c.days else {}).get("skills") or ())
                            if r.get("skill") == "tidur"), dict(task="sleep", skill="tidur", verified=False))
                c.live.pending_sleep = dict(row=row, observed=dict(observed),
                                            reason=observed.get("reason") or "observed_day_change")
            return None  # ADR 198: a native bed Yes is judged by its ticket; never a pass-out
        if date != c.date and c.live.pending_sleep:
            return None  # ADR 105: a bed with a native verdict is judged by its ticket on the fresh read, first
        if date in (c.date, next_date(c.date)):
            wallet = c.live.last_wallet or {}
            self._live7_passout_clock = now if date == c.date else PASS_OUT_CLOCK  # rollover: vanilla 02:00
            self._journal(dict(ev="live7_day_lost", reason=PASSED_OUT_REASON, before=c.date,
                               after=date, time_of_day=now, verified=False,
                               money_before=wallet.get("money"), money_before_observed=wallet or None,
                               money_after=(context or {}).get("money") if date != c.date else None))
            return self._skill_end_locked(session, "finished", "partial", PASSED_OUT_REASON)
        return self._skill_end_locked(session, "held", SAFETY_STOP, "live7:unexpected_day_change")

    def _live7_shop_discovery(self, session, facts, context):
        """Smoke item 11: ARTI learns Pierre's closed day by going there, then remembers it."""
        c = session.campaign
        p = c.live
        tried = p.shop_failures or any(h.get("skill") == "pergi_ke" and h.get("arg") == "seed shop" for h in p.history)
        weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[(c.date[2] - 1) % 7] if c.date else None
        # ADR 105 (a47 d14 J1317): the native keeper fact of a counter she stands at; the door's "buka" never said it.
        keeper = [r["availability"]["shopkeeper_present"] for r in (context.get("mines_ui") or {}).get("extras_targets", ())
                  if isinstance(r, Mapping) and r.get("kind") == "shop_counter" and isinstance(r.get("availability"), Mapping)
                  and type(r["availability"].get("shopkeeper_present")) is bool]
        hours_changed = bool(facts.location and p.shop_memory.note_hours(facts.location, c.date, facts.time_of_day,
                            (context.get('mines_ui') or {}).get('extras_targets', ())))
        keeper_changed = bool(keeper and facts.location and p.shop_memory.note_counter(facts.location, c.date, facts.time_of_day, any(keeper)))
        if hours_changed or keeper_changed:
            self._journal(dict(ev="live7_shop_memory", seen=dict(date=list(c.date), weekday=weekday, time_of_day=facts.time_of_day,
                               location=facts.location, evidence="counter_present" if any(keeper) else "counter_absent"),
                               memory=p.shop_memory.dump()))
        seen = shop_closed_seen(facts, context) if tried else None
        if seen and p.shop_memory.record(c.date, weekday, learn_weekday=seen == "route"):
            p.shop_retry_at = None  # no second trip today: she has seen it shut
            self._journal(dict(ev="live7_shop_memory", seen=dict(date=list(c.date), weekday=weekday,
                               time_of_day=facts.time_of_day, evidence=seen), memory=p.shop_memory.dump()))

    def _live7_advance(self, session):
        from arti_stardew_runtime import OwnerTurnResult
        if getattr(self, "_pause_boundary", lambda: False)():
            return OwnerTurnResult(admitted=True, reason="game_paused_wait")
        c = session.campaign
        if c.finished:
            return self._skill_end_locked(session, "finished", "achieved", "live7_sleep_verified")
        if c.live.wait_since is not None:
            return OwnerTurnResult(admitted=True, reason="live7_settle_before_replan")
        facts, motor, context, held = self._live7_read(session)
        if held:
            if journal_refusal(held):
                return self._skill_end_locked(session, "held", SAFETY_STOP, "live7:" + held)
            c.live.wait_since = c.live.wait_since or game_now(self)
            return OwnerTurnResult(admitted=True, reason="live7_waiting_for_readback")
        if self._live7_sleep_recheck(session, motor):
            return self._skill_end_locked(session, "finished", "achieved", "live7_sleep_verified")
        if c.live.pending_sleep and self._live7_morning_pending():
            return OwnerTurnResult(admitted=True, reason="live7_post_sleep_pending")
        before = getattr(c.live, "rejoin_baseline", None)
        if before is not None:
            after = self._live7_baseline(facts, motor, context)
            c.live.uncertain_items.update(q for q in set(before.get("inventory", {})) | set(after["inventory"])
                                          if before.get("inventory", {}).get(q) != after["inventory"].get(q))
            self._journal(dict(ev="live7_rejoin_reconcile", before=before, after=after,
                               replay=False, credit=False, outcome_class=UNCERTAIN))
            c.live.rejoin_baseline = None
        ended = self._live7_guard_day(session, motor, context=context)
        if ended is not None:
            return ended
        if (getattr(self, "_config", None) or {}).get("stardew_live7_goal_ladder", False) is True:
            from arti_runtime.goal_ladder_runtime import morning
            if morning(self, session, facts, context):
                return self._live7_advance(session)  # fresh date/clock/identity after the bounded reflection
        self._live7_shop_discovery(session, facts, context)
        self._live7_moments("day", session, motor, context=context)
        snapshot = self._build_decision_snapshot(self._active)
        if home_due(facts, c.live):
            from arti_runtime.decision_plan import drop
            drop(self, session, "curfew_boundary")
            c.live.sleep_only = True
        elif c.live.sleep_only:
            self._live7_reserve_pause(session, facts)  # ADR 155: a hop into a map where people are, with time left
        calls = self._live7_calls(session, snapshot, facts)
        from arti_runtime import mines_exit
        exit_exhausted = mines_exit.exhausted(c.live, facts.location)
        if exit_exhausted:
            memo = c.live.mines_exit_recovery[facts.location]
            if not memo.get("help_sent"):
                memo["help_sent"] = True
                self._journal(dict(ev="live7_floor_exit_unavailable", location=facts.location,
                                   fact=mines_exit.fact(c.live, context, facts.location, calls)))
                self._live7_help(session, mines_exit.EXHAUSTED + ":" + facts.location)
        if (c.live.travel_loop and not c.live.sleep_only and not c.live.loop_help_asked
                and not any(x.name not in ("pergi_ke", "tidur", "cek_pagi", "makan") for x in calls)):
            # Review 2026-10-23: the shuttle fact is in her offer; ask for help once a day, and only when no work is
            # offered where she stands (never while work options remain here).
            c.live.loop_help_asked = True
            self._live7_help(session, "unproductive_travel_loop")
        if c.live.home_failures >= max(HOME_RETRIES - 1, 1) and not c.live.home_help_asked:
            # Help is the last resort (owner 2026-10-24), asked while ONE reserve retry remains (review cand-g: at
            # exhaustion the session ended in the same tick, too late for the owner or viewers to act). Only the
            # bedtime reserve spends home retries; a daytime failure is only a fact in her offer and never reaches this.
            c.live.home_help_asked = True
            self._live7_help(session, "home_route_unreachable:" + str(facts.location))
        home_blocked = c.live.home_blocked_here(facts.location)
        if c.live.home_failures >= HOME_RETRIES and c.live.home_unreachable is None:
            # Exhaustion of a direct approach still leaves a currently offered alternate hop.
            alternates = [x for x in calls if x.name == "tidur" and key(x) != c.live.last_failed
                          or not home_call(x) and x.name == "pergi_ke"]
            if alternates and home_blocked is None:
                calls = alternates
            else:
                # FIX_TRAVEL_DEADLINE (smoke cand-o d3a0 L2431/L2432): ending the session here left ARTI idle
                # outdoors until the native pass-out, with no decision trace. The reserve's automatic retries stay
                # spent; from here every choice is hers, with the facts. The pass-out guard still ends the day.
                c.live.home_unreachable = home_unreachable_fact(facts, c.live.home_failures, c.live.last_failed, calls,
                                                                blocked=home_blocked)
                self._journal(dict(ev="live7_home_unreachable", **c.live.home_unreachable,
                                   offered=[[x.name, x.arg] for x in calls], chooser="model"))
        if c.live.home_unreachable is not None:
            c.live.home_unreachable = home_unreachable_fact(facts, c.live.home_failures, c.live.last_failed, calls,
                                                            blocked=home_blocked)
        if not calls:
            if not exit_exhausted:
                self._live7_help(session, "no_admissible_skill_or_home_route")
            return OwnerTurnResult(admitted=True, reason="live7_waiting_for_native_offer")
        p = c.live
        # her choice outside the automatic reserve; a proven route block hands the choice back to her (fix10e)
        from arti_runtime.mines_exit import blocked as mine_exit_blocked
        choosing = (not p.sleep_only or p.home_unreachable is not None or home_blocked is not None
                    or mines_exit.exhausted(p, facts.location)
                    or mine_exit_blocked(p, context, facts.location) is not None)
        if p.finish_streak >= FINISH_RETRIES and choosing:
            self._live7_help(session, "no_progress:finish_without_next_task")
            return OwnerTurnResult(admitted=True, reason="live7_waiting_for_next_task")
        if p.empty_fallbacks >= EMPTY_FALLBACK_REASKS and choosing:
            self._live7_help(session, EMPTY_FALLBACK_REASON)
            return OwnerTurnResult(admitted=True, reason="live7_waiting_for_next_task")
        # fix57 (a42 D2:J129-J153): after a provider failure the next ask waits its backoff, and never into a lane a
        # timed-out call still owns. A tick spent waiting for the brain is not one of her idle boundaries.
        provider_wait = choosing and self._live7_provider_wait(session)
        with self._lock:
            if facts.time_of_day != p.progress_clock:
                p.progress_clock, p.idle_boundaries = facts.time_of_day, 0
            if p.idle_boundaries >= NO_PROGRESS_BOUNDARIES and choosing:
                # A verified row, resumed clock, or new advisory is a change.
                self._live7_help(session, "no_progress:clock_stopped_at_%04d" % facts.time_of_day)
                return OwnerTurnResult(admitted=True, reason="live7_waiting_for_progress")
            if not provider_wait:
                p.idle_boundaries += 1
        # The clock reserve is the emergency home path: do not spend it waiting for a provider.
        # Every step still uses the same native admission, identity fence and world-effect verifier.
        guard = self._live7_mines_contact_guard(session, facts, context, calls)
        carry = self.__dict__.get("_live7_carry_after_close")
        if isinstance(carry, S.SkillCall) and carry not in calls:
            self.__dict__.pop("_live7_carry_after_close", None)
            carry = None
        if guard is not None:
            # ADR 174: on a Mines floor, contact plus falling HP moves her body up the offered way out
            # before any model call (a page is standing still while being hit; co-op never pauses).
            from arti_runtime.decision_plan import drop
            drop(self, session, "mines_contact_guard")
            session.model_pick = None
            selected = guard
        elif isinstance(carry, S.SkillCall):
            from arti_runtime.decision_plan import drop
            drop(self, session, "menu_world_after_close")
            session.model_pick = None
            selected = self.__dict__.pop("_live7_carry_after_close")
        elif not choosing:
            session.model_pick = None
            selected = reserve_choice(calls, facts)
            if selected is None:
                return OwnerTurnResult(admitted=True, reason="live7_waiting_for_home_offer")
            from arti_runtime.travel_time import reserve
            self._journal(dict(ev="live7_home_reserve", time_of_day=facts.time_of_day,
                               skill=selected.name, arg=selected.arg,
                               reserve=reserve(facts.location, facts.time_of_day, p)))
        else:
            reflex = self._live7_reflex(session, facts, context, calls)
            if reflex is not None:
                from arti_runtime.decision_plan import drop
                drop(self, session, "under_attack_reflex")
                selected = reflex  # owner 2026-10-23 safety reflex: no provider call while being hit
            elif provider_wait:
                return OwnerTurnResult(admitted=True, reason="live7_waiting_for_provider")
            elif self._live7_catalog_wait(p, snapshot, context):
                return OwnerTurnResult(admitted=True, reason="live7_waiting_for_catalog")
            else:
                selected = self._skill_model_choose(session, "live7_next_skill")
                if self._live7_provider_result(session, selected):
                    # A transient provider failure is not her unusable answer: it spends no re-ask of hers, asks
                    # nobody for help and starts no activity. live7_provider_retry names it; she is asked again.
                    session.model_pick = None
                    return OwnerTurnResult(admitted=True, reason="live7_waiting_for_provider")
        if not self._live7_active():
            return OwnerTurnResult(admitted=False, reason="skill_session_inactive")
        if selected == "superseded":
            return OwnerTurnResult(admitted=True, reason="live7_refresh_choice")
        # The provider latency can change time, energy or native offers.
        facts, motor, context, held = self._live7_read(session)
        if held:
            c.live.wait_since = c.live.wait_since or game_now(self)
            return OwnerTurnResult(admitted=True, reason="live7_waiting_for_readback")
        ended = self._live7_guard_day(session, motor, context=context)
        if ended is not None:
            return ended
        if home_due(facts, c.live):
            c.live.sleep_only = True  # provider latency may have consumed the reserve
        if (not c.live.sleep_only or c.live.home_unreachable is not None) and selected in ("finish", "ask_owner"):
            session.model_pick = None
            if selected == "ask_owner":
                self._live7_help(session, session.question or "model_asked_for_help")
                return OwnerTurnResult(admitted=True, reason="live7_waiting_for_next_task")
            p.finish_streak += 1
            self._journal(dict(ev="live7_task_finished", next_task_chosen=False, count=p.finish_streak, verified=False))
            if p.finish_streak >= FINISH_RETRIES:
                self._live7_help(session, "no_progress:finish_without_next_task")
            return OwnerTurnResult(admitted=True, reason="live7_choose_next_task")
        fresh = self._live7_calls(session, self._build_decision_snapshot(self._active), facts)
        if isinstance(selected, S.SkillCall) and selected not in fresh and choosing and context.get("world_knowledge") is None:
            facts, motor, context = self._live7_frame_for_pick(session, facts, motor, context)
            fresh = self._live7_calls(session, self._build_decision_snapshot(self._active), facts)
        if isinstance(selected, S.SkillCall) and selected not in fresh:
            from arti_runtime.choice_contract import fresh_call
            repinned = fresh_call(selected, fresh)
            if repinned is not selected:
                selected = repinned
                if session.model_pick:
                    session.model_pick = (selected, session.model_pick[1])
                self._journal(dict(ev="choice_normalized", fresh_pin=1))
        goods_batch = []
        goods_implicit = isinstance(selected, S.SkillCall) and selected.batch == ("semua",)
        if isinstance(selected, S.SkillCall) and selected.name in S.GOODS_SKILLS and not c.live.sleep_only:
            from arti_stardew_skills_goods import batch_calls
            native_goods = goods_facts(context)
            goods_batch = batch_calls(selected, fresh, native_goods, p.gift_from)
            if goods_batch:
                # Admit via the fresh catalogue, then pin each exact observed stack internally.
                selected = next(x for x in fresh if x.name == selected.name and x.arg == goods_batch[0].arg
                                and (x.name != "simpan_ke_chest" or x.quality == goods_batch[0].quality))
                if session.model_pick:
                    session.model_pick = (selected, session.model_pick[1])
            elif selected.batch or (selected.name == 'simpan_ke_chest' and type(selected.n) is int
                    and any(type(r.get('slot')) is int and type(r.get('quality')) is int for r in native_goods)):
                # A changed native stack cannot expand an exact deposit.
                # Legacy unpinned calls still reach their native target verifier.
                self._journal(dict(ev="live7_goods_batch_stopped", reason="no_eligible_stacks"))
                if (getattr(self, '_config', None) or {}).get('stardew_live7_ship_on_the_way'):
                    # ADR 232 part G (mabar 2026-11-17 Spring 12 23:20 kirim semua, re-read at 23:40 without kirim):
                    # the choice was neither run nor refused to her; her next page says it did not run, and why.
                    from arti_runtime.ship_pause import batch_refused
                    session.choice_feedback = batch_refused(selected, fresh)
                return OwnerTurnResult(admitted=True, reason="live7_refresh_choice")
        if c.live.home_failures >= HOME_RETRIES and c.live.home_unreachable is None:
            fresh = [x for x in fresh if x.name == "tidur" and key(x) != c.live.last_failed
                     or not home_call(x) and x.name == "pergi_ke"]
        if (isinstance(selected, S.SkillCall) and selected not in fresh and choosing
                and p.stale_reasks < STALE_REASKS):
            # operator-b5 J2447-J2458 (tebang -> pecah_batu) and smoke D1 J1424-J1440 (jalan_ke 69,17 -> cangkul): her
            # pick was no longer admissible after the provider latency and the plan silently started fresh[0].
            # Ask her again on the fresh offer; a repeated stale pick reaches the bounded unusable-choice path.
            session.model_pick = None
            p.stale_reasks += 1
            by = "reflex" if p.reflex_pending and selected == S.SkillCall("lawan") else "model"
            if by == "reflex":
                p.reflex_pending = False
                p.reflex_starts.pop()  # r1 #7: a reflex that never started does not spend the reflex cap
            self._journal(dict(ev="live7_choice_stale", skill=selected.name, arg=selected.arg, by=by,
                               reask=p.stale_reasks, cap=STALE_REASKS, frame=context.get("world_knowledge") is not None))
            session.choice_feedback = dict(code="offer_changed", input_taken=False,
                submitted=dict(name=selected.name, arg=selected.arg),
                field="menu_open" if facts.menu_open else "current_offer",
                valid="Pilihan ini perlu prasyarat native yang masih berlaku; pilih dari halaman baru.")
            return OwnerTurnResult(admitted=True, reason="live7_refresh_choice")
        p.stale_reasks = 0
        if choosing and not p.sleep_only and selected not in fresh:
            # An unusable model choice grants no different activity. Return
            # fresh facts, then ask for help at the existing bound. Emergency
            # home/food safety selection below remains independent.
            session.model_pick = None
            p.empty_fallbacks += 1
            if selected == "invalid" and ((session.choice_feedback or {}).get("submitted") or {}).get("kind") == "wait":
                # fix57: her own wait at a skill boundary changes nothing native; a repeat is the same tanpa_efek
                # fact as any other no-effect repeat (ADR 79). A count, never a filter: she still chooses.
                from arti_runtime import loop_facts
                loop_facts.note(p.no_effect, "wait", None, effect=False, reason="tanpa_aksi", clock=facts.time_of_day)
            self._journal(dict(ev="live7_choice_not_run", reason=str(selected)[:160],
                               count=p.empty_fallbacks, cap=EMPTY_FALLBACK_REASKS))
            if p.empty_fallbacks >= EMPTY_FALLBACK_REASKS:
                self._live7_help(session, EMPTY_FALLBACK_REASON)
                return OwnerTurnResult(admitted=True, reason="live7_waiting_for_next_task")
            return OwnerTurnResult(admitted=True, reason="live7_refresh_choice")
        force_home = (p.sleep_only and p.home_unreachable is None and home_blocked is None
                      and mine_exit_blocked(p, context, facts.location) is None)
        if selected not in fresh or force_home and selected != reserve_choice(fresh, facts):
            session.model_pick = None
            # ADR 108 (a48 d9 J838-J841): her ask_owner under the reserve became a plan's trip with no row saying so.
            if isinstance(selected, S.SkillCall) or selected in ("invalid", "ask_owner", "finish"):
                by = "reflex" if p.reflex_pending and selected == S.SkillCall("lawan") else "model"
                if by == "reflex":
                    p.reflex_pending = False
                    p.reflex_starts.pop()  # r1 #7: never started, never counted
                self._journal(dict(ev="live7_choice_substituted", chosen=[selected.name, selected.arg]
                                   if isinstance(selected, S.SkillCall) else selected, by=by,
                                   sleep_only=p.sleep_only))
            # Information reading is her choice; a failed/invalid answer never starts TV or mail.
            fallback = [x for x in fresh if x.name != "cek_pagi" and (p.sleep_only or x.name != "tidur"
                        or p.bedtime_return and bedtime_default(facts, p, fresh))]
            selected = (reserve_choice(fallback, facts) if p.sleep_only else
                        next((x for x in fallback if x.name == "makan"), None) or next(iter(fallback), None))
        if selected is None:
            # Review 2026-10-23: her answer was unusable and only her own optional reads (TV/mail) or a
            # not-yet-bedtime bed remain. Re-ask a bounded number of times, then ask for help, never loop.
            p.empty_fallbacks += 1
            self._journal(dict(ev="live7_choice_empty_fallback", count=p.empty_fallbacks, cap=EMPTY_FALLBACK_REASKS))
            if p.empty_fallbacks >= EMPTY_FALLBACK_REASKS:
                self._live7_help(session, EMPTY_FALLBACK_REASON)
                return OwnerTurnResult(admitted=True, reason="live7_waiting_for_next_task")
            return OwnerTurnResult(admitted=True, reason="live7_refresh_choice")
        p.empty_fallbacks = 0
        pick = session.model_pick
        if selected == S.SkillCall("pergi_ke", "home"):
            model_home = bool(pick and pick[0] == selected and type(facts.time_of_day) is int)
            p.home_return_chosen = bool(model_home and facts.time_of_day >= BED_RETURN_CHOICE_FROM)
            p.bedtime_return = bool(model_home and facts.time_of_day >= BED_OFFER_FROM)
        elif selected.name in ("pergi_ke", "jalan_ke") and selected.arg != "home":
            p.bedtime_return = False  # ARTI may explicitly choose another trip; never force bedtime
            p.home_return_chosen = False
        p.finish_streak = 0
        c.task = "sleep" if c.live.sleep_only or selected.name == "tidur" or selected == S.SkillCall("pergi_ke", "home") else selected.name
        c.travel = {"active": selected.name in ("pergi_ke", "jalan_ke")}
        session.campaign_start = c.task
        reflex_start, p.reflex_pending = p.reflex_pending and selected == S.SkillCall("lawan"), False
        from arti_runtime.mines_body import body_guard_running, note_body_busy
        if body_guard_running(self):
            # ADR 183 (a74 B d3 J2387-J2399): her body is busy keeping her alive; the choice is re-asked after.
            note_body_busy(self, selected.name, "skill_start")
            if guard is not None:
                # ADR 189 (a76 d3 J2602-J2621): the contact guard's way up waits for the guard; it is owed, not a page.
                from arti_runtime import mines_contact
                mines_contact.owe_leave(self, facts.location, "dropped_for_body_guard")
            session.model_pick = None
            return OwnerTurnResult(admitted=True, reason="live7_waiting_for_body_guard")
        from arti_runtime.bag_sitting import seed_start
        first = goods_batch[0] if goods_batch and selected.name in S.GOODS_SKILLS else selected
        seeded = (dict(goods_remaining=goods_batch[1:], goods_completed=0, goods_implicit=goods_implicit,
                       goods_basis=(tuple(context.get('identity') or ()), self._date_tuple(motor), motor.location))
                  if len(goods_batch) > 1 else None)
        with seed_start(session, first, seeded):
            result = self._skill_start(session, first,
                                       by="reflex" if reflex_start else "model" if pick and pick[0] == selected
                                       else "plan", facts=facts)
        run = session.current
        if run is not None:
            from arti_runtime.mines_survival import escape_offered
            if selected.name == "lawan_mine" and escape_offered(context, facts.location):
                run.state["mines_escape_only"] = True
            if selected.name in ("lawan_mine", "lawan") and pick and pick[0] == selected:
                # ADR 189 rule 4: her own fight, chosen on a page already showing the contact and the way up (ADR 153).
                from arti_runtime import mines_contact
                if (mines_contact.attackers((context or {}).get("combat"))
                        and mines_contact.exit_call(calls, facts.location) is not None):
                    run.state["fight_chosen_in_contact"] = True
            p.interrupted_travel = None  # her next admitted world activity replaces the interrupted intent
            p.route_pause = None  # ADR 155: the pause fact served the page it paused for
        if run is not None and goods_batch and run.call == goods_batch[0]:
            run.state.update(goods_remaining=goods_batch[1:], goods_completed=0,
                             goods_implicit=goods_implicit,
                             goods_basis=(tuple(context.get("identity") or ()), self._date_tuple(motor), motor.location))
        if run is not None and run.call.name == "lawan":
            combat_now = context.get("combat") or {}
            run.state.update(live7_lawan_by="reflex" if reflex_start else "model" if pick and pick[0] == selected
                             else "plan", live7_lawan_hp=combat_now.get("hp"),
                             live7_lawan_monsters=[[m.get("type"), m.get("distance")] for m in
                                                   (combat_now.get("monsters") or ())[:3] if isinstance(m, Mapping)])
        if run is not None:
            ref = p.crop_walk_targets.get(key(selected)) if selected.name == "jalan_ke" else None
            if ref:
                run.state.update(live7_crop_ref=ref,
                                 live7_crop_length=p.crop_routes[ref].get("planned_length"))
            if selected.name == "langkah" and pick and pick[0] == selected:
                from arti_runtime.raw_hands import step_target
                run.state["step_target"] = step_target((getattr(self, "_live7_mind_last", None) or {}).get("why"), context)
            if (getattr(self, "_config", None) or {}).get("stardew_live7_local_walks") is True:
                if selected.name == "jalan_ke" and key(selected) in getattr(self, "_live7_local_walk_keys", ()):
                    _lw_here = S._wave1_xy(getattr(motor, "tile", None))
                    run.state["local_walk"] = dict(
                        location=getattr(motor, "location", None),
                        tile=list(selected.tile) if selected.tile is not None else None,
                        start_tile=list(_lw_here) if _lw_here is not None else None,
                    )
            self._live7_start_baseline(run, facts, motor, context)
        threshold = self._config.get("stardew_live7_cost_alarm_calls", 24)  # notification only, once per day
        provider = getattr(self, "_provider", None)
        dollars = self._config.get("stardew_live7_cost_alarm_usd",
                                   float(getattr(getattr(provider, "policy", None), "budget_usd", 0)))
        estimated_cost = float(getattr(provider, "_committed_cost_usd", 0))
        calls_alarm = type(threshold) is int and threshold > 0 and session.model_decisions >= threshold
        cost_alarm = type(dollars) in (int, float) and dollars > 0 and estimated_cost >= dollars
        if (calls_alarm or cost_alarm) and not c.live.alarmed:
            c.live.alarmed = True
            self._journal(dict(ev="live7_cost_alarm", model_calls=session.model_decisions,
                               estimated_cost_usd=estimated_cost, action="notify_only"))
        return result

    def _live7_crop_walk_result(self, campaign, run, row, motor, context):
        p, ref = campaign.live, run.state["live7_crop_ref"]
        crop = p.crop_memory.crops.get(ref)
        if crop is None:
            return  # native absence/ready/death already retired it
        center = S._wave1_xy((context.get("local_window") or {}).get("center"))
        local = (crop["location"] == S._g(motor, "location") and center is not None
                 and max(abs(a-b) for a, b in zip(center, crop["tile"])) <= 1)
        here = S._wave1_xy(S._g(motor, "tile"))
        remaining = None
        if here is not None and crop["location"] == S._g(motor, "location"):
            crop_waypoint(p, ref, crop, context, here)
            remaining = p.crop_routes[ref].get("planned_length")
        skipped = p.crop_memory.walk_result(ref, campaign.date, verified=row["verified"],
            remaining=remaining, start_remaining=run.state.get("live7_crop_length"), local_read=local)
        row["evidence"]["crop_walk"] = dict(ref=ref, remaining=remaining, local_read=local, skipped=skipped)
        self._journal(dict(ev="live7_crop_memory", at=time.time(), game=dict(date=list(campaign.date)),
                           crops={ref: dict(r) for ref, r in p.crop_memory.crops.items()}))

    def _live7_floor_contact(self, run, goal, motor):
        """ADR 174: a running row on a Mines floor that is neither the way up nor her fight in flight ends
        ``under_attack`` between inputs when the contact guard is due, so the next boundary moves her body
        (never a model page first). Input-in-flight, uncertainty and mutation fences still win."""
        from arti_runtime import mines_contact
        from arti_runtime.mines_body import mod_guard_failed_recently
        location = str(getattr(motor, "location", "") or "")
        if location.startswith("UndergroundMine"):
            # ADR 180: HP history is kept while a row is exempt too, so a failed mod guard sees the drop at once.
            seen = getattr(self._obs.get(self._binding_view()[1], "ui.context"), "facts", None)
            seen_hp = min((v for v in (getattr(motor, "health", None), ((seen or {}).get("combat") or {}).get("hp")
                                       if isinstance(seen, Mapping) else None) if type(v) is int), default=None)
            mines_contact.note_hp(self, location, seen_hp)
        exit_row = (run.call == S.SkillCall("pergi_ke", "mine")
                    or run.call.name == "aksi" and str(run.call.arg or "").startswith("xt:mine:")
                    or run.state.get("wave1_driver") == "exit")
        if exit_row and location.startswith("UndergroundMine"):
            mines_contact.refresh_owed(self, location)  # ADR 189: the exit row owns the owed leave; time never expires it
        # ADR 189 rule 4 (a76 d5 J898-J954): an owed leave beats a fight she did not choose on a page showing contact and
        # the way up (ADR 153 keeps the chosen one).
        owed = (location.startswith("UndergroundMine") and mines_contact.leave_owed(self, location)
                and not run.state.get("fight_chosen_in_contact"))
        if (not location.startswith("UndergroundMine") or goal is None or goal.goal_id != run.goal_id
                or run.call == S.SkillCall("pergi_ke", "mine")
                or run.call.name == "aksi" and str(run.call.arg or "").startswith("xt:mine:")
                or run.state.get("wave1_driver") == "exit"
                or run.state.get("wave1_driver") in ("combat", "combat_floor")
                and not mod_guard_failed_recently(self, location) and not owed
                or run.state.get("mines_death") or self._state.is_stale(self._binding_view()[1])
                or getattr(self, "_action_busy", False) or getattr(self, "_results_pending", 0)
                or self._gather_mutation_unresolved or getattr(goal, "gather_uncertain", False)
                or getattr(goal, "tool_work_uncertain", False)):
            return None
        action = getattr(goal, "last_action", None)
        if action is not None and (S._g(action, "cleanup_confirmed") is not True
                or S._g(action, "lifecycle") not in CLEAN_LIFECYCLES
                or S._g(action, "world_effect_status") in ("uncertain", "possible_mutation", "unverified")):
            return None
        rec = self._obs.get(self._binding_view()[1], "ui.context")
        facts = getattr(rec, "facts", None)
        combat = facts.get("combat") if isinstance(facts, Mapping) else None
        hp = min((v for v in (getattr(motor, "health", None), (combat or {}).get("hp")) if type(v) is int), default=None)
        fact = mines_contact.due(self, combat, hp, location)
        if fact is None:
            return None
        if not run.state.get("mines_contact_logged"):
            run.state["mines_contact_logged"] = True
            self._journal(dict(ev="mines_contact_guard", status="row_ended", skill=[run.call.name, run.call.arg],
                               **fact, action="end_row_recoverable"))
        return "fail", UNDER_ATTACK_REASON

    def _live7_mines_contact_guard(self, session, facts, context, calls):
        """ADR 174: the offered way up when a live monster is in contact and native HP is falling on a Mines
        floor and no fight batch is in flight, else None. Never a fight, home trip or descent ban."""
        from arti_runtime import mines_contact
        if not str(facts.location or "").startswith("UndergroundMine") or getattr(self, "_action_busy", False):
            return None
        combat = context.get("combat") or {}
        hp = min((v for v in (facts.health, combat.get("hp")) if type(v) is int), default=None)
        fact = mines_contact.due(self, combat, hp, facts.location)
        if fact is None:
            return None
        call = mines_contact.exit_call(calls, facts.location)
        if call is None:
            self._journal(dict(ev="mines_contact_guard", status="no_way_up_offered", **fact,
                               offered=[[x.name, x.arg] for x in calls][:8]))
            return None
        act = mines_contact.started(self, fact, call, "naik_lewat_jalan_keluar")
        self._journal(dict(ev="mines_contact_guard", status="started", skill=[call.name, call.arg],
                           **{k: v for k, v in act.items() if k not in ("skill", "action", "at")}))
        return call

    def _live7_reflex(self, session, facts, context, calls):
        """The owner's safety reflex (2026-10-23): `lawan` without a model call when ``reflex_due``, else None."""
        p = session.campaign.live
        p.reflex_pending = False  # a stale re-ask never attributes her own later lawan pick to the reflex
        lawan = S.SkillCall("lawan")
        combat = context.get("combat")
        t = threat(combat, facts.health, damaged_at=p.damaged_at, now=facts.time_of_day)
        refusal = defend_refusal(combat, facts.inventory, t, facts.location)
        if lawan not in calls or not reflex_due(p, t, refusal, facts.time_of_day):
            if t is None:
                p.reflex_due = False  # the attack is over; nothing left to answer
            return None
        trigger = "under_attack_row" if p.reflex_due else "native_hit" if t["took_damage"] else "contact"
        p.reflex_due, p.reflex_pending = False, True
        p.reflex_starts.append(minutes(facts.time_of_day))
        session.model_pick = None
        self._journal(dict(ev="live7_reflex", trigger=trigger, time_of_day=facts.time_of_day, hp=t["hp"],
                           max_hp=t["max_hp"], monsters=t["monsters"], weapon=weapon_of(combat, facts.inventory)[0],
                           auto_starts_in_window=sum(0 <= minutes(facts.time_of_day) - s < REFLEX_WINDOW_MIN
                                                     for s in p.reflex_starts), cap=REFLEX_AUTO_MAX))
        return lawan

    def _live7_exhaustion(self, run, goal, motor):
        """Stop known exhaustion between inputs; never clear an uncertainty fence.

        An already felled tree may still need its drops collected. Its original
        verifier owns completion; low energy is not a success or a stuck label.
        """
        from arti_stardew_runtime import READY
        if (not run.state.get("live7") or run.call.name not in ENERGY_TOOLS
                or goal is None or goal.goal_id != run.goal_id or self._status != READY
                or self._worker_stop.is_set() or self._action_cancel.is_set()
                or self._gather_mutation_unresolved or getattr(goal, "gather_uncertain", False)
                or getattr(goal, "tool_work_uncertain", False)
                or getattr(getattr(goal, "tool_work", None), "status", None) == "UNCERTAIN"
                or getattr(getattr(goal, "gather_progress", None), "status", None) in
                    ("UNCERTAIN", "PENDING_PICKUP", "INVENTORY_SATISFIED") or self._stage_lock_holds()):
            return None
        action = getattr(goal, "last_action", None)
        if action is not None and (S._g(action, "cleanup_confirmed") is not True
                or S._g(action, "lifecycle") not in CLEAN_LIFECYCLES
                or S._g(action, "world_effect_status") in ("uncertain", "possible_mutation", "unverified")):
            return None
        pid = self._binding_view()[1]
        current = self._state.latest(pid)
        if motor is None or current is None or self._state.is_stale(pid):
            return None
        energy = getattr(current, "energy", None)  # may have dropped since the decision snapshot
        if (type(energy) in (int, float) and energy < VERY_LOW_ENERGY
                or S._g(action, "reason") == "exhausted" and S._g(action, "lifecycle") == "rejected"
                    and S._g(action, "world_effect_status") == "zero_effect"):
            if not run.state.get("live7_exhaustion_logged"):
                run.state["live7_exhaustion_logged"] = True
                self._journal(dict(ev="live7_exhausted", skill=run.call.name, energy=energy,
                                   request_id=S._g(action, "request_id"), threshold=VERY_LOW_ENERGY))
            return "fail", "exhausted"
        return None

    def _live7_attack(self, run, goal, motor):
        """Smoke 2026-10-23 #12: a monster attack ends a running work row between inputs as a recoverable
        CLEAN_FAIL (``under_attack``), with the same fences as exhaustion: never over an uncertainty, an
        unclean last action or a locked inventory stage. The next Live7 offer leads with the way home.
        """
        if run.call.name in S.MINES_SKILLS:
            idle = self._skill_mines_idle_damage(run, goal, motor)
            return idle if idle is not None else self._live7_floor_contact(run, goal, motor)
        floor_contact = self._live7_floor_contact(run, goal, motor)
        if floor_contact is not None:
            return floor_contact
        from arti_stardew_runtime import READY
        if (not run.state.get("live7") or not attack_work(run.call, run.state) or run.pending is not None
                or goal is None or goal.goal_id != run.goal_id or self._status != READY
                or getattr(goal, "progress_marker", None) == "terminal"  # a finished row is never relabelled
                or self._worker_stop.is_set() or self._action_cancel.is_set()
                or getattr(self, "_gather_mutation_unresolved", False) or getattr(goal, "gather_uncertain", False)
                or getattr(goal, "tool_work_uncertain", False)
                or getattr(getattr(goal, "tool_work", None), "status", None) == "UNCERTAIN"
                or getattr(getattr(goal, "gather_progress", None), "status", None) in
                    ("UNCERTAIN", "PENDING_PICKUP", "INVENTORY_SATISFIED")
                or getattr(self, "_stage_lock_holds", lambda: False)()):
            return None
        action = getattr(goal, "last_action", None)
        if action is not None and (S._g(action, "cleanup_confirmed") is not True
                or S._g(action, "lifecycle") not in CLEAN_LIFECYCLES
                or S._g(action, "world_effect_status") in ("uncertain", "possible_mutation", "unverified")):
            return None
        pid = self._binding_view()[1]
        current = self._state.latest(pid)
        session = getattr(self, "_skill_session", None)
        c = getattr(session, "campaign", None)
        if (motor is None or current is None or self._state.is_stale(pid) or getattr(c, "live", None) is None
                or getattr(current, "active_menu", None) or getattr(motor, "active_menu", None)):
            return None  # an open menu (chest, shipping, dialogue) keeps its own cleanup; never end the row over it
        rec = self._obs.get(pid, "ui.context")
        facts = getattr(rec, "facts", None)
        received = getattr(rec, "received_monotonic", None)
        if (not isinstance(facts, Mapping) or type(received) not in (int, float)
                or not 0 <= time.monotonic() - received <= 2):
            return None
        combat = facts.get("combat")
        why = attack_interrupt(run.call, run.state, combat, getattr(current, "health", None), c.live,
                               getattr(current, "time_of_day", None))
        if why is None:
            return None
        c.live.reflex_due = True  # the next boundary may answer with the lawan reflex (reflex_due checks the rest)
        if not run.state.get("live7_attack_logged"):
            run.state["live7_attack_logged"] = True
            self._journal(dict(ev="live7_under_attack", skill=run.call.name, arg=run.call.arg,
                               hp=combat.get("hp"), start_hp=run.state.get("live7_attack_hp"),
                               monsters=[[m.get("type"), m.get("distance")] for m in (combat.get("monsters") or ())[:3]
                                         if isinstance(m, Mapping)],
                               time_of_day=getattr(current, "time_of_day", None), action="end_row_recoverable"))
        return "fail", why

    def _live7_route_scene(self, location):
        """ADR 155: the fresh (<=2 s) scene facts of ``location``, or {} (a read of the previous map names nobody)."""
        from arti_runtime.route_pause import scene_on_map
        rec = self._obs.get(self._binding_view()[1], "scene.current_location")
        received = getattr(rec, "received_monotonic", None)
        if (rec is None or getattr(rec, "available", True) is False or getattr(rec, "stale", False)
                or type(received) not in (int, float) or not 0 <= time.monotonic() - received <= 2):
            return {}
        from arti_runtime.social_facts import with_greeted
        # ADR 161: a villager she verified talking to today is no reason to pause, even before the native flag catches up.
        return with_greeted(scene_on_map(getattr(rec, "facts", None), location),
                            getattr(self, "_live7_greeted_names", frozenset)())

    def _live7_route_pause_fence(self, p, facts):
        """ADR 155: why no pause may start here, or None. Low HP / low energy / a recent hit keep their own way home."""
        if ((facts.health is not None and facts.health < 30)
                or (facts.energy is not None and facts.energy < VERY_LOW_ENERGY)):
            return "low_vitals"
        if p.reflex_due or (type(p.damaged_at) is int and type(facts.time_of_day) is int
                            and 0 <= minutes(facts.time_of_day) - minutes(p.damaged_at) <= DAMAGE_RECENT_MIN):
            return "recent_damage"
        return None

    def _live7_route_pause_due(self, session, call, facts, previous, *, reserve):
        """ADR 155: the pause record when ``call``'s trip just crossed from ``previous`` into ``facts.location`` and that
        map shows a villager she has not talked to today, else None. One pause per map per game day. Talking and
        continuing must both pass the existing clock admission here (a talk she could not choose is no reason to stop).
        Arrival at the destination map is never a pause (``destination_map``, including generic ``@Location``)."""
        from arti_runtime.route_pause import new_villagers
        c = session.campaign
        p = c.live
        location = facts.location
        date = tuple(c.date)
        if (getattr(self, "_config", None) or {}).get("stardew_live7_mail_on_the_way"):
            from arti_runtime import mail_pause  # the flagged mailbox beside her door, once a day
            mail = mail_pause.due(self, session, call, facts, previous, reserve=reserve)
            if mail is not None:
                return mail
        if (not location or location == previous or (date, location) in p.route_pauses
                or destination_map(call.arg) == location or self._live7_route_pause_fence(p, facts) is not None):
            return None
        if (getattr(self, "_config", None) or {}).get("stardew_live7_ship_on_the_way"):
            from arti_runtime import ship_pause  # ADR 228 part D: the box on the way home, once a day
            box = ship_pause.due(self, session, call, facts, previous, reserve=reserve)
            if box is not None:
                return box
        who = new_villagers(self._live7_route_scene(location))
        if not who:
            return None
        from arti_runtime.travel_time import admission_budget
        if (type(facts.time_of_day) is not int or minutes(facts.time_of_day) >
                admission_budget(S.SkillCall("ngobrol_hadiah", who[0]["name"]), facts, p)["cutoff_min"]):
            return None
        if not reserve and (admission(call, facts, p) is not None or p.blocked(call, location)):
            return None
        return dict(date=list(date), map=location, from_map=previous, destination=call.arg, reserve=reserve,
                    villagers=[w["name"] for w in who], villager_refs=[w["ref"] for w in who],
                    clock=facts.time_of_day)

    def _live7_route_pause_start(self, session, pause):
        p = session.campaign.live
        p.route_pauses.add((tuple(pause["date"]), pause.get("once") or pause["map"]))
        p.route_pause = dict(pause)
        self._journal(dict(ev="live7_route_pause", session_id=session.session_id, **pause))
        if (getattr(self, "_config", None) or {}).get("voice_game_memory_enabled") is True:
            from arti_runtime import day_record
            run = session.current
            left_event = run is not None and run.state.get("starting_native_event") and not (
                (self._live7_native_event() or {}).get("event_up") is True)
            if left_event:
                run.state.pop("starting_native_event", None)
            label = day_record.place_word(pause["map"], getattr(self, "_live7_raw_knowledge", None))
            day_record.live(self, pause["date"], pause["clock"], "event_left" if left_event else "travel_arrived",
                f"Aku keluar dari acara dan sampai di {label}." if left_event else f"Aku sampai di {label}.",
                place=pause["map"])

    def _live7_route_pause(self, run, goal, motor):
        """ADR 155/195: a model-chosen ``pergi_ke`` that crosses into a map showing a villager she has not talked to
        today pauses once per map per day for a people page. The pause is not a failed walk. Nobody new: re-observe
        and continue. Arrival at the destination is never a pause. Same fences as ``_live7_attack``; never on the
        clock reserve's own route (that pauses at its hop boundary, ``_live7_reserve_pause``)."""
        from arti_stardew_runtime import READY
        session = getattr(self, "_skill_session", None)
        c = getattr(session, "campaign", None)
        if (not run.state.get("live7") or run.call.name != "pergi_ke" or run.pending is not None
                or run.state.get("route_clear") or getattr(c, "live", None) is None or c.live.sleep_only
                or goal is None or goal.goal_id != run.goal_id or self._status != READY
                or getattr(goal, "progress_marker", None) == "terminal"
                or self._worker_stop.is_set() or self._action_cancel.is_set()
                or getattr(self, "_gather_mutation_unresolved", False)
                or getattr(self, "_stage_lock_holds", lambda: False)()):
            return None
        action = getattr(goal, "last_action", None)
        if action is not None and (S._g(action, "cleanup_confirmed") is not True
                or S._g(action, "lifecycle") not in CLEAN_LIFECYCLES
                or S._g(action, "world_effect_status") in ("uncertain", "possible_mutation", "unverified")):
            return None
        location = getattr(motor, "location", None)
        facts = self._skill_current_facts()
        if (motor is None or not location or facts is None or facts.location != location
                or getattr(motor, "active_menu", None) or not facts.player_free or facts.menu_open):
            return None
        previous = run.state.setdefault("live7_route_map", location)
        if previous == location:
            return None
        if not self._live7_route_scene(location):
            return None  # the scene has not caught up with the warp: look again at the next boundary
        run.state["live7_route_map"] = location  # this map is judged once per run, on its first fresh read
        dest = destination_map(run.call.arg)
        if dest == location:
            return None  # the walk's own arrival verifier finishes; a destination is not a hop pause
        pause = self._live7_route_pause_due(session, run.call, facts, previous, reserve=False)
        if pause is None:
            seen = run.state.setdefault("live7_route_continued", [])
            if location not in seen:
                seen.append(location)
                self._journal(dict(ev="live7_route_continue", session_id=session.session_id,
                                   map=location, from_map=previous, destination=run.call.arg,
                                   villagers=[], villager_refs=[]))
            return None
        self._live7_route_pause_start(session, pause)
        from arti_runtime.route_pause import ROUTE_PAUSE_MAIL, ROUTE_PAUSE_PEOPLE
        return "fail", ROUTE_PAUSE_MAIL if pause.get("mail") else ROUTE_PAUSE_PEOPLE

    def _live7_reserve_pause(self, session, facts):
        """ADR 155: the clock reserve's home route runs hop by hop, each hop its own plan row. After a hop into a new map
        that shows a villager she has not talked to today, while the existing clock admission still admits a talk here,
        the reserve is released and she gets an ordinary page (the way home stays offered; ``home_due`` latches the
        reserve again on the first tick it is due). Never after a home failure, a proven block or at curfew."""
        c = session.campaign
        p = c.live
        last = session.runs[-1] if session.runs else None
        if (not p.sleep_only or home_due(facts, p) or p.home_failures or p.home_unreachable is not None
                or p.home_blocked is not None or last is None or last.call.name != "pergi_ke"
                or not facts.player_free or facts.menu_open
                or str(facts.location or "").startswith(("Cabin", "FarmHouse"))):
            return False
        previous = (last.state.get("live7_baseline") or {}).get("location")
        if not previous or previous == facts.location:
            return False
        pause = self._live7_route_pause_due(session, S.SkillCall("pergi_ke", "home"), facts, previous, reserve=True)
        if pause is None:
            return False
        p.sleep_only = False
        self._live7_route_pause_start(session, dict(pause, reserve_released=True))
        return True

    @staticmethod
    def _live7_baseline(facts, motor, context):
        from arti_stardew_tool_work import live_crop
        # Native today's weather (goal_route.weather, DLL 0ff66c60+): the post-live report scores M2 rain
        # from it. Journal only; the planner reads it in _live7_context. None on an older mod.
        route = (context.get("mines_ui") or {}).get("goal_route")
        route = route if isinstance(route, Mapping) else {}
        weather = route.get("weather")
        if not isinstance(weather, str) and route.get("is_raining") is True:
            weather = "rain"  # same fallback as _live7_context
        from arti_runtime.fact_names import source_names
        return dict(inventory=dict(facts.inventory), names=source_names(context), money=context.get("money"), location=facts.location,
                    location_id=context.get("location_id"),
                    energy=facts.energy,
                    time_of_day=getattr(motor, "time_of_day", None),
                    tile=getattr(motor, "tile", None), pixel=getattr(motor, "pixel", None),
                    crops={ref: dict(f["crop"]) for ref, f in context.get("targets", {}).items() if live_crop(f)},
                    targets=sorted(context.get("targets", {})),
                    weather=weather if isinstance(weather, str) else None)

    def _live7_start_baseline(self, run, facts, motor, context):
        """The bag before this skill, journaled once it is chosen. FIX77B: with the native name of the target she chose, for its row."""
        from arti_runtime.fact_names import target_name
        base = run.state.setdefault("live7_baseline", self._live7_baseline(facts, motor, context))
        if stay := getattr(self, "_live7_visit_open", None):
            run.state.update(visit_start=stay["at"],
                             visit_prior_request=S._g(S._g(self._active, "last_action"), "request_id"))
        if run.call.name == "pergi_ke":
            run.state.setdefault("travel_observed_route", [facts.location])
            if (getattr(self, "_config", None) or {}).get("voice_game_memory_enabled") is True:
                event = self._live7_native_event() or {}
                if event.get("event_up") is True and event.get("event_id"):
                    run.state["starting_native_event"] = event["event_id"]
        if name := target_name(run.call.arg, context):
            base["names"] = {**(base.get("names") or {}), run.call.arg: name}
        self._journal(dict(ev="live7_baseline", skill=run.call.name, arg=run.call.arg, before=base))

    def _live7_recovery_finish(self, session, run, action, cleanup, facts, motor, context):
        """Retain a finish only on a fresh released boundary of the same run."""
        from arti_stardew_ui_effects import same_date

        identity = tuple(context.get("identity") or ())[:5]
        if (len(identity) != 5 or identity != session.campaign.epoch
                or not same_date(session.campaign.date, self._date_tuple(motor))
                or S._g(self._active, "goal_id") != run.goal_id or not self._live7_recovery_open(session)):
            return None
        pending = run.pending
        observed = None
        # A mail finish adds its independently verified letter receipts.
        mail = (pending == ("done", "observed_morning_plan_input")
                and run.call.name == "cek_pagi" and run.call.arg in (None, "mail"))
        if mail:
            plan = run.state.get("plan_input") or {}
            letters = len(plan.get("mail_receipts") or ())
            if (not letters or letters != plan.get("letters_read") or plan.get("complete") is not True
                    or identity != run.state.get("morning_identity")
                    or not same_date(run.state.get("morning_date"), self._date_tuple(motor))):
                return None
        elif (getattr(self, "_config", None) or {}).get("stardew_live7_success_stays") is not True or action is None:
            return None
        if action is not None and (S._g(action, "cleanup_confirmed") is not True
                or S._g(action, "lifecycle") not in CLEAN_LIFECYCLES
                or S._g(action, "world_effect_status") == "unreconciled"):
            return None
        if pending is None:
            from arti_stardew_fishing import released_catch
            before = (run.state.get("live7_baseline") or {}).get("inventory")
            catch = released_catch(self._active, action, before, facts.inventory)
            if catch is not None:
                why, observed = catch
                pending = ("done", why)
        status = "READY" if self._status == "PAUSED_RECOVERY" else self._status
        if (pending is None or pending[0] != "done" or classify(pending, cleanup, action, status=status,
                mutation_unresolved=self._gather_mutation_unresolved) != VERIFIED):
            return None
        if mail:
            run.state["recovered_mail_letters"] = letters
        return pending, observed

    def _live7_leg(self, session, run, outcome, cleanup, *, recover_finish=None):
        # Recovery passes True to consider a finish or False if an explicit failure owns the boundary.
        c = session.campaign
        p = c.live
        halt_interrupted = S._g(cleanup, "halt_interrupted") is True or (
            outcome[0] == "fail" and self.__dict__.get("_live7_ladder_halted") == session.session_id
            and not getattr(self, "_gather_mutation_unresolved", False))
        if run.call.name == "lawan_mine":
            from arti_runtime.mines_fight import close
            outcome = close(run, *outcome)
        facts, motor, context, held = self._live7_read(session)
        action = getattr(self._active, "last_action", None)
        refusal = journal_refusal(S._g(action, "reason", "")) or journal_refusal(held) or journal_refusal(outcome[1])
        if refusal:
            outcome = ("fail", refusal)
            self._live7_journal_alert(session, refusal)
        stopped = self._worker_stop.is_set() or getattr(self, "_owner_play_stopped", False)
        identity_ok = held not in ("identity_mismatch", "crash_or_identity_unavailable")
        fence = bool(getattr(self, "_gather_mutation_unresolved", False))
        recovered = (self._live7_recovery_finish(session, run, action, cleanup, facts, motor, context)
                     if recover_finish and outcome[0] == "fail" and not held and not refusal else None)
        if recovered:
            outcome, recovered_observation = recovered
            run.state.pop("ui_pause", None)  # the UI hold ended on this fresh read
        status = "READY" if self._status == "PAUSED_RECOVERY" and recovered else self._status
        category = classify(outcome, cleanup, action, status=status, stopped=stopped,
                            identity_ok=identity_ok, mutation_unresolved=fence)
        from scripts.live7.ready_checks import sleep_cancelled
        readiness = run.state.get("generic_ui_readback") or {}
        if (run.call.name == "tidur" and outcome[1] == "sleep_readiness_cancelled"
                and sleep_cancelled(readiness)):
            # ADR 154: the generic "cancel" uncertainty rule concerns unresolved
            # input. This exact native receipt proves withdrawal, not a bad bed
            # route. Reclassify only the reason; all safety/action fences remain.
            category = classify(("fail", "observed_sleep_readiness_withdrawn"), cleanup, action,
                status=self._status, stopped=stopped, identity_ok=identity_ok, mutation_unresolved=fence)
        if isinstance(cleanup, Mapping) and cleanup.get("food_clear") is False:
            run.state["recovery_food_required"] = True
        arrived = run.state.get("arrived_native") if outcome[0] == "done" and isinstance(cleanup, Mapping) else None
        if arrived and cleanup.get("location") == arrived["location"]:
            # ADR 105: the event froze her after the trip's own verdict. Only the free/menu terms, which are the event's, are lifted.
            category = classify(outcome, dict(cleanup, menu=None, free=True), action, status=status,
                                stopped=stopped, identity_ok=identity_ok, mutation_unresolved=fence)
        if (category in (SAFETY_STOP, UNCERTAIN) and isinstance(cleanup, Mapping) and cleanup.get("menu") is not None
                and cleanup.get("input_released") is True
                and unplanned_ui_reason(S._g(action, "reason", ""), outcome[1])):
            # The native walk release is confirmed. Remove only the open-UI terms when classifying;
            # identity, STOP, journal faults and possibly-applied world mutations retain precedence.
            category = classify(outcome, dict(cleanup, menu=None, free=True), action, status=self._status,
                                stopped=stopped, identity_ok=identity_ok, mutation_unresolved=fence)
            if category != SAFETY_STOP:
                benign = (not fence and (category != UNCERTAIN
                    or "menu_opened_mid_move" in str(S._g(action, "reason", "")).casefold()
                    and S._g(action, "cleanup_confirmed") is True))
                if benign:
                    category = NOT_READY
                    run.state["ui_pause"] = True
                    self.__dict__.setdefault("_live7_ui_paused", set()).add(session.session_id)
                self._journal(dict(ev="live7_ui_interrupted", skill=run.call.name, action="pause_and_inspect",
                                   input_released=True, replay=False))
        if run.state.get("ui_pause") and category != SAFETY_STOP and not fence:
            category = NOT_READY  # the UI may already have closed before this row's finalization
        if (getattr(self, "_config", None) or {}).get("live7_fact_line_enabled") is True:
            from arti_runtime import walk_refusal  # ADR 231 part B1: the game's dialogue ended the walk; a fact
            category = walk_refusal.category(outcome, category, fence)
        if outcome[1] == KNOCKOUT_REASON and category == SAFETY_STOP and p.knockout is not None:
            # M12: the clinic wake-up event holds the menu; everything else must be clean. The recovery poll
            # advances the event (bounded) and the day continues; any other safety term still stops.
            patched = dict(cleanup, menu=None, free=True, food_clear=True) if isinstance(cleanup, Mapping) else cleanup
            category = classify(outcome, patched, action, status=self._status, stopped=stopped,
                                identity_ok=identity_ok, mutation_unresolved=fence)
            if category not in (SAFETY_STOP, UNCERTAIN):
                category = NOT_READY
        # F6: a done bed whose first wake read is held is re-checked on the fresh read.
        morning_pending = run.call.name == "tidur" and self._live7_morning_pending()
        sleep_recheck = category == VERIFIED and (held or morning_pending) and run.call.name == "tidur"
        from arti_runtime.mines_fight import completion
        native_mines = (outcome[0] == "done" and outcome[1] in ("exited_mines", "observed_mines_progress")
                        and self._active is not None and self._active.goal_id == run.goal_id
                        and (not held or str(held).startswith("runtime_not_ready:"))
                        and completion(run, outcome, cleanup, getattr(self._active, "last_finish", None)))
        if native_mines:
            # The native receipt already proved this effect; a missing bag projection / revoked
            # play grant can pause the NEXT action without erasing the completed Mines work.
            category = classify(outcome, cleanup, action, status=self._status, stopped=stopped,
                                identity_ok=identity_ok, mutation_unresolved=fence, native_mines=True)
        if category == VERIFIED and (held or morning_pending) and not native_mines:
            category = NOT_READY
        if halt_interrupted and category != SAFETY_STOP:
            category = PRECONDITION
            run.state["halt_interrupted"] = True
        observed = S._g(getattr(self._active, "last_finish", None), "observed", {}) or {}
        last = getattr(self._active, "last_finish", None)
        if recovered and recovered_observation is not None:
            observed = recovered_observation
        if arrived and not observed:
            observed = dict(objective="enter_location", transitions=arrived["transitions"])
        cleanup_clean = (isinstance(cleanup, Mapping) and cleanup.get("input_released") is True
                         and cleanup.get("menu") is None and cleanup.get("free") is True
                         and cleanup.get("food_clear") is not False)
        if (category == CLEAN_FAIL and cleanup_clean and not held and not fence
                and self._active.goal_id == run.goal_id and S._g(last, 'status') == 'partial'
                and S._g(last, 'reason') == outcome[1]
                and outcome[1] in ('observed_paired_transfer_partial', 'observed_ship_partial', 'observed_bag_sitting_partial')
                and (observed.get('actual_stored') or observed.get('actual_shipped')
                     or ((observed.get('bag_sitting') or {}).get('closed') is True
                         and any(r.get('moved',0)>0 for r in observed['bag_sitting']['items'])
                         and all(r.get('state')!='unverified' for r in observed['bag_sitting']['items'])))):
            category = 'PARTIAL'  # native movement is real; the original objective remains incomplete
        if (category == UNCERTAIN and cleanup_clean and not held and outcome[0] == "done"
                and "unconfirmed" not in str(outcome[1]).lower()
                and self._active.goal_id == run.goal_id and S._g(last, "status") == "achieved"
                and S._g(last, "reason") == outcome[1]):
            category = VERIFIED  # an independent native verifier settled the effect; no replay
        if (category == UNCERTAIN and run.call.name == "tidur" and not fence and S._g(last, "status") == "achieved"
                and observed.get("answered") is True and tuple(observed.get("start_date", ())) == c.date):
            sleep_recheck = True  # ADR 105 (a47 d5 J1426): a native bed verdict, whatever else was unclean; never a pass-out
        answer = run.state.get("native_sleep_answer")
        if (run.call.name == "tidur" and answer and S._g(last, "status") != "achieved"
                and category != SAFETY_STOP and not stopped and identity_ok
                and tuple(answer.get("start_date", ())) == c.date
                and (self._date_tuple(motor) != c.date or self._live7_morning_pending())):
            # ADR 148: a62's native Locked sleep check independently proved Yes,
            # despite the lease timeout. Keep it across row cleanup; only a fresh
            # native morning may turn it into credit. Never replay the answer.
            sleep_recheck = True
            observed = dict(answer)
            category = NOT_READY
        from arti_runtime.sleep_truth import bed_evidence
        if (run.call.name == "tidur" and bed_evidence(observed) and category != SAFETY_STOP
                and outcome[1] not in ("sleep_readiness_cancelled", "sleep_not_started")
                and tuple(observed.get("start_date") or ()) == c.date and not c.finished):
            sleep_recheck = True  # ADR 198: a native bed Yes, including a held/NOT_READY row; never a pass-out
        if category == VERIFIED and run.call.name == "tidur":
            if self._live7_sleep_honest(c, observed, motor):
                c.days[-1]["transition"] = list(self._date_tuple(motor))
                c.finished = True
            else:
                category, outcome = UNCERTAIN, ("fail", "sleep_transition_unverified")
        if category == VERIFIED and run.call.name == "forage":
            receipt = observed.get("forage") or {}
            consumed = receipt.get("source_fact_id")
            if (isinstance(consumed, str) and receipt.get("delta", 0) > 0
                    and "inventory" in receipt.get("verified_by", ())):
                # R7: the pickup was real; the global scene kept the consumed Daffodil.
                # Exclude this exact ref for the day, leaving other forage available.
                p.refs.add(consumed)
                p.forage_memory.forget(consumed)
                p.seen_objects.forget(consumed)
                for loc in list(p.forage_sightings):
                    p.forage_sightings[loc] = [e for e in p.forage_sightings[loc] if e["ref"] != consumed]
        target = (run.state.get("wave1_pin") or {}).get("ref") or S._g(
            getattr(self._active, "approach_progress", None), "target_ref")
        if (run.call.name == "tidur" and category == CLEAN_FAIL and not held and not fence
                and outcome[1] in ("no_effect_progress", "sleep_readiness_cancelled")
                and sleep_cancelled(readiness)):
            # N smoke L607/L610/L611: cancelling readiness is a failed sleep, not a failed approach.
            # Retiring the already-reached side stranded her on the bed when the other side was blocked.
            # Keep the successful route reusable; all native movement/entry gates and retry caps still apply.
            target = None
            outcome = ("fail", "sleep_readiness_cancelled")
        if (run.call.name == "tidur" and category == CLEAN_FAIL and not held and not fence
                and outcome[1] == "sleep_not_started"):
            # fix54H (a39 Sp13 D0009:J959-J1319): her Yes opened no ReadyCheck. Retiring the reached bed side
            # left both sides quarantined -> bed_not_offered x8 -> pass-out. A failed sleep, not a failed approach.
            target = None
        if category == CLEAN_FAIL and not held and not fence and outcome[1] in ("goods_menu_changed", "chest_full"):
            # FIX73B (a54 d3 J1623-J1648, J1668, J1852): the host's gift question replaced the menu after she reached the
            # chest. Nothing was wrong with the chest; retiring it for the day ended in chest_not_offered once she stepped away.
            # Same for chest_full: an item with an existing stack still fits, and she may still open the chest herself.
            target = None
        if run.call.name == "siram":
            target = run.state.get("water_crop_ref")  # only this run's crop can receive the failure
        before = run.state.get("live7_baseline", {})
        # A held read is no evidence: it must not freeze the whole bag. The
        # fresh post-release read in _live7_recover recomputes the delta.
        after = self._live7_baseline(facts, motor, context) if not held and not halt_interrupted else None
        if category == VERIFIED and run.call.name == "siram":
            ref = observed.get("target_ref")
            dry = before.get("crops", {}).get(ref, {})
            wet = (after or {}).get("crops", {}).get(ref, {})
            if (observed.get("kind") != "water_tile" or dry.get("watered") is not False
                    or wet.get("watered") is not True
                    or any(dry.get(k) != wet.get(k) for k in ("qualified_item_id", "phase"))):
                category, outcome = CLEAN_FAIL, ("fail", "watered_crop_unverified")
        items = self._live7_changed_items(before, after) if after else set()
        if category == UNCERTAIN:
            items |= self._live7_call_items(run.call)
        from arti_runtime.action_outcomes import unreadable
        unknown = not halt_interrupted and unreadable(outcome[1])
        if unknown and category != SAFETY_STOP:
            category = UNCERTAIN  # every recoverable read gap owes the same observation
            items |= self._live7_call_items(run.call)
        prior_holds = (set(p.refs), set(p.quarantine), set(p.uncertain_items), set(session.blocked_refs)) if unknown else None
        location = getattr(motor, "location", None)
        unpublished_walk = (S._g(action, "lifecycle") == "not_taken"
                            and S._g(action, "request_id") is None and S._g(action, "cleanup_confirmed") is True
                            and S._g(action, "reason") in ("approach_partial", "approach_replan_required",
                                                             "approach_target_blocked"))
        if (not held and not fence and (category == CLEAN_FAIL or unpublished_walk)
                and run.call.name == "pergi_ke" and outcome[1] == "exit_not_offered"
                and before.get("location") == location):
            # R3 J2198/2321: a failed walking approach poisoned the native warp
            # across goals. Released input + a fresh same-location read permits
            # a NEW corridor approach, never replay of a crossing or a mutation.
            fact = context.get("targets", {}).get(target) or {}
            if fact.get("kind") in ("warp", "door", "building_door"):
                self._active.approach_blocked_refs.discard(target)
                session.blocked_refs.discard(target)
                p.refs.discard(target)
                target = None
        # live7-game-m10 D3 J2731 / D4 J2883: the store row ended on a refused, never-written press (not_taken,
        # request_id null) and its goal never reached the store chain. Only then can "nothing was stored" be claimed.
        goal_now = self._active
        unsent = (run.call.name == "simpan_ke_chest" and not held and not fence
                  and goal_now is not None and goal_now.goal_id == run.goal_id
                  and not getattr(goal_now, "store_chain_attempted", False)
                  and S._g(action, "lifecycle") == "not_taken" and S._g(action, "request_id") is None
                  and (cleanup or {}).get("menu") is None and (cleanup or {}).get("input_released") is True)
        store_check = None
        if unsent and category == CLEAN_FAIL:
            # A clean refusal that sent nothing: reported with its reason; her chest is not retired for the day.
            outcome = ("fail", self._live7_store_miss_reason(outcome[1], action))
            target = None
            self._journal(dict(ev="live7_store_miss_reported", skill=run.call.name, arg=run.call.arg,
                               was=CLEAN_FAIL, reason=outcome[1], read="terminal_clean"))
        elif (unsent and category == UNCERTAIN and before.get("inventory")
              and str(target or "").startswith("obj:") and str(target).endswith(CHEST_SUFFIXES)):
            # Stays UNCERTAIN (quarantined) until the fresh post-release read: _live7_reconcile relabels it only if the
            # bag is unchanged there too, and then undoes exactly what this row added (snapshots below).
            store_check = dict(chest=target, reason=outcome[1], last_action=S._g(action, "reason", None),
                               refs=set(p.refs), blocked=set(session.blocked_refs),
                               quarantine=set(p.quarantine), items=set(p.uncertain_items))
        from arti_runtime import loop_facts, raw_hands
        movement = run.state.get("step_movement") if run.call.name == "langkah" else None
        if category == VERIFIED and movement and movement["kind"] == raw_hands.STILL:
            category = NO_EFFECT
        if recovered and category != VERIFIED:
            run.state.pop("recovered_mail_letters", None)
            outcome = ("fail", outcome[1])
        if (recover_finish is not None and outcome[0] == "fail" and not unknown
                and category not in (SAFETY_STOP, INTERRUPTED, "PARTIAL")):
            from arti_runtime.action_outcomes import success_observation
            if success_observation(outcome[1]):
                run.state["recovery_observed_reason"] = outcome[1]
                outcome = ("fail", "recovery_unverified")
        if recovered or run.state.get("recovery_observed_reason"):
            run.state["recovery_terminal"] = outcome
        productive = (category == VERIFIED and (bool(items) or
            after is not None and before.get("money") != after.get("money")
            or run.call.name not in ("pergi_ke", "jalan_ke", "tidur", "cek_pagi")
            and not any(token in outcome[1] for token in ("_no_", "no_effect", "already_"))))
        if run.call.name == "langkah":
            productive = category == VERIFIED and movement is not None and movement["kind"] == raw_hands.MOVED
        if run.call.name == "cek_pagi":
            p.note_read(run.state.get("plan_input"))
        if halt_interrupted:
            loop = False  # A HALT supplies no evidence against the target or the work.
        elif run.state.get("ui_pause") and category == NOT_READY:
            loop = False  # a released UI pause is not a skill failure, quarantine or home retry
            p.history.append(dict(skill=run.call.name, arg=run.call.arg, outcome_class=NOT_READY,
                                  reason="unplanned_ui_pause", location=location, productive=False))
            del p.history[:-8]
        else:
            route_refused = False
            if category == CLEAN_FAIL and not held:
                from arti_runtime import offer_withhold
                here = S._wave1_xy(S._g(motor, "tile"))
                if here is not None and (ref := offer_withhold.failed_route_ref(run, outcome[1], location)):
                    now = game_now(self)
                    clock = self._live7_effect_clock(session, now)
                    evidence = self._live7_effect_evidence(facts, motor, context, clock.evidence or {})
                    self._live7_effect_watch(session, now, evidence, getattr(motor, "active_menu", None), self._date_tuple(motor))
                    offer_withhold.bind_route(run, ref, clock, location, here, context.get("nav_grid"))
                    route_refused = True
            if category in (CLEAN_FAIL, UNCERTAIN, NOT_READY, PRECONDITION):
                from arti_runtime.mines_exit import note_failed
                note_failed(p, run, location, outcome[1], context)
            loop = p.record(run.call, category, outcome[1], target=target, location=location, items=items,
                            start_location=before.get("location"), productive=productive, time_of_day=facts.time_of_day,
                            tile=(context or {}).get("actor_tile"), route_refused=route_refused)
            if (getattr(self, "_config", None) or {}).get("stardew_live7_local_walks") is True:
                if (run.state.get("local_walk") and category == CLEAN_FAIL
                        and outcome[1] in ("walk_blocked", "local_walk_no_open_path", "local_walk_warp_blocked")
                        and run.call.arg and location):
                    p.refs.add(f"local_walk:{location}:{run.call.arg}")
            if observed.get('bag_sitting') and p.history:
                p.history[-1]['bag_sitting'] = observed['bag_sitting']
            if run.call.name == "langkah":
                if category in (VERIFIED, NO_EFFECT) or (category == UNCERTAIN and movement
                        and movement.get("transition") == "incidental"):
                    loop_facts.note_step(p.no_effect, run.call.arg, movement,
                                         target=run.state.get("step_target"), clock=facts.time_of_day,
                                         keep_presses=bool((getattr(self, "_config", None) or {}).get("stardew_live7_press_results")))
            elif category in (VERIFIED, CLEAN_FAIL):
                loop_facts.note(p.no_effect, run.call.name, run.call.arg, reason=outcome[1], clock=facts.time_of_day,
                                effect=loop_facts.native_effect(before, after, location, items))
            if (getattr(self, "_config", None) or {}).get("stardew_live7_press_results"):
                from arti_runtime import press_results
                try:
                    spot = press_results.note(p.press_results, run, category=category, before=before, after=after,
                                              location=location, items=items, date=c.date, clock=facts.time_of_day,
                                              context=context)
                except Exception as e:  # a count never stops a settled skill
                    spot = None
                    self._journal(dict(ev="live7_press_results_error", at="settle", error=type(e).__name__))
                if spot is not None:
                    press_results.tag_no_effect(p.no_effect, run.call, spot)
                    self._journal(dict(ev="live7_press_results", spots=press_results.export(p.press_results)))
        if fence and category == VERIFIED:
            # A native verifier settled this row's effect: the fence is resolved now.
            with self._lock:
                self._gather_mutation_unresolved = False
            self._journal(dict(ev="live7_fence_cleared", skill=run.call.name, by="native_verified"))
        elif fence and category == UNCERTAIN:
            p.fence_settled = True  # quarantined above; _live7_recover clears it after the fresh read
        if store_check is not None:
            store_check.update(added_refs=p.refs - store_check["refs"],
                               added_quarantine=p.quarantine - store_check["quarantine"],
                               added_items=p.uncertain_items - store_check["items"],
                               history=p.history[-1] if p.history else None)
        elif outcome[1].startswith("chest_not_reached:") and p.history:
            p.history[-1]["note"] = STORE_MISS_NOTE
        elif (category == CLEAN_FAIL and run.call.name == "simpan_ke_chest"
                and outcome[1] == "observed_paired_transfer_partial" and p.history):
            p.history[-1].update(transferred=observed.get("transferred"), requested=observed.get("requested"),
                note="Sebagian sudah tersimpan dan terverifikasi; jumlah yang diminta belum terpenuhi.")
        session.blocked_refs |= p.refs
        speaker = run.state.get("wave1_talk_speaker") if run.call.name == "ngobrol_hadiah" else None
        # Exit geometry memory bounds a failed route; day-long work-target
        # quarantine cannot erase a newly usable native way out (ADR 165).
        # A held/safety read has no current context. Preserve its fences and
        # hand-off; only a current floor read can restore native exit refs.
        if (not held and context is not None and category != SAFETY_STOP
                and str(location or "").startswith("UndergroundMine")):
            from arti_runtime.mines_exit import ups
            native_ups = {e["ref"] for e in ups(context, location, require_ready=False)}
            session.blocked_refs -= native_ups
            p.refs -= native_ups
            if self._active is not None:
                self._active.approach_blocked_refs -= native_ups
        if isinstance(speaker, str) and speaker.strip() and run.state.get("wave1_talk_done"):
            # ADR 161: the talk leg's verified speaker; the row's last leg (the gift, or the closing leg) has no talk
            # observation of its own (a recorded run: `observed: {}` read as a chat). Greeted once, today.
            from arti_runtime.social_facts import mark_greeted
            mark_greeted(getattr(p, "greeted", None), speaker, c.date)
            if not observed.get("speaker"):
                observed = dict(observed, speaker=speaker.strip())
        idle_damage = run.state.get("mines_idle_damage_fact")
        if outcome[1] == UNDER_ATTACK_REASON and isinstance(idle_damage, Mapping):
            observed = dict(observed, idle_hp=dict(idle_damage))
            if p.history:
                p.history[-1]["note"] = ("HP %s → %s turun tanpa input berjalan; keterampilan berakhir."
                                         % (idle_damage["hp_before"], idle_damage["hp"]))
        elif str(outcome[1]).startswith("mines_combat_resume_unavailable:") and p.history:
            p.history[-1]["note"] = "Batch tempur berakhir tanpa kill; batch berikutnya tidak berjalan; keterampilan berakhir."
        row = dict(task=c.task, skill=run.call.name, arg=run.call.arg, outcome_class=category,
                   verified=category == VERIFIED, reason=outcome[1], evidence=dict(cleanup=cleanup, observed=observed))
        if observed_reason := run.state.get("recovery_observed_reason"):
            row["observed_reason"] = observed_reason
        if category == VERIFIED and (letters := run.state.get("recovered_mail_letters")):
            row["evidence"]["observed"] = dict(observed, letters_read=letters)
        if movement:
            row["evidence"]["movement"] = movement
        if run.call.name == "lawan_mine":
            row["evidence"]["mines_progress"] = dict(run.state.get("mines_progress") or {})
        if unknown:
            row.update(outcome="unknown", outcome_id=f"{session.session_id}:{run.goal_id}:{len(c.days[-1]['skills'])}")
            p.history[-1].update(outcome="unknown", note="hasilnya belum kebaca")
            run.state.update(outcome="unknown", outcome_id=row["outcome_id"])
        if run.call.name == "pergi_ke" and run.call.arg == "mine":
            row["evidence"]["from_location"] = before.get("location")
        from arti_runtime.fact_names import source_names
        from arti_runtime.place_names import source_place
        row["evidence"]["names"] = dict(before.get("names") or {}, **source_names(context))
        if movement is not None and movement["kind"] == raw_hands.MOVED:
            start_tile = S._wave1_xy(movement["before"].get("tile"))
            end_tile = S._wave1_xy((S._g(action, "after") or {}).get("tile"))
            if start_tile is not None and end_tile is not None and start_tile != end_tile:
                row["step_tile_changed"] = True
        from arti_runtime.fact_names import inventory_gains
        from scripts.live7.transfers import chest_moves
        if moves := chest_moves(run.state.get("chosen_transfers")):
            row["evidence"]["chest_moves"] = moves
        if after is not None and facts.inventory_current and category != VERIFIED:
            row["evidence"]["inventory_gains"] = inventory_gains(before, after, row["evidence"]["names"], moves)
        if run.state.get("native_landed_catch"):
            row["evidence"]["landed_catch"] = dict(run.state["native_landed_catch"])
        row["evidence"]["place_name"] = source_place(location, (context or {}).get("world_knowledge"))
        row["evidence"]["objective"] = dict(skill=run.call.name, item=run.call.arg,
            quantity=run.call.n if type(run.call.n) is int else observed.get("requested"))
        fish = observed.get("fish") or {}
        if observed.get("effect") == "caught" and fish.get("item"):
            # The existing native catch log counts each request once, including ground catches.
            observed = dict(observed, kind_count_session=sum(r.get("item") == fish["item"] for r in p.catches) or None,
                            kind_count_run=p.catch_kinds.get(fish["item"]))  # FIX73D: the same kind across all days
            row["evidence"]["observed"] = observed
        if not held and (scene := getattr(session, "_persona_scene_state", None)) is not None:
            row["evidence"]["scene_state"] = scene
        if run.call.name == "panen" and category == VERIFIED:
            from arti_runtime.progress_counts import day_harvest
            if total := day_harvest([*c.days[-1]["skills"], row]):
                row["evidence"]["day_total"] = total  # ADR 110: this day's harvests by crop beside the one-row receipt
        for evidence_key in ("native_ui_episode", "terminal_cleanup_attempts"):
            if evidence_key in run.state:
                row["evidence"][evidence_key] = run.state[evidence_key]
        if arrived and category == VERIFIED:
            event = self._live7_native_event() or {}  # a separate fact: what held her, when the game says so
            row["evidence"]["arrived_native"] = dict(arrived, **({"event_id": event["event_id"]} if event.get("event_id") else {}))
        if outcome[1] == "sleep_readiness_cancelled":
            row["evidence"]["readiness"] = dict(readiness)
        if (category == VERIFIED and run.call.name == "cangkul" and location == "Farm"
                and observed.get("kind") == "till_tile" and observed.get("effect") == "tile_tilled"):
            from arti_stardew_tool_work import tile_ref
            for fact in (context.get("local_window") or {}).get("targets", {}).values():
                tile = S._wave1_xy(fact.get("tile"))
                if (fact.get("terrain_type") == "HoeDirt" and tile is not None
                        and observed.get("target_ref") == tile_ref(location, *tile)):
                    p.seed_own_soil.add(tile)
                    if tile in p.seed_own_order:
                        p.seed_own_order.remove(tile)
                    p.seed_own_order.append(tile)
                    row["evidence"]["hoed_soil"] = dict(location=location, tile=list(tile))
        if category == VERIFIED and run.call.name == "tanam" and after:
            tile = S._wave1_xy((run.state.get("plant_target") or {}).get("tile"))
            if tile is not None:
                ref = f"tf:{location}:{tile[0]},{tile[1]}:HoeDirt"
                crop = after.get("crops", {}).get(ref)
                if crop and crop.get("phase") == 0:
                    row["evidence"]["crop"] = dict(location=location, tile=list(tile), planted_date=list(c.date),
                        watered_date=list(c.date) if crop.get("watered") is True else None,
                        watered_time=getattr(motor, "time_of_day", None) if crop.get("watered") is True else None,
                        # ADR 45: the native crop identity at planting names the crop in her dated facts
                        item=crop.get("qualified_item_id") if isinstance(crop.get("qualified_item_id"), str) else None,
                        phase=0, state="growing")
        if category == UNCERTAIN and run.call.name == "tanam" and isinstance(run.state.get("plant_before"), dict):
            # ADR 76: an uncertain press keeps its exact target tile, seed kind and pre-press seed count as
            # facts, so a later native read (new crop there + one seed gone) can prove the planting hers.
            pb = run.state["plant_before"]
            tile = S._wave1_xy(pb.get("tile"))
            ref = f"tf:{location}:{tile[0]},{tile[1]}:HoeDirt" if tile is not None else None
            if ref and isinstance(pb.get("qid"), str) and type(pb.get("count")) is int and not before.get("crops", {}).get(ref):
                row["evidence"]["planting_attempt"] = dict(location=location, tile=list(tile), seed=pb["qid"],
                    seed_count_before=pb["count"], date=list(c.date), time=getattr(motor, "time_of_day", None))
        crops_before = {ref: dict(r) for ref, r in p.crop_memory.crops.items()}
        p.crop_memory.record(row, c.date, now=getattr(motor, "time_of_day", None))
        if category == UNCERTAIN and after is not None and p.crop_memory.uncertain:
            self._live7_crop_reconcile(session, after, read="terminal")
        if crops_before != p.crop_memory.crops:
            # Receipt-time snapshot, including the empty ledger after the last harvest.
            self._journal(dict(ev="live7_crop_memory", at=time.time(), game=dict(date=list(c.date)),
                               crops={ref: dict(r) for ref, r in p.crop_memory.crops.items()}))
        crop_ref = run.state.get("live7_crop_ref")
        if run.call.name == "jalan_ke" and crop_ref and not held and not halt_interrupted:
            self._live7_crop_walk_result(c, run, row, motor, context)
        if category == UNCERTAIN:
            row["evidence"]["reconcile"] = dict(before=before, after=after, items=sorted(items),
                                                read="held" if held else "terminal", replay=False, credit=False)
            pending = dict(row=row, before=before, call=run.call, store_check=store_check)
            if unknown:
                from copy import deepcopy
                pending.update(run=run, action=deepcopy(action), history=p.history[-1],
                    added_holds=(p.refs - prior_holds[0], p.quarantine - prior_holds[1],
                                 p.uncertain_items - prior_holds[2], session.blocked_refs - prior_holds[3]))
            p.pending_reconcile.append(pending)
        c.days[-1]["skills"].append(row)
        c.days[-1]["rows"][c.task] = row["verified"]
        run.state["live7_outcome"] = category
        if sleep_recheck:
            p.pending_sleep = dict(row=row, observed=dict(observed),
                                   reason=S._g(last, "reason") or "observed_day_change")
        self._journal(dict(ev="part_live_row", session_id=session.session_id, day=len(c.days), **row))
        if ((getattr(self, "_live7_visit_open", None) or getattr(self, "_live7_visit_closing", None))
                and not held and not halt_interrupted and not stopped
                and S._g(cleanup, "input_released") is True):
            from arti_runtime.place_visit_result import capture, settle
            pin = self._ui_key_context(motor.player_id)
            with self._lock:
                settle(self, capture(self, pin, c, motor), context, run, action, outcome, category, before, after, items)
            self._live7_flush_visit(c)
        result_location = arrived["location"] if recovered and arrived and row["verified"] else location
        if row["verified"] and (getattr(self, "_config", None) or {}).get("voice_game_memory_enabled") is True:
            from arti_runtime import day_record
            if (run.state.get("starting_native_event") and before.get("location") != location
                    and not (self._live7_native_event() or {}).get("event_up")):
                row["evidence"]["left_native_event"] = True
            day_record.skill(self, row, c.date, getattr(motor, "time_of_day", None), result_location,
                             catalog=(context or {}).get("world_knowledge"))
        if unknown:
            self._journal(dict(ev="part_live_outcome_unknown", session_id=session.session_id,
                outcome_id=row["outcome_id"], skill=run.call.name, arg=run.call.arg, reason=outcome[1],
                request_id=S._g(action, "request_id"), reobserve=category == UNCERTAIN, replay=False))
        from arti_runtime.travel_facts import remember_trip
        if run.call.name == "pergi_ke" and category == VERIFIED:
            path = run.state.setdefault("travel_observed_route", [before.get("location")])
            if result_location and path[-1] != result_location:
                path.append(result_location)
        if not halt_interrupted and (trip := remember_trip(
                                 p, run, ('done' if category == VERIFIED else 'fail', outcome[1]), c.date, game_now(self),
                                 game_time=facts.time_of_day if result_location == location else None,
                                 observed_location=result_location,
                                 arrival_verified=not held and facts is not None and S._g(cleanup,"input_released") is True)):
            self._journal(dict(ev='travel_elapsed', origin=before.get('location'), destination=run.call.arg,
                               observed_location=result_location, **trip))
            from arti_runtime.travel_time import export
            self._journal(dict(ev='live7_travel_memory', trips=export(p)))
        if run.call.name == "lawan":
            # Owner 2026-10-23: every reflex / lawan encounter is journaled (monster, distance, swings, outcome).
            cf = (context or {}).get("combat") or {}
            if category == CLEAN_FAIL and outcome[1] in LAWAN_HOLD_REASONS:
                p.lawan_hold = dict(damaged_at=p.damaged_at, ids=[m.get("id") for m in S.lawan_near(cf)])
            self._journal(dict(ev="live7_lawan_result", by=run.state.get("live7_lawan_by"), outcome_class=category,
                               reason=outcome[1], monsters=run.state.get("live7_lawan_monsters"),
                               swings=run.state.get("lawan_swings", 0), calls=run.state.get("lawan_calls", 0),
                               kills=run.state.get("wave1_kills", 0), legs=run.state.get("lawan_legs", 0),
                               last_verdict=run.state.get("lawan_last_verdict"),
                               weapon_used=cf.get("weapon") if cf.get("weapon_held") is True else None,
                               hp_start=run.state.get("live7_lawan_hp"), hp_end=cf.get("hp"),
                               monsters_after=[[m.get("type"), m.get("distance")] for m in
                                               (cf.get("monsters") or ())[:3] if isinstance(m, Mapping)]))
        if not unknown:  # ADR 157: only the settled result enters lived activity memory.
            self._live7_moments("row", session, motor, row=row, before=before, after=after, context=context)
        if loop:
            session.blocked_refs |= p.refs
            self._journal(dict(ev="live7_loop", skill=run.call.name, arg=run.call.arg, target=target,
                               location=location, outcome_class=category,
                               count=min(p.travel_edges.values(), default=0) if p.travel_loop else
                                     1 if outcome[1] == "no_effect_progress" else 3,
                               reason="unproductive_travel" if p.travel_loop else
                                      "no_effect_progress" if outcome[1] == "no_effect_progress" else "repeated_failure",
                               action="different_approach_or_task"))
            self._live7_moments("loop", session, motor)
        floor_retry = (run.call == S.SkillCall("pergi_ke", "mine") and category in (CLEAN_FAIL, UNCERTAIN, NOT_READY)
                       and str(location).startswith("UndergroundMine") and p.blocked(run.call, location)
                       and location not in p.floor_retry)
        if floor_retry:
            # F7: a quarantined floor exit must not strand ARTI. One bounded
            # recovery per floor: settle, fresh native read, new goal (the
            # existing reset clears the old hold), then the native exit again
            # from that fresh pose. A new attempt, never this run's replay.
            p.floor_retry.add(location)
            p.quarantine.discard((*key(run.call), location))
            self._journal(dict(ev="live7_floor_exit_recovery", location=location, outcome_class=category,
                               replay=False, action="fresh_goal_then_native_exit"))
        if category == SAFETY_STOP:
            run.pending = ("fail", "live7:safety_stop:" + outcome[1])
        elif category in (UNCERTAIN, NOT_READY) or floor_retry:
            # Never resume this run. The next boundary resets only after a fresh
            # released/identity-fenced read and retains the campaign/quarantine.
            run.state["campaign_deferred"] = True
            p.wait_since = p.wait_since or game_now(self)
            self._live7_recovery_fault(session, outcome[1], observed=cleanup)
        elif outcome[0] != "done":
            run.state["campaign_deferred"] = True
        return False

    def _live7_crop_reconcile(self, session, after, *, read):
        """ADR 76: one fresh read (crops + bag) may prove an uncertain planting hers; journal the result."""
        c = session.campaign
        memory = c.live.crop_memory
        before = {ref: dict(r) for ref, r in memory.crops.items()}
        enrolled = memory.reconcile_uncertain(c.date, after.get("crops") or {}, after.get("inventory"),
                                              now=after.get("time_of_day"))
        if enrolled:
            self._journal(dict(ev="live7_crop_reconciled", session_id=session.session_id, read=read, replay=False,
                               game=dict(date=list(c.date)), refs=sorted(enrolled),
                               crops={ref: dict(memory.crops[ref]) for ref in sorted(enrolled)}))
        if read != "terminal" and before != memory.crops:
            self._journal(dict(ev="live7_crop_memory", at=time.time(), game=dict(date=list(c.date)),
                               crops={ref: dict(r) for ref, r in memory.crops.items()}))
        return enrolled

    @staticmethod
    def _live7_changed_items(before, after):
        old, new = before.get("inventory", {}), (after or {}).get("inventory", {})
        return {q for q in set(old) | set(new) if old.get(q) != new.get(q)}

    @staticmethod
    def _live7_call_items(call):
        if call.name in ("simpan_ke_chest", "jual", "kirim"):
            return {q for q in (S.item_qid(call.arg),) if q}
        return set()

    def _live7_morning_pending(self):
        from arti_stardew_day_start import native_morning_pending
        _, pid = self._binding_view()
        ui = self._obs.get(pid, "ui.context") if pid is not None else None
        motor = self._state.latest(pid) if pid is not None else None
        return native_morning_pending(getattr(ui, "facts", None), self._date_tuple(motor) if motor else None)

    def _live7_native_event(self):
        from arti_stardew_day_start import native_event_state
        _, pid = self._binding_view()
        ui = self._obs.get(pid, "ui.context") if pid is not None else None
        if (ui is None or not getattr(ui, "authoritative", False) or getattr(ui, "stale", True)
                or getattr(ui, "requires_resync", True) or not getattr(ui, "is_complete", False)
                or not 0 <= time.monotonic() - ui.received_monotonic <= 2):
            return None
        event = native_event_state(ui.facts)
        motor = self._state.latest(pid)
        if event and "date" in event and (motor is None or event["date"] != list(self._date_tuple(motor) or ())):
            return None
        return event

    def _live7_sleep_honest(self, c, observed, motor):
        from arti_stardew_part_a import next_date
        from arti_runtime.sleep_truth import bed_evidence
        return (motor is not None and bed_evidence(observed)
                and tuple(observed.get("start_date", ())) == c.date
                and self._date_tuple(motor) == next_date(c.date) and not self._day_start_active
                and not self._live7_morning_pending()
                and type(motor.time_of_day) is int and 600 <= motor.time_of_day < 700)

    def _live7_sleep_recheck(self, session, motor):
        """F6: re-judge a done bed on the first fresh read; a held read never credits it."""
        c = session.campaign
        if not c.live.pending_sleep and not c.finished:
            from arti_runtime.sleep_truth import bed_evidence, last_bed_observed
            observed = last_bed_observed(c)
            if bed_evidence(observed) and tuple(observed.get("start_date") or ()) == c.date:
                row = next((r for r in reversed((c.days[-1] if c.days else {}).get("skills") or ())
                            if r.get("skill") == "tidur"), dict(task="sleep", skill="tidur", verified=False))
                c.live.pending_sleep = dict(row=row, observed=dict(observed),
                                            reason="observed_day_change")
        if c.live.pending_sleep and (self._live7_morning_pending() or self._day_start_active):
            return False  # keep the ticket; generic UI choices and native watchdog still own the night
        pending, c.live.pending_sleep = c.live.pending_sleep, None
        if not pending:
            return False
        row = pending["row"]
        honest = self._live7_sleep_honest(c, pending["observed"], motor)
        if not honest:
            from arti_stardew_part_a import next_date
            date, clock = self._date_tuple(motor), getattr(motor, "time_of_day", None)
            ready = date == next_date(c.date) and type(clock) is int and 600 <= clock < 700
            # The night is over and the first fresh read came after 07:00: judge now. A ticket that waited
            # for a 06:xx read which can no longer come would hold the day open for good.
            late = date != c.date and type(clock) is int and 700 <= clock < 2600
            if not ready and not late:
                c.live.pending_sleep = pending
                return False
            from arti_runtime.sleep_truth import bed_evidence
            observed = pending["observed"]
            if (late and date == next_date(c.date) and bed_evidence(observed)
                    and tuple(observed.get("start_date") or ()) == c.date
                    and not self._day_start_active and not self._live7_morning_pending()):
                honest = True  # the native bed Yes and the one-day change stand, whatever the clock of the read
        if honest:
            row.update(outcome_class=VERIFIED, verified=True, **({"reason": pending["reason"]} if pending.get("reason") else {}))
            c.days[-1]["rows"][row["task"]] = True
            c.days[-1]["transition"] = list(self._date_tuple(motor))
            c.finished = True
        else:
            row.update(outcome_class=UNCERTAIN, verified=False, reason="sleep_transition_unverified")
        if row.get("outcome") == "unknown" and honest:
            from arti_runtime.action_outcomes import settle
            for check in list(c.live.pending_reconcile):
                if check["row"] is row:
                    proof = dict(pending["observed"], reason=row["reason"])
                    settle(check, proof, motor.location)
                    row["evidence"]["reconcile"].update(read="fresh_after_wake", credit=True, settlement=proof)
                    c.live.pending_reconcile.remove(check)
                    break
        self._journal(dict(ev="part_live_row_rechecked", session_id=session.session_id,
                           **row, read="fresh_after_wake"))
        if honest:
            if (getattr(self, "_config", None) or {}).get("voice_game_memory_enabled") is True:
                from arti_runtime import day_record
                day_record.skill(self, row, c.date, getattr(motor, "time_of_day", None), motor.location)
            self._live7_moments("row", session, motor, row=dict(row, evidence=dict(observed=pending["observed"])),
                               before={}, after={}, context={})
        return honest

    def _live7_reconcile(self, session, facts, motor, context):
        """F2: every pending UNCERTAIN delta is recomputed from the fresh post-release read."""
        p = session.campaign.live
        after = self._live7_baseline(facts, motor, context)
        if p.crop_memory.uncertain:
            self._live7_crop_reconcile(session, after, read="post_release")
        for pending in p.pending_reconcile:
            reconcile = pending["row"]["evidence"]["reconcile"]
            if pending["row"].get("outcome") == "unknown":
                self._live7_outcome_recheck(session, pending, facts, motor, context, after)
                continue  # no unchanged-bag relabel can turn a missing read into failure
            check = pending.get("store_check")
            if check is not None and not self._live7_changed_items(pending["before"], after):
                self._live7_store_relabel(session, pending, check, after)
                continue
            items = self._live7_changed_items(pending["before"], after) | self._live7_call_items(pending["call"])
            items |= set(reconcile.get("items") or ())  # a non-held terminal read is evidence too
            p.uncertain_items |= items
            reconcile.update(after=after, items=sorted(items), read="post_release")
            self._journal(dict(ev="live7_reconcile", session_id=session.session_id, skill=pending["call"].name,
                               arg=pending["call"].arg, items=sorted(items), replay=False, credit=False))
        p.pending_reconcile = []

    def _live7_outcome_recheck(self, session, pending, facts, motor, context, after):
        """ADR 157: one fresh recovery observation, no action dispatch or replay."""
        from arti_runtime.action_outcomes import generic_receipt, settle
        row, run, action = pending["row"], pending["run"], pending["action"]
        proof = None
        goal = self._active
        if goal is not None and goal.goal_id == run.goal_id:
            proof = generic_receipt(run, action, facts.location)
            if proof is None:
                try:
                    snapshot = self._build_decision_snapshot(goal)
                    if (getattr(goal, "progress_marker", None) == "terminal"
                            and S._g(getattr(goal, "last_finish", None), "status") == "achieved"
                            and not run.state.get("route_clear")):
                        # Reuse the whole skill's rule on scratch state only.
                        # A returned next-leg never runs or credits the parent.
                        from copy import copy, deepcopy
                        probe = copy(run)
                        probe.state, probe.pending, probe.status = deepcopy(run.state), None, "running"
                        result = self._skill_required_run_outcome(session, probe)
                        if result and result[0] == "done":
                            proof = dict(S._g(goal.last_finish, "observed", {}) or {}, reason=result[1])
                    elif (not run.state.get("route_clear")
                            and S.REGISTRY[run.call.name].family != S.F_WAVE1
                            and self._skill_finish_ready(goal, SimpleNamespace(snapshot=snapshot))):
                        proof = dict(reason="observed_native_objective", effect="native_objective_achieved",
                                     location=facts.location)
                except (AttributeError, TypeError, ValueError) as exc:
                    row["evidence"]["recheck_unavailable"] = type(exc).__name__
        if proof and run.call.name == "tidur" and not self._live7_sleep_honest(session.campaign, proof, motor):
            proof = None  # the existing next-morning proof remains authoritative
        if proof:
            settle(pending, proof, facts.location)
            row["evidence"].update(observed=proof, original_cleanup=row["evidence"]["cleanup"],
                                   cleanup=self.skill_post_row_state())
            from arti_runtime.place_names import source_place
            row["evidence"]["place_name"] = source_place(facts.location, context.get("world_knowledge"))
            session.campaign.days[-1]["rows"][row["task"]] = True
            p = session.campaign.live
            refs, quarantine, items, blocked = pending["added_holds"]
            p.refs -= refs
            session.blocked_refs -= blocked
            p.quarantine -= quarantine
            p.uncertain_items -= items
            if p.last_failed == key(run.call):
                p.last_failed = None
            self._live7_moments("row", session, motor, row=row, before=pending["before"],
                               after=after, context=context)
        else:
            # Preserve recovery's item safety even when the effect cannot be credited.
            items = (self._live7_changed_items(pending["before"], after)
                     | self._live7_call_items(run.call) | set(row["evidence"]["reconcile"].get("items") or ()))
            session.campaign.live.uncertain_items |= items
            row["evidence"]["reconcile"]["items"] = sorted(items)
        row["evidence"]["reconcile"].update(after=after, read="post_release", replay=False,
                                             credit=bool(proof), settlement=proof or "still_unknown")
        self._journal(dict(ev="part_live_row_rechecked", session_id=session.session_id,
                           **row, read="post_release", settlement=proof or "still_unknown", replay=False))

    @staticmethod
    def _live7_store_miss_reason(reason, action):
        last = S._g(action, "reason", None)
        return "chest_not_reached:" + str(reason) + (":" + str(last) if last and last != reason else "")

    def _live7_store_relabel(self, session, pending, check, after):
        """Review 2026-10-23: an unsent store row whose fresh post-release bag equals its baseline stored nothing. It
        becomes the reported CLEAN_FAIL chest_not_reached; only what THIS row added to the day quarantine is undone
        (an earlier safety quarantine of the same chest or item survives)."""
        p = session.campaign.live
        row = pending["row"]
        reason = self._live7_store_miss_reason(check["reason"], dict(reason=check["last_action"]))
        row.update(outcome_class=CLEAN_FAIL, verified=False, reason=reason)
        row["evidence"]["reconcile"].update(after=after, items=[], read="post_release", relabel="unsent_bag_unchanged")
        chest = check["chest"]
        if chest in check["added_refs"]:
            p.refs.discard(chest)
        if chest not in check["blocked"]:
            session.blocked_refs.discard(chest)
        p.quarantine -= check["added_quarantine"]
        p.uncertain_items -= check["added_items"]
        if check.get("history") is not None:
            check["history"].update(outcome_class=CLEAN_FAIL, reason=reason, note=STORE_MISS_NOTE)
        self._journal(dict(ev="live7_store_miss_reported", skill=row["skill"], arg=row["arg"], was=UNCERTAIN,
                           reason=reason, read="post_release"))
        self._journal(dict(ev="part_live_row_rechecked", session_id=session.session_id, skill=row["skill"],
                           outcome_class=CLEAN_FAIL, verified=False, read="post_release", reason=reason))

    # ------------------------------------------------------------------ STOP hooks (WP3 calls these)
    def live7_monitor_violation(self, violations, *, source="pad_watch"):
        """ADR 192: a held-pad incident releases/verifies first; other protections still stop.

        Accept a watch row, violation names, dictionaries, or exit codes. Returns
        True when handled (including recovery), False for an empty/inactive hook.
        """
        if isinstance(violations, Mapping):
            violations = violations.get("violations") or ()
        if isinstance(violations, (str, int)):
            violations = (violations,)
        items = list(violations or ())
        codes = [str(PAD_WATCH_CODES.get(v.get("kind") if isinstance(v, Mapping) else v,
                                       v.get("kind") if isinstance(v, Mapping) else v)) for v in items]
        if not codes:
            return False
        if codes == ["6"] and self._live7_active():
            from arti_runtime import pad_recovery
            file = pad_recovery.path(self._config)
            if file is not None:
                with self._lock:
                    self._cancel_action("pad_watch_recovery")
                src = pad_recovery.RuntimeSource(self)
                value = items[0] if isinstance(items[0], Mapping) else dict(outstanding=1, seconds=None)
                pad_recovery.recover(file, src, src.session(), value)
                with self._lock:
                    pad_recovery.boundary(self)
                return True
        with self._lock:
            session = getattr(self, "_skill_session", None)
            if not self._live7_active():
                return False
            self._journal(dict(ev="live7_monitor_violation", source=source, codes=codes, action="stop"))
            reason = ("pad_watch_neutral_unverified:unknown(no_recovery_scope)" if codes == ["6"]
                      else "monitor_violation:" + ",".join(codes))
            self._skill_end_locked(session, "held", SAFETY_STOP, reason)
            self._worker_wakeup.notify_all()
        return True

    def live7_stop_file_check(self):
        """STOP file (PLAN 2.3 RUN/LIVE7_STOP): config ``stardew_live7_stop_file`` names it; the
        supervisor creates it. Checked at every skill boundary; returns True when it ended a session."""
        from arti_stardew_stop import pending
        path = self._config.get("stardew_live7_stop_file")
        current = dict(self._config.get("stardew_live7_scope") or {})
        current["cycle_id"] = self._config.get("stardew_live7_cycle_id")
        if not isinstance(path, str) or not path or not self._live7_active() or not pending(path, current):
            if self._live7_active():
                from arti_runtime.pad_recovery import boundary
                with self._lock:
                    return boundary(self)
            return False
        with self._lock:
            session = getattr(self, "_skill_session", None)
            if not self._live7_active():
                return False
            from arti_runtime import pad_recovery
            if (pad_recovery.read(pad_recovery.path(self._config)) or {}).get("status") == "failed":
                return pad_recovery.boundary(self)
            self._journal(dict(ev="live7_stop_file", action="stop"))
            self._skill_end_locked(session, "held", SAFETY_STOP, "stop:live7_stop_file")
            self._worker_wakeup.notify_all()
        return True

    def _live7_knockout_cleanup(self, session, run, state):
        """M12 (skills_runtime terminal cleanup hook): True when the row ended in the clinic wake-up of a knockout.

        Only on a live7 session, with inputs released, READY, a DialogueBox up in the Hospital, and the row started
        elsewhere (its own baseline). Records ``live.knockout`` for the recovery poll and journals the facts.
        """
        from arti_stardew_runtime import READY
        c = getattr(session, "campaign", None)
        p = getattr(c, "live", None)
        before = (run.state.get("live7_baseline") or {}) if run is not None else {}
        if (p is None or not run.state.get("live7") or self._status != READY or self._worker_stop.is_set()
                or getattr(self, "_owner_play_stopped", False) or not isinstance(state, Mapping)
                or state.get("input_released") is not True or state.get("menu") != "DialogueBox"
                or state.get("location") != KNOCKOUT_LOCATION or before.get("location") in (None, KNOCKOUT_LOCATION)):
            return False
        motor = self._state.latest(self._binding_view()[1])
        p.knockout = dict(at=game_now(self), clock=getattr(motor, "time_of_day", None),
                          from_location=before.get("location"), before=dict(money=before.get("money"),
                          inventory=dict(before.get("inventory") or {})), presses=0, skill=run.call.name)
        self._journal(dict(ev="live7_knockout", skill=run.call.name, arg=run.call.arg, from_location=before.get("location"),
                           time_of_day=p.knockout["clock"], hp=getattr(motor, "health", None),
                           action="advance_wake_up_event_then_continue"))
        return True

    def _live7_knockout_event(self, session):
        """M12: advance the clinic wake-up event with the existing G3 event plan (text_event OBJ_EVENT: plain lines
        only, never a question), bounded KNOCKOUT_PRESSES / KNOCKOUT_SECONDS. True = handled (pressed or waiting)."""
        import arti_stardew_text_event as text_event
        p = session.campaign.live
        k = p.knockout
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        if (k is None or pid is None or motor is None or self._state.is_stale(pid)
                or not k.get("native_event") and getattr(motor, "location", None) != KNOCKOUT_LOCATION
                or getattr(motor, "active_menu", None) != "DialogueBox"):
            return False
        if k["presses"] >= KNOCKOUT_PRESSES or game_now(self) - k["at"] > KNOCKOUT_SECONDS:
            if not k.get("capped"):
                k["capped"] = True
                self._journal(dict(ev="live7_knockout_event", outcome="cap", presses=k["presses"]))
            return False
        rec = self._obs.get(pid, "ui.context")
        view = self._ui_pad_view(pid)
        step = text_event.plan_step(text_event.TextEventObjective(text_event.OBJ_EVENT), view,
                                    getattr(rec, "facts", None) if rec is not None else None)
        if step.kind == "wait":
            return True  # typing / not ready: observed, bounded by KNOCKOUT_SECONDS
        if step.kind != "act" or step.reason != "event_advance":
            self._journal(dict(ev="live7_knockout_event", outcome="refused", step=step.kind, reason=step.reason))
            return False  # a question remains the chooser's decision
        with self._lock:
            if (self._action_busy or self._worker_stop.is_set() or getattr(self, "_owner_play_stopped", False)
                    or self._action_cancel.is_set() or not self._live7_active()):
                return True
            self._action_busy = True
        k["presses"] += 1
        try:
            result, _ = self._run_ui_pad("ui_press", step.args, origin="live7_knockout_event")
        finally:
            with self._lock:
                self._action_busy = False
        ok = bool(result.cleanup and result.verified)
        self._journal(dict(ev="live7_knockout_event", outcome="pressed" if ok else "press_unverified",
                           effect=getattr(result, "effect", None), presses=k["presses"]))
        return ok

    def _live7_plain_dialogue(self, session):
        """Sat T5 (2026-10-23): a plain DialogueBox (text, no choice) left up after an interrupted action.

        Live7 smoke: a box opened mid pad walk; the mod now releases the walk (UNCERTAIN) instead of a T5
        FailFast, and recovery would otherwise wait for ``menu is None`` and SAFETY_STOP the cycle. FIX4's
        bounded press of the offered A only: <= FOUND_BOX_PRESSES press ATTEMPTS per receiver and
        FOUND_RUN_PRESSES attempts per session (an attempt counts whether or not it was verified), FOUND_BOX_S
        per box; a typing box is waited for, never forced. Never while an event is up (cutscene / festival
        lines run their own course); questions / choices / other menus are never adopted. True = handled
        (typing wait or a verified press); False = reobserve, then the generic progress deadline.
        """
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        if (pid is None or motor is None or self._state.is_stale(pid) or self._day_start_active
                or getattr(motor, "active_menu", None) != "DialogueBox"):
            return False
        now = time.monotonic()
        rec = self._obs.get(pid, "ui.context")
        fresh = (rec is not None and getattr(rec, "authoritative", False) and not getattr(rec, "stale", True)
                 and not getattr(rec, "requires_resync", True)
                 and 0 <= now - getattr(rec, "received_monotonic", -1) <= 2)
        if not fresh or S.event_up(rec.facts):
            return False
        receiver = S.plain_dialogue_receiver(rec.facts, "DialogueBox")
        if receiver is None:
            return False
        ledger = self.__dict__.setdefault("_live7_dialogue_attempts", {}).setdefault(session.session_id, {})
        box = ledger.setdefault(receiver, dict(at=game_now(self), attempts=0))
        total = sum(b["attempts"] for b in ledger.values())
        text = rec.facts["interactive"]["visible_text"][:DIALOGUE_TEXT_MAX]
        if (game_now(self) - box["at"] > S.FOUND_BOX_S or box["attempts"] >= S.FOUND_BOX_PRESSES
                or total >= S.FOUND_RUN_PRESSES):
            if not box.get("capped"):
                box["capped"] = True
                self._journal(dict(ev="live7_dialogue_dismiss", receiver=receiver, outcome="cap", text=text,
                                   attempts=box["attempts"], session_attempts=total))
            return False
        view = self._ui_pad_view(pid)
        if (view is None or not view.available or view.receiver_token != receiver or view.context != "dialogue"
                or view.held_item is not None or receiver in self._ui_pad_uncertain or not view.press_allowed("A")):
            return True  # typing: the native refusal is observed, never bypassed (bounded by FOUND_BOX_S)
        with self._lock:
            # Check-and-set: no concurrent action, and the owner STOP / worker stop re-read right before the press.
            if (self._action_busy or self._inflight_generation is not None or self._worker_stop.is_set()
                    or self._action_cancel.is_set() or getattr(self, "_owner_play_stopped", False)
                    or not self._live7_active()):
                return False
            self._action_busy = True
        box["attempts"] += 1
        try:
            result, _ = self._run_ui_pad("ui_press", view.press_args("A"), origin="live7_recovery_dialogue")
        finally:
            with self._lock:
                self._action_busy = False
        ok = bool(result.cleanup and result.verified)
        if not ok:  # Sat gift: an unverified recovery press never earns the longer native-question wait
            self.__dict__.setdefault("_live7_unverified_press", set()).add(session.session_id)
        self._journal(dict(ev="live7_dialogue_dismiss", receiver=receiver, outcome="pressed" if ok else "press_unverified",
                           effect=getattr(result, "effect", None), reason=getattr(result, "reason", None), text=text,
                           attempts=box["attempts"], session_attempts=total + 1))
        return ok

    def _live7_gift_request_current(self, request):
        """M15: permit only the current gift-choice request in released recovery, without rearming world play."""
        offer = getattr(self, "_live7_gift_offer", None)
        session = getattr(self, "_skill_session", None)
        if (self._status != "PAUSED_RECOVERY" or not self._live7_active() or not offer
                or self._worker_stop.is_set() or self._action_cancel.is_set()
                or getattr(self, "_owner_play_stopped", False) or self._action_busy
                or request.superseded.is_set() or request.generation_id != self._inflight_generation
                or self._active is None or request.goal_id != self._active.goal_id
                or request.goal_revision != self._active.revision
                or {s["name"] for s in (request.skill_offer or {}).get("skills", ())} != set(offer.get("choices", GIFT_SKILLS))):
            return False
        _, _, _, held = self._live7_read(session)
        state = self.skill_post_row_state()
        key = self._live7_waiting_gift(session, held, state)
        return bool(key is not None and key.available and key.receiver_token == offer["receiver"]
                    and key.gift == offer["gift"])

    @W.span('settle_readback', 5, runtime=True)
    def _live7_waiting_gift(self, session, held, state):
        """A fresh pinned gift with confirmed released input is a benign wait, even after its grace period."""
        from arti_stardew_ui import GIFT_QUESTION_KEY
        if held or state.get("input_released") is not True or state.get("menu") != "DialogueBox":
            return None
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        rec = self._obs.get(pid, "ui.context") if pid is not None else None
        if (motor is None or self._state.is_stale(pid) or getattr(motor, "active_menu", None) != "DialogueBox"
                or rec is None or not getattr(rec, "authoritative", False) or getattr(rec, "stale", True)
                or getattr(rec, "requires_resync", True)
                or not 0 <= time.monotonic() - rec.received_monotonic <= 2 or S.event_up(rec.facts)):
            return None
        key = self._ui_key_view(pid)
        return key if (key is not None and key.kind == "question" and key.question_key == GIFT_QUESTION_KEY
                       and key.gift is not None) else None

    def _live7_gift_question(self, session):
        """Sat gift: the native gift prompt gets bounded chooser retries, terima / tolak, facts
        only; the answer goes through the existing ui_key answer path and is verified (box closed; terima: the bag
        gained the stack). "verified" / "unverified" = her answer was pressed (the settle restarts: the ordinary re-read, or the
        QUESTION_SETTLE_SECONDS grace if the box stays). False = not the gift, already answered or no pick;
        "unsafe" = uncertain publication/cleanup. Never any other question."""
        from arti_stardew_interact import inventory_count
        from arti_stardew_ui import GIFT_QUESTION_KEY
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        if (pid is None or motor is None or self._state.is_stale(pid) or self._day_start_active
                or getattr(motor, "active_menu", None) != "DialogueBox"):
            return False
        rec = self._obs.get(pid, "ui.context")
        fresh = (rec is not None and getattr(rec, "authoritative", False) and not getattr(rec, "stale", True)
                 and not getattr(rec, "requires_resync", True)
                 and 0 <= time.monotonic() - getattr(rec, "received_monotonic", -1) <= 2)
        if not fresh or S.event_up(rec.facts):
            return False
        key = self._ui_key_view(pid)
        if (key is None or not key.available or key.kind != "question" or key.question_key != GIFT_QUESTION_KEY
                or key.gift is None):
            return False
        receiver, gift = key.receiver_token, key.gift
        _, _, _, held = self._live7_read(session)
        if self._live7_waiting_gift(session, held, self.skill_post_row_state()) is None:
            return False  # wait for the complete identity/readback before spending the one decision
        seen = self.__dict__.setdefault("_live7_gift_answered", set())
        if (session.session_id, receiver) in seen:
            return False  # at most one answer publication per box
        # PM-1 fix8: the answers are the native-published ones (its arm admission), not a fixed Python pair.
        pad = self._ui_pad_view(pid)
        published = {e.get("response_key") for e in (getattr(pad, "affordances", None) or {}).get("list", ())
                     if e.get("role") in ("answer", "decline")} if getattr(pad, "receiver_token", None) == receiver else set()
        choices = tuple(n for n, k in zip(GIFT_SKILLS, ("Yes", "No"))
                        if getattr(pad, "affordances", None) is None or k in published)
        if not choices:
            return False  # native names none: <reason>; nothing to answer on this receiver yet
        retry = self._live7_choice_retry_budget(session, (receiver, json.dumps(gift, sort_keys=True)))
        if self._live7_provider_lane_busy():
            # fix56B (a41 D1:J451-J488, ADR 82): a logically timed-out call still ran physically, so three asks in 24 s
            # were refused unsent (provider_physical_inflight), spent every re-offer and the recovery declined for her.
            # A refusal that never reached her is no chooser attempt: wait for the lane (the 600 s deadline still runs).
            self._live7_lane_wait_note(session, receiver, "live7_gift_question")
            return False
        # ADR 128 (ADR 105 for this path): after the bounded re-offers the box stays hers. A chooser that keeps failing,
        # as in a machine stall (a54 d23 J665), never becomes a No pressed for her. The asks start again after the
        # longest backoff; the global 600 s progress deadline still runs.
        if retry.exhausted(game_now(self)):
            self._journal(dict(ev="live7_recovery_no_exit", receiver=receiver, context="question", menu="DialogueBox",
                               input=False, chooser_attempts=retry.attempts, trigger="live7_gift_question"))
            retry.restart(game_now(self))
            return False
        if not retry.ready(game_now(self)):
            return False  # backoff: global 600s progress deadline still runs
        bag = self._obs.get(pid, "progress.inventory")
        before = inventory_count(bag, gift["item_qid"]) if bag is not None else None
        name = gift["item_name"] or gift["item_qid"]
        retry.start()
        facts = dict(fakta="%s ngasih kamu %s (x%d)" % (gift["sender_name"] or "Seseorang", name, gift["stack"]),
                     tas_muat=gift["bag_has_room"], pilihan=list(choices))
        self._live7_gift_offer = dict(receiver=receiver, gift=dict(gift), facts=facts, choices=choices)
        try:
            pick = self._skill_model_choose(session, "live7_gift_question")
        finally:
            self._live7_gift_offer = None
        row = dict(ev="live7_gift_answer", receiver=receiver, item_qid=gift["item_qid"], stack=gift["stack"],
                   pick=getattr(pick, "name", pick if isinstance(pick, str) else None))
        if pick == "provider_error" and self._live7_provider_lane_busy():
            retry.refund(game_now(self))  # fix56B: refused unsent (lane raced busy), not her miss
            self._journal(dict(row, outcome="provider_lane_busy", chooser_attempt=retry.attempts))
            return False
        if not isinstance(pick, S.SkillCall) or pick.name not in choices:
            retry.failed(game_now(self))
            self._journal(dict(row, outcome="no_pick", chooser_attempt=retry.attempts,
                               retry_at=retry.next_at, retry_exhausted=not retry.ready(retry.next_at)))
            self._live7_recovery_fault(session, "question_chooser_no_pick",
                refs=dict(receiver=receiver, chooser_attempt=retry.attempts, trigger="live7_gift_question"))
            return False
        want = "Yes" if pick.name == "terima_hadiah" else "No"
        key = self._ui_key_view(pid)
        options = [o for o in (key.options if key is not None else ()) if o.get("response_key") == want]
        _, _, _, held = self._live7_read(session)
        state = self.skill_post_row_state()
        if (key is None or not key.available or key.receiver_token != receiver or key.question_key != GIFT_QUESTION_KEY
                or key.gift != gift or len(options) != 1 or self._live7_waiting_gift(session, held, state) is None):
            retry.failed(game_now(self))
            self._journal(dict(row, outcome="prompt_changed"))
            return False
        with self._lock:
            if (self._action_busy or self._inflight_generation is not None or self._worker_stop.is_set()
                    or self._action_cancel.is_set() or getattr(self, "_owner_play_stopped", False)
                    or self._status not in ("READY", "PAUSED_RECOVERY") or not self._live7_active()):
                self._journal(dict(row, outcome="stopped_or_busy"))
                return False
            seen.add((session.session_id, receiver))  # provider errors never consume this one-input fence
            self._action_busy = True
        try:
            result = self._run_ui_key(key.args_for("answer", option_index=options[0]["index"]), origin="live7_gift_answer")
        finally:
            with self._lock:
                self._action_busy = False
        closed = gained = False
        deadline = game_now(self) + GIFT_VERIFY_SECONDS
        while bool(result.cleanup and result.verified) and game_now(self) < deadline:
            wait_resume(self)
            now_key = self._ui_key_view(pid)
            closed = now_key is None or now_key.receiver_token != receiver
            bag = self._obs.get(pid, "progress.inventory")
            count = inventory_count(bag, gift["item_qid"]) if bag is not None else None
            gained = type(before) is int and type(count) is int and count >= before + gift["stack"]
            if closed and (want == "No" or gained):
                break
            W.call('poll', 7, time.sleep, 0.1)
        ok = bool(result.cleanup and result.verified) and closed and (want == "No" or gained)
        self._journal(dict(row, outcome="verified" if ok else "unverified", answer=want, closed=closed,
                           gained=gained if want == "Yes" else None, reason=getattr(result, "reason", None)))
        if getattr(result, "hard_uncertain", False):
            return "unsafe"  # recovery retains the one-publication and native uncertainty fences
        if ok and want == "Yes":
            sender = gift["sender_name"] or None
            if sender:
                session.campaign.live.gift_from[gift["item_qid"]] = sender[:64]
            self._journal(dict(ev="live7_gift_received", item_qid=gift["item_qid"], item_name=name,
                               stack=gift["stack"], sender=sender))
        if (ok and session.current is not None and session.current.call.name == "cek_pagi"
                and session.current.call.arg in (None, "mail")):
            session.current.state["mail_gift_interrupted"] = True
        return "verified" if ok else "unverified"

    def _live7_resume_mail(self, session, run, facts, motor, context, state):
        """ADR 70: continue an admitted mail visit after released native UI.

        Fresh goals acquire fresh offers; no input is replayed and the original
        run retains its decisions, inputs, deadline and unread-count baseline.
        """
        from arti_stardew_ui_effects import same_date
        from arti_stardew_skills_ui import MAIL_UI_RESUME_MAX
        if (run.call.name != "cek_pagi" or run.call.arg not in (None, "mail")
                or run.call.arg is None and "mail_unread_before" not in run.state
                    and run.state.get("wave1_driver") != "morning_farm"
                or not (run.state.get("ui_pause") or run.state.get("mail_gift_interrupted")
                        or session.session_id in self.__dict__.get("_live7_ui_paused", ()))
                or run.pending is not None and run.pending != ("fail", "mail_letter_held:attachment")
                or state.get("input_released") is not True or state.get("menu") is not None
                or state.get("free") is not True or state.get("food_clear") is False
                or self._worker_stop.is_set() or self._action_cancel.is_set()
                or getattr(self, "_owner_play_stopped", False)
                or getattr(self, "_gather_mutation_unresolved", False)):
            return False
        identity = tuple((context or {}).get("identity") or ())[:5]
        if (len(identity) != 5 or identity != run.state.get("morning_identity")
                or not same_date(run.state.get("morning_date"), self._date_tuple(motor))):
            return False
        action = getattr(self._active, "last_action", None)
        # a39 D01:J314: a cleaned PressCompleted walk was interrupted by a HOST
        # gift, leaving no endpoint. Only this non-mutating travel leg may be
        # discarded after native control returns; mailbox writes stay fenced.
        travel_interrupt = (run.state.get("wave1_driver") == "morning_farm"
            and run.state.get("mail_gift_interrupted")
            and S._g(action, "outcome") == "PressCompleted"
            and S._g(action, "lifecycle") == "completed")
        if (action is not None and (S._g(action, "cleanup_confirmed") is not True
                or S._g(action, "lifecycle") not in CLEAN_LIFECYCLES
                or S._g(action, "world_effect_status") in ("possible_mutation", "unreconciled")
                or S._g(action, "world_effect_status") == "uncertain" and not travel_interrupt)):
            return False
        if run.state.get("mail_ui_resumes", 0) >= MAIL_UI_RESUME_MAX:
            run.pending = ("fail", "mail_ui_resume_bound")
            return False
        run.state["mail_ui_resumes"] = run.state.get("mail_ui_resumes", 0) + 1
        result = self._skill_session_goal(session)
        if not result.admitted:
            run.pending = ("fail", "mail_resume_refused:" + str(result.reason))
            return False
        run.state.pop("ui_pause", None)
        run.state.pop("mail_gift_interrupted", None)
        run.state.pop("ui_identity", None)
        run.state.pop("morning_letter_held", None)
        if "plan_input" in run.state:
            run.state["plan_input"].pop("held_letter", None)
        session.campaign.live.wait_since = session.campaign.live.release_after = None
        self.__dict__.setdefault("_live7_ui_paused", set()).discard(session.session_id)
        result = self._skill_start(session, run.call, by=run.by, facts=facts, resume=run)
        self._journal(dict(ev="mail_visit_resumed", admitted=result.admitted,
                           resumes=run.state["mail_ui_resumes"], replay=False,
                           unread=(context or {}).get("mailbox_count")))
        if not result.admitted:
            run.pending = ("fail", "mail_resume_refused:" + str(result.reason))
        return bool(result.admitted)

    def _live7_resume_world(self, session, run, action, facts, state) -> bool:
        """ADR 195: a walk or talk that opened a menu mid-move resumes after the existing generic UI path.

        The menu must already be gone (her answer or a close). A still-open box stays on the generic UI path.
        """
        from arti_stardew_runtime import READY
        why = str(S._g(action, "reason") or "")
        pending = run.pending[1] if run.pending else ""
        if "MENU_OPENED_MID_MOVE" not in why and "MENU_OPENED_MID_MOVE" not in str(pending):
            return False
        if run.call.name not in ("ngobrol_hadiah", "pergi_ke"):
            return False
        if self._status != READY:
            return False
        if S._g(action, "cleanup_confirmed") is not True:
            return False
        if state.get("menu") is not None or state.get("free") is not True or state.get("input_released") is not True:
            return False
        if facts is not None and (facts.menu_open or not facts.player_free):
            return False
        if run.state.get("world_ui_resumes", 0) >= WORLD_UI_RESUME_MAX:
            run.pending = ("fail", "world_ui_resume_bound")
            return False
        run.state["world_ui_resumes"] = run.state.get("world_ui_resumes", 0) + 1
        result = self._skill_session_goal(session)
        if not result.admitted:
            run.pending = ("fail", "world_resume_refused:" + str(result.reason))
            return False
        run.state.pop("ui_pause", None)
        run.pending = None
        self.__dict__.setdefault("_live7_ui_paused", set()).discard(session.session_id)
        started = self._skill_start(session, run.call, by="plan", facts=facts, resume=run)
        admitted = getattr(started, "admitted", True)
        self._journal(dict(ev="live7_world_resume", skill=run.call.name, arg=run.call.arg,
                           n=run.state["world_ui_resumes"], reason="MENU_OPENED_MID_MOVE",
                           admitted=bool(admitted)))
        if not admitted:
            run.pending = ("fail", "world_resume_refused:" + str(getattr(started, "reason", "")))
            return False
        return True

    def _live7_settle_bound(self, session, held, state):
        """Seconds before another bounded recovery/release attempt. Longer for benign native UI with the pad
        released: a question box (no event) or a busy farmer without a menu (item hold-up). Anything else, an
        unreleased pad or a held / unfresh read keeps SETTLE_SECONDS."""
        if ((held and not str(held).startswith("runtime_not_ready")) or state.get("input_released") is not True
                or session.session_id in self.__dict__.get("_live7_unverified_press", ())):
            return SETTLE_SECONDS
        if state.get("menu") is None and state.get("free") is not True:
            return BUSY_SETTLE_SECONDS
        if state.get("menu") != "DialogueBox":
            return SETTLE_SECONDS
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        if (pid is None or motor is None or self._state.is_stale(pid)
                or getattr(motor, "active_menu", None) != "DialogueBox"):
            return SETTLE_SECONDS
        rec = self._obs.get(pid, "ui.context")
        fresh = (rec is not None and getattr(rec, "authoritative", False) and not getattr(rec, "stale", True)
                 and not getattr(rec, "requires_resync", True)
                 and 0 <= time.monotonic() - getattr(rec, "received_monotonic", -1) <= 2)
        if not fresh or S.event_up(rec.facts):
            return SETTLE_SECONDS
        receiver = S.question_dialogue_receiver(rec.facts, "DialogueBox")
        if receiver is None:
            return SETTLE_SECONDS
        seen = self.__dict__.setdefault("_live7_question_waits", set())
        if (session.session_id, receiver) not in seen:
            seen.add((session.session_id, receiver))
            text = (rec.facts.get("interactive") or {}).get("visible_text")
            self._journal(dict(ev="live7_native_question_wait", receiver=receiver, bound_s=QUESTION_SETTLE_SECONDS,
                               text=text[:DIALOGUE_TEXT_MAX] if isinstance(text, str) else None))
        return QUESTION_SETTLE_SECONDS

    def _live7_interrupt_travel(self, session, event):
        """Retain an admitted trip as a fact through native UI cleanup (ADR 59).

        A stale/rejected choice grants no intent. Recovery still settles the old
        action; only a new choice on fresh offers may start a new travel run.
        """
        run = session.current
        p = session.campaign.live
        if run is None or run.call.name not in ("pergi_ke", "jalan_ke") or p.interrupted_travel is not None:
            return
        p.interrupted_travel = dict(name=run.call.name, arg=run.call.arg,
            date=list(session.campaign.date), event_id=event.get("event_id"), credit=False)
        self._journal(dict(ev="live7_travel_interrupted", **p.interrupted_travel))

    def _live7_recovery_open(self, session):
        return not (self._action_cancel.is_set() or self._worker_stop.is_set()
                    or getattr(self, "_owner_play_stopped", False)
                    or self.__dict__.get("_live7_ladder_halted") == session.session_id)

    def _live7_recover(self, session, reason=None):
        """One bounded, nonblocking readback poll. Owner STOP can acquire the lock immediately."""
        if getattr(self, "_pause_boundary", lambda: False)():
            return 0
        from arti_stardew_runtime import READY, PAUSED_RECOVERY
        c = session.campaign
        if c is None:
            return 0
        if self._live7_watchdog(session):
            return 1
        p = c.live
        with self._lock:
            if not self._live7_active() or session.advancing:
                return 0
            if self._worker_stop.is_set() or getattr(self, "_owner_play_stopped", False) or self._status not in (READY, PAUSED_RECOVERY):
                self._skill_end_locked(session, "held", SAFETY_STOP, "live7:safety_stop:" + self._status)
                return 1
            if self._action_busy or self._inflight_generation is not None:
                # F4: a stuck action/generation gets the ordinary not-READY
                # watchdog's settle deadline; it never waits forever.
                now = game_now(self)
                p.busy_since = p.busy_since or now
                bound = self._skill_cfg("stardew_skill_not_ready_ms", 30000, 0, 600000) / 1000
                if now - p.busy_since >= bound:
                    self._live7_recovery_fault(session, "settle_deadline:" + (
                        "action_busy" if self._action_busy else "generation_inflight"))
                    # Never race an executor or a provider. The 600s watchdog
                    # still runs here; an unknown/busy flag alone is not input leakage.
                return 0
            if p.busy_since is not None:
                p.busy_since = None
                p.wait_since = game_now(self)  # the readback settle starts when the action ends
            session.advancing += 1
        try:
            facts, motor, context, held = self._live7_read(session)
            handoff = self._live7_control_handoff(session)
            if handoff in ("waiting", "stopped"):
                return 1 if handoff == "stopped" else 0
            state = self.skill_post_row_state()
            now = game_now(self)
            p.wait_since = p.wait_since or now
            if held in ("identity_mismatch", "crash_or_identity_unavailable") or journal_refusal(held):
                with self._lock:
                    self._skill_end_locked(session, "held", SAFETY_STOP, "live7:" + held)
                return 1
            if (self._gather_mutation_unresolved
                    and self.__dict__.get("_live7_ladder_halted") == session.session_id):
                # A receipt can predate the HALT. Retain attribution until native clears it.
                p.release_after = None
                return 0
            native_event = self._live7_native_event()
            if native_event is not None:
                self._live7_interrupt_travel(session, native_event)
                slot = self.__dict__.get("_live7_native_event_slot")
                if slot is None or slot[0] != session.session_id:
                    slot = (session.session_id, time.monotonic(), None)
                signature = repr(native_event)
                if signature != slot[2]:
                    self._journal(dict(ev="live7_native_event", facts=native_event, bound_s=600))
                self._live7_native_event_slot = (slot[0], slot[1], signature)
                passive = native_event.get("event_player_control") is not True
                if not passive:
                    # Native player-control sequences retain their ordinary actions and duration.
                    self._live7_native_event_slot = (slot[0], time.monotonic(), signature)
                if passive and time.monotonic() - slot[1] >= 600:
                    with self._lock:
                        self._skill_end_locked(session, "held", SAFETY_STOP, "live7:native_event_timeout")
                    return 1
                if (passive and state.get("input_released") is True and state.get("menu") is None
                        and (not held or str(held).startswith("runtime_not_ready"))):
                    return 0  # native animation/commands own this phase; no input or failure inferred
            elif not held and state.get("free") is True and state.get("menu") is None:
                self.__dict__.pop("_live7_native_event_slot", None)
            if (p.pending_sleep and self._live7_morning_pending() and state.get("input_released") is True
                    and state.get("menu") is None and (not held or str(held).startswith("runtime_not_ready"))):
                p.wait_since = now
                return 0  # native overnight lifecycle owns this menu-free gap; no lost-day/replay
            if held or not state.get("input_released") or state.get("menu") is not None or state.get("free") is not True:
                if ((not held or str(held).startswith("runtime_not_ready")) and state.get("input_released") is True
                        and state.get("menu") == "DialogueBox" and p.knockout is not None
                        and self._live7_knockout_event(session)):
                    p.wait_since = game_now(self)  # M12: the clinic wake-up event, advanced line by line
                    return 0
                if ((not held or str(held).startswith("runtime_not_ready")) and state.get("input_released") is True
                        and state.get("menu") == "DialogueBox"):
                    answer = self._live7_gift_question(session)
                    if answer == "unsafe":
                        return self._live7_uncertain_ui(session, "gift_cleanup_unconfirmed")
                    if answer:
                        p.wait_since = game_now(self)  # her answer was pressed; the settle restarts
                        return 0
                # Preserve the existing gift transfer verifier and its one-answer fence. Every other
                # structured UI (including unknown dialogue, questions, letters and events) uses one boundary.
                # PM-1 a10 d3:J989-992: once the one gift answer is spent (unverified / lease lapsed) the box is an
                # ordinary question again: its published answers return to her chooser instead of a silent read-only
                # wait (600 s stop). The one-publication gift fence itself is unchanged.
                waiting = self._live7_waiting_gift(session, held, state)
                spent = waiting is not None and (session.session_id, waiting.receiver_token) in self.__dict__.get(
                    "_live7_gift_answered", ())
                generic = False if waiting is not None and not spent else self._live7_generic_ui(session)
                if generic == "unsafe":
                    return self._live7_uncertain_ui(session, "ui_cleanup_or_readback_unconfirmed")
                if generic:
                    p.wait_since = game_now(self)  # same pause-aware clock as every other recovery settle
                    return 0
                if (not held and state.get("input_released") is True and state.get("menu") == "DialogueBox"
                        and self._live7_plain_dialogue(session)):
                    p.wait_since = game_now(self)  # the readback settle restarts once the box is closed
                    return 0
                # The model/press can take seconds. Never decide cleanup from the pre-decision read or clock.
                facts, motor, context, held = self._live7_read(session)
                state = self.skill_post_row_state()
                now = game_now(self)
                if held in ("identity_mismatch", "crash_or_identity_unavailable") or journal_refusal(held):
                    with self._lock:
                        self._skill_end_locked(session, "held", SAFETY_STOP, "live7:" + held)
                    return 1
                gift = self._live7_waiting_gift(session, held, state)
                if gift is not None:
                    # One answer attempt only. After 120 s remain read-only until withdrawal/closure or STOP;
                    # a released, fresh native gift box alone is never a safety failure.
                    waited = self.__dict__.setdefault("_live7_gift_wait_timeouts", set())
                    token = (session.session_id, gift.receiver_token)
                    if now - p.wait_since >= QUESTION_SETTLE_SECONDS and token not in waited:
                        waited.add(token)
                        self._journal(dict(ev="live7_gift_wait_timeout", receiver=gift.receiver_token,
                                           bound_s=QUESTION_SETTLE_SECONDS, action="wait_for_box_close"))
                    self._live7_ui_hold(session, "gift_wait", gift.receiver_token)  # fix9's 20 s bound covers it
                    return 0
                if state.get("body_guard_active"):
                    # ADR 183 (a74 B d3 J2445/J2446): her guard's held input is controlled; wait, then re-observe.
                    from arti_runtime.mines_body import note_body_busy
                    note_body_busy(self, None, "settle")
                    p.wait_since = now
                    return 0
                if now - p.wait_since >= self._live7_settle_bound(session, held, state):
                    self._live7_recovery_fault(session, "unconfirmed_cleanup_or_readback", observed=state)
                    if state.get("input_released") is not True:
                        if (not self._live7_release_for_recovery()
                                and self.skill_post_row_state().get("input_held") is True):
                            with self._lock:
                                self._skill_end_locked(session, "held", SAFETY_STOP, "live7:uncontrolled_input")
                            return 1
                    # Read-only until a fresh safe receiver/world boundary arrives.
                    # Save/shipping barriers are never forcibly closed.
                return 0
            if p.release_after is None:
                p.release_after = time.monotonic()  # sensor receipt boundary, not a gameplay deadline
                return 0  # require a new native motor + complete bag after confirmed release
            if self._live7_sleep_recheck(session, motor):
                p.wait_since = p.release_after = None
                with self._lock:
                    self._skill_end_locked(session, "finished", "achieved", "live7_sleep_verified")
                return 1
            if p.pending_sleep and self._live7_morning_pending():
                return 0
            ended = self._live7_guard_day(session, motor, allow_answered_sleep=True, context=context)
            if ended is not None:
                return 1
            run = session.current
            if run is not None:
                if run.state.get("recovery_food_required"):
                    from arti_runtime.mines_body import body_guard_running, note_body_busy
                    if body_guard_running(self):
                        # ADR 189 rule 2: her guard holds the slot; the hand is re-checked after it, never judged now.
                        note_body_busy(self, run.call.name, "held_item_cleanup")
                        p.wait_since = game_now(self)
                        return 0
                    cleared = self._skill_food_cleanup()
                    if cleared is not True:
                        self._live7_recovery_fault(session, "held_item_cleanup_unconfirmed")
                        fresh = self.skill_post_row_state()
                        # fix10e (a14 d9): the wait for a chooser/native readback is bounded like every settle,
                        # never a silent wait (14:50 -> 26:00 with no input and no fault after the first).
                        if fresh.get("menu") is not None:
                            run.state.pop("recovery_food_wait_since", None)
                            return 0  # her chooser owns the open UI; cleanup retries once it closes
                        since = run.state.setdefault("recovery_food_wait_since", game_now(self))
                        if held_item_danger(run):
                            # ADR 189: positive native evidence: her own selections were published and the hand still
                            # holds the object (HELD_ITEM_PUBLISHED_FAILS). Danger that persists: stop.
                            with self._lock:
                                self._skill_end_locked(session, "held", SAFETY_STOP, "live7:unsafe_held_item")
                            return 1
                        if game_now(self) - since < HELD_ITEM_REOBSERVE_S:
                            return 0  # refused before input / unknown read: re-observe (a76 J1047/J1055 slot_busy)
                        # ADR 189 rule 2: bounded re-observe spent without evidence of danger: continue with a FAULT.
                        # The held object stays a fact; every held-tool verb pins its tool natively (fail-closed).
                        self._live7_recovery_fault(session, "held_item_cleanup_deferred",
                                                   observed=dict(fresh, attempts=held_item_attempts(run)))
                    run.state.pop("recovery_food_required")
                    run.state.pop("recovery_food_wait_since", None)
                if self._live7_resume_mail(session, run, facts, motor, context, state):
                    return 1
                action = getattr(self._active, "last_action", None)
                if self._live7_resume_world(session, run, action, facts, state):
                    return 1
                why = reason or (run.pending[1] if run.pending else None) or S._g(action, "reason") or self._live7_leg_end_reason(session, self._status)
                # ADR 257: her press opened the menu (its own receipt); a later input in it stayed unconfirmed and has its
                # own FAULT. The opener closes with its proved result, never as a failure.
                outcome = (tuple(run.pending) if reason is None and run.pending is not None and run.pending[0] == "done"
                           and run.state.get("menu_input_unconfirmed") and self._live7_recovery_open(session)
                           else ("fail", why))
                recover_finish = reason is None
                if outcome[0] == "fail" and (getattr(self, "_config", None) or {}).get("live7_fact_line_enabled") is True:
                    from arti_runtime import walk_refusal  # ADR 231 part B1: an outcome she saw, not a FAULT
                    if walk_refusal.refused(self, session, run, action, state):
                        why, reason = walk_refusal.REASON, None
                    elif walk_refusal.event_arrival(self, session, run, action, state):
                        why, reason = walk_refusal.EVENT_REASON, None
                        self.__dict__.setdefault("_live7_ui_paused", set()).add(session.session_id)
                outcome = outcome if outcome[0] == "done" else ("fail", why)
                self._live7_leg(session, run, outcome, state, recover_finish=recover_finish)
                if run.state.get("live7_outcome") == SAFETY_STOP:
                    with self._lock:
                        self._skill_end_locked(session, "held", SAFETY_STOP, "live7:" + why)
                    return 1
                with self._lock:
                    if session.status != "active" or session.current is not run:
                        return 1
                    terminal = run.state.pop("recovery_terminal", outcome)
                    if terminal[0] == "done" and not self._live7_recovery_open(session):
                        run.state["recovery_observed_reason"] = terminal[1]
                        terminal = ("fail", "recovery_unverified")
                    self._skill_close_run_locked(session, run, terminal)
            self._live7_reconcile(session, facts, motor, context)
            fence = bool(getattr(self, "_gather_mutation_unresolved", False))
            if fence and not p.fence_settled:
                # F1: a possibly-applied mutation that no quarantined/verified row
                # owns is never cleared (its target could be offered again).
                with self._lock:
                    self._skill_end_locked(session, "held", SAFETY_STOP, "live7:unattributed_possible_mutation")
                return 1
            ui_paused = session.session_id in self.__dict__.setdefault("_live7_ui_paused", set())
            self._live7_ui_paused.discard(session.session_id)
            if not ui_paused:
                p.recoveries += 1
            status = reason or self._status
            signature = ("recovery", status, facts.location)
            if not ui_paused:
                p.failures[signature] += 1
            p.wait_since = None
            p.release_after = None
            self._journal(dict(ev="live7_recovery", attempt=p.recoveries,
                               reason="unplanned_ui_pause" if ui_paused else reason or "released_readback_ready",
                               replay=False, approval="automatic"))
            # At this point fresh identity, motor, bag and native release have
            # passed. The prior action/cleanup fault is already recorded; the
            # recovery's completion is not another readiness failure (a23).
            if reason:
                self._live7_recovery_fault(session, reason, observed=state)
            if not ui_paused:
                last = session.runs[-1] if session.runs else None
                p.recovery_history.append(dict(reason=status, location=facts.location,
                    count=p.failures[signature], last_skill=last.call.name if last else None,
                    last_reason=(last.reason or last.verdict) if last else None))
                del p.recovery_history[:-8]
                if p.failures[signature] >= 3:
                    # Recovery is a fresh, released decision boundary, never an activity choice.
                    # Only the clock/vitals safety reserves may restrict her to food/home/sleep.
                    self._journal(dict(ev="live7_loop", reason=status, count=p.failures[signature],
                                       action="model_choose_next_activity", location=facts.location))
                    self._live7_moments("loop", session, motor)
            # Quarantine persists; a new goal owns no unresolved input. The
            # complete fresh bag is the new baseline, not evidence of success.
            with self._lock:
                if not self._live7_active():
                    return 0
                if fence:
                    self._gather_mutation_unresolved = False  # attributed: quarantined or verified
                p.fence_settled = False
            result = self._skill_session_goal(session)  # existing PAUSED_RECOVERY reset path
            if not result.admitted:
                self._live7_help(session, "automatic_recovery_refused:" + str(result.reason))
            return 1
        finally:
            with self._lock:
                session.advancing -= 1
                self._worker_wakeup.notify_all()
