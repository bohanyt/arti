namespace ArtiStardewBridge.Protocol;

/// <summary>PM-1 fix11b #2: a native item-grant menu (vanilla <c>essential</c> ItemGrabMenu: overflow from
/// addItemByMenuIfNecessary, fishing treasure, event rewards) holding items meant for the player.
/// Vanilla rescues them only on a forced shutdown; a normal OK/close without <c>superEssential</c> discards them
/// (ItemGrabMenu.cleanupBeforeExit). a16: an item-grant notice with a 12/12 bag, closed via okButton,
/// pole gone. Facts only; which item she takes, swaps or drops stays her choice.</summary>
public sealed record GrantItem(int Id, string Item, int Stack);
/// <param name="Held">PM-1 fix12a #2 (a17 d4:J878-1035): the menu cursor item that no bag slot can take. Vanilla
/// exit/OK then throws it on the ground (MenuWithInventory.RescueHeldItemOnExit / ItemGrabMenu.receiveLeftClick
/// DropHeldItem), with no room left to pick it up.</param>
public sealed record GenericUiGrantFacts(GrantItem[] Items, int BagFree, bool CloseDiscards, GrantItem? Held = null);

public static class GenericUiGrant
{
    public const string CloseGate = "grant_unclaimed_take_or_make_room";

    /// <param name="essential">The menu's own native <c>essential</c> flag (items are meant for the player).</param>
    /// <param name="slots">Item slots: <c>Foreign</c> = not the player's own bag inventory.</param>
    /// <param name="held">The menu's own cursor item. PM-1 fix12a #2: a17 took the Rusty Sword onto the cursor
    /// (12/12 bag), the grant slot emptied, the guard released, and her OK dropped the sword on the Mine floor. A held
    /// item stays guarded while the bag has no free slot; it is released by settling it into the bag (swap) or after
    /// room is made, and deliberate drop/trash keep their own item policy.</param>
    public static GenericUiGrantFacts? Facts(bool essential, bool superEssential,
        IEnumerable<(int Id, string? Item, int Stack, bool Foreign)> slots, int bagFree,
        (string Item, int Stack)? held = null)
    {
        if (!essential) return null;
        var items = slots.Where(s => s.Foreign && !string.IsNullOrEmpty(s.Item))
            .Select(s => new GrantItem(s.Id, s.Item!, s.Stack)).ToArray();
        var cursor = held is { } h && !string.IsNullOrEmpty(h.Item) && bagFree <= 0 ? new GrantItem(-1, h.Item, h.Stack) : null;
        return items.Length == 0 && cursor is null ? null
            : new GenericUiGrantFacts(items, Math.Max(0, bagFree), !superEssential, cursor);
    }

    /// <summary>Every gesture that exits the menu: the configured close/cancel/menu keys and a pointer press on its
    /// own OK or close control. Taking (a grant slot) and making room (bag slot / cursor settle) stay available.</summary>
    public static bool ExitGesture(string operation, string? fieldPath, string? kind) =>
        operation is "close" or "key_cancel" or "key_menu"
        || operation is "left" or "right" or "shift_left" or "shift_right"
            && (kind == "close" || fieldPath?.EndsWith(".okButton", StringComparison.Ordinal) == true);

    public static Dictionary<string, object> Wire(GenericUiGrantFacts f)
    {
        var wire = new Dictionary<string, object>
        {
            ["items"] = f.Items.Select(i => new object[] { i.Id, i.Item, i.Stack }).ToArray(),
            ["take"] = f.Items.Select(i => i.Id).ToArray(),
            ["bag_free"] = f.BagFree,
            ["close_discards"] = f.CloseDiscards,
            ["close_blocked"] = CloseGate,
        };
        // fix12a: an exit with this cursor item drops it at her feet (vanilla), even when the menu is superEssential.
        if (f.Held is { } h) wire["held"] = new object[] { h.Item, h.Stack };
        return wire;
    }
}
