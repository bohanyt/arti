using System.Globalization;
using System.Security.Cryptography;
using System.Text;

namespace ArtiStardewBridge.Protocol;

// Primitives v2 WS-G2 (PLAN.md 1.2 ShopMenu / CraftingPage / ItemGrabMenu rows, 5.3, 6.1 D10; WS-G2 brief 3): the
// economy layer on top of the G1 generic menu layer. SMAPI-free: ModEntry.Economy.cs reads the game into these facts.
//   shop   (ShopMenu):    A on a stock row = buy {qty} (1 / 5 = LeftShift / 25 = LeftShift+LeftControl through a
//                         Game1.oldKBState override held across ONE synchronous call, ShopMenu.cs:1105; any other qty =
//                         the private tryToPurchaseItem by reflection, :1319); X on a stock row = buy one (:1640-1670);
//                         X on a player slot = sell ONE (InventoryMenu.rightClick, InventoryMenu.cs:387-417; the override
//                         CLEARS LeftShift so a stale modifier never turns it into half); X{half} = LeftShift -> ceil(n/2)
//                         (:405-408); A{stack} on a player slot = sell the WHOLE stack (ShopMenu.cs:1022-1031); A without
//                         {stack} on a filled slot is refused; LT / RT = the stock <-> inventory toggle (:1769-1797).
//   crafting (CraftingPage, GameMenu tab 4 or standalone cooking / workbench): A on a recipe = craft {qty 1/5/25}
//                         (CraftingPage.cs:396), X on a recipe = craft one (:507); A on a slot only places a held item.
//   Every sell / ship / store / buy / craft is gated by the owner item policy (PLAN 6.1 D10, config table, conservative
//   default since 2026-10-23: a denylist, ItemPolicyConfig.FreeDispose; legacy: an explicit allowlist, optional price cap;
//   tools / weapons / quest / special / equipment / recipes never). Every effect is
//   verified from a before / after ledger (money, item counts incl. the held item, row stock, toolBeingUpgraded).
// Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G2 live witnesses (PRB-5 modifier override, PRB-6 reflection).

/// <summary>Read-only facts of one item (field reads / pure getters only).</summary>
public sealed record EconItem(string QualifiedId, string Name, int Stack, int Quality = 0, int Category = 0,
    bool IsTool = false, bool QuestItem = false, bool SpecialItem = false, bool Equipment = false, bool IsRecipe = false,
    int UnitSellPrice = 0, bool CanBeTrashed = true, bool Shippable = true, int MaxStack = 999);

/// <summary>Item policy decisions. Null = allowed; else the refusal reason (constant vocabulary).</summary>
public static class ItemPolicy
{
    public const string Sell = "sell", Ship = "ship", Store = "store", Take = "take", Trash = "trash";

    /// <summary>Sell / ship / trash of <paramref name="quantity"/> units of <paramref name="item"/>.</summary>
    public static string? RefuseDispose(ItemPolicyConfig c, string action, EconItem item, int quantity)
    {
        if (action == Trash)
            return "trash_never";
        if (action is not (Sell or Ship))
            return "unknown_action";
        if (quantity < 1 || quantity > item.Stack)
            return "quantity_out_of_range";
        if (item.IsTool) return "tool";
        if (item.QuestItem) return "quest_item";
        if (action == Ship && !item.Shippable)
            return "not_shippable";
        return null; // Owner: all other items may be sold; native handlers retain their factual limits.
    }

    /// <summary>Chest put (quick move): any non-tool, non-quest item (non-destructive).</summary>
    public static string? RefuseStore(EconItem item) =>
        item.IsTool ? "tool" : item.QuestItem ? "quest_item" : null;

    /// <summary>One purchase of <paramref name="qty"/> from <paramref name="row"/>.</summary>
    public static string? RefuseBuy(ItemPolicyConfig c, ShopRow row, int qty, string? shopId, int money, string? toolUpgrading = null)
    {
        if (qty < 1 || qty > 999)
            return "qty_out_of_range";
        bool upgrade = shopId == ShopFactsTable.ClintUpgrade;
        // Barter rows are P2 (MS27); Clint's upgrade rows trade bars (Data/Shops ClintUpgrade TradeItemId) and are the
        // one admitted exception, checked against the bars in the inventory.
        if (row.TradeItem is not null && !upgrade)
            return "trade_item_row_p2";
        if (row.TradeItem is not null && row.TradeHave < row.TradeCount * qty)
            return "not_enough_trade_item";
        if (row.IsRecipe)
            return "recipe_row_p2";
        if (row.Price <= 0)
            return "free_row_unverifiable";
        long total = (long)row.Price * qty;
        if (upgrade)
        {
            if (!c.AllowToolUpgrade)
                return "tool_upgrade_not_allowed";
            if (qty != 1)
                return "tool_upgrade_qty_one";
            if (row.RequiredToolId is null || row.RequiredToolCount != 1)
                return "upgrade_required_tool_unavailable";
            if (toolUpgrading is not null)
                return "tool_already_upgrading";
        }
        else if (total > c.BuyMaxTotal)
            return "over_buy_max_total";
        if (total > money)
            return "not_enough_money";
        if (row.Stock >= 0 && row.Stock < qty)
            return "not_enough_stock";
        if (qty > Math.Max(1, row.MaxStack))
            return "over_max_stack";
        return null;
    }

