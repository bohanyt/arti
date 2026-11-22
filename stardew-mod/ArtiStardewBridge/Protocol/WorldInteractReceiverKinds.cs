namespace ArtiStardewBridge.Protocol;

// WS3b (Primitives v1, 2026-10-07): world_interact kinds whose press opens a DialogueBox
// INSIDE the press frame (npc talk, eat prompt) and the bed variant of move_through (the
// Sleep touch action opens its question mid-walk). Pure, SMAPI-free decision logic only;
// the game-coupled sampling lives in ModEntry.InteractB.cs / BackgroundReceiverRuntime.cs.
// Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the CT live witness.
//
// Decompiled 1.6.15 facts this layer encodes:
// - NPC.checkAction (NPC.cs ~2465) -> Game1.drawDialogue -> activeClickableMenu = DialogueBox in
//   the same Game1._update as the press; holding an object there would be a GIFT, so capture
//   requires a plain tool in hand.
// - Game1.pressActionButton (Game1.cs ~11460): with an edible ActiveObject and nothing to
//   check at the grab tile / below / above / own tile, createQuestionDialogue(..., "Eat") in
//   the same frame; player.faceDirection(2) turns the farmer DOWN (facing drift by design).
// - Game1.activeClickableMenu setter calls player.Halt() (movementDirections cleared) for any
//   non-null menu outside events, so a menu opened mid-move leaves no held movement.
// - Game1._update runs UpdateControlInput only while activeClickableMenu == null: once the
//   DialogueBox is up no R / N frame is ever consumed (the hold release proof must not wait
//   for them; the retained history keeps the H frame until the next request's N-H).

/// <summary>Per-kind effect rules for the synchronous-receiver kinds (npc, eat).</summary>
public static class BgReceiverEffect
{
    public const string EatEffectMismatch = "EAT_EFFECT_MISMATCH";

    public static bool Handles(BgActionKind kind) => kind is BgActionKind.Npc or BgActionKind.Eat;

    /// <summary>The DialogueBox opens in the press frame; no menu within the bound = no effect.</summary>
    public static bool Settled(BgActionEffectFacts now, long sinceReleaseMs) =>
        now.MenuOpen || sinceReleaseMs >= BgActionEffect.NoTransitionMs;

    public static BgActionEffectVerdict Classify(BgActionKind kind, BgActionEffectFacts? before, BgActionEffectFacts? now)
    {
        if (before is null || now is null)
            return Uncertain(BgActionEffect.EffectUnread);
        if (!string.Equals(now.LocationToken, before.LocationToken, StringComparison.Ordinal))
            return Uncertain(BgActionEffect.UnexpectedLocation + now.LocationToken);
        if (now.MenuOpen)
        {
            if (!now.ExpectedReceiver)
                return Uncertain(BgActionEffect.UnexpectedMenu + (now.MenuType ?? "unknown"));
            // Eat: the question only; nothing may be eaten before the answer.
            if (kind == BgActionKind.Eat && now.ItemCount != before.ItemCount)
                return Uncertain(EatEffectMismatch);
            return new BgActionEffectVerdict(kind == BgActionKind.Npc ? WorldInteractionKinds.DialogueOpened
                : WorldInteractionKinds.QuestionOpened, true, null, null, 0);
        }
        if (kind == BgActionKind.Eat && now.ItemCount != before.ItemCount)
            return Uncertain(EatEffectMismatch);
        // Nothing opened and nothing changed (NPC without dialogue / emote only, eat refused).
        return new BgActionEffectVerdict(BgActionEffect.InteractionNoEffect, false, null, null, 0);
    }

    static BgActionEffectVerdict Uncertain(string reason) =>
        new(BgHoldCompletion.InteractionUncertain, false, reason, null, 0);
}

