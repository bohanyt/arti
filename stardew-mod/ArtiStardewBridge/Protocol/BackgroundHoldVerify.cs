namespace ArtiStardewBridge.Protocol;

// V4 Stage 2 background use_tool / world_interact, pure layer.
// Authority: freeze #278/5994543545, correction 2.1 #278/5994935998, correction
// 2.2 #278/5995067151 (wins over 2.1/freeze), dispatch #278/5995122084 R1-R5
// (wins over all). Static installed-IL facts: the three independent Stage 2
// review files (artifacts/stage2-design-review-20261005).
//
// Pure, SMAPI-free logic only: no game/SMAPI types, no input emission, no file
// I/O, no thread, no termination. The game-coupled runtime (S2 layer,
// BackgroundControlInputHooks.cs) samples facts into these records and applies
// the decisions. Evidence ceiling of this layer: UNIT_TESTED.
//
// Mechanism (2.1 section 1 as corrected by 2.2): verified retained-press mode.
// The runtime issues ONE SMAPI Press for ONE pinned keyboard key and later the
// matching Suppress; the S2 hook runs in a VERIFY role (BgHookEntry with a
// BgHoldFramePlan): the retained frame must equal the exact expected owned
// frame, otherwise Parent/action is skipped. Nothing is substituted or written.

public enum BgHoldKind { Tool, Action }

/// <summary>Stage 2 reason vocabulary (refusal/divergence detail only; no new result outcome strings).</summary>
public static class BgHoldReason
{
    public const string NoKeyboardCandidate = "NO_KEYBOARD_CANDIDATE";
    public const string ToolNotWhitelisted = "TOOL_NOT_WHITELISTED";
    public const string CursorNotInert = "CURSOR_NOT_INERT";
    public const string VerifyFrameMismatch = "VERIFY_FRAME_MISMATCH";
    public const string HoldFactsMissing = "HOLD_FACTS_MISSING";
    public const string RetainedHistoryIncoherent = "RETAINED_HISTORY_INCOHERENT";
    public const string InvalidHoldTicks = "INVALID_HOLD_TICKS";
    public const string MovementDuringHold = "MOVEMENT_DURING_HOLD";
    public const string PinDrift = "PIN_DRIFT";
    public const string ContainmentNotArmed = "CONTAINMENT_NOT_ARMED";
    public const string MenuWaitTimeout = "MENU_WAIT_TIMEOUT";
    public const string PressNotConsumed = "PRESS_NOT_CONSUMED";
    public const string ReleaseUnverified = "RELEASE_UNVERIFIED";
    public const string ToolStillInUse = "TOOL_STILL_IN_USE";
    public const string SwingCount = "SWING_COUNT_";
    public const string FramesNotConsumed = "FRAMES_NOT_CONSUMED";
    public const string PressCountWithoutPress = "PRESS_COUNT_WITHOUT_PRESS";
    /// <summary>WS3a: the pinned action target is no longer what the capture resolves (pre-press only).</summary>
    public const string TargetGone = "TARGET_GONE";
    /// <summary>WS-G0 (PLAN 1.5 d): no UpdateControlInput tick with no Press outstanding and a neutral oldPadState before H.</summary>
    public const string PadNeutralTickUnseen = "PAD_NEUTRAL_TICK_UNSEEN";
    /// <summary>WS-G0: the pad path needs the ARTI-owned gamepad mode and the _oldGamepadConnectedState pin.</summary>
    public const string PadModeUnavailable = "PAD_MODE_UNAVAILABLE";
}

/// <summary>
/// TOOL_ONLY / ACTION_ONLY keyboard candidate derivation (freeze section 2 as
/// corrected by 2.1 section 1 / M5 / m5). Keyboard only. MouseLeft / MouseRight
/// are never candidates (the installed input code hardcodes them to use-tool /
/// action / cancel). Keys the installed UpdateControlInput hardcodes outside
/// Options are excluded, as is Escape. A key that also appears in any other
/// Options field (including the movement fields) is AMBIGUOUS_BINDING.
/// </summary>
public static class BgHoldBindings
{
    public const string UseToolField = "useToolButton";
    public const string ActionField = "actionButton";

    /// <summary>
    /// Frozen hardcoded key set read from the installed UpdateControlInput lambda
    /// (independent Stage 2 review Q4.2): key codes 27, 49-55, 66, 72-78, 80, 81,
    /// 84, 85, 89, 113, 115-118, 121, 162, as XNA <c>Keys</c> names.
    /// </summary>
    public static readonly IReadOnlyCollection<string> HardcodedControlKeys = new HashSet<string>(StringComparer.Ordinal)
    {
        "Escape", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "B", "H", "I", "J", "K", "L", "M", "N",
        "P", "Q", "T", "U", "Y", "F2", "F4", "F5", "F6", "F7", "F10", "LeftControl",
    };

    public static string FieldFor(BgHoldKind kind) => kind == BgHoldKind.Tool ? UseToolField : ActionField;

    public static bool IsHardcoded(BgButton b) =>
        b.Device == BgDevice.Keyboard && HardcodedControlKeys.Contains(b.Name);

    /// <summary>Deterministic: the first admissible keyboard key in the kind's Options field order.</summary>
    public static bool TryDerive(BgBindingTable table, BgHoldKind kind, out BgButton key, out string? reason)
    {
        string own = FieldFor(kind);
        bool ambiguous = false;
        foreach (BgButton b in table.Field(own))
        {
            if (b.Device != BgDevice.Keyboard)
                continue; // mouse/pad never candidates
            if (b.IsEscape || IsHardcoded(b))
                continue;
            if (table.FieldsContaining(b).Any(f => !string.Equals(f, own, StringComparison.Ordinal)))
            {
                ambiguous = true;
                continue;
            }
            key = b;
            reason = null;
            return true;
        }
        key = default;
        reason = ambiguous ? BgReason.AmbiguousBinding : BgHoldReason.NoKeyboardCandidate;
        return false;
    }
}

/// <summary>Admission constants (2.1 section 3 / M6; 2.1 section 1 / m4 / M3).</summary>
public static class BgHoldAdmission
{
    /// <summary>Exact runtime type names; subclasses, charged/area tools, weapons, rods, slingshots refused.</summary>
    public static readonly IReadOnlyCollection<string> ToolWhitelist = new HashSet<string>(StringComparer.Ordinal)
    {
        "StardewValley.Tools.Axe",
        "StardewValley.Tools.Pickaxe",
    };

    public static bool ToolWhitelisted(string? exactRuntimeTypeFullName) =>
        exactRuntimeTypeFullName is not null && ToolWhitelist.Contains(exactRuntimeTypeFullName);

    /// <summary>
    /// WS1 (primitives v1): per-kind tick cap after classification. Axe/Pickaxe keep the
    /// Stage 2 bound; the farm tools take at most 4 ticks (~67 ms), far below the
    /// 150 ms strafe, 250 ms auto-repeat and 600 ms charge thresholds.
    /// </summary>
    public static string? ValidateToolKindTicks(BgToolKind kind, int ticks) =>
        ticks >= 1 && ticks <= BgToolClass.MaxTicks(kind) ? null : BgHoldReason.InvalidHoldTicks;

    /// <summary>TOOL ticks in [1, 14] (below the ~15-tick auto-repeat); ACTION exactly 1.</summary>
    public static string? ValidateTicks(BgHoldKind kind, int ticks) => kind == BgHoldKind.Tool
        ? (ticks >= 1 && ticks <= BgHoldFramePlan.ToolMaxTicks ? null : BgHoldReason.InvalidHoldTicks)
        : (ticks == BgHoldFramePlan.ActionTicks ? null : BgHoldReason.InvalidHoldTicks);
}

/// <summary>WS1 tool kinds admitted for a background TOOL hold.</summary>
public enum BgToolKind { Axe, Pickaxe, Hoe, WateringCan, Scythe }

