"""W1 gap B4 (S28 ``surat_quest <item> ke <npc>``): read mail -> accept the exact understood delivery quest -> bind the
native quest -> travel / approach its NPC -> offer the requested item -> close -> reconcile the quest and the bag debit.

Every step is an existing verified button: the actor's mailbox ``world_interact`` (LetterViewerMenu), letter RT / B,
the letter's accept-quest A (mod QuestRules.AcceptLetter: only the presented, readable, not-yet-active item-delivery
quest), the existing pergi_ke travel legs, observed-open walking, ``select_item`` and the faced villager A that the mod
offers as ``quest_delivery`` only when ONE active native delivery quest's read-only probe matches (captured before the
gift kind). The native quest identity comes from ui.context ``quests`` / ``letter_quest`` (mod QuestFacts.cs). No IO,
no coordinate literal, no success shortcut: a gift counter is never a delivery, and the finish needs the SAME bound
quest instance completed (or terminally removed) plus the exact item debit after the dialogue closed.

Native caveat (LetterViewerMenu.cleanupBeforeExit, decomp 1.6.15 :1009-1014): closing a letter that still presents a
quest ACCEPTS it. A presented quest that is not the owner's understood request is therefore never closed here: the run
holds with the letter open (``quest_letter_not_understood``) for the operator.
"""
from collections.abc import Mapping
from urllib.parse import unquote

import arti_stardew_skills as S

ITEM_DELIVERY = "ItemDelivery"
# Bounded read (S29 precedent): the Riverland staging has 2 pending letters + the staged quest letter, plus room for one
# native day-start letter. A larger backlog is a staging problem, never skipped or hidden.
MAIL_LETTERS_MAX = 4
LETTER_PAGES_MAX = 8
GIFT_COUNTERS = ("gifts_today", "gifts_this_week")  # mod npc_gift entry (ModEntry.Extras.cs), native Friendship
SELECT_TRIES_MAX = 2
GRAB_FLUSH_MAX = 4          # short moves toward the faced quest NPC (grab tile still on ARTI's own tile), per run
DELIVERY_PRESSES_MAX = 2    # hand-in A decisions per run; a re-press only after a proven refusal before publication
DELIVERY_PHASES = ("deliver", "delivering", "dialogue")
LETTER_OPEN_WAIT_S = 4.0
QUEST_READBACK_WAIT_S = 4.0
DIALOGUE_WAIT_S = 8.0       # each bounded wait around the NPC's post-delivery dialogue (offer, readiness, its close)
LETTER_ACCEPT_ID = 103      # LetterViewerMenu.acceptQuestButton.myID (native component id, not a coordinate)
LETTER_NEXT_ID = 102
# FINAL Part C (J L624-L642, 08:25 HUD inference): after the accept, the Town arrival met the SeedShop doors natively
# locked (scene door facts enterable false: NativeB1Offers.ResolveDoor offered nothing before Pierre opens) and the
# SeedShop leg stalled on "observe exits". The arrival now parks, input-free (the Part B shop/Mines wait pattern:
# the worker boundary re-polls without the runtime lock), until the native door fact turns enterable or the clock
# passes the opening tick those waits already use. Admitted only before that tick; bounded by DOOR_WAIT_SECONDS
# and by the run's own seconds cap (never renewed). The plan step's own fresh read then decides: a door still
# natively locked is a clean named refusal (``<destination>_door_locked``), never a stall.
DOOR_WAIT_OPEN = 900
DOOR_WAIT_HANDBACK = 910    # one native clock tick after the opening: the plan step's fresh read decides
DOOR_WAIT_SECONDS = 180.0
DOOR_WAIT_MARGIN_S = 60.0   # left of the run's own seconds cap (and the trial's) for the hand-in after the wait
DOOR_WAITING = "door_wait:waiting"


def door_wait_destination(call, state):
    """S28: the interior the bound delivery travels to next (``_deliver`` records it), else None."""
    if call.name != "surat_quest":
        return None
    q = (state.get("attempt") or {}).get("quest") or {}
    destination = q.get("destination")
    if not q.get("bound") or q.get("phase") != "deliver" or not isinstance(destination, str) or not destination:
        return None
    return destination


def locked_doors(targets, destination):
    """Refs of the current location's native door facts into ``destination`` when EVERY one is locked now
    (mod SceneDoorFact ``enterable`` False), else (). No door fact at all is not a lock."""
    doors = sorted((str(ref), f) for ref, f in (targets or {}).items() if isinstance(f, Mapping)
                   and f.get("kind") == "door" and f.get("target_location") == destination)
    if not doors or any(f.get("enterable") is not False for _, f in doors):
        return ()
    return tuple(ref for ref, _ in doors)


