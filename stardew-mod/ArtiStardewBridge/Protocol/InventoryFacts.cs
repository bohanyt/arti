using System.Text.Json.Serialization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Kind token di dalam body.facts OBS-3A. Bukan CLR type name sebagai identity.
/// </summary>
public static class InventoryFactKind
{
    public const string Meta = "meta";
    public const string Slot = "slot";
    public const string Equipment = "equipment";
}

/// <summary>
/// Lokasi equipment farmer yang masuk scope OBS-3A. Bukan chest/UI/cursor.
/// </summary>
public static class InventoryEquipmentLocation
{
    public const string Hat = "hat";
    public const string Shirt = "shirt";
    public const string Pants = "pants";
    public const string Boots = "boots";
    public const string LeftRing = "left_ring";
    public const string RightRing = "right_ring";

    public static readonly string[] All =
    {
        Hat, Shirt, Pants, Boots, LeftRing, RightRing,
    };
}

public static class InventoryObservationCaps
{
    public const int Slots = 48;
    public const int Equipment = 8;
    /// <summary>S31: attachment entries per tool (vanilla rods have at most 3; slingshot 1).</summary>
    public const int Attachments = 4;
    public const int ReceiverMaxLineBytes = SceneObservationCaps.ReceiverMaxLineBytes;
}

/// <summary>
/// Item yang sudah dinormalisasi. Bukan graf Stardew.Item.
/// ProtocolTests membangun ini langsung; capture runtime mengisinya.
/// </summary>
public sealed record InventoryItemView
{
    public string QualifiedItemId { get; init; } = "";

    public string? Name { get; init; }

    public string? DisplayName { get; init; }

    public int Stack { get; init; }

    public int Quality { get; init; }

    public int Category { get; init; }

    public string TypeDefinitionId { get; init; } = "";

    public string? ToolSubtype { get; init; }

    public int? ToolUpgradeLevel { get; init; }

    public string? WeaponSubtype { get; init; }

    /// <summary>WS1: watering can water level (WaterLeft; bottomless = max). Null for anything else.</summary>
    public int? ToolWaterLeft { get; init; }

    /// <summary>WS1: watering can capacity (waterCanMax). Null for anything else.</summary>
    public int? ToolWaterMax { get; init; }

    /// <summary>S31: native attachment slot count (Tool.AttachmentSlotsCount). Null for non-attachment items.</summary>
    public int? ToolAttachmentSlots { get; init; }

    /// <summary>S31: one entry per native attachment slot, explicit empty included. Null for non-attachment items.</summary>
    public IReadOnlyList<InventoryAttachmentView>? Attachments { get; init; }

    /// <summary>S31: lifetime identity of this tool instance in this process (weak reference identity,
    /// never a save id). Same token = same object, so a same-qid replacement is visible.</summary>
    public string? ToolInstanceToken { get; init; }
}

/// <summary>S31: one native tool attachment slot (FishingRod bait/tackle, slingshot ammo). Read-only view.</summary>
public sealed record InventoryAttachmentView
{
    public int Slot { get; init; }

    /// <summary>Null = the native slot is empty.</summary>
    public string? QualifiedItemId { get; init; }

    public int Stack { get; init; }

    public int Quality { get; init; }

    public int Category { get; init; }

    /// <summary>Tackle wear (Object.uses). Null when not applicable.</summary>
    public int? Uses { get; init; }
}

public sealed record InventorySlotInput
{
    public int Slot { get; init; }

    public InventoryItemView? Item { get; init; }
}

public sealed record InventoryEquipmentInput
{
    public string Location { get; init; } = "";

    public InventoryItemView? Item { get; init; }
}

/// <summary>
/// Masukan murni untuk builder snapshot. Tidak memuat objek runtime Stardew.
/// </summary>
public sealed record InventorySnapshotInput
{
    public int ActiveSlot { get; init; }

    public IReadOnlyList<InventorySlotInput> Slots { get; init; } =
        Array.Empty<InventorySlotInput>();

    public IReadOnlyList<InventoryEquipmentInput> Equipment { get; init; } =
        Array.Empty<InventoryEquipmentInput>();
}

