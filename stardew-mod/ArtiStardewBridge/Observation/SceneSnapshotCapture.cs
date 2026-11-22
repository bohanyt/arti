using System.Runtime.CompilerServices;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Monsters;
using StardewValley.TerrainFeatures;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge.Observation;

/// <summary>
/// Adapter read-only OBS-1. HARUS dipanggil dari game thread.
///
/// Membaca Game1/GameLocation/objek runtime di sini, membangun DTO
/// imutabel, lalu menyerahkan DTO ke lapis async. Jangan memanggil
/// method ini dari thread kumpulan tugas atau thread jaringan.
///
/// Project ProtocolTests hanya mengompilasi Protocol/**/*.cs — PASS C#
/// BUKAN bukti berkas ini kompil terhadap Stardew. Crop harvest memakai
/// Crop.indexOfHarvest + ItemRegistry.QualifyItemId (1.6), bukan helper
/// harvest yang tidak ada di decompile 1.6 yang di-review.
/// </summary>
public sealed class SceneSnapshotCapture
{
    readonly SceneNpcIdentityRegistry npcIds = new();
    readonly ConditionalWeakTable<Monster, string> monsterIds = new();
    int nextMonsterOrdinal = 1;
    // One derived cache per farmer/screen; weak ownership also releases old save state.
    static readonly ConditionalWeakTable<Farmer, RefillSourceCache> refillSourceCaches = new();
    static long refillSourceRevision;

    // Bumped on the local warp (OBS-2B2b guard: no extra event subscriptions); a reloaded map is a new Map reference.
    public static void InvalidateRefillSourceCaches() => refillSourceRevision++;

