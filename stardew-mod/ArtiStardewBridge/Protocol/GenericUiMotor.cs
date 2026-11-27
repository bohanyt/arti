using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// PM-1 attempt 7 #4 ("Gamepad disconnected." x68, each within 600 ticks of a UI_MOTOR frame): Auto
/// CheckGamepadMode (G1:3596-3630) compares the seated input's GetGamePadState().IsConnected with
/// _oldGamepadConnectedState and shows the HUD banner on true -> false. After a world hold SMAPI's retained
/// overrides read "connected" and the pad owner pins it; a motor frame without pad buttons returned a default
/// (disconnected) pad, so every UI press toggled the connection. A seated frame reports the connection the
/// game already holds, whatever its buttons; only real hardware/SMAPI state can change it.
/// </summary>
public static class GenericUiPadLink
{
    public static bool Connected(bool? gameHeld, bool hostConnected) => gameHeld ?? hostConnected;
}

// A real update consumes each frame. No synthetic menu.update or per-menu input adapter.
public sealed class GenericUiFrames
{
    public const int MaxHoldTicks = 120;
    readonly int holdTicks;
    int consumed;
    public GenericUiFrames(int holdTicks = 1)
    {
        if (holdTicks < 1 || holdTicks > MaxHoldTicks) throw new ArgumentOutOfRangeException(nameof(holdTicks));
        this.holdTicks = holdTicks;
    }
    public bool Pressed => this.consumed > 0 && this.consumed <= this.holdTicks;
    public bool WasPressed => this.consumed > 1 && this.consumed <= this.holdTicks + 1;
    public bool Done => this.consumed >= this.holdTicks + 2;
    public void Consume() { if (!this.Done) this.consumed++; }
}

/// <summary>
/// Receiver continuity across a delivered callback and each real native update, including
/// hold/release consumers. Callers must match the entry receiver BEFORE opening either window;
/// observations between windows never authenticate a replacement. No menu-type knowledge.
/// </summary>
public sealed class GenericUiSuccession
{
    object? owner;
    UiPadFacts current;
    bool observing;
    readonly List<GenericUiSuccessor> successors = new();
    public GenericUiSuccession(object? owner, UiPadFacts before) { this.owner = owner; this.current = before; }
    public IReadOnlyList<GenericUiSuccessor> Successors => this.successors;
    public bool Matches(object? owner, UiPadFacts facts) => ReferenceEquals(this.owner, owner)
        && this.current.ReceiverToken == facts.ReceiverToken;

    public bool Begin(object? owner, UiPadFacts before)
    {
        if (this.observing || !this.Matches(owner, before)) return false;
        this.observing = true;
        return true;
    }
    public bool End(object? owner, UiPadFacts after, bool delivered, bool completed, string source, string phase)
    {
        bool opened = this.observing;
        this.observing = false;
        if (!opened || !completed) return false;
        if (!this.Matches(owner, after))
        {
            if (!delivered || string.IsNullOrEmpty(after.ReceiverToken)) return false;
            this.successors.Add(new GenericUiSuccessor(source, phase, this.current.ReceiverToken,
                after.ReceiverToken, after.MenuOpen ? after.MenuType : null,
                after.GenericValues?.GetValueOrDefault("ui_input_gate") as string,
                UiPadExecutor.PressEffect(this.current, after) ?? "replaced"));
        }
        this.owner = owner; this.current = after;
        return true;
    }
}

public sealed record GenericUiSuccessor(string source, string phase, string? before_receiver,
    string? after_receiver, string? result_menu, string? input_gate, string effect);

