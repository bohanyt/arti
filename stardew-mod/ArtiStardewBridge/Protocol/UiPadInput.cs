using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

// Primitives v2 WS-G1 (PLAN.md 1.1-1.4, WS-G1 brief sections 2-6): the generic menu layer.
//   ui_inspect = read-only publication of the active menu (ui.context "ui_pad"; fields and pure getters only,
//                never performHoverAction, which rewrites DialogueBox.selectedResponse / the snap, DialogueBox.cs:500-543);
//   ui_nav(dir) = one snap step on the snap owner (GameMenu: its current page, GameMenu.cs:448) through
//                applyMovementKey (IClickableMenu.cs:315); the OS-cursor guard (WS-G0) blocks the snap warp;
//   ui_press(btn) = the vanilla dispatcher's calls mirrored as direct calls on the deepest menu (G1:4878-4887,
//                5049-5081): receiveGamePadButton(b), then for A/X receiveLeftClick / receiveRightClick at the
//                snapped component's centre, then receiveKeyPress(mapGamePadButtonToKey(b)). Menus get no input
//                dispatch while the window is inactive (G1:4842-4849), so no pad Press is ever involved.
// The per-context table lives here (the mod), never in the model: every "-" cell and every denylist row is
// refused BEFORE any call (zero calls). The v1 ui_key kinds (dialogue advance / question answer + cancel /
// level-up OK / shipping OK / letter close) are re-expressed as ui_press on their contexts with ui_key's own
// call underneath (UiPadStepKind.UiKey), so the day-start rule and the v1 receipts stay byte-identical.
// SMAPI-free: ModEntry.Ui.cs reads the game into UiPadFacts and implements IUiPadPort.
// Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G1 live witness.

public static class UiPadTable
{
    public const string PrimitiveNav = "ui_nav";
    public const string PrimitivePress = "ui_press";
    public const string CapabilityKey = "ui_pad";

    public const string CtxGameMenu = "game_menu", CtxLetter = "letter", CtxBillboard = "billboard",
        CtxSpecialOrders = "special_orders", CtxQuestLog = "quest_log", CtxDialogue = "dialogue",
        CtxQuestion = "question", CtxNpcQuestion = "npc_question", CtxLevelUp = "level_up",
        CtxProfession = "level_up_profession", CtxShipping = "shipping", CtxItemGrab = "item_grab",
        CtxPondQuery = "pond_query", CtxEvent = "event", CtxTextEntry = "text_entry", CtxOther = "other",
        // WS-G2 (EconomyMenusEnabled only; flag off = ShopMenu / standalone CraftingPage stay "other").
        CtxShop = "shop", CtxCrafting = "crafting",
        // WS-G6 (WorldExtrasEnabled only): the elevator floor menu, the Junimo bundle note, the museum donation menu.
        CtxMineElevator = "mine_elevator", CtxJunimoNote = "junimo_note", CtxMuseum = "museum";

    // WS-G3 menu contexts (only with TextEventMenusEnabled; UiG3Table): NamingMenu, NumberSelectionMenu, ReadyCheckDialog.
    public const string CtxNaming = UiG3Table.CtxNaming, CtxNumber = UiG3Table.CtxNumber, CtxReadyCheck = UiG3Table.CtxReadyCheck;

    public static readonly IReadOnlyList<string> Contexts = new[]
    {
        CtxGameMenu, CtxLetter, CtxBillboard, CtxSpecialOrders, CtxQuestLog, CtxDialogue, CtxQuestion, CtxNpcQuestion,
        CtxLevelUp, CtxProfession, CtxShipping, CtxItemGrab, CtxPondQuery, CtxEvent, CtxTextEntry, CtxOther,
        CtxNaming, CtxNumber, CtxReadyCheck,
        CtxShop, CtxCrafting,
        CtxMineElevator, CtxJunimoNote, CtxMuseum, PurchaseAnimalsUi.Context,
        CharCustomTable.Context, // Live7 character creation (CharacterCreationEnabled only)
    };

    /// <summary>applyMovementKey directions: 0 up, 1 right, 2 down, 3 left (IClickableMenu.cs:315).</summary>
    public static readonly IReadOnlyList<string> Directions = new[] { "up", "right", "down", "left" };

    public const string BtnA = "A", BtnB = "B", BtnX = "X", BtnY = "Y", BtnLB = "LB", BtnRB = "RB", BtnLT = "LT",
        BtnRT = "RT", BtnStart = "Start", BtnSelect = "Select", BtnTab = "tab";

    /// <summary>The ui_press buttons (PLAN 1.1). "tab" = a named GameMenu tab by direct changeTab(n).</summary>
    public static readonly IReadOnlyList<string> PressButtons = new[]
        { BtnA, BtnB, BtnX, BtnY, BtnLB, BtnRB, BtnLT, BtnRT, BtnStart, BtnSelect, BtnTab };

    public const string ReservedText = "text", ReservedValue = "value";

    /// <summary>Reserved ui_press payloads (G2 shop/craft quantities, G3 text / value / choice / festival): parsed;
    /// G3 contexts consume theirs (UiG3Table.ArgsFor); WS-G2 consumes qty (int 1..999) / half (true) / stack (true) on
    /// the shop and crafting rows (EconomyPlans); quick stays refused (chests use container_transfer mode quick).</summary>
    public static readonly IReadOnlyList<string> ReservedArgs = new[]
        { "qty", "half", "stack", "quick", ReservedText, ReservedValue, "choice", "festival", "slot", "item", "building", "shift", "settle" };

    /// <summary>GameMenu tab names in tab order (GameMenu.cs:12-30; same keys as GameTabSemantics).</summary>
    public static readonly IReadOnlyList<string> TabNames = new[]
        { "inventory", "skills", "social", "map", "crafting", "animals", "powers", "collections", "options", "exit" };

    public static readonly IReadOnlyList<string> PageTypes = new[]
    {
        "InventoryPage", "SkillsPage", "SocialPage", "MapPage", "CraftingPage", "AnimalPage", "PowersTab",
        "CollectionsPage", "OptionsPage", "ExitPage",
    };

    public const int InventoryTab = 0, MapTab = 3, CraftingTab = 4, ExitTab = 9, LastTab = 9;
    public const int TabIdBase = 12340;

    /// <summary>InventoryPage trash (InventoryPage.cs:97-108; Utility.trashItem :577-582).</summary>
    public const int InventoryTrash = 105;
    /// <summary>InventoryPage organize (organizes the player inventory; organize goal only).</summary>
    public const int InventoryOrganize = 106;
    /// <summary>ItemGrabMenu trash (trashCan.myID = 106, ItemGrabMenu.cs:165; its organize button shares 106, :379).</summary>
    public const int ItemGrabTrash = 106;
    /// <summary>The player InventoryMenu's invisible drop button (InventoryMenu.cs:122-124; drops the held item).</summary>
    public const int DropButton = 107;

    public const int MaxComponents = 48, MaxLabel = 80, MaxText = 128, MaxBodyText = 4096;

    public const string Denied = "UI_DENIED", NoMeaning = "NoMeaning";

    public static int DirectionIndex(string? dir) => dir is null ? -1 : IndexOf(Directions, dir);

    public static int TabIndex(string? name) => name is null ? -1 : IndexOf(TabNames, name);

    public static int TabOfPage(string? pageType) => pageType is null ? -1 : IndexOf(PageTypes, pageType);

    static int IndexOf(IReadOnlyList<string> list, string value)
    {
        for (int i = 0; i < list.Count; i++)
            if (string.Equals(list[i], value, StringComparison.Ordinal))
                return i;
        return -1;
    }

    public static string Trim(string? text, int max = MaxLabel)
    {
        if (string.IsNullOrEmpty(text))
            return "";
        string flat = text.Replace('\n', ' ').Replace('\r', ' ').Replace('^', ' ').Trim();
        return flat.Length <= max ? flat : flat[..max];
    }
}

/// <summary>One visible snap target as read from fields (bounds stay in the mod / runtime: never in the prompt).</summary>
public sealed record UiPadComponent(int Id, string Kind, string Label, string? ItemId = null, int Stack = 0, int Quality = 0,
    int Up = -1, int Right = -1, int Down = -1, int Left = -1, bool Visible = true, int X = 0, int Y = 0, int W = 0, int H = 0,
    string? HoverText = null, string? FieldPath = null, string? TextValue = null, bool? AcceptsItem = null,
    bool ProtectedItem = false)
{
    public const string KindSlot = "slot", KindEquipment = "equipment", KindTab = "tab", KindResponse = "response",
        KindItem = "item", KindClose = "close", KindButton = "button";

    public int CenterX => this.X + this.W / 2;
    public int CenterY => this.Y + this.H / 2;
}

/// <summary>Raw facts of the deepest menu (one tick). All read-only.</summary>
public sealed record UiPadFacts(
    string MenuType,
    bool MenuOpen = true,
    string? ReceiverToken = null,
    string? PageType = null,
    int Tab = -1,
    bool IsActive = true,
    bool? ReadyToClose = true,
    bool HasChildOrParent = false,
    bool EventUp = false,
    bool TextSubscriber = false,
    bool MinigameActive = false,
    int? SnappedId = null,
    IReadOnlyList<UiPadComponent>? Components = null,
    string? HeldItemId = null,
    int HeldStack = 0,
    int PageIndex = 0,
    UiKeyState? UiKey = null,
    bool ForcePreventClose = false,
    int InventorySlots = 0,
    string? Text = null,
    bool LetterFromCollection = false,
    UiG3Facts? G3 = null,
    EconomyFacts? Economy = null,
    // WS-G6: menu facts of the G6 contexts (null = WorldExtrasEnabled off: those menus stay "other", as in G1).
    UiPadExtrasFacts? Extras = null,
    // FIXB7 D-B5b-3: the active ItemGrabMenu is the exact shipping-bin menu (EconomyMenusEnabled on; else false).
    bool ShippingBin = false, PurchaseAnimalsFacts? PurchaseAnimals = null,
    // Live7: the farmhand CharacterCustomization (null = CharacterCreationEnabled off, another source or another menu).
    CharCustomFacts? CharCustom = null,
    // Ordinary fallback menus: field-read controls; close clicks dispatch the observed exit itself.
    bool GenericMenu = false, string? GenericLedger = null, bool GenericHud = false, IReadOnlyDictionary<string, object>? GenericValues = null, IReadOnlyList<string>? GenericState = null,
    CursorItemSettleSnapshot? CursorSettle = null, IReadOnlyList<string>? GenericAmbientState = null,
    IReadOnlyDictionary<string, object>? GenericIdentityProbe = null, GenericShopState? ShopCatalog = null)
{
    public IReadOnlyList<UiPadComponent> Comps => this.Components ?? Array.Empty<UiPadComponent>();

    public UiPadComponent? Snapped => this.SnappedId is int id ? this.Comps.FirstOrDefault(c => c.Id == id && c.Visible) : null;

    public static UiPadFacts Closed() => new("", MenuOpen: false);
}

public enum UiPadStepKind
{
    /// <summary>receiveGamePadButton(b) on the deepest menu (G1:4878-4887).</summary>
    GamePadButton,
    /// <summary>receiveLeftClick at the snapped centre (G1:5049-5055).</summary>
    LeftClick,
    /// <summary>receiveRightClick at the snapped centre (G1:5060-5064).</summary>
    RightClick,
    /// <summary>receiveKeyPress(Utility.mapGamePadButtonToKey(b)) (G1:5069-5081).</summary>
    KeyPress,
    /// <summary>GameMenu.changeTab(n) (GameMenu.cs:419), the vanilla tab-click path.</summary>
    ChangeTab,
    /// <summary>The v1 ui_key call underneath (ModEntry.UiKey.cs InvokeUiKey, its readiness and settle).</summary>
    UiKey,
    /// <summary>A WS-G3 direct call (UiG3Executor: text / number / event / ready check / festival).</summary>
    G3,
    /// <summary>WS-G2: ShopMenu's private tryToPurchaseItem(forSale[row], heldItem, qty, x, y) + the vanilla buy-loop tail
    /// (ShopMenu.cs:1101-1140) for a qty with no vanilla modifier (PRB-6, pinned assembly only).</summary>
    ShopBuyDirect,
    /// <summary>WS-G2: the vanilla SnappyMenus tail (ShopMenu.cs:1132): a bought heldItem goes into the inventory if it fits.</summary>
    ShopStoreHeld,
    /// <summary>WS-G6 museum: receiveLeftClick at a free suitable museum tile the mod picks (MuseumMenu.cs:208-249).</summary>
    MuseumPlace,
    AnimalClick,
    GenericInput,
    /// <summary>Live7: one character-creation control (CharCustomExecutor: a native click or the text-entry primitive).</summary>
    CharCustom,
}

