"""Semantic UI read model Stardew. Murni: nol input, nol LLM, nol network."""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from collections.abc import Callable, Mapping
from typing import Protocol

from arti_stardew import StardewState, UiKomponen

ARAH_UI = ("atas", "kanan", "bawah", "kiri")
ARAH_NEIGHBOR = {
    "atas": "up",
    "kanan": "right",
    "bawah": "down",
    "kiri": "left",
}


class HasilGerak(Protocol):
    berhasil: bool
    terverifikasi: bool
    kode: str
    alasan: str


@dataclass(frozen=True)
class UiNavigationResult:
    ok: bool
    verified: bool = True
    code: str = ""
    message: str = ""
    transitions: int = 0


@dataclass(frozen=True)
class UiElement:
    key: str
    role: str
    text: str | None
    count: int | None
    selected: bool


@dataclass(frozen=True)
class UiSnapshot:
    surface: str | None
    page: str | None
    revision: int | None
    focus: str | None
    visible_text: str | None
    speaker: str | None
    elements: tuple[UiElement, ...]

    def cari(self, key: str) -> tuple[UiElement, ...]:
        """Tuple sengaja: semantic key ambigu harus terlihat, bukan ditimpa."""
        return tuple(element for element in self.elements if element.key == key)


class UiAdapter:
    def cocok(self, state: StardewState) -> bool:
        return True

    def components(self, state: StardewState) -> tuple[UiKomponen, ...]:
        return state.menu_components

    def role(self, component: UiKomponen) -> str:
        return component.role


class DialogueAdapter(UiAdapter):
    def cocok(self, state: StardewState) -> bool:
        return state.menu_type == "DialogueBox"

    def components(self, state: StardewState) -> tuple[UiKomponen, ...]:
        return tuple(c for c in state.menu_components if c.role == "choice")


class GameMenuAdapter(UiAdapter):
    def cocok(self, state: StardewState) -> bool:
        return state.menu_type == "GameMenu"

    def role(self, component: UiKomponen) -> str:
        if component.role == "inventory_slot" and component.text:
            return "inventory_item"
        return component.role


class ReadyCheckAdapter(UiAdapter):
    def cocok(self, state: StardewState) -> bool:
        return state.menu_type == "ReadyCheckDialog"

    def components(self, state: StardewState) -> tuple[UiKomponen, ...]:
        return tuple(c for c in state.menu_components if c.role == "choice")


_ADAPTERS = (DialogueAdapter(), GameMenuAdapter(), ReadyCheckAdapter())


def bangun_ui_snapshot(state: StardewState) -> UiSnapshot:
    adapter = next((a for a in _ADAPTERS if a.cocok(state)), UiAdapter())
    elements = tuple(
        UiElement(
            key=component.key,
            role=adapter.role(component),
            text=component.text or component.label or component.name,
            count=component.count,
            selected=component.selected,
        )
        for component in adapter.components(state)
    )
    return UiSnapshot(
        surface=state.menu_type or state.active_menu,
        page=state.menu_page_type,
        revision=state.ui_revision,
        focus=state.menu_focus_key,
        visible_text=state.menu_visible_text,
        speaker=state.menu_speaker,
        elements=elements,
    )