/// <summary>
/// Classification facts sampled read-only from the held tool: exact runtime type full
/// name, QualifiedItemId, UpgradeLevel, enchantment count and (watering can) IsBottomless.
/// </summary>
public readonly record struct BgToolFacts(string? TypeFullName, string? QualifiedItemId, int UpgradeLevel,
    int Enchantments, bool Bottomless);

/// <summary>
/// WS1 tool classification, replacing the type-name whitelist (source evidence from the
/// decompiled 1.6.15 Tool.beginUsing: a basic non-weapon tool ends its use on the press,
/// so a basic hoe/can swings once with no charge). Axe/Pickaxe at any upgrade, always.
/// With the default-OFF farm-tools flag only: basic unenchanted Hoe, basic unenchanted
/// non-bottomless WateringCan, and the basic Scythe MeleeWeapon (W)47. Exact type names:
/// subclasses, other weapons, rods, slingshot, pan, pail, shears are refused.
/// </summary>
public static class BgToolClass
{
    public const string ScytheQualifiedItemId = "(W)47";
    public const int AxePickaxeMaxTicks = BgHoldFramePlan.ToolMaxTicks;
    public const int FarmToolMaxTicks = 4;

    public static BgToolKind? Classify(BgToolFacts f, bool farmToolsEnabled) =>
        Classify(f.TypeFullName, f.QualifiedItemId, f.UpgradeLevel, f.Enchantments, f.Bottomless, farmToolsEnabled);

    public static BgToolKind? Classify(string? typeFullName, string? qid, int upgradeLevel, int enchantments,
        bool bottomless, bool farmToolsEnabled)
    {
        switch (typeFullName)
        {
            case "StardewValley.Tools.Axe": return BgToolKind.Axe;
            case "StardewValley.Tools.Pickaxe": return BgToolKind.Pickaxe;
        }
        if (!farmToolsEnabled)
            return null;
        return typeFullName switch
        {
            "StardewValley.Tools.Hoe" when upgradeLevel == 0 && enchantments == 0 => BgToolKind.Hoe,
            "StardewValley.Tools.WateringCan" when upgradeLevel == 0 && enchantments == 0 && !bottomless => BgToolKind.WateringCan,
            "StardewValley.Tools.MeleeWeapon" when string.Equals(qid, ScytheQualifiedItemId, StringComparison.Ordinal) => BgToolKind.Scythe,
            _ => null,
        };
    }

    /// <summary>
    /// Review fix (WS1 minor 7): the foreground use_tool route has no faced tool-tile guard and a
    /// mouse-dependent hit point, so these farm-tool types are refused there (background only).
    /// </summary>
    public static bool IsFarmToolType(string? typeFullName) => typeFullName is "StardewValley.Tools.Hoe"
        or "StardewValley.Tools.WateringCan" or "StardewValley.Tools.MeleeWeapon";

    public static int MaxTicks(BgToolKind k) =>
        k is BgToolKind.Axe or BgToolKind.Pickaxe ? AxePickaxeMaxTicks : FarmToolMaxTicks;
}

/// <summary>WS1 faced tool-tile guard refusal codes (PreconditionUnavailable details).</summary>
public static class BgToolTileReason
{
    public const string VillagerAtToolTile = "VILLAGER_AT_TOOL_TILE";
    public const string PlacedObjectAtToolTile = "PLACED_OBJECT_AT_TOOL_TILE";
    public const string ToolTileNotAdjacent = "TOOL_TILE_NOT_ADJACENT";
    public const string PickaxeTargetNotStone = "PICKAXE_TARGET_NOT_STONE";
    public const string HoeTileNotTillable = "HOE_TILE_NOT_TILLABLE";
    public const string CanTargetInvalid = "CAN_TARGET_INVALID";
    public const string ScytheTargetInvalid = "SCYTHE_TARGET_INVALID";
    public const string CropAtToolTile = "CROP_AT_TOOL_TILE";
    public const string ScytheAreaUnsafe = "SCYTHE_AREA_UNSAFE";
    public const string StaminaLow = "STAMINA_LOW";
    public const string FactsUnreadable = "TOOL_TILE_FACTS_UNREADABLE";
    /// <summary>
    /// WS-G0 pad path: a furniture bounding box intersects the tool tile. With the cursor hidden,
    /// Game1.pressUseToolButton passes the TOOL point to GameLocation.LowPriorityLeftClick, which picks
    /// up passable furniture there (probe s2: the Cabin rug; G1:11633-11719). Rugs included.
    /// </summary>
    public const string FurnitureAtToolTile = "FURNITURE_AT_TOOL_TILE";
    /// <summary>
    /// WS-G0 pad path, ACTION: non-walkable furniture at the grab tile (checkForAction would sit /
    /// rotate / take it). A rug or other walkable floor furniture holding nothing is harmless there:
    /// pressActionButton never calls LowPriorityLeftClick (v1 rug exemption reasoning).
    /// </summary>
    public const string FurnitureAtGrabTile = "FURNITURE_AT_GRAB_TILE";
}

/// <summary>
/// Read-only facts about the exact tile the held tool will hit (sampled on the game
/// thread at admission). ToolTile = floor(Character.GetToolLocation(ignoreClick: true) / 64);
/// with the cursor beyond radius 1 (cursor-inert) the hit point is facing based.
/// </summary>
public sealed record BgToolTileFacts(
    int PlayerTileX,
    int PlayerTileY,
    int Facing,
    int ToolTileX,
    int ToolTileY,
    bool VillagerAtToolTile,
    bool HasObject,
    string? ObjectQualifiedItemId,
    string? ObjectName,
    string? ObjectType,
    bool ObjectBigCraftable,
    bool ObjectIsBreakableStone,
    bool ObjectIsWeeds,
    bool ObjectIsChest,
    bool ObjectIsArtifactSpot,
    string? TerrainType,
    bool TerrainHoeDirtHasCrop,
    bool Tillable,
    bool WaterRefill,
    int? WaterLeft,
    int? WaterMax,
    float Stamina,
    IReadOnlyList<BgAreaTileFacts>? ScytheArea = null,
    // WS-G0 pad path only (sampled when gamepad input is on; false on the keyboard path): any
    // furniture bounding box intersects the tool tile, rugs included.
    bool FurnitureAtToolTile = false,
    // WS-G0 slice 2 (W27, P2), pad path only: the tool tile is a PetBowl building tile with the
    // "PetBowl" Buildings property (PetBowl.performToolAction waters it, PetBowl.cs:81-90), and its state.
    bool PetBowlAtToolTile = false,
    bool PetBowlWatered = false,
    // ADR 246: a wild tree's growth stage on the tool tile (Tree.growthStage; null = no Tree), for the generic USE harm.
    int? ToolTileTreeGrowthStage = null)
{
    /// <summary>The tool hits exactly the tile next to the player in its facing direction.</summary>
    public bool Adjacent => BgToolTileGuard.FacingDelta(this.Facing) is var (dx, dy)
        && this.ToolTileX == this.PlayerTileX + dx && this.ToolTileY == this.PlayerTileY + dy;
}

/// <summary>
/// One tile of the scythe sweep (sampled only for a held scythe): a wild tree's growth stage
/// (null = no Tree) and whether a HoeDirt there holds a crop.
/// </summary>
public readonly record struct BgAreaTileFacts(int X, int Y, int? TreeGrowthStage, bool HoeDirtHasCrop);

/// <summary>
/// WS1 faced tool-tile guard (pure). First matching rule wins; a refusal is a Rejected
/// terminal before any Press (nothing pressed, runtime zero_effect). Source evidence:
/// decompiled 1.6.15 Game1.pressUseToolButton (villager redirect), Object.performToolAction
/// (heavy hitters pick up Crafting objects), HoeDirt.performToolAction (crop destruction),
/// WateringCan.DoFunction (refill / empty can), Character.GetToolLocation (outer 8 px band).
/// HYPOTHESIS until live.
/// </summary>
public static class BgToolTileGuard
{
    public const float MinStamina = 4f;

