using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>Two buttons, no target taxonomy or expected gameplay result.</summary>
public static class GenericWorld
{
    public const string Action = "generic_action", Use = "generic_use";
    public const string Changed = "WorldDeltaObserved", Unchanged = "WorldUnchanged";
    public const int Radius = 2, MaxFields = 256, MaxChanges = 96;
    public const long SettleMs = 3000;
    public static bool IsKind(string? kind) => kind is Action or Use;

    // ADR 112: a native map Action is an ordinary button surface, even with no
    // chest or vendor in this room. Native input and literal readback still decide the result.
    public static bool OrdinarySurface(bool container, bool vendor, bool extras, string? mapAction) =>
        container || vendor || extras && !string.IsNullOrWhiteSpace(mapAction);

    public static GenericWorldDelta? Compare(GenericWorldFrame? before, GenericWorldFrame? after)
    {
        if (before is null || after is null || !before.Complete || !after.Complete
            || before.Binding != after.Binding || before.Window != after.Window
            || before.Fields.Count > MaxFields || after.Fields.Count > MaxFields
            || after.Tick <= before.Tick)
            return null;
        var changes = new List<GenericWorldChange>();
        foreach (string path in before.Fields.Keys.Concat(after.Fields.Keys).Distinct().OrderBy(k => k, StringComparer.Ordinal))
        {
            before.Fields.TryGetValue(path, out string? b);
            after.Fields.TryGetValue(path, out string? a);
            if (b != a) changes.Add(new(path, b, a));
        }
        return changes.Count > MaxChanges ? null : new(before.Tick, after.Tick, changes);
    }

    public static BgActionEffectVerdict Classify(GenericWorldFrame? before, GenericWorldFrame? after)
    {
        var delta = Compare(before, after);
        return delta is null
            ? new(BgHoldCompletion.InteractionUncertain, false, "GENERIC_READBACK_UNAVAILABLE", null, 0)
            : new(delta.Changes.Count > 0 ? Changed : Unchanged, delta.Changes.Count > 0, null, null, 0);
    }

    public static string? Detail(GenericWorldFrame? before, GenericWorldFrame? after) =>
        Compare(before, after) is { } delta ? JsonSerializer.Serialize(delta) : null;
}

/// <summary>Values are canonical JSON of observed fields. Missing is distinct from JSON null.
/// Binding identifies the actor/session/date; Window pins the sampled area across a warp.
/// This is a per-command baseline, never another world store.</summary>
public sealed record GenericWorldFrame(string Binding, string Window, long Tick,
    IReadOnlyDictionary<string, string> Fields, bool Complete = true);
public sealed record GenericWorldChange(string Path, string? Before, string? After);
public sealed record GenericWorldDelta(long BeforeTick, long AfterTick, IReadOnlyList<GenericWorldChange> Changes);
