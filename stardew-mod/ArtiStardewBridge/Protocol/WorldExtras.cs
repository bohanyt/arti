namespace ArtiStardewBridge.Protocol;

// Primitives v2 WS-G6 (PLAN.md 1.2 world row, 1.4 denylist; WS-G6 brief section 2): world extras on the
// pad path. Pure, SMAPI-free: the kind table (which pad button, which held item, which success outcome),
// the EXPLICIT per-kind guard list evaluated in order (first failing guard = the refusal), the policies
// (profession-menu XP rule, mines time rule mirroring WS-G5's ReflexContracts constants) and the per-kind
// effect readback classification. ModEntry.Extras.cs samples the game into these records. Flag
// WorldExtrasEnabled (default OFF): with it off no extras kind is ever captured or offered.
// Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G6 live witnesses.
//
// Decomp (Stardew 1.6.15, G1 = StardewValley/Game1.cs):
//   A -> pressActionButton -> tryToCheckAt(grab tile) -> GameLocation.checkAction: CheckPetAnimal first when no
//   object is on the tile (GameLocation.cs:7638-7645; FarmAnimal.pet refuses at >= 19:00 with a sleep dialogue,
//   FarmAnimal.cs:703-707, grants +5 Farming XP :782), buildings, farmers, characters (Pet.checkAction Pet.cs:619,
//   lastPetDay per farmer :641-643; Horse.checkAction Horse.cs:599, naming menu when unnamed :622-638), objects
//   (machines: checkForAction with no ActiveObject, Object.cs:3930 / CheckForActionOnMachine :4593 incl.
//   ExperienceGainOnHarvest; hay hopper :4538), mount (dismount, GameLocation.cs:7842), terrain features
//   (Tree.performUseAction -> shake, Tree.cs:393/639; FruitTree.cs:222; HoeDirt.performUseAction -> Crop.harvest,
//   HoeDirt.cs:478, Crop.cs:484, XP round(16 ln(0.018 price + 1)) Crop.cs:691-694), Buildings-layer actions
//   (MineElevator / MinecartTransport, GameLocation.cs:9790-9806). MineShaft.checkAction runs first: 173 ladder
//   -> enterMine(level+1), 174 shaft -> question "Shaft", 112 elevator -> MineElevatorMenu (MineShaft.cs:3053-3096).
//   BusStop: 1057 bus ticket question "Bus" (ccVault), 958/1080/1081 minecart (BusStop.cs:70-95).
//   X -> pressUseToolButton (G1:11589-11722): canGrabSomethingFromHere at the tool point first (a ready object or
//   crop is HARVESTED), leftClick, then with an object held: stowing (options.allowStowing && CanPlayerStowItem,
//   G1:11734) BEFORE checkAction at the tool point (machine drop-in) and Utility.tryToPlaceItem at
//   GetNearbyValidPlacementPosition (Utility.cs:6868; with the cursor hidden it walks from the tool tile toward the
//   player: the faced tile must itself be valid or the item lands elsewhere). MilkPail / Shears beginUsing pick
//   the animal under the tool rect (MilkPail.cs:36-45), DoFunction adds the product + 5 Farming XP (:107-130).

/// <summary>The WS-G6 world_interact kinds (wire "kind" values).</summary>
/// <summary>FIXB14 review: the nav grid rebuild gate (pure; ModEntry holds one).</summary>
public sealed class NavGridCache
{
    string? key;
    long builtMs = long.MinValue;

    public int Rebuilds { get; private set; }

    /// <summary>True when the grid must be rebuilt now: the key changed (or none yet) and the last build is at least
    /// NavGridMinIntervalMs old. A changed key inside the interval waits (the previous grid stays published).</summary>
    public bool ShouldRebuild(string key, long nowMs)
    {
        if (this.key == key || (this.key is not null && nowMs - this.builtMs < WorldExtras.NavGridMinIntervalMs))
            return false;
        this.key = key;
        this.builtMs = nowMs;
        this.Rebuilds++;
        return true;
    }

    public void Reset() => this.key = null;
}

public static class WorldExtras
{
    // Read-only mine offer window. Press admission still recaptures the faced native tile.
    public const int MineLadderOfferRadius = 40;
    public static string? MineTileOffer(int tileIndex, int distance) => tileIndex switch
    {
        173 when distance is >= 0 and <= MineLadderOfferRadius => LadderDown,
        174 when distance is >= 0 and <= MineLadderOfferRadius => Shaft,
        112 when distance is >= 0 and <= 12 => MineElevator,
        _ => null,
    };

    /// <summary>FINAL B7 (S23 saw no ore node on floors 1-3: the scene publishes only the 40 nearest objects, a few
    /// tiles on a dense mine floor): ui.context "mine_ore_nodes" lists the native ore-node objects within
    /// <see cref="MineLadderOfferRadius"/>, nearest first, at most <see cref="MineOreOffersMax"/>. Read-only survey
    /// destinations; no offer, approach entry or press is admitted by it (the faced scene/tool capture still is).
    /// Ids = GameLocation.breakStone's ore branches (copper 751/849, iron 290/850, gold 764, iridium 765).</summary>
    public const int MineOreOffersMax = 6;
    public static readonly IReadOnlyCollection<string> OreNodeIds =
        new HashSet<string>(StringComparer.Ordinal) { "(O)751", "(O)849", "(O)290", "(O)850", "(O)764", "(O)765" };

    public static IReadOnlyList<(int X, int Y, string Qid)> MineOreOffers(
        IEnumerable<(int X, int Y, string? Qid)> objects, int atX, int atY) =>
        objects.Where(o => o.Qid is not null && OreNodeIds.Contains(o.Qid))
            .Select(o => (o.X, o.Y, Qid: o.Qid!, D: Math.Max(Math.Abs(o.X - atX), Math.Abs(o.Y - atY))))
            .Where(o => o.D <= MineLadderOfferRadius)
            .OrderBy(o => o.D).ThenBy(o => o.Y).ThenBy(o => o.X)
            .Take(MineOreOffersMax)
            .Select(o => (o.X, o.Y, o.Qid))
            .ToList();

    public const string Pet = "pet", AnimalPet = "animal_pet", AnimalMilk = "animal_milk", AnimalShear = "animal_shear",
        HorseMount = "horse_mount", HorseDismount = "horse_dismount", ShakeTree = "shake_tree", HarvestCrop = "harvest_crop",
        MachineCollect = "machine_collect", MachineInsert = "machine_insert", HayHopper = "hay_hopper", HayPlace = "hay_place",
        LadderDown = "ladder_down", Shaft = "shaft", MineElevator = "mine_elevator", Bus = "bus", Minecart = "minecart",
        PlaceItem = "place_item", NpcGift = "npc_gift",
        // Fix D-B2-1 (live batch 2): the shop counter (Buildings "Buy General" at Pierre's / "OpenShop") opens the ShopMenu
        // in the press frame. Captured with WorldExtrasEnabled OR EconomyMenusEnabled (the G2 opener), pad path only.
        AnimalShop = "animal_shop", Blacksmith = "blacksmith", CommunityNote = "community_note",
        ShopCounter = "shop_counter",
        // FIXB7 D-B5b-3 (live V2B5bG2ship-a): the shipping bin (ShippingBin building, 2x1) opens ItemGrabMenu(shippingBin)
        // in the press frame (ShippingBin.doAction -> ShowShipMenu). Captured with EconomyMenusEnabled, pad path only.
        ShippingBin = "shipping_bin",
        // FIXB17 D-B5-0 (acceptance step 11 `exited_mines`): the Mines up-ladder (Buildings tile 115) opens the question
        // "ExitMine" (Leave / Do, MineShaft.cs:3073-3081), the runtime answers Leave (the combat goal's exit leg).
        LadderUp = "ladder_up", Tv = "tv", Mailbox = "mailbox",
        // W1 gap B3 S30: the faced Kitchen A in the actor's own home opens the standalone cooking CraftingPage.
        Kitchen = "kitchen",
        // W1 gap B4 S28: the faced villager A with a held stack that an active item-delivery quest wants (Protocol/QuestFacts.cs).
        QuestDelivery = "quest_delivery";

