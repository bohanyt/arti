using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework.Input;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

// Primitives v2 WS-G0: the game side of the pad path (default OFF behind ModConfig.GamepadInputEnabled).
// The logic lives in PadModeAdapter (behaviour-tested in the HoldRuntimeTests harness); this file only
// wires it: the IPadGame implementation over Game1 / Options / the reflected (MVID-pinned)
// _oldGamepadConnectedState pin, the OS-cursor guard install, the pad panic console command and the
// pad_panic.json control file, the lifecycle hooks and the session.json / ui.context pad facts. Every
// Press/Suppress goes through the runtime's HelperPressPort; the only oldPadState writer is
// PadHistoryHygiene. Evidence ceiling: UNIT_TESTED (+ LOCAL_BUILT); HYPOTHESIS until live.
public sealed partial class ModEntry
{
    /// <summary>Pad panic control file in the command inbox directory (idempotent; not the request slot).</summary>
    internal const string PadPanicFileName = "pad_panic.json";
    internal const string PadPanicCommand = "arti_pad_panic";

    private PadModeAdapter? padAdapter;
    private IReflectedField<bool>? padPinField;
    private string? padPinFailure;
    private bool padPinResolved;
    private uint padPanicPollTick;

    /// <summary>The pad path is configured: flag on, the adapter built and the Stage 2 background runtime present.</summary>
    bool PadPathConfigured => this.config.GamepadInputEnabled && this.padAdapter is not null && this.backgroundMove is not null;

    /// <summary>Entry (before the background runtime is built): the host the runtime calls at pad admission.</summary>
    private IPadModeHost? SetUpPadHost(IModHelper helper)
    {
        string? refusal = PadModeAdapter.ConfigRefusal(this.config.GamepadInputEnabled, this.config.BackgroundMovementEnabled,
            this.config.BackgroundToolUiEnabled, this.config.GamepadProbeEnabled, this.config.ReflexProbeEnabled);
        if (refusal is not null)
        {
            if (this.config.GamepadInputEnabled)
                this.Monitor.Log($"GamepadInputEnabled refused ({refusal}): the pad path stays disabled.", LogLevel.Warn);
            return null;
        }
        this.padAdapter = new PadModeAdapter(new PadGame(this), () => this.backgroundMove,
            () => this.config.GamepadInputEnabled, () => this.config.FarmhandInactiveRenderingEnabled,
            () => PadCursorWarpGuard.Verified && PadDropdownInitializationWitness.Installed,
            PadCursorDrawWitness.Describe);
        if (!PadCursorWarpGuard.Installed)
            PadCursorWarpGuard.Install(helper.ModRegistry.ModID,
                () => this.config.GamepadInputEnabled || this.config.UiPadMenusEnabled, this.Monitor);
        PadDropdownInitializationWitness.Install(helper.ModRegistry.ModID, () => this.padAdapter, this.Monitor);
        PadCursorDrawWitness.Install(helper.ModRegistry.ModID, () => this.config.GamepadInputEnabled, this.Monitor);
        helper.ConsoleCommands.Add(PadPanicCommand,
            "WS-G0 pad panic: Suppress every pad button and stick direction, neutral oldPadState, give gamepadMode back.",
            (_, _) => this.padAdapter?.RequestPanic("console"));
        if (!this.config.FarmhandInactiveRenderingEnabled)
            this.Monitor.Log("GamepadInputEnabled without FarmhandInactiveRenderingEnabled: every pad request refuses CURSOR_NOT_HIDDEN:draw_path_off.",
                LogLevel.Warn);
        return this.padAdapter;
    }

