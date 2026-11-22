using System.Reflection;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Menus;

namespace ArtiStardewBridge;

// Read-only native admission shared by faced capture and the navigation catalog.
// Never invoke lockedDoorWarp, checkBundle or a menu constructor here.
internal static class NativeB1Offers
{
    internal sealed record Door(string Action, string Destination, int X, int Y);

    internal static Door? ResolveDoor(GameLocation location, Farmer actor, Point tile)
    {
        string? action = location.doesTileHaveProperty(tile.X, tile.Y, "Action", "Buildings");
        if (action is null) return null;
        string[] a = ArgUtility.SplitBySpace(action);
        if (a.Length == 0) return null;
        if (a[0] is not ("WarpCommunityCenter" or "Warp" or "LockedDoorWarp")) return null;
        if (a[0] == "WarpCommunityCenter" ? a.Length != 1 : a.Length < 4
            || !int.TryParse(a[1], out int x) || x < 0 || !int.TryParse(a[2], out int y) || y < 0
            || string.IsNullOrWhiteSpace(a[3]) || a[3] == "BoatTunnel") return null;
        if (a[0] == "Warp" && a.Length != 4) return null;
        if (a[0] == "LockedDoorWarp" && a.Length is < 6 or > 8) return null;
        if (Protocol.WorldGateRules.Door(a, Game1.timeOfDay,
            a[0]=="Warp" ? Protocol.WorldDoorState.Empty : DoorState(location, actor, a)) is not null) return null;
        Warp? warp = location.getWarpFromDoor(tile, actor);
        if (warp is null || string.IsNullOrWhiteSpace(warp.TargetName) || warp.TargetX < 0 || warp.TargetY < 0
            || (a[0] != "WarpCommunityCenter" && (warp.TargetName != a[3]
                || warp.TargetX != int.Parse(a[1]) || warp.TargetY != int.Parse(a[2])))) return null;
        return new(action, warp.TargetName, warp.TargetX, warp.TargetY);
    }

    internal static Protocol.WorldDoorState DoorState(GameLocation location, Farmer actor, string[] a)
    {
        if (a.Length>0 && a[0]=="WarpCommunityCenter")
            return Protocol.WorldDoorState.Empty with { CommunityCenterOpen=Game1.MasterPlayer.mailReceived.Contains("ccDoorUnlock")
                || Game1.MasterPlayer.mailReceived.Contains("JojaMember") };
        string to = a.Length >= 4 ? a[3] : "";
        int friendship = a.Length >= 7 && actor.friendshipData.TryGetValue(a[6], out Friendship? f) ? f.Points : 0;
        return new(location.InValleyContext(), GameLocation.AreStoresClosedForFestival(), actor.HasTownKey,
            location is BeachNightMarket, Game1.shortDayNameFromDayOfSeason(Game1.dayOfMonth) == "Wed",
            to=="SeedShop" && Utility.HasAnyPlayerSeenEvent("191393"), to=="FishShop" && actor.mailReceived.Contains("willyHours"), location.IsWinterHere(),
            location.IsGreenRainingHere() && Game1.year == 1 && location is not Beach && location is not Forest && to != "AdventureGuild",
            friendship, false);
    }

    /// <summary>
    /// The current location's native door facts resolved NOW (ResolveDoor at the current Game1.timeOfDay). The full
    /// snapshot (SceneSnapshotCapture.CaptureDoors) and the clock refresh (FINAL Part C3, J L958-L973: the SeedShop doors
    /// were still enterable:false at 09:10) share this builder, so both send the identical door wire shape.
    /// </summary>
    internal static List<Protocol.ScenePreparedFact> DoorFacts(GameLocation location, Farmer actor, string locationRaw)
    {
        var facts = new List<Protocol.ScenePreparedFact>();
        if (location.doors is null) return facts;
        foreach (KeyValuePair<Point, string> pair in location.doors.Pairs)
        {
            Point tile = pair.Key;
            Door? offer = ResolveDoor(location, actor, tile);
            facts.Add(Protocol.SceneDoorRefresh.Fact(locationRaw, tile.X, tile.Y, offer?.Destination ?? pair.Value ?? "",
                DoorActionToken(location, tile), offer is not null, offer?.X, offer?.Y));
        }
        return facts;
    }