    public static string? RefuseCraft(ItemPolicyConfig c, RecipeRow r) =>
        c.CraftDeny.Contains(r.Name, StringComparer.OrdinalIgnoreCase) ? "craft_denied" : null;
}

/// <summary>Modifier state written into Game1.oldKBState for exactly one synchronous call (PRB-5).</summary>
[Flags]
public enum KbModifiers
{
    /// <summary>No modifier: the override CLEARS LeftShift / LeftControl (a stale modifier never changes the quantity).</summary>
    None = 0,
    Shift = 1,
    Control = 2,
}

/// <summary>
/// The oldKBState override block (taxonomy B 3a): save, write, call, restore in finally (exception included).
/// Generic so the restore order is unit-tested without XNA; ModEntry passes Game1.oldKBState accessors.
/// </summary>
public static class KbOverride
{
    /// <summary>
    /// The production modifier block (ModEntry.WithKeyboard is a one-line call of this with Game1.oldKBState and the XNA
    /// KeyboardState): build the state with exactly the <paramref name="mods"/> keys, run the call, restore in finally.
    /// </summary>
    public static void RunModifiers<TState, TKey>(Func<TState> get, Action<TState> set, Func<TKey[], TState> make, TKey shift,
        TKey control, KbModifiers mods, Action call)
    {
        var keys = new List<TKey>();
        if (mods.HasFlag(KbModifiers.Shift))
            keys.Add(shift);
        if (mods.HasFlag(KbModifiers.Control))
            keys.Add(control);
        Run(get, set, make(keys.ToArray()), call);
    }

    /// <summary>The vanilla quantity a modifier set means on a shop stock row / crafting recipe (ShopMenu.cs:1105, CraftingPage.cs:396).</summary>
    public static KbModifiers? ForQuantity(int qty) => qty switch
    {
        1 => KbModifiers.None,
        5 => KbModifiers.Shift,
        25 => KbModifiers.Shift | KbModifiers.Control,
        _ => null,
    };

    public static string Describe(KbModifiers m) => m switch
    {
        KbModifiers.None => "none",
        KbModifiers.Shift => "LeftShift",
        KbModifiers.Shift | KbModifiers.Control => "LeftShift+LeftControl",
        _ => "LeftControl",
    };

    public static void Run<TState>(Func<TState> get, Action<TState> set, TState temporary, Action call)
    {
        TState saved = get();
        set(temporary);
        try
        {
            call();
        }
        finally
        {
            set(saved);
        }
    }
}

public static class ShopFactsTable
{
    public const string ClintUpgrade = "ClintUpgrade";
    /// <summary>ShopMenu.region_shopButtonModifier (forSaleButtons[k].myID = 3546 + k, ShopMenu.cs:160, 402).</summary>
    public const int StockRowBase = 3546, StockRows = 4;
    public const int UpArrow = 97865, DownArrow = 106;
    public const int MaxRows = 60, MaxRecipes = 60, MaxIngredients = 8;

    public static bool IsStockRowId(int id) => id >= StockRowBase && id < StockRowBase + StockRows;

    /// <summary>CraftingPage recipe components: myID = 200 + n (CraftingPage.cs:229); page up 88 / down 89.</summary>
    public const int RecipeIdBase = 200, RecipeIdEnd = 1000, PageUp = 88, PageDown = 89;

    public static bool IsRecipeId(int id) => id >= RecipeIdBase && id < RecipeIdEnd;
}

/// <summary>One for-sale entry (index into forSale; ComponentId when it is one of the 4 visible rows).</summary>
public sealed record ShopRow(int Index, int? ComponentId, string ItemId, string Name, int Price, int Stock, int ItemStack = 1,
    int MaxStack = 999, bool IsTool = false, bool IsRecipe = false, string? TradeItem = null, int TradeCount = 0, int Capacity = 999,
    int TradeHave = 0, string? RequiredToolId = null, int RequiredToolCount = 0)
{
    /// <summary>Review fix: the inventory takes <paramref name="qty"/> purchases of this row (qty x ItemStack units).</summary>
    public bool FitsQty(int qty) => (long)qty * Math.Max(1, this.ItemStack) <= this.Capacity;

    public bool Fits => this.FitsQty(1);
}

/// <summary>One player inventory slot as the shop sees it (Sellable = the shop's highlightItemToSell).</summary>
public sealed record SellSlot(int ComponentId, int Slot, EconItem? Item, bool Sellable);

public sealed record RecipeIngredient(string Key, int Need, int Have);

public sealed record RecipeRow(int ComponentId, string Name, string DisplayName, string ProducesId, int Produces, bool Craftable,
    bool Ghosted, IReadOnlyList<RecipeIngredient> Ingredients)
{
    /// <summary>How many crafts the inventory covers (exact-key ingredients; the vanilla loop re-checks each).</summary>
    public int MaxCrafts => this.Ingredients.Count == 0 ? 0
        : this.Ingredients.Min(i => i.Need <= 0 ? int.MaxValue : i.Have / i.Need);
}