    public static (int Dx, int Dy)? FacingDelta(int facing) => facing switch
    {
        0 => (0, -1),
        1 => (1, 0),
        2 => (0, 1),
        3 => (-1, 0),
        _ => null,
    };

    /// <summary>
    /// Pure mirror of Character.GetToolLocation without a click (Farmer bounding box
    /// X = position.X + 8, width 48): the hit point is 72 px left/right of the standing
    /// pixel, or 64 px above/below it. Facing left/right inside the outer 8 px band of the
    /// standing tile hits the tile TWO away. Floor division (negative-safe).
    /// </summary>
    public static (int X, int Y) ToolTileFromStandingPixel(int standingX, int standingY, int facing)
    {
        (int px, int py) = facing switch
        {
            0 => (standingX, standingY - 64),
            1 => (standingX + 72, standingY),
            2 => (standingX, standingY + 64),
            3 => (standingX - 72, standingY),
            _ => (standingX, standingY),
        };
        return (FloorDiv64(px), FloorDiv64(py));
    }

    public static int FloorDiv64(int v) => v >= 0 ? v / 64 : -((-v + 63) / 64);

    /// <summary>
    /// Review fix (WS1 major 1): the scythe damages every border tile of MeleeWeapon.getAreaOfEffect,
    /// which sweeps across the animation frames (decompiled MeleeWeapon.DoDamage). Conservative cover:
    /// the player's tile and its two side tiles, plus the faced tile and its two perpendicular
    /// neighbours. Saplings (Tree.performSproutDestroy) and crops (scythe harvest / dead-crop removal)
    /// in that sweep are unsafe. HYPOTHESIS until live.
    /// </summary>
    public static IReadOnlyList<(int X, int Y)> ScytheAreaTiles(int playerX, int playerY, int facing)
    {
        if (FacingDelta(facing) is not var (dx, dy))
            return Array.Empty<(int, int)>();
        int px = Math.Abs(dy), py = Math.Abs(dx);
        var tiles = new List<(int X, int Y)>(6);
        for (int k = 0; k <= 1; k++)
        {
            int bx = playerX + k * dx, by = playerY + k * dy;
            tiles.Add((bx, by));
            tiles.Add((bx - px, by - py));
            tiles.Add((bx + px, by + py));
        }
        return tiles;
    }

    static bool ScytheAreaUnsafe(BgToolTileFacts f)
    {
        IReadOnlyList<(int X, int Y)> expected = ScytheAreaTiles(f.PlayerTileX, f.PlayerTileY, f.Facing);
        if (f.ScytheArea is null || expected.Count == 0
            || !expected.All(t => f.ScytheArea.Any(a => a.X == t.X && a.Y == t.Y)))
            return true; // not sampled for the whole sweep: fail closed
        return f.ScytheArea.Any(a => (a.TreeGrowthStage is int g && g < 5) || a.HoeDirtHasCrop);
    }

    static bool IsPlacedObject(BgToolTileFacts f) =>
        f.HasObject && (f.ObjectBigCraftable || f.ObjectIsChest
            || string.Equals(f.ObjectType, "Crafting", StringComparison.Ordinal));

    static bool IsRefill(BgToolKind kind, BgToolTileFacts f) =>
        kind == BgToolKind.WateringCan && f.WaterRefill && f.WaterLeft is int left && f.WaterMax is int max && left < max;

    /// <summary>null = admitted; otherwise the refusal code.</summary>
    public static string? Reject(BgToolKind kind, BgToolTileFacts? f)
    {
        if (f is null)
            return BgToolTileReason.FactsUnreadable;
        if (f.VillagerAtToolTile)
            return BgToolTileReason.VillagerAtToolTile;
        if (f.FurnitureAtToolTile)
            return BgToolTileReason.FurnitureAtToolTile;
        if (kind != BgToolKind.Scythe && IsPlacedObject(f))
            return BgToolTileReason.PlacedObjectAtToolTile;
        // Review fix (WS1 major 2): a heavy hitter destroys a planted crop (HoeDirt.performToolAction).
        // Tree tiles are never HoeDirt, so the live M1 axe path is unaffected.
        if (kind is BgToolKind.Axe or BgToolKind.Pickaxe or BgToolKind.Hoe
            && string.Equals(f.TerrainType, "HoeDirt", StringComparison.Ordinal) && f.TerrainHoeDirtHasCrop)
            return BgToolTileReason.CropAtToolTile;
        // Axe keeps the live M1 path unchanged: no adjacency rule.
        if (kind != BgToolKind.Axe && !f.Adjacent)
            return BgToolTileReason.ToolTileNotAdjacent;
        switch (kind)
        {
            case BgToolKind.Pickaxe:
                if (!f.HasObject || !f.ObjectIsBreakableStone
                    || (f.ObjectName ?? "").Contains("Boulder", StringComparison.OrdinalIgnoreCase))
                    return BgToolTileReason.PickaxeTargetNotStone;
                break;
            case BgToolKind.Hoe:
                // Vanilla hoeing digs Artifact Spots even without the map's Diggable property.
                if (!f.Tillable && !(f.HasObject && f.ObjectIsArtifactSpot && f.ObjectQualifiedItemId == "(O)590"))
                    return BgToolTileReason.HoeTileNotTillable;
                break;
            case BgToolKind.WateringCan:
                bool dirt = string.Equals(f.TerrainType, "HoeDirt", StringComparison.Ordinal);
                // W27 (pad path): an unwatered pet bowl is a valid can target (GameLocation.performToolAction
                // -> PetBowl.performToolAction, GameLocation.cs:16257-16261). HYPOTHESIS until live.
                bool bowl = f.PetBowlAtToolTile && !f.PetBowlWatered && f.WaterLeft is int bowlLeft && bowlLeft > 0;
                if (!IsRefill(kind, f) && !(dirt && f.WaterLeft is int left && left > 0) && !bowl)
                    return BgToolTileReason.CanTargetInvalid;
                break;
            case BgToolKind.Scythe:
                if (!(f.HasObject && f.ObjectIsWeeds) && !string.Equals(f.TerrainType, "Grass", StringComparison.Ordinal))
                    return BgToolTileReason.ScytheTargetInvalid;
                if (ScytheAreaUnsafe(f))
                    return BgToolTileReason.ScytheAreaUnsafe;
                break;
        }
        if (kind != BgToolKind.Scythe && !IsRefill(kind, f) && !(f.Stamina >= MinStamina))
            return BgToolTileReason.StaminaLow;
        return null;
    }
}

/// <summary>
/// Bounded hold plan: exactly one pinned keyboard key held for TargetHFrames
/// VERIFY-admitted callbacks, then R, then N. The consumption clock is actual
/// VERIFY-admitted hook callbacks, never SMAPI ticks or Press calls.
/// </summary>
public sealed class BgHoldFramePlan : IBgFramePlan
{
    public const int ToolMaxTicks = 14;
    public const int ActionTicks = 1;
    /// <summary>
    /// The longest reflex press: WS-G4 cast (FishingReflexMachine.CastHoldBudget = 150) and WS-G5 charge
    /// (ReflexContracts.ChargeMaxTicksMax = 200); = max over BgReflexPress.MaxTicks (unit-tested).
    /// </summary>
    public const int ReflexMaxTicks = 200;

    public BgHoldFramePlan(BgHoldKind kind, BgButton key, int targetHFrames)
        : this(kind, key, targetHFrames, null)
    {
    }

    /// <summary>WS-G4 reflex press: a TOOL pad hold with the reflex's own tick cap (cast up to 150, tap 1).</summary>
    public BgHoldFramePlan(BgHoldKind kind, BgButton key, int targetHFrames, int reflexMaxTicks)
        : this(kind, key, targetHFrames, (int?)reflexMaxTicks)
    {
    }

