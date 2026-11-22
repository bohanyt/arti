using ArtiStardewBridge.Protocol;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>PM-1 fix14b (ADR 33): the game's own held-item menu facts, read from public menu fields. Field reads and
/// pure getters only; outside ModEntry so its compiler-generated member ordinals (IL D-row keys) stay stable.</summary>
static class HeldItemMenu
{
    /// <summary>Whether this menu passes a held item to a non-bag inventory at all. ItemGrabMenu only does through its
    /// own behaviorFunction / reverseGrab (ItemGrabMenu.receiveLeftClick); an overflow / reward menu (a16/a17/a19)
    /// has neither, so its grant slots ignore a held item. Other menus keep their published route.</summary>
    public static bool ContainerRoute(IClickableMenu? menu) =>
        menu is not ItemGrabMenu grab || grab.behaviorFunction is not null || grab.reverseGrab;

    /// <summary>The menu's own exit with a cursor item. MenuWithInventory.readyToClose() is heldItem == null unless
    /// AllowExitWithHeldItem; then RescueHeldItemOnExit applies HeldItemExitBehavior and drops what the bag cannot
    /// take. CraftingPage / InventoryPage / ShopMenu: readyToClose() is "no held item".</summary>
    public static string Exit(IEnumerable<IClickableMenu?> owners, Item cursor)
    {
        MenuWithInventory? owner = null;
        foreach (var menu in owners)
            if (menu is MenuWithInventory m && ReferenceEquals(m.heldItem, cursor)) { owner = m; break; }
        if (owner is not { AllowExitWithHeldItem: true }) return CursorItemSettle.ExitBlocked;
        return owner.HeldItemExitBehavior switch
        {
            ItemExitBehavior.Discard => CursorItemSettle.ExitDiscards,
            ItemExitBehavior.Drop => CursorItemSettle.ExitDrops,
            // a free slot takes it; a partial stack merge is not assumed (the conservative fact is the drop)
            _ => Game1.player.freeSpotsInInventory() > 0 ? CursorItemSettle.ExitReturns : CursorItemSettle.ExitDrops,
        };
    }

    /// <summary>The game's own slot eligibility (InventoryMenu.highlightMethod); null = a mod callback, never invoked.</summary>
    public static bool? Highlighted(InventoryMenu inventory, Item item) => inventory.highlightMethod is null ? true
        : inventory.highlightMethod.Method.DeclaringType?.Assembly == typeof(Game1).Assembly ? inventory.highlightMethod(item) : null;
}
