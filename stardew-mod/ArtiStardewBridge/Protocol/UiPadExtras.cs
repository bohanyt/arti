namespace ArtiStardewBridge.Protocol;

// Primitives v2 WS-G6 menu contexts on WS-G1's ui_nav / ui_press layer (PLAN 1.2 rows JunimoNoteMenu,
// MineElevatorMenu / minecart, CarpenterMenu / MuseumMenu; WS-G6 brief section 2). Pure and SMAPI-free: the per-context
// allowlist of what A / B / X do on the snapped component; every other cell is refused BEFORE any call.
//   MineElevatorMenu: A on a floor button = MineElevatorMenu.receiveLeftClick (MineElevatorMenu.cs:69-100: enterMine /
//     warp to the Mine entrance), gated by the WS-G5 mirrored time rule (no deeper floor after 20:00, nothing after 01:00).
//   JunimoNoteMenu: the menu's own held-item path, never a drag (JunimoNoteMenu.cs:430-597): A on a bundle opens its
//     page; A on an inventory slot picks the stack into heldItem (inventory.leftClick); A on an ingredient slot that
//     accepts the held item deposits it (tryToDepositThisItem); back / close only with nothing held. Static canClick,
//     scrambled text (no Junimo letter yet), a bundle completing, partial donations, purchase (money) and reward
//     buttons are refused. A click outside the menu with an item held would drop it (:589-594): clicks are always at
//     the snapped component's centre.
//   MuseumMenu: A on a donatable inventory slot picks ONE item (MuseumMenu.cs:186-198); with an item held, A places it
//     on a free suitable museum tile the mod picks (museumPieces.Add, :208-249). Rearranging donated pieces and B (it
//     would skip the menu's fade-out that restores viewportFreeze) are refused; leave with A on OK.
// Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G6 live witnesses.

/// <summary>Read-only facts of the G6 menus (null on UiPadFacts = flag off).</summary>
public sealed record UiPadExtrasFacts(
    int TimeOfDay = 900,
    int CurrentMineLevel = 0,
    bool JunimoSpecificPage = false,
    bool JunimoCanClick = true,
    bool JunimoScrambled = false,
    bool JunimoPartialDonation = false,
    bool JunimoBundleBusy = false,
    IReadOnlyCollection<int>? AcceptingSlots = null,
    bool MuseumReady = false,
    bool MuseumHoldingPiece = false,
    bool MuseumFreeTile = false,
    IReadOnlyCollection<int>? DonatableSlots = null,
    BundlePageFacts? BundlePage = null,
    // W1 gap B4 S28: the quest a mail LetterViewerMenu presents (Protocol/QuestFacts.cs); null for every other menu.
    LetterQuestFacts? LetterQuest = null);

// Donation slots are interchangeable; required ingredient icons are a separate list.
public sealed record BundleIngredientFacts(int Index, string Item, int Need, int Quality, bool Completed,
    IReadOnlyList<int> MatchingSlots, string Name = "", bool? PersistentCompleted = null);
public sealed record BundlePageFacts(int Index, string Name, bool DepositsAllowed,
    IReadOnlyList<BundleIngredientFacts> Ingredients, IReadOnlyList<int> EmptySlots, IReadOnlyList<int> AcceptingSlots)
{
    public Dictionary<string, object> Snapshot() => new()
    {
        ["index"] = this.Index, ["name"] = UiPadTable.Trim(this.Name), ["deposits_allowed"] = this.DepositsAllowed,
        ["empty_slots"] = this.EmptySlots, ["accepting_slots"] = this.AcceptingSlots,
        ["ingredients"] = this.Ingredients.Select(i => new Dictionary<string, object?>
        {
            ["index"] = i.Index, ["item"] = i.Item, ["name"] = UiPadTable.Trim(i.Name), ["need"] = i.Need, ["quality"] = i.Quality,
            ["completed"] = i.Completed, ["persistent_completed"] = i.PersistentCompleted, ["matching_slots"] = i.MatchingSlots,
        }).ToArray(),
    };
}

