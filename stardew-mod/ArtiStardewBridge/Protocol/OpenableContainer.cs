namespace ArtiStardewBridge.Protocol;

public static class OpenableContainer
{
    // Vanilla starter gifts have id (BC)-1 and playerChest=false. No contents are inspected here.
    public static bool Eligible(bool chest, bool gift, bool playerChest) => chest && (gift || !playerChest);
    public static Dictionary<string, object> Fact(string reference, int x, int y, bool gift, bool starter, bool otherOwner) => new()
    {
        ["ref"] = reference, ["tile"] = new[] { x, y }, ["kind"] = gift ? "gift_container" : "reward_container",
        ["starter_gift"] = starter, ["contents"] = "unknown_until_opened",
        ["interaction"] = "interact / ACTION; contents are owned only when inventory delta proves receipt",
        ["status"] = otherOwner ? "belongs_to_another_player" : "openable",
    };
}
