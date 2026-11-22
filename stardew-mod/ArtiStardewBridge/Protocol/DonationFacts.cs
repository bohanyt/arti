namespace ArtiStardewBridge.Protocol;

// ADR 49: eligibility is knowledge, never authorization to press or a donation receipt.
public static class DonationFacts
{
    public static bool Fits(ProgressRules.BundleNeed need, int stack, int quality, int category,
        string qualifiedId, bool sameItem) => !need.Done && need.Item != "money" && need.Quantity > 0
        && stack >= need.Quantity && quality >= need.Quality
        && (int.TryParse(need.Item, out int number) && number < 0
            ? category == number || number == -5 && qualifiedId == "(O)107" // native Bundle dinosaur egg exception
            : sameItem);

    public static object[] UnlockSources(IEnumerable<KeyValuePair<string, string>> events, string flag) => events
        .Where(e => e.Value.Split('/').Any(command => command == "addMailReceived " + flag))
        .Select(e => (object)e.Key).Take(8).ToArray();
}
