using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>
/// PM-1 fix10d (a13 d4:J2319): the chest arm's own put-back of the item its click left on the menu cursor. One vanilla
/// click on the same source slot with the item held: a chest slot goes through ItemGrabMenu.behaviorFunction
/// (Chest.grabItemFromInventory adds it back and reopens the menu), an actor slot through InventoryMenu.leftClick
/// (places it back). The caller runs it with no keyboard modifier. Kept off ModEntry so its compiler-generated IL site
/// names stay unchanged.
/// </summary>
static class TransferCursorReturn
{
    public static void Click(Func<bool> sameActor, Func<IClickableMenu?, bool> sameChest, Point point)
    {
        if (!sameActor() || Game1.activeClickableMenu is not ItemGrabMenu now || !sameChest(now) || now.heldItem is null)
            throw new InvalidOperationException("return target lost");
        now.receiveLeftClick(point.X, point.Y);
        if (Game1.activeClickableMenu is ItemGrabMenu after && sameChest(after))
            after.releaseLeftClick(point.X, point.Y);
    }
}

/// <summary>
/// PM-1 fix10d: Stardew 1.6 MenuWithInventory.heldItem is a PROPERTY over the private _heldItem, so the generic
/// field-named "heldItem" reads never saw a chest / shipping-bin cursor item (a13: the Parsnip on the chest cursor read
/// held_item=null while native refused close and transfers for cursor_item). Pure getter (il_read_allowlist).
/// </summary>
static class MenuHeldItem
{
    public static Item? Of(object? owner) => (owner as MenuWithInventory)?.heldItem;
}