    /// <summary>WS3a: first token of the Buildings-layer Action at a door tile (bounded), null when absent.</summary>
    internal static string? DoorActionToken(GameLocation location, Point tile)
    {
        try
        {
            string? action = location.doesTileHaveProperty(tile.X, tile.Y, "Action", "Buildings");
            if (string.IsNullOrWhiteSpace(action))
                return null;
            string first = ArgUtility.SplitBySpace(action).FirstOrDefault() ?? "";
            return first.Length == 0 || first == "Warp" ? null
                : Protocol.SceneWire.Bound(first, Protocol.SceneWireLimits.DoorActionMax);
        }
        catch
        {
            return null;
        }
    }

    internal static bool BlacksmithReady(GameLocation location, Farmer actor, Point tile) =>
        location.doesTileHaveProperty(tile.X, tile.Y, "Action", "Buildings") == "Blacksmith"
        && actor.Tile.Y > tile.Y && location.NameOrUniqueName == "Blacksmith"
        && location.getCharacterFromName("Clint") is not null && actor.toolBeingUpgraded.Value is null;

    // Native area bounds live in CommunityCenter. Bind the exact read-only method, never copy its coordinates.
    static readonly MethodInfo? NoteArea = typeof(CommunityCenter).GetMethod("getAreaNumberFromLocation",
        BindingFlags.Instance | BindingFlags.NonPublic, null, new[] { typeof(Vector2) }, null);

    internal static int? ResolveNote(GameLocation location, Farmer actor, Point tile, Vector2 stand)
    {
        if (location is not CommunityCenter cc || NoteArea is null
            || !actor.hasOrWillReceiveMail("canReadJunimoText")) return null;
        int index = cc.getTileIndexAt(tile.X, tile.Y, "Buildings", "indoors");
        int area = index == 1799 && cc.numberOfCompleteBundles() > 2 ? 5
            : index is >= 1824 and <= 1833 ? (int)(NoteArea.Invoke(cc, new object[] { stand }) ?? -1) : -1;
        if (area < 0 || area >= cc.bundleMutexes.Count || !cc.isJunimoNoteAtArea(area)
            || cc.areasComplete[area] || cc.bundleMutexes[area].IsLocked()) return null;
        return area;
    }

    // S27 (live B1c 2026-10-19: CC entry tile, Crafts Room note outside the 12-tile scan, no note offered): the native
    // note tile of each area, read-only via the exact private method; the game's own empty sentinel means no note.
    static readonly MethodInfo? NotePosition = typeof(CommunityCenter).GetMethod("getNotePosition",
        BindingFlags.Instance | BindingFlags.NonPublic, null, new[] { typeof(int) }, null);

    /// <summary>S27: one entry per area whose native note tile resolves (ResolveNote) from a cardinal stand inside that
    /// same area. Bounded by the native area count; never a scan radius, never a copied coordinate.</summary>
    internal static List<(int Area, Point Tile)> NoteTiles(CommunityCenter cc, Farmer actor)
    {
        var notes = new List<(int Area, Point Tile)>();
        if (NotePosition is null) return notes;
        for (int area = 0; area < cc.bundleMutexes.Count; area++)
        {
            if (NotePosition.Invoke(cc, new object[] { area }) is not Point tile || tile.Equals(default(Point))) continue;
            foreach (var (sx, sy) in Protocol.WorldExtras.StandCandidates(tile.X, tile.Y))
                if (ResolveNote(cc, actor, tile, new Vector2(sx, sy)) == area)
                {
                    notes.Add((area, tile));
                    break;
                }
        }
        return notes;
    }

    internal static bool NoteReceiver(object? menu, CommunityCenter cc, int area) =>
        menu is JunimoNoteMenu note && note.whichArea == area && JunimoNoteMenu.canClick && !note.scrambledText
        && !note.fromGameMenu && note.bundles.Count > 0 && note.bundles.All(b => b.depositsAllowed)
        && cc.bundleMutexes[area].IsLockHeld();
}
