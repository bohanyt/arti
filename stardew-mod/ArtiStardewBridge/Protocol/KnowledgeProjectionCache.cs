namespace ArtiStardewBridge.Protocol;

/// <summary>ADR 50: one latest value, exact change detection; no clock/count-only cache keys.</summary>
public sealed class KnowledgeProjectionCache<T> where T : class
{
    string[]? previous;
    T? value;
    public T Get(string[] input, Func<T> project)
    {
        if (this.value is not null && this.previous is not null && input.SequenceEqual(this.previous, StringComparer.Ordinal))
            return this.value;
        T next = project(); // failures never install a key for a stale result
        this.previous = input;
        return this.value = next;
    }
    public void Invalidate() { this.previous = null; this.value = null; }
}

/// <summary>Game thread copies string pairs only. Script parsing runs in one worker, never waits in Capture.
/// A changed asset returns unknown until its own result is ready; superseded jobs cannot publish old facts.</summary>
public sealed class EventConditionCache
{
    readonly Func<KeyValuePair<string, string>[], KeyValuePair<string, string>[], (object[] Unlock, object[] Translation)> project;
    public long Revision { get; private set; }
    Task<(object[] Unlock, object[] Translation)>? pending;
    KeyValuePair<string, string>[]? town, wizard;
    public EventConditionCache(Func<KeyValuePair<string, string>[], KeyValuePair<string, string>[],
        (object[] Unlock, object[] Translation)>? project = null) => this.project = project ?? ((t, w) => (
            DonationFacts.UnlockSources(t, "ccDoorUnlock"),
            w.Where(p => p.Key.Split('/').Contains("n seenJunimoNote")).Take(8).Select(p => (object)p.Key).ToArray()));
    public (object[] Unlock, object[] Translation)? Capture(
        KeyValuePair<string, string>[] town, KeyValuePair<string, string>[] wizard)
    {
        bool changed = this.town is null || !town.SequenceEqual(this.town) || !wizard.SequenceEqual(this.wizard!)
            || this.pending?.IsFaulted == true;
        // Never queue work behind a blocked worker. Invalidate old publication immediately.
        if (changed && this.pending is { IsCompleted: false }) return null;
        if (changed) {
            this.town = town; this.wizard = wizard;
            ++this.Revision;
            this.pending = Task.Run(() => this.project(town, wizard));
        }
        return this.pending is { IsCompletedSuccessfully: true } done ? done.Result : null;
    }
    public void Invalidate() { this.town = null; this.wizard = null; ++this.Revision; }
}
