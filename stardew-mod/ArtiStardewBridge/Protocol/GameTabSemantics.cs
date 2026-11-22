namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Semantic IDs untuk native GameMenu tabs Stardew 1.6.
/// Dipisahkan dari observer supaya layout dapat diuji tanpa game runtime.
/// </summary>
public static class GameTabSemantics
{
    private const int FirstNativeId = 12340;

    private static readonly string[] Keys =
    {
        "inventory", "skills", "social", "map", "crafting",
        "animals", "powers", "collections", "options", "exit",
    };

    public static string? KeyForNativeId(int nativeId)
    {
        int index = nativeId - FirstNativeId;
        return index >= 0 && index < Keys.Length
            ? $"game.tab.{Keys[index]}"
            : null;
    }

    public static bool MatchesPage(string key, string pageType) =>
        (key, pageType) switch
        {
            ("game.tab.inventory", "InventoryPage") => true,
            ("game.tab.skills", "SkillsPage") => true,
            ("game.tab.social", "SocialPage") => true,
            ("game.tab.map", "MapPage") => true,
            ("game.tab.crafting", "CraftingPage") => true,
            ("game.tab.animals", "AnimalPage") => true,
            ("game.tab.powers", "PowersTab") => true,
            ("game.tab.collections", "CollectionsPage") => true,
            ("game.tab.options", "OptionsPage") => true,
            ("game.tab.exit", "ExitPage") => true,
            _ => false,
        };
}