def door_wait_sample(wait, read, elapsed):
    """One read-only sample of a parked door wait: None = open (start the leg), DOOR_WAITING, or a refusal.

    ``read`` = dict(location, date, time, free, menu, doors, locked) from ONE fresh native read."""
    if (read is None or read.get("location") != wait["location"] or read.get("date") != wait["date"]
            or read.get("free") is not True or read.get("menu") is not None
            or type(read.get("time")) is not int or read["time"] < wait["time"]):
        return "context_changed"
    if read.get("doors") and not read.get("locked") or read["time"] >= DOOR_WAIT_HANDBACK:
        return None
    if elapsed >= wait["max_seconds"]:
        return "expired"
    return DOOR_WAITING


def _fail(reason):
    return S.Step("fail", reason=reason)


def _ui(context):
    ui = context.get("mines_ui")
    return ui if isinstance(ui, Mapping) else {}


def quest_facts(context):
    """The mod's ui.context ``quests`` (actor quest log, newest first), validated; None when unreadable."""
    raw = _ui(context).get("quests")
    if not isinstance(raw, (list, tuple)):
        return None
    out = []
    for q in raw:
        if (isinstance(q, Mapping) and isinstance(q.get("instance"), str) and q["instance"]
                and isinstance(q.get("quest_id"), str) and type(q.get("completed")) is bool):
            out.append(dict(q))
    return out


def letter_quest(context, receiver):
    """The quest the open letter (this receiver) presents, or None."""
    q = _ui(context).get("letter_quest")
    if isinstance(q, Mapping) and q.get("receiver_token") == receiver and isinstance(q.get("quest_id"), str):
        return dict(q)
    return None


def _owned(inventory, qid):
    return sum(f.get("stack") or 0 for f in inventory if f.get("qualified_item_id") == qid)


def understood(call_item, call_npc, lq, inventory):
    """The owner's request matches this presented quest: an item-delivery quest for the named NPC and item."""
    if (lq.get("type") != ITEM_DELIVERY or lq.get("readable") is not True or lq.get("already_active") is not False
            or lq.get("accept_shown") is not True or type(lq.get("number")) is not int or lq["number"] < 1):
        return False
    if str(lq.get("target") or "").casefold() != call_npc:
        return False
    qid = str(lq.get("item") or "")
    if not qid:
        return False
    if qid.casefold() == call_item:
        return True
    names = {str(f.get(k) or "").casefold() for f in inventory if f.get("qualified_item_id") == qid
             for k in ("name", "display_name")}
    return call_item in names


def _source_parts(source):
    """``xt:quest:<location>:<npc>:<quest id>`` (mod QuestDeliverySource) -> (npc, quest id), or None."""
    parts = str(source or "").split(":")
    if len(parts) != 5 or parts[:2] != ["xt", "quest"]:
        return None
    return unquote(parts[3]), unquote(parts[4])


def _last(snap):
    return S._g(S._g(snap, "working_memory"), "last_action")


def _ui_receipt(last, receiver, btn, effect):
    before, after = S._g(last, "before") or {}, S._g(last, "after") or {}
    press = before.get("ui_press") or {}
    return bool(S._g(last, "request_id") and S._g(last, "lifecycle") == "completed"
                and S._g(last, "cleanup_confirmed") is True and S._g(last, "world_effect_status") == "observed_ui_press"
                and press.get("receiver_token") == receiver and press.get("btn") == btn
                and (after.get("ui_press") or {}).get("effect") == effect)


def _extras_receipt(last, kind, outcome):
    after = S._g(last, "after") or {}
    return bool(S._g(last, "request_id") and S._g(last, "lifecycle") == "completed"
                and S._g(last, "cleanup_confirmed") is True and S._g(last, "outcome") == outcome
                and S._g(last, "world_effect_status") == "observed_extras_effect" and after.get("kind") == kind), after


def plan(call, snap, state, context, attempt):
    """One S28 step. ``attempt`` = arti_stardew_skills_attempts (ledger, travel, walk, end)."""
    A = attempt
    a = state["attempt"]
    if not call.arg or " ke " not in call.arg:
        return _fail("quest_item_and_npc_not_named")  # never a default quest, item or NPC
    item, npc = call.arg.rsplit(" ke ", 1)
    q = a.setdefault("quest", dict(phase="mail", letters=0))
    quests = quest_facts(context)
    if quests is None:
        return S._wait(state, "quest_facts", "quest_facts_unavailable", "Amati fakta quest native", S.WAIT_S)
    if "start_quests" not in q:
        q["start_quests"] = [dict(id=f["quest_id"], instance=f["instance"], completed=f["completed"]) for f in quests][:12]
    view = context.get("ui_view")
    last = _last(snap)
    phase = q["phase"]
    if phase in ("mail", "opening", "reading", "accepting", "closing", "closed"):
        return _mail(call, snap, state, context, A, q, item, npc, quests, view, last)
    if phase in ("deliver", "delivering", "dialogue"):
        return _deliver(snap, state, context, A, q, quests, view, last)
    return _fail("quest_phase_unknown:" + str(phase))


