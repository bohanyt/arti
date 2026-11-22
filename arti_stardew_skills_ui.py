"""Bounded Wave 1 compositions of the existing G1/G6 world and menu controls."""
from collections.abc import Mapping
from copy import deepcopy
import re

import arti_stardew_skills as S
from arti_stardew_generic_world import opened_menu
from arti_stardew_ui_pad import menu_route
from arti_stardew_ui_effects import same_date


def _fail(reason):
    return S.Step("fail", reason=reason)


def _press(snap, button):
    offer = S._g(snap, "ui_pad_target") or {}
    if button not in offer.get("press", {}):
        return _fail("menu_button_not_offered:" + button)
    return S._decision(S.act("ui_press", dict(offer.get("pins") or {},
        btn=button, snapped_id=offer.get("snapped_id"))))


# IClickableMenu neighbour sentinel for a menu-specific snap (UiPadNav.CustomSnap): JunimoNoteMenu's overview bundles
# carry it on all four edges (JunimoNoteMenu.cs:366-371), so no published concrete edge reaches a bundle.
CUSTOM_SNAP = -7777
# JunimoNoteMenu.customSnapBehavior (JunimoNoteMenu.cs:232-284) constants: off-axis weight and the "no candidate" bound.
JUNIMO_SNAP_ACROSS = 3
JUNIMO_SNAP_LIMIT = 10000
# customSnapBehavior runs only from bundle ids region_bundleModifier .. +9 (JunimoNoteMenu.cs:20, :234).
JUNIMO_BUNDLE_ID0 = 5000
JUNIMO_BUNDLE_SPAN = 10
# Every ui_nav of one donation (snap bootstrap + bundle + slot routes); a bounded walk, never a search loop.
DONATION_NAV_MAX = 40
MAIL_UI_RESUME_MAX = 8  # same visit/caps; repeated native interruptions cannot renew it forever


def junimo_custom_snap(view, old, direction):
    """Replica of the native JunimoNoteMenu.customSnapBehavior over the PUBLISHED bundle components and their bounds:
    the bundle whose centre is on the ``direction`` side of the snapped bundle with the least along + 3 x across
    distance, first in native bundle order on ties; None = the native code leaves the bundles (present/area buttons)."""
    boxes = {b[0]: b[1:] for b in getattr(view, "bounds", ()) or ()}
    bundles = sorted(c["id"] for c in view.components if c.get("kind") == "bundle" and c["id"] in boxes)
    if old not in bundles or not 0 <= old - JUNIMO_BUNDLE_ID0 < JUNIMO_BUNDLE_SPAN:
        return None

    def centre(cid):
        x, y, w, h = boxes[cid]
        return x + w // 2, y + h // 2  # XNA Rectangle.Center

    cx, cy = centre(old)
    best, pick = None, None
    for cid in bundles:
        if cid == old:
            continue
        bx, by = centre(cid)
        cost = {"left": cx - bx + abs(cy - by) * JUNIMO_SNAP_ACROSS if bx < cx else None,
                "up": cy - by + abs(cx - bx) * JUNIMO_SNAP_ACROSS if by < cy else None,
                "right": bx - cx + abs(cy - by) * JUNIMO_SNAP_ACROSS if bx > cx else None,
                "down": by - cy + abs(cx - bx) * JUNIMO_SNAP_ACROSS if by > cy else None}[direction]
        if cost is not None and cost < JUNIMO_SNAP_LIMIT and (best is None or cost < best):
            best, pick = cost, cid
    return pick


def _bundle_route(view, start, targets):
    """Directions from a snapped overview bundle to a requested bundle using only native custom-snap edges."""
    rows = {c["id"]: c for c in view.components if c.get("kind") == "bundle"}
    if start not in rows:
        return []
    pending, seen = [(start, [])], {start}
    for cid, path in pending:
        if cid in targets:
            return path
        for direction, raw in zip(("up", "right", "down", "left"), rows[cid].get("nbr", ())):
            nxt = junimo_custom_snap(view, cid, direction) if raw == CUSTOM_SNAP else None
            if nxt is not None and nxt not in seen and nxt not in view.denylisted_ids:
                seen.add(nxt)
                pending.append((nxt, path + [{"from": cid, "dir": direction, "to": nxt}]))
    return []


def _go(snap, view, targets, state=None):
    state = state if state is not None else {}
    expected = state.pop("donation_nav_expect", None)
    if expected is not None and view.snapped_id != expected:
        # Our replica of the native custom snap disagreed with the game: stop, never keep guessing.
        return _fail("donation_navigation_unpredicted")
    if view.snapped_id in targets:
        return _press(snap, "A")
    offer = S._g(snap, "ui_pad_target") or {}
    if not offer.get("nav"):
        return _fail("donation_navigation_not_offered")
    navs = state.get("donation_navs", 0)
    if navs >= DONATION_NAV_MAX:
        return _fail("donation_navigation_cap")
    page = "overview" if view.bundle is None else "bundle:%s" % view.bundle.get("index")
    if view.snapped_id is None or view.snapped_id == -1:
        # S27 live 4a (journal-attempt27 L460): the note opens with nothing snapped (wire -1), so there is no graph
        # origin. One ordinary offered nav per page snaps the native default (UiPadInput.Nav: snapToDefault first).
        booted = state.setdefault("donation_snap_boot", [])
        if page in booted:
            return _fail("donation_navigation_not_offered")
        booted.append(page)
        state["donation_navs"] = navs + 1
        return S._decision(S.act("ui_nav", dict(offer.get("pins") or {}, dir="right")))
    route = menu_route(view.components, view.snapped_id, targets, view.denylisted_ids)
    if not route and view.bundle is None:
        route = _bundle_route(view, view.snapped_id, targets)
        if route:
            state["donation_nav_expect"] = route[0]["to"]
    if not route:
        return _fail("donation_navigation_not_offered")
    state["donation_navs"] = navs + 1
    return S._decision(S.act("ui_nav", dict(offer.get("pins") or {}, dir=route[0]["dir"])))