    private BgHoldFramePlan(BgHoldKind kind, BgButton key, int targetHFrames, int? reflexMaxTicks)
    {
        if (reflexMaxTicks is int cap
                ? kind != BgHoldKind.Tool || key.Device != BgDevice.Pad || targetHFrames < 1 || targetHFrames > cap
                    || cap > ReflexMaxTicks
                : BgHoldAdmission.ValidateTicks(kind, targetHFrames) is not null)
            throw new ArgumentOutOfRangeException(nameof(targetHFrames), "TOOL hold ticks must be 1..14; ACTION exactly 1; reflex pad X 1..cap");
        // WS-G0: a pad hold pins a fixed pad button (X / A / ...); the keyboard rule is unchanged.
        if (key.Device == BgDevice.Pad ? !BgPadButtons.IsHoldPin(key)
            : key.Device != BgDevice.Keyboard || key.IsEscape || BgHoldBindings.IsHardcoded(key))
            throw new ArgumentException("hold key must be a non-hardcoded keyboard key or a pad hold pin", nameof(key));
        this.Kind = kind;
        this.Key = key;
        this.TargetHFrames = targetHFrames;
    }

    public bool Verify => true;
    public BgHoldKind Kind { get; }
    public BgButton Key { get; }
    public int TargetHFrames { get; }
    public int ConsumedH { get; private set; }
    public bool RConsumed { get; private set; }
    public bool NConsumed { get; private set; }
    public string? ReleaseReason { get; private set; }

    public BgFrameKind Next
    {
        get
        {
            if (this.NConsumed) return BgFrameKind.Done;
            if (this.ReleaseReason is null && this.ConsumedH < this.TargetHFrames) return BgFrameKind.H;
            if (this.ConsumedH > 0 && !this.RConsumed) return BgFrameKind.R;
            return BgFrameKind.N;
        }
    }

    public bool Active => this.Next != BgFrameKind.Done;

    /// <summary>The runtime holds the key down exactly while the next expected frame is H.</summary>
    public bool KeyShouldBeDown => this.Next == BgFrameKind.H;

    public BgFrameKind EffectiveNext(bool stopOrLeaseInvalid)
    {
        BgFrameKind next = this.Next;
        if (next == BgFrameKind.H && stopOrLeaseInvalid)
            return this.ConsumedH > 0 ? BgFrameKind.R : BgFrameKind.N;
        return next;
    }

    public void RequestRelease(string reason) => this.ReleaseReason ??= reason;

    public void OnConsumed(BgFrameKind kind)
    {
        if (kind != this.Next)
            throw new InvalidOperationException("consumed frame " + kind + " does not match hold plan " + this.Next);
        switch (kind)
        {
            case BgFrameKind.H: this.ConsumedH++; break;
            case BgFrameKind.R: this.RConsumed = true; break;
            case BgFrameKind.N: this.NConsumed = true; break;
            default: throw new InvalidOperationException("hold plan already done");
        }
    }

    /// <summary>Expected owned frame: B + {key} for H, B for R/N (B is button-free for hold plans).</summary>
    public BgInputState FrameFor(BgFrameKind kind, BgInputState baseline) => this.Key.Device == BgDevice.Pad
        ? BgPadFrame.Build(baseline, kind == BgFrameKind.H ? this.Key : null)
        : BgSyntheticFrame.Build(baseline, kind == BgFrameKind.H ? this.Key : null);
}

/// <summary>
/// Read-only cursor facts (2.1 section 2 / N6), sampled against the installed Game1
/// MVID aa1f513a-94f0-4ef6-a35f-2d5b4a3721bf. Tile = (oldMouse + viewport) / 64;
/// on-screen-menu hover uses isWithinBounds with the UI-scaled mouse position;
/// furniture by bounding box; objects / terrain features / clumps / buildings by tile.
/// </summary>
public sealed record BgCursorFacts(
    bool CursorVisible,
    int CursorTileX,
    int CursorTileY,
    int PlayerTileX,
    int PlayerTileY,
    bool OverOnScreenMenu,
    bool TileHasObject,
    bool TileHasTerrainFeature,
    bool TileHasLargeTerrainFeature,
    bool TileHasResourceClump,
    bool TileHasBuilding,
    bool FurnitureContainsCursor,
    // Acceptance 2 (live R6ACC4a/4c): every furniture whose bounding box holds the cursor is walkable
    // floor furniture (rug, furniture_type 12, or isPassable()) holding nothing. Sampled for the
    // ACTION rule only; see BgCursorInert.
    bool FurnitureAllPassable = false);

public static class BgCursorInert
{
    /// <summary>
    /// CursorInert: cursor not visible, OR (cursor tile beyond Chebyshev radius 1 of the
    /// player AND not over any on-screen menu AND nothing interactable at the cursor).
    /// Null facts (unreadable) are never inert. <paramref name="kind"/> defaults to the strict
    /// TOOL rule.
    ///
    /// Walkable floor furniture (a rug) under the cursor is inert for ACTION only (live R6ACC4a/4c:
    /// a park on the Cabin rug's edge refused world_interact as FURNITURE). Decompiled 1.6.15:
    /// Game1.pressActionButton reads the cursor tile only within radius 1 (else player.GetGrabTile())
    /// and never reaches leftClick / LowPriorityLeftClick, so a rug beyond radius 1 is not touched;
    /// Furniture.isPassable() is true for furniture_type 12 and a rug has no seat (GetSeatCapacity 0)
    /// or action. TOOL stays strict: Game1.pressUseToolButton calls GameLocation.LowPriorityLeftClick
    /// with the cursor position, which REMOVES passable furniture under it when canBeRemoved and
    /// (CanFreePlaceFurniture() [every DecoratableLocation, i.e. the cabin] or IsCloseEnoughToFarmer).
    /// Non-passable furniture (tables, chairs, beds) stays not inert for TOOL; ACTION beyond radius 1 never reads it (PM-1 a7 #2).
    /// </summary>
    public static string? Reject(BgCursorFacts? f, BgHoldKind kind = BgHoldKind.Tool)
    {
        if (f is null)
            return BgHoldReason.CursorNotInert + ":UNREADABLE";
        if (!f.CursorVisible)
            return null;
        int chebyshev = Math.Max(Math.Abs(f.CursorTileX - f.PlayerTileX), Math.Abs(f.CursorTileY - f.PlayerTileY));
        if (chebyshev <= 1)
            return BgHoldReason.CursorNotInert + ":WITHIN_RADIUS_1";
        if (f.OverOnScreenMenu)
            return BgHoldReason.CursorNotInert + ":OVER_ON_SCREEN_MENU";
        // PM-1 a7 #2 (N:20879-22308: the home door refused 27x, CURSOR_NOT_INERT:TERRAIN_FEATURE, cursor tile (52,12)
        // four tiles off): fix6 freed BUILDING only. pressActionButton replaces the cursor tile by player.GetGrabTile()
        // beyond radius 1 for every target kind, so nothing under a far cursor is touched by ACTION. TOOL stays strict.
        if (kind == BgHoldKind.Action)
            return null;
        if (f.FurnitureContainsCursor)
            return BgHoldReason.CursorNotInert + ":FURNITURE";
        // TileHasObject is sampled as "a Furniture item among the location objects" (the only Object
        // subtype overriding clicked; see SampleCursorFacts). Other objects beyond radius 1 are inert.
        if (f.TileHasObject)
            return BgHoldReason.CursorNotInert + ":OBJECT";
        // TileHasTerrainFeature is sampled as HoeDirt only (crops: Utility.canGrabSomethingFromHere).
        // Beyond radius 1, Stardew 1.6.15 ignores other terrain under the cursor on both hold paths
        // (decompiled, live 2026-10-07 M1 attempts 10r/10t): pressUseToolButton reaches the cursor
        // only through canGrabSomethingFromHere (objects, table furniture, ready HoeDirt; the latter
        // refused beyond radius 1), GameLocation.leftClick (buildings, objects) and
        // Character.GetToolLocation (facing tile beyond radius 1); pressActionButton uses
        // player.GetGrabTile() beyond radius 1. Grass, trees, bushes and resource clumps are inert
        // there. Large/clump facts stay sampled for diagnostics only.
        if (f.TileHasTerrainFeature)
            return BgHoldReason.CursorNotInert + ":TERRAIN_FEATURE";
        // PM-1 fix6 #1 (N:21548-21550): only pressUseToolButton reaches a building through GameLocation.leftClick.
        if (f.TileHasBuilding)
            return BgHoldReason.CursorNotInert + ":BUILDING";
        return null;
    }
}

