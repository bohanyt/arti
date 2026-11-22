using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Quests;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

// W1 gap B4 (S28 surat_quest). The faced villager A with a held stack that ONE active native item-delivery quest of the
// actor wants: captured BEFORE the gift kind (NPC.tryToReceiveActiveObject runs the quest hook before any gift branch),
// on the same pad A retained-press hold as the gift. Read-only probe (QuestRules.Select mirrors NotifyQuests +
// ItemDeliveryQuest.OnItemOfferedToNpc without calling them); the pinned readback follows the SAME quest instance.
// Also the read-only facts of the quest a letter presents. Linked into ProtocolTests against offline doubles.
// Evidence ceiling: UNIT_TESTED until the B4 live witnesses.
public sealed partial class ModEntry
{
    static string QuestDeliverySource(GameLocation location, NPC npc, string questId) => "xt:quest:"
        + SceneIdentity.Token(location.NameOrUniqueName) + ":" + SceneIdentity.Token(npc.Name) + ":" + SceneIdentity.Token(questId);

    static List<QuestRules.OfferQuest> QuestOfferLog(Farmer player) => player.questLog.Select(q => q is ItemDeliveryQuest d
            ? new QuestRules.OfferQuest(d.completed.Value, true, d.target.Value ?? "", d.ItemId.Value ?? "", d.number.Value)
            : new QuestRules.OfferQuest(q?.completed.Value ?? true, false, "", "", 0)).ToList();

    /// <summary>A special order's onItemDelivered hook runs before the quest hook (NPC.cs:1755-1776): conservatively,
    /// any such hook means the A may not be exactly the quest hand-in.</summary>
    static bool QuestSpecialOrderHook(Farmer player) => player.team.specialOrders.Any(o => o?.onItemDelivered is not null);

