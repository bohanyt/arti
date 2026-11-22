namespace ArtiStardewBridge.Protocol;

/// <summary>
/// fix54G (runbooks/briefs/WITNESS_MUSEUM_GEODE_PLAN.md G1): MuseumMenu places its held item on a left click at a free
/// museum floor tile (MuseumMenu.receiveLeftClick, decomp 1.6.15: fadeTimer 0, y above the inventory bound, then
/// isTileSuitableForMuseumPiece + isItemSuitableForDonation). Floor tiles are no ClickableComponent, so the generic
/// any-menu graph (UiPadMenusEnabled) published no control the native placement branch accepts; the WS-G6
/// MuseumPlace step lives only on the non-generic ctx path. This is the pure half of the one published accepts-item
/// spot: the free tile the native predicates admit, its UI point and its row. The mod supplies the native predicates;
/// the press is the ordinary generic left click. No item choice, no scripted donation, no save coordinates.
/// </summary>
public static class MuseumSpot
{
    public const string FieldPath = "menu.museumSpot", Name = "museumSpot";
    public const int Tile = 64;

    public readonly record struct Spot(int UiX, int UiY, int TileX, int TileY);

    /// <summary>
    /// The visible free tile whose UI point round-trips to that same tile and is not blocked (inventory, OK button,
    /// y bound). Row-major as before; a tile at least <paramref name="edge"/> screen px inside the viewport wins over an
    /// edge tile, because MuseumMenu.update pans the view while the mouse rests within 64 px of an edge.
    /// </summary>
    public static Spot? Pick(int viewX, int viewY, int viewW, int viewH, Func<int, int, bool> suitable,
        Func<float, float> toUi, Func<float, float> fromUi, Func<int, int, bool> blocked, int edge = Tile)
    {
        Spot? fallback = null;
        for (int ty = viewY / Tile; ty <= (viewY + viewH) / Tile; ty++)
            for (int tx = viewX / Tile; tx <= (viewX + viewW) / Tile; tx++)
            {
                if (!suitable(tx, ty))
                    continue;
                int sx = tx * Tile + Tile / 2 - viewX, sy = ty * Tile + Tile / 2 - viewY;
                int x = (int)toUi(sx), y = (int)toUi(sy);
                // The menu converts back with ModifyCoordinateFromUIScale + viewport (MuseumMenu.receiveLeftClick).
                if ((int)(fromUi(x) + viewX) / Tile != tx || (int)(fromUi(y) + viewY) / Tile != ty || blocked(x, y))
                    continue;
                var spot = new Spot(x, y, tx, ty);
                if (sx >= edge && sx < viewW - edge && sy >= edge && sy < viewH - edge)
                    return spot;
                fallback ??= spot;
            }
        return fallback;
    }

    /// <summary>A tile-sized box whose integer Center is exactly the spot's UI point (the generic press clicks Center).</summary>
    public static (int X, int Y, int W, int H) Bounds(Spot spot, int tileUi)
    {
        int size = Math.Max(1, tileUi);
        return (spot.UiX - size / 2, spot.UiY - size / 2, size, size);
    }

    public static string Label(Spot spot, string? heldName) =>
        "museum donation spot (free tile " + spot.TileX + "," + spot.TileY + "): places the held "
        + (string.IsNullOrWhiteSpace(heldName) ? "item" : heldName) + " here";
}