/// <summary>Facts sampled at every hold hook entry (VERIFY role), before Parent.</summary>
public sealed record BgHoldHookFacts(
    BgHoldKind Kind,
    bool IsMainPlayer,
    bool IsSplitScreen,
    bool WorldReady,
    bool MenuOpen,
    bool MenuIsPinnedChest,
    bool EventOrMinigame,
    bool Chatting,
    bool UsingTool,
    bool UsingSlingshot,
    bool Fishing,
    bool CanMove,
    bool Paused,
    bool GlobalFade,
    bool FreezeControls,
    bool PanMode,
    bool GamepadMode,
    int MovementDirections,
    bool SlotUnchanged,
    bool ToolItemUnchanged,
    bool TileUnchanged,
    bool FacingUnchanged,
    bool LocationUnchanged,
    BgCursorFacts? Cursor,
    bool VillagerAtToolTile = false,
    // WS3a: the pinned action target is still at the grab tile (always true for TOOL holds).
    bool TargetPresent = true,
    // WS-G0 pad path: gamepad mode is REQUIRED and the cursor-hidden gate replaces CursorInert.
    bool PadMode = false,
    BgCursorHiddenFacts? CursorHidden = null,
    // WS-G6 (pinned extras target only): after the consumed press the milk pail / shears animation sets UsingTool,
    // and petting a farm animal / mounting / dismounting / the harvest animation may turn or move the farmer.
    bool ToolUseAfterPress = false,
    bool PoseMayChangeAfterPress = false,
    bool GenericWorldPress = false);

/// <summary>
/// Hold gate (2.1 section 3 / M2 / m1 / m2, mirroring the foreground rule that
/// accepts UsingTool while a use-tool hold is active). Before the first consumed
/// press every stage-1 gate is strict. After it, ONLY these facts relax:
/// TOOL: UsingTool = true and !CanMove; ACTION: !CanMove (chest lock freezePause)
/// and, on a non-H frame, an open menu that is the pinned chest ItemGrabMenu by
/// reference. movementDirections == 0, pinned slot/item/tile/facing/location and
/// CursorInert are explicit checks on every entry. Everything else stays strict.
/// </summary>
public static class BgHoldGate
{
    public static string? Reject(BgHoldHookFacts f, bool pressConsumed, BgFrameKind frame)
    {
        if (f.IsMainPlayer) return BgReason.NotFarmhand;
        if (f.IsSplitScreen) return BgReason.SplitScreen;
        if (!f.WorldReady) return BgReason.WorldNotReady;
        bool genericRelease = f.GenericWorldPress && pressConsumed && frame != BgFrameKind.H;
        if (f.EventOrMinigame && !genericRelease) return BgReason.EventOrMinigame;
        if (f.Chatting) return BgReason.Chatting;
        if (f.GlobalFade && !genericRelease) return BgReason.GlobalFade;
        if (f.FreezeControls && !genericRelease) return BgReason.FreezeControls;
        if (f.PanMode) return BgReason.PanMode;
        if (f.Paused) return BgReason.FreezePause;
        if (f.PadMode ? !f.GamepadMode : f.GamepadMode)
            return f.PadMode ? BgReason.GamepadModeNotOwned : BgReason.GamepadMode;
        if (f.UsingSlingshot && !genericRelease) return BgReason.UsingSlingshot;
        if (f.Fishing && !genericRelease) return BgReason.Fishing;
        // WS3a: the action button falls through to held-item use / placement / the tiles
        // below/above when its target vanished. Never deliver the press then.
        if (!f.TargetPresent && !pressConsumed && frame == BgFrameKind.H) return BgHoldReason.TargetGone;
        if (f.MenuOpen && !(f.Kind == BgHoldKind.Action && pressConsumed && frame != BgFrameKind.H && f.MenuIsPinnedChest))
            return BgReason.MenuOpen;
        if (f.UsingTool && !((f.Kind == BgHoldKind.Tool || f.ToolUseAfterPress) && pressConsumed))
            return BgReason.ToolInteraction;
        if (!f.CanMove && !pressConsumed)
            return BgReason.CannotMove;
        if (f.MovementDirections != 0)
            return BgHoldReason.MovementDuringHold;
        if (!f.SlotUnchanged || !f.ToolItemUnchanged && !genericRelease)
            return BgHoldReason.PinDrift + ":SLOT";
        bool poseRelaxed = f.PoseMayChangeAfterPress && pressConsumed;
        if (!f.TileUnchanged && !poseRelaxed)
            return BgHoldReason.PinDrift + ":TILE";
        if (!f.FacingUnchanged && !poseRelaxed)
            return BgHoldReason.PinDrift + ":FACING";
        if (!f.LocationUnchanged && !genericRelease)
            return BgHoldReason.PinDrift + ":LOCATION";
        // Review fix (WS1 minor 3): a villager walking into the hit point before the press is consumed
        // would turn the press into a talk (pressUseToolButton); re-checked at every hook entry.
        if (f.Kind == BgHoldKind.Tool && !pressConsumed && f.VillagerAtToolTile)
            return BgToolTileReason.VillagerAtToolTile;
        return f.PadMode ? BgCursorHidden.Reject(f.CursorHidden) : BgCursorInert.Reject(f.Cursor, f.Kind);
    }
}

/// <summary>Hold-plan baseline helpers.</summary>
public static class BgHoldBaseline
{
    /// <summary>
    /// VERIFY is exact only when the post-N0 baseline B (Game1.old*) and the retained
    /// snapshot are the same button-free state (same mouse X/Y/scroll, pad default).
    /// Otherwise the first owned press would carry an unowned edge: refuse before Press.
    /// </summary>
    public static string? Coherence(BgInputState retainedSnapshot, BgInputState baseline) =>
        retainedSnapshot.Equals(baseline) && baseline.Down.Count == 0 && !baseline.PadAnalogActive
            && baseline.LeftStickX == 0 && baseline.LeftStickY == 0
            ? null
            : BgHoldReason.RetainedHistoryIncoherent;

    /// <summary>
    /// Dispatch R5: the per-tick N-R suppress set re-applied while inactive must not
    /// contain the pinned key once N0 is verified, otherwise the owned Press is
    /// cancelled every tick.
    /// </summary>
    public static IReadOnlyList<BgButton> SuppressSetAfterN0(IReadOnlyList<BgButton> nrSuppress, BgButton pinnedKey) =>
        nrSuppress.Where(b => !b.Equals(pinnedKey)).ToArray();
}

/// <summary>The only three input operations a hold session may perform (adapter over SMAPI in S2).</summary>
public interface IBgPressPort
{
    void Press(BgButton key);
    void Suppress(BgButton key);
    bool IsSuppressed(BgButton key);
}

public enum BgPressIntent { Hold, Release }

/// <summary>
/// One owned press override for one pinned key (2.2 sections 1-2, R1/R2).
/// <list type="bullet">
/// <item>R2: containment is armed BEFORE Press; if arming fails, Press is never issued.</item>
/// <item>At most one Press per session; after any release the key is never pressed again.</item>
/// <item><see cref="Tick"/> runs once per UpdateTicking: on ANY non-hold intent, or any
/// exception while deciding, Suppress is issued in a finally within the same call
/// (and re-issued every later tick until the release is proved).</item>
/// <item>Release is proved only by Suppress issued + IsSuppressed + key absent from the
/// effective state, plus a VERIFY receipt with the key absent where a callback can
/// occur (R1). SMAPI IsDown is never evidence.</item>
/// </list>
/// </summary>
public sealed class BgPressSession
{
    private readonly BackgroundContainmentPolicy policy;
    private readonly IBgPressPort port;
    private readonly Func<long> clockMs;
    private readonly bool isMainPlayer;
    private readonly bool isSplitScreen;
    private readonly string requestId;
    private readonly string runtimeId;