/// <summary>Synchronous receiver rules for the ACTION hold (npc / eat).</summary>
public static class BgSyncReceiver
{
    // WS-G6: the shaft / bus / minecart question and the MineElevatorMenu open inside the press frame too.
    // ADR 136: so does a found quest item's dialogue. A forage counts only with a named receiver: every rule below needs one observed.
    public static bool IsSynchronous(BgActionKind kind) => kind is BgActionKind.Npc or BgActionKind.Eat or BgActionKind.ExtraReceiver or BgActionKind.EventReceiver or BgActionKind.Generic or BgActionKind.Forage;

    /// <summary>
    /// The H frame was consumed and the expected receiver opened before any R frame: the
    /// game will not run UpdateControlInput again while it is up (Game1.cs ~4286).
    /// </summary>
    public static bool FramesDone(BgActionKind kind, int consumedH, bool rConsumed, bool receiverObserved) =>
        IsSynchronous(kind) && consumedH == 1 && !rConsumed && receiverObserved;

    /// <summary>
    /// History at the final readback: the canonical baseline, or (synchronous receiver only)
    /// exactly the consumed H frame, which the game wrote as Game1.old* in that frame and can
    /// no longer overwrite while the DialogueBox is up. Never any third state.
    /// </summary>
    public static bool HistoryAcceptable(BgActionKind kind, bool receiverObserved, bool rConsumed,
        bool historyIsBaseline, bool historyIsConsumedH) =>
        historyIsBaseline || (IsSynchronous(kind) && receiverObserved && !rConsumed && historyIsConsumedH);
}

/// <summary>Read-only facts of a villager at the grab tile (talk capture).</summary>
public sealed record BgNpcTalkFacts(
    string Name,
    bool Villager,
    bool CanSocialize,
    bool Invisible,
    bool Sleeping,
    bool TalkedToday,
    bool IsSpouse,
    bool InEvent,
    int CandidatesAtTile,
    int OtherVillagersAtFallbackTiles);

public static class BgNpcTalk
{
    /// <summary>
    /// Special-cased in NPC.checkAction (shops, quests, prompts, emotes) or not a plain talk:
    /// never offered. Names are internal names.
    /// </summary>
    public static readonly IReadOnlyCollection<string> ExcludedNames = new HashSet<string>(StringComparer.Ordinal)
    {
        "Krobus", "Dwarf", "Henchman", "Bouncer", "Leo", "Fizz", "Mister Qi", "Gunther", "Marlon", "Morris",
        // Review 2026-10-07: the forest raccoons (Raccoon class: bundle / shop menus, never plain talk).
        "Raccoon", "MrsRaccoon",
    };

    public static string? Miss(BgNpcTalkFacts f)
    {
        if (f.CandidatesAtTile == 0)
            return "no_villager_at_grab_tile";
        if (f.CandidatesAtTile > 1)
            return "several_npcs_at_grab_tile";
        if (!f.Villager || !f.CanSocialize)
            return "npc_not_villager";
        if (ExcludedNames.Contains(f.Name))
            return "npc_special_case";
        if (f.Invisible)
            return "npc_invisible";
        if (f.Sleeping)
            return "npc_sleeping";
        if (f.IsSpouse)
            return "npc_spouse";
        if (f.InEvent)
            return "event_or_festival";
        if (f.TalkedToday)
            return "npc_talked_today";
        if (f.OtherVillagersAtFallbackTiles > 0)
            return "npc_at_fallback_tile";
        return null;
    }
}

/// <summary>Read-only facts for the eat prompt capture (held item + the four checked tiles).</summary>
public sealed record BgEatFacts(
    bool HeldIsObject,
    int Edibility,
    bool QuestItem,
    bool HasObjectData,
    bool Placeable,
    bool IsFurniture,
    bool CanEatNow,
    bool EatBlocked,
    string? SiteMiss);

