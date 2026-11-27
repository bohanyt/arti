namespace ArtiStardewBridge.Protocol;

/// <summary>Publish only a current, visible ACTION tile. Source identity stays in the native adapter.</summary>
public static class CodeActionCells
{
    public const int ScanRadius = 12; // The ordinary local interaction scan.

    public static bool Allowed(bool farmhand, bool eventOrFestival) => farmhand && !eventOrFestival;

    public static bool Visible(int x, int y, int originX, int originY, int left, int top, int right, int bottom) =>
        x >= left && x <= right && y >= top && y <= bottom
        && Math.Max(Math.Abs(x - originX), Math.Abs(y - originY)) <= ScanRadius;

    static string Pin(string location, int x, int y) => UiKeyTargetToken.Hash(location + ":" + x + "," + y)[..20];

    public static string Ref(string location, int x, int y) => "map-code:" + Pin(location, x, y);

    // One group per code-backed cell: no inferred family shared with adjacent cells.
    public static object[] Row(string location, int x, int y) =>
        new object[] { Ref(location, x, y), x, y, new[] { "press" }, Pin(location, x, y) };
}