/// <summary>Economy facts of the open shop / crafting page (one tick, read-only).</summary>
public sealed record EconomyFacts(
    string Kind,
    int Money,
    ItemPolicyConfig Policy,
    string? ShopId = null,
    int Currency = 0,
    int SafetyTimerMs = 0,
    bool ReadOnly = false,
    bool CustomSell = false,
    bool HasPurchaseCheck = false,
    bool ReflectionBound = false,
    IReadOnlyList<ShopRow>? Rows = null,
    IReadOnlyList<SellSlot>? Slots = null,
    bool Cooking = false,
    IReadOnlyList<RecipeRow>? Recipes = null,
    string? ToolUpgrading = null,
    int HeldQuality = 0)
{
    public const string KindShop = "shop", KindCrafting = "crafting";

    public IReadOnlyList<ShopRow> RowList => this.Rows ?? Array.Empty<ShopRow>();
    public IReadOnlyList<SellSlot> SlotList => this.Slots ?? Array.Empty<SellSlot>();
    public IReadOnlyList<RecipeRow> RecipeList => this.Recipes ?? Array.Empty<RecipeRow>();

    public ShopRow? RowAt(int componentId) => this.RowList.FirstOrDefault(r => r.ComponentId == componentId);
    public SellSlot? SlotAt(int componentId) => this.SlotList.FirstOrDefault(s => s.ComponentId == componentId);
    public RecipeRow? RecipeAt(int componentId) => this.RecipeList.FirstOrDefault(r => r.ComponentId == componentId);

    /// <summary>State digest (part of the ui_pad target token): money, stock, sellable slots, craftable flags.</summary>
    public string Digest()
    {
        StringBuilder text = new();
        text.Append(this.Kind).Append('|').Append(this.Money).Append('|').Append(this.ShopId).Append('|').Append(this.ToolUpgrading)
            .Append('|').Append(this.SafetyTimerMs > 0 ? 1 : 0);
        foreach (ShopRow r in this.RowList)
            text.Append('\u001f').Append(r.Index).Append(',').Append(r.ComponentId).Append(',').Append(r.ItemId).Append(',')
                .Append(r.Price).Append(',').Append(r.Stock).Append(',').Append(r.Capacity);
        if (this.ShopId == ShopFactsTable.ClintUpgrade)
        {
            text.Append("|upgrade_allowed=").Append(this.Policy.AllowToolUpgrade);
            foreach (ShopRow r in this.RowList)
                text.Append('|').Append(r.TradeItem).Append(',').Append(r.TradeCount).Append(',').Append(r.TradeHave)
                    .Append(',').Append(r.RequiredToolId).Append(',').Append(r.RequiredToolCount);
        }
        foreach (SellSlot s in this.SlotList)
            text.Append('\u001e').Append(s.ComponentId).Append(',').Append(s.Item?.QualifiedId).Append(',').Append(s.Item?.Stack)
                .Append(',').Append(s.Sellable ? 1 : 0).Append(',').Append(s.Item?.UnitSellPrice);
        foreach (RecipeRow r in this.RecipeList)
            text.Append('\u001d').Append(r.ComponentId).Append(',').Append(r.Name).Append(',').Append(r.Craftable ? 1 : 0)
                .Append(',').Append(string.Join(";", r.Ingredients.Select(i => i.Key + ":" + i.Have)));
        return UiKeyTargetToken.Hash(text.ToString())[..32];
    }
}

/// <summary>Typed G2 ui_press payload (qty / half / stack). Invalid = the first malformed key (refused).</summary>
public sealed record UiPressExtras(int? Qty = null, bool Half = false, bool Stack = false, string? Invalid = null, string? Reserved = null,
    string FirstKey = "")
{
    public static readonly UiPressExtras None = new();

    public bool Any => this.Qty is not null || this.Half || this.Stack || this.Invalid is not null || this.Reserved is not null;

    /// <summary>From the parsed typed reserved args: qty int 1..999, half / stack JSON true; quick / text / value = Reserved.</summary>
    public static UiPressExtras From(IReadOnlyDictionary<string, object>? typed)
    {
        if (typed is not { Count: > 0 })
            return None;
        int? qty = null;
        bool half = false, stack = false;
        string? invalid = null, reserved = null;
        foreach (var kv in typed.OrderBy(k => k.Key, StringComparer.Ordinal))
        {
            switch (kv.Key)
            {
                case "qty":
                    if (kv.Value is int n && n is >= 1 and <= 999) qty = n;
                    else invalid ??= "qty";
                    break;
                case "half":
                    if (kv.Value is true) half = true;
                    else invalid ??= "half";
                    break;
                case "stack":
                    if (kv.Value is true) stack = true;
                    else invalid ??= "stack";
                    break;
                default:
                    reserved ??= kv.Key;
                    break;
            }
        }
        if (invalid is null && (qty is not null ? 1 : 0) + (half ? 1 : 0) + (stack ? 1 : 0) > 1)
            invalid = "combination";
        return new UiPressExtras(qty, half, stack, invalid, reserved, typed.Keys.OrderBy(k => k, StringComparer.Ordinal).First());
    }
}

/// <summary>What the press is expected to do to the ledger (judged after the call).</summary>
public sealed record EconomyExpect(string Kind, string ItemId, int Quantity, int UnitPrice = 0, int ItemStack = 1,
    bool FiniteStock = false, int StockBefore = -1, int Produces = 1, IReadOnlyList<RecipeIngredient>? Ingredients = null,
    string? ShopId = null)
{
    public const string Sell = "sell", Buy = "buy", Craft = "craft", Place = "place", Upgrade = "upgrade";
}

