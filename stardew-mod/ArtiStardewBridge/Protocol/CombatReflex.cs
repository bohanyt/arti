namespace ArtiStardewBridge.Protocol;

// =====================================================================================================
// WS-G5 combat reflex: pure decision layer (no game, no SMAPI). The production adapter (CombatReflexRunner, behind
// ModConfig.ReflexCombatEnabled) reads a CombatSnapshot each UpdateTicking, calls CombatReflexMachine.Step and turns
// the decision into AT MOST ONE pad press on the G0 path at a time:
//   Step(dir, ticks) -> a D-pad hold of `ticks` frames (face = 1 frame; approach / retreat = a few frames)
//   Swing            -> a 1-frame pad X tap (MeleeWeapon swing; each swing needs a fresh X edge, PRB0 §C2)
//   Release          -> request the release of the press in flight (every terminal decision carries it)
// Pad A is never pressed by this machine: with a MeleeWeapon held, A = the special move (live PRB-8 H10).
// The heal leg is handed back to the runtime (verdict Aborted(heal_needed) + the food slot): the runtime runs the
// existing routine_eat chain and may re-arm. The retreat leg walks to the Mines up-ladder tile (tileBeneathLadder) and
// ends there with Retreated(reason); taking the ladder (ExitMine question) is the runtime's / G6's next step.
// Decomp facts: docs/stardew/primitives-v2/PRB0_REFLEX_FACTS.md (§C).
// =====================================================================================================

/// <summary>
/// One monster of the current location. <paramref name="TileX"/>/<paramref name="TileY"/> = standing pixel / 64 (fractional
/// tile units; the centre of tile (x, y) is (x + 0.5, y + 0.5)). Id = a session-stable identity token (no game id field).
/// <paramref name="Flying"/> = the game's own glider flag (Monster.isGlider, Data/Monsters field 4: Bat, Fly, Ghost, ...):
/// it ignores tile collision, so its tile may be a wall, a tree or the void (post-Sat F2: Bats, live M13).
/// </summary>
/// <paramref name="Hidden"/> = Monster.IsInvisible (a Duggy underground): GameLocation.damageMonster never damages it (ADR 153).
public readonly record struct MonsterView(string Id, string Type, float TileX, float TileY, int Health, int MaxHealth, bool Invincible,
    bool Flying = false, bool Hidden = false);

public static class CombatMenuKind
{
    public const string None = "none";
    public const string Other = "other";
}

/// <summary>
/// Per-tick facts. Player position in the same fractional tile units as <see cref="MonsterView"/>. <paramref name="ExitTileX"/>
/// / <paramref name="ExitTileY"/> = the up-ladder stand tile (MineShaft.tileBeneathLadder), null = unknown / not in the Mines.
/// <paramref name="PadPending"/> = a press is armed and its terminal has not arrived; <paramref name="PadFault"/> = the last
/// press ended without delivering a frame (refused / uncertain before the press).
/// </summary>
public readonly record struct CombatSnapshot(
    long Tick,
    bool WindowActive,
    bool StopRequested,
    bool LeaseHeld,
    bool EventUp,
    string MenuKind,
    string Location,
    bool InMines,
    int TimeOfDay,
    int Health,
    int MaxHealth,
    bool KillScreen,
    bool UsingTool,
    bool WeaponHeld,
    float TileX,
    float TileY,
    int Facing,                 // 0 up, 1 right, 2 down, 3 left
    IReadOnlyList<MonsterView> Monsters,
    int FoodSlot,               // -1: no edible food in the inventory
    int? ExitTileX,
    int? ExitTileY,
    bool PadPending = false,
    string? PadFault = null,
    // WS-G5 review minor 3: a location the time rule does not cover (VolcanoDungeon): combat is refused there.
    bool LocationRefused = false,
    // WS-G5 review minor 5: Game1.stats.MonstersKilled (the local farmer's counter, GameLocation.cs:4876); -1 = unreadable.
    long MonstersKilledStat = -1,
    // FIXB15 D-B13-3: the nav-grid cell of a tile ('.' open, 's' breakable stone, others blocked); null = no routing.
    Func<int, int, char>? NavCell = null);

/// <summary>
/// FIXB15 D-B13-3 (live V2B13ACC11b: the reflex's greedy approach pressed into the Stone (9,22) toward a Bug five times):
/// the reflex approach step from a bounded BFS over the nav-grid rule. A rock-free route first; else the route through
/// breakable stones: walk it, and when its next tile is the stone, report it (the runtime's clear_path breaks it); no
/// route at all = none.
/// </summary>
public static class CombatRoute
{
    public const int Radius = 14;
    public const int MaxNodes = 1200;
    static readonly (int Dx, int Dy, int Dir)[] Steps = { (0, -1, 0), (1, 0, 1), (0, 1, 2), (-1, 0, 3) };

    public readonly record struct Next(int Dir, (int X, int Y)? Rock, string Status);

    public static Next Plan((int X, int Y) start, (int X, int Y) monster, Func<int, int, char> cell)
    {
        var cache = new Dictionary<(int, int), char>();
        char C(int x, int y)
        {
            if (!cache.TryGetValue((x, y), out char c))
                cache[(x, y)] = c = x < 0 || y < 0 ? '#' : cell(x, y);
            return c;
        }
        bool Goal((int X, int Y) t) => Math.Max(Math.Abs(t.X - monster.X), Math.Abs(t.Y - monster.Y)) <= 1;
        if (Goal(start))
            return new Next(-1, null, "in_reach");
        foreach (bool stones in new[] { false, true })
        {
            var prev = new Dictionary<(int, int), (int, int)>();
            var frontier = new List<(int X, int Y)> { start };   // BFS by index (no Queue<> in Protocol/: guard)
            int head = 0;
            prev[start] = start;
            (int X, int Y)? found = null;
            while (head < frontier.Count && prev.Count < MaxNodes)
            {
                var t = frontier[head++];
                if (Goal(t)) { found = t; break; }
                foreach (var (dx, dy, _) in Steps)
                {
                    var n = (t.X + dx, t.Y + dy);
                    if (prev.ContainsKey(n) || Math.Max(Math.Abs(n.Item1 - start.X), Math.Abs(n.Item2 - start.Y)) > Radius)
                        continue;
                    char c = C(n.Item1, n.Item2);
                    bool open = c == '.' || (n.Item1, n.Item2) == monster;
                    if (!open && !(stones && c == 's'))
                        continue;
                    prev[n] = t;
                    frontier.Add(n);
                }
            }
            if (found is not { } end)
                continue;
            var path = new List<(int X, int Y)>();
            for (var t = end; t != start; t = prev[t])
                path.Add(t);
            path.Reverse();
            var first = path[0];
            int dir = Steps.First(s => start.X + s.Dx == first.X && start.Y + s.Dy == first.Y).Dir;
            if (stones && C(first.X, first.Y) == 's')
                return new Next(dir, first, "rock_next");
            return new Next(dir, null, stones ? "toward_rock" : "open");
        }
        return new Next(-1, null, "no_route");
    }

