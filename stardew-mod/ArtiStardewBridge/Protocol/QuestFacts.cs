namespace ArtiStardewBridge.Protocol;

// W1 gap B4 (S28 surat_quest; W1_GAP_CLOSURE.md "B4"). Pure, SMAPI-free rules for the observable letter quest
// acceptance and the native item-delivery hand-in. ModEntry.QuestCapture.cs samples the game into these records;
// ModEntry.QuestContext.cs publishes the bounded actor quest facts. No executor: the mod never calls questComplete,
// NotifyQuests or addQuest. The letter A is the vanilla accept button click; the hand-in A is the ordinary pad A on
// the faced villager, whose native tryToReceiveActiveObject runs the quest branch. Evidence ceiling: UNIT_TESTED until
// the B4 live witnesses.
//
// Decomp (Stardew 1.6.15, G/ = decomp-all):
//   LetterViewerMenu "%item quest <id> [true]" (StardewValley.Menus/LetterViewerMenu.cs:514-526): with "true" the quest
//     is added on open; without it questID is kept and acceptQuestButton (myID 103, :188) is shown on the last page
//     (ShouldShowInteractable :970-977). receiveLeftClick on it -> AcceptQuest (:755-757, :817-826) = addQuest + questID
//     null. NOTE: cleanupBeforeExit (:1009-1014) ALSO accepts a pending quest when the letter closes (B / exit).
//   Farmer.addQuest (Farmer.cs:7993-8014): one Quest.getQuestFromId instance per id (hasQuest guard).
//   Quest.getQuestFromId "ItemDelivery" (Quest.cs:352-368): conditions field 4 = "<npc> <itemId> [number]", type 3.
//   NPC.tryToReceiveActiveObject (NPC.cs:1713-1790): SimpleNonVillagerNPC -> false; Henchman branch; special orders'
//     onItemDelivered; then who.NotifyQuests(OnItemOfferedToNpc, onlyOneQuest: true) BEFORE any gift branch.
//   Farmer.NotifyQuests (Farmer.cs:8965-8988): questLog from the END, skipping completed quests; first true wins.
//   ItemDeliveryQuest.OnItemOfferedToNpc (ItemDeliveryQuest.cs:490-515): not completed, npc.IsVillager, npc.Name ==
//     target, item.QualifiedItemId == ItemId; Stack >= number -> Items.Reduce(item, number), target dialogue,
//     friendship, questComplete (true); a smaller stack only pushes a "need more" dialogue and returns false (the
//     offer then continues to earlier quests and to the gift branch). Only ItemDeliveryQuest overrides this hook.
//   Quest.questComplete (Quest.cs:581-631): completed = true; the quest stays in the log when it has a reward, else it is
//     removed (questLog.Remove(this)).
public static class QuestRules
{
    public const int TypeItemDelivery = 3;
    public const string ItemDeliveryName = "ItemDelivery";
    /// <summary>Success outcome of the hand-in (Python arti_stardew_command.WORLD_EXTRAS_OUTCOMES mirrors it).</summary>
    public const string Delivered = WorldExtras.QuestDelivered;
    /// <summary>ui.context "quests": at most this many active quest-log entries (log order kept from the end).</summary>
    public const int QuestsMax = 12;
    /// <summary>Native quest state of a pinned instance: active in the log, completed in the log, removed from the log.</summary>
    public const int StateActive = 0, StateCompleted = 1, StateRemoved = 2;

    public const string RefusedInsufficient = "quest_refused:insufficient_stack",
        RefusedAmbiguous = "quest_refused:ambiguous_quest", RefusedSpecialOrder = "quest_refused:special_order_may_take",
        RefusedHenchman = "quest_refused:henchman", RefusedNotVillager = "quest_refused:npc_not_villager",
        RefusedEarlier = "quest_refused:earlier_action_target";

    /// <summary>Quest.type_* names (Quest.cs:25-45); anything else is "other".</summary>
    public static string TypeName(int type) => type switch
    {
        1 => "Basic", 2 => "Crafting", 3 => ItemDeliveryName, 4 => "Monster", 5 => "Socialize", 6 => "Location",
        7 => "Fishing", 8 => "Building", 9 => "ItemHarvest", 10 => "ResourceCollection", 11 => "Weeding", _ => "other",
    };

