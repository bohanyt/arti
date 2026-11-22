using StardewValley;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    private long? hostPauseBudgetSampleMs;
    private long hostPauseBudgetGameTick;

    // Execution progress only. Command admission, owner lease/STOP and the
    // independent physical-containment clock retain their real-time bounds.
    private void TickHostPauseBudgets()
    {
        long now = NowUnixMs();
        long tick = Game1.ticks;
        if (this.hostPauseBudgetSampleMs is long previous)
        {
            long elapsed = now - previous;
            if (elapsed >= 0 && elapsed <= 500) // never forgive a missing game-thread heartbeat
            {
                long ticks = Math.Max(0, tick - this.hostPauseBudgetGameTick);
                if (this.houseTravel is { Deadline: > 0, Stopping: false } house)
                {
                    house.Deadline += elapsed;
                    house.Started += ticks;
                }
                if (this.eggRun is { ExecutionDeadline: > 0, Stopping: false } route)
                {
                    route.ExecutionDeadline += elapsed;
                    route.Started += ticks;
                }
                if (this.festivalRun is { Ending: false } festival)
                    festival.StartTick += ticks;
            }
        }
        this.hostPauseBudgetSampleMs = Game1.paused || Game1.HostPaused ? now : null;
        this.hostPauseBudgetGameTick = tick;
    }
}
