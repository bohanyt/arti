using System.Globalization;
using System.Text.Json;
using ArtiStardewBridge.Observation;
using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewModdingAPI.Utilities;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Monsters;
using StardewValley.TerrainFeatures;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

public sealed partial class ModEntry : Mod
{
    private const string LoopbackHost = "127.0.0.1";
    private readonly PerScreen<ScreenState> screens = new(() => new ScreenState());
    private readonly WorldPressProbe worldPressProbe = new();
    private readonly WorldPressDelayedProbe delayedWorldPressProbe = new();
    private readonly ControllerStickHoldProbe stickHoldProbe = new();
    private readonly ControllerMenuStepProbe menuStepProbe = new();
    // PART_C2: the live mod is the ONLY owner of the shared settled journal path. The parameterless inbox
    // (unit tests, harnesses) gets an isolated per-instance journal and can never touch this file.
    private readonly MenuStepCommandInbox commandInbox =
        new MenuStepCommandInbox(new CommandSettledJournal(CommandSettledJournal.DefaultPath()));
    private readonly NativeActionOwner actionOwner = new();
    private readonly UseToolHoldBackend useToolHold = new();
    private readonly UseToolNativeAdapterState useToolAdapter = new();
    private UseToolInputInvocation? useToolInput;
    private MenuStepCommandRequest? useToolInputRequest;
    private uint useToolInputTick;
    private bool useToolCleanupObserverReady;
    private ActionOwnerRecordPoller? actionOwnerPoller;
    private long lastAppliedOwnerRecordSeq;
    private ControllerStickHoldDriver? stickHoldDriver;
    private ControllerMenuStepDriver? menuStepDriver;
    private GamepadProbe? gamepadProbe;
    private SButton stickHoldButton = SButton.None;
    private int? stickHoldStartSnapId;
    private string? stickHoldStartSnapName;
    private int? stickHoldExpectedNeighborId;
    private string? commandInboxDir;
    private string commandSessionId = "";
    private CommandInboxIdentity? commandWorldIdentity;
    private readonly string runtimeInstanceId = Guid.NewGuid().ToString("N");
    private readonly ContextGenerationTracker contextGenerationTracker = new();
    private readonly MenuInstanceTokenTracker menuInstanceTokenTracker = new();
    private readonly MenuInstanceTokenTracker toolbarInstanceTokenTracker = new();
    private readonly MenuInstanceTokenTracker containerInstanceTokenTracker = new();
    private bool selectionCleanupUncertain;
    private readonly MenuInstanceTokenTracker worldTargetInstanceTokenTracker = new();
    private WorldInteractionInput? worldInteraction;
    private MenuStepCommandRequest? worldInteractionRequest;
    private Game1? worldInteractionGame;
    private StardewValley.Objects.Chest? worldInteractionChest;
    private bool worldInteractionUncertain;
    private bool transferObserverReady;
    readonly TransferDoubt transferDoubt = new(); // PM-1 fix6 #2: effect vs cleanup doubt
    private Action<IClickableMenu?,bool>? transferTransition;
    private readonly TextInputTokenTracker textInputTokenTracker = new();
    private CommandInboxSession? lastPublishedSession;
    private ModConfig config = new();
    private Actions.WaterOnceController? waterOnce;
    // V4 stage-1 background movement (S2). Both stay null unless the default-OFF
    // BackgroundMovementEnabled gate is on; no hook/latch/thread exists otherwise.
    private BackgroundContainmentWatchdog? backgroundWatchdog;
    private BackgroundMoveRuntime? backgroundMove;
    private RenderTiming.FarmhandRenderTiming? renderTiming;
    // V4 Stage 2: latest background use_tool verdict; the gather window's last_use
    // source together with the foreground invocation verdict (cleared on a new
    // foreground use_tool arm so the newest verdict always wins).
    private UseToolInputVerdict? backgroundUseToolVerdict;

    // While a background TOOL runs there is no last verdict (same as the foreground
    // TryStart, which clears its own verdict); the previous run is never re-published.
    private UseToolInputVerdict? LastUseToolVerdict => this.backgroundMove?.ToolHoldInFlight == true
        ? null
        : this.backgroundUseToolVerdict ?? this.useToolInput?.Verdict;

    bool UseToolCleanupPending =>
        this.useToolInput?.Active == true
        || this.useToolHold.HasOwnedInflight
        || this.useToolAdapter.HasOwnedNativeSurface
        || this.useToolAdapter.HasNativeCleanupDebt
        || this.useToolAdapter.LifecycleFailClosed;