# ---------------------------------------------------------------------------- mail -> accept -> bind

def _mail(call, snap, state, context, A, q, item, npc, quests, view, last):
    phase = q["phase"]
    if view is None:
        if phase == "opening":
            if (S._g(last, "lifecycle") in ("completed", "rejected") and S._g(last, "cleanup_confirmed") is True
                    and S._g(last, "world_effect_status") in ("no_effect_observed", "unchanged", "zero_effect")):
                A.note(state, "letter_not_opened", status=S._g(last, "world_effect_status"), **A._refusal(snap, context))
                return A.end(state, context, "quest_letter_not_opened")
            step = S._wait(state, "quest_letter_open", "quest_letter_not_opened", "Amati surat", LETTER_OPEN_WAIT_S)
            return step if step.kind != "fail" else A.end(state, context, "quest_letter_not_opened")
        if phase == "closing":
            if not _ui_receipt(last, q.get("receiver"), "B", "closed"):
                return _fail("quest_letter_close_unverified")
            q["letters"] += 1
            A.note(state, "letter_closed", receiver=q.get("receiver"), accepted=bool(q.get("bound")))
            q.pop("receiver", None)
            q.pop("pages", None)
            q["phase"] = phase = "deliver" if q.get("bound") else "mail"
            if phase == "deliver":
                return _deliver(snap, state, context, A, q, quests, view, last)
        if S.menu_up(snap):
            return _settle(snap, state)
        return _open_mailbox(snap, state, context, A, q)
    # A letter is up.
    if view.context != "letter":
        return _fail("unexpected_quest_menu")
    if phase == "opening":
        ok, after = _extras_receipt(last, "mailbox", "MenuOpened")
        if not ok or after.get("receiver_token") != view.receiver_token:
            return _fail("quest_letter_open_unverified")
        state.pop("wait:quest_letter_open", None)
        q.update(phase="reading", receiver=view.receiver_token, pages=[])
        phase = "reading"
    if view.receiver_token != q.get("receiver"):
        return _fail("quest_letter_receiver_changed")
    if not view.available:
        return S._wait(state, "quest_letter_ready", "quest_letter_not_ready", "Tunggu surat siap", S.WAIT_S)
    state.pop("wait:quest_letter_ready", None)
    if phase == "accepting":
        return _bind(snap, state, context, A, q, quests, view, last)
    if phase == "closing":
        return _fail("quest_letter_close_unverified")
    # phase == "reading"
    if not view.text:
        return _fail("mail_text_unavailable")
    pages = q.setdefault("pages", [])
    if not pages or pages[-1] != view.text:
        pages.append(view.text)
    if len(pages) > LETTER_PAGES_MAX:
        return _fail("mail_page_bound")
    if any(c.get("id") == LETTER_NEXT_ID and c.get("visible", True) for c in view.components):
        return _ui_press(snap, "RT")  # native text paging only (never a component click)
    lq = letter_quest(context, view.receiver_token)
    if lq is None:
        q["phase"] = "closing"
        A.note(state, "letter_read", receiver=view.receiver_token, quest=None, pages=len(pages))
        return _ui_press(snap, "B")
    A.note(state, "letter_read", receiver=view.receiver_token, pages=len(pages),
           quest={k: lq.get(k) for k in ("quest_id", "type", "target", "item", "number", "already_active")})
    if not understood(item, npc, lq, context["inventory"]):
        # Closing would natively accept this quest (cleanupBeforeExit): hold with the letter open.
        return A.end(state, context, "quest_letter_not_understood")
    if view.snapped_id != LETTER_ACCEPT_ID:
        offer = S._g(snap, "ui_pad_target") or {}
        if (view.snapped_id is None or view.snapped_id == -1) and offer.get("nav") and not q.get("snap_initialized"):
            q["snap_initialized"] = True  # one offered nav initializes a null native snap (the letter defaults to 103)
            return S._decision(S.act("ui_nav", dict(offer.get("pins") or {}, dir="right")))
        return A.end(state, context, "quest_accept_not_offered")
    q.update(phase="accepting", letter_quest={k: lq.get(k) for k in ("quest_id", "type", "target", "item", "number", "title")})
    return _ui_press(snap, "A")


def _settle(snap, state):
    """No readable menu, but the actor is not free yet (a closing animation): wait; a real menu is never driven here."""
    if S._g(S._g(snap, "motor"), "active_menu"):
        return _fail("unexpected_quest_menu")
    return S._wait(state, "quest_settle", "quest_actor_not_free", "Amati keadaan bebas", S.WAIT_S)


def _ui_press(snap, button):
    from arti_stardew_skills_ui import _press
    return _press(snap, button)


