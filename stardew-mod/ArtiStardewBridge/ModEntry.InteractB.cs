using System.Text.Json;
using ArtiStardewBridge.Observation;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Buildings;
using StardewValley.Locations;
using StardewValley.Menus;
using StardewValley.Objects;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

// WS3b (Primitives v1, 2026-10-07): background world_interact kinds npc (talk) and eat (eat
// prompt), and the bed variant of move_through. Read-only capture mirroring the decompiled
// 1.6.15 action-button path (Game1.pressActionButton / GameLocation.checkAction / NPC.checkAction)
// and the pinned-target closures the background runtime re-checks. No new input path: presses
// stay the BgHoldKind.Action retained-press hold, walking stays the background move run, and
// the DialogueBoxes they open are closed / answered only by WS2's ui_key (direct menu calls).
// Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the CT live witness.
public sealed partial class ModEntry
{
    bool NpcEatBedEnabled => this.config.NpcEatBedEnabled;

    static bool IsQuestionMenu(object? menu, string questionKey) =>
        menu is DialogueBox { isQuestion: true, characterDialogue: null }
        && string.Equals(Game1.currentLocation?.lastQuestionKey, questionKey, StringComparison.Ordinal);

    // ------------------------------------------------------------------
    // npc talk
    // ------------------------------------------------------------------

    /// <summary>
    /// checkAction step "other farmers / NPC boxes" at the grab tile. Returns null when no
    /// character intersects the tile (the WS3a resolution continues), else the talk target or
    /// a miss. Another farmer there is left to the WS3a occupant miss (null).
    /// </summary>
    InteractKindCapture? CaptureNpcTalkKind(Farmer player, GameLocation location, Rectangle rect, Vector2 tile)
    {
        if (!this.NpcEatBedEnabled)
            return null;
        if (location.farmers.Any(f => f is not null && !ReferenceEquals(f, player) && f.GetBoundingBox().Intersects(rect)))
            return null;
        List<NPC> atTile = location.characters.Where(c => c is not null && c.GetBoundingBox().Intersects(rect)).ToList();
        if (atTile.Count == 0)
        {
            // D27 diagnostic: a villager right next to the grab tile but not in it (stale scene tile / moved).
            Rectangle around = new(rect.X - 64, rect.Y - 64, 192, 192);
            if (location.characters.FirstOrDefault(c => c is not null && c.IsVillager && c.GetBoundingBox().Intersects(around)) is NPC near)
            {
                long now = Environment.TickCount64;
                if (now - this.lastNpcNearLogMs >= 5000)
                {
                    this.lastNpcNearLogMs = now;
                    this.Monitor.Log($"world_interact npc near grab tile: {near.Name} box={near.GetBoundingBox()} "
                        + $"tile={near.TilePoint} grab={tile}", LogLevel.Debug);
                }
                this.MaybeRefreshSceneForNpc("talk_miss", radius: 3);
            }
            return null;
        }
        if (InteractInvariantMiss(player, tile) is string invariant)
            return InteractKindCapture.Missed(invariant);
        NPC npc = atTile[0];
        Rectangle below = new(rect.X, rect.Y + 64, 64, 64);
        Rectangle above = new(rect.X, rect.Y - 64, 64, 64);
        int others = location.characters.Count(c => c is not null && !ReferenceEquals(c, npc)
            && (c.GetBoundingBox().Intersects(below) || c.GetBoundingBox().Intersects(above)));
        bool talked = player.friendshipData.TryGetValue(npc.Name ?? "", out Friendship? friendship) && friendship?.TalkedToToday == true;
        string? miss = BgNpcTalk.Miss(new BgNpcTalkFacts(npc.Name ?? "", npc.IsVillager, npc.CanSocialize, npc.IsInvisible,
            npc.isSleeping.Value, talked, string.Equals(player.spouse, npc.Name, StringComparison.Ordinal),
            Game1.eventUp || Game1.CurrentEvent is not null || Game1.isFestival(), atTile.Count, others));
        if (miss is not null)
            return InteractKindCapture.Missed(miss);
        string locationRaw = location.NameOrUniqueName ?? location.Name ?? "_";
        string source = this.NpcFactIdFor(location, locationRaw, npc);
        string instance = this.worldTargetInstanceTokenTracker.Observe(npc)!;
        string token = this.InteractToken(WorldInteractionKinds.Npc, instance, source, player, null);
        var target = new WorldInteractionTarget(token, source, (int)tile.X, (int)tile.Y, (int)player.Tile.X, (int)player.Tile.Y,
            player.FacingDirection, WorldInteractionKinds.Npc, null);
        if (!target.IsValid)
            return InteractKindCapture.Missed("target_invalid");
        NPC pinned = npc;
        GameLocation pinnedLocation = location;
        Vector2 pinnedTile = tile;
        Rectangle pinnedRect = rect;
        bool StillPresent() => Game1.player is Farmer p && ReferenceEquals(p.currentLocation, pinnedLocation)
            && p.GetGrabTile() == pinnedTile && pinnedLocation.characters.Contains(pinned)
            && pinned.GetBoundingBox().Intersects(pinnedRect) && !pinned.IsInvisible && !pinned.isSleeping.Value
            && p.CurrentItem is Tool && p.ActiveObject is null
            && !(p.friendshipData.TryGetValue(pinned.Name ?? "", out Friendship? f) && f?.TalkedToToday == true)
            && NpcOccupantsExact(p, pinnedLocation, pinnedRect, pinned);
        bool Expected(object? menu) => menu is DialogueBox box
            && (ReferenceEquals(box.characterDialogue?.speaker, pinned) || ReferenceEquals(Game1.currentSpeaker, pinned));
        BgActionEffectFacts Sample() =>
            SampleInteractEffect(() => pinnedLocation.characters.Contains(pinned), "") with { ExpectedReceiver = Expected(Game1.activeClickableMenu) };
        return new InteractKindCapture(target, new BgActionTarget(BgActionKind.Npc, null, null, "", StillPresent, Sample)
            { ExpectedReceiver = Expected }, null, false);
    }

