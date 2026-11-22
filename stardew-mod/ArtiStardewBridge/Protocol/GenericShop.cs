namespace ArtiStardewBridge.Protocol;

/// <summary>Read-only stock of the actual open menu. No vendor, item or itinerary allowlist.</summary>
public sealed record GenericShopRow(int Index, string Item, string Name, int Price, int Stock, int Stack,
    int Capacity, int? Control = null, string? TradeItem = null, int TradeCount = 0, int TradeHave = 0, int MaxPurchase = 999);
public sealed record GenericSaleRow(int Control, int Slot, string Item, string Name, int Stack, int Quality, int UnitPrice, bool Accepted);
/// <param name="Complete">ADR 246 (review B1): false when a stock part the menu shows has no readable row (its item or
/// its price could not be read). A press without a named choice is then refused, because the rows cannot tell what it
/// would buy.</param>
public sealed record GenericShopState(string Id, int Money, int Currency, int Balance, int Capacity, int Occupied,
    bool Ready, bool Held, IReadOnlyList<GenericShopRow> Rows, IReadOnlyList<GenericSaleRow>? Sales = null, bool Complete = true)
{
    public Dictionary<string, object> Wire()
    {
        var wire = GenericShop.Wire(Id, Money, Currency, Balance, Capacity, Occupied, Ready, Held, Rows);
        wire["sell_fields"] = new[] { "control", "slot", "item", "name", "stack", "quality", "unit_price", "accepted" };
        wire["sell"] = (Sales ?? Array.Empty<GenericSaleRow>()).Select(r => new object[] {
            r.Control, r.Slot, r.Item, r.Name, r.Stack, r.Quality, r.UnitPrice, r.Accepted }).ToArray();
        return wire;
    }
}

public static class GenericShop
{
    public static string? Refusal(GenericShopRow r, int balance, int quantity, bool ready, bool held) =>
        !ready ? "shop_not_ready" : held ? "cursor_item" : quantity is not (1 or 5 or 25) ? "quantity_requires_native_clicks"
        : quantity > r.MaxPurchase ? "native_stack_quantity_limit" : r.Control is null ? "stock_not_on_screen_scroll_to_row" : r.Stock <= 0 ? "sold_out"
        : r.Stock != int.MaxValue && r.Stock < quantity ? "stock_insufficient"
        : r.Price < 0 ? "price_unknown" : (long)r.Price * quantity > balance ? "currency_insufficient"
        : r.TradeItem is not null && (long)r.TradeCount * quantity > r.TradeHave ? "trade_item_insufficient"
        : r.Capacity < (long)r.Stack * quantity ? "inventory_capacity_insufficient" : null;

    public static Dictionary<string, object> Wire(string shop, int money, int currency, int balance,
        int capacity, int occupied, bool ready, bool held, IReadOnlyList<GenericShopRow> rows) => new()
    {
        ["id"] = shop, ["money"] = money, ["currency"] = currency, ["balance"] = balance,
        ["bag_capacity"] = capacity, ["bag_occupied"] = occupied, ["ready"] = ready,
        ["stock_fields"] = new[] { "index", "item", "name", "price", "stock", "stack", "capacity", "affordable", "control", "buy_quantities", "refused" },
        ["stock"] = rows.Select(r => new object?[] { r.Index, r.Item, r.Name, r.Price, r.Stock, r.Stack, r.Capacity,
            r.Price >= 0 && r.Price <= balance && (r.TradeItem is null || r.TradeHave >= r.TradeCount), r.Control,
            new[] { 1, 5, 25 }.Where(n => Refusal(r, balance, n, ready, held) is null).ToArray(),
            Refusal(r, balance, 1, ready, held) }).ToArray(),
        ["trades"] = rows.Where(r => r.TradeItem is not null).Select(r => new object[] { r.Index, r.TradeItem!, r.TradeCount, r.TradeHave }).ToArray(),
        ["contents"] = "All native forSale rows; unlimited stock=2147483647. Prices use currency; trade requirements are additional. Purchases may remain held until settled into the bag.",
    };
}
