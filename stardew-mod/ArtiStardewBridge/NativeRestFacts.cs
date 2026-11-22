using StardewValley;
using StardewValley.Locations;
using StardewValley.Objects;
using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge;

/// <summary>Ownership and clock observations only. Never a route or input authorization (ADR-51).</summary>
internal static class NativeRestFacts
{
    internal static Dictionary<string, object?> Capture(Farmer actor, IEnumerable<GameLocation> locations,
        GameLocation current, int clock)
    {
        // homeLocation alone can name the host's house. Use native ownership,
        // as scene bed capture and the sleep crossing do, including custom cabins.
        var homes = locations.OfType<FarmHouse>().Where(h => h.OwnerId == actor.UniqueMultiplayerID).ToArray();
        var ownBeds = homes.Select(h => new {
            location = SceneIdentity.Token(h.NameOrUniqueName),
            beds = h.furniture.OfType<BedFurniture>().Where(IsAdult).Select(b => new[] {
                (int)b.TileLocation.X, (int)b.TileLocation.Y }).ToArray()
        }).ToArray();
        // fix42 (a33 d7:J232): the host's house was a bare "@FarmHouse" travel arg and was read as her home on
        // a refill trip. The same native ownership names every other player's house.
        var others = locations.OfType<FarmHouse>().Where(h => h.OwnerId != actor.UniqueMultiplayerID)
            .Select(h => SceneIdentity.Token(h.NameOrUniqueName)).Distinct().ToArray();
        bool owned = current is FarmHouse house && house.OwnerId == actor.UniqueMultiplayerID;
        bool bed = current.furniture.OfType<BedFurniture>().Any(IsAdult);
        return new() {
            ["home"] = homes.Length == 1 ? SceneIdentity.Token(homes[0].NameOrUniqueName) : null,
            ["other_homes"] = others,
            ["own_beds"] = ownBeds,
            ["home_unknown"] = homes.Length == 0 ? "native owned home unavailable" : homes.Length > 1 ? "multiple native owned homes" : null,
            ["current_location"] = SceneIdentity.Token(current.NameOrUniqueName),
            ["current_bed_usable_by_actor"] = owned && bed,
            ["current_bed_reason"] = !bed ? "no adult bed observed" : !owned ? "location is not owned by actor" : "adult bed in actor-owned home",
            ["use_basis"] = "ownership eligibility; approach and input still require current native affordances",
            ["time_of_day"] = clock,
            ["pass_out_at"] = 2600,
            ["minutes_until_pass_out"] = clock >= 600 && clock <= 2600 && clock % 100 < 60
                ? Math.Max(0, 26 * 60 - (clock / 100 * 60 + clock % 100)) : (int?)null
        };
    }

    static bool IsAdult(BedFurniture bed) => bed.bedType != BedFurniture.BedType.Child;
}
