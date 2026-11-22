namespace ArtiStardewBridge.Protocol;

// Primitives v2 WS-G6 review MINOR 3: the machine drop-in input policy on top of WS-G2's item policy types (EconItem,
// ItemPolicyConfig). Kept out of WorldExtras.cs so test projects compiling the hold / interaction core (LatchTests) do
// not need the economy layer. Pure, SMAPI-free. Evidence ceiling: UNIT_TESTED.
public static class WorldExtrasPolicy
{
    /// <summary>
    /// The item an X drop-in consumes: G2's hard rules (tool / quest / special / equipment / recipe), then the owner's
    /// machine-input allowlist (wins over G2's never-dispose list, which holds the ores a furnace takes), then G2's
    /// never-dispose list, then the price cap. Legendary fish / valuable crops / gems are refused by default.
    /// </summary>
    public static string? InsertPolicyRefusal(EconItem item, ItemPolicyConfig policy, IReadOnlyCollection<string> inputAllowlist,
        int inputPriceCap)
    {
        if (item.IsTool) return "input_policy:tool";
        if (item.QuestItem) return "input_policy:quest_item";
        if (item.SpecialItem || !item.CanBeTrashed) return "input_policy:special_item";
        if (item.Equipment) return "input_policy:equipment";
        if (item.IsRecipe) return "input_policy:recipe";
        if (inputAllowlist.Contains(item.QualifiedId))
            return null;
        if (policy.NeverDispose.Contains(item.QualifiedId, StringComparer.Ordinal))
            return "input_policy:never_list";
        if (inputPriceCap > 0 && item.UnitSellPrice > 0 && item.UnitSellPrice <= inputPriceCap)
            return null;
        return "input_policy:not_allowlisted";
    }

    /// <summary>Default machine inputs: copper / iron / gold / iridium ore (furnace) + G2's basic forage / crops (keg / jar).</summary>
    public static readonly IReadOnlyList<string> DefaultMachineInputAllowlist =
        new[] { "(O)378", "(O)380", "(O)384", "(O)386" }.Concat(ItemPolicyConfig.DefaultAllowlist).ToArray();
}
