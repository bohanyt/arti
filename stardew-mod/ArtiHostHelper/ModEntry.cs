using System.Text.Json;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Menus;
using StardewValley.Network;
using StardewValley.Objects;
using StardewValley.Pathfinding;
using ArtiStardewBridge.Protocol;

namespace ArtiHostHelper;

public sealed class Config
{
    public bool Enabled { get; set; }
    public string RunId { get; set; } = "";
    public string OutputDirectory { get; set; } = "";
    public string SaveFolder { get; set; } = "";
    public ulong SaveId { get; set; }
    public long HostId { get; set; }
    public long FarmhandId { get; set; }
    public int SleepAt { get; set; } = 2400;
    public int HostDelaySeconds { get; set; }
    public int StartupPauseSeconds { get; set; } // opt-in isolated harness; legacy helpers unchanged
    public int[] GiftTimes { get; set; } = new[] { 900, 1300 };
    // ADR 74: "idle" waits for a still peer so free play is never cut; "moving" is the interruption witness.
    public string GiftWhen { get; set; } = GiftOpportunity.Idle;
    public int GiftIdleSeconds { get; set; } = 20;
    public string GiftItem { get; set; } = "(O)388";
    public int StageHostGiftCount { get; set; }
    // Legacy config compatibility. Native peer-entered check ids, rather than an allowlist, govern passive readiness.
    public string[] ReadyChecks { get; set; } = new[] { "sleep" };
    // Exact native lastQuestionKey -> responseKey. No fallback to first/last answer.
    public Dictionary<string, string> Answers { get; set; } = new();
}

public sealed class ModEntry : Mod, IHelperHoldPort
{
    Config cfg = new();
    string session = Guid.NewGuid().ToString("N");
    bool loaded, stopped;
    int day = -1;
    HashSet<int> gifts = new();
    IClickableMenu? seenMenu, ownedGiftPrompt;
    Proposal? proposal;
    string? response;
    int giftSlot, giftBefore;
    DateTime menuSince, sleepSince;
    bool soloHost;
    long? bedPathStarted;
    int bedPathInitialSteps;
    Farmer? bedWalker;
    PathFindController? bedPath;
    BedApproach? bedApproach;
    readonly HelperSleep sleep = new(); // review M4e item 1: its sleep state, one night only
    IClickableMenu? ownedGiftResponse;
    bool hostStaged;
    GiftOpportunity gift = new(GiftOpportunity.Idle, 20);
    int lastTime;
    int sequence;
    long rendered;
    PassiveReadyBudget passiveReady = new(); // review M4c: renewed when a wait ends (no budget spent across an unsampled wait)
    StartupPauseGate startup = new(0);
    readonly HostGiftSafetyInbox giftSafety = new();

    public override void Entry(IModHelper helper)
    {
        cfg = helper.ReadConfig<Config>();
        if (!cfg.Enabled) return;
        if (cfg.RunId.Length < 8 || !Path.IsPathFullyQualified(cfg.OutputDirectory)
            || cfg.HostId == 0 || cfg.FarmhandId == 0 || cfg.HostId == cfg.FarmhandId || cfg.SaveId == 0
            || Path.GetFileName(cfg.SaveFolder) != cfg.SaveFolder || cfg.HostDelaySeconds is < 0 or > 60
            || cfg.StartupPauseSeconds is < 0 or > 300)
            throw new InvalidOperationException("HOST helper requires explicit isolated-save identities and output directory");
        gift = new(cfg.GiftWhen, cfg.GiftIdleSeconds);
        Directory.CreateDirectory(cfg.OutputDirectory);
        startup = new(cfg.StartupPauseSeconds);
        soloHost = SoloHostLaunch.Verified(cfg.OutputDirectory);
        if (soloHost)
        {
            try { HostBedTouch.Install(ModManifest.UniqueID, BeginBedTouch, CaptureBedTouch); }
            catch (Exception ex) { Monitor.Log("HOST bed touch unavailable: " + ex.GetType().Name, LogLevel.Warn); }
        }
        Log("helper_started", new { config = cfg, dll = GetType().Assembly.ManifestModule.ModuleVersionId });
        helper.Events.GameLoop.UpdateTicked += Tick;
        helper.Events.Multiplayer.ModMessageReceived += (_, e) =>
        {
            if (!Own() || e.FromPlayerID != cfg.FarmhandId || e.FromModID != HostGiftSafety.BridgeModId
                || e.Type != HostGiftSafety.MessageType) return;
            var message = e.ReadAs<HostGiftSafetyMessage>();
            if (message.SaveId == cfg.SaveId) giftSafety.Observe(message.Stamp);
        };
        helper.Events.Multiplayer.PeerDisconnected += (_, e) => { if (e.Peer.PlayerID == cfg.FarmhandId) giftSafety.Clear(); };
        helper.Events.GameLoop.ReturnedToTitle += (_, _) => giftSafety.Clear();
        helper.Events.Display.Rendered += (_, _) => rendered++;
        helper.Events.GameLoop.Saved += (_, _) => { if (Own()) Log("native_saved", new { date = Game1.Date.TotalDays }); };
        // Review M4e item 1: the game's day start ends the helper's sleep state, even while he is active (Step returns).
        helper.Events.GameLoop.DayStarted += (_, _) => ClearSleep("day_started");
        // ADR 255 (review M4b): NO input fence. The owner's keys, mouse and controller always reach his game. His real input
        // (SMAPI ButtonPressed: keyboard, mouse buttons, controller buttons; MouseWheelScrolled) marks him active, and while
        // he is active the helper does nothing in the HOST. The helper itself never sends input (no Input.Press anywhere),
        // and the harness drives the HOST only through this helper's own calls, so every such event is his.
        helper.Events.Input.ButtonPressed += OnOwnerButton;
        helper.Events.Input.MouseWheelScrolled += OnOwnerWheel;
    }

    readonly HostOwnerPresence owner = new();

    /// <summary>ADR 255 (M4b): he is using the HOST (any input of his in the last HostOwnerPresence.IdleSeconds).</summary>
    bool OwnerActive()
    {
        try { return owner.Active(Environment.TickCount64); }
        catch { return true; } // any doubt: the HOST is his
    }

    void OnOwnerButton(object? sender, ButtonPressedEventArgs e) => OwnerInput(e.Button.ToString());

    void OnOwnerWheel(object? sender, MouseWheelScrolledEventArgs e) => OwnerInput("wheel");

