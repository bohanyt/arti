namespace ArtiStardewBridge.Protocol;

/// <summary>One multi-target BFS, sliced across game ticks. Closest reachable stand wins.
/// The frontier is bounded by the actual map, never by distance from the farmer.</summary>
public sealed class FestivalMapSearch
{
    public const int CellsPerTick = 256, MaxCells = 65536;
    private readonly int width, height;
    private readonly (int X, int Y) start;
    private readonly List<(int X, int Y)> queue = new();
    private readonly Dictionary<(int X, int Y), (int X, int Y)> parents = new();
    private readonly Dictionary<(int X, int Y), int> distances = new();
    private int? goalDistance;
    private int goalPreference = int.MinValue;
    private int cursor;
    public bool Done { get; private set; }
    public IReadOnlyList<(int X, int Y)>? Path { get; private set; }
    public (int X, int Y)? Goal { get; private set; }
    public int ExpandedLastTick { get; private set; }

    public FestivalMapSearch(int width, int height, (int X, int Y) start)
    {
        this.width = width; this.height = height; this.start = start;
        if (width <= 0 || height <= 0 || (long)width * height > MaxCells || !this.OnMap(start))
        { this.Done = true; return; }
        this.queue.Add(start); this.parents[start] = start; this.distances[start] = 0;
    }

    private bool OnMap((int X, int Y) t) => t.X >= 0 && t.Y >= 0 && t.X < this.width && t.Y < this.height;

    // Publication shares one bounded map frontier across all target poses.
    public IReadOnlyList<(int X, int Y)>? PathTo((int X, int Y) at)
    {
        if (!this.parents.ContainsKey(at)) return null;
        var path = new List<(int X, int Y)>();
        while (at != this.start) { path.Add(at); at = this.parents[at]; }
        path.Reverse(); return path;
    }

    public bool TryParent((int X,int Y) at, out (int X,int Y) parent) => this.parents.TryGetValue(at,out parent);

    public void Tick(Func<int, int, bool> clear, Func<int, int, bool> goal, Func<int, int, int> preference)
        => this.Tick(clear, goal, CellsPerTick, preference);

    public void Tick(Func<int, int, bool> clear, Func<int, int, bool> goal,
        int budget = CellsPerTick, Func<int, int, int>? preference = null)
    {
        this.ExpandedLastTick = 0;
        while (!this.Done && this.cursor < this.queue.Count && this.ExpandedLastTick < Math.Clamp(budget,1,CellsPerTick))
        {
            var at = this.queue[this.cursor++]; this.ExpandedLastTick++;
            int distance = this.distances[at];
            if (this.goalDistance is { } bestDistance && distance > bestDistance) { this.Done = true; break; }
            // Revalidate cells queued on earlier ticks, including moving blockers.
            if (at != this.start && !clear(at.X, at.Y)) continue;
            if (goal(at.X, at.Y))
            {
                int rank = preference?.Invoke(at.X, at.Y) ?? 0;
                if (this.Goal is null || rank > this.goalPreference)
                {
                    var path = new List<(int X, int Y)>(); this.Goal = at; this.goalPreference = rank; this.goalDistance = distance;
                    var step = at;
                    while (step != this.start) { path.Add(step); step = this.parents[step]; }
                    path.Reverse(); this.Path = path;
                }
                if (preference is null) { this.Done = true; return; }
            }
            if (this.goalDistance is not null) continue;
            foreach (var (dx, dy) in new[] { (0, -1), (-1, 0), (1, 0), (0, 1) })
            {
                var next = (at.X + dx, at.Y + dy);
                if (this.OnMap(next) && !this.parents.ContainsKey(next) && clear(next.Item1, next.Item2))
                { this.parents[next] = at; this.distances[next] = distance + 1; this.queue.Add(next); }
            }
        }
        if (this.cursor >= this.queue.Count) this.Done = true;
    }
}
