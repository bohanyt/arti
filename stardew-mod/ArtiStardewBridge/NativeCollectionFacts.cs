using ArtiStardewBridge.Protocol;
using StardewValley;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

/// <summary>ADR 137: pure acceptance reads. Farmer.couldInventoryAcceptThisItem also posts a red HUD
/// message on refusal; observation must not call it. Mirror its slot/stack test with native canStackWith,
/// and ask GetItemReceiveBehavior whether the game takes the item without a bag slot.</summary>
internal static class NativeCollectionFacts
{
    internal static bool Accepts(Farmer player, Item item)
    {
        if (item.IsRecipe) return true;
        player.GetItemReceiveBehavior(item, out bool needsSpace, out _);
        if (!needsSpace) return true;
        for (int i = 0; i < player.MaxItems && i < player.Items.Count; i++)
        {
            Item? slot = player.Items[i];
            if (slot is null || item is SObject && slot is SObject existing
                && (long)slot.Stack + item.Stack <= slot.maximumStackSize() && existing.canStackWith(item)) return true;
        }
        return false;
    }

    internal static bool AcceptsPickup(Farmer player, GameLocation location, SObject obj)
    {
        // checkAction changes forage quality before asking acceptance. Only a detached copy is changed;
        // the game's query creates its own day/tile-seeded Random, never consumes Game1.random.
        Item received = obj.getOne();
        received.Stack = obj.Stack;
        if (obj.isForage()) received.Quality = location.GetHarvestSpawnedObjectQuality(player, true, obj.TileLocation);
        return Accepts(player, received);
    }

    internal static bool AcceptsHarvest(Farmer player, Crop crop)
    {
        string? harvest = crop.forageCrop.Value ? WorldExtras.ForageCropGrabQid(crop.whichForageCrop.Value) : crop.indexOfHarvest.Value;
        if (string.IsNullOrEmpty(harvest)) return false;
        // Same conservative quality set as the existing native crop guard, without rolling a future crop.
        if (crop.forageCrop.Value)
        {
            foreach (int quality in WorldExtras.ForageCropQualities)
                if (!Accepts(player, ItemRegistry.Create(harvest, 1, quality))) return false;
            return true;
        }
        return Accepts(player, ItemRegistry.Create(harvest));
    }

    internal static object[] Read(Farmer player, GameLocation location)
    {
        var at = player.TilePoint;
        string loc = SceneIdentity.Token(location.NameOrUniqueName ?? location.Name ?? "_");
        return location.objects.Pairs
            .Where(p => p.Value is not null && GroundPickup.NativePickup(p.Value.IsSpawnedObject, p.Value.Type)
                && Math.Max(Math.Abs(p.Key.X - at.X), Math.Abs(p.Key.Y - at.Y)) <= 12)
            .OrderBy(p => Math.Abs(p.Key.X - at.X) + Math.Abs(p.Key.Y - at.Y))
            .ThenBy(p => p.Key.X).ThenBy(p => p.Key.Y).Take(SceneObservationCaps.Objects)
            .Select(p => {
                SObject obj = p.Value;
                string? reason = GroundPickup.QuestMiss(obj.questItem.Value, obj.questId.Value,
                    obj.questId.Value is { } quest && player.hasQuest(quest));
                if (reason is null && !AcceptsPickup(player, location, obj)) reason = "inventory_cannot_accept";
                return (object)new Dictionary<string, object> {
                    ["ref"] = SceneIdentity.ObjectFactId(loc, (int)p.Key.X, (int)p.Key.Y, obj.QualifiedItemId),
                    ["item"] = obj.QualifiedItemId, ["name"] = obj.DisplayName,
                    ["tile"] = new[] { (int)p.Key.X, (int)p.Key.Y }, ["accepts"] = reason is null,
                    ["reason"] = reason ?? "",
                };
            }).ToArray();
    }
}