def _page_text(observation):
    """The prose of one native dialogue page, without the menu's field dump around it."""
    text = observation.get("text") or ""
    match = re.search(r"menu\.dialogues\[\d+\]:\s*(.*?)\s*menu\.", text, re.S)
    return " ".join((match[1] if match else text).split())


def tv_episode_read(trace):
    """(channel, pages) when the native trace shows a TV show watched through: fix63B (ADR 95).

    The generic chooser answers the channel question and the native advance turns the pages, so a run ends with the
    menu already closed and no receipt of its own (a45: 15 of 15 TV reads failed `partial:native_ui_completion_unverified`
    or QuestionOpened while the fortune was on screen, D12:J102). The receiver chain is the proof: the question this run
    opened, one linked answer on it, then the opening page and the actual forecast (vanilla TV, TV.cs:201-257). None
    when any of that is missing; ("left", []) for the channel-less answer."""
    if (trace.get("opened") or {}).get("kind") != "tv":
        return None
    rows = trace.get("observations") or ()
    questions = [o for o in rows if o.get("context") == "question" and o.get("linked")
                 and o.get("receiver") == trace["opened"].get("receiver")]
    # Readiness can publish the same receiver before its responses exist.
    question = next((o for o in reversed(questions) if o.get("options")),
                    next((o for o in questions if "options" not in o), None))
    answer = next((p for p in trace.get("inputs") or () if p.get("linked") and question is not None
                   and p.get("before_receiver") == question["receiver"] and (p.get("args") or {}).get("choice") is not None), None)
    if answer is None:
        return None
    # ADR 122 removes reflection paths from human text. The native option keys
    # already travel separately; motor proof must not parse that presentation.
    keys = ({str(o["index"]): o.get("response_key") for o in question["options"]}
            if "options" in question else
            dict(re.findall(r"menu\.responses\[(\d+)\]\.responseKey:\s*(\S+)", question.get("text") or "")))
    key = keys.get(str(answer["args"]["choice"]))
    if key is None:
        return None
    if key == "(Leave)":
        return "left", []
    channel = {"Weather": "weather", "Fortune": "fortune"}.get(key)
    if channel is None:
        return None  # ADR 95: another show (Livin', Queen) is not leaving; no credit, honest partial
    after = rows[rows.index(question) + 1:]
    pages = list(dict.fromkeys(t for o in after if o.get("menu") == "DialogueBox" and o.get("context") != "question"
                               and (t := _page_text(o))))
    return (channel, pages) if len(pages) >= 2 and after[0].get("linked") else None


def show_chain_intact(state, trace):
    """ADR 124: a finished show passes a continuation pin only with its effect fence. Each input on the show's own
    receiver chain stays linked, in scope and unconsumed; inputs off that chain (a53 d2 J430 reopened it) say nothing."""
    scope, consumed = state.get("ui_effect_scope"), state.get("ui_effect_consumed") or ()
    receivers = {(trace.get("opened") or {}).get("receiver")}
    for entry in trace.get("inputs") or ():
        if entry.get("before_receiver") in receivers:
            if (not entry.get("linked") or scope and entry.get("scope") != scope
                    or entry.get("request_id") in consumed):
                return False
            receivers.add(entry.get("after_receiver"))
    return True


def _plan_input(state):
    return state.setdefault("plan_input", {"weather": None, "fortune": None, "mail": [], "complete": False, "hold": None})


def _receipt(snap, receiver, *, state, context, effect="changed", choice=None):
    from arti_stardew_ui_effects import consume
    return consume(state, context, receiver, (effect,), snap=snap, choice=choice)


