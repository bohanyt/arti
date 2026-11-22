using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

// Native cleanup and trade sampling seams, also compiled by the no-game runtime harness.
public sealed partial class ModEntry
{
    bool UiMotorUnsafe {
        get {
            if (uiMotorFault is not { } f) return false;
            bool idle;
            try {
                var input = Game1.input;
                var mouse = input.GetMouseState(); var pad = input.GetGamePadState();
                idle = !uiMotorSeat.Staged && uiFaultLocks.All(m => !m.IsLockHeld())
                    && input.GetKeyboardState().GetPressedKeys().Length == 0
                    && mouse.LeftButton == ButtonState.Released && mouse.RightButton == ButtonState.Released
                    && mouse.MiddleButton == ButtonState.Released && mouse.XButton1 == ButtonState.Released && mouse.XButton2 == ButtonState.Released
                    && pad.ThumbSticks.Left == Vector2.Zero && pad.ThumbSticks.Right == Vector2.Zero
                    && pad.Triggers.Left == 0 && pad.Triggers.Right == 0
                    && !Enum.GetValues<Buttons>().Any(b => b != 0 && pad.IsButtonDown(b)) && ReferenceEquals(input, Game1.input);
            } catch { return true; } // Unknown native input cannot acknowledge cleanup.
            if (f.Recovered(CaptureReceiverFacts(false).Epoch, idle)) uiMotorFault = null;
            return !idle;
        }
    }
    static int StockOf(ISalable item, ItemStockInformation info) =>
        info.Stock == int.MaxValue || item.IsInfiniteStock() ? -1 : info.Stock;

    static Item? EconomyHeldItem(IClickableMenu top, IClickableMenu owner) => owner switch
    {
        CraftingPage page => page.heldItem,
        _ => top is ShopMenu shop ? shop.heldItem as Item : null,
    };

    sealed record TradeSample(object Game, ShopMenu Shop, Farmer Player, CommandInboxIdentity Identity, UiPadGamePort Port,
        EconomyExpect? Expected, EconomyLedger? Before, int Quality, string? Unavailable);
    TradeSample? BeginNativeTrade(GenericWidget? widget, IClickableMenu? receiver = null)
    {
        receiver ??= widget?.Dispatch;
        if (receiver is not ShopMenu shop) return null;
        string? unavailable = shop.currency != 0 ? "currency" : shop.readOnly ? "read_only"
            : shop.onSell is not null || shop.onPurchase is not null || shop.canPurchaseCheck is not null ? "callback" : null;
        EconomyExpect? expected = null;
        Item? item = widget?.Item;
        if (widget is not null && shop.inventory.inventory.Contains(widget.Component) && item is not null && shop.heldItem is null)
            expected = new(EconomyExpect.Sell, item.QualifiedItemId, 1,
                Math.Max(0, (int)(item.sellToStorePrice(-1L) * this.ShopSellPercentage(shop))));
        int button = shop.forSaleButtons.IndexOf(widget?.Component as ClickableTextureComponent);
        int row = shop.currentItemIndex + button;
        if (button >= 0 && row >= 0 && row < shop.forSale.Count && shop.forSale[row] is Item offered
            && shop.itemPriceAndStock.TryGetValue(offered, out var stock))
        {
            item = offered;
            expected = new(EconomyExpect.Buy, item.QualifiedItemId, 1, stock.Price, Math.Max(1, item.Stack),
                StockOf(offered, stock) >= 0, StockOf(offered, stock));
            unavailable ??= offered.IsRecipe ? "recipe" : stock.TradeItem is not null ? "barter" : null;
        }
        unavailable ??= expected is null || item is null ? "unclassified_control" : null;
        var port = new UiPadGamePort(this, shop);
        return new(Game1.game1, shop, Game1.player, this.CurrentCommandIdentity(), port, expected,
            unavailable is null ? port.ReadLedger(expected!) : null, item?.Quality ?? 0, unavailable);
    }
    void EndNativeTrade(TradeSample? sample, UiMotorLease lease)
    {
        if (sample is null) return;
        string target = lease.Attempt.Request.RequestId;
        try {
            bool same = ReferenceEquals(sample.Game, Game1.game1) && ReferenceEquals(sample.Shop, Game1.activeClickableMenu) && ReferenceEquals(sample.Player, Game1.player)
                && sample.Identity == this.CurrentCommandIdentity() with { ContextGeneration = sample.Identity.ContextGeneration } && lease.Delivered;
            lease.Attempt.TradeWitness = sample.Unavailable is null && same
                ? NativeTradeWitness.Match(sample.Expected!, sample.Before!, sample.Port.ReadLedger(sample.Expected!), sample.Quality, target, Game1.ticks, true)
                    ?? NativeTradeWitness.Unavailable("unmatched_readback", target, Game1.ticks)
                : NativeTradeWitness.Unavailable(same ? sample.Unavailable! : "binding_changed", target, Game1.ticks);
        } catch { lease.Attempt.TradeWitness = NativeTradeWitness.Unavailable("readback_fault", target, Game1.ticks); }
    }

    private sealed partial class UiPadGamePort
    {
        static int Count(string itemId)
        {
            int n = 0;
            foreach (Item? i in Game1.player.Items)
                if (i is not null && i.QualifiedItemId == itemId)
                    n += i.Stack;
            return n;
        }

        public EconomyLedger ReadLedger(EconomyExpect expect)
        {
            Item? held = EconomyHeldItem(this.top, this.Owner);
            int count = Count(expect.ItemId) + (held is not null && held.QualifiedItemId == expect.ItemId ? held.Stack : 0);
            int? stock = null;
            bool rowPresent = false;
            if (this.top is ShopMenu shop)
            {
                ISalable? entry = shop.forSale.FirstOrDefault(s => s is not null && s.QualifiedItemId == expect.ItemId);
                if (entry is not null && shop.itemPriceAndStock.TryGetValue(entry, out ItemStockInformation? info) && info is not null)
                {
                    rowPresent = true;
                    stock = StockOf(entry, info);
                }
            }
            Dictionary<string, int>? ingredients = expect.Ingredients is { Count: > 0 } list
                ? list.GroupBy(i => i.Key).ToDictionary(g => g.Key, g => Game1.player.getItemCount(g.Key), StringComparer.Ordinal)
                : null;
            return new EconomyLedger(Game1.player.Money, count, held?.QualifiedItemId, held?.Stack ?? 0, stock, rowPresent,
                Game1.player.toolBeingUpgraded.Value?.QualifiedItemId, ingredients, Game1.activeClickableMenu?.GetType().Name);
        }

    }
}
