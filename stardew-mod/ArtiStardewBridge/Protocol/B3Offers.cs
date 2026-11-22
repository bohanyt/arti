namespace ArtiStardewBridge.Protocol;

// W1 gap B3 (S24 crab_pot, S30 masak; owner 2026-10-19). Pure, SMAPI-free rules for the crab-pot subtype of the
// existing place_item / machine_insert / machine_collect kinds and for the faced Kitchen A. ModEntry.B3Capture.cs
// samples the game into these; NativeB3Offers.cs binds the native predicates. No input path, no executor, no
// coordinate: every tile comes from the native location. Evidence ceiling: UNIT_TESTED until the B3 live witnesses.
//
// Decomp (Stardew 1.6.15, G/ = decomp-all):
//   CrabPot.IsValidCrabPotLocationTile (StardewValley.Objects/CrabPot.cs:146-158): not Caldera / Volcano / Mine, no
//     object, the tile is water, its two horizontal OR two vertical neighbours are water, no Buildings "Passable".
//   Object.placementAction (O)710 (Object.cs:6534-6540) -> new CrabPot().placementAction: owner = who (CrabPot.cs:166).
//   pressUseToolButton (Game1.cs:11734-11760): stowing first; then checkAction at the tool tile, which for a crab pot
//     with an object in hand probes performObjectDropInAction (GameLocation.cs:7762-7782): a category -21 item while
//     NeedsBait sets bait = getOne() and reduces the active item by one (CrabPot.cs:232-252). ANY other held object
//     falls through to checkForAction, which REMOVES an unbaited pot into the bag (CrabPot.cs:304-323): never offered.
//   checkForAction collect (CrabPot.cs:270-302): only while tileIndexToShow == 714 (set by draw when heldObject != null,
//     CrabPot.cs:507-510): heldObject -> bag (x2 at 25 % with Book_Crabbing), bait = null, readyForHarvest = false,
//     the pot stays; +5 Fishing XP. DayUpdate (CrabPot.cs:325+) leaves bait set and fills heldObject overnight.
//   GameLocation.performAction "kitchen" / "Kitchen" (GameLocation.cs:8979-8982) -> ActivateKitchen
//     (GameLocation.cs:8185-8218): an async MultipleMutexRequest on the fridge (+ mini-fridges); success opens the
//     standalone CraftingPage(cooking: true); failure shows "Kitchen_InUse" (no menu = no effect).
//   FarmHouse.checkAction (StardewValley.Locations/FarmHouse.cs:696-712): tile index 173 (fridge) / 2173 run first;
//     Cabin.checkAction (Cabin.cs:106-122): indoor 647/648 (farmhand inventory) first.
public static class CrabPotRule
{
    public const string Qid = "(O)710";
    public const int BaitCategory = -21;
    /// <summary>CrabPot.tileIndexToShow of a pot whose catch A collects (CrabPot.cs:276).</summary>
    public const int ReadyTileIndex = 714;
    /// <summary>Count readback when the pinned pot is no longer the object at its tile (removed / replaced): never a
    /// success delta, never "unchanged" (BgExtrasEffect classifies it EXTRAS_EFFECT_MISMATCH).</summary>
    public const int VanishedCount = -1_000_000;
    public const string Water = "water", Pot = "pot";
    public const int WatersMax = 6, PotsMax = 4;

    /// <summary>
    /// #322: the bounded ui.context catalog order. The actor's own pots come first (the S24 pin is always an own pot),
    /// then foreign pots, each group by tile (Y, X), capped at <see cref="PotsMax"/>. Foreign pots that sort earlier can
    /// no longer push an own pot out; the totals (<see cref="CatalogMeta"/>) still tell the skill when own pots exceed
    /// the cap, so an omission is never read as absence.
    /// </summary>
    public static IReadOnlyList<T> CatalogOrder<T>(IEnumerable<T> pots, Func<T, bool> own, Func<T, int> x, Func<T, int> y) =>
        pots.OrderBy(p => own(p) ? 0 : 1).ThenBy(y).ThenBy(x).Take(PotsMax).ToList();

    /// <summary>#322 completeness metadata published beside "crab_pots" (additive keys; older readers ignore them).</summary>
    public static Dictionary<string, object> CatalogMeta(int total, int ownTotal, int listed, int ownListed) => new()
    {
        ["crab_pots_total"] = total, ["crab_pots_own_total"] = ownTotal,
        ["crab_pots_truncated"] = listed < total, ["crab_pots_own_complete"] = ownListed == ownTotal,
    };

