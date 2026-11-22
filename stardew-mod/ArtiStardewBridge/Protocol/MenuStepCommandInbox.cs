using System.Text.Json;
using System.Text.Json.Serialization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Existing single-slot command inbox. Protocol-v1 legacy callers keep the
/// historical in-memory 256-id fail-closed behavior. Journal-capable leased
/// mutations add durable admission/settlement metadata through
/// <see cref="CommandSettledJournal"/> without adding a second ingress,
/// result mailbox, action owner, or native motor.
/// </summary>
public sealed class MenuStepCommandInbox
{
    public const int ProtocolVersion = 1;
    public const string OpMenuStep = "menu_step";
    public const string OpWaterOnce = "water_once";
    public const string OpWorldPress = "world_press";
    public const string OpInputOnce = "input_once";
    public const string FamilyWorldKey = "world_key";
    public const string PrimitiveMoveCardinal = "move_cardinal";
    public const string PrimitiveUseTool = "use_tool";
    public const string OpInspectTiles = InspectTilesQuery.Op;
    public const string OpInspectContext = InspectContextQuery.Op;
    public const string ObservedWatered = "ObservedWatered";
    public const string PressCompleted = "PressCompleted";
    /// <summary>WS3a move_through success outcome (observed expected location transition).</summary>
    public const string TransitionObserved = "TransitionObserved";
    /// <summary>Expected transition observed; an arrival event / menu holds the player.</summary>
    public const string TransitionObservedEventUp = "TransitionObservedEventUp";
    public const string Inspected = InspectTilesQuery.OutcomeInspected;
    public const string ContextInspected = InspectContextQuery.OutcomeInspected;

    public const int MaxTileCoordinate = 10000;
    public const string RequestFileName = "request.json";
    public const string ResultFileName = "result.json";
    public const string SessionFileName = "session.json";
    public const string ControlFileName = "control.json";
    public const string StopFileName = "stop.json";
    public const int MaxRequestBytes = 4096;
    public const int MaxRequestIdLength = 128;

    /// <summary>
    /// Compatibility ceiling for legacy arbitrary-id requests only. Journal
    /// mode does not consume this set and therefore does not dead-end at 256.
    /// </summary>
    public const int MaxRememberedRequestIds = 256;

    private readonly HashSet<string> terminalRequestIds = new(StringComparer.Ordinal);

    /// <summary>
    /// D21 (live round 4, 2026-10-08): operator <c>inspect_tiles</c> calls filled the 256-id mutation
    /// set and every later legacy request was refused CommandSessionCapacityReached. Read-only inspect
    /// ids live in this separate bounded ring (oldest evicted) and never count toward the mutation cap;
    /// mutation ids keep the fail-closed set (an evicted mutation id could replay an input).
    /// </summary>
    public const int MaxRememberedInspectIds = 4096;
    private readonly HashSet<string> inspectRequestIds = new(StringComparer.Ordinal);
    private readonly string?[] inspectRequestRing = new string?[MaxRememberedInspectIds];
    private int inspectRequestNext;
    private readonly CommandSettledJournal settledJournal;
    private readonly InboxClient inboxClient;
    public ExecutionSlot ExecutionSlot { get; }
    private MenuStepCommandRequest? inflight => this.ExecutionSlot.ActiveAttempt is { Client: "inbox", ResultRecorded: false } a ? a.Request : null;
    private int inflightRunId;
    private bool inflightRunIdKnown;

    /// <summary>
    /// PART_C2 (live C2, 2026-10-18): an isolated, per-instance journal file. The live mod passes
    /// <see cref="CommandSettledJournal.DefaultPath"/> explicitly (ModEntry). Before this, every test
    /// harness that built a parameterless inbox loaded and re-persisted the LIVE
    /// %LOCALAPPDATA%\ArtiStardewBridge journal: a concurrent test run recovered the game's in-flight
    /// Admitted row as Uncertain on disk and the game's own finalize write failed, which latched
    /// JournalPersistenceFault for the rest of the game process.
    /// </summary>
    public MenuStepCommandInbox()
        : this(new CommandSettledJournal(CommandSettledJournal.IsolatedPath()))
    {
    }

    internal string JournalPath => this.settledJournal.PathName;
    public int JournalUnresolvedCount => this.settledJournal.UnresolvedRowCount;
    public int JournalUnresolvedLimit => this.settledJournal.MaxUnresolvedRows;
    public string? JournalBlockedReason => this.settledJournal.BlockedReason;
    /// <summary>fix10e: native day boundary / save retires input-released uncertainty with a recorded outcome.</summary>
    public int RetireReleasedUncertain(string reason) => this.settledJournal.RetireReleasedUncertain(reason);
    public bool BindJournalScope(CommandInboxIdentity identity) => this.settledJournal.BindScope(identity);

    public MenuStepCommandResult ResolveMovementReadback(MenuStepCommandRequest request, MenuStepCommandResult result,
        BgMoveCompletionFacts facts, CommandInboxIdentity identity, long completedUnixMs) =>
        IsJournalMutation(request)
            ? this.settledJournal.ResolveMovementReadback(request, facts, identity, completedUnixMs) ?? result : result;

    public MenuStepCommandInbox(CommandSettledJournal settledJournal)
    {
        this.settledJournal = settledJournal
            ?? throw new ArgumentNullException(nameof(settledJournal));
        this.ExecutionSlot = new ExecutionSlot(settledJournal);
        this.inboxClient = new InboxClient(this.ExecutionSlot);
    }

    public bool HasInflight => this.inflight is not null;
    public string? InflightRequestId => this.inflight?.RequestId;
    public string? InflightOp => this.inflight?.Op;

    public bool IsTerminalRequestId(string? requestId) =>
        requestId is { Length: > 0 } id && (this.terminalRequestIds.Contains(id) || this.inspectRequestIds.Contains(id));

    public int TerminalRequestIdCount => this.terminalRequestIds.Count;

    public int InspectRequestIdCount => this.inspectRequestIds.Count;

    public bool SessionCapacityReached =>
        this.terminalRequestIds.Count >= MaxRememberedRequestIds;

    /// <summary>
    /// Called only at the same boundary that rotates commandSessionId. Legacy
    /// dedupe may clear because the old session no longer binds. Journal rows
    /// may likewise retire here; an old request still fails SessionMismatch.
    /// </summary>
    public void ResetForNewSession()
    {
        this.terminalRequestIds.Clear();
        this.inspectRequestIds.Clear();
        Array.Clear(this.inspectRequestRing);
        this.inspectRequestNext = 0;
        this.ClearInflight();
        this.settledJournal.ResetForNewSession();
    }

    public bool TryGetInflightRunId(out int runId)
    {
        runId = this.inflightRunId;
        return this.inflightRunIdKnown;
    }

    public static bool TryValidateInputOnceCurrentBinding(
        in MenuStepCommandRequest pinned,
        CommandInboxIdentity current,
        bool worldReady,
        bool isPlayerFree,
        bool canPlayerMove,
        out CommandRejectReason reason)
    {
        reason = CommandRejectReason.None;
        if (!IsInputOnce(pinned.Op))
        {
            reason = CommandRejectReason.UnsupportedOp;
            return false;
        }
        // WS-G4: a reflex_fish request stays bound while its own presses put the rod in use (canMove false); its
        // admission checks player-free itself (FishReflexAdmission) before the first press.
        if (!worldReady || (!IsContainerTransfer(pinned) && !IsContainerExit(pinned) && !IsUiKey(pinned) && !IsUiPad(pinned)
                && !IsReflex(pinned) && (!isPlayerFree || !canPlayerMove)))
        {
            reason = CommandRejectReason.BindingMismatch;
            return false;
        }
        if (current.IsMainPlayer || current.IsSplitScreen)
        {
            reason = CommandRejectReason.PlayerMismatch;
            return false;
        }
        if (current.SessionId.Length == 0
            || !string.Equals(current.SessionId, pinned.SessionId, StringComparison.Ordinal))
        {
            reason = CommandRejectReason.SessionMismatch;
            return false;
        }
        if (pinned.ExpectedPlayerId != current.PlayerId)
        {
            reason = CommandRejectReason.PlayerMismatch;
            return false;
        }
        if (pinned.ExpectedPlayerName is { Length: > 0 } wanted
            && !string.Equals(wanted, current.PlayerName, StringComparison.Ordinal))
        {
            reason = CommandRejectReason.PlayerMismatch;
            return false;
        }
        if (pinned.ExpectedLocationId.Length == 0
            || current.LocationId.Length == 0
            || !string.Equals(current.LocationId, pinned.ExpectedLocationId, StringComparison.Ordinal))
        {
            reason = CommandRejectReason.LocationMismatch;
            return false;
        }
        if (pinned.ExpectedScreenId != current.ScreenId)
        {
            reason = CommandRejectReason.ScreenMismatch;
            return false;
        }
        if (current.RuntimeInstanceId.Length > 0
            && !string.Equals(
                current.RuntimeInstanceId,
                pinned.RuntimeInstanceId,
                StringComparison.Ordinal))
        {
            reason = CommandRejectReason.RuntimeMismatch;
            return false;
        }
        if (current.ContextGeneration != pinned.ContextGeneration)
        {
            reason = CommandRejectReason.BindingMismatch;
            return false;
        }
        return true;
    }

    public bool TryValidateInflightInputOnceCurrentBinding(
        CommandInboxIdentity current,
        bool worldReady,
        bool isPlayerFree,
        bool canPlayerMove,
        out CommandRejectReason reason)
    {
        reason = CommandRejectReason.None;
        if (this.inflight is not { } pinned || !IsInputOnce(pinned.Op))
            return true;
        return TryValidateInputOnceCurrentBinding(
            pinned,
            current,
            worldReady,
            isPlayerFree,
            canPlayerMove,
            out reason);
    }

    public static bool TryParse(
        string? raw,
        out MenuStepCommandRequest request,
        out CommandRejectReason reason)
    {
        request = default;
        reason = CommandRejectReason.MalformedJson;
        if (string.IsNullOrWhiteSpace(raw))
            return false;

        JsonDocument doc;
        try
        {
            doc = JsonDocument.Parse(raw);
        }
        catch (JsonException)
        {
            return false;
        }

        using (doc)
        {
            JsonElement root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                return false;

            string requestId = TryString(root, "request_id") ?? "";
            if (requestId.Length == 0 || requestId.Length > MaxRequestIdLength)
            {
                reason = CommandRejectReason.MissingRequestId;
                return false;
            }
            request = request with { RequestId = requestId };

            if (!TryInt(root, "protocol", out int protocol) || protocol != ProtocolVersion)
            {
                reason = CommandRejectReason.UnsupportedProtocol;
                return false;
            }

            string op = TryString(root, "op") ?? "";
            if (op == "verb") return VerbRequest.TryParse(raw!, out request, out reason);
            request = request with { Op = op };
            if (string.Equals(op, OpInspectTiles, StringComparison.Ordinal))
                return TryParseInspectTiles(root, requestId, out request, out reason);
            if (string.Equals(op, OpInspectContext, StringComparison.Ordinal))
                return TryParseInspectContext(root, requestId, out request, out reason);
            if (string.Equals(op, OpInputOnce, StringComparison.Ordinal))
                return TryParseInputOnce(root, requestId, out request, out reason);
            if (string.Equals(op, OpWorldPress, StringComparison.Ordinal))
                return TryParseWorldPress(root, requestId, out request, out reason);
            if (string.Equals(op, OpWaterOnce, StringComparison.Ordinal))
                return TryParseWaterOnce(root, requestId, out request, out reason);

            if (!string.Equals(op, OpMenuStep, StringComparison.Ordinal))
            {
                reason = CommandRejectReason.UnsupportedOp;
                return false;
            }
            if (!ControllerStickHoldProbe.TryNormalizeDirection(
                    TryString(root, "direction"),
                    out string direction))
            {
                reason = CommandRejectReason.InvalidDirection;
                return false;
            }

            int timeoutTicks = ControllerMenuStepProbe.DefaultTimeoutTicks;
            if (Present(root, "timeout_ticks")
                && (!TryInt(root, "timeout_ticks", out timeoutTicks)
                    || timeoutTicks < ControllerMenuStepProbe.MinTimeoutTicks
                    || timeoutTicks > ControllerMenuStepProbe.MaxTimeoutTicks))
            {
                reason = CommandRejectReason.InvalidTimeout;
                return false;
            }

            int delayTicks = 0;
            if (Present(root, "delay_ticks")
                && (!TryInt(root, "delay_ticks", out delayTicks)
                    || delayTicks < ControllerMenuStepProbe.MinDelayTicks
                    || delayTicks > ControllerMenuStepProbe.MaxDelayTicks))
            {
                reason = CommandRejectReason.InvalidDelay;
                return false;
            }

            if (!TryLong(root, "deadline_unix_ms", out long deadline)
                || !TryLong(root, "expected_player_id", out long expectedPlayerId))
            {
                reason = CommandRejectReason.MalformedJson;
                return false;
            }

            request = new MenuStepCommandRequest(
                RequestId: requestId,
                SessionId: TryString(root, "session_id") ?? "",
                ExpectedPlayerId: expectedPlayerId,
                ExpectedPlayerName: TryString(root, "expected_player_name"),
                Direction: direction,
                TimeoutTicks: timeoutTicks,
                DelayTicks: delayTicks,
                DeadlineUnixMs: deadline,
                Op: OpMenuStep);
            reason = CommandRejectReason.None;
            return true;
        }
    }

    public CommandAdmission Admit(
        string? raw,
        CommandInboxIdentity identity,
        bool enabled,
        bool otherInputBusy,
        long nowUnixMs,
        NativeActionOwner? actionOwner = null) => this.Admit(
            raw,
            identity,
            inboxEnabled: enabled,
            menuStepEnabled: enabled,
            waterOnceEnabled: enabled,
            worldPressEnabled: enabled,
            otherInputBusy,
            nowUnixMs,
            actionOwner);

    public static bool TryParseControl(
        string? raw,
        out ActionOwnerControl control,
        out CommandRejectReason reason) =>
        ActionOwnerControl.TryParse(raw, out control, out reason);

