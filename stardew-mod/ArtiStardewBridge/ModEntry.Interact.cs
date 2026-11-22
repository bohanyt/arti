using System.Text.Json;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Buildings;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

// WS3a (Primitives v1, 2026-10-07): background world_interact kinds door / forage and the
// walk-onto warp primitive move_through. Read-only capture mirroring GameLocation.checkAction
// order (decompiled 1.6.15) and the target closures the background runtime re-checks. No new
// input path: presses stay the BgHoldKind.Action retained-press hold, walking stays the
// background move run. Evidence ceiling: UNIT_TESTED until the CT live witness.
public sealed partial class ModEntry
{
#if !PM1_NATIVE_CAPTURE_TEST
    /// <summary>WS3a background kinds are captured only with the default-OFF flag on.</summary>
    bool InteractKindsEnabled => this.config.WorldInteractKindsEnabled;

    /// <summary>
    /// Common invariants for door / forage (brief §3.2): a plain tool in hand (no gift, no
    /// placement, no eat prompt, no weapon special; a picked item cannot land in the active
    /// slot), no mount, no event / festival, not sitting, grab tile is not the own tile.
    /// </summary>
    static string? InteractInvariantMiss(Farmer player, Vector2 tile)
    {
        if (player.CurrentItem is not Tool tool || tool is MeleeWeapon || tool is Slingshot || tool is FishingRod
            || player.ActiveObject is not null)
            return "held_item_not_plain_tool";
        if (player.isRidingHorse() || player.mount is not null)
            return "mounted";
        if (Game1.eventUp || Game1.CurrentEvent is not null || player.currentLocation?.currentEvent is not null || Game1.isFestival())
            return "event_or_festival";
        if (player.IsSitting())
            return "sitting";
        if (tile == player.Tile)
            return "grab_tile_is_own_tile";
        return null;
    }

    static bool IsLooseAnimalProduct(GameLocation location, SObject obj) => AnimalProductPickup.Allowed(
        obj.QualifiedItemId, obj.Category, location.ParentBuilding?.buildingType.Value, location is AnimalHouse,
        obj.GetType() == typeof(SObject), obj.IsSpawnedObject, obj.CanBeGrabbed, obj.CanBeSetDown,
        obj.questItem.Value, obj.bigCraftable.Value, obj.GetMachineData() is not null || obj.heldObject.Value is not null,
        obj.isTemporarilyInvisible, obj.Stack, obj.Type);