def _bind(snap, state, context, A, q, quests, view, last):
    """The accept A: its verified press, the letter no longer presenting it and ONE new matching native quest instance."""
    lq = q["letter_quest"]
    if not _ui_receipt(last, q["receiver"], "A", "changed"):
        return A.end(state, context, "quest_accept_unverified")
    if letter_quest(context, view.receiver_token) is not None:
        return S._wait(state, "quest_accept_readback", "quest_accept_unverified", "Amati quest diterima", QUEST_READBACK_WAIT_S)
    known = {f["instance"] for f in q["start_quests"]}
    hits = [f for f in quests if f["quest_id"] == lq["quest_id"] and f["instance"] not in known]
    if not hits:
        step = S._wait(state, "quest_accept_readback", "quest_accept_unverified", "Amati quest diterima", QUEST_READBACK_WAIT_S)
        return step if step.kind != "fail" else A.end(state, context, "quest_accept_unverified")
    f = hits[0]
    if (len(hits) != 1 or f.get("type") != ITEM_DELIVERY or f["completed"] is not False or f.get("target") != lq["target"]
            or f.get("item") != lq["item"] or f.get("number") != lq["number"]):
        A.note(state, "quest_identity", facts=[{k: h.get(k) for k in ("quest_id", "type", "target", "item", "number")}
                                               for h in hits][:3])
        return A.end(state, context, "quest_identity_mismatch")
    state.pop("wait:quest_accept_readback", None)
    q["bound"] = dict(instance=f["instance"], quest_id=f["quest_id"], target=f["target"], item=f["item"], number=f["number"])
    A.note(state, "quest_accepted", **q["bound"])
    q["phase"] = "closing"
    return _ui_press(snap, "B")  # nothing is pending any more: the close accepts nothing else


def _open_mailbox(snap, state, context, A, q):
    if q["letters"] >= MAIL_LETTERS_MAX:
        return A.end(state, context, "quest_letter_not_found")
    count = context.get("mailbox_count")
    if type(count) is not int or count < 0:
        return A.end(state, context, "mailbox_presence_unavailable")
    if count == 0:
        A.note(state, "mailbox_empty", letters=q["letters"])
        return A.end(state, context, "quest_letter_not_found")
    location = A._location(snap)
    target = S._g(snap, "world_interaction_target") or {}
    from arti_stardew_auto_act import plain_hand, plain_tool_choice
    if not plain_hand(snap):  # the native mailbox A refuses an object in hand (held_item_not_plain_tool)
        choice = plain_tool_choice(snap)
        if choice is None or q.get("hand_selected"):
            return A.end(state, context, "mailbox_plain_hand_not_offered")
        q["hand_selected"] = choice[0] == "select_item"  # fix74a: a row swap is not the selection
        return S._decision(S.act(choice[0], choice[2]))
    if target.get("kind") == "mailbox":
        q["phase"] = "opening"
        A.note(state, "mailbox_pressed", location=location, mailbox_count=count)
        return S._decision(S.act("world_interact", {}))
    here = A._here(snap)
    boxes = sorted(((sum(abs(p - r) for p, r in zip(here, tile)), str(e.get("ref")), tile, e)
                    for e in context.get("extras", ()) if e.get("kind") == "mailbox" and e.get("stands")
                    and (tile := S._wave1_target_tile(e)) is not None), key=lambda k: k[:3]) if here is not None else []
    if not boxes:
        if A._interior(location):
            step = A.travel(snap, state, "Farm")
            if step is not None:
                return step
        A.note(state, "mailbox_not_observed", location=location)
        return A.end(state, context, "mailbox_action_not_offered")
    _, _, tile, e = boxes[0]
    step = A.walk_to(snap, state, context, tile, stands=e.get("stands"))
    if step is not None:
        return step
    A.note(state, "mailbox_not_faced", tile=list(tile), **A._refusal(snap, context))
    return A.end(state, context, "mailbox_action_not_offered")


# ---------------------------------------------------------------------------- travel -> approach -> hand-in -> reconcile

def _bound_fact(q, quests):
    b = q["bound"]
    hits = [f for f in quests if f["instance"] == b["instance"]]
    return hits[0] if len(hits) == 1 and hits[0]["quest_id"] == b["quest_id"] else None


def npc_approach_entries(ui, here, q):
    """Reuse the moving-villager catalog/stands without granting a gift objective or a gift press."""
    import arti_stardew_world_extras as extras
    quests = quest_facts({"mines_ui": ui})
    # gap8n (J L836-L838): the hand-in press flips deliver -> delivering at DECISION time. When the catalog depended
    # on phase == "deliver", the publication re-read lost Pierre's entry (L837 count 0) and the continuation gate
    # discarded the press (L2513, fields ["approach_targets"]). The catalog now spans the whole hand-in (deliver,
    # its pending press, the NPC dialogue) and is otherwise gated only by the native quest facts below.
    if not q.get("bound") or quests is None or q.get("phase") not in DELIVERY_PHASES:
        return []
    fact, b = _bound_fact(q, quests), q["bound"]
    if (fact is None or fact["completed"] is not False or fact.get("type") != ITEM_DELIVERY
            or any(fact.get(k) != b[k] for k in ("target", "item", "number"))):
        return []
    # Only positional entries are reused. Never attach this transient catalog filter to the goal:
    # gift eligibility/counters and the gift auto-rule must not govern a native quest hand-in.
    return extras.approach_entries(ui, here, extras.ExtrasObjective("gift", name=b["target"].casefold()))


