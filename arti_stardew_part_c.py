"""Final Wave 1 progress day: ordinary verified skills on the shared lifecycle."""
from collections.abc import Mapping

import arti_stardew_skills as S
import arti_stardew_skills_attempts as attempts

GOAL = ("Arti, main mandiri selama satu hari: cek cuaca, keberuntungan, dan surat pagi ini, lalu besok baca surat "
        "dan antar sashimi ke Pierre untuk quest, upgrade Copper Watering Can di Clint, "
        "donasi leek ke bundle spring foraging, lalu pulang tidur sebelum tengah malam.")
# Owner 2026-10-20 (FINAL Part C variant): PLAN section 2 permits "[recorded remark omitted]"; Part C
# uses S29 on the approval (Wednesday) morning, BEFORE the preface sleep. S26 stays individually witnessed elsewhere.
# The staged quest letter is queued with mailfortomorrow, so it arrives only on Thursday: Wednesday's S29 reads the
# ordinary letters already in the mailbox, and S28 still reads/accepts its own letter on Thursday.
PREFACE_TASKS = ("morning", "sleep")
# Thursday: the timed shops before the Community Center.
# PART_C_CLOCK (docs/stardew/skills-v1/PART_C_CLOCK.md): quest -> upgrade -> bundle -> bed, all inside the native hours
# below; the CC (no hours) is last.
TASKS = ("quest", "geode", "bundle", "sleep")
ROWS = ("morning",) + TASKS  # every mandatory Part C row: the preface S29 row, then the progress day
# S29 needs about 2 game hours and its own native TV/mail window ends at 12:00 (s29w measured 36 inputs / 85 s).
MORNING_START_LATEST = 945

# Native hours, Stardew 1.6.15 Content (Maps/Town LockedDoorWarp; Characters/schedules, Thursday = "spring"):
# SeedShop door 09:00-21:00 (Pierre in the shop all day); Blacksmith door 09:00-16:00 (Clint at the counter until
# 17:00); the CC door (WarpCommunityCenter) has no hours. A shop row must reach its site before the close.
SHOP_CLOSE = {"quest": 2100, "geode": 1600}
SEEDSHOP_OPEN = 900
# S28 (a native quest title): the native Data/Quests key. The mod's quest log / letter_quest facts carry quest ids as strings
# (arti_stardew_skills_quest.quest_facts rejects anything else); FINAL C4 J L1314 bound "quest_id": "117".
QUEST_ID = "117"
# Bed must be BEFORE midnight (Part A sleep transition: bed_time < 2400, then the next morning).
BED_DEADLINE = 2400
# Measured game minutes (native clock fitted to each report's native 10-minute reads, ~1.37 game min per s).
# FINAL = run/skills-w1-final-partc (proof1); gap8t/gap8r/gap8s/gap7a = verified single-row runs. Lines are journal.
ROW_BOUNDARY = 7  # row end -> next row's first leg: FINAL L3142->L3157 7, L2293->L2309 6, L878->L893 4
QUEST_TO_DOOR = {"Farm": 192, "Town": 52}  # Cabin->Town 140 (gap8t L13->L523; FINAL L88->L597 136) + Town->door 52 (gap8t L523->L698)
QUEST_WORK = 61  # SeedShop entry -> quest verified (gap8t L698->L883)
# Leaving a site's interior: SeedShop door<->Pierre 33 (gap8t L697->L821), Blacksmith 3 (gap8r L257->L276),
# AnimalShop 4 (gap8s L118->L141), CommunityCenter -> Town 31 (FINAL L3157->L3278).
EXIT_MINUTES = {"SeedShop": 33, "Blacksmith": 3, "AnimalShop": 4, "CommunityCenter": 31}
# Outdoor area -> the row's site entry. EST = no live walk yet: measured pace of the same driver x native distance
# (pergi_ke 81 min/121 tiles FINAL L893->L1185; beli_hewan 24/31 gap8s L27->L118; donasi 65/86 FINAL L2698->L2951).
TO_SITE = {("Town", "geode"): 67,           # gap8r L13->L257 (from a start farther than the SeedShop door)
           ("Forest", "bundle"): 27 + 94,   # EST Forest->Town 27 + EST Town (Forest warp)->CC 94
           ("Town", "bundle"): 94}          # EST (the farthest Town start, the Forest warp)