/// <summary>Before / after facts the judge compares (read by the port; counts include the menu's held item).</summary>
public sealed record EconomyLedger(int Money, int ItemCount, string? HeldId = null, int HeldStack = 0, int? Stock = null,
    bool RowPresent = true, string? ToolUpgrading = null, IReadOnlyDictionary<string, int>? IngredientCounts = null,
    string? MenuType = null);

public sealed record EconomyReport(string Kind, string ItemId, int Quantity, bool Observed, string Effect, string Reason,
    EconomyLedger Before, EconomyLedger After, string Modifiers = "none", string Path = "click")
{
    public int MoneyDelta => this.After.Money - this.Before.Money;
    public int ItemDelta => this.After.ItemCount - this.Before.ItemCount;

    public Dictionary<string, object> Wire()
    {
        var d = new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["kind"] = this.Kind, ["item"] = this.ItemId, ["quantity"] = this.Quantity, ["observed"] = this.Observed,
            ["effect"] = this.Effect, ["reason"] = this.Reason, ["money_before"] = this.Before.Money,
            ["money_after"] = this.After.Money, ["item_before"] = this.Before.ItemCount, ["item_after"] = this.After.ItemCount,
            ["modifiers"] = this.Modifiers, ["path"] = this.Path,
        };
        if (this.Before.Stock is int sb) d["stock_before"] = sb;
        if (this.After.Stock is int sa) d["stock_after"] = sa;
        if (this.After.HeldId is { } held) d["held_after"] = new Dictionary<string, object> { ["item"] = held, ["stack"] = this.After.HeldStack };
        if (this.After.ToolUpgrading is { } tool) d["tool_upgrading"] = tool;
        if (this.Kind == EconomyExpect.Upgrade)
        {
            d["ingredients_before"] = this.Before.IngredientCounts ?? new Dictionary<string, int>();
            d["ingredients_after"] = this.After.IngredientCounts ?? new Dictionary<string, int>();
        }
        return d;
    }
}

/// <summary>Deterministic ledger judge: observed = exactly the expected deltas; none = no change; else mismatch.</summary>
public static class EconomyJudge
{
    public const string EffectSold = "sold", EffectBought = "bought", EffectCrafted = "crafted", EffectPlaced = "placed",
        EffectUpgrade = "upgrade_started";

    public static (bool Observed, bool None, string Effect, string Reason) Judge(EconomyExpect x, EconomyLedger b, EconomyLedger a)
    {
        int dMoney = a.Money - b.Money, dItem = a.ItemCount - b.ItemCount;
        bool noChange = dMoney == 0 && dItem == 0 && a.HeldId == b.HeldId && a.HeldStack == b.HeldStack
            && a.Stock == b.Stock && a.ToolUpgrading == b.ToolUpgrading;
        switch (x.Kind)
        {
            case EconomyExpect.Sell:
            {
                long wantMoney = (long)x.UnitPrice * x.Quantity;
                if (noChange) return (false, true, "", "no_change");
                if (dItem != -x.Quantity) return (false, false, "", "item_delta:" + dItem);
                if (dMoney != wantMoney) return (false, false, "", "money_delta:" + dMoney);
                return (true, false, EffectSold, "sold:" + x.Quantity);
            }
            case EconomyExpect.Buy:
            {
                if (noChange) return (false, true, "", "no_change");
                long wantMoney = -(long)x.UnitPrice * x.Quantity;
                if (dMoney != wantMoney) return (false, false, "", "money_delta:" + dMoney);
                if (dItem != x.Quantity * Math.Max(1, x.ItemStack)) return (false, false, "", "item_delta:" + dItem);
                if (x.FiniteStock)
                {
                    int wantStock = x.StockBefore - x.Quantity;
                    bool stockOk = wantStock <= 0 ? !a.RowPresent || a.Stock is null or 0 : a.Stock == wantStock;
                    if (!stockOk) return (false, false, "", "stock:" + (a.Stock?.ToString(CultureInfo.InvariantCulture) ?? "gone"));
                }
                return (true, false, EffectBought, "bought:" + x.Quantity);
            }
            case EconomyExpect.Upgrade:
            {
                if (noChange) return (false, true, "", "no_change");
                if (b.ToolUpgrading is not null || a.ToolUpgrading != x.ItemId) return (false, false, "", "tool_upgrading_unchanged");
                if (dMoney != -x.UnitPrice) return (false, false, "", "money_delta:" + dMoney);
                if (x.Quantity != 1 || dItem != 0 || b.HeldId is not null || a.HeldId is not null
                    || x.Ingredients is not { Count: 2 } required)
                    return (false, false, "", "upgrade_ledger_incomplete");
                foreach (var ing in required)
                    if (ing.Need <= 0 || b.IngredientCounts is null || a.IngredientCounts is null
                        || !b.IngredientCounts.TryGetValue(ing.Key, out int before)
                        || !a.IngredientCounts.TryGetValue(ing.Key, out int after) || before - after != ing.Need)
                        return (false, false, "", "upgrade_ingredient_delta:" + ing.Key);
                return (true, false, EffectUpgrade, "upgrade:" + a.ToolUpgrading);
            }
            case EconomyExpect.Craft:
            {
                if (noChange) return (false, true, "", "no_change");
                if (dMoney != 0) return (false, false, "", "money_delta:" + dMoney);
                int want = x.Produces * x.Quantity;
                if (dItem != want) return (false, false, "", "item_delta:" + dItem);
                foreach (RecipeIngredient ing in x.Ingredients ?? Array.Empty<RecipeIngredient>())
                {
                    if (ing.Key == x.ItemId || b.IngredientCounts is null || a.IngredientCounts is null
                        || !b.IngredientCounts.TryGetValue(ing.Key, out int before) || !a.IngredientCounts.TryGetValue(ing.Key, out int after))
                        continue;
                    int used = before - after;
                    // Material containers (a workbench's chests) may supply part: never MORE than the recipe, never a gain.
                    if (used < 0 || used > ing.Need * x.Quantity)
                        return (false, false, "", "ingredient_delta:" + ing.Key + ":" + (after - before));
                }
                return (true, false, EffectCrafted, "crafted:" + want);
            }
            case EconomyExpect.Place:
            {
                if (noChange) return (false, true, "", "no_change");
                if (dMoney != 0) return (false, false, "", "money_delta:" + dMoney);
                if (a.HeldId is not null) return (false, false, "", "still_held");
                if (dItem != 0) return (false, false, "", "item_delta:" + dItem);
                return (true, false, EffectPlaced, "placed");
            }
            default:
                return (false, false, "", "unknown_kind");
        }
    }
}

