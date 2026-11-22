using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Tools;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

/// <summary>
/// WS1 (primitives v1) game-coupled, READ-ONLY tool facts for the background TOOL hold
/// and the gather window. Game thread only. No input, no world write, no map scan:
/// dictionary lookups and the game's own tile predicates for at most nine tiles.
/// Shared by <see cref="BackgroundMoveRuntime.TryArmHold"/> (faced tool-tile guard) and
/// SceneSnapshotCapture.CaptureGather (tool_tile / tiles facts), so the guard and the
/// observation use one predicate. Evidence ceiling: UNIT_TESTED + source reading of the
/// decompiled 1.6.15 game; HYPOTHESIS until live.
/// </summary>
internal static class BackgroundToolTileProbe
{
    /// <summary>Classification facts of the held tool, or null without a tool / on a read fault.</summary>
    public static BgToolFacts? SampleTool(Farmer? player)
    {
        try
        {
            if (player?.CurrentTool is not Tool tool)
                return null;
            bool bottomless = tool is WateringCan can && can.IsBottomless;
            return new BgToolFacts(tool.GetType().FullName, tool.QualifiedItemId, tool.UpgradeLevel,
                tool.enchantments.Count, bottomless);
        }
        catch
        {
            return null;
        }
    }

    /// <summary>
    /// Exact tool hit tile: floor(Character.GetToolLocation(ignoreClick: true) / 64). With the
    /// cursor beyond radius 1 (cursor-inert at admission and every hook entry) the press uses the
    /// same facing-based point (decompiled Game1.pressUseToolButton / Character.GetToolLocation).
    /// </summary>
    public static (Vector2 Point, int X, int Y)? ToolTile(Farmer? player)
    {
        try
        {
            if (player is null)
                return null;
            Vector2 point = player.GetToolLocation(true);
            return (point, BgToolTileGuard.FloorDiv64((int)Math.Floor(point.X)), BgToolTileGuard.FloorDiv64((int)Math.Floor(point.Y)));
        }
        catch
        {
            return null;
        }
    }

    /// <summary>
    /// Hoe.DoFunction + GameLocation.makeHoeDirt predicate, minus the MineShaft branch (a mine
    /// level is never tillable here): Diggable Back tile, no object, no terrain feature, passable,
    /// not blocked by anything but characters/farmers.
    /// </summary>
    public static bool Tillable(GameLocation location, int x, int y)
    {
        if (location is StardewValley.Locations.MineShaft)
            return false;
        Vector2 tile = new(x, y);
        return location.doesTileHaveProperty(x, y, "Diggable", "Back") != null
            && !location.objects.ContainsKey(tile)
            && !location.terrainFeatures.ContainsKey(tile)
            && location.isTilePassable(new xTile.Dimensions.Location(x, y), Game1.viewport)
            && !location.IsTileBlockedBy(tile, ~(CollisionMask.Characters | CollisionMask.Farmers));
    }

    /// <summary>The game's own refill predicate (water tile, WaterSource, well building, kitchen sink).</summary>
    public static bool WaterRefill(GameLocation location, int x, int y) => location.CanRefillWateringCanOnTile(x, y);

    /// <summary>Same expression as Game1.pressUseToolButton's villager redirect (talk instead of tool).</summary>
    public static bool VillagerAtPoint(GameLocation location, Vector2 point) =>
        location.doesPositionCollideWithCharacter(Utility.getRectangleCenteredAt(point, 64), ignoreMonsters: true)?.IsVillager ?? false;

    /// <summary>A villager whose bounding box overlaps the tile.</summary>
    public static bool VillagerOnTile(GameLocation location, int x, int y) =>
        location.doesPositionCollideWithCharacter(new Rectangle(x * 64, y * 64, 64, 64), ignoreMonsters: true)?.IsVillager ?? false;

    /// <summary>
    /// Hook-entry re-check (review fix, WS1 minor 3): a villager at the current hit point.
    /// A read fault counts as a villager (fail closed).
    /// </summary>
    public static bool VillagerAtToolPoint(Farmer? player)
    {
        try
        {
            GameLocation? location = player?.currentLocation;
            return location is not null && ToolTile(player) is { } hit && VillagerAtPoint(location, hit.Point);
        }
        catch
        {
            return true;
        }
    }

