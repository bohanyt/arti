"""Live7 operational facts; all filesystem work belongs to HelpRelay's worker.

The voice path reads an immutable snapshot and acknowledges completed speech.
Queue acceptance alone is not delivery. Facts get at most three attempts within five minutes, while
unresolved status remains available to viewer turns after that deadline.
"""

from __future__ import annotations

from arti_runtime import ipc
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import threading

from arti_help_bus import sanitize_hint_text
from arti_live7_moments import MomentText

LIVE7_AGENCY_FACT = (
    "Kamu memilih tindakan; pilihanmu dijalankan melalui siklus Live7 pada karaktermu sendiri. "
    "Petani HOST adalah karakter game dengan identitas dari pembacaan native. "
    "Penonton chat berada di luar game; identitas chat tidak menetapkan identitas pemain. "
    "Status berhenti berarti siklus tindakan tertahan, bukan kamu tidak punya kendali."
)

RETRY_BACKOFF_SEC = (10.0, 30.0, 60.0)
RETRY_TTL_SEC = 300.0
_READ_BYTES = 262144
_TRIAL = re.compile(r"day-\d+-attempt-\d+\Z")
_TAGS = re.compile(r"\[[^\]]*\]|<[^>]*>")

# Own-activity facts (sat hotfix 10-24): what ARTI's character is doing right now.
ACTIVITY_STALE_WALL_SEC = 180.0      # drop anything not observed within ~3 minutes wall
NATIVE_PIN_SEC = 45.0               # fix55B: a native place outranks anchor-stamped rows only while as fresh as speech facts
ACTIVITY_STALE_GAME_MIN = 120        # ... or more than ~2 game hours behind the latest game clock
ACTIVITY_SCREEN_CHARS = 600          # cap for the "teks/menu di layar" part
_ARCHIVE_NAME = re.compile(r"(\d{3})-(request|response)\.json\Z")
_ARCHIVE_WEATHER = re.compile(r'"weather":"([a-z_]{1,16})","weather_tomorrow":"([a-z_]{1,16})"')
_OUTCOME_ID = {"VERIFIED": "terverifikasi", "PARTIAL": "sebagian terverifikasi", "UNCERTAIN": "belum pasti", "SAFETY_STOP": "berhenti darurat",
               "FAILED": "gagal", "INTERRUPTED": "terputus", "CLEAN_FAIL": "gagal", "NOT_READY": "belum bisa"}
_BAG_FULL_HUD = re.compile("(?<![A-Za-z0-9_])Inventory Full(?![A-Za-z0-9_])", re.I)  # FIX73D: a HUD moment, stale once a newer bag readback has room
_MENU_ID = {"DialogueBox": "kotak dialog", "ShopMenu": "menu toko", "ItemGrabMenu": "menu peti/wadah",
            "GameMenu": "menu utama", "LevelUpMenu": "layar naik level", "ShippingMenu": "ringkasan kiriman",
            "LetterViewerMenu": "surat", "ReadyCheckDialog": "dialog siap tidur", "CraftingPage": "halaman crafting"}


def _detail(value, limit=120):
    from arti_runtime.fact_names import text
    return sanitize_hint_text(_TAGS.sub(" ", text(value)))[:limit] if isinstance(value, str) else ""


