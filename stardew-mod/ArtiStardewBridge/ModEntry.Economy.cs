using System.Globalization;
using System.Reflection;
using System.Text.Json;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework.Input;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Buildings;
using StardewValley.Menus;
using StardewValley.Internal;
using StardewValley.Objects;
using StardewValley.Tools;

namespace ArtiStardewBridge;

/// <summary>
/// Primitives v2 WS-G2 wiring (game thread only): the economy rows of the G1 menu layer (ShopMenu buy / sell,
/// CraftingPage craft incl. standalone cooking, Clint's upgrade shop) and the container_transfer modes quick /
/// take_out (pinned chest) and ship_one / ship_stack (shipping bin). The tables, policy, ledger judge and verdicts
/// are pure (<see cref="EconomyPlans"/>, <see cref="ItemPolicy"/>, <see cref="EconomyJudge"/>,
/// <see cref="ShippingBinInput"/>); this file reads the game with field reads and pure getters only and performs
/// the calls. Modifiers are written into <c>Game1.oldKBState</c> for exactly one synchronous call and restored in
/// finally (<see cref="KbOverride"/>; PRB-5). The private <c>ShopMenu.tryToPurchaseItem</c> is bound by name + exact
/// parameter types and only on the pinned assembly MVID (PRB-6); any miss fails closed. Flag
/// <c>EconomyMenusEnabled</c> (default OFF). Evidence ceiling: UNIT_TESTED; HYPOTHESIS until the G2 witnesses.
/// </summary>
// getItemCount / getItemCountInList are what CraftingRecipe.doesFarmerHaveIngredientsInInventory uses (category keys
// such as "-5" included), so the published "have" counts match the vanilla craftable check exactly.
#pragma warning disable CS0618
public sealed partial class ModEntry
{
    static readonly Guid PinnedGameMvid = BackgroundMoveRuntime.PinnedGameMvid;

    static bool GamePinned => typeof(Game1).Module.ModuleVersionId == PinnedGameMvid;

    /// <summary>ShopMenu.tryToPurchaseItem(ISalable, ISalable, int, int, int) : bool, or null (unbound = refused).</summary>
    static readonly Lazy<MethodInfo?> TryToPurchaseItemMethod = new(() =>
    {
        if (!GamePinned)
            return null;
        MethodInfo? m = typeof(ShopMenu).GetMethod(EconomyReflection.TryToPurchaseItem, BindingFlags.Instance | BindingFlags.NonPublic,
            null, new[] { typeof(ISalable), typeof(ISalable), typeof(int), typeof(int), typeof(int) }, null);
        return m is not null && EconomyReflection.Bound(true, true, m.GetParameters().Select(p => p.ParameterType.Name).ToArray(),
            m.ReturnType.Name) ? m : null;
    });

    static readonly Lazy<FieldInfo?> SellPercentageField = new(() =>
        typeof(ShopMenu).GetField(EconomyReflection.SellPercentage, BindingFlags.Instance | BindingFlags.NonPublic) is { FieldType: var t } f
        && t == typeof(float) ? f : null);

    /// <summary>The WS-G1 admission + the WS-G2 flag.</summary>
    bool EconomyReady => this.UiPadReady && this.config.EconomyMenusEnabled;

    /// <summary>Shipping-bin container_transfer modes: inactive farmhand (G1 pad path admission) + transfer flags.</summary>
    bool BinTransferReady => this.config.Enabled && this.config.ContainerTransferInputEnabled && this.config.EconomyMenusEnabled
        && this.transferObserverReady && !this.transferDoubt.EffectDoubt && !TransferHeldEvidence.Pending(this.transferDoubt, NowUnixMs()) && this.UiPadReady && GamePinned
        && Game1.options?.SnappyMenus == false && Game1.keyboardDispatcher?.Subscriber is null;

    /// <summary>The container_transfer family admission: v1 (chest) or the WS-G2 bin.</summary>
    bool AnyContainerTransferReady => this.ContainerTransferReady || this.BinTransferReady;

