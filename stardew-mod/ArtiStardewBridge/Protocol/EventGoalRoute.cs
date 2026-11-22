namespace ArtiStardewBridge.Protocol;

/// <summary>Bounded cardinal graph over native read-only safe cells; no guessed or remote edges.</summary>
public static class EventGoalRoute
{
    public static IReadOnlyList<(int X, int Y)>? Plan((int X, int Y) start, (int X, int Y) goal,
        Func<int, int, bool> clear)
    {
        if (!clear(goal.X, goal.Y)) return null;
        // At most 145 cells in the cardinal radius-eight diamond. This is a local search frontier,
        // never an observation-event queue or retained state store.
        var queue = new List<((int X, int Y) Tile, int Depth)>(145);
        var parent = new Dictionary<(int, int), (int, int)> { [start] = start };
        queue.Add((start, 0));
        for (int cursor = 0; cursor < queue.Count && cursor < 145; cursor++)
        {
            var (at, depth) = queue[cursor];
            if (at == goal)
            {
                var path = new List<(int, int)>();
                while (at != start) { path.Add(at); at = parent[at]; }
                path.Reverse(); return path;
            }
            if (depth >= EventControlAction.MaxRouteTiles) continue;
            foreach (var (dx, dy) in new[] { (0, 1), (1, 0), (0, -1), (-1, 0) })
            {
                var next = (at.X + dx, at.Y + dy);
                if (!parent.ContainsKey(next) && clear(next.Item1, next.Item2))
                { parent[next] = at; queue.Add((next, depth + 1)); }
            }
        }
        return null;
    }
}
