using ArtiCore.Pure;

namespace ArtiStardewBridge.Protocol;

public sealed record ControllerFrame(ValidityFacts Facts, ReceiverSurface Surface, object? Tool,
    bool OwnContinuation = false, object? Pose = null, bool CanReceiveInput = true, int PageIndex = 0, string? QuestionKey = null, ProfessionState? Profession = null);

/// <summary>A subordinate client of one admitted ticket; phase algorithms remain in the runners.</summary>
public sealed class ControllerExecution
{
    readonly ExecutionSlot slot;
    readonly ActiveAttempt attempt;
    readonly string kind;
    readonly Func<string> phase;
    readonly object? tool, pose;
    readonly AttemptValidity validity;
    readonly ControllerEpochs epochs;
    ReceiverSurface previous;
    public string Phase { get; private set; }
    public string? EndedBy { get; private set; }
    public IReadOnlyList<EpochAdoption> Adoptions => epochs.Adoptions;
    public HashSet<string> AdoptedPresenceIds { get; } = new();
    public Action<EpochAdoption>? Adopted { get; set; }
    public object Receipt => new { phase_at_end = Phase, ended_by = EndedBy ?? (attempt.CancelReason is null ? "completed"
        : attempt.CancelReason is "stop" or "lease" or "focus" or "barrier" ? attempt.CancelReason : "cancel"), adopted = Adoptions };

    internal ControllerExecution(ExecutionSlot slot, ActiveAttempt attempt, string kind, Func<string> phase)
    {
        this.slot = slot; this.attempt = attempt; this.kind = kind; this.phase = phase; Phase = phase();
        var frame = slot.ObserveController?.Invoke(attempt.Request, Phase)
            ?? throw new InvalidOperationException("adapter_unsupported_capability");
        tool = frame.Tool; pose = frame.Pose; previous = frame.Surface;
        var rules = NativeVerbRules.Load();
        validity = attempt.Validity ?? new AttemptValidity(rules, "run_controller", "self.controller:" + kind, previous.Epoch);
        epochs = new ControllerEpochs(rules, kind, tool ?? throw new InvalidOperationException("tool_binding"), previous.Epoch);
    }

    // ADR 152: cancelled authority cannot issue another edge, but its exact
    // physical binding must still consume R/N. No epoch adoption during cleanup.
    internal bool CanNeutralize()
    {
        if (!ReferenceEquals(slot.ActiveAttempt, attempt)) return false;
        var frame = slot.ObserveController?.Invoke(attempt.Request, Phase);
        return frame is not null && frame.Facts.IdentityMatches && frame.Facts.BindingMatches
            && ReferenceEquals(tool, frame.Tool) && (kind != "charge" || Equals(pose, frame.Pose))
            && epochs.CanWrite(frame.Surface);
    }

    public bool Check()
    {
        if (EndedBy is not null || !ReferenceEquals(slot.ActiveAttempt, attempt) || attempt.CancelReason is not null) return false;
        Phase = phase();
        var frame = slot.ObserveController?.Invoke(attempt.Request, Phase);
        string? failure = frame is null ? "validity_violation" : null;
        if (frame is not null)
        {
            bool binding = frame.Facts.BindingMatches && ReferenceEquals(tool, frame.Tool)
                && (kind != "charge" || Equals(pose, frame.Pose));
            if (!frame.Facts.IdentityMatches || !binding || !frame.Facts.WithinBudget) failure = "validity_violation";
            else if (!epochs.CanWrite(frame.Surface))
            {
                bool closed = previous.Top is { } old && frame.Surface.Top is null
                    && !frame.Surface.Parked.Any(p => ReferenceEquals(p.Instance, old.Instance));
                if (kind == "fish" && epochs.TryAdopt(Phase, Phase, frame.Surface, frame.Tool!, frame.OwnContinuation, closed))
                { if (frame.Surface.Top is { } adopted) AdoptedPresenceIds.Add(adopted.PresenceId); Adopted?.Invoke(epochs.LastAdoption!); }
                else failure = "foreign_epoch";
            }
            if (failure is null && !validity.IsValid(Phase, frame.Facts with { BindingMatches = binding }, epochs, frame.Surface))
                failure = "validity_violation";
            previous = frame.Surface;
        }
        if (failure is null) return true;
        EndedBy = failure; slot.Cancel(failure); return false;
    }
}
