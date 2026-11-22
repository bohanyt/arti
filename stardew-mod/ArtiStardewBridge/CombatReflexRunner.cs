using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge;

// Primitives v2 WS-G5: the production combat reflex loop (behind ModConfig.ReflexCombatEnabled, default OFF).
// One reflex_combat request -> CombatReflexMachine stepped once per UpdateTicking (before the background runtime's own
// UpdateTicking) -> at most one pad press at a time through ReflexPressDriver (swing = 1-frame X TOOL hold, face /
// approach / retreat = D-pad move of the reflex request) -> one verdict through the existing inbox, written only after
// every press is released and proved (bounded; else uncertain).
//   * STOP / lease loss / focus regain / pad panic / exceptions / the night save abort: release, then the verdict;
//   * Downed (killScreen / health 0) ends the reflex on its first tick (no further press; release only);
//   * pad A is never pressed (with a MeleeWeapon it is the special move, live PRB-8 H10);
//   * the heal leg is handed back: Aborted(heal_needed) + the food slot in the reflex payload.
// Game access goes through ICombatGame (ModEntry.ReflexG5.cs; a scripted fake in HoldRuntimeTests). Evidence: UNIT_TESTED.

/// <summary>Game reads the combat runner needs (game thread only; no writes).</summary>
internal interface ICombatGame
{
    long Tick { get; }
    bool WindowActive { get; }
    bool EventUp { get; }
    /// <summary>CombatMenuKind.None or Other (any menu / dialogue).</summary>
    string MenuKind { get; }
    string Location { get; }
    bool InMines { get; }
    int TimeOfDay { get; }
    int Health { get; }
    int MaxHealth { get; }
    bool KillScreen { get; }
    bool UsingTool { get; }
    bool WeaponHeld { get; }
    /// <summary>Standing pixel / 64 (fractional tiles).</summary>
    (float X, float Y) Position { get; }
    int Facing { get; }
    IReadOnlyList<MonsterView> Monsters();
    /// <summary>First edible inventory slot, -1 = none.</summary>
    int FoodSlot { get; }
    /// <summary>The Mines up-ladder stand tile (MineShaft.tileBeneathLadder), or null.</summary>
    (int X, int Y)? ExitTile { get; }
    int Money { get; }
    int ItemCount { get; }
    /// <summary>VolcanoDungeon (a location the time rule does not cover): combat refused.</summary>
    bool LocationRefused { get; }
    /// <summary>Game1.stats.MonstersKilled, -1 = unreadable.</summary>
    long MonstersKilledStat { get; }
    /// <summary>FIXB15 D-B13-3: the nav-grid cell of a tile (the stands' walkability rule); '.' when unknown.</summary>
    char NavCell(int x, int y);
    void Log(string line, bool warn = false);
}

/// <summary>A WS-G5 verdict ready for the inbox (CompleteReflexG5).</summary>
internal sealed record ReflexG5Outcome(string RequestId, string Verdict, bool CleanupConfirmed, int PressCount, bool EffectObserved,
    Dictionary<string, object> Reflex);

internal sealed class CombatReflexRunner
{
    public const int FinishBudgetTicks = 300;   // release proof + settle after the verdict (5 s)
    const string Tag = "REFLEX_COMBAT";

    private readonly ICombatGame game;
    private readonly Func<BackgroundMoveRuntime?> runtime;
    private readonly Func<bool> leaseHeld;
    private ControllerExecution controller = null!;
    private readonly Func<bool> stopLatched;
    private readonly ReflexPressDriver driver;
    private MenuStepCommandRequest? request;
    private CombatReflexMachine? machine;
    private CombatReflexArgs args;
    private int panicsAtStart;
    private bool panicSeen;
    private string? verdict;
    private string detail = "";
    private long verdictTick;
    private long startTick;
    private CombatPhase lastPhase = CombatPhase.Idle;
    private int moneyStart, itemsStart, moneyAtVerdict, itemsAtVerdict, healthAtVerdict;
    private ReflexG5Outcome? outcome;

    public CombatReflexRunner(ICombatGame game, Func<BackgroundMoveRuntime?> runtime, Func<bool> leaseHeld, Func<bool> stopLatched)
    {
        this.game = game;
        this.runtime = runtime;
        this.leaseHeld = leaseHeld;
        this.stopLatched = stopLatched;
        this.driver = new ReflexPressDriver(runtime, (line, warn) => game.Log(line, warn), Tag);
    }

