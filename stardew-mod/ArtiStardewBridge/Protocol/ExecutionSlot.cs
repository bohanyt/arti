using ArtiCore.Pure;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>Game-thread admission boundary. Journal-capable tickets follow the durable admission write;
/// runners arm afterwards. Result publication and evidence finality never occupy this slot.</summary>
public sealed partial class ExecutionSlot
{
    private readonly CommandSettledJournal journal;
    private NativeActionOwner? admittedOwner;
    private static readonly NativeVerbRules VerbRules = NativeVerbRules.Load();
    // Installed by the native dispatch adapter; absent facts never authorize a v2 mutation.
    public Func<MenuStepCommandRequest, VerbAdmissionFacts?>? ObserveVerb { get; set; }
    public Func<MenuStepCommandRequest, string, ControllerFrame?>? ObserveController { get; set; }
    public ControllerExecution BindController(MenuStepCommandRequest request, string kind, Func<string> phase,
        object? client = null, Action<string>? cancel = null, Action? step = null, Action? arm = null, Func<bool>? busy = null)
    {
        if (!this.Owns(request) || this.ActiveAttempt!.Controller is not null)
            throw new InvalidOperationException("controller_unadmitted_or_already_bound");
        var controller = this.ActiveAttempt!.Controller = new ControllerExecution(this, this.ActiveAttempt, kind, phase);
        if (client is not null) this.AttachClient(request, client, cancel!, step, arm, busy);
        return controller;
    }
    public void CheckController() => this.ActiveAttempt?.Controller?.Check();
    public string? RefusalCode { get; private set; }
    public MenuStepCommandResult? RefusalResult { get; internal set; }
    public Func<(long Epoch, long Tick)>? ObserveRefusal { get; set; }
    public Func<object[]>? ObserveDebts { get; set; }
    public string? RefusalDetail { get; private set; }
    public ActiveAttempt? ActiveAttempt { get; private set; }
    // Native cleanup evidence can outlive the legacy result. Never infer idle from result publication.
    public Func<bool>? CleanupPending { get; set; }
    public bool InputBusy => this.ActiveAttempt is not null || this.CleanupPending?.Invoke() == true;
    public bool ReleaseReady => this.CleanupPending?.Invoke() != true
        && (this.ActiveAttempt is null || (this.ActiveAttempt.CancelReason is not null || this.ActiveAttempt.Receipt is not null)
            && this.ActiveAttempt.Clients.Values.All(c => c.Busy?.Invoke() != true));

    public bool Blocks(MenuStepCommandRequest request) => this.CleanupPending?.Invoke() == true
        || !this.Owns(request);

    public bool Owns(MenuStepCommandRequest request) => this.ActiveAttempt is { } active
        && active.Request.RequestId == request.RequestId && active.Request.RuntimeInstanceId == request.RuntimeInstanceId
        && active.Request.OwnerId == request.OwnerId && active.Request.Generation == request.Generation
        && active.Request.SessionId == request.SessionId && active.Request.ExpectedPlayerId == request.ExpectedPlayerId
        && active.Request.ExpectedScreenId == request.ExpectedScreenId;

    public bool CanContinue(ActiveAttempt? attempt, string phase, ValidityFacts facts) => attempt is not null
        && ReferenceEquals(this.ActiveAttempt, attempt) && attempt.CancelReason is null
        && (attempt.Controller is { } controller ? facts.IdentityMatches && facts.BindingMatches && facts.WithinBudget && controller.Check()
            : attempt.Validity?.IsValid(phase, facts)
            ?? (facts.IdentityMatches && facts.BindingMatches && facts.WithinBudget));

    // Cancellation withdraws input authority, not the obligation to release on the same binding.
    // This permits neutral cleanup only; callers must never use it to authorize a held frame.
    internal bool CanNeutralize(ActiveAttempt? attempt, string phase, ValidityFacts facts) => attempt is not null
        && ReferenceEquals(this.ActiveAttempt, attempt) && attempt.CancelReason is not null
        && phase is "release" or "walking" or "post_edge"
        && (attempt.Controller is { } controller ? facts.IdentityMatches && facts.BindingMatches && controller.CanNeutralize()
            : attempt.Validity?.IsValid(phase, facts)
            ?? (facts.IdentityMatches && facts.BindingMatches && facts.WithinBudget));

