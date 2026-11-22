using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Menus;
using StardewValley.Objects;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

// W1 gap B3: read-only native admission shared by the faced capture (ModEntry.B3Capture.cs) and the ui.context
// catalog. Every call here is a native predicate or a field read; never placementAction, performObjectDropInAction
// with probe=false, checkForAction, ActivateKitchen or a menu constructor. Rules live in Protocol/B3Offers.cs.
internal static class NativeB3Offers
{
    /// <summary>The crab pot at a tile (the exact object instance), or null.</summary>
    internal static CrabPot? PotAt(GameLocation location, Vector2 tile) =>
        location.objects.TryGetValue(tile, out SObject? o) && o is CrabPot pot ? pot : null;

    /// <summary>Native CrabPot.IsValidCrabPotLocationTile; read fault = invalid.</summary>
    internal static bool ValidPotTile(GameLocation location, int x, int y)
    {
        try { return CrabPot.IsValidCrabPotLocationTile(location, x, y); }
        catch { return false; }
    }

    /// <summary>BOTH native placement predicates for a held crab pot at the tool tile: the crab-pot water rule and
    /// Utility.playerCanPlaceItemHere (forbidden locations, events, bridge, reach, canBePlacedHere, isPlaceable).</summary>
    internal static bool CanPlacePot(GameLocation location, Farmer player, SObject held, Vector2 tile)
    {
        bool canPlace;
        try { canPlace = Utility.playerCanPlaceItemHere(location, held, (int)tile.X * 64, (int)tile.Y * 64, player); }
        catch { canPlace = false; }
        return CrabPotRule.PlaceAdmitted(held.QualifiedItemId, ValidPotTile(location, (int)tile.X, (int)tile.Y), canPlace);
    }

    /// <summary>The native drop-in PROBE of a bait object (probe: true mutates nothing, CrabPot.cs:236-251).</summary>
    internal static bool BaitProbe(CrabPot pot, SObject held, Farmer player)
    {
        try { return pot.performObjectDropInAction(held, true, player); }
        catch { return false; }
    }

    internal static bool NeedsBait(CrabPot pot, Farmer player)
    {
        try { return pot.NeedsBait(player); }
        catch { return false; }
    }

    internal static bool CollectReady(CrabPot pot) =>
        CrabPotRule.CollectReady(pot.tileIndexToShow, pot.heldObject.Value is not null, pot.readyForHarvest.Value);

    /// <summary>CrabPot.checkForAction doubles the catch at 25 % with the Book_Crabbing stat (CrabPot.cs:281).</summary>
    internal static bool CrabbingDoubling(Farmer player)
    {
        try { return player.stats.Get("Book_Crabbing") != 0; }
        catch { return true; } // unknown: admit the native doubling as the only other exact count
    }

    internal static string PotId(GameLocation location, Vector2 tile, CrabPot pot) =>
        CrabPotRule.PotId(SceneIdentity.Token(location.NameOrUniqueName ?? location.Name ?? "_"), (int)tile.X, (int)tile.Y,
            pot.owner.Value);

    /// <summary>The actor's own kitchen at a tile: the exact vanilla action in a home this actor owns, no earlier
    /// FarmHouse / Cabin tile handler. null = admitted, else the refusal (KitchenRule.Refusal).</summary>
    internal static string? KitchenMiss(GameLocation location, Farmer actor, Point tile)
    {
        string? action = location.doesTileHaveProperty(tile.X, tile.Y, "Action", "Buildings");
        if (!KitchenRule.IsKitchenAction(action))
            return "not_kitchen";
        int buildings = -1, indoor = -1;
        try
        {
            buildings = location.getTileIndexAt(tile.X, tile.Y, "Buildings", "untitled tile sheet");
            indoor = location.getTileIndexAt(tile.X, tile.Y, "Buildings", "indoor");
        }
        catch
        {
            return "kitchen_tile_unreadable";
        }
        FarmHouse? home = location as FarmHouse;
        bool owned = home is not null && ReferenceEquals(actor, Game1.player) && home.OwnerId == actor.UniqueMultiplayerID;
        return KitchenRule.Refusal(action, home is not null, owned, location.IsOutdoors, buildings, indoor, location is Cabin);
    }

    /// <summary>A fridge / mini-fridge mutex already locked: ActivateKitchen's MultipleMutexRequest would fail.</summary>
    internal static bool KitchenBusy(GameLocation location)
    {
        try
        {
            if (location.GetFridge() is { } fridge && fridge.mutex.IsLocked())
                return true;
            return location.objects.Values.Any(o => o is Chest { } c && c.bigCraftable.Value && c.fridge.Value && c.mutex.IsLocked());
        }
        catch
        {
            return true;
        }
    }

    /// <summary>The standalone cooking page ActivateKitchen opens (never a GameMenu-hosted crafting tab).</summary>
    internal static bool CookingReceiver(object? menu) => menu is CraftingPage { cooking: true } page
        && ReferenceEquals(Game1.activeClickableMenu, page);
}
