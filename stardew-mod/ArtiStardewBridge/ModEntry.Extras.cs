using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Buildings;
using StardewValley.Characters;
using StardewValley.GameData.Crops;
using StardewValley.Locations;
using StardewValley.Menus;
using StardewValley.Objects;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

// Primitives v2 WS-G6 (WS-G6 brief section 2): the game side of the world extras world_interact kinds on the
// pad path. Read-only capture mirroring Game1.pressActionButton / pressUseToolButton and GameLocation.checkAction
// order (decompiled 1.6.15, cited in Protocol/WorldExtras.cs), the explicit per-kind guard facts, the pinned
// target closures (pre-press re-capture, per-kind effect readback) and the ui.context extras_targets list for the
// runtime's approach catalog. No new input path: every press is the existing BgHoldKind.Action retained-press hold
// (pad A, or pad X for the X kinds), every menu step stays G1's ui_nav / ui_press or v1's ui_key.
// Flag WorldExtrasEnabled (default OFF) + the pad path + WorldInteractKindsEnabled.
// Evidence ceiling: UNIT_TESTED (pure layer) + LOCAL_BUILT. HYPOTHESIS until the G6 live witnesses.
public sealed partial class ModEntry
{
    /// <summary>The extras capture runs only with its flag, the WS3a kinds flag and the WS-G0 pad path.</summary>
    bool ExtrasEnabled => this.config.WorldExtrasEnabled && this.config.WorldInteractKindsEnabled && this.PadPathConfigured;

    /// <summary>
    /// Fix D-B2-1: the shop counter (G2's opener for sell / buy) is captured with the economy flag too, on the same pad
    /// path; with WorldExtrasEnabled off nothing but the counter is ever captured (EconomyMenusEnabled off = unchanged).
    /// </summary>
    bool ShopCounterEnabled => this.config.EconomyMenusEnabled && this.config.WorldInteractKindsEnabled && this.PadPathConfigured;

    const int ExtrasTargetRadius = 12;
    /// <summary>FIXB15 review (D-B13-4): pets / horses / farm animals move; they are published over the nav-grid window.</summary>
    const int ExtrasCharacterRadius = 24;
    /// <summary>FIXB17 D-B5-0: the level's up-ladder is published anywhere in the nav window (the exit leg routes to it).</summary>
    const int ExtrasLadderUpRadius = WorldExtras.MineLadderOfferRadius;
    const int ExtrasTargetsMax = 16;
    /// <summary>FIXB7 D-B5b-1: placed store chests published within this radius (ui.context "store_chests").</summary>
    const int StoreChestRadius = 24;
    const int StoreChestsMax = 4;

    // ------------------------------------------------------------------ shared facts

    static string ExtrasHeldClass(Item? item) => item is MeleeWeapon ? PadHeld.MeleeWeapon : PadHeldOf(item);

    static string ExtrasToolType(Item? item) => item switch
    {
        MilkPail => "MilkPail",
        Shears => "Shears",
        Pan => "Pan",
        Wand => "Wand",
        Tool t => t.GetType().Name,
        _ => "",
    };

    ExtrasGuardFacts ExtrasBaseFacts(Farmer player, Vector2 grab, GameLocation location)
    {
        Vector2? tool = BackgroundToolTileProbe.ToolTile(player) is { } t ? new Vector2(t.X, t.Y) : null;
        return new ExtrasGuardFacts(
            Held: ExtrasHeldClass(player.CurrentItem),
            HeldToolType: ExtrasToolType(player.CurrentItem),
            EventUp: Game1.eventUp || Game1.CurrentEvent is not null || location.currentEvent is not null || Game1.isFestival(),
            Mounted: player.isRidingHorse() || player.mount is not null,
            Sitting: player.IsSitting(),
            GrabIsOwnTile: grab == player.Tile,
            TimeOfDay: Game1.timeOfDay,
            ToolTileIsGrabTile: tool is { } tt && tt == grab,
            Money: player.Money,
            Health: player.health,
            MaxHealth: player.maxHealth,
            Stamina: player.Stamina,
            AllowSkullCavern: this.config.ExtrasAllowSkullCavern);
    }

    /// <summary>FIXB13 D-B11-1: the tile an X kind resolves at (the tool tile; the grab tile when unreadable).</summary>
    /// <summary>FIXB14 D-B12-1: the tile the pad press of a world_interact kind acts on (X kinds: tool tile).</summary>
    /// <summary>FIXB23 D-B20-3: a character A kind acts on the vanilla fallback tile when one applies (ExtrasCharacterFallback).</summary>
    static Vector2 PadPressTile(Farmer player, string? kind)
    {
        Vector2 grab = player.GetGrabTile();
        var tool = BackgroundToolTileProbe.ToolTile(player);
        if (GenericWorld.IsKind(kind))
            return kind == GenericWorld.Use && tool is { } genericTool ? new Vector2(genericTool.X, genericTool.Y) : grab;
        (int X, int Y)? fallback = WorldExtras.IsCharacterKind(kind) && ExtrasCharacterFallback(player) is { } f
            ? ((int)f.X, (int)f.Y) : null;
        var (x, y) = WorldExtras.PressTile(kind, ((int)grab.X, (int)grab.Y), tool is { } t ? (t.X, t.Y) : null, fallback);
        return new Vector2(x, y);
    }

    /// <summary>FIXB23 D-B20-3: the tile a character A kind is resolved at (the fallback tile when one applies, else the grab
    /// tile); every other kind resolves at the grab tile as before.</summary>
    static Vector2 ExtrasResolveTile(Farmer player, string? kind) =>
        WorldExtras.IsCharacterKind(kind) && ExtrasCharacterFallback(player) is { } f ? f : player.GetGrabTile();

    /// <summary>FIXB23 D-B20-3: vanilla pressActionButton's fallback tile for this pose (WorldExtras.CharacterFallbackTile:
    /// grab = own tile, facing up / down, the own tile takes nothing), or null. Read fault = null (no fallback).</summary>
    static Vector2? ExtrasCharacterFallback(Farmer player)
    {
        try
        {
            if (player.currentLocation is not GameLocation location)
                return null;
            Vector2 grab = player.GetGrabTile(), own = player.Tile;
            (int X, int Y) g = ((int)grab.X, (int)grab.Y), o = ((int)own.X, (int)own.Y);
            if (!WorldExtras.CharacterFallbackPose(g, o, player.FacingDirection))
                return null; // no scan: the pose cannot use the fallback
            // FIXB23 review: the own-tile scan runs on a changed pose key, else at most every RescanTicks ticks.
            bool handFree = player.ActiveObject is null && player.mount is null;
            if (characterFallbackThrottle.Due(g, o, player.FacingDirection, location.NameOrUniqueName, handFree, Game1.ticks))
            {
                bool inert = ExtrasOwnTileInert(player, location, own);
                characterFallbackCached = WorldExtras.CharacterFallbackTile(g, o, player.FacingDirection, inert)
                    is { } f ? new Vector2(f.X, f.Y) : null;
            }
            return characterFallbackCached;
        }
        catch
        {
            return null;
        }
    }

    static readonly CharacterFallbackThrottle characterFallbackThrottle = new();
    static Vector2? characterFallbackCached;

    /// <summary>FIXB23 D-B20-3: pressActionButton reaches its fallback probes only when tryToCheckAt(own tile) takes nothing
    /// (GameLocation.checkAction: characters, farmers, animals, buildings, objects, clumps, terrain, furniture, Action tiles),
    /// nothing is held (ActiveObject null: no placement / use), no character is at the tile (flag3), ARTI is not mounted,
    /// sitting or on a bridge (tryToCheckAt false). Conservative: anything there = not inert (no fallback).</summary>
    static bool ExtrasOwnTileInert(Farmer player, GameLocation location, Vector2 own)
    {
        if (player.ActiveObject is not null || player.isRidingHorse() || player.mount is not null || player.IsSitting()
            || player.onBridge.Value || Game1.eventUp || location.currentEvent is not null)
            return false;
        Rectangle r = TileRect(own);
        if (location.characters.Any(c => c is not null && c.GetBoundingBox().Intersects(r))
            || location.farmers.Any(f => f is not null && !ReferenceEquals(f, player) && f.GetBoundingBox().Intersects(r))
            || ExtrasAnimalAt(location, r, null) || location.objects.ContainsKey(own) || ExtrasFurnitureAt(location, r)
            || location.resourceClumps.Any(c => c is not null && c.getBoundingBox().Intersects(r))
            || location.largeTerrainFeatures.Any(f => f is not null && f.getBoundingBox().Intersects(r))
            || BuildingTakesGrab(location, (int)own.X, (int)own.Y))
            return false;
        if (location.terrainFeatures.TryGetValue(own, out TerrainFeature? feature)
            && feature is not (Grass or Flooring) && !(feature is HoeDirt { crop: null }))
            return false;
        return location.doesTileHaveProperty((int)own.X, (int)own.Y, "Action", "Buildings") is null;
    }

    /// <summary>FIXB23 D-B20-3: the character target at the vanilla fallback tile (or its refusal), else null. Only a character
    /// A kind is taken there (the probe would act on anything, but the stands / the runtime ask only for these); any other
    /// capture at that tile is dropped and the previous refusal target restored.</summary>
    InteractKindCapture? ExtrasCharacterCapture(Farmer player)
    {
        if (!this.ExtrasEnabled || ExtrasCharacterFallback(player) is not { } tile)
            return null;
        var saved = this.extrasMissTarget;
        this.extrasMissTarget = null;
        InteractKindCapture? alt = this.CaptureExtrasKind(player, tile);
        if (alt is { Target: { } target } && WorldExtras.IsCharacterKind(target.Kind))
            return alt;
        if (alt is { Target: null, Miss: not null } && this.extrasMissTarget is { } refused && WorldExtras.IsCharacterKind(refused.Kind))
            return alt;
        this.extrasMissTarget = saved;
        return null;
    }

