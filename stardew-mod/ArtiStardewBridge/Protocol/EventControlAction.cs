namespace ArtiStardewBridge.Protocol;

/// <summary>Event ACTION kinds with native target and effect readback. No tools.</summary>
public static class EventControlAction
{
    public const string Target = "event_target", Npc = "festival_npc", Shop = "festival_shop",
        Seat = "festival_seat", Egg = "festival_egg", EggRoute = "festival_egg_route";
    /// <summary>The one control while she sits on a festival seat: the game's own A on a seat stands her up
    /// (GameLocation.checkAction calls Farmer.StopSitting() before any festival interceptor).</summary>
    public const string Stand = "festival_stand";
    public const string TargetRoute = "event_target_route", NpcRoute = "festival_npc_route",
        ShopRoute = "festival_shop_route", SeatRoute = "festival_seat_route";
    public const string Interact = "event_interact", InteractRoute = "event_interact_route";
    public static readonly string[] Kinds = { Target, Npc, Shop, Seat, Egg, EggRoute, TargetRoute, NpcRoute, ShopRoute, SeatRoute,
        Interact, InteractRoute, Stand };
    public static bool IsApproach(string? kind) => kind is TargetRoute or NpcRoute or ShopRoute or SeatRoute or InteractRoute;
    public static bool IsRoute(string? kind) => kind == EggRoute || IsApproach(kind);
    public static string NativeKind(string kind) => IsRoute(kind) ? kind[..^6] : kind;
    public const int MaxApproachPresses = 512, MaxApproachTicks = 7200, ApproachMs = 120_000;
    public static int RouteBudget(int pathLength) => Math.Min(MaxApproachPresses, checked(pathLength * 4 + 12));
    public const int SegmentTiles = 4, SegmentTicks = 40;
    // Known geometry, no map-sized worst-case reservation. Six presses per
    // straight chunk cover bounded centering/correction; twelve cover the final
    // face/grab/A. Execution stops at this exact published cap; no refund/retry.
    public static int RouteBudget(IReadOnlyList<(int X, int Y)> path, (int X, int Y) start)
    {
        int segments = 0, index = 0;
        while (index < path.Count)
        {
            int end = SegmentEnd(path, index, start);
            start = path[end]; index = end + 1; segments++;
        }
        return Math.Min(MaxApproachPresses, checked(segments * 6 + 12));
    }
    public static int SegmentEnd(IReadOnlyList<(int X, int Y)> path, int index, (int X, int Y) start)
    {
        int dx = path[index].X - start.X, dy = path[index].Y - start.Y, end = index;
        if (Math.Abs(dx) + Math.Abs(dy) != 1) return end;
        while (end + 1 < path.Count && end - index + 1 < SegmentTiles
            && path[end + 1].X - path[end].X == dx && path[end + 1].Y - path[end].Y == dy) end++;
        return end;
    }
    public static bool IsKind(string? kind) => Kinds.Contains(kind, StringComparer.Ordinal);
    public const int MaxOffers = 6, MaxRouteTiles = 8, MaxRoutePresses = 40, MaxRouteTicks = 1200;
    // ADR 240: with the menu reader on, an event map routes up to eight distinct people and four of each other
    // kind (sixteen in all), and names up to 24 people and 24 press cells, nearest first, before any route exists.
    public const int MaxEventPeopleRoutes = 8, MaxEventKindRoutes = 4, MaxEventRoutes = 16, MaxEventSeen = 24;
    // Review R5 S7: a plan inherits the map's Action-cell scan for at most this long. The plans refresh every 180 ticks,
    // so a press cell the event adds in place on an empty tile is seen within about this age plus one sliced rescan.
    public const long CellScanMaxAgeMs = 10_000;

    public static bool TargetTile(string? sequence, int x, int y) => (sequence, x, y) is
        ("haleyBeach", 53, 8) or ("haleyBeach2", 35, 11);

    // ScopeValid is sampled from pinned Event + location + command/raw/sequence + phase.
    // Only the exact native target-A transition can change a command/sequence.
    public static bool Effect(EventActionFacts before, EventActionFacts now) =>
        before.ScopeValid && now.ScopeValid && before.Control && before.Kind == now.Kind && before.TargetId == now.TargetId
        && (before.Kind is Target or Interact || before.Sequence == now.Sequence && before.Control == now.Control
            && before.Command == now.Command && before.TargetX == now.TargetX && before.TargetY == now.TargetY
            && before.TimerPositive == now.TimerPositive)
        && before.Command >= 0 && (before.Kind switch
        {
            Target when before.Sequence == "haleyBeach" => TargetTile(before.Sequence, before.TargetX, before.TargetY) && now.Sequence == "haleyBeach2"
                && now.Control && now.Command == before.Command && now.TargetX == 35 && now.TargetY == 11,
            Target when before.Sequence == "haleyBeach2" => TargetTile(before.Sequence, before.TargetX, before.TargetY) && !now.Control && now.Sequence is null
                && now.Command == before.Command + 1,
            Interact => !before.ExpectedMenu && (now.ExpectedMenu && now.MenuType is not null
                || now.Command > before.Command || now.Sequence != before.Sequence || !now.Control),
            Npc => !before.ExpectedMenu && now.ExpectedMenu && now.MenuType == "DialogueBox",
            Shop => !before.ExpectedMenu && now.ExpectedMenu && now.MenuType == "ShopMenu",
            Seat => !before.Sitting && now.Sitting && now.MenuType is null,
            Stand => before.Sitting && !now.Sitting && now.MenuType is null,
            Egg or EggRoute => before.TimerPositive && now.TimerPositive && before.Present
                && !now.Present && now.Score == before.Score + 1 && now.MenuType is null,
            _ => false,
        });

    public static BgActionEffectVerdict Classify(EventActionFacts? before, EventActionFacts? now)
    {
        bool success = before is not null && now is not null && Effect(before, now);
        return new(success ? now!.Kind switch { Target => "EventTargetActivated", Npc => "DialogueOpened",
            Shop => "MenuOpened", Seat => "FestivalSeated", Stand => "FestivalStood", Interact => "EventInteractionObserved", _ => "FestivalEggCollected" }
            : BgHoldCompletion.InteractionUncertain, success, success ? null : "EVENT_EFFECT_UNVERIFIED", null,
            success && now!.Kind is Egg or EggRoute ? 1 : 0);
    }

    /// <summary>FIXB55 principle: every step must verify and clean up; a later fault is never hidden.</summary>
    public static bool RouteStep(bool completed, bool cleanup, bool samePin, string direction, int ticks,
        float dx, float dy, bool segment = false) => completed && cleanup && samePin
            && EventControlMove.Displacement(direction, ticks, dx, dy, segment ? SegmentTicks : EventControlMove.MaxTicks, slidePxPerTick: 0);
}

public sealed record EventActionFacts(string Kind, string TargetId, bool ScopeValid, string? Sequence,
    bool Control, int Command, int TargetX, int TargetY, bool TimerPositive = false, int Score = 0,
    bool Present = true, bool ExpectedMenu = false, string? MenuType = null, bool Sitting = false);