    /// <summary>FIXB15 review: in reach by tiles but not by the swing distance (a diagonal): an OPEN orthogonal neighbour
    /// that brings ARTI closer, or -1 (then swing from here; never a step into a wall corner).</summary>
    public static int CloserOpenNeighbour((float X, float Y) pos, (float X, float Y) monster, Func<int, int, char> cell)
    {
        int tx = (int)Math.Floor(pos.X), ty = (int)Math.Floor(pos.Y);
        double now = Math.Sqrt((monster.X - pos.X) * (monster.X - pos.X) + (monster.Y - pos.Y) * (monster.Y - pos.Y));
        int best = -1;
        double bestD = now;
        foreach (var (dx, dy, dir) in Steps)
        {
            int nx = tx + dx, ny = ty + dy;
            if (nx < 0 || ny < 0 || cell(nx, ny) != '.')
                continue;
            double d = Math.Sqrt((monster.X - (nx + 0.5)) * (monster.X - (nx + 0.5)) + (monster.Y - (ny + 0.5)) * (monster.Y - (ny + 0.5)));
            if (d < bestD - 0.05)
            {
                bestD = d;
                best = dir;
            }
        }
        return best;
    }
}

public enum CombatAction { None, Step, Swing, Release }

/// <summary>
/// ADR 177: the disengage leg's path to the up-ladder stand over the native nav grid (BFS, by index, bounded).
/// Guarded pass: an edge leaving contact never re-enters it, an edge inside contact never reduces separation
/// (the runtime's ADR 165 corridor rule, at game tick). Open pass: open cells only. Stone pass: the first
/// breakable stone on the path is reported (the runtime's route-clear seam), never walked into.
/// </summary>
public static class ExitRoute
{
    public const int Radius = 48;
    public const int MaxNodes = 6000;
    static readonly (int Dx, int Dy, int Dir)[] Steps = { (0, -1, 0), (1, 0, 1), (0, 1, 2), (-1, 0, 3) };

    /// <summary>Cell = first cell of the path; RunEnd = last cell of the first straight run (ADR 179: one hold goes there).</summary>
    public readonly record struct Next(int Dir, (int X, int Y)? Rock, string Status, int Length, (int X, int Y)? Cell,
        (int X, int Y)? RunEnd = null, int Run = 0);

    public static float Separation((int X, int Y) c, IReadOnlyList<MonsterView> threats)
    {
        float best = float.MaxValue;
        foreach (MonsterView m in threats)
        {
            float dx = m.TileX - (c.X + 0.5f), dy = m.TileY - (c.Y + 0.5f);
            best = Math.Min(best, (float)Math.Sqrt(dx * dx + dy * dy));
        }
        return best;
    }

    /// <param name="mode">"guarded" | "open" | "stones"</param>
    public static Next Plan((int X, int Y) start, (int X, int Y) goal, Func<int, int, char> cell,
        IReadOnlyList<MonsterView> threats, string mode, float contact)
    {
        if (start == goal)
            return new Next(-1, null, "at_goal", 0, start);
        var cache = new Dictionary<(int, int), char>();
        char C(int x, int y)
        {
            if (!cache.TryGetValue((x, y), out char c))
                cache[(x, y)] = c = x < 0 || y < 0 ? '#' : cell(x, y);
            return c;
        }
        bool Edge((int X, int Y) a, (int X, int Y) b)
        {
            if (mode != "guarded" || threats.Count == 0)
                return true;
            float sa = Separation(a, threats), sb = Separation(b, threats);
            return sa <= contact ? sb >= sa : sb > contact;
        }
        var prev = new Dictionary<(int, int), (int, int)> { [start] = start };
        var frontier = new List<(int X, int Y)> { start };
        int head = 0;
        while (head < frontier.Count && prev.Count < MaxNodes && !prev.ContainsKey(goal))
        {
            var t = frontier[head++];
            foreach (var (dx, dy, _) in Steps)
            {
                var n = (t.X + dx, t.Y + dy);
                if (prev.ContainsKey(n) || Math.Max(Math.Abs(n.Item1 - start.X), Math.Abs(n.Item2 - start.Y)) > Radius)
                    continue;
                char c = C(n.Item1, n.Item2);
                bool open = c == '.' || n == goal;
                if (!(open || mode == "stones" && c == 's') || !Edge(t, n))
                    continue;
                prev[n] = t;
                frontier.Add(n);
            }
        }
        if (!prev.ContainsKey(goal))
            return new Next(-1, null, "no_route", 0, null);
        // ADR 179 (a71 SMAPI: 52 holds for 23.5 tiles, the BFS parent path turned every 1-3 cells): among the shortest
        // paths take the one with the fewest turns. Distances to the goal over the cells the BFS reached, then walk
        // forward keeping the current direction whenever a neighbour is still one step closer.
        var toGoal = new Dictionary<(int, int), int> { [goal] = 0 };
        var back = new List<(int X, int Y)> { goal };
        for (int i = 0; i < back.Count; i++)
        {
            var t = back[i];
            foreach (var (dx, dy, _) in Steps)
            {
                var n = (t.X + dx, t.Y + dy);
                if (toGoal.ContainsKey(n) || !prev.ContainsKey(n) || !Edge(n, t))
                    continue;
                toGoal[n] = toGoal[t] + 1;
                back.Add(n);
            }
        }
        var path = new List<(int X, int Y)>();
        var cur = start;
        int lastDir = -1;
        while (cur != goal && toGoal.TryGetValue(cur, out int d) && path.Count < MaxNodes)
        {
            (int X, int Y)? pick = null;
            int pickDir = -1;
            foreach (var (dx, dy, sd) in Steps)
            {
                var n = (cur.X + dx, cur.Y + dy);
                if (!toGoal.TryGetValue(n, out int nd) || nd != d - 1 || !Edge(cur, n))
                    continue;
                if (pick is null || sd == lastDir) { pick = n; pickDir = sd; }
            }
            if (pick is not { } next)
                break;
            path.Add(next);
            lastDir = pickDir;
            cur = next;
        }
        if (path.Count == 0 || path[^1] != goal)
            return new Next(-1, null, "no_route", 0, null);
        (int X, int Y)? rock = null;
        foreach (var p in path)
            if (C(p.X, p.Y) == 's') { rock = p; break; }
        var first = path[0];
        int dir = Steps.First(st => start.X + st.Dx == first.X && start.Y + st.Dy == first.Y).Dir;
        int run = 1;
        while (run < path.Count && path[run].X - path[run - 1].X == first.X - start.X && path[run].Y - path[run - 1].Y == first.Y - start.Y
               && C(path[run].X, path[run].Y) != 's')
            run++;
        return new Next(dir, rock, rock is null ? "open" : "rock", path.Count, first, path[run - 1], run);
    }
}

