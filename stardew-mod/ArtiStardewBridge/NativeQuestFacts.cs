using ArtiStardewBridge.Protocol;
using StardewValley;
using StardewValley.Quests;

namespace ArtiStardewBridge;

/// <summary>Read-only quest requirements shared by knowledge.world and ui.context (ADR 69).
/// Never call lazy text getters/loadQuestInfo: they mutate native quest state.</summary>
internal static class NativeQuestFacts
{
    internal static QuestLogFact Read(Quest q, Farmer player, Func<string, string>? npcLocation = null)
    {
        string id = q.id.Value ?? "";
        string[]? raw = null;
        try { raw = id.Length > 0 ? Quest.GetRawQuestFields(id) : null; } catch (Exception) { }
        var (description, objective, money, reward, cancel) = ProgressRules.QuestRaw(raw);
        string title = q._questTitle is { Length: > 0 } t ? t : raw is { Length: > 1 } ? raw[1] ?? "" : "";
        description = q._questDescription is { Length: > 0 } d ? d : description;
        objective = q._currentObjective is { Length: > 0 } o ? o : objective;
        string target = "", location = "", search = "", item = "", itemName = "", note = "", kind = "";
        int? have = null, need = null;
        switch (q)
        {
            case ItemDeliveryQuest x:
                target = x.target.Value ?? ""; item = x.ItemId.Value ?? ""; need = x.number.Value;
                item = Qualify(item); have = player.Items.Where(i => i is not null &&
                    i.itemId.Value is { } value && i.TypeDefinitionId + value == item).Sum(i => i!.Stack);
                kind = "in bag"; break;
            case SlayMonsterQuest x:
                target = x.target.Value ?? ""; itemName = x.monsterName.Value ?? "";
                have = x.numberKilled.Value; need = x.numberToKill.Value; kind = "slain"; break;
            case ResourceCollectionQuest x:
                target = x.target.Value ?? ""; item = x.ItemId.Value ?? "";
                have = x.numberCollected.Value; need = x.number.Value; kind = "collected"; break;
            case FishingQuest x:
                target = x.target.Value ?? ""; item = x.ItemId.Value ?? "";
                have = x.numberFished.Value; need = x.numberToFish.Value; kind = "caught"; break;
            case LostItemQuest x:
                target = x.npcName.Value ?? ""; item = x.ItemId.Value ?? ""; need = 1;
                have = x.itemFound.Value ? 1 : 0; kind = "found";
                search = x.locationOfItem.Value ?? "";
                note = x.itemFound.Value ? "item found" : "item not found yet"; break;
            case SecretLostItemQuest x:
                target = x.npcName.Value ?? ""; item = x.ItemId.Value ?? ""; need = 1;
                have = x.itemFound.Value ? 1 : 0; kind = "found"; break;
            case SocializeQuest x:
                // Native total==0 is uninitialized, not a completed 0/0 objective.
                if (x.total.Value > 0)
                { need = x.total.Value; have = x.total.Value - x.whoToGreet.Count; kind = "people met"; }
                break;
            case ItemHarvestQuest x:
                item = x.ItemId.Value ?? ""; need = x.Number.Value; kind = "remaining"; break;
            case CraftingQuest x:
                item = x.ItemId.Value ?? ""; need = 1; kind = "craft"; break;
            case GoSomewhereQuest x:
                location = x.whereToGo.Value ?? ""; break;
            case HaveBuildingQuest x:
                target = x.buildingType.Value ?? ""; need = 1; kind = "build"; break;
        }
        item = Qualify(item);
        if (item.Length > 0)
            itemName = ItemRegistry.GetData(item)?.DisplayName ?? item;
        // "null" is the native no-recipient sentinel, not an NPC name.
        if (target == "null") target = "";
        if (location.Length == 0 && target.Length > 0 && q is not HaveBuildingQuest && !q.completed.Value)
            location = npcLocation?.Invoke(target) ?? "";
        // WorldDate.TotalDays is zero based; protocol dates use the existing one-based plan ordinal.
        // Farmer.dayUpdate decrements before removing at <=0: daysLeft=1 means the last usable day is today.
        int observed = Game1.Date.TotalDays + 1;
        int? deadline = q.IsTimedQuest() ? observed + q.daysLeft.Value - 1 : null;
        return new QuestLogFact(id, QuestRules.TypeName(q.questType.Value), title, description, objective,
            q.accepted.Value, q.completed.Value, q.dailyQuest.Value, q.daysLeft.Value,
            q.moneyReward.Value > 0 ? q.moneyReward.Value : money,
            q.rewardDescription.Value is { Length: > 0 } r ? r : reward, q.canBeCancelled.Value || cancel == true,
            target, item, have, need, note, itemName, location, kind, deadline, observed, Game1.timeOfDay, search);
    }

    static string Qualify(string id) => id.Length == 0 || id.StartsWith('-') ? id : ItemRegistry.QualifyItemId(id) ?? id;
}