/// <summary>
/// Review 2026-10-07 (MAJOR 1 / MINOR 4): what may ever be eaten. Cheap everyday food only — never
/// a valuable / artisan / bundle / quest item, and never an item pressActionButton would USE first
/// (Object.performUseAction, Object.cs ~3464: books -102/-103, "Totem" names, secret notes 79/842,
/// horse flute 911, monster musk 879). Pure; the mod applies it to the held item and to every
/// published eat_candidate.
/// </summary>
public sealed record BgEatCandidateFacts(
    int Edibility,
    int Price,
    int Category,
    bool QuestItem,
    bool HasObjectData,
    bool Placeable,
    bool IsFurniture,
    bool Usable,
    IReadOnlyCollection<string> ContextTags);

public static class BgEatPrompt
{
    /// <summary>FIXB18 review MAJOR: ui.context "eat_full" = stamina AND health at their maximum (eating gains nothing).
    /// Only this bool is published: it changes when ARTI crosses "full", never per swing or hit (no context churn).</summary>
    public static bool VitalsFull(float stamina, int maxStamina, int health, int maxHealth) =>
        maxStamina > 0 && maxHealth > 0 && stamina >= maxStamina && health >= maxHealth;

    public const string QuestionKey = "Eat";
    public const int MaxPrice = 100;
    /// <summary>FINAL B7 (J3156-J3176: HP 65/100 on UndergroundMine3, the 10 staged Salads (sell 220g) on the hotbar
    /// were refused as held_item_valuable, the backpack Parsnip's staging was refused next to a monster and S15 ended
    /// no_food_candidate): below this share of max HP a HOTBAR food up to <see cref="SurvivalMaxPrice"/> is a candidate.
    /// Mirror of arti_stardew_skills_mines.SURVIVAL_HP_PCT. The backpack stays cheap-only.</summary>
    public const int SurvivalHealthPct = 80;
    public const int SurvivalMaxPrice = 300;

    /// <summary>One stable bool: changes only when HP crosses the survival line (no per-hit context churn).</summary>
    public static bool Survival(int health, int maxHealth) =>
        maxHealth > 0 && health > 0 && (long)health * 100 < (long)SurvivalHealthPct * maxHealth;
    public const int ArtisanCategory = -26;
    static readonly HashSet<string> UsableIds = new(StringComparer.Ordinal) { "(O)79", "(O)842", "(O)911", "(O)879" };

    /// <summary>Read-only mirror of the Object.performUseAction branches that return true.</summary>
    public static bool UsableItem(string? qualifiedId, string? name, int category) =>
        category is -102 or -103 || (name ?? "").Contains("Totem", StringComparison.Ordinal)
        || (qualifiedId is not null && UsableIds.Contains(qualifiedId));

    public static string? CandidateMiss(BgEatCandidateFacts f, bool survival = false)
    {
        if (f.IsFurniture)
            return "held_item_not_food";
        if (f.Edibility <= 0)
            return "held_item_not_edible";
        if (f.QuestItem || f.ContextTags.Any(t => t.Contains("quest", StringComparison.OrdinalIgnoreCase)))
            return "held_item_quest";
        if (f.ContextTags.Any(t => t.EndsWith("_bundle", StringComparison.OrdinalIgnoreCase)))
            return "held_item_bundle";
        if (f.Category == ArtisanCategory)
            return "held_item_artisan";
        if (f.Price > (survival ? SurvivalMaxPrice : MaxPrice))
            return "held_item_valuable";
        if (!f.HasObjectData)
            return "held_item_no_object_data";
        if (f.Placeable)
            return "held_item_placeable";
        if (f.Usable)
            return "held_item_usable";
        return null;
    }

    public static string? Miss(BgEatFacts f)
    {
        if (!f.HeldIsObject || f.IsFurniture)
            return "held_item_not_food";
        if (f.Edibility <= 0)
            return "held_item_not_edible";
        if (f.QuestItem)
            return "held_item_quest";
        if (!f.HasObjectData)
            return "held_item_no_object_data";
        if (f.Placeable)
            return "held_item_placeable";
        if (!f.CanEatNow)
            return "cannot_eat_now";
        if (f.EatBlocked)
            return "eat_blocked";
        return f.SiteMiss;
    }
}