    /// <summary>
    /// Occupant re-check for a pinned talk target (same rule as capture): no other farmer at the grab
    /// tile, exactly the pinned villager there, and no other character on the fallback tiles below /
    /// above (the press would otherwise talk to / gift someone else).
    /// </summary>
    static bool NpcOccupantsExact(Farmer player, GameLocation location, Rectangle rect, NPC pinned)
    {
        if (location.farmers.Any(f => f is not null && !ReferenceEquals(f, player) && f.GetBoundingBox().Intersects(rect)))
            return false;
        Rectangle below = new(rect.X, rect.Y + 64, 64, 64);
        Rectangle above = new(rect.X, rect.Y - 64, 64, 64);
        foreach (NPC c in location.characters)
        {
            if (c is null)
                continue;
            Rectangle box = c.GetBoundingBox();
            if (ReferenceEquals(c, pinned))
            {
                if (!box.Intersects(rect))
                    return false;
            }
            else if (box.Intersects(rect) || box.Intersects(below) || box.Intersects(above))
                return false;
        }
        return true;
    }

    /// <summary>The scene fact id scene.current_location publishes for this NPC (same registry).</summary>
    string NpcFactIdFor(GameLocation location, string locationRaw, NPC npc)
    {
        SceneSnapshotCapture capture = this.screens.Value.SceneCapture;
        if (capture.TryGetNpcFactId(locationRaw, npc, out string factId))
            return factId;
        return capture.CaptureNpc(location, locationRaw, npc).Id;
    }

    // ------------------------------------------------------------------
    // eat prompt
    // ------------------------------------------------------------------