    public ObservationPayload CaptureCurrent(
        Farmer farmer,
        IEnumerable<GameLocation> activeLocations,
        SceneObservationScheduler scheduler,
        long domainRevision
    )
    {
        GameLocation? location = farmer?.currentLocation;
        bool isCurrent = location is not null && ReferenceEquals(farmer!.currentLocation, location);
        bool activeSynced = false;
        if (location is not null)
        {
            foreach (GameLocation candidate in activeLocations)
            {
                if (ReferenceEquals(candidate, location))
                {
                    activeSynced = true;
                    break;
                }
            }
        }

        SceneAuthorityDecision authority = SceneAuthority.Classify(
            hasCurrentLocation: location is not null,
            isCurrentFarmerLocation: isCurrent,
            locationIsActiveSynced: activeSynced
        );
        string sessionId = scheduler.SessionId;
        if (string.IsNullOrEmpty(sessionId))
            return SceneSnapshotBuilder.Error("obs:missing:0:0", domainRevision, "session_mismatch", "no observation session");
        if (!authority.CanSnapshot || location is null)
            return SceneSnapshotBuilder.Error(
                sessionId,
                domainRevision,
                authority.ErrorReason ?? "unavailable",
                "current location cannot be captured as scene.current_location"
            );

        // Identity dari raw utuh. Jangan Bound 32 karakter dulu — itu
        // menabrak dua lokasi mod yang berbagi prefix.
        string locationRaw = location.NameOrUniqueName ?? location.Name ?? "_";
        string locationId = SceneIdentity.Token(locationRaw);
        int mapWidth = 0;
        int mapHeight = 0;
        var layers = location.Map?.Layers;
        if (layers is { Count: > 0 })
        {
            mapWidth = layers[0].LayerWidth;
            mapHeight = layers[0].LayerHeight;
        }

        SceneLocationFact locationFact = new()
        {
            LocationId = locationId,
            LocationKind = SceneWire.Bound(location.GetType().Name, SceneWireLimits.LocationKindMax),
            IsOutdoors = location.IsOutdoors,
            MapWidth = mapWidth,
            MapHeight = mapHeight,
            HomeLocation = string.IsNullOrEmpty(farmer?.homeLocation?.Value) ? null : SceneIdentity.Token(farmer!.homeLocation.Value),
        };

        SceneFactBag warps = new(SceneObservationCaps.Warps);
        if (location.warps is not null)
        {
            foreach (Warp warp in location.warps)
            {
                if (warp is null)
                    continue;
                warps.TryAdd(() => CaptureWarp(locationRaw, warp));
            }
        }

        SceneFactBag doors = new(SceneObservationCaps.Doors);
        CaptureDoors(location, locationRaw, doors);
        SceneFactBag buildingDoors = new(SceneObservationCaps.BuildingDoors);

        // Nearest-first traversal (SceneTraversalOrder): the per-category caps keep the
        // farmer's surroundings instead of the map's first entries. Keys only are
        // materialised; DTOs are still built only under the cap (SceneFactBag).
        int originX = (int)farmer!.Tile.X;
        int originY = (int)farmer.Tile.Y;

        CaptureBuildingDoors(location, locationRaw, farmer, originX, originY, buildingDoors);
        CaptureBeds(location, locationRaw, farmer, buildingDoors); // WS3b: own-home beds (shared bag)

        SceneFactBag objects = new(SceneObservationCaps.Objects);
        if (location.objects is not null)
        {
            List<(int X, int Y, SObject Value)> objectItems = new();
            foreach (KeyValuePair<Vector2, SObject> pair in location.objects.Pairs)
                objectItems.Add(((int)pair.Key.X, (int)pair.Key.Y, pair.Value));
            foreach ((int X, int Y, SObject Value) item in SceneTraversalOrder.Nearest(objectItems, originX, originY))
                objects.TryAdd(() => CaptureObject(locationRaw, new Vector2(item.X, item.Y), item.Value, this.ObserveAffordances));
        }

        SceneFactBag terrain = new(SceneObservationCaps.Terrain);
        if (location.terrainFeatures is not null)
        {
            List<(int X, int Y, TerrainFeature Value)> terrainItems = new();
            foreach (KeyValuePair<Vector2, TerrainFeature> pair in location.terrainFeatures.Pairs)
                terrainItems.Add(((int)pair.Key.X, (int)pair.Key.Y, pair.Value));
            // Crop lifecycle reads must survive the terrain cap, including distant ripe crops
            // that died overnight. The independent local window still captures nearby terrain.
            foreach ((int X, int Y, TerrainFeature Value) item in SceneTraversalOrder.Nearest(
                         terrainItems, originX, originY, t => t is HoeDirt { crop: not null }))
                terrain.TryAdd(() => CaptureTerrain(locationRaw, new Vector2(item.X, item.Y), item.Value));
        }

        SceneFactBag npcs = new(SceneObservationCaps.Npcs);
        SceneFactBag monsters = new(SceneObservationCaps.Monsters);
        if (location.characters is not null)
        {
            foreach (NPC npc in location.characters)
            {
                if (npc is null)
                    continue;
                if (npc is Monster monster)
                    monsters.TryAdd(() => CaptureMonster(locationRaw, monster));
                else
                    npcs.TryAdd(() => this.CaptureNpc(location, locationRaw, npc));
            }
        }

        SceneFactBag clumps = new(SceneObservationCaps.Clumps);
        if (location.resourceClumps is not null)
        {
            List<(int X, int Y, ResourceClump Value)> clumpItems = new();
            foreach (ResourceClump clump in location.resourceClumps)
            {
                if (clump is null)
                    continue;
                clumpItems.Add(((int)clump.Tile.X, (int)clump.Tile.Y, clump));
            }
            foreach ((int X, int Y, ResourceClump Value) item in SceneTraversalOrder.Nearest(clumpItems, originX, originY))
                clumps.TryAdd(() => CaptureClump(locationRaw, item.Value));
        }

        return SceneSnapshotBuilder.Snapshot(
            sessionId,
            domainRevision,
            authority,
            locationFact,
            warps.ToBatch(),
            doors.ToBatch(),
            objects.ToBatch(),
            terrain.ToBatch(),
            npcs.ToBatch(),
            monsters.ToBatch(),
            clumps.ToBatch(),
            buildingDoors.ToBatch()
        );
    }