    /// <summary>
    /// WS3a resolution at the grab tile, mirroring GameLocation.checkAction order: pets/animals,
    /// building doors, other farmers, NPCs, resource clumps, objects (chest rule unchanged,
    /// spawned forage), terrain / large terrain, furniture, Buildings-layer Action (Warp only).
    /// Anything earlier in that order than the resolved candidate is a miss.
    /// </summary>
    InteractKindCapture CaptureInteractKind(Farmer player, Vector2 tile)
    {
        GameLocation location = player.currentLocation;
        string locationRaw = location.NameOrUniqueName ?? location.Name ?? "_";
        Rectangle rect = new((int)tile.X * 64, (int)tile.Y * 64, 64, 64);
        bool objectHere = location.objects.TryGetValue(tile, out SObject? obj) && obj is not null;
        // WS-G6 (flag WorldExtrasEnabled, pad path only): pets, animals, horse, machines, trees, crops, mines, bus /
        // minecart, placement. null = not an extras target: the v1 resolution below is unchanged.
        if (this.CaptureExtrasKind(player, tile) is { } extras)
            return extras;
        // WS3b: an edible object in hand with a clear eat site resolves to the eat prompt.
        if (this.CaptureEatKind(player, tile) is { } eat)
            return eat;

        if (!objectHere && location.animals.Values.Any(a => a is not null && a.GetBoundingBox().Intersects(rect)))
            return InteractKindCapture.Missed("animal_at_grab_tile");

        foreach (Building b in location.buildings)
        {
            if (b is null)
                continue;
            bool door = b.humanDoor.X + b.tileX.Value == (int)tile.X && b.humanDoor.Y + b.tileY.Value == (int)tile.Y
                && b.humanDoor.X >= 0 && b.humanDoor.Y >= 0;
            if (door && b.GetIndoors() is GameLocation indoors)
            {
                if (b.daysOfConstructionLeft.Value > 0 || b is GreenhouseBuilding || player.team.demolishLock.IsLocked() || b.isMoving)
                    return InteractKindCapture.Missed("building_door_not_enterable");
                // Building.doAction runs before the farmer / NPC checks in checkAction: an occupant cannot steal it.
                if (InteractInvariantMiss(player, tile) is string miss)
                    return InteractKindCapture.Missed(miss);
                string expected = SceneIdentity.Token(indoors.NameOrUniqueName);
                string source = SceneIdentity.BuildingDoorFactId(locationRaw, (int)tile.X, (int)tile.Y, indoors.NameOrUniqueName);
                string instance = this.worldTargetInstanceTokenTracker.Observe(b)!;
                string token = this.InteractToken(WorldInteractionKinds.Door, instance, source, player, expected);
                var target = new WorldInteractionTarget(token, source, (int)tile.X, (int)tile.Y, (int)player.Tile.X, (int)player.Tile.Y,
                    player.FacingDirection, WorldInteractionKinds.Door, expected);
                if (!target.IsValid)
                    return InteractKindCapture.Missed("target_invalid");
                Building pinned = b;
                GameLocation pinnedIndoors = indoors;
                GameLocation pinnedLocation = location;
                Vector2 pinnedTile = tile;
                bool StillPresent() => Game1.player is Farmer p && ReferenceEquals(p.currentLocation, pinnedLocation)
                    && pinnedLocation.buildings.Contains(pinned) && ReferenceEquals(pinned.GetIndoors(), pinnedIndoors)
                    && pinned.daysOfConstructionLeft.Value <= 0 && !pinned.isMoving && !p.team.demolishLock.IsLocked()
                    && p.GetGrabTile() == pinnedTile;
                return new InteractKindCapture(target, new BgActionTarget(BgActionKind.Door, null, expected, "", StillPresent,
                    () => SampleInteractEffect(StillPresent, "")), null, false);
            }
            if (WorldInteractionBuildingRule.Blocks(b.occupiesTile(tile), b.occupiesTile(tile, applyTilePropertyRadius: true),
                    b.isTilePassable(tile)))
                return InteractKindCapture.Missed("building_at_grab_tile");
        }

        // WS3b: a talkable villager at the grab tile (checkAction NPC step) resolves to npc.
        if (this.CaptureNpcTalkKind(player, location, rect, tile) is { } talk)
            return talk;
        if (this.InteractOccupantMiss(player, location, rect, tile, includeBelow: false) is string blocked)
        {
            // An NPC / other farmer in front of a chest turns the press into talk / gift: no target.
            return InteractKindCapture.Missed(blocked);
        }
        if (location.resourceClumps.Any(c => c is not null && c.getBoundingBox().Intersects(rect)))
            return InteractKindCapture.Missed("clump_at_grab_tile");

        if (objectHere)
        {
            if (obj is StardewValley.Objects.Chest)
                return InteractKindCapture.Chest;
            // FIX77C (ADR 136): what checkAction picks up for this farmer, not a family of items.
            bool forage = GroundPickup.NativePickup(obj!.IsSpawnedObject, obj.Type);
            bool animalProduct = IsLooseAnimalProduct(location, obj);
            // Allowlisted products always use their strict guard, even if modded data gives them a forage tag.
            bool productIdentity = AnimalProductPickup.IsProductId(obj.QualifiedItemId);
            if (productIdentity ? !animalProduct : !forage)
                return InteractKindCapture.Chest; // the existing chest rule reports the miss
            if (GroundPickup.QuestMiss(obj.questItem.Value, obj.questId.Value, obj.questId.Value is { } questId && player.hasQuest(questId)) is string questMiss)
                return InteractKindCapture.Missed(questMiss); // the game's own refusal: a press here would have no effect
            if (InteractInvariantMiss(player, tile) is string miss)
                return InteractKindCapture.Missed(miss);
            if (!NativeCollectionFacts.AcceptsPickup(player, location, obj))
                return InteractKindCapture.Missed("inventory_cannot_accept");
            string qid = obj.QualifiedItemId ?? "";
            string source = SceneIdentity.ObjectFactId(locationRaw, (int)tile.X, (int)tile.Y, qid);
            string instance = this.worldTargetInstanceTokenTracker.Observe(obj)!;
            string token = this.InteractToken(WorldInteractionKinds.Forage, instance, source, player, null);
            var target = new WorldInteractionTarget(token, source, (int)tile.X, (int)tile.Y, (int)player.Tile.X, (int)player.Tile.Y,
                player.FacingDirection, WorldInteractionKinds.Forage, null);
            if (!target.IsValid)
                return InteractKindCapture.Missed("target_invalid");
            SObject pinnedObject = obj;
            GameLocation pinnedLocation = location;
            Vector2 pinnedTile = tile;
            bool ObjectPresent() => pinnedLocation.objects.TryGetValue(pinnedTile, out SObject? now) && ReferenceEquals(now, pinnedObject);
            Rectangle pinnedRect = rect;
            // Review 2026-10-07: farmers / NPCs come before objects in checkAction; one stepping onto the grab
            // tile between arm and press would turn the press into talk / gift.
            bool StillPresent() => Game1.player is Farmer p && ReferenceEquals(p.currentLocation, pinnedLocation)
                && ObjectPresent() && p.GetGrabTile() == pinnedTile && NativeCollectionFacts.AcceptsPickup(p, pinnedLocation, pinnedObject)
                && pinnedObject.QualifiedItemId == qid && (!animalProduct || IsLooseAnimalProduct(pinnedLocation, pinnedObject))
                && this.InteractOccupantMiss(p, pinnedLocation, pinnedRect, pinnedTile, includeBelow: false) is null;
            // ADR 136: a found quest item opens the game's dialogue inside the press frame; no other forage names a receiver.
            Func<object?, bool>? found = GroundPickup.MayOpenFoundDialogue(obj.questItem.Value)
                ? menu => menu is StardewValley.Menus.DialogueBox { isQuestion: false } : null;
            return new InteractKindCapture(target, new BgActionTarget(BgActionKind.Forage, null, null, qid, StillPresent,
                // An object replacing the original at this tile is not verified removal for a product.
                () => SampleInteractEffect(animalProduct ? () => pinnedLocation.objects.ContainsKey(pinnedTile) : ObjectPresent, qid)
                    with { SinglePickup = animalProduct, ExpectedReceiver = found?.Invoke(Game1.activeClickableMenu) == true })
                { ExpectedReceiver = found }, null, false);
        }

        // Terrain feature boxes are their own tile (trees, grass, dirt, flooring): one lookup, no map scan.
        if (location.terrainFeatures.TryGetValue(tile, out TerrainFeature? feature) && feature is not null)
            return InteractKindCapture.Missed("terrain_at_grab_tile");
        if (location.largeTerrainFeatures.Any(f => f is not null && f.getBoundingBox().Intersects(rect)))
            return InteractKindCapture.Missed("large_terrain_at_grab_tile");
        if (location.furniture.Any(f => f is not null && f.GetBoundingBox().Intersects(rect)))
            return InteractKindCapture.Missed("furniture_at_grab_tile");

        return this.CaptureTileDoor(player, location, tile, rect);
    }