    /// <summary>
    /// Runs first in the WS3a resolution while an Object is held: null when the held item is not
    /// an edible object (the normal resolution, incl. the M1 chest rule, continues), else the eat
    /// target or a miss. pressActionButton reaches the eat prompt only when nothing is checked at
    /// the grab tile, the tile below, the tile above and the own tile, and the item is neither
    /// usable nor placeable; every one of those is required here.
    /// </summary>
    InteractKindCapture? CaptureEatKind(Farmer player, Vector2 tile)
    {
        if (!this.NpcEatBedEnabled || player.ActiveObject is not SObject food || food is Furniture || food.Edibility <= 0)
            return null;
        GameLocation location = player.currentLocation;
        if (player.isRidingHorse() || player.mount is not null)
            return InteractKindCapture.Missed("mounted");
        if (Game1.eventUp || Game1.CurrentEvent is not null || location.currentEvent is not null || Game1.isFestival())
            return InteractKindCapture.Missed("event_or_festival");
        if (player.IsSitting())
            return InteractKindCapture.Missed("sitting");
        if (tile == player.Tile)
            return InteractKindCapture.Missed("grab_tile_is_own_tile");
        // Anything checkable around the grab tile wins over the eat prompt in pressActionButton:
        // leave it to the normal resolution (M1 chest rule, WS3a kinds / misses) unchanged.
        if (EatSiteMiss(player, location, tile) is not null)
            return null;
        if (HotbarEatMiss(player, food) is string itemMiss)
            return InteractKindCapture.Missed(itemMiss);
        string? miss = BgEatPrompt.Miss(new BgEatFacts(true, food.Edibility, food.questItem.Value,
            Game1.objectData.ContainsKey(food.ItemId), food.isPlaceable(), food is Furniture,
            !player.isEating && !player.canOnlyWalk && !player.FarmerSprite.PauseForSingleAnimation && !Game1.fadeToBlack
                && !Game1.dialogueUp && player.CanMove,
            player.hasBuff("25") && !food.HasContextTag("ginger_item"), null));
        if (miss is not null)
            return InteractKindCapture.Missed(miss);
        string qid = food.QualifiedItemId ?? "";
        int slot = player.CurrentToolIndex;
        string source = "inv:" + slot + ":" + SceneIdentity.Token(qid);
        string instance = this.worldTargetInstanceTokenTracker.Observe(food)!;
        string token = this.InteractToken(WorldInteractionKinds.Eat, instance, source, player, null);
        var target = new WorldInteractionTarget(token, source, (int)tile.X, (int)tile.Y, (int)player.Tile.X, (int)player.Tile.Y,
            player.FacingDirection, WorldInteractionKinds.Eat, null);
        if (!target.IsValid)
            return InteractKindCapture.Missed("target_invalid");
        SObject pinned = food;
        GameLocation pinnedLocation = location;
        Vector2 pinnedTile = tile;
        bool StillPresent() => Game1.player is Farmer p && ReferenceEquals(p.currentLocation, pinnedLocation)
            && p.GetGrabTile() == pinnedTile && ReferenceEquals(p.ActiveObject, pinned) && p.CurrentToolIndex == slot
            && !p.isEating && EatSiteMiss(p, pinnedLocation, pinnedTile) is null;
        static bool Expected(object? menu) => IsQuestionMenu(menu, BgEatPrompt.QuestionKey);
        BgActionEffectFacts Sample() => SampleInteractEffect(() => true, qid) with { ExpectedReceiver = Expected(Game1.activeClickableMenu) };
        return new InteractKindCapture(target, new BgActionTarget(BgActionKind.Eat, null, null, qid, StillPresent, Sample)
            { ExpectedReceiver = Expected }, null, false);
    }

    /// <summary>FINAL B7: the hotbar / held-food policy (cheap food; below the survival HP line also dearer food).</summary>
    static string? HotbarEatMiss(Farmer player, SObject o) =>
        BgEatPrompt.CandidateMiss(EatCandidateFactsOf(o), BgEatPrompt.Survival(player.health, player.maxHealth));

    /// <summary>Read-only candidate facts (price, category, tags, use action) of one object.</summary>
    static BgEatCandidateFacts EatCandidateFactsOf(SObject o) => new(o.Edibility, o.sellToStorePrice(), o.Category,
        o.questItem.Value, Game1.objectData.ContainsKey(o.ItemId), o.isPlaceable(), o is Furniture,
        BgEatPrompt.UsableItem(o.QualifiedItemId, o.Name, o.Category), o.GetContextTags());