    public BgPressSession(BgButton key, BackgroundContainmentPolicy policy, IBgPressPort port, Func<long> clockMs,
        bool isMainPlayer, bool isSplitScreen, string requestId, string runtimeId)
    {
        // WS-G0: a pad pin (X / A / stick / D-pad) is the other admissible hold key.
        if (key.Device == BgDevice.Pad ? !BgPadButtons.IsHoldPin(key) : key.Device != BgDevice.Keyboard || key.IsEscape)
            throw new ArgumentException("hold key must be keyboard (not Escape) or a pad hold pin", nameof(key));
        this.Key = key;
        this.policy = policy ?? throw new ArgumentNullException(nameof(policy));
        this.port = port ?? throw new ArgumentNullException(nameof(port));
        this.clockMs = clockMs ?? throw new ArgumentNullException(nameof(clockMs));
        this.isMainPlayer = isMainPlayer;
        this.isSplitScreen = isSplitScreen;
        this.requestId = requestId ?? "";
        this.runtimeId = runtimeId ?? "";
    }

    public BgButton Key { get; }
    public bool PressIssued { get; private set; }
    public bool ReleaseLatched { get; private set; }
    public bool SuppressIssued { get; private set; }
    public int SuppressCalls { get; private set; }
    public long? FirstSuppressMs { get; private set; }
    public bool ReleaseProved { get; private set; }
    public string? ArmFailure { get; private set; }
    public string? ExitReason { get; private set; }
    public string? SuppressFault { get; private set; }

    /// <summary>An owned override may still be in effect.</summary>
    public bool Outstanding => this.PressIssued && !this.ReleaseProved;

    /// <summary>
    /// ADR 244: Press calls that renewed the held override on an active-window tick. SMAPI's active
    /// TrueUpdate applies a pending press once and then clears it, so a held press is re-issued every
    /// active tick while Hold is wanted. Containment was armed once for the outstanding press; a renewal
    /// is the same press, not a new one. Always 0 while the window stays inactive.
    /// </summary>
    public int RenewCalls { get; private set; }

    /// <summary>
    /// UpdateTicking: Hold keeps / issues the single Press; anything else releases in the finally.
    /// <paramref name="renew"/> (ADR 244): the window is active this tick, so a kept press is re-issued.
    /// </summary>
    public void Tick(Func<BgPressIntent> intent, bool renew = false)
    {
        bool keepHeld = false;
        try
        {
            BgPressIntent wanted = intent();
            if (wanted == BgPressIntent.Hold && !this.ReleaseLatched)
            {
                if (!this.PressIssued)
                    keepHeld = this.TryPressOnce();
                else
                {
                    keepHeld = true;
                    if (renew)
                    {
                        this.RenewCalls++;
                        this.port.Press(this.Key);
                    }
                }
            }
            else if (wanted != BgPressIntent.Hold)
                this.ExitReason ??= "RELEASE";
        }
        catch (Exception ex)
        {
            this.ExitReason ??= "TICK_EXCEPTION:" + ex.GetType().Name;
            keepHeld = false;
            throw;
        }
        finally
        {
            if (!keepHeld)
                this.EnsureReleased();
        }
    }

    /// <summary>Lifecycle exit outside UpdateTicking (e.g. ReturnedToTitle): release now.</summary>
    public void ForceRelease(string reason)
    {
        this.ExitReason ??= reason;
        this.EnsureReleased();
    }

    private bool TryPressOnce()
    {
        if (!this.policy.TryArmForPressOutstanding(this.clockMs(), this.isMainPlayer, this.isSplitScreen, this.requestId, this.runtimeId))
        {
            this.ArmFailure ??= BgHoldReason.ContainmentNotArmed;
            this.ReleaseLatched = true;
            return false;
        }
        // Marked before the call: a throwing Press still gets its finally Suppress.
        this.PressIssued = true;
        this.port.Press(this.Key);
        return true;
    }

    private void EnsureReleased()
    {
        this.ReleaseLatched = true;
        if (!this.PressIssued || this.ReleaseProved)
            return;
        // B1: start the containment release deadline BEFORE the Suppress call, so a
        // Suppress that keeps faulting is contained (T5) instead of retried forever.
        this.policy.OnPressReleaseRequested(this.clockMs());
        try
        {
            this.SuppressCalls++;
            this.port.Suppress(this.Key);
            this.SuppressIssued = true;
            this.FirstSuppressMs ??= this.clockMs();
        }
        catch (Exception ex)
        {
            // Never mask the original exit; the next tick re-issues Suppress.
            this.SuppressFault ??= ex.GetType().Name;
        }
    }

    /// <summary>
    /// UpdateTicked readback (R1). <paramref name="effectiveKeyDown"/> is the effective
    /// keyboard state the game reads; <paramref name="keyAbsentWitness"/> is a VERIFY
    /// receipt with the key absent, or the fact that no callback can occur (latch
    /// removed / pinned menu open).
    /// </summary>
    public bool TryProveRelease(long nowMs, bool effectiveKeyDown, bool keyAbsentWitness)
    {
        if (this.ReleaseProved)
            return true;
        if (!this.PressIssued || !this.SuppressIssued || effectiveKeyDown || !keyAbsentWitness)
            return false;
        bool suppressed;
        try
        {
            suppressed = this.port.IsSuppressed(this.Key);
        }
        catch
        {
            return false;
        }
        if (!suppressed)
            return false;
        if (!this.policy.OnPressReleaseProved(nowMs, this.SuppressIssued, suppressed, keyAbsent: true))
            return false;
        this.ReleaseProved = true;
        return true;
    }
}

/// <summary>Independent completion evidence for one background hold request.</summary>
public sealed record BgHoldCompletionFacts(
    BgHoldKind Kind,
    int TargetTicks,
    int PressCount,
    bool PressIssued,
    bool RConsumed,
    bool NConsumed,
    bool ReleaseProved,
    string ContainmentLabel,
    bool LatchInstalled,
    bool LatchRestoredExact,
    bool DispatchImpossible,
    bool HistoryCanonical,
    bool Faulted,
    string? ExitLabel,
    string? ReleaseReason,
    int UsingToolRisingEdges,
    bool UsingToolAtFinal,
    bool PinsUnchanged,
    int MovementDirections,
    bool PinnedMenuObserved,
    bool MenuOpenedNoCallback,
    // WS3a: per-kind action evidence. Chest keeps the M1 rules above; door/forage are judged
    // by the sampled effect (location / inventory) and the slot/tool pins only.
    BgActionKind ActionKind = BgActionKind.Chest,
    bool SlotToolUnchanged = true,
    BgActionEffectVerdict? Effect = null,
    bool SynchronousToolReceiver = false);

/// <summary>Mapped onto the EXISTING verdict vocabulary only.</summary>
public sealed record BgHoldOutcome(string Outcome, int PressCount, bool CleanupConfirmed, string? Reason, bool CycleObserved, bool Opened);

/// <summary>
/// Verdict mapping (2.1 section 5 / M7, 2.2 section 1 / N1, R3). PressCount is the
/// number of VERIFY-admitted callbacks in which the pinned key was present, never
/// Press calls. CleanupConfirmed only after verified release. No frame-sent success.
/// </summary>
public static class BgHoldCompletion
{
    public const string InputCompleted = "InputCompleted";
    public const string InputInterrupted = "InputInterrupted";
    public const string InputUncertain = "InputUncertain";
    public const string ContainerOpened = "ContainerOpened";
    public const string LocationChanged = WorldInteractionKinds.LocationChanged;
    public const string ItemPickedUp = WorldInteractionKinds.ItemPickedUp;
    public const string InteractionNoEffect = BgActionEffect.InteractionNoEffect;
    public const string InteractionRejected = "InteractionRejected";
    public const string InteractionUncertain = "InteractionUncertain";
    public const string ReleaseVerifiedLabel = "RELEASE_VERIFIED";

