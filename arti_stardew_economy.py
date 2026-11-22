"""Primitives v2 WS-G2: pure helpers for the economy primitives (shop buy / sell, crafting, shipping bin, chest take-out).

No IO, no provider, no command inbox. The mod is authoritative for every gate and every effect (it publishes the shop
stock / sell list / recipes in ``ui.context`` ``ui_pad.economy``, the bin in ``shipping_bin``, refuses every "-" cell,
applies its own copy of the owner item policy and judges each press on a before / after ledger). This module only

* loads the owner item policy table from config (``stardew_item_policy`` -> :class:`ItemPolicy`, conservative
  buy/craft/storage defaults from the mod's ``ItemPolicyConfig``) and refuses before anything is sent (journal
  ``item_policy_refused``),
* parses the owner's economy objectives (authenticated owner text only, ID / EN, anchored):
  "sell one <item>" / "jual satu <item>", "buy one <item>" / "beli satu <item>", "craft one <item>" /
  "buat satu <item>" / "masak <item>", "ship one <item>" / "kirim satu <item>", "take <item> from the chest" /
  "ambil [N] <item> dari peti" (+ counts, "all" / "semua" for sell / ship). take_out moves the WHOLE stack (the
  mod's vanilla click): "ambil N" with N below the stack takes the full stack (the verdict needs >= N),
* binds the objective's item text to an item id only from names the mod published (never invented),
* decides which economy presses / transfers may be offered for the active objective,
* computes the deterministic finish verdict (``economy_rule``) from VERIFIED mod results only (ledger deltas).

Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G2 live witnesses.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

KIND_SELL, KIND_BUY, KIND_CRAFT, KIND_SHIP, KIND_TAKE = "sell", "buy", "craft", "ship", "take"
KINDS = (KIND_SELL, KIND_BUY, KIND_CRAFT, KIND_SHIP, KIND_TAKE)
FINISH = {KIND_SELL: "observed_sale", KIND_BUY: "observed_purchase", KIND_CRAFT: "observed_craft",
          KIND_SHIP: "observed_ship", KIND_TAKE: "observed_take_out"}
ECONOMY_OP = "economy_transfer"
KIND_SHOP_COUNTER = "shop_counter"
KIND_SHIPPING_BIN = "shipping_bin"  # FIXB7 D-B5b-3: the mod's world_interact kind that opens ItemGrabMenu(shippingBin)   # fix D-B2-1: the mod's world_interact kind that opens the ShopMenu
COUNTER_REACH = 12
MAX_QTY = 999

# Mirror of the mod's ItemPolicyConfig defaults (Protocol/ItemPolicyConfig.cs). Shared vector in the tests.
DEFAULT_ALLOWLIST = (
    "(O)16", "(O)18", "(O)20", "(O)22", "(O)24", "(O)188", "(O)190", "(O)192", "(O)248", "(O)250", "(O)296",
    "(O)396", "(O)398", "(O)399", "(O)402", "(O)404", "(O)406", "(O)408", "(O)410", "(O)412", "(O)414", "(O)416",
    "(O)418",
)
DEFAULT_NEVER = (
    "(O)388", "(O)390", "(O)771", "(O)382", "(O)378", "(O)380", "(O)384", "(O)386", "(O)334", "(O)335", "(O)336",
    "(O)337", "(O)709", "(O)92", "(O)330", "(O)766", "(O)74", "(O)72",
)
DEFAULT_BUY_MAX_TOTAL = 500
# FIXB27 D-B24-2 (CT AFK default 2026-10-12, owner-overridable via stardew_item_policy.sell.fish_allow / fish_price_cap;
# OWNER_INBOX): common low-value fish ARTI may sell, each unit at most DEFAULT_SELL_FISH_PRICE_CAP gold (an iridium-star
# fish above the cap stays refused). Carp, Bullhead, Sunfish, Chub, Bream, Smallmouth Bass. Mirror of the mod's
# ItemPolicyConfig.DefaultSellFish / SellFishPriceCap.
DEFAULT_SELL_FISH = ("(O)142", "(O)700", "(O)145", "(O)702", "(O)132", "(O)137")
DEFAULT_SELL_FISH_PRICE_CAP = 100


HARD_DISPOSE_CATEGORIES = frozenset({-99, -98})  # Object.toolCategory, Object.weaponCategory


def hard_dispose_refusal(row: Mapping) -> str | None:
    """Owner B (2026-10-23): only tools/weapons and observed quest items are protected.

    InventorySnapshotCapture publishes subtypes, category and qualified id, but no quest flag.
    The existing menu policy can identify a quest item once its receiver is observed.
    """
    qid = str(row.get("qualified_item_id") or row.get("item") or "")
    if (qid.startswith(("(T)", "(W)")) or row.get("category") in HARD_DISPOSE_CATEGORIES
            or row.get("is_tool") is True or row.get("tool_subtype") or row.get("weapon_subtype")
            or row.get("type_definition") in ("(T)", "(W)")
            or any(row.get(k) == "refused:tool" for k in ("policy", "ship_policy"))):
        return "tool"
    if (row.get("quest_item") is True or row.get("questItem") is True
            or any(row.get(k) == "refused:quest_item" for k in ("policy", "ship_policy"))):
        return "quest_item"
    return None


# Vanilla item identities already present in inventory observations. This classifies a sentimental preference,
# never permission. Artifact IDs: 96..127 except Lost Book (102), plus 579..589.
ARTIFACT_IDS = frozenset(f"(O){i}" for i in (*range(96, 102), *range(103, 128), *range(579, 590)))
RARE_DROP_IDS = frozenset({"(O)74", "(O)347"})  # Prismatic Shard, Rare Seed
HIGH_VALUE_G = 500  # a preference threshold on an observed unit sell price, never an estimated value


def sentimental_facts(row: Mapping) -> list[str]:
    """Explicit rare identities/artifacts or observed value; category alone is not rarity evidence."""
    qid = row.get("qualified_item_id") or row.get("item")
    if qid in ARTIFACT_IDS or row.get("item_type") == "Arch":
        return ["langka: artefak"]
    if qid in RARE_DROP_IDS:
        return ["langka"]
    price = row.get("unit_price", row.get("price"))
    return ["bernilai tinggi"] if type(price) is int and price >= HIGH_VALUE_G else []

# D4 storage scope: farm produce plus explicitly earned wood/stone. Seeds, hay, tools and food outside
# the published cheap-food candidates cannot enter this allowlist. Owner policy may narrow it further.
STORE_ANIMAL_PRODUCTS = frozenset({"(O)107", "(O)174", "(O)176", "(O)180", "(O)182", "(O)184", "(O)186",
                                   "(O)430", "(O)436", "(O)438", "(O)440", "(O)442", "(O)444", "(O)446",
                                   "(O)289", "(O)769"})
STORE_RESOURCES = frozenset({"(O)388", "(O)390"})
LIVE7_STORE_RESOURCES = STORE_RESOURCES | frozenset({"(O)92", "(O)771"})

MODEL_GUIDANCE = (" Economy goal: sell/buy/craft only the owner's item (economy_goal). In a shop LT/RT toggles stock vs"
                  " inventory; X on your item sells ONE; A on a stock row buys (qty from economy_goal.qty); in crafting X"
                  " crafts. At the shipping bin or chest use economy_transfer (copy observed_targets). B closes.")

CRAFT_MODEL_GUIDANCE = (" Economy goal: craft only the owner's item (economy_goal). X crafts; B closes.")


def _qualify(item_id: object) -> str | None:
    if not isinstance(item_id, str) or not item_id or len(item_id) > 128:
        return None
    return item_id if item_id.startswith("(") else ("(O)" + item_id if item_id.isdigit() else item_id)


def _ids(raw: object, default: tuple[str, ...]) -> frozenset[str]:
    if raw is None:
        return frozenset(default)
    if not isinstance(raw, (list, tuple)) or not all(isinstance(x, str) and 0 < len(x) <= 128 for x in raw):
        return frozenset(default)  # malformed owner table: fall back to the conservative default, never wider
    return frozenset(_qualify(x) for x in raw)


def _cap(raw: object, default: int) -> int:
    return raw if type(raw) is int and 0 <= raw <= 1_000_000 else default


@dataclass(frozen=True)
class ItemPolicy:
    """Owner B frees disposal. Legacy lists still protect gifts/storage; buy/craft gates are unchanged."""
    sell_allow: frozenset = frozenset(DEFAULT_ALLOWLIST)
    sell_price_cap: int = 0
    ship_allow: frozenset = frozenset(DEFAULT_ALLOWLIST)
    ship_price_cap: int = 0
    never: frozenset = frozenset(DEFAULT_NEVER)
    buy_max_total: int = DEFAULT_BUY_MAX_TOTAL
    allow_tool_upgrade: bool = False
    craft_deny: frozenset = frozenset()
    sell_fish: frozenset = frozenset(DEFAULT_SELL_FISH)
    sell_fish_price_cap: int = DEFAULT_SELL_FISH_PRICE_CAP

    @classmethod
    def from_config(cls, config: Mapping | None) -> "ItemPolicy":
        raw = config.get("stardew_item_policy") if isinstance(config, Mapping) else None
        if not isinstance(raw, Mapping):
            return cls()
        sell = raw.get("sell") if isinstance(raw.get("sell"), Mapping) else {}
        ship = raw.get("ship") if isinstance(raw.get("ship"), Mapping) else {}
        deny = raw.get("craft_deny")
        return cls(
            sell_allow=_ids(sell.get("allow"), DEFAULT_ALLOWLIST), sell_price_cap=_cap(sell.get("price_cap"), 0),
            ship_allow=_ids(ship.get("allow"), DEFAULT_ALLOWLIST), ship_price_cap=_cap(ship.get("price_cap"), 0),
            never=_ids(raw.get("never"), DEFAULT_NEVER),
            buy_max_total=_cap(raw.get("buy_max_total"), DEFAULT_BUY_MAX_TOTAL),
            allow_tool_upgrade=raw.get("allow_tool_upgrade") is True,
            craft_deny=frozenset(x.casefold() for x in deny if isinstance(x, str)) if isinstance(deny, (list, tuple)) else frozenset(),
            sell_fish=_ids(sell.get("fish_allow"), DEFAULT_SELL_FISH),
            sell_fish_price_cap=_cap(sell.get("fish_price_cap"), DEFAULT_SELL_FISH_PRICE_CAP),
        )

    def refuse_dispose(self, action: str, item_id: str | None, unit_price: object, *, mod_policy: object = "ok",
                       mod_priced: bool = False, item: Mapping | None = None) -> str | None:
        """Free sell/ship policy. Old allowlists, never-lists and price caps do not restrict owner B.

        Native receiver refusals remain authoritative; this does not bypass the unchanged DLL.
        """
        if action == "trash":
            return "trash_never"
        if action not in (KIND_SELL, KIND_SHIP):
            return "unknown_action"
        item_id = _qualify(item_id)
        if item_id is None:
            return "unknown_item"
        refusal = hard_dispose_refusal(dict(item or {}, qualified_item_id=item_id))
        if refusal:
            return refusal
        if mod_policy != "ok":
            return "mod_policy:" + (mod_policy if isinstance(mod_policy, str) else "missing")[:80]
        return None

    def refuse_gift(self, action: str, item_id: str | None, unit_price: object, *, mod_policy: object = "ok",
                       mod_priced: bool = False) -> str | None:
        """Legacy protection for gifts/letters/donations. Owner B only changes selling and shipping."""
        if action == "trash":
            return "trash_never"
        if action not in (KIND_SELL, KIND_SHIP):
            return "unknown_action"
        item_id = _qualify(item_id)
        if item_id is None:
            return "unknown_item"
        if mod_policy != "ok":
            return "mod_policy:" + (mod_policy if isinstance(mod_policy, str) else "missing")[:80]
        if item_id in self.never:
            return "never_list"
        allow = self.sell_allow if action == KIND_SELL else self.ship_allow
        cap = self.sell_price_cap if action == KIND_SELL else self.ship_price_cap
        if item_id in allow:
            return None
        if cap > 0 and type(unit_price) is int and 0 < unit_price <= cap:
            return None
        if cap > 0 and unit_price is None and mod_priced and mod_policy == "ok":
            return None  # live7 2026-10-23: a configured ship cap is checked by the mod where no price is known
        if (action == KIND_SELL and item_id in self.sell_fish and self.sell_fish_price_cap > 0
                and type(unit_price) is int and 0 < unit_price <= self.sell_fish_price_cap):
            return None  # FIXB27 D-B24-2: a common low-value fish at or under its cap
        return "not_allowlisted"

    def store_candidates(self, inventory: Mapping, *, protected: object = (), native_crops: object = (),
                         sentimental: object = (), refused: object = (),
                         live7: bool = False) -> tuple[str, ...]:
        """Observed earned products permitted by D4 and the existing item policy, in inventory order.

        Storage is not disposal: wood/stone (also sap/fiber in Live7) stay eligible despite DEFAULT_NEVER. Other goods must
        be crops in the existing ship allowlist or known animal products, and still pass owner `never`.
        FIX71C (a52 d8-d24: 0 offers beside a chest, bag 12/12 with Geode, Coal, junk, Parsnip): in Live7 the chest takes any
        stack but tools and quest items (``refused``). ``protected`` is ignored there: each day's process enrolls the morning
        bag as the reserve, so food carried overnight was fenced out. Storing destroys nothing; what to keep where is hers.
        """
        if live7:
            denied = set(refused) if isinstance(refused, (list, tuple, set, frozenset)) else set()
            return tuple(qid for qid, count in inventory.items()
                         if isinstance(qid, str) and type(count) is int and count > 0
                         and not qid.startswith(("(T)", "(W)")) and qid not in denied)
        blocked = set(protected) if isinstance(protected, (list, tuple, set, frozenset)) else set()
        crops = set(native_crops) if isinstance(native_crops, (list, tuple, set, frozenset)) else set()
        keepsakes = set(sentimental) if live7 and isinstance(sentimental, (list, tuple, set, frozenset)) else set()
        out = []
        for qid, count in inventory.items():
            if not isinstance(qid, str) or type(count) is not int or count <= 0 or qid in blocked:
                continue
            if qid.startswith(("(T)", "(W)")):
                continue
            if qid in keepsakes:
                out.append(qid)
                continue
            if qid in (LIVE7_STORE_RESOURCES if live7 else STORE_RESOURCES):
                out.append(qid)
            elif qid not in self.never and (qid in crops or (qid in self.ship_allow and qid in DEFAULT_ALLOWLIST)
                                             or qid in STORE_ANIMAL_PRODUCTS):
                out.append(qid)
        return tuple(out)

    def refuse_buy(self, *, price: object, qty: int, money: object, shop_id: object, tool: bool = False) -> str | None:
        if type(price) is not int or price <= 0 or type(qty) is not int or not 1 <= qty <= MAX_QTY:
            return "unverifiable_price_or_qty"
        total = price * qty
        if shop_id == "ClintUpgrade":
            if not self.allow_tool_upgrade:
                return "tool_upgrade_not_allowed"
        elif total > self.buy_max_total:
            return "over_buy_max_total"
        if type(money) is not int or total > money:
            return "not_enough_money"
        return None

    def refuse_craft(self, recipe_name: object) -> str | None:
        return "craft_denied" if isinstance(recipe_name, str) and recipe_name.casefold() in self.craft_deny else None


# ---------------------------------------------------------------------------- objectives (owner text only)

_N = r"(?:(?P<n>semua|all|satu|one|sebuah|an|a|[1-9][0-9]{0,2}) )?"
_ITEM = r"(?P<item>[a-z][a-z' \-]{0,39}?)"
_CHAINED = re.compile(r"\b(?:and|dan|lalu|then|terus|kemudian|also|juga)\b")
_COUNT = {"satu": 1, "one": 1, "a": 1, "an": 1, "sebuah": 1}


def _count(raw: str | None) -> int | None:
    """None = all (stack); else 1..999."""
    if raw is None:
        return 1
    if raw in ("semua", "all"):
        return None
    return _COUNT.get(raw) or int(raw)


@dataclass
class EconomyObjective:
    """One owner-authored economy objective (authenticated owner text only, never model / scene text)."""
    kind: str
    item_text: str
    quantity: int | None            # None = the whole stack ("all" / "semua"; sell / ship only)
    item_id: str | None = None      # bound from a name the mod published
    item_name: str | None = None
    done: int = 0
    events: list = field(default_factory=list)
    uncertain: str | None = None
    finish_recorded: bool = False
    quality: int | None = None        # FIXB6 review: owner-named quality (silver / gold / iridium), else any
    slot: int | None = None           # internal batch pin to one observed inventory stack
    receiver_token: str | None = None # the batch leg cannot migrate to a replacement menu
    shop_rule_steps: int = 0          # D-B5-1: deterministic shop-stage steps taken (bounded)
    shop_rule_stop: str | None = None  # D-B5-1: why the shop stage handed the menu back to the model
    take_blocker: dict | None = None   # D-B7b-3: {reason: bag_full, room, stack} when the bag cannot take the stack

    @property
    def remaining(self) -> int | None:
        return None if self.quantity is None else max(0, self.quantity - self.done)


_QUALITY_WORDS = {"silver": 1, "perak": 1, "gold": 2, "emas": 2, "iridium": 4}


def parse_economy_objective(text: str) -> EconomyObjective | None:
    objective = _parse_economy_objective(text)
    if objective is not None:
        words = objective.item_text.split()
        # "jual satu gold parsnip" / "jual satu parsnip emas": the named quality (presentation + goal row only).
        for i in (0, len(words) - 1):
            if len(words) > 1 and words[i] in _QUALITY_WORDS:
                objective.quality = _QUALITY_WORDS[words[i]]
                objective.item_text = " ".join(w for j, w in enumerate(words) if j != i)
                break
    return objective


def _parse_economy_objective(text: str) -> EconomyObjective | None:
    t = re.sub(r"\s+", " ", (text or "").strip().rstrip(".!")).casefold()
    if _CHAINED.search(t):
        return None  # one owner-named effect per goal (no chained objectives)
    m = re.fullmatch(r"(?:jual|sell) " + _N + _ITEM, t)
    if m and m.group("item").strip():
        return EconomyObjective(KIND_SELL, m.group("item").strip(), _count(m.group("n")))
    m = re.fullmatch(r"(?:beli|buy) " + _N + _ITEM, t)
    if m and m.group("item").strip() and m.group("n") not in ("semua", "all"):
        return EconomyObjective(KIND_BUY, m.group("item").strip(), _count(m.group("n")))
    m = re.fullmatch(r"(?:buat|bikin|craft|make|masak|cook) (?:(?:satu|one|1|a|an|sebuah) )?" + _ITEM, t)
    if m and m.group("item").strip():
        return EconomyObjective(KIND_CRAFT, m.group("item").strip(), 1)
    m = re.fullmatch(r"(?:kirim|ship) " + _N + _ITEM + r"(?: (?:ke|to) (?:the )?(?:shipping bin|bin|kotak pengiriman))?", t)
    if m and m.group("item").strip():
        return EconomyObjective(KIND_SHIP, m.group("item").strip(), _count(m.group("n")))
    m = re.fullmatch(r"(?:ambil|take) (?:the )?" + _N + _ITEM + r" (?:dari|from) (?:the |my )?(?:chest|peti)(?:nya)?", t)
    if m and m.group("item").strip():
        # FIXB10 review: take_out moves the whole stack, so "ambil N" with N < stack takes the full stack (>= N).
        return EconomyObjective(KIND_TAKE, m.group("item").strip(), _count(m.group("n")) if m.group("n") else None)
    return None


def _norm(name: object) -> str:
    if not isinstance(name, str):
        return ""
    n = re.sub(r"[^a-z' \-]", "", name.casefold()).strip()
    n = re.sub(r"\s+", " ", n)
    return n[:-1] if n.endswith("s") and len(n) > 3 else n


def name_matches(objective_text: str, published_name: object) -> bool:
    return bool(_norm(objective_text)) and _norm(objective_text) == _norm(published_name)


def bind_item(objective: EconomyObjective | None, candidates: list[tuple[object, object]]) -> bool:
    """Bind the objective's item id from (published name, item id) pairs (first exact name match). True if bound now."""
    if objective is None or objective.item_id is not None:
        return False
    for name, item_id in candidates:
        qid = _qualify(item_id)
        if qid is not None and name_matches(objective.item_text, name):
            objective.item_id, objective.item_name = qid, name if isinstance(name, str) else None
            return True
    return False


