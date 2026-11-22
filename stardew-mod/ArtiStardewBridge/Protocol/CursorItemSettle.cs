using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

// Generic cursor transaction. Planning and readback only; the existing UI owner executes the press.
// PM-1 fix14b (runbooks/spec-audit/HELD_ITEM_FULL_BAG_ROOTCAUSE_2026-10-30.md, ADR 33): destinations follow the
// game's own held-item click path (InventoryMenu.leftClick place/stack/swap, the menu's held-item route into a
// container, drop outside the menu / trash can per Item.canBeTrashed), not the widget graph's shape.
/// <param name="Trashable">The game's own <c>Item.canBeTrashed()</c>: vanilla menus drop or trash a held item only then.</param>
public sealed record CursorItemFact(string Instance, string Qid, int Quality, int Stack, bool Tool = false, bool QuestItem = false,
    bool Trashable = true, string? Name = null)
{
    public string Key => Qid + ":q" + Quality;
}
/// <param name="Swap">kind "swap": the occupied bag slot's item, which the game puts on the cursor in exchange.</param>
public sealed record CursorItemDestination(int Id, string Kind, string Inventory, int Slot, int Room, string Token,
    CursorItemFact? Swap = null);
public sealed record CursorItemSettleSnapshot(string Binding, string Receiver, CursorItemFact? Cursor,
    IReadOnlyList<CursorItemDestination> Destinations, IReadOnlyDictionary<string, int> Owned,
    IReadOnlyDictionary<string, int> InventoryCounts, IReadOnlyDictionary<string, int> GroundInstances,
    bool Complete = true, string? Refusal = null)
{
    /// <summary>Item slots the game ignores with this cursor item (locked bag slots, a container the menu does not
    /// route a held item into, a bag slot InventoryMenu.leftClick skips). Never a published press while held.</summary>
    public IReadOnlyList<int> Inert { get; init; } = Array.Empty<int>();
    /// <summary>Free player bag slots (Farmer.freeSpotsInInventory); -1 = unread.</summary>
    public int BagFree { get; init; } = -1;
    /// <summary>Player bag capacity (Farmer.MaxItems); 0 = unread.</summary>
    public int BagCapacity { get; init; }
    /// <summary>What the menu itself does with the cursor item on exit (CursorItemSettle.Exit*); "" = no cursor item.</summary>
    public string Exit { get; init; } = "";
    public string Status => !Complete ? "Uncertain" : Cursor is null ? "Empty"
        : Destinations.Any(d => d.Kind != "trash" && (!CursorItemSettle.Destructive(d.Kind)
            || CursorItemSettle.HeldDisposeRefusal(Cursor, d.Kind) is null)) ? "NeedsChoice" : "BlockedInventory";
}
/// <summary>PM-1 fix14b: one visible widget as the game's held-item click path sees it. <paramref name="Room"/> = what
/// InventoryMenu.leftClick would place (empty slot: max stack; same stack: free room; otherwise 0).
/// <paramref name="Highlighted"/> = InventoryMenu.highlightMethod(slot item); null = unknown (mod callback).</summary>
public sealed record HeldWidget(int Id, string Kind, bool Slot = false, bool Bag = false, bool Locked = false,
    CursorItemFact? Item = null, bool Stackable = false, bool? Highlighted = true, int Room = 0, string Inventory = "", int Index = -1);
public sealed record HeldTarget(HeldWidget Widget, string Kind, int Room, CursorItemFact? Swap);
public sealed record CursorItemSettlePrepared(CursorItemSettleSnapshot Before, CursorItemDestination Destination, UiPressTarget Press);
public sealed record CursorItemSettleVerdict(string Status, bool Conserved, int Moved, string? Refusal,
    IReadOnlyDictionary<string, int> Deltas, CursorItemSettleSnapshot? After);

public static class CursorItemSettle
{
    /// <summary>Menu exit with a cursor item (MenuWithInventory.AllowExitWithHeldItem / HeldItemExitBehavior; every
    /// other vanilla held-item menu has readyToClose() == (heldItem == null)).</summary>
    public const string ExitBlocked = "blocked_until_placed", ExitReturns = "returns_to_bag",
        ExitDrops = "drops_at_feet", ExitDiscards = "discards";
    /// <summary>Close is withheld only while the game refuses it or it would destroy the item.</summary>
    public static bool ExitAdmitted(CursorItemSettleSnapshot? s) => s?.Cursor is null || s.Exit is ExitReturns or ExitDrops;

