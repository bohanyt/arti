"""Primitives v2 WS-G3: pure helpers for text / number entry, events and the ReadyCheck (no IO, no provider).

The mod is authoritative (``ui_pad`` contexts ``naming`` / ``number`` / ``ready_check`` / ``event`` with a ``g3``
sub-block, plus the top-level ``ui_event`` / ``ready_check`` blocks; Protocol/UiTextEventInput.cs). This module

* parses the owner's G3 objectives (authenticated owner text only, never model / scene text):
  "name the animal <x>" (D3: the owner names it; the model never types names),
  "answer the event question [with <n>]", "[recorded remark omitted]";
* plans ONE deterministic runtime step per tick (``act`` = exact ui_press args copied from the published block,
  ``wait``, ``model`` = the event question is offered to the model, ``stop`` = refused / bounded out, ``done``);
* records progress only from VERIFIED ui_press results (effect + ``ui_pad_readback``) and fresh observations;
* computes the finish verdict (``text_rule``: the entered name read back equals the owner's name; the event answer
  read back; the ready check observed and then cleared).

Policies mirrored from the mod (the mod refuses first anyway): skip never (D6; FIXB31: opt-in plain-cutscene skip, see
``event_skip_step``), ReadyCheck confirm only a sleep check ARTI started / wait otherwise, never cancel (D4, D13), event
questions only on the D7 allowlist.

Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G3 live witnesses.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

import arti_stardew_command as command

OBJ_NAME = "name"
OBJ_EVENT = "event_question"
OBJ_READY = "ready_check"

FINISH_NAMED = "observed_name_entered"
FINISH_ANSWERED = "observed_event_question_answered"
FINISH_READY = "observed_ready_check_cleared"

MAX_EVENT_ADVANCES = 40
MAX_STEPS = 48
G3_CONTEXTS = ("naming", "number", "ready_check", "event")


@dataclass
class TextEventObjective:
    """One owner-authored G3 objective and its verified progress."""
    kind: str
    name: str | None = None            # OBJ_NAME: the owner's exact name (D3)
    choice: int | None = None          # OBJ_EVENT: the owner's answer index (None = offered to the model)
    naming_seen: bool = False
    named: bool = False
    readback: str | None = None        # the mod's ui_pad_readback of the finishing step
    advances: int = 0
    question_seen: bool = False
    question_key: str | None = None
    question_class: str | None = None
    answered: bool = False
    ready_seen: bool = False
    ready_kind: str | None = None
    ready_count: int | None = None
    ready_needed: int | None = None
    ready_policy: str | None = None
    ready_cleared: bool = False
    steps: int = 0
    blocked: str | None = None
    finish_recorded: bool = False


_NAME_VERB = r"(?:name|beri nama|kasih nama|namai|namain)"
_NAME_WHAT = (r"(?:(?:the |my |a |new |si )*(?:animal|chicken|cow|duck|goat|sheep|pig|rabbit|pet|dog|cat|horse|baby|child"
              r"|hewan|ayam|sapi|bebek|kambing|domba|babi|kelinci|anjing|kucing|kuda|bayi|anak)(?:nya| baru)?\s+)?")
_NAME_RE = re.compile(r"^\s*" + _NAME_VERB + r"\s+" + _NAME_WHAT + r"(?:with |dengan |as |jadi |sebagai )?[\"'“”]?(?P<name>.+?)[\"'“”]?\s*[.!]?\s*$",
                      re.IGNORECASE)
_EVENT_RE = re.compile(
    r"^\s*(?:answer|jawab)\s+(?:the |an |pertanyaan |soal )?(?:event|cutscene|acara)(?: question| pertanyaannya)?"
    r"(?:\s+(?:with|dengan|pakai)\s+(?:(?:choice|option|answer|pilihan|jawaban|nomor|no\.?)\s*)?(?P<choice>\d{1,2}))?\s*[.!]?\s*$",
    re.IGNORECASE)
_EVENT_RE_ID = re.compile(
    r"^\s*jawab\s+pertanyaan\s+(?:di\s+)?(?:event|acara|cutscene)(?:nya)?"
    r"(?:\s+(?:dengan|pakai)\s+(?:(?:pilihan|jawaban|nomor)\s*)?(?P<choice>\d{1,2}))?\s*[.!]?\s*$", re.IGNORECASE)
_READY_RE = re.compile(r"^\s*(?:wait for|tunggu|tungguin)\s+(?:the |a )?(?:ready[ -]?check|readycheck|semua siap|pemain lain siap)\s*[.!]?\s*$",
                       re.IGNORECASE)


def parse_text_event_objective(text: str) -> TextEventObjective | None:
    """Owner text -> objective, or None. A name that breaks the mod's name policy is never an objective."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    m = _READY_RE.match(t)
    if m:
        return TextEventObjective(OBJ_READY)
    m = _EVENT_RE.match(t) or _EVENT_RE_ID.match(t)
    if m:
        choice = m.group("choice")
        return TextEventObjective(OBJ_EVENT, choice=int(choice) - 1 if choice else None) if not choice or int(choice) >= 1 else None
    m = _NAME_RE.match(t)
    if m:
        name = m.group("name").strip()
        first = name.casefold().split()[0] if name.split() else ""
        if command.name_refusal(name) is None and first not in _NAME_STOP:
            return TextEventObjective(OBJ_NAME, name=name)
    return None