public readonly record struct UiPadStep(UiPadStepKind Kind, string Button = "", int Tab = -1, string? UiKeyKind = null,
    string? UiKeyKey = null, string? G3Action = null, string? G3Arg = null, int Qty = 0, int RowIndex = -1,
    int? ChoiceIndex = null)
{
    public override string ToString() => this.Kind switch
    {
        UiPadStepKind.GamePadButton => "receiveGamePadButton(" + this.Button + ")",
        UiPadStepKind.LeftClick => "receiveLeftClick(snapped)",
        UiPadStepKind.RightClick => "receiveRightClick(snapped)",
        UiPadStepKind.KeyPress => "receiveKeyPress(" + this.Button + ")",
        UiPadStepKind.ChangeTab => "changeTab(" + this.Tab + ")",
        UiPadStepKind.G3 => "g3(" + this.G3Action + ")",
        UiPadStepKind.ShopBuyDirect => "tryToPurchaseItem(row " + this.RowIndex + ", qty " + this.Qty + ")",
        UiPadStepKind.ShopStoreHeld => "storeHeldItem",
        UiPadStepKind.MuseumPlace => "receiveLeftClick(free museum tile)",
        UiPadStepKind.CharCustom => "char_custom(" + this.G3Action + ")",
        _ => "ui_key(" + this.UiKeyKind + ":" + this.UiKeyKey + ")",
    };
}

/// <summary>What one button does in the current context: the mirror steps, or the refusal (zero calls).</summary>
public sealed record UiPadPlan(string Button, string? Refusal, IReadOnlyList<UiPadStep> Steps, string Does,
    KbModifiers? Modifiers = null, EconomyExpect? Expect = null)
{
    /// <summary>WS-G2: the plan writes Game1.oldKBState around its clicks and / or is judged on the economy ledger.</summary>
    public bool IsEconomy => this.Modifiers is not null || this.Expect is not null
        || this.Steps.Any(s => s.Kind is UiPadStepKind.ShopBuyDirect or UiPadStepKind.ShopStoreHeld);

    public bool Allowed => this.Refusal is null;

    public bool IsUiKey => this.Steps.Count == 1 && this.Steps[0].Kind == UiPadStepKind.UiKey;

    public bool IsG3 => this.Allowed && this.Steps.Count == 1 && this.Steps[0].Kind == UiPadStepKind.G3;

    public static UiPadPlan No(string button, string refusal) => new(button, refusal, Array.Empty<UiPadStep>(), "");

    public static UiPadPlan Ok(string button, string does, params UiPadStep[] steps) => new(button, null, steps, does);

    public Dictionary<string, object> Wire()
    {
        var cell = new Dictionary<string, object>(StringComparer.Ordinal);
        if (this.Refusal is { } refused)
            cell["refused"] = refused;
        else
            cell["does"] = this.Does;
        return cell;
    }
}

public static class UiPadContextTable
{
    internal static string Deny(string why) => UiPadTable.Denied + ":" + why;
    internal static string None(string why) => UiPadTable.NoMeaning + ":" + why;

    public static string ContextOf(UiPadFacts f)
    {
        if (!f.MenuOpen)
            return UiPadTable.CtxOther;
        // Live7 (flag on only): the farmhand CharacterCustomization, before the text-entry rule (its own name / favourite
        // box may be the keyboard subscriber; a foreign subscriber is refused by CharCustomPolicy.Gate instead).
        if (f.MenuType == CharCustomTable.MenuType && f.CharCustom is not null && !f.EventUp && !f.MinigameActive)
            return CharCustomTable.Context;
        if (f.GenericMenu)
            return f.UiKey is { Options.Count: > 0 } ? UiPadTable.CtxQuestion : UiPadTable.CtxOther;
        if (f.MenuType == "PurchaseAnimalsMenu"&& f.PurchaseAnimals is { } animals && !f.EventUp && !f.MinigameActive)
            return animals.Phase == "naming" && f.G3?.Naming is not null ? UiPadTable.CtxNaming : PurchaseAnimalsUi.Context;
        // WS-G3 (flag on only): NamingMenu / NumberSelectionMenu / ReadyCheckDialog before eventUp (they open
        // inside events and festivals too).
        if (f.G3 is { Enabled: true } && UiG3Table.MenuContext(f.MenuType) is { } g3)
            return g3;
        if (f.EventUp || f.MinigameActive)
            return UiPadTable.CtxEvent;
        if (f.TextSubscriber)
            return UiPadTable.CtxTextEntry;
        return f.MenuType switch
        {
            "GameMenu" => UiPadTable.CtxGameMenu,
            "LetterViewerMenu" => f.LetterFromCollection ? UiPadTable.CtxOther : UiPadTable.CtxLetter,
            "Billboard" => UiPadTable.CtxBillboard,
            "SpecialOrdersBoard" => UiPadTable.CtxSpecialOrders,
            "QuestLog" => UiPadTable.CtxQuestLog,
            "ItemGrabMenu" => UiPadTable.CtxItemGrab,
            "PondQueryMenu" => UiPadTable.CtxPondQuery,
            "DialogueBox" => f.UiKey?.Kind switch
            {
                UiKeyTable.Question => UiPadTable.CtxQuestion,
                UiKeyTable.NpcQuestion => UiPadTable.CtxNpcQuestion,
                _ => UiPadTable.CtxDialogue,
            },
            "LevelUpMenu" => f.UiKey?.Kind == UiKeyTable.LevelUpProfession ? UiPadTable.CtxProfession : UiPadTable.CtxLevelUp,
            "ShippingMenu" => UiPadTable.CtxShipping,
            // Precedence (explicit): G3 modal dialogs (above) > event / minigame > text entry > economy (shop / crafting)
            // > WS-G6 (mine_elevator / junimo_note / museum) > the G1 generic rows. The menu types are disjoint, so the
            // switch order only documents it; a G6 menu inside an event stays "event".
            "ShopMenu" when f.Economy is { Kind: EconomyFacts.KindShop } => UiPadTable.CtxShop,
            "CraftingPage" when f.Economy is { Kind: EconomyFacts.KindCrafting } => UiPadTable.CtxCrafting,
            "MineElevatorMenu" when f.Extras is not null => UiPadTable.CtxMineElevator,
            "JunimoNoteMenu" when f.Extras is not null => UiPadTable.CtxJunimoNote,
            "MuseumMenu" when f.Extras is not null => UiPadTable.CtxMuseum,
            _ => UiPadTable.CtxOther,
        };
    }

    /// <summary>
    /// Snap ids ui_nav must never land on and ui_press must never click (PLAN 1.4): InventoryPage trash 105,
    /// ItemGrabMenu trash 106 (shared with its organize button), the player inventory's drop button 107.
    /// </summary>
    public static IReadOnlyCollection<int> DenylistedIds(string context, string? pageType) => context switch
    {
        UiPadTable.CtxGameMenu when pageType == "InventoryPage" => new[] { UiPadTable.InventoryTrash, UiPadTable.DropButton },
        // CraftingPage trash 106 (CraftingPage.cs:89; Utility.trashItem :411-414) + drop 107 (:93, :416-420).
        UiPadTable.CtxGameMenu when pageType == "CraftingPage" => new[] { UiPadTable.ItemGrabTrash, UiPadTable.DropButton },
        UiPadTable.CtxCrafting => new[] { UiPadTable.ItemGrabTrash, UiPadTable.DropButton },
        UiPadTable.CtxShop => new[] { UiPadTable.DropButton },
        UiPadTable.CtxItemGrab => new[] { UiPadTable.ItemGrabTrash, UiPadTable.DropButton },
        // WS-G6: both menus carry an InventoryMenu whose invisible drop button drops the held item.
        UiPadTable.CtxJunimoNote or UiPadTable.CtxMuseum => new[] { UiPadTable.DropButton },
        _ => Array.Empty<int>(),
    };

    /// <summary>
    /// Contexts in which ui_nav may step the snap (read contexts included: a snap step is no effect). NOT the chest
    /// (item_grab): v1 container_transfer / container_exit require an unsnapped ItemGrabMenu (ModEntry.cs
    /// "snapped_component" refusal) and nothing clears a snap once set, so one nav would break the store chain.
    /// </summary>
    public static bool NavContext(string context) => context == PurchaseAnimalsUi.Context || context is UiPadTable.CtxGameMenu or UiPadTable.CtxLetter
        or UiPadTable.CtxBillboard or UiPadTable.CtxSpecialOrders or UiPadTable.CtxQuestLog or UiPadTable.CtxQuestion
        or UiPadTable.CtxNpcQuestion or UiPadTable.CtxPondQuery or UiPadTable.CtxShop or UiPadTable.CtxCrafting
        or UiPadTable.CtxMineElevator or UiPadTable.CtxJunimoNote or UiPadTable.CtxMuseum;

    /// <summary>Gates every ui_nav / ui_press shares (refused before any call).</summary>
    public static string? CommonGate(UiPadFacts f, string context)
    {
        if (!f.MenuOpen && !f.GenericHud)
            return Deny("no_menu");
        if (context == UiPadTable.CtxEvent && f.G3 is not { Enabled: true })
            return Deny("event_g3");
        if (context == UiPadTable.CtxTextEntry)
            return Deny("text_entry_g3");
        if (!f.IsActive)
            return Deny("menu_inactive");
        if (f.HasChildOrParent && !f.GenericMenu)
            return Deny("child_menu");
        return null;
    }

    static readonly UiPadStep[] NoSteps = Array.Empty<UiPadStep>();

    internal static UiPadPlan MirrorClick(string button, string does) => UiPadPlan.Ok(button, does,
        new UiPadStep(UiPadStepKind.GamePadButton, button),
        new UiPadStep(button == UiPadTable.BtnX ? UiPadStepKind.RightClick : UiPadStepKind.LeftClick, button),
        new UiPadStep(UiPadStepKind.KeyPress, button));

    internal static UiPadPlan MirrorClose(string button, string does) => UiPadPlan.Ok(button, does,
        new UiPadStep(UiPadStepKind.GamePadButton, button),
        new UiPadStep(UiPadStepKind.KeyPress, button));

    /// <summary>The responses a pinned TV / Blacksmith / Marnie question may answer (vanilla response keys:
    /// TV.checkForAction, GameLocation.blacksmith / animalShop, GameLocation.cs:10997-11010 / 11034-11043).</summary>
    static bool PinnedResponse(string? questionKey, string? responseKey) => questionKey switch
    {
        "TV" => responseKey is "Weather" or "Fortune" or "(Leave)",
        "Marnie" => responseKey is "Purchase" or "Leave",
        UiKeyTable.GiftQuestionKey => UiKeyTable.GiftResponse(responseKey),
        _ => responseKey is "Upgrade" or "Leave",
    };

    static UiPadPlan ViaUiKey(string button, UiKeyState? state, string kind, string key, string does)
    {
        if (state is null || state.Kind != kind)
            return UiPadPlan.No(button, Deny("ui_key_kind_mismatch"));
        if (!state.Ready)
            return UiPadPlan.No(button, Deny("not_ready:" + (state.NotReadyReason ?? "unknown")));
        if (!UiKeyTable.IsAllowed(state.MenuType, kind, key))
            return UiPadPlan.No(button, Deny("ui_key_outside_table"));
        return UiPadPlan.Ok(button, does, new UiPadStep(UiPadStepKind.UiKey, button, UiKeyKind: kind, UiKeyKey: key));
    }

    /// <summary>Named tab / LT / RT target with the map + exit guard (GameMenu.cs:239-265, 244-258).</summary>
    public static string? TabGuard(UiPadFacts f, int target)
    {
        if (f.Tab == UiPadTable.MapTab || target == UiPadTable.MapTab)
            return Deny("map_tab_replaces_menu");
        if (f.Tab == UiPadTable.ExitTab || target == UiPadTable.ExitTab)
            return Deny("exit_tab");
        if (target < 0 || target > UiPadTable.LastTab)
            return None("no_tab");
        if (target == f.Tab)
            return None("same_tab");
        if (f.ForcePreventClose || f.ReadyToClose != true)
            return Deny("not_ready_to_close");
        return null;
    }

