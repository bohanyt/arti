namespace ArtiStardewBridge.Protocol;

// Primitives v2 WS-G0 slice 2 (PLAN.md section 1.1 rules 1/4/7, 1.2 world row, 1.4 world denylist;
// WS-G0 brief section 3 items 4-5): the per-context translation table of the abstract pad buttons.
// It lives in the mod (never in the model or the prompt). Pure and SMAPI-free: the adapter samples
// the context and the held item; this table answers what a button means and refuses every "—"
// cell and every denylist row BEFORE any Press or mirror call. Menus are WS-G1's: every button in a
// menu context is NoMeaning here (G1 adds the menu rows). Evidence ceiling: UNIT_TESTED.
//
// Decomp (Stardew 1.6.15, G1 = StardewValley/Game1.cs):
//   A edge -> pressActionButton at the grab tile (cursor hidden); X edge -> pressUseToolButton at the
//   faced tile; with an object held X places it (G1:11654-11673) and A places / gifts / eats it
//   (G1:11365-11380, NPC.cs:2499/2761, G1:11468-11471); A with a melee weapon and nothing to check
//   is the weapon special (G1:11475-11479). B / Start open the GameMenu (G1:13383-13402); Back opens
//   the QuestLog (G1:13403-13409); Y opens the GameMenu on the crafting tab (G1:13420-13428);
//   shoulders shift the toolbar row (G1:12721-12728, Farmer.shiftToolbar); triggers switch the slot
//   (G1:12689-12697) and are never pad-emulated (probe s3b) -> select_item slot -+1.

/// <summary>The model-facing abstract buttons (PLAN section 1.1, fixed by the owner).</summary>
public static class PadAbstractButton
{
    public const string Stick = "stick";
    public const string A = "A";
    public const string X = "X";
    public const string B = "B";
    public const string Y = "Y";
    public const string LB = "LB";
    public const string RB = "RB";
    public const string LT = "LT";
    public const string RT = "RT";
    public const string Start = "Start";
    public const string Select = "Select";
    public const string SelectSlot = "select_slot";

    public static readonly IReadOnlyList<string> All = new[] { Stick, A, X, B, Y, LB, RB, LT, RT, Start, Select, SelectSlot };
}

/// <summary>Where the farmhand is, as far as the button table is concerned.</summary>
public enum PadContext
{
    /// <summary>No menu, no event, player free: the world row.</summary>
    World,
    /// <summary>A menu (or a dialogue box) is open: WS-G1's rows (NoMeaning here).</summary>
    Menu,
    /// <summary>eventUp / festival / cutscene: refused (owner policy D6).</summary>
    Event,
    /// <summary>World not ready, player busy (using a tool, frozen, warping).</summary>
    Unavailable,
}

/// <summary>Coarse class of the held item (the denylist rows depend on it only).</summary>
public static class PadHeld
{
    public const string None = "none";
    /// <summary>A plain tool (Axe, Pickaxe, Hoe, WateringCan, Scythe (W)47, pan, shears, pail...).</summary>
    public const string Tool = "tool";
    /// <summary>A melee weapon other than the scythe (A with nothing to check = special).</summary>
    public const string MeleeWeapon = "melee_weapon";
    public const string Rod = "rod";
    public const string Slingshot = "slingshot";
    /// <summary>An edible object (A = the Eat question).</summary>
    public const string Food = "food";
    /// <summary>A placeable object (X or A places it).</summary>
    public const string Placeable = "placeable";
    /// <summary>Any other object (A on an NPC = gift / quest delivery).</summary>
    public const string Object = "object";

    /// <summary>Pure classification from read-only facts of Farmer.CurrentItem.</summary>
    public static string Classify(bool isItem, bool isTool, bool isMeleeWeapon, bool isScythe, bool isRod, bool isSlingshot,
        bool isObject, bool edible, bool placeable)
    {
        if (!isItem) return None;
        if (isTool)
        {
            if (isRod) return Rod;
            if (isSlingshot) return Slingshot;
            if (isMeleeWeapon && !isScythe) return MeleeWeapon;
            return Tool;
        }
        if (isObject && edible) return Food;
        if (isObject && placeable) return Placeable;
        return Object;
    }
}

