namespace ArtiStardewBridge.Protocol;

/// <summary>Map properties for where carried tools and items can be used. No location names.</summary>
public static class UsePlaceFacts
{
    /// <summary>
    /// Location-level "can seeds be planted on this map", matching Stardew 1.6.15.
    /// The game has no 0-arg <c>CanPlantSeedsHere</c>. Its five-arg body (IL on
    /// 1.6.15.24356) feeds <c>CheckItemPlantRules(..., defaultAllowed)</c> with
    /// <c>GetData()?.CanPlantHere ?? IsFarm</c>. <c>LocationData.CanPlantHere</c> is
    /// <c>bool?</c>; GameData.xml: omitted defaults true on the farm and false
    /// elsewhere. Greenhouse plantability is that flag (vanilla data sets it),
    /// not <c>IsGreenhouse</c> — the defaultAllowed expression does not read
    /// IsGreenhouse. No map-name table.
    /// </summary>
    public static bool MapAllowsSeeds(bool? canPlantHere, bool isFarm)
        => canPlantHere ?? isFarm;

    public static string? Soil(bool plantHere, int tilled, int planted, int dry)
    {
        if (!plantHere && planted == 0 && tilled == 0)
            return null;
        string text = "tillable soil: reachable ground observed here; hoe and seeds";
        if (planted > 0)
            text += "; observed planted crops " + planted + "; watering can waters them"
                + (dry > 0 ? "; " + dry + " dry" : "");
        else if (tilled > 0)
            text += "; " + tilled + " hoed tiles";
        return text;
    }
}