public sealed class ProgressionClient
{
    /// <summary>PM-1 fix3: re-observation of uncertain-latched receivers (pad and ui_key verdicts).</summary>
    public UncertainReceiverSettle PadUncertain { get; } = new();
    public UncertainReceiverSettle KeyUncertain { get; } = new();
    public enum FaultKind { Uncertain, InputOwnershipLost, MutexHeld }
    public sealed record Fault(FaultKind Kind, long Epoch) {
        public bool Recovered(long epoch, bool idle) => idle && epoch != Epoch;
    }
    public static bool InformationalLetter(int pages, bool collection, bool quest, bool attachment, bool cursor, bool singleControl) =>
        pages == 1 && !collection && !quest && !attachment && !cursor && singleControl;
    // Reading dwell; the native adapter supplies only whitelist (a)-(e).
    public const long AbandonedMs = 4000;
    string decisionBinding = "";
    long decisionUntil;
    public const long NightProgressionMs = 10000;
    public static long Dwell(string entry, int chars) => entry switch {
        "c" => 10000, "d" => 6000, "e" => 4000, _ => Math.Clamp(2000L + chars * 50L, 2000, 6000) };
    bool nightSummary;
    string advanceBinding = "", advanceProgress = "";
    long advanceSince;
    bool advanceAttempted;
    public bool AdvanceDue(string binding, string progress, long now, bool advanceOnly, bool eligible, bool leased,
        bool night = false, long dwellMs = AbandonedMs, bool scripted = false)
    {
        this.nightSummary = night;
        if (!advanceOnly) { this.advanceBinding = ""; return false; }
        if (this.advanceBinding != binding || !night && this.advanceProgress != progress)
        {
            this.advanceBinding = binding; this.advanceProgress = progress;
            this.advanceSince = now; this.advanceAttempted = false; this.undelivered = 0;
            this.undeliveredStage = ""; this.pageSeq++;
        }
        this.LastDwellMs = dwellMs;
        // PM-1 a11 #2 (owner choice 3): an info-only night page (summary / level-up OK / lost items) auto-advances
        // once its dwell elapsed. A held motor arm lease is not input: Python kept it renewed while the summary
        // churned, and the OK fired only after Python stopped. Input in flight still fences this edge (the watchdog
        // returns before AdvanceDue while the slot is busy); a bounded in-flight decision lease still holds.
        // PM-1 fix10e (a14 d9, Marlon's Mine intro): scripted-event prose is the same system progression. Each page
        // waited 9-15 s behind the renewed arm lease while the multiplayer clock ran (3 game hours for 10 pages).
        // PM-1 fix10f (a15 d11:J1130-1315): the remaining 3-9 s per page was her decision lease. Each prose page took a
        // model decision whose lease held the edge past its dwell (and superseded decisions re-took it). Info-only
        // prose is owner choice 3 (natural reading dwell); a decision still holds every other advance-only page.
        return eligible && !(leased && !night && !scripted) && !(this.Deciding(binding, now) && !scripted) && !this.advanceAttempted
            && now - this.advanceSince >= dwellMs;
    }
    public void AdvanceAttempt() => this.advanceAttempted = true;
    /// <summary>ADR 241 C4: read-only; its one edge was sent for the current page.</summary>
    public bool Attempted => this.advanceAttempted;
    // PM-1 a9 night 11: a cancelled edge that never reached the menu (fault barrier, delivered=false) is not the
    // page's one automatic edge. Bounded re-arm; a delivered edge still ends automation for this page.
    public const int MaxUndelivered = 3;
    int undelivered;
    string undeliveredStage = "";
    long pageSeq;
    /// <param name="stage">Review R5 S4: where the edge stopped ("slot_admission", "motor:&lt;fault&gt;").</param>
    public void AdvanceUndelivered(string stage = "not_delivered")
    {
        this.undeliveredStage = stage;
        if (this.advanceAttempted && ++this.undelivered <= MaxUndelivered) this.advanceAttempted = false;
    }
    /// <summary>Review R5 S4: the page's edge never reached it and its re-arms are spent. The Held report names the stage
    /// instead of reading "attempted" as "already sent". null = sent, or a re-arm is still due.</summary>
    public string? UndeliveredGate => this.advanceAttempted && this.undelivered > MaxUndelivered
        ? "advance_undelivered:" + this.undeliveredStage : null;
    /// <summary>Review R5 S5: the watchdog's own semantic page (binding + progress sequence), for the Held report's age.</summary>
    public string PageKey => this.advanceBinding + "#" + this.pageSeq;
    /// <summary>Review R5 S5: the dwell AdvanceDue applied to the current page (the Held report waits for the same one).</summary>
    public long LastDwellMs { get; private set; }
    public bool DecisionLease(string binding, long now, int timeoutMs)
    {
        if (timeoutMs < 0 || timeoutMs > 120000) return false;
        if (timeoutMs > 0 && this.nightSummary && binding == this.advanceBinding
            && now - this.advanceSince >= NightProgressionMs) return false;
        this.decisionBinding = binding;
        this.decisionUntil = now + timeoutMs;
        return true;
    }
    public bool Deciding(string binding, long now) => binding == this.decisionBinding && now < this.decisionUntil;

}