    static Vector2 ExtrasXTile(Farmer player, Vector2 grab)
    {
        var tool = BackgroundToolTileProbe.ToolTile(player);
        var (x, y) = WorldExtras.XResolutionTile(((int)grab.X, (int)grab.Y), tool is { } t ? (t.X, t.Y) : null);
        return new Vector2(x, y);
    }

    static Rectangle TileRect(Vector2 tile) => new((int)tile.X * 64, (int)tile.Y * 64, 64, 64);

    /// <summary>Another farmer or any non-monster character at the tile, except the pinned target itself.</summary>
    static bool ExtrasOccupant(Farmer player, GameLocation location, Rectangle rect, object? except)
    {
        if (location.farmers.Any(f => f is not null && !ReferenceEquals(f, player) && f.GetBoundingBox().Intersects(rect)))
            return true;
        return location.characters.Any(c => c is not null && !ReferenceEquals(c, except) && !c.IsMonster
            && c.GetBoundingBox().Intersects(rect));
    }

    static bool ExtrasAnimalAt(GameLocation location, Rectangle rect, object? except) =>
        location.animals.Values.Any(a => a is not null && !ReferenceEquals(a, except) && a.GetBoundingBox().Intersects(rect));

    static bool ExtrasFurnitureAt(GameLocation location, Rectangle rect) =>
        location.furniture.Any(f => f is not null && f.GetBoundingBox().Intersects(rect));

    /// <summary>Object.checkForAction's boxed-in rule (Object.cs:3936-3943): four non-passable objects around the farmer.</summary>
    static bool ExtrasBoxedIn(Farmer player, GameLocation location)
    {
        Point t = player.TilePoint;
        Point[] around = { new(t.X, t.Y - 1), new(t.X, t.Y + 1), new(t.X + 1, t.Y), new(t.X - 1, t.Y) };
        return around.All(p => location.getObjectAtTile(p.X, p.Y) is SObject o && !o.isPassable());
    }

    static int ExtrasHayWithdrawal(GameLocation location) => WorldExtras.HayWithdrawalCount(
        location.GetRootLocation().piecesOfHay.Value,
        location is AnimalHouse house ? house.animalsThatLiveHere.Count : null,
        location is AnimalHouse h ? h.animalLimit.Value : 0,
        location is AnimalHouse a ? a.numberOfObjectsWithName("Hay") : 0);

    /// <summary>XP gain of a skill crossing level 5 / 10 without the owner policy (null = allowed).</summary>
    string? ExtrasProfession(Farmer player, int skill, int gain) =>
        skill is < 0 or > 5 ? null
            : WorldExtras.ProfessionRefusal(player.experiencePoints[skill], gain, this.config.ExtrasAllowProfessionMenu);

    string? ExtrasMachineProfession(Farmer player, string? experienceGain)
    {
        if (string.IsNullOrWhiteSpace(experienceGain))
            return null;
        string[] parts = experienceGain.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        for (int i = 0; i + 1 < parts.Length; i += 2)
        {
            int skill = Farmer.getSkillNumberFromName(parts[i]);
            if (this.ExtrasProfession(player, skill, WorldExtras.MachineXpFor(experienceGain, parts[i])) is { } refused)
                return refused;
        }
        return null;
    }

    /// <summary>The single grab tile has nothing pressActionButton would check before the dismount (mounted A).</summary>
    static bool ExtrasDismountTileClear(Farmer player, GameLocation location, Vector2 tile)
    {
        if (!location.isTileOnMap(tile))
            return false;
        Rectangle r = TileRect(tile);
        if (ExtrasOccupant(player, location, r, player.mount) || ExtrasAnimalAt(location, r, null) || location.objects.ContainsKey(tile)
            || ExtrasFurnitureAt(location, r) || location.resourceClumps.Any(c => c is not null && c.getBoundingBox().Intersects(r))
            || location.largeTerrainFeatures.Any(f => f is not null && f.getBoundingBox().Intersects(r))
            || location.buildings.Any(b => b is not null && b.occupiesTile(tile, applyTilePropertyRadius: true)))
            return false;
        if (location.terrainFeatures.TryGetValue(tile, out TerrainFeature? feature)
            && feature is not (Grass or Flooring) && !(feature is HoeDirt { crop: null }))
            return false;
        return location.map?.GetLayer("Buildings")?.Tiles[(int)tile.X, (int)tile.Y] is null;
    }

    // ------------------------------------------------------------------ resolution (mirrors the press order)

    /// <summary>
    /// WS-G6 resolution at the grab tile. null = not an extras target (the v1 resolution continues unchanged);
    /// otherwise the captured target or the first failing guard. Order: mounted (dismount only), the X kinds by the
    /// held item (pail / shears -> animal at the tool tile; object -> machine drop-in at the tool tile, else
    /// placement), then the A kinds in GameLocation.checkAction order.
    /// </summary>
    InteractKindCapture? CaptureExtrasKind(Farmer player, Vector2 grab)
    {
        if (!(this.ExtrasEnabled || this.ShopCounterEnabled) || player.currentLocation is not GameLocation location)
            return null;
        try
        {
            ExtrasGuardFacts facts = this.ExtrasBaseFacts(player, grab, location);
            if (this.ShopCounterEnabled && this.CaptureShippingBin(player, location, grab, facts) is { } bin)
                return bin; // FIXB7 D-B5b-3: the shipping bin (economy flag), before the buildings are left to v1
            if (!this.ExtrasEnabled)
                return this.CaptureShopCounter(player, location, grab, facts); // economy flag only: the counter alone
            if (facts.Mounted)
                return this.CaptureDismount(player, location, grab, facts);
            // FIXB13 D-B11-1 (live V2B11PLANT-a/b: ARTI at (53,19) facing down at the top of its tile, GetGrabTile (+5 px)
            // = its own tile -> `grab_tile_is_own_tile`, the place target never published): the X kinds act where the
            // X press acts, the tool tile (Character.GetToolLocation, +48 px: always the faced neighbour).
            Vector2 xTile = ExtrasXTile(player, grab);
            ExtrasGuardFacts xFacts = xTile == grab ? facts : this.ExtrasBaseFacts(player, xTile, location);
            if (player.CurrentItem is MilkPail or Shears)
                return this.CaptureAnimalTool(player, location, xTile, xFacts);
            if (this.CaptureHayPlace(player, location, grab, facts) is { } hayPlace)
                return hayPlace;
            if (player.ActiveObject is SObject held && held is not Furniture)
                return this.CaptureQuestDelivery(player, location, grab, held, facts) // W1 gap B4 S28 (ModEntry.QuestCapture.cs)
                    ?? this.CaptureGiftKind(player, location, grab, held, facts)
                    ?? this.CaptureCrabPotX(player, location, xTile, held, xFacts) // W1 gap B3 S24 (ModEntry.B3Capture.cs)
                    ?? this.CaptureMachineInsert(player, location, xTile, held, xFacts) ?? this.CapturePlace(player, location, xTile, held, xFacts);
            return this.CaptureExtrasA(player, location, grab, facts)
                ?? this.CaptureBlacksmithOwnTileMiss(player, location, grab, facts);
        }
        catch (Exception ex)
        {
            this.Monitor.LogOnce($"world extras capture fault: {ex.GetType().Name}: {ex.Message}", LogLevel.Warn);
            return InteractKindCapture.Missed("extras_capture_fault");
        }
    }

    InteractKindCapture? CaptureDismount(Farmer player, GameLocation location, Vector2 grab, ExtrasGuardFacts facts)
    {
        if (player.mount is not Horse horse)
            return InteractKindCapture.Missed(WorldExtras.GMounted);
        facts = facts with { DismountTileClear = ExtrasDismountTileClear(player, location, grab) };
        return this.ExtrasCapture(WorldExtras.HorseDismount, player, location, grab, facts, horse, horse.Name, null,
            () => new BgExtraFacts(WorldExtras.HorseDismount, Game1.player?.mount is null, 0));
    }

    InteractKindCapture? CaptureMachineInsert(Farmer player, GameLocation location, Vector2 grab, SObject held, ExtrasGuardFacts facts)
    {
        if (BackgroundToolTileProbe.ToolTile(player) is not { } tool
            || !location.objects.TryGetValue(new Vector2(tool.X, tool.Y), out SObject? machine) || machine is null
            || !machine.bigCraftable.Value || machine is Chest || machine.Type is not ("Crafting" or "interactive"))
            return null;
        Rectangle rect = TileRect(grab);
        bool busy = machine.heldObject.Value is not null || machine.readyForHarvest.Value || machine.MinutesUntilReady > 0;
        bool accepted = false;
        if (!busy && WorldExtras.InsertMachines.Contains(machine.QualifiedItemId))
        {
            try { accepted = machine.performObjectDropInAction(held, probe: true, player); }
            catch { accepted = false; }
        }
        facts = facts with
        {
            OccupantAtTile = ExtrasOccupant(player, location, rect, null) || ExtrasAnimalAt(location, rect, null),
            FurnitureAtTile = ExtrasFurnitureAt(location, rect),
            MachineAllowlisted = WorldExtras.InsertMachines.Contains(machine.QualifiedItemId),
            MachineBusy = busy,
            StowHazard = Game1.options?.allowStowing == true && !held.bigCraftable.Value,
            InputAccepted = accepted,
            InputPolicyDetail = WorldExtrasPolicy.InsertPolicyRefusal(EconItemOf(held, 1f), this.config.EconomyItemPolicy,
                this.config.ExtrasMachineInputAllowlist, this.config.ExtrasMachineInputPriceCap),
        };
        string inQid = held.QualifiedItemId ?? "";
        SObject pinned = machine;
        GameLocation pinnedLocation = location;
        Vector2 machineTile = new(tool.X, tool.Y);
        return this.ExtrasCapture(WorldExtras.MachineInsert, player, location, grab, facts, machine, machine.QualifiedItemId, null,
            () => new BgExtraFacts(WorldExtras.MachineInsert,
                pinnedLocation.objects.TryGetValue(machineTile, out SObject? now) && ReferenceEquals(now, pinned)
                    && (pinned.heldObject.Value is not null || pinned.MinutesUntilReady > 0),
                Game1.player is Farmer p ? InventoryCount(p, inQid) : 0, 0, inQid));
    }