def _item_selected(snap, context, qid):
    """Fresh toolbar readback, including the active slot (selectable_items deliberately excludes it).

    S28's composition has no ExtrasObjective, so the snapshot's objective-scoped held_item is normally absent.
    A SelectionObserved receipt or a selection proposal is never latched across subsequent walking steps.
    """
    toolbar = _ui(context).get("toolbar")
    if isinstance(toolbar, Mapping):
        active = toolbar.get("active_slot")
        return (type(active) is int
                and any(isinstance(t, Mapping) and t.get("slot") == active and t.get("qualified_item_id") == qid
                        for t in toolbar.get("targets") or ())
                and any(f.get("slot") == active and f.get("qualified_item_id") == qid
                        and (f.get("stack") or 0) > 0 for f in context["inventory"]))
    return (S._g(snap, "held_item") or {}).get("qualified_item_id") == qid


def _deliver(snap, state, context, A, q, quests, view, last):
    b, phase = q["bound"], q["phase"]
    if phase == "delivering":
        own = _press_receipt(q, last, context)
        if own == "not_published":
            # gap8n L838: the press was refused before publication (no input, receipt unchanged). Nothing reached the
            # game; re-run the hand-in checks (same active instance, untouched bag, exact offer) for a bounded re-press.
            A.note(state, "delivery_not_published", attempt=q["press"]["n"])
            if q["press"]["n"] >= DELIVERY_PRESSES_MAX or _bag(context["inventory"]) != q.get("bag_before"):
                return A.end(state, context, "quest_delivery_not_published")
            q["phase"] = phase = "deliver"
        elif own == "pending":
            # One input was published, but the newest receipt is still the pre-press one: await its own receipt.
            step = S._wait(state, "quest_delivery_receipt", "quest_delivery_unverified", "Amati hasil serah quest",
                           QUEST_READBACK_WAIT_S)
            if step.kind != "fail":
                return step
            own = "stale_receipt"
        if own not in ("own", "not_published"):
            # Never judge a delivery from an older action's receipt (gap8n: L833's move PressCompleted).
            A.note(state, "delivery_receipt", own=own, outcome=S._g(last, "outcome"),
                   status=S._g(last, "world_effect_status"))
            return A.end(state, context, "quest_delivery_unverified")
        state.pop("wait:quest_delivery_receipt", None)
    if phase == "delivering":
        ok, after = _extras_receipt(last, "quest_delivery", "QuestDelivered")
        if (not ok or after.get("item") != b["item"] or after.get("delta") != -b["number"]
                or not (after.get("value") == 1 or after.get("value") == 2)):
            A.note(state, "delivery_receipt", outcome=S._g(last, "outcome"), status=S._g(last, "world_effect_status"),
                   after={k: after.get(k) for k in ("kind", "item", "delta", "value")})
            return A.end(state, context, "quest_delivery_unverified")
        receiver = after.get("receiver_token")
        A.note(state, "quest_delivered", item=after["item"], delta=after["delta"], quest_state=after["value"],
               receiver=receiver)
        if not isinstance(receiver, str) or not receiver:
            return A.end(state, context, "quest_delivery_dialogue_unbound")
        q.update(phase="dialogue", dialogue=receiver)
        phase = "dialogue"
    if phase == "dialogue":
        return _close_dialogue(snap, state, context, A, q, quests, view)
    # phase == "deliver": the bound quest must still be the active native instance.
    if view is not None:
        return _fail("unexpected_quest_menu")
    if S.menu_up(snap):
        return _settle(snap, state)
    state.pop("wait:quest_settle", None)
    fact = _bound_fact(q, quests)
    if fact is None or fact["completed"] is not False:
        A.note(state, "bound_quest", fact={k: (fact or {}).get(k) for k in ("quest_id", "completed")})
        return A.end(state, context, "quest_identity_changed")
    if _owned(context["inventory"], b["item"]) < b["number"]:
        return A.end(state, context, "quest_item_missing")
    location = A._location(snap)
    entries = [e for e in context.get("extras", ()) if e.get("kind") == "npc_gift"
               and str(e.get("label", "")) == b["target"] and S._wave1_target_tile(e) is not None]
    if not entries:
        destination = fact.get("target_location") or ""
        if destination and destination != location:
            q["destination"] = destination  # the boundary's door wait reads it at each travel arrival
            locked = locked_doors(context.get("targets"), destination)
            if locked:
                # FINAL Part C J L624-L642: a natively locked door offers no exit; the leg could only stall.
                A.note(state, "door_locked", destination=destination, location=location, doors=list(locked),
                       time=S._g(S._g(snap, "motor"), "time_of_day"))
                return A.end(state, context, destination.casefold() + "_door_locked")
            step = A.travel(snap, state, destination)
            if step is not None:
                return step
            A.note(state, "quest_target_location", destination=destination, location=location)
            return A.end(state, context, "quest_target_location_unreachable")
        A.note(state, "quest_npc_not_observed", npc=b["target"], location=location, hint=destination or None)
        return A.end(state, context, "quest_npc_not_observed")
    if not _item_selected(snap, context, b["item"]):
        if q.get("selects", 0) >= SELECT_TRIES_MAX:
            return A.end(state, context, "quest_item_not_offered")
        choice = S._select(snap, {b["item"]})
        if choice is None:
            return A.end(state, context, "quest_item_not_offered")
        q["selects"] = q.get("selects", 0) + 1
        return S._decision(choice)
    target = S._g(snap, "world_interaction_target") or {}
    here = A._here(snap)
    entry = min(entries, key=lambda e: (sum(abs(p - r) for p, r in zip(here, S._wave1_target_tile(e))) if here else 0, str(e.get("ref"))))
    tile = S._wave1_target_tile(entry)
    if target.get("kind") == "quest_delivery" and _source_parts(target.get("source_fact_id")) == (b["target"], b["quest_id"]):
        n = (q.get("press") or {}).get("n", 0) + 1
        pubs = context.get("publications")
        if n > DELIVERY_PRESSES_MAX or type(pubs) is not int:
            return A.end(state, context, "quest_delivery_unverified")
        # The press basis: the only receipt that may judge this hand-in is the NEXT one, from exactly one new input.
        q.update(phase="delivering", npc_tile=list(S._wave1_target_tile(target) or tile),
                 press=dict(n=n, pub=pubs, rid=S._g(last, "request_id")))
        if n == 1:
            q["bag_before"] = _bag(context["inventory"])
            # "No gift counted": the NPC's own native gift counters (npc_gift entry) at the hand-in, re-read at the end.
            q["gifts_before"] = dict(ref=entry.get("ref"), **{k: entry.get(k) for k in GIFT_COUNTERS})
        A.note(state, "delivery_pressed", npc=b["target"], quest_id=b["quest_id"], tile=q["npc_tile"], attempt=n)
        return S._decision(S.act("world_interact", {}))
    offered = next((e for e in S._g(snap, "approach_targets", ()) or () if e.get("ref") == entry.get("ref")
                    and e.get("kind") == "npc_gift" and S._wave1_target_tile(e) == tile), None)
    if offered is not None:
        progress = S._g(S._g(snap, "working_memory"), "approach_progress") or {}
        if (progress.get("status") != "ARRIVED" or progress.get("target_ref") != offered["ref"]
                or progress.get("target_tile") != list(tile)):
            # The existing controller re-reads a moving NPC, routes around blocked counter cells to its native
            # stands, then faces/flushes the grab tile. This identity is positional, never permission to give a gift.
            return S._decision(S.act("approach_target", {}, offered["ref"]))
    else:
        # Older snapshots without a catalog still use the bounded observed-open walk and native stand facts.
        step = A.walk_to(snap, state, context, tile, stands=entry.get("stands"))
        if step is not None:
            return step
    step = _npc_grab_flush(snap, state, A, q, tile, last)
    if step is not None:
        return step
    # Faced and freshly read, yet no hand-in offer: the native probe does not match (or a gift would be the meaning).
    refusal = A._refusal(snap, context)
    A.note(state, "delivery_not_offered", npc=b["target"], tile=list(tile), **refusal, **_grab_facts(snap, A, q, tile, last))
    reason = ((refusal.get("extras_refusal") or {}).get("reason") or "")
    if reason.startswith("quest_refused:"):
        return A.end(state, context, reason.replace(":", "_", 1))
    return A.end(state, context, "quest_delivery_not_offered")


