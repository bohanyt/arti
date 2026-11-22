namespace ArtiStardewBridge.Protocol;

// ADR 246 (fix143A): the named harms of a generic press are refused in the mod's own admission, for every caller.
// Pure and SMAPI-free. The facts come from the game's own objects and predicates (GenericHarmProbe for the world
// press, GenericShopState / CursorItemSettle for a menu press); no villager, crop, item or shop is listed here.

/// <summary>
/// Read-only facts of one generic WORLD press (the mod's generic ACTION = pad A on the grab tile, generic USE = pad X
/// with the held item on the tool tile). <paramref name="ToolKind"/> is the held tool's game class in plain words
/// (<see cref="GenericHarm.ToolAxe"/> ...), null without a tool. <paramref name="VillagerName"/>: the game's display name
/// of a villager whose body overlaps the pressed tile (or the tile below it, the head of a two-tile sprite).
/// <paramref name="TerrainType"/>: the game class of the terrain feature on the pressed tile (HoeDirt, Tree, FruitTree,
/// Grass ...). <paramref name="SweepArea"/>: the scythe / weapon sweep (null when not sampled).
/// </summary>
public sealed record GenericHarmFacts(
    bool Readable,
    bool Use,
    bool ObjectHeld,
    string? HeldName,
    string? ToolKind,
    string? VillagerName,
    bool ShippingBinAtTile,
    int TileX,
    int TileY,
    string? TerrainType,
    bool CropAtTile,
    int? TreeGrowthStage,
    IReadOnlyList<BgAreaTileFacts>? SweepArea = null,
    int PlayerTileX = 0,
    int PlayerTileY = 0,
    int Facing = -1);

public static class GenericHarm
{
    public const string Prefix = "GENERIC_HARM";
    // The harm, in the game's terms. Each code is followed by the names it concerns (who, what, where).
    public const string GiftToVillager = "gift_to_villager";
    public const string ShipHeldItem = "ship_held_item";
    public const string DestroysCrop = "destroys_crop";
    public const string DestroysSapling = "destroys_sapling";
    public const string DestroysFruitTree = "destroys_fruit_tree";
    public const string SweepDestroys = "sweep_destroys_crop_or_sapling";
    public const string FactsUnreadable = "facts_unreadable";

    public const string ToolAxe = "axe", ToolPickaxe = "pickaxe", ToolHoe = "hoe", ToolCan = "watering_can",
        ToolScythe = "scythe", ToolWeapon = "weapon", ToolOther = "other_tool";

    /// <summary>A wild tree below this growth stage is a seed or sapling (Tree.treeStage: 5 = mature).</summary>
    public const int MatureTreeStage = 5;

    /// <summary>Tools whose hit removes a crop, a sapling or a fruit tree on the hit tile (HoeDirt / Tree / FruitTree
    /// performToolAction with an axe, pickaxe or hoe).</summary>
    public static bool HeavyHitter(string? toolKind) => toolKind is ToolAxe or ToolPickaxe or ToolHoe;

    /// <summary>A sweeping blade: the scythe's and a weapon's area of effect covers the faced tile's neighbours.</summary>
    public static bool Sweeps(string? toolKind) => toolKind is ToolScythe or ToolWeapon;

    /// <summary>null = no named harm; otherwise <c>GENERIC_HARM:&lt;code&gt;:&lt;names&gt;</c>.</summary>
    public static string? Refusal(GenericHarmFacts f)
    {
        if (!f.Readable)
            return Prefix + ":" + FactsUnreadable;
        if (!f.Use)
        {
            // Game1.pressActionButton -> NPC.checkAction with an object in hand: the villager takes it (a gift, or a
            // quest delivery). The same press with an empty hand or a tool only talks.
            if (f.ObjectHeld && f.VillagerName is { Length: > 0 } who)
                return Code(GiftToVillager, who, f.HeldName ?? "?");
            // The shipping bin takes the held stack on the action press (a sale tonight).
            if (f.ObjectHeld && f.ShippingBinAtTile)
                return Code(ShipHeldItem, f.HeldName ?? "?");
            return null;
        }
        string tile = f.TileX + "," + f.TileY;
        if (HeavyHitter(f.ToolKind))
        {
            if (string.Equals(f.TerrainType, "HoeDirt", StringComparison.Ordinal) && f.CropAtTile)
                return Code(DestroysCrop, tile);
            if (string.Equals(f.TerrainType, "Tree", StringComparison.Ordinal) && f.TreeGrowthStage is int stage && stage < MatureTreeStage)
                return Code(DestroysSapling, tile);
            if (string.Equals(f.TerrainType, "FruitTree", StringComparison.Ordinal))
                return Code(DestroysFruitTree, tile);
        }
        if (Sweeps(f.ToolKind) && SweepUnsafe(f))
            return Code(SweepDestroys, tile);
        return null;
    }