    /// <summary>
    /// Anything tryToCheckAt could act on at the grab tile, below, above or the own tile
    /// (<paramref name="own"/>: a hypothetical stand tile for the D25 eat-site hint). A rug
    /// (furniture type 12: checkForAction -> clicked, nothing held on it) is floor.
    /// </summary>
    static string? EatSiteMiss(Farmer player, GameLocation location, Vector2 grab, Vector2? own = null)
    {
        foreach ((string name, Vector2 t) in new[] { ("grab", grab), ("below", grab + new Vector2(0, 1)),
                     ("above", grab + new Vector2(0, -1)), ("own", own ?? player.Tile) })
        {
            if (!location.isTileOnMap(t))
                return "eat_site_off_map:" + name;
            Rectangle r = new((int)t.X * 64, (int)t.Y * 64, 64, 64);
            if (location.farmers.Any(f => f is not null && !ReferenceEquals(f, player) && f.GetBoundingBox().Intersects(r)))
                return "eat_site_farmer:" + name;
            if (location.characters.Any(c => c is not null && c.GetBoundingBox().Intersects(r)))
                return "eat_site_character:" + name;
            if (location.animals.Values.Any(a => a is not null && a.GetBoundingBox().Intersects(r)))
                return "eat_site_animal:" + name;
            if (location.objects.ContainsKey(t))
                return "eat_site_object:" + name;
            if (location.furniture.Any(f => f is not null && f.furniture_type.Value != Furniture.rug && f.GetBoundingBox().Intersects(r)))
                return "eat_site_furniture:" + name;
            if (location.buildings.Any(b => b is not null && b.occupiesTile(t, applyTilePropertyRadius: true)))
                return "eat_site_building:" + name;
            if (location.resourceClumps.Any(c => c is not null && c.getBoundingBox().Intersects(r)))
                return "eat_site_clump:" + name;
            if (location.terrainFeatures.TryGetValue(t, out TerrainFeature? feature) && feature is not (Grass or Flooring))
                return "eat_site_terrain:" + name;
            if (location.largeTerrainFeatures.Any(f => f is not null && f.getBoundingBox().Intersects(r)))
                return "eat_site_large_terrain:" + name;
            // Buildings-layer tiles carry actions, tile-index actions, signs and walls.
            if (location.map?.GetLayer("Buildings")?.Tiles[(int)t.X, (int)t.Y] is not null)
                return "eat_site_map_tile:" + name;
        }
        return null;
    }

    /// <summary>FIXB26: backpack food candidates published beside the hotbar ones (bounded).</summary>
    const int EatBackpackCandidatesMax = 4;

    /// <summary>ui.context eat_candidates: edible, non-quest, non-placeable objects in the 12 hotbar slots.</summary>
    void EnrichEatCandidates(Dictionary<string, object> snapshot)
    {
        if (!this.NpcEatBedEnabled || !Context.IsWorldReady || Game1.player is not Farmer player)
            return;
        try
        {
            var candidates = new List<Dictionary<string, object>>();
            for (int slot = 0; slot < Math.Min(12, player.Items.Count); slot++)
            {
                if (player.Items[slot] is not SObject o || HotbarEatMiss(player, o) is not null)
                    continue;
                var candidate = new Dictionary<string, object>
                {
                    ["slot"] = slot, ["qualified_item_id"] = o.QualifiedItemId ?? "", ["edibility"] = o.Edibility,
                    ["stack"] = o.Stack, ["quality"] = o.Quality, ["price"] = o.sellToStorePrice(),
                };
                if (o.sellToStorePrice() > BgEatPrompt.MaxPrice)
                    candidate["survival"] = true; // FINAL B7: admitted only by the survival exception
                candidates.Add(candidate);
            }
            snapshot["eat_candidates"] = candidates;
            snapshot["eat_survival"] = BgEatPrompt.Survival(player.health, player.maxHealth);
            // FIXB26 (live batch 23: the Parsnips sat in the backpack, `eat_candidates` toolbar-only -> no_food_candidate):
            // the same filter over the backpack (slot >= 12), at most EatBackpackCandidatesMax; the runtime moves the
            // chosen one into the toolbar with its item_to_toolbar stage (as seeds / the rod).
            var backpack = new List<Dictionary<string, object>>();
            for (int slot = 12; slot < player.Items.Count && backpack.Count < EatBackpackCandidatesMax; slot++)
            {
                if (player.Items[slot] is not SObject b || BgEatPrompt.CandidateMiss(EatCandidateFactsOf(b)) is not null)
                    continue;
                backpack.Add(new Dictionary<string, object>
                {
                    ["slot"] = slot, ["qualified_item_id"] = b.QualifiedItemId ?? "", ["edibility"] = b.Edibility,
                    ["stack"] = b.Stack, ["quality"] = b.Quality, ["price"] = b.sellToStorePrice(),
                });
            }
            snapshot["eat_backpack_candidates"] = backpack;
            // FIXB18 D-B15a-2 (live V2B15aACC6b: the food was eaten with energy and health already full, gain 0): the
            // runtime's eat objective reads whether eating can gain anything. Review: one stable bool, never the
            // per-tick stamina / health values (they would churn the ui.context revision on every swing or hit).
            snapshot["eat_full"] = BgEatPrompt.VitalsFull(player.Stamina, player.MaxStamina, player.health, player.maxHealth);
            if (this.EatSiteHint(player) is { } hint)
                snapshot["eat_site"] = hint;
        }
        catch (Exception)
        {
            snapshot.Remove("eat_candidates");
            snapshot.Remove("eat_backpack_candidates");
            snapshot.Remove("eat_survival");
            snapshot.Remove("eat_site");
            snapshot.Remove("eat_full");
        }
    }

