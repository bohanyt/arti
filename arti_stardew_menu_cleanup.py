"""Pure terminal menu ownership and close-button selection. Native permission stays authoritative."""
from arti_stardew_generic_ui import unsafe_control


def close_button(view):
    """Choose only a published close; B on questions can ANSWER and is never inferred safe."""
    if view is None or not view.available or view.held_item is not None:
        return None
    for button in ("B", "Start"):
        does = str((view.buttons.get(button) or {}).get("does", "")).casefold()
        if (view.press_allowed(button) and "close" in does
                and "last response" not in does and not unsafe_control(view, button)):
            return button
    return None


def owned_menu(run, goal_id, last, view):
    """Ownership follows a verified receipt, never a menu type or an interrupted movement."""
    if run is None or run.goal_id != goal_id or view is None:
        return False
    get = lambda key, default=None: last.get(key, default) if isinstance(last, dict) else getattr(last, key, default)
    if not get("request_id") or get("lifecycle") != "completed" or get("cleanup_confirmed") is not True:
        return False
    after = get("after") or {}
    receivers = [after.get("receiver_token")]
    receivers += [(after.get(k) or {}).get("result_receiver_token") for k in ("ui_press", "ui_key", "ui_nav")]
    return view.receiver_token in receivers