public static class JunimoNavigation
{
    // IClickableMenu.automaticSnapBehavior, Junimo specific page: no alignment weighting.
    // The caller supplies candidates filtered through the native pure IsAutomaticSnapValid.
    public static int Resolve(UiPadComponent from, int direction, IEnumerable<UiPadComponent> candidates, bool prioritizeAligned = false)
    {
        int result = from.Id;
        float best = -1;
        foreach (UiPadComponent c in candidates)
        {
            if (!c.Visible || c.Id == from.Id || c.Id == -500 || c.Kind == UiPadComponent.KindClose
                || (c.Up == -1 && c.Right == -1 && c.Down == -1 && c.Left == -1)) continue;
            float x = c.CenterX - from.CenterX, y = c.CenterY - from.CenterY;
            float distance = x * x + y * y;
            float dot = (direction switch { 0 => -y, 1 => x, 2 => y, _ => -x }) / MathF.Sqrt(distance);
            if (!(dot > .01f)) continue;
            // IClickableMenu.cs:583-602; the caller supplies the owner's native alignment hook.
            if (prioritizeAligned && (dot > .99999f || (direction is 0 or 2 ? MathF.Abs(x) < 32 : MathF.Abs(y) < 32)))
                distance *= .01f;
            if (best == -1 || distance < best) { best = distance; result = c.Id; }
        }
        return result;
    }
}

public static class BundleDonation
{
    public static bool Observed(UiPadFacts before, UiPadFacts after)
    {
        if (before.Extras?.BundlePage is not { } b || after.Extras?.BundlePage is not { } a
            || before.ReceiverToken != after.ReceiverToken || !after.MenuOpen || b.Index != a.Index
            || before.HeldItemId is null || before.SnappedId is not int slot || !b.AcceptingSlots.Contains(slot)
            || b.Ingredients.Count != a.Ingredients.Count || a.EmptySlots.Count != b.EmptySlots.Count - 1) return false;
        var filled = before.Comps.Count(c => c.Kind == UiPadExtrasTable.KindIngredient && c.ItemId is not null);
        if (after.Comps.Count(c => c.Kind == UiPadExtrasTable.KindIngredient && c.ItemId is not null) != filled + 1) return false;
        var paired = b.Ingredients.Zip(a.Ingredients).ToArray();
        if (paired.Any(p => p.First.Index != p.Second.Index || p.First.Item != p.Second.Item
            || p.First.Need != p.Second.Need || p.First.Quality != p.Second.Quality)) return false;
        var changed = paired.Where(p => p.First.Completed != p.Second.Completed).ToArray();
        // Bundle.cs writes the menu description first, then the independent CommunityCenter netfield.
        // NetArray<bool, NetBool>'s local setter is synchronous even on the farmhand; no host round-trip is needed.
        if (changed.Length != 1 || changed[0].First.Completed || !changed[0].Second.Completed
            || changed[0].First.PersistentCompleted is not false || changed[0].Second.PersistentCompleted is not true) return false;
        int need = changed[0].First.Need;
        var deposited = after.Comps.FirstOrDefault(c => c.Id == slot);
        bool category = int.TryParse(changed[0].First.Item, out int cat) && cat < 0;
        // Vanilla displays a representative object for category ingredients, not necessarily the donated QID.
        if (deposited?.ItemId is null || (!category && deposited.ItemId != before.HeldItemId) || deposited.Stack != need) return false;
        int remaining = before.HeldStack - need;
        if (remaining < 0 || (remaining == 0 ? after.HeldItemId is not null
                : after.HeldItemId != before.HeldItemId || after.HeldStack != remaining)) return false;
        // Inventory counts must be conserved during the deposit (pick already removed the stack from the bag).
        int Count(UiPadFacts f) => f.Comps.Where(c => c.Kind == UiPadComponent.KindSlot && c.ItemId == before.HeldItemId).Sum(c => c.Stack);
        return Count(before) == Count(after);
    }
}

public static class UiPadExtrasTable
{
    // Component kinds the mod labels in these menus (UiPadComponent.Kind).
    public const string KindFloor = "floor", KindBundle = "bundle", KindIngredient = "ingredient", KindPurchase = "purchase",
        KindPresent = "present", KindBack = "back", KindArea = "area", KindOk = "ok";

    /// <summary>InventoryPage.junimoNoteIcon myID (InventoryPage.cs:158-163).</summary>
    public const int JunimoNoteIconId = 898;

    static string Deny(string why) => UiPadContextTable.Deny(why);
    static string None(string why) => UiPadContextTable.None(why);

    public static UiPadPlan MineElevator(string button, UiPadFacts f)
    {
        UiPadExtrasFacts x = f.Extras ?? new UiPadExtrasFacts();
        switch (button)
        {
            case UiPadTable.BtnB:
                return f.ReadyToClose == true ? UiPadContextTable.MirrorClose(button, "close the elevator")
                    : UiPadPlan.No(button, Deny("not_ready_to_close"));
            case UiPadTable.BtnA:
                if (f.Snapped is not { } c)
                    return UiPadPlan.No(button, Deny("nothing_snapped"));
                if (c.Kind == UiPadComponent.KindClose)
                    return UiPadContextTable.MirrorClick(button, "close the elevator");
                if (c.Kind != KindFloor || !int.TryParse(c.Label, out int floor) || floor < 0)
                    return UiPadPlan.No(button, Deny("component_not_allowlisted"));
                return WorldExtras.ElevatorFloorRefusal(floor, x.CurrentMineLevel, x.TimeOfDay) is { } refused
                    ? UiPadPlan.No(button, Deny(refused))
                    : UiPadContextTable.MirrorClick(button, floor == 0 ? "ride the elevator up to the Mines entrance" : "ride the elevator to floor " + floor);
            default:
                return UiPadPlan.No(button, None("mine_elevator"));
        }
    }

