using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Tools;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    // Only existing Farm animal houses: bounded read-only native state, under ui.context's actor/session envelope.
    // Counts cover every resident, including residents temporarily outdoors. No truncated list can certify completion.
    void EnrichAnimalRoutine(Dictionary<string, object> snapshot, Farmer player, GameLocation location)
    {
        if (!this.ExtrasEnabled)
            return;
        var farm = Game1.getFarm();
        var buildings = farm.buildings.Where(b => b.GetIndoors() is AnimalHouse).ToList();
        bool complete = buildings.Count <= AnimalRoutine.HousesMax;
        var houses = new List<object>();
        var targets = new List<Dictionary<string, object>>();
        var currentAnimals = new List<object>();
        var currentTroughs = new List<object>();
        int residentsSeen = 0;
        int productsSeen = 0;
        foreach (var b in buildings.Take(AnimalRoutine.HousesMax))
        {
            var house = (AnimalHouse)b.GetIndoors();
            string loc = SceneIdentity.Token(house.NameOrUniqueName);
            bool here = ReferenceEquals(location, house);
            bool known = house.animalsThatLiveHere.Count <= AnimalRoutine.AnimalsMax
                && house.animals.Length <= AnimalRoutine.AnimalsMax && house.objects.Length <= 256
                && house.map is not null && house.map.Layers[0]!.LayerWidth <= 64 && house.map.Layers[0]!.LayerHeight <= 64;
            known = known && house.animals.Keys.All(id => house.animalsThatLiveHere.Contains(id));
            int allowance = Math.Max(0, AnimalRoutine.AnimalsMax - residentsSeen);
            known &= house.animalsThatLiveHere.Count <= allowance;
            int unpetted = 0, ready = 0, missing = 0, insideUnpetted = 0, insideReady = 0;
            var roster = new List<object>();
            var products = new Dictionary<string, int>(StringComparer.Ordinal);
            foreach (long id in house.animalsThatLiveHere.Take(allowance))
            {
                residentsSeen++;
                FarmAnimal? a = house.animals.TryGetValue(id, out FarmAnimal? inside) ? inside
                    : farm.animals.TryGetValue(id, out FarmAnimal? outside) ? outside : null;
                if (a is null) { missing++; continue; }
                string? tool = a.GetAnimalData()?.HarvestTool;
                bool productReady = a.currentProduce.Value is not null && a.isAdult()
                    && (tool == "Milk Pail" ? a.CanGetProduceWithTool(new MilkPail())
                        : tool == "Shears" && a.CanGetProduceWithTool(new Shears()));
                string instance = this.animalTargetInstances.Single(a);
                roster.Add(new object[] { id.ToString(), instance });
                if (!a.wasPet.Value) unpetted++;
                if (productReady) ready++;
                if (productReady)
                {
                    string qid = ItemRegistry.QualifyItemId(a.currentProduce.Value)!;
                    products[qid] = products.GetValueOrDefault(qid) + (a.hasEatenAnimalCracker.Value ? 2 : 1);
                }
                bool inHouse = ReferenceEquals(a, inside);
                if (inHouse && !a.wasPet.Value) insideUnpetted++;
                if (inHouse && productReady) insideReady++;
                if ((here && inHouse) || (ReferenceEquals(location, farm) && !inHouse))
                {
                    string reference = "xt:animal:" + SceneIdentity.Token(location.NameOrUniqueName) + ":" + id;
                    currentAnimals.Add(new Dictionary<string, object>
                    {
                        ["id"] = id.ToString(), ["instance"] = instance, ["name"] = a.displayName ?? a.Name,
                        ["tile"] = new[] { a.TilePoint.X, a.TilePoint.Y }, ["wasPet"] = a.wasPet.Value,
                        ["ready"] = productReady, ["tool"] = tool ?? "", ["product"] = a.currentProduce.Value ?? "",
                    });
                    AddTarget(WorldExtras.AnimalPet, reference, a.TilePoint.X, a.TilePoint.Y, !a.wasPet.Value, instance);
                    if (productReady)
                        AddTarget(tool == "Shears" ? WorldExtras.AnimalShear : WorldExtras.AnimalMilk,
                            reference + ":product", a.TilePoint.X, a.TilePoint.Y, true, instance,
                            player.couldInventoryAcceptThisItem(AnimalProduct(a)) ? null : WorldExtras.GInventory);
                }
            }
            int empty = 0, filled = 0, blocked = 0, floor = 0;
            if (known)
            {
                for (int y = 0; y < house.map!.Layers[0].LayerHeight; y++)
                    for (int x = 0; x < house.map.Layers[0].LayerWidth; x++)
                    {
                        if (house.doesTileHaveProperty(x, y, "Trough", "Back") is null) continue;
                        bool occupied = house.objects.TryGetValue(new Vector2(x, y), out SObject? o);
                        bool hay = o?.QualifiedItemId == "(O)178" && o.Stack == 1;
                        if (!occupied) empty++; else if (hay) filled++; else blocked++;
                        if (here)
                        {
                            currentTroughs.Add(new Dictionary<string, object> { ["tile"] = new[] { x, y }, ["filled"] = hay, ["occupied"] = occupied });
                            if (!occupied) AddTarget(WorldExtras.HayPlace, "xt:trough:" + loc + ":" + x + "," + y, x, y, true, "");
                        }
                    }
                foreach (var pair in house.objects.Pairs)
                {
                    var o = pair.Value;
                    if (o is null) continue;
                    if (AnimalProductPickup.IsProductId(o.QualifiedItemId))
                    {
                        floor++;
                        productsSeen++;
                        products[o.QualifiedItemId] = products.GetValueOrDefault(o.QualifiedItemId) + o.Stack;
                        if (!IsLooseAnimalProduct(house, o)) known = false;
                        if (here && IsLooseAnimalProduct(house, o))
                            AddTarget(WorldInteractionKinds.Forage, SceneIdentity.ObjectFactId(house.NameOrUniqueName,
                                (int)pair.Key.X, (int)pair.Key.Y, o.QualifiedItemId), (int)pair.Key.X, (int)pair.Key.Y, true,
                                this.animalTargetInstances.Single(o), player.couldInventoryAcceptThisItem(o) ? null : WorldExtras.GInventory);
                    }
                    if (here && o.QualifiedItemId == WorldExtras.HayHopperQid)
                        AddTarget(WorldExtras.HayHopper, "xt:obj:" + loc + ":" + (int)pair.Key.X + "," + (int)pair.Key.Y,
                            (int)pair.Key.X, (int)pair.Key.Y, ExtrasHayWithdrawal(house) > 0,
                            this.animalTargetInstances.Single(o), ExtrasHayWithdrawal(house) > 0
                                && (player.freeSpotsInInventory() <= 0 || !player.couldInventoryAcceptThisItem("(O)178", ExtrasHayWithdrawal(house)))
                                ? WorldExtras.GInventory : null);
                }
            }
            known &= missing == 0 && empty + filled + blocked <= AnimalRoutine.TroughsMax && productsSeen <= AnimalRoutine.ProductsMax;
            complete &= known;
            houses.Add(new Dictionary<string, object>
            {
                ["location"] = loc, ["instance"] = this.animalTargetInstances.Single(house),
                ["type"] = b.buildingType.Value, ["roster"] = roster.ToArray(), ["known"] = known,
                ["unpetted"] = unpetted, ["ready"] = ready, ["floor"] = floor,
                ["inside_unpetted"] = insideUnpetted, ["inside_ready"] = insideReady,
                ["products"] = products.Select(p => new Dictionary<string, object> { ["qid"] = p.Key, ["count"] = p.Value }).ToArray(),
                ["empty"] = empty, ["filled"] = filled, ["blocked"] = blocked, ["missing"] = missing,
                ["hay_stock"] = house.GetRootLocation().piecesOfHay.Value,
                ["door"] = new[] { b.tileX.Value + b.humanDoor.Value.X, b.tileY.Value + b.humanDoor.Value.Y },
                ["door_ref"] = SceneIdentity.BuildingDoorFactId(farm.NameOrUniqueName,
                    b.tileX.Value + b.humanDoor.Value.X, b.tileY.Value + b.humanDoor.Value.Y, house.NameOrUniqueName),
                ["enterable"] = b.daysOfConstructionLeft.Value <= 0 && !b.isMoving && !player.team.demolishLock.IsLocked(),
            });
        }
        snapshot["animal_routine"] = new Dictionary<string, object>
        {
            ["complete"] = complete && targets.Count <= 256, ["location"] = SceneIdentity.Token(location.NameOrUniqueName),
            ["location_name"] = location.Name,
            ["date"] = new object[] { Game1.year, Game1.currentSeason, Game1.dayOfMonth },
            ["hay_inventory"] = InventoryCount(player, "(O)178"),
            ["houses"] = houses.ToArray(), ["animals"] = currentAnimals.ToArray(),
            ["troughs"] = currentTroughs.Take(AnimalRoutine.TroughsMax).ToArray(),
            ["targets"] = targets.Take(256).ToArray(),
        };

        this.EnrichHouseTravel((Dictionary<string, object>)snapshot["animal_routine"]);

        void AddTarget(string kind, string reference, int x, int y, bool ready, string instance, string? refusal = null)
        {
            var (stands, misses) = ExtrasStandTiles(location, kind, x, y);
            targets.Add(new Dictionary<string, object>
            {
                ["kind"] = kind, ["ref"] = reference, ["x"] = x, ["y"] = y, ["ready"] = ready,
                ["instance"] = instance, ["stands"] = stands, ["stand_misses"] = misses,
            });
            if (refusal is not null) targets[^1]["refusal"] = refusal;
        }
    }
}