def _faced_grab(snap, A, tile, last):
    """(own tile, delta, direction, facing, grab tile, pixel) toward the adjacent NPC tile, or None.

    MotorView carries no facing: the arrival toward ``tile`` names it, and the last receipt's observed facing, when
    present, must agree. The grab tile mirrors Character.GetGrabTile from the observed Farmer.Position pixel."""
    from arti_stardew_runtime import _grab_tile
    here, pixel = A._here(snap), A._pixel(snap)
    if here is None or pixel is None or tile is None:
        return None
    delta = (tile[0] - here[0], tile[1] - here[1])
    direction = S._DIRS.get(delta)
    if direction is None:
        return None
    facing = ("up", "right", "down", "left").index(direction)
    seen = (S._g(last, "after") or {}).get("facing")
    if type(seen) is int and seen != facing:
        return None
    return here, delta, direction, facing, _grab_tile(pixel, facing), pixel


def _npc_grab_flush(snap, state, A, q, tile, last):
    """gap8l (J L806-L821): arrived at Pierre's native stand behind the counter (3,17) facing right at Position.X 186.0,
    GetGrabTile right = (186+8+48+5)//64 = 3 = ARTI's own tile, so the mod's quest capture never ran on Pierre's tile
    and no offer was published. The runtime's one moving-NPC flush (3 ticks, 15.84 px) had landed short; its re-arm is
    consumed only by another approach press, which the skill (ARRIVED) never asked for. As at the crab pot / kitchen /
    counter: short ordinary moves toward the NPC, at most GRAB_FLUSH_MAX, with fresh forward pixel progress and less
    than one tile from the arrival pixel. Only a subsequent native ``quest_delivery`` offer authorizes A."""
    from arti_stardew_interact_b import EAT_SITE_FLUSH_TICKS
    g = _faced_grab(snap, A, tile, last)
    if g is None:
        return None
    here, delta, direction, _, grab, pixel = g
    if grab != here:
        return None
    key = [list(here), list(tile)]
    flush = q.get("grab_flush")
    if not isinstance(flush, dict) or flush.get("key") != key:
        flush = q["grab_flush"] = dict(key=key, origin=list(pixel), last=None, n=0, total=(flush or {}).get("total", 0))
    along = lambda p, o: sum((p[i] - o[i]) * delta[i] for i in range(2))
    if (flush["total"] >= GRAB_FLUSH_MAX or along(pixel, flush["origin"]) >= A.TILE_PX
            or flush["last"] is not None and along(pixel, flush["last"]) <= 0):
        return None
    flush.update(last=list(pixel), n=flush["n"] + 1, total=flush["total"] + 1)
    A.note(state, "quest_grab_flush", tile=list(here), target=list(tile), pixel=list(pixel), grab=list(grab), n=flush["n"])
    return S._decision(S.act("world_key.move_cardinal", dict(direction=direction, ticks=EAT_SITE_FLUSH_TICKS)))


