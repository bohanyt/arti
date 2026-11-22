namespace ArtiStardewBridge.Protocol;

/// <summary>Human home labels from native ownership, independent of map instance names.</summary>
public static class FactNames
{
    public static string Home(long owner, long actor, long host, bool actorIsHost) =>
        owner == 0 ? "rumah/kabin (pemilik belum diketahui)"
        : owner == actor ? actorIsHost ? "rumahmu" : "kabinmu"
        : owner == host ? "rumah utama HOST" : "rumah petani lain";

    // Reflected field paths and sound cue names are metadata, not shown menu text.
    public static string ScreenText(string row)
    {
        int split = row.IndexOf(": ", StringComparison.Ordinal);
        if (split < 0 || !(row.StartsWith("menu.") || row.StartsWith("page."))) return row;
        return row[..split].EndsWith("Sound", StringComparison.OrdinalIgnoreCase) ? "" : row[(split + 2)..];
    }
}