/// <summary>The shop / crafting rows of the per-context table (PLAN 1.2). Refusals carry zero steps.</summary>
public static class EconomyPlans
{
    static string Deny(string why) => UiPadTable.Denied + ":" + why;
    static string None(string why) => UiPadTable.NoMeaning + ":" + why;

    static UiPadPlan Click(string button, string does, KbModifiers mods, EconomyExpect? expect, bool storeHeldAfter = false)
    {
        var steps = new List<UiPadStep>
        {
            new(UiPadStepKind.GamePadButton, button),
            new(button == UiPadTable.BtnX ? UiPadStepKind.RightClick : UiPadStepKind.LeftClick, button),
            new(UiPadStepKind.KeyPress, button),
        };
        if (storeHeldAfter)
            steps.Add(new UiPadStep(UiPadStepKind.ShopStoreHeld, button));
        return new UiPadPlan(button, null, steps, does, mods, expect);
    }

    static UiPadPlan Close(string button, UiPadFacts f, string does) =>
        f.ForcePreventClose || f.ReadyToClose != true || f.HeldItemId is not null
            ? UiPadPlan.No(button, Deny(f.HeldItemId is not null ? "held_item_place_first" : "not_ready_to_close"))
            : UiPadPlan.Ok(button, does, new UiPadStep(UiPadStepKind.GamePadButton, button), new UiPadStep(UiPadStepKind.KeyPress, button));

    /// <summary>
    /// Review fix: vanilla InventoryMenu.leftClick SWAPS the held item with a different one in the slot (InventoryMenu.cs:
    /// 309-345), which leaves an item on the cursor (B refused) — so a place is admitted only into an empty slot, or onto
    /// the same item / quality whose stack still fits.
    /// </summary>
    public static string? PlaceRefusal(UiPadFacts f, EconomyFacts e, SellSlot? slot)
    {
        if (slot is null)
            return "slot_unknown";
        if (slot.Item is not { } there)
            return null;
        if (there.QualifiedId != f.HeldItemId || there.Quality != e.HeldQuality)
            return "held_item_slot_occupied";
        if ((long)there.Stack + f.HeldStack > there.MaxStack)
            return "held_item_stack_overflow";
        return null;
    }

    // ------------------------------------------------------------------ shop

    public static UiPadPlan Shop(string button, UiPadFacts f, EconomyFacts e, UiPressExtras x)
    {
        if (x.Invalid is { } invalid)
            return UiPadPlan.No(button, Deny("invalid_arg:" + invalid));
        if (x.Reserved is { } reserved)
            return UiPadPlan.No(button, Deny(reserved == "quick" ? "quick_use_container_transfer" : "reserved_arg_g3:" + reserved));
        switch (button)
        {
            case UiPadTable.BtnB:
            case UiPadTable.BtnStart:
                return x.Any ? UiPadPlan.No(button, Deny("arg_not_for_button")) : Close(button, f, "leave the shop");
            case UiPadTable.BtnLT:
            case UiPadTable.BtnRT:
                // Either trigger toggles stock <-> inventory (ShopMenu.cs:1769-1797): a toggle, not a direction.
                return x.Any ? UiPadPlan.No(button, Deny("arg_not_for_button"))
                    : UiPadPlan.Ok(button, "toggle stock / inventory", new UiPadStep(UiPadStepKind.GamePadButton, button));
            case UiPadTable.BtnA:
            case UiPadTable.BtnX:
                break;
            default:
                return UiPadPlan.No(button, None("shop"));
        }
        if (f.Snapped is not { } c)
            return UiPadPlan.No(button, Deny("nothing_snapped"));
        if (c.Id == UiPadTable.DropButton)
            return UiPadPlan.No(button, Deny("drop_button"));
        if (c.Id is ShopFactsTable.UpArrow or ShopFactsTable.DownArrow)
            return button == UiPadTable.BtnA && !x.Any
                ? Click(button, c.Id == ShopFactsTable.UpArrow ? "scroll the stock up" : "scroll the stock down", KbModifiers.None, null)
                : UiPadPlan.No(button, Deny("component_not_allowlisted"));
        if (e.ReadOnly)
            return UiPadPlan.No(button, Deny("shop_read_only"));
        if (e.SafetyTimerMs > 0)
            return UiPadPlan.No(button, Deny("not_ready:safety_timer"));
        if (e.Currency != 0)
            return UiPadPlan.No(button, Deny("non_gold_currency_p2"));
        if (ShopFactsTable.IsStockRowId(c.Id))
            return ShopBuy(button, f, e, x, c.Id);
        if (e.SlotAt(c.Id) is { } slot)
            return ShopSlot(button, f, e, x, slot);
        return UiPadPlan.No(button, Deny("component_not_allowlisted"));
    }