    InteractKindCapture? CapturePlace(Farmer player, GameLocation location, Vector2 grab, SObject held, ExtrasGuardFacts facts)
    {
        bool allowlisted = WorldExtras.PlaceableAllowlisted(held.QualifiedItemId, held.Category, held.isSapling());
        if (!held.isPlaceable() || !allowlisted)
            return null; // not a G6 placeable: the v1 resolution (eat prompt / misses) continues
        Rectangle rect = TileRect(grab);
        bool valid;
        try { valid = Utility.playerCanPlaceItemHere(location, held, (int)grab.X * 64, (int)grab.Y * 64, player); }
        catch { valid = false; }
        bool seed = held.Category == -74;
        bool harvestable = location.terrainFeatures.TryGetValue(grab, out TerrainFeature? feature) && feature is HoeDirt dirt
            && dirt.readyForHarvest();
        facts = facts with
        {
            PlaceableAllowlisted = allowlisted,
            OccupantAtTile = ExtrasOccupant(player, location, rect, null) || ExtrasAnimalAt(location, rect, null),
            ObjectAtTile = location.objects.ContainsKey(grab),
            FurnitureAtTile = ExtrasFurnitureAt(location, rect),
            HarvestableAtTile = harvestable,
            // Review MINOR 5 (documented limit): with options.allowStowing on, an allowlisted SPRINKLER ((O)599/621/645, not a
            // big craftable) is ALWAYS refused here as "stowing_would_apply"; seeds with a valid tile and chests ((BC)) are not.
            // pressUseToolButton stows a non-big-craftable object BEFORE placing it unless it is a seed / fertilizer
            // with a valid placement (CanPlayerStowItem, G1:11734-11757).
            StowHazard = Game1.options?.allowStowing == true && !held.bigCraftable.Value && !(seed && valid),
            // The cursor-hidden placement walks from the tool tile toward the player: the faced tile itself must be valid.
            PlacementValid = valid && (!seed || feature is HoeDirt { crop: null }),
        };
        string qid = held.QualifiedItemId ?? "";
        GameLocation pinnedLocation = location;
        Vector2 pinnedTile = grab;
        return this.ExtrasCapture(WorldExtras.PlaceItem, player, location, grab, facts, location, qid, null,
            () => new BgExtraFacts(WorldExtras.PlaceItem,
                seed ? pinnedLocation.terrainFeatures.TryGetValue(pinnedTile, out TerrainFeature? t) && t is HoeDirt { crop: not null }
                     : pinnedLocation.objects.ContainsKey(pinnedTile),
                Game1.player is Farmer p ? InventoryCount(p, qid) : 0, 0, qid));
    }

    /// <summary>FIXB19 D-B16-1 / FIXB21 D-B18-4: the press admission's building step at a tile, the vanilla rule
    /// (WorldInteractionBuildingRule.TakesPress, Building.doAction); the stands use the same check for the target tile.</summary>
    static bool BuildingTakesGrab(GameLocation location, int gx, int gy) => BuildingGrabMiss(location, gx, gy) is not null;

    /// <summary>FIXB21 review: the building step's miss at a tile (null = the press reaches the characters / objects).</summary>
    static string? BuildingGrabMiss(GameLocation location, int gx, int gy)
    {
        var grab = new Vector2(gx, gy);
        foreach (Building b in location.buildings)
        {
            if (b is null)
                continue;
            if (b.daysOfConstructionLeft.Value > 0 && b.occupiesTile(grab))
                return WorldInteractionBuildingRule.UnderConstructionMiss;
            bool door = b.humanDoor.X + b.tileX.Value == gx && b.humanDoor.Y + b.tileY.Value == gy && b.humanDoor.X >= 0
                && b.GetIndoors() is not null;
            bool animalDoor = false, action = false;
            try
            {
                var data = b.GetData();
                if (data is not null)
                {
                    Rectangle r = b.getRectForAnimalDoor(data);
                    animalDoor = r != Rectangle.Empty && new Rectangle(r.X / 64, r.Y / 64, r.Width / 64, r.Height / 64).Contains(gx, gy);
                    action = b.occupiesTile(grab, applyTilePropertyRadius: true) && !b.isTilePassable(grab)
                        && data.GetActionAtTile(gx - b.tileX.Value, gy - b.tileY.Value) is not null;
                }
            }
            catch
            {
                action = b.occupiesTile(grab);  // unreadable data: conservative
            }
            bool subclass = WorldInteractionBuildingRule.SubclassTakes(b.GetType().Name, b.occupiesTile(grab), b.tileX.Value,
                b.tileY.Value, gx, gy);
            if (WorldInteractionBuildingRule.TakesPress(door, animalDoor, action, subclass))
                return WorldExtras.StandBuildingMiss;
        }
        return null;
    }