def plan(call, snap, state, context):
    # The runtime gates records for freshness/session/screen. Never turn an
    # absent/truncated inventory into zero, or an old UI view into authority.
    if not context.get("ui_complete") or not context.get("inventory_complete"):
        return _fail("composition_readback_unavailable")
    identity = context.get("identity")
    if identity is None or state.setdefault("ui_identity", identity) != identity:
        return _fail("composition_identity_changed")
    last = S._g(S._g(snap, "working_memory"), "last_action")
    if last and (S._g(last, "cleanup_confirmed") is False
                 or S._g(last, "lifecycle") in ("uncertain", "interrupted")):
        return _fail("action_unreconciled")
    view = context.get("ui_view")
    if view is not None and getattr(view, "hud", False):
        view = None  # an inspectable HUD is not an open modal requiring a pad offer
    trace = state.get("native_ui_episode") or {}
    if (view is None and not S.menu_up(snap) and (trace.get("opened") or {}).get("request_id")
            and tuple(trace.get("identity") or ()) == identity[:5]
            and same_date(trace.get("date"), context.get("date"))
            and (not state.get("ui_continuation_receiver")
                 or call.name == "cek_pagi" and tv_episode_read(trace) is not None and show_chain_intact(state, trace))
            and not (call.name == "cek_pagi" and "mail_unread_before" in state)):
        # A chooser may finish handling the native receiver while a composition
        # is suspended. Reconcile that shared handoff BEFORE the old planner
        # can wait for/reopen a receiver which demonstrably already opened.
        # Native UI facts/input effects are creditable; domain completion still
        # needs its own verifier, including any external/watchdog succession.
        reason = "partial:native_ui_completion_unverified"
        read = tv_episode_read(trace) if call.name == "cek_pagi" else None
        if read is not None and read[0] == "left":
            reason = "tv_left_by_choice"
        elif read is not None:
            result = _plan_input(state)
            result.update({read[0]: "\n".join(read[1])}, native_ui=deepcopy(trace), hold=None, read_channel=read[0])
            if call.arg in ("weather", "fortune"):
                if S._g(S._g(snap, "motor"), "is_player_free") is not True:
                    return S._wait(state, "morning_free", "morning_not_free", "Amati keadaan bebas", S.WAIT_S)
                state.pop("native_ui_episode", None)
                if read[0] != call.arg:
                    # ADR 95: she watched the other channel. Keep that real text, credit nothing for the named one.
                    reason = "partial:other_channel_read"
                    result["hold"] = reason
                    return _fail(reason)
                result.update(complete=True, menu_closed=True)
                return S.Step("done", reason="observed_morning_plan_input")
            state.pop("native_ui_episode", None)
            trace = {}
        if trace and "plan_input" in state:
            state["plan_input"].update(native_ui=deepcopy(trace), hold=reason)
        if trace:
            return _fail(reason)
    offer = S._g(snap, "ui_pad_target") or {}
    if view is not None:
        if call.name == "donasi" and not view.available:
            return S._wait(state, "donation_ready", "donation_menu_not_ready", "Tunggu catatan siap", S.WAIT_S)
        if call.name == "cek_pagi" and not view.available:
            waiting = _page_wait(state, view)
            if waiting.kind == "fail" and "plan_input" in state:
                state["plan_input"]["hold"] = waiting.reason
            return waiting
        if call.name == "cek_pagi":
            state.pop("wait:morning_ready", None)  # each verified ready page ends its own animation wait
        pins = offer.get("pins") or {}
        if pins.get("receiver_token") != view.receiver_token or pins.get("target_token") != view.target_token:
            # FINAL Part C2 S29 (journal L142-L150): the decision snapshot was built before the native offer of this
            # freshly opened question was published (no ui_inspect before trial_decision 18), while the live view
            # read at plan time was already available. Never act on (or across) a stale offer: a bounded read-only
            # wait (inspect, no input) lets a fresh snapshot pin this exact view; past the bound it fails as before.
            # S29 only: donation keeps its immediate fail (B1/B24 foreign-receiver and no-offer fences).
            if call.name != "cek_pagi":
                return _fail("composition_menu_not_offered")
            return S._wait(state, "menu_offer", "composition_menu_not_offered", "Tunggu menu ditawarkan", S.WAIT_S)
        state.pop("wait:menu_offer", None)  # each matched offer ends its own stale-offer wait
        state["ui_continuation_receiver"] = view.receiver_token
    if call.name == "cek_pagi":
        step = _morning(snap, state, view, context, selection=call.arg)
        if step.kind == "fail" and "plan_input" in state:
            state["plan_input"]["hold"] = step.reason
        return step
    return _donate(call, snap, state, context, view)


def _page_wait(state, view):
    """ADR 98 (a46 nine `morning_ui_not_ready`, d3 J82-J108): one wait per native page state. The plan does not run while
    the chooser holds a ready page, so a window opened on page 1 used to expire on page 2 with the forecast on screen."""
    page = (view.receiver_token, bool(view.available))
    if state.get("morning_ready_page") != page:
        state["morning_ready_page"] = page
        state.pop("wait:morning_ready", None)
    return S._wait(state, "morning_ready", "morning_ui_not_ready", "Tunggu teks native siap", S.MORNING_READY_WAIT_S)