/// <summary>Read-only facts the table needs (sampled by the adapter on the game thread).</summary>
public sealed record PadWorldFacts(
    PadContext Context,
    string Held,
    bool AutoRun,
    int ToolbarSlot,
    bool PadButtonEnabled,
    bool SelectJournalEnabled,
    // WS-G4: with ReflexFishingEnabled on the pad path, X with a rod is the fishing reflex.
    bool ReflexFishEnabled = false,
    // WS-G5: with ReflexCombatEnabled on the pad path, X with a melee weapon is the combat reflex.
    bool ReflexCombatEnabled = false,
    // FIXB31: EventSkipEnabled only (null = off: the event row is unchanged); "" = the skip is allowed now, else the
    // EventSkipPolicy refusal.
    string? EventSkip = null,
    // fix63A: FestivalLeavePolicy.State (null = not a festival: the event row is unchanged).
    string? FestivalLeave = null);

/// <summary>One cell: the family that executes it (and a detail), or the refusal.</summary>
public readonly record struct PadMeaning(string Button, string? Family, string? Detail, string? Refusal)
{
    public bool Allowed => this.Refusal is null;

    public static PadMeaning Ok(string button, string family, string? detail = null) => new(button, family, detail, null);

    public static PadMeaning No(string button, string refusal) => new(button, null, null, refusal);
}

public static class PadContextTable
{
    /// <summary>"—" cell (PLAN 1.2): the button has no meaning here; zero effect.</summary>
    public const string NoMeaning = "NoMeaning";
    /// <summary>PLAN 1.4 denylist row: the button would act on a held item outside an admitted kind.</summary>
    public const string Denied = "Denied";

    public const string FamilyMove = "move_cardinal";
    public const string FamilyInteract = "world_interact";
    public const string FamilyUseTool = "use_tool";
    public const string FamilySelectItem = "select_item";
    public const string FamilyPadButton = "pad_button";
    public const string FamilyReflexFish = "reflex_fish"; // WS-G4
    public const string FamilyReflexCombat = "reflex_combat"; // WS-G5
    public const string EventSkipDetail = "skipEvent"; // FIXB31: pad_button Select in a plain cutscene
    public const string FestivalLeaveDetail = "festivalLeave"; // fix63A: pad_button Select in festival free roam

    public const int ToolbarRowSlots = 12;

    public static PadContext ContextOf(bool worldReady, bool menuOpen, bool eventUp, bool playerFree)
    {
        if (!worldReady) return PadContext.Unavailable;
        if (eventUp) return PadContext.Event;
        if (menuOpen) return PadContext.Menu;
        return playerFree ? PadContext.World : PadContext.Unavailable;
    }

    /// <summary>Row-relative previous / next slot (vanilla trigger semantics, wrap inside the 12-slot row).</summary>
    public static int AdjacentSlot(int slot, bool next)
    {
        int row = Math.Max(0, slot) / ToolbarRowSlots * ToolbarRowSlots;
        int inRow = Math.Max(0, slot) % ToolbarRowSlots;
        return row + (inRow + (next ? 1 : ToolbarRowSlots - 1)) % ToolbarRowSlots;
    }