class UiNavigator:
    """Planner deterministik: hint untuk rute, state live sebagai kebenaran."""

    def __init__(self) -> None:
        self._graph: dict[tuple, dict[tuple[int, str], int | None]] = {}
        self._observed: dict[tuple, set[tuple[int, str]]] = {}

    @staticmethod
    def signature(state: StardewState) -> tuple:
        return (
            state.menu_type or state.active_menu,
            state.menu_page_type,
            tuple((c.key, c.native_id) for c in state.menu_components),
        )

    @staticmethod
    def resolve_target(state: StardewState, target: str) -> tuple[UiKomponen, ...]:
        return tuple(c for c in state.menu_components if c.key == target)

    def focus(
        self,
        state: StardewState,
        target: str,
        *,
        latest: Callable[[], StardewState | None],
        move: Callable[[str, int | None], HasilGerak],
        max_transitions: int = 32,
    ) -> UiNavigationResult:
        signature = self.signature(state)
        self._forget_blocked(signature)
        transitions = 0

        while transitions < max(1, max_transitions):
            current = latest()
            if current is None:
                return UiNavigationResult(
                    False, False, "ACTION_UNVERIFIED", "state UI hilang", transitions
                )
            if self.signature(current) != signature:
                return UiNavigationResult(
                    False, True, "UI_CHANGED", "surface/page UI berubah", transitions
                )

            targets = self.resolve_target(current, target)
            if not targets:
                return UiNavigationResult(
                    False, True, "TARGET_NOT_FOUND",
                    f"target tidak ada: {target}", transitions,
                )
            if len(targets) != 1:
                return UiNavigationResult(
                    False, True, "TARGET_AMBIGUOUS",
                    f"target ambigu: {target}", transitions,
                )
            target_component = targets[0]
            same_native = [
                c for c in current.menu_components
                if c.native_id == target_component.native_id
            ]
            if len(same_native) != 1:
                return UiNavigationResult(
                    False, True, "TARGET_AMBIGUOUS",
                    f"native target ambigu: {target}", transitions,
                )
            if current.menu_snap_id is None:
                return UiNavigationResult(
                    False, True, "NO_FOCUS", "kursor UI tidak terlihat", transitions
                )
            if current.menu_focus_key == target:
                if current.menu_snap_id != target_component.native_id:
                    return UiNavigationResult(
                        False, True, "UI_CHANGED",
                        "focus semantic/native tidak konsisten", transitions,
                    )
                return UiNavigationResult(True, True, transitions=transitions)

            self._seed_hints(signature, current)
            route = self._route(signature, current.menu_snap_id, target_component.native_id)
            direction = route[0] if route else self._exploration_step(
                signature, current.menu_snap_id
            )
            if direction is None:
                return UiNavigationResult(
                    False, True, "TARGET_NOT_REACHABLE",
                    f"target tidak terjangkau: {target}", transitions,
                )

            result = move(direction, current.ui_revision)
            after = latest()
            transitions += 1
            if after is None:
                return UiNavigationResult(
                    False, False, "ACTION_UNVERIFIED",
                    "state sesudah navigasi hilang", transitions,
                )
            if self.signature(after) != signature:
                return UiNavigationResult(
                    False, True, "UI_CHANGED", "surface/page UI berubah", transitions
                )
            if result.berhasil:
                if after.menu_snap_id is None:
                    return UiNavigationResult(
                        False, True, "NO_FOCUS",
                        "focus hilang sesudah navigasi", transitions,
                    )
                self._observe(signature, current.menu_snap_id, direction, after.menu_snap_id)
                if (
                    after.menu_focus_key == target
                    and after.menu_snap_id == target_component.native_id
                ):
                    return UiNavigationResult(True, True, transitions=transitions)
                continue
            if result.kode == "NO_OBSERVABLE_CHANGE":
                self._observe(signature, current.menu_snap_id, direction, None)
                continue
            return UiNavigationResult(
                False, result.terverifikasi, result.kode or "BLOCKED",
                result.alasan, transitions,
            )

        return UiNavigationResult(
            False, True, "TARGET_NOT_REACHABLE",
            f"batas {max_transitions} transisi tercapai", transitions,
        )

    def _seed_hints(self, signature: tuple, state: StardewState) -> None:
        graph = self._graph.setdefault(signature, {})
        known_ids = {component.native_id for component in state.menu_components}
        for component in state.menu_components:
            for direction in ARAH_UI:
                edge = (component.native_id, direction)
                if edge in self._observed.setdefault(signature, set()):
                    continue
                neighbor = component.neighbors.get(ARAH_NEIGHBOR[direction])
                if neighbor in known_ids:
                    graph[edge] = neighbor

    def _observe(
        self,
        signature: tuple,
        source: int,
        direction: str,
        destination: int | None,
    ) -> None:
        self._graph.setdefault(signature, {})[(source, direction)] = destination
        self._observed.setdefault(signature, set()).add((source, direction))

    def _forget_blocked(self, signature: tuple) -> None:
        """Edge mandek hanya berlaku untuk satu command, bukan selamanya.

        `NO_OBSERVABLE_CHANGE` bisa berarti arah memang buntu, tetapi juga bisa
        berarti snapshot baru terlambat melewati timeout motor. Transisi sukses
        aman dipelajari lintas command; hasil nihil wajib boleh dicoba ulang.
        """
        graph = self._graph.setdefault(signature, {})
        observed = self._observed.setdefault(signature, set())
        blocked = {edge for edge in observed if graph.get(edge) is None}
        for edge in blocked:
            observed.discard(edge)
            graph.pop(edge, None)

    def _route(self, signature: tuple, source: int, target: int) -> list[str] | None:
        graph = self._graph.setdefault(signature, {})
        queue = deque([(source, [])])
        seen = {source}
        while queue:
            node, path = queue.popleft()
            if node == target:
                return path
            for direction in ARAH_UI:
                destination = graph.get((node, direction))
                if destination is None or destination in seen:
                    continue
                seen.add(destination)
                queue.append((destination, [*path, direction]))
        return None

    def _exploration_step(self, signature: tuple, current: int) -> str | None:
        observed = self._observed.setdefault(signature, set())
        for direction in ARAH_UI:
            if (current, direction) not in observed:
                return direction

        # Kembali lewat graph yang sudah benar-benar diamati menuju node yang
        # masih punya sisi belum dicoba. Hint tidak dipakai untuk backtracking.
        graph = self._graph.setdefault(signature, {})
        queue = deque([(current, [])])
        seen = {current}
        while queue:
            node, path = queue.popleft()
            if any((node, direction) not in observed for direction in ARAH_UI):
                return path[0] if path else None
            for direction in ARAH_UI:
                edge = (node, direction)
                destination = graph.get(edge)
                if edge not in observed or destination is None or destination in seen:
                    continue
                seen.add(destination)
                queue.append((destination, [*path, direction]))
        return None