    public static readonly IReadOnlyList<string> Kinds = new[]
    {
        Tv, Mailbox, Pet, AnimalPet, AnimalMilk, AnimalShear, HorseMount, HorseDismount, ShakeTree, HarvestCrop, MachineCollect,
        MachineInsert, HayHopper, HayPlace, LadderDown, Shaft, MineElevator, Bus, Minecart, PlaceItem, ShopCounter, ShippingBin, LadderUp, NpcGift, AnimalShop, Blacksmith, CommunityNote, Kitchen,
        QuestDelivery,
    };

    // Success outcomes (mod verdict vocabulary; Python OUTCOME_SUKSES_INTERACT mirrors them).
    public const string GiftGiven = "GiftGiven", Petted = "Petted", AnimalPetted = "AnimalPetted", ProductCollected = "ProductCollected",
        Mounted = "Mounted", Dismounted = "Dismounted", TreeShaken = "TreeShaken", CropHarvested = "CropHarvested",
        MachineCollected = "MachineCollected", MachineLoaded = "MachineLoaded", HayTaken = "HayTaken",
        ItemPlaced = "ItemPlaced", MenuOpened = "MenuOpened",
        // W1 gap B4 S28: the pinned active item-delivery quest completed (or terminally removed) with its exact bag debit.
        QuestDelivered = "QuestDelivered";

    public static bool IsKind(string? kind) => kind is not null && Kinds.Contains(kind, StringComparer.Ordinal);

    /// <summary>Pad button of the kind: X for the held-item / tool kinds (PLAN 1.4 X rows), else A.</summary>
    public static string ButtonOf(string? kind) => UsesX(kind) ? PadAbstractButton.X : PadAbstractButton.A;

    public static bool UsesX(string? kind) => kind is AnimalMilk or AnimalShear or MachineInsert or PlaceItem;

    /// <summary>FIXB10 D-B7b-1: the candidate stands of an A-kind target, in order: below, above, right, left (the mod
    /// keeps the walkable ones; facing the target from any of them puts the A grab tile on it).</summary>
    /// <summary>FIXB13 D-B11-1: the X kinds (pail / shears / drop-in / placement) resolve at the X press's tool tile;
    /// without a readable tool tile, the grab tile (as before).</summary>
    public static (int X, int Y) XResolutionTile((int X, int Y) grab, (int X, int Y)? tool) => tool ?? grab;

    /// <summary>FIXB14 D-B12-1: the tile a pad press acts on for a world_interact kind: the X kinds the tool tile (the faced
    /// neighbour), every other kind the grab tile (A press).
    /// FIXB23 D-B20-3: a character A kind with the vanilla fallback tile (CharacterFallbackTile) acts there.</summary>
    public static (int X, int Y) PressTile(string? kind, (int X, int Y) grab, (int X, int Y)? tool,
        (int X, int Y)? characterFallback = null) =>
        UsesX(kind) ? XResolutionTile(grab, tool) : IsCharacterKind(kind) && characterFallback is { } f ? f : grab;

    /// <summary>FIXB23 D-B20-3: the character A kinds (the farm pet, the horse, petting a farm animal).</summary>
    public static bool IsCharacterKind(string? kind) => kind is Pet or HorseMount or AnimalPet;

    /// <summary>FIXB23 D-B20-3 (live V2B20PET-a: ARTI tile-centred on the stand (54,9) facing up at Dudley (54,8); GetGrabTile =
    /// (54,9), ARTI's own tile -> SMAPI `no_chest_at_grab_tile:54,9`, no target offered -> `extras_stand_invalid`). Vanilla
    /// Game1.pressActionButton (decompiled 1.6.15 G1:11331-11421): when tryToCheckAt(grab) takes nothing, nothing is in hand
    /// (ActiveObject null) and no character is at the grab tile, it re-checks grab+(0,1) when the facing agrees (dot >= 0),
    /// then grab-(0,1) likewise, then the own tile. With grab = own tile the faced neighbour is reached facing up (the second
    /// probe) and facing down (the first probe) only; facing left / right the probes are the tiles below / above, never the
    /// faced neighbour (those stands keep the runtime's bounded flush step). <paramref name="ownTileInert"/>: the own tile
    /// takes nothing in the first check (mod-side read). null = no fallback: the press acts on the grab tile.</summary>
    public static (int X, int Y)? CharacterFallbackTile((int X, int Y) grab, (int X, int Y) own, int facing, bool ownTileInert) =>
        ownTileInert && grab == own && (facing == 0 || facing == 2) ? (own.X, own.Y + (facing == 0 ? -1 : 1)) : null;

    /// <summary>FIXB23 review: the own-tile scan behind CharacterFallbackTile only runs for a pose that can use it (grab = own
    /// tile, facing up / down).</summary>
    public static bool CharacterFallbackPose((int X, int Y) grab, (int X, int Y) own, int facing) =>
        grab == own && (facing == 0 || facing == 2);

    // ------------------------------------------------------------------ FIXB14 nav grid (D-B12-2..4)

    /// <summary>FIXB14: the published walkability window (ui.context "nav_grid"): NavGridSize x NavGridSize tiles whose
    /// origin snaps to NavGridAlign so the window (and the ui.context revision) changes only when ARTI crosses a block;
    /// ARTI is always at least NavGridMargin tiles from every edge.</summary>
    public const int NavGridSize = 40, NavGridAlign = 8, NavGridMargin = 16;

    /// <summary>One building as the walkability rule sees it: its footprint (tile origin, size), the extra tile-property
    /// radius Building.intersects inflates it by, and the game's own per-tile verdict (Building.isTilePassable: the
    /// building data's collision map, e.g. a cabin's door-front tile inside its footprint is walkable).</summary>
    public readonly record struct BuildingShape(int TileX, int TileY, int Wide, int High, int Radius, Func<int, int, bool> Passable)
    {
        public bool Covers(int x, int y) =>
            x >= this.TileX - this.Radius && x < this.TileX + this.Wide + this.Radius
            && y >= this.TileY - this.Radius && y < this.TileY + this.High + this.Radius;
    }

    /// <summary>FIXB16 review (D-B14-2, live V2B14CURFEW-b: the cabin's footprint rows y14-16 made the door-front tile (52,16)
    /// a '#' cell; the home leg oscillated at (52,17)): a tile is a building cell when ANY building covering it reports it
    /// not passable (Building.intersects, the collision the farmer really meets). A tile one building opens (its door
    /// front) stays blocked when another building covers it. The stands (ExtrasTileMiss) and the nav grid share this.</summary>
    public static bool BuildingBlocksTile(IEnumerable<BuildingShape> buildings, int x, int y)
    {
        foreach (var b in buildings)
            if (b.Covers(x, y) && !b.Passable(x, y))
                return true;
        return false;
    }

    /// <summary>FIXB16 review: every building cell of the nav window (BuildingBlocksTile for each covered window tile).</summary>
    public static HashSet<(int X, int Y)> BuildingBlockedCells(IReadOnlyList<BuildingShape> buildings, int x0, int y0, int size)
    {
        var cells = new HashSet<(int X, int Y)>();
        foreach (var b in buildings)
            for (int x = Math.Max(x0, b.TileX - b.Radius); x < Math.Min(x0 + size, b.TileX + b.Wide + b.Radius); x++)
                for (int y = Math.Max(y0, b.TileY - b.Radius); y < Math.Min(y0 + size, b.TileY + b.High + b.Radius); y++)
                    if (!cells.Contains((x, y)) && BuildingBlocksTile(buildings, x, y))
                        cells.Add((x, y));
        return cells;
    }

