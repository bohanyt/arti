using System.Collections;
using System.Text.Json;
using ArtiStardewBridge.Protocol;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Tools;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    CursorItemFact CursorFact(Item item) => new(this.genericWorldInstances.Single(item), item.QualifiedItemId,
        item.Quality, item.Stack, item is Tool, item is StardewValley.Object obj && obj.questItem.Value, item.canBeTrashed(), item.DisplayName);

    static string CursorWidgetKind(GenericWidget widget) => widget.Kind == "slot" ? "slot"
        : widget.Path.Contains("trash", StringComparison.OrdinalIgnoreCase) ? "trash"
        : widget.Path.Contains("dropItem", StringComparison.OrdinalIgnoreCase) ? "drop" : widget.Kind;

    Item? ReadCursorItem(GenericGraph graph, out bool complete)
    {
        complete = graph.Complete;
        var items = new HashSet<Item>(ReferenceEqualityComparer.Instance);
        if (Game1.player.CursorSlotItem is { } cursor) items.Add(cursor);
        foreach (var owner in new[] { graph.Root, graph.Dispatch, graph.Dispatch is null ? null : CurrentInteractiveMenu(graph.Dispatch) }
                     .Where(m => m is not null).Distinct(ReferenceEqualityComparer.Instance))
        {
            foreach (var field in GenericFields(owner!.GetType()).Where(f => f.Name == "heldItem"))
            {
                object? value = field.GetValue(owner);
                if (value is Item item) items.Add(item);
                else if (value is not null) complete = false;
            }
            if (MenuHeldItem.Of(owner) is { } held) items.Add(held); // PM-1 fix10d: the 1.6 property
        }
        if (items.Count > 1) complete = false;
        return items.Count == 1 ? items.Single() : null;
    }

    CursorItemSettleSnapshot CaptureCursorItemSettleCore(GenericGraph graph, string receiver)
    {
        var player = Game1.player;
        Item? cursor = this.ReadCursorItem(graph, out bool complete);
        var owned = new Dictionary<string, int>(StringComparer.Ordinal);
        var inventoryCounts = new Dictionary<string, int>(StringComparer.Ordinal);
        var ground = new Dictionary<string, int>(StringComparer.Ordinal);
        var destinations = new List<CursorItemDestination>();
        var groups = new Dictionary<object, string>(ReferenceEqualityComparer.Instance);
        var slots = new Dictionary<ClickableComponent, (object Inventory, int Slot, Item? Item, InventoryMenu Menu)>(ReferenceEqualityComparer.Instance);
        var locked = new HashSet<ClickableComponent>(ReferenceEqualityComparer.Instance);
        var inert = new List<int>();
        string Inventory(object inventory) => this.genericWorldInstances.Single(inventory);
        void Add(Dictionary<string, int> ledger, string key, int count) => ledger[key] = ledger.GetValueOrDefault(key) + count;
        void ReadInventory(object identity, IEnumerable<Item?> items)
        {
            if (groups.ContainsKey(identity)) return;
            string id = Inventory(identity);
            groups.Add(identity, id);
            foreach (Item? item in items)
                if (item is not null)
                {
                    var fact = this.CursorFact(item);
                    Add(owned, fact.Key, fact.Stack);
                    Add(inventoryCounts, id + ":" + fact.Key, fact.Stack);
                }
        }
        ReadInventory(player.Items, player.Items);
        if (graph.Dispatch is not null)
        {
            IClickableMenu owner = CurrentInteractiveMenu(graph.Dispatch) ?? graph.Dispatch;
            var roots = new List<(string, object)> { ("menu", graph.Dispatch) };
            if (!ReferenceEquals(owner, graph.Dispatch)) roots.Add(("page", owner));
            complete &= GenericUiInspection.Walk(roots,
                value => value is InventoryMenu || value is IClickableMenu menu
                    && (ReferenceEquals(menu, graph.Dispatch) || ReferenceEquals(menu, owner))
                    || value is not IClickableMenu && value is not ClickableComponent
                        && value.GetType().Namespace == typeof(IClickableMenu).Namespace,
                (value, path) => {
                    if (value is not InventoryMenu inventory) return;
                    ReadInventory(inventory.actualInventory, inventory.actualInventory);
                    for (int i = 0; i < inventory.inventory.Count; i++)
                        if (i < inventory.actualInventory.Count)
                            slots[inventory.inventory[i]] = (inventory.actualInventory, i, inventory.actualInventory[i], inventory);
                        else locked.Add(inventory.inventory[i]); // a greyed slot past capacity: leftClick skips it
                });
        }
        if (cursor is not null)
        {
            var held = this.CursorFact(cursor);
            Add(owned, held.Key, held.Stack);
            var widgets = new List<HeldWidget>();
            foreach (var widget in graph.Widgets.Where(w => w.Component is not null && GenericWidgetVisible(w)
                && w.Bounds.Width > 0 && w.Bounds.Height > 0))
            {
                if (locked.Contains(widget.Component!)) { widgets.Add(new(widget.Id, "slot", Slot: true, Locked: true)); continue; }
                if (!slots.TryGetValue(widget.Component!, out var cell)) { widgets.Add(new(widget.Id, CursorWidgetKind(widget))); continue; }
                bool stackable = cell.Item is not null && cell.Item.canStackWith(cursor);
                int room = cell.Item is null ? cursor.maximumStackSize()
                    : stackable ? Math.Max(0, cell.Item.maximumStackSize() - cell.Item.Stack) : 0;
                widgets.Add(new(widget.Id, "slot", Slot: true, Bag: ReferenceEquals(cell.Inventory, player.Items),
                    Item: cell.Item is null ? null : this.CursorFact(cell.Item), Stackable: stackable,
                    Highlighted: cell.Item is null ? true : HeldItemMenu.Highlighted(cell.Menu, cell.Item), Room: room,
                    Inventory: Inventory(cell.Inventory), Index: cell.Slot));
            }
            var (targets, inertSlots) = CursorItemSettle.Classify(held, HeldItemMenu.ContainerRoute(graph.Dispatch), widgets);
            inert.AddRange(inertSlots);
            foreach (var t in targets)
            {
                string token = UiKeyTargetToken.Hash(JsonSerializer.Serialize(new object?[] {
                    this.CurrentCommandIdentity(), receiver, held, t.Widget.Id, t.Kind, t.Widget.Inventory, t.Widget.Index,
                    t.Room, t.Widget.Item }));
                destinations.Add(new(t.Widget.Id, t.Kind, t.Widget.Inventory, t.Widget.Index, t.Room, token, t.Swap));
            }
        }
        int count = 0;
        foreach (var debris in player.currentLocation.debris)
        {
            if (++count > 256) { destinations.RemoveAll(d => d.Kind == "drop"); ground.Clear(); break; }
            if (debris.item is { } item)
            {
                Add(ground, this.genericWorldInstances.Single(item), item.Stack);
                Add(ground, "key:" + this.CursorFact(item).Key, item.Stack);
            }
        }
        string binding = JsonSerializer.Serialize(new object[] { this.runtimeInstanceId, this.CurrentCommandIdentity().SessionId,
            player.UniqueMultiplayerID, Game1.Date.TotalDays, this.genericWorldInstances.Single(player.currentLocation) });
        return new(binding, receiver, cursor is null ? null : this.CursorFact(cursor), destinations, owned,
            inventoryCounts, ground, complete, complete ? null : "cursor_or_inventory_unreadable")
        {
            Inert = cursor is null ? Array.Empty<int>() : inert, BagFree = player.freeSpotsInInventory(),
            BagCapacity = player.MaxItems, Exit = cursor is null ? "" : HeldItemMenu.Exit(new[] { graph.Dispatch,
                graph.Dispatch is null ? null : CurrentInteractiveMenu(graph.Dispatch), graph.Root }, cursor),
        };
    }

    // Public to sibling ModEntry partials (watchdog). No input is issued by these APIs.
    internal CursorItemSettleSnapshot CaptureCursorItemSettle()
    {
        try { return this.ReadUiPadFacts().CursorSettle ?? UnavailableCursorItemSettle(); }
        catch { return UnavailableCursorItemSettle(); }
    }
    static CursorItemSettleSnapshot UnavailableCursorItemSettle() => new("", "", null, Array.Empty<CursorItemDestination>(),
            new Dictionary<string, int>(), new Dictionary<string, int>(), new Dictionary<string, int>(), false, "ui_unavailable");
    internal bool TryPrepareCursorItemSettle(string choiceToken, out CursorItemSettlePrepared? prepared, out string? refusal)
    {
        prepared = null;
        UiPadFacts facts;
        try { facts = this.ReadUiPadFacts(); }
        catch { refusal = "cursor_readback_unavailable"; return false; }
        var snapshot = facts.CursorSettle;
        var destination = snapshot?.Destinations.SingleOrDefault(d => d.Token == choiceToken && d.Kind != "trash");
        refusal = destination is null ? "cursor_destination_changed" : CursorItemSettle.PrepareRefusal(snapshot, destination.Id);
        if (refusal is not null || destination is null || snapshot is null) return false;
        string context = UiPadContextTable.ContextOf(facts);
        string token = UiPadToken.Compute(snapshot.Receiver, facts.MenuType, context, facts);
        var press = new UiPressTarget(snapshot.Receiver, facts.MenuType, context, token, "A", facts.SnappedId ?? -1,
            Reserved: new Dictionary<string, string> { ["choice"] = destination.Id.ToString(), ["settle"] = "true" },
            ReservedTyped: new Dictionary<string, object> { ["choice"] = destination.Id.ToString(), ["settle"] = true });
        prepared = new(snapshot, destination, press);
        return true;
    }
    internal CursorItemSettleVerdict VerifyCursorItemSettle(CursorItemSettlePrepared prepared) =>
        CursorItemSettle.Verify(prepared.Before, this.CaptureCursorItemSettle(), prepared.Destination);

    /// <summary>Call on the actual pressed edge, after re-inspecting the component graph.</summary>
    internal string? ValidateCursorItemSettleEdge(int componentId) =>
        CursorItemSettle.PrepareRefusal(this.CaptureCursorItemSettle(), componentId);

    /// <summary>The only owner disposal denylist: tools and quest items. Native handlers may still refuse.</summary>
    internal string? ValidateItemDisposeEdge(int componentId)
    {
        var snapshot = this.CaptureCursorItemSettle();
        var target = snapshot.Complete
            ? snapshot.Destinations.FirstOrDefault(d => d.Id == componentId && CursorItemSettle.Destructive(d.Kind)) : null;
        if (target is null) return "dispose_destination_or_cursor_unavailable";
        return CursorItemSettle.HeldDisposeRefusal(snapshot.Cursor, target.Kind);
    }

    /// <summary>Actual-edge safety guard for every generic item disposal gesture.
    /// Toolbar Shift-right is a native drop gesture even with no cursor item.</summary>
    internal string? ValidateGenericItemPolicyEdge(int componentId, string operation)
    {
        try
        {
            var graph = GenericMenuSurface is { } menu ? GenericInspect(menu) : GenericInspectHud();
            var widget = graph.Widgets.SingleOrDefault(w => w.Id == componentId);
            if (widget is null) return null; // non-component key/close operations have their own admission
            string kind = CursorWidgetKind(widget);
            if ((widget.Dispatch is ShopMenu || widget.Dispatch is ItemGrabMenu { shippingBin: true })
                && widget.Item is { } offeredItem
                && CursorItemSettle.DisposeRefusal(this.CursorFact(offeredItem)) is { } protectedItem)
                return protectedItem;
            if (CursorItemSettle.Destructive(kind)) return this.ValidateItemDisposeEdge(componentId);
            if (widget.Dispatch is Toolbar && operation.StartsWith("shift_", StringComparison.Ordinal)
                && operation.EndsWith("right", StringComparison.Ordinal))
            {
                if (!int.TryParse(widget.Component?.name, out int slot) || slot < 0 || slot >= Game1.player.Items.Count)
                    return "toolbar_slot_unreadable";
                Item? item = Game1.player.Items[slot];
                return item is null ? null : CursorItemSettle.DisposeRefusal(this.CursorFact(item));
            }
            return null;
        }
        catch { return "item_policy_unreadable"; }
    }
}