def bind_seed_item(objective, block, policy, *, allow_quantity=False):
    """S11's generic seed choice, from fresh SeedShop stock under normal buy policy."""
    if (objective is None or objective.kind != KIND_BUY or objective.item_text != "benih"
            or objective.item_id is not None or type(objective.quantity) is not int
            or objective.quantity != 1 and not allow_quantity
            or not 1 <= objective.quantity <= MAX_QTY
            or not isinstance(block, Mapping) or block.get("kind") != "shop"
            or block.get("shop_id") != "SeedShop" or block.get("ready") is not True
            or block.get("currency") != 0):
        return False
    rows = [r for r in block.get("rows", ()) if isinstance(r, Mapping)
            and str(r.get("item", "")).startswith("(O)")
            and re.search(r"\bseeds?$", str(r.get("name", "")), re.I)
            and r.get("stock") != 0 and (objective.quantity == 1 or r.get("stock") == -1
                or type(r.get("stock")) is int and r["stock"] >= objective.quantity)
            and r.get("fits") is True and r.get("capacity", 0) >= objective.quantity
            and not r.get("tool") and not r.get("trade_item")
            and policy.refuse_buy(price=r.get("price"), qty=objective.quantity, money=block.get("money"),
                                  shop_id=block.get("shop_id")) is None]
    if not rows:
        return False
    row = min(rows, key=lambda r: (r["price"], r["index"], r["item"]))
    objective.item_id, objective.item_name = row["item"], row["name"]
    return True