    /// <summary>
    /// WS3a: building human doors with an interior (cabins, farmhouse, sheds...), nearest-first.
    /// Fact id matches the world_interact door source (SceneIdentity.BuildingDoorFactId).
    /// </summary>
    static void CaptureBuildingDoors(GameLocation location, string locationId, Farmer farmer, int originX, int originY,
        SceneFactBag doors)
    {
        if (location.buildings is null)
            return;
        string? home = string.IsNullOrEmpty(farmer.homeLocation?.Value) ? null : SceneIdentity.Token(farmer.homeLocation.Value);
        List<(int X, int Y, StardewValley.Buildings.Building Value)> items = new();
        foreach (StardewValley.Buildings.Building b in location.buildings)
        {
            if (b is null || b.humanDoor.X < 0 || b.humanDoor.Y < 0 || b.GetIndoors() is null)
                continue;
            items.Add((b.tileX.Value + b.humanDoor.X, b.tileY.Value + b.humanDoor.Y, b));
        }
        foreach ((int X, int Y, StardewValley.Buildings.Building Value) item in SceneTraversalOrder.Nearest(items, originX, originY))
        {
            StardewValley.Buildings.Building b = item.Value;
            doors.TryAdd(() =>
            {
                GameLocation indoors = b.GetIndoors();
                string target = indoors.NameOrUniqueName ?? indoors.Name ?? "_";
                string targetToken = SceneIdentity.Token(target);
                return new ScenePreparedFact(
                    SceneIdentity.BuildingDoorFactId(locationId, item.X, item.Y, target),
                    new SceneBuildingDoorFact
                    {
                        Tile = new TileXY { X = item.X, Y = item.Y },
                        Footprint = new SceneBounds { X = b.tileX.Value, Y = b.tileY.Value, Width = b.tilesWide.Value, Height = b.tilesHigh.Value },
                        TargetLocation = targetToken,
                        OwnedByActor = home is not null && string.Equals(home, targetToken, StringComparison.Ordinal),
                        Enterable = b.daysOfConstructionLeft.Value <= 0 && b is not StardewValley.Buildings.GreenhouseBuilding && !b.isMoving,
                    });
            });
        }
    }

    /// <summary>
    /// WS3b: beds the local farmhand can sleep in (BedFurniture, not a child bed) in a FarmHouse /
    /// Cabin, at most two, in the building-door bag (no new snapshot batch). Read-only.
    /// </summary>
    static void CaptureBeds(GameLocation location, string locationRaw, Farmer? farmer, SceneFactBag bag)
    {
        if (farmer is null || location is not StardewValley.Locations.FarmHouse home || location.furniture is null)
            return;
        bool owned = home.owner is Farmer owner && owner.UniqueMultiplayerID == farmer.UniqueMultiplayerID;
        int added = 0;
        foreach (StardewValley.Objects.Furniture furniture in location.furniture)
        {
            if (added >= 2)
                break;
            if (furniture is not StardewValley.Objects.BedFurniture bed
                || bed.bedType == StardewValley.Objects.BedFurniture.BedType.Child)
                continue;
            int x = (int)bed.TileLocation.X;
            int y = (int)bed.TileLocation.Y;
            int sleepX = x + bed.bedTileOffset;
            int width = bed.getTilesWide();
            if (bag.TryAdd(() => new ScenePreparedFact(SceneBedFact.FactId(locationRaw, x, y), new SceneBedFact
                {
                    Tile = new TileXY { X = x, Y = y },
                    SleepTile = new TileXY { X = sleepX, Y = y + 1 },
                    Width = width,
                    EntryTiles = SceneBedFact.Entry(x, y + 1, width),
                    OwnedByActor = owned,
                })))
                added++;
        }
    }

