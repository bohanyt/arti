using StardewValley;
using ArtiStardewBridge.Protocol;
using StardewValley.Menus;
using Microsoft.Xna.Framework;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    sealed record VendorCounterSample(GameLocation Location, Vector2 Tile, Vector2 ActorTile, long Tick,
        string[] Action, string? Refusal);
    VendorCounterSample? vendorCounterSample;

    Dictionary<string, object>? ReadVendorCounterFacts()
    {
        if (this.vendorCounterSample is not { } s || Game1.player is not Farmer p
            || !ReferenceEquals(p.currentLocation, s.Location) || p.Tile != s.ActorTile
            || p.GetGrabTile() != s.Tile || Game1.ticks != s.Tick) return null;
        var result = new Dictionary<string, object> { ["tile"] = new[] { (int)s.Tile.X, (int)s.Tile.Y },
            ["native_action"] = string.Join(" ", s.Action),
            ["status"] = s.Refusal ?? "counter_available",
            ["fact"] = s.Refusal == "owner_absent" ? "penjaga toko tidak ada di posisi konter"
                : s.Refusal == "shop_hours" ? "di luar jam konter yang terbaca" : s.Refusal ?? "konter tersedia" };
        // Hours exist on OpenShop map actions. Do not invent hours for legacy actions or NPC schedules.
        if (s.Action.Length > 4 && s.Action[0] == "OpenShop"
            && int.TryParse(s.Action[3], out int open) && int.TryParse(s.Action[4], out int close))
            result["hours"] = new[] { open, close };
        return result;
    }

    // Native stock rows are recycled components named 0,1,..., not item labels.
    // Read the current scroll index and native stock; never invent a control or invoke a shop callback.
    static void EnrichVendorWidgets(GenericGraph graph)
    {
        if (graph.Dispatch is not ShopMenu shop) return;
        for (int i = 0; i < graph.Widgets.Count; i++)
        {
            var w = graph.Widgets[i];
            int button = w.Component is null ? -1 : shop.forSaleButtons.IndexOf(w.Component);
            int index = shop.currentItemIndex + button;
            if (button < 0 || index < 0 || index >= shop.forSale.Count) continue;
            var sale = shop.forSale[index];
            if (sale is null) // W1b B1: a shown stock part with no item is left unnamed (ReadGenericShop marks the rows incomplete)
                continue;
            string label = sale.DisplayName;
            if (shop.itemPriceAndStock.TryGetValue(sale, out var stock))
                label += " | price=" + stock.Price + " stock=" + stock.Stock + " currency=" + shop.currency;
            graph.Widgets[i] = w with { Item = sale as Item, Label = label, Kind = "stock" };
        }
    }

    GenericShopState? ReadGenericShop(GenericGraph graph)
    {
        if (graph.Dispatch is not ShopMenu shop) return null;
        var rows = new List<GenericShopRow>();
        bool complete = true; // ADR 246 review B1: a shown stock part whose row cannot be read leaves the rows incomplete
        for (int index = 0; index < shop.forSale.Count; index++)
        {
            var sale = shop.forSale[index];
            if (sale is null || !shop.itemPriceAndStock.TryGetValue(sale, out var info) || info is null)
            {
                int shown = index - shop.currentItemIndex;
                if (shown >= 0 && shown < shop.forSaleButtons.Count) // W1 B1: a shown button with no item is incomplete too
                    complete = false;
                continue;
            }
            var button = index - shop.currentItemIndex;
            int? control = button >= 0 && button < shop.forSaleButtons.Count
                ? graph.Widgets.FirstOrDefault(w => ReferenceEquals(w.Component, shop.forSaleButtons[button])
                    && GenericWidgetVisible(w))?.Id : null;
            string? trade = info.TradeItem is { Length: > 0 } t ? ItemRegistry.QualifyItemId(t) ?? t : null;
            int stock = StockOf(sale, info);
            rows.Add(new(index, sale.QualifiedItemId, sale.DisplayName ?? sale.Name ?? sale.QualifiedItemId,
                info.Price, stock < 0 ? int.MaxValue : stock, Math.Max(1, sale.Stack), sale is Item item ? InventoryCapacity(item) : 0,
                control, trade, info.TradeItemCount ?? 5, trade is null ? 0 : Game1.player.Items.CountId(trade), Math.Max(1, sale.maximumStackSize())));
        }
        var sales = new List<GenericSaleRow>();
        float percentage = this.ShopSellPercentage(shop);
        foreach (var w in graph.Widgets.Where(w => w.Kind == "slot" && w.Item is not null))
        {
            if (w.Component is null || !shop.inventory.inventory.Contains(w.Component)
                || !int.TryParse(w.Component.name, out int slot)) continue;
            var item = w.Item!;
            var sale = EconItemOf(item, percentage); // the existing native price/item reader (no new getter surface)
            sales.Add(new(w.Id, slot, sale.QualifiedId, sale.Name, sale.Stack,
                sale.Quality, sale.UnitSellPrice,
                shop.inventory.highlightMethod?.Invoke(item) ?? true));
        }
        return new(shop.ShopId ?? "", Game1.player.Money, shop.currency,
            ShopMenu.getPlayerCurrencyAmount(Game1.player, shop.currency), Game1.player.MaxItems,
            Game1.player.Items.Take(Game1.player.MaxItems).Count(i => i is not null),
            shop.safetyTimer <= 0 && !shop.readOnly, shop.heldItem is not null, rows, sales, complete);
    }

    static bool VendorProtected(GenericWidget w) => w.Dispatch is ShopMenu && w.Kind == "slot"
        && (w.Item is Tool || w.Item is StardewValley.Tools.MeleeWeapon
            || w.Item is StardewValley.Object obj && obj.questItem.Value);
}