    /// <summary>The whole sweep must be read (fail closed); a sapling or a crop anywhere in it is unsafe.</summary>
    static bool SweepUnsafe(GenericHarmFacts f)
    {
        IReadOnlyList<(int X, int Y)> expected = BgToolTileGuard.ScytheAreaTiles(f.PlayerTileX, f.PlayerTileY, f.Facing);
        if (f.SweepArea is not { } area || expected.Count == 0)
            return true;
        foreach ((int x, int y) in expected)
        {
            bool sampled = false;
            foreach (BgAreaTileFacts a in area)
            {
                if (a.X != x || a.Y != y)
                    continue;
                sampled = true;
                if ((a.TreeGrowthStage is int g && g < MatureTreeStage) || a.HoeDirtHasCrop)
                    return true;
            }
            if (!sampled)
                return true;
        }
        return false;
    }

    static string Code(string code, params string[] names)
    {
        var parts = new List<string>(names.Length + 2) { Prefix, code };
        foreach (string n in names)
            parts.Add(n.Replace(':', ' ').Trim());
        return string.Join(":", parts);
    }

    /// <summary>
    /// A generic MENU press without a named choice (no <c>choice</c> argument: it lands on the snapped part or the first
    /// visible one). On a shop's stock row it buys, on a shop's accepted bag slot it sells, on a bag slot of the shipping
    /// bin it ships, on the trash can or the drop zone it destroys or drops the cursor item. Each is refused with the
    /// harm in the game's terms; the same parts stay pressable by their named choice (the shop's own quantity path,
    /// her named sale, her named disposal with its item rules). null = the blind press harms nothing named.
    /// </summary>
    /// <summary>
    /// W1b B1 (CT ruling): a close of any menu kind with an item held never drops it. It is refused by name when the
    /// game's exit would drop the item at her feet, and when the cursor read is not complete (an unknown cursor is never
    /// taken for an empty one). A close whose exit returns the item to the bag stays allowed; a discarding exit is already
    /// withheld (CursorItemSettle.ExitAdmitted). null = the close is safe for the held item.
    /// </summary>
    public static string? HeldItemClose(UiPadFacts f)
    {
        if (f.CursorSettle is { Complete: false })
            return Code(BlindUnclassified, "cursor_unreadable", "close");
        bool held = f.HeldItemId is not null || f.CursorSettle?.Cursor is not null;
        if (held && f.CursorSettle?.Exit == CursorItemSettle.ExitDrops)
            return Code(BlindDispose, "drop_on_close", f.CursorSettle?.Cursor?.Name ?? f.HeldItemId ?? "?");
        return null;
    }

    public static string? BlindMenuPress(UiPadFacts f, UiPadComponent target)
    {
        // W1b B1: a cursor read that is not complete cannot tell whether an item is held; nothing unnamed is pressed.
        if (f.CursorSettle is { Complete: false })
            return Code(BlindUnclassified, "cursor_unreadable", target.Label);
        bool slotWithItem = string.Equals(target.Kind, UiPadComponent.KindSlot, StringComparison.Ordinal) && target.ItemId is not null;
        if (f.ShopCatalog is { } shop)
        {
            foreach (GenericShopRow row in shop.Rows)
                if (row.Control == target.Id)
                    return Code(BlindPurchase, row.Name, row.Price + "g");
            bool classified = false;
            foreach (GenericSaleRow sale in shop.Sales ?? Array.Empty<GenericSaleRow>())
            {
                if (sale.Control != target.Id)
                    continue;
                if (sale.Accepted)
                    return Code(BlindSale, sale.Name, sale.UnitPrice + "g");
                classified = true;
            }
            // Review B1, fail closed: the reader could not name every stock part the shop shows, or this bag slot holds an
            // item the sale rows do not classify. The press cannot be judged, so it is refused. A close is never a trade.
            if (!shop.Complete && !IsClose(target))
                return Code(BlindUnclassified, "shop_rows_incomplete", target.Label);
            if (slotWithItem && !classified)
                return Code(BlindUnclassified, "sale_unclassified", target.Label);
        }
        if (f.ShippingBin && slotWithItem)
            return Code(BlindShip, target.Label);
        string? destination = null;
        foreach (CursorItemDestination d in f.CursorSettle?.Destinations ?? Array.Empty<CursorItemDestination>())
            if (d.Id == target.Id)
                destination = d.Kind;
        string kind = destination ?? target.Kind;
        if (CursorItemSettle.Destructive(kind))
            return Code(BlindDispose, kind, f.CursorSettle?.Cursor?.Name ?? f.HeldItemId ?? "?");
        // Review B1 (W1), fail closed with an item held. If the destinations were not read completely (no snapshot, an
        // incomplete one, or no cursor in it), a trash can or a drop zone cannot be told from a slot. A part that is not
        // one of the item's read destinations (a crafting page's outside-page drop, a generic-looking button) is not
        // classified either. A close keeps its own held-item rule (the exit gesture), so it is not refused here.
        if (f.HeldItemId is not null && !IsClose(target))
        {
            if (f.CursorSettle is not { Complete: true, Cursor: not null })
                return Code(BlindUnclassified, "held_item_destinations_unread", target.Label);
            if (destination is null)
                return Code(BlindUnclassified, "held_item_target_not_a_destination", target.Label);
        }
        return null;
    }

