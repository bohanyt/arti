namespace ArtiStardewBridge.Protocol;

// ADR 55. Only immutable snapshots cross threads. No game reads or input on the emitter.
public sealed record ProgressCoverage(string Monitor, string Save, long Player, long ObservedWallMs,
    int Deposits, int Donations, int Letters, bool Ambiguous);

public sealed class NativeProgressHeartbeat : IDisposable
{
    readonly Action<object> emit;
    readonly TickStallProbe diagnostics;
    readonly Func<long> clock;
    ProgressCoverage? latest;
    long sequence, lastEmission;
    bool emitted;
    int stopped, invalidated, emitting;
    public const int IntervalMs = 5000; // margin inside the evaluator's unchanged 15 s bound

    public NativeProgressHeartbeat(Action<object> emit, TickStallProbe diagnostics, Func<long>? clock = null)
    {
        this.emit = emit;
        this.diagnostics = diagnostics;
        this.clock = clock ?? (() => Environment.TickCount64);
        this.diagnostics.DiagnosticTick += this.Check;
    }
    public void Publish(ProgressCoverage sample)
    {
        Interlocked.Exchange(ref this.latest,
            sample with { Ambiguous = sample.Ambiguous || Volatile.Read(ref this.invalidated) != 0 });
    }
    public void Invalidate()
    {
        Interlocked.Exchange(ref this.invalidated, 1);
    }
    public static object Row(ProgressCoverage sample, long sequence, long wallMs) => new {
        ev = "native_progress_coverage", schema = "arti.native_progress/2", sample_kind = "heartbeat",
        monitor_id = sample.Monitor, seq = sequence, save_id = sample.Save, player_id = sample.Player,
        wall_ms = wallMs, observed_wall_ms = sample.ObservedWallMs,
        cc_bundle_deposits = sample.Deposits, museum_donations = sample.Donations,
        letter_tasks_completed = sample.Letters, ambiguous = sample.Ambiguous
    };
    void Check()
    {
        // The existing 1 Hz diagnostic timer may overlap callbacks if a sink blocks.
        // Never queue/backfill missed heartbeats or let an older callback reorder rows.
        if (Interlocked.CompareExchange(ref this.emitting, 1, 0) != 0) return;
        try {
            if (Volatile.Read(ref this.stopped) != 0 || Volatile.Read(ref this.latest) is not { } sample) return;
            long now = this.clock();
            if (this.emitted && now - this.lastEmission < IntervalMs) return;
            this.emitted = true;
            this.lastEmission = now;
            sample = sample with { Ambiguous = sample.Ambiguous || Volatile.Read(ref this.invalidated) != 0 };
            try { this.emit(Row(sample, Interlocked.Increment(ref this.sequence), DateTimeOffset.UtcNow.ToUnixTimeMilliseconds())); }
            catch { this.Invalidate(); } // no synthetic backfill; skipped sequence/freshness fails closed
        } finally { Volatile.Write(ref this.emitting, 0); }
    }
    public void Dispose()
    {
        if (Interlocked.Exchange(ref this.stopped, 1) == 0) this.diagnostics.DiagnosticTick -= this.Check;
    }
}