    // Shared native pre-arm admission: keep the reserved-payload gate ahead of translation.
    // TV is authoritative only when UiKeyClassifier has accepted the exact pinned question.
    public static UiPadPlan AdmitPress(UiPressTarget target, UiPadFacts facts)
    {
        string context = ContextOf(facts);
        bool g3 = facts.G3 is { Enabled: true } && UiG3Table.IsG3Context(context);
        bool pinnedChoice = context == UiPadTable.CtxQuestion && facts.UiKey is { Ready: true, QuestionKey: "TV" or "Blacksmith" or "Marnie" or UiKeyTable.GiftQuestionKey };
        if (context == CharCustomTable.Context)
            // Live7: choice / value / text are this context's own payload (CharCustomPolicy refuses every other key).
            return Translate(target.Button, facts, target.Tab, target.Reserved ?? new Dictionary<string, string>(StringComparer.Ordinal));
        if (facts.GenericMenu)
            return Translate(target.Button, facts, reserved: target.Reserved ?? new Dictionary<string, string>());
        if (context == PurchaseAnimalsUi.Context)
            return Translate(target.Button, facts, reserved: target.Reserved ?? new Dictionary<string, string>());
        bool economy = facts.Economy is not null && (context is UiPadTable.CtxShop or UiPadTable.CtxCrafting
            || (context == UiPadTable.CtxGameMenu && facts.PageType == "CraftingPage"));
        if (!g3 && !economy && target.Reserved is { Count: > 0 } reserved)
        {
            string? denied = reserved.Keys.OrderBy(k => k, StringComparer.Ordinal)
                .FirstOrDefault(k => !(pinnedChoice && k == "choice"));
            if (denied is not null)
                return UiPadPlan.No(target.Button, Deny("reserved_arg_g2_g3:" + denied));
        }
        return Translate(target.Button, facts, target.Tab,
            g3 || pinnedChoice ? target.Reserved ?? new Dictionary<string, string>(StringComparer.Ordinal) : null,
            g3 || pinnedChoice ? null : target.Extras);
    }

    /// <summary>What <paramref name="button"/> means now (PLAN 1.2 menu rows). Refusals carry zero steps.</summary>
    public static UiPadPlan Translate(string button, UiPadFacts f, string? tabName = null,
        IReadOnlyDictionary<string, string>? reserved = null, UiPressExtras? extras = null)
    {
        if (!UiPadTable.PressButtons.Contains(button, StringComparer.Ordinal))
            return UiPadPlan.No(button, None("unknown_button"));
        string context = ContextOf(f);
        if (CommonGate(f, context) is { } gate)
            return UiPadPlan.No(button, gate);
        if (context == CharCustomTable.Context)
            return button == UiPadTable.BtnTab ? UiPadPlan.No(button, None("tab_outside_game_menu"))
                : CharCustomPolicy.Translate(button, f, reserved);
        if (f.GenericMenu) return Generic(button, f, reserved);
        if (f.MenuType == "PurchaseAnimalsMenu" && f.PurchaseAnimals is { } purchase)
        {
            if (context == PurchaseAnimalsUi.Context || button == UiPadTable.BtnB) return PurchaseAnimalsUi.Translate(button, f, reserved);
            if (!purchase.Ready || purchase.Money < purchase.Price || purchase.Animal is null
                || !purchase.Buildings.Any(b => b.Ref == purchase.SelectedBuilding))
                return UiPadPlan.No(button, Deny("animal_not_ready"));
            if (reserved?.TryGetValue("text", out var name) == true && purchase.Names.Contains(name))
                return UiPadPlan.No(button, Deny("animal_duplicate_name"));
        }
        if (f.G3 is { Enabled: true } && UiG3Table.IsG3Context(context))
            // WS-G3: text / number / ready check / event rows (null reserved = describe for the ui_inspect row).
            return button == UiPadTable.BtnTab ? UiPadPlan.No(button, None("tab_outside_game_menu"))
                : UiG3Table.Translate(button, f, context, reserved);
        if (button == UiPadTable.BtnTab && context != UiPadTable.CtxGameMenu)
            return UiPadPlan.No(button, None("tab_outside_game_menu"));
        if (context == UiPadTable.CtxQuestion && f.UiKey?.QuestionKey is "TV" or "Blacksmith" or "Marnie" or UiKeyTable.GiftQuestionKey)
        {
            if (button != UiPadTable.BtnA)
                return UiPadPlan.No(button, None("tv_read_channels_only"));
            int? index = f.SnappedId ?? 0;
            // W1 gap7t (live gap5 L666 / gap7a L673: snapped_id -1, gamepad controls off, no native auto-snap): the
            // published row (reserved null, never an execution path: AdmitPress passes a non-null dictionary for a
            // pinned question) described index 0 = "Shop" / "Supplies" and so refused A for Clint and Marnie, while
            // the TV's index 0 "Weather" happened to pass. Unless the snapped response is itself admissible, the row
            // describes the first observed admissible response; the press still needs that explicit choice (an empty
            // reserved answers the snapped / index-0 response and is refused here before any call).
            if (reserved is null && f.UiKey.Options is { Count: > 0 } observed
                && !(index is int snapped && snapped >= 0 && snapped < observed.Count
                    && PinnedResponse(f.UiKey.QuestionKey, observed[snapped].ResponseKey)))
                index = Enumerable.Range(0, observed.Count).Cast<int?>()
                    .FirstOrDefault(i => PinnedResponse(f.UiKey.QuestionKey, observed[i!.Value].ResponseKey)) ?? index;
            if (reserved is { Count: > 0 })
            {
                if (reserved.Count != 1 || !reserved.TryGetValue("choice", out string? raw)
                    || !int.TryParse(raw, out int choice) || choice < 0)
                    return UiPadPlan.No(button, Deny("tv_choice_invalid"));
                index = choice;
            }
            if (index is not int i || i < 0 || i >= (f.UiKey.Options?.Count ?? 0)
                || !PinnedResponse(f.UiKey.QuestionKey, f.UiKey.Options![i].ResponseKey))
                return UiPadPlan.No(button, Deny("tv_channel_not_readable"));
            var plan = ViaUiKey(button, f.UiKey, UiKeyTable.Question, UiKeyTable.Answer, f.UiKey.QuestionKey == "Marnie" ? "choose the offered Marnie service" : f.UiKey.QuestionKey == UiKeyTable.GiftQuestionKey ? "answer the offered gift (Yes / No)" : f.UiKey.QuestionKey == "Blacksmith" ? "choose the offered Blacksmith service" : "read the offered TV channel");
            return plan.Allowed ? plan with { Steps = plan.Steps.Select(s => s with { ChoiceIndex = i }).ToArray() } : plan;
        }
        UiPressExtras x = extras ?? UiPressExtras.None;
        bool economyRow = context is UiPadTable.CtxShop or UiPadTable.CtxCrafting
            || (context == UiPadTable.CtxGameMenu && f.PageType == "CraftingPage" && f.Economy is { Kind: EconomyFacts.KindCrafting }
                && button is UiPadTable.BtnA or UiPadTable.BtnX);
        if (x.Any && !economyRow)
            // G1 contract: payloads only where the WS-G2 economy rows give them a meaning (text / value: G3).
            return UiPadPlan.No(button, Deny("reserved_arg_g2_g3:" + x.FirstKey));
        return context switch
        {
            UiPadTable.CtxShop => EconomyPlans.Shop(button, f, f.Economy!, x),
            UiPadTable.CtxCrafting => EconomyPlans.Crafting(button, f, f.Economy!, x, standalone: true),
            UiPadTable.CtxGameMenu => GameMenu(button, f, tabName, x),
            UiPadTable.CtxLetter => Letter(button, f),
            UiPadTable.CtxBillboard => Board(button, f, new[] { 0 }, "accept the daily quest"),
            UiPadTable.CtxSpecialOrders => Board(button, f, new[] { 0, 1 }, "accept the snapped special order"),
            UiPadTable.CtxQuestLog => QuestLog(button, f),
            UiPadTable.CtxDialogue => button == UiPadTable.BtnA
                ? ViaUiKey(button, f.UiKey, UiKeyTable.Dialogue, UiKeyTable.Advance, "advance the dialogue")
                : UiPadPlan.No(button, None("dialogue")),
            UiPadTable.CtxQuestion => Question(button, f, UiKeyTable.Question),
            UiPadTable.CtxNpcQuestion => Question(button, f, UiKeyTable.NpcQuestion),
            UiPadTable.CtxLevelUp => button == UiPadTable.BtnA
                ? ViaUiKey(button, f.UiKey, UiKeyTable.LevelUp, UiKeyTable.Confirm, "OK (level-up)")
                : UiPadPlan.No(button, None("level_up")),
            UiPadTable.CtxProfession => UiPadPlan.No(button, Deny("profession_policy_runtime")),
            UiPadTable.CtxShipping => button == UiPadTable.BtnA
                ? ViaUiKey(button, f.UiKey, UiKeyTable.ShippingSummary, UiKeyTable.Confirm, "OK (shipping summary)")
                : UiPadPlan.No(button, None("shipping")),
            UiPadTable.CtxItemGrab => button switch
            {
                UiPadTable.BtnA or UiPadTable.BtnX => UiPadPlan.No(button, Deny("use_container_transfer")),
                // FIXB7 D-B5b-3: the shipping bin has no container_exit (chest only): its B closes it like the game's
                // menu button (ItemGrabMenu.receiveKeyPress, canExitOnKey) once nothing is on the cursor.
                UiPadTable.BtnB when f.ShippingBin => f.HeldItemId is not null
                    ? UiPadPlan.No(button, Deny("cursor_item")) : MirrorClose(button, "close the shipping bin"),
                UiPadTable.BtnB or UiPadTable.BtnStart => UiPadPlan.No(button, Deny("use_container_exit")),
                // ItemGrabMenu Select organizes the PLAYER inventory (ItemGrabMenu.cs:1203-1208), not the chest.
                UiPadTable.BtnSelect => UiPadPlan.No(button, Deny("select_organizes_player_inventory")),
                _ => UiPadPlan.No(button, None("item_grab")),
            },
            UiPadTable.CtxPondQuery => button == UiPadTable.BtnB
                ? Close(button, f, "close the pond menu")
                : button == UiPadTable.BtnA ? UiPadPlan.No(button, Deny("read_only_menu")) : UiPadPlan.No(button, None("pond_query")),
            UiPadTable.CtxMineElevator => UiPadExtrasTable.MineElevator(button, f),
            UiPadTable.CtxJunimoNote => UiPadExtrasTable.JunimoNote(button, f),
            UiPadTable.CtxMuseum => UiPadExtrasTable.Museum(button, f),
            UiPadTable.CtxOther when f.GenericMenu => Generic(button, f, reserved),
            _ => UiPadPlan.No(button, None("context_unsupported")),
        };
    }

    static UiPadPlan Generic(string button, UiPadFacts f, IReadOnlyDictionary<string, string>? reserved) =>
        GenericUiPolicy.Plan(button, f, reserved);

    static UiPadPlan Close(string button, UiPadFacts f, string does) =>
        f.ForcePreventClose || f.ReadyToClose != true ? UiPadPlan.No(button, Deny("not_ready_to_close")) : MirrorClose(button, does);

    static UiPadPlan GameMenu(string button, UiPadFacts f, string? tabName, UiPressExtras x)
    {
        string? page = f.PageType;
        int tab = f.Tab;
        switch (button)
        {
            case UiPadTable.BtnB:
            case UiPadTable.BtnStart:
                return Close(button, f, "close the game menu");
            case UiPadTable.BtnLT:
            case UiPadTable.BtnRT:
            {
                int target = tab + (button == UiPadTable.BtnRT ? 1 : -1);
                return TabGuard(f, target) is { } refused ? UiPadPlan.No(button, refused)
                    // GameMenu's own trigger handler (GameMenu.cs:239-265); the key press maps to Keys.None (skipped).
                    : UiPadPlan.Ok(button, "tab " + UiPadTable.TabNames[target], new UiPadStep(UiPadStepKind.GamePadButton, button));
            }
            case UiPadTable.BtnTab:
            {
                int target = UiPadTable.TabIndex(tabName);
                if (target < 0)
                    return UiPadPlan.No(button, None("unknown_tab"));
                return TabGuard(f, target) is { } refused ? UiPadPlan.No(button, refused)
                    : UiPadPlan.Ok(button, "tab " + tabName, new UiPadStep(UiPadStepKind.ChangeTab, button, Tab: target));
            }
            case UiPadTable.BtnLB:
            case UiPadTable.BtnRB:
                // Forwarded to the page's own shoulder handler (GameMenu.cs:262-264); read pages only.
                return page is "InventoryPage" or "SkillsPage" or "SocialPage" or "CollectionsPage" or "AnimalPage" or "PowersTab"
                    ? UiPadPlan.Ok(button, "page shoulder", new UiPadStep(UiPadStepKind.GamePadButton, button))
                    : UiPadPlan.No(button, None("shoulder_page"));
            case UiPadTable.BtnSelect:
                // InventoryPage Back = organize the player inventory (InventoryPage.cs:605-612): organize goal only (P2).
                return UiPadPlan.No(button, page == "InventoryPage" ? Deny("organize_goal_only") : None("select"));
            case UiPadTable.BtnY:
                // Y maps to the menu key (Utility.cs:7036): it would close the menu. "-" cell.
                return UiPadPlan.No(button, None("y_in_menu"));
            case UiPadTable.BtnA:
            case UiPadTable.BtnX:
                break;
        }
        // A / X: page-specific allowlists (read pages refuse; crafting is WS-G2).
        switch (page)
        {
            case "InventoryPage":
                return InventoryClick(button, f);
            case "CraftingPage":
                // WS-G2: the crafting rows (EconomyMenusEnabled); flag off = the G1 refusal.
                return f.Economy is { Kind: EconomyFacts.KindCrafting } e
                    ? EconomyPlans.Crafting(button, f, e, x, standalone: false)
                    : UiPadPlan.No(button, Deny("crafting_g2"));
            case "MapPage":
                return UiPadPlan.No(button, Deny("map_tab"));
            case "ExitPage":
                return UiPadPlan.No(button, Deny("exit_tab"));
            default:
                return UiPadPlan.No(button, Deny("read_only_page"));
        }
    }