    public override void Entry(IModHelper helper)
    {
        this.config = helper.ReadConfig<ModConfig>();
        this.SetupReadyCheckFacts();
        TickStallEvents.Register(helper.Events); // fix14c: first, so every later handler of these events is attributed
        NativeEventControl.GenericControlEnabled = this.config.GenericWorldInputEnabled || this.config.UiPadMenusEnabled;
        this.SetUpGenericUiMotor(helper);
        this.SetUpBarriers(helper);
        if(this.config.BackgroundMovementEnabled && this.config.BackgroundToolUiEnabled
            && this.config.WorldInteractKindsEnabled && this.config.WorldInteractionInputEnabled && this.config.MoveThroughEnabled)
        {
            try { this.houseNativeFenceReady=HouseNativeMovementFence.Install(helper.ModRegistry.ModID,this.HouseNativeMovementSafe); }
            catch(Exception ex) { this.Monitor.Log("Animal house travel unavailable: native movement fence: "+ex,LogLevel.Error); }
        }
        // MAP_INDEPENDENCE_NAV: nav grid / stands read the game's own farmer collision in every location.
        try { NativeRouteCell.Install(helper.ModRegistry.ModID); this.Monitor.Log("Nav walkability: native farmer collision in every location (effects suppressed during probes).", LogLevel.Info); }
        catch(Exception ex) { this.Monitor.Log("Nav walkability: native collision unavailable, legacy tile classes kept: "+ex.GetType().Name+" "+ex.Message,LogLevel.Warn); }
        if (this.config.GamepadInputEnabled && this.UiG3On)
            FestivalDialogueWitness.Install(helper.ModRegistry.ModID, () => this.config.Enabled && this.UiG3On, this.Monitor);
        this.renderTiming = new(message => this.Monitor.Log(message, LogLevel.Error));
        helper.ConsoleCommands.Add("arti_render_timing_off",
            "Disable S3 inactive render timing for this process and restore its original value.",
            (_, _) =>
            {
                this.config.FarmhandInactiveRenderingEnabled = false;
                this.renderTiming.Restore();
            });
        this.useToolInput = new(this.useToolHold, this.useToolAdapter);
        this.commandInbox.ExecutionSlot.ObserveController = this.ReadControllerFrame;
        this.commandInbox.ExecutionSlot.ObserveVerb = this.ObserveNativeVerb;
        this.commandInbox.ExecutionSlot.CleanupPending = () => this.selectionCleanupUncertain
            || this.worldInteractionUncertain || TransferHeldEvidence.Pending(this.transferDoubt, NowUnixMs()) || this.UiMotorUnsafe
            || this.UseToolCleanupPending || this.backgroundMove?.CleanupPending == true
            || this.stickHoldProbe.ReleaseOwed || this.menuStepProbe.ReleaseOwed || this.menuStepProbe.AwaitingEndSnap;
        this.commandInbox.ExecutionSlot.StuckObserved = this.LogRetainedSlot; // PM-1 a10 #2 (method group: no closure)
        this.actionOwner.BindRuntime(this.runtimeInstanceId);
        this.actionOwner.Slot = this.commandInbox.ExecutionSlot;
        this.actionOwner.ObserveReceiver = () => (this.receiverState.Value.Observer, this.CaptureReceiverFacts());
        this.commandInbox.ExecutionSlot.ObserveRefusal = () => (this.CaptureReceiverFacts().Epoch, checked((long)this.commandInbox.ExecutionSlot.ModTick));
        this.commandInbox.ExecutionSlot.ObserveDebts = () => this.backgroundMove?.MenuDebts ?? Array.Empty<object>();
        this.commandInbox.ExecutionSlot.ObserveClosure = this.ReadNativeClosure;
        this.commandInbox.ExecutionSlot.ObserveProgressEdge = this.ProgressEdge;
        this.commandInbox.ExecutionSlot.OpClosed += record => this.occurrenceRing?.Append(record);
        this.commandInbox.ExecutionSlot.ReceiptChanged = result => {
            if (result is not null) this.EmitCommandResult(result);
            this.lastPublishedSession = null; this.PublishCommandSessionSafe();
        };
        AppDomain.CurrentDomain.ProcessExit += (_, _) => this.actionOwner.WriteModExit(NowUnixMs());
        IPadModeHost? padModeHost = this.SetUpPadHost(helper); // WS-G0: null unless GamepadInputEnabled
        if (this.config.BackgroundMovementEnabled)
        {
            // Dedicated containment thread starts once here; it can only arm on a
            // farmhand (policy refuses main player / split-screen).
            this.backgroundWatchdog = new BackgroundContainmentWatchdog();
            this.backgroundWatchdog.Start();
            HelperPressPort.OwnerPhysicallyDown = OwnerInputProbe.RawDown; // review R5 S1: the owner's physical buttons
            this.backgroundMove = new BackgroundMoveRuntime(
                helper,
                this.Monitor,
                this.actionOwner,
                this.backgroundWatchdog,
                this.runtimeInstanceId,
                this.CurrentCommandIdentity,
                () => this.config.BackgroundFarmToolsEnabled,
                padModeHost,
                () => this.UiG3On);
            this.backgroundMove.ExecutionSlot = this.commandInbox.ExecutionSlot;
            this.backgroundMove.ReceiverEpoch = () => this.CaptureReceiverFacts().Epoch;
        }
        if (this.config.BackgroundToolUiEnabled && !this.config.BackgroundMovementEnabled)
        {
            this.Monitor.Log(
                "BackgroundToolUiEnabled requires BackgroundMovementEnabled; background use_tool/world_interact stay disabled.",
                LogLevel.Warn);
        }
        this.waterOnce = new Actions.WaterOnceController(
            helper,
            this.Monitor,
            () => this.config.WaterOnceEnabled,
            () => this.commandInbox.ExecutionSlot.CleanupPending?.Invoke() == true
                || this.MutationInflight && this.commandInbox.InflightOp != MenuStepCommandInbox.OpWaterOnce,
            this.ApplyActionOwnerRecords,
            this.AdviseWaterOnce);
        this.waterOnce.OnTerminal = this.OnWaterOnceTerminal;
        if (this.config.Port is < 1 or > 65535)
        {
            this.Monitor.Log(
                $"Port {this.config.Port} tidak sah; bridge read-only dimatikan.",
                LogLevel.Error
            );
            this.config.Enabled = false;
        }

        helper.ConsoleCommands.Add(
            WorldPressProbe.CommandName,
            "TEST-ONLY farmhand world press. Usage: arti_probe_press <left|right|up|down> <ticks>",
            this.OnProbePressCommand
        );
        helper.ConsoleCommands.Add(
            WorldPressDelayedProbe.CommandName,
            "TEST-ONLY delayed farmhand world press. Usage: arti_probe_after <delayTicks> <left|right|up|down> <pressTicks>",
            this.OnProbeAfterCommand
        );
        helper.ConsoleCommands.Add(
            ControllerMenuStepProbe.CommandName,
            "TEST-ONLY P5-R3C menu_step candidate: hold analog until native snap changes, "
                + "then Suppress on the next UpdateTicking. "
                + "Disabled unless ControllerMenuStepEnabled=true. "
                + "Usage: arti_menu_step <down|right|up|left> [timeoutTicks] [delayTicks]",
            this.OnMenuStepCommand
        );
        helper.ConsoleCommands.Add(
            ControllerStickHoldProbe.CommandName,
            "TEST-ONLY P5-R3C analog hold-duration. "
                + "Disabled unless ControllerStickHoldProbeEnabled=true. "
                + "Usage: arti_probe_stickhold <down|right|up|left> <holdTicks> [delayTicks]",
            this.OnProbeStickHoldCommand
        );
        helper.ConsoleCommands.Add(
            ControllerMenuSetup.CommandName,
            "TEST-ONLY setup: construct GameMenu for an eligible farmhand. "
                + "Not motor proof that input can open a menu. "
                + "Gated by ControllerMenuStepEnabled or ControllerStickHoldProbeEnabled.",
            this.OnProbeMenuCommand
        );
        helper.ConsoleCommands.Add("arti_stage_event", "TEST-ONLY operator staging (requires OperatorEventStagingEnabled): arti_stage_event question|control|festival-start",
            this.OnStageEventCommand);
        this.SetUpHostRehearsalConsole(helper); // REHEARSAL-ONLY arti_host; refuses unless RehearsalHostConsoleEnabled
        // WS-G0 (PLAN 1.5 f): the legacy foreground pad drivers are a second pad writer; they are not
        // constructed with GamepadInputEnabled on, and their entry points refuse (LegacyPadWriterRefused).
        if (!this.config.GamepadInputEnabled)
        {
            this.stickHoldDriver = new ControllerStickHoldDriver(
                this.stickHoldProbe,
                new HelperStickHoldInput(helper.Input)
            );
            this.menuStepDriver = new ControllerMenuStepDriver(
                this.menuStepProbe,
                new HelperStickHoldInput(helper.Input)
            );
        }
        this.SetUpCommandInbox(helper);
        if (this.config.GamepadProbeEnabled)
        {
            string inbox = (this.config.CommandInboxDirectory ?? "").Trim().TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string probeDir = inbox.Length > 0
                ? Path.Combine(Path.GetDirectoryName(inbox) ?? inbox, "probe-gamepad")
                : Path.Combine(helper.DirectoryPath, "probe-gamepad");
            this.gamepadProbe = new GamepadProbe(helper, this.Monitor, () => this.config.GamepadProbeEnabled, probeDir,
                () => this.config.ReflexProbeEnabled);
        }
        // UpdateTicking, BUKAN UpdateTicked: SCore melipat override ke
        // ControllerState (ApplyOverrides) SESUDAH event ini dan SEBELUM
        // Game1.Update, jadi hanya dari sini injeksi terlihat oleh
        // CheckGamepadMode pada tick yang sama. Bukti IL: P5-R1 bagian 6.5.
        try
        {
            UseToolCleanupUpdateObserver.Install(this.ModManifest.UniqueID, this.ObserveUseToolGameUpdate);
            this.useToolCleanupObserverReady = true;
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"use-tool cleanup observer unavailable; cleanup stays fail-closed: {ex}", LogLevel.Error);
        }
        helper.Events.GameLoop.UpdateTicking += this.OnUpdateTicking;
        try { UseToolCleanupUpdateObserver.InstallTransferReceipt(this.ModManifest.UniqueID,
            (after,ok)=>{ this.transferTransition?.Invoke(after,ok); this.uiKeyTransition?.Invoke(after,ok); this.receiverState.Value.Surface = null; this.CaptureReceiverFacts(); });
            UseToolCleanupUpdateObserver.InstallReceiverReceipt(this.ModManifest.UniqueID, () => {
                try { this.receiverState.Value.Surface = null; this.CaptureReceiverFacts(); }
                catch (Exception ex) { this.Monitor.Log($"receiver child observation failed: {ex}", LogLevel.Error); }
            });
            this.transferObserverReady=true; }
        catch(Exception ex) { this.Monitor.Log($"transfer receiver observer unavailable: {ex}",LogLevel.Error); }
        helper.Events.GameLoop.UpdateTicked += this.OnUpdateTicked;
        helper.Events.GameLoop.ReturnedToTitle += this.OnReturnedToTitle;
        helper.Events.Player.Warped += this.OnWarped;
        helper.Events.Content.AssetsInvalidated += this.OnKnowledgeAssetsInvalidated;
        helper.Events.Player.InventoryChanged += this.OnInventoryChanged;
        helper.Events.World.ObjectListChanged += this.OnObjectListChanged;
        helper.Events.World.TerrainFeatureListChanged += this.OnTerrainFeatureListChanged;
        helper.Events.World.NpcListChanged += this.OnNpcListChanged;
        this.ReportNativeCompatibility();
        this.Monitor.Log(
            this.config.Enabled
                ? $"Mata read-only siap; hello diarahkan ke {LoopbackHost}:{this.config.Port}."
                : "Mata read-only nonaktif lewat config.",
            LogLevel.Info
        );
    }

    private void OnUpdateTicked(object? sender, UpdateTickedEventArgs e)
    {
        // PM-1 fix14e (CRT-06, ADR 34): an attempt admitted anywhere in this handler starts its tick-lapse clock when
        // the handler returns (finally: also on early returns and exceptions). One method, so IL D-row keys are
        // unchanged; the body keeps its original indentation (source-shape tests pin it).
        try {
        if (BridgeExitWhenSafe.Quitting) return; // ADR 256 (W1b E3): after a published "exiting", nothing more is done
        TickStallEvents.Probe.Enter("ticked:receiver");
        this.commandInbox.ExecutionSlot.AdvanceTick(NowUnixMs());
        this.CaptureReceiverFacts();
        this.EndGenericUiFrame();
        this.renderTiming?.Tick(this.config.FarmhandInactiveRenderingEnabled);
        this.gamepadProbe?.OnUpdateTicked(e.Ticks);
        this.PollPadPanicFile(e.Ticks); // WS-G0 pad panic control file (no-op unless GamepadInputEnabled)
        this.ReadUseToolCleanupAfterUpdate(e.Ticks);
        this.ApplyActionOwnerRecords();
        this.RequestOwnedMutationRelease();
        this.TickBackgroundMoveAfter();
        TickStallEvents.Probe.Enter("ticked:occurrences");
        // HOST observation is independent of world readiness and command-inbox execution flags.
        if (Context.IsMainPlayer) this.TickStoryMemory();
        this.TickOccurrences();
        this.TickBarriers();
        TickStallEvents.Probe.Enter("ticked:ui_watchdog");
        this.TickGenericUiWatchdog();

        // Probe P2/P3 harus hidup SETIAP update tick, bukan 10 Hz observer,
        // dan tidak tergantung config bridge read-only. Delayed dulu:
        // trigger boleh meng-arm P2, lalu tick yang sama menekan sekali.
        if (!Game1.paused && !Game1.HostPaused)
            this.TickDelayedWorldPressProbe(e);
        TickStallEvents.Probe.Enter("ticked:world_probes");
        this.TickWorldPressProbe();
        this.TickStickHoldAfter(e);
        this.TickMenuStepAfter(e);
        this.PollOperatorEventStaging(e.Ticks); // operator-only file; independent of hidden SMAPI stdin

        // Kotak masuk perintah SESUDAH tick lifecycle: satu permintaan baru
        // hanya boleh di-arm setelah putusan run sebelumnya tertulis. Sengaja
        // di atas gerbang config.Enabled -- jalur perintah tidak bergantung
        // pada bridge observer read-only, sama seperti probe di atasnya.

        // Native UI G3-0/G3-1 dulu: setiap tick eligible, SEBELUM sample 10 Hz
        // dan SEBELUM Pending latest-wins. Busy send boleh skip kawat, bukan
        // capture/history. SMAPI memanggil event ini PER SCREEN.
        TickStallEvents.Probe.Enter("ticked:pre_world");
        if (!this.config.Enabled || !Context.IsWorldReady)
        {
            this.TickJoinScreenFacts(); // Live7: join_screen.json while not in a world (no-op with CharacterCreationEnabled off)
            this.LogPlayerControlUnavailable(this.screens.Value);
            this.ReadWorldInteractionAfterUpdate(e.Ticks);
            this.TickUiKeySettle();
            this.TickUiPadSettle(); // WS-G1
            this.TickCommandInbox();
            // ADR 256 (W1b): out of the world a request is still answered (waiting world_not_ready, expired, refused); an
            // exit needs her save and farmer, so it is decided here only in a ready world with the observer off.
            if (BridgeExitWhenSafe.Tick(this.commandInboxDir, this.runtimeInstanceId, this.InputInflight, this.Monitor,
                this.actionOwner.StopLatched))
                Game1.game1.Exit();
            return;
        }

        ScreenState screen = this.screens.Value;
        TickStallEvents.Probe.Enter("ticked:discoveries");
        this.ObserveWorldDiscoveries(screen);
        TickStallEvents.Probe.Enter("ticked:ui_context");
        this.AdvanceUiContextNative(screen);
        TickStallEvents.Probe.Enter("ticked:settle_inbox");
        this.ReadWorldInteractionAfterUpdate(e.Ticks);
        this.TickUiKeySettle();
        this.TickUiPadSettle(); // WS-G1
        this.TickCommandInbox();
        // ADR 256 "exit when safe" (W1b E3): the answer is published durably, then the quit, in this tick. Her clean-exit
        // record is the process-exit hook's (above), so it exists only when the process really ends.
        if (BridgeExitWhenSafe.Tick(this.commandInboxDir, this.runtimeInstanceId, this.InputInflight, this.Monitor,
                this.actionOwner.StopLatched))
        {
            Game1.game1.Exit(); // ADR 256 (M4c rule 6): nothing between the last veto and this call
            this.Monitor.Log("EXIT_WHEN_SAFE exiting (ADR 256)", LogLevel.Info);
            return;
        }

        if (!e.IsMultipleOf(6))
            return;

        // FIXB37: use the canonical state capture's native facts. Log transitions BEFORE the
        // socket/Pending gates, so onset and clearance survive a soak stop or disconnected reader.
        TickStallEvents.Probe.Enter("ticked:player_control");
        PlayerControlDiagnostics playerControl = CapturePlayerControlDiagnostics();
        this.LogPlayerControlTransition(screen, playerControl);

        // LATEST-STATE-WINS: kalau kiriman sebelumnya belum selesai, buang
        // snapshot kawat ini. JANGAN menumpuk antrean -- backlog membuat motor
        // membaca posisi masa lalu, dan itu lebih berbahaya daripada
        // kehilangan satu frame. Capture native G3-0 sudah terjadi di atas.
        if (screen.Pending is { IsCompleted: false })
            return;

        if (screen.Pending is not null)
        {
            TryAcknowledgeCompletedSend(screen);
            if (screen.Pending is { IsCompletedSuccessfully: true } selesai && selesai.Result
                && !screen.HelloDilaporkan)
            {
                screen.HelloDilaporkan = true;
                this.Monitor.Log(
                    $"Hello terkirim: farmer={Game1.player.Name}, "
                        + $"player_id={Game1.player.UniqueMultiplayerID}, screen={Context.ScreenId}.",
                    LogLevel.Debug
                );
            }
            screen.Pending = null;
        }

        TickStallEvents.Probe.Enter("ticked:hello");
        screen.Client ??= new BridgeClient(LoopbackHost, this.config.Port);
        BridgeClient client = screen.Client;

        // SEMUA pembacaan Game1/Context terjadi DI SINI, di game thread.
        // Lapis async di bawah hanya menyerialisasi DTO yang sudah jadi.
        // Membaca Game1 dari Task.Run itu balapan dengan game loop.
        long playerId = Game1.player.UniqueMultiplayerID;
        int screenId = Context.ScreenId;
        string playerName = Game1.player.Name;
        bool isMainPlayer = Context.IsMainPlayer;
        bool isSplitScreen = Context.IsSplitScreen;

        if (!client.HelloSent)
        {
            screen.HelloDilaporkan = false;
            string saveToken = Constants.SaveFolderName;
            if (string.IsNullOrWhiteSpace(saveToken))
                saveToken = Game1.uniqueIDForThisGame.ToString();
            if (screen.Scene.HasSession)
            {
                screen.Scene.OnReconnect(saveToken, playerId);
                screen.Deltas.Invalidate();
            }
            else
                screen.Scene.BeginSession(saveToken, playerId);
            screen.Inventory.BindSession(screen.Scene.SessionId);
            screen.UiContext.BindSession(screen.Scene.SessionId);
            screen.Knowledge.BindSession(screen.Scene.SessionId);
            screen.KnowledgeSnapshotSent = false;
            screen.UiText.BindSession(screen.Scene.SessionId);
            screen.Pending = client.EnsureHelloAsync(seq =>
                string.IsNullOrWhiteSpace(playerName)
                    ? null
                    : HelloEnvelope.Create(
                        seq,
                        playerId,
                        screenId,
                        playerName,
                        isMainPlayer,
                        isSplitScreen
                    )
            );
            return;
        }

        TickStallEvents.Probe.Enter("ticked:payload");
        StatePayload payload = this.CapturePayload(screen, playerControl);
        bool affordances = this.config.UiPadMenusEnabled && this.PadPathConfigured && this.WorldInteractionBackgroundReady;
        ScenePreparedFact gather = SceneSnapshotCapture.CaptureGather(Game1.player, this.LastUseToolVerdict,
            this.config.BackgroundFarmToolsEnabled);
        var sceneLocation = Game1.player.currentLocation;
        var mapAffordances = affordances ? SceneSnapshotCapture.CaptureMapAffordances(sceneLocation, payload.Tile,
            screen.WorldKnowledge, GenericWorldCapture.CodeCells(this, Game1.player, sceneLocation)) : null;
        if (!screen.Scene.SnapshotNeeded)
        {
            screen.Deltas.Upsert(gather);
            if (mapAffordances is { } extra) screen.Deltas.Upsert(extra);
            RefreshDoorFactsOnClock(screen);
        }
        ObservationPayload? sceneObs = null;
        if (screen.Scene.SnapshotNeeded)
        {
            long revision = screen.Scene.ReserveSnapshotRevision();
            try
            {
                screen.SceneCapture.ObserveAffordances = affordances;
                sceneObs = screen.SceneCapture.CaptureCurrent(
                    Game1.player,
                    this.Helper.Multiplayer.GetActiveLocations(),
                    screen.Scene,
                    revision
                );
                sceneObs = SceneSnapshotBuilder.WithGather(sceneObs, gather, mapAffordances);
            }
            catch (Exception ex)
            {
                this.Monitor.Log($"OBS-1 capture gagal: {ex}", LogLevel.Warn);
                sceneObs = SceneSnapshotBuilder.Error(
                    screen.Scene.HasSession ? screen.Scene.SessionId : "obs:missing:0:0",
                    revision,
                    "unavailable",
                    ex.GetType().Name
                );
            }
        }
        else if (
            screen.Deltas.TryBuildDelta(
                screen.Scene.SessionId,
                screen.Scene.DomainRevision,
                this.ClassifyCurrentSceneAuthority(),
                out ObservationPayload? delta
            )
        )
        {
            long revision = screen.Scene.ReserveDeltaRevision();
            if (delta!.DomainRevision != revision)
                throw new InvalidOperationException(
                    "OBS-2B1 delta revision tidak cocok dengan scheduler reserve."
                );
            sceneObs = delta;
        }
        else if (screen.Deltas.RequiresSnapshot)
        {
            // Builder menolak patch yang masih ≥ MAX_LINE_BYTES setelah cap.
            // Jangan diam-diam macetkan accumulator; snapshot penuh adalah resync.
            RequestOverflowSnapshot(screen);
        }

        string? sceneLocationId = sceneLocation?.NameOrUniqueName;
        if (sceneObs is not null)
        {
            if (sceneObs.Kind == ObservationKind.Snapshot)
                screen.Deltas.StageSnapshot(sceneObs);
            screen.InflightSceneAck = screen.Scene.CurrentAckToken();
            screen.InflightSceneLocationId = sceneLocationId ?? "";
            screen.InflightScenePayload = sceneObs;
        }
        else
        {
            screen.ClearInflightScene();
        }

        TickStallEvents.Probe.Enter("ticked:inventory");
        ObservationPayload? inventoryObs = CaptureInventoryObservation(screen);
        if (inventoryObs is not null)
        {
            screen.InflightInventoryAck = screen.Inventory.CurrentAckToken();
            screen.InflightInventoryPayload = inventoryObs;
        }
        else
        {
            screen.ClearInflightInventory();
        }

        // Unchanged ui.context is re-sent at its SAME revision, bounded by
        // ObservationRefreshIntervalMs, so the receiver's freshness window holds.
        ObservationPayload? uiObs = screen.UiContext.ConsumeDirtySnapshot()
            ?? screen.UiContext.ConsumeRefreshSnapshot(NowUnixMs(), this.config.ObservationRefreshIntervalMs);
        if (uiObs is not null)
        {
            screen.InflightUiPayload = uiObs;
        }
        else
        {
            screen.ClearInflightUi();
        }

        // ui.text sesudah ui.context. Kalau payload tidak muat 32KiB,
        // Consume mengembalikan null dan dirty tetap — jangan kelaparan context.
        ObservationPayload? uiTextObs = screen.UiText.ConsumeDirtyPayload();
        if (uiTextObs is not null)
        {
            screen.InflightUiTextPayload = uiTextObs;
        }
        else
        {
            screen.ClearInflightUiText();
        }

        TickStallEvents.Probe.Enter("ticked:knowledge");
        ObservationPayload? knowledgeObs = this.CaptureWorldKnowledge(screen, e.Ticks);
        screen.InflightKnowledge = knowledgeObs;
        screen.Pending = SendMotorThenSceneAsync(
            client,
            playerId,
            screenId,
            payload,
            sceneObs,
            inventoryObs,
            uiObs,
            uiTextObs,
            knowledgeObs
        );
        } finally {
            this.PublishHostGiftSafety();
            this.commandInbox.ExecutionSlot.EndTickedHandler(NowUnixMs());
        }
    }

    /// <summary>
    /// Ack snapshot HANYA di game thread, memakai token yang di-capture
    /// sebelum kirim. Completion session lama tidak boleh menyentuh session baru.
    /// </summary>
    private static void TryAcknowledgeCompletedSend(ScreenState screen)
    {
        bool ok = screen.Pending is { IsCompletedSuccessfully: true } selesai && selesai.Result;
        ObservationPayload? sent = screen.InflightScenePayload;
        if (sent is not null)
        {
            if (ok)
            {
                if (sent.Kind == ObservationKind.Snapshot)
                {
                    bool current = screen.Scene.TryMarkSnapshotSent(
                        screen.InflightSceneAck,
                        screen.InflightSceneLocationId
                    );
                    if (current && !screen.Deltas.TryCommitStagedSnapshot(sent))
                    {
                        screen.Deltas.Invalidate();
                        screen.Scene.RequestSnapshot();
                    }
                }
                else if (sent.Kind == ObservationKind.Delta)
                {
                    bool current = screen.Scene.TryMarkDeltaSent(screen.InflightSceneAck);
                    if (current && !screen.Deltas.TryCommitDelta(sent))
                    {
                        screen.Deltas.Invalidate();
                        screen.Scene.RequestSnapshot();
                    }
                }
                else
                {
                    screen.Scene.TryMarkSnapshotSent(
                        screen.InflightSceneAck,
                        screen.InflightSceneLocationId
                    );
                    screen.Deltas.Invalidate();
                }
            }
            if (!ok && sent.Kind == ObservationKind.Delta)
            {
                // Hasil gagal bersifat ambigu: receiver mungkin melihat R+1 atau
                // tidak. Jangan lanjut R+2 dalam session yang sama. Buang client
                // supaya jalur hello di bawah membuka observation session baru.
                screen.AbandonClient();
                screen.Deltas.Invalidate();
                screen.Scene.RequestSnapshot();
            }
        }
        screen.ClearInflightScene();

        ObservationPayload? inventorySent = screen.InflightInventoryPayload;
        if (inventorySent is not null && ok)
        {
            screen.Inventory.TryMarkSnapshotSent(
                screen.InflightInventoryAck,
                screen.InflightInventoryFingerprint,
                NowUnixMs()
            );
        }
        screen.ClearInflightInventory();

        ObservationPayload? uiSent = screen.InflightUiPayload;
        if (uiSent is not null)
        {
            if (ok)
                screen.UiContext.AckSent(NowUnixMs());
            else
                screen.UiContext.NackSent();
        }
        screen.ClearInflightUi();

        ObservationPayload? uiTextSent = screen.InflightUiTextPayload;
        if (uiTextSent is not null)
        {
            if (ok)
                screen.UiText.AckSent();
            else
                screen.UiText.NackSent();
        }
        screen.ClearInflightUiText();
        if (screen.InflightKnowledge is not null)
        {
            if (ok)
            {
                screen.Knowledge.AckSent(NowUnixMs());
                screen.KnowledgeSnapshotSent = screen.InflightKnowledge.Kind != ObservationKind.Error;
                if (screen.InflightKnowledge.Body?.Removes is { } removed)
                    screen.KnowledgeWire.AckRemoves(removed);
            }
            else
            {
                screen.Knowledge.NackSent();
                screen.KnowledgeSnapshotSent = false;
            }
            screen.InflightKnowledge = null;
        }
    }

    /// <summary>
    /// Snapshot inventaris on-demand. HARUS dipanggil dari game thread.
    /// Fingerprint menekan state yang tidak berubah; bukan emit 10 Hz.
    /// </summary>
    private ObservationPayload? CaptureInventoryObservation(ScreenState screen)
    {
        if (!screen.Inventory.HasSession)
            return null;

        InventorySnapshotInput input;
        try
        {
            input = InventorySnapshotCapture.Read(Game1.player);
        }
        catch (Exception ex)
        {
            if (!screen.Inventory.SnapshotNeeded)
                return null;
            this.Monitor.Log($"OBS-3A capture gagal: {ex}", LogLevel.Warn);
            long revision = screen.Inventory.ReserveSnapshotRevision();
            screen.InflightInventoryFingerprint = "";
            return InventorySnapshotBuilder.Error(
                screen.Inventory.SessionId,
                revision,
                "unavailable",
                ex.GetType().Name
            );
        }

        string fingerprint = InventoryIdentity.Fingerprint(input);
        screen.Inventory.ObserveFingerprint(fingerprint);
        if (!screen.Inventory.SnapshotNeeded || screen.Inventory.TrySuppressUnchanged(fingerprint))
        {
            // Unchanged inventory: re-send the last acknowledged facts at the CURRENT
            // revision (no ReserveSnapshotRevision), bounded by ObservationRefreshIntervalMs,
            // so the receiver's 2 s freshness window holds. The normal ack path accepts it
            // (token revision == DomainRevision); a failed send changes nothing.
            if (!screen.Inventory.RefreshDue(fingerprint, NowUnixMs(), this.config.ObservationRefreshIntervalMs))
                return null;
            screen.InflightInventoryFingerprint = fingerprint;
            return InventorySnapshotBuilder.Snapshot(
                screen.Inventory.SessionId,
                screen.Inventory.DomainRevision,
                input
            );
        }

        long reserved = screen.Inventory.ReserveSnapshotRevision();
        screen.InflightInventoryFingerprint = fingerprint;
        return InventorySnapshotBuilder.Snapshot(
            screen.Inventory.SessionId,
            reserved,
            input
        );
    }

    /// <summary>
    /// Authority delta dibaca di game thread tanpa capture scene baru.
    /// </summary>
    private SceneAuthorityDecision ClassifyCurrentSceneAuthority()
    {
        GameLocation? location = Game1.player?.currentLocation;
        bool activeSynced = location is not null
            && this.Helper.Multiplayer.GetActiveLocations()
                .Any(candidate => ReferenceEquals(candidate, location));
        return SceneAuthority.Classify(
            hasCurrentLocation: location is not null,
            isCurrentFarmerLocation: location is not null,
            locationIsActiveSynced: activeSynced
        );
    }

    /// <summary>
    /// Potret keadaan farmer layar ini. HARUS dipanggil dari game thread.
    /// </summary>
    private StatePayload CapturePayload(ScreenState screen, PlayerControlDiagnostics playerControl)
    {
        Farmer player = Game1.player;
        Point tile = player.TilePoint;
        MenuInfo? menu = CaptureMenu();
        return new StatePayload
        {
            Native = JsonSerializer.SerializeToElement(new { halt = this.actionOwner.HaltFact, rearmed = this.actionOwner.Rearmed, halt_notes = this.actionOwner.HaltNotes.ToArray() }),
            Location = player.currentLocation?.Name ?? "",
            LocationId = CurrentLocationId(player),
            Tile = new TileXY { X = tile.X, Y = tile.Y },
            Pixel = new PixelXY { X = player.Position.X, Y = player.Position.Y },
            Facing = player.FacingDirection,
            TimeOfDay = NativeWorldClock.Now,
            Date = new GameDate
            {
                Season = Game1.currentSeason ?? "",
                Day = Game1.dayOfMonth,
                Year = Game1.year,
            },
            Energy = player.Stamina,
            Health = player.health,
            OwnState = NativePersonaFacts.OwnState(player),
            PeerSleep = NativePersonaFacts.Sleep(player, Game1.MasterPlayer, Game1.getOnlineFarmers()),
            MenuContainer = NativePersonaFacts.Container(CurrentInteractiveMenu(Game1.activeClickableMenu)),
            // Helper SMAPI, bukan tebakan sendiri: keduanya sudah
            // memperhitungkan menu, cutscene, festival, dan event.
            IsPlayerFree = Context.IsPlayerFree,
            CanPlayerMove = Context.CanPlayerMove,
            PlayerControl = playerControl,
            ActiveMenu = Game1.activeClickableMenu?.GetType().Name,
            UiRevision = screen.UiRevision.Observe(menu),
            Menu = menu,
            Inventory = CaptureInventory(player),
        };
    }

    /// <summary>
    /// Receiver interaktif yang sama dengan yang dibaca CaptureMenu.
    /// <c>GameMenu</c> hanya bingkai; halaman aktifnya (<c>GetCurrentPage</c>)
    /// yang harus di-token. Ganti halaman = ganti receiver.
    /// </summary>
    private static IClickableMenu? CurrentInteractiveMenu(IClickableMenu? menu)
    {
        if (menu is null)
            return null;
        return menu is GameMenu gm ? (gm.GetCurrentPage() ?? menu) : menu;
    }

    /// <summary>
    /// Receiver token in the SAME namespace ui.context publishes (<c>rcv:miN</c>).
    /// Verdicts and receiver readbacks are compared by Python (and by the next
    /// transfer/exit target) against ui.context, so they must never carry the raw
    /// tracker serial. Live 2026-10-07 attempt 10z43: a background chest open
    /// returned the raw serial while ui.context showed <c>rcv:</c>+serial; the open read back uncertain.
    /// </summary>
    private string? ReceiverTokenOf(object? receiver) =>
        InspectContextQuery.ReceiverToken(this.menuInstanceTokenTracker.Observe(receiver));

    /// <summary>
    /// G3-0: capture/dirty UI sekarang. Game thread. Reuse tracker + builder
    /// G2. HOST/split-screen gagal tertutup tanpa meminjam facts layar lain.
    /// Tidak mengirim kawat dan tidak menumpuk antrean.
    /// Observation envelope session = <c>screen.Scene.SessionId</c>;
    /// <c>facts.session_id</c> = <c>commandSessionId</c> (G2 inspect_context).
    /// </summary>
    private void AdvanceUiContextNative(ScreenState screen)
    {
        if (Context.IsMainPlayer || Context.IsSplitScreen)
        {
            screen.UiContext.AdvanceUnsupported();
            screen.UiText.AdvanceUnsupported();
            return;
        }

        Farmer? farmer = Game1.player;
        GameLocation? location = farmer?.currentLocation;
        string locationRaw = location is null
            ? ""
            : location.NameOrUniqueName ?? location.Name ?? "";
        string locationId = string.IsNullOrEmpty(locationRaw)
            ? ""
            : SceneIdentity.Token(locationRaw);
        if (farmer is null || location is null || locationId.Length == 0)
        {
            screen.UiContext.AdvanceUnsupported();
            screen.UiText.AdvanceUnsupported();
            return;
        }

        IClickableMenu? interactive = CurrentInteractiveMenu(Game1.activeClickableMenu);
        string? menuInstanceToken = this.menuInstanceTokenTracker.Observe(interactive);
        long contextGeneration = this.contextGenerationTracker.Observe(
            locationId, menuInstanceToken);
        MenuInfo? menu = CaptureMenu();
        long uiRevision = screen.UiRevision.Observe(menu);
        object? textSubscriber = Game1.keyboardDispatcher?.Subscriber;
        bool hasTextInput = textSubscriber is not null;
        string? textInputToken = this.textInputTokenTracker.Observe(textSubscriber);
        // G3-1 memakai MenuInfo + token G3-0 yang sama pada tick ini.
        // Capture ticks = native Game1.ticks.
        this.AdvanceUiTextNative(
            screen,
            Game1.ticks,
            locationId,
            contextGeneration,
            uiRevision,
            menuInstanceToken,
            menu,
            farmer.UniqueMultiplayerID);
        // Envelope/accumulator epoch tetap milik session observasi
        // (BindSession di hello). facts.session_id milik command-inbox
        // yang sama dengan G2 inspect_context / CurrentCommandIdentity.
        // Bukan observation epoch, dan bukan session ID ketiga.
        if (!screen.Scene.HasSession)
            return;
        if (this.commandSessionId.Length == 0)
            return;

        Dictionary<string, object> snapshot = InspectContextQuery.BuildSnapshot(
            this.runtimeInstanceId,
            contextGeneration,
            Context.ScreenId,
            uiRevision,
            InspectContextQuery.InitialViewportTransformRevision,
            locationId,
            this.commandSessionId,
            farmer.UniqueMultiplayerID,
            Context.IsPlayerFree,
            Context.CanPlayerMove,
            menu,
            menuInstanceToken,
            this.config.WorldPressEnabled,
            this.config.ControllerMenuStepEnabled,
            Context.IsWorldReady,
            hasTextInput,
            textInputToken, BgToolUiRoute.AdmitTerm(this.UseToolInputReady, this.BackgroundUseToolEligibleNow));

        this.EnrichToolbarContext(snapshot);
        this.EnrichGenericWorldContext(snapshot);
        this.EnrichContainerContext(snapshot);
        this.EnrichShippingBinContext(snapshot); // WS-G2: only with EconomyMenusEnabled and the bin menu open
        this.EnrichWorldInteractionContext(snapshot);
        this.EnrichUiKeyContext(snapshot);
        this.EnrichUiPadContext(snapshot); // WS-G1: only with UiPadMenusEnabled
        this.EnrichEventMovementContext(snapshot); // FIXB53: native pinned event control, separate scope
        InspectContextQuery.AddMoveSegment(snapshot, this.backgroundMove is not null); // fix89D / ADR 158; ADR 244: no focus term
        InspectContextQuery.AddMovePath(snapshot, this.backgroundMove is { PadPathOn: true }); // fix92A / ADR 162; ADR 244
        RequireUiActionFacts(snapshot);
        UiContextFitLog.Note(this.Monitor, UiContextWire.Fit(snapshot)); // ADR 44: the published line always fits the receiver

        screen.UiContext.Advance(snapshot);
    }

    /// <summary>
    /// G3-1: presented-text evidence dari MenuInfo tick yang sama.
    /// Tidak mengulangi capture menu. HOST/split sudah gagal tertutup di pemanggil.
    /// Capture time = <c>Game1.ticks</c>, bukan receiver seq.
    /// </summary>
    private void AdvanceUiTextNative(
        ScreenState screen,
        long ticks,
        string locationId,
        long contextGeneration,
        long uiRevision,
        string? menuInstanceToken,
        MenuInfo? menu,
        long playerId
    )
    {
        UiTextTickSample sample = UiTextTickSample.FromCapturedMenu(
            menu,
            menuInstanceToken,
            ticks,
            this.runtimeInstanceId,
            this.commandSessionId,
            playerId,
            Context.ScreenId,
            locationId,
            contextGeneration,
            uiRevision);
        screen.UiText.AdvanceBatch(this.CaptureScreenText(sample));
    }

    /// <summary>
    /// Potret kursor UI. HARUS dipanggil dari game thread.
    /// </summary>
    private static MenuInfo? CaptureMenu()
    {
        IClickableMenu? menu = Game1.activeClickableMenu;
        if (menu is null)
            return null;

        IClickableMenu inner = CurrentInteractiveMenu(menu) ?? menu;
        ClickableComponent? snap =
            menu.currentlySnappedComponent ?? inner.currentlySnappedComponent;
        string menuType = menu.GetType().Name;
        string pageType = inner.GetType().Name;
        List<(ClickableComponent Component, string Source, int Index)> raw = new();
        AddExistingComponents(raw, menu.allClickableComponents, "frame");
        if (!ReferenceEquals(inner, menu))
            AddExistingComponents(raw, inner.allClickableComponents, "page");
        if (snap is not null && !raw.Any(x => ReferenceEquals(x.Component, snap)))
            raw.Add((snap, "focus", raw.Count));

        var components = raw
            .Take(512)
            .Select(x => CaptureComponent(
                x.Component, x.Source, x.Index, menuType, pageType,
                menu.upperRightCloseButton?.myID, snap
            ))
            .ToArray();
        if (menu is DialogueBox dialogue)
            EnrichDialogue(components, raw, dialogue);
        int focusIndex = snap is null
            ? -1
            : raw.FindIndex(x => ReferenceEquals(x.Component, snap));
        string? focusKey = focusIndex >= 0 && focusIndex < components.Length
            ? components[focusIndex].Key
            : null;

        return new MenuInfo
        {
            Name = menuType,
            MenuType = menuType,
            PageType = pageType,
            SnapId = snap?.myID,
            SnapLabel = VisibleLabel(snap),
            SnapBounds = BoundsOrNull(snap),
            CloseId = menu.upperRightCloseButton?.myID,
            FocusKey = focusKey,
            VisibleText = menu is DialogueBox box
                ? box.getCurrentString()
                : menuType == "ReadyCheckDialog"
                    ? string.Join("\n", components
                        .Select(x => x.Text)
                        .Where(x => !string.IsNullOrWhiteSpace(x))
                        .Distinct())
                    : null,
            Speaker = menu is DialogueBox
                ? Game1.currentSpeaker?.displayName
                : null,
            Components = components,
        };
    }

    private static void AddExistingComponents(
        List<(ClickableComponent Component, string Source, int Index)> target,
        IEnumerable<ClickableComponent>? source,
        string sourceName
    )
    {
        if (source is null)
            return;
        int index = 0;
        foreach (ClickableComponent? component in source)
        {
            if (component is null)
                continue;
            if (!target.Any(x => ReferenceEquals(x.Component, component)))
                target.Add((component, sourceName, index));
            index++;
        }
    }

    private static UiComponentInfo CaptureComponent(
        ClickableComponent component,
        string source,
        int index,
        string menuType,
        string pageType,
        int? closeId,
        ClickableComponent? focused
    )
    {
        (string key, string role) = SemanticComponent(
            component.myID, source, index, menuType, pageType, closeId
        );
        string? text = VisibleLabel(component);
        int? count = null;
        if (role == "inventory_slot" && component.myID < Game1.player.Items.Count)
        {
            Item? item = Game1.player.Items[component.myID];
            text = item?.DisplayName;
            count = item?.Stack;
        }
        return new UiComponentInfo
        {
            Key = key,
            NativeId = component.myID,
            Source = source,
            Role = role,
            Name = string.IsNullOrWhiteSpace(component.name) ? null : component.name,
            Label = string.IsNullOrWhiteSpace(component.label) ? null : component.label,
            Bounds = BoundsOrNull(component),
            Neighbors = new NeighborInfo
            {
                Up = component.upNeighborID,
                Right = component.rightNeighborID,
                Down = component.downNeighborID,
                Left = component.leftNeighborID,
            },
            Text = text,
            Count = count,
            Selected = role == "tab"
                ? GameTabSemantics.MatchesPage(key, pageType)
                : ReferenceEquals(component, focused),
        };
    }

    private static (string Key, string Role) SemanticComponent(
        int nativeId,
        string source,
        int index,
        string menuType,
        string pageType,
        int? closeId
    )
    {
        string? tabKey = menuType == nameof(GameMenu)
            ? GameTabSemantics.KeyForNativeId(nativeId)
            : null;
        if (tabKey is not null)
            return (tabKey, "tab");
        if (pageType == nameof(InventoryPage)
            && nativeId >= 0 && nativeId < Game1.player.Items.Count)
            return ($"inventory.slot.{nativeId}", "inventory_slot");
        if (closeId == nativeId)
            return ("ui.close", "close");
        if (menuType == nameof(DialogueBox))
            return ($"dialogue.component.{index}", "component");
        if (menuType == "ReadyCheckDialog")
            return ($"ready.choice.{index}", "choice");
        return ($"ui.{source}.component.{index}", "component");
    }

    private static void EnrichDialogue(
        UiComponentInfo[] components,
        List<(ClickableComponent Component, string Source, int Index)> raw,
        DialogueBox dialogue
    )
    {
        Response[] responses = dialogue.responses?.ToArray() ?? Array.Empty<Response>();
        List<ClickableComponent>? responseComponents = dialogue.responseCC;
        if (responseComponents is null)
            return;
        int count = Math.Min(responses.Length, responseComponents.Count);
        for (int i = 0; i < count; i++)
        {
            int componentIndex = raw.FindIndex(x =>
                ReferenceEquals(x.Component, responseComponents[i]));
            if (componentIndex < 0 || componentIndex >= components.Length)
                continue;
            components[componentIndex] = components[componentIndex] with
            {
                Key = $"dialogue.choice.{i}",
                Role = "choice",
                Text = responses[i].responseText,
                Selected = dialogue.selectedResponse == i,
            };
        }
    }

    private static string? VisibleLabel(ClickableComponent? component)
    {
        if (component is null)
            return null;
        foreach (string? candidate in new[] { component.label, component.name })
        {
            if (!string.IsNullOrWhiteSpace(candidate))
                return candidate;
        }
        return null;
    }

    private static BoundsXY? BoundsOrNull(ClickableComponent? component)
    {
        if (component is null || component.bounds.Width <= 0 || component.bounds.Height <= 0)
            return null;
        return new BoundsXY
        {
            X = component.bounds.X,
            Y = component.bounds.Y,
            W = component.bounds.Width,
            H = component.bounds.Height,
        };
    }

    /// <summary>
    /// Potret inventaris. HARUS dipanggil dari game thread, sama seperti
    /// pemanggilnya — `player.Items` dibaca langsung dari state game.
    /// </summary>
    private static InventoryInfo CaptureInventory(Farmer player)
    {
        const int HotbarSize = 12;
        var hotbar = new List<SlotItem>(HotbarSize);
        int slotsUsed = 0;
        int itemsTotal = 0;

        IList<Item?> items = player.Items;
        for (int i = 0; i < items.Count; i++)
        {
            Item? item = items[i];
            if (item is null)
                continue;

            slotsUsed++;
            itemsTotal += item.Stack;

            if (i < HotbarSize)
            {
                hotbar.Add(new SlotItem
                {
                    Slot = i,
                    Name = item.Name ?? "",
                    Stack = item.Stack,
                    IsTool = item is Tool,
                });
            }
        }

        return new InventoryInfo
        {
            ActiveSlot = player.CurrentToolIndex,
            Hotbar = hotbar,
            SlotsUsed = slotsUsed,
            ItemsTotal = itemsTotal,
        };
    }

    /// <summary>
    /// Motor dulu, observation belakangan, SATU writer. DTO sudah imutabel.
    /// Jangan baca Game1 di sini. Inventaris snapshot-on-demand, bukan 10 Hz.
    /// ui.context G3-0 lalu paling banyak satu chunk ui.text G3-1.
    /// Latest-wins, tanpa antrean. Jangan masukkan G3 ke StatePayload.
    /// </summary>
    private static async Task<bool> SendMotorThenSceneAsync(
        BridgeClient client,
        long playerId,
        int screenId,
        StatePayload payload,
        ObservationPayload? scene,
        ObservationPayload? inventory = null,
        ObservationPayload? uiContext = null,
        ObservationPayload? uiText = null,
        ObservationPayload? knowledge = null
    )
    {
        bool motor = await client.SendAfterHelloAsync(
            seq => StateEnvelope.Create(seq, playerId, screenId, payload)
        );
        if (!motor)
            return false;
        if (scene is not null)
        {
            bool sceneOk = await client.SendAfterHelloAsync(
                seq => ObservationEnvelope.Create(seq, playerId, screenId, scene)
            );
            if (!sceneOk)
                return false;
        }
        if (inventory is not null)
        {
            bool inventoryOk = await client.SendAfterHelloAsync(
                seq => ObservationEnvelope.Create(seq, playerId, screenId, inventory)
            );
            if (!inventoryOk)
                return false;
        }
        if (uiContext is not null)
        {
            bool uiOk = await client.SendAfterHelloAsync(
                seq => ObservationEnvelope.Create(seq, playerId, screenId, uiContext)
            );
            if (!uiOk)
                return false;
        }
        if (uiText is not null && !await client.SendAfterHelloAsync(
            seq => ObservationEnvelope.Create(seq, playerId, screenId, uiText)
        )) return false;
        return knowledge is null || await client.SendAfterHelloAsync(
            seq => ObservationEnvelope.Create(seq, playerId, screenId, knowledge));
    }

    private void OnWarped(object? sender, WarpedEventArgs e)
    {
        if (!e.IsLocalPlayer)
            return;
        if (!ReferenceEquals(e.OldLocation, e.NewLocation) && NativeB1Offers.HasCodeCells(e.OldLocation))
            NativeCodeActions.Current.Reset();
        SceneSnapshotCapture.InvalidateRefillSourceCaches();
        ScreenState screen = this.screens.Value;
        screen.Scene.OnLocalWarp(e.NewLocation?.NameOrUniqueName);
        // WS3a: door / move_through completion requires the local Warped event for this run.
        this.backgroundMove?.OnLocalWarp(e.NewLocation is null ? null : SceneIdentity.Token(e.NewLocation.NameOrUniqueName));
        if (screen.Scene.SnapshotNeeded)
            screen.Deltas.Invalidate();
    }

    /// <summary>
    /// OBS-3A: tas farmer lokal berubah. Hanya menandai dirty; kiriman
    /// snapshot terkoales di tick 10 Hz yang sudah ada. Bukan polling baru.
    /// Tidak mutasi tas.
    /// </summary>
    private void OnInventoryChanged(object? sender, InventoryChangedEventArgs e)
    {
        this.OnDiscoveryInventoryChanged(sender,e);
        if (Context.IsWorldReady && e.IsLocalPlayer)
        {
            foreach (var item in e.Added)
                this.RecordOccurrence("item_received", item.QualifiedItemId,
                    new { source = "other", item_id = item.QualifiedItemId, qty = item.Stack });
            foreach (var change in e.QuantityChanged.Where(c => c.NewSize > c.OldSize))
                this.RecordOccurrence("item_received", change.Item.QualifiedItemId,
                    new { source = "other", item_id = change.Item.QualifiedItemId, qty = change.NewSize - change.OldSize });
        }
        if (!this.config.Enabled || !Context.IsWorldReady || !e.IsLocalPlayer)
            return;
        this.screens.Value.Inventory.RequestSnapshot();
    }

    /// <summary>
    /// OBS-2B1: hanya object add/remove di lokasi farmer layar ini.
    /// SMAPI memanggil event pada game thread; semua pembacaan Object selesai
    /// di callback ini sebelum DTO imutabel diserahkan ke async writer.
    /// </summary>
    private void OnObjectListChanged(object? sender, ObjectListChangedEventArgs e)
    {
        if (!this.config.Enabled || !Context.IsWorldReady || !e.IsCurrentLocation)
            return;

        ScreenState screen = this.screens.Value;
        string locationRaw = e.Location.NameOrUniqueName ?? e.Location.Name ?? "_";

        foreach (KeyValuePair<Vector2, SObject> pair in e.Removed)
        {
            string id = SceneIdentity.ObjectFactId(
                locationRaw,
                (int)pair.Key.X,
                (int)pair.Key.Y,
                pair.Value.QualifiedItemId ?? ""
            );
            if (
                screen.Deltas.Remove(id)
                == SceneDeltaAccumulateResult.OverflowRequiresSnapshot
            )
            {
                RequestOverflowSnapshot(screen);
                return;
            }
        }

        foreach (KeyValuePair<Vector2, SObject> pair in e.Added)
        {
            ScenePreparedFact fact = SceneSnapshotCapture.CaptureObject(
                locationRaw,
                pair.Key,
                pair.Value,
                this.config.UiPadMenusEnabled && this.PadPathConfigured && this.WorldInteractionBackgroundReady
            );
            if (
                screen.Deltas.Upsert(fact)
                == SceneDeltaAccumulateResult.OverflowRequiresSnapshot
            )
            {
                RequestOverflowSnapshot(screen);
                return;
            }
        }
    }

    /// <summary>
    /// OBS-2B2a: hanya membership terrain feature di lokasi farmer layar ini.
    /// Removed diproses dulu supaya pergantian type pada tile yang sama menjadi
    /// remove identity lama + upsert identity baru dalam satu patch global.
    /// </summary>
    private void OnTerrainFeatureListChanged(
        object? sender,
        TerrainFeatureListChangedEventArgs e
    )
    {
        if (!this.config.Enabled || !Context.IsWorldReady || !e.IsCurrentLocation)
            return;

        ScreenState screen = this.screens.Value;
        string locationRaw = e.Location.NameOrUniqueName ?? e.Location.Name ?? "_";

        foreach (KeyValuePair<Vector2, TerrainFeature> pair in e.Removed)
        {
            string id = SceneIdentity.TerrainFactId(
                locationRaw,
                (int)pair.Key.X,
                (int)pair.Key.Y,
                pair.Value.GetType().Name
            );
            if (
                screen.Deltas.Remove(id)
                == SceneDeltaAccumulateResult.OverflowRequiresSnapshot
            )
            {
                RequestOverflowSnapshot(screen);
                return;
            }
        }

        foreach (KeyValuePair<Vector2, TerrainFeature> pair in e.Added)
        {
            ScenePreparedFact fact = SceneSnapshotCapture.CaptureTerrain(
                locationRaw,
                pair.Key,
                pair.Value
            );
            if (
                screen.Deltas.Upsert(fact)
                == SceneDeltaAccumulateResult.OverflowRequiresSnapshot
            )
            {
                RequestOverflowSnapshot(screen);
                return;
            }
        }
    }

    /// <summary>
    /// OBS-2B2b: hanya membership NPC non-Monster di lokasi farmer layar ini.
    /// Added/Removed adalah dua diff set tanpa chronology lintas-array. Gabungkan
    /// instance terdampak, lalu baca membership final dari characters saat masih
    /// di game thread. Tidak ada polling gerak, arah hadap, atau posisi.
    /// </summary>
    private void OnNpcListChanged(object? sender, NpcListChangedEventArgs e)
    {
        if (!this.config.Enabled || !Context.IsWorldReady || !e.IsCurrentLocation)
            return;

        ScreenState screen = this.screens.Value;
        string locationRaw = e.Location.NameOrUniqueName ?? e.Location.Name ?? "_";
        HashSet<NPC> affected = SceneNpcDeltaLogic.CollectAffected(
            e.Added,
            e.Removed,
            static npc => npc is Monster
        );

        foreach (NPC npc in affected)
        {
            bool isPresent = e.Location.characters.Any(current => ReferenceEquals(current, npc));
            ScenePreparedFact? finalFact = isPresent
                ? screen.SceneCapture.CaptureNpc(e.Location, locationRaw, npc)
                : null;
            string? knownFactId = null;
            if (
                !isPresent
                && !screen.SceneCapture.TryGetNpcFactId(
                    locationRaw,
                    npc,
                    out knownFactId
                )
            )
            {
                // Added lalu Removed sebelum pernah masuk snapshot: final absent,
                // jadi tidak ada fact lama yang perlu dihapus.
                continue;
            }

            if (
                SceneNpcDeltaLogic.ApplyFinalMembership(
                    screen.Deltas,
                    finalFact,
                    knownFactId
                )
                == SceneDeltaAccumulateResult.OverflowRequiresSnapshot
            )
            {
                RequestOverflowSnapshot(screen);
                return;
            }
        }
    }

    /// <summary>
    /// FINAL Part C3 (J L958-L973): door <c>enterable</c> follows the native clock. Once per (location, day,
    /// Game1.timeOfDay) the current location's doors are re-resolved (NativeB1Offers.ResolveDoor, read-only) and only
    /// changed facts go out as an ordinary delta; a changed door identity resyncs with a full snapshot.
    /// Game thread only (state-send path); no input, no world mutation.
    /// </summary>
    private static void RefreshDoorFactsOnClock(ScreenState screen)
    {
        GameLocation? location = Game1.player?.currentLocation;
        if (location is null)
            return;
        string locationRaw = location.NameOrUniqueName ?? location.Name ?? "_";
        if (!screen.DoorClock.Due(locationRaw, Game1.dayOfMonth, Game1.timeOfDay))
            return;
        SceneDoorRefreshResult refreshed = SceneDoorRefresh.Apply(
            screen.Deltas,
            locationRaw,
            NativeB1Offers.DoorFacts(location, Game1.player!, locationRaw)
        );
        // NoBaseline: no delta can flow yet; the pending/next full snapshot resolves every door at its own capture.
        if (refreshed == SceneDoorRefreshResult.RequiresSnapshot)
            RequestOverflowSnapshot(screen);
    }

    private static void RequestOverflowSnapshot(ScreenState screen)
    {
        screen.Scene.RequestSnapshot();
        screen.Deltas.Invalidate();
    }

    private void OnReturnedToTitle(object? sender, ReturnedToTitleEventArgs e)
    {
        this.nightLedger?.Clear("reconnect"); this.occurrenceScope = null;
        this.ResetNativeProgress();
        this.renderTiming?.Restore();
        if (this.worldInteraction?.Busy == true)
        {
            this.worldInteraction.Interrupt();
            this.worldInteractionUncertain = true;
        }
        this.InterruptUseToolNativeForLifecycle("ReturnedToTitle");
        this.AbortReflexForLifecycle("ReturnedToTitle"); // WS-G4: neutral oldPadState + release; generic interrupted result below
        this.AbortReflexG5ForLifecycle("ReturnedToTitle"); // WS-G5: combat / charge release; generic interrupted result below
        this.ResetHouseTravelForTitle();
        this.eggRun = null;
        this.festivalRun = null;
        this.festivalAttempted = null;
        this.festivalSummary = null;
        this.lastEventAction = null;
        // Owned latch removed compare-before-restore; the existing interrupted result
        // below closes the inbox slot. Armed containment is not disarmed here.
        this.backgroundMove?.AbortForLifecycle("ReturnedToTitle");
        this.OnPadLifecycleExit("ReturnedToTitle"); // WS-G0: Suppress outstanding pad, restore gamepadMode next tick
        this.delayedWorldPressProbe.Cancel();
        this.worldPressProbe.Cancel();
        this.stickHoldDriver?.ForceCleanup(
            "ReturnedToTitle",
            ControllerStickHoldProbe.StickButtonName(this.stickHoldProbe.ActiveDirection ?? "")
        );
        this.menuStepDriver?.ForceCleanup(
            "ReturnedToTitle",
            ControllerStickHoldProbe.StickButtonName(this.menuStepProbe.ActiveDirection ?? "")
        );
        this.ClearStickHoldBookkeeping();

        // Perintah yang sedang jalan mati bersama sesinya. Menulis putusan
        // "interrupted" lebih baik daripada diam: tanpa itu pemanggil hanya
        // melihat timeout dan tidak bisa membedakannya dari input yang hilang.
        MenuStepCommandResult? terputus = this.commandInbox.CompleteInterrupted(
            "ReturnedToTitle", this.CurrentCommandIdentity(), NowUnixMs());
        if (terputus is not null)
            this.WriteCommandResult(terputus);

        // Session id baru: permintaan yang dibuat untuk save sebelumnya tidak
        // boleh dieksekusi di save berikutnya. Daftar dedupe ikut dikosongkan
        // -- scope-nya memang satu command session; berkas lama tetap ditolak.
        this.RotateCommandSession();
        this.screens.Value.Reset();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) this.progressHeartbeat?.Dispose();
        if (disposing) this.AbortGenericUiMotor("dispose");
        if (disposing) this.renderTiming?.Dispose();
        if (disposing) this.DisposePad(); // WS-G0: gamepadMode / gamepadControls / snappyMenus back
        base.Dispose(disposing);
    }

    private void OnUpdateTicking(object? sender, UpdateTickingEventArgs e)
    {
        TickStallEvents.Probe.Enter("ticking");
        this.receiverState.Value.Surface = null;
        this.CaptureReceiverFacts();
        this.useToolInputTick = e.Ticks;
        try { this.gamepadProbe?.OnUpdateTicking(); } catch (Exception ex) { this.Monitor.Log("GAMEPAD_PROBE ticking fault " + ex, LogLevel.Error); }
        this.BeginUseToolCleanupUpdate(e.Ticks);
        try
        {
            this.ApplyActionOwnerRecords();
            this.RequestOwnedMutationRelease();
            this.TickHostPauseBudgets();
            if (Game1.paused || Game1.HostPaused)
            {
                // Physical release and containment continue; gameplay writers /
                // route/reflex planners wait before any new Press is possible.
                this.backgroundMove?.OnUpdateTicking();
                return;
            }
            // Before SMAPI ApplyOverrides / Game1.Update: advisory checks, owned latch
            // removal and the pre-consumer frame token for this update.
            this.TickFestivalBeforeRuntime(); // FIXB61: native timed phase + lease auto-start; no model decision
            this.CheckHouseTravelBeforeRuntime();
            this.CheckEventRouteBeforeRuntime();
            try { this.commandInbox.ExecutionSlot.Tick(() => this.backgroundMove?.OnUpdateTicking()); }
            catch { this.actionOwner.Contain(NowUnixMs()); throw; }
            this.TickHouseTravel();
            this.TickEventEggRoute(); // FIXB59: one-egg verified route through the same pad writer
            this.TickFestivalAfterRuntime(); // FIXB61: ice cast approach via the same verified move path
            this.TickWorldInteraction(e.Ticks);
            this.TickUseToolNativeAdapter();
            this.TickStickHold(e);
            this.TickMenuStep(e);
        }
        catch
        {
            this.useToolAdapter.NoteCleanupBoundaryFailure();
            throw;
        }
        finally
        {
            // WS-G0: after every ARTI Press/Suppress of this tick (PLAN 1.5 a tick order).
            this.TickPadModeTicking();
            this.BeginGenericUiFrame();
        }
    }

    // ---------------------------------------------------------------------
    // P5-R3C: kandidat aksi menu_step (utama) + diagnostik hold-duration.
    //
    // menu_step: baca snap awal, tahan analog, Stop pada UpdateTicking
    // BERIKUTNYA setelah UpdateTicked melihat snap berubah. Sukses hanya
    // jika end = derived neighbor. Press terkirim bukan sukses.
    //
    // Stickhold: diagnostik durasi, bukan pusat. Satu pemilik I/O masing-
    // masing di UpdateTicking. Observasi tidak Press/Suppress.
    // Provenance: analog mapping spike 677f7c4; snap/neighbor PR #70
    // c909279; hold/release spike DITOLAK.
    // ---------------------------------------------------------------------

    private void OnProbeStickHoldCommand(string command, string[] args)
    {
        if (this.LegacyPadWriterRefused("arti_probe_stickhold"))
            return;
        if (this.RejectUnleasedMutation("arti_probe_stickhold"))
            return;
        if (!ControllerStickHoldProbe.TryParseCommand(
                args, out string direction, out int holdTicks, out int delayTicks,
                out StickHoldStatus parseError))
        {
            this.Monitor.Log($"arti_probe_stickhold ditolak: {parseError}", LogLevel.Warn);
            return;
        }

        StickHoldRuntimeContext context = this.BuildStickHoldContext();
        StickHoldStatus status = this.stickHoldProbe.TryArm(
            direction, holdTicks, delayTicks, context);
        if (status != StickHoldStatus.Started)
        {
            this.Monitor.Log($"arti_probe_stickhold ditolak: {status}", LogLevel.Warn);
            return;
        }

        this.stickHoldButton = SButton.None;
        this.stickHoldStartSnapId = null;
        this.stickHoldStartSnapName = null;
        this.stickHoldExpectedNeighborId = null;
        this.Monitor.Log(
            $"arti_probe_stickhold armed: run={this.stickHoldProbe.RunId} "
                + $"direction={direction} holdTicks={holdTicks} delayTicks={delayTicks} "
                + $"button={ControllerStickHoldProbe.StickButtonName(direction)} "
                + $"gameTicks={Game1.ticks}",
            LogLevel.Info
        );
    }

    private void OnMenuStepCommand(string command, string[] args)
    {
        if (this.LegacyPadWriterRefused("arti_menu_step"))
            return;
        if (this.RejectUnleasedMutation("arti_menu_step"))
            return;
        if (!ControllerMenuStepProbe.TryParseCommand(
                args, out string direction, out int timeoutTicks, out int delayTicks,
                out StickHoldStatus parseError))
        {
            this.Monitor.Log($"arti_menu_step ditolak: {parseError}", LogLevel.Warn);
            return;
        }

        bool haveSnap = TryGetNativeSnap(out ClickableComponent? startSnap, out _);
        int? startId = haveSnap ? startSnap!.myID : null;
        int? expected = haveSnap ? NeighborIdFor(startSnap!, direction) : null;
        if (expected is < 0)
            expected = null;

        StickHoldRuntimeContext context = this.BuildMenuStepContext();
        StickHoldStatus status = this.menuStepProbe.TryArm(
            direction, timeoutTicks, delayTicks, context, startId, expected);
        if (status != StickHoldStatus.Started)
        {
            this.Monitor.Log($"arti_menu_step ditolak: {status}", LogLevel.Warn);
            return;
        }

        this.stickHoldButton = SButton.None;
        this.stickHoldStartSnapId = startId;
        this.stickHoldStartSnapName = haveSnap ? startSnap!.name : null;
        this.stickHoldExpectedNeighborId = expected;
        this.Monitor.Log(
            $"arti_menu_step armed: run={this.menuStepProbe.RunId} direction={direction} "
                + $"timeout={timeoutTicks} delay={delayTicks} "
                + $"start_snap_id={startId?.ToString() ?? "null"} "
                + $"expected_neighbor_id={expected?.ToString() ?? "null"} "
                + $"button={ControllerStickHoldProbe.StickButtonName(direction)} "
                + $"gameTicks={Game1.ticks}",
            LogLevel.Info
        );
    }

    private StickHoldRuntimeContext BuildMenuStepContext()
    {
        IClickableMenu? menu = Game1.activeClickableMenu;
        IClickableMenu? inner = menu is GameMenu gm ? (gm.GetCurrentPage() ?? menu) : menu;
        return new StickHoldRuntimeContext(
            World: new ProbeWorldContext(
                Context.IsWorldReady,
                Context.IsMultiplayer,
                Context.IsMainPlayer,
                Context.IsSplitScreen
            ),
            Enabled: this.config.ControllerMenuStepEnabled,
            MenuOpen: menu is not null,
            ExistingSnapPresent: TryGetNativeSnap(out _, out _),
            OtherProbeBusy: this.commandInbox.InflightOp != MenuStepCommandInbox.OpMenuStep && this.MutationInflight
                || this.stickHoldProbe.IsActive
                || this.stickHoldProbe.ReleaseOwed,
            MenuType: menu?.GetType().Name,
            PageType: inner?.GetType().Name,
            ScreenId: Context.ScreenId
        );
    }

    private void TickMenuStep(UpdateTickingEventArgs e)
    {
        if (this.menuStepDriver is null)
            return;
        if (!this.menuStepProbe.IsActive && !this.menuStepProbe.ReleaseOwed)
            return;

        StickHoldRuntimeContext context = this.BuildMenuStepContext();
        string mapped = ControllerStickHoldProbe.StickButtonName(
            this.menuStepProbe.ActiveDirection ?? "");
        if (mapped.Length > 0 && Enum.TryParse(mapped, out SButton parsed) && parsed != SButton.None)
            this.stickHoldButton = parsed;

        StickHoldDriverReport report;
        try
        {
            report = this.menuStepDriver.TickTicking(e.Ticks, context, mapped);
        }
        catch (Exception ex)
        {
            this.Monitor.Log(
                $"arti_menu_step EXCEPTION {ex.GetType().Name}: {ex.Message}",
                LogLevel.Warn
            );
            this.LogMenuStepResult("EXCEPTION_CLEANUP");
            return;
        }

        if (report.InputOp == StickHoldInputOp.None
            && report.Kind is StickHoldTickingKind.Idle or StickHoldTickingKind.Waiting)
        {
            return;
        }

        this.Monitor.Log(
            string.Join(
                " ",
                "arti_menu_step TICKING",
                $"run={report.RunId}",
                $"kind={report.Kind}",
                $"input_op={report.InputOp}",
                $"phase={report.PhaseLabel}",
                $"change_seen={this.menuStepProbe.ChangeSeen}",
                $"press_count={this.menuStepProbe.PressCount}",
                $"timeout_remaining={this.menuStepProbe.RemainingTimeoutTicks}",
                $"release_owed={this.menuStepProbe.ReleaseOwed}",
                $"updateTicks={e.Ticks}",
                $"gameTicks={Game1.ticks}"
            ),
            LogLevel.Info
        );

        if (!this.menuStepProbe.IsActive && !this.menuStepProbe.ReleaseOwed)
            this.LogMenuStepResult(report.PhaseLabel);
    }

    private void TickMenuStepAfter(UpdateTickedEventArgs e)
    {
        if (this.menuStepDriver is null)
            return;
        if (!this.menuStepProbe.IsActive
            && !this.menuStepProbe.ReleaseOwed
            && !this.menuStepProbe.AwaitingEndSnap)
            return;

        int? snapId = TryGetNativeSnap(out ClickableComponent? snap, out _) ? snap.myID : null;
        this.menuStepProbe.NoteObservedSnap(snapId, this.BuildMenuStepContext());
        StickHoldObserveResult observed = this.menuStepDriver.TickTicked(e.Ticks);
        if (observed.ShouldLog)
        {
            this.Monitor.Log(
                string.Join(
                    " ",
                    "arti_menu_step OBSERVE",
                    $"run={observed.RunId}",
                    $"phase={observed.PhaseLabel}",
                    $"snap_id={(snapId?.ToString() ?? "null")}",
                    $"start_snap_id={(this.menuStepProbe.StartSnapId?.ToString() ?? "null")}",
                    $"expected_neighbor_id={(this.menuStepProbe.ExpectedNeighborId?.ToString() ?? "null")}",
                    $"change_seen={this.menuStepProbe.ChangeSeen}",
                    $"press_count={this.menuStepProbe.PressCount}",
                    $"updateTicks={e.Ticks}",
                    $"gameTicks={Game1.ticks}"
                ),
                LogLevel.Info
            );
        }

        if (!this.menuStepProbe.IsActive && !this.menuStepProbe.ReleaseOwed)
            this.LogMenuStepResult(ControllerStickHoldLabels.AfterSuppress);
    }

    private void LogMenuStepResult(string phase)
    {
        MenuStepVerdict v = this.menuStepProbe.Verdict();
        TryGetNativeSnap(out ClickableComponent? snap, out _);
        this.Monitor.Log(
            string.Join(
                " ",
                "arti_menu_step RESULT",
                $"run={v.RunId}",
                $"phase={phase}",
                $"outcome={v.Outcome}",
                $"success={v.Success}",
                $"moved={v.Moved}",
                $"neighbor_matched={v.NeighborMatched}",
                $"change_seen={v.ChangeSeen}",
                $"timeout_reached={v.TimeoutReached}",
                $"start_snap_id={(v.StartSnapId?.ToString() ?? "null")}",
                $"end_snap_id={(v.EndSnapId?.ToString() ?? "null")}",
                $"expected_neighbor_id={(v.ExpectedNeighborId?.ToString() ?? "null")}",
                $"first_changed_snap_id={(v.FirstChangedSnapId?.ToString() ?? "null")}",
                $"live_snap_id={(snap?.myID.ToString() ?? "null")}",
                $"press_count={v.PressCount}",
                $"gameIsActive={Game1.game1.IsActive}"
            ),
            v.Success ? LogLevel.Info : LogLevel.Warn
        );
        if (this.menuStepProbe.IsActive || this.menuStepProbe.ReleaseOwed)
            return;

        // Satu-satunya tempat hasil perintah ditulis, dan hanya dari putusan
        // yang SAMA dengan yang baru dicatat di log. Run konsol tidak pernah
        // menutup permintaan Python: run id-nya harus cocok.
        if (this.commandInbox.TryGetInflightRunId(out int boundRun) && boundRun == v.RunId)
        {
            this.WriteCommandResult(
                this.commandInbox.CompleteVerdict(v, this.CurrentCommandIdentity(), NowUnixMs())
            );
        }

        this.ClearStickHoldBookkeeping();
    }

    // ---------------------------------------------------------------------
    // P5-R3D -- kotak masuk perintah lokal untuk SATU menu_step.
    //
    // Pertanyaan tunggalnya: bisakah satu pemanggilan Python meminta satu
    // menu_step dan menerima hasil terstruktur, tanpa mengetik konsol?
    //
    // Yang SENGAJA tidak ada di sini:
    //   * motor kedua. Arm memakai this.menuStepProbe.TryArm yang sama persis
    //     dengan jalur konsol, dengan context yang sama, dan hasilnya diambil
    //     dari MenuStepVerdict yang sama;
    //   * antrean. Satu slot, satu berkas, satu aksi aktif;
    //   * retry. Kotak masuk tidak pernah membaca ulang permintaan yang sudah
    //     ditutup, dan tidak pernah membuat permintaan sendiri;
    //   * socket baru. Koneksi observer read-only tidak disentuh sama sekali.
    //
    // Semua IO di sini terjadi di game thread (UpdateTicked), SETIAP tick --
    // bukan 4 Hz; lihat alasannya di TickCommandInbox. Kerjanya satu berkas
    // kecil, dan hanya jika gerbangnya nyala.
    // ---------------------------------------------------------------------

    private void SetUpCommandInbox(IModHelper helper)
    {
        this.commandSessionId = Guid.NewGuid().ToString("N");
        if (!this.config.MenuStepCommandInboxEnabled)
        {
            this.Monitor.Log(
                "Kotak masuk perintah P5-R3D mati (MenuStepCommandInboxEnabled=false).",
                LogLevel.Debug
            );
            return;
        }

        string configured = this.config.CommandInboxDirectory?.Trim() ?? "";
        string dir = configured.Length == 0
            ? Path.Combine(helper.DirectoryPath, "command")
            : Path.IsPathRooted(configured)
                ? configured
                : Path.Combine(helper.DirectoryPath, configured);

        try
        {
            Directory.CreateDirectory(dir);
            this.commandInboxDir = dir;
            this.StartActionOwnerPoller(dir);
        }
        catch (Exception ex)
        {
            this.Monitor.Log(
                $"Kotak masuk perintah dimatikan; folder {dir} tidak bisa dipakai: "
                    + $"{ex.GetType().Name}: {ex.Message}",
                LogLevel.Error
            );
            return;
        }

        // Retain pending publications/claims and the last correlated receipt.
        // New-session identity checks reject old requests through the ordinary
        // result path; startup must not silently discard B while recovering A.

        this.Monitor.Log(
            $"Kotak masuk perintah P5-R3D siap di {dir}; session={this.commandSessionId}; "
                + $"menu_step_enabled={this.config.ControllerMenuStepEnabled}; "
                + $"water_once_enabled={this.config.WaterOnceEnabled}; "
                + $"world_press_enabled={this.config.WorldPressEnabled}.",
            LogLevel.Info
        );
    }

    private static long NowUnixMs() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

    MutationAdvice AdviseWaterOnce()
    {
        bool owned = this.commandInbox.HasInflight
            && string.Equals(
                this.commandInbox.InflightOp,
                MenuStepCommandInbox.OpWaterOnce,
                StringComparison.Ordinal);
        var advice = this.actionOwner.Advise(NowUnixMs(), owned);
        return owned ? advice : advice with { AllowNewMutation = false, RejectReason = "NoAdmittedAttempt" };
    }

    bool RejectUnleasedMutation(string label)
        => this.RejectUnleasedMutation(label, out _);

    bool RejectUnleasedMutation(string label, out CommandRejectReason reason)
    {
        this.ApplyActionOwnerRecords();
        // Spec §3.2: probe/staging mutations require a diagnostics build, outside this bridge.
        reason = CommandRejectReason.InboxDisabled;
        this.Monitor.Log($"{label} ditolak: {reason}", LogLevel.Warn);
        return true;
    }

    bool MutationInflight => this.commandInbox.ExecutionSlot.InputBusy;
    // ADR 181: the runtime's lease owns every attempt except the mod's own body guard (which STOP/halt still cancel).
    bool OwnedInflight => this.MutationInflight
        && this.commandInbox.ExecutionSlot.ActiveAttempt?.Client != ExecutionSlot.BodyGuardClient;
    void LogRetainedSlot(string why) => this.Monitor.Log("EXECUTION_SLOT retained " + why
        + " cleanup=" + this.commandInbox.ExecutionSlot.CleanupPending?.Invoke(), LogLevel.Warn);
    // A read-only inspect is not input: it must not break ambient learning (PM-1 a9 night 11).
    bool InputInflight => this.commandInbox.ExecutionSlot.ActiveAttempt is { ReadOnly: false }
        || this.commandInbox.ExecutionSlot.CleanupPending?.Invoke() == true;

    (bool DeleteStop, bool DeleteControl, string? StopIdentity, string? ControlIdentity)? pendingOwnerAck;

    void ApplyActionOwnerRecords()
    {
        if (this.actionOwnerPoller is null)
            return;
        // Re-arm is evaluated on every game tick, even without a new poller sequence.
        this.actionOwner.ConsumeHalt(this.actionOwnerPoller.Latest, this.CurrentCommandIdentity(), NowUnixMs(), this.MutationInflight);
        if (this.pendingOwnerAck is { } pending)
        {
            // FIXB15 review: an applied control is acknowledged only once session.json shows it (publish retried here).
            if (!this.PublishCommandSessionSafe())
                return;
            this.pendingOwnerAck = null;
            this.actionOwnerPoller.AckConsumed(pending.DeleteStop, pending.DeleteControl, pending.StopIdentity, pending.ControlIdentity);
        }

        ActionOwnerRecordHandoff snap = this.actionOwnerPoller.Latest;
        if (snap.Seq == 0 || snap.Seq == this.lastAppliedOwnerRecordSeq)
            return;

        // STOP dulu (termasuk present-tapi-invalid), lalu control.
        // Snapshot sudah di-parse off-thread; jalur tick hanya konsumsi handoff.
        RecordHandoffConsumption consumed = this.actionOwner.ConsumeRecordHandoff(
            snap,
            this.CurrentCommandIdentity(),
            NowUnixMs(),
            this.OwnedInflight);
        if (snap.StopPresent && !snap.StopValid)
        {
            this.Monitor.Log($"stop.json gagal tertutup: {snap.StopReason}", LogLevel.Warn);
        }

        if (snap.ControlValid && !consumed.ControlApplied.Accepted)
        {
            this.Monitor.Log(
                $"control.json {snap.Control!.Value.Kind} ditolak: {consumed.ControlApplied.Reason}",
                LogLevel.Warn);
        }
        else if (!snap.ControlValid && snap.ControlPresent)
        {
            this.Monitor.Log($"control.json ditolak: {snap.ControlReason}", LogLevel.Warn);
        }

        this.lastAppliedOwnerRecordSeq = snap.Seq;
        if (snap.ControlValid || consumed.StopApplied.Accepted)
        {
            // FIXB15 D-B13-6 (live V2B13ACC10b: `renew_rejected` mid-cast, no mod rejection logged): the runtime reads
            // "control.json gone + session.json control_seq unchanged" as a refusal. Inside a reflex hold this apply runs
            // from the hold tick, one tick before TickCommandInbox republishes the session, so an accepted renew looked
            // refused. Publish the new owner state BEFORE the control file is acknowledged (deleted).
            if (!this.PublishCommandSessionSafe())
            {
                // FIXB15 review: the publish failed (a reader held session.json): no ack yet; retried next tick.
                this.pendingOwnerAck = (consumed.DeleteStop, consumed.DeleteControl, snap.StopIdentity, snap.ControlIdentity);
                return;
            }
        }
        this.actionOwnerPoller.AckConsumed(
            consumed.DeleteStop,
            consumed.DeleteControl,
            snap.StopIdentity,
            snap.ControlIdentity);
    }

    void StartActionOwnerPoller(string dir)
    {
        this.StopActionOwnerPoller();
        this.actionOwnerPoller = new ActionOwnerRecordPoller(dir);
        this.actionOwner.StartHalt(dir, this.CurrentCommandIdentity(), NowUnixMs(), this.actionOwnerPoller.PollOnce());
        this.lastAppliedOwnerRecordSeq = 0;
        this.pendingOwnerAck = null;
        this.actionOwnerPoller.Start();
        this.Monitor.Log(
            $"Poller control/stop G2-0 {this.actionOwnerPoller.IntervalMs}ms di {dir}.",
            LogLevel.Debug);
    }

    void RestartActionOwnerPoller()
    {
        if (this.commandInboxDir is null)
        {
            this.StopActionOwnerPoller();
            return;
        }

        this.StartActionOwnerPoller(this.commandInboxDir);
    }

    void StopActionOwnerPoller()
    {
        this.actionOwnerPoller?.Dispose();
        this.actionOwnerPoller = null;
        this.lastAppliedOwnerRecordSeq = 0;
    }

    void RequestOwnedMutationRelease()
    {
        MutationAdvice advice = this.actionOwner.Advise(NowUnixMs(), this.OwnedInflight);
        if (!advice.RequestOwnedRelease)
            return;

        if (this.commandInbox.ExecutionSlot.ActiveAttempt?.Client == "progression" && this.actionOwner.ProgressionAllowed) return;
        // ADR 181: a runtime release / lease expiry never cancels the running body guard; STOP and a fault do.
        if (this.commandInbox.ExecutionSlot.ActiveAttempt?.Client == ExecutionSlot.BodyGuardClient
            && !this.actionOwner.StopLatched && !this.actionOwner.RecordFaulted) return;
        if (this.commandInbox.ExecutionSlot.ActiveAttempt?.Receipt is null)
            this.commandInbox.ExecutionSlot.Cancel(this.actionOwner.StopLatched || this.actionOwner.RecordFaulted ? "stop" : advice.ReleaseReason ?? CommandRejectReason.StopLatched.ToString());
        if (this.commandInbox.ExecutionSlot.ReleaseReady && this.worldInteraction?.Busy != true)
            this.actionOwner.AckOwnedRelease();
    }

    void TryInterruptInboxOp(string op, string reason)
    {
        if (!this.commandInbox.HasInflight
            || !string.Equals(this.commandInbox.InflightOp, op, StringComparison.Ordinal))
        {
            return;
        }

        MenuStepCommandResult? terputus = this.commandInbox.CompleteInterrupted(
            reason, this.CurrentCommandIdentity(), NowUnixMs());
        if (terputus is not null)
            this.WriteCommandResult(terputus);
    }

    private CommandInboxIdentity CurrentCommandIdentity()
    {
        if (Context.IsWorldReady && Game1.player is Farmer player)
        {
            return new CommandInboxIdentity(
                this.commandSessionId,
                player.UniqueMultiplayerID,
                player.Name ?? "",
                CurrentLocationId(player),
                Context.ScreenId,
                Context.IsMainPlayer,
                Context.IsSplitScreen,
                this.runtimeInstanceId,
                this.contextGenerationTracker.Current,
                Game1.uniqueIDForThisGame.ToString(CultureInfo.InvariantCulture)
            );
        }

        return new CommandInboxIdentity(
            this.commandSessionId,
            0,
            "",
            "",
            0,
            Context.IsMainPlayer,
            Context.IsSplitScreen,
            this.runtimeInstanceId,
            this.contextGenerationTracker.Current
        );
    }

    private static string CurrentLocationId(Farmer? farmer)
    {
        GameLocation? location = farmer?.currentLocation;
        if (location is null)
            return "";
        string locationRaw = location.NameOrUniqueName ?? location.Name ?? "";
        return string.IsNullOrEmpty(locationRaw) ? "" : SceneIdentity.Token(locationRaw);
    }

    /// <summary>
    /// SENGAJA tanpa cadence sendiri. Guard arsitektur OBS-2B1 menuntut
    /// UpdateTicked hanya punya SATU irama, dan itu milik observer 10 Hz.
    /// Menambah irama kedua di sini berarti polling tambahan; menyiasatinya
    /// dengan penghitung tick sendiri hanya membuat guard itu hijau karena
    /// kebetulan. Jadi jalur ini berjalan tiap tick, tetapi kerjanya satu
    /// <c>File.Exists</c> pada berkas kecil, dan hanya bila gerbangnya nyala.
    /// Berkas sesi ditulis ulang hanya kalau isinya benar-benar berubah.
    /// </summary>
    private void TickCommandInbox()
    {
        if (this.commandInboxDir is null)
            return;

        CommandInboxIdentity identity = this.RefreshCommandWorldIdentity(
            this.CurrentCommandIdentity(), Context.IsWorldReady, NowUnixMs());
        this.PublishCommandSession(identity);

        // Keep publishing identity/STOP/release state while paused; leave new
        // input requests unclaimed. Python re-observes after /resume.
        if (Game1.paused || Game1.HostPaused)
            return;

        // Satu aksi aktif. Selama slot terisi, berkas permintaan baru
        // ditinggalkan di tempatnya sampai putusan sebelumnya tertulis.
        if (this.commandInbox.ExecutionSlot.ActiveAttempt is not null)
            return;

        string requestPath = Path.Combine(
            this.commandInboxDir, MenuStepCommandInbox.RequestFileName);
        string raw;
        try
        {
            if (!CommandRequestReader.TryConsume(requestPath, out raw)) return;
        }
        catch (Exception ex)
        {
            this.Monitor.Log(
                $"Kotak masuk perintah gagal membaca {requestPath}: "
                    + $"{ex.GetType().Name}: {ex.Message}",
                LogLevel.Warn
            );
            return;
        }

        // V4 Stage 2 (2.1 section 4 / M1, 2.2 section 5 / N5) and ADR 244: window focus is NOT a
        // routing or admission term. With both default-OFF flags on, every request is served by the
        // verified path whether her window is active or not. The focus sample is kept for the log only.
        bool windowActiveAtAdmit = Game1.game1?.IsActive == true;
        bool backgroundToolUi = BgToolUiRoute.Enabled(
            this.config.BackgroundToolUiEnabled, this.config.BackgroundMovementEnabled, this.backgroundMove is not null);
        bool backgroundToolEligible = BgToolUiRoute.BackgroundEligible(backgroundToolUi,
            backgroundToolUi && this.config.UseToolInputEnabled
                && this.BackgroundToolKindNow is not null);
        bool backgroundActionEligible = BgToolUiRoute.BackgroundEligible(backgroundToolUi,
            backgroundToolUi
                && (this.CaptureBackgroundWorldInteractionTarget().Target is not null || this.GenericWorldReady
                    || this.HouseDoors().Count > 0 // FIXB76: published native house routes need no adjacent action target.
                    || this.EventActionsReady && (this.EventEggCandidates().Count > 0 || this.EventApproachReady)));

        // Kotak masuk harus nyala, plus gerbang op yang diminta. Kotak masuk
        // saja tidak pernah cukup untuk menyentuh input.
        CommandAdmission admission = this.commandInbox.Admit(
            raw,
            identity,
            this.config.MenuStepCommandInboxEnabled,
            this.config.ControllerMenuStepEnabled,
            this.config.WaterOnceEnabled,
            this.config.WorldPressEnabled,
            this.MutationInflight,
            NowUnixMs(),
            this.actionOwner,
            useToolEnabled: BgToolUiRoute.AdmitTerm(this.UseToolInputReady, backgroundToolEligible),
            selectItemEnabled: this.SelectItemInputReady,
            worldInteractionEnabled: BgToolUiRoute.AdmitTerm(this.WorldInteractionReady, backgroundActionEligible),
            containerTransferEnabled: this.AnyContainerTransferReady, // v1 chest, or the WS-G2 bin (EconomyMenusEnabled)
            containerExitEnabled: this.ContainerExitReady,
            uiKeyEnabled: this.UiKeyReady,
            padButtonEnabled: this.PadButtonReady, // WS-G0 slice 2: false unless GamepadInputEnabled + PadButtonInputEnabled
            uiPadEnabled: this.UiPadReady, // WS-G1: false unless GamepadInputEnabled + UiPadMenusEnabled (inactive farmhand)
            chatSendEnabled: this.ChatSendReady, // WS-G3 owner D5: false unless ChatSendEnabled
            reflexFishEnabled: this.ReflexFishReady, // WS-G4: false unless ReflexFishingEnabled on the pad path
            reflexCombatEnabled: this.ReflexCombatReady, // WS-G5: false unless ReflexCombatEnabled on the pad path
            reflexChargeEnabled: this.ReflexChargeReady // WS-G5: false unless ReflexChargeEnabled on the pad path
        );

        if (!admission.Accepted)
        {
            this.Monitor.Log(
                "arti_command DITOLAK "
                    + $"request_id={admission.Request.RequestId ?? "null"} "
                    + $"reason={admission.Reason} code={this.commandInbox.ExecutionSlot.RefusalCode ?? "null"} "
                    + $"detail={this.commandInbox.ExecutionSlot.RefusalDetail ?? "null"}",
                LogLevel.Warn
            );
            this.WriteCommandResult(
                this.commandInbox.ExecutionSlot.RefusalResult ?? this.commandInbox.CompleteRejected(
                    admission.Request, admission.Reason, identity, NowUnixMs())
            );
            return;
        }

        this.commandInbox.ExecutionSlot.Bracket(() => {
        // m3 / R3: while a background-opened chest menu holds its lock (or may still
        // open late), only container ops and read-only inspects proceed.
        if (this.backgroundMove?.MenuDebtDisallows(admission.Request) == true)
        {
            this.Monitor.Log(
                $"arti_command ditolak: request_id={admission.Request.RequestId} reason=BackgroundChestMenuDebt code=debt_active "
                    + $"detail=op:{admission.Request.Op}/{admission.Request.Primitive ?? "null"} debt:{this.backgroundMove.MenuDebtId ?? "null"}",
                LogLevel.Warn);
            this.WriteCommandResult(this.commandInbox.CompleteRejected(
                admission.Request, CommandRejectReason.CommandBusy, identity, NowUnixMs(),
                "BackgroundChestMenuDebt", "debt_active", this.backgroundMove.MenuDebtId));
            return;
        }

        if (string.Equals(
                admission.Request.Op, MenuStepCommandInbox.OpInspectTiles, StringComparison.Ordinal))
        {
            this.ExecuteInspectTiles(admission.Request, identity);
            return;
        }

        if (string.Equals(
                admission.Request.Op, MenuStepCommandInbox.OpInspectContext, StringComparison.Ordinal))
        {
            this.ExecuteInspectContext(admission.Request, identity);
            return;
        }

        if (string.Equals(
                admission.Request.Op, MenuStepCommandInbox.OpWaterOnce, StringComparison.Ordinal))
        {
            this.ArmWaterOnceFromCommand(admission.Request, identity);
            return;
        }

        if (MenuStepCommandInbox.IsReflexFish(admission.Request))
        {
            this.ArmReflexFishFromCommand(admission.Request); // WS-G4: pad path only, never a foreground fallback
            return;
        }

        if (MenuStepCommandInbox.IsReflexCombat(admission.Request))
        {
            this.ArmReflexCombatFromCommand(admission.Request); // WS-G5: pad path only, never a foreground fallback
            return;
        }

        if (MenuStepCommandInbox.IsReflexCharge(admission.Request))
        {
            this.ArmReflexChargeFromCommand(admission.Request); // WS-G5: pad path only, never a foreground fallback
            return;
        }

        if (MenuStepCommandInbox.IsWorldInteraction(admission.Request))
        {
            if (admission.Request.InteractionTarget?.Kind == AnimalHouseRoute.Kind)
            {
                this.ArmHouseTravel(admission.Request);
                return;
            }
            if (EventControlAction.IsRoute(admission.Request.InteractionTarget?.Kind))
            {
                this.ArmEventEggRoute(admission.Request);
                return;
            }
            // Both flags: the verified runtime only (ADR 244: whatever the focus), never a foreground fallback.
            if (BgToolUiRoute.RouteBackground(backgroundToolUi))
                this.ArmBackgroundHoldFromCommand(admission.Request, BgHoldKind.Action);
            else
                this.ArmWorldInteraction(admission.Request);
            return;
        }

        if (MenuStepCommandInbox.IsUiPad(admission.Request))
        {
            this.ExecuteUiPad(admission.Request); // WS-G1: ui_nav / ui_press, table-gated direct calls
            return;
        }

        if (MenuStepCommandInbox.IsUiKey(admission.Request))
        {
            this.ExecuteUiKey(admission.Request, !windowActiveAtAdmit);
            return;
        }

        if (MenuStepCommandInbox.IsPadButton(admission.Request))
        {
            this.ExecutePadButton(admission.Request); // WS-G0 slice 2: direct call, table-gated
            return;
        }

        if (MenuStepCommandInbox.IsChatSend(admission.Request))
        {
            this.ExecuteChatSend(admission.Request); // WS-G3 owner D5: ChatBox.textBoxEnter, gated (default OFF)
            return;
        }

        if (MenuStepCommandInbox.IsContainerExit(admission.Request))
        {
            this.ExecuteContainerExit(admission.Request);
            return;
        }

        if (MenuStepCommandInbox.IsContainerTransfer(admission.Request))
        {
            this.ExecuteContainerTransfer(admission.Request);
            return;
        }

        if (MenuStepCommandInbox.IsSelectItem(admission.Request))
        {
            this.ExecuteSelectItem(admission.Request);
            return;
        }

        if (MenuStepCommandInbox.IsUseTool(admission.Request))
        {
            // Live diagnostic (M1 attempt 9g, 2026-10-06): the routing decision is otherwise silent.
            this.Monitor.Log(
                $"arti_command route use_tool: request_id={admission.Request.RequestId} inactive={!windowActiveAtAdmit} "
                    + $"bgToolUi={backgroundToolUi} bgEligible={backgroundToolEligible} "
                    + $"runtime={this.backgroundMove is not null} "
                    + $"toolKind={this.BackgroundToolKindNow?.ToString() ?? "-"}",
                LogLevel.Info);
            // Both flags: the verified runtime only (ADR 244: whatever the focus), never a foreground fallback.
            if (BgToolUiRoute.RouteBackground(backgroundToolUi))
                this.ArmBackgroundHoldFromCommand(admission.Request, BgHoldKind.Tool);
            else
                this.ArmUseToolFromCommand(admission.Request);
            return;
        }

        if (MenuStepCommandInbox.IsMoveThrough(admission.Request))
        {
            // WS3a: background route only (the move run with an expected transition); never foreground.
            this.ArmMoveThroughFromCommand(admission.Request);
            return;
        }

        if (string.Equals(
                admission.Request.Op, MenuStepCommandInbox.OpWorldPress, StringComparison.Ordinal)
            || string.Equals(
                admission.Request.Op, MenuStepCommandInbox.OpInputOnce, StringComparison.Ordinal))
        {
            this.ArmWorldPressFromCommand(admission.Request, identity);
            return;
        }

        this.ArmMenuStepFromCommand(admission.Request, identity);
        });
    }

    /// <summary>
    /// Inspeksi ubin lokal read-only. Capture lewat adapter OBS-1 yang sudah
    /// ada (<see cref="SceneSnapshotCapture.CaptureObject"/> /
    /// <see cref="SceneSnapshotCapture.CaptureTerrain"/>), jendela Chebyshev
    /// terbatas, lokasi farmer saat ini saja. Nol input, nol mutasi, tidak
    /// menulis store delta, tidak menaikkan cap snapshot global.
    /// </summary>
    private void ExecuteInspectTiles(
        MenuStepCommandRequest request,
        CommandInboxIdentity identity
    )
    {
        Farmer? farmer = Game1.player;
        GameLocation? location = farmer?.currentLocation;
        string locationRaw = location is null
            ? ""
            : location.NameOrUniqueName ?? location.Name ?? "";
        string locationId = string.IsNullOrEmpty(locationRaw)
            ? ""
            : SceneIdentity.Token(locationRaw);

        if (farmer is null
            || location is null
            || locationId.Length == 0
            || request.ExpectedPlayerId != identity.PlayerId
            || !string.Equals(identity.SessionId, request.SessionId, StringComparison.Ordinal)
            || !string.Equals(locationId, request.ExpectedLocationId, StringComparison.Ordinal))
        {
            CommandRejectReason reason =
                farmer is null
                || request.ExpectedPlayerId != identity.PlayerId
                    ? CommandRejectReason.PlayerMismatch
                    : !string.Equals(identity.SessionId, request.SessionId, StringComparison.Ordinal)
                        ? CommandRejectReason.SessionMismatch
                        : CommandRejectReason.LocationMismatch;
            this.WriteCommandResult(
                this.commandInbox.CompleteRejected(
                    request, reason, identity, NowUnixMs())
            );
            return;
        }

        List<ScenePreparedFact> objects = new();
        List<ScenePreparedFact> terrain = new();
        try
        {
            foreach ((int x, int y) in InspectTilesQuery.EnumerateTiles(
                         request.CenterX, request.CenterY, request.Radius))
            {
                Vector2 tile = new(x, y);
                if (location.objects is not null
                    && location.objects.TryGetValue(tile, out SObject? obj)
                    && obj is not null)
                {
                    objects.Add(SceneSnapshotCapture.CaptureObject(locationRaw, tile, obj,
                        this.config.UiPadMenusEnabled && this.PadPathConfigured && this.WorldInteractionBackgroundReady));
                }

                if (location.terrainFeatures is not null
                    && location.terrainFeatures.TryGetValue(tile, out TerrainFeature? feature)
                    && feature is not null)
                {
                    terrain.Add(SceneSnapshotCapture.CaptureTerrain(locationRaw, tile, feature));
                }
            }

            this.WriteCommandResult(
                this.commandInbox.CompleteInspect(
                    objects,
                    terrain,
                    identity,
                    locationId,
                    Game1.ticks,
                    NowUnixMs())
            );
        }
        catch (Exception ex)
        {
            this.Monitor.Log(
                $"arti_command inspect_tiles gagal capture request_id={request.RequestId}: "
                    + $"{ex.GetType().Name}: {ex.Message}",
                LogLevel.Error
            );
            this.WriteCommandResult(
                this.commandInbox.CompleteInterrupted(
                    "inspect_capture_failed", identity, NowUnixMs())
                ?? this.commandInbox.CompleteRejected(
                    request, CommandRejectReason.ResultTooLarge, identity, NowUnixMs())
            );
            return;
        }

        this.Monitor.Log(
            $"arti_command inspect_tiles: request_id={request.RequestId} "
                + $"location={locationId} center=({request.CenterX},{request.CenterY}) "
                + $"radius={request.Radius} objects={objects.Count} terrain={terrain.Count} "
                + $"ticks={Game1.ticks}",
            LogLevel.Info
        );
    }

    /// <summary>
    /// Snapshot konteks native read-only. Reuse <see cref="CaptureMenu"/>,
    /// tracker generasi/token instance, dan bendera SMAPI yang sudah ada.
    /// Nol input, nol mutasi, tidak menulis store.
    /// </summary>
    private void ExecuteInspectContext(
        MenuStepCommandRequest request,
        CommandInboxIdentity identity
    )
    {
        Farmer? farmer = Game1.player;
        GameLocation? location = farmer?.currentLocation;
        string locationRaw = location is null
            ? ""
            : location.NameOrUniqueName ?? location.Name ?? "";
        string locationId = string.IsNullOrEmpty(locationRaw)
            ? ""
            : SceneIdentity.Token(locationRaw);

        if (farmer is null
            || location is null
            || locationId.Length == 0
            || request.ExpectedPlayerId != identity.PlayerId
            || request.ExpectedScreenId != identity.ScreenId
            || !string.Equals(identity.SessionId, request.SessionId, StringComparison.Ordinal)
            || !string.Equals(locationId, request.ExpectedLocationId, StringComparison.Ordinal)
            || Context.IsMainPlayer
            || Context.IsSplitScreen)
        {
            CommandRejectReason reason =
                Context.IsMainPlayer || Context.IsSplitScreen
                || farmer is null
                || request.ExpectedPlayerId != identity.PlayerId
                    ? CommandRejectReason.PlayerMismatch
                    : request.ExpectedScreenId != identity.ScreenId
                        ? CommandRejectReason.ScreenMismatch
                        : !string.Equals(identity.SessionId, request.SessionId, StringComparison.Ordinal)
                            ? CommandRejectReason.SessionMismatch
                            : CommandRejectReason.LocationMismatch;
            this.WriteCommandResult(
                this.commandInbox.CompleteRejected(
                    request, reason, identity, NowUnixMs())
            );
            return;
        }

        try
        {
            ScreenState screen = this.screens.Value;
            IClickableMenu? interactive = CurrentInteractiveMenu(Game1.activeClickableMenu);
            string? menuInstanceToken = this.menuInstanceTokenTracker.Observe(interactive);
            long contextGeneration = this.contextGenerationTracker.Observe(
                locationId, menuInstanceToken);
            MenuInfo? menu = CaptureMenu();
            long uiRevision = screen.UiRevision.Observe(menu);
            object? textSubscriber = Game1.keyboardDispatcher?.Subscriber;
            bool hasTextInput = textSubscriber is not null;
            string? textInputToken = this.textInputTokenTracker.Observe(textSubscriber);

            Dictionary<string, object> snapshot = InspectContextQuery.BuildSnapshot(
                this.runtimeInstanceId,
                contextGeneration,
                identity.ScreenId,
                uiRevision,
                InspectContextQuery.InitialViewportTransformRevision,
                locationId,
                identity.SessionId,
                identity.PlayerId,
                Context.IsPlayerFree,
                Context.CanPlayerMove,
                menu,
                menuInstanceToken,
                this.config.WorldPressEnabled,
                this.config.ControllerMenuStepEnabled,
                Context.IsWorldReady,
                hasTextInput,
                textInputToken, BgToolUiRoute.AdmitTerm(this.UseToolInputReady, this.BackgroundUseToolEligibleNow));

            this.EnrichToolbarContext(snapshot);
            this.EnrichGenericWorldContext(snapshot);
            this.EnrichContainerContext(snapshot);
            this.EnrichShippingBinContext(snapshot); // WS-G2: only with EconomyMenusEnabled and the bin menu open
            this.EnrichWorldInteractionContext(snapshot);
            this.EnrichUiKeyContext(snapshot);
            this.EnrichUiPadContext(snapshot); // WS-G1: only with UiPadMenusEnabled
            if (request.UiDecisionReceiver is { } decisionReceiver)
            {
                bool decisionBound = this.config.UiPadMenusEnabled && !this.actionOwner.StopLatched
                    && !this.actionOwner.RecordFaulted && this.uiMotor is null && this.uiPadSettle is null
                    && this.uiKeySettle is null && this.ReadUiPadFacts().ReceiverToken == decisionReceiver;
                snapshot["ui_decision_lease"] = decisionBound
                    && this.uiWatchdog.DecisionLease(this.GenericUiWatchdogBinding(decisionReceiver),
                        Environment.TickCount64, request.UiDecisionTimeoutMs);
            }
            this.EnrichEventMovementContext(snapshot); // FIXB53: native pinned event control, separate scope
            InspectContextQuery.AddMoveSegment(snapshot, this.backgroundMove is not null); // fix89D / ADR 158; ADR 244: no focus term
            InspectContextQuery.AddMovePath(snapshot, this.backgroundMove is { PadPathOn: true }); // fix92A / ADR 162; ADR 244
            RequireUiActionFacts(snapshot);
            UiContextFitLog.Note(this.Monitor, UiContextWire.Fit(snapshot)); // ADR 44: the result always fits its 64 KiB file bound

            this.WriteCommandResult(
                this.commandInbox.CompleteInspectContext(
                    snapshot,
                    identity,
                    locationId,
                    Game1.ticks,
                    NowUnixMs(),
                    this.runtimeInstanceId,
                    contextGeneration,
                    uiRevision,
                    InspectContextQuery.InitialViewportTransformRevision)
            );

            this.Monitor.Log(
                $"arti_command inspect_context: request_id={request.RequestId} "
                    + $"location={locationId} screen={identity.ScreenId} "
                    + $"generation={contextGeneration} mode={snapshot["mode"]} "
                    + $"ticks={Game1.ticks}",
                LogLevel.Info
            );
        }
        catch (Exception ex)
        {
            this.Monitor.Log(
                $"arti_command inspect_context gagal capture request_id={request.RequestId}: "
                    + $"{ex.GetType().Name}: {ex.Message}",
                LogLevel.Error
            );
            this.WriteCommandResult(
                this.commandInbox.CompleteInterrupted(
                    "inspect_capture_failed", identity, NowUnixMs())
                ?? this.commandInbox.CompleteRejected(
                    request, CommandRejectReason.ResultTooLarge, identity, NowUnixMs())
            );
        }
    }

    /// <summary>
    /// Arm lewat lifecycle menu_step yang SUDAH ADA. Urutannya sengaja
    /// menyalin <see cref="OnMenuStepCommand"/> baris per baris: kalau jalur
    /// perintah dan jalur konsol berbeda, bukti live salah satunya tidak
    /// berlaku untuk yang lain.
    /// </summary>
    private void ArmMenuStepFromCommand(
        MenuStepCommandRequest request,
        CommandInboxIdentity identity
    )
    {
        if (this.LegacyPadWriterRefused("menu_step"))
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(
                request, CommandRejectReason.ArmRejected, identity, NowUnixMs(), "LegacyPadWriterRefused"));
            return;
        }
        bool haveSnap = TryGetNativeSnap(out ClickableComponent? startSnap, out _);
        int? startId = haveSnap ? startSnap!.myID : null;
        int? expected = haveSnap ? NeighborIdFor(startSnap!, request.Direction) : null;
        if (expected is < 0)
            expected = null;

        StickHoldStatus status = this.menuStepProbe.TryArm(
            request.Direction,
            request.TimeoutTicks,
            request.DelayTicks,
            this.BuildMenuStepContext(),
            startId,
            expected
        );
        if (status != StickHoldStatus.Started)
        {
            this.Monitor.Log(
                $"arti_command arm ditolak: request_id={request.RequestId} status={status}",
                LogLevel.Warn
            );
            this.WriteCommandResult(
                this.commandInbox.CompleteRejected(
                    request,
                    CommandRejectReason.ArmRejected,
                    identity,
                    NowUnixMs(),
                    status.ToString()
                )
            );
            return;
        }

        this.stickHoldButton = SButton.None;
        this.stickHoldStartSnapId = startId;
        this.stickHoldStartSnapName = haveSnap ? startSnap!.name : null;
        this.stickHoldExpectedNeighborId = expected;
        this.commandInbox.BindRun(this.menuStepProbe.RunId);
        this.commandInbox.ExecutionSlot.AttachClient(request, this.menuStepProbe, reason => this.CancelMenuStepClient(request, reason),
            busy: () => this.menuStepProbe.IsActive || this.menuStepProbe.ReleaseOwed || this.menuStepProbe.AwaitingEndSnap);
        this.Monitor.Log(
            $"arti_command armed: request_id={request.RequestId} "
                + $"run={this.menuStepProbe.RunId} direction={request.Direction} "
                + $"timeout={request.TimeoutTicks} delay={request.DelayTicks} "
                + $"start_snap_id={startId?.ToString() ?? "null"} "
                + $"expected_neighbor_id={expected?.ToString() ?? "null"} "
                + $"gameTicks={Game1.ticks}",
            LogLevel.Info
        );
    }

    /// <summary>
    /// Arm lewat <see cref="Actions.WaterOnceController.TryStart"/> yang SUDAH
    /// ADA — jalur yang sama dengan <c>arti_water</c>. Tidak ada lifecycle
    /// watering kedua, tidak ada BeginUsingTool di sini.
    /// </summary>
    void CancelMenuStepClient(MenuStepCommandRequest request, string reason)
    {
        this.menuStepDriver?.ForceCleanup(reason, ControllerStickHoldProbe.StickButtonName(request.Direction));
        this.TryInterruptInboxOp(MenuStepCommandInbox.OpMenuStep, reason);
    }

    private void ArmWaterOnceFromCommand(
        MenuStepCommandRequest request,
        CommandInboxIdentity identity
    )
    {
        if (this.waterOnce is null)
        {
            this.WriteCommandResult(
                this.commandInbox.CompleteRejected(
                    request,
                    CommandRejectReason.ArmRejected,
                    identity,
                    NowUnixMs(),
                    "RejectedDisabled"
                )
            );
            return;
        }

        string status = this.waterOnce.TryStart(request.TileX, request.TileY);
        if (status != "Queued")
        {
            this.Monitor.Log(
                $"arti_command arm ditolak: request_id={request.RequestId} "
                    + $"op=water_once status={status}",
                LogLevel.Warn
            );
            this.WriteCommandResult(
                this.commandInbox.CompleteRejected(
                    request,
                    CommandRejectReason.ArmRejected,
                    identity,
                    NowUnixMs(),
                    status
                )
            );
            return;
        }

        this.commandInbox.BindRun(this.waterOnce.RunId);
        this.commandInbox.ExecutionSlot.AttachClient(request, this.waterOnce,
            reason => this.TryInterruptInboxOp(MenuStepCommandInbox.OpWaterOnce, reason), busy: () => this.waterOnce?.IsActive == true);
        this.Monitor.Log(
            $"arti_command armed: request_id={request.RequestId} "
                + $"op=water_once run={this.waterOnce.RunId} "
                + $"tile=({request.TileX},{request.TileY}) "
                + $"gameTicks={Game1.ticks}",
            LogLevel.Info
        );
    }

    /// <summary>
    /// Arm lewat <see cref="WorldPressProbe.TryArm"/> yang SUDAH ADA — jalur
    /// yang sama dengan <c>arti_probe_press</c>. Tidak ada state machine gerak
    /// kedua, tidak ada WASD keras, tidak ada klaim displacement.
    /// </summary>
    private void ArmWorldPressFromCommand(
        MenuStepCommandRequest request,
        CommandInboxIdentity identity
    )
    {
        if (request.EventToken is not null && !this.EventMoveBindingReady(request)) // ADR 244: no focus term
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(
                request, CommandRejectReason.BindingMismatch, identity, NowUnixMs(), "EVENT_CONTROL_CHANGED"));
            return;
        }
        if (string.Equals(request.Op, MenuStepCommandInbox.OpInputOnce, StringComparison.Ordinal))
        {
            CommandInboxIdentity current = this.CurrentCommandIdentity();
            if (!MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(
                    request,
                    current,
                    Context.IsWorldReady,
                    Context.IsPlayerFree || this.EventMoveBindingReady(request),
                    Context.CanPlayerMove || this.EventMoveBindingReady(request),
                    out CommandRejectReason currentReason))
            {
                this.Monitor.Log(
                    $"arti_command arm ditolak: request_id={request.RequestId} "
                        + $"op={request.Op} reason={currentReason}",
                    LogLevel.Warn
                );
                this.WriteCommandResult(
                    this.commandInbox.CompleteRejected(
                        request, currentReason, current, NowUnixMs())
                );
                return;
            }
        }

        // V4 stage 1: same inbox op, same lease/owner. With the default-OFF gate on, the verified
        // path takes every move request whatever the window focus (ADR 244); without it, the old probe.
        if (this.backgroundMove is not null
            && BackgroundMoveRuntime.IsBackgroundMoveRequest(request))
        {
            this.ArmBackgroundMoveFromCommand(request);
            return;
        }
        if (request.SegmentStop is not null)
        {
            // fix89D / ADR 158: the focused foreground press has no position guard; a segment hold is never
            // pressed there (fail closed before any input; the capability is not advertised while focused).
            this.WriteCommandResult(this.commandInbox.CompleteRejected(
                request, CommandRejectReason.ArmRejected, identity, NowUnixMs(), MoveSegmentGuard.ReasonInvalid + ":FOREGROUND"));
            return;
        }

        var context = new ProbeWorldContext(
            Context.IsWorldReady,
            Context.IsMultiplayer,
            Context.IsMainPlayer,
            Context.IsSplitScreen
        );
        if (!TryMapMoveButton(request.Direction, out SButton button))
        {
            this.Monitor.Log(
                $"arti_command arm ditolak: request_id={request.RequestId} "
                    + $"op={request.Op} status=RejectedUnmappedBinding",
                LogLevel.Warn
            );
            this.WriteCommandResult(
                this.commandInbox.CompleteRejected(
                    request,
                    CommandRejectReason.ArmRejected,
                    identity,
                    NowUnixMs(),
                    ProbePressStatus.RejectedUnmappedBinding.ToString()
                )
            );
            return;
        }

        ProbePressStatus status = this.worldPressProbe.TryArm(
            request.Direction,
            request.Ticks,
            context,
            () => this.Helper.Input.Press(button)
        );
        if (status != ProbePressStatus.Started)
        {
            this.Monitor.Log(
                $"arti_command arm ditolak: request_id={request.RequestId} "
                    + $"op={request.Op} status={status}",
                LogLevel.Warn
            );
            this.WriteCommandResult(
                this.commandInbox.CompleteRejected(
                    request,
                    CommandRejectReason.ArmRejected,
                    identity,
                    NowUnixMs(),
                    status.ToString()
                )
            );
            return;
        }

        this.commandInbox.BindRun(this.worldPressProbe.RunId);
        this.commandInbox.ExecutionSlot.AttachClient(request, this.worldPressProbe, reason => {
            this.worldPressProbe.Cancel();
            this.TryInterruptInboxOp(request.Op, reason);
        }, busy: () => this.worldPressProbe.IsActive);
        this.Monitor.Log(
            $"arti_command armed: request_id={request.RequestId} "
                + $"op={request.Op} run={this.worldPressProbe.RunId} "
                + $"direction={request.Direction} ticks={request.Ticks} "
                + $"button={button} gameTicks={Game1.ticks}",
            LogLevel.Info
        );
    }

    /// <summary>
    /// V4 stage-1 background arm through the existing inbox slot. A refusal is
    /// written as the existing rejected result; nothing was mutated.
    /// </summary>
    private void ArmBackgroundMoveFromCommand(MenuStepCommandRequest request)
    {
        BackgroundMoveTerminal? refused = this.backgroundMove!.TryArm(request);
        if (refused is not null)
        {
            this.WriteBackgroundTerminal(refused);
            return;
        }

        this.commandInbox.BindRun(this.backgroundMove.RunId);
        this.Monitor.Log(
            $"arti_command armed: request_id={request.RequestId} op={request.Op} "
                + $"background run={this.backgroundMove.RunId} direction={request.Direction} "
                + $"ticks={request.Ticks} gameTicks={Game1.ticks}",
            LogLevel.Info
        );
    }

    /// <summary>
    /// V4 Stage 2: inactive use_tool / world_interact through the SAME inbox slot,
    /// lease and NativeActionOwner. <see cref="BackgroundMoveRuntime"/> is the only
    /// Press/Suppress issuer; the foreground use-tool and world-interaction objects
    /// are never constructed, armed or ticked for this request (2.2 N1).
    /// </summary>
    private void ArmBackgroundHoldFromCommand(MenuStepCommandRequest request, BgHoldKind kind)
    {
        this.ApplyActionOwnerRecords();
        CommandInboxIdentity identity = this.CurrentCommandIdentity();
        BgActionTarget? actionTarget = null;
        bool targetMatches;
        if (kind == BgHoldKind.Tool)
        {
            targetMatches = request.ToolTarget is { } tool && tool == this.CurrentUseToolTarget();
        }
        else
        {
            // R4: read-only capture without the IsActive term; same identity/token checks.
            var current = this.CaptureBackgroundWorldInteractionTarget(request.InteractionTarget?.Kind);
            // WS-G0: the pad A press uses the grab tile once the cursor is hidden (G1:11317); the
            // cursor-hidden gate is enforced at admission and at every VERIFY hook entry.
            Vector2 effective = this.PadPathConfigured && !MenuStepCommandInbox.IsSeatCrossing(request) && Game1.player is Farmer padActor
                ? PadPressTile(padActor, request.InteractionTarget?.Kind) : EffectiveActionTarget(); // FIXB14 D-B12-1
            bool lease = !this.actionOwner.ReleaseRequested
                && this.actionOwner.CanAdmitLeasedInputOnce(identity, NowUnixMs(), request.RuntimeInstanceId,
                    request.OwnerId, request.Generation, request.ControlSeq, out _);
            targetMatches = request.InteractionTarget is { } target
                && WorldInteractionTarget.CanPress(target, current.Target, identity, current.Target is not null,
                    Game1.options?.SnappyMenus != false, lease, true, (int)effective.X, (int)effective.Y);
            actionTarget = current.Action;
        }
        bool eventAction = actionTarget?.EventScopeValid?.Invoke(false) == true;
        bool binding = MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request, identity,
            Context.IsWorldReady, Context.IsPlayerFree || eventAction, Context.CanPlayerMove || eventAction, out CommandRejectReason reason);
        // WS-G0 D-2 (live PRB-4, 2026-10-08): the pad A press acts on Character.GetGrabTile (bounding box +-5 px,
        // Character.cs:1169-1179); a tile-centred stand returns ARTI's own tile and A does nothing. Refuse
        // before any Press. No mod-side bump: the target capture already uses the same grab tile, and
        // re-placing the actor is the runtime's existing approach / chest-flush move (now a stick move).
        if (kind == BgHoldKind.Action && this.PadGrabTileMismatch(request) is string grabMiss)
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request, CommandRejectReason.ArmRejected, identity, NowUnixMs(),
                BgReason.PreconditionUnavailable(grabMiss)));
            return;
        }
        // WS-G0 slice 2: PadContextTable world row + PLAN 1.4 denylist (A / X), refused before any Press.
        if (this.PadDeniedForHold(request, kind) is string padDenied)
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request, CommandRejectReason.ArmRejected, identity, NowUnixMs(),
                BgReason.PreconditionUnavailable(padDenied)));
            return;
        }
        if (!binding || !targetMatches || this.backgroundMove is null)
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request,
                binding ? CommandRejectReason.BindingMismatch : reason, identity, NowUnixMs()));
            return;
        }

        BackgroundMoveTerminal? refused = this.backgroundMove.TryArmHoldTarget(request, kind, actionTarget);
        if (refused is not null)
        {
            this.WriteBackgroundTerminal(refused);
            return;
        }

        this.commandInbox.BindRun(this.backgroundMove.RunId);
        if (kind == BgHoldKind.Tool)
            this.backgroundUseToolVerdict = null; // the previous background verdict never stands in for this run
        this.Monitor.Log(
            $"arti_command armed: request_id={request.RequestId} op={request.Op} primitive={request.Primitive} "
                + $"background hold run={this.backgroundMove.RunId} kind={kind} ticks={request.Ticks} gameTicks={Game1.ticks}",
            LogLevel.Info);
    }

    private void TickBackgroundMoveAfter()
    {
        if (this.backgroundMove is null)
            return;
        this.backgroundMove.OnUpdateTicked();
        BackgroundMoveTerminal? terminal = this.backgroundMove.TakeTerminal();
        if ((Game1.paused || Game1.HostPaused) && terminal is null)
            return; // no next parent-route leg while paused; faults still report
        if (this.RouteHouseTravelTerminal(terminal)) terminal = null;
        if (this.RouteEventEggTerminal(terminal)) terminal = null;
        if (this.RouteFestivalIceTerminal(terminal)) terminal = null;
        if (!this.RouteReflexTerminal(terminal) && !this.RouteReflexG5Terminal(terminal)) // WS-G4/G5: reflex presses report to the reflex
            this.WriteBackgroundTerminal(terminal);
        this.FlushReflexOutcome();
        this.FlushReflexG5Outcome(); // WS-G5
        this.TickBodyGuard(); // ADR 179: the body protects itself at game tick, after this tick's outcomes
    }

    /// <summary>
    /// The only place a background movement result reaches the inbox. Completed
    /// requires the full V4 evidence set; containment/focus/divergence paths are
    /// interrupted (uncertain), never completed.
    /// </summary>
    private void WriteBackgroundTerminal(BackgroundMoveTerminal? terminal)
    {
        if (terminal is null)
            return;
        this.MaybeRefreshSceneForNpc("background_terminal"); // WS3b D27: fresh NPC tiles after a step
        if (!this.commandInbox.HasInflight
            || !string.Equals(
                this.commandInbox.InflightRequestId,
                terminal.Request.RequestId,
                StringComparison.Ordinal))
        {
            this.Monitor.Log(
                $"background terminal without matching inflight request: request_id={terminal.Request.RequestId} "
                    + $"kind={terminal.Kind} label={terminal.Label}",
                LogLevel.Warn
            );
            return;
        }

        CommandInboxIdentity identity = this.CurrentCommandIdentity();
        long now = NowUnixMs();
        if (terminal.Hold is { } hold && terminal.Kind != BackgroundTerminalKind.Rejected)
        {
            // Stage 2: per-primitive verdict writers on the EXISTING vocabulary; never CompleteWorldPress.
            this.WriteCommandResult(this.CompleteBackgroundHold(terminal, hold, identity, now));
            return;
        }
        if (MenuStepCommandInbox.IsMoveThrough(terminal.Request) && terminal.Kind != BackgroundTerminalKind.Rejected)
        {
            this.WriteCommandResult(this.CompleteMoveThroughTerminal(terminal, identity, now));
            return;
        }
        MenuStepCommandResult? result = terminal.Kind switch
        {
            BackgroundTerminalKind.Rejected => this.commandInbox.CompleteRejected(
                terminal.Request, CommandRejectReason.ArmRejected, identity, now, terminal.Label),
            BackgroundTerminalKind.Completed => this.commandInbox.CompleteWorldPress(
                MenuStepCommandInbox.PressCompleted, terminal.ConsumedH, terminal.Direction, identity, now, terminal.Path),
            BackgroundTerminalKind.NotCompleted => this.commandInbox.CompleteWorldPress(
                terminal.Label, terminal.ConsumedH, terminal.Direction, identity, now, terminal.Path),
            _ => this.commandInbox.CompleteInterrupted(terminal.Label, identity, now),
        };
        if (result is not null)
        {
            if (terminal.MovementReadback is { } readback)
                result = this.commandInbox.ResolveMovementReadback(terminal.Request, result, readback, identity, now);
            this.WriteCommandResult(result);
        }
    }

    /// <summary>
    /// V4 Stage 2 verdicts (2.1 section 5 / M7, 2.2 N1): use_tool -> UseToolInputVerdict
    /// via CompleteUseTool (PressCount = VERIFY-admitted callbacks with the key present;
    /// CleanupConfirmed only after verified release) and the gather last_use source;
    /// world_interact -> WorldInteractionVerdict via CompleteWorldInteraction, Opened only
    /// for the pinned chest ItemGrabMenu by reference at context generation pin+1.
    /// </summary>
    private MenuStepCommandResult CompleteBackgroundHold(BackgroundMoveTerminal terminal, BackgroundHoldResult hold,
        CommandInboxIdentity identity, long now)
    {
        BgHoldOutcome o = hold.Outcome;
        if (hold.Kind == BgHoldKind.Tool)
        {
            UseToolInputVerdict verdict = new(terminal.Request.RequestId ?? "", o.Outcome, o.PressCount, o.CleanupConfirmed,
                o.Reason ?? (o.Outcome == BgHoldCompletion.InputCompleted ? null : terminal.Label), o.CycleObserved);
            this.backgroundUseToolVerdict = verdict;
            return this.commandInbox.CompleteUseTool(verdict, identity, now);
        }

        string outcome = o.Outcome;
        bool opened = o.Opened;
        string? receiver = null;
        long? generation = null;
        BgActionTarget? pinned = this.backgroundMove?.LastHoldTarget;
        if (pinned is { Kind: BgActionKind.Generic })
        {
            this.AdvanceUiContextNative(this.screens.Value);
            return this.commandInbox.CompleteWorldInteraction(new WorldInteractionVerdict(outcome, opened,
                o.CleanupConfirmed, o.PressCount, null, null,
                EffectDetail: this.backgroundMove?.LastGenericDelta), this.CurrentCommandIdentity(), now);
        }
        if (pinned is { Kind: BgActionKind.Event or BgActionKind.EventReceiver })
        {
            this.RecordEventAction(terminal.Request, outcome, opened && o.CleanupConfirmed);
            return this.commandInbox.CompleteWorldInteraction(new WorldInteractionVerdict(outcome, opened,
                o.CleanupConfirmed, o.PressCount, null, null, EffectDetail: opened ? pinned.ItemId : null), identity, now);
        }
        if (pinned is { Kind: BgActionKind.Npc or BgActionKind.Eat or BgActionKind.ExtraReceiver })
            return this.CompleteReceiverKindHold(terminal, hold, pinned, identity, now); // WS3b npc / eat, WS-G6 question / elevator
        if (pinned is { Kind: not BgActionKind.Chest })
        {
            // WS3a door / forage: no receiver. A door's location change bumps the context generation.
            (string? resultLocation, string? detail) = InteractVerdictFields(hold, pinned);
            if (pinned.Kind == BgActionKind.Extra && opened)
                detail = this.backgroundMove?.ExtraEffectDetail(hold.Facts?.Effect); // WS-G6 "kind|item|delta|value"
            if (opened && pinned.Kind == BgActionKind.Door)
            {
                if (this.config.Enabled && Context.IsWorldReady)
                    this.AdvanceUiContextNative(this.screens.Value);
                CommandInboxIdentity moved = this.CurrentCommandIdentity();
                if (!string.Equals(moved.LocationId, resultLocation, StringComparison.Ordinal))
                {
                    outcome = BgHoldCompletion.InteractionUncertain;
                    opened = false;
                    resultLocation = null;
                }
                else
                {
                    generation = moved.ContextGeneration;
                    identity = moved;
                }
            }
            return this.commandInbox.CompleteWorldInteraction(new WorldInteractionVerdict(outcome, opened, o.CleanupConfirmed,
                o.PressCount, null, generation, opened ? resultLocation : null, opened ? detail : null), identity, now);
        }
        if (opened)
        {
            if (this.config.Enabled && Context.IsWorldReady)
                this.AdvanceUiContextNative(this.screens.Value);
            IClickableMenu? menu = Context.IsWorldReady ? Game1.activeClickableMenu : null;
            CommandInboxIdentity after = this.CurrentCommandIdentity();
            receiver = ReferenceEquals(menu, hold.Menu) ? this.ReceiverTokenOf(CurrentInteractiveMenu(menu)) : null;
            if (receiver is null || after.ContextGeneration != terminal.Request.ContextGeneration + 1)
            {
                outcome = BgHoldCompletion.InteractionUncertain;
                opened = false;
                receiver = null;
            }
            else
            {
                generation = after.ContextGeneration;
                identity = after;
            }
        }
        WorldInteractionVerdict interaction = new(outcome, opened, o.CleanupConfirmed, o.PressCount, receiver, generation);
        return this.commandInbox.CompleteWorldInteraction(interaction, identity, now);
    }

    /// <summary>
    /// Satu-satunya tempat hasil world_press ditulis untuk kotak masuk:
    /// siklus probe milik run yang diikat sudah menghabiskan N tekan.
    /// Run konsol / run asing tidak boleh menutup permintaan Python.
    /// </summary>
    private void TryCompleteInboxWorldPress(int runId, int pressCount, string? direction)
    {
        if (!this.commandInbox.TryGetInflightRunId(out int boundRun) || boundRun != runId)
            return;
        if (!string.Equals(
                this.commandInbox.InflightOp,
                MenuStepCommandInbox.OpWorldPress,
                StringComparison.Ordinal)
            && !string.Equals(
                this.commandInbox.InflightOp,
                MenuStepCommandInbox.OpInputOnce,
                StringComparison.Ordinal))
            return;

        this.WriteCommandResult(
            this.commandInbox.CompleteWorldPress(
                MenuStepCommandInbox.PressCompleted,
                pressCount,
                direction,
                this.CurrentCommandIdentity(),
                NowUnixMs())
        );
    }

    /// <summary>
    /// Satu-satunya tempat hasil water_once ditulis untuk kotak masuk:
    /// status terminal dari controller yang sudah ada. <c>ReturnedToTitle</c>
    /// dibiarkan ke <see cref="OnReturnedToTitle"/> supaya statusnya
    /// <c>interrupted</c> seperti menu_step, bukan completed.
    /// </summary>
    private void OnWaterOnceTerminal(int runId, string status)
    {
        if (!this.commandInbox.TryGetInflightRunId(out int boundRun) || boundRun != runId)
            return;
        if (!string.Equals(
                this.commandInbox.InflightOp,
                MenuStepCommandInbox.OpWaterOnce,
                StringComparison.Ordinal))
            return;
        if (string.Equals(status, "ReturnedToTitle", StringComparison.Ordinal))
            return;

        this.WriteCommandResult(
            this.commandInbox.CompleteWaterStatus(
                status, this.CurrentCommandIdentity(), NowUnixMs())
        );
    }

    bool PublishCommandSessionSafe()
    {
        try
        {
            return this.PublishCommandSession(this.CurrentCommandIdentity());
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"session publish before ack failed: {ex.GetType().Name}", LogLevel.Warn);
            return false;
        }
    }

    /// <summary>True when session.json shows the current state (unchanged or written now); false on a write failure.</summary>
    private bool PublishCommandSession(CommandInboxIdentity identity)
    {
        if (this.commandInboxDir is null)
            return true;

        this.BindCommandJournalScope(identity, Context.IsWorldReady);

        // V4 Stage 2 (M1 attempt 8): publish the same capability terms Admit uses,
        // foreground readiness OR the background-eligible term. Both background terms
        // short-circuit on BackgroundToolUiNow (ADR 244: no focus term), so with the
        // default-OFF flags off this is exactly the previous foreground-only value.
        CommandSessionFlags flags = CommandSessionFlags.Compose(
            this.UseToolInputReady, this.BackgroundUseToolEligibleNow,
            this.WorldInteractionReady, this.BackgroundWorldInteractionEligibleNow);

        BgCursorFacts? cursorFacts = Context.IsWorldReady ? BackgroundMoveRuntime.SampleCursorFacts() : null;
        string? cursorTool = BgCursorInert.Reject(cursorFacts, BgHoldKind.Tool);
        string? cursorAction = BgCursorInert.Reject(cursorFacts, BgHoldKind.Action);
        CommandInboxSession session = new()
        {
            ModTick = this.commandInbox.ExecutionSlot.ModTick,
            NativeMs = NowUnixMs(),
            AcquiredMs = this.actionOwner.IsArmed ? this.actionOwner.AcquiredUnixMs : null,
            InputIdle = this.commandInbox.ExecutionSlot.InputIdle,
            SessionId = identity.SessionId,
            PlayerId = identity.PlayerId,
            PlayerName = identity.PlayerName,
            LocationId = identity.LocationId,
            ScreenId = Context.ScreenId,
            IsMainPlayer = Context.IsMainPlayer,
            IsSplitScreen = Context.IsSplitScreen,
            InboxEnabled = this.config.MenuStepCommandInboxEnabled,
            MenuStepEnabled = this.config.ControllerMenuStepEnabled,
            WaterOnceEnabled = this.config.WaterOnceEnabled,
            WorldPressEnabled = this.config.WorldPressEnabled,
            UseToolEnabled = flags.UseToolEnabled,
            SelectItemEnabled = this.SelectItemInputReady,
            WorldInteractionEnabled = flags.WorldInteractionEnabled,
            ContainerTransferEnabled = this.ContainerTransferReady,
            ContainerExitEnabled = this.ContainerExitReady,
            UiKeyEnabled = this.UiKeyReady,
            WorldReady = Context.IsWorldReady,
            InflightRequestId = this.commandInbox.InflightRequestId,
            InputBusy = this.MutationInflight,
            CursorInert = cursorTool is null,
            CursorInertReason = cursorTool,
            CursorInertAction = cursorAction is null,
            CursorInertActionReason = cursorAction,
            CursorTile = cursorFacts is null ? null : new CursorTileView(cursorFacts.CursorTileX, cursorFacts.CursorTileY, cursorFacts.CursorVisible),
            RuntimeInstanceId = this.runtimeInstanceId,
            OwnerId = this.actionOwner.IsArmed ? this.actionOwner.OwnerId : null,
            Generation = this.actionOwner.IsArmed ? this.actionOwner.Generation : null,
            ControlSeq = this.actionOwner.IsArmed ? this.actionOwner.ControlSeq : null,
            LeaseUntilUnixMs = this.actionOwner.IsArmed ? this.actionOwner.LeaseExpiresUnixMs : null,
            StopLatched = this.actionOwner.StopLatched,
            StopGeneration = this.actionOwner.StopLatched ? this.actionOwner.StopGeneration : null,
            RecordFaulted = this.actionOwner.RecordFaulted,
            ReleaseRequested = this.actionOwner.ReleaseRequested,
            ReleaseAcked = this.actionOwner.ReleaseAcked,
            JournalUnresolvedCount = this.commandInbox.JournalUnresolvedCount,
            JournalUnresolvedLimit = this.commandInbox.JournalUnresolvedLimit,
            JournalBlockedReason = this.commandInbox.JournalBlockedReason,
            FencedGeneration = this.actionOwner.FencedGeneration,
            StatusAnswers = this.actionOwner.StatusAnswers.ToArray(),
            ClaimAnswers = this.actionOwner.ClaimAnswers.ToArray(),
            CancelAnswer = this.actionOwner.CancelAnswer,
            Halt = this.actionOwner.HaltFact,
            Rearmed = this.actionOwner.Rearmed,
            HaltNotes = this.actionOwner.HaltNotes.ToArray(),
        };
        session = this.WithPadSessionFacts(session); // WS-G0: no-op with GamepadInputEnabled off
        session = this.WithUiPadSessionFacts(session); // WS-G1: no-op with UiPadMenusEnabled off
        session = this.WithUiG3SessionFacts(session); // WS-G3: no-op with TextEventMenusEnabled / ChatSendEnabled off
        if (this.config.WorldExtrasEnabled)
            session = session with { WorldExtrasEnabled = this.ExtrasEnabled }; // WS-G6: omitted with the flag off

        // Record = kesetaraan nilai. Perbandingan ini yang membuat jalur
        // per-tick murah: serialisasi dan tulis berkas hanya terjadi kalau
        // ada yang benar-benar berubah.
        if (this.lastPublishedSession is { } previous && session with { ModTick = 0, NativeMs = 0,
                StatusAnswers = previous.StatusAnswers, ClaimAnswers = previous.ClaimAnswers, HaltNotes = previous.HaltNotes } == previous
            && session.StatusAnswers.SequenceEqual(previous.StatusAnswers) && session.ClaimAnswers.SequenceEqual(previous.ClaimAnswers)
            && session.HaltNotes.SequenceEqual(previous.HaltNotes))
            return true;

        string json = JsonSerializer.Serialize(session, ProtocolJson.Options);
        if (!this.TryWriteAtomic(MenuStepCommandInbox.SessionFileName, json))
            return false;
        this.lastPublishedSession = session with { ModTick = 0, NativeMs = 0 };
        return true;
    }

    private void WriteCommandResult(MenuStepCommandResult result) => this.commandInbox.ExecutionSlot.PublishResult(result);
    private void EmitCommandResult(MenuStepCommandResult result)
    {
        this.RetainOccurrenceTicket(result);
        this.Monitor.Log(
            "arti_command RESULT "
                + $"request_id={result.RequestId ?? "null"} status={result.Status} "
                + $"accepted={result.Accepted} success={result.Success} "
                + $"outcome={result.Outcome ?? "null"} "
                + $"reject_reason={result.RejectReason ?? "null"} "
                + $"arm_status={result.ArmStatus ?? "null"}",
            result.Success ? LogLevel.Info : LogLevel.Warn
        );
        this.TryWriteAtomic(
            MenuStepCommandInbox.ResultFileName,
            AdmissionRefusals.Serialize(result)
        );
        this.lastPublishedSession = null;
    }

    /// <summary>
    /// Tulis lewat berkas sementara lalu ganti nama. Pembaca Python tidak
    /// boleh pernah melihat JSON separuh jadi dan menyimpulkan sesuatu.
    /// </summary>
    private bool TryWriteAtomic(string fileName, string content)
    {
        if (this.commandInboxDir is null)
            return false;

        string target = Path.Combine(this.commandInboxDir, fileName);
        // D9 (live batch 2): a Python reader can hold the target during the rename. Bounded retry
        // (AtomicFileWriter: 6 tries, 20 ms); never throws into the tick. session.json is a status file
        // re-published next tick on failure (lastPublishedSession stays unset), so its failures are
        // logged at most once per minute with a count; a result.json failure is always an error.
        AtomicFileWriter.Result written = AtomicFileWriter.TryWrite(target, content);
        if (written.Ok)
            return true;
        bool status = string.Equals(fileName, MenuStepCommandInbox.SessionFileName, StringComparison.Ordinal);
        long now = Environment.TickCount64;
        if (!status || now - this.lastWriteFailureLogMs >= 60_000)
        {
            this.Monitor.Log(
                $"Kotak masuk perintah gagal menulis {target} setelah {written.Tries} percobaan: "
                    + $"{written.Error?.GetType().Name}: {written.Error?.Message} hresult=0x{written.Error?.HResult:X8}"
                    + (status ? $" (gagal sebelumnya tidak dicatat: {this.suppressedWriteFailures})" : ""),
                status ? LogLevel.Warn : LogLevel.Error
            );
            if (status)
            {
                this.lastWriteFailureLogMs = now;
                this.suppressedWriteFailures = 0;
            }
        }
        else
        {
            this.suppressedWriteFailures++;
        }
        return false;
    }

    private long lastWriteFailureLogMs = -60_000;
    private int suppressedWriteFailures;

    private void TryDelete(string path)
    {
        try
        {
            if (File.Exists(path))
                File.Delete(path);
        }
        catch (Exception ex)
        {
            this.Monitor.Log(
                $"Gagal menghapus {path}: {ex.GetType().Name}: {ex.Message}",
                LogLevel.Warn
            );
        }
    }

    private void OnProbeMenuCommand(string command, string[] args)
    {
        _ = command;
        _ = args;
        if (this.RejectUnleasedMutation("arti_probe_menu"))
            return;
        var world = new ProbeWorldContext(
            Context.IsWorldReady,
            Context.IsMultiplayer,
            Context.IsMainPlayer,
            Context.IsSplitScreen
        );
        StickHoldStatus status = ControllerMenuSetup.Evaluate(
            this.config.ControllerMenuStepEnabled || this.config.ControllerStickHoldProbeEnabled,
            world,
            menuAlreadyOpen: Game1.activeClickableMenu is not null,
            stickHoldBusy: this.MutationInflight || this.stickHoldProbe.IsActive || this.stickHoldProbe.ReleaseOwed
                || this.menuStepProbe.IsActive || this.menuStepProbe.ReleaseOwed
        );
        if (status == StickHoldStatus.MenuAlreadyOpen)
        {
            this.Monitor.Log(
                $"arti_probe_menu: menu sudah terbuka ({Game1.activeClickableMenu?.GetType().Name}); "
                    + "setup-only, bukan bukti motor membuka menu lewat input.",
                LogLevel.Info
            );
            return;
        }

        if (status != StickHoldStatus.Started)
        {
            this.Monitor.Log($"arti_probe_menu ditolak: {status}", LogLevel.Warn);
            return;
        }

        Game1.activeClickableMenu = new GameMenu();
        IClickableMenu? page = Game1.activeClickableMenu is GameMenu gm ? gm.GetCurrentPage() : null;
        this.Monitor.Log(
            $"arti_probe_menu OPENED menu={Game1.activeClickableMenu?.GetType().Name ?? "null"} "
                + $"page={page?.GetType().Name ?? "null"} "
                + $"native_snap_id={(Game1.activeClickableMenu?.currentlySnappedComponent?.myID.ToString() ?? "null")} "
                + $"gamepadControls={Game1.options.gamepadControls} "
                + "(setup-only; bukan bukti input motor dapat membuka menu)",
            LogLevel.Info
        );
    }

    private StickHoldRuntimeContext BuildStickHoldContext()
    {
        IClickableMenu? menu = Game1.activeClickableMenu;
        IClickableMenu? inner = menu is GameMenu gm ? (gm.GetCurrentPage() ?? menu) : menu;
        return new StickHoldRuntimeContext(
            World: new ProbeWorldContext(
                Context.IsWorldReady,
                Context.IsMultiplayer,
                Context.IsMainPlayer,
                Context.IsSplitScreen
            ),
            Enabled: this.config.ControllerStickHoldProbeEnabled,
            MenuOpen: menu is not null,
            ExistingSnapPresent: TryGetNativeSnap(out _, out _),
            OtherProbeBusy: this.MutationInflight
                || this.menuStepProbe.IsActive
                || this.menuStepProbe.ReleaseOwed,
            MenuType: menu?.GetType().Name,
            PageType: inner?.GetType().Name,
            ScreenId: Context.ScreenId
        );
    }

    private void TickStickHold(UpdateTickingEventArgs e)
    {
        if (this.stickHoldDriver is null)
            return;
        if (!this.stickHoldProbe.IsActive && !this.stickHoldProbe.ReleaseOwed)
            return;

        StickHoldRuntimeContext context = this.BuildStickHoldContext();
        string mapped = ControllerStickHoldProbe.StickButtonName(
            this.stickHoldProbe.ActiveDirection ?? "");
        if (mapped.Length > 0 && Enum.TryParse(mapped, out SButton parsed) && parsed != SButton.None)
            this.stickHoldButton = parsed;

        if (this.stickHoldStartSnapId is null && TryGetNativeSnap(out ClickableComponent? start, out _))
        {
            this.stickHoldStartSnapId = start.myID;
            this.stickHoldStartSnapName = start.name;
            this.stickHoldExpectedNeighborId = NeighborIdFor(
                start, this.stickHoldProbe.ActiveDirection ?? "");
        }

        StickHoldDriverReport report;
        try
        {
            if (reportNeedsBeforeLog(this.stickHoldProbe, context))
                this.LogStickHoldSnapshot("BEFORE_INPUT", -1, e.Ticks, reportPending: true);
            report = this.stickHoldDriver.TickTicking(e.Ticks, context, mapped);
        }
        catch (Exception ex)
        {
            this.Monitor.Log(
                $"arti_probe_stickhold EXCEPTION {ex.GetType().Name}: {ex.Message}",
                LogLevel.Warn
            );
            this.LogStickHoldSnapshot("EXCEPTION_CLEANUP", -1, e.Ticks, reportPending: false);
            if (!this.stickHoldProbe.IsActive && !this.stickHoldProbe.ReleaseOwed)
                this.ClearStickHoldBookkeeping();
            return;
        }

        if (report.InputOp == StickHoldInputOp.None && report.Kind == StickHoldTickingKind.Waiting)
        {
            if (this.stickHoldProbe.RemainingDelayTicks % 60 == 0)
            {
                this.Monitor.Log(
                    $"arti_probe_stickhold countdown: remainingDelay={this.stickHoldProbe.RemainingDelayTicks} "
                        + $"updateTicks={e.Ticks} gameTicks={Game1.ticks} run={report.RunId}",
                    LogLevel.Info
                );
            }
            return;
        }

        if (report.InputOp == StickHoldInputOp.None && report.Kind is StickHoldTickingKind.Idle)
            return;

        this.LogStickHoldSnapshot(report.PhaseLabel, -1, e.Ticks, reportPending: false);
        this.Monitor.Log(
            string.Join(
                " ",
                "arti_probe_stickhold TICKING",
                $"run={report.RunId}",
                $"kind={report.Kind}",
                $"input_op={report.InputOp}",
                $"phase={report.PhaseLabel}",
                $"release_intent={report.ReleaseIntent}",
                $"suppress_issued={report.SuppressIssued}",
                $"hold={this.stickHoldProbe.RequestedHoldTicks}",
                $"remaining={this.stickHoldProbe.RemainingHoldTicks}",
                $"press_count={this.stickHoldProbe.PressCount}",
                $"release_issued_count={this.stickHoldProbe.ReleaseIssuedCount}",
                $"release_owed={this.stickHoldProbe.ReleaseOwed}",
                $"button={this.stickHoldButton}",
                $"updateTicks={e.Ticks}",
                $"gameTicks={Game1.ticks}"
            ),
            LogLevel.Info
        );

        if (!this.stickHoldProbe.IsActive && !this.stickHoldProbe.ReleaseOwed)
            this.ClearStickHoldBookkeeping();

        static bool reportNeedsBeforeLog(
            ControllerStickHoldProbe probe, StickHoldRuntimeContext ctx)
        {
            _ = ctx;
            return probe.Phase is StickHoldPhase.Waiting or StickHoldPhase.Holding
                or StickHoldPhase.Releasing;
        }
    }

    private void TickStickHoldAfter(UpdateTickedEventArgs e)
    {
        if (this.stickHoldDriver is null)
            return;
        if (!this.stickHoldProbe.IsActive && !this.stickHoldProbe.ReleaseOwed)
            return;

        StickHoldObserveResult observed = this.stickHoldDriver.TickTicked(e.Ticks);
        if (!observed.ShouldLog)
        {
            if (!this.stickHoldProbe.IsActive && !this.stickHoldProbe.ReleaseOwed)
                this.ClearStickHoldBookkeeping();
            return;
        }

        this.LogStickHoldSnapshot(
            observed.PhaseLabel,
            observed.Offset,
            e.Ticks,
            reportPending: false
        );
        if (!this.stickHoldProbe.IsActive && !this.stickHoldProbe.ReleaseOwed)
        {
            this.Monitor.Log(
                $"arti_probe_stickhold selesai run={observed.RunId}; tidak ada input lanjutan.",
                LogLevel.Info
            );
            this.ClearStickHoldBookkeeping();
        }
    }

    private void ClearStickHoldBookkeeping()
    {
        this.stickHoldButton = SButton.None;
        this.stickHoldStartSnapId = null;
        this.stickHoldStartSnapName = null;
        this.stickHoldExpectedNeighborId = null;
    }

    private static bool TryGetNativeSnap(out ClickableComponent snap, out IClickableMenu? inner)
    {
        IClickableMenu? menu = Game1.activeClickableMenu;
        inner = menu is GameMenu gm ? (gm.GetCurrentPage() ?? menu) : menu;
        ClickableComponent? found =
            menu?.currentlySnappedComponent ?? inner?.currentlySnappedComponent;
        if (found is null)
        {
            snap = null!;
            return false;
        }

        snap = found;
        return true;
    }

    private static int? NeighborIdFor(ClickableComponent snap, string direction) =>
        direction switch
        {
            "down" => snap.downNeighborID,
            "right" => snap.rightNeighborID,
            "up" => snap.upNeighborID,
            "left" => snap.leftNeighborID,
            _ => null,
        };

    private void LogStickHoldSnapshot(
        string phase,
        int offset,
        uint updateTicks,
        bool reportPending
    )
    {
        _ = reportPending;
        IClickableMenu? menu = Game1.activeClickableMenu;
        IClickableMenu? inner = menu is GameMenu gm ? (gm.GetCurrentPage() ?? menu) : menu;
        ClickableComponent? snap =
            menu?.currentlySnappedComponent ?? inner?.currentlySnappedComponent;
        MenuInfo? semantic = CaptureMenu();
        Options options = Game1.options;
        GamePadState pad = Game1.input.GetGamePadState();
        GamePadState oldPad = Game1.oldPadState;
        Farmer player = Game1.player;
        InventoryInfo inventory = CaptureInventory(player);
        bool observedNeutral = Math.Abs(pad.ThumbSticks.Left.X) < 0.01f
            && Math.Abs(pad.ThumbSticks.Left.Y) < 0.01f;
        string derivedNeighbor = this.stickHoldExpectedNeighborId?.ToString() ?? "null";
        SButton button = this.stickHoldButton;

        string line = string.Join(
            " ",
            "arti_probe_stickhold SNAP",
            $"run={this.stickHoldProbe.RunId}",
            $"phase={phase}",
            $"offset={offset}",
            $"updateTicks={updateTicks}",
            $"gameTicks={Game1.ticks}",
            $"hold_ticks={this.stickHoldProbe.RequestedHoldTicks}",
            $"remaining_hold={this.stickHoldProbe.RemainingHoldTicks}",
            $"press_count={this.stickHoldProbe.PressCount}",
            $"release_issued_count={this.stickHoldProbe.ReleaseIssuedCount}",
            $"release_intent={this.stickHoldProbe.Phase == StickHoldPhase.Releasing}",
            $"suppress_issued={this.stickHoldProbe.ReleaseIssuedCount > 0}",
            $"release_owed={this.stickHoldProbe.ReleaseOwed}",
            $"observed_neutral={observedNeutral}",
            $"button={button}",
            $"direction={this.stickHoldProbe.ActiveDirection ?? "null"}",
            $"menu={menu?.GetType().Name ?? "null"}",
            $"page={inner?.GetType().Name ?? "null"}",
            $"native_snap_id={(snap?.myID.ToString() ?? "null")}",
            $"native_snap_name={(snap?.name ?? "null")}",
            $"start_snap_id={(this.stickHoldStartSnapId?.ToString() ?? "null")}",
            $"start_snap_name={(this.stickHoldStartSnapName ?? "null")}",
            $"expected_neighbor_id={derivedNeighbor}",
            $"semantic_snap_id={(semantic?.SnapId?.ToString() ?? "null")}",
            $"semantic_focus_key={(semantic?.FocusKey ?? "null")}",
            $"neighbor_up={(snap?.upNeighborID.ToString() ?? "null")}",
            $"neighbor_right={(snap?.rightNeighborID.ToString() ?? "null")}",
            $"neighbor_down={(snap?.downNeighborID.ToString() ?? "null")}",
            $"neighbor_left={(snap?.leftNeighborID.ToString() ?? "null")}",
            $"gamepadControls={options.gamepadControls}",
            $"pad_connected={pad.IsConnected}",
            $"pad_dpad=U:{pad.DPad.Up},D:{pad.DPad.Down},L:{pad.DPad.Left},R:{pad.DPad.Right}",
            $"pad_lstick={pad.ThumbSticks.Left.X.ToString("0.###", CultureInfo.InvariantCulture)},"
                + $"{pad.ThumbSticks.Left.Y.ToString("0.###", CultureInfo.InvariantCulture)}",
            $"helper_isdown={button != SButton.None && this.Helper.Input.IsDown(button)}",
            $"helper_issuppressed={button != SButton.None && this.Helper.Input.IsSuppressed(button)}",
            $"gameIsActive={Game1.game1.IsActive}",
            $"tile=({player.TilePoint.X},{player.TilePoint.Y})",
            $"pixel=({player.Position.X.ToString("0.####", CultureInfo.InvariantCulture)},"
                + $"{player.Position.Y.ToString("0.####", CultureInfo.InvariantCulture)})",
            $"facing={player.FacingDirection}",
            $"location={player.currentLocation?.Name ?? "null"}",
            $"inv_slots_used={inventory.SlotsUsed}",
            $"inv_items_total={inventory.ItemsTotal}",
            $"active_slot={inventory.ActiveSlot}"
        );
        this.Monitor.Log(line, LogLevel.Info);
    }

    private sealed class HelperStickHoldInput : IStickHoldInput
    {
        private readonly IInputHelper input;

        public HelperStickHoldInput(IInputHelper input)
        {
            this.input = input;
        }

        public void Press(string button)
        {
            if (!Enum.TryParse(button, out SButton parsed) || parsed == SButton.None)
                throw new InvalidOperationException("tombol analog tidak terpetakan");
            this.input.Press(parsed);
        }

        public void Suppress(string button)
        {
            if (!Enum.TryParse(button, out SButton parsed) || parsed == SButton.None)
                return;
            this.input.Suppress(parsed);
        }
    }

    private void OnProbePressCommand(string command, string[] args)
    {
        if (this.RejectUnleasedMutation("arti_probe_press"))
            return;
        if (this.MutationInflight || this.delayedWorldPressProbe.IsPending)
        {
            this.Monitor.Log("arti_probe_press ditolak: another input action is active or pending", LogLevel.Warn);
            return;
        }
        if (!WorldPressProbe.TryParseCommand(
                args, out string direction, out int ticks, out ProbePressStatus parseError))
        {
            this.Monitor.Log($"arti_probe_press ditolak: {parseError}", LogLevel.Warn);
            return;
        }

        var context = new ProbeWorldContext(
            Context.IsWorldReady,
            Context.IsMultiplayer,
            Context.IsMainPlayer,
            Context.IsSplitScreen
        );
        if (!TryMapMoveButton(direction, out SButton button))
        {
            this.Monitor.Log(
                "arti_probe_press ditolak: RejectedUnmappedBinding",
                LogLevel.Warn
            );
            return;
        }

        ProbePressStatus status = this.worldPressProbe.TryArm(
            direction,
            ticks,
            context,
            () => this.Helper.Input.Press(button)
        );
        this.Monitor.Log(
            status == ProbePressStatus.Started
                ? $"arti_probe_press mulai: {direction} x {ticks} tick ({button})."
                : $"arti_probe_press ditolak: {status}",
            status == ProbePressStatus.Started ? LogLevel.Info : LogLevel.Warn
        );
    }

    /// <summary>
    /// Binding pakai-alat dari opsi game: hanya entri pertama
    /// <c>Game1.options.useToolButton</c> lalu <c>ToSButton()</c>.
    /// Null/kosong/None/tak terpetakan gagal tertutup. Bukan fallback
    /// keycode keras, MouseLeft generik, atau binding gerak.
    /// </summary>
    private static bool TryMapFirstConfiguredUseToolButton(out SButton button, out string fingerprint)
    {
        button = SButton.None;
        fingerprint = "";
        Options? options = Game1.options;
        return TryMapConfiguredBinding(options?.useToolButton, allowMouseRight:false, out button, out fingerprint);
    }

    static bool TryMapConfiguredBinding(InputButton[]? raw, bool allowMouseRight, out SButton button, out string fingerprint)
    {
        button=SButton.None; fingerprint="";
        if (raw is null || raw.Length == 0)
            return false;
        if (!UseToolHoldBackend.TryMapFirstUseToolButton(raw, out InputButton first))
            return false;
        if (!IsCoherentConfiguredUseToolBinding(first, allowMouseRight))
            return false;

        button = first.ToSButton();
        if (button == SButton.None)
            return false;
        if (!UseToolNativeAdapterState.TryResolveMappedButton(button.ToString(), out _))
            return false;

        fingerprint = FingerprintConfiguredUseTool(raw);
        return true;
    }

    static bool IsCoherentConfiguredUseToolBinding(InputButton first, bool allowMouseRight=false)
    {
        if (first.mouseLeft && first.mouseRight)
            return false;
        if ((first.mouseLeft || first.mouseRight) && first.key != Keys.None)
            return false;
        if (first.mouseRight)
            return allowMouseRight;
        if (first.mouseLeft)
            return first.key == Keys.None;
        return first.key != Keys.None && Enum.IsDefined(typeof(Keys), first.key);
    }

    static string FingerprintConfiguredUseTool(InputButton[] raw)
    {
        var parts = new string[raw.Length];
        for (int i = 0; i < raw.Length; i++)
        {
            InputButton item = raw[i];
            parts[i] = ((int)item.key).ToString(CultureInfo.InvariantCulture)
                + ":" + (item.mouseLeft ? "L" : "-")
                + ":" + (item.mouseRight ? "R" : "-");
        }

        return raw.Length.ToString(CultureInfo.InvariantCulture) + "|" + string.Join("|", parts);
    }

    /// <summary>The tile key under which this exact chest instance is placed in the location, or null.
    /// Live M1 attempt 10z44 (2026-10-07): a farmhand-side chest's TileLocation did not match its object key
    /// (the game itself repairs a (0,0) TileLocation in Chest.HandleChestHit), so a TileLocation lookup reported
    /// container_identity_unavailable for a really opened chest. Identity is by reference, as before.</summary>
    static Vector2? PlacedTileOf(GameLocation? location, StardewValley.Objects.Chest chest)
    {
        if (location is null) return null;
        if (location.objects.TryGetValue(chest.TileLocation, out var direct) && ReferenceEquals(direct, chest))
            return chest.TileLocation;
        foreach (var pair in location.objects.Pairs)
            if (ReferenceEquals(pair.Value, chest)) return pair.Key;
        return null;
    }

    void EnrichContainerContext(Dictionary<string, object> snapshot)
    {
        if (this.backgroundMove?.ChestMenuInputFence(Context.IsWorldReady ? Game1.activeClickableMenu : null) is { } fence)
            snapshot["input_fence"] = fence;
        // Read only the actually opened receiver. Never call Chest.GetItemsForPlayer or construct a menu.
        if (Context.IsMainPlayer || Context.IsSplitScreen || !Context.IsWorldReady
            || Game1.activeClickableMenu is not ItemGrabMenu menu
            || menu.source != ItemGrabMenu.source_chest || menu.context is not StardewValley.Objects.Chest chest
            || !ReferenceEquals(menu.sourceItem, chest))
        {
            this.containerInstanceTokenTracker.Observe(null);
            return;
        }
        try
        {
            var location = Game1.player.currentLocation;
            Vector2? placedTile = location is null ? null : PlacedTileOf(location, chest);
            string? miss = location is null ? "no_location" : placedTile is null ? "chest_not_placed_here"
                : menu.inventory is null ? "no_actor_inventory" : menu.ItemsToGrabMenu is null ? "no_container_inventory"
                : !ReferenceEquals(menu.inventory.actualInventory, Game1.player.Items) ? "actor_inventory_not_player"
                : snapshot.GetValueOrDefault("interactive") is not Dictionary<string, object> it0
                    || it0.GetValueOrDefault("receiver_token") is not string ? "no_receiver_token" : null;
            if (miss is not null
                || snapshot.GetValueOrDefault("interactive") is not Dictionary<string, object> interactive
                || interactive.GetValueOrDefault("receiver_token") is not string receiver)
            {
                // Constant per-term reason (no coordinates): diagnostic for live M1 attempts 10z44/10z46.
                // Stardew 1.6.15 always builds the actor grid with playerInventory:false (MenuWithInventory.cs:69),
                // so identity is the player's own Items list, not that flag.
                snapshot["container"] = ContainerUiSnapshot.Unavailable("container_identity_unavailable:" + (miss ?? "unknown"));
                if (miss != this.lastLoggedContainerMiss)
                {
                    this.lastLoggedContainerMiss = miss;
                    int chests = 0; string atTile = "none";
                    if (location is not null)
                    {
                        foreach (var pair in location.objects.Pairs) if (pair.Value is StardewValley.Objects.Chest) chests++;
                        if (location.objects.TryGetValue(chest.TileLocation, out var o)) atTile = o.GetType().Name + "/" + o.QualifiedItemId + "/same=" + ReferenceEquals(o, chest);
                    }
                    this.Monitor.Log($"container identity miss={miss} loc={location?.NameOrUniqueName} chestTile={chest.TileLocation} atTile={atTile} chestsHere={chests} chestLoc={chest.Location?.NameOrUniqueName}", LogLevel.Debug);
                }
                return;
            }
            this.lastLoggedContainerMiss = null;
            string container = this.containerInstanceTokenTracker.Observe(chest)!;
            string source = SceneIdentity.ObjectFactId(location.NameOrUniqueName ?? location.Name,
                (int)placedTile!.Value.X, (int)placedTile.Value.Y, chest.QualifiedItemId);
            snapshot["container"] = ContainerUiSnapshot.Build(receiver, container, source,
                CapturePresentedSlots(menu.inventory, ContainerUiSnapshot.MaxActorSlots),
                CapturePresentedSlots(menu.ItemsToGrabMenu, ContainerUiSnapshot.MaxContainerSlots),
                PresentedItemOf(menu.heldItem), menu.readyToClose(), this.CaptureContainerClose(menu, chest, out string? closeWhy),
                closeWhy, Game1.player.MaxItems); // PM-1 fix10d: backpack slots beyond MaxItems are locked
            if(snapshot.GetValueOrDefault("capabilities") is Dictionary<string,object> caps) {
                this.LogContainerTransferMiss(menu);
                caps[ContainerTransferTarget.Primitive]=new Dictionary<string,object> {
                    ["implemented"]=true,["available"]=this.ContainerTransferReady && menu.currentlySnappedComponent is null && menu.heldItem is null,
                    ["single_item"]=this.SingleItemTransferReady(menu),
                    ["reason"]="current_quantity_requires_owner_and_capacity_revalidation" };
                caps[ContainerExitTarget.Primitive]=new Dictionary<string,object> {
                    ["implemented"]=true,
                    ["available"]=this.ContainerExitReady && snapshot["container"] is Dictionary<string,object> current
                        && current.ContainsKey("close_target"),
                    ["reason"]="ordinary_close_requires_current_target_owner_and_no_loss_revalidation" };
            }
            // PM-1 fix8: the chest's exit / transfer affordances, from the same predicates its arms use.
            if(snapshot["container"] is Dictionary<string,object> published && published.GetValueOrDefault("available") is true)
                published["affordances"]=this.ContainerAffordances(snapshot).ToArray();
        }
        catch (Exception)
        {
            snapshot["container"] = ContainerUiSnapshot.Unavailable("current_receiver_read_failed");
        }
    }

    string? lastLoggedTransferMiss;

    /// <summary>
    /// D-B2-7 (live V2B2G2take-a): the capability's wire reason is a constant, so an open chest with container_transfer
    /// unavailable gave no clue why. SMAPI log only (the ui.context wire stays byte-identical): the first failing term,
    /// once per change. Live cause: the chest was opened by the operator's key press (staging), not by ARTI's background
    /// world_interact, so the actor term (inactive window needs ARTI's pinned chest) is false. The actor term is derived
    /// by elimination from ContainerTransferReady (its use sites stay exactly the guarded four).
    /// </summary>
    void LogContainerTransferMiss(ItemGrabMenu menu)
    {
        string? why = !this.config.Enabled || !this.config.ContainerTransferInputEnabled ? "backend_disabled"
            : !this.transferObserverReady ? "observer_unavailable"
            : this.transferDoubt.EffectDoubt ? "transfer_uncertain_latched"
            : typeof(Game1).Module.ModuleVersionId != new Guid("aa1f513a-94f0-4ef6-a35f-2d5b4a3721bf") ? "assembly_unpinned"
            : Game1.options?.SnappyMenus != false ? "snappy_menus"
            : Game1.keyboardDispatcher?.Subscriber is not null ? "text_input"
            : !this.ContainerTransferReady ? (!this.config.BackgroundToolUiEnabled ? "actor_ineligible"
                : "chest_not_opened_by_arti_background_world_interact")
            : menu.currentlySnappedComponent is not null ? "snapped_component"
            : menu.heldItem is not null ? "cursor_item" : null;
        if (why == this.lastLoggedTransferMiss)
            return;
        this.lastLoggedTransferMiss = why;
        if (why is not null)
            this.Monitor.Log($"container_transfer unavailable: {why}", LogLevel.Debug);
    }

    PresentedCloseControl? CaptureContainerClose(ItemGrabMenu menu, StardewValley.Objects.Chest chest) =>
        this.CaptureContainerClose(menu, chest, out _);

    PresentedCloseControl? CaptureContainerClose(ItemGrabMenu menu, StardewValley.Objects.Chest chest, out string? reason)
    {
        // Exact installed ordinary close handler, not generic confirm or the legacy close_id.
        // Live B1W1b (2026-10-07): a VISIBLE colour picker was a blanket refusal and left M1's chest open; it is now
        // an "other control" in the OK overlap check (ContainerCloseGate). The pending-colour term stays a refusal.
        var picker = menu.chestColorPicker;
        reason = ContainerCloseGate.FirstRefusal(new (bool, string)[] {
            (typeof(Game1).Module.ModuleVersionId!=new Guid("aa1f513a-94f0-4ef6-a35f-2d5b4a3721bf"), "assembly_unpinned"),
            (menu.GetType()!=typeof(ItemGrabMenu) || chest.GetType()!=typeof(StardewValley.Objects.Chest), "not_exact_types"),
            (chest.QualifiedItemId is not ("(BC)130" or "(BC)232"), "not_ordinary_chest"),
            (menu.source!=ItemGrabMenu.source_chest || !ReferenceEquals(menu.context,chest)
                || !ReferenceEquals(menu.sourceItem,chest), "not_pinned_chest"),
            (menu.heldItem is not null, "cursor_item"),
            (!menu.readyToClose(), "not_ready_to_close"),
            (menu.shippingBin || menu.destroyItemOnClick || menu.superEssential, "special_menu"),
            (menu.exitFunction is not null || menu.behaviorBeforeCleanup is not null, "exit_hooks"),
            (menu.GetParentMenu() is not null || menu.GetChildMenu() is not null || menu.HasDependencies(), "menu_stack"),
            (Game1.currentLocation?.currentEvent is not null, "event_active"),
            (Game1.options?.SnappyMenus!=false, "snappy_menus"),
            (menu.currentlySnappedComponent is not null, "snapped_component"),
            (Game1.keyboardDispatcher?.Subscriber is not null, "text_input"),
            (picker is not null && chest.playerChoiceColor.Value!=DiscreteColorPicker.getColorFromSelection(picker.colorSelection),
                "color_pending"),
            (menu.okButton is not { visible:true }, "ok_not_visible"),
        });
        if (reason is not null || menu.okButton is not { visible:true } close) return null;
        var other = menu.inventory.inventory.Concat(menu.ItemsToGrabMenu.inventory).ToList();
        foreach(var button in new ClickableComponent?[]{menu.trashCan,menu.organizeButton,menu.fillStacksButton,
            menu.colorPickerToggleButton,menu.specialButton,menu.junimoNoteIcon,menu.lastShippedHolder,
            menu.upperRightCloseButton})
            if(button is not null && !ReferenceEquals(button,close))other.Add(button);
        var b=close.bounds;
        var bounds = ContainerCloseGate.WithColorPicker(
            other.Select(c=>new[]{c.bounds.X,c.bounds.Y,c.bounds.Width,c.bounds.Height}).ToArray(),
            picker is not null, picker?.visible==true,
            picker is null ? Array.Empty<int>() : new[]{picker.xPositionOnScreen,picker.yPositionOnScreen,picker.width,picker.height});
        var geometry=new PresentedCloseControl(b.X,b.Y,b.Width,b.Height,Game1.uiViewport.Width,Game1.uiViewport.Height,
            close.visible,close.containsPoint(b.Center.X,b.Center.Y),bounds);
        if (!geometry.IsValid) reason = geometry.Overlaps ? "ok_overlaps_other_control" : "close_geometry_invalid";
        return geometry.IsValid ? geometry : null;
    }

    static PresentedItem? PresentedItemOf(Item? item) => item is null ? null
        : new(item.QualifiedItemId, item.Stack, item.Quality, item.maximumStackSize());

    static IReadOnlyList<PresentedInventorySlot> CapturePresentedSlots(InventoryMenu inventory, int max)
    {
        if (inventory.actualInventory.Count > max || inventory.inventory.Count > max)
            return Array.Empty<PresentedInventorySlot>();
        var slots = new List<PresentedInventorySlot>();
        foreach (ClickableComponent button in inventory.inventory)
        {
            if (!int.TryParse(button.name, out int slot) || slot < 0 || slot >= max)
                return Array.Empty<PresentedInventorySlot>();
            var bounds = button.bounds;
            slots.Add(new(slot, bounds.X, bounds.Y, bounds.Width, bounds.Height,
                slot < inventory.actualInventory.Count ? PresentedItemOf(inventory.actualInventory[slot]) : null));
        }
        // A partial presentation is not a complete inventory oracle.
        if (Enumerable.Range(0, inventory.actualInventory.Count).Any(i => !slots.Any(s => s.Slot == i)))
            return Array.Empty<PresentedInventorySlot>();
        return slots;
    }

    /// <summary>
    /// V4 Stage 2 (2.1 section 4 / M4, m7): the UseToolCleanupEligible conjuncts with
    /// ONLY the IsActive term replaced. ADR 244: with the background tool UI on, the anchor is
    /// the background-ACTION pinned chest menu whatever the window focus (her chest is hers in
    /// front too, and a chest the owner opened in her window is never eligible); with it off,
    /// the old foreground term. Used at exactly four sites: ContainerExitReady, container_exit
    /// SameBinding, ContainerTransferReady, container_transfer SameActor. No Helper.Input press
    /// path uses it; UseToolCleanupEligible itself is unchanged.
    /// </summary>
    bool ContainerActorEligible => this.useToolCleanupObserverReady
        && Context.IsWorldReady && Game1.game1 is not null
        && (this.config.BackgroundToolUiEnabled
            ? this.backgroundMove?.PinnedChestMenuEligible(Game1.activeClickableMenu) == true
            : Game1.game1.IsActive)
        && !Context.IsMainPlayer && !Context.IsSplitScreen && Game1.player is not null;

    bool ContainerExitReady => this.config.Enabled && this.config.ContainerExitInputEnabled
        && this.transferObserverReady && !TransferHeldEvidence.Pending(this.transferDoubt, NowUnixMs()) && this.ContainerActorEligible
        && typeof(Game1).Module.ModuleVersionId==new Guid("aa1f513a-94f0-4ef6-a35f-2d5b4a3721bf")
        && Game1.options?.SnappyMenus==false && Game1.keyboardDispatcher?.Subscriber is null;

    void ExecuteContainerExit(MenuStepCommandRequest request)
    {
        this.ApplyActionOwnerRecords();
        var pin=this.CurrentCommandIdentity();var game=Game1.game1;
        var day=(Game1.year,Game1.currentSeason,Game1.dayOfMonth);
        if(!this.ContainerExitReady || request.ExitTarget is not { } target
            || Game1.activeClickableMenu is not ItemGrabMenu original
            || original.context is not StardewValley.Objects.Chest chest
            || this.CaptureContainerClose(original,chest) is null
            || !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request,pin,Context.IsWorldReady,
                Context.IsPlayerFree,Context.CanPlayerMove,out _)
            || !this.actionOwner.CanAdmitLeasedInputOnce(pin,NowUnixMs(),request.RuntimeInstanceId,
                request.OwnerId,request.Generation,request.ControlSeq,out _)) {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request,CommandRejectReason.BindingMismatch,pin,NowUnixMs()));
            return;
        }
        bool SameBinding() { var now=this.CurrentCommandIdentity();return ReferenceEquals(game,Game1.game1)
            && this.ContainerActorEligible && now==pin with {ContextGeneration=now.ContextGeneration}; }
        bool SameActor()=>SameBinding() && day==(Game1.year,Game1.currentSeason,Game1.dayOfMonth)
            && !this.actionOwner.ReleaseRequested && !this.actionOwner.StopLatched;
        bool SameChest()=>PlacedTileOf(Game1.player.currentLocation,chest) is not null;
        ContainerExitReadback? Read() {
            this.ApplyActionOwnerRecords();
            if(!SameActor() || !SameChest())return null;
            string? actor=ContainerExitReadback.Contents(Game1.player.Items.Select(PresentedItemOf),48);
            string? stored=ContainerExitReadback.Contents(original.ItemsToGrabMenu.actualInventory.Select(PresentedItemOf),72);
            if(actor is null || stored is null)return null;
            this.AdvanceUiContextNative(this.screens.Value);
            return new(this.ReceiverTokenOf(Game1.activeClickableMenu),
                this.CurrentCommandIdentity().ContextGeneration,actor,stored,Game1.player.CurrentToolIndex,
                original.heldItem is null,Game1.activeClickableMenu is null && Context.IsPlayerFree && Context.CanPlayerMove);
        }
        ContainerExitTarget? current;ContainerExitReadback? before;
        try { current=this.ArmableExit(original,chest,this.CaptureTransferFacts(original));before=Read(); }
        catch { current=null;before=null; }
        if(current!=target || before is null || before.ReceiverToken!=target.ReceiverToken) {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request,CommandRejectReason.BindingMismatch,pin,NowUnixMs()));return;
        }
        var input=new ContainerExitInput();var point=original.okButton.bounds.Center;
        IClickableMenu? previous=original;
        this.transferTransition=(next,ok)=> {
            string? oldToken=this.ReceiverTokenOf(previous);
            string? nextToken=this.ReceiverTokenOf(next);
            input.ObserveReceiverTransition(oldToken,nextToken,SameActor()&&SameChest(),ok);previous=next;
        };
        ContainerExitVerdict verdict;
        try { verdict=input.Execute(target,current,before,true,
            ()=> { this.ApplyActionOwnerRecords();
                if(!SameActor() || !SameChest() || !ReferenceEquals(Game1.activeClickableMenu,original)
                    || this.CaptureContainerClose(original,chest) is null
                    || ContainerExitTarget.Read(this.CaptureTransferFacts(original))!=target)
                    throw new InvalidOperationException("original close target lost");
                original.receiveLeftClick(point.X,point.Y); },
            ()=> { if(!SameBinding())throw new InvalidOperationException("original actor binding lost");
                original.releaseLeftClick(point.X,point.Y); },Read);
        } finally {this.transferTransition=null;}
        this.transferDoubt.Note(verdict.ClickCount>0&&!verdict.Exited, verdict.CleanupConfirmed);
        this.WriteCommandResult(this.commandInbox.CompleteExit(verdict,this.CurrentCommandIdentity(),NowUnixMs()));
    }

    /// <summary>PM-1 fix8: the container_exit arm terms; the published exit affordance is this same predicate.</summary>
    ContainerExitTarget? ArmableExit(ItemGrabMenu menu,StardewValley.Objects.Chest chest,JsonElement facts) =>
        this.ContainerExitReady && menu.heldItem is null && this.CaptureContainerClose(menu,chest) is not null
            ? ContainerExitTarget.Read(facts) : null;

    /// <summary>PM-1 fix8: the open chest's exit and transfers, each admitted by its own arm predicate and the chest debt.</summary>
    IEnumerable<Dictionary<string,object>> ContainerAffordances(Dictionary<string,object> snapshot)
    {
        var list=new List<Dictionary<string,object>>();
        if(!Context.IsWorldReady || Game1.activeClickableMenu is not ItemGrabMenu menu || menu.context is not StardewValley.Objects.Chest chest
            || menu.source!=ItemGrabMenu.source_chest || snapshot.GetValueOrDefault("container") is not Dictionary<string,object> box
            || box.GetValueOrDefault("available") is not true)return list;
        var facts=JsonSerializer.SerializeToElement(box,ProtocolJson.Options);
        if(this.backgroundMove?.MenuDebtRefusal(ContainerExitTarget.Primitive) is null && this.ArmableExit(menu,chest,facts) is not null)
            list.Add(new(){["verb"]="choose",["control"]="close",["role"]="exit",["primitive"]=ContainerExitTarget.Primitive});
        if(this.backgroundMove?.MenuDebtRefusal(ContainerTransferTarget.Primitive) is null && menu.currentlySnappedComponent is null
            && menu.heldItem is null && chest.GetType()==typeof(StardewValley.Objects.Chest) // the arms' chest kinds, read from
            && box.GetValueOrDefault("source_fact_id") is string fact && (fact.EndsWith(":(BC)130") || fact.EndsWith(":(BC)232")))
            list.AddRange(ContainerTransferAdmission.Publish(facts,this.CurrentCommandIdentity().ContextGeneration,this.ContainerTransferReady,
                this.config.EconomyMenusEnabled,this.SingleItemTransferReady(menu),slot=>slot<Game1.player.Items.Count
                    && Game1.player.Items[slot] is { } item ? ItemPolicy.RefuseStore(EconItemOf(item,1f)) : "empty_slot"));
        return list;
    }

    bool ContainerTransferReady => this.config.Enabled && this.config.ContainerTransferInputEnabled
        && this.transferObserverReady && !this.transferDoubt.EffectDoubt && !TransferHeldEvidence.Pending(this.transferDoubt, NowUnixMs()) && this.ContainerActorEligible
        && typeof(Game1).Module.ModuleVersionId==new Guid("aa1f513a-94f0-4ef6-a35f-2d5b4a3721bf")
        && Game1.options?.SnappyMenus==false && Game1.keyboardDispatcher?.Subscriber is null;

    // Installed InventoryMenu.rightClick uses oldKBState LeftShift (Keys=160)
    // to take half a stack. Never spoof that state; reject it before a single tap.
    bool SingleItemTransferReady(ItemGrabMenu menu) => this.ContainerTransferReady
        && menu.allowRightClick && !Game1.oldKBState.IsKeyDown(Keys.LeftShift)
        && !this.Helper.Input.IsDown(SButton.LeftShift);

    JsonElement CaptureTransferFacts(ItemGrabMenu menu)
    {
        var snapshot=new Dictionary<string,object> { ["interactive"]=new Dictionary<string,object> {
            ["receiver_token"]=this.ReceiverTokenOf(menu)! } };
        this.EnrichContainerContext(snapshot);
        return JsonSerializer.SerializeToElement(snapshot.GetValueOrDefault("container"),ProtocolJson.Options);
    }

    void ExecuteContainerTransfer(MenuStepCommandRequest request)
    {
        if (request.TransferTarget is { Mode: not null })
        {
            this.ExecuteContainerTransferMode(request); // WS-G2 quick / take_out / ship_one / ship_stack
            return;
        }
        this.ApplyActionOwnerRecords();
        var pin=this.CurrentCommandIdentity();
        var game=Game1.game1;
        if(!this.ContainerTransferReady || request.TransferTarget is not { } target
            || Game1.activeClickableMenu is not ItemGrabMenu original
            || original.context is not StardewValley.Objects.Chest chest
            || chest.GetType()!=typeof(StardewValley.Objects.Chest) || chest.QualifiedItemId is not ("(BC)130" or "(BC)232")
            || original.source!=ItemGrabMenu.source_chest || !ReferenceEquals(original.sourceItem,chest)
            || original.currentlySnappedComponent is not null || original.heldItem is not null
            || this.actionOwner.ReleaseRequested
            || !this.actionOwner.CanAdmitLeasedInputOnce(pin,NowUnixMs(),request.RuntimeInstanceId,
                request.OwnerId,request.Generation,request.ControlSeq,out _)
            || !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request,pin,Context.IsWorldReady,
                Context.IsPlayerFree,Context.CanPlayerMove,out _))
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request,CommandRejectReason.BindingMismatch,pin,NowUnixMs()));
            return;
        }
        ContainerTransferReadback? before; ContainerTransferTarget? current; int capacity;
        ClickableComponent? button;
        try {
            string? refused=ContainerTransferAdmission.Refusal(this.CaptureTransferFacts(original),target,pin.ContextGeneration,
                this.ContainerTransferReady,this.SingleItemTransferReady(original),out before,out current,out capacity); // PM-1 fix8: = published
            button=original.inventory.inventory.SingleOrDefault(b=>b.name==target.Slot.ToString(CultureInfo.InvariantCulture));
            if(refused is not null || button is null
                || original.inventory.inventory.Count(b=>b.containsPoint(button.bounds.Center.X,button.bounds.Center.Y))!=1)
                throw new InvalidOperationException("current observed transfer unavailable");
        } catch {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request,CommandRejectReason.BindingMismatch,pin,NowUnixMs()));
            return;
        }
        var input=new ContainerTransferInput(); var point=button.bounds.Center;
        var slotValid = this.BindTransferSlot(request);
        bool SameActor() { var now=this.CurrentCommandIdentity(); return ReferenceEquals(game,Game1.game1)
            && slotValid.Valid() && this.ContainerActorEligible && now==pin with {ContextGeneration=now.ContextGeneration}; }
        bool SameChest(IClickableMenu? receiver)=>receiver is ItemGrabMenu m && m.source==ItemGrabMenu.source_chest
            && ReferenceEquals(m.context,chest) && ReferenceEquals(m.sourceItem,chest)
            && PlacedTileOf(Game1.player.currentLocation,chest) is not null;
        IClickableMenu? previous=original;
        this.transferTransition=(next,ok)=> {
            string? oldToken=this.ReceiverTokenOf(previous);
            string? nextToken=this.ReceiverTokenOf(next);
            if(SameChest(next))slotValid.OwnTransition(); // PM-1 fix6 #2: the chest's own menu replacement
            input.ObserveReceiverTransition(oldToken,nextToken,SameChest(next),SameActor(),ok);
            previous=next;
        };
        ContainerTransferReadback? Read() {
            if(!SameActor() || !SameChest(Game1.activeClickableMenu))return null;
            this.AdvanceUiContextNative(this.screens.Value);
            return ContainerTransferReadback.Read(this.CaptureTransferFacts((ItemGrabMenu)Game1.activeClickableMenu),
                target,this.CurrentCommandIdentity().ContextGeneration,out _,out _);
        }
        ContainerTransferVerdict verdict;
        try {
            verdict=input.Execute(target,current,before,true,Game1.options.SnappyMenus,
                original.currentlySnappedComponent is null,capacity,
                ()=> { if(!SameActor())throw new InvalidOperationException("original actor lost");
                    if(target.Quantity is null)original.receiveLeftClick(point.X,point.Y);
                    else original.receiveRightClick(point.X,point.Y); },
                ()=> { if(!SameActor())throw new InvalidOperationException("original actor lost");
                    // Right-click is a synchronous handler with no held input or
                    // releaseRightClick API. Do not inject a left-button release.
                    if(target.Quantity is null)original.releaseLeftClick(point.X,point.Y); },Read,
                singleItemReady:this.SingleItemTransferReady(original),
                returnCursor:()=>TransferCursorReturn.Click(SameActor,SameChest,point)); // PM-1 fix10d
        } finally { this.transferTransition=null; }
        this.transferDoubt.Note(verdict.Outcome=="TransferUncertain", verdict.CleanupConfirmed);
        if(!verdict.Paired)this.Monitor.Log($"container_transfer VERDICT request_id={request.RequestId} outcome={verdict.Outcome} detail={verdict.Detail}",LogLevel.Warn);
        this.WriteCommandResult(this.commandInbox.CompleteTransfer(verdict,this.CurrentCommandIdentity(),NowUnixMs()));
    }

    bool SelectItemInputReady => this.config.SelectItemInputEnabled && Context.IsWorldReady
        && !Context.IsMainPlayer && !Context.IsSplitScreen && !this.selectionCleanupUncertain
        && Game1.player?.CursorSlotItem is null;

    // The read-only effective-target predicate below is tied to the inspected ABI.
    // A new installation requires source review, never a guessed input fallback.
    bool WorldInteractionReady => this.config.Enabled && this.config.WorldInteractionInputEnabled
        && this.UseToolCleanupEligible && !this.worldInteractionUncertain
        && typeof(Game1).Module.ModuleVersionId == new Guid("aa1f513a-94f0-4ef6-a35f-2d5b4a3721bf")
        && Game1.options?.SnappyMenus == false && TryMapConfiguredAction(out _, out _);

    static bool TryMapConfiguredAction(out SButton button, out string fingerprint)
    {
        return TryMapConfiguredBinding(Game1.options?.actionButton, allowMouseRight:true, out button, out fingerprint);
    }

    (WorldInteractionTarget? Target, StardewValley.Objects.Chest? Chest, BgActionTarget? Action) CaptureWorldInteractionTarget() =>
        this.CaptureWorldInteractionTargetCore(this.WorldInteractionReady);

    // V4 Stage 2 R4: WorldInteractionReady minus ONLY its IsActive term (via
    // UseToolCleanupEligible), for the read-only background chest-target capture.
    bool WorldInteractionBackgroundReady => this.config.Enabled && this.config.WorldInteractionInputEnabled
        && this.useToolCleanupObserverReady && Context.IsWorldReady && Game1.game1 is not null
        && !Context.IsMainPlayer && !Context.IsSplitScreen && Game1.player is not null
        && !this.worldInteractionUncertain
        && typeof(Game1).Module.ModuleVersionId == new Guid("aa1f513a-94f0-4ef6-a35f-2d5b4a3721bf")
        && Game1.options?.SnappyMenus == false && TryMapConfiguredAction(out _, out _);

    /// <summary>R4: same target/identity/token derivation as the foreground capture, no IsActive term, plus CursorInert.</summary>
    (WorldInteractionTarget? Target, StardewValley.Objects.Chest? Chest, BgActionTarget? Action) CaptureBackgroundWorldInteractionTarget(string? genericKind = null) =>
        GenericWorld.IsKind(genericKind) ? this.CaptureGenericWorld(genericKind!)
        : this.CaptureWorldInteractionTargetCore(this.WorldInteractionBackgroundReady
            && (this.PadPathConfigured || BackgroundMoveRuntime.CursorInertReason(BgHoldKind.Action) is null),
            kinds: this.InteractKindsEnabled);

    // Diagnostic only (live M1 2026-10-07 attempt 10z36): the first failing term of the last
    // capture, appended to the unavailable reason so a hidden chest target is attributable.
    string? worldTargetMiss;
    string? lastLoggedWorldTargetMiss;
    string? lastLoggedContainerMiss;
    long lastWorldTargetMissLogMs;

    // WS3a: kinds=true (background capture with WorldInteractKindsEnabled) also resolves door /
    // forage in checkAction order; the foreground capture stays chest-only.
    (WorldInteractionTarget? Target, StardewValley.Objects.Chest? Chest, BgActionTarget? Action) CaptureWorldInteractionTargetCore(bool ready,
        bool kinds = false)
    {
        if (ready && kinds && this.EventActionsReady)
        {
            var eventTarget = this.CaptureEventAction(Game1.player.GetGrabTile().ToPoint());
            return (eventTarget.Target, null, eventTarget.Action);
        }
        this.extrasMissTarget = null;  // FIXB16 review: set only by this capture's refused extras target
        this.worldTargetMiss = !ready ? "not_ready" : Game1.activeClickableMenu is not null ? "menu"
            : Game1.keyboardDispatcher?.Subscriber is not null ? "keyboard_subscriber" : !Context.IsPlayerFree ? "player_not_free"
            : !Context.CanPlayerMove ? "cannot_move" : Game1.player is not Farmer p0 ? "no_player" : p0.UsingTool ? "using_tool" : null;
        if (this.worldTargetMiss is not null || Game1.player is not Farmer player) return (null,null,null);
        Vector2 tile=player.GetGrabTile();
        if (kinds)
        {
            var resolved=this.CaptureInteractKind(player,tile);
            // FIXB23 D-B20-3: nothing at the grab tile (= ARTI's own tile, facing up / down): vanilla A falls back to the
            // faced neighbour; a pet / horse / farm animal there is offered at that tile (the press tile agrees: PadPressTile).
            if (resolved.Target is null && this.ExtrasCharacterCapture(player) is { } character)
            {
                this.worldTargetMiss=character.Miss;
                return (character.Target,null,character.Action);
            }
            if (!resolved.ChestPath)
            {
                this.worldTargetMiss=resolved.Miss;
                return (resolved.Target,null,resolved.Action);
            }
        }
        if (kinds && player.currentLocation.objects.TryGetValue(tile, out var openable) && IsOpenableContainer(openable))
            return this.CaptureGenericWorld(GenericWorld.Action);
        if (!player.currentLocation.objects.TryGetValue(tile,out var item)
            || item is not StardewValley.Objects.Chest chest || chest.GetType()!=typeof(StardewValley.Objects.Chest)
            || chest.QualifiedItemId is not ("(BC)130" or "(BC)232"))
        {
            this.worldTargetMiss = "no_chest_at_grab_tile:" + (int)tile.X + "," + (int)tile.Y;
            return (null,null,null);
        }
        string instance=this.worldTargetInstanceTokenTracker.Observe(chest)!;
        string source=SceneIdentity.ObjectFactId(player.currentLocation.NameOrUniqueName ?? player.currentLocation.Name,
            (int)tile.X,(int)tile.Y,chest.QualifiedItemId);
        var identity=this.CurrentCommandIdentity();
        string token=Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new object[] {
                instance,source,identity,(int)player.Tile.X,(int)player.Tile.Y,player.FacingDirection })))).ToLowerInvariant();
        var target=new WorldInteractionTarget(token,source,(int)tile.X,(int)tile.Y,
            (int)player.Tile.X,(int)player.Tile.Y,player.FacingDirection);
        if (!target.IsValid) this.worldTargetMiss = "target_invalid";
        GameLocation pinnedLocation=player.currentLocation;
        Vector2 pinnedTile=tile;
        StardewValley.Objects.Chest pinnedChest=chest;
        // WS3a pre-press re-check: the same chest object still at the grab tile.
        bool ChestPresent() => Game1.player is Farmer p && ReferenceEquals(p.currentLocation,pinnedLocation)
            && p.GetGrabTile()==pinnedTile && pinnedLocation.objects.TryGetValue(pinnedTile,out var now) && ReferenceEquals(now,pinnedChest);
        return target.IsValid ? (target,chest,BgActionTarget.ForChest(chest,ChestPresent)) : (null,null,null);
    }

    string WorldInteractionReadyMiss(bool background) =>
        !this.config.Enabled ? "config_disabled" : !this.config.WorldInteractionInputEnabled ? "world_interaction_disabled"
        : !background && !this.UseToolCleanupEligible ? "use_tool_cleanup_ineligible"
        : !this.useToolCleanupObserverReady ? "observer_not_ready" : !Context.IsWorldReady ? "world_not_ready"
        : Game1.game1 is null ? "no_game" : Context.IsMainPlayer ? "main_player" : Context.IsSplitScreen ? "split_screen"
        : Game1.player is null ? "no_player" : this.worldInteractionUncertain ? "interaction_uncertain"
        : typeof(Game1).Module.ModuleVersionId != new Guid("aa1f513a-94f0-4ef6-a35f-2d5b4a3721bf") ? "game_mvid"
        : Game1.options?.SnappyMenus != false ? "snappy_menus" : !TryMapConfiguredAction(out _, out _) ? "action_binding"
        : background && !this.PadPathConfigured && BackgroundMoveRuntime.CursorInertReason(BgHoldKind.Action) is string cursor ? cursor
        : this.worldTargetMiss ?? "unknown";

    static Vector2 EffectiveActionTarget()
    {
        // Read-only projection of installed Game1.pressActionButton IL04f9..0570.
        // Never call that outcome dispatcher, warp cursor or spoof mouse state.
        Vector2 mouse=new((Game1.getOldMouseX()+Game1.viewport.X)/64,
                          (Game1.getOldMouseY()+Game1.viewport.Y)/64);
        Vector2 effective=Game1.wasMouseVisibleThisFrame && Game1.mouseCursorTransparency!=0f
            && Utility.tileWithinRadiusOfPlayer((int)mouse.X,(int)mouse.Y,1,Game1.player)
            ? mouse : Game1.player.GetGrabTile();
        return effective;
    }

    // V4 Stage 2: observation advertises exactly what Admit's background-eligible term
    // (N5) would accept: both default-OFF flags on, same tool / R4 target checks. ADR 244: no focus term.
    bool BackgroundToolUiNow =>
        BgToolUiRoute.Enabled(this.config.BackgroundToolUiEnabled, this.config.BackgroundMovementEnabled, this.backgroundMove is not null);

    bool BackgroundUseToolEligibleNow => this.BackgroundToolUiNow && this.config.UseToolInputEnabled
        && this.BackgroundToolKindNow is not null;

    // WS1: the held tool's admitted background kind (Axe/Pickaxe; farm tools only with the flag).
    BgToolKind? BackgroundToolKindNow => BackgroundMoveRuntime.CurrentToolKind(this.config.BackgroundFarmToolsEnabled);

    // Same terms as Admit's backgroundActionEligible. Used by the per-tick session.json
    // publish: the R4 capture runs only while inactive with both flags on (short-circuit),
    // and is a grab-tile dictionary lookup plus, only when a chest is adjacent, one small
    // SHA-256 token (the same work EnrichWorldInteractionContext already does per snapshot).
    bool BackgroundWorldInteractionEligibleNow => this.BackgroundToolUiNow
        && (this.CaptureBackgroundWorldInteractionTarget().Target is not null || this.GenericWorldReady
            || this.HouseDoors().Count > 0 // FIXB76: publish the same route eligibility used by command admission.
            || this.EventActionsReady && (this.EventEggCandidates().Count > 0 || this.EventApproachReady));

    void EnrichWorldInteractionContext(Dictionary<string, object> snapshot)
    {
        var current=this.CaptureWorldInteractionTarget();
        bool background=false;
        if (current.Target is null && this.BackgroundToolUiNow)
        {
            background=true;
            current=this.CaptureBackgroundWorldInteractionTarget(); // R4: same target/token derivation, no IsActive term
        }
        if (snapshot.GetValueOrDefault("capabilities") is Dictionary<string,object> caps)
            caps[WorldInteractionTarget.Primitive]=new Dictionary<string,object> {
                ["implemented"]=true,["available"]=current.Target is not null,
                ["reason"]=current.Target is null ? "backend_or_observed_adjacent_chest_unavailable"
                    : "current_target_requires_effective_input_revalidation" };
        // Diagnostic only, off the observed facts (review 2026-10-07: a position-dependent reason churned the
        // ui.context revision every step). Logged on change, at most once per 5 s.
        Dictionary<string, object>? refusal = null;
        if (current.Target is null)
        {
            string miss=this.WorldInteractionReadyMiss(background);
            if (this.ExtrasEnabled && this.extrasMissTarget is { } refused)
                refusal = WorldExtras.RefusalRecord(WorldExtras.CaptureRefusal(miss, refused.Kind), refused.Kind, refused.Source, refused.X, refused.Y);
            long now=Environment.TickCount64;
            if (miss!=this.lastLoggedWorldTargetMiss && now-this.lastWorldTargetMissLogMs>=5000)
            {
                this.lastLoggedWorldTargetMiss=miss; this.lastWorldTargetMissLogMs=now;
                this.Monitor.Log("world_interact unavailable: "+miss, LogLevel.Debug);
            }
        }
        if (current.Target is { } target) snapshot["world_interaction_target"]=target.Args();
        // FIXB16 D-B14-4 (live V2B14G6harvest-a: `profession_menu_policy:level_5` only in the SMAPI log; the rule ping-ponged
        // between two crops): a policy refusal of the faced extras target is published (stable while ARTI faces it), with
        // the refused target's kind / source / tile (review: only an objective of that kind ends on it).
        if (refusal is not null) snapshot["extras_refusal"]=refusal; else snapshot.Remove("extras_refusal");
        this.EnrichMoveThroughContext(snapshot);
        this.EnrichEatCandidates(snapshot); // WS3b
        NativeCraftingFacts.Enrich(snapshot); // PM-1 fix13b: known recipes, have/need, craftable now
        this.EnrichPadContext(snapshot); // WS-G0: only with GamepadInputEnabled
        this.EnrichFishingContext(snapshot); // WS-G4: only with ReflexFishingEnabled
        this.EnrichGoalRouteContext(snapshot); // FIXB38: bounded read-only shop/fish travel preconditions
        this.EnrichEventActionContext(snapshot);
        this.EnrichFestivalContext(snapshot);
        this.EnrichExtrasTargets(snapshot); // WS-G6: only with WorldExtrasEnabled on the pad path
        this.EnrichNavGrid(snapshot); // FIXB14: walkability window for the runtime routing (same flag)
        this.EnrichB3Targets(snapshot); // W1 gap B3: crab pots / crab-pot water / own kitchen (same flag)
        this.EnrichQuestContext(snapshot); // W1 gap B4: actor quest log + the letter's presented quest (same flag)
        this.EnrichCombatChargeContext(snapshot); // WS-G5: only with ReflexCombatEnabled / ReflexChargeEnabled
    }

    // ADR 139: register blocks by their role at the capture boundary, before both
    // stream and inspect fitting. Unknown predicates inside a block inherit its role.
    static void RequireUiActionFacts(Dictionary<string, object> snapshot) =>
        UiContextWire.Require(snapshot, "world_interaction_target", "extras_refusal", "world_buttons",
            "toolbar", "container", ShippingBinSnapshot.BlockKey, "ui_motor", "ui_event", "ready_check",
            "event_movement", "event_actions", "event_action_receipt", "input_fence");

    void ArmWorldInteraction(MenuStepCommandRequest request)
    {
        this.ApplyActionOwnerRecords();
        var current=this.CaptureWorldInteractionTarget();
        var identity=this.CurrentCommandIdentity();
        if (request.InteractionTarget is not { } target || current.Target!=target
            || !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request,identity,
                Context.IsWorldReady,Context.IsPlayerFree,Context.CanPlayerMove,out _)
            || !TryMapConfiguredAction(out SButton button,out string fingerprint))
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request,CommandRejectReason.BindingMismatch,identity,NowUnixMs()));
            return;
        }
        this.worldInteractionGame=Game1.game1; this.worldInteractionChest=current.Chest;
        this.worldInteractionRequest=request;
        this.worldInteraction=new(identity,button.ToString(),fingerprint,NowUnixMs()+1500);
        this.commandInbox.ExecutionSlot.AttachClient(request, this.worldInteraction, _ => this.worldInteraction.Interrupt(),
            busy: () => this.worldInteraction?.Busy == true);
    }

    void TickWorldInteraction(uint ticks)
    {
        if (this.worldInteraction is not { Busy:true } input) return;
        var identity=this.CurrentCommandIdentity();
        bool original=ReferenceEquals(this.worldInteractionGame,Game1.game1)
            && !identity.IsMainPlayer && !identity.IsSplitScreen
            && this.worldInteractionRequest is { } pinned
            && identity.PlayerId==pinned.ExpectedPlayerId && identity.ScreenId==pinned.ExpectedScreenId
            && identity.SessionId==pinned.SessionId && identity.RuntimeInstanceId==pinned.RuntimeInstanceId;
        input.BeginUpdate(ticks,Game1.ticks,identity,this.UseToolCleanupEligible&&original);
        var request=this.worldInteractionRequest;
        bool lease=request is { } r && !this.actionOwner.ReleaseRequested
            && this.actionOwner.CanAdmitLeasedInputOnce(identity,NowUnixMs(),r.RuntimeInstanceId,r.OwnerId,r.Generation,r.ControlSeq,out _);
        SButton currentButton=SButton.None; string fingerprint="";
        bool mapped=this.UseToolCleanupEligible && TryMapConfiguredAction(out currentButton,out fingerprint);
        bool admitted=lease && this.WorldInteractionReady && mapped && currentButton.ToString()==input.Button
            && fingerprint==input.Fingerprint;
        if (!input.PressAttempted)
        {
            Vector2 effective=this.UseToolCleanupEligible ? EffectiveActionTarget() : new(-1,-1);
            admitted=request?.InteractionTarget is { } target && WorldInteractionTarget.CanPress(target,
                this.CaptureWorldInteractionTarget().Target,identity,this.WorldInteractionReady,
                Game1.game1 is null || Game1.options?.SnappyMenus!=false,lease,
                mapped && currentButton.ToString()==input.Button && fingerprint==input.Fingerprint,
                (int)effective.X,(int)effective.Y);
        }
        input.Tick(NowUnixMs(),admitted,
            ()=>this.Helper.Input.Press(Enum.Parse<SButton>(input.Button)),
            ()=> { if (!original) throw new InvalidOperationException("original game instance unavailable");
                   this.Helper.Input.Suppress(Enum.Parse<SButton>(input.Button)); });
    }

    void ReadWorldInteractionAfterUpdate(uint ticks)
    {
        if (this.worldInteraction is not { Busy:true } input) return;
        bool eligible=this.UseToolCleanupEligible && ReferenceEquals(this.worldInteractionGame,Game1.game1);
        IClickableMenu? current=Context.IsWorldReady && Game1.game1 is not null ? Game1.activeClickableMenu : null;
        string? receiver=this.ReceiverTokenOf(CurrentInteractiveMenu(current));
        bool matches=current is ItemGrabMenu menu && menu.source==ItemGrabMenu.source_chest
            && ReferenceEquals(menu.context,this.worldInteractionChest) && ReferenceEquals(menu.sourceItem,this.worldInteractionChest);
        input.AfterUpdate(ticks,NowUnixMs(),this.CurrentCommandIdentity(),eligible,
            eligible ? this.Helper.Input.IsDown(Enum.Parse<SButton>(input.Button)) : null,receiver,matches);
        if (input.Verdict is { } verdict && this.worldInteractionRequest is { } request)
        {
            this.worldInteractionUncertain |= verdict.Outcome=="InteractionUncertain" || !verdict.CleanupConfirmed;
            if (this.commandInbox.InflightRequestId==request.RequestId)
                this.WriteCommandResult(this.commandInbox.CompleteWorldInteraction(verdict,this.CurrentCommandIdentity(),NowUnixMs()));
            this.worldInteractionRequest=null;
        }
    }

    // Current on-screen receiver only; never construct a toolbar or change player selection directly.
    (Toolbar? Receiver, List<(ClickableComponent Button, SelectItemTarget Target)> Targets) CaptureToolbar()
    {
        var targets = new List<(ClickableComponent, SelectItemTarget)>();
        Toolbar[] candidates = Context.IsWorldReady && !Context.IsMainPlayer && !Context.IsSplitScreen
            && Game1.displayHUD && Game1.activeClickableMenu is null && Game1.keyboardDispatcher?.Subscriber is null
            ? Game1.onScreenMenus.OfType<Toolbar>().Take(2).ToArray() : Array.Empty<Toolbar>();
        Toolbar? toolbar = candidates.Length == 1 ? candidates[0] : null;
        string? receiver = this.toolbarInstanceTokenTracker.Observe(toolbar);
        if (toolbar is null || receiver is null) return (null, targets);
        foreach (ClickableComponent button in toolbar.buttons.Take(48))
        {
            if (!int.TryParse(button.name, out int slot) || slot < 0 || slot >= Game1.player.Items.Count
                || button.bounds.Width <= 0 || button.bounds.Height <= 0)
                continue;
            var item = Game1.player.Items[slot];
            string pin = JsonSerializer.Serialize(new object[] { receiver, slot, item?.QualifiedItemId ?? "",
                item?.Stack ?? 0, item?.Quality ?? 0, button.bounds.X, button.bounds.Y, button.bounds.Width,
                button.bounds.Height, Game1.uiViewport.Width, Game1.uiViewport.Height });
            string token = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
                System.Text.Encoding.UTF8.GetBytes(pin))).ToLowerInvariant();
            var target = new SelectItemTarget(receiver, token, slot, item?.QualifiedItemId ?? "", item?.Stack ?? 0, item?.Quality ?? 0);
            if (target.IsValid) targets.Add((button, target));
        }
        if (targets.Select(t => t.Item2.Slot).Distinct().Count() != targets.Count)
            return (null, new());
        return (toolbar, targets);
    }

    void EnrichToolbarContext(Dictionary<string, object> snapshot)
    {
        var toolbar = this.CaptureToolbar();
        bool ready = this.SelectItemInputReady && Context.IsPlayerFree && Context.CanPlayerMove
            && toolbar.Receiver is not null && toolbar.Targets.Count > 0;
        if (snapshot["capabilities"] is Dictionary<string, object> caps)
            caps["select_item"] = ready ? new Dictionary<string, object> { ["implemented"] = true, ["available"] = true }
                : new Dictionary<string, object> { ["implemented"] = true, ["available"] = false,
                    ["reason"] = this.config.SelectItemInputEnabled ? "preconditions_unmet" : "backend_disabled" };
        if (toolbar.Receiver is not null)
            snapshot["toolbar"] = new Dictionary<string, object> {
                ["active_slot"] = Game1.player.CurrentToolIndex,
                ["targets"] = toolbar.Targets.Select(t => t.Target.Args()).ToArray() };
    }

    void ExecuteSelectItem(MenuStepCommandRequest request)
    {
        this.ApplyActionOwnerRecords(); // A priority stop beats an admitted but not yet executed click.
        var identity = this.CurrentCommandIdentity();
        bool eligible = this.SelectItemInputReady && !this.actionOwner.ReleaseRequested
            && this.actionOwner.CanAdmitLeasedInputOnce(identity, NowUnixMs(), request.RuntimeInstanceId,
                request.OwnerId, request.Generation, request.ControlSeq, out _)
            && MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request, identity,
                Context.IsWorldReady, Context.IsPlayerFree, Context.CanPlayerMove, out _);
        var toolbar = this.CaptureToolbar();
        var match = toolbar.Targets.SingleOrDefault(t => t.Target == request.SelectionTarget);
        if (!eligible || toolbar.Receiver is null || match.Button is null || request.SelectionTarget is not { } target)
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request,
                CommandRejectReason.BindingMismatch, identity, NowUnixMs()));
            return;
        }
        var original = toolbar.Receiver;
        Point point = match.Button.bounds.Center;
        if (original.buttons.Count(b => b.containsPoint(point.X, point.Y)) != 1)
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request,
                CommandRejectReason.BindingMismatch, identity, NowUnixMs()));
            return;
        }
        var verdict = SelectItemInput.Execute(target, match.Target, true,
            () => original.receiveLeftClick(point.X, point.Y),
            () => original.releaseLeftClick(point.X, point.Y),
            () => Game1.player.CurrentToolIndex == target.Slot
                && ReferenceEquals(original, this.CaptureToolbar().Receiver)
                && this.CaptureToolbar().Targets.Any(t => t.Target == target) ? target : null);
        this.selectionCleanupUncertain |= !verdict.CleanupConfirmed;
        this.WriteCommandResult(this.commandInbox.CompleteSelection(verdict, identity, NowUnixMs()));
    }

    bool UseToolInputReady => this.config.UseToolInputEnabled
        && this.useToolCleanupObserverReady && Context.IsWorldReady
        && !Context.IsMainPlayer && !Context.IsSplitScreen && Game1.game1?.IsActive == true
        && !this.useToolAdapter.LifecycleFailClosed && !this.useToolAdapter.UncertainEffect
        && !this.useToolAdapter.ReplayLatched
        && TryMapFirstConfiguredUseToolButton(out _, out _);

    UseToolInputTarget CurrentUseToolTarget()
    {
        Farmer? farmer = Game1.player;
        if (farmer?.CurrentTool is not { } tool) return default;
        return new(farmer.CurrentToolIndex, tool.QualifiedItemId,
            (int)farmer.Tile.X, (int)farmer.Tile.Y, farmer.FacingDirection);
    }

    void ArmUseToolFromCommand(MenuStepCommandRequest request)
    {
        CommandInboxIdentity identity = this.CurrentCommandIdentity();
        bool binding = MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request, identity,
            Context.IsWorldReady, Context.IsPlayerFree, Context.CanPlayerMove, out var reason);
        if (binding && this.ForegroundFarmToolRefused(request))
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request, CommandRejectReason.ArmRejected,
                identity, NowUnixMs()));
            return;
        }
        if (!binding || !this.UseToolInputReady || Game1.game1?.IsActive != true
            || Game1.activeClickableMenu is not null || Game1.keyboardDispatcher?.Subscriber is not null
            || request.ToolTarget is not { } tool || tool != this.CurrentUseToolTarget()
            || this.useToolInput is null
            || !this.useToolInput.TryStart(request.RequestId, request.Ticks, tool,
                this.CurrentUseToolFence(identity), NowUnixMs()))
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request,
                binding ? CommandRejectReason.BindingMismatch : reason, identity, NowUnixMs()));
            return;
        }
        this.useToolInputRequest = request;
        this.commandInbox.ExecutionSlot.AttachClient(request, this.useToolInput, this.useToolInput.Interrupt,
            busy: () => this.useToolInput?.Active == true || this.UseToolCleanupPending);
        this.backgroundUseToolVerdict = null;
        this.Monitor.Log(
            $"arti_command armed: request_id={request.RequestId} op={request.Op} primitive={request.Primitive} "
                + $"foreground use_tool ticks={request.Ticks} gameTicks={Game1.ticks}",
            LogLevel.Info);
    }

    // WS1 review fix (minor 7): the foreground route has no faced tool-tile guard and a mouse-dependent
    // hit point; hoe / watering can / scythe use_tool is background-only (BgToolTileGuard there).
    bool ForegroundFarmToolRefused(MenuStepCommandRequest request)
    {
        if (!BgToolClass.IsFarmToolType(Game1.player?.CurrentTool?.GetType().FullName))
            return false;
        this.Monitor.Log($"arti_command foreground use_tool refused: request_id={request.RequestId} farm tool is background-only",
            LogLevel.Info);
        return true;
    }

    UseToolHoldBindingFence CurrentUseToolFence(CommandInboxIdentity identity) =>
        new(
            identity.IsMainPlayer,
            identity.IsSplitScreen,
            identity.RuntimeInstanceId,
            identity.SessionId,
            identity.PlayerId,
            identity.ScreenId,
            identity.ContextGeneration,
            identity.LocationId);

    /// <summary>
    /// Pintu in-process untuk pemanggil G6/skill kelak. Slice ini tidak
    /// menemu pemanggil itu dan tidak mengiklankan capability.
    /// </summary>
    void RequestUseToolNativeIntent(UseToolHoldIntent intent) =>
        this.useToolAdapter.SetCallerIntent(intent);

    bool UseToolCleanupEligible => this.useToolCleanupObserverReady
        && Context.IsWorldReady && Game1.game1 is not null && Game1.game1.IsActive
        && !Context.IsMainPlayer && !Context.IsSplitScreen && Game1.player is not null;

    void BeginUseToolCleanupUpdate(uint ticks)
    {
        try
        {
            this.useToolAdapter.BeginCleanupUpdate(ticks, Game1.ticks,
                this.CurrentUseToolFence(this.CurrentCommandIdentity()), this.UseToolCleanupEligible);
        }
        catch (Exception)
        {
            this.useToolAdapter.NoteCleanupBoundaryFailure();
        }
    }

    void ObserveUseToolGameUpdate(Game1 instance, bool succeeded)
    {
        try
        {
            this.worldInteraction?.ObserveGameUpdate(Game1.ticks, this.CurrentCommandIdentity(),
                this.UseToolCleanupEligible && ReferenceEquals(instance, this.worldInteractionGame), succeeded);
            this.CaptureReceiverFacts(); // controller validity at the native consumption bracket end
            this.useToolAdapter.NoteCleanupGameUpdate(Game1.ticks,
                this.CurrentUseToolFence(this.CurrentCommandIdentity()),
                this.UseToolCleanupEligible, succeeded && ReferenceEquals(instance, Game1.game1));
        }
        catch (Exception)
        {
            this.useToolAdapter.NoteCleanupBoundaryFailure();
        }
    }

    void ReadUseToolCleanupAfterUpdate(uint ticks)
    {
        try
        {
            string? pin = this.useToolAdapter.PinnedButton;
            bool eligible = this.UseToolCleanupEligible;
            bool? down = eligible && pin is not null && Enum.TryParse(pin, out SButton button)
                && button != SButton.None ? this.Helper.Input.IsDown(button) : null;
            if (this.useToolAdapter.TryCompleteCleanupUpdate(this.useToolHold, ticks,
                this.CurrentUseToolFence(this.CurrentCommandIdentity()), eligible, pin ?? "", down))
                this.Monitor.Log($"use-tool cleanup receipt ACK pin={pin} tick={ticks}", LogLevel.Debug);
        }
        catch (Exception)
        {
            this.useToolAdapter.NoteCleanupBoundaryFailure();
        }
        this.useToolInput?.ObserveUseCycle(Game1.player?.UsingTool == true);
        this.useToolInput?.ObserveAfterUpdate(NowUnixMs());
        if (this.useToolInput?.Verdict is { } verdict && this.useToolInputRequest is { } request
            && verdict.RequestId == request.RequestId)
        {
            if (this.commandInbox.InflightRequestId == request.RequestId)
                this.WriteCommandResult(this.commandInbox.CompleteUseTool(verdict,
                    this.CurrentCommandIdentity(), NowUnixMs()));
            this.useToolInputRequest = null;
        }
    }

    void TickUseToolNativeAdapter()
    {
        CommandInboxIdentity identity = this.CurrentCommandIdentity();
        long now = NowUnixMs();
        MutationAdvice advice = this.actionOwner.Advise(now, this.UseToolCleanupPending);
        // CT-044/045 + LOCAL E2 5585870719: focus loss skips TrueUpdate.
        // Eligibility = world-ready + instance + Game1.game1.IsActive.
        // Unfocused: no first-Hold pin; Holding => ReleaseOwed on original pin.
        // IsActive true again is not cleanup ACK / Hold re-arm.
        bool worldReady = Context.IsWorldReady && Game1.game1 is not null && Game1.game1.IsActive;
        // Game1.options dereferences game1 in the installed getter. Mapping must
        // wait for eligibility; an owned Hold still reaches the adapter for cleanup.
        bool mapped = false;
        SButton mappedButton = SButton.None;
        string fingerprint = "";
        if (worldReady)
        {
            mapped = TryMapFirstConfiguredUseToolButton(out mappedButton, out fingerprint);
        }
        bool leaseMatches = false;
        string? leaseWhy = "LeaseInvalid";
        if (this.actionOwner.IsArmed)
        {
            leaseMatches = this.actionOwner.CanAdmitLeasedInputOnce(
                identity,
                now,
                this.runtimeInstanceId,
                this.actionOwner.OwnerId,
                this.actionOwner.Generation,
                this.actionOwner.ControlSeq,
                out CommandRejectReason leaseReason);
            leaseWhy = leaseMatches ? null : leaseReason.ToString();
        }
        else if (this.UseToolCleanupPending)
        {
            leaseMatches = false;
            leaseWhy = nameof(CommandRejectReason.LeaseExpired);
        }

        if (this.useToolInputRequest is { } request && this.useToolInput?.Active == true)
        {
            // Recheck the original request owner; a newly acquired lease is
            // never authority to continue an older invocation.
            leaseMatches = leaseMatches && request.OwnerId == this.actionOwner.OwnerId
                && request.Generation == this.actionOwner.Generation
                && request.RuntimeInstanceId == identity.RuntimeInstanceId;
            bool eligible = this.UseToolCleanupEligible && Game1.activeClickableMenu is null
                && Game1.keyboardDispatcher?.Subscriber is null
                && (Context.IsPlayerFree && Context.CanPlayerMove || Game1.player?.UsingTool == true);
            this.useToolInput.PrepareTick(this.useToolInputTick, now, this.CurrentUseToolTarget(),
                this.CurrentUseToolFence(identity), this.UseToolInputReady, eligible, leaseMatches);
        }
        UseToolNativeAdapterPlan plan = this.useToolAdapter.Tick(
            this.useToolHold,
            advice,
            leaseMatches,
            leaseWhy,
            this.CurrentUseToolFence(identity),
            mapped ? mappedButton.ToString() : null,
            mapped ? fingerprint : null,
            worldReady,
            eligibleUpdateTicking: true);
        this.ApplyUseToolNativePlan(plan);
    }

    void ApplyUseToolNativePlan(UseToolNativeAdapterPlan plan)
    {
        if (plan.Action == UseToolNativeAction.None || plan.Button is null)
            return;
        if (!Enum.TryParse(plan.Button, out SButton button) || button == SButton.None)
            return;

        try
        {
            if (plan.Action == UseToolNativeAction.Press)
            {
                this.Helper.Input.Press(button);
                this.useToolAdapter.NoteNativePressIssued();
                this.useToolInput?.NoteNativePressIssued(this.useToolInputTick);
                this.Monitor.Log(
                    $"use-tool native Press pin={button} backend={plan.BackendOutput} "
                        + $"phase={plan.BackendPhase}",
                    LogLevel.Trace);
            }
            else if (plan.Action == UseToolNativeAction.Suppress)
            {
                this.Helper.Input.Suppress(button);
                this.useToolAdapter.NoteNativeSuppressIssued();
                this.Monitor.Log(
                    $"use-tool native Suppress pin={button} reason={plan.Reason}; "
                        + "cleanup debt retained until E2 application/readback.",
                    LogLevel.Info);
            }
        }
        catch (Exception ex)
        {
            this.useToolAdapter.NoteNativeException();
            this.Monitor.Log(
                $"use-tool native {plan.Action} uncertain: {ex.GetType().Name}: {ex.Message}",
                LogLevel.Warn);
        }

        // Jangan AcknowledgeCleanup / AckOwnedRelease di handler yang sama.
        // Helper.Input.IsDown di sini bukan bukti. UpdateTicked telanjang bukan bukti.
    }

    void InterruptUseToolNativeForLifecycle(string reason)
    {
        this.useToolInput?.Interrupt(reason);
        this.useToolAdapter.SetCallerIntent(UseToolHoldIntent.Neutral);
        this.useToolAdapter.NoteLifecycleReset();
        CommandInboxIdentity identity = this.CurrentCommandIdentity();
        MutationAdvice advice = this.actionOwner.Advise(NowUnixMs(), this.UseToolCleanupPending);
        UseToolNativeAdapterPlan plan = this.useToolAdapter.Tick(
            this.useToolHold,
            advice,
            leaseMatches: false,
            leaseRejectReason: reason,
            this.CurrentUseToolFence(identity),
            this.useToolAdapter.PinnedButton,
            this.useToolAdapter.PinnedFingerprint,
            worldReady: false,
            eligibleUpdateTicking: false);
        this.ApplyUseToolNativePlan(plan);
        this.Monitor.Log(
            $"use-tool native lifecycle {reason}: pin={this.useToolAdapter.PinnedButton} "
                + $"debt={this.useToolAdapter.HasNativeCleanupDebt} "
                + "fail-closed unconfirmed cleanup (LOCAL E2 gate remaining).",
            LogLevel.Warn);
    }

    /// <summary>
    /// Binding gerak dari opsi game, bukan WASD keras. Index 0 mengikuti
    /// contoh resmi SMAPI <c>Game1.options.moveLeftButton[0].ToSButton()</c>.
    /// </summary>
    private static bool TryMapMoveButton(string direction, out SButton button)
    {
        button = SButton.None;
        Options? options = Game1.options;
        if (options is null)
            return false;

        InputButton[]? raw = direction switch
        {
            "left" => options.moveLeftButton,
            "right" => options.moveRightButton,
            "up" => options.moveUpButton,
            "down" => options.moveDownButton,
            _ => null,
        };
        if (raw is null || raw.Length == 0)
            return false;

        button = raw[0].ToSButton();
        return true;
    }

    private void OnProbeAfterCommand(string command, string[] args)
    {
        if (this.RejectUnleasedMutation("arti_probe_after"))
            return;
        if (this.MutationInflight)
        {
            this.Monitor.Log("arti_probe_after ditolak: menu input action is active", LogLevel.Warn);
            return;
        }
        if (!WorldPressDelayedProbe.TryParseCommand(
                args,
                out int delayTicks,
                out string direction,
                out int pressTicks,
                out ProbePressStatus parseError))
        {
            this.Monitor.Log($"arti_probe_after ditolak: {parseError}", LogLevel.Warn);
            return;
        }

        var context = new ProbeWorldContext(
            Context.IsWorldReady,
            Context.IsMultiplayer,
            Context.IsMainPlayer,
            Context.IsSplitScreen
        );
        ProbePressStatus status = this.delayedWorldPressProbe.TryArm(
            delayTicks,
            direction,
            pressTicks,
            context,
            immediateProbeActive: this.worldPressProbe.IsActive
        );
        this.Monitor.Log(
            status == ProbePressStatus.Started
                ? $"arti_probe_after armed: delay={delayTicks} direction={direction} movement={pressTicks} gameTicks={Game1.ticks} remaining={delayTicks}"
                : $"arti_probe_after ditolak: {status}",
            status == ProbePressStatus.Started ? LogLevel.Info : LogLevel.Warn
        );
    }

    private void TickDelayedWorldPressProbe(UpdateTickedEventArgs e)
    {
        if (!this.delayedWorldPressProbe.IsPending)
            return;

        var context = new ProbeWorldContext(
            Context.IsWorldReady,
            Context.IsMultiplayer,
            Context.IsMainPlayer,
            Context.IsSplitScreen
        );
        DelayedProbeTickResult result = this.delayedWorldPressProbe.ConsumeTick(context);
        if (result.Kind == DelayedProbeTickKind.Waiting)
        {
            if (result.RemainingDelayTicks % 60 == 0)
            {
                this.Monitor.Log(
                    $"arti_probe_after countdown: remaining={result.RemainingDelayTicks} updateTicks={e.Ticks} gameTicks={Game1.ticks} direction={result.Direction} movement={result.MovementTicks}",
                    LogLevel.Info
                );
            }
            return;
        }

        if (result.Kind == DelayedProbeTickKind.Rejected)
        {
            this.Monitor.Log(
                $"arti_probe_after ditolak saat trigger: {result.Rejection} updateTicks={e.Ticks} gameTicks={Game1.ticks}",
                LogLevel.Warn
            );
            return;
        }

        if (result.Kind != DelayedProbeTickKind.Trigger)
            return;

        this.Monitor.Log(
            $"arti_probe_after trigger: updateTicks={e.Ticks} gameTicks={Game1.ticks} direction={result.Direction} movement={result.MovementTicks}",
            LogLevel.Info
        );
        if (!TryMapMoveButton(result.Direction!, out SButton button))
        {
            this.Monitor.Log(
                $"arti_probe_after ditolak saat trigger: {ProbePressStatus.RejectedUnmappedBinding} updateTicks={e.Ticks} gameTicks={Game1.ticks}",
                LogLevel.Warn
            );
            return;
        }

        ProbePressStatus armed = this.worldPressProbe.TryArm(
            result.Direction!,
            result.MovementTicks,
            context,
            () => this.Helper.Input.Press(button)
        );
        this.Monitor.Log(
            armed == ProbePressStatus.Started
                ? $"arti_probe_after mapped SButton: {button}; P2 mulai {result.Direction} x {result.MovementTicks} tick."
                : $"arti_probe_after ditolak saat trigger: {armed} updateTicks={e.Ticks} gameTicks={Game1.ticks}",
            armed == ProbePressStatus.Started ? LogLevel.Info : LogLevel.Warn
        );
    }

    private void TickWorldPressProbe()
    {
        MutationAdvice advice = this.actionOwner.Advise(
            NowUnixMs(),
            ownedInflight: this.commandInbox.HasInflight
                && (string.Equals(
                        this.commandInbox.InflightOp,
                        MenuStepCommandInbox.OpWorldPress,
                        StringComparison.Ordinal)
                    || string.Equals(
                        this.commandInbox.InflightOp,
                        MenuStepCommandInbox.OpInputOnce,
                        StringComparison.Ordinal)));
        if (advice.RequestOwnedRelease)
        {
            this.worldPressProbe.Cancel();
            return;
        }

        if (this.worldPressProbe.IsActive
            && string.Equals(
                this.commandInbox.InflightOp,
                MenuStepCommandInbox.OpInputOnce,
                StringComparison.Ordinal))
        {
            CommandInboxIdentity current = this.CurrentCommandIdentity();
            if (!this.commandInbox.TryValidateInflightInputOnceCurrentBinding(
                    current,
                    Context.IsWorldReady,
                    Context.IsPlayerFree,
                    Context.CanPlayerMove,
                    out CommandRejectReason stale))
            {
                this.worldPressProbe.Cancel();
                this.TryInterruptInboxOp(MenuStepCommandInbox.OpInputOnce, stale.ToString());
                return;
            }
        }

        bool wasActive = this.worldPressProbe.IsActive;
        int runId = this.worldPressProbe.RunId;
        string? direction = this.worldPressProbe.ActiveDirection;
        if (!this.worldPressProbe.TryConsumeTick())
            return;
        if (wasActive && !this.worldPressProbe.IsActive)
        {
            this.Monitor.Log("arti_probe_press selesai sendiri.", LogLevel.Info);
            this.TryCompleteInboxWorldPress(
                runId, this.worldPressProbe.PressCount, direction);
        }
    }

    private sealed class ScreenState
    {
        public BridgeClient? Client { get; set; }

        public Task<bool>? Pending { get; set; }

        /// <summary>Cegah log hello terulang 10x per detik.</summary>
        public bool HelloDilaporkan { get; set; }

        public UiRevisionTracker UiRevision { get; } = new();

        public PlayerControlTransitionTracker PlayerControlTransitions { get; } = new();

        public string? PlayerControlUnavailableReason { get; set; }

        public SceneObservationScheduler Scene { get; } = new();

        public SceneSnapshotCapture SceneCapture { get; private set; } = new();
        public UiContextAccumulator Knowledge { get; } = new();
        internal NativeWorldKnowledge? WorldKnowledge { get; set; }
        public ObservationPayload? InflightKnowledge { get; set; }
        public uint KnowledgeTicks { get; set; }
        public bool KnowledgeSnapshotSent { get; set; }
        public WorldKnowledgeWire KnowledgeWire { get; private set; } = new();

        public SceneDeltaAccumulator Deltas { get; } = new();

        /// <summary>FINAL Part C3: one door re-resolve per (location, day, native clock).</summary>
        public SceneDoorClock DoorClock { get; } = new();

        public SceneAckToken InflightSceneAck { get; set; }

        public string InflightSceneLocationId { get; set; } = "";

        public ObservationPayload? InflightScenePayload { get; set; }

        public InventoryObservationScheduler Inventory { get; } = new();

        public SceneAckToken InflightInventoryAck { get; set; }

        public string InflightInventoryFingerprint { get; set; } = "";

        public ObservationPayload? InflightInventoryPayload { get; set; }

        public UiContextSnapshotCapture UiContext { get; } = new();

        public ObservationPayload? InflightUiPayload { get; set; }

        public UiTextSnapshotCapture UiText { get; } = new();

        public ObservationPayload? InflightUiTextPayload { get; set; }

        public void ClearInflightScene()
        {
            this.InflightSceneAck = default;
            this.InflightSceneLocationId = "";
            this.InflightScenePayload = null;
        }

        public void ClearInflightInventory()
        {
            this.InflightInventoryAck = default;
            this.InflightInventoryFingerprint = "";
            this.InflightInventoryPayload = null;
        }

        public void ClearInflightUi()
        {
            this.InflightUiPayload = null;
        }

        public void ClearInflightUiText()
        {
            this.InflightUiTextPayload = null;
        }

        public void AbandonClient()
        {
            BridgeClient? previous = this.Client;
            this.Client = null;
            this.HelloDilaporkan = false;
            if (previous is not null)
                _ = Task.Run(async () => await previous.DisposeAsync());
        }

        public void Reset()
        {
            this.AbandonClient();
            this.Pending = null;
            this.ClearInflightScene();
            this.ClearInflightInventory();
            this.ClearInflightUi();
            this.ClearInflightUiText();
            this.UiRevision.Reset();
            this.PlayerControlTransitions.Reset();
            this.PlayerControlUnavailableReason = null;
            this.Scene.OnReturnedToTitle();
            this.Inventory.OnReturnedToTitle();
            this.UiContext.OnReturnedToTitle();
            this.Knowledge.OnReturnedToTitle();
            this.WorldKnowledge = null;
            this.InflightKnowledge = null;
            this.KnowledgeTicks = 0;
            this.KnowledgeSnapshotSent = false;
            this.KnowledgeWire = new();
            this.UiText.OnReturnedToTitle();
            this.Deltas.Invalidate();
            this.SceneCapture = new SceneSnapshotCapture();
        }
    }
}