    private (float X, float Y) startPosition;
    private long firstPressTick = -1;

    /// <summary>ADR 181: the tick of the first press this run armed (first held frame follows on the next tick).</summary>
    public void NotePressArmed() { if (this.firstPressTick < 0) this.firstPressTick = this.game.Tick; }

    public bool Active => this.request is not null;

    public string? RequestId => this.request?.RequestId;

    public CombatPhase Phase => this.machine?.Phase ?? CombatPhase.Idle;

    public ReflexPressDriver Driver => this.driver;

    /// <summary>Start one reflex for an admitted request (the caller ran CombatReflexAdmission + ReflexPadAdmission).</summary>
    public void Start(MenuStepCommandRequest req, CombatReflexArgs a)
    {
        if (this.Active)
            throw new InvalidOperationException("combat reflex already active");
        var slot = this.runtime()?.ExecutionSlot ?? throw new InvalidOperationException("controller_unadmitted");
        this.controller = slot.BindController(req, "combat", () => this.Phase == CombatPhase.Retreat ? "retreat" : "fight", this, this.EndController,
            this.BeforeRuntimeTicking, this.AfterRuntimeTicking, () => this.Active && this.outcome is null);
        this.request = req;
        this.args = a;
        this.machine = new CombatReflexMachine(a);
        this.driver.Reset(req);
        this.panicsAtStart = this.runtime()?.PadPanics ?? 0;
        this.panicSeen = false;
        this.verdict = null;
        this.detail = "";
        this.startTick = this.game.Tick;
        this.lastPhase = CombatPhase.Idle;
        this.moneyStart = this.game.Money;
        this.startPosition = this.game.Position;
        this.firstPressTick = -1;
        this.itemsStart = this.game.ItemCount;
        this.outcome = null;
        this.game.Log($"{Tag} start request_id={req.RequestId} target_kind={a.TargetKind ?? "any"} max_s={a.MaxSeconds} hp_floor_pct={a.HpFloorPct} max_kills={a.MaxKills} "
            + $"retreat={a.Retreat} hp={this.game.Health}/{this.game.MaxHealth} time={this.game.TimeOfDay} loc={this.game.Location} tick={this.startTick}");
    }

