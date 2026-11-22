namespace ArtiStardewBridge.Protocol;

/// <summary>Gift captures pin two live objects, not one active menu receiver.
/// Weak keys preserve reference identity without retaining NPCs/items after unload.
/// Session/runtime/location/context/pose remain in the outer interaction token.</summary>
public sealed class GiftTargetInstances
{
    sealed record Identity(long Serial);
    readonly System.Runtime.CompilerServices.ConditionalWeakTable<object, Identity> identities = new();
    long serial;

    string Token(object instance) => "gi" + this.identities.GetValue(instance,
        _ => new Identity(++this.serial)).Serial;

    public string Pair(object npc, object held) => this.Token(npc) + ":" + this.Token(held);

    /// <summary>One weakly pinned world instance (FIXB60 house/animal observation).</summary>
    public string Single(object instance) => this.Token(instance);
}

// FIXB44: read-only facts, carried by the registered extras target/effect envelopes.
public sealed record GiftNpcFacts(bool Villager, bool Social, bool ReceivesGifts, bool Sleeping,
    bool Birthday, int GiftsToday, int GiftsThisWeek, bool Divorced, bool Spouse);

public static class GiftRules
{
    public static string? Miss(GiftNpcFacts f) =>
        !f.Villager || !f.Social || !f.ReceivesGifts ? "gift_refused:npc_not_giftable"
        : f.Sleeping ? "gift_refused:npc_sleeping"
        : f.Birthday ? "gift_refused:birthday"
        : f.GiftsToday > 0 ? "gift_refused:already_gifted_today"
        : f.GiftsThisWeek >= 2 ? "gift_refused:weekly_limit"
        : f.Divorced || f.Spouse ? "gift_refused:relationship_special_case" : null;
}

public static class BgGiftEffect
{
    public static BgActionEffectVerdict Classify(BgActionEffectFacts before, BgActionEffectFacts now)
    {
        if (before.Extra is not { Kind: WorldExtras.NpcGift } b || now.Extra is not { Kind: WorldExtras.NpcGift } n
            || b.ItemId != n.ItemId || b.ItemId.Length == 0 || b.GiftsThisWeek is null || n.GiftsThisWeek is null)
            return No("gift_effect_unreadable");
        // A quest delivery or dialogue alone cannot increment BOTH gift counters.
        if (!now.MenuOpen || !now.ExpectedReceiver || b.Value != 0 || n.Value != 1
            || n.GiftsThisWeek != b.GiftsThisWeek + 1 || n.Count != b.Count - 1)
            return No("gift_effect_mismatch");
        return new(WorldExtras.GiftGiven, true, null, null, -1);
    }
    static BgActionEffectVerdict No(string reason) => new(BgHoldCompletion.InteractionUncertain, false, reason, null, 0);
}