    static bool IsEquipmentId(int id) => id is 101 or 102 or 103 or 104 or 108 or 109 or 110 || id is >= 120 and < 130;

    static UiPadPlan InventoryClick(string button, UiPadFacts f)
    {
        if (f.Snapped is not { } c)
            return UiPadPlan.No(button, Deny("nothing_snapped"));
        int id = c.Id;
        if (id == UiPadTable.InventoryTrash)
            return UiPadPlan.No(button, Deny("trash"));
        if (id == UiPadTable.DropButton)
            return UiPadPlan.No(button, Deny("drop_button"));
        if (id == UiPadTable.InventoryOrganize)
            return UiPadPlan.No(button, Deny("organize_goal_only"));
        if (id is >= UiPadTable.TabIdBase and <= UiPadTable.TabIdBase + UiPadTable.LastTab)
            // A tab click with a held item falls through to the page outside its bounds and drops the item
            // (GameMenu.cs:362-372, InventoryPage.cs:577-586): tabs only via the "tab" button.
            return UiPadPlan.No(button, Deny("tab_component_use_tab"));
        bool slot = c.Kind == UiPadComponent.KindSlot && id >= 0 && id < Math.Max(f.InventorySlots, 0);
        if (button == UiPadTable.BtnX)
            return slot ? MirrorClick(button, "take one / split the snapped stack") : UiPadPlan.No(button, Deny("component_not_allowlisted"));
        if (slot)
            return MirrorClick(button, f.HeldItemId is null ? "pick up the snapped stack" : "place / swap the held item");
        if (IsEquipmentId(id) && c.Kind == UiPadComponent.KindEquipment)
            return MirrorClick(button, "equip / unequip the snapped slot");
        // WS-G6 (WorldExtrasEnabled): the Junimo note icon opens the bundle menu (InventoryPage.cs:595-601).
        if (id == UiPadExtrasTable.JunimoNoteIconId && f.Extras is not null && button == UiPadTable.BtnA)
            return f.HeldItemId is null ? MirrorClick(button, "open the Junimo bundles") : UiPadPlan.No(button, Deny("item_held"));
        return UiPadPlan.No(button, Deny("component_not_allowlisted"));
    }

    static UiPadPlan Letter(string button, UiPadFacts f)
    {
        switch (button)
        {
            case UiPadTable.BtnRT:
                if (f.UiKey is not { Kind: UiKeyTable.Letter, Ready: true })
                    return UiPadPlan.No(button, Deny("letter_not_ready"));
                return f.Comps.Any(c => c.Id == 102 && c.Visible)
                    ? UiPadPlan.Ok(button, "next text page", new UiPadStep(UiPadStepKind.GamePadButton, button))
                    : UiPadPlan.No(button, None("letter_no_next_page"));
            case UiPadTable.BtnB:
                return ViaUiKey(button, f.UiKey, UiKeyTable.Letter, UiKeyTable.Close, "close the letter");
            case UiPadTable.BtnA:
                if (f.UiKey is { Ready: false } notReady)
                    return UiPadPlan.No(button, Deny("not_ready:" + (notReady.NotReadyReason ?? "unknown")));
                if (f.Snapped is not { } c)
                    return UiPadPlan.No(button, Deny("nothing_snapped"));
                return c.Id switch
                {
                    101 => MirrorClick(button, "previous page"),
                    102 => MirrorClick(button, "next page"),
                    104 => MirrorClick(button, "take the attachment"),
                    103 => QuestRules.AcceptLetter(button, f), // W1 gap B4: only the exact presented delivery quest
                    _ => UiPadPlan.No(button, Deny("component_not_allowlisted")),
                };
            default:
                return UiPadPlan.No(button, None("letter"));
        }
    }

    static UiPadPlan Board(string button, UiPadFacts f, int[] acceptIds, string does)
    {
        switch (button)
        {
            case UiPadTable.BtnB:
                return Close(button, f, "close the board");
            case UiPadTable.BtnA:
                if (f.Snapped is not { } c)
                    return UiPadPlan.No(button, Deny("nothing_snapped"));
                return acceptIds.Contains(c.Id) && c.Visible ? MirrorClick(button, does)
                    : UiPadPlan.No(button, Deny("read_only_component"));
            default:
                return UiPadPlan.No(button, None("board"));
        }
    }

    static UiPadPlan QuestLog(string button, UiPadFacts f)
    {
        switch (button)
        {
            case UiPadTable.BtnB:
                return Close(button, f, "close the journal");
            case UiPadTable.BtnA:
                if (f.Snapped is not { } c)
                    return UiPadPlan.No(button, Deny("nothing_snapped"));
                return c.Id switch
                {
                    >= 0 and <= 5 => MirrorClick(button, "open the snapped quest"),
                    101 => MirrorClick(button, "next page"),
                    102 => MirrorClick(button, "previous page"),
                    103 => UiPadPlan.No(button, Deny("reward_not_admitted")),
                    104 => UiPadPlan.No(button, Deny("cancel_quest")),
                    _ => UiPadPlan.No(button, Deny("component_not_allowlisted")),
                };
            default:
                return UiPadPlan.No(button, None("quest_log"));
        }
    }

    static UiPadPlan Question(string button, UiPadFacts f, string kind)
    {
        // Unknown native questions use the already observed, snapped response component.
        // A single click avoids double-dispatch after the callback replaces the menu. Never guess/cancel
        // by choosing the last response. Timers and the complete response grid remain native gates.
        if (kind == UiKeyTable.Question && f.UiKey is { Ready: true } generic
            && !UiKeyTable.IsAllowedQuestionKey(generic.QuestionKey))
        {
            if (button != UiPadTable.BtnA)
                return UiPadPlan.No(button, None("generic_question_choose_or_wait"));
            if (f.SnappedId is not int selected || selected < 0 || selected >= generic.OptionCount
                || f.Snapped is not { Visible: true } component || component.Id != selected
                || generic.Options is null || generic.Options.Count != generic.OptionCount)
                return UiPadPlan.No(button, Deny("unverifiable_response"));
            return UiPadPlan.Ok(button, "answer the observed snapped response",
                new UiPadStep(UiPadStepKind.LeftClick, button));
        }
        switch (button)
        {
            case UiPadTable.BtnA:
            {
                UiPadPlan plan = ViaUiKey(button, f.UiKey, kind, UiKeyTable.Answer, "answer the snapped response");
                if (!plan.Allowed)
                    return plan;
                // Answers bind to the SNAPPED response (DialogueBox.update auto-snaps response 0 in gamepad mode,
                // DialogueBox.cs:752-767), never to a hover-set selectedResponse.
                if (f.SnappedId is not int id || id < 0 || id >= (f.UiKey?.OptionCount ?? 0))
                    return UiPadPlan.No(button, Deny("nothing_snapped"));
                return plan;
            }
            case UiPadTable.BtnB:
                return kind == UiKeyTable.Question
                    ? ViaUiKey(button, f.UiKey, kind, UiKeyTable.Cancel, "cancel (last response)")
                    : UiPadPlan.No(button, None("npc_question_b"));
            default:
                return UiPadPlan.No(button, None("question"));
        }
    }

    /// <summary>The ui_nav plan for <paramref name="dir"/>: refusal (zero calls) or null = allowed.</summary>
    public static string? NavRefusal(UiPadFacts f, string dir)
    {
        int d = UiPadTable.DirectionIndex(dir);
        if (d < 0)
            return None("unknown_dir");
        string context = ContextOf(f);
        if (CommonGate(f, context) is { } gate)
            return gate;
        if (context == UiPadTable.CtxEvent)
            return EventNavRefusal(f, d);
        if (f.MenuType == "PurchaseAnimalsMenu" && f.PurchaseAnimals is not { Phase: "species", Ready: true })
            return Deny("animal_navigation_phase");
        if (f.GenericMenu) return None("use_observed_component_id");
        if (!NavContext(context))
            return None("nav_context");
        if (f.SnappedId is null)
            return null; // first step snaps to the menu default (post-checked against the denylist)
        UiPadNavPrediction p = UiPadNav.Predict(f, d);
        IReadOnlyCollection<int> deny = DenylistedIds(context, f.PageType);
        if (p.Predictable && p.TargetId is int target && deny.Contains(target))
            return Deny("denylisted_target:" + target);
        return null;
    }

    // FIXB49: event is NOT a generic nav context. Only an exact, ready, allowlisted question's response grid
    // can be snapped; movement never reaches the world/event motor or a script command.
    static string? EventNavRefusal(UiPadFacts f, int dir)
    {
        if (f.MinigameActive || f.TextSubscriber)
            return Deny("event_nav_input_busy");
        if (f.MenuType != "DialogueBox" || f.G3?.Event?.Dialogue is not { IsQuestion: true } question)
            return Deny("event_nav_needs_question");
        if (!question.Ready || question.Transitioning)
            return Deny("event_nav_not_ready");
        // NPC chooseResponse has no native chosen-index readback here. Its dialogue transition alone
        // cannot prove the chosen response, so FIXB49 nav is confined to the event-script key path.
        if (question.NpcQuestion)
            return Deny("event_nav_needs_script_question");
        UiG3QuestionClass cls = UiG3Policy.EventQuestion(question);
        if (!cls.Allowed)
            return Deny("event_question_refused:" + cls.Class);
        // Native DialogueBox.update creates one responseCC per response, IDs 0..N-1 and cardinal neighbours
        // only. Missing/foreign/duplicate components or custom snaps cannot authorize a call.
        if (f.Comps.Count != question.Choices.Count || Enumerable.Range(0, question.Choices.Count).Any(i =>
                f.Comps.Count(c => c.Id == i && c.Kind == UiPadComponent.KindResponse && c.Visible) != 1))
            return Deny("event_response_grid_unavailable");
        if (f.SnappedId is null)
            return null;
        if (f.Snapped is not { } from)
            return Deny("event_response_snap_unavailable");
        int neighbour = UiPadNav.Neighbour(from, dir);
        return neighbour == -1 || f.Comps.Any(c => c.Id == neighbour)
            ? null : Deny("event_response_neighbour_unsupported");
    }

    /// <summary>The whole button row (ui_inspect "buttons": what each button does here, or the refusal).</summary>
    public static Dictionary<string, object> Row(UiPadFacts f)
    {
        var row = new Dictionary<string, object>(StringComparer.Ordinal);
        foreach (string b in UiPadTable.PressButtons)
        {
            if (b == UiPadTable.BtnTab)
                continue;
            row[b] = Translate(b, f).Wire();
        }
        if (ContextOf(f) == UiPadTable.CtxGameMenu)
        {
            var tabs = new Dictionary<string, object>(StringComparer.Ordinal);
            foreach (string name in UiPadTable.TabNames)
            {
                UiPadPlan plan = Translate(UiPadTable.BtnTab, f, name);
                if (plan.Allowed)
                    tabs[name] = "changeTab";
            }
            row[UiPadTable.BtnTab] = new Dictionary<string, object> { ["tabs"] = tabs.Keys.ToArray() };
        }
        string? nav = null;
        foreach (string dir in UiPadTable.Directions)
        {
            string? why = NavRefusal(f, dir);
            if (why is null)
            {
                nav = null;
                break;
            }
            nav ??= why;
        }
        row["stick"] = nav is null ? new Dictionary<string, object> { ["does"] = "ui_nav (snap step)" }
            : new Dictionary<string, object> { ["refused"] = nav };
        return row;
    }
}

public readonly record struct UiPadNavPrediction(bool Predictable, int? TargetId);

/// <summary>Read-only prediction of the snap target (IClickableMenu.moveCursorInDirection, :345-500).</summary>
public static class UiPadNav
{
    public const int AutoSnap = -99998, CustomSnap = -7777, DefaultSnap = -99999;

