using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>
/// PM-1 fix13b (game thread, read-only): the recipe facts behind <see cref="CraftingFacts"/>. The same reads the game's
/// CraftingPage uses: the actor's known recipe names, CraftingRecipe's own ingredient list / output / producer count and
/// doesFarmerHaveIngredientsInInventory, Farmer.getItemCount (category keys included). Nothing is written; a recipe object
/// is built once per name like the CraftingPage builds its own (contracts/il_read_allowlist.json).
/// </summary>
internal static class NativeCraftingFacts
{
    static readonly Dictionary<string, CraftingRecipe> Recipes = new(StringComparer.Ordinal);
    static string? signature;
    static Dictionary<string, object>? cached;

    static CraftingRecipe Recipe(string name)
    {
        if (!Recipes.TryGetValue(name, out CraftingRecipe? recipe))
            Recipes[name] = recipe = new CraftingRecipe(name, false);
        return recipe;
    }

    static string ItemName(string key)
    {
        if (key.StartsWith('-'))
            return "category " + key;
        return ItemRegistry.GetData(key)?.DisplayName ?? key;
    }

    /// <summary>One recipe as the game reports it now (the page's material containers and cursor item when a page is open).</summary>
    internal static CraftRecipe Fact(CraftingRecipe recipe, Farmer player, IList<Item>? extra, Item? held, bool known)
    {
        var ingredients = new List<CraftIngredient>();
        foreach (var pair in recipe.recipeList)
            ingredients.Add(new CraftIngredient(pair.Key, ItemName(pair.Key), pair.Value,
                player.getItemCount(pair.Key) + (extra is null ? 0 : player.getItemCountInList(extra, pair.Key))));
        string makes = recipe.GetItemData(true)?.QualifiedItemId ?? "";
        int produces = Math.Max(1, recipe.numberProducedPerCraft);
        bool single = recipe.itemToProduce.Count == 1;
        bool ok = single && known && recipe.doesFarmerHaveIngredientsInInventory(extra);
        int capacity = 0;
        int emptySlots = 0;
        for (int i = 0; i < player.MaxItems && i < player.Items.Count; i++)
        {
            Item? slot = player.Items[i];
            if (slot is null)
                emptySlots++;
            else if (slot.QualifiedItemId == makes)
                capacity += Math.Max(0, slot.maximumStackSize() - slot.Stack);
        }
        if (emptySlots > 0)
            capacity += produces;
        bool fits = CraftRecipe.OutputFits(capacity, produces, ingredients, key => WholeStackUsed(player, key, ingredients));
        bool heldBlocks = held is not null && !(held.QualifiedItemId == makes && held.Stack + produces <= held.maximumStackSize());
        return new CraftRecipe(recipe.name, recipe.DisplayName ?? recipe.name, makes, produces, known, single, ok, fits,
            heldBlocks, ingredients);
    }

    /// <summary>The craft uses up every bag unit of this exact item id, so its slot frees for the output.</summary>
    static bool WholeStackUsed(Farmer player, string key, IReadOnlyList<CraftIngredient> ingredients)
    {
        if (key.StartsWith('-'))
            return false;
        foreach (CraftIngredient i in ingredients)
            if (i.Key == key)
                return i.Have == i.Need && player.getItemCount(key) == i.Need;
        return false;
    }

    /// <summary>ui.context crafting_known (observation only; world ready, not the main player).</summary>
    internal static void Enrich(Dictionary<string, object> snapshot)
    {
        snapshot.Remove(CraftingFacts.KnownKey);
        if (!Context.IsWorldReady || Game1.player is not { } player)
            return;
        try
        {
            var names = new List<string>();
            foreach (string name in player.craftingRecipes.Keys)
                names.Add(name);
            names.Sort(StringComparer.Ordinal);
            var sig = new System.Text.StringBuilder(string.Join("|", names));
            for (int i = 0; i < player.Items.Count; i++)
                sig.Append('|').Append(player.Items[i]?.QualifiedItemId).Append(':').Append(player.Items[i]?.Stack ?? 0);
            if (sig.ToString() != signature || cached is null)
            {
                var known = new List<CraftRecipe>();
                foreach (string name in names)
                    known.Add(Fact(Recipe(name), player, null, null, true));
                cached = CraftingFacts.Known(known);
                signature = sig.ToString();
            }
            var published = new Dictionary<string, object>(cached);
            // The active journal's crafting step needs material facts even when
            // they are in a chest, so it is absent from craftable-now.
            string[] objectives = player.questLog.OfType<StardewValley.Quests.CraftingQuest>()
                .Where(q => !q.completed.Value).Select(q => NativeQuestFacts.Read(q, player).Objective).ToArray();
            published["quest_recipes"] = names.Where(name => objectives.Any(text =>
                    text.Contains(name, StringComparison.OrdinalIgnoreCase)))
                .Select(name => {
                    var fact = Fact(Recipe(name), player, null, null, true);
                    return new { recipe = fact.Recipe, name = fact.Name, makes = fact.Makes,
                        needs = fact.Ingredients.Select(i => new object[] { i.Name, i.Have, i.Need }).ToArray() };
                }).ToArray();
            snapshot[CraftingFacts.KnownKey] = published;
        }
        catch (Exception ex)
        {
            snapshot[CraftingFacts.KnownKey] = new Dictionary<string, object> { ["unknown"] = "capture " + ex.GetType().Name };
        }
    }

    /// <summary>The GameMenu crafting page's own cursor item (CraftingPage.heldItem: a mouse craft's output lands there,
    /// CraftingPage.readyToClose is heldItem == null). Generic UI reads the root's heldItem only; this is the page's.</summary>
    internal static Item? PageHeld(IClickableMenu? menu) =>
        ((menu is GameMenu gm ? gm.GetCurrentPage() : null) as CraftingPage)?.heldItem;

    /// <summary>The open CraftingPage's current recipe page: component -> its native recipe (ghosted = not known).</summary>
    internal static (Dictionary<ClickableComponent, CraftRecipe> Recipes, bool Cooking, int Page, int Pages,
        Microsoft.Xna.Framework.Rectangle? HeldDropOutside)? PageRecipes(IClickableMenu? menu)
    {
        IClickableMenu? owner = menu is GameMenu gm ? gm.GetCurrentPage() : menu;
        if (owner is not CraftingPage page || page.pagesOfCraftingRecipes is not { Count: > 0 } pages
            || page.currentCraftingPage < 0 || page.currentCraftingPage >= pages.Count || Game1.player is not { } player)
            return null;
        IList<Item>? extra = null;
        if (page._materialContainers is { } containers)
        {
            var list = new List<Item>();
            foreach (var inventory in containers)
                list.AddRange(inventory.Where(item => item is not null)); // the page's own material list, as ModEntry.Economy
            extra = list;
        }
        var map = new Dictionary<ClickableComponent, CraftRecipe>(ReferenceEqualityComparer.Instance);
        foreach (var pair in pages[page.currentCraftingPage])
            if (pair.Value is { } recipe)
                map[pair.Key] = Fact(recipe, player, extra, page.heldItem, pair.Key.hoverText != "ghosted");
        // With a held item, a click outside the page's own rectangle drops it (CraftingPage.receiveLeftClick).
        var bounds = new Microsoft.Xna.Framework.Rectangle(page.xPositionOnScreen, page.yPositionOnScreen, page.width, page.height);
        return (map, page.cooking, page.currentCraftingPage, pages.Count, page.heldItem is null ? null : bounds);
    }
}