    /// <summary>FIXB16 review: a bed's sleep row (the bed tiles BedFurniture.IntersectsForCollision lets the farmer onto;
    /// stepping there starts the Sleep question). Walkable for the farmer, never for a route: nav cell 'z'.</summary>
    public const string SleepRowMiss = "bed_sleep_row";

    public static bool BedSleepTile(bool isBed, bool insideBox, bool intersectsForCollision) => isBed && insideBox && !intersectsForCollision;

    /// <summary>FIXB16 D-B14-4: a policy refusal of the faced extras target (stable while ARTI faces it) for ui.context
    /// "extras_refusal"; null for every other miss (position-dependent misses would churn the record).</summary>
    public static string? PolicyRefusal(string? miss) =>
        miss is not null && (miss.StartsWith(GProfession, StringComparison.Ordinal) || miss.StartsWith(GSkull, StringComparison.Ordinal)
            || miss.StartsWith(GInputPolicy, StringComparison.Ordinal) || miss.StartsWith("gift_refused:", StringComparison.Ordinal)
            || miss.StartsWith("quest_refused:", StringComparison.Ordinal))
            ? (miss.Length <= 60 ? miss : miss[..60]) : null;

    // S25 attempts need positional/facility misses too, scoped to the faced counter. W1 gap7t: S26's Marnie counter
    // likewise (own grab tile, or Marnie not at counter x / x-1, y-1 per GameLocation.animalShop -> facility_unavailable).
    public static string? CaptureRefusal(string? miss, string? kind) =>
        kind is Blacksmith or Kitchen or AnimalShop && miss is not null ? (miss.Length <= 60 ? miss : miss[..60]) : PolicyRefusal(miss);

    /// <summary>FIXB16 review MAJOR (a pet goal ended `pet_refused_profession_policy` while facing a ripe crop): the
    /// published refusal names the refused target (kind, capture source id, tile) so the runtime ends an objective only
    /// for a refusal of its own kind. Null without a policy refusal or without the refused capture.</summary>
    public static Dictionary<string, object>? RefusalRecord(string? policy, string? kind, string? source, int x, int y) =>
        policy is null || string.IsNullOrEmpty(kind) || string.IsNullOrEmpty(source) ? null
        : new Dictionary<string, object>
        {
            ["reason"] = policy, ["kind"] = kind, ["ref"] = source.Length <= 120 ? source : source[..120], ["x"] = x, ["y"] = y,
        };

    /// <summary>FIXB16 D-B14b-1: a furniture tile blocks only when the piece is not passable AND collides there.</summary>
    public static bool FurnitureBlocks(bool passable, bool intersectsForCollision) => !passable && intersectsForCollision;

    /// <summary>FIXB19 D-B16-1 (live V2B16PET-a/b: the stand (54,9) was published for Dudley at (54,8), the press there
    /// resolved `building_at_grab_tile`): the A kinds GameLocation.checkAction resolves AFTER its buildings step
    /// (characters, objects, terrain features). For them a target tile a building takes (door, or the building rule)
    /// can never be pressed from any side: no stand is published.</summary>
    public static bool AfterBuildingStep(string? kind) => kind is NpcGift or Pet or HorseMount or HayHopper or MachineCollect or ShakeTree
        or HarvestCrop or QuestDelivery;

    public const string StandBuildingMiss = "building_at_target_tile";

    /// <summary>FIXB19: the stand verdicts for one target: every candidate refused when the press admission's building
    /// rule takes the target tile, else the per-side walkability misses.</summary>
    public static (List<(int X, int Y)> Stands, List<(int X, int Y, string Miss)> Misses) StandsFor(
        string? kind, int x, int y, bool buildingTakesTarget, Func<int, int, string?> sideMiss) =>
        StandsFor(kind, x, y, buildingTakesTarget ? StandBuildingMiss : null, sideMiss);

    /// <summary>FIXB21 review: the same with the building step's own miss (`building_at_target_tile` /
    /// `building_under_construction`), null = not taken.</summary>
    public static (List<(int X, int Y)> Stands, List<(int X, int Y, string Miss)> Misses) StandsFor(
        string? kind, int x, int y, string? buildingMiss, Func<int, int, string?> sideMiss)
    {
        var stands = new List<(int X, int Y)>(4);
        var misses = new List<(int X, int Y, string Miss)>(4);
        foreach ((int sx, int sy) in StandCandidates(x, y))
        {
            string? miss = AfterBuildingStep(kind) && buildingMiss is not null ? buildingMiss : sideMiss(sx, sy);
            if (miss is null)
                stands.Add((sx, sy));
            else
                misses.Add((sx, sy, miss));
        }
        return (stands, misses);
    }

    public static int NavGridOrigin(int actor) => Math.Max(0, actor - Mod(actor, NavGridAlign) - NavGridMargin);

    static int Mod(int v, int m) => ((v % m) + m) % m;

    /// <summary>One nav grid cell: '.' walkable, 'x' a warp source tile (never routed through: a step would warp),
    /// 'z' a bed's sleep row (walkable, but a step there starts Sleep: only ever ARTI's own start tile), 's' a breakable
    /// stone (pickaxe), 'b' a resource clump (boulder / stump / log), 'w' water, '#' anything else that blocks (walls,
    /// buildings, other objects, trees, bushes, furniture).</summary>
    public static char NavChar(string? standMiss, bool breakableStone, bool warp = false) =>
        warp ? 'x' : standMiss is null ? '.' : standMiss == SleepRowMiss ? 'z' : breakableStone && standMiss == "object" ? 's'
        : standMiss == "resource_clump" ? 'b' : standMiss == "water" ? 'w' : '#';

    /// <summary>MAP_INDEPENDENCE_NAV: the published cell once the game's own farmer movement collision answered
    /// (<paramref name="walkable"/>; null = no native verdict). Warp 'x' and sleep-row 'z' describe automatic effects
    /// and are kept. Natively walkable is '.', whatever the tile class (a bridge / Paths overlay on a Back "Water"
    /// tile, a Buildings "Passable" tile). Natively blocked keeps its class ('w' real water, 's' stone, 'b' clump), and
    /// a legacy-open cell that the game refuses becomes '#'. No location, map or coordinate input.</summary>
    public static char NativeNavChar(char legacy, bool? walkable) =>
        legacy is 'x' or 'z' || walkable is null ? legacy : walkable.Value ? '.' : legacy == '.' ? '#' : legacy;

    /// <summary>MAP_INDEPENDENCE_NAV: the stand verdict under the same rule (null = walkable stand).</summary>
    public static string? NativeStandMiss(string? legacy, bool? walkable) =>
        legacy == SleepRowMiss || walkable is null ? legacy : walkable.Value ? null : legacy ?? "collision";

    /// <summary>FIXB14 review: the grid is rebuilt at most NavGridMinIntervalMs apart (<= 4 Hz) and only when its key
    /// (location, window origin, object / terrain / building / feature / clump / furniture counts) changed.</summary>
    public const long NavGridMinIntervalMs = 250;


    public static (int X, int Y)[] StandCandidates(int x, int y) => new[] { (x, y + 1), (x, y - 1), (x + 1, y), (x - 1, y) };

    /// <summary>FIXB13 D-B10-3: the first reason a candidate stand is not walkable, or null (walkable). Passable terrain
    /// (grass, hoed dirt, flooring) and passable objects never block.</summary>
    public static string? StandMiss(bool onMap, bool water, bool mapPassable, bool building, bool blockingObject,
        bool blockingTerrain, bool blockingBush, bool clump, bool blockingFurniture, bool sleepRow = false) =>
        !onMap ? "off_map" : water ? "water" : !mapPassable ? "map_not_passable" : building ? "building"
        : blockingObject ? "object" : blockingTerrain ? "terrain_feature" : blockingBush ? "bush" : clump ? "resource_clump"
        : blockingFurniture ? "furniture" : sleepRow ? SleepRowMiss : null;