WORK_MINUTES = {"geode": 77, "bundle": 152}  # gap8r L257->L376; gap7a L378->L597 (FINAL 130)
# Outdoor area -> Town for a row that starts off its planned area: BusStop 27 (gap8t L398->L523), Farm 116 (gap8t L188->L523).
AREA_TO_TOWN = {"BusStop": 27, "Farm": 116}
# Outdoor area -> asleep, including the sleep row boundary: Town->BusStop 70 (FINAL L3278->L3534), BusStop->Farm 29
# (FINAL L1185->L1307), Farm->home door 89 (reverse of gap8t L188->L398), door->bed 8 (reverse FINAL L101->L148),
# tidur 3 (FINAL L40->L66). Forest: EST Forest->Town 27 + Town (no completed proof1 Forest->Farm home walk exists).
HOME_MINUTES = {"Farm": ROW_BOUNDARY + 89 + 8 + 3}
HOME_MINUTES["BusStop"] = HOME_MINUTES["Farm"] + 29
HOME_MINUTES["Town"] = HOME_MINUTES["BusStop"] + 70
HOME_MINUTES["Forest"] = HOME_MINUTES["Town"] + 27
SITE = {"quest": "SeedShop", "geode": "Blacksmith", "bundle": "CommunityCenter"}
PARENT = dict(attempts.INTERIOR_PARENT, SeedShop="Town")
NAMES = frozenset({"cek_pagi", "pergi_ke", "upgrade_alat", "donasi", "surat_quest", "tidur"})


def recognizes(text):
    from arti_stardew_part_a import recognizes_goal
    return recognizes_goal(text, GOAL)


def calls(task):
    C = S.SkillCall
    return {"morning": [C("cek_pagi")],
            "quest": [C("surat_quest", "sashimi ke pierre")],
            "geode": [C("upgrade_alat", "copper watering can")],
            "bundle": [C("donasi", "leek ke bundle spring foraging")],
            "sleep": [C("pergi_ke", "home"), C("tidur")]}[task]


def _minutes(hhmm):
    return hhmm // 100 * 60 + hhmm % 100


def _clock(minutes):
    return minutes // 60 * 100 + minutes % 60


def area(location):
    location = str(location or "")
    if location.startswith(("Cabin", "FarmHouse")):
        return "Farm"
    return PARENT.get(location, location)


def projection(task, location, time_of_day):
    """Measured clock projection for a row starting now: (arrival, finish, bed) as hhmm, or None."""
    here, now = area(location), _minutes(time_of_day) + ROW_BOUNDARY + EXIT_MINUTES.get(str(location), 0)
    if task == "quest":
        door = QUEST_TO_DOOR.get(here)
        if door is None:
            return None
        arrival = max(now + door, _minutes(SEEDSHOP_OPEN))  # 71b16608 waits for the native door, no input
        finish = arrival + QUEST_WORK
    else:
        leg = TO_SITE.get((here, task))
        if leg is None and here in AREA_TO_TOWN and ("Town", task) in TO_SITE:
            leg = AREA_TO_TOWN[here] + TO_SITE[("Town", task)]
        if leg is None:
            return None
        arrival = now + leg
        finish = arrival + WORK_MINUTES[task]
    site = SITE[task]
    bed = finish + EXIT_MINUTES.get(site, 0) + HOME_MINUTES[area(site)]
    return dict(arrival=_clock(arrival), finish=_clock(finish), bed=_clock(bed))


def admission(task, location, time_of_day):
    """(held reason or None, projection). No row starts that would miss its shop hours or bed before 24:00."""
    if task == "morning":
        # S29 has no shop site: its own limit is the native TV/mail morning (it fails not_morning at 12:00).
        late = not 600 <= time_of_day <= MORNING_START_LATEST
        return ("part_c_clock:morning_after_" + f"{MORNING_START_LATEST:04d}" if late else None,
                dict(latest_start=MORNING_START_LATEST))
    p = projection(task, location, time_of_day)
    if p is None:
        return "part_c_clock:unprojected:" + area(location), None
    if task in SHOP_CLOSE and p["arrival"] >= SHOP_CLOSE[task]:
        return f"part_c_clock:{task}_closes_{SHOP_CLOSE[task]}", p
    if p["bed"] >= BED_DEADLINE:
        return "part_c_clock:bed_after_2400", p
    return None, p


def _direct_exit(runtime, location, destination):
    """A current native walk-onto warp from this location straight to ``destination`` (never a created exit)."""
    read = getattr(runtime, "_scene_facts_now", None)
    facts = read() if read is not None else None
    if not isinstance(facts, Mapping) or not any(
            isinstance(f, Mapping) and f.get("kind") == "location" and f.get("location_id") == location
            for f in facts.values()):
        return False
    return any(isinstance(f, Mapping) and f.get("kind") == "warp" and f.get("npc_only") is not True
               and f.get("target_location") == destination for f in facts.values())