    /// <summary>
    /// WS-G0 D-2: on the pad path the A press uses Character.GetGrabTile() (bounding box +-5 px). null when
    /// the grab tile is the request's target tile (or the pad path is off); else GRAB_TILE_MISMATCH
    /// (":own_tile" when it is ARTI's own tile: a centred stand). The v1 keyboard press uses the same grab
    /// tile with the cursor beyond radius 1, so the target capture and the runtime's existing bounded flush
    /// step already depend on it; the mod itself never bumps the actor.
    /// </summary>
    private string? PadGrabTileMismatch(MenuStepCommandRequest request)
    {
        if (!this.PadPathConfigured || request.InteractionTarget is not { } target || Game1.player is not Farmer player)
            return null;
        Microsoft.Xna.Framework.Vector2 grab = player.GetGrabTile();
        // FIXB14 D-B12-1 (live V2B12PLANT-a: place_item armed with grab (53,19) = own tile, target (53,20) =
        // GRAB_TILE_MISMATCH:own_tile): the X kinds press X, which acts on the tool tile (the faced neighbour).
        Microsoft.Xna.Framework.Vector2 press = target.Kind == GenericWorld.Use && BackgroundToolTileProbe.ToolTile(player) is { } tool
            ? new Microsoft.Xna.Framework.Vector2(tool.X, tool.Y) : PadPressTile(player, target.Kind);
        if ((int)press.X == target.TileX && (int)press.Y == target.TileY)
            return null;
        string why = grab == player.Tile ? "GRAB_TILE_MISMATCH:own_tile" : "GRAB_TILE_MISMATCH";
        this.Monitor.Log($"background PAD world_interact refused {why} grab=({(int)grab.X},{(int)grab.Y}) target=({target.TileX},{target.TileY}) "
            + $"actor=({(int)player.Tile.X},{(int)player.Tile.Y}) facing={player.FacingDirection}", LogLevel.Info);
        return why;
    }

    /// <summary>PLAN 1.5 f: with GamepadInputEnabled the legacy foreground pad probes / drivers refuse at entry.</summary>
    private bool LegacyPadWriterRefused(string what)
    {
        if (!this.config.GamepadInputEnabled)
            return false;
        this.Monitor.Log($"{what} refused: LegacyPadWriterRefused (GamepadInputEnabled; HelperPressPort is the only pad writer)", LogLevel.Warn);
        return true;
    }

    /// <summary>Resolve Game1._oldGamepadConnectedState once (protected instance field, G1:1091), MVID-pinned.</summary>
    private string? ResolvePadPin()
    {
        if (this.padPinResolved)
            return this.padPinFailure;
        this.padPinResolved = true;
        this.padPinFailure = null;
        try
        {
            if (typeof(Game1).Module.ModuleVersionId != BackgroundMoveRuntime.PinnedGameMvid)
                this.padPinFailure = "pin_game_mvid";
            else if (Game1.game1 is null)
                this.padPinFailure = "pin_no_game";
            else
            {
                this.padPinField = this.Helper.Reflection.GetField<bool>(Game1.game1, "_oldGamepadConnectedState", required: true);
                _ = this.padPinField.GetValue();
            }
        }
        catch (Exception ex)
        {
            this.padPinField = null;
            this.padPinFailure = "pin_reflection:" + ex.GetType().Name;
        }
        if (this.padPinFailure is not null)
        {
            this.padPinResolved = false; // retried at the next admission (e.g. game instance not ready yet)
            this.Monitor.Log("PAD_MODE pin unavailable: " + this.padPinFailure, LogLevel.Warn);
        }
        return this.padPinFailure;
    }

    /// <summary>End of every UpdateTicking (after every ARTI Press/Suppress of the tick).</summary>
    private void TickPadModeTicking()
    {
        if (this.PadPathConfigured)
            this.padAdapter!.Tick();
    }

