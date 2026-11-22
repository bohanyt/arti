"""Optional, passive Live7 situations for the Bridge; never speech or game commands.

Capture uses existing fresh reads / verified rows only. File IO is deferred to
the skill flush, outside the runtime lock. Restart the trial to load config.
"""
from __future__ import annotations

from arti_runtime import ipc
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time


SITUATIONS = {
    "bought_seeds": "Baru beli {count} bibit, bekal buat kebun bertambah.",
    "planted": "{count} bibit berhasil tertanam di kebun.",
    "watered_all": "Tanaman yang terpantau di sekitar sudah basah semua, beres urusan siram.",
    # FIX81B (a59 T20): the old row "[recorded remark omitted]" was told with 3 dead crops and none of her own alive; the
    # rain row now says the native rule and her own live count (rain_situation), never that something got water.
    "rain_day": "Hari ini hujan (data game); hujan menyiram tanaman hidup di luar ruangan.",
    "shop_closed": "Toko bibit lagi tutup, belanja bibit belum bisa.",
    "went_to_town": "Baru sampai di kota.",
    "harvested": "{count} hasil panen baru masuk tas.",
    "bed": "Hari sudah berganti setelah istirahat di kasur.",
    "stuck": "Jalan atau kerjaan berulang tanpa kemajuan.",
}
INTERVAL_S = 60.0
TTL_S = 120


def rain_situation(crops, date):
    """FIX81B (ADR 145): the rain row from her own crop ledger (CropMemory: her VERIFIED plantings, ADR 45).

    States how many of her own crops are alive and where, and how many are dead, so a rain day with nothing alive
    reads as that. The ledger is hers only; crops she did not plant are not counted as hers. None = no ledger read."""
    if not isinstance(crops, dict):
        return None
    alive, dead = {}, 0
    for record in crops.values():
        if not isinstance(record, dict):
            continue
        state = record.get("state")
        if state in ("growing", "ready"):
            place = record.get("location") if isinstance(record.get("location"), str) else "?"
            alive[place] = alive.get(place, 0) + 1
        elif state == "dead":
            dead += 1
    from arti_runtime.place_names import place_label
    head = "Hari ini hujan (data game); hujan menyiram tanaman hidup di luar. "
    if alive:
        where = ", ".join("%s %d" % (place_label(k) or k, n) for k, n in sorted(alive.items())[:3])
        line = "Tanaman hidup tanamanmu: %d (%s)" % (sum(alive.values()), where)
    else:
        line = "Tanaman hidup tanamanmu: tidak ada, jadi tak ada tanamanmu yang tersiram"
    if dead:
        line += "; %d tanamanmu sudah mati" % dead
    return (head + line + ".")[:189]  # the bridge keeps 200 chars with the "[Stardew] " tag


def discovery_importance(event):
    """Adapter-owned tier hints, never a decision about what the persona must say."""
    kind = event.get("kind", "unknown")
    if kind == "connection_discovered":
        return "flood"
    # ADR 90: "unknown" carries only a raw id (a39-a44: mail flags, sentinel items), nothing she could react to.
    if kind in ("item_seen", "connection_unlocked", "unknown"):
        return "low"
    return "high"  # first entry/unlock, obtained item/tool, NPC, story/rare, her own activity


# ADR 90: how she gets somewhere or tidies up is not something that happened to her; every other row is.
ACTIVITY_SKIP = frozenset({"inventori", "pergi_ke", "jalan_ke", "langkah", "cek_pagi", "tidur", "tutup_menu",
                           "pegang", "siapkan_pancing"})
# FIX77B: this verdict says only that a press was answered, with no receipt of what it yielded; a bag gain beside it is true and its
# source is not known. A press with its own receipt (a purchase) ends with that receipt's reason and keeps its gain.
BARE_PRESS = "observed_generic_readback"


