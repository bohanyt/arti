"""Shared bounded Wave 1 campaign lifecycle and Part A farm policy.

Parts B/C supply task/travel/readback policy without copying this lifecycle.

Offline tests are UNIT DOUBLES, never a three-day live acceptance receipt.
"""
from __future__ import annotations

from arti_runtime import wait_ledger as W

from dataclasses import dataclass, field, replace
from collections.abc import Mapping
import time
from arti_stardew_pause import game_now, wait_resume

import arti_stardew_skills as S

GOAL = ("Arti, main mandiri selama tiga hari: rawat tanaman dan semua hewan, simpan hasilnya di peti, "
        "kumpulkan sedikit kayu dan batu kalau aman, makan bila perlu, lalu pulang tidur sebelum tengah malam.")
NAMES = frozenset({"pergi_ke", "siram", "isi_gembor", "rawat_hewan", "simpan_ke_chest", "makan",
                   "tebang", "pecah_batu", "pungut_drop", "bersihkan", "mesin", "tidur", "panen"})
LIMITS = dict(seconds=5400, input=900, skills=24, provider=24, steps=4096)
# Owner 2026-10-17 (live a24: three farm days projected ~1,200 physical inputs after waste cuts): the Part A
# input cap is 1,300. Owner 2026-10-18 ("[recorded remark omitted]"): Part B is 1,300 too.
# Owner 2026-10-19 after host1 a36 (Day 3 crops/food/storage held sleep_reserved at 1194/1300): Part A 1,500.
# Owner 2026-10-20: Part C 1,300; keep reserves measured against their original caps.
# Every other limit is shared and unchanged.
CAMPAIGN_INPUT = {"a": 1500, "b": 1300, "c": 1300}
# The sleep/row reserves were measured live under a 1,300-input cap; only a smaller approved cap scales them.
RESERVE_MEASURED_INPUT = {"a": 1300, "b": 1300, "c": 900}


def limits_for(part):
    return dict(LIMITS, input=CAMPAIGN_INPUT[part])
# A23 (journal 2566-2616; report step 176): pet Dudley stood on ARTI's own Farm tile 51,10; the native
# collision grid marks that cell '#', so open_paths() refused the start: the machine waypoint vanished
# (no_observed_route_or_stand) and clear/wood/stone/storage were held native_grid_unavailable in one tight
# loop on the same pose. Grid-shaped travel holds now wait for a few FRESH native ui.context reads first.
TRAVEL_REREAD_REASONS = (":native_grid_unavailable", ":fresh_context_required", ":no_observed_route_or_stand",
                         # PART_B1: Parts B/C read native goal_route exits (actor tile + clock must match the
                         # motor); one minute tick between reads is the same transient, never an unmet row.
                         ":fresh_native_exits_required")
TRAVEL_REREADS = 4
TRAVEL_REREAD_SECONDS = 3.0
_NO_CLEANUP = object()  # _part_a_skip_clean_row runs its own end-of-row cleanup
WALK_REINSPECTS = 2
# A25 input budget (journal: D3 home leg from the Barn hit 1300/1300 at Farm 41,21 after 63 inputs).
# Each day's sleep keeps SLEEP_RESERVE inputs: worst home leg = Barn door -> house (~115) + tidur (~11).
# A row may start only if the remaining session inputs cover the reserve plus its own measured
# a24/a25 maximum (optional rows / the animal revisit); no non-sleep row may spend into the reserve.
SLEEP_RESERVE = 140
ROW_INPUTS = {"stone": 45, "machine": 80, "wood": 50, "clear": 30, "food": 15}
REVISIT_INPUTS = 60
SLEEP_RESERVED = "part_a_budget:sleep_reserved"
# PART_B4: row-local, released travel stalls after which the Part B sleep row re-derives its route once.
SLEEP_TRAVEL_RETRY_REASONS = ("stalled:repeated_input_without_physical_change", "stalled:no_progress",
                              "stalled:no_continuation", "exit_not_offered")
# A25 D3: the trough debt was revisited after storage (chest -> Barn -> house, ~150 inputs).
# Revisit next, from the Barn door, after a short read-only wait for the blocking animal to move.
ANIMAL_REVISIT_WAIT_SECONDS = 6.0


def machine_input(context, food_keep):
    """Narrow the native cheap-food list to allowed jar crops; keep every possible S5 meal."""
    from arti_stardew_economy import DEFAULT_ALLOWLIST
    if not context.get("inventory_complete"):
        return None
    bag = context.get("inventory", ())
    candidates = []
    for food in context.get("machine_food", ()):
        qid = food.get("qualified_item_id")
        # Crop/fruit/forage subset only; native drop-in still proves compatibility and policy.
        if (qid in DEFAULT_ALLOWLIST and type(food.get("price")) is int and 0 < food["price"] <= 50
                and type(food.get("stack")) is int and food["stack"] > food_keep
                and any(f.get("occupied") is True and f.get("qualified_item_id") == qid
                        and f.get("slot") == food.get("slot") and f.get("stack") == food["stack"]
                        and f.get("quality") == 0 and f.get("category") in (-75, -79, -81, -80)
                        for f in bag)):
            candidates.append(food)
    return min(candidates, key=lambda f: (f["price"], f["slot"])) if candidates else None


def unpublished_placeholder(action):
    """The approach refusal placeholder recorded before any input publication."""
    from types import SimpleNamespace
    from arti_stardew_animal_routine import unpublished_replan
    fields = ("lifecycle", "request_id", "outcome", "cleanup_confirmed", "reason")
    return unpublished_replan(SimpleNamespace(**{k: S._g(action, k) for k in fields}))


def unpublished_budget_refusal(action):
    """Exhausted approach: no publication, no retry or allowance renewal."""
    return (action is not None and S._g(action, "lifecycle") == "not_taken"
        and S._g(action, "reason") == "approach_budget_exhausted"
        and S._g(action, "request_id") is None and S._g(action, "outcome") is None
        and S._g(action, "cleanup_confirmed") is True
        and S._g(action, "world_effect_status") in ("uncertain", "zero_effect")
        and not S._g(action, "observed_changes"))


CROP_ENROLL_MAX = 4  # PLAN Â§2: 1-4 enrolled crop tiles
RESOURCE_TRAVEL_LEGS = 8


def resource_fact(fact, task):
    """A native fact that is a current mature tree (wood) or a Stone object (stone)."""
    if not isinstance(fact, Mapping):
        return False
    if task == "wood":
        wild = fact.get("wild_tree") or {}
        return (fact.get("kind") == "terrain" and fact.get("terrain_type") == "Tree" and wild.get("stump") is not True
                and wild.get("tapped") is not True
                and type(wild.get("growth_stage")) is int and wild["growth_stage"] >= 5)
    return fact.get("kind") == "object" and fact.get("name") == "Stone"


def wood_fall_stands(grid, tree):
    """Adjacent open stands with an observed retreat out of the trunk/debris row.

    A side stand makes the trunk fall away; north/south stands allow either side.
    Keep an escape to two rows north/south of the tree, without crossing the
    trunk row. This local refuge replaces A18's nine-cell, both-sides drop-pickup
    corridor. Drops still require their own native route and inventory proof.
    None means a possible stand/retreat is unobserved, never work authority.
    """
    if not grid or tree is None:
        return None
    from arti_stardew_native_targets import adjacent
    tx, ty = tree
    out, unknown = set(), False

    def escape(stand, allowed):
        if grid.get(stand) not in allowed:
            return False
        seen, todo = {stand}, [stand]
        for tile in todo:
            if abs(tile[1]-ty) >= 2:
                return True
            for step in adjacent(tile):
                if (step not in seen and step[1] != ty
                        and abs(step[0]-stand[0])+abs(step[1]-stand[1]) <= 3
                        and grid.get(step) in allowed):
                    seen.add(step)
                    todo.append(step)
        return False

    for stand in adjacent(tree):
        if escape(stand, (".",)):
            out.add(stand)
        elif escape(stand, (".", None)):
            unknown = True
    return out if out or not unknown else None


def wood_search_stands(grid, tree, facts):
    """Inspected occupancy can veto an unseen stand/escape, never prove it walkable.

    Current cells supersede older discovery facts (a verified clear may remove them).
    """
    from arti_stardew_debris_route import footprint
    discovery = dict(grid)
    for fact in facts:
        if (not isinstance(fact, Mapping) or fact.get('kind') == 'location'
                or fact.get('terrain_type') in ('Grass', 'HoeDirt')):
            continue
        # Small sprouts are walkable; mature trees and other occupants veto.
        stage = (fact.get('wild_tree') or {}).get('growth_stage')
        if fact.get('terrain_type') == 'Tree' and type(stage) is int and stage < 3:
            continue
        for tile in footprint(fact):
            discovery.setdefault(tile, '#')
    return wood_fall_stands(discovery, tree)


def recognizes_goal(text, goal):
    """Exact authenticated acceptance intent; no substring grants."""
    import re
    t = re.sub(r"^arti[\s,:]+", "", (text or "").strip(), flags=re.I)
    t = re.sub(r"^main mandiri\s*:\s*", "", t, flags=re.I)
    expected = goal.split("Arti, ", 1)[1]
    return t.rstrip(".").casefold() in (expected.rstrip(".").casefold(),
        expected.split("main mandiri ", 1)[1].rstrip(".").casefold())


def recognizes(text):
    return recognizes_goal(text, GOAL)


def next_date(date):
    year, season, day = date
    seasons = ("spring", "summer", "fall", "winter")
    if day < 28:
        return year, season, day + 1
    i = seasons.index(season)
    return year + (i == 3), seasons[(i + 1) % 4], 1


@dataclass
class Campaign:
    owner_goal: str
    date: tuple
    reserve: dict
    epoch: tuple
    part: str = "a"
    days: list = field(default_factory=list)
    tasks: list = field(default_factory=list)
    task: str | None = None
    legs: list = field(default_factory=list)
    starts: int = 0
    finished: bool = False
    crops: tuple = ()
    travel: dict = field(default_factory=dict)
    baseline_units_deposited: dict = field(default_factory=dict)
    animal_deferred: dict = field(default_factory=dict)
    animal_revisit: bool = False
    preface: dict | None = None
    machine_cycle: list = field(default_factory=list)
    shop_wait_started: float | None = None
    mines_wait: dict | None = None  # Part B lobby wait; re-polled by the boundary, never under a held lock
    mine_recovery_failed: str | None = None  # B6: one failed work-row escape sends remaining work to curfew
    farm_entry: bool = False
    native_targets: dict = field(default_factory=dict)
    storage_verified: set = field(default_factory=set)
    storage_rejected: set = field(default_factory=set)
    machine_busy: dict | None = None
    machine_scan_day: int = 0
    live: object | None = None
    live_limits: tuple | None = None

    def morning(self):
        if self.part == "live":
            from arti_stardew_live7 import DayPolicy
            learned = getattr(self.live, "shop_refused", None) or {}
            ground = getattr(self.live, "ground_items", None)
            chests = getattr(self.live, "chest_memory", None)
            forage = getattr(self.live, "forage_memory", None)
            seen = getattr(self.live, "seen_objects", None)
            cc_seen = getattr(self.live, "cc_seen", None)
            names = getattr(self.live, "item_names", {})
            totals = getattr(self.live, "catch_totals", {})
            kinds = getattr(self.live, "catch_kinds", {})
            trips = getattr(self.live, "last_trips", {})
            mines_encounter = getattr(self.live, "mines_last_encounter", None)
            self.live = DayPolicy()
            self.live.mines_last_encounter = mines_encounter  # ADR 169: witnessed encounter, never a predicted floor
            self.live.shop_refused = {k: set(v) for k, v in learned.items()}  # fix10e: a shop's rule outlives the day
            if ground is not None:
                self.live.ground_items = ground  # fix59A (ADR 86): an item on the ground outlives the day
            self.live.catch_totals = totals  # FIX71C: what she has landed, by place, outlives it too
            self.live.catch_kinds = kinds  # FIX73D: and by kind, so a run-wide ordinal survives the day
            if chests is not None:
                self.live.chest_memory = chests  # FIX72D: what she put away outlives the day too
            if forage is not None:
                self.live.forage_memory = forage  # FIX73E: where she has seen forage outlives the day too
            if seen is not None:
                self.live.seen_objects = seen  # FIX77C (ADR 136): the game re-places a lost item on each warp; a sighting outlives the day
            self.live.cc_seen = cc_seen  # FIX75B (ADR 129): the Community Center state she last saw in full outlives the day
            self.live.item_names = names  # FIX72D: and the names she has read, for things no longer in her hands
            self.live.last_trips = trips  # measured trip facts outlive the day, not the campaign identity
            self.days.append(dict(date=list(self.date), rows={}, skills=[], transition=None))
            self.tasks = []  # the model chooses from fresh offers at every boundary
            return
        if self.part in ("b", "c"):
            if self.part == "c":
                from arti_stardew_part_c import TASKS
                order = list(TASKS)
            else:
                from arti_stardew_part_b import TASKS, schedule
                order = schedule()  # PART_B_MINES_PLAN: ORDER's rows, unscheduled rows named, then sleep
                self.native_targets.pop("forage_onway", None)  # sightings belong to this native day only
            self.days.append(dict(date=list(self.date), rows={t: False for t in TASKS}, skills=[], transition=None))
            self.tasks = order
            return
        self.animal_deferred = {}
        self.animal_revisit = False
        self.farm_entry = False
        self.days.append(dict(date=list(self.date), rows={}, skills=[], transition=None))
        self.tasks = ["farm", "animals", "crops", "food"]
        if len(self.days) == 1:
            # A24 input budget: Day 1 walked house->Coop->plot->machine->house->Stone(36,38)->chest
            # (~218 tiles, ~1.45 inputs/tile). Follow the farm instead: the nearest observed rock is
            # by the Coop/Barn, so stone right after animals, then the plot by the house, machine 43,9,
            # wood/clear by the house, chest 55,22 (~157 tiles). Animals stay first (A2).
            self.tasks = ["farm", "animals", "stone", "crops", "food", "machine", "wood", "clear"]
        elif self.machine_cycle and not any(e["kind"] == "collect" for e in self.machine_cycle):
            self.tasks += ["machine"]
        elif self.machine_busy and not any(d["rows"].get("machine") for d in self.days[:-1]):
            # PROOF_A2: Day 1's only machine was processing (native held item, minutes > 0), so neither a
            # collect nor an insert existed. Retry the row on a later day; never credit the busy read.
            self.tasks += ["machine"]
        if len(self.days) > 1 and not any(d["rows"].get("wood") for d in self.days[:-1]):
            self.tasks += ["wood"]  # A20: PLAN needs one tree felled across the run, not on Day 1 only
        self.tasks += ["food", "storage", "sleep"]

    def report(self):
        return dict(owner_goal=self.owner_goal, starts=self.starts, crops=self.crops, days=self.days,
                    baseline_units_deposited=dict(self.baseline_units_deposited), machine_cycle=list(self.machine_cycle),
                    **({"fourth_morning": self.finished} if self.part == "a" else {"next_morning": self.finished, "part": self.part}), **({"preface": dict(self.preface)} if self.preface is not None else {}))


