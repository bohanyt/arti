namespace ArtiStardewBridge.Protocol;

/// <summary>
/// FIX77C (ADR 136): the object step of GameLocation.checkAction (GameLocation.cs:7791), asked instead of a family of items. A spawned
/// object whose Type is neither Crafting nor interactive goes to the pickup branch, whatever its category; a forage tag is not part of
/// that test. A quest item with a quest id is refused unless this farmer holds that quest (GameLocation.cs:7799). Pure, no game read.
/// </summary>
public static class GroundPickup
{
    public const string QuestNotHeld = "quest_item_quest_not_held";

    public static bool NativePickup(bool spawned, string? type) =>
        spawned && type is not null && type is not ("Crafting" or "interactive");

    /// <summary>The press the game itself refuses: checkAction returns false and nothing moves. Offering it would be a press with no effect.</summary>
    public static string? QuestMiss(bool questItem, string? questId, bool hasQuest) =>
        questItem && questId is not null && questId != "0" && !hasQuest ? QuestNotHeld : null;

    /// <summary>
    /// A found quest item opens the game's own "found it" dialogue inside the press frame (LostItemQuest.OnItemReceived), so no release
    /// frame follows. Only a quest item's target names this receiver; any other forage keeps its rule that every menu is unexpected.
    /// </summary>
    public static bool MayOpenFoundDialogue(bool questItem) => questItem;
}