def _grab_facts(snap, A, q, tile, last):
    """Diagnostic only: why the quest capture had nothing to probe (it runs at the native grab tile only)."""
    g = _faced_grab(snap, A, tile, last)
    flushes = (q.get("grab_flush") or {}).get("total", 0)
    if g is None:
        return dict(capture_miss="not_adjacent_or_not_facing", here=list(A._here(snap) or ()) or None,
                    pixel=list(A._pixel(snap) or ()) or None, grab_flushes=flushes)
    here, _, _, facing, grab, pixel = g
    miss = ("grab_tile_is_own_tile" if grab == here else "grab_tile_not_npc_tile" if grab != tuple(tile)
            else "quest_probe_no_offer")
    return dict(capture_miss=miss, here=list(here), pixel=[round(v, 2) for v in pixel], facing=facing,
                grab=list(grab) if grab else None, grab_flushes=flushes)


def _press_receipt(q, last, context):
    """Whose receipt ``last`` is, relative to the hand-in press basis (publications + newest request id at decision).

    own: exactly one input was published since and the receipt is new; not_published: no input and the receipt is the
    pre-press one (the press was refused before it reached the game); pending: published, receipt not yet new;
    otherwise a named mismatch. Only ``own`` may be judged as the delivery."""
    press, pubs = q.get("press"), context.get("publications")
    if not isinstance(press, Mapping) or type(pubs) is not int or type(press.get("pub")) is not int:
        return "no_press_basis"
    rid = S._g(last, "request_id")
    if pubs == press["pub"]:
        return "not_published" if rid == press.get("rid") else "receipt_without_input"
    if pubs != press["pub"] + 1:
        return "foreign_inputs"
    if not rid:
        return "no_receipt"
    return "pending" if rid == press.get("rid") else "own"