FISH_WORDS = frozenset({"ikan", "fish"})
FISH_CATEGORY = -4  # Object.FishCategory


def bind_category_item(objective: EconomyObjective | None, economy: Mapping | None,
                       slot_categories: Mapping[int, int]) -> bool:
    """FIXB24 D-B21-4 (live V2B21SELL-a: `jual satu ikan` -> `goal_item_not_published` with a fish in the bag): a sell goal
    naming the fish category ("ikan" / "fish") binds the first inventory row whose item is a fish (inventory category -4
    at the row's slot), a row the sell policy allows first. True if bound now."""
    if (objective is None or objective.item_id is not None or objective.kind != KIND_SELL
            or _norm(objective.item_text) not in FISH_WORDS or not isinstance(economy, Mapping)):
        return False
    fish = [e for e in economy.get("sell") or () if isinstance(e, Mapping)
            and slot_categories.get(e.get("slot")) == FISH_CATEGORY and _qualify(e.get("item")) is not None]
    fish.sort(key=lambda e: e.get("policy", "ok") != "ok")
    if not fish:
        return False
    objective.item_id = _qualify(fish[0].get("item"))
    objective.item_name = fish[0].get("name") if isinstance(fish[0].get("name"), str) else None
    return True


def economy_candidates(economy: Mapping | None) -> list[tuple[object, object]]:
    """(name, item id) pairs from a parsed ui_pad economy block (stock rows, sell list, recipes)."""
    if not isinstance(economy, Mapping):
        return []
    pairs: list[tuple[object, object]] = []
    for key in ("rows", "sell", "recipes"):
        for entry in economy.get(key) or ():
            if isinstance(entry, Mapping):
                pairs.append((entry.get("name"), entry.get("item")))
    return pairs


# ---------------------------------------------------------------------------- ui_press filtering (shop / crafting)