def social_bag(arg, old, new, names):
    """FIX81B (a59 T7): the bag delta of a talk or gift, from the row's own before/after bag reads; '' when unread."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return ""
    from arti_runtime.fact_names import item_name
    lost = [(q, old[q] - new.get(q, 0)) for q in sorted(old)
            if type(old[q]) is int and type(new.get(q, 0)) is int and old[q] > new.get(q, 0)]
    words = ", ".join("%s -%d" % (item_name(q, names), n) for q, n in lost[:3])
    if isinstance(arg, str) and " ke " in arg:
        return "; keluar dari tas: " + words if lost else "; tas tidak berkurang, hadiah belum terlihat keluar dari tas"
    return "; tas berkurang selama itu, asal belum terverifikasi: " + words if lost else "; tas tidak berkurang"


def row_activity(row, before, after, context):
    """One plain fact for any classified skill row, success or setback; None for movement and bag rows.

    Generic by design (ADR 90): the outcome text is the status relay's own, and gains are the row's native bag
    delta. The fact is verified or says it failed; only the persona decides whether it is worth a word."""
    skill = row.get("skill")
    if not isinstance(skill, str) or skill in ACTIVITY_SKIP:
        return None
    from arti_live7_status_facts import _outcome_text
    from arti_runtime.fact_names import source_names
    row = dict(row, evidence=dict(row.get("evidence") or {}))
    row["evidence"]["names"] = {**(before.get("names") or {}), **source_names(context),
                                **(row["evidence"].get("names") or {})}
    if row.get("verified") is not True and "inventory_gains" not in row["evidence"]:
        from arti_runtime.fact_names import inventory_gains
        row["evidence"]["inventory_gains"] = inventory_gains(before, after, row["evidence"]["names"], row["evidence"].get("chest_moves"))
    text = _outcome_text(row)
    if row.get("verified") is True:
        old, new = before.get("inventory") or {}, after.get("inventory") or {}
        names = {f.get("qualified_item_id"): f.get("name") for f in context.get("inventory", ())
                 if isinstance(f, dict) and isinstance(f.get("name"), str)}
        # ADR 113 (a50 T233 "[recorded words omitted]", T279 Wood/Sap): the delta is the whole window, and a row that names its
        # own item says which gain that was. The others stay stated, as what they are.
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        observed = evidence.get("observed") if isinstance(evidence.get("observed"), dict) else {}
        own = observed.get("item") if isinstance(observed.get("item"), str) else None
        # FIX76A (a55 d19 J1318, T147): a craft's row names its item in words ("wood fence"), so its own Wood Fence read "[recorded remark omitted]".
        own = next((q for q, name in names.items() if own and str(name).casefold() == own.casefold()), own)
        harvest = {}
        for e in observed.get('effects', ()):
            if (isinstance(e, dict) and e.get('kind') == 'harvest_crop' and isinstance(e.get('item'), str)
                    and type(e.get('delta')) is int and e['delta'] > 0):
                harvest[e['item']] = harvest.get(e['item'], 0) + e['delta']
        gained, others = [], []
        bare_press = row.get("reason") == BARE_PRESS
        from arti_runtime.fact_names import moved_into_bag
        into_bag = moved_into_bag(evidence.get("chest_moves"))  # FIX77B: her own stack out of her chest is told as a move, never as a gain
        for q in sorted(new):
            if q in names and type(new[q]) is int and new[q] - old.get(q, 0) - into_bag.get(q, 0) > 0:
                delta = new[q] - old.get(q, 0) - into_bag.get(q, 0)
                if skill == 'panen':
                    attributed = min(delta, harvest.get(q, 0))
                    if attributed:
                        gained.append(f"{names[q]} +{attributed}")
                    if delta > attributed:
                        others.append(f"{names[q]} +{delta - attributed}")
                else:
                    (others if bare_press or (own and q != own) else gained).append(f"{names[q]} +{delta}")
        money_old, money_new = before.get("money"), after.get("money")
        if type(money_old) is int and type(money_new) is int and money_new != money_old:
            gained.append(f"uang {money_new - money_old:+d}g")
        if gained:
            text += "; " + ", ".join(gained[:4])
        if others:
            text += ("; masuk tas selama itu, asal belum terverifikasi: " if bare_press or (skill == 'panen' and not harvest)
                     else "; juga masuk tas selama itu, bukan hasilnya: ") + ", ".join(others[:3])
    if skill == "ngobrol_hadiah":
        text += social_bag(row.get("arg"), before.get("inventory"), after.get("inventory"), row["evidence"]["names"])
    from arti_runtime.fact_names import text as named_text
    return named_text(text, row["evidence"]["names"])[:300]


class MomentSource:
    def __init__(self, folder, journal, *, resume=(), config=None):
        self.config = config or {}
        self.held_intent = None
        self.intent_history = {}
        self.seen_places = set()
        self.folder = folder
        self.journal = journal
        self.lock = threading.Lock()
        self.pending = []
        self.seen = set()
        self.last_at = None
        self.seq = 0
        self.activity_seq = 0
        self.error_reported = False
        # The supervisor carries these journal receipts through a same-day
        # rejoin. The Bridge deletes event files, so they cannot be the ledger.
        for row in resume:
            if row.get("kind") in SITUATIONS and len(row.get("date", ())) == 3:
                self.seen.add((tuple(row["date"]), row["kind"]))
                ts = row.get("ts")
                if type(ts) in (float, int):
                    at = time.monotonic() - max(0.0, time.time() - ts)
                    self.last_at = at if self.last_at is None else max(at, self.last_at)

    def offer(self, kind, date, clock, *, count=None, situation=None):
        if kind not in SITUATIONS or len(date) != 3 or type(clock) is not int:
            return False
        if "{count}" in SITUATIONS[kind] and (type(count) is not int or count < 1):
            return False
        # FIX81B: a kind may carry its own native-fact text (rain_situation); the kind and its once-a-day key stay.
        situation = "[Stardew] " + (situation if isinstance(situation, str) and situation else SITUATIONS[kind].format(count=count))
        if len(situation) > 200:
            return False
        with self.lock:
            now = time.monotonic()
            key = (tuple(date), kind)
            if key in self.seen or self.last_at is not None and now - self.last_at < INTERVAL_S:
                return False
            self.last_at = now
            self.seen.add(key)  # stronger than kind+arg: only one of each kind per day
            payload = dict(schema="arti.stardew_moment/1", ts=time.time(),
                           game_time=f"{str(date[1]).capitalize()} {date[2]} {clock // 100:02}:{clock % 100:02}",
                           kind=kind, situation=situation, ttl_s=TTL_S)
            self.pending.append((payload, list(date)))
        return True

    def error(self, exc):
        with self.lock:
            if self.error_reported:
                return
            self.error_reported = True
        try:
            self.journal(dict(ev="live7_moment_error", outcome_class="FAULT", error=type(exc).__name__))
        except Exception:
            pass  # even a broken journal must never change gameplay

    def discovery(self, event, game_time, *, play_facts=None):
        """Adapter facts in a game-independent event envelope; the relay needs no game interpretation."""
        date=event.get("day_label") or event.get("date")
        text=("Discovery "+str(date)+": " if date else "")+event["text"]
        # Some native display text is an unresolved localization token. Keep
        # the observed kind/subject usable even when source-tag stripping removes it.
        subject = event.get("subject")
        from arti_runtime.fact_names import text as named_text
        text = named_text(text)
        payload = dict(schema="arti.game_event/1", ts=time.time(), game_time=date or game_time,
                       event_time=event.get("event_time") or date or game_time,
                       event_place=event.get("event_place") or (subject if event.get("kind") == "location_entered" else None),
                       kind="discovery", id=event["id"], source="game_adapter",
                       importance=discovery_importance(event),
                       count=event.get("count", 1),
                       situation=text[:360],
                       facts=dict(id=event["id"], kind=event.get("kind", "unknown"),
                                  subject=event.get("subject"), text=event["text"],
                                  source=event.get("source", "unknown"), date=event.get("date"),
                                  event_time=event.get("event_time") or date or game_time,
                                  event_place=event.get("event_place") or (subject if event.get("kind") == "location_entered" else None),
                                  place_name=event.get("place_name"),
                                  detail_ref=event["id"]), ttl_s=TTL_S)
        if play_facts is not None:
            payload["facts"].update({k: v for k, v in play_facts.items() if k != "page"})
            payload["facts"]["reactive_play"] = True
        with self.lock:
            self.pending.append((payload, []))

    def activity(self, text, session_id, skill, date, clock, place):
        """Her own row outcome through the durable discovery envelope (ADR 90); no per-day or interval cap."""
        self.activity_seq += 1  # the wall clock alone repeats within a tick; a resumed session keeps its old ids apart
        self.discovery(dict(id=f"activity:{session_id}:{time.time():.6f}:{self.activity_seq}", kind="activity", subject=skill, text=text,
                            source="live7_row", date=f"Y{date[0]} {date[1]} {date[2]}",
                            event_time=f"{date[0]}:{date[1]}:{date[2]} {clock}", event_place=place),
                       f"{date[0]}:{date[1]}:{date[2]} {clock}")

    def intent(self, why, *, skill=None, arg=None, eta=None, goal_ref=None, page=None, place=None):
        if self.config.get("live7_reactive_play_enabled", False):
            from arti_runtime.reactive_play import is_long
            if place:
                self.seen_places.add(place)
            self.held_intent = dict(niat=why, skill=skill, arg=arg, goal_ref=goal_ref,
                                    long=is_long(skill, eta, self.config), page=page or {})
            if self.held_intent["long"] and self._intent_allowed(self.held_intent, ()):
                self._reactive_event(dict(self.held_intent, kind="own_intent", outcome=[]))
            return
        from arti_runtime.streamer_voice import natural_intent
        line = natural_intent(why)
        if line:
            self.discovery(dict(id=f"intent:{time.time_ns()}", kind="own_intent", text=line), "")

    def _intent_allowed(self, intent, outcome):
        """Same goal OR normalized intent needs a new receipt inside the repeat window."""
        import re
        from arti_runtime.reactive_play import number
        now = time.monotonic()
        window = number(self.config, "live7_intent_repeat_sec", 600)
        normalized = " ".join(re.findall(r"\w+", str(intent.get("niat") or "").casefold()))
        keys = [("text", normalized)] if normalized else []
        if intent.get("goal_ref"):
            keys.append(("goal", str(intent["goal_ref"])))
        facts = {" ".join(line.split()).casefold() for line in outcome}
        prior = [self.intent_history[k] for k in keys if k in self.intent_history
                 and now - self.intent_history[k][0] < window]
        already_seen = set().union(*(p[1] for p in prior))
        if prior and not facts.difference(already_seen):
            return False
        for key in keys:
            previous = self.intent_history.get(key)
            seen = set(previous[1]) if previous and now - previous[0] < window else set()
            self.intent_history[key] = (now, seen | facts)
        self.intent_history = {k: v for k, v in self.intent_history.items() if now - v[0] < window}
        while len(self.intent_history) > 256:
            self.intent_history.pop(next(iter(self.intent_history)))
        return True

    def _reactive_event(self, facts, *, date="", place=None):
        self.discovery(dict(id=f"play:{time.time_ns()}:{self.activity_seq}", kind=facts["kind"],
                            text="; ".join(facts.get("outcome") or [str(facts.get("niat") or facts.get("skill") or "")]),
                            date=date, event_place=place), date, play_facts=facts)

    def reactive_outcome(self, row, before, after, context, session, clock):
        from arti_runtime.reactive_play import outcome_facts, MENU_SKILLS
        intent, self.held_intent = self.held_intent, None
        # An automatic/composite row cannot borrow another skill's intent.
        if intent is None or intent.get("skill") != row.get("skill") or intent.get("arg") != row.get("arg"):
            intent = dict(skill=row.get("skill"), arg=row.get("arg"), long=False)
        # Page facts describe the START location. Only use them at the end if still there.
        page = intent.get("page") if before.get("location") == after.get("location") else None
        lines, notable = outcome_facts(row, before, after, context, self.seen_places, page)
        if (intent.get("long") or row.get("skill") in MENU_SKILLS) and not notable:
            return False
        if not lines or not self._intent_allowed(intent, lines):
            return False
        facts = {**intent, "kind": "own_play", "outcome": lines}
        self.activity_seq += 1
        self._reactive_event(facts, date=str(session.campaign.date), place=after.get("location"))
        return True

    def flush(self, *, enabled=True):
        with self.lock:
            pending, self.pending = self.pending, []
        for payload, date in pending:
            temporary = None
            try:
                discovery = payload["schema"] == "arti.game_event/1"
                if not discovery and (not enabled or time.time() >= payload["ts"] + payload["ttl_s"]):
                    continue
                folder = Path(self.folder)
                folder.mkdir(parents=True, exist_ok=True)
                self.seq += 1
                target = folder / f"ev-{int(payload['ts'] * 1000)}-{os.getpid() * 1000000 + self.seq}.json"
                ipc.write_json(target, payload)
                self.journal(dict(ev="live7_moment_emitted", kind=payload["kind"], date=date,
                                  ts=payload["ts"], game_time=payload["game_time"],
                                  situation_len=len(payload["situation"]),
                                  situation_sha256=hashlib.sha256(payload["situation"].encode("utf-8")).hexdigest()))
            except Exception as exc:
                if payload["schema"] == "arti.game_event/1":
                    with self.lock:
                        self.pending.append((payload, date))
                self.error(exc)
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except Exception as exc:
                        self.error(exc)


def row_moments(row, before, after, context):
    """Interpret an already classified row; never upgrade an outcome or reread the game."""
    if row.get("verified") is not True or row.get("outcome_class") != "VERIFIED":
        return
    observed = row["evidence"]["observed"]
    skill = row["skill"]
    old, new = before.get("inventory", {}), after.get("inventory", {})
    if skill == "beli" and observed.get("objective") == "economy_buy":
        seeds = {f["qualified_item_id"] for f in context.get("inventory", ()) if f.get("category") == -74}
        count = sum(max(0, new.get(q, 0) - old.get(q, 0)) for q in seeds)
        if count > 0 and count == observed.get("done"):
            yield "bought_seeds", count
    elif skill in ("tanam", "panen"):
        effect_kind, sign, kind = (("place_item", -1, "planted") if skill == "tanam" else
                                   ("harvest_crop", 1, "harvested"))
        count = 0
        for effect in observed.get("effects", ()):
            q, delta = effect.get("item"), effect.get("delta")
            if (effect.get("kind") == effect_kind and type(delta) is int and delta * sign > 0
                    and (new.get(q, 0) - old.get(q, 0)) * sign >= delta * sign):
                count += delta * sign
        if count > 0:
            yield kind, count
    elif skill == "siram":
        from arti_stardew_tool_work import live_crop
        targets = context.get("targets", {}).values()
        crops = [f["crop"] for f in targets if live_crop(f)]
        if (context.get("scene_complete") and crops and all(c.get("watered") is True for c in crops)
                and any(f.get("kind") == "location" and f.get("location_id") == after.get("location") for f in targets)):
            yield "watered_all", None
    elif (skill == "pergi_ke" and after.get("location") == "Town"
          and isinstance(before.get("location"), str) and before["location"] != "Town"):
        yield "went_to_town", None
    elif skill == "tidur":
        yield "bed", None  # the caller already enforced the bed + fresh morning verifier


class Live7MomentsMixin:
    def _live7_intent_moment(self, session, why, *, skill=None, arg=None, eta=None, goal_ref=None, page=None, place=None):
        if not self._config.get("live7_own_play_enabled", False):
            return
        try:
            folder = self._config.get("stardew_live7_help_dir") or self._config.get("live7_help_dir")
            if not folder:
                return
            source = getattr(self, "_live7_moment_source", None)
            if source is None:
                source = self._live7_moment_source = MomentSource(folder, self._journal, config=self._config)
            source.intent(why, skill=skill, arg=arg, eta=eta, goal_ref=goal_ref, page=page, place=place)
            if self._config.get("live7_reactive_play_enabled"):
                source.flush()  # mind-decision hook is outside the runtime lock, before the chosen action
        except Exception:
            pass  # speech must never affect her decisions or actions

    def _live7_discovery_event(self, session, event):
        folder = self._config.get("stardew_live7_help_dir") or self._config.get("live7_help_dir")
        if not folder:
            return
        source = getattr(self, "_live7_moment_source", None)
        if source is None:
            source = self._live7_moment_source = MomentSource(folder, self._journal, config=self._config)
        source.discovery(event, str(session.campaign.date))

    def _live7_moments(self, event, session, motor, **evidence):
        if self._config.get("live7_moments_enabled") is not True:
            return
        source = None
        try:
            with self._lock:
                source = getattr(self, "_live7_moment_source", None)
                if source is None:
                    source = self._live7_moment_source = MomentSource(
                        self._config.get("stardew_live7_help_dir") or self._config.get("live7_help_dir"), self._journal,
                        resume=(self._config.get("stardew_live7_resume") or {}).get("moments", ()), config=self._config)
                date, clock = session.campaign.date, motor.time_of_day
                if event == "day":
                    from arti_stardew_live7 import BAD_WEATHER, seed_shop_closed
                    route = (evidence["context"].get("mines_ui") or {}).get("goal_route") or {}
                    weather = route.get("weather")
                    if weather in BAD_WEATHER or route.get("is_raining") is True:
                        memory = getattr(getattr(session.campaign, "live", None), "crop_memory", None)
                        source.offer("rain_day", date, clock, situation=rain_situation(getattr(memory, "crops", None), date))
                    weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[(date[2] - 1) % 7]
                    # Only a closure she saw or remembers (smoke 2026-10-23 item 11), never rule-table foresight.
                    memory = getattr(getattr(session.campaign, "live", None), "shop_memory", None)
                    if seed_shop_closed(date, weekday, evidence["context"], clock, memory) is not None:
                        source.offer("shop_closed", date, clock)
                elif event == "row":
                    if self._config.get("live7_reactive_play_enabled") and self._config.get("live7_own_play_enabled"):
                        source.reactive_outcome(evidence["row"], evidence["before"], evidence["after"] or {},
                                                evidence["context"] or {}, session, clock)
                        return
                    offered = False
                    for kind, count in row_moments(evidence["row"], evidence["before"],
                                                   evidence["after"] or {}, evidence["context"] or {}):
                        if kind == "bed":
                            clock = evidence["row"]["evidence"]["observed"]["bed_time"]
                        offered = source.offer(kind, date, clock, count=count) or offered
                    if not offered and (self._config.get("live7_activity_events_enabled") is True
                                        or self._config.get("live7_own_play_enabled") is True):
                        after = evidence["after"] or {}
                        text = row_activity(evidence["row"], evidence["before"], after, evidence["context"] or {})
                        if text:
                            source.activity(text, session.session_id, evidence["row"]["skill"], date, clock,
                                            after.get("location"))
                elif event == "loop":
                    source.offer("stuck", date, clock)
        except Exception as exc:
            if source is not None:
                source.error(exc)
            elif not getattr(self, "_live7_moment_init_error", False):
                self._live7_moment_init_error = True
                try:
                    self._journal(dict(ev="live7_moment_error", outcome_class="FAULT", error=type(exc).__name__))
                except Exception:
                    pass

    def _live7_flush_moments(self):
        source = getattr(self, "_live7_moment_source", None)
        if source is not None:
            source.flush(enabled=self._config.get("live7_moments_enabled") is True)