    public static bool TryParseStop(
        string? raw,
        out ActionOwnerStop stop,
        out CommandRejectReason reason) =>
        ActionOwnerStop.TryParse(raw, out stop, out reason);

    public CommandAdmission Admit(
        string? raw,
        CommandInboxIdentity identity,
        bool inboxEnabled,
        bool menuStepEnabled,
        bool waterOnceEnabled,
        bool worldPressEnabled,
        bool otherInputBusy,
        long nowUnixMs,
        NativeActionOwner? actionOwner = null,
        bool useToolEnabled = false,
        bool selectItemEnabled = false,
        bool worldInteractionEnabled = false,
        bool containerTransferEnabled = false, bool containerExitEnabled = false,
        bool uiKeyEnabled = false, bool padButtonEnabled = false, bool uiPadEnabled = false, bool chatSendEnabled = false,
        bool reflexFishEnabled = false, bool reflexCombatEnabled = false, bool reflexChargeEnabled = false)
    {
        this.ExecutionSlot.RefusalResult = null;
        if (raw is not null && raw.Length > MaxRequestBytes)
            return CommandAdmission.Reject(CommandRejectReason.MalformedJson, default);
        if (!TryParse(raw, out MenuStepCommandRequest request, out CommandRejectReason parseError))
            return CommandAdmission.Reject(parseError, request);

        bool journalMode = IsJournalMutation(request);
        if (journalMode)
        {
            CommandJournalLookup lookup = this.settledJournal.Lookup(request);
            CommandRejectReason journalReason = lookup.Kind switch
            {
                CommandJournalLookupKind.CachedSettled => CommandRejectReason.JournalDuplicateSettled,
                CommandJournalLookupKind.OutstandingUncertain => CommandRejectReason.JournalUncertain,
                CommandJournalLookupKind.SettledCompacted => CommandRejectReason.JournalSettledCompacted,
                CommandJournalLookupKind.IdentityMismatch => CommandRejectReason.JournalIdentityMismatch,
                CommandJournalLookupKind.SequenceConflict => CommandRejectReason.JournalSequenceConflict,
                CommandJournalLookupKind.Faulted => CommandRejectReason.JournalPersistenceFault,
                _ => CommandRejectReason.None,
            };
            if (journalReason != CommandRejectReason.None)
                return CommandAdmission.Reject(journalReason, request);
        }
        else
        {
            if (this.IsTerminalRequestId(request.RequestId))
                return CommandAdmission.Reject(CommandRejectReason.DuplicateRequestId, request);
            if (this.SessionCapacityReached && !IsReadOnlyInspect(request.Op))
            {
                return CommandAdmission.Reject(
                    CommandRejectReason.CommandSessionCapacityReached,
                    request);
            }
        }

        if (!inboxEnabled || !LegacyFormMap.Enabled(request, new Dictionary<string, bool> {
            ["menu_step"] = menuStepEnabled, ["water_once"] = waterOnceEnabled, ["move"] = worldPressEnabled,
            ["use"] = useToolEnabled, ["select"] = selectItemEnabled, ["interact"] = worldInteractionEnabled,
            ["transfer"] = containerTransferEnabled, ["container_exit"] = containerExitEnabled,
            ["ui_key"] = uiKeyEnabled, ["pad_button"] = padButtonEnabled, ["ui"] = uiPadEnabled,
            ["fish"] = reflexFishEnabled, ["combat"] = reflexCombatEnabled, ["charge"] = reflexChargeEnabled }))
            return CommandAdmission.Reject(CommandRejectReason.InboxDisabled, request);
        if (!this.inboxClient.TryAdmit(request, identity, actionOwner, nowUnixMs))
            return CommandAdmission.Reject(Enum.Parse<CommandRejectReason>(this.ExecutionSlot.RefusalResult!.RejectReason!), request);
        this.inflightRunId = 0;
        this.inflightRunIdKnown = false;
        return CommandAdmission.Accept(this.ExecutionSlot.ActiveAttempt!.Request);
    }

    static bool IsInspectTiles(string? op) =>
        string.Equals(op, OpInspectTiles, StringComparison.Ordinal);

    static bool IsInspectContext(string? op) =>
        string.Equals(op, OpInspectContext, StringComparison.Ordinal);

    static bool IsInputOnce(string? op) =>
        string.Equals(op, OpInputOnce, StringComparison.Ordinal);

