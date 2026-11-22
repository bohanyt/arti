"""Part B policy for the shared Part A campaign lifecycle. Offline readiness only."""
from collections.abc import Mapping
import time

import arti_stardew_skills as S

GOAL = ("Arti, main mandiri selama satu hari: pergi ke kota, beli benih, jual hasil panen, "
        "sapa dan beri warga yang terlihat hadiah daffodil jika diizinkan, pungut forage di perjalanan, "
        "jelajahi tambang sampai lantai lima lalu keluar hidup-hidup, coba kumpulkan bijih, "
        "tangkap ikan di danau Mountain dekat tambang, "
        "lalu pulang tidur sebelum tengah malam.")
TASKS = ("town", "buy", "sell", "talk_gift", "forage", "fish", "mines", "ore", "sleep")  # the day's row ledger
# Owner 2026-10-20: shops, forage on the way, the Mines, Mountain lake, home. Inside the single Mines
# visit S23 runs first: both rows start at the native Mine entrance, and S15's alive exit no longer
# pushes S23's own 09:00-18:00 entry past 18:00. S16 follows the Mines with an offered villager.
# forage_in_wait() moves S21 into the pre-09:00 shop wait, else it follows the shops before the trek.
# Historical diagnostic policies keep their own order.
ORDERS = {"mines_day": ("mines", "ore", "sleep"),
          "final_day": ("town", "buy", "sell", "forage", "ore", "mines", "talk_gift", "fish", "sleep"),
          "shop_day": ("town", "buy", "sell", "talk_gift", "forage", "fish", "mines", "ore", "sleep")}  # B5 order
ORDER = ORDERS["final_day"]
NOT_SCHEDULED = "part_b_order:not_scheduled_mines_day"
# Part B sleep input reserve (cf. Part A SLEEP_RESERVE), measured on the 1,300 cap and scaled down for a smaller
# approval: worst home leg = Beach->Town->BusStop (B5 mines row 170) + BusStop->Farm->Cabin+tidur (B5 sleep 93)
# = 263; the estimated Mine->Mountain->Backwoods->Farm->Cabin leg is 198-252 + tidur 11. No non-sleep row may
# start below reserve + its own need or spend into the reserve.
SLEEP_RESERVE = 280
SLEEP_RESERVED = "part_b_budget:sleep_reserved"
ROW_INPUTS = {"mines": 572}  # Cabin->Mine trek <=252 (estimate) + lawan_mine's own 320-input cap (owner 2026-10-18)
MINE_TREK_INPUTS = 252  # the trek share of ROW_INPUTS["mines"]; the final day charges it to the row that walks it
# Historical diagnostic exit deadline; the final route fishes beside the Mine
# and returns via Backwoods, so its shorter home leg permits an evening exit.
MINES_EXIT_DUE = 1700


def schedule():
    """The day's task list: ORDER's work rows, then every unscheduled ledger row (named, never run), then sleep."""
    work = [t for t in ORDER if t != "sleep"]
    return work + [t for t in TASKS if t not in ORDER] + ["sleep"]
NAMES = frozenset({"pergi_ke", "beli", "jual", "ngobrol_hadiah", "forage", "tangkap_ikan",
                   "lawan_mine", "tambang_bijih", "tidur"})
# B1 (journal 153-342): `pergi_ke town` from the Cabin ran on S7's standalone 32-input cap and held at Farm 65,17
# after 33 inputs. Live B21 needed 13/54/38 inputs for the SeedShop/Town/BusStop crossings, so every hop,
# including BusStop->Town, now runs as a travel leg; the final hop IS the row's own S7 call (see
# PartACampaignMixin._part_a_leg) and its native transition/arrival is the town readback.
MINES_ENTER_LAST = 1800  # skills.wave1_plan: lawan_mine enters only 09:00-18:00
FORAGE_LOCATIONS = ("BusStop", "Town", "Beach")
FORAGE_RETURN_HOPS = 2  # cumulative remembered-location hops, never an exploration itinerary
FORAGE_RETURN_INPUT_ALLOWANCE = 160  # conservative admission estimate per shipped hop
FORAGE_APPROACH_LEGS = 8  # current-grid waypoints only; no survey or repeated waypoint
FINAL_FORAGE_INPUTS = 160  # cumulative travel + approach + S21, inside the unchanged sleep reserve
FINAL_FORAGE_HOP_INPUTS = 60  # admission estimate only; the cumulative ceiling owns actual publications
FINAL_FORAGE_RETURN_LAST = 1300  # stop chasing memories after lunch; no Beach fallback on the final day
# FINAL B3 (J L697-L840): a fresh BusStop offer 10 tiles away (inside FORAGE_REACH) needed a ~38-cell
# observed detour; S21 started at once and its own 30-input cap ended after 23 cells (30 inputs, 7 of
# them pixel-only, 0.77 cells/input) with 0 pickups. S21 keeps its cap for the last cells + pickup
# (select/face/press): at most this many observed-open cells are left to it; the campaign walks the rest
# as ordinary current-grid waypoint legs charged to the unchanged 160-input row ceiling (PROOF B8 shape).
FORAGE_S21_WALK_CELLS = 12
MINES_OPEN = 900
MINES_WAIT_SECONDS = 180.0
# Reference costs, not whole-day live proof. FINAL B J784/995/1146 measured
# Mine->Farm 1+32+19=52; Farm->BusStop 23 before the interruption. B10 supplies
# BusStop->Town 38, shop/forage/fish/home costs. S15 gap8k: 158.1 wall seconds.
# S23's 20-input witness began at a floor-4 node: lobby search is EXTRA.
CLOCK_INPUTS = dict(ore=20, town=52+23+38, buy=84, sell=10, talk_gift=14,
                    forage=122, fish=22, sleep=172+93)
MINE_TO_MOUNTAIN_INPUTS = 1  # FINAL B J784: native Mine->Mountain exit
FINAL_CLOCK_INPUTS = dict(town=57, buy=67, sell=8, talk_gift=14, forage=52,
                         mines=142, ore=100, fish=11, sleep=84)
