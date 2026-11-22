"""Test double of the native affordance publisher (UiAffordances.Publish / ContainerTransferAdmission.Publish).

Hand-built UiPadView / container doubles carry no native publication; this stands in for the mod so those older
scenarios keep exercising the Python boundary. It is never production code: the real publication is emitted by
ProtocolTests and replayed by a replay test (which bypasses this double); neither is in this repository.
"""
V1 = ("(O)388", "(O)390")


def publish_view(view):
    if not view.available:
        return dict(list=[], none=view.wait_reason or "not_ready")  # native: the gate refuses every control
    out = []
    for button, cell in view.buttons.items():
        does = cell.get("does") if isinstance(cell, dict) else None
        if button == "tab" or not isinstance(does, str):
            continue
        if button == "stick":
            if not view.generic:
                out.append(dict(verb="choose", control="stick", dirs=["up", "right", "down", "left"]))
            continue
        low = does.casefold()
        role = "exit" if button in ("B", "Start") and "close" in low else "advance" if "advance" in low or "confirm" in low else None
        if "choice=id" in does and button in ("A", "X", "Y"):
            visible = [c["id"] for c in view.components if c.get("visible", True) is True]
            if button == "Y":
                ids = [c["id"] for c in view.components if c.get("kind") == "text_field" and c.get("visible", True) is True]
                if ids:
                    out.append(dict(verb="type", controls=ids))
            elif visible:
                out.append(dict(verb="choose", controls=visible, buttons=[button, "shift" + button]))
            if view.context == "question" or view.snapped is None or view.snapped.get("visible", True) is not True:
                continue
        out.append(dict(verb="scroll" if "scroll" in low else "choose", control=button, **({"role": role} if role else {})))
    settle = view.cursor_settle or {}
    if settle.get("cursor"):
        ids = [t["id"] for t in settle.get("targets", ()) if t.get("kind") != "trash"]
        if ids:
            out.append(dict(verb="choose", controls=ids, buttons=["settle"]))
    return dict(list=out)


def publish_transfers(container, facts):
    """Puts the native transfer arm admits: capability ready, cursor clear, capacity (ContainerTransferReadback.Read)."""
    caps = ((facts or {}).get("capabilities") or {}).get("container_transfer") or {}
    if caps.get("available") is not True or container.get("cursor") is not None:
        return set()
    published = set()
    for slot in container.get("actor_slots", ()):
        item = slot.get("item")
        if not item or type(item.get("max_stack")) is not int or not 0 < item["max_stack"] <= 999:
            continue
        capacity = sum(item["max_stack"] if d["item"] is None else max(0, item["max_stack"] - d["item"]["stack"])
                       if (d["item"]["qualified_item_id"], d["item"]["quality"], d["item"].get("max_stack"))
                       == (item["qualified_item_id"], item["quality"], item["max_stack"]) else 0
                       for d in container.get("container_slots", ()))
        v1 = item["qualified_item_id"] in V1 and item["quality"] == 0
        if capacity >= item["stack"]:
            published.add((slot["slot"], "put" if v1 else "quick"))
        if v1 and capacity >= 1 and item["stack"] > 1 and caps.get("single_item") is True:
            published.add((slot["slot"], "put_one"))
    return published
