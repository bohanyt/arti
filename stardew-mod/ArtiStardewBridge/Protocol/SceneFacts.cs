using System.Text.Json.Serialization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Kind token di dalam body.facts OBS-1. Bukan CLR type name sebagai identity.
/// </summary>
public static class SceneFactKind
{
    public const string Location = "location";
    public const string Warp = "warp";
    public const string Door = "door";
    public const string Object = "object";
    public const string Terrain = "terrain";
    public const string Npc = "npc";
    public const string Monster = "monster";
    public const string Clump = "clump";
    /// <summary>WS3a: building human door (cabin, farmhouse, shed...) with its indoors target.</summary>
    public const string BuildingDoor = "building_door";
}

public sealed record SceneLocationFact
{
    [JsonPropertyName("kind")]
    public string Kind { get; init; } = SceneFactKind.Location;

    [JsonPropertyName("location_id")]
    public string LocationId { get; init; } = "";

    [JsonPropertyName("location_kind")]
    public string LocationKind { get; init; } = "";

    [JsonPropertyName("is_outdoors")]
    public bool? IsOutdoors { get; init; }

    [JsonPropertyName("map_width")]
    public int MapWidth { get; init; }

    [JsonPropertyName("map_height")]
    public int MapHeight { get; init; }

    /// <summary>WS3a: token of the local player's home (farmhand cabin indoors); omitted when unknown.</summary>
    [JsonPropertyName("home_location")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? HomeLocation { get; init; }
}

public sealed record SceneWarpFact
{
    [JsonPropertyName("kind")]
    public string Kind { get; init; } = SceneFactKind.Warp;

    [JsonPropertyName("source_tile")]
    public TileXY SourceTile { get; init; } = new();

    [JsonPropertyName("target_location")]
    public string TargetLocation { get; init; } = "";

    [JsonPropertyName("target_tile")]
    public TileXY TargetTile { get; init; } = new();