def travel(runtime, task, facts):
    # S28 owns mailbox -> letter -> acceptance -> delivery, within its 260 cap; S29 owns its cabin TV and the
    # mailbox walk (its own morning_farm leg). Other rows start their work envelope at the witnessed outdoor
    # location; inter-site travel is campaign travel, not a renewed child-skill budget.
    if task in ("quest", "morning"):
        return None
    location = facts.location
    cabin = str(location).startswith(("Cabin", "FarmHouse"))
    if cabin:
        return None if task == "sleep" else S.SkillCall("pergi_ke", "farm")
    destination = "Farm" if task == "sleep" else "Town"
    if location == destination:
        return None
    if location in attempts.INTERIOR_PARENT:
        return S.SkillCall("pergi_ke", attempts.INTERIOR_PARENT[location].casefold())
    snap = runtime._build_decision_snapshot(runtime._active)
    pin = snap.water_context or snap.movement_context
    if pin is None or runtime._goal_route_facts(pin) is None:
        return "part_c_travel:fresh_native_exits_required"
    # PART_C_CLOCK: a direct observed native warp to the destination first (FINAL J L1332-L2292: the Farm detour
    # to the Forest spent 213 inputs and 3.5 game hours on proof1). Else the shipped itinerary.
    hop = destination if _direct_exit(runtime, location, destination) else attempts._hop(location, destination)
    return S.SkillCall("pergi_ke", hop.casefold()) if hop else "part_c_travel:no_shipped_itinerary"


def verify(task, run, observed, facts, motor):
    if not facts.player_free:
        return False
    if task == "morning":
        # S29: native TV weather + fortune text, every letter of the starting mailbox read and closed (none held),
        # the mailbox empty, the menu closed (S29's own readback and the independent motor read) and a free actor.
        p = run.state.get("plan_input", {})
        start = p.get("mailbox_start")
        return (run.call == calls(task)[0] and p.get("complete") is True and p.get("hold") is None
                and isinstance(p.get("weather"), str) and bool(p["weather"].strip())
                and isinstance(p.get("fortune"), str) and bool(p["fortune"].strip())
                and type(start) is int and start >= 0 and p.get("letters_read") == start
                and len(p.get("mail", ())) == start and all(isinstance(m, str) and m.strip() for m in p["mail"])
                and p.get("mailbox_end") == 0 and not p.get("held_letter")
                and run.state.get("morning_letter_held") is None
                and p.get("menu_closed") is True and getattr(motor, "active_menu", None) is None)
    before = run.state.get("campaign_inventory_start", {})
    bag = facts.inventory
    a = run.state.get("attempt", {})
    if task == "bundle":
        e = run.state.get("donation_evidence", {})
        return (e.get("item") == "(O)20" and e.get("bundle") == "spring foraging"
                and e.get("persistent_completed") is True and e.get("quantity") == 1
                and e.get("bag_before") == e.get("bag_after", 0) + 1
                and before.get("(O)20", 0) - bag.get("(O)20", 0) == 1
                and e.get("menu_closed") is True)
    if task == "geode":
        e = a.get("upgrade_evidence", {})
        return (run.call == calls(task)[0] and e.get("item") == "(T)CopperWateringCan"
                and e.get("required_tool") == "(T)WateringCan" and e.get("bars") == 5
                and e.get("money") == 2000 and bool(e.get("request_id"))
                and e.get("menu_closed") is True
                and before.get("(O)334", 0) - bag.get("(O)334", 0) == 5
                and before.get("(T)WateringCan", 0) - bag.get("(T)WateringCan", 0) == 1)
    if task == "quest":
        q = a.get("quest", {})
        bound = q.get("bound", {})
        return (bound.get("quest_id") == QUEST_ID and bool(bound.get("instance"))
                and bound.get("target") == "Pierre" and bound.get("item") == "(O)227" and bound.get("number") == 1
                and before.get("(O)227", 0) - bag.get("(O)227", 0) == 1
                and any(e.get("leg") == "quest_reconciled" and e.get("quest_state") in ("completed", "removed")
                        and e.get("moved") == {"(O)227": -1} and e.get("gift_counted") is False
                        and e.get("menu_closed") is True for e in a.get("legs", ())))
    return False


def reason(task, outcome, ok):
    return outcome[1] if outcome[0] != "done" or ok else "part_c_readback:" + task


def _records(campaign):
    """The preface (approval-day S29) record, then the progress day's; a run with no preface has no S29 row."""
    preface = campaign.preface if isinstance(campaign.preface, Mapping) else {}
    return [r for r in (preface, campaign.days[0] if campaign.days else {}) if r]


def unmet(campaign):
    """Every mandatory Part C row (ROWS) without its verified native readback."""
    done = {task for record in _records(campaign) for task, ok in record.get("rows", {}).items() if ok}
    return [task for task in ROWS if task not in done]


def final_reason(campaign):
    details = {r["task"]: r.get("reason", "unmet") for record in _records(campaign) for r in record.get("skills", ())}
    return "part_c_unmet:" + ",".join(f"{task}={details.get(task, 'unattempted')}" for task in unmet(campaign))
