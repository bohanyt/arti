namespace ArtiStardewBridge.Protocol;

/// <summary>Bounded observation of controls actually drawn for one receiver. No callbacks or input.</summary>
public sealed class GenericUiDrawVisibility
{
    object? receiver;
    long at;
    bool complete;
    readonly HashSet<object> drawn = new(ReferenceEqualityComparer.Instance);
    public void Begin(object? receiver, long now)
    { this.receiver = receiver; this.at = now; this.complete = false; this.drawn.Clear(); }
    public void Record(object component)
    { if (!this.complete && this.drawn.Count < 8192) this.drawn.Add(component); }
    public void End() => this.complete = true;
    public bool? Visible(object? receiver, object control, long now)
    {
        if (!this.complete || !ReferenceEquals(receiver, this.receiver) || now < this.at || now - this.at > 2000)
            return null;
        return this.drawn.Contains(control);
    }
}