    /// <summary>
    /// ADR 255 (M4b): one real input of his. Nothing is suppressed. He is now active: the helper drops its own movement at
    /// once (a bed path it started never pulls him along) and does nothing else until he has been idle for
    /// HostOwnerPresence.IdleSeconds. Named once per burst as owner_input, with what it was.
    /// </summary>
    void OwnerInput(string what)
    {
        try
        {
            var line = owner.Input(what, Environment.TickCount64);
            bedApproach = null;
            if (OwnsBedPath())
            {
                DropBedPath();
                Log("host_path_dropped_for_owner", new { what });
            }
            if (line is not null && Own())
            {
                this.Monitor.Log("HOST " + line + ": the owner is using the HOST; the harness waits (ADR 255)", LogLevel.Info);
                Log("owner_input", new { what, line, harness_waits = true });
            }
        }
        catch { /* naming never changes his input */ }
    }


    /// <summary>Review M4c rule 1: once per tick, the raw state of his devices (held keys and buttons, a stick or trigger
    /// off its rest, the mouse moving inside the HOST window). Any of these is his input, like a ButtonPressed event.</summary>
    void SampleOwner()
    {
        try
        {
            bool active = Game1.game1?.IsActive == true;
            KeyboardState k = Keyboard.GetState();
            MouseState m = Mouse.GetState();
            GamePadState pad = GamePad.GetState(PlayerIndex.One);
            var bounds = Game1.game1!.Window.ClientBounds;
            bool inWindow = m.X >= 0 && m.Y >= 0 && m.X < bounds.Width && m.Y < bounds.Height;
            bool mouseHeld = m.LeftButton == ButtonState.Pressed || m.RightButton == ButtonState.Pressed
                || m.MiddleButton == ButtonState.Pressed || m.XButton1 == ButtonState.Pressed || m.XButton2 == ButtonState.Pressed;
            bool padHeld = pad.IsConnected && Enum.GetValues<Buttons>().Any(b => pad.IsButtonDown(b));
            bool padOff = pad.IsConnected && (Math.Abs(pad.ThumbSticks.Left.X) > HostOwnerPresence.PadRest
                || Math.Abs(pad.ThumbSticks.Left.Y) > HostOwnerPresence.PadRest || Math.Abs(pad.ThumbSticks.Right.X) > HostOwnerPresence.PadRest
                || Math.Abs(pad.ThumbSticks.Right.Y) > HostOwnerPresence.PadRest || pad.Triggers.Left > HostOwnerPresence.PadRest
                || pad.Triggers.Right > HostOwnerPresence.PadRest);
            var raw = new HostOwnerPresence.RawInput(active, k.GetPressedKeys().Length > 0, mouseHeld, padHeld, padOff, m.X, m.Y, inWindow);
            if (owner.Sample(raw, Environment.TickCount64) is { } line && Own())
            {
                this.Monitor.Log("HOST " + line + ": the owner is using the HOST; the harness waits (ADR 255)", LogLevel.Info);
                Log("owner_input", new { what = line, harness_waits = true, source = "raw_state" });
            }
        }
        catch { /* a failed read is not his input; the event path still stamps him */ }
        // Review M4d item 1 (a): while he is present, the helper's own holds are released at once (every tick, after the
        // game has read his input), not only at the next step.
        try { if (OwnerActive() && Own()) { ReleaseHolds("owner_input"); ClearSleep("owner_input"); } } catch { }
    }

    int Today() => Context.IsWorldReady ? Game1.Date.TotalDays : -1;

    /// <summary>Review M4e item 1: the helper's sleep state belongs to one night. Cleared at the day start, on any owner
    /// input and on any stop; logged once when there was something to clear.</summary>
    void ClearSleep(string why)
    {
        try
        {
            bool any = sleep.Clear() | ownSleepPrompt is not null;
            ownSleepPrompt = null;
            DropBedPath();
            bedPathStarted = null;
            if (any) { try { Log("helper_sleep_cleared", new { why }); } catch { } }
        }
        catch { }
    }

    /// <summary>Review M4c rule 2: what keeps the HOST from being clean right now (null = clean): a menu or dialogue open,
    /// an item on the cursor, an event, a minigame, a festival, a tool or rod in use, or a player who cannot move.</summary>
    static string? CleanBlock(Farmer host)
    {
        if (Game1.activeClickableMenu is { } m) return "menu:" + m.GetType().Name;
        if (Game1.dialogueUp) return "dialogue";
        if (host.CursorSlotItem is not null) return "cursor_item";
        if (Game1.eventUp || Game1.CurrentEvent is not null || Game1.farmEvent is not null) return "event";
        if (Game1.currentMinigame is not null) return "minigame";
        if (Game1.isFestival()) return "festival";
        if (host.UsingTool || host.CurrentTool is StardewValley.Tools.FishingRod rod && rod.inUse()) return "tool";
        if (!host.CanMove) return "cannot_move";
        return null;
    }

    /// <summary>Review M4c rule 2: the HOST is not clean; the helper does nothing and says so once per episode.</summary>
    void NotClean(string blocks)
    {
        if (notClean == blocks) return;
        notClean = blocks;
        this.Monitor.Log("HOST host_not_clean: " + blocks + " (the helper does nothing; no stop)", LogLevel.Info);
        try { Log("host_not_clean", new { blocks }); }
        catch (Exception ex) { this.Monitor.Log("HOST not-clean log failed: " + ex.GetType().Name, LogLevel.Warn); }
    }

    string? notClean;
    bool inert;
    IClickableMenu? ownSleepPrompt;

    /// <summary>After any stop recognized by HostStop, the helper takes no action in this HOST launch.
    /// One log row. A host/STOP still latches the helper stop (HOST_STOP evidence) as before.</summary>
    bool Inert(string? observed = null)
    {
        if (inert) return true;
        string? why = stopped ? "helper_stop" : observed ?? StopMarker();
        if (why is null) return false;
        inert = true;
        ReleaseHolds("stop_seen:" + why); // review M4d item 1 (b, c): the first act when a stop is seen and it goes inert
        ClearSleep("stop_seen");
        if (why == "STOP" && !stopped) Stop("harness_stop");
        this.Monitor.Log("HOST host_inert after " + why + ": no action in the HOST until a new launch (ADR 255)", LogLevel.Warn);
        try { Log("host_inert", new { why }); } catch { }
        return true;
    }

    string? StopMarker() => HostStop.StopMarker(cfg.OutputDirectory);

    bool Own() => Context.IsWorldReady && Context.IsMainPlayer && !Context.IsSplitScreen && Game1.IsMasterGame
        && Game1.uniqueIDForThisGame == cfg.SaveId && Game1.player.UniqueMultiplayerID == cfg.HostId;

    bool GiftSafe(Farmer? peer) => peer is not null && giftSafety.MayOffer(peer.currentLocation is Farm,
        peer.hasMenuOpen.Value, peer.UsingTool, peer.hidden.Value,
        DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());

