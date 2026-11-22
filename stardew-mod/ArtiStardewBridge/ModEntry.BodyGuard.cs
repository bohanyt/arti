using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

// =====================================================================================================
// ADR 179 body guard (game-coupled glue; the decision is the pure BodyGuardPolicy). Each UpdateTicked, after the
// reflex outcomes: on a Mines floor, contact + an HP drop within 10 s + no fight batch / input attempt in flight -> the
// mod admits a "body_guard" slot attempt and starts the existing disengage leg (CombatReflexMachine) by itself, whatever
// the runtime is doing (model wait, page build, an open closable menu is closed first; a question is never closed).
// The verdict is not an inbox result: it is published in ui.context combat.body_guard for the runtime's next read and
// the attempt is neutralized through the same release proof as every reflex. STOP / lease loss cancel it (owned
// release), exactly like any other attempt.
// =====================================================================================================
public sealed partial class ModEntry
{
    private readonly BodyGuardPolicy bodyGuard = new();
    private string? bodyGuardRequestId;
    private Dictionary<string, object>? bodyGuardFact;
    private long bodyGuardSeq;

    bool BodyGuardConfigured => this.config.ReflexCombatEnabled && this.PadPathConfigured;

    /// <summary>ADR 181: the running guard's own authority (its slot attempt; STOP / a fault end it).</summary>
    bool BodyGuardAuthority => this.bodyGuardRequestId is not null
        && this.commandInbox.ExecutionSlot.ActiveAttempt?.Client == ExecutionSlot.BodyGuardClient
        && !this.actionOwner.StopLatched && !this.actionOwner.RecordFaulted;

    private void TickBodyGuard()
    {
        if (!this.BodyGuardConfigured || !Context.IsWorldReady || Context.IsMainPlayer || Context.IsSplitScreen)
            return;
        CombatGame g = this.CombatGameAdapter;
        ExecutionSlot slot = this.commandInbox.ExecutionSlot;
        (float x, float y) = g.Position;
        IClickableMenu? menu = Game1.activeClickableMenu;
        string menuKind = menu is null ? BodyGuardMenu.None
            : menu is DialogueBox { isQuestion: true } || Game1.player?.CursorSlotItem is not null || menu is ReadyCheckDialog
                ? BodyGuardMenu.Question : BodyGuardMenu.Closable;
        var facts = new BodyGuardFacts(g.Tick, g.Location, g.InMines, g.Health, g.MaxHealth, x, y, g.Monsters(),
            g.ExitTile is not null, this.combatRunner?.Active == true || this.bodyGuardRequestId is not null,
            // ADR 180 (a72 J3893-J3900): never in the same tick as a runtime command or an owned release; the runtime's
            // input wins the slot and its release ack is never held hostage by the guard.
            slot.ActiveAttempt is null && !slot.InputBusy && !this.commandInbox.HasInflight,
            this.actionOwner.BoundRuntimeId.Length > 0 && !this.actionOwner.StopLatched && !this.actionOwner.RecordFaulted
                && !this.actionOwner.IsReleasing && !this.actionOwner.ReleaseRequested && this.ReflexG5BaseReady, g.EventUp, menuKind,
            g.ExitTile is { } stand && Math.Abs(stand.X + 0.5f - x) <= CombatReflexMachine.ExitArrive
                && Math.Abs(stand.Y + 0.5f - y) <= CombatReflexMachine.ExitArrive,
            Game1.player?.CurrentTool is not null);
        BodyGuardAction act = this.bodyGuard.Observe(facts);
        if (act == BodyGuardAction.None)
            return;
        if (act == BodyGuardAction.Unbound)
        {
            this.Monitor.Log($"BODY_GUARD refused hand_not_bound hp={g.Health}/{g.MaxHealth} loc={g.Location}", LogLevel.Warn);
            this.PublishBodyGuardFact("refused:hand_not_bound", null, g);
            return;
        }
        if (act == BodyGuardAction.CloseMenu)
        {
            // Co-op never pauses: a closable menu over a body being hit closes; the next tick starts the leg.
            Game1.exitActiveMenu();
            this.PublishBodyGuardFact("menu_closed", null, g);
            return;
        }
        CommandInboxIdentity pin = this.CurrentCommandIdentity();
        var request = new MenuStepCommandRequest
        {
            RuntimeInstanceId = this.runtimeInstanceId, SessionId = pin.SessionId, ExpectedPlayerId = pin.PlayerId,
            ExpectedScreenId = pin.ScreenId, ExpectedLocationId = pin.LocationId,
        };
        if (!slot.TryAdmitBodyGuard(request, this.actionOwner))
            return;
        MenuStepCommandRequest admitted = slot.ActiveAttempt!.Request;
        CombatReflexArgs args = ReflexCombatTarget.ToArgs(ReflexContracts.CombatDisengageMaxSeconds, ReflexContracts.CombatHpFloorPctDefault,
            1, true) with { Disengage = true, TargetKind = "monster" };
        string? refuse = CombatReflexAdmission.Refuse(this.SampleCombatAdmission(args.HpFloorPct), disengage: true)
            ?? this.backgroundMove?.ReflexPadAdmission(out _) ?? (this.backgroundMove is null ? "no_background_runtime" : null);
        if (refuse is not null)
        {
            this.Monitor.Log($"BODY_GUARD refused {refuse} hp={g.Health}/{g.MaxHealth} loc={g.Location}", LogLevel.Warn);
            slot.Neutralize(NowUnixMs(), "body_guard_refused");
            this.bodyGuard.Ended(g.Tick);
            this.PublishBodyGuardFact("refused:" + refuse, null, g);
            return;
        }
        this.bodyGuardRequestId = admitted.RequestId;
        this.Monitor.Log($"BODY_GUARD start request_id={admitted.RequestId} hp_from={this.bodyGuard.DropFrom} hp={g.Health}/{g.MaxHealth} "
            + $"drop_tick={this.bodyGuard.LastDropTick} tick={g.Tick} loc={g.Location}", LogLevel.Info);
        // ADR 189 (a76 S49599): a start that cannot bind never leaves the slot admitted.
        if (BodyGuardStart.Run(slot, () => this.CombatRunner.Start(admitted, args), NowUnixMs()) is { } failed)
        {
            this.Monitor.Log($"BODY_GUARD {failed} hp={g.Health}/{g.MaxHealth} loc={g.Location}", LogLevel.Warn);
            this.bodyGuardRequestId = null;
            this.bodyGuard.EndedWithoutMoving(g.Tick);
            this.PublishBodyGuardFact("refused:" + failed, null, g);
            return;
        }
        this.PublishBodyGuardFact("started", null, g);
    }