    public static int Neighbour(UiPadComponent c, int dir) => dir switch
    {
        0 => c.Up,
        1 => c.Right,
        2 => c.Down,
        _ => c.Left,
    };

    public static UiPadNavPrediction Predict(UiPadFacts f, int dir)
    {
        if (f.Snapped is not { } from)
            return new UiPadNavPrediction(false, null);
        int n = Neighbour(from, dir);
        if (n == CustomSnap || n == DefaultSnap)
            return new UiPadNavPrediction(false, null);
        if (n == AutoSnap)
            return new UiPadNavPrediction(true, AutomaticSnap(f.Comps, from, dir)?.Id);
        if (n == -500)
            return new UiPadNavPrediction(true, null);
        UiPadComponent? target = f.Comps.FirstOrDefault(c => c.Id == n && c.Visible);
        return new UiPadNavPrediction(true, target?.Id);
    }

    /// <summary>Replica of IClickableMenu.automaticSnapBehavior (base rules; page overrides are post-checked).</summary>
    public static UiPadComponent? AutomaticSnap(IReadOnlyList<UiPadComponent> comps, UiPadComponent from, int dir)
    {
        (float vx, float vy) = dir switch { 3 => (-1f, 0f), 1 => (1f, 0f), 0 => (0f, -1f), _ => (0f, 1f) };
        float best = -1f;
        UiPadComponent? pick = null;
        foreach (UiPadComponent c in comps)
        {
            if ((c.Left == -1 && c.Right == -1 && c.Up == -1 && c.Down == -1) || c.Id == -500 || !c.Visible
                || c.Kind == UiPadComponent.KindClose || ReferenceEquals(c, from) || c == from)
                continue;
            float dx = c.CenterX - from.CenterX, dy = c.CenterY - from.CenterY;
            float len = MathF.Sqrt(dx * dx + dy * dy);
            if (len <= 0f)
                continue;
            float dot = vx * (dx / len) + vy * (dy / len);
            if (!(dot > 0.01f))
                continue;
            float dist = dx * dx + dy * dy;
            bool aligned = dir is 0 or 2 ? MathF.Abs(dx) < 32f : MathF.Abs(dy) < 32f;
            if (dot > 0.99999f || aligned)
                dist *= 0.01f;
            if (best == -1f || dist < best)
            {
                best = dist;
                pick = c;
            }
        }
        return pick;
    }
}

/// <summary>Binding tokens: "up:" + sha256(receiver|menu|context|tab|snapped|state digest)[..32].</summary>
public static class UiPadToken
{
    public static string StateDigest(UiPadFacts f)
    {
        StringBuilder text = new();
        text.Append(f.MenuType).Append('|').Append(f.PageType).Append('|').Append(f.Tab).Append('|')
            .Append(f.SnappedId?.ToString() ?? "-").Append('|').Append(f.HeldItemId).Append('x').Append(f.HeldStack).Append('|')
            .Append(f.PageIndex).Append('|').Append(UiKeyTargetToken.Hash(f.Text));
        if (f.G3 is { Enabled: true } g3)
            text.Append('|').Append(g3.Digest()); // WS-G3 only (flag off = the G1 digest unchanged)
        if (f.PurchaseAnimals is { } purchase) text.Append("|animal:").Append(purchase.Digest());
        if (f.CharCustom is { } charCustom)
            text.Append("|cc:").Append(charCustom.Digest()); // Live7: every control value changes the token
        if (f.Economy is { } economy)
            text.Append("|econ:").Append(economy.Digest()); // WS-G2: money / stock / craftable change the token
        if (f.Extras?.BundlePage is { } bundle)
            text.Append("|bundle:").Append(JsonSerializer.Serialize(bundle.Snapshot()));
        if (f.Extras?.LetterQuest is { } letterQuest)
            text.Append("|lq:").Append(letterQuest.Digest()); // W1 gap B4: an accepted letter quest is a state change
        text.Append("|generic:").Append(f.GenericMenu).Append('|').Append(f.GenericLedger);
        foreach (UiPadComponent c in f.Comps)
        {
            text.Append('\u001f').Append(c.Id).Append(',').Append(c.Kind).Append(',').Append(c.Label).Append(',')
                .Append(c.ItemId).Append(',').Append(c.Stack).Append(',').Append(c.Quality).Append(',').Append(c.Visible ? 1 : 0);
            if (f.GenericMenu)
                text.Append(',').Append(c.X).Append(',').Append(c.Y).Append(',').Append(c.W).Append(',').Append(c.H);
            if (f.Extras?.BundlePage is not null || f.GenericMenu)
                text.Append(',').Append(c.Up).Append(',').Append(c.Right).Append(',').Append(c.Down).Append(',').Append(c.Left);
        }
        return UiKeyTargetToken.Hash(text.ToString())[..32];
    }

    public static string Compute(string receiverToken, string menuType, string context, UiPadFacts f)
    {
        string text = string.Join("|", receiverToken, menuType, context, f.Tab, f.SnappedId?.ToString() ?? "-", StateDigest(f));
        return "up:" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text))).ToLowerInvariant()[..32];
    }

    public static bool IsShape(string? token) =>
        token is { Length: 35 } && token.StartsWith("up:", StringComparison.Ordinal)
        && token[3..].All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f');
}

/// <summary>Common ui_nav / ui_press binding args (copied from the published ui_pad block).</summary>
public static class UiPadArgs
{
    public static readonly string[] Base = { "receiver_token", "menu_type", "context", "target_token" };

    internal static bool ValidBase(string receiver, string menuType, string context, string target) =>
        new[] { receiver, menuType, context, target }.All(s => !string.IsNullOrWhiteSpace(s) && s.Length <= UiPadTable.MaxText)
        && receiver.StartsWith("rcv:", StringComparison.Ordinal)
        && UiPadToken.IsShape(target)
        && UiPadTable.Contexts.Contains(context, StringComparer.Ordinal);

    internal static bool TryProps(JsonElement args, out List<JsonProperty> props)
    {
        props = new List<JsonProperty>();
        if (args.ValueKind != JsonValueKind.Object)
            return false;
        props = args.EnumerateObject().ToList();
        return props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() == props.Count;
    }

    internal static string? Str(JsonElement args, string key) =>
        args.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
}

public sealed record UiNavTarget(string ReceiverToken, string MenuType, string Context, string TargetToken, string Dir)
{
    public const string Primitive = UiPadTable.PrimitiveNav;

    public bool IsValid => UiPadArgs.ValidBase(this.ReceiverToken, this.MenuType, this.Context, this.TargetToken)
        && UiPadTable.DirectionIndex(this.Dir) >= 0;

    public Dictionary<string, object> Args() => new(StringComparer.Ordinal)
    {
        ["receiver_token"] = this.ReceiverToken, ["menu_type"] = this.MenuType, ["context"] = this.Context,
        ["target_token"] = this.TargetToken, ["dir"] = this.Dir,
    };

    public static bool TryParse(JsonElement args, out UiNavTarget? target)
    {
        target = null;
        if (!UiPadArgs.TryProps(args, out var props))
            return false;
        string[] keys = UiPadArgs.Base.Append("dir").ToArray();
        if (props.Count != keys.Length || props.Any(p => !keys.Contains(p.Name) || p.Value.ValueKind != JsonValueKind.String))
            return false;
        target = new UiNavTarget(UiPadArgs.Str(args, "receiver_token")!, UiPadArgs.Str(args, "menu_type")!,
            UiPadArgs.Str(args, "context")!, UiPadArgs.Str(args, "target_token")!, UiPadArgs.Str(args, "dir")!);
        return target.IsValid;
    }
}

public sealed record UiPressTarget(string ReceiverToken, string MenuType, string Context, string TargetToken, string Button,
    int SnappedId, string? Tab = null, IReadOnlyDictionary<string, string>? Reserved = null,
    IReadOnlyDictionary<string, object>? ReservedTyped = null)
{
    /// <summary>WS-G2 typed payload (qty / half / stack; quick / text / value kept as Reserved for the refusal).</summary>
    public UiPressExtras Extras => UiPressExtras.From(this.ReservedTyped);

    public const string Primitive = UiPadTable.PrimitivePress;

    /// <summary>Canonical reserved payload ("k=v;..." sorted; "" = none) for the journal digest.</summary>
    public string ReservedCanonical => this.Reserved is { Count: > 0 } r
        ? string.Join(";", r.OrderBy(kv => kv.Key, StringComparer.Ordinal).Select(kv => kv.Key + "=" + kv.Value)) : "";

    public bool IsValid => UiPadArgs.ValidBase(this.ReceiverToken, this.MenuType, this.Context, this.TargetToken)
        && UiPadTable.PressButtons.Contains(this.Button, StringComparer.Ordinal)
        && this.SnappedId is >= -1 and <= 1_000_000
        && (this.Button == UiPadTable.BtnTab ? UiPadTable.TabIndex(this.Tab) >= 0 : this.Tab is null)
        && (this.Reserved is null || this.Reserved.Keys.All(k => UiPadTable.ReservedArgs.Contains(k, StringComparer.Ordinal)));

    public Dictionary<string, object> Args()
    {
        var args = new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["receiver_token"] = this.ReceiverToken, ["menu_type"] = this.MenuType, ["context"] = this.Context,
            ["target_token"] = this.TargetToken, ["btn"] = this.Button, ["snapped_id"] = this.SnappedId,
        };
        if (this.Tab is { } tab)
            args["tab"] = tab;
        // Echo the payload with its JSON type (the runtime compares input_args to what it sent).
        if (this.ReservedTyped is { } typed)
            foreach (var kv in typed)
                args[kv.Key] = kv.Value;
        else if (this.Reserved is { } reserved)
            foreach (var kv in reserved)
                args[kv.Key] = kv.Value;
        return args;
    }

    public static bool TryParse(JsonElement args, out UiPressTarget? target)
    {
        target = null;
        if (!UiPadArgs.TryProps(args, out var props))
            return false;
        string[] required = UiPadArgs.Base.Concat(new[] { "btn", "snapped_id" }).ToArray();
        if (required.Any(k => !args.TryGetProperty(k, out _)))
            return false;
        string? tab = null;
        Dictionary<string, string>? reserved = null;
        Dictionary<string, object>? typed = null;
        foreach (JsonProperty p in props)
        {
            if (required.Contains(p.Name))
            {
                if (p.Name == "snapped_id" ? p.Value.ValueKind != JsonValueKind.Number : p.Value.ValueKind != JsonValueKind.String)
                    return false;
                continue;
            }
            if (p.Name == "tab")
            {
                if (p.Value.ValueKind != JsonValueKind.String)
                    return false;
                tab = p.Value.GetString();
                continue;
            }
            if (!UiPadTable.ReservedArgs.Contains(p.Name, StringComparer.Ordinal))
                return false;
            // Reserved for G2 / G3: kept (so the refusal names it) as a bounded canonical scalar.
            string? value = p.Value.ValueKind switch
            {
                JsonValueKind.Number when p.Value.TryGetInt32(out int n) => n.ToString(System.Globalization.CultureInfo.InvariantCulture),
                JsonValueKind.True => "true",
                JsonValueKind.False => "false",
                JsonValueKind.String => p.Value.GetString(),
                _ => null,
            };
            if (value is null || value.Length > UiPadTable.MaxText)
                return false;
            (reserved ??= new Dictionary<string, string>(StringComparer.Ordinal))[p.Name] = value;
            (typed ??= new Dictionary<string, object>(StringComparer.Ordinal))[p.Name] = p.Value.ValueKind switch
            {
                JsonValueKind.Number => p.Value.GetInt32(),
                JsonValueKind.True => true,
                JsonValueKind.False => false,
                _ => value,
            };
        }
        if (!args.GetProperty("snapped_id").TryGetInt32(out int snapped))
            return false;
        target = new UiPressTarget(UiPadArgs.Str(args, "receiver_token")!, UiPadArgs.Str(args, "menu_type")!,
            UiPadArgs.Str(args, "context")!, UiPadArgs.Str(args, "target_token")!, UiPadArgs.Str(args, "btn")!, snapped, tab, reserved,
            typed);
        return target.IsValid;
    }
}

/// <summary>The game side the executor drives (ModEntry.Ui.cs over the real menu; a recording fake in tests).</summary>
public interface IUiPadPort
{
    /// <summary>Fresh facts of the current deepest menu (snap owner = GameMenu page).</summary>
    UiPadFacts Read();

    /// <summary>snapToDefaultClickableComponent on the snap owner (populating its list first, as applyMovementKey does).</summary>
    void SnapToDefault();

    /// <summary>applyMovementKey(dir) on the snap owner.</summary>
    void ApplyMovementKey(int dir);