    void Log(string ev, object? facts = null)
    {
        var row = new { ev, utc = DateTime.UtcNow.ToString("O"), at = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000d,
            actor = "deterministic_host", run_id = cfg.RunId, session, sequence = ++sequence, facts };
        try { HarnessFileIo.Append(Path.Combine(cfg.OutputDirectory, "host.jsonl"), JsonSerializer.Serialize(row) + "\n"); }
        catch (IOException ex) { this.Monitor.Log("FAULT host_transcript " + ex.GetType().Name, LogLevel.Error); }
    }

    /// <summary>
    /// ADR 255 (review M4d item 1): give up every hold the helper itself imposed on the HOST (HelperHolds lists them), as
    /// the FIRST act on his input, any stop seen, the helper becoming inert, or a helper fault; with or without a release
    /// token. Never throws: each release is its own try. Logged once per release that did something.
    /// </summary>
    void ReleaseHolds(string why)
    {
        try
        {
            bool world = Context.IsWorldReady && Game1.IsMasterGame;
            var menu = world ? Game1.activeClickableMenu : null;
            var facts = new HoldFacts(
                PausedByHelper: startup.ReleaseHold(),
                HelperWalking: world && OwnsBedPath(),
                OwnPromptOpen: menu is not null && (ReferenceEquals(menu, ownSleepPrompt) || ReferenceEquals(menu, ownedGiftPrompt)
                    || ReferenceEquals(menu, ownedGiftResponse)),
                // Review M4e item 1: its own sleep wait only by its own record (the dialog it captured after its own Yes,
                // today), never by type and check name.
                OwnSleepWait: menu is ReadyCheckDialog wait && sleep.Owns(wait, Today()) && wait.isCancelable(),
                OwnGiftInFlight: world && proposal is not null && ReferenceEquals(Game1.player.team.GetOutgoingProposal(), proposal));
            var released = HelperHolds.Release(facts, this);
            if (released.Count == 0) return;
            this.Monitor.Log("HOST helper_holds_released (" + why + "): " + string.Join(", ", released) + " (ADR 255)", LogLevel.Info);
            try { Log("helper_holds_released", new { why, released }); } catch { }
            if (facts.PausedByHelper)
                try
                {
                    HarnessFileIo.AtomicWrite(Path.Combine(cfg.OutputDirectory, "startup.json"), JsonSerializer.Serialize(new {
                        utc = DateTime.UtcNow, run_id = cfg.RunId, state = startup.State, released_by = why,
                        host_paused = Game1.HostPaused }));
                }
                catch { }
        }
        catch (Exception ex) { this.Monitor.Log("HOST hold release failed: " + ex.GetType().Name, LogLevel.Error); }
    }

    void IHelperHoldPort.Unpause() => Game1.netWorldState.Value.IsPaused = false;

    void IHelperHoldPort.DropPath() => DropBedPath();

    bool OwnsBedPath() => bedPath is not null && Own() && ReferenceEquals(Game1.player, bedWalker)
        && ReferenceEquals(Game1.player.controller, bedPath);

    void DropBedPath()
    {
        if (OwnsBedPath())
        {
            Game1.player.controller = null;
            if (!OwnerActive()) Game1.player.Halt(); // never halts him while he moves the HOST
        }
        bedPath = null;
        bedWalker = null;
        bedApproach = null;
    }

    BedSite? CurrentBedSite()
    {
        if (!Own() || Game1.player.currentLocation is not FarmHouse house
            || !ReferenceEquals(Game1.currentLocation, house) || house.owner.UniqueMultiplayerID != cfg.HostId
            || house.GetPlayerBed() is not { } bed)
            return null;
        var destination = bed.GetBedSpot();
        return new BedSite(Game1.player, house, bed, Today(), (destination.X, destination.Y));
    }

    static bool IsOwnBedTouch(BedSite site, GameLocation location, Vector2 tile)
    {
        if (!ReferenceEquals(site.House, location) || location.lastTouchActionLocation != tile || Game1.player.Tile != tile)
            return false;
        // A single bed's native Sleep tile may be one tile left of GetPlayerBedSpot().
        string action = "";
        return ((BedFurniture)site.Bed).DoesTileHaveProperty((int)tile.X, (int)tile.Y, "TouchAction", "Back", ref action)
            && action == "Sleep";
    }

    BedApproach? BeginBedTouch(GameLocation location, Vector2 tile)
    {
        if (bedApproach is not { } approach || stopped || inert || OwnerActive()
            || CurrentBedSite() is not { } site || !IsOwnBedTouch(site, location, tile)
            || bedPath?.pathToEndPoint is not { } steps || steps.Count >= bedPathInitialSteps
            || CleanBlock(Game1.player) is not null || Inert())
            return null;
        return approach.CanTouch(site, Game1.player.controller, Game1.activeClickableMenu,
            ((int)tile.X, (int)tile.Y), Environment.TickCount64) ? approach : null;
    }

    void CaptureBedTouch(BedApproach approach, GameLocation location, Vector2 tile)
    {
        if (!ReferenceEquals(approach, bedApproach) || stopped || inert || OwnerActive()
            || CurrentBedSite() is not { } site || !IsOwnBedTouch(site, location, tile)
            || Game1.activeClickableMenu is not DialogueBox { isQuestion: true } prompt || location.lastQuestionKey != "Sleep")
            return;
        approach.Capture(site, Game1.player.controller, prompt, Environment.TickCount64);
    }

    void BedPathArrived(Character character, GameLocation location)
    {
        if (bedApproach is { } approach && CurrentBedSite() is { } site
            && ReferenceEquals(site.Farmer, character) && ReferenceEquals(site.House, location))
            approach.Arrive(site, character.controller, Environment.TickCount64);
    }

    void ObserveBedApproach()
    {
        if (bedApproach is { } approach && (CurrentBedSite() is not { } site
            || !approach.Observe(site, Game1.player.controller, Game1.activeClickableMenu, Environment.TickCount64)))
            DropBedPath();
    }

    bool OwnsBedTouch(IClickableMenu menu) => bedApproach is { } approach && CurrentBedSite() is { } site
        && approach.Owns(site, Game1.player.controller, menu, Environment.TickCount64);

    bool CanAnswerSleep(DialogueBox menu, bool native, int today)
    {
        if (OwnerActive() || stopped || inert) return false;
        if (!Own() || !ReferenceEquals(Game1.activeClickableMenu, menu) || Today() != today || !sleep.MayPressYes(today)
            || !menu.isQuestion || Game1.player.currentLocation is not { lastQuestionKey: "Sleep", afterQuestion: null }
            || Game1.eventUp
            || Game1.CurrentEvent is not null || Game1.farmEvent is not null
            || !(native ? OwnsBedTouch(menu) : ReferenceEquals(menu, ownSleepPrompt)))
        {
            NotClean("menu:" + menu.GetType().Name);
            return false;
        }
        return true;
    }

    bool CanPressMenu(IClickableMenu menu) => !Inert() && ReferenceEquals(Game1.activeClickableMenu, menu);