def _morning(snap, state, view, context, *, selection=None):
    # Bare S29 retains the historical Part C TV+mail composition. Live7 offers explicit,
    # independent choices: one supported native channel or mail with no TV prerequisite.
    if selection not in (None, "weather", "fortune", "mail"):
        return _fail("morning_selection_not_supported")
    channels = ("weather", "fortune") if selection is None else (() if selection == "mail" else (selection,))
    time = S._g(S._g(snap, "motor"), "time_of_day")
    if type(time) is not int or not 600 <= time < (1200 if selection is None else 2600):
        return _fail("not_morning")
    result = _plan_input(state)
    identity = context.get("identity")
    if state.setdefault("morning_identity", identity[:5]) != identity[:5]:
        return _fail("morning_identity_changed")
    if state.setdefault("morning_date", context.get("date")) != context.get("date"):
        return _fail("morning_date_changed")
    last = S._g(S._g(snap, "working_memory"), "last_action")
    pending = state.get("morning_pending")
    if "mail_unread_before" in state:
        # ADR 70: an open/closed UI is not the mail effect. The same actor's
        # native queue must lose exactly one letter for each mailbox press.
        count = context.get("mailbox_count")
        if type(count) is not int or count < 0:
            return _fail("unsupported:mailbox_presence_unavailable")
        if count > state["mail_unread_before"]:
            # ADR 76 (fix54 review L5): a letter that arrives during the visit is a fact for her, not the
            # end of the visit. Re-baseline once per visit. With a letter open (or its close pending) the
            # native queue already lost that letter at the open, so the next exact one-letter decrease is
            # measured from count + 1; before the open it is measured from count. A second rise in the same
            # visit still ends the leg with the named reason; MAIL_UI_RESUME_MAX, the deadline and the
            # per-letter decrement rule are unchanged, so the drain stays bounded.
            if state.get("mail_arrival") is not None:
                return _fail("mail_unread_increased")
            opened = (view is not None or S.menu_up(snap) or pending not in (None, "mailbox")
                      or bool(((state.get("native_ui_episode") or {}).get("opened") or {}).get("request_id")))
            arrival = dict(before=state["mail_unread_before"], observed=count, letter_open=bool(opened),
                           fact="new letter arrived while reading")
            state["mail_arrival"] = arrival
            result.setdefault("mail_arrivals", []).append(dict(arrival))
            state["mail_unread_before"] = count + 1 if opened else count
        if view is None and not S.menu_up(snap):
            if count == state["mail_unread_before"]:
                return S._wait(state, "mail_decrement", "mail_unread_not_decreased",
                               "Amati jumlah surat native", S.WAIT_S)
            if count != state["mail_unread_before"] - 1:
                return _fail("mail_unread_delta_unexpected")
            trace = state.get("native_ui_episode") or {}
            if trace and (tuple(trace.get("identity") or ()) != identity[:5]
                          or not same_date(trace.get("date"), context.get("date"))
                          or (trace.get("opened") or {}).get("kind") not in (None, "mailbox")
                          or (trace.get("opened") or {}).get("menu") != "LetterViewerMenu"):
                return _fail("mail_handoff_identity_unverified")
            proof = dict(before=state.pop("mail_unread_before"), after=count)
            if state.get("mail_arrival") is not None and not state["mail_arrival"].get("credited"):
                state["mail_arrival"]["credited"] = True
                proof["arrival"] = dict(state["mail_arrival"])  # the re-baselined before, with its cause
            if trace:
                proof["native_ui"] = deepcopy(trace)
                # Generic native letter menus use context=other. Preserve their
                # observed prose without claiming unlinked inputs as ours.
                pages = [o["text"] for o in trace.get("observations", ())
                         if o.get("menu") == "LetterViewerMenu" and o.get("text")]
                if pages and not state.get("mail_pages"):
                    result["mail"].append("\n".join(dict.fromkeys(pages)))
            result.setdefault("mail_receipts", []).append(proof)
            state["morning_letters_read"] = state.get("morning_letters_read", 0) + 1
            result.update(letters_read=state["morning_letters_read"], mailbox_remaining=count)
            for key in ("morning_pending", "mail_receiver", "mail_pages", "native_ui_episode",
                        "ui_continuation_receiver", "wait:mail_decrement", "wait:morning_open",
                        "mail_attachment_wait", "morning_letter_held", "wait:mail_attachment", "wait:mail_ui"):
                state.pop(key, None)
            result.pop("held_letter", None)
            pending = None
        elif view is not None and (view.context != "letter" or getattr(view, "generic", False)):
            # The existing native UI chooser owns gifts, attachments and their
            # successors. Keep the queue baseline until it returns to the HUD.
            return S._wait(state, "mail_ui", "mail_ui_not_closed", "Tunggu dialog surat selesai", S.MORNING_READY_WAIT_S)
    if view is None:
        if S.menu_up(snap):
            return _fail("morning_menu_unreadable")
        if pending in ("tv", "mailbox"):
            return S._wait(state, "morning_open", "morning_receiver_not_opened", "Amati menu yang dibuka", S.WAIT_S)
        if state.get("morning_channel"):
            closed = _receipt(snap, state.get("morning_page_receiver") or state.get("morning_receiver"),
                state=state, context=context, effect="closed",
                choice=state.get("morning_choice") if state.get("morning_pending") == "reading" else None)
            if not closed:
                return _fail("tv_close_unverified")
            channel = state["morning_channel"]
            pages = state.setdefault("morning_pages", [])
            for observation in closed.get("observations", ()):
                if (observation.get("menu") == "DialogueBox" and observation.get("context") != "question"
                        and observation.get("text") and observation["text"] not in pages):
                    pages.append(observation["text"])
            if len(pages) < 2:  # vanilla TV: opening, then actual forecast/fortune (TV.cs:201-257)
                return S._wait(state, "morning_page", "tv_forecast_not_read", "Tunggu ramalan native berikutnya", S.WAIT_S)
            result[channel] = "\n".join(pages)
            result["hold"] = None
            state.pop("morning_channel")
            state.pop("morning_pages", None)
        if pending == "mail_close":  # legacy already-open letter (no mailbox press baseline)
            if not _receipt(snap, state.get("mail_receiver"), state=state, context=context, effect="closed"):
                return _fail("mail_close_unverified")
            state.pop("morning_pending", None)
            state.pop("mail_receiver", None)
            state.pop("mail_pages", None)
            # Without a pre-open count this closure/prose is an observation,
            # never credit for a new mailbox read. Subsequent presses are verified above.
        channel = next((k for k in channels if result[k] is None), None)
        if channel is not None:
            return _morning_open(snap, state, "tv", result)
        if selection in ("weather", "fortune"):
            if S._g(S._g(snap, "motor"), "is_player_free") is not True:
                return S._wait(state, "morning_free", "morning_not_free", "Amati keadaan bebas", S.WAIT_S)
            result.update(complete=True, hold=None, menu_closed=True)
            return S.Step("done", reason="observed_morning_plan_input")
        count = context.get("mailbox_count")
        if type(count) is not int or count < 0:
            result["hold"] = "unsupported:mailbox_presence_unavailable"
            return _fail(result["hold"])
        # Part C readback: the native mailbox count when the mail leg begins (TV first, so no letter is read yet).
        result.setdefault("mailbox_start", count + state.get("morning_letters_read", 0))
        if count:
            result["mailbox_remaining"] = count
            if str(S._g(S._g(snap, "motor"), "location") or "").startswith(("Cabin", "FarmHouse")):
                return S.wave1_repin(state, "pergi ke farm", "morning_farm", "observed_location_change_objective")
            return _morning_open(snap, state, "mailbox", result)
        if S._g(S._g(snap, "motor"), "is_player_free") is not True:
            return S._wait(state, "morning_free", "morning_not_free", "Amati keadaan bebas", S.WAIT_S)
        result.update(complete=True, hold=None, mailbox_end=count, letters_read=state.get("morning_letters_read", 0),
                      menu_closed=True)
        return S.Step("done", reason="observed_morning_plan_input")
    if pending in ("tv", "mailbox"):
        expected = "QuestionOpened" if pending == "tv" else "MenuOpened"
        opened = opened_menu(last)
        generic_open = (state.get("morning_generic_open") is True and opened is not None
            and opened["type"] == view.menu_type and opened["receiver"] == view.receiver_token
            and (view.context == "question" and (getattr(view, "question_key", None)
                or (S._g(snap, "ui_key_target") or {}).get("question_key")) == "TV"
                if pending == "tv" else view.context == "letter"))
        if not (generic_open or S._g(last, "request_id") and S._g(last, "lifecycle") == "completed"
                and S._g(last, "cleanup_confirmed") is True and S._g(last, "outcome") == expected
                and S._g(last, "world_effect_status") == "observed_extras_effect"
                and (S._g(last, "after") or {}).get("kind") == pending):
            return _fail("morning_open_receipt_unverified")
        state["morning_pending"] = "channel" if pending == "tv" else "mail"
        result["hold"] = None
        state["morning_receiver"] = view.receiver_token
        if pending == "tv":
            state["morning_tv_receiver"] = view.receiver_token
            state["morning_open_request"] = S._g(last, "request_id")
    if (view.context == "question" and state.get("morning_pending") == "reading"
            and S._g(last, "lifecycle") == "rejected" and S._g(last, "cleanup_confirmed") is True
            and S._g(last, "world_effect_status") == "unchanged"):
        return _fail("tv_channel_arm_rejected")
    if view.context == "question":
        question = getattr(view, "question_key", None) or (S._g(snap, "ui_key_target") or {}).get("question_key")
        phase = state.get("morning_pending")
        if (phase not in ("channel", "reading") or view.receiver_token != state.get("morning_receiver")
                or question not in (None, "TV")):
            return _fail("unexpected_morning_question")
        channel = next((k for k in channels if result[k] is None), None)
        if channel is None:
            return _fail("unexpected_morning_question")
        wanted = "Weather" if channel == "weather" else "Fortune"
        choices = [o for o in view.options if o.get("response_key") == wanted]
        if len(choices) != 1 or "A" not in (S._g(snap, "ui_pad_target") or {}).get("press", {}):
            return _fail("tv_channel_not_offered")
        if phase == "reading":
            if str(choices[0]["index"]) != state.get("morning_choice"):
                return _fail("tv_channel_not_offered")
            # B3 D1 J108-J114: planning advanced the phase, then the runtime
            # discarded that choice for a changed native context BEFORE dispatch.
            # Only the unchanged verified TV-open receipt permits replanning.
            # An answered question may linger in observation; never answer twice.
            if not (S._g(last, "request_id") == state.get("morning_open_request")
                    and S._g(last, "lifecycle") == "completed" and S._g(last, "cleanup_confirmed") is True
                    and (S._g(last, "world_effect_status") == "observed_extras_effect"
                        and S._g(last, "outcome") == "QuestionOpened" and (S._g(last, "after") or {}).get("kind") == "tv"
                        or state.get("morning_generic_open") is True and opened_menu(last) ==
                            dict(type=view.menu_type, receiver=view.receiver_token))):
                return S._wait(state, "morning_channel_send", "tv_channel_selection_unverified",
                               "Amati hasil pilihan saluran TV", S.WAIT_S)
        sending = S._wait(state, "morning_channel_send", "tv_channel_not_dispatched",
                          "Tunggu pilihan saluran TV", S.WAIT_S)
        if sending.kind == "fail":
            return sending
        state.update(morning_channel=channel, morning_pending="reading", morning_pages=[],
                     morning_choice=str(choices[0]["index"]))
        return S._decision(S.act("ui_press", dict(view.press_args("A"), choice=state["morning_choice"])))
    if view.context == "dialogue" and state.get("morning_channel"):
        state.pop("wait:morning_page", None)
        from arti_stardew_ui_effects import consume
        effect = consume(state, context,
            state.get("morning_receiver") if state.get("morning_pending") == "reading" else state.get("morning_page_receiver"),
            ("advanced", "replaced", "answered", "changed"), snap=snap, successor=view.receiver_token,
            choice=state.get("morning_choice") if state.get("morning_pending") == "reading" else None)
        if effect is None:
            return _fail("tv_channel_selection_unverified" if state.get("morning_pending") == "reading" else "tv_page_advance_unverified")
        if state.get("morning_pending") == "reading":
            state["morning_pending"] = "pages"
            state.pop("wait:morning_channel_send", None)
        if effect.get("after_receiver") != view.receiver_token or effect.get("result_menu") != "DialogueBox":
            return _fail("tv_page_receiver_unverified")
        state["morning_page_receiver"] = state["morning_tv_receiver"] = view.receiver_token
        if not view.text:
            return _fail("tv_text_unavailable")
        if not state["morning_pages"] or state["morning_pages"][-1] != view.text:
            state["morning_pages"].append(view.text)
        if len(state["morning_pages"]) > 6:
            return _fail("tv_page_bound")
        return _press(snap, "A")  # existing verified dialogue advance, never a response choice
    if (view.context == "other" and view.menu_type == "DialogueBox" and selection != "mail"
            and ((state.get("native_ui_episode") or {}).get("opened") or {}).get("kind") == "tv"):
        # ADR 98 (a46 d4 J188-J191): a generic page of the show this run opened. Her choice and the native advance own it;
        # tv_episode_read gives the verdict once it closes. Never an unexpected menu.
        return _page_wait(state, view)
    if view.context != "letter" or state.get("morning_pending") not in ("mail", "mail_close"):
        # Legacy already-open letters may be read, but cannot complete TV/weather/fortune.
        if view.context != "letter" or pending is not None:
            return _fail("unexpected_morning_menu")
    if state.setdefault("mail_receiver", view.receiver_token) != view.receiver_token:
        return _fail("mail_receiver_changed")
    if not view.text:
        return _fail("mail_text_unavailable")
    pages = state.setdefault("mail_pages", [])
    if not pages:
        state["mail_index"] = len(result["mail"])
        result["mail"].append(view.text)
    if not pages or pages[-1] != view.text:
        pages.append(view.text)
        result["mail"][state["mail_index"]] = "\n".join(pages)
    if len(pages) > 8:
        return _fail("mail_page_bound")
    following = [c["id"] for c in view.components if c.get("id") == 102 and c.get("visible", True)]
    if following:
        # Native RightTrigger advances only the text page, even with no snap.
        # Never navigate through attachment / quest / membership components.
        return _press(snap, "RT")
    # Vanilla 1.6.15 LetterViewerMenu.cleanupBeforeExit (decomp :1009-1014) ACCEPTS a presented quest / special order
    # and takes every attachment when the letter closes, and the mod lets that B close through (UiPadInput Letter B
    # -> ui_key close). A presented quest is NOT a safety matter: vanilla offers no decline, the close only files the
    # quest in her journal, and she chooses later whether to pursue it (Live7 2026-10-24: a held quest letter ended the
    # stream day in SAFETY_STOP). S29 records the quest as a fact and closes. Only attachments still hold the letter
    # open (a full bag turns the close into an ItemGrabMenu the cleanup cannot own).
    key = context.get("ui_key_view")
    held = letter_close_hold(key, view.receiver_token)
    if held == "facts_unavailable":
        waiting = S._wait(state, "mail_letter_facts", "mail_letter_held:facts_unavailable", "Amati isi surat",
                          S.WAIT_S)
        if waiting.kind != "fail":
            return waiting
    if held:
        state["morning_letter_held"] = view.receiver_token
        result["held_letter"] = dict(receiver=view.receiver_token, index=state["mail_index"], reason=held)
        if held == "attachment" and state.get("live7") and "mail_unread_before" in state:
            state["mail_attachment_wait"] = True
            return S._wait(state, "mail_attachment", "mail_attachment_not_settled",
                           "Amati pilihan lampiran surat", S.MORNING_READY_WAIT_S)
        return _fail("mail_letter_held:" + held)
    quest = letter_quest_fact(key, view.receiver_token, context.get("mines_ui"), state["mail_index"])
    if quest is not None and not any(q.get("receiver") == view.receiver_token for q in result.setdefault("mail_quests", [])):
        result["mail_quests"].append(quest)
    state.pop("wait:mail_letter_facts", None)
    state["morning_pending"] = "mail_close"
    return _press(snap, "B")  # proven: no attachment; a presented quest is filed natively by this close (her fact)