public readonly record struct CombatDecision(CombatAction Action, int Dir, int Ticks, string? Verdict, string Note)
{
    public static CombatDecision Wait(string note) => new(CombatAction.None, -1, 0, null, note);
}

public enum CombatPhase { Idle, Fight, Retreat, Done }

/// <summary>The time rule (brief §3.1, owner window): pure, shared by the admission, the machine and ui.context.</summary>
public static class CombatTimeRule
{
    /// <summary>null = a combat reflex may START now; else the refusal reason (<see cref="ReflexContracts.ReasonTimeRule"/>).</summary>
    public static string? StartRefusal(bool inMines, int timeOfDay)
    {
        if (timeOfDay >= ReflexContracts.CombatHardStopTime)
            return ReflexContracts.ReasonTimeRule;
        if (inMines && (timeOfDay < ReflexContracts.CombatEarliestStartTime || timeOfDay >= ReflexContracts.CombatLatestStartTime))
            return ReflexContracts.ReasonTimeRule;
        return null;
    }

    /// <summary>In the Mines from 24:00 the fight ends through the retreat leg.</summary>
    public static bool RetreatDue(bool inMines, int timeOfDay) => inMines && timeOfDay >= ReflexContracts.CombatRetreatTime;

    /// <summary>01:00 everywhere: stop at once (pass-out at 02:00).</summary>
    public static bool HardStop(int timeOfDay) => timeOfDay >= ReflexContracts.CombatHardStopTime;
}

/// <summary>
/// Begin (admission terms re-checked) -> Fight: nearest monster within the radius; greedy D-pad approach; face (1-frame
/// D-pad); swing (1-frame X) only while !UsingTool and at the cadence; re-evaluated every tick (moved / died / retarget)
/// -> MonsterKilled(n) at max_kills (or when no monster is left in the radius after a kill). HP under hp_floor -> the
/// retreat leg (or Aborted(hp_floor) with retreat=false); HP under the hard floor with food -> Aborted(heal_needed);
/// Downed beats everything. One press at a time; one instance per request; ticks at 60 updates/s.
/// </summary>
public sealed class CombatReflexMachine
{
    public const float Reach = 1.35f;               // tiles, centre to centre (sword area ~1 tile ahead, MeleeWeapon.cs:695+)
    public const float ExitArrive = 0.45f;          // tiles from the up-ladder stand tile centre
    public const int StepTicks = 8;                 // one approach / retreat D-pad hold (~2/3 tile at speed 5)
    public const int FaceTicks = 1;                 // one D-pad frame turns the farmer (G1:12729-12775)
    public const int ApproachStuckTicks = 90;       // no 0.1-tile progress -> stuck
    public const int RetreatStuckTicks = 180;
    public const int RetreatBudgetTicks = 1800;     // 30 s to reach the ladder
    public const int PressBudgetTicks = 240;        // one press: arm .. terminal (N-R .. settle) bound
    public const int MaxConsecutiveRefusals = 3;
    // post-Sat F2 (live M13: 0 kills vs Bats, 8 x approach_no_route / approach_stuck): a flying target is never chased
    // into "no route". Beyond FlyingHoldRange ARTI walks a route to a walkable tile next to it when one exists; closer,
    // or with no route / a rock on it / no progress, she holds her tile, faces it and swings once it is in Reach (a Bat
    // dives at the farmer). Bounded: FlyingOutOfReachBudgetTicks without the target in Reach -> Aborted(stuck).
    public const float FlyingHoldRange = 2.5f;
    public const int FlyingOutOfReachBudgetTicks = 600;   // 10 s
    public const int FlyingFaceCadenceTicks = 10;         // hold: at most one 1-frame face press per 10 ticks
    // ADR 177 disengage leg. ContactRadius mirrors the runtime's CONTACT_RADIUS_TILES (Reach 1.35 + drift).
    public const float ContactRadius = 1.5f;
    public const int DefendNoProgressTicks = 240;         // 4 s of swings that do not lower the attacker's HP
    // ADR 179: one disengage hold walks the whole straight run (re-plan only at its end or when blocked), at most this
    // long so contact is re-read at least every 0.5 s. 12.1 ticks per tile at the measured 5.28 px/tick.
    public const int DisengageHoldMaxTicks = 30;
    public const float TicksPerTile = 12.1f;

    private readonly CombatReflexArgs args;
    private long startTick = -1;
    private string? startLocation;
    private string? targetId;
    private string? targetType;
    private int swingsAtTarget;
    private long lastSwingTick = -1000;
    private float bestDistance = float.MaxValue;
    private long bestDistanceTick;
    private long retreatStartTick;
    private bool pressRequested;
    private bool pressSeenPending;
    private long pressTick;
    private int refusalsInARow;
    private readonly List<string> killedTypes = new();
    private ((int, int), (int, int))? planKey;
    private CombatRoute.Next? planCached;
    private long statStart = -1, statLast = -1;
    private long flyingOutOfReachSince = -1;
    private bool flyingApproachGaveUp;
    private long lastFlyingFaceTick = -1000;
    private bool defending;
    private long defendSinceTick;
    private int defendTargetHp;
    private readonly HashSet<string> defendGaveUp = new(StringComparer.Ordinal);
    private int bestRouteLength = int.MaxValue;
    private long bestRouteTick;
    // ADR 153 (fix87B): what the batch did to its targets, so a stopped batch says whether the fight progressed.
    private readonly Dictionary<string, int> firstSeenHp = new(StringComparer.Ordinal);
    private readonly Dictionary<string, int> lastSeenHp = new(StringComparer.Ordinal);
    private readonly HashSet<string> swungAt = new(StringComparer.Ordinal);
    private readonly HashSet<string> hiddenSeen = new(StringComparer.Ordinal);

    public CombatReflexMachine(CombatReflexArgs args)
    {
        string? bad = args.Validate();
        if (bad != null)
            throw new ArgumentException(bad, nameof(args));
        this.args = args;
    }