public static class GenericUiReceipt
{
    public static (bool Waiting, string? Fault) SettleWorldClose(bool closedToHud, bool successor,
        bool lockHeld, bool worldFree, long elapsedMs)
    {
        if (!closedToHud || successor) return (false, "world_control_unverified");
        if (!lockHeld && worldFree) return (false, null);
        if (elapsedMs < 4000) return (true, null);
        return (false, lockHeld ? "mutex_release_unverified" : "world_control_unverified");
    }

    /// <summary>
    /// ADR 250 review B4/S1: the press was refused before any edge reached the game, so it gets the named not-delivered
    /// receipt (<see cref="StaleComponent"/>). Stale and item-policy faults are pre-edge by construction. A missing hook or
    /// an owner gesture counts only while nothing was delivered; after an edge it stays the uncertain judge's.
    /// </summary>
    public static bool NotDelivered(string? fault, bool delivered) =>
        fault == "stale_component" || fault?.StartsWith("item_policy:", StringComparison.Ordinal) == true
        || !delivered && (fault == UiMotorFrontPath.Unavailable || fault == UiMotorFrontPath.SeatUnavailable
            || fault?.StartsWith(UiMotorFrontPath.OwnerHeldPrefix, StringComparison.Ordinal) == true);

    /// <summary>
    /// Review W1b should-fix (profession seat): an internal ui_key (the profession pick) that reached no game update
    /// because the seat was missing or refused invoked nothing. Its result is a named zero-press refusal
    /// (<c>PRECONDITION_UNAVAILABLE(ui_key_not_delivered:&lt;fault&gt;)</c>), never an uncertain one. Null = judge as before.
    /// </summary>
    public static string? UiKeyNotDelivered(string? fault, bool delivered) =>
        !delivered && fault is not null && NotDelivered(fault, delivered)
            ? BgReason.PreconditionUnavailable("ui_key_not_delivered:" + fault) : null;

    /// <summary>ADR 250 review S3: the receipt of an Escape aimed at a minigame, from the pinned minigame (MinigameExit.Receipt).</summary>
    public static UiPadVerdict MinigameEscape(string state, UiPadFacts before, UiPadFacts after, bool delivered)
    {
        bool closed = delivered && state == MinigameExit.ReceiptClosed, replaced = delivered && state == MinigameExit.ReceiptReplaced;
        string reason = !delivered ? "minigame_escape_not_delivered" : "minigame_" + state;
        // W1 S1: the same receiver and ledger evidence as the judge's receipt, so Live7 can adopt a verified close and
        // treat an ignored Escape (still_running) as recoverable even when HUD text drifted in the same frames.
        return new UiPadVerdict(closed || replaced ? UiPadVerdict.PressApplied : UiPadVerdict.PressNoEffect, closed || replaced,
            closed ? "closed" : replaced ? "replaced" : null,
            delivered ? new[] { "minigame:receiveKeyPress:Escape" } : Array.Empty<string>(), reason,
            after.SnappedId, after.MenuOpen ? after.MenuType : null, after.ReceiverToken,
            JsonSerializer.Serialize(new { before = before.GenericValues, after = after.GenericValues,
                before_state = before.GenericLedger, after_state = after.GenericLedger,
                before_components = before.GenericState, after_components = after.GenericState,
                before_receiver = before.ReceiverToken, after_receiver = after.ReceiverToken,
                result_menu = after.MenuOpen ? after.MenuType : null,
                effect = closed ? "closed" : replaced ? "replaced" : "no_effect", minigame = state, delivered, released = true }));
    }