    public static ScenePreparedFact CaptureGather(Farmer farmer, UseToolInputVerdict? lastUse, bool farmTools = false)
    {
        object Capture()
        {
            var location = farmer.currentLocation;
            if (location is null) return GatherFeedback.Unavailable("location_unavailable");
            var layers = location.Map?.Layers;
            var refillCache = refillSourceCaches.GetOrCreateValue(farmer);
            refillCache.Bind(location, location.Map, layers is { Count: > 0 } ? layers[0].LayerWidth : 0,
                layers is { Count: > 0 } ? layers[0].LayerHeight : 0, refillSourceRevision);
            string raw = location.NameOrUniqueName;
            var center = farmer.Tile;
            var targets = new List<ScenePreparedFact>();
            // Nine dictionary lookups per category; never enumerate the world map.
            foreach (var (x, y) in InspectTilesQuery.EnumerateTiles((int)center.X, (int)center.Y, 1))
            {
                var tile = new Vector2(x, y);
                if (location.objects.TryGetValue(tile, out var obj) && obj is not null)
                    targets.Add(CaptureObject(raw, tile, obj));
                if (location.terrainFeatures.TryGetValue(tile, out var terrain) && terrain is not null)
                    targets.Add(CaptureTerrain(raw, tile, terrain));
            }
            var candidates = new List<(float X, float Y, object Value)>();
            bool complete = true;
            int groups = 0, chunks = 0;
            // Bound enumeration itself, including empty/far-away debris groups.
            foreach (var debris in location.debris)
            {
                if (++groups > GatherFeedback.MaxGroups) return GatherFeedback.Unavailable("drop_scan_limit");
                // Live 2026-10-06 (M1 attempts 9s-9u): the drop id carried Game1.ticks, so the same
                // resting item had a new identity every frame and the runtime (which keys approach
                // refs and decision equivalence on the id) could never act. The id is now stable for
                // the lifetime of the Debris object in this process: identity handle + chunk index.
                int debrisHandle = System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(debris) & 0x7fffffff;
                int chunkIndex = -1;
                foreach (var chunk in debris.Chunks)
                {
                    chunkIndex++;
                    if (++chunks > GatherFeedback.MaxChunks) return GatherFeedback.Unavailable("chunk_scan_limit");
                    var pos = chunk.position.Value;
                    if (pos.X < Game1.viewport.X || pos.Y < Game1.viewport.Y
                        || pos.X >= Game1.viewport.X + Game1.viewport.Width
                        || pos.Y >= Game1.viewport.Y + Game1.viewport.Height
                        || Math.Abs(pos.X / 64f - center.X) > 4 || Math.Abs(pos.Y / 64f - center.Y) > 4)
                        continue;
                    // Only materialized item identity. No RNG, future contents or HP.
                    string? qid = debris.item?.QualifiedItemId;
                    if (string.IsNullOrEmpty(qid) && !string.IsNullOrEmpty(debris.itemId.Value))
                        qid = ItemRegistry.QualifyItemId(debris.itemId.Value);
                    if (string.IsNullOrEmpty(qid)) { complete = false; continue; }
                    candidates.Add((pos.X, pos.Y, new Dictionary<string, object?> {
                        ["id"] = $"drop:{SceneIdentity.Token(raw)}:{debrisHandle}:{chunkIndex}:0",
                        ["qualified_item_id"] = qid, ["quality"] = debris.item?.Quality ?? debris.itemQuality,
                        ["pixel"] = new PixelXY { X = pos.X, Y = pos.Y },
                        ["quantity"] = (int?)null, ["provenance"] = "visible_materialized_item" }));
                }
            }
            var drops = GatherFeedback.NearestDrops(candidates, center.X * 64f + 32f, center.Y * 64f + 32f, out bool truncated);
            if (truncated) complete = false;
            // WS1: the exact tool hit tile (one GetToolLocation read) always; the nine tile predicates
            // only with BackgroundFarmToolsEnabled (no per-tick building/character scans otherwise).
            TileXY? toolTile = BackgroundToolTileProbe.ToolTile(farmer) is { } hit ? new TileXY { X = hit.X, Y = hit.Y } : null;
            List<GatherTileFact>? tiles = null;
            IReadOnlyList<TileXY>? refillSources = null;
            if (farmTools)
            {
                tiles = new List<GatherTileFact>(9);
                foreach (var (x, y) in InspectTilesQuery.EnumerateTiles((int)center.X, (int)center.Y, 1))
                    tiles.Add(new GatherTileFact {
                        X = x, Y = y,
                        Tillable = BackgroundToolTileProbe.Tillable(location, x, y),
                        WaterRefill = BackgroundToolTileProbe.WaterRefill(location, x, y),
                        Villager = BackgroundToolTileProbe.VillagerOnTile(location, x, y) });
                // M12: an empty can needs discoverable water beyond the 3x3 tool window.
                // Reuse the scoped gather fact; no fishing rod, map-name guesses or new store.
                if (farmer.Items.OfType<StardewValley.Tools.WateringCan>().Any(can => can.WaterLeft == 0))
                {
                    bool Refill(int x, int y) => BackgroundToolTileProbe.WaterRefill(location, x, y);
                    bool SourceClear(int x, int y) => !BackgroundToolTileProbe.VillagerOnTile(location, x, y);
                    bool Standable(int x, int y) => location.isTileOnMap(x, y)
                        && !location.isWaterTile(x, y) && location.isTilePassable(new Vector2(x, y))
                        && !location.IsTileBlockedBy(new Vector2(x, y), ~(CollisionMask.Characters | CollisionMask.Farmers), CollisionMask.All);
                    refillSources = refillCache.Find((int)center.X, (int)center.Y, Refill, Standable, SourceClear);
                }
            }
            return GatherFeedback.Build(SceneIdentity.Token(raw), Game1.ticks,
                new TileXY { X = (int)center.X, Y = (int)center.Y }, targets, drops, complete,
                farmer.UsingTool, lastUse, toolTile, tiles, refillSources);
        }
        try { return new ScenePreparedFact(GatherFeedback.FactId, Capture()); }
        catch { return new ScenePreparedFact(GatherFeedback.FactId, GatherFeedback.Unavailable("capture_failed")); }
    }

