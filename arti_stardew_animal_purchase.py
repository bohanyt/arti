"""S26 composition: one explicitly named animal, using only native offered buttons and readback."""
from collections.abc import Mapping
import re


READBACK_WAIT_S = 15.0


def request(arg):
    m = re.fullmatch(r"([a-z][a-z -]{0,30}) nama ([A-Za-z][A-Za-z0-9 -]{0,11}) ke (coop|barn)", arg or "")
    return (m[1], m[2], m[3]) if m else None


def payload_reason(run, offer, args):
    wanted = request(run.call.arg) if run is not None and run.call.name == "beli_hewan" else None
    if wanted is None:
        return "animal_purchase_requires_owner_request"
    facts = (offer.get("view") or {}).get("purchase_animals") or {}
    stage = run.state.get("attempt", {}).get("animal_phase")
    if args.get("btn") == "A" and stage != {"species": "selected", "home": "home", "naming": "submitted"}.get(facts.get("phase")):
        return "animal_purchase_phase_not_offered"
    if "text" in args and args["text"] != wanted[1]:
        return "animal_name_not_requested"
    if "building" in args and not any(b.get("ref") == args["building"] and
            b.get("type", "").casefold().split()[-1:] == [wanted[2]] for b in facts.get("buildings", ())):
        return "animal_building_not_offered"
    if "building" in args and args["building"] != run.state.get("attempt", {}).get("animal_home", {}).get("ref"):
        return "animal_building_not_requested"
    if args.get("btn") == "A" and facts.get("phase") == "species" and not any(
            r.get("id") == args.get("snapped_id") and species_matches(r, wanted[0])
            for r in facts.get("species", ())):
        return "animal_species_not_requested"
    return None


def species_matches(row, wanted):
    """The owner's species word against ONE native shop row: its Data/FarmAnimals id (the button hoverText, e.g.
    "White Chicken") or its native shop display name (FarmAnimal.GetDisplayName(id, forShop: true), e.g. "Chicken"),
    case-insensitively and exactly. Never a substring or another species (live gap8a S26 journal L332)."""
    names = (row.get("species"), row.get("display"))
    return any(isinstance(n, str) and n and n.casefold() == wanted for n in names)


def native_type_matches(selected, pending):
    """Vanilla AlternatePurchaseTypes permits these ordinary chicken colours; no suffix matching.
    The exact pending instance/type remains pinned through naming and adoption."""
    chickens = {"White Chicken", "Brown Chicken", "Blue Chicken"}
    return selected == pending or selected in chickens and pending in chickens


def _record(raw, shape, allow_empty=()):
    if not isinstance(raw, Mapping) or any(type(raw.get(k)) is not t for k, t in shape.items()):
        raise ValueError("animal_purchase_shape")
    if any(isinstance(v, str) and (len(v) > 128 or not v and k not in allow_empty) for k, v in raw.items() if v is not None):
        raise ValueError("animal_purchase_string")
    return dict(raw)


def animal(raw):
    # A pending animal has no home until native adoptAnimal sets homeInterior.
    return _record(raw, dict(id=str, instance=str, type=str, name=str, owner=str, home=str), allow_empty=("home",))


def ids(raw, cap=64):
    if not isinstance(raw, (tuple, list)) or len(raw) > cap or any(type(v) is not str or not v or len(v) > 128 for v in raw):
        raise ValueError("animal_roster")
    if len(set(raw)) != len(raw):
        raise ValueError("animal_roster_duplicate")
    return list(raw)


def adoption(raw):
    if raw is None:
        return None
    out = _record(raw, dict(receiver=str, actor=str, building=str, price=int, money_before=int, money_after=int,
                            same_instance=bool, in_house=bool))
    out["animal"] = animal(raw.get("animal"))
    out["before"], out["after"] = ids(raw.get("before")), ids(raw.get("after"))
    return out


