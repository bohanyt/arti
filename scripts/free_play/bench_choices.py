"""Trial-only deterministic replies. Every input still goes through Live7."""
from __future__ import annotations

import json
from decimal import Decimal

from arti_stardew_runtime import DecisionProviderPolicy, ProductionDecisionProvider
from scripts.free_play.expectations import matches

SETTING = "rig_bench_trial"
PAGE_FIELDS = ("facts", "day", "skills", "unknown_ui", "game_facts")


def choose(page, step, snapshot=None):
    targets = {}
    if "select" in step and any(key.startswith("event_target.") for key in step["select"]):
        from arti_runtime.festival_facts import event_choices
        offers = getattr(snapshot, "event_actions", None) or {}
        # ADR 240: a choice the map shows before its route carries its kind and tile; routed ones the native target.
        targets = {arg: offers["targets"][row["handle"]] if row["handle"] else dict(
                       kind=row["kind"], tile_x=(row["tile"] or (None, None))[0], tile_y=(row["tile"] or (None, None))[1])
                   for arg, row in event_choices(snapshot).items()}
    candidates = []
    for entry in page.get("skills", []):
        if entry.get("name") != step["skill"]:
            continue
        for arg in entry.get("args") or [None]:
            if "select" in step:
                label = (entry.get("nama") or {}).get(arg)
                if not matches(dict(entry, arg=arg, label=label, event_target=targets.get(arg)), step["select"]):
                    continue
            elif arg != step["arg"]:
                continue
            if arg not in candidates:
                candidates.append(arg)
    if len(candidates) == 1:
        return step["skill"], candidates[0]
    return None


def reply(request, pick):
    choice = dict(kind="act", operation="skill", target_ref=None,
                  args=dict(name=pick[0], arg=pick[1])) if pick else dict(kind="finish", scope="activity")
    return json.dumps(dict(v=1, request_nonce=request.request_nonce, goal_id=request.goal_id,
                           goal_revision=request.goal_revision, context_ref=request.context_ref,
                           speech="Bench scripted choice.", choice=choice))


def no_transport(*args, **kwargs):
    raise RuntimeError("buttons mode has no model transport")


class NoModelProvider(ProductionDecisionProvider):
    """Use the normal bounded renderer with a policy that cannot issue a request."""
    def __init__(self):
        super().__init__(DecisionProviderPolicy("bench", "bench", "", "bench", "", True,
                       Decimal(0), Decimal(0)), session=None, text_chat_fn=no_transport,
                       logical_timeout_sec=5, continuation_enabled=True)
        self.transport_provenance = "bench_buttons"

    def complete_reflection(self, *args, **kwargs):
        return "{}"


class TrialChoices:
    def __init__(self, spec):
        self.mode = spec["mode"]
        self.steps = [("setup", step) for step in spec["setup"]]
        self.steps.extend(("buttons", step) for step in spec["buttons"])
        self.index, self.pending = 0, None
        self.page_count = 0
        self.last_page = None

    def attach(self, runtime, provider):
        original_complete, original_journal = provider.complete, runtime._journal

        def journal(row):
            original_journal(row)
            if row.get("ev") == "skill_model_decision" and self.last_page:
                context = self.last_page
                if isinstance(row.get("choice"), list):
                    original_journal(dict(ev="bench_decision", **context, choice=row["choice"]))
                self.last_page = None
            if row.get("ev") == "skill_model_decision" and self.pending is not None:
                generation, pick, expected = self.pending
                if row.get("choice") == list(expected):
                    self.index += 1
                    original_journal(dict(ev="bench_choice_accepted", generation=generation,
                                          index=self.index, skill=pick[0], arg=pick[1]))
                self.pending = None

        def complete(request, *, start_fence=None):
            self.pending = None
            if request.skill_offer is None:
                if self.mode == "buttons" or self.index < len(self.steps):
                    raise ValueError("bench requires a skill offer for scripted choices")
                return original_complete(request, start_fence=start_fence)
            page = json.loads(provider._render_messages(request)[1]["content"])
            phase, step = self.steps[self.index] if self.index < len(self.steps) else (self.mode, None)
            menu = (getattr(request, "ui_offer_pin", None) or {}).get("pins", {}).get("menu_type")
            gift = request.skill_offer.get("gift")
            if gift:
                menu = "DialogueBox"

            def record():
                self.page_count += 1
                self.last_page = dict(phase=phase, generation=request.generation_id, index=self.page_count,
                                      menu=menu, host_question=bool(gift))
                facts = {k: page[k] for k in PAGE_FIELDS if k in page}
                if said := (page.get("mind") or {}).get("dikatakan"):
                    facts["said"] = said
                original_journal(dict(ev="bench_page", **self.last_page, page=facts,
                                      game=runtime._journal_game_stamp()))

            def fence(start):
                def send():
                    record()
                    start()
                start_fence(send)
            if self.mode == "play" and step is None:
                return original_complete(request, start_fence=fence)
            pick = (choose(page, step, getattr(request, "snapshot", None)) if step and matches(dict(page=page, menu=menu), step.get("when", {}))
                    else None)
            if pick is not None:
                aliases = getattr(request, "item_choice_aliases", {}).get(pick[0], {})
                self.pending = (request.generation_id, pick, (pick[0], aliases.get(pick[1], pick[1])))
            else:
                pick = choose(page, {"skill": "ui_action", "arg": "wait"})
                if pick is None:
                    reads = [a for e in page.get("skills", []) if e.get("name") == "read_boundary"
                             for a in e.get("args", [])]
                    if reads:
                        pick = ("read_boundary", reads[0])
            fence(lambda: None)
            return reply(request, pick)

        runtime._journal = journal
        provider.complete = complete
