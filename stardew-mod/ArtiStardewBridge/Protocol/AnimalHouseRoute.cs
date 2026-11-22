namespace ArtiStardewBridge.Protocol;

/// <summary>P13 travel vocabulary and bounded native-map planning. No game or input writes.</summary>
public static class AnimalHouseRoute
{
    public const string Kind = "animal_house_route";
    public const int MaxInputs = 96, MaxTicks = 7200, ExecutionMs = 120_000;
    // A partial route owes no interaction effect only if EVERY delivered step
    // was settled cardinal travel. Never infer this from the final pose alone.
    public static bool MovementOnly(bool cleanup, bool sameIdentity, bool acting, bool stopping, int inputs,
        IReadOnlyList<(string Kind, bool Verified, bool Cleanup)> trace)
        => cleanup && sameIdentity && !acting && !stopping && inputs is >= 1 and <= MaxInputs
            && trace.Count == inputs && trace.All(t => t.Verified && t.Cleanup
                && t.Kind is "up" or "right" or "down" or "left");
    // This allowance is reserved before publication and is also enforced by the mod.
    // A detour costing more stops honestly; it never borrows the next task's inputs.
    public static int Budget(int distance, bool exit, float speed = 5.3f)
    {
        if (!float.IsFinite(speed) || speed < 2 || speed > 12) return MaxInputs;
        int nominal = exit ? 6 : distance > 24 ? 36 : Math.Min(36, (distance + 3) / 4 + 8);
        // Farmer.getMovementSpeed uses elapsed milliseconds, speed and buffs.
        // Reserve the slow-speed holds before publication; never refund unused
        // allowance. The coordinator still enforces this offer's exact budget.
        int movement = exit ? 3 : distance > 24 ? 24 : Math.Max(1, nominal - 6);
        return Math.Min(MaxInputs, nominal - movement + (int)Math.Ceiling(movement * Math.Max(1, 5.3f / speed)));
    }

    public static bool Step(bool clean, bool samePin, string direction, int ticks, float dx, float dy)
        => clean && samePin && ticks is >= 1 and <= 60 && float.IsFinite(dx) && float.IsFinite(dy)
            && Math.Abs(dx) + Math.Abs(dy) > 0 && Math.Abs(dx) + Math.Abs(dy) <= ticks * 16
            && (direction switch { "up" => dx == 0 && dy < 0, "down" => dx == 0 && dy > 0,
                "left" => dy == 0 && dx < 0, "right" => dy == 0 && dx > 0, _ => false });

    // Reuse FIXB61/63's whole-map frontier. Parent reconstruction and reversal are
    // sliced too: no map-sized loop can run in one UpdateTicking.
    public static IEnumerable<int> Plan(int width, int height, (int X, int Y) start,
        IReadOnlyList<(int X, int Y)> goals, Func<int, int, bool> clear,
        Action<List<(int X, int Y)>?, (int X, int Y)> finish)
    {
        var search = new FestivalMapSearch(width, height, start);
        while (!search.Done) { search.Tick(clear, (_, _) => false, 1); yield return 0; }
        List<(int X, int Y)>? best = null; (int X, int Y) stand = default;
        foreach (var goal in goals)
        {
            if (!clear(goal.X, goal.Y) || !search.TryParent(goal, out _)) { yield return 0; continue; }
            var path = new List<(int X, int Y)>(); var at = goal;
            bool valid = true;
            while (at != start)
            {
                if (path.Count >= 512 || !clear(at.X, at.Y) || !search.TryParent(at, out var parent))
                { valid = false; break; }
                path.Add(at); at = parent; yield return 0;
            }
            if (!valid) continue;
            for (int l = 0, r = path.Count - 1; l < r; l++, r--)
            { (path[l], path[r]) = (path[r], path[l]); yield return 0; }
            if (best is null || path.Count < best.Count) { best = path; stand = goal; }
        }
        finish(best, stand);
    }
}
