using System.Collections;
using System.Reflection;

namespace ArtiStardewBridge.Protocol;

// ADR 64: native server facts relayed over SMAPI; never infer a farmer's state from client counts.
public sealed record ReadyFarmer(long Id, string Name, bool Self, string State);
public sealed record NativeReadyCheck(string Check, ReadyFarmer[] Players);
public sealed record ReadyCheckRelay(ulong SaveId, NativeReadyCheck[] Checks);

public static class NativeReadyChecks
{
    const BindingFlags Fields = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
    public static NativeReadyCheck[] Server(object synchronizer, IReadOnlyDictionary<long, string> names, long local)
    {
        if (synchronizer.GetType().GetField("ReadyChecks", Fields)?.GetValue(synchronizer) is not IDictionary checks)
            return Array.Empty<NativeReadyCheck>();
        var result = new List<NativeReadyCheck>();
        foreach (DictionaryEntry entry in checks)
        {
            if (result.Count >= 32) break;
            if (entry.Key is not string id || entry.Value is null || id.Length > 128
                || entry.Value.GetType().GetProperty("IsReady")?.GetValue(entry.Value) is true
                || entry.Value.GetType().GetField("ReadyStates", Fields)?.GetValue(entry.Value) is not IDictionary states)
                continue;
            // No state entry means native NotReady for an online required farmer (ServerReadyCheck.Update).
            var required = entry.Value.GetType().GetField("RequiredFarmers", Fields)?.GetValue(entry.Value) as IEnumerable<long>;
            var ids = required?.ToArray() ?? Array.Empty<long>();
            var players = names.Where(p => ids.Length == 0 || ids.Contains(p.Key)).Take(8)
                .Select(p => new ReadyFarmer(p.Key, p.Value[..Math.Min(40, p.Value.Length)], p.Key == local,
                    states.Contains(p.Key) ? states[p.Key]?.ToString() ?? "unknown" : "NotReady")).ToArray();
            result.Add(new NativeReadyCheck(id, players));
        }
        return result.ToArray();
    }
}

public sealed class PassiveReadyBudget
{
    public const double WallSeconds = 600;
    public const int MaxReady = 128;
    double? since;
    int readies;
    public bool Observe(bool active, double now)
    {
        if (!active) { since = null; readies = 0; return true; }
        since ??= now;
        return now - since < WallSeconds && readies < MaxReady;
    }
    public bool ShouldReady(NativeReadyCheck check, long host, long peer) =>
        readies < MaxReady && check.Players.Any(p => p.Id == peer && p.State is "Ready" or "Locked")
        && check.Players.Any(p => p.Id == host && p.State == "NotReady");
    public void Readied() => ++readies;
}