    /// <summary>Menu wait bound after RELEASE (2.2 section 3), monotonic milliseconds.</summary>
    public const long MenuWaitMs = 1500;

    public static bool CleanupConfirmed(BgHoldCompletionFacts f)
    {
        bool pressed = f.PressIssued;
        bool releaseOk = !pressed
            || (f.ReleaseProved && string.Equals(f.ContainmentLabel, ReleaseVerifiedLabel, StringComparison.Ordinal));
        // WS3b: a synchronous receiver (npc / eat DialogueBox opened inside the H frame) means
        // no R frame can ever be consumed; its observed receiver stands in for R.
        bool syncReceiver = f.Kind == BgHoldKind.Action && BgSyncReceiver.IsSynchronous(f.ActionKind)
            && f.PinnedMenuObserved && f.MenuOpenedNoCallback;
        syncReceiver |= f.Kind == BgHoldKind.Tool && f.TargetTicks == 1 && f.PressCount == 1
            && f.SynchronousToolReceiver && f.PinnedMenuObserved && f.MenuOpenedNoCallback;
        bool framesOk = !pressed
            || ((f.PressCount == 0 || f.RConsumed || syncReceiver)
                && (f.NConsumed || syncReceiver || (f.Kind == BgHoldKind.Action && f.MenuOpenedNoCallback)));
        bool latchOk = !f.LatchInstalled || f.LatchRestoredExact;
        return releaseOk && framesOk && latchOk && !f.DispatchImpossible && f.HistoryCanonical && !f.Faulted;
    }

    /// <summary>The hook label for an H frame skipped pre-consumer because the action target vanished.</summary>
    public const string TargetGoneSkipLabel = BgReason.PreconsumerHistoryDivergence + ":" + BgHoldReason.TargetGone;

    /// <summary>
    /// PART_A26: an ACTION hold whose H frame was skipped pre-consumer with TARGET_GONE never reached
    /// the game. With no consumed frame, a proven release, RELEASE_VERIFIED containment, the latch
    /// restored exactly, no swing and no menu, it is the same zero-press rejection PrePressTargetCheck
    /// emits. Every other divergence (or any missing fact) stays uncertain.
    /// </summary>
    public static bool TargetGoneSkipSettled(BgHoldCompletionFacts f) =>
        f.Kind == BgHoldKind.Action
        && string.Equals(f.ExitLabel, TargetGoneSkipLabel, StringComparison.Ordinal)
        && f.PressCount == 0
        && f.ReleaseProved
        && string.Equals(f.ContainmentLabel, ReleaseVerifiedLabel, StringComparison.Ordinal)
        && f.LatchRestoredExact
        && f.UsingToolRisingEdges == 0 && !f.UsingToolAtFinal
        && !f.PinnedMenuObserved && !f.MenuOpenedNoCallback;

    /// <summary>
    /// fix54A (a39 D22:J647-J656, farmhand SMAPI run=2171): the Community Center door press consumed H/R/N,
    /// release was proved and the latch removed; during the settle wait the farmhand's location load lapsed a
    /// tick, the execution slot neutralized ("barrier") and that late release request became the hold's
    /// ReleaseReason. The verdict then read InteractionUncertain before looking at the sampled effect, although
    /// the native effect was the expected arrival (TRAVEL can_move in CommunityCenter). A release request that
    /// arrives after every frame of a one-press door hold was consumed cuts nothing; the observed effect decides,
    /// and only a successful arrival in the expected location (BgActionEffect.Classify) passes. Any exit label,
    /// an unconsumed frame or any other effect keeps the old uncertain verdict.
    /// fix54L (review M1): only the slot-neutralize reason ("barrier", the a39 load lapse) qualifies. A late STOP,
    /// lease, deadline, host pause or any other release keeps its reason in the uncertain verdict, so callers that
    /// key the STOP off the hold reason still see it.
    /// </summary>
    public const string LateReleaseBarrier = "barrier";
    /// <summary>fix55D (REVIEW2 #8): the hooks' own release when the slot stops being valid mid-settle
    /// (BackgroundControlInputHooks.TickingCore): the warp's world load, the new location without the door target, or
    /// moved pins. After every frame of a one-press door was consumed that is the arrival itself, like the slot
    /// "barrier". A late "focus", STOP, lease, deadline or pause is still an outside interruption and stays uncertain.</summary>
    public const string LateReleaseSlotValidity = "SLOT_VALIDITY_CHANGED";

    public static bool DoorArrivedBeforeLateRelease(BgHoldCompletionFacts f) =>
        f.ActionKind == BgActionKind.Door && f.ExitLabel is null
        && (string.Equals(f.ReleaseReason, LateReleaseBarrier, StringComparison.Ordinal)
            || string.Equals(f.ReleaseReason, LateReleaseSlotValidity, StringComparison.Ordinal))
        && f.PressIssued && f.TargetTicks == 1 && f.PressCount == 1 && f.RConsumed && f.NConsumed && f.ReleaseProved
        && f.Effect is { Success: true };

    public static BgHoldOutcome Evaluate(BgHoldCompletionFacts f)
    {
        bool cleanup = CleanupConfirmed(f);
        bool cycle = f.UsingToolRisingEdges > 0;
        string? early = f.ExitLabel ?? f.ReleaseReason;
        if (f.Kind == BgHoldKind.Tool)
        {
            if (!cleanup)
                return new(InputUncertain, f.PressCount, false, f.ExitLabel ?? BgHoldReason.ReleaseUnverified, cycle, false);
            // Post-press world-effect evidence takes precedence over interruption
            // (independent review M1): an unfinished swing, a swing count other than the
            // one expected for the consumed presses (1 after any consumed press, 0 before),
            // or a pin drift is uncertain even when STOP / lease / deadline ended the hold.
            if (f.PressIssued)
            {
                int expectedSwings = f.PressCount > 0 ? 1 : 0;
                if (f.UsingToolAtFinal)
                    return new(InputUncertain, f.PressCount, true, BgHoldReason.ToolStillInUse, cycle, false);
                if (f.UsingToolRisingEdges != expectedSwings)
                    return new(InputUncertain, f.PressCount, true, BgHoldReason.SwingCount + f.UsingToolRisingEdges, cycle, false);
                if (!f.PinsUnchanged || f.MovementDirections != 0)
                    return new(InputUncertain, f.PressCount, true, BgHoldReason.PinDrift, cycle, false);
            }
            else if (f.PressCount > 0 || f.UsingToolRisingEdges > 0)
            {
                // Credited presses or a swing without an owned Press: inconsistent evidence.
                return new(InputUncertain, f.PressCount, true, BgHoldReason.PressCountWithoutPress, cycle, false);
            }
            if (early is not null)
                return new(InputInterrupted, f.PressCount, true, early, cycle, false);
            if (f.PressCount != f.TargetTicks)
                return new(InputInterrupted, f.PressCount, true, BgHoldReason.FramesNotConsumed, cycle, false);
            return new(InputCompleted, f.PressCount, true, null, cycle, false);
        }

        if (TargetGoneSkipSettled(f))
            return new(InteractionRejected, 0, true, BgHoldReason.TargetGone, false, false);
        if (!cleanup)
            return new(InteractionUncertain, f.PressCount, false, f.ExitLabel ?? BgHoldReason.ReleaseUnverified, false, false);
        if (!f.PressIssued)
            return new(InteractionRejected, 0, true, early, false, false);
        if (f.PressCount == 0)
            return new(InteractionUncertain, 0, true, early ?? BgHoldReason.PressNotConsumed, false, false);
        if (early is not null && !DoorArrivedBeforeLateRelease(f))
            return new(InteractionUncertain, f.PressCount, true, early, false, false);
        if (f.PressCount != 1)
            return new(InteractionUncertain, f.PressCount, true, BgHoldReason.FramesNotConsumed, false, false);
        if (f.ActionKind != BgActionKind.Chest)
        {
            // Door: tile / facing / location pins are expected to change with the warp; the slot and
            // tool pins never may. Forage and a door without effect keep every pin strict.
            if (!f.SlotToolUnchanged || f.MovementDirections != 0)
                return new(InteractionUncertain, f.PressCount, true, BgHoldReason.PinDrift, false, false);
            if (f.Effect is not { } effect)
                return new(InteractionUncertain, f.PressCount, true, BgActionEffect.EffectUnread, false, false);
            bool strictPins = f.ActionKind == BgActionKind.Forage || effect.Outcome == InteractionNoEffect;
            if (strictPins && !f.PinsUnchanged && effect.Outcome != InteractionUncertain)
                return new(InteractionUncertain, f.PressCount, true, BgHoldReason.PinDrift, false, false);
            return new(effect.Outcome, f.PressCount, true, effect.Reason, false, effect.Success);
        }
        if (!f.PinnedMenuObserved)
            return new(InteractionUncertain, f.PressCount, true, BgHoldReason.MenuWaitTimeout, false, false);
        if (!f.PinsUnchanged || f.MovementDirections != 0)
            return new(InteractionUncertain, f.PressCount, true, BgHoldReason.PinDrift, false, false);
        return new(ContainerOpened, f.PressCount, true, null, false, true);
    }
}

