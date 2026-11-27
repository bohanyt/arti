using System.Collections;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Reflection;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework.Input;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>
/// Primitives v2 WS-G1 wiring (game thread only): ui_inspect (ui.context "ui_pad"), ui_nav, ui_press.
/// The table, binding, denylist, executor and verdicts are pure (<see cref="UiPadContextTable"/>,
/// <see cref="UiPadExecutor"/>); this file reads the active menu into <see cref="UiPadFacts"/> with field
/// reads only (ui_inspect never calls performHoverAction and never writes) and implements
/// <see cref="IUiPadPort"/> over the real menu. Snap calls warp the OS cursor through
/// Game1.setMousePosition -> InputState.SetMousePosition, which the WS-G0 guard skips while the farmhand
/// window is inactive: every snap entry point asserts the guard first. The v1 ui_key contexts run ui_key's
/// own call (InvokeUiKey) and settle. Never Helper.Input, never a pad Press, never an
/// activeClickableMenu assignment. Flag <c>UiPadMenusEnabled</c> (default OFF). Evidence ceiling: UNIT_TESTED.
/// </summary>
public sealed partial class ModEntry
{
    sealed class UiPadSettleState
    {
        public MenuStepCommandRequest Request;
        public UiKeyInput Input = null!;
        public IClickableMenu Original = null!;
        public UiKeyTarget Target = null!;
        public UiKeyState State = null!;
        public Stopwatch Clock = null!;
        public long PlayerId;
        public string Step = "";
    }

    private UiPadSettleState? uiPadSettle;
    private string? uiPadUncertainReceiver;
    private static readonly ConcurrentDictionary<Type, FieldInfo[]> UiPadFieldCache = new();

    /// <summary>
    /// Admission: flag, inbox, the WS-G0 pad path + verified OS-cursor guard. ADR 244: no window-focus term;
    /// a menu press is a direct call into the menu and the guard blocks every snap warp in both states.
    /// </summary>
    bool UiPadReady => UiPadRoute.Ready(this.config.Enabled, this.config.MenuStepCommandInboxEnabled, this.config.UiPadMenusEnabled,
        this.PadPathConfigured, PadCursorWarpGuard.Verified, this.transferObserverReady, Context.IsMainPlayer, Context.IsSplitScreen,
        Context.IsWorldReady);

    /// <summary>session.json ui_pad_enabled (omitted with the flag off: the v1 file is unchanged).</summary>
    private CommandInboxSession WithUiPadSessionFacts(CommandInboxSession session) =>
        this.config.UiPadMenusEnabled ? session with
        {
            UiPadEnabled = this.UiPadReady,
            EconomyEnabled = this.config.EconomyMenusEnabled ? this.EconomyReady : null, // WS-G2 (omitted with its flag off)
            // WS-G2: the shipping-bin modes admit container_transfer too (flag on only; v1 term unchanged otherwise).
            ContainerTransferEnabled = session.ContainerTransferEnabled || (this.config.EconomyMenusEnabled && this.BinTransferReady),
        } : session;

    // ------------------------------------------------------------------ read-only facts (ui_inspect)

    static string UiPadTrim(string? text, int max = UiPadTable.MaxLabel) => UiPadTable.Trim(text, max);

    static FieldInfo[] UiPadFields(Type type) => UiPadFieldCache.GetOrAdd(type, t => t.GetFields());

    /// <summary>
    /// Read-only replica of IClickableMenu.populateClickableComponentList (IClickableMenu.cs:206-310) into a LOCAL
    /// list: used only when the menu has not populated its own list yet (snappy menus off). No field is written.
    /// </summary>
    static List<ClickableComponent> UiPadReplicaComponents(IClickableMenu menu)
    {
        var result = new List<ClickableComponent>();
        foreach (FieldInfo field in UiPadFields(menu.GetType()))
        {
            Type type = field.FieldType;
            if (type.IsPrimitive || type == typeof(string) || field.GetCustomAttribute<SkipForClickableAggregation>() is not null
                || field.DeclaringType == typeof(IClickableMenu))
                continue;
            object? value = field.GetValue(menu);
            switch (value)
            {
                case ClickableComponent single:
                    result.Add(single);
                    break;
                case List<List<ClickableTextureComponent>> grid:
                    foreach (List<ClickableTextureComponent> row in grid)
                        result.AddRange(row.Where(c => c is not null));
                    break;
                case InventoryMenu inventory:
                    result.AddRange(inventory.inventory);
                    result.Add(inventory.dropItemInvisibleButton);
                    break;
                case List<Dictionary<ClickableTextureComponent, CraftingRecipe>> recipes:
                    foreach (Dictionary<ClickableTextureComponent, CraftingRecipe> page in recipes)
                        result.AddRange(page.Keys);
                    break;
                case Dictionary<int, List<List<ClickableTextureComponent>>> pages:
                    foreach (List<List<ClickableTextureComponent>> page in pages.Values)
                        foreach (List<ClickableTextureComponent> row in page)
                            result.AddRange(row);
                    break;
                case IDictionary map when type.IsGenericType && type.GetGenericTypeDefinition() == typeof(Dictionary<,>):
                    foreach (DictionaryEntry entry in map)
                    {
                        if (entry.Key is ClickableComponent k) result.Add(k);
                        if (entry.Value is ClickableComponent v) result.Add(v);
                    }
                    break;
                case IEnumerable list when type.IsGenericType && type.GetGenericTypeDefinition() == typeof(List<>)
                                           && typeof(ClickableComponent).IsAssignableFrom(type.GetGenericArguments()[0]):
                    foreach (object item in list)
                        if (item is ClickableComponent c) result.Add(c);
                    break;
            }
        }
        if (menu.upperRightCloseButton is { } close)
            result.Add(close);
        return result;
    }