/// <summary>D25: facing order for the eat-site hint (current facing first, then up, right, down, left).</summary>
public static class BgEatSite
{
    public static (int Dx, int Dy) Vector(int facing) => facing switch
    {
        0 => (0, -1),
        1 => (1, 0),
        2 => (0, 1),
        3 => (-1, 0),
        _ => (0, 0),
    };

    public static IReadOnlyList<int> Order(int currentFacing) =>
        new[] { currentFacing, 0, 1, 2, 3 }.Where(f => f is >= 0 and <= 3).Distinct().ToArray();
}

/// <summary>
/// D27: the tile checkAction's NPC box test always hits — the bounding-box centre tile (Character.TilePoint
/// = StandingPixel / 64, StandingPixel = GetBoundingBox().Center; NPC box = (X+8, Y+16, spriteWidth*3, 32)).
/// </summary>
public static class BgNpcTile
{
    public static (int X, int Y) CentreTile(float positionX, float positionY, int spriteWidth = 16)
    {
        int width = spriteWidth * 4 * 3 / 4;
        return (((int)positionX + 8 + width / 2) / 64, ((int)positionY + 16 + 16) / 64);
    }

    public static bool BoxHitsTile(float positionX, float positionY, int tileX, int tileY, int spriteWidth = 16)
    {
        int width = spriteWidth * 4 * 3 / 4;
        int bx = (int)positionX + 8, by = (int)positionY + 16;
        int tx = tileX * 64, ty = tileY * 64;
        return tx < bx + width && bx < tx + 64 && ty < by + 32 && by < ty + 64;
    }
}

/// <summary>Facts for the bed move_through terminal (sampled every UpdateTicked of a bed run).</summary>
public sealed record BgBedFacts(
    bool TouchSeen,
    bool SleepQuestionOpen,
    bool OtherMenuOpen,
    bool SameLocation,
    bool LatchRemoved,
    bool LatchRestoredExact,
    bool ContainmentSettled,
    int MovementDirections,
    long SinceTouchMs);

/// <summary>
/// move_through kind "bed" (brief section 3.7): the move run walks along the bed's sleep row;
/// GameLocation.UpdateWhenCurrentLocation fires TouchAction Sleep when the farmer ENTERS the
/// sleep tile and createQuestionDialogue(..., "Sleep") opens mid-walk. The question's menu
/// setter halts the farmer, so no R / N frame follows: the run ends at the question once the
/// latch is removed and containment released.
/// </summary>
public static class BgBedThrough
{
    public const string QuestionKey = "Sleep";
    public const string TouchQuestionSleep = "TOUCH_QUESTION:Sleep";
    public const long SettleBoundMs = 3000;

    /// <summary>
    /// The sleep tile lies on the bed's middle row, entered sideways; the offset of a single bed
    /// follows the farmer's side (BedFurniture.UpdateBedTile), so it may be one or two tiles away.
    /// </summary>
    public static bool ReachValid(int dx, int dy, int toTileX, int toTileY) =>
        dy == 0 && dx != 0 && toTileY == 0 && (toTileX == dx || toTileX == 2 * dx);

    public static string? Decide(BgBedFacts f)
    {
        if (!f.TouchSeen)
            return null;
        if (!f.SameLocation)
            return "UNCERTAIN(" + BgActionEffect.UnexpectedLocation.TrimEnd(':') + ")";
        if (f.OtherMenuOpen)
            return "UNCERTAIN(" + BgActionEffect.UnexpectedMenu.TrimEnd(':') + ")";
        if (!f.SleepQuestionOpen)
            return "UNCERTAIN(TOUCH_QUESTION_LOST)";
        if (f.LatchRemoved && f.LatchRestoredExact && f.ContainmentSettled && f.MovementDirections == 0)
            return TouchQuestionSleep;
        if (f.SinceTouchMs >= SettleBoundMs)
            return "UNCERTAIN(RELEASE_UNVERIFIED)";
        return null;
    }
}
