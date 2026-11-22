using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

// Primitives v2 WS-G2: the shipping-bin receiver (ItemGrabMenu with shippingBin, ShippingBin.cs:262-275) as
// container_transfer modes ship_one / ship_stack. Unlike a chest, shipping does NOT rebuild the menu
// (ShippingBin.shipItem only moves the item, ShippingBin.cs:158-171): the receiver stays the same object, so the
// verdict expects ZERO receiver transitions. Readback = the farm bin count (Farm.getShippingBin(player)) and the
// player slot, both published in ui.context "shipping_bin". No ui_nav / ui_press on the bin (G1 reviewer rule).
// Per-click quantity of the right click (ship_one = exactly one) is a HYPOTHESIS until G2W (brief section 6 a).
// Evidence ceiling: UNIT_TESTED.

public static class ShippingBinSnapshot
{
    public const string BlockKey = "shipping_bin";
    public const int MaxBinEntries = 72;

    public static Dictionary<string, object> Unavailable(string reason) => new() { ["available"] = false, ["reason"] = reason };

    /// <summary>
    /// Bounded bin facts: actor slots (with the ship policy per slot), aggregated bin counts per item / quality,
    /// the cursor item, and a readback token over all of it. Bounds stay runtime-only (never in a prompt).
    /// </summary>
    public static Dictionary<string, object> Build(string receiverToken, string containerToken, string sourceFactId,
        IReadOnlyList<PresentedInventorySlot> actor, IReadOnlyDictionary<int, EconItem> econ,
        IReadOnlyDictionary<string, int> binCounts, PresentedItem? cursor, ItemPolicyConfig policy)
    {
        if (string.IsNullOrWhiteSpace(receiverToken) || receiverToken.Length > 128 || string.IsNullOrWhiteSpace(containerToken)
            || containerToken.Length > 128 || string.IsNullOrWhiteSpace(sourceFactId) || sourceFactId.Length > 512
            || actor.Count is < 1 or > ContainerUiSnapshot.MaxActorSlots || binCounts.Count > MaxBinEntries
            || actor.Select(s => s.Slot).Distinct().Count() != actor.Count || cursor is { IsValid: false }
            || actor.Any(s => s.Item is { IsValid: false } || s.Slot < 0 || s.Slot >= ContainerUiSnapshot.MaxActorSlots))
            return Unavailable("invalid_or_unbounded_receiver");
        object[] slots = actor.OrderBy(s => s.Slot).Select(s =>
        {
            var d = new Dictionary<string, object>
            {
                ["slot"] = s.Slot, ["side"] = "actor", ["item"] = s.Item?.Facts()!,
                ["bounds"] = new[] { s.X, s.Y, s.Width, s.Height },
                ["target_token"] = EconomyHash.Of(JsonSerializer.Serialize(new object[]
                    { receiverToken, containerToken, "actor", s.Slot, s.X, s.Y, s.Width, s.Height, s.Item! })),
            };
            if (s.Item is not null && econ.TryGetValue(s.Slot, out EconItem? item))
            {
                d["name"] = UiPadTable.Trim(item.Name);
                d["ship_policy"] = ItemPolicy.RefuseDispose(policy, ItemPolicy.Ship, item, 1) is { } why ? "refused:" + why : "ok";
            }
            return (object)d;
        }).ToArray();
        object[] bin = binCounts.OrderBy(kv => kv.Key, StringComparer.Ordinal).Select(kv =>
        {
            string[] parts = kv.Key.Split('|');
            return (object)new Dictionary<string, object>
            {
                ["qualified_item_id"] = parts[0], ["quality"] = parts.Length > 1 && int.TryParse(parts[1], out int q) ? q : 0,
                ["count"] = kv.Value,
            };
        }).ToArray();
        var facts = new Dictionary<string, object>
        {
            ["available"] = true, ["receiver_token"] = receiverToken, ["container_token"] = containerToken,
            ["source_fact_id"] = sourceFactId, ["actor_slots"] = slots, ["bin"] = bin, ["cursor"] = cursor?.Facts()!,
        };
        facts["readback_token"] = EconomyHash.Of(JsonSerializer.Serialize(facts, ProtocolJson.Options));
        if (JsonSerializer.SerializeToUtf8Bytes(facts, ProtocolJson.Options).Length > ContainerUiSnapshot.MaxBytes)
            return Unavailable("receiver_snapshot_too_large");
        return facts;
    }

    public static string BinKey(string qualifiedItemId, int quality) => qualifiedItemId + "|" + quality;
}