    static string UiPadItemId(Item? item) => item?.QualifiedItemId ?? "";

    // Pure native virtual hook: CraftingPage=false, Junimo=!specificBundlePage,
    // InventoryPage/other base menus=true. Invoke dispatches the owner's override;
    // never call automaticSnapBehavior (which would write the snapped component).
    static bool UiPadPrioritizeAligned(IClickableMenu owner) =>
        (bool)(typeof(IClickableMenu).GetMethod("_ShouldAutoSnapPrioritizeAlignedElements",
            BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(owner, null)!);

    /// <summary>Components of the snap owner, labelled from fields only (item in slot, response text, tab label...).</summary>
    static List<UiPadComponent> ReadUiPadComponents(IClickableMenu owner, GameMenu? gameMenu, bool extras = false)
    {
        if (NativeJunimoUi.Unreadable(owner)) return new();
        var slotItems = new Dictionary<ClickableComponent, Item?>(ReferenceEqualityComparer.Instance);
        foreach (FieldInfo field in UiPadFields(owner.GetType()))
        {
            if (field.FieldType != typeof(InventoryMenu) || field.GetValue(owner) is not InventoryMenu inv)
                continue;
            foreach (ClickableComponent slot in inv.inventory)
            {
                Item? item = int.TryParse(slot.name, out int index) && inv.actualInventory is { } actual
                    && index >= 0 && index < actual.Count ? actual[index] : null;
                slotItems[slot] = item;
            }
        }
        var tabs = new HashSet<ClickableComponent>(gameMenu?.tabs ?? new List<ClickableComponent>(), ReferenceEqualityComparer.Instance);
        var equipment = new HashSet<ClickableComponent>((owner as InventoryPage)?.equipmentIcons ?? new List<ClickableComponent>(),
            ReferenceEqualityComparer.Instance);
        DialogueBox? box = owner as DialogueBox;
        List<ClickableComponent> source = extras && owner is JunimoNoteMenu note ? NativeJunimoUi.Components(note)
            : owner.allClickableComponents is { } own ? own.ToList() : UiPadReplicaComponents(owner);
        if (owner.allClickableComponents is null && gameMenu is not null)
            source.AddRange(gameMenu.tabs);
        if (box?.responseCC is { } responses)
            source.AddRange(responses);
        var seen = new HashSet<ClickableComponent>(ReferenceEqualityComparer.Instance);
        var result = new List<UiPadComponent>();
        foreach (ClickableComponent c in source)
        {
            if (c is null || !seen.Add(c))
                continue;
            string kind;
            string label;
            Item? item = null;
            if (box?.responseCC is { } rcc && rcc.Contains(c))
            {
                kind = UiPadComponent.KindResponse;
                label = box.responses is { } rs && c.myID >= 0 && c.myID < rs.Length ? rs[c.myID]?.responseText ?? "" : "";
            }
            else if (tabs.Contains(c))
            {
                kind = UiPadComponent.KindTab;
                label = c.label ?? c.name ?? "";
            }
            else if (ReferenceEquals(c, owner.upperRightCloseButton))
            {
                kind = UiPadComponent.KindClose;
                label = "close";
            }
            else if (slotItems.TryGetValue(c, out Item? slotItem))
            {
                kind = UiPadComponent.KindSlot;
                item = slotItem;
                label = slotItem?.DisplayName ?? "empty slot";
            }
            else if (extras && G6ComponentKind(owner, c) is { } g6)
            {
                // WS-G6: floor buttons, bundle / ingredient / back / purchase buttons, museum OK (labels from fields).
                kind = g6.Kind;
                label = g6.Label;
                item = g6.Item;
            }
            else if (equipment.Contains(c))
            {
                kind = UiPadComponent.KindEquipment;
                label = c.name ?? "";
            }
            else if (c.item is { } held)
            {
                kind = UiPadComponent.KindItem;
                item = held;
                label = held.DisplayName ?? "";
            }
            else
            {
                kind = UiPadComponent.KindButton;
                label = !string.IsNullOrWhiteSpace(c.ScreenReaderText) ? c.ScreenReaderText
                    : !string.IsNullOrWhiteSpace(c.label) ? c.label
                    : c is ClickableTextureComponent t && !string.IsNullOrWhiteSpace(t.hoverText) ? t.hoverText
                    : c.name ?? "";
            }
            result.Add(new UiPadComponent(c.myID, kind, UiPadTrim(label), item is null ? null : UiPadItemId(item),
                item?.Stack ?? 0, item?.Quality ?? 0, c.upNeighborID, c.rightNeighborID, c.downNeighborID, c.leftNeighborID,
                c.visible, c.bounds.X, c.bounds.Y, c.bounds.Width, c.bounds.Height));
        }
        // Resolve automatic edges read-only. Never snap/move the menu just to inspect it.
        if (extras && owner is JunimoNoteMenu { specificBundlePage: true, currentPartialIngredientDescriptionIndex: < 0 } junimo)
            result = result.Select(c =>
            {
                ClickableComponent? native = source.FirstOrDefault(n => n.myID == c.Id);
                if (native is null) return c;
                int Edge(int direction, int raw) => raw != -99998 ? raw : JunimoNavigation.Resolve(c, direction,
                    result.Where(candidate => source.FirstOrDefault(n => n.myID == candidate.Id) is { } target
                        && junimo.IsAutomaticSnapValid(direction, native, target)), UiPadPrioritizeAligned(junimo));
                return c with { Up = Edge(0, c.Up), Right = Edge(1, c.Right), Down = Edge(2, c.Down), Left = Edge(3, c.Left) };
            }).ToList();
        if (owner is CraftingPage crafting)
            result = result.Select(c =>
            {
                ClickableComponent? native = source.FirstOrDefault(n => n.myID == c.Id);
                if (native is null) return c;
                int Edge(int direction, int raw) => raw != -99998 ? raw : JunimoNavigation.Resolve(c, direction,
                    result.Where(candidate => source.FirstOrDefault(n => n.myID == candidate.Id) is { } target
                        && crafting.IsAutomaticSnapValid(direction, native, target)), UiPadPrioritizeAligned(crafting));
                return c with { Up = Edge(0, c.Up), Right = Edge(1, c.Right), Down = Edge(2, c.Down), Left = Edge(3, c.Left) };
            }).ToList();
        return result;
    }

    int UiPadPageIndex(IClickableMenu owner)
    {
        try
        {
            return owner switch
            {
                LetterViewerMenu letter => letter.page,
                QuestLog log => this.Helper.Reflection.GetField<int>(log, "currentPage").GetValue() * 100
                    + this.Helper.Reflection.GetField<int>(log, "questPage").GetValue() + 1,
                SocialPage social => social.slotPosition,
                CraftingPage crafting => crafting.currentCraftingPage,
                CollectionsPage collections => collections.currentPage,
                JunimoNoteMenu note when this.config.WorldExtrasEnabled => NativeJunimoUi.PageIndex(note),
                // WS-G2: a stock scroll (ShopMenu.customSnapBehavior -> downArrowPressed, ShopMenu.cs:688-724) keeps the
                // snapped row; the scroll index makes ui_nav report it as "scrolled".
                ShopMenu shop when this.config.EconomyMenusEnabled => shop.currentItemIndex,
                _ => 0,
            };
        }
        catch
        {
            return 0;
        }
    }

    /// <summary>
    /// readyToClose only where it is a pure getter (decomp-checked: GameMenu -> its pages, boards, quest log, pond,
    /// letter); null elsewhere (e.g. AnimalQueryMenu.readyToClose writes textBox.Selected, AnimalQueryMenu.cs:413).
    /// A null value refuses every close (UI_DENIED:not_ready_to_close).
    /// </summary>
    static bool? UiPadReadyToClose(IClickableMenu top, bool economy = false)
    {
        Type t = top.GetType();
        bool pure = t == typeof(GameMenu) || t == typeof(Billboard) || t == typeof(SpecialOrdersBoard) || t == typeof(QuestLog)
            || t == typeof(PondQueryMenu) || t == typeof(LetterViewerMenu)
            // WS-G2: ShopMenu.readyToClose = heldItem == null && no animations (ShopMenu.cs:1255-1262); CraftingPage =
            // heldItem == null (CraftingPage.cs:600-603): both pure.
            || (economy && (t == typeof(ShopMenu) || t == typeof(CraftingPage)))
            // WS-G6 (decomp-checked pure): IClickableMenu.readyToClose (true) / MuseumMenu.readyToClose (fields only).
            || t == typeof(MineElevatorMenu) || t == typeof(MuseumMenu);
        return pure || NativeJunimoUi.Unreadable(top) ? top.readyToClose() : null;
    }

    /// <summary>One read of the active menu. Field reads and pure getters only (no hover, no populate, no write).</summary>
    UiPadFacts ReadUiPadFacts()
    {
        IClickableMenu? top = Game1.activeClickableMenu;
        if (this.config.UiPadMenusEnabled)
        {
            IClickableMenu? surface = GenericMenuSurface;
            UiPadFacts facts = this.ReadGenericUiFacts(surface);
            // Character creation keeps its named controls and native executor over the generic observation.
            return this.ReadCharCustom(surface) is { } charCustom
                ? facts with { CharCustom = charCustom, EventUp = Game1.eventUp || Game1.farmEvent is not null,
                    MinigameActive = Game1.currentMinigame is not null }
                : facts;
        }
        if (top is null)
            return UiPadFacts.Closed() with { PurchaseAnimals = this.PurchaseIdentityCurrent ? this.purchaseAnimals?.Read() : null };
        GameMenu? gameMenu = top as GameMenu;
        IClickableMenu owner = gameMenu?.GetCurrentPage() ?? top;
        string type = top.GetType().Name;
        // Generic v2 inspection keeps all readiness gates but can describe unfamiliar response keys.
        // The v1 ui_key classifier/executor keeps its existing allowlist.
        UiKeyState? uiKey = UiKeyWhitelistedType(top) ? UiKeyClassifier.Classify(this.ReadUiKeyFacts(top), genericUi: true) : null;
        EconomyFacts? economy = this.ReadEconomyFacts(top, owner); // WS-G2: null with EconomyMenusEnabled off
        Item? held = owner is InventoryPage || top is PurchaseAnimalsMenu ? Game1.player?.CursorSlotItem
            : economy is not null ? EconomyHeldItem(top, owner)
            : top is JunimoNoteMenu junimo && this.config.WorldExtrasEnabled ? junimo.heldItem // WS-G6
            : (top as MenuWithInventory)?.heldItem;
        string? text = top switch
        {
            JunimoNoteMenu { scrambledText: true } => NativeJunimoUi.UnreadableText,
            DialogueBox box => box.getCurrentString(),
            LetterViewerMenu letter when letter.mailMessage is { } pages && letter.page >= 0 && letter.page < pages.Count => pages[letter.page],
            _ => null,
        };
        var purchase = this.ReadPurchaseAnimals(top);
        return new UiPadFacts(
            MenuType: type,
            MenuOpen: true,
            ReceiverToken: this.ReceiverTokenOf(CurrentInteractiveMenu(top)),
            PageType: gameMenu is null ? null : owner.GetType().Name,
            Tab: gameMenu?.currentTab ?? -1,
            IsActive: top.IsActive(),
            ReadyToClose: UiPadReadyToClose(top, economy is not null),
            HasChildOrParent: top.GetChildMenu() is not null || top.GetParentMenu() is not null,
            EventUp: Game1.eventUp || Game1.farmEvent is not null,
            TextSubscriber: Game1.keyboardDispatcher?.Subscriber is not null,
            MinigameActive: Game1.currentMinigame is not null,
            SnappedId: owner is JunimoNoteMenu note && this.config.WorldExtrasEnabled
                ? NativeJunimoUi.SnappedId(note) : owner.currentlySnappedComponent?.myID,
            Components: purchase is not null ? this.purchaseAnimals!.Components() : LabelEconomyComponents(ReadUiPadComponents(owner, gameMenu, this.config.WorldExtrasEnabled), economy),
            HeldItemId: held is null ? null : UiPadItemId(held),
            HeldStack: held?.Stack ?? 0,
            PageIndex: this.UiPadPageIndex(owner),
            UiKey: uiKey,
            ForcePreventClose: gameMenu is not null && GameMenu.forcePreventClose,
            InventorySlots: (owner as InventoryPage)?.inventory?.inventory?.Count ?? 0,
            Text: text,
            LetterFromCollection: top is LetterViewerMenu { isFromCollection: true },
            G3: this.ReadUiG3Facts(top), // WS-G3: null with TextEventMenusEnabled off
            Economy: economy,
            Extras: this.config.WorldExtrasEnabled ? this.ReadUiPadExtrasFacts(top) : null, // WS-G6
            ShippingBin: this.config.EconomyMenusEnabled && IsShippingBinMenu(top, out _, out _), PurchaseAnimals: purchase ?? (this.PurchaseIdentityCurrent ? this.purchaseAnimals?.Read() : null), // FIXB7 D-B5b-3
            CharCustom: this.ReadCharCustom(top)); // Live7: null with CharacterCreationEnabled off
    }

    /// <summary>Observation only: capability + the bounded ui_pad block (flag off = nothing added).</summary>
    void EnrichUiPadContext(Dictionary<string, object> snapshot)
    {
        if (!this.config.UiPadMenusEnabled)
            return;
        this.EnsureGenericUiRenderText();
        snapshot["rendered_ui_text"] = GenericUiRenderedText.Current(Game1.activeClickableMenu is null ? "hud" : "menu", Game1.activeClickableMenu);
        snapshot["generic_ui_state"] = GenericUiLedger();
        snapshot["hud_messages"] = GenericHudText();
        this.EnrichUiMotorContext(snapshot);
        this.EnrichUiG3Context(snapshot); // WS-G3: ui_event / ready_check (no-op with TextEventMenusEnabled off)
        this.EnrichFarmerProfile(snapshot); // Live7: farmer_profile (no-op with CharacterCreationEnabled off)
        Dictionary<string, object>? caps = snapshot.GetValueOrDefault("capabilities") as Dictionary<string, object>;
        try
        {
            bool ready = this.UiPadReady;
            UiPadFacts facts = this.ReadUiPadFacts();
            if (GenericMenuSurface is { } surface && !ReferenceEquals(surface, Game1.activeClickableMenu))
                snapshot["interactive"] = new Dictionary<string, object> {
                    ["receiver_token"] = facts.ReceiverToken!, ["menu_type"] = facts.MenuType,
                    ["visible_text"] = facts.Text ?? "" };
            string? published = (snapshot.GetValueOrDefault("interactive") as Dictionary<string, object>)
                ?.GetValueOrDefault("receiver_token") as string;
            if ((!facts.MenuOpen && !facts.GenericHud) || facts.ReceiverToken is null || !facts.GenericHud && facts.ReceiverToken != published)
            {
                if (caps is not null)
                    caps[UiPadTable.CapabilityKey] = UiPadSnapshot.Capability(false, ready ? "no_menu" : "backend_disabled");
                return;
            }
            bool latched = this.uiPadUncertainReceiver == facts.ReceiverToken || this.uiKeyUncertainReceiver == facts.ReceiverToken;
            bool busy = this.UiMotorUnsafe || this.uiMotor is not null || this.uiPadSettle is not null || this.uiKeySettle is not null;
            string reason = !ready ? "backend_disabled" : latched ? "uncertain_latched" : busy ? "busy" : "ready";
            if (caps is not null)
                caps[UiPadTable.CapabilityKey] = UiPadSnapshot.Capability(ready && !latched && !busy, reason);
            var block = UiPadSnapshot.Build(facts, facts.ReceiverToken, ready && !latched && !busy,
                latched ? "uncertain_latched" : busy ? "busy" : !ready ? "backend_disabled" : null);
            block["affordances"] = UiAffordances.Publish(facts, this.UiGate(facts.ReceiverToken), (snapshot.GetValueOrDefault("container") as Dictionary<string, object>)
                ?.GetValueOrDefault("affordances") as Dictionary<string, object>[]);
            // ADR 230 N1: the open calendar board's cells (day, mark, its own display string), read one by one by her.
            if (CalendarBoard.Cells(Game1.activeClickableMenu) is { } cells) block["calendar_days"] = cells;
            snapshot[UiPadTable.CapabilityKey] = block;
            // ADR 143: fit only at the completed stream/inspect capture boundary, after
            // RequireUiActionFacts. An early fit can delete the wire behind these affordances.
        }
        catch (Exception)
        {
            if (caps is not null)
                caps[UiPadTable.CapabilityKey] = UiPadSnapshot.Capability(false, "read_failed");
            snapshot.Remove(UiPadTable.CapabilityKey);
        }
    }

    // ------------------------------------------------------------------ the port over the real menu

    static Buttons UiPadButtonOf(string button) => button switch
    {
        UiPadTable.BtnA => Buttons.A,
        UiPadTable.BtnB => Buttons.B,
        UiPadTable.BtnX => Buttons.X,
        UiPadTable.BtnY => Buttons.Y,
        UiPadTable.BtnLB => Buttons.LeftShoulder,
        UiPadTable.BtnRB => Buttons.RightShoulder,
        UiPadTable.BtnLT => Buttons.LeftTrigger,
        UiPadTable.BtnRT => Buttons.RightTrigger,
        UiPadTable.BtnStart => Buttons.Start,
        UiPadTable.BtnSelect => Buttons.Back,
        _ => throw new ArgumentOutOfRangeException(nameof(button), button, "no pad button"),
    };

    /// <summary>
    /// IUiPadPort over the deepest menu (= Game1.activeClickableMenu: a child / parent chain is refused before)
    /// and its snap owner (GameMenu: the current page). Every call runs in UI mode like Game1.updateActiveMenu
    /// (G1:4276-4278). Snap entry points assert the OS-cursor guard first.
    /// </summary>
    private sealed partial class UiPadGamePort : IEconomyPort, IGenericUiPort
    {
        private readonly ModEntry mod;
        private readonly IClickableMenu top;

        public UiPadGamePort(ModEntry mod, IClickableMenu top)
        {
            this.mod = mod;
            this.top = top;
        }

        private IClickableMenu Owner => (this.top as GameMenu)?.GetCurrentPage() ?? this.top;

        public long UnblockedWarps => PadCursorWarpGuard.Counters.UnblockedInactive;

        public UiPadFacts Read() => this.mod.ReadUiPadFacts();

        /// <summary>The snap warp must hit the guard: the verified prefix (ADR 244: it blocks with the window active too).</summary>
        private static void AssertWarpGuarded()
        {
            if (!PadCursorWarpGuard.Verified)
                throw new InvalidOperationException("ui_pad snap without the OS-cursor guard");
        }

        private void InUiMode(Action call)
        {
            Game1.PushUIMode();
            try { this.mod.ObserveNativeProgressInput(call); }
            finally { Game1.PopUIMode(); }
        }

        public void SnapToDefault()
        {
            AssertWarpGuarded();
            IClickableMenu owner = this.Owner;
            InUiMode(() =>
            {
                if (owner.allClickableComponents is null || owner is JunimoNoteMenu && this.mod.config.WorldExtrasEnabled)
                    owner.populateClickableComponentList(); // what applyMovementKey does first (IClickableMenu.cs:317)
                owner.snapToDefaultClickableComponent();
                this.SyncEventResponseSelection(owner);
            });
        }

        public void ApplyMovementKey(int dir)
        {
            AssertWarpGuarded();
            IClickableMenu owner = this.Owner;
            InUiMode(() =>
            {
                if (owner is JunimoNoteMenu && this.mod.config.WorldExtrasEnabled)
                    owner.populateClickableComponentList(); // admitted navigation uses the current native page
                owner.applyMovementKey(dir);
                this.SyncEventResponseSelection(owner);
            });
        }

        // FIXB49: inactive input dispatch does not run vanilla's hover synchronization. Mirror ONLY its
        // response-selection field, after admitted navigation; never call hover or mutate during inspect.
        private void SyncEventResponseSelection(IClickableMenu owner)
        {
            if (this.mod.UiG3On && Game1.eventUp && owner is DialogueBox { isQuestion: true } box
                && box.GetType() == typeof(DialogueBox) && box.currentlySnappedComponent is { } snapped
                && box.responseCC is { } responses && responses.Contains(snapped)
                && snapped.myID >= 0 && snapped.myID < box.responses.Length)
                box.selectedResponse = snapped.myID;
        }

        public void SetSnapped(int id)
        {
            AssertWarpGuarded();
            IClickableMenu owner = this.Owner;
            InUiMode(() =>
            {
                owner.setCurrentlySnappedComponentTo(id);
                owner.snapCursorToCurrentSnappedComponent();
            });
        }

        public void GamePadButton(string button)
        {
            AssertWarpGuarded();
            InUiMode(() => this.top.receiveGamePadButton(UiPadButtonOf(button)));
        }

        public void LeftClick(int x, int y)
        {
            AssertWarpGuarded();
            InUiMode(() => this.top.receiveLeftClick(x, y));
        }

        public void RightClick(int x, int y)
        {
            AssertWarpGuarded();
            InUiMode(() => this.top.receiveRightClick(x, y));
        }

        public string KeyClass(string button)
        {
            Keys key = Utility.mapGamePadButtonToKey(UiPadButtonOf(button));
            if (key == Keys.None)
                return "none";
            Options o = Game1.options;
            if (o.doesInputListContain(o.menuButton, key))
                return "menu";
            if (o.doesInputListContain(o.moveUpButton, key) || o.doesInputListContain(o.moveRightButton, key)
                || o.doesInputListContain(o.moveDownButton, key) || o.doesInputListContain(o.moveLeftButton, key))
                return "move";
            if (o.doesInputListContain(o.journalButton, key))
                return "journal";
            InputButton[][] slots =
            {
                o.inventorySlot1, o.inventorySlot2, o.inventorySlot3, o.inventorySlot4, o.inventorySlot5, o.inventorySlot6,
                o.inventorySlot7, o.inventorySlot8, o.inventorySlot9, o.inventorySlot10, o.inventorySlot11, o.inventorySlot12,
            };
            return slots.Any(s => o.doesInputListContain(s, key)) ? "slot" : "other";
        }

        public void KeyPress(string button)
        {
            AssertWarpGuarded();
            InUiMode(() => this.top.receiveKeyPress(Utility.mapGamePadButtonToKey(UiPadButtonOf(button))));
        }

        public void ChangeTab(int tab)
        {
            AssertWarpGuarded();
            InUiMode(() => (this.top as GameMenu ?? throw new InvalidOperationException("changeTab outside GameMenu")).changeTab(tab));
        }

        public bool StillActive() => this.top.IsActive();

        public void MuseumPlace()
        {
            AssertWarpGuarded();
            if (this.top is not MuseumMenu museum || MuseumPlacePoint(museum) is not { } point)
                throw new InvalidOperationException("no free visible museum tile");
            InUiMode(() => museum.receiveLeftClick(point.X, point.Y));
        }
    }

    // ------------------------------------------------------------------ admission + execution

    /// <summary>PM-1 fix8: the receiver-state arm terms, shared by ExecuteUiPad and the published affordances.</summary>
    UiArmGate UiGate(string? receiver) => new(this.UiPadReady,
        this.UiMotorUnsafe || this.uiMotor is not null || this.uiPadSettle is not null || this.uiKeySettle is not null
            || this.commandInbox.ExecutionSlot.CleanupPending?.Invoke() == true,
        this.actionOwner.ReleaseRequested || this.actionOwner.StopLatched,
        receiver is not null && (this.uiPadUncertainReceiver == receiver || this.uiKeyUncertainReceiver == receiver),
        this.EconomyReady, this.backgroundMove?.MenuDebtRefusal(UiPadTable.PrimitivePress));

    void RejectUiPad(MenuStepCommandRequest request, CommandInboxIdentity pin, string reason)
    {
        this.Monitor.Log($"ui_pad REJECT request_id={request.RequestId} primitive={request.Primitive} reason={reason}", LogLevel.Warn);
        CommandRejectReason code = reason == "UiPadBusy" ? CommandRejectReason.CommandBusy
            : reason == "UiPadTargetChanged" ? CommandRejectReason.BindingMismatch
            : CommandRejectReason.ArmRejected;
        this.WriteCommandResult(this.commandInbox.CompleteRejected(request, code, pin, NowUnixMs(),
            code == CommandRejectReason.ArmRejected ? BgReason.PreconditionUnavailable(reason) : null));
    }

    void ExecuteUiPad(MenuStepCommandRequest request)
    {
        this.ApplyActionOwnerRecords();
        CommandInboxIdentity pin = this.CurrentCommandIdentity();
        bool nav = MenuStepCommandInbox.IsUiNav(request);
        UiNavTarget? navTarget = request.UiNav;
        UiPressTarget? pressTarget = request.UiPress;
        string? receiver = navTarget?.ReceiverToken ?? pressTarget?.ReceiverToken;
        string? menuType = navTarget?.MenuType ?? pressTarget?.MenuType;
        string? contextArg = navTarget?.Context ?? pressTarget?.Context;
        string? token = navTarget?.TargetToken ?? pressTarget?.TargetToken;
        UiPadFacts facts;
        try { facts = this.ReadUiPadFacts(); }
        catch (Exception ex)
        {
            this.Monitor.Log($"ui_pad facts unreadable: {ex.GetType().Name}", LogLevel.Warn);
            this.RejectUiPad(request, pin, "UiPadUnsupported");
            return;
        }
        string context = UiPadContextTable.ContextOf(facts);
        string button = nav ? "nav:" + navTarget?.Dir : pressTarget?.Button ?? "";
        UiPadPlan? plan = !nav && pressTarget is not null
            ? UiPadContextTable.AdmitPress(pressTarget, facts) : null;
        if (request.VerbEnvelope is { } envelope && envelope.GetProperty("args").TryGetProperty("notches", out var notches)
            && plan is { Allowed: true } && facts.GenericMenu)
            plan = plan with { Steps = plan.Steps.Select(s => s.Kind == UiPadStepKind.GenericInput && s.G3Action == "scroll"
                ? s with { G3Arg = (notches.GetInt32() * 120).ToString() } : s).ToArray() };
        UiArmGate gate = this.UiGate(receiver);
        string? refuse = !this.UiPadReady ? "UiPadNotReady"
            : (navTarget is null && pressTarget is null) || receiver is null ? "UiPadUnsupported"
            : gate.Early() is { } early ? early
            : this.commandInbox.ExecutionSlot.Blocks(request) ? "UiPadBusy"
            : !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request, pin, Context.IsWorldReady,
                Context.IsPlayerFree, Context.CanPlayerMove, out _) ? "UiPadTargetChanged"
            : !this.actionOwner.CanAdmitLeasedInputOnce(pin, NowUnixMs(), request.RuntimeInstanceId, request.OwnerId,
                request.Generation, request.ControlSeq, out _) ? "UiPadNotReady"
            : (!facts.MenuOpen && !facts.GenericHud) || facts.ReceiverToken != receiver || facts.MenuType != menuType || context != contextArg
                ? "UiPadTargetChanged"
            : UiPadToken.Compute(receiver, facts.MenuType, context, facts) != token ? "UiPadTargetChanged"
            : !nav && pressTarget!.SnappedId != (facts.SnappedId ?? -1) ? "UiPadTargetChanged"
            // ADR 250: only when a motor hook failed to install; the same check runs again at every staged and seated frame.
            : UiMotorFrontPath.Refusal(UiMotorInputSeatPatch.Installed, UiMotorMenuUpdatePatch.Installed,
                Game1.game1?.IsActive == true, plan) is { } frontRefusal ? frontRefusal
            : gate.Late(facts, plan, nav ? navTarget!.Dir : null); // PM-1 fix8: the same terms the affordances publish
        this.Monitor.Log($"arti_command {request.Primitive} ADMIT request_id={request.RequestId} receiver={receiver} type={facts.MenuType} "
            + $"page={facts.PageType ?? "-"} context={context} button={button} snapped={facts.SnappedId?.ToString() ?? "-"} "
            + $"refuse={refuse ?? "-"}", LogLevel.Info);
        if (refuse is not null)
        {
            this.RejectUiPad(request, pin, refuse);
            return;
        }
        IClickableMenu top = (facts.GenericMenu ? GenericMenuSurface : Game1.activeClickableMenu)!;
        if (!nav && plan!.Steps.Any(s => s.Kind == UiPadStepKind.CharCustom))
        {
            this.ExecuteCharCustom(request, facts, plan, top, receiver!); // Live7 (CharCustomExecutor)
            return;
        }
        if (!nav && plan!.Steps.Any(s => s.Kind == UiPadStepKind.AnimalClick))
        {
            this.ExecutePurchaseAnimals(request, facts, plan, receiver!);
            return;
        }
        if (!nav && plan!.IsG3)
        {
            this.ExecuteUiG3(request, facts, plan, top, receiver!); // WS-G3 direct calls (UiG3Executor)
            return;
        }
        if (!nav && plan!.IsUiKey)
        {
            this.StartUiPadViaUiKey(request, pin, facts, plan, top, receiver!);
            return;
        }
        var port = new UiPadGamePort(this, top);
        UiPadVerdict verdict;
        try
        {
            verdict = nav ? UiPadExecutor.Nav(port, facts, UiPadTable.DirectionIndex(navTarget!.Dir))
                : UiPadExecutor.Press(port, facts, plan!);
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"ui_pad executor fault: {ex.GetType().Name}: {ex.Message}", LogLevel.Error);
            verdict = new UiPadVerdict(nav ? UiPadVerdict.NavUncertain : UiPadVerdict.PressUncertain, false, null,
                Array.Empty<string>(), "executor_fault:" + ex.GetType().Name, null, null, null);
        }
        if (this.uiMotor is { } pending)
        {
            pending.Request = request;
            if (verdict.Uncertain) { pending.Fault = verdict.Reason; this.FinishGenericUiFrame(); }
            return; // receipt belongs to real updates, including the release frame
        }
        this.FinishUiPad(request, verdict, receiver!);
    }

    /// <summary>ui_press on a v1 ui_key context: ui_key's own target, readiness, call and async settle.</summary>
    void StartUiPadViaUiKey(MenuStepCommandRequest request, CommandInboxIdentity pin, UiPadFacts facts, UiPadPlan plan,
        IClickableMenu original, string receiver)
    {
        UiPadStep step = plan.Steps[0];
        UiKeyState state = facts.UiKey!;
        int? option = step.UiKeyKey == UiKeyTable.Answer ? facts.SnappedId : null;
        if (facts.UiKey?.QuestionKey is "TV" or "Blacksmith" or "Marnie" or UiKeyTable.GiftQuestionKey && step.ChoiceIndex is int channel)
            option = channel; // only the pinned TV read-channel plan carries an explicit choice
        var target = new UiKeyTarget(receiver, state.MenuType, step.UiKeyKind!, step.UiKeyKey!, state.TokenFor(receiver, step.UiKeyKey!), option);
        UiKeyProbe? before;
        try { before = this.ProbeUiKey(original, state, target, pin.PlayerId); }
        catch { before = null; }
        if (before is null)
        {
            this.RejectUiPad(request, pin, "UiPadTargetChanged");
            return;
        }
        var input = new UiKeyInput();
        string? previous = receiver;
        this.uiKeyTransition = (next, ok) =>
        {
            string? nextToken = this.ReceiverTokenOf(CurrentInteractiveMenu(next));
            input.ObserveReceiverTransition(previous, nextToken);
            previous = nextToken;
        };
        UiKeyVerdict? verdict;
        try
        {
            verdict = input.Start(target, state, receiver, true, before,
                () => this.InvokeUiKey(original, target, state),
                () => this.ProbeUiKey(original, state, target, pin.PlayerId));
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"ui_pad ui_key executor fault: {ex.GetType().Name}", LogLevel.Error);
            verdict = input.Invoked
                ? new UiKeyVerdict(UiKeyVerdict.OutcomeUncertain, false, null, 1, false, "UnverifiedReceiverTransition",
                    null, null, null, "executor_fault")
                : new UiKeyVerdict(UiKeyVerdict.OutcomeRejected, false, null, 0, false, "NotInvoked", null, null, null, "UiKeyUnsupported");
        }
        if (verdict is { Rejected: true })
        {
            this.uiKeyTransition = null;
            this.RejectUiPad(request, pin, verdict.Reason == "UiKeyTargetChanged" ? "UiPadTargetChanged" : UiPadTable.Denied + ":" + verdict.Reason);
            return;
        }
        var settle = new UiPadSettleState
        {
            Request = request, Input = input, Original = original, Target = target, State = state,
            Clock = Stopwatch.StartNew(), PlayerId = pin.PlayerId, Step = step.ToString(),
        };
        if (verdict is not null)
        {
            this.uiKeyTransition = null;
            this.FinishUiPad(request, UiPadExecutor.FromUiKey(verdict, settle.Step), receiver);
            return;
        }
        this.uiPadSettle = settle; // inbox stays inflight; TickUiPadSettle observes once per tick
    }

    /// <summary>Every UpdateTicked (both branches), before TickCommandInbox. Monotonic deadline (ui_key's).</summary>
    void TickUiPadSettle()
    {
        if (this.uiPadSettle is not { } settle)
        {
            if (this.uiPadUncertainReceiver is { } latched
                && this.ReceiverTokenOf(CurrentInteractiveMenu(Game1.activeClickableMenu)) != latched)
            {
                this.Monitor.Log("ui_pad uncertain latch cleared (receiver changed)", LogLevel.Info);
                this.uiPadUncertainReceiver = null;
            }
            else if (this.uiPadUncertainReceiver is { } stable && this.uiWatchdog.PadUncertain.Due(!this.UiMotorUnsafe
                    && this.uiMotor is null && this.uiKeySettle is null && this.commandInbox.ExecutionSlot.ActiveAttempt is null, NowUnixMs())
                && this.uiWatchdog.PadUncertain.Resolved(stable, UiPadToken.StateDigest(this.ReadUiPadFacts()), NowUnixMs()))
            {
                this.Monitor.Log("ui_pad uncertain latch cleared (re-observed stable, input released)", LogLevel.Info);
                this.uiPadUncertainReceiver = null;
            }
            return;
        }
        if (!this.commandInbox.HasInflight || this.commandInbox.InflightRequestId != settle.Request.RequestId)
        {
            this.Monitor.Log($"ui_pad settle abandoned (inbox slot changed) request_id={settle.Request.RequestId}", LogLevel.Warn);
            this.uiPadSettle = null;
            this.uiKeyTransition = null;
            this.uiPadUncertainReceiver = settle.Target.ReceiverToken;
            return;
        }
        if (Game1.paused || Game1.HostPaused)
        {
            settle.Clock.Stop();
            return;
        }
        settle.Clock.Start();
        UiKeyProbe? probe;
        try { probe = this.ProbeUiKey(settle.Original, settle.State, settle.Target, settle.PlayerId); }
        catch { probe = null; }
        UiKeyVerdict? verdict = settle.Input.Observe(probe, settle.Clock.ElapsedMilliseconds);
        if (verdict is null)
            return;
        this.uiPadSettle = null;
        this.uiKeyTransition = null;
        this.FinishUiPad(settle.Request, UiPadExecutor.FromUiKey(verdict, settle.Step), settle.Target.ReceiverToken);
    }

    void FinishUiPad(MenuStepCommandRequest request, UiPadVerdict verdict, string receiver)
    {
        if (verdict.Uncertain)
            this.uiPadUncertainReceiver = receiver;
        long? generation = null;
        try
        {
            if (this.config.Enabled && Context.IsWorldReady)
                this.AdvanceUiContextNative(this.screens.Value);
            generation = this.CurrentCommandIdentity().ContextGeneration;
        }
        catch (Exception) { generation = null; }
        this.Monitor.Log($"arti_command {request.Primitive} VERDICT request_id={request.RequestId} outcome={verdict.Outcome} "
            + $"effect={verdict.Effect ?? "-"} reason={verdict.Reason} calls=[{string.Join(", ", verdict.Calls)}] "
            + $"snapped_after={verdict.SnappedAfter?.ToString() ?? "-"} result_menu={verdict.ResultMenuType ?? "-"}",
            verdict.Uncertain ? LogLevel.Warn : LogLevel.Info);
        this.WriteCommandResult(this.commandInbox.CompleteUiPad(verdict, this.CurrentCommandIdentity(), NowUnixMs(), generation));
    }
}