    public static readonly string[] CatalogMetaKeys =
        { "crab_pots_total", "crab_pots_own_total", "crab_pots_truncated", "crab_pots_own_complete" };

    /// <summary>The A collect admission: only the native collect branch can run (no removal branch).</summary>
    public static bool CollectReady(int tileIndexToShow, bool hasCatch, bool readyForHarvest) =>
        tileIndexToShow == ReadyTileIndex && hasCatch && readyForHarvest;

    /// <summary>The X bait admission: a bait-category object, a pot that needs bait and holds no catch, the native probe.</summary>
    public static bool BaitAccepted(int heldCategory, bool needsBait, bool hasCatch, bool readyForHarvest, bool nativeProbe) =>
        heldCategory == BaitCategory && needsBait && !hasCatch && !readyForHarvest && nativeProbe;

    /// <summary>The X placement admission: the held item is a crab pot and BOTH native predicates pass at the tool tile.</summary>
    public static bool PlaceAdmitted(string? heldQid, bool validCrabPotTile, bool playerCanPlaceHere) =>
        heldQid == Qid && validCrabPotTile && playerCanPlaceHere;

    /// <summary>Stable native pot identity across days: location, tile and owner (no object reference survives a save).</summary>
    public static string PotId(string location, int x, int y, long owner) =>
        UiKeyTargetToken.Hash("crab_pot|" + location + "|" + x + "|" + y + "|" + owner)[..12];

    /// <summary>The published state of one pot: "ready" (A collects), "catch" (a catch not yet collectable: not drawn
    /// yet), "baited" (waiting for the night), "empty" (needs bait).</summary>
    public static string State(bool needsBait, bool hasCatch, bool collectReady) =>
        collectReady ? "ready" : hasCatch ? "catch" : needsBait ? "empty" : "baited";

    /// <summary>The strict subtype readback applies (the crab-pot captures set ProductQuantity; other machines do not).</summary>
    public static bool Strict(BgExtraFacts before) =>
        before.ProductQuantity is int && before.Kind is WorldExtras.PlaceItem or WorldExtras.MachineInsert or WorldExtras.MachineCollect;

    /// <summary>Exact identity / count readback: same item and quality, the pinned pot reached its done state from not
    /// done, and the bag moved by exactly the native quantity (place / bait -1; collect +stack, or 2 x stack only when
    /// the Book_Crabbing doubling is possible: Value = 2).</summary>
    public static bool Succeeded(BgExtraFacts b, BgExtraFacts n)
    {
        if (b.ProductQuantity is not int q || q <= 0 || !n.Done || b.Done || n.ItemId != b.ItemId || n.ItemId.Length == 0
            || n.ProductQuality != b.ProductQuality || n.ProductQuantity != q || n.Count == VanishedCount || b.Count == VanishedCount)
            return false;
        int delta = n.Count - b.Count;
        return n.Kind == WorldExtras.MachineCollect ? delta == q || (b.Value >= 2 && delta == 2 * q) : delta == -q;
    }

    /// <summary>Anything but an exact success or a fully unchanged readback is a mismatch (never a silent no-effect).</summary>
    public static bool Unchanged(BgExtraFacts b, BgExtraFacts n) =>
        n.Done == b.Done && n.Count == b.Count && n.ItemId == b.ItemId && n.Count != VanishedCount;
}

/// <summary>S30: the faced Kitchen A (GameLocation.performAction "kitchen"), pinned to the actor's own home.</summary>
public static class KitchenRule
{
    /// <summary>FarmHouse.checkAction / Cabin.checkAction tile indices that run before the Buildings action.</summary>
    public const int FridgeTile = 173, ParrotTile = 2173, CabinInventoryA = 647, CabinInventoryB = 648;
    public const int TargetsMax = 3;

    /// <summary>The full Buildings Action value is exactly the vanilla kitchen action (no arguments, no wildcard).</summary>
    public static bool IsKitchenAction(string? action) => action is "kitchen" or "Kitchen";

    /// <summary>null = admitted; else why the faced tile is not the actor's own kitchen.</summary>
    public static string? Refusal(string? action, bool farmHouse, bool ownedByActor, bool outdoors, int buildingsIndex,
        int indoorIndex, bool cabin)
    {
        if (!IsKitchenAction(action))
            return "not_kitchen";
        if (!farmHouse || outdoors)
            return "kitchen_not_in_home";
        if (!ownedByActor)
            return "kitchen_not_owned";
        if (buildingsIndex is FridgeTile or ParrotTile || (cabin && indoorIndex is CabinInventoryA or CabinInventoryB))
            return "kitchen_tile_taken";
        return null;
    }
}