    public static int State(bool inLog, bool completed) => !inLog ? StateRemoved : completed ? StateCompleted : StateActive;

    /// <summary>One quest-log entry as the native offer hook sees it (log order).</summary>
    public sealed record OfferQuest(bool Completed, bool ItemDelivery, string Target, string Item, int Number);

    /// <summary>
    /// Read-only mirror of NotifyQuests(OnItemOfferedToNpc, onlyOneQuest) for one held stack offered to one NPC. Returns
    /// (-1, null) when no active delivery quest wants this item from this NPC (the gift path stays the native meaning);
    /// (index, null) for the ONE quest the A would complete; (-1, refusal) when a matching quest exists but the A would
    /// not be exactly that one hand-in (short stack, two candidates, a special order or the Henchman branch first).
    /// </summary>
    public static (int Index, string? Miss) Select(IReadOnlyList<OfferQuest> log, bool villager, string npcName,
        string heldQid, int heldStack, bool specialOrderHook, bool henchman)
    {
        var matches = new List<int>();
        for (int i = log.Count - 1; i >= 0; i--)
        {
            OfferQuest q = log[i];
            if (!q.Completed && q.ItemDelivery && q.Target.Length > 0 && q.Target == npcName && q.Item.Length > 0 && q.Item == heldQid)
                matches.Add(i);
        }
        if (matches.Count == 0)
            return (-1, null);
        if (henchman)
            return (-1, RefusedHenchman);
        if (!villager)
            return (-1, RefusedNotVillager);
        if (specialOrderHook)
            return (-1, RefusedSpecialOrder);
        if (matches.Count != 1)
            return (-1, RefusedAmbiguous);
        OfferQuest only = log[matches[0]];
        if (only.Number < 1 || heldStack < only.Number)
            return (-1, RefusedInsufficient);
        return (matches[0], null);
    }

    /// <summary>Data/Quests fields of one id (Quest.GetRawQuestFields): type, title, delivery target / item / number.
    /// The item stays as written (the native wrapper qualifies it). Unreadable = Readable false.</summary>
    public static (string Type, string Title, string Target, string Item, int Number, bool Readable) ParseRaw(string[]? raw)
    {
        if (raw is null || raw.Length < 2 || string.IsNullOrWhiteSpace(raw[0]))
            return ("", "", "", "", 0, false);
        string type = raw[0], title = raw[1] ?? "";
        if (type != ItemDeliveryName)
            return (type, title, "", "", 0, true);
        string[] cond = raw.Length > 4 ? raw[4].Split(' ', StringSplitOptions.RemoveEmptyEntries) : Array.Empty<string>();
        if (cond.Length < 2 || raw.Length < 10 || string.IsNullOrWhiteSpace(raw[9]))
            return (type, title, "", "", 0, false);
        int number = 1;
        if (cond.Length > 2 && (!int.TryParse(cond[2], out number) || number < 1))
            return (type, title, "", "", 0, false);
        return (type, title, cond[0], cond[1], number, true);
    }

    /// <summary>
    /// The letter's A on the accept-quest button (component 103): admitted only for the exact presented item-delivery
    /// quest that is readable, not already in the log, on the letter's last page (native ShouldShowInteractable) and
    /// snapped. Every other quest letter keeps the old refusal; reading / closing rows are unchanged.
    /// </summary>
    public static UiPadPlan AcceptLetter(string button, UiPadFacts f)
    {
        if (f.Extras?.LetterQuest is not { } q)
            return UiPadPlan.No(button, UiPadContextTable.Deny("quest_accept_not_admitted"));
        if (f.Snapped is not { Id: 103, Visible: true })
            return UiPadPlan.No(button, UiPadContextTable.Deny("quest_accept_not_snapped"));
        string? why = !q.Readable ? "quest_unreadable" : q.Type != ItemDeliveryName ? "quest_type_not_item_delivery"
            : q.AlreadyActive ? "quest_already_active" : !q.Interactable ? "quest_accept_not_shown"
            : q.Target.Length == 0 || q.Item.Length == 0 || q.Number < 1 ? "quest_unreadable" : null;
        return why is null ? UiPadContextTable.MirrorClick(button, "accept the presented item-delivery quest " + q.QuestId)
            : UiPadPlan.No(button, UiPadContextTable.Deny(why));
    }
}