    InteractKindCapture? CaptureExtrasA(Farmer player, GameLocation location, Vector2 grab, ExtrasGuardFacts facts)
    {
        Rectangle rect = TileRect(grab);
        int gx = (int)grab.X, gy = (int)grab.Y;

        // 1. Location overrides that run before GameLocation.checkAction (MineShaft.cs:3053, BusStop.cs:70).
        if (location is MineShaft shaft)
        {
            int index = shaft.getTileIndexAt(gx, gy, "Buildings", "mine");
            facts = facts with { OccupantAtTile = ExtrasOccupant(player, location, rect, null) };
            switch (index)
            {
                case 173:
                {
                    string expected = SceneIdentity.Token(MineShaft.GetLevelName(shaft.mineLevel + 1));
                    return this.ExtrasCapture(WorldExtras.LadderDown, player, location, grab,
                        facts with { TargetMineLevel = shaft.mineLevel + 1 }, shaft, null, expected, null);
                }
                case 174:
                    // A shaft drops 3-15 levels (MineShaft.cs:3012-3022): the shallowest landing still counts as Skull Cavern.
                    return this.ExtrasCapture(WorldExtras.Shaft, player, location, grab, facts with { TargetMineLevel = shaft.mineLevel + 3 },
                        shaft, null, null,
                        () => new BgExtraFacts(WorldExtras.Shaft, false, 0), menu => IsQuestionMenu(menu, "Shaft"));
                case 115:
                    // FIXB17 D-B5-0: the up-ladder -> question ExitMine (Leave / Do); the runtime answers Leave.
                    return this.ExtrasCapture(WorldExtras.LadderUp, player, location, grab, facts, shaft, null, null,
                        () => new BgExtraFacts(WorldExtras.LadderUp, false, 0), menu => IsQuestionMenu(menu, "ExitMine"));
                case 112 when shaft.mineLevel <= 120:
                    return this.ExtrasCapture(WorldExtras.MineElevator, player, location, grab, facts, shaft, null, null,
                        () => new BgExtraFacts(WorldExtras.MineElevator, false, 0), menu => menu is MineElevatorMenu);
            }
        }
        if (location is BusStop)
        {
            int index = location.getTileIndexAt(gx, gy, "Buildings", "outdoors");
            facts = facts with { OccupantAtTile = ExtrasOccupant(player, location, rect, null) };
            if (index == 1057)
                return this.ExtrasCapture(WorldExtras.Bus, player, location, grab,
                    facts with { FacilityUnavailable = !Game1.MasterPlayer.mailReceived.Contains("ccVault") }, location, null, null,
                    () => new BgExtraFacts(WorldExtras.Bus, false, 0), menu => IsQuestionMenu(menu, "Bus"));
            if (index is 958 or 1080 or 1081)
                return this.ExtrasCapture(WorldExtras.Minecart, player, location, grab,
                    facts with { FacilityUnavailable = MinecartLocked("Default", location) }, location, null, null,
                    () => new BgExtraFacts(WorldExtras.Minecart, false, 0), menu => IsQuestionMenu(menu, "pagedResponse"));
        }

        bool objectHere = location.objects.TryGetValue(grab, out SObject? obj) && obj is not null;

        // 2. Native animal pet capture (shared with the production-capture harness).
        if (this.CaptureAnimalPet(player, location, grab, facts) is { } petCapture)
            return petCapture;

        // 3. Buildings / farmers come before characters in checkAction: leave them to the v1 resolution.
        if (this.CaptureMorningMailbox(player, location, grab, facts) is { } mailbox)
            return mailbox;
        if (BuildingTakesGrab(location, gx, gy))
            return null;
        if (location.farmers.Any(f => f is not null && !ReferenceEquals(f, player) && f.GetBoundingBox().Intersects(rect)))
            return null;

        // 4. Characters: the farm pet (Pet.checkAction) and the horse (Horse.checkAction); villagers stay v1 (npc).
        List<NPC> characters = location.characters.Where(c => c is not null && !c.IsMonster && c.GetBoundingBox().Intersects(rect)).ToList();
        if (characters.Count > 0)
        {
            NPC first = characters[0];
            facts = facts with { TargetCandidates = characters.Count, OccupantAtTile = false };
            if (first is Pet pet)
            {
                long id = player.UniqueMultiplayerID;
                int today = Game1.Date.TotalDays;
                bool petted = pet.lastPetDay.TryGetValue(id, out int day) && day == today;
                Pet pinned = pet;
                return this.ExtrasCapture(WorldExtras.Pet, player, location, grab, facts with { AlreadyDone = petted }, pet, pet.Name, null,
                    () => new BgExtraFacts(WorldExtras.Pet, pinned.lastPetDay.TryGetValue(id, out int d) && d == Game1.Date.TotalDays, 0,
                        pinned.friendshipTowardFarmer.Value));
            }
            if (first is Horse horse)
            {
                bool busy = horse.mutex.IsLocked();
                try { busy |= this.Helper.Reflection.GetField<int>(horse, "munchingCarrotTimer").GetValue() > 0; }
                catch { busy = true; }
                facts = facts with
                {
                    HorseHasRider = horse.rider is not null,
                    HorseUnnamed = string.IsNullOrEmpty(horse.Name),
                    HorseBusy = busy,
                };
                Horse pinned = horse;
                return this.ExtrasCapture(WorldExtras.HorseMount, player, location, grab, facts, horse, horse.Name, null,
                    () => new BgExtraFacts(WorldExtras.HorseMount, Game1.player?.mount is { } m && ReferenceEquals(m, pinned), 0));
            }
            return null; // a villager: the v1 npc kind (or its occupant miss)
        }
        if (location.resourceClumps.Any(c => c is not null && c.getBoundingBox().Intersects(rect)))
            return null;

        // 5. Objects: the hay hopper and ready allowlisted machines (empty hand / plain tool: ActiveObject is null).
        if (objectHere)
        {
            facts = facts with { OccupantAtTile = false, BoxedIn = ExtrasBoxedIn(player, location) };
            if (obj!.QualifiedItemId == WorldExtras.HayHopperQid)
            {
                GameLocation root = location.GetRootLocation();
                int amount = ExtrasHayWithdrawal(location);
                facts = facts with
                {
                    FacilityUnavailable = amount <= 0,
                    InventoryAccepts = amount > 0 && player.freeSpotsInInventory() > 0
                        && player.couldInventoryAcceptThisItem("(O)178", amount),
                };
                return this.ExtrasCapture(WorldExtras.HayHopper, player, location, grab, facts, obj, null, null,
                    () => new BgExtraFacts(WorldExtras.HayHopper, false, Game1.player is Farmer p ? InventoryCount(p, "(O)178") : 0,
                        root.piecesOfHay.Value, "(O)178"));
            }
            if (obj is CrabPot pot)
                return this.CaptureCrabPotCollect(player, location, grab, pot, facts); // W1 gap B3 S24 (ModEntry.B3Capture.cs)
            if (obj.bigCraftable.Value && obj is not Chest && obj.Type is "Crafting" or "interactive"
                && (obj.readyForHarvest.Value || WorldExtras.CollectMachines.Contains(obj.QualifiedItemId)))
            {
                SObject? output = obj.heldObject.Value;
                string outQid = output?.QualifiedItemId ?? "";
                bool ready = obj.readyForHarvest.Value && output is not null;
                facts = facts with
                {
                    MachineAllowlisted = WorldExtras.CollectMachines.Contains(obj.QualifiedItemId),
                    TargetReady = ready,
                    InventoryAccepts = output is not null && player.couldInventoryAcceptThisItem(output),
                    ProfessionDetail = ready ? this.ExtrasMachineProfession(player, obj.GetMachineData()?.ExperienceGainOnHarvest) : null,
                };
                SObject pinned = obj;
                GameLocation pinnedLocation = location;
                Vector2 pinnedTile = grab;
                return this.ExtrasCapture(WorldExtras.MachineCollect, player, location, grab, facts, obj, obj.QualifiedItemId, null,
                    () => new BgExtraFacts(WorldExtras.MachineCollect,
                        !(pinnedLocation.objects.TryGetValue(pinnedTile, out SObject? now) && ReferenceEquals(now, pinned)
                            && pinned.readyForHarvest.Value),
                        Game1.player is Farmer p ? InventoryCount(p, outQid) : 0, 0, outQid));
            }
            return null;
        }

        // 6. Terrain features (Tree / FruitTree shake, HoeDirt harvest).
        if (location.terrainFeatures.TryGetValue(grab, out TerrainFeature? feature) && feature is not null)
        {
            facts = facts with { OccupantAtTile = false };
            switch (feature)
            {
                case Tree tree:
                {
                    Tree pinned = tree;
                    return this.ExtrasCapture(WorldExtras.ShakeTree, player, location, grab,
                        facts with { TargetReady = tree.hasSeed.Value && tree.growthStage.Value >= 5 && !tree.stump.Value && !tree.tapped.Value },
                        tree, "tree", null, () => new BgExtraFacts(WorldExtras.ShakeTree, !pinned.hasSeed.Value, 0));
                }
                case FruitTree fruitTree:
                {
                    FruitTree pinned = fruitTree;
                    return this.ExtrasCapture(WorldExtras.ShakeTree, player, location, grab,
                        facts with { TargetReady = fruitTree.fruit.Count > 0 && fruitTree.growthStage.Value >= 4 && !fruitTree.stump.Value },
                        fruitTree, "fruit_tree", null,
                        () => new BgExtraFacts(WorldExtras.ShakeTree, pinned.fruit.Count == 0, 0, pinned.fruit.Count));
                }
                case HoeDirt dirt when dirt.crop is { } crop:
                {
                    // Live7 follow-up: a forage crop (Spring Onion) yields its native grab item, never indexOfHarvest.
                    bool forage = crop.forageCrop.Value;
                    string? forageQid = forage ? WorldExtras.ForageCropGrabQid(crop.whichForageCrop.Value) : null;
                    string harvest = forage ? forageQid ?? "" : crop.indexOfHarvest.Value ?? "";
                    string qid = harvest.Length == 0 ? "" : ItemRegistry.QualifyItemId(harvest) ?? harvest;
                    int price = 0;
                    try { price = (ItemRegistry.Create(harvest) as SObject)?.Price ?? 0; } catch { price = 0; }
                    facts = facts with
                    {
                        TargetReady = dirt.readyForHarvest() && (!forage || forageQid is not null),
                        ScytheCrop = crop.GetHarvestMethod() == HarvestMethod.Scythe,
                        // The forage quality is rolled at harvest: every possible quality must fit the bag.
                        InventoryAccepts = qid.Length > 0 && (forage
                            ? WorldExtras.ForageCropQualities.All(q => player.couldInventoryAcceptThisItem(qid, 1, q))
                            : player.couldInventoryAcceptThisItem(harvest, 1)),
                        ProfessionDetail = forage
                            ? this.ExtrasProfession(player, Farmer.foragingSkill, WorldExtras.ForageCropXp)
                            : this.ExtrasProfession(player, Farmer.farmingSkill, WorldExtras.HarvestXpEstimate(price)),
                    };
                    HoeDirt pinnedDirt = dirt;
                    Crop pinnedCrop = crop;
                    return this.ExtrasCapture(WorldExtras.HarvestCrop, player, location, grab, facts, crop, harvest, null,
                        () => new BgExtraFacts(WorldExtras.HarvestCrop,
                            !ReferenceEquals(pinnedDirt.crop, pinnedCrop) || !pinnedDirt.readyForHarvest(),
                            Game1.player is Farmer p ? InventoryCount(p, qid) : 0, 0, qid));
                }
            }
            return null;
        }

        // TV is the only newly admitted furniture action. Earlier objects/terrain/occupants retain priority.
        var furniture = location.furniture.Where(f => f is not null && f.GetBoundingBox().Intersects(rect)).ToList();
        if (furniture.Count > 0)
        {
            if (furniture.Count != 1 || furniture[0] is not TV tv || tv.heldObject.Value is not null)
                return null;
            return this.ExtrasCapture(WorldExtras.Tv, player, location, grab,
                facts with { OccupantAtTile = ExtrasOccupant(player, location, rect, null),
                    FacilityUnavailable = tv.GetType() != typeof(TV) }, tv, tv.QualifiedItemId, null,
                () => new BgExtraFacts(WorldExtras.Tv, false, 0),
                menu => menu is DialogueBox { isQuestion: true, characterDialogue: null }
                    && ReferenceEquals(location.afterQuestion?.Target, tv));
        }

        if (location is CommunityCenter && this.CaptureCommunityNote(player, location, grab, facts) is { } note)
            return note;

        // 7. Buildings-layer tile actions (GameLocation.performAction): elevator, minecart, the Mines entrance ladder.
        string? action = location.doesTileHaveProperty(gx, gy, "Action", "Buildings");
        if (action is null)
            return null;
        string[] parts = ArgUtility.SplitBySpace(action);
        facts = facts with { OccupantAtTile = this.InteractOccupantMiss(player, location, rect, grab, includeBelow: true) is not null };
        switch (parts.Length > 0 ? parts[0] : "")
        {
            case "AnimalShop":
                return this.CaptureAnimalShop(player, location, grab, facts);
            case "Blacksmith":
                return this.CaptureBlacksmith(player, location, grab, facts);
            case "Buy" or "OpenShop":
                return this.CaptureShopCounter(player, location, grab, facts);
            case "kitchen" or "Kitchen":
                return this.CaptureKitchen(player, location, grab, facts); // W1 gap B3 S30 (ModEntry.B3Capture.cs)
            case "MineElevator":
                return this.ExtrasCapture(WorldExtras.MineElevator, player, location, grab,
                    facts with { FacilityUnavailable = MineShaft.lowestLevelReached < 5 }, location, null, null,
                    () => new BgExtraFacts(WorldExtras.MineElevator, false, 0), menu => menu is MineElevatorMenu);
            case "MinecartTransport":
                return this.ExtrasCapture(WorldExtras.Minecart, player, location, grab,
                    facts with { FacilityUnavailable = MinecartLocked(parts.Length > 1 ? parts[1] : "Default", location) }, location, null, null,
                    () => new BgExtraFacts(WorldExtras.Minecart, false, 0), menu => IsQuestionMenu(menu, "pagedResponse"));
            case "Mine" or "NextMineLevel":
            {
                int level = parts.Length > 1 && int.TryParse(parts[1], out int l) ? l : 1;
                string expected = SceneIdentity.Token(MineShaft.GetLevelName(level));
                return this.ExtrasCapture(WorldExtras.LadderDown, player, location, grab, facts with { TargetMineLevel = level },
                    location, null, expected, null);
            }
            default:
                return null;
        }
    }