    /// <summary>setCurrentlySnappedComponentTo(id) + snapCursorToCurrentSnappedComponent (OS warp guarded).</summary>
    void SetSnapped(int id);

    void GamePadButton(string button);

    void LeftClick(int x, int y);

    void RightClick(int x, int y);

    /// <summary>Class of mapGamePadButtonToKey(button): none | menu | move | journal | slot | other.</summary>
    string KeyClass(string button);

    void KeyPress(string button);

    void ChangeTab(int tab);

    /// <summary>The deepest menu still IsActive() (vanilla stops dispatching once it is not).</summary>
    bool StillActive();

    /// <summary>OS-cursor guard counter: SetMousePosition calls that went through while inactive.</summary>
    long UnblockedWarps { get; }

    /// <summary>WS-G6 museum: one receiveLeftClick at the free suitable museum tile the mod picks (fails if none).</summary>
    void MuseumPlace() => throw new NotSupportedException("museum place not supported by this port");
}

/// <summary>WS-G2 additions over the real menu (ModEntry.Economy.cs) or a recording fake (tests).</summary>
public interface IEconomyPort : IUiPadPort
{
    /// <summary>Game1.oldKBState = exactly <paramref name="mods"/> for the duration of <paramref name="call"/> (restored in finally).</summary>
    void WithModifiers(KbModifiers mods, Action call);

    /// <summary>Money / item count (incl. the menu's held item) / row stock / toolBeingUpgraded for the judge.</summary>
    EconomyLedger ReadLedger(EconomyExpect expect);

    /// <summary>The private ShopMenu.tryToPurchaseItem for forSale[rowIndex] with stockToBuy = qty + the vanilla loop tail.</summary>
    void ShopBuyDirect(int rowIndex, int qty, string itemId);

    /// <summary>The SnappyMenus tail (ShopMenu.cs:1132) / the CraftingPage gamepad tail (CraftingPage.cs:499-503): heldItem
    /// into the inventory when it fits.</summary>
    void ShopStoreHeld();
}

public sealed record UiPadVerdict(string Outcome, bool Applied, string? Effect, IReadOnlyList<string> Calls, string Reason,
    int? SnappedAfter, string? ResultMenuType, string? ResultReceiverToken, string? Readback = null, EconomyReport? Economy = null)
{
    public const string NavApplied = "UiNavApplied", NavNoEffect = "UiNavNoEffect", NavUncertain = "UiNavUncertain";
    public const string PressApplied = "UiPressApplied", PressNoEffect = "UiPressNoEffect", PressUncertain = "UiPressUncertain";

    public bool Uncertain => this.Outcome is NavUncertain or PressUncertain;
    public bool NoEffect => this.Outcome is NavNoEffect or PressNoEffect;
}

/// <summary>Pure ui_nav / ui_press executor over <see cref="IUiPadPort"/> (mirror plans; ui_key plans settle in ModEntry).</summary>
public static class UiPadExecutor
{
    /// <summary>Press effect from the before / after facts: closed, replaced, tab, changed (null = none).</summary>
    public static string? PressEffect(UiPadFacts before, UiPadFacts after)
    {
        if (!after.MenuOpen && before.MenuOpen)
            return "closed";
        if (after.MenuType != before.MenuType)
            return "replaced";
        if (before.MenuType == "GameMenu" && after.Tab != before.Tab)
            return "tab";
        if (after.ReceiverToken != before.ReceiverToken)
            return "replaced";
        return UiPadToken.StateDigest(after) != UiPadToken.StateDigest(before) ? "changed" : null;
    }

    public static UiPadVerdict Nav(IUiPadPort port, UiPadFacts before, int dir)
    {
        var calls = new List<string>();
        long warps = port.UnblockedWarps;
        UiPadFacts after;
        string context = UiPadContextTable.ContextOf(before);
        if (context == UiPadTable.CtxEvent && UiPadContextTable.NavRefusal(before,
                dir is >= 0 and < 4 ? UiPadTable.Directions[dir] : "") is { } refusal)
            return Finish(UiPadVerdict.NavNoEffect, false, null, calls, refusal, before, warps, port);
        try
        {
            if (before.SnappedId is null)
            {
                calls.Add("snapToDefaultClickableComponent");
                port.SnapToDefault();
                if (port.Read().SnappedId is null)
                {
                    calls.Add("applyMovementKey(" + dir + ")");
                    port.ApplyMovementKey(dir);
                }
            }
            else
            {
                calls.Add("applyMovementKey(" + dir + ")");
                port.ApplyMovementKey(dir);
            }
            after = port.Read();
            if (context == UiPadTable.CtxEvent && (after.Snapped is not { Kind: UiPadComponent.KindResponse }
                    || UiPadContextTable.NavRefusal(after, UiPadTable.Directions[dir]) is not null
                    || UiPadToken.StateDigest(before with { SnappedId = after.SnappedId }) != UiPadToken.StateDigest(after)))
                return Finish(UiPadVerdict.NavUncertain, false, null, calls, "event_question_changed", after, warps, port);
            IReadOnlyCollection<int> deny = UiPadContextTable.DenylistedIds(context, before.PageType);
            if (after.SnappedId is int landed && deny.Contains(landed))
            {
                // An unpredictable neighbour (-7777 / -99999 / page override) landed on a denylisted id: undo the snap.
                if (before.SnappedId is int old)
                {
                    calls.Add("setCurrentlySnappedComponentTo(" + old + ")");
                    port.SetSnapped(old);
                    after = port.Read();
                    return Finish(UiPadVerdict.NavNoEffect, false, null, calls, "denylisted_landing_reverted:" + landed, after, warps, port);
                }
                return Finish(UiPadVerdict.NavUncertain, false, null, calls, "denylisted_landing:" + landed, after, warps, port);
            }
        }
        catch (Exception ex)
        {
            UiPadFacts? now = null;
            try { now = port.Read(); } catch { }
            return Finish(UiPadVerdict.NavUncertain, false, null, calls, "call_fault:" + ex.GetType().Name, now, warps, port);
        }
        if (!after.MenuOpen || after.MenuType != before.MenuType || after.ReceiverToken != before.ReceiverToken)
            return Finish(UiPadVerdict.NavUncertain, false, null, calls, "receiver_changed", after, warps, port);
        if (after.SnappedId != before.SnappedId)
            return Finish(UiPadVerdict.NavApplied, true, "snapped", calls, "snapped:" + (after.SnappedId?.ToString() ?? "-"), after, warps, port);
        if (after.PageIndex != before.PageIndex)
            return Finish(UiPadVerdict.NavApplied, true, "scrolled", calls, "page_index:" + after.PageIndex, after, warps, port);
        return Finish(UiPadVerdict.NavNoEffect, false, null, calls, "edge_or_no_neighbour", after, warps, port);
    }

    /// <summary>Runs a mirror plan (never a ui_key plan) in the vanilla order and reads the effect back.</summary>
    public static UiPadVerdict Press(IUiPadPort port, UiPadFacts before, UiPadPlan plan)
    {
        if (!plan.Allowed || plan.IsUiKey)
            throw new InvalidOperationException("ui_press executor needs an allowed mirror plan");
        var calls = new List<string>();
        long warps = port.UnblockedWarps;
        UiPadComponent? snapped = before.Snapped;
        IEconomyPort? economy = port as IEconomyPort;
        if (plan.IsEconomy && economy is null)
            return Finish(UiPadVerdict.PressUncertain, false, null, calls, "economy_port_missing", null, warps, port);
        EconomyLedger? ledgerBefore = null;
        if (plan.Expect is { } expectBefore)
        {
            try { ledgerBefore = economy!.ReadLedger(expectBefore); }
            catch (Exception ex)
            {
                // Nothing was called yet: a ledger that cannot be read refuses the press as no effect.
                return Finish(UiPadVerdict.PressNoEffect, false, null, calls, "ledger_unreadable:" + ex.GetType().Name, null, warps, port);
            }
        }
        try
        {
            for (int i = 0; i < plan.Steps.Count; i++)
            {
                UiPadStep step = plan.Steps[i];
                if (i > 0 && !port.StillActive())
                {
                    calls.Add("stop:menu_inactive");
                    break;
                }
                switch (step.Kind)
                {
                    case UiPadStepKind.GenericInput:
                        if (port is not IGenericUiPort genericPort)
                            throw new InvalidOperationException("generic UI port unavailable");
                        calls.Add("generic_ui:" + step.G3Action);
                        genericPort.ApplyGeneric(step.G3Action!, step.RowIndex, step.G3Arg);
                        break;
                    case UiPadStepKind.GamePadButton:
                        calls.Add(step.ToString());
                        port.GamePadButton(step.Button);
                        break;
                    case UiPadStepKind.LeftClick:
                    case UiPadStepKind.RightClick:
                        if (snapped is null)
                            throw new InvalidOperationException("click without a snapped component");
                        calls.Add(step.ToString());
                        UiPadComponent at = snapped;
                        Action click = step.Kind == UiPadStepKind.LeftClick
                            ? () => port.LeftClick(at.CenterX, at.CenterY)
                            : () => port.RightClick(at.CenterX, at.CenterY);
                        if (plan.Modifiers is { } mods)
                        {
                            // One synchronous block: oldKBState = exactly these modifiers, restored in finally (PRB-5).
                            calls.Add("oldKBState(" + KbOverride.Describe(mods) + ")");
                            economy!.WithModifiers(mods, click);
                        }
                        else
                            click();
                        break;
                    case UiPadStepKind.KeyPress:
                    {
                        string cls = port.KeyClass(step.Button);
                        bool clickButton = step.Button is UiPadTable.BtnA or UiPadTable.BtnX;
                        // A / X: the mapped action / use-tool key is inert in these menus under the default
                        // bindings; a key bound to a menu / move / slot / journal meaning would add a SECOND effect
                        // (e.g. close) -> skipped and recorded. B / Start: the menu key IS the close.
                        if (cls == "none" || (clickButton && cls != "other"))
                        {
                            calls.Add("skip:receiveKeyPress(" + step.Button + "):" + cls);
                            break;
                        }
                        calls.Add(step.ToString());
                        port.KeyPress(step.Button);
                        break;
                    }
                    case UiPadStepKind.ChangeTab:
                        calls.Add(step.ToString());
                        port.ChangeTab(step.Tab);
                        break;
                    case UiPadStepKind.ShopBuyDirect:
                    {
                        // Review fix: the reflected purchase binds to forSale[row]; re-read it right before the call and
                        // refuse (zero calls) when that row no longer holds the expected item (scroll / sold out / rebuilt).
                        string want = plan.Expect?.ItemId ?? "";
                        UiPadFacts now = port.Read();
                        if (now.Economy?.RowList.FirstOrDefault(r => r.Index == step.RowIndex)?.ItemId != want)
                            return Finish(UiPadVerdict.PressNoEffect, false, null, calls, "shop_row_changed", now, warps, port);
                        calls.Add(step.ToString());
                        economy!.ShopBuyDirect(step.RowIndex, step.Qty, want);
                        break;
                    }
                    case UiPadStepKind.ShopStoreHeld:
                        calls.Add(step.ToString());
                        economy!.ShopStoreHeld();
                        break;
                    case UiPadStepKind.MuseumPlace:
                        calls.Add(step.ToString());
                        port.MuseumPlace();
                        break;
                    default:
                        throw new InvalidOperationException("ui_key step in a mirror plan");
                }
            }
        }
        catch (Exception ex)
        {
            UiPadFacts? now = null;
            try { now = port.Read(); } catch { }
            return Finish(UiPadVerdict.PressUncertain, false, null, calls, "call_fault:" + ex.GetType().Name, now, warps, port);
        }
        UiPadFacts after;
        try { after = port.Read(); }
        catch (Exception ex)
        {
            return Finish(UiPadVerdict.PressUncertain, false, null, calls, "readback_fault:" + ex.GetType().Name, null, warps, port);
        }
        if (plan.Expect is { } expect)
        {
            EconomyLedger ledgerAfter;
            try { ledgerAfter = economy!.ReadLedger(expect); }
            catch (Exception ex)
            {
                return Finish(UiPadVerdict.PressUncertain, false, null, calls, "ledger_readback_fault:" + ex.GetType().Name, after, warps, port);
            }
            (bool observed, bool none, string judged, string why) = EconomyJudge.Judge(expect, ledgerBefore!, ledgerAfter);
            string path = plan.Steps.Any(s => s.Kind == UiPadStepKind.ShopBuyDirect) ? "reflection" : "click";
            var report = new EconomyReport(expect.Kind, expect.ItemId, expect.Quantity, observed, judged, why, ledgerBefore!, ledgerAfter,
                plan.Modifiers is { } m ? KbOverride.Describe(m) : "none", path);
            UiPadVerdict judgedVerdict = observed
                ? Finish(UiPadVerdict.PressApplied, true, judged, calls, "economy:" + why, after, warps, port)
                : none
                    ? Finish(UiPadVerdict.PressNoEffect, false, null, calls, "economy_no_change", after, warps, port)
                    // Something changed, but not exactly what the press means: never claim it (latched uncertain).
                    : Finish(UiPadVerdict.PressUncertain, false, null, calls, "economy_mismatch:" + why, after, warps, port);
            return judgedVerdict with { Economy = report };
        }
        string? effect = PressEffect(before, after);
        if (before.GenericMenu)
        {
            string? itemOperation = plan.Steps.FirstOrDefault(s => s.Kind == UiPadStepKind.GenericInput).G3Action;
            if (CursorItemSettle.ItemOperation(itemOperation) is not null)
            {
                int id = plan.Steps.First(s => s.Kind == UiPadStepKind.GenericInput).RowIndex;
                var destination = before.CursorSettle?.Destinations.SingleOrDefault(d => d.Id == id);
                var itemVerdict = CursorItemSettle.Verify(before.CursorSettle, after.CursorSettle, destination);
                string itemReadback = JsonSerializer.Serialize(new { cursor_settle = itemVerdict,
                    before = before.GenericValues, after = after.GenericValues });
                return (itemVerdict.Status == "Uncertain"
                    ? Finish(UiPadVerdict.PressUncertain, false, null, calls, itemVerdict.Refusal ?? "item_readback_unavailable", after, warps, port)
                    : itemVerdict.Status == "NoEffect"
                    ? Finish(UiPadVerdict.PressNoEffect, false, null, calls, "cursor_no_effect", after, warps, port)
                    : Finish(UiPadVerdict.PressApplied, true, CursorItemSettle.Effect(itemVerdict.Status), calls,
                        "inventory_conserved", after, warps, port)) with { Readback = itemReadback };
            }
            effect = ReadyCheckObservation.Effect(before, after, effect, out bool readinessUnreadable);
            bool readinessLost = effect == "readiness_lost";
            string readback = JsonSerializer.Serialize(new { before = before.GenericValues, after = after.GenericValues,
                before_state = before.GenericLedger, after_state = after.GenericLedger,
                before_components = before.GenericState, after_components = after.GenericState,
                result_menu = after.MenuOpen ? after.MenuType : null, effect = effect ?? "no_effect", released = true });
            return (readinessUnreadable
                ? Finish(UiPadVerdict.PressUncertain, false, null, calls, "readiness_readback_unavailable", after, warps, port)
                : readinessLost
                ? Finish(UiPadVerdict.PressNoEffect, false, "readiness_lost", calls, "readiness_lost", after, warps, port)
                : effect is null
                ? Finish(UiPadVerdict.PressNoEffect, false, null, calls, "no_state_change", after, warps, port)
                : Finish(UiPadVerdict.PressApplied, true, effect, calls, "effect:" + effect, after, warps, port))
                with { Readback = readback };
        }
        if (before.Extras?.BundlePage is { } bundleBefore && snapped?.Kind == UiPadExtrasTable.KindIngredient
            && before.HeldItemId is not null && plan.Button == UiPadTable.BtnA)
        {
            bool donated = BundleDonation.Observed(before, after);
            return donated ? Finish(UiPadVerdict.PressApplied, true, "changed", calls, "bundle:donated", after, warps, port)
                : effect is null ? Finish(UiPadVerdict.PressNoEffect, false, null, calls, "no_state_change", after, warps, port)
                : Finish(UiPadVerdict.PressUncertain, false, null, calls, "bundle_readback_mismatch", after, warps, port);
        }
        return effect is null
            ? Finish(UiPadVerdict.PressNoEffect, false, null, calls, "no_state_change", after, warps, port)
            : Finish(UiPadVerdict.PressApplied, true, effect, calls, "effect:" + effect, after, warps, port);
    }