    void IHelperHoldPort.CloseOwnPrompt()
    {
        var menu = Game1.activeClickableMenu;
        if (ReferenceEquals(menu, ownSleepPrompt) || ReferenceEquals(menu, ownedGiftPrompt) || ReferenceEquals(menu, ownedGiftResponse))
        {
            if (menu is DialogueBox own) own.closeDialogue(); // only the prompt it opened itself, by reference
        }
        ownSleepPrompt = ownedGiftPrompt = ownedGiftResponse = null;
    }

    void IHelperHoldPort.CancelOwnSleepWait()
    {
        if (Game1.activeClickableMenu is ReadyCheckDialog wait && sleep.Owns(wait, Today()) && wait.isCancelable())
        {
            var center = wait.cancelButton.bounds.Center; // the native cancel: it withdraws the HOST's "sleep" readiness
            wait.receiveLeftClick(center.X, center.Y);
        }
        sleep.Clear(); // the night is no longer its own
    }

    void IHelperHoldPort.WithdrawOwnGift()
    {
        if (proposal is not null && ReferenceEquals(Game1.player.team.GetOutgoingProposal(), proposal))
            proposal.canceled.Value = true; // the sender's withdrawal (the bridge answers no withdrawn gift)
    }

    void Stop(string reason, bool fault = false)
    {
        if (stopped) return;
        stopped = true;
        ReleaseHolds(fault ? "fault" : "helper_stop"); // review M4d item 1 (b, d): its own holds go first
        ClearSleep(fault ? "fault" : "helper_stop"); // review M4e item 1
        this.Monitor.Log("HOST_SAFETY_STOP: " + reason, LogLevel.Error);
        // Each evidence channel is independent: a failed log must not suppress HOST_STOP.
        try { Log("HOST_SAFETY_STOP", new { reason }); }
        catch (Exception ex) { this.Monitor.Log("HOST stop log failed: " + ex, LogLevel.Error); }
        try { HarnessFileIo.AtomicWrite(Path.Combine(cfg.OutputDirectory, "HOST_STOP"), reason); }
        catch (Exception ex) { this.Monitor.Log("HOST stop marker failed: " + ex, LogLevel.Error); }
    }

    readonly ExitWhenSafe exitContract = new();
    ExitWhenSafe.Request? exitRequest;
    readonly List<ExitWhenSafe.MenuKind> exitMenus = new();
    bool exitCommitted; // W1b E3: an "exiting" answer is published; the helper does nothing more
    Dictionary<string, object>? exitAnswer; // the published "exiting" answer, logged only after Exit()

    /// <summary>
    /// ADR 256 "exit when safe", the HOST half. The request is read four times a second and every tick is judged.
    /// <para>W1b E2: the request must name this run, this helper session, this save and this HOST farmer, its requester
    /// process must be alive, and the helper must still own its HOST (<c>Own()</c>, no latched stop).</para>
    /// <para>The safe test adds: no farmhand mid-join (a server connection that is not yet a farmer), and nobody ready for
    /// sleep (the overnight is about to begin).</para>
    /// <para>W1b E3: at the decision the request is re-read and must be the same; the "exiting" answer is written durably
    /// (flushed, read back); only then does the HOST quit (no save). A failed write never quits.</para>
    /// </summary>
    bool TickExitWhenSafe(UpdateTickedEventArgs e)
    {
        try
        {
            if (e.IsMultipleOf(15)) exitRequest = ExitWhenSafe.ReadRequest(cfg.OutputDirectory);
            DateTimeOffset now = DateTimeOffset.UtcNow;
            bool ready = Context.IsWorldReady && Game1.player is not null;
            // Review M4 E2 / M4b rule 4: a request dies with a HOST stop (latched, or a STOP on disk not yet polled), an
            // ownership handoff (Own() false), a new controller session (controller.json, written once per supervisor;
            // none = none can be named), and while the owner uses the HOST (and for 180 s after his last input).
            // Review M4d item 2: accepted only from the same clean HOST that gates every helper action: no stop (the latched
            // one, inert, and any stop marker of the run while a request exists), his 180 s, and CleanBlock below.
            // Item 4: the request must name this process (the recorded HOST launch).
            var self = new ExitWhenSafe.Self(cfg.RunId, session, ready ? Game1.uniqueIDForThisGame : null,
                ready ? Game1.player!.UniqueMultiplayerID : null, ready && HostStillOwned() && !inert
                    && (exitRequest is null || StopMarker() is null), ReadControllerSession() ?? "",
                OwnerActive(), Environment.ProcessId);
            string? why = null;
            if (exitRequest is not null)
            {
                exitMenus.Clear();
                IClickableMenu? menu = Game1.activeClickableMenu;
                for (int depth = 0; menu is not null && depth < ExitWhenSafe.MaxMenuChain; depth++)
                {
                    exitMenus.Add(new ExitWhenSafe.MenuKind(menu.GetType().Name, menu is ConfirmationDialog, menu is ShippingMenu,
                        menu is LevelUpMenu, menu is SaveGameMenu, menu is ShopMenu { heldItem: not null }));
                    menu = menu.GetChildMenu();
                }
                bool joining = Game1.server is { } server && server.connectionsCount > Game1.otherFarmers.Count;
                // Review M4 E4: only a current sleep ready check holds an exit (a retained prior-night bedtime does not).
                bool sleepReady = ready && Game1.netReady.GetNumberReady("sleep") > 0;
                why = ExitWhenSafe.Why(Game1.game1.IsSaving, Game1.newDay, Game1.showingEndOfNightStuff, ready,
                    Game1.eventUp || Game1.CurrentEvent is not null, ExitWhenSafe.CommittingMenu(exitMenus), joining,
                    sleepReady: sleepReady, notClean: ready ? CleanBlock(Game1.player!) : null);
            }
            var decision = exitContract.Tick(exitRequest, self, ExitWhenSafe.ProcessAlive, why, now,
                ready ? "total_days:" + Game1.Date.TotalDays : "", Game1.timeOfDay);
            if (decision is null) return false;
            if (decision.Exit && ExitWhenSafe.ReadRequest(cfg.OutputDirectory) != decision.For)
            {
                exitContract.Abandon(); // W1b E2: revoked or replaced since the last read
                this.Monitor.Log("HOST EXIT_WHEN_SAFE request changed at the decision; not exiting (ADR 256)", LogLevel.Info);
                return false;
            }
            bool ok = ExitWhenSafe.PublishDurably(cfg.OutputDirectory, decision.Answer);
            if (!ok) this.Monitor.Log("FAULT exit_answer write (" + decision.State + "); not exiting (ADR 256)", LogLevel.Error);
            bool quit = exitContract.Published(decision, ok, now);
            // Review M4 E2: judged again after the write: revoked, replaced, past its deadline, stopped or handed off
            // during the write means no quit, and an "aborted" answer replaces the "exiting" one.
            // Review M4b rule 4: this is the last judgement before Exit(): his input, a new controller session, and any
            // stop of the run (M4c: StopMarker, the same reader that makes the helper inert, bridge markers included), the
            // native save state and the requester process count here too, in the same tick as Exit().
            if (quit && ExitWhenSafe.DiedDuringPublish(decision.For, ExitWhenSafe.ReadRequest(cfg.OutputDirectory),
                    HostStillOwned() && StopMarker() is null && !Game1.game1.IsSaving && !Game1.newDay && !Game1.showingEndOfNightStuff
                    && CleanBlock(Game1.player) is null && Game1.netReady.GetNumberReady("sleep") == 0 // review M4d item 2: again here
                    && ExitWhenSafe.ProcessAlive(decision.For.RequesterPid, decision.For.RequesterImage)
                    && ReadControllerSession() is { } ctl && ctl == decision.For.Session,
                    DateTimeOffset.UtcNow, OwnerActive()) is { } died)
            {
                var aborted = exitContract.AbortedAnswer(decision.For, died, DateTimeOffset.UtcNow,
                    Context.IsWorldReady ? "total_days:" + Game1.Date.TotalDays : "", Game1.timeOfDay);
                ExitWhenSafe.PublishDurably(cfg.OutputDirectory, aborted);
                this.Monitor.Log("HOST EXIT_WHEN_SAFE request died during the answer write (" + died + "); not exiting (ADR 256)", LogLevel.Warn);
                try { Log("exit_answered", aborted); } catch { }
                return false;
            }
            if (quit) { exitAnswer = decision.Answer; return true; } // review M4d: its log comes after Exit(), never before
            if (ok && decision.State != ExitWhenSafe.Waiting)
            {
                try { Log("exit_answered", decision.Answer); }
                catch (Exception ex) { this.Monitor.Log("HOST exit_answered log failed: " + ex.GetType().Name, LogLevel.Warn); }
            }
            return false;
        }
        catch (Exception ex)
        {
            this.Monitor.Log("HOST EXIT_WHEN_SAFE fault " + ex.GetType().Name + "; not exiting (ADR 256)", LogLevel.Error);
            return false; // any doubt: the HOST keeps playing
        }
    }