    /// <summary>Fix D-B2-1: the shop counter at the grab tile (Buildings "Buy General" / "OpenShop"), or null (not a counter).</summary>
    InteractKindCapture? CaptureShopCounter(Farmer player, GameLocation location, Vector2 grab, ExtrasGuardFacts facts)
    {
        int gx = (int)grab.X, gy = (int)grab.Y;
        string? action = location.doesTileHaveProperty(gx, gy, "Action", "Buildings");
        if (action == "AnimalShop") return this.ExtrasEnabled ? this.CaptureAnimalShop(player, location, grab, facts) : null;
        if (action is null)
            return null;
        string[] parts = ArgUtility.SplitBySpace(action);
        string? shopId = ShopCounterRule.ShopId(parts);
        if (shopId is null)
            return null;
        string? closed = CounterRefusal(player, location, parts, gx, gy, player.TilePoint.X, player.TilePoint.Y);
        Rectangle rect = TileRect(grab);
        facts = facts with
        {
            OccupantAtTile = this.InteractOccupantMiss(player, location, rect, grab, includeBelow: true) is not null,
            FacilityUnavailable = closed is not null,
        };
        if (closed is not null && closed != this.lastShopCounterMiss)
            this.Monitor.Log($"shop_counter {shopId} unavailable: {closed}", LogLevel.Debug);
        this.lastShopCounterMiss = closed;
        this.vendorCounterSample = new(location, grab, player.Tile, Game1.ticks, parts, closed);
        return this.ExtrasCapture(WorldExtras.ShopCounter, player, location, grab, facts, location, shopId, null,
            () => new BgExtraFacts(WorldExtras.ShopCounter, false, 0), menu => menu is ShopMenu);
    }

    static string? CounterRefusal(Farmer player, GameLocation location, string[] parts, int gx, int gy, int px, int py)
        => ShopCounterRule.Closed(CounterFacts(player, location, parts, gx, gy, px, py));

    static ShopCounterRule.Facts CounterFacts(Farmer player, GameLocation location, string[] parts, int gx, int gy, int px, int py)
    {
        string? shopId = ShopCounterRule.ShopId(parts);
        NPC? pierre = location is SeedShop ? location.getCharacterFromName("Pierre") : null;
        return new ShopCounterRule.Facts(parts, px, py, gx, gy, Game1.timeOfDay,
            location is SeedShop, pierre is null ? null : (pierre.TilePoint.X, pierre.TilePoint.Y),
            ShopOwnerInArea(shopId ?? "", location, parts), shopId == "FishShop" && location.characters.Any(c =>
                c is not null && c.Name == "Willy" && c.TilePoint.Y <= py - 1),
            parts.Length > 0 && parts[0] == "AnimalShop" && AnimalCounterOwnerPresent(location, new Vector2(gx, gy)));
    }

    string? lastShopCounterMiss;

    /// <summary>
    /// FIXB7 D-B5b-3 (live V2B5bG2ship-a: the bin could not be opened): the grab tile on a built ShippingBin's 2x1 tiles
    /// (ShippingBin.doAction: tileX..tileX+1, tileY -> ShowShipMenu = ItemGrabMenu(shippingBin)), or null. The receiver is
    /// the exact shipping-bin menu (IsShippingBinMenu); the press is the existing pad A retained-press hold.
    /// </summary>
    InteractKindCapture? CaptureShippingBin(Farmer player, GameLocation location, Vector2 grab, ExtrasGuardFacts facts)
    {
        int gx = (int)grab.X, gy = (int)grab.Y;
        ShippingBin? bin = location.buildings.OfType<ShippingBin>().FirstOrDefault(b => b is not null
            && gy == b.tileY.Value && gx >= b.tileX.Value && gx <= b.tileX.Value + 1);
        if (bin is null)
            return null;
        facts = facts with
        {
            OccupantAtTile = this.InteractOccupantMiss(player, location, TileRect(grab), grab, includeBelow: true) is not null,
            FacilityUnavailable = bin.daysOfConstructionLeft.Value > 0 || Context.IsMainPlayer || Context.IsSplitScreen,
        };
        return this.ExtrasCapture(WorldExtras.ShippingBin, player, location, grab, facts, bin, "ShippingBin", null,
            () => new BgExtraFacts(WorldExtras.ShippingBin, false, 0), menu => IsShippingBinMenu(menu as IClickableMenu, out _, out _));
    }

    /// <summary>
    /// FIXB7 ui.context "shipping_bins" (D-B5b-3, any distance in the location) and "store_chests" (D-B5b-1: the ordinary
    /// placed chests (BC)130 / (BC)232 within StoreChestRadius, nearest first, under their scene object fact ids) so the
    /// runtime can approach the bin / its store chest beyond the scene window. Read-only; same gate as extras_targets.
    /// </summary>
    void EnrichStoreTargets(Dictionary<string, object> snapshot, Farmer player, GameLocation location)
    {
        Point at = player.TilePoint;
        string raw = location.NameOrUniqueName ?? location.Name ?? "_";
        string loc = SceneIdentity.Token(raw);
        if (this.ShopCounterEnabled)
            snapshot["shipping_bins"] = location.buildings.OfType<ShippingBin>()
                .Where(b => b is not null && b.daysOfConstructionLeft.Value <= 0)
                .OrderBy(b => b.tileX.Value).ThenBy(b => b.tileY.Value).Take(2)
                .Select(b => (object)new Dictionary<string, object>
                {
                    ["kind"] = WorldExtras.ShippingBin, ["ref"] = "xt:bin:" + loc + ":" + b.tileX.Value + "," + b.tileY.Value,
                    ["x"] = b.tileX.Value, ["y"] = b.tileY.Value,
                }).ToArray();
        var chests = new List<(int Dist, string Ref, Dictionary<string, object> Entry)>();
        foreach (var pair in location.objects.Pairs)
        {
            int x = (int)pair.Key.X, y = (int)pair.Key.Y;
            int d = Math.Max(Math.Abs(x - at.X), Math.Abs(y - at.Y));
            if (d > StoreChestRadius || pair.Value is not Chest chest || chest.GetType() != typeof(Chest) || !chest.playerChest.Value
                || chest.QualifiedItemId is not ("(BC)130" or "(BC)232"))
                continue;
            string reference = SceneIdentity.ObjectFactId(raw, x, y, chest.QualifiedItemId);
            chests.Add((d, reference, new Dictionary<string, object> { ["ref"] = reference, ["x"] = x, ["y"] = y }));
        }
        snapshot["store_chests"] = chests.OrderBy(c => c.Dist).ThenBy(c => c.Ref, StringComparer.Ordinal).Take(StoreChestsMax)
            .Select(c => (object)c.Entry).ToArray();
    }

    /// <summary>OpenShop with an owner area (args 5-8): an owner of the shop's current owners stands inside it (Utility.TryOpenShopMenu).</summary>
    static bool ShopOwnerInArea(string shopId, GameLocation location, string[] parts)
    {
        try
        {
            if (parts.Length <= 8 || !int.TryParse(parts[5], out int x) || !int.TryParse(parts[6], out int y)
                || !int.TryParse(parts[7], out int w) || !int.TryParse(parts[8], out int h)
                || !DataLoader.Shops(Game1.content).TryGetValue(shopId, out var data))
                return false;
            var area = new Rectangle(x, y, w, h);
            return StardewValley.Internal.ShopBuilder.GetCurrentOwners(data).Any(owner =>
                location.characters.Any(npc => npc is not null && owner.IsValid(npc.Name) && area.Contains(npc.TilePoint)));
        }
        catch
        {
            return false;
        }
    }

    static bool MinecartLocked(string networkId, GameLocation location)
    {
        try
        {
            if (Game1.player?.mount is not null)
                return true;
            return !DataLoader.Minecarts(Game1.content).TryGetValue(networkId, out var network) || network is null
                || !GameStateQuery.CheckConditions(network.UnlockCondition, location)
                || network.Destinations is not { Count: > 0 };
        }
        catch
        {
            return true;
        }
    }

    /// <summary>
    /// FIXB10 D-B7b-1 (live V2B7bG6pet-a: the cat at (53,8) was approached from the cabin side, every press blocked,
    /// SMAPI `building_at_grab_tile`): the target's cardinal neighbours a farmer can stand on (on the map, not water,
    /// passable, nothing but characters / farmers blocking: buildings, objects, terrain features and furniture refuse),
    /// in the order below, up, right, left. Facing the target from one of them puts the A grab tile on it. Read-only.
    /// </summary>
    /// <summary>FIXB10 review: the stands are computed only for the published (sorted, capped) entries.</summary>
    static object[] WithStands(GameLocation location, IEnumerable<Dictionary<string, object>> entries) =>
        entries.Select(e =>
        {
            if (e["kind"] is string kind && !WorldExtras.UsesX(kind))
            {
                var (stands, misses) = ExtrasStandTiles(location, kind, (int)e["x"], (int)e["y"]);
                if (kind == WorldExtras.CommunityNote && e.TryGetValue("area", out object? nativeArea))
                    stands = stands.Where(s => NativeB1Offers.ResolveNote(location, Game1.player,
                        new Point((int)e["x"], (int)e["y"]), new Vector2(s[0], s[1])) == (int)nativeArea).ToArray();
                if (kind == WorldExtras.ShopCounter)
                {
                    string[] action = ArgUtility.SplitBySpace(location.doesTileHaveProperty((int)e["x"], (int)e["y"], "Action", "Buildings") ?? "");
                    var available = stands.Where(s => CounterRefusal(Game1.player, location, action, (int)e["x"], (int)e["y"], s[0], s[1]) is null).ToArray();
                    e["ready"] = available.Length > 0;
                    var stand = available.FirstOrDefault() ?? stands.FirstOrDefault() ?? new[] { (int)e["x"], (int)e["y"] + 1 };
                    e["availability"] = ShopCounterRule.Availability(CounterFacts(Game1.player, location, action,
                        (int)e["x"], (int)e["y"], stand[0], stand[1]), available.Length > 0);
                    if (available.Length == 0) e["refused"] = stands.Length == 0 ? "counter_stand_unreachable"
                        : CounterRefusal(Game1.player, location, action, (int)e["x"], (int)e["y"], stands[0][0], stands[0][1]) ?? "unknown";
                    stands = available;
                }
                e["stands"] = stands; // D-B7b-1: the A kinds' walkable sides
                if (misses.Length > 0)
                    e["stand_misses"] = misses; // FIXB13 D-B10-3: why each refused side was refused (diagnostic)
            }
            return (object)e;
        }).ToArray();

