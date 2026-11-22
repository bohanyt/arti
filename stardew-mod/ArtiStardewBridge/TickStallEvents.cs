using ArtiStardewBridge.Protocol;
using StardewModdingAPI.Events;
using StardewModdingAPI;

namespace ArtiStardewBridge;

/// <summary>PM-1 fix14c: TickStallProbe markers at the start of the non-update events the bridge also handles (save
/// load, day start, draw), registered first in Entry, so later handlers of these events are attributed to them; and
/// low-priority after-markers for UpdateTicking / UpdateTicked (ModEntry keeps exactly one subscription to each).
/// Outside ModEntry so its member ordinals (IL D-row keys) stay stable.</summary>
static class TickStallEvents
{
    /// <summary>One probe per process (the game thread is one thread for every screen).</summary>
    public static readonly TickStallProbe Probe = new();

    public static void Register(IModEvents events)
    {
        var probe = Probe;
        events.GameLoop.SaveLoaded += (_, _) => probe.Enter("save_loaded");
        events.GameLoop.DayStarted += (_, _) => probe.Enter("day_started");
        events.Display.Rendering += (_, _) => probe.Enter("draw");
        events.Display.Rendered += (_, _) => probe.Enter("between_ticks");
        events.GameLoop.UpdateTicking += AfterTicking;
        events.GameLoop.UpdateTicked += AfterTicked;
    }

    // Low priority: SMAPI runs these after the bridge's own (normal-priority) UpdateTicking / UpdateTicked handlers,
    // so the marker says "the game's update body runs next" / "the bridge's update work is done".
    [EventPriority(EventPriority.Low)]
    static void AfterTicking(object? sender, UpdateTickingEventArgs e) => Probe.AfterTicking(sender, e);
    [EventPriority(EventPriority.Low)]
    static void AfterTicked(object? sender, UpdateTickedEventArgs e) => Probe.AfterTicked(sender, e);
}