    /// <summary>null = no active delivery quest wants this stack from the faced NPC (the gift kind decides).</summary>
    InteractKindCapture? CaptureQuestDelivery(Farmer player, GameLocation location, Vector2 tile, SObject held, ExtrasGuardFacts facts)
    {
        Rectangle rect = TileRect(tile);
        List<NPC> hits = location.characters.Where(c => c is not null && c.GetBoundingBox().Intersects(rect)).ToList();
        if (hits.Count == 0)
            return null;
        NPC npc = hits[0];
        string qid = held.QualifiedItemId ?? "";
        var (index, refusal) = QuestRules.Select(QuestOfferLog(player), npc.IsVillager && !npc.SimpleNonVillagerNPC, npc.Name,
            qid, held.Stack, QuestSpecialOrderHook(player), npc.Name == "Henchman");
        if (index < 0 && refusal is null)
            return null;
        ItemDeliveryQuest? quest = index >= 0 ? player.questLog[index] as ItemDeliveryQuest : null;
        string source = QuestDeliverySource(location, npc, quest?.id.Value ?? "_");
        if (BuildingTakesGrab(location, (int)tile.X, (int)tile.Y) || location.animals.Values.Any(a => a.GetBoundingBox().Intersects(rect)))
            refusal = QuestRules.RefusedEarlier;
        facts = facts with { TargetCandidates = hits.Count, OccupantAtTile = !NpcOccupantsExact(player, location, rect, npc),
            InputPolicyDetail = refusal };
        string? miss = WorldExtras.Miss(WorldExtras.QuestDelivery, facts);
        if (miss is not null || quest is null)
        {
            this.extrasMissTarget = (WorldExtras.QuestDelivery, source, (int)tile.X, (int)tile.Y);
            return InteractKindCapture.Missed(miss ?? QuestRules.RefusedAmbiguous);
        }
        int slot = player.CurrentToolIndex, day = Game1.Date.TotalDays, number = quest.number.Value;
        string npcFact = this.NpcFactIdFor(location, location.NameOrUniqueName, npc);
        string instance = this.giftTargetInstances.Pair(npc, held) + ":" + this.giftTargetInstances.Single(quest);
        string token = this.InteractToken(WorldExtras.QuestDelivery,
            instance + ":" + slot + ":" + held.Stack + ":" + qid + ":" + number, source, player, null);
        var target = new WorldInteractionTarget(token, source, (int)tile.X, (int)tile.Y,
            (int)player.Tile.X, (int)player.Tile.Y, player.FacingDirection, WorldExtras.QuestDelivery, null);
        if (!target.IsValid) return InteractKindCapture.Missed("target_invalid");
        bool Present() => this.ExtrasEnabled && ReferenceEquals(Game1.player, player) && ReferenceEquals(player.currentLocation, location)
            && Game1.Date.TotalDays == day && player.GetGrabTile() == tile && location.characters.Contains(npc)
            && this.NpcFactIdFor(location, location.NameOrUniqueName, npc) == npcFact
            && player.CurrentToolIndex == slot && ReferenceEquals(player.ActiveObject, held)
            && player.questLog.Contains(quest) && !quest.completed.Value
            && this.CaptureQuestDelivery(player, location, tile, held, this.ExtrasBaseFacts(player, tile, location))?.Target?.TargetToken == token;
        bool Expected(object? menu) => menu is DialogueBox { isQuestion: false } box
            && (ReferenceEquals(box.characterDialogue?.speaker, npc) || ReferenceEquals(Game1.currentSpeaker, npc));
        BgActionEffectFacts Sample()
        {
            bool valid = ReferenceEquals(Game1.player, player) && ReferenceEquals(player.currentLocation, location)
                && Game1.Date.TotalDays == day && location.characters.Contains(npc) && player.CurrentToolIndex == slot;
            player.friendshipData.TryGetValue(npc.Name, out Friendship? f);
            int state = QuestRules.State(player.questLog.Contains(quest), quest.completed.Value);
            return SampleInteractEffect(() => valid, qid) with { ExpectedReceiver = Expected(Game1.activeClickableMenu),
                Extra = valid ? new BgExtraFacts(WorldExtras.QuestDelivery, state != QuestRules.StateActive, InventoryCount(player, qid),
                    state, qid, f?.GiftsThisWeek ?? 0, ProductQuantity: number) : null };
        }
        return new InteractKindCapture(target, new BgActionTarget(BgActionKind.ExtraReceiver, null, null, qid, Present, Sample)
            { ExtraKind = WorldExtras.QuestDelivery, ExpectedReceiver = Expected, PoseMayChangeAfterPress = true }, null, false);
    }

    /// <summary>The quest an ordinary (not from-collection) letter presents for acceptance, from Data/Quests only (no Quest
    /// instance is created); null when the letter presents none.</summary>
    static LetterQuestFacts? ReadLetterQuest(LetterViewerMenu letter, Farmer? player)
    {
        if (letter.isFromCollection || letter.questID is not string id || id.Length == 0 || player is null)
            return null;
        var (type, title, target, item, number, readable) = QuestRules.ParseRaw(Quest.GetRawQuestFields(id));
        string qualified = item.Length == 0 ? "" : ItemRegistry.QualifyItemId(item) ?? item;
        return new LetterQuestFacts(id, type, title, target, qualified, number, readable, player.hasQuest(id),
            letter.ShouldShowInteractable());
    }

    /// <summary>The actor's quest log as bounded facts (newest first, at most QuestRules.QuestsMax), pinned instance tokens.</summary>
    List<QuestFact> ReadQuestFacts(Farmer player, Func<string, string>? targetLocation = null)
    {
        var facts = new List<QuestFact>();
        for (int i = player.questLog.Count - 1; i >= 0 && facts.Count < QuestRules.QuestsMax; i--)
        {
            Quest? q = player.questLog[i];
            if (q is null)
                continue;
            var detail = NativeQuestFacts.Read(q, player, targetLocation);
            facts.Add(new QuestFact(this.giftTargetInstances.Single(q), q.id.Value ?? "", q.questType.Value, q.accepted.Value,
                q.completed.Value, q.dailyQuest.Value, q.daysLeft.Value, detail.Target, detail.Item, detail.Need ?? 0,
                detail.Title, detail.TargetLocation, detail));
        }
        return facts;
    }
}