    public static bool IsUseTool(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == PrimitiveUseTool;

    public static bool IsSelectItem(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == SelectItemInput.Primitive;

    public static bool IsWorldInteraction(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == WorldInteractionTarget.Primitive;

    /// <summary>WS3a: walk-onto warp through the background move run (family world_key).</summary>
    public static bool IsMoveThrough(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == MoveThroughTarget.Primitive;

    /// <summary>PM-1 fix5 (ADR-23 Q14): every crossing (walk-onto warp/bed, or the action press on a warp door) rides the seat path.</summary>
    public static bool IsSeatCrossing(in MenuStepCommandRequest request) => IsMoveThrough(request)
        || IsWorldInteraction(request) && request.InteractionTarget?.Kind == WorldInteractionKinds.Door;

    public static bool IsContainerTransfer(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == ContainerTransferTarget.Primitive;

    public static bool IsContainerExit(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == ContainerExitTarget.Primitive;

    /// <summary>WS2: one direct menu-method call; exempt from player-free/move like container ops.</summary>
    public static bool IsUiKey(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == UiKeyTarget.Primitive;

    /// <summary>Primitives v2 WS-G0: one world pad button as a direct call (LB/RB/Start/Y/Select; family world_key).</summary>
    public static bool IsPadButton(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == PadButtonTarget.Primitive;

    /// <summary>Primitives v2 WS-G3: one chat line through ChatBox.textBoxEnter (family world_key; owner D5, default OFF).</summary>
    public static bool IsChatSend(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == ChatSendTarget.Primitive;

    /// <summary>Primitives v2 WS-G1: one snap step on the active menu (family ui_nav).</summary>
    public static bool IsUiNav(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == UiNavTarget.Primitive;

    /// <summary>Primitives v2 WS-G1: one mirrored menu button on the snapped component (family ui_press).</summary>
    public static bool IsUiPress(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == UiPressTarget.Primitive;

    /// <summary>WS-G1 menu layer (ui_nav / ui_press): exempt from player-free/move like ui_key.</summary>
    public static bool IsUiPad(in MenuStepCommandRequest request) => IsUiNav(request) || IsUiPress(request);

    /// <summary>Primitives v2 WS-G4: the fishing reflex (one model call -> one bounded mod loop -> one verdict).</summary>
    public static bool IsReflexFish(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == ReflexFishTarget.Primitive;

    /// <summary>Primitives v2 WS-G5: the combat reflex (face / swing / retreat in the mod, one verdict).</summary>
    public static bool IsReflexCombat(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == ReflexCombatTarget.Primitive;

    /// <summary>Primitives v2 WS-G5: the tool-charge reflex (X held to a toolPower level, one verdict).</summary>
    public static bool IsReflexCharge(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) && request.Primitive == ReflexChargeTarget.Primitive;

    /// <summary>Any reflex request (its own admission checks player-free before the first press).</summary>
    public static bool IsReflex(in MenuStepCommandRequest request) =>
        IsReflexFish(request) || IsReflexCombat(request) || IsReflexCharge(request);

    static bool IsLeasedWaterOnce(in MenuStepCommandRequest request) =>
        string.Equals(request.Op, OpWaterOnce, StringComparison.Ordinal)
        && request.RuntimeInstanceId.Length > 0;

    static bool IsLeasedMutation(in MenuStepCommandRequest request) =>
        IsInputOnce(request.Op) || IsLeasedWaterOnce(request);

    static bool IsJournalMutation(in MenuStepCommandRequest request) =>
        (request.VerbEnvelope is not null || IsLeasedMutation(request)) && CommandSettledJournal.IsJournalRequest(request);

    static bool IsReadOnlyInspect(string? op) =>
        IsInspectTiles(op) || IsInspectContext(op);

    public void BindRun(int runId)
    {
        if (this.inflight is null)
            return;
        this.inflightRunId = runId;
        this.inflightRunIdKnown = true;
    }

    public MenuStepCommandResult CompleteRejected(
        MenuStepCommandRequest request,
        CommandRejectReason reason,
        CommandInboxIdentity identity,
        long completedUnixMs,
        string? armStatus = null, string? refusalCode = null, string? debtId = null)
    {
        bool held = this.inflight is { } current
            && string.Equals(current.RequestId, request.RequestId, StringComparison.Ordinal);

        // The journal answers duplicates/replays of a request this inbox does NOT currently hold.
        // The request it holds was durably admitted by TryBegin a moment ago (state Admitted), so a
        // post-admission arm refusal must settle that row as rejected below, never bounce off the
        // Admitted row as "JournalOutstandingUncertain" and leave the slot inflight forever
        // (live M1 attempts 9g/9h, 2026-10-06: a refused background use_tool hold did exactly that).
        if (!held && IsJournalMutation(request))
        {
            MenuStepCommandResult? recovered = this.settledJournal.BuildResponse(
                request,
                identity,
                completedUnixMs);
            if (recovered is not null)
                return this.TypeRefusal(request, recovered);
        }

        MenuStepCommandResult result = new()
        {
            RequestId = request.RequestId is { Length: > 0 } rid ? rid : null,
            SessionId = identity.SessionId,
            Op = request.Op is { Length: > 0 } op ? op : OpMenuStep,
            Accepted = false,
            Status = MenuStepCommandResult.StatusRejected,
            RejectReason = reason.ToString(),
            ArmStatus = armStatus,
            Success = false,
            CompletedUnixMs = completedUnixMs,
            RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };

        // UI pre-arm refusals need the same correlation pins as applied UI results.
        // This asserts no input/cleanup effect; the caller still verifies unchanged
        // receiver state and releases its lease before treating the refusal as clean.
        if (IsUiPress(request) && request.UiPress is { } press)
            result = result with {
                Primitive = request.Primitive, RuntimeInstanceId = request.RuntimeInstanceId,
                ContextGeneration = request.ContextGeneration, PlayerId = request.ExpectedPlayerId,
                ScreenId = request.ExpectedScreenId, LocationId = request.ExpectedLocationId,
                InputArgs = press.Args(),
            };

        result = this.TypeRefusal(request, result, held, refusalCode, debtId);

        if (held && IsJournalMutation(request))
        {
            result = this.FinalizeJournalOrUncertain(
                request,
                result,
                uncertain: false,
                identity,
                completedUnixMs);
        }
        else if (!IsJournalMutation(request))
        {
            this.MarkLegacyTerminal(request);
        }

        if (held)
            this.ClearInflight();
        return result;
    }

    private MenuStepCommandResult TypeRefusal(MenuStepCommandRequest request, MenuStepCommandResult result,
        bool held = false, string? code = null, string? debtId = null)
    {
        if (result.Status != MenuStepCommandResult.StatusRejected || result.RefusalEnvelope is not null) return result;
        if (request.VerbEnvelope is null && (result.RejectReason == nameof(CommandRejectReason.UnsupportedOp)
            || result.RejectReason == nameof(CommandRejectReason.InboxDisabled))) code ??= LegacyFormMap.Resolve(request).Refusal;
        var refusal = AdmissionRefusals.Classify(result.RejectReason ?? "", result.ArmStatus, code, debtId);
        var stamp = this.ExecutionSlot.ObserveRefusal?.Invoke() ?? (Epoch: 0L, Tick: 0L);
        return this.ExecutionSlot.FinishResult(request, AdmissionRefusals.Attach(result, request, refusal, stamp.Epoch, stamp.Tick, held, this.ExecutionSlot.ObserveDebts?.Invoke()));
    }

    public MenuStepCommandResult CompleteVerdict(
        MenuStepVerdict verdict,
        CommandInboxIdentity identity,
        long completedUnixMs)
    {
        MenuStepCommandRequest request = this.inflight ?? default;
        MenuStepCommandResult result = new()
        {
            RequestId = request.RequestId is { Length: > 0 } rid ? rid : null,
            SessionId = identity.SessionId,
            Op = request.Op is { Length: > 0 } op ? op : OpMenuStep,
            Accepted = true,
            Status = MenuStepCommandResult.StatusCompleted,
            RejectReason = null,
            ArmStatus = StickHoldStatus.Started.ToString(),
            RunId = verdict.RunId,
            Outcome = verdict.Outcome.ToString(),
            Success = verdict.Success,
            Moved = verdict.Moved,
            NeighborMatched = verdict.NeighborMatched,
            ChangeSeen = verdict.ChangeSeen,
            TimeoutReached = verdict.TimeoutReached,
            StartSnapId = verdict.StartSnapId,
            EndSnapId = verdict.EndSnapId,
            ExpectedNeighborId = verdict.ExpectedNeighborId,
            FirstChangedSnapId = verdict.FirstChangedSnapId,
            PressCount = verdict.PressCount,
            CompletedUnixMs = completedUnixMs,
            RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };
        result = this.FinalizeIfJournal(
            request,
            result,
            uncertain: false,
            identity,
            completedUnixMs);
        this.MarkLegacyIfNeeded(request);
        this.ClearInflight();
        return result;
    }

    public MenuStepCommandResult CompleteWaterStatus(
        string waterStatus,
        CommandInboxIdentity identity,
        long completedUnixMs)
    {
        MenuStepCommandRequest request = this.inflight ?? default;
        int? runId = this.inflightRunIdKnown ? this.inflightRunId : null;
        bool observed = string.Equals(waterStatus, ObservedWatered, StringComparison.Ordinal);
        MenuStepCommandResult result = new()
        {
            RequestId = request.RequestId is { Length: > 0 } rid ? rid : null,
            SessionId = identity.SessionId,
            Op = OpWaterOnce,
            Accepted = true,
            Status = MenuStepCommandResult.StatusCompleted,
            RejectReason = null,
            ArmStatus = "Queued",
            RunId = runId,
            Outcome = waterStatus,
            Success = observed,
            CompletedUnixMs = completedUnixMs,
            RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };
        result = this.FinalizeIfJournal(
            request,
            result,
            uncertain: !observed,
            identity,
            completedUnixMs);
        this.MarkLegacyIfNeeded(request);
        this.ClearInflight();
        return result;
    }

    public MenuStepCommandResult CompleteWorldPress(
        string outcome,
        int pressCount,
        string? direction,
        CommandInboxIdentity identity,
        long completedUnixMs,
        MovePathReport? path = null)
    {
        MenuStepCommandRequest request = this.inflight ?? default;
        int? runId = this.inflightRunIdKnown ? this.inflightRunId : null;
        bool completed = string.Equals(outcome, PressCompleted, StringComparison.Ordinal);
        MenuStepCommandResult result = new()
        {
            RequestId = request.RequestId is { Length: > 0 } rid ? rid : null,
            SessionId = identity.SessionId,
            Op = request.Op is { Length: > 0 } op ? op : OpWorldPress,
            Accepted = true,
            Status = MenuStepCommandResult.StatusCompleted,
            RejectReason = null,
            ArmStatus = ProbePressStatus.Started.ToString(),
            RunId = runId,
            Outcome = outcome,
            Success = completed,
            Direction = direction is { Length: > 0 } dir ? dir : request.Direction,
            PressCount = pressCount,
            Path = path, // fix92A / ADR 162
            CompletedUnixMs = completedUnixMs,
            RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };
        result = this.FinalizeIfJournal(
            request,
            result,
            uncertain: false,
            identity,
            completedUnixMs);
        this.MarkLegacyIfNeeded(request);
        this.ClearInflight();
        return result;
    }

    public MenuStepCommandResult CompleteUseTool(UseToolInputVerdict verdict,
        CommandInboxIdentity identity, long completedUnixMs)
    {
        if (this.inflight is not { } request || !IsUseTool(request)
            || request.RequestId != verdict.RequestId || request.ToolTarget is not { } tool)
            throw new InvalidOperationException("use-tool verdict does not own inbox request");
        bool completed = verdict.Outcome == "InputCompleted" && verdict.CleanupConfirmed
            && verdict.PressCount == request.Ticks;
        // Same rule as CompleteWorldInteraction: an "InputUncertain" outcome is uncertain
        // even when cleanup was confirmed (V4 Stage 2 background verdicts: swing count != 1,
        // tool still in use, pin drift). The foreground invocation only emits InputUncertain
        // with CleanupConfirmed=false, so its results are unchanged.
        bool uncertain = !verdict.CleanupConfirmed || verdict.Outcome == "InputUncertain";
        MenuStepCommandResult result = new()
        {
            RequestId = request.RequestId, SessionId = request.SessionId,
            Op = OpInputOnce, Primitive = PrimitiveUseTool, Accepted = true,
            Status = uncertain ? MenuStepCommandResult.StatusUncertain
                : completed ? MenuStepCommandResult.StatusCompleted : MenuStepCommandResult.StatusInterrupted,
            Outcome = verdict.Outcome, Success = completed, PressCount = verdict.PressCount,
            CleanupConfirmed = verdict.CleanupConfirmed, WorldEffectStatus = "unverified",
            RejectReason = verdict.Reason, CompletedUnixMs = completedUnixMs,
            PlayerId = request.ExpectedPlayerId, ScreenId = request.ExpectedScreenId,
            RuntimeInstanceId = request.RuntimeInstanceId, ContextGeneration = request.ContextGeneration,
            LocationId = request.ExpectedLocationId,
            InputArgs = new Dictionary<string, object> {
                ["ticks"] = request.Ticks, ["slot"] = tool.Slot,
                ["qualified_item_id"] = tool.QualifiedItemId, ["tile_x"] = tool.TileX,
                ["tile_y"] = tool.TileY, ["facing"] = tool.Facing },
            RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };
        result = this.FinalizeIfJournal(request, result, uncertain, identity, completedUnixMs);
        this.MarkLegacyIfNeeded(request);
        this.ClearInflight();
        return result;
    }

    public MenuStepCommandResult CompleteSelection(SelectItemVerdict verdict,
        CommandInboxIdentity identity, long completedUnixMs)
    {
        if (this.inflight is not { } request || !IsSelectItem(request)
            || request.SelectionTarget is not { } target)
            throw new InvalidOperationException("selection verdict does not own inbox request");
        bool uncertain = verdict.Outcome == "SelectionUncertain" || !verdict.CleanupConfirmed;
        MenuStepCommandResult result = new() {
            RequestId = request.RequestId, SessionId = request.SessionId,
            Op = OpInputOnce, Primitive = SelectItemInput.Primitive, Accepted = true,
            Status = uncertain ? MenuStepCommandResult.StatusUncertain : MenuStepCommandResult.StatusCompleted,
            Outcome = verdict.Outcome, Success = verdict.Selected, PressCount = verdict.ClickCount,
            CleanupConfirmed = verdict.CleanupConfirmed,
            WorldEffectStatus = verdict.Selected ? "observed_selection" : "unverified",
            CompletedUnixMs = completedUnixMs, PlayerId = request.ExpectedPlayerId,
            ScreenId = request.ExpectedScreenId, RuntimeInstanceId = request.RuntimeInstanceId,
            ContextGeneration = request.ContextGeneration, LocationId = request.ExpectedLocationId,
            InputArgs = target.Args(), RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };
        result = this.FinalizeIfJournal(request, result, uncertain, identity, completedUnixMs);
        this.MarkLegacyIfNeeded(request);
        this.ClearInflight();
        return result;
    }

    public MenuStepCommandResult CompleteExit(ContainerExitVerdict verdict,
        CommandInboxIdentity identity,long completedUnixMs)
    {
        if(this.inflight is not { } request || !IsContainerExit(request) || request.ExitTarget is not { } target)
            throw new InvalidOperationException("exit verdict does not own inbox request");
        bool uncertain=verdict.ClickCount>0&&!verdict.Exited || !verdict.CleanupConfirmed;
        MenuStepCommandResult result=new() {
            RequestId=request.RequestId,SessionId=request.SessionId,Op=OpInputOnce,
            Primitive=ContainerExitTarget.Primitive,Accepted=true,
            Status=uncertain?MenuStepCommandResult.StatusUncertain:MenuStepCommandResult.StatusCompleted,
            Outcome=verdict.Outcome,Success=verdict.Exited,PressCount=verdict.ClickCount,
            CleanupConfirmed=verdict.CleanupConfirmed,InputSettled=verdict.CleanupConfirmed&&verdict.ClickCount==1,
            ReceiverTransitionStatus=verdict.ExpectedTransition?"ExpectedReceiverTransition":"UnverifiedReceiverTransition",
            WorldEffectStatus="unverified",CompletedUnixMs=completedUnixMs,
            PlayerId=request.ExpectedPlayerId,ScreenId=request.ExpectedScreenId,RuntimeInstanceId=request.RuntimeInstanceId,
            ContextGeneration=request.ContextGeneration,LocationId=request.ExpectedLocationId,InputArgs=target.Args(),
            ResultReceiverToken=null,ResultContextGeneration=verdict.ResultContextGeneration,
            RequestSeq=request.RequestSeq>0?request.RequestSeq:null };
        result=this.FinalizeIfJournal(request,result,uncertain,identity,completedUnixMs);
        this.MarkLegacyIfNeeded(request);this.ClearInflight();return result;
    }

    /// <summary>
    /// WS-G0 pad_button verdict after exactly one direct call (rejections before the call use
    /// <see cref="CompleteRejected"/>). No device state exists, so cleanup is always confirmed.
    /// </summary>
    public MenuStepCommandResult CompletePadButton(PadButtonVerdict verdict, CommandInboxIdentity identity, long completedUnixMs)
    {
        if (this.inflight is not { } request || !IsPadButton(request) || request.PadButton is not { } target)
            throw new InvalidOperationException("pad_button verdict does not own inbox request");
        bool uncertain = verdict.IsUncertain;
        MenuStepCommandResult result = new()
        {
            RequestId = request.RequestId, SessionId = request.SessionId, Op = OpInputOnce,
            Primitive = PadButtonTarget.Primitive, Accepted = true,
            Status = uncertain ? MenuStepCommandResult.StatusUncertain : MenuStepCommandResult.StatusCompleted,
            Outcome = verdict.Outcome, Success = verdict.Applied, PressCount = verdict.InvokeCount,
            CleanupConfirmed = true, InputSettled = !uncertain,
            WorldEffectStatus = verdict.Applied ? "observed_pad_button" : verdict.Outcome == PadButtonVerdict.NoEffect ? "unchanged" : "unverified",
            CompletedUnixMs = completedUnixMs, PlayerId = request.ExpectedPlayerId, ScreenId = request.ExpectedScreenId,
            RuntimeInstanceId = request.RuntimeInstanceId, ContextGeneration = request.ContextGeneration,
            LocationId = request.ExpectedLocationId, InputArgs = target.Args(),
            ResultMenuType = verdict.ResultMenuType, UiKeyEffect = verdict.Detail,
            RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };
        result = this.FinalizeIfJournal(request, result, uncertain, identity, completedUnixMs);
        this.MarkLegacyIfNeeded(request);
        this.ClearInflight();
        return result;
    }

    /// <summary>
    /// WS-G3 chat_send verdict after exactly one ChatBox.textBoxEnter(text) (rejections before the call use
    /// <see cref="CompleteRejected"/>). No device state exists, so cleanup is always confirmed.
    /// </summary>
    public MenuStepCommandResult CompleteChatSend(ChatSendVerdict verdict, CommandInboxIdentity identity, long completedUnixMs)
    {
        if (this.inflight is not { } request || !IsChatSend(request) || request.ChatSend is not { } target)
            throw new InvalidOperationException("chat_send verdict does not own inbox request");
        bool uncertain = verdict.IsUncertain;
        MenuStepCommandResult result = new()
        {
            RequestId = request.RequestId, SessionId = request.SessionId, Op = OpInputOnce,
            Primitive = ChatSendTarget.Primitive, Accepted = true,
            Status = uncertain ? MenuStepCommandResult.StatusUncertain : MenuStepCommandResult.StatusCompleted,
            Outcome = verdict.Outcome, Success = verdict.Applied, PressCount = verdict.InvokeCount,
            CleanupConfirmed = true, InputSettled = !uncertain,
            WorldEffectStatus = verdict.Applied ? "observed_chat_send" : verdict.Outcome == ChatSendVerdict.NoEffect ? "unchanged" : "unverified",
            CompletedUnixMs = completedUnixMs, PlayerId = request.ExpectedPlayerId, ScreenId = request.ExpectedScreenId,
            RuntimeInstanceId = request.RuntimeInstanceId, ContextGeneration = request.ContextGeneration,
            LocationId = request.ExpectedLocationId, InputArgs = target.Args(), UiKeyEffect = verdict.Detail,
            RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };
        result = this.FinalizeIfJournal(request, result, uncertain, identity, completedUnixMs);
        this.MarkLegacyIfNeeded(request);
        this.ClearInflight();
        return result;
    }

    /// <summary>
    /// WS-G1 ui_nav / ui_press verdict after the mirrored calls (rejections before any call use
    /// <see cref="CompleteRejected"/>). Direct calls only, no device state: cleanup is always confirmed.
    /// </summary>
    public MenuStepCommandResult CompleteUiPad(UiPadVerdict verdict, CommandInboxIdentity identity, long completedUnixMs,
        long? resultContextGeneration)
    {
        if (this.inflight is not { } request || !IsUiPad(request))
            throw new InvalidOperationException("ui_pad verdict does not own inbox request");
        Dictionary<string, object> args = request.UiNav is { } nav ? nav.Args()
            : request.UiPress is { } press ? press.Args() : throw new InvalidOperationException("ui_pad target missing");
        bool uncertain = verdict.Uncertain;
        string? resultReceiver = verdict.ResultReceiverToken is { Length: > 0 } r && r.StartsWith("rcv:", StringComparison.Ordinal)
            ? r : null;
        string observed = IsUiNav(request) ? "observed_ui_nav" : "observed_ui_press";
        MenuStepCommandResult result = new()
        {
            RequestId = request.RequestId, SessionId = request.SessionId, Op = OpInputOnce,
            Primitive = request.Primitive, Accepted = true,
            Status = uncertain ? MenuStepCommandResult.StatusUncertain : MenuStepCommandResult.StatusCompleted,
            Outcome = verdict.Outcome, Success = verdict.Applied, PressCount = verdict.Calls.Count(c => !c.StartsWith("skip:", StringComparison.Ordinal) && !c.StartsWith("stop:", StringComparison.Ordinal)),
            CleanupConfirmed = true, InputSettled = !uncertain,
            WorldEffectStatus = verdict.Applied ? observed : verdict.NoEffect ? "unchanged" : "unverified",
            CompletedUnixMs = completedUnixMs, PlayerId = request.ExpectedPlayerId, ScreenId = request.ExpectedScreenId,
            RuntimeInstanceId = request.RuntimeInstanceId, ContextGeneration = request.ContextGeneration,
            LocationId = request.ExpectedLocationId, InputArgs = args,
            ResultReceiverToken = resultReceiver, ResultContextGeneration = resultContextGeneration,
            UiKeyEffect = verdict.Effect, ResultMenuType = verdict.ResultMenuType,
            UiPadCalls = verdict.Calls.ToArray(), UiPadSnappedAfter = verdict.SnappedAfter, UiPadReason = verdict.Reason,
            UiPadReadback = verdict.Readback,
            UiPadEconomy = verdict.Economy?.Wire(),
            RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };
        result = this.FinalizeIfJournal(request, result, uncertain, identity, completedUnixMs);
        this.MarkLegacyIfNeeded(request);
        this.ClearInflight();
        return result;
    }

    /// <summary>
    /// WS-G4 reflex_fish verdict (rejections before the first press use <see cref="CompleteRejected"/>). Outcome =
    /// the verdict kind; success only for FishCaught with every reflex press released and proved
    /// (<paramref name="cleanupConfirmed"/>). An unproved release is uncertain, never completed.
    /// </summary>
    public MenuStepCommandResult CompleteReflexFish(string verdict, bool cleanupConfirmed, int pressCount,
        Dictionary<string, object> reflex, CommandInboxIdentity identity, long completedUnixMs)
    {
        if (this.inflight is not { } request || !IsReflexFish(request) || request.ReflexFish is not { } args)
            throw new InvalidOperationException("reflex_fish verdict does not own inbox request");
        if (!ReflexContracts.TryParseVerdict(verdict, out string kind, out _) || !ReflexContracts.VerdictAllowedFor(ReflexContracts.RequestFish, kind))
            throw new ArgumentException("not a reflex_fish verdict: " + verdict, nameof(verdict));
        bool caught = kind == ReflexContracts.FishCaught;
        bool uncertain = !cleanupConfirmed;
        MenuStepCommandResult result = new()
        {
            RequestId = request.RequestId, SessionId = request.SessionId, Op = OpInputOnce,
            Primitive = ReflexFishTarget.Primitive, Accepted = true,
            Status = uncertain ? MenuStepCommandResult.StatusUncertain : MenuStepCommandResult.StatusCompleted,
            Outcome = kind, Success = caught && cleanupConfirmed, PressCount = pressCount,
            CleanupConfirmed = cleanupConfirmed, InputSettled = cleanupConfirmed,
            WorldEffectStatus = !cleanupConfirmed ? "unverified" : caught ? "observed_fish_caught" : "unchanged",
            CompletedUnixMs = completedUnixMs, PlayerId = request.ExpectedPlayerId, ScreenId = request.ExpectedScreenId,
            RuntimeInstanceId = request.RuntimeInstanceId, ContextGeneration = request.ContextGeneration,
            LocationId = request.ExpectedLocationId, InputArgs = ReflexFishTarget.Args(args),
            ReflexVerdict = verdict, Reflex = reflex,
            RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };
        result = this.FinalizeIfJournal(request, result, uncertain, identity, completedUnixMs);
        this.MarkLegacyIfNeeded(request);
        this.ClearInflight();
        return result;
    }

    /// <summary>
    /// WS-G5 reflex_combat / reflex_charge verdict (rejections before the first press use <see cref="CompleteRejected"/>).
    /// Outcome = the verdict kind; success only for MonsterKilled(n) / ChargeReached(n) with every reflex press released and
    /// proved (<paramref name="cleanupConfirmed"/>) and, for the charge, at least one area tile changed
    /// (<paramref name="effectObserved"/>). An unproved release is uncertain, never completed.
    /// </summary>
    public MenuStepCommandResult CompleteReflexG5(string verdict, bool cleanupConfirmed, int pressCount, bool effectObserved,
        Dictionary<string, object> reflex, CommandInboxIdentity identity, long completedUnixMs)
    {
        if (this.inflight is not { } request || !(IsReflexCombat(request) || IsReflexCharge(request)))
            throw new InvalidOperationException("reflex_combat/charge verdict does not own inbox request");
        bool combat = IsReflexCombat(request);
        string family = combat ? ReflexContracts.RequestCombat : ReflexContracts.RequestCharge;
        if (!ReflexContracts.TryParseVerdict(verdict, out string kind, out _) || !ReflexContracts.VerdictAllowedFor(family, kind))
            throw new ArgumentException("not a " + family + " verdict: " + verdict, nameof(verdict));
        bool success = cleanupConfirmed && (combat ? kind == ReflexContracts.MonsterKilled : kind == ReflexContracts.ChargeReached && effectObserved);
        string effect = !cleanupConfirmed ? "unverified"
            : combat ? (kind == ReflexContracts.MonsterKilled ? "observed_monster_killed" : kind == ReflexContracts.Downed ? "observed_downed" : "unchanged")
            : (effectObserved ? "observed_charge_area" : "unchanged");
        MenuStepCommandResult result = new()
        {
            RequestId = request.RequestId, SessionId = request.SessionId, Op = OpInputOnce,
            Primitive = family, Accepted = true,
            Status = cleanupConfirmed ? MenuStepCommandResult.StatusCompleted : MenuStepCommandResult.StatusUncertain,
            Outcome = kind, Success = success, PressCount = pressCount,
            CleanupConfirmed = cleanupConfirmed, InputSettled = cleanupConfirmed,
            WorldEffectStatus = effect,
            CompletedUnixMs = completedUnixMs, PlayerId = request.ExpectedPlayerId, ScreenId = request.ExpectedScreenId,
            RuntimeInstanceId = request.RuntimeInstanceId, ContextGeneration = request.ContextGeneration,
            LocationId = request.ExpectedLocationId,
            InputArgs = combat ? ReflexCombatTarget.Args(request.ReflexCombat!.Value) : ReflexChargeTarget.Args(request.ReflexCharge!.Value),
            ReflexVerdict = verdict, Reflex = reflex,
            RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };
        result = this.FinalizeIfJournal(request, result, !cleanupConfirmed, identity, completedUnixMs);
        this.MarkLegacyIfNeeded(request);
        this.ClearInflight();
        return result;
    }

    /// <summary>
    /// WS2 ui_key verdict after exactly one invocation (rejections before any invocation use
    /// <see cref="CompleteRejected"/>). No device state exists, so cleanup is always confirmed.
    /// </summary>
    public MenuStepCommandResult CompleteUiKey(UiKeyVerdict verdict, CommandInboxIdentity identity,
        long completedUnixMs, long? resultContextGeneration, string? actorContentsBefore = null,
        string? actorContentsAfter = null)
    {
        if (this.inflight is not { } request || !IsUiKey(request) || request.UiKeyTarget is not { } target)
            throw new InvalidOperationException("ui_key verdict does not own inbox request");
        bool uncertain = verdict.Uncertain;
        string? resultReceiver = verdict.ResultReceiverToken is { Length: > 0 } r && r.StartsWith("rcv:", StringComparison.Ordinal)
            ? r : null;
        MenuStepCommandResult result = new()
        {
            RequestId = request.RequestId, SessionId = request.SessionId, Op = OpInputOnce,
            Primitive = UiKeyTarget.Primitive, Accepted = true,
            Status = uncertain ? MenuStepCommandResult.StatusUncertain : MenuStepCommandResult.StatusCompleted,
            Outcome = verdict.Outcome, Success = verdict.Applied, PressCount = verdict.InvokeCount,
            CleanupConfirmed = true, InputSettled = verdict.Settled,
            ReceiverTransitionStatus = verdict.ReceiverTransitionStatus,
            WorldEffectStatus = verdict.Applied ? "observed_ui_key" : verdict.Outcome == UiKeyVerdict.OutcomeNoEffect ? "unchanged" : "unverified",
            CompletedUnixMs = completedUnixMs, PlayerId = request.ExpectedPlayerId, ScreenId = request.ExpectedScreenId,
            RuntimeInstanceId = request.RuntimeInstanceId, ContextGeneration = request.ContextGeneration,
            LocationId = request.ExpectedLocationId, InputArgs = target.Args(),
            ResultReceiverToken = resultReceiver, ResultContextGeneration = resultContextGeneration,
            UiKeyEffect = verdict.Effect, ResultMenuType = verdict.ResultMenuType, ProfessionId = verdict.ProfessionId,
            ActorContentsBefore = actorContentsBefore, ActorContentsAfter = actorContentsAfter,
            RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };
        result = this.FinalizeIfJournal(request, result, uncertain, identity, completedUnixMs);
        this.MarkLegacyIfNeeded(request);
        this.ClearInflight();
        return result;
    }

    public MenuStepCommandResult CompleteTransfer(ContainerTransferVerdict verdict,
        CommandInboxIdentity identity,long completedUnixMs)
    {
        if(this.inflight is not { } request || !IsContainerTransfer(request) || request.TransferTarget is not { } target)
            throw new InvalidOperationException("transfer verdict does not own inbox request");
        bool uncertain=verdict.Outcome=="TransferUncertain" || !verdict.CleanupConfirmed;
        var result=new MenuStepCommandResult {
            RequestId=request.RequestId,SessionId=request.SessionId,Op=OpInputOnce,
            Primitive=ContainerTransferTarget.Primitive,Accepted=true,
            Status=uncertain?MenuStepCommandResult.StatusUncertain:MenuStepCommandResult.StatusCompleted,
            Outcome=verdict.Outcome,Success=verdict.Paired,PressCount=verdict.ClickCount,
            CleanupConfirmed=verdict.CleanupConfirmed,InputSettled=verdict.CleanupConfirmed&&(verdict.ClickCount==1 || verdict.Outcome=="TransferNoEffect" && verdict.ClickCount==2), // fix10d put-back
            // WS-G2 ship modes: the bin menu is never replaced (zero transitions expected and observed).
            ReceiverTransitionStatus=verdict.NoTransitionExpected
                ? (verdict.Paired?"NoReceiverTransition":"UnverifiedReceiverTransition")
                : verdict.ExpectedTransition?"ExpectedReceiverTransition":"UnverifiedReceiverTransition",
            EffectDetail=verdict.Detail,
            WorldEffectStatus="unverified",CompletedUnixMs=completedUnixMs,
            PlayerId=request.ExpectedPlayerId,ScreenId=request.ExpectedScreenId,RuntimeInstanceId=request.RuntimeInstanceId,
            ContextGeneration=request.ContextGeneration,LocationId=request.ExpectedLocationId,InputArgs=target.Args(),
            ResultReceiverToken=verdict.ReceiverToken,ResultContextGeneration=verdict.ResultContextGeneration,
            // A paired receiver transition is the same chest's rebuilt ItemGrabMenu. Shipping does not rebuild it.
            ResultMenuType=verdict.Paired && verdict.ExpectedTransition ? "ItemGrabMenu" : null,
            ResultReadbackToken=verdict.ReadbackToken,RequestSeq=request.RequestSeq>0?request.RequestSeq:null };
        result=this.FinalizeIfJournal(request,result,uncertain,identity,completedUnixMs);
        this.MarkLegacyIfNeeded(request);this.ClearInflight();return result;
    }

    public MenuStepCommandResult CompleteWorldInteraction(WorldInteractionVerdict verdict,
        CommandInboxIdentity identity, long completedUnixMs)
    {
        if (this.inflight is not { } request || !IsWorldInteraction(request)
            || request.InteractionTarget is not { } target)
            throw new InvalidOperationException("interaction verdict does not own inbox request");
        bool uncertain = verdict.Outcome == "InteractionUncertain" || !verdict.CleanupConfirmed;
        MenuStepCommandResult result = new() {
            RequestId=request.RequestId, SessionId=request.SessionId, Op=OpInputOnce,
            Primitive=WorldInteractionTarget.Primitive, Accepted=true,
            Status=uncertain?MenuStepCommandResult.StatusUncertain:MenuStepCommandResult.StatusCompleted,
            Outcome=verdict.Outcome, Success=verdict.Opened, PressCount=verdict.PressCount,
            CleanupConfirmed=verdict.CleanupConfirmed, WorldEffectStatus="unverified",
            InputSettled=verdict.CleanupConfirmed && (target.Kind == AnimalHouseRoute.Kind
                ? verdict.PressCount is >= 1 and <= AnimalHouseRoute.MaxInputs : EventControlAction.IsApproach(target.Kind)
                ? verdict.PressCount is >= 1 and <= EventControlAction.MaxApproachPresses : target.Kind == EventControlAction.EggRoute
                ? verdict.PressCount is >= 1 and <= EventControlAction.MaxRoutePresses : verdict.PressCount==1),
            // WS3a: only a receiver kind (chest; WS3b npc / eat) reports a receiver transition; door/forage carry none.
            ReceiverTransitionStatus=!WorldInteractionKinds.HasReceiver(target.Kind) ? null
                : verdict.Opened?"ExpectedReceiverTransition":"UnverifiedReceiverTransition",
            Kind=target.Kind, ResultLocationId=verdict.ResultLocationId, EffectDetail=verdict.EffectDetail,
            CompletedUnixMs=completedUnixMs, PlayerId=request.ExpectedPlayerId, ScreenId=request.ExpectedScreenId,
            RuntimeInstanceId=request.RuntimeInstanceId, ContextGeneration=request.ContextGeneration,
            LocationId=request.ExpectedLocationId, InputArgs=target.Args(),
            ResultReceiverToken=verdict.ReceiverToken, ResultContextGeneration=verdict.ResultContextGeneration,
            RequestSeq=request.RequestSeq>0?request.RequestSeq:null,
        };
        result=this.FinalizeIfJournal(request,result,uncertain,identity,completedUnixMs);
        this.MarkLegacyIfNeeded(request); this.ClearInflight();
        return result;
    }

    /// <summary>
    /// WS3a move_through terminal. Success only for an observed transition to the expected
    /// location (<c>TransitionObserved</c>); a run that crossed no warp keeps the ordinary
    /// move outcome (never success); uncertain runs stay uncertain in the journal.
    /// </summary>
    public MenuStepCommandResult CompleteMoveThrough(string outcome, bool transition, bool uncertain, int pressCount,
        bool cleanupConfirmed, string? resultLocationId, CommandInboxIdentity identity, long completedUnixMs,
        string? receiverToken = null, long? resultContextGeneration = null)
    {
        if (this.inflight is not { } request || !IsMoveThrough(request) || request.ThroughTarget is not { } target)
            throw new InvalidOperationException("move_through verdict does not own inbox request");
        bool success = transition && !uncertain && cleanupConfirmed;
        MenuStepCommandResult result = new() {
            RequestId=request.RequestId, SessionId=request.SessionId, Op=OpInputOnce,
            Primitive=MoveThroughTarget.Primitive, Accepted=true,
            Status=uncertain?MenuStepCommandResult.StatusUncertain:MenuStepCommandResult.StatusCompleted,
            Outcome=!success?outcome:string.Equals(outcome,BgMoveThrough.WarpedEventUp,StringComparison.Ordinal)
                ?TransitionObservedEventUp:TransitionObserved, Success=success, PressCount=pressCount,
            CleanupConfirmed=cleanupConfirmed, WorldEffectStatus="unverified",
            InputSettled=cleanupConfirmed, Direction=request.Direction,
            CompletedUnixMs=completedUnixMs, PlayerId=request.ExpectedPlayerId, ScreenId=request.ExpectedScreenId,
            RuntimeInstanceId=request.RuntimeInstanceId, ContextGeneration=request.ContextGeneration,
            LocationId=request.ExpectedLocationId, InputArgs=target.Args(),
            Kind=target.Kind, ResultLocationId=success?resultLocationId:null,
            // WS3b bed: the Sleep question is the receiver ui_key answers next.
            ResultReceiverToken=success?receiverToken:null, ResultContextGeneration=success?resultContextGeneration:null,
            ReceiverTransitionStatus=success && receiverToken is not null ? "ExpectedReceiverTransition" : null,
            RequestSeq=request.RequestSeq>0?request.RequestSeq:null,
        };
        result=this.FinalizeIfJournal(request,result,uncertain,identity,completedUnixMs);
        this.MarkLegacyIfNeeded(request); this.ClearInflight();
        return result;
    }

    public MenuStepCommandResult CompleteInspect(
        IReadOnlyList<ScenePreparedFact> objects,
        IReadOnlyList<ScenePreparedFact> terrain,
        CommandInboxIdentity identity,
        string locationId,
        int capturedTicks,
        long completedUnixMs)
    {
        MenuStepCommandRequest request = this.inflight ?? default;
        this.MarkLegacyTerminal(request);
        this.ClearInflight();

        int maxFacts = InspectTilesQuery.MaxFactsForRadius(request.Radius);
        int total = objects.Count + terrain.Count;
        if (total > maxFacts)
            return InspectRejected(request, identity, completedUnixMs, CommandRejectReason.ResultTooLarge);

        CommandRejectReason? invalid = InspectTilesQuery.ValidateCapturedFacts(
            objects,
            terrain,
            request.CenterX,
            request.CenterY,
            request.Radius);
        if (invalid is { } bound)
            return InspectRejected(request, identity, completedUnixMs, bound);

        Dictionary<string, object> facts = InspectTilesQuery.AssembleFacts(objects, terrain);
        MenuStepCommandResult ok = new()
        {
            RequestId = request.RequestId is { Length: > 0 } rid ? rid : null,
            SessionId = identity.SessionId,
            Op = OpInspectTiles,
            Accepted = true,
            Status = MenuStepCommandResult.StatusCompleted,
            RejectReason = null,
            Success = true,
            Outcome = Inspected,
            CompletedUnixMs = completedUnixMs,
            PlayerId = identity.PlayerId,
            LocationId = locationId,
            CapturedTicks = capturedTicks,
            CenterX = request.CenterX,
            CenterY = request.CenterY,
            Radius = request.Radius,
            Facts = facts,
        };

        string json = JsonSerializer.Serialize(ok, ProtocolJson.Options);
        if (SceneWire.Utf8Bytes(json) > InspectTilesQuery.MaxResultBytes)
            return InspectRejected(request, identity, completedUnixMs, CommandRejectReason.ResultTooLarge);
        return this.ExecutionSlot.FinishResult(request, ok);
    }

    public MenuStepCommandResult CompleteInspectContext(
        Dictionary<string, object> context,
        CommandInboxIdentity identity,
        string locationId,
        int capturedTicks,
        long completedUnixMs,
        string runtimeInstanceId,
        long contextGeneration,
        long uiRevision,
        long viewportTransformRevision)
    {
        MenuStepCommandRequest request = this.inflight ?? default;
        this.MarkLegacyTerminal(request);
        this.ClearInflight();

        if (!InspectContextQuery.TryValidateBinding(
                context,
                identity,
                locationId,
                runtimeInstanceId,
                contextGeneration,
                uiRevision,
                viewportTransformRevision,
                out CommandRejectReason bindingReason))
        {
            return ContextRejected(request, identity, completedUnixMs, bindingReason);
        }
        if (!InspectContextQuery.ValidateSnapshotSize(context))
        {
            return ContextRejected(
                request,
                identity,
                completedUnixMs,
                CommandRejectReason.ResultTooLarge);
        }

        MenuStepCommandResult ok = new()
        {
            RequestId = request.RequestId is { Length: > 0 } rid ? rid : null,
            SessionId = identity.SessionId,
            Op = OpInspectContext,
            Accepted = true,
            Status = MenuStepCommandResult.StatusCompleted,
            RejectReason = null,
            Success = true,
            Outcome = ContextInspected,
            CompletedUnixMs = completedUnixMs,
            PlayerId = identity.PlayerId,
            ScreenId = identity.ScreenId,
            LocationId = locationId,
            CapturedTicks = capturedTicks,
            RuntimeInstanceId = runtimeInstanceId,
            ContextGeneration = contextGeneration,
            UiRevision = uiRevision,
            ViewportTransformRevision = viewportTransformRevision,
            Context = context,
        };

        string json = JsonSerializer.Serialize(ok, ProtocolJson.Options);
        if (SceneWire.Utf8Bytes(json) > InspectContextQuery.MaxResultBytes)
        {
            return ContextRejected(
                request,
                identity,
                completedUnixMs,
                CommandRejectReason.ResultTooLarge);
        }
        return this.ExecutionSlot.FinishResult(request, ok);
    }

    static MenuStepCommandResult ContextRejected(
        MenuStepCommandRequest request,
        CommandInboxIdentity identity,
        long completedUnixMs,
        CommandRejectReason reason) => new()
    {
        RequestId = request.RequestId is { Length: > 0 } rid ? rid : null,
        SessionId = identity.SessionId,
        Op = OpInspectContext,
        Accepted = false,
        Status = MenuStepCommandResult.StatusRejected,
        RejectReason = reason.ToString(),
        Success = false,
        CompletedUnixMs = completedUnixMs,
        PlayerId = identity.PlayerId,
        ScreenId = identity.ScreenId,
        LocationId = identity.LocationId is { Length: > 0 } loc ? loc : null,
    };

    static MenuStepCommandResult InspectRejected(
        MenuStepCommandRequest request,
        CommandInboxIdentity identity,
        long completedUnixMs,
        CommandRejectReason reason) => new()
    {
        RequestId = request.RequestId is { Length: > 0 } rid ? rid : null,
        SessionId = identity.SessionId,
        Op = OpInspectTiles,
        Accepted = false,
        Status = MenuStepCommandResult.StatusRejected,
        RejectReason = reason.ToString(),
        Success = false,
        CompletedUnixMs = completedUnixMs,
        PlayerId = identity.PlayerId,
        LocationId = identity.LocationId is { Length: > 0 } loc ? loc : null,
        CenterX = request.CenterX,
        CenterY = request.CenterY,
        Radius = request.Radius,
    };

    public MenuStepCommandResult? CompleteInterrupted(
        string alasan,
        CommandInboxIdentity identity,
        long completedUnixMs)
    {
        if (this.inflight is not { } request)
            return null;

        MenuStepCommandResult result = new()
        {
            RequestId = request.RequestId,
            SessionId = identity.SessionId,
            Op = request.Op is { Length: > 0 } op ? op : OpMenuStep,
            Accepted = true,
            Status = MenuStepCommandResult.StatusInterrupted,
            RejectReason = alasan,
            Success = false,
            CompletedUnixMs = completedUnixMs,
            RequestSeq = request.RequestSeq > 0 ? request.RequestSeq : null,
        };
        result = this.FinalizeIfJournal(
            request,
            result,
            uncertain: true,
            identity,
            completedUnixMs);
        this.MarkLegacyIfNeeded(request);
        this.ClearInflight();
        return result;
    }

    private MenuStepCommandResult FinalizeIfJournal(
        in MenuStepCommandRequest request,
        MenuStepCommandResult result,
        bool uncertain,
        CommandInboxIdentity identity,
        long completedUnixMs)
    {
        result = this.ExecutionSlot.FinishResult(request, result);
        if (!IsJournalMutation(request))
            return result;
        return this.FinalizeJournalOrUncertain(
            request,
            result,
            uncertain,
            identity,
            completedUnixMs);
    }

    private MenuStepCommandResult FinalizeJournalOrUncertain(
        in MenuStepCommandRequest request,
        MenuStepCommandResult result,
        bool uncertain,
        CommandInboxIdentity identity,
        long completedUnixMs)
    {
        result = this.ExecutionSlot.FinishResult(request, result);
        if (this.settledJournal.TryFinalize(
                request,
                result,
                uncertain,
                out CommandRejectReason journalReason))
        {
            return result with
            {
                RequestSeq = request.RequestSeq,
                JournalState = uncertain ? "uncertain" : "settled",
            };
        }

        return result with
        {
            Status = MenuStepCommandResult.StatusUncertain,
            Success = false,
            RejectReason = journalReason == CommandRejectReason.None
                ? CommandRejectReason.JournalPersistenceFault.ToString()
                : journalReason.ToString(),
            CompletedUnixMs = completedUnixMs,
            RequestSeq = request.RequestSeq,
            JournalState = "uncertain",
        };
    }

    private void MarkLegacyIfNeeded(in MenuStepCommandRequest request)
    {
        if (!IsJournalMutation(request))
            this.MarkLegacyTerminal(request);
    }

    private void MarkLegacyTerminal(in MenuStepCommandRequest request)
    {
        if (request.RequestId is not { Length: > 0 } id)
            return;
        if (IsReadOnlyInspect(request.Op))
        {
            // D21 (live round 4): read-only inspects get their own bounded ring. Evicting an old
            // inspect id is safe: replaying it can only re-run a read-only query, never an input.
            if (this.inspectRequestIds.Add(id))
            {
                if (this.inspectRequestRing[this.inspectRequestNext] is { } oldest)
                    this.inspectRequestIds.Remove(oldest);
                this.inspectRequestRing[this.inspectRequestNext] = id;
                this.inspectRequestNext = (this.inspectRequestNext + 1) % MaxRememberedInspectIds;
            }
            return;
        }
        if (this.SessionCapacityReached)
            return;
        this.terminalRequestIds.Add(id);
    }

    private void ClearInflight()
    {
        if (this.ExecutionSlot.ActiveAttempt is { } attempt) attempt.ResultRecorded = true;
        this.inflightRunId = 0;
        this.inflightRunIdKnown = false;
    }

    private static bool Present(JsonElement root, string name) =>
        root.TryGetProperty(name, out JsonElement el) && el.ValueKind != JsonValueKind.Null;

    private static bool TryInt(JsonElement root, string name, out int value)
    {
        value = 0;
        return root.TryGetProperty(name, out JsonElement el)
            && el.ValueKind == JsonValueKind.Number
            && el.TryGetInt32(out value);
    }

    private static bool TryLong(JsonElement root, string name, out long value)
    {
        value = 0;
        if (!Present(root, name))
            return true;
        JsonElement el = root.GetProperty(name);
        return el.ValueKind == JsonValueKind.Number && el.TryGetInt64(out value);
    }

    private static string? TryString(JsonElement root, string name) =>
        root.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? el.GetString()
            : null;

    static bool TryParseWorldPress(
        JsonElement root,
        string requestId,
        out MenuStepCommandRequest request,
        out CommandRejectReason reason)
    {
        request = request = new MenuStepCommandRequest(
            RequestId: requestId,
            SessionId: "",
            ExpectedPlayerId: 0,
            ExpectedPlayerName: null,
            Direction: "",
            TimeoutTicks: 0,
            DelayTicks: 0,
            DeadlineUnixMs: 0,
            Op: OpWorldPress);
        if (!WorldPressProbe.TryNormalizeDirection(
                TryString(root, "direction"),
                out string direction))
        {
            reason = CommandRejectReason.InvalidDirection;
            return false;
        }
        if (!TryInt(root, "ticks", out int ticks)
            || ticks < WorldPressProbe.MinTickCount
            || ticks > WorldPressProbe.MaxTickCount)
        {
            reason = CommandRejectReason.InvalidTickCount;
            return false;
        }
        if (!TryLong(root, "deadline_unix_ms", out long deadline)
            || !TryLong(root, "expected_player_id", out long playerId))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }
        request = new MenuStepCommandRequest(
            RequestId: requestId,
            SessionId: TryString(root, "session_id") ?? "",
            ExpectedPlayerId: playerId,
            ExpectedPlayerName: TryString(root, "expected_player_name"),
            Direction: direction,
            TimeoutTicks: 0,
            DelayTicks: 0,
            DeadlineUnixMs: deadline,
            Op: OpWorldPress,
            Ticks: ticks);
        reason = CommandRejectReason.None;
        return true;
    }

    static bool TryParseInspectTiles(
        JsonElement root,
        string requestId,
        out MenuStepCommandRequest request,
        out CommandRejectReason reason)
    {
        request = new MenuStepCommandRequest(
            requestId, "", 0, null, "", 0, 0, 0, OpInspectTiles);
        if (!TryInt(root, "center_x", out int centerX)
            || !TryInt(root, "center_y", out int centerY)
            || centerX < 0 || centerY < 0
            || centerX > MaxTileCoordinate || centerY > MaxTileCoordinate)
        {
            reason = CommandRejectReason.InvalidTile;
            return false;
        }
        if (!TryInt(root, "radius", out int radius)
            || radius < 0 || radius > InspectTilesQuery.MaxRadius)
        {
            reason = CommandRejectReason.InvalidRadius;
            return false;
        }
        string locationId = TryString(root, "expected_location_id") ?? "";
        if (locationId.Length == 0 || locationId.Length > InspectTilesQuery.MaxLocationIdLength)
        {
            reason = CommandRejectReason.LocationMismatch;
            return false;
        }
        if (!TryLong(root, "deadline_unix_ms", out long deadline)
            || !TryLong(root, "expected_player_id", out long expectedPlayerId))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }
        request = new MenuStepCommandRequest(
            RequestId: requestId,
            SessionId: TryString(root, "session_id") ?? "",
            ExpectedPlayerId: expectedPlayerId,
            ExpectedPlayerName: TryString(root, "expected_player_name"),
            Direction: "",
            TimeoutTicks: 0,
            DelayTicks: 0,
            DeadlineUnixMs: deadline,
            Op: OpInspectTiles,
            ExpectedLocationId: locationId,
            CenterX: centerX,
            CenterY: centerY,
            Radius: radius);
        reason = CommandRejectReason.None;
        return true;
    }

    static bool TryParseInspectContext(
        JsonElement root,
        string requestId,
        out MenuStepCommandRequest request,
        out CommandRejectReason reason)
    {
        string? decisionReceiver = TryString(root, "ui_decision_receiver");
        int decisionTimeout = 0;
        if ((Present(root, "ui_decision_receiver") || Present(root, "ui_decision_timeout_ms"))
            && (decisionReceiver is null || !decisionReceiver.StartsWith("rcv:", StringComparison.Ordinal)
                || decisionReceiver.Length > 128 || !TryInt(root, "ui_decision_timeout_ms", out decisionTimeout)
                || decisionTimeout < 0 || decisionTimeout > 120000))
        {
            request = default; reason = CommandRejectReason.MalformedJson; return false;
        }
        request = new MenuStepCommandRequest(
            requestId, "", 0, null, "", 0, 0, 0, OpInspectContext);
        string locationId = TryString(root, "expected_location_id") ?? "";
        if (locationId.Length == 0 || locationId.Length > InspectContextQuery.MaxLocationIdLength)
        {
            reason = CommandRejectReason.LocationMismatch;
            return false;
        }
        if (!TryInt(root, "expected_screen_id", out int expectedScreenId)
            || expectedScreenId < 0)
        {
            reason = CommandRejectReason.ScreenMismatch;
            return false;
        }
        if (!TryLong(root, "deadline_unix_ms", out long deadline)
            || !TryLong(root, "expected_player_id", out long expectedPlayerId))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }
        request = new MenuStepCommandRequest(
            RequestId: requestId,
            SessionId: TryString(root, "session_id") ?? "",
            ExpectedPlayerId: expectedPlayerId,
            ExpectedPlayerName: TryString(root, "expected_player_name"),
            Direction: "",
            TimeoutTicks: 0,
            DelayTicks: 0,
            DeadlineUnixMs: deadline,
            Op: OpInspectContext,
            ExpectedLocationId: locationId,
            ExpectedScreenId: expectedScreenId, UiDecisionReceiver: decisionReceiver, UiDecisionTimeoutMs: decisionTimeout);
        reason = CommandRejectReason.None;
        return true;
    }

    static readonly HashSet<string> LeasedWaterOnceAllowedKeys = new(StringComparer.Ordinal)
    {
        "protocol", "op", "request_id", "request_seq", "runtime_instance_id", "session_id",
        "owner_id", "generation", "control_seq", "context_generation", "expected_player_id",
        "expected_player_name", "expected_screen_id", "expected_location_id", "tile_x", "tile_y",
        "deadline_unix_ms",
    };

    static bool HasLeasedWaterOnceMarker(JsonElement root) =>
        root.TryGetProperty("runtime_instance_id", out _)
        || root.TryGetProperty("owner_id", out _)
        || root.TryGetProperty("generation", out _)
        || root.TryGetProperty("control_seq", out _)
        || root.TryGetProperty("context_generation", out _)
        || root.TryGetProperty("request_seq", out _);

    static bool TryParseWaterOnce(
        JsonElement root,
        string requestId,
        out MenuStepCommandRequest request,
        out CommandRejectReason reason)
    {
        request = new MenuStepCommandRequest(
            requestId, "", 0, null, "", 0, 0, 0, OpWaterOnce);

        bool leased = HasLeasedWaterOnceMarker(root);
        if (leased && HasUnknownProperty(root, LeasedWaterOnceAllowedKeys))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }
        if (!TryInt(root, "tile_x", out int tileX)
            || !TryInt(root, "tile_y", out int tileY)
            || tileX < 0 || tileY < 0
            || tileX > MaxTileCoordinate || tileY > MaxTileCoordinate)
        {
            reason = CommandRejectReason.InvalidTile;
            return false;
        }

        if (!leased)
        {
            if (!TryLong(root, "deadline_unix_ms", out long legacyDeadline)
                || !TryLong(root, "expected_player_id", out long legacyPlayerId))
            {
                reason = CommandRejectReason.MalformedJson;
                return false;
            }
            request = new MenuStepCommandRequest(
                RequestId: requestId,
                SessionId: TryString(root, "session_id") ?? "",
                ExpectedPlayerId: legacyPlayerId,
                ExpectedPlayerName: TryString(root, "expected_player_name"),
                Direction: "",
                TimeoutTicks: 0,
                DelayTicks: 0,
                DeadlineUnixMs: legacyDeadline,
                Op: OpWaterOnce,
                TileX: tileX,
                TileY: tileY);
            reason = CommandRejectReason.None;
            return true;
        }

        string runtime = TryString(root, "runtime_instance_id") ?? "";
        string session = TryString(root, "session_id") ?? "";
        string owner = TryString(root, "owner_id") ?? "";
        if (runtime.Length == 0 || runtime.Length > MaxRequestIdLength
            || session.Length == 0 || session.Length > MaxRequestIdLength
            || owner.Length == 0 || owner.Length > MaxRequestIdLength)
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }
        if (!TryRequiredLong(root, "generation", out long generation) || generation < 1
            || !TryRequiredLong(root, "control_seq", out long controlSeq) || controlSeq < 1
            || !TryRequiredLong(root, "context_generation", out long contextGeneration)
                || contextGeneration < 1
            || !TryRequiredLong(root, "deadline_unix_ms", out long deadline)
            || !TryRequiredLong(root, "expected_player_id", out long expectedPlayerId))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }

        long requestSeq = 0;
        if (Present(root, "request_seq")
            && (!TryRequiredLong(root, "request_seq", out requestSeq) || requestSeq < 1))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }

        string locationId = TryString(root, "expected_location_id") ?? "";
        if (locationId.Length == 0 || locationId.Length > InspectContextQuery.MaxLocationIdLength)
        {
            reason = CommandRejectReason.LocationMismatch;
            return false;
        }
        if (!TryInt(root, "expected_screen_id", out int expectedScreenId)
            || expectedScreenId < 0)
        {
            reason = CommandRejectReason.ScreenMismatch;
            return false;
        }
        if (Present(root, "expected_player_name")
            && TryString(root, "expected_player_name") is null)
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }

        request = new MenuStepCommandRequest(
            RequestId: requestId,
            SessionId: session,
            ExpectedPlayerId: expectedPlayerId,
            ExpectedPlayerName: TryString(root, "expected_player_name"),
            Direction: "",
            TimeoutTicks: 0,
            DelayTicks: 0,
            DeadlineUnixMs: deadline,
            Op: OpWaterOnce,
            TileX: tileX,
            TileY: tileY,
            ExpectedLocationId: locationId,
            ExpectedScreenId: expectedScreenId,
            RuntimeInstanceId: runtime,
            OwnerId: owner,
            Generation: generation,
            ControlSeq: controlSeq,
            ContextGeneration: contextGeneration,
            RequestSeq: requestSeq);
        reason = CommandRejectReason.None;
        return true;
    }

    static readonly HashSet<string> InputOnceAllowedKeys = new(StringComparer.Ordinal)
    {
        "protocol", "op", "request_id", "request_seq", "runtime_instance_id", "session_id",
        "owner_id", "generation", "control_seq", "context_generation", "expected_player_id",
        "expected_player_name", "expected_screen_id", "expected_location_id", "family", "primitive",
        "args", "deadline_unix_ms", "execution_deadline_unix_ms",
    };

    static readonly HashSet<string> InputOnceArgsAllowedKeys = new(StringComparer.Ordinal)
    {
        "direction", "ticks", "event_token", MoveSegmentStop.ArgName, MovePathStops.ArgName,
    };

    static readonly HashSet<string> UseToolArgsAllowedKeys = new(StringComparer.Ordinal)
    {
        "ticks", "slot", "qualified_item_id", "tile_x", "tile_y", "facing",
    };

    static bool TryParseInputOnce(
        JsonElement root,
        string requestId,
        out MenuStepCommandRequest request,
        out CommandRejectReason reason)
    {
        request = new MenuStepCommandRequest(
            requestId, "", 0, null, "", 0, 0, 0, OpInputOnce);
        if (HasUnknownProperty(root, InputOnceAllowedKeys))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }

        string family = TryString(root, "family") ?? "";
        string primitive = TryString(root, "primitive") ?? "";
        request = request with { Primitive = primitive };
        bool useTool = primitive == PrimitiveUseTool;
        bool selectItem = primitive == SelectItemInput.Primitive;
        bool interact = primitive == WorldInteractionTarget.Primitive;
        bool transfer = primitive == ContainerTransferTarget.Primitive;
        bool exit = primitive == ContainerExitTarget.Primitive;
        bool uiKey = primitive == UiKeyTarget.Primitive;
        bool through = primitive == MoveThroughTarget.Primitive;
        bool padButton = primitive == PadButtonTarget.Primitive; // WS-G0: family world_key, args {button}
        bool chatSend = primitive == ChatSendTarget.Primitive;   // WS-G3: family world_key, args {text} (owner D5)
        bool uiNav = primitive == UiNavTarget.Primitive;     // WS-G1: family ui_nav
        bool uiPress = primitive == UiPressTarget.Primitive; // WS-G1: family ui_press
        bool uiPad = uiNav || uiPress;
        bool reflexFish = primitive == ReflexFishTarget.Primitive; // WS-G4: family world_key, args {max_s, cast_power, treasure, hook_retries}
        bool reflexCombat = primitive == ReflexCombatTarget.Primitive; // WS-G5: args {max_s, hp_floor_pct, max_kills, retreat}
        bool reflexCharge = primitive == ReflexChargeTarget.Primitive; // WS-G5: args {target_level, max_ticks}
        bool reflexAny = reflexFish || reflexCombat || reflexCharge;
        if (family != (uiPad ? primitive : uiKey ? UiKeyTarget.Primitive : exit ? ContainerExitTarget.Primitive : transfer ? ContainerTransferTarget.Primitive : selectItem ? SelectItemInput.Primitive : FamilyWorldKey)
            || (!uiKey && !uiPad && !exit && !transfer && !interact && !selectItem && !useTool && !through && !padButton && !chatSend && !reflexAny && primitive != PrimitiveMoveCardinal))
        {
            reason = CommandRejectReason.UnsupportedOp;
            return false;
        }
        if (!root.TryGetProperty("args", out JsonElement args)
            || args.ValueKind != JsonValueKind.Object
            || (!uiKey && !uiPad && !exit && !transfer && !interact && !selectItem && !through && !padButton && !chatSend && !reflexAny && HasUnknownProperty(args, useTool ? UseToolArgsAllowedKeys : InputOnceArgsAllowedKeys)))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }
        string direction = "";
        if (!uiKey && !uiPad && !exit && !transfer && !interact && !useTool && !selectItem && !padButton && !chatSend && !reflexAny && !WorldPressProbe.TryNormalizeDirection(
                TryString(args, "direction"),
                out direction))
        {
            reason = CommandRejectReason.InvalidDirection;
            return false;
        }
        // fix89D / ADR 158: only a plain move_cardinal may name its straight segment's last tile;
        // then (and only then) the hold may run past 60 frames, up to MoveSegmentGuard.MaxTicks.
        MoveSegmentStop? segmentStop = null;
        if (Present(args, MoveSegmentStop.ArgName)
            && (primitive != PrimitiveMoveCardinal || Present(args, "event_token")
                || !MoveSegmentStop.TryParse(args, out segmentStop) || segmentStop is null))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }
        // fix92A / ADR 162: the following stops of a guarded path ride only with a segment_stop; the whole
        // path may then hold up to MovePath.MaxTicks frames. The polyline itself is checked at arm time.
        MovePathStops? pathNext = null;
        if (Present(args, MovePathStops.ArgName)
            && (segmentStop is null || !MovePathStops.TryParse(args, out pathNext) || pathNext is null))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }
        int ticks = 1;
        if (!uiKey && !uiPad && !exit && !transfer && !interact && !selectItem && !padButton && !chatSend && !reflexAny && (!TryInt(args, "ticks", out ticks)
            || ticks < WorldPressProbe.MinTickCount
            || ticks > (segmentStop is null ? WorldPressProbe.MaxTickCount : pathNext is null ? MoveSegmentGuard.MaxTicks : MovePath.MaxTicks)))
        {
            reason = CommandRejectReason.InvalidTickCount;
            return false;
        }

        string? eventToken = null;
        if (Present(args, "event_token"))
        {
            eventToken = TryString(args, "event_token");
            if (primitive != PrimitiveMoveCardinal || !EventControlMove.TokenShape(eventToken)
                || ticks > EventControlMove.MaxTicks)
            { reason = CommandRejectReason.MalformedJson; return false; }
        }

        SelectItemTarget selection = default;
        WorldInteractionTarget interaction = default;
        ContainerExitTarget? exitTarget = null;
        if (exit && !ContainerExitTarget.TryParse(args,out exitTarget))
        { reason=CommandRejectReason.MalformedJson; return false; }
        UiKeyTarget? uiKeyTarget = null;
        if (uiKey && !UiKeyTarget.TryParse(args, out uiKeyTarget))
        { reason = CommandRejectReason.MalformedJson; return false; }
        PadButtonTarget padTarget = default;
        if (padButton && !PadButtonTarget.TryParse(args, out padTarget))
        { reason = CommandRejectReason.MalformedJson; return false; }
        ChatSendTarget chatTarget = default;
        if (chatSend && !ChatSendTarget.TryParse(args, out chatTarget))
        { reason = CommandRejectReason.MalformedJson; return false; }
        UiNavTarget? uiNavTarget = null;
        if (uiNav && !UiNavTarget.TryParse(args, out uiNavTarget))
        { reason = CommandRejectReason.MalformedJson; return false; }
        UiPressTarget? uiPressTarget = null;
        if (uiPress && !UiPressTarget.TryParse(args, out uiPressTarget))
        { reason = CommandRejectReason.MalformedJson; return false; }
        FishReflexArgs fishArgs = default;
        if (reflexFish && !ReflexFishTarget.TryParse(args, out fishArgs))
        { reason = CommandRejectReason.MalformedJson; return false; }
        CombatReflexArgs combatArgs = default;
        if (reflexCombat && !ReflexCombatTarget.TryParse(args, out combatArgs))
        { reason = CommandRejectReason.MalformedJson; return false; }
        ChargeReflexArgs chargeArgs = default;
        if (reflexCharge && !ReflexChargeTarget.TryParse(args, out chargeArgs))
        { reason = CommandRejectReason.MalformedJson; return false; }
        ContainerTransferTarget transferTarget = default;
        if (transfer && !ContainerTransferTarget.TryParse(args,out transferTarget))
        {
            reason=CommandRejectReason.MalformedJson;
            return false;
        }
        if (interact && !WorldInteractionTarget.TryParse(args, out interaction))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }
        long? executionDeadline = null;
        if (Present(root, "execution_deadline_unix_ms"))
        {
            if (!interact || !(interaction.Kind == AnimalHouseRoute.Kind || EventControlAction.IsRoute(interaction.Kind))
                || !TryRequiredLong(root, "execution_deadline_unix_ms", out long execution) || execution <= 0)
            { reason = CommandRejectReason.MalformedJson; return false; }
            executionDeadline = execution;
        }
        MoveThroughTarget throughTarget = default;
        if (through && (!MoveThroughTarget.TryParse(args, out throughTarget)
            || !string.Equals(throughTarget.Direction, direction, StringComparison.Ordinal) || throughTarget.Ticks != ticks))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }
        if (selectItem && !SelectItemInput.TryParse(args, out selection))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }
        UseToolInputTarget tool = default;
        if (useTool)
        {
            if (!TryInt(args, "slot", out int slot)
                || !TryInt(args, "tile_x", out int x) || !TryInt(args, "tile_y", out int y)
                || !TryInt(args, "facing", out int facing)
                || TryString(args, "qualified_item_id") is not { } item
                || !(tool = new(slot, item, x, y, facing)).IsValid)
            {
                reason = CommandRejectReason.BindingMismatch;
                return false;
            }
        }

        string runtime = TryString(root, "runtime_instance_id") ?? "";
        string session = TryString(root, "session_id") ?? "";
        string owner = TryString(root, "owner_id") ?? "";
        if (runtime.Length == 0 || runtime.Length > MaxRequestIdLength
            || session.Length == 0 || session.Length > MaxRequestIdLength
            || owner.Length == 0 || owner.Length > MaxRequestIdLength)
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }
        if (!TryRequiredLong(root, "generation", out long generation) || generation < 1
            || !TryRequiredLong(root, "control_seq", out long controlSeq) || controlSeq < 1
            || !TryRequiredLong(root, "context_generation", out long contextGeneration)
                || contextGeneration < 1
            || !TryRequiredLong(root, "deadline_unix_ms", out long deadline)
            || !TryRequiredLong(root, "expected_player_id", out long expectedPlayerId))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }

        long requestSeq = 0;
        if (Present(root, "request_seq")
            && (!TryRequiredLong(root, "request_seq", out requestSeq) || requestSeq < 1))
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }

        string locationId = TryString(root, "expected_location_id") ?? "";
        if (locationId.Length == 0 || locationId.Length > InspectContextQuery.MaxLocationIdLength)
        {
            reason = CommandRejectReason.LocationMismatch;
            return false;
        }
        if (!TryInt(root, "expected_screen_id", out int expectedScreenId)
            || expectedScreenId < 0)
        {
            reason = CommandRejectReason.ScreenMismatch;
            return false;
        }
        if (Present(root, "expected_player_name")
            && TryString(root, "expected_player_name") is null)
        {
            reason = CommandRejectReason.MalformedJson;
            return false;
        }

        request = new MenuStepCommandRequest(
            RequestId: requestId,
            SessionId: session,
            ExpectedPlayerId: expectedPlayerId,
            ExpectedPlayerName: TryString(root, "expected_player_name"),
            Direction: direction,
            TimeoutTicks: 0,
            DelayTicks: 0,
            DeadlineUnixMs: deadline,
            Op: OpInputOnce,
            Ticks: ticks,
            ExpectedLocationId: locationId,
            ExpectedScreenId: expectedScreenId,
            Family: family,
            Primitive: primitive,
            ToolTarget: useTool ? tool : null,
            SelectionTarget: selectItem ? selection : null,
            InteractionTarget: interact ? interaction : null,
            TransferTarget: transfer ? transferTarget : null,
            ExitTarget: exitTarget,
            UiKeyTarget: uiKeyTarget,
            ThroughTarget: through ? throughTarget : null,
            PadButton: padButton ? padTarget : null,
            ChatSend: chatSend ? chatTarget : null,
            UiNav: uiNavTarget,
            UiPress: uiPressTarget,
            ReflexFish: reflexFish ? fishArgs : null,
            ReflexCombat: reflexCombat ? combatArgs : null,
            ReflexCharge: reflexCharge ? chargeArgs : null,
            EventToken: eventToken,
            SegmentStop: segmentStop,
            PathNext: pathNext,
            ExecutionDeadlineUnixMs: executionDeadline,
            RuntimeInstanceId: runtime,
            OwnerId: owner,
            Generation: generation,
            ControlSeq: controlSeq,
            ContextGeneration: contextGeneration,
            RequestSeq: requestSeq);
        reason = CommandRejectReason.None;
        return true;
    }

    static bool HasUnknownProperty(JsonElement obj, HashSet<string> allowed)
    {
        foreach (JsonProperty property in obj.EnumerateObject())
        {
            if (!allowed.Contains(property.Name))
                return true;
        }
        return false;
    }

    static bool TryRequiredLong(JsonElement root, string name, out long value)
    {
        value = 0;
        return root.TryGetProperty(name, out JsonElement el)
            && el.ValueKind == JsonValueKind.Number
            && el.TryGetInt64(out value);
    }
}

