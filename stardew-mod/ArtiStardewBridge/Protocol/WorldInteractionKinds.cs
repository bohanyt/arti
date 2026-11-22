using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

// WS3a (Primitives v1, 2026-10-07): generalized background world_interact (door, forage)
// and the walk-onto warp variant of the background move run (move_through). Pure,
// SMAPI-free decision logic only; the game-coupled sampling lives in ModEntry /
// BackgroundInteractionRuntime.cs. Evidence ceiling of this layer: UNIT_TESTED.
//
// Every action-button press stays the existing verified retained-press hold
// (BgHoldKind.Action). This file only names the per-kind expectation and judges the
// observed effect; it never presses, warps or writes game state.
//
// WS3b extension points (not implemented here): kind "npc" (DialogueOpened, synchronous
// receiver), move_through kind "bed" (TOUCH_QUESTION:Sleep). Add them to Supported /
// MoveThroughTarget.Kinds and to the classify switches below.

/// <summary>Kind vocabulary for <see cref="WorldInteractionTarget"/>.</summary>
public static class WorldInteractionKinds
{
    /// <summary>Wire key of a door's / warp's expected destination location token.</summary>
    public const string ExpectedLocationKey = "target_location_id";
    public const string Chest = "chest";
    public const string Door = "door";
    public const string Forage = "forage";
    // WS3b: talk to a villager / eat the held edible item (both open a DialogueBox in the press frame).
    public const string Npc = "npc";
    public const string Eat = "eat";

    public const string ContainerOpened = "ContainerOpened";
    public const string LocationChanged = "LocationChanged";
    /// <summary>Expected location reached, but an arrival event / menu keeps the player busy (review 2026-10-07).</summary>
    public const string LocationChangedEventUp = "LocationChangedEventUp";
    public const string ItemPickedUp = "ItemPickedUp";
    public const string DialogueOpened = "DialogueOpened";
    public const string QuestionOpened = "QuestionOpened";

    /// <summary>
    /// Wire vocabulary: the v1 kinds plus the WS-G6 extras kinds (<see cref="WorldExtras.Kinds"/>). A kind on the wire
    /// is admitted only when the mod captured the same target, and the extras capture runs only with the default-OFF
    /// WorldExtrasEnabled flag on the pad path, so the flag-off behaviour (and every v1 digest) is unchanged.
    /// </summary>
    public static readonly IReadOnlyCollection<string> Supported =
        new HashSet<string>(new[] { Chest, Door, Forage, Npc, Eat, AnimalHouseRoute.Kind, GenericWorld.Action, GenericWorld.Use }.Concat(WorldExtras.Kinds).Concat(EventControlAction.Kinds), StringComparer.Ordinal);

    public static bool IsSupported(string? kind) => kind is not null && Supported.Contains(kind);

    public static bool RequiresExpectedLocation(string? kind) =>
        kind == AnimalHouseRoute.Kind || string.Equals(kind, Door, StringComparison.Ordinal) || WorldExtras.RequiresExpectedLocation(kind);

    /// <summary>Kinds whose success is a menu receiver (chest ItemGrabMenu, npc / eat DialogueBox, G6 question / elevator).</summary>
    public static bool HasReceiver(string? kind) => kind is Chest or Npc or Eat || WorldExtras.IsReceiver(kind);

    public static string ExpectedOutcome(string? kind) => kind switch
    {
        GenericWorld.Action or GenericWorld.Use => GenericWorld.Changed,
        EventControlAction.Target or EventControlAction.TargetRoute => "EventTargetActivated",
        EventControlAction.Npc or EventControlAction.NpcRoute => "DialogueOpened",
        EventControlAction.Shop or EventControlAction.ShopRoute => "MenuOpened",
        EventControlAction.Seat or EventControlAction.SeatRoute => "FestivalSeated",
        EventControlAction.Interact or EventControlAction.InteractRoute => "EventInteractionObserved",
        EventControlAction.Egg or EventControlAction.EggRoute => "FestivalEggCollected",
        Door or AnimalHouseRoute.Kind => LocationChanged,
        Forage => ItemPickedUp,
        Npc => DialogueOpened,
        Eat => QuestionOpened,
        _ when WorldExtras.IsKind(kind) => WorldExtras.OutcomeOf(kind),
        _ => ContainerOpened,
    };

