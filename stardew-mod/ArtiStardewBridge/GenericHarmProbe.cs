using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Buildings;
using StardewValley.Tools;

namespace ArtiStardewBridge;

/// <summary>
/// ADR 246 (fix143A): game-coupled, READ-ONLY facts for the named harms of one generic world press
/// (<see cref="GenericHarm"/>). Game thread only. No input, no world write, no map scan: the pressed tile, the
/// villagers whose bodies overlap it and the buildings that occupy it, through the game's own objects and predicates
/// (Character.IsVillager, Building.occupiesTile, the terrain feature's own class, HoeDirt.crop, Tree.growthStage).
/// No list of villagers, crops or items. Shared tile facts come from <see cref="BackgroundToolTileProbe"/>, so the
/// generic USE and the named tool hold read the hit tile with one predicate. Written without lambdas on purpose:
/// compiler-generated closures in a scanned class renumber the IL policy's closure rows (ADR 25).
/// Evidence ceiling: UNIT_TESTED; the game proves what the press does.
/// </summary>
internal static class GenericHarmProbe
{
    /// <summary>The facts for a generic ACTION (<paramref name="use"/> false, pad A) or USE (true, pad X).</summary>
    public static GenericHarmFacts Sample(Farmer? player, bool use)
    {
        try
        {
            GameLocation? location = Game1.currentLocation;
            if (player is null || location is null)
                return Unreadable(use);
            StardewValley.Object? held = player.ActiveObject;
            Tool? tool = player.CurrentTool;
            string? toolKind = held is null ? ToolKindOf(tool) : null;
            if (!use)
            {
                Vector2 grab = player.GetGrabTile();
                int gx = (int)grab.X, gy = (int)grab.Y;
                return new GenericHarmFacts(true, false, held is not null, held?.Name, toolKind,
                    VillagerOverlapping(location, gx, gy), ShippingBinAt(location, grab), gx, gy, null, false, null);
            }
            if (!GenericHarm.HeavyHitter(toolKind) && !GenericHarm.Sweeps(toolKind))
            {
                // No named harm can follow (a can, a pan, an object): only the tile is reported.
                return BackgroundToolTileProbe.ToolTile(player) is { } hit
                    ? new GenericHarmFacts(true, true, held is not null, held?.Name, toolKind, null, false, hit.X, hit.Y, null, false, null)
                    : Unreadable(use);
            }
            BgToolKind sample = GenericHarm.Sweeps(toolKind) ? BgToolKind.Scythe : BgToolKind.Axe;
            if (BackgroundToolTileProbe.SampleTileFacts(player, sample) is not { } f)
                return Unreadable(use);
            return new GenericHarmFacts(true, true, held is not null, held?.Name, toolKind, null, false, f.ToolTileX, f.ToolTileY,
                f.TerrainType, f.TerrainHoeDirtHasCrop, f.ToolTileTreeGrowthStage, f.ScytheArea, f.PlayerTileX, f.PlayerTileY, f.Facing);
        }
        catch
        {
            return Unreadable(use);
        }
    }

    static GenericHarmFacts Unreadable(bool use) => new(false, use, false, null, null, null, false, 0, 0, null, false, null);

    /// <summary>The held tool's game class in plain words (the game's own types; a scythe by Tool.isScythe).</summary>
    public static string? ToolKindOf(Tool? tool)
    {
        if (tool is null)
            return null;
        if (tool is Axe)
            return GenericHarm.ToolAxe;
        if (tool is Pickaxe)
            return GenericHarm.ToolPickaxe;
        if (tool is Hoe)
            return GenericHarm.ToolHoe;
        if (tool is WateringCan)
            return GenericHarm.ToolCan;
        if (tool is MeleeWeapon)
            return tool.isScythe() ? GenericHarm.ToolScythe : GenericHarm.ToolWeapon;
        return GenericHarm.ToolOther;
    }

    /// <summary>
    /// The display name of a villager whose body overlaps the pressed tile or the tile below it (a villager's sprite is
    /// two tiles tall and the action press finds the character by its body), or null.
    /// </summary>
    static string? VillagerOverlapping(GameLocation location, int x, int y)
    {
        Rectangle reach = new(x * 64, y * 64, 64, 128);
        foreach (NPC npc in location.characters)
        {
            if (npc is not null && npc.IsVillager && npc.GetBoundingBox().Intersects(reach))
                return npc.displayName;
        }
        return null;
    }

    static bool ShippingBinAt(GameLocation location, Vector2 tile)
    {
        foreach (Building building in location.buildings)
        {
            if (building is ShippingBin && building.occupiesTile(tile))
                return true;
        }
        return false;
    }
}
