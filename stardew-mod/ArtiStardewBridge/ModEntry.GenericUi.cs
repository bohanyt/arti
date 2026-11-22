using System.Collections;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.RegularExpressions;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    sealed record GenericWidget(int Id, string Path, ClickableComponent? Component, TextBox? TextBox,
        Rectangle Bounds, string Label, string Kind, Item? Item, IClickableMenu Dispatch, bool? AcceptsItem = null,
        InventoryMenu? Inventory = null);
    sealed class GenericGraph
    {
        public IClickableMenu Root = null!;
        public IClickableMenu Dispatch = null!;
        public List<GenericWidget> Widgets = new();
        public List<string> Text = new();
        public List<string> State = new();
        public List<Dictionary<string, object>> Items = new();
        public bool UnclaimedItems;
        public bool Complete = true;
        public Dictionary<string, object>? Crafting; // PM-1 fix13b: the open CraftingPage's recipe facts
    }
    static IReadOnlyList<FieldInfo> GenericFields(Type type) => GenericUiInspection.Fields(type);
    static bool GenericWidgetVisible(GenericWidget w) =>
        (w.Component is not ClickableTextureComponent || GenericUiRenderedText.ControlVisible(w.Component) == true)
        && (w.Inventory is null || GenericUiRenderedText.ControlVisible(w.Inventory) == true)
        && GenericUiInspection.NativeVisible(w.Dispatch,
        w.Path.Split('.').Last(), w.Component?.visible ?? true,
        // These native show* predicates expose the same draw/click gate. No
        // unknown assembly, ready-check, cleanup or sync method is invoked.
        method => method.DeclaringType == typeof(ItemListMenu)
            && method.Name is "showForwardButton" or "showBackButton");
    static GenericGraph GenericInspect(IClickableMenu root)
    {
        var graph = new GenericGraph { Root = root, Dispatch = root };
        var childSeen = new HashSet<IClickableMenu>(ReferenceEqualityComparer.Instance);
        while (graph.Dispatch.GetChildMenu() is { } child && childSeen.Add(graph.Dispatch)) graph.Dispatch = child;
        IClickableMenu owner = CurrentInteractiveMenu(graph.Dispatch) ?? graph.Dispatch;
        var seen = new HashSet<object>(ReferenceEqualityComparer.Instance);
        var components = new List<(ClickableComponent Component, string Path)>();
        var items = new Dictionary<ClickableComponent, Item?>(ReferenceEqualityComparer.Instance);
        var accepts = new Dictionary<ClickableComponent, bool?>(ReferenceEqualityComparer.Instance);
        var inventories = new Dictionary<ClickableComponent, InventoryMenu>(ReferenceEqualityComparer.Instance);
        bool cursorHeld = Game1.player.CursorSlotItem is not null || GenericFields(graph.Dispatch.GetType())
            .Any(f => f.Name == "heldItem" && f.GetValue(graph.Dispatch) is Item) || MenuHeldItem.Of(graph.Dispatch) is not null;
        var boxes = new List<(TextBox Box, string Path)>();
        var roots = new List<(string, object)> { ("menu", graph.Dispatch) };
        if (!ReferenceEquals(owner, graph.Dispatch)) roots.Add(("page", owner));
        graph.Complete = GenericUiInspection.Walk(roots,
            value => value is Response || value is InventoryMenu || value is IClickableMenu menu
                && (ReferenceEquals(menu, graph.Dispatch) || ReferenceEquals(menu, owner))
                || value is not IClickableMenu && value is not ClickableComponent
                    && value.GetType().Namespace == typeof(IClickableMenu).Namespace,
            (value, path) =>
            {
                if (value is string text && !string.IsNullOrWhiteSpace(text) && graph.Text.Count < 128)
                    graph.Text.Add(path + ": " + text);
                if (value is ClickableComponent component) components.Add((component, path));
                if (value is TextBox box) boxes.Add((box, path));
                if (value is Item nativeItem)
                    graph.Items.Add(new Dictionary<string, object> { ["path"] = path,
                        ["item"] = nativeItem.QualifiedItemId, ["stack"] = nativeItem.Stack,
                        ["quality"] = nativeItem.Quality });
                if (value is InventoryMenu inventory)
                {
                    graph.UnclaimedItems |= !ReferenceEquals(inventory.actualInventory, Game1.player.Items)
                        && inventory.actualInventory.Any(item => item is not null);
                    for (int i = 0; i < inventory.inventory.Count; i++)
                    {
                        Item? slotItem = i < inventory.actualInventory.Count ? inventory.actualInventory[i] : null;
                        items[inventory.inventory[i]] = slotItem;
                        inventories[inventory.inventory[i]] = inventory;
                        // The native highlight predicate is also the native click eligibility check.
                        accepts[inventory.inventory[i]] = cursorHeld ? null : slotItem is null ? true : inventory.highlightMethod is null ? true
                            : inventory.highlightMethod.Method.DeclaringType?.Assembly == typeof(Game1).Assembly
                                ? inventory.highlightMethod(slotItem) : null; // never invoke an unknown mod callback while inspecting
                    }
                }
                if (value is int or long or bool or float or double)
                    graph.State.Add(path + "=" + JsonSerializer.Serialize(value));
            });
        graph.Text.AddRange(GenericUiRenderedText.Current("menu", root));
        int id = 0;
        var unnamed = new HashSet<int>();
        foreach (var (component, path) in components)
        {
            Item? item = items.TryGetValue(component, out Item? actual) ? actual : component.item;
            string strings = string.Join(" | ", new[] { component.ScreenReaderText, component.label,
                (component as ClickableTextureComponent)?.hoverText }.Where(t => !string.IsNullOrWhiteSpace(t)).Distinct());
            // Reflection visits allClickableComponents before inherited named fields; recover
            // the real field provenance so OK/close semantics survive that aliasing.
            string canonicalPath = GenericFields(graph.Dispatch.GetType())
                .FirstOrDefault(f => typeof(ClickableComponent).IsAssignableFrom(f.FieldType)
                    && ReferenceEquals(f.GetValue(graph.Dispatch), component)) is { } named ? "menu." + named.Name : path;
            string fieldName = canonicalPath.Split('.').Last();
            string description = string.Join(" ", graph.Text.Where(t => t.Contains(fieldName + "Description", StringComparison.OrdinalIgnoreCase)));
            string label = item?.DisplayName ?? (strings.Length > 0 ? strings : GenericUiPartText.Unnamed);
            if (description.Length > 0) label += " " + description;
            if (item is null && strings.Length == 0 && description.Length == 0) unnamed.Add(id);
            string kind = items.ContainsKey(component) ? "slot" : ReferenceEquals(component, root.upperRightCloseButton)
                || Regex.IsMatch(fieldName, "^(cancelButton|closeButton|upperRightCloseButton)$", RegexOptions.IgnoreCase)
                    ? "close" : "button";
            graph.Widgets.Add(new GenericWidget(id++, canonicalPath, component, null, component.bounds, label, kind, item, graph.Dispatch,
                accepts.TryGetValue(component, out bool? accepted) ? accepted : null, inventories.GetValueOrDefault(component)));
        }
        // fix137A (ADR 237 A): an unnamed part the game drew this frame takes the words the game drew on it
        // (GenericUiPartText), never a field name. Draw-witnessed textures only: a plain ClickableComponent's visible
        // flag does not say the game drew it (a QuestLog detail page keeps its list buttons). Field reads only; no game
        // call, no new ModEntry member or lambda (the IL policy pins generated caller names).
        PlacedText[] drawnText = unnamed.Count > 0 ? GenericUiRenderedText.Placed(root) : Array.Empty<PlacedText>();
        if (drawnText.Length > 0)
        {
            var parts = new List<PartBox>();
            foreach (GenericWidget w in graph.Widgets)
            {
                bool drawn = w.Component is ClickableTextureComponent texture && GenericUiRenderedText.ControlVisible(texture) == true;
                if (w.Component is null || !(drawn || w.Component is not ClickableTextureComponent && w.Component.visible)) continue;
                parts.Add(new PartBox(w.Id, w.Bounds.X, w.Bounds.Y, w.Bounds.Width, w.Bounds.Height,
                    drawn && unnamed.Contains(w.Id) && w.Kind != "slot"));
            }
            Dictionary<int, string> drawnWords = GenericUiPartText.Describe(parts, drawnText);
            for (int i = 0; i < graph.Widgets.Count; i++)
                if (drawnWords.TryGetValue(graph.Widgets[i].Id, out string? shown))
                    graph.Widgets[i] = graph.Widgets[i] with { Label = shown };
        }
        foreach (var (box, path) in boxes)
        {
            graph.Widgets.Add(new GenericWidget(id++, path, null, box, new Rectangle(box.X, box.Y, box.Width, box.Height),
                path.Split('.').Last() + ": " + box.Text, "text_field", null, graph.Dispatch));
            graph.Text.Add(path + ": " + box.Text);
        }
        EnrichVendorWidgets(graph);
        AddMuseumSpot(graph); // fix54G: the museum floor placement step (no native component)
        // PM-1 fix13b: a recipe component carries no text; label it with the game's own recipe facts (CraftingFacts).
        if (NativeCraftingFacts.PageRecipes(graph.Dispatch) is { } crafting)
        {
            var visible = new List<(int Id, CraftRecipe Recipe)>();
            var drops = new List<int>();
            for (int i = 0; i < graph.Widgets.Count; i++)
            {
                GenericWidget w = graph.Widgets[i];
                if (crafting.HeldDropOutside is { } page && w.Kind != "slot" && !page.Contains(w.Bounds.Center))
                    drops.Add(w.Id);
                if (w.Component is null || !crafting.Recipes.TryGetValue(w.Component, out CraftRecipe? recipe))
                    continue;
                graph.Widgets[i] = w = w with { Label = CraftingFacts.Label(recipe), Kind = "recipe" };
                if (GenericWidgetVisible(w))
                    visible.Add((w.Id, recipe));
            }
            graph.Crafting = CraftingFacts.Page(visible, crafting.Cooking, crafting.Page, crafting.Pages, drops);
        }
        if (graph.Widgets.Count > 512) graph.Complete = false;
        return graph;
    }

    static GenericGraph GenericInspectHud()
    {
        var result = new GenericGraph();
        foreach (IClickableMenu surface in Game1.onScreenMenus)
        {
            GenericGraph graph = GenericInspect(surface);
            result.Complete &= graph.Complete;
            result.Text.AddRange(graph.Text);
            result.State.AddRange(graph.State);
            result.Items.AddRange(graph.Items);
            result.UnclaimedItems |= graph.UnclaimedItems;
            foreach (GenericWidget widget in graph.Widgets)
                result.Widgets.Add(widget with { Id = result.Widgets.Count });
        }
        result.Text.AddRange(GenericHudText());
        result.Text.AddRange(GenericUiRenderedText.Current("hud", null));
        if (result.Widgets.Count > 512) result.Complete = false;
        return result;
    }

    static Dictionary<string, object> GenericUiLedger()
    {
        var player = Game1.player;
        return new Dictionary<string, object>
        {
            ["money"] = player.Money, ["capacity"] = player.MaxItems,
            ["active_slot"] = player.CurrentToolIndex,
            ["inventory"] = player.Items.Select((item, slot) => new Dictionary<string, object>
            {
                ["slot"] = slot, ["item"] = item?.QualifiedItemId ?? "", ["stack"] = item?.Stack ?? 0,
                ["quality"] = item?.Quality ?? 0, ["name"] = item?.DisplayName ?? "",
                ["is_tool"] = item is StardewValley.Tool,
                ["quest_item"] = item is StardewValley.Object obj && obj.questItem.Value,
            }).ToArray(),
            // Same native per-player shipment ledger used by the container verifier; observation only.
            ["shipping_items"] = Game1.getFarm().getShippingBin(player).Where(item => item is not null)
                .GroupBy(item => (item.QualifiedItemId, item.Quality))
                .Select(group => new Dictionary<string, object> { ["item"] = group.Key.QualifiedItemId,
                    ["stack"] = group.Sum(item => item.Stack), ["quality"] = group.Key.Quality }).ToArray(),
            ["professions"] = player.professions.ToArray(),
            ["cursor_item"] = player.CursorSlotItem?.QualifiedItemId ?? "", ["cursor_stack"] = player.CursorSlotItem?.Stack ?? 0,
            ["minigame"] = Game1.currentMinigame?.GetType().Name ?? "",
            ["event_id"] = Game1.CurrentEvent?.id ?? "",
            ["event_command"] = Game1.CurrentEvent?.CurrentCommand ?? -1,
            ["event_player_control"] = Game1.CurrentEvent?.playerControlSequence == true,
            ["event_skippable"] = Game1.CurrentEvent is { skippable: true, skipped: false },
            ["fish_caught"] = player.CurrentTool is StardewValley.Tools.FishingRod { fishCaught: true },
            ["location"] = player.currentLocation?.NameOrUniqueName ?? "",
            ["using_tool"] = player.UsingTool, ["can_move"] = player.CanMove,
            ["is_saving"] = Game1.game1?.IsSaving == true,
            ["passout_readiness"] = new Dictionary<string, object> {
                ["menu_clear"] = Game1.activeClickableMenu is null, ["event_clear"] = !Game1.eventUp,
                ["can_move"] = player.CanMove, ["tool_clear"] = !player.UsingTool,
                ["location_request_clear"] = Game1.locationRequest is null },
            ["end_of_night_status"] = Game1.getOnlineFarmers().Select(f => new Dictionary<string, object> {
                ["player_id"] = f.UniqueMultiplayerID,
                ["status"] = Game1.player.team.endOfNightStatus.GetStatusText(f.UniqueMultiplayerID) ?? "" }).ToArray(),
        };
    }
    static string[] GenericHudText() => Game1.hudMessages.Select(message =>
        string.Join(" ", GenericFields(message.GetType()).Where(f => f.FieldType == typeof(string))
            .Select(f => f.GetValue(message) as string).Where(t => !string.IsNullOrWhiteSpace(t))))
        .Where(t => t.Length > 0).Select(t => UiPadTrim(t, 128)).Take(32).ToArray();

    UiPadFacts ReadGenericUiFacts(IClickableMenu? root, string? readyCheckName = null)
    {
        GenericGraph graph = root is null ? GenericInspectHud() : GenericInspect(root);
        // Cursor items are a generic field, including menus whose slot item differs from the player's cursor.
        Item? held = graph.Dispatch is null ? Game1.player.CursorSlotItem : GenericFields(graph.Dispatch.GetType())
            .Where(f => f.Name == "heldItem").Select(f => f.GetValue(graph.Dispatch) as Item)
            .FirstOrDefault(i => i is not null) ?? MenuHeldItem.Of(graph.Dispatch) // PM-1 fix10d
            ?? NativeCraftingFacts.PageHeld(graph.Dispatch) ?? Game1.player.CursorSlotItem; // PM-1 fix13b: a craft's output
        var values = GenericUiLedger();
        if (this.ReadJournal(graph) is { } journal) values["journal"] = journal;
        if (NativePersonaFacts.Container(graph.Dispatch) is { } container) values["menu_container"] = container;
        var shopCatalog = ReadGenericShop(graph);
        if (shopCatalog is not null) values["shop"] = shopCatalog.Wire();
        values["q12_backend_edges"] = this.config.Q12BackendEdgesEnabled;
        values["q12_state"] = Q12Capability.State;
        var receiverFacts = this.CaptureReceiverFacts();
        values["receiver_epoch"] = receiverFacts.Epoch;
        values["captured_tick"] = this.commandInbox.ExecutionSlot.ModTick;
        values["rev"] = new { frame = Game1.ticks };
        values["native"] = this.NativeObserverFacts();
        values["authority"] = new { holder = receiverFacts.Authority.Holder, since_frame = receiverFacts.Authority.SinceFrame };
        values["stack"] = receiverFacts.Stack.Select(r => new { receiver_id = r.ReceiverId, presence_id = r.PresenceId, menu_type = r.MenuType, depth = r.Depth }).ToArray();
        values["parked"] = receiverFacts.Parked.Select(r => new { receiver_id = r.ReceiverId, presence_id = r.PresenceId, menu_type = r.MenuType, holder = r.Holder }).ToArray();
        values["menu_items"] = graph.Items.ToArray();
        if (graph.Crafting is { } craftingPage) values[CraftingFacts.PageKey] = craftingPage;
        var safety = this.ReadGenericUiSafety(graph, held);
        var grant = GrantOf(graph);
        values["ui_safety"] = new Dictionary<string, object> { ["state"] = GenericUiSafety.State(safety),
            ["unclaimed_items"] = safety.UnclaimedItems, ["advance_ready"] = safety.Ready,
            ["night_progression"] = safety.NightProgression, ["advance_id"] = safety.AdvanceId,
            ["safety_advance_after_ms"] = this.NativeProgressionEntry(graph.Dispatch) is { } e ? ProgressionClient.Dwell(e, graph.Text.Sum(t => t.Length)) : 0 };
        var keyFacts = graph.Dispatch is null ? null : this.ReadUiKeyFacts(graph.Dispatch);
        var dialogue = keyFacts?.Dialogue;
        string? dialogueGate = dialogue is null ? null : UiG3Policy.DialogueReadiness(dialogue).Reason;
        values["ui_input_gate"] = dialogue is not null && dialogueGate is not null ? dialogueGate
            : GenericUiSafety.InputGate(Game1.isWarping, Game1.locationRequest is not null,
                (Game1.globalFade || Game1.fadeToBlack) && dialogue is null, safety) ?? "";
        values["ui_prose"] = dialogue is { IsQuestion: false };
        // The end-of-night summary's own exit is its observed OK (the game ignores B there; PM-1 a9 night 11).
        values["ui_close_blocked"] = dialogue is { IsQuestion: false } ? "advance_only_use_confirm"
            : GenericUiSafety.State(safety) == "NIGHT_SUMMARY" ? "night_summary_use_ok"
            : grant is not null ? GenericUiGrant.CloseGate : "";
        // PM-1 fix14d (a21 d1:J245-1174): the same predicate PrepareGenericUiFrame refuses a close with (NeedsChoice),
        // published so the arm never offers a B the motor will refuse. A question's answers / Escape-hotkey decline stay.
        values["ui_required_choice"] = GenericRequiredChoice(graph);
        var readyDialog = graph.Dispatch as ReadyCheckDialog;
        string? checkName = readyDialog?.checkName ?? readyCheckName;
        if (checkName is not null)
        {
            bool cancelable = readyDialog?.isCancelable() ?? Game1.netReady.IsReadyCheckCancelable(checkName);
            var readiness = this.NativeReadyFacts(checkName, cancelable);
            values["ready_check"] = readiness;
            graph.Text.Add("ready_check: " + JsonSerializer.Serialize(readiness));
            if (readyDialog is not null)
                graph.Widgets = graph.Widgets.Select(w => ReferenceEquals(w.Component, readyDialog.cancelButton)
                    ? w with { Label = "Cancel readiness: " + checkName + " (closes waiting dialog)" }
                    : ReferenceEquals(w.Component, readyDialog.okButton)
                    ? w with { Label = "Confirm (hidden; native readiness completes automatically)" } : w).ToList();
        }
        values["ui_held_item"] = held?.QualifiedItemId ?? "";
        values["ui_held_stack"] = held?.Stack ?? 0;
        string cursorReceiver = root is null ? "rcv:hud:" + this.runtimeInstanceId : this.ReceiverTokenOf(CurrentInteractiveMenu(root)) ?? "";
        var cursorSettle = this.CaptureCursorItemSettleCore(graph, cursorReceiver);
        values["cursor_settle"] = CursorItemSettle.Wire(cursorSettle);
        values["ui_slots"] = graph.Widgets.Where(w => w.Kind == "slot").Select(w => new Dictionary<string, object>
        {
            ["id"] = w.Id, ["field_path"] = w.Path, ["item"] = w.Item?.QualifiedItemId ?? "",
            ["stack"] = w.Item?.Stack ?? 0, ["quality"] = w.Item?.Quality ?? 0,
        }).ToArray();
        var components = graph.Widgets.Select(w => new UiPadComponent(w.Id, CursorWidgetKind(w), UiPadTrim(w.Label),
            w.Item?.QualifiedItemId, w.Item?.Stack ?? 0, w.Item?.Quality ?? 0,
            Visible: GenericWidgetVisible(w), X: w.Bounds.X, Y: w.Bounds.Y, W: w.Bounds.Width, H: w.Bounds.Height,
            HoverText: (w.Component as ClickableTextureComponent)?.hoverText, FieldPath: w.Path, TextValue: w.TextBox?.Text,
            AcceptsItem: w.AcceptsItem, ProtectedItem: VendorProtected(w))).ToArray();
        if (grant is not null && GrantOf(graph, components, values["ui_held_item"] as string) is { } published)
            ((Dictionary<string, object>)values["ui_safety"])["grant"] = GenericUiGrant.Wire(published);
        string receiver = root is null ? "rcv:hud:" + this.runtimeInstanceId : this.ReceiverTokenOf(CurrentInteractiveMenu(root)) ?? "";
        string identityValues = GenericUiIdentity.ValuesDigest(values);
        string anchor = identityValues + JsonSerializer.Serialize(components)
            + string.Join("|", graph.Text) + string.Join("|", graph.State.Where(s => s.EndsWith("=true") || s.EndsWith("=false")));
        var pad = Game1.input.GetGamePadState();
        var mouse = Game1.input.GetMouseState();
        bool inputFree = !this.InputInflight && Game1.oldKBState.GetPressedKeys().Length == 0
                && Game1.input.GetKeyboardState().GetPressedKeys().Length == 0
                && mouse.LeftButton == ButtonState.Released && mouse.RightButton == ButtonState.Released
                && Game1.oldMouseState.LeftButton == ButtonState.Released && Game1.oldMouseState.RightButton == ButtonState.Released
                && mouse.ScrollWheelValue == Game1.oldMouseState.ScrollWheelValue
                && Enum.GetValues<Buttons>().All(button => pad.IsButtonUp(button));
        var state = this.genericUiIdentity.Sample(this.GenericUiWatchdogBinding(receiver), anchor, graph.State,
            Game1.ticks, inputFree);
        if (GenericUiSafety.State(safety) == "NIGHT_SUMMARY") state = GenericUiIdentity.InfoOnly(state);
        // Readback-only probe (never in a digest): lets a run tell anchor drift from blocked learning.
        var probe = new Dictionary<string, object> { ["anchor"] = UiKeyTargetToken.Hash(anchor)[..12], ["input_free"] = inputFree,
            ["input_inflight"] = this.InputInflight, ["learned"] = state.Ambient.Length };
        string ledger = identityValues + string.Join("|", graph.Text) + string.Join("|", state.Semantic)
            + (graph.Dispatch is null ? "hud" : RuntimeHelpers.GetHashCode(graph.Dispatch).ToString());
        return new UiPadFacts(root?.GetType().Name ?? "HUD", MenuOpen: root is not null,
            ReceiverToken: root is null ? "rcv:hud:" + this.runtimeInstanceId + ":" + (GenericInputOwner is { } inputOwner
                ? RuntimeHelpers.GetHashCode(inputOwner) : 0) : this.ReceiverTokenOf(CurrentInteractiveMenu(root)),
            PageType: graph.Dispatch?.GetType().Name, IsActive: graph.Complete && ((graph.Dispatch?.IsActive() ?? true)
                || root is not null && ReferenceEquals(root, GenericMenuSurface) && !ReferenceEquals(root, Game1.activeClickableMenu)),
            ReadyToClose: null, Components: components, HeldItemId: held?.QualifiedItemId, HeldStack: held?.Stack ?? 0,
            UiKey: dialogue is { IsQuestion: true, Responses.Count: > 0 } ? UiKeyClassifier.Classify(keyFacts!, genericUi: true) : null,
            Text: UiPadTrim(string.Join("\n", graph.Text.Select(FactNames.ScreenText).Where(t => t.Length > 0)), UiPadTable.MaxBodyText),
            GenericMenu: true, GenericLedger: UiKeyTargetToken.Hash(ledger), GenericHud: root is null, GenericValues: values, GenericState: state.Semantic,
            GenericAmbientState: state.Ambient, GenericIdentityProbe: probe,
            CursorSettle: cursorSettle, ShopCatalog: shopCatalog,
            // ADR 246 review B1: the blind-ship rule reads this on the generic path too (it was only set by the v1 reader).
            ShippingBin: IsShippingBinMenu(graph.Dispatch, out _, out _));
    }

    readonly GenericUiIdentity genericUiIdentity = new();

    /// <summary>PM-1 fix11b #2: the menu's own native essential flag plus its non-bag item slots (GenericUiGrant).
    /// Item ids come from the already-published component rows; presence alone gates the exit.</summary>
    static GenericUiGrantFacts? GrantOf(GenericGraph graph, IReadOnlyList<UiPadComponent>? published = null,
        string? heldId = null)
    {
        if (graph.Dispatch is not { } menu) return null;
        var fields = GenericFields(menu.GetType());
        bool Flag(string name) => fields.Any(f => f.Name == name && f.GetValue(menu) is true);
        var bag = Game1.player.Items;
        var ids = published?.ToDictionary(c => c.Id, c => c.ItemId);
        // fix12a #2: the menu's own cursor item (MenuWithInventory.heldItem), the same field ReadGenericUiFacts reads;
        // its id is the one already published as ui_held_item (presence alone gates the exit).
        Item? held = fields.Where(f => f.Name == "heldItem").Select(f => f.GetValue(menu) as Item)
            .FirstOrDefault(i => i is not null) ?? MenuHeldItem.Of(menu);
        return GenericUiGrant.Facts(Flag("essential"), Flag("superEssential"),
            graph.Widgets.Where(w => w.Kind == "slot" && w.Inventory is not null).Select(w => (w.Id,
                w.Item is null ? null : ids?.GetValueOrDefault(w.Id) ?? "unpublished", w.Item?.Stack ?? 0,
                !ReferenceEquals(w.Inventory!.actualInventory, bag))),
            Game1.player.freeSpotsInInventory(),
            held is null ? null : (string.IsNullOrEmpty(heldId) ? "unpublished" : heldId, held.Stack));
    }

    GenericUiSafetyFacts ReadGenericUiSafety(GenericGraph graph, Item? held)
    {
        var menu = graph.Dispatch;
        if (menu is null) return new GenericUiSafetyFacts(MenuOpen: false, CursorItem: held is not null);
        var fields = GenericFields(menu.GetType());
        bool barrier = Game1.game1.IsSaving || Game1.player.team.GetOutgoingProposal() is not null
            || fields.Any(f => f.Name is "hasDrawn" or "checkName")
            || fields.Any(f => typeof(IClickableMenu).IsAssignableFrom(f.FieldType)
                && f.Name.Contains("save", StringComparison.OrdinalIgnoreCase) && f.GetValue(menu) is not null)
            || fields.Any(f => f.FieldType.Name.Contains("Proposal", StringComparison.Ordinal));
        bool choice = GenericRequiredChoice(graph) || graph.Widgets.Any(w => w.TextBox is not null)
            || fields.Any(f => f.GetValue(menu) is IEnumerable<Response> rs && rs.Any());
        choice |= fields.Any(f => f.Name == "onConfirm" && f.GetValue(menu) is Delegate);
        bool loot = GenericUiSafety.HasUnclaimedItems(graph.UnclaimedItems,
            fields.Any(f => (f.Name is "essential" or "superEssential") && f.GetValue(menu) is true),
            graph.Items.Count > 0, graph.Widgets.Select(w => (w.Kind, w.Item is not null)));
        var buttons = graph.Widgets.Where(w => GenericWidgetVisible(w) && w.Kind == "button"
            && w.Bounds.Width > 0 && w.Bounds.Height > 0).ToArray();
        bool singleOk = buttons.Length == 1 && buttons[0].Path.EndsWith(".okButton", StringComparison.Ordinal)
            && !graph.Widgets.Any(w => w.Kind == "close" && GenericWidgetVisible(w));
        // Reuse the existing native dialogue observation, including typing and safety gates.
        var dialogue = this.ReadUiKeyFacts(menu).Dialogue;
        bool prose = dialogue is { IsQuestion: false };
        bool ready = prose ? UiG3Policy.DialogueReadiness(dialogue!).Item1 : GenericUiSafety.TimersReady(graph.State);
        ready &= !Game1.isWarping && Game1.locationRequest is null
            && (prose || this.nightEnding || !Game1.globalFade && !Game1.fadeToBlack);
        var facts = new GenericUiSafetyFacts(barrier, choice, graph.Widgets.Any(w => w.TextBox is not null),
            loot, held is not null, !choice && !loot && (prose || singleOk), ready,
            singleOk ? buttons[0].Id : -1, graph.Widgets.Any(w => w.Kind == "close" && GenericWidgetVisible(w)));
        return GenericUiSafety.NightProgression(facts, this.nightEnding && this.nightLedger?.Pending.plan is not null,
            graph.Widgets.Select(w => new UiPadComponent(w.Id, w.Kind, w.Label,
                Visible: GenericWidgetVisible(w), X: w.Bounds.X, Y: w.Bounds.Y,
                W: w.Bounds.Width, H: w.Bounds.Height, FieldPath: w.Path)).ToArray());
    }

    UiPadFacts ReadGenericUiReadback(UiPadFacts before) =>
        this.ReadGenericUiFacts(GenericMenuSurface, ReadyCheckObservation.CheckName(before));

    private sealed partial class UiPadGamePort
    {
        public void ApplyGeneric(string operation, int componentId, string? value)
        {
            AssertWarpGuarded();
            GenericGraph graph = this.top is null ? GenericInspectHud() : GenericInspect(this.top);
            GenericWidget? widget = graph.Widgets.SingleOrDefault(w => w.Id == componentId);
            if (widget is not null && VendorProtected(widget))
                throw new InvalidOperationException("item_policy:tool_or_quest");
            if (operation == "settle")
            {
                string receiver = this.top is null ? "rcv:hud:" + this.mod.runtimeInstanceId
                    : this.mod.ReceiverTokenOf(CurrentInteractiveMenu(this.top)) ?? "";
                var cursor = this.mod.CaptureCursorItemSettleCore(graph, receiver);
                if (CursorItemSettle.PrepareRefusal(cursor, componentId) is { } refused)
                    throw new InvalidOperationException(refused);
            }
            if (widget is not null && CursorWidgetKind(widget) is "trash" or "drop")
            {
                Item? cursor = this.mod.ReadCursorItem(graph, out bool known);
                if (!known || cursor is null
                    || CursorItemSettle.HeldDisposeRefusal(this.mod.CursorFact(cursor), CursorWidgetKind(widget)) is { })
                    throw new InvalidOperationException("item_policy:tool_or_quest_or_unreadable");
            }
            if (operation == "type")
            {
                TextBox box = widget?.TextBox ?? throw new InvalidOperationException("text field unavailable");
                var subscriber = Game1.keyboardDispatcher.Subscriber;
                bool selected = box.Selected;
                this.mod.commandInbox.ExecutionSlot.Bracket(() => {
                try
                {
                    Game1.keyboardDispatcher.Subscriber = box;
                    box.Selected = true;
                    box.Text = "";
                    box.RecieveTextInput(value ?? "");
                    if (box.Text != value) throw new InvalidOperationException("text readback mismatch");
                }
                finally {
                    Game1.keyboardDispatcher.Subscriber = subscriber; box.Selected = selected;
                    this.mod.Helper.Reflection.GetField<bool>(box, "_showKeyboard").SetValue(false);
                }
                });
                return;
            }
            this.mod.StartGenericUiFrame(operation, componentId, value);
        }
    }
}