def letter_close_hold(key, receiver):
    """None when closing this open mail letter is safe; else why it must stay open.

    ``key`` is the current ui_key view of the SAME receiver (mod UiKeyLetterFacts: has_quest = HasQuestOrSpecialOrder,
    has_items = itemsLeftToGrab()). A presented quest never holds: the vanilla close files it, which is her business."""
    letter = getattr(key, "letter", None) if getattr(key, "receiver_token", None) == receiver else None
    if not isinstance(letter, Mapping) or any(type(letter.get(k)) is not bool for k in ("has_quest", "has_items")):
        return "facts_unavailable"
    return "attachment" if letter["has_items"] else None


def letter_quest_fact(key, receiver, ui, index):
    """The quest this open letter presents, as a fact for her (None when the letter presents none).

    ``ui`` = ui.context facts; its ``letter_quest`` (mod QuestFacts) names the quest with this letter's receiver token."""
    letter = getattr(key, "letter", None) if getattr(key, "receiver_token", None) == receiver else None
    presented = ui.get("letter_quest") if isinstance(ui, Mapping) else None
    if not (isinstance(presented, Mapping) and presented.get("receiver_token") == receiver):
        presented = None
    if presented is None and not (isinstance(letter, Mapping) and letter.get("has_quest") is True):
        return None
    fact = dict(receiver=receiver, index=index, accepted_on_close=True)
    for k in ("quest_id", "type", "title", "target", "item", "number"):
        v = (presented or {}).get(k)
        if isinstance(v, (str, int)) and not isinstance(v, bool) and v != "":
            fact[k] = v
    return fact