# B4/B5 native row clocks include latency/waits which a per-input travel proxy omits.
# S23 100 is a search allowance (within its 120 cap), S15 142 is gap8k's input witness.
FINAL_CLOCK_MINUTES = dict(town=80, forage=90, buy=110, sell=10, mines_travel=140,
                           talk_gift=30, fish=60, sleep=160)
# The sleep admission keeps the earlier conservative home allowance, not the 84-input witness.
FINAL_HOME_ALLOWANCE_INPUTS = 51 + 55
GAME_MIN_PER_INPUT = 1.25  # upper measured travel pace (PLAN Mines timing)
GAME_MIN_PER_SECOND = 1.47
# Sleep is mandatory and its unchanged verifier needs ARTI's observed bed_time < 24:00
# (part_a._part_a_sleep_transition). Final-day fish and the S15 exit-by are derived back from it.
BED_BEFORE = 2400
WORST_MINES_EXIT_SECONDS = 56  # B35 a1: floor 2 -> Mine alive exit, the worst recorded (wall s)
FISH_SKILL_INPUTS = S.skill_costs(S.SkillCall("tangkap_ikan"))[1]  # S14's own input cap (72; B4/B5/B10 used 22-24)


def _minutes(hhmm):
    return hhmm // 100 * 60 + hhmm % 100


def final_fish_home_minutes(game_min_per_input=GAME_MIN_PER_INPUT):
    """Game minutes a final-day fish row must still have before the bed limit.

    Mine->Mountain plus the fish row at S14's whole input cap (the measured 22 leaves the
    cap's remainder as the search/approach margin), then the measured home leg (FINAL
    Mountain->Backwoods 32 + Backwoods->Farm 19, B10 Farm->Cabin 44 + tidur 11).
    Bite waits are game time without inputs: they remain unmeasured.
    """
    return (MINE_TO_MOUNTAIN_INPUTS + FISH_SKILL_INPUTS + FINAL_HOME_ALLOWANCE_INPUTS) * game_min_per_input


def _final_mines_exit_due():
    """S15 exit-by: bed limit minus the worst recorded exit, then fish + home as above.

    Floored to the game's 10-minute clock tick. 1440-82.3-91.3-132.5 game min = 18:54 -> 18:50.
    """
    minute = _minutes(BED_BEFORE) - WORST_MINES_EXIT_SECONDS * GAME_MIN_PER_SECOND - final_fish_home_minutes()
    minute = int(minute) // 10 * 10
    return minute // 60 * 100 + minute % 60


FINAL_MINES_EXIT_DUE = _final_mines_exit_due()  # 1850; replaces the fixed 20:00 (projected bed ~24:04)
S15_WITNESS_SECONDS = 158.1  # gap8k: floor 5 + native kill + alive exit, standalone


def _final_ore_search_due():
    """FINAL B3: S23 starts no new ladder search (stone break / descent) from this clock on the final day.

    S23 runs before S15 inside the one Mines visit; its new floor search must not push S15's entry past
    the latest one that still lets the gap8k witness end by the S15 exit-by at the same slower rate:
    18:50 - 158.1 s x 1.47 = 14:57.6, floored to the 10-minute tick = 14:50. An offered node is still
    mined after it; this is a turn-around for search only, never a cap, grant or verifier change.
    """
    minute = _minutes(FINAL_MINES_EXIT_DUE) - S15_WITNESS_SECONDS * GAME_MIN_PER_SECOND
    minute = int(minute) // 10 * 10
    return minute // 60 * 100 + minute % 60


FINAL_ORE_SEARCH_DUE = _final_ore_search_due()  # 1450


def ore_search_due():
    return FINAL_ORE_SEARCH_DUE if _final_day() else None


def fish_admission(time_of_day, *, travel_inputs=0):
    """None when a final-day fish row started now still projects the bed before 24:00, else a refusal.

    Protects the mandatory sleep row over fish. Never a cap, grant or verifier change.
    """
    if type(time_of_day) is not int:
        return "part_b_fish:bed_projection_clock_unknown"
    if _minutes(time_of_day) + travel_inputs * GAME_MIN_PER_INPUT + final_fish_home_minutes() >= _minutes(BED_BEFORE):
        return "part_b_fish:bed_before_2400_projection"
    return None


# B3/B4/B5 Town->Mountain crossing: 35 inputs. Reverse is a proxy, not a witnessed return.
SOCIAL_TOWN_HOP_INPUTS = 35
SOCIAL_TOWN_ROUNDTRIP_INPUTS = 2 * SOCIAL_TOWN_HOP_INPUTS


def social_admission(time_of_day, *, town_detour=False, town_return_only=False):
    """Measured social work plus fish/home; unknown NPC pursuit is refused, never waived."""
    if type(time_of_day) is not int:
        return "part_b_talk_gift:bed_projection_clock_unknown"
    need = FINAL_CLOCK_MINUTES["talk_gift"]
    if town_detour:
        need += SOCIAL_TOWN_ROUNDTRIP_INPUTS * GAME_MIN_PER_INPUT
    elif town_return_only:
        need += SOCIAL_TOWN_HOP_INPUTS * GAME_MIN_PER_INPUT
    if _minutes(time_of_day) + need + final_fish_home_minutes() >= _minutes(BED_BEFORE):
        return "part_b_talk_gift:bed_before_2400_projection"
    return None


def clock_plan(tasks, start=MINES_OPEN, *, inputs=None):
    """Conservative reference row clocks in game minutes; missing search costs remain extra."""
    minute = start // 100 * 60 + start % 100
    rows = []
    inputs = CLOCK_INPUTS if inputs is None else inputs
    for task in tasks:
        duration = 158.1 * GAME_MIN_PER_SECOND if task == "mines" else inputs[task] * GAME_MIN_PER_INPUT
        rows.append(dict(task=task, start=minute, end=minute+duration))
        minute += duration
    return rows