    /// <summary>The press opens a menu receiver in the press frame (question / MineElevatorMenu).</summary>
    public static bool IsReceiver(string? kind) => kind is Tv or Mailbox or Shaft or Bus or Minecart or MineElevator or ShopCounter or ShippingBin or LadderUp or NpcGift or AnimalShop or Blacksmith or CommunityNote or Kitchen
        or QuestDelivery;

    /// <summary>The press warps (door machinery: expected location + LocationChanged).</summary>
    public static bool RequiresExpectedLocation(string? kind) => kind == LadderDown;

    /// <summary>Read-only map action label; ordinary generic ACTION owns input and menu readback.</summary>
    public static string? OrdinaryCounterKind(string? action) => action == "Gunther" ? "museum_counter" : null;

    /// <summary>Mine entrance Buildings action, not an inferred ladder coordinate.</summary>
    public static int? MineEntranceLevel(string? location, string[] action)
    {
        if (location != "Mine" || action.Length is < 1 or > 2 || action[0] is not ("Mine" or "NextMineLevel"))
            return null;
        return action.Length == 1 ? 1 : int.TryParse(action[1], out int level) && level > 0 ? level : null;
    }

    /// <summary>X with a tool held starts UsingTool after the press (milk pail / shears animation).</summary>
    public static bool ToolUseAfterPress(string? kind) => kind is AnimalMilk or AnimalShear;

    /// <summary>The effect turns / moves the farmer (FarmAnimal.pet faces the animal; mount / dismount jump; harvest anim).</summary>
    public static bool PoseMayChange(string? kind) => kind is NpcGift or AnimalPet or HorseMount or HorseDismount or HarvestCrop
        or QuestDelivery;

    public static string? QuestionKeyOf(string? kind) => kind switch
    {
        AnimalShop => "Marnie",
        Blacksmith => "Blacksmith",
        Tv => "TV",
        Shaft => "Shaft",
        LadderUp => "ExitMine",
        Bus => "Bus",
        Minecart => "pagedResponse",
        _ => null,
    };

    /// <summary>Question keys the runtime may answer under a G6 objective (ui_key stub until G3's contracts).</summary>
    public static readonly IReadOnlyList<string> QuestionKeys = new[] { "Shaft", "Bus", "pagedResponse", "ExitMine", "TV", "Blacksmith", "Marnie" };

    public static string OutcomeOf(string? kind) => kind switch
    {
        NpcGift => GiftGiven,
        QuestDelivery => QuestDelivered,
        Pet => Petted,
        AnimalPet => AnimalPetted,
        AnimalMilk or AnimalShear => ProductCollected,
        HorseMount => Mounted,
        HorseDismount => Dismounted,
        ShakeTree => TreeShaken,
        HarvestCrop => CropHarvested,
        MachineCollect => MachineCollected,
        MachineInsert => MachineLoaded,
        HayHopper => HayTaken,
        HayPlace => ItemPlaced,
        LadderDown => WorldInteractionKinds.LocationChanged,
        AnimalShop or Blacksmith or Tv or Shaft or Bus or Minecart or LadderUp => WorldInteractionKinds.QuestionOpened,
        CommunityNote or Mailbox or MineElevator or ShopCounter or ShippingBin or Kitchen => MenuOpened,
        PlaceItem => ItemPlaced,
        _ => BgHoldCompletion.InteractionUncertain,
    };

    // ------------------------------------------------------------------ policies

    /// <summary>WS-G5 mirror: never go deeper into the Mines after 20:00 (ReflexContracts.CombatLatestStartTime).</summary>
    public const int MinesLatestDescendTime = ReflexContracts.CombatLatestStartTime;
    /// <summary>WS-G5 mirror: 01:00 hard stop (pass-out at 02:00): no extras kind starts at or after it.</summary>
    public const int HardStopTime = ReflexContracts.CombatHardStopTime;
    /// <summary>FarmAnimal.pet: a standing animal at or after 19:00 only shows its sleep dialogue (FarmAnimal.cs:703).</summary>
    public const int AnimalSleepTime = 1900;
    public const int BusTicketPrice = 500;

    public static bool IsMinesDescend(string? kind) => kind is LadderDown or Shaft;

    // ------------------------------------------------------------------ descent safety (review MAJOR 2)

    /// <summary>
    /// MineShaft.enterMineShaft (MineShaft.cs:3012-3022) drops 3-15 levels and sets health = max(1, health - levels x 3):
    /// up to -45 HP. Shafts only exist in the Skull Cavern (mine level &gt; 120), where no combat reflex runs.
    /// </summary>
    public const int ShaftMaxDamage = 45;
    public const int ShaftHealthMargin = 10;
    /// <summary>Ladder / shaft: health at least half of max (a fresh level spawns monsters next to the ladder).</summary>
    public const int DescendHealthPercent = 50;
    /// <summary>Ladder / shaft: stamina floor (G1 stamina-shake threshold, Game1.cs pressUseToolButton: Stamina &lt;= 20).</summary>
    public const int DescendStaminaFloor = 20;
    /// <summary>Mine levels above 120 are the Skull Cavern (MineShaft.mineLevel 121+).</summary>
    public const int SkullCavernFirstLevel = 121;

    /// <summary>Health / stamina floor of a descent (null = allowed).</summary>
    public static string? DescentHealthRefusal(string? kind, int health, int maxHealth, float stamina)
    {
        if (!IsMinesDescend(kind))
            return null;
        if (maxHealth <= 0 || health * 100 < DescendHealthPercent * maxHealth)
            return "health_floor:half_max";
        if (kind == Shaft && health < ShaftMaxDamage + ShaftHealthMargin)
            return "health_floor:shaft_damage";
        if (stamina < DescendStaminaFloor)
            return "stamina_floor";
        return null;
    }

    /// <summary>Any descent into or inside the Skull Cavern needs the owner policy ExtrasAllowSkullCavern (default off).</summary>
    public static string? SkullCavernRefusal(string? kind, int targetLevel, bool allowSkullCavern) =>
        IsMinesDescend(kind) && targetLevel >= SkullCavernFirstLevel && !allowSkullCavern ? "skull_cavern_policy" : null;

    // Machine input policy (review MINOR 3): WorldExtrasPolicy.cs (kept apart: it needs the WS-G2 economy types).

    /// <summary>The time rule: null = allowed.</summary>
    public static string? TimeRefusal(string? kind, int timeOfDay)
    {
        if (timeOfDay >= HardStopTime)
            return "time_rule:hard_stop";
        if (IsMinesDescend(kind) && timeOfDay >= MinesLatestDescendTime)
            return "time_rule:mines_after_2000";
        if (kind == AnimalPet && timeOfDay >= AnimalSleepTime)
            return "time_rule:animal_sleeping";
        return null;
    }

    /// <summary>Elevator floor press (MineElevatorMenu): deeper floors only before 20:00, nothing after 01:00.</summary>
    public static string? ElevatorFloorRefusal(int floor, int currentLevel, int timeOfDay)
    {
        if (timeOfDay >= HardStopTime)
            return "time_rule:hard_stop";
        if (floor > 0 && timeOfDay >= MinesLatestDescendTime)
            return "time_rule:mines_after_2000";
        if (floor == currentLevel)
            return "same_floor";
        return null;
    }

    /// <summary>Farmer.getBaseExperienceForLevel (Farmer.cs:3121-3136).</summary>
    public static int BaseExperienceForLevel(int level) => level switch
    {
        1 => 100, 2 => 380, 3 => 770, 4 => 1300, 5 => 2150, 6 => 3300, 7 => 4800, 8 => 6900, 9 => 10000, 10 => 15000, _ => -1,
    };