    /// <summary>Review M4b rule 4: the controller session the supervisor wrote once into this folder (controller.json,
    /// this run's), read fresh; null = none or unreadable (no request can then be honoured).</summary>
    string? ReadControllerSession()
    {
        try
        {
            string path = Path.Combine(cfg.OutputDirectory, "controller.json");
            if (!File.Exists(path)) return null;
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            return root.ValueKind == JsonValueKind.Object && root.TryGetProperty("run_id", out var run)
                && run.ValueKind == JsonValueKind.String && run.GetString() == cfg.RunId
                && root.TryGetProperty("session", out var s) && s.ValueKind == JsonValueKind.String && s.GetString() is { Length: > 0 } id
                ? id : null;
        }
        catch { return null; }
    }

    /// <summary>Review M4 E2: the HOST is still the harness's: its own HOST and save, no latched stop, and no STOP or
    /// HOST_STOP on disk (a stop written since the last Step poll counts at once).</summary>
    bool HostStillOwned()
    {
        try
        {
            return Own() && !stopped && !File.Exists(Path.Combine(cfg.OutputDirectory, "STOP"))
                && !File.Exists(Path.Combine(cfg.OutputDirectory, "HOST_STOP"));
        }
        catch { return false; }
    }

    /// <summary>ADR 255 (review M4d item 3): while it waits, host_wait.json is rewritten at least this often.</summary>
    public const double WaitPeriodSeconds = 2;
    string? waitWritten = "";
    double waitWrittenAt = double.NegativeInfinity;

    /// <summary>
    /// Review M4d item 3: the helper's wait is a fact only while the helper is provably alive. It rewrites host_wait.json
    /// (state waiting / clear / inert, what it waits on, unix <c>at</c>, <c>period_s</c>) on each change and at least every
    /// WaitPeriodSeconds. A reader treats a waiting row older than three periods as no wait (the normal fault path applies).
    /// </summary>
    void WriteWait()
    {
        try
        {
            string? what = stopped || inert ? null : OwnerActive() ? "owner_input" : notClean is { } blocks ? "host_not_clean:" + blocks : null;
            string state = stopped || inert ? "inert" : what is null ? "clear" : "waiting";
            double now = Environment.TickCount64 / 1000d;
            if (state + what == waitWritten && now - waitWrittenAt < WaitPeriodSeconds) return;
            waitWritten = state + what; waitWrittenAt = now;
            HarnessFileIo.AtomicWrite(Path.Combine(cfg.OutputDirectory, "host_wait.json"), JsonSerializer.Serialize(new {
                run_id = cfg.RunId, session, state, what, period_s = WaitPeriodSeconds,
                at = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000d }));
        }
        catch (Exception ex) { this.Monitor.Log("HOST wait row failed: " + ex.GetType().Name, LogLevel.Warn); } // a stale row reads as no wait
    }

    void Tick(object? sender, UpdateTickedEventArgs e)
    {
        SampleOwner(); // review M4c rule 1: every tick, before anything else (his input this tick counts before an exit)
        if (exitCommitted) return; // ADR 256 (W1b E3): after a published "exiting", nothing more is done
        if (TickExitWhenSafe(e)) // ADR 256 (M4c rule 6): the last veto, the exiting answer and Exit() in this one tick
        {
            exitCommitted = true;
            Game1.game1.Exit(); // nothing between the last veto and this call (the log comes after)
            this.Monitor.Log("HOST EXIT_WHEN_SAFE exiting (ADR 256)", LogLevel.Info);
            try { Log("exit_answered", exitAnswer); } catch { }
            return;
        }
        try
        {
            ObserveBedApproach();
            if (!e.IsMultipleOf(15)) return;
            if (e.IsMultipleOf(60))
            {
                try { HarnessFileIo.AtomicWrite(Path.Combine(cfg.OutputDirectory, "heartbeat"), DateTime.UtcNow.ToString("O")); }
                catch (IOException ex) { this.Monitor.Log("FAULT host_heartbeat " + ex.GetType().Name, LogLevel.Error); }
                string path = Path.Combine(cfg.OutputDirectory, "barrier.json");
                HarnessFileIo.AtomicWrite(path, JsonSerializer.Serialize(new {
                    utc = DateTime.UtcNow, run_id = cfg.RunId, runtime_id = session, world_ready = Context.IsWorldReady, // ADR 256: runtime_id
                    saving = Game1.game1.IsSaving || Game1.newDay || Game1.showingEndOfNightStuff || !Context.IsWorldReady,
                    rendered, stopped, accepting = HostAcceptingFacts.Read(cfg.FarmhandId) })); // whether this HOST can accept her farmhand
            }
            if (!stopped) Step(e);
            WriteWait(); // review M4d item 3: a periodic, fresh wait row (or clear / inert)
        }
        catch (Exception ex) { Stop(ex.ToString(), fault: true); } // full path/operation + inner stack; review M4d (d): a fault releases its holds first
    }

