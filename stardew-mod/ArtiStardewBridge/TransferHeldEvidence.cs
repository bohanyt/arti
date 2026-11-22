using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>
/// PM-1 fix6 #2: a synchronous menu click holds nothing but a cursor item; that is the native held evidence a
/// transfer's cleanup doubt resolves on. Kept off ModEntry so its compiler-generated IL site names stay unchanged.
/// </summary>
static class TransferHeldEvidence
{
    public static bool Pending(TransferDoubt doubt, long nowMs)
    {
        var menu = Context.IsWorldReady ? Game1.activeClickableMenu as ItemGrabMenu : null;
        if (menu is null) doubt.MenuGone();
        return doubt.CleanupPending(menu?.heldItem is null && Game1.player?.CursorSlotItem is null, nowMs);
    }
}