    public static UiPadPlan JunimoNote(string button, UiPadFacts f)
    {
        UiPadExtrasFacts x = f.Extras ?? new UiPadExtrasFacts();
        if (button is not (UiPadTable.BtnA or UiPadTable.BtnB or UiPadTable.BtnX))
            return UiPadPlan.No(button, None("junimo_note"));
        if (!x.JunimoCanClick)
            return UiPadPlan.No(button, Deny("not_ready:can_click"));
        if (x.JunimoScrambled)
            return UiPadPlan.No(button, Deny("scrambled_text"));
        if (x.JunimoBundleBusy)
            return UiPadPlan.No(button, Deny("not_ready:bundle_completing"));
        bool held = f.HeldItemId is not null;
        if (button == UiPadTable.BtnX)
            return UiPadPlan.No(button, Deny("partial_donation_refused"));
        if (button == UiPadTable.BtnB)
            return held ? UiPadPlan.No(button, Deny("item_held"))
                : UiPadContextTable.MirrorClose(button, x.JunimoSpecificPage ? "back to the bundles" : "close the bundle menu");
        if (f.Snapped is not { } c)
            return UiPadPlan.No(button, Deny("nothing_snapped"));
        if (x.JunimoPartialDonation)
            return UiPadPlan.No(button, Deny("partial_donation_refused"));
        if (!x.JunimoSpecificPage)
            return c.Kind == KindBundle ? UiPadContextTable.MirrorClick(button, "open the bundle page")
                : c.Kind == UiPadComponent.KindClose ? UiPadContextTable.MirrorClick(button, "close the bundle menu")
                : UiPadPlan.No(button, Deny("component_not_allowlisted"));
        return c.Kind switch
        {
            UiPadComponent.KindSlot => UiPadContextTable.MirrorClick(button, held ? "put the held stack back / swap" : "pick up the stack"),
            KindIngredient when held && (x.AcceptingSlots?.Contains(c.Id) ?? false) =>
                UiPadContextTable.MirrorClick(button, "put the held item into the bundle slot"),
            KindIngredient => UiPadPlan.No(button, Deny(held ? "ingredient_not_accepting" : "nothing_held")),
            KindBack or UiPadComponent.KindClose => held ? UiPadPlan.No(button, Deny("item_held"))
                : UiPadContextTable.MirrorClick(button, "back to the bundles"),
            _ => UiPadPlan.No(button, Deny("component_not_allowlisted")),
        };
    }

    public static UiPadPlan Museum(string button, UiPadFacts f)
    {
        UiPadExtrasFacts x = f.Extras ?? new UiPadExtrasFacts();
        if (button == UiPadTable.BtnB)
            return UiPadPlan.No(button, Deny("use_ok_button"));
        if (button != UiPadTable.BtnA)
            return UiPadPlan.No(button, None("museum"));
        if (!x.MuseumReady)
            return UiPadPlan.No(button, Deny("not_ready:museum_fade"));
        if (x.MuseumHoldingPiece)
            return UiPadPlan.No(button, Deny("rearranging_refused"));
        if (f.HeldItemId is not null)
            return x.MuseumFreeTile
                ? UiPadPlan.Ok(button, "donate: place the held item on a free museum spot", new UiPadStep(UiPadStepKind.MuseumPlace, button))
                : UiPadPlan.No(button, Deny("no_free_visible_tile"));
        if (f.Snapped is not { } c)
            return UiPadPlan.No(button, Deny("nothing_snapped"));
        if (c.Kind == KindOk)
            return f.ReadyToClose == true ? UiPadContextTable.MirrorClick(button, "OK (leave the museum menu)")
                : UiPadPlan.No(button, Deny("not_ready_to_close"));
        if (c.Kind == UiPadComponent.KindSlot && (x.DonatableSlots?.Contains(c.Id) ?? false))
            return UiPadContextTable.MirrorClick(button, "pick one item to donate");
        return UiPadPlan.No(button, Deny(c.Kind == UiPadComponent.KindSlot ? "not_donatable" : "component_not_allowlisted"));
    }
}
