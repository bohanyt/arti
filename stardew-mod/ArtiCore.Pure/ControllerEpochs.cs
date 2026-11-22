namespace ArtiCore.Pure;

public sealed record EpochAdoption(long From, long To, string Phase);
public sealed class ControllerEpochs
{
    readonly System.Text.Json.JsonElement[] phases;
    readonly object tool;
    long epoch;
    int adoptedPhase = -1;
    object? receiver;
    readonly List<EpochAdoption> adoptions = new();
    public IReadOnlyList<EpochAdoption> Adoptions => adoptions.AsReadOnly();
    public EpochAdoption? LastAdoption => adoptions.LastOrDefault();
    public ControllerEpochs(NativeVerbRules rules, string kind, object tool, long admissionEpoch)
    { phases = rules.ControllerPhases(kind).EnumerateArray().ToArray(); this.tool = tool; epoch = admissionEpoch; }
    public bool CanWrite(ReceiverSurface frame) => frame.Epoch == epoch && ReferenceEquals(frame.Top?.Instance, receiver);
    public bool TryAdopt(string currentPhase, string nextPhase, ReceiverSurface frame, object boundTool,
        bool ownContinuation, bool previousClosed)
    {
        int index = Array.FindIndex(phases, p => p.GetProperty("name").GetString() == nextPhase);
        int current = Array.FindIndex(phases, p => p.GetProperty("name").GetString() == currentPhase);
        if (index <= adoptedPhase || index < 0 || current < index - 1 || current > index || frame.Epoch != epoch + 1
            || !ReferenceEquals(tool, boundTool) || !phases[index].TryGetProperty("adopts", out var adopts)
            || adopts.GetProperty("receiver").GetString() != (frame.Top?.MenuType ?? "hud")) return false;
        if (frame.Top is null ? !previousClosed || receiver is null : !ownContinuation) return false;
        adoptions.Add(new(epoch, frame.Epoch, nextPhase)); epoch = frame.Epoch; adoptedPhase = index;
        receiver = frame.Top?.Instance; return true;
    }
}