    // Clients register their own cleanup at arm time; no mod-wide cancellation sweep.
    public bool AttachClient(MenuStepCommandRequest request, object client, Action<string> cancel,
        Action? step = null, Action? arm = null, Func<bool>? busy = null)
    {
        if (!this.Owns(request) || this.ActiveAttempt!.CancelReason is not null) return false;
        this.ActiveAttempt.Clients[client] = new(cancel, step, arm, busy);
        return true;
    }

    public void Cancel(string reason)
    {
        if (this.ActiveAttempt is not { CancelReason: null } attempt) return;
        attempt.CancelReason = reason;
        if (attempt.ReleaseVia == "release") attempt.ReleaseVia = "neutralize";
        foreach (var client in attempt.Clients.Values.ToArray()) client.Cancel(reason);
    }

    public bool Cancel(string attemptId, string requestId, long generation, string reason)
    {
        if (this.ActiveAttempt is not { } active || active.AttemptId != attemptId
            || active.Request.RequestId != requestId || active.Request.Generation != generation) return false;
        this.Cancel(reason);
        return true;
    }

    public string Claim((ReceiverObserver Observer, ReceiverSurface Surface) observed, long epoch)
    {
        if (observed.Surface.Epoch != epoch || observed.Surface.Top is null) return "claim_stale_epoch";
        if (this.ActiveAttempt is { Client: "progression" } active && active.Validity?.ReceiverEpoch == epoch)
            return "authority_busy";
        return observed.Observer.Claim(epoch, observed.Surface.Frame) ? "accepted" : "claim_stale_epoch";
    }

    public JsonElement AttemptStatus(JsonElement query, long nowMs) => this.journal.AttemptStatus(query, nowMs);

    // The slot brackets subordinate consumption. ModEntry has no controller tick-order graph.
    public void Tick(Action consume)
    {
        this.Bracket(() => {
            var attempt = this.ActiveAttempt;
            var clients = attempt?.Clients.Values.ToArray() ?? Array.Empty<ExecutionClient>();
            foreach (var client in clients) client.Step?.Invoke();
            consume();
            if (!ReferenceEquals(attempt, this.ActiveAttempt) || attempt?.CancelReason is not null) return;
            foreach (var client in clients) client.Arm?.Invoke();
        });
    }

    public ExecutionSlot(CommandSettledJournal journal) => this.journal = journal;

    public bool TryAdmit(MenuStepCommandRequest request, CommandInboxIdentity identity,
        NativeActionOwner? owner, long nowMs)
    {
        this.RefusalResult = null;
        if (this.TryAdmitCore(request, identity, owner, nowMs, out var reason)) return true;
        var refusal = AdmissionRefusals.Classify(reason.ToString(), this.RefusalDetail,
            this.RefusalCode == "malformed_json" ? "adapter_malformed" : this.RefusalCode);
        this.RefusalCode = refusal.Reason;
        var stamp = this.ObserveRefusal?.Invoke() ?? (Epoch: 0L, Tick: 0L);
        this.RefusalResult = this.FinishResult(request, AdmissionRefusals.Attach(new MenuStepCommandResult {
            RequestId = request.RequestId, RejectReason = reason.ToString(), CompletedUnixMs = nowMs
        }, request, refusal, stamp.Epoch, stamp.Tick, debts: this.ObserveDebts?.Invoke()));
        return false;
    }

