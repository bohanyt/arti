namespace ArtiStardewBridge.Protocol;

/// <summary>
/// PM-1 fix13b (PROGRESS_GAP_AUDIT_2026-10-29 ranked 3): crafting as native facts and a native affordance. Pure shaping of
/// what the game itself reports for a recipe: what it makes, each ingredient have / need, and whether the game would craft
/// it now. Published twice from the same rows: <c>crafting_known</c> in ui.context (every known recipe, the offer source)
/// and <c>crafting</c> in a generic CraftingPage's ui_pad block (the visible recipe components, the choose source). A
/// recipe that cannot be crafted now is refused at the arm with its reason, so native never publishes it as a choice.
/// Facts only; no recipe preference, order or routine.
/// </summary>
public static class CraftingFacts
{
    public const string KnownKey = "crafting_known", PageKey = "crafting";
    public const int MaxKnownRows = 32, MaxNeeds = 6;
    public const string Missing = "missing_ingredients", MultiOutput = "multi_output_recipe", NoRoom = "no_room_for_output",
        HeldItem = "held_item_place_first", Unknown = "recipe_not_known", ShiftOne = "recipe_one_per_press",
        DropsHeld = "held_item_drops_outside_page";

    /// <summary>A fact refusal = recipe component id -> reason; the generic arm refuses a choose on it.</summary>
    public static string? ChooseRefusal(IReadOnlyDictionary<string, object>? values, int componentId, bool shift)
    {
        if (values?.GetValueOrDefault(PageKey) is not IReadOnlyDictionary<string, object> page
            || page.GetValueOrDefault("blocked") is not IReadOnlyDictionary<string, object> blocked)
            return null;
        if (blocked.GetValueOrDefault(componentId.ToString()) is string why)
            return "crafting:" + why;
        // CraftingPage drops its held item where a click lands outside the page (the GameMenu tabs, the close button).
        if (page.GetValueOrDefault("drop_ids") is int[] drops && drops.Contains(componentId))
            return "crafting:" + DropsHeld;
        // The vanilla shift click crafts several; one recipe press stays one craft so the bag readback is exact.
        return shift && page.GetValueOrDefault("recipe_ids") is int[] ids && ids.Contains(componentId) ? "crafting:" + ShiftOne : null;
    }

    public static string Needs(CraftRecipe r) =>
        string.Join(", ", r.Ingredients.Take(MaxNeeds).Select(i => i.Name + " " + i.Have + "/" + i.Need));

    /// <summary>The component label: name, what it makes, have/need, and the game's verdict (generic menus have no text).</summary>
    public static string Label(CraftRecipe r) => UiPadTable.Trim(r.Name + " x" + r.Produces + " | " + Needs(r) + " | "
        + (r.Refusal is { } why ? why : "craftable"), 160);

    /// <summary>ui.context crafting_known: every known recipe counted; craftable-now rows listed (her offer source).</summary>
    public static Dictionary<string, object> Known(IReadOnlyList<CraftRecipe> known)
    {
        var craftable = known.Where(r => r.Refusal is null).ToList();
        return new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["known"] = known.Count,
            ["craftable_total"] = craftable.Count,
            ["craftable"] = craftable.Take(MaxKnownRows).Select(Row).ToArray(),
            ["blocked"] = known.Where(r => r.Refusal is not null).Take(MaxKnownRows)
                .Select(r => new object[] { r.Recipe, r.Refusal!, Needs(r) }).ToArray(),
        };
    }

    static Dictionary<string, object> Row(CraftRecipe r) => new(StringComparer.Ordinal)
    {
        ["recipe"] = UiPadTable.Trim(r.Recipe, 64), ["name"] = UiPadTable.Trim(r.Name, 64), ["makes"] = r.Makes,
        ["n"] = r.Produces, ["needs"] = r.Ingredients.Take(MaxNeeds).Select(i => new object[] { i.Name, i.Have, i.Need }).ToArray(),
        ["max_crafts"] = Math.Min(999, r.MaxCrafts),
    };

    /// <summary>The generic CraftingPage block: visible recipe components (id -> fact) and the refused ids.</summary>
    public static Dictionary<string, object> Page(IReadOnlyList<(int Id, CraftRecipe Recipe)> visible, bool cooking, int page, int pages,
        IReadOnlyList<int>? dropIds = null)
    {
        var blocked = new Dictionary<string, object>(StringComparer.Ordinal);
        foreach (var (id, r) in visible)
            if (r.Refusal is { } why) blocked[id.ToString()] = why;
        return new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["cooking"] = cooking, ["page"] = page, ["pages"] = pages,
            ["recipes"] = visible.Select(v => new object[] { v.Id, UiPadTable.Trim(v.Recipe.Name, 64), v.Recipe.Makes,
                v.Recipe.Produces, v.Recipe.Refusal ?? "craftable", Needs(v.Recipe), UiPadTable.Trim(v.Recipe.Recipe, 64) }).ToArray(),
            ["recipe_ids"] = visible.Select(v => v.Id).ToArray(),
            ["blocked"] = blocked,
            ["drop_ids"] = (dropIds ?? Array.Empty<int>()).ToArray(),
        };
    }
}

public sealed record CraftIngredient(string Key, string Name, int Need, int Have);

/// <summary>One recipe as the game reports it now. <paramref name="Fits"/> = the bag takes the output after the craft
/// consumes its ingredients; <paramref name="HeldBlocks"/> = the menu cursor holds an item the output cannot stack on.</summary>
public sealed record CraftRecipe(string Recipe, string Name, string Makes, int Produces, bool Known, bool SingleOutput,
    bool IngredientsOk, bool Fits, bool HeldBlocks, IReadOnlyList<CraftIngredient> Ingredients)
{
    public int MaxCrafts => this.Ingredients.Count == 0 ? 0 : this.Ingredients.Min(i => i.Need <= 0 ? int.MaxValue : i.Have / i.Need);

    public string? Refusal => !this.Known ? CraftingFacts.Unknown : !this.SingleOutput ? CraftingFacts.MultiOutput
        : !this.IngredientsOk ? CraftingFacts.Missing : this.HeldBlocks ? CraftingFacts.HeldItem
        : !this.Fits ? CraftingFacts.NoRoom : null;

    /// <summary>The output fits: free capacity for it now, or an ingredient stack the craft uses up frees its slot.</summary>
    public static bool OutputFits(int capacity, int produces, IReadOnlyList<CraftIngredient> ingredients, Func<string, bool> wholeStackUsed) =>
        capacity >= produces || ingredients.Any(i => wholeStackUsed(i.Key));
}