class PartACampaignMixin:
    def _part_a_begin(self, session):
        if session.mode == "part_live":
            return self._live7_begin(session)
        facts = self._skill_current_facts()
        binding, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        date = self._date_tuple(motor) if motor is not None else None
        if (binding.status != "bound" or pid is None or facts is None or not facts.inventory_current or date is None
                or type(motor.time_of_day) is not int or not 0 <= motor.time_of_day < 2400
                or not facts.player_free or self._state.is_stale(pid) or self._active is None):
            return self._skill_end_locked(session, "held", "partial", "part_a_normal_morning_required")
        session.campaign = Campaign(session.goal_text, date, dict(facts.inventory), tuple(self._active.play_identity),
                                    part=session.mode.removeprefix("part_"))
        if 600 <= motor.time_of_day < 700:
            session.campaign.morning()
        else:
            # Host loading/join time can consume the morning. S1 earns a fresh
            # Day 1 within this same session and its original campaign caps.
            session.campaign.preface = dict(start_time=motor.time_of_day, sleep_verified=False, transition=None)
            session.campaign.tasks = ["sleep"]
            if session.campaign.part == "c":
                # Owner 2026-10-20 FINAL Part C: S29 cek_pagi on the approval (Wednesday) morning, before the
                # preface sleep. Its row lives on the preface record and counts in the Part C result.
                from arti_stardew_part_c import PREFACE_TASKS
                session.campaign.preface.update(rows={"morning": False}, skills=[])
                session.campaign.tasks = list(PREFACE_TASKS)
        self._journal({"ev": "part_" + session.campaign.part + "_started", "session_id": session.session_id,
                       "owner_goal_id": session.goal_id, "date": date, "reserve": facts.inventory})
        return self._part_a_advance(session)

    def _part_a_advance(self, session):
        if session.mode == "part_live":
            return self._live7_advance(session)
        c = session.campaign
        if c is None:
            return self._part_a_begin(session)
        if c.finished and c.part in ("b", "c"):
            if c.part == "c":
                from arti_stardew_part_c import unmet, final_reason
                gaps = unmet(c)  # the preface S29 row and the progress day's rows
            else:
                from arti_stardew_part_b import TASKS
                gaps = [k for k in TASKS if not c.days[0]["rows"].get(k)]
            reason = (final_reason(c) if c.part == "c" else "part_b_unmet:" + ",".join(gaps)) if gaps else (
                "part_" + c.part + "_verified")
            return self._skill_end_locked(session, "held" if gaps else "finished",
                                          "partial" if gaps else "achieved", reason)
        if c.finished:
            # Acceptance rows require independent readback, never only a model finish.
            gaps = [f"day{i+1}:{k}" for i, day in enumerate(c.days)
                    for k in ("crops", "animals", "storage", "sleep") if not day["rows"].get(k)]
            for name in ("wood", "stone", "clear", "machine"):
                if not any(d["rows"].get(name) for d in c.days):
                    gaps.append(name)
            if not any(d["rows"].get("food") for d in c.days):
                gaps.append("food_consumption")
            return self._skill_end_locked(session, "held" if gaps else "finished",
                "partial" if gaps else "achieved", "part_a_unmet:" + ",".join(gaps) if gaps else "part_a_verified")
        while c.tasks:
            if c.part == "a" and self._part_a_inputs_exhausted():
                # A24: never start (or blame) a row once the PLAN physical-input cap is spent.
                return self._skill_end_locked(session, "held", "partial", "part_a_budget:inputs")
            settle = getattr(self, "skill_found_settle", None)
            if settle is not None:
                # PROOF_A2 (journal 962-971): ARTI's own Geode hold-up (cannot move, no menu) then its "You
                # found" box arrived AFTER the stone row's cleanup; crops/machine/wood were read inside it.
                settle()
            task = c.tasks.pop(0)
            if c.part == "a" and task == "clear" and c.days[-1]["rows"].get("clear"):
                continue  # Same native debris-removal witness already settled during route repair.
            facts = self._skill_current_facts()
            if facts is None or not facts.inventory_current:
                return self._skill_end_locked(session, "held", "partial", "part_a_fresh_facts_required")
            _, pid = self._binding_view()
            motor = self._state.latest(pid)
            if (self._state.is_stale(pid) or self._date_tuple(motor) != c.date
                    or tuple(self._active.play_identity) != c.epoch):
                return self._skill_end_locked(session, "held", "partial", "part_a_epoch_or_date_changed")
            if (motor.time_of_day >= (2100 if c.part in ("b", "c") else 2300)
                    or c.part == "b" and c.mine_recovery_failed) and task != "sleep":
                # Preserve unmet rows, go home early, never call curfew rescue success.
                if c.part in ("b", "c"):
                    for skipped in [task] + [t for t in c.tasks if t != "sleep"]:
                        reason = "mine_recovery_failed:" + c.mine_recovery_failed if c.mine_recovery_failed else "curfew"
                        if c.part == "b":
                            from arti_stardew_part_b import ORDER, NOT_SCHEDULED
                            reason = reason if skipped in ORDER else NOT_SCHEDULED
                        row = dict(task=skipped, verified=False, reason=reason, evidence=dict(time=motor.time_of_day))
                        c.days[-1]["skills"].append(row)
                        self._journal(dict(ev="part_" + c.part + "_row", session_id=session.session_id,
                                           owner_goal_id=session.goal_id, day=1, **row))
                c.tasks = ["sleep"]
                continue
            if c.part == "c" and task != "sleep":
                # PART_C_CLOCK: the 21:00 rescue above stays the backstop; before it, no row starts whose measured
                # projection misses its native shop hours or puts bed at/after 24:00. A clean failure, never a waiver.
                from arti_stardew_part_c import admission
                held, clock = admission(task, facts.location, motor.time_of_day)
                self._journal(dict(ev="part_c_clock", session_id=session.session_id, day=1, task=task,
                                   location=facts.location, time=motor.time_of_day, projection=clock,
                                   admitted=held is None, reason=held))
                if held:
                    c.task = task
                    if self._part_a_skip_clean_row(session, held, deterministic=True):
                        continue
                    return self._skill_end_locked(session, "held", "partial", held)
            if c.part == "b" and task == "buy" and motor.time_of_day < 900 and c.shop_wait_started is None:
                from arti_stardew_part_b import forage_in_wait
                if forage_in_wait(self, session, motor.time_of_day):
                    task = "forage"  # owner 2026-10-20: S21 uses the idle pre-09:00 time, same row bounds
            if c.part == "b" and task == "buy" and motor.time_of_day < 900:
                # Multiplayer clock continues while everyone is awake. Wait at
                # the settled Town row, with no model/input/start/cap renewal.
                if c.shop_wait_started is None:
                    c.shop_wait_started = game_now(self)
                    self._journal(dict(ev="part_b_shop_wait", session_id=session.session_id, until=900))
                if game_now(self)-c.shop_wait_started < 180 and not self._action_cancel.is_set():
                    from arti_stardew_runtime import OwnerTurnResult
                    c.tasks.insert(0, task)
                    session.current = None
                    return OwnerTurnResult(admitted=True, applied=True, skip_reply_tags=True, reason="part_b_shop_wait")
                # A frozen clock does not consume the whole campaign.
                c.task = task
                if self._part_a_skip_clean_row(session, "part_b_shop_wait_expired"):
                    continue
                return self._skill_end_locked(session, "held", "partial", "part_b_shop_wait_unclean")
            if c.part == "b" and task != "sleep":
                from arti_stardew_part_b import ORDER, NOT_SCHEDULED
                if task not in ORDER:
                    # PART_B_MINES_PLAN: measured clock pace leaves no time for this row on a Mines day.
                    c.task = task
                    if self._part_a_skip_clean_row(session, NOT_SCHEDULED, deterministic=True):
                        continue
                    return self._skill_end_locked(session, "held", "partial", NOT_SCHEDULED)
            if task == "animals" and c.animal_deferred:
                debt = c.animal_deferred
                if game_now(self) >= debt["deadline"] or any(cap <= 0 for cap in debt["caps"]):
                    c.days[-1]["skills"].append(dict(task=task, status="held", reason="animal_budget_exhausted"))
                    continue  # no renewed S3 allowance; keep the row unverified and go home
            c.task = task
            if task in ("animals", "crops") and c.crops and facts.location != "Farm":
                # A19 (journal 1164/3304): rawat_hewan ended inside the Barn/Coop and the Farm-only
                # native soil read returned nothing. Off-Farm absence proves nothing: keep the pinned
                # enrollment; crops travel to Farm first and the post-travel native read re-checks it.
                crop_state = None
            elif task in ("animals", "crops"):
                # Enroll on Farm before livestock can leave/hold. This native
                # crop/soil scan is independent of the actor's nearby scene.
                crop_state = self._part_a_soil()
                rereads = 0
                while (not crop_state and rereads < TRAVEL_REREADS and self._part_a_await_fresh_context()):
                    # PROOF_A2: an empty read is "no current native read" (no movement pin while ARTI's
                    # farmer was held), never proof that the enrolled plot changed. Bounded, read-only.
                    rereads += 1
                    crop_state = self._part_a_soil()
                if not c.crops and 1 <= len(crop_state) <= 4:
                    c.crops = tuple(sorted(crop_state))
                if (not c.crops or set(crop_state) != set(c.crops)
                        or any(v not in (True, False) for v in crop_state.values())):
                    reason = "part_a_crop_enrollment_unavailable"
                    self._journal({"ev": "part_a_crop_enrollment_check", "session_id": session.session_id,
                                   "day": len(c.days), "task": task, "rereads": rereads,
                                   "enrolled": [list(t) for t in c.crops],
                                   "read": {f"{k[0]},{k[1]}": v for k, v in crop_state.items()},
                                   "lost": [list(t) for t in c.days[-1].get("crops_lost", ())]})
                    if self._part_a_skip_clean_row(session, reason):
                        continue
                    return self._skill_end_locked(session, "held", "partial", reason)
                c.days[-1]["crop_before"] = {str(k): v for k, v in crop_state.items()}
            calls = self._part_a_calls(task, facts)
            if isinstance(calls, str):
                if self._part_a_skip_clean_row(session, calls, deterministic=True):
                    continue
                return self._skill_end_locked(session, "held", "partial", calls)
            if not calls:
                if task == "machine":
                    c.days[-1]["rows"][task] = False
                    reason = "machine_pending_collect_not_offered"
                    c.days[-1]["skills"].append(dict(task=task, verified=False, status="held", reason=reason))
                    self._journal({"ev": "part_" + session.campaign.part + "_row", "session_id": session.session_id, "owner_goal_id": session.goal_id,
                                   "day": len(c.days), "task": task, "verified": False, "status": "held", "reason": reason,
                                   "evidence": dict(machine_cycle=list(c.machine_cycle))})
                else:
                    c.days[-1]["skills"].append(dict(task=task, status="not_applicable"))
                continue
            if c.starts >= 24:
                return self._skill_end_locked(session, "held", "partial", "part_a_skill_limit")
            if c.part in ("a", "b") and task != "sleep":
                remaining = self._part_a_inputs_remaining()
                reserve, reserved, row_inputs = self._campaign_sleep_reserve(c.part)
                extra = row_inputs.get(task, 0)
                if c.part == "b":
                    from arti_stardew_part_b import row_inputs as part_b_row_inputs
                    extra = part_b_row_inputs(task, facts.location)  # the Mine trek is charged once
                need = self._part_a_reserve(reserve + (REVISIT_INPUTS if task == "animals" and c.animal_deferred
                                                       else extra), c.part)
                if remaining is not None and remaining < need:
                    self._journal({"ev": "part_" + c.part + "_sleep_reserve", "session_id": session.session_id,
                                   "day": len(c.days), "task": task, "remaining": remaining, "need": need})
                    if self._part_a_skip_clean_row(session, reserved, deterministic=True):
                        continue
                    return self._skill_end_locked(session, "held", "partial", reserved)
                if task == "animals" and c.animal_deferred and ANIMAL_REVISIT_WAIT_SECONDS > 0:
                    cancel = getattr(self, "_action_cancel", None)
                    if cancel is not None and W.call('poll', 111, cancel.wait, ANIMAL_REVISIT_WAIT_SECONDS):
                        return self._skill_end_locked(session, "held", "partial", "part_a_cancelled")
            if c.part == "a" and task == "farm" and c.crops:
                # A20: farm entry is a deterministic travel leg. Once the plot is enrolled it folds
                # into the next started task (one start fewer per day, never a brain call).
                c.farm_entry = True
                self._journal({"ev": "part_a_brain_call", "session_id": session.session_id, "day": len(c.days),
                               "task": task, "brain_call": "skipped_deterministic", "folded_into_next_start": True})
                continue
            if c.part == "a" and task != "sleep" and c.starts >= LIMITS["skills"]-1:
                # The last start always belongs to the day's sleep: never trade the night for a row.
                if self._part_a_skip_clean_row(session, "part_a_start_reserved_for_sleep", deterministic=True):
                    continue
                return self._skill_end_locked(session, "held", "partial", "part_a_start_reserved_for_sleep")
            c.task, c.legs = task, list(calls[1:])
            c.travel = {}
            if c.part in ("a", "b") and task != "sleep" and (remaining := self._part_a_inputs_remaining()) is not None:
                c.travel["input_ceiling"] = max(0, remaining - self._part_a_reserve(
                    self._campaign_sleep_reserve(c.part)[0], c.part))
            if c.part == "b" and task in ("mines", "ore"):
                from arti_stardew_part_b import mines_exit_due, ore_search_due
                c.travel["mines_exit_due"] = mines_exit_due()
                if task == "ore" and ore_search_due() is not None:
                    c.travel["ore_search_due"] = ore_search_due()
            if task == "animals" and c.animal_deferred:
                c.travel["work_caps"] = c.animal_deferred["caps"]
            if task == "storage":
                c.travel["work_caps"] = tuple(sum(S.skill_costs(call)[i] for call in calls) for i in range(3))
            session.plan = list(calls)
            session.index = 1
            # A20: settle travel and preconditions BEFORE any brain call. A row already known to be
            # unavailable is recorded without asking the model (journal: brain_call skipped_deterministic).
            call = calls[0]
            travel = self._part_a_travel_call(task, facts)
            rereads = 0
            while (isinstance(travel, str) and travel.endswith(TRAVEL_REREAD_REASONS)
                   and rereads < TRAVEL_REREADS and self._part_a_await_fresh_context()):
                # Bounded re-read: only a newer native ui.context (grid) may settle the row; no cell invented.
                rereads += 1
                facts = self._skill_current_facts() or facts
                travel = self._part_a_travel_call(task, facts)
            if rereads:
                self._journal({"ev": "part_a_travel_reread", "session_id": session.session_id, "day": len(c.days),
                               "task": task, "reads": rereads,
                               "result": travel if isinstance(travel, str) else "route" if travel else "at_stand"})
            if (travel is None and c.part == "a" and c.farm_entry and task != "sleep"
                    and facts.location != "Farm"):
                travel = S.SkillCall("pergi_ke", "farm")  # the folded farm-entry leg
            if c.part == "b" and isinstance(travel, str):
                from arti_stardew_part_b import MINES_WAITING
                if travel == MINES_WAITING:
                    # Already in the Mine lobby before it opens: the shop wait's re-entrant hold.
                    # The boundary re-polls without the runtime lock; no start/brain call/input.
                    from arti_stardew_runtime import OwnerTurnResult
                    c.tasks.insert(0, task)
                    session.current = None
                    return OwnerTurnResult(admitted=True, applied=True, skip_reply_tags=True, reason="part_b_mines_wait")
            if isinstance(travel, str):
                cleanup = _NO_CLEANUP
                if c.part == "b" and task == "forage":
                    # One end-of-row cleanup serves both the deferral check and a refused deferral's skip.
                    cleanup = getattr(self, "skill_terminal_cleanup", lambda: None)()
                    if self._part_a_clean_failure(None, ("fail", travel), cleanup):
                        from arti_stardew_part_b import defer_forage
                        if defer_forage(self, session, travel):
                            continue  # nothing offered before 09:00: no start, input or row verdict
                if self._part_a_skip_clean_row(session, travel, deterministic=True, cleanup=cleanup):
                    continue
                return self._skill_end_locked(session, "held", "partial", travel)
            if travel is not None:
                c.legs.insert(0, call)
                call = travel
                c.travel["active"] = True
            elif task == "crops":
                facts = replace(facts, dry_soil=sum(v is False for v in crop_state.values()))
            failures = S.precondition_failures(call, facts)
            if failures:
                reason = "part_a_precondition:" + ",".join(failures)
                if self._part_a_skip_clean_row(session, reason, deterministic=True):
                    continue
                return self._skill_end_locked(session, "held", "partial", reason)
            if c.part == "a" and (task == "farm" or task == "sleep" and not c.tasks):
                # Farm entry / preface / final sleep offer no choice: run them without a brain call.
                choice = "plan"
                self._journal({"ev": "part_a_brain_call", "session_id": session.session_id, "day": len(c.days),
                               "task": task, "brain_call": "skipped_deterministic"})
            else:
                # One boundary per logical S-skill, not per button/transfer/crop.
                session.campaign_offer = calls[0]
                choice = self._skill_model_choose(session, "part_a:" + task)
                session.campaign_offer = None
            if choice == "ask_owner":
                # A19 (journal 4185-4188): Day 3 animals boundary chose ask_owner after Day 2's
                # unreachable_house row and the campaign HELD. The owner already granted this whole
                # campaign: never ask mid-run; the scheduled task runs under the same fences/caps.
                self._journal({"ev": "part_" + c.part + "_ask_owner_overridden", "session_id": session.session_id,
                               "day": len(c.days), "task": task})
                choice = "invalid"
            if not isinstance(choice, S.SkillCall) and choice not in ("invalid", "finish", "plan"):
                return self._skill_end_locked(session, "held", "partial", "part_a_choice:" + str(choice))
            # A malformed answer / model finish cannot erase required work.
            session.campaign_start = task
            return self._skill_start(session, call, by="model" if isinstance(choice, S.SkillCall) else "plan", facts=facts)
        return self._skill_end_locked(session, "held", "partial", "part_a_missing_sleep_transition")

    def _part_a_calls(self, task, facts):
        if self._skill_session.campaign.part == "c":
            from arti_stardew_part_c import calls
            return calls(task)
        if self._skill_session.campaign.part == "b":
            from arti_stardew_part_b import calls
            return calls(task)
        C = S.SkillCall
        if task == "farm":
            return [] if facts.location == "Farm" else [C("pergi_ke", "farm")]
        if task == "crops":
            # The accepted baseline enrolls 1-4 dry tiles. The production water
            # verifier, not the model, terminates each tile and the final scan.
            return [C("siram")] * 4
        if task == "animals":
            return [C("rawat_hewan")]
        if task == "machine":
            c = self._skill_session.campaign
            inserted = next((e for e in c.machine_cycle if e["kind"] == "insert"), None)
            if inserted and hasattr(self, "_skill_wave1_context"):
                context = self._skill_wave1_context(self._build_decision_snapshot(self._active))
                if not any(e.get("kind") == "machine_collect" and e.get("ready") is not False
                           and S._wave1_target_tile(e) == tuple(inserted["tile"])
                           for e in context.get("extras", ())):
                    return []  # no S20 start without a current ready offer; recheck next morning
            if inserted:
                return [C("mesin", "ambil", tile=tuple(inserted["tile"]))]
            target = self._part_a_native_target("machine")
            if target:
                return [C("mesin", tile=tuple(target["tile"]))]
            busy = self._part_a_machine_busy()
            if busy is not None:
                c.machine_busy = busy
                self._journal(dict(ev="part_a_machine_busy", **busy))
                return "part_a_machine_busy:" + busy["ref"]
            return "part_a_target_unavailable:machine"
        if task == "clear":
            target = self._part_a_native_target("clear")
            if target is None:
                return "part_a_target_unavailable:clear"
            x, y = target["tile"]
            # Keep target selection bounded. S17 separately proves the entire
            # native scythe sweep harmless, including cells outside this tile.
            return [C("bersihkan", f"{x},{y}:{x},{y}")]
        if task == "wood":
            return [C("tebang")]
        if task == "stone":
            return [C("pecah_batu"), C("pungut_drop", "batu")]
        if task == "food":
            c = self._skill_session.campaign
            if c.days[-1]["rows"].get("food"):
                return []  # one S5 per day leaves starts for all three cabin exits
            # A27 (1146/1300 inputs, every other row met): energy never fell below 150, so PLAN's
            # survival row stayed unwitnessed. The run's last food leg (Day 3, before storage)
            # offers S5 once; the routine itself skips `already_full` vitals (no forced eating).
            last_chance = (c.part == "a" and len(c.days) == 3 and "food" not in c.tasks
                           and not any(d["rows"].get("food") for d in c.days))
            return [C("makan")] if (last_chance or facts.energy is not None and facts.energy < 150
                                     or facts.health is not None and facts.health < 80) else []
        if task == "storage":
            c = self._skill_session.campaign
            reserve = {q: max(0, n-c.baseline_units_deposited.get(q, 0)) for q, n in c.reserve.items()}
            out = []
            for qid, quality, count in facts.store_stacks:
                # Only earned units are the objective. Native product quick-move may
                # also carry baseline units; food/tools remain excluded by store_stacks.
                keep = min(count, reserve.get(qid, 0))
                reserve[qid] = reserve.get(qid, 0)-keep
                if count > keep:
                    out.append(C("simpan_ke_chest", qid, count-keep, quality=quality))
            return out
        return [C("pergi_ke", "home"), C("tidur")]

    def _part_a_inputs_remaining(self):
        limit = getattr(self, "_action_session_limit", None)
        spent = getattr(self, "_physical_action_publications", None)
        return max(0, limit - spent) if type(limit) is int and type(spent) is int else None

    def _part_a_reserve(self, inputs, part="a"):
        """Measured on a 1,300-input cap; a smaller approved cap scales it down."""
        limit = getattr(self, "_action_session_limit", None)
        cap = RESERVE_MEASURED_INPUT[part]
        return inputs if type(limit) is not int or limit >= cap else inputs * limit // cap

    @staticmethod
    def _campaign_sleep_reserve(part):
        """(reserve, held reason, per-row extra need) for the Part A / Part B day's home leg."""
        if part == "b":
            import arti_stardew_part_b as B
            return B.SLEEP_RESERVE, B.SLEEP_RESERVED, B.ROW_INPUTS
        return SLEEP_RESERVE, SLEEP_RESERVED, ROW_INPUTS

    def _part_a_inputs_exhausted(self):
        limit = getattr(self, "_action_session_limit", None)
        spent = getattr(self, "_physical_action_publications", None)
        return type(limit) is int and type(spent) is int and spent >= limit

    def _part_a_travel_call(self, task, facts):
        """Stage Farm work using the shipped observed-open waypoint planner.

        Food already uses the native in-place/safe-site driver. Sleep has its
        verified home leg; animals have their native house itinerary.
        """
        c = self._skill_session.campaign
        if c.part == "c":
            from arti_stardew_part_c import travel
            return travel(self, task, facts)
        if c.part == "b":
            from arti_stardew_part_b import travel
            return travel(self, task, facts)
        if task not in ("crops", "storage", "machine", "clear", "wood", "stone"):
            return None
        if facts.location != "Farm":
            return S.SkillCall("pergi_ke", "farm")
        if task == "crops":
            soil = self._part_a_soil()
            targets = (tuple(t for t in c.crops if soil[t] is False)
                       if set(soil) == set(c.crops) and all(v in (True, False) for v in soil.values()) else c.crops)
            if not targets:
                return None
        elif task in ("wood", "stone"):
            targets = ()  # the resource selector below uses only current native facts
        else:
            target = self._part_a_native_target(task)
            if target is None:
                return "part_a_target_unavailable:" + task
            targets = (tuple(target["tile"]),)
        return self._campaign_local_travel(task, facts, targets)

    def _part_a_native_target(self, task, snapshot=None):
        """Pin the nearest offered work identity, shared by travel and the work leg."""
        from arti_stardew_native_targets import nearest, tile, machines, debris
        c = self._skill_session.campaign
        snap = snapshot or self._build_decision_snapshot(self._active)
        context = self._skill_wave1_context(snap)
        here = S._g(S._g(snap, "motor"), "tile")
        held = c.native_targets.get(task)
        if task == "clear":
            # The global scene can be clipped to distant objects. A single
            # bounded read near our pose exposes cheap local work before pinning.
            pin = S._g(snap, "water_context") or S._g(snap, "movement_context")
            center = tuple(held["tile"]) if held else here
            if pin is not None and center is not None and (not held or held["ref"] not in context.get("targets", {})):
                local = self._part_a_inspect(pin, *center, 1 if held else 4)
                if local is not None:
                    context = dict(context, targets=dict(context.get("targets", {}), **local))
        entries = (context.get("chests", ()) if task == "storage" else
                   machines(context) if task == "machine" else debris(context))
        if task == "storage":
            # A35: discovery is not capacity/ownership proof. Keep unusable or
            # already refused identities out for this campaign (at most four).
            entries = [e for e in entries if e.get("ref") not in c.storage_rejected
                       and e.get("usable") is not False]
            if len(c.storage_rejected) >= 4:
                return None
            if not held:
                pid = S._g(S._g(snap, "binding"), "player_id")
                def preference(e):
                    if e.get("ref") in c.storage_verified:
                        return 0
                    if pid is not None and e.get("owner_id", e.get("owner_player_id")) == pid:
                        return 1
                    return 2 if e.get("usable") is True else 3
                best = min((preference(e) for e in entries), default=3)
                entries = [e for e in entries if preference(e) == best]
        if task == "machine" and c.machine_cycle:
            inserted = next((e for e in c.machine_cycle if e["kind"] == "insert"), None)
            if inserted:
                entries = [e for e in entries if tile(e) == tuple(inserted["tile"])]
        if held:
            selected = next((e for e in entries if e.get("ref") == held["ref"] and tile(e) == tuple(held["tile"])), None)
        else:
            selected = nearest(entries, here, grid=context.get("nav_grid"),
                               remaining=self._part_a_remaining_work(context, task),
                               player_id=S._g(S._g(snap, "binding"), "player_id"))
        if selected is None:
            return None
        target = dict(ref=selected["ref"], tile=tile(selected))
        if "stands" in selected:
            target["stands"] = selected["stands"]
        c.native_targets[task] = target
        return target

    def _part_a_machine_busy(self):
        """A native Farm machine that is processing now (held item, not ready, minutes > 0), else None.

        PROOF_A2 (A1 L377, A2 L967): the mod publishes machine_collect only when readyForHarvest and
        machine_insert only when heldObject is null (ModEntry.Extras.cs:1104-1107), so a busy jar has no
        offer at all. Scene object facts (SceneSnapshotCapture.cs:398-422) still carry machine, held_item,
        ready_for_harvest and minutes_until_ready. A clipped scene gets ONE bounded farm scan per day.
        """
        c = self._skill_session.campaign
        snap = self._build_decision_snapshot(self._active)
        pin = S._g(snap, "water_context") or S._g(snap, "movement_context")
        if pin is None or pin.location != "Farm":
            return None
        context = self._skill_wave1_context(snap)
        def busy(facts):
            out = []
            for ref, f in facts.items():
                if (isinstance(f, Mapping) and f.get("kind") == "object" and f.get("machine") is True
                        and isinstance(f.get("held_item"), Mapping) and f.get("ready_for_harvest") is False
                        and type(f.get("minutes_until_ready")) is int and f["minutes_until_ready"] > 0
                        and (t := S._wave1_xy(f.get("tile"))) is not None):
                    out.append(dict(ref=ref, tile=list(t), minutes=f["minutes_until_ready"],
                                    held=f["held_item"].get("qualified_item_id")))
            return min(out, key=lambda e: (e["minutes"], e["ref"])) if out else None
        found = busy(context.get("targets", {}))
        if found is None and not context.get("scene_complete") and c.machine_scan_day != len(c.days):
            c.machine_scan_day = len(c.days)
            found = busy(self._part_a_farm_scan(pin, context) or {})
        if found is not None:
            found["day"] = len(c.days)
        return found

    def _part_a_remaining_work(self, context, task):
        """Tie-break only: today's native work and the actor's own home door."""
        from arti_stardew_native_targets import machines, debris
        c = self._skill_session.campaign
        if c.part != "a":
            return ()  # Farm work ordering does not add requirements to B/C travel.
        facts = {ref: f for ref, f in context.get("targets", {}).items() if isinstance(f, Mapping)}
        home = next((f.get("home_location") for f in facts.values()
                     if f.get("kind") == "location" and f.get("home_location")), None)
        groups = dict(storage=context.get("chests", ()), machine=machines(context), clear=debris(context),
                      crops=[dict(tile=t) for t in c.crops],
                      sleep=[dict(tile=f["tile"], stands=[(t[0], t[1]+1)])
                             for f in facts.values() if home and f.get("target_location") == home
                             and f.get("kind") == "building_door" and f.get("enterable") is True
                             and (t := S._wave1_xy(f.get("tile"))) is not None])
        for resource in ("wood", "stone"):
            groups[resource] = [f for f in facts.values() if resource_fact(f, resource)]
        return [groups[name] for name in dict.fromkeys(c.tasks) if name != task and name in groups and groups[name]]

    def _campaign_local_travel(self, task, facts, targets, *, exact=False):
        c = self._skill_session.campaign
        snap = self._build_decision_snapshot(self._active)
        pin = snap.water_context or snap.movement_context
        here = S._wave1_xy(getattr(snap.motor, "tile", None))
        last = S._g(S._g(snap, "working_memory"), "last_action")
        if (pin is None or pin.location != facts.location or here is None
                or self._action_cancel.is_set() or self._gather_mutation_unresolved
                or self._goal_route_facts(pin) is None
                or last is not None and (S._g(last, "cleanup_confirmed") is False
                    or S._g(last, "lifecycle") in ("uncertain", "interrupted"))):
            return "part_a_travel:" + task + ":fresh_context_required"
        from arti_stardew_goal_routes import open_paths, open_path
        from arti_stardew_animal_routine import house_waypoint, actor_cell_open
        # A23: a pet/animal/farmer on ARTI's own cell marks it '#'; only that one cell is opened.
        raw_grid = self._nav_grid(pin)
        grid = actor_cell_open(raw_grid, here)
        if grid is not raw_grid:
            # Report evidence strips nav_grid; keep the one opened cell visible in the journal.
            self._journal({"ev": "part_a_actor_cell_opened", "task": task, "tile": list(here)})
        resource_tiles, default_targets = set(), targets
        if task in ("wood", "stone"):
            context = self._skill_wave1_context(snap)
            facts_seen = [dict(f, ref=r) for r, f in context.get('targets', {}).items() if isinstance(f, Mapping)]
            if c.part == "a" and not context.get("scene_complete"):
                if not c.travel.get("resource_scan_attempted"):
                    c.travel["resource_scan_attempted"] = True
                    scan = self._part_a_farm_scan(pin, context)
                    if scan is not None:
                        c.travel["resource_scan_targets"] = [dict(f, ref=r) for r, f in scan.items() if isinstance(f, Mapping)]
                facts_seen += c.travel.get("resource_scan_targets", [])
            if task == "wood" and c.part == "a" and facts.location == "Farm":
                # Native tree offers can survive a clipped global scene. Read their own tiles.
                context = self._skill_wave1_context(snap)
                centers = {S._wave1_target_tile(e) for e in context.get("extras", ())
                           if e.get("kind") == "shake_tree"}
                if not any(resource_fact(f, task) for f in c.travel.get("resource_scan_targets", ())):
                    # A slim Tree projection is a discovery hint, never a mature
                    # work target. Its bounded native read must supply the stage.
                    centers.update(S._wave1_xy(f.get("tile")) for f in facts_seen
                                   if f.get('kind') == 'terrain' and f.get('terrain_type') == 'Tree')
                for center in sorted(t for t in centers if t is not None):
                    facts_seen += [dict(f, ref=r) for r, f in (self._part_a_inspect(pin, *center, 4) or {}).items()]
            for fact in facts_seen:
                tile = S._wave1_xy(fact.get("tile")) if isinstance(fact, Mapping) else None
                if resource_fact(fact, task) and tile is not None:
                    resource_tiles.add(tile)
            rejected = c.travel.setdefault("rejected_trees" if task == "wood" else "rejected_stones", [])
            held = c.travel.get("resource_tile")
            if held is not None:
                fresh = {r: f for r, f in context.get('targets', {}).items()
                         if resource_fact(f, task) and S._wave1_xy(f.get('tile')) == held}
                if not fresh:
                    fresh = self._part_a_inspect(pin, *held, 1)
                if fresh is None:
                    return "part_a_target_unavailable:" + task
                if c.travel.get('resource_ref') and c.travel['resource_ref'] not in fresh:
                    return 'part_a_travel:' + task + ':resource_identity_changed'
                if not any(resource_fact(f, task) and S._wave1_xy(f.get("tile")) == held for f in fresh.values()):
                    rejected.append(c.travel.pop("resource_tile"))
                    resource_tiles.discard(held)
            while True:
                candidates = resource_tiles - set(rejected)
                if candidates and "resource_tile" not in c.travel:
                    from arti_stardew_native_targets import nearest, work_tiles
                    def resource_stands(entry):
                        if task == "wood":
                            fall = wood_search_stands(grid, entry["tile"], facts_seen)
                            if fall is not None:
                                return fall
                            from arti_stardew_native_targets import adjacent
                            return adjacent(entry["tile"])
                        return work_tiles(entry)
                    chosen = nearest([dict(tile=t) for t in candidates], here, grid=grid,
                                     stands=resource_stands, remaining=self._part_a_remaining_work(context, task))
                    c.travel.setdefault("resource_tile", chosen["tile"])
                    c.travel['resource_ref'] = min(f['ref'] for f in facts_seen
                        if resource_fact(f, task) and S._wave1_xy(f.get('tile')) == chosen['tile'])
                    c.travel['survey'] = {}  # distances belong to this target only
                    self._journal(dict(ev='part_a_resource_selected', task=task, tile=chosen['tile'],
                        ref=c.travel['resource_ref'],
                        fall_stands=(None if (fall := wood_fall_stands(grid, chosen['tile'])) is None
                                     else sorted(fall)) if task == 'wood' else None))
                if task != "wood" or "resource_tile" not in c.travel:
                    break
                # Reject a known trap; an unseen stand remains discovery only.
                fall = wood_search_stands(grid, c.travel["resource_tile"], facts_seen)
                if fall is None or fall:
                    break
                rejected.append(c.travel.pop("resource_tile"))
                self._journal(dict(ev='part_a_resource_rejected', task=task, tile=rejected[-1],
                                   reason='known_blocked_fall_corridor'))
                if not resource_tiles - set(rejected):
                    return "part_a_travel:wood:no_open_fall_corridor"
            if "resource_tile" not in c.travel:
                # A19: the fixed placeholders (53,17)/(54,18) are Grass after S17 (journal 2036/2659),
                # never a tree/stone. No observed resource is an honest unmet row, not a walk there.
                return "part_a_travel:" + task + (":target_stand_unreachable" if rejected else
                                                  ":no_observed_" + ("tree" if task == "wood" else "stone"))
            targets = (c.travel["resource_tile"],)
        parents = open_paths(grid, here)
        if not parents:
            return "part_a_travel:" + task + ":native_grid_unavailable"
        stands = set(targets) if exact else {(x+dx, y+dy) for x, y in targets for dx, dy in ((0,1),(-1,0),(0,-1),(1,0))}
        if task == "wood" and "resource_tile" in c.travel:
            tx, ty = c.travel["resource_tile"]
            fall = wood_fall_stands(grid, (tx, ty))
            from arti_stardew_native_targets import adjacent
            stands = set(adjacent((tx, ty))) if fall is None else fall
            if here in stands and fall is None:
                return "part_a_travel:wood:fall_row_unobserved"  # never start on an unobserved row
        if task in ("storage", "machine", "clear") and not exact:
            from arti_stardew_native_targets import work_tiles
            selected = c.native_targets.get(task)
            if selected and tuple(selected["tile"]) in targets:
                stands = set(work_tiles(selected))
        if here in stands and grid.get(here) == ".":
            if task in ("wood", "stone") and c.travel.get("resource_tile") not in resource_tiles:
                return "part_a_travel:" + task + ":resource_target_not_current"
            return None
        paths = [p for t in stands if (p := open_path(parents, t)) is not None]
        if not stands:
            return "part_a_travel:" + task + ":target_stand_unreachable"
        if not paths:
            # Hand blocked native stands to the same bounded navigation run.
            from arti_stardew_debris_route import blocking_path
            context = self._skill_wave1_context(snap)
            repair_facts = context.get("targets", {})
            if task in ("wood", "stone"):
                # Discovery facts only suggest an ingress. The fresh grid must
                # prove its stand reachable, and S17 must re-offer the blocker
                # locally before a tool input can be admitted.
                repair_facts = {f['ref']: f for f in facts_seen}
                repair_facts.update(context.get("targets", {}))
            repair = (blocking_path(grid, here, stands, repair_facts,
                       [f.get("qualified_item_id") for f in context.get("inventory", ())])
                      if context.get("inventory_complete") and context.get("ui_complete") else None)
            if repair is not None:
                if repair.get("blocker"):
                    return "part_a_travel:" + task + ":" + repair["blocker"]
                destination = min(stands, key=lambda t: (abs(t[0]-here[0])+abs(t[1]-here[1]), t))
                if repair.get("path") and repair["path"][-1] in stands:
                    destination = repair["path"][-1]
                ingress = tuple(repair['target']['stand'])
                if ingress != here:
                    destination = ingress
                return self._campaign_travel_waypoint(task, destination)
        if paths:
            from arti_stardew_native_targets import WorkRanker
            context = self._skill_wave1_context(snap)
            ranker = WorkRanker(grid, here, remaining=self._part_a_remaining_work(context, task),
                                stands=lambda e: (e["tile"],))
            destination = min(paths, key=lambda p: ranker.rank(dict(tile=p[-1])))[-1]
        else:
            # Known covered stands without an open path cannot be invented.
            if all(t in grid for t in stands):
                rejected = c.travel.get("rejected_trees" if task == "wood" else "rejected_stones")
                if task in ("wood", "stone") and "resource_tile" in c.travel and len(rejected) < 8:
                    # Try the next observed resource; the original budgets still bound the row.
                    rejected.append(c.travel.pop("resource_tile"))
                    return self._campaign_local_travel(task, facts, default_targets, exact=exact)
                return "part_a_travel:" + task + ":target_stand_unreachable"
            destination = min(stands, key=lambda t: (abs(t[0]-here[0])+abs(t[1]-here[1]), t))
        part_a = c.part == "a"
        tile = house_waypoint(grid, here, destination, c.travel.setdefault("survey", {}), window_best=part_a)
        if tile is None and part_a and not c.travel.get("survey_reset"):
            # A25: one fresh survey from the current pose; only the current grid authorizes a leg.
            c.travel["survey_reset"] = True
            c.travel["survey"] = {}
            tile = house_waypoint(grid, here, destination, c.travel["survey"], window_best=True)
        if tile is None:
            return "part_a_travel:" + task + ":no_observed_route_or_stand"
        return self._campaign_travel_waypoint(task, tile)

    def _campaign_travel_waypoint(self, task, tile):
        c = self._skill_session.campaign
        if task in ('wood', 'stone'):
            visited = c.travel.setdefault('resource_waypoints', [])
            # A waypoint reused for a different resource can be necessary
            # backtracking. Repeats toward the SAME resource still fail closed;
            # the total eight-leg cap spans every candidate.
            visit = (c.travel.get('resource_tile'), tile)
            if visit in visited:
                return 'part_a_travel:' + task + ':resource_route_cycle'
            if len(visited) >= RESOURCE_TRAVEL_LEGS:
                return 'part_a_travel:' + task + ':resource_route_leg_cap'
            visited.append(visit)
        return S.SkillCall("jalan_ke", f"{tile[0]},{tile[1]}", tile=tile)

    def _part_a_travel_entries(self, pin):
        """Publish only a still-reachable current-grid waypoint to local approach."""
        session = getattr(self, "_skill_session", None)
        run = session.current if session is not None and session.status == "active" else None
        if (run is None or session.campaign is None or self._active is None
                or not run.state.get("campaign_travel") or run.call.name != "jalan_ke"
                or run.state.get('route_clear')
                or self._active.goal_id != run.goal_id or pin is None
                or session.campaign.part == "a" and pin.location != "Farm"):
            return None
        from arti_stardew_goal_routes import open_paths, open_path
        from arti_stardew_animal_routine import actor_cell_open
        motor = self._state.latest(pin.player_id)
        if motor is None or self._state.is_stale(pin.player_id) or motor.location != pin.location:
            return []
        if (self._action_cancel.is_set() or self._gather_mutation_unresolved
                or self._goal_route_facts(pin) is None):
            return []
        # A23 (journal 2597-2603): pet Dudley walked onto ARTI's tile 51,10 mid-walk; the own '#'
        # cell emptied open_paths and the waypoint vanished (TARGET_REMOVED). Open only that cell.
        path = open_path(open_paths(actor_cell_open(self._nav_grid(pin), motor.tile), motor.tile), run.call.tile)
        if path is None:
            return []
        x, y = run.call.tile
        return [dict(ref=f"part_a_waypoint:{x},{y}", kind="part_a_route_stand", tile=[x,y],
                     reach="DIRECT_TARGET", revision=f"{x},{y}")]

    def _part_a_crop_view(self, view):
        c = self._skill_session.campaign
        if c is not None and c.task == "crops":
            enrolled = set(c.crops)
            view.approach_targets = tuple(e for e in view.approach_targets
                if S._wave1_target_tile(e) in enrolled)
            # Native tool_work_target is a scene ref string, not a tile dict.
            target = view.tool_work_target
            allowed = {f"tf:Farm:{x},{y}:HoeDirt" for x, y in enrolled}
            if target is not None and not (target in allowed if isinstance(target, str) else
                    isinstance(target, Mapping) and S._wave1_target_tile(target) in enrolled):
                view.tool_work_target, view.use_tool_available = None, False
            soil = self._part_a_soil()
            view.crop_target_tiles = (tuple(t for t in c.crops if soil[t] is False)
                if set(soil) == enrolled and all(v in (True, False) for v in soil.values()) else None)
            view.crop_enrollment_current = view.crop_target_tiles is not None
            if view.crop_target_tiles is not None:
                from arti_stardew_native_targets import WorkRanker
                here = S._wave1_xy(S._g(S._g(view, "motor"), "tile"))
                context = self._skill_wave1_context(view) if hasattr(self, "_skill_wave1_context") else {}
                if here is not None:
                    ranker = WorkRanker(context.get("nav_grid"), here,
                                        remaining=self._part_a_remaining_work(context, "crops"))
                    view.approach_targets = tuple(sorted((e for e in view.approach_targets
                        if S._wave1_target_tile(e) in view.crop_target_tiles), key=ranker.rank))
        return view

    def _part_a_walk_step(self, run, snap):
        seen = self._live7_seen_step(run, snap) if run.state.get("live7") else None  # ADR 136: a walk to a sighting
        if seen is not None:
            return seen
        last = S._g(S._g(snap, "working_memory"), "last_action")
        if last is not None and (S._g(last, "cleanup_confirmed") is False
                                or S._g(last, "lifecycle") in ("uncertain", "interrupted")):
            return S.Step("fail", reason="action_unreconciled")
        if tuple(snap.motor.tile) == run.call.tile:
            return S.Step("decision", S.FINISH)
        if S._g(S._g(snap, "working_memory"), "last_inspect_status") != "current":
            return S.Step("decision", S.INSPECT)
        move = S._approach(snap, lambda e: e.get("kind") == "part_a_route_stand")
        if move:
            return S.Step("decision", move)
        if run.state.get("part_a_walk_reinspects", 0) < WALK_REINSPECTS:
            # A23: a wandering character can hide the waypoint for a moment. A bounded read-only
            # re-inspect (no input) re-reads the native grid before the leg is held.
            run.state["part_a_walk_reinspects"] = run.state.get("part_a_walk_reinspects", 0) + 1
            return S.Step("decision", S.INSPECT)
        return S.Step("fail", reason="part_a_travel:no_observed_route_or_stand")

    def _part_a_await_fresh_context(self, seconds=None):
        """Block (outside the runtime lock) until a NEWER native ui.context arrives; False on timeout/cancel.

        Read-only: no request, input or lease. The caller re-derives travel from that fresh grid."""
        obs = getattr(self, "_obs", None)
        if obs is None or getattr(self, "_action_cancel", None) is None:
            return False
        try:
            _, pid = self._binding_view()
            seen = obs.get(pid, "ui.context") if pid is not None else None
            if seen is None:
                return False
            basis = (seen.domain_revision, seen.received_monotonic)
            deadline = game_now(self) + (TRAVEL_REREAD_SECONDS / TRAVEL_REREADS if seconds is None else seconds)
            stop = getattr(self, "_worker_stop", None)
            while game_now(self) < deadline:
                if W.call('poll', 110, self._action_cancel.wait, .05) or stop is not None and stop.is_set():
                    return False
                rec = obs.get(pid, "ui.context")
                if rec is not None and (rec.domain_revision, rec.received_monotonic) != basis:
                    return True
        except Exception:  # noqa: BLE001 - no fresh read is the old (held) result
            return False
        return False

    def _part_a_inspect(self, pin, cx, cy, radius):
        """Bounded native Farm inspect (<= radius 4) under the pinned identity fences; facts or None."""
        try:
            if (pin is None or pin.location != "Farm" or self._action_cancel.is_set()
                    or self._gather_mutation_unresolved):
                return None
            folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
            session = self._commands.baca_sesi(folder)
            if (session.session_id != pin.command_session_id or session.player_id != pin.player_id
                    or session.screen_id != pin.screen_id
                    or session.mentah.get("runtime_instance_id") != pin.runtime_instance_id):
                return None
            result = self._commands.minta_inspect_tiles(cx, cy, radius, expected_location_id="Farm", folder=folder, sesi=session)
            raw = result.mentah or {}
            current = self._water_context(pin.player_id)
            if (not result.sukses or result.outcome != "Inspected" or current is None
                    or self._action_cancel.is_set() or self._gather_mutation_unresolved
                    or any(getattr(current, k) != getattr(pin, k) for k in
                        ("runtime_instance_id", "command_session_id", "observation_session_id", "player_id", "screen_id", "location"))
                    or raw.get("session_id") != pin.command_session_id or raw.get("player_id") != pin.player_id
                    or raw.get("location_id") != "Farm" or raw.get("center_x") != cx or raw.get("center_y") != cy
                    or raw.get("radius") != radius or not isinstance(raw.get("facts"), Mapping)):
                return None
            return raw["facts"]
        except Exception:
            return None

    def _part_a_farm_scan(self, pin, context):
        """Complete bounded windows over native Farm dimensions, using the existing inspect primitive."""
        locations = [f for f in context.get("targets", {}).values()
                     if isinstance(f, Mapping) and f.get("kind") == "location" and f.get("location_id") == "Farm"]
        if len(locations) != 1:
            return None
        width, height = locations[0].get("map_width"), locations[0].get("map_height")
        if not all(type(n) is int and 0 < n <= 1000 for n in (width, height)):
            return None
        found = {}
        radius = 4
        diameter = radius * 2 + 1
        for y in range(radius, height + radius, diameter):
            for x in range(radius, width + radius, diameter):
                deadline = getattr(self, "_trial_deadline_unix_ms", None)
                if type(deadline) is int and time.time() * 1000 >= deadline:
                    return None
                window = self._part_a_inspect(pin, min(x, width-1), min(y, height-1), radius)
                if window is None:
                    return None
                found.update(window)
        return found

    def _part_a_soil(self):
        snapshot = self._build_decision_snapshot(self._active)
        pin = snapshot.water_context or snapshot.movement_context
        if pin is None or pin.location != "Farm" or self._action_cancel.is_set() or self._gather_mutation_unresolved:
            return {}
        context = self._skill_wave1_context(snapshot)
        c = self._skill_session.campaign
        found = context.get("targets", {})
        if not c.crops and not context.get("scene_complete"):
            found = self._part_a_farm_scan(pin, context)
            if found is None:
                return {}
        if c.crops:
            candidates = set(c.crops)
        else:
            planted = {}
            for f in found.values():
                if (isinstance(f, Mapping) and f.get("terrain_type") == "HoeDirt" and isinstance(f.get("crop"), Mapping)
                        and (t := S._wave1_xy(f.get("tile"))) is not None):
                    planted[t] = f["crop"].get("watered")
            candidates = set(planted)
            if len(candidates) > CROP_ENROLL_MAX:
                # PROOF_A2 map independence: a larger farm is not "no plot". Enroll the PLAN's 4 tiles from
                # native facts only: dry first, then nearest to ARTI's own tile, then tile order.
                here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile")) or (0, 0)
                candidates = set(sorted(candidates, key=lambda t: (planted[t] is not False,
                    abs(t[0]-here[0]) + abs(t[1]-here[1]), t))[:CROP_ENROLL_MAX])
        if not 1 <= len(candidates) <= CROP_ENROLL_MAX:
            return {}
        soil, lost = {}, []
        # Each enrolled tile is re-read through the original native identity fences.
        for center in sorted(candidates):
            found = self._part_a_inspect(pin, *center, 2)
            if found is None:
                return {}
            seen = False
            for f in found.values():
                if (isinstance(f, Mapping) and f.get("terrain_type") == "HoeDirt"
                        and isinstance(f.get("crop"), Mapping)):
                    tile = S._wave1_xy(f.get("tile"))
                    if tile in candidates:
                        soil[tile] = f["crop"].get("watered")
                        seen = seen or tile == center
            if c.crops and not seen:
                lost.append(center)
        if lost and len(lost) < len(candidates):
            # PROOF_A2 / A1 Day 3 (a native damage notice: decomp GameLocation.cs:15397-15414
            # removes HoeDirt under new weeds). A completed native read of the enrolled tile shows no planted
            # HoeDirt: nothing is left to water there. Shrink the enrollment, record it, never credit it.
            c.crops = tuple(t for t in c.crops if t not in lost)
            if c.days:
                c.days[-1].setdefault("crops_lost", []).extend(list(t) for t in lost)
            self._journal({"ev": "part_a_crop_lost", "tiles": [list(t) for t in lost],
                           "enrolled": [list(t) for t in c.crops]})
            candidates = set(c.crops)
        return soil if set(soil) == candidates else {}

    def _part_a_chest_matches(self, *, location, tile, ref=None):
        from arti_stardew_native_targets import xy
        c = self._skill_session.campaign
        selected = c.native_targets.get("storage")
        return bool(location == "Farm" and selected and xy(tile) == tuple(selected["tile"])
                    and (ref is None or ref == selected["ref"]))

    def _part_a_storage(self):
        session = getattr(self, "_skill_session", None)
        return bool(session is not None and session.status == "active" and session.campaign is not None
                    and session.campaign.task == "storage")

    def _part_a_store_chain_stopped(self, goal, stop, close):
        """A35: stop this skill before it reopens a chest with no deposit offer.

        Called after the existing failsafe close. Only the ordinary skill boundary
        may retry, after fresh cleanup, identity and inventory checks. A partial
        deposit is retained as a named gap rather than replaying the full amount.
        """
        if stop != "transfer_not_offered" or not self._part_a_storage():
            return
        session = self._skill_session
        run = session.current
        if (self._active is not goal or run is None or run.goal_id != goal.goal_id
                or run.call.name != "simpan_ke_chest"):
            return
        chest = getattr(goal, "store_chest_pin", None)
        close = close or {}
        run.state["campaign_storage_refusal"] = dict(
            ref=chest.get("ref") if chest else None, closed=close.get("status") == "closed",
            transferred=(getattr(goal, "transfer_progress", None) or {}).get("transferred", 0))
        run.pending = ("fail", "storage_transfer_not_offered")

    def _part_a_storage_retry(self, session, run, cleanup):
        """Retain this run's counters/deadline and retry one different native chest."""
        c = session.campaign
        refusal = run.state.get("campaign_storage_refusal", {})
        selected = c.native_targets.get("storage")
        if (not selected or refusal.get("ref") != selected["ref"]
                or refusal.get("closed") is not True or refusal.get("transferred") != 0
                or not self._part_a_clean_failure(run, run.pending, cleanup)):
            return False
        c.storage_rejected.add(selected["ref"])
        c.native_targets.pop("storage", None)
        # A new route must fit the original row's remaining work allowance,
        # even though ordinary initial Farm travel has a larger discovery cap.
        total = run.state.get("campaign_work_caps", (128, 160, 600))
        base = run.state.get("campaign_work_base", (0, 0, 0))
        limits = run.state.get("campaign_travel_caps", (float("inf"),) * 3)
        run.state["campaign_travel_caps"] = tuple(min(limit, used + cap)
            for limit, used, cap in zip(limits, base, total))
        c.travel = {}  # routing hints belong to the old target; run caps do not
        target = self._part_a_native_target("storage")
        self._journal(dict(ev="part_a_storage_retry", rejected=selected["ref"],
                           selected=target["ref"] if target else None, attempts=len(c.storage_rejected)))
        if target is None:
            run.pending = ("fail", "part_a_target_unavailable:storage")
            return False
        facts = self._skill_current_facts()
        travel = self._part_a_travel_call("storage", facts)
        if isinstance(travel, str):
            run.pending = ("fail", travel)
            return False
        c.legs.insert(0, run.call)
        if travel is not None:
            c.legs.insert(0, travel)
        # Preserve cumulative travel AND work caps. The new route is charged to
        # the same row; reaching it must not reset campaign_work_base.
        run.state["campaign_storage_retrying"] = True
        run.state["campaign_travel"] = travel is not None
        c.travel["active"] = travel is not None
        return self._campaign_resume_leg(c, run)

    def _part_a_sleep_transition(self, campaign, observed):
        _, pid = self._binding_view()
        motor = self._state.latest(pid)
        date = self._date_tuple(motor) if motor is not None else None
        verified = (observed.get("answered") is True and tuple(observed.get("start_date", ())) == campaign.date
                    and date == next_date(campaign.date) and type(motor.time_of_day) is int
                    and 600 <= motor.time_of_day < 700
                    and not self._state.is_stale(pid) and not self._day_start_active
                    and type(observed.get("bed_time")) is int and observed["bed_time"] < 2400)
        return verified, date

    def _part_a_finish_preface(self, session, run, verified, date=None):
        c = session.campaign
        c.preface.update(sleep_verified=verified, transition=list(date) if date else None)
        self._journal({"ev": "part_" + session.campaign.part + "_preface", "session_id": session.session_id, "owner_goal_id": session.goal_id,
                       **c.preface, "evidence": dict(legs=run.state["campaign_evidence"])})
        if verified:
            c.date = date
            c.morning()
        else:
            run.pending = ("fail", "part_a_preface_sleep_failed")
        return False

    def _part_a_skip_clean_row(self, session, reason, deterministic=False, cleanup=_NO_CLEANUP):
        """``cleanup`` reuses an end-of-row cleanup the caller already ran for this same boundary."""
        if cleanup is _NO_CLEANUP:
            cleanup = self.skill_terminal_cleanup() if hasattr(self, "skill_terminal_cleanup") else None
        if not self._part_a_clean_failure(None, ("fail", reason), cleanup):
            return False
        c = session.campaign
        brain = {"brain_call": "skipped_deterministic"} if deterministic else {}
        record = c.days[-1] if c.days else c.preface  # Part C: the approval-day S29 row is a preface row
        record["rows"][c.task] = False
        record["skills"].append(dict(task=c.task, verified=False, status="held", reason=reason, cleanup=cleanup, **brain))
        self._journal({"ev": "part_" + session.campaign.part + "_row", "session_id": session.session_id, "owner_goal_id": session.goal_id,
                       "day": len(c.days) or 1, "task": c.task, "verified": False, "status": "held", "reason": reason,
                       **({"preface": True} if not c.days else {}), **brain, "evidence": dict(cleanup=cleanup)})
        return True

    def _part_a_clean_failure(self, run, outcome, cleanup, *, sleep_travel=False):
        """Only released, reconciled row failures can leave unmet work behind.

        ``sleep_travel`` (PART_B4) asks the same question for the sleep row's own travel hop."""
        c = self._skill_session.campaign
        # Part C's approval-day S29 row runs before Day 1 exists, on the preface record.
        preface_row = not c.days and c.task == "morning" and isinstance(c.preface, Mapping) and "rows" in c.preface
        if ((not c.days and not preface_row) or (c.task == "sleep" and not sleep_travel) or outcome[0] != "fail"
                or any(word in outcome[1].casefold() for word in
                       ("uncertain", "unreconciled", "possible_mutation", "cleanup", "stop", "cancel",
                        "epoch", "identity", "date_changed", "goal_replaced", "part_a_readback",
                        "input_envelope_exhausted"))
                or not isinstance(cleanup, Mapping) or cleanup.get("menu") is not None
                or cleanup.get("free") is not True or cleanup.get("input_released") is not True
                or getattr(self, "_gather_mutation_unresolved", False)
                or getattr(self._active, "gather_uncertain", False)
                or any(event is not None and event.is_set() for event in
                       (getattr(self, "_action_cancel", None), getattr(self, "_worker_stop", None)))):
            return False
        action = getattr(self._active, "last_action", None)
        # A17 admitted this pre-publication placeholder for S3 only; A18b's S6 drop approach
        # ended on the same not_taken/request_id=None/cleanup=True refusal and so never
        # continued. No input was published for it, so no world effect is owed by any row.
        unpublished = action is not None and unpublished_placeholder(action)
        # A5's exhausted approach is also pre-publication, but is deliberately
        # not a replan: terminate the row without renewing its movement budget.
        budget_refusal = unpublished_budget_refusal(action)
        if action is not None and (S._g(action, "cleanup_confirmed") is not True
                or S._g(action, "lifecycle") not in ("completed", "rejected", "not_taken")
                or S._g(action, "world_effect_status") in ("uncertain", "possible_mutation", "unverified")
                and not ((unpublished or budget_refusal) and S._g(action, "world_effect_status") == "uncertain")):
            return False
        facts = self._skill_current_facts()
        _, pid = self._binding_view()
        motor = self._state.latest(pid)
        return (facts is not None and facts.inventory_current and facts.player_free
                and not self._state.is_stale(pid) and self._date_tuple(motor) == c.date
                and tuple(self._active.play_identity) == c.epoch)

    def _part_a_leg(self, session, run, outcome, cleanup):
        """Called only after ordinary terminal cleanup. True resumes the SAME S-skill."""
        if session.mode == "part_live":
            return self._live7_leg(session, run, outcome, cleanup)
        c = session.campaign
        if c is None:
            return False
        if c.part == "a" and c.days and not c.days[-1]["rows"].get("clear"):
            # S17 and route repair use the same target-matched tool-work oracle.
            # Only a released, current campaign boundary can credit the receipt;
            # selecting a blocker or merely completing X never proves removal.
            kinds = {"weeds": ("cut_weed", "weed_cut"), "twig": ("cut_twig", "twig_cut"),
                     "stone": ("break_rock", "rock_removed")}
            removals = []
            for receipt in run.state.get("route_clear_evidence", ()):
                target, effect = receipt.get("target", {}), receipt.get("observed", {})
                expected = kinds.get(target.get("kind"))
                if (expected and receipt.get("reason") == "observed_tool_effect"
                        and target.get("ref") and effect.get("target_ref") == target["ref"]
                        and effect.get("origin") == "tool_work_rule" and effect.get("request_id")
                        and (effect.get("kind"), effect.get("effect")) == expected):
                    removals.append(receipt)
            if removals and self._part_a_clean_failure(run, ("fail", "route_clear_verified"), cleanup):
                row = dict(task="clear", verified=True, reason="observed_route_debris_removal",
                           evidence=dict(route_clear=removals, cleanup=cleanup))
                c.days[-1]["rows"]["clear"] = True
                c.days[-1]["skills"].append(row)
                c.tasks = [task for task in c.tasks if task != "clear"]
                self._journal(dict(ev="part_a_row", session_id=session.session_id, owner_goal_id=session.goal_id,
                                   day=len(c.days), **row))
        ceiling = run.state.get("campaign_input_ceiling")
        if (outcome[0] == "fail" and type(ceiling) is int
                and outcome[1] in ("skill_budget_inputs", "animal_budget_exhausted")
                and self._physical_action_publications - run.inputs_start >= ceiling):
            # A25: the row stopped at the day's sleep reserve, not at its own skill budget.
            outcome = ("fail", self._campaign_sleep_reserve(c.part)[1])
            run.pending = outcome
        evidence = self._active.last_finish
        observed = dict(evidence.observed) if evidence is not None else {}
        if c.task == "animals":
            observed = dict(run.state.get("animal_care_observed", {}))
        leg = dict(skill=run.call.name, arg=run.call.arg, outcome=list(outcome), observed=observed,
                   cleanup=cleanup, inputs=self._physical_action_publications-run.inputs_start)
        if c.task == "animals":
            leg.update({key: run.state[key] for key in ("animal_care_observed", "animal_pet_status", "animal_pet_observed")
                        if key in run.state})
        run.state.setdefault("campaign_evidence", []).append(leg)
        if c.part == "a" and c.farm_entry and run.call == S.SkillCall("pergi_ke", "farm") and c.days:
            # A20: the folded farm-entry leg keeps its own farm row (verified by the leg's own readback).
            c.farm_entry = False
            ok = outcome[0] == "done"
            c.days[-1]["rows"]["farm"] = ok
            c.days[-1]["skills"].append(dict(task="farm", verified=ok, folded_into=c.task,
                                             **({} if ok else {"reason": outcome[1]}), evidence=[leg]))
            self._journal({"ev": "part_a_row", "session_id": session.session_id, "owner_goal_id": session.goal_id,
                           "day": len(c.days), "task": "farm", "verified": ok, "folded_into": c.task,
                           **({} if ok else {"reason": outcome[1]}), "evidence": dict(legs=[leg])})
        if c.task == "storage" and run.call.name == "simpan_ke_chest" and outcome[0] == "done":
            gain = observed.get("chest_gain")
            paired = (outcome[1] == "observed_paired_transfer_objective"
                      and type(gain) is int and gain >= run.call.n
                      and type(observed.get("bag_decrease")) is int and observed["bag_decrease"] == gain
                      and observed.get("cursor_empty") is True
                      and isinstance(cleanup, Mapping) and cleanup.get("menu") is None
                      and cleanup.get("free") is True and cleanup.get("input_released") is True)
            if paired:
                selected = c.native_targets.get("storage")
                if selected:
                    c.storage_verified.add(selected["ref"])
                qid = run.call.arg
                baseline = min(gain-run.call.n, max(0, c.reserve.get(qid, 0)-c.baseline_units_deposited.get(qid, 0)))
                leg.update(earned_quantity=run.call.n, baseline_units_deposited=baseline, quality=run.call.quality)
                if baseline:
                    c.baseline_units_deposited[qid] = c.baseline_units_deposited.get(qid, 0)+baseline
            else:
                outcome = ("fail", "part_a_readback:storage")
                leg["outcome"] = list(outcome)
                run.pending = outcome
        if run.state.get("campaign_travel") and outcome[0] == "done":
            facts = self._skill_current_facts()
            _, pid = self._binding_view()
            motor = self._state.latest(pid)
            reason = None
            if (facts is None or not facts.inventory_current or self._state.is_stale(pid)
                    or self._date_tuple(motor) != c.date or tuple(self._active.play_identity) != c.epoch):
                reason = "part_a_travel:epoch_or_fresh_facts_changed"
            call = None if reason else self._part_a_travel_call(c.task, facts)
            waiting = False
            if c.part == "b" and isinstance(call, str):
                from arti_stardew_part_b import MINES_WAITING
                if call == MINES_WAITING:
                    # Arrived in the Mine lobby before it opens. The row's own call is prepared
                    # now but held; the boundary re-polls the wait WITHOUT the runtime lock.
                    waiting, call = True, None
            if isinstance(call, str):
                reason, call = call, None
            arrived = (c.part != "a" and not reason and call is None
                       and bool(c.legs) and c.legs[0] == run.call)
            if arrived:
                # B1: the final travel hop WAS the row's own scheduled call (Part B town: BusStop->Town
                # S7). Its native transition/arrival is the row readback; never re-run it at the target.
                c.legs.pop(0)
                c.travel["active"] = False
            elif not reason and call is None:
                if c.task == "crops":
                    soil = self._part_a_soil()  # fresh bounded native read AFTER travel
                    if set(soil) != set(c.crops) or any(v not in (True, False) for v in soil.values()):
                        reason = "part_a_crop_enrollment_unavailable"
                    else:
                        c.days[-1].setdefault("crop_before", {str(k): v for k, v in soil.items()})
                        facts = replace(facts, dry_soil=sum(v is False for v in soil.values()))
                if not reason:
                    call = c.legs.pop(0)
                    failures = S.precondition_failures(call, facts)
                    if failures:
                        reason = "part_a_precondition:" + ",".join(failures)
                c.travel["active"] = False
                if not run.state.get("campaign_storage_retrying"):
                    run.state["campaign_work_base"] = (run.decisions,
                        self._physical_action_publications-run.inputs_start, game_now(self)-run.started_at)
            if reason:
                outcome = ("fail", reason)
                run.pending = outcome
                if waiting:
                    c.mines_wait = None
            elif not arrived:
                saved = {k: v for k, v in run.state.items() if k.startswith(("campaign_", "route_"))}
                run.state = saved
                run.state["campaign_travel"] = c.travel.get("active", False)
                run.state["campaign_leg_base"] = (run.decisions,
                    self._physical_action_publications-run.inputs_start, game_now(self)-run.started_at)
                run.call, run.pending = call, None
                if waiting:
                    run.state["campaign_mines_wait"] = True
                return True
        if (c.part == "b" and c.task == "sleep" and c.days and run.state.get("campaign_travel")
                and outcome[0] == "fail" and outcome[1] in SLEEP_TRAVEL_RETRY_REASONS
                and "campaign_sleep_travel_retry" not in run.state
                and self._part_a_clean_failure(run, outcome, cleanup, sleep_travel=True)):
            # PART_B4 (journal 2585-2613): one stalled hop (Beach 40,1 -> warp 40,-1, crossing 40,0 closed) failed
            # mines, ore AND the day's sleep row, and the day ended without the home leg. The sleep row re-derives
            # its route ONCE from fresh native facts (the same travel planner; this day's observed no-effect warp
            # now ranks last) and walks on under the unchanged session input/time caps. Nothing is credited: the
            # row is verified only by the native sleep transition readback, and a second stall ends it as before.
            facts = self._skill_current_facts()
            call = self._part_a_travel_call("sleep", facts) if facts is not None else None
            if isinstance(call, S.SkillCall):
                run.state["campaign_sleep_travel_retry"] = outcome[1]
                self._journal({"ev": "part_b_sleep_travel_retry", "session_id": session.session_id,
                               "reason": outcome[1], "skill": call.name, "arg": call.arg,
                               "location": getattr(facts, "location", None)})
                saved = {k: v for k, v in run.state.items() if k.startswith("campaign_")}
                run.state = saved
                run.state["campaign_leg_base"] = (run.decisions,
                    self._physical_action_publications-run.inputs_start, game_now(self)-run.started_at)
                run.call, run.pending = call, None
                return True
        if (c.part == "b" and c.task == "talk_gift" and c.days and run.call.name == "ngobrol_hadiah"
                and outcome[0] == "fail" and str(outcome[1]).partition(":")[0] in (  # ADR 161 named the old one's cases
                    "talk_target_not_offered", "talk_target_left", "talk_nobody_new", "talk_already_greeted")
                and "campaign_social_town_retry" not in run.state
                and self._part_a_clean_failure(run, outcome, cleanup)):
            # FINAL B7 (J3303-J3325): on the Mountain S16 failed talk_target_not_offered after 5 inputs and the row
            # ended without the once-only Town route the itinerary documents (part_b.travel offers it only when NO
            # villager is offered on the Mountain). One bounded Town hop, then S16 again from fresh offers there,
            # only while the Town-detour clock projection still reaches bed before 24:00. Nothing is credited.
            from arti_stardew_part_b import _final_day, social_admission
            facts = self._skill_current_facts()
            refused = (None if facts is not None and facts.location == "Mountain" and _final_day()
                       else "not_mountain_final_day")
            if refused is None:
                refused = social_admission(facts.time_of_day, town_detour=True)
            run.state["campaign_social_town_retry"] = refused or "town"
            self._journal({"ev": "part_b_social_town_retry", "session_id": session.session_id,
                           "reason": outcome[1], "refused": refused,
                           "location": getattr(facts, "location", None), "time": getattr(facts, "time_of_day", None)})
            if refused is None:
                saved = {k: v for k, v in run.state.items() if k.startswith("campaign_")}
                run.state = saved
                run.state["campaign_travel"] = True
                run.state["campaign_leg_base"] = (run.decisions,
                    self._physical_action_publications-run.inputs_start, game_now(self)-run.started_at)
                c.legs.insert(0, run.call)  # the same S16 call, re-run from fresh Town offers after the hop
                c.travel["active"] = True
                run.call, run.pending = S.SkillCall("pergi_ke", "town"), None
                return True
        if (c.part == "a" and c.task == "storage" and run.call.name == "simpan_ke_chest"
                and outcome == ("fail", "storage_transfer_not_offered")):
            if self._part_a_storage_retry(session, run, cleanup):
                return True
            outcome = run.pending or outcome
            leg["outcome"] = list(outcome)
        if c.part in ("b", "c"):
            return self._part_b_result(session, run, outcome, cleanup, observed)
        if c.task == "crops" and outcome == ("fail", "needs_refill") and not run.state.get("campaign_refilled"):
            run.state["campaign_refilled"] = True
            c.legs.insert(0, S.SkillCall("siram"))
            call = S.SkillCall("isi_gembor")
        elif outcome[0] != "done":
            if c.preface is not None and not c.days:
                return self._part_a_finish_preface(session, run, False)
            row = c.days[-1]
            row["rows"][c.task] = False
            row["skills"].append(dict(task=c.task, status="held", reason=outcome[1],
                                      evidence=run.state["campaign_evidence"]))
            self._journal({"ev": "part_" + session.campaign.part + "_row", "session_id": session.session_id, "owner_goal_id": session.goal_id,
                           "day": len(c.days), "task": c.task, "verified": False,
                           "status": "held", "reason": outcome[1],
                           "evidence": dict(legs=run.state["campaign_evidence"])})
            executor = getattr(self._active, "animal_executor", None) or {}
            deferred = executor.get("deferred_remaining")
            from arti_stardew_animal_routine import basis
            exit_failure = (c.task == "animals" and run.call.name == "rawat_hewan"
                            and outcome[1] in ("unreachable_house:Farm", "house_route_refused:Farm", "exit_not_captured"))
            exit_hold = (exit_failure and outcome[1] == executor.get("exception") and executor.get("cleanup") is True
                         and (raw := self._animal_raw(self._water_context(self._binding_view()[1]))) is not None
                         and basis(raw) == self._active.animal_basis)
            if (self._part_a_clean_failure(run, outcome, cleanup)
                    and c.task == "animals" and run.call.name == "rawat_hewan" and deferred
                    and outcome[0] == "fail" and outcome[1] == executor.get("exception")
                    and (outcome[1] == "blocked_targets_remaining"
                         or outcome[1].startswith("trough_access_blocked_by_animal:"))
                    and executor.get("cleanup") is True
                    and isinstance(cleanup, Mapping)
                    and cleanup.get("location") == "Farm" and cleanup.get("menu") is None
                    and cleanup.get("free") is True and cleanup.get("input_released") is True
                    and (raw := self._animal_raw(self._water_context(self._binding_view()[1]))) is not None
                    and basis(raw) == self._active.animal_basis):
                # A clean Farm return with bounded obstruction debt is partial work,
                # never animal completion. Safety/budget/identity failures still hold.
                run.state["campaign_deferred"] = True
                if not c.animal_revisit:
                    routine = self._active.extras_objective.routine
                    remaining = {}
                    for house in raw["houses"]:
                        for product in house.get("products", ()):
                            remaining[product["qid"]] = remaining.get(product["qid"], 0) + product["count"]
                    # Preserve proof for already collected products before storage.
                    checks = routine.get("inventory_check", ())
                    ledger_verified = (len(checks) == len(self._active.animal_products_required)
                        and all(v.get("current") is not None and v["current"] >= v["baseline"]
                                + max(0, v["required_gain"] - remaining.get(v["qid"], 0)) for v in checks))
                    caps = self._part_a_caps(run)
                    caps = (caps[0], min(caps[1], executor["inputs_max"]), caps[2])
                    spent = (run.decisions, self._physical_action_publications-run.inputs_start,
                             game_now(self)-run.started_at)
                    c.animal_deferred = dict(refs=list(deferred), basis=self._active.animal_basis,
                        deadline=executor["deadline"], steps=executor["steps"],
                        caps=tuple(max(0, cap-used) for cap, used in zip(caps, spent)),
                        ledger_verified=ledger_verified)
                    c.animal_revisit = True
                    # A25: revisit from the Barn/Coop door now, not after storage by the house.
                    c.tasks.insert(0, "animals")
                leg["deferred_targets"] = list(deferred)
                leg["animal_inventory_check"] = self._active.extras_objective.routine.get("inventory_check", [])
                self._journal({"ev": "part_a_animals_deferred", "session_id": session.session_id,
                               "targets": list(deferred), "reason": outcome[1],
                               "evidence": dict(legs=run.state["campaign_evidence"]),
                               "remaining_caps": c.animal_deferred["caps"]})
            if (exit_hold or not deferred and not exit_failure) and self._part_a_clean_failure(run, outcome, cleanup):
                run.state["campaign_deferred"] = True
            return False
        else:
            call = None
        if c.task == "crops" and self._skill_no_dry_crops(self._build_decision_snapshot(self._active)):
            c.legs.clear()
        if c.task == "stone" and run.call.name == "pecah_batu" and outcome == ("done", "observed_tool_effect"):
            facts = self._skill_current_facts()
            before = run.state.get("campaign_inventory_start", {}).get(S.STONE, 0)
            if facts is not None and facts.inventory_current and facts.inventory.get(S.STONE, 0) > before:
                run.state["campaign_stone_gain"] = facts.inventory[S.STONE]-before
                c.legs.clear()  # native pickup already settled; never demand a second rock/drop
        if call is not None or c.legs:
            call = call or c.legs.pop(0)
            saved = {k: v for k, v in run.state.items() if k.startswith(("campaign_", "route_"))}
            run.state = saved
            run.state["campaign_leg_base"] = (run.decisions,
                self._physical_action_publications-run.inputs_start, game_now(self)-run.started_at)
            run.call, run.pending = call, None
            return True
        if c.preface is not None and not c.days:
            verified, date = self._part_a_sleep_transition(c, observed)
            return self._part_a_finish_preface(session, run, verified, date)
        row = c.days[-1]
        verified = True
        if c.task == "food":
            from arti_stardew_interact_b import eat_consumption_observed
            verified = outcome[1] == "observed_eat_effect" and eat_consumption_observed(observed)
        if c.task in ("animals", "machine", "wood", "stone", "storage"):
            expected = {"animals": "observed_all_animals_cared_for", "machine": "observed_extras_effect",
                        "wood": "observed_inventory_objective", "stone": "observed_inventory_objective",
                        "storage": "observed_paired_transfer_objective"}[c.task]
            verified = outcome[1] == expected or c.task == "stone" and run.state.get("campaign_stone_gain", 0) > 0
            if c.task == "animals":
                verified = verified and run.state.get("animal_care_verified") is True
                if c.animal_deferred:
                    verified = verified and c.animal_deferred["ledger_verified"]
        if c.task == "machine" and verified:
            driver = run.state.get("wave1_driver", "machine_collect")
            tile = run.state.get("wave1_pin", {}).get("tile", run.call.tile)
            kind = "insert" if driver == "machine_insert" else "collect"
            c.machine_cycle.append(dict(kind=kind, day=len(c.days), date=list(c.date), tile=list(tile),
                input=run.state.get("campaign_machine_input"), observed=observed))
            if kind == "insert":
                verified = False  # a load is progress; collection earns the S20 witness
                run.state["campaign_deferred"] = True
        if c.task == "clear":
            verified = (run.state.get("wave1_count", 0) > 0
                        or outcome == ("done", "observed_no_removable_debris"))
        if c.task == "crops":
            soil = self._part_a_soil()
            row["crop_after"] = {str(k): v for k, v in soil.items()}
            verified = (all(soil.get(k) is True for k in c.crops)
                        and self._skill_no_dry_crops(self._build_decision_snapshot(self._active))
                        and any(e["outcome"][1] == "observed_tool_effect" for e in run.state["campaign_evidence"]))
        if c.task == "sleep":
            verified, date = self._part_a_sleep_transition(c, observed)
            row["transition"] = list(date) if date else None
            if verified:
                c.date = date
                c.finished = len(c.days) == 3
        row["rows"][c.task] = verified
        row["skills"].append(dict(task=c.task, verified=verified,
            **({"reason": "machine_inserted_pending_collect" if c.task == "machine" and run.state.get("campaign_deferred")
               else "part_a_readback:" + c.task} if not verified else {}), evidence=run.state["campaign_evidence"]))
        self._journal({"ev": "part_" + session.campaign.part + "_row", "session_id": session.session_id, "owner_goal_id": session.goal_id,
                       "day": len(c.days), "task": c.task, "verified": verified,
                       **({"reason": row["skills"][-1]["reason"]} if not verified else {}),
                       "evidence": dict(legs=run.state["campaign_evidence"],
                           **({"machine_cycle": list(c.machine_cycle)} if c.task == "machine" else {}),
                           **({k: row[k] for k in ("crop_before", "crop_after") if k in row} if c.task == "crops" else {}))})
        if c.task == "sleep" and verified and not c.finished:
            c.morning()
        if not verified:
            run.pending = ("fail", row["skills"][-1]["reason"])
            if c.task == "food" and self._part_a_clean_failure(run, ("fail", "food_unconsumed"), cleanup):
                run.state["campaign_deferred"] = True  # retain the missing-consumption gap; continue to storage/sleep
            if c.task == "animals" and c.animal_deferred:
                run.state["campaign_deferred"] = True
        return False

    def _part_b_result(self, session, run, outcome, cleanup, observed):
        c = session.campaign
        if c.part == "c":
            from arti_stardew_part_c import verify
        else:
            from arti_stardew_part_b import verify
        if c.preface is not None and not c.days and c.task != "morning":
            if outcome[0] != "done":
                return self._part_a_finish_preface(session, run, False)
            if c.legs:
                return self._campaign_resume_leg(c, run)
            ok, date = self._part_a_sleep_transition(c, observed)
            return self._part_a_finish_preface(session, run, ok, date)
        clean = self._part_a_clean_failure(run, ("fail", "row_check"), cleanup) if c.task != "sleep" else False
        if outcome[0] == "done" and c.legs:
            return self._campaign_resume_leg(c, run)
        facts = self._skill_current_facts()
        _, pid = self._binding_view()
        motor = self._state.latest(pid)
        current = (facts is not None and facts.inventory_current and not self._state.is_stale(pid)
                   and tuple(self._active.play_identity) == c.epoch)
        ok = False
        if c.task == "sleep" and outcome[0] == "done":
            ok, date = self._part_a_sleep_transition(c, observed)
            c.days[-1]["transition"] = list(date) if date else None
            if ok:
                c.date, c.finished = date, True
        elif outcome[0] == "done" and clean and current:
            ok = verify(c.task, run, observed, facts, motor)
        if c.part == "c":
            from arti_stardew_part_c import reason as row_reason
            reason = row_reason(c.task, outcome, ok)
        else:
            reason = (outcome[1] if outcome[0] != "done"
                      else "part_b_readback:" + c.task if not ok else outcome[1])
        preface = not c.days  # Part C's approval-day S29 row
        row = c.days[-1] if c.days else c.preface
        row["rows"][c.task] = bool(ok)
        evidence = dict(legs=run.state["campaign_evidence"], native_legs=run.state.get("campaign_native_legs", {}),
            inventory_before=run.state.get("campaign_inventory_start", {}),
            inventory_after=dict(facts.inventory) if facts is not None and facts.inventory_current else None,
            location=getattr(motor, "location", None), health=getattr(motor, "health", None),
            floor=run.state.get("wave1_floor"), talk_done=run.state.get("wave1_talk_done"),
            gift_verified=run.state.get("wave1_gift_verified"))
        if c.part == "c":
            evidence["plan_input"] = dict(run.state.get("plan_input", {}))
            evidence["donation_evidence"] = dict(run.state.get("donation_evidence", {}))
            if "attempt" in run.state:
                evidence["attempt"] = {k: v for k, v in run.state["attempt"].items() if k not in ("survey", "walk_from")}
        if c.part == "b" and c.task == "ore":
            evidence["ore"] = dict(run.state.get("ore", {}).get("evidence", {}))
            evidence["alive_exit"] = run.state.get("alive_exit")
        row["skills"].append(dict(task=c.task, verified=bool(ok), reason=reason, evidence=evidence))
        self._journal(dict(ev="part_" + c.part + "_row", session_id=session.session_id, owner_goal_id=session.goal_id,
                           day=1, task=c.task, verified=bool(ok), reason=reason, evidence=evidence,
                           **({"preface": True, "date": list(c.date)} if preface else {})))
        if not ok:
            run.pending = ("fail", reason)
            # Readback failure is a named unmet row. Safety failures never continue.
            if clean and (outcome[0] == "done" or self._part_a_clean_failure(run, outcome, cleanup)):
                if (c.part == "b" and run.state.get("campaign_mine_recovery")
                        and str(getattr(motor, "location", "")).startswith("UndergroundMine")):
                    c.mine_recovery_failed = reason
                run.state["campaign_deferred"] = True
        return False

    def _campaign_resume_leg(self, c, run):
        call = c.legs.pop(0)
        run.state = {k: v for k, v in run.state.items() if k.startswith("campaign_")}
        run.state["campaign_leg_base"] = (run.decisions,
            self._physical_action_publications-run.inputs_start, game_now(self)-run.started_at)
        run.call, run.pending = call, None
        return True

    @staticmethod
    def _part_a_caps(run):
        if run.state.get("live7"):
            return (float("inf"),) * 3
        caps = PartACampaignMixin._part_a_row_caps(run)
        sleep_cap = run.state.get("campaign_sleep_input_cap")
        if type(sleep_cap) is int and run.state.get("campaign_task") == "sleep":
            # B7: the Part B reserve is cumulative from run.inputs_start. Do not let
            # home's 120 or Part A's 136-work cap strand this row at 257/273 inputs.
            # Non-travel work still keeps its own per-leg bound (e.g. tidur's 32).
            leg = run.state.get("campaign_leg_base", (0, 0, 0))
            local = sleep_cap if (run.state.get("campaign_travel")
                or run.call == S.SkillCall("pergi_ke", "home")) else leg[1] + (
                    32 if run.call.name == "tidur" else S.skill_costs(run.call)[1])
            remaining = run.state.get("campaign_travel_caps", (float("inf"),) * 3)[1]
            caps = (caps[0], min(sleep_cap, local, remaining), caps[2])
        ceiling = run.state.get("campaign_input_ceiling")
        return caps if type(ceiling) is not int else (caps[0], min(caps[1], ceiling), caps[2])

    @staticmethod
    def _part_a_row_caps(run):
        caps = S.skill_costs(run.call)
        if run.state.get("campaign_task") == "sleep" and run.call.name == "tidur":
            # A28: two occupant/blocked bumps inside the cabin spent tidur's standalone 16 inputs one
            # tile from the bed (a27: <=11). The sleep row's own 136-input total still bounds the leg.
            caps = (caps[0], 32, caps[2])
        if run.state.get("campaign_travel"):
            return run.state["campaign_travel_caps"]
        if "campaign_task" not in run.state:
            return caps
        base = run.state.get("campaign_leg_base", (0, 0, 0))
        total = {"crops": (64, 40, 300), "storage": (128, 160, 600),
                 "sleep": (64, 136, 900), "stone": (56, 42, 300)}.get(run.state["campaign_task"], caps)
        total = run.state.get("campaign_work_caps", total)
        work_base = run.state.get("campaign_work_base", (0, 0, 0))
        limits = run.state.get("campaign_travel_caps", (float("inf"),) * 3)
        return tuple(min(limit, w+t, b+x) for limit, w, t, b, x in zip(limits, work_base, total, base, caps))
