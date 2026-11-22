using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Objects;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

// W1 gap B3 (S24 crab_pot, S30 masak). Faced-capture subtypes of the EXISTING world_interact kinds, on the existing pad
// A / X retained-press hold: place_item (X, held crab pot), machine_insert (X, held bait into an empty pot),
// machine_collect (A, a native-ready pot) and the new receiver kind kitchen (A, the actor's own kitchen). Each capture
// is the native predicate + the explicit WorldExtras guard list + a pinned exact readback (Protocol/B3Offers.cs);
// pre-press re-capture goes through ExtrasCapture's StillPresent as for every extras kind. Linked into ProtocolTests
// against offline doubles. Evidence ceiling: UNIT_TESTED until the B3 live witnesses.
public sealed partial class ModEntry
{
    /// <summary>X with a held object at the tool tile: a held crab pot -> placement; a crab pot at the tool tile -> bait
    /// (any held object there is captured, so a non-bait object is refused instead of reaching the removal branch).
    /// null = not a crab-pot target (the existing machine / placement captures continue).</summary>
    InteractKindCapture? CaptureCrabPotX(Farmer player, GameLocation location, Vector2 xTile, SObject held, ExtrasGuardFacts facts)
    {
        if (NativeB3Offers.PotAt(location, xTile) is { } pot)
            return this.CaptureCrabPotBait(player, location, xTile, pot, held, facts);
        return held.QualifiedItemId == CrabPotRule.Qid ? this.CaptureCrabPotPlace(player, location, xTile, held, facts) : null;
    }

    InteractKindCapture CaptureCrabPotPlace(Farmer player, GameLocation location, Vector2 tile, SObject held, ExtrasGuardFacts facts)
    {
        Rectangle rect = TileRect(tile);
        facts = facts with
        {
            PlaceableAllowlisted = true,
            OccupantAtTile = ExtrasOccupant(player, location, rect, null) || ExtrasAnimalAt(location, rect, null),
            ObjectAtTile = location.objects.ContainsKey(tile),
            FurnitureAtTile = ExtrasFurnitureAt(location, rect),
            HarvestableAtTile = false,
            // pressUseToolButton stows a non-big-craftable object BEFORE placing it (Game1.cs:11734): never offered then.
            StowHazard = this.B3Stowing() && !held.bigCraftable.Value,
            PlacementValid = NativeB3Offers.CanPlacePot(location, player, held, tile),
        };
        long owner = player.UniqueMultiplayerID;
        GameLocation pinnedLocation = location;
        Vector2 pinnedTile = tile;
        return this.ExtrasCapture(WorldExtras.PlaceItem, player, location, tile, facts, location, CrabPotRule.Qid, null,
            () => new BgExtraFacts(WorldExtras.PlaceItem,
                NativeB3Offers.PotAt(pinnedLocation, pinnedTile) is { } placed && placed.owner.Value == owner
                    && placed.bait.Value is null && placed.heldObject.Value is null,
                Game1.player is Farmer p && ReferenceEquals(p, player) ? InventoryCount(p, CrabPotRule.Qid) : CrabPotRule.VanishedCount,
                0, CrabPotRule.Qid, ProductQuality: 0, ProductQuantity: 1));
    }

    InteractKindCapture CaptureCrabPotBait(Farmer player, GameLocation location, Vector2 tile, CrabPot pot, SObject held,
        ExtrasGuardFacts facts)
    {
        Rectangle rect = TileRect(tile);
        bool needsBait = NativeB3Offers.NeedsBait(pot, player);
        bool hasCatch = pot.heldObject.Value is not null;
        bool probe = held.Category == CrabPotRule.BaitCategory && needsBait && !hasCatch && NativeB3Offers.BaitProbe(pot, held, player);
        facts = facts with
        {
            OccupantAtTile = ExtrasOccupant(player, location, rect, null) || ExtrasAnimalAt(location, rect, null),
            FurnitureAtTile = ExtrasFurnitureAt(location, rect),
            MachineAllowlisted = true,
            MachineBusy = !needsBait || hasCatch || pot.readyForHarvest.Value,
            StowHazard = this.B3Stowing() && !held.bigCraftable.Value,
            InputAccepted = CrabPotRule.BaitAccepted(held.Category, needsBait, hasCatch, pot.readyForHarvest.Value, probe),
            InputPolicyDetail = this.B3InsertPolicy(held),
        };
        string baitQid = held.QualifiedItemId ?? "";
        long owner = player.UniqueMultiplayerID;
        CrabPot pinned = pot;
        GameLocation pinnedLocation = location;
        Vector2 pinnedTile = tile;
        return this.ExtrasCapture(WorldExtras.MachineInsert, player, location, tile, facts, pot, CrabPotRule.Qid, null,
            () =>
            {
                bool same = ReferenceEquals(NativeB3Offers.PotAt(pinnedLocation, pinnedTile), pinned);
                SObject? bait = pinned.bait.Value;
                return new BgExtraFacts(WorldExtras.MachineInsert,
                    same && bait is not null && bait.QualifiedItemId == baitQid && bait.Stack == 1 && pinned.owner.Value == owner,
                    same && Game1.player is Farmer p && ReferenceEquals(p, player) ? InventoryCount(p, baitQid) : CrabPotRule.VanishedCount,
                    0, baitQid, ProductQuality: 0, ProductQuantity: 1);
            }, instancePin: () => ReferenceEquals(NativeB3Offers.PotAt(pinnedLocation, pinnedTile), pinned));
    }

