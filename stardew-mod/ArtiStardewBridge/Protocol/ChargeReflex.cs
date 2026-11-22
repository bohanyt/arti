namespace ArtiStardewBridge.Protocol;

// =====================================================================================================
// WS-G5 tool charge (reflex_charge): pure decision layer. Decomp (PRB0_REFLEX_FACTS.md §T):
//   * Only a Hoe or a Watering Can with upgradeLevel >= 1 stays in UsingTool after BeginUsingTool; level-0 tools,
//     every Axe and every Pickaxe call EndUsingTool inside beginUsing (Tool.cs:633-636, Axe.cs:56-61,
//     Pickaxe.cs:75-80) -> they never charge (live PRB-3 H8: the Axe never charges).
//   * Charge loop (Game1.cs:13030-13050, runs while X is held, canReleaseTool, stamina >= 1, not a rod):
//     first held tick after the press sets toolHold = 600 * AnimationSpeedModifier; every later tick subtracts
//     ElapsedGameTime.Milliseconds (16 at the fixed 60 Hz step); <= 0 -> toolPowerIncrease(). Cap: toolPower
//     stops at upgradeLevel + (Reaching ? 1 : 0). Live PRB-3 H7: Copper level 1 on held tick 40, capped, 3 tiles.
//   * Release (X up edge) -> useToolButtonReleased -> EndUsingTool (Game1.cs:13022-13025) -> the tool fires with
//     power = farmer.toolPower (Hoe.cs:60, WateringCan.cs:152) over Tool.tilesAffected (Tool.cs:914+).
// The production adapter (ChargeReflexRunner, behind ModConfig.ReflexChargeEnabled) holds ONE pad X press on the G0
// path and requests its release on the first tick toolPower >= target.
// =====================================================================================================

public static class ChargeToolKind
{
    public const string Hoe = "hoe";
    public const string WateringCan = "can";
    public const string Axe = "axe";
    public const string Pickaxe = "pickaxe";
    public const string Other = "other";
}

/// <summary>
/// Per-tick facts. <paramref name="PadPending"/> = the X press is armed and its terminal has not arrived;
/// <paramref name="PadDown"/> = the press is issued and not Suppressed yet (frames are being delivered);
/// <paramref name="PadFault"/> = the press ended without delivering a frame (refused / uncertain before the press).
/// </summary>
public readonly record struct ChargeSnapshot(
    long Tick,
    bool WindowActive,
    bool StopRequested,
    bool LeaseHeld,
    bool EventUp,
    bool MenuOpen,
    string ToolKind,
    int UpgradeLevel,
    bool Reaching,
    int ToolPower,
    bool UsingTool,
    float Stamina,
    bool PadPending = false,
    bool PadDown = false,
    string? PadFault = null);

public readonly record struct ChargeDecision(PadXIntent Pad, string? Verdict, string Note);

public enum ChargePhase { Idle, Holding, Settling, Done }

/// <summary>
/// Admission (chargeable, cap, stamina, idle) -> Holding: one pad X hold on the G0 path; held frames are counted only while
/// the press is down (PadDown) -> release on the first tick toolPower >= target (or at the held-frame budget / an abort) ->
/// Settling: the release fires the tool at the current toolPower (Hoe.cs:60, WateringCan.cs:152); the verdict follows
/// once the tool animation ended and the press terminal arrived. An abort after the press was down still fires the tool
/// (X release = EndUsingTool): the verdict is ChargeAborted(reason) with <see cref="PowerAtRelease"/> recorded.
/// </summary>
public sealed class ChargeReflexMachine
{
    public const int SettleBudget = 120;  // tool animation after the release (8 frames x 60 ms, Tool.cs:606-622) + terminal
    public const int ArmBudget = 120;     // press armed .. first delivered frame (N-R / N-H / latch / N0)

    private readonly ChargeReflexArgs args;
    private long startTick = -1;
    private long releaseTick;
    private int reachedPower;
    private bool holding;
    private bool pressSeen;
    private string? pendingVerdict;
    private string pendingNote = "";

    public ChargeReflexMachine(ChargeReflexArgs args) { this.args = args; }

    public ChargePhase Phase { get; private set; } = ChargePhase.Idle;
    public string? Verdict { get; private set; }
    public string Detail { get; private set; } = "";
    /// <summary>Delivered held frames (PadDown ticks).</summary>
    public int HeldTicks { get; private set; }
    /// <summary>toolPower on the release tick (-1 = never released by the reflex); the tool fires with this power.</summary>
    public int PowerAtRelease { get; private set; } = -1;
    public int MaxPowerSeen { get; private set; }
    /// <summary>The X press delivered at least one frame (an abort after this still fired the tool).</summary>
    public bool PressDelivered => this.pressSeen;

    public static bool IsChargeable(string kind, int upgradeLevel) =>
        (kind == ChargeToolKind.Hoe || kind == ChargeToolKind.WateringCan) && upgradeLevel >= 1;

    public static int Cap(int upgradeLevel, bool reaching) => upgradeLevel + (reaching ? 1 : 0);