    static ScenePreparedFact CaptureWarp(string locationId, Warp warp)
    {
        string target = warp.TargetName ?? "";
        return new ScenePreparedFact(
            SceneIdentity.WarpFactId(locationId, warp.X, warp.Y, target, warp.TargetX, warp.TargetY),
            new SceneWarpFact
            {
                SourceTile = new TileXY { X = warp.X, Y = warp.Y },
                TargetLocation = SceneIdentity.Token(target),
                TargetTile = new TileXY { X = warp.TargetX, Y = warp.TargetY },
                NpcOnly = warp.npcOnly.Value ? true : null,
            }
        );
    }

    static void CaptureDoors(GameLocation location, string locationId, SceneFactBag doors)
    {
        // Subset umum: GameLocation.doors (tile -> target location).
        // Transisi khusus (tangga tambang, bus, dll.) ditunda; lihat OBS-1 doc.
        // FINAL Part C3: the same builder feeds the per-clock door refresh (ModEntry.RefreshDoorFactsOnClock).
        foreach (ScenePreparedFact fact in NativeB1Offers.DoorFacts(location, Game1.player, locationId))
            doors.TryAdd(() => fact);
    }

    /// <summary>
    /// Satu-satunya adapter object OBS-1/OBS-2B1. Event add harus melewati
    /// method ini juga supaya semantic ID dan DTO snapshot/delta identik.
    /// HARUS dipanggil dari game thread.
    /// </summary>
    internal static ScenePreparedFact CaptureObject(
        string locationId,
        Vector2 tile,
        SObject obj
    )
    {
        int x = (int)tile.X;
        int y = (int)tile.Y;
        string qid = obj.QualifiedItemId ?? "";
        Rectangle box = obj.GetBoundingBox();
        SObject? held = obj.heldObject?.Value;
        bool machine = obj.GetMachineData() is not null || held is not null;
        return new ScenePreparedFact(
            SceneIdentity.ObjectFactId(locationId, x, y, qid),
            new SceneObjectFact
            {
                QualifiedItemId = SceneIdentity.Token(qid),
                Name = SceneWire.Bound(obj.Name, SceneWireLimits.NameMax),
                DisplayName = SceneWire.Bound(obj.DisplayName, SceneWireLimits.DisplayNameMax),
                Tile = new TileXY { X = x, Y = y },
                Bounds = new SceneBounds { X = box.X, Y = box.Y, Width = box.Width, Height = box.Height },
                Passable = obj.isPassable(),
                Grabbable = obj.IsSpawnedObject || obj.isForage(),
                Spawned = obj.IsSpawnedObject ? true : null,
                Machine = machine,
                ReadyForHarvest = obj.readyForHarvest.Value,
                MinutesUntilReady = obj.MinutesUntilReady,
                HeldItem = held is null
                    ? null
                    : new SceneHeldItemFact
                    {
                        QualifiedItemId = SceneIdentity.Token(held.QualifiedItemId ?? ""),
                        Stack = held.Stack,
                        Name = SceneWire.Bound(held.Name, SceneWireLimits.NameMax),
                    },
            }
        );
    }