def final_clock_plan(start=620, *, game_min_per_input=GAME_MIN_PER_INPUT,
                     game_min_per_second=1.32, mine_travel_inputs=77, forage_in_wait=True,
                     ore_seconds=90, mines_seconds=133, social_town_detour=False):
    """Measured B4/B5 row clocks and an explicit wider-ladder hypothesis, NOT live fit proof.

    The 77-input connector actually took 140 native minutes. Moving S16 saves 20-30 minutes,
    so this evidence supports entry ~13:20-13:30, not a guaranteed 12:50. S23's 90 wall s
    and S15's 133 wall s assume a normal floor-1 search and 15-20 s ladder walks on floors 2-4.
    Callers can substitute tails/misses instead of silently projecting successful rows.
    """
    minute = start // 100 * 60 + start % 100
    rows = []
    inputs = dict(FINAL_CLOCK_INPUTS, mines_travel=mine_travel_inputs)

    def add(name):
        nonlocal minute
        duration = (mines_seconds * game_min_per_second if name == "mines"
                    else ore_seconds * game_min_per_second if name == "ore"
                    else FINAL_CLOCK_MINUTES["mines_travel"] * mine_travel_inputs / 77 if name == "mines_travel"
                    else FINAL_CLOCK_MINUTES[name])
        rows.append(dict(task=name, inputs=inputs[name], start=minute, end=minute+duration))
        minute += duration

    first_mine = next(t for t in ORDERS["final_day"] if t in ("mines", "ore"))
    for task in ORDERS["final_day"]:
        if task == "forage" and any(r["task"] == "forage" for r in rows):
            continue
        if task == "buy":
            if forage_in_wait and minute < 9*60:
                add("forage")  # admitted only while the shop is still shut
            minute = max(minute, 9*60)  # existing no-input shop wait
        if task == first_mine:
            add("mines_travel")
        if task == "talk_gift":
            inputs["fish"] -= MINE_TO_MOUNTAIN_INPUTS
        if task == "talk_gift" and social_town_detour:
            inputs["social_travel"] = MINE_TO_MOUNTAIN_INPUTS + SOCIAL_TOWN_ROUNDTRIP_INPUTS
            duration = inputs["social_travel"] * game_min_per_input
            rows.append(dict(task="social_travel", inputs=inputs["social_travel"], start=minute, end=minute+duration))
            minute += duration
        elif task == "talk_gift":
            # The native Mine->Mountain exit used to belong to fish; the total is unchanged.
            inputs["talk_gift"] += MINE_TO_MOUNTAIN_INPUTS
        add(task)
    return rows


def _final_day():
    return ORDER == ORDERS["final_day"]


def final_day_routes(runtime):
    """The FINAL Part B Town<->Mountain itinerary, only inside an active Part B final-day campaign.

    Every other part, standalone skill and helper keeps the shipped itinerary graph.
    """
    campaign = getattr(getattr(runtime, "_skill_session", None), "campaign", None)
    if getattr(campaign, "part", None) != "b" or not _final_day():
        return ()
    from arti_stardew_goal_routes import PART_B_FINAL_ITINERARIES
    return tuple(PART_B_FINAL_ITINERARIES.values())


def mines_exit_due():
    return FINAL_MINES_EXIT_DUE if _final_day() else MINES_EXIT_DUE


def _forage_allowance():
    return FINAL_FORAGE_HOP_INPUTS if _final_day() else FORAGE_RETURN_INPUT_ALLOWANCE


def row_inputs(task, location):
    """Per-row admission need beyond the unchanged sleep reserve (never a cap or a grant).

    The final day charges the Mine trek once, to the Mine row that actually walks it: S23
    from Town needs trek + its own cap; S15 starting at the entrance needs only its own cap.
    """
    if not _final_day() or task not in ("mines", "ore"):
        return ROW_INPUTS.get(task, 0)
    own = S.skill_costs(S.SkillCall("lawan_mine" if task == "mines" else "tambang_bijih"))[1]
    at_mine = location == "Mine" or str(location).startswith("UndergroundMine")
    return own if at_mine else MINE_TREK_INPUTS + own


SHOP_OPEN = 900


def forage_in_wait(runtime, session, time_of_day):
    """Move S21 into the otherwise idle pre-09:00 shop wait (final day, once, no input spent here).

    The row keeps its ordinary travel, 160-input ceiling, 13:00 memory bound, fresh offer and
    paired pickup verifier. Returns True when the caller should run "forage" now.
    """
    c = session.campaign
    if (not _final_day() or c is None or c.part != "b" or not c.days or "forage" not in c.tasks
            or type(time_of_day) is not int or time_of_day >= SHOP_OPEN
            or "forage_in_wait" in c.days[-1]):
        return False
    c.tasks.remove("forage")
    c.tasks.insert(0, "buy")
    c.days[-1]["forage_in_wait"] = dict(status="started", time=time_of_day)
    _forage_journal(runtime, "part_b_forage_in_wait", session_id=session.session_id,
                    time=time_of_day, until=SHOP_OPEN)
    return True


def defer_forage(runtime, session, reason):
    """No forage target before 09:00: restore S21's owner slot instead of recording the row unmet.

    Only for the in-wait attempt, only before any S21 start/input, and only once per day. The later
    attempt is the post-shop remembered-target row with its own unchanged bounds.
    """
    c = session.campaign
    wait = c.days[-1].get("forage_in_wait") if c is not None and c.days else None
    if not isinstance(wait, dict) or wait.get("status") != "started" or c.task != "forage":
        return False
    wait.update(status="deferred", reason=reason)
    later = [i for i, t in enumerate(c.tasks) if t in ORDER and ORDER.index(t) > ORDER.index("forage")]
    c.tasks.insert(later[0] if later else len(c.tasks), "forage")
    _forage_journal(runtime, "part_b_forage_deferred", session_id=session.session_id, reason=reason,
                    tasks=list(c.tasks))
    return True


MINES_WAITING = "part_b_mines_wait:waiting"  # re-entrant hold, never a row result


