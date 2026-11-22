namespace ArtiStardewBridge.Protocol;

/// <summary>ADR 63: positive native state diffs, never event-ID/calendar inference.</summary>
public static class NativeStoryFacts
{
    public static IEnumerable<string> Additions(IReadOnlyDictionary<string, string>? before,
        IReadOnlyDictionary<string, string> after)
    {
        if (before is null) yield break;
        foreach (var pair in after) {
            before.TryGetValue(pair.Key, out string? old);
            bool changed = pair.Key.StartsWith("accessible:") || pair.Key.StartsWith("door:") || pair.Key.StartsWith("key:")
                ? old == "false" && pair.Value == "true"
                : pair.Key.StartsWith("item:")
                    ? int.TryParse(pair.Value.Split(' ')[0], out int quantity) && quantity > 0
                        && (old is null || int.TryParse(old.Split(' ')[0], out int previous) && quantity > previous)
                    : old is null;
            if (changed) yield return pair.Key + ": " + (old ?? "absent") + " -> " + pair.Value;
        }
    }

    /// <summary>
    /// The diff baseline while one native instance (event, farm event, letter) stays open (ADR 71).
    /// Menus shown inside an instance temporarily swap the farmer's item list, so a snapshot can show
    /// the bag empty and the next one refilled. A key that vanishes inside the instance is kept at its
    /// last observed value; its return is then no acquisition (a39 event 980559: five old items were
    /// recorded "absent -> added", only the Training Rod was new). Keys present in <paramref name="current"/>
    /// take their current value, so a real gain inside the instance is still reported. Outside an
    /// instance the caller uses the raw snapshot, so a real removal is forgotten there, never here.
    /// </summary>
    public static Dictionary<string, string> Carry(IReadOnlyDictionary<string, string>? previous,
        IReadOnlyDictionary<string, string> current)
    {
        var merged = new Dictionary<string, string>(current);
        if (previous is null) return merged;
        foreach (var pair in previous)
            if (!merged.ContainsKey(pair.Key)) merged[pair.Key] = pair.Value;
        return merged;
    }
}