def _morning_open(snap, state, kind, result):
    target = S._g(snap, "world_interaction_target") or {}
    if target.get("kind") == kind:
        if kind == "mailbox":
            state["mail_unread_before"] = result["mailbox_remaining"]
            state.pop("wait:mail_ui", None)
        state["morning_pending"] = kind
        args = S.native_action_args(snap, target)
        state["morning_generic_open"] = bool(args)
        return S._decision(S.act("world_interact", args))
    approach = S._approach(snap, lambda t: t.get("kind") == kind)
    if approach:
        return S._decision(approach)
    # ADR 98 (a46 d24 J216, J1136): native TV / mailbox offers need a plain hand (WorldExtras GHeldPlain); with a rod or
    # scythe in hand the box was simply never offered and she was told it does not exist.
    hand = S.plain_hand_step(snap, state)
    if hand is not None:
        return hand
    result["hold"] = ("unsupported:tv_object_action_not_offered" if kind == "tv"
                       else "unsupported:mailbox_action_not_offered")
    return _fail(result["hold"])


def blacksmith_cleanup_args(run, last, view, key, current_identity):
    """Leave only the exact Blacksmith question this upgrade attempt opened, using its native Leave answer."""
    a = run.state.get("attempt", {}) if run is not None else {}
    if (run is None or run.call.name != "upgrade_alat" or view is None or key is None
            or not view.available or not key.available or view.context != "question" or key.kind != "question"
            or key.question_key != "Blacksmith" or view.receiver_token != key.receiver_token
            or current_identity is None or a.get("clint_identity") != current_identity):
        return None
    receiver = a.get("clint_question")
    after = S._g(last, "after") or {}
    if (receiver is None and a.get("clint_phase") == "counter" and S._g(last, "request_id")
            and S._g(last, "lifecycle") == "completed" and S._g(last, "cleanup_confirmed") is True
            and S._g(last, "world_effect_status") == "observed_extras_effect"
            and S._g(last, "outcome") == "QuestionOpened" and after.get("kind") == "blacksmith"):
        receiver = after.get("receiver_token")
    leave = [o for o in key.options if o.get("response_key") == "Leave"]
    if receiver != key.receiver_token or len(leave) != 1:
        return None
    return key.args_for("answer", option_index=leave[0]["index"])


