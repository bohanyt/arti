using System.Runtime.CompilerServices;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Menus;
using StardewValley.Monsters;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge;

// Primitives v2 WS-G5: the game side of the combat and charge reflexes (default OFF behind ModConfig.ReflexCombatEnabled /
// ReflexChargeEnabled, and only on the G0 pad path). The loops live in CombatReflexRunner / ChargeReflexRunner (behaviour-
// tested in HoldRuntimeTests with scripted fakes); this file wires them like WS-G4: admission (pure CombatReflexAdmission /
// ChargeReflexAdmission + the runtime's pad-mode / cursor-hidden admission), the per-tick hooks (before / after the
// background runtime's UpdateTicking, terminal routing and the verdict in UpdateTicked), the save / lifecycle aborts, the
// read-only game adapters, ui.context "combat" / "charge" + capabilities, session.json terms and the post-Downed loss
// journal. Evidence ceiling: UNIT_TESTED (+ LOCAL_BUILT); HYPOTHESIS until the CT witnesses (V2ACC11, G5W1).
public sealed partial class ModEntry
{
    private CombatReflexRunner? combatRunner;
    private ChargeReflexRunner? chargeRunner;
    private CombatGame? combatGame;

    bool ReflexCombatConfigured => this.config.ReflexCombatEnabled && this.PadPathConfigured;

    bool ReflexChargeConfigured => this.config.ReflexChargeEnabled && this.PadPathConfigured;

    bool ReflexG5BaseReady => this.config.Enabled && Context.IsWorldReady && !Context.IsMainPlayer && !Context.IsSplitScreen
        && Game1.game1?.IsActive != true;

    /// <summary>reflex_combat admission term (inbox Admit / session.json).</summary>
    bool ReflexCombatReady => this.ReflexCombatConfigured && this.ReflexG5BaseReady;

    /// <summary>reflex_charge admission term (inbox Admit / session.json).</summary>
    bool ReflexChargeReady => this.ReflexChargeConfigured && this.ReflexG5BaseReady;

    /// <summary>A WS-G5 reflex holds the inbox slot (ModEntry.Reflex.cs ReflexBusy includes it).</summary>
    bool ReflexG5Busy => this.combatRunner?.Active == true || this.chargeRunner?.Active == true;

    private CombatGame CombatGameAdapter => this.combatGame ??= new CombatGame(this);

    private CombatReflexRunner CombatRunner => this.combatRunner ??= new CombatReflexRunner(this.CombatGameAdapter,
        () => this.backgroundMove, () => this.ReflexLeaseHeld || this.BodyGuardAuthority, () => this.actionOwner.StopLatched);

    private ChargeReflexRunner ChargeRunner => this.chargeRunner ??= new ChargeReflexRunner(new ChargeGame(this),
        () => this.backgroundMove, () => this.ReflexLeaseHeld, () => this.actionOwner.StopLatched);

    // ------------------------------------------------------------------ admission

    private CombatAdmissionFacts SampleCombatAdmission(int hpFloorPct)
    {
        CombatGame g = this.CombatGameAdapter;
        Farmer? p = Context.IsWorldReady ? Game1.player : null;
        (float x, float y) = g.Position;
        bool weapon = p?.CurrentTool is MeleeWeapon;
        return new CombatAdmissionFacts(
            FlagOn: this.config.ReflexCombatEnabled,
            PadPathConfigured: this.PadPathConfigured,
            WindowActive: Game1.game1?.IsActive == true,
            WorldReady: Context.IsWorldReady && p?.currentLocation is not null,
            MenuOpen: Game1.activeClickableMenu is not null || Game1.dialogueUp,
            EventUp: g.EventUp,
            PlayerFree: Context.IsPlayerFree && Context.CanPlayerMove,
            MeleeWeaponHeld: weapon,  // owner 2026-10-23: the scythe counts as a weapon
            ScytheHeld: weapon && CombatWeapon.IsScythe(p!.CurrentTool.QualifiedItemId),
            UsingTool: p?.UsingTool == true,
            RuntimeBusy: this.backgroundMove?.Busy == true || this.backgroundMove?.MenuDebtActive == true,
            LocationRefused: g.LocationRefused,
            InMines: g.InMines,
            TimeOfDay: Game1.timeOfDay,
            Health: p?.health ?? 0,
            MaxHealth: p?.maxHealth ?? 0,
            HpFloorPct: hpFloorPct,
            NearestMonsterTiles: CombatFacts.Nearest(g.Monsters(), x, y),
            RadiusTiles: ReflexContracts.CombatRadiusTilesMax,
            PadModeRefusal: null,
            CursorHiddenRefusal: null);
    }

