using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Tools;

namespace ArtiStardewBridge;

// Primitives v2 WS-G4: the game side of the fishing reflex (default OFF behind ModConfig.ReflexFishingEnabled, and
// only on the G0 pad path). The loop lives in FishingReflexRunner (behaviour-tested in HoldRuntimeTests with a
// scripted fake game); this file wires it: admission (FishReflexAdmission + the runtime's pad-mode / cursor-hidden
// admission), the per-tick hooks (UpdateTicking after the background runtime, terminal routing in UpdateTicked), the
// verdict through the existing inbox, the FishingGame reads, ui.context "fishing" / capability "reflex_fish" and the
// session.json term. Evidence ceiling: UNIT_TESTED (+ LOCAL_BUILT); HYPOTHESIS until the CT witnesses (G4W1, V2ACC10).
public sealed partial class ModEntry
{
    private FishingReflexRunner? reflexRunner;

    /// <summary>The reflex path is configured: its flag on the configured pad path.</summary>
    bool ReflexFishConfigured => this.config.ReflexFishingEnabled && this.PadPathConfigured;

    /// <summary>reflex_fish admission term (inbox Admit / session.json): configured, enabled bridge, inactive farmhand.</summary>
    bool ReflexFishReady => this.ReflexFishConfigured && this.config.Enabled && Context.IsWorldReady && !Context.IsMainPlayer
        && !Context.IsSplitScreen && Game1.game1?.IsActive != true;

    bool ReflexBusy => this.reflexRunner?.Active == true || this.ReflexG5Busy; // WS-G5: any reflex holds the slot

    private FishingReflexRunner ReflexRunner => this.reflexRunner ??= new FishingReflexRunner(new FishingGame(this),
        () => this.backgroundMove, () => this.ReflexLeaseHeld, () => this.actionOwner.StopLatched);

    bool ReflexLeaseHeld => this.actionOwner.IsArmed && !this.actionOwner.StopLatched && !this.actionOwner.RecordFaulted
        && !this.actionOwner.IsReleasing && NowUnixMs() <= this.actionOwner.LeaseExpiresUnixMs;

    /// <summary>1-2 currently reachable water-edge stand tiles near ARTI; collision is refreshed each read.</summary>
    private IReadOnlyList<WaterEdge> WaterEdgesNow()
    {
        try
        {
            Farmer? p = Context.IsWorldReady ? Game1.player : null;
            GameLocation? loc = p?.currentLocation;
            if (p is null || loc is null)
                return Array.Empty<WaterEdge>();
            Point t = p.TilePoint;
            bool Standable(int x, int y)
            {
                return loc.isTileOnMap(x, y) && !loc.isWaterTile(x, y) && NativeRouteCell.IsWalkable(loc,p,x,y);
            }
            bool Water(int x, int y) => loc.isTileOnMap(x, y) && loc.isWaterTile(x, y);
            bool Fishable(int x, int y) => loc.isTileOnMap(x, y) && loc.canFishHere() && loc.isTileFishable(x, y);
            IReadOnlyList<WaterEdge> edges = WaterEdgeSearch.Find(t.X, t.Y, Standable, Water, Fishable, p.FishingLevel,
                (float)ReflexContracts.FishCastPowerDefault);
            return edges;
        }
        catch
        {
            return Array.Empty<WaterEdge>();
        }
    }

