using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>Read-only native self facts for speech (ADR 40); never input policy.</summary>
static class NativePersonaFacts
{
    public static Dictionary<string, object?> Sleep(Farmer player, Farmer? host, IEnumerable<Farmer>? online = null) => new()
    {
        // Native bed/readiness values, never an inferred waiting intent or sleep order.
        ["role"] = "host", ["peer_bed"] = host?.isInBed.Value,
        ["peer_ready"] = host?.timeWentToBed.Value, ["self_bed"] = player.isInBed.Value,
        ["players"] = (online ?? (host is null ? new[] { player } : new[] { player, host }))
            .Distinct().OrderBy(f => f == host ? 0 : f == player ? 1 : 2).Take(8)
            .Select(f => (object)new Dictionary<string, object?> {
                ["name"] = f.Name is { } name ? name[..Math.Min(name.Length, 40)] : "",
                ["role"] = f == host ? "host farmer" : "farmhand",
                ["self"] = f == player, ["in_bed"] = f.isInBed.Value,
                ["ready_time"] = f.timeWentToBed.Value,
            }).ToArray(),
    };

    public static Dictionary<string, object> OwnState(Farmer player)
    {
        int used = player.Items.Take(player.MaxItems).Count(item => item is not null);
        return new()
        {
            ["inventory"] = new { used, free = player.MaxItems - used, capacity = player.MaxItems },
            ["energy"] = new { current = player.Stamina, max = player.MaxStamina },
            ["health"] = new { current = player.health, max = player.maxHealth },
            ["money"] = player.Money,
        };
    }

    public static Dictionary<string, object>? Container(IClickableMenu? menu)
    {
        if (menu is not ItemGrabMenu grab) return null;
        // The shipping flag takes precedence even when a mod also supplies a chest source.
        string kind = grab.shippingBin || grab.context is StardewValley.Buildings.ShippingBin
            ? "shipping_bin"
            : grab.context is StardewValley.Objects.Chest || grab.sourceItem is StardewValley.Objects.Chest
                ? "chest" : "other";
        return new()
        {
            ["kind"] = kind, ["menu_type"] = grab.GetType().Name, ["native_source"] = grab.source,
            ["context_type"] = grab.context?.GetType().Name ?? "",
            ["source_item_type"] = grab.sourceItem?.GetType().Name ?? "",
        };
    }
}
