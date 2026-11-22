namespace ArtiStardewBridge.Protocol;

// One controller for all menus. Widget IDs come only from inspect; no menu names or per-menu tables.
public interface IGenericUiPort
{
    void ApplyGeneric(string operation, int componentId, string? value);
}

public static class GenericUiPolicy
{
    public static UiPadPlan Plan(string button, UiPadFacts f, IReadOnlyDictionary<string, string>? args)
    {
        UiPadPlan No(string why) => UiPadPlan.No(button, "UI_DENIED:" + why);
        UiPadPlan Input(string op, int id = -1, string? value = null, string? does = null) =>
            UiPadPlan.Ok(button, does ?? op, new UiPadStep(UiPadStepKind.GenericInput, button,
                G3Action: op, G3Arg: value, RowIndex: id));
        // ADR 246 review B1: the native ui_press parse hands an EMPTY payload for a bare press (UiPadContextTable.AdmitPress).
        // A bare press and a press with only modifiers are the same press without a named choice; only "choice" names a part.
        if (args is { Count: 0 })
            args = null;
        bool named = args?.ContainsKey("choice") == true;
        if (f.GenericValues?.GetValueOrDefault("ui_input_gate") is string { Length: > 0 } gate)
            return No("blocked_now:" + gate);
        if (button is "B" or "LB" && f.GenericValues?.GetValueOrDefault("ui_close_blocked") is string { Length: > 0 } closeGate)
            return No("blocked_now:" + closeGate);
        if (f.GenericValues?.GetValueOrDefault("ui_prose") is true && button is not "Start")
            return No("blocked_now:advance_only_use_confirm");
        if (args?.Keys.Any(k => k is not ("choice" or "text" or "shift" or "value" or "settle" or "qty")) == true)
            return No("generic_payload_invalid");
        string? duration = null;
        if (args?.TryGetValue("value", out string? ticks) == true)
        {
            if (!int.TryParse(ticks, out int count) || count < 1 || count > GenericUiFrames.MaxHoldTicks
                || button is not ("A" or "X" or "RB")) return No("generic_hold_invalid");
            duration = "ticks:" + count;
        }
        bool nonmodalOwner = f.GenericValues?.GetValueOrDefault("minigame") is string { Length: > 0 }
            || f.GenericValues?.GetValueOrDefault("event_id") is string { Length: > 0 }
            || f.GenericValues?.GetValueOrDefault("fish_caught") is true;
        if (!f.MenuOpen && !nonmodalOwner && button is "Start" or "LB" or "RB")
        {
            if (f.GenericValues?.GetValueOrDefault("q12_state") as string == "adapter_unsupported_capability")
                return No("adapter_unsupported_capability:q12_native_edge_not_consumed");
            return f.GenericValues?.GetValueOrDefault("q12_backend_edges") is true
                && f.GenericValues?.GetValueOrDefault("q12_state") as string is "unproven" or "supported"
                ? Input(button == "Start" ? "key_menu" : "key_" + button) : No("blocked_now:q12_backend_disabled");
        }
        if (!f.MenuOpen && !nonmodalOwner && button is "Start" or "LB" or "RB" or "Select")
            return No("no_input_owner");
        if (button == "RB") return Input("key_use", value: duration, does: "use / hold through the configured tool key (value=1..120 ticks)");
        // PM-1 fix14b (ADR 33): with a cursor item the menu's own readyToClose() refuses Escape / B / OK unless it
        // declares a safe exit (CursorItemSettle.Exit). a19 d2:J80-85 chose LB with two crafted Chests held: no effect.
        string? heldExit = f.HeldItemId is null ? null
            : f.CursorSettle?.Cursor is null ? "cursor_item" : CursorItemSettle.ExitAdmitted(f.CursorSettle) ? null
            : "cursor_item:" + f.CursorSettle.Exit;
        // The action key does nothing in a held-item menu either (a19 J987-1553: press:Start x13, no effect).
        if (button == "Start")
            return heldExit is not null ? No(heldExit) : Input("key_confirm", does: "confirm / advance with the configured action key");
        // W1b B1: a close with an item held never drops it (every close path: LB, B and an exit part pressed with A/X).
        if (button == "LB")
            return heldExit is not null ? No(heldExit) : GenericHarm.HeldItemClose(f) is string lbDrop ? No(lbDrop)
                : Input("key_cancel", does: "cancel with Escape through a virtual frame");
        if (button == "Select")
            return f.HeldItemId is null && f.GenericValues?.GetValueOrDefault("event_skippable") is true
                ? Input("key_skip", does: "skip the currently skippable event") : No("not_skippable");
        if (button == "B")
            return f.GenericValues?.GetValueOrDefault("ready_check") is IReadOnlyDictionary<string, object> check
                && check.GetValueOrDefault("cancelable") is false ? No("ready_check_not_cancelable")
                : heldExit is not null ? No(heldExit)
                : GenericHarm.HeldItemClose(f) is string bDrop ? No(bDrop)
                : f.GenericValues?.GetValueOrDefault("ui_required_choice") is true ? No("blocked_now:required_choice")
                : Input("close", does: f.HeldItemId is null
                ? "close using the offered exit or guarded native exit"
                : "close; the game returns the held item to the bag");
        if (button is "LT" or "RT")
        {
            int scrollTarget = -1;
            if (args?.TryGetValue("choice", out string? choice) == true
                    && (!int.TryParse(choice, out scrollTarget) || !f.Comps.Any(c => c.Id == scrollTarget)))
                return No("generic_component_unavailable");
            return Input("scroll", scrollTarget, button == "LT" ? "120" : "-120", "scroll the active UI");
        }
        if (button is not ("A" or "X" or "Y")) return No("generic_button_not_offered");
        int id = f.SnappedId ?? -1;
        if (args?.TryGetValue("choice", out string? raw) == true && !int.TryParse(raw, out id))
            return No("generic_component_invalid");
        UiPadComponent? target = f.Comps.SingleOrDefault(c => c.Id == id);
        if (!named && target is null)
            target = f.Comps.FirstOrDefault(c => c.Visible && (button != "Y" || c.Kind == "text_field"));
        if (target is null || !target.Visible || target.W <= 0 || target.H <= 0) return No("generic_component_unavailable");
        if (f.GenericValues?.GetValueOrDefault("ui_close_blocked") as string == GenericUiGrant.CloseGate
                && GenericUiGrant.ExitGesture(button == "A" ? "left" : "right", target.FieldPath, target.Kind))
            return No("blocked_now:" + GenericUiGrant.CloseGate);
        if (heldExit is not null && GenericUiGrant.ExitGesture(button == "A" ? "left" : "right", target.FieldPath, target.Kind))
            return No(heldExit);
        if ((GenericUiGrant.ExitGesture(button == "A" ? "left" : "right", target.FieldPath, target.Kind)
                || string.Equals(target.Kind, "close", StringComparison.Ordinal))
            && GenericHarm.HeldItemClose(f) is string partDrop)
            return No(partDrop); // W1b B1: an exit part, named or not, never drops the held item
        if (f.CursorSettle is { Cursor: not null } held && held.Inert.Contains(target.Id))
            return No("held_item_slot_inert"); // the game's own click path ignores this slot while an item is held
        // ADR 246: a press without a named choice (bare, or with only modifiers) never buys, sells, ships, trashes or
        // drops (the harm in game terms), and facts that cannot tell are refused (review B1, fail closed).
        if (!named && button is "A" or "X" && GenericHarm.BlindMenuPress(f, target) is string blind)
            return No(blind);
        string? itemKind = f.CursorSettle?.Destinations.SingleOrDefault(d => d.Id == target.Id)?.Kind;
        if (CursorItemSettle.Destructive(target.Kind) || CursorItemSettle.Destructive(itemKind))
        {
            if (CursorItemSettle.HeldDisposeRefusal(f.CursorSettle?.Cursor, itemKind ?? target.Kind) is { } denied)
                return No("item_policy:" + denied);
            bool disposalShift = args?.TryGetValue("shift", out string? modifier) == true && modifier == "true";
            return Input((disposalShift ? "shift_" : "") + (itemKind ?? target.Kind) + (button == "X" ? "_right" : "_left"),
                target.Id, does: "dispose chosen item (choice=id); inventory/debris readback required");
        }
        if (args?.TryGetValue("settle", out string? settle) == true)
        {
            if (settle != "true" || button != "A" || args.ContainsKey("shift")) return No("cursor_settle_payload");
            if (CursorItemSettle.PrepareRefusal(f.CursorSettle, target.Id) is { } denied) return No(denied);
            return Input("settle", target.Id, does: "settle cursor in the chosen observed destination; conserve items");
        }
        if (System.Text.RegularExpressions.Regex.IsMatch(target.Label,
                @"quit|exit.?game|exit.?to.?title|return.?to.?title|delete.*save|overwrite.*save|keluar.*(?:game|permainan|judul)|kembali.*judul",
                System.Text.RegularExpressions.RegexOptions.IgnoreCase)) return No("irreversible_control");
        if (button == "Y")
        {
            if (target.Kind != "text_field") return No("generic_text_field_required");
            if (args is null) return Input("type", target.Id, does: "type {text} into offered text field (choice=id)");
            if (!args.TryGetValue("text", out string? text) || text.Length > 128
                    || text.Any(c => char.IsControl(c))) return No("generic_text_invalid");
            return Input("type", target.Id, text, "type into the offered text field");
        }
        if (args?.TryGetValue("qty", out string? quantity) == true)
        {
            var row = f.ShopCatalog?.Rows.SingleOrDefault(r => r.Control == target.Id);
            if (button != "A" || !int.TryParse(quantity, out int qty) || row is null
                || args.Keys.Any(k => k is not ("choice" or "qty"))) return No("shop_quantity_invalid");
            if (GenericShop.Refusal(row, f.ShopCatalog!.Balance, qty, f.ShopCatalog.Ready, f.ShopCatalog.Held) is { } refused)
                return No(refused);
            return Input(qty == 25 ? "control_shift_left" : qty == 5 ? "shift_left" : "left", target.Id,
                does: "buy " + qty + " of the observed stock row; read currency and inventory delta");
        }
        bool shift = args?.TryGetValue("shift", out string? shifted) == true && shifted == "true";
        // PM-1 fix13b: a recipe the game would not craft now (its own reason) is never a published choice.
        if (CraftingFacts.ChooseRefusal(f.GenericValues, target.Id, shift) is { } craft) return No(craft);
        return Input((shift ? "shift_" : "") + (button == "A" ? "left" : "right"), target.Id, duration,
            does: "press the offered control (choice=id)");
    }
}

public static class Q12Capability
{
    public const string State = "adapter_unsupported_capability";
}
