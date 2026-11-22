using System.Text.Json;
using System.Text.Json.Serialization;

namespace ArtiStardewBridge.Protocol;

// Native observations, never purchase commands or success flags. IDs/footprints stay bound in ui_pad's digest.
public sealed record PurchaseSpecies([property: JsonPropertyName("id")] int Id, [property: JsonPropertyName("species")] string Species, [property: JsonPropertyName("price")] int Price, [property: JsonPropertyName("available")] bool Available,
    [property: JsonPropertyName("display")] string? Display = null);
public sealed record PurchaseHome([property: JsonPropertyName("ref")] string Ref, [property: JsonPropertyName("type")] string Type, [property: JsonPropertyName("interior")] string Interior, [property: JsonPropertyName("x")] int X, [property: JsonPropertyName("y")] int Y, [property: JsonPropertyName("width")] int Width, [property: JsonPropertyName("height")] int Height,
    [property: JsonPropertyName("capacity")] int Capacity, [property: JsonPropertyName("residents")] IReadOnlyList<string> Residents);
public sealed record PurchaseAnimal([property: JsonPropertyName("id")] string Id, [property: JsonPropertyName("instance")] string Instance, [property: JsonPropertyName("type")] string Type, [property: JsonPropertyName("name")] string Name, [property: JsonPropertyName("owner")] string Owner, [property: JsonPropertyName("home")] string Home);
public sealed record PurchaseAdoption([property: JsonPropertyName("receiver")] string Receiver, [property: JsonPropertyName("actor")] string Actor, [property: JsonPropertyName("building")] string Building, [property: JsonPropertyName("price")] int Price, [property: JsonPropertyName("money_before")] int MoneyBefore,
    [property: JsonPropertyName("money_after")] int MoneyAfter, [property: JsonPropertyName("animal")] PurchaseAnimal Animal, [property: JsonPropertyName("before")] IReadOnlyList<string> Before, [property: JsonPropertyName("after")] IReadOnlyList<string> After,
    [property: JsonPropertyName("same_instance")] bool SameInstance, [property: JsonPropertyName("in_house")] bool InHouse);
public sealed record PurchaseAnimalsFacts([property: JsonPropertyName("phase")] string Phase, [property: JsonPropertyName("actor")] string Actor, [property: JsonPropertyName("target")] string Target, [property: JsonPropertyName("money")] int Money, [property: JsonPropertyName("price")] int Price,
    [property: JsonPropertyName("animal")] PurchaseAnimal? Animal, [property: JsonPropertyName("species")] IReadOnlyList<PurchaseSpecies> Species, [property: JsonPropertyName("buildings")] IReadOnlyList<PurchaseHome> Buildings,
    [property: JsonPropertyName("names")] IReadOnlyList<string> Names, [property: JsonPropertyName("ready")] bool Ready, PurchaseAdoption? Adoption = null, string? SelectedBuilding = null, string? SelectedSpecies = null)
{
    public Dictionary<string, object?> Snapshot() => new()
    {
        ["phase"] = Phase, ["actor"] = Actor, ["target"] = Target, ["money"] = Money, ["price"] = Price,
        ["animal"] = Animal, ["species"] = Species, ["buildings"] = Buildings, ["names"] = Names,
        ["ready"] = Ready, ["adoption"] = Adoption, ["selected_building"] = SelectedBuilding, ["selected_species"] = SelectedSpecies,
    };
    public string Digest() => JsonSerializer.Serialize(this);
}

public static class PurchaseAnimalsUi
{
    public const string Context = "purchase_animals";
    public static UiPadPlan Translate(string button, UiPadFacts f, IReadOnlyDictionary<string, string>? args)
    {
        var p = f.PurchaseAnimals!;
        UiPadPlan No(string why) => UiPadPlan.No(button, "UI_DENIED:animal_" + why);
        if (!p.Ready || f.EventUp || f.MinigameActive) return No("not_ready");
        if (button == "B" && (args is null || args.Count == 0))
            return UiPadPlan.Ok(button, "cancel the animal menu", new UiPadStep(UiPadStepKind.AnimalClick, "B"));
        if (button != "A") return No("button");
        if (p.Phase == "species")
        {
            if (args is { Count: > 0 }) return No("payload");
            var row = p.Species.SingleOrDefault(s => s.Id == f.SnappedId);
            if (row is null || !row.Available || row.Price <= 0 || p.Money < row.Price) return No("species_unavailable");
            return UiPadPlan.Ok(button, "select the offered animal species", new UiPadStep(UiPadStepKind.AnimalClick, "A"));
        }
        if (p.Phase == "home")
        {
            if (args is null) return UiPadPlan.Ok(button, "select an offered {building}", new UiPadStep(UiPadStepKind.AnimalClick, "A"));
            if (args.Count != 1 || !args.TryGetValue("building", out var home)
                || !p.Buildings.Any(b => b.Ref == home && b.Residents.Count < b.Capacity)) return No("building_changed");
            return UiPadPlan.Ok(button, "select the offered building", new UiPadStep(UiPadStepKind.AnimalClick, "A", G3Arg: home));
        }
        return No("phase");
    }

    /// <summary>
    /// #320: the transaction boundary of one native adoption, judged inside the receiver (no name / request facts):
    /// the same pending instance in its pinned home, a positive price debited exactly from the pre-submit wallet, and
    /// the roster grown by exactly that animal. Only this pass may freeze the money / roster pair.
    /// </summary>
    public static bool DebitBoundary(int price, int moneyBefore, int moneyNow, IReadOnlyList<string> before,
        IReadOnlyList<string> after, string animalId, bool sameInstance, bool inHouse) =>
        sameInstance && inHouse && price > 0 && moneyNow == moneyBefore - price && !before.Contains(animalId)
        && after.Count == before.Count + 1 && after.Distinct().Count() == after.Count && after.Contains(animalId)
        && before.All(after.Contains);

    public static bool ExactAdoption(PurchaseAdoption a, PurchaseAnimal animal, string name) =>
        a.SameInstance && a.InHouse && a.Animal.Id == animal.Id && a.Animal.Instance == animal.Instance
        && a.Animal.Type == animal.Type && a.Animal.Owner == animal.Owner && a.Actor == animal.Owner
        && a.Animal.Name == name && a.Animal.Home.Length > 0 && a.Price > 0
        && a.MoneyAfter == a.MoneyBefore - a.Price && !a.Before.Contains(animal.Id)
        && a.After.Count == a.Before.Count + 1 && a.After.Distinct().Count() == a.After.Count
        && a.After.Contains(animal.Id) && a.Before.All(a.After.Contains);

    public static bool ExactAdoption(PurchaseAdoption a, PurchaseAnimalsFacts before, string name) =>
        before.Animal is { } animal && ExactAdoption(a, animal, name) && a.Building == before.SelectedBuilding
        && a.Price == before.Price && a.MoneyBefore == before.Money
        && before.Buildings.Any(b => b.Ref == a.Building && b.Interior == a.Animal.Home && b.Residents.SequenceEqual(a.Before));
}