    /// <summary>
    /// Faced tool-tile facts for the guard, or null (unreadable => refused). Review fix (WS1 minor 5):
    /// only the predicates the held kind needs are evaluated, so a fault in an irrelevant predicate
    /// (e.g. the refill building scan for the Axe) can never refuse the proven path.
    /// </summary>
    public static BgToolTileFacts? SampleTileFacts(Farmer? player, BgToolKind kind, bool furnitureAtToolTile = false)
    {
        GameLocation? location = player?.currentLocation;
        if (player is null || location is null || ToolTile(player) is not { } hit)
            return null;
        try
        {
            Vector2 tile = new(hit.X, hit.Y);
            location.objects.TryGetValue(tile, out SObject? obj);
            location.terrainFeatures.TryGetValue(tile, out var terrain);
            int? waterLeft = null, waterMax = null;
            bool tillable = false, refill = false;
            IReadOnlyList<BgAreaTileFacts>? area = null;
            Vector2 playerTile = player.Tile;
            switch (kind)
            {
                case BgToolKind.Hoe:
                    tillable = Tillable(location, hit.X, hit.Y);
                    break;
                case BgToolKind.WateringCan:
                    if (player.CurrentTool is WateringCan can)
                    {
                        waterLeft = can.WaterLeft;
                        waterMax = can.waterCanMax;
                    }
                    refill = WaterRefill(location, hit.X, hit.Y);
                    break;
                case BgToolKind.Scythe:
                    area = ScytheArea(location, (int)playerTile.X, (int)playerTile.Y, player.FacingDirection);
                    break;
            }
            string qid = obj?.QualifiedItemId ?? "";
            return new BgToolTileFacts(
                PlayerTileX: (int)playerTile.X,
                PlayerTileY: (int)playerTile.Y,
                Facing: player.FacingDirection,
                ToolTileX: hit.X,
                ToolTileY: hit.Y,
                VillagerAtToolTile: VillagerAtPoint(location, hit.Point),
                HasObject: obj is not null,
                ObjectQualifiedItemId: obj?.QualifiedItemId,
                ObjectName: obj?.Name,
                ObjectType: obj?.Type,
                ObjectBigCraftable: obj?.bigCraftable.Value == true,
                ObjectIsBreakableStone: obj?.IsBreakableStone() == true,
                ObjectIsWeeds: obj?.IsWeeds() == true,
                ObjectIsChest: obj is StardewValley.Objects.Chest,
                ObjectIsArtifactSpot: qid is "(O)590" or "(O)SeedSpot",
                TerrainType: terrain?.GetType().Name,
                TerrainHoeDirtHasCrop: terrain is StardewValley.TerrainFeatures.HoeDirt dirt && dirt.crop is not null,
                Tillable: tillable,
                WaterRefill: refill,
                WaterLeft: waterLeft,
                WaterMax: waterMax,
                Stamina: player.Stamina,
                ScytheArea: area,
                // WS-G0 pad path only: any furniture box intersecting the tool tile (rugs included;
                // LowPriorityLeftClick at the TOOL point picks up passable furniture, probe s2).
                FurnitureAtToolTile: furnitureAtToolTile && location.furniture.Any(f => f is not null
                    && f.GetBoundingBox().Intersects(new Rectangle(hit.X * 64, hit.Y * 64, 64, 64))),
                PetBowlAtToolTile: furnitureAtToolTile && kind == BgToolKind.WateringCan && PetBowlAt(location, hit.X, hit.Y) is not null,
                PetBowlWatered: furnitureAtToolTile && kind == BgToolKind.WateringCan && PetBowlAt(location, hit.X, hit.Y)?.watered.Value == true,
                ToolTileTreeGrowthStage: terrain is StardewValley.TerrainFeatures.Tree wild ? (int?)wild.growthStage.Value : null);
        }
        catch
        {
            return null;
        }
    }

    /// <summary>
    /// WS-G0 W27: the PetBowl building whose "PetBowl" Buildings tile property covers the tile (the only
    /// tile PetBowl.performToolAction waters), or null. Read-only.
    /// </summary>
    public static StardewValley.Buildings.PetBowl? PetBowlAt(GameLocation location, int x, int y)
    {
        foreach (var building in location.buildings)
        {
            string? value = null;
            if (building is StardewValley.Buildings.PetBowl bowl && bowl.occupiesTile(new Vector2(x, y))
                && bowl.doesTileHaveProperty(x, y, "PetBowl", "Buildings", ref value))
                return bowl;
        }
        return null;
    }

    /// <summary>Scythe sweep facts (BgToolTileGuard.ScytheAreaTiles): tree growth stage and HoeDirt crop.</summary>
    static IReadOnlyList<BgAreaTileFacts> ScytheArea(GameLocation location, int playerX, int playerY, int facing)
    {
        var area = new List<BgAreaTileFacts>(6);
        foreach (var (x, y) in BgToolTileGuard.ScytheAreaTiles(playerX, playerY, facing))
        {
            location.terrainFeatures.TryGetValue(new Vector2(x, y), out var feature);
            int? stage = feature is StardewValley.TerrainFeatures.Tree tree ? tree.growthStage.Value : null;
            bool crop = feature is StardewValley.TerrainFeatures.HoeDirt dirt && dirt.crop is not null;
            area.Add(new BgAreaTileFacts(x, y, stage, crop));
        }
        return area;
    }
}