/// <summary>
/// m3 + R3: an open pinned-chest menu holds the chest lock while inactive. After an
/// ACTION terminal the debt refuses non-container requests until the menu closes
/// (container_exit); when the menu was not (yet) observed, the check stays active for one
/// more bounded window for a late open. No FailFast. ADR 244: window focus no longer
/// clears the debt (the pinned chest is hers whatever the window does).
/// </summary>
public sealed class BgChestMenuDebt
{
    public const long LateOpenWindowMs = BgHoldCompletion.MenuWaitMs;

    private long watchUntilMs;
    private long sinceMs;

    public bool Active { get; private set; }
    public bool ObservedOpen { get; private set; }
    public string? OpenedBy { get; private set; }
    public (string? Attempt, long Frame) ObservedEffect { get; private set; }
    public string? DebtId => this.Active ? "chest:" + (this.OpenedBy?.Length > 122
        ? Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(this.OpenedBy))).ToLowerInvariant()
        : this.OpenedBy) : null;
    // PM-1 fix8: ONE predicate for the arm (Allows) and the published fence / affordances (AllowedOperations).
    public string[] AllowedOperations => this.ObservedOpen
        ? new[] { "container_transfer", "container_exit", "ui:close", InspectTilesQuery.Op, InspectContextQuery.Op }
        : new[] { InspectTilesQuery.Op, InspectContextQuery.Op };
    public bool AllowsOp(string op) => !this.Active || this.AllowedOperations.Contains(op);
    public static string OpOf(MenuStepCommandRequest request) => request.Op is InspectTilesQuery.Op or InspectContextQuery.Op ? request.Op
        : MenuStepCommandInbox.IsContainerTransfer(request) ? "container_transfer" : MenuStepCommandInbox.IsContainerExit(request) ? "container_exit"
        : request.VerbEnvelope is { } e && e.GetProperty("verb").GetString() == "ui" && e.GetProperty("args").GetProperty("gesture").GetString() == "close"
            ? "ui:close" : request.Primitive ?? request.Op;
    public bool Allows(MenuStepCommandRequest request) => this.AllowsOp(OpOf(request));
    public object[] Snapshot => !this.Active || this.OpenedBy is null ? Array.Empty<object>() : new object[] { new {
        debt_id = this.DebtId, kind = "chest_mutex", opened_by = new { attempt_id = this.OpenedBy },
        allows = this.ObservedOpen ? new[] { "transfer", "ui:close", "observe" } : new[] { "observe" }, since_ms = this.sinceMs
    } };

    public void Begin(bool pinnedMenuOpenNow, long nowMs, string? openedBy = null, long sinceUnixMs = 0, long frame = 0)
    {
        this.Active = true;
        this.OpenedBy = openedBy;
        this.sinceMs = sinceUnixMs;
        this.ObservedOpen = pinnedMenuOpenNow;
        if (pinnedMenuOpenNow) this.ObservedEffect = (openedBy, frame);
        this.watchUntilMs = pinnedMenuOpenNow ? 0 : nowMs + LateOpenWindowMs;
    }

    /// <summary>Once per UpdateTicked, before command execution.</summary>
    public void Observe(bool pinnedMenuOpen, long nowMs, long frame = 0)
    {
        if (!this.Active)
            return;
        if (pinnedMenuOpen)
        {
            if (!this.ObservedOpen) this.ObservedEffect = (this.OpenedBy, frame);
            this.ObservedOpen = true;
            return;
        }
        if (this.ObservedOpen || nowMs > this.watchUntilMs)
            this.Clear();
    }

    /// <summary>
    /// Background container eligibility anchor: the pinned chest menu is open, or it was
    /// open at the last refresh and closed during this tick's own container command.
    /// </summary>
    public bool ContainerEligible(bool pinnedMenuOpen, bool menuIsNull) =>
        this.Active && (pinnedMenuOpen || (menuIsNull && this.ObservedOpen));

    /// <summary>Read-only admission fact; publishing it never releases the chest lock.</summary>
    public Dictionary<string, object>? InputFence(bool pinnedMenuOpen) => !this.Active ? null : new()
    {
        ["reason"] = "BackgroundChestMenuDebt",
        ["debt_id"] = this.DebtId!,
        ["opened_by"] = this.OpenedBy ?? "",
        ["continuation"] = this.ObservedOpen && pinnedMenuOpen,
        ["pinned_menu_open"] = pinnedMenuOpen,
        ["waiting_for_late_open"] = !this.ObservedOpen,
        ["allowed_operations"] = this.AllowedOperations
    };

    public void Clear()
    {
        this.Active = false;
        this.ObservedOpen = false;
        this.OpenedBy = null;
        this.watchUntilMs = 0;
    }
}

/// <summary>
/// Routing / admission (2.1 section 4 / M1, 2.2 section 5 / N5; ADR 244). The verified
/// path exists only with both default-OFF flags on and the runtime present, and then it
/// serves every request whatever the window focus: no request falls back to the
/// foreground arm. Window focus is not a routing or admission term (ADR 244).
/// </summary>
public static class BgToolUiRoute
{
    public static bool Enabled(bool backgroundToolUiEnabled, bool backgroundMovementEnabled, bool runtimePresent) =>
        backgroundToolUiEnabled && backgroundMovementEnabled && runtimePresent;

    /// <summary>N5 term: enabled AND (tool whitelisted / pinned chest target present). ADR 244: no focus term.</summary>
    public static bool BackgroundEligible(bool enabled, bool primitiveTargetPresent) =>
        enabled && primitiveTargetPresent;

    /// <summary>Admit capability: foreground readiness unchanged, OR the background-eligible term.</summary>
    public static bool AdmitTerm(bool foregroundReady, bool backgroundEligible) => foregroundReady || backgroundEligible;

    /// <summary>Dispatch: the verified path exactly when enabled, active window or not (ADR 244); never a foreground fallback.</summary>
    public static bool RouteBackground(bool enabled) => enabled;
}

/// <summary>
/// session.json use_tool / world_interaction flags (M1 attempt 8, 2026-10-06): the
/// published flags carry exactly the Admit capability terms, i.e. foreground readiness
/// OR the same background-eligible term Admit uses. Before this, session.json carried
/// only the IsActive-gated foreground readiness, so an inactive farmhand advertised
/// use_tool in ui.context while session.json said use_tool_enabled=false.
/// </summary>
public readonly record struct CommandSessionFlags(bool UseToolEnabled, bool WorldInteractionEnabled)
{
    public static CommandSessionFlags Compose(
        bool useToolInputReady, bool backgroundUseToolEligible,
        bool worldInteractionReady, bool backgroundWorldInteractionEligible) =>
        new(BgToolUiRoute.AdmitTerm(useToolInputReady, backgroundUseToolEligible),
            BgToolUiRoute.AdmitTerm(worldInteractionReady, backgroundWorldInteractionEligible));
}