def _close_dialogue(snap, state, context, A, q, quests, view):
    """gap8p (J L834-L840): the hand-in's own receipt named Pierre's thank-you DialogueBox (after.receiver_token rcv:mi7),
    but the next decision had no offered close yet (S._menu_close None) and ``_settle`` called the open DialogueBox an
    unexpected menu. Now the S25 closure (shared ``A.dialogue_close_receipt`` / ``dialogue_close_press``, VERIFIED LIVE
    for S25 in 8k): accept only the dialogue the delivery receipt bound, press its offered A (cap 8), follow only each
    receipt's resulting DialogueBox, and take an advanced final page that then closes natively as closed. Only then
    read back the quest, the bag and the NPC's gift counters (``_reconcile``)."""
    def wait(key, gap, question):
        step = S._wait(state, key, gap, question, DIALOGUE_WAIT_S)
        return step if step.kind != "fail" else A.end(state, context, gap)

    if q.get("quest_close_receiver"):
        got = A.dialogue_close_receipt(snap, view, q, "quest")
        if got in ("pending", "unsettled"):
            return wait("quest_dialogue_close", "quest_dialogue_close_unverified", "Amati dialog quest ditutup")
        if got == "closed":
            state.pop("wait:quest_dialogue_close", None)
            return _reconcile(snap, state, context, A, q, quests)
        if got != "advanced":
            last = _last(snap)
            A.note(state, "quest_dialogue_close", receiver=q["quest_close_receiver"], outcome=S._g(last, "outcome"),
                   after=(S._g(last, "after") or {}).get("ui_press"), view=getattr(view, "receiver_token", None))
            return A.end(state, context, "quest_dialogue_close_unverified")
    elif view is None:
        # The extras chain settled before the dialogue's offer was read (L839): await it, bounded; never close blind.
        return wait("quest_dialogue_offer", "quest_dialogue_not_offered", "Amati dialog quest")
    elif view.receiver_token != q["dialogue"]:
        return _fail("unexpected_quest_menu")  # only the delivery receipt's own dialogue is ever pressed
    key = S._g(snap, "ui_key_target") or {}
    if key.get("kind") in ("question", "npc_question") or getattr(view, "options", ()):
        return _fail("unknown_menu_choice")  # never answer a question
    if view.context != "dialogue" or view.menu_type != "DialogueBox" or view.held_item:
        return _fail("unexpected_quest_menu")
    if not view.available or not S._g(snap, "ui_pad_target"):
        return wait("quest_dialogue_offer", "quest_dialogue_not_offered", "Amati dialog quest")
    state.pop("wait:quest_dialogue_offer", None)
    return A.dialogue_close_press(snap, state, q, view, "quest", "quest_dialogue_close")


def _bag(inventory):
    """Exact bag counts per qualified id (the pre-press baseline and the reconcile readback)."""
    return {k: _owned(inventory, k) for k in {f.get("qualified_item_id") for f in inventory} if isinstance(k, str)}


def _reconcile(snap, state, context, A, q, quests):
    """Menus closed: the SAME bound instance completed (or removed) and the bag lost exactly the quest's number."""
    if S._g(S._g(snap, "motor"), "is_player_free") is not True:
        return S._wait(state, "quest_free", "quest_not_free", "Amati keadaan bebas", S.WAIT_S)
    b = q["bound"]
    hits = [f for f in quests if f["instance"] == b["instance"]]
    state_now = "removed" if not hits else "completed" if hits[0]["completed"] is True else "active"
    bag, before = _bag(context["inventory"]), q["bag_before"]
    moved = {k: bag.get(k, 0) - before.get(k, 0) for k in set(bag) | set(before) if bag.get(k, 0) != before.get(k, 0)}
    # No gift counted: the SAME NPC entry's native counters equal those read at the hand-in (a gift raises them).
    gifts = q.get("gifts_before") or {}
    seen = [e for e in context.get("extras", ()) if e.get("kind") == "npc_gift" and e.get("ref") == gifts.get("ref")]
    gifts_now = {k: seen[0].get(k) for k in GIFT_COUNTERS} if len(seen) == 1 else None
    if (gifts_now is None or any(type(gifts.get(k)) is not int or type(gifts_now[k]) is not int for k in GIFT_COUNTERS)):
        step = S._wait(state, "quest_gift_readback", "quest_gift_counter_unverified", "Amati hadiah NPC",
                       QUEST_READBACK_WAIT_S)
        if step.kind != "fail":
            return step
        A.note(state, "quest_gift_counters", ref=gifts.get("ref"), before={k: gifts.get(k) for k in GIFT_COUNTERS},
               now=gifts_now)
        return A.end(state, context, "quest_gift_counter_unverified")
    state.pop("wait:quest_gift_readback", None)
    gift_counted = any(gifts_now[k] != gifts[k] for k in GIFT_COUNTERS)
    if state_now == "active" and not (hits and hits[0]["quest_id"] != b["quest_id"]):
        # The native receipt already saw it complete; allow one bounded quest-log refresh, never a re-delivery.
        step = S._wait(state, "quest_completion_readback", "quest_completion_unverified", "Amati quest selesai",
                       QUEST_READBACK_WAIT_S)
        if step.kind != "fail":
            return step
    A.note(state, "quest_reconciled", quest_state=state_now, moved=moved, gift_counted=gift_counted,
           dialogue=q.get("dialogue"), dialogue_presses=q.get("quest_close_presses", 0), menu_closed=True)
    if state_now == "active" or hits and hits[0]["quest_id"] != b["quest_id"]:
        return A.end(state, context, "quest_completion_unverified")
    if moved != {b["item"]: -b["number"]}:
        return A.end(state, context, "quest_debit_unreconciled")
    if gift_counted:
        return A.end(state, context, "quest_gift_counted")
    return S.Step("done", reason="observed_quest_delivery")