    /// <summary>
    /// Profession-menu policy: an XP gain that crosses level 5 or 10 queues a profession chooser at night, which the
    /// runtime cannot answer (owner D8). Refused unless the owner policy allow_profession_menu is on
    /// (ModConfig.ExtrasAllowProfessionMenu). The live save (2026-10-08) sits five Farming XP short of level 5:
    /// every Farming-XP kind (harvest, animal pet / milk / shear, Farming-XP machines) is refused there.
    /// </summary>
    public static string? ProfessionRefusal(int experience, int gain, bool allowProfessionMenu)
    {
        if (allowProfessionMenu || gain <= 0)
            return null;
        foreach (int level in new[] { 5, 10 })
        {
            int threshold = BaseExperienceForLevel(level);
            if (experience < threshold && experience + gain >= threshold)
                return "profession_menu_policy:level_" + level;
        }
        return null;
    }

    /// <summary>Crop.harvest XP for one harvested item of base price <paramref name="price"/> (Crop.cs:691-694), +2 margin.</summary>
    public static int HarvestXpEstimate(int price) =>
        (int)Math.Round(16.0 * Math.Log(0.018 * Math.Max(0, price) + 1.0, Math.E)) + 2;

    // Live7 follow-up (Spring Onion): vanilla forage crops live in HoeDirt (Crop(forageCrop: true, which), e.g.
    // Forest.DayUpdate). Crop.harvest (1.6.15 Crop.cs:495-551) puts ONLY whichForageCrop "1" (Spring Onion, (O)399) into
    // the bag on A (+3 Foraging XP, quality from the foraging level / Botanist); "2" (Ginger) only shakes on A and is dug
    // with a hoe (Crop.hitWithHoe, (O)829), and HoeDirt.readyForHarvest excludes it. Any other forage id has no native
    // yield (Crop.harvest would dereference null): never offered. No tile or map is involved.
    public const string SpringOnionQid = "(O)399", GingerQid = "(O)829";
    public const int ForageCropXp = 3;
    /// <summary>Every quality Crop.harvest can give a forage crop (0, silver 1, gold 2, iridium 4 with Botanist).</summary>
    public static readonly int[] ForageCropQualities = { 0, 1, 2, 4 };

    /// <summary>The item an A press (HarvestMethod.Grab) puts in the bag for a forage crop, or null (never offered).</summary>
    public static string? ForageCropGrabQid(string? whichForageCrop) => whichForageCrop == "1" ? SpringOnionQid : null;

    /// <summary>The native yield of a forage crop by any means (A for Spring Onion, hoe for Ginger), or null.</summary>
    public static string? ForageCropYieldQid(string? whichForageCrop) =>
        ForageCropGrabQid(whichForageCrop) ?? (whichForageCrop == "2" ? GingerQid : null);

    /// <summary>The A harvest_crop offer: a ready crop with a grab harvest; a forage crop only with a native grab yield.</summary>
    public static bool CropHarvestOffered(bool readyForHarvest, bool grabMethod, bool forageCrop, string? whichForageCrop) =>
        readyForHarvest && grabMethod && (!forageCrop || ForageCropGrabQid(whichForageCrop) is not null);

    /// <summary>Farming XP of the animal kinds (FarmAnimal.pet :782, MilkPail/Shears DoFunction).</summary>
    public const int AnimalFarmingXp = 5;

    /// <summary>Parses Data/Machines ExperienceGainOnHarvest ("Farming 5 Mining 2") into the gain of one skill name.</summary>
    public static int MachineXpFor(string? experienceGainOnHarvest, string skill)
    {
        if (string.IsNullOrWhiteSpace(experienceGainOnHarvest))
            return 0;
        string[] parts = experienceGainOnHarvest.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        int total = 0;
        for (int i = 0; i + 1 < parts.Length; i += 2)
            if (string.Equals(parts[i], skill, StringComparison.OrdinalIgnoreCase) && int.TryParse(parts[i + 1], out int v))
                total += Math.Max(0, v);
        return total;
    }

    // ------------------------------------------------------------------ allowlists (owner D10 defaults)

    public const string HayHopperQid = "(BC)99";

    /// <summary>Object.CheckForActionOnFeedHopper (1.6.15 Object.cs:4553-4573): a house
    /// withdraws for its residents, capped by root stock and unfilled trough slots; elsewhere one.
    /// Invalid negative/overfull house facts fail closed rather than offering a negative stack.</summary>
    public static int HayWithdrawalCount(int stock, int? residents, int animalLimit, int troughHay)
    {
        if (stock <= 0) return 0;
        if (residents is null) return 1;
        if (residents < 0 || animalLimit < 0 || troughHay < 0 || troughHay >= animalLimit) return 0;
        return Math.Min(Math.Max(1, Math.Min(residents.Value, stock)), animalLimit - troughHay);
    }

    /// <summary>Machines whose ready output a plain-tool / empty-hand A collects (no menu, no choice).</summary>
    public static readonly IReadOnlyCollection<string> CollectMachines = new HashSet<string>(StringComparer.Ordinal)
    {
        "(BC)9", "(BC)10", "(BC)12", "(BC)13", "(BC)15", "(BC)16", "(BC)17", "(BC)19", "(BC)20", "(BC)21", "(BC)24",
        "(BC)25", "(BC)90", "(BC)105", "(BC)114", "(BC)154", "(BC)231", "(BC)264", "(BC)Dehydrator", "(BC)FishSmoker",
        "(BC)BaitMaker", "(BC)DeluxeWormBin", "(BC)MushroomLog",
    };

    /// <summary>Machines an X drop-in may load (owner drop-in list D10; the game's own probe decides the input).</summary>
    public static readonly IReadOnlyCollection<string> InsertMachines = new HashSet<string>(StringComparer.Ordinal)
    {
        "(BC)12", "(BC)13", "(BC)15", "(BC)16", "(BC)17", "(BC)19", "(BC)20", "(BC)24", "(BC)25", "(BC)90", "(BC)114",
        "(BC)Dehydrator", "(BC)FishSmoker", "(BC)BaitMaker",
    };

    /// <summary>Sprinklers (Object.IsSprinkler qualified ids) the place kind admits.</summary>
    public static readonly IReadOnlyCollection<string> PlaceSprinklers = new HashSet<string>(StringComparer.Ordinal)
        { "(O)599", "(O)621", "(O)645" };

    /// <summary>Chests the place kind admits (plain chest; big chest).</summary>
    public static readonly IReadOnlyCollection<string> PlaceChests = new HashSet<string>(StringComparer.Ordinal)
        { "(BC)130", "(BC)BigChest" };

    /// <summary>Seeds (category -74, not a sapling), an allowlisted sprinkler, or a chest.</summary>
    public static bool PlaceableAllowlisted(string? qid, int category, bool isSapling) =>
        qid is not null && !isSapling && (category == -74 || PlaceSprinklers.Contains(qid) || PlaceChests.Contains(qid));

    // ------------------------------------------------------------------ explicit per-kind guard lists

    public const string GEvent = "event_or_festival", GMounted = "mounted", GNotMounted = "not_mounted", GSitting = "sitting",
        GOwnTile = "grab_tile_is_own_tile", GTime = "time_rule", GHeldPlain = "held_item_not_plain_tool",
        GHeldPail = "held_item_not_milk_pail", GHeldShears = "held_item_not_shears", GHeldPlaceable = "held_item_not_placeable",
        GHeldObject = "held_item_not_object", GToolTile = "tool_tile_not_grab_tile", GOccupant = "occupant_at_tile",
        GAmbiguous = "target_ambiguous", GAlreadyDone = "already_done_today", GNotReady = "target_not_ready",
        GInventory = "inventory_cannot_accept", GFurniture = "furniture_at_tile", GObjectAtTile = "object_at_tile",
        GHarvestable = "harvestable_at_tile", GStow = "stowing_would_apply", GPlacement = "placement_invalid",
        GPlaceList = "placeable_not_allowlisted", GMachineList = "machine_not_allowlisted", GMachineBusy = "machine_busy",
        GInput = "input_not_accepted", GStamina = "stamina_low", GHorseRider = "horse_has_rider",
        GHorseUnnamed = "horse_unnamed", GHorseBusy = "horse_busy", GDismount = "dismount_tile_not_clear",
        GFacility = "facility_unavailable", GMoney = "insufficient_money", GBoxedIn = "boxed_in",
        GProfession = "profession_menu_policy", GScytheCrop = "harvest_needs_scythe", GHealth = "health_floor",
        GSkull = "skull_cavern_policy", GInputPolicy = "input_policy";