    /// <summary>
    /// What <paramref name="button"/> means now. <paramref name="kind"/>: the explicit request kind for A
    /// (world_interact kinds chest/door/forage/npc/eat; null = the generic offer). Unknown buttons and
    /// every non-world context are NoMeaning.
    /// </summary>
    public static PadMeaning Translate(string button, PadWorldFacts f, string? kind = null)
    {
        if (!PadAbstractButton.All.Contains(button, StringComparer.Ordinal))
            return PadMeaning.No(button, NoMeaning + ":unknown_button");
        switch (f.Context)
        {
            case PadContext.Menu: return PadMeaning.No(button, NoMeaning + ":menu_context_g1");
            case PadContext.Event:
                if (button == PadAbstractButton.Select && f.PadButtonEnabled && f.FestivalLeave is "")
                    return PadMeaning.Ok(button, FamilyPadButton, FestivalLeaveDetail);
                if (button == PadAbstractButton.Select && f.PadButtonEnabled && f.FestivalLeave is FestivalLeavePolicy.Seated)
                    return PadMeaning.No(button, Denied + ":festival_leave:" + FestivalLeavePolicy.Seated);
                if (button == PadAbstractButton.Select && f.EventSkip is { } skip)
                {
                    // FIXB31 (EventSkipEnabled): Select = the vanilla gamepad Back skip, on a plain cutscene only.
                    if (!f.PadButtonEnabled) return PadMeaning.No(button, NoMeaning + ":pad_button_off");
                    return skip.Length == 0 ? PadMeaning.Ok(button, FamilyPadButton, EventSkipDetail)
                        : PadMeaning.No(button, Denied + ":event_skip:" + skip);
                }
                return PadMeaning.No(button, NoMeaning + ":event");
            case PadContext.Unavailable: return PadMeaning.No(button, NoMeaning + ":unavailable");
        }
        switch (button)
        {
            case PadAbstractButton.Stick:
                return PadMeaning.Ok(button, FamilyMove, f.AutoRun ? "runs" : "walks");
            case PadAbstractButton.A:
                return WorldA(f, kind);
            case PadAbstractButton.X:
                return WorldExtras.UsesX(kind) ? ExtrasX(f, kind!) : WorldX(f);
            case PadAbstractButton.B:
                // Vanilla B in the world opens the GameMenu (use Start); refused (PLAN 1.2).
                return PadMeaning.No(button, NoMeaning + ":world_b");
            case PadAbstractButton.Y:
                return f.PadButtonEnabled ? PadMeaning.Ok(button, FamilyPadButton, "GameMenu:crafting")
                    : PadMeaning.No(button, NoMeaning + ":pad_button_off");
            case PadAbstractButton.Start:
                return f.PadButtonEnabled ? PadMeaning.Ok(button, FamilyPadButton, "GameMenu:inventory")
                    : PadMeaning.No(button, NoMeaning + ":pad_button_off");
            case PadAbstractButton.Select:
                return !f.PadButtonEnabled ? PadMeaning.No(button, NoMeaning + ":pad_button_off")
                    : f.SelectJournalEnabled ? PadMeaning.Ok(button, FamilyPadButton, "QuestLog")
                    : PadMeaning.No(button, NoMeaning + ":select_journal_off");
            case PadAbstractButton.LB:
            case PadAbstractButton.RB:
                return f.PadButtonEnabled ? PadMeaning.Ok(button, FamilyPadButton, button == PadAbstractButton.LB ? "shiftToolbar:left" : "shiftToolbar:right")
                    : PadMeaning.No(button, NoMeaning + ":pad_button_off");
            case PadAbstractButton.LT:
            case PadAbstractButton.RT:
                return PadMeaning.Ok(button, FamilySelectItem, "slot=" + AdjacentSlot(f.ToolbarSlot, button == PadAbstractButton.RT));
            default: // select_slot
                return PadMeaning.Ok(button, FamilySelectItem, "slot");
        }
    }

    static PadMeaning WorldA(PadWorldFacts f, string? kind)
    {
        const string a = PadAbstractButton.A;
        if (WorldExtras.IsKind(kind))
        {
            // WS-G6 A kinds: plain tool or empty hand only (an object would gift / place / eat / use; a weapon
            // with the target gone would fire its special). The X kinds are never an A press.
            if (WorldExtras.UsesX(kind))
                return PadMeaning.No(a, Denied + ":extras_kind_is_x");
            if (kind == WorldExtras.HayPlace)
                return f.Held is PadHeld.Object or PadHeld.Placeable
                    ? PadMeaning.Ok(a, FamilyInteract, kind) : PadMeaning.No(a, Denied + ":hay_requires_object");
            if (kind is WorldExtras.NpcGift or WorldExtras.QuestDelivery) // W1 gap B4: the same held-stack A on a villager
                return f.Held is PadHeld.Object or PadHeld.Food or PadHeld.Placeable
                    ? PadMeaning.Ok(a, FamilyInteract, kind) : PadMeaning.No(a, Denied + ":gift_requires_object");
            return f.Held switch
            {
                PadHeld.None or PadHeld.Tool => PadMeaning.Ok(a, FamilyInteract, kind),
                PadHeld.MeleeWeapon => PadMeaning.No(a, Denied + ":weapon_special"),
                // FIXB7 review: the shipping bin is a building: pressActionButton's checkAction at the grab tile reaches
                // ShippingBin.doAction before any held-item placement / eat, so the crop to ship may be in hand.
                PadHeld.Object or PadHeld.Food or PadHeld.Placeable when kind == WorldExtras.ShippingBin
                    => PadMeaning.Ok(a, FamilyInteract, kind),
                _ => PadMeaning.No(a, Denied + ":held_item_not_plain_tool"),
            };
        }
        switch (f.Held)
        {
            case PadHeld.Food:
                // A with food held = the Eat question (G1:11468-11471): only the eat routine kind.
                return kind is null or WorldInteractionKinds.Eat ? PadMeaning.Ok(a, FamilyInteract, "eat")
                    : PadMeaning.No(a, Denied + ":food_held_eat_only");
            case PadHeld.Placeable:
                // A places a placeable (G1:11365-11380): only kind place (W9), not admitted in G0.
                return PadMeaning.No(a, Denied + ":place_not_admitted");
            case PadHeld.Object:
                // A on an NPC with an object held = gift / quest delivery (NPC.cs:2499, 2761).
                return kind == WorldInteractionKinds.Npc ? PadMeaning.No(a, Denied + ":gift_not_admitted")
                    : PadMeaning.No(a, Denied + ":held_item_not_plain_tool");
            case PadHeld.MeleeWeapon:
                // A with a melee weapon and nothing to check = the weapon special (G1:11475-11479).
                return PadMeaning.No(a, Denied + ":weapon_special");
            case PadHeld.Rod:
            case PadHeld.Slingshot:
                return PadMeaning.No(a, Denied + ":held_item_not_plain_tool");
        }
        // Plain tool or nothing held: the v1 kinds; eat needs food in hand.
        if (kind == WorldInteractionKinds.Eat)
            return PadMeaning.No(a, Denied + ":eat_needs_food");
        return PadMeaning.Ok(a, FamilyInteract, kind ?? "grab_tile");
    }