    /// <summary>
    /// Satu-satunya adapter terrain OBS-1/OBS-2B2a. Event add harus melewati
    /// method ini supaya semantic ID dan SceneTerrainFact snapshot/delta identik.
    /// HARUS dipanggil dari game thread.
    /// </summary>
    internal static ScenePreparedFact CaptureTerrain(
        string locationId,
        Vector2 tile,
        TerrainFeature feature
    )
    {
        int x = (int)tile.X;
        int y = (int)tile.Y;
        string terrainType = feature.GetType().Name;
        SceneCropBlock? crop = null;
        SceneWildTreeBlock? wild = null;
        SceneFruitTreeBlock? fruit = null;
        bool? passable = null;
        try
        {
            passable = feature.isPassable();
        }
        catch (Exception)
        {
            passable = null;
        }

        if (feature is HoeDirt dirt)
        {
            Crop? c = dirt.crop;
            crop = new SceneCropBlock
            {
                QualifiedItemId = QualifyCropHarvestId(c),
                Phase = c?.currentPhase.Value,
                Dead = c?.dead.Value,
                Watered = dirt.state.Value == HoeDirt.watered,
                ReadyForHarvest = dirt.readyForHarvest(),
                ForageCrop = c?.forageCrop.Value == true ? true : null,
                HarvestMethod = c?.GetHarvestMethod().ToString(),
            };
        }
        else if (feature is Tree tree)
        {
            wild = new SceneWildTreeBlock
            {
                TreeType = SceneWire.Bound(tree.treeType.Value?.ToString(), SceneWireLimits.TreeTypeMax),
                GrowthStage = tree.growthStage.Value,
                Tapped = tree.tapped.Value,
                Stump = tree.stump.Value,
            };
        }
        else if (feature is FruitTree fruitTree)
        {
            fruit = new SceneFruitTreeBlock
            {
                TreeType = SceneWire.Bound(fruitTree.treeId.Value?.ToString(), SceneWireLimits.TreeTypeMax),
                GrowthStage = fruitTree.growthStage.Value,
                HasFruit = fruitTree.fruit?.Count > 0,
            };
        }

        return new ScenePreparedFact(
            SceneIdentity.TerrainFactId(locationId, x, y, terrainType),
            new SceneTerrainFact
            {
                TerrainType = SceneIdentity.Token(terrainType),
                Tile = new TileXY { X = x, Y = y },
                Passable = passable,
                Crop = crop,
                WildTree = wild,
                FruitTree = fruit,
            }
        );
    }