    static readonly string[] Common = { GEvent, GMounted, GSitting, GOwnTile, GTime };

    static string[] With(params string[] tail) => Common.Concat(tail).ToArray();

    /// <summary>The explicit guard list per kind, in evaluation order (the first failing guard is the refusal).</summary>
    public static readonly IReadOnlyDictionary<string, string[]> Guards = new Dictionary<string, string[]>(StringComparer.Ordinal)
    {
        [Tv] = With(GHeldPlain, GOccupant, GAmbiguous, GFacility),
        [Mailbox] = With(GHeldPlain, GOccupant, GFacility),
        [NpcGift] = With(GHeldObject, GOccupant, GAmbiguous, GInputPolicy),
        [QuestDelivery] = With(GHeldObject, GOccupant, GAmbiguous, GInputPolicy),
        [Pet] = With(GHeldPlain, GOccupant, GAmbiguous, GAlreadyDone),
        [AnimalPet] = With(GHeldPlain, GAmbiguous, GAlreadyDone, GProfession),
        [AnimalMilk] = With(GHeldPail, GToolTile, GOccupant, GObjectAtTile, GFurniture, GAmbiguous, GNotReady, GInventory, GStamina, GProfession),
        [AnimalShear] = With(GHeldShears, GToolTile, GOccupant, GObjectAtTile, GFurniture, GAmbiguous, GNotReady, GInventory, GStamina, GProfession),
        [HorseMount] = With(GHeldPlain, GOccupant, GAmbiguous, GHorseRider, GHorseUnnamed, GHorseBusy),
        [HorseDismount] = new[] { GEvent, GNotMounted, GOwnTile, GTime, GHeldPlain, GDismount },
        [ShakeTree] = With(GHeldPlain, GOccupant, GNotReady),
        // A checks the actual grab tile, including a walkable crop underfoot.
        // ADR 68: the shared own-tile fence hid it after an approach walked onto it.
        [HarvestCrop] = With(GHeldPlain, GOccupant, GNotReady, GScytheCrop, GInventory, GProfession)
            .Where(g => g != GOwnTile).ToArray(),
        [MachineCollect] = With(GHeldPlain, GOccupant, GMachineList, GNotReady, GInventory, GBoxedIn, GProfession),
        [MachineInsert] = With(GHeldObject, GInputPolicy, GToolTile, GOccupant, GFurniture, GMachineList, GMachineBusy, GStow, GInput),
        [HayHopper] = With(GHeldPlain, GOccupant, GFacility, GInventory, GBoxedIn),
        [HayPlace] = With(GHeldObject, GOccupant, GObjectAtTile, GPlacement),
        [LadderDown] = With(GHeldPlain, GOccupant, GSkull, GHealth),
        [Shaft] = With(GHeldPlain, GOccupant, GSkull, GHealth),
        [LadderUp] = With(GHeldPlain, GOccupant),
        [MineElevator] = With(GHeldPlain, GOccupant, GFacility),
        [AnimalShop] = With(GHeldPlain, GOccupant, GFacility),
        [Blacksmith] = With(GHeldPlain, GOccupant, GFacility),
        [CommunityNote] = With(GHeldPlain, GOccupant, GFacility),
        [Kitchen] = With(GHeldPlain, GOccupant, GFacility),
        [ShopCounter] = With(GHeldPlain, GOccupant, GFacility),
        // FIXB7 D-B5b-3: checkAction reaches the building before any held-item placement, so a held object is fine
        // (the item to ship is usually in hand); the common guards (own tile, mounted, event, time) still apply.
        [ShippingBin] = With(GOccupant, GFacility),
        [Bus] = With(GHeldPlain, GOccupant, GFacility, GMoney),
        [Minecart] = With(GHeldPlain, GOccupant, GFacility),
        [PlaceItem] = With(GHeldPlaceable, GPlaceList, GToolTile, GOccupant, GObjectAtTile, GFurniture, GHarvestable, GStow, GPlacement),
    };

    /// <summary>First failing guard of <paramref name="kind"/> (null = admitted); time / profession carry their detail.</summary>
    public static string? Miss(string kind, ExtrasGuardFacts f)
    {
        if (!Guards.TryGetValue(kind, out string[]? list))
            return "unknown_kind";
        foreach (string guard in list)
        {
            if (guard == GTime)
            {
                if (TimeRefusal(kind, f.TimeOfDay) is { } time)
                    return time;
                continue;
            }
            if (guard == GHealth)
            {
                if (DescentHealthRefusal(kind, f.Health, f.MaxHealth, f.Stamina) is { } health)
                    return health;
                continue;
            }
            if (guard == GSkull)
            {
                if (SkullCavernRefusal(kind, f.TargetMineLevel, f.AllowSkullCavern) is { } skull)
                    return skull;
                continue;
            }
            if (Failing(guard, f))
                return guard == GProfession && f.ProfessionDetail is { } detail ? detail
                    : guard == GInputPolicy && f.InputPolicyDetail is { } input ? input : guard;
        }
        return null;
    }

    public static bool PlainActionHand(string held, string? toolType) =>
        held is PadHeld.None or PadHeld.Tool && toolType is not ("MilkPail" or "Shears" or "Pan" or "Wand");

    static bool Failing(string guard, ExtrasGuardFacts f) => guard switch
    {
        GEvent => f.EventUp,
        GMounted => f.Mounted,
        GNotMounted => !f.Mounted,
        GSitting => f.Sitting,
        GOwnTile => f.GrabIsOwnTile,
        // Plain tool or empty hand: no object (gift / place / eat / use), no weapon (special), no rod / slingshot,
        // and none of the tools whose A / X meaning differs (pail / shears / pan / wand).
        GHeldPlain => !PlainActionHand(f.Held, f.HeldToolType),
        GHeldPail => f.HeldToolType != "MilkPail",
        GHeldShears => f.HeldToolType != "Shears",
        GHeldPlaceable => f.Held != PadHeld.Placeable,
        GHeldObject => f.Held is not (PadHeld.Object or PadHeld.Food or PadHeld.Placeable),
        GToolTile => !f.ToolTileIsGrabTile,
        GOccupant => f.OccupantAtTile,
        GAmbiguous => f.TargetCandidates != 1,
        GAlreadyDone => f.AlreadyDone,
        GNotReady => !f.TargetReady,
        GInventory => !f.InventoryAccepts,
        GFurniture => f.FurnitureAtTile,
        GObjectAtTile => f.ObjectAtTile,
        GHarvestable => f.HarvestableAtTile,
        GStow => f.StowHazard,
        GPlacement => !f.PlacementValid,
        GPlaceList => !f.PlaceableAllowlisted,
        GMachineList => !f.MachineAllowlisted,
        GMachineBusy => f.MachineBusy,
        GInput => !f.InputAccepted,
        GStamina => f.StaminaLow,
        GHorseRider => f.HorseHasRider,
        GHorseUnnamed => f.HorseUnnamed,
        GHorseBusy => f.HorseBusy,
        GDismount => !f.DismountTileClear,
        GFacility => f.FacilityUnavailable,
        GMoney => f.Money < BusTicketPrice,
        GBoxedIn => f.BoxedIn,
        GProfession => f.ProfessionDetail is not null,
        GScytheCrop => f.ScytheCrop,
        GInputPolicy => f.InputPolicyDetail is not null,
        _ => true,
    };
}