def facts(raw):
    if raw is None:
        return None
    out = _record(raw, dict(phase=str, actor=str, target=str, money=int, price=int, ready=bool))
    if out["phase"] not in ("species", "home", "naming", "transition", "return"):
        raise ValueError("animal_phase")
    for key, cap in (("species", 48), ("buildings", 32)):
        if not isinstance(raw.get(key), (list, tuple)) or len(raw[key]) > cap:
            raise ValueError("animal_offers")
    out["species"] = [_record(r, dict(id=int, species=str, price=int, available=bool), allow_empty=("display",))
                      for r in raw["species"]]
    if any(r.get("display") is not None and type(r["display"]) is not str for r in out["species"]):
        raise ValueError("animal_species_display")
    out["buildings"] = []
    for b in raw["buildings"]:
        row = _record(b, dict(ref=str, type=str, interior=str, x=int, y=int, width=int, height=int, capacity=int))
        row["residents"] = ids(b.get("residents"))
        if row["capacity"] <= len(row["residents"]) or min(row["width"], row["height"]) <= 0:
            raise ValueError("animal_building_full")
        out["buildings"].append(row)
    out["animal"] = animal(raw["animal"]) if raw.get("animal") is not None else None
    if not isinstance(raw.get("names"), (list, tuple)) or len(raw["names"]) > 256:
        raise ValueError("animal_names")
    out["names"] = list(raw["names"])
    if any(type(n) is not str or len(n) > 128 for n in out["names"]):
        raise ValueError("animal_names")
    out["adoption"] = adoption(raw.get("adoption"))
    return out


