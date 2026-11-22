using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Menus;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    // Building.doAction dispatches cabin data actions before map tile actions.
    // Share recognition/ownership between the remote catalog and faced pre-A capture.
    static bool MorningMailboxAt(Farmer player, GameLocation location, Vector2 tile, out bool unavailable)
    {
        int x = (int)tile.X, y = (int)tile.Y;
        var building = location.getBuildingAt(tile);
        bool cabinMailbox = building is { isCabin: true } && building.getMailboxPosition() == new Point(x, y)
            && building.daysOfConstructionLeft.Value <= 0 && building.occupiesTile(tile, applyTilePropertyRadius: true)
            && !building.isTilePassable(tile)
            && building.GetData()?.GetActionAtTile(x - building.tileX.Value, y - building.tileY.Value) == "Mailbox";
        unavailable = Game1.mailbox.Count == 0 || location is Farm
            && (player.getMailboxPosition() != new Point(x, y)
                || building?.GetIndoors() is FarmHouse { IsOwnedByCurrentPlayer: false });
        return cabinMailbox || location.doesTileHaveProperty(x, y, "Action", "Buildings") == "Mailbox";
    }

    InteractKindCapture? CaptureMorningMailbox(Farmer player, GameLocation location, Vector2 grab, ExtrasGuardFacts facts)
    {
        if (!MorningMailboxAt(player, location, grab, out bool unavailable))
            return null;
        return this.ExtrasCapture(WorldExtras.Mailbox, player, location, grab,
            facts with { FacilityUnavailable = unavailable,
                OccupantAtTile = ExtrasOccupant(player, location, TileRect(grab), null) }, location, null, null,
            () => new BgExtraFacts(WorldExtras.Mailbox, false, 0), menu => menu is LetterViewerMenu);
    }

}