public sealed record InventoryMetaFact
{
    [JsonPropertyName("kind")]
    public string Kind { get; init; } = InventoryFactKind.Meta;

    [JsonPropertyName("active_slot")]
    public int ActiveSlot { get; init; }

    [JsonPropertyName("slot_count")]
    public int SlotCount { get; init; }
}

public sealed record InventorySlotFact
{
    [JsonPropertyName("kind")]
    public string Kind { get; init; } = InventoryFactKind.Slot;

    [JsonPropertyName("slot")]
    public int Slot { get; init; }

    [JsonPropertyName("occupied")]
    public bool Occupied { get; init; }

    [JsonPropertyName("is_active")]
    public bool IsActive { get; init; }

    [JsonPropertyName("qualified_item_id")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? QualifiedItemId { get; init; }

    [JsonPropertyName("name")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Name { get; init; }

    [JsonPropertyName("display_name")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? DisplayName { get; init; }

    [JsonPropertyName("stack")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? Stack { get; init; }

    [JsonPropertyName("quality")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? Quality { get; init; }

    [JsonPropertyName("category")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? Category { get; init; }

    [JsonPropertyName("type_definition")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? TypeDefinition { get; init; }

    [JsonPropertyName("type_token")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? TypeToken { get; init; }

    [JsonPropertyName("tool_subtype")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ToolSubtype { get; init; }

    [JsonPropertyName("tool_upgrade_level")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? ToolUpgradeLevel { get; init; }

    [JsonPropertyName("weapon_subtype")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? WeaponSubtype { get; init; }

    [JsonPropertyName("tool_water_left")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? ToolWaterLeft { get; init; }

    [JsonPropertyName("tool_water_max")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? ToolWaterMax { get; init; }

    /// <summary>S31: lifetime tool instance token (attachment tools only).</summary>
    [JsonPropertyName("tool_instance")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ToolInstance { get; init; }

    /// <summary>S31: native attachment slot count; attachments carries min(count, cap) entries.</summary>
    [JsonPropertyName("attachment_slots")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? AttachmentSlots { get; init; }

    [JsonPropertyName("attachments")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public IReadOnlyList<InventoryAttachmentFact>? Attachments { get; init; }
}

/// <summary>S31: one attachment slot on the wire. Empty slots are explicit (occupied=false).</summary>
public sealed record InventoryAttachmentFact
{
    [JsonPropertyName("slot")]
    public int Slot { get; init; }

    [JsonPropertyName("occupied")]
    public bool Occupied { get; init; }

    [JsonPropertyName("qualified_item_id")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? QualifiedItemId { get; init; }

    [JsonPropertyName("stack")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? Stack { get; init; }

    [JsonPropertyName("quality")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? Quality { get; init; }

    [JsonPropertyName("category")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? Category { get; init; }

    [JsonPropertyName("uses")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? Uses { get; init; }
}

public sealed record InventoryEquipmentFact
{
    [JsonPropertyName("kind")]
    public string Kind { get; init; } = InventoryFactKind.Equipment;

    [JsonPropertyName("location")]
    public string Location { get; init; } = "";

    [JsonPropertyName("occupied")]
    public bool Occupied { get; init; }

    [JsonPropertyName("qualified_item_id")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? QualifiedItemId { get; init; }

    [JsonPropertyName("name")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Name { get; init; }

    [JsonPropertyName("display_name")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? DisplayName { get; init; }

    [JsonPropertyName("stack")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? Stack { get; init; }

    [JsonPropertyName("quality")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? Quality { get; init; }

    [JsonPropertyName("category")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? Category { get; init; }

    [JsonPropertyName("type_definition")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? TypeDefinition { get; init; }

    [JsonPropertyName("type_token")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? TypeToken { get; init; }

    [JsonPropertyName("tool_subtype")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ToolSubtype { get; init; }

    [JsonPropertyName("tool_upgrade_level")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? ToolUpgradeLevel { get; init; }

    [JsonPropertyName("weapon_subtype")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? WeaponSubtype { get; init; }

    [JsonPropertyName("tool_water_left")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? ToolWaterLeft { get; init; }

    [JsonPropertyName("tool_water_max")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? ToolWaterMax { get; init; }
}