/// <summary>One bin readback (counts of the wanted item / quality on both sides).</summary>
public sealed record ShippingBinReadback(string ReceiverToken, string ContainerToken, string SourceFactId, string ReadbackToken,
    long Generation, int ActorCount, int BinCount, bool CursorClear, int? SourceStack)
{
    public static ShippingBinReadback? Read(JsonElement facts, ContainerTransferTarget wanted, long generation,
        out ContainerTransferTarget? current)
    {
        current = null;
        if (facts.ValueKind != JsonValueKind.Object || !facts.TryGetProperty("available", out var available)
            || available.ValueKind != JsonValueKind.True)
            return null;
        JsonElement[] actor = facts.GetProperty("actor_slots").EnumerateArray().ToArray();
        if (actor.Length is < 1 or > ContainerUiSnapshot.MaxActorSlots)
            return null;
        bool Match(JsonElement item) => item.ValueKind == JsonValueKind.Object
            && item.GetProperty("qualified_item_id").GetString() == wanted.QualifiedItemId
            && item.GetProperty("quality").GetInt32() == wanted.Quality;
        int actorCount = actor.Sum(s => Match(s.GetProperty("item")) ? s.GetProperty("item").GetProperty("stack").GetInt32() : 0);
        int binCount = facts.GetProperty("bin").EnumerateArray()
            .Where(b => b.GetProperty("qualified_item_id").GetString() == wanted.QualifiedItemId && b.GetProperty("quality").GetInt32() == wanted.Quality)
            .Sum(b => b.GetProperty("count").GetInt32());
        JsonElement source = actor.SingleOrDefault(s => s.GetProperty("slot").GetInt32() == wanted.Slot);
        if (source.ValueKind == JsonValueKind.Undefined)
            return null;
        string Text(string k) => facts.GetProperty(k).GetString()!;
        JsonElement item = source.GetProperty("item");
        if (Match(item))
            current = new ContainerTransferTarget(Text("receiver_token"), Text("container_token"), Text("readback_token"),
                Text("source_fact_id"), source.GetProperty("target_token").GetString()!, wanted.Slot, wanted.QualifiedItemId,
                item.GetProperty("stack").GetInt32(), wanted.Quality, wanted.OwnerCeiling, wanted.Quantity, wanted.Mode);
        return new ShippingBinReadback(Text("receiver_token"), Text("container_token"), Text("source_fact_id"), Text("readback_token"),
            generation, actorCount, binCount, facts.GetProperty("cursor").ValueKind == JsonValueKind.Null,
            Match(item) ? item.GetProperty("stack").GetInt32() : item.ValueKind == JsonValueKind.Null ? 0 : null);
    }
}

/// <summary>
/// ship_one / ship_stack executor: one click, ZERO receiver transitions, readback paired on both sides
/// (player slot - n, bin + n). Any transition, a cursor item left, or a different delta = uncertain.
/// </summary>
public sealed class ShippingBinInput
{
    bool consumed, fault;
    int transitions;

    public void ObserveReceiverTransition(string? from, string? to)
    {
        transitions++;
        fault = true; // shipping never replaces the bin menu (ShippingBin.shipItem, ShippingBin.cs:158-171)
    }

    public ContainerTransferVerdict Execute(ContainerTransferTarget wanted, ContainerTransferTarget? current, ShippingBinReadback before,
        bool eligible, string? policyRefusal, Action click, Func<ShippingBinReadback?> read)
    {
        if (consumed)
            return new("TransferRejected", false, true, 0, false, null, null, null, Detail: "consumed");
        consumed = true;
        string? refuse = !eligible ? "not_eligible" : !wanted.IsShip ? "not_a_ship_mode" : !wanted.IsValid ? "invalid_target"
            : policyRefusal is { } p ? "item_policy:" + p : current != wanted ? "target_changed" : !before.CursorClear ? "cursor_item"
            : before.ReceiverToken != wanted.ReceiverToken || before.ContainerToken != wanted.ContainerToken
              || before.SourceFactId != wanted.SourceFactId || before.ReadbackToken != wanted.ReadbackToken ? "binding_changed"
            : before.SourceStack != wanted.Stack || before.ActorCount < wanted.Amount ? "source_changed" : null;
        if (refuse is not null)
            return new("TransferRejected", false, true, 0, false, null, null, null, Detail: refuse);
        ShippingBinReadback? first = null;
        try
        {
            click();
            first = read();
        }
        catch
        {
            fault = true;
        }
        bool sameReceiver = first is not null && first.ReceiverToken == before.ReceiverToken && transitions == 0;
        int wantSource = wanted.Stack - wanted.Amount;
        bool paired = !fault && sameReceiver && first!.CursorClear && first.SourceStack == wantSource
            && before.ActorCount - first.ActorCount == wanted.Amount && first.BinCount - before.BinCount == wanted.Amount;
        string detail = first is null ? "readback_unavailable"
            : paired ? "shipped:" + wanted.Amount
            : "actor_delta:" + (first.ActorCount - before.ActorCount) + ";bin_delta:" + (first.BinCount - before.BinCount)
              + (sameReceiver ? "" : ";receiver_changed") + (first.CursorClear ? "" : ";cursor_item");
        bool none = !fault && first is not null && sameReceiver && first.ActorCount == before.ActorCount && first.BinCount == before.BinCount
            && first.CursorClear;
        return new(paired ? "TransferObserved" : none ? "TransferNoEffect" : "TransferUncertain", paired, true, 1, false,
            first?.ReceiverToken, first?.Generation, first?.ReadbackToken, Detail: detail, NoTransitionExpected: true);
    }
}