    public static UiPadVerdict StaleComponent(UiPadFacts after, string reason = "stale_component") => new(UiPadVerdict.PressNoEffect, false, null,
        Array.Empty<string>(), reason, after.SnappedId, after.MenuOpen ? after.MenuType : null,
        after.ReceiverToken, JsonSerializer.Serialize(new { effect = "no_effect", delivered = false, released = true }));
    public static UiPadVerdict Judge(UiPadFacts before, UiPadFacts after, string operation, bool released,
        string? fault = null, int componentId = -1, bool delivered = true,
        IReadOnlyList<GenericUiSuccessor>? succession = null)
    {
        string? effect = UiPadExecutor.PressEffect(before, after);
        effect = ReadyCheckObservation.Effect(before, after, effect, out bool readinessUnreadable);
        fault ??= readinessUnreadable ? "readiness_readback_unavailable" : null;
        bool readinessLost = effect == "readiness_lost";
        bool uncertain = !released || fault is not null || !delivered && effect is not null;
        if (CursorItemSettle.ItemOperation(operation) is not null)
        {
            var destination = before.CursorSettle?.Destinations.SingleOrDefault(d => d.Id == componentId);
            var item = CursorItemSettle.Verify(before.CursorSettle, after.CursorSettle, destination);
            bool unsafeItem = uncertain || item.Status == "Uncertain";
            bool moved = !unsafeItem && item.Status is "Settled" or "Partial" or "Swapped";
            return new UiPadVerdict(unsafeItem ? UiPadVerdict.PressUncertain : moved ? UiPadVerdict.PressApplied : UiPadVerdict.PressNoEffect,
                moved, moved ? CursorItemSettle.Effect(item.Status) : null,
                new[] { "virtual_frame:" + operation, "release:" + released }, fault ?? item.Refusal ?? item.Status,
                after.SnappedId, after.MenuOpen ? after.MenuType : null, after.ReceiverToken,
                JsonSerializer.Serialize(new { cursor_settle = item, before = before.GenericValues, after = after.GenericValues,
                    released, before_state = before.GenericLedger, after_state = after.GenericLedger,
                    effect = moved ? item.Status : "no_effect" }));
        }
        return new UiPadVerdict(uncertain ? UiPadVerdict.PressUncertain : effect is null || readinessLost
            ? UiPadVerdict.PressNoEffect : UiPadVerdict.PressApplied, !uncertain && effect is not null && !readinessLost,
            uncertain ? null : effect, new[] { "virtual_frame:" + operation, "release:" + released },
            fault ?? (readinessLost ? "readiness_lost" : released ? effect is null ? "no_state_change" : "effect:" + effect : "release_unverified"),
            after.SnappedId, after.MenuOpen ? after.MenuType : null, after.ReceiverToken,
            JsonSerializer.Serialize(new { before = before.GenericValues, after = after.GenericValues,
                before_state = before.GenericLedger, after_state = after.GenericLedger,
                before_components = before.GenericState, after_components = after.GenericState,
                before_receiver = before.ReceiverToken, after_receiver = after.ReceiverToken,
                result_menu = after.MenuOpen ? after.MenuType : null, effect = effect ?? "no_effect", released, delivered,
                succession = succession ?? Array.Empty<GenericUiSuccessor>() }));
    }
}