def tv_cleanup_args(run, last, view, key, current_identity=None):
    """Leave the owned question or advance its verified TV dialogue chain to closure."""
    if (run is None or run.call.name != "cek_pagi" or view is None or key is None
            or not view.available or not key.available or view.receiver_token != key.receiver_token):
        return None
    if run.state.get("ui_identity") is not None and run.state["ui_identity"] != current_identity:
        return None
    receiver = run.state.get("morning_tv_receiver")
    after = S._g(last, "after") or {}
    if view.context == "dialogue" and run.state.get("morning_channel"):
        result = after.get("ui_press") or {}
        if (receiver != view.receiver_token and _receipt_from_action(last, receiver)
                and result.get("effect") in ("replaced", "answered", "advanced")
                and result.get("result_receiver_token") == view.receiver_token
                and result.get("result_menu_type") == "DialogueBox"):
            receiver = view.receiver_token
        return key.args_for("advance") if receiver == key.receiver_token and key.kind == "dialogue" else None
    if view.context != "question" or key.question_key != "TV":
        return None
    if (run.state.get("morning_pending") == "tv" and S._g(last, "request_id")
            and S._g(last, "lifecycle") == "completed" and S._g(last, "cleanup_confirmed") is True
            and S._g(last, "world_effect_status") == "observed_extras_effect"
            and S._g(last, "outcome") == "QuestionOpened" and after.get("kind") == "tv"
            and after.get("generation") == key.context_generation):
        receiver = after.get("receiver_token")
    leave = [o for o in key.options if o.get("response_key") == "(Leave)"]
    if receiver != key.receiver_token or len(leave) != 1:
        return None
    return key.args_for("answer", option_index=leave[0]["index"])


def _receipt_from_action(last, receiver):
    press = (S._g(last, "before") or {}).get("ui_press") or {}
    return bool(S._g(last, "request_id") and S._g(last, "lifecycle") == "completed"
        and S._g(last, "cleanup_confirmed") is True and S._g(last, "world_effect_status") == "observed_ui_press"
        and press.get("receiver_token") == receiver and press.get("btn") == "A")


