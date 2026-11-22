using System.Runtime.CompilerServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using ArtiStardewBridge.Protocol;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Quests;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    // ADR 184: inspect the native list/entry and bind its controls; never mark quests viewed here.
    Dictionary<string, object>? ReadJournal(GenericGraph graph)
    {
        if (graph.Dispatch is not QuestLog log) return null;
        try
        {
            var pages = this.Helper.Reflection.GetField<List<List<IQuest>>>(log, "pages").GetValue();
            int page = this.Helper.Reflection.GetField<int>(log, "currentPage").GetValue();
            int entry = this.Helper.Reflection.GetField<int>(log, "questPage").GetValue();
            int? Control(object c) => graph.Widgets.FirstOrDefault(w => ReferenceEquals(w.Component, c))?.Id;
            var rows = pages[page].Select((q, i) =>
            {
                var quest = q as Quest;
                var details = quest is null ? null : NativeQuestFacts.Read(quest, Game1.player);
                return new Dictionary<string, object?> { ["key"] = RuntimeHelpers.GetHashCode(q).ToString(),
                    ["control"] = i < log.questLogButtons.Count ? Control(log.questLogButtons[i]) : -1,
                    ["is_new"] = q.ShouldDisplayAsNew(), ["complete"] = q.ShouldDisplayAsComplete(),
                    ["reward"] = NativeJournalFacts.Reward(q), ["title"] = details?.Title,
                    ["money"] = quest is null ? null : quest.moneyReward.Value,
                    ["reward_text"] = quest?.rewardDescription.Value, ["destroyed"] = quest?.destroy.Value };
            }).ToArray();
            if (entry >= 0 && entry < rows.Length && rows[entry]["complete"] is true && rows[entry]["reward"] is not false)
                // fix137A (ADR 237 A): the words the game draws on the reward part come first (DescribeByDrawnWords);
                // this English row stays only for a frame that carried no drawn words.
                graph.Widgets = graph.Widgets.Select(w => ReferenceEquals(w.Component, log.rewardBox) && w.Label == GenericUiPartText.Unnamed
                    ? w with { Label = "Collect completed quest money reward (native)" } : w).ToList();
            return new() { ["page"] = page, ["entry"] = entry, ["pages"] = pages.Count,
                ["total"] = pages.Sum(p => p.Count), ["entries"] = rows,
                ["back"] = Control(log.backButton) ?? -1, ["next"] = Control(log.forwardButton) ?? -1,
                ["reward"] = Control(log.rewardBox) ?? -1, ["close"] = Control(log.upperRightCloseButton) ?? -1,
                ["keys"] = pages.SelectMany(p => p).Take(128).Select(q => RuntimeHelpers.GetHashCode(q).ToString()).ToArray(),
                ["keys_complete"] = pages.Sum(p => p.Count) <= 128,
                ["indicator"] = Game1.player.hasNewQuestActivity() };
        }
        catch { return new() { ["unknown"] = true }; }
    }
}

internal static class NativeJournalFacts
{
    // SpecialOrder.HasMoneyReward calls a lazy cache writer. Never invoke it from observation.
    internal static bool? Reward(IQuest q) => q is Quest quest
        ? quest.completed.Value && quest.moneyReward.Value > 0 : null;
    internal static Dictionary<string, object> Activity(Farmer p, Dictionary<string, object> fact)
    {
        fact["date"] = $"{Game1.year}:{Game1.currentSeason}:{Game1.dayOfMonth}";
        fact["time"] = Game1.timeOfDay;
        try
        {
            var orders = p.team.specialOrders.Where(q => !q.IsHidden()).Cast<IQuest>().ToArray();
            var quests = p.questLog.Where(q => q is not null && !q.IsHidden() && !q.destroy.Value).ToArray();
            var all = orders.Concat(quests.Cast<IQuest>()).ToArray();
            fact["new_entries"] = all.Count(q => q.ShouldDisplayAsNew());
            if (all.All(q => Reward(q).HasValue)) fact["unclaimed_rewards"] = all.Count(q => Reward(q) is true);
            fact["completed_entries"] = all.Count(q => q.ShouldDisplayAsComplete());
            // Complete native content, not quest counts or a wall-clock guess. Special-order progress is unknown.
            if (orders.Length == 0)
            {
                var content = quests.Select(q => NativeQuestFacts.Read(q, p).Wire()).ToArray();
                foreach (var row in content) { row.Remove("observed_time"); row.Remove("observed_day"); }
                fact["revision"] = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(content))));
            }
        }
        catch { /* Native Boolean remains valid; unavailable causes/revision stay unknown. */ }
        return fact;
    }
}