def snapped_role(economy: Mapping | None, snapped_id: int) -> tuple[str | None, Mapping | None]:
    """("stock" | "sell" | "recipe", entry) for the snapped component, else (None, None)."""
    if not isinstance(economy, Mapping):
        return None, None
    for key, role in (("rows", "stock"), ("sell", "sell"), ("recipes", "recipe")):
        for entry in economy.get(key) or ():
            if isinstance(entry, Mapping) and entry.get("id") == snapped_id:
                return role, entry
    return None, None


def press_refusal(objective: EconomyObjective | None, policy: ItemPolicy, economy: Mapping | None, snapped_id: int,
                  btn: str, payload: Mapping, *, held_item: object = None) -> str | None:
    """Why the runtime must not send this economy press (None = allowed). Non-economy presses return None."""
    role, entry = snapped_role(economy, snapped_id)
    extras = {k: payload[k] for k in ("qty", "half", "stack") if k in payload}
    if role is None:
        return "economy_payload_off_row" if extras else None
    if btn not in ("A", "X"):
        return "economy_payload_on_button" if extras else None
    if held_item is not None:
        # Placing a held (bought / crafted) item back into a slot is never a new effect: always allowed (A, no payload).
        return None if role == "sell" and btn == "A" and not extras else "held_item_place_first"
    if objective is None:
        return "no_economy_objective"
    if objective.uncertain:
        return "economy_objective_uncertain"
    item = _qualify(entry.get("item"))
    if objective.item_id is None or item != objective.item_id:
        return "not_the_objective_item"
    remaining = objective.remaining
    if role == "stock":
        if objective.kind != KIND_BUY:
            return "objective_is_not_buy"
        qty = extras.get("qty", 1)
        if "half" in extras or "stack" in extras or (btn == "X" and qty != 1):
            return "invalid_buy_payload"
        if remaining is None or qty > remaining:
            return "qty_over_remaining"
        if type(entry.get("capacity")) is int and qty > entry["capacity"]:
            return "inventory_full"  # review fix 3: the mod publishes the units the inventory takes
        return policy.refuse_buy(price=entry.get("price"), qty=qty, money=(economy or {}).get("money"),
                                 shop_id=(economy or {}).get("shop_id"))
    if role == "sell":
        if objective.kind != KIND_SELL:
            return "objective_is_not_sell"
        if ((objective.slot is not None and entry.get("slot") != objective.slot)
                or (objective.quality is not None and entry.get("quality") != objective.quality)):
            return "not_the_objective_stack"
        if "qty" in extras or "half" in extras:
            return "invalid_sell_payload"
        if btn == "A" and not extras.get("stack"):
            return "sell_stack_needs_stack_arg"
        if extras.get("stack") and (btn != "A" or objective.quantity is not None and entry.get("stack") != remaining):
            return "stack_not_named_by_goal"
        if btn == "X" and remaining is not None and remaining < 1:
            return "objective_already_met"
        return policy.refuse_dispose(KIND_SELL, item, entry.get("unit_price"), mod_policy=entry.get("policy"), item=entry)
    if role == "recipe":
        if objective.kind != KIND_CRAFT:
            return "objective_is_not_craft"
        if "half" in extras or "stack" in extras or extras.get("qty", 1) != 1:
            return "invalid_craft_payload"
        if entry.get("craftable") is not True:
            return "recipe_not_craftable"
        return policy.refuse_craft(entry.get("recipe"))
    return None


def press_payload(objective: EconomyObjective | None, economy: Mapping | None, snapped_id: int) -> dict:
    """The payload the model should send for the snapped row (empty = none)."""
    role, entry = snapped_role(economy, snapped_id)
    if objective is None or entry is None or objective.item_id != _qualify(entry.get("item")):
        return {}
    remaining = objective.remaining
    if role == "stock" and objective.kind == KIND_BUY and remaining and remaining > 1:
        return {"qty": min(remaining, MAX_QTY)}
    if (role == "sell" and objective.kind == KIND_SELL
            and (objective.quantity is None or objective.slot is not None and entry.get("stack") == remaining)):
        return {"stack": True}
    return {}


# ---------------------------------------------------------------------------- transfers (bin / chest)

def batch_receiver_current(objective: EconomyObjective | None, token) -> bool:
    if objective is None or objective.slot is None:
        return True
    if not isinstance(token, str) or not token:
        return False
    if objective.receiver_token is None:
        objective.receiver_token = token
    if objective.receiver_token != token:
        objective.uncertain = "goods_menu_changed"
        return False
    return True


def bin_targets(objective: EconomyObjective | None, policy: ItemPolicy, facts: Mapping | None) -> list[dict]:
    """economy_transfer targets (container_transfer + mode) at the shipping bin for a ship objective."""
    if objective is None or objective.kind != KIND_SHIP or objective.uncertain or not isinstance(facts, Mapping):
        return []
    block, caps = facts.get("shipping_bin"), facts.get("capabilities")
    cap = caps.get("shipping_bin") if isinstance(caps, Mapping) else None
    if (not isinstance(block, Mapping) or block.get("available") is not True or not isinstance(cap, Mapping)
            or cap.get("available") is not True or block.get("cursor") is not None):
        return []
    if not batch_receiver_current(objective, block.get("receiver_token")):
        return []
    if objective.item_id is None:
        bind_item(objective, [(s.get("name"), (s.get("item") or {}).get("qualified_item_id"))
                              for s in block.get("actor_slots") or () if isinstance(s, Mapping) and isinstance(s.get("item"), Mapping)])
    remaining = objective.remaining
    out = []
    for slot in block.get("actor_slots") or ():
        item = slot.get("item") if isinstance(slot, Mapping) else None
        if not isinstance(item, Mapping) or _qualify(item.get("qualified_item_id")) != objective.item_id:
            continue
        if ((objective.slot is not None and slot.get("slot") != objective.slot)
                or (objective.quality is not None and item.get("quality") != objective.quality)):
            continue
        if policy.refuse_dispose(KIND_SHIP, objective.item_id, None, mod_policy=slot.get("ship_policy"),
                                 mod_priced=True, item=item) is not None:
            continue
        stack = item.get("stack")
        if type(stack) is not int or not 0 < stack <= 999:
            continue
        base = {"receiver_token": block.get("receiver_token"), "container_token": block.get("container_token"),
                "readback_token": block.get("readback_token"), "source_fact_id": block.get("source_fact_id"),
                "target_token": slot.get("target_token"), "slot": slot.get("slot"),
                "qualified_item_id": item.get("qualified_item_id"), "stack": stack, "quality": item.get("quality")}
        if remaining is None or remaining >= stack:
            out.append(dict(base, owner_ceiling=stack, mode="ship_stack"))
        if remaining is None or remaining >= 1:
            out.append(dict(base, owner_ceiling=1, mode="ship_one"))
    return out


def take_room(block: Mapping, item: Mapping) -> int | None:
    """D-B7b-3: how many of ``item`` the actor's inventory can take (the mod's ContainerTransferReadback capacity: an
    empty slot holds max_stack, a slot with the same item and quality its free stack room). None = max_stack unknown."""
    max_stack = item.get("max_stack")
    if type(max_stack) is not int or not 0 < max_stack <= 999:
        return None
    room = 0
    # PM-1 fix10d: the actor grid shows 36 buttons; slots at or past the mod's actor_usable (Farmer.MaxItems) are locked.
    usable = block.get("actor_usable") if type(block.get("actor_usable")) is int else None
    for slot in block.get("actor_slots") or ():
        if usable is not None and isinstance(slot, Mapping) and type(slot.get("slot")) is int and slot["slot"] >= usable:
            continue
        dest = slot.get("item") if isinstance(slot, Mapping) else None
        if dest is None:
            room += max_stack
        elif (isinstance(dest, Mapping) and dest.get("qualified_item_id") == item.get("qualified_item_id")
              and dest.get("quality") == item.get("quality") and dest.get("max_stack") == max_stack
              and type(dest.get("stack")) is int):
            room += max(0, max_stack - dest["stack"])
    return room