    /// <summary>WS3a: true for NPC-only warps (farmers never trigger them); omitted otherwise.</summary>
    [JsonPropertyName("npc_only")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? NpcOnly { get; init; }
}

public sealed record SceneDoorFact
{
    [JsonPropertyName("enterable")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? Enterable { get; init; }

    [JsonPropertyName("kind")]
    public string Kind { get; init; } = SceneFactKind.Door;

    [JsonPropertyName("tile")]
    public TileXY Tile { get; init; } = new();

    [JsonPropertyName("target_location")]
    public string TargetLocation { get; init; } = "";

    [JsonPropertyName("target_tile")]
    public TileXY? TargetTile { get; init; }

    /// <summary>
    /// WS3a: first token of the Buildings-layer Action when it is NOT a plain "Warp"
    /// (LockedDoorWarp, Door, ...). Omitted for Warp doors and when unread (wire budget);
    /// the mod capture stays authoritative for what world_interact offers.
    /// </summary>
    [JsonPropertyName("action")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Action { get; init; }
}

/// <summary>WS3a: a building human door. Tile is absolute; footprint lets routing treat the building as blocked.</summary>
public sealed record SceneBuildingDoorFact
{
    [JsonPropertyName("kind")]
    public string Kind { get; init; } = SceneFactKind.BuildingDoor;

    [JsonPropertyName("tile")]
    public TileXY Tile { get; init; } = new();

    [JsonPropertyName("footprint")]
    public SceneBounds Footprint { get; init; } = new();

    [JsonPropertyName("target_location")]
    public string TargetLocation { get; init; } = "";

    [JsonPropertyName("owned_by_actor")]
    public bool OwnedByActor { get; init; }

    [JsonPropertyName("enterable")]
    public bool Enterable { get; init; }
}

public sealed record SceneHeldItemFact
{
    [JsonPropertyName("qualified_item_id")]
    public string QualifiedItemId { get; init; } = "";

    [JsonPropertyName("stack")]
    public int Stack { get; init; }

    [JsonPropertyName("name")]
    public string? Name { get; init; }
}

public sealed record SceneBounds
{
    [JsonPropertyName("x")]
    public int X { get; init; }

    [JsonPropertyName("y")]
    public int Y { get; init; }

    [JsonPropertyName("width")]
    public int Width { get; init; }

    [JsonPropertyName("height")]
    public int Height { get; init; }
}

public sealed record SceneObjectFact
{
    [JsonPropertyName("kind")]
    public string Kind { get; init; } = SceneFactKind.Object;

    [JsonPropertyName("qualified_item_id")]
    public string QualifiedItemId { get; init; } = "";

    [JsonPropertyName("name")]
    public string? Name { get; init; }

    [JsonPropertyName("display_name")]
    public string? DisplayName { get; init; }

    [JsonPropertyName("tile")]
    public TileXY Tile { get; init; } = new();

    [JsonPropertyName("bounds")]
    public SceneBounds? Bounds { get; init; }

    [JsonPropertyName("passable")]
    public bool? Passable { get; init; }

    [JsonPropertyName("grabbable")]
    public bool? Grabbable { get; init; }

    [JsonPropertyName("tool_breakable")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? ToolBreakable { get; init; }

    /// <summary>WS3a: IsSpawnedObject (pickable forage); written only when true.</summary>
    [JsonPropertyName("spawned")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? Spawned { get; init; }

    [JsonPropertyName("machine")]
    public bool? Machine { get; init; }

    [JsonPropertyName("ready_for_harvest")]
    public bool? ReadyForHarvest { get; init; }

    [JsonPropertyName("minutes_until_ready")]
    public int? MinutesUntilReady { get; init; }

    [JsonPropertyName("held_item")]
    public SceneHeldItemFact? HeldItem { get; init; }
}

public sealed record SceneCropBlock
{
    [JsonPropertyName("qualified_item_id")]
    public string? QualifiedItemId { get; init; }

    [JsonPropertyName("phase")]
    public int? Phase { get; init; }

    [JsonPropertyName("dead")]
    public bool? Dead { get; init; }

    [JsonPropertyName("watered")]
    public bool? Watered { get; init; }

    [JsonPropertyName("ready_for_harvest")]
    public bool? ReadyForHarvest { get; init; }

    /// <summary>Live7 follow-up: true for a vanilla forage crop in HoeDirt (Spring Onion / Ginger, Crop.forageCrop);
    /// qualified_item_id is then its native yield. Omitted (null) for every planted crop: the wire is unchanged.</summary>
    [JsonPropertyName("forage_crop")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? ForageCrop { get; init; }

    /// <summary>Native crop interaction data, not an action policy.</summary>
    [JsonPropertyName("harvest_method")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? HarvestMethod { get; init; }
}

public sealed record SceneWildTreeBlock
{
    [JsonPropertyName("stump")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? Stump { get; init; }

    [JsonPropertyName("tree_type")]
    public string? TreeType { get; init; }

    [JsonPropertyName("growth_stage")]
    public int? GrowthStage { get; init; }

    [JsonPropertyName("tapped")]
    public bool? Tapped { get; init; }
}

public sealed record SceneFruitTreeBlock
{
    [JsonPropertyName("tree_type")]
    public string? TreeType { get; init; }

    [JsonPropertyName("growth_stage")]
    public int? GrowthStage { get; init; }

    [JsonPropertyName("has_fruit")]
    public bool? HasFruit { get; init; }
}

public sealed record SceneTerrainFact
{
    [JsonPropertyName("kind")]
    public string Kind { get; init; } = SceneFactKind.Terrain;

    [JsonPropertyName("terrain_type")]
    public string TerrainType { get; init; } = "";

    [JsonPropertyName("tile")]
    public TileXY Tile { get; init; } = new();

    [JsonPropertyName("passable")]
    public bool? Passable { get; init; }

    [JsonPropertyName("crop")]
    public SceneCropBlock? Crop { get; init; }

    [JsonPropertyName("wild_tree")]
    public SceneWildTreeBlock? WildTree { get; init; }

    [JsonPropertyName("fruit_tree")]
    public SceneFruitTreeBlock? FruitTree { get; init; }
}

public sealed record SceneMonsterBlock
{
    [JsonPropertyName("ephemeral")]
    public bool Ephemeral { get; init; } = true;

    [JsonPropertyName("health")]
    public int? Health { get; init; }

    [JsonPropertyName("max_health")]
    public int? MaxHealth { get; init; }
}

public sealed record SceneCharacterFact
{
    [JsonPropertyName("kind")]
    public string Kind { get; init; } = SceneFactKind.Npc;

    [JsonPropertyName("category")]
    public string Category { get; init; } = "npc";

    [JsonPropertyName("internal_name")]
    public string InternalName { get; init; } = "";

    [JsonPropertyName("display_name")]
    public string? DisplayName { get; init; }

    [JsonPropertyName("tile")]
    public TileXY Tile { get; init; } = new();

    [JsonPropertyName("pixel")]
    public PixelXY Pixel { get; init; } = new();

    [JsonPropertyName("facing")]
    public int Facing { get; init; }

    [JsonPropertyName("moving")]
    public bool Moving { get; init; }

    /// <summary>WS3b: IsVillager and CanSocialize (talk candidate); omitted otherwise.</summary>
    [JsonPropertyName("villager")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? Villager { get; init; }

    /// <summary>WS3b: the local player already talked to this NPC today; omitted otherwise.</summary>
    [JsonPropertyName("talked_today")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? TalkedToday { get; init; }

    [JsonPropertyName("monster")]
    public SceneMonsterBlock? Monster { get; init; }
}

public sealed record SceneClumpFact
{
    [JsonPropertyName("kind")]
    public string Kind { get; init; } = SceneFactKind.Clump;

    [JsonPropertyName("clump_type")]
    public string ClumpType { get; init; } = "";

    [JsonPropertyName("tile")]
    public TileXY Tile { get; init; } = new();

    [JsonPropertyName("width")]
    public int Width { get; init; }

    [JsonPropertyName("height")]
    public int Height { get; init; }

    [JsonPropertyName("parent_sheet_index")]
    public int? ParentSheetIndex { get; init; }
}

/// <summary>Fact yang sudah punya ID semantik, siap masuk body.facts.</summary>
public readonly record struct ScenePreparedFact(string Id, object Dto);