    private ChargeAdmissionFacts SampleChargeAdmission(int targetLevel)
    {
        ChargeGame g = new(this);
        Farmer? p = Context.IsWorldReady ? Game1.player : null;
        string? area = null;
        if (ChargeReflexMachine.IsChargeable(g.ToolKind, g.UpgradeLevel) && targetLevel >= 1)
        {
            area = g.ToolTile is { } t
                ? ChargeAreaGuard.Reject(g.ToolKind, g.SampleArea(ChargeArea.Tiles(t.X, t.Y, t.Facing, targetLevel)), g.WaterLeft)
                : "area_unreadable";
        }
        return new ChargeAdmissionFacts(
            FlagOn: this.config.ReflexChargeEnabled,
            PadPathConfigured: this.PadPathConfigured,
            WindowActive: Game1.game1?.IsActive == true,
            WorldReady: Context.IsWorldReady && p?.currentLocation is not null,
            MenuOpen: g.MenuOpen,
            EventUp: g.EventUp,
            PlayerFree: Context.IsPlayerFree && Context.CanPlayerMove,
            ToolKind: g.ToolKind,
            UpgradeLevel: g.UpgradeLevel,
            Reaching: g.Reaching,
            UsingTool: p?.UsingTool == true,
            RuntimeBusy: this.backgroundMove?.Busy == true || this.backgroundMove?.MenuDebtActive == true,
            Stamina: p?.Stamina ?? 0f,
            TargetLevel: targetLevel,
            AreaRefusal: area,
            PadModeRefusal: null,
            CursorHiddenRefusal: null);
    }