    // ------------------------------------------------------------------ read-only facts

    static bool IsEquipment(Item item) => item is Ring or Boots or Hat or Clothing or Furniture or StardewValley.Objects.Trinkets.Trinket;

    /// <summary>
    /// Units of <paramref name="item"/> the inventory can take (pure: Farmer.couldInventoryAcceptThisItem shows a red HUD
    /// message): an empty slot takes a full stack, a stackable same item its remaining room (Farmer.cs:4714-4748).
    /// </summary>
    static int InventoryCapacity(Item item)
    {
        Farmer p = Game1.player;
        if (item.IsRecipe)
            return 99999;
        int max = Math.Max(1, item.maximumStackSize());
        long capacity = 0;
        for (int i = 0; i < p.MaxItems && i < p.Items.Count; i++)
        {
            Item? slot = p.Items[i];
            if (slot is null)
                capacity += item is StardewValley.Object ? max : 1;
            else if (item is StardewValley.Object && slot is StardewValley.Object o && o.canStackWith(item))
                capacity += Math.Max(0, slot.maximumStackSize() - slot.Stack);
        }
        return (int)Math.Min(capacity, 99999);
    }

    static bool FitsInventory(Item item, int stack) => InventoryCapacity(item) >= stack;

    static EconItem EconItemOf(Item item, float sellPercentage) => new(
        QualifiedId: item.QualifiedItemId, Name: item.DisplayName ?? item.Name ?? "", Stack: item.Stack, Quality: item.Quality,
        Category: item.Category, IsTool: item is Tool, QuestItem: item is StardewValley.Object o && o.questItem.Value,
        SpecialItem: item.specialItem, Equipment: IsEquipment(item), IsRecipe: item.IsRecipe,
        UnitSellPrice: Math.Max(0, (int)(item.sellToStorePrice(-1L) * sellPercentage)), CanBeTrashed: item.canBeTrashed(),
        Shippable: item.canBeShipped(), MaxStack: Math.Max(1, item.maximumStackSize()));

    /// <summary>The player slots of an InventoryMenu as economy slots (ShopMenu: sellable = its highlightItemToSell).</summary>
    static List<SellSlot> EconomySlots(InventoryMenu? menu, float pct, bool sellable)
    {
        var slots = new List<SellSlot>();
        if (menu?.inventory is not { } buttons || menu.actualInventory is not { } actual)
            return slots;
        foreach (ClickableComponent b in buttons)
        {
            if (!int.TryParse(b.name, NumberStyles.Integer, CultureInfo.InvariantCulture, out int slot) || slot < 0 || slot >= actual.Count)
                continue;
            Item? item = actual[slot];
            bool canSell = sellable && item is not null && (menu.highlightMethod?.Invoke(item) ?? true);
            slots.Add(new SellSlot(b.myID, slot, item is null ? null : EconItemOf(item, pct), canSell));
        }
        return slots;
    }

    float ShopSellPercentage(ShopMenu shop)
    {
        try { return SellPercentageField.Value?.GetValue(shop) is float f ? f : 1f; }
        catch { return 1f; }
    }

