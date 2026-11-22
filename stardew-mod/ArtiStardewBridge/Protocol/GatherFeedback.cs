using System.Text.Json;
using System.Text.Json.Serialization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// WS1: one tile of the 3x3 gather window with the game's own tool predicates (read-only):
/// tillable (Hoe.DoFunction + makeHoeDirt), water_refill (CanRefillWateringCanOnTile),
/// villager (a villager overlaps the tile). Empty tiles and water are not scene facts.
/// </summary>
public sealed record GatherTileFact
{
    [JsonPropertyName("x")] public int X { get; init; }
    [JsonPropertyName("y")] public int Y { get; init; }
    [JsonPropertyName("tillable")] public bool Tillable { get; init; }
    [JsonPropertyName("water_refill")] public bool WaterRefill { get; init; }
    [JsonPropertyName("villager")] public bool Villager { get; init; }
}

/// <summary>One bounded current scene fact; no input, loot map or outcome oracle.</summary>
public static class GatherFeedback
{
    public const string FactId = "gather:nearby";
    // Scan bounds over the whole location's debris (live 2026-10-07: several felled trees leave
    // wood, sap and seeds on the Farm for days). Output stays capped at MaxDrops nearest drops.
    public const int MaxGroups = 512;
    public const int MaxChunks = 1024;
    public const int MaxDrops = 16;

    /// <summary>
    /// The MaxDrops drops nearest to (centerX, centerY) in pixels, stable by input order on ties;
    /// truncated = more candidates than MaxDrops. Live 2026-10-07 (M1 attempt 10z8): a felled tree
    /// scattered more than 16 visible drops and the whole gather window became unavailable, which
    /// also emptied the approach catalog. A truncated list is reported as drops_complete=false.
    /// </summary>
    public static List<T> NearestDrops<T>(IReadOnlyList<(float X, float Y, T Value)> candidates,
        float centerX, float centerY, out bool truncated)
    {
        truncated = candidates.Count > MaxDrops;
        return candidates
            .Select((c, i) => (c, i, d: (c.X - centerX) * (c.X - centerX) + (c.Y - centerY) * (c.Y - centerY)))
            .OrderBy(t => t.d).ThenBy(t => t.i)
            .Take(MaxDrops).OrderBy(t => t.i).Select(t => t.c.Value).ToList();
    }
    public const int MaxBytes = 12288;
    public const int MaxTiles = 9;
    public const int RefillRadius = 28;
    public const int MaxRefillSources = 4;

    /// <summary>Bounded read-only refill sightings, independent of fishing gear/cast geometry.
    /// A sighting proposes travel only; the adjacent native tool gate still authorizes use.</summary>
    public static IReadOnlyList<TileXY> FindRefillSources(int x, int y,
        IReadOnlyList<TileXY> refillTiles, Func<int, int, bool> standable,
        Func<int, int, bool> sourceClear)
    {
        // Keep only four candidates in distance/Y/X order; no map probes, full sort or
        // per-water-tile allocations on warm snapshots. Range stays Chebyshev, as on wire.
        var found = new List<TileXY>(MaxRefillSources);
        int Compare(TileXY a, TileXY b)
        {
            int distance = (Math.Abs(a.X - x) + Math.Abs(a.Y - y))
                .CompareTo(Math.Abs(b.X - x) + Math.Abs(b.Y - y));
            return distance != 0 ? distance : a.Y != b.Y ? a.Y.CompareTo(b.Y) : a.X.CompareTo(b.X);
        }
        foreach (var tile in refillTiles)
        {
            if (Math.Abs(tile.X - x) > RefillRadius || Math.Abs(tile.Y - y) > RefillRadius)
                continue;
            int index = 0;
            while (index < found.Count && Compare(found[index], tile) <= 0) index++;
            if (index == MaxRefillSources || !sourceClear(tile.X, tile.Y)
                || !(standable(tile.X - 1, tile.Y) || standable(tile.X + 1, tile.Y)
                    || standable(tile.X, tile.Y - 1) || standable(tile.X, tile.Y + 1)))
                continue;
            if (found.Count == MaxRefillSources) found.RemoveAt(MaxRefillSources - 1);
            found.Insert(index, tile);
        }
        return found;
    }
    /// <summary>
    /// tool_tile may sit TWO tiles away (facing left/right inside the outer 8 px band of the
    /// standing tile); the runtime then reports it as not adjacent instead of losing the window.
    /// </summary>
    public const int ToolTileRadius = 2;