    public static BgActionKind ToActionKind(string? kind) => kind switch
    {
        GenericWorld.Action or GenericWorld.Use => BgActionKind.Generic,
        EventControlAction.Npc or EventControlAction.Shop or EventControlAction.Interact => BgActionKind.EventReceiver,
        _ when EventControlAction.IsKind(kind) => BgActionKind.Event,
        Door => BgActionKind.Door,
        Forage => BgActionKind.Forage,
        Npc => BgActionKind.Npc,
        Eat => BgActionKind.Eat,
        // WS-G6: the ladder warps exactly like a door (expected location, LocationChanged).
        WorldExtras.LadderDown => BgActionKind.Door,
        _ when WorldExtras.IsReceiver(kind) => BgActionKind.ExtraReceiver,
        _ when WorldExtras.IsKind(kind) => BgActionKind.Extra,
        _ => BgActionKind.Chest,
    };

    /// <summary>Location token form (SceneIdentity.Token output): non-empty, at most 128 printable ASCII, no spaces.</summary>
    public static bool IsLocationToken(string? value) =>
        !string.IsNullOrEmpty(value) && value.Length <= 128 && value.All(c => c > ' ' && c < 127);
}

/// <summary>
/// Review 2026-10-07: Building.doAction only acts on a grab tile it covers (footprint) or, within its
/// tile-property radius, on a tile that is NOT passable (data tile action / obelisk). A passable tile
/// inside the radius (e.g. a store chest next to a shed) stays with the objects rule.
/// </summary>
public static class WorldInteractionBuildingRule
{
    public static bool Blocks(bool footprint, bool withinPropertyRadius, bool tilePassable) =>
        footprint || (withinPropertyRadius && !tilePassable);

    /// <summary>FIXB21 D-B18-4 (live V2B18PET-b: the FIXB19 footprint / radius rule refused the stand (54,9) for Dudley
    /// at (54,8), a tile ARTI petted from in batch 16): vanilla Building.doAction (Building.cs:933) takes the A press only
    /// on the human door (with an interior), the animal door, an action tile of the building data (inside the property
    /// radius and not passable) or anything on a building subclass that overrides doAction (bin, mill, fish pond...).
    /// Everything else falls through to the characters: the farmhouse's porch area does not hide a pet.</summary>
    public static bool TakesPress(bool humanDoorWithIndoors, bool animalDoor, bool actionTile, bool subclassFootprint) =>
        humanDoorWithIndoors || animalDoor || actionTile || subclassFootprint;

    /// <summary>FIXB21 review MAJOR: the 1.6 building subclasses whose doAction override takes the press on their tiles:
    /// FishPond and JunimoHut on their footprint, ShippingBin on (tileX..tileX+1, tileY). Barn / Coop / Stable / Mill /
    /// Greenhouse / PetBowl do not override it (their footprint never hides a character).</summary>
    public static bool SubclassTakes(string typeName, bool footprint, int tileX, int tileY, int gx, int gy) =>
        typeName switch
        {
            "FishPond" or "JunimoHut" => footprint,
            "ShippingBin" => gy == tileY && gx >= tileX && gx <= tileX + 1,
            _ => false,
        };

    /// <summary>FIXB21 review: Building.doAction on an occupied tile of a building under construction shows its dialogue
    /// (a menu would open instead of the press): such a tile is refused as `building_under_construction`.</summary>
    public const string UnderConstructionMiss = "building_under_construction";
}

/// <summary>Per-kind pinned action target (2026-10-07 WS3a). Chest keeps the M1 path.</summary>
public enum BgActionKind
{
    Chest, Door, Forage, Npc, Eat,
    /// <summary>WS-G6 effect kinds (pet, animals, horse, shake, harvest, machines, hopper, place): judged by BgExtraFacts.</summary>
    Extra,
    /// <summary>WS-G6 synchronous receiver kinds (shaft / bus / minecart question, MineElevatorMenu).</summary>
    ExtraReceiver,
    Event, EventReceiver,
    Generic,
}

/// <summary>
/// Read-only effect facts sampled by the runtime (UpdateTicked) for a pinned door / forage
/// target. Location is the token form; "" while the farmhand has no current location
/// (server round-trip during a warp).
/// </summary>
public sealed record BgActionEffectFacts(
    string LocationToken,
    bool WarpObserved,
    bool Transitioning,
    bool PlayerFree,
    bool MenuOpen,
    string? MenuType,
    bool TargetPresent,
    int ItemCount,
    // Game1.eventUp / CurrentEvent at the sample (arrival cutscene keeps the player busy).
    bool EventUp = false,
    // WS3b: the open menu is the pinned target's expected receiver (npc DialogueBox / Eat question).
    bool ExpectedReceiver = false,
    // WS-G6: the extras kind readback (null for every v1 kind).
    BgExtraFacts? Extra = null,
    // FIXB48: indoor animal-product pickup requires exactly one, unlike Gatherer forage.
    bool SinglePickup = false,
    EventActionFacts? Event = null,
    GenericWorldFrame? Generic = null,
    // Native pending warp identity, sampled with the effect; never inferred from a fade alone.
    string? PendingLocationToken = null, double? FadeAlpha = null, string? RequestEpoch = null);