    EconomyFacts? ReadEconomyFacts(IClickableMenu top, IClickableMenu owner)
    {
        if (!this.config.EconomyMenusEnabled)
            return null;
        ItemPolicyConfig policy = this.config.EconomyItemPolicy ?? new ItemPolicyConfig();
        Farmer player = Game1.player;
        string? upgrading = player.toolBeingUpgraded.Value?.QualifiedItemId;
        if (top is ShopMenu shop && ReferenceEquals(owner, top))
        {
            float pct = this.ShopSellPercentage(shop);
            var rows = new List<ShopRow>();
            for (int i = 0; i < shop.forSale.Count && i < ShopFactsTable.MaxRows; i++)
            {
                ISalable item = shop.forSale[i];
                if (item is null || !shop.itemPriceAndStock.TryGetValue(item, out ItemStockInformation? info) || info is null)
                    continue;
                int k = i - shop.currentItemIndex;
                int? component = k >= 0 && k < Math.Min(ShopFactsTable.StockRows, shop.forSaleButtons.Count) ? ShopFactsTable.StockRowBase + k : null;
                string? trade = info.TradeItem is { Length: > 0 } t ? ItemRegistry.QualifyItemId(t) ?? t : null;
                int tradeCount = info.TradeItemCount ?? 5;
                string? requiredTool = shop.ShopId == ShopFactsTable.ClintUpgrade && item is Tool upgrade
                    ? ShopBuilder.GetToolUpgradeData(upgrade.GetToolData(), player)?.RequireToolId : null;
                requiredTool = requiredTool is null ? null : ItemRegistry.QualifyItemId(requiredTool);
                rows.Add(new ShopRow(i, component, item.QualifiedItemId, item.DisplayName ?? item.Name ?? "", info.Price,
                    StockOf(item, info), Math.Max(1, item.Stack), item.maximumStackSize(), item is Tool, item.IsRecipe, trade, tradeCount,
                    item is Item asItem ? InventoryCapacity(asItem) : 0,
                    trade is null ? 0 : player.Items.CountId(trade), requiredTool,
                    requiredTool is null ? 0 : player.Items.CountId(requiredTool)));
            }
            List<SellSlot> slots = EconomySlots(shop.inventory, pct, sellable: true);
            return new EconomyFacts(EconomyFacts.KindShop, player.Money, policy, shop.ShopId, shop.currency, shop.safetyTimer, shop.readOnly,
                CustomSell: shop.onSell is not null, HasPurchaseCheck: shop.canPurchaseCheck is not null,
                ReflectionBound: TryToPurchaseItemMethod.Value is not null, Rows: rows, Slots: slots, ToolUpgrading: upgrading,
                HeldQuality: (shop.heldItem as Item)?.Quality ?? 0);
        }
        if (owner is CraftingPage page && page.pagesOfCraftingRecipes is { Count: > 0 } pages
            && page.currentCraftingPage >= 0 && page.currentCraftingPage < pages.Count)
        {
            IList<Item>? extra = null;
            if (page._materialContainers is { } containers)
            {
                var list = new List<Item>();
                foreach (var inv in containers)
                    list.AddRange(inv.Where(x => x is not null));
                extra = list;
            }
            var recipes = new List<RecipeRow>();
            foreach (var kv in pages[page.currentCraftingPage])
            {
                CraftingRecipe recipe = kv.Value;
                if (recipe is null || recipes.Count >= ShopFactsTable.MaxRecipes)
                    continue;
                var ingredients = recipe.recipeList.Select(r => new RecipeIngredient(r.Key, r.Value,
                    player.getItemCount(r.Key) + (extra is null ? 0 : player.getItemCountInList(extra, r.Key)))).ToList();
                bool ghosted = kv.Key.hoverText == "ghosted";
                // Multi-output recipes pick an output at random (CraftingRecipe.createItem -> GetItemData()): not craftable here.
                bool single = recipe.itemToProduce.Count == 1;
                recipes.Add(new RecipeRow(kv.Key.myID, recipe.name, recipe.DisplayName ?? recipe.name,
                    recipe.GetItemData(useFirst: true).QualifiedItemId, Math.Max(1, recipe.numberProducedPerCraft),
                    single && !ghosted && recipe.doesFarmerHaveIngredientsInInventory(extra), ghosted || !single, ingredients));
            }
            return new EconomyFacts(EconomyFacts.KindCrafting, player.Money, policy, Cooking: page.cooking, Recipes: recipes,
                ToolUpgrading: upgrading, Slots: EconomySlots(page.inventory, 1f, sellable: false),
                HeldQuality: page.heldItem?.Quality ?? 0);
        }
        return null;
    }