    private bool TryAdmitCore(MenuStepCommandRequest request, CommandInboxIdentity identity,
        NativeActionOwner? owner, long nowMs, out CommandRejectReason reason)
    {
        reason = CommandRejectReason.MalformedJson;
        this.RefusalCode = null;
        this.RefusalDetail = null;
        if (identity.SessionId.Length == 0 || request.SessionId != identity.SessionId) { reason = CommandRejectReason.SessionMismatch; return false; }
        if (identity.IsMainPlayer || identity.IsSplitScreen || request.ExpectedPlayerId != identity.PlayerId || request.ExpectedPlayerName is { Length: > 0 } name && name != identity.PlayerName)
        { reason = CommandRejectReason.PlayerMismatch; return false; }
        bool legacy = request.VerbEnvelope is null;
        bool readOnly = AdmissionRefusals.Verb(request) == "observe";
        if (legacy && readOnly) request = request with { RuntimeInstanceId = identity.RuntimeInstanceId,
            ExpectedScreenId = request.Op == "inspect_tiles" ? identity.ScreenId : request.ExpectedScreenId };
        bool inheritLease = legacy && !readOnly && string.IsNullOrEmpty(request.OwnerId);
        if (inheritLease) {
            if (owner is null) {
                reason = CommandRejectReason.LeaseExpired; return false;
            }
            request = request with { OwnerId = owner.OwnerId, Generation = owner.Generation,
                ControlSeq = owner.ControlSeq, RuntimeInstanceId = identity.RuntimeInstanceId,
                ExpectedLocationId = request.ExpectedLocationId.Length == 0 ? identity.LocationId : request.ExpectedLocationId, ExpectedScreenId = identity.ScreenId,
                ContextGeneration = identity.ContextGeneration };
        }
        if (request.ExpectedLocationId is { Length: > 0 } location && location != identity.LocationId)
        { reason = CommandRejectReason.LocationMismatch; return false; }
        if (legacy && !readOnly && request.ContextGeneration != identity.ContextGeneration)
        { this.RefusalDetail = "context_generation != native"; reason = CommandRejectReason.BindingMismatch; return false; }
        if (request.ExpectedScreenId != identity.ScreenId) { reason = CommandRejectReason.ScreenMismatch; return false; }
        if (request.RuntimeInstanceId != identity.RuntimeInstanceId) { reason = CommandRejectReason.RuntimeMismatch; return false; }
        if (!readOnly && owner is null) { reason = CommandRejectReason.CommandBusy; return false; }
        if (!readOnly && !owner!.CanAdmitLeasedMutation(identity, nowMs, request.RuntimeInstanceId, request.OwnerId,
            request.Generation, request.ControlSeq, out reason)) {
            if (inheritLease && !owner.IsArmed && !owner.StopLatched && !owner.RecordFaulted) reason = CommandRejectReason.LeaseExpired;
            return false;
        }
        if (request.DeadlineUnixMs <= 0 || nowMs > request.DeadlineUnixMs
            || !legacy && !readOnly && (owner!.AcquiredUnixMs > long.MaxValue - AdmissionWindowMs
                || request.DeadlineUnixMs != owner.AcquiredUnixMs + AdmissionWindowMs))
        { reason = CommandRejectReason.Expired; return false; }
        if (this.ObserveVerb?.Invoke(request) is not { } facts)
        { this.RefusalCode = "adapter_unsupported_capability"; reason = CommandRejectReason.BindingMismatch; return false; }
        var prepared = request.VerbEnvelope ?? LegacyFormMap.Intent(request, identity, facts);
        if (prepared is null) { this.RefusalCode = LegacyFormMap.Resolve(request).Refusal ?? "adapter_unmapped_form"; return false; }
        request = (facts.Dispatch ?? request) with { VerbEnvelope = request.VerbEnvelope, NativeIntent = prepared };
        var envelope = prepared.Value;
        var pins = envelope.GetProperty("pins");
        if (pins.GetProperty("session_id").GetString() != identity.SessionId
            || pins.GetProperty("player_id").GetInt64() != identity.PlayerId
            || pins.GetProperty("location").GetString() != identity.LocationId) {
            this.RefusalCode = "precondition_stale"; this.RefusalDetail = "pins.identity != native"; return false;
        }
        string? rowError = VerbRules.CheckRow(envelope.GetProperty("verb").GetString()!,
            envelope.GetProperty("args"), envelope.GetProperty("pins"), facts.Discriminators);
        if (rowError == "precondition_stale") this.RefusalDetail = "pins.row_key: "
            + envelope.GetProperty("pins").GetProperty("row_key").GetString() + " != "
            + VerbRules.Resolve(envelope.GetProperty("verb").GetString()!, envelope.GetProperty("args"), facts.Discriminators);
        if (rowError is null && (!envelope.GetProperty("pins").TryGetProperty("receiver_epoch", out var epoch)
            || epoch.ValueKind != JsonValueKind.Number || !epoch.TryGetInt64(out long expectedEpoch)
            || expectedEpoch != facts.ReceiverEpoch))
        { rowError = "precondition_stale"; this.RefusalDetail = "pins.receiver_epoch != " + facts.ReceiverEpoch; }
        if (rowError is null && facts.Pins is { } nativePins) {
            foreach (var required in VerbRules.Row(envelope.GetProperty("verb").GetString()!,
                envelope.GetProperty("pins").GetProperty("row_key").GetString()!).GetProperty("required_pins").EnumerateArray()) {
                string pin = required.GetString()!;
                if (!nativePins.TryGetProperty(pin, out var native) || !ArtiCore.Pure.JsonEquality.Equal(
                    native, envelope.GetProperty("pins").GetProperty(pin))) {
                    rowError = "precondition_stale"; this.RefusalDetail = "pins." + pin + " != native"; break;
                }
            }
        }
        if (!facts.TickReasserted && facts.BarrierOpening) {
            this.RefusalCode = "adapter_unsupported_capability"; this.RefusalDetail = "q14_unproven_pad_forceon";
            reason = CommandRejectReason.BindingMismatch; return false;
        }
        if (rowError is not null)
        // # SPEC-Q ADR-17: frozen legacy native_map has no v2 enum entries. A7 consumes the exact code.
        { this.RefusalCode = rowError; reason = rowError == "malformed_json" ? CommandRejectReason.MalformedJson : CommandRejectReason.BindingMismatch; return false; }
        var validity = new AttemptValidity(VerbRules, envelope.GetProperty("verb").GetString()!,
            envelope.GetProperty("pins").GetProperty("row_key").GetString()!, facts.ReceiverEpoch);
        if (!this.TryAdmitValidated(request, CommandSettledJournal.IsJournalRequest(request), out reason, validity)) return false;
        this.admittedOwner = owner;
        if (envelope.GetProperty("verb").GetString() == "ui" && this.ObserveController?.Invoke(request, "single_edge") is { } frame)
            this.ActiveAttempt!.AnswerWindow = new ReceiverAnswerWindow(VerbRules, this.ActiveAttempt.AttemptId, frame.Surface,
                facts.AnswerRole ?? facts.Discriminators.ControlRole ?? "other", envelope.GetProperty("args").GetProperty("gesture").GetString()!);
        return true;
    }