    /// <summary>
    /// FIXB13 D-B10-3 (live V2B10G6shake-a: trees (47,23) / (48,26) published `stands: []`): the walkable check is now the
    /// explicit list of what really blocks a farmer on that tile (map passability, buildings, NON-passable objects /
    /// terrain features / bushes / clumps / furniture), so passable grass, hoed dirt and flooring count as walkable; the
    /// previous IsTileBlockedBy(..., ignorePassables) call is gone. Each refused side carries its reason.
    /// </summary>
    /// <summary>FIXB14: the shared walkability verdict of one tile (stands and the nav grid use the same rule).</summary>
    static string? ExtrasTileMiss(GameLocation location, int sx, int sy)
    {
        var v = new Vector2(sx, sy);
        try
        {
            bool onMap = location.isTileOnMap(v);
            Rectangle rect = TileRect(v);
            string? miss = WorldExtras.StandMiss(
                onMap,
                onMap && location.isWaterTile(sx, sy),
                onMap && location.isTilePassable(new xTile.Dimensions.Location(sx, sy), Game1.viewport),
                WorldExtras.BuildingBlocksTile(BuildingShapes(location), sx, sy),  // FIXB16 review: the grid's building rule
                location.objects.TryGetValue(v, out SObject? o) && o is not null && !o.isPassable(),
                location.terrainFeatures.TryGetValue(v, out TerrainFeature? tf) && tf is not null && !tf.isPassable(),
                location.largeTerrainFeatures.Any(f => f is not null && !f.isPassable() && f.getBoundingBox().Intersects(rect)),
                location.resourceClumps.Any(c => c is not null && c.getBoundingBox().Intersects(rect)),
                location.furniture.Any(f => f is not null
                    && WorldExtras.FurnitureBlocks(f.isPassable(), f.IntersectsForCollision(rect))),  // FIXB16 D-B14b-1
                location.furniture.Any(f => f is not null && WorldExtras.BedSleepTile(f is BedFurniture,
                    f.GetBoundingBox().Intersects(rect), f.IntersectsForCollision(rect))));  // FIXB16 review: sleep row
            return NativeRouteCell.StandMiss(location, Game1.player, sx, sy, miss);
        }
        catch
        {
            return "unreadable";
        }
    }

    /// <summary>
    /// FIXB14 D-B12-2..4 (live V2B12G6pet-a / shake-a / ACC11a: the approach pressed into the farmhouse, bushes and Mines
    /// rocks one tile at a time; the runtime routing only knew scene objects / terrain with a passable flag, never
    /// buildings, walls or water, and could not tell a Stone from a wall): ui.context "nav_grid" = the walkability of a
    /// block-aligned window around ARTI with the same rule as the stands, a breakable Stone marked 's'. Read-only.
    /// </summary>
    readonly NavGridCache navGridCache = new();
    Dictionary<string, object>? navGridLast;

    /// <summary>FIXB15 review: one cell of the last published grid when it is this location's and covers the tile.</summary>
    char? NavGridCell(GameLocation location, int x, int y)
    {
        if (this.navGridLast is not { } g || g["rows"] is not string[] rows
            || !string.Equals(g["location"] as string, SceneIdentity.Token(location.NameOrUniqueName ?? location.Name ?? "_"), StringComparison.Ordinal))
            return null;
        int dx = x - (int)g["x0"], dy = y - (int)g["y0"];
        return dy >= 0 && dy < rows.Length && dx >= 0 && dx < rows[dy].Length ? rows[dy][dx] : null;
    }

    void EnrichNavGrid(Dictionary<string, object> snapshot)
    {
        if (!this.ExtrasEnabled || !Context.IsWorldReady || Game1.player is not Farmer player
            || player.currentLocation is not GameLocation location)
        {
            snapshot.Remove("nav_grid");
            this.navGridLast = null;
            this.navGridCache.Reset();
            return;
        }
        try
        {
            int x0 = WorldExtras.NavGridOrigin((int)player.Tile.X), y0 = WorldExtras.NavGridOrigin((int)player.Tile.Y);
            string token = SceneIdentity.Token(location.NameOrUniqueName ?? location.Name ?? "_");
            // FIXB14 review MAJOR (cost): rebuilt only when the window / location / world counts change, <= 4 Hz.
            string key = string.Join("|", token, x0, y0, location.objects.Length, location.terrainFeatures.Length,
                location.buildings.Count, location.largeTerrainFeatures.Count, location.resourceClumps.Count,
                location.furniture.Count, location.warps.Count, player.GetBoundingBox(),
                NativeEventControl.Token() ?? "ordinary", Environment.TickCount64 / WorldExtras.NavGridMinIntervalMs);
            if (this.navGridCache.ShouldRebuild(key, Environment.TickCount64) || this.navGridLast is null)
                this.navGridLast = BuildNavGrid(location, token, x0, y0);
            snapshot["nav_grid"] = this.navGridLast;
        }
        catch (Exception)
        {
            snapshot.Remove("nav_grid");
            this.navGridLast = null;
            this.navGridCache.Reset();
        }
    }

    /// <summary>FIXB16 review: the location's buildings for the shared building rule (WorldExtras.BuildingBlocksTile).</summary>
    static List<WorldExtras.BuildingShape> BuildingShapes(GameLocation location)
    {
        var shapes = new List<WorldExtras.BuildingShape>();
        foreach (var b in location.buildings)
            if (b is not null)
            {
                var building = b;
                shapes.Add(new WorldExtras.BuildingShape(b.tileX.Value, b.tileY.Value, b.tilesWide.Value, b.tilesHigh.Value,
                    Math.Max(0, b.GetAdditionalTilePropertyRadius()), (x, y) => building.isTilePassable(new Vector2(x, y))));
            }
        return shapes;
    }

    /// <summary>One nav grid build (read-only). Buildings, large features, clumps and furniture are rasterised once from
    /// their bounding boxes; warps from location.warps; the per-tile rule is the stands' (ExtrasTileMiss order).</summary>
    static Dictionary<string, object> BuildNavGrid(GameLocation location, string token, int x0, int y0)
    {
        int size = WorldExtras.NavGridSize;
        var big = new HashSet<(int, int)>();
        var sleep = new HashSet<(int, int)>();
        var clumps = new HashSet<(int, int)>();
        var warps = new HashSet<(int, int)>();
        void Mark(HashSet<(int, int)> set, Rectangle box)
        {
            for (int tx = box.Left / 64; tx <= (box.Right - 1) / 64; tx++)
                for (int ty = box.Top / 64; ty <= (box.Bottom - 1) / 64; ty++)
                    if (tx >= x0 && tx < x0 + size && ty >= y0 && ty < y0 + size)
                        set.Add((tx, ty));
        }
        // FIXB16 review (D-B14-2): the game's own per-building collision (door fronts inside a footprint are open unless
        // another building covers them); the same rule as the stands (ExtrasTileMiss).
        var buildings = WorldExtras.BuildingBlockedCells(BuildingShapes(location), x0, y0, size);
        foreach (var f in location.largeTerrainFeatures)
            if (f is not null && !f.isPassable())
                Mark(big, f.getBoundingBox());
        foreach (var c in location.resourceClumps)
            if (c is not null)
                Mark(clumps, c.getBoundingBox());
        foreach (var f in location.furniture)
        {
            if (f is null || f.isPassable())
                continue;
            // FIXB16 D-B14b-1 (live V2B14bACC4a: ARTI on the bed tile (10,8) was boxed in by '#' bed cells): only the
            // tiles the furniture really collides with (BedFurniture.IntersectsForCollision: the sleep area is walkable).
            var box = f.GetBoundingBox();
            for (int tx = box.Left / 64; tx <= (box.Right - 1) / 64; tx++)
                for (int ty = box.Top / 64; ty <= (box.Bottom - 1) / 64; ty++)
                {
                    bool collides = f.IntersectsForCollision(TileRect(new Vector2(tx, ty)));
                    if (WorldExtras.FurnitureBlocks(f.isPassable(), collides))
                        Mark(big, TileRect(new Vector2(tx, ty)));
                    else if (WorldExtras.BedSleepTile(f is BedFurniture, true, collides))
                        sleep.Add((tx, ty));  // FIXB16 review: the sleep row is 'z' (a route never steps onto it)
                }
        }
        foreach (var w in location.warps)
            if (w is not null)
                warps.Add((w.X, w.Y));
        var rows = new string[size];
        var row = new char[size];
        for (int dy = 0; dy < size; dy++)
        {
            for (int dx = 0; dx < size; dx++)
            {
                int x = x0 + dx, y = y0 + dy;
                var v = new Vector2(x, y);
                string? miss;
                try
                {
                    bool onMap = location.isTileOnMap(v);
                    miss = WorldExtras.StandMiss(
                        onMap,
                        onMap && location.isWaterTile(x, y),
                        onMap && location.isTilePassable(new xTile.Dimensions.Location(x, y), Game1.viewport),
                        buildings.Contains((x, y)),
                        location.objects.TryGetValue(v, out SObject? o) && o is not null && !o.isPassable(),
                        location.terrainFeatures.TryGetValue(v, out TerrainFeature? tf) && tf is not null && !tf.isPassable(),
                        big.Contains((x, y)),
                        clumps.Contains((x, y)),
                        false,
                        sleep.Contains((x, y)));
                }
                catch
                {
                    miss = "unreadable";
                }
                bool stone = miss == "object" && location.objects.TryGetValue(v, out SObject? s) && s is not null
                    && (s.IsBreakableStone() || s.Name == "Stone");
                row[dx] = NativeRouteCell.NavChar(location, Game1.player, x, y,
                    WorldExtras.NavChar(miss, stone, warps.Contains((x, y))));
            }
            rows[dy] = new string(row);
        }
        return new Dictionary<string, object>
        {
            ["location"] = token, ["x0"] = x0, ["y0"] = y0, ["w"] = size, ["h"] = size, ["rows"] = rows,
        };
    }