def chest_take_targets(objective: EconomyObjective | None, facts: Mapping | None) -> list[dict]:
    """economy_transfer take_out targets from the pinned chest's published container block."""
    if objective is None or objective.kind != KIND_TAKE or objective.uncertain or objective.done > 0 or not isinstance(facts, Mapping):
        return []
    block, caps = facts.get("container"), facts.get("capabilities")
    cap = caps.get("container_transfer") if isinstance(caps, Mapping) else None
    if (not isinstance(block, Mapping) or block.get("available") is not True or not isinstance(cap, Mapping)
            or cap.get("available") is not True or block.get("cursor") is not None):
        return []
    if objective.item_id is None:
        pad = facts.get("ui_pad") if isinstance(facts.get("ui_pad"), Mapping) else {}
        bind_item(objective, [(c.get("label"), (c.get("value") or {}).get("item")) for c in pad.get("components") or ()
                              if isinstance(c, Mapping) and isinstance(c.get("value"), Mapping)])
    out = []
    for slot in block.get("container_slots") or ():
        item = slot.get("item") if isinstance(slot, Mapping) else None
        if not isinstance(item, Mapping) or _qualify(item.get("qualified_item_id")) != objective.item_id:
            continue
        stack = item.get("stack")
        if type(stack) is not int or not 0 < stack <= 999:
            continue
        room = take_room(block, item)
        if room is not None and room < stack:
            # D-B7b-3 (live V2B7bG2take-b: the mod rejected `current chest slot unavailable: binding` = capacity < stack,
            # the bag nearly full): the mod moves the whole stack only; never offered, reported as bag_full instead.
            objective.take_blocker = {"reason": "bag_full", "room": room, "stack": stack, "slot": slot.get("slot")}
            continue
        objective.take_blocker = None
        out.append({"receiver_token": block.get("receiver_token"), "container_token": block.get("container_token"),
                    "readback_token": block.get("readback_token"), "source_fact_id": block.get("source_fact_id"),
                    "target_token": slot.get("target_token"), "slot": slot.get("slot"),
                    "qualified_item_id": item.get("qualified_item_id"), "stack": stack, "quality": item.get("quality"),
                    "owner_ceiling": stack, "mode": "take_out"})
        break  # one stack is the objective
    return out


# ---------------------------------------------------------------------------- progress + finish (economy_rule)

def note_press(objective: EconomyObjective | None, economy: Mapping | None, *, verified: bool, uncertain: bool) -> str | None:
    """Fold one ui_press result into the objective. Returns the progress label, or None."""
    if objective is None or not isinstance(economy, Mapping):
        return None
    if uncertain:
        objective.uncertain = "ui_press_uncertain:" + str(economy.get("reason"))[:60]
        return "uncertain"
    kind = {"sell": KIND_SELL, "buy": KIND_BUY, "craft": KIND_CRAFT, "upgrade": KIND_BUY}.get(economy.get("kind"))
    if (not verified or economy.get("observed") is not True or kind != objective.kind
            or _qualify(economy.get("item")) != objective.item_id):
        return None
    ints = [economy.get(k) for k in ("money_before", "money_after", "item_before", "item_after", "quantity")]
    if not all(type(v) is int for v in ints):
        return None
    mb, ma, ib, ia, qty = ints
    # Re-check the mod's judgement on the published numbers (deterministic arithmetic, never trust a label alone).
    if kind == KIND_SELL and not (ia - ib == -qty and ma > mb):
        objective.uncertain = "ledger_inconsistent"
        return "uncertain"
    if kind == KIND_BUY and economy.get("kind") == "buy" and not (ia - ib >= qty and ma < mb):
        objective.uncertain = "ledger_inconsistent"
        return "uncertain"
    if kind == KIND_CRAFT and not (ia - ib >= qty and ma == mb):
        objective.uncertain = "ledger_inconsistent"
        return "uncertain"
    gained = qty if kind in (KIND_SELL, KIND_BUY) else ia - ib
    objective.done += gained
    objective.events.append({"kind": kind, "item": objective.item_id, "quantity": gained, "money_delta": ma - mb})
    return "progress"


def note_transfer(objective: EconomyObjective | None, args: Mapping, *, verified: bool, outcome: object,
                  uncertain: bool, request_id=None, her_menu_pick=True) -> str | None:
    if objective is None or objective.finish_recorded or args.get("mode") not in ("ship_one", "ship_stack", "take_out"):
        return None
    if uncertain:
        objective.uncertain = "transfer_uncertain:" + str(outcome)[:40]
        return "uncertain"
    want = KIND_TAKE if args.get("mode") == "take_out" else KIND_SHIP
    qid = _qualify(args.get("qualified_item_id"))
    if (not verified or outcome != "TransferObserved" or want != objective.kind or not qid
            or want != KIND_SHIP and qid != objective.item_id
            or request_id and any(e.get('request_id') == request_id for e in objective.events)):
        return None
    moved = 1 if args.get("mode") == "ship_one" else args.get("stack")
    if type(moved) is not int or moved < 1:
        return None
    if objective.receiver_token is None:
        objective.receiver_token = args.get('receiver_token')
    matches = (qid == objective.item_id and objective.quality in (None,args.get('quality',objective.quality))
               and objective.slot in (None,args.get('slot',objective.slot)))
    if matches or want != KIND_SHIP:
        objective.done += moved
    objective.events.append({"kind": want, "item": qid, "quantity": moved, "mode": args.get("mode"),
                             "quality":args.get('quality',objective.quality), 'request_id':request_id,
                             'retargeted_by_choice':not matches if want == KIND_SHIP else False,
                             'selected_by_model':want == KIND_SHIP, 'her_menu_pick':want == KIND_SHIP and her_menu_pick,
                             'slot':args.get('slot'),
                             # D-B3-2: the economy_rule close of the chest this take came from (container identity).
                             "container_token": args.get("container_token"), "source_fact_id": args.get("source_fact_id")})
    return "progress"


def note_generic_transfer(objective, proof):
    """Verification only: generic input chooses no item, receiver or operation here.

    Credit the pinned stack only from paired native ledgers. Cursor pickup, money-only
    changes, destination-only changes and unrelated items never satisfy the objective.
    """
    if objective is None or objective.uncertain or objective.finish_recorded:
        return None
    before, after = proof["before"], proof["after"]
    if (objective.receiver_token is not None and proof.get("before_receiver") is not None
            and proof["before_receiver"] != objective.receiver_token):
        return None
    if objective.item_id is None:
        rows = before.get("inventory")
        if not isinstance(rows, list):
            return None
        bind_item(objective, [(row.get("name"), row.get("item")) for row in rows if isinstance(row, dict)
                  and (objective.slot is None or row.get("slot") == objective.slot)
                  and (objective.quality is None or row.get("quality") == objective.quality)])
    if objective.item_id is None:
        return None
    qid, quality = objective.item_id, objective.quality
    rid = proof.get("request_id")
    if rid and any(event.get("request_id") == rid for event in objective.events):
        return None
    if any(hard_dispose_refusal(row) for row in before.get("inventory", ())
           if isinstance(row, dict) and row.get("item") == qid):
        return None
    def count(state, key):
        rows = state.get(key)
        if not isinstance(rows, list):
            return None
        if any(not isinstance(r, dict) or type(r.get("stack")) is not int or r["stack"] < 0 for r in rows):
            return None
        return sum(r["stack"] for r in rows if r.get("item") == qid
                   and (quality is None or r.get("quality") == quality))
    ib, ia = count(before, "inventory"), count(after, "inventory")
    mb, ma = before.get("money"), after.get("money")
    if ib is None or ia is None or type(mb) is not int or type(ma) is not int:
        return None
    qty = ib - ia
    if qty <= 0 or objective.remaining is not None and qty > objective.remaining:
        return None
    if objective.slot is not None:
        b = next((r for r in before["inventory"] if r.get("slot") == objective.slot), None)
        a = next((r for r in after["inventory"] if r.get("slot") == objective.slot), None)
        if (b is None or a is None or b.get("item") != qid or b.get("quality") != quality
                or b["stack"] - a["stack"] != qty or a.get("item") not in (qid, "")):
            return None
    if before.get("ui_held_item") or after.get("ui_held_item"):
        return None
    if objective.kind == KIND_SELL:
        result = note_press(objective, dict(kind="sell", observed=True, item=qid,
            money_before=mb, money_after=ma, item_before=ib, item_after=ia, quantity=qty),
            verified=ma > mb, uncertain=False)
        if result == "progress":
            objective.events[-1]["request_id"] = rid
        return result
    if objective.kind == KIND_SHIP:
        bb, ba = count(before, "shipping_items"), count(after, "shipping_items")
        if bb is None or ba is None or ba-bb != qty or ma != mb:
            return None
        result = note_transfer(objective, dict(mode="ship_stack", qualified_item_id=qid, stack=qty),
                               verified=True, outcome="TransferObserved", uncertain=False)
        if result == "progress":
            objective.events[-1]["request_id"] = rid
        return result
    return None


