using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    private FestivalRun? festivalRun;
    private Event? festivalAttempted;
    private Dictionary<string, object>? festivalSummary;
    private int festivalRequestSerial;
    private FestivalRun? festivalCompletedRun;
    private string EventObject(object value) => this.eventObjects.GetValue(value, _ => new EventObjectId("ei" + ++this.nextEventObjectId)).Value;
    private string FestivalLifecycleId(Event ev) => "ecm:" + UiKeyTargetToken.Hash(System.Text.Json.JsonSerializer.Serialize(new {
        ev = this.EventObject(ev), festival = ev.id, location = this.EventObject(Game1.currentLocation!),
        player = this.EventObject(Game1.player!), identity = this.CurrentCommandIdentity() with { ContextGeneration = 0 } }));
    private sealed class FestivalRun
    {
        public Event Event = null!;
        public GameLocation Location = null!;
        public Farmer Player = null!;
        public CommandInboxIdentity Identity;
        public string Pin = "", Kind = "", Owner = "", EndReason = "", LifecycleId = "";
        public Dictionary<string, int> PrizeBaseline = new();
        public string? FishRequestId;
        public long Generation, StartTick, EndTick;
        public int StartScore, Verified, Replans, CastScore, FaultPanics;
        public bool Cleanup = true, Ending;
        public FestivalMapSearch? Search;
        public List<(Point Tile, Prop Prop)> Eggs = new();
        public List<Dictionary<string, object>> Effects = new();
        public List<string> CleanupFaults = new();
        public IReadOnlyList<(int X, int Y)>? IcePath;
        public Point IceStand, IceWaypoint, IceBeforeTile;
        public int IceFacing, IcePathIndex, IceTicks;
        public Vector2 IceBefore;
        public string IceDirection = "";
        public MenuStepCommandRequest? IceStep;
    }

    private bool FestivalReflexBusy => this.festivalRun is not null;
    private bool FestivalLeaseValid(FestivalRun? run = null)
    {
        var identity = this.CurrentCommandIdentity();
        return this.ReflexLeaseHeld && !this.actionOwner.ReleaseRequested
            && (run is null || run.Owner == this.actionOwner.OwnerId && run.Generation == this.actionOwner.Generation
                && identity == run.Identity with { ContextGeneration = identity.ContextGeneration }
                && ReferenceEquals(Game1.player, run.Player) && ReferenceEquals(Game1.currentLocation, run.Location));
    }
    private bool FestivalPhaseValid(FestivalRun run) => ReferenceEquals(Game1.CurrentEvent, run.Event)
        && (run.Kind == "eggHunt" ? this.EventActionsReady && NativeEventControl.Token() == run.Pin
            : this.ReflexFishReady && NativeFestivalFishing.Token() == run.Pin);

    private MenuStepCommandRequest FestivalRequest(string primitive, string pin)
    {
        var id = this.CurrentCommandIdentity();
        return new MenuStepCommandRequest("festival-auto-" + ++this.festivalRequestSerial,
            id.SessionId, id.PlayerId, null, "", 0, 0, NowUnixMs() + Game1.CurrentEvent.festivalTimer,
            MenuStepCommandInbox.OpInputOnce, Ticks: 1, ExpectedLocationId: id.LocationId, ExpectedScreenId: id.ScreenId,
            Family: MenuStepCommandInbox.FamilyWorldKey, Primitive: primitive, RuntimeInstanceId: id.RuntimeInstanceId,
            OwnerId: this.actionOwner.OwnerId, Generation: this.actionOwner.Generation, ControlSeq: this.actionOwner.ControlSeq,
            ContextGeneration: id.ContextGeneration, EventToken: pin);
    }

    // Before the ordinary fishing runner: acquisition never competes with an inbox mutation.
    private void TickFestivalBeforeRuntime()
    {
        try { this.TickFestivalCore(); }
        catch (Exception ex)
        {
            if (this.festivalRun is { } run) { run.Cleanup = false; this.EndFestival("exception:" + ex.GetType().Name); }
            this.Monitor.Log("FESTIVAL_REFLEX fault " + ex.GetType().Name, LogLevel.Warn);
        }
    }

    private void TickFestivalCore()
    {
        if (!this.config.FestivalReflexEnabled) return;
        this.UpdateFestivalResult();
        if (this.festivalRun is not { } run)
        {
            // A dialogue A has its own short lease. Never consume the one
            // contest attempt under that lease before Python releases it.
            if (!this.actionOwner.OwnerId.StartsWith("festival-", StringComparison.Ordinal)
                || !this.FestivalLeaseValid() || this.padAdapter?.SaveWindow == true || this.commandInbox.HasInflight
                || this.padAdapter?.CleanupFault is not null || this.backgroundMove?.FestivalContainmentFault is not null
                || this.backgroundMove?.Busy != false || this.backgroundMove.MenuDebtActive || this.ReflexBusy
                || this.MutationInflight || this.eggRun is not null
                || Game1.CurrentEvent is not Event ev || ReferenceEquals(ev, this.festivalAttempted)) return;
            string? egg = this.EventActionsReady && ev.id == "festival_spring13" && ev.playerControlSequenceID == "eggHunt"
                ? NativeEventControl.Token() : null;
            string? ice = this.ReflexFishReady && this.UiG3On ? NativeFestivalFishing.Token() : null;
            if (egg is null && ice is null) return;
            if (ice is not null && !ev.canPlayerUseTool()) return;
            this.festivalAttempted = ev;
            run = this.festivalRun = new FestivalRun { Event = ev, Location = Game1.currentLocation, Player = Game1.player,
                Identity = this.CurrentCommandIdentity(), Pin = egg ?? ice!, Kind = egg is not null ? "eggHunt" : "iceFishing",
                Owner = this.actionOwner.OwnerId, Generation = this.actionOwner.Generation, StartScore = Game1.player.festivalScore,
                StartTick = Game1.ticks, FaultPanics = this.backgroundMove.FestivalFaultPanics,
                LifecycleId = this.FestivalLifecycleId(ev), PrizeBaseline = FestivalInventory(Game1.player) };
            this.festivalSummary = null;
            this.Monitor.Log("FESTIVAL_REFLEX start kind=" + run.Kind + " score=" + run.StartScore, LogLevel.Info);
        }
        this.AggregateFestivalCleanupFaults(run);
        if (!run.Ending && (!run.Cleanup || !this.FestivalLeaseValid(run) || !this.FestivalPhaseValid(run)
            || this.padAdapter?.SaveWindow == true))
            this.EndFestival(run.CleanupFaults.Count > 0 ? run.CleanupFaults[0] : !this.FestivalLeaseValid(run) ? "stop_or_lease_lost"
                : run.Event.festivalTimer <= 0 ? "timer_end" : "phase_or_containment_changed");
        if (run.Ending)
        {
            if (this.eggRun is null && this.reflexRunner?.Active != true && this.backgroundMove?.Busy != true)
                this.FinishFestival();
            else if (Game1.ticks - run.EndTick > FishingReflexRunner.FinishBudgetTicks)
            { run.Cleanup = false; this.FinishFestival(); } // report debt; never disarm the underlying containment
            return;
        }
        if (run.Kind == "eggHunt")
        {
            if (this.eggRun is not null) return;
            this.PlanFestivalEgg(run);
        }
        else if (this.reflexRunner?.Active != true && this.backgroundMove?.Busy == false)
        {
            if (run.IcePath is not null) return;
            // canPlayerUseTool mutates previousFacingDirection; invoke only at each idle cast admission.
            if (!run.Event.canPlayerUseTool()) { this.EndFestival("tool_use_refused"); return; }
            float power = 0;
            for (int i = 30; i <= 95; i++)
            {
                var facts = this.SampleFishingFacts(i / 100f);
                if (facts.RodHeld && facts.LandingFishable) { power = i / 100f; break; }
            }
            if (power == 0) { this.PlanFestivalIce(run); return; }
            var admission = this.SampleFishAdmission(power) with { EventUp = false, PlayerFree = run.Player.CanMove,
                FreeSlots = 1, FacesWater = true }; // native landing, not adjacent water, admits a cast; no inventory gain in festivals.
            string? refusal = FishReflexAdmission.Refuse(admission) ?? this.backgroundMove.ReflexPadAdmission(out _);
            if (refusal is not null) { this.EndFestival(refusal); return; }
            var args = FishReflexArgs.Default with { CastPower = power, Treasure = false };
            var req = this.FestivalRequest(ReflexFishTarget.Primitive, run.Pin) with { ReflexFish = args };
            run.CastScore = run.Player.festivalScore;
            run.FishRequestId = req.RequestId;
            this.ReflexRunner.Start(req, args, () => this.FestivalLeaseValid(run) && !run.Ending && this.FestivalPhaseValid(run),
                () => run.Player.festivalScore);
        }
    }

    private int IceCastFacing(FestivalRun run, int x, int y)
    {
        if (x >= 79 || y >= 43) return -1; // Event.caughtFish's competition scoring area (:13161).
        for (int facing = 0; facing < 4; facing++)
            foreach (float power in new[] { .3f, .4f, .5f, .6f, .7f, .8f, .9f, .95f })
                if (FishCastGeometry.WindowFishable(x * 64 + 32, y * 64 + 32, facing, power, run.Player.FishingLevel,
                    (tx,ty) => run.Location.isTileOnMap(tx,ty) && run.Location.canFishHere() && run.Location.isTileFishable(tx,ty))) return facing;
        return -1;
    }

    private void PlanFestivalIce(FestivalRun run)
    {
        var layer = run.Location.Map.Layers[0];
        run.Search ??= new FestivalMapSearch(layer.LayerWidth, layer.LayerHeight, (run.Player.TilePoint.X, run.Player.TilePoint.Y));
        run.Search.Tick((x,y) => this.EventRouteCell(run.Location,run.Event,run.Player,x,y) && !run.Location.isWaterTile(x,y),
            (x,y) => this.IceCastFacing(run,x,y) >= 0);
        if (!run.Search.Done) return;
        if (run.Search.Goal is not { } goal || run.Search.Path is not { } path) { this.EndFestival("no_reachable_ice_cast"); return; }
        run.IceStand = new(goal.X,goal.Y); run.IceFacing = this.IceCastFacing(run,goal.X,goal.Y);
        run.IcePath = path; run.IcePathIndex = 0; run.Search = null;
    }

    // After the shared ledger starts. Movement/facing reuse TryArm; no native farmer writes.
    private void TickFestivalAfterRuntime()
    {
        if (this.festivalRun is not { Kind: "iceFishing", Ending: false, IcePath: not null } run
            || run.IceStep is not null || this.backgroundMove?.Busy != false) return;
        try
        {
            if (!this.FestivalLeaseValid(run) || !this.FestivalPhaseValid(run)) { this.EndFestival("ice_approach_pin_changed"); return; }
            var path = run.IcePath;
            Point waypoint = run.IcePathIndex < path.Count ? new(path[run.IcePathIndex].X,path[run.IcePathIndex].Y) : run.IceStand;
            if (!this.EventRouteCell(run.Location,run.Event,run.Player,waypoint.X,waypoint.Y)) { run.IcePath = null; run.Replans++; return; }
            var box = run.Player.GetBoundingBox();
            Vector2 delta = new(waypoint.X*64+32-(box.X+box.Width/2),waypoint.Y*64+32-(box.Y+box.Height/2));
            if (Math.Abs(delta.X) <= 6 && Math.Abs(delta.Y) <= 6 && run.Player.TilePoint == waypoint)
            {
                if (run.IcePathIndex < path.Count) { run.IcePathIndex++; return; }
                if (run.Player.FacingDirection == run.IceFacing) { run.IcePath = null; return; }
                var (dx,dy) = FishCastGeometry.FacingDelta(run.IceFacing); delta = new(dx*6,dy*6);
            }
            string direction = Math.Abs(delta.X) > 5 ? delta.X > 0 ? "right" : "left" : delta.Y > 0 ? "down" : "up";
            int ticks = Math.Clamp((int)(Math.Max(Math.Abs(delta.X),Math.Abs(delta.Y))/6),1,4);
            run.IceBefore = run.Player.Position; run.IceBeforeTile = run.Player.TilePoint; run.IceWaypoint = waypoint;
            run.IceDirection = direction; run.IceTicks = ticks;
            var req = this.FestivalRequest(MenuStepCommandInbox.PrimitiveMoveCardinal,run.Pin) with { Direction = direction,Ticks = ticks };
            run.IceStep = req;
            if (this.backgroundMove.TryArm(req) is { } refused) this.RouteFestivalIceTerminal(refused);
        }
        catch (Exception ex) { run.Cleanup = false; this.EndFestival("ice_approach_fault:"+ex.GetType().Name); }
    }

    private bool RouteFestivalIceTerminal(BackgroundMoveTerminal? terminal)
    {
        if (terminal is null || this.festivalRun is not { IceStep: not null } run || terminal.Request.RequestId != run.IceStep.Value.RequestId) return false;
        run.IceStep = null;
        bool clean = terminal.Kind is BackgroundTerminalKind.Completed or BackgroundTerminalKind.NotCompleted
            || terminal.Label.StartsWith("NOT_COMPLETED:RELEASED_EARLY",StringComparison.Ordinal)
            || terminal.ConsumedH == 0 && terminal.Label.StartsWith("RELEASED_BEFORE_LATCH:",StringComparison.Ordinal);
        clean |= this.backgroundMove?.FestivalReleaseConfirmed == true;
        run.Cleanup &= clean && this.backgroundMove?.PressOverrideOutstanding != true;
        Vector2 d = run.Player.Position-run.IceBefore;
        bool verified = EventControlAction.RouteStep(terminal.Kind == BackgroundTerminalKind.Completed,run.Cleanup,
            this.FestivalLeaseValid(run) && this.FestivalPhaseValid(run)
                && (run.Player.TilePoint == run.IceBeforeTile || run.Player.TilePoint == run.IceWaypoint),run.IceDirection,run.IceTicks,d.X,d.Y);
        if (!verified) this.EndFestival("ice_approach_unverified");
        return true;
    }

    private void PlanFestivalEgg(FestivalRun run)
    {
        if (run.Search is null)
        {
            run.Eggs.Clear();
            foreach (Prop prop in run.Event.festivalProps.Take(128))
            {
                Rectangle box = this.Helper.Reflection.GetField<Rectangle>(prop, "boundingRect").GetValue();
                if (box.Width == 64 && box.Height == 64 && box.X % 64 == 0 && box.Y % 64 == 0)
                    run.Eggs.Add((new Point(box.X / 64, box.Y / 64), prop));
            }
            var layer = run.Location.Map.Layers[0];
            run.Search = new FestivalMapSearch(layer.LayerWidth, layer.LayerHeight, (run.Player.TilePoint.X, run.Player.TilePoint.Y));
        }
        bool Clear(int x, int y) => this.EventRouteCell(run.Location, run.Event, run.Player, x, y);
        run.Search.Tick(Clear, (x, y) => Clear(x,y) && run.Eggs.Any(e => EventEggPoses(e.Tile).Any(p => p.Stand == new Point(x,y))
            && run.Event.festivalProps.Contains(e.Prop)
            && this.CaptureEventAction(e.Tile, route: true, inspectOnly: true).Target is not null),
            // Equal shortest paths prefer a nearby cluster, conserving future
            // walking time. This is a bounded heuristic, not an optimal tour.
            (x,y) => run.Eggs.Where(e => run.Event.festivalProps.Contains(e.Prop))
                .Sum(e => Math.Max(0, 8 - Math.Abs(e.Tile.X - x) - Math.Abs(e.Tile.Y - y))));
        if (!run.Search.Done) return;
        if (run.Search.Goal is not { } goal || run.Search.Path is not { } path)
        { run.Search = null; return; } // map changes / other players may open a route on later ticks
        var candidate = run.Eggs.FirstOrDefault(e => EventEggPoses(e.Tile).Any(p => p.Stand == new Point(goal.X,goal.Y))
            && run.Event.festivalProps.Contains(e.Prop) && this.CaptureEventAction(e.Tile, route: true, inspectOnly: true).Target is not null);
        if (candidate.Prop is null) { run.Search = null; run.Replans++; return; }
        var pose = EventEggPoses(candidate.Tile).First(p => p.Stand == new Point(goal.X,goal.Y));
        // Revalidate the entire proposed corridor before arm; each waypoint is rechecked during execution.
        run.Search = null;
        if (path.Any(t => !Clear(t.X, t.Y))) { run.Replans++; return; }
        var req = this.FestivalRequest(WorldInteractionTarget.Primitive, run.Pin) with { EventToken = null };
        this.eggRun = new EventEggRun { Request = req, Event = run.Event, Location = run.Location, Player = run.Player,
            Prop = candidate.Prop, Pin = run.Pin, Tile = candidate.Tile, Stand = pose.Stand, Facing = pose.Facing, Path = path,
            StartScore = run.Player.festivalScore, Started = Game1.ticks, ExecutionDeadline = req.DeadlineUnixMs,
            Automatic = true };
    }

    private bool FestivalFishOutcome(FishReflexOutcome outcome)
    {
        if (this.festivalRun is not { Kind: "iceFishing" } run || outcome.RequestId != run.FishRequestId) return false;
        run.FishRequestId = null;
        run.Cleanup &= outcome.CleanupConfirmed;
        bool caught = outcome.Verdict == ReflexContracts.FishCaught && outcome.CleanupConfirmed && this.FestivalPhaseValid(run)
            && this.FestivalLeaseValid(run) && run.Player.festivalScore == run.CastScore + 1;
        if (caught) run.Verified++;
        run.Effects.Add(new() { ["kind"] = "fish", ["verified"] = caught, ["score_before"] = run.CastScore,
            ["score_after"] = run.Player.festivalScore, ["cleanup"] = outcome.CleanupConfirmed, ["outcome"] = outcome.Verdict });
        if (!outcome.CleanupConfirmed || outcome.Verdict.StartsWith("Aborted(", StringComparison.Ordinal)
            || outcome.Verdict == ReflexContracts.FishCaught && !caught) this.EndFestival("fish_unverified:" + outcome.Verdict);
        return true;
    }

    private void EndFestival(string reason)
    {
        if (this.festivalRun is not { } run) return;
        if (!run.Ending) { run.Ending = true; run.EndReason = reason; run.EndTick = Game1.ticks; }
        this.backgroundMove?.RequestRelease("FESTIVAL_REFLEX_END");
        if (this.eggRun is { Automatic: true } egg) egg.ExecutionDeadline = 0;
    }

    private void AggregateFestivalCleanupFaults(FestivalRun run)
    {
        void Fault(string reason)
        {
            run.Cleanup = false;
            if (!run.CleanupFaults.Contains(reason)) run.CleanupFaults.Add(reason);
        }
        if (this.padAdapter?.CleanupFault is { } fault) Fault("adapter_cleanup_fault:" + fault);
        if (this.backgroundMove?.FestivalContainmentFault is { } containment) Fault("containment:" + containment);
        if (this.backgroundMove?.FestivalFaultPanics != run.FaultPanics) Fault("pad_panic");
    }

    private void FinishFestival()
    {
        if (this.festivalRun is not { } run) return;
        this.AggregateFestivalCleanupFaults(run);
        run.Cleanup &= this.backgroundMove?.Busy == false && this.backgroundMove.PressOverrideOutstanding == false
            && this.padAdapter?.RestoreWritePending != true;
        this.festivalSummary = this.FestivalFields(run, "end");
        this.festivalCompletedRun = run;
        this.Monitor.Log("FESTIVAL_REFLEX end " + System.Text.Json.JsonSerializer.Serialize(this.festivalSummary), LogLevel.Info);
        this.festivalRun = null;
    }

    private Dictionary<string, object> FestivalFields(FestivalRun run, string state) => new() {
        ["run_id"] = run.Pin,
        ["lifecycle_id"] = run.LifecycleId,
        ["state"] = state, ["kind"] = run.Kind, ["auto_started"] = true, ["start_score"] = run.StartScore,
        ["score"] = run.Player.festivalScore, ["verified_pickups"] = run.Verified, ["timer_ms"] = run.Event.festivalTimer,
        ["cleanup"] = run.Cleanup, ["cleanup_faults"] = run.CleanupFaults.ToArray(), ["reason"] = run.EndReason, ["replans"] = run.Replans,
        ["effects"] = run.Effects.TakeLast(16).ToArray(), ["result"] = "pending_native_result" };

    private void EnrichFestivalContext(Dictionary<string, object> snapshot)
    {
        if (!this.config.FestivalReflexEnabled && (this.EventActionsReady
                && Game1.CurrentEvent is { id: "festival_spring13", playerControlSequenceID: "eggHunt" }
            || this.ReflexFishReady && this.UiG3On && NativeFestivalFishing.Token() is not null))
        {
            snapshot["festival_reflex"] = new Dictionary<string, object> {
                ["state"] = "disabled", ["auto_started"] = false,
                ["not_executed"] = AdmissionRefusals.Classify("InboxDisabled", "FestivalReflexEnabled=false",
                    "adapter_unsupported_capability") };
            return;
        }
        this.UpdateFestivalResult();
        if (this.festivalRun is { } run) snapshot["festival_reflex"] = this.FestivalFields(run, run.Ending ? "stopping" : "live");
        else if (this.festivalSummary is not null && this.FestivalResultScopeValid())
            snapshot["festival_reflex"] = this.festivalSummary;
        else if (this.EventActionsReady && Game1.CurrentEvent is { id: "festival_spring13", playerControlSequenceID: "eggHunt" }
            || this.ReflexFishReady && this.UiG3On && NativeFestivalFishing.Token() is not null)
            snapshot["festival_reflex"] = new Dictionary<string, object> { ["state"] = "ready", ["kind"] = Game1.CurrentEvent.playerControlSequenceID,
                ["run_id"] = NativeEventControl.Token() ?? NativeFestivalFishing.Token()!,
                ["lifecycle_id"] = this.FestivalLifecycleId(Game1.CurrentEvent),
                ["timer_ms"] = Game1.CurrentEvent.festivalTimer, ["auto_started"] = false };
        else if (this.FestivalIntro() is { } intro) snapshot["festival_reflex"] = intro;
    }

    // Main-event observation only. No cutscene input admission is widened: the
    // runtime must use a fresh native event-dialogue A offer for every box.
    private Dictionary<string, object>? FestivalIntro()
    {
        if (!this.config.Enabled || !this.UiG3On || !this.PadPathConfigured || !Context.IsWorldReady || Context.IsMainPlayer || Context.IsSplitScreen
            || !Game1.eventUp || Game1.CurrentEvent is not { isFestival: true, eventSwitched: true, festivalTimer: <= 0 } ev
            || !ReferenceEquals(ev, Game1.currentLocation?.currentEvent) || Game1.player is null
            || !ReferenceEquals(Game1.player.currentLocation, Game1.currentLocation)
            || ev.eventCommands is not { Length: > 0 } script || ev.CurrentCommand < 0 || ev.CurrentCommand >= script.Length)
            return null;
        string kind = ev.id == "festival_spring13" ? "eggHunt" : ev.id == "festival_winter8" ? "iceFishing" : "";
        if (kind == "" || kind == "iceFishing" && !this.ReflexFishReady
            || kind == "eggHunt" && (!this.config.WorldInteractKindsEnabled || !this.config.WorldInteractionInputEnabled
                || !this.config.BackgroundToolUiEnabled || !this.config.WorldPressEnabled || !this.config.MenuStepCommandInboxEnabled)
            || script[^1] != "playerControl " + kind || ev.CurrentCommand >= script.Length - 1) return null;
        string pin = this.FestivalLifecycleId(ev);
        return new() { ["state"] = "intro", ["kind"] = kind, ["run_id"] = pin, ["lifecycle_id"] = pin,
            ["timer_ms"] = 0, ["auto_started"] = false, ["command_index"] = ev.CurrentCommand };
    }

    // Capture and every native ui_press re-read share this exact ownership check.
    private FestivalDialogueOwner? ReadFestivalDialogueOwner(Event ev, StardewValley.Menus.IClickableMenu? top)
        => this.FestivalIntro() is not null || this.festivalSummary is not null && this.FestivalResultScopeValid()
            ? FestivalDialogueWitness.Read(ev, top, this.FestivalLifecycleId(ev)) : null;

    private static Dictionary<string, int> FestivalInventory(Farmer player) => player.Items.Where(i => i is not null)
        .GroupBy(i => i!.QualifiedItemId).ToDictionary(g => g.Key, g => g.Sum(i => i!.Stack));

    private bool FestivalResultScopeValid() => Context.IsWorldReady && !Context.IsMainPlayer && !Context.IsSplitScreen
        && this.festivalCompletedRun is { } run && ReferenceEquals(Game1.CurrentEvent, run.Event)
        && ReferenceEquals(Game1.currentLocation?.currentEvent, run.Event) && ReferenceEquals(Game1.currentLocation, run.Location)
        && ReferenceEquals(Game1.player, run.Player)
        && this.CurrentCommandIdentity() == run.Identity with { ContextGeneration = this.CurrentCommandIdentity().ContextGeneration }
        && this.FestivalLifecycleId(run.Event) == run.LifecycleId;

    private void UpdateFestivalResult()
    {
        // Native winners are populated only when the post-game winner command executes.
        // Observe that command having passed; a timer ending alone cannot establish the result.
        if (this.festivalSummary is null || this.festivalAttempted is not { } ev || this.festivalCompletedRun is not { } run
            || !this.FestivalResultScopeValid()) return;
        string command = (string)this.festivalSummary["kind"] == "eggHunt" ? "eggHuntWinner" : "iceFishingWinner";
        if (!this.festivalSummary.ContainsKey("winner_command"))
        {
            int winnerIndex = Array.FindIndex(ev.eventCommands, s => s == "cutscene " + command);
            if (winnerIndex < 0 || ev.CurrentCommand <= winnerIndex) return;
            var winners = this.Helper.Reflection.GetField<HashSet<long>>(ev, "festivalWinners").GetValue();
            int players = Game1.numberOfPlayers() - (Game1.HasDedicatedHost ? 1 : 0);
            int threshold = command == "iceFishingWinner" ? 5 : players switch { 1 => 9, 2 => 6, 3 => 5, _ => 4 };
            int finalScore = run.Player.festivalScore;
            this.festivalSummary["score"] = finalScore;
            this.festivalSummary["win_threshold"] = threshold;
            // Both winner commands set this native flag from the highest
            // score vs the threshold. Capture before Fork replaces the script
            // (or iceFishingWinnerMP repurposes the flag for local membership).
            bool playerWin = !ev.specialEventVariable1;
            this.festivalSummary["result"] = winners.Contains(run.Player.UniqueMultiplayerID) && finalScore >= threshold ? "win" : "lose";
            this.festivalSummary["winner_membership"] = winners.Contains(run.Player.UniqueMultiplayerID);
            this.festivalSummary["winner_ids"] = playerWin ? winners.OrderBy(id => id).Select(id => id.ToString()).ToArray()
                : new[] { command == "iceFishingWinner" ? "npc:Willy" : "npc:Abigail" };
            this.festivalSummary["winner_command"] = winnerIndex;
        }
        bool won = (string)this.festivalSummary["result"] == "win";
        // The native winner box must be acknowledged before completion. Prize
        // credit is independent: actual inventory gain or the native prize menu.
        int prizeIndex = Array.FindIndex(ev.eventCommands, s => s == "awardFestivalPrize");
        bool prizePassed = prizeIndex >= 0 && ev.CurrentCommand > prizeIndex;
        var gains = FestivalInventory(run.Player).Where(i => i.Value > run.PrizeBaseline.GetValueOrDefault(i.Key)).Take(4)
            .Select(i => new Dictionary<string, object> { ["item_id"] = i.Key, ["count"] = i.Value - run.PrizeBaseline.GetValueOrDefault(i.Key) }).ToArray();
        var offered = Game1.activeClickableMenu is StardewValley.Menus.ItemGrabMenu { context: Event prizeEvent } menu
                && ReferenceEquals(prizeEvent, ev)
            ? menu.ItemsToGrabMenu.actualInventory.Where(i => i is not null).Take(4)
                .Select(i => new Dictionary<string, object> { ["item_id"] = i!.QualifiedItemId, ["count"] = i.Stack }).ToArray()
            : Array.Empty<Dictionary<string, object>>();
        this.festivalSummary["prize"] = new Dictionary<string, object> {
            ["status"] = !won ? "none" : !prizePassed ? "pending" : gains.Length > 0 ? "inventory_verified"
                : offered.Length > 0 ? "native_menu_offered" : "not_observed",
            ["items"] = gains.Length > 0 ? gains : offered };
        bool complete = (!won || prizePassed) && !Game1.dialogueUp && Game1.activeClickableMenu is not StardewValley.Menus.DialogueBox;
        if (complete && !this.festivalSummary.ContainsKey("final_verified"))
        {
            this.festivalSummary["final_verified"] = true;
            this.Monitor.Log("FESTIVAL_REFLEX result " + System.Text.Json.JsonSerializer.Serialize(this.festivalSummary), LogLevel.Info);
        }
    }

    private void AbortFestivalForSave(string reason)
    {
        if (this.festivalRun is { } run) { run.Cleanup = false; this.EndFestival("save:" + reason); }
    }
}