    static UiPadVerdict Finish(string outcome, bool applied, string? effect, List<string> calls, string reason, UiPadFacts? after,
        long warpsBefore, IUiPadPort port)
    {
        long warps;
        try { warps = port.UnblockedWarps; } catch { warps = warpsBefore + 1; }
        if (warps != warpsBefore)
        {
            // The OS cursor moved while inactive: never claim a clean effect (global stop rule, PLAN 6.2).
            outcome = outcome.StartsWith("UiNav", StringComparison.Ordinal) ? UiPadVerdict.NavUncertain : UiPadVerdict.PressUncertain;
            applied = false;
            reason = "os_warp_unblocked";
        }
        return new UiPadVerdict(outcome, applied, effect, calls.ToArray(), reason, after?.SnappedId,
            after is { MenuOpen: true } ? after.MenuType : null, after is { MenuOpen: true } ? after.ReceiverToken : null);
    }

    /// <summary>Maps a ui_key verdict (ui_press on a ui_key context) onto the ui_press vocabulary.</summary>
    public static UiPadVerdict FromUiKey(UiKeyVerdict v, string step)
    {
        string outcome = v.Outcome switch
        {
            UiKeyVerdict.OutcomeApplied => UiPadVerdict.PressApplied,
            UiKeyVerdict.OutcomeNoEffect => UiPadVerdict.PressNoEffect,
            _ => UiPadVerdict.PressUncertain,
        };
        return new UiPadVerdict(outcome, v.Applied, v.Effect, new[] { step }, "ui_key:" + v.Reason, null, v.ResultMenuType,
            v.ResultReceiverToken);
    }
}

/// <summary>ui_inspect publication (ui.context "ui_pad"): labels / values / neighbour ids; bounds for the runtime only.</summary>
public static class UiPadSnapshot
{
    /// <summary>A window of at most MaxComponents visible components around the snapped one (list order kept).</summary>
    public static IReadOnlyList<UiPadComponent> Window(UiPadFacts f)
    {
        if (f.GenericMenu) return f.Comps;
        List<UiPadComponent> visible = f.Comps.Where(c => c.Visible).ToList();
        if (visible.Count <= UiPadTable.MaxComponents)
            return visible;
        if (f.Economy is { Kind: EconomyFacts.KindCrafting } crafting)
        {
            // A bag-heavy window around 88 hid the recipe side of automatic left edges (W6 MS5).
            // Keep active recipe nodes/page controls before bag filler; the wire bound is unchanged.
            var ids = crafting.RecipeList.Select(r => r.ComponentId).ToHashSet();
            ids.UnionWith(new[] { 88, 89 });
            if (f.Snapped is { } snap)
            {
                ids.Add(snap.Id);
                ids.UnionWith(new[] { snap.Up, snap.Right, snap.Down, snap.Left });
            }
            var priority = visible.Where(c => ids.Contains(c.Id)).Take(UiPadTable.MaxComponents).ToList();
            priority.AddRange(visible.Where(c => !ids.Contains(c.Id)).Take(UiPadTable.MaxComponents - priority.Count));
            return visible.Where(c => priority.Contains(c)).ToList();
        }
        int at = f.SnappedId is int id ? Math.Max(0, visible.FindIndex(c => c.Id == id)) : 0;
        int start = Math.Clamp(at - UiPadTable.MaxComponents / 2, 0, visible.Count - UiPadTable.MaxComponents);
        return visible.GetRange(start, UiPadTable.MaxComponents);
    }

