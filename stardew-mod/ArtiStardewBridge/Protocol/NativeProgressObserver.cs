namespace ArtiStardewBridge.Protocol;

// ADR 38: only native transitions are receipts. Baselines (including reloads) never earn credit.
public sealed record ProgressStack(string Item, int Quality, int Category, int Quantity);
public sealed record ProgressIngredient(string Bundle, int Slot, string Item, int Quantity, int Quality, bool Done);
public sealed record ProgressTask(string Kind, string Id, string Letter, string LinkSource, bool Completed, bool Actor);
public sealed record ProgressSample(string Save, long Player, long Day, long WallMs,
    IReadOnlyList<ProgressIngredient> Ingredients, IReadOnlyCollection<string> Museum,
    IReadOnlyList<ProgressStack> Inventory, IReadOnlyList<ProgressTask> Tasks, string Menu);
public sealed record ProgressCorrelation(string Request, long Player, string Menu, bool Delivered);

public sealed class NativeProgressObserver
{
    ProgressSample? previous;
    readonly HashSet<string> emitted = new(StringComparer.Ordinal);
    public int Deposits { get; private set; }
    public int Donations { get; private set; }
    public int Letters { get; private set; }
    public bool Ambiguous { get; private set; }
    public void Gap() { previous = null; Ambiguous = true; }
    public IReadOnlyList<Dictionary<string, object?>> Observe(ProgressSample now, ProgressCorrelation? action = null)
    {
        var rows = new List<Dictionary<string, object?>>();
        var before = previous; previous = now;
        if (before is null || before.Save != now.Save || before.Player != now.Player || now.Day < before.Day)
            return rows;
        // Bag AND cursor/menu-held stacks: moving an item onto the cursor isn't a donation.
        var losses = before.Inventory.ToDictionary(s => (s.Item, s.Quality, s.Category), s => s.Quantity);
        foreach (var s in now.Inventory) losses[(s.Item, s.Quality, s.Category)] = losses.GetValueOrDefault((s.Item, s.Quality, s.Category)) - s.Quantity;
        bool Actor(string menu) => action is { Delivered: true } && action.Player == now.Player
            && action.Request.Length > 0 && action.Menu == menu && before.Menu == menu;
        Dictionary<string, object?>? Receipt(string kind, string key, bool actor)
        {
            string id = now.Save + ":" + (kind == "letter_task_completed" ? now.Player + ":" : "") + kind + ":" + key;
            if (!emitted.Add(id)) return null;
            var row = new Dictionary<string, object?> { ["ev"] = "native_progress", ["schema"] = "arti.native_progress/1",
                ["kind"] = kind, ["event_id"] = id, ["save_id"] = now.Save, ["player_id"] = now.Player,
                ["wall_ms"] = now.WallMs, ["actor_verified"] = actor, ["request_id"] = actor ? action?.Request : null };
            rows.Add(row); return row;
        }
        foreach (var n in now.Ingredients.Where(n => n.Done && n.Item != "money" && n.Quantity > 0))
        {
            var b = before.Ingredients.SingleOrDefault(b => b.Bundle == n.Bundle && b.Slot == n.Slot);
            if (b is null || b.Done || b.Item != n.Item || b.Quantity != n.Quantity || b.Quality != n.Quality) continue;
            var matches = losses.Where(p => p.Value > 0 && p.Key.Quality >= n.Quality
                && (p.Key.Item == n.Item || int.TryParse(n.Item, out int cat) && cat < 0 && p.Key.Category == cat))
                .GroupBy(p => p.Key.Item).Where(g => g.Sum(p => p.Value) == n.Quantity).ToArray();
            bool paired = Actor("bundle") && matches.Length == 1;
            var row = Receipt("cc_bundle_deposit", n.Bundle + ":" + n.Slot, paired);
            if (row is null) continue;
            row["bundle_id"] = n.Bundle; row["slot"] = n.Slot; row["item"] = paired ? matches[0].Key : n.Item;
            row["quantity"] = n.Quantity; row["flag_before"] = false; row["flag_after"] = true;
            row["inventory_delta"] = paired ? -n.Quantity : null;
            if (paired) { foreach (var loss in matches[0]) losses[loss.Key] = 0; Deposits++; } else Ambiguous = true;
        }
        foreach (string item in now.Museum.Except(before.Museum))
        {
            var matches = losses.Where(p => p.Key.Item == item && p.Value == 1).ToArray();
            bool paired = Actor("museum") && matches.Length == 1;
            var row = Receipt("museum_donation", item, paired);
            if (row is null) continue;
            row["item"] = item; row["quantity"] = 1; row["museum_before"] = false; row["museum_after"] = true;
            row["inventory_delta"] = paired ? -1 : null;
            if (paired) { losses[matches[0].Key] = 0; Donations++; } else Ambiguous = true;
        }
        foreach (var n in now.Tasks.Where(t => t.Completed && t.Letter.Length > 0))
        {
            var b = before.Tasks.SingleOrDefault(b => b.Kind == n.Kind && b.Id == n.Id && b.Letter == n.Letter);
            if (b is null || b.Completed || b.LinkSource != n.LinkSource) continue;
            // Quest log is per actor; special orders require native participant membership.
            var row = Receipt("letter_task_completed", n.Kind + ":" + n.Id + ":" + n.Letter, n.Actor);
            if (row is null) continue;
            row["request_id"] = n.Actor && action is { Delivered: true } && action.Player == now.Player ? action.Request : null;
            row["task_id"] = n.Id; row["task_kind"] = n.Kind; row["letter_id"] = n.Letter; row["link_source"] = n.LinkSource;
            row["completed_before"] = false; row["completed"] = true;
            if (n.Actor) Letters++; else Ambiguous = true;
        }
        return rows;
    }
}
