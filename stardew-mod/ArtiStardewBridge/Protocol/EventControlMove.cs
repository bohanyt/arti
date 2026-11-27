namespace ArtiStardewBridge.Protocol;

/// <summary>FIXB53: reviewed, untimed event control; never ordinary-world authority.</summary>
public static class EventControlMove
{
    public const int MaxTicks = 4;
    public static bool TokenShape(string? token) => token is { Length: 68 } && token.StartsWith("ecm:", StringComparison.Ordinal)
        && token[4..].All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f');

    public static bool Reviewed(bool eventUp, bool sameEvent, bool festival, bool control, string? sequence,
        string? command, bool menu, bool minigame, bool farmEvent, bool subscriber) =>
        eventUp && sameEvent && !festival && control && sequence is "haleyBeach" or "haleyBeach2" && command == "playerControl"
        && !menu && !minigame && !farmEvent && !subscriber;

    public static bool FestivalReviewed(string? id, string? sequence, string? raw, bool main, int timer,
        int index = -1, int count = -1) =>
        !main && timer <= 0 && raw?.Split(' ')[0] == "playerControl" && (id, sequence) is
            ("festival_spring13", "eggFestival") or ("festival_spring24", "flowerFestival")
            or ("festival_summer11", "luau") or ("festival_summer28", "jellies")
            or ("festival_winter8", "iceFestival") or ("festival_winter25", "christmas")
        || main && timer > 0 && id == "festival_spring13" && sequence == "eggHunt"
            && raw == "playerControl eggHunt" && count > 0 && index == count;

    /// <summary>ADR 94/97: a45 D20:J1226 moved dx -1.25, dy 15.84 over four ticks past a tree. Keep the native corner slide,
    /// but require at least 1 px forward per tick so sideways-only motion cannot prove a step.</summary>
    public const int NativeSlidePxPerTick = 2;

    /// <param name="slidePxPerTick">Reviewed routes (<see cref="EventControlAction.RouteStep"/>) pass 0: their steps also prove
    /// corridor membership, so they stay exactly cardinal.</param>
    public static bool Displacement(string direction, int ticks, float dx, float dy, int maxTicks = MaxTicks,
        int slidePxPerTick = NativeSlidePxPerTick)
    {
        if (!float.IsFinite(dx) || !float.IsFinite(dy) || ticks < 1 || ticks > maxTicks
            || Math.Abs(dx) + Math.Abs(dy) <= 0 || Math.Abs(dx) + Math.Abs(dy) > ticks * 16)
            return false;
        (float along, float across) = direction switch
        {
            "up" => (-dy, dx), "down" => (dy, dx), "left" => (-dx, dy), "right" => (dx, dy), _ => (-1f, 0f),
        };
        return along >= ticks && Math.Abs(across) <= ticks * slidePxPerTick;
    }
}

/// <summary>fix63A (a45, Flower Dance): the leave question of a festival she walks freely in. That walled map has no reachable
/// edge or warp, so this asks through the game's own Event.TryStartEndFestivalDialogue; nothing else changes.</summary>
public static class FestivalLeavePolicy
{
    public const string Detail = PadContextTable.FestivalLeaveDetail;

    public const string Seated = "seated";

    /// <summary>Null outside a festival (the event row is unchanged); "" = she can be asked now; else the refusal.
    /// <see cref="Seated"/> = free roam, but she sits on a seat of this map; festival_stand comes first.</summary>
    public static string? State(bool festival, bool freeRoamToken, bool playerControl, bool mainEvent, bool seated = false) =>
        !festival ? null : mainEvent ? "main_event" : !playerControl ? "no_player_control"
        : !freeRoamToken ? seated ? Seated : "no_free_roam" : "";
}