    /// <summary>The owner's sell/dispose denylist (tools and quest items): selling, shipping and toolbar drops.</summary>
    public static string? DisposeRefusal(CursorItemFact? item) => item is null ? "cursor_unreadable"
        : item.Tool ? "tool" : item.QuestItem ? "quest_item" : null;
    /// <summary>PM-1 fix14b: a held item leaving the cursor. A drop keeps the item in the world, so only the game's
    /// own rule applies (Item.canBeTrashed, which already keeps quest items, basic tools and scythes). Trash destroys
    /// it, so the owner's denylist applies on top of the game's rule.</summary>
    public static string? HeldDisposeRefusal(CursorItemFact? item, string? kind) => item is null ? "cursor_unreadable"
        : item.QuestItem ? "quest_item" : !item.Trashable ? "game_keeps_item"
        : kind == "drop" ? null : DisposeRefusal(item);
    public static bool Destructive(string? kind) => kind is "drop" or "trash";
    /// <summary>PM-1 fix14b (ADR 33): the held-item destinations and inert slots, from the game's own click path:
    /// a bag slot places/stacks (room) or swaps (occupied, eligible, not stackable); a non-bag inventory takes it only
    /// when the menu routes a held item there; locked slots, ineligible bag slots and full stacks do nothing; the
    /// drop / trash widgets take the whole stack (their own item rule is HeldDisposeRefusal).</summary>
    public static (List<HeldTarget> Targets, List<int> Inert) Classify(CursorItemFact cursor, bool containerRouted,
        IEnumerable<HeldWidget> widgets)
    {
        var targets = new List<HeldTarget>();
        var inert = new List<int>();
        foreach (var w in widgets)
        {
            if (w.Locked) { inert.Add(w.Id); continue; }
            if (w.Slot)
            {
                if (!w.Bag && !containerRouted) { inert.Add(w.Id); continue; } // vanilla ignores it (fix12a assumed a put-back)
                if (w.Room > 0) { targets.Add(new(w, w.Bag ? "inventory" : "container", w.Room, null)); continue; }
                if (!w.Bag) continue; // an occupied container slot: the menu's own route decides, no settle offered
                if (w.Item is { } present && !w.Stackable)
                {   // with a full bag this is the game's own way to keep the item (a19 Bamboo Pole)
                    if (w.Highlighted == true) targets.Add(new(w, "swap", cursor.Stack, present));
                    else if (w.Highlighted == false) inert.Add(w.Id);
                    continue;
                }
                inert.Add(w.Id); // a full matching stack: the click hands the item back unchanged
                continue;
            }
            if (Destructive(w.Kind)) targets.Add(new(w, w.Kind, cursor.Stack, null));
        }
        return (targets, inert);
    }

    /// <summary>Applied verdict effect: the whole stack placed, part of it, or exchanged with a bag slot (fix14b).</summary>
    public static string Effect(string status) => status switch
        { "Settled" => "cursor_settled", "Swapped" => "cursor_swapped", _ => "cursor_partial" };
    public static string? ItemOperation(string? operation) => operation == "settle" ? "settle"
        : operation?.Split('_').FirstOrDefault(Destructive);
    public static string? PrepareRefusal(CursorItemSettleSnapshot? snapshot, int id, string? kind = null)
    {
        if (snapshot is null || !snapshot.Complete) return "cursor_readback_unavailable";
        if (snapshot.Cursor is not { Stack: > 0 }) return "cursor_empty";
        var target = snapshot.Destinations.SingleOrDefault(d => d.Id == id && (kind is null || d.Kind == kind));
        if (target is null || target.Room <= 0) return "cursor_destination_unavailable";
        return Destructive(target.Kind) ? HeldDisposeRefusal(snapshot.Cursor, target.Kind) : null;
    }
    static int Count(IReadOnlyDictionary<string, int> values, string key) => values.TryGetValue(key, out int n) ? n : 0;
    static Dictionary<string, int> Delta(IReadOnlyDictionary<string, int> before, IReadOnlyDictionary<string, int> after) =>
        before.Keys.Concat(after.Keys).Distinct().Where(k => Count(after, k) != Count(before, k))
            .ToDictionary(k => k, k => Count(after, k) - Count(before, k));

