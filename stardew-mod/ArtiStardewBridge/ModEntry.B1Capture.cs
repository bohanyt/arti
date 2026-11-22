using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Locations;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    InteractKindCapture CaptureTileDoor(Farmer player, GameLocation location, Vector2 tile, Rectangle rect)
    {
        string locationRaw = location.NameOrUniqueName;
        string? action = location.doesTileHaveProperty((int)tile.X, (int)tile.Y, "Action", "Buildings");
        if (action is null)
            return InteractKindCapture.Chest; // nothing here: the chest rule reports the existing miss
        var door = NativeB1Offers.ResolveDoor(location, player, new Point((int)tile.X, (int)tile.Y));
        if (door is null)
            return InteractKindCapture.Missed("door_not_eligible");
        if (InteractInvariantMiss(player, tile) is string invariant)
            return InteractKindCapture.Missed(invariant);
        // checkAction talks to an NPC standing below a tile action before performing it.
        if (this.InteractOccupantMiss(player, location, rect, tile, includeBelow: true) is string below)
            return InteractKindCapture.Missed(below);
        {
            string targetName = door.Destination;
            string expected = SceneIdentity.Token(targetName);
            string source = SceneIdentity.DoorFactId(locationRaw, (int)tile.X, (int)tile.Y, targetName);
            string instance = this.worldTargetInstanceTokenTracker.Observe(location)!
                + ":" + UiKeyTargetToken.Hash(door.Action + "|" + door.X + "|" + door.Y);
            string token = this.InteractToken(WorldInteractionKinds.Door, instance, source, player, expected);
            var target = new WorldInteractionTarget(token, source, (int)tile.X, (int)tile.Y, (int)player.Tile.X, (int)player.Tile.Y,
                player.FacingDirection, WorldInteractionKinds.Door, expected);
            if (!target.IsValid)
                return InteractKindCapture.Missed("target_invalid");
            GameLocation pinnedLocation = location;
            Vector2 pinnedTile = tile;
            string pinnedAction = action;
            var identity = this.CurrentCommandIdentity();
            Vector2 playerTile = player.Tile;
            int facing = player.FacingDirection;
            Rectangle pinnedRect = rect;
            bool StillPresent() => Game1.player is Farmer p && ReferenceEquals(p.currentLocation, pinnedLocation)
                && p.GetGrabTile() == pinnedTile
                && ReferenceEquals(p, player) && this.CurrentCommandIdentity() == identity
                && p.Tile == playerTile && p.FacingDirection == facing
                && NativeB1Offers.ResolveDoor(pinnedLocation, p, new Point((int)pinnedTile.X, (int)pinnedTile.Y)) == door
                && string.Equals(pinnedLocation.doesTileHaveProperty((int)pinnedTile.X, (int)pinnedTile.Y, "Action", "Buildings"),
                    pinnedAction, StringComparison.Ordinal)
                && this.InteractOccupantMiss(p, pinnedLocation, pinnedRect, pinnedTile, includeBelow: true) is null;
            return new InteractKindCapture(target, new BgActionTarget(BgActionKind.Door, null, expected, "", StillPresent,
                () => SampleInteractEffect(StillPresent, "")), null, false);
        }
    }

    // Diagnostic only: a logical counter stand can still grab the actor's own tile.
    // Do not offer the fallback tile or change the native A resolution.
    // W1 gap7t (live gap7a S26 L296-L305): ARTI stood on Marnie's stand 12,16 facing up at Position.Y 1057.8 then 1041.9;
    // GetGrabTile up = (Y - 5) / 64 = 16 = its own tile, so CaptureExtrasA read the floor and returned null: no target
    // and NO refusal (extras_refusal null in the ledger). The same positional diagnostic now names Marnie's counter.
    InteractKindCapture? CaptureBlacksmithOwnTileMiss(Farmer player, GameLocation location, Vector2 grab, ExtrasGuardFacts facts)
    {
        Vector2 faced = new(player.Tile.X, player.Tile.Y - 1f);
        if (grab != player.Tile || player.FacingDirection != 0)
            return null;
        string? action = location.doesTileHaveProperty((int)faced.X, (int)faced.Y, "Action", "Buildings");
        string verb = action is null ? "" : ArgUtility.SplitBySpace(action) is { Length: > 0 } parts ? parts[0] : "";
        return verb switch
        {
            "Blacksmith" => this.CaptureBlacksmith(player, location, faced, facts with { GrabIsOwnTile = true }),
            "AnimalShop" => this.CaptureAnimalShop(player, location, faced, facts with { GrabIsOwnTile = true }),
            _ => null,
        };
    }

    InteractKindCapture? CaptureBlacksmith(Farmer player, GameLocation location, Vector2 tile, ExtrasGuardFacts facts)
    {
        var point = new Point((int)tile.X, (int)tile.Y);
        var identity = this.CurrentCommandIdentity();
        return this.ExtrasCapture(WorldExtras.Blacksmith, player, location, tile,
            facts with { FacilityUnavailable = !NativeB1Offers.BlacksmithReady(location, player, point),
                OccupantAtTile = this.InteractOccupantMiss(player, location, TileRect(tile), tile, true) is not null },
            (object?)location.getCharacterFromName("Clint") ?? location, "Blacksmith", null, () => new BgExtraFacts(WorldExtras.Blacksmith, false, 0),
            menu => ReferenceEquals(Game1.player, player) && ReferenceEquals(player.currentLocation, location)
                && this.CurrentCommandIdentity() with { ContextGeneration = identity.ContextGeneration } == identity && IsQuestionMenu(menu, "Blacksmith"));
    }

    InteractKindCapture? CaptureCommunityNote(Farmer player, GameLocation location, Vector2 tile, ExtrasGuardFacts facts)
    {
        int? area = NativeB1Offers.ResolveNote(location, player, new Point((int)tile.X, (int)tile.Y), player.Tile);
        if (area is not int a || location is not CommunityCenter cc) return null;
        var identity = this.CurrentCommandIdentity();
        return this.ExtrasCapture(WorldExtras.CommunityNote, player, location, tile,
            facts with { OccupantAtTile = this.InteractOccupantMiss(player, location, TileRect(tile), tile, true) is not null },
            location, a.ToString(), null, () => new BgExtraFacts(WorldExtras.CommunityNote, false, a),
            menu => ReferenceEquals(Game1.player, player) && ReferenceEquals(player.currentLocation, location)
                && this.CurrentCommandIdentity() with { ContextGeneration = identity.ContextGeneration } == identity && NativeB1Offers.NoteReceiver(menu, cc, a));
    }
}