    /// <summary>Another farmer or any character (villager, pet, horse, child) at the grab tile (or the tile below).</summary>
    string? InteractOccupantMiss(Farmer player, GameLocation location, Rectangle rect, Vector2 tile, bool includeBelow)
    {
        foreach (Farmer farmer in location.farmers)
        {
            if (farmer is not null && !ReferenceEquals(farmer, player) && farmer.GetBoundingBox().Intersects(rect))
                return "farmer_at_grab_tile";
        }
        foreach (NPC npc in location.characters)
        {
            if (npc is not null && npc.GetBoundingBox().Intersects(rect))
                return "npc_at_grab_tile";
        }
        if (includeBelow && location.isCharacterAtTile(tile + new Vector2(0f, 1f)) is not null)
            return "npc_below_action_tile";
        return null;
    }

    // ------------------------------------------------------------------
    // move_through (walk-onto warp)
    // ------------------------------------------------------------------

    /// <summary>
    /// The non-npcOnly warp on the faced cardinal neighbour of the actor tile, as a
    /// move_through observation (ticks filled by the runtime). Background route only.
    /// </summary>
#endif
    (MoveThroughTarget? Target, BgThroughExpectation? Expectation) CaptureMoveThroughTarget()
    {
        if (!this.config.MoveThroughEnabled || !this.BackgroundToolUiNow || this.backgroundMove is null
            || !this.config.Enabled || !this.config.WorldPressEnabled
            || !Context.IsWorldReady || Context.IsMainPlayer || Context.IsSplitScreen
            || Game1.activeClickableMenu is not null || Game1.keyboardDispatcher?.Subscriber is not null
            || !Context.IsPlayerFree || !Context.CanPlayerMove || Game1.player is not Farmer player
            || player.currentLocation is not GameLocation location || player.UsingTool
            || player.isRidingHorse() || player.mount is not null || player.IsSitting()
            || Game1.eventUp || Game1.CurrentEvent is not null || location.currentEvent is not null || Game1.isFestival())
            return (null, null);
        // Exactly one direction with a non-npcOnly warp one or two tiles straight ahead (nearest per
        // direction); the direction points at it. Facing is not required (deviation from the brief),
        // and live B2W6a (D6) showed the approach must stop one tile short of an edge warp.
        int ax = (int)player.Tile.X, ay = (int)player.Tile.Y;
        List<(string Direction, Warp Warp)> matches = new();
        foreach (string candidate in new[] { "up", "right", "down", "left" })
        {
            (int cx, int cy) = MoveThroughTarget.Vector(candidate);
            for (int k = 1; k <= MoveThroughTarget.MaxDistance; k++)
            {
                Warp? hit = location.warps.FirstOrDefault(w => w is not null && !w.npcOnly.Value
                    && w.X == ax + k * cx && w.Y == ay + k * cy);
                if (hit is null)
                    continue;
                matches.Add((candidate, hit));
                break;
            }
        }
        if (matches.Count == 0)
            return this.CaptureBedThroughTarget(player, location); // WS3b: own bed's sleep tile
        if (matches.Count != 1 || string.IsNullOrWhiteSpace(matches[0].Warp.TargetName)
            || matches[0].Warp.TargetName is "BoatTunnel" or "VolcanoEntrance")
            return (null, null);
        string direction = matches[0].Direction;
        Warp warp = matches[0].Warp;
        int wx = warp.X, wy = warp.Y;
        string locationRaw = location.NameOrUniqueName ?? location.Name ?? "_";
        string expected = SceneIdentity.Token(warp.TargetName);
        string source = SceneIdentity.WarpFactId(locationRaw, warp.X, warp.Y, warp.TargetName, warp.TargetX, warp.TargetY);
        CommandInboxIdentity identity = this.CurrentCommandIdentity();
        var target = MoveThroughTarget.Observe(direction, source, identity, wx, wy, ax, ay, player.FacingDirection, expected);
        if (!target.IsValid)
            return (null, null);
        static BgThroughProbe Probe()
        {
            Farmer? p = Game1.player;
            string? request = Game1.locationRequest?.Name;
            return new BgThroughProbe(
                Game1.isWarping,
                string.IsNullOrEmpty(request) ? null : SceneIdentity.Token(request),
                Game1.currentLocation is null ? "" : CurrentLocationId(p),
                Game1.isWarping || Game1.fadeToBlack || Game1.globalFade || Game1.fadeToBlackAlpha > 0.001 || Game1.locationRequest is not null
                    || Game1.currentLocation is null || p?.currentLocation is null,
                Context.IsWorldReady && Context.IsPlayerFree && Context.CanPlayerMove,
                Game1.activeClickableMenu is not null || Game1.dialogueUp,
                Game1.eventUp || Game1.CurrentEvent is not null, FadeAlpha: Game1.fadeToBlackAlpha,
                RequestEpoch: NativeTravelRequestEpoch());
        }
        return (target, new BgThroughExpectation(MoveThroughTarget.KindWarp, expected, Probe));
    }

#if !PM1_NATIVE_CAPTURE_TEST
    /// <summary>ui.context publication of the observed move_through target (absent when none).</summary>
    void EnrichMoveThroughContext(Dictionary<string, object> snapshot)
    {
        if (this.CaptureMoveThroughTarget().Target is { } target)
            snapshot["move_through_target"] = target.ObservedArgs();
    }