    /// <summary>A on a pot: only the native collect branch (tileIndexToShow 714 + catch) is ever offered; an empty pot's
    /// A would pick the pot up (CrabPot.cs:304-323), so it is a guarded miss, never a target.</summary>
    InteractKindCapture CaptureCrabPotCollect(Farmer player, GameLocation location, Vector2 tile, CrabPot pot, ExtrasGuardFacts facts)
    {
        SObject? caught = pot.heldObject.Value;
        bool ready = NativeB3Offers.CollectReady(pot);
        facts = facts with
        {
            OccupantAtTile = false,
            BoxedIn = false, // CrabPot.checkForAction has no boxed-in rule
            MachineAllowlisted = true,
            TargetReady = ready,
            InventoryAccepts = caught is not null && player.couldInventoryAcceptThisItem(caught),
            ProfessionDetail = ready ? this.ExtrasProfession(player, Farmer.fishingSkill, 5) : null, // gainExperience(1, 5)
        };
        string outQid = caught?.QualifiedItemId ?? "";
        int quality = caught?.Quality ?? 0, stack = caught?.Stack ?? 0;
        int doubling = NativeB3Offers.CrabbingDoubling(player) ? 2 : 1;
        CrabPot pinned = pot;
        GameLocation pinnedLocation = location;
        Vector2 pinnedTile = tile;
        return this.ExtrasCapture(WorldExtras.MachineCollect, player, location, tile, facts, pot, CrabPotRule.Qid, null,
            () =>
            {
                bool same = ReferenceEquals(NativeB3Offers.PotAt(pinnedLocation, pinnedTile), pinned);
                return new BgExtraFacts(WorldExtras.MachineCollect,
                    same && pinned.heldObject.Value is null && !pinned.readyForHarvest.Value,
                    same && Game1.player is Farmer p && ReferenceEquals(p, player) ? B3CountQuality(p, outQid, quality) : CrabPotRule.VanishedCount,
                    doubling, outQid, ProductQuality: quality, ProductQuantity: stack);
            }, instancePin: () => ReferenceEquals(NativeB3Offers.PotAt(pinnedLocation, pinnedTile), pinned));
    }

    /// <summary>The faced Kitchen A of the actor's own home (GameLocation.performAction "kitchen" -> ActivateKitchen). A
    /// locked fridge mutex is FacilityUnavailable; the receiver is only the standalone cooking CraftingPage.</summary>
    InteractKindCapture? CaptureKitchen(Farmer player, GameLocation location, Vector2 tile, ExtrasGuardFacts facts)
    {
        var point = new Point((int)tile.X, (int)tile.Y);
        if (!KitchenRule.IsKitchenAction(location.doesTileHaveProperty(point.X, point.Y, "Action", "Buildings")))
            return null;
        string? miss = NativeB3Offers.KitchenMiss(location, player, point);
        var identity = this.CurrentCommandIdentity();
        return this.ExtrasCapture(WorldExtras.Kitchen, player, location, tile,
            facts with
            {
                FacilityUnavailable = miss is not null || NativeB3Offers.KitchenBusy(location),
                OccupantAtTile = this.InteractOccupantMiss(player, location, TileRect(tile), tile, true) is not null,
            },
            location, "kitchen", null, () => new BgExtraFacts(WorldExtras.Kitchen, false, 0),
            menu => ReferenceEquals(Game1.player, player) && ReferenceEquals(player.currentLocation, location)
                && this.CurrentCommandIdentity() with { ContextGeneration = identity.ContextGeneration } == identity
                && NativeB3Offers.CookingReceiver(menu));
    }

    static int B3CountQuality(Farmer player, string qid, int quality) =>
        qid.Length == 0 ? 0 : player.Items.Where(i => i is SObject o && o.QualifiedItemId == qid && o.Quality == quality).Sum(i => i!.Stack);
}
