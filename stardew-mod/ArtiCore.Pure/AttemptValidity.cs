namespace ArtiCore.Pure;

public sealed record ValidityFacts(bool IdentityMatches, long Epoch, bool BindingMatches, bool TargetExists = true,
    bool InReach = true, bool PoseMatches = true, bool WithinBudget = true, bool NativeControllerPhase = false);

/// <summary>The admitted row never changes. Native runners supply phase facts; effect evolution after an edge is legal.</summary>
public sealed class AttemptValidity
{
    readonly string[] phases;
    readonly string verb;
    readonly long epoch;
    public string RowKey { get; }
    public string EndPhase => this.phases.Last();
    public long ReceiverEpoch => this.epoch;
    public AttemptValidity(NativeVerbRules rules, string verb, string rowKey, long epoch)
    { phases = rules.Phases(verb, rowKey); this.verb = verb; RowKey = rowKey; this.epoch = epoch; }
    public bool IsValid(string phase, ValidityFacts facts, ControllerEpochs? controller = null, ReceiverSurface? surface = null)
    {
        if (!phases.Contains(phase) || !facts.IdentityMatches || !facts.BindingMatches || !facts.WithinBudget) return false;
        if (facts.Epoch != epoch && !(verb == "run_controller" && controller is not null && surface is not null
            && surface.Epoch == facts.Epoch && controller.CanWrite(surface))) return false;
        return phase switch {
            "pre_edge" => facts.TargetExists && facts.InReach,
            "charging" => facts.PoseMatches && facts.TargetExists && facts.InReach,
            "post_edge" or "release" or "moving" or "walking" or "single_edge" or "read_only" => true,
            _ => verb == "run_controller" && facts.NativeControllerPhase };
    }
}