    /// <summary>UpdateTicking, BEFORE BackgroundMoveRuntime.OnUpdateTicking: step the machine, request releases.</summary>
    public void BeforeRuntimeTicking()
    {
        if (!this.Active || this.outcome is not null)
            return;
        if (this.verdict is not null)
        {
            this.TickFinishing();
            return;
        }
        try
        {
            if (!this.controller.Check()) return;
            this.Step();
        }
        catch (Exception ex)
        {
            this.game.Log($"{Tag} fault {ex.GetType().Name}: {ex.Message}", warn: true);
            this.driver.Release("REFLEX_EXCEPTION");
            this.BeginFinish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonException), "exception:" + ex.GetType().Name);
        }
    }

    /// <summary>UpdateTicking, AFTER BackgroundMoveRuntime.OnUpdateTicking: arm the press the machine asked for.</summary>
    public void AfterRuntimeTicking()
    {
        if (!this.Active || this.outcome is not null || this.verdict is not null)
            return;
        try
        {
            if (!this.controller.Check()) return;
            this.driver.ArmDue();
            if (this.driver.Armed > 0) this.NotePressArmed();
        }
        catch (Exception ex)
        {
            this.game.Log($"{Tag} arm fault {ex.GetType().Name}: {ex.Message}", warn: true);
            this.driver.Release("ARM_EXCEPTION");
        }
    }

    public bool OwnsTerminal(BackgroundMoveTerminal? terminal) =>
        terminal is not null && this.request is { } req && string.Equals(terminal.Request.RequestId, req.RequestId, StringComparison.Ordinal);

    public void OnPressTerminal(BackgroundMoveTerminal terminal) => this.driver.OnTerminal(terminal);

    public ReflexG5Outcome? TakeOutcome()
    {
        ReflexG5Outcome? o = this.outcome;
        if (o is null)
            return null;
        this.outcome = null;
        this.request = null;
        this.machine = null;
        this.driver.Clear();
        return o;
    }

    /// <summary>GameLoop.DayEnding / Saving (no UpdateTicking while saving): release every press now; Aborted(interrupted).</summary>
    public void AbortForSave(string evt)
    {
        if (!this.Active || this.outcome is not null)
            return;
        this.driver.ReleaseForSave(evt);
        if (this.verdict is not null)
            return;
        this.MarkVerdict(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonInterrupted), "save:" + evt);
    }

    /// <summary>ReturnedToTitle / dispose: release now and report uncertain.</summary>
    public void Abort(string reason)
    {
        if (!this.Active || this.outcome is not null)
            return;
        this.driver.Release("REFLEX_LIFECYCLE");
        if (this.verdict is null)
            this.MarkVerdict(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonInterrupted), "lifecycle:" + reason);
        this.Complete(cleanup: false);
    }

    private void EndController(string reason)
    {
        if (reason.StartsWith("save:", StringComparison.Ordinal)) { this.AbortForSave(reason[5..]); return; }
        this.driver.Release(reason); this.runtime()?.NeutralizeController(reason);
        if (this.verdict is null) this.BeginFinish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonInterrupted), reason);
    }

    private void Step()
    {
        CombatReflexMachine m = this.machine!;
        BackgroundMoveRuntime? rt = this.runtime();
        if (rt is not null && rt.PadPanics != this.panicsAtStart)
            this.panicSeen = true;
        (float x, float y) = this.game.Position;
        (int X, int Y)? exit = this.game.ExitTile;
        CombatSnapshot snap = new(
            this.game.Tick,
            this.game.WindowActive,
            this.stopLatched() || this.panicSeen,
            this.leaseHeld(),
            this.game.EventUp,
            this.game.MenuKind,
            this.game.Location,
            this.game.InMines,
            this.game.TimeOfDay,
            this.game.Health,
            this.game.MaxHealth,
            this.game.KillScreen,
            this.game.UsingTool,
            this.game.WeaponHeld,
            x,
            y,
            this.game.Facing,
            this.game.Monsters(),
            this.game.FoodSlot,
            exit?.X,
            exit?.Y,
            this.driver.Pending,
            this.driver.Fault,
            this.game.LocationRefused,
            this.game.MonstersKilledStat,
            this.game.NavCell);
        CombatDecision d = m.Step(snap);
        switch (d.Action)
        {
            case CombatAction.Swing:
                this.driver.RequestHold(BgReflexPressKind.Swing, 1);
                break;
            case CombatAction.Step:
                this.driver.RequestStep(d.Dir, d.Ticks);
                break;
            case CombatAction.Release:
                this.driver.Release("REFLEX_RELEASE");
                break;
        }
        if (m.Phase != this.lastPhase)
        {
            this.game.Log($"{Tag} phase={m.Phase} tick=+{this.game.Tick - this.startTick} note={d.Note} hp={snap.Health}/{snap.MaxHealth}");
            this.lastPhase = m.Phase;
        }
        if (d.Verdict is { } v)
            this.BeginFinish(v, d.Note);
    }

    private void MarkVerdict(string v, string why)
    {
        this.verdict = v;
        this.detail = why;
        this.verdictTick = this.game.Tick;
        this.moneyAtVerdict = this.game.Money;
        this.itemsAtVerdict = this.game.ItemCount;
        this.healthAtVerdict = this.game.Health;
        CombatReflexMachine? m = this.machine;
        this.game.Log($"{Tag} verdict={v} detail={why} tick=+{this.verdictTick - this.startTick} kills={m?.Kills ?? 0} swings={m?.Swings ?? 0} "
            + $"steps={m?.Steps ?? 0} presses={this.driver.Armed} refused={this.driver.Refused} h={this.driver.HFrames} hp={this.healthAtVerdict}",
            warn: !v.StartsWith(ReflexContracts.MonsterKilled, StringComparison.Ordinal));
    }

    private void BeginFinish(string v, string why)
    {
        this.driver.Release("REFLEX_DONE");
        this.MarkVerdict(v, why);
        this.TickFinishing();
    }

    private void TickFinishing()
    {
        this.driver.Release("REFLEX_DONE");
        if (this.driver.Settled)
        {
            this.Complete(cleanup: this.driver.CleanupConfirmed);
            return;
        }
        if (this.game.Tick - this.verdictTick > FinishBudgetTicks)
        {
            this.game.Log($"{Tag} finish_unsettled pending={this.driver.Pending} busy={this.runtime()?.Busy}", warn: true);
            this.Complete(cleanup: false);
        }
    }

    private void Complete(bool cleanup)
    {
        CombatReflexMachine? m = this.machine;
        Dictionary<string, object> reflex = new(StringComparer.Ordinal)
        {
            ["validity"] = this.controller.Receipt,
            ["verdict"] = this.verdict ?? "",
            ["detail"] = this.detail,
            ["ticks"] = this.game.Tick - this.startTick,
            ["kills"] = m?.Kills ?? 0,
            ["kill_witness"] = new Dictionary<string, object>(StringComparer.Ordinal)
            {
                ["hp_zero_seen"] = m?.KillsWitnessedHp ?? 0, ["stats_monsters_killed_delta"] = m?.StatsKillsDelta ?? -1,
                ["vanished_unwitnessed"] = m?.VanishedUnwitnessed ?? 0,
            },
            ["killed_types"] = (m?.KilledTypes ?? Array.Empty<string>()).ToArray(),
            ["swings"] = m?.Swings ?? 0,
            ["steps"] = m?.Steps ?? 0,
            ["presses"] = this.driver.Armed,
            ["presses_refused"] = this.driver.Refused,
            ["releases_proved"] = this.driver.ReleasesProved,
            ["hp_start"] = m?.StartHealth ?? 0,
            ["hp_min"] = m is null || m.MinHealth == int.MaxValue ? 0 : m.MinHealth,
            ["hp_end"] = this.healthAtVerdict,
            ["max_hp"] = this.game.MaxHealth,
            ["at_exit"] = m?.AtExit == true,
            // ADR 181: reaction and speed from the result alone.
            ["start_tick"] = this.startTick,
            ["first_press_tick"] = this.firstPressTick,
            ["tiles_moved"] = Math.Round(Math.Sqrt(Math.Pow(this.game.Position.X - this.startPosition.X, 2)
                + Math.Pow(this.game.Position.Y - this.startPosition.Y, 2)), 2),
            // ADR 153 (fix87B): what this batch did to the fight, read from the monsters themselves.
            ["damage_dealt"] = m?.DamageDealt ?? 0,
        };
        if (m?.TargetId is { } tid)
            reflex["target"] = new Dictionary<string, object>(StringComparer.Ordinal)
            {
                ["id"] = tid, ["type"] = m.TargetType ?? "", ["hp_start"] = m.TargetHpStart, ["hp_end"] = m.TargetHpEnd,
                ["max_hp"] = m.TargetMaxHp, ["dist_start"] = Math.Round(m.TargetDistStart, 1), ["dist_end"] = Math.Round(m.TargetDistEnd, 1),
            };
        if (m is not null && m.HiddenTypes.Count > 0)
            reflex["hidden_types"] = m.HiddenTypes.ToArray();
        if (m?.RetreatReason is { } rr)
            reflex["retreat_reason"] = rr;
        if (m is not null && (m.Defends > 0 || m.DisengageRoute is not null))
        {
            // ADR 177: what the body did on its own way out (walk / walk in contact / defend) and how often it defended.
            reflex["defends"] = m.Defends;
            reflex["disengage_route"] = m.DisengageRoute ?? "";
        }
        if (m is not null && m.HealSlot >= 0)
            reflex["food_slot"] = m.HealSlot;
        if (this.verdict == ReflexContracts.Downed)
        {
            // The loss itself happens after the revive warp + PlayerKilled event (PRB0 §C5): ModEntry journals it later.
            reflex["downed"] = new Dictionary<string, object>(StringComparer.Ordinal)
            {
                ["money_start"] = this.moneyStart, ["items_start"] = this.itemsStart,
                ["money_at_verdict"] = this.moneyAtVerdict, ["items_at_verdict"] = this.itemsAtVerdict,
            };
        }
        bool killed = this.verdict is { } v && v.StartsWith(ReflexContracts.MonsterKilled, StringComparison.Ordinal);
        this.outcome = new ReflexG5Outcome(this.request!.Value.RequestId ?? "", this.verdict!, cleanup, this.driver.HFrames, killed, reflex);
        this.game.Log($"{Tag} complete request_id={this.outcome.RequestId} verdict={this.outcome.Verdict} cleanup={cleanup} h={this.driver.HFrames}");
    }
}