/// <summary>Effect verdict mapped onto the WorldInteractionVerdict vocabulary.</summary>
public sealed record BgActionEffectVerdict(string Outcome, bool Success, string? Reason, string? ResultLocationId, int Delta);

/// <summary>
/// Settle and classification rules per kind (brief §3.5). Pure: the caller supplies the
/// facts at arm (<c>before</c>), the latest facts, whether any transition was ever seen,
/// and the monotonic time since the first release attempt.
/// </summary>
public static class BgActionEffect
{
    /// <summary>Farmhand warps wait for host location data; tune from the live witness.</summary>
    public const long WarpWaitMs = 8000;
    /// <summary>A door press that starts no transition within this bound had no effect.</summary>
    public const long NoTransitionMs = BgHoldCompletion.MenuWaitMs;

    public const string InteractionNoEffect = "InteractionNoEffect";
    public const string UnexpectedMenu = "UNEXPECTED_MENU:";
    public const string UnexpectedLocation = "UNEXPECTED_LOCATION:";
    public const string WarpTimeout = "WARP_TIMEOUT";
    public const string TransitionWithoutChange = "TRANSITION_WITHOUT_LOCATION_CHANGE";
    public const string ForageEffectMismatch = "FORAGE_EFFECT_MISMATCH";
    public const string EffectUnread = "EFFECT_UNREAD";

    static bool Arrived(string? expected, BgActionEffectFacts now) =>
        now.WarpObserved && expected is not null && string.Equals(now.LocationToken, expected, StringComparison.Ordinal)
        && (!now.Transitioning || now.EventUp || now.MenuOpen);

    static bool WarpDone(string? expected, BgActionEffectFacts now) =>
        Arrived(expected, now) && now.PlayerFree && !now.MenuOpen && !now.EventUp;

    /// <summary>Arrived in the expected location while an event / menu holds the player (never WARP_TIMEOUT).</summary>
    static bool ArrivedBusy(string? expected, BgActionEffectFacts now) =>
        Arrived(expected, now) && (now.EventUp || now.MenuOpen || !now.PlayerFree);

    public static bool Settled(BgActionKind kind, string? expected, BgActionEffectFacts before, BgActionEffectFacts now,
        bool transitionSeen, long sinceReleaseMs)
    {
        if (kind == BgActionKind.Generic)
            return now.Generic is { } generic && (now.MenuOpen || !now.Transitioning && now.PlayerFree
                && sinceReleaseMs >= GenericWorld.SettleMs) || sinceReleaseMs >= WarpWaitMs;
        if (kind is BgActionKind.Event or BgActionKind.EventReceiver)
            return EventControlAction.Classify(before.Event, now.Event).Success || sinceReleaseMs >= NoTransitionMs;
        if (BgReceiverEffect.Handles(kind))
            return BgReceiverEffect.Settled(now, sinceReleaseMs);
        if (kind == BgActionKind.ExtraReceiver)
            return BgExtrasEffect.SettledReceiver(now, sinceReleaseMs);
        if (kind == BgActionKind.Extra)
            return BgExtrasEffect.Settled(before, now, sinceReleaseMs);
        if (now.MenuOpen)
            return true;
        switch (kind)
        {
            case BgActionKind.Door:
                if (WarpDone(expected, now) || (Arrived(expected, now) && (now.EventUp || now.MenuOpen)))
                    return true;
                // Arrived somewhere else: nothing more can make it the expected location.
                if (now.WarpObserved && !now.Transitioning && now.LocationToken.Length > 0
                    && !string.Equals(now.LocationToken, before.LocationToken, StringComparison.Ordinal)
                    && !string.Equals(now.LocationToken, expected, StringComparison.Ordinal))
                    return true;
                if (!transitionSeen && string.Equals(now.LocationToken, before.LocationToken, StringComparison.Ordinal)
                    && sinceReleaseMs >= NoTransitionMs)
                    return true;
                return sinceReleaseMs >= WarpWaitMs;
            case BgActionKind.Forage:
                if (!now.TargetPresent && (before.SinglePickup
                    ? now.ItemCount - before.ItemCount == 1 : now.ItemCount >= before.ItemCount + 1))
                    return true;
                return sinceReleaseMs >= NoTransitionMs;
            default:
                return true;
        }
    }