/// <summary>The quest a non-collection LetterViewerMenu presents (questID != null), read from Data/Quests only.</summary>
public sealed record LetterQuestFacts(string QuestId, string Type, string Title, string Target, string Item, int Number,
    bool Readable, bool AlreadyActive, bool Interactable)
{
    public string Digest() => UiKeyTargetToken.Hash(string.Join("|", this.QuestId, this.Type, this.Target, this.Item,
        this.Number, this.Readable, this.AlreadyActive, this.Interactable))[..16];

    public Dictionary<string, object> Wire(string? receiverToken) => new()
    {
        ["quest_id"] = UiPadTable.Trim(this.QuestId, 40), ["type"] = UiPadTable.Trim(this.Type, 24),
        ["title"] = UiPadTable.Trim(this.Title, 60), ["target"] = UiPadTable.Trim(this.Target, 40),
        ["item"] = UiPadTable.Trim(this.Item, 60), ["number"] = this.Number, ["readable"] = this.Readable,
        ["already_active"] = this.AlreadyActive, ["accept_shown"] = this.Interactable,
        ["receiver_token"] = receiverToken ?? "",
    };
}

/// <summary>One active / completed quest-log entry of the local actor (ui.context "quests").</summary>
public sealed record QuestFact(string Instance, string Id, int Type, bool Accepted, bool Completed, bool Daily, int DaysLeft,
    string Target, string Item, int Number, string Title, string TargetLocation, QuestLogFact? Details = null)
{
    public Dictionary<string, object> Wire()
    {
        var wire = new Dictionary<string, object>
        {
            ["instance"] = this.Instance, ["quest_id"] = UiPadTable.Trim(this.Id, 40), ["type"] = QuestRules.TypeName(this.Type),
            ["accepted"] = this.Accepted, ["completed"] = this.Completed, ["daily"] = this.Daily, ["days_left"] = this.DaysLeft,
            ["target"] = UiPadTable.Trim(this.Target, 40), ["item"] = UiPadTable.Trim(this.Item, 60), ["number"] = this.Number,
            ["title"] = UiPadTable.Trim(this.Title, 60), ["target_location"] = UiPadTable.Trim(this.TargetLocation, 60),
        };
        if (this.Details is { } details)
            foreach (var pair in details.Wire())
                if (pair.Key != "id" && pair.Value is not null) wire.TryAdd(pair.Key, pair.Value);
        return wire;
    }
}

/// <summary>
/// The hand-in readback (receiver kind quest_delivery). The capture pins ONE quest instance, NPC, held stack, slot,
/// day and actor; its Sample reports Value = that instance's native state (QuestRules.State), Count = the bag count of
/// the pinned item, ProductQuantity = the quest's number, GiftsThisWeek = the NPC's gift counter. Success only when the
/// expected NPC dialogue is open, the SAME instance went active -> completed (or terminal removal), the bag lost exactly
/// the quest's number and no gift was counted. A dialogue / friendship change alone is never a delivery.
/// </summary>
public static class QuestDeliveryEffect
{
    public static BgActionEffectVerdict Classify(BgActionEffectFacts before, BgActionEffectFacts now)
    {
        if (before.Extra is not { Kind: WorldExtras.QuestDelivery } b || now.Extra is not { Kind: WorldExtras.QuestDelivery } n
            || b.ItemId.Length == 0 || b.ItemId != n.ItemId || b.ProductQuantity is not int number || number < 1
            || n.ProductQuantity != number || b.GiftsThisWeek is null || n.GiftsThisWeek is null)
            return No("quest_effect_unreadable");
        if (b.Value != QuestRules.StateActive)
            return No("quest_not_active_before");
        if (!now.MenuOpen || !now.ExpectedReceiver)
            return n.Value == QuestRules.StateActive && n.Count == b.Count && n.GiftsThisWeek == b.GiftsThisWeek
                ? new(BgActionEffect.InteractionNoEffect, false, null, null, 0) : No("quest_effect_mismatch");
        if (n.Value is not (QuestRules.StateCompleted or QuestRules.StateRemoved) || n.Count != b.Count - number
            || n.GiftsThisWeek != b.GiftsThisWeek)
            return No("quest_effect_mismatch");
        return new(QuestRules.Delivered, true, null, null, -number);
    }

    static BgActionEffectVerdict No(string reason) => new(BgHoldCompletion.InteractionUncertain, false, reason, null, 0);
}
