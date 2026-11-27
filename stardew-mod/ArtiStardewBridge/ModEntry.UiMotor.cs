using System.Diagnostics;
using System.Text.Json;
using ArtiCore.Pure;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Network;
using StardewValley.Tools;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    void SetUpGenericUiMotor(IModHelper helper)
    {
        if (!this.config.UiPadMenusEnabled) return;
        // Mouse-mode native callbacks can also warp the OS cursor (e.g. Escape with an item).
        // Install independently of pad admission; pad probes/refusals still control the pad path.
        if (!PadCursorWarpGuard.Installed)
            PadCursorWarpGuard.Install(helper.ModRegistry.ModID,
                () => this.config.UiPadMenusEnabled || this.config.GamepadInputEnabled, this.Monitor);
        helper.Events.GameLoop.ReturnedToTitle += (_, _) => this.AbortGenericUiMotor("returned_to_title");
        // SMAPI casts Game1.input to its sealed SInputState around UpdateTicking (TrueUpdate,
        // ApplyOverrides) and in every IInputHelper read; a frame left there skips the whole base
        // update. Seat the frame only inside vanilla Game1.Update, after SMAPI's fold-in.
        try { UiMotorInputSeatPatch.Install(this.ModManifest.UniqueID, this.EnterUiMotorGameUpdate, this.ExitUiMotorGameUpdate); }
        catch (Exception ex)
        { this.Monitor.Log("UI motor update seat unavailable; polled consumers will not see motor frames: " + ex, LogLevel.Error); }
        // ADR 250: while her frame is seated the game's menu update takes its inactive branch in both window states.
        try { UiMotorMenuUpdatePatch.Install(this.ModManifest.UniqueID, this.uiMotorSeat); }
        catch (Exception ex)
        { this.Monitor.Log("UI motor menu-update patch unavailable; a menu press with her window active is refused ("
            + UiMotorFrontPath.Unavailable + "): " + ex, LogLevel.Error); }
    }
    void EnterUiMotorGameUpdate(Game1 instance)
    {
        if (!ReferenceEquals(instance, Game1.game1) || !this.uiMotorSeat.Staged || this.uiMotor is not { } lease) return;
        try
        {
            // Other handlers/SMAPI run between our edge and this prefix. They cannot donate a receiver.
            if (!this.UiMotorValid(lease) || !lease.Succession.Begin(GenericInputOwner, this.ReadUiPadFacts()))
            { lease.Fault ??= "ui_receiver_changed_outside_input"; this.RestoreGenericUiFrame(); return; }
            // ADR 250 review B4: seat only where the frame is consumed once, in the window state of this update.
            if (UiMotorFrontPath.FrameRefusal(UiMotorInputSeatPatch.Installed, UiMotorMenuUpdatePatch.Installed,
                    Game1.game1?.IsActive == true, lease.Operation) is { } unseatable)
            { lease.Fault ??= unseatable; this.RestoreGenericUiFrame(); return; }
            // ADR 250 review S1: while her frame is seated the game reads her frame, not the owner's devices. The owner's
            // input in her window during her press is therefore not delivered; it is named (UI_MOTOR owner_input).
            if (Game1.game1?.IsActive == true)
            {
                BgInputState real = BackgroundInputNormalizer.Normalize(Game1.input.GetKeyboardState(), Game1.input.GetMouseState(),
                    Game1.input.GetGamePadState(), padPath: false);
                if (UiMotorFrontPath.OwnerInput(real, lease.OwnerWheel) is { } ownerInput && !lease.OwnerInput.Contains(ownerInput))
                    lease.OwnerInput.Add(ownerInput); // W1 S2: a wheel step alone is named too
                lease.OwnerWheel = real.ScrollWheel;
            }
            if (this.uiMotorSeat.EnterGameUpdate(lease.Fault is null))
            { this.uiMotorUpdating = lease; lease.UpdateFrames++; lease.Consumption = this.commandInbox.ExecutionSlot.BeginConsumption();
                // W1 B3: a polled receiver's edge counts as delivered only when its pressed frame is seated in a game update.
                if (lease.Frames.Pressed && (lease.Operation is "key_menu" or "key_LB" or "key_RB" || lease.PolledReceiver)) lease.Delivered = true; }
        }
        catch { lease.Fault ??= "readback_fault"; this.RestoreGenericUiFrame(); }
    }
    void ExitUiMotorGameUpdate(Game1 instance, bool completed)
    {
        if (!ReferenceEquals(instance, Game1.game1)) return;
        var lease = this.uiMotorUpdating;
        this.uiMotorUpdating = null;
        if (this.uiMotorSeat.ExitGameUpdate() == GenericUiInputSeat<InputState, (MouseState, KeyboardState, GamePadState)>.Release.OwnershipLost
            && lease is not null) lease.Fault = "input_layer_ownership_lost";
        if (lease?.Delivered == true) lease.Attempt.Delivered = true;
        if (completed && lease?.Delivered == true) this.ObserveSleepAnswer(lease.Owner as IClickableMenu, lease.Before.UiKey?.QuestionKey);
        this.commandInbox.ExecutionSlot.EndConsumption(lease?.Consumption, completed);
        if (lease is null || !ReferenceEquals(this.uiMotor, lease)) return;
        if (!completed || !this.UiMotorBound(lease)) lease.Fault ??= "ui_native_update_unconfirmed";
        if (lease.Fault is not null) return;
        try
        {
            if (!lease.Succession.End(GenericInputOwner, this.ReadGenericUiReadback(lease.Before),
                    lease.Delivered, completed, "native_update", lease.Frames.Pressed ? "held" : lease.Frames.WasPressed ? "release" : "neutral"))
                lease.Fault = "ui_receiver_changed_outside_input";
            this.EndUiMotorConsumption(lease);
        }
        catch { lease.Fault = "readback_fault"; }
    }
    sealed class UiMotorInput : InputState
    {
        readonly MouseState mouse;
        readonly KeyboardState keyboard;
        readonly GamePadState pad;
        /// <summary>GenericUiPadLink: the connection the game already holds (null = the frame's own pad).</summary>
        public Func<bool>? Connected { get; init; }
        public UiMotorInput(Point point, bool left, bool right, Keys[] keys, int wheel = 0, Buttons? button = null)
        {
            this.mouse = new MouseState(point.X, point.Y, wheel, left ? ButtonState.Pressed : ButtonState.Released,
                ButtonState.Released, right ? ButtonState.Pressed : ButtonState.Released, ButtonState.Released, ButtonState.Released);
            this.keyboard = new KeyboardState(keys);
            this.pad = button is { } b ? new GamePadState(Vector2.Zero, Vector2.Zero, 0, 0, b) : default;
        }
        public override void UpdateStates() { }
        public override void Update() { }
        public override MouseState GetMouseState() => this.mouse;
        public override KeyboardState GetKeyboardState() => this.keyboard;
        // GenericUiPadLink: CheckGamepadMode reads the connection the game already holds (no banner toggle).
        // A pad-button frame (LB/RB, q12-gated) is connected by construction and keeps that.
        public override GamePadState GetGamePadState() => this.Connected?.Invoke() == true && !this.pad.IsConnected
            ? new GamePadState(Vector2.Zero, Vector2.Zero, 0f, 0f, Array.Empty<Buttons>()) : this.pad;
        public override void SetMousePosition(int x, int y) { }
    }
    sealed class UiMotorLease
    {
        public GenericUiFrames Frames = null!;
        public object? Owner;
        public GenericUiSuccession Succession = null!;
        public string Session = "", Operation = "";
        public long Player;
        public string Location = "";
        public int Day;
        public Point Point;
        public bool Right, Pointer, Shift, Control;
        public Keys Key;
        public Buttons? Pad;
        public int Wheel;
        public Stopwatch Deadline = Stopwatch.StartNew();
        public UiPadFacts Before = null!;
        public MenuStepCommandRequest? Request;
        public ActiveAttempt Attempt = null!;
        public ActiveAttempt? Consumption;
        public long Epoch;
        public string? EndedBy;
        public string? Fault;
        public bool Watchdog, Delivered;
        public int ComponentId;
        public int UpdateFrames; // native game updates that ran with the frame seated
        public List<NetMutex> Locks = new();
        // After the old receiver goes away, only readback may run for a world note close.
        public bool SettlingUnreadableClose => this.Operation == "close" && this.Delivered
            && this.Owner is JunimoNoteMenu { scrambledText: true, fromGameMenu: false }
            && !ReferenceEquals(GenericInputOwner, this.Owner);
        public bool HoldsMutex
        {
            get
            {
                foreach (var mutex in this.Locks)
                    if (mutex.IsLockHeld()) return true;
                return false;
            }
        }
        /// <summary>ADR 250 review B3/S3: the minigame this press is aimed at (no menu surface), pinned at admission.</summary>
        public StardewValley.Minigames.IMinigame? Minigame;
        /// <summary>ADR 250 review S1: the owner's input seen in her window during her seated frames (named, not delivered).</summary>
        public List<string> OwnerInput = new();
        /// <summary>W1 B3: the receiver consumes the frame in the game's own update (profession chooser, text entry, emotes).</summary>
        public bool PolledReceiver;
        /// <summary>W1 S2: the owner's wheel value at the last seated sample (a change is named).</summary>
        public int? OwnerWheel;
    }
    UiMotorLease? uiMotor;
    UiMotorLease? uiMotorUpdating;
    readonly GenericUiInputSeat<InputState, (MouseState, KeyboardState, GamePadState)> uiMotorSeat = new(
        () => Game1.input, input => Game1.input = input,
        () => (Game1.oldMouseState, Game1.oldKBState, Game1.oldPadState),
        history => (Game1.oldMouseState, Game1.oldKBState, Game1.oldPadState) = history);
    readonly ProgressionClient uiWatchdog = new();
    Dictionary<string, object>? uiMotorFact;
    long uiMotorFactSeq;
    long uiMotorLastRender;
    long uiWatchdogNextPoll;
    // ADR 241 C4: a held progression page names its own cause (read-only; see NoteProgressionHeld).
    readonly ProgressionHeld progressionHeld = new();
    long progressionHeldNextPoll;
    ProgressionClient.Fault? uiMotorFault;
    List<NetMutex> uiFaultLocks = new();
    static IClickableMenu? GenericMenuSurface => Game1.overlayMenu ?? (IClickableMenu?)Game1.emoteMenu
        ?? Game1.textEntry ?? Game1.activeClickableMenu;
    static object? GenericInputOwner => (object?)GenericMenuSurface ?? Game1.currentMinigame
        ?? (object?)Game1.CurrentEvent ?? Game1.player?.CursorSlotItem
        ?? (object?)(Game1.player?.CurrentTool is FishingRod { fishCaught: true } rod ? rod : null);
    static Keys GenericMenuKey => Game1.options.getFirstKeyboardKeyFromInputButtonList(Game1.options.menuButton);
    static Keys GenericConfirmKey => Game1.options.getFirstKeyboardKeyFromInputButtonList(Game1.options.actionButton);

    void StartGenericUiFrame(string operation, int id, string? value, bool watchdog = false)
    {
        var slot = this.commandInbox.ExecutionSlot;
        var surface = this.CaptureReceiverFacts();
        long epoch = surface.Epoch;
        string? entry = watchdog ? this.NativeProgressionEntry(surface.Top?.Instance as IClickableMenu) : null;
        var pin = this.CurrentCommandIdentity();
        if (watchdog && !slot.TryAdmitProgression(new MenuStepCommandRequest {
            RuntimeInstanceId = this.runtimeInstanceId, SessionId = pin.SessionId, ExpectedPlayerId = pin.PlayerId,
            ExpectedScreenId = pin.ScreenId, ExpectedLocationId = pin.LocationId },
            this.actionOwner, surface, entry, this.occurrenceRing!)) { this.uiWatchdog.AdvanceUndelivered("slot_admission"); return; }
        var attempt = slot.ActiveAttempt ?? throw new InvalidOperationException("ui_motor_unadmitted");
        if (entry == "e" && surface.Top?.Instance is ItemListMenu list) this.koLists.GetValue(list, _ => new()).Admitted = true;
        try
        {
            this.PrepareGenericUiFrame(operation, id, value, watchdog);
            if (this.uiMotor is not { } lease) return;
            lease.Attempt = attempt; lease.Epoch = epoch;
            lease.Request = watchdog ? null : attempt.Request;
            if (!slot.AttachClient(attempt.Request, lease, reason => {
                lease.Fault = reason; this.FinishGenericUiFrame();
            }, busy: () => ReferenceEquals(this.uiMotor, lease)))
            { lease.Fault = "ui_motor_unadmitted"; this.FinishGenericUiFrame(); }
        }
        finally { if (watchdog && this.uiMotor is null && ReferenceEquals(slot.ActiveAttempt, attempt)) {
            slot.Neutralize(NowUnixMs()); } }
    }

    void PrepareGenericUiFrame(string operation, int id, string? value, bool watchdog)
    {
        if (this.uiMotor is not null) throw new InvalidOperationException("ui_motor_busy");
        GenericGraph graph = GenericMenuSurface is { } top ? GenericInspect(top) : GenericInspectHud();
        GenericWidget? widget = graph.Widgets.SingleOrDefault(w => w.Id == id);
        bool close = operation == "close";
        if (NativeJunimoUi.Unreadable(graph.Dispatch)
            && (!close || !JunimoNoteMenu.canClick || UiPadReadyToClose(graph.Dispatch) != true))
            throw new InvalidOperationException("unreadable_menu_close_only");
        // PM-1 fix11b #2: an essential grant menu holding her items is never exited by any gesture (her own or a
        // recovery/watchdog path); vanilla OK/close discards them. Taking/swapping/dropping stays available.
        if (GrantOf(graph) is not null && GenericUiGrant.ExitGesture(operation, widget?.Path, widget?.Kind))
            throw new InvalidOperationException("item_policy:" + GenericUiGrant.CloseGate);
        if (close && GenericInputOwner is null) return; // nonmodal HUD close must never open a menu
        bool minigameKey = false;
        if (close && GenericMenuSurface is null && Game1.currentMinigame is { } game)
        {
            // Same contract as vanilla's abort path: false retains the minigame and all ownership.
            bool accepted = game.forceQuit();
            if (accepted && ReferenceEquals(Game1.currentMinigame, game)) Game1.currentMinigame = null;
            // ADR 246: when the minigame refuses its abort, her way out is the key a player presses to leave it
            // (Escape), delivered to the minigame itself by this frame; the minigame's own tick ends it.
            string exit = MinigameExit.After(accepted, ReferenceEquals(Game1.currentMinigame, game));
            this.PublishUiMotorFact("minigame_exit", exit == MinigameExit.Closed ? "Closed" : "Blocked",
                "forceQuit=" + accepted + (exit == MinigameExit.Closed ? "" : "; escape_key_to_minigame"));
            if (exit == MinigameExit.Closed) return;
            minigameKey = true;
        }
        if (operation == "key_skip" && Game1.CurrentEvent is not { skippable: true, skipped: false })
            throw new InvalidOperationException("not_skippable");
        if (close)
        {
            if (this.ReadUiPadFacts().HeldItemId is not null) throw new InvalidOperationException("cursor_item");
            if (GenericRequiredChoice(graph)) throw new InvalidOperationException("NeedsChoice");
            // A reflected X is not necessarily drawn or handled (R: shipping bin's inherited X).
            // Closing is the menu's configured native exit gesture; an explicit component press
            // remains a pointer press and is judged independently by its observed effect.
            widget = graph.Dispatch is ReadyCheckDialog ready
                ? ready.isCancelable() ? graph.Widgets.Single(w => ReferenceEquals(w.Component, ready.cancelButton))
                    : throw new InvalidOperationException("ready_check_not_cancelable")
                : null;
        }
        bool key = operation.StartsWith("key_", StringComparison.Ordinal) || close && widget is null;
        if (!key && widget is null && operation != "scroll") throw new InvalidOperationException("component_unavailable");
        Point point = widget?.Bounds.Center ?? Point.Zero;
        var pin = this.CurrentCommandIdentity();
        this.uiMotor = new UiMotorLease { Frames = new GenericUiFrames(value?.StartsWith("ticks:") == true
                ? int.Parse(value[6..]) : 1), Owner = GenericInputOwner, Session = pin.SessionId, Player = pin.PlayerId,
            Location = pin.LocationId, Day = Game1.Date.TotalDays,
            Before = this.ReadUiPadFacts(), Operation = operation, Point = new Point((int)(point.X * Game1.options.uiScale),
                (int)(point.Y * Game1.options.uiScale)), Pointer = !key && operation != "scroll",
            Right = operation.EndsWith("right", StringComparison.Ordinal), Shift = operation.Contains("shift_", StringComparison.Ordinal), Control = operation.StartsWith("control_", StringComparison.Ordinal),
            Pad = operation == "key_LB" ? Buttons.LeftShoulder : operation == "key_RB" ? Buttons.RightShoulder : null,
            Key = key ? operation is "key_LB" or "key_RB" ? Keys.None
                : operation == "key_use" ? Game1.options.getFirstKeyboardKeyFromInputButtonList(Game1.options.useToolButton)
                : operation == "key_confirm" ? GenericConfirmKey : operation == "key_skip"
                ? Game1.options.getFirstKeyboardKeyFromInputButtonList(Game1.options.journalButton)
                : minigameKey ? Keys.Escape : operation == "key_menu" || close ? GenericMenuKey : Keys.Escape : Keys.None,
            Wheel = operation == "scroll" ? int.Parse(value!) : 0, Watchdog = watchdog, ComponentId = widget?.Id ?? -1,
            Minigame = GenericMenuSurface is null ? Game1.currentMinigame : null,
            // Review W1b S2: the owner's wheel is sampled when her press begins, so a wheel step before her first
            // seated update is named too (not only from the second comparable sample). Behind, the first active one is.
            OwnerWheel = Game1.game1?.IsActive == true ? Game1.input.GetMouseState().ScrollWheelValue : null };
        this.uiMotor.Succession = new GenericUiSuccession(this.uiMotor.Owner, this.uiMotor.Before);
        if (close) this.uiMotor.Locks = GenericMenuLocks(graph);
    }

    static bool GenericRequiredChoice(GenericGraph graph) => graph.Dispatch is not null && GenericFields(graph.Dispatch.GetType())
        .Any(f => (f.Name is "isQuestion" or "isProfessionChooser" or "isChoosingProfession") && f.GetValue(graph.Dispatch) is true);

    static List<NetMutex> GenericMenuLocks(GenericGraph graph)
    {
        var locks = new HashSet<NetMutex>(ReferenceEqualityComparer.Instance);
        if (graph.Dispatch is JunimoNoteMenu { scrambledText: true } note
            && NativeB1Offers.NoteMutex(Game1.currentLocation, note) is { } noteMutex)
            locks.Add(noteMutex);
        foreach (var menu in new[] { graph.Root, graph.Dispatch }.Where(m => m is not null).Distinct())
            foreach (var field in GenericFields(menu.GetType()))
            {
                object? value = field.GetValue(menu);
                if (value is NetMutex mutex) locks.Add(mutex);
                else if (value is not null && value.GetType().Assembly == typeof(Game1).Assembly)
                {
                    // A native context may expose a shared lock rather than its own field.
                    var getter = value.GetType().GetMethod("GetMutex", Type.EmptyTypes);
                    if (getter?.ReturnType == typeof(NetMutex) && getter.Invoke(value, null) is NetMutex contextual)
                        locks.Add(contextual);
                }
            }
        return locks.Where(m => m.IsLockHeld()).ToList();
    }

    bool UiMotorBound(UiMotorLease lease) => Context.IsWorldReady && !Context.IsMainPlayer && !Context.IsSplitScreen
        && this.UiPadReady // ADR 244: a direct menu call never asks about window focus
        && this.CurrentCommandIdentity().SessionId == lease.Session && Game1.player.UniqueMultiplayerID == lease.Player
        && this.CurrentCommandIdentity().LocationId == lease.Location && Game1.Date.TotalDays == lease.Day;

    bool UiMotorValid(UiMotorLease lease) => this.commandInbox.ExecutionSlot.CanContinue(lease.Attempt, "single_edge",
        new ValidityFacts(this.UiMotorBound(lease), this.CaptureReceiverFacts().Epoch,
            ReferenceEquals(GenericInputOwner, lease.Owner) && this.CaptureReceiverFacts().Epoch == lease.Epoch,
            WithinBudget: lease.Deadline.ElapsedMilliseconds <= 4000))
        && (!lease.Watchdog || this.actionOwner.ProgressionAllowed && (lease.Delivered
            || this.CaptureReceiverFacts().Authority.Holder == "progression"
            && this.NativeProgressionEntry(this.CaptureReceiverFacts().Top?.Instance as IClickableMenu) == lease.Attempt.ProgressionEntry));

    void EndUiMotorConsumption(UiMotorLease lease)
    {
        if (this.UiMotorValid(lease)) return;
        lease.EndedBy = "foreign_epoch";
        if (lease.Succession.Successors.Count == 0) lease.Fault ??= "ui_receiver_changed_outside_input";
        this.FinishGenericUiFrame(); // restore locally; never release a button into the successor
    }

    void BeginGenericUiFrame()
    {
        // Always restore a previous substitution first, including an interrupted game update.
        this.RestoreGenericUiFrame();
        if (this.uiMotor is not { } lease) return;
        if (lease.Fault is not null) { this.FinishGenericUiFrame(); return; }
        bool settlingClose = lease.SettlingUnreadableClose;
        if (!(settlingClose ? this.UiMotorBound(lease) : this.UiMotorValid(lease))
            || !lease.Watchdog && this.actionOwner.StopLatched || this.actionOwner.RecordFaulted || !lease.Watchdog && this.actionOwner.ReleaseRequested
            || !lease.Watchdog && NowUnixMs() > this.actionOwner.LeaseExpiresUnixMs
            || !settlingClose && lease.Deadline.ElapsedMilliseconds > 4000)
        { lease.Fault = "ui_frame_cancelled"; this.FinishGenericUiFrame(); return; }
        if (settlingClose) { this.FinishGenericUiFrame(); return; }
        if (Game1.paused || Game1.HostPaused) return;
        // ADR 250 review B4: a frame is staged only on a path that consumes it once, in the window state it is staged in.
        // Checked at every frame, so a focus change in the middle of her press is caught before the next frame.
        if (UiMotorFrontPath.FrameRefusal(UiMotorInputSeatPatch.Installed, UiMotorMenuUpdatePatch.Installed,
                Game1.game1?.IsActive == true, lease.Operation) is { } unseatable)
        { lease.Fault = unseatable; this.FinishGenericUiFrame(); return; }
        // ADR 250 review S1: her press does not start under the owner's gesture in her window (a held key, button or drag).
        // The owner's gesture ends natively; her press is refused by name with nothing staged.
        if (lease.UpdateFrames == 0 && !lease.Frames.Pressed && !lease.Frames.WasPressed && Game1.game1?.IsActive == true
            && UiMotorFrontPath.OwnerInput(BackgroundInputNormalizer.Normalize(Game1.input.GetKeyboardState(),
                Game1.input.GetMouseState(), Game1.input.GetGamePadState(), padPath: false)) is { } ownerHeld)
        { lease.Fault = UiMotorFrontPath.OwnerHeldPrefix + ownerHeld; this.FinishGenericUiFrame(); return; }
        if (!lease.Succession.Matches(GenericInputOwner, this.ReadUiPadFacts()))
        {
            lease.Fault = "ui_receiver_changed_outside_input";
            this.FinishGenericUiFrame(); return;
        }
        if (!ReferenceEquals(GenericInputOwner, lease.Owner))
        { this.FinishGenericUiFrame(); return; } // never deliver the remaining hold/release to a successor
        bool pressed = lease.Frames.Pressed, prior = lease.Frames.WasPressed;
        if (pressed && !prior && lease.ComponentId >= 0)
        {
            UiPadComponent? original = lease.Before.Comps.SingleOrDefault(c => c.Id == lease.ComponentId);
            UiPadComponent? current = this.ReadUiPadFacts().Comps.SingleOrDefault(c => c.Id == lease.ComponentId);
            if (original != current)
            { lease.Fault = "stale_component"; this.FinishGenericUiFrame(); return; }
        }
        Keys[] KeysFor(bool down) => down ? new[] { lease.Key, lease.Shift ? Keys.LeftShift : Keys.None, lease.Control ? Keys.LeftControl : Keys.None }
            .Where(k => k != Keys.None).ToArray() : Array.Empty<Keys>();
        if (!this.padPinResolved) this.ResolvePadPin();
        bool hostConnected = Game1.input.GetGamePadState().IsConnected;
        IReflectedField<bool>? held = this.padPinField;
        bool HeldConnection() => GenericUiPadLink.Connected(held?.GetValue(), hostConnected);
        // ADR 250 review B3: a press aimed at a running minigame is delivered once, by the direct call. Its frames carry no
        // mouse button, and the key reads as already held on the pressed frame, so vanilla's minigame loop (key edges, then
        // tick) never sees a new edge to deliver a second time, in either window state.
        bool toMinigame = lease.Minigame is not null;
        var input = new UiMotorInput(lease.Point, pressed && lease.Pointer && !lease.Right && !toMinigame,
            pressed && lease.Pointer && lease.Right && !toMinigame, KeysFor(pressed), pressed ? lease.Wheel : 0,
            button: lease.Pad is { } button ? pressed ? button : (Buttons)0 : null) { Connected = HeldConnection };
        var history = new UiMotorInput(lease.Point, prior && lease.Pointer && !lease.Right && !toMinigame,
            prior && lease.Pointer && lease.Right && !toMinigame, KeysFor(UiMotorFrontPath.HistoryHoldsKey(prior, pressed, toMinigame)).Concat(pressed
                ? new[] { lease.Shift ? Keys.LeftShift : Keys.None, lease.Control ? Keys.LeftControl : Keys.None }.Where(k => k != Keys.None)
                : Array.Empty<Keys>()).Distinct().ToArray(),
            button: lease.Pad is { } oldButton ? prior ? oldButton : (Buttons)0 : null) { Connected = HeldConnection };
        // Staged only: the slot stays SMAPI's through the rest of UpdateTicking and its fold-in.
        this.uiMotorSeat.Stage(input, (history.GetMouseState(), history.GetKeyboardState(), history.GetGamePadState()));
        try
        {
            // Inactive vanilla updateActiveMenu updates polled controls but returns before callbacks.
            // Supply that dispatch once at the edge; the normal game update consumes the SAME frame
            // (seated by the Game1.Update prefix). No menu-specific adapters and no manual update call.
            if (pressed && !prior)
            {
                if (!lease.Succession.Begin(GenericInputOwner, this.ReadUiPadFacts()))
                { lease.Fault = "ui_receiver_changed_outside_input"; this.FinishGenericUiFrame(); return; }
                this.uiMotorSeat.Seat(); // callbacks read Game1.input/old* synchronously
                this.commandInbox.ExecutionSlot.Bracket(() => {
                    try { this.DispatchGenericUiEdge(lease); lease.Attempt.Delivered |= lease.Delivered; }
                    finally { this.UnseatGenericUiFrame(); }
                });
                if (!ReferenceEquals(this.uiMotor, lease)) return;
                if (lease.Fault is null && !lease.Succession.End(GenericInputOwner,
                        this.ReadGenericUiReadback(lease.Before), lease.Delivered, true, "edge", "press"))
                    lease.Fault = "ui_receiver_changed_outside_input";
                this.EndUiMotorConsumption(lease);
                if (!ReferenceEquals(this.uiMotor, lease)) return;
            }
            if (!ReferenceEquals(GenericInputOwner, lease.Owner) || lease.Succession.Successors.Count > 0)
                this.FinishGenericUiFrame();
        }
        catch (Exception ex) { lease.Fault = "dispatch_fault:" + ex.GetType().Name; this.FinishGenericUiFrame(); }
    }
    void DispatchGenericUiEdge(UiMotorLease lease)
    {
        var current = this.ReadUiPadFacts();
        if (current.ReceiverToken != lease.Before.ReceiverToken)
            throw new InvalidOperationException("ui_receiver_changed");
        if (lease.Pad is null && !GenericUiSafety.CurrentTarget(lease.Before, current))
        {
            lease.Fault = "stale_component";
            this.FinishGenericUiFrame();
            return; // semantic state changed after admission; no edge was delivered
        }
        if (CursorItemSettle.ItemOperation(lease.Operation) is not null)
        {
            var expected = lease.Before.CursorSettle?.Destinations.SingleOrDefault(d => d.Id == lease.ComponentId);
            var actual = current.CursorSettle?.Destinations.SingleOrDefault(d => d.Id == lease.ComponentId);
            if (expected is null || actual?.Token != expected.Token || current.CursorSettle?.Cursor != lease.Before.CursorSettle?.Cursor)
                throw new InvalidOperationException("cursor_destination_changed");
        }
        if (this.ValidateGenericItemPolicyEdge(lease.ComponentId, lease.Operation) is { } itemPolicy)
        {
            lease.Fault = "item_policy:" + itemPolicy;
            this.FinishGenericUiFrame();
            return; // no callback or game update has consumed the installed frame
        }
        if (lease.Operation == "key_skip")
        {
            if (Game1.CurrentEvent is not { skippable: true, skipped: false } ev || current.HeldItemId is not null)
                throw new InvalidOperationException("not_skippable");
            lease.Delivered = true;
            ev.skipped = true;
            ev.skipEvent();
            return;
        }
        if (GenericMenuSurface is { } root)
        {
            // These native owner channels dispatch callbacks while inactive too. They consume
            // the installed frame themselves; dispatching here would double-submit input.
            // W1 B3: these consume the frame in the game's own update, so without the seat hook nothing can consume it
            // (named, not delivered), and "delivered" waits for a seated update (EnterUiMotorGameUpdate).
            if (ReferenceEquals(root, Game1.textEntry) || ReferenceEquals(root, Game1.emoteMenu))
            {
                if (!UiMotorInputSeatPatch.Installed) { lease.Fault = UiMotorFrontPath.SeatUnavailable; this.FinishGenericUiFrame(); return; }
                lease.PolledReceiver = true; return;
            }
            GenericGraph graph = GenericInspect(root);
            var widget = graph.Widgets.SingleOrDefault(w => w.Id == lease.ComponentId);
            if (lease.Operation == "settle" && this.ValidateCursorItemSettleEdge(lease.ComponentId) is { } refused)
                throw new InvalidOperationException(refused);
            if ((CursorItemSettle.ItemOperation(lease.Operation) is "drop" or "trash" || widget is not null && CursorWidgetKind(widget) is "drop" or "trash")
                && this.ValidateItemDisposeEdge(lease.ComponentId) is { } disposeRefusal)
                throw new InvalidOperationException(disposeRefusal);
            IClickableMenu menu = widget?.Dispatch ?? graph.Dispatch;
            if (menu is LevelUpMenu { isProfessionChooser: true }) // the profession click is polled in its update (W1 B3)
            {
                if (!UiMotorInputSeatPatch.Installed) { lease.Fault = UiMotorFrontPath.SeatUnavailable; this.FinishGenericUiFrame(); return; }
                lease.PolledReceiver = true; return;
            }
            var trade = this.BeginNativeTrade(widget, menu);
            Game1.PushUIMode();
            try
            {
                var point = widget?.Bounds.Center ?? Point.Zero;
                lease.Delivered = true;
                if (lease.Operation == "scroll") menu.receiveScrollWheelAction(lease.Wheel);
                else if (lease.Pointer)
                {
                    menu.performHoverAction(point.X, point.Y);
                    if (lease.Right) menu.receiveRightClick(point.X, point.Y); else menu.receiveLeftClick(point.X, point.Y);
                }
                else menu.receiveKeyPress(lease.Key);
            }
            finally {
                try { Game1.PopUIMode(); }
                finally { this.EndNativeTrade(trade, lease); }
            }
            this.ObserveSleepAnswer(menu, current.UiKey?.QuestionKey);
        }
        else if (Game1.currentMinigame is { } game) { lease.Delivered = true; game.receiveKeyPress(lease.Key); }
        else if (lease.Operation == "key_confirm" && Game1.CurrentEvent is not null)
        { lease.Delivered = true; Game1.pressActionButton(Game1.input.GetKeyboardState(), Game1.input.GetMouseState(), default); }
    }
    void UnseatGenericUiFrame()
    {
        if (this.uiMotorSeat.Unseat() == GenericUiInputSeat<InputState, (MouseState, KeyboardState, GamePadState)>.Release.OwnershipLost
            && this.uiMotor is { } lease) lease.Fault = "input_layer_ownership_lost";
    }
    void RestoreGenericUiFrame()
    {
        if (this.uiMotorSeat.Clear() == GenericUiInputSeat<InputState, (MouseState, KeyboardState, GamePadState)>.Release.OwnershipLost
            && this.uiMotor is { } lease) lease.Fault = "input_layer_ownership_lost";
    }
    void EndGenericUiFrame()
    {
        bool consumed = this.uiMotorSeat.Staged;
        this.RestoreGenericUiFrame();
        if (!consumed || this.uiMotor is not { } lease) return;
        // The finalizer witnessed the actual update, not the intervening UpdateTicked handlers.
        if (!lease.Succession.Matches(GenericInputOwner, this.ReadUiPadFacts()))
            lease.Fault ??= "ui_receiver_changed_outside_input";
        lease.Frames.Consume();
        if (lease.Fault is not null || lease.Frames.Done || lease.Succession.Successors.Count > 0) this.FinishGenericUiFrame();
    }
    void FinishGenericUiFrame()
    {
        this.RestoreGenericUiFrame();
        if (this.uiMotor is not { } lease) return;
        if (lease.Fault is null && lease.SettlingUnreadableClose)
        {
            var (waiting, fault) = GenericUiReceipt.SettleWorldClose(
                lease.Succession.Successors.Count > 0
                    && lease.Succession.Successors[^1] is { result_menu: null, effect: "closed" },
                GenericInputOwner is not null || Game1.eventUp || Game1.CurrentEvent is not null || Game1.farmEvent is not null,
                lease.HoldsMutex, Context.IsPlayerFree && Context.CanPlayerMove,
                lease.Deadline.ElapsedMilliseconds);
            if (waiting) return;
            lease.Fault = fault;
        }
        else if (lease.Fault is null && !ReferenceEquals(GenericInputOwner, lease.Owner)
            && lease.Locks.Any(m => m.IsLockHeld()))
        {
            if (lease.Deadline.ElapsedMilliseconds < 4000) return;
            lease.Fault = "mutex_release_unverified";
        }
        this.uiMotor = null;
        UiPadVerdict verdict;
        // ADR 250 review B4/S1: a refusal taken before any edge (stale, item policy, an owner gesture, a missing hook) is the
        // named not-delivered receipt. Review S3: an Escape aimed at a minigame is judged by that pinned minigame alone.
        try { verdict = GenericUiReceipt.NotDelivered(lease.Fault, lease.Delivered)
            ? GenericUiReceipt.StaleComponent(this.ReadUiPadFacts(), lease.Fault!)
            : lease.Minigame is { } pinned && lease.Fault is null && lease.Key == Keys.Escape
            ? GenericUiReceipt.MinigameEscape(MinigameExit.Receipt(Game1.currentMinigame is not null,
                ReferenceEquals(Game1.currentMinigame, pinned)), lease.Before, this.ReadGenericUiReadback(lease.Before), lease.Delivered)
            : GenericUiReceipt.Judge(lease.Before, this.ReadGenericUiReadback(lease.Before), lease.Operation, true, lease.Fault,
                lease.ComponentId, lease.Delivered, lease.Succession.Successors); }
        catch { verdict = new UiPadVerdict(UiPadVerdict.PressUncertain, false, null, Array.Empty<string>(), "readback_fault", null, null, null); }
        if (verdict.Uncertain) this.uiPadUncertainReceiver = lease.Before.ReceiverToken;
        if (lease.Watchdog) {
            if (!lease.Delivered) this.uiWatchdog.AdvanceUndelivered("motor:" + (lease.Fault ?? "not_delivered"));
            lease.Attempt.ProgressionAccepted = verdict.Applied && !verdict.Uncertain;
            if (verdict.Uncertain) {
                uiMotorFault = new(lease.Fault == "input_layer_ownership_lost" ? ProgressionClient.FaultKind.InputOwnershipLost
                    : lease.Locks.Any(m => m.IsLockHeld()) ? ProgressionClient.FaultKind.MutexHeld : ProgressionClient.FaultKind.Uncertain, lease.Epoch);
                uiFaultLocks = lease.Locks;
                lease.Attempt.ProgressionFault = uiMotorFault.Kind.ToString();
            }
            this.commandInbox.ExecutionSlot.Neutralize(NowUnixMs());
        }
        this.PublishUiMotorFact(lease.Operation, verdict.Uncertain ? "Uncertain" : verdict.Effect == "closed" ? "Closed"
            : verdict.Effect == "cursor_settled" ? "Settled" : verdict.Applied ? "Advanced" : "Blocked",
            JsonSerializer.Serialize(new { update_frames = lease.UpdateFrames, fault = lease.Fault,
                ended_by = lease.EndedBy,
                // ADR 250: cumulative menu updates that took the inactive branch for her frame, and those in front.
                inactive_branch_frames = UiMotorMenuUpdatePatch.InactiveFrames, front_frames = UiMotorMenuUpdatePatch.FrontFrames,
                owner_input = lease.OwnerInput, // review S1: the owner's input in her window during her seated frames
                minigame = lease.Minigame?.GetType().Name,
                // PM-1 fix14d: a "barrier" fault (a21 J202) now names the slot's own neutralize cause (e.g. tick_lapse).
                release_via = lease.Attempt?.ReleaseVia, release_lapse = lease.Attempt?.ReleaseLapse,
                succession = lease.Succession.Successors, receipt = verdict.Readback ?? verdict.Reason, observed_locks = lease.Locks.Count,
                locks_released = lease.Locks.All(m => !m.IsLockHeld()) }));
        if (lease.OwnerInput.Count > 0) // ADR 250 review S1, the same name ADR 244 gives the owner's input in the world
            this.Monitor.Log("UI_MOTOR " + BgForeignInput.Prefix + " not delivered during her seated frames: "
                + string.Join(" ", lease.OwnerInput) + " op=" + lease.Operation, LogLevel.Info);
        if (lease.Request is { } request && this.commandInbox.HasInflight && this.commandInbox.InflightRequestId == request.RequestId)
        {
            if (request.UiKeyTarget is not null && GenericUiReceipt.UiKeyNotDelivered(lease.Fault, lease.Delivered) is { } zeroPress) {
                // Review W1b should-fix: the seat was missing or refused, nothing was invoked: a named zero-press refusal.
                this.Monitor.Log($"ui_key REJECT request_id={request.RequestId} reason={zeroPress} (no edge reached the game)", LogLevel.Warn);
                this.WriteCommandResult(this.commandInbox.CompleteRejected(request, CommandRejectReason.ArmRejected,
                    this.CurrentCommandIdentity(), NowUnixMs(), zeroPress));
            } else if (request.UiKeyTarget is { } target) {
                bool selected = lease.Fault is null && lease.Attempt.AnswerWitness?.Profession == target.ProfessionId;
                this.WriteCommandResult(this.commandInbox.CompleteUiKey(new(selected ? UiKeyVerdict.OutcomeApplied : UiKeyVerdict.OutcomeUncertain,
                    selected, selected ? "profession_selected" : null, lease.Delivered ? 1 : 0, true, "NativeUpdate",
                    verdict.ResultReceiverToken, verdict.ResultMenuType, target.ProfessionId, verdict.Reason),
                    this.CurrentCommandIdentity(), NowUnixMs(), this.CurrentCommandIdentity().ContextGeneration));
            } else this.FinishUiPad(request, verdict, lease.Before.ReceiverToken!);
        }
    }
    void PublishUiMotorFact(string action, string outcome, string detail)
    {
        var pin = this.CurrentCommandIdentity();
        this.uiMotorFact = new Dictionary<string, object> { ["seq"] = ++this.uiMotorFactSeq,
            ["runtime_instance_id"] = this.runtimeInstanceId, ["session_id"] = pin.SessionId,
            ["player_id"] = pin.PlayerId, ["screen_id"] = Context.ScreenId, ["sample_tick"] = Game1.ticks,
            ["action"] = action, ["outcome"] = outcome, ["detail"] = UiPadTrim(detail, 3000) };
        this.Monitor.Log("UI_MOTOR " + JsonSerializer.Serialize(this.uiMotorFact), LogLevel.Info);
    }

    void EnrichUiMotorContext(Dictionary<string, object> snapshot)
    {
        snapshot["captured_tick"] = this.commandInbox.ExecutionSlot.ModTick;
        snapshot["rev"] = new { frame = Game1.ticks };
        snapshot["native"] = this.NativeObserverFacts();
        var pin = this.CurrentCommandIdentity();
        var owner = GenericInputOwner;
        var control = CapturePlayerControlDiagnostics();
        snapshot["ui_motor"] = new Dictionary<string, object> {
            ["session_id"] = pin.SessionId, ["runtime_instance_id"] = this.runtimeInstanceId,
            ["player_id"] = pin.PlayerId, ["screen_id"] = Context.ScreenId, ["sample_tick"] = Game1.ticks,
            ["control_taken"] = ControlHandoff.Reason(control) ?? "",
            ["event_id"] = control.EventId ?? control.LocationEventId ?? "", ["event_command"] = control.EventCommand ?? -1,
            ["location"] = pin.LocationId,
            ["owner"] = owner?.GetType().Name ?? "free", ["pending"] = this.uiMotor is not null,
            ["unsafe"] = this.UiMotorUnsafe,
            ["surface"] = GenericMenuSurface?.GetType().Name ?? "HUD",
            ["active_menu"] = Game1.activeClickableMenu?.GetType().Name ?? "",
            ["surface_receiver"] = this.ReadUiPadFacts().ReceiverToken ?? "",
            ["cursor_only"] = GenericMenuSurface is null && Game1.currentMinigame is null && Game1.CurrentEvent is null
                && Game1.player.CursorSlotItem is not null,
            ["render_age_ms"] = this.uiMotorLastRender == 0 ? -1 : Environment.TickCount64 - this.uiMotorLastRender,
            ["event_player_control"] = Game1.CurrentEvent?.playerControlSequence == true,
            ["event_skippable"] = Game1.CurrentEvent is { skippable: true, skipped: false },
            ["last_recovery"] = this.uiMotorFact is { } fact && (string)fact["session_id"] == pin.SessionId
                && (long)fact["player_id"] == pin.PlayerId ? fact : new Dictionary<string, object>(),
        };
    }
    void TickGenericUiWatchdog()
    {
        if (!this.config.UiPadMenusEnabled || !this.UiPadReady || this.UiMotorUnsafe || !this.actionOwner.ProgressionAllowed
            || this.uiMotor is not null || this.uiPadSettle is not null || this.uiKeySettle is not null
            || this.MutationInflight)
        {
            if (Environment.TickCount64 >= this.progressionHeldNextPoll) this.NoteProgressionHeld(this.WatchdogEarlyGate());
            return;
        }
        long now = Environment.TickCount64;
        if (now < this.uiWatchdogNextPoll) return;
        this.uiWatchdogNextPoll = now + 250;
        try
        {
            var facts = this.ReadUiPadFacts();
            if (facts.ReceiverToken == this.uiPadUncertainReceiver || facts.ReceiverToken == this.uiKeyUncertainReceiver)
            {
                this.NoteProgressionHeld("receiver_uncertain");
                return;
            }
            var surface = this.CaptureReceiverFacts();
            string? entry = this.NativeProgressionEntry(surface.Top?.Instance as IClickableMenu);
            if (entry is null || surface.Authority.Holder != "progression" || this.occurrenceRing is null
                || surface.Top?.Instance is ItemListMenu lost && this.koLists.TryGetValue(lost, out var k) && k.Admitted)
            {
                if (entry is not null)
                    this.NoteProgressionHeld(surface.Authority.Holder != "progression" ? "authority_holder:" + surface.Authority.Holder
                        : this.occurrenceRing is null ? "occurrence_ring_missing" : "ko_list_admitted");
                return;
            }
            string binding = this.GenericUiWatchdogBinding(facts.ReceiverToken);
            var graph = GenericMenuSurface is { } current ? GenericInspect(current) : GenericInspectHud();
            var safety = this.ReadGenericUiSafety(graph, facts.HeldItemId is null ? null : this.ReadCursorItem(graph, out _));
            if (entry == "b" && graph.Dispatch is LetterViewerMenu letter) safety = safety with {
                AdvanceOnly = true, Ready = letter.readyToClose() && letter.scale >= 1,
                AdvanceId = graph.Widgets.Single(w => w.Kind == "close" && GenericWidgetVisible(w)).Id };
            if (entry == "e") safety = safety with { AdvanceOnly = true, UnclaimedItems = false,
                AdvanceId = graph.Widgets.Single(w => w.Path.EndsWith(".okButton", StringComparison.Ordinal)).Id };
            safety = safety with { CursorItem = facts.HeldItemId is not null };
            bool eligible = GenericInputOwner is not null && !Game1.paused && !Game1.HostPaused
                && GenericUiSafety.Eligible(Game1.globalFade || Game1.fadeToBlack, Game1.isWarping,
                    Game1.locationRequest is not null, safety);
            bool leased = this.actionOwner.IsArmed && NowUnixMs() <= this.actionOwner.LeaseExpiresUnixMs;
            string progress = UiPadToken.StateDigest(facts); // layout/text/item changes are progress too
            // Prose and a proven informational OK are system progression, not
            // choices. Attendance/repeated model waits cannot postpone the one
            // edge on this semantic page. Active decisions/input still own it.
            if (this.uiWatchdog.AdvanceDue(binding, progress, now,
                    GenericUiSafety.State(safety) is "ADVANCE_ONLY" or "NIGHT_SUMMARY", eligible, leased,
                    entry is "c" or "d" or "e", ProgressionClient.Dwell(entry, facts.Text?.Length ?? 0),
                    scripted: entry == "a" && Game1.CurrentEvent is not null)
                && (GenericUiSafety.NightOperation(safety) ?? GenericUiSafety.Operation(safety, 0)) is { } advance)
            {
                this.uiWatchdog.AdvanceAttempt();
                this.PublishUiMotorFact("watchdog_advance", "Advance", "Ready advance-only native page; one observed input, then re-inspect.");
                this.StartGenericUiFrame(advance, safety.AdvanceId, null, watchdog: true);
                return;
            }
            this.NoteProgressionHeld(this.ProgressionNotDueGate(safety, eligible, leased, binding, now,
                night: entry is "c" or "d" or "e", scripted: entry == "a" && Game1.CurrentEvent is not null));
        }
        catch (Exception ex) { this.PublishUiMotorFact("watchdog", "Blocked", ex.GetType().Name + ":" + ex.Message); }
    }

    /// <summary>ADR 241 C4 (bridge lead 2026-11-19): a held state names its own cause. When a native progression page
    /// (entry a-e) has been up longer than its dwell plus <see cref="ProgressionHeld.GraceMs"/> and the watchdog has not
    /// advanced it, ONE fact (with its UI_MOTOR log line) names the gate that holds it: once per menu occurrence, and
    /// again only when the gate changes (<see cref="ProgressionHeld.Note"/>). Read-only: it never changes what the
    /// watchdog does, and it never throws.</summary>
    void NoteProgressionHeld(string? gate)
    {
        long now = Environment.TickCount64;
        if (now < this.progressionHeldNextPoll) return;
        this.progressionHeldNextPoll = now + 1000;
        try
        {
            var menu = GenericMenuSurface;
            string? entry = this.NativeProgressionEntry(menu);
            // Review R5 S5: the age is the watchdog's own semantic page (a box that advances in place starts a new page),
            // and the wait is the dwell the watchdog applied to that page, not a fixed 1,000-character one.
            string? occurrence = menu is null || entry is null ? null
                : entry + ":" + System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(menu) + ":" + this.uiWatchdog.PageKey;
            long dwell = entry is null ? 0 : this.uiWatchdog.LastDwellMs > 0 ? this.uiWatchdog.LastDwellMs : ProgressionClient.Dwell(entry, 1000);
            string? said = this.progressionHeld.Note(occurrence, dwell, gate, now);
            if (said is not null)
                this.PublishUiMotorFact("progression_held", "Held",
                    $"entry={entry} menu={menu!.GetType().Name} held_ms={this.progressionHeld.HeldMs(now)} gate={said}");
        }
        catch (Exception) { }  // a report must never touch the watchdog
    }

    /// <summary>ADR 241 C4: the first closed gate of the watchdog's early return (<see cref="ProgressionHeld.EarlyGate"/>).</summary>
    string? WatchdogEarlyGate() => ProgressionHeld.EarlyGate(this.config.UiPadMenusEnabled, this.UiPadReady,
        this.UiMotorUnsafe, this.actionOwner.ProgressionAllowed, this.actionOwner.RecordFaulted,
        this.actionOwner.HaltFact?["scope"]?.ToString(), this.uiMotor is { Watchdog: true });

    /// <summary>ADR 241 C4: why AdvanceDue did not fire on a page the watchdog reached, read without calling it again
    /// (it keeps state; <see cref="ProgressionHeld.NotDueGate"/>).</summary>
    string? ProgressionNotDueGate(GenericUiSafetyFacts safety, bool eligible, bool leased, string binding, long now,
        bool night, bool scripted) =>
        ProgressionHeld.NotDueGate(GenericUiSafety.State(safety), eligible ? null : IneligibleWhy(),
            this.uiWatchdog.Attempted, leased, night, scripted, this.uiWatchdog.Deciding(binding, now),
            (GenericUiSafety.NightOperation(safety) ?? GenericUiSafety.Operation(safety, 0)) is not null,
            this.uiWatchdog.UndeliveredGate); // review R5 S4

    /// <summary>ADR 241 C4: which part of the watchdog's eligibility is false, in its own order.</summary>
    static string IneligibleWhy() =>
        GenericInputOwner is null ? "no_input_owner" : Game1.paused ? "game_paused" : Game1.HostPaused ? "host_paused"
        : Game1.isWarping ? "warping" : Game1.locationRequest is not null ? "location_request"
        : Game1.globalFade || Game1.fadeToBlack ? "fade" : "not_eligible";
    string GenericUiWatchdogBinding(string? receiver) => this.CurrentCommandIdentity().SessionId + ":" + receiver;
    void AbortGenericUiMotor(string reason)
    {
        if (this.uiMotor is { } lease) { lease.Fault = reason; this.FinishGenericUiFrame(); }
        else this.RestoreGenericUiFrame();
    }
}
