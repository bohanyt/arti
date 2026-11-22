namespace ArtiStardewBridge.Protocol;

// Primitives v2 WS-G2: the owner item policy table (PLAN.md 6.1 D10) bound from config.json (ModConfig.EconomyItemPolicy).
// Dependency-free on purpose: ModConfig.cs is also compiled alone by the RenderTimingTests harness.

/// <summary>Owner item policy (PLAN 6.1 D10). A config table; the defaults are deliberately conservative.</summary>
public sealed class ItemPolicyConfig
{
    /// <summary>
    /// Owner 2026-10-23 sell policy (B), DEFAULT ON: sell / ship is a denylist. Refused: tools and weapons, equipment
    /// (rings / boots / hats / clothing / furniture / trinkets), quest items, special / story (untrashable) items and
    /// recipes (ItemPolicy.Hard), items the game cannot ship, and <see cref="FreeDisposeNever"/>. Everything else may go
    /// (wood, stone, ores, coal, bars, gems, minerals, artifacts, rare seeds). Off = the old allowlist / price-cap /
    /// <see cref="NeverDispose"/> mode below. A separate key so an existing config.json written with the old lists
    /// still gets the owner's choice; the machine drop-in policy (WorldExtrasPolicy) keeps NeverDispose.
    /// </summary>
    public bool FreeDispose { get; set; } = true;

    /// <summary>Qualified ids the owner still refuses under <see cref="FreeDispose"/> (default: none).</summary>
    public List<string> FreeDisposeNever { get; set; } = new();

    /// <summary>Items ARTI may sell at a shop (qualified ids). Default: spring/summer/fall forage + basic crops.</summary>
    public List<string> SellAllowlist { get; set; } = new(DefaultAllowlist);

    /// <summary>0 = allowlist only; &gt; 0 also admits any item whose unit sell price is at most this (never-list still wins).</summary>
    public int SellPriceCap { get; set; } = 0;

    /// <summary>Items ARTI may put in the shipping bin (qualified ids). Default = the sell allowlist.</summary>
    public List<string> ShipAllowlist { get; set; } = new(DefaultAllowlist);

    public int ShipPriceCap { get; set; } = 0;

    /// <summary>FIXB27 D-B24-2 (CT AFK default 2026-10-12, owner-overridable): common low-value fish ARTI may sell, each unit
    /// at most <see cref="SellFishPriceCap"/> gold (an iridium-star fish above the cap stays refused).</summary>
    public List<string> SellFishAllowlist { get; set; } = new(DefaultSellFish);

    public int SellFishPriceCap { get; set; } = 100;

    /// <summary>Never sold / shipped even under a price cap (crafting materials). Tools / quest / special are hard-coded.</summary>
    public List<string> NeverDispose { get; set; } = new(DefaultNeverDispose);

    /// <summary>Upper bound of one purchase (price x qty, gold). Backpack (2,000 g) is refused by default.</summary>
    public int BuyMaxTotal { get; set; } = 500;

    /// <summary>Clint's tool-upgrade shop (ShopId ClintUpgrade): the tool leaves the inventory for 2 days.</summary>
    public bool AllowToolUpgrade { get; set; } = false;

    /// <summary>Recipe names ARTI may not craft / cook (default: none).</summary>
    public List<string> CraftDeny { get; set; } = new();

    /// <summary>Trash / drop: never (PLAN 1.4, MS9). Not configurable here.</summary>
    public const bool TrashNever = true;

    public static readonly string[] DefaultAllowlist =
    {
        "(O)16", "(O)18", "(O)20", "(O)22", "(O)24", "(O)188", "(O)190", "(O)192", "(O)248", "(O)250", "(O)296",
        "(O)396", "(O)398", "(O)399", "(O)402", "(O)404", "(O)406", "(O)408", "(O)410", "(O)412", "(O)414", "(O)416",
        "(O)418",
    };

    /// <summary>Carp, Bullhead, Sunfish, Chub, Bream, Smallmouth Bass.</summary>
    public static readonly string[] DefaultSellFish = { "(O)142", "(O)700", "(O)145", "(O)702", "(O)132", "(O)137" };

    public static readonly string[] DefaultNeverDispose =
    {
        "(O)388", "(O)390", "(O)771", "(O)382", "(O)378", "(O)380", "(O)384", "(O)386", "(O)334", "(O)335", "(O)336",
        "(O)337", "(O)709", "(O)92", "(O)330", "(O)766", "(O)74", "(O)72",
    };
}