    /// <summary>Inbox dispatch: admit (nothing pressed on refusal) and start the reflex.</summary>
    private void ArmReflexFishFromCommand(MenuStepCommandRequest request)
    {
        this.ApplyActionOwnerRecords();
        CommandInboxIdentity pin = this.CurrentCommandIdentity();
        FishReflexArgs args = request.ReflexFish ?? FishReflexArgs.Default;
        string? refuse = !this.ReflexFishReady ? FishReflexAdmission.Prefix + ":not_ready"
            : request.ReflexFish is null ? FishReflexAdmission.Prefix + ":args"
            // Review major 1: never start during the night save (DayEnding/Saving .. Saved/DayStarted); same refusal as
            // the pad admission's TakeForAdmission (PAD_MODE_UNAVAILABLE:saving), checked before any fact sampling.
            : this.padAdapter?.SaveWindow == true ? BgReason.PreconditionUnavailable(BgHoldReason.PadModeUnavailable + ":saving")
            : this.commandInbox.ExecutionSlot.Blocks(request)
                ? FishReflexAdmission.Prefix + ":busy"
            : this.actionOwner.ReleaseRequested || this.actionOwner.StopLatched ? FishReflexAdmission.Prefix + ":stop_or_release"
            : !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request, pin, Context.IsWorldReady, Context.IsPlayerFree,
                Context.CanPlayerMove, out _) ? FishReflexAdmission.Prefix + ":binding"
            : !this.actionOwner.CanAdmitLeasedInputOnce(pin, NowUnixMs(), request.RuntimeInstanceId, request.OwnerId,
                request.Generation, request.ControlSeq, out _) ? FishReflexAdmission.Prefix + ":lease"
            : FishReflexAdmission.Refuse(this.SampleFishAdmission((float)args.CastPower));
        if (refuse is null)
        {
            // Gamepad mode take-over + cursor-hidden gate last (PLAN §1.5 a/b), exactly like a pad hold's admission.
            refuse = this.backgroundMove!.ReflexPadAdmission(out _);
        }
        this.Monitor.Log($"arti_command reflex_fish request_id={request.RequestId} max_s={args.MaxSeconds} cast_power={args.CastPower} "
            + $"refuse={refuse ?? "-"}", refuse is null ? LogLevel.Info : LogLevel.Warn);
        if (refuse is not null)
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request, CommandRejectReason.ArmRejected, pin, NowUnixMs(),
                refuse.StartsWith("PRECONDITION_UNAVAILABLE(", StringComparison.Ordinal) ? refuse : BgReason.PreconditionUnavailable(refuse)));
            return;
        }
        this.ReflexRunner.Start(request, args);
    }

    /// <summary>UpdateTicked: a background terminal of a reflex press goes to the runner, never to the inbox.</summary>
    private bool RouteReflexTerminal(BackgroundMoveTerminal? terminal)
    {
        if (terminal is null || this.reflexRunner?.OwnsTerminal(terminal) != true)
            return false;
        this.reflexRunner.OnPressTerminal(terminal);
        return true;
    }

    /// <summary>UpdateTicked: write the reflex verdict once it is ready.</summary>
    private void FlushReflexOutcome()
    {
        if (this.reflexRunner?.TakeOutcome() is not { } o)
            return;
        if (this.FestivalFishOutcome(o)) return;
        if (!this.commandInbox.HasInflight || !string.Equals(this.commandInbox.InflightRequestId, o.RequestId, StringComparison.Ordinal))
        {
            this.Monitor.Log($"reflex_fish outcome without matching inflight request: request_id={o.RequestId} verdict={o.Verdict}", LogLevel.Warn);
            return;
        }
        this.WriteCommandResult(this.commandInbox.CompleteReflexFish(o.Verdict, o.CleanupConfirmed, o.PressCount, o.Reflex,
            this.CurrentCommandIdentity(), NowUnixMs()));
    }

    /// <summary>ReturnedToTitle: release now; the generic interrupted result closes the inbox slot.</summary>
    private void AbortReflexForLifecycle(string reason)
    {
        if (this.reflexRunner?.Active != true)
            return;
        this.reflexRunner.Abort(reason);
        _ = this.reflexRunner.TakeOutcome();
    }

    private void AbortRoutesForSave(string evt)
    {
        try
        {
            this.AbortHouseTravelForSave();
            this.AbortEventEggForSave(); // FIXB59: stop the route before the shared pad-mode save restoration
            this.AbortFestivalForSave(evt);
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"Route save abort fault evt={evt}: {ex.GetType().Name}: {ex.Message}", LogLevel.Warn);
        }
    }

    /// <summary>ui.context: capability reflex_fish + the "fishing" fact (only with the flag; a rod in the inventory, held or not).</summary>
    void EnrichFishingContext(Dictionary<string, object> snapshot)
    {
        if (!this.config.ReflexFishingEnabled)
            return;
        float power = (float)ReflexContracts.FishCastPowerDefault;
        FishAdmissionFacts facts = this.SampleFishAdmission(power);
        string? reason = this.reflexRunner?.Active == true ? FishReflexAdmission.Prefix + ":active" : FishReflexAdmission.Refuse(facts);
        if (snapshot.GetValueOrDefault("capabilities") is Dictionary<string, object> caps)
            caps[ReflexFishTarget.Primitive] = new Dictionary<string, object>
            {
                ["implemented"] = true, ["available"] = reason is null && this.ReflexFishReady,
                ["reason"] = reason ?? "pad_mode_and_cursor_rechecked_at_admission",
            };
        // D-B2-4 (live V2B2ACC10a): published whenever a rod is in the inventory (held or not), so the runtime can offer
        // the water-edge approach and select_item of the rod (rod_slot) before the rod is equipped.
        var items = Context.IsWorldReady ? Game1.player?.Items : null;
        int rodSlot = FishingFactsWire.FirstRodSlot(items, item => item is FishingRod);
        if (!facts.RodHeld && rodSlot < 0)
            return;
        var f = this.SampleFishingFacts(power);
        var fishing = FishingFactsWire.Build(facts.RodHeld, rodSlot, f.FacesWater, f.LandingFishable, f.LandX, f.LandY,
            f.Level, this.WaterEdgesNow())!;
        fishing["can_fish_here"] = Game1.currentLocation?.canFishHere() == true;
        var rods = new List<object>();
        int slot = 0;
        foreach (var candidate in (IEnumerable<Item>?)items ?? Enumerable.Empty<Item>())
        {
            if (candidate is FishingRod rod)
            {
                string id = rod.TypeDefinitionId + rod.itemId.Value;
                rods.Add(new object[] { slot, id });
                if (slot == rodSlot) fishing["rod_item"] = id;
            }
            slot++;
        }
        fishing["rods"] = rods;
        snapshot["fishing"] = fishing;
    }

    /// <summary>The real game behind FishingReflexRunner (game thread only).</summary>
    private sealed class FishingGame : IFishingGame
    {
        private readonly ModEntry mod;

        public FishingGame(ModEntry mod) => this.mod = mod;

        public long Tick => Game1.ticks;

        public bool WindowActive => Game1.game1?.IsActive == true;

        public bool EventUp => Game1.eventUp || Game1.CurrentEvent is not null || Game1.currentMinigame is not null || Game1.farmEvent is not null;

        public bool GamepadControls => Game1.options?.gamepadControls == true;

        public string Location => Game1.player?.currentLocation?.NameOrUniqueName ?? "";

        public RodView Rod
        {
            get
            {
                Farmer? p = Context.IsWorldReady ? Game1.player : null;
                if (p?.CurrentTool is not FishingRod r)
                    return new RodView(false, p?.UsingTool == true, false, 0f, false, false, false, false, false, false, false, false, false);
                return new RodView(true, p.UsingTool, r.isTimingCast, r.castingPower, r.isCasting, r.castedButBobberStillInAir,
                    r.isFishing, r.isNibbling, r.hit, r.isReeling, r.pullingOutOfWater, r.fishCaught, r.showingTreasure);
            }
        }

        public float Stamina => Game1.player?.Stamina ?? 0f;

        public int FreeSlots => Game1.player?.freeSpotsInInventory() ?? 0;

        public int ItemCount => Game1.player?.Items.Where(i => i is not null).Sum(i => i!.Stack) ?? 0;

        public string MenuKind(out object? deepest)
        {
            IClickableMenu? menu = Game1.activeClickableMenu;
            for (int depth = 0; menu?.GetChildMenu() is { } child && depth < 8; depth++)
                menu = child;
            deepest = menu;
            return menu switch
            {
                null => Game1.dialogueUp ? FishMenuKind.Other : FishMenuKind.None,
                BobberBar => FishMenuKind.BobberBar,
                ItemGrabMenu grab when grab.context is FishingRod rod && ReferenceEquals(rod, Game1.player?.CurrentTool)
                    => FishMenuKind.ItemGrab,
                _ => FishMenuKind.Other,
            };
        }

        public BobberBarView? ReadBar(object bar)
        {
            if (bar is not BobberBar b)
                return null;
            return new BobberBarView(b.bobberPosition, b.bobberSpeed, b.bobberBarPos, b.bobberBarSpeed, b.bobberBarHeight,
                b.distanceFromCatching, b.bobberInBar, b.treasure && b.treasureAppearTimer <= 0f, b.treasureCaught, b.treasurePosition,
                b.fadeIn, b.fadeOut);
        }

        public FishCatchFacts? CatchFacts()
        {
            if (Game1.player?.CurrentTool is not FishingRod r || r.whichFish is null)
                return null;
            string id = r.whichFish.QualifiedItemId;
            int category;
            try
            {
                category = ItemRegistry.GetData(id)?.Category ?? 0;
            }
            catch
            {
                category = 0;
            }
            return new FishCatchFacts(id, r.fishQuality, r.fishSize, r.treasureCaught, category, ItemRegistry.GetData(id)?.DisplayName);
        }

        public string? NeutralizeBarEntry()
        {
            List<BgButton> remove = new();
            if (Game1.oldMouseState.LeftButton == ButtonState.Pressed)
                remove.Add(BgButton.Mouse("Left"));
            foreach (InputButton b in Game1.options?.useToolButton ?? Array.Empty<InputButton>())
            {
                if (b.key != Keys.None && Game1.oldKBState.IsKeyDown(b.key))
                    remove.Add(BgButton.Key(b.key.ToString()));
            }
            if (remove.Count == 0)
                return null;
            BackgroundMoveRuntime.WriteHistoryRemoval(remove, resetPad: false);
            return string.Join(",", remove);
        }

        public bool WriteBarX(object bar) => bar is BobberBar b && BobberBarPadWriter.WriteX(b);

        public void WriteNeutral() => PadHistoryHygiene.WriteNeutral();

        public string RetrieveLine() => this.EndCast(null);

        public string EndCast(object? ownedMenu)
        {
            // ADR 111: the vanilla no-catch reel-in, also the bounded exit for a stalled owned cast.
            // The runner pins farmer/tool/location and passes only its captured receiver (or no receiver).
            if (Game1.player is not Farmer p || p.CurrentTool is not FishingRod r)
                return "no_rod";
            this.MenuKind(out object? deepest);
            if (!ReferenceEquals(deepest, ownedMenu)) return "foreign_menu";
            if (ownedMenu is IClickableMenu menu)
            {
                menu.exitThisMenu();
                // Closing a receiver can run callbacks. They grant no authority over a successor.
                if (!ReferenceEquals(Game1.player, p) || !ReferenceEquals(p.CurrentTool, r)) return "binding_changed";
                if (this.MenuKind(out _) != FishMenuKind.None) return "foreign_menu";
            }
            try
            {
                r.doneFishing(p, false);
            }
            finally
            {
                // FIXB15 review MAJOR: whatever doneFishing did (it can throw with no lastUser), the farmer is free again.
                p.UsingTool = false;
                p.CanMove = true;
                p.completelyStopAnimatingOrDoingAction();
            }
            return r.isFishing ? "still_fishing" : "retrieved";
        }

        public object? Farmer => Context.IsWorldReady ? Game1.player : null;
        public object? Tool => Game1.player?.CurrentTool;
        public bool CanMove => Game1.player?.CanMove == true;

        public void Log(string line, bool warn = false) => this.mod.Monitor.Log(line, warn ? LogLevel.Warn : LogLevel.Info);
    }
}
