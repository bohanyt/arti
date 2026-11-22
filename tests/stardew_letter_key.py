"""UNIT DOUBLE of the mod's ui_key block for an open mail LetterViewerMenu (ModEntry.UiKey.EnrichUiKeyContext:
``letter`` = page / has_items (itemsLeftToGrab) / has_quest (HasQuestOrSpecialOrder) / is_mail), parsed by the REAL
``arti_stardew_ui.baca_ui_key`` so a test double carries exactly the production fields."""
import arti_stardew_ui as ui


def letter_key_facts(receiver, *, has_quest=False, has_items=False, page=0):
    return {"context_generation": 1, "interactive": {"receiver_token": receiver, "menu_type": "LetterViewerMenu"},
            "capabilities": {"ui_key": {"implemented": True, "available": True, "reason": "ready"}},
            "day_transition": {"showing_end_of_night_stuff": False, "end_of_night_menus_pending": 0},
            "ui_key": {"receiver_token": receiver, "menu_type": "LetterViewerMenu", "ready": True, "wait_only": False,
                       "kind": "letter", "keys": [{"key": "close", "target_token": "uk:" + "0" * 32}],
                       "letter": {"page": page, "has_items": has_items, "has_quest": has_quest, "is_mail": True}}}


def letter_key(receiver, **kw):
    view = ui.baca_ui_key(letter_key_facts(receiver, **kw), age_s=0.1)
    assert view is not None and view.kind == "letter" and view.receiver_token == receiver
    return view
