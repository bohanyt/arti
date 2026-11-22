namespace ArtiStardewBridge.Protocol;

/// <summary>FIXB48: vanilla overnight floor products, never a category-wide object permission.</summary>
public static class AnimalProductPickup
{
    static int? ExpectedCategory(string? qid) => qid switch
    {
        // White/brown eggs and large eggs, duck, void, golden and ostrich eggs.
        "(O)176" or "(O)180" or "(O)174" or "(O)182" or "(O)442" or "(O)305" or "(O)928" or "(O)289" => -5,
        "(O)107" => 0, // Dinosaur egg (Arch, no numeric category).
        "(O)440" or "(O)444" or "(O)446" => -18, // Wool, duck feather, rabbit's foot.
        _ => null,
    };

    public static bool IsProductId(string? qid) => ExpectedCategory(qid) is not null;
    public static bool IdentityAllowed(string? qid, int category) => ExpectedCategory(qid) is int expected && category == expected;

    public static bool Allowed(string? qid, int category, string? buildingType, bool animalHouse,
        bool plainObject, bool spawned, bool grabbable, bool canBeSetDown, bool questItem,
        bool bigCraftable, bool machine, bool invisible, int stack, string? type) =>
        IdentityAllowed(qid, category) && animalHouse
        && (buildingType is "Coop" or "Big Coop" or "Deluxe Coop" or "Barn" or "Big Barn" or "Deluxe Barn")
        && plainObject && spawned && grabbable && !canBeSetDown && !questItem && !bigCraftable
        && !machine && !invisible && stack == 1 && type is not null && type is not ("Crafting" or "interactive");
}