    /// <summary>FIXB15 D-B13-3: one nav cell on demand (the reflex route): the stand rule, a breakable stone 's', a warp 'x'.</summary>
    static char NavCellAt(GameLocation location, int x, int y)
    {
        try
        {
            string? miss = ExtrasTileMiss(location, x, y);
            bool stone = miss == "object" && location.objects.TryGetValue(new Vector2(x, y), out SObject? s) && s is not null
                && (s.IsBreakableStone() || s.Name == "Stone");
            bool warp = location.warps.Any(w => w is not null && w.X == x && w.Y == y);
            return NativeRouteCell.NavChar(location, Game1.player, x, y, WorldExtras.NavChar(miss, stone, warp));
        }
        catch
        {
            return '#';
        }
    }

    static (int[][] Stands, object[] Misses) ExtrasStandTiles(GameLocation location, string kind, int x, int y)
    {
        // FIXB19 D-B16-1: the same building step the press admission runs at the grab tile (= the target tile).
        string? taken = WorldExtras.AfterBuildingStep(kind) ? BuildingGrabMiss(location, x, y) : null;
        var (stands, misses) = WorldExtras.StandsFor(kind, x, y, taken, (sx, sy) => ExtrasTileMiss(location, sx, sy));
        return (stands.Select(s => new[] { s.X, s.Y }).ToArray(), misses.Select(m => (object)new object[] { m.X, m.Y, m.Miss }).ToArray());
    }

    // ------------------------------------------------------------------ ui.context extras_targets (approach catalog)