    public const int AdmissionWindowMs = 1000;

    private bool TryAdmitValidated(MenuStepCommandRequest request, bool journalMode, out CommandRejectReason reason, AttemptValidity? validity = null, string client = "inbox", bool tickReasserted = false)
    {
        reason = CommandRejectReason.BindingMismatch;
        if (client == "progression" && !tickReasserted) return false; // Q14 applies to native progression too
        reason = CommandRejectReason.CommandBusy;
        if (this.ActiveAttempt is not null || request.Intent?.GetProperty("verb").GetString() != "observe"
            && this.CleanupPending?.Invoke() == true) return false;
        if (journalMode && !this.journal.TryBegin(request, out reason)) return false;
        this.ActiveAttempt = new ActiveAttempt(request) { Validity = validity, Client = client, AdmitTick = this.ModTick,
            AdmittedInTicked = this.tickedHandlerOpen };
        reason = CommandRejectReason.None;
        return true;
    }

    /// <summary>Called with native release facts, independently of result production. A6 supplies
    /// the per-client wire receipt; the old ticket cannot release a successor.</summary>
    public bool TryRelease(ActiveAttempt attempt, AttemptTerminal terminal, NativeActionOwner? owner,
        out CommandRejectReason reason)
    {
        reason = CommandRejectReason.CommandBusy;
        if (!ReferenceEquals(this.ActiveAttempt, attempt) || terminal.RequestId != attempt.Request.RequestId
            || terminal.AttemptId != attempt.AttemptId || !terminal.InputIdle || !ReferenceEquals(owner, this.admittedOwner)
            || attempt.UsesLease && (owner is null || owner.IsArmed || !owner.ReleaseAcked)) return false;
        terminal = terminal with { Client = attempt.Client, Via = attempt.ReleaseVia, Lapse = attempt.ReleaseLapse, Fault = attempt.ProgressionFault };
        if (attempt.Receipt is { } receipt) terminal = terminal with { Tick = receipt.GetProperty("tick").GetUInt64(),
            Frame = receipt.GetProperty("frame").GetInt64(), Ms = receipt.GetProperty("ms").GetInt64() };
        this.RecordReceipt(attempt, terminal);
        terminal = terminal with { Closure = this.RefreshClosure(attempt) };
        if (attempt.ProgressionHistory is { } history) {
            if (!attempt.ProgressionSettled) {
                history.Progression(attempt, true, terminal.Frame, terminal.Tick, terminal.Ms);
                attempt.ProgressionSettled = true;
            }
            if (!history.Flush()) return false;
        }
        if (CommandSettledJournal.IsJournalRequest(attempt.Request)
            && !this.journal.TryRecordTerminal(attempt.Request, terminal, out reason)) return false;
        this.ActiveAttempt = null;
        this.admittedOwner = null;
        return true;
    }