    void Step(UpdateTickedEventArgs e)
    {
        // Review M4c rule 1: the owner check is the FIRST statement: while he uses the HOST (or within 180 s of his last
        // input) the helper does nothing, not even the startup pause. Rule 4: after any stop it does nothing at all.
        if (OwnerActive()) { ReleaseHolds("owner_input"); ClearSleep("owner_input"); return; } // M4d item 1: its own holds go first
        if (Inert()) { ReleaseHolds("inert"); return; }
        // Loading mirrors CoopMenu.HostFileSlot.Activate; never loads a different slot or rejoins automatically.
        if (!Context.IsWorldReady)
        {
            if (!loaded && Game1.activeClickableMenu is TitleMenu && TitleMenu.subMenu == null
                && Game1.currentLoader == null && !Game1.IsClient)
            {
                loaded = true;
                if (!File.Exists(Path.Combine(Constants.SavesPath, cfg.SaveFolder, cfg.SaveFolder))) { Stop("save_missing"); return; }
                Log("STAGING_host_load", new { cfg.SaveFolder });
                Game1.multiplayerMode = 2;
                SaveGame.Load(cfg.SaveFolder);
                Game1.exitActiveMenu(); // Only native title-slot transition, never a world menu force-close.
            }
            return;
        }
        if (!Own()) { Stop("host_or_save_identity_mismatch"); return; }
        if (File.Exists(Path.Combine(cfg.OutputDirectory, "STOP"))) { Stop("harness_stop"); return; }
        // ADR 65: this exact isolated HOST owns one bounded native startup pause.
        // ADR 242: only a held pause reads the release token, through the shared, contention-tolerant read;
        // once released the gate never needs the file again (it was read every 15 ticks for the whole session).
        string releasePath = Path.Combine(cfg.OutputDirectory, "STARTUP_RELEASE");
        bool release = startup.State == "held" && HarnessFileIo.TryReadToken(releasePath, cfg.RunId);
        string startupEdge = startup.Advance(Environment.TickCount64 / 1000d, Game1.HostPaused, release);
        if (startupEdge == "fail") { Stop(startup.Reason!); return; }
        if (startupEdge is "pause" or "resume")
        {
            Game1.netWorldState.Value.IsPaused = startupEdge == "pause";
            Log("startup_" + startupEdge, new { time = Game1.timeOfDay, day = Game1.Date.TotalDays });
        }
        if (e.IsMultipleOf(60) || startupEdge is "pause" or "resume")
            HarnessFileIo.AtomicWrite(Path.Combine(cfg.OutputDirectory, "startup.json"), JsonSerializer.Serialize(new {
                utc = DateTime.UtcNow, run_id = cfg.RunId, state = startup.State,
                host_paused = Game1.HostPaused, time = Game1.timeOfDay, day = Game1.Date.TotalDays }));
        if (owner.Ended(Environment.TickCount64) is { } ended)
        {
            this.Monitor.Log("HOST owner_input_ended (" + ended + "): the harness takes the HOST back (ADR 255)", LogLevel.Info);
            Log("owner_input_ended", new { reason = ended, idle_seconds = HostOwnerPresence.IdleSeconds });
        }
        var host = Game1.player;
        if (!hostStaged)
        {
            hostStaged = true;
            if (cfg.StageHostGiftCount is < 0 or > 100) { Stop("invalid_host_staging_count"); return; }
            if (cfg.StageHostGiftCount > 0)
            {
                if (File.Exists(Path.Combine(cfg.OutputDirectory, "ARMED"))) { Stop("staging_after_arm_refused"); return; }
                bool added = host.addItemToInventoryBool(ItemRegistry.Create(cfg.GiftItem, cfg.StageHostGiftCount));
                Log("STAGING_host_gift_inventory", new { cfg.GiftItem, cfg.StageHostGiftCount, added });
                if (!added) { Stop("host_staging_inventory_full"); return; }
            }
        }
        var peer = Game1.getOnlineFarmers().FirstOrDefault(p => p.UniqueMultiplayerID == cfg.FarmhandId);
        // Passive readiness/menu/sleep/gift work belongs to the released session only.
        // Telemetry still runs while startup is held (below).
        var names = Game1.getOnlineFarmers().ToDictionary(p => p.UniqueMultiplayerID, p => p.Name);
        if (peer is not null && e.IsMultipleOf(60))
            this.Helper.Multiplayer.SendMessage(new ReadyCheckRelay(cfg.SaveId,
                NativeReadyChecks.Server(Game1.netReady, names, cfg.HostId)), "native_ready_checks",
                modIDs: new[] { "bohanyt.ArtiStardewBridge" }, playerIDs: new[] { cfg.FarmhandId });
        bool armed = File.Exists(Path.Combine(cfg.OutputDirectory, "ARMED"));
        if (day != Game1.Date.TotalDays)
        {
            // A reached slot that never met its condition is reported, never retried on another day.
            foreach (int time in cfg.GiftTimes.Where(t => day >= 0 && armed && lastTime >= t && !gifts.Contains(t)))
                Log("gift_not_exercised", new { time, reason = "peer_not_" + cfg.GiftWhen });
            // Review M4e item 1: the sleep state is not reset here (Step does not run while he is active); the game's
            // DayStarted ends it, and his input or any stop ends it sooner.
            day = Game1.Date.TotalDays; gifts.Clear();
            Log("native_day", new { day, Game1.timeOfDay });
        }
        lastTime = Game1.timeOfDay;
        var peerPose = peer == null ? null : $"{peer.currentLocation?.NameOrUniqueName}:{peer.Position.X}:{peer.Position.Y}";
        // Synced peer net fields only: menu open, tool in use, hidden (event, warp, bus).
        double now = Environment.TickCount64 / 1000d;
        bool giftReady = gift.Observe(peerPose, peer?.isMoving() == true,
            !this.GiftSafe(peer), now);
        bool moving = gift.PeerMoving;
        if (e.IsMultipleOf(60))
        {
            var facts = new { day, time = Game1.timeOfDay, host_id = host.UniqueMultiplayerID,
                menu = Game1.activeClickableMenu?.GetType().Name, location = host.currentLocation?.NameOrUniqueName,
                tile = host.TilePoint, host_bed = host.isInBed.Value, host_ready = host.timeWentToBed.Value,
                peer_id = peer?.UniqueMultiplayerID, peer_pose = peerPose, peer_moving = moving,
                peer_bed = peer?.isInBed.Value, peer_ready = peer?.timeWentToBed.Value,
                peer_items = peer?.Items.Select((i, n) => new { slot = n, id = i?.QualifiedItemId, count = i?.Stack }).ToArray(),
                peer_health = peer?.health, peer_stamina = peer?.Stamina, host_pause = Game1.HostPaused,
                event_up = Game1.eventUp, farm_event_type = Game1.farmEvent?.GetType().Name,
                event_id = Game1.CurrentEvent?.id, event_command = Game1.CurrentEvent?.CurrentCommand,
                sleep_ready = Game1.netReady.GetNumberReady("sleep"), sleep_required = Game1.netReady.GetNumberRequired("sleep"),
                game_version = Game1.version, rendered };
            Log("native_heartbeat", facts);
            string path = Path.Combine(cfg.OutputDirectory, "status.json");
            HarnessFileIo.AtomicWrite(path, JsonSerializer.Serialize(new { utc = DateTime.UtcNow, run_id = cfg.RunId, facts }));
        }
        if (startup.State == "held") return;
        if (proposal != null)
        {
            var next = proposal.response.Value.ToString();
            if (next != response) { response = next; Log("gift_native_response", new { response, receiver = proposal.receiver.UID }); }
            if (host.team.GetOutgoingProposal() == null)
            {
                Log("gift_native_completed", new { response, giftSlot, before = giftBefore, after = host.Items[giftSlot]?.Stack ?? 0,
                    receiver_items = peer?.Items.Select(i => new { id = i?.QualifiedItemId, count = i?.Stack }).ToArray() });
                proposal = null;
                ownedGiftResponse = Game1.activeClickableMenu is DialogueBox text && !text.isQuestion ? text : null;
            }
        }
        var menu = Game1.activeClickableMenu;
        if (!ReferenceEquals(menu, ownSleepPrompt)) ownSleepPrompt = null;
        bool nativeSleep = menu is not null && OwnsBedTouch(menu);
        if (!ReferenceEquals(menu, seenMenu))
        {
            seenMenu = menu; menuSince = DateTime.UtcNow;
            Log("host_menu", new { type = menu?.GetType().Name });
            if (nativeSleep) Log("host_bed_question", new { own_prompt = true, native_touch = true });
        }
        // The helper acts only on its own sleep and gift prompts, the gift reply and the menus of its own night.
        // The solo HOST's native sleep prompt belongs to the bed walk that produced it. Any other menu is not clean:
        // the helper does nothing (it never closes, answers or advances it) and says so once per episode.
        if (menu != null)
        {
            sleep.Capture(menu, menu is ReadyCheckDialog { checkName: "sleep" }, Today()); // only right after its own Yes, today
            bool ownSleep = nativeSleep || ReferenceEquals(menu, ownSleepPrompt);
            bool own = ownSleep || ReferenceEquals(menu, ownedGiftPrompt) || ReferenceEquals(menu, ownedGiftResponse);
            bool ownNightMenu = sleep.OwnNight(Today()) && (menu is ShippingMenu || menu is SaveGameMenu || menu is LevelUpMenu { isProfessionChooser: false });
            if (!own && !ownNightMenu) { NotClean("menu:" + menu.GetType().Name); return; }
            // A modal question blocks movement; the game abandons a stalled controller after five seconds.
            // The configured sleep delay already ran before the walk. Keep the one-second minimum for a native touch.
            if ((DateTime.UtcNow - menuSince).TotalSeconds < (nativeSleep ? 1 : Math.Max(1, cfg.HostDelaySeconds))) return;
            switch (menu)
            {
                case SaveGameMenu: return; // Native update/draw owns persistence. Never force-close.
                case ShippingMenu shipping:
                    var center = shipping.okButton.bounds.Center;
                    if (!CanPressMenu(menu)) return;
                    shipping.receiveLeftClick(center.X, center.Y);
                    Log("host_shipping_ok", new { own_night = true }); return;
                case LevelUpMenu level:
                    if (level.isActive && CanPressMenu(menu)) { level.okButtonClicked(); Log("host_level_ok", new { own_night = true }); } return;
                case DialogueBox dialogue:
                    if (dialogue.transitioning || dialogue.safetyTimer > 0) return;
                    if (ownSleep && dialogue.characterIndexInDialogue < dialogue.getCurrentString().Length - 1) return;
                    if (ownSleep && (!dialogue.isQuestion || host.currentLocation?.lastQuestionKey != "Sleep"))
                    { NotClean("menu:" + menu.GetType().Name); return; }
                    var plan = PassiveDialoguePolicy.Plan(dialogue.isQuestion, host.currentLocation?.lastQuestionKey,
                        ownSleep, ReferenceEquals(menu, ownedGiftPrompt), cfg.Answers);
                    if (plan.Advance)
                    {
                        if (!CanPressMenu(menu)) return;
                        dialogue.receiveLeftClick(dialogue.xPositionOnScreen + 32, dialogue.yPositionOnScreen + 32);
                        Log("host_text_ok", new { own = true });
                        if (Game1.activeClickableMenu != dialogue) ownedGiftResponse = null;
                        return;
                    }
                    string? answer = plan.Answer;
                    // Recheck before sending the native proposal, not just when
                    // opening the HOST confirmation on the previous step.
                    if (ReferenceEquals(menu, ownedGiftPrompt) && !this.GiftSafe(peer))
                        answer = "No";
                    if (answer == null) { NotClean("question:" + host.currentLocation?.lastQuestionKey); return; }
                    int index = Array.FindIndex(dialogue.responses, r => r.responseKey == answer);
                    if (index < 0 || dialogue.responseCC == null || index >= dialogue.responseCC.Count) return;
                    var p = dialogue.responseCC[index].bounds.Center;
                    int answerDay = Today();
                    if (ownSleep)
                    {
                        if (nativeSleep && !SoloHostLaunch.Verified(cfg.OutputDirectory))
                        { DropBedPath(); NotClean("menu:" + menu.GetType().Name); return; }
                        SampleOwner();
                        if (Inert() || !CanAnswerSleep(dialogue, nativeSleep, answerDay)) return;
                    }
                    dialogue.performHoverAction(p.X, p.Y);
                    if (ownSleep)
                    {
                        if (dialogue.selectedResponse != index || !CanAnswerSleep(dialogue, nativeSleep, answerDay)) return;
                        if (!HostStop.TryClickSleep(cfg.OutputDirectory, dialogue, p, sleep, answerDay, out var stop))
                        {
                            if (stop is not null) Inert(stop);
                            return;
                        }
                    }
                    else
                    {
                        if (!CanPressMenu(menu)) return;
                        dialogue.receiveLeftClick(p.X, p.Y);
                    }
                    Log("host_answer", new { answer, gift = ReferenceEquals(menu, ownedGiftPrompt), own_sleep = ownSleep });
                    if (ownSleep)
                    {
                        ownSleepPrompt = null;
                        DropBedPath();
                        // Review M4e item 1: its own night, recorded with its day; the sleep wait that follows is captured by
                        // reference (now, or on the next step before anything clears the record).
                        if (answer == "Yes")
                        {
                            sleep.Answered(answerDay);
                            sleep.Capture(Game1.activeClickableMenu, Game1.activeClickableMenu is ReadyCheckDialog { checkName: "sleep" }, Today());
                        }
                    }
                    if (ReferenceEquals(menu, ownedGiftPrompt))
                    {
                        proposal = host.team.GetOutgoingProposal(); response = null;
                        Log("gift_proposal", new { sent = proposal != null, receiver = peer?.UniqueMultiplayerID, moving });
                        ownedGiftPrompt = null;
                    }
                    return;
                default: NotClean("menu:" + menu.GetType().Name); return;
            }
        }
        // ADR 255 (M4c rule 2): with no menu open the HOST must still be clean (no dialogue, nothing on the cursor, no event,
        // minigame, festival or tool, a player who can move). Otherwise the helper does nothing and says so.
        if (CleanBlock(host) is { } blocks) { NotClean(blocks); return; }
        if (notClean is not null)
        {
            Log("host_clean_again", new { after = notClean });
            notClean = null;
            passiveReady = new(); // a wait spends no ready-check budget
        }
        // Ready checks the farmhand waits on are readied from a clean HOST only.
        var checks = NativeReadyChecks.Server(Game1.netReady, names, cfg.HostId);
        bool waitingCheck = checks.Any(c => c.Players.Any(p => p.Id == cfg.FarmhandId && p.State is "Ready" or "Locked"));
        if (!passiveReady.Observe(waitingCheck, Environment.TickCount64 / 1000d))
        { Stop("passive_ready_check_bound"); return; }
        foreach (var check in checks.Where(c => startup.State != "held" && passiveReady.ShouldReady(c, cfg.HostId, cfg.FarmhandId)))
        {
            if (Inert()) return;
            Game1.netReady.SetLocalReady(check.Check, true); // local HOST only; never opens a check for ARTI
            passiveReady.Readied();
            Log("host_ready_check", new { check = check.Check, before = check.Players,
                ready = Game1.netReady.GetNumberReady(check.Check), required = Game1.netReady.GetNumberRequired(check.Check) });
        }
        if (peer == null) return;
        // Host stays in its own farmhouse. No farm/chest transactions or pursuit. ADR 255 (M4c rule 3): no warp. A HOST in
        // another location is never moved; the game's own 2:00 AM ends the day (a pass-out on the HOST's farm).
        if (host.currentLocation is not FarmHouse house || house.owner.UniqueMultiplayerID != cfg.HostId)
        { NotClean("away:" + host.currentLocation?.NameOrUniqueName); return; }
        if (peer.timeWentToBed.Value != 0 || Game1.timeOfDay >= cfg.SleepAt)
        {
            if (!sleep.MayPressYes(Today())) return;
            if (!sleep.Due) { sleep.Due = true; sleepSince = DateTime.UtcNow; Log("host_sleep_due", new { peer_ready = peer.timeWentToBed.Value, Game1.timeOfDay }); }
            if ((DateTime.UtcNow - sleepSince).TotalSeconds < cfg.HostDelaySeconds) return;
            var site = soloHost ? CurrentBedSite() : null;
            if (soloHost && site is null) { NotClean("own_bed_missing"); return; }
            var bed = site is { } ownBed ? new Point(ownBed.Destination.X, ownBed.Destination.Y) : house.GetPlayerBedSpot();
            if (host.TilePoint == bed)
            {
                if (bedApproach is not null || host.controller is not null && !OwnsBedPath()) return;
                if (soloHost && !SoloHostLaunch.Verified(cfg.OutputDirectory)) return;
                if (Inert()) return;
                DropBedPath();
                bedPathStarted = null;
                house.createQuestionDialogue(Game1.content.LoadString("Strings\\Locations:FarmHouse_Bed_GoToSleep"), house.createYesNoResponses(), "Sleep", null);
                ownSleepPrompt = Game1.activeClickableMenu; // its OWN prompt, by reference: the only sleep prompt it answers
                Log("host_bed_question", new { bed, own_prompt = ownSleepPrompt is DialogueBox }); return;
            }
            if (bedPathStarted is null)
            {
                if (host.controller is not null || !house.farmers.Contains(host)
                    || soloHost && !SoloHostLaunch.Verified(cfg.OutputDirectory) || Inert()) return;
                var origin = host.TilePoint;
                var started = Environment.TickCount64;
                var controller = new PathFindController(host, house, bed, 2, BedPathArrived);
                bedWalker = host;
                bedPath = controller;
                bedPathStarted = started;
                bedPathInitialSteps = controller.pathToEndPoint?.Count ?? 0;
                if (site is { } walkSite)
                    bedApproach = new BedApproach(walkSite, controller, (origin.X, origin.Y), started);
                host.controller = controller;
                Log("host_path_to_own_bed", new { bed });
            }
            if (Environment.TickCount64 - bedPathStarted >= BedApproach.TimeoutMs) Stop("host_bed_path_timeout");
            return;
        }
        if (proposal != null || !armed || !giftReady || !this.GiftSafe(peer)) return;
        foreach (int time in cfg.GiftTimes)
        {
            if (Game1.timeOfDay < time || gifts.Contains(time)) continue;
            gifts.Add(time); // One opportunity per declared slot; never retry to get a pass.
            giftSlot = Enumerable.Range(0, host.Items.Count).FirstOrDefault(n => host.Items[n]?.QualifiedItemId == cfg.GiftItem, -1);
            if (giftSlot < 0) { Log("gift_not_exercised", new { time, reason = "host_item_missing" }); return; }
            host.CurrentToolIndex = giftSlot; giftBefore = host.Items[giftSlot].Stack;
            // Farmer.checkAction's gift branch has no range check. It creates the real HOST confirmation.
            if (host.ActiveObject?.canBeGivenAsGift() != true || host.ActiveObject.questItem.Value)
            { Log("gift_not_exercised", new { time, reason = "item_not_giftable" }); return; }
            peer.checkAction(host, host.currentLocation);
            ownedGiftPrompt = Game1.activeClickableMenu;
            Log("gift_host_prompt", new { time, receiver = peer.UniqueMultiplayerID, moving, when = cfg.GiftWhen,
                still_s = Math.Round(gift.StillSeconds(now), 1), native_prompt = ownedGiftPrompt is DialogueBox });
            return;
        }
    }
}