    static Dictionary<string, object> Component(UiPadComponent c)
    {
        var d = new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["id"] = c.Id, ["kind"] = c.Kind, ["label"] = UiPadTable.Trim(c.Label),
            ["nbr"] = new[] { c.Up, c.Right, c.Down, c.Left },
            // Runtime-only (token binding / denylist diagnostics); the Python view strips it before any prompt.
            ["bounds"] = new[] { c.X, c.Y, c.W, c.H },
        };
        if (c.HoverText is not null) d["hover_text"] = UiPadTable.Trim(c.HoverText, 512);
        if (c.FieldPath is not null)
        {
            d["field_path"] = UiPadTable.Trim(c.FieldPath, 256);
            if (c.AcceptsItem is { } accepted) d["accepts_item"] = accepted;
            if (c.ProtectedItem) d["protected_item"] = true;
            d["visible"] = c.Visible;
        }
        if (c.TextValue is not null) d["text_value"] = UiPadTable.Trim(c.TextValue, 128);
        if (c.ItemId is { } item)
            d["value"] = new Dictionary<string, object> { ["item"] = item, ["stack"] = c.Stack, ["quality"] = c.Quality };
        return d;
    }

    public static Dictionary<string, object> Build(UiPadFacts f, string receiverToken, bool admissible, string? notReady)
    {
        string context = UiPadContextTable.ContextOf(f);
        string token = UiPadToken.Compute(receiverToken, f.MenuType, context, f);
        IReadOnlyList<UiPadComponent> window = Window(f);
        string? wait = notReady ?? UiPadContextTable.CommonGate(f, context)
            ?? (f.UiKey is { Ready: false } k && k.Kind is not null ? "not_ready:" + k.NotReadyReason : null);
        var block = new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["receiver_token"] = receiverToken, ["menu_type"] = f.MenuType, ["context"] = context,
            ["target_token"] = token, ["ready"] = admissible && wait is null,
            ["page_index"] = f.PageIndex,
            ["components_total"] = f.GenericMenu ? f.Comps.Count : f.Comps.Count(c => c.Visible),
            ["components"] = window.Select(Component).ToArray(),
            ["buttons"] = UiPadContextTable.Row(f),
            ["denylisted_ids"] = UiPadContextTable.DenylistedIds(context, f.PageType).ToArray(),
        };
        if (f.GenericMenu)
        {
            block["generic"] = true;
            block["hud"] = f.GenericHud;
            if (f.GenericValues?.GetValueOrDefault("ready_check") is { } readiness) block["ready_check"] = readiness;
            block["state_digest"] = f.GenericLedger ?? "";
            block["component_state"] = (f.GenericState ?? Array.Empty<string>()).Take(128).Select(s => UiPadTable.Trim(s, 256)).ToArray();
            block["ambient_state"] = (f.GenericAmbientState ?? Array.Empty<string>()).Take(128).Select(s => UiPadTable.Trim(s, 256)).ToArray();
            if (f.GenericValues?.GetValueOrDefault("ui_safety") is { } safety) block["ui_safety"] = safety;
            if (f.GenericValues?.GetValueOrDefault("menu_items") is { } menuItems) block["menu_items"] = menuItems;
            if (f.GenericValues?.GetValueOrDefault("menu_container") is { } container) block["menu_container"] = container;
            if (f.GenericValues?.GetValueOrDefault("journal") is { } journal) block["journal"] = journal;
            if (f.GenericValues?.GetValueOrDefault(CraftingFacts.PageKey) is { } crafting) block[CraftingFacts.PageKey] = crafting;
            if (f.GenericValues?.GetValueOrDefault("end_of_night_status") is { } night) block["end_of_night_status"] = night;
            if (f.GenericValues?.GetValueOrDefault("passout_readiness") is { } passout) block["passout_readiness"] = passout;
            if (f.CursorSettle is { } cursor) block["cursor_settle"] = CursorItemSettle.Wire(cursor);
            if (f.GenericIdentityProbe is { } probe) block["identity_probe"] = probe;
        }
        if (wait is not null)
            block["wait_reason"] = wait;
        if (f.Extras?.JunimoScrambled == true || f.GenericValues?.GetValueOrDefault("ui_unreadable") is true)
            block["ui_unreadable"] = true;
        if (f.PageType is { } page)
            block["page_type"] = page;
        if (f.Tab >= 0)
        {
            block["tab"] = f.Tab;
            if (f.Tab <= UiPadTable.LastTab)
                block["tab_name"] = UiPadTable.TabNames[f.Tab];
        }
        block["snapped"] = f.Snapped is { } s
            ? new Dictionary<string, object> { ["id"] = s.Id, ["kind"] = s.Kind, ["label"] = UiPadTable.Trim(s.Label) }
            : (object)new Dictionary<string, object>();
        if (f.Snapped?.ItemId is { } snappedItem)
            ((Dictionary<string, object>)block["snapped"])["value"] = new Dictionary<string, object>
                { ["item"] = snappedItem, ["stack"] = f.Snapped.Stack, ["quality"] = f.Snapped.Quality };
        if (f.HeldItemId is { } held)
            block["held_item"] = new Dictionary<string, object> { ["item"] = held, ["stack"] = f.HeldStack };
        if (!string.IsNullOrEmpty(f.Text))
            block["text"] = UiPadTable.Trim(f.Text, UiPadTable.MaxBodyText);
        if (f.ShopCatalog is { } catalog) { block["shop"] = catalog.Wire(); block["money"] = catalog.Money; }
        if (f.Economy is { } economy)
        {
            block["money"] = economy.Money;
            block["economy"] = EconomySnapshot.Build(economy);
        }
        if (f.MenuType == "PurchaseAnimalsMenu" && f.PurchaseAnimals is { } purchase) block["purchase_animals"] = purchase.Snapshot();
        if (context == CharCustomTable.Context && f.CharCustom is { } charCustom)
            block[CharCustomTable.BlockKey] = CharCustomSnapshot.Build(charCustom); // Live7 (flag on only)
        if (f.Extras?.BundlePage is { } bundle)
            block["bundle"] = bundle.Snapshot();
        if (f.UiKey is { Kind: { } kind } uk)
        {
            block["ui_key_kind"] = kind;
            if (kind == UiKeyTable.Question && uk.QuestionKey is { } questionKey)
                block["question_key"] = questionKey;
            if (uk.Options is { Count: > 0 } options)
                block["options"] = options.Select((o, i) => new Dictionary<string, object>
                    { ["index"] = i, ["label"] = UiPadTable.Trim(o.Label), ["response_key"] = o.ResponseKey }).ToArray();
        }
        if (UiG3Snapshot.Block(f, context) is { } g3)
            block["g3"] = g3; // WS-G3 only
        return block;
    }

    /// <summary>
    /// The ui_pad block's own step of <see cref="UiContextWire.Fit"/> (ADR 44; <paramref name="fits"/> = its wire
    /// budget): economy rows, then the component window shrinks to the snapped component; a generic block becomes
    /// close-only (<see cref="CloseOnly"/>); then the block is dropped. Returns false when the block was reduced to
    /// close-only or removed.
    /// </summary>
    public static bool FitToBudget(Dictionary<string, object> snapshot, Func<Dictionary<string, object>, bool> fits)
    {
        if (fits(snapshot))
            return true;
        // FIXB14 review: the nav grid (runtime routing aid, ~1.7 KB) is dropped before anything of the ui_pad block.
        if (snapshot.Remove(NavGridKey) && fits(snapshot))
            return true;
        if (snapshot.GetValueOrDefault(UiPadTable.CapabilityKey) is not Dictionary<string, object> block)
            return true;
        if (block.GetValueOrDefault("generic") is true)
        {
            // ADR 44 (fix32): readback-only bulk first (the identity probe is never in a digest; ambient values
            // never fence input), then a close-only block. No truncated graph may authorize a blind press: the stub
            // keeps only the exits / answers / settles native itself admitted over the full graph.
            block.Remove("identity_probe");
            block.Remove("ambient_state");
            if (fits(snapshot))
                return true;
            // FIX78B (ADR 138; a57: 50 of 74 chest opens went close-only, so her chest page showed no slot, no item and
            // no store / take control, d12-d15). Two rungs that only remove before close-only:
            // (1) the whole graph without its geometry: bounds (runtime-only), an all -1 neighbour row (no snap graph)
            //     and a hover text equal to the label. Every control, label, value and admission stays.
            if (Compact(block) && fits(snapshot))
                return true;
            // (2) close-only plus the menu's own native primitive entries (container transfers): each carries its
            //     wire from the container block and its own readback, so no component graph is needed to admit it.
            snapshot[UiPadTable.CapabilityKey] = CloseOnly(block, keepPrimitives: true);
            if (fits(snapshot))
                return false;
            snapshot[UiPadTable.CapabilityKey] = CloseOnly(block);
            if (fits(snapshot))
                return false;
            snapshot.Remove(UiPadTable.CapabilityKey);
            return false; // explicitly unavailable
        }
        if (block.GetValueOrDefault("economy") is Dictionary<string, object> economy)
        {
            // WS-G2: the stock / recipe list shrinks to the visible rows before the component window does.
            foreach (string key in new[] { "rows", "recipes" })
                if (economy.GetValueOrDefault(key) is Dictionary<string, object>[] list)
                    economy[key] = list.Where(r => r.ContainsKey("id")).Take(8).ToArray();
            economy["truncated"] = true;
            if (fits(snapshot))
                return true;
        }
        if (block.GetValueOrDefault("components") is Dictionary<string, object>[] comps
            && block.GetValueOrDefault("snapped") is Dictionary<string, object> snapped)
        {
            block["components"] = comps.Where(c => snapped.TryGetValue("id", out object? id) && Equals(c["id"], id)).ToArray();
            block["components_truncated"] = true;
            if (fits(snapshot))
                return true;
        }
        snapshot.Remove(UiPadTable.CapabilityKey);
        return false;
    }

    public const string NavGridKey = "nav_grid";

    /// <summary>Marks a block reduced by <see cref="CloseOnly"/>; the runtime reads it as close-only.</summary>
    public const string BudgetOmittedKey = "budget_omitted";

    static readonly string[] CloseOnlyKeys =
    {
        "receiver_token", "menu_type", "context", "target_token", "ready", "page_index", "components_total", "buttons",
        "denylisted_ids", "generic", "hud", "state_digest", "wait_reason", "page_type", "tab", "tab_name", "snapped",
        "held_item", "ui_safety", "ready_check", "passout_readiness", "cursor_settle", "menu_container",
    };

    static readonly string[] CloseOnlyRoles = { "exit", "decline", "advance", "answer" };

    /// <summary>
    /// ADR 44 (fix32): the menu's identity plus only the controls that leave it, as native admitted them over the
    /// full graph: exit / decline / advance / answer entries, the container exit, and settle variants for a held
    /// cursor item. Components are only the ones those entries and the cursor settle name. Never a new admission.
    /// </summary>
    /// <summary>Marks a generic block whose components were published without geometry (<see cref="Compact"/>).</summary>
    public const string ComponentsCompactKey = "components_compact";

    /// <summary>Marks a close-only stub that also keeps the menu's native primitive entries (FIX78B).</summary>
    public const string PrimitivesKeptKey = "primitives_kept";

    /// <summary>On a primitives stub: item id -> the label its shed slot row carried (the game's display name).</summary>
    public const string ItemNamesKey = "item_names";

    /// <summary>
    /// FIX78B (ADR 138): the same graph without what only the runtime's geometry aids read: component bounds, an all -1
    /// neighbour row and a hover text equal to the label. Ids, kinds, labels, field paths, values, visibility, accepts and
    /// every affordance stay. Returns false when nothing could be removed.
    /// </summary>
    public static bool Compact(Dictionary<string, object> block)
    {
        if (block.GetValueOrDefault("components") is not Dictionary<string, object>[] comps)
            return false;
        bool removed = false;
        var compact = comps.Select(c =>
        {
            var row = new Dictionary<string, object>(c, StringComparer.Ordinal);
            removed |= row.Remove("bounds");
            if (row.GetValueOrDefault("nbr") is int[] nbr && nbr.All(n => n == -1))
                removed |= row.Remove("nbr");
            if (row.GetValueOrDefault("hover_text") is string hover && (hover.Length == 0 || hover == row.GetValueOrDefault("label") as string))
                removed |= row.Remove("hover_text");
            return row;
        }).ToArray();
        if (!removed)
            return false;
        block["components"] = compact;
        block[ComponentsCompactKey] = true;
        return true;
    }

    public static Dictionary<string, object> CloseOnly(Dictionary<string, object> block, bool keepPrimitives = false)
    {
        var stub = new Dictionary<string, object>(StringComparer.Ordinal);
        foreach (string key in CloseOnlyKeys)
            if (block.TryGetValue(key, out object? value))
                stub[key] = value;
        var ids = new HashSet<int>();
        if (block.GetValueOrDefault("affordances") is Dictionary<string, object> published
            && published.GetValueOrDefault("list") is IEnumerable<Dictionary<string, object>> list)
        {
            var kept = new List<Dictionary<string, object>>();
            var index = new Dictionary<int, int>();
            int at = 0;
            foreach (var entry in list)
            {
                int old = at++;
                Dictionary<string, object>? keep = null;
                if (entry.GetValueOrDefault("role") is string role && CloseOnlyRoles.Contains(role)
                    || entry.GetValueOrDefault("primitive") is "container_exit"
                    || keepPrimitives && entry.GetValueOrDefault("verb") is "transfer")
                    keep = entry;
                else if (entry.GetValueOrDefault("buttons") is string[] buttons && buttons.Contains("settle"))
                    keep = new Dictionary<string, object>(entry, StringComparer.Ordinal) { ["buttons"] = new[] { "settle" } };
                if (keep is null)
                    continue;
                index[old] = kept.Count;
                kept.Add(keep);
                if (keep.GetValueOrDefault("control") is int control) ids.Add(control);
                if (keep.GetValueOrDefault("controls") is int[] controls) ids.UnionWith(controls);
            }
            var reduced = new Dictionary<string, object>(StringComparer.Ordinal) { ["v"] = published.GetValueOrDefault("v") ?? 1, ["list"] = kept.ToArray() };
            if (published.TryGetValue("none", out object? none)) reduced["none"] = none;
            if (published.GetValueOrDefault("pending") is Dictionary<string, object> pending)
            {
                var remapped = new Dictionary<string, object>(StringComparer.Ordinal);
                foreach (var (key, value) in pending)
                    if (value is int i) { if (index.TryGetValue(i, out int moved)) remapped[key] = moved; }
                    else if (value is int[] many) remapped[key] = many.Where(index.ContainsKey).Select(m => index[m]).ToArray();
                    else remapped[key] = value; // kind / receiver_token
                reduced["pending"] = remapped;
            }
            stub["affordances"] = reduced;
        }
        if (block.GetValueOrDefault("cursor_settle") is { } settle)
        {   // CursorItemSettle.Wire is an anonymous shape; read its target ids from its own wire form
            JsonElement wire = JsonSerializer.SerializeToElement(settle, ProtocolJson.Options);
            if (wire.ValueKind == JsonValueKind.Object && wire.TryGetProperty("targets", out JsonElement targets)
                && targets.ValueKind == JsonValueKind.Array)
                foreach (JsonElement target in targets.EnumerateArray())
                    if (target.ValueKind == JsonValueKind.Object && target.TryGetProperty("id", out JsonElement id)
                        && id.TryGetInt32(out int tid)) ids.Add(tid);
        }
        if (block.GetValueOrDefault("snapped") is Dictionary<string, object> snapped && snapped.GetValueOrDefault("id") is int snap)
            ids.Add(snap);
        // Hover / text values are presentation; the row keeps what the runtime checks a control with.
        stub["components"] = block.GetValueOrDefault("components") is Dictionary<string, object>[] comps
            ? comps.Where(c => c.GetValueOrDefault("id") is int id && ids.Contains(id))
                .Select(c => c.Where(p => p.Key is not ("hover_text" or "text_value"))
                    .ToDictionary(p => p.Key, p => p.Value, StringComparer.Ordinal)).ToArray()
            : Array.Empty<Dictionary<string, object>>();
        stub["components_truncated"] = true;
        stub[BudgetOmittedKey] = true;
        if (keepPrimitives)
        {
            stub[PrimitivesKeptKey] = true;
            // The game's own display name of each item the shed slot rows held (their labels), so a kept move says
            // what it carries. One row per item id, bounded like the component list.
            var names = new Dictionary<string, object>(StringComparer.Ordinal);
            if (block.GetValueOrDefault("components") is Dictionary<string, object>[] rows)
                foreach (var row in rows)
                    if (row.GetValueOrDefault("value") is Dictionary<string, object> value && value.GetValueOrDefault("item") is string item
                        && row.GetValueOrDefault("label") is string label && label.Length > 0 && names.Count < 96)
                        names.TryAdd(item, label);
            stub[ItemNamesKey] = names;
        }
        return stub;
    }

    /// <summary>Capability entry (always published with the flag; availability = admission now).</summary>
    public static Dictionary<string, object> Capability(bool available, string reason) => new(StringComparer.Ordinal)
    {
        ["implemented"] = true, ["available"] = available, ["reason"] = reason,
        ["primitives"] = new[] { "ui_inspect", UiPadTable.PrimitiveNav, UiPadTable.PrimitivePress },
    };
}

/// <summary>Admission composition. No window-focus term anywhere (ADR 244).</summary>
public static class UiPadRoute
{
    /// <summary>ADR 244: no window-focus term. A menu press is a direct call; the warp guard blocks snaps in both states.</summary>
    public static bool Ready(bool configEnabled, bool inboxEnabled, bool uiPadEnabled, bool padPathConfigured, bool warpGuardVerified,
        bool observerReady, bool isMainPlayer, bool isSplitScreen, bool worldReady) =>
        configEnabled && inboxEnabled && uiPadEnabled && padPathConfigured && warpGuardVerified && observerReady
        && !isMainPlayer && !isSplitScreen && worldReady;
}