    /// <summary>Component kinds / labels from the economy facts (stock rows, recipes); everything else unchanged.</summary>
    static List<UiPadComponent> LabelEconomyComponents(List<UiPadComponent> comps, EconomyFacts? e)
    {
        if (e is null)
            return comps;
        return comps.Select(c =>
        {
            if (e.RowAt(c.Id) is { } row)
                return c with { Kind = "stock", Label = UiPadTable.Trim(row.Name + " " + row.Price + "g"), ItemId = row.ItemId, Stack = row.ItemStack };
            if (e.RecipeAt(c.Id) is { } recipe)
                return c with { Kind = "recipe", Label = UiPadTable.Trim(recipe.DisplayName + (recipe.Ghosted ? " (unknown)" : recipe.Craftable ? "" : " (missing ingredients)")),
                    ItemId = recipe.ProducesId, Stack = recipe.Produces };
            if (e.Kind == EconomyFacts.KindCrafting && c.Id is ShopFactsTable.PageUp or ShopFactsTable.PageDown)
                return c with { Label = c.Id == ShopFactsTable.PageUp ? "previous recipe page (A)" : "next recipe page (A)" };
            return c;
        }).ToList();
    }

    /// <summary>The held item of the economy menus (ShopMenu / CraftingPage keep their own heldItem field).</summary>
    // ------------------------------------------------------------------ the IEconomyPort half of the game port

    private sealed partial class UiPadGamePort
    {
        public void WithModifiers(KbModifiers mods, Action call) => WithKeyboard(mods, call);

        public void ShopBuyDirect(int rowIndex, int qty, string itemId)
        {
            AssertWarpGuarded();
            if (this.top is not ShopMenu shop || TryToPurchaseItemMethod.Value is not { } method)
                throw new InvalidOperationException("tryToPurchaseItem unbound");
            if (rowIndex < 0 || rowIndex >= shop.forSale.Count || shop.canPurchaseCheck is not null || shop.safetyTimer > 0 || shop.readOnly)
                throw new InvalidOperationException("shop row unavailable");
            ISalable item = shop.forSale[rowIndex];
            // Review fix: the row must still hold the item the plan priced (scroll / sell-out / rebuild in between).
            if (item is null || item.QualifiedItemId != itemId)
                throw new InvalidOperationException("shop row changed");
            int k = Math.Clamp(rowIndex - shop.currentItemIndex, 0, Math.Max(0, shop.forSaleButtons.Count - 1));
            var center = shop.forSaleButtons.Count > 0 ? shop.forSaleButtons[k].bounds.Center
                : new Microsoft.Xna.Framework.Point(shop.xPositionOnScreen, shop.yPositionOnScreen);
            InUiMode(() =>
            {
                // The vanilla buy loop body (ShopMenu.cs:1101-1140) with stockToBuy = qty instead of the modifier quantity.
                if (method.Invoke(shop, new object?[] { item, shop.heldItem, qty, center.X, center.Y }) is true)
                {
                    shop.itemPriceAndStock.Remove(item);
                    shop.forSale.Remove(item);
                }
                shop.currentItemIndex = Math.Max(0, Math.Min(shop.forSale.Count - 4, shop.currentItemIndex));
                shop.updateSaleButtonNeighbors();
            });
        }

        public void ShopStoreHeld()
        {
            // ShopMenu.cs:1132 (the SnappyMenus tail): the pad-mode farmhand runs with snappyMenus off, so the bought
            // item would otherwise stay on the cursor (B refused while held).
            if (this.top is ShopMenu shop && shop.heldItem is Item held && Game1.activeClickableMenu is ShopMenu
                && FitsInventory(held, held.Stack) && Game1.player.addItemToInventoryBool(held))
                shop.heldItem = null;
            // FIXB3 review MAJOR: the crafted item, CraftingPage.cs:499-503 (the gamepadControls tail): into the inventory
            // when it fits (no HUD read: FitsInventory); otherwise it stays held and the runtime reports the craft uncertain.
            else if (this.Owner is CraftingPage page && page.heldItem is Item crafted
                     && FitsInventory(crafted, crafted.Stack) && Game1.player.addItemToInventoryBool(crafted))
                page.heldItem = null;
        }
    }