    /// <summary>ADR 179: the mod's own body guard holds the slot for one disengage leg. Same shape as native progression:
    /// its own owner id, no journal row, input released preserving the core lease. Only when nothing else is admitted,
    /// no cleanup is pending and the core lease is armed with no STOP or recorded fault.</summary>
    public const string BodyGuardClient = "body_guard";

    public bool TryAdmitBodyGuard(MenuStepCommandRequest request, NativeActionOwner owner)
    {
        // ADR 181: the guard acts during model waits and pages too (no runtime lease armed then), like native
        // progression; a runtime release in progress or STOP / a fault refuse it.
        if (owner.BoundRuntimeId.Length == 0 || owner.StopLatched || owner.RecordFaulted || owner.IsReleasing
            || owner.ReleaseRequested) return false;
        request = BodyGuardRequest(request, owner, this.ModTick);
        if (!this.TryAdmitValidated(request, false, out _, null, BodyGuardClient)) return false;
        this.admittedOwner = owner;
        return true;
    }

    /// <summary>Admit one native festival route, ice approach step or cast under the festival lease.
    /// The mod neutralizes each attempt as it ends, with a separate receipt and no inbox journal row.</summary>
    public const string FestivalClient = "festival_reflex";

    public bool TryAdmitFestival(MenuStepCommandRequest request, NativeActionOwner owner)
    {
        if (!owner.IsArmed || owner.StopLatched || owner.RecordFaulted || owner.IsReleasing || owner.ReleaseRequested
            || owner.BoundRuntimeId != request.RuntimeInstanceId || owner.OwnerId != request.OwnerId
            || owner.Generation != request.Generation) return false;
        if (!this.TryAdmitValidated(request, false, out _, null, FestivalClient)) return false;
        this.admittedOwner = owner;
        return true;
    }

    /// <summary>ADR 180 (a72 SMAPI 11229-11231 UNSUPPORTED_PRIMITIVE): the guard's request is the same input_once
    /// reflex_combat primitive the runtime-requested leg uses (BackgroundControlInputHooks.TryArmCore accepts a reflex step
    /// only for IsReflexCombat), under the core lease identity the press path checks (LeaseValidFor: owner id, generation,
    /// bound runtime). Its own request id; no journal row.</summary>
    public static MenuStepCommandRequest BodyGuardRequest(MenuStepCommandRequest request, NativeActionOwner owner, ulong tick) =>
        request with { Op = MenuStepCommandInbox.OpInputOnce, Primitive = ReflexCombatTarget.Primitive,
            OwnerId = owner.OwnerId, Generation = owner.Generation, ControlSeq = owner.ControlSeq, RequestSeq = 0,
            RequestId = BodyGuardClient + "-" + tick };

