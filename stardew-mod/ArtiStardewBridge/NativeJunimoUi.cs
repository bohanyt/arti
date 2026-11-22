using StardewValley.Menus;

namespace ArtiStardewBridge;

// JunimoNoteMenu.setUpBundleSpecificPage returns before populateClickableComponentList when SnappyMenus is off.
// Read current page fields, never its potentially stale aggregation. No menu field is changed by this capture.
internal static class NativeJunimoUi
{
    internal static List<ClickableComponent> Components(JunimoNoteMenu menu)
    {
        var result = new List<ClickableComponent>();
        if (menu.specificBundlePage)
        {
            result.AddRange(menu.inventory.inventory);
            result.AddRange(menu.ingredientSlots);
            result.AddRange(menu.ingredientList);
            if (menu.backButton is { } back) result.Add(back);
            if (menu.purchaseButton is { } purchase) result.Add(purchase);
        }
        else
        {
            result.AddRange(menu.bundles);
            if (menu.presentButton is { } present) result.Add(present);
            if (menu.areaNextButton is { } next) result.Add(next);
            if (menu.areaBackButton is { } previous) result.Add(previous);
        }
        if (menu.upperRightCloseButton is { } close) result.Add(close);
        return result;
    }

    internal static int? SnappedId(JunimoNoteMenu menu) => menu.currentlySnappedComponent is { } snapped
        && Components(menu).Any(c => ReferenceEquals(c, snapped)) ? snapped.myID : null;

    internal static int PageIndex(JunimoNoteMenu menu) => menu.specificBundlePage ? 1 : 0;
}