    // ------------------------------------------------------------------ shipping bin (ui.context "shipping_bin")

    static bool IsShippingBinMenu(IClickableMenu? menu, out ItemGrabMenu grab, out ShippingBin bin)
    {
        grab = null!;
        bin = null!;
        if (menu is not ItemGrabMenu g || g.GetType() != typeof(ItemGrabMenu) || !g.shippingBin || g.context is not ShippingBin b
            || g.inventory is null || !ReferenceEquals(g.inventory.actualInventory, Game1.player.Items))
            return false;
        grab = g;
        bin = b;
        return true;
    }

    void EnrichShippingBinContext(Dictionary<string, object> snapshot)
    {
        if (!this.config.EconomyMenusEnabled || Context.IsMainPlayer || Context.IsSplitScreen || !Context.IsWorldReady
            || !IsShippingBinMenu(Game1.activeClickableMenu, out ItemGrabMenu menu, out ShippingBin bin))
            return;
        Dictionary<string, object>? caps = snapshot.GetValueOrDefault("capabilities") as Dictionary<string, object>;
        try
        {
            if (snapshot.GetValueOrDefault("interactive") is not Dictionary<string, object> interactive
                || interactive.GetValueOrDefault("receiver_token") is not string receiver)
            {
                snapshot[ShippingBinSnapshot.BlockKey] = ShippingBinSnapshot.Unavailable("no_receiver_token");
                return;
            }
            string location = Game1.getFarm()?.NameOrUniqueName ?? "Farm";
            string source = "building:" + location + ":" + bin.tileX.Value + ":" + bin.tileY.Value + ":ShippingBin";
            string container = "bin:" + EconomyHash.Of(source)[..24];
            IReadOnlyList<PresentedInventorySlot> actor = CapturePresentedSlots(menu.inventory, ContainerUiSnapshot.MaxActorSlots);
            var econ = new Dictionary<int, EconItem>();
            for (int i = 0; i < Game1.player.Items.Count; i++)
                if (Game1.player.Items[i] is { } item)
                    econ[i] = EconItemOf(item, 1f) with { Shippable = item.canBeShipped() && (menu.inventory.highlightMethod?.Invoke(item) ?? true) };
            var counts = new Dictionary<string, int>(StringComparer.Ordinal);
            foreach (Item? i in Game1.getFarm().getShippingBin(Game1.player))
                if (i is not null)
                {
                    string key = ShippingBinSnapshot.BinKey(i.QualifiedItemId, i.Quality);
                    counts[key] = counts.GetValueOrDefault(key) + i.Stack;
                }
            snapshot[ShippingBinSnapshot.BlockKey] = counts.Count > ShippingBinSnapshot.MaxBinEntries
                ? ShippingBinSnapshot.Unavailable("bin_too_large")
                : ShippingBinSnapshot.Build(receiver, container, source, actor, econ, counts, PresentedItemOf(menu.heldItem),
                    this.config.EconomyItemPolicy ?? new ItemPolicyConfig());
            if (caps is not null)
                caps[ShippingBinSnapshot.BlockKey] = new Dictionary<string, object>
                {
                    ["implemented"] = true, ["available"] = this.BinTransferReady && menu.heldItem is null,
                    ["modes"] = new[] { ContainerTransferTarget.ModeShipOne, ContainerTransferTarget.ModeShipStack },
                    ["reason"] = this.BinTransferReady ? "ship_policy_and_current_slot_revalidated" : "backend_disabled",
                };
        }
        catch (Exception)
        {
            snapshot[ShippingBinSnapshot.BlockKey] = ShippingBinSnapshot.Unavailable("current_receiver_read_failed");
        }
    }