# ---------------------------------------------------------------------------
# WS2 ui_key read model (pure, fail-closed). Mirrors the mod's ui.context
# `ui_key` block; never builds a target the mod did not publish.
# ---------------------------------------------------------------------------

UI_KEY_MAX_AGE_S = 2.0


GIFT_QUESTION_KEY = "GiftPlayerItem"  # Sat gift: the mod's synthetic key, published only under the native gift pin


@dataclass(frozen=True)
class UiKeyView:
    receiver_token: str
    menu_type: str
    kind: str | None
    ready: bool
    available: bool
    not_ready_reason: str | None
    wait_only: bool
    keys: tuple[tuple[str, str], ...]
    options: tuple[dict, ...]
    professions: tuple[dict, ...]
    skill: int | None
    level: int | None
    letter: dict | None
    context_generation: int
    showing_end_of_night_stuff: bool
    end_of_night_menus_pending: int
    # WS3b: allowlisted location question key (Sleep / Eat) of a kind "question" receiver, else None.
    question_key: str | None = None
    # Sat gift: the native gift facts while question_key == GiftPlayerItem, else None.
    gift: dict | None = None

    def token_for(self, key: str) -> str | None:
        return next((token for k, token in self.keys if k == key), None)

    def args_for(self, key: str, option_index: int | None = None, profession_id: int | None = None) -> dict:
        """Exact mod args for one published key; raises KesalahanPerintah when not derivable."""
        import arti_stardew_command as command
        token = self.token_for(key)
        if self.kind is None or token is None:
            raise command.KesalahanPerintah("ui_key_key_not_published")
        args = dict(receiver_token=self.receiver_token, menu_type=self.menu_type, kind=self.kind,
                    key=key, target_token=token)
        if option_index is not None:
            args["option_index"] = option_index
        if profession_id is not None:
            args["profession_id"] = profession_id
        command.validate_ui_key_args(args)
        if key == "answer" and option_index >= len(self.options):
            raise command.KesalahanPerintah("ui_key_option_not_published")
        if key == "choose" and not any(p.get("index") == option_index and p.get("profession_id") == profession_id
                                       for p in self.professions):
            raise command.KesalahanPerintah("ui_key_profession_not_published")
        return args