    /// <summary>
    /// Stardew 1.6 menyimpan hasil panen di Crop.indexOfHarvest sebagai
    /// item ID belum tentu qualified. Kualifikasi resmi: ItemRegistry.
    /// </summary>
    static string? QualifyCropHarvestId(Crop? crop)
    {
        if (crop is null)
            return null;
        // Live7 follow-up: a forage crop's yield is fixed by Crop.harvest / hitWithHoe, not indexOfHarvest.
        if (crop.forageCrop.Value)
            return WorldExtras.ForageCropYieldQid(crop.whichForageCrop.Value) is { } forage ? SceneIdentity.Token(forage) : null;
        string raw = crop.indexOfHarvest.Value.ToString() ?? "";
        if (string.IsNullOrWhiteSpace(raw))
            return null;
        string qualified = ItemRegistry.QualifyItemId(raw);
        if (string.IsNullOrWhiteSpace(qualified))
            qualified = SceneHarvestIds.QualifyObjectItemId(raw);
        return SceneIdentity.Token(qualified);
    }

    /// <summary>
    /// Satu-satunya adapter NPC OBS-1/OBS-2B2b. Event add harus melewati
    /// method ini supaya semantic ID dan SceneCharacterFact snapshot/delta identik.
    /// HARUS dipanggil dari game thread.
    /// </summary>
    internal ScenePreparedFact CaptureNpc(
        GameLocation location,
        string locationId,
        NPC npc
    )
    {
        Point tile = npc.TilePoint;
        string internalName = npc.Name ?? "";
        Guid nativeMembershipId = location.characters.GuidOf(npc);
        string id = this.npcIds.GetOrCreateFactId(
            npc,
            nativeMembershipId,
            locationId,
            internalName
        );
        return new ScenePreparedFact(
            id,
            new SceneCharacterFact
            {
                Kind = SceneFactKind.Npc,
                Category = "npc",
                InternalName = SceneIdentity.Token(internalName),
                DisplayName = SceneWire.Bound(npc.displayName, SceneWireLimits.DisplayNameMax),
                Tile = new TileXY { X = tile.X, Y = tile.Y },
                Pixel = new PixelXY { X = npc.Position.X, Y = npc.Position.Y },
                Facing = npc.FacingDirection,
                Moving = npc.isMoving(),
                // WS3b: talkable villager / already talked today (local player's friendship read only).
                Villager = npc.IsVillager && npc.CanSocialize ? true : null,
                TalkedToday = Game1.player?.friendshipData is { } friends
                    && friends.TryGetValue(npc.Name ?? "", out Friendship? friendship) && friendship?.TalkedToToday == true ? true : null,
            }
        );
    }

    /// <summary>
    /// REMOVE terjadi setelah NPC keluar dari NetCollection, sehingga GuidOf
    /// sudah kosong. Pakai discriminator yang dicache oleh snapshot/ADD.
    /// </summary>
    internal bool TryGetNpcFactId(
        string locationId,
        NPC npc,
        out string factId
    )
    {
        return this.npcIds.TryGetFactId(
            npc,
            locationId,
            out factId
        );
    }