    public static Dictionary<string, object?> Unavailable(string reason) => new()
    {
        ["kind"] = "gather_window", ["available"] = false, ["reason"] = reason,
    };

    public static Dictionary<string, object?> Build(string location, int tick, TileXY center,
        IReadOnlyList<ScenePreparedFact> targets, IReadOnlyList<object> drops,
        bool dropsComplete, bool usingTool, UseToolInputVerdict? lastUse,
        TileXY? toolTile = null, IReadOnlyList<GatherTileFact>? tiles = null,
        IReadOnlyList<TileXY>? refillSources = null)
    {
        if (targets.Count > 18 || drops.Count > MaxDrops)
            return Unavailable("output_limit");
        // Review fix (WS1 minor 6): an out-of-range hit tile only drops the optional key; the window
        // itself (the M1 axe evidence) stays available.
        if (toolTile is not null && (Math.Abs(toolTile.X - center.X) > ToolTileRadius || Math.Abs(toolTile.Y - center.Y) > ToolTileRadius))
            toolTile = null;
        if (tiles is not null && (tiles.Count > MaxTiles
                || tiles.Any(t => t is null || Math.Abs(t.X - center.X) > 1 || Math.Abs(t.Y - center.Y) > 1)
                || tiles.Select(t => (t.X, t.Y)).Distinct().Count() != tiles.Count))
            return Unavailable("tiles_invalid");
        if (refillSources is not null && (refillSources.Count > MaxRefillSources
                || refillSources.Any(t => t is null || t.X < 0 || t.Y < 0
                    || Math.Abs(t.X - center.X) > RefillRadius || Math.Abs(t.Y - center.Y) > RefillRadius)
                || refillSources.Select(t => (t.X, t.Y)).Distinct().Count() != refillSources.Count))
            return Unavailable("refill_sources_invalid");
        var facts = new Dictionary<string, object>();
        foreach (var target in targets)
        {
            TileXY? tile = target.Dto switch {
                SceneObjectFact o => o.Tile, SceneTerrainFact t => t.Tile, _ => null };
            if (tile is null || Math.Abs(tile.X - center.X) > 1 || Math.Abs(tile.Y - center.Y) > 1
                || !facts.TryAdd(target.Id, target.Dto)) return Unavailable("invalid_target");
        }
        Dictionary<string, object?> result = new()
        {
            ["kind"] = "gather_window", ["available"] = true, ["reason"] = null,
            ["location_id"] = location, ["captured_ticks"] = tick, ["center"] = center,
            ["radius"] = 1, ["targets"] = facts, ["drops"] = drops,
            ["drops_complete"] = dropsComplete, ["using_tool"] = usingTool,
            ["last_use"] = lastUse is null ? null : new Dictionary<string, object?> {
                ["request_id"] = lastUse.RequestId, ["cycle_observed"] = lastUse.CycleObserved,
                ["input_completed"] = lastUse.Outcome == "InputCompleted",
                ["cleanup_confirmed"] = lastUse.CleanupConfirmed },
        };
        // WS1 optional keys: absent when not captured (older consumers ignore unknown keys).
        if (toolTile is not null)
            result["tool_tile"] = new TileXY { X = toolTile.X, Y = toolTile.Y };
        if (tiles is not null)
            result["tiles"] = tiles.ToList();
        if (refillSources is not null)
            result["refill_sources"] = refillSources.ToList();
        return JsonSerializer.SerializeToUtf8Bytes(result, ProtocolJson.Options).Length <= MaxBytes
            ? result : Unavailable("payload_limit");
    }
}
