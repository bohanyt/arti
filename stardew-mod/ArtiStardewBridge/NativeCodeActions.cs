using Microsoft.Xna.Framework;
using ArtiStardewBridge.Protocol;
using StardewValley;

namespace ArtiStardewBridge;

// Read-only sources for currently drawn tiles whose locations handle ACTION in code.
internal static class NativeCodeActions
{
    internal readonly record struct Cell(Point Tile, int Area);
    static readonly Action<GameLocation, Farmer, List<Cell>>[] Sources =
    {
        NativeB1Offers.CollectUnreadNoteCells,
    };

    internal static readonly Sample Current = new();

    // The scene and all faced/arm captures in a game tick share this game-thread sample.
    internal sealed class Sample
    {
        readonly List<Cell> cells = new();
        GameLocation? location;
        Farmer? actor;
        long tick = -1;
        bool faulted;

        internal void Reset()
        {
            this.location = null;
            this.actor = null;
            this.tick = -1;
            this.faulted = false;
            this.cells.Clear();
        }

        internal IReadOnlyList<Cell> Read(GameLocation location, Farmer actor, long tick, out Exception? fault)
        {
            fault = null;
            if (!NativeB1Offers.HasCodeCells(location)) return Array.Empty<Cell>();
            if (!ReferenceEquals(this.location, location))
            {
                this.Reset();
                this.location = location;
            }
            if (this.faulted || this.tick == tick && ReferenceEquals(this.actor, actor)) return this.cells;
            this.actor = actor;
            this.tick = tick;
            this.cells.Clear();
            try
            {
                foreach (var source in Sources) source(location, actor, this.cells);
            }
            catch (Exception ex)
            {
                this.Disable();
                fault = ex;
            }
            return this.cells;
        }

        internal void Disable()
        {
            this.faulted = true;
            this.cells.Clear();
        }
    }

    internal static bool Visible(Point tile, Point origin, xTile.Dimensions.Rectangle view, int tileSize) =>
        view.Width > 0 && view.Height > 0
        && CodeActionCells.Visible(tile.X, tile.Y, origin.X, origin.Y, view.X / tileSize, view.Y / tileSize,
            (view.X + view.Width - 1) / tileSize, (view.Y + view.Height - 1) / tileSize);

    internal static void AppendVisible(List<(int X, int Y, object[] Value)> rows, string location,
        IReadOnlyList<Cell> cells, Point origin, xTile.Dimensions.Rectangle view, int tileSize)
    {
        for (int i = 0; i < cells.Count; i++)
        {
            Point tile = cells[i].Tile;
            if (Visible(tile, origin, view, tileSize) && !rows.Exists(row => row.X == tile.X && row.Y == tile.Y))
                rows.Add((tile.X, tile.Y, CodeActionCells.Row(location, tile.X, tile.Y)));
        }
    }
}