def note_generic_ship_choice(objective, proof, selected):
    """Consume an authenticated model press's paired shipment, including a retarget.

    The caller authenticates the exact leased press and its selected native slot.
    A spontaneous ledger change, watchdog or cursor operation is not a selection.
    Planned progress stays separate from the item ARTI actually chose to ship.
    """
    if (objective is None or objective.kind != KIND_SHIP or objective.uncertain or objective.finish_recorded
            or not isinstance(selected, dict) or not proof.get("request_id")
            or proof.get("before_menu") != "ItemGrabMenu" or proof.get("effect") != "changed"
            or not proof.get("before_receiver")
            or objective.receiver_token not in (None, proof["before_receiver"])
            or any(e.get("request_id") == proof["request_id"] for e in objective.events)):
        return None
    if any(s.get("cursor_item") or s.get("ui_held_item") or s.get("ui_held_stack", 0)
           or (s.get("cursor_settle") or {}).get("cursor") for s in (proof["before"], proof["after"])):
        return None
    slot, qid, quality = (selected.get(k) for k in ("slot", "item", "quality"))
    if type(slot) is not int or type(quality) is not int or quality not in (0, 1, 2, 4) or not isinstance(qid, str) or not qid:
        return None
    rows = proof["before"].get("inventory")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        return None
    row = next((r for r in rows if r.get("slot") == slot), None)
    if (row is None or any(row.get(k) != selected.get(k) for k in ("item", "stack", "quality"))
            or hard_dispose_refusal(row)):
        return None
    actual = EconomyObjective(KIND_SHIP, row.get("name") or qid, None, item_id=qid, slot=slot, quality=quality,
                              receiver_token=proof["before_receiver"])
    if note_generic_transfer(actual, proof) != "progress":
        return None
    # Bind the original plan independently. The clicked row must never rename it.
    if objective.item_id is None:
        bind_item(objective, [(r.get("name"), r.get("item")) for r in proof["before"].get("inventory", ())
            if objective.slot is None or r.get("slot") == objective.slot])
    matches = (qid == objective.item_id and objective.quality in (None, quality)
               and objective.slot in (None, slot))
    event = dict(actual.events[-1], quality=quality, slot=slot, name=row.get("name") or qid,
                 selected_by_model=True, retargeted_by_choice=not matches)
    objective.events.append(event)
    if matches:
        objective.done += event["quantity"]
    return "progress"


def note_generic_craft(objective, proof):
    """PM-1 fix13b: a craft on her own (generic) crafting page, credited only from the paired native ledgers.

    The made item is bound from the page's own recipe facts (CraftingFacts.Page), never from a label. Credit = the
    bag plus the page cursor gained that item, money unchanged; a press that only moved a cursor item gains nothing.
    """
    if (objective is None or objective.kind != KIND_CRAFT or objective.uncertain or objective.finish_recorded
            or not proof.get("request_id") or any(e.get("request_id") == proof["request_id"] for e in objective.events)):
        return None
    before, after = proof["before"], proof["after"]
    if objective.item_id is None:
        want = str(objective.item_text or "").casefold()
        rows = [r for r in (before.get("crafting") or {}).get("recipes", ()) if isinstance(r, list) and len(r) >= 7
                and want in (str(r[1]).casefold(), str(r[6]).casefold())]
        if len({r[2] for r in rows}) != 1 or not rows[0][2]:
            return None
        objective.item_id, objective.item_name = rows[0][2], rows[0][1]
    qid = objective.item_id

    def owned(state):
        rows = state.get("inventory")
        if not isinstance(rows, list) or any(not isinstance(r, dict) or type(r.get("stack")) is not int for r in rows):
            return None
        held = state.get("ui_held_stack", 0) if state.get("ui_held_item") == qid else 0
        return sum(r["stack"] for r in rows if r.get("item") == qid) + (held if type(held) is int else 0)
    ib, ia, mb, ma = owned(before), owned(after), before.get("money"), after.get("money")
    if ib is None or ia is None or type(mb) is not int or mb != ma or ia <= ib:
        return None
    objective.done += ia - ib
    objective.events.append({"kind": KIND_CRAFT, "item": qid, "quantity": ia - ib, "money_delta": 0,
                             "request_id": proof["request_id"], "source": "generic_crafting_page"})
    return "progress"


def note_generic_ship_return(objective, proof):
    """A paired undo removes existing credit; it never selects or retargets an item."""
    if (objective is None or objective.kind != KIND_SHIP or objective.finish_recorded or objective.uncertain
            or not proof.get("request_id") or proof.get("before_menu") != "ItemGrabMenu"
            or proof.get("before_receiver") != objective.receiver_token):
        return
    before, after = proof["before"], proof["after"]
    if (type(before.get("money")) is not int or before["money"] != after.get("money")
            or any(s.get("ui_held_item") or s.get("cursor_item") for s in (before, after))):
        return
    rid = proof["request_id"]
    if any(rid in e.get("return_requests", ()) for e in objective.events):
        return
    def count(state, key, qid, quality):
        rows = state.get(key)
        if not isinstance(rows, list) or any(not isinstance(r, dict) or type(r.get("stack")) is not int or r["stack"] < 0 for r in rows):
            return None
        return sum(r["stack"] for r in rows if r.get("item") == qid and r.get("quality") == quality)
    returned = {}
    for event in reversed(objective.events):
        if event.get("selected_by_model") is not True:
            continue
        key = event["item"], event["quality"]
        if key not in returned:
            ib, ia = (count(s, "inventory", *key) for s in (before, after))
            bb, ba = (count(s, "shipping_items", *key) for s in (before, after))
            returned[key] = ia - ib if None not in (ib, ia, bb, ba) and ia - ib == bb - ba and ia > ib else 0
        qty = min(returned[key], event["quantity"] - event.get("returned_quantity", 0))
        if qty:
            event["returned_quantity"] = event.get("returned_quantity", 0) + qty
            event.setdefault("return_requests", []).append(rid)
            returned[key] -= qty
            if event.get("retargeted_by_choice") is False:
                objective.done -= qty


def shipping_summary(objective):
    events = [e for e in objective.events if e.get("kind") == KIND_SHIP]
    return dict(planned_target=dict(item=objective.item_id, name=objective.item_text, quantity=objective.quantity,
                                   quality=objective.quality, slot=objective.slot),
        planned_item_shipped=objective.done,
        planned_item_not_shipped=objective.done == 0,
        retargeted_by_choice=any(e.get("retargeted_by_choice") is True for e in events),
        actual_shipped=[dict(item=e["item"], quantity=e["quantity"] - e.get("returned_quantity", 0),
                             quality=e.get("quality", objective.quality), request_id=e.get("request_id"))
                        for e in events if e["quantity"] > e.get("returned_quantity", 0)])


def objective_verdict(objective: EconomyObjective) -> tuple[str, str]:
    if objective.uncertain:
        return "uncertain", objective.uncertain
    if objective.item_id is None:
        return "partial", "item_not_bound"
    if objective.quantity is None:
        met = objective.done > 0 and any(e.get("mode") in ("ship_stack", "take_out") or e["kind"] == KIND_SELL
                                         for e in objective.events)
    else:
        met = objective.done >= objective.quantity
    if met:
        return "achieved", FINISH[objective.kind]
    return "partial", ("nothing_verified" if objective.done == 0 else f"verified_{objective.done}_of_{objective.quantity}")