    /// <summary>FlushReflexG5Outcome: the body guard's own verdict never reaches the inbox (no runtime request).</summary>
    private bool TakeBodyGuardOutcome(ReflexG5Outcome o)
    {
        if (this.bodyGuardRequestId is null || o.RequestId != this.bodyGuardRequestId)
            return false;
        this.bodyGuardRequestId = null;
        CombatGame g = this.CombatGameAdapter;
        if (o.PressCount == 0)
            this.bodyGuard.EndedWithoutMoving(g.Tick);   // ADR 185: a no-press result never spends the cap
        else
            this.bodyGuard.Ended(g.Tick);
        this.Monitor.Log($"BODY_GUARD verdict={o.Verdict} cleanup={o.CleanupConfirmed} hp={g.Health}/{g.MaxHealth} loc={g.Location}",
            LogLevel.Info);
        this.PublishBodyGuardFact("settled", o, g);
        this.commandInbox.ExecutionSlot.Neutralize(NowUnixMs(), "body_guard_done");
        return true;
    }

    private void PublishBodyGuardFact(string status, ReflexG5Outcome? o, CombatGame g)
    {
        var fact = new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["seq"] = ++this.bodyGuardSeq, ["status"] = status, ["location"] = g.Location, ["tick"] = g.Tick,
            ["hp"] = g.Health, ["max_hp"] = g.MaxHealth, ["hp_from"] = this.bodyGuard.DropFrom,
            ["drop_tick"] = this.bodyGuard.LastDropTick, ["starts"] = this.bodyGuard.Starts,
        };
        if (o is not null)
        {
            fact["verdict"] = o.Verdict;
            fact["cleanup_confirmed"] = o.CleanupConfirmed;
            fact["reflex"] = o.Reflex;
        }
        this.bodyGuardFact = fact;
    }
}