/// <summary>Read-only guard facts of one candidate target (sampled by ModEntry.Extras.cs; defaults = permissive).</summary>
public sealed record ExtrasGuardFacts(
    string Held = PadHeld.None,
    string HeldToolType = "",
    bool EventUp = false,
    bool Mounted = false,
    bool Sitting = false,
    bool GrabIsOwnTile = false,
    int TimeOfDay = 900,
    bool ToolTileIsGrabTile = true,
    bool OccupantAtTile = false,
    int TargetCandidates = 1,
    bool AlreadyDone = false,
    bool TargetReady = true,
    bool InventoryAccepts = true,
    bool FurnitureAtTile = false,
    bool ObjectAtTile = false,
    bool HarvestableAtTile = false,
    bool StowHazard = false,
    bool PlacementValid = true,
    bool PlaceableAllowlisted = true,
    bool MachineAllowlisted = true,
    bool MachineBusy = false,
    bool InputAccepted = true,
    bool StaminaLow = false,
    bool HorseHasRider = false,
    bool HorseUnnamed = false,
    bool HorseBusy = false,
    bool DismountTileClear = true,
    bool FacilityUnavailable = false,
    int Money = 0,
    bool BoxedIn = false,
    string? ProfessionDetail = null,
    bool ScytheCrop = false,
    // Review MAJOR 2: descent safety (defaults: full health / stamina, a regular mine level, no Skull Cavern policy).
    int Health = 100,
    int MaxHealth = 100,
    float Stamina = 270f,
    int TargetMineLevel = 1,
    bool AllowSkullCavern = false,
    // Review MINOR 3: the held machine input refused by InsertPolicyRefusal (null = admitted).
    string? InputPolicyDetail = null);

/// <summary>
/// Per-kind readback (sampled before the press and every settle tick). <see cref="Done"/> = the kind's own state flag
/// (petted today, wasPet, produce gone, mounted / dismounted, seed / fruit gone, crop no longer ready, machine output
/// gone / loaded, object placed); <see cref="Count"/> = inventory count of the kind's item (product / harvest / output /
/// input / hay / placed item); <see cref="Value"/> = a kind detail (pet friendship, pieces of hay).
/// </summary>
public sealed record BgExtraFacts(string Kind, bool Done, int Count, int Value = 0, string ItemId = "", int? GiftsThisWeek = null,
    int? ProductQuality = null, int? ProductQuantity = null);

/// <summary>Settle + classification of the effect kinds (receiver kinds: <see cref="ClassifyReceiver"/>).</summary>
public static class BgExtrasEffect
{
    public const string EffectMismatch = "EXTRAS_EFFECT_MISMATCH";
    /// <summary>Mount / dismount (horse mutex round trip + jump), milk / shear (tool animation) settle longer.</summary>
    public const long SlowWaitMs = 3000;

    public static long WaitMs(string? kind) => kind is WorldExtras.HorseMount or WorldExtras.HorseDismount
        or WorldExtras.AnimalMilk or WorldExtras.AnimalShear ? SlowWaitMs : BgHoldCompletion.MenuWaitMs;

    /// <summary>+1: the kind adds its item; -1: it consumes the held item; 0: no inventory change allowed; 2: ignored.</summary>
    public static int CountSign(string? kind) => kind switch
    {
        WorldExtras.AnimalMilk or WorldExtras.AnimalShear or WorldExtras.HarvestCrop or WorldExtras.MachineCollect
            or WorldExtras.HayHopper => 1,
        WorldExtras.MachineInsert or WorldExtras.PlaceItem or WorldExtras.HayPlace => -1,
        WorldExtras.ShakeTree => 2, // the seed / fruit drops as debris (not picked up by the press)
        _ => 0,
    };

    static bool Succeeded(BgExtraFacts before, BgExtraFacts now)
    {
        if (CrabPotRule.Strict(before))
            return CrabPotRule.Succeeded(before, now); // W1 gap B3: exact crab-pot subtype readback
        int delta = now.Count - before.Count;
        if (now.Kind is WorldExtras.AnimalMilk or WorldExtras.AnimalShear && before.ProductQuantity is int quantity)
            return now.Done && !before.Done && before.ItemId == now.ItemId && before.ProductQuality == now.ProductQuality
                && now.ProductQuantity == quantity && quantity > 0 && delta == quantity;
        if (now.Kind == WorldExtras.HayPlace)
            return now.ItemId == "(O)178" && before.ItemId == now.ItemId
                && AnimalRoutine.HayPlaced(before.Done, now.Done, before.Count, now.Count);
        if (now.Kind == WorldExtras.HayHopper)
            return delta >= 1 && before.ItemId == "(O)178" && now.ItemId == before.ItemId
                && now.Value >= 0 && before.Value - now.Value == delta;
        if (!now.Done || before.Done)
            return false;
        return CountSign(now.Kind) switch
        {
            1 => delta >= 1,
            -1 => delta <= -1,
            0 => delta == 0,
            _ => true,
        };
    }

    public static bool Settled(BgActionEffectFacts before, BgActionEffectFacts now, long sinceReleaseMs)
    {
        if (now.MenuOpen || !string.Equals(now.LocationToken, before.LocationToken, StringComparison.Ordinal))
            return true;
        if (before.Extra is { } b && now.Extra is { } n && Succeeded(b, n))
            return true;
        return sinceReleaseMs >= WaitMs(now.Extra?.Kind ?? before.Extra?.Kind);
    }

    public static BgActionEffectVerdict Classify(BgActionEffectFacts? before, BgActionEffectFacts? now)
    {
        if (before?.Extra is not { } b || now?.Extra is not { } n || !string.Equals(b.Kind, n.Kind, StringComparison.Ordinal))
            return Uncertain(BgActionEffect.EffectUnread);
        if (!string.Equals(now.LocationToken, before.LocationToken, StringComparison.Ordinal))
            return Uncertain(BgActionEffect.UnexpectedLocation + now.LocationToken);
        if (now.MenuOpen)
            return Uncertain(BgActionEffect.UnexpectedMenu + (now.MenuType ?? "unknown"));
        int delta = n.Count - b.Count;
        if (n.Kind is WorldExtras.AnimalMilk or WorldExtras.AnimalShear && b.ProductQuantity is not null
            && (n.ItemId != b.ItemId || n.ProductQuality != b.ProductQuality || n.ProductQuantity != b.ProductQuantity))
            return Uncertain(EffectMismatch);
        if (Succeeded(b, n))
            return new BgActionEffectVerdict(WorldExtras.OutcomeOf(n.Kind), true, null, null, delta);
        if (CrabPotRule.Strict(b))
            return CrabPotRule.Unchanged(b, n) ? new BgActionEffectVerdict(BgActionEffect.InteractionNoEffect, false, null, null, 0)
                : Uncertain(EffectMismatch);
        bool unchanged = n.Kind == WorldExtras.HayHopper ? delta == 0 && n.Value == b.Value
            : n.Done == b.Done && (delta == 0 || CountSign(n.Kind) == 2);
        return unchanged ? new BgActionEffectVerdict(BgActionEffect.InteractionNoEffect, false, null, null, 0) : Uncertain(EffectMismatch);
    }

    /// <summary>Receiver kinds (shaft / bus / minecart question, MineElevatorMenu): opened in the press frame.</summary>
    public static bool SettledReceiver(BgActionEffectFacts now, long sinceReleaseMs) =>
        now.MenuOpen || sinceReleaseMs >= BgActionEffect.NoTransitionMs;

