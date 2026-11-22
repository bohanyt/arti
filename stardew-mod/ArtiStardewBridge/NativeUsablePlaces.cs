using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.TerrainFeatures;
using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge;

/// <summary>Positive, current-map proofs. Remote map metadata cannot prove usable ground or water (ADR 207).</summary>
internal static class NativeUsablePlaces
{
    internal static (bool Ground, int Tilled, int Planted, int Dry, bool Fishing) Read(GameLocation loc, Farmer p)
    {
        try { return Sample(loc,p); }
        catch { return default; } // unsupported/unavailable native geometry is unknown, never a positive use fact
    }

    private static (bool Ground, int Tilled, int Planted, int Dry, bool Fishing) Sample(GameLocation loc, Farmer p)
    {
        if (!ReferenceEquals(p, Game1.player) || !ReferenceEquals(loc, Game1.currentLocation)) return default;
        // TilePoint/StandingPixel update native caches; read the same bounding-box centre without those setters (ADR 25).
        Point centre = ((Character)p).GetBoundingBox().Center;
        Point start = new(centre.X / Game1.tileSize, centre.Y / Game1.tileSize);
        // Reuse the exact native farmer collision predicate; never reposition the actor.
        var reached = new HashSet<Point> { start };
        var frontier = new List<Point> { start };   // BFS by index (no Queue<> in the bridge: OBS-2B1 guard)
        int head = 0;
        bool ground = false, fishing = false;
        int tilled = 0, planted = 0, dry = 0;
        var inspected = new HashSet<Point>();
        while (head < frontier.Count && reached.Count < 4096)
        {
            Point stand = frontier[head++];
            for (int facing = 0; facing < 4; facing++)
            {
                var (dx, dy) = FishCastGeometry.FacingDelta(facing);
                Point tile = new(stand.X + dx, stand.Y + dy);
                if (!loc.isTileOnMap(tile.X, tile.Y)) continue;
                if (loc.canFishHere() && loc.isWaterTile(tile.X, tile.Y))
                {
                    var (sx, sy) = FishCastGeometry.TileCentre(stand.X, stand.Y);
                    fishing |= FishCastGeometry.WindowFishable(sx, sy, facing,
                        (float)ReflexContracts.FishCastPowerDefault, p.FishingLevel,
                        (x,y) => loc.isTileOnMap(x,y) && loc.isTileFishable(x,y));
                }
                if (inspected.Add(tile))
                {
                    ground |= BackgroundToolTileProbe.Tillable(loc, tile.X, tile.Y);
                    if (loc.terrainFeatures.TryGetValue(new Vector2(tile.X, tile.Y), out var feature) && feature is HoeDirt dirt)
                    {
                        tilled++;
                        if (dirt.crop is not null) { planted++; if (dirt.state.Value != HoeDirt.watered) dry++; }
                    }
                }
                if (!reached.Contains(tile) && NativeRouteCell.IsWalkable(loc, p, tile.X, tile.Y))
                { reached.Add(tile); frontier.Add(tile); }
            }
            if (ground && fishing) break; // positive proofs need no full map census
        }
        return (ground, tilled, planted, dry, fishing);
    }
}