    public static BgActionEffectVerdict Classify(BgActionKind kind, string? expected, BgActionEffectFacts? before,
        BgActionEffectFacts? now, bool transitionSeen)
    {
        if (before is null || now is null)
            return Uncertain(EffectUnread);
        if (kind == BgActionKind.Generic)
            return GenericWorld.Classify(before.Generic, now.Generic);
        if (kind is BgActionKind.Event or BgActionKind.EventReceiver)
            return EventControlAction.Classify(before.Event, now.Event);
        if (BgReceiverEffect.Handles(kind))
            return BgReceiverEffect.Classify(kind, before, now);
        if (kind == BgActionKind.ExtraReceiver)
            return BgExtrasEffect.ClassifyReceiver(before, now);
        if (kind == BgActionKind.Extra)
            return BgExtrasEffect.Classify(before, now);
        if (kind == BgActionKind.Door && !WarpDone(expected, now) && ArrivedBusy(expected, now))
            return new BgActionEffectVerdict(WorldInteractionKinds.LocationChangedEventUp, true,
                now.EventUp ? "EVENT_UP" : now.MenuOpen ? UnexpectedMenu + (now.MenuType ?? "unknown") : "PLAYER_NOT_FREE",
                now.LocationToken, 0);
        // ADR 136: a found quest item's own dialogue is its receiver; the pickup below still has to be read, or it stays unexpected.
        if (now.MenuOpen && !(kind == BgActionKind.Forage && now.ExpectedReceiver))
            return Uncertain(UnexpectedMenu + (now.MenuType ?? "unknown"));
        bool sameLocation = string.Equals(now.LocationToken, before.LocationToken, StringComparison.Ordinal);
        switch (kind)
        {
            case BgActionKind.Door:
                if (WarpDone(expected, now))
                    return new BgActionEffectVerdict(WorldInteractionKinds.LocationChanged, true, null, now.LocationToken, 0);
                if (!sameLocation && now.LocationToken.Length > 0 && !now.Transitioning
                    && !string.Equals(now.LocationToken, expected, StringComparison.Ordinal))
                    return Uncertain(UnexpectedLocation + now.LocationToken);
                if (now.Transitioning || !sameLocation)
                    return Uncertain(WarpTimeout);
                return transitionSeen
                    ? Uncertain(TransitionWithoutChange)
                    : new BgActionEffectVerdict(InteractionNoEffect, false, null, null, 0);
            case BgActionKind.Forage:
                if (!sameLocation)
                    return Uncertain(UnexpectedLocation + now.LocationToken);
                int delta = now.ItemCount - before.ItemCount;
                if (before.TargetPresent && !now.TargetPresent && (before.SinglePickup ? delta == 1 : delta >= 1))
                    return new BgActionEffectVerdict(WorldInteractionKinds.ItemPickedUp, true, null, null, delta);
                if (now.MenuOpen)
                    return Uncertain(UnexpectedMenu + (now.MenuType ?? "unknown"));
                if (!before.SinglePickup && now.TargetPresent && delta == 0)
                    return new BgActionEffectVerdict(InteractionNoEffect, false, null, null, 0);
                return Uncertain(ForageEffectMismatch);
            default:
                return Uncertain(EffectUnread);
        }
    }

    static BgActionEffectVerdict Uncertain(string reason) =>
        new(BgHoldCompletion.InteractionUncertain, false, reason, null, 0);
}