def _age_text(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds} dtk lalu" if seconds < 60 else f"{seconds // 60} mnt lalu"


def _game_minutes(hhmm):
    return hhmm // 100 * 60 + hhmm % 100 if type(hhmm) is int and 0 <= hhmm <= 2800 else None


def _clock_text(hhmm):
    minutes = _game_minutes(hhmm)
    if minutes is None:
        return ""
    hour, minute = minutes // 60, minutes % 60
    return "%02d:%02d" % (hour % 24, minute) + (" (lewat tengah malam)" if hour >= 24 else "")


def _date_text(date):
    if isinstance(date, dict):
        date = [date.get("year"), date.get("season"), date.get("day")]
    if (isinstance(date, (list, tuple)) and len(date) == 3 and type(date[0]) is int and isinstance(date[1], str)
            and type(date[2]) is int):
        return f"{_detail(date[1], 12)} {date[2]} tahun {date[0]}"
    return ""


def _game_ctx(row):
    """(location, time, date) from any journal row that carries the character's own game context."""
    game = row.get("game") if isinstance(row.get("game"), dict) else None
    if game is not None:
        return (game.get("location") if isinstance(game.get("location"), str) else None,
                game.get("time") if type(game.get("time")) is int else None,
                game.get("date") if isinstance(game.get("date"), list) else None)
    ev = row.get("ev")
    if ev == "live7_baseline" and isinstance(row.get("before"), dict):
        before = row["before"]
        return (before.get("location") if isinstance(before.get("location"), str) else None,
                before.get("time_of_day") if type(before.get("time_of_day")) is int else None, None)
    if ev == "location_changed" and isinstance(row.get("to"), str):
        return row["to"], None, None
    if ev == "finish_verified" and isinstance(row.get("verdict"), dict):
        observed = row["verdict"].get("observed")
        if isinstance(observed, dict) and isinstance(observed.get("location"), str):
            return observed["location"], None, None
    return None, None, None


def _date_list(label):
    """``"Y1 spring 28"`` (the mind clock label) as the journal's ``[1, "spring", 28]``; anything else is None."""
    m = re.fullmatch(r"Y(\d+) ([a-z]+) (\d+)", label) if isinstance(label, str) else None
    return [int(m[1]), m[2], int(m[3])] if m else None


def _place_text(location):
    # Mod location ids like "FarmHouse2c3d4e~fedcba9876543210" are the owner's cabin/house instance.
    from arti_runtime.place_names import place_label
    return place_label(location) or "tempat (nama belum diketahui)"


# ADR 113 (a50 T107 "[recorded words omitted]"): these codes mean she closed the menu; the journal keeps them, speech gets the meaning.
# FIX73D (a54 T67, a52 T108 "[recorded words omitted]"): "[recorded remark omitted]" also parses as "[recorded remark omitted]".
# The row now names her as the one who closed it and says the menu did not close on its own.
# FIX76A (a55 T18, T168 `CLEAN_FAIL (purchase_refused_by_choice)`, told as bad luck; T167's row got it right): one path for every
# terminal reason. A `*_by_choice` is her own act by its form, so a new code cannot reach her raw; the others say what stopped it.
_MENU_NAMES = {"shop": "menu toko", "shipping": "menu kirim", "store": "menu peti", "crafting": "menu crafting"}
_BY_CHOICE = {"purchase_refused_by_choice": "kamu sendiri yang membatalkan pembelian, bukan toko yang menolak",
              "tv_left_by_choice": "kamu sendiri yang meninggalkan TV"}
_REASON_WORDS = (("terminal_cleanup_unconfirmed:ui_choice_timeout", "menu ditutup otomatis saat waktunya habis, bukan oleh pilihanmu"),
                 ("recovery_unverified", "hasil akhirnya belum bisa dipastikan setelah pemulihan"),
                 ("curfew", "waktunya pulang, kegiatan ini berhenti"), ("no_effect_progress", "tidak ada kemajuan teramati"),
                 ("stalled", "macet, tidak ada kemajuan teramati"), ("partial:mines_low_hp_exit", "keluar tambang karena HP rendah"),
                 ("partial:mines_contact_exhausted_exit", "keluar tambang setelah batas upaya mendekati monster"),
                 ("partial:mines_curfew_exit", "keluar tambang: perkiraan jalan pulang tidak cukup sebelum pingsan jam 02:00"),
                 ("partial:mines_budget_exit", "keluar tambang: jatah skill ini hampir habis"),
                 ("partial:mines_energy_exit", "keluar tambang: energi disisakan untuk membersihkan jalan keluar"),
                 ("mines_curfew", "jalan pulang dari lantai tidak cukup sebelum pingsan jam 02:00"),
                 ("floor_exhausted", "lantai ini sudah habis"), ("water_result_unconfirmed", "hasil siramnya belum terkonfirmasi"),
                 ("interact_target_not_reachable_or_not_offered", "sasarannya tidak bisa dicapai"),
                 ("ladder_down_blocked", "tangga turun terhalang batu atau monster; pecah_batu ditawarkan jika game menawarkannya"),
                 ("ladder_down_not_in_catalog", "tangga turun masih ada tapi belum di katalog pendekatan"),
                 ("ladder_down_not_listed", "tangga turun tidak lagi teramati"),
                 ("observed_mines_progress", "kemajuan tambang teramati (lantai, batu, musuh, atau barang)"),
                 ("partial:observed_mines_progress", "kemajuan tambang teramati, belum selesai"),
                 # ADR 155: not a failed trip; the same destination continues it
                 ("route_paused_new_map", "perjalanan berhenti di peta baru yang ada warganya; tujuannya bisa dilanjutkan"),
                 ("route_paused_people_here", "perjalanan berhenti dulu karena ada yang terlihat di jalan; tujuannya bisa dilanjutkan"),
                 ("route_paused_mailbox", "perjalanan berhenti di dekat kotak suratmu yang bertanda surat; tujuannya bisa dilanjutkan"))


def reason_words(reason):
    """The plain words for a terminal reason. The journal keeps its code; speech gets the meaning."""
    reason = reason if isinstance(reason, str) else ""
    if reason.endswith("_by_choice"):
        thing = reason[:-len("_closed_by_choice")] if reason.endswith("_closed_by_choice") else None
        if thing is not None:
            return ("kamu sendiri yang menutup %s, bukan menu yang tertutup sendiri" % _MENU_NAMES.get(thing, "menu ini")
                    + ("; tokonya buka" if thing == "shop" else ""))
        return _BY_CHOICE.get(reason, "kamu sendiri yang memilih berhenti")
    if talk := talk_words(reason):
        return talk
    return next((words for code, words in _REASON_WORDS if reason == code or reason.startswith(code + ":")), None)


# ADR 161 (a65: 4 of 7 talks ended `talk_target_not_offered`, told as a refusal): what happened to the villager she chose.
_TALK_WORDS = {
    "talk_target_left": "%s keburu pergi dari peta ini sebelum sempat disapa",
    "talk_target_not_reached": "%s belum terjangkau untuk disapa dalam usaha ini",
    "talk_already_greeted": "%s sudah disapa hari ini",
    "talk_nobody_new": "tidak ada warga baru untuk disapa di sini",
    "talk_target_not_offered": "sudah di dekat warganya, tapi game belum memberi tombol ngobrol",
}


def talk_words(reason):
    """The plain words for a talk outcome code (``code`` or ``code:<villager>``), else None."""
    code, _, who = str(reason or "").partition(":")
    words = _TALK_WORDS.get(code)
    if words is None:
        return None
    return words % (_detail(who, 24) or "warganya") if "%s" in words else words


def catch_kind(fish):
    """What the game says a catch is, from its native category; an unknown category says nothing."""
    category = (fish or {}).get("category")
    return "sampah, bukan ikan" if category == -20 else "ikan" if category == -4 else None


def _catch_text(observed):
    """ADR 113 (a50 T85 "[recorded words omitted]" on Broken Glasses, told only `(caught)`): the native row names the catch."""
    fish = observed.get("fish") if isinstance(observed.get("fish"), dict) else None
    if not fish or not isinstance(fish.get("name"), str):
        return None
    kind = catch_kind(fish)
    text = _detail(fish["name"], 32) + (f", {kind}" if kind else "")
    count = observed.get("kind_count_session")
    if type(count) is int and count > 0:
        text += "; " + ("pertama kali dalam sesi hari ini" if count == 1 else f"ke-{count} dalam sesi hari ini")
    # ADR 144: even a carried ledger cannot attest to the farm's unobserved past.
    # The ordinal counts the available records, never the whole game.
    run = observed.get("kind_count_run")
    if type(run) is int and run > 0:
        text += "; " + ("pertama dalam catatan yang tersedia" if run == 1 else f"ke-{run} dalam catatan yang tersedia")
        text += "; riwayat sebelum catatan ini tidak diketahui"
    return f" ({_detail(observed.get('effect'), 12)}: {text}" + ("; jatuh ke tanah)" if observed.get("destination") == "ground" else ")")


# FIX81B (a59 T7, ADR 145): `ngobrol_hadiah` read as "[recorded remark omitted]" after a talk-only finish (bag unchanged). The row says what
# the game did: whom she talked to (the native dialogue speaker, else the villager she chose) and, for a talk-only call,
# that no gift was given. A gift call names its item and villager; row_activity adds the bag delta.
NO_GIFT = "ngobrol saja, tidak ada hadiah yang diberikan"


def social_label(arg, observed, names=None):
    """(label, talk_only clause or None) for an ngobrol_hadiah row."""
    from arti_runtime.fact_names import text
    arg = arg.strip() if isinstance(arg, str) else ""
    if " ke " in arg:
        item, who = arg.rsplit(" ke ", 1)
        return "ngobrol lalu kasih hadiah %s ke %s" % (_detail(text(item, names or {}), 40), _detail(who, 24)), None
    speaker = observed.get("speaker") if isinstance(observed, dict) else None
    who = _detail(speaker, 24) if isinstance(speaker, str) and speaker.strip() else ""
    if not who and arg and arg.casefold() != "warga":  # social_facts.ANY_TALK: no villager of her choosing
        who = _detail(arg, 24) + " (yang kamu pilih)"
    return "ngobrol dengan " + (who or "warga"), NO_GIFT


def _outcome_text(row, known=None):
    """One compact Indonesian outcome for a part_live_row / skill_failed row. `known`: names already read from earlier native rows."""
    from arti_runtime.fact_names import text, item_name, moves_text, ref_name
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    observed = evidence.get("observed") if isinstance(evidence.get("observed"), dict) else {}
    names = {**(known or {}), **(evidence.get("names") or {})}
    for native in (observed, observed.get("fish") or {}, observed.get("planned_target") or {},
                   *observed.get("actual_stored", ()), *observed.get("actual_shipped", ())):
        if native.get("item") and native.get("name"):
            names.setdefault(native["item"], native["name"])
    skill, arg = _detail(row.get("skill"), 32), _detail(text(row.get("arg"), names), 60)
    label = (skill + (" " + arg if arg else "")).strip() or "aksi"
    from arti_runtime.mines_offer import up_label
    label = up_label(row.get("skill"), row.get("arg"), evidence.get("from_location")) or label
    talk_only = None
    if row.get("skill") == "ngobrol_hadiah":
        label, talk_only = social_label(row.get("arg"), observed, names)
    place = evidence.get("place_name") or _place_text(_game_ctx(row)[0] or (evidence.get("cleanup") or {}).get("location"))
    from arti_runtime.action_outcomes import unreadable, UNKNOWN_TEXT
    if row.get("outcome") == "unknown" or unreadable(row.get("reason")):
        return f"{label}: {UNKNOWN_TEXT}; di {place}"
    if row.get("ev") == "skill_failed":
        word = "tidak jadi" if str(row.get("reason")).endswith("_by_choice") else "gagal"
        return f"{label}: {word} ({reason_words(row.get('reason')) or _stop_reason(row)}); di {place}"
    outcome = _OUTCOME_ID.get(row.get("outcome_class"), _detail(row.get("outcome_class"), 20) or "?")
    if str(row.get("reason")).endswith("_by_choice"):
        outcome = "tidak jadi"  # her own act is no failure (a failure word beside "kamu sendiri" fed a wrong mood word, a55 T168)
    observed = evidence.get("observed") if isinstance(evidence.get("observed"), dict) else {}
    extra = ""
    moved = observed.get('actual_stored') or observed.get('actual_shipped')
    moves = evidence.get("chest_moves") or ()
    told_moved = bool(moved) and (row.get('verified') is True or row.get('outcome_class') == 'PARTIAL')
    if told_moved:
        verb = 'tersimpan' if observed.get('actual_stored') else 'terkirim'
        chests = {m["source"] for m in moves}
        if observed.get('actual_stored') and len(chests) == 1:
            verb += f" di {ref_name(next(iter(chests)), names)} (dari tas)"
        extra = '; ' + verb + ' pilihanmu: ' + ', '.join(
            f"{item_name(r.get('item'), names, r.get('name'))} x{r['quantity']} (kualitas {r.get('quality')})" for r in moved)
        if observed.get('original_objective_satisfied') is False:
            target = observed.get('planned_target') or evidence.get('objective') or {}
            if target.get('item') and type(target.get('quantity')) is int:
                extra += f"; tujuan {skill}: {item_name(target['item'], names, target.get('name'))} x{target['quantity']} belum terpenuhi"
            else:
                extra += '; target aksi belum diketahui; pemenuhannya belum terverifikasi'
    elif row.get("verified") is True:
        if isinstance(observed.get("item"), str) and type(observed.get("quantity")) is int:
            extra = f" {item_name(observed['item'], names, observed.get('name'))} x{observed['quantity']}"
        elif catch := _catch_text(observed):
            extra = catch
        elif isinstance(observed.get("effect"), str):
            extra = f" ({_detail(observed['effect'], 24)})"
        elif observed.get("objective") == "enter_location" and isinstance(evidence.get("cleanup"), dict):
            arrival = (evidence.get("arrived_native") or {}).get("location") or evidence["cleanup"].get("location")
            extra = f" (sampai di {_place_text(arrival)})"
    else:
        reason = _stop_reason(row) if row.get("outcome_class") == "SAFETY_STOP" else (
            reason_words(row.get("reason")) or _detail(row.get("reason"), 60))
        extra = f" ({reason})" if reason else ""
        if skill == "pergi_ke" and isinstance(evidence.get("cleanup"), dict):
            actual = evidence["cleanup"].get("location")
            if isinstance(actual, str) and actual:
                extra += f"; belum terverifikasi sampai tujuan, lokasi terbaca {_place_text(actual)}"
    from scripts.live7.whole_state import receipt_line
    if row.get("verified") is not True:
        landed = evidence.get("landed_catch") or {}
        if landed.get("item"):
            extra += "; tangkapan teramati: " + item_name(landed["item"], names, landed.get("name"))
            if type(landed.get("quantity")) is int:
                extra += f" x{landed['quantity']}"
            extra += "; tujuan aksi/cleanup belum terverifikasi"
        gains = evidence.get("inventory_gains") or []
        if gains:
            extra += "; tas bertambah: " + ", ".join(f"{item_name(r['item'], names, r.get('name'))} +{r['quantity']}" for r in gains[:4])
            extra += "; asal pertambahan belum terverifikasi"
    if told := moves_text(moves, names, stored_told=told_moved and bool(observed.get('actual_stored'))):
        extra += "; " + told
    if talk_only:
        extra += "; " + talk_only
    scene = receipt_line(evidence.get("scene_state")) if skill in ("siram", "tanam", "panen") else ""
    total = evidence.get("day_total") if isinstance(evidence.get("day_total"), str) else ""  # ADR 110
    return text(f"{label}: {outcome}{extra}" + "".join("; " + t for t in (scene, total) if t), names) + f"; di {place}"


class ActivityFacts:
    """Fresh, verified facts about ARTI's own character, from journal rows the relay already tails.

    Journal rows rarely carry wall time; supervisor ``trial_decision`` rows do, so every row is
    stamped with the latest such anchor (a lower bound: facts age conservatively). Nothing here is
    an incident; it is per-turn context for voice, and everything stale is dropped."""

    def __init__(self):
        self.anchor = None          # latest wall `at` seen in the journal
        self.game = None            # (location, time, date, wall)
        self.place_name = None      # (location, readable name) of the newest native delivery (ADR 110)
        self.latest_time = None     # latest game clock seen, for the 2-game-hour rule
        self.current = None         # in-progress skill: dict(label, session, wall, game_time)
        self.done = []              # last completed actions: (text, wall, game_time), newest last
        self._unknown_outcomes = {}  # ADR 157: replace an unknown with its own rechecked row.
        self.screen = []            # recent menu/screen items: (text, wall), newest last, deduped
        self.weather = None         # (today, tomorrow, wall)
        self.speech = None          # (ARTI's own in-game line, wall)
        self.bag = None             # FIX73D: (used, free, capacity, wall) from the newest native readback
        self.names = {}             # FIX77B: native names read on rows so far, so a target she just chose is not "unknown"
        self._archive_seen = set()
        self._native = None         # stamp of the native delivery that owns self.game, else None (journal owns it)

    def _stamp(self, row, now, initial):
        at = row.get("at")
        if type(at) in (int, float) and at > 0:
            self.anchor = max(self.anchor or 0, float(at))
            return float(at)
        if self.anchor is not None:
            return self.anchor
        return None if initial else now

    def note(self, row, now, *, initial=False):
        if row.get("domain", "stardew") != "stardew":
            return
        wall = self._stamp(row, now, initial)
        if wall is None:
            return
        location, clock, date = _game_ctx(row)
        # fix54L (review M2): a row stamped older than the stored place (a newer native delivery) never moves the
        # place or clock back. Journal-only stamps never go backwards, so this gate only orders journal vs native.
        newer = self.game is None or wall >= self.game[3]
        if not newer and not initial and self._native is not None and type(row.get("at")) not in (int, float):
            # fix55B (REVIEW2 #5, a39 D22:J651): an anchor stamp is only a lower bound. A live-tailed native
            # transition out of the pinned place is newer by causality (Town -> CommunityCenter while the pin says
            # Town), and a pin older than the speech-fact window no longer outranks rows observed now.
            leaves_pin = (row.get("ev") == "location_changed" and isinstance(row.get("from"), str)
                          and row["from"] == self.game[0])
            if leaves_pin or now - self._native > NATIVE_PIN_SEC:
                newer, wall = True, max(wall, now)
        if clock is not None and newer:
            self.latest_time = clock
        if newer and (location is not None or clock is not None or date is not None):
            prev = self.game or (None, None, None, wall)
            self.game = (location if location is not None else prev[0],
                         clock if clock is not None else prev[1],
                         date if date is not None else prev[2], wall)
            self._native = None
        ev = row.get("ev")
        session = row.get("session_id") if isinstance(row.get("session_id"), str) else None
        if ev in {"skill_started", "skill_leg_started"}:
            if _detail(row.get("skill"), 32):
                self.current = {"label": self._current_label(row), "skill": row.get("skill"), "arg": row.get("arg"),
                                "session": session,
                                "wall": wall, "game_time": clock if clock is not None else self.latest_time,
                                "seen": wall}
        elif (ev in {"part_live_row", "part_live_row_rechecked", "skill_unknown", "skill_failed", "skill_partial"}
              and (ev != "part_live_row_rechecked" or row.get("outcome_id"))
              and not (ev == "skill_unknown" and row.get("outcome_id") in self._unknown_outcomes)):
            if isinstance((row.get("evidence") or {}).get("names"), dict):
                self.names.update(row["evidence"]["names"])
            text = _outcome_text(row, self.names)
            outcome_id = row.get("outcome_id")
            if outcome_id and ev == "part_live_row_rechecked":
                prior = self._unknown_outcomes.pop(outcome_id, None)
                if prior in self.done:
                    self.done.remove(prior)
            if not (self.done and self.done[-1][0] == text):
                self.done.append((text, wall, clock if clock is not None else self.latest_time))
                del self.done[:-3]
            if outcome_id and row.get("outcome") == "unknown":
                self._unknown_outcomes[outcome_id] = self.done[-1]
            self._unknown_outcomes = {k: v for k, v in self._unknown_outcomes.items() if v in self.done}
            if ev in {"skill_unknown", "skill_failed", "skill_partial"} or row.get("outcome_class") in {"VERIFIED", "PARTIAL", "UNCERTAIN", "SAFETY_STOP", "FAILED"}:
                self.current = None
        elif ev == "skill_finished":
            self.current = None
        elif ev == "skill_session_finished":
            self.current = None
        elif self.current is not None and session and session == self.current.get("session"):
            self.current["seen"] = wall
        if ev == "live7_baseline" and isinstance(row.get("before"), dict):
            if isinstance(row["before"].get("names"), dict):
                self.names.update(row["before"]["names"])
            if self.current is not None and self.current.get("arg") == row.get("arg"):
                self.current["label"] = self._current_label(row)  # the baseline follows the start row and carries the target's name
            weather = row["before"].get("weather")
            if isinstance(weather, str):
                tomorrow = self.weather[1] if self.weather else None
                self.weather = (_detail(weather, 16), tomorrow, wall)
        self._note_screen(row, wall)

    def _current_label(self, row):
        from arti_runtime.fact_names import text
        from arti_runtime.mines_offer import up_label
        if label := up_label(row.get("skill"), row.get("arg"), (row.get("before") or {}).get("location")):
            return label
        arg = _detail(text(row.get("arg"), self.names), 32) if isinstance(row.get("arg"), str) else ""
        return _detail(row.get("skill"), 32) + (" " + arg if arg else "")

    def note_bag(self, inventory):
        """FIX73D (a54 T23/T112/T116 "[recorded words omitted]" at 11/12, a53 T33 "[recorded words omitted]" at 10/12): the bag count sat only inside the
        [SPEECH_FACTS] JSON while the status line still showed an older "Inventory Full" HUD. The newest native
        readback (own_state.inventory, stamped at reception) becomes one plain line and retires that older HUD text."""
        value = (inventory or {}).get("value") if isinstance(inventory, dict) else None
        stamp = (inventory or {}).get("observed_ts") if isinstance(inventory, dict) else None
        if (not isinstance(value, dict) or type(stamp) not in (int, float)
                or not all(type(value.get(k)) is int for k in ("used", "free", "capacity"))):
            return
        if self.bag is not None and stamp < self.bag[3]:
            return
        self.bag = (value["used"], value["free"], value["capacity"], float(stamp))
        if value["free"] > 0:
            self.screen = [(t, w) for t, w in self.screen if not (_BAG_FULL_HUD.search(t) and w <= stamp)]

    def note_native(self, current):
        """The newest native delivery owns the current place and clock (ADR 71).

        ``current`` is ``time_grounding.current_state`` of mind/now.json, stamped at native pose reception;
        the journal stamps above arrive only at skill boundaries. a39 T44: a Cabin->Farm warp mid-skill
        left this block saying Cabin while the speech facts said Farm. Older or unknown native state
        never overrides a newer journal stamp."""
        if not isinstance(current, dict) or current.get("basis") != "latest_observation":
            return
        stamp = current.get("observed_ts")
        if type(stamp) not in (int, float) or (self.game is not None and stamp < self.game[3]):
            return
        location = current.get("current_place")
        when = current.get("current_time") if isinstance(current.get("current_time"), dict) else {}
        clock = when.get("time") if type(when.get("time")) is int else None
        date = _date_list(when.get("date"))
        if location is None and clock is None and date is None:
            return
        if clock is not None:
            self.latest_time = clock
        prev = self.game or (None, None, None, stamp)
        self.game = (location if isinstance(location, str) else prev[0],
                     clock if clock is not None else prev[1],
                     date if date is not None else prev[2], float(stamp))
        self._native = float(stamp)
        self.place_name = (location, current.get("place_name"))  # ADR 110: Temp is a festival only by the native calendar

    def _note_screen(self, row, wall):
        from arti_stardew_screen_text import status_text
        ev, text = row.get("ev"), status_text(row)
        if text:
            pass
        elif ev == "ui_inspect":
            menu = _detail(row.get("menu_type"), 24)
            if menu:
                context = _detail(row.get("context"), 16)
                page = _detail(row.get("page_type"), 24)
                text = _MENU_ID.get(menu, menu) + (f" ({_MENU_ID.get(page, page)})" if page else "") \
                    + (f" [{context}]" if context and context not in {"game_menu", "dialogue", "question"} else "") \
                    + (" pertanyaan" if context == "question" else "")
                names = [_detail(r.get("name"), 24) for r in (row.get("recipes") or [])[:6]
                         if isinstance(r, dict) and isinstance(r.get("name"), str)]
                if names:
                    text += ": " + ", ".join(n for n in names if n)
        elif ev in {"ui_press", "ui_nav"}:
            label = _detail(row.get("snapped_label"), 48)
            if label:
                text = f"pilihan menu '{label}'"
        elif ev == "ui_reading_dwell" and isinstance(row.get("kind"), str) and type(row.get("chars")) is int:
            kind = {"dialogue": "dialog", "question": "pertanyaan", "letter": "surat"}.get(row["kind"], _detail(row["kind"], 16))
            text = f"membaca {kind} ({row['chars']} huruf; isi teks tidak tercatat)"
        elif ev == "day_start_detected":
            menu = _detail(row.get("menu_type"), 24)
            if menu:
                text = _MENU_ID.get(menu, menu) + " (awal hari)"
        elif ev == "extras_effect" and row.get("kind") == "tv" and row.get("outcome") == "QuestionOpened":
            text = "TV menyala (menu saluran terbuka)"
        elif ev == "bed_waiting_for_host":
            text = _MENU_ID["ReadyCheckDialog"]
        if not text:
            return
        self.screen = [(t, w) for t, w in self.screen if t != text]
        self.screen.append((text, wall))
        del self.screen[:-8]

    def note_archive(self, directory, now, *, initial=False):
        """Newest provider_archive files: ARTI's own in-game speech and the game's weather facts.

        File mtimes are real wall timestamps. On attach only the newest pair is read."""
        try:
            names = sorted(n for n in os.listdir(directory) if _ARCHIVE_NAME.fullmatch(n))
        except OSError:
            return
        fresh = [n for n in names if n not in self._archive_seen]
        if initial:
            self._archive_seen.update(names)
            fresh = fresh[-2:]
        for name in fresh:
            self._archive_seen.add(name)
            path = Path(directory) / name
            try:
                stat = path.stat()
                if stat.st_size > 262144:
                    continue
                with ipc.open_shared(path, "rb") as handle:
                    record = json.loads(handle.read(262144))
            except (OSError, ValueError, UnicodeError, RecursionError):
                continue
            if not isinstance(record, dict) or record.get("domain", "stardew") != "stardew":
                continue
            wall = float(stat.st_mtime)
            content = record.get("content")
            if name.endswith("-response.json") and isinstance(content, dict):
                raw = content.get("raw_response")
                try:
                    reply = json.loads(raw) if isinstance(raw, str) else None
                except (ValueError, RecursionError):
                    reply = None
                speech = _detail(reply.get("speech"), 160) if isinstance(reply, dict) else ""
                if speech and (self.speech is None or wall >= self.speech[1]):
                    self.speech = (speech, wall)
            elif name.endswith("-request.json") and isinstance(content, dict) and isinstance(content.get("prompt"), str):
                found = _ARCHIVE_WEATHER.search(content["prompt"])
                # The prompt is the only on-disk source of tomorrow's weather; a slightly newer
                # baseline row (today only) must not erase it, so keep the newest wall of the pair.
                if found and (self.weather is None or wall >= self.weather[2] - ACTIVITY_STALE_WALL_SEC):
                    self.weather = (found.group(1), found.group(2), max(wall, self.weather[2] if self.weather else wall))
        if len(self._archive_seen) > 4096:
            self._archive_seen = set(names)

    def _fresh(self, wall, game_time, now):
        if type(wall) not in (int, float) or not 0 <= now - wall <= ACTIVITY_STALE_WALL_SEC:
            return False
        latest, mine = _game_minutes(self.latest_time), _game_minutes(game_time)
        return latest is None or mine is None or latest - mine <= ACTIVITY_STALE_GAME_MIN

    def render(self, now, *, travel_intent=False):
        """``travel_intent`` (the live play rules, V2 of the 2026-11-17 mabar test): a pergi_ke in progress is her
        destination, not her place; the place comes only from the "[recorded remark omitted]" fact above it."""
        parts = []
        if self.game is not None and self._fresh(self.game[3], None, now):
            location, clock, date, wall = self.game
            named = self.place_name[1] if self.place_name and self.place_name[0] == location else None
            where = f"aku di {named or _place_text(location)}" if location else ""
            when = ", ".join(t for t in (f"jam {_clock_text(clock)}" if clock is not None else "",
                                         _date_text(date)) if t)
            if where or when:
                parts.append("Sekarang " + ", ".join(t for t in (where, when) if t) + f" ({_age_text(now - wall)}).")
        if self.bag is not None and self._fresh(self.bag[3], None, now):
            used, free, capacity, wall = self.bag
            parts.append(f"Tas: {used}/{capacity} slot terisi, {free} kosong (bacaan native {_age_text(now - wall)}).")
        if self.current is not None and self._fresh(self.current["seen"], self.current["game_time"], now):
            label, age = self.current["label"], _age_text(now - self.current["wall"])
            if travel_intent and self.current.get("skill") == "pergi_ke":
                destination = label[len("pergi_ke "):] if label.startswith("pergi_ke ") else label
                parts.append(f"Tujuanku: {destination} (belum sampai; pergi_ke dimulai {age} lalu; "
                             "ini niat, bukan tempatku sekarang).")
            else:
                parts.append(f"Sedang kukerjakan: {label} (mulai {age}).")
        done = [f"{t} ({_age_text(now - w)})" for t, w, g in self.done if self._fresh(w, g, now)]
        if done:
            parts.append("Baru selesai: " + "; ".join(done) + ".")
        if self.weather is not None and self._fresh(self.weather[2], None, now):
            today, tomorrow, wall = self.weather
            text = f"Cuaca hari ini {today}" + (f", besok {tomorrow}" if tomorrow else "")
            parts.append(text + f" (data game, {_age_text(now - wall)}).")
        if self.speech is not None and self._fresh(self.speech[1], None, now):
            parts.append(f"Kalimatku di game barusan: \"{self.speech[0]}\" ({_age_text(now - self.speech[1])}).")
        screen, used = [], 0
        for text, wall in reversed(self.screen):
            if not self._fresh(wall, None, now):
                continue
            item = f"{text} ({_age_text(now - wall)})"
            if used + len(item) + 2 > ACTIVITY_SCREEN_CHARS:
                break
            screen.append(item)
            used += len(item) + 2
        if screen:
            parts.append("Teks/menu di layar game-ku barusan: " + "; ".join(screen) + ".")
        return " ".join(parts)


@dataclass(frozen=True)
class StatusEvent:
    event_id: str
    group: str
    priority: int
    fact: str
    resolved: bool = False
    kind: str = ""


def _stop_reason(row):
    """Translate a journal witness, without inventing completion or a deeper cause."""
    reason = _detail(row.get("reason"))
    if (row.get("skill") == "simpan_ke_chest"
            and reason.startswith("terminal_cleanup_unconfirmed:")
            and "paired_transfer_not_verified" in reason):
        return ("macet waktu nutup peti setelah mencoba nyimpan barang; "
                "hasil pemindahan barang belum terverifikasi")
    if (row.get("ev") == "day_start_blocked" and row.get("menu_type") == "ShippingMenu"
            and reason in {"ui_context_unavailable", "wall_bound"}):
        return "tertahan di layar pergantian hari; game belum siap dilanjutkan"
    return reason or _detail(row.get("source")) or "alasan belum diketahui"


def parse_event(row, source="cycle", *, peer_sleep=None, owner=None):
    """Decode actual supervisor/journal records (neither stream has a native ID)."""
    if not isinstance(row, dict) or row.get("domain", "stardew") != "stardew":
        return None
    ev = row.get("ev", row.get("event"))
    if not isinstance(ev, str):
        return None
    reason = _detail(row.get("reason"))
    # Only fix44's native farmer identities can name the host. Never use a chat/config name.
    players = peer_sleep.get("players", ()) if isinstance(peer_sleep, dict) else ()
    host_names = [_detail(p.get("name"), 64) for p in players
                  if isinstance(p, dict) and p.get("role") == "host farmer" and p.get("self") is False]
    host = "petani HOST" + (" (" + host_names[0] + ")" if len(host_names) == 1 and host_names[0] else "")
    if ev == "host_left":
        # host_left is its own plain event, never an incident asking anyone to check anything.
        who = _detail(owner, 64) if owner else "host"
        observed = str(row.get("observed") or "").strip().casefold()
        what = (f"{who} keluar dari sesi Stardew"
                if observed in {"process_gone", "host_left", "quit"}
                else f"koneksi ke sesi Stardew {who} berakhir")
        at = row.get("at")
        when = ""
        if type(at) in (int, float):
            try:
                when = " pada " + datetime.fromtimestamp(at).strftime("%H:%M")
            except (OSError, OverflowError, ValueError):
                when = ""
        last_saved = _date_text(row.get("last_saved_date"))
        if row.get("day_saved") is True:
            save_text = (f"Hari terakhir ({last_saved}) sudah tersimpan (save terverifikasi)."
                         if last_saved else "Hari terakhir sudah tersimpan (save terverifikasi).")
        elif last_saved:
            save_text = f"Hari yang sedang berjalan belum tersimpan (save terverifikasi terakhir: {last_saved})."
        else:
            save_text = "Hari terakhir belum tersimpan (belum ada save terverifikasi)."
        group, priority, resolved = "stop", 1, True
        fact = f"{what}{when}. {save_text} Sesi Stardew berakhir; tidak ada yang dimulai ulang otomatis."
    elif ev in {"live7_stop_file_created", "live7_stop_file"} and row.get("source") == "host_left":
        return None
    elif (ev in {"operator_stop", "observed_stop", "cycle_stopped"}
            or ev in {"live7_stop_file_created", "live7_stop_file"}
            and row.get("source") in {"operator_stop", "observed_stop"}):
        group, priority, resolved, ev = "stop", 2, True, "planned_stop"
        fact = f"{owner or 'Bohan'} menghentikan sesi Stardew."
    elif (ev in {"SAFETY_STOP", "cycle_safety_stop", "live7_stop_file_created",
                 "live7_stop_file", "live7_monitor_violation", "live7_journal_alert", "pause_identity_mismatch"}
            or row.get("outcome_class") == "SAFETY_STOP"):
        reason = _stop_reason(row)
        group, priority, resolved = "stop", 0, False
        state = ("berhenti" if ev in {"live7_stop_file_created", "live7_stop_file"}
                 and row.get("source") not in {"cycle_safety_stop", "pad_watch", "journal_alert", "monitor_violation"}
                 else "berhenti darurat")
        fact = f"game kamu (Stardew) lagi {state} — alasan: {reason}. {owner or 'Bohan'} perlu ngecek."
    elif ev in {"crash", "farmhand_crash", "trial_crash", "session_dropped"}:
        group, priority, resolved = "connection", 1, False
        fact = ("proses pemain ARTI di Stardew crash; permainanmu terputus."
                if ev != "session_dropped" else "sesi pemain ARTI di Stardew terputus.")
    elif ev == "host_crash":
        group, priority, resolved = "connection", 1, False
        fact = "HOST Stardew terputus; menunggu HOST tersedia lagi sebelum menyambung."
    elif ev in {"rejoin_attempt", "rejoin_started", "rejoin_start", "rejoin_failed"}:
        group, priority, resolved = "connection", 1, False
        fact = ("percobaan menyambung ulang ke Stardew gagal; belum tersambung."
                if ev == "rejoin_failed" else "pemain ARTI sedang menyambung ulang ke Stardew; belum tersambung.")
    elif ev in {"rejoin_complete", "rejoin_ended", "rejoin_end"}:
        group, priority, resolved = "connection", 1, True
        fact = "pemain ARTI sudah tersambung kembali ke Stardew."
    elif ev in {"game_paused", "game_resumed", "game_pause_unavailable"}:
        group, priority, resolved = "game_pause", 2, ev != "game_paused"
        fact = (f"game dijeda oleh {owner or 'Bohan'}; aku menunggu game dilanjutkan."
                if not resolved else f"game dilanjutkan oleh {owner or 'Bohan'}; aku bisa melanjutkan.")
    elif (ev in {"host_wait", "bed_waiting_for_host"}
          or ev == "day_start_waiting" and row.get("host_wait") is True):
        group, priority, resolved = "host_wait", 2, False
        fact = (f"malam ARTI sudah beres; hari baru mulai setelah layar malam {host}/pemain lain selesai."
                if row.get("phase") == "day_start" else
                f"pemain ARTI di Stardew sudah di tempat tidur; hari berganti setelah {host} juga tidur.")
    elif ev in {"day_start_cleared", "day_start_blocked", "host_wait_ended", "bed_wait_ended", "day_trial_started", "day_ended", "night_saved"}:
        group, priority, resolved = "host_wait", 2, True
        fact = "waktu menunggu HOST di Stardew sudah selesai."
    elif ev in {"stop_cleared", "live7_stop_file_cleared"}:
        group, priority, resolved = "stop", 0, True
        fact = "penanda berhenti darurat Stardew sudah dilepas; kondisi game belum dipastikan."
    else:
        return None
    native_id = row.get("event_id")
    identity = str(native_id) if isinstance(native_id, (str, int)) else hashlib.sha256(
        json.dumps(row, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
    return StatusEvent(f"{source}:{identity}", group, priority, fact, resolved, ev)


class _Tail:
    """Bounded incremental binary tail, including split UTF-8 and truncation."""

    def __init__(self):
        self.offset = 0
        self.partial = b""
        self.identity = None

    def read(self, path, *, initial_tail=False):
        try:
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            fresh = identity != self.identity
            if fresh or stat.st_size < self.offset:
                self.offset, self.partial = 0, b""
            self.identity = identity
            with ipc.open_shared(path, "rb") as handle:
                if fresh and initial_tail and stat.st_size > _READ_BYTES:
                    # Journals are megabytes long. Bootstrap only recent
                    # diagnostics; skip the first potentially split record.
                    handle.seek(stat.st_size - _READ_BYTES)
                    handle.readline(_READ_BYTES)
                else:
                    handle.seek(self.offset)
                data = handle.read(_READ_BYTES)
                self.offset = handle.tell()
        except OSError:
            return []
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        if len(self.partial) > _READ_BYTES:
            self.partial = b""  # malformed oversized line; never grow unbounded
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows


class StatusText(MomentText):
    """Trusted moment subtype: same log/RAG treatment, independent enable flag."""

    _live7_status = True

    def is_current(self):
        return self._owner.is_current(self)

    def mark_voiced(self):
        self._owner.mark_voiced(self)


@dataclass
class _Pending:
    event: StatusEvent
    expires: float
    next_try: float = 0.0
    text: StatusText | None = None
    attempts: int = 0


def cycle_stop_present(cycle_dir) -> bool:
    """True when LIVE7_STOP or WATCH_STOP marks this cycle/run as ended."""
    if cycle_dir is None:
        return False
    cycle = Path(cycle_dir)
    try:
        return any(p.exists() for p in (
            cycle / "LIVE7_STOP", cycle / "WATCH_STOP",
            cycle.parent.parent / "LIVE7_STOP"))
    except OSError:
        return False


def _operator_stop_present(cycle_dir) -> bool:
    """True when an operator or non-host_left stop file is present on this cycle/run."""
    if cycle_dir is None:
        return False
    from arti_stardew_stop import read_stop
    cycle = Path(cycle_dir)
    try:
        if (cycle / "OPERATOR_STOP").exists():
            return True
        for path in (cycle / "LIVE7_STOP", cycle.parent.parent / "LIVE7_STOP"):
            raw, stop = read_stop(path)
            if raw is not None and not (isinstance(stop, dict) and stop.get("writer") == "supervisor.host_left"):
                return True
    except (OSError, ValueError):
        return True
    return False


class StatusFactsRelay:
    def __init__(self, config, moments, *, clock, voice_ready=None, extra_session_facts=None, on_host_left=None):
        self.config, self._moments, self._clock = config, moments, clock
        self._voice_ready = voice_ready or (lambda: True)
        self._extra_session_facts = extra_session_facts or (lambda: ())
        self._on_host_left = on_host_left
        self._session_over = False
        self._lock = threading.RLock()
        self._cycle = None
        self._tails = {}
        self._seen = set()
        self._active = {}
        self._gifts = {}
        self._pending = {}
        self._snapshot = ""
        self.attention_native = {}  # ADR 172: memory-only, existing native facts only
        self.attention_traits = []
        from arti_runtime.read_in_game import ReadInGameRelay
        self._read_in_game = ReadInGameRelay()
        self._trial = None
        self._diagnostic = None
        self._diagnostic_anchor = None
        self._connection_cause = ""
        self._stop_present = False
        self._stopped = False
        self._host_left_active = False
        self._operator_stopped = False
        self._ownership = {}  # smoke 10-24 rows 14/22: own money + own planted crops, facts only
        self._activity = ActivityFacts()  # sat hotfix 10-24: location/time/skill/results/screen, aged, stale dropped
        self._peer_sleep, self._native_ts, self._owner_hosted, self._solo_run = None, None, False, False
        self._pause_until = 0.0
        self._self_key, self._self_lines = None, []
        self._publish()  # session facts (mabar) are known before the first game row

    def status_block(self):
        """Memory-only, lock-free read on the latency-sensitive voice path."""
        return self._snapshot if not self._stopped and self.config.get("live7_status_facts_enabled", True) else ""

    def stop(self):
        with self._lock:
            self._stopped = True
            self._pending.clear()
            self._snapshot = ""

    def _self_facts(self):
        """Mabar: her own verified facts (e.g. her look read back from the native profile), from a small file the
        operator writes after verifying them; re-read only when it changes. Missing or unreadable = none."""
        path = self.config.get("live7_self_facts_file")
        if not path:
            return []
        try:
            st = os.stat(path)
        except OSError:
            return []
        key = (st.st_mtime_ns, st.st_size)
        if key != self._self_key:
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                lines = [str(x).strip()[:300] for x in (data.get("lines") or ())[:6] if str(x).strip()]
            except (OSError, ValueError, AttributeError):
                lines = []
            self._self_key, self._self_lines = key, lines
        return self._self_lines

    def _memory_down(self):
        """Mabar: say plainly when vault recall stops answering mid-session (owner rule: memory failure is visible,
        never faked). Startup failures already ride in the session facts."""
        if self.config.get("livepath_mode") != "mabar" or not self.config.get("vault_rag_enabled"):
            return ""
        try:
            import arti_vault_rag
            down = arti_vault_rag.embedding_breaker_open()
            fails = 0
            try:
                fails = int((arti_vault_rag.embedding_status() or {}).get("fails") or 0)
            except Exception:
                fails = 0
        except Exception:
            return ""
        if not down:
            return ""
        return ("Ingatan vault-mu lagi mati sementara (sebab: embedding tidak menjawab, gagal %d kali); "
                "jangan ngarang soal sesi lama." % fails)

    def _publish(self):
        if self._host_left_active:
            stop_fact = self._active.get("stop")
            self._snapshot = (
                "\n[STARDEW STATUS] " + stop_fact
                + " Pakai fakta ini apa adanya; jangan mengarang kondisi game.\n"
                if stop_fact else ""
            )
            return
        facts = [self._active[k] for k in ("stop", "connection", "game_pause", "host_wait") if k in self._active]
        now = self._clock()
        activity = self._activity.render(now, travel_intent=bool(self.config.get("live_play_rules_enabled")))
        from arti_runtime.human_host import peer_sight_fact
        peer_sight = ("" if self._stop_present or self._session_over or "stop" in self._active or self._solo_run else
                      peer_sight_fact(self._peer_sleep, now=now, max_age=NATIVE_PIN_SEC, owner_hosted=self._owner_hosted,
                                      native_fresh=self._native_ts is not None and 0 <= now - self._native_ts <= NATIVE_PIN_SEC))
        owned = [self._ownership[k] for k in ("money", "crops") if k in self._ownership]
        owned.extend(self._gifts.values())
        # Mabar: plain session facts the operator states (e.g. new co-op farm with the owner), never a script.
        limit = max(4, int(self.config.get("live7_session_facts_limit", 4)))
        session = [str(f).strip()[:200] for f in (self.config.get("live7_session_facts") or ())[:limit] if str(f).strip()]
        extra = [str(f).strip()[:160] for f in (self._extra_session_facts() or ()) if str(f).strip()]
        if extra:
            session = session + extra
        if session:
            owned = owned + ["Sesi: " + " ".join(session)]
        memory = self._memory_down()
        if memory:
            owned = owned + [memory]
        own = self._self_facts()
        if own:
            owned = owned + ["Dirimu, dibaca dari profil native game (yang tidak tercatat di sini belum diketahui): "
                             + " ".join(own)]
        if not facts and not owned and not activity and not peer_sight:
            self._snapshot = ""
            return
        # One line: trim_system_prompt_for_llm reserves exactly this line outside every cut.
        from arti_runtime.coop_session import owner_name
        body = facts + ([activity] if activity else []) + ([peer_sight] if peer_sight else []) + owned
        self._snapshot = ("\n[STARDEW STATUS] " + LIVE7_AGENCY_FACT + " " + " ".join(body)
                          + " Fakta ini tentang karakter ARTI sendiri (sebut 'aku'), bukan " + owner_name(self.config) + " atau penonton."
                          + " Pakai fakta ini apa adanya; jangan mengarang apa pun di luar fakta ini"
                          + " (lokasi, jam, energi, HP, gerakan, isi teks layar). Yang tidak tercatat di sini, bilang belum tahu."
                          + (" Kalau ditanya kenapa diam, jujur soal kondisi ini; jangan mengaku sedang bekerja." if facts else "")
                          + "\n")

    def _note_ownership(self, row):
        """Own-wallet gold and own VERIFIED plantings from existing journal rows; never an offer rule."""
        ev = row.get("ev")
        if ev == "live7_baseline":
            money = (row.get("before") or {}).get("money") if isinstance(row.get("before"), dict) else None
            if type(money) is int and money >= 0:
                self._ownership["money"] = f"Uangmu sendiri: {money}g (dompet karakter ARTI)."
        elif ev == "live7_crop_memory" and isinstance(row.get("crops"), dict):
            places, ripe, dead, death_dates = {}, [], 0, []
            for record in row["crops"].values():
                if not isinstance(record, dict) or not isinstance(record.get("location"), str):
                    continue
                tile = record.get("tile")
                label = ("%d,%d" % tuple(tile) if isinstance(tile, list) and len(tile) == 2
                         and all(type(v) is int for v in tile) else "?")
                state = record.get("state")
                if state == "ready":  # ADR 45: a ready, unharvested own crop stays a dated fact across days
                    since = record.get("ready_date")
                    ripe.append("%s %s%s" % (_detail(record["location"]) or "?", label,
                                             " sejak Y%s %s %s" % tuple(since) if isinstance(since, list) and len(since) == 3 else ""))
                    continue
                if state == "dead":
                    dead += 1
                    date = record.get("dead_date")
                    if isinstance(date, list) and len(date) == 3:
                        reason = "season change" if record.get("dead_reason") == "season_change" else "native dead"
                        label = "Y%s %s %s (%s)" % (*date, reason)
                        if label not in death_dates:
                            death_dates.append(label)
                    continue
                places.setdefault(record["location"], []).append(label)
            growing = sum(len(t) for t in places.values())
            total = growing + len(ripe) + dead
            where = "; ".join("%d di %s (petak %s%s)" % (len(t), _detail(loc) or "?", ", ".join(t[:3]),
                                                        " +%d" % (len(t) - 3) if len(t) > 3 else "")
                              for loc, t in sorted(places.items()))
            parts = [where] if where else []
            if ripe:
                parts.append("%d siap panen belum kamu panen (%s%s)" % (len(ripe), "; ".join(ripe[:3]),
                                                                       " +%d" % (len(ripe) - 3) if len(ripe) > 3 else ""))
            if dead:
                parts.append("%d mati" % dead + ("; terlihat " + ", ".join(death_dates[:2]) if death_dates else ""))
            self._ownership["crops"] = (f"Tanaman yang kamu tanam sendiri (terverifikasi): {total}; {'; '.join(parts)}."
                                        if total else "Tanaman yang kamu tanam sendiri (terverifikasi): 0 saat ini.")
            at = row.get("at")
            date = _date_text((row.get("game") or {}).get("date"))
            if type(at) in (int, float):
                self._ownership["crops"] += f" Catatan tanaman: {date or 'tanggal belum diketahui'}, diterima {at:.3f}."

    def _note_gift(self, row):
        # Only the bag-verified receipt establishes provenance. This records
        # receipt history, not a claim that the item is still in the bag.
        if row.get("ev") != "live7_gift_received" or row.get("domain", "stardew") != "stardew":
            return
        qid, stack = row.get("item_qid"), row.get("stack")
        sender = _detail(row.get("sender")).strip()[:64]
        if (not isinstance(qid, str) or not qid.strip() or len(qid) > 96
                or not sender or type(stack) is not int or not 0 < stack <= 999999):
            return
        name = (_detail(row.get("item_name")) or _detail(qid)).strip()[:80]
        if not name:
            return
        with self._lock:
            key = (qid, sender)
            self._gifts.pop(key, None)
            self._gifts[key] = f"Barang yang kamu terima dari {sender}: {name} (x{stack})."
            while len(self._gifts) > 8:
                self._gifts.pop(next(iter(self._gifts)))
            self._publish()

    def _accept(self, event, now, *, notify=True):
        on_left = None
        with self._lock:
            if event.event_id in self._seen:
                return
            self._seen.add(event.event_id)
            if event.kind == "host_left":
                # Clear ownership, gifts, pause facts, activity and co-op block at once.
                self._host_left_active = True
                self._active.clear()
                self._pending.clear()
                self._ownership.clear()
                self._gifts.clear()
                self._activity = ActivityFacts()
                self._peer_sleep = self._native_ts = None
                self._pause_until = 0.0
                self._connection_cause, self._diagnostic = "", None
                self._trial = None
                on_left = self._on_host_left
            elif event.kind == "planned_stop":
                self._active.clear()
                self._pending.clear()
                self._connection_cause, self._diagnostic = "", None
                self._trial = None  # do not reinterpret late shutdown journal rows as a new emergency
            if event.group == "connection":
                # A disconnect ends the old bed wait. Preserve the cause when
                # crash + rejoin start/end arrive in one worker poll.
                self._active.pop("host_wait", None)
                self._pending.pop("host_wait", None)
                if event.kind in {"crash", "farmhand_crash", "trial_crash", "session_dropped", "host_crash"}:
                    self._connection_cause = event.fact
                elif self._connection_cause:
                    event = replace(event, fact="Sebelumnya " + self._connection_cause + " " + event.fact)
                if event.resolved:
                    self._connection_cause = ""
            previous = self._active.get(event.group)
            # A planned STOP or host_left resolves an incident, but the session remains
            # stopped. Keep that neutral fact for ordinary voice questions
            # until STOP clears or a new cycle replaces this one.
            keep_resolved = event.kind in ("planned_stop", "host_left")
            if event.resolved and not keep_resolved:
                self._active.pop(event.group, None)
            else:
                self._active[event.group] = event.fact
            # Coalesce supervisor mirrors/reminders, and retire obsolete speech
            # when resolution arrives before the model gets a voice gap.
            if previous == event.fact or (event.resolved and previous is None and not keep_resolved):
                return
            self._pending.pop(event.group, None)
            if notify and not (event.group == "host_wait" and event.resolved):
                # ADR 90: an ordinary bed wait ends in seconds and stays in the status block; only one that
                # lasts is worth her words (a44: 25 nights, 5-11 s each, 25 of 40 self-started replies).
                wait = (max(0.0, float(self.config.get("live7_host_wait_speech_after_sec", 0) or 0))
                        if event.group == "host_wait" else 0.0)
                self._pending[event.group] = _Pending(event, now + RETRY_TTL_SEC + wait, next_try=now + wait)
            self._publish()
        if callable(on_left):
            on_left()

    def _read(self, path, *, initial_tail=False):
        tail = self._tails.setdefault(path, _Tail())
        return tail.read(path, initial_tail=initial_tail)

    def _poll(self, cycle_dir, now):
        if cycle_dir is None:
            return
        # Worker-only I/O; the voice path continues reading an immutable snapshot.
        import arti_mind
        from arti_runtime import mind_scope
        from arti_runtime.time_grounding import current_state, speech_facts
        current = (mind_scope.read_now_for_config(self.config, now=now, cycle_dir=cycle_dir)
                   if mind_scope.enabled(self.config)
                   else arti_mind.read_now(arti_mind.mind_dir(self.config))) or {}
        native = speech_facts(current) if current.get("game") == "stardew" else {}
        self.attention_native = native
        if self.config.get("attention_desk_enabled"):
            self.attention_traits = arti_mind.load_prefs(arti_mind.mind_dir(self.config))
        peer_sleep = (native.get("peer_sleep") or {}).get("value")
        cycle = Path(cycle_dir)
        if cycle != self._cycle:
            from arti_runtime.human_host import is_owner_hosted_run, is_solo_run
            owner_hosted = is_owner_hosted_run(cycle.parent.parent)
            solo_run = is_solo_run(cycle.parent.parent) or self.config.get("stardew_host_controller") == "harness"
            with self._lock:
                self._cycle = cycle
                self._tails, self._seen, self._active, self._pending = {}, set(), {}, {}
                self._gifts = {}
                self._trial, self._diagnostic, self._stop_present = None, None, False
                self._connection_cause = ""
                self._diagnostic_anchor = None
                self._ownership = {}
                self._activity = ActivityFacts()
                self._peer_sleep, self._native_ts, self._owner_hosted, self._solo_run = None, None, owner_hosted, solo_run
                self._pause_until = 0.0
                self._host_left_active = False
                self._operator_stopped = False
                self._publish()
        rows = self._read(cycle / "cycle_events.jsonl")
        for row in rows:
            trial = row.get("day_trial")
            if isinstance(trial, str) and _TRIAL.fullmatch(trial):
                if trial != self._trial:
                    self._diagnostic = None
                    self._diagnostic_anchor = None
                self._trial = trial
        if self._trial:
            journal = cycle / self._trial / "journal" / "g1_journal.jsonl"
            initial = journal not in self._tails
            for row in self._read(journal, initial_tail=True):
                if row.get("domain", "stardew") != "stardew":
                    continue
                if self.config.get("attention_desk_enabled"):
                    self._read_in_game.note(row)
                if type(row.get("at")) in (int, float):
                    self._diagnostic_anchor = row["at"]
                with self._lock:
                    self._activity.note(row, now, initial=initial)
                self._note_gift(row)
                if row.get("ev") in {"live7_baseline", "live7_crop_memory"}:
                    # Ownership is state, not an incident: history rows restore it on attach too.
                    with self._lock:
                        self._note_ownership(row)
                        self._publish()
                    continue
                if row.get("ev") in {"day_start_blocked", "live7_journal_alert", "part_live_row"}:
                    if row.get("ev") != "part_live_row" or row.get("outcome_class") == "SAFETY_STOP":
                        # On attach, only an explicit or preceding same-trial
                        # timestamp proves age. Newly tailed lines are observed now.
                        self._diagnostic = (dict(row), row.get("at", self._diagnostic_anchor if initial else now))
                from arti_runtime.coop_session import owner_name
                event = parse_event(row, str(journal), peer_sleep=peer_sleep, owner=owner_name(self.config))
                if event is not None and event.group == "game_pause":
                    at = row.get("at")
                    if event.resolved or type(at) in (int, float) and 0 <= now - at <= 3:
                        self._pause_until = now + 3 if not event.resolved else 0
                        self._accept(event, now, notify=not initial)
                # On attach, the cycle history is authoritative over the older
                # journal. New journal records can announce a stop immediately.
                if event is not None and not initial:
                    self._accept(event, now)
            with self._lock:
                self._activity.note_archive(journal.parent / "provider_archive", now, initial=initial)
        for row in rows:
            from arti_runtime.coop_session import owner_name
            event = parse_event(row, str(cycle), peer_sleep=peer_sleep, owner=owner_name(self.config))
            if event is None:
                continue
            if (event.kind in {"SAFETY_STOP", "cycle_safety_stop"}
                    or event.kind in {"live7_stop_file_created", "live7_stop_file"}
                    and row.get("source") == "cycle_safety_stop"):
                # The supervisor emits STOP-file then SAFETY_STOP with no reason.
                # Enrich both mirrors, consuming once at the terminal event. Journal
                # timestamps are sparse: a preceding trial_decision is a conservative
                # wall-time witness, scoped to this trial (never attach-time freshness).
                diagnostic = self._diagnostic
                if event.kind in {"SAFETY_STOP", "cycle_safety_stop"}:
                    self._diagnostic = None
                if not row.get("reason") and diagnostic:
                    detail, at = diagnostic
                    if type(at) in (int, float) and 0 <= now - at <= RETRY_TTL_SEC:
                        enriched = dict(row, reason=_stop_reason(detail))
                        event = replace(event, fact=parse_event(enriched, str(cycle), owner=owner_name(self.config)).fact)
            at = row.get("at")
            recent = type(at) in (int, float) and 0 <= now - at <= RETRY_TTL_SEC
            self._accept(event, now, notify=recent)
        nf = current.get("native_facts") if isinstance(current.get("native_facts"), dict) else {}
        cm = nf.get("current_menu") if isinstance(nf.get("current_menu"), dict) else {}
        on_title = cm.get("value") == "TitleMenu" and (cm.get("observed_ts") is None or (type(cm.get("observed_ts")) in (int, float) and 0 <= now - cm["observed_ts"] <= NATIVE_PIN_SEC))
        if current.get("game") == "stardew" and not self._host_left_active and not on_title:
            # After this poll's journal rows: the newest stamp wins, whichever side wrote it. Freshness is
            # wall time, as for the peer facts above and speech_facts; the relay clock only ages the line.
            self._activity.note_native(current_state(current))
            self._activity.note_bag(((nf.get("own_state") or {}).get("inventory")))
            self._peer_sleep = nf.get("peer_sleep") if isinstance(nf.get("peer_sleep"), dict) else None
            cur_state = current_state(current, now=now, max_age=NATIVE_PIN_SEC)
            self._native_ts = max((float(s) for s in (cur_state.get("observed_ts") if cur_state.get("basis") == "latest_observation" else None,
                                                      (self._peer_sleep or {}).get("observed_ts")) if type(s) in (int, float) and 0 <= now - s <= NATIVE_PIN_SEC), default=None)
        else:
            self._peer_sleep = self._native_ts = None
        if self._pause_until and now > self._pause_until:
            with self._lock:
                self._active.pop("game_pause", None)
                self._pending.pop("game_pause", None)
                self._pause_until = 0
                self._publish()
        # Keep tailing the selected cycle after the resolver loses its active
        # status at SAFETY_STOP. A new active cycle explicitly replaces it.
        present = cycle_stop_present(cycle)
        self._operator_stopped = _operator_stop_present(cycle)
        if self._stop_present and not present and "stop" in self._active:
            if self._host_left_active:
                with self._lock:
                    self._active.pop("stop", None)
                    self._pending.pop("stop", None)
                    self._host_left_active = False
                    self._publish()
            else:
                self._accept(StatusEvent(f"{cycle}:cleared:{now}", "stop", 0,
                    "penanda berhenti darurat Stardew sudah dilepas; kondisi game belum dipastikan.", True), now)
        self._stop_present = present

    def _host_left_deliverable(self):
        pending = self._pending.get("stop")
        return (not self._stopped and self._host_left_active and not self._operator_stopped
                and pending is not None and pending.event.kind == "host_left")

    def is_current(self, text):
        with self._lock:
            pending = self._pending.get(text._group)
            if (self._stopped or not self.config.get("live7_status_facts_enabled", True)
                    or pending is None or pending.text is not text or self._clock() >= pending.expires):
                return False
            if self._host_left_deliverable() and pending.event.kind == "host_left":
                return True
            return not self._stop_present and not self._session_over

    def mark_voiced(self, text):
        with self._lock:
            if self.is_current(text):
                pending = self._pending.pop(text._group)
                if pending.event.priority < 2:
                    self._moments._last_voiced_at = self._clock()

    def tick(self, cycle_dir, enqueue, *, now, help_open=False, defer_low_priority=False,
             session_over=False):
        if self._stopped or not self.config.get("live7_status_facts_enabled", True):
            with self._lock:
                self._pending.clear()
                self._snapshot = ""
            return False
        self._poll(cycle_dir, now)
        self._session_over = bool(session_over or self._stop_present)
        if self._session_over:
            self._read_in_game.pending.clear()
            with self._lock:
                if self._host_left_deliverable():
                    pending = self._pending["stop"]
                    self._pending = {"stop": pending} if now < pending.expires else {}
                else:
                    self._pending.clear()
                self._publish()
                deliver_host_left = self._host_left_deliverable()
            if deliver_host_left:
                return self.deliver(enqueue, now=now, help_open=help_open, high_priority_only=False)
            return False
        if self.config.get("attention_desk_enabled"):
            self._read_in_game.deliver(enqueue)
        with self._lock:
            self._publish()
            self._pending = {k: p for k, p in self._pending.items() if now < p.expires}
        return self.deliver(enqueue, now=now, help_open=help_open, high_priority_only=defer_low_priority)

    def low_priority_pending(self):
        with self._lock:
            return any(p.event.priority >= 2 for p in self._pending.values())

    def deliver(self, enqueue, *, now, help_open=False, high_priority_only=False):
        """Memory-only delivery pass; optional moments go before low-priority facts."""
        with self._lock:
            host_left_only = self._host_left_deliverable()
        if (self._stopped or not self.config.get("live7_status_facts_enabled", True)
                or ((self._stop_present or self._session_over) and not host_left_only)):
            return False
        with self._lock:
            pending = min(self._pending.values(), key=lambda p: p.event.priority, default=None)
            if pending is None:
                return False
            blocking = pending.event.priority < 2
            if high_priority_only and not blocking:
                return False
        # Predicates and enqueue never hold the status lock. Fail closed and
        # leave the fact pending if the queue or an advisory gate fails.
        try:
            if (pending.attempts >= len(RETRY_BACKOFF_SEC) or now < pending.next_try or not self._voice_ready()
                    or not self._moments.can_deliver(now, help_open=help_open)):
                return blocking
            text = StatusText("[Stardew] Fakta: " + pending.event.fact)
            text._owner, text._group = self, pending.event.group
            with self._lock:
                pending.text, pending.next_try = text, now + RETRY_BACKOFF_SEC[pending.attempts]
                pending.attempts += 1
            enqueue(text, trigger_type="game")
        except Exception:
            pass
        return blocking  # only stop/connection facts reserve delivery ahead of moments