# A "name" that is really the noun / article ("name the chicken" without a name) is never an objective.
_NAME_STOP = frozenset({"the", "my", "a", "an", "new", "si", "it", "him", "her", "them", "animal", "chicken", "cow", "duck",
                        "goat", "sheep", "pig", "rabbit", "pet", "dog", "cat", "horse", "baby", "child", "hewan", "ayam",
                        "sapi", "bebek", "kambing", "domba", "babi", "kelinci", "anjing", "kucing", "kuda", "bayi", "anak",
                        "hewannya", "ayamnya", "with", "dengan", "as", "jadi", "sebagai"})


@dataclass(frozen=True)
class TextEventStep:
    kind: str                      # act | wait | model | stop | done
    reason: str
    args: dict | None = None       # exact ui_press args (act)


def _g3(view, section: str) -> dict | None:
    g3 = getattr(view, "g3", None) if view is not None else None
    value = g3.get(section) if isinstance(g3, Mapping) else None
    return value if isinstance(value, Mapping) else None


def note_observation(objective: TextEventObjective, view, facts: Mapping | None) -> bool:
    """Fresh ui.context / ui_pad view -> progress that needs no input (seen / cleared). True when it changed."""
    changed = False
    facts = facts or {}
    if objective.kind == OBJ_NAME and view is not None and view.context == "naming" and not objective.naming_seen:
        objective.naming_seen = changed = True
    if objective.kind == OBJ_EVENT:
        event = _g3(view, "event") or (facts.get("ui_event") if isinstance(facts.get("ui_event"), Mapping) else None)
        dialogue = event.get("dialogue") if isinstance(event, Mapping) else None
        if isinstance(dialogue, Mapping) and dialogue.get("kind") in ("question", "npc_question") and not objective.question_seen:
            objective.question_seen = changed = True
            objective.question_key = dialogue.get("question_key")
            objective.question_class = dialogue.get("question_class")
    if objective.kind == OBJ_READY:
        ready = facts.get("ready_check") if isinstance(facts.get("ready_check"), Mapping) else _g3(view, "ready_check")
        if isinstance(ready, Mapping):
            snapshot = (ready.get("kind"), ready.get("ready_count"), ready.get("needed"), ready.get("policy"))
            if not objective.ready_seen or snapshot != (objective.ready_kind, objective.ready_count, objective.ready_needed,
                                                         objective.ready_policy):
                objective.ready_seen = changed = True
                objective.ready_kind, objective.ready_count, objective.ready_needed, objective.ready_policy = snapshot
        elif objective.ready_seen and not objective.ready_cleared:
            interactive = facts.get("interactive") if isinstance(facts.get("interactive"), Mapping) else None
            if interactive is None or interactive.get("menu_type") != "ReadyCheckDialog":
                objective.ready_cleared = changed = True
    return changed


