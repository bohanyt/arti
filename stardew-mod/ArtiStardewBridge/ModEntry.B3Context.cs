using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Locations;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

// W1 gap B3: the read-only ui.context catalog for S24 / S30 and the two config reads the faced captures use. Not linked
// into ProtocolTests (it needs the Extras stand rule and the economy item policy); the captures it feeds are.
public sealed partial class ModEntry
{
    bool B3Stowing() => Game1.options?.allowStowing == true;

    string? B3InsertPolicy(SObject held) => WorldExtrasPolicy.InsertPolicyRefusal(EconItemOf(held, 1f), this.config.EconomyItemPolicy,
        this.config.ExtrasMachineInputAllowlist, this.config.ExtrasMachineInputPriceCap);

    /// <summary>
    /// ui.context "crab_pots" (crab pots in the location, any distance: the S24 collect leg starts from the cabin; #322:
    /// own pots first, capped, with "crab_pots_total" / "crab_pots_own_total" / "crab_pots_truncated" /
    /// "crab_pots_own_complete" so an omitted pot is never read as a missing one),
    /// "crab_water" (native-valid crab-pot water tiles with a walkable stand, only while the bag holds a crab pot) and
    /// kitchen entries appended to "extras_targets" (the actor's own home). Pots / waters are sorted by tile and the
    /// water scan is the block-aligned nav window, so walking does not churn the ui.context revision. The faced capture
    /// stays authoritative; these entries only tell the skill where to walk.
    /// </summary>
    void EnrichB3Targets(Dictionary<string, object> snapshot)
    {
        if (!this.ExtrasEnabled || !Context.IsWorldReady || Game1.player is not Farmer player
            || player.currentLocation is not GameLocation location)
        {
            snapshot.Remove("crab_pots");
            snapshot.Remove("crab_water");
            foreach (string key in CrabPotRule.CatalogMetaKeys)
                snapshot.Remove(key);
            return;
        }
        try
        {
            string loc = SceneIdentity.Token(location.NameOrUniqueName ?? location.Name ?? "_");
            var pots = new List<Dictionary<string, object>>();
            var all = location.objects.Pairs.Where(p => p.Value is StardewValley.Objects.CrabPot)
                .Select(p => (Tile: p.Key, Pot: (StardewValley.Objects.CrabPot)p.Value)).ToList();
            bool Own((Vector2 Tile, StardewValley.Objects.CrabPot Pot) e) => e.Pot.owner.Value == player.UniqueMultiplayerID;
            var listed = CrabPotRule.CatalogOrder(all, Own, e => (int)e.Tile.X, e => (int)e.Tile.Y);
            foreach (var (tile, pot) in listed)
            {
                int x = (int)tile.X, y = (int)tile.Y;
                SObject? caught = pot.heldObject.Value;
                bool ready = NativeB3Offers.CollectReady(pot);
                var (stands, _) = ExtrasStandTiles(location, WorldExtras.MachineCollect, x, y);
                pots.Add(new Dictionary<string, object>
                {
                    ["ref"] = "xt:crabpot:" + loc + ":" + x + "," + y, ["x"] = x, ["y"] = y,
                    ["pot_id"] = NativeB3Offers.PotId(location, tile, pot),
                    ["own"] = pot.owner.Value == player.UniqueMultiplayerID,
                    ["state"] = CrabPotRule.State(NativeB3Offers.NeedsBait(pot, player), caught is not null, ready),
                    ["bait"] = pot.bait.Value?.QualifiedItemId ?? "",
                    ["catch"] = caught?.QualifiedItemId ?? "", ["catch_quality"] = caught?.Quality ?? 0,
                    ["catch_stack"] = caught?.Stack ?? 0, ["doubling"] = NativeB3Offers.CrabbingDoubling(player),
                    ["stands"] = stands,
                });
            }
            snapshot["crab_pots"] = pots.Select(p => (object)p).ToArray();
            foreach (var (key, value) in CrabPotRule.CatalogMeta(all.Count, all.Count(Own), listed.Count, listed.Count(Own)))
                snapshot[key] = value;
            var waters = new List<Dictionary<string, object>>();
            if (player.Items.Any(i => i is not null && i.QualifiedItemId == CrabPotRule.Qid))
            {
                int x0 = WorldExtras.NavGridOrigin((int)player.Tile.X), y0 = WorldExtras.NavGridOrigin((int)player.Tile.Y);
                for (int y = y0; y < y0 + WorldExtras.NavGridSize && waters.Count < CrabPotRule.WatersMax; y++)
                    for (int x = x0; x < x0 + WorldExtras.NavGridSize && waters.Count < CrabPotRule.WatersMax; x++)
                    {
                        if (!location.isTileOnMap(new Vector2(x, y)) || !NativeB3Offers.ValidPotTile(location, x, y))
                            continue;
                        var (stands, _) = ExtrasStandTiles(location, WorldExtras.PlaceItem, x, y);
                        if (stands.Length > 0)
                            waters.Add(new Dictionary<string, object>
                            {
                                ["ref"] = "xt:crabwater:" + loc + ":" + x + "," + y, ["x"] = x, ["y"] = y, ["stands"] = stands,
                            });
                    }
            }
            snapshot["crab_water"] = waters.Select(w => (object)w).ToArray();
            if (location is FarmHouse && snapshot.GetValueOrDefault("extras_targets") is object[] targets)
            {
                Point at = player.TilePoint;
                var kitchens = new List<object>();
                for (int y = at.Y - ExtrasTargetRadius; y <= at.Y + ExtrasTargetRadius && kitchens.Count < KitchenRule.TargetsMax; y++)
                    for (int x = at.X - ExtrasTargetRadius; x <= at.X + ExtrasTargetRadius && kitchens.Count < KitchenRule.TargetsMax; x++)
                    {
                        if (!location.isTileOnMap(new Vector2(x, y)) || NativeB3Offers.KitchenMiss(location, player, new Point(x, y)) is not null)
                            continue;
                        var (stands, misses) = ExtrasStandTiles(location, WorldExtras.Kitchen, x, y);
                        var entry = new Dictionary<string, object>
                        {
                            ["kind"] = WorldExtras.Kitchen, ["ref"] = ExtrasSource(WorldExtras.Kitchen, location, new Vector2(x, y), "kitchen"),
                            ["x"] = x, ["y"] = y, ["label"] = "Kitchen", ["ready"] = !NativeB3Offers.KitchenBusy(location), ["stands"] = stands,
                        };
                        if (misses.Length > 0)
                            entry["stand_misses"] = misses;
                        kitchens.Add(entry);
                    }
                if (kitchens.Count > 0)
                    snapshot["extras_targets"] = targets.Concat(kitchens).ToArray();
            }
        }
        catch (Exception)
        {
            snapshot.Remove("crab_pots");
            snapshot.Remove("crab_water");
            foreach (string key in CrabPotRule.CatalogMetaKeys)
                snapshot.Remove(key);
        }
    }
}