def _bounded_str(value: object, limit: int = 128) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def baca_ui_key(facts: object, *, age_s: float | None, authoritative: bool = True,
                complete: bool = True) -> UiKeyView | None:
    """ui.context facts -> UiKeyView, or None on any doubt (stale, partial, mismatch, bad shapes)."""
    import arti_stardew_command as command
    if (not isinstance(facts, Mapping) or not authoritative or not complete or age_s is None
            or not 0 <= age_s <= UI_KEY_MAX_AGE_S):
        return None
    block, interactive = facts.get("ui_key"), facts.get("interactive")
    caps = facts.get("capabilities")
    if not isinstance(block, Mapping) or not isinstance(interactive, Mapping) or not isinstance(caps, Mapping):
        return None
    capability = caps.get("ui_key")
    generation = facts.get("context_generation")
    receiver, menu_type, kind = block.get("receiver_token"), block.get("menu_type"), block.get("kind")
    if (not _bounded_str(receiver) or not receiver.startswith("rcv:") or not _bounded_str(menu_type)
            or receiver != interactive.get("receiver_token") or menu_type != interactive.get("menu_type")
            or type(generation) is not int or generation < 1 or not isinstance(capability, Mapping)
            or type(capability.get("available")) is not bool):
        return None
    ready, wait_only = block.get("ready"), block.get("wait_only")
    if type(ready) is not bool or type(wait_only) is not bool:
        return None
    reason = block.get("not_ready_reason")
    if reason is not None and not _bounded_str(reason, 64):
        return None
    if wait_only and (kind is not None or ready or menu_type not in command.UI_KEY_WAIT_ONLY and reason != "event_active"
                      and reason != "text_input"):
        return None
    keys: tuple[tuple[str, str], ...] = ()
    if kind is not None:
        table = command.UI_KEY_TABLE.get(kind)
        raw_keys = block.get("keys")
        if (table is None or table[0] != menu_type or not isinstance(raw_keys, (list, tuple))
                or len(raw_keys) != len(table[1])):
            return None
        for item in raw_keys:
            if (not isinstance(item, Mapping) or item.get("key") not in table[1]
                    or not command.ui_key_token_shape(item.get("target_token"))):
                return None
            keys += ((item["key"], item["target_token"]),)
        if {k for k, _ in keys} != set(table[1]):
            return None
    elif ready:
        return None
    options: tuple[dict, ...] = ()
    if block.get("options") is not None:
        raw = block["options"]
        if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= command.UI_KEY_MAX_OPTIONS:
            return None
        for i, item in enumerate(raw):
            if (not isinstance(item, Mapping) or item.get("index") != i or type(item.get("index")) is not int
                    or not isinstance(item.get("label"), str) or len(item["label"]) > 128
                    or not isinstance(item.get("response_key"), str) or len(item["response_key"]) > 128):
                return None
            options += ({"index": i, "label": item["label"], "response_key": item["response_key"]},)
    if kind in ("question", "npc_question") and ready and not options:
        return None
    professions: tuple[dict, ...] = ()
    if block.get("professions") is not None:
        raw = block["professions"]
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return None
        for i, item in enumerate(raw):
            pid = item.get("profession_id") if isinstance(item, Mapping) else None
            if (not isinstance(item, Mapping) or item.get("index") != i or type(pid) is not int or not 0 <= pid <= 29
                    or not isinstance(item.get("name", ""), str) or len(item.get("name", "")) > 64):
                return None
            professions += ({"index": i, "profession_id": pid, "name": item.get("name", "")},)
    if kind == "level_up_profession" and ready and len(professions) != 2:
        return None
    skill, level = block.get("skill"), block.get("level")
    if (skill is not None and (type(skill) is not int or not 0 <= skill <= 5)) or (
            level is not None and (type(level) is not int or not 1 <= level <= 10)):
        return None
    letter = block.get("letter")
    if letter is not None and (not isinstance(letter, Mapping)
                               or any(type(letter.get(k)) is not bool for k in ("has_items", "has_quest"))
                               or type(letter.get("page")) is not int):
        return None
    day = facts.get("day_transition") if isinstance(facts.get("day_transition"), Mapping) else {}
    showing, pending = day.get("showing_end_of_night_stuff", False), day.get("end_of_night_menus_pending", 0)
    if type(showing) is not bool or type(pending) is not int or pending < 0:
        return None
    question_key = block.get("question_key")
    # WS-G6: the mod publishes Shaft / Bus / pagedResponse only with WorldExtrasEnabled (G3 stub).
    if question_key is not None and (kind != "question" or question_key not in ("Sleep", "Eat", "Shaft", "Bus", "pagedResponse",
                                                                                 "ExitMine", "TV", "Blacksmith", "Marnie",
                                                                                 GIFT_QUESTION_KEY)):  # native-owned TV pin
        return None
    gift = _gift_block(block.get("gift")) if question_key == GIFT_QUESTION_KEY else None
    if question_key == GIFT_QUESTION_KEY and gift is None:
        question_key = None  # Sat gift: a malformed / missing native gift block is never answerable
    return UiKeyView(receiver, menu_type, kind, ready, capability["available"] is True and ready, reason, wait_only,
                     keys, options, professions, skill, level, dict(letter) if letter else None, generation,
                     showing, pending, question_key, gift)


def _gift_block(raw) -> dict | None:
    """Sat gift: the mod's native gift facts (pinned by the pending Gift proposal), bounded; else None."""
    if not isinstance(raw, Mapping):
        return None
    qid, stack, name, sender, room = (raw.get("item_qid"), raw.get("stack"), raw.get("item_name"),
                                      raw.get("sender_name"), raw.get("bag_has_room"))
    if (not isinstance(qid, str) or not 1 <= len(qid) <= 64 or type(stack) is not int or not 1 <= stack <= 999
            or not isinstance(name, str) or len(name) > 64 or not isinstance(sender, str) or len(sender) > 64
            or type(room) is not bool):
        return None
    return dict(item_qid=qid, stack=stack, item_name=name, sender_name=sender, bag_has_room=room)