def _donate(call, snap, state, context, view):
    if not call.arg or " ke " not in call.arg:
        return _fail("donation_item_not_requested")
    item, destination = call.arg.rsplit(" ke ", 1)
    if destination == "museum":
        return _fail("unsupported:museum_piece_ledger_and_counter_action_unavailable")
    if not destination.startswith("bundle "):
        return _fail("donation_target_not_offered")
    requested = destination[7:]
    inventory = context["inventory"]
    bag = {}
    for fact in inventory:
        if isinstance(fact.get("qualified_item_id"), str) and type(fact.get("stack")) is int:
            q = fact["qualified_item_id"]
            bag[q] = bag.get(q, 0) + fact["stack"]
    state.setdefault("donation_bag_before", bag)
    matches = [f for f in inventory if (str(f.get("qualified_item_id", "")).casefold() == item
        or str(f.get("name") or f.get("display_name") or "").casefold() == item)]
    if "donation_qid" not in state:
        qids = {f.get("qualified_item_id") for f in matches}
        if len(qids) != 1:
            return _fail("donation_item_not_owned" if not qids else "donation_item_ambiguous")
        qid = next(iter(qids))
        if context.get("disposal_refusals", {}).get(qid):
            return _fail("donation_item_policy_refused")
        state["donation_qid"] = qid
        state["donation_before"] = sum(f.get("stack", 0) for f in inventory if f.get("qualified_item_id") == qid)
    qid = state["donation_qid"]
    count = sum(f.get("stack", 0) for f in inventory if f.get("qualified_item_id") == qid)
    expected_bag = dict(state["donation_bag_before"])
    expected_bag[qid] = expected_bag.get(qid, 0) - 1
    expected_bag = {q: n for q, n in expected_bag.items() if n}
    if (state.get("donation_pending") or state.get("donation_effect")) and bag != expected_bag:
        return _fail("donation_bag_or_close_unverified")
    if state.get("donation_effect"):
        if view is None:
            if S.menu_up(snap) or count != state["donation_before"] - 1:
                return _fail("donation_bag_or_close_unverified")
            state["donation_evidence"] = dict(item=qid, quantity=1, bundle=requested,
                bag_before=state["donation_before"], bag_after=count, persistent_completed=True, menu_closed=True)
            return S.Step("done", reason="observed_requested_donation")
        if view.context != "junimo_note" or view.held_item:
            return _fail("donation_close_unverified")
        return _press(snap, "B")
    if view is None:
        if S.menu_up(snap):
            return _fail("unexpected_donation_menu")
        if state.get("donation_open_pending"):
            return S._wait(state, "donation_open", "community_note_not_opened", "Amati catatan fisik", S.WAIT_S)
        notes = [e for e in context.get("extras", ()) if e.get("kind") == "community_note"
                 and e.get("ready") is not False and requested in [str(n).casefold() for n in e.get("bundles", ())]]
        if not notes:
            return _fail("unsupported:community_center_bundle_opener_not_offered;inventory_bundle_view_only")
        from arti_stardew_skills_attempts import walk_to
        entry = notes[0]
        tile = (entry["x"], entry["y"])
        target = S._g(snap, "world_interaction_target") or {}
        if target.get("kind") == "community_note" and S._wave1_target_tile(target) == tile:
            state["donation_open_pending"] = True
            state["donation_note_source"] = target.get("source_fact_id")
            return S._decision(S.act("world_interact", {}))
        step = walk_to(snap, state, context, tile, stands=entry.get("stands"))
        return step if step is not None else _fail("community_note_not_offered")
    if "donation_note_receiver" not in state:
        last = S._g(S._g(snap, "working_memory"), "last_action")
        after = S._g(last, "after") or {}
        if (not state.get("donation_open_pending") or not S._g(last, "request_id")
                or S._g(last, "lifecycle") != "completed" or S._g(last, "cleanup_confirmed") is not True
                or S._g(last, "world_effect_status") != "observed_extras_effect"
                or S._g(last, "outcome") != "MenuOpened" or after.get("kind") != "community_note"
                or after.get("receiver_token") != view.receiver_token):
            return _fail("donation_physical_note_unverified")
        state["donation_note_receiver"] = view.receiver_token
    if state["donation_note_receiver"] != view.receiver_token:
        return _fail("donation_receiver_changed")
    if view.context != "junimo_note":
        return _fail("unexpected_donation_menu")
    bundle = view.bundle
    if bundle is None:
        return _go(snap, view, [c["id"] for c in view.components
            if c.get("kind") == "bundle" and str(c.get("label", "")).casefold() == requested], state)
    if str(bundle["name"]).casefold() != requested:
        return _fail("donation_bundle_not_requested")
    identity = (view.receiver_token, bundle["index"])
    if state.setdefault("donation_menu", identity) != identity:
        return _fail("donation_receiver_changed")
    if state.get("donation_pending"):
        ingredient = next((i for i in bundle["ingredients"] if i["index"] == state["donation_ingredient"]), {})
        last_press = (S._g(S._g(S._g(snap, "working_memory"), "last_action"), "before") or {}).get("ui_press") or {}
        if (not _receipt(snap, view.receiver_token, state=state, context=context) or ingredient.get("completed") is not True
                or ingredient.get("persistent_completed") is not True or view.held_item
                or tuple(ingredient.get(k) for k in ("item", "need", "quality")) != state["donation_requirement"]
                or last_press.get("snapped_id") != state["donation_slot"]
                or last_press.get("context") != "junimo_note"
                or count != state["donation_before"] - 1):
            return _fail("donation_effect_or_bag_unverified")
        state["donation_effect"] = True
        return _press(snap, "B")
    if view.held_item:
        if (view.held_item.get("item") != qid or view.held_item.get("stack") != 1
                or not state.get("donation_pick") or count != state["donation_before"] - 1):
            return _fail("donation_held_item_mismatch")
        targets = bundle["accepting_slots"]
        if view.snapped_id in targets:
            state["donation_pending"] = True
            state["donation_slot"] = view.snapped_id
        return _go(snap, view, targets, state)
    if not bundle.get("deposits_allowed"):
        return _fail("unsupported:community_center_bundle_opener_not_offered;inventory_bundle_view_only")
    if count != state["donation_before"]:
        return _fail("donation_precondition_changed")
    slots = {c["id"]: c for c in view.components if c.get("kind") == "slot"
             and (c.get("value") or {}).get("item") == qid and (c.get("value") or {}).get("stack") == 1}
    requirements = [i for i in bundle["ingredients"] if i.get("completed") is False
        and i.get("persistent_completed") is False and i.get("need") == 1
        and set(i.get("matching_slots", ())) & slots.keys()]
    if len(requirements) != 1:
        return _fail("donation_item_not_requested_by_bundle_or_requires_stack_split")
    state["donation_ingredient"] = requirements[0]["index"]
    state["donation_requirement"] = tuple(requirements[0][k] for k in ("item", "need", "quality"))
    targets = set(requirements[0]["matching_slots"]) & slots.keys()
    if view.snapped_id in targets:
        state["donation_pick"] = True
    return _go(snap, view, targets, state)
