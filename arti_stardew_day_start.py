"""WS2 deterministic day-start rule (pure planner). Zero I/O, zero provider calls, zero input.

The runtime feeds the freshest ui.context facts plus the parsed ``UiKeyView`` and gets back ONE
step: ``act`` (exact ui_key args the mod published), ``wait``, ``done``, ``blocked`` or ``idle``.
The runtime executes ``act`` through its leased ui_key path (origin ``day_start``) and reports the
verdict back with :func:`note_result`. Every step is bounded (actions, wall clock, no-effect and
uncertain counts) so a stuck end-of-night screen ends as ``blocked`` instead of a silent hold.

Evidence ceiling: UNIT_TESTED (the end-of-night observability across save/host sync is a HYPOTHESIS).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from arti_stardew_reading_dwell import ReadingDwell, page_text
from arti_stardew_ui import UiKeyView

# WP P1 (R0-06 B.2 R1, owner O4): ARTI picks professions herself, so the program has no pick. Keys would be
# "<skill>:5" / "<skill>:10:<level5 id>", resolved against the OBSERVED option ids; empty = the chooser is never touched.
DEFAULT_PROFESSION_POLICY: dict[str, int] = {}

DAY_START_HOST_WAIT_S = 1800.0  # finite 30-minute multiplayer/save synchronization grace

DAY_START_MENUS = ("LevelUpMenu", "ShippingMenu")
# WP P1 (R0-06 B.2 R1): N1's ProgressionClient is the only actor on these whitelist surfaces
# (contracts/progression_whitelist.json entries c, d); the legacy rule waits on them and never confirms.
# ADR-19 item 3 (accepted): entries a/b (dialogue, letter) keep the owner-approved info-only auto-advance.
PROGRESSION_SURFACES = {"ShippingMenu": "shipping_summary", "LevelUpMenu": "level_up"}
WAIT_MENUS = ("SaveGameMenu", "ReadyCheckDialog")


@dataclass(frozen=True)
class DayStartPolicy:
    enabled: bool = False
    professions: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_PROFESSION_POLICY))
    max_actions: int = 8
    wall_s: float = 240.0
    host_wait_s: float = DAY_START_HOST_WAIT_S
    max_no_effect: int = 2
    max_uncertain: int = 1
    dialogue_window_s: float = 60.0
    # Viewer reading time before a confirm (smoke 2026-10-23 item 8). A planner ``wait``: no lease, no key.
    dwell: ReadingDwell = field(default_factory=ReadingDwell.default)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "DayStartPolicy":
        cfg = cfg or {}
        if cfg.get("stardew_profession_policy"):
            raise ValueError("stardew_profession_policy is neutralised (owner O4: ARTI picks professions)")
        return cls(enabled=cfg.get("stardew_day_start_handler_enabled") is True, dwell=ReadingDwell.from_config(cfg))

    def profession_for(self, skill: int, level: int, options: tuple[int, ...]) -> int | None:
        """The owner-approved id for this (skill, level), only if it is one of the two observed options."""
        if len(options) != 2 or skill not in range(5):
            return None
        if level == 5:
            chosen = self.professions.get(f"{skill}:5")
        elif level == 10:
            # The game offers skill*6+{2,3} iff the level-5 pick was skill*6, else skill*6+{4,5}.
            first = skill * 6 if set(options) == {skill * 6 + 2, skill * 6 + 3} else (
                skill * 6 + 1 if set(options) == {skill * 6 + 4, skill * 6 + 5} else None)
            chosen = self.professions.get(f"{skill}:10:{first}") if first is not None else None
        else:
            chosen = None
        return chosen if chosen in options else None


@dataclass
class DayStartState:
    """Mutable episode state owned by the runtime (one episode = date + first receiver seen)."""
    episode_key: tuple | None = None
    started_at: float | None = None
    actions: int = 0
    no_effect: dict[str, int] = field(default_factory=dict)
    uncertain: int = 0
    awaiting_newer_than: int | None = None
    date_changed_at: float | None = None
    blocked: str | None = None
    finished: str | None = None
    menus: list[str] = field(default_factory=list)
    last_wait: tuple[str, float] | None = None
    identity: tuple | None = None
    host_sync_pending: bool = False
    host_wait_deadline: float | None = None  # real monotonic safety bound, never pause-adjusted
    dwell_seen: dict = field(default_factory=dict)  # (receiver, kind, keys) -> first act-ready time

    @property
    def active(self) -> bool:
        return self.started_at is not None and self.finished is None and self.blocked is None

    def wall_limit(self, policy):
        return policy.wall_s + policy.host_wait_s if self.host_sync_pending else policy.wall_s

    def reset(self) -> None:
        date_changed_at = self.date_changed_at
        self.__init__()  # type: ignore[misc]
        self.date_changed_at = date_changed_at


@dataclass(frozen=True)
class DayStartStep:
    kind: str  # act | wait | done | blocked | idle
    reason: str
    ui_key_args: dict | None = None
    menu_type: str | None = None
    ui_kind: str | None = None
    profession: dict | None = None


def _menu_up(facts: Mapping | None) -> bool:
    return bool(facts) and (facts.get("mode") == "menu" or isinstance(facts.get("interactive"), Mapping))


def native_morning_pending(facts: Mapping | None, date: tuple | None = None) -> bool:
    """The date changes before native farm events, summaries and save sync finish.

    Additive wire contract: older observations lack morning_ready; retain their existing
    verifier, while every explicit native pending fact fences early completion.
    """
    day = facts.get("day_transition") if isinstance(facts, Mapping) else None
    if not isinstance(day, Mapping):
        return False
    if date is not None and "date" in day and day.get("date") != list(date):
        return True  # a young UI sample from yesterday cannot certify today's morning
    return (day.get("morning_ready") is False or day.get("new_day_sync_pending") is True
            or bool(day.get("farm_event_type")) or day.get("event_up") is True
            or day.get("showing_end_of_night_stuff") is True
            or isinstance(day.get("end_of_night_menus_pending"), int)
            and day["end_of_night_menus_pending"] > 0)


def native_event_state(facts: Mapping | None) -> dict | None:
    """Small native lifecycle fact, independent of story ID or dialogue text (ADR-60)."""
    day = facts.get("day_transition") if isinstance(facts, Mapping) else None
    if not isinstance(day, Mapping) or not (day.get("event_up") is True or day.get("farm_event_type")):
        return None
    return {k: day[k] for k in ("date", "farm_event_type", "event_up", "event_player_control", "event_id", "event_command")
            if k in day}


def plan_day_start_step(facts: Mapping | None, view: UiKeyView | None, policy: DayStartPolicy,
                        state: DayStartState, *, now: float, context_revision: int | None = None,
                        date: tuple | None = None, agent_acted: bool = False) -> DayStartStep:
    """One deterministic step. Never answers a question; never touches a menu without a published key.

    ``agent_acted`` (PART_C2): ARTI already published its own (non-day-start) input since the date change. A
    wake-up dialogue/letter is up before ARTI acts; one that appears after ARTI's own input was opened by that
    input (C2: the S29 TV forecast page, 15 s after waking) and belongs to the skill, never to this rule.
    """
    if not policy.enabled:
        return DayStartStep("idle", "disabled")
    if state.blocked is not None:
        return DayStartStep("blocked", state.blocked)
    if state.active and state.started_at is not None and now - state.started_at > state.wall_limit(policy):
        state.blocked = "wall_bound"
        return DayStartStep("blocked", state.blocked)
    if not facts:
        return DayStartStep("wait", "waiting_for_host_or_players" if state.host_sync_pending
                            else "ui_context_unavailable") if state.active else DayStartStep("idle", "no_context")
    day = facts.get("day_transition") if isinstance(facts.get("day_transition"), Mapping) else {}
    showing = day.get("showing_end_of_night_stuff") is True
    in_window = (state.date_changed_at is not None and 0 <= now - state.date_changed_at <= policy.dialogue_window_s
                 and not agent_acted)
    menu_type = view.menu_type if view is not None else (
        (facts.get("interactive") or {}).get("menu_type") if isinstance(facts.get("interactive"), Mapping) else None)
    relevant = (menu_type in DAY_START_MENUS
                or (showing and _menu_up(facts))
                or (view is not None and view.kind in ("dialogue", "letter") and in_window))
    if state.finished is not None:
        # A finished episode never re-arms on the same screens: only a frame with no menu ends it.
        if _menu_up(facts):
            return DayStartStep("done", state.finished, menu_type=menu_type)
        state.reset()
    if not state.active:
        if not relevant:
            return DayStartStep("idle", "no_day_start_menu")
        state.episode_key = (date, view.receiver_token if view is not None else menu_type)
        state.started_at = now
    assert state.started_at is not None
    if menu_type is not None and (not state.menus or state.menus[-1] != menu_type):
        state.menus.append(menu_type)
    if now - state.started_at > state.wall_limit(policy):
        state.blocked = "wall_bound"
        return DayStartStep("blocked", "wall_bound", menu_type=menu_type)
    if state.uncertain >= policy.max_uncertain:
        state.blocked = "ui_key_uncertain"
        return DayStartStep("blocked", "ui_key_uncertain", menu_type=menu_type)
    if not _menu_up(facts):
        if facts.get("is_player_free") is True and not showing:
            state.finished = "cleared"
            return DayStartStep("done", "cleared")
        return DayStartStep("wait", "player_not_free" if not showing else "end_of_night_pending")
    if state.awaiting_newer_than is not None and (context_revision is None or context_revision <= state.awaiting_newer_than):
        return DayStartStep("wait", "awaiting_newer_context", menu_type=menu_type)
    if view is None:
        return DayStartStep("wait", "ui_view_unavailable", menu_type=menu_type)
    if view.wait_only or view.menu_type in WAIT_MENUS:
        # Primitives v2 WS-G3 (owner D4): a ReadyCheck is wait-only here too; the mod's ready_check block names the
        # kind, the policy and the counts (the HOST side needs the owner, D13: never cancelled to unblock).
        ready = facts.get("ready_check") if view.menu_type == "ReadyCheckDialog" else None
        if isinstance(ready, Mapping):
            return DayStartStep("wait", f"ready_check:{ready.get('kind')}:{ready.get('policy')}:"
                                        f"{ready.get('ready_count')}/{ready.get('needed')}", menu_type=view.menu_type)
        return DayStartStep("wait", "wait_only:" + view.menu_type, menu_type=view.menu_type)
    if view.kind in ("question", "npc_question"):
        # Questions are never auto-answered by the handler; the model may answer after the day starts.
        state.finished = "question_left_to_model"
        return DayStartStep("done", "question_left_to_model", menu_type=view.menu_type, ui_kind=view.kind)
    if view.kind is None:
        return DayStartStep("wait", "unsupported_menu:" + view.menu_type, menu_type=view.menu_type)
    if PROGRESSION_SURFACES.get(view.menu_type) == view.kind:
        if view.kind == "shipping_summary" and view.not_ready_reason in ("saving", "outro"):
            # ADR-19 item 4 (accepted): the native OK is observed (save running), so the host-sync grace starts here.
            state.host_sync_pending = True
        return DayStartStep("wait", "progression_client:" + view.kind, menu_type=view.menu_type, ui_kind=view.kind)
    if view.kind in ("dialogue", "letter") and not (showing or in_window):
        state.finished = "menu_not_day_start"
        return DayStartStep("done", "menu_not_day_start", menu_type=view.menu_type, ui_kind=view.kind)
    if not view.ready or not view.available:
        return DayStartStep("wait", view.not_ready_reason or "not_ready", menu_type=view.menu_type, ui_kind=view.kind)
    if state.no_effect.get(view.receiver_token, 0) >= policy.max_no_effect:
        state.blocked = "ui_key_no_effect"
        return DayStartStep("blocked", "ui_key_no_effect", menu_type=view.menu_type, ui_kind=view.kind)
    if state.actions >= policy.max_actions:
        state.blocked = "action_bound"
        return DayStartStep("blocked", "action_bound", menu_type=view.menu_type, ui_kind=view.kind)
    # Reading dwell: the page (receiver + its published key tokens) stays up for the viewer before the confirm.
    # Counted from the first act-ready tick of THIS page; the wall bound above still holds.
    page = (view.receiver_token, view.kind, view.keys)
    need = policy.dwell.seconds(page_text(facts), kind=view.kind)
    if need > 0 and now - state.dwell_seen.setdefault(page, now) < need:
        return DayStartStep("wait", "reading_dwell", menu_type=view.menu_type, ui_kind=view.kind)
    key = {"dialogue": "advance", "letter": "close", "level_up": "confirm",
           "shipping_summary": "confirm", "level_up_profession": "choose"}[view.kind]
    if key == "choose":
        options = tuple(p["profession_id"] for p in view.professions)
        chosen = policy.profession_for(view.skill if view.skill is not None else -1,
                                       view.level if view.level is not None else -1, options)
        profession = {"skill": view.skill, "level": view.level, "options": list(options), "chosen": chosen}
        if chosen is None:
            state.blocked = "profession_policy_missing"
            return DayStartStep("blocked", "profession_policy_missing", menu_type=view.menu_type,
                                ui_kind=view.kind, profession=profession)
        args = view.args_for("choose", options.index(chosen), chosen)
        return DayStartStep("act", "profession_policy", args, view.menu_type, view.kind, profession)
    return DayStartStep("act", "day_start_" + view.kind, view.args_for(key), view.menu_type, view.kind)


def note_result(state: DayStartState, step: DayStartStep, *, outcome: str | None, uncertain: bool,
                context_revision: int | None, max_uncertain: int = 1, submitted: bool = True,
                verified: bool = False, effect: str | None = None) -> None:
    """Account one attempted act (every attempt counts toward max_actions).

    Spacing: after an applied step the next one needs a ui.context strictly newer than the one this step
    was planned on. A refusal before any write (not submitted) is neither a no-effect nor a reason to wait.
    """
    if step.kind != "act" or step.ui_key_args is None:
        return
    state.actions += 1
    receiver = step.ui_key_args["receiver_token"]
    if uncertain:
        state.uncertain += 1
        if state.uncertain >= max_uncertain:
            state.blocked = "ui_key_uncertain"  # no retry; the runtime may already be PAUSED_RECOVERY
    elif outcome == "UiKeyApplied":
        state.awaiting_newer_than = context_revision
        if (step.menu_type == "ShippingMenu" and step.ui_kind == "shipping_summary"
                and verified and effect == "shipping_confirmed"):
            state.host_sync_pending = True
    elif submitted:
        state.no_effect[receiver] = state.no_effect.get(receiver, 0) + 1
