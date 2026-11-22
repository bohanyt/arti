"""A tiny synthetic Stardew day for the Live7 offline simulator (test double only).

It renders native-shaped observations (``SkillFacts``, the wave-1 scene context,
a motor read) and applies a coarse, deterministic effect for each started skill.
It is a fixture generator, never a planner input store or a second runtime world
model: nothing in the shipped runtime imports it, and every decision about what
to offer or choose is made by the real Live7 planner/runtime code.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import heapq
import json
from pathlib import Path
from types import SimpleNamespace

import arti_stardew_skills as S

DATA = Path(__file__).with_name("spring1.json")
IDENTITY = ("sim-runtime", "sim-command", "sim-observation", -1, 0)
FLAGS = frozenset({"tool_work", "extras", "economy", "interact_b", "fishing"})
SEED = "(O)472"
BARE_CROP = dict(phase=None, qualified_item_id=None, dead=None, watered=None, ready_for_harvest=None)
DEBRIS_NAMES = ("Weeds", "Stone", "Twig")
DEBRIS_ITEM = {"Weeds": "(O)771", "Stone": "(O)390", "Twig": "(O)388"}
# Coarse game-minute cost per skill (rounded from rehearsal receipts) and energy use.
WORK = dict(cek_pagi=(40, 0), tebang=(50, 30), pecah_batu=(10, 4), bersihkan=(10, 0), cangkul=(10, 2),
            tanam=(10, 0), siram=(10, 2), beli=(20, 0))
IDLE_MINUTES = 10  # a boundary that starts nothing still lets the native clock run
EXIT_OFFSET = 3  # rows between a place's stand tile and its synthetic exit tiles


def add_minutes(hhmm, minutes):
    total = hhmm // 100 * 60 + hhmm % 100 + minutes
    return total // 60 * 100 + total % 60


def _xy(tile):
    return {"x": int(tile[0]), "y": int(tile[1])}


@dataclass
class Place:
    name: str
    tile: tuple
    exits: dict = field(default_factory=dict)
    trees: int = 0
    stones: int = 0
    debris: dict = field(default_factory=dict)      # (x, y) -> object name
    hoed: dict = field(default_factory=dict)        # (x, y) -> crop dict (BARE_CROP when empty)
    bed: tuple | None = None
    tv: tuple | None = None
    shop_counter: bool = False
    soil: bool = False                              # cardinal neighbours of the stand tile can be tilled
    soil_tiles: set | None = None                    # optional finite Diggable area (Forest cabin paths etc.)
    radius: int | None = None                       # native scene radius around the stand tile (None: all)
    areas: dict = field(default_factory=dict)       # area name -> stand tile
    arrivals: dict = field(default_factory=dict)    # previous location -> area name
    shipping_bin: bool = False
    chests: dict = field(default_factory=dict)      # tile -> stored item counts

    def visible(self, tile):
        return self.radius is None or max(abs(tile[0] - self.tile[0]), abs(tile[1] - self.tile[1])) <= self.radius

    @property
    def tillable(self):
        if not self.soil:
            return []
        return [t for dx, dy in S._FACING if (t := (self.tile[0] + dx, self.tile[1] + dy)) not in self.debris
                and t not in self.hoed and t not in self.chests
                and (self.soil_tiles is None or t in self.soil_tiles)]

    def arrive(self, origin):
        area = self.arrivals.get(origin)
        if area in self.areas:
            self.tile = tuple(self.areas[area])


@dataclass
class World:
    """Mutable synthetic day. ``observe()`` renders it; ``execute()`` applies one started skill."""
    date: tuple
    weekday: str
    weather: str | None
    location: str
    clock: int
    energy: float
    hp: int
    money: int
    inventory: dict
    names: dict
    places: dict
    travel: dict
    day: int = 1
    can_water: int = 40
    log: list = field(default_factory=list)
    capacity: int = 12
    shipping_gold: int = 0
    held: str | None = None  # an item left on a menu cursor (the menu stays open)
    # live7-game-m2: the mod's global scene is a location snapshot + list add/remove deltas, so an
    # in-place watering/planting stays stale outside the fresh 3x3 local window around the last tool
    # tile. ``stale_scene`` renders that split; ``view`` is the runtime's merged targets (SimRuntime).
    stale_scene: bool = False
    snapshot: dict = field(default_factory=dict)  # (x, y) -> crop dict as of the last Farm snapshot
    window_center: tuple | None = None
    view: dict = field(default_factory=dict)
    navigation_radius: int | None = None  # optional rolling native-grid fixture
    pierre_closed_weekdays: tuple = ("Wed",)  # vanilla; the planner must discover it, never read it here
    visited: set = field(default_factory=set)  # simulated actor visits, not the loaded map catalogue

    def pierre_open(self):
        """The mod's PierreOpen(time, Wednesday, festival) for the sim's own clock."""
        return 900 <= self.clock < 1700 and self.weekday not in self.pierre_closed_weekdays

    # ------------------------------------------------------------------ construction
    @classmethod
    def spring1(cls, *, data=None, **start):
        """The packaged Spring 1 farm. ``start`` overrides location/clock/energy/hp/money/inventory."""
        raw = json.loads(DATA.read_text(encoding="utf-8")) if data is None else deepcopy(data)
        places = {}
        for name, p in raw["places"].items():
            debris = {tuple(t): DEBRIS_NAMES[i % 3] for i, t in enumerate(p.get("debris", ()))}
            areas = {k: tuple(v) for k, v in p.get("areas", {}).items()}
            tile = tuple(p["tile"]) if "tile" in p else areas[p["start_area"]]
            places[name] = Place(name, tile, dict(p.get("exits", {})), p.get("trees", 0),
                                 p.get("stones", 0), debris,
                                 bed=tuple(p["bed"]) if p.get("bed") else None,
                                 tv=tuple(p["tv"]) if p.get("tv") else None,
                                 shop_counter=bool(p.get("shop_counter")), soil=bool(p.get("soil")),
                                 radius=p.get("radius"), areas=areas, arrivals=dict(p.get("arrivals", {})))
        names = {q: tuple(v[:2]) + (v[2] if len(v) > 2 else 0,) for q, v in raw["items"].items()}
        names.update({q: (v[0], v[1], 0) for q, v in raw["inventory"].items()})
        travel = {tuple(k.split(">")): v for k, v in raw["travel_minutes"].items()}
        s = dict(raw["start"])
        inventory = {q: 1 for q in raw["inventory"]}
        inventory.update(start.pop("inventory", {}))
        s.update(start)
        world = cls(tuple(raw["date"]), raw["weekday"], raw.get("weather"), s["location"], s["clock"],
                    s["energy"], s["hp"], s["money"], inventory, names, places, travel)
        world.visited.update(s.get('visited', ()))  # explicit prior experience in a replay setup
        return world

    @property
    def here(self) -> Place:
        return self.places[self.location]

    def hoe(self, *tiles, crop=None):
        """Fixture helper: pre-till Farm tiles (optionally with a live crop dict)."""
        farm = self.places["Farm"]
        for t in tiles:
            farm.debris.pop(tuple(t), None)
            farm.hoed[tuple(t)] = dict(crop) if crop else dict(BARE_CROP)
        return self

    # ------------------------------------------------------------------ observation
    def seeds(self):
        return self.inventory.get(SEED, 0)

    def take_snapshot(self):
        """A native location snapshot (arrival / new day): every HoeDirt as it is now."""
        self.snapshot = {t: dict(c) for t, c in self.places["Farm"].hoed.items()}
        self.window_center = None

    def in_window(self, tile):
        cx, cy = self.window_center or self.here.tile
        return max(abs(tile[0] - cx), abs(tile[1] - cy)) <= 1

    def dry_crops(self, place=None):
        place = place or self.here
        return [t for t, c in place.hoed.items() if place.visible(t) and c.get("dead") is False
                and c.get("watered") is False and c.get("ready_for_harvest") is False and c.get("phase") is not None]

    def facts(self):
        tools = tuple(q for q in self.inventory if q.startswith(("(T)", "(W)")))
        return S.SkillFacts(location=self.location, time_of_day=self.clock, energy=self.energy, health=self.hp,
                            player_free=True, inventory={q: n for q, n in self.inventory.items() if n},
                            toolbar=tools, free_slots=max(0, self.capacity - sum(n > 0 for n in self.inventory.values())),
                            flags=FLAGS, trees=self.here.trees, chests=len(self.here.chests),
                            store_stacks=tuple((q, 0, n) for q, n in self.inventory.items()
                                               if q in {"(O)388", "(O)390", "(O)92", "(O)771"} and n > 0),
                            stones=self.here.stones, dry_soil=len(self.dry_crops()),
                            can_water=(self.can_water, 40),
                            shop_open=self.location == "SeedShop" and self.pierre_open())

    def targets(self):
        p, loc, out = self.here, self.location, {}
        for dest, kind in p.exits.items():
            prefix = {"warp": "warp", "door": "door", "building_door": "bdoor"}[kind]
            # Synthetic exit tile a few steps north of the stand tile (never the stand tile itself).
            out[f"{prefix}:{loc}:{dest}"] = dict(kind=kind, target_location=dest, enterable=True,
                                                 tile=_xy((p.tile[0], p.tile[1] - EXIT_OFFSET)))
        for tile, name in p.debris.items():
            if not p.visible(tile):
                continue
            out[f"obj:{loc}:{tile[0]},{tile[1]}:{name}"] = dict(kind="object", name=name, tile=_xy(tile))
        for i in range(p.trees):
            out[f"tf:{loc}:tree{i}:Tree"] = dict(kind="terrain", terrain_type="Tree", tile=_xy((p.tile[0] + 3 + i, p.tile[1] - 4)))
        for tile, crop in p.hoed.items():
            if not p.visible(tile):
                continue
            if self.stale_scene and loc == "Farm":
                crop = self.snapshot.get(tile, crop)  # in-place changes emit no scene delta
            out[f"tf:{loc}:{tile[0]},{tile[1]}:HoeDirt"] = dict(kind="terrain", terrain_type="HoeDirt",
                                                               tile=_xy(tile), crop=dict(crop))
        if p.bed:
            out[f"bed:{loc}"] = dict(kind="bed", owned_by_actor=True, entry_tiles=[_xy(p.bed)], tile=_xy(p.bed))
        if p.tv:
            out[f"obj:{loc}:{p.tv[0]},{p.tv[1]}:TV"] = dict(kind="object", name="TV", tile=_xy(p.tv))
        for tile in p.chests:
            out[f"obj:{loc}:{tile[0]},{tile[1]}:Chest"] = dict(kind="object", name="Chest", tile=_xy(tile))
        return out

    def knowledge(self):
        # This is the simulated native adapter, using the simulator's own world/travel data.
        self.visited.add(self.location)
        places=[]
        for name in self.places:
            path=self.route(self.location,name)
            reason=None if path is not None else "unknown: no simulated connection"
            if name=="SeedShop" and not self.pierre_open(): reason="simulated shop closed"
            places.append(dict(id=name,label=name,reachable_now=reason is None,locked=reason,
                route=path[1][1:] if path else [],motor_route=path[1][1:] if path else [],
                go_to_available=reason is None,facts=[]))
        home=next((name for name,place in self.places.items() if place.bed),None)
        return dict(scope="simulator:actor",date="%s:%s:%s" % (self.date[0],self.date[1],self.date[2]+self.day-1),game_facts=[],
            discoveries=[dict(id='location_entered:' + name, kind='location_entered', subject=name,
                              source='simulated_native_visit', text='Entered ' + name) for name in sorted(self.visited)],
            world_graph=dict(current=self.location,home=home,complete=True,places=places,connections=[]))

    def context(self):
        p = self.here
        inventory = [dict(qualified_item_id=q, name=self.names.get(q, (q,))[0], stack=n,
                          category=self.names.get(q, (q, 0))[1], price=self.names.get(q, (q, 0, 0))[2])
                     for q, n in self.inventory.items() if n]
        window = None
        if p.soil:
            fresh = {f"tf:{self.location}:{t[0]},{t[1]}:HoeDirt": dict(kind="terrain", terrain_type="HoeDirt",
                     tile=_xy(t), crop=dict(c)) for t, c in p.hoed.items() if self.in_window(t)}
            window = dict(center=_xy(self.window_center or p.tile), targets=fresh,
                          tiles=[dict(x=t[0], y=t[1], tillable=True, villager=False) for t in p.tillable])
            if p.soil_tiles is not None:
                cx, cy = self.window_center or p.tile
                window['tiles'] = [dict(x=x, y=y, villager=False,
                    tillable=(x, y) in p.soil_tiles and (x, y) not in p.hoed
                             and (x, y) not in p.debris and (x, y) not in p.chests)
                    for x in range(cx-1, cx+2) for y in range(cy-1, cy+2)]
        extras = [dict(kind="shop_counter", ref=f"shop:{self.location}")] if p.shop_counter else []
        if p.shipping_bin:
            extras.append(dict(kind="shipping_bin", ref=f"ship:{self.location}"))
        return dict(ui_complete=True, inventory_complete=True, identity=IDENTITY + (self.location,),
                    targets=self.targets(), inventory=inventory, money=self.money, world_knowledge=self.knowledge(),
                    mines_ui=dict(goal_route=dict(({"weather": self.weather} if self.weather else {}),
                                                  shop_open=self.pierre_open(), time_of_day=self.clock),
                                  crafting_known=self.crafting_known()), extras=extras,
                    tillable_tiles=[_xy(t) for t in p.tillable], nav_grid=self.nav_grid(),
                    **({"local_window": window} if window else {}))

    def crafting_known(self):
        """Mirror of the mod's CraftingFacts.Known (PM-1 fix13b) for the one recipe this world models: the Chest is
        craftable with 50 Wood and room for it after the craft (a free slot, or the whole 50-Wood stack used up)."""
        wood = self.inventory.get("(O)388", 0)
        row = dict(recipe="Chest", name="Chest", makes="(BC)130", n=1, needs=[["Wood", wood, 50]], max_crafts=wood // 50)
        ok = wood >= 50 and (self.facts().free_slots or wood == 50)
        return dict(known=1, craftable_total=int(bool(ok)), craftable=[row] if ok else [],
                    blocked=[] if ok else [["Chest", "missing_ingredients" if wood < 50 else "no_room_for_output",
                                            "Wood %d/50" % wood]])

    def nav_grid(self):
        if self.navigation_radius is None:
            return {}
        x, y = self.here.tile
        radius = self.navigation_radius
        return {(a, b): '#' if (a, b) in self.here.debris or (a, b) in self.here.chests else '.'
                for a in range(x-radius, x+radius+1) for b in range(y-radius, y+radius+1)}

    def motor(self, motor=None):
        motor = motor or SimpleNamespace()
        motor.__dict__.update(location=self.location, time_of_day=self.clock, tile=_xy(self.here.tile), pixel=None,
                              health=self.hp, active_menu=None, is_player_free=True,
                              year=self.date[0], season=self.date[1], day=self.date[2] + self.day - 1)
        return motor

    # ------------------------------------------------------------------ effects
    def route(self, start, goal):
        """Cheapest hop sequence over the packaged travel table, or None."""
        queue, seen = [(0, start, [start])], set()
        while queue:
            cost, here, path = heapq.heappop(queue)
            if here == goal:
                return cost, path
            if here in seen:
                continue
            seen.add(here)
            for (a, b), minutes in self.travel.items():
                if a == here and b not in seen:
                    heapq.heappush(queue, (cost + minutes, b, path + [b]))
        return None

    def _pass(self, minutes, energy=0):
        self.clock = add_minutes(self.clock, minutes)
        self.energy = max(0, self.energy - energy)

    def idle(self, minutes=IDLE_MINUTES):
        self._pass(minutes)

    def gain(self, qid, n=1):
        self.inventory[qid] = self.inventory.get(qid, 0) + n

    def execute(self, call):
        """Apply one started skill. Returns ((done|fail, reason), observed)."""
        from arti_stardew_interact import _LOCATION_ALIASES
        name, p = call.name, self.here
        minutes, energy = WORK.get(name, (10, 0))
        observed = {}
        if name == "pergi_ke":
            dest = "Cabin" if call.arg == "home" else _LOCATION_ALIASES.get(call.arg)
            found = self.route(self.location, dest) if dest else None
            if not found or dest == self.location:
                self._pass(10)
                return ("fail", "exit_not_offered"), observed
            minutes, path = found
            if dest == "SeedShop" and not self.pierre_open() and "Town" in path:
                # Native: the locked SeedShop door is no entry; the trip ends in Town in front of it.
                town, _ = self.route(self.location, "Town")
                self._pass(town)
                previous = path[path.index("Town") - 1]
                self.location = "Town"
                self.here.arrive(previous)
                return ("fail", "exit_not_offered"), dict(location="Town")
            self._pass(minutes)
            self.location = dest
            self.here.arrive(path[-2])
            if dest == "Farm" and self.stale_scene:
                self.take_snapshot()
            return ("done", "observed_location_change_objective"), dict(location=dest)
        if name == "jalan_ke":
            from arti_stardew_goal_routes import open_paths, open_path
            path = open_path(open_paths(self.nav_grid(), p.tile), call.tile) if call.tile else None
            if not path:
                return ("fail", "target_not_offered"), observed
            p.tile = tuple(call.tile)
            self.window_center = None
            self._pass(len(path))
            return ("done", "observed_goal_tile"), dict(tile=list(p.tile))
        if name == "tidur":
            if self.here.bed is None:
                self._pass(10)
                return ("fail", "bed_not_offered"), observed
            bed_time = self.clock
            start = (self.date[0], self.date[1], self.date[2] + self.day - 1)
            self.day += 1
            self.clock, self.energy = 610, 270
            self.money += self.shipping_gold
            self.shipping_gold = 0
            if self.stale_scene:
                self.take_snapshot()
            return ("done", "observed_day_change"), dict(answered=True, start_date=start, bed_time=bed_time)
        outcome = ("fail", "sim_not_modeled:" + name)
        if name == "cek_pagi" and p.tv:
            outcome = ("done", "observed_morning_plan_input")
        elif name == "tebang" and p.trees:
            p.trees -= 1
            self.gain("(O)388", 8)
            self.gain("(O)92", 3)
            outcome = ("done", "observed_inventory_objective")
        elif name == "tebang":
            outcome = ("fail", "no_tree")
        elif name == "pecah_batu" and p.stones:
            p.stones -= 1
            self.gain("(O)390", 1)
            outcome = ("done", "observed_tool_effect")
        elif name == "bersihkan" and isinstance(call.arg, str):
            x, y = (int(v) for v in call.arg.split(":")[0].split(","))
            debris = p.debris.pop((x, y), None)
            if debris:
                self.gain(DEBRIS_ITEM[debris])
                outcome = ("done", "observed_tool_effect")
            else:
                outcome = ("done", "observed_no_removable_debris")
        elif name == "cangkul" and p.tillable:
            tile = p.tillable[0]
            p.hoed[tile] = dict(BARE_CROP)
            if self.stale_scene:
                self.snapshot[tile] = dict(BARE_CROP)  # a new HoeDirt is a list add: the scene delta has it
                self.window_center = tile
            outcome = ("done", "observed_tool_effect")
        elif name == "tanam" and self.seeds():
            empty = next((t for t, c in p.hoed.items() if c.get("phase") is None and p.visible(t)), None)
            if empty is not None:
                self.inventory[SEED] -= 1
                p.hoed[empty] = dict(phase=0, qualified_item_id="(O)24", dead=False, watered=p.hoed[empty].get("watered") is True,
                                     ready_for_harvest=False)
                if self.stale_scene:
                    self.window_center = empty  # planting is in place: only the window shows the crop
                outcome = ("done", "observed_extras_effect")
                observed = dict(tile=list(empty))
            else:
                outcome = ("fail", "plant_target_unavailable")
        elif name == "siram" and self.stale_scene and self.can_water:
            # The skill approaches the first tile the runtime's merged view still calls dry.
            from arti_stardew_tool_work import crop_needs_water
            tile = next(((f["tile"]["x"], f["tile"]["y"]) for f in self.view.values()
                         if isinstance(f, dict) and crop_needs_water(f)), None)
            if tile is None:
                outcome = ("fail", "tool_target_not_offered")
            else:
                self.window_center = tile
                if p.hoed[tile].get("watered") is True:
                    outcome = ("fail", "no_effect_progress")  # live7-game-m2 day 2: an already wet tile
                else:
                    p.hoed[tile]["watered"] = True
                    self.can_water -= 1
                    observed = dict(kind="water_tile", target_ref=f"tf:{self.location}:{tile[0]},{tile[1]}:HoeDirt")
                    outcome = ("done", "observed_tool_effect")
        elif name == "siram" and self.dry_crops() and self.can_water:
            tile = self.dry_crops()[0]
            p.hoed[tile]["watered"] = True
            self.can_water -= 1
            observed = dict(kind="water_tile", target_ref=f"tf:{self.location}:{tile[0]},{tile[1]}:HoeDirt")
            outcome = ("done", "observed_tool_effect")
        elif name == "beli" and p.shop_counter:
            from arti_stardew_economy import ItemPolicy
            from arti_stardew_live7 import seed_buy_policy
            n = call.n or 5
            price = self.names.get(SEED, (0, 0, 20))[2]
            policy = seed_buy_policy(ItemPolicy(), self.money)
            if policy.refuse_buy(price=price, qty=n, money=self.money, shop_id="SeedShop") is None:
                self.money -= n * price
                self.gain(SEED, n)
                outcome = ("done", "observed_purchase")
            else:
                outcome = ("fail", "not_affordable")
        elif name in ("jual", "kirim") and (p.shop_counter if name == "jual" else p.shipping_bin):
            from arti_stardew_economy import ItemPolicy
            action = "sell" if name == "jual" else "ship"
            qid = next((q for q in self.inventory if self.names.get(q, (q,))[0] == call.arg), None)
            price = self.names.get(qid, (None, 0, 0))[2]
            count = min(call.n or 1, self.inventory.get(qid, 0))
            if count and ItemPolicy().refuse_dispose(action, qid, price) is None:
                self.inventory[qid] -= count
                if name == "jual":
                    self.money += price * count
                else:
                    self.shipping_gold += price * count
                outcome = ("done", "observed_sale" if name == "jual" else "observed_ship")
        elif name == "buat" and call.arg == "peti" and self.inventory.get("(O)388", 0) >= 50:
            fits = self.inventory.get("(O)388") == 50 or self.facts().free_slots
            self.inventory["(O)388"] -= 50
            self.names["(BC)130"] = ("Chest", -9, 0)
            if fits:
                self.gain("(BC)130")
                outcome = ("done", "observed_craft")
            else:
                # Mod behaviour (ModEntry.Economy.cs): the wood is spent, the crafted chest
                # stays on the CraftingPage cursor and the GameMenu cannot close.
                self.held = "(BC)130"
                outcome = ("fail", "craft_uncertain_item_held")
        elif name == "pasang" and call.arg == "peti" and self.inventory.get("(BC)130", 0):
            if call.tile in p.tillable:
                self.inventory["(BC)130"] -= 1
                p.chests[call.tile] = {}
                outcome = ("done", "observed_extras_effect")
        elif name == "simpan_ke_chest" and p.chests and self.inventory.get(call.arg, 0):
            count = min(call.n or 1, self.inventory[call.arg])
            chest = next(iter(p.chests.values()))
            chest[call.arg] = chest.get(call.arg, 0) + count
            self.inventory[call.arg] -= count
            outcome = ("done", "observed_paired_transfer_objective")
        self._pass(minutes, energy if outcome[0] == "done" else 0)
        return outcome, observed