public enum CommandRejectReason
{
    None,
    MalformedJson,
    UnsupportedProtocol,
    UnsupportedOp,
    MissingRequestId,
    InvalidDirection,
    InvalidTimeout,
    InvalidDelay,
    InvalidTile,
    InvalidTickCount,
    InvalidRadius,
    DuplicateRequestId,
    CommandBusy,
    CommandSessionCapacityReached,
    InboxDisabled,
    SessionMismatch,
    PlayerMismatch,
    LocationMismatch,
    ScreenMismatch,
    Expired,
    StopLatched,
    LeaseExpired,
    StaleControlSeq,
    OwnerMismatch,
    RuntimeMismatch,
    GenerationMismatch,
    ResultTooLarge,
    ArmRejected,
    BindingMismatch,
    ControlRecordFault,

    JournalDuplicateSettled,
    JournalUncertain,
    JournalSettledCompacted,
    JournalIdentityMismatch,
    JournalSequenceConflict,
    JournalPersistenceFault,
    JournalCapacityReached,
}

public readonly record struct MenuStepCommandRequest(
    string RequestId,
    string SessionId,
    long ExpectedPlayerId,
    string? ExpectedPlayerName,
    string Direction,
    int TimeoutTicks,
    int DelayTicks,
    long DeadlineUnixMs,
    string Op = MenuStepCommandInbox.OpMenuStep,
    int TileX = 0,
    int TileY = 0,
    int Ticks = 0,
    string ExpectedLocationId = "",
    int ExpectedScreenId = 0,
    int CenterX = 0,
    int CenterY = 0,
    int Radius = 0,
    string Family = "",
    string Primitive = "",
    string RuntimeInstanceId = "",
    string OwnerId = "",
    long Generation = 0,
    long ControlSeq = 0,
    long ContextGeneration = 0,
    long RequestSeq = 0,
    UseToolInputTarget? ToolTarget = null,
    SelectItemTarget? SelectionTarget = null,
    WorldInteractionTarget? InteractionTarget = null,
    ContainerTransferTarget? TransferTarget = null,
    ContainerExitTarget? ExitTarget = null,
    UiKeyTarget? UiKeyTarget = null,
    MoveThroughTarget? ThroughTarget = null,
    PadButtonTarget? PadButton = null,
    UiNavTarget? UiNav = null,
    UiPressTarget? UiPress = null,
    ChatSendTarget? ChatSend = null,
    FishReflexArgs? ReflexFish = null,
    CombatReflexArgs? ReflexCombat = null,
    ChargeReflexArgs? ReflexCharge = null,
    string? EventToken = null,
    long? ExecutionDeadlineUnixMs = null, string? UiDecisionReceiver = null, int UiDecisionTimeoutMs = 0,
    JsonElement? VerbEnvelope = null, JsonElement? NativeIntent = null,
    MoveSegmentStop? SegmentStop = null,
    MovePathStops? PathNext = null)
{
    public JsonElement? Intent => NativeIntent ?? VerbEnvelope;
}