    public CombatPhase Phase { get; private set; } = CombatPhase.Idle;
    public string? Verdict { get; private set; }
    public string Detail { get; private set; } = "";
    public int Kills { get; private set; }
    public IReadOnlyList<string> KilledTypes => this.killedTypes;
    public int Swings { get; private set; }
    public int Steps { get; private set; }
    public int MinHealth { get; private set; } = int.MaxValue;
    /// <summary>Kill witnesses (review minor 5): the target seen at health &lt;= 0, and the MonstersKilled counter delta.</summary>
    public int KillsWitnessedHp { get; private set; }
    public long StatsKillsDelta => this.statStart >= 0 && this.statLast >= 0 ? this.statLast - this.statStart : -1;
    /// <summary>A swung-at target vanished without either witness (not counted while the counter is readable).</summary>
    public int VanishedUnwitnessed { get; private set; }
    public int StartHealth { get; private set; }
    /// <summary>Food slot handed back with Aborted(heal_needed); -1 otherwise.</summary>
    public int HealSlot { get; private set; } = -1;
    /// <summary>The retreat leg ended on the up-ladder stand tile.</summary>
    public bool AtExit { get; private set; }
    public string? RetreatReason { get; private set; }
    /// <summary>post-Sat F2: Step ticks spent holding position for a flying target (diagnostics).</summary>
    public int FlyingHolds { get; private set; }
    /// <summary>ADR 177: defences started by a cornered disengage leg, and the last route the leg used.</summary>
    public int Defends { get; private set; }
    public string? DisengageRoute { get; private set; }
    public bool Defending => this.defending;
    /// <summary>ADR 153: HP the monsters this batch swung at lost (observed health drops; a killed one counts its last HP).</summary>
    public int DamageDealt { get; private set; }
    /// <summary>ADR 153: the last target: id, type, HP at first sight / last sight, max HP, distance at first sight / last.</summary>
    public string? TargetId => this.targetId ?? this.lastTargetId;
    public string? TargetType => this.targetType;
    public int TargetHpStart { get; private set; } = -1;
    public int TargetHpEnd { get; private set; } = -1;
    public int TargetMaxHp { get; private set; } = -1;
    public float TargetDistStart { get; private set; } = -1f;
    public float TargetDistEnd { get; private set; } = -1f;
    /// <summary>ADR 153: monster types this batch saw but could not hit now (Monster.IsInvisible: a burrowed Duggy).</summary>
    public IReadOnlyCollection<string> HiddenTypes => this.hiddenTypes;
    private readonly List<string> hiddenTypes = new();
    private string? lastTargetId;