/// <summary>
/// Owns WHEN the UI motor's synthetic input frame occupies the game's single input slot
/// (<c>Game1.input</c> plus its old mouse/keyboard history). Installed SMAPI 4.5.2 casts that slot
/// to its own sealed internal <c>SInputState</c> at several points of a tick (<c>TrueUpdate</c>
/// before UpdateTicking, <c>ApplyOverrides</c> after it, <c>IInputHelper</c> reads in any handler).
/// A frame left in the slot across those points makes SMAPI throw <c>InvalidCastException</c> and
/// skip the whole base game update. So a frame is only STAGED by the tick handler and seated for
/// exactly two native windows: the direct edge dispatch (<see cref="Seat"/>/<see cref="Unseat"/>)
/// and the vanilla game update body (<see cref="EnterGameUpdate"/>/<see cref="ExitGameUpdate"/>,
/// called from a prefix/finalizer on that method). Everything outside those windows sees the
/// host's own input object. Generic: no menu, event or location knowledge.
/// </summary>
public sealed class GenericUiInputSeat<TInput, THistory> : IUiMotorSeatState where TInput : class
{
    public enum Release { NotSeated, Restored, OwnershipLost }
    readonly Func<TInput> readSlot;
    readonly Action<TInput> writeSlot;
    readonly Func<THistory> readHistory;
    readonly Action<THistory> writeHistory;
    TInput? frame, previous, seated;
    bool updateConsumed;
    THistory frameHistory = default!, previousHistory = default!;

    public GenericUiInputSeat(Func<TInput> readSlot, Action<TInput> writeSlot, Func<THistory> readHistory, Action<THistory> writeHistory)
    { this.readSlot = readSlot; this.writeSlot = writeSlot; this.readHistory = readHistory; this.writeHistory = writeHistory; }

    public bool Staged => this.frame is not null;
    public bool Seated => this.seated is not null;

    /// <summary>Prepare the frame for this tick without touching the slot.</summary>
    public void Stage(TInput frame, THistory history)
    {
        if (this.seated is not null) throw new InvalidOperationException("ui_input_seat_busy");
        this.frame = frame; this.frameHistory = history; this.updateConsumed = false;
    }

    /// <summary>Put the staged frame in the slot. Idempotent while seated; false when nothing is staged.</summary>
    public bool Seat()
    {
        if (this.frame is null) return false;
        if (this.seated is not null) return true;
        this.previous = this.readSlot(); this.previousHistory = this.readHistory();
        this.seated = this.frame;
        this.writeSlot(this.frame); this.writeHistory(this.frameHistory);
        return true;
    }

    /// <summary>Give the slot back; the frame stays staged. Never overwrites a slot someone else replaced.</summary>
    public Release Unseat()
    {
        if (this.seated is not { } mine) return Release.NotSeated;
        bool owned = ReferenceEquals(this.readSlot(), mine);
        if (owned) { this.writeSlot(this.previous!); this.writeHistory(this.previousHistory); }
        this.seated = null; this.previous = null; this.previousHistory = default!;
        return owned ? Release.Restored : Release.OwnershipLost;
    }

    /// <summary>Unseat and drop the staged frame (frame end, abort, cancellation).</summary>
    public Release Clear()
    {
        var released = this.Unseat();
        this.frame = null; this.frameHistory = default!;
        return released;
    }

    /// <summary>Game update prefix: seat the staged frame for the native body only.</summary>
    public bool EnterGameUpdate(bool eligible)
    {
        if (!eligible || this.updateConsumed || this.frame is null || this.seated is not null) return false;
        return this.Seat();
    }

    /// <summary>Game update finalizer: runs on success AND on exception, before any later host code.</summary>
    public Release ExitGameUpdate() { this.updateConsumed = true; return this.Unseat(); }
}
