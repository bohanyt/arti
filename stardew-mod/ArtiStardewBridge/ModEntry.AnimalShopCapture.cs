using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    InteractKindCapture? CaptureAnimalShop(Farmer player, GameLocation location, Vector2 tile, ExtrasGuardFacts facts)
    {
        var marnie = location.getCharacterFromName("Marnie");
        bool present = AnimalCounterOwnerPresent(location, tile);
        var pin = this.CurrentCommandIdentity();
        return this.ExtrasCapture(WorldExtras.AnimalShop, player, location, tile,
            facts with { FacilityUnavailable = !present || location.NameOrUniqueName != "AnimalShop" },
            (object?)marnie ?? location, "AnimalShop", null, () => new BgExtraFacts(WorldExtras.AnimalShop, false, 0),
            menu => ReferenceEquals(Game1.player, player) && ReferenceEquals(player.currentLocation, location)
                && this.CurrentCommandIdentity() with { ContextGeneration = pin.ContextGeneration } == pin
                && IsQuestionMenu(menu, "Marnie"));
    }

    static bool AnimalCounterOwnerPresent(GameLocation location, Vector2 tile)
    {
        var marnie = location.getCharacterFromName("Marnie");
        return location.doesTileHaveProperty((int)tile.X, (int)tile.Y, "Action", "Buildings") == "AnimalShop"
            && marnie is not null && (marnie.Tile == new Vector2(tile.X, tile.Y - 1)
            || marnie.Tile == new Vector2(tile.X - 1, tile.Y - 1));
    }

}