    public CombatDecision Step(in CombatSnapshot s)
    {
        if (this.Phase == CombatPhase.Done)
            return CombatDecision.Wait("done");

        // Press bookkeeping: a press is done when its terminal arrived (pending seen, now clear) or its arm was refused.
        if (this.pressRequested && s.PadPending)
            this.pressSeenPending = true;
        if (this.pressRequested && !s.PadPending && (this.pressSeenPending || s.PadFault != null))
        {
            this.pressRequested = false;
            this.refusalsInARow = s.PadFault != null ? this.refusalsInARow + 1 : 0;
        }
        if (s.Health > 0)
            this.MinHealth = Math.Min(this.MinHealth, s.Health);
        if (s.MonstersKilledStat >= 0)
            this.statLast = s.MonstersKilledStat;
        this.Observe(s);

        // Downed beats everything (Farmer.cs:7657-7700: killScreen, then the revive warp + PlayerKilled event).
        if (s.KillScreen || s.Health <= 0)
            return this.Finish(ReflexContracts.Downed, "downed");

        if (this.startTick < 0)
            return this.Begin(s);

        string? abort = null;
        if (s.StopRequested) abort = ReflexContracts.ReasonStop;
        else if (!s.LeaseHeld) abort = ReflexContracts.ReasonLeaseLost;
        else if (s.WindowActive) abort = ReflexContracts.ReasonFocusRegain;
        else if (s.EventUp) abort = ReflexContracts.ReasonEvent;
        else if (!this.args.Escape && !this.args.Disengage && CombatTimeRule.HardStop(s.TimeOfDay)) abort = ReflexContracts.ReasonTimeRule;
        else if (s.MenuKind != CombatMenuKind.None) abort = ReflexContracts.ReasonForeignMenu;
        else if (s.Location != this.startLocation) abort = ReflexContracts.ReasonLocationChanged;
        else if (!s.WeaponHeld && (!this.args.Disengage || this.defending)) abort = ReflexContracts.ReasonToolChanged;
        else if (this.refusalsInARow >= MaxConsecutiveRefusals) abort = ReflexContracts.ReasonPadRefused;
        else if (this.pressRequested && s.Tick - this.pressTick > PressBudgetTicks) abort = ReflexContracts.ReasonPadRefused;
        if (abort != null)
            return this.Finish(ReflexContracts.AbortedVerdict(abort), "abort:" + abort);

        // Heal hand-off and the hp floor are checked every tick, also while a press is in flight.
        int pct = Pct(s);
        if (!this.args.Escape && !this.args.Disengage && pct < ReflexContracts.CombatHardFloorPct && s.FoodSlot >= 0)
        {
            this.HealSlot = s.FoodSlot;
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonHealNeeded), $"heal_needed hp={s.Health}/{s.MaxHealth}");
        }

        if (this.pressRequested)
            return CombatDecision.Wait("press_pending");

        return this.Phase switch
        {
            CombatPhase.Fight => this.StepFight(s, pct),
            CombatPhase.Retreat => this.StepRetreat(s),
            _ => CombatDecision.Wait("idle"),
        };
    }

    private CombatDecision Begin(in CombatSnapshot s)
    {
        this.startTick = s.Tick;
        this.startLocation = s.Location;
        this.StartHealth = s.Health;
        this.statStart = s.MonstersKilledStat;
        if (this.args.Disengage)
            return this.BeginDisengage(s);
        string? refuse = null;
        if (s.StopRequested) refuse = ReflexContracts.ReasonStop;
        else if (!s.LeaseHeld) refuse = ReflexContracts.ReasonLeaseLost;
        else if (s.WindowActive) refuse = ReflexContracts.ReasonFocusRegain;
        else if (s.EventUp) refuse = ReflexContracts.ReasonEvent;
        else if (s.MenuKind != CombatMenuKind.None) refuse = ReflexContracts.ReasonForeignMenu;
        else if (!s.WeaponHeld) refuse = ReflexContracts.ReasonToolChanged;
        else if (s.LocationRefused) refuse = ReflexContracts.ReasonLocationRefused;
        else if (this.args.Escape && !s.InMines) refuse = ReflexContracts.ReasonLocationRefused;
        else if (!this.args.Escape && CombatTimeRule.StartRefusal(s.InMines, s.TimeOfDay) is { } time) refuse = time;
        else if (!this.args.Escape && Pct(s) <= this.args.HpFloorPct) refuse = ReflexContracts.ReasonHpFloor;
        else if (s.UsingTool || s.PadPending) refuse = ReflexContracts.ReasonInterrupted;
        if (refuse != null)
            return this.Finish(ReflexContracts.AbortedVerdict(refuse), "refused:" + refuse);
        MonsterView? first = this.PickTarget(s);
        if (first == null)
            return this.Finish(ReflexContracts.NoTarget, this.hiddenTypes.Count > 0
                ? "only_hidden type=" + this.hiddenTypes[0] : "no_monster_in_radius");
        this.SetTarget(first.Value, s);
        this.Phase = CombatPhase.Fight;
        return this.StepFight(s, Pct(s));
    }

    private CombatDecision StepFight(in CombatSnapshot s, int pct)
    {
        if (this.args.Disengage && this.DefendEnd(s) is { } resumed)
            return resumed;
        if (!this.args.Escape && !this.args.Disengage && CombatTimeRule.RetreatDue(s.InMines, s.TimeOfDay))
            return this.StartRetreat(s, ReflexContracts.ReasonTimeRule);
        if (!this.args.Escape && !this.args.Disengage && pct < this.args.HpFloorPct)
        {
            if (!this.args.Retreat)
                return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonHpFloor), $"hp_floor hp={s.Health}/{s.MaxHealth}");
            return this.StartRetreat(s, ReflexContracts.ReasonHpFloor);
        }
        if (s.Tick - this.startTick > this.args.MaxSeconds * 60L)
            return this.Kills > 0
                ? this.Finish(ReflexContracts.MonsterKilledVerdict(this.Kills), "max_s")
                : this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonTimeout), "max_s_no_kill");

        if (this.targetId is { } tid && s.Monsters.Any(x => x.Id == tid && x.Health > 0 && x.Hidden))
        {
            // ADR 153: the target burrowed (Duggy): it is alive and cannot be hit now (damageMonster skips IsInvisible).
            // Not a kill, not a vanish; another visible monster is fought, else the batch says why it stopped.
            this.NoteHidden(this.targetType);
            this.targetId = null;
            MonsterView? visible = this.PickTarget(s);
            if (visible == null)
                return this.Kills > 0
                    ? this.Finish(ReflexContracts.MonsterKilledVerdict(this.Kills), "area_clear")
                    : this.Finish(ReflexContracts.NoTarget, "target_hidden type=" + (this.targetType ?? "monster"));
            this.SetTarget(visible.Value, s);
        }
        MonsterView? target = Find(s, this.targetId);
        if (target == null)
        {
            // Gone from the location's character list (or health <= 0) = killed: damageMonster removes it (GL:4783).
            // Only a monster this reflex swung at counts; anything else just loses the target.
            if (this.targetId != null && this.swingsAtTarget > 0)
            {
                // Second witness (review minor 5): the target seen at health <= 0, or the farmer's MonstersKilled counter
                // above the kills counted so far. Without a readable counter the disappearance alone counts (reported).
                string id = this.targetId;
                bool hpZero = s.Monsters.Any(x => x.Id == id && x.Health <= 0);
                bool counter = this.StatsKillsDelta > this.Kills;
                if (hpZero || counter || s.MonstersKilledStat < 0)
                {
                    this.Kills++;
                    this.killedTypes.Add(this.targetType ?? "");
                    if (hpZero)
                        this.KillsWitnessedHp++;
                    if (this.lastSeenHp.TryGetValue(id, out int remaining) && remaining > 0)
                        this.DamageDealt += remaining;   // ADR 153: the killing blow took what was left
                    this.lastSeenHp[id] = 0;
                    this.TargetHpEnd = 0;
                }
                else
                {
                    this.VanishedUnwitnessed++;
                }
            }
            this.targetId = null;
            if (this.args.Disengage)
                return this.ResumeRetreat(s, "attacker_dead");
            if (this.Kills >= this.args.MaxKills)
                return this.Finish(ReflexContracts.MonsterKilledVerdict(this.Kills), "max_kills");
            MonsterView? next = this.PickTarget(s);
            if (next == null)
                return this.Kills > 0
                    ? this.Finish(ReflexContracts.MonsterKilledVerdict(this.Kills), "area_clear")
                    : this.Finish(ReflexContracts.NoTarget, "target_lost");
            this.SetTarget(next.Value, s);
            target = next;
        }

        MonsterView m = target.Value;
        float dx = m.TileX - s.TileX;
        float dy = m.TileY - s.TileY;
        float dist = (float)Math.Sqrt(dx * dx + dy * dy);
        this.TargetDistEnd = dist;
        if (dist > this.args.RadiusTiles + 2f && this.args.TargetId == null)
        {
            // The target wandered off: pick again (or finish when nothing is near).
            this.targetId = null;
            MonsterView? next = this.PickTarget(s);
            if (next == null)
                return this.Kills > 0
                    ? this.Finish(ReflexContracts.MonsterKilledVerdict(this.Kills), "target_left_radius")
                    : this.Finish(ReflexContracts.NoTarget, "target_left_radius");
            this.SetTarget(next.Value, s);
            return CombatDecision.Wait("retarget");
        }
        if (s.UsingTool)
            return CombatDecision.Wait("swing_in_progress");
        int want = DirectionTo(dx, dy);
        if (m.Flying)
            return this.StepFlying(s, m, dist, want);
        CombatRoute.Next? plan = null;
        bool swingFromHere = false;
        if (dist > Reach && s.NavCell is { } navCell)
        {
            // FIXB15 D-B13-3 (+ review: one plan per (tile, target tile); a diagonal in tile reach never steps into a wall).
            var key = (((int)Math.Floor(s.TileX), (int)Math.Floor(s.TileY)), ((int)Math.Floor(m.TileX), (int)Math.Floor(m.TileY)));
            if (this.planKey != key || this.planCached is null)
            {
                this.planKey = key;
                this.planCached = CombatRoute.Plan(key.Item1, key.Item2, navCell);
            }
            plan = this.planCached;
            if (plan.Value.Status == "in_reach")
            {
                int closer = CombatRoute.CloserOpenNeighbour((s.TileX, s.TileY), (m.TileX, m.TileY), navCell);
                if (closer >= 0)
                    plan = new CombatRoute.Next(closer, null, "closer");
                else
                    swingFromHere = true;
            }
        }
        if (dist > Reach && !swingFromHere)
        {
            if (dist < this.bestDistance - 0.1f)
            {
                this.bestDistance = dist;
                this.bestDistanceTick = s.Tick;
            }
            else if (s.Tick - this.bestDistanceTick > ApproachStuckTicks)
                return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonStuck), "approach_stuck");
            int stepDir = want;
            if (plan is { } p)
            {
                // FIXB15 D-B13-3: never step into a known-blocked cell; a rock on the only route is the runtime's.
                if (p.Rock is { } rock)
                    return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonRouteBlocked),
                        $"route_blocked rock={rock.X},{rock.Y}");
                if (p.Status == "no_route")
                    return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonStuck), "approach_no_route");
                if (p.Dir >= 0)
                    stepDir = p.Dir;
            }
            return this.Press(new CombatDecision(CombatAction.Step, stepDir, StepTicks, null, $"approach d={dist:0.0}"), s.Tick);
        }
        this.bestDistance = dist;
        this.bestDistanceTick = s.Tick;
        return this.FaceAndSwing(s, want);
    }

    private CombatDecision FaceAndSwing(in CombatSnapshot s, int want)
    {
        if (s.Facing != want)
            return this.Press(new CombatDecision(CombatAction.Step, want, FaceTicks, null, "face"), s.Tick);
        if (s.Tick - this.lastSwingTick < this.args.SwingCadenceTicks)
            return CombatDecision.Wait("cadence");
        this.lastSwingTick = s.Tick;
        this.Swings++;
        this.swingsAtTarget++;
        if (this.targetId != null)
            this.swungAt.Add(this.targetId);
        return this.Press(new CombatDecision(CombatAction.Swing, want, 1, null, "swing"), s.Tick);
    }

    /// <summary>post-Sat F2: a flying target (no walkable route needed). In Reach: face + swing (same cadence, same caps).
    /// Out of Reach: route-approach while far and a route exists, else hold + face; bounded by the out-of-reach budget.</summary>
    private CombatDecision StepFlying(in CombatSnapshot s, MonsterView m, float dist, int want)
    {
        if (dist <= Reach)
        {
            this.flyingOutOfReachSince = -1;
            this.flyingApproachGaveUp = false;
            this.bestDistance = dist;
            this.bestDistanceTick = s.Tick;
            return this.FaceAndSwing(s, want);
        }
        if (this.flyingOutOfReachSince < 0)
        {
            this.flyingOutOfReachSince = s.Tick;
            this.bestDistance = dist;
            this.bestDistanceTick = s.Tick;
        }
        else if (s.Tick - this.flyingOutOfReachSince > FlyingOutOfReachBudgetTicks)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonStuck), $"flying_out_of_reach d={dist:0.0}");
        if (dist < this.bestDistance - 0.1f)
        {
            this.bestDistance = dist;
            this.bestDistanceTick = s.Tick;
        }
        else if (s.Tick - this.bestDistanceTick > ApproachStuckTicks)
            this.flyingApproachGaveUp = true;   // no progress (it circles): hold instead of chasing

        if (dist > FlyingHoldRange && !this.flyingApproachGaveUp)
        {
            int stepDir = -1;
            if (s.NavCell is { } navCell)
            {
                var key = (((int)Math.Floor(s.TileX), (int)Math.Floor(s.TileY)), ((int)Math.Floor(m.TileX), (int)Math.Floor(m.TileY)));
                if (this.planKey != key || this.planCached is null)
                {
                    this.planKey = key;
                    this.planCached = CombatRoute.Plan(key.Item1, key.Item2, navCell);
                }
                // Only a rock-free route is walked; a rock / no route / tile reach = hold (it flies to her anyway).
                if (this.planCached is { Status: "open", Dir: >= 0 } p)
                    stepDir = p.Dir;
            }
            else
                stepDir = want;
            if (stepDir >= 0)
                return this.Press(new CombatDecision(CombatAction.Step, stepDir, StepTicks, null, $"approach_flying d={dist:0.0}"), s.Tick);
        }
        this.FlyingHolds++;
        if (s.Facing != want && s.Tick - this.lastFlyingFaceTick >= FlyingFaceCadenceTicks)
        {
            this.lastFlyingFaceTick = s.Tick;
            return this.Press(new CombatDecision(CombatAction.Step, want, FaceTicks, null, "hold_face"), s.Tick);
        }
        return CombatDecision.Wait($"hold_flying d={dist:0.0}");
    }

    private CombatDecision StartRetreat(in CombatSnapshot s, string reason)
    {
        this.RetreatReason = reason;
        if (!s.InMines)
            return this.Finish(ReflexContracts.Verdict(ReflexContracts.Retreated, reason), "disengaged_outside_mines:" + reason);
        if (s.ExitTileX == null || s.ExitTileY == null)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonNoExit), "retreat_no_exit:" + reason);
        this.Phase = CombatPhase.Retreat;
        this.retreatStartTick = s.Tick;
        this.bestRouteLength = int.MaxValue;
        this.bestRouteTick = s.Tick;
        this.bestDistance = float.MaxValue;
        this.bestDistanceTick = s.Tick;
        return new CombatDecision(CombatAction.Release, -1, 0, null, "retreat:begin:" + reason);
    }

    private CombatDecision StepRetreat(in CombatSnapshot s)
    {
        if (this.args.Disengage && s.NavCell is { } navCell)
            return this.StepDisengage(s, navCell);
        if (s.ExitTileX == null || s.ExitTileY == null)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonNoExit), "exit_lost");
        if (s.Tick - this.retreatStartTick > RetreatBudgetTicks)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonStuck), "retreat_timeout:" + this.RetreatReason);
        if (s.UsingTool)
            return CombatDecision.Wait("retreat:swing_settle");
        float ex = s.ExitTileX.Value + 0.5f, ey = s.ExitTileY.Value + 0.5f;
        float dx = ex - s.TileX, dy = ey - s.TileY;
        float dist = (float)Math.Sqrt(dx * dx + dy * dy);
        if (Math.Abs(dx) <= ExitArrive && Math.Abs(dy) <= ExitArrive)
        {
            // On the stand tile: face the ladder (one tile up) and stop. Taking it (A -> ExitMine -> Leave) is not a reflex press.
            if (s.Facing != 0)
                return this.Press(new CombatDecision(CombatAction.Step, 0, FaceTicks, null, "retreat:face_ladder"), s.Tick);
            this.AtExit = true;
            return this.Finish(ReflexContracts.Verdict(ReflexContracts.Retreated, this.RetreatReason!), "at_exit:" + this.RetreatReason);
        }
        if (dist < this.bestDistance - 0.1f)
        {
            this.bestDistance = dist;
            this.bestDistanceTick = s.Tick;
        }
        else if (s.Tick - this.bestDistanceTick > RetreatStuckTicks)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonStuck), "retreat_stuck:" + this.RetreatReason);
        // Greedy, axis by axis: the larger offset first, short holds near the tile (no BFS inside the reflex).
        int dir;
        float along;
        if (Math.Abs(dx) > ExitArrive && (Math.Abs(dx) >= Math.Abs(dy) || Math.Abs(dy) <= ExitArrive))
        {
            dir = dx > 0 ? 1 : 3;
            along = Math.Abs(dx);
        }
        else
        {
            dir = dy > 0 ? 2 : 0;
            along = Math.Abs(dy);
        }
        int ticks = Math.Max(1, Math.Min(StepTicks, (int)Math.Floor(along * 12f)));
        return this.Press(new CombatDecision(CombatAction.Step, dir, ticks, null, $"retreat:walk d={dist:0.0}"), s.Tick);
    }

    // ------------------------------------------------------------------ ADR 177 disengage leg

    private CombatDecision BeginDisengage(in CombatSnapshot s)
    {
        string? refuse = null;
        if (s.StopRequested) refuse = ReflexContracts.ReasonStop;
        else if (!s.LeaseHeld) refuse = ReflexContracts.ReasonLeaseLost;
        else if (s.WindowActive) refuse = ReflexContracts.ReasonFocusRegain;
        else if (s.EventUp) refuse = ReflexContracts.ReasonEvent;
        else if (s.MenuKind != CombatMenuKind.None) refuse = ReflexContracts.ReasonForeignMenu;
        else if (s.LocationRefused || !s.InMines) refuse = ReflexContracts.ReasonLocationRefused;
        else if (s.UsingTool || s.PadPending) refuse = ReflexContracts.ReasonInterrupted;
        if (refuse != null)
            return this.Finish(ReflexContracts.AbortedVerdict(refuse), "refused:" + refuse);
        return this.StartRetreat(s, ReflexContracts.ReasonContact);
    }

    private static List<MonsterView> Threats(in CombatSnapshot s)
    {
        var list = new List<MonsterView>();
        foreach (MonsterView m in s.Monsters)
            if (m.Health > 0 && !m.Hidden)
                list.Add(m);
        return list;
    }

    private static MonsterView? InContact(in CombatSnapshot s)
    {
        MonsterView? best = null;
        float bestD = ContactRadius;
        foreach (MonsterView m in s.Monsters)
        {
            if (m.Health <= 0 || m.Hidden)
                continue;
            float dx = m.TileX - s.TileX, dy = m.TileY - s.TileY;
            float d = (float)Math.Sqrt(dx * dx + dy * dy);
            if (d <= bestD)
            {
                bestD = d;
                best = m;
            }
        }
        return best;
    }

    private static ExitRoute.Next RouteOut(in CombatSnapshot s, Func<int, int, char> cell, string mode)
    {
        var here = ((int)Math.Floor(s.TileX), (int)Math.Floor(s.TileY));
        var exit = (s.ExitTileX!.Value, s.ExitTileY!.Value);
        return ExitRoute.Plan(here, exit, cell, Threats(s), mode, ContactRadius);
    }

    private CombatDecision StepDisengage(in CombatSnapshot s, Func<int, int, char> cell)
    {
        if (s.ExitTileX == null || s.ExitTileY == null)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonNoExit), "exit_lost");
        if (s.Tick - this.startTick > this.args.MaxSeconds * 60L)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonTimeout), "disengage_timeout");
        if (s.UsingTool)
            return CombatDecision.Wait("disengage:swing_settle");
        float ex = s.ExitTileX.Value + 0.5f, ey = s.ExitTileY.Value + 0.5f;
        if (Math.Abs(ex - s.TileX) <= ExitArrive && Math.Abs(ey - s.TileY) <= ExitArrive)
        {
            if (s.Facing != 0)
                return this.Press(new CombatDecision(CombatAction.Step, 0, FaceTicks, null, "disengage:face_ladder"), s.Tick);
            this.AtExit = true;
            return this.Finish(ReflexContracts.Verdict(ReflexContracts.Retreated, ReflexContracts.ReasonContact), "at_exit:contact");
        }
        MonsterView? contact = InContact(s);
        ExitRoute.Next route = RouteOut(s, cell, "guarded");
        if (route.Status == "at_goal")
        {
            // ADR 208: standing on the native exit tile near its edge is not a missing route.
            // Centre within this already reached tile before facing/handing off to the native A/Leave.
            float dx = ex - s.TileX, dy = ey - s.TileY;
            int dir = Math.Abs(dx) >= Math.Abs(dy) ? (dx > 0 ? 1 : 3) : (dy > 0 ? 2 : 0);
            float centreDistance = Math.Max(Math.Abs(dx), Math.Abs(dy));
            int centreTicks = Math.Max(1, Math.Min(StepTicks, (int)Math.Floor(centreDistance * TicksPerTile)));
            return this.Press(new CombatDecision(CombatAction.Step, dir, centreTicks, null, "disengage:centre_stand"), s.Tick);
        }
        if (route.Status != "open")
        {
            if (contact is { } c && s.WeaponHeld && !this.defendGaveUp.Contains(c.Id))
            {
                // Cornered with an attacker in contact: the body defends until it dies, contact breaks or a way opens.
                this.DisengageRoute = "defend";
                this.defending = true;
                this.Defends++;
                this.defendSinceTick = s.Tick;
                this.defendTargetHp = Math.Max(0, c.Health);
                this.SetTarget(c, s);
                this.Phase = CombatPhase.Fight;
                return this.StepFight(s, Pct(s));
            }
            ExitRoute.Next open = RouteOut(s, cell, "open");
            if (open.Status == "open")
                route = open;   // nothing to defend with (or it does not die): moving beats standing in contact
            else
            {
                ExitRoute.Next stones = RouteOut(s, cell, "stones");
                if (stones.Rock is { } rock)
                    return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonRouteBlocked), $"route_blocked rock={rock.X},{rock.Y}");
                return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonRouteBlocked), "no_route_to_exit");
            }
        }
        this.DisengageRoute = contact is null ? "walk" : "walk_in_contact";
        if (route.Length < this.bestRouteLength)
        {
            this.bestRouteLength = route.Length;
            this.bestRouteTick = s.Tick;
        }
        else if (s.Tick - this.bestRouteTick > RetreatStuckTicks)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonStuck), "disengage_stuck");
        var end = route.RunEnd ?? route.Cell!.Value;
        float along = route.Dir is 1 or 3 ? Math.Abs(end.X + 0.5f - s.TileX) : Math.Abs(end.Y + 0.5f - s.TileY);
        int ticks = Math.Max(1, Math.Min(DisengageHoldMaxTicks, (int)Math.Ceiling(along * TicksPerTile)));
        return this.Press(new CombatDecision(CombatAction.Step, route.Dir, ticks, null,
            $"disengage:walk len={route.Length} run={route.Run}"), s.Tick);
    }

    /// <summary>While defending: the end of the defence (contact broken / no damage / a way open), or null.</summary>
    private CombatDecision? DefendEnd(in CombatSnapshot s)
    {
        if (!this.defending)
            return null;
        MonsterView? t = Find(s, this.targetId);
        if (t is not { } tv)
            return null;   // the kill bookkeeping witnesses it and resumes the walk
        if (tv.Hidden)
            return this.ResumeRetreat(s, "attacker_hidden");
        float dx = tv.TileX - s.TileX, dy = tv.TileY - s.TileY;
        if ((float)Math.Sqrt(dx * dx + dy * dy) > ContactRadius + 0.5f)
            return this.ResumeRetreat(s, "contact_broken");
        if (tv.Health < this.defendTargetHp)
        {
            this.defendTargetHp = tv.Health;
            this.defendSinceTick = s.Tick;
        }
        else if (s.Tick - this.defendSinceTick > DefendNoProgressTicks)
        {
            this.defendGaveUp.Add(tv.Id);
            return this.ResumeRetreat(s, "defend_no_progress");
        }
        if (s.NavCell is { } cell && s.ExitTileX != null && s.ExitTileY != null && RouteOut(s, cell, "guarded").Status == "open")
            return this.ResumeRetreat(s, "way_open");
        return null;
    }

    private CombatDecision ResumeRetreat(in CombatSnapshot s, string why)
    {
        this.defending = false;
        this.targetId = null;
        this.Phase = CombatPhase.Retreat;
        this.bestRouteLength = int.MaxValue;
        this.bestRouteTick = s.Tick;
        return CombatDecision.Wait("disengage:resume:" + why);
    }

    private CombatDecision Press(CombatDecision d, long tick)
    {
        this.pressRequested = true;
        this.pressSeenPending = false;
        this.pressTick = tick;
        if (d.Action == CombatAction.Step)
            this.Steps++;
        return d;
    }

    private void SetTarget(MonsterView m, in CombatSnapshot s)
    {
        this.targetId = m.Id;
        this.lastTargetId = m.Id;
        this.targetType = m.Type;
        this.swingsAtTarget = 0;
        this.bestDistance = float.MaxValue;
        this.bestDistanceTick = s.Tick;
        this.flyingOutOfReachSince = -1;
        this.flyingApproachGaveUp = false;
        // ADR 153: the readback describes the last target from the moment it was picked.
        float dx = m.TileX - s.TileX, dy = m.TileY - s.TileY;
        this.TargetDistStart = this.TargetDistEnd = (float)Math.Sqrt(dx * dx + dy * dy);
        this.TargetHpStart = this.TargetHpEnd = Math.Max(0, m.Health);
        this.TargetMaxHp = m.MaxHealth;
        if (!this.firstSeenHp.ContainsKey(m.Id))
            this.firstSeenHp[m.Id] = Math.Max(0, m.Health);
        this.lastSeenHp.TryAdd(m.Id, Math.Max(0, m.Health));
    }

    /// <summary>ADR 153: observed HP drops of the monsters this batch picked or swung at (never inferred from a press).</summary>
    private void Observe(in CombatSnapshot s)
    {
        foreach (MonsterView m in s.Monsters)
        {
            if (m.Id != this.targetId && !this.swungAt.Contains(m.Id))
                continue;
            int hp = Math.Max(0, m.Health);
            if (this.lastSeenHp.TryGetValue(m.Id, out int prev) && hp < prev)
                this.DamageDealt += prev - hp;
            this.lastSeenHp[m.Id] = hp;
            if (m.Id == this.targetId)
                this.TargetHpEnd = hp;
        }
    }

    private void NoteHidden(string? type)
    {
        string t = type ?? "monster";
        if (!this.hiddenTypes.Contains(t))
            this.hiddenTypes.Add(t);
    }

    private MonsterView? PickTarget(in CombatSnapshot s)
    {
        if (this.args.TargetId != null)
            return Find(s, this.args.TargetId);
        MonsterView? best = null;
        float bestD = float.MaxValue;
        foreach (MonsterView m in s.Monsters)
        {
            if (m.Health <= 0 || !ReflexContracts.KindMatches(this.args.TargetKind, m.Type))
                continue; // FIXB15 D-B13-3: only the objective's kind (a Bug is no slime)
            if (m.Hidden)
            {
                // ADR 153: a burrowed Duggy cannot be damaged (GameLocation.damageMonster: !monster.IsInvisible).
                if (this.hiddenSeen.Add(m.Id))
                    this.NoteHidden(m.Type);
                continue;
            }
            float dx = m.TileX - s.TileX, dy = m.TileY - s.TileY;
            float d = (float)Math.Sqrt(dx * dx + dy * dy);
            if (d <= this.args.RadiusTiles && d < bestD)
            {
                bestD = d;
                best = m;
            }
        }
        return best;
    }

    private static MonsterView? Find(in CombatSnapshot s, string? id)
    {
        if (id == null)
            return null;
        foreach (MonsterView m in s.Monsters)
            if (m.Id == id && m.Health > 0)
                return m;
        return null;
    }

    /// <summary>0 up, 1 right, 2 down, 3 left (Stardew facing). Ties go vertical.</summary>
    public static int DirectionTo(float dx, float dy)
    {
        if (Math.Abs(dx) > Math.Abs(dy))
            return dx > 0 ? 1 : 3;
        return dy > 0 ? 2 : 0;
    }

    public static int Pct(in CombatSnapshot s) => s.MaxHealth <= 0 ? 0 : (int)Math.Floor(100.0 * s.Health / s.MaxHealth);

    private CombatDecision Finish(string verdict, string note)
    {
        this.Verdict = verdict;
        this.Detail = note;
        this.Phase = CombatPhase.Done;
        return new CombatDecision(CombatAction.Release, -1, 0, verdict, note);
    }
}
