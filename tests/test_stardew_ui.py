"""Kontrak semantic UI: hanya yang terlihat pemain, tanpa input/game API."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arti_stardew import StardewStateStore, decode_event  # noqa: E402
from arti_stardew_ui import bangun_ui_snapshot  # noqa: E402

P2 = -100000000000000001


def _snapshot(menu: dict, *, active_menu: str, ui_revision: int = 10):
    payload = {
        "location": "Cabin",
        "tile": {"x": 10, "y": 9},
        "pixel": {"x": 640, "y": 576},
        "facing": 1,
        "time_of_day": 610,
        "date": {"season": "spring", "day": 1, "year": 1},
        "energy": 270,
        "health": 100,
        "is_player_free": False,
        "can_player_move": False,
        "active_menu": active_menu,
        "ui_revision": ui_revision,
        "menu": menu,
    }
    event = decode_event(json.dumps({
        "protocol": 1, "ev": "state", "seq": 2,
        "player_id": P2, "screen_id": 1, "payload": payload,
    }))
    assert event is not None
    store = StardewStateStore()
    store.catat(event)
    return store.latest(P2)


def _component(
    key: str,
    native_id: int,
    role: str,
    *,
    text: str | None = None,
    count: int | None = None,
    selected: bool = False,
):
    return {
        "key": key,
        "native_id": native_id,
        "source": "page",
        "role": role,
        "name": key,
        "label": text,
        "text": text,
        "count": count,
        "selected": selected,
        "bounds": {"x": native_id * 64, "y": 64, "w": 64, "h": 64},
        "neighbors": {"up": None, "right": native_id + 1,
                      "down": None, "left": native_id - 1},
    }


def test_dialogue_adapter_membaca_teks_speaker_dan_choices_visible():
    state = _snapshot({
        "name": "DialogueBox",
        "menu_type": "DialogueBox",
        "page_type": "DialogueBox",
        "snap_id": 1,
        "snap_label": "No",
        "snap_bounds": {"x": 64, "y": 64, "w": 64, "h": 64},
        "close_id": None,
        "focus_key": "dialogue.choice.1",
        "visible_text": "Ready to start?",
        "speaker": "Robin",
        "components": [
            _component("dialogue.choice.0", 0, "choice", text="Yes"),
            _component("dialogue.choice.1", 1, "choice", text="No", selected=True),
        ],
    }, active_menu="DialogueBox")

    ui = bangun_ui_snapshot(state)

    assert ui.surface == "DialogueBox"
    assert ui.visible_text == "Ready to start?"
    assert ui.speaker == "Robin"
    assert ui.focus == "dialogue.choice.1"
    assert [(e.key, e.text, e.selected) for e in ui.elements] == [
        ("dialogue.choice.0", "Yes", False),
        ("dialogue.choice.1", "No", True),
    ]


def test_inventory_adapter_memakai_semantic_slot_dan_display_item_visible():
    state = _snapshot({
        "name": "GameMenu",
        "menu_type": "GameMenu",
        "page_type": "InventoryPage",
        "snap_id": 3,
        "snap_label": "Parsnip Seeds",
        "snap_bounds": {"x": 192, "y": 128, "w": 64, "h": 64},
        "close_id": 9175502,
        "focus_key": "inventory.slot.3",
        "components": [
            _component("game.tab.inventory", 12340, "tab", text="Inventory", selected=True),
            _component("inventory.slot.0", 0, "inventory_slot", text="Hoe", count=1),
            _component("inventory.slot.3", 3, "inventory_slot", text="Parsnip Seeds", count=15, selected=True),
        ],
    }, active_menu="GameMenu")

    ui = bangun_ui_snapshot(state)

    assert ui.page == "InventoryPage"
    assert ui.focus == "inventory.slot.3"
    assert [(e.key, e.role, e.text, e.count) for e in ui.elements] == [
        ("game.tab.inventory", "tab", "Inventory", None),
        ("inventory.slot.0", "inventory_item", "Hoe", 1),
        ("inventory.slot.3", "inventory_item", "Parsnip Seeds", 15),
    ]


def test_ready_check_adapter_hanya_mengekspos_pilihan_yang_terlihat():
    state = _snapshot({
        "name": "ReadyCheckDialog",
        "menu_type": "ReadyCheckDialog",
        "page_type": "ReadyCheckDialog",
        "snap_id": 0,
        "snap_label": "Ready",
        "snap_bounds": {"x": 64, "y": 64, "w": 64, "h": 64},
        "close_id": None,
        "focus_key": "ready.choice.0",
        "visible_text": "Waiting for other players",
        "speaker": None,
        "components": [
            _component("ready.choice.0", 0, "choice", text="Ready", selected=True),
            _component("ready.choice.1", 1, "choice", text="Cancel"),
        ],
    }, active_menu="ReadyCheckDialog")

    ui = bangun_ui_snapshot(state)

    assert ui.visible_text == "Waiting for other players"
    assert [e.key for e in ui.elements] == ["ready.choice.0", "ready.choice.1"]
    assert all(e.role == "choice" for e in ui.elements)