public readonly record struct CommandInboxIdentity(
    string SessionId,
    long PlayerId,
    string PlayerName,
    string LocationId = "",
    int ScreenId = 0,
    bool IsMainPlayer = false,
    bool IsSplitScreen = false,
    string RuntimeInstanceId = "",
    long ContextGeneration = 0,
    string SaveId = "");

public readonly record struct CommandAdmission(
    bool Accepted,
    CommandRejectReason Reason,
    MenuStepCommandRequest Request)
{
    public static CommandAdmission Accept(MenuStepCommandRequest request) =>
        new(true, CommandRejectReason.None, request);

    public static CommandAdmission Reject(
        CommandRejectReason reason,
        MenuStepCommandRequest request) => new(false, reason, request);
}

public sealed record MenuStepCommandResult
{
    [JsonPropertyName("release_receipt"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public JsonElement? ReleaseReceipt { get; init; }
    [JsonPropertyName("closure"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public JsonElement? Closure { get; init; }
    [JsonPropertyName("input_settled")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? InputSettled { get; init; }
    [JsonPropertyName("receiver_transition_status")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ReceiverTransitionStatus { get; init; }
    [JsonPropertyName("result_receiver_token")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ResultReceiverToken { get; init; }
    [JsonPropertyName("result_readback_token")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ResultReadbackToken { get; init; }
    [JsonPropertyName("result_context_generation")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? ResultContextGeneration { get; init; }
    [JsonPropertyName("ui_key_effect")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? UiKeyEffect { get; init; }
    [JsonPropertyName("result_menu_type")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ResultMenuType { get; init; }
    [JsonPropertyName("profession_id")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? ProfessionId { get; init; }
    [JsonPropertyName("actor_contents_before")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ActorContentsBefore { get; init; }
    [JsonPropertyName("actor_contents_after")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ActorContentsAfter { get; init; }
    // Primitives v2 WS-G1 additive fields (ui_nav / ui_press): the mirrored calls in order and the snap after.
    [JsonPropertyName("ui_pad_calls")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string[]? UiPadCalls { get; init; }
    [JsonPropertyName("ui_pad_snapped_after")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? UiPadSnappedAfter { get; init; }
    [JsonPropertyName("ui_pad_reason")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? UiPadReason { get; init; }
    // Primitives v2 WS-G3 additive field: the readback of a G3 step (entered name, number, answer, ready count, item).
    [JsonPropertyName("ui_pad_readback")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? UiPadReadback { get; init; }
    // Primitives v2 WS-G2: the economy ledger of a shop / crafting ui_press (before / after money, item, stock).
    [JsonPropertyName("ui_pad_economy")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public Dictionary<string, object>? UiPadEconomy { get; init; }
    // WS3a additive fields (world_interact kinds, move_through).
    [JsonPropertyName("kind")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Kind { get; init; }
    [JsonPropertyName("result_location_id")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ResultLocationId { get; init; }
    [JsonPropertyName("effect_detail")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? EffectDetail { get; init; }
    // Primitives v2 WS-G4 additive fields (reflex_fish).
    [JsonPropertyName("reflex_verdict")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ReflexVerdict { get; init; }
    [JsonPropertyName("reflex")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public Dictionary<string, object>? Reflex { get; init; }
    public const string StatusCompleted = "completed";
    public const string StatusRejected = "rejected";
    public const string StatusInterrupted = "interrupted";
    public const string StatusUncertain = "uncertain";

    [JsonPropertyName("protocol")]
    public int Protocol { get; init; } = MenuStepCommandInbox.ProtocolVersion;

    [JsonPropertyName("ev")]
    public string Event { get; init; } = "command_result";

    [JsonPropertyName("op")]
    public string Op { get; init; } = MenuStepCommandInbox.OpMenuStep;

    [JsonPropertyName("request_id")]
    public string? RequestId { get; init; }

    [JsonPropertyName("session_id")]
    public string SessionId { get; init; } = "";

    [JsonPropertyName("accepted")]
    public bool Accepted { get; init; }

    [JsonPropertyName("status")]
    public string Status { get; init; } = StatusRejected;

    [JsonPropertyName("reject_reason")]
    public string? RejectReason { get; init; }

    [JsonPropertyName("executed"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? Executed => this.Status == StatusRejected ? false : null;

    private AdmissionRefusal? typedRefusal;
    [JsonPropertyName("not_executed"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public AdmissionRefusal? NotExecuted { get => this.Status == StatusRejected
        ? this.RefusalEnvelope is { } envelope
            ? JsonSerializer.Deserialize<AdmissionRefusal>(envelope.GetProperty("not_executed").GetRawText())
            : this.typedRefusal ?? AdmissionRefusals.Classify(this.RejectReason ?? "", this.ArmStatus) : null;
        init => this.typedRefusal = value; }

    // Persist with the settled legacy row, then project only this envelope to result.json.
    [JsonPropertyName("refusal_envelope"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public JsonElement? RefusalEnvelope { get; init; }

    [JsonPropertyName("arm_status")]
    public string? ArmStatus { get; init; }

    [JsonPropertyName("run_id")]
    public int? RunId { get; init; }

    [JsonPropertyName("outcome")]
    public string? Outcome { get; init; }

    [JsonPropertyName("success")]
    public bool Success { get; init; }

    [JsonPropertyName("moved")]
    public bool? Moved { get; init; }

    [JsonPropertyName("neighbor_matched")]
    public bool? NeighborMatched { get; init; }

    [JsonPropertyName("change_seen")]
    public bool? ChangeSeen { get; init; }

    [JsonPropertyName("timeout_reached")]
    public bool? TimeoutReached { get; init; }

    [JsonPropertyName("start_snap_id")]
    public int? StartSnapId { get; init; }

    [JsonPropertyName("end_snap_id")]
    public int? EndSnapId { get; init; }

    [JsonPropertyName("expected_neighbor_id")]
    public int? ExpectedNeighborId { get; init; }

    [JsonPropertyName("first_changed_snap_id")]
    public int? FirstChangedSnapId { get; init; }

    [JsonPropertyName("press_count")]
    public int? PressCount { get; init; }

    /// <summary>fix92A / ADR 162: a guarded path's segments, stops reached, turns and end reason (diagnostics only).</summary>
    [JsonPropertyName("path")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public MovePathReport? Path { get; init; }

    [JsonPropertyName("primitive")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Primitive { get; init; }

    [JsonPropertyName("input_args")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public Dictionary<string, object>? InputArgs { get; init; }

    [JsonPropertyName("cleanup_confirmed")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? CleanupConfirmed { get; init; }

    [JsonPropertyName("world_effect_status")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? WorldEffectStatus { get; init; }

    [JsonPropertyName("direction")]
    public string? Direction { get; init; }

    [JsonPropertyName("completed_unix_ms")]
    public long CompletedUnixMs { get; init; }

    [JsonPropertyName("player_id")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? PlayerId { get; init; }

    [JsonPropertyName("location_id")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? LocationId { get; init; }

    [JsonPropertyName("captured_ticks")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? CapturedTicks { get; init; }

    [JsonPropertyName("center_x")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? CenterX { get; init; }

    [JsonPropertyName("center_y")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? CenterY { get; init; }

    [JsonPropertyName("radius")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? Radius { get; init; }

    [JsonPropertyName("screen_id")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? ScreenId { get; init; }

    [JsonPropertyName("runtime_instance_id")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? RuntimeInstanceId { get; init; }

    [JsonPropertyName("context_generation")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? ContextGeneration { get; init; }

    [JsonPropertyName("ui_revision")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? UiRevision { get; init; }

    [JsonPropertyName("viewport_transform_revision")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? ViewportTransformRevision { get; init; }

    [JsonPropertyName("context")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public Dictionary<string, object>? Context { get; init; }

    [JsonPropertyName("facts")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public Dictionary<string, object>? Facts { get; init; }

    [JsonPropertyName("request_seq")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? RequestSeq { get; init; }

    [JsonPropertyName("journal_recovered")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingDefault)]
    public bool JournalRecovered { get; init; }

    [JsonPropertyName("journal_state")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? JournalState { get; init; }
}

/// <summary>Cursor tile in session.json (value equality keeps the per-tick publish cheap).</summary>
public readonly record struct CursorTileView(
    [property: JsonPropertyName("x")] int X,
    [property: JsonPropertyName("y")] int Y,
    [property: JsonPropertyName("visible")] bool Visible);

public sealed record CommandInboxSession
{
    [JsonPropertyName("mod_tick")] public ulong ModTick { get; init; }
    [JsonPropertyName("native_ms")] public long NativeMs { get; init; }
    [JsonPropertyName("acquired_ms")] public long? AcquiredMs { get; init; }
    [JsonPropertyName("input_idle")] public object? InputIdle { get; init; }
    [JsonPropertyName("halt")] public System.Text.Json.Nodes.JsonObject? Halt { get; init; }
    [JsonPropertyName("rearmed")] public System.Text.Json.Nodes.JsonObject? Rearmed { get; init; }
    [JsonPropertyName("halt_notes")] public string[] HaltNotes { get; init; } = Array.Empty<string>();
    [JsonPropertyName("fenced_generation")] public long FencedGeneration { get; init; }
    [JsonPropertyName("status_answers")] public JsonElement[] StatusAnswers { get; init; } = Array.Empty<JsonElement>();
    [JsonPropertyName("claim_answers")] public JsonElement[] ClaimAnswers { get; init; } = Array.Empty<JsonElement>();
    [JsonPropertyName("cancel_answer")] public JsonElement? CancelAnswer { get; init; }
    [JsonPropertyName("protocol")]
    public int Protocol { get; init; } = MenuStepCommandInbox.ProtocolVersion;

    [JsonPropertyName("ev")]
    public string Event { get; init; } = "command_session";

    [JsonPropertyName("session_id")]
    public string SessionId { get; init; } = "";

    [JsonPropertyName("player_id")]
    public long PlayerId { get; init; }

    [JsonPropertyName("player_name")]
    public string PlayerName { get; init; } = "";

    [JsonPropertyName("location_id")]
    public string LocationId { get; init; } = "";

    [JsonPropertyName("screen_id")]
    public int ScreenId { get; init; }

    [JsonPropertyName("is_main_player")]
    public bool IsMainPlayer { get; init; }

    [JsonPropertyName("is_split_screen")]
    public bool IsSplitScreen { get; init; }

    [JsonPropertyName("inbox_enabled")]
    public bool InboxEnabled { get; init; }

    [JsonPropertyName("menu_step_enabled")]
    public bool MenuStepEnabled { get; init; }

    [JsonPropertyName("water_once_enabled")]
    public bool WaterOnceEnabled { get; init; }

    [JsonPropertyName("world_press_enabled")]
    public bool WorldPressEnabled { get; init; }

    [JsonPropertyName("use_tool_enabled")]
    public bool UseToolEnabled { get; init; }

    [JsonPropertyName("select_item_enabled")]
    public bool SelectItemEnabled { get; init; }

    [JsonPropertyName("world_interaction_enabled")]
    public bool WorldInteractionEnabled { get; init; }

    [JsonPropertyName("container_transfer_enabled")]
    public bool ContainerTransferEnabled { get; init; }

    [JsonPropertyName("container_exit_enabled")]
    public bool ContainerExitEnabled { get; init; }

    /// <summary>WS2 ui_key admission term (UiKeyReady). Default-OFF mod config.</summary>
    [JsonPropertyName("ui_key_enabled")]
    public bool UiKeyEnabled { get; init; }

    [JsonPropertyName("world_ready")]
    public bool WorldReady { get; init; }

    [JsonPropertyName("inflight_request_id")]
    public string? InflightRequestId { get; init; }

    [JsonPropertyName("input_busy")]
    public bool InputBusy { get; init; }

    /// <summary>
    /// Operator diagnostic (acceptance 2, R6ACC4a/4c): the current CursorInert evaluation, refreshed with
    /// every session publish (same cadence as input_busy). Read-only, no behaviour change. cursor_inert /
    /// cursor_inert_reason use the strict TOOL rule (use_tool); the _action pair the ACTION rule
    /// (world_interact; a rug under the cursor is inert there). Reason null = inert; unreadable facts
    /// publish false + CURSOR_NOT_INERT:UNREADABLE and cursor_tile null.
    /// </summary>
    [JsonPropertyName("cursor_inert")]
    public bool CursorInert { get; init; }

    [JsonPropertyName("cursor_inert_reason")]
    public string? CursorInertReason { get; init; }

    [JsonPropertyName("cursor_inert_action")]
    public bool CursorInertAction { get; init; }

    [JsonPropertyName("cursor_inert_action_reason")]
    public string? CursorInertActionReason { get; init; }

    [JsonPropertyName("cursor_tile")]
    public CursorTileView? CursorTile { get; init; }

    [JsonPropertyName("runtime_instance_id")]
    public string RuntimeInstanceId { get; init; } = "";

    [JsonPropertyName("owner_id")]
    public string? OwnerId { get; init; }

    [JsonPropertyName("generation")]
    public long? Generation { get; init; }

    [JsonPropertyName("control_seq")]
    public long? ControlSeq { get; init; }

    [JsonPropertyName("lease_until_unix_ms")]
    public long? LeaseUntilUnixMs { get; init; }

    [JsonPropertyName("stop_latched")]
    public bool StopLatched { get; init; }

    [JsonPropertyName("stop_generation")]
    public long? StopGeneration { get; init; }

    [JsonPropertyName("record_faulted")]
    public bool RecordFaulted { get; init; }

    [JsonPropertyName("release_requested")]
    public bool ReleaseRequested { get; init; }

    [JsonPropertyName("release_acked")]
    public bool ReleaseAcked { get; init; } = true;

    /// <summary>
    /// Explicit capability gate for #184. Old binaries do not emit this flag,
    /// so Python keeps legacy arbitrary-UUID semantics against them.
    /// </summary>
    [JsonPropertyName("journal_supported")]
    public bool JournalSupported { get; init; } = true;

    [JsonPropertyName("journal_schema")]
    public int JournalSchema { get; init; } = CommandSettledJournal.SchemaVersion;

    [JsonPropertyName("journal_unresolved_count")]
    public int JournalUnresolvedCount { get; init; }

    [JsonPropertyName("journal_unresolved_limit")]
    public int JournalUnresolvedLimit { get; init; } = CommandSettledJournal.DefaultMaxUnresolvedRows;

    [JsonPropertyName("journal_blocked_reason")]
    public string? JournalBlockedReason { get; init; }

    // Primitives v2 WS-G0: pad-path facts, present only with ModConfig.GamepadInputEnabled. Null =
    // omitted, so session.json is byte-identical to v1 with the flag off. Read-only diagnostics plus
    // the runtime's pad switch (pad_mode_owned / cursor_hidden replace the cursor_inert park check).
    [JsonPropertyName("gamepad_input_enabled"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? GamepadInputEnabled { get; init; }

    [JsonPropertyName("pad_mode_owned"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? PadModeOwned { get; init; }

    [JsonPropertyName("pad_mode_restore_pending"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? PadModeRestorePending { get; init; }

    /// <summary>Empty when resolved; latched fault refuses new pad-mode takes until teardown/reload.</summary>
    [JsonPropertyName("pad_mode_cleanup_fault"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? PadModeCleanupFault { get; init; }

    /// <summary>The Game1._oldGamepadConnectedState pin is active (PLAN 1.5 a pinned restore).</summary>
    [JsonPropertyName("pad_connected_pin"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? PadConnectedPin { get; init; }

    /// <summary>ARTI controller overrides SMAPI may still apply (PadOverrideLedger count; G7 PAD_KEYS "overrides").</summary>
    [JsonPropertyName("pad_override_count"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? PadOverrideCount { get; init; }

    /// <summary>
    /// Held ARTI pad Presses only (PadOverrideLedger.Outstanding). pad_override_count also counts the
    /// Suppress-only entries, which persist for the whole inactive session (review major 4, G7).
    /// </summary>
    [JsonPropertyName("pad_outstanding_count"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? PadOutstandingCount { get; init; }

    /// <summary>Game1.options.gamepadMode as a string (Auto / ForceOn / ForceOff; G7 PAD_KEYS "gamepad_mode").</summary>
    [JsonPropertyName("gamepad_mode"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? GamepadMode { get; init; }

    [JsonPropertyName("cursor_hidden"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? CursorHidden { get; init; }

    /// <summary>CURSOR_NOT_HIDDEN:&lt;why&gt; when not hidden; omitted when hidden or the flag is off.</summary>
    [JsonPropertyName("cursor_hidden_reason"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? CursorHiddenReason { get; init; }

    [JsonPropertyName("os_warp_calls"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? OsWarpCalls { get; init; }

    [JsonPropertyName("os_warp_blocked"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? OsWarpBlocked { get; init; }

    /// <summary>Must stay 0: an inactive SetMousePosition that reached the OS (global stop).</summary>
    [JsonPropertyName("os_warp_unblocked_inactive"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? OsWarpUnblockedInactive { get; init; }

    /// <summary>ADR 244: snap warps blocked while her window was the active one (part of os_warp_blocked).</summary>
    [JsonPropertyName("os_warp_blocked_active"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? OsWarpBlockedActive { get; init; }

    /// <summary>
    /// ADR 244: her hands do not depend on window focus (the verified path serves both states and pad mode
    /// stays owned in front). Published with the pad flag only; monitors use it instead of the old rule
    /// "pad mode owned while focused is a fault".
    /// </summary>
    [JsonPropertyName("focus_independent"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? FocusIndependent { get; init; }

    /// <summary>
    /// ADR 250: each hand path on its own (<c>world</c>: the verified pad path; <c>menu</c>: the UI motor's frame takes
    /// the game's inactive menu branch in both window states). <c>focus_independent</c> above is the weaker value.
    /// </summary>
    [JsonPropertyName("focus_independent_paths"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public FocusPaths? FocusIndependentPaths { get; init; }

    /// <summary>pad_button admission term (WS-G0 slice 2; PadButtonInputEnabled, default OFF).</summary>
    [JsonPropertyName("pad_button_enabled"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? PadButtonEnabled { get; init; }

    /// <summary>ui_nav / ui_press admission term (WS-G1; UiPadMenusEnabled, default OFF; omitted with the flag off).</summary>
    [JsonPropertyName("ui_pad_enabled"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? UiPadEnabled { get; init; }
    // Primitives v2 WS-G2: the economy rows + container_transfer modes are admissible now (omitted with the flag off).
    [JsonPropertyName("economy_enabled"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? EconomyEnabled { get; init; }

    /// <summary>WS-G3 G3 contexts on (TextEventMenusEnabled + UiPadMenusEnabled; omitted with the flag off).</summary>
    [JsonPropertyName("text_event_menus_enabled"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? TextEventMenusEnabled { get; init; }

    /// <summary>chat_send admission term (WS-G3, owner D5: ChatSendEnabled, default OFF; omitted with the flag off).</summary>
    [JsonPropertyName("chat_send_enabled"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? ChatSendEnabled { get; init; }

    /// <summary>reflex_fish admission term (WS-G4; ReflexFishingEnabled + the pad path, default OFF).</summary>
    [JsonPropertyName("reflex_fish_enabled"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? ReflexFishEnabled { get; init; }

    /// <summary>WS-G6 world extras admission term (WorldExtrasEnabled, default OFF; omitted with the flag off).</summary>
    [JsonPropertyName("world_extras_enabled"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? WorldExtrasEnabled { get; init; }

    /// <summary>reflex_combat admission term (WS-G5; ReflexCombatEnabled + the pad path, default OFF).</summary>
    [JsonPropertyName("reflex_combat_enabled"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? ReflexCombatEnabled { get; init; }

    /// <summary>reflex_charge admission term (WS-G5; ReflexChargeEnabled + the pad path, default OFF).</summary>
    [JsonPropertyName("reflex_charge_enabled"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? ReflexChargeEnabled { get; init; }

    /// <summary>Options.autoRun: the farmhand always runs with the stick while true (Options.cs:679).</summary>
    [JsonPropertyName("auto_run"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? AutoRun { get; init; }
}