def objective_summary(objective: EconomyObjective | None) -> dict | None:
    if objective is None:
        return None
    summary = {"objective": "economy_" + objective.kind, "item": objective.item_text, "quantity": objective.quantity,
               "item_bound": objective.item_id is not None, "done": objective.done}
    if objective.kind == KIND_SHIP:
        summary.update(shipping_summary(objective))
    if objective.kind == KIND_BUY and objective.remaining and objective.remaining > 1:
        summary["qty"] = objective.remaining
    if objective.quantity is None and objective.kind in (KIND_SELL, KIND_SHIP):
        summary["stack"] = True
    if objective.kind == KIND_TAKE and objective.take_blocker:
        summary["blocked"] = dict(objective.take_blocker)  # D-B7b-3: bag_full (room < stack), never a silent mismatch
    return summary


# ---------------------------------------------------------------------------- model-facing compaction (D-B2-2)
# Live batch 2 (2026-10-08): a shop ui_pad block (45 components + 35 stock rows + 26 sell rows) rendered 13.9 KB and the
# crafting tab 13.4 KB, so every economy request failed provider_input_bound_exceeded before the model saw it. The model
# can only act on the objective's row (the filter withholds every other buy / sell / craft press) and on the snap
# neighbourhood (ui_nav), so the wire keeps those plus a bounded window of the rest; everything else becomes a count.
# Presentation only: the parsed view, the economy filter, the press gates and the mod's own checks are unchanged.
WIRE_MAX_ROWS = 12
WIRE_MAX_SELL = 12
WIRE_MAX_RECIPES = 12
WIRE_MAX_COMPONENTS = 12


def focus_of(objective: EconomyObjective | None) -> dict | None:
    """The objective's item for the compact view (bound id and / or the owner's item text)."""
    if objective is None:
        return None
    return {"item_id": objective.item_id, "item_text": objective.item_text}


def _is_focus(entry: Mapping, focus: Mapping | None, *, name_key: str = "name", item_key: str = "item") -> bool:
    if not isinstance(focus, Mapping) or not isinstance(entry, Mapping):
        return False
    if focus.get("item_id") is not None and _qualify(entry.get(item_key)) == focus.get("item_id"):
        return True
    return bool(focus.get("item_text")) and name_matches(focus["item_text"], entry.get(name_key))


def _window(entries: list, keep: set, limit: int, snapped_id: int) -> list[int]:
    """Indices to keep: focus first, then the entries closest to the snapped entry (else the head), original order."""
    chosen = [i for i, e in enumerate(entries) if i in keep][:limit]
    anchor = next((i for i, e in enumerate(entries) if isinstance(e, Mapping) and e.get("id") == snapped_id), 0)
    for i in sorted(range(len(entries)), key=lambda i: (abs(i - anchor), i)):
        if len(chosen) >= limit:
            break
        if i not in chosen:
            chosen.append(i)
    return sorted(chosen)


def _compact_rows(economy: Mapping, key: str, limit: int, snapped_id: int, focus: Mapping | None, shape) -> tuple[list, int]:
    entries = [e for e in economy.get(key) or () if isinstance(e, Mapping)]
    keep = {i for i, e in enumerate(entries) if _is_focus(e, focus)}
    idx = _window(entries, keep, limit, snapped_id)
    return [shape(entries[i], i in keep) for i in idx], len(entries) - len(idx)


def _stock_row(r: Mapping, goal: bool) -> dict:
    out = {"id": r.get("id"), "name": r.get("name"), "price": r.get("price")}
    if r.get("stock", -1) != -1:
        out["stock"] = r.get("stock")
    if r.get("fits") is False:
        out["fits"] = False
    if r.get("trade_item"):
        out["trade_item"] = r["trade_item"]
    if goal:
        out["goal"] = True
        if "capacity" in r:
            out["capacity"] = r["capacity"]
    return out


def _sell_row(r: Mapping, goal: bool) -> dict:
    out = {"id": r.get("id"), "name": r.get("name"), "stack": r.get("stack"), "unit_price": r.get("unit_price")}
    if goal:
        out["goal"] = True
        if r.get("policy") != "ok":
            out["policy"] = r.get("policy")
    return out


def _recipe_row(r: Mapping, goal: bool) -> dict:
    out = {"id": r.get("id"), "name": r.get("name"), "craftable": r.get("craftable")}
    if goal:
        out["goal"] = True
        for key in ("max_crafts", "known"):
            if key in r:
                out[key] = r[key]
        out["ingredients"] = [dict(i) for i in r.get("ingredients") or () if isinstance(i, Mapping)]
    return out


def compact_economy(economy: Mapping, snapped_id: int, focus: Mapping | None = None) -> dict:
    """Bounded model-facing copy of a parsed ``ui_pad.economy`` block (see the comment above)."""
    out: dict = {"kind": economy.get("kind"), "money": economy.get("money")}
    for key in ("shop_id", "currency", "ready", "tool_upgrading", "cooking", "truncated"):
        if key in economy and economy[key] not in (None, 0, ""):
            out[key] = economy[key]
    lists = ((("rows", WIRE_MAX_ROWS, _stock_row), ("sell", WIRE_MAX_SELL, _sell_row))
             if economy.get("kind") == "shop" else (("recipes", WIRE_MAX_RECIPES, _recipe_row),))
    for key, limit, shape in lists:
        rows, omitted = _compact_rows(economy, key, limit, snapped_id, focus, shape)
        out[key] = rows
        if omitted:
            out[key + "_omitted"] = omitted
    if "rows_total" in economy and economy.get("kind") == "shop":
        out["rows_total"] = economy["rows_total"]
    return out


def compact_components(components: list, snapped: Mapping | None, focus: Mapping | None,
                       limit: int = WIRE_MAX_COMPONENTS) -> tuple[list, list]:
    """(components kept, goal component ids): the snapped one, its neighbours, the objective's item, then the rest."""
    comps = [c for c in components if isinstance(c, Mapping)]
    snapped_id = (snapped or {}).get("id")
    snap = next((c for c in comps if c.get("id") == snapped_id), None)
    nbrs = set((snap or {}).get("nbr") or ())
    goal_ids = [c["id"] for c in comps
                if _is_focus({"name": c.get("label"), "item": (c.get("value") or {}).get("item")}, focus)]
    priority = ([c for c in comps if c.get("id") == snapped_id] + [c for c in comps if c.get("id") in goal_ids]
                + [c for c in comps if c.get("id") in nbrs])
    keep_ids: list = []
    for c in priority:
        if c["id"] not in keep_ids:
            keep_ids.append(c["id"])
    keep_ids = keep_ids[:limit]
    if len(keep_ids) < limit:
        # Then the components nearest the snapped one in published order (the menu's own reading order).
        anchor = next((i for i, c in enumerate(comps) if c.get("id") == snapped_id), 0)
        for i in sorted(range(len(comps)), key=lambda i: (abs(i - anchor), i)):
            if len(keep_ids) >= limit:
                break
            if comps[i]["id"] not in keep_ids:
                keep_ids.append(comps[i]["id"])
    kept = []
    for c in comps:
        if c["id"] in keep_ids:
            item = {k: v for k, v in c.items() if k != "value"}
            if (c["id"] in goal_ids or c["id"] == snapped_id) and c.get("value") is not None:
                item["value"] = c["value"]
            kept.append(item)
    return kept, goal_ids[:8]


# ---------------------------------------------------------------------------- shop counter approach (fix D-B2-1)

