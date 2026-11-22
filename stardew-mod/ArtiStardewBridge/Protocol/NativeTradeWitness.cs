namespace ArtiStardewBridge.Protocol;

/// <summary>Native trade facts; price never authorizes input.</summary>
public static class NativeTradeWitness
{
    // Only ordinary gold, inventory-producing buys/sells, including integral bundles.
    // Recipe acquisition, currencies/barter and callbacks have unknown effects here.
    public sealed record UnavailableFact(string target, object before, object after, long frame) {
        public string kind => "witness_unavailable";
    }
    public static UnavailableFact Unavailable(string reason, string target, long frame) => new(
        target, new { domain = "gold_inventory", reason }, new { outcome = "unknown" }, frame);
    public static object? Match(EconomyExpect expected, EconomyLedger before, EconomyLedger after,
        int quality, string target, long frame, bool sameBinding)
    {
        int units = expected.Kind == EconomyExpect.Sell ? before.ItemCount - after.ItemCount
            : after.ItemCount - before.ItemCount;
        int stack = expected.Kind == EconomyExpect.Buy ? Math.Max(1, expected.ItemStack) : 1;
        if (!sameBinding || expected.Kind is not (EconomyExpect.Sell or EconomyExpect.Buy)
            || units is < 1 or > 999 || units % stack != 0 || expected.UnitPrice < 0
            || expected.UnitPrice % stack != 0 || quality is not (0 or 1 or 2 or 4)) return null;
        var judged = EconomyJudge.Judge(expected with { Quantity = units / stack }, before, after);
        return !judged.Observed ? null : new { kind = judged.Effect, target,
            item = new { item_id = expected.ItemId, qty = units, quality }, qty = units,
            price = expected.UnitPrice / stack, before = new { money = before.Money, count = before.ItemCount },
            after = new { money = after.Money, count = after.ItemCount }, frame };
    }
}