    public static CursorItemSettleVerdict Verify(CursorItemSettleSnapshot? before, CursorItemSettleSnapshot? after,
        CursorItemDestination? destination)
    {
        CursorItemSettleVerdict No(string why) => new("Uncertain", false, 0, why, new Dictionary<string, int>(), after);
        if (before?.Cursor is not { } cursor || after is null || destination is null
            || !before.Complete || !after.Complete || before.Binding != after.Binding || before.Receiver != after.Receiver)
            return No("cursor_binding_or_readback_changed");
        if (Destructive(destination.Kind) && HeldDisposeRefusal(cursor, destination.Kind) is { } denied) return No("policy:" + denied);
        if (destination.Kind == "swap") return VerifySwap(before, after, cursor, destination);
        if (after.Cursor is { } remaining && (remaining.Key != cursor.Key || remaining.Instance != cursor.Instance))
            return No("cursor_item_changed");
        int moved = cursor.Stack - (after.Cursor?.Stack ?? 0);
        var delta = Delta(before.Owned, after.Owned);
        if (moved == 0 && delta.Count == 0) return new("NoEffect", true, 0, null, delta, after);
        if (moved <= 0 || moved > Math.Min(cursor.Stack, destination.Room)) return No("cursor_quantity_mismatch");
        if (Destructive(destination.Kind))
        {
            if (delta.Count != 1 || !delta.TryGetValue(cursor.Key, out int debit) || debit != -moved)
                return No("dispose_inventory_mismatch");
            if (destination.Kind == "drop" && Count(after.GroundInstances, cursor.Instance)
                    - Count(before.GroundInstances, cursor.Instance) != moved
                    && Count(after.GroundInstances, "key:" + cursor.Key) - Count(before.GroundInstances, "key:" + cursor.Key) != moved)
                return No("drop_debris_unverified");
        }
        else
        {
            string key = destination.Inventory + ":" + cursor.Key;
            if (delta.Count != 0 || Count(after.InventoryCounts, key) - Count(before.InventoryCounts, key) != moved)
                return No("cursor_conservation_mismatch");
        }
        return new(after.Cursor is null ? "Settled" : "Partial", true, moved, null, delta, after);
    }

    /// <summary>PM-1 fix14b: InventoryMenu.leftClick on an occupied bag slot (Utility.addItemToInventory) puts the held
    /// item there and returns the slot's item on the cursor. Proof: that exact instance is now held, the bag gained
    /// the held stack and lost the swapped stack, and nothing left the player's possession.</summary>
    static CursorItemSettleVerdict VerifySwap(CursorItemSettleSnapshot before, CursorItemSettleSnapshot after,
        CursorItemFact cursor, CursorItemDestination destination)
    {
        CursorItemSettleVerdict No(string why) => new("Uncertain", false, 0, why, new Dictionary<string, int>(), after);
        var owned = Delta(before.Owned, after.Owned);
        if (destination.Swap is not { } swapped || swapped.Key == cursor.Key) return No("swap_destination_invalid");
        if (after.Cursor is { } same && same.Instance == cursor.Instance && same.Stack == cursor.Stack && owned.Count == 0
                && Delta(before.InventoryCounts, after.InventoryCounts).Count == 0)
            return new("NoEffect", true, 0, null, owned, after);
        if (after.Cursor is not { } back || back.Instance != swapped.Instance || back.Key != swapped.Key || back.Stack != swapped.Stack)
            return No("swap_cursor_mismatch");
        string bag = destination.Inventory + ":";
        int gained = Count(after.InventoryCounts, bag + cursor.Key) - Count(before.InventoryCounts, bag + cursor.Key);
        int lost = Count(after.InventoryCounts, bag + swapped.Key) - Count(before.InventoryCounts, bag + swapped.Key);
        if (owned.Count != 0 || gained != cursor.Stack || lost != -swapped.Stack) return No("swap_conservation_mismatch");
        return new("Swapped", true, cursor.Stack, null, owned, after);
    }

    public static object Wire(CursorItemSettleSnapshot snapshot) => new {
        status = snapshot.Status, complete = snapshot.Complete, refusal = snapshot.Refusal,
        cursor = snapshot.Cursor is { } c ? new { item = c.Qid, name = c.Name, quality = c.Quality, stack = c.Stack,
            is_tool = c.Tool, quest_item = c.QuestItem, dispose_refusal = DisposeRefusal(c), trashable = c.Trashable } : null,
        targets = snapshot.Destinations.Select(d => new { id = d.Id, kind = d.Kind, room = d.Room, token = d.Token,
            inventory = d.Inventory, slot = d.Slot,
            refused = Destructive(d.Kind) ? HeldDisposeRefusal(snapshot.Cursor, d.Kind) : null,
            swap = d.Swap is { } w ? new { item = w.Qid, name = w.Name, quality = w.Quality, stack = w.Stack } : null }).ToArray(),
        // PM-1 fix14b: the game's own held-item facts. Python renders them; it never re-derives them.
        bag_free = snapshot.BagFree, bag_capacity = snapshot.BagCapacity, exit = snapshot.Exit,
        inert = snapshot.Inert.ToArray(),
    };
}