    static bool IsClose(UiPadComponent target) => string.Equals(target.Kind, "close", StringComparison.Ordinal);

    /// <summary>
    /// Review B2: a generic hold whose pin re-check found a named harm after admission ends on the existing zero-press
    /// TARGET_GONE rejection; its reason then names the harm (<c>TARGET_GONE:GENERIC_HARM:destroys_crop:10,11</c>).
    /// null = leave the reason as it is.
    /// </summary>
    public static string? GoneLabel(string? reason, string? harmFound) =>
        harmFound is not null && string.Equals(reason, BgHoldReason.TargetGone, StringComparison.Ordinal)
            ? BgHoldReason.TargetGone + ":" + harmFound : null;

    /// <summary>W1 B2: a release reason that is a harm found after admission (<c>TARGET_GONE:GENERIC_HARM:...</c>).</summary>
    public static bool IsHarmGone(string? reason) =>
        reason?.StartsWith(BgHoldReason.TargetGone + ":" + Prefix + ":", StringComparison.Ordinal) == true;

    /// <summary>Review B2: the terminal label of that rejection (<c>PRECONDITION_UNAVAILABLE(TARGET_GONE)</c>) names the harm.</summary>
    public static string GoneTerminalLabel(string label, string? harmFound) => harmFound is null ? label
        : label.Replace("(" + BgHoldReason.TargetGone + ")", "(" + BgHoldReason.TargetGone + ":" + harmFound + ")", StringComparison.Ordinal);

    public const string BlindPurchase = "blind_purchase", BlindSale = "blind_sale", BlindShip = "blind_ship", BlindDispose = "blind_dispose";

    /// <summary>Review B1: the menu's facts cannot tell what a press without a named choice would do (fail closed).</summary>
    public const string BlindUnclassified = "blind_unclassified";

    /// <summary>One plain sentence for a refusal code (logs and the ADR; the code itself is the wire).</summary>
    public static string Words(string refusal)
    {
        string[] p = refusal.Split(':');
        string code = p.Length > 1 ? p[1] : "";
        return code switch
        {
            GiftToVillager when p.Length > 3 => "the action button would hand the " + p[3] + " in your hand to " + p[2] + " (a gift)",
            ShipHeldItem when p.Length > 2 => "the action button would put the " + p[2] + " in your hand into the shipping bin (sold tonight)",
            DestroysCrop when p.Length > 2 => "this tool would destroy the crop on tile " + p[2],
            DestroysSapling when p.Length > 2 => "this tool would destroy the sapling on tile " + p[2],
            DestroysFruitTree when p.Length > 2 => "this tool would cut the fruit tree on tile " + p[2],
            SweepDestroys when p.Length > 2 => "the swing would sweep over a crop or a sapling next to tile " + p[2],
            FactsUnreadable => "the game's tile could not be read, so the press is not made",
            BlindPurchase when p.Length > 3 => "a press without a named choice would buy " + p[2] + " for " + p[3],
            BlindSale when p.Length > 3 => "a press without a named choice would sell " + p[2] + " for " + p[3] + " each",
            BlindShip when p.Length > 2 => "a press without a named choice would ship " + p[2],
            BlindDispose when p.Length > 3 => "a press without a named choice would " + (p[2] == "trash" ? "trash " : "drop ") + p[3],
            BlindUnclassified when p.Length > 2 => "the menu's facts cannot tell what a press without a named choice would do here ("
                + p[2].Replace('_', ' ') + "); name the part to press it",
            _ => refusal,
        };
    }
}

/// <summary>
/// ADR 246: her way out of a running minigame, in the game's own order (the UI motor's close with no menu up). First
/// the minigame's abort, <c>IMinigame.forceQuit()</c>, the path vanilla itself uses on a warp or at the end of a day;
/// when the minigame refuses it, the key a player presses to leave (Escape) goes to the minigame itself through the
/// motor frame, and the minigame's own tick ends it. The press's readback then says whether it ended.
/// </summary>
public static class MinigameExit
{
    public const string Closed = "Closed";
    public const string EscapeKey = "EscapeKey";

    /// <summary><paramref name="sameMinigameRunning"/>: Game1.currentMinigame is still the minigame that was asked.</summary>
    public static string After(bool forceQuitAccepted, bool sameMinigameRunning) =>
        forceQuitAccepted || !sameMinigameRunning ? Closed : EscapeKey;

    public const string ReceiptClosed = "closed", ReceiptStillRunning = "still_running", ReceiptReplaced = "replaced";

    /// <summary>
    /// Review S3: what an Escape aimed at a minigame did, judged by the minigame pinned at admission. Other menu or HUD
    /// changes in the same frames do not count. <paramref name="sameRunning"/>: Game1.currentMinigame is still that minigame.
    /// </summary>
    public static string Receipt(bool anyRunning, bool sameRunning) =>
        sameRunning ? ReceiptStillRunning : anyRunning ? ReceiptReplaced : ReceiptClosed;
}