    /// <summary>Shared inbox-side refusals of a reflex request (nothing pressed on refusal).</summary>
    private string? ReflexG5CommonRefusal(MenuStepCommandRequest request, string prefix, bool ready, bool argsPresent)
    {
        CommandInboxIdentity pin = this.CurrentCommandIdentity();
        return !ready ? prefix + ":not_ready"
            : !argsPresent ? prefix + ":args"
            : this.padAdapter?.SaveWindow == true ? BgReason.PreconditionUnavailable(BgHoldReason.PadModeUnavailable + ":saving")
            : this.commandInbox.ExecutionSlot.Blocks(request) ? prefix + ":busy"
            : this.actionOwner.ReleaseRequested || this.actionOwner.StopLatched ? prefix + ":stop_or_release"
            : !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request, pin, Context.IsWorldReady, Context.IsPlayerFree,
                Context.CanPlayerMove, out _) ? prefix + ":binding"
            : !this.actionOwner.CanAdmitLeasedInputOnce(pin, NowUnixMs(), request.RuntimeInstanceId, request.OwnerId,
                request.Generation, request.ControlSeq, out _) ? prefix + ":lease"
            : null;
    }

    private void RejectReflexG5(MenuStepCommandRequest request, string refuse)
    {
        this.WriteCommandResult(this.commandInbox.CompleteRejected(request, CommandRejectReason.ArmRejected, this.CurrentCommandIdentity(),
            NowUnixMs(), refuse.StartsWith("PRECONDITION_UNAVAILABLE(", StringComparison.Ordinal) ? refuse : BgReason.PreconditionUnavailable(refuse)));
    }

    /// <summary>Inbox dispatch of reflex_combat: admit (nothing pressed on refusal) and start.</summary>
    private void ArmReflexCombatFromCommand(MenuStepCommandRequest request)
    {
        this.ApplyActionOwnerRecords();
        CombatReflexArgs args = request.ReflexCombat ?? ReflexCombatTarget.Default;
        string? refuse = this.ReflexG5CommonRefusal(request, CombatReflexAdmission.Prefix, this.ReflexCombatReady, request.ReflexCombat is not null)
            ?? CombatReflexAdmission.Refuse(this.SampleCombatAdmission(args.HpFloorPct), args.Escape, args.Disengage)
            ?? this.backgroundMove!.ReflexPadAdmission(out _);
        this.Monitor.Log($"arti_command reflex_combat request_id={request.RequestId} max_s={args.MaxSeconds} hp_floor_pct={args.HpFloorPct} "
            + $"max_kills={args.MaxKills} retreat={args.Retreat} refuse={refuse ?? "-"}", refuse is null ? LogLevel.Info : LogLevel.Warn);
        if (refuse is not null)
        {
            this.RejectReflexG5(request, refuse);
            return;
        }
        this.CombatRunner.Start(request, args);
    }

    /// <summary>Inbox dispatch of reflex_charge: admit (nothing pressed on refusal) and start.</summary>
    private void ArmReflexChargeFromCommand(MenuStepCommandRequest request)
    {
        this.ApplyActionOwnerRecords();
        ChargeReflexArgs args = request.ReflexCharge ?? ReflexChargeTarget.ToArgs(1, ReflexContracts.ChargeMaxTicksDefault);
        string? refuse = this.ReflexG5CommonRefusal(request, ChargeReflexAdmission.Prefix, this.ReflexChargeReady, request.ReflexCharge is not null)
            ?? ChargeReflexAdmission.Refuse(this.SampleChargeAdmission(args.Level))
            ?? this.backgroundMove!.ReflexPadAdmission(out _);
        this.Monitor.Log($"arti_command reflex_charge request_id={request.RequestId} target_level={args.Level} max_ticks={args.HoldBudgetTicks} "
            + $"refuse={refuse ?? "-"}", refuse is null ? LogLevel.Info : LogLevel.Warn);
        if (refuse is not null)
        {
            this.RejectReflexG5(request, refuse);
            return;
        }
        this.ChargeRunner.Start(request, args);
    }

    // ------------------------------------------------------------------ per-tick hooks

    /// <summary>UpdateTicked: a background terminal of a WS-G5 reflex press goes to its runner, never to the inbox.</summary>
    private bool RouteReflexG5Terminal(BackgroundMoveTerminal? terminal)
    {
        if (terminal is null)
            return false;
        if (this.combatRunner?.OwnsTerminal(terminal) == true)
        {
            this.combatRunner.OnPressTerminal(terminal);
            return true;
        }
        if (this.chargeRunner?.OwnsTerminal(terminal) == true)
        {
            this.chargeRunner.OnPressTerminal(terminal);
            return true;
        }
        return false;
    }

    /// <summary>UpdateTicked: write a WS-G5 verdict once it is ready; publish its terminal receipt.</summary>
    private void FlushReflexG5Outcome()
    {
        foreach (ReflexG5Outcome? o in new[] { this.combatRunner?.TakeOutcome(), this.chargeRunner?.TakeOutcome() })
        {
            if (o is null)
                continue;
            if (this.TakeBodyGuardOutcome(o))
                continue;   // ADR 179: the mod's own leg publishes a fact, never an inbox result
            if (!this.commandInbox.HasInflight || !string.Equals(this.commandInbox.InflightRequestId, o.RequestId, StringComparison.Ordinal))
            {
                this.Monitor.Log($"reflex outcome without matching inflight request: request_id={o.RequestId} verdict={o.Verdict}", LogLevel.Warn);
                continue;
            }
            this.WriteCommandResult(this.commandInbox.CompleteReflexG5(o.Verdict, o.CleanupConfirmed, o.PressCount, o.EffectObserved, o.Reflex,
                this.CurrentCommandIdentity(), NowUnixMs()));
        }
    }

    /// <summary>ReturnedToTitle: release now; the generic interrupted result closes the inbox slot.</summary>
    private void AbortReflexG5ForLifecycle(string reason)
    {
        if (this.combatRunner?.Active == true)
        {
            this.combatRunner.Abort(reason);
            _ = this.combatRunner.TakeOutcome();
        }
        if (this.chargeRunner?.Active == true)
        {
            this.chargeRunner.Abort(reason);
            _ = this.chargeRunner.TakeOutcome();
        }
    }

    // ------------------------------------------------------------------ ui.context

    /// <summary>ui.context: capabilities reflex_combat / reflex_charge and the "combat" / "charge" facts (only with the flags).</summary>
    void EnrichCombatChargeContext(Dictionary<string, object> snapshot)
    {
        Dictionary<string, object>? caps = snapshot.GetValueOrDefault("capabilities") as Dictionary<string, object>;
        if (this.config.ReflexCombatEnabled)
        {
            CombatAdmissionFacts f = this.SampleCombatAdmission(ReflexContracts.CombatHpFloorPctDefault);
            string? reason = this.combatRunner?.Active == true ? CombatReflexAdmission.Prefix + ":active" : CombatReflexAdmission.Refuse(f);
            if (caps is not null)
                caps[ReflexCombatTarget.Primitive] = new Dictionary<string, object>
                {
                    ["implemented"] = true, ["available"] = reason is null && this.ReflexCombatReady,
                    ["escape_available"] = this.combatRunner?.Active != true && this.ReflexCombatReady
                        && CombatReflexAdmission.Refuse(f, escape: true) is null,
                    // ADR 177: the body's own walk out of contact (no weapon / time / HP-floor term).
                    ["disengage_available"] = this.combatRunner?.Active != true && this.ReflexCombatReady
                        && CombatReflexAdmission.Refuse(f, disengage: true) is null,
                    ["reason"] = reason ?? "pad_mode_and_cursor_rechecked_at_admission",
                };
            CombatGame g = this.CombatGameAdapter;
            (float x, float y) = g.Position;
            IReadOnlyList<MonsterView> monsters = g.Monsters();
            if (f.MeleeWeaponHeld || g.InMines || Game1.player?.currentLocation?.Name == "Mine"
                || monsters.Count > 0)
            {
                Dictionary<string, object> combat = new(StringComparer.Ordinal)
                {
                    ["in_mines"] = g.InMines,
                    ["mine_level"] = Game1.player?.currentLocation is MineShaft ms ? ms.mineLevel : 0,
                    ["time_of_day"] = Game1.timeOfDay,
                    ["hp"] = g.Health, ["max_hp"] = g.MaxHealth,
                    ["times_unconscious"] = Game1.player?.stats.Get("timesUnconscious") ?? 0u,
                    ["weapon_held"] = f.MeleeWeaponHeld,
                    ["weapon"] = f.MeleeWeaponHeld ? Game1.player!.CurrentTool.QualifiedItemId : "",
                    ["held_name"] = Game1.player?.CurrentItem?.DisplayName ?? "empty hand",
                    ["weapons"] = Game1.player!.Items.OfType<MeleeWeapon>()
                        .Where(w => !CombatWeapon.IsScythe(w.QualifiedItemId))
                        .Select(w => new Dictionary<string, object> {
                            ["name"] = w.DisplayName, ["qualified_item_id"] = w.QualifiedItemId,
                            ["min_damage"] = w.minDamage.Value, ["max_damage"] = w.maxDamage.Value }).ToArray(),
                    ["scythe_held"] = f.ScytheHeld,
                    ["monsters"] = CombatFacts.MonstersWire(monsters, x, y),
                    ["food_slot"] = g.FoodSlot,
                    ["start_window"] = CombatTimeRule.StartRefusal(g.InMines, Game1.timeOfDay) is null,
                    ["retreat_due"] = CombatTimeRule.RetreatDue(g.InMines, Game1.timeOfDay),
                };
                if (g.ExitTile is { } exit)
                    combat["exit_tile"] = new[] { exit.X, exit.Y };
                if (Game1.player?.CurrentTool is MeleeWeapon weapon)
                    combat["weapon_stats"] = new Dictionary<string, object>
                    {
                        ["name"] = weapon.DisplayName, ["qualified_item_id"] = weapon.QualifiedItemId,
                        ["min_damage"] = weapon.minDamage.Value, ["max_damage"] = weapon.maxDamage.Value,
                    };
                if (this.bodyGuardFact is { } guardFact)
                    combat["body_guard"] = guardFact;   // ADR 179: what the body did by itself, for the runtime's next read
                snapshot["combat"] = combat;
            }
        }
        if (this.config.ReflexChargeEnabled)
        {
            ChargeGame g = new(this);
            int cap = ChargeReflexMachine.IsChargeable(g.ToolKind, g.UpgradeLevel)
                ? Math.Min(ReflexContracts.ChargeTargetLevelMax, ChargeReflexMachine.Cap(g.UpgradeLevel, g.Reaching)) : 0;
            ChargeAdmissionFacts f = this.SampleChargeAdmission(Math.Max(1, cap));
            string? reason = this.chargeRunner?.Active == true ? ChargeReflexAdmission.Prefix + ":active"
                : ChargeReflexAdmission.Refuse(this.SampleChargeAdmission(1));
            if (caps is not null)
                caps[ReflexChargeTarget.Primitive] = new Dictionary<string, object>
                {
                    ["implemented"] = true, ["available"] = reason is null && this.ReflexChargeReady,
                    ["reason"] = reason ?? "pad_mode_and_cursor_rechecked_at_admission",
                };
            if (g.ToolKind is ChargeToolKind.Hoe or ChargeToolKind.WateringCan)
            {
                Dictionary<string, object> levels = new(StringComparer.Ordinal);
                for (int level = 1; level <= cap; level++)
                    levels[level.ToString(System.Globalization.CultureInfo.InvariantCulture)] =
                        (object?)ChargeReflexAdmission.Refuse(level == cap ? f : this.SampleChargeAdmission(level)) ?? "ok";
                Dictionary<string, object> charge = new(StringComparer.Ordinal)
                {
                    ["tool"] = g.ToolKind, ["upgrade_level"] = g.UpgradeLevel, ["reaching"] = g.Reaching, ["cap"] = cap,
                    ["levels"] = levels,
                };
                if (g.ToolTile is { } t)
                {
                    charge["tool_tile"] = new[] { t.X, t.Y };
                    charge["facing"] = t.Facing;
                }
                snapshot["charge"] = charge;
            }
        }
    }

    // ------------------------------------------------------------------ the real game behind the runners

    /// <summary>Read-only combat facts (game thread only). Monster ids: one token per Monster instance (session-stable).</summary>
    private sealed class CombatGame : ICombatGame
    {
        private readonly ModEntry mod;
        private readonly ConditionalWeakTable<Monster, string> ids = new();
        private int nextId;

        public CombatGame(ModEntry mod) => this.mod = mod;

        // FIXB15 review: the cached nav grid (when published for this location) answers inside its window; elsewhere the
        // same per-tile rule on demand. Not gated by WorldExtrasEnabled: the reflex route is part of reflex_combat
        // (ReflexCombatEnabled), the grid is only its cache.
        public char NavCell(int x, int y) =>
            P?.currentLocation is not GameLocation loc ? '.'
            : this.mod.NavGridCell(loc, x, y) is char cached ? cached : NavCellAt(loc, x, y);

        private static Farmer? P => Context.IsWorldReady ? Game1.player : null;

        public long Tick => Game1.ticks;
        public bool WindowActive => Game1.game1?.IsActive == true;
        public bool EventUp => Game1.eventUp || Game1.CurrentEvent is not null || Game1.currentMinigame is not null || Game1.farmEvent is not null;
        public string MenuKind => Game1.activeClickableMenu is not null || Game1.dialogueUp ? CombatMenuKind.Other : CombatMenuKind.None;
        public string Location => P?.currentLocation?.NameOrUniqueName ?? "";
        public bool InMines => P?.currentLocation is MineShaft;
        public int TimeOfDay => Game1.timeOfDay;
        public int Health => P?.health ?? 0;
        public int MaxHealth => P?.maxHealth ?? 0;
        public bool KillScreen => Game1.killScreen;
        public bool UsingTool => P?.UsingTool == true;
        public bool WeaponHeld => P?.CurrentTool is MeleeWeapon;  // owner 2026-10-23: the scythe counts as a weapon

        public (float X, float Y) Position
        {
            get
            {
                if (P is not { } p)
                    return (0f, 0f);
                Point s = p.StandingPixel;
                return (s.X / 64f, s.Y / 64f);
            }
        }

        public int Facing => P?.FacingDirection ?? 2;

        public IReadOnlyList<MonsterView> Monsters()
        {
            GameLocation? loc = P?.currentLocation;
            if (loc is null)
                return Array.Empty<MonsterView>();
            List<MonsterView> list = new();
            foreach (NPC c in loc.characters)
            {
                if (c is not Monster m || !m.IsMonster)
                    continue;
                string id = this.ids.GetValue(m, _ => "m" + (++this.nextId).ToString(System.Globalization.CultureInfo.InvariantCulture));
                Point s = m.StandingPixel;
                list.Add(new MonsterView(id, m.GetType().Name, s.X / 64f, s.Y / 64f, m.Health, m.MaxHealth, m.isInvincible(),
                    m.isGlider.Value,   // post-Sat F2: the game's flying flag (Bat, Fly, Ghost, ...)
                    m.IsInvisible));     // ADR 153: a burrowed Duggy (damageMonster skips IsInvisible)
            }
            return list;
        }

        public int FoodSlot
        {
            get
            {
                if (P is not { } p)
                    return -1;
                try
                {
                    // Review minor 4: the WS3b eat policy (cheap, non-artisan / quest / bundle / usable), toolbar only.
                    var facts = new List<BgEatCandidateFacts?>();
                    for (int i = 0; i < Math.Min(CombatFood.ToolbarSlots, p.Items.Count); i++)
                        facts.Add(p.Items[i] is SObject o ? EatCandidateFactsOf(o) : null);
                    return CombatFood.FirstEligibleSlot(facts);
                }
                catch
                {
                    return -1;
                }
            }
        }

        public bool LocationRefused => P?.currentLocation is VolcanoDungeon;

        public long MonstersKilledStat
        {
            get
            {
                try
                {
                    return Game1.stats is { } stats ? stats.MonstersKilled : -1;
                }
                catch
                {
                    return -1;
                }
            }
        }

        public (int X, int Y)? ExitTile => P?.currentLocation is MineShaft ms
            ? ((int)ms.tileBeneathLadder.X, (int)ms.tileBeneathLadder.Y)
            : null;

        public int Money => P?.Money ?? 0;
        public int ItemCount => P?.Items.Where(i => i is not null).Sum(i => i!.Stack) ?? 0;
        public void Log(string line, bool warn = false) => this.mod.Monitor.Log(line, warn ? LogLevel.Warn : LogLevel.Info);
    }

    /// <summary>Read-only charge facts (game thread only).</summary>
    private sealed class ChargeGame : IChargeGame
    {
        private readonly ModEntry mod;

        public ChargeGame(ModEntry mod) => this.mod = mod;

        private static Farmer? P => Context.IsWorldReady ? Game1.player : null;

        public long Tick => Game1.ticks;
        public bool WindowActive => Game1.game1?.IsActive == true;
        public bool EventUp => Game1.eventUp || Game1.CurrentEvent is not null || Game1.currentMinigame is not null || Game1.farmEvent is not null;
        public bool MenuOpen => Game1.activeClickableMenu is not null || Game1.dialogueUp;

        public string ToolKind => P?.CurrentTool switch
        {
            Hoe => ChargeToolKind.Hoe,
            WateringCan => ChargeToolKind.WateringCan,
            Axe => ChargeToolKind.Axe,
            Pickaxe => ChargeToolKind.Pickaxe,
            _ => ChargeToolKind.Other,
        };

        public int UpgradeLevel => P?.CurrentTool?.UpgradeLevel ?? 0;

        public bool Reaching
        {
            get
            {
                try
                {
                    return P?.CurrentTool?.hasEnchantmentOfType<StardewValley.Enchantments.ReachingToolEnchantment>() == true;
                }
                catch
                {
                    return false;
                }
            }
        }

        public int ToolPower => P?.toolPower.Value ?? 0;
        public bool UsingTool => P?.UsingTool == true;
        public float Stamina => P?.Stamina ?? 0f;
        public int? WaterLeft => P?.CurrentTool is WateringCan can ? can.WaterLeft : null;

        public (int X, int Y, int Facing)? ToolTile => BackgroundToolTileProbe.ToolTile(P) is { } hit && P is { } p
            ? (hit.X, hit.Y, p.FacingDirection)
            : null;

        public IReadOnlyList<ChargeTileFacts> SampleArea(IReadOnlyList<(int X, int Y)> tiles)
        {
            GameLocation? loc = P?.currentLocation;
            if (loc is null)
                return Array.Empty<ChargeTileFacts>();
            List<ChargeTileFacts> list = new(tiles.Count);
            foreach ((int x, int y) in tiles)
            {
                Vector2 v = new(x, y);
                bool onMap = loc.isTileOnMap(x, y);
                loc.terrainFeatures.TryGetValue(v, out TerrainFeature? tf);
                HoeDirt? dirt = tf as HoeDirt;
                list.Add(new ChargeTileFacts(x, y, onMap,
                    HasObject: loc.objects.ContainsKey(v),
                    HasTerrain: tf is not null,
                    HoeDirt: dirt is not null,
                    Crop: dirt?.crop is not null,
                    Watered: dirt?.state.Value == HoeDirt.watered,
                    Tillable: onMap && BackgroundToolTileProbe.Tillable(loc, x, y),
                    Villager: onMap && BackgroundToolTileProbe.VillagerOnTile(loc, x, y),
                    Refill: onMap && BackgroundToolTileProbe.WaterRefill(loc, x, y)));
            }
            return list;
        }

        public void Log(string line, bool warn = false) => this.mod.Monitor.Log(line, warn ? LogLevel.Warn : LogLevel.Info);
    }
}