    /// <summary>
    /// Held ticks (counting the press tick as tick 0) after which toolPower first equals <paramref name="level"/>:
    /// tick 1 arms toolHold, then ceil(holdMs / msPerTick) more ticks per level, +1 arm tick per extra level.
    /// At 16 ms/tick and modifier 1: level 1 on tick 39 (~650 ms), level 2 on tick 78. Live PRB-3 (H7): level 1 on held
    /// tick 40 (the arm lands on held tick 2).
    /// </summary>
    public static int TicksToLevel(int level, float animationSpeedModifier = 1f, int msPerTick = 16)
    {
        if (level <= 0)
            return 0;
        int remaining = (int)(600f * animationSpeedModifier);
        int perLevel = 0;   // decrements until toolHold <= 0 (Game1.cs:13045-13048)
        while (remaining > 0)
        {
            remaining -= msPerTick;
            perLevel++;
        }
        // tick 0 = press (BeginUsingTool, returns early); tick 1 arms; decrements on ticks 2..(1+perLevel)
        return level * (1 + perLevel);
    }

    /// <summary>Tiles a release at <paramref name="power"/> affects (Tool.tilesAffected, power+1 internally).</summary>
    public static int TilesForPower(int power) => power switch
    {
        <= 0 => 1,
        1 => 3,
        2 => 5,
        3 => 9,
        4 => 18,
        _ => 25,
    };

    public ChargeDecision Step(in ChargeSnapshot s)
    {
        if (this.Phase == ChargePhase.Done)
            return new ChargeDecision(PadXIntent.None, null, "done");
        if (this.startTick < 0)
        {
            this.startTick = s.Tick;
            if (!IsChargeable(s.ToolKind, s.UpgradeLevel))
                return this.Finish(ReflexContracts.Verdict(ReflexContracts.ChargeAborted, ReflexContracts.ReasonNotChargeable), "not_chargeable");
            string? bad = this.args.Validate(s.UpgradeLevel, s.Reaching);
            if (bad == ReflexContracts.ReasonAboveCap)
                return this.Finish(ReflexContracts.Verdict(ReflexContracts.ChargeAborted, ReflexContracts.ReasonAboveCap), "above_cap");
            if (bad != null)
                throw new ArgumentException(bad);
            string? refuse = this.Interrupt(s) ?? (s.Stamina < 1f ? ReflexContracts.ReasonStaminaLow : null)
                ?? (s.UsingTool || s.PadPending ? ReflexContracts.ReasonInterrupted : null);
            if (refuse != null)
                return this.Finish(ReflexContracts.Verdict(ReflexContracts.ChargeAborted, refuse), "refused:" + refuse);
            this.Phase = ChargePhase.Holding;
            this.holding = true;
            return new ChargeDecision(PadXIntent.Hold, null, "press");
        }

        this.MaxPowerSeen = Math.Max(this.MaxPowerSeen, s.ToolPower);
        if (this.Phase == ChargePhase.Holding)
        {
            string? abort = this.Interrupt(s);
            if (abort != null)
                return this.pressSeen
                    ? this.Release(s, ReflexContracts.Verdict(ReflexContracts.ChargeAborted, abort), "abort:" + abort)
                    : this.Finish(ReflexContracts.Verdict(ReflexContracts.ChargeAborted, abort), "abort_before_press:" + abort);
            if (s.PadFault is { } fault && !this.pressSeen)
                return this.Finish(ReflexContracts.Verdict(ReflexContracts.ChargeAborted, ReflexContracts.ReasonPadRefused), "press_refused:" + fault);
            if (s.PadDown)
            {
                this.pressSeen = true;
                this.HeldTicks++;
            }
            if (!this.pressSeen)
            {
                long t = s.Tick - this.startTick;
                if (t > ArmBudget || (!s.PadPending && t > 2))
                    return this.Finish(ReflexContracts.Verdict(ReflexContracts.ChargeAborted, ReflexContracts.ReasonPadRefused), "press_not_delivered");
                return new ChargeDecision(PadXIntent.Hold, null, "wait_press");
            }
            if (s.ToolPower >= this.args.Level)
                return this.Release(s, ReflexContracts.ChargeReachedVerdict(Math.Min(s.ToolPower, ReflexContracts.ChargeLevelMax)),
                    $"release power={s.ToolPower}");
            if (!s.PadDown)
                // The press ended without our release (gate refused mid-hold, plan exhausted): the tool fired at this power.
                return this.Release(s, ReflexContracts.Verdict(ReflexContracts.ChargeAborted, ReflexContracts.ReasonInterrupted),
                    $"press_ended power={s.ToolPower}");
            if (this.HeldTicks > 3 && !s.UsingTool)
                return this.Release(s, ReflexContracts.Verdict(ReflexContracts.ChargeAborted, ReflexContracts.ReasonInterrupted), "tool_use_ended_early");
            if (this.HeldTicks >= this.args.HoldBudgetTicks)
                return this.Release(s, ReflexContracts.Verdict(ReflexContracts.ChargeAborted, ReflexContracts.ReasonTimeout), $"timeout power={s.ToolPower}");
            return new ChargeDecision(PadXIntent.Hold, null, $"hold power={s.ToolPower}");
        }

        // Settling: the release fired the tool; the verdict waits for the animation and the press terminal (bounded).
        bool settled = !s.UsingTool && !s.PadPending;
        if (settled || s.Tick - this.releaseTick > SettleBudget)
            return this.Finish(this.pendingVerdict!, this.pendingNote + (settled ? " settled" : " settle_timeout"));
        return new ChargeDecision(PadXIntent.None, null, "settling");
    }