def plan_step(objective: TextEventObjective, view, facts: Mapping | None) -> TextEventStep:
    """One deterministic step. Never answers outside the owner's objective; never types a name the owner did not give."""
    if objective.blocked is not None:
        return TextEventStep("stop", objective.blocked)
    if objective.steps >= MAX_STEPS:
        return TextEventStep("stop", "step_budget")
    facts = facts or {}
    if objective.kind == OBJ_READY:
        if objective.ready_cleared:
            return TextEventStep("done", FINISH_READY)
        # D4: the dialog readies ARTI itself every tick; ARTI never confirms another's check nor cancels (D13).
        return TextEventStep("wait", "ready_check_waiting_for_owner" if objective.ready_seen else "ready_check_not_seen")
    if objective.kind == OBJ_NAME:
        if objective.named:
            return TextEventStep("done", FINISH_NAMED)
        if view is None or view.context != "naming":
            return TextEventStep("wait", "naming_menu_not_open")
        if not view.available or not view.press_allowed("A"):
            return TextEventStep("wait", "naming_not_ready:" + (view.wait_reason or "unavailable"))
        entry = _g3(view, "text_entry") or {}
        why = command.name_refusal(objective.name, min_length=int(entry.get("min_length") or 1),
                                   text_limit=int(entry.get("max_length") or -1))
        if why is not None:
            return TextEventStep("stop", "name_policy:" + why)
        return TextEventStep("act", "enter_owner_name", view.press_args("A", text=objective.name))
    # OBJ_EVENT
    if objective.answered:
        return TextEventStep("done", FINISH_ANSWERED)
    if view is None or view.context != "event":
        return TextEventStep("wait", "event_cutscene" if isinstance(facts.get("ui_event"), Mapping) else "no_event")
    event = _g3(view, "event") or {}
    dialogue = event.get("dialogue") if isinstance(event.get("dialogue"), Mapping) else None
    if dialogue is None:
        return TextEventStep("wait", "event_menu_not_dialogue")
    if not view.available or dialogue.get("ready") is not True:
        return TextEventStep("wait", "event_not_ready:" + str(dialogue.get("wait_reason") or view.wait_reason or "unavailable"))
    if dialogue.get("kind") == "dialogue":
        if objective.advances >= MAX_EVENT_ADVANCES:
            return TextEventStep("stop", "event_advance_budget")
        if not view.press_allowed("A"):
            return TextEventStep("wait", "event_advance_unavailable")
        return TextEventStep("act", "event_advance", view.press_args("A"))
    if dialogue.get("answerable") is not True or not view.press_allowed("A"):
        # D7: a refused key (permanent / money / minigame / unreviewed) is never answered by ARTI.
        return TextEventStep("stop", "event_question_refused:" + str(dialogue.get("question_class") or "unknown"))
    choices = dialogue.get("choices") or []
    if objective.choice is None:
        return TextEventStep("model", "event_question_for_model")
    if not 0 <= objective.choice < len(choices):
        return TextEventStep("stop", "owner_choice_out_of_range")
    return TextEventStep("act", "answer_owner_choice", view.press_args("A", choice=str(objective.choice)))


def note_result(objective: TextEventObjective, *, context: str | None, args: Mapping, verified: bool, effect: str | None,
                readback: str | None, uncertain: bool) -> bool:
    """A ui_press result on a G3 context (runtime or model). Only VERIFIED effects count. True when progress changed."""
    objective.steps += 1
    if uncertain:
        objective.blocked = "ui_press_uncertain"
        return True
    if not verified:
        return False
    if objective.kind == OBJ_NAME and context == "naming" and effect == "named":
        objective.named = True
        objective.readback = readback
        return True
    if objective.kind == OBJ_EVENT and context == "event":
        if effect == "answered":
            objective.answered = True
            objective.readback = readback
            return True
        if "choice" not in args and effect in ("advanced", "closed", "replaced"):
            objective.advances += 1
            return True
    return False


def objective_verdict(objective: TextEventObjective) -> tuple[str, str]:
    """(status, reason) from verified progress only (text_rule)."""
    if objective.kind == OBJ_NAME:
        if objective.named and objective.readback == objective.name:
            return "achieved", FINISH_NAMED
        if objective.named:
            return "uncertain", "name_readback_mismatch"
        return "partial", objective.blocked or ("name_not_entered" if objective.naming_seen else "naming_menu_not_seen")
    if objective.kind == OBJ_EVENT:
        if objective.answered:
            if objective.choice is not None and not (objective.readback or "").startswith(f"{objective.choice}:"):
                return "uncertain", "answer_readback_mismatch"
            return "achieved", FINISH_ANSWERED
        return "partial", objective.blocked or ("event_question_not_answered" if objective.question_seen
                                                else "event_question_not_seen")
    if objective.ready_seen and objective.ready_cleared:
        return "achieved", FINISH_READY
    if objective.ready_seen:
        return "partial", f"ready_check_waiting_for_owner:{objective.ready_count}/{objective.ready_needed}"
    return "partial", "ready_check_not_seen"


def objective_summary(objective: TextEventObjective | None) -> dict | None:
    if objective is None:
        return None
    status, reason = objective_verdict(objective)
    summary = {"objective": "g3_" + objective.kind, "status": status, "reason": reason}
    if objective.kind == OBJ_NAME:
        summary["name"] = objective.name
    if objective.kind == OBJ_EVENT:
        summary["owner_choice"] = objective.choice
        summary["advances"] = objective.advances
    if objective.kind == OBJ_READY and objective.ready_seen:
        summary["ready_check"] = {"kind": objective.ready_kind, "ready": objective.ready_count,
                                  "needed": objective.ready_needed, "policy": objective.ready_policy}
    return summary


