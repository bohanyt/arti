"""Generic UI primitives. All target IDs, permissions and bindings come from one native inspect."""
from arti_stardew_ui_pad import UiPadView


def inspect_ui(view: UiPadView | None, context=None):
    return dict(menu=view.model_view() if view else None,
                hud_messages=list((context or {}).get("hud_messages") or ()),
                rendered_text=list((context or {}).get("rendered_ui_text") or ()) ,
                state=(context or {}).get("generic_ui_state"))


def press(view, component_id, *, right=False, shift=False):
    return _target(view, component_id, "X" if right else "A", **({"shift": True} if shift else {}))


def type_text(view, component_id, text):
    row = next((c for c in view.components if c["id"] == component_id), None)
    if row is None or row.get("kind") != "text_field":
        raise ValueError("text_field_unavailable")
    return _target(view, component_id, "Y", text=text)


def scroll(view, *, up=False, component_id=None):
    return (_button(view, "LT" if up else "RT") if component_id is None
            else _target(view, component_id, "LT" if up else "RT"))


def close(view, *, settle_target=None):
    if view.held_item is not None or (view.cursor_settle or {}).get("cursor"):
        from arti_stardew_cursor_item import close_step
        return close_step(view, settle_target)
    return _button(view, "B")


def key(view, key):
    return _button(view, {"confirm": "Start", "cancel": "LB", "skip": "Select", "use": "RB"}[key])


def hold(view, ticks, component_id=None, *, right=False):
    if type(ticks) is not int or not 1 <= ticks <= 120:
        raise ValueError("invalid_generic_ui_hold")
    return (_button(view, "RB", value=str(ticks)) if component_id is None
            else _target(view, component_id, "X" if right else "A", value=str(ticks)))
def settle_cursor_item(view, destination_id):
    from arti_stardew_cursor_item import settle_cursor_item as settle
    return settle(view, destination_id)


def _target(view, component_id, button, **payload):
    if type(component_id) is not int or not any(c["id"] == component_id for c in view.components):
        raise ValueError("component_unavailable")
    return _button(view, button, choice=component_id, **payload)


def _button(view, button, **payload):
    if not view.available or not view.generic or not view.press_allowed(button):
        raise ValueError("generic_ui_unavailable")
    return "ui_press", view.press_args(button, **payload)
