using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Cloud 51 — jendela inspeksi ubin lokal read-only.
///
/// Bukan snapshot OBS-1, bukan store kedua, bukan pathfinding.
/// Hanya: batas radius, urutan deterministik, dan merakit DTO
/// <see cref="SceneObjectFact"/> / <see cref="SceneTerrainFact"/> yang
/// sudah dihasilkan adapter kanonik. Capture runtime tetap di
/// <c>SceneSnapshotCapture.CaptureObject</c> /
/// <c>CaptureTerrain</c> (game thread).
/// </summary>
public static class InspectTilesQuery
{
    public const string Op = "inspect_tiles";
    public const string OutcomeInspected = "Inspected";

    /// <summary>
    /// Radius Chebyshev maksimum (inklusi). 4 => jendela 9×9 = 81 ubin.
    /// Dipilih sebagai hard cap struktural, bukan convention.
    /// </summary>
    public const int MaxRadius = 4;

    public const int MaxLocationIdLength = 128;

    /// <summary>
    /// Batas berkas hasil. SENGAJA terpisah dari
    /// <see cref="SceneObservationCaps.ReceiverMaxLineBytes"/> dan cap
    /// snapshot global (objects/terrain = 40). Melewati batas = gagal
    /// tertutup, bukan truncate yang mengaku lengkap.
    /// </summary>
    public const int MaxResultBytes = 128 * 1024;

    public static bool InWindow(int x, int y, int centerX, int centerY, int radius) =>
        Math.Max(Math.Abs(x - centerX), Math.Abs(y - centerY)) <= radius;

    public static int TileCount(int radius)
    {
        if (radius < 0 || radius > MaxRadius)
            return 0;
        int edge = (2 * radius) + 1;
        return edge * edge;
    }

    /// <summary>
    /// Paling banyak satu objek + satu terrain per ubin di jendela.
    /// Lebih dari itu = gagal tertutup, bukan cap snapshot 40.
    /// </summary>
    public static int MaxFactsForRadius(int radius) => TileCount(radius) * 2;

    public static IEnumerable<(int X, int Y)> EnumerateTiles(int centerX, int centerY, int radius)
    {
        if (radius < 0 || radius > MaxRadius)
            yield break;
        for (int y = centerY - radius; y <= centerY + radius; y++)
        {
            for (int x = centerX - radius; x <= centerX + radius; x++)
                yield return (x, y);
        }
    }

    public static CommandRejectReason? ValidateCapturedFacts(
        IReadOnlyList<ScenePreparedFact> objects,
        IReadOnlyList<ScenePreparedFact> terrain,
        int centerX,
        int centerY,
        int radius
    )
    {
        if (objects.Count + terrain.Count > MaxFactsForRadius(radius))
            return CommandRejectReason.ResultTooLarge;

        HashSet<string> seen = new(StringComparer.Ordinal);
        foreach (ScenePreparedFact fact in EnumerateCaptured(objects, terrain))
        {
            if (fact.Id.Length == 0 || !seen.Add(fact.Id))
                return CommandRejectReason.ResultTooLarge;
            if (fact.Dto is not SceneObjectFact and not SceneTerrainFact)
                return CommandRejectReason.ResultTooLarge;
            if (!InWindow(TileOf(fact.Dto).X, TileOf(fact.Dto).Y, centerX, centerY, radius))
                return CommandRejectReason.ResultTooLarge;
        }

        return null;
    }

    static IEnumerable<ScenePreparedFact> EnumerateCaptured(
        IReadOnlyList<ScenePreparedFact> objects,
        IReadOnlyList<ScenePreparedFact> terrain
    )
    {
        for (int i = 0; i < objects.Count; i++)
            yield return objects[i];
        for (int i = 0; i < terrain.Count; i++)
            yield return terrain[i];
    }

    public static Dictionary<string, object> AssembleFacts(
        IReadOnlyList<ScenePreparedFact> objects,
        IReadOnlyList<ScenePreparedFact> terrain
    )
    {
        List<ScenePreparedFact> ordered = new(objects.Count + terrain.Count);
        ordered.AddRange(objects);
        ordered.AddRange(terrain);
        ordered.Sort(CompareFact);

        Dictionary<string, object> facts = new(ordered.Count, StringComparer.Ordinal);
        foreach (ScenePreparedFact fact in ordered)
        {
            if (fact.Id.Length == 0 || facts.ContainsKey(fact.Id))
                continue;
            facts[fact.Id] = fact.Dto;
        }

        return facts;
    }

    static int CompareFact(ScenePreparedFact a, ScenePreparedFact b)
    {
        int kind = KindRank(a.Dto).CompareTo(KindRank(b.Dto));
        if (kind != 0)
            return kind;

        TileXY ta = TileOf(a.Dto);
        TileXY tb = TileOf(b.Dto);
        int byY = ta.Y.CompareTo(tb.Y);
        if (byY != 0)
            return byY;
        int byX = ta.X.CompareTo(tb.X);
        if (byX != 0)
            return byX;
        return string.CompareOrdinal(a.Id, b.Id);
    }

    static int KindRank(object dto) => dto switch
    {
        SceneObjectFact => 0,
        SceneTerrainFact => 1,
        _ => 2,
    };

    static TileXY TileOf(object dto) => dto switch
    {
        SceneObjectFact obj => obj.Tile,
        SceneTerrainFact terrain => terrain.Tile,
        _ => new TileXY(),
    };
}