    /// <summary>UpdateTicked: poll the pad_panic.json control file (~4 Hz) when the pad path is configured.</summary>
    private void PollPadPanicFile(uint ticks)
    {
        if (!this.PadPathConfigured || this.commandInboxDir is null || ticks - this.padPanicPollTick < 15)
            return;
        this.padPanicPollTick = ticks;
        string path = Path.Combine(this.commandInboxDir, PadPanicFileName);
        string? text = null;
        try
        {
            if (!File.Exists(path))
                return;
            // G7 contract (m1_paths.PAD_PANIC_FILE): read, delete, panic; the content is only logged (request_id).
            text = AtomicFileWriter.ReadAllText(path);
            File.Delete(path);
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"PAD_PANIC control file read fault: {ex.GetType().Name}: {ex.Message}; panic requested anyway", LogLevel.Warn);
        }
        this.padAdapter!.OnPanicFile(text);
    }

    /// <summary>ReturnedToTitle / disconnect: Suppress outstanding now, restore at the next UpdateTicking.</summary>
    private void OnPadLifecycleExit(string reason) => this.padAdapter?.LifecycleExit(reason);

    /// <summary>Mod dispose: give the options back now (no tick can pin afterwards).</summary>
    private void DisposePad() => this.padAdapter?.Dispose();

    /// <summary>session.json pad facts (null when the flag is off: the v1 file is unchanged).</summary>
    private CommandInboxSession WithPadSessionFacts(CommandInboxSession session)
    {
        if (!this.config.GamepadInputEnabled)
            return session;
        PadModeOwner? owner = this.padAdapter?.Owner;
        BackgroundMoveRuntime? runtime = this.backgroundMove;
        string? hidden = runtime is not null && Context.IsWorldReady ? runtime.PadCursorHiddenReason() : BgCursorHidden.Prefix + ":unreadable";
        OsWarpGuardCounters warp = PadCursorWarpGuard.Counters;
        // ADR 250: world = the verified pad path (ADR 244); menu = the motor's frame takes the game's inactive menu branch
        // in both window states (no generic menu motor = no seated menu frame). focus_independent is the weaker value.
        FocusPaths focusPaths = UiMotorFrontPath.Paths(this.PadPathConfigured,
            !this.config.UiPadMenusEnabled || UiMotorInputSeatPatch.Installed && UiMotorMenuUpdatePatch.Installed); // review B4: both
        return session with
        {
            GamepadInputEnabled = this.PadPathConfigured,
            PadModeOwned = owner?.Owned == true,
            PadModeRestorePending = owner?.RestorePending == true || this.padAdapter?.RestoreWritePending == true,
            PadModeCleanupFault = this.padAdapter?.CleanupFault ?? "",
            PadConnectedPin = owner?.PinActive == true,
            PadOverrideCount = runtime?.PadLedger.Count ?? 0,
            PadOutstandingCount = runtime?.PadLedger.Outstanding.Count ?? 0,
            GamepadMode = Game1.options?.gamepadMode.ToString(),
            CursorHidden = hidden is null,
            CursorHiddenReason = hidden,
            OsWarpCalls = warp.Calls,
            OsWarpBlocked = warp.Blocked,
            OsWarpUnblockedInactive = warp.UnblockedInactive,
            OsWarpBlockedActive = warp.BlockedActive, // ADR 244
            FocusIndependent = UiMotorFrontPath.Weaker(focusPaths), // ADR 244, ADR 250: the weaker path
            FocusIndependentPaths = focusPaths, // ADR 250
            AutoRun = Game1.options?.autoRun == true,
            PadButtonEnabled = this.PadButtonReady,
            ReflexFishEnabled = this.config.ReflexFishingEnabled ? this.ReflexFishReady : null,
            ReflexCombatEnabled = this.config.ReflexCombatEnabled ? this.ReflexCombatReady : null, // WS-G5
            ReflexChargeEnabled = this.config.ReflexChargeEnabled ? this.ReflexChargeReady : null, // WS-G5
        };
    }

    /// <summary>
    /// ui.context "pad" fact (only with the flag; low-churn values, no counters): mode / cursor facts
    /// plus the PadContextTable row for the current context ("buttons": family + detail, or refused).
    /// </summary>
    void EnrichPadContext(Dictionary<string, object> snapshot)
    {
        if (!this.config.GamepadInputEnabled)
            return;
        string? hidden = this.backgroundMove is { } runtime && Context.IsWorldReady ? runtime.PadCursorHiddenReason() : BgCursorHidden.Prefix + ":unreadable";
        PadWorldFacts facts = this.SamplePadWorldFacts();
        snapshot["pad"] = new Dictionary<string, object>
        {
            ["enabled"] = this.PadPathConfigured,
            ["mode_owned"] = this.padAdapter?.Owner.Owned == true,
            ["restore_pending"] = this.padAdapter?.Owner.RestorePending == true || this.padAdapter?.RestoreWritePending == true,
            ["cleanup_fault"] = this.padAdapter?.CleanupFault ?? "",
            ["cursor_hidden"] = hidden is null,
            ["auto_run"] = Game1.options?.autoRun == true,
            ["context"] = facts.Context.ToString(),
            ["held"] = facts.Held,
            ["buttons"] = PadContextTable.Row(facts).ToDictionary(kv => kv.Key, kv => (object)PadContextTable.Wire(kv.Value), StringComparer.Ordinal),
        };
    }

    // ------------------------------------------------------------------
    // WS-G0 slice 2: the per-context button table (PadContextTable) and the pad_button primitive.
    // ------------------------------------------------------------------

    /// <summary>Read-only facts for PadContextTable (context, held item class, toolbar slot, sub-flags).</summary>
    private PadWorldFacts SamplePadWorldFacts()
    {
        Farmer? player = Context.IsWorldReady ? Game1.player : null;
        PadContext context = PadContextTable.ContextOf(player is not null,
            Game1.activeClickableMenu is not null || Game1.dialogueUp,
            Game1.eventUp || Game1.CurrentEvent is not null || Game1.currentMinigame is not null || Game1.farmEvent is not null,
            Context.IsPlayerFree && Context.CanPlayerMove && player?.UsingTool != true);
        return new PadWorldFacts(context, PadHeldOf(player?.CurrentItem), Game1.options?.autoRun == true,
            player?.CurrentToolIndex ?? 0, this.PadButtonReady, this.config.PadSelectJournalEnabled, this.ReflexFishConfigured,
            this.ReflexCombatConfigured,
            // FIXB31: only with EventSkipEnabled (off: null, the event row is unchanged)
            this.config.EventSkipEnabled && context == PadContext.Event ? EventSkipPolicy.Refusal(SampleEventSkipFacts()) ?? "" : null,
            context == PadContext.Event ? SampleFestivalLeave() : null);
    }

    /// <summary>fix63A: FestivalLeavePolicy.State of the current event; the free-roam token already excludes menus and minigames.</summary>
    private static string? SampleFestivalLeave() => Game1.CurrentEvent is not Event ev ? null
        : FestivalLeavePolicy.State(ev.isFestival, NativeEventControl.Token() is not null, ev.playerControlSequence, ev.eventSwitched);

    /// <summary>FIXB31: read-only facts of the current event for EventSkipPolicy (game thread).</summary>
    private static EventSkipFacts SampleEventSkipFacts()
    {
        Event? ev = Game1.CurrentEvent;
        IClickableMenu? menu = Game1.activeClickableMenu;
        return new EventSkipFacts(
            EventUp: Game1.eventUp && ev is not null,
            FarmEvent: Game1.farmEvent is not null,
            Minigame: Game1.currentMinigame is not null,
            Festival: ev?.isFestival == true,
            Wedding: ev?.isWedding == true,
            Skippable: ev?.skippable == true,
            Skipped: ev?.skipped == true,
            PlayerControl: ev?.playerControlSequence == true,
            MenuOpen: menu is not null,
            MenuType: menu?.GetType().Name,
            QuestionPending: menu is DialogueBox box && box.isQuestion);
    }

    /// <summary>Pure classification input from Farmer.CurrentItem (read-only).</summary>
    private static string PadHeldOf(Item? item)
    {
        try
        {
            StardewValley.Object? obj = item as StardewValley.Object;
            return PadHeld.Classify(
                isItem: item is not null,
                isTool: item is Tool,
                isMeleeWeapon: item is StardewValley.Tools.MeleeWeapon,
                isScythe: item is StardewValley.Tools.MeleeWeapon w && w.isScythe(),
                isRod: item is StardewValley.Tools.FishingRod,
                isSlingshot: item is StardewValley.Tools.Slingshot,
                isObject: obj is not null,
                edible: obj is not null && obj.Edibility != -300,
                placeable: obj is not null && obj.isPlaceable());
        }
        catch
        {
            return PadHeld.Object; // unreadable => the strictest non-tool class (A and X refused)
        }
    }

    /// <summary>
    /// WS-G0 PLAN 1.4 world denylist on the pad path, before any Press: TOOL = button X, ACTION = button A
    /// with the request's kind. null = allowed (or the pad path is off).
    /// </summary>
    private string? PadDeniedForHold(MenuStepCommandRequest request, BgHoldKind kind)
    {
        if (!this.PadPathConfigured)
            return null;
        string? interactKind = kind == BgHoldKind.Action ? request.InteractionTarget?.Kind : null;
        if (GenericWorld.IsKind(interactKind))
            return this.GenericWorldReady && this.CaptureGenericWorld(interactKind!).Target == request.InteractionTarget
                ? null : "PAD_DENIED:generic_target_changed";
        if (kind == BgHoldKind.Action && EventControlAction.IsKind(interactKind))
        {
            var current = this.CaptureEventAction(new Microsoft.Xna.Framework.Point(request.InteractionTarget!.Value.TileX,
                request.InteractionTarget.Value.TileY));
            return current.Target == request.InteractionTarget && current.Action?.EventScopeValid?.Invoke(false) == true
                ? null : "PAD_DENIED:event_target_changed";
        }
        // WS-G6: the extras X kinds press pad X under their explicit kind (PLAN 1.4 X rows).
        string button = kind == BgHoldKind.Tool || WorldExtras.UsesX(interactKind) ? PadAbstractButton.X : PadAbstractButton.A;
        PadMeaning meaning = PadContextTable.Translate(button, this.SamplePadWorldFacts(), interactKind);
        // WS-G4: X with a rod is the fishing reflex family (reflex_fish only), never a use_tool hold.
        if (meaning.Allowed && kind == BgHoldKind.Tool && meaning.Family != PadContextTable.FamilyUseTool)
            return "PAD_DENIED:family_" + meaning.Family;
        return meaning.Allowed ? null : "PAD_DENIED:" + meaning.Refusal;
    }

    /// <summary>pad_button admission term: the pad path, its own default-OFF flag, a farmhand. ADR 244: no focus term (a direct call).</summary>
    bool PadButtonReady => this.PadPathConfigured && this.config.PadButtonInputEnabled && this.config.Enabled
        && Context.IsWorldReady && !Context.IsMainPlayer && !Context.IsSplitScreen;

    /// <summary>
    /// One world pad button as ONE direct call mirroring the vanilla UpdateControlInput branch and its
    /// predicate (G1:12721-12728 shoulders, 13383-13428 Start / Back / Y). PadContextTable refuses first;
    /// no pad Press, no device state; the verdict is read back from the inventory order / the menu.
    /// </summary>
    private void ExecutePadButton(MenuStepCommandRequest request)
    {
        this.ApplyActionOwnerRecords();
        CommandInboxIdentity pin = this.CurrentCommandIdentity();
        string button = request.PadButton?.Button ?? "";
        PadWorldFacts facts = this.SamplePadWorldFacts();
        PadMeaning meaning = PadContextTable.Translate(button, facts);
        // FIXB31: the event skip runs while the player is not free (eventUp); its own policy gated it above.
        bool eventSkip = meaning.Allowed && meaning.Detail == EventSkipPolicy.Detail;
        bool festivalLeave = meaning.Allowed && meaning.Detail == FestivalLeavePolicy.Detail; // fix63A: also runs under eventUp
        string? refuse = !this.PadButtonReady ? "PadButtonNotReady"
            : request.PadButton is null ? "PadButtonUnsupported"
            : this.commandInbox.ExecutionSlot.Blocks(request) ? "PadButtonBusy"
            : this.actionOwner.ReleaseRequested || this.actionOwner.StopLatched ? "PadButtonNotReady"
            : !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request, pin, Context.IsWorldReady,
                eventSkip || festivalLeave || Context.IsPlayerFree, eventSkip || festivalLeave || Context.CanPlayerMove, out _) ? "PadButtonTargetChanged"
            : !this.actionOwner.CanAdmitLeasedInputOnce(pin, NowUnixMs(), request.RuntimeInstanceId, request.OwnerId,
                request.Generation, request.ControlSeq, out _) ? "PadButtonNotReady"
            : !meaning.Allowed || meaning.Family != PadContextTable.FamilyPadButton ? "PAD_DENIED:" + (meaning.Refusal ?? "family")
            : null;
        this.Monitor.Log($"arti_command pad_button request_id={request.RequestId} button={button} context={facts.Context} "
            + $"meaning={meaning.Family ?? meaning.Refusal} refuse={refuse ?? "-"}", LogLevel.Info);
        if (refuse is not null)
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request, CommandRejectReason.ArmRejected, pin, NowUnixMs(),
                BgReason.PreconditionUnavailable(refuse)));
            return;
        }

        PadButtonVerdict verdict;
        try
        {
            Farmer player = Game1.player;
            if (festivalLeave)
            {
                // fix63A: the game's own leave flow, as if she had reached the map edge. She answers it like any ready check.
                bool asked = Game1.CurrentEvent?.TryStartEndFestivalDialogue(player) == true;
                verdict = PadButtonEffect.FestivalLeave(asked, Game1.activeClickableMenu?.GetType().Name);
            }
            else if (eventSkip)
            {
                // FIXB31: the vanilla gamepad Back branch, exactly (Game1.cs:4859 with a menu / 13414 without one).
                Event? ev = Game1.CurrentEvent;
                bool skippedBefore = ev?.skipped == true, called = false;
                if (ev is not null && !ev.skipped && ev.skippable)
                {
                    ev.skipped = true;
                    ev.skipEvent();
                    Game1.freezeControls = false;
                    called = true;
                }
                verdict = PadButtonEffect.Skip(ev is not null, skippedBefore, called, ReferenceEquals(Game1.CurrentEvent, ev),
                    ev?.skipped == true);
            }
            else if (button is PadAbstractButton.LB or PadAbstractButton.RB)
            {
                bool right = button == PadAbstractButton.RB;
                List<Item?> before = player.Items.ToList();
                // Vanilla: shoulders shift the toolbar row only while the HUD is drawn (G1:12721-12728).
                if (Game1.IsHudDrawn)
                    player.shiftToolbar(right);
                verdict = PadButtonEffect.Toolbar<Item?>(before, player.Items.ToList(), right);
            }
            else
            {
                bool menuWasNull = Game1.activeClickableMenu is null;
                if (menuWasNull)
                {
                    switch (button)
                    {
                        case PadAbstractButton.Start when Game1.CanShowPauseMenu():
                            Game1.PushUIMode();
                            Game1.activeClickableMenu = new GameMenu();
                            Game1.PopUIMode();
                            break;
                        case PadAbstractButton.Y when Game1.dayOfMonth > 0 && player.CanMove && !Game1.dialogueUp && !Game1.eventUp:
                            Game1.PushUIMode();
                            Game1.activeClickableMenu = new GameMenu(GameMenu.craftingTab);
                            Game1.PopUIMode();
                            break;
                        case PadAbstractButton.Select when Game1.dayOfMonth > 0 && player.CanMove && !Game1.dialogueUp && !Game1.eventUp:
                            Game1.activeClickableMenu = new QuestLog();
                            break;
                    }
                }
                IClickableMenu? after = Game1.activeClickableMenu;
                verdict = PadButtonEffect.Menu(button, menuWasNull, after?.GetType().Name, (after as GameMenu)?.currentTab, GameMenu.craftingTab);
            }
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"pad_button call fault: {ex.GetType().Name}: {ex.Message}", LogLevel.Error);
            verdict = new PadButtonVerdict(PadButtonVerdict.Uncertain, false, 1, "call_fault:" + ex.GetType().Name, null);
        }
        this.Monitor.Log($"arti_command pad_button VERDICT request_id={request.RequestId} button={button} outcome={verdict.Outcome} "
            + $"detail={verdict.Detail ?? "-"} menu={verdict.ResultMenuType ?? "-"}", verdict.Applied ? LogLevel.Info : LogLevel.Warn);
        this.WriteCommandResult(this.commandInbox.CompletePadButton(verdict, this.CurrentCommandIdentity(), NowUnixMs()));
    }

    /// <summary>The real game behind PadModeAdapter (game thread only).</summary>
    private sealed class PadGame : IPadGame
    {
        private readonly ModEntry mod;

        public PadGame(ModEntry mod) => this.mod = mod;

        public bool Active => Game1.game1?.IsActive == true;

        public bool WorldReady => Context.IsWorldReady;

        public bool MenuOpen => Game1.activeClickableMenu is not null;

        public bool SaveMenuOpen => NativeSaveMenuOpen;

        public bool StopLatched => this.mod.actionOwner.StopLatched;

        public bool LeaseReleased => !this.mod.actionOwner.IsArmed && this.mod.actionOwner.ReleaseAcked
            && !this.mod.actionOwner.ReleaseRequested && this.mod.commandInbox.InflightRequestId is null
            && !this.mod.MutationInflight;

        public bool PadConnected => Game1.input.GetGamePadState().IsConnected;

        public long Tick => Game1.ticks;

        public PadModeBinding? ReadBinding() => !Context.IsMainPlayer && !Context.IsSplitScreen
            && Game1.game1 is { } game && Game1.player is { } player && Game1.options is { } options
            && this.mod.commandSessionId.Length > 0
                ? new PadModeBinding(game, player, options, this.mod.commandSessionId, Context.ScreenId)
                : null;

        public PadModeSnapshot? ReadOptions() => Game1.options is Options o
            ? new PadModeSnapshot(o.gamepadMode.ToString(), o.gamepadControls, o.snappyMenus)
            : null;

        public void WriteOptions(PadModeSnapshot values)
        {
            if (Game1.options is not Options o)
                return;
            o.gamepadMode = Enum.TryParse(values.GamepadMode, out Options.GamepadModes mode) ? mode : Options.GamepadModes.Auto;
            o.gamepadControls = values.GamepadControls;
            o.snappyMenus = values.SnappyMenus;
        }

        public void TakeForceOn()
        {
            Options o = Game1.options;
            o.gamepadMode = Options.GamepadModes.ForceOn;
            o.gamepadControls = true;
            // v1 container_transfer / container_exit need SnappyMenus == false (no snap, no snapped component).
            o.snappyMenus = false;
            // Mirror of the skipped CheckGamepadMode transition block (G1:3641-3655): fade starts now. No snap call.
            Game1.timerUntilMouseFade = 0;
        }

        public void ReassertForceOn()
        {
            Game1.options.gamepadMode = Options.GamepadModes.ForceOn;
            Game1.options.gamepadControls = true;
        }

        public string? ResolvePin() => this.mod.ResolvePadPin();

        public void WritePin(bool value) =>
            (this.mod.padPinField ?? throw new InvalidOperationException("pin unresolved")).SetValue(value);

        public void Log(string line, bool warn = false) => this.mod.Monitor.Log(line, warn ? LogLevel.Warn : LogLevel.Info);
    }
}
