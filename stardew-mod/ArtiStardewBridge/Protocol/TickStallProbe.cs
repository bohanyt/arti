namespace ArtiStardewBridge.Protocol;

/// <summary>PM-1 fix14c: names where the game thread stalls. a20 (1ddd82c1e) advanced ~6 ticks in 233 s right after
/// world-ready, with no log line saying where. The game thread marks each bridge update stage; a background timer reads
/// only that marker (no game state, no SMAPI call) and writes one TICK_STALL line to stdout per stall of at least
/// <see cref="ThresholdMs"/>, then TICK_STALL_END when the thread moves on. "between_ticks" = outside the bridge's update
/// handlers (game update/draw, other mods, patches). Diagnostics only: never input, never a game write.</summary>
public sealed class TickStallProbe : IDisposable
{
    public const long ThresholdMs = 5000;
    readonly Func<long> clock;
    readonly Action<string> log;
    readonly Timer? timer;
    readonly object gate = new();
    string stage = "start", last = "start";
    long enteredMs, reportedAt = long.MinValue;

    // ADR 55: immutable progress telemetry shares this existing diagnostic cadence.
    public event Action? DiagnosticTick;

    public TickStallProbe(Func<long>? clock = null, Action<string>? log = null, bool startTimer = true)
    {
        this.clock = clock ?? (() => Environment.TickCount64);
        this.log = log ?? (line => Console.Out.WriteLine("[Arti Stardew Bridge] " + line));
        this.enteredMs = this.clock();
        this.timer = startTimer ? new Timer(_ => this.Check(), null, 1000, 1000) : null;
    }

    /// <summary>Game thread: the bridge is now in <paramref name="name"/>.</summary>
    public void Enter(string name)
    {
        string? ended = null;
        lock (this.gate)
        {
            long now = this.clock();
            if (this.reportedAt == this.enteredMs)
                ended = $"TICK_STALL_END {{\"stage\":\"{this.stage}\",\"after\":\"{this.last}\",\"elapsed_ms\":{now - this.enteredMs},\"next\":\"{name}\",\"journal_write\":{JournalWriteProbe.LiveJson()}}}";
            if (this.stage != "between_ticks") this.last = this.stage;
            this.stage = name;
            this.enteredMs = now;
        }
        if (ended is not null) this.log(ended);
    }

    /// <summary>SMAPI UpdateTicked handler registered after the bridge's own: its update work for this tick is done.</summary>
    public void AfterTicked(object? sender, EventArgs e) => this.Enter("between_ticks");

    /// <summary>SMAPI UpdateTicking handler registered after the bridge's own: the game's update body runs next.</summary>
    public void AfterTicking(object? sender, EventArgs e) => this.Enter("game_update");

    /// <summary>Timer thread (also called directly by tests). Returns the line it wrote, if any.</summary>
    public string? Check()
    {
        this.DiagnosticTick?.Invoke();
        JournalWriteProbe.LogPending(this.log); // Existing timer thread: no diagnostic file I/O on the action thread.
        string line;
        lock (this.gate)
        {
            long age = this.clock() - this.enteredMs;
            if (age < ThresholdMs || this.reportedAt == this.enteredMs) return null;
            this.reportedAt = this.enteredMs;
            line = $"TICK_STALL {{\"stage\":\"{this.stage}\",\"after\":\"{this.last}\",\"elapsed_ms\":{age},\"journal_write\":{JournalWriteProbe.LiveJson()}}}";
        }
        this.log(line);
        return line;
    }

    public void Dispose() => this.timer?.Dispose();
}
