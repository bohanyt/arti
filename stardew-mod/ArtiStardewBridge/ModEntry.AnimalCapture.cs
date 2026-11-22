using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Tools;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    readonly GiftTargetInstances animalTargetInstances = new();
    static SObject AnimalProduct(FarmAnimal a) => ItemRegistry.Create<SObject>(
        ItemRegistry.QualifyItemId(a.currentProduce.Value)!, a.hasEatenAnimalCracker.Value ? 2 : 1, a.produceQuality.Value);

    // The binding includes the current Farm and building association, not just a same-name location.
    static Func<bool> PinAnimalLocation(Farmer player, GameLocation location)
    {
        var farm = Game1.getFarm();
        var building = location.ParentBuilding;
        return () => ReferenceEquals(Game1.player, player) && ReferenceEquals(player.currentLocation, location)
            && ReferenceEquals(Game1.currentLocation, location) && ReferenceEquals(Game1.getFarm(), farm)
            && (location is AnimalHouse
                ? building is not null && ReferenceEquals(location.ParentBuilding, building)
                    && farm.buildings.Contains(building) && ReferenceEquals(building.GetIndoors(), location)
                : ReferenceEquals(location, farm));
    }

    static bool AnimalStillHere(GameLocation location, FarmAnimal animal)
    {
        if (!location.animals.TryGetValue(animal.myID.Value, out FarmAnimal? now) || !ReferenceEquals(now, animal))
            throw new InvalidOperationException("animal_instance_changed");
        return true;
    }

    static AnimalProductFingerprint AnimalFingerprint(FarmAnimal animal) => new(
        ItemRegistry.QualifyItemId(animal.currentProduce.Value) ?? "",
        animal.produceQuality.Value, animal.hasEatenAnimalCracker.Value ? 2 : 1);

    static int AnimalInventoryCount(Farmer player, AnimalProductFingerprint product) => player.Items
        .Where(i => i is SObject o && o.QualifiedItemId == product.Qid && o.Quality == product.Quality).Sum(i => i!.Stack);

    InteractKindCapture? CaptureHayPlace(Farmer player, GameLocation location, Vector2 grab, ExtrasGuardFacts facts)
    {
        if (location is not AnimalHouse || player.ActiveObject?.QualifiedItemId != "(O)178"
            || location.doesTileHaveProperty((int)grab.X, (int)grab.Y, "Trough", "Back") is null)
            return null;
        bool occupied = location.objects.ContainsKey(grab);
        bool occupant = ExtrasOccupant(player, location, TileRect(grab), null) || ExtrasAnimalAt(location, TileRect(grab), null);
        string? miss = AnimalRoutine.HayPlaceMiss(true, true, occupied, player.ActiveObject.QualifiedItemId,
            facts.EventUp, facts.GrabIsOwnTile, facts.Mounted, facts.Sitting, occupant, facts.TimeOfDay);
        if (miss is not null)
            return InteractKindCapture.Missed(miss);
        facts = facts with { PlacementValid = true, OccupantAtTile = occupant, ObjectAtTile = occupied };
        return this.ExtrasCapture(WorldExtras.HayPlace, player, location, grab, facts, location, "(O)178", null,
            () => new BgExtraFacts(WorldExtras.HayPlace,
                location.objects.TryGetValue(grab, out SObject? hay) && hay.QualifiedItemId == "(O)178" && hay.Stack == 1,
                InventoryCount(player, "(O)178"), 0, "(O)178"));
    }

    InteractKindCapture? CaptureAnimalTool(Farmer player, GameLocation location, Vector2 grab, ExtrasGuardFacts facts)
    {
        Tool tool = (Tool)player.CurrentItem!;
        string kind = tool is MilkPail ? WorldExtras.AnimalMilk : WorldExtras.AnimalShear;
        Vector2 toolPoint = player.GetToolLocation(true);
        Rectangle toolRect = new((int)toolPoint.X - 32, (int)toolPoint.Y - 32, 64, 64);
        FarmAnimal? animal = Utility.GetBestHarvestableFarmAnimal(location.animals.Values, tool, toolRect);
        if (animal is null)
            return null; // nothing to milk / shear here: the v1 kinds may still apply with this tool
        string? produce = animal.currentProduce.Value;
        Rectangle rect = TileRect(grab);
        facts = facts with
        {
            OccupantAtTile = ExtrasOccupant(player, location, toolRect, null) || ExtrasOccupant(player, location, rect, null),
            ObjectAtTile = location.objects.ContainsKey(grab),
            FurnitureAtTile = ExtrasFurnitureAt(location, rect),
            TargetCandidates = 1,
            TargetReady = produce is not null && animal.isAdult() && animal.CanGetProduceWithTool(tool),
            InventoryAccepts = produce is not null && player.couldInventoryAcceptThisItem(AnimalProduct(animal)),
            StaminaLow = player.Stamina <= 4f,
            ProfessionDetail = this.ExtrasProfession(player, Farmer.farmingSkill, WorldExtras.AnimalFarmingXp),
        };
        FarmAnimal pinned = animal;
        AnimalProductFingerprint product = AnimalFingerprint(animal);
        int productBaseline = AnimalInventoryCount(player, product);
        return this.ExtrasCapture(kind, player, location, grab, facts, animal, animal.myID.Value.ToString(), null,
            () =>
            {
                AnimalStillHere(location, pinned);
                // Produce disappears on collection; quality/cracker state remains native and must still match.
                if (pinned.produceQuality.Value != product.Quality
                    || (pinned.hasEatenAnimalCracker.Value ? 2 : 1) != product.Quantity
                    || (pinned.currentProduce.Value is not null && AnimalFingerprint(pinned) != product))
                    throw new InvalidOperationException("animal_product_changed");
                int count = AnimalInventoryCount(player, product);
                // Native collection adds the exact item before clearing currentProduce (MilkPail/Shears).
                if (pinned.currentProduce.Value is null && count - productBaseline != product.Quantity)
                    throw new InvalidOperationException("animal_product_inventory_mismatch");
                return new BgExtraFacts(kind, pinned.currentProduce.Value is null, count,
                    pinned.friendshipTowardFarmer.Value, product.Qid, ProductQuality: product.Quality, ProductQuantity: product.Quantity);
            }, product: product);
    }

    InteractKindCapture? CaptureAnimalPet(Farmer player, GameLocation location, Vector2 grab, ExtrasGuardFacts facts)
    {
        Rectangle rect = TileRect(grab);
        if (!location.objects.ContainsKey(grab))
        {
            List<FarmAnimal> animals = location.animals.Values.Where(a => a is not null && a.GetBoundingBox().Intersects(rect)).ToList();
            if (animals.Count > 0)
            {
                List<FarmAnimal> unpetted = animals.Where(a => !a.wasPet.Value).ToList();
                FarmAnimal animal = unpetted.FirstOrDefault() ?? animals[0];
                facts = facts with
                {
                    TargetCandidates = Math.Max(1, unpetted.Count),
                    AlreadyDone = unpetted.Count == 0,
                    ProfessionDetail = this.ExtrasProfession(player, Farmer.farmingSkill, WorldExtras.AnimalFarmingXp),
                };
                FarmAnimal pinned = animal;
                return this.ExtrasCapture(WorldExtras.AnimalPet, player, location, grab, facts, animal, animal.myID.Value.ToString(), null,
                    () => new BgExtraFacts(WorldExtras.AnimalPet, AnimalStillHere(location, pinned) && pinned.wasPet.Value, 0, pinned.friendshipTowardFarmer.Value));
            }
        }

        return null;
    }

}