    ScenePreparedFact CaptureMonster(string locationId, Monster monster)
    {
        if (!this.monsterIds.TryGetValue(monster, out string? eph))
        {
            eph = SceneIdentity.MonsterEphemeral(this.nextMonsterOrdinal++);
            this.monsterIds.Add(monster, eph);
        }

        Point tile = monster.TilePoint;
        return new ScenePreparedFact(
            SceneIdentity.MonsterFactId(locationId, eph),
            new SceneCharacterFact
            {
                Kind = SceneFactKind.Monster,
                Category = "monster",
                InternalName = SceneIdentity.Token(monster.Name ?? ""),
                DisplayName = SceneWire.Bound(monster.displayName, SceneWireLimits.DisplayNameMax),
                Tile = new TileXY { X = tile.X, Y = tile.Y },
                Pixel = new PixelXY { X = monster.Position.X, Y = monster.Position.Y },
                Facing = monster.FacingDirection,
                Moving = monster.isMoving(),
                Monster = new SceneMonsterBlock
                {
                    Ephemeral = true,
                    Health = monster.Health,
                    MaxHealth = monster.MaxHealth,
                },
            }
        );
    }

    static ScenePreparedFact CaptureClump(string locationId, ResourceClump clump)
    {
        int x = (int)clump.Tile.X;
        int y = (int)clump.Tile.Y;
        int sheet = clump.parentSheetIndex.Value;
        string discriminator = sheet.ToString();
        return new ScenePreparedFact(
            SceneIdentity.ClumpFactId(locationId, x, y, discriminator),
            new SceneClumpFact
            {
                ClumpType = SceneWire.Bound(ClumpTypeName(sheet), SceneWireLimits.ClumpTypeMax),
                Tile = new TileXY { X = x, Y = y },
                Width = clump.width.Value,
                Height = clump.height.Value,
                ParentSheetIndex = sheet,
            }
        );
    }

    static string ClumpTypeName(int parentSheetIndex) => parentSheetIndex switch
    {
        600 => "stump",
        602 => "hollow_log",
        672 => "boulder",
        622 => "meteorite",
        _ => $"sheet_{parentSheetIndex}",
    };

    internal bool ObserveAffordances { get; set; }

    internal static ScenePreparedFact CaptureObject(string locationRaw, Vector2 tile, SObject obj, bool affordances)
    {
        var captured = CaptureObject(locationRaw, tile, obj);
        return affordances && obj.IsBreakableStone() && captured.Dto is SceneObjectFact fact
            ? new ScenePreparedFact(captured.Id, fact with { ToolBreakable = true }) : captured;
    }

    internal static ScenePreparedFact? CaptureMapAffordances(GameLocation? location, TileXY origin, NativeWorldKnowledge? knowledge)
    {
        if (location is null) return null;
        string raw = location.NameOrUniqueName;
        var rows = new List<(int X, int Y, object[] Value)>();
        bool ordinary = !Game1.eventUp;
        if (ordinary && knowledge is not null)
        {
            foreach (var (tile, action, touch) in knowledge.ActionCells(location))
            {
                if (touch || location.doesTileHaveProperty(tile.X, tile.Y, "Action", "Buildings") != action)
                    continue;
                string pin = UiKeyTargetToken.Hash(raw + ":" + tile.X + "," + tile.Y + ":" + action);
                string[] parts = action.Split(' ', StringSplitOptions.RemoveEmptyEntries);
                string family = parts.Length == 2 && int.TryParse(parts[1], out _) ? parts[0] : action;
                string group = UiKeyTargetToken.Hash(raw + ":" + family);
                rows.Add((tile.X, tile.Y, new object[] { "map-action:" + pin[..20], tile.X, tile.Y, new[] { "press" }, group[..20] }));
            }
        }
        const int limit = 96;
        var cells = rows.Count <= limit ? rows : SceneTraversalOrder.Nearest(rows, origin.X, origin.Y).Take(limit);
        return new ScenePreparedFact("affordance:map", new {
            kind = "map_affordances", location_id = SceneIdentity.Token(raw), ordinary_world = ordinary,
            cells = cells.Select(row => row.Value).ToArray(), omitted = Math.Max(0, rows.Count - limit) });
    }
}