    JsonElement CaptureShippingBinFacts()
    {
        var snapshot = new Dictionary<string, object>
        {
            ["interactive"] = new Dictionary<string, object> { ["receiver_token"] = this.ReceiverTokenOf(Game1.activeClickableMenu)! },
        };
        this.EnrichShippingBinContext(snapshot);
        return JsonSerializer.SerializeToElement(snapshot.GetValueOrDefault(ShippingBinSnapshot.BlockKey), ProtocolJson.Options);
    }

    static void WithKeyboard(KbModifiers mods, Action call) =>
        KbOverride.RunModifiers(() => Game1.oldKBState, s => Game1.oldKBState = s, keys => new KeyboardState(keys), Keys.LeftShift,
            Keys.LeftControl, mods, call);

    // ------------------------------------------------------------------ container_transfer modes

    void RejectTransferMode(MenuStepCommandRequest request, CommandInboxIdentity pin, string why)
    {
        this.Monitor.Log($"container_transfer mode REJECT request_id={request.RequestId} mode={request.TransferTarget?.Mode} reason={why}",
            LogLevel.Warn);
        this.WriteCommandResult(this.commandInbox.CompleteRejected(request, CommandRejectReason.BindingMismatch, pin, NowUnixMs()));
    }

    /// <summary>WS-G2 container_transfer with a mode (quick / take_out on the pinned chest, ship_one / ship_stack on the bin).</summary>
    void ExecuteContainerTransferMode(MenuStepCommandRequest request)
    {
        this.ApplyActionOwnerRecords();
        CommandInboxIdentity pin = this.CurrentCommandIdentity();
        if (request.TransferTarget is not { Mode: not null } target || !this.config.EconomyMenusEnabled
            || this.actionOwner.ReleaseRequested
            || !this.actionOwner.CanAdmitLeasedInputOnce(pin, NowUnixMs(), request.RuntimeInstanceId, request.OwnerId,
                request.Generation, request.ControlSeq, out _)
            || !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request, pin, Context.IsWorldReady,
                Context.IsPlayerFree, Context.CanPlayerMove, out _))
        {
            this.RejectTransferMode(request, pin, "admission");
            return;
        }
        if (target.IsShip)
            this.ExecuteShip(request, pin, target);
        else
            this.ExecuteChestMode(request, pin, target);
    }

    TransferSlotBinding BindTransferSlot(MenuStepCommandRequest request)
    {
        var slot = this.commandInbox.ExecutionSlot; var ticket = slot.ActiveAttempt;
        return new(() => this.CaptureReceiverFacts().Epoch, (now, pinned) => slot.Owns(request)
            && slot.CanContinue(ticket, "moving", new ArtiCore.Pure.ValidityFacts(Context.IsWorldReady, now, pinned == now)));
    }

    void ExecuteShip(MenuStepCommandRequest request, CommandInboxIdentity pin, ContainerTransferTarget target)
    {
        if (!this.BinTransferReady || !IsShippingBinMenu(Game1.activeClickableMenu, out ItemGrabMenu original, out _)
            || original.heldItem is not null || original.currentlySnappedComponent is not null)
        {
            this.RejectTransferMode(request, pin, "bin_not_ready");
            return;
        }
        ShippingBinReadback? before;
        ContainerTransferTarget? current;
        ClickableComponent? button;
        string? policy;
        try
        {
            before = ShippingBinReadback.Read(this.CaptureShippingBinFacts(), target, pin.ContextGeneration, out current);
            button = original.inventory.inventory.SingleOrDefault(b => b.name == target.Slot.ToString(CultureInfo.InvariantCulture));
            Item? item = target.Slot < Game1.player.Items.Count ? Game1.player.Items[target.Slot] : null;
            policy = item is null ? "empty_slot"
                : !(original.inventory.highlightMethod?.Invoke(item) ?? true) ? "not_shippable"
                : ItemPolicy.RefuseDispose(this.config.EconomyItemPolicy ?? new ItemPolicyConfig(), ItemPolicy.Ship, EconItemOf(item, 1f),
                    target.Amount);
            if (before is null || button is null
                || original.inventory.inventory.Count(b => b.containsPoint(button.bounds.Center.X, button.bounds.Center.Y)) != 1)
                throw new InvalidOperationException("current bin slot unavailable");
        }
        catch
        {
            this.RejectTransferMode(request, pin, "bin_readback_unavailable");
            return;
        }
        var input = new ShippingBinInput();
        var slotValid = this.BindTransferSlot(request);
        var point = button.bounds.Center;
        this.transferTransition = (next, ok) => input.ObserveReceiverTransition(this.ReceiverTokenOf(original), this.ReceiverTokenOf(next));
        ContainerTransferVerdict verdict;
        try
        {
            verdict = input.Execute(target, current, before, this.BinTransferReady, policy,
                () =>
                {
                    if (!slotValid.Valid() || !ReferenceEquals(Game1.activeClickableMenu, original))
                        throw new InvalidOperationException("bin menu lost");
                    // One synchronous block with NO modifier: a stale LeftShift would turn ship_one into half (InventoryMenu.cs:405-408).
                    WithKeyboard(KbModifiers.None, () =>
                    {
                        if (target.Mode == ContainerTransferTarget.ModeShipOne)
                            original.receiveRightClick(point.X, point.Y);
                        else
                        {
                            original.receiveLeftClick(point.X, point.Y);
                            if (slotValid.Valid()) original.releaseLeftClick(point.X, point.Y);
                        }
                    });
                },
                () => IsShippingBinMenu(Game1.activeClickableMenu, out _, out _)
                    ? ShippingBinReadback.Read(this.CaptureShippingBinFacts(), target, this.CurrentCommandIdentity().ContextGeneration, out _)
                    : null);
        }
        finally
        {
            this.transferTransition = null;
        }
        if (verdict.Outcome == "TransferRejected")
        {
            this.RejectTransferMode(request, pin, verdict.Detail ?? "rejected");
            return;
        }
        this.transferDoubt.Note(verdict.Outcome == "TransferUncertain", verdict.CleanupConfirmed);
        this.Monitor.Log($"container_transfer {target.Mode} VERDICT request_id={request.RequestId} outcome={verdict.Outcome} detail={verdict.Detail}",
            verdict.Paired ? LogLevel.Info : LogLevel.Warn);
        if (this.config.Enabled && Context.IsWorldReady)
            this.AdvanceUiContextNative(this.screens.Value);
        this.WriteCommandResult(this.commandInbox.CompleteTransfer(verdict, this.CurrentCommandIdentity(), NowUnixMs()));
    }

    void ExecuteChestMode(MenuStepCommandRequest request, CommandInboxIdentity pin, ContainerTransferTarget target)
    {
        var game = Game1.game1;
        if (!this.ContainerTransferReady || Game1.activeClickableMenu is not ItemGrabMenu original
            || original.context is not Chest chest
            || chest.GetType() != typeof(Chest) || chest.QualifiedItemId is not ("(BC)130" or "(BC)232")
            || original.source != ItemGrabMenu.source_chest || !ReferenceEquals(original.sourceItem, chest)
            || original.currentlySnappedComponent is not null || original.heldItem is not null)
        {
            this.RejectTransferMode(request, pin, "chest_not_ready");
            return;
        }
        ContainerTransferReadback? before;
        ContainerTransferTarget? current;
        int capacity;
        ClickableComponent? button;
        try
        {
            string? refused = ContainerTransferAdmission.Refusal(this.CaptureTransferFacts(original), target, pin.ContextGeneration,
                this.ContainerTransferReady, this.SingleItemTransferReady(original), out before, out current, out capacity); // PM-1 fix8: = published
            InventoryMenu side = target.IsTakeOut ? original.ItemsToGrabMenu : original.inventory;
            button = side.inventory.SingleOrDefault(b => b.name == target.Slot.ToString(CultureInfo.InvariantCulture));
            Item? item = target.IsTakeOut
                ? target.Slot < original.ItemsToGrabMenu.actualInventory.Count ? original.ItemsToGrabMenu.actualInventory[target.Slot] : null
                : target.Slot < Game1.player.Items.Count ? Game1.player.Items[target.Slot] : null;
            string? policy = item is null ? "empty_slot" : target.IsTakeOut ? null : ItemPolicy.RefuseStore(EconItemOf(item, 1f));
            if (policy is not null || refused is not null || button is null
                || side.inventory.Count(b => b.containsPoint(button.bounds.Center.X, button.bounds.Center.Y)) != 1)
                throw new InvalidOperationException("current chest slot unavailable: " + (policy ?? "binding"));
        }
        catch (Exception ex)
        {
            this.RejectTransferMode(request, pin, ex.Message.Length <= 80 ? ex.Message : "chest_readback_unavailable");
            return;
        }
        var input = new ContainerTransferInput();
        var slotValid = this.BindTransferSlot(request);
        var point = button.bounds.Center;
        bool SameActor()
        {
            var now = this.CurrentCommandIdentity();
            return slotValid.Valid() && ReferenceEquals(game, Game1.game1) && this.ContainerActorEligible && now == pin with { ContextGeneration = now.ContextGeneration };
        }
        bool SameChest(IClickableMenu? receiver) => receiver is ItemGrabMenu m && m.source == ItemGrabMenu.source_chest
            && ReferenceEquals(m.context, chest) && ReferenceEquals(m.sourceItem, chest)
            && PlacedTileOf(Game1.player.currentLocation, chest) is not null;
        IClickableMenu? previous = original;
        this.transferTransition = (next, ok) =>
        {
            string? oldToken = this.ReceiverTokenOf(previous);
            string? nextToken = this.ReceiverTokenOf(next);
            if (SameChest(next)) slotValid.OwnTransition(); // PM-1 fix6 #2: the chest's own menu replacement
            input.ObserveReceiverTransition(oldToken, nextToken, SameChest(next), SameActor(), ok);
            previous = next;
        };
        ContainerTransferReadback? Read()
        {
            if (!SameActor() || !SameChest(Game1.activeClickableMenu))
                return null;
            this.AdvanceUiContextNative(this.screens.Value);
            return ContainerTransferReadback.Read(this.CaptureTransferFacts((ItemGrabMenu)Game1.activeClickableMenu), target,
                this.CurrentCommandIdentity().ContextGeneration, out _, out _);
        }
        ContainerTransferVerdict verdict;
        try
        {
            // quick = the vanilla shift-click (LeftShift in oldKBState for this one call); take_out clears every modifier.
            KbModifiers mods = target.Mode == ContainerTransferTarget.ModeQuick ? KbModifiers.Shift : KbModifiers.None;
            verdict = input.Execute(target, current, before, true, Game1.options.SnappyMenus, original.currentlySnappedComponent is null,
                capacity,
                () => WithKeyboard(mods, () => {
                    if (!SameActor()) throw new InvalidOperationException("original actor lost");
                    original.receiveLeftClick(point.X, point.Y);
                }),
                () =>
                {
                    if (!SameActor())
                        throw new InvalidOperationException("original actor lost");
                    original.releaseLeftClick(point.X, point.Y);
                },
                Read, returnCursor: () => WithKeyboard(KbModifiers.None, () => TransferCursorReturn.Click(SameActor, SameChest, point)));
        }
        finally
        {
            this.transferTransition = null;
        }
        this.transferDoubt.Note(verdict.Outcome == "TransferUncertain", verdict.CleanupConfirmed);
        this.Monitor.Log($"container_transfer {target.Mode} VERDICT request_id={request.RequestId} outcome={verdict.Outcome} detail={verdict.Detail}",
            verdict.Paired ? LogLevel.Info : LogLevel.Warn);
        this.WriteCommandResult(this.commandInbox.CompleteTransfer(verdict, this.CurrentCommandIdentity(), NowUnixMs()));
    }
}
