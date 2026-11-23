namespace ArtiHostHelper;

/// <summary>
/// Whether this HOST can accept ARTI's farmhand right now. The helper publishes the facts and this rule as
/// <c>accepting</c> in barrier.json; a watchdog restart and its re-arm token wait for <c>ok</c>, and a missing or stale
/// fact holds (an older helper build fails closed). No game types here: the rule is tested without the game.
/// </summary>
public static class HostAccepting
{
    /// <summary>Her own slot: her cabin exists and her farmer is not online (a ghost of a crashed game still holds it).
    /// A watchdog restart rejoins her by name only, so an unclaimed cabin does not make room for her.</summary>
    public static bool SlotFree(bool ownCabin, bool ownOnline) => ownCabin && !ownOnline;

    /// <summary>A running server, no connection that is not yet a farmer (mid-join: the exit veto's test), her slot free.</summary>
    public static bool Ok(bool serverUp, int connections, int farmers, bool slotFree)
        => serverUp && connections == farmers && slotFree;
}