    /// <summary>
    /// D25 (live B4W4a/b): with a cheap food held but no eat target (grab tile on furniture or on
    /// ARTI's own tile), name where to stand and which empty floor tile to face: the own tile first,
    /// then a free cardinal neighbour; facings in <see cref="BgEatSite.Order"/>. Read-only; the eat
    /// capture stays authoritative once ARTI actually faces it.
    /// </summary>
    Dictionary<string, object>? EatSiteHint(Farmer player)
    {
        if (player.ActiveObject is not SObject food || HotbarEatMiss(player, food) is not null
            || player.currentLocation is not GameLocation location || player.isRidingHorse() || player.IsSitting())
            return null;
        if (this.CaptureEatKind(player, player.GetGrabTile()) is { Target: not null })
            return null; // already offered
        Vector2 own = player.Tile;
        var stands = new List<Vector2> { own };
        foreach (int d in new[] { 0, 1, 2, 3 })
        {
            (int dx, int dy) = BgEatSite.Vector(d);
            Vector2 n = own + new Vector2(dx, dy);
            if (location.isTileOnMap(n) && location.isTilePassable(new xTile.Dimensions.Location((int)n.X, (int)n.Y), Game1.viewport)
                && EatSiteMiss(player, location, n, n) is null)
                stands.Add(n);
        }
        foreach (Vector2 stand in stands)
        {
            foreach (int facing in BgEatSite.Order(player.FacingDirection))
            {
                (int dx, int dy) = BgEatSite.Vector(facing);
                Vector2 face = stand + new Vector2(dx, dy);
                if (location.isTileOnMap(face) && EatSiteMiss(player, location, face, stand) is null)
                    return new Dictionary<string, object>
                    {
                        ["stand_x"] = (int)stand.X, ["stand_y"] = (int)stand.Y, ["face_x"] = (int)face.X, ["face_y"] = (int)face.Y,
                        ["facing"] = facing,
                    };
            }
        }
        return null;
    }

    // ------------------------------------------------------------------
    // D27: fresh NPC facts on demand (snapshot-on-demand, never a position poll)
    // ------------------------------------------------------------------

    long lastNpcSceneRefreshMs;
    long lastNpcNearLogMs;

    /// <summary>
    /// D27 (live B4W3a): NPC scene facts are captured at snapshots / list changes only, so a
    /// villager who walked since the last snapshot sits elsewhere than his fact. After a
    /// background run ends, or when a talk capture misses with a villager next to the grab
    /// tile, request ONE scene snapshot (bounded to one per 3 s) so the runtime re-approaches
    /// the villager's current tile (TilePoint = bounding-box centre, the tile checkAction's box
    /// test always hits).
    /// </summary>
    void MaybeRefreshSceneForNpc(string reason, int radius = 2)
    {
        if (!this.NpcEatBedEnabled || !Context.IsWorldReady || Game1.player is not Farmer player
            || player.currentLocation is not GameLocation location)
            return;
        long now = Environment.TickCount64;
        if (now - this.lastNpcSceneRefreshMs < 3000)
            return;
        Point at = player.TilePoint;
        NPC? near = location.characters.FirstOrDefault(c => c is not null && c.IsVillager
            && Math.Max(Math.Abs(c.TilePoint.X - at.X), Math.Abs(c.TilePoint.Y - at.Y)) <= radius);
        if (near is null)
            return;
        this.lastNpcSceneRefreshMs = now;
        this.screens.Value.Scene.RequestSnapshot();
        this.Monitor.Log($"scene refresh for npc ({reason}): {near.Name} tile={near.TilePoint} box={near.GetBoundingBox()}", LogLevel.Debug);
    }

