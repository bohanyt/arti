namespace ArtiCore.Pure;

// ADR-16 interim: legacy fixtures stay verbatim; native v3 facts are supplied only by the Observer.
public sealed record FrameFacts(string ReceiverToken, string MenuType, string PageType,
    string Context, int SnappedId, int Components, int PageIndex)
{
    public ReceiverSurface? Receiver { get; init; }
}