    static UiPadPlan ShopBuy(string button, UiPadFacts f, EconomyFacts e, UiPressExtras x, int componentId)
    {
        if (x.Half || x.Stack)
            return UiPadPlan.No(button, Deny("arg_not_for_stock_row"));
        if (e.RowAt(componentId) is not { } row)
            return UiPadPlan.No(button, Deny("stock_row_empty"));
        if (f.HeldItemId is not null)
            return UiPadPlan.No(button, Deny("held_item_place_first"));
        int qty = x.Qty ?? 1;
        if (button == UiPadTable.BtnX && qty != 1)
            return UiPadPlan.No(button, Deny("x_buys_one"));
        if (ItemPolicy.RefuseBuy(e.Policy, row, qty, e.ShopId, e.Money, e.ToolUpgrading) is { } refused)
            return UiPadPlan.No(button, Deny("item_policy:" + refused));
        bool upgradeRow = e.ShopId == ShopFactsTable.ClintUpgrade;
        if (!row.FitsQty(qty) && !upgradeRow)
            return UiPadPlan.No(button, Deny("inventory_full"));
        if (e.HasPurchaseCheck)
            // canPurchaseCheck (ShopMenu.cs:1097-1100) is an arbitrary delegate: never evaluated from ui_inspect.
            return UiPadPlan.No(button, Deny("purchase_check_shop_p2"));
        bool upgrade = upgradeRow;
        var expect = new EconomyExpect(upgrade ? EconomyExpect.Upgrade : EconomyExpect.Buy, row.ItemId, qty, row.Price,
            row.ItemStack, row.Stock >= 0, row.Stock,
            Ingredients: upgrade && row.TradeItem is not null && row.RequiredToolId is not null
                ? new[] { new RecipeIngredient(row.TradeItem, row.TradeCount, row.TradeHave),
                    new RecipeIngredient(row.RequiredToolId, 1, row.RequiredToolCount) } : null, ShopId: e.ShopId);
        string does = (upgrade ? "upgrade " : "buy " + qty + " ") + row.Name + " (" + (long)row.Price * qty + "g)";
        if (button == UiPadTable.BtnX)
            return Click(button, does, KbModifiers.None, expect, storeHeldAfter: !upgrade);
        if (KbOverride.ForQuantity(qty) is { } mods)
            return Click(button, does, mods, expect, storeHeldAfter: !upgrade);
        // Any other qty: the private tryToPurchaseItem with the exact stockToBuy (PRB-6, pinned assembly only).
        if (!e.ReflectionBound)
            return UiPadPlan.No(button, Deny("reflection_unbound"));
        return new UiPadPlan(button, null, new[]
        {
            new UiPadStep(UiPadStepKind.ShopBuyDirect, button, Qty: qty, RowIndex: row.Index),
            new UiPadStep(UiPadStepKind.ShopStoreHeld, button),
        }, does, KbModifiers.None, expect);
    }

    static UiPadPlan ShopSlot(string button, UiPadFacts f, EconomyFacts e, UiPressExtras x, SellSlot slot)
    {
        if (x.Qty is not null)
            return UiPadPlan.No(button, Deny("qty_not_for_slot"));
        if (f.HeldItemId is not null)
        {
            // Place the held (bought) item on the slot: A only, empty slot or same stackable item (vanilla leftClick).
            if (button != UiPadTable.BtnA || x.Any)
                return UiPadPlan.No(button, Deny("held_item_place_with_a"));
            if (PlaceRefusal(f, e, slot) is { } occupied)
                return UiPadPlan.No(button, Deny(occupied));
            return Click(button, "place the held item", KbModifiers.None,
                new EconomyExpect(EconomyExpect.Place, f.HeldItemId, f.HeldStack));
        }
        if (slot.Item is not { } item)
            return UiPadPlan.No(button, Deny("empty_slot"));
        if (e.CustomSell)
            return UiPadPlan.No(button, Deny("custom_sell_shop_p2"));
        if (!slot.Sellable)
            return UiPadPlan.No(button, Deny("shop_does_not_buy_this"));
        int n;
        KbModifiers mods;
        if (button == UiPadTable.BtnX)
        {
            if (x.Stack)
                return UiPadPlan.No(button, Deny("stack_with_a"));
            n = x.Half ? (item.Stack <= 1 ? 1 : (item.Stack + 1) / 2) : 1;
            mods = x.Half ? KbModifiers.Shift : KbModifiers.None;
        }
        else
        {
            if (!x.Stack)
                // A on a filled slot sells the WHOLE stack (ShopMenu.cs:1022-1031): only when the goal named the stack.
                return UiPadPlan.No(button, Deny("sell_stack_needs_stack_arg"));
            n = item.Stack;
            mods = KbModifiers.None;
        }
        if (ItemPolicy.RefuseDispose(e.Policy, ItemPolicy.Sell, item, n) is { } refused)
            return UiPadPlan.No(button, Deny("item_policy:" + refused));
        if (item.UnitSellPrice <= 0)
            return UiPadPlan.No(button, Deny("no_sell_price"));
        string does = "sell " + (n == item.Stack && n > 1 ? "all " + n : n.ToString(CultureInfo.InvariantCulture)) + " " + item.Name
            + " (" + (long)item.UnitSellPrice * n + "g)";
        return Click(button, does, mods, new EconomyExpect(EconomyExpect.Sell, item.QualifiedId, n, item.UnitSellPrice));
    }