    // ------------------------------------------------------------------
    // move_through kind "bed"
    // ------------------------------------------------------------------

    /// <summary>
    /// The owned bed's sleep tile one or two tiles sideways along its sleep row (the only side
    /// the bed lets the farmer enter), with GameLocation's Sleep touch preconditions true now.
    /// Called by the WS3a move_through capture when no warp neighbours the actor.
    /// </summary>
    (MoveThroughTarget? Target, BgThroughExpectation? Expectation) CaptureBedThroughTarget(Farmer player, GameLocation location)
    {
        if (!this.NpcEatBedEnabled || location is not FarmHouse home || home.owner is not Farmer owner
            || owner.UniqueMultiplayerID != player.UniqueMultiplayerID
            || Game1.newDay || !Game1.shouldTimePass() || !player.hasMoved || player.passedOut || Game1.timeOfDay >= 2600)
            return (null, null);
        int ax = (int)player.Tile.X, ay = (int)player.Tile.Y;
        List<(BedFurniture Bed, int SleepX, int SleepY)> beds = location.furniture.OfType<BedFurniture>()
            .Where(b => b.bedType != BedFurniture.BedType.Child)
            .Select(b => (b, (int)b.TileLocation.X + b.bedTileOffset, (int)b.TileLocation.Y + 1))
            .Where(c => c.Item3 == ay && Math.Abs(c.Item2 - ax) is 1 or 2)
            .ToList();
        if (beds.Count != 1)
            return (null, null);
        (BedFurniture bed, int sx, int sy) = beds[0];
        int step = Math.Sign(sx - ax);
        string direction = step > 0 ? "right" : "left";
        if (Math.Abs(sx - ax) == 2)
        {
            // The middle tile must be the bed's own sleep row (passable by design), nothing else.
            int mx = ax + step;
            int bx = (int)bed.TileLocation.X;
            if (mx < bx || mx >= bx + bed.getTilesWide())
                return (null, null);
        }
        if (location.lastTouchActionLocation == new Vector2(sx, sy))
            return (null, null);
        string locationRaw = location.NameOrUniqueName ?? location.Name ?? "_";
        string source = SceneBedFact.FactId(locationRaw, (int)bed.TileLocation.X, (int)bed.TileLocation.Y);
        CommandInboxIdentity identity = this.CurrentCommandIdentity();
        string token = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new object?[] {
                MoveThroughTarget.KindBed, source, identity, ax, ay, player.FacingDirection, sx, sy })))).ToLowerInvariant();
        var target = new MoveThroughTarget(direction, 1, MoveThroughTarget.KindBed, source, token, sx, sy, ax, ay, null);
        if (!target.IsValid)
            return (null, null);
        static BgThroughProbe Probe()
        {
            Farmer? p = Game1.player;
            return new BgThroughProbe(
                Game1.isWarping,
                null,
                CurrentLocationId(p),
                Game1.isWarping || Game1.fadeToBlack || Game1.globalFade || Game1.locationRequest is not null
                    || Game1.currentLocation is null || p?.currentLocation is null,
                Context.IsWorldReady && Context.IsPlayerFree,
                Game1.activeClickableMenu is not null || Game1.dialogueUp,
                EventUp: Game1.eventUp || Game1.CurrentEvent is not null,
                SleepQuestion: IsQuestionMenu(Game1.activeClickableMenu, BgBedThrough.QuestionKey));
        }
        return (target, new BgThroughExpectation(MoveThroughTarget.KindBed, null, Probe));
    }

    // ------------------------------------------------------------------
    // verdict writers
    // ------------------------------------------------------------------

    /// <summary>
    /// npc / eat hold verdict: success only with the pinned expected DialogueBox still the active
    /// receiver (by reference) at context generation pin+1, like the chest's ContainerOpened.
    /// </summary>
    private MenuStepCommandResult CompleteReceiverKindHold(BackgroundMoveTerminal terminal, BackgroundHoldResult hold,
        BgActionTarget pinned, CommandInboxIdentity identity, long now)
    {
        BgHoldOutcome o = hold.Outcome;
        string outcome = o.Outcome;
        bool opened = o.Opened;
        string? receiver = null;
        long? generation = null;
        if (opened)
        {
            if (this.config.Enabled && Context.IsWorldReady)
                this.AdvanceUiContextNative(this.screens.Value);
            IClickableMenu? menu = Context.IsWorldReady ? Game1.activeClickableMenu : null;
            CommandInboxIdentity after = this.CurrentCommandIdentity();
            bool expected = pinned.ExpectedReceiver?.Invoke(menu) == true;
            receiver = expected && ReferenceEquals(menu, hold.Menu) ? this.ReceiverTokenOf(CurrentInteractiveMenu(menu)) : null;
            if (receiver is null || after.ContextGeneration != terminal.Request.ContextGeneration + 1)
            {
                outcome = BgHoldCompletion.InteractionUncertain;
                opened = false;
                receiver = null;
            }
            else
            {
                generation = after.ContextGeneration;
                identity = after;
            }
        }
        if (opened && pinned.Kind == BgActionKind.ExtraReceiver)
            this.PinExtrasQuestion(pinned.ExtraKind); // WS-G6: only this receiver's question key becomes answerable
        this.Monitor.Log($"background WS3b {pinned.Kind} verdict request_id={terminal.Request.RequestId} outcome={outcome} "
            + $"receiver={receiver ?? "null"} generation={generation?.ToString() ?? "null"}",
            opened ? LogLevel.Info : LogLevel.Warn);
        return this.commandInbox.CompleteWorldInteraction(new WorldInteractionVerdict(outcome, opened, o.CleanupConfirmed,
            o.PressCount, receiver, generation, null, !opened ? null : pinned.Kind == BgActionKind.Eat ? pinned.ItemId
                : pinned.ExtraKind is WorldExtras.NpcGift or WorldExtras.QuestDelivery ? this.backgroundMove?.ExtraEffectDetail(hold.Facts?.Effect)
                : pinned.Kind == BgActionKind.ExtraReceiver ? pinned.ExtraKind : null), identity, now);
    }

    /// <summary>
    /// bed move_through verdict: TransitionObserved only for TOUCH_QUESTION:Sleep with the Sleep
    /// question still the active receiver at context generation pin+1 (receiver token for ui_key).
    /// </summary>
    private MenuStepCommandResult CompleteBedThroughTerminal(BackgroundMoveTerminal terminal, CommandInboxIdentity identity, long now)
    {
        bool touched = terminal.Kind == BackgroundTerminalKind.Completed
            && string.Equals(terminal.Label, BgBedThrough.TouchQuestionSleep, StringComparison.Ordinal);
        bool uncertain = terminal.Kind == BackgroundTerminalKind.Uncertain;
        string? receiver = null;
        long? generation = null;
        if (touched)
        {
            if (this.config.Enabled && Context.IsWorldReady)
                this.AdvanceUiContextNative(this.screens.Value);
            IClickableMenu? menu = Context.IsWorldReady ? Game1.activeClickableMenu : null;
            CommandInboxIdentity after = this.CurrentCommandIdentity();
            receiver = IsQuestionMenu(menu, BgBedThrough.QuestionKey) ? this.ReceiverTokenOf(CurrentInteractiveMenu(menu)) : null;
            if (receiver is null || after.ContextGeneration != terminal.Request.ContextGeneration + 1)
            {
                touched = false;
                uncertain = true;
                receiver = null;
            }
            else
            {
                generation = after.ContextGeneration;
                identity = after;
            }
        }
        this.Monitor.Log($"background WS3b bed verdict request_id={terminal.Request.RequestId} label={terminal.Label} "
            + $"touched={touched} receiver={receiver ?? "null"}", touched ? LogLevel.Info : LogLevel.Warn);
        return this.commandInbox.CompleteMoveThrough(terminal.Label, touched, uncertain, terminal.ConsumedH,
            !uncertain, null, identity, now, receiver, generation);
    }
}