    /// <summary>
    /// WS-G6 X kinds (PLAN 1.4 X rows): milk pail / shears on an animal (tool held), a machine drop-in (object held) and
    /// placement (placeable held) are admitted ONLY under their explicit kind; everything else keeps the G0 refusal.
    /// </summary>
    static PadMeaning ExtrasX(PadWorldFacts f, string kind)
    {
        const string x = PadAbstractButton.X;
        bool held = kind switch
        {
            WorldExtras.AnimalMilk or WorldExtras.AnimalShear => f.Held == PadHeld.Tool,
            WorldExtras.PlaceItem => f.Held == PadHeld.Placeable,
            WorldExtras.MachineInsert => f.Held is PadHeld.Object or PadHeld.Food or PadHeld.Placeable,
            _ => false,
        };
        return held ? PadMeaning.Ok(x, FamilyInteract, kind) : PadMeaning.No(x, Denied + ":held_item_mismatch:" + kind);
    }

    static PadMeaning WorldX(PadWorldFacts f)
    {
        const string x = PadAbstractButton.X;
        return f.Held switch
        {
            PadHeld.Tool => PadMeaning.Ok(x, FamilyUseTool, "faced_tile"),
            // X with a placeable places it (G1:11654-11673): only kind place (W9), not admitted in G0.
            PadHeld.Placeable => PadMeaning.No(x, Denied + ":place_not_admitted"),
            PadHeld.MeleeWeapon => f.ReflexCombatEnabled ? PadMeaning.Ok(x, FamilyReflexCombat, "kill_one_monster")
                : PadMeaning.No(x, Denied + ":combat_reflex_g5"),
            PadHeld.Rod => f.ReflexFishEnabled ? PadMeaning.Ok(x, FamilyReflexFish, "catch_one_fish")
                : PadMeaning.No(x, Denied + ":fishing_reflex_g4"),
            PadHeld.Slingshot => PadMeaning.No(x, Denied + ":slingshot_p2"),
            _ => PadMeaning.No(x, NoMeaning + ":no_tool"),
        };
    }

    /// <summary>The whole row for the current context (ui.context "pad.buttons").</summary>
    public static IReadOnlyDictionary<string, PadMeaning> Row(PadWorldFacts f) =>
        PadAbstractButton.All.ToDictionary(b => b, b => Translate(b, f), StringComparer.Ordinal);

    /// <summary>Wire form of one cell: {family, detail} or {refused}.</summary>
    public static Dictionary<string, object> Wire(PadMeaning m)
    {
        var cell = new Dictionary<string, object>(StringComparer.Ordinal);
        if (m.Refusal is { } refused)
        {
            cell["refused"] = refused;
            return cell;
        }
        cell["family"] = m.Family!;
        if (m.Detail is { } detail)
            cell["detail"] = detail;
        return cell;
    }
}