    private ChargeDecision Release(in ChargeSnapshot s, string verdict, string note)
    {
        this.reachedPower = s.ToolPower;
        this.PowerAtRelease = s.ToolPower;
        this.holding = false;
        this.releaseTick = s.Tick;
        this.pendingVerdict = verdict;
        this.pendingNote = note;
        this.Phase = ChargePhase.Settling;
        return new ChargeDecision(PadXIntent.Release, null, note);
    }

    private string? Interrupt(in ChargeSnapshot s)
    {
        if (s.StopRequested) return ReflexContracts.ReasonStop;
        if (!s.LeaseHeld) return ReflexContracts.ReasonLeaseLost;
        if (s.WindowActive) return ReflexContracts.ReasonFocusRegain;
        if (s.EventUp) return ReflexContracts.ReasonEvent;
        if (s.MenuOpen) return ReflexContracts.ReasonForeignMenu;
        if (this.startTick >= 0 && this.Phase != ChargePhase.Idle && !IsChargeable(s.ToolKind, s.UpgradeLevel))
            return ReflexContracts.ReasonToolChanged;
        return null;
    }

    private ChargeDecision Finish(string verdict, string note)
    {
        PadXIntent pad = this.holding ? PadXIntent.Release : PadXIntent.None;
        this.holding = false;
        this.Verdict = verdict;
        this.Detail = note;
        this.Phase = ChargePhase.Done;
        return new ChargeDecision(pad, verdict, note);
    }

    /// <summary>The level of a ChargeReached verdict (0 otherwise).</summary>
    public int ReachedPower =>
        this.Verdict is { } v && v.StartsWith(ReflexContracts.ChargeReached, StringComparison.Ordinal) ? this.reachedPower : 0;
}

/// <summary>
/// The tiles a charged release affects: a pure port of Tool.tilesAffected (Tool.cs:914-1043) from the faced tool tile
/// (GetToolLocation / 64) and the facing, at toolPower <paramref name="power"/> (internally power + 1). Same order as
/// the decomp.
/// </summary>
public static class ChargeArea
{
    public static IReadOnlyList<(int X, int Y)> Tiles(int toolTileX, int toolTileY, int facing, int power)
    {
        int p = power + 1;
        // (fx, fy) along the facing; (sx, sy) across it with the decomp's sign convention (+x for facing 0/2, +y for 1/3).
        (int fx, int fy) = facing switch { 0 => (0, -1), 1 => (1, 0), 2 => (0, 1), _ => (-1, 0) };
        (int sx, int sy) = (Math.Abs(fy), Math.Abs(fx));
        var list = new List<(int X, int Y)> { (toolTileX, toolTileY) };
        if (p >= 6)
        {
            int cx = toolTileX + 2 * fx, cy = toolTileY + 2 * fy;
            list.Clear();
            for (int i = cx - 2; i <= cx + 2; i++)
                for (int j = cy - 2; j <= cy + 2; j++)
                    list.Add((i, j));
            return list;
        }
        (int X, int Y) At(int along, int across) => (toolTileX + along * fx + across * sx, toolTileY + along * fy + across * sy);
        if (p >= 2)
        {
            list.Add(At(1, 0));
            list.Add(At(2, 0));
        }
        if (p >= 3)
        {
            list.Add(At(3, 0));
            list.Add(At(4, 0));
        }
        if (p >= 4)
        {
            list.RemoveAt(list.Count - 1);
            list.RemoveAt(list.Count - 1);
            if (facing is 0 or 2)
            {
                // facing 0: (1,-2) (1,-1) (1,0) (-1,-2) (-1,-1) (-1,0); facing 2: (1,2) (1,1) (1,0) (-1,2) (-1,1) (-1,0)
                list.Add(At(2, 1)); list.Add(At(1, 1)); list.Add(At(0, 1));
                list.Add(At(2, -1)); list.Add(At(1, -1)); list.Add(At(0, -1));
            }
            else
            {
                // facing 1: (0,-1) (1,-1) (2,-1) (0,1) (1,1) (2,1); facing 3: (0,-1) (-1,-1) (-2,-1) (0,1) (-1,1) (-2,1)
                list.Add(At(0, -1)); list.Add(At(1, -1)); list.Add(At(2, -1));
                list.Add(At(0, 1)); list.Add(At(1, 1)); list.Add(At(2, 1));
            }
        }
        if (p >= 5)
        {
            for (int k = list.Count - 1; k >= 0; k--)
                list.Add((list[k].X + 3 * fx, list[k].Y + 3 * fy));
        }
        return list;
    }
}