def _mines_wait_state(c, task):
    wait = getattr(c, "mines_wait", None)
    return wait if wait is not None and wait["task"] == task and wait["date"] == c.date else None


def _mines_wait_admission(runtime, session, task, facts):
    """Only THIS row's own window, clock and input fit; later rows keep their own checks."""
    costs = FINAL_CLOCK_INPUTS if _final_day() else CLOCK_INPUTS
    row = clock_plan([task], inputs=costs)[0]
    reason = None
    if row["start"] >= 21*60 or row["end"] >= _minutes(mines_exit_due()):
        reason = "curfew:" + task
    remaining = runtime._part_a_inputs_remaining()
    work = S.skill_costs(S.SkillCall("lawan_mine" if task == "mines" else "tambang_bijih"))[1]
    need = max(work, 142 if task == "mines" else costs["ore"]) + runtime._part_a_reserve(SLEEP_RESERVE, "b")
    if remaining is None or remaining < need:
        reason = "input_budget"
    wait_minutes = 9*60 - (facts.time_of_day // 100 * 60 + facts.time_of_day % 100)
    wait_seconds = wait_minutes / 1.29 + 2  # slow end of measured native pace, plus readback
    deadline_ms = getattr(runtime, "_trial_deadline_unix_ms", None)
    if deadline_ms is not None:
        seconds_left = deadline_ms/1000 - time.time()  # the approved trial seconds/expiry
    else:
        from arti_stardew_part_a import limits_for
        seconds_left = limits_for("b")["seconds"] - (time.monotonic() - session.started_at)
    row_seconds = (row["end"]-row["start"]) / 1.29
    if wait_seconds > MINES_WAIT_SECONDS or seconds_left < wait_seconds + row_seconds + 30:
        reason = "time_budget"
    return reason, dict(clock_plan=[row], remaining=remaining, need=need, wait_seconds=wait_seconds)


def mine_wait(runtime, task, facts):
    """One read-only lobby sample: None at the opening tick, MINES_WAITING to re-poll, else a refusal.

    It never sleeps, so no caller holds the runtime lock across the wait. The campaign
    boundary re-polls without the lock (cf. the Part B shop wait). Never spends a
    start/input, renews caps, or waits past the row's own deadline.
    """
    session = runtime._skill_session
    c = session.campaign
    wait = _mines_wait_state(c, task)
    if wait is None:
        reason, detail = _mines_wait_admission(runtime, session, task, facts)
        if reason:
            c.mines_wait = None
            runtime._journal(dict(ev="part_b_mines_wait_refused", task=task, reason=reason, clock_plan=detail["clock_plan"],
                                  remaining=detail["remaining"], need=detail["need"]))
            return "part_b_mines_wait:" + reason
        wait = c.mines_wait = dict(task=task, date=c.date, goal=runtime._active, time=facts.time_of_day,
                                   started=time.monotonic(), max_seconds=min(MINES_WAIT_SECONDS, detail["wait_seconds"]))
        runtime._journal(dict(ev="part_b_mines_wait", task=task, until=MINES_OPEN,
                              clock_plan=detail["clock_plan"], max_seconds=MINES_WAIT_SECONDS))
    held = _mines_wait_sample(runtime, session, c, wait)
    if held != MINES_WAITING:
        c.mines_wait = None
    return held


def _mines_wait_sample(runtime, session, c, wait):
    goal = wait["goal"]
    binding, pid = runtime._binding_view()
    motor = runtime._state.latest(pid)
    state = runtime.skill_post_row_state()  # read-only: the wait never presses anything
    current = runtime._skill_current_facts()
    if (runtime._action_cancel.is_set() or runtime._worker_stop.is_set()
            or runtime._active is not goal or session.status != "active"
            or binding.status != "bound" or runtime._state.is_stale(pid)
            or tuple(goal.play_identity) != c.epoch or runtime._date_tuple(motor) != c.date
            or current is None or not current.inventory_current or current.location != "Mine"
            or type(current.time_of_day) is not int or current.time_of_day < wait["time"]
            or not current.player_free or runtime._gather_mutation_unresolved
            or any(state.get(k) is not True for k in ("free", "input_released"))
            or state.get("menu") is not None):
        return "part_b_mines_wait:context_changed"
    if current.time_of_day >= MINES_OPEN:
        # A delayed clock sample cannot admit a dive beyond the planned opening tick.
        if current.time_of_day != MINES_OPEN:
            return "part_b_mines_wait:opening_tick_missed"
        runtime._journal(dict(ev="part_b_mines_wait_done", task=wait["task"], time=current.time_of_day,
                              waited_s=round(time.monotonic()-wait["started"], 2)))
        return None
    if time.monotonic()-wait["started"] >= wait["max_seconds"]:
        return "part_b_mines_wait:expired"
    return MINES_WAITING


def _forage_epoch_skip(runtime, c, pin):
    """None when this view belongs to the campaign's native epoch, else a short skip reason."""
    identity = getattr(getattr(runtime, "_active", None), "play_identity", None)
    if identity is not None:
        return None if tuple(identity) == c.epoch else "epoch_changed"
    # Proof B2/B3: S7/S21 leg goals never carry a play identity (only local_activity grants set
    # it), so B2 crashed on tuple(None) and B3's guard silently dropped every sighting. Fall back
    # to the epoch's observation session/player/screen, which _approach_targets pinned freshly.
    if pin is None or len(c.epoch) != 5:
        return "identity_unavailable"
    view = (pin.observation_session_id, pin.player_id, pin.screen_id)
    return None if tuple(c.epoch[2:]) == view else "epoch_changed"


def observe_forage(runtime, location, facts, motor, pin=None):
    """Remember native sightings, never pickup credit or an executable cached target.

    Called only behind _approach_targets' native freshness/session/screen checks.
    Navigation keeps its own offers; S21 re-observes before using its normal driver.
    """
    import arti_stardew_interact as interact
    session = getattr(runtime, "_skill_session", None)
    c = getattr(session, "campaign", None)
    if (c is None or session.status != "active" or c.part != "b" or not c.days or location not in FORAGE_LOCATIONS
            or not (c.task == "forage" or "forage" in c.tasks)):
        return
    skip = ("date_changed" if runtime._date_tuple(motor) != c.date else _forage_epoch_skip(runtime, c, pin))
    # FINAL B3: the offer a campaign walk is under way to stays in view beyond FORAGE_REACH while the fresh
    # scene still reports it (the walk's west detour leaves the 12-tile catalog, as in B2 J785-J787).
    keep = c.travel.get("forage_walk_ref") if c.task == "forage" else None
    entries = [] if skip else interact.forage_entries(facts, tuple(motor.tile),
                                                      interact.InteractObjective("forage_pickup"),
                                                      interact.blocked_tiles(facts), keep_ref=keep)
    refs = [e["ref"] for e in entries]
    if skip:
        pass
    elif c.task == "forage":
        # The offers' own native reach tiles ride along (JSON-safe): the final-day walk below
        # measures the observed-open path to them; they are never an executable target or credit.
        c.travel["forage_current"] = dict(location=location, refs=refs,
                                          reach=[[list(t) for t in e.get("reach_tiles", ())] for e in entries])
    elif c.travel.get("active") and refs:
        memory = c.native_targets.setdefault("forage_onway", {}).setdefault(location, [])
        remembered = {e["ref"]: e for e in memory}
        remembered.update((e["ref"], dict(ref=e["ref"], tile=list(e["tile"]))) for e in entries)
        memory[:] = remembered.values()
    # B3 had no trail at all: journal each distinct (task, location, outcome) once per day.
    seen = c.travel.setdefault("forage_sight_log", [])  # JSON-safe: travel may be journaled
    key = "|".join((str(c.task), location, str(skip), ",".join(refs)))
    journal = getattr(runtime, "_journal", None)
    if key not in seen and callable(journal):
        seen.append(key)
        journal({"ev": "part_b_forage_sighting", "task": c.task, "location": location,
                 "skip": skip, "refs": refs[:4], "travel_active": bool(c.travel.get("active"))})


def _forage_available(runtime, c):
    # input_ceiling already excludes the sleep reserve. Charge every return and
    # approach publication against it, including those spent on an empty arrival.
    used = runtime._physical_action_publications
    start = c.travel.setdefault("forage_inputs_start", used)
    return max(0, c.travel.get("input_ceiling", 0) - (used - start))


def _forage_journal(runtime, event, **fields):
    journal = getattr(runtime, "_journal", None)
    if callable(journal):
        journal(dict(ev=event, **fields))


def _forage_empty(runtime, location, reason):
    c = runtime._skill_session.campaign
    tried = c.travel.setdefault("forage_tried", [])
    if location not in tried:
        tried.append(location)
        _forage_journal(runtime, "part_b_forage_empty", location=location, reason=reason,
                        inputs_available=_forage_available(runtime, c))
    c.travel.pop("forage_return", None)
    c.travel.pop("forage_approach_start", None)


def _forage_return_late(time_of_day):
    """Final day: no walk to a remembered spot starts at/after 13:00 (or on an unknown clock)."""
    return _final_day() and (type(time_of_day) is not int or time_of_day >= FINAL_FORAGE_RETURN_LAST)


def forage_destination(runtime, location, time_of_day=None):
    """Fresh local offer, then bounded untried memories; final day never detours to Beach.

    Final day: the 13:00 bound refuses only remembered-spot returns/approaches; a fresh
    offer at ARTI's current Town/BusStop position needs no walk and stays eligible.
    """
    from arti_stardew_goal_routes import next_hop
    c = runtime._skill_session.campaign
    if c is None:
        return "Beach"
    view = c.travel.get("forage_current", {})
    available = _forage_available(runtime, c)
    if (view.get("location") == location and view.get("refs")
            and (not _final_day() or location in ("Town", "BusStop"))):
        chosen, hops = location, 0
    elif _forage_return_late(time_of_day):
        c.travel.pop("forage_return", None)
        return "part_b_forage:return_clock_bound"
    elif c.travel.get("forage_return"):
        return c.travel["forage_return"]
    else:
        chosen, hops = None, 0
    # Leave the policy's per-hop estimate plus the whole pickup cap outside the
    # sleep reserve. Final-day returns also share a hard 160-input row ceiling.
    pickup_inputs = S.skill_costs(S.SkillCall("forage"))[1]
    candidates = []
    remaining_hops = FORAGE_RETURN_HOPS - c.travel.get("forage_return_hops", 0)
    for destination in c.native_targets.get("forage_onway", {}) if chosen is None else ():
        if _final_day() and destination not in ("Town", "BusStop"):
            continue
        if destination in c.travel.get("forage_tried", ()):
            continue
        here = location
        for hops in range(remaining_hops + 1):
            if here == destination:
                if available >= hops * _forage_allowance() + pickup_inputs:
                    candidates.append((hops, destination))
                break
            here = next_hop(here, destination, final_day_routes(runtime))
            if here is None:
                break
    if candidates:
        hops, chosen = min(candidates)
        c.travel["forage_return"] = chosen
        c.travel["forage_return_hops"] = c.travel.get("forage_return_hops", 0) + hops
    c.travel["forage_return_done"] = chosen is None
    if chosen is None and _final_day():
        return "part_b_forage:no_fresh_or_remembered_target"
    chosen = chosen or "Beach"
    if c.travel.get("forage_last_destination") != chosen:
        c.travel["forage_last_destination"] = chosen
        _forage_journal(runtime, "part_b_forage_destination", location=location, destination=chosen,
                        hops=hops if candidates else 0, inputs_available=available)
    return chosen


def _forage_approach(runtime, snap, location):
    """A memory authorizes walking only. The ordinary S21 driver still needs a fresh offer."""
    from arti_stardew_animal_routine import actor_cell_open
    from arti_stardew_goal_routes import waypoint
    import arti_stardew_interact as interact
    c = runtime._skill_session.campaign
    here = tuple(snap.motor.tile)
    targets = c.native_targets.get("forage_onway", {}).get(location, ())
    # Once all remembered tiles are in catalog reach, this fresh empty look is
    # conclusive for this attempt. Never execute a ref or infer a pickup from it.
    distant = [tuple(e["tile"]) for e in targets
               if max(abs(e["tile"][i] - here[i]) for i in (0, 1)) > interact.FORAGE_REACH]
    if not distant:
        return "no_fresh_offer_in_reach"
    pickup_inputs = S.skill_costs(S.SkillCall("forage"))[1]
    used = runtime._physical_action_publications
    start = c.travel.setdefault("forage_approach_start", used)
    if (used - start >= _forage_allowance()
            or _forage_available(runtime, c) < _forage_allowance() + pickup_inputs):
        return "approach_input_allowance"
    visited = c.travel.setdefault("forage_waypoints", [])
    if len(visited) >= FORAGE_APPROACH_LEGS:
        return "approach_leg_cap"
    pin = snap.water_context or snap.movement_context
    grid = actor_cell_open(runtime._nav_grid(pin), here)
    target = min(distant, key=lambda t: (abs(t[0]-here[0])+abs(t[1]-here[1]), t))
    # waypoint() uses only observed-open paths toward this tile. Unlike
    # house_waypoint(), it never surveys an unseen frontier to explore a map.
    tile = waypoint(grid, here, target)
    key = [location, list(tile)] if tile is not None else None
    if tile is None or key in visited:
        return "no_observed_approach"
    visited.append(key)
    return runtime._campaign_travel_waypoint("forage", tile)


def _forage_fresh_walk(runtime, snap, location, view, time_of_day):
    """Final day: walk most of the observed-open path to a fresh in-reach offer, or None (S21 now).

    A fresh offer only authorizes walking. S21 still re-observes, approaches and owns the paired
    pickup verifier. None (unchanged behaviour) when the fresh grid shows no open path, the path
    already fits S21's own cap, the clock is past the memory-walk bound, or the shared approach
    allowance / waypoint legs are spent. Every leg is a current-grid waypoint (<= WAYPOINT_PATH_MAX
    cells), charged to the same 160-input row ceiling as return hops and remembered approaches.
    """
    from arti_stardew_animal_routine import actor_cell_open
    from arti_stardew_goal_routes import open_paths, open_path, WAYPOINT_PATH_MAX
    if not _final_day() or _forage_return_late(time_of_day):
        return None
    c = runtime._skill_session.campaign
    here = tuple(snap.motor.tile)
    pin = snap.water_context or snap.movement_context
    grid = actor_cell_open(runtime._nav_grid(pin), here) if pin is not None else None
    parents = open_paths(grid, here) if grid else {}
    paths = [(p, ref) for ref, reach in zip(view.get("refs", ()), view.get("reach", ())) for t in reach
             if (p := open_path(parents, tuple(t))) is not None]
    if not paths:
        return None
    path, ref = min(paths, key=lambda pr: (len(pr[0]), pr[0][-1], pr[1]))
    if len(path) - 1 <= FORAGE_S21_WALK_CELLS:
        return None
    used = runtime._physical_action_publications
    start = c.travel.setdefault("forage_approach_start", used)
    pickup_inputs = S.skill_costs(S.SkillCall("forage"))[1]
    visited = c.travel.setdefault("forage_waypoints", [])
    if (used - start >= _forage_allowance() or _forage_available(runtime, c) < _forage_allowance() + pickup_inputs
            or len(visited) >= FORAGE_APPROACH_LEGS):
        return None
    tile = path[min(WAYPOINT_PATH_MAX, len(path) - 1 - FORAGE_S21_WALK_CELLS)]
    key = [location, list(tile)]
    if key in visited:
        return None
    visited.append(key)
    c.travel["forage_walk_ref"] = ref
    _forage_journal(runtime, "part_b_forage_fresh_walk", ref=ref, location=location, path_cells=len(path) - 1,
                    waypoint=list(tile), legs=len(visited))
    return runtime._campaign_travel_waypoint("forage", tile)



def recognizes(text):
    from arti_stardew_part_a import recognizes_goal
    return recognizes_goal(text, GOAL)


def calls(task):
    C = S.SkillCall
    return {"town": [C("pergi_ke", "town")], "buy": [C("beli", "benih", 1)],
            "sell": [C("jual", "parsnip", 1)],
            "talk_gift": [C("ngobrol_hadiah", "daffodil ke warga" if _final_day() else "daffodil ke pierre")],
            "forage": [C("forage")], "fish": [C("tangkap_ikan")],
            "mines": [C("lawan_mine")], "ore": [C("tambang_bijih")],
            "sleep": [C("pergi_ke", "home"), C("tidur")]}[task]


def travel(runtime, task, facts):
    """Existing itinerary hops plus direct native coastal/Mine exits; no invented exit."""
    from arti_stardew_goal_routes import next_hop
    c = runtime._skill_session.campaign
    if str(facts.location).startswith("UndergroundMine"):
        # B6: a floor has native up-ladders, no S7 warp to the entrance. The runtime turns this
        # travel hop into the bounded native Leave leg before any row admission or normal travel.
        return S.SkillCall("pergi_ke", "mine")
    if task == "forage" and _final_day():
        if c is None:
            return "part_b_forage:campaign_required"
        # The runtime copies this ceiling into the run before starting any leg,
        # so return travel, remembered approaches AND the pickup share one bound.
        c.travel["input_ceiling"] = min(c.travel.get("input_ceiling", FINAL_FORAGE_INPUTS), FINAL_FORAGE_INPUTS)
        if _forage_available(runtime, c) <= 0:
            return "part_b_forage:input_bound"
        # The 13:00 bound applies only to walking to a remembered spot (forage_destination);
        # a fresh native offer at ARTI's current Town/BusStop position stays eligible.
    if task == "fish" and _final_day():
        refused = fish_admission(facts.time_of_day,
            travel_inputs=SOCIAL_TOWN_HOP_INPUTS if facts.location == "Town" else 0)
        if refused:
            return refused
    if task == "talk_gift" and _final_day():
        refused = social_admission(facts.time_of_day, town_return_only=facts.location == "Town")
        if refused:
            return refused
    if task in ("mines", "ore") and _mines_wait_state(c, task) and facts.location != "Mine":
        c.mines_wait = None  # a held wait never travels on; the lobby context is gone
        return "part_b_mines_wait:context_changed"
    if (task in ("mines", "ore") and facts.location == "Mine" and type(facts.time_of_day) is int
            and (facts.time_of_day < MINES_OPEN or _mines_wait_state(c, task))):
        held = mine_wait(runtime, task, facts)
        if held:
            return held
    if task == "town" and facts.location == "Town":
        return None  # nothing left to travel; S7 readback still needs a transition
    destination = {"town": "Town", "buy": "SeedShop", "sell": "SeedShop", "talk_gift": "SeedShop",
                   "forage": "Beach", "fish": "Mountain" if _final_day() else "Beach", "mines": "Mine", "ore": "Mine",
                   "sleep": "Farm"}[task]
    if (task in ("mines", "ore") and type(facts.time_of_day) is int and facts.time_of_day > MINES_ENTER_LAST
            and not str(facts.location).startswith("UndergroundMine")):
        # The native Mines driver refuses entry after 18:00 (skills.wave1_plan mines_time_window); a
        # multi-hop trek to the Mine would only spend the inputs/hours the home leg needs. At the Mine
        # entrance itself (e.g. S15 after a late S23 exit) the same refusal costs a start and a brain call.
        return "part_b_" + task + ":mines_time_window_enter_0900_1800"
    if (task in ("mines", "ore") and type(facts.time_of_day) is int and facts.time_of_day >= mines_exit_due()
            and facts.location != "Mine" and not str(facts.location).startswith("UndergroundMine")):
        # PART_B_MINES_PLAN: past the campaign exit-by time a dive would be turned around at once (B5 walked
        # Beach->BusStop for 170 inputs and was held anyway); keep the hours for the home leg.
        return "part_b_" + task + ":mines_exit_due_" + str(mines_exit_due())
    if str(facts.location).startswith(("Cabin", "FarmHouse")) and task == "sleep":
        return None  # already home: the sleep row's own pergi_ke home + tidur legs need no route read
    if str(facts.location).startswith(("Cabin", "FarmHouse")) and destination != "Farm":
        # B2 (journal: town held part_b_travel:fresh_native_exits_required in the Cabin): indoors there
        # is no water/movement pin for route facts. The existing S7 Farm objective resolves only an
        # observed exit (Part A's every-morning pergi_ke farm), so leave the Cabin first.
        return S.SkillCall("pergi_ke", "farm")
    if task in ("mines", "ore") and facts.location == "Farm":
        # Use the witnessed S7 multi-hop route, including its route envelope and
        # rolling native-grid memory, rather than rebuilding it as separate goals.
        return S.SkillCall("pergi_ke", "mine")
    if task == "forage" and runtime._skill_session.campaign is not None:
        runtime._skill_session.campaign.travel.pop("forage_current", None)
    snap = runtime._build_decision_snapshot(runtime._active)
    if task == "talk_gift" and _final_day():
        from arti_stardew_native_targets import gift_villager
        from arti_stardew_economy import ItemPolicy
        if ItemPolicy.from_config(getattr(runtime, "_config", {})).refuse_gift("sell", "(O)18", None):
            return "part_b_talk_gift:gift_policy_refused"
        if facts.location in ("Mountain", "Town"):
            context = runtime._skill_wave1_context(snap)
            if gift_villager(context, snap.motor.tile) is not None:
                return None  # S16 reselects a fresh offered NPC and owns talk, approach and gift
            if facts.location == "Town":
                return "part_b_talk_gift:no_offered_villager"
            refused = social_admission(facts.time_of_day, town_detour=True)
            if refused:
                return refused
            destination = "Town"  # one route fallback; native exits, no NPC schedule/coordinates
        else:
            destination = "Mountain"
    if task == "forage":
        destination = forage_destination(runtime, facts.location, facts.time_of_day)
        if destination.startswith("part_b_forage:"):
            return destination
        if facts.location == destination:
            c = runtime._skill_session.campaign
            if c is None:
                return None
            view = c.travel.get("forage_current", {})
            if view.get("location") == facts.location and view.get("refs"):
                walk = _forage_fresh_walk(runtime, snap, facts.location, view, facts.time_of_day)
                if walk is not None:
                    return walk
                return None  # S21 itself must re-observe, approach and verify a real pickup
            if c.travel.get("forage_return") == facts.location:
                pin = snap.water_context or snap.movement_context
                if (view.get("location") != facts.location or pin is None or pin.location != facts.location
                        or runtime._goal_route_facts(pin) is None):
                    return "part_b_travel:forage:fresh_context_required"
                approach = _forage_approach(runtime, snap, facts.location)
                if not isinstance(approach, str):
                    return approach
                _forage_empty(runtime, facts.location, approach)
                destination = forage_destination(runtime, facts.location, facts.time_of_day)
                if destination.startswith("part_b_forage:"):
                    return destination
            if facts.location == destination:
                return None
    pin = snap.water_context or snap.movement_context
    raw = runtime._goal_route_facts(pin) if pin is not None else None
    if raw is None:
        return "part_b_travel:fresh_native_exits_required"
    if facts.location != destination:
        # Beach<->Town and Mountain<->Mine must be directly offered by native
        # exits. All intervening hops reuse the shipped itinerary graph.
        if facts.location == "Beach":
            hop = "Town"
        elif facts.location == "Mine" or str(facts.location).startswith("UndergroundMine"):
            hop = "Mountain" if facts.location == "Mine" else "Mine"
        elif destination == "Beach" and facts.location == "Town":
            hop = "Beach"
        elif destination == "Mine" and facts.location == "Mountain":
            hop = "Mine"
        else:
            anchor = "Town" if destination == "Beach" else "Mountain" if destination == "Mine" else destination
            hop = next_hop(facts.location, anchor, final_day_routes(runtime))
        if hop is None:
            # Farmhand wakes inside its native Cabin. The existing S7 Farm
            # objective resolves only an observed exit, never a map shortcut.
            if str(facts.location).startswith(("Cabin", "FarmHouse")):
                hop = "Farm"
            else:
                return "part_b_travel:no_shipped_itinerary"
        return S.SkillCall("pergi_ke", hop.casefold() if hop != "SeedShop" else "seed shop")
    # Existing drivers own counter, villager, forage, rod and ladder approach /
    # facing. A grid shore is approach evidence only; native fishing offers still own casting.
    if task in ("buy", "sell", "talk_gift", "fish"):
        from arti_stardew_native_targets import work_stands, fishing_search_target, FISHING_STAND_GAP
        context = runtime._skill_wave1_context(snap)
        targets = work_stands(context, "fish" if task == "fish" else "counter")
        if task == "fish" and not targets:
            state = runtime._skill_session.campaign.travel.setdefault("water_search", {})
            target = fishing_search_target(runtime._nav_grid(pin), snap.motor.tile, state)
            if target is None:
                return FISHING_STAND_GAP
            return runtime._campaign_local_travel(task, facts, (target,), exact=True)
        if not targets:
            return "part_b_target_unavailable:" + ("fishing_stand" if task == "fish" else "shop_counter")
        return runtime._campaign_local_travel(task, facts, targets, exact=True)
    return None


def verify(task, run, observed, facts, motor):
    """Reconcile native verifier ledgers with independently refreshed actor/bag."""
    before = run.state.get("campaign_inventory_start", {})
    bag = facts.inventory
    if task == "town":
        return facts.location == "Town" and observed.get("transitions", 0) > 0
    if task in ("buy", "sell"):
        from arti_stardew_seeds import CROP_SEEDS
        kind, sign = ("buy", 1) if task == "buy" else ("sell", -1)
        return facts.location == "SeedShop" and any(
            e.get("kind") == kind and type(e.get("quantity")) is int and e["quantity"] >= 1
            and type(e.get("money_delta")) is int and e["money_delta"] * sign < 0
            and (e.get("item") in CROP_SEEDS if task == "buy" else e.get("item") == "(O)24")
            and (bag.get(e["item"], 0)-before.get(e["item"], 0))*sign >= e["quantity"]
            for e in observed.get("events", ()) if isinstance(e, Mapping))
    if task == "talk_gift":
        npc = run.state.get("wave1_social_npc", "pierre")
        gifts = [leg for leg in run.state.get("campaign_native_legs", {}).values()
                 if leg.get("driver") == "gift" and leg.get("npc") == npc]
        return (run.state.get("wave1_talk_done") is True and run.state.get("wave1_gift_verified") is True
                and before.get("(O)18", 0)-bag.get("(O)18", 0) == 1
                and any(leg.get("observed", {}).get("origin") == "extras_rule" and any(
                    e.get("kind") == "npc_gift" and e.get("item") == "(O)18"
                    and e.get("delta") == -1 and e.get("value") == 1
                    and str(e.get("receiver_token", "")).startswith("rcv:")
                    for e in leg.get("observed", {}).get("effects", ()) if isinstance(e, Mapping))
                    for leg in gifts))
    if task == "forage":
        import arti_stardew_interact as interact
        pickup = observed.get("forage") or {}
        qid = pickup.get("qid")
        native_delta = pickup.get("native_delta", 1)
        return (observed.get("pickups", 0) == 1 and bool(pickup.get("request_id"))
                and bool(pickup.get("source_fact_id"))
                and pickup["source_fact_id"].startswith("obj:" + str(facts.location) + ":")
                and interact.forage_qid(pickup["source_fact_id"]) == qid and qid is not None
                and type(native_delta) is int and native_delta >= 1
                and (qid not in interact.ANIMAL_PRODUCT_IDS or native_delta == 1)
                and pickup.get("delta") == native_delta
                and set(pickup.get("verified_by", ())) in ({"inventory", "scene"}, {"inventory", "pickup_effect"})
                and bag.get(qid, 0) - before.get(qid, 0) == native_delta)
    if task == "fish":
        fish = observed.get("fish") or {}
        qid = fish.get("item") if isinstance(fish, Mapping) else None
        # PART_B4: the fishing objective already reaches CAUGHT in Part B only on the native category -4
        # (fishing.settle; catch_any_item is off for part_b), but the row ledger dropped that category, so the
        # row itself checked only the id. When the ledger carries the native category it must be the fish one.
        from arti_stardew_fishing import FISH_CATEGORY
        native_fish = (isinstance(fish, Mapping) and "category" not in fish
                       or isinstance(fish, Mapping) and type(fish["category"]) is int and fish["category"] == FISH_CATEGORY)
        return (observed.get("origin") == "fishing_rule" and bool(observed.get("request_id"))
                and qid is not None and bag.get(qid, 0) > before.get(qid, 0) and native_fish)
    if task == "mines":
        return (run.state.get("wave1_floor", 0) >= 5 and bool(observed.get("exited_mines"))
                and facts.location == "Mine" and type(motor.health) in (int, float) and motor.health > 0)
    if task == "ore":
        from arti_stardew_skills_mines import ORE_NODES
        evidence = (run.state.get("ore") or {}).get("evidence") or {}
        qid, gained = evidence.get("ore"), evidence.get("gained")
        return (ORE_NODES.get(evidence.get("node_qid")) == qid and qid is not None and type(gained) is int and gained > 0
                and evidence.get("drops_settled") is True
                and evidence.get("bag_after") == evidence.get("bag_before", 0) + gained
                and bag.get(qid, 0) - before.get(qid, 0) == gained
                and run.state.get("alive_exit") == "verified" and facts.location == "Mine"
                and type(motor.health) in (int, float) and motor.health > 0 and facts.player_free)
    return False