/// <summary>
/// One observed walk-onto warp (brief §3.7): the actor stands on the cardinal neighbour
/// of a non-npcOnly warp tile and faces it. The move run walks <see cref="Ticks"/> frames
/// toward it and ends at the expected location transition. WS3b extension: kind "bed".
/// </summary>
public readonly record struct MoveThroughTarget(string Direction, int Ticks, string Kind, string SourceFactId,
    string TargetToken, int TileX, int TileY, int ActorTileX, int ActorTileY, string? ExpectedLocationId)
{
    public const string Primitive = "move_through";
    public const string KindWarp = "warp";
    /// <summary>WS3b: walk onto the owned bed's sleep tile (TouchAction Sleep opens the Sleep question).</summary>
    public const string KindBed = "bed";
    /// <summary>
    /// D29 (live B3W6c / B4W1a): a distance-2 warp with 20 ticks travelled 50-74 px and stopped short; the
    /// runtime sizes it up to 47. Same move run, still inside BgMoveFramePlan's 1..60; containment unchanged.
    /// </summary>
    public const int MaxTicks = 48;
    /// <summary>Duration is execution sizing, not target identity; validate it independently.</summary>
    public bool Matches(MoveThroughTarget wanted) => wanted.IsValid && this.IsValid
        && this with { Ticks = wanted.Ticks } == wanted;

    public static MoveThroughTarget Observe(string direction, string source, CommandInboxIdentity identity,
        int x, int y, int ax, int ay, int facing, string expected)
    {
        string token = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new object?[] {
                KindWarp, source, identity, ax, ay, facing, expected })))).ToLowerInvariant();
        return new(direction, 1, KindWarp, source, token, x, y, ax, ay, expected);
    }
    /// <summary>
    /// Live B2W6a (D6): an approach that ends ON the tile next to an edge warp can already cross it
    /// (a 10-tick step overshoots). The runtime stops one tile short and move_through walks the
    /// last one or two tiles straight toward the warp.
    /// </summary>
    public const int MaxDistance = 2;
    public static readonly IReadOnlyCollection<string> Kinds = new HashSet<string>(StringComparer.Ordinal) { KindWarp, KindBed };
    static readonly string[] BaseKeys = { "direction", "ticks", "kind", "source_fact_id", "target_token", "tile_x", "tile_y", "actor_tile_x", "actor_tile_y" };

    public static (int Dx, int Dy) Vector(string? direction) => direction switch
    {
        "up" => (0, -1),
        "down" => (0, 1),
        "left" => (-1, 0),
        "right" => (1, 0),
        _ => (0, 0),
    };

    public static int FacingOf(string? direction) => direction switch
    {
        "up" => 0,
        "right" => 1,
        "down" => 2,
        "left" => 3,
        _ => -1,
    };

    public bool IsValid
    {
        get
        {
            (int dx, int dy) = Vector(Direction);
            return (dx != 0 || dy != 0) && Ticks is >= 1 and <= MaxTicks && Kinds.Contains(Kind ?? "")
                && !string.IsNullOrWhiteSpace(SourceFactId) && SourceFactId.Length <= 256
                && !string.IsNullOrWhiteSpace(TargetToken) && TargetToken.Length <= 128
                && TileX is >= -1 and <= 4097 && TileY is >= -1 and <= 4097
                && ActorTileX is >= 0 and <= 4096 && ActorTileY is >= 0 and <= 4096
                && (Kind == KindBed ? BgBedThrough.ReachValid(dx, dy, TileX - ActorTileX, TileY - ActorTileY)
                    : (TileX - ActorTileX == dx && TileY - ActorTileY == dy)
                      || (TileX - ActorTileX == MaxDistance * dx && TileY - ActorTileY == MaxDistance * dy))
                && (Kind == KindWarp ? WorldInteractionKinds.IsLocationToken(ExpectedLocationId) : ExpectedLocationId is null);
        }
    }

    public Dictionary<string, object> Args()
    {
        var args = new Dictionary<string, object>
        {
            ["direction"] = Direction, ["ticks"] = Ticks, ["kind"] = Kind, ["source_fact_id"] = SourceFactId,
            ["target_token"] = TargetToken, ["tile_x"] = TileX, ["tile_y"] = TileY,
            ["actor_tile_x"] = ActorTileX, ["actor_tile_y"] = ActorTileY,
        };
        if (ExpectedLocationId is { } expected)
            args[WorldInteractionKinds.ExpectedLocationKey] = expected;
        return args;
    }

    /// <summary>The published observation (ui.context move_through_target): everything but ticks.</summary>
    public Dictionary<string, object> ObservedArgs()
    {
        var args = this.Args();
        args.Remove("ticks");
        return args;
    }

    public static bool TryParse(JsonElement args, out MoveThroughTarget target)
    {
        target = default;
        if (args.ValueKind != JsonValueKind.Object
            || !args.TryGetProperty("kind", out JsonElement kindValue) || kindValue.ValueKind != JsonValueKind.String
            || kindValue.GetString() is not { } kind || !Kinds.Contains(kind))
            return false;
        bool expectation = kind == KindWarp;
        string[] keys = expectation ? BaseKeys.Append(WorldInteractionKinds.ExpectedLocationKey).ToArray() : BaseKeys;
        if (args.EnumerateObject().Count() != keys.Length
            || args.EnumerateObject().Select(p => p.Name).Distinct().Count() != keys.Length
            || args.EnumerateObject().Any(p => !keys.Contains(p.Name)))
            return false;
        foreach (string text in new[] { "direction", "source_fact_id", "target_token" })
            if (args.GetProperty(text).ValueKind != JsonValueKind.String)
                return false;
        string? expected = null;
        if (expectation)
        {
            if (args.GetProperty(WorldInteractionKinds.ExpectedLocationKey).ValueKind != JsonValueKind.String)
                return false;
            expected = args.GetProperty(WorldInteractionKinds.ExpectedLocationKey).GetString();
        }
        string[] ints = { "ticks", "tile_x", "tile_y", "actor_tile_x", "actor_tile_y" };
        int[] n = new int[ints.Length];
        for (int i = 0; i < ints.Length; i++)
            if (args.GetProperty(ints[i]).ValueKind != JsonValueKind.Number || !args.GetProperty(ints[i]).TryGetInt32(out n[i]))
                return false;
        target = new MoveThroughTarget(args.GetProperty("direction").GetString()!, n[0], kind,
            args.GetProperty("source_fact_id").GetString()!, args.GetProperty("target_token").GetString()!,
            n[1], n[2], n[3], n[4], expected);
        return target.IsValid;
    }
}