    // ------------------------------------------------------------------ crafting

    /// <summary>A / X on a CraftingPage (GameMenu tab 4 or standalone). B / triggers are the caller's (GameMenu) or here (standalone).</summary>
    public static UiPadPlan Crafting(string button, UiPadFacts f, EconomyFacts e, UiPressExtras x, bool standalone)
    {
        if (x.Invalid is { } invalid)
            return UiPadPlan.No(button, Deny("invalid_arg:" + invalid));
        if (x.Reserved is { } reserved)
            return UiPadPlan.No(button, Deny(reserved == "quick" ? "quick_not_in_crafting" : "reserved_arg_g3:" + reserved));
        if (standalone && button is UiPadTable.BtnB or UiPadTable.BtnStart)
            return x.Any ? UiPadPlan.No(button, Deny("arg_not_for_button")) : Close(button, f, e.Cooking ? "close the cooking menu" : "close the crafting menu");
        if (button is not (UiPadTable.BtnA or UiPadTable.BtnX))
            return UiPadPlan.No(button, None("crafting"));
        if (f.Snapped is not { } c)
            return UiPadPlan.No(button, Deny("nothing_snapped"));
        if (c.Id == UiPadTable.DropButton)
            return UiPadPlan.No(button, Deny("drop_button"));
        if (c.Id == UiPadTable.ItemGrabTrash)
            return UiPadPlan.No(button, Deny("trash"));
        if (c.Id is ShopFactsTable.PageUp or ShopFactsTable.PageDown)
            return c.Id == ShopFactsTable.PageUp && f.PageIndex == 0
                ? UiPadPlan.No(button, Deny("first_recipe_page"))
                : button == UiPadTable.BtnA && !x.Any
                ? Click(button, c.Id == ShopFactsTable.PageUp ? "previous recipe page" : "next recipe page", KbModifiers.None, null)
                : UiPadPlan.No(button, Deny("component_not_allowlisted"));
        if (e.RecipeAt(c.Id) is { } recipe)
        {
            if (x.Half || x.Stack)
                return UiPadPlan.No(button, Deny("arg_not_for_recipe"));
            int qty = x.Qty ?? 1;
            if (button == UiPadTable.BtnX && qty != 1)
                return UiPadPlan.No(button, Deny("x_crafts_one"));
            if (KbOverride.ForQuantity(qty) is not { } mods)
                return UiPadPlan.No(button, Deny("qty_not_vanilla_craft"));
            if (f.HeldItemId is not null)
                return UiPadPlan.No(button, Deny("held_item_place_first"));
            if (recipe.Ghosted)
                return UiPadPlan.No(button, Deny("recipe_unknown"));
            if (!recipe.Craftable || recipe.MaxCrafts < qty)
                return UiPadPlan.No(button, Deny("insufficient_ingredients"));
            if (ItemPolicy.RefuseCraft(e.Policy, recipe) is { } refused)
                return UiPadPlan.No(button, Deny("item_policy:" + refused));
            string does = (e.Cooking ? "cook " : "craft ") + qty + " " + recipe.DisplayName;
            // FIXB3 review MAJOR: the crafted item stayed on the CraftingPage cursor (GameMenu open, B refused). Vanilla's
            // gamepad tail (CraftingPage.cs:499-503) stores it into the inventory when it fits; the same bounded store-held
            // step runs after the click (the ledger counts the held item, so the craft judge is unchanged).
            return Click(button, does, mods, new EconomyExpect(EconomyExpect.Craft, recipe.ProducesId, qty, Produces: recipe.Produces,
                Ingredients: recipe.Ingredients), storeHeldAfter: true);
        }
        if (c.Kind == UiPadComponent.KindSlot)
        {
            if (f.HeldItemId is null)
                return UiPadPlan.No(button, Deny("crafting_slot_pick_not_admitted"));
            if (button != UiPadTable.BtnA || x.Any)
                return UiPadPlan.No(button, Deny("held_item_place_with_a"));
            if (PlaceRefusal(f, e, e.SlotAt(c.Id)) is { } occupied)
                return UiPadPlan.No(button, Deny(occupied));
            return Click(button, "place the crafted item", KbModifiers.None, new EconomyExpect(EconomyExpect.Place, f.HeldItemId, f.HeldStack));
        }
        return UiPadPlan.No(button, Deny("component_not_allowlisted"));
    }
}