    /// <summary>
    /// ui.context "extras_targets" (flag on only): up to 16 nearby candidates the runtime may approach for a G6 objective
    /// (kind, ref, tile, label). Ordinary targets within 12 tiles, mine ladders/shafts within 40;
    /// the capture at the grab tile stays authoritative.
    /// </summary>
    void EnrichExtrasTargets(Dictionary<string, object> snapshot)
    {
        snapshot.Remove("shop_counter_status");
        if (!(this.ExtrasEnabled || this.ShopCounterEnabled) || !Context.IsWorldReady || Game1.player is not Farmer player
            || player.currentLocation is not GameLocation location)
            return;
        try
        {
            // World-interaction capture has just sampled this tick's counter predicates.
            // A failed capacity read cannot erase unrelated native abilities from this snapshot.
            try { snapshot["collection_fit"] = NativeCollectionFacts.Read(player, location); }
            catch { snapshot["collection_fit_status"] = "unreadable"; }
            if (this.ReadVendorCounterFacts() is { } counter) snapshot["shop_counter_status"] = counter;
            var found = new List<(int Dist, Dictionary<string, object> Entry)>();
            Point at = player.TilePoint;
            void Add(string kind, string reference, int x, int y, string label, bool ready = true)
            {
                int d = Math.Max(Math.Abs(x - at.X), Math.Abs(y - at.Y));
                // S27: a CC note comes from the native note tiles (<= one per area), so no scan radius applies to it.
                int radius = kind is WorldExtras.NpcGift or WorldExtras.Pet or WorldExtras.HorseMount or WorldExtras.AnimalPet
                    ? ExtrasCharacterRadius : kind is WorldExtras.LadderUp or WorldExtras.LadderDown or WorldExtras.Shaft ? ExtrasLadderUpRadius
                    : kind is WorldExtras.CommunityNote or WorldExtras.ShopCounter ? int.MaxValue : ExtrasTargetRadius;
                if (d > radius || reference.Length > 100)
                    return;
                found.Add((d, new Dictionary<string, object>
                {
                    ["kind"] = kind, ["ref"] = reference, ["x"] = x, ["y"] = y, ["label"] = UiPadTable.Trim(label, 40), ["ready"] = ready,
                }));
            }
            void AddNote(int x, int y, int area)
            {
                if (found.Any(f => (string)f.Entry["kind"] == WorldExtras.CommunityNote && (int)f.Entry["x"] == x && (int)f.Entry["y"] == y))
                    return;
                int prior = found.Count;
                Add(WorldExtras.CommunityNote, ExtrasSource(WorldExtras.CommunityNote, location, new Vector2(x, y), area.ToString()), x, y, "Community Center note " + area);
                if (found.Count > prior)
                {
                    found[^1].Entry["area"] = area;
                    string name = CommunityCenter.getAreaNameFromNumber(area);
                    // Read existing synchronized data; BundleData's getter can initialize the world.
                    found[^1].Entry["bundles"] = this.Helper.Reflection
                        .GetField<StardewValley.Network.NetStringDictionary<string, Netcode.NetString>>(Game1.netWorldState.Value, "netBundleData").GetValue().Pairs
                        .Where(b => b.Key.Split('/')[0] == name).Take(10)
                        .Select(b => UiPadTable.Trim(b.Value.Split('/')[0], 80)).ToArray();
                }
            }
            string loc = SceneIdentity.Token(location.NameOrUniqueName ?? location.Name ?? "_");
            snapshot["money"] = player.Money;
            snapshot["tool_upgrading"] = player.toolBeingUpgraded.Value?.QualifiedItemId ?? "";
            snapshot["mailbox_count"] = Game1.mailbox.Count; // local farmhand's native pending mailbox, read-only
            if (this.ExtrasEnabled)
                foreach (TV tv in location.furniture.OfType<TV>().Where(t => t.GetType() == typeof(TV)))
                {
                    // Publish each native bounding-box tile; the faced tile is re-captured before A.
                    Rectangle box = tv.GetBoundingBox();
                    for (int y = box.Top / 64; y < (box.Bottom + 63) / 64; y++)
                        for (int x = box.Left / 64; x < (box.Right + 63) / 64; x++)
                            Add(WorldExtras.Tv, ExtrasSource(WorldExtras.Tv, location, new Vector2(x, y), tv.QualifiedItemId), x, y, tv.DisplayName);
                }
            int today = Game1.Date.TotalDays;
            this.EnrichAnimalRoutine(snapshot, player, location);
            this.EnrichStoreTargets(snapshot, player, location); // FIXB7 D-B5b-1 / D-B5b-3

            // Fix D-B2-1: shop counters (Buildings "Buy General" / "OpenShop") for the economy sell / buy opener.
            for (int y = at.Y - ExtrasTargetRadius; y <= at.Y + ExtrasTargetRadius; y++)
                for (int x = at.X - ExtrasTargetRadius; x <= at.X + ExtrasTargetRadius; x++)
                {
                    if (!location.isTileOnMap(new Vector2(x, y)))
                        continue;
                    string? action = location.doesTileHaveProperty(x, y, "Action", "Buildings");
                    // ADR 59: a named destination for ordinary ACTION, not a donation executor.
                    // LibraryMuseum.performAction("Gunther") opens its native dialogue, which
                    // may offer Donate/Collect/Leave or explain that nothing is eligible.
                    if (this.ExtrasEnabled && location is LibraryMuseum
                        && WorldExtras.OrdinaryCounterKind(action) is { } counterKind)
                        Add(counterKind, ExtrasSource(counterKind, location, new Vector2(x, y), action),
                            x, y, "Gunther: museum donations and rewards");
                    if (this.ExtrasEnabled && MorningMailboxAt(player, location, new Vector2(x, y), out bool unavailable) && !unavailable)
                        Add(WorldExtras.Mailbox, ExtrasSource(WorldExtras.Mailbox, location, new Vector2(x, y), null), x, y, "Mailbox");
                    if (this.ExtrasEnabled && action == "Blacksmith")
                        Add(WorldExtras.Blacksmith, ExtrasSource(WorldExtras.Blacksmith, location, new Vector2(x, y), "Blacksmith"), x, y, "Blacksmith");
                    if (this.ExtrasEnabled && location is CommunityCenter)
                        foreach (var stand in WorldExtras.StandCandidates(x, y))
                            if (NativeB1Offers.ResolveNote(location, player, new Point(x, y), new Vector2(stand.X, stand.Y)) is int area)
                            {
                                AddNote(x, y, area);
                                break;
                            }
                    if (action is not null && ShopCounterRule.ShopId(ArgUtility.SplitBySpace(action)) is { } shopId)
                        Add(WorldExtras.ShopCounter, "xt:shop:" + loc + ":" + x + "," + y, x, y, shopId);
                    if (this.ExtrasEnabled && action is not null
                        && WorldExtras.MineEntranceLevel(location.Name, ArgUtility.SplitBySpace(action)) is not null)
                        Add(WorldExtras.LadderDown, "xt:mine:" + loc + ":" + x + "," + y, x, y, WorldExtras.LadderDown);
                }
            // ADR 38: counters beyond the local scan use the existing map-action catalog, not another map scan.
            foreach (var mapCounter in this.screens.Value.WorldKnowledge?.LocalCounters(location)
                ?? Enumerable.Empty<(Point Tile, string Shop)>())
            {
                bool duplicate = false;
                foreach (var existing in found)
                    if ((string)existing.Entry["kind"] == WorldExtras.ShopCounter
                        && (int)existing.Entry["x"] == mapCounter.Tile.X && (int)existing.Entry["y"] == mapCounter.Tile.Y)
                        duplicate = true;
                if (!duplicate)
                    Add(WorldExtras.ShopCounter, "xt:shop:" + loc + ":" + mapCounter.Tile.X + "," + mapCounter.Tile.Y,
                        mapCounter.Tile.X, mapCounter.Tile.Y, mapCounter.Shop);
            }
            // S27: the room notes (e.g. the Crafts Room seen from the CC entry) lie beyond the scan; publish each native note tile.
            if (this.ExtrasEnabled && location is CommunityCenter cc)
                foreach (var (area, tile) in NativeB1Offers.NoteTiles(cc, player))
                    AddNote(tile.X, tile.Y, area);
            if (!this.ExtrasEnabled)
            {
                // Economy flag only: the counters alone.
                snapshot.Remove("mine_ore_nodes");
                snapshot["extras_targets"] = WithStands(location, found.OrderBy(f => f.Dist)
                    .ThenBy(f => (string)f.Entry["ref"], StringComparer.Ordinal).Take(ExtrasTargetsMax).Select(f => f.Entry));
                return;
            }
            var blockers = new List<Dictionary<string, object>>();
            foreach (NPC c in location.characters)
            {
                if (c is not (Pet or Horse) && !c.IsMonster && !c.IsInvisible)
                {
                    int giftCount = found.Count;
                    Add(WorldExtras.NpcGift, GiftSource(location, c), c.TilePoint.X, c.TilePoint.Y, c.Name);
                    if (found.Count > giftCount && found[^1].Entry is { } giftEntry)
                    {
                        player.friendshipData.TryGetValue(c.Name, out Friendship? friendship);
                        giftEntry["npc_fact_id"] = this.NpcFactIdFor(location, location.NameOrUniqueName ?? location.Name ?? "_", c);
                        giftEntry["gifts_today"] = friendship?.GiftsToday ?? 0;
                        giftEntry["gifts_this_week"] = friendship?.GiftsThisWeek ?? 0;
                        giftEntry["gift_refusal"] = GiftNpcMiss(player, c) ?? "";
                    }
                }
                if (c is Horse unnamed && unnamed.rider is null && string.IsNullOrEmpty(unnamed.Name) && blockers.Count < 4)
                {
                    // FIXB15 D-B12-5 (live batch 12 extension: the only horse is unnamed; vanilla Horse.checkAction opens
                    // the naming menu for the stable owner only): never a mount target, but its blocker is published
                    // (any distance in the location) so the runtime reports a staging requirement, not "no target".
                    string? owner = null;
                    try { owner = Game1.getFarmerMaybeOffline(unnamed.ownerId.Value)?.Name; } catch { owner = null; }
                    blockers.Add(new Dictionary<string, object>
                    {
                        ["kind"] = WorldExtras.HorseMount, ["blocker"] = "horse_unnamed",
                        ["x"] = unnamed.TilePoint.X, ["y"] = unnamed.TilePoint.Y,
                        ["stable_owner"] = UiPadTable.Trim(owner ?? "", 40),
                    });
                }
                if (c is Pet pet)
                    Add(WorldExtras.Pet, "xt:pet:" + loc + ":" + SceneIdentity.Token(pet.Name), pet.TilePoint.X, pet.TilePoint.Y, pet.displayName ?? pet.Name,
                        !(pet.lastPetDay.TryGetValue(player.UniqueMultiplayerID, out int d) && d == today));
                else if (c is Horse horse && horse.rider is null && !string.IsNullOrEmpty(horse.Name))
                    Add(WorldExtras.HorseMount, "xt:horse:" + loc + ":" + SceneIdentity.Token(horse.Name), horse.TilePoint.X, horse.TilePoint.Y, horse.displayName ?? horse.Name);
            }
            foreach (FarmAnimal a in location.animals.Values)
            {
                if (a is null)
                    continue;
                string reference = "xt:animal:" + loc + ":" + a.myID.Value;
                Add(WorldExtras.AnimalPet, reference, a.TilePoint.X, a.TilePoint.Y, a.displayName ?? "", !a.wasPet.Value);
                if (a.currentProduce.Value is not null && a.isAdult() && a.GetAnimalData()?.HarvestTool is { } harvestTool)
                    Add(harvestTool == "Shears" ? WorldExtras.AnimalShear : WorldExtras.AnimalMilk, reference + ":product",
                        a.TilePoint.X, a.TilePoint.Y, a.displayName ?? "");
            }
            foreach (var pair in location.objects.Pairs)
            {
                SObject o = pair.Value;
                int x = (int)pair.Key.X, y = (int)pair.Key.Y;
                if (o is null || Math.Max(Math.Abs(x - at.X), Math.Abs(y - at.Y)) > ExtrasTargetRadius)
                    continue;
                string reference = "xt:obj:" + loc + ":" + x + "," + y;
                if (o.QualifiedItemId == WorldExtras.HayHopperQid)
                {
                    int amount = ExtrasHayWithdrawal(location);
                    int countBefore = found.Count;
                    Add(WorldExtras.HayHopper, reference, x, y, o.DisplayName ?? "", amount > 0);
                    if (found.Count > countBefore)
                    {
                        found[^1].Entry["withdrawal_count"] = amount;
                        found[^1].Entry["hay_stock"] = location.GetRootLocation().piecesOfHay.Value;
                    }
                }
                else if (o.readyForHarvest.Value && WorldExtras.CollectMachines.Contains(o.QualifiedItemId))
                    Add(WorldExtras.MachineCollect, reference, x, y, o.DisplayName ?? "");
                else if (o.heldObject.Value is null && !o.readyForHarvest.Value && WorldExtras.InsertMachines.Contains(o.QualifiedItemId))
                    Add(WorldExtras.MachineInsert, reference, x, y, o.DisplayName ?? "");
            }
            // Smoke 2026-10-23 D3 (journal 335/359/486): the first four bare plots in terrainFeatures order (the OLDEST
            // hoed tiles) were offered, so the plot ARTI had just hoed beside her never was. Keep the four NEAREST.
            var dirtFree = new List<(int D, string Ref, int X, int Y)>();
            foreach (var pair in location.terrainFeatures.Pairs)
            {
                int x = (int)pair.Key.X, y = (int)pair.Key.Y;
                if (Math.Max(Math.Abs(x - at.X), Math.Abs(y - at.Y)) > ExtrasTargetRadius)
                    continue;
                string reference = "xt:tf:" + loc + ":" + x + "," + y;
                switch (pair.Value)
                {
                    case Tree tree when tree.hasSeed.Value && tree.growthStage.Value >= 5 && !tree.stump.Value && !tree.tapped.Value:
                        Add(WorldExtras.ShakeTree, reference, x, y, "tree");
                        break;
                    case FruitTree ft when ft.fruit.Count > 0 && !ft.stump.Value:
                        Add(WorldExtras.ShakeTree, reference, x, y, "fruit tree");
                        break;
                    case HoeDirt dirt when dirt.crop is { } crop && WorldExtras.CropHarvestOffered(dirt.readyForHarvest(),
                                               crop.GetHarvestMethod() == HarvestMethod.Grab, crop.forageCrop.Value, crop.whichForageCrop.Value):
                        int priorHarvest = found.Count;
                        Add(WorldExtras.HarvestCrop, reference, x, y, (crop.forageCrop.Value
                            ? WorldExtras.ForageCropGrabQid(crop.whichForageCrop.Value) : crop.indexOfHarvest.Value) ?? "crop");
                        if (found.Count > priorHarvest)
                        {
                            try { found[^1].Entry["inventory_accepts"] = NativeCollectionFacts.AcceptsHarvest(player, crop); }
                            catch { found[^1].Entry["inventory_accepts_reason"] = "unreadable"; }
                        }
                        break;
                    case HoeDirt { crop: null } when !location.objects.ContainsKey(pair.Key):
                        dirtFree.Add((Math.Max(Math.Abs(x - at.X), Math.Abs(y - at.Y)), reference, x, y));
                        break;
                }
            }
            foreach (var dirt in dirtFree.OrderBy(t => t.D).ThenBy(t => t.Ref, StringComparer.Ordinal).Take(4))
                Add(WorldExtras.PlaceItem, dirt.Ref, dirt.X, dirt.Y, "hoed dirt");
            if (location is MineShaft shaft)
            {
                // Same read-only window as the up-ladder; no descent or press is admitted by this scan.
                for (int y = at.Y - ExtrasLadderUpRadius; y <= at.Y + ExtrasLadderUpRadius; y++)
                    for (int x = at.X - ExtrasLadderUpRadius; x <= at.X + ExtrasLadderUpRadius; x++)
                    {
                        if (!shaft.isTileOnMap(new Vector2(x, y)))
                            continue;
                        int index = shaft.getTileIndexAt(x, y, "Buildings", "mine");
                        string? kind = WorldExtras.MineTileOffer(index, Math.Max(Math.Abs(x - at.X), Math.Abs(y - at.Y)));
                        if (kind is not null)
                            Add(kind, "xt:mine:" + loc + ":" + x + "," + y, x, y, kind);
                    }
                // FIXB17 D-B5-0: the up-ladder above MineShaft.tileBeneathLadder, at any distance in the window.
                int ux = (int)shaft.tileBeneathLadder.X, uy = (int)shaft.tileBeneathLadder.Y - 1;
                if (shaft.isTileOnMap(new Vector2(ux, uy)) && shaft.getTileIndexAt(ux, uy, "Buildings", "mine") == 115)
                    Add(WorldExtras.LadderUp, "xt:mine:" + loc + ":" + ux + "," + uy, ux, uy, WorldExtras.LadderUp);
                // FINAL B7: read-only ore-node survey destinations in the same 40-tile window (never an offer).
                snapshot["mine_ore_nodes"] = WorldExtras.MineOreOffers(
                        shaft.objects.Pairs.Select(p => ((int)p.Key.X, (int)p.Key.Y, p.Value?.QualifiedItemId)), at.X, at.Y)
                    .Select(o => (object)new Dictionary<string, object> { ["x"] = o.X, ["y"] = o.Y, ["qualified_item_id"] = o.Qid })
                    .ToArray();
            }
            else
                snapshot.Remove("mine_ore_nodes");
            snapshot["extras_targets"] = WithStands(location, found.OrderBy(f => f.Dist)
                .ThenBy(f => (string)f.Entry["ref"], StringComparer.Ordinal).Take(ExtrasTargetsMax).Select(f => f.Entry));
            if (blockers.Count > 0)
                snapshot["extras_blockers"] = blockers.ToArray(); // FIXB15 D-B12-5
            else
                snapshot.Remove("extras_blockers");
            UiContextWire.Require(snapshot, "collection_fit", "collection_fit_status", "extras_targets", "extras_blockers");
        }
        catch (Exception)
        {
            snapshot.Remove("collection_fit");
            snapshot.Remove("animal_routine");
            snapshot.Remove("extras_blockers");
            snapshot.Remove("extras_targets");
            snapshot.Remove("mine_ore_nodes");
            snapshot.Remove("shipping_bins");
            snapshot.Remove("store_chests");
        }
    }
}