    public static BgActionEffectVerdict ClassifyReceiver(BgActionEffectFacts? before, BgActionEffectFacts? now)
    {
        if (before is null || now is null)
            return Uncertain(BgActionEffect.EffectUnread);
        if (!string.Equals(now.LocationToken, before.LocationToken, StringComparison.Ordinal))
            return Uncertain(BgActionEffect.UnexpectedLocation + now.LocationToken);
        string kind = now.Extra?.Kind ?? before.Extra?.Kind ?? "";
        if (kind == WorldExtras.NpcGift)
            return BgGiftEffect.Classify(before, now);
        if (kind == WorldExtras.QuestDelivery)
            return QuestDeliveryEffect.Classify(before, now); // W1 gap B4 (Protocol/QuestFacts.cs)
        if (now.MenuOpen)
            return now.ExpectedReceiver
                ? new BgActionEffectVerdict(WorldExtras.OutcomeOf(kind), true, null, null, 0)
                : Uncertain(BgActionEffect.UnexpectedMenu + (now.MenuType ?? "unknown"));
        return new BgActionEffectVerdict(BgActionEffect.InteractionNoEffect, false, null, null, 0);
    }

    /// <summary>Verdict EffectDetail: "kind|item|delta|value" (Python arti_stardew_world_extras.parse_effect_detail).</summary>
    public static string Detail(BgExtraFacts? now, int delta)
    {
        if (now is null)
            return "";
        string item = now.ItemId.Replace("|", "_");
        return now.Kind + "|" + item + "|" + delta.ToString(System.Globalization.CultureInfo.InvariantCulture) + "|"
            + now.Value.ToString(System.Globalization.CultureInfo.InvariantCulture);
    }

    static BgActionEffectVerdict Uncertain(string reason) => new(BgHoldCompletion.InteractionUncertain, false, reason, null, 0);
}

/// <summary>
/// Fix D-B2-1 (live batch 2, 2026-10-08): whether a shop counter tile action would open its ShopMenu right now (pure; the
/// mod samples the facts). Decomp 1.6.15 GameLocation.performAction: "Buy General" in the SeedShop (HandleBuyAction,
/// GameLocation.cs:10750-10770: player tile Y >= counter Y, Pierre inside (4,17,1,1) with Y <= player Y - 1, else the
/// island money box); "OpenShop shopId [dir open close x y w h]" (:9061-9110: direction rule, open / close time, owner in
/// the area when one is given). Anything else is not a counter. A closed / unowned counter is refused with zero presses
/// (FacilityUnavailable) so the press never shows the "closed" dialogue.
/// </summary>
public static class ShopCounterRule
{
    /// <summary>FIXB26 D-B23-1: <paramref name="WillyAbove"/> = Willy is in the FishShop with tile Y &lt;= player Y - 1.</summary>
    public sealed record Facts(string[] Action, int PlayerX, int PlayerY, int TileX, int TileY, int TimeOfDay, bool SeedShop,
        (int X, int Y)? PierreTile, bool OwnerInArea, bool WillyAbove = false, bool AnimalOwnerPresent = false);

    public static bool? OwnerPresent(Facts f) => f.Action[0] == "AnimalShop" ? f.AnimalOwnerPresent
        : f.Action[0] == "Buy" && f.Action[1] == "Fish" ? f.WillyAbove
        : f.Action[0] == "Buy" ? Closed(f with { PlayerY = f.TileY + 1 }) is null
        : f.Action.Length > 8 ? f.OwnerInArea : null;

    public static Dictionary<string, object?> Availability(Facts f, bool openNow)
    {
        string[] a = f.Action;
        int? open = a[0] == "OpenShop" && a.Length > 3 && int.TryParse(a[3], out int o) ? o : null;
        int? close = a[0] == "OpenShop" && a.Length > 4 && int.TryParse(a[4], out int c) ? c : null;
        return new() { ["open_now"] = openNow, ["opens_at"] = open, ["closes_at"] = close,
            ["closed_today"] = null, ["shopkeeper_present"] = OwnerPresent(f),
            ["hours_basis"] = open is null ? "unknown in native counter action" : "native counter action" };
    }

    /// <summary>The counter's shop id, or null when the tile action is not a supported counter. FIXB26 D-B23-1 (live
    /// V2B23SELL-a: Willy's counter offered no target): "Buy Fish" (GameLocation.HandleBuyAction, GameLocation.cs:10753-10757:
    /// Utility.TryOpenShopMenu("FishShop", this, null, maxOwnerY = player Y - 1)).</summary>
    public static string? ShopId(string[] action) => action.Length >= 2 && action[0] == "Buy" && action[1] == "General" ? "SeedShop"
        : action.Length == 1 && action[0] == "AnimalShop" ? "AnimalShop"
        : action.Length >= 2 && action[0] == "Buy" && action[1] == "Fish" ? "FishShop"
        : action.Length >= 2 && action[0] == "OpenShop" && action[1].Length > 0 ? action[1] : null;

    /// <summary>Map-data meaning only. Entry hours never assert that a counter's owner is available.</summary>
    public static string? Meaning(string[] action)
    {
        if (ShopId(action) is not { } id) return null;
        string hours = action[0] == "OpenShop" && action.Length > 4
            && int.TryParse(action[3], out int opening) && int.TryParse(action[4], out int closing)
            ? "native hours " + opening + "-" + closing : "counter hours unknown in map action";
        return "shop counter " + id + "; " + hours
            + "; owner availability checked locally; stock/prices in the open menu";
    }

    /// <summary>null = the counter opens now; else the refusal detail.</summary>
    public static string? Closed(Facts f)
    {
        string[] a = f.Action;
        if (ShopId(a) is null)
            return "not_a_counter";
        if (a[0] == "AnimalShop")
            return f.AnimalOwnerPresent ? null : "owner_absent";
        if (a[0] == "Buy" && a[1] == "Fish")
        {
            // performAction "Buy" (GameLocation.cs:9882-9893): the player at or below the counter row; then the owner Willy
            // standing above the player (maxOwnerY), else the closed message.
            if (f.PlayerY < f.TileY)
                return "direction";
            return f.WillyAbove ? null : "owner_absent";
        }
        if (a[0] == "Buy")
        {
            if (!f.SeedShop)
                return "buy_general_outside_seedshop";
            if (f.PlayerY < f.TileY)
                return "direction";
            return f.PierreTile is { } p && p.X == 4 && p.Y == 17 && p.Y <= f.PlayerY - 1 ? null : "owner_absent";
        }
        string dir = a.Length > 2 ? a[2] : "";
        bool dirOk = dir switch
        {
            "down" => f.PlayerY >= f.TileY,
            "up" => f.PlayerY <= f.TileY,
            "left" => f.PlayerX <= f.TileX,
            "right" => f.PlayerX >= f.TileX,
            _ => true,
        };
        if (!dirOk)
            return "direction";
        int open = a.Length > 3 && int.TryParse(a[3], out int o) ? o : -1;
        int close = a.Length > 4 && int.TryParse(a[4], out int c) ? c : -1;
        if ((open >= 0 && f.TimeOfDay < open) || (close >= 0 && f.TimeOfDay >= close))
            return "shop_hours";
        bool area = a.Length > 8;
        return area && !f.OwnerInArea ? "owner_absent" : null;
    }
}

/// <summary>FIXB23 review: the character-fallback own-tile scan (ExtrasOwnTileInert: characters, farmers, animals, objects,
/// furniture, clumps, buildings, terrain, Action tile) is not re-run every capture: only when the pose key (grab tile, own
/// tile, facing, location, hand state) changed, else at most every <see cref="RescanTicks"/> ticks; between scans the
/// previous verdict is reused. Pure; one instance per mod.</summary>
public sealed class CharacterFallbackThrottle
{
    public const int RescanTicks = 10;

    (int, int, int, int, int, string?, bool)? key;
    long lastTick;

    /// <summary>Scans performed so far (diagnostic / tests).</summary>
    public int Scans { get; private set; }

    public bool Due((int X, int Y) grab, (int X, int Y) own, int facing, string? location, bool handFree, long tick)
    {
        var k = (grab.X, grab.Y, own.X, own.Y, facing, location, handFree);
        if (this.key == k && tick >= this.lastTick && tick - this.lastTick < RescanTicks)
            return false;
        this.key = k;
        this.lastTick = tick;
        this.Scans++;
        return true;
    }
}