/// <summary>ui_inspect publication of the economy facts (inside the ui_pad block; labels and prices only).</summary>
public static class EconomySnapshot
{
    static Dictionary<string, object> Item(EconItem i, string policy) => new(StringComparer.Ordinal)
    {
        ["item"] = i.QualifiedId, ["name"] = UiPadTable.Trim(i.Name), ["stack"] = i.Stack, ["quality"] = i.Quality,
        ["unit_price"] = i.UnitSellPrice, ["policy"] = policy,
    };

    public static Dictionary<string, object> Build(EconomyFacts e)
    {
        var block = new Dictionary<string, object>(StringComparer.Ordinal) { ["kind"] = e.Kind, ["money"] = e.Money };
        if (e.Kind == EconomyFacts.KindShop)
        {
            block["shop_id"] = UiPadTable.Trim(e.ShopId, 64);
            block["currency"] = e.Currency;
            block["ready"] = e.SafetyTimerMs <= 0 && !e.ReadOnly;
            block["rows"] = e.RowList.Take(ShopFactsTable.MaxRows).Select(r =>
            {
                var row = new Dictionary<string, object>(StringComparer.Ordinal)
                {
                    ["index"] = r.Index, ["item"] = r.ItemId, ["name"] = UiPadTable.Trim(r.Name), ["price"] = r.Price,
                    ["stock"] = r.Stock, ["fits"] = r.Fits, ["capacity"] = Math.Min(r.Capacity, 9999),
                };
                if (r.ComponentId is int id) row["id"] = id;
                if (r.TradeItem is { } trade)
                {
                    row["trade_item"] = trade; row["trade_count"] = r.TradeCount; row["trade_have"] = r.TradeHave;
                }
                if (r.RequiredToolId is { } required) row["required_tool"] = required;
                if (r.IsTool) row["tool"] = true;
                return row;
            }).ToArray();
            block["rows_total"] = e.RowList.Count;
            if (e.ShopId == ShopFactsTable.ClintUpgrade) block["upgrade_allowed"] = e.Policy.AllowToolUpgrade;
            block["sell"] = e.SlotList.Where(s => s.Item is not null).Select(s =>
            {
                string policy = !s.Sellable ? "shop_does_not_buy_this"
                    : ItemPolicy.RefuseDispose(e.Policy, ItemPolicy.Sell, s.Item!, 1) is { } why ? "refused:" + why : "ok";
                Dictionary<string, object> d = Item(s.Item!, policy);
                d["id"] = s.ComponentId;
                d["slot"] = s.Slot;
                return d;
            }).ToArray();
            if (e.ToolUpgrading is { } tool)
                block["tool_upgrading"] = tool;
        }
        else
        {
            block["cooking"] = e.Cooking;
            block["recipes"] = e.RecipeList.Take(ShopFactsTable.MaxRecipes).Select(r => new Dictionary<string, object>(StringComparer.Ordinal)
            {
                ["id"] = r.ComponentId, ["name"] = UiPadTable.Trim(r.DisplayName), ["recipe"] = UiPadTable.Trim(r.Name),
                ["item"] = r.ProducesId, ["produces"] = r.Produces, ["craftable"] = r.Craftable && !r.Ghosted,
                ["known"] = !r.Ghosted, ["max_crafts"] = Math.Min(999, r.MaxCrafts),
                ["ingredients"] = r.Ingredients.Take(ShopFactsTable.MaxIngredients).Select(i => new Dictionary<string, object>
                    { ["item"] = i.Key, ["need"] = i.Need, ["have"] = i.Have }).ToArray(),
            }).ToArray();
        }
        return block;
    }
}

/// <summary>
/// Private-member reflection binding (PRB-6): resolved by name + exact parameter types against the INSTALLED assembly
/// and only on the pinned MVID (ModEntry); any miss fails closed ("reflection_unbound", the click path stays).
/// </summary>
public static class EconomyReflection
{
    public const string TryToPurchaseItem = "tryToPurchaseItem";
    public const string ClickCraftingRecipe = "clickCraftingRecipe";
    public const string SellPercentage = "sellPercentage";

    /// <summary>Expected parameter type names (decomp ShopMenu.cs:1319 / CraftingPage.cs:433).</summary>
    public static readonly string[] TryToPurchaseItemParams = { "ISalable", "ISalable", "Int32", "Int32", "Int32" };
    public static readonly string[] ClickCraftingRecipeParams = { "ClickableTextureComponent", "Boolean" };

    public static bool Matches(IReadOnlyList<string> actual, IReadOnlyList<string> expected) =>
        actual.Count == expected.Count && actual.Zip(expected).All(p => p.First == p.Second);

    public static bool Bound(bool mvidPinned, bool methodFound, IReadOnlyList<string>? parameterTypes, string returnType) =>
        mvidPinned && methodFound && parameterTypes is not null && Matches(parameterTypes, TryToPurchaseItemParams)
        && returnType == "Boolean";
}

/// <summary>Short sha256 (hex) helper shared by the shipping-bin snapshot.</summary>
internal static class EconomyHash
{
    public static string Of(string value) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();
}
