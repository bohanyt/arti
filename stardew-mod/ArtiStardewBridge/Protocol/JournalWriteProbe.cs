using System.Diagnostics;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>Measured persistence latency, including serialization, retries and durable flushes.
/// Timer diagnostics read atomics only; they never wait on the journal/game-thread lock.</summary>
public sealed class JournalWriteProbe
{
    static JournalWriteProbe? live;
    internal static JournalWriteProbe? Live { get => Volatile.Read(ref live); set => Volatile.Write(ref live, value); }
    readonly Func<long> clock;
    readonly double frequency;
    long writes, failures, total, last, maximum, started, recordBytes, sequence, reportedWrites, reportedMaximum;
    int active;
    string phase = "none";
    /// <summary>fix54I: the off-game-thread terminal writer's own measured durable I/O (null when not deferred).</summary>
    internal JournalWriteProbe? Deferred { get; set; }
    internal Func<int>? DeferredPending { get; set; }
    /// <summary>fix55D: the named deferred-writer fault (deferred_drain_timeout, deferred_write_failed, ...), else null.</summary>
    internal Func<string?>? DeferredFault { get; set; }

    public JournalWriteProbe() : this(Stopwatch.GetTimestamp, Stopwatch.Frequency) { }
    internal JournalWriteProbe(Func<long> clock, double frequency) { this.clock = clock; this.frequency = frequency; }
    internal long Begin(string phase)
    {
        long now = this.clock();
        Volatile.Write(ref this.phase, phase); Interlocked.Exchange(ref this.recordBytes, 0);
        Interlocked.Exchange(ref this.started, now); Volatile.Write(ref this.active, 1);
        return now;
    }
    internal void Record(long sequence, long bytes)
    { Interlocked.Exchange(ref this.sequence, sequence); Interlocked.Exchange(ref this.recordBytes, bytes); }
    internal void End(long start, bool succeeded)
    {
        long ticks = Math.Max(0, this.clock() - start);
        Interlocked.Exchange(ref this.last, ticks); Interlocked.Add(ref this.total, ticks);
        long old;
        do { old = Interlocked.Read(ref this.maximum); if (old >= ticks) break; }
        while (Interlocked.CompareExchange(ref this.maximum, ticks, old) != old);
        if (!succeeded) Interlocked.Increment(ref this.failures);
        Interlocked.Increment(ref this.writes); Volatile.Write(ref this.active, 0);
    }
    double Ms(long ticks) => Math.Round(ticks * 1000.0 / this.frequency, 3);
    public JsonElement Snapshot() => JsonSerializer.SerializeToElement(new {
        writes = Interlocked.Read(ref this.writes), failures = Interlocked.Read(ref this.failures),
        total_ms = this.Ms(Interlocked.Read(ref this.total)), last_ms = this.Ms(Interlocked.Read(ref this.last)),
        max_ms = this.Ms(Interlocked.Read(ref this.maximum)), record_bytes = Interlocked.Read(ref this.recordBytes),
        sequence = Interlocked.Read(ref this.sequence), phase = Volatile.Read(ref this.phase),
        active = Volatile.Read(ref this.active) != 0,
        active_ms = Volatile.Read(ref this.active) == 0 ? 0 : this.Ms(Math.Max(0, this.clock() - Interlocked.Read(ref this.started))),
        deferred = this.Deferred?.Snapshot(),
        deferred_pending = this.DeferredPending?.Invoke() ?? 0,
        deferred_fault = this.DeferredFault?.Invoke(),
    });
    internal static string LiveJson() => Live?.Snapshot().GetRawText() ?? "null";
    internal static void LogPending(Action<string> log)
    {
        if (Live is not { } probe) return;
        LogPending(probe, "COMMAND_JOURNAL_WRITE ", log);
        if (probe.Deferred is { } deferred) LogPending(deferred, "COMMAND_JOURNAL_DEFERRED_WRITE ", log);
    }
    static void LogPending(JournalWriteProbe probe, string label, Action<string> log)
    {
        long count = Interlocked.Read(ref probe.writes), max = Interlocked.Read(ref probe.maximum);
        if (count - Interlocked.Read(ref probe.reportedWrites) < 64
            && !(probe.Ms(max) >= 250 && max > Interlocked.Read(ref probe.reportedMaximum))) return;
        Interlocked.Exchange(ref probe.reportedWrites, count); Interlocked.Exchange(ref probe.reportedMaximum, max);
        log(label + probe.Snapshot().GetRawText());
    }
}
