using System.Runtime.CompilerServices;
using System.Text.Json.Serialization;

namespace ArtiCore.Pure;

public sealed record ReceiverLayer(string ReceiverId, string PresenceId, string MenuType, int Depth, string? Holder,
    [property: JsonIgnore] object Instance);
public sealed record ReceiverAuthority(string Holder, long SinceFrame);
public sealed record ReceiverSurface(long Epoch, long Frame, ReceiverAuthority Authority,
    IReadOnlyList<ReceiverLayer> Stack, IReadOnlyList<ReceiverLayer> Parked)
{
    public ReceiverLayer? Top => Stack.LastOrDefault();
}

/// <summary>One instance per runtime/screen, sampled on the game thread. Object identity is never a page identity.</summary>
public sealed class ReceiverObserver
{
    readonly Func<object, object?> child;
    readonly Func<object, string> menuClass;
    readonly ConditionalWeakTable<object, Token> tokens = new();
    Dictionary<object, string> presences = new(ReferenceEqualityComparer.Instance);
    sealed record Token(string Id);
    long nextToken, nextPresence, epoch;
    object? top;
    bool initialized, claimed;
    ReceiverAuthority authority = new("none", 0);
    public ReceiverAuthority Authority => authority;
    public ReceiverObserver(Func<object, object?> child, Func<object, string> menuClass)
    { this.child = child; this.menuClass = menuClass; }

    public ReceiverSurface Capture(object? root, IEnumerable<(string Holder, object Instance)> holders, long frame, bool progression)
    {
        var retained = new Dictionary<object, string>(ReferenceEqualityComparer.Instance);
        ReceiverLayer Layer(object instance, int depth, string? holder)
        {
            string id = tokens.GetValue(instance, _ => new Token("receiver:" + ++nextToken)).Id;
            if (!presences.TryGetValue(instance, out string? presence)) presence = "presence:" + ++nextPresence;
            retained[instance] = presence;
            return new(id, presence, menuClass(instance), depth, holder, instance);
        }
        var stack = new List<ReceiverLayer>(); var parked = new List<ReceiverLayer>();
        for (object? current = root; current is not null && !retained.ContainsKey(current); current = child(current))
            stack.Add(Layer(current, stack.Count, null));
        foreach (var holder in holders)
            for (object? current = holder.Instance; current is not null && !retained.ContainsKey(current); current = child(current))
                parked.Add(Layer(current, 0, holder.Holder));
        object? deepest = stack.LastOrDefault()?.Instance;
        if (!initialized || !ReferenceEquals(top, deepest))
        { initialized = true; top = deepest; epoch++; claimed = false; authority = new("none", frame); }
        string owner = deepest is null ? "none" : progression && !claimed ? "progression" : "core";
        if (authority.Holder != owner) authority = new(owner, frame);
        presences = retained;
        return new(epoch, frame, authority, stack.AsReadOnly(), parked.AsReadOnly());
    }

    public bool Claim(long expectedEpoch, long frame)
    {
        if (!initialized || top is null || epoch != expectedEpoch) return false;
        claimed = true;
        if (authority.Holder != "core") authority = new("core", frame);
        return true;
    }
}
