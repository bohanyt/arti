"""ADR 42: bounded game-only inputs for diagnosing UI render pressure."""
import hashlib
import json
import os
from pathlib import Path


MAX_BYTES = 512 * 1024
# Explicit allowlist: never serialize the request, snapshot, owner goal, mind,
# preferences, provider messages/instructions, or configuration.
UI_FIELDS = frozenset((
    "menu_type", "context", "components", "components_total", "snapped", "generic", "hud",
    "receiver", "state_digest", "component_state", "ambient_state", "ui_safety", "menu_items",
    "end_of_night_status", "passout_readiness", "cursor_settle", "held_item", "text", "options",
    "buttons", "ready_check", "crafting", "shop", "transaction", "activity", "actions", "exit_actions",
    "inspect_page", "world", "discoveries", "recent_ui_results", "recovery_facts", "native_motor",
    "last_ui_readback", "prior_ui_delta", "prior_world_delta", "prior_world_delta_source"))
# fix42 (a33 d7:J649/659/669): skill-turn render failures left no input at all. Game blocks only; never the owner
# goal, mind, viewer/owner advisories or decision feedback. Captured on render_failed only (no per-turn I/O).
SKILL_FIELDS = frozenset((
    "trigger", "live7", "skills", "facts", "world", "discoveries", "game_facts", "progress", "capabilities", "day",
    "outcomes", "recovery", "recent_actions", "observed_ui_delta", "native_ui_notices", "boundary_page",
    "render_budget"))
PRIVATE_FIELDS = frozenset(("owner_goal", "mind", "prefs", "preferences", "speech", "why", "prompt",
                           "provider_prompt", "base_instructions", "developer_instructions", "instruction"))


def _game_only(value):
    if isinstance(value, dict):
        return {k: _game_only(v) for k, v in value.items() if k not in PRIVATE_FIELDS}
    if isinstance(value, (list, tuple)):
        return [_game_only(v) for v in value]
    return value


def archive_ui_input(folder, request, slot, reason):
    """Two rotating files, <=512 KiB each; never append input blobs to the journal.

    Oversized inputs get a digest/count record, explicitly not an exact capture.
    Failure to write diagnostics never prevents a native decision.
    """
    offer = request.skill_offer
    if not isinstance(offer.get("unknown_ui"), dict):
        if reason != "render_failed" or not isinstance(offer.get("skills"), list):
            return None
        game = dict(skill_offer=_game_only({k: v for k, v in offer.items() if k in SKILL_FIELDS}))
        return _write(folder, slot, reason, game)
    game = dict(unknown_ui=_game_only({k: v for k, v in offer["unknown_ui"].items() if k in UI_FIELDS}),
                skills=[dict(name="ui_action", args=e.get("args", [])) for e in offer.get("skills", ())
                        if e.get("name") == "ui_action"], render_budget=offer.get("render_budget"))
    return _write(folder, slot, reason, game)


def _write(folder, slot, reason, game):
    raw = json.dumps(game, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf8")
    record = dict(schema="ui-render-input.v1", reason=reason, bytes=len(raw),
                  sha256=hashlib.sha256(raw).hexdigest(), exact_game_input=True, game=game)
    encoded = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("utf8")
    if len(encoded) > MAX_BYTES:
        record.pop("game")
        record["exact_game_input"] = False
        record["omitted"] = "size_cap"
        encoded = json.dumps(record, separators=(",", ":")).encode("utf8")
    try:
        root = Path(folder) / "ui-render-inputs"
        root.mkdir(parents=True, exist_ok=True)
        target = root / ("slot-%d.json" % (slot % 2))
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, target)
        return dict(file=target.name, bytes=len(encoded), exact_game_input=record["exact_game_input"])
    except OSError:
        return None