/// <summary>Facts for the move_through terminal decision (sampled every UpdateTicked of a through run).</summary>
public sealed record BgThroughFacts(
    bool WarpStarted,
    string? WrongDestination,
    bool HeldAfterWarpStart,
    bool ReleaseVerified,
    bool WarpObserved,
    string LocationToken,
    bool Transitioning,
    bool PlayerFree,
    bool MenuOpen,
    long SinceWarpStartMs,
    bool EventUp = false);

/// <summary>
/// Terminal rules for a move_through run (brief §3.7). Returns null while the run must keep
/// waiting (or, before any warp start, to fall back to the ordinary move completion).
/// </summary>
public static class BgMoveThrough
{
    public const string Warped = "WARPED";
    /// <summary>Expected location reached; an arrival event / menu holds the player (success, never WARP_TIMEOUT).</summary>
    public const string WarpedEventUp = "WARPED_EVENT_UP";
    public const string HeldAfterWarp = "MOVEMENT_AFTER_WARP_START";

    /// <summary>Warp start detection: Game1.isWarping with a pending location request.</summary>
    public static (bool Started, string? Wrong) ObserveWarpStart(bool isWarping, string? requestToken, string expected)
    {
        // Game1 clears isWarping BEFORE asking the host for the destination.
        // The caller admits this only after its own consumed input and a clean baseline.
        if (string.IsNullOrEmpty(requestToken))
            return (false, null);
        return string.Equals(requestToken, expected, StringComparison.Ordinal) ? (true, null) : (false, requestToken);
    }

    public static string? Decide(BgThroughFacts f, string expected)
    {
        if (f.WrongDestination is { } wrong)
            return "UNCERTAIN(" + BgActionEffect.UnexpectedLocation + wrong + ")";
        if (!f.WarpStarted)
            return null;
        if (f.HeldAfterWarpStart)
            return "UNCERTAIN(" + HeldAfterWarp + ")";
        bool arrived = f.ReleaseVerified && f.WarpObserved && string.Equals(f.LocationToken, expected, StringComparison.Ordinal)
            && (!f.Transitioning || f.EventUp || f.MenuOpen);
        if (arrived && f.PlayerFree && !f.MenuOpen && !f.EventUp)
            return Warped;
        if (arrived && (f.EventUp || f.MenuOpen || f.SinceWarpStartMs >= BgActionEffect.WarpWaitMs))
            return WarpedEventUp;
        if (f.MenuOpen)
            return "UNCERTAIN(" + BgActionEffect.UnexpectedMenu.TrimEnd(':') + ")";
        if (f.SinceWarpStartMs >= BgActionEffect.WarpWaitMs)
        {
            if (!f.ReleaseVerified)
                return "UNCERTAIN(RELEASE_UNVERIFIED)";
            if (!f.Transitioning && f.LocationToken.Length > 0 && !string.Equals(f.LocationToken, expected, StringComparison.Ordinal))
                return "UNCERTAIN(" + BgActionEffect.UnexpectedLocation + f.LocationToken + ")";
            return "UNCERTAIN(" + BgActionEffect.WarpTimeout + ")";
        }
        return null;
    }
}
