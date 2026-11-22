"""Pure derived view over canonical Stardew ``ui.text`` + ``ui.context`` facts.

The module deliberately owns no receiver, cache, thread, network client, model,
or runtime wiring.  It only interprets caller-supplied observation facts and
fails closed before marking a current choice set actionable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class DialogueOptionView:
    """One visible choice as represented by the canonical ``ui.text`` fact."""

    index: int
    label: str
    selected: bool


@dataclass(frozen=True, slots=True)
class DialogueBinding:
    """Identity pins copied from the canonical ``ui.text.binding`` fact."""

    runtime_instance_id: str
    session_id: str
    player_id: int
    screen_id: int
    location_id: str
    context_generation: int
    ui_revision: int
    menu_instance_token: str
    capture_ticks: int


@dataclass(frozen=True, slots=True)
class PresentedDialogueView:
    """Immutable, read-only interpretation of one dialogue occurrence."""

    available: bool
    readable: bool
    current: bool
    actionable: bool
    provenance: str
    occurrence_key: str | None
    occurrence_seq: int | None
    origin_occurrence_seq: int | None
    status: str | None
    replayed: bool
    family: str | None
    source: str | None
    instance_token: str | None
    page_type: str | None
    text: str | None
    text_revision: int | None
    reveal_status: str | None
    reveal_complete: bool
    capture_complete: bool
    speaker: str | None
    options: tuple[DialogueOptionView, ...]
    options_complete: bool
    binding: DialogueBinding | None
    context_matches: bool
    blockers: tuple[str, ...]


_UNAVAILABLE = PresentedDialogueView(
    available=False,
    readable=False,
    current=False,
    actionable=False,
    provenance="DERIVED",
    occurrence_key=None,
    occurrence_seq=None,
    origin_occurrence_seq=None,
    status=None,
    replayed=False,
    family=None,
    source=None,
    instance_token=None,
    page_type=None,
    text=None,
    text_revision=None,
    reveal_status=None,
    reveal_complete=False,
    capture_complete=False,
    speaker=None,
    options=(),
    options_complete=False,
    binding=None,
    context_matches=False,
    blockers=("ui_text_unavailable",),
)


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    for key in value:
        if not isinstance(key, str):
            return None
    return value


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value


def _parse_binding(value: object) -> DialogueBinding | None:
    obj = _mapping(value)
    if obj is None:
        return None
    runtime_instance_id = _nonempty_str(obj.get("runtime_instance_id"))
    session_id = _nonempty_str(obj.get("session_id"))
    location_id = _nonempty_str(obj.get("location_id"))
    menu_instance_token = _nonempty_str(obj.get("menu_instance_token"))
    player_id = obj.get("player_id")
    screen_id = obj.get("screen_id")
    context_generation = obj.get("context_generation")
    ui_revision = obj.get("ui_revision")
    capture_ticks = obj.get("capture_ticks")
    if (
        runtime_instance_id is None
        or session_id is None
        or location_id is None
        or menu_instance_token is None
        or not _plain_int(player_id)
        or not _plain_int(screen_id)
        or not _plain_int(context_generation)
        or not _plain_int(ui_revision)
        or not _plain_int(capture_ticks)
    ):
        return None
    return DialogueBinding(
        runtime_instance_id=runtime_instance_id,
        session_id=session_id,
        player_id=player_id,
        screen_id=screen_id,
        location_id=location_id,
        context_generation=context_generation,
        ui_revision=ui_revision,
        menu_instance_token=menu_instance_token,
        capture_ticks=capture_ticks,
    )


def _active_meta(value: object) -> tuple[frozenset[int], bool, bool]:
    """Return active seqs, replayed-history flag, and whether meta is usable."""

    meta = _mapping(value)
    if meta is None:
        return frozenset(), False, False
    active = meta.get("active_seqs")
    replayed_history = meta.get("replayed_history")
    if not isinstance(active, (list, tuple)) or not isinstance(replayed_history, bool):
        return frozenset(), False, False
    seqs: set[int] = set()
    for item in active:
        if not _plain_int(item) or item < 0:
            return frozenset(), replayed_history, False
        seqs.add(item)
    return frozenset(seqs), replayed_history, True


def _occurrence_candidates(facts: Mapping[str, object]) -> list[tuple[str, Mapping[str, object]]]:
    out: list[tuple[str, Mapping[str, object]]] = []
    for key, value in facts.items():
        if not key.startswith("occ:"):
            continue
        occurrence = _mapping(value)
        if occurrence is None:
            continue
        if occurrence.get("family") != "dialogue":
            continue
        out.append((key, occurrence))
    return out


def _choose_occurrence(
    facts: Mapping[str, object], occurrence_seq: int | None
) -> tuple[str, Mapping[str, object]] | None:
    candidates = _occurrence_candidates(facts)
    if occurrence_seq is not None:
        if not _plain_int(occurrence_seq) or occurrence_seq < 0:
            return None
        key = f"occ:{occurrence_seq}"
        for candidate_key, occurrence in candidates:
            if candidate_key == key:
                return candidate_key, occurrence
        return None

    active_seqs, _, meta_ok = _active_meta(facts.get("meta"))
    if meta_ok:
        current: list[tuple[int, str, Mapping[str, object]]] = []
        for key, occurrence in candidates:
            seq = occurrence.get("occurrence_seq")
            if (
                _plain_int(seq)
                and seq in active_seqs
                and occurrence.get("status") == "active"
                and occurrence.get("replayed") is False
            ):
                current.append((seq, key, occurrence))
        if current:
            current.sort(key=lambda item: item[0])
            _, key, occurrence = current[-1]
            return key, occurrence

    numbered: list[tuple[int, str, Mapping[str, object]]] = []
    for key, occurrence in candidates:
        seq = occurrence.get("occurrence_seq")
        if _plain_int(seq):
            numbered.append((seq, key, occurrence))
    if not numbered:
        return None
    numbered.sort(key=lambda item: item[0])
    _, key, occurrence = numbered[-1]
    return key, occurrence


def _parse_options(value: object) -> tuple[tuple[DialogueOptionView, ...], bool]:
    if value is None:
        return (), True
    if not isinstance(value, (list, tuple)):
        return (), False
    out: list[DialogueOptionView] = []
    indexes: set[int] = set()
    for raw in value:
        option = _mapping(raw)
        if option is None:
            return tuple(out), False
        index = option.get("index")
        label = option.get("label")
        selected = option.get("selected")
        if (
            not _plain_int(index)
            or index < 0
            or not isinstance(label, str)
            or not isinstance(selected, bool)
            or index in indexes
        ):
            return tuple(out), False
        indexes.add(index)
        out.append(DialogueOptionView(index=index, label=label, selected=selected))
    return tuple(out), True


def _context_check(
    context_facts: Mapping[str, object] | None,
    binding: DialogueBinding | None,
    *,
    occurrence_instance: str | None,
    occurrence_page_type: str | None,
) -> tuple[bool, bool, tuple[str, ...]]:
    """Return identity-match, selectable-context, blockers."""

    if context_facts is None:
        return False, False, ("context_unavailable",)
    if binding is None:
        return False, False, ("binding_invalid",)

    mismatches: list[str] = []
    pin_pairs = (
        ("runtime_instance_id", binding.runtime_instance_id),
        ("session_id", binding.session_id),
        ("player_id", binding.player_id),
        ("screen_id", binding.screen_id),
        ("location_id", binding.location_id),
        ("context_generation", binding.context_generation),
        ("ui_revision", binding.ui_revision),
    )
    for name, expected in pin_pairs:
        actual = context_facts.get(name)
        if actual != expected or isinstance(actual, bool) and not isinstance(expected, bool):
            mismatches.append(name)

    if occurrence_instance != binding.menu_instance_token:
        mismatches.append("occurrence_instance_token")

    interactive = _mapping(context_facts.get("interactive"))
    if interactive is None:
        mismatches.append("interactive")
        selectable = False
    else:
        expected_receiver = f"rcv:{binding.menu_instance_token}"
        if interactive.get("receiver_token") != expected_receiver:
            mismatches.append("receiver_token")
        if interactive.get("menu_type") != "DialogueBox":
            mismatches.append("menu_type")
        context_page_type = interactive.get("page_type")
        if (
            occurrence_page_type
            and isinstance(context_page_type, str)
            and context_page_type
            and context_page_type != occurrence_page_type
        ):
            mismatches.append("page_type")

        snap_id = interactive.get("snap_id")
        caps = _mapping(context_facts.get("capabilities"))
        nav = _mapping(caps.get("ui_navigate")) if caps is not None else None
        selectable = (
            context_facts.get("mode") == "menu"
            and _plain_int(snap_id)
            and nav is not None
            and nav.get("available") is True
        )

    if mismatches:
        return False, False, tuple(f"context_mismatch:{name}" for name in mismatches)
    return True, selectable, (() if selectable else ("context_not_selectable",))


def derive_presented_dialogue(
    ui_text_facts: Mapping[str, object] | None,
    ui_context_facts: Mapping[str, object] | None,
    *,
    occurrence_seq: int | None = None,
) -> PresentedDialogueView:
    """Derive one dialogue/question view without mutating or retaining inputs.

    When ``occurrence_seq`` is omitted, the current active dialogue is preferred;
    if no active dialogue exists, the newest retained dialogue is returned as
    readable history but can never become current/actionable.
    """

    text_facts = _mapping(ui_text_facts)
    if text_facts is None or not text_facts:
        return _UNAVAILABLE

    chosen = _choose_occurrence(text_facts, occurrence_seq)
    if chosen is None:
        return replace(_UNAVAILABLE, blockers=("dialogue_unavailable",))

    occurrence_key, occurrence = chosen
    blockers: list[str] = []

    seq = occurrence.get("occurrence_seq")
    valid_seq = _plain_int(seq) and seq >= 0
    origin_seq = occurrence.get("origin_occurrence_seq")
    valid_origin = _plain_int(origin_seq) and origin_seq >= 0
    expected_key = f"occ:{seq}" if valid_seq else None
    occurrence_identity_valid = valid_seq and occurrence_key == expected_key
    if not occurrence_identity_valid:
        blockers.append("occurrence_identity_invalid")
    origin_occurrence_valid = valid_origin and valid_seq and origin_seq == seq
    if not origin_occurrence_valid:
        blockers.append("origin_occurrence_mismatch")

    status = occurrence.get("status") if isinstance(occurrence.get("status"), str) else None
    replayed_raw = occurrence.get("replayed")
    replayed_valid = isinstance(replayed_raw, bool)
    replayed = replayed_raw is True
    if not replayed_valid:
        blockers.append("replayed_invalid")

    family = occurrence.get("family") if isinstance(occurrence.get("family"), str) else None
    source = occurrence.get("source") if isinstance(occurrence.get("source"), str) else None
    instance_token = (
        occurrence.get("instance_token")
        if isinstance(occurrence.get("instance_token"), str)
        else None
    )
    page_type = occurrence.get("page_type") if isinstance(occurrence.get("page_type"), str) else None
    text = occurrence.get("text") if isinstance(occurrence.get("text"), str) else None
    text_revision = occurrence.get("text_revision")
    text_revision_valid = _plain_int(text_revision) and text_revision >= 1
    if not text_revision_valid:
        text_revision = None
        blockers.append("text_revision_invalid")

    reveal_status = (
        occurrence.get("reveal_status")
        if isinstance(occurrence.get("reveal_status"), str)
        else None
    )
    reveal_complete = reveal_status == "full"
    if reveal_status != "full":
        blockers.append("reveal_incomplete")

    capture_complete = occurrence.get("complete") is True
    if occurrence.get("complete") not in (True, False):
        blockers.append("capture_complete_invalid")
        capture_complete = False
    elif not capture_complete:
        blockers.append("capture_incomplete")

    speaker = occurrence.get("speaker") if isinstance(occurrence.get("speaker"), str) else None
    options, options_shape_complete = _parse_options(occurrence.get("options"))
    options_complete = capture_complete and options_shape_complete
    if not options_shape_complete:
        blockers.append("options_invalid")

    binding = _parse_binding(text_facts.get("binding"))
    if binding is None:
        blockers.append("binding_invalid")

    active_seqs, replayed_history, meta_ok = _active_meta(text_facts.get("meta"))
    if not meta_ok:
        blockers.append("meta_invalid")

    text_current = (
        occurrence_identity_valid
        and origin_occurrence_valid
        and replayed_valid
        and text_revision_valid
        and meta_ok
        and seq in active_seqs
        and status == "active"
        and not replayed
        and not replayed_history
    )
    if status != "active":
        blockers.append("history_or_expired")
    if replayed or replayed_history:
        blockers.append("replayed_history")
    if valid_seq and meta_ok and seq not in active_seqs:
        blockers.append("not_active_seq")

    context = _mapping(ui_context_facts)
    context_matches, context_selectable, context_blockers = _context_check(
        context,
        binding,
        occurrence_instance=instance_token,
        occurrence_page_type=page_type,
    )
    blockers.extend(context_blockers)

    readable = family == "dialogue" and (text is not None or bool(options))
    current = readable and text_current and context_matches
    actionable = (
        current
        and reveal_complete
        and options_complete
        and bool(options)
        and context_selectable
    )

    # ``actionable`` on ui.text is evidence-only in the producer and is never
    # consumed here as authority.  Unknown/additive fields are intentionally
    # ignored, so they cannot grant actionability either.
    if not readable:
        blockers.append("dialogue_unreadable")
    elif current and not options:
        blockers.append("no_selectable_options")

    # Preserve order while de-duplicating deterministic blocker names.
    blockers = list(dict.fromkeys(blockers))

    return PresentedDialogueView(
        available=True,
        readable=readable,
        current=current,
        actionable=actionable,
        provenance="DERIVED",
        occurrence_key=occurrence_key,
        occurrence_seq=seq if valid_seq else None,
        origin_occurrence_seq=origin_seq if valid_origin else None,
        status=status,
        replayed=replayed,
        family=family,
        source=source,
        instance_token=instance_token,
        page_type=page_type,
        text=text,
        text_revision=text_revision,
        reveal_status=reveal_status,
        reveal_complete=reveal_complete,
        capture_complete=capture_complete,
        speaker=speaker,
        options=options,
        options_complete=options_complete,
        binding=binding,
        context_matches=context_matches,
        blockers=tuple(blockers),
    )