    internal bool TryAdmitProgression(MenuStepCommandRequest request, NativeActionOwner owner, ReceiverSurface surface, string? entry,
        OccurrenceRing history) {
        if (surface.Authority.Holder != "progression" || entry is not ("a" or "b" or "c" or "d" or "e")) return false;
        long epoch = surface.Epoch;
        request = request with { Op = "progression/" + entry, ContextGeneration = epoch,
            OwnerId = "progression/" + request.RuntimeInstanceId, Generation = 1,
            NativeIntent = JsonSerializer.SerializeToElement(new { verb = "ui",
            args = new { gesture = "advance" }, pins = new { row_key = "receiver.role:advance", receiver_epoch = epoch } }) };
        request = request with { RequestSeq = checked(this.journal.HighestRequestSeq(request) + 1) };
        request = request with { RequestId = CommandSettledJournal.ExpectedRequestId(request) };
        if (!owner.ProgressionAllowed || !this.TryAdmitValidated(request, true, out _,
            new AttemptValidity(VerbRules, "ui", "receiver.role:advance", epoch), "progression", true)) return false;
        this.admittedOwner = owner;
        var a = this.ActiveAttempt!; a.ProgressionEntry = entry; a.ProgressionHistory = history;
        history.Progression(a, false, surface.Frame, ModTick, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
        if (history.Flush()) return true;
        this.Neutralize(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()); return false;
    }
}

public sealed record ActiveAttempt(MenuStepCommandRequest Request)
{
    internal JsonElement? Receipt;
    internal string? ProgressionEntry;
    internal bool ProgressionSettled, ProgressionAccepted;
    internal OccurrenceRing? ProgressionHistory;
    internal string? ProgressionFault;
    internal string ReleaseVia = "release";
    internal JsonElement? ReleaseLapse;
    internal JsonElement? ProvenClosure, ClosureRecord;
    internal readonly HashSet<string> Markers = new();
    internal readonly HashSet<string> OpenedPresenceIds = new();
    internal NativeClosureFacts? BeforeConsumption;
    internal int BracketDepth;
    internal bool BracketClosed, BracketFailed, ClosureEmitted;
    internal long? SendFrame;
    internal string? Incoming;
    internal Dictionary<object, ExecutionClient> Clients { get; } = new(ReferenceEqualityComparer.Instance);
    internal bool ResultRecorded;
    internal MenuStepCommandResult? PendingResult;
    internal ulong? AdmitTick, FirstEdgeTick;
    // PM-1 fix14e: admitted inside UpdateTicked; ArmedMs = when that handler returned (lapse clock start).
    internal bool AdmittedInTicked;
    internal long? ArmedMs;
    internal bool Delivered;
    internal ReceiverAnswerWindow? AnswerWindow;
    internal NativeAnswerWitness? AnswerWitness;
    internal object? TradeWitness;
    public string? CancelReason { get; internal set; }
    public ControllerExecution? Controller { get; internal set; }
    public AttemptValidity? Validity { get; init; }
    public string Client { get; init; } = "inbox";
    internal bool ReadOnly => this.Request.Intent?.GetProperty("verb").GetString() == "observe";
    // Each festival attempt releases the slot while the timed contest keeps its lease.
    internal bool UsesLease => this.Client is not ("progression" or ExecutionSlot.BodyGuardClient or ExecutionSlot.FestivalClient)
        && !this.ReadOnly;
    public string AttemptId => this.Request.VerbEnvelope?.GetProperty("attempt_id").GetString() ?? this.Request.RequestId;
}

internal sealed record ExecutionClient(Action<string> Cancel, Action? Step, Action? Arm, Func<bool>? Busy);

public sealed record AttemptTerminal(string AttemptId, string RequestId, ulong Tick, long Frame, long Ms, bool InputIdle,
    string Client = "inbox", string Via = "release", JsonElement? Lapse = null, JsonElement? Closure = null, string? Fault = null)
{
    [System.Text.Json.Serialization.JsonIgnore]
    public JsonElement Receipt => JsonSerializer.SerializeToElement(new Dictionary<string, object?> {
        ["attempt_id"] = AttemptId, ["request_id"] = RequestId, ["client"] = Client, ["tick"] = Tick,
        ["frame"] = Frame, ["ms"] = Ms, ["idle"] = InputIdle, ["via"] = Via
    }.Concat(Lapse is { } l ? new[] { new KeyValuePair<string, object?>("lapse", l) } : Array.Empty<KeyValuePair<string, object?>>()).ToDictionary(x => x.Key, x => x.Value));
}
public sealed record VerbAdmissionFacts(VerbDiscriminators Discriminators, long ReceiverEpoch, bool TickReasserted = false,
    JsonElement? Pins = null, JsonElement? Args = null, MenuStepCommandRequest? Dispatch = null, bool BarrierOpening = false, string? AnswerRole = null);

/// <summary>First slot client. Uses the existing consume/rename transport; has no motor or queue.</summary>
public sealed class InboxClient
{
    private readonly ExecutionSlot slot;
    public InboxClient(ExecutionSlot slot) => this.slot = slot;

    public bool TryAdmit(MenuStepCommandRequest request, CommandInboxIdentity identity, NativeActionOwner? owner, long nowMs)
        => this.slot.TryAdmit(request, identity, owner, nowMs);
}
