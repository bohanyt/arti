using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>One click on a currently presented toolbar. No input owner or inventory mutation.</summary>
public static class SelectItemInput
{
    public const string Primitive = "select_item";
    static readonly HashSet<string> Keys = new(StringComparer.Ordinal) {
        "receiver_token", "target_token", "slot", "qualified_item_id", "stack", "quality" };

    public static bool TryParse(JsonElement args, out SelectItemTarget target)
    {
        target = default;
        if (args.ValueKind != JsonValueKind.Object || args.EnumerateObject().Count() != Keys.Count
            || args.EnumerateObject().Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != Keys.Count
            || args.EnumerateObject().Any(p => !Keys.Contains(p.Name))) return false;
        string? Text(string key) => args.GetProperty(key).ValueKind == JsonValueKind.String
            ? args.GetProperty(key).GetString() : null;
        bool Int(string key, out int value) {
            value = 0;
            return args.GetProperty(key).ValueKind == JsonValueKind.Number
                && args.GetProperty(key).TryGetInt32(out value);
        }
        if (!Int("slot", out int slot) || !Int("stack", out int stack) || !Int("quality", out int quality))
            return false;
        target = new(Text("receiver_token") ?? "", Text("target_token") ?? "", slot,
            Text("qualified_item_id") ?? "", stack, quality);
        return target.IsValid;
    }

    /// <summary>
    /// Both delegates address the ORIGINAL observed receiver and coordinates.
    /// An exception is ambiguous even if cleanup returned. No retry occurs here.
    /// The readback delegate must read the actual selection after ordinary input.
    /// </summary>
    public static SelectItemVerdict Execute(SelectItemTarget wanted, SelectItemTarget? current,
        bool eligible, Action click, Action release, Func<SelectItemTarget?> selected)
    {
        if (!eligible || !wanted.IsValid || current != wanted)
            return new("SelectionRejected", false, true, 0);
        bool clean = false, fault = false;
        try { click(); }
        catch { fault = true; }
        finally {
            try { release(); clean = true; }
            catch { fault = true; }
        }
        if (fault) return new("SelectionUncertain", false, clean, 1);
        try {
            return selected() == wanted
                ? new("SelectionObserved", true, clean, 1)
                : new("SelectionUnchanged", false, clean, 1);
        }
        catch { return new("SelectionUncertain", false, clean, 1); }
    }
}

public readonly record struct SelectItemTarget(string ReceiverToken, string TargetToken, int Slot,
    string QualifiedItemId, int Stack, int Quality)
{
    public bool IsValid => Slot is >= 0 and < 48 && Stack is >= 0 and <= 999999
        && Quality is 0 or 1 or 2 or 4
        && !string.IsNullOrWhiteSpace(ReceiverToken) && ReceiverToken.Length <= 128
        && !string.IsNullOrWhiteSpace(TargetToken) && TargetToken.Length <= 128
        && (Stack == 0 ? QualifiedItemId == "" && Quality == 0
            : !string.IsNullOrWhiteSpace(QualifiedItemId) && QualifiedItemId.Length <= 128);
    public Dictionary<string, object> Args() => new() {
        ["receiver_token"] = ReceiverToken, ["target_token"] = TargetToken, ["slot"] = Slot,
        ["qualified_item_id"] = QualifiedItemId, ["stack"] = Stack, ["quality"] = Quality };
}

public sealed record SelectItemVerdict(string Outcome, bool Selected, bool CleanupConfirmed, int ClickCount);