def counter_entries(ui_facts: Mapping | None, motor_tile: tuple[int, int]) -> list[dict]:
    """Approach entries for the shop counters in the mod's ``extras_targets`` (stand on a cardinal neighbour and face it;
    the mod's capture at the grab tile decides whether the counter opens: direction, hours, owner present)."""
    import hashlib
    import json
    raw = (ui_facts or {}).get("extras_targets") if isinstance(ui_facts, Mapping) else None
    out = []
    for e in raw if isinstance(raw, (list, tuple)) else ():
        if (not isinstance(e, Mapping) or e.get("kind") != KIND_SHOP_COUNTER or not isinstance(e.get("ref"), str)
                or not 0 < len(e["ref"]) <= 100 or any(type(e.get(k)) is not int or e[k] < 0 for k in ("x", "y"))):
            continue
        from arti_stardew_world_extras import stand_tiles
        stands = stand_tiles(e)
        if e.get("ready") is False or stands == []:
            continue
        if stands is None and max(abs(e["x"] - motor_tile[0]), abs(e["y"] - motor_tile[1])) > COUNTER_REACH:
            continue
        revision = hashlib.sha256(json.dumps([e["ref"], e["x"], e["y"]]).encode()).hexdigest()[:16]
        out.append(dict(ref=e["ref"], tile=[e["x"], e["y"]], kind=KIND_SHOP_COUNTER, label=str(e.get("label") or "")[:40],
                        revision=revision, **({"reach_tiles": stands} if stands is not None else {})))
    return out[:4]


def bin_entries(ui_facts: Mapping | None) -> list[dict]:
    """FIXB7 D-B5b-3 (live V2B5bG2ship-a: no opener for the bin): approach entries for the shipping bins the mod published
    in ``shipping_bins`` (any distance in the location). The bin is a 2x1 building: the entry targets its left tile and
    stands below it facing up (the mod's capture at the grab tile decides; a tile-centred stand gets the one flush)."""
    import hashlib
    import json
    raw = (ui_facts or {}).get("shipping_bins") if isinstance(ui_facts, Mapping) else None
    out = []
    for e in raw if isinstance(raw, (list, tuple)) else ():
        if (not isinstance(e, Mapping) or e.get("kind") != KIND_SHIPPING_BIN or not isinstance(e.get("ref"), str)
                or not 0 < len(e["ref"]) <= 100 or any(type(e.get(k)) is not int or not 0 <= e[k] < 1000 for k in ("x", "y"))):
            continue
        x, y = e["x"], e["y"]
        revision = hashlib.sha256(json.dumps([e["ref"], x, y]).encode()).hexdigest()[:16]
        out.append(dict(ref=e["ref"], tile=[x, y], kind=KIND_SHIPPING_BIN, label="shipping bin",
                        reach_tiles=[[x, y + 1]], revision=revision))
    return out[:2]


STORE_CHEST_SUFFIXES = (":(BC)130", ":(BC)232")


def store_chests(ui_facts: Mapping | None) -> list[dict]:
    """FIXB7 D-B5b-1: the ordinary placed chests the mod published in ``store_chests`` (scene object fact ids, nearest
    first), validated."""
    raw = (ui_facts or {}).get("store_chests") if isinstance(ui_facts, Mapping) else None
    out = []
    for e in raw if isinstance(raw, (list, tuple)) else ():
        if (isinstance(e, Mapping) and isinstance(e.get("ref"), str) and 0 < len(e["ref"]) <= 120
                and e["ref"].startswith("obj:") and e["ref"].endswith(STORE_CHEST_SUFFIXES)
                and all(type(e.get(k)) is int and 0 <= e[k] < 1000 for k in ("x", "y"))):
            chest = {"ref": e["ref"], "tile": [e["x"], e["y"]]}
            for key in ("owner_id", "owner_player_id"):
                if type(e.get(key)) is int:
                    chest[key] = e[key]
            if type(e.get("usable")) is bool:
                chest["usable"] = e["usable"]
            out.append(chest)
    return out[:4]


# ---------------------------------------------------------------------------- deterministic shop stage (D-B5-1)
SHOP_RULE_STEPS_MAX = 40


def _neighbour_direction(components, start, goal) -> str | None:
    """First step of the shortest path start -> goal over the published nbr ids (up, right, down, left)."""
    from arti_stardew_world_extras import nav_direction
    return nav_direction(components, start, goal)


def shop_step(objective: EconomyObjective | None, view, offer: Mapping | None) -> tuple[str, str | None, dict | None, str]:
    """D-B5-1 (live V2B5G2sell-a: the model toggled LT/RT and walked stock rows, never reached Parsnip in slot 15):
    the next deterministic ShopMenu step toward the objective's own row -> ("act", operation, args, reason) |
    ("wait" | "stop" | "done", None, None, reason). Uses only the published ui_pad block (components + nbr ids, economy
    rows / sell list) and the economy-filtered offer (pins, buttons, payload): the mod judges every press as before."""
    if objective is None or objective.kind not in (KIND_SELL, KIND_BUY):
        return "done", None, None, "not_a_shop_objective"
    if objective.uncertain or objective.finish_recorded or (objective.remaining is not None and objective.remaining <= 0) \
            or (objective.remaining is None and objective.done > 0):
        return "done", None, None, "objective_met_or_uncertain"
    if view is None or getattr(view, "context", None) != "shop" or not isinstance(offer, Mapping):
        return "wait", None, None, "shop_not_open"
    if not view.available:
        return "wait", None, None, view.wait_reason or "not_ready"
    if not batch_receiver_current(objective, getattr(view, "receiver_token", None)):
        return "stop", None, None, "goods_menu_changed"
    econ = view.economy if isinstance(view.economy, Mapping) else {}
    key, want_role = ("sell", "sell") if objective.kind == KIND_SELL else ("rows", "stock")
    if objective.item_id is None:
        bind_item(objective, economy_candidates(econ))
    goal = next((e for e in econ.get(key) or () if isinstance(e, Mapping) and _qualify(e.get("item")) == objective.item_id
                 and (objective.quality is None or e.get("quality", 0) == objective.quality)
                 and (objective.slot is None or e.get("slot") == objective.slot)), None)
    if goal is None or objective.item_id is None:
        return "stop", None, None, "goal_item_not_published"
    if objective.kind == KIND_SELL and goal.get("policy") not in (None, "ok"):
        # FIXB24 D-B21-4 (live V2B21SELL-b: Acorn bound, walked to its row, X never offered: the mod's sell policy
        # refuses it): the published row policy ends the stage before any navigation (then closed + partial).
        return "stop", None, None, "item_policy:" + str(goal.get("policy"))[:40]
    pins = offer.get("pins") or {}
    press = offer.get("press") or {}
    if view.snapped_id == goal.get("id"):
        payloads = offer.get("payload") or {}
        btn = "A" if (objective.kind == KIND_BUY or "A" in payloads) else "X"
        if btn not in press:
            return "stop", None, None, "press_withheld:" + btn
        args = dict(pins, btn=btn, snapped_id=view.snapped_id, **(payloads.get(btn) or {}))
        return "act", "ui_press", args, ("buy" if objective.kind == KIND_BUY else "sell") + ":" + str(goal.get("item"))
    if not offer.get("nav"):
        return "stop", None, None, "nav_not_offered"
    if view.snapped_id < 0:
        return "act", "ui_nav", dict(pins, dir="down"), "first_snap"
    role, entry = snapped_role(econ, view.snapped_id)
    if role in ("stock", "sell") and role != want_role:
        btn = next((b for b in ("RT", "LT") if b in press), None)
        if btn is None:
            return "stop", None, None, "toggle_not_offered"
        return "act", "ui_press", dict(pins, btn=btn, snapped_id=view.snapped_id), "toggle_to_" + want_role
    direction = _neighbour_direction(view.components, view.snapped_id, goal.get("id"))
    if direction is None:
        # The goal is outside the published window: step toward it by row index (stock) / inventory row (12 slots).
        if role == "stock" and type(entry.get("index")) is int and type(goal.get("index")) is int:
            direction = "down" if goal["index"] > entry["index"] else "up"
        elif role == "sell" and type(entry.get("slot")) is int and type(goal.get("slot")) is int:
            here, there = entry["slot"], goal["slot"]
            direction = ("down" if there // 12 > here // 12 else "up" if there // 12 < here // 12
                         else "right" if there > here else "left")
        else:
            return "stop", None, None, "no_route_to_goal"
    return "act", "ui_nav", dict(pins, dir=direction), "nav_" + direction