def model_offer_allowed(objective: TextEventObjective | None, view) -> bool:
    """PLAN 5.3 rule 8 for the G3 contexts: naming / number / ready_check are never offered to the model (D3, D4);
    ready ordinary event dialogue may advance; questions still need the owner's
    event-question objective without an owner choice, on an answerable question."""
    if view is None or view.context not in G3_CONTEXTS:
        return True
    if view.context != "event":
        return False
    if objective is not None and objective.kind == OBJ_EVENT and objective.choice is not None:
        return False  # the existing owner-chosen-answer routine also owns preceding dialogue
    dialogue = (_g3(view, "event") or {}).get("dialogue")
    if (view.menu_type == "DialogueBox" and isinstance(dialogue, Mapping)
            and dialogue.get("kind") == "dialogue" and dialogue.get("ready") is True
            and view.press_allowed("A")):
        return True  # ordinary non-question advance; no trial or answer authorization needed
    if objective is None or objective.kind != OBJ_EVENT or objective.choice is not None:
        return False
    return isinstance(dialogue, Mapping) and dialogue.get("kind") in ("question", "npc_question") and dialogue.get("answerable") is True


def model_args_reason(view, args: Mapping) -> str | None:
    """Extra check for a model ui_press on the event question: an in-range choice (the mod re-checks)."""
    if view is None or view.context != "event":
        return None
    if set(args) & {"text", "value", "festival", "slot", "item"}:
        return "ui_press_payload_not_offered"
    choice = args.get("choice")
    if choice is None:
        return None
    dialogue = (_g3(view, "event") or {}).get("dialogue") or {}
    if dialogue.get("kind") == "dialogue":
        return "ui_press_payload_not_offered"
    choices = dialogue.get("choices") or []
    if not isinstance(choice, str) or not choice.isdigit() or int(choice) >= len(choices):
        return "ui_press_choice_out_of_range"
    return None


EVENT_SKIP_DETAIL = "skipEvent"  # mod PadContextTable.EventSkipDetail (pad_button Select in a plain cutscene)
EVENT_SKIP_ARGS = {"button": "Select"}


def event_skip_step(facts: Mapping | None, objective: TextEventObjective | None, *, enabled: bool) -> tuple[str, str]:
    """FIXB31 (owner decision 2026-10-12, opt-in): ("skip", reason) when ARTI may skip the current cutscene now, else
    ("none", why). Needs the runtime flag AND the mod's own EventSkipEnabled cell (the mod's ui.context ``pad`` row offers
    Select = pad_button ``skipEvent`` only on a plain skippable cutscene: no question / festival / wedding / player
    control / other menu / minigame). Never while the owner's event-question objective is open (the owner wants it
    answered), never on a published event question (double check of the mod's policy)."""
    if not enabled:
        return "none", "event_skip_disabled"
    facts = facts or {}
    pad = facts.get("pad") if isinstance(facts.get("pad"), Mapping) else None
    if pad is None or pad.get("enabled") is not True or pad.get("context") != "Event":
        return "none", "no_event"
    cell = (pad.get("buttons") or {}).get("Select") if isinstance(pad.get("buttons"), Mapping) else None
    if not isinstance(cell, Mapping) or cell.get("family") != "pad_button" or cell.get("detail") != EVENT_SKIP_DETAIL:
        refused = cell.get("refused") if isinstance(cell, Mapping) else None
        return "none", "mod_refused:" + str(refused or "no_cell")
    if objective is not None and objective.kind == OBJ_EVENT and not objective.answered:
        return "none", "owner_event_question_objective"
    event = facts.get("ui_event") if isinstance(facts.get("ui_event"), Mapping) else None
    dialogue = event.get("dialogue") if isinstance(event, Mapping) else None
    if isinstance(dialogue, Mapping) and dialogue.get("kind") in ("question", "npc_question"):
        return "none", "question_pending"
    if isinstance(event, Mapping) and event.get("festival") is True:
        return "none", "festival"
    return "skip", "plain_cutscene"


def event_skip_readback(facts: Mapping | None) -> bool:
    """FIXB31: a strictly newer ui.context shows the skip took: the event row is gone, or Select no longer skips (the
    mod refuses ``already_skipped`` while the event's end behaviours run)."""
    pad = (facts or {}).get("pad")
    if not isinstance(pad, Mapping) or pad.get("context") != "Event":
        return True
    cell = (pad.get("buttons") or {}).get("Select") if isinstance(pad.get("buttons"), Mapping) else None
    return not (isinstance(cell, Mapping) and cell.get("detail") == EVENT_SKIP_DETAIL)


def ready_check_wait_reason(facts: Mapping | None) -> str | None:
    """Day-start / bed wait detail from the mod's ready_check block (D4 policy + counts), or None."""
    ready = (facts or {}).get("ready_check")
    if not isinstance(ready, Mapping):
        return None
    return (f"ready_check:{ready.get('kind')}:{ready.get('policy')}:"
            f"{ready.get('ready_count')}/{ready.get('needed')}")
