using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Objects;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    readonly GiftTargetInstances giftTargetInstances = new();
    static string GiftSource(GameLocation location, NPC npc) => "xt:gift:"
        + SceneIdentity.Token(location.NameOrUniqueName) + ":" + SceneIdentity.Token(npc.Name);

    static string? GiftNpcMiss(Farmer player, NPC npc)
    {
        player.friendshipData.TryGetValue(npc.Name, out Friendship? f);
        return GiftRules.Miss(new GiftNpcFacts(npc.IsVillager, npc.CanSocialize, npc.CanReceiveGifts(),
            npc.isSleeping.Value || npc.IsInvisible, npc.isBirthday(), f?.GiftsToday ?? 0,
            f?.GiftsThisWeek ?? 0, f?.IsDivorced() == true, player.spouse == npc.Name));
    }

    InteractKindCapture? CaptureGiftKind(Farmer player, GameLocation location, Vector2 tile,
        SObject held, ExtrasGuardFacts facts)
    {
        Rectangle rect = TileRect(tile);
        List<NPC> hits = location.characters.Where(c => c is not null && c.GetBoundingBox().Intersects(rect)).ToList();
        if (hits.Count == 0)
            return null;
        NPC npc = hits[0];
        string source = GiftSource(location, npc);
        string? refusal = GiftNpcMiss(player, npc);
        // No probe tryToReceiveActiveObject: even its probe has special-case mutation branches.
        if (held is Furniture || !held.canBeGivenAsGift() || held.questItem.Value || held.bigCraftable.Value
            || held.HasContextTag("not_giftable") || held.QualifiedItemId is "(O)StardropTea" or "(O)458" or "(O)460"
                or "(O)808" or "(O)809" or "(O)897" or "(O)277" or "(O)233" or "(O)71"
                or "(O)867" or "(O)864" or "(O)865" or "(O)866" or "(O)868" or "(O)869" or "(O)870")
            refusal = "gift_refused:item_not_giftable_or_special";
        if (BuildingTakesGrab(location, (int)tile.X, (int)tile.Y) || location.animals.Values.Any(a => a.GetBoundingBox().Intersects(rect)))
            refusal = "gift_refused:earlier_action_target";
        facts = facts with { TargetCandidates = hits.Count, OccupantAtTile = !NpcOccupantsExact(player, location, rect, npc),
            InputPolicyDetail = refusal };
        if (WorldExtras.Miss(WorldExtras.NpcGift, facts) is { } miss)
        {
            this.extrasMissTarget = (WorldExtras.NpcGift, source, (int)tile.X, (int)tile.Y);
            return InteractKindCapture.Missed(miss);
        }
        string qid = held.QualifiedItemId;
        int slot = player.CurrentToolIndex, day = Game1.Date.TotalDays;
        string npcFact = this.NpcFactIdFor(location, location.NameOrUniqueName, npc);
        string instance = this.giftTargetInstances.Pair(npc, held);
        string token = this.InteractToken(WorldExtras.NpcGift, instance + ":" + slot + ":" + held.Stack + ":" + qid, source, player, null);
        var target = new WorldInteractionTarget(token, source, (int)tile.X, (int)tile.Y,
            (int)player.Tile.X, (int)player.Tile.Y, player.FacingDirection, WorldExtras.NpcGift, null);
        if (!target.IsValid) return InteractKindCapture.Missed("target_invalid");
        bool Present() => this.ExtrasEnabled && ReferenceEquals(Game1.player, player) && ReferenceEquals(player.currentLocation, location)
            && Game1.Date.TotalDays == day && player.GetGrabTile() == tile && location.characters.Contains(npc)
            && this.NpcFactIdFor(location, location.NameOrUniqueName, npc) == npcFact
            && player.CurrentToolIndex == slot && ReferenceEquals(player.ActiveObject, held)
            && this.CaptureGiftKind(player, location, tile, held, this.ExtrasBaseFacts(player, tile, location))?.Target?.TargetToken == token;
        bool Expected(object? menu) => menu is DialogueBox { isQuestion: false } box
            && (ReferenceEquals(box.characterDialogue?.speaker, npc) || ReferenceEquals(Game1.currentSpeaker, npc));
        BgActionEffectFacts Sample()
        {
            bool valid = ReferenceEquals(Game1.player, player) && ReferenceEquals(player.currentLocation, location)
                && Game1.Date.TotalDays == day && location.characters.Contains(npc) && player.CurrentToolIndex == slot;
            player.friendshipData.TryGetValue(npc.Name, out Friendship? f);
            return SampleInteractEffect(() => valid, qid) with { ExpectedReceiver = Expected(Game1.activeClickableMenu),
                Extra = valid ? new BgExtraFacts(WorldExtras.NpcGift, false, InventoryCount(player, qid),
                    f?.GiftsToday ?? 0, qid, f?.GiftsThisWeek ?? 0) : null };
        }
        return new InteractKindCapture(target, new BgActionTarget(BgActionKind.ExtraReceiver, null, null, qid, Present, Sample)
            { ExtraKind = WorldExtras.NpcGift, ExpectedReceiver = Expected, PoseMayChangeAfterPress = true }, null, false);
    }
}
