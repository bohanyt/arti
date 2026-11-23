namespace ArtiHostHelper;

internal readonly record struct BedSite(object Farmer, object House, object Bed, int Day, (int X, int Y) Destination)
{
    internal bool Matches(BedSite other) => Day == other.Day && Destination == other.Destination
        && ReferenceEquals(Farmer, other.Farmer) && ReferenceEquals(House, other.House) && ReferenceEquals(Bed, other.Bed);
}

/// <summary>One clean walk and, at most, the exact prompt produced by its native bed touch.</summary>
internal sealed class BedApproach
{
    internal const int TimeoutMs = 20_000;
    const int ArrivalWindowMs = 500;
    readonly BedSite site;
    readonly object controller;
    readonly (int X, int Y) origin;
    readonly long started;
    long? arrived;
    object? prompt;
    bool invalidated;

    internal BedApproach(BedSite site, object controller, (int X, int Y) origin, long started)
    {
        this.site = site;
        this.controller = controller;
        this.origin = origin;
        this.started = started;
    }

    bool PathMatches(BedSite current, object? currentController, long now) =>
        !invalidated && now - started < TimeoutMs && site.Matches(current)
        && (ReferenceEquals(currentController, controller)
            || currentController is null && arrived is long at && (prompt is not null || now - at <= ArrivalWindowMs));

    internal void Arrive(BedSite current, object? currentController, long now)
    {
        if (ReferenceEquals(currentController, controller) && PathMatches(current, currentController, now))
            arrived ??= now;
    }

    internal bool CanTouch(BedSite current, object? currentController, object? menu, (int X, int Y) tile, long now) =>
        menu is null && prompt is null && tile != origin && PathMatches(current, currentController, now);

    internal bool Capture(BedSite current, object? currentController, object menu, long now)
    {
        if (prompt is not null || !PathMatches(current, currentController, now)) return false;
        prompt = menu;
        return true;
    }

    internal bool Observe(BedSite current, object? currentController, object? menu, long now)
    {
        invalidated |= !PathMatches(current, currentController, now) || !ReferenceEquals(menu, prompt);
        return !invalidated;
    }

    internal bool Owns(BedSite current, object? currentController, object menu, long now) =>
        Observe(current, currentController, menu, now) && prompt is not null;
}
