namespace ArtiStardewBridge.Protocol;

public sealed record HostGiftSafetyMessage(ulong SaveId, string Stamp);

/// <summary>ADR 207: absence of a fresh, released farmhand owner is not permission to interrupt.</summary>
public static class HostGiftSafety
{
    public const string BridgeModId = "bohanyt.ArtiStardewBridge";
    public const string HelperModId = "bohan.ArtiHostHelper";
    public const string MessageType = "host_gift_safety";
    public const int MaxAgeMs = 2000;
    public static string Format(bool busy, long now)
        => now.ToString(System.Globalization.CultureInfo.InvariantCulture) + (busy ? ":busy" : ":released");

    public static bool TryParse(string? value, out long observed, out bool released)
    {
        observed = 0; released = false;
        var parts = value?.Split(':');
        if (parts is not { Length: 2 } || parts[1] is not ("busy" or "released")
            || !long.TryParse(parts[0], System.Globalization.NumberStyles.None,
                System.Globalization.CultureInfo.InvariantCulture, out observed)) return false;
        released = parts[1] == "released";
        return true;
    }

    public static bool Released(string? value, long now)
    {
        return TryParse(value, out long observed, out bool released) && released
            && now >= observed && now-observed <= MaxAgeMs;
    }
    public static bool MayOffer(bool onFarm, bool menuOpen, bool usingTool, bool hidden, string? release, long now)
        => onFarm && !menuOpen && !usingTool && !hidden && Released(release, now);
}

/// <summary>One authenticated peer/save's transient message state; never persisted (ADR 25/207).</summary>
public sealed class HostGiftSafetyInbox
{
    string? stamp;
    long latest = -1;
    public void Clear() { this.stamp = null; this.latest = -1; }
    public void Observe(string? value)
    {
        if (!HostGiftSafety.TryParse(value, out long observed, out bool released))
        { this.stamp = null; return; }
        if (observed < this.latest || (observed == this.latest && !HostGiftSafety.Released(this.stamp, observed) && released)) return;
        this.latest = observed; this.stamp = value;
    }
    public bool MayOffer(bool onFarm, bool menuOpen, bool usingTool, bool hidden, long now)
        => HostGiftSafety.MayOffer(onFarm, menuOpen, usingTool, hidden, this.stamp, now);
}