def plan(call, snap, state, context):
    import arti_stardew_skills as S
    import arti_stardew_skills_attempts as A
    wanted = request(call.arg)
    if wanted is None:
        return S.Step("fail", reason="animal_species_name_home_not_requested")
    species, name, home = wanted
    a = state["attempt"]
    identity = context.get("identity")
    # Native placement changes the viewed location. Session/actor/screen identity must remain exact.
    stable = tuple(identity[:-1]) if isinstance(identity, (list, tuple)) and len(identity) >= 6 else None
    if stable is None or a.setdefault("animal_identity", stable) != stable:
        return S.Step("fail", reason="animal_identity_changed")
    view, pad = context.get("ui_view"), S._g(snap, "ui_pad_target") or {}
    p = getattr(view, "purchase_animals", None) if view else None
    phase = a.get("animal_phase")
    last = S._g(S._g(snap, "working_memory"), "last_action")
    before, after = S._g(last, "before") or {}, S._g(last, "after") or {}

    def fail(why):
        return S.Step("fail", reason="animal_" + why)

    def wait(why):
        return S._wait(state, "animal_" + why, "animal_" + why, "Amati pembelian hewan native", READBACK_WAIT_S)

    def press(**payload):
        if "A" not in pad.get("press", {}):
            return fail("button_not_offered")
        return S._decision(S.act("ui_press", dict(pad.get("pins") or {}, btn="A",
                                                  snapped_id=pad.get("snapped_id"), **payload)))

    if phase in ("submitted", "adoption", "close", "closed"):
        if phase == "submitted":
            receipt = after.get("ui_press") or {}
            source = before.get("ui_press") or {}
            # gap8j J359 has readback="Arti", but J360's ActionEvidenceView deliberately carries only
            # the press effect/receiver. Authenticate the input here; verify name/home from the actual
            # house animal below, never the saved preview or a textbox echo. Pin the request BEFORE
            # waiting, since a settle ui_inspect can replace last_action. Never replay the submit.
            if (not A._press_receipt(last, a["animal_menu"]) or source.get("btn") != "A"
                    or source.get("text") != name or receipt.get("effect") != "animal_adopted"
                    or S._g(last, "request_id") == a.get("animal_submit_after_request")):
                return fail("naming_receipt_unverified")
            a.update(animal_phase="adoption", animal_request=S._g(last, "request_id"))
            phase = "adoption"
        try:
            ledger = adoption((context.get("mines_ui") or {}).get("animal_purchase"))
        except ValueError:
            return fail("adoption_malformed")
        if ledger is None:
            return wait("adoption_pending")
        pending, building = a["animal_pending"], a["animal_home"]
        actual = ledger["animal"]
        # The closure's money_after is a LIVE wallet read, not a second transaction.
        # Freeze only an exact, authenticated adoption proof; later reads still have
        # to prove the actual animal/house/roster and the return independently.
        # Native follow-up: publish the immutable debit/rosters in the naming
        # request's terminal. Until then a wallet change BEFORE this first exact
        # Python read remains unprovable and must fail closed.
        frozen = a.get("animal_ledger")
        debit = frozen or ledger
        if (ledger["receiver"] != a["animal_menu"] or ledger["actor"] != pending["owner"]
                or ledger["building"] != building["ref"] or ledger["price"] != a["animal_price"]
                or ledger["money_before"] != a["animal_money"]
                or debit["money_after"] != a["animal_money"] - a["animal_price"]
                or (frozen is None and context.get("money") != ledger["money_after"])
                or (frozen is not None and frozen["request_id"] != a["animal_request"])
                or ledger["same_instance"] is not True
                or ledger["in_house"] is not True or actual["name"] != name or actual["home"] != building["interior"]
                or any(actual[k] != pending[k] for k in ("id", "instance", "type", "owner"))
                or ledger["before"] != building["residents"] or pending["id"] in ledger["before"]
                or set(ledger["after"]) != set(ledger["before"]) | {pending["id"]}
                or len(ledger["after"]) != len(ledger["before"]) + 1):
            return fail("adoption_or_deltas_unverified")
        if frozen is None:
            a["animal_ledger"] = dict(ledger, request_id=a["animal_request"])
        if phase == "adoption":
            a["animal_phase"] = "close"
        transition = (context.get("mines_ui") or {}).get("animal_purchase_transition") or {}
        if (transition.get("receiver") != a["animal_menu"] or transition.get("viewing_target") is not False
                or transition.get("frozen") is not False or transition.get("fading") is not False or transition.get("warping") is not False
                or transition.get("location") != "AnimalShop" or transition.get("cursor_empty") is not True
                or A._location(snap) != "AnimalShop"):
            return wait("return_pending")
        if view is None and S._g(S._g(snap, "motor"), "is_player_free") is not True:
            return wait("return_pending")
        if S.menu_up(snap):
            if p is not None:
                return wait("return_pending")
            if (view is None and a.get("animal_close_receiver")
                    and A.dialogue_close_receipt(snap, view, a, "animal") in ("pending", "unsettled")):
                return wait("close_pending")  # our A advanced Marnie's last page; the box is still closing (bounded)
            # The native return's observed dialogue uses offered A/advance until a verified close.
            if (A._location(snap) != "AnimalShop" or view is None or view.context != "dialogue" or view.options
                    or transition.get("return_receiver") != view.receiver_token):
                return fail("return_menu_changed")
            # Shared S25 closure (A.dialogue_close_receipt / dialogue_close_press, VERIFIED LIVE for S25 in 8k).
            if a.get("animal_close_receiver"):
                got = A.dialogue_close_receipt(snap, view, a, "animal")
                if got == "pending":
                    return wait("close_pending")  # the planned A has not produced a new receipt yet
                if got != "advanced":
                    return fail("close_unverified")
            if not view.available:
                return wait("close_pending")
            a["animal_phase"] = "closed"
            return A.dialogue_close_press(snap, state, a, view, "animal", "animal_close_pending")
        if (transition.get("menu_type") is not None or A._location(snap) != "AnimalShop"
                or S._g(S._g(snap, "motor"), "is_player_free") is not True):
            return wait("return_pending")
        if a.get("animal_close_receiver"):
            # gap8p J L232-L244: Marnie's post-adoption DialogueBox rcv:mi3 took one A ("advanced" its final page,
            # L234) and then closed natively (L244 cleanup: menu null, free). The old check demanded effect "closed"
            # and failed at L242 `animal_close_unverified`. As S25 gap8j: that settled close is the closed state.
            got = A.dialogue_close_receipt(snap, view, a, "animal")
            if got == "pending":
                return wait("close_pending")
            if got != "closed":
                return fail("close_unverified")
        # The actual Coop animal (re-read above from the native house dictionary on THIS step): same id/instance/
        # type/owner, name, home interior and roster +1; historical debit stays frozen.
        A.note(state, "animal_adopted", animal=actual, building=building["ref"], price=a["animal_price"],
               request_id=a["animal_request"], roster_before=len(a["animal_ledger"]["before"]),
               roster_after=len(a["animal_ledger"]["after"]), money_after=a["animal_ledger"]["money_after"],
               current_money=context.get("money"), menu_closed=True)
        a["animal_phase"] = "done"
        return S.Step("done", reason="observed_animal_adoption")
    if phase == "done":
        return S.Step("done", reason="observed_animal_adoption")
    if phase == "selected":
        # gap8l J192-198: the species A completed, but the continuation stayed
        # current with an older pad offer. Authenticate the
        # one press BEFORE any refresh can replace last_action; never replay it.
        if not a.get("animal_species_request"):
            source, result = before.get("ui_press") or {}, after.get("ui_press") or {}
            if (not A._press_receipt(last, a["animal_menu"]) or source.get("btn") != "A"
                    or source.get("snapped_id") != a["animal_row"]["id"]
                    or source.get("context") != "purchase_animals"
                    or source.get("building") is not None or source.get("text") is not None
                    or result.get("effect") != "changed"
                    or result.get("result_receiver_token") != a["animal_menu"]
                    or result.get("result_menu_type") != "PurchaseAnimalsMenu"):
                return fail("species_selection_unverified")
            a["animal_species_request"] = S._g(last, "request_id")
        if view is not None and (view.receiver_token != a["animal_menu"] or p is None):
            return fail("purchase_receiver_changed")
        # One shared deadline covers stale pins, absent/unready captures and the
        # native fade. Read-only paced inspects obtain matching offers; a changed
        # press receipt alone never authorizes the next building button.
        if (view is None or not view.available or (pad.get("pins") or {}) != view.pins()
                or not p["ready"] or p["phase"] in ("species", "transition")):
            return wait("home_transition_pending")
    if view is not None and not view.available:
        return wait("menu_not_ready")
    if view is not None and (pad.get("pins") or {}) != view.pins():
        return fail("menu_not_offered")
    if phase == "counter":
        if view is None:
            return wait("counter_question_pending")
        if (not S._g(last, "request_id") or S._g(last, "lifecycle") != "completed"
                or S._g(last, "cleanup_confirmed") is not True or S._g(last, "outcome") != "QuestionOpened"
                or S._g(last, "world_effect_status") != "observed_extras_effect"
                or after.get("kind") != "animal_shop" or after.get("receiver_token") != view.receiver_token):
            return fail("counter_question_unverified")
        key = S._g(snap, "ui_key_target") or {}
        choices = [o for o in view.options if o.get("response_key") == "Purchase"]
        if (view.context != "question" or (getattr(view, "question_key", None) or key.get("question_key")) != "Marnie"
                or len(choices) != 1):
            return fail("purchase_response_not_offered")
        a.update(animal_phase="shop", animal_question=view.receiver_token, animal_choice=str(choices[0]["index"]))
        return press(choice=a["animal_choice"])
    if phase == "shop":
        if p is None:
            return wait("species_menu_pending")
        receipt = after.get("ui_press") or {}
        source = before.get("ui_press") or {}
        if (not A._press_receipt(last, a["animal_question"]) or source.get("choice") != a["animal_choice"]
                or receipt.get("result_receiver_token") != view.receiver_token
                or receipt.get("result_menu_type") != "PurchaseAnimalsMenu"):
            return fail("species_menu_unverified")
        a.update(animal_phase="species", animal_menu=view.receiver_token)
        phase = "species"
    if phase in ("species", "selected", "home", "naming"):
        if p is None or view.receiver_token != a["animal_menu"]:
            return fail("purchase_receiver_changed")
        if not p["ready"] or p["phase"] == "transition":
            return wait("transition_pending")
        if name in p["names"]:
            return fail("duplicate_name")
        if phase == "species":
            rows = [r for r in p["species"] if species_matches(r, species) and r["available"]]
            if len(rows) != 1 or rows[0]["price"] <= 0 or p["money"] < rows[0]["price"]:
                # Live gap8a: the report lacked the published rows/money; record them so the cause is never guessed.
                A.note(state, "animal_species_read", wanted=species, money=p["money"], phase=p["phase"],
                       ready=p["ready"], species=[{k: r.get(k) for k in ("id", "species", "display", "price", "available")}
                                                  for r in p["species"][:12]])
                return fail("requested_species_or_money_unavailable")
            row = rows[0]
            step = A._go(snap, view, [row["id"]], "animal_species_navigation_not_offered", a)
            if step is not None:
                return step
            a.update(animal_phase="selected", animal_row=row, animal_money=p["money"], animal_price=row["price"])
            return press()
        if p["price"] != a["animal_price"] or p["money"] != a["animal_money"] or p["animal"] is None:
            return fail("selection_or_money_changed")
        if phase == "selected":
            if p["phase"] != "home":
                return fail("home_phase_missing")
            # The native selection is the clicked button's hoverText: the exact Data/FarmAnimals id of the chosen row.
            if p.get("selected_species") != a["animal_row"]["species"] or p["animal"]["owner"] != p["actor"]:
                return fail("selected_species_or_owner_changed")
            if not native_type_matches(a["animal_row"]["species"], p["animal"]["type"]):
                return fail("selected_native_type_mismatch")
            homes = [b for b in p["buildings"] if b["type"].casefold().split()[-1:] == [home]]
            if len(homes) != 1:
                return fail("requested_home_unavailable_or_ambiguous")
            a.update(animal_pending=p["animal"], animal_home=homes[0], animal_phase="home")
            return press(building=homes[0]["ref"])
        if any(p["animal"][k] != a["animal_pending"][k] for k in ("id", "instance", "type", "owner")):
            return fail("pending_animal_changed")
        if (p["phase"] != "naming" or p.get("selected_building") != a["animal_home"]["ref"]
                or a["animal_home"] not in p["buildings"]):
            return fail("selected_home_changed")
        if phase == "home":
            if (not A._press_receipt(last, a["animal_menu"])
                    or (before.get("ui_press") or {}).get("building") != a["animal_home"]["ref"]):
                return fail("home_selection_unverified")
        a.update(animal_phase="submitted", animal_submit_after_request=S._g(last, "request_id"))
        # Irreversible submit is never retried, even if return is delayed.
        return press(text=name)
    if S.menu_up(snap):
        return fail("unexpected_menu")
    target = S._g(snap, "world_interaction_target") or {}
    if target.get("kind") == "animal_shop":
        a.update(animal_phase="counter", counter=dict(label=target.get("label")))
        return S._decision(S.act("world_interact", {}))
    counters = [e for e in context.get("extras", ()) if e.get("kind") == "shop_counter" and e.get("label") == "AnimalShop"]
    if counters:
        tile = S._wave1_target_tile(counters[0])
        step = A.walk_to(snap, state, context, tile, stands=counters[0].get("stands"))
        if step is not None:
            return step
        from arti_stardew_runtime import _grab_tile
        motor, here = S._g(snap, "motor"), A._here(snap)
        if here and _grab_tile(S._g(motor, "pixel"), 0) == here:
            step = A._clint_flush(snap, state, context, tile, owner="animal")
            if step is not None:
                return step
    refusal = A._refusal(snap, context).get("extras_refusal") or {}
    if refusal.get("kind") == "animal_shop" and refusal.get("reason") == "grab_tile_is_own_tile":
        step = A._clint_flush(snap, state, context, (refusal.get("x"), refusal.get("y")), owner="animal")
        if step is not None:
            return step
    return A.end(state, context, "animalshop_counter_not_offered")