    /// <summary>
    /// Inactive move_through through the SAME inbox slot / lease / owner as move_cardinal:
    /// the request must equal the current observation (ticks aside) and binding, then the
    /// background runtime runs the ordinary move with the expected transition.
    /// </summary>
    private void ArmMoveThroughFromCommand(MenuStepCommandRequest request)
    {
        this.ApplyActionOwnerRecords();
        CommandInboxIdentity identity = this.CurrentCommandIdentity();
        var current = this.CaptureMoveThroughTarget();
        bool matches = request.ThroughTarget is { } wanted && current.Target is { } observed
            && current.Expectation is not null && observed.Matches(wanted);
        bool binding = MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request, identity,
            Context.IsWorldReady, Context.IsPlayerFree, Context.CanPlayerMove, out CommandRejectReason reason);
        if (!binding || !matches || this.backgroundMove is null)
        {
            this.Monitor.Log($"arti_command move_through refused: request_id={request.RequestId} binding={binding} "
                + $"target_matches={matches} window_active={Game1.game1?.IsActive == true}", LogLevel.Warn); // ADR 244: logged, not a term
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request,
                binding ? CommandRejectReason.BindingMismatch : reason, identity, NowUnixMs()));
            return;
        }
        BackgroundMoveTerminal? refused = this.backgroundMove.TryArmThrough(request, current.Expectation!);
        if (refused is not null)
        {
            this.WriteBackgroundTerminal(refused);
            return;
        }
        this.commandInbox.BindRun(this.backgroundMove.RunId);
        this.Monitor.Log(
            $"arti_command armed: request_id={request.RequestId} op={request.Op} primitive={request.Primitive} "
                + $"background run={this.backgroundMove.RunId} direction={request.Direction} ticks={request.Ticks} "
                + $"expected={request.ThroughTarget?.ExpectedLocationId} gameTicks={Game1.ticks}",
            LogLevel.Info);
    }

    /// <summary>move_through terminal writer (never CompleteWorldPress success for a through run).</summary>
    private MenuStepCommandResult CompleteMoveThroughTerminal(BackgroundMoveTerminal terminal, CommandInboxIdentity identity, long now)
    {
        if (terminal.Request.ThroughTarget is { Kind: MoveThroughTarget.KindBed })
            return this.CompleteBedThroughTerminal(terminal, identity, now); // WS3b
        bool warped = terminal.Kind == BackgroundTerminalKind.Completed
            && terminal.Label is BgMoveThrough.Warped or BgMoveThrough.WarpedEventUp;
        bool uncertain = terminal.Kind == BackgroundTerminalKind.Uncertain;
        if (warped)
        {
            if (this.config.Enabled && Context.IsWorldReady)
                this.AdvanceUiContextNative(this.screens.Value);
            identity = this.CurrentCommandIdentity();
        }
        return this.commandInbox.CompleteMoveThrough(terminal.Label, warped, uncertain, terminal.ConsumedH,
            !uncertain, warped ? terminal.ResultLocationId : null, identity, now);
    }

    /// <summary>World-interact verdict fields for the WS3a kinds (door: result location; forage: qid+delta).</summary>
    static (string? ResultLocationId, string? EffectDetail) InteractVerdictFields(BackgroundHoldResult hold, BgActionTarget? target)
    {
        BgActionEffectVerdict? effect = hold.Facts?.Effect;
        if (effect is null || !hold.Outcome.Opened)
            return (null, null);
        return (effect.ResultLocationId, BackgroundMoveRuntime.EffectDetailOf(target, effect));
    }
#endif
}
